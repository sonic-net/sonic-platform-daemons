//! Lifetime filesystem read/write counters, across restarts.
//!
//! Ports `stormond:DaemonStorage._load_fsio_rw_json`,
//! `stormond:DaemonStorage._load_fsio_rw_statedb`,
//! `stormond:DaemonStorage._determine_sot` and
//! `stormond:DaemonStorage._reconcile_fsio_rw_values`.
//!
//! `/proc/diskstats` counts from boot, and what stormond publishes is a total
//! over the life of the disk.  So every cycle's reading is a *delta source*,
//! not a total, and the daemon has to carry a baseline across its own restarts
//! and the switch's reboots.  It keeps two: STATE_DB, which survives a daemon
//! crash but not a reboot, and a JSON file on the host, which survives both but
//! is only written once a day.
//!
//! Choosing between them wrong does not fail loudly -- it publishes a lifetime
//! total that is quietly too small or absurdly large, which is why the three
//! cases below are spelled out one at a time.

use std::collections::BTreeMap;
use std::path::Path;

use pmon_common::db::TableLike;

/// The four fields that are both published and carried across a restart.
///
/// `stormond:DaemonStorage.statedb_json_sync_fields` takes them as
/// `dynamic_fields[3:7]`; naming them is the same list with the slice's
/// fragility removed.
pub const SYNC_FIELDS: [&str; 4] = [
    "latest_fsio_reads",
    "latest_fsio_writes",
    "total_fsio_reads",
    "total_fsio_writes",
];

/// This directory binds to `/host/pmon/stormond/` on the host, which is what
/// makes the file outlive the container.
pub const FSIO_RW_JSON_FILE: &str = "/usr/share/stormond/fsio-rw-stats.json";

/// The key the sync time is published under, beside the disks.
pub const FSSTATS_SYNC_KEY: &str = "FSSTATS_SYNC";

/// One disk's carried counters.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct Counters {
    pub latest_reads: i64,
    pub latest_writes: i64,
    pub total_reads: i64,
    pub total_writes: i64,
}

/// Which baseline the daemon is reconciling against.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Baseline {
    /// Neither source loaded: a first-ever start.  This cycle's reading *is*
    /// the total, which understates a disk that has been in service -- and is
    /// the only honest answer when nothing recorded what came before.
    Init,
    /// The JSON file: a planned reboot or power cycle.  STATE_DB is volatile,
    /// so its absence is the signal.  The counters restarted at zero with the
    /// machine, so this cycle's reading adds to the recorded total.
    Json,
    /// STATE_DB: the daemon crashed and came back on a switch that stayed up.
    /// The kernel counters did not restart, so what is owed is the *difference*
    /// between this reading and the last one published.
    StateDb,
}

/// The two baselines and the verdict between them.
#[derive(Debug, Default)]
pub struct Reconciler {
    json: BTreeMap<String, Counters>,
    statedb: BTreeMap<String, Counters>,
    baseline: Option<Baseline>,
}

/// A field that will not do as a counter, and what it held.
type Unusable = (&'static str, Option<String>);

/// Read every sync field of one disk out of a row source.
///
/// `Err` names the first field that is not there or is not a number, so the
/// warning can say which, as Python's does.
fn counters_from(get: impl Fn(&str) -> Option<String>) -> Result<Counters, Unusable> {
    let n = |f: &'static str| {
        let v = get(f);
        v.as_deref().and_then(|s| s.trim().parse::<i64>().ok()).ok_or((f, v))
    };
    Ok(Counters {
        latest_reads: n(SYNC_FIELDS[0])?,
        latest_writes: n(SYNC_FIELDS[1])?,
        total_reads: n(SYNC_FIELDS[2])?,
        total_writes: n(SYNC_FIELDS[3])?,
    })
}

/// Say why one disk's counters void a baseline.
///
/// A value that is not there is Python's `None`, and gets Python's warning
/// (`stormond:DaemonStorage._load_fsio_rw_json`,
/// `stormond:DaemonStorage._load_fsio_rw_statedb`).  One that is there and is
/// not a number Python takes, and fails on later in the arithmetic; here it
/// is refused now, and says what it was.
fn warn_unusable(disk: &str, source: &str, (field, value): Unusable) {
    match value {
        None => log::warn!("{disk}:{field} value = None in {source}"),
        Some(v) => log::warn!("{disk}:{field} value = {v} in {source} is not a number"),
    }
}

impl Reconciler {
    /// Load both baselines and decide which one to use.
    ///
    /// `disks` is every disk this daemon publishes counters for, which is what
    /// both sanity checks are against: a baseline that does not cover every
    /// one is not a baseline, because the disk it misses would silently start
    /// from zero.
    pub fn load(disks: &[String], json_text: Option<&str>, table: &dyn TableLike) -> Self {
        let mut r = Self::default();
        let statedb_ok = r.load_statedb(disks, table);
        let json_ok = r.load_json(disks, json_text);

        r.baseline = Some(if statedb_ok {
            Baseline::StateDb
        } else if json_ok {
            Baseline::Json
        } else {
            Baseline::Init
        });
        r
    }

    /// Read `/usr/share/stormond/fsio-rw-stats.json`, if it is there.
    ///
    /// Absent is a first boot, said at NOTICE as Python says it.  There and
    /// unreadable is an error, in Python's words for a file it could not load
    /// (`stormond:DaemonStorage._load_fsio_rw_json`) -- not "not present",
    /// which would send whoever reads the log looking for a file that exists.
    /// Either way there is no file baseline.
    pub fn read_json_file(path: &Path) -> Option<String> {
        match std::fs::read_to_string(path) {
            Ok(text) => Some(text),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                pmon_common::notice!("{} not present.", path.display());
                None
            }
            Err(e) => {
                log::error!("JSON file could not be loaded: {e}");
                None
            }
        }
    }

    /// The file baseline, out of `text` -- `None` when there was no file to
    /// read, which `read_json_file` has already said.
    fn load_json(&mut self, disks: &[String], text: Option<&str>) -> bool {
        let Some(text) = text else { return false };
        let parsed: serde_json::Value = match serde_json::from_str(text) {
            Ok(v) => v,
            Err(e) => {
                log::error!("JSON file could not be loaded: {e}");
                return false;
            }
        };
        for disk in disks {
            let get = |f: &str| {
                parsed
                    .get(disk)
                    .and_then(|d| d.get(f))
                    .map(|v| v.as_str().map_or_else(|| v.to_string(), str::to_string))
                    .filter(|v| v != "null")
            };
            match counters_from(get) {
                Ok(c) => {
                    self.json.insert(disk.clone(), c);
                }
                Err(unusable) => {
                    // One disk missing from the file invalidates the file, not
                    // just that disk: the alternative is a partial baseline,
                    // where one disk's total restarts from zero and nothing
                    // says why (`stormond:DaemonStorage._load_fsio_rw_json`).
                    warn_unusable(disk, "JSON file", unusable);
                    self.json.clear();
                    return false;
                }
            }
        }
        true
    }

    fn load_statedb(&mut self, disks: &[String], table: &dyn TableLike) -> bool {
        // A row count that does not match the disks plus the sync-time key
        // means something else has been editing the table, and a baseline read
        // out of it would be arithmetic on someone else's numbers.
        //
        // A read that fails is no baseline either, and is said at ERROR, as the
        // `except` in `stormond:DaemonStorage._load_fsio_rw_statedb` says it.
        // Not fatal there or here: the file may still be one.
        let keys = match table.get_keys() {
            Ok(keys) => keys,
            Err(e) => {
                log::error!("Reading STATE_DB failed with: {e}");
                return false;
            }
        };
        if keys.len() != disks.len() + 1 {
            return false;
        }
        for disk in disks {
            let row = match table.get(disk) {
                Ok(row) => row.unwrap_or_default(),
                Err(e) => {
                    log::error!("Reading STATE_DB failed with: {e}");
                    self.statedb.clear();
                    return false;
                }
            };
            let get = |f: &str| {
                row.iter().find(|(k, _)| k == f).map(|(_, v)| v.clone())
            };
            match counters_from(get) {
                Ok(c) => {
                    self.statedb.insert(disk.clone(), c);
                }
                Err(unusable) => {
                    warn_unusable(disk, "StateDB", unusable);
                    self.statedb.clear();
                    return false;
                }
            }
        }
        true
    }

    pub fn baseline(&self) -> Baseline {
        self.baseline.unwrap_or(Baseline::Init)
    }

    /// This cycle's lifetime totals for one disk.
    ///
    /// `latest` is what `/proc/diskstats` says now.
    pub fn totals(&self, disk: &str, latest_reads: i64, latest_writes: i64) -> (i64, i64) {
        match self.baseline() {
            Baseline::Init => (latest_reads, latest_writes),
            Baseline::Json => {
                let b = self.json.get(disk).copied().unwrap_or_default();
                (b.total_reads + latest_reads, b.total_writes + latest_writes)
            }
            Baseline::StateDb => {
                let b = self.statedb.get(disk).copied().unwrap_or_default();
                // What the disk did while the daemon was not running.  The
                // counters did not restart, so this is a difference and not a
                // sum -- adding instead would double the lifetime total on
                // every crash.
                (
                    b.total_reads + (latest_reads - b.latest_reads),
                    b.total_writes + (latest_writes - b.latest_writes),
                )
            }
        }
    }
}

/// The JSON file's contents, built from what is currently in STATE_DB.
///
/// Read back out of the table rather than from the values just computed: the
/// file is a snapshot of what was *published*, and a file that recorded a total
/// STATE_DB never got would move the baseline past the published figure.
///
/// A row that is not there is written as nulls, which the loader rejects; a
/// table that cannot be read is an error, so the caller can leave the file
/// alone rather than replace it with one of nulls.
pub fn sync_document(
    disks: &[String],
    table: &dyn TableLike,
    when: &str,
) -> Result<String, String> {
    let mut doc = serde_json::Map::new();
    for disk in disks {
        let row = table
            .get(disk)
            .map_err(|e| format!("failed to read {disk}: {e}"))?
            .unwrap_or_default();
        let mut fields = serde_json::Map::new();
        for f in SYNC_FIELDS {
            let v = row.iter().find(|(k, _)| k == f).map(|(_, v)| v.clone());
            fields.insert(
                f.to_string(),
                match v {
                    Some(v) => serde_json::Value::String(v),
                    None => serde_json::Value::Null,
                },
            );
        }
        doc.insert(disk.clone(), serde_json::Value::Object(fields));
    }
    doc.insert(
        "successful_sync_time".to_string(),
        serde_json::Value::String(when.to_string()),
    );
    Ok(serde_json::Value::Object(doc).to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use pmon_common::db::MockTable;

    fn disks() -> Vec<String> {
        vec!["sda".to_string()]
    }

    fn statedb(latest_r: i64, total_r: i64) -> MockTable {
        let t = MockTable::new();
        t.set(
            "sda",
            &[
                ("latest_fsio_reads", latest_r.to_string()),
                ("latest_fsio_writes", "0".to_string()),
                ("total_fsio_reads", total_r.to_string()),
                ("total_fsio_writes", "0".to_string()),
            ],
        )
        .unwrap();
        t.set(FSSTATS_SYNC_KEY, &[("successful_sync_time", "x".to_string())]).unwrap();
        t
    }

    fn json(total_r: i64) -> String {
        format!(
            r#"{{"sda": {{"latest_fsio_reads": "5", "latest_fsio_writes": "0",
                "total_fsio_reads": "{total_r}", "total_fsio_writes": "0"}},
                "successful_sync_time": "2026-09-17 00:00:00"}}"#
        )
    }

    /// A first-ever start has nothing to add to.  Publishing this cycle's
    /// reading as the total understates a disk that has been in service, and
    /// is still the only figure anything recorded.
    #[test]
    fn with_no_baseline_the_reading_is_the_total() {
        let r = Reconciler::load(&disks(), None, &MockTable::new());
        assert_eq!(r.baseline(), Baseline::Init);
        assert_eq!(r.totals("sda", 100, 50), (100, 50));
    }

    /// After a reboot the kernel counters restarted at zero, so this cycle's
    /// reading is new work and *adds* to the recorded total.
    #[test]
    fn after_a_reboot_the_reading_adds_to_the_recorded_total() {
        let doc = json(1000);
        let r = Reconciler::load(&disks(), Some(&doc), &MockTable::new());
        assert_eq!(r.baseline(), Baseline::Json);
        assert_eq!(r.totals("sda", 100, 0), (1100, 0));
    }

    /// After a daemon crash the switch stayed up and the counters did not
    /// restart, so what is owed is the *difference*.  Adding here instead
    /// would double the lifetime total on every crash -- the single most
    /// consequential line in this module.
    #[test]
    fn after_a_crash_only_the_difference_is_owed() {
        let t = statedb(80, 1000);
        let r = Reconciler::load(&disks(), None, &t);
        assert_eq!(r.baseline(), Baseline::StateDb);
        assert_eq!(r.totals("sda", 100, 0), (1020, 0), "1000 + (100 - 80)");
    }

    /// Both present is the crash case: STATE_DB is the fresher of the two,
    /// since the file is written once a day.
    #[test]
    fn state_db_wins_over_the_file() {
        let doc = json(999_999);
        let t = statedb(80, 1000);
        let r = Reconciler::load(&disks(), Some(&doc), &t);
        assert_eq!(r.baseline(), Baseline::StateDb);
    }

    /// The row count is the corruption check: something else editing the table
    /// would otherwise have the daemon do arithmetic on its numbers.
    #[test]
    fn a_table_with_the_wrong_number_of_rows_is_not_a_baseline() {
        let t = statedb(80, 1000);
        t.set("sdb", &[("latest_fsio_reads", "1".to_string())]).unwrap();
        let r = Reconciler::load(&disks(), None, &t);
        assert_eq!(r.baseline(), Baseline::Init);
    }

    /// A disk missing from the file invalidates the whole file.  Falling back
    /// per-disk would restart one disk's lifetime total at zero with nothing
    /// in the log to say which or why.
    #[test]
    fn a_partial_file_is_not_a_partial_baseline() {
        let log = pmon_common::logging::capture();
        let doc = r#"{"sda": {"latest_fsio_reads": "5"}}"#;
        let r = Reconciler::load(&disks(), Some(doc), &MockTable::new());
        assert_eq!(r.baseline(), Baseline::Init);
        assert!(
            log.logged(log::Level::Warn, "sda:latest_fsio_writes value = None in JSON file"),
            "the warning names the field, as Python's does"
        );
    }

    /// A value that is there and is not a number says what it was: "None"
    /// would send the reader looking for a field that is present.
    #[test]
    fn a_counter_that_is_not_a_number_says_what_it_held() {
        let log = pmon_common::logging::capture();
        let t = statedb(80, 1000);
        t.set("sda", &[("total_fsio_writes", "N/A".to_string())]).unwrap();
        let r = Reconciler::load(&disks(), None, &t);
        assert_eq!(r.baseline(), Baseline::Init);
        assert!(log.logged(
            log::Level::Warn,
            "sda:total_fsio_writes value = N/A in StateDB is not a number"
        ));
    }

    /// A STATE_DB that cannot be read is no baseline, and says so at ERROR as
    /// Python's `except` does -- not quietly, as though the table were empty.
    #[test]
    fn a_state_db_that_cannot_be_read_is_said_so() {
        let log = pmon_common::logging::capture();
        let t = statedb(80, 1000);
        t.fail_reads("connection reset");
        let doc = json(1000);
        let r = Reconciler::load(&disks(), Some(&doc), &t);
        assert_eq!(r.baseline(), Baseline::Json, "and the file is still one");
        assert!(log.logged(log::Level::Error, "Reading STATE_DB failed with: connection reset"));
    }

    /// No file is a first boot, said at NOTICE; a file that is there and will
    /// not read is an error, and says why.
    #[test]
    fn an_absent_file_and_an_unreadable_one_are_told_apart() {
        let log = pmon_common::logging::capture();
        let d = tempfile::tempdir().unwrap();
        assert!(Reconciler::read_json_file(&d.path().join("absent.json")).is_none());
        assert!(log.logged(log::Level::Info, "absent.json not present."));
        assert!(!log.logged(log::Level::Error, "JSON file could not be loaded"));

        // A directory where the file should be: there, and not readable as one.
        assert!(Reconciler::read_json_file(d.path()).is_none());
        assert!(log.logged(log::Level::Error, "JSON file could not be loaded: "));
    }

    #[test]
    fn a_null_in_the_file_is_not_a_number() {
        let doc = r#"{"sda": {"latest_fsio_reads": null, "latest_fsio_writes": "0",
                      "total_fsio_reads": "9", "total_fsio_writes": "0"}}"#;
        let r = Reconciler::load(&disks(), Some(doc), &MockTable::new());
        assert_eq!(r.baseline(), Baseline::Init);
    }

    #[test]
    fn unparseable_json_is_not_a_baseline() {
        let r = Reconciler::load(&disks(), Some("{not json"), &MockTable::new());
        assert_eq!(r.baseline(), Baseline::Init);
    }

    /// The file records what was published, so it is read back out of the
    /// table.  Writing the freshly computed numbers instead would move the
    /// baseline past a figure STATE_DB never took.
    #[test]
    fn the_file_records_what_state_db_holds() {
        let t = statedb(80, 1000);
        let doc = sync_document(&disks(), &t, "2026-09-17 12:00:00").unwrap();
        let v: serde_json::Value = serde_json::from_str(&doc).unwrap();
        assert_eq!(v["sda"]["total_fsio_reads"], "1000");
        assert_eq!(v["sda"]["latest_fsio_reads"], "80");
        assert_eq!(v["successful_sync_time"], "2026-09-17 12:00:00");
    }

    /// A disk with nothing in the table yet writes nulls, which is what the
    /// loader then rejects -- so a half-written file cannot become a baseline.
    #[test]
    fn a_disk_with_no_row_writes_nulls_the_loader_will_reject() {
        let doc = sync_document(&disks(), &MockTable::new(), "now").unwrap();
        let r = Reconciler::load(&disks(), Some(&doc), &MockTable::new());
        assert_eq!(r.baseline(), Baseline::Init);
    }
}
