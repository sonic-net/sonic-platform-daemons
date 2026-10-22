//! psud, in Rust.  Ports `sonic-psud/scripts/psud`.
//!
//! PSU_INFO every three seconds, the PSU half of FAN_INFO, the PSU tray LEDs,
//! and -- on a modular chassis -- the power budget and the master LED.
//!
//! Three seconds is much faster than thermalctld's minute, and deliberately so:
//! a PSU being pulled has to reach STATE_DB before the next `show platform
//! psustatus`, which is also why this daemon writes the fan rows of a PSU it
//! saw change rather than leaving them to thermalctld
//! (`psud:DaemonPsud._update_single_power_entity_data` calls
//! `_update_psu_fan_data`).

#[cfg(all(test, feature = "parity"))]
mod parity;
mod power_budget;
mod status;
mod updater;

use std::time::Duration;

use platform_api::{
    ChassisInfoCols, FanDrawerInfoCols, FanInfoCols, ModuleInfoCols, PlatformApi, PlatformError, PowerEntityKind,
};
use clap::Parser;
use platform_provider::PlatformImpl;
use pmon_common::cycles::{Cycles, Tick};
use pmon_common::db::{self, TableLike};
use pmon_common::logging;
use pmon_common::report_once::Latch;

use power_budget::{PowerBudget, CHASSIS_POWER_KEY};
use updater::{Tables, Updater, CHASSIS_INFO_KEY};

const SYSLOG_IDENTIFIER: &str = "psud";

/// The command line, which is one switch.
///
/// Which vendor package gets imported is not here and never was: the image
/// installs one `sonic_platform` and which one is its business.  What is here
/// is which *implementation* of the platform API to open, because a platform
/// with a native Rust one has to be able to say so without a new binary.
#[derive(Parser, Debug)]
#[command(name = "psud-rs", about = "SONiC PSU daemon, in Rust")]
struct Args {
    /// Which platform API implementation to use: `pyo3` or `native`.
    ///
    /// Filled in from `platform_api_psud` in `pmon_daemon_control.json` by the
    /// supervisord template.  Absent means `pyo3`, which is what every platform
    /// runs today -- an unset switch has to leave a platform on the
    /// implementation it has always had.
    #[arg(long, default_value_t = PlatformImpl::Pyo3)]
    platform_api: PlatformImpl,
}

const CHASSIS_INFO_TABLE: &str = "CHASSIS_INFO";
const PSU_INFO_TABLE: &str = "PSU_INFO";
const FAN_INFO_TABLE: &str = "FAN_INFO";
const PHYSICAL_ENTITY_INFO_TABLE: &str = "PHYSICAL_ENTITY_INFO";

/// `psud:PSU_INFO_UPDATE_PERIOD_SECS`.
const UPDATE_PERIOD: Duration = Duration::from_secs(3);

/// `psud:PSUUTIL_LOAD_ERROR` and `psud:PSU_DB_CONNECT_ERROR`.  supervisord
/// reads the code, so the two have to keep meaning what they meant.
const PSUUTIL_LOAD_ERROR: i32 = 1;
const PSU_DB_CONNECT_ERROR: i32 = 2;

/// How many PSUs and PDBs the chassis has.
fn counts(psus: &[platform_api::PsuInfo]) -> (usize, usize) {
    let count = |k: PowerEntityKind| psus.iter().filter(|p| p.kind == k).count();
    (count(PowerEntityKind::Psu), count(PowerEntityKind::Pdb))
}

/// Write the counts; whether they were written.
///
/// A count and not a list: `show platform psustatus` iterates `PSU 1..n`, so a
/// wrong number here hides a PSU that is otherwise being published correctly.
fn publish_counts(psus: &[platform_api::PsuInfo], table: &dyn TableLike) -> bool {
    let (psu_num, pdb_num) = counts(psus);
    let fvs = [("psu_num", psu_num.to_string()), ("pdb_num", pdb_num.to_string())];
    match table.set(CHASSIS_INFO_KEY, &fvs) {
        Ok(()) => true,
        Err(e) => {
            log::error!("Failed to set PSU/PDB number to DB: {e}");
            false
        }
    }
}

/// The counts as last written, so they are published from the first read
/// that succeeds and again only if they change.
///
/// Python publishes them once, from `psud:DaemonPsud.__init__`, and can afford
/// to: `ChassisBase.get_num_psus` is `len(self._psu_list)`, which reads no
/// hardware and cannot fail.  The facade has no count -- it is
/// `len(get_psus())` -- and `get_psus()` reads every PSU, so here the count
/// inherits that read's failures.  Published from a failed read, it would be
/// `0`: numeric, so the FRU MIB and `show platform psustatus` iterate `1..0`
/// and every PSU vanishes while `PSU_INFO` fills in normally, and nothing would
/// ever correct it.
#[derive(Default)]
struct Counts(Option<(usize, usize)>);

impl Counts {
    fn refresh(&mut self, psus: &[platform_api::PsuInfo], table: &dyn TableLike) {
        let now = counts(psus);
        if self.0 != Some(now) && publish_counts(psus, table) {
            self.0 = Some(now);
        }
    }
}

/// The power budget, on the passes the chassis says it is modular.
///
/// Asked every pass, as `psud:DaemonPsud.run` asks `is_modular_chassis()`
/// every pass, rather than decided once at start-up: a failed read latched as
/// "fixed" would leave a modular chassis without a budget or a master PSU LED
/// for the life of the daemon.  Once built the budget is kept, as Python keeps
/// `psu_chassis_info`, because it carries the master LED's last colour.
///
/// A read that fails skips the budget for that pass.  In Python the exception
/// escapes `run()` and supervisord restarts the daemon; skipping one pass is
/// the same outcome without the restart.
#[derive(Default)]
struct Budget {
    budget: Option<PowerBudget>,
    read: Latch,
}

impl Budget {
    fn current(&mut self, platform: &mut dyn PlatformApi) -> Option<&mut PowerBudget> {
        match platform.get_chassis_info(CHASSIS_COLS) {
            Ok(c) => {
                pmon_common::recovered!(self.read, "Chassis read recovered");
                if !c.is_modular_chassis {
                    return None;
                }
            }
            // `ChassisBase.is_modular_chassis` answers False; a platform that
            // declines to answer is saying the same.
            Err(PlatformError::NotSupported(_)) => return None,
            Err(e) => {
                pmon_common::fail_once_warn!(
                    self.read,
                    "Failed to read whether the chassis is modular - {e}"
                );
                return None;
            }
        }
        Some(self.budget.get_or_insert_with(PowerBudget::default))
    }
}

/// Every handle this daemon writes through.
struct Handles {
    chassis: Box<dyn TableLike>,
    psu: Box<dyn TableLike>,
    fan: Box<dyn TableLike>,
    entity: Box<dyn TableLike>,
}

/// Open every table, naming the one that refused.
///
/// The set is fixed, so it is worth an assertion: a typo in a table name is
/// otherwise invisible until the daemon is on a switch, where it shows up as a
/// table nobody is writing.
fn open_all(open: db::Opener<'_>) -> Result<Handles, String> {
    let one = |name: &str| {
        open(db::STATE_DB, name).map_err(|e| format!("Failed to connect to STATE_DB: {e}"))
    };
    Ok(Handles {
        chassis: one(CHASSIS_INFO_TABLE)?,
        psu: one(PSU_INFO_TABLE)?,
        fan: one(FAN_INFO_TABLE)?,
        entity: one(PHYSICAL_ENTITY_INFO_TABLE)?,
    })
}

/// The one `ChassisInfo` column psud reads: whether to run a power budget.
const CHASSIS_COLS: ChassisInfoCols = ChassisInfoCols::IS_MODULAR_CHASSIS;

/// What the power budget needs off a fan drawer and a module: how much each
/// draws, and whether it is there to draw it.  `name` is always read.
const DRAWER_COLS: FanDrawerInfoCols =
    FanDrawerInfoCols::PRESENCE.with(FanDrawerInfoCols::MAXIMUM_CONSUMED_POWER);
const MODULE_COLS: ModuleInfoCols =
    ModuleInfoCols::PRESENCE.with(ModuleInfoCols::MAXIMUM_CONSUMED_POWER);

/// The `FanInfo` columns psud publishes.
///
/// It writes a PSU fan's name, direction, speed and LED, and nothing else --
/// `presence` and `status` in FAN_INFO are the *PSU's*, taken from the PSU row
/// (`psud:DaemonPsud._update_psu_fan_data`).  Declining the rest is not only
/// tidiness: `is_under_speed` reaches `psu{n}_fan_min`, which a switched-off
/// PSU leaves readable and empty, and Mellanox logs that read at ERR before
/// swallowing the exception.  Asking for a column psud discards turned a
/// 60-second race in thermalctld into a 3-second one under psud's syslog
/// identifier.
///
/// If this list and the fields the updater reads ever disagree, the missing
/// ones arrive as None and nothing says so; `psud_asks_for_every_column_it_reads`
/// is what keeps them together.
const FAN_COLS: FanInfoCols = FanInfoCols::DIRECTION
    .with(FanInfoCols::SPEED_PCT)
    .with(FanInfoCols::STATUS_LED);

/// What the LED read-back asks of a fan: its colour, and nothing else.
const FAN_LED_COLS: FanInfoCols = FanInfoCols::STATUS_LED;

/// One pass: publish, apply the LED colours, read them back, and -- on a
/// modular chassis -- account for the power budget.
///
/// The read-back is not redundant when something was written:
/// `get_status_led` answers what is on the device, so calling it after the
/// write is what makes the published colour the current one rather than the
/// one from before the pass.  The fans are re-read too, not only the PSUs: on
/// Mellanox a PSU fan has no LED of its own and answers with its PSU's, so a
/// PSU write changes the fan's colour as well.
///
/// When nothing was written the pass's own read already is the device's
/// colour, and reading again would double the per-cycle hardware traffic for
/// nothing -- Python asks only `get_status_led` of each PSU and PSU fan per
/// cycle (`psud:DaemonPsud._update_led_color`), not the whole row.  The fan
/// re-read is narrowed to that one column for the same reason.
fn one_pass(
    platform: &mut dyn PlatformApi,
    updater: &mut Updater,
    counts: &mut Counts,
    budget: &mut Budget,
    tables: &Tables<'_>,
    chassis_tbl: &dyn TableLike,
    read: &mut Latch,
) {
    // The fans absorbing every getter's exception, column by column, as
    // `psud:try_get` reads them; the PSUs are always read that way.
    let (psus, fans) = match (platform.get_psus(), platform.get_fans(FAN_COLS, true)) {
        (Ok(p), Ok(f)) => {
            pmon_common::recovered!(read, "PSU read recovered");
            (p, f)
        }
        (p, f) => {
            let e = p.err().map(|e| e.to_string())
                .or_else(|| f.err().map(|e| e.to_string()))
                .unwrap_or_default();
            // WARNING and this wording, both from the catch-all `except` in
            // `psud:DaemonPsud.update_psu_data`.
            //
            // The granularity differs and cannot be made to match: Python
            // reads one PSU at a time and warns per PSU, while the facade
            // returns every PSU in one call, so one unreadable PSU fails the
            // batch.  What can match is how loud it is -- a port that logged
            // ERROR here would have LogAnalyzer flagging a transient i2c read
            // that the Python daemon only warns about.  `fail_once` keeps it
            // to one line per outage where Python repeats it every cycle.
            pmon_common::fail_once_warn!(read, "Failed to update PSU data - {e}");
            // Not treated as "every PSU vanished": that would blank PSU_INFO
            // on a transient i2c failure and red every tray LED.
            return;
        }
    };
    counts.refresh(&psus, chassis_tbl);

    let leds = updater.refresh(&psus, &fans, tables);
    let wrote_led = !leds.is_empty();
    for led in leds {
        match platform.set_psu_led(&led.psu, led.color) {
            // The answer is not read: `_set_psu_led` does not read it either.
            Ok(_) => {}
            // `psud:DaemonPsud._set_psu_led`, word for word.  The error is not
            // appended: the base class raises `NotImplementedError` with no
            // message, so all it added was a dangling "not supported: ".
            Err(PlatformError::NotSupported(_)) => log::warn!("set_status_led() not implemented"),
            // Anything else escapes `_set_psu_led` in Python and lands in the
            // per-PSU catch-all in `psud:DaemonPsud.update_psu_data`.
            Err(e) => log::warn!("Failed to update PSU data - {e}"),
        }
    }

    if wrote_led {
        let fresh_psus = platform.get_psus().unwrap_or_default();
        let fresh_fans = platform.get_fans(FAN_LED_COLS, true).unwrap_or_default();
        updater.update_led_color(&fresh_psus, &fresh_fans, tables);
    } else {
        updater.update_led_color(&psus, &fans, tables);
    }

    if let Some(budget) = budget.current(platform) {
        let drawers = platform.get_fan_drawers(DRAWER_COLS).unwrap_or_default();
        let modules = platform.get_modules(MODULE_COLS).unwrap_or_default();
        budget.run(&psus, &drawers, &modules, chassis_tbl);

        if let Some(color) = budget.master_status() {
            // The master LED is one lamp for all the PSUs; `PsuBase`
            // implements it as a classmethod, so any row reaches it.  With no
            // PSU at all there is nothing to reach and nothing to light.
            if let Some(any) = psus.first() {
                if let Err(e) = platform.set_psu_master_led(&any.key, color) {
                    log::warn!("Failed to set the PSU master LED: {e}");
                }
            }
        }
    }
}

/// The daemon's loop, with everything it needs handed to it.
///
/// Separated from `main` so it can be driven by a test: `main` is the part that
/// cannot be -- it embeds an interpreter and opens a redis -- and this is the
/// part worth being sure of.
async fn run(
    platform: &mut dyn PlatformApi,
    tables: &Tables<'_>,
    chassis_tbl: &dyn TableLike,
    cycles: &mut Cycles,
) -> i32 {
    let mut updater = Updater::new();
    let mut budget = Budget::default();
    // Reported once rather than every three seconds.
    let mut read = Latch::new();

    // Before the first cycle, as Python publishes them from `__init__`: the
    // FRU MIB reads them as soon as the daemon is up.  A failed read publishes
    // nothing and the first pass that reads the PSUs tries again.
    let mut counts = Counts::default();
    if let Ok(psus) = platform.get_psus() {
        counts.refresh(&psus, chassis_tbl);
    }

    let code = loop {
        match cycles.next(UPDATE_PERIOD, platform).await {
            Tick::Exit(code) => break code,
            Tick::Cycle => {}
        }
        one_pass(platform, &mut updater, &mut counts, &mut budget, tables, chassis_tbl, &mut read);
    };

    // These rows are this daemon's, and a stale one is worse than none: nothing
    // else refreshes them, and `show platform psustatus` cannot tell the
    // difference.  Python does this from `psud:DaemonPsud.__del__`.
    updater.clear(tables);
    let _ = chassis_tbl.del(CHASSIS_INFO_KEY);
    let _ = chassis_tbl.del(CHASSIS_POWER_KEY);
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
            log::warn!("Failed to load chassis due to {e}");
            // Python does not give up here: it falls back to the platform's
            // legacy psuutil plugin, and exits with this code only when that
            // fails too (`psud:DaemonPsud.__init__`).  There is no such
            // fallback here, on purpose.  The plugin predates the platform
            // API, so a platform that has only the plugin has no facade for
            // this daemon to open, and runs the Python psud.  The code stays
            // the one Python leaves with when it finds no platform at all.
            std::process::exit(PSUUTIL_LOAD_ERROR);
        }
    };

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

/// Open the tables, decide what this platform needs, and run.
///
/// Separated from `main` because `main` is the part a test cannot drive -- it
/// embeds an interpreter, opens a redis and calls `process::exit` -- and this
/// is where the decisions are: which tables get opened, and which exit code a
/// failure to open gets.  The second is what supervisord reads to decide
/// whether to restart.
async fn start(platform: &mut dyn PlatformApi, open: db::Opener<'_>, cycles: &mut Cycles) -> i32 {
    let handles = match open_all(open) {
        Ok(h) => h,
        Err(e) => {
            log::error!("{e}");
            return PSU_DB_CONNECT_ERROR;
        }
    };
    let (chassis_tbl, psu_tbl, fan_tbl, entity_tbl) =
        (handles.chassis, handles.psu, handles.fan, handles.entity);

    let tables = Tables {
        psu: psu_tbl.as_ref(),
        fan: fan_tbl.as_ref(),
        entity: entity_tbl.as_ref(),
    };

    run(platform, &tables, chassis_tbl.as_ref(), cycles).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use platform_api::{ChassisInfoCols, DeviceKey, FanInfo, FanInfoCols, LedColor, PlatformError};
    use pmon_common::db::MockTable;

    use crate::updater::tests::key_of;

    /// The whole table set.  A typo in one is otherwise invisible until the
    /// daemon is on a switch.
    #[test]
    fn the_four_tables_are_opened_on_state_db() {
        let o = pmon_common::db::MockOpener::new();
        open_all(&|d, t| o.open(d, t)).expect("opens");
        assert_eq!(o.asked(), vec![
            ("STATE_DB".to_string(), "CHASSIS_INFO".to_string()),
            ("STATE_DB".to_string(), "PSU_INFO".to_string()),
            ("STATE_DB".to_string(), "FAN_INFO".to_string()),
            ("STATE_DB".to_string(), "PHYSICAL_ENTITY_INFO".to_string()),
        ]);
    }

    /// There is nowhere to publish, so the daemon says so rather than running
    /// with a handle it cannot write through.
    #[test]
    fn a_table_that_will_not_open_stops_the_daemon() {
        let o = pmon_common::db::MockOpener::failing("FAN_INFO");
        let e = open_all(&|d, t| o.open(d, t)).err().expect("cannot publish");
        assert!(e.contains("STATE_DB"), "{e}");
    }

    /// `FAN_COLS` has to name every column the updater goes on to read.
    ///
    /// A column left out of the projection arrives as `None`, and nothing
    /// says so -- the row is well-formed, the daemon publishes `N/A`, and the
    /// only symptom is a field that used to have a value and quietly stopped.
    /// That is a worse failure than the over-reading the projection exists to
    /// stop, so it gets a test rather than a comment.
    #[test]
    fn psud_asks_for_every_column_it_reads() {
        use platform_api::FanInfoCol;

        // The four `updater.rs` touches: name (always read, not declinable),
        // direction, speed and LED.  `kind` and `parent_name`, which it
        // filters on, are always populated too: they are `Parent()` fields in
        // the facade, filled from where the fan was found, and have no column
        // bit to forget.
        for col in [FanInfoCol::Direction, FanInfoCol::SpeedPct, FanInfoCol::StatusLed] {
            assert!(FAN_COLS.contains(col), "psud reads {} but does not ask for it",
                    col.as_str());
        }
        // And nothing more: the point is to decline `is_under_speed`, whose
        // Mellanox implementation reads a file a switched-off PSU empties.
        assert!(!FAN_COLS.contains(FanInfoCol::IsUnderSpeed));
        assert!(!FAN_COLS.contains(FanInfoCol::IsOverSpeed));
    }

    /// The same contract for the other three projections psud narrows.
    #[test]
    fn psud_asks_for_every_other_column_it_reads() {
        use platform_api::{ChassisInfoCol, FanDrawerInfoCol, ModuleInfoCol};

        // One column of twenty: whether to run a power budget at all.
        assert!(CHASSIS_COLS.contains(ChassisInfoCol::IsModularChassis));
        assert_eq!(CHASSIS_COLS.bits().count_ones(), 1);

        // The budget wants what each unit draws and whether it is there.
        for col in [FanDrawerInfoCol::Presence, FanDrawerInfoCol::MaximumConsumedPower] {
            assert!(DRAWER_COLS.contains(col), "{} not asked for", col.as_str());
        }
        for col in [ModuleInfoCol::Presence, ModuleInfoCol::MaximumConsumedPower] {
            assert!(MODULE_COLS.contains(col), "{} not asked for", col.as_str());
        }
        // Not the midplane, the serial, the reboot cause: chassisd's columns.
        assert!(!MODULE_COLS.contains(ModuleInfoCol::MidplaneIp));
        assert!(!MODULE_COLS.contains(ModuleInfoCol::Serial));
    }

    /// A platform that records what was done to its LEDs, and can be made to
    /// fail its reads.
    #[derive(Default)]
    struct FakePlatform {
        psus: Vec<platform_api::PsuInfo>,
        fail: bool,
        /// Fail this many `get_psus` calls, then answer.
        fail_first: usize,
        modular: bool,
        /// Fail this many `get_chassis_info` calls, then answer.
        chassis_fail_first: usize,
        leds: Vec<(DeviceKey, LedColor)>,
        led_error: Option<PlatformError>,
        master_leds: Vec<LedColor>,
        reads: usize,
        fan_cols: Vec<FanInfoCols>,
    }

    impl PlatformApi for FakePlatform {
        fn get_psus(&mut self) -> Result<Vec<platform_api::PsuInfo>, PlatformError> {
            self.reads += 1;
            if self.fail || self.reads <= self.fail_first {
                return Err(PlatformError::Backend("i2c timeout".into()));
            }
            Ok(self.psus.clone())
        }
        fn get_chassis_info(&mut self, _cols: ChassisInfoCols) -> Result<platform_api::ChassisInfo, PlatformError> {
            if self.chassis_fail_first > 0 {
                self.chassis_fail_first -= 1;
                return Err(PlatformError::Backend("i2c timeout".into()));
            }
            Ok(platform_api::ChassisInfo { is_modular_chassis: self.modular, ..Default::default() })
        }
        fn get_fans(&mut self, cols: FanInfoCols, _absorb: bool) -> Result<Vec<FanInfo>, PlatformError> {
            self.fan_cols.push(cols);
            if self.fail {
                return Err(PlatformError::Backend("i2c timeout".into()));
            }
            Ok(Vec::new())
        }
        fn set_psu_led(&mut self, psu: &DeviceKey, color: LedColor) -> Result<Option<bool>, PlatformError> {
            self.leds.push((psu.clone(), color));
            if let Some(e) = self.led_error.clone() {
                return Err(e);
            }
            // As the hardware does: the next `get_status_led` answers the new
            // colour, which is what the read-back exists to publish.
            for p in self.psus.iter_mut().filter(|p| p.key == *psu) {
                p.status_led = Some(color.to_string());
            }
            Ok(Some(true))
        }
        fn set_psu_master_led(&mut self, _psu: &DeviceKey, color: LedColor) -> Result<Option<bool>, PlatformError> {
            self.master_leds.push(color);
            Ok(Some(true))
        }
    }

    fn healthy(name: &str) -> platform_api::PsuInfo {
        platform_api::PsuInfo {
            presence: true,
            power_good: true,
            voltage: Some(platform_api::Number::Float(12.0)),
            temperature: Some(platform_api::Number::Float(40.0)),
            maximum_supplied_power: Some(platform_api::Number::Float(1000.0)),
            status_led: Some("green".to_string()),
            ..row(name, PowerEntityKind::Psu)
        }
    }

    struct Db {
        psu: MockTable,
        fan: MockTable,
        entity: MockTable,
        chassis: MockTable,
    }

    impl Db {
        fn new() -> Self {
            Self {
                psu: MockTable::new(),
                fan: MockTable::new(),
                entity: MockTable::new(),
                chassis: MockTable::new(),
            }
        }
        fn tables(&self) -> Tables<'_> {
            Tables { psu: &self.psu, fan: &self.fan, entity: &self.entity }
        }
    }

    /// The loop end to end: a pass publishes, and the teardown takes it away
    /// again -- including the two chassis keys, which nothing else owns.
    #[tokio::test]
    async fn the_loop_publishes_and_then_clears_up_after_itself() {
        tokio::time::pause();
        let db = Db::new();
        let mut p = FakePlatform { psus: vec![healthy("PSU 1")], ..Default::default() };
        db.chassis.set(CHASSIS_INFO_KEY, &[("psu_num", "1".to_string())]).unwrap();

        let mut cycles = Cycles::Fixed { remaining: 1, code: 143 };
        let code = run(&mut p, &db.tables(), &db.chassis, &mut cycles).await;

        assert_eq!(code, 143);
        assert_eq!(p.leds, vec![(key_of("PSU 1"), LedColor::Green)],
            "the first pass writes every LED");
        assert!(db.psu.is_empty() && db.entity.is_empty());
        assert!(db.chassis.is_empty(), "the count and the budget go too");
    }

    /// A transient read failure must publish nothing and delete nothing: the
    /// alternative reds every tray LED on a bus hiccup.
    #[tokio::test]
    async fn a_read_failure_writes_nothing_and_touches_no_led() {
        tokio::time::pause();
        let db = Db::new();
        let mut p = FakePlatform { fail: true, ..Default::default() };
        let mut cycles = Cycles::Fixed { remaining: 3, code: 0 };
        run(&mut p, &db.tables(), &db.chassis, &mut cycles).await;
        assert!(p.leds.is_empty());
        assert!(db.psu.is_empty());
    }

    /// Only a modular chassis accounts for power.  A fixed one has no line
    /// cards to draw it, and Python never builds the object at all.
    #[tokio::test]
    async fn the_power_budget_runs_only_when_there_is_one() {
        tokio::time::pause();
        let db = Db::new();
        let mut p = FakePlatform { psus: vec![healthy("PSU 1")], ..Default::default() };
        let mut cycles = Cycles::Fixed { remaining: 1, code: 0 };
        run(&mut p, &db.tables(), &db.chassis, &mut cycles).await;
        assert!(p.master_leds.is_empty(), "no budget, no master LED");

        let db = Db::new();
        let mut p = FakePlatform { psus: vec![healthy("PSU 1")], modular: true, ..Default::default() };
        let mut cycles = Cycles::Fixed { remaining: 1, code: 0 };
        run(&mut p, &db.tables(), &db.chassis, &mut cycles).await;
        assert_eq!(p.master_leds.len(), 1, "the first pass always writes it");
    }

    /// A failed read at start-up publishes no count, and the first read that
    /// succeeds publishes the right one.  Published from the failed read the
    /// count would be `0`, and nothing would ever correct it: Python's count
    /// comes from a list length and never fails, so it never had to.
    ///
    /// Counted in writes because the teardown deletes the key: a zero
    /// published first would show up as a second write when the real count
    /// replaced it.
    #[tokio::test]
    async fn a_failed_read_publishes_no_count_and_a_later_one_does() {
        tokio::time::pause();
        let db = Db::new();
        // The read before the first cycle and the first pass's read both fail.
        let mut p = FakePlatform {
            psus: vec![healthy("PSU 1"), healthy("PSU 2")],
            fail_first: 2,
            ..Default::default()
        };
        let mut cycles = Cycles::Fixed { remaining: 2, code: 0 };
        run(&mut p, &db.tables(), &db.chassis, &mut cycles).await;
        let n = db.chassis.writes().iter()
            .filter(|(k, f)| k == CHASSIS_INFO_KEY && f == "psu_num").count();
        assert_eq!(n, 1, "written once, by the pass whose read succeeded");
    }

    /// What gets written is the count of what was read, kept so it is not
    /// written again.
    #[test]
    fn the_counts_are_written_from_the_rows_and_remembered() {
        let t = MockTable::new();
        let mut c = Counts::default();
        let psus = [row("PSU 1", PowerEntityKind::Psu), row("PSU 2", PowerEntityKind::Psu)];
        c.refresh(&psus, &t);
        assert_eq!(t.field(CHASSIS_INFO_KEY, "psu_num").as_deref(), Some("2"));
        assert_eq!(c.0, Some((2, 0)));
    }

    /// A write that fails is not remembered as done, so the next pass retries.
    #[test]
    fn a_count_that_could_not_be_written_is_written_again() {
        let t = MockTable::new();
        t.fail_writes("redis gone");
        let mut c = Counts::default();
        c.refresh(&[row("PSU 1", PowerEntityKind::Psu)], &t);
        assert_eq!(c.0, None);
        t.allow_writes();
        c.refresh(&[row("PSU 1", PowerEntityKind::Psu)], &t);
        assert_eq!(t.field(CHASSIS_INFO_KEY, "psu_num").as_deref(), Some("1"));
    }

    /// A platform that does not answer is a fixed chassis, and says nothing
    /// about it: `ChassisBase.is_modular_chassis` answers False by default.
    #[test]
    fn a_platform_that_does_not_say_is_not_modular_and_is_not_warned_about() {
        struct Silent;
        impl PlatformApi for Silent {}
        let log = pmon_common::logging::capture();
        assert!(Budget::default().current(&mut Silent).is_none());
        assert!(!log.logged(log::Level::Warn, "modular"));
    }

    /// The count is written again only when it changes.
    #[tokio::test]
    async fn an_unchanged_count_is_not_rewritten() {
        tokio::time::pause();
        let db = Db::new();
        let mut p = FakePlatform { psus: vec![healthy("PSU 1")], ..Default::default() };
        let mut cycles = Cycles::Fixed { remaining: 3, code: 0 };
        run(&mut p, &db.tables(), &db.chassis, &mut cycles).await;
        let n = db.chassis.writes().iter()
            .filter(|(k, f)| k == CHASSIS_INFO_KEY && f == "psu_num").count();
        assert_eq!(n, 1);
    }

    /// A chassis read that fails skips the budget for that pass and no more.
    /// Latched as "not modular" it would leave a modular chassis without a
    /// budget or a master LED for the life of the daemon.
    #[tokio::test]
    async fn a_failed_chassis_read_does_not_disable_the_budget_for_good() {
        tokio::time::pause();
        let log = pmon_common::logging::capture();
        let db = Db::new();
        let mut p = FakePlatform {
            psus: vec![healthy("PSU 1")],
            modular: true,
            chassis_fail_first: 1,
            ..Default::default()
        };
        let mut cycles = Cycles::Fixed { remaining: 2, code: 0 };
        run(&mut p, &db.tables(), &db.chassis, &mut cycles).await;
        assert_eq!(p.master_leds.len(), 1, "the second pass runs the budget");
        assert!(log.logged(log::Level::Warn, "Failed to read whether the chassis is modular"));
    }

    /// The colour published is the one read back after the write, which is why
    /// a pass that wrote an LED reads the PSUs twice.  Publishing the
    /// pre-write colour would have `show platform psustatus` say red about a
    /// PSU whose LED is already green.
    ///
    /// Driven through `one_pass` rather than `run` because `run`'s teardown
    /// takes the row away before it can be read.
    #[test]
    fn a_pass_that_wrote_an_led_publishes_the_colour_read_back_after_it() {
        let db = Db::new();
        // Healthy, but showing red from before the daemon started: the first
        // pass writes green over it.
        let mut stale = healthy("PSU 1");
        stale.status_led = Some("red".to_string());
        let mut p = FakePlatform { psus: vec![stale], ..Default::default() };
        one_pass(&mut p, &mut Updater::new(), &mut Counts::default(), &mut Budget::default(),
                 &db.tables(), &db.chassis, &mut Latch::new());
        assert_eq!(p.leds, vec![(key_of("PSU 1"), LedColor::Green)]);
        assert_eq!(p.reads, 2, "once for the pass, once to read the colour back");
        assert_eq!(db.psu.field("PSU 1", "led_status").as_deref(), Some("green"));
    }

    /// The same through the loop, counted: the start-up count read, the
    /// pass's read, and the read-back the first pass's LED writes call for.
    #[tokio::test]
    async fn the_first_cycle_reads_the_psus_three_times() {
        tokio::time::pause();
        let db = Db::new();
        let mut p = FakePlatform { psus: vec![healthy("PSU 1")], ..Default::default() };
        let mut cycles = Cycles::Fixed { remaining: 1, code: 0 };
        run(&mut p, &db.tables(), &db.chassis, &mut cycles).await;
        assert_eq!(p.reads, 3, "once for the count, once for the pass, once to read the colour back");
    }

    /// The re-read asks a fan for its colour and nothing else.  The fans are
    /// re-read at all because on Mellanox a PSU fan answers with its PSU's
    /// LED; the rest of the row is what the pass already has.
    #[tokio::test]
    async fn the_fan_read_back_asks_only_for_the_led() {
        tokio::time::pause();
        let db = Db::new();
        let mut p = FakePlatform { psus: vec![healthy("PSU 1")], ..Default::default() };
        let mut cycles = Cycles::Fixed { remaining: 1, code: 0 };
        run(&mut p, &db.tables(), &db.chassis, &mut cycles).await;
        assert_eq!(p.fan_cols, vec![FAN_COLS, FanInfoCols::STATUS_LED]);
    }

    /// A pass that wrote no LED publishes the colour its own read saw, without
    /// reading every PSU and fan a second time: nothing has changed on the
    /// device since, and Python asks only `get_status_led` per cycle.
    #[tokio::test]
    async fn a_pass_that_wrote_no_led_does_not_read_the_platform_again() {
        tokio::time::pause();
        let db = Db::new();
        let mut p = FakePlatform { psus: vec![healthy("PSU 1")], ..Default::default() };
        // The first pass writes every LED; the second, steady, writes none.
        let mut cycles = Cycles::Fixed { remaining: 2, code: 0 };
        run(&mut p, &db.tables(), &db.chassis, &mut cycles).await;
        assert_eq!(p.leds.len(), 1, "only the first pass wrote");
        assert_eq!(p.reads, 4, "the count, two passes, and one read-back");
        assert_eq!(p.fan_cols, vec![FAN_COLS, FanInfoCols::STATUS_LED, FAN_COLS]);
        let led_writes = db.psu.writes().iter()
            .filter(|(k, f)| k == "PSU 1" && f == "led_status").count();
        assert_eq!(led_writes, 2, "the steady pass still publishes the colour");
    }

    /// And what the steady pass publishes is the colour that pass read, not a
    /// remembered one: an LED something else changed shows up within a cycle.
    #[test]
    fn a_pass_that_wrote_no_led_publishes_the_colour_its_own_read_saw() {
        let db = Db::new();
        let mut p = FakePlatform { psus: vec![healthy("PSU 1")], ..Default::default() };
        let mut updater = Updater::new();
        let (mut counts, mut budget, mut read) = (Counts::default(), Budget::default(), Latch::new());
        one_pass(&mut p, &mut updater, &mut counts, &mut budget, &db.tables(), &db.chassis, &mut read);
        // Someone else turns it amber; the PSU itself is unchanged.
        p.psus[0].status_led = Some("amber".to_string());
        let before = p.reads;
        one_pass(&mut p, &mut updater, &mut counts, &mut budget, &db.tables(), &db.chassis, &mut read);
        assert_eq!(p.reads - before, 1, "no read-back without a write");
        assert_eq!(db.psu.field("PSU 1", "led_status").as_deref(), Some("amber"));
    }

    /// A platform without a PSU LED gets Python's line and nothing after it.
    /// The base class raises with no message, so appending the error left a
    /// dangling "not supported: " on every switch without the LED.
    #[tokio::test]
    async fn an_unimplemented_led_is_warned_in_pythons_words() {
        tokio::time::pause();
        let log = pmon_common::logging::capture();
        let db = Db::new();
        let mut p = FakePlatform {
            psus: vec![healthy("PSU 1")],
            led_error: Some(PlatformError::NotSupported(String::new())),
            ..Default::default()
        };
        let mut cycles = Cycles::Fixed { remaining: 1, code: 0 };
        run(&mut p, &db.tables(), &db.chassis, &mut cycles).await;
        assert!(log.logged(log::Level::Warn, "set_status_led() not implemented"));
        assert!(!log.contains("not implemented:"), "nothing after Python's wording");
    }

    /// Any other LED failure is not "not implemented".  Python lets it out of
    /// `_set_psu_led` into the per-PSU catch-all, which words it this way.
    #[tokio::test]
    async fn a_failing_led_is_warned_as_a_failed_psu_update() {
        tokio::time::pause();
        let log = pmon_common::logging::capture();
        let db = Db::new();
        let mut p = FakePlatform {
            psus: vec![healthy("PSU 1")],
            led_error: Some(PlatformError::Backend("i2c timeout".into())),
            ..Default::default()
        };
        let mut cycles = Cycles::Fixed { remaining: 1, code: 0 };
        run(&mut p, &db.tables(), &db.chassis, &mut cycles).await;
        assert!(log.logged(log::Level::Warn, "Failed to update PSU data - platform error: i2c timeout"));
        assert!(!log.contains("not implemented"));
    }

    fn row(name: &str, kind: PowerEntityKind) -> platform_api::PsuInfo {
        platform_api::PsuInfo {
            name: name.to_string(),
            key: key_of(name),
            kind,
            position_in_parent: Some(1),
            presence: true,
            is_replaceable: true,
            model: None,
            serial: None,
            revision: None,
            power_good: true,
            status_led: None,
            voltage: None,
            current: None,
            power: None,
            input_voltage: None,
            input_current: None,
            input_power: None,
            temperature: None,
            temperature_high_threshold: None,
            voltage_high_threshold: None,
            voltage_low_threshold: None,
            maximum_supplied_power: None,
            power_warning_suppress_threshold: None,
            power_critical_threshold: None,
        }
    }

    /// One row list carries both kinds, and the two counts are read separately
    /// by `show platform psustatus`.  Counting the list would report PDBs as
    /// PSUs on every platform that has both.
    #[test]
    fn the_two_kinds_are_counted_apart() {
        let t = MockTable::new();
        publish_counts(
            &[
                row("PSU 1", PowerEntityKind::Psu),
                row("PSU 2", PowerEntityKind::Psu),
                row("PDB 1", PowerEntityKind::Pdb),
            ],
            &t,
        );
        assert_eq!(t.field(CHASSIS_INFO_KEY, "psu_num").as_deref(), Some("2"));
        assert_eq!(t.field(CHASSIS_INFO_KEY, "pdb_num").as_deref(), Some("1"));
    }

    /// A platform with no PDBs publishes a zero rather than nothing: the field
    /// missing and the field being 0 read differently to a consumer.
    #[test]
    fn a_platform_without_pdbs_still_publishes_the_count() {
        let t = MockTable::new();
        publish_counts(&[row("PSU 1", PowerEntityKind::Psu)], &t);
        assert_eq!(t.field(CHASSIS_INFO_KEY, "pdb_num").as_deref(), Some("0"));
    }

    // ── the wiring that used to be inside main ───────────────────────────────

    /// A platform that answers both of the questions `start` asks.
    #[derive(Default)]
    struct Shaped {
        psus: Vec<platform_api::PsuInfo>,
        modular: bool,
    }

    impl PlatformApi for Shaped {
        fn get_psus(&mut self) -> Result<Vec<platform_api::PsuInfo>, PlatformError> {
            Ok(self.psus.clone())
        }
        fn get_chassis_info(&mut self, _cols: ChassisInfoCols) -> Result<platform_api::ChassisInfo, PlatformError> {
            Ok(platform_api::ChassisInfo {
                is_modular_chassis: self.modular,
                ..Default::default()
            })
        }
    }

    fn psu(name: &str) -> platform_api::PsuInfo {
        row(name, PowerEntityKind::Psu)
    }

    /// The counts go in before the first cycle.  `CHASSIS_INFO.psu_num` is what
    /// the Cisco FRU MIB calls `int()` on: absent or non-numeric and the whole
    /// FRU table answers None, so every PSU disappears from SNMP while
    /// `show platform psustatus` still looks fine.
    #[tokio::test]
    async fn the_psu_count_is_published_before_the_first_cycle() {
        let o = pmon_common::db::MockOpener::new();
        let mut p = Shaped { psus: vec![psu("PSU 1"), psu("PSU 2")], modular: false };
        let code = start(
            &mut p,
            &|d, t| o.open(d, t),
            &mut Cycles::Fixed { remaining: 0, code: 0 },
        )
        .await;
        assert_eq!(code, 0);
        let chassis = o.table("CHASSIS_INFO").unwrap();
        assert!(chassis.wrote(CHASSIS_INFO_KEY, "psu_num"));
        assert!(chassis.wrote(CHASSIS_INFO_KEY, "pdb_num"));
        // And taken out again: the count is this daemon's and a stale one
        // outlives the daemon that could correct it.
        assert!(chassis.get(CHASSIS_INFO_KEY).unwrap().is_none());
    }

    /// A table that will not open gets the database code, not the load code:
    /// they are different numbers in Python and supervisord reads them.
    #[tokio::test]
    async fn a_table_that_will_not_open_is_a_database_error() {
        let log = pmon_common::logging::capture();
        let o = pmon_common::db::MockOpener::failing("PSU_INFO");
        let code = start(
            &mut Shaped::default(),
            &|d, t| o.open(d, t),
            &mut Cycles::Fixed { remaining: 0, code: 0 },
        )
        .await;
        assert_eq!(code, PSU_DB_CONNECT_ERROR);
        assert!(log.logged(log::Level::Error, "Failed to connect to STATE_DB"));
    }

    /// Only a modular chassis keeps a power budget.  A fixed switch has no line
    /// cards to account for, and Python never builds the object at all --
    /// publishing a budget there would put `supplied_power` on a chassis that
    /// has no suppliers.
    #[tokio::test]
    async fn a_fixed_switch_keeps_no_power_budget() {
        for modular in [false, true] {
            let o = pmon_common::db::MockOpener::new();
            let mut p = Shaped { psus: vec![psu("PSU 1")], modular };
            start(&mut p, &|d, t| o.open(d, t), &mut Cycles::Fixed { remaining: 1, code: 0 }).await;
            let touched = o
                .table("CHASSIS_INFO")
                .unwrap()
                .writes()
                .iter()
                .any(|(k, _)| k == CHASSIS_POWER_KEY);
            assert_eq!(touched, modular, "modular={modular}");
        }
    }
}
