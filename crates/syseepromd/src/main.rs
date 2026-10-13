//! syseepromd, in Rust.  Ports `sonic-syseepromd/scripts/syseepromd`.
//!
//! The daemon is small because the work is not its own: the platform decodes
//! the EEPROM and publishes EEPROM_INFO itself, through
//! `Eeprom.update_eeprom_db()`.  What is left here is a period, an integrity
//! check, and a teardown -- which is exactly what the Python one is too.
//!
//! Republishing is not unconditional.  The table is a cache that `show platform
//! syseeprom` reads instead of touching hardware, and something else may have
//! cleared it; the daemon notices that by comparing the key set against the one
//! its own write produced, and only then rewrites.

use std::collections::BTreeSet;
use std::time::Duration;

use pmon_common::cycles::{Cycles, Tick};
use pmon_common::db::{self, TableLike};
use pmon_common::logging;

use clap::Parser;
use platform_api::PlatformApi;
use platform_provider::PlatformImpl;

const SYSLOG_IDENTIFIER: &str = "syseepromd";

/// The command line, which is one switch.
///
/// Which vendor package gets imported is not here and never was: the image
/// installs one `sonic_platform` and which one is its business.  What is here
/// is which *implementation* of the platform API to open, because a platform
/// with a native Rust one has to be able to say so without a new binary.
#[derive(Parser, Debug)]
#[command(name = "syseepromd-rs", about = "SONiC system EEPROM daemon, in Rust")]
struct Args {
    /// Which platform API implementation to use: `pyo3` or `native`.
    ///
    /// Filled in from `platform_api_syseepromd` in `pmon_daemon_control.json`
    /// by the supervisord template.  Absent means `pyo3`, which is what every
    /// platform runs today -- an unset switch has to leave a platform on the
    /// implementation it has always had.
    #[arg(long, default_value_t = PlatformImpl::Pyo3)]
    platform_api: PlatformImpl,
}

const EEPROM_TABLE: &str = "EEPROM_INFO";

/// `syseepromd:EEPROM_INFO_UPDATE_PERIOD_SECS`.
const UPDATE_PERIOD: Duration = Duration::from_secs(60);

/// `syseepromd:ERR_EEPROM_LOAD`, the one the Python daemon exits with when it
/// cannot get an EEPROM at all.  supervisord's `autorestart=unexpected` reads
/// the code, so it has to keep meaning the same thing.
const ERR_EEPROM_LOAD: i32 = 5;

/// What a daemon that cannot reach CONFIG_DB/STATE_DB leaves with.
///
/// Python has no constant for this because it does not choose: the write
/// raises, nothing catches it, and the interpreter exits 1.  The value has to
/// be non-zero for supervisord's `autorestart=unexpected` to restart us, and
/// restarting is the whole point -- `DBConnector` never reconnects, so a new
/// process is the only way back to a working redis.
const ERR_DB_WRITE: i32 = 1;

/// Every key in the table, so a later cycle can tell the table the platform's
/// writer produced apart from one something else has edited.
///
/// An unreadable table is an error, never an empty key set.  Read as empty it
/// would pass the integrity check whenever the cache was empty too -- nothing
/// would look changed, and a daemon that had lost its redis would stay up
/// publishing nothing.  Python's `getKeys()` is unwrapped wherever it is called
/// (`syseepromd:DaemonSyseeprom.post_eeprom_to_db`,
/// `syseepromd:DaemonSyseeprom.clear_db`,
/// `syseepromd:DaemonSyseeprom.detect_eeprom_table_integrity`), so a read it
/// cannot do ends the process; the callers here do the same with the error.
fn snapshot(table: &dyn TableLike) -> Result<BTreeSet<String>, String> {
    let keys = table
        .get_keys()
        .map_err(|e| format!("failed to read the keys of {EEPROM_TABLE}: {e}"))?;
    Ok(keys.into_iter().collect())
}

/// Remove every row, as `syseepromd:DaemonSyseeprom.clear_db` does.
///
/// Stops at the first row that will not go, because Python does: `_del` there
/// is not wrapped, so the exception leaves `clear_db`, leaves `run`, and ends
/// the process.  Carrying on past a failed delete would leave the table half
/// cleared and the daemon believing it is empty.
fn clear(table: &dyn TableLike) -> Result<(), String> {
    for key in snapshot(table)? {
        table
            .del(&key)
            .map_err(|e| format!("failed to delete {EEPROM_TABLE}|{key}: {e}"))?;
    }
    Ok(())
}

/// Ask the platform to republish, and remember what that produced.
///
/// `Ok(None)` when the platform could not read or decode the EEPROM.  The
/// Python daemon logs ERR_FAILED_EEPROM / ERR_FAILED_UPDATE_DB and carries on
/// with whatever the table already held, which is better than half of one.
///
/// `Err` when the platform wrote but the table cannot be read back.  That is
/// the daemon's redis, not the platform's EEPROM, and `snapshot` explains why
/// it is not folded into "nothing published": Python's `getKeys()` after the
/// write (`syseepromd:DaemonSyseeprom.post_eeprom_to_db`) is unwrapped too.
fn publish(
    plat: &mut dyn PlatformApi,
    table: &dyn TableLike,
) -> Result<Option<BTreeSet<String>>, String> {
    // `true` is success.  `Eeprom.update_eeprom_db` returns 0 for success, and
    // the facade inverts it (`return not eeprom.update_eeprom_db(data)` in
    // platform_api/_escape_hatch.py).
    match plat.eeprom_update_db() {
        Ok(true) => snapshot(table).map(Some),
        Ok(false) => {
            log::error!("Failed to post system EEPROM info to database");
            Ok(None)
        }
        Err(e) => {
            log::error!("Failed to post system EEPROM info to database: {e}");
            Ok(None)
        }
    }
}

/// Rewrite the table if it is no longer the one this daemon published.
///
/// Returns whether it rewrote.  The comparison is against the key set the
/// platform's own writer produced, which is the only way to tell "somebody
/// cleared this" from "the platform reports fewer TLVs than it used to".
fn republish_if_changed(
    plat: &mut dyn PlatformApi,
    table: &dyn TableLike,
    published: &mut BTreeSet<String>,
) -> Result<bool, String> {
    if snapshot(table)? == *published {
        return Ok(false);
    }
    log::info!("System EEPROM table was changed, needs update");
    clear(table)?;
    if let Some(keys) = publish(plat, table)? {
        *published = keys;
    }
    Ok(true)
}

/// The daemon's loop, with everything it needs handed to it.
///
/// Separated from `main` so it can be driven by a test: `main` is the part
/// that cannot be -- it embeds an interpreter and opens a redis -- and this is
/// the part worth being sure of.
async fn run(
    platform: &mut dyn PlatformApi,
    table: &dyn TableLike,
    cycles: &mut Cycles,
) -> i32 {
    // Post once at start-up, before the first wait -- `show platform syseeprom`
    // must not have to sit through a minute of nothing on a fresh container.
    //
    // A table that cannot be read back is a lost redis here as much as in the
    // loop, and gets the same exit; see below.
    let code = match publish(platform, table) {
        Err(e) => {
            log::error!("{e}");
            ERR_DB_WRITE
        }
        Ok(keys) => {
            let mut published = keys.unwrap_or_default();
            loop {
                match cycles.next(UPDATE_PERIOD).await {
                    Tick::Exit(code) => break code,
                    Tick::Cycle => {}
                }

                // A table we cannot write is a redis we have lost, and
                // `DbConnector` does not reconnect -- so leaving is how we get
                // a working one, the same way Python's unwrapped `_del` ends
                // the process and lets supervisord start it again.  Staying
                // would mean a daemon that is up and publishing nothing.
                if let Err(e) = republish_if_changed(platform, table, &mut published) {
                    log::error!("{e}");
                    break ERR_DB_WRITE;
                }
            }
        }
    };

    // The table is this daemon's, and a stale one is worse than none: whoever
    // reads it next would get an EEPROM nobody is refreshing.  Python does this
    // from `syseepromd:DaemonSyseeprom.__del__`.
    //
    // A failure here is only logged: the process is already on its way out, so
    // there is no state left to protect and no restart that would help.
    if let Err(e) = clear(table) {
        log::warn!("{e}");
    }
    code
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let args = Args::parse();
    logging::init(SYSLOG_IDENTIFIER);
    log::info!("Starting up...");

    let mut platform = match platform_provider::open(SYSLOG_IDENTIFIER, args.platform_api) {
        Ok(p) => p,
        Err(e) => {
            log::error!("Failed to load platform-specific eeprom: {e}");
            std::process::exit(ERR_EEPROM_LOAD);
        }
    };

    // After the platform is opened, not before as Python's `DaemonBase`
    // orders it, and the order is load-bearing.  Opening the PyO3 bridge ends
    // with `signal.signal(SIGTERM/SIGINT, SIG_DFL)` -- a real `sigaction`,
    // undoing whatever a vendor chassis installed -- and tokio installs its
    // own `sigaction` once per signal, on first registration.  Registered
    // first, the handlers would be overwritten and never put back, and every
    // SIGTERM for the life of the process would kill it before the teardown
    // below runs.  What the Python order buys -- a clean exit for a SIGTERM
    // during the import -- has nothing to clean up here: nothing has been
    // published yet.
    let code = start(
        &mut platform,
        &db::open,
        &mut Cycles::signals().expect("failed to install the signal handlers"),
    )
    .await;

    log::info!("Shutting down...");
    // Whatever the implementation set up, released before the process goes
    // away.  The PyO3 bridge runs Python's `atexit` handlers here; `main` does
    // not need to know that is what it holds.
    platform.finalize();
    std::process::exit(code);
}

/// Open the table and run.
///
/// One decision, and it is the exit code: a table that will not open gets
/// ERR_EEPROM_LOAD, the same code Python leaves with when it cannot get an
/// EEPROM at all (`syseepromd:ERR_EEPROM_LOAD`), because supervisord's
/// `autorestart=unexpected` reads the code and it has to keep meaning the same
/// thing.
async fn start(platform: &mut dyn PlatformApi, open: db::Opener<'_>, cycles: &mut Cycles) -> i32 {
    let table = match open(db::STATE_DB, EEPROM_TABLE) {
        Ok(t) => t,
        Err(e) => {
            log::error!("Failed to open {EEPROM_TABLE}: {e}");
            return ERR_EEPROM_LOAD;
        }
    };
    run(platform, table.as_ref(), cycles).await
}

// ── Tests ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use pmon_common::db::MockTable;
    use platform_api::PlatformError;

    /// A platform whose writer fills the table, so the daemon's own logic --
    /// snapshot, compare, clear, republish -- can be exercised without a redis
    /// or an interpreter.
    struct FakePlatform {
        table: MockTable,
        rows: Vec<&'static str>,
        ok: bool,
        /// Answer with this error instead of writing.
        error: Option<PlatformError>,
        calls: usize,
    }

    impl FakePlatform {
        fn new(table: MockTable, rows: Vec<&'static str>) -> Self {
            Self { table, rows, ok: true, error: None, calls: 0 }
        }
    }

    impl PlatformApi for FakePlatform {
        fn eeprom_update_db(&mut self) -> Result<bool, PlatformError> {
            self.calls += 1;
            if let Some(e) = self.error.clone() {
                return Err(e);
            }
            if !self.ok {
                return Ok(false);
            }
            for r in &self.rows {
                // Ignored, not unwrapped: a vendor's writer that cannot reach
                // redis returns an error, it does not abort the process.  The
                // daemon's own reaction to that is what these tests are about.
                let _ = self.table.set(r, &[("Value", "x".to_string())]);
            }
            Ok(true)
        }
    }

    #[test]
    fn publishing_records_the_keys_the_platform_wrote() {
        let t = MockTable::new();
        let mut p = FakePlatform::new(t.clone(), vec!["TlvHeader", "0x21"]);
        let keys = publish(&mut p, &t).unwrap().expect("the platform wrote the table");
        assert_eq!(keys, ["0x21".to_string(), "TlvHeader".to_string()].into());
    }

    /// The daemon must not adopt the key set of a write that failed, or the
    /// next cycle compares against something that was never published and it
    /// stops noticing that the table is wrong.
    #[test]
    fn a_failed_write_yields_no_key_set() {
        let t = MockTable::new();
        let mut p = FakePlatform::new(t.clone(), vec!["0x21"]);
        p.ok = false;
        assert!(publish(&mut p, &t).unwrap().is_none());
    }

    /// A platform that errors rather than answering `false` -- the bridge's
    /// mapping of a Python exception -- is the same failed write: no key set
    /// to adopt, and the error in the log so the cause is not lost.
    #[test]
    fn a_platform_error_yields_no_key_set_and_logs_why() {
        let log = pmon_common::logging::capture();
        let t = MockTable::new();
        let mut p = FakePlatform::new(t.clone(), vec!["0x21"]);
        p.error = Some(PlatformError::Backend("EEPROM checksum mismatch".into()));
        assert!(publish(&mut p, &t).unwrap().is_none());
        assert!(t.is_empty(), "nothing was written");
        assert!(log.logged(
            log::Level::Error,
            "Failed to post system EEPROM info to database: "
        ));
        assert!(log.logged(log::Level::Error, "EEPROM checksum mismatch"));
    }

    /// The platform wrote, but the table cannot be read back: that is the
    /// daemon's redis gone, not an EEPROM it could not decode, and it is an
    /// error rather than "nothing published".
    #[test]
    fn a_table_that_cannot_be_read_back_after_the_write_is_an_error() {
        let t = MockTable::new();
        let mut p = FakePlatform::new(t.clone(), vec!["0x21"]);
        t.fail_reads("redis is gone");
        let e = publish(&mut p, &t).expect_err("the write cannot be confirmed");
        assert!(e.contains("redis is gone"), "{e}");
    }

    /// And at start-up that ends the daemon with the database code, as the
    /// same failure in the loop does: Python's unwrapped `getKeys()` after the
    /// first post raises out of `__init__` and the process exits non-zero.
    #[tokio::test]
    async fn a_table_that_cannot_be_read_at_startup_ends_the_daemon() {
        let t = MockTable::new();
        let mut p = FakePlatform::new(t.clone(), vec!["0x21"]);
        t.fail_reads("redis is gone");
        // No cycles: the exit has to come from the start-up publish itself,
        // not from the first cycle's integrity check tripping over the same
        // unreadable table a minute later.
        let mut cycles = Cycles::Fixed { remaining: 0, code: 143 };
        assert_eq!(run(&mut p, &t, &mut cycles).await, ERR_DB_WRITE);
    }

    /// The loop, driven end to end: publish at start-up, notice the table has
    /// been emptied, rewrite it, and clear on the way out.
    #[tokio::test]
    async fn the_loop_publishes_at_startup_and_clears_on_the_way_out() {
        let t = MockTable::new();
        let mut p = FakePlatform::new(t.clone(), vec!["TlvHeader", "0x21"]);
        let mut cycles = Cycles::Fixed { remaining: 0, code: 143 };

        let code = run(&mut p, &t, &mut cycles).await;
        assert_eq!(code, 143, "the exit code is the signal's");
        assert_eq!(p.calls, 1, "published once, before the first wait");
        assert!(t.is_empty(), "a stale EEPROM is worse than no EEPROM");
    }

    /// The trigger: something else emptied the table, and the daemon notices
    /// by comparing against the key set its own write produced.
    #[test]
    fn a_table_someone_emptied_is_rewritten() {
        let t = MockTable::new();
        let mut p = FakePlatform::new(t.clone(), vec!["TlvHeader", "0x21"]);
        let mut published = publish(&mut p, &t).unwrap().unwrap();

        assert!(!republish_if_changed(&mut p, &t, &mut published).unwrap(), "nothing changed");
        assert_eq!(p.calls, 1);

        t.del("0x21").unwrap();
        assert!(republish_if_changed(&mut p, &t, &mut published).unwrap());
        assert_eq!(p.calls, 2);
        assert_eq!(t.keys(), vec!["0x21".to_string(), "TlvHeader".to_string()]);
    }

    /// A platform that has genuinely stopped reporting a TLV also trips this,
    /// and the answer is the same: republish and adopt what came back.  The
    /// key set is a statement about what this daemon wrote, not about the
    /// EEPROM.
    #[test]
    fn a_platform_that_reports_fewer_tlvs_settles_on_the_new_set() {
        let t = MockTable::new();
        let mut p = FakePlatform::new(t.clone(), vec!["TlvHeader", "0x21"]);
        let mut published = publish(&mut p, &t).unwrap().unwrap();

        p.rows = vec!["TlvHeader"];
        t.del("0x21").unwrap();
        assert!(republish_if_changed(&mut p, &t, &mut published).unwrap());
        assert_eq!(published, ["TlvHeader".to_string()].into());
        assert!(!republish_if_changed(&mut p, &t, &mut published).unwrap(), "and settles");
    }

    /// And an untouched table is left alone: republishing every minute would
    /// churn STATE_DB and wake every subscriber for nothing.
    #[tokio::test]
    async fn an_untouched_table_is_not_rewritten_by_the_loop() {
        let t = MockTable::new();
        let mut p = FakePlatform::new(t.clone(), vec!["TlvHeader"]);
        let mut cycles = Cycles::Fixed { remaining: 5, code: 0 };
        run(&mut p, &t, &mut cycles).await;
        assert_eq!(p.calls, 1, "five cycles, one publish");
    }

    /// A platform that cannot answer does not get its failure adopted as the
    /// key set: the next cycle would then compare against something that was
    /// never published and stop noticing that the table is wrong.
    #[tokio::test]
    async fn a_platform_that_fails_at_startup_still_runs() {
        let t = MockTable::new();
        let mut p = FakePlatform::new(t.clone(), vec!["0x21"]);
        p.ok = false;
        let mut cycles = Cycles::Fixed { remaining: 2, code: 0 };
        assert_eq!(run(&mut p, &t, &mut cycles).await, 0);
        assert!(t.is_empty());
    }

    #[test]
    fn teardown_leaves_nothing_behind() {
        let t = MockTable::new();
        let mut p = FakePlatform::new(t.clone(), vec!["TlvHeader", "0x21", "Checksum"]);
        publish(&mut p, &t).unwrap().unwrap();
        clear(&t).unwrap();
        assert!(t.is_empty(), "a stale EEPROM is worse than no EEPROM");
    }

    // ── the wiring that used to be inside main ───────────────────────────────

    /// A table that will not open is the load error, the same code Python
    /// leaves with when it cannot get an EEPROM at all: supervisord's
    /// `autorestart=unexpected` reads it, so it has to keep meaning the same
    /// thing.
    #[tokio::test]
    async fn a_table_that_will_not_open_is_the_load_error() {
        let log = pmon_common::logging::capture();
        let o = pmon_common::db::MockOpener::failing(EEPROM_TABLE);
        let mut p = FakePlatform::new(MockTable::new(), vec![]);
        let code =
            start(&mut p, &|d, t| o.open(d, t), &mut Cycles::Fixed { remaining: 0, code: 0 }).await;
        assert_eq!(code, ERR_EEPROM_LOAD);
        assert!(log.logged(log::Level::Error, "Failed to open EEPROM_INFO"));
    }

    /// And the table it does open is EEPROM_INFO on STATE_DB.  The vendor's
    /// `eeprom.py:Eeprom.get_system_eeprom_info` prefers this table over the
    /// hardware, so a daemon publishing to the wrong one would leave
    /// `get_serial()` and `get_base_mac()` reading rows nobody refreshes.
    #[tokio::test]
    async fn the_table_is_eeprom_info_on_state_db() {
        let o = pmon_common::db::MockOpener::new();
        let mut p = FakePlatform::new(o.table("x").unwrap_or_default(), vec![]);
        let code =
            start(&mut p, &|d, t| o.open(d, t), &mut Cycles::Fixed { remaining: 1, code: 143 }).await;
        assert_eq!(code, 143);
        assert!(o.asked().contains(&(db::STATE_DB.to_string(), EEPROM_TABLE.to_string())));
    }

    /// Clearing a reachable table empties it.
    #[test]
    fn clearing_removes_every_row() {
        let t = MockTable::new();
        t.set("0x21", &[("Value", "MSN4700".to_string())]).unwrap();
        t.set("0x22", &[("Value", "LC".to_string())]).unwrap();
        clear(&t).unwrap();
        assert!(t.is_empty());
    }

    /// A row that will not delete stops the clear and is reported.
    ///
    /// This is Python's behaviour, not a choice of ours:
    /// `syseepromd:DaemonSyseeprom.clear_db` does not wrap `_del`, so the first
    /// failure leaves the function and ends the daemon.  A delete that fails
    /// means redis is gone, and since `DbConnector` never reconnects, every
    /// later row would fail too -- continuing only buys a longer log.
    #[test]
    fn a_row_that_will_not_delete_stops_the_clear() {
        let t = MockTable::new();
        t.set("0x21", &[("Value", "MSN4700".to_string())]).unwrap();
        t.fail_writes("redis is gone");
        let e = clear(&t).expect_err("a table that will not delete has to be reported");
        assert!(e.contains("0x21"), "the error names the row it stopped on: {e}");
    }

    /// And a key list that cannot be read is reported, rather than read as an
    /// empty table and the table called cleared when nothing looked.
    #[test]
    fn a_table_that_cannot_be_read_stops_the_clear() {
        let t = MockTable::new();
        t.set("0x21", &[("Value", "MSN4700".to_string())]).unwrap();
        t.fail_reads("redis is gone");
        let e = clear(&t).expect_err("a table that cannot be read has to be reported");
        assert!(e.contains("redis is gone"), "the error carries the cause: {e}");
        assert!(!t.is_empty(), "and nothing was removed");
    }

    /// The error names the table, as the delete error names the row: in the
    /// log it is the line before "Shutting down...", and "redis is gone"
    /// alone does not say which of the daemon's reads it was.
    #[test]
    fn a_key_read_that_fails_names_the_table() {
        let t = MockTable::new();
        t.fail_reads("redis is gone");
        let e = snapshot(&t).expect_err("unreadable");
        assert!(e.contains(EEPROM_TABLE), "{e}");
        let e = clear(&t).expect_err("unreadable");
        assert!(e.contains(EEPROM_TABLE), "{e}");
    }

    /// The snapshot is the integrity check, so it has to be able to fail:
    /// comparing an unreadable table against an empty cache would find them
    /// equal and decide nothing had changed.
    #[test]
    fn an_unreadable_table_is_not_an_unchanged_one() {
        let t = MockTable::new();
        let mut p = FakePlatform::new(t.clone(), vec!["0x21"]);
        let mut published = BTreeSet::new();
        t.fail_reads("redis is gone");
        republish_if_changed(&mut p, &t, &mut published)
            .expect_err("a table that cannot be read has not been checked");
    }

    /// And that failure is what takes the daemon down, so supervisord can
    /// restart it onto a fresh connection.
    ///
    /// The setup is the one real case: rows are in the table, the platform
    /// cannot republish (so `published` is empty and the snapshot will not
    /// match), and redis has gone away -- which is exactly when the clear that
    /// precedes a republish fails.
    #[tokio::test]
    async fn a_table_that_cannot_be_written_ends_the_daemon() {
        let o = pmon_common::db::MockOpener::new();
        // Registered through `open` so the handle below is the very table the
        // daemon will be given -- `MockOpener::table` only knows a name it has
        // already handed out.
        o.open(db::STATE_DB, EEPROM_TABLE).unwrap();
        let t = o.table(EEPROM_TABLE).expect("just opened");
        t.set("0x99", &[("Value", "stale".to_string())]).unwrap();
        let mut p = FakePlatform::new(t.clone(), vec![]);
        p.ok = false;
        t.fail_writes("redis is gone");
        let code = start(
            &mut p,
            &|d, tbl| o.open(d, tbl),
            &mut Cycles::Fixed { remaining: 2, code: 143 },
        )
        .await;
        assert_eq!(code, ERR_DB_WRITE, "a lost redis has to be a non-zero exit");
    }
}
