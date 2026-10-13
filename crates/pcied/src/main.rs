//! pcied, in Rust.  Ports `sonic-pcied/scripts/pcied`.
//!
//! Once a minute, check the machine against the parts list in its `pcie.yaml`
//! and publish what was found: PCIE_DEVICES|status as PASSED or FAILED, and one
//! PCIE_DEVICE row per device carrying its id and its AER counters.
//!
//! This is the one pmon daemon that does not reach the platform through the
//! chassis; see the two escape hatches behind `get_pcie_devices`.

mod detach;

use std::path::Path;
use std::time::Duration;

use platform_api::{ChassisInfoCols, PcieDevice, PlatformApi, PlatformError};
use clap::Parser;
use platform_provider::PlatformImpl;
use pmon_common::cycles::{Cycles, Tick};
use pmon_common::db::{self, TableLike};
use pmon_common::logging;
use pmon_common::report_once::Latch;

const SYSLOG_IDENTIFIER: &str = "pcied";

/// The command line, which is one switch.
///
/// Which vendor package gets imported is not here and never was: the image
/// installs one `sonic_platform` and which one is its business.  What is here
/// is which *implementation* of the platform API to open, because a platform
/// with a native Rust one has to be able to say so without a new binary.
#[derive(Parser, Debug)]
#[command(name = "pcied-rs", about = "SONiC PCIe daemon, in Rust")]
struct Args {
    /// Which platform API implementation to use: `pyo3` or `native`.
    ///
    /// Filled in from `platform_api_pcied` in `pmon_daemon_control.json` by the
    /// supervisord template.  Absent means `pyo3`, which is what every platform
    /// runs today -- an unset switch has to leave a platform on the
    /// implementation it has always had.
    #[arg(long, default_value_t = PlatformImpl::Pyo3)]
    platform_api: PlatformImpl,
}

const PCIE_DEVICE_TABLE: &str = "PCIE_DEVICE";
const PCIE_STATUS_TABLE: &str = "PCIE_DEVICES";
const PCIE_DETACH_INFO_TABLE: &str = "PCIE_DETACH_INFO";

/// The kernel's, not the platform's: the path is fixed and the file is the
/// kernel's own, so a facade call would be a round trip through Python to
/// `open()`.
const PCI_DEVICES_DIR: &str = "/sys/bus/pci/devices";

/// `pcied:PCIED_MAIN_THREAD_SLEEP_SECS`.
const UPDATE_PERIOD: Duration = Duration::from_secs(60);

/// `pcied:PCIEUTIL_CONF_FILE_ERROR` and `pcied:PCIEUTIL_LOAD_ERROR`.
/// supervisord reads the code.
const PCIEUTIL_CONF_FILE_ERROR: i32 = 1;
const PCIEUTIL_LOAD_ERROR: i32 = 2;

/// What the daemon leaves with when a table it must read is unreachable.
///
/// Python has no constant: the `getKeys()` in
/// `pcied:DaemonPcied.is_dpu_in_detaching_mode` is unwrapped and the
/// interpreter exits 1.  Non-zero is what supervisord's
/// `autorestart=unexpected` reads, and a restart is the only way back --
/// `DBConnector` does not reconnect.
const ERR_DB_READ: i32 = 1;

/// The one `ChassisInfo` column pcied reads.
///
/// It asks whether this is a SmartSwitch and nothing else; the other nineteen
/// columns reach the vendor for a reboot cause, a serial, a slot number that
/// pcied has no use for.  See `Snapshot(projected=True)` in platform_api's
/// facade, which is what leaves the undeclared columns unread.
const CHASSIS_COLS: ChassisInfoCols = ChassisInfoCols::IS_SMARTSWITCH;

/// `bb:dd.f`, which is the PCIE_DEVICE key and the sysfs directory name.
///
/// Without the domain: the daemon's key has never carried it
/// (`pcied:DaemonPcied.device_name`), and everything SONiC runs on is domain 0.
fn device_key(d: &PcieDevice) -> String {
    format!("{:02x}:{:02x}.{}", d.bus, d.dev, d.r#fn)
}

/// The same device as a full BDF, which is how PCIE_DETACH_INFO names it.
fn bus_info(d: &PcieDevice) -> String {
    format!("0000:{:02x}:{:02x}.{}", d.bus, d.dev, d.r#fn)
}

/// The device's PCI id, straight out of sysfs.
///
/// Read here rather than through the platform API because it is not platform
/// knowledge: the file is the kernel's, the path is fixed, and a facade call
/// would be a round trip through Python to `open()`.  The root is an argument
/// so a test can supply one.
fn read_id_in(root: &Path, key: &str) -> Option<String> {
    std::fs::read_to_string(root.join(format!("0000:{key}")).join("device"))
        .ok()
        .map(|s| s.trim().to_string())
}

/// One pass: check every device, publish what was found, answer the count of
/// devices that were expected and are not there.
fn check_devices(
    platform: &mut dyn PlatformApi,
    devices: &[PcieDevice],
    detaching: &dyn Fn(&str) -> bool,
    sysfs: &Path,
    table: &dyn TableLike,
) -> usize {
    let mut missing = 0;
    for d in devices {
        let key = device_key(d);
        if !d.present {
            // A DPU being detached is expected to vanish, and saying so every
            // minute would train an operator to ignore the message that
            // matters (`pcied:DaemonPcied.check_pcie_devices`).
            if detaching(&bus_info(d)) {
                log::debug!("PCIe Device: {} is in detaching mode, skipping warning.", bus_info(d));
                continue;
            }
            log::warn!("PCIe Device: {} Not Found", d.name);
            missing += 1;
            continue;
        }

        let Some(id) = read_id_in(sysfs, &key) else { continue };
        if let Err(e) = table.set(&key, &[("id", id)]) {
            log::error!("Exception while checking AER attributes for {key}: {e}");
            continue;
        }
        publish_aer(platform, d, &key, table);
    }
    missing
}

/// The device's AER counters, as `severity|field` columns of its row.
fn publish_aer(platform: &mut dyn PlatformApi, d: &PcieDevice, key: &str, table: &dyn TableLike) {
    let stats = match platform.get_pcie_aer_stats(d.bus, d.dev, d.r#fn) {
        Ok(s) => s,
        Err(e) => {
            log::error!("Exception while checking AER attributes for {key}: {e}");
            return;
        }
    };
    if stats.is_empty() {
        // Most devices expose no AER files at all; that is not a fault.
        log::debug!("PCIe device {key} has no AER attributes");
        return;
    }
    let fvs: Vec<(String, String)> = stats
        .iter()
        .map(|s| (format!("{}|{}", s.severity, s.field), s.value.clone()))
        .collect();
    let fvs: Vec<(&str, String)> = fvs.iter().map(|(k, v)| (k.as_str(), v.clone())).collect();
    if let Err(e) = table.set(key, &fvs) {
        log::error!("Exception while updating AER attributes to STATE_DB for {key}: {e}");
    }
}

/// PCIE_DEVICES|status: PASSED or FAILED for the scan as a whole.
///
/// Nothing in the tree reads it -- `show platform pcieinfo` calls the platform
/// API directly and system-health does not look at this table -- but it is
/// also written by `files/image_config/pcie-check/pcie-check.sh`, which runs
/// once on the host at boot.  Two writers, one key: the last one to run wins,
/// and after the first poll that is always this daemon.  Kept because the
/// Python daemon published it and something outside the tree may consume it.
fn publish_status(missing: usize, table: &dyn TableLike) {
    let status = if missing > 0 { "FAILED" } else { "PASSED" };
    if missing > 0 {
        log::error!("PCIe device status check : {status}");
    } else {
        log::info!("PCIe device status check : {status}");
    }
    if let Err(e) = table.set("status", &[("status", status.to_string())]) {
        log::error!("Exception while updating PCIe device status to STATE_DB: {e}");
    }
}

/// Drop every row of a table this daemon owns.
fn clear(table: &dyn TableLike) {
    // Swallowed, unlike the detach table below: Python's cleanup reads these
    // keys inside a `try` (`pcied:DaemonPcied.__del__`) and warns rather than
    // leaving.
    for key in table.get_keys().unwrap_or_default() {
        if let Err(e) = table.del(&key) {
            log::warn!("Exception during cleanup: {e}");
        }
    }
}

/// One pass: check the parts list and publish the verdict.
///
/// An `Err` is a database this process has lost; the caller ends the daemon.
/// `sysfs` is the PCI devices directory -- [`PCI_DEVICES_DIR`] on a switch, a
/// directory the test owns otherwise.
fn one_pass(
    platform: &mut dyn PlatformApi,
    is_smartswitch: bool,
    sysfs: &Path,
    tables: &Tables<'_>,
    read: &mut Latch,
) -> Result<(), String> {
    let devices = match platform.get_pcie_devices() {
        Ok(d) => {
            pmon_common::recovered!(read, "PCIe device check recovered");
            d
        }
        Err(e) => {
            pmon_common::fail_once!(read, "Failed to check PCIe devices: {e}");
            return Ok(());
        }
    };
    // A platform with no config file has nothing to check, and saying PASSED
    // about an empty parts list would be a claim nobody made.  Python returns
    // early on `None` (`pcied:DaemonPcied.check_pcie_devices`).
    if devices.is_empty() {
        return Ok(());
    }

    // Python does not guard this read, so a lost redis ends the daemon there
    // too.  Carrying on would mean reporting a DPU's device missing while it
    // is being detached deliberately.
    let detaching = detach::Detaching::read(is_smartswitch, tables.detach)
        .map_err(|e| format!("Failed to read the detach table: {e}"))?;
    let missing = check_devices(
        platform,
        &devices,
        &|bdf| detaching.contains(bdf),
        sysfs,
        tables.device,
    );
    publish_status(missing, tables.status);
    Ok(())
}

/// The three tables this daemon reads and writes.
pub struct Tables<'a> {
    pub device: &'a dyn TableLike,
    pub status: &'a dyn TableLike,
    pub detach: &'a dyn TableLike,
}

/// The owned handles behind those three.
struct Handles {
    device: Box<dyn TableLike>,
    status: Box<dyn TableLike>,
    detach: Box<dyn TableLike>,
}

/// Open every table, naming the one that refused.
///
/// The set is fixed, so it is worth an assertion: a typo in a table name is
/// otherwise invisible until the daemon is on a switch, where it shows up as a
/// table nobody is writing.
///
/// The message starts as Python's does (`pcied:DaemonPcied.__init__`), so a
/// log pattern written against the Python daemon still matches; the table
/// name is added after it because Python opens all three in one `try` and
/// cannot say which one it was.
fn open_all(open: db::Opener<'_>) -> Result<Handles, String> {
    let one = |name: &str| {
        open(db::STATE_DB, name).map_err(|e| {
            format!("Failed to connect to STATE_DB or create table {name}. Error: {e}")
        })
    };
    Ok(Handles {
        device: one(PCIE_DEVICE_TABLE)?,
        status: one(PCIE_STATUS_TABLE)?,
        detach: one(PCIE_DETACH_INFO_TABLE)?,
    })
}

/// The daemon's loop, with everything it needs handed to it.
///
/// Separated from `main` so it can be driven by a test: `main` is the part that
/// cannot be -- it embeds an interpreter and opens a redis -- and this is the
/// part worth being sure of.
async fn run(
    platform: &mut dyn PlatformApi,
    is_smartswitch: bool,
    sysfs: &Path,
    tables: &Tables<'_>,
    cycles: &mut Cycles,
) -> i32 {
    let mut read = Latch::new();
    let code = loop {
        match cycles.next(UPDATE_PERIOD).await {
            Tick::Exit(code) => break code,
            Tick::Cycle => {}
        }
        if let Err(e) = one_pass(platform, is_smartswitch, sysfs, tables, &mut read) {
            log::error!("{e}");
            break ERR_DB_READ;
        }
    };

    // Both tables are this daemon's; a stale PASSED after it has stopped
    // checking is the worst of the three things it could leave behind.
    clear(tables.device);
    clear(tables.status);
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
            log::error!("Failed to load any PCIe utility module. Exiting... {e}");
            std::process::exit(PCIEUTIL_LOAD_ERROR);
        }
    };

    // After the platform is opened, not before as Python's `DaemonBase`
    // orders it, and the order is load-bearing.  Opening the PyO3 bridge ends
    // with `signal.signal(SIGTERM/SIGINT, SIG_DFL)` -- a real `sigaction`,
    // undoing whatever a vendor chassis installed -- and tokio installs its
    // own `sigaction` once per signal, on first registration.  Registered
    // first, the handlers would be overwritten and never put back, and every
    // SIGTERM for the life of the process would kill it before the teardown
    // runs.  What the Python order buys -- a clean exit for a SIGTERM during
    // the import -- has nothing to clean up here: nothing has been published
    // yet.
    let code = start(
        &mut platform,
        &db::open,
        Path::new(PCI_DEVICES_DIR),
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

/// Whether to look in the detach table: whether this is a SmartSwitch.
///
/// Asked once, at start-up, and the answer kept, so a failed read cannot be
/// the answer: Python asks `device_info.is_smartswitch()` on every pass
/// (`pcied:DaemonPcied.check_pcie_devices`), which reads configuration and
/// cannot fail transiently, and a `false` latched from one bad chassis read
/// would have a SmartSwitch report every detaching DPU as missing -- FAILED
/// for a planned operation -- for the life of the process.  So a read that
/// fails is treated as "yes".  That is the cheap mistake: the detach table is
/// empty anywhere else, and the cost is one scan of it per pass.
///
/// `NotSupported` is not a failure.  A platform that does not answer is one
/// whose `ChassisBase.is_smartswitch` answers False, and says nothing about it.
fn is_smartswitch(platform: &mut dyn PlatformApi) -> bool {
    match platform.get_chassis_info(CHASSIS_COLS) {
        Ok(c) => c.is_smartswitch,
        Err(PlatformError::NotSupported(_)) => false,
        Err(e) => {
            log::warn!(
                "Failed to read whether this is a SmartSwitch, \
                 checking the detach table every pass: {e}"
            );
            true
        }
    }
}

/// Open the tables, decide what this platform needs, and run.
///
/// Separated from `main` for the same reason as everywhere else here: `main`
/// embeds an interpreter and opens a redis, and this is where the decisions
/// are.  The SmartSwitch question is one of them -- the detach table is empty
/// on everything else, and looking in it would be a scan per absent device.
async fn start(
    platform: &mut dyn PlatformApi,
    open: db::Opener<'_>,
    sysfs: &Path,
    cycles: &mut Cycles,
) -> i32 {
    let handles = match open_all(open) {
        Ok(h) => h,
        Err(e) => {
            log::error!("{e}");
            return PCIEUTIL_CONF_FILE_ERROR;
        }
    };
    let (device_table, status_table, detach_table) =
        (handles.device, handles.status, handles.detach);

    // Only a SmartSwitch has DPUs to detach; elsewhere the table is empty and
    // the lookup would be a scan per absent device for nothing.
    let is_smartswitch = is_smartswitch(platform);

    let tables = Tables {
        device: device_table.as_ref(),
        status: status_table.as_ref(),
        detach: detach_table.as_ref(),
    };

    run(platform, is_smartswitch, sysfs, &tables, cycles).await
}

#[cfg(test)]
mod tests {

    /// `CHASSIS_COLS` has to name every column pcied reads.
    ///
    /// A column left out arrives as its absent value with nothing to say so --
    /// `is_smartswitch` would silently read `false` and pcied would stop
    /// publishing DPU PCIe state.  Cheaper to assert than to find in the field.
    #[test]
    fn pcied_asks_for_every_chassis_column_it_reads() {
        use platform_api::ChassisInfoCol;
        assert!(CHASSIS_COLS.contains(ChassisInfoCol::IsSmartswitch));
        // And nothing more: the other nineteen reach the vendor for a reboot
        // cause, a serial, a slot number pcied has no use for.
        assert_eq!(CHASSIS_COLS.bits().count_ones(), 1);
    }

    use super::*;
    use platform_api::{ChassisInfoCols, PcieAerStat, PlatformError};
    use pmon_common::db::MockTable;

    fn device(name: &str, bus: i64, dev: i64, func: i64, present: bool) -> PcieDevice {
        PcieDevice { name: name.to_string(), bus, dev, r#fn: func, present }
    }

    struct FakePlatform {
        stats: Vec<PcieAerStat>,
        calls: usize,
        devices: Vec<PcieDevice>,
        fail: bool,
    }

    impl FakePlatform {
        fn new() -> Self {
            Self { stats: vec![], calls: 0, devices: vec![], fail: false }
        }
    }

    impl PlatformApi for FakePlatform {
        fn get_pcie_devices(&mut self) -> Result<Vec<PcieDevice>, PlatformError> {
            if self.fail {
                return Err(PlatformError::Backend("no pcie.yaml".into()));
            }
            Ok(self.devices.clone())
        }

        fn get_pcie_aer_stats(
            &mut self,
            _bus: i64,
            _dev: i64,
            _func: i64,
        ) -> Result<Vec<PcieAerStat>, PlatformError> {
            self.calls += 1;
            Ok(self.stats.clone())
        }
    }

    fn never(_: &str) -> bool {
        false
    }

    /// The key is the sysfs directory name without the domain, which is what
    /// `show platform pcieinfo` prints and what the id file path is built from.
    #[test]
    fn the_key_is_the_bdf_in_lower_case_hex() {
        assert_eq!(device_key(&device("x", 0x3, 0xa, 1, true)), "03:0a.1");
        assert_eq!(device_key(&device("x", 0, 0, 0, true)), "00:00.0");
        assert_eq!(bus_info(&device("x", 0x3, 0xa, 1, true)), "0000:03:0a.1");
    }

    /// A device the parts list expects and the bus does not have is the whole
    /// point of the daemon.
    #[test]
    fn a_missing_device_makes_the_check_fail() {
        let t = MockTable::new();
        let mut p = FakePlatform::new();
        let missing = check_devices(
            &mut p,
            &[device("RootPort", 0, 0, 0, false), device("NIC", 1, 0, 0, false)],
            &never,
            Path::new("/nonexistent"),
            &t,
        );
        assert_eq!(missing, 2);

        let status = MockTable::new();
        publish_status(missing, &status);
        assert_eq!(status.field("status", "status").as_deref(), Some("FAILED"));
    }

    #[test]
    fn a_complete_parts_list_passes() {
        let t = MockTable::new();
        publish_status(0, &t);
        assert_eq!(t.field("status", "status").as_deref(), Some("PASSED"));
    }

    /// A DPU on its way out is expected to disappear.  Counting it would put
    /// the switch into FAILED for as long as the detach takes, which is a
    /// planned operation.
    #[test]
    fn a_detaching_device_is_not_counted_as_missing() {
        let t = MockTable::new();
        let mut p = FakePlatform::new();
        let gone = device("DPU0", 0x3b, 0, 0, false);
        let missing = check_devices(&mut p, &[gone], &|bdf| bdf == "0000:3b:00.0", Path::new("/nonexistent"), &t);
        assert_eq!(missing, 0);
    }

    /// An absent device has no AER counters to read, and asking for them would
    /// be one Python round trip per missing device per minute.
    #[test]
    fn a_missing_device_is_not_asked_for_aer_stats() {
        let t = MockTable::new();
        let mut p = FakePlatform::new();
        check_devices(&mut p, &[device("RootPort", 0, 0, 0, false)], &never, Path::new("/nonexistent"), &t);
        assert_eq!(p.calls, 0);
    }

    /// The three severities collapse into one row, keyed `severity|field`,
    /// which is the shape `show platform pcieinfo -d` reads.
    #[test]
    fn aer_counters_are_published_as_severity_pipe_field() {
        let t = MockTable::new();
        let mut p = FakePlatform {
            stats: vec![
                PcieAerStat { severity: "correctable".into(), field: "BadTLP".into(), value: "0".into() },
                PcieAerStat { severity: "non_fatal".into(), field: "RxOF".into(), value: "2".into() },
            ],
            ..FakePlatform::new()
        };
        publish_aer(&mut p, &device("x", 3, 0, 0, true), "03:00.0", &t);
        assert_eq!(t.field("03:00.0", "correctable|BadTLP").as_deref(), Some("0"));
        assert_eq!(t.field("03:00.0", "non_fatal|RxOF").as_deref(), Some("2"));
    }

    /// Most devices expose no AER files.  Writing an empty row would create a
    /// PCIE_DEVICE key with nothing in it for every one of them.
    #[test]
    fn a_device_without_aer_files_gets_no_row_from_this_path() {
        let t = MockTable::new();
        let mut p = FakePlatform::new();
        publish_aer(&mut p, &device("x", 3, 0, 0, true), "03:00.0", &t);
        assert!(t.is_empty());
    }

    #[test]
    fn the_three_tables_are_opened_on_state_db() {
        let o = pmon_common::db::MockOpener::new();
        open_all(&|d, t| o.open(d, t)).expect("opens");
        assert_eq!(o.asked(), vec![
            ("STATE_DB".to_string(), "PCIE_DEVICE".to_string()),
            ("STATE_DB".to_string(), "PCIE_DEVICES".to_string()),
            ("STATE_DB".to_string(), "PCIE_DETACH_INFO".to_string()),
        ]);
    }

    /// And says which one: the three open in a row, and a log line that does
    /// not name the table leaves the operator guessing.
    #[test]
    fn a_table_that_will_not_open_stops_the_daemon() {
        let o = pmon_common::db::MockOpener::failing("PCIE_DEVICES");
        let e = open_all(&|d, t| o.open(d, t)).err().expect("cannot publish");
        assert!(e.starts_with("Failed to connect to STATE_DB or create table"), "{e}");
        assert!(e.contains("PCIE_DEVICES"), "{e}");
    }

    /// A sysfs the test owns, so the present-device path can be walked.
    fn sysfs(devices: &[(&str, &str)]) -> tempfile::TempDir {
        let d = tempfile::tempdir().unwrap();
        for (key, id) in devices {
            let dir = d.path().join(format!("0000:{key}"));
            std::fs::create_dir_all(&dir).unwrap();
            std::fs::write(dir.join("device"), format!("{id}\n")).unwrap();
        }
        d
    }

    #[test]
    fn a_devices_id_comes_out_of_the_kernels_own_file() {
        let fs = sysfs(&[("03:00.0", "0x1017")]);
        assert_eq!(read_id_in(fs.path(), "03:00.0").as_deref(), Some("0x1017"));
        assert_eq!(read_id_in(fs.path(), "04:00.0"), None, "a device with no such file");
    }

    /// A present device gets its id and its AER counters; the id read is what
    /// creates the row, so a device sysfs has forgotten about gets none.
    #[test]
    fn a_present_device_is_published_with_its_id() {
        let fs = sysfs(&[("03:00.0", "0x1017")]);
        let t = MockTable::new();
        let mut p = FakePlatform {
            stats: vec![PcieAerStat {
                severity: "correctable".into(), field: "BadTLP".into(), value: "0".into(),
            }],
            ..FakePlatform::new()
        };
        let missing = check_devices(
            &mut p,
            &[device("RootPort", 3, 0, 0, true), device("Absent", 9, 0, 0, true)],
            &never,
            fs.path(),
            &t,
        );
        assert_eq!(missing, 0, "both are reported present by the platform");
        assert_eq!(t.field("03:00.0", "id").as_deref(), Some("0x1017"));
        assert_eq!(t.field("03:00.0", "correctable|BadTLP").as_deref(), Some("0"));
        assert!(t.field("09:00.0", "id").is_none(), "no sysfs entry, no row");
    }

    struct Db {
        device: MockTable,
        status: MockTable,
        detach: MockTable,
    }

    impl Db {
        fn new() -> Self {
            Self { device: MockTable::new(), status: MockTable::new(), detach: MockTable::new() }
        }
        fn tables(&self) -> Tables<'_> {
            Tables { device: &self.device, status: &self.status, detach: &self.detach }
        }
    }

    /// The loop end to end.  The verdict is published every pass, and the
    /// teardown takes both tables away.
    #[tokio::test]
    async fn the_loop_publishes_a_verdict_and_clears_on_the_way_out() {
        tokio::time::pause();
        let db = Db::new();
        let mut p = FakePlatform {
            devices: vec![device("RootPort", 3, 0, 0, false)],
            ..FakePlatform::new()
        };
        let fs = sysfs(&[]);
        let mut cycles = Cycles::Fixed { remaining: 1, code: 143 };
        let code = run(&mut p, false, fs.path(), &db.tables(), &mut cycles).await;
        assert_eq!(code, 143);
        assert!(db.status.wrote("status", "status"), "the pass published a verdict");
        assert!(db.status.is_empty(), "a stale PASSED outlives the checking");
    }

    /// A platform with no `pcie.yaml` has nothing to check, and saying PASSED
    /// about an empty parts list would be a claim nobody made.
    #[tokio::test]
    async fn an_empty_parts_list_publishes_no_verdict() {
        tokio::time::pause();
        let db = Db::new();
        let mut p = FakePlatform::new();
        let fs = sysfs(&[]);
        let mut cycles = Cycles::Fixed { remaining: 3, code: 0 };
        run(&mut p, false, fs.path(), &db.tables(), &mut cycles).await;
        // The writes, not what is left: the teardown would empty the table
        // whether or not a verdict had been published.
        assert!(db.status.writes().is_empty());
    }

    /// Neither does a platform whose check failed outright.
    #[tokio::test]
    async fn a_failing_check_publishes_no_verdict() {
        tokio::time::pause();
        let db = Db::new();
        let mut p = FakePlatform { fail: true, ..FakePlatform::new() };
        let fs = sysfs(&[]);
        let mut cycles = Cycles::Fixed { remaining: 3, code: 0 };
        run(&mut p, false, fs.path(), &db.tables(), &mut cycles).await;
        assert!(db.status.writes().is_empty());
    }

    /// A SmartSwitch whose detach table cannot be read stops, with no verdict
    /// published: carrying on would report a DPU's device missing while it is
    /// being detached on purpose.  Python reads that table bare.
    #[tokio::test]
    async fn an_unreadable_detach_table_stops_the_daemon() {
        let log = pmon_common::logging::capture();
        tokio::time::pause();
        let db = Db::new();
        db.detach.fail_reads("connection reset");
        let mut p = FakePlatform {
            // Present, so a pass that got as far as checking would ask for
            // its AER counters.
            devices: vec![device("DPU0", 6, 0, 0, true)],
            ..FakePlatform::new()
        };
        let fs = sysfs(&[("06:00.0", "0xa2dc")]);
        let mut cycles = Cycles::Fixed { remaining: 5, code: 143 };
        let code = run(&mut p, true, fs.path(), &db.tables(), &mut cycles).await;
        assert_eq!(code, ERR_DB_READ);
        assert!(log.logged(log::Level::Error, "Failed to read the detach table"));
        assert!(db.status.writes().is_empty(), "no verdict on a check that did not finish");
        assert_eq!(p.calls, 0, "and no device was looked at");
    }

    #[test]
    fn teardown_leaves_nothing_behind() {
        let t = MockTable::new();
        t.set("03:00.0", &[("id", "0x1234".to_string())]).unwrap();
        t.set("status", &[("status", "PASSED".to_string())]).unwrap();
        clear(&t);
        assert!(t.is_empty(), "a stale PASSED outlives the checking that produced it");
    }

    // ── the wiring that used to be inside main ───────────────────────────────

    /// A platform that answers both of the questions `start` asks.
    #[derive(Default)]
    struct Shaped {
        smartswitch: bool,
        devices: Vec<PcieDevice>,
        /// What the chassis read fails with, if it does.
        chassis_error: Option<PlatformError>,
    }

    impl PlatformApi for Shaped {
        fn get_pcie_devices(&mut self) -> Result<Vec<PcieDevice>, PlatformError> {
            Ok(self.devices.clone())
        }
        fn get_chassis_info(&mut self, _cols: ChassisInfoCols) -> Result<platform_api::ChassisInfo, PlatformError> {
            if let Some(e) = self.chassis_error.clone() {
                return Err(e);
            }
            Ok(platform_api::ChassisInfo {
                is_smartswitch: self.smartswitch,
                ..Default::default()
            })
        }
    }

    /// The three tables, on the three databases they actually live on, and the
    /// status row published before the daemon leaves.  `PCIE_DEVICES|status` has
    /// a second writer -- `pcie-check.sh` -- so opening the wrong database here
    /// would be invisible: the other writer keeps the row looking right.
    ///
    /// The device's id comes out of a sysfs the test owns, which is also what
    /// keeps this test off the host's own `/sys/bus/pci/devices`.
    #[tokio::test]
    async fn the_tables_are_opened_and_the_status_is_published() {
        let o = pmon_common::db::MockOpener::new();
        let fs = sysfs(&[("05:00.0", "0xcf6c")]);
        let mut p = Shaped { devices: vec![device("ASIC", 5, 0, 0, true)], ..Default::default() };
        let code = start(
            &mut p,
            &|d, t| o.open(d, t),
            fs.path(),
            &mut Cycles::Fixed { remaining: 1, code: 0 },
        )
        .await;
        assert_eq!(code, 0);
        assert!(o.table("PCIE_DEVICES").unwrap().writes().iter().any(|(_, f)| f == "status"));
        assert!(o.table("PCIE_DEVICE").unwrap().wrote("05:00.0", "id"),
            "the id was read from the sysfs handed in");
    }

    /// A table that will not open gets `PCIEUTIL_CONF_FILE_ERROR`, because that
    /// is the code Python leaves with for this very failure
    /// (`pcied:DaemonPcied.__init__` exits with it when the STATE_DB connect
    /// or a table fails), whatever the name suggests -- supervisord reads the
    /// code.  The message names the table Python's could not.
    #[tokio::test]
    async fn a_table_that_will_not_open_is_a_conf_file_error() {
        let log = pmon_common::logging::capture();
        let o = pmon_common::db::MockOpener::failing("PCIE_DEVICE");
        let fs = sysfs(&[]);
        let code = start(
            &mut Shaped::default(),
            &|d, t| o.open(d, t),
            fs.path(),
            &mut Cycles::Fixed { remaining: 0, code: 0 },
        )
        .await;
        assert_eq!(code, PCIEUTIL_CONF_FILE_ERROR);
        assert!(log.logged(log::Level::Error, "Failed to connect to STATE_DB or create table PCIE_DEVICE."));
    }

    /// Only a SmartSwitch looks in the detach table.  Everywhere else it is
    /// empty, and the lookup would be a scan per absent device every cycle.
    #[tokio::test]
    async fn only_a_smartswitch_consults_the_detach_table() {
        for smartswitch in [false, true] {
            let o = pmon_common::db::MockOpener::new();
            let fs = sysfs(&[]);
            let mut p = Shaped {
                smartswitch,
                devices: vec![device("DPU0", 6, 0, 0, false)],
                ..Default::default()
            };
            start(&mut p, &|d, t| o.open(d, t), fs.path(), &mut Cycles::Fixed { remaining: 1, code: 0 })
                .await;
            assert_eq!(
                o.table("PCIE_DETACH_INFO").unwrap().scans() > 0,
                smartswitch,
                "smartswitch={smartswitch}"
            );
        }
    }

    /// A chassis read that fails at start-up is taken as "this is a
    /// SmartSwitch", and said so.  Taken as "no", a SmartSwitch would report
    /// every DPU it detaches as a missing device, FAILED, until the daemon next
    /// restarted.  Taken as "yes" on a switch without DPUs, it costs one scan
    /// of an empty table per pass.
    #[tokio::test]
    async fn a_failed_smartswitch_read_still_consults_the_detach_table() {
        let log = pmon_common::logging::capture();
        let o = pmon_common::db::MockOpener::new();
        let fs = sysfs(&[]);
        let mut p = Shaped {
            devices: vec![device("DPU0", 6, 0, 0, false)],
            chassis_error: Some(PlatformError::Backend("i2c timeout".into())),
            ..Default::default()
        };
        start(&mut p, &|d, t| o.open(d, t), fs.path(), &mut Cycles::Fixed { remaining: 1, code: 0 })
            .await;
        assert!(o.table("PCIE_DETACH_INFO").unwrap().scans() > 0);
        assert!(log.logged(log::Level::Warn, "Failed to read whether this is a SmartSwitch"));
    }

    /// And a detaching DPU on such a switch is not counted as missing, which
    /// is the point of failing that way.
    #[test]
    fn a_failed_smartswitch_read_still_spares_a_detaching_dpu() {
        let mut chassis = Shaped {
            chassis_error: Some(PlatformError::Backend("i2c timeout".into())),
            ..Default::default()
        };
        assert!(is_smartswitch(&mut chassis));

        let db = Db::new();
        db.detach.set("DPU0", &[
            ("bus_info", "0000:06:00.0".to_string()),
            ("dpu_state", "detaching".to_string()),
        ]).unwrap();
        let mut p = FakePlatform { devices: vec![device("DPU0", 6, 0, 0, false)], ..FakePlatform::new() };
        let fs = sysfs(&[]);
        one_pass(&mut p, is_smartswitch(&mut chassis), fs.path(), &db.tables(), &mut Latch::new())
            .unwrap();
        assert_eq!(db.status.field("status", "status").as_deref(), Some("PASSED"));
    }

    /// A platform that does not answer is not a SmartSwitch, and nothing is
    /// said about it: `ChassisBase.is_smartswitch` answers False by default,
    /// and a warning every start on every platform without the method would
    /// be noise.
    #[tokio::test]
    async fn a_platform_that_does_not_say_is_not_a_smartswitch_and_is_not_warned_about() {
        let log = pmon_common::logging::capture();
        let o = pmon_common::db::MockOpener::new();
        let fs = sysfs(&[]);
        let mut p = Shaped {
            devices: vec![device("DPU0", 6, 0, 0, false)],
            chassis_error: Some(PlatformError::NotSupported("is_smartswitch".into())),
            ..Default::default()
        };
        start(&mut p, &|d, t| o.open(d, t), fs.path(), &mut Cycles::Fixed { remaining: 1, code: 0 })
            .await;
        assert_eq!(o.table("PCIE_DETACH_INFO").unwrap().scans(), 0);
        assert!(!log.contains("SmartSwitch"));
    }
}
