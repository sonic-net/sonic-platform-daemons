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

use std::collections::{BTreeMap, BTreeSet};
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
const BOOT_ID_PATH: &str = "/proc/sys/kernel/random/boot_id";

fn boot_id() -> Result<String, String> {
    std::fs::read_to_string(BOOT_ID_PATH)
        .map_err(|error| format!("failed to read boot identity: {error}"))
        .and_then(|id| {
            let id = id.trim();
            if id.is_empty() {
                Err("boot identity is empty".into())
            } else {
                Ok(id.into())
            }
        })
}

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
    /// The JSON file, used when STATE_DB is unavailable. Matching boot
    /// identity means only the counter delta is owed; a new boot adds its
    /// since-boot counters to the persisted lifetime total.
    Json,
    /// STATE_DB: the daemon crashed and came back on a switch that stayed up.
    /// The kernel counters did not restart, so what is owed is the *difference*
    /// between this reading and the last one published.
    StateDb,
    /// Different disks recovered from different sources, or one has no baseline.
    Mixed,
}

/// The two baselines and the verdict between them.
#[derive(Debug, Default)]
pub struct Reconciler {
    json: BTreeMap<String, Counters>,
    statedb: BTreeMap<String, Counters>,
    baseline: Option<Baseline>,
    json_same_boot: BTreeSet<String>,
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
        v.as_deref()
            .and_then(|s| s.trim().parse::<i64>().ok())
            .ok_or((f, v))
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
    /// each source is validated per disk. A missing disk never discards another
    /// disk's valid lifetime counters; STATE_DB takes precedence where usable.
    #[cfg(test)]
    pub fn load(disks: &[String], json_text: Option<&str>, table: &dyn TableLike) -> Self {
        Self::load_known(disks, disks, json_text, table)
    }

    pub fn load_known(
        disks: &[String],
        discovered: &[String],
        json_text: Option<&str>,
        table: &dyn TableLike,
    ) -> Self {
        let current_boot = boot_id().map_err(|error| log::warn!("{error}")).ok();
        Self::load_in_boot(disks, discovered, json_text, table, current_boot.as_deref())
    }

    fn load_in_boot(
        disks: &[String],
        discovered: &[String],
        json_text: Option<&str>,
        table: &dyn TableLike,
        current_boot: Option<&str>,
    ) -> Self {
        let mut r = Self::default();
        r.load_statedb(disks, discovered, table);
        r.load_json(disks, json_text, current_boot);
        let first = disks
            .first()
            .map_or(Baseline::Init, |disk| r.disk_baseline(disk));
        r.baseline = Some(if disks.iter().all(|disk| r.disk_baseline(disk) == first) {
            first
        } else {
            Baseline::Mixed
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
    fn load_json(
        &mut self,
        disks: &[String],
        text: Option<&str>,
        current_boot: Option<&str>,
    ) -> bool {
        let Some(text) = text else { return false };
        let parsed: serde_json::Value = match serde_json::from_str(text) {
            Ok(v) => v,
            Err(e) => {
                log::error!("JSON file could not be loaded: {e}");
                return false;
            }
        };
        for disk in disks {
            if parsed
                .get(disk)
                .and_then(|row| row.get("baseline_valid"))
                .and_then(|value| value.as_bool())
                == Some(false)
            {
                log::warn!("{disk}: JSON snapshot has no known baseline");
                continue;
            }
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
                    if current_boot.is_some() && snapshot_boot(&parsed, disk) == current_boot {
                        self.json_same_boot.insert(disk.clone());
                    }
                }
                Err(unusable) => {
                    warn_unusable(disk, "JSON file", unusable);
                }
            }
        }
        !self.json.is_empty()
    }

    fn load_statedb(
        &mut self,
        disks: &[String],
        discovered: &[String],
        table: &dyn TableLike,
    ) -> bool {
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
        if !keys.iter().any(|k| k == FSSTATS_SYNC_KEY)
            || keys
                .iter()
                .any(|k| k != FSSTATS_SYNC_KEY && !discovered.contains(k))
        {
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
            let get = |f: &str| row.iter().find(|(k, _)| k == f).map(|(_, v)| v.clone());
            match counters_from(get) {
                Ok(c) => {
                    self.statedb.insert(disk.clone(), c);
                }
                Err(unusable) => {
                    warn_unusable(disk, "StateDB", unusable);
                }
            }
        }
        !self.statedb.is_empty()
    }

    pub fn baseline(&self) -> Baseline {
        self.baseline.unwrap_or(Baseline::Init)
    }

    fn disk_baseline(&self, disk: &str) -> Baseline {
        if self.statedb.contains_key(disk) {
            Baseline::StateDb
        } else if self.json.contains_key(disk) {
            Baseline::Json
        } else {
            Baseline::Init
        }
    }

    /// This cycle's lifetime totals for one disk.
    ///
    /// `latest` is what `/proc/diskstats` says now.
    pub fn totals(&self, disk: &str, latest_reads: i64, latest_writes: i64) -> (i64, i64) {
        match self.disk_baseline(disk) {
            Baseline::Init | Baseline::Mixed => (latest_reads, latest_writes),
            Baseline::Json => {
                let b = self.json.get(disk).copied().unwrap_or_default();
                if self.json_same_boot.contains(disk) {
                    (
                        b.total_reads + latest_reads - b.latest_reads,
                        b.total_writes + latest_writes - b.latest_writes,
                    )
                } else {
                    // Legacy files have no boot identity. Retain their old
                    // reboot interpretation; newly written files disambiguate
                    // every subsequent same-boot fallback.
                    (b.total_reads + latest_reads, b.total_writes + latest_writes)
                }
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
/// Healthy disks advance independently. Failed disks retain their previous
/// counters and original boot identity; an unknown baseline is marked invalid.
/// With no fresh disk, the previous file survives unchanged. The legacy Python
/// loader ignores the additive metadata and still receives numeric fields.
#[cfg(test)]
pub fn sync_document(
    disks: &[String],
    table: &dyn TableLike,
    when: &str,
) -> Result<String, String> {
    sync_document_in_boot(disks, table, when, &boot_id()?)
}

/// Preserve failed disks' previous baselines while persisting healthy ones.
pub fn sync_document_with_previous(
    disks: &[String],
    table: &dyn TableLike,
    when: &str,
    previous: Option<&str>,
) -> Result<String, String> {
    compose_snapshot(disks, table, when, &boot_id()?, previous)
}

fn snapshot_boot<'a>(document: &'a serde_json::Value, disk: &str) -> Option<&'a str> {
    match document.get(disk).and_then(|row| row.get("boot_id")) {
        // An explicit null means legacy/unknown identity, not the new file's boot.
        Some(value) => value.as_str(),
        None => document.get("boot_id").and_then(|value| value.as_str()),
    }
}

#[cfg(test)]
fn sync_document_in_boot(
    disks: &[String],
    table: &dyn TableLike,
    when: &str,
    current_boot: &str,
) -> Result<String, String> {
    compose_snapshot(disks, table, when, current_boot, None)
}

fn compose_snapshot(
    disks: &[String],
    table: &dyn TableLike,
    when: &str,
    current_boot: &str,
    previous: Option<&str>,
) -> Result<String, String> {
    let mut doc = serde_json::Map::new();
    let saved: serde_json::Value = previous
        .and_then(|text| serde_json::from_str(text).ok())
        .unwrap_or_default();
    let mut fresh = 0;
    for disk in disks {
        let row = table
            .get(disk)
            .map_err(|e| format!("failed to read {disk}: {e}"))?
            .unwrap_or_default();
        let counters = counters_from(|field| {
            row.iter()
                .find(|(key, _)| key == field)
                .map(|(_, value)| value.clone())
        });
        let mut fields = serde_json::Map::new();
        if counters.is_ok() {
            fresh += 1;
            for field in SYNC_FIELDS {
                if let Some((_, value)) = row.iter().find(|(key, _)| key == field) {
                    fields.insert(field.into(), serde_json::Value::String(value.clone()));
                }
            }
            fields.insert("boot_id".into(), current_boot.into());
        } else {
            log::warn!("{disk}: no current FSIO counters; retaining its previous baseline while syncing healthy disks");
            let old = saved.get(disk);
            let valid_old = old
                .and_then(|value| value.get("baseline_valid"))
                .and_then(|value| value.as_bool())
                != Some(false)
                && counters_from(|field| {
                    old.and_then(|value| value.get(field))
                        .and_then(|value| value.as_str())
                        .map(str::to_owned)
                })
                .is_ok();
            if valid_old {
                for field in SYNC_FIELDS {
                    fields.insert(
                        field.into(),
                        old.and_then(|value| value.get(field))
                            .cloned()
                            .unwrap_or_default(),
                    );
                }
                fields.insert(
                    "boot_id".into(),
                    snapshot_boot(&saved, disk).map_or(serde_json::Value::Null, |id| id.into()),
                );
            } else {
                // Numeric neutral fields keep the legacy Python loader usable.
                // The validity marker prevents Rust from treating them as data.
                for field in SYNC_FIELDS {
                    fields.insert(field.into(), "0".into());
                }
                fields.insert("baseline_valid".into(), false.into());
                fields.insert("boot_id".into(), serde_json::Value::Null);
            }
        }
        doc.insert(disk.clone(), serde_json::Value::Object(fields));
    }
    if fresh == 0 {
        return Err("no current disk counters; preserving the existing snapshot".into());
    }
    doc.insert(
        "boot_id".to_string(),
        serde_json::Value::String(current_boot.to_string()),
    );
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

    #[test]
    fn an_unavailable_known_disk_does_not_force_a_same_boot_json_fallback() {
        let table = statedb(100, 150);
        table
            .set("sdb", &[("serial", "temporarily unavailable".into())])
            .unwrap();
        let tracked = disks();
        let known = vec!["sda".into(), "sdb".into()];
        let reconciler = Reconciler::load_known(&tracked, &known, None, &table);
        assert_eq!(reconciler.baseline(), Baseline::StateDb);
        assert_eq!(reconciler.totals("sda", 200, 0).0, 250);
        table.set("unknown-device", &[]).unwrap();
        assert_eq!(
            Reconciler::load_known(&tracked, &known, None, &table).baseline(),
            Baseline::Init
        );
    }

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
        t.set(
            FSSTATS_SYNC_KEY,
            &[("successful_sync_time", "x".to_string())],
        )
        .unwrap();
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
        t.set("sdb", &[("latest_fsio_reads", "1".to_string())])
            .unwrap();
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
            log.logged(
                log::Level::Warn,
                "sda:latest_fsio_writes value = None in JSON file"
            ),
            "the warning names the field, as Python's does"
        );
    }

    /// A value that is there and is not a number says what it was: "None"
    /// would send the reader looking for a field that is present.
    #[test]
    fn a_counter_that_is_not_a_number_says_what_it_held() {
        let log = pmon_common::logging::capture();
        let t = statedb(80, 1000);
        t.set("sda", &[("total_fsio_writes", "N/A".to_string())])
            .unwrap();
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
        assert!(log.logged(
            log::Level::Error,
            "Reading STATE_DB failed with: connection reset"
        ));
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

    /// An incomplete snapshot must leave the previous usable file alone.
    #[test]
    fn a_disk_with_no_counter_row_cannot_replace_the_baseline() {
        assert!(sync_document(&disks(), &MockTable::new(), "now").is_err());
        let table = statedb(80, 1000);
        table
            .set("sda", &[("total_fsio_reads", "invalid".into())])
            .unwrap();
        assert!(sync_document(&disks(), &table, "now").is_err());
    }

    #[test]
    fn persisted_snapshot_reconciles_same_boot_fallback_and_reboot_differently() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("fsio.json");
        let table = statedb(80, 1000);
        let doc = sync_document_in_boot(&disks(), &table, "now", "boot-one").unwrap();
        std::fs::write(&path, doc).unwrap();
        let saved = Reconciler::read_json_file(&path).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&saved).unwrap();
        assert_eq!(parsed["boot_id"], "boot-one");
        assert_eq!(parsed["sda"]["total_fsio_reads"], "1000");
        let rejected_state = MockTable::new();
        let same = Reconciler::load_in_boot(
            &disks(),
            &disks(),
            Some(&saved),
            &rejected_state,
            Some("boot-one"),
        );
        assert_eq!(same.baseline(), Baseline::Json);
        assert_eq!(same.totals("sda", 100, 0), (1020, 0));
        let reboot = Reconciler::load_in_boot(
            &disks(),
            &disks(),
            Some(&saved),
            &rejected_state,
            Some("boot-two"),
        );
        assert_eq!(reboot.totals("sda", 100, 0), (1100, 0));
        let unknown =
            Reconciler::load_in_boot(&disks(), &disks(), Some(&saved), &rejected_state, None);
        assert_eq!(unknown.totals("sda", 100, 0), (1100, 0));
    }

    #[test]
    fn healthy_disks_persist_while_failed_disks_keep_their_original_boot() {
        let disks = vec!["sda".into(), "sdb".into()];
        let prior = serde_json::json!({"boot_id":"boot-old", "sdb": {
            "latest_fsio_reads":"80", "latest_fsio_writes":"0",
            "total_fsio_reads":"1000", "total_fsio_writes":"0"}})
        .to_string();
        let table = statedb(200, 2200);
        let doc = compose_snapshot(&disks, &table, "now", "boot-new", Some(&prior)).unwrap();
        let saved: serde_json::Value = serde_json::from_str(&doc).unwrap();
        assert_eq!(saved["sda"]["total_fsio_reads"], "2200");
        assert_eq!(saved["sda"]["boot_id"], "boot-new");
        assert_eq!(saved["sdb"]["boot_id"], "boot-old");
        let empty = MockTable::new();
        let same = Reconciler::load_in_boot(&disks, &disks, Some(&doc), &empty, Some("boot-new"));
        assert_eq!(same.totals("sda", 230, 0), (2230, 0));
        assert_eq!(same.totals("sdb", 100, 0), (1100, 0));
        let old_boot =
            Reconciler::load_in_boot(&disks, &disks, Some(&doc), &empty, Some("boot-old"));
        assert_eq!(old_boot.totals("sdb", 100, 0), (1020, 0));
        // Later snapshots must not assign the failed disk the latest root boot.
        let again = compose_snapshot(&disks, &table, "later", "boot-third", Some(&doc)).unwrap();
        let again: serde_json::Value = serde_json::from_str(&again).unwrap();
        assert_eq!(again["sdb"]["boot_id"], "boot-old");
    }

    #[test]
    fn unknown_and_legacy_disks_do_not_discard_a_healthy_baseline() {
        let disks = vec!["sda".into(), "sdb".into()];
        let table = statedb(200, 2200);
        let doc = compose_snapshot(&disks, &table, "now", "boot", None).unwrap();
        let saved: serde_json::Value = serde_json::from_str(&doc).unwrap();
        assert_eq!(saved["sdb"]["baseline_valid"], false);
        let loaded =
            Reconciler::load_in_boot(&disks, &disks, Some(&doc), &MockTable::new(), Some("boot"));
        assert_eq!(loaded.baseline(), Baseline::Mixed);
        assert_eq!(loaded.totals("sda", 230, 0), (2230, 0));
        assert_eq!(loaded.totals("sdb", 100, 0), (100, 0));
        let legacy = serde_json::json!({"sdb": {"latest_fsio_reads":"80", "latest_fsio_writes":"0", "total_fsio_reads":"1000", "total_fsio_writes":"0"}}).to_string();
        let doc = compose_snapshot(&disks, &table, "now", "boot", Some(&legacy)).unwrap();
        let saved: serde_json::Value = serde_json::from_str(&doc).unwrap();
        assert!(saved["sdb"]["boot_id"].is_null());
        let loaded =
            Reconciler::load_in_boot(&disks, &disks, Some(&doc), &MockTable::new(), Some("boot"));
        assert_eq!(loaded.totals("sdb", 100, 0), (1100, 0));
    }

    #[test]
    fn each_disk_selects_its_own_state_or_file_baseline() {
        let disks = vec!["sda".into(), "sdb".into()];
        let table = statedb(200, 2200);
        let prior = serde_json::json!({"boot_id":"boot", "sdb": {"latest_fsio_reads":"80", "latest_fsio_writes":"0", "total_fsio_reads":"1000", "total_fsio_writes":"0"}}).to_string();
        let loaded = Reconciler::load_in_boot(&disks, &disks, Some(&prior), &table, Some("boot"));
        assert_eq!(loaded.baseline(), Baseline::Mixed);
        assert_eq!(loaded.totals("sda", 230, 0), (2230, 0));
        assert_eq!(loaded.totals("sdb", 100, 0), (1020, 0));
    }
}
