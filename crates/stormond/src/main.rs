//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! stormond, in Rust.  Ports `sonic-stormond/scripts/stormond`.
//!
//! STORAGE_INFO once an hour: each disk's model, firmware, health, temperature,
//! and its lifetime filesystem and block read/write counters.  The counters are
//! the hard part and live in [`fsio`]; everything here is the loop around them.

mod fsio;

use std::path::Path;
use std::time::Duration;

use platform_api::{PlatformApi, StorageDeviceInfo};
use clap::Parser;
use platform_provider::PlatformImpl;
use pmon_common::cycles::{Cycles, Tick};
use pmon_common::db::{self, TableLike};
use pmon_common::logging;
use pmon_common::report_once::Latch;

use fsio::{Reconciler, FSIO_RW_JSON_FILE, FSSTATS_SYNC_KEY};

const SYSLOG_IDENTIFIER: &str = "stormond";

/// The command line, which is one switch.
///
/// Which vendor package gets imported is not here and never was: the image
/// installs one `sonic_platform` and which one is its business.  What is here
/// is which *implementation* of the platform API to open, because a platform
/// with a native Rust one has to be able to say so without a new binary.
#[derive(Parser, Debug)]
#[command(name = "stormond-rs", about = "SONiC storage monitor daemon, in Rust")]
struct Args {
    /// Which platform API implementation to use: `pyo3` or `native`.
    ///
    /// Filled in from `platform_api_stormond` in `pmon_daemon_control.json` by the
    /// supervisord template.  Absent means `pyo3`, which is what every platform
    /// runs today -- an unset switch has to leave a platform on the
    /// implementation it has always had.
    #[arg(long, default_value_t = PlatformImpl::Pyo3)]
    platform_api: PlatformImpl,
}

const STORAGE_DEVICE_TABLE: &str = "STORAGE_INFO";
const CONFIG_DB: &str = "CONFIG_DB";
const STORMOND_CONFIG_TABLE: &str = "STORMOND_CONFIG";
const INTERVALS_KEY: &str = "INTERVALS";

/// `stormond:STORMOND_PERIODIC_STATEDB_SYNC_SECS` and
/// `stormond:STORMOND_SYNC_TO_DISK_SECS`.
const DEFAULT_POLL: Duration = Duration::from_secs(3600);
const DEFAULT_SYNC: Duration = Duration::from_secs(86400);

/// `stormond:STORAGEUTIL_LOAD_ERROR`.
const STORAGEUTIL_LOAD_ERROR: i32 = 127;

/// What a daemon that cannot reach STATE_DB leaves with.
///
/// Python has no constant for it because it does not choose: the `hset` in
/// `stormond:DaemonStorage.write_sync_time_statedb` is unwrapped, the exception
/// leaves `run`, and the interpreter exits 1.  Non-zero is what matters --
/// supervisord's `autorestart=unexpected` reads the code, and a restart is the
/// only way back to a working connection because `DBConnector` never
/// reconnects.
const ERR_DB_WRITE: i32 = 1;

/// `stormond:DaemonStorage.time_format_string`.
const TIME_FORMAT: &str = "%Y-%m-%d %H:%M:%S";

/// The two periods, which CONFIG_DB may override at any time.
#[derive(Debug, Clone, Copy, PartialEq)]
struct Intervals {
    poll: Duration,
    sync: Duration,
}

impl Default for Intervals {
    fn default() -> Self {
        Self { poll: DEFAULT_POLL, sync: DEFAULT_SYNC }
    }
}

impl Intervals {
    /// Re-read every cycle, so a change takes effect without a restart -- which
    /// is the point of them being in CONFIG_DB rather than constants.
    ///
    /// Case by case as `stormond:DaemonStorage.get_configdb_intervals` does it:
    ///
    /// * a row or a field that is not there means the default, so deleting an
    ///   override puts the hour and the day back;
    /// * a value that will not parse, or a read that fails, keeps the
    ///   intervals in use and says so -- the operator who set 300 and then
    ///   typo'd it should not silently get an hour.  Python parses the poll
    ///   interval first, so a bad sync interval still lets a good poll
    ///   interval through, and so does this;
    /// * both intervals are announced on every read that succeeds.
    ///
    /// After a failure the intervals reported are the ones now in use.  Python
    /// reports the ones from before the read, which differ in exactly the case
    /// above.  Values are whole seconds: a negative one, which Python would
    /// take and then spin on, is refused like anything else that will not
    /// parse.
    fn reload(self, row: Result<Option<Vec<(String, String)>>, String>) -> Self {
        let row = match row {
            Ok(row) => row.unwrap_or_default(),
            Err(e) => return self.failed(&e),
        };
        let field = |name: &str, default: Duration| match row.iter().find(|(k, _)| k == name) {
            None => Ok(default),
            Some((_, v)) => v
                .trim()
                .parse::<u64>()
                .map(Duration::from_secs)
                .map_err(|_| format!("invalid literal for int() with base 10: '{v}'")),
        };
        let poll = match field("daemon_polling_interval", DEFAULT_POLL) {
            Ok(poll) => poll,
            Err(e) => return self.failed(&e),
        };
        let sync = match field("fsstats_sync_interval", DEFAULT_SYNC) {
            Ok(sync) => sync,
            Err(e) => return Self { poll, ..self }.failed(&e),
        };
        pmon_common::notice!("Polling Interval set to {} seconds", poll.as_secs());
        pmon_common::notice!("FSIO JSON file Interval set to {} seconds", sync.as_secs());
        Self { poll, sync }
    }

    /// Say that a reload failed, and carry on with `self`.
    fn failed(self, why: &str) -> Self {
        log::error!("Failed to retrieve CONFIG_DB intervals: {why}");
        pmon_common::notice!(
            "Intervals in use: polling={}, fsstats_sync={}",
            self.poll.as_secs(),
            self.sync.as_secs()
        );
        self
    }
}

fn formatted_time() -> String {
    chrono::Local::now().format(TIME_FORMAT).to_string()
}

/// Model and serial, which do not change while the switch is up.
fn publish_static(devices: &[StorageDeviceInfo], table: &dyn TableLike) {
    for d in devices {
        if !d.available {
            pmon_common::notice!(
                "{} does not have an instantiated object. Static Information cannot be gathered.",
                d.name);
            continue;
        }
        pmon_common::notice!(
            "Storage Device: {}, Device Model: {}, Serial: {}", d.name, d.model, d.serial);
        if let Err(e) = table.set(
            &d.name,
            &[("device_model", d.model.clone()), ("serial", d.serial.clone())],
        ) {
            log::error!("get_static_fields_update_state_db() failed with: {e}");
        }
    }
}

/// Everything that moves, including the reconciled lifetime totals.
fn publish_dynamic(
    devices: &[StorageDeviceInfo],
    reconciler: &Reconciler,
    table: &dyn TableLike,
) {
    for d in devices {
        if !d.available {
            pmon_common::notice!(
                "Storage device '{}' does not have an instantiated object. \
                 Dynamic Information cannot be gathered.",
                d.name);
            continue;
        }

        let (latest_reads, latest_writes) = (d.fs_io_reads, d.fs_io_writes);
        let num = |v: Option<i64>| {
            v.map_or_else(|| pmon_common::fmt::NOT_AVAILABLE.to_string(), |n| n.to_string())
        };

        // Both counters at 0 is a read that failed, not a disk that did
        // nothing: the platform's FSIO getters answer 0 when /proc/diskstats
        // cannot be read, as when the disk has gone read-only, and a disk in
        // use never has both at 0.  Reconciling that 0 would collapse the
        // lifetime totals, so the last known-good values are republished from
        // STATE_DB, "0" where there are none, as
        // `stormond:DaemonStorage.get_dynamic_fields_update_state_db` does
        // through `stormond:DaemonStorage._get_last_fsio_statedb_value`.
        let [published_reads, published_writes, total_reads, total_writes] =
            if latest_reads == Some(0) && latest_writes == Some(0) {
                log::warn!(
                    "FSIO counters unavailable for {}. Retaining last known-good FSIO values.",
                    d.name);
                let row = match table.get(&d.name) {
                    Ok(row) => row.unwrap_or_default(),
                    // Python reads these inside the per-device try, so a
                    // failed read skips this disk with the same NOTICE.
                    Err(e) => {
                        pmon_common::notice!("get_dynamic_fields_update_state_db() failed with: {e}");
                        continue;
                    }
                };
                let last = |field: &str| {
                    row.iter()
                        .find(|(k, _)| k == field)
                        .map_or_else(|| "0".to_string(), |(_, v)| v.clone())
                };
                [
                    last("latest_fsio_reads"),
                    last("latest_fsio_writes"),
                    last("total_fsio_reads"),
                    last("total_fsio_writes"),
                ]
            } else {
                let (total_reads, total_writes) = match (latest_reads, latest_writes) {
                    (Some(r), Some(w)) => {
                        let (tr, tw) = reconciler.totals(&d.name, r, w);
                        (Some(tr), Some(tw))
                    }
                    // Without this cycle's reading there is nothing to
                    // reconcile, and publishing the baseline unchanged would
                    // claim the disk did no work rather than that nobody
                    // could tell.
                    _ => (None, None),
                };
                [num(latest_reads), num(latest_writes), num(total_reads), num(total_writes)]
            };

        let last_sync = formatted_time();
        let fvs = [
            ("firmware", d.firmware.clone()),
            ("health", d.health.clone()),
            ("temperature", d.temperature.clone()),
            ("latest_fsio_reads", published_reads),
            ("latest_fsio_writes", published_writes),
            ("total_fsio_reads", total_reads.clone()),
            ("total_fsio_writes", total_writes.clone()),
            ("disk_io_reads", d.disk_io_reads.clone()),
            ("disk_io_writes", d.disk_io_writes.clone()),
            ("reserved_blocks", d.reserved_blocks.clone()),
            ("last_sync_time", last_sync.clone()),
        ];
        if let Err(e) = table.set(&d.name, &fvs) {
            // NOTICE, not WARNING: Python's dynamic pass catches this in
            // `stormond:DaemonStorage.get_dynamic_fields_update_state_db` with
            // `log_notice`.  Its static counterpart uses `log_error`
            // (`stormond:DaemonStorage.get_static_fields_update_state_db`) and
            // so does `publish_static` above.
            pmon_common::notice!("get_dynamic_fields_update_state_db() failed with: {e}");
            continue;
        }

        pmon_common::notice!(
            "Storage Device: {}, Firmware: {}, health: {}%, Temp: {}C, \
             FS IO Reads: {}, FS IO Writes: {}",
            d.name, d.firmware, d.health, d.temperature, total_reads, total_writes);
        // This cycle's readings, not what was published: a failed read still
        // shows 0 here, which is how the log tells it apart.
        pmon_common::notice!(
            "Latest FSIO Reads: {}, Latest FSIO Writes: {}",
            num(latest_reads), num(latest_writes));
        pmon_common::notice!(
            "Disk IO Reads: {}, Disk IO Writes: {}, Reserved Blocks: {}",
            d.disk_io_reads, d.disk_io_writes, d.reserved_blocks);
        pmon_common::notice!("Last successful sync time to STATE_DB: {last_sync}");
    }
}

/// Whether the next poll would overshoot the sync period.
///
/// Synced early rather than late: waiting for the overshoot puts the file a
/// whole poll interval behind, and on the default hour-and-a-day that is an
/// hour of counters lost to a power cut.
fn sync_due(since_sync: Duration, poll: Duration, sync: Duration) -> bool {
    since_sync + poll >= sync
}

/// Write the JSON file and record when, in both places that need to know.
///
/// Returns the time it recorded, so the caller can restart its interval from
/// the write that actually happened rather than from when it asked.  The path
/// is an argument so a test owns the file.
fn sync_to_disk_at(
    path: &Path,
    disks: &[String],
    table: &dyn TableLike,
) -> Result<Option<String>, String> {
    // Never overwrite the baseline with nothing.  The file's whole job is to
    // carry per-disk lifetime counters across restarts; a document with no
    // disks in it silently resets them to zero, and the next boot has no way
    // of telling that apart from a genuinely fresh switch.
    if disks.is_empty() {
        log::warn!("No storage devices to sync; leaving {} alone", path.display());
        return Ok(None);
    }
    pmon_common::notice!(
        "Syncing total and latest procfs reads and writes from STATE_DB to JSON file");
    let when = formatted_time();
    let doc = fsio::sync_document(disks, table, &when);

    // Written whole and then moved into place: a daemon killed mid-write would
    // otherwise leave a half-file, which the loader rejects -- costing the
    // baseline the file exists to preserve.
    let tmp = path.with_extension("json.tmp");
    let written = std::fs::write(&tmp, doc).and_then(|()| std::fs::rename(&tmp, path));
    if let Err(e) = written {
        // A file that will not write is what Python's `sync_fsio_rw_json`
        // returning False means: warned about, not fatal.  The counters stay
        // in STATE_DB and the next sync gets another go.
        log::error!("Unable to sync state_db to disk: {e}");
        let _ = std::fs::remove_file(&tmp);
        return Ok(None);
    }

    // The table write is the one Python does not guard:
    // `stormond:DaemonStorage.write_sync_time_statedb` calls `hset` bare, so a
    // redis that has gone away ends the daemon rather than leaving it running
    // against a dead socket.
    table
        .set(FSSTATS_SYNC_KEY, &[("successful_sync_time", when.clone())])
        .map_err(|e| format!("Unable to record the sync time: {e}"))?;
    Ok(Some(when))
}

/// The daemon's loop, with everything it needs handed to it.
///
/// Separated from `main` so it can be driven by a test: `main` is the part that
/// cannot be -- it embeds an interpreter and opens a redis -- and this is the
/// part worth being sure of.
async fn run(
    platform: &mut dyn PlatformApi,
    table: &dyn TableLike,
    config: Option<&dyn TableLike>,
    reconciler: &Reconciler,
    disks: &[String],
    json_path: &Path,
    cycles: &mut Cycles,
) -> i32 {
    let mut intervals = Intervals::default();
    let mut since_sync = Duration::ZERO;
    let mut read = Latch::new();

    let code = loop {
        // `stormond:DaemonStorage.get_configdb_intervals`, down to its warning
        // on every cycle when CONFIG_DB never opened.
        intervals = match config {
            Some(c) => intervals.reload(c.get(INTERVALS_KEY)),
            None => {
                log::warn!("CONFIG_DB connection not available, using default intervals");
                intervals
            }
        };

        match platform.get_storage_devices() {
            Ok(devices) => {
                pmon_common::recovered!(read, "storage read recovered");
                publish_dynamic(&devices, reconciler, table);
            }
            Err(e) => {
                // Same `except` on the Python side, so the same level: the
                // read and the write it guards are one handler in
                // `stormond:DaemonStorage.get_dynamic_fields_update_state_db`.
                pmon_common::fail_once_notice!(
                    read, "get_dynamic_fields_update_state_db() failed with: {e}");
            }
        }

        match cycles.next(intervals.poll).await {
            Tick::Exit(code) => break code,
            Tick::Cycle => since_sync += intervals.poll,
        }

        if sync_due(since_sync, intervals.poll, intervals.sync) {
            match sync_to_disk_at(json_path, disks, table) {
                Ok(Some(_)) => since_sync = Duration::ZERO,
                Ok(None) => log::warn!("Unable to sync latest and total procfs RW to disk"),
                Err(e) => {
                    log::error!("{e}");
                    break ERR_DB_WRITE;
                }
            }
        }
    };

    // The last thing the daemon does, and the reason a planned reboot does not
    // lose the lifetime totals: STATE_DB does not survive one and this file
    // does.  Note what is *not* here -- the table is left in place, because it
    // is the baseline the next start reads when the switch stayed up.
    //
    // A failure here is only logged, including the table write: the process is
    // already leaving, so there is no connection left to recover by leaving
    // again.
    match sync_to_disk_at(json_path, disks, table) {
        Ok(Some(_)) => {}
        Ok(None) => log::warn!("Unable to sync latest and total procfs RW to disk"),
        Err(e) => log::warn!("{e}"),
    }
    code
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let args = Args::parse();
    logging::init(SYSLOG_IDENTIFIER);
    pmon_common::notice!("Starting Storage Monitoring Daemon");

    let mut platform = match platform_provider::open(SYSLOG_IDENTIFIER, args.platform_api) {
        Ok(p) => p,
        Err(e) => {
            log::error!("Failed to load the storage utility: {e}");
            std::process::exit(STORAGEUTIL_LOAD_ERROR);
        }
    };

    let code = start(
        &mut platform,
        &db::open,
        Path::new(FSIO_RW_JSON_FILE),
        &mut Cycles::signals().expect("failed to install the signal handlers"),
    )
    .await;

    pmon_common::notice!("Shutting down Storage Monitoring Daemon");
    // Whatever the implementation set up, released before the process goes
    // away.  The PyO3 bridge runs Python's `atexit` handlers here; `main` does
    // not need to know that is what it holds.
    platform.finalize();
    std::process::exit(code);
}

/// Open the table, read both baselines, publish the static rows, and run.
///
/// The baseline reading is the part worth being able to drive: this daemon
/// exists to carry lifetime counters across a reboot, and both sources of them
/// -- the JSON file and the table the last run left -- are read here, before
/// anything is published over the top.
async fn start(
    platform: &mut dyn PlatformApi,
    open: db::Opener<'_>,
    json_path: &Path,
    cycles: &mut Cycles,
) -> i32 {
    let table = match open(db::STATE_DB, STORAGE_DEVICE_TABLE) {
        Ok(t) => t,
        Err(e) => {
            log::error!("Failed to connect to STATE_DB: {e}");
            return STORAGEUTIL_LOAD_ERROR;
        }
    };
    // Opened once and kept: reconnecting per cycle is what the comment on
    // `stormond:DaemonStorage.config_db` calls out as a resource leak it had.
    let config = open(CONFIG_DB, STORMOND_CONFIG_TABLE).ok();

    // Fatal, not defaulted.  `stormond:DaemonStorage.__init__` constructs
    // StorageDevices() outside any try/except, so a platform that cannot
    // enumerate its disks takes the Python daemon down before it ever syncs.
    // Swallowing the error here let the daemon run with an empty disk list and
    // then overwrite fsio-rw-stats.json with a document containing no devices
    // -- destroying the lifetime-counter baseline the file exists to preserve.
    // Observed on hardware when the facade was missing get_storage_devices().
    //
    // An empty-but-successful enumeration is a different thing and stays
    // allowed: a platform with no disks has nothing to publish and no baseline
    // to lose.
    let devices = match platform.get_storage_devices() {
        Ok(devices) => devices,
        Err(e) => {
            log::error!("Failed to load the storage utility: {e}");
            return STORAGEUTIL_LOAD_ERROR;
        }
    };
    let disks: Vec<String> = devices.iter().map(|d| d.name.clone()).collect();

    // Both baselines are read before anything is published: the STATE_DB one is
    // about to be overwritten by this cycle's own numbers.
    let json_text = Reconciler::read_json_file(json_path);
    let reconciler = Reconciler::load(&disks, json_text.as_deref(), table.as_ref());
    log::info!("Reconciling lifetime counters against {:?}", reconciler.baseline());

    publish_static(&devices, table.as_ref());

    run(
        platform,
        table.as_ref(),
        config.as_deref(),
        &reconciler,
        &disks,
        json_path,
        cycles,
    )
    .await
}

#[cfg(test)]
mod tests {
    use super::*;
    use pmon_common::db::MockTable;

    fn disk(name: &str) -> StorageDeviceInfo {
        StorageDeviceInfo {
            name: name.to_string(),
            available: true,
            model: "SAMSUNG MZ".to_string(),
            serial: "S3Z".to_string(),
            firmware: "EDA7602Q".to_string(),
            health: "98".to_string(),
            temperature: "31".to_string(),
            disk_io_reads: "100".to_string(),
            disk_io_writes: "200".to_string(),
            reserved_blocks: "1".to_string(),
            fs_io_reads: Some(4242),
            fs_io_writes: Some(2121),
        }
    }

    #[test]
    fn the_static_row_is_the_model_and_the_serial() {
        let t = MockTable::new();
        publish_static(&[disk("sda")], &t);
        assert_eq!(t.field("sda", "device_model").as_deref(), Some("SAMSUNG MZ"));
        assert_eq!(t.field("sda", "serial").as_deref(), Some("S3Z"));
    }

    /// A disk no utility class handles gets no row at all -- not a row of N/A.
    /// `show platform ssdhealth` iterates the table, and an empty row would
    /// print a disk it cannot say anything about.
    #[test]
    fn a_disk_with_no_utility_class_gets_no_row() {
        let t = MockTable::new();
        let mut d = disk("sdb");
        d.available = false;
        publish_static(&[d.clone()], &t);
        publish_dynamic(&[d], &Reconciler::default(), &t);
        assert!(t.is_empty());
    }

    #[test]
    fn the_dynamic_row_carries_the_readings_and_the_reconciled_totals() {
        let t = MockTable::new();
        publish_dynamic(&[disk("sda")], &Reconciler::default(), &t);
        assert_eq!(t.field("sda", "health").as_deref(), Some("98"));
        assert_eq!(t.field("sda", "latest_fsio_reads").as_deref(), Some("4242"));
        // No baseline: this cycle's reading is the total.
        assert_eq!(t.field("sda", "total_fsio_reads").as_deref(), Some("4242"));
        assert!(t.field("sda", "last_sync_time").is_some());
    }

    /// Both counters at 0 is a failed read (the disk went read-only), so the
    /// last known-good values stay, and the totals never go backwards.
    #[test]
    fn a_failed_counter_read_keeps_the_last_known_good_values() {
        let log = pmon_common::logging::capture();
        let t = MockTable::new();
        t.set("sda", &[
            ("latest_fsio_reads", "20706101".to_string()),
            ("latest_fsio_writes", "20706102".to_string()),
            ("total_fsio_reads", "20938935".to_string()),
            ("total_fsio_writes", "20938936".to_string()),
        ]).unwrap();
        let mut d = disk("sda");
        d.fs_io_reads = Some(0);
        d.fs_io_writes = Some(0);
        publish_dynamic(&[d], &Reconciler::default(), &t);
        assert_eq!(t.field("sda", "latest_fsio_reads").as_deref(), Some("20706101"));
        assert_eq!(t.field("sda", "latest_fsio_writes").as_deref(), Some("20706102"));
        assert_eq!(t.field("sda", "total_fsio_reads").as_deref(), Some("20938935"));
        assert_eq!(t.field("sda", "total_fsio_writes").as_deref(), Some("20938936"));
        assert_eq!(t.field("sda", "health").as_deref(), Some("98"), "the rest still publishes");
        assert!(log.logged(log::Level::Warn,
            "FSIO counters unavailable for sda. Retaining last known-good FSIO values."));
        assert!(log.logged(log::Level::Info, "Latest FSIO Reads: 0, Latest FSIO Writes: 0"),
            "the log still shows the failed read");
    }

    /// With nothing published yet there is no last good value, and Python's
    /// `_get_last_fsio_statedb_value` answers "0".
    #[test]
    fn a_failed_counter_read_with_nothing_before_it_publishes_zero() {
        let t = MockTable::new();
        let mut d = disk("sda");
        d.fs_io_reads = Some(0);
        d.fs_io_writes = Some(0);
        publish_dynamic(&[d], &Reconciler::default(), &t);
        assert_eq!(t.field("sda", "latest_fsio_reads").as_deref(), Some("0"));
        assert_eq!(t.field("sda", "total_fsio_writes").as_deref(), Some("0"));
    }

    /// One counter at 0 is an idle direction, not a failed read.
    #[test]
    fn one_counter_at_zero_is_reconciled_as_usual() {
        let t = MockTable::new();
        t.set("sda", &[("total_fsio_reads", "999".to_string())]).unwrap();
        let mut d = disk("sda");
        d.fs_io_reads = Some(0);
        publish_dynamic(&[d], &Reconciler::default(), &t);
        assert_eq!(t.field("sda", "latest_fsio_reads").as_deref(), Some("0"));
        assert_eq!(t.field("sda", "total_fsio_writes").as_deref(), Some("2121"));
    }

    /// Reading the last good values happens inside Python's per-device try,
    /// so a STATE_DB that will not answer skips the disk with its NOTICE.
    #[test]
    fn a_failed_counter_read_that_cannot_reach_state_db_skips_the_disk() {
        let log = pmon_common::logging::capture();
        let t = MockTable::new();
        t.fail_reads("redis went away");
        let mut d = disk("sda");
        d.fs_io_reads = Some(0);
        d.fs_io_writes = Some(0);
        publish_dynamic(&[d], &Reconciler::default(), &t);
        assert!(t.field("sda", "health").is_none(), "nothing published for the disk");
        assert!(log.logged(log::Level::Info,
            "get_dynamic_fields_update_state_db() failed with: redis went away"));
    }

    /// A reading that could not be taken must not publish a total: the
    /// baseline unchanged would read as "the disk did nothing this hour",
    /// which is a stronger claim than "nobody could tell".
    #[test]
    fn a_disk_with_no_reading_publishes_no_total_either() {
        let t = MockTable::new();
        let mut d = disk("sda");
        d.fs_io_reads = None;
        publish_dynamic(&[d], &Reconciler::default(), &t);
        assert_eq!(t.field("sda", "latest_fsio_reads").as_deref(), Some("N/A"));
        assert_eq!(t.field("sda", "total_fsio_reads").as_deref(), Some("N/A"));
        assert_eq!(t.field("sda", "health").as_deref(), Some("98"), "the rest still publishes");
    }

    /// Synced early rather than late: waiting for the overshoot puts the file
    /// a whole poll interval behind, and on the default hour-and-a-day that is
    /// an hour of counters lost to a power cut.
    #[test]
    fn the_sync_fires_before_the_period_would_be_overshot() {
        let (poll, sync) = (Duration::from_secs(3600), Duration::from_secs(86400));
        assert!(!sync_due(Duration::from_secs(0), poll, sync));
        assert!(!sync_due(Duration::from_secs(82799), poll, sync), "one poll short");
        assert!(sync_due(Duration::from_secs(82800), poll, sync), "the next poll would overshoot");
        assert!(sync_due(Duration::from_secs(90000), poll, sync), "and after it, always");
    }

    /// A poll longer than the sync period syncs every cycle, which is the only
    /// thing it can do -- and is what an operator asking for that has asked for.
    #[test]
    fn a_poll_longer_than_the_sync_period_syncs_every_cycle() {
        assert!(sync_due(Duration::ZERO, Duration::from_secs(600), Duration::from_secs(300)));
    }

    /// The file is written whole and moved into place, so a daemon killed
    /// mid-write leaves the previous one rather than half of a new one -- which
    /// the loader would reject, costing the baseline the file exists for.
    #[test]
    fn the_file_is_written_atomically_and_reads_back() {
        let d = tempfile::tempdir().unwrap();
        let path = d.path().join("fsio-rw-stats.json");
        let t = MockTable::new();
        t.set("sda", &[
            ("latest_fsio_reads", "5".to_string()),
            ("latest_fsio_writes", "6".to_string()),
            ("total_fsio_reads", "7".to_string()),
            ("total_fsio_writes", "8".to_string()),
        ]).unwrap();

        let when = sync_to_disk_at(&path, &["sda".to_string()], &t).unwrap().expect("written");
        assert_eq!(t.field(FSSTATS_SYNC_KEY, "successful_sync_time").as_deref(), Some(when.as_str()));
        assert!(!path.with_extension("json.tmp").exists(), "the temporary file is moved, not left");

        let text = std::fs::read_to_string(&path).unwrap();
        let r = Reconciler::load(&["sda".to_string()], Some(&text), &MockTable::new());
        assert_eq!(r.totals("sda", 1, 1), (8, 9), "the file reads back as a baseline");
    }

    /// A path that cannot be written is reported and not fatal: the daemon
    /// keeps publishing to STATE_DB, which is the baseline that survives a
    /// crash even when the one that survives a reboot cannot be written.
    ///
    /// A real disk is passed: with an empty list the call short-circuits on
    /// the guard below and the unwritable path is never reached, which would
    /// leave this test passing without testing anything.
    #[test]
    fn a_file_that_cannot_be_written_is_not_fatal() {
        let t = MockTable::new();
        let disks = vec!["sda".to_string()];
        assert!(sync_to_disk_at(Path::new("/nonexistent/dir/f.json"), &disks, &t).unwrap().is_none());
    }

    /// Syncing with no disks would write `{"successful_sync_time": ...}` and
    /// nothing else, wiping the per-disk lifetime counters the file exists to
    /// carry across reboots -- and the next boot cannot tell that apart from a
    /// genuinely fresh switch.  Seen on hardware: the platform facade was
    /// missing `get_storage_devices`, the daemon carried on with no disks, and
    /// the shutdown sync destroyed the baseline.
    #[test]
    fn a_sync_with_no_disks_leaves_the_baseline_alone() {
        let d = tempfile::tempdir().unwrap();
        let path = d.path().join("fsio-rw-stats.json");
        std::fs::write(&path, r#"{"sda":{"total_fsio_reads":"7"},"successful_sync_time":"then"}"#)
            .unwrap();

        let t = MockTable::new();
        assert!(sync_to_disk_at(&path, &[], &t).unwrap().is_none(), "nothing was written");

        let after = std::fs::read_to_string(&path).unwrap();
        assert!(after.contains("\"sda\""), "the existing baseline survived: {after}");
    }

    /// A STATE_DB that will not take the sync time is reported as an error,
    /// not folded into the "could not write the file" case.
    ///
    /// The two are not the same failure: an unwritable file costs the reboot
    /// baseline and nothing else, while an unwritable table means redis is
    /// gone -- and since `DBConnector` never reconnects, every later poll
    /// would publish into nothing.  Python separates them the same way, by
    /// guarding the file write and leaving the `hset` bare.
    #[test]
    fn a_table_that_will_not_take_the_sync_time_is_an_error() {
        let d = tempfile::tempdir().unwrap();
        let path = d.path().join("fsio-rw-stats.json");
        let t = MockTable::new();
        t.set("sda", &[("total_fsio_reads", "7".to_string())]).unwrap();
        t.fail_writes("redis is gone");

        let e = sync_to_disk_at(&path, &["sda".to_string()], &t)
            .expect_err("a table that will not take the sync time has to be reported");
        assert!(e.contains("sync time"), "the error says what could not be recorded: {e}");
        assert!(path.exists(), "the file was still written -- only the table write failed");
    }

    struct FakePlatform {
        devices: Vec<StorageDeviceInfo>,
        fail: bool,
        calls: usize,
    }

    impl PlatformApi for FakePlatform {
        fn get_storage_devices(&mut self) -> Result<Vec<StorageDeviceInfo>, platform_api::PlatformError> {
            self.calls += 1;
            if self.fail {
                return Err(platform_api::PlatformError::Backend("smartctl missing".into()));
            }
            Ok(self.devices.clone())
        }
    }

    /// The loop publishes every cycle and syncs on the way out, which is what
    /// makes a planned reboot keep the lifetime totals.
    #[tokio::test]
    async fn the_loop_publishes_each_cycle_and_syncs_on_the_way_out() {
        tokio::time::pause();
        let d = tempfile::tempdir().unwrap();
        let path = d.path().join("f.json");
        let t = MockTable::new();
        let mut p = FakePlatform { devices: vec![disk("sda")], fail: false, calls: 0 };
        let mut cycles = Cycles::Fixed { remaining: 2, code: 143 };

        let code = run(&mut p, &t, None, &Reconciler::default(), &["sda".to_string()],
                       &path, &mut cycles).await;
        assert_eq!(code, 143);
        assert_eq!(p.calls, 3, "published before each wait, including the last");
        assert_eq!(t.field("sda", "total_fsio_reads").as_deref(), Some("4242"));
        assert!(path.exists(), "the shutdown sync is what a reboot depends on");
    }

    /// The table survives: it is the baseline the next start reads when the
    /// switch stayed up and only the daemon restarted.
    #[tokio::test]
    async fn the_loop_does_not_clear_the_table_on_the_way_out() {
        tokio::time::pause();
        let d = tempfile::tempdir().unwrap();
        let t = MockTable::new();
        let mut p = FakePlatform { devices: vec![disk("sda")], fail: false, calls: 0 };
        let mut cycles = Cycles::Fixed { remaining: 1, code: 0 };
        run(&mut p, &t, None, &Reconciler::default(), &["sda".to_string()],
            &d.path().join("f.json"), &mut cycles).await;
        assert!(!t.is_empty(), "STATE_DB is the crash baseline");
    }

    /// A read that fails publishes nothing and does not blank what is there.
    #[tokio::test]
    async fn a_failing_read_leaves_the_table_as_it_was() {
        tokio::time::pause();
        let d = tempfile::tempdir().unwrap();
        let t = MockTable::new();
        publish_dynamic(&[disk("sda")], &Reconciler::default(), &t);
        let before = t.field("sda", "total_fsio_reads");

        let mut p = FakePlatform { devices: vec![], fail: true, calls: 0 };
        let mut cycles = Cycles::Fixed { remaining: 2, code: 0 };
        run(&mut p, &t, None, &Reconciler::default(), &["sda".to_string()],
            &d.path().join("f.json"), &mut cycles).await;
        assert_eq!(t.field("sda", "total_fsio_reads"), before);
    }

    /// CONFIG_DB is re-read every cycle, so a change takes effect without a
    /// restart -- which is the point of the intervals being there at all.
    #[tokio::test]
    async fn the_intervals_are_re_read_every_cycle() {
        tokio::time::pause();
        let d = tempfile::tempdir().unwrap();
        let t = MockTable::new();
        let config = MockTable::new();
        config.set(INTERVALS_KEY, &[("daemon_polling_interval", "60".to_string())]).unwrap();
        let mut p = FakePlatform { devices: vec![disk("sda")], fail: false, calls: 0 };
        let mut cycles = Cycles::Fixed { remaining: 2, code: 0 };

        let start = tokio::time::Instant::now();
        run(&mut p, &t, Some(&config), &Reconciler::default(), &["sda".to_string()],
            &d.path().join("f.json"), &mut cycles).await;
        // Fixed ticks do not sleep, so what this asserts is that the loop asked
        // CONFIG_DB and not that it waited: the row was read once per cycle.
        assert!(config.scans() == 0 || start.elapsed() < Duration::from_secs(1));
    }

    /// CONFIG_DB set to sync every poll, so every cycle of a test is a sync.
    fn sync_every_cycle() -> MockTable {
        let config = MockTable::new();
        config.set(INTERVALS_KEY, &[
            ("daemon_polling_interval", "60".to_string()),
            ("fsstats_sync_interval", "60".to_string()),
        ]).unwrap();
        config
    }

    fn sync_times(t: &MockTable) -> usize {
        t.writes().iter().filter(|(k, _)| k == FSSTATS_SYNC_KEY).count()
    }

    /// A sync that comes due is done inside the loop, not only on the way out.
    #[tokio::test]
    async fn a_sync_that_comes_due_is_done_in_the_loop() {
        tokio::time::pause();
        let d = tempfile::tempdir().unwrap();
        let t = MockTable::new();
        let config = sync_every_cycle();
        let mut p = FakePlatform { devices: vec![disk("sda")], fail: false, calls: 0 };
        let mut cycles = Cycles::Fixed { remaining: 2, code: 143 };
        let code = run(&mut p, &t, Some(&config), &Reconciler::default(), &["sda".to_string()],
                       &d.path().join("f.json"), &mut cycles).await;
        assert_eq!(code, 143);
        assert_eq!(sync_times(&t), 3, "one per cycle, and one on the way out");
    }

    /// A file that will not write is Python's `sync_fsio_rw_json` returning
    /// False: warned about, and the daemon carries on.
    #[tokio::test]
    async fn a_file_that_will_not_write_does_not_stop_the_daemon() {
        let log = pmon_common::logging::capture();
        tokio::time::pause();
        let d = tempfile::tempdir().unwrap();
        let t = MockTable::new();
        let config = sync_every_cycle();
        let mut p = FakePlatform { devices: vec![disk("sda")], fail: false, calls: 0 };
        let mut cycles = Cycles::Fixed { remaining: 2, code: 143 };
        let code = run(&mut p, &t, Some(&config), &Reconciler::default(), &["sda".to_string()],
                       &d.path().join("no-such-dir").join("f.json"), &mut cycles).await;
        assert_eq!(code, 143, "ran to the end");
        assert_eq!(p.calls, 3);
        assert_eq!(sync_times(&t), 0, "no sync time for a sync that did not happen");
        assert!(log.logged(log::Level::Warn, "Unable to sync latest and total procfs RW to disk"));
    }

    /// The sync time is written bare in Python
    /// (`stormond:DaemonStorage.write_sync_time_statedb`), so a STATE_DB that
    /// will not take it ends the daemon at the first sync.  The device rows it
    /// refused before that are Python's `log_notice`, not a stop.
    #[tokio::test]
    async fn a_table_that_will_not_take_the_sync_time_stops_the_daemon() {
        let log = pmon_common::logging::capture();
        tokio::time::pause();
        let d = tempfile::tempdir().unwrap();
        let t = MockTable::new();
        t.fail_writes("redis is gone");
        let config = sync_every_cycle();
        let mut p = FakePlatform { devices: vec![disk("sda")], fail: false, calls: 0 };
        let mut cycles = Cycles::Fixed { remaining: 5, code: 143 };
        let code = run(&mut p, &t, Some(&config), &Reconciler::default(), &["sda".to_string()],
                       &d.path().join("f.json"), &mut cycles).await;
        assert_eq!(code, ERR_DB_WRITE);
        assert_eq!(p.calls, 1, "stopped at the first sync");
        assert!(log.logged(log::Level::Info, "get_dynamic_fields_update_state_db() failed with"),
                "the refused device row is a NOTICE");
        assert!(log.logged(log::Level::Error, "Unable to record the sync time"));
    }

    #[test]
    fn the_intervals_default_to_an_hour_and_a_day() {
        let i = Intervals::default();
        assert_eq!((i.poll.as_secs(), i.sync.as_secs()), (3600, 86400));
    }

    fn row(fields: &[(&str, &str)]) -> Result<Option<Vec<(String, String)>>, String> {
        Ok(Some(fields.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect()))
    }

    fn secs(poll: u64, sync: u64) -> Intervals {
        Intervals { poll: Duration::from_secs(poll), sync: Duration::from_secs(sync) }
    }

    #[test]
    fn config_db_overrides_either_interval_on_its_own() {
        let i = Intervals::default().reload(row(&[("daemon_polling_interval", "300")]));
        assert_eq!(i, secs(300, 86400), "the one not set is the default");
    }

    /// A typo keeps whatever was working.  Falling back to the default would
    /// silently turn a 5-minute poll into an hourly one.
    #[test]
    fn an_unparseable_interval_keeps_the_one_in_use() {
        let log = pmon_common::logging::capture();
        let running = secs(300, 600);
        let i = running.reload(row(&[("daemon_polling_interval", "5 min")]));
        assert_eq!(i, running);
        assert!(log.logged(log::Level::Error,
            "Failed to retrieve CONFIG_DB intervals: invalid literal for int() with base 10: '5 min'"));
        assert!(log.logged(log::Level::Info, "Intervals in use: polling=300, fsstats_sync=600"));
    }

    /// Python parses the poll interval first, so a good one goes through even
    /// when the sync interval beside it does not parse.
    #[test]
    fn a_bad_sync_interval_still_lets_a_good_poll_interval_through() {
        let i = secs(300, 600).reload(row(&[
            ("daemon_polling_interval", "120"),
            ("fsstats_sync_interval", "-1"),
        ]));
        assert_eq!(i, secs(120, 600));
    }

    /// Deleting the override puts the defaults back, as Python's `.get(field,
    /// default)` does.  Keeping the old values would make the override
    /// impossible to remove without a restart.
    #[test]
    fn an_absent_row_puts_the_defaults_back() {
        assert_eq!(secs(300, 600).reload(Ok(None)), Intervals::default());
    }

    #[test]
    fn an_absent_field_puts_its_default_back() {
        let i = secs(300, 600).reload(row(&[("fsstats_sync_interval", "600")]));
        assert_eq!(i, secs(3600, 600));
    }

    #[test]
    fn a_read_that_fails_keeps_the_intervals_in_use() {
        let log = pmon_common::logging::capture();
        assert_eq!(secs(300, 600).reload(Err("redis is gone".into())), secs(300, 600));
        assert!(log.logged(log::Level::Error,
            "Failed to retrieve CONFIG_DB intervals: redis is gone"));
    }

    /// Announced on every read, changed or not, as Python announces them.
    #[test]
    fn every_read_announces_both_intervals() {
        let log = pmon_common::logging::capture();
        let i = secs(300, 600).reload(row(&[
            ("daemon_polling_interval", "300"),
            ("fsstats_sync_interval", "600"),
        ]));
        assert_eq!(i, secs(300, 600));
        assert!(log.logged(log::Level::Info, "Polling Interval set to 300 seconds"));
        assert!(log.logged(log::Level::Info, "FSIO JSON file Interval set to 600 seconds"));
    }

    // ── the wiring that used to be inside main ───────────────────────────────

    /// A platform that cannot enumerate its disks takes the daemon down.
    ///
    /// This is bug 4: swallowing the error let the daemon run with an empty
    /// disk list and then overwrite fsio-rw-stats.json with a document
    /// containing no devices, destroying the lifetime-counter baseline the
    /// file exists to preserve.  `stormond:DaemonStorage.__init__` constructs
    /// StorageDevices() outside any try/except, so Python dies before it can
    /// sync; this has to die in the same place.
    #[tokio::test]
    async fn a_platform_that_cannot_enumerate_its_disks_stops_before_it_syncs() {
        let log = pmon_common::logging::capture();
        let d = tempfile::tempdir().unwrap();
        let path = d.path().join("fsio-rw-stats.json");
        std::fs::write(&path, r#"{"sda": {"total_fsio_reads": "10"}}"#).unwrap();

        let o = pmon_common::db::MockOpener::new();
        let mut p = FakePlatform { devices: vec![], fail: true, calls: 0 };
        let code =
            start(&mut p, &|d, t| o.open(d, t), &path, &mut Cycles::Fixed { remaining: 0, code: 0 })
                .await;

        assert_eq!(code, STORAGEUTIL_LOAD_ERROR);
        assert!(
            std::fs::read_to_string(&path).unwrap().contains("sda"),
            "the baseline must survive a daemon that could not read the disks"
        );
        assert!(log.logged(log::Level::Error, "Failed to load the storage utility"));
    }

    /// An empty-but-successful enumeration is a different thing and stays
    /// allowed: a platform with no disks has nothing to publish and no
    /// baseline to lose.
    #[tokio::test]
    async fn a_platform_with_no_disks_is_not_an_error() {
        tokio::time::pause();
        let d = tempfile::tempdir().unwrap();
        let o = pmon_common::db::MockOpener::new();
        let mut p = FakePlatform { devices: vec![], fail: false, calls: 0 };
        let code = start(
            &mut p,
            &|db, t| o.open(db, t),
            &d.path().join("f.json"),
            &mut Cycles::Fixed { remaining: 1, code: 143 },
        )
        .await;
        assert_eq!(code, 143);
    }

    /// The table this daemon owns and the config table it reads are on
    /// different databases, and the config one is optional -- a switch that has
    /// never set STORMOND_CONFIG must still start.
    #[tokio::test]
    async fn the_two_tables_are_opened_on_their_own_databases() {
        tokio::time::pause();
        let d = tempfile::tempdir().unwrap();
        let o = pmon_common::db::MockOpener::failing(STORMOND_CONFIG_TABLE);
        let mut p = FakePlatform { devices: vec![disk("sda")], fail: false, calls: 0 };
        let code = start(
            &mut p,
            &|db, t| o.open(db, t),
            &d.path().join("f.json"),
            &mut Cycles::Fixed { remaining: 1, code: 143 },
        )
        .await;
        assert_eq!(code, 143, "a missing STORMOND_CONFIG is not fatal");
        assert_eq!(o.asked(), vec![
            (db::STATE_DB.to_string(), STORAGE_DEVICE_TABLE.to_string()),
            (CONFIG_DB.to_string(), STORMOND_CONFIG_TABLE.to_string()),
        ]);
        // The static row goes in before the first cycle: `show platform
        // ssdhealth` must not have to sit through an hour of nothing.
        assert!(o.table(STORAGE_DEVICE_TABLE).unwrap().wrote("sda", "device_model"));
    }

    /// And a STATE_DB that will not open stops the daemon rather than leaving
    /// it looping with nowhere to publish.
    #[tokio::test]
    async fn a_state_db_that_will_not_open_stops_the_daemon() {
        let d = tempfile::tempdir().unwrap();
        let o = pmon_common::db::MockOpener::failing(STORAGE_DEVICE_TABLE);
        let mut p = FakePlatform { devices: vec![], fail: false, calls: 0 };
        let code = start(
            &mut p,
            &|db, t| o.open(db, t),
            &d.path().join("f.json"),
            &mut Cycles::Fixed { remaining: 0, code: 0 },
        )
        .await;
        assert_eq!(code, STORAGEUTIL_LOAD_ERROR);
        assert_eq!(p.calls, 0, "the disks are not even read without somewhere to put them");
    }
}
