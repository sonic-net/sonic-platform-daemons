//! chassisd, in Rust.  Ports `sonic-chassisd/scripts/chassisd`.
//!
//! Two machines share this daemon and very little else.  On a **modular
//! chassis** it publishes what cards are in which slot, which ASICs they carry
//! and whether the midplane reaches them, and cleans up after a card that has
//! been out for half an hour.  On a **SmartSwitch** it runs the DPUs: their
//! admin state, their reboot causes, and the recovery state machine that power
//! cycles one that will not come back.
//!
//! Which machine it is, is the chassis' own answer (`is_smartswitch`), and the
//! two paths share only the CONFIG_DB table they watch -- which they read
//! oppositely; see [`config_updater::admin_state_for`].

mod chassis_app_db;
mod config_updater;
mod dpu_reboot_cause;
mod dpu_recovery;
mod dpu_state;
mod dpu_updater;
mod module_updater;
mod names;
#[cfg(all(test, feature = "parity"))]
mod parity;

use std::collections::BTreeMap;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime};

use clap::Parser;
use platform_api::{
    ChassisInfoCols, DeviceKey, ModuleInfo, ModuleInfoCols, ModuleStatus, PlatformApi,
    PlatformError,
};
use platform_provider::PlatformImpl;
use pmon_common::cycles::{Cycles, Tick};
use pmon_common::db::{self, TableLike};
use pmon_common::report_once::Latch;
use pmon_common::{logging, platform_env};

use config_updater::Machine;
use module_updater::{ModuleUpdater, Tables};
use names::*;

const SYSLOG_IDENTIFIER: &str = "chassisd";

/// The command line, which is one switch.
///
/// Which vendor package gets imported is not here and never was: the image
/// installs one `sonic_platform` and which one is its business.  What is here
/// is which *implementation* of the platform API to open, because a platform
/// with a native Rust one has to be able to say so without a new binary.
#[derive(Parser, Debug)]
#[command(name = "chassisd-rs", about = "SONiC chassis daemon, in Rust")]
struct Args {
    /// Which platform API implementation to use: `pyo3` or `native`.
    ///
    /// Filled in from `platform_api_chassisd` in `pmon_daemon_control.json` by the
    /// supervisord template.  Absent means `pyo3`, which is what every platform
    /// runs today -- an unset switch has to leave a platform on the
    /// implementation it has always had.
    #[arg(long, default_value_t = PlatformImpl::Pyo3)]
    platform_api: PlatformImpl,
}

/// `chassisd:CHASSIS_INFO_UPDATE_PERIOD_SECS`.
const UPDATE_PERIOD: Duration = Duration::from_secs(10);

/// `chassisd:CHASSIS_LOAD_ERROR`: the platform, or a table this daemon cannot
/// run without, would not load.
const CHASSIS_LOAD_ERROR: i32 = 1;

/// `chassisd:CHASSIS_NOT_SUPPORTED`: a fixed switch, which answers neither
/// slot and has nothing for this daemon to do.
const CHASSIS_NOT_SUPPORTED: i32 = 2;

/// What the daemon leaves with when STATE_DB went away.
///
/// Python has no constant for it: `module_db_update` and
/// `check_midplane_reachability` leave their `set`/`_del` calls unwrapped
/// (`chassisd:ModuleUpdater.module_db_update`,
/// `chassisd:ModuleUpdater.check_midplane_reachability`,
/// `chassisd:SmartSwitchModuleUpdater.module_db_update`,
/// `chassisd:SmartSwitchModuleUpdater.check_midplane_reachability`), the main
/// loop is inside a `try`/`finally` that does not catch
/// (`chassisd:ChassisdDaemon.run`), and the interpreter exits 1.  Non-zero is
/// what matters -- supervisord's `autorestart=unexpected` reads the code, and a
/// restart is the only way back to a working connection because `DBConnector`
/// never reconnects.
const ERR_DB_WRITE: i32 = 1;

/// `chassisd:DEFAULT_LINECARD_REBOOT_TIMEOUT`, overridable from
/// `platform_env.conf`.
const DEFAULT_LINECARD_REBOOT_TIMEOUT: Duration = Duration::from_secs(180);
const LINECARD_REBOOT_TIMEOUT_KEY: &str = "linecard_reboot_timeout";

fn linecard_reboot_timeout() -> Duration {
    reboot_timeout_from(platform_env::value(LINECARD_REBOOT_TIMEOUT_KEY).as_deref())
}

/// The same, from whatever the conf file held.
///
/// Anything that is not a whole number of seconds keeps the default.  The
/// timeout is how long an expected line-card reboot may keep the midplane down
/// before "not restored in N seconds" is worth a line; a switch that read
/// `180s` as zero would print that for every reboot, the moment it began.  (The
/// chassis app DB cleanup is not on this clock -- it waits its own half hour.)
fn reboot_timeout_from(value: Option<&str>) -> Duration {
    value
        .and_then(|v| v.trim().parse::<u64>().ok())
        .map_or(DEFAULT_LINECARD_REBOOT_TIMEOUT, Duration::from_secs)
}

/// One pass of the modular-chassis loop.
fn chassis_pass(
    platform: &mut dyn PlatformApi,
    updater: &mut ModuleUpdater,
    tables: &Tables<'_>,
    is_supervisor: bool,
    read: &mut Latch,
    now: SystemTime,
) -> Result<(), String> {
    // The modules and their ASICs are one reading: a pass publishes both or
    // neither.
    //
    // A failed read is not treated as "every card was pulled": that would
    // blank CHASSIS_MODULE_TABLE and drop every ASIC row on a transient
    // failure, which on a chassis is a forwarding outage.  Nor is a failed
    // ASIC read "no ASICs": that would publish `num_asics` 0 for every card,
    // and a line card's 0 in its hostname row is the count the supervisor's
    // cleanup reads, so a card pulled later would be cleaned up after with
    // nothing.  An ASIC getter the platform does not implement *is* "none", as
    // `chassisd:try_get` reads it.
    //
    // And not an error either, which is a departure from Python:
    // `chassisd:try_get` turns only `NotImplementedError` into a default, so
    // any other failure reading the modules or the ASICs ends the Python
    // daemon and supervisord restarts it.  Skipping the cycle keeps the rows
    // that are there.
    let reading = platform
        .get_modules(ModuleInfoCols::ALL)
        .map_err(|e| format!("Failed to read the modules: {e}"))
        .and_then(|modules| match platform.get_asics() {
            Ok(asics) => Ok((modules, asics)),
            Err(PlatformError::NotSupported(_)) => Ok((modules, Vec::new())),
            Err(e) => Err(format!("Failed to read the ASICs: {e}")),
        });
    let (modules, asics) = match reading {
        Ok(r) => {
            pmon_common::recovered!(read, "module read recovered");
            r
        }
        Err(e) => {
            pmon_common::fail_once!(read, "{e}");
            return Ok(());
        }
    };

    updater.refresh(&modules, &asics, tables)?;
    updater.check_midplane(&modules, tables)?;

    for key in chassis_app_db::due(is_supervisor, updater.down_modules().iter(), now) {
        let slot = updater.down_modules().get(&key).map(|m| m.slot).unwrap_or(INVALID_SLOT);
        if chassis_app_db::cleanup(&key, slot, tables) {
            updater.mark_cleaned(&key);
        }
    }
    Ok(())
}

/// The modular-chassis loop, with everything it needs handed to it.
///
/// Separated from `main` so it can be driven by a test: `main` is the part that
/// cannot be -- it embeds an interpreter and opens a redis -- and this is the
/// part worth being sure of.
async fn run_chassis(
    platform: &mut dyn PlatformApi,
    updater: &mut ModuleUpdater,
    tables: &Tables<'_>,
    is_supervisor: bool,
    cycles: &mut Cycles,
    watcher_lost: &AtomicBool,
) -> i32 {
    let mut read = Latch::new();
    log::info!("Start daemon main loop");
    loop {
        match cycles.next(UPDATE_PERIOD, &mut *platform).await {
            Tick::Exit(code) => return code,
            Tick::Cycle => {}
        }
        // A watcher that has stopped for good -- its subscription lost, or its
        // platform would not load -- has said why already.  It shares this
        // redis, so a lost subscription means the writes below are about to
        // fail too.  Checked here rather than acted on over there because
        // leaving is the poll loop's to do: the rows still have to be cleared
        // on the way out.
        if watcher_lost.load(Ordering::SeqCst) {
            log::error!("A watcher has stopped; stopping the daemon");
            return ERR_DB_WRITE;
        }
        // A STATE_DB that will not take a write is a redis this process has
        // lost for good, so it leaves and supervisord brings it back onto a
        // fresh one -- what Python's unwrapped `set` does by letting the
        // exception through `try`/`finally` and out of `main`.  One
        // difference: Python's exception skips the `deinit` that would clear
        // the tables, and here the caller clears them on the way out either
        // way.
        if let Err(e) = chassis_pass(
            platform, updater, tables, is_supervisor, &mut read, SystemTime::now())
        {
            log::error!("{e}");
            return ERR_DB_WRITE;
        }
    }
}

/// Which of the daemon's subscriptions to start.
#[derive(Clone, Copy, Debug, PartialEq)]
enum Watch {
    /// CONFIG_DB CHASSIS_MODULE, applied to the hardware.
    Config(Machine),
    /// The boot_ids the DPUs publish into CHASSIS_STATE_DB DPU_STATE, on a
    /// SmartSwitch's NPU, each new one a reboot to record.
    BootIds,
}

/// What the watchers and the poll loop say to each other.
#[derive(Debug, Default)]
struct WatchFlags {
    /// Raised by a watcher that has stopped for good -- its subscription lost,
    /// or its platform would not load -- for the poll loop to act on.
    lost: AtomicBool,
    /// Raised by the poll loop on the way out.  A watcher checks it between
    /// selects, which time out every second, and returns.
    stop: AtomicBool,
}

/// How long the way out waits for the watchers to see `stop` -- all of them,
/// one wait between them.
///
/// Long enough for a select to time out and a change already being applied to
/// finish; not so long that one stuck in a slow hardware call holds up a
/// shutdown supervisord is timing.  A watcher still running after it is left
/// to the process exit.
const WATCHER_STOP_WAIT: Duration = Duration::from_secs(5);

/// Tell the watchers to stop, and wait for them -- for a while.
///
/// Told through a flag because `abort` does nothing to a `spawn_blocking` task
/// once it is running, as tokio documents.  Waited for so that no watcher is
/// still applying a line card's CONFIG_DB change, or recording a reboot, while
/// the tables are cleared and the platform is finalized -- the order Python
/// keeps by stopping its tasks before `deinit` (`chassisd:ChassisdDaemon.run`).
/// A DPU's change is not covered: the watcher hands it to a thread of its own
/// that nobody joins, in Python as here, and that thread can outlive this.
///
/// One deadline for all of them rather than a wait each, so two stuck
/// watchers cost the shutdown what one does.
async fn stop_watchers(flags: &WatchFlags, handles: Vec<tokio::task::JoinHandle<()>>) {
    flags.stop.store(true, Ordering::SeqCst);
    let deadline = tokio::time::Instant::now() + WATCHER_STOP_WAIT;
    for h in handles {
        if tokio::time::timeout_at(deadline, h).await.is_err() {
            log::warn!(
                "A watcher did not stop within {}s; leaving it to the exit",
                WATCHER_STOP_WAIT.as_secs()
            );
        }
    }
}

/// How the daemon starts a subscription.
///
/// Injected rather than called directly because the real ones build a second
/// interpreter and subscribe to a redis, and under `cargo test` there is
/// neither -- while everything they are wired into is worth driving.
type Watcher<'a> = &'a dyn Fn(Watch, Arc<WatchFlags>) -> tokio::task::JoinHandle<()>;
/// Shut this batch of DPUs, each named and at its key, down and wait for all
/// of them.  Injected for the same reason as [`Watcher`], and so a test can
/// record the batch instead.
type Shutter<'a> = &'a dyn Fn(&[(String, DeviceKey)]);

/// Start one DPU's graceful admin-state change and return without waiting for
/// it.  Injected for the same reason as [`Shutter`]: the real one hands the
/// call to a thread that loads its own vendor module, which a test neither can
/// nor wants to do.
type Gracefully<'a> = &'a dyn Fn(&str, &DeviceKey, bool);

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let args = Args::parse();
    logging::init(SYSLOG_IDENTIFIER);
    log::info!("Starting up...");

    let mut platform = match platform_provider::open(SYSLOG_IDENTIFIER, args.platform_api) {
        Ok(p) => p,
        Err(e) => {
            log::error!("Failed to load chassis due to {e}");
            std::process::exit(CHASSIS_LOAD_ERROR);
        }
    };

    // The watcher opens a platform of its own on another thread, so it needs
    // to know which implementation to open.  Captured here rather than added
    // to `Watcher`: threading it through `dispatch` and the three run loops
    // would put a platform-selection argument into five signatures that have
    // nothing else to do with it.
    let spawn_watcher = move |watch, flags| match watch {
        Watch::Config(machine) => spawn_config_watcher(machine, args.platform_api, flags),
        Watch::BootIds => spawn_boot_id_watcher(args.platform_api, flags),
    };
    // And the start-up sweep opens one per DPU, on threads of its own, for the
    // same reason and by the same route.
    let shut_down =
        move |dpus: &[(String, DeviceKey)]| shut_down_in_parallel(dpus, args.platform_api);

    // After the platform is opened, not before as Python's `DaemonBase`
    // orders it, and the order is load-bearing.  Opening the PyO3 bridge ends
    // with `signal.signal(SIGTERM/SIGINT, SIG_DFL)` -- a real `sigaction`,
    // undoing whatever a vendor chassis installed -- and tokio installs its
    // own `sigaction` once per signal, on first registration.  Registered
    // first, the handlers would be overwritten and never put back, and every
    // SIGTERM for the life of the process would kill it before the teardown
    // runs -- the DPUs left marked ready, the module rows left behind.  What
    // the Python order buys -- a SIGTERM during the import handled rather than
    // fatal -- costs nothing here: nothing has been published yet.  The
    // watcher threads open bridges of their own later; the reset is a no-op
    // off the main thread, so they leave these handlers alone.
    let code = dispatch(
        &mut platform,
        &db::open,
        &spawn_watcher,
        &shut_down,
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

/// Which of the three shapes this daemon takes, and the code it leaves with.
///
/// Everything above this is what a test cannot drive -- an embedded
/// interpreter, a redis, `process::exit` -- and everything below it is a
/// decision.  Which shape to take is the decision that once went wrong on
/// hardware, so it is made here rather than in `main`.
async fn dispatch<P: PlatformApi>(
    platform: &mut P,
    open: db::Opener<'_>,
    spawn_watcher: Watcher<'_>,
    shut_down: Shutter<'_>,
    cycles: &mut Cycles,
) -> i32 {
    // Not `.ok()`.  Discarding the error here makes every failure look like
    // "this is a fixed switch": `None` answers false to `is_smartswitch`, both
    // slots come back INVALID, and the daemon exits CHASSIS_NOT_SUPPORTED --
    // a message that says the platform is wrong when the truth is that the
    // platform API raised.  On a SmartSwitch with four live DPUs that is
    // exactly what happened: `get_dpu_id()` raised TypeError, chassisd
    // exited 2 within a second with nothing in the log, supervisord marked it
    // FATAL, and nothing alarmed -- pmon's critical_processes is empty, so
    // system-health checks none of these daemons and the exit listener does
    // not subscribe to PROCESS_STATE_FATAL.  A switch would have shipped with
    // no chassisd and no indication of it.
    let shape = match shape(platform) {
        Ok(s) => s,
        Err(code) => return code,
    };
    match shape {
        // A DPU runs its own copy of this daemon, and it does something else
        // entirely: it reports its own two planes to the NPU rather than
        // managing anything.
        Shape::Dpu(dpu) => run_on_dpu(platform, &dpu, open, cycles).await,
        Shape::SmartSwitch => {
            // The three things the SmartSwitch path reads from outside this
            // process, read here so the loop below takes them as arguments and
            // can be driven without a switch under it.  The key space is
            // connected to as Python connects to it, in
            // `chassisd:SmartSwitchModuleUpdater.__init__` without a `try`.
            let sweep = match db::Db::connect(CHASSIS_STATE_DB) {
                Ok(d) => d,
                Err(e) => {
                    log::error!("Failed to connect to {CHASSIS_STATE_DB}: {e}");
                    return CHASSIS_LOAD_ERROR;
                }
            };
            let npu_reboot_cause =
                dpu_recovery::read_npu_reboot_cause(Path::new(dpu_recovery::REBOOT_CAUSE_FILE));
            run_smartswitch(
                platform,
                open,
                Some(&sweep),
                npu_reboot_cause.as_deref(),
                thresholds(),
                spawn_watcher,
                shut_down,
                cycles,
            )
            .await
        }
        Shape::Modular(slots) => run_modular(platform, &slots, open, spawn_watcher, cycles).await,
    }
}

/// Which of the three shapes this is, with what the shape goes on to need.
enum Shape {
    /// A DPU of a SmartSwitch; the row holds which one.
    Dpu(platform_api::ChassisInfo),
    /// The NPU of a SmartSwitch.
    SmartSwitch,
    /// A card of a modular chassis; the row holds the two slots.  A fixed
    /// switch lands here too, answers neither slot, and is turned away.
    Modular(platform_api::ChassisInfo),
}

/// Tell the shape, asking the getters Python asks, in its order, and only on
/// the path it asks them on: `is_dpu()` on a SmartSwitch only, as
/// `chassisd:main`'s `and` asks it, and the slots off one only
/// (`chassisd:ChassisdDaemon.run`).
///
/// Column by column rather than every column at once: a row of every column
/// runs every chassis getter, and one this daemon has no use for -- a LED, a
/// reboot cause -- raising would end it.
fn shape(platform: &mut dyn PlatformApi) -> Result<Shape, i32> {
    let mut read = |cols: ChassisInfoCols| {
        platform.get_chassis_info(cols).map_err(|e| {
            log::error!("Failed to read chassis info: {e}");
            CHASSIS_LOAD_ERROR
        })
    };
    let smartswitch = read(ChassisInfoCols::IS_SMARTSWITCH)?.is_smartswitch;
    log::info!("smartswitch: {smartswitch}");
    if !smartswitch {
        return Ok(Shape::Modular(read(SLOT_COLS)?));
    }
    if read(ChassisInfoCols::IS_DPU)?.is_dpu {
        return Ok(Shape::Dpu(read(ChassisInfoCols::DPU_ID)?));
    }
    Ok(Shape::SmartSwitch)
}

/// Which card of a modular chassis this is.
const SLOT_COLS: ChassisInfoCols = ChassisInfoCols::MY_SLOT.with(ChassisInfoCols::SUPERVISOR_SLOT);

/// The modular-chassis shape: this card is a supervisor or a line card.
async fn run_modular<P: PlatformApi>(
    platform: &mut P,
    chassis: &platform_api::ChassisInfo,
    open: db::Opener<'_>,
    spawn_watcher: Watcher<'_>,
    cycles: &mut Cycles,
) -> i32 {
    let my_slot = chassis.my_slot.unwrap_or(INVALID_SLOT);
    let supervisor_slot = chassis.supervisor_slot.unwrap_or(INVALID_SLOT);

    // A fixed switch answers neither slot, and there is nothing for this daemon
    // to do on one.  Python leaves with a code of its own for it
    // (`chassisd:ChassisdDaemon.run`); supervisord reads any non-zero code the
    // same way, so the code says what happened to whoever reads the exit
    // status, not to supervisord.
    if my_slot == INVALID_SLOT || supervisor_slot == INVALID_SLOT {
        log::error!("Chassisd not supported for this platform");
        return CHASSIS_NOT_SUPPORTED;
    }

    let is_supervisor = my_slot == supervisor_slot;
    let h = match open_chassis_tables(open, is_supervisor) {
        Ok(h) => h,
        Err(e) => {
            log::error!("{e}");
            return CHASSIS_LOAD_ERROR;
        }
    };
    let (chassis_tbl, module_tbl, midplane_tbl, entity_tbl, asic_tbl, hostname_tbl, reboot_tbl) = (
        h.chassis, h.module, h.midplane, h.entity, h.asic, h.hostname, h.module_reboot,
    );

    let tables = Tables {
        chassis: chassis_tbl.as_ref(),
        module: module_tbl.as_ref(),
        midplane: midplane_tbl.as_ref(),
        entity: entity_tbl.as_ref(),
        asic: asic_tbl.as_ref(),
        hostname: hostname_tbl.as_ref(),
        module_reboot: reboot_tbl.as_ref(),
        config: Some(h.config.as_ref()),
        device_metadata: Some(h.device_metadata.as_ref()),
    };

    // The midplane has to be up before anything is asked about reachability;
    // failing it is reported and not fatal, because the module table is still
    // worth publishing on a chassis whose midplane did not come up.  A False
    // is a failure too: `try_get(init_midplane_switch, default=False)`.
    let midplane_initialized = answered(platform.init_midplane_switch(), false).unwrap_or(false);
    if !midplane_initialized {
        log::error!("Chassisd midplane intialization failed");
    }

    let mut updater = ModuleUpdater::new(
        my_slot,
        supervisor_slot,
        linecard_reboot_timeout(),
        midplane_initialized,
    );

    let modules = match start_up_modules(platform) {
        Ok(m) => m,
        Err(code) => return code,
    };
    if let Err(e) = module_updater::publish_module_count(&modules, chassis_tbl.as_ref()) {
        log::error!("{e}");
        return ERR_DB_WRITE;
    }

    // The CONFIG_DB watcher runs on the supervisor only: a line card does not
    // administer its peers, and two writers would fight over the hardware.
    let flags = Arc::new(WatchFlags::default());
    let watchers: Vec<_> = is_supervisor
        .then(|| spawn_watcher(Watch::Config(Machine::Chassis), Arc::clone(&flags)))
        .into_iter()
        .collect();

    let code =
        run_chassis(platform, &mut updater, &tables, is_supervisor, cycles, &flags.lost).await;

    stop_watchers(&flags, watchers).await;
    updater.clear(&modules_for_teardown(platform, modules), &tables);
    code
}

/// The modules, read once before the loop starts.
///
/// A read that fails ends the daemon, as Python's unwrapped
/// `get_num_modules()` in its updaters' constructors does.  Read as "no
/// modules" -- which is what this used to do -- the daemon ran on with nothing
/// to publish and, on a SmartSwitch, a recovery machine seeded with no DPUs at
/// all, power cycling nothing for the life of the process; and the log said
/// "has no modules", which was not true.
fn start_up_modules(platform: &mut dyn PlatformApi) -> Result<Vec<ModuleInfo>, i32> {
    platform.get_modules(ModuleInfoCols::ALL).map_err(|e| {
        log::error!("Failed to read the modules: {e}");
        CHASSIS_LOAD_ERROR
    })
}

/// The modules to clear on the way out: read again, so a card that came or
/// went while the daemon ran is the one cleared; the start-up list when that
/// read fails, which is still every row this daemon wrote then.
fn modules_for_teardown(
    platform: &mut dyn PlatformApi,
    at_start: Vec<ModuleInfo>,
) -> Vec<ModuleInfo> {
    match platform.get_modules(ModuleInfoCols::ALL) {
        Ok(m) => m,
        Err(e) => {
            log::warn!(
                "Failed to read the modules on the way out, clearing the start-up list: {e}"
            );
            at_start
        }
    }
}

/// Every handle the modular-chassis path reads and writes through.
struct ChassisHandles {
    chassis: Box<dyn TableLike>,
    module: Box<dyn TableLike>,
    midplane: Box<dyn TableLike>,
    entity: Box<dyn TableLike>,
    asic: Box<dyn TableLike>,
    hostname: Box<dyn TableLike>,
    module_reboot: Box<dyn TableLike>,
    config: Box<dyn TableLike>,
    device_metadata: Box<dyn TableLike>,
}

/// A CONFIG_DB table this daemon cannot run without.
///
/// Required, not optional: Python connects to CONFIG_DB without a `try`
/// (`chassisd:ModuleUpdater.__init__`) and reads the admin status out of it
/// bare, so a CONFIG_DB it cannot reach ends that daemon.  The hostname is the
/// one read that is not fatal; see `ModuleUpdater::hostname`.  Opened as
/// optional, its absence read as "nothing configured" for the life of the
/// process -- every card admin up on a chassis, every DPU admin down on a
/// SmartSwitch, with nothing in the log to say why.
fn config_table(open: db::Opener<'_>, name: &str) -> Result<Box<dyn TableLike>, String> {
    open(CONFIG_DB, name).map_err(|e| format!("Failed to connect to {CONFIG_DB}: {e}"))
}

/// Open the modular-chassis tables, naming the one that refused.
///
/// Which database each lives on is the part worth asserting: the module table
/// is STATE_DB's and the hostname table is CHASSIS_STATE_DB's, and they have
/// the same name.  Opening one on the other's database would have a supervisor
/// cleaning up against its own rows.
fn open_chassis_tables(
    open: db::Opener<'_>,
    is_supervisor: bool,
) -> Result<ChassisHandles, String> {
    let state = |name: &str| {
        open(db::STATE_DB, name).map_err(|e| format!("Failed to connect to STATE_DB: {e}"))
    };
    let chassis_state = |name: &str| {
        open(CHASSIS_STATE_DB, name)
            .map_err(|e| format!("Failed to connect to {CHASSIS_STATE_DB}: {e}"))
    };
    Ok(ChassisHandles {
        chassis: state(CHASSIS_INFO_TABLE)?,
        module: state(CHASSIS_MODULE_INFO_TABLE)?,
        midplane: state(CHASSIS_MIDPLANE_INFO_TABLE)?,
        entity: state(PHYSICAL_ENTITY_INFO_TABLE)?,
        // The supervisor's holds the fabric cards' ASICs; a line card's holds
        // its own.  Which one cannot change while the daemon is running.
        asic: chassis_state(if is_supervisor {
            CHASSIS_FABRIC_ASIC_INFO_TABLE
        } else {
            CHASSIS_ASIC_INFO_TABLE
        })?,
        hostname: chassis_state(CHASSIS_MODULE_HOSTNAME_TABLE)?,
        module_reboot: chassis_state(CHASSIS_MODULE_REBOOT_INFO_TABLE)?,
        config: config_table(open, CHASSIS_CFG_TABLE)?,
        device_metadata: config_table(open, DEVICE_METADATA_TABLE)?,
    })
}

/// Every handle the SmartSwitch path reads and writes through.
struct DpuHandles {
    chassis: Box<dyn TableLike>,
    module: Box<dyn TableLike>,
    midplane: Box<dyn TableLike>,
    dpu_state: Box<dyn TableLike>,
    config: Box<dyn TableLike>,
    device_metadata: Box<dyn TableLike>,
}

/// Open the SmartSwitch tables, naming the one that refused.
fn open_dpu_tables(open: db::Opener<'_>) -> Result<DpuHandles, String> {
    let state = |name: &str| {
        open(db::STATE_DB, name).map_err(|e| format!("Failed to connect to STATE_DB: {e}"))
    };
    let chassis_state = |name: &str| {
        open(CHASSIS_STATE_DB, name)
            .map_err(|e| format!("Failed to connect to {CHASSIS_STATE_DB}: {e}"))
    };
    Ok(DpuHandles {
        chassis: state(CHASSIS_INFO_TABLE)?,
        module: state(CHASSIS_MODULE_INFO_TABLE)?,
        midplane: state(CHASSIS_MIDPLANE_INFO_TABLE)?,
        dpu_state: chassis_state(dpu_updater::DPU_STATE_TABLE)?,
        config: config_table(open, CHASSIS_CFG_TABLE)?,
        device_metadata: config_table(open, DEVICE_METADATA_TABLE)?,
    })
}

/// Apply one CONFIG_DB event to the hardware.
///
/// A DPU is brought down *gracefully* -- it is running an OS that wants to be
/// told -- where a line card is not.  Using the abrupt call on a DPU would cut
/// the power under a running SONiC.
///
/// The two machines also differ in *who waits*.  A line card is changed inline
/// (`chassisd:ModuleConfigUpdater.module_config_update`).  A DPU is handed to
/// `gracefully`, which starts the call and returns, because that is what Python
/// does: its SmartSwitch updater starts a thread per event and never joins it
/// (`chassisd:SmartSwitchModuleConfigUpdater.module_config_update`), so four
/// DPUs change state at once.  Doing it inline here instead serialised them --
/// measured on a SmartSwitch, four DPUs down took 12m44s and back up 7m27s against
/// sonic-mgmt's 375s and 200s budgets, which are sized for the concurrent
/// behaviour, and the deploy failed every time.
///
/// The CONFIG_DB key is a module's name, and it is resolved as Python resolves
/// it, with `get_module_index`; the change is then made at the key that
/// answers.
fn apply_config_event(
    platform: &mut dyn PlatformApi,
    machine: Machine,
    name: &str,
    event: config_updater::Event<'_>,
    gracefully: Gracefully<'_>,
) {
    let up = config_updater::admin_state_for(machine, event);
    let key = match platform.get_module_key(name) {
        Ok(Some(key)) => key,
        // `try_get(get_module_index, default=INVALID_MODULE_INDEX)`: not
        // implemented, or no such module.  Python prints the admin state as
        // its `MODULE_ADMIN_DOWN`/`MODULE_ADMIN_UP`, 0 or 1.
        Ok(None) | Err(PlatformError::NotSupported(_)) => {
            log::error!(
                "Unable to get module-index for key {name} to set admin-state {}",
                u8::from(up)
            );
            return;
        }
        // An exception from `get_module_index` ends Python's config updater;
        // here it ends only this one event.
        Err(e) => {
            log::error!("Failed to look up module {name} to set admin-state {}: {e}", u8::from(up));
            return;
        }
    };
    log::info!("Changing module {name} to admin {} state", if up { "UP" } else { "DOWN" });
    match machine {
        Machine::SmartSwitch => gracefully(name, &key, up),
        Machine::Chassis => {
            if let Err(err) = platform.set_module_admin_state(&key, up) {
                log::error!("Failed to set admin state of {name}: {err}");
            }
        }
    }
}

/// Watch CONFIG_DB CHASSIS_MODULE and apply what it says to the hardware.
///
/// Python runs this in a separate *process* (`ProcessTaskBase`) because its
/// `select()` is blocking and would otherwise hold up the poll loop; a task on
/// a blocking thread is the same separation without the fork -- and without the
/// bug that made the daemon outlive an exception in the child
/// (sonic-buildimage#24775, which the `finally` in
/// `chassisd:ChassisdDaemon.run` works around).
///
/// A panic here aborts the daemon (`pmon_common::thread`), and supervisord
/// starts it again with its watcher.  An exception in Python's child ends only
/// the child, and the daemon carries on without anything applying CONFIG_DB.
fn spawn_config_watcher(
    machine: Machine,
    which: PlatformImpl,
    flags: Arc<WatchFlags>,
) -> tokio::task::JoinHandle<()> {
    tokio::task::spawn_blocking(move || {
        pmon_common::thread::abort_on_panic("CONFIG_DB watcher", || {
            config_watch_loop(machine, which, &flags)
        })
    })
}

fn config_watch_loop(machine: Machine, which: PlatformImpl, flags: &WatchFlags) {
    use swss_common::{DbConnector, SubscriberStateTable};

    // Its own platform handle and its own connection: the poll loop holds the
    // other one mutably, and each thread owning its own is what the rest of
    // these daemons already do.  One that will not load is a watcher that can
    // never run, and it says so to the poll loop like a lost subscription: a
    // daemon up but deaf to `config chassis modules` is the state the strict
    // rule below exists to prevent.
    let mut platform = match platform_provider::open(SYSLOG_IDENTIFIER, which) {
        Ok(p) => p,
        Err(e) => {
            log::error!("config watcher: failed to load chassis: {e}");
            flags.lost.store(true, Ordering::SeqCst);
            return;
        }
    };

    // Detached on purpose: Python never joins these
    // (`chassisd:SmartSwitchModuleConfigUpdater.module_config_update`), and
    // joining is what serialised four DPUs into twenty minutes.
    let gracefully = move |name: &str, key: &DeviceKey, up: bool| {
        drop(spawn_admin_state_change(name, key, up, which));
    };

    let sst = match DbConnector::new_named(CONFIG_DB, true, 0)
        .and_then(|db| SubscriberStateTable::new(db, CHASSIS_CFG_TABLE, None, None))
    {
        Ok(s) => s,
        Err(e) => {
            log::error!("config watcher: cannot subscribe to {CHASSIS_CFG_TABLE}: {e:?}");
            flags.lost.store(true, Ordering::SeqCst);
            return;
        }
    };

    watch_config(&sst, machine, flags, &mut |key, event| {
        apply_config_event(&mut platform, machine, key, event, &gracefully)
    });
}

/// One change to a subscribed table: the row, whether it went, and the
/// fields it now holds.
struct Change {
    key: String,
    deleted: bool,
    fields: BTreeMap<String, String>,
}

/// A subscription the watchers read.
///
/// `SubscriberStateTable` is the one that runs; the trait is what lets the
/// loop's handling of a lost redis be driven by a test.
trait ChangeFeed {
    fn wait(&self, timeout: Duration) -> Result<swss_common::SelectResult, String>;
    fn take(&self) -> Result<Vec<Change>, String>;
}

impl ChangeFeed for swss_common::SubscriberStateTable {
    fn wait(&self, timeout: Duration) -> Result<swss_common::SelectResult, String> {
        // Not interrupted by a signal, as Python's `sel.select` is not: its
        // `interrupt_on_signal` defaults to False.
        self.read_data(timeout, false).map_err(|e| format!("{e:?}"))
    }

    fn take(&self) -> Result<Vec<Change>, String> {
        let events = self.pops().map_err(|e| format!("{e:?}"))?;
        Ok(events
            .into_iter()
            .map(|e| Change {
                deleted: matches!(e.operation, swss_common::KeyOperation::Del),
                fields: e
                    .field_values
                    .iter()
                    .filter_map(|(k, v)| v.to_str().ok().map(|v| (k.clone(), v.to_string())))
                    .collect(),
                key: e.key,
            })
            .collect())
    }
}

/// Hand every change the feed delivers to `on_change` until it is told to stop
/// or is lost.
///
/// Told to stop through `flags.stop`, which is checked each time round -- at
/// least once a second, the select's timeout.  A signal is not a stop: the
/// select waits through one, as Python's `sel.select` does, and a `Signal`
/// answer, should a feed give one, is waited through the same way.  Returning
/// on a signal was how this used to stop, and SIGHUP -- which the daemon
/// ignores -- landing on a watcher's thread then left the daemon up and deaf
/// to CONFIG_DB, with nothing in the log.
///
/// Losing the redis is reported and left on, not retried.
///
/// The watchers and the poll loop share one redis, so a subscription a watcher
/// cannot hold is one the poll loop is about to fail a write on -- retrying
/// here would only decide which of the two noticed first.  And
/// `SubscriberStateTable` does not reconnect: a socket whose server is gone
/// stays readable, so `read_data` keeps answering while `pops` keeps failing.
/// Measured on a SmartSwitch by restarting redis under the daemon, that put
/// 6642 `poll_descriptors: readData error` lines into one second.
///
/// Python's watchers are separate processes whose `pop()` raises, and they
/// never come back -- leaving the daemon up but deaf.  Taking the process down
/// instead is the one place this port is deliberately stricter, because a
/// restart repairs the watcher too.
fn watch(
    feed: &dyn ChangeFeed,
    what: &str,
    flags: &WatchFlags,
    on_change: &mut dyn FnMut(&Change),
) {
    use swss_common::SelectResult;

    // The same one-second timeout Python uses, so a shutdown is not waited out.
    const SELECT_TIMEOUT: Duration = Duration::from_millis(1000);

    while !flags.stop.load(Ordering::SeqCst) {
        match feed.wait(SELECT_TIMEOUT) {
            Ok(SelectResult::Data) => {}
            Ok(SelectResult::Timeout) => continue,
            Ok(SelectResult::Signal) => {
                // Not a warning: SIGTERM lands here as often as not on the
                // way out, and a line per shutdown saying something went
                // wrong would be one more line nobody reads.
                log::debug!("{what}: select interrupted by a signal");
                continue;
            }
            Err(e) => {
                log::error!("{what}: select failed: {e}");
                flags.lost.store(true, Ordering::SeqCst);
                return;
            }
        }
        let changes = match feed.take() {
            Ok(changes) => changes,
            // A closed connection stays readable, so `wait` can keep
            // answering `Data` with only this call failing.
            Err(e) => {
                log::error!("{what}: pops failed: {e}");
                flags.lost.store(true, Ordering::SeqCst);
                return;
            }
        };
        for c in &changes {
            on_change(c);
        }
    }
}

/// Apply every CHASSIS_MODULE change to the hardware.
fn watch_config(
    feed: &dyn ChangeFeed,
    machine: Machine,
    flags: &WatchFlags,
    apply: &mut dyn FnMut(&str, config_updater::Event<'_>),
) {
    watch(feed, "config watcher", flags, &mut |c| {
        if !config_updater::should_apply(machine, &c.key) {
            return;
        }
        let event = if c.deleted {
            config_updater::Event::Del
        } else {
            config_updater::Event::Set {
                admin_status: c.fields.get("admin_status").map(String::as_str),
            }
        };
        apply(&c.key, event);
    });
}

/// Hand every boot_id a DPU publishes to `capture`.
///
/// `chassisd:RebootCauseSubscriberTask.task_worker`: a row that went, or a
/// change that does not carry a boot_id, is not a boot.  Whether the boot_id
/// is new is `capture`'s question, because the answer is on disk.
fn watch_boot_ids(
    feed: &dyn ChangeFeed,
    flags: &WatchFlags,
    capture: &mut dyn FnMut(&str, &str),
) {
    watch(feed, "reboot-cause watcher", flags, &mut |c| {
        if c.deleted {
            return;
        }
        if let Some(boot_id) = c.fields.get(dpu_state::BOOT_ID) {
            capture(&c.key, boot_id);
        }
    });
}

/// Record a DPU's reboot each time it publishes a new boot_id.
///
/// `chassisd:RebootCauseSubscriberTask`.  Its own platform handle and its own
/// connections, for the reason the config watcher has them.  The subscription
/// replays every DPU_STATE row when it starts, which is how a reboot that
/// happened while this daemon was down is still recorded -- and why a boot_id
/// already recorded has to record nothing.
///
/// A panic here aborts the daemon, as one in the config watcher does.
fn spawn_boot_id_watcher(
    which: PlatformImpl,
    flags: Arc<WatchFlags>,
) -> tokio::task::JoinHandle<()> {
    tokio::task::spawn_blocking(move || {
        pmon_common::thread::abort_on_panic("DPU boot-id watcher", || {
            boot_id_watch_loop(which, &flags)
        })
    })
}

fn boot_id_watch_loop(which: PlatformImpl, flags: &WatchFlags) {
    use swss_common::{DbConnector, SubscriberStateTable};

    // One that will not load can record no reboot ever, and says so to the
    // poll loop, as the config watcher does.
    let mut platform = match platform_provider::open(SYSLOG_IDENTIFIER, which) {
        Ok(p) => p,
        Err(e) => {
            log::error!("reboot-cause watcher: failed to load chassis: {e}");
            flags.lost.store(true, Ordering::SeqCst);
            return;
        }
    };
    let reboot_cause = match db::open(CHASSIS_STATE_DB, "REBOOT_CAUSE") {
        Ok(t) => t,
        Err(e) => {
            log::error!("reboot-cause watcher: cannot open REBOOT_CAUSE: {e}");
            flags.lost.store(true, Ordering::SeqCst);
            return;
        }
    };
    let sst = match DbConnector::new_named(CHASSIS_STATE_DB, true, 0)
        .and_then(|db| SubscriberStateTable::new(db, dpu_updater::DPU_STATE_TABLE, None, None))
    {
        Ok(s) => s,
        Err(e) => {
            log::error!(
                "reboot-cause watcher: cannot subscribe to {}: {e:?}",
                dpu_updater::DPU_STATE_TABLE
            );
            flags.lost.store(true, Ordering::SeqCst);
            return;
        }
    };

    let root = Path::new(dpu_reboot_cause::MODULE_REBOOT_CAUSE_DIR);
    watch_boot_ids(&sst, flags, &mut |name, boot_id| {
        dpu_reboot_cause::boot_id_update(
            root,
            name,
            boot_id,
            &mut platform,
            reboot_cause.as_ref(),
            &dpu_updater::time_name(),
            dpu_updater::formatted_from_name,
        );
    });
}

/// The one-time pass a SmartSwitch makes over its DPUs before the loop starts.
///
/// Ports `chassisd:ChassisdDaemon.set_initial_dpu_admin_state`.  Three things
/// happen per DPU, and the third is the one with teeth:
///
/// * the state-transition and gNOI-halt flags are cleared, because a daemon
///   that died holding one would lock every later operation out;
/// * DPU_STATE is seeded from the hardware's own answer, so the recovery
///   machine's first cycle reads planes rather than an absent row;
/// * a DPU with **no CONFIG_DB row at all** is shut down.  [`dpu_updater`]'s
///   `admin_up` already *reads* an unconfigured DPU as not-up, but reading it
///   is not enough: without the shutdown the DPU stays powered with its
///   `oper_status` Online while the CLI renders the missing row as admin
///   `down`.  That pair is in neither half of the accepted set
///   `{(up, Online), (down, Offline)}`, so a SmartSwitch that should have come
///   up dark never converges -- and its DPUs keep burning power.
///
/// A row that *exists* and says `down` is left alone: Python treats only the
/// missing row as `MODULE_STATUS_EMPTY`
/// (`chassisd:SmartSwitchModuleUpdater.get_module_admin_status`), and shutting
/// a configured DPU down here would re-shut one an operator had just started.
///
/// The shutdowns are handed to `shut_down` as one batch rather than called
/// here, because they must run *concurrently* -- see [`shut_down_in_parallel`]
/// -- and because a test wants them recorded rather than performed.
fn init_dpu_admin_state(
    platform: &mut dyn PlatformApi,
    modules: &[ModuleInfo],
    tables: &dpu_updater::Tables<'_>,
    reboot_root: &Path,
    shut_down: Shutter<'_>,
) {
    let mut unconfigured = Vec::new();
    for m in modules {
        // Warned about and passed, where Python makes both calls outside its
        // `try` and an exception ends the daemon
        // (`chassisd:ChassisdDaemon.set_initial_dpu_admin_state`).  A flag
        // that will not clear locks out this one DPU's operations; ending
        // the daemon would leave every DPU on the switch unmanaged.
        if let Err(e) = platform.clear_module_state_transition(&m.key) {
            log::warn!("{}: clear_module_state_transition() failed: {e}", m.name);
        }
        if let Err(e) = platform.clear_module_gnoi_halt(&m.key) {
            log::warn!("{}: clear_module_gnoi_halt() failed: {e}", m.name);
        }

        // A seed that will not write is logged and the sweep goes on, as in
        // Python: the write is `update_dpu_state`, which catches it.  Stopping
        // here would leave every unconfigured DPU powered because
        // CHASSIS_STATE_DB was briefly unavailable.
        //
        // Seeded before the CONFIG_DB row is read.  Python reads the admin
        // status first, in the same `try`, so a row that will not read seeds
        // nothing for that DPU; the seed is the hardware's own answer and
        // does not depend on the configuration, so it is written regardless.
        dpu_updater::init_dpu_state(
            &m.name,
            m.oper_status == Some(ModuleStatus::Online),
            reboot_root,
            tables,
        );

        // A DPU whose CONFIG_DB row cannot be read at all is left alone, and
        // said so in Python's words: Python reaches that case through an
        // exception and shuts nothing down (the `except Exception` in
        // `chassisd:ChassisdDaemon.set_initial_dpu_admin_state`).  Guessing
        // "unconfigured" would power a DPU off because a database was briefly
        // unavailable.
        let Some(config) = tables.config else { continue };
        match config.get(&m.name) {
            Ok(None) => unconfigured.push((m.name.clone(), m.key.clone())),
            Ok(Some(_)) => {}
            Err(e) => {
                log::error!("Error in run: {}: cannot read its CHASSIS_MODULE row: {e}", m.name)
            }
        }
    }
    if !unconfigured.is_empty() {
        shut_down(&unconfigured);
    }
}

/// Shut every named DPU down at once, and wait for all of them.
///
/// One thread per DPU, each with its own platform handle -- the shape
/// `platform_provider::open` was built for, and the same one the CONFIG_DB
/// watcher uses.  Python threads-and-joins here too
/// (`chassisd:ChassisdDaemon.set_initial_dpu_admin_state`), and the reason is
/// measured rather than stylistic: on a SmartSwitch one graceful shutdown takes
/// **~3m20s** -- the vendor call spends over three minutes waiting for the
/// DPU's OS to go down before it so much as logs -- so four in a row would hold
/// up the daemon's first publish by a quarter of an hour and refuse every
/// `config chassis modules startup` in the meantime.  Run together the four
/// finished in 3m24s, the slowest DPU rather than their sum.
///
/// `finalize` is deliberately *not* called on these handles: it runs the
/// interpreter's `atexit` handlers, which belong to the process's exit and not
/// to a worker thread that happens to have finished.
fn shut_down_in_parallel(dpus: &[(String, DeviceKey)], which: PlatformImpl) {
    let threads: Vec<_> = dpus
        .iter()
        .map(|(name, key)| {
            log::info!("Changing module {name} to admin DOWN state");
            spawn_admin_state_change(name, key, false, which)
        })
        .collect();
    for t in threads {
        let _ = t.join();
    }
}

/// Start one DPU's graceful admin-state change on a thread of its own.
///
/// Its own platform handle, because a `Bridge` cannot be shared across threads
/// and because that is how Python gets here too -- every one of its callers
/// reaches `set_admin_state_gracefully` from a fresh `threading.Thread`.
///
/// The caller decides whether to join: start-up does
/// (`chassisd:ChassisdDaemon.set_initial_dpu_admin_state`), the CONFIG_DB
/// watcher does not
/// (`chassisd:SmartSwitchModuleConfigUpdater.module_config_update`).
///
/// A panic ends this thread and nothing else, as an exception does on
/// Python's: the change to this one DPU is lost and logged, and the daemon
/// carries on (`pmon_common::thread`).
fn spawn_admin_state_change(
    name: &str,
    key: &DeviceKey,
    up: bool,
    which: PlatformImpl,
) -> std::thread::JoinHandle<()> {
    let name = name.to_string();
    let key = key.clone();
    std::thread::spawn(move || {
        let thread = format!("Admin state change of {name}");
        pmon_common::thread::log_on_panic(&thread, || {
            match platform_provider::open(SYSLOG_IDENTIFIER, which) {
                Ok(mut p) => {
                    if let Err(e) = p.set_module_admin_state_gracefully(&key, up) {
                        log::error!("Failed to set admin state of {name}: {e}");
                    }
                }
                Err(e) => log::error!("{name}: failed to load chassis: {e}"),
            }
        });
    })
}

// ── SmartSwitch ───────────────────────────────────────────────────────────────

/// `chassisd:PLATFORM_JSON_FILE`, which holds the DPU recovery thresholds
/// (read in `chassisd:SmartSwitchModuleUpdater.__init__`).
const PLATFORM_JSON_FILE: &str = "/usr/share/sonic/platform/platform.json";

/// Power cycling through the platform API, with the state-transition lock.
///
/// The lock is what stops this racing an operator's `module shutdown`: a
/// power cycle that interleaved with one would leave the DPU in whichever
/// state the last write happened to set.
struct PlatformCycler<'a, P: PlatformApi> {
    platform: &'a mut P,
}

/// What a recovery power cycle holds the state-transition lock under.
const TRANSITION_TYPE_RECOVERY: &str = "recovery";

/// Power cycle one DPU, under the lock.
///
/// The order is the behaviour and it is `module_base`'s: take the lock, tell
/// the DPU's OS it is going down, cut and restore the power, tell it it is
/// back, release.  A DPU is running an OS, and cutting its power without
/// telling it loses whatever it had not written out; skipping the lock would
/// race an operator's `module shutdown` and leave the DPU in whichever state
/// the last write happened to set.
///
/// False when the lock could not be taken, which means another operation owns
/// this DPU and *nothing* was done to it.  The base class says so with a
/// False, not an error (`module_base.py:648`): another operation took the
/// lock and has not timed out.  A lock call that *raises* is the `Err`:
/// nothing has been done to the DPU yet, and the caller says so in its own
/// words.  Python's call is outside its `try`, and there the exception ends
/// the daemon.
///
/// Everything after the lock is
/// `chassisd:SmartSwitchModuleUpdater._enter_power_cycle_or_unrecoverable`'s
/// `try`: a hook or a power call that raises is "Power-cycle failed".  A
/// `module_pre_shutdown` that raises cuts nothing: the DPU's OS was not told,
/// so its power is not cut either.  One that answers False is warned about
/// and the cycle goes on, as in Python.
fn power_cycle_sequence(
    platform: &mut dyn PlatformApi,
    module: &str,
    key: &DeviceKey,
) -> Result<bool, PlatformError> {
    if !answered(platform.set_module_state_transition(key, TRANSITION_TYPE_RECOVERY), false)? {
        return Ok(false);
    }
    match answered(platform.module_pre_shutdown(key), true) {
        Ok(told) => {
            if !told {
                log::warn!("{module}: module_pre_shutdown() failed");
            }
            cut_and_restore(platform, module, key);
        }
        Err(e) => log::error!("{module}: Power-cycle failed: {e}"),
    }
    // Released whatever happened above: a lock left held blocks every later
    // operation on this DPU, including the operator's -- so a release that
    // fails is an error somebody needs to read.
    if let Err(e) = answered(platform.clear_module_state_transition(key), false) {
        log::error!("{module}: clear_module_state_transition() failed: {e}");
    }
    Ok(true)
}

/// Power down, power up, and tell the DPU's OS it is back.
///
/// Each call read as Python reads it: `try_get(..., default=...)`, so one the
/// platform does not implement is silent.  One that raises is "Power-cycle
/// failed".  Python's `except` then skips the rest -- including the power-up
/// after a power-down that raised.  Here the power-up is tried regardless: a
/// DPU left powered off by a half-run cycle is the worst way for one to end,
/// and an up on a DPU that is already up costs nothing.
fn cut_and_restore(platform: &mut dyn PlatformApi, module: &str, key: &DeviceKey) {
    for up in [false, true] {
        if let Err(e) = answered(platform.set_module_admin_state(key, up), false) {
            log::error!("{module}: Power-cycle failed: {e}");
        }
    }
    match answered(platform.module_post_startup(key), true) {
        Ok(true) => {}
        Ok(false) => log::warn!("{module}: module_post_startup() failed"),
        Err(e) => log::error!("{module}: Power-cycle failed: {e}"),
    }
}

/// A bool answer, read the way Python's `try_get(f, default=...)` reads it:
/// a method the platform does not implement, or one that answered nothing,
/// gives the default.  Any other error is not an answer at all.
fn answered(
    answer: Result<Option<bool>, PlatformError>,
    default: bool,
) -> Result<bool, PlatformError> {
    match answer {
        Ok(Some(b)) => Ok(b),
        Ok(None) | Err(PlatformError::NotSupported(_)) => Ok(default),
        Err(e) => Err(e),
    }
}

impl<P: PlatformApi> dpu_recovery::PowerCycler for PlatformCycler<'_, P> {
    fn power_cycle(&mut self, name: &str, key: &DeviceKey) -> Result<bool, PlatformError> {
        power_cycle_sequence(self.platform, name, key)
    }

    fn midplane_down_reason(
        &mut self,
        key: &DeviceKey,
    ) -> Result<platform_api::ModuleMidplaneDownReason, PlatformError> {
        self.platform.get_module_midplane_down_reason(key)
    }
}

/// Read the three recovery thresholds out of `platform.json`.
///
/// Three, where Python reads four: `dpu_reboot_timeout` is read there too and
/// then never used.  No file is the defaults, said nothing about; a file that
/// is there and will not read is an error, as Python's `except` logs it.
fn thresholds() -> dpu_recovery::Thresholds {
    let text = match std::fs::read_to_string(PLATFORM_JSON_FILE) {
        Ok(text) => Some(text),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
        // Python's message, which names the file: an `OSError` carries it.
        Err(e) => {
            log::error!("Unexpected error: {e}: '{PLATFORM_JSON_FILE}'");
            None
        }
    };
    thresholds_from(text.as_deref())
}

/// The same, from the file's contents.
///
/// A platform that says nothing keeps the defaults, and so does one whose file
/// will not parse: the alternative is a switch that power cycles its DPUs on a
/// schedule somebody typo'd.
///
/// A value is read as Python's `int()` reads a sensible one: a whole number,
/// a number with a fraction (cut to its whole part), or a string of digits.
/// Negatives and booleans, which `int()` takes too, are refused: a negative
/// timeout or limit is no setting anybody meant -- in Python it times every
/// boot out at once, or gives up on a DPU before its first power cycle -- and
/// `true` as a count of 1 is a typo read as a value.  Anything refused is
/// logged and keeps its default -- one value, where Python's `int()` raising
/// out of its single `try` keeps the defaults for every value read after it
/// too.
fn thresholds_from(text: Option<&str>) -> dpu_recovery::Thresholds {
    let mut t = dpu_recovery::Thresholds::default();
    let Some(text) = text else { return t };
    let v = match serde_json::from_str::<serde_json::Value>(text) {
        Ok(v) => v,
        Err(e) => {
            log::error!("Error parsing {PLATFORM_JSON_FILE}: {e}");
            return t;
        }
    };
    let whole = |k: &str| -> Option<u64> {
        let x = v.get(k)?;
        let n = match x {
            serde_json::Value::Number(n) => n.as_u64().or_else(|| {
                n.as_f64().filter(|f| f.is_finite() && *f >= 0.0).map(|f| f.trunc() as u64)
            }),
            serde_json::Value::String(s) => s.trim().parse::<u64>().ok(),
            _ => None,
        };
        if n.is_none() {
            log::error!(
                "Error parsing {PLATFORM_JSON_FILE}: {k} is {x}, not a non-negative whole number"
            );
        }
        n
    };
    if let Some(n) = whole("dpu_boot_timeout") {
        t.boot_timeout = Duration::from_secs(n);
    }
    if let Some(n) = whole("dpu_self_recovery_timeout") {
        t.self_recovery_timeout = Duration::from_secs(n);
    }
    if let Some(n) = whole("dpu_reset_limit") {
        t.reset_limit = u32::try_from(n).unwrap_or(u32::MAX);
    }
    t
}

/// One pass of the SmartSwitch loop, over a module list already read.
///
/// The read happens in the caller because the power cycler borrows the
/// platform: taking both here would be two mutable borrows of one platform.
/// It is also what makes the pass testable -- the caller is the part that
/// embeds an interpreter.
fn smartswitch_pass(
    modules: &[platform_api::ModuleInfo],
    hw: &mut dyn dpu_recovery::PowerCycler,
    updater: &mut dpu_updater::DpuUpdater,
    tables: &dpu_updater::Tables<'_>,
    chassis_state: Option<&dyn db::Keyspace>,
    now: Instant,
) -> Result<(), String> {
    updater.refresh(modules, tables)?;
    updater.check_midplane(modules, tables, hw)?;
    updater.update_recovery(modules, tables, hw, now)?;
    if let Some(rows) = chassis_state {
        updater.cleanup_shut_down(modules, tables, rows);
    }
    Ok(())
}

/// The SmartSwitch loop, with everything it needs handed to it.
///
/// Generic rather than `&mut dyn PlatformApi` because the power cycler borrows
/// the platform: the two cannot both be taken through one trait object, and a
/// concrete implementation would weld the loop to the PyO3 bridge and put it out of
/// a test's reach.
async fn run_smartswitch_loop<P: PlatformApi>(
    platform: &mut P,
    updater: &mut dpu_updater::DpuUpdater,
    tables: &dpu_updater::Tables<'_>,
    chassis_state: Option<&dyn db::Keyspace>,
    cycles: &mut Cycles,
    watcher_lost: &AtomicBool,
) -> i32 {
    let mut read = Latch::new();
    log::info!("Start daemon main loop");
    loop {
        match cycles.next(UPDATE_PERIOD, &mut *platform).await {
            Tick::Exit(code) => return code,
            Tick::Cycle => {}
        }
        // Same reasoning as the modular loop.  There are two watchers here,
        // the config one and the reboot-cause one, and either can raise it.
        if watcher_lost.load(Ordering::SeqCst) {
            log::error!("A watcher has stopped; stopping the daemon");
            return ERR_DB_WRITE;
        }
        // A failed read skips the pass, as on a chassis (`chassis_pass`), and
        // departs from Python the same way: its reads go through
        // `chassisd:try_get`, which turns only `NotImplementedError` into a
        // default (`chassisd:SmartSwitchModuleUpdater.module_db_update`), so
        // any other failure ends its daemon.  Skipping keeps every DPU's rows
        // and recovery state for the next pass.
        let modules = match platform.get_modules(ModuleInfoCols::ALL) {
            Ok(m) => {
                pmon_common::recovered!(read, "module read recovered");
                m
            }
            Err(e) => {
                pmon_common::fail_once!(read, "Failed to read the modules: {e}");
                continue;
            }
        };
        // `hw` borrows the platform for the whole pass, so what the pass asks
        // of the platform -- a power cycle, a midplane-down reason -- goes
        // through it.  The reboot cause is not asked here at all: it is
        // recorded when a DPU's boot_id changes, by the watcher that
        // subscribes to them.
        let mut hw = PlatformCycler { platform: &mut *platform };
        if let Err(e) =
            smartswitch_pass(&modules, &mut hw, updater, tables, chassis_state, Instant::now())
        {
            // Same reasoning as the modular loop: a STATE_DB that will not
            // take a write is a redis this process has lost, and only a
            // restart gets it back.
            log::error!("{e}");
            return ERR_DB_WRITE;
        }
    }
}

/// The NPU side of a SmartSwitch.
///
/// Everything it reads from outside the process is an argument: the table
/// opener, the key space it sweeps a shut-down DPU out of, the NPU's own last
/// reboot cause and the recovery thresholds.  That is what lets the start-up
/// sequence -- which is where the DPU recovery state machine is seeded, and so
/// where getting it wrong power cycles a healthy DPU -- be driven by a test.
#[allow(clippy::too_many_arguments)]
async fn run_smartswitch<P: PlatformApi>(
    platform: &mut P,
    open: db::Opener<'_>,
    chassis_state: Option<&dyn db::Keyspace>,
    npu_reboot_cause: Option<&str>,
    thresholds: dpu_recovery::Thresholds,
    spawn_watcher: Watcher<'_>,
    shut_down: Shutter<'_>,
    cycles: &mut Cycles,
) -> i32 {
    let h = match open_dpu_tables(open) {
        Ok(h) => h,
        Err(e) => {
            log::error!("{e}");
            return CHASSIS_LOAD_ERROR;
        }
    };
    let (chassis_tbl, module_tbl, midplane_tbl, dpu_state_tbl) =
        (h.chassis, h.module, h.midplane, h.dpu_state);

    let tables = dpu_updater::Tables {
        chassis: chassis_tbl.as_ref(),
        module: module_tbl.as_ref(),
        midplane: midplane_tbl.as_ref(),
        dpu_state: dpu_state_tbl.as_ref(),
        config: Some(h.config.as_ref()),
        device_metadata: Some(h.device_metadata.as_ref()),
    };

    let midplane_initialized = answered(platform.init_midplane_switch(), false).unwrap_or(false);
    if !midplane_initialized {
        log::error!("Chassisd midplane intialization failed");
    }

    let modules = match start_up_modules(platform) {
        Ok(m) => m,
        Err(code) => return code,
    };
    if let Err(e) = module_updater::publish_module_count(&modules, chassis_tbl.as_ref()) {
        log::error!("{e}");
        return ERR_DB_WRITE;
    }

    // Before anything else looks at the DPUs, and before the recovery machine
    // is seeded -- the order `chassisd:ChassisdDaemon.run` runs them in.
    let reboot_root = Path::new(dpu_reboot_cause::MODULE_REBOOT_CAUSE_DIR);
    init_dpu_admin_state(platform, &modules, &tables, reboot_root, shut_down);

    let mut updater = dpu_updater::DpuUpdater::new(
        &modules,
        thresholds,
        midplane_initialized,
        dpu_reboot_cause::MODULE_REBOOT_CAUSE_DIR,
        Instant::now(),
    );

    // The NPU's own memory is what died in a kernel panic, so nothing it
    // believes about the DPUs survives it.
    let npu_crashed = dpu_recovery::npu_crash_on_last_boot(npu_reboot_cause);
    {
        let mut hw = PlatformCycler { platform };
        let now = Instant::now();
        if let Err(e) = updater.init_recovery(&modules, &tables, &mut hw, npu_crashed, now) {
            log::error!("{e}");
            return ERR_DB_WRITE;
        }
    }

    let flags = Arc::new(WatchFlags::default());
    let watchers = vec![
        spawn_watcher(Watch::Config(Machine::SmartSwitch), Arc::clone(&flags)),
        // `chassisd:RebootCauseSubscriberTask`, started beside the config
        // manager in `chassisd:ChassisdDaemon.run` and stopped with it.
        spawn_watcher(Watch::BootIds, Arc::clone(&flags)),
    ];

    let code =
        run_smartswitch_loop(platform, &mut updater, &tables, chassis_state, cycles, &flags.lost)
            .await;

    stop_watchers(&flags, watchers).await;
    updater.clear(&modules_for_teardown(platform, modules), &tables);
    code
}

// ── the copy that runs on a DPU ───────────────────────────────────────────────

/// The two chassis columns the DPU side reads, and nothing else.
///
/// Read every cycle, so a column more is a vendor getter more every ten
/// seconds -- among them `get_reboot_cause` and the LED, which the facade's
/// own rule keeps out of anything read per cycle.  Python's DPU side calls the
/// two plane getters and nothing else.
const DPU_PLANE_COLS: ChassisInfoCols =
    ChassisInfoCols::DATAPLANE_STATE.with(ChassisInfoCols::CONTROLPLANE_STATE);

/// The databases a DPU falls back to when its platform cannot answer.
#[derive(Clone, Copy, Default)]
struct Sources<'a> {
    config_ports: Option<&'a dyn TableLike>,
    port_table: Option<&'a dyn TableLike>,
    system_ready: Option<&'a dyn TableLike>,
}

/// The DPU-side loop, with everything it needs handed to it.
///
/// Separated from the wiring so it can be driven by a test.  Publishing `down`
/// on the way out is part of it: this daemon stopping is the DPU's SONiC going
/// away, and the NPU should hear it now rather than infer it from a timeout.
async fn run_dpu_loop(
    platform: &mut dyn PlatformApi,
    name: &str,
    boot_id: Option<&str>,
    sources: Sources<'_>,
    table: &dyn TableLike,
    cycles: &mut Cycles,
) -> i32 {
    // Written once before anything else, as `chassisd:DpuStateManagerTask`
    // does: the write is the event the NPU records a reboot on, and a daemon
    // restarted without one would leave a reboot the NPU missed unrecorded.
    if let Some(id) = boot_id {
        if let Err(e) = table.set(name, &[(dpu_state::BOOT_ID, id.to_string())]) {
            log::error!("Failed to publish the boot_id for {name}: {e}");
            return ERR_DB_WRITE;
        }
    }
    log::info!("Start daemon main loop");
    let mut read = Latch::new();
    let code = loop {
        // Read afresh each cycle: a platform that implements these answers for
        // itself, and one that does not falls back to the databases -- which is
        // a per-cycle question, not a start-up one, because a platform can
        // begin answering once its own services are up.  Python decides once,
        // at start-up (`chassisd:DpuStateUpdater`), and a daemon that did that
        // would keep reading the fallback forever.
        //
        // A read that fails falls back to the databases too, and says so once:
        // the planes still get an answer, from the same tables Python's
        // fallback reads.  Python's getter raising there would end its daemon.
        let info = match platform.get_chassis_info(DPU_PLANE_COLS) {
            Ok(info) => {
                pmon_common::recovered!(read, "plane state read recovered");
                Some(info)
            }
            Err(e) => {
                pmon_common::fail_once!(
                    read,
                    "Failed to read the plane state, answering from the databases: {e}"
                );
                None
            }
        };
        let planes = match dpu_planes(info.as_ref(), sources) {
            Ok(p) => p,
            Err(e) => {
                log::error!("Failed to read the DPU's plane state: {e}");
                break ERR_DB_WRITE;
            }
        };
        // The DPU's own loop, and the same rule: a CHASSIS_STATE_DB that will
        // not take the planes is one this process has lost for good.
        if let Err(e) = dpu_state::publish(name, &planes, boot_id, table) {
            log::error!("{e}");
            break ERR_DB_WRITE;
        }

        match cycles.next(UPDATE_PERIOD, &mut *platform).await {
            Tick::Exit(code) => break code,
            Tick::Cycle => {}
        }
    };
    dpu_state::shutdown(name, table);
    code
}

/// This DPU's two planes, from the platform or from the databases.
///
/// The platform's answer when it gives one; the databases' otherwise.  The
/// fallbacks read STATE_DB and APPL_DB bare, as Python does
/// (`chassisd:DpuStateUpdater._get_data_plane_state_common`,
/// `chassisd:DpuStateUpdater._get_control_plane_state_common`), and a plane
/// that cannot be determined is an error rather than "down": the NPU's
/// recovery machine acts on "down", and on a DPU that is up and healthy it
/// would end in a power cycle.  A source table that is not there at all is the
/// same error -- `run_on_dpu` does not start without them, so that is a test
/// that left one out.
fn dpu_planes(
    info: Option<&platform_api::ChassisInfo>,
    sources: Sources<'_>,
) -> Result<dpu_state::Planes, String> {
    let missing = |what: &str| format!("no {what} table to read the plane state from");
    let data = match info.and_then(|c| c.dataplane_state) {
        Some(state) => state,
        None => {
            let ports = sources.config_ports.ok_or_else(|| missing("PORT"))?;
            let port_table = sources.port_table.ok_or_else(|| missing("PORT_TABLE"))?;
            dpu_state::data_plane_from_ports(ports, port_table)?
        }
    };
    let control = match info.and_then(|c| c.controlplane_state) {
        Some(state) => state,
        None => {
            let ready = sources.system_ready.ok_or_else(|| missing("SYSTEM_READY"))?;
            dpu_state::control_plane_from_system_ready(ready)?
        }
    };
    Ok(dpu_state::Planes { data, control })
}

/// Report this DPU's own two planes to the NPU.
///
/// Everything it might read is opened up front, and a database that will not
/// open ends the daemon, as Python's unwrapped connects in
/// `chassisd:DpuStateUpdater` do.  The fallback tables are needed only when
/// the platform does not answer for its planes, but whether it does is asked
/// every cycle -- and a fallback opened as optional and then missing read as
/// "down", which is the one answer the NPU power cycles a DPU over.
async fn run_on_dpu<P: PlatformApi>(
    platform: &mut P,
    chassis: &platform_api::ChassisInfo,
    open: db::Opener<'_>,
    cycles: &mut Cycles,
) -> i32 {
    // Which DPU this is names the row the NPU reads.  A platform that cannot
    // say is not guessed for: `DPU0` would overwrite DPU0's own row on the
    // NPU, planes and boot_id both.  Python calls `get_dpu_id()` bare and an
    // exception there ends its daemon.
    let Some(dpu_id) = chassis.dpu_id else {
        log::error!("Failed to read the DPU id; cannot tell the NPU which DPU this is");
        return CHASSIS_LOAD_ERROR;
    };
    let name = format!("DPU{dpu_id}");

    let opened = (|| -> Result<_, String> {
        let one = |db: &str, table: &str| {
            open(db, table).map_err(|e| format!("Failed to connect to {db} for {table}: {e}"))
        };
        Ok((
            one(CHASSIS_STATE_DB, dpu_updater::DPU_STATE_TABLE)?,
            one(CONFIG_DB, "PORT")?,
            one("APPL_DB", "PORT_TABLE")?,
            one(db::STATE_DB, "SYSTEM_READY")?,
        ))
    })();
    let (dpu_state_tbl, config_ports, port_table, system_ready) = match opened {
        Ok(t) => t,
        Err(e) => {
            log::error!("{e}");
            return CHASSIS_LOAD_ERROR;
        }
    };

    let boot_id = dpu_state::read_boot_id(Path::new(dpu_state::BOOT_ID_PATH));
    run_dpu_loop(
        platform,
        &name,
        boot_id.as_deref(),
        Sources {
            config_ports: Some(config_ports.as_ref()),
            port_table: Some(port_table.as_ref()),
            system_ready: Some(system_ready.as_ref()),
        },
        dpu_state_tbl.as_ref(),
        cycles,
    )
    .await
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;
    use std::collections::VecDeque;

    use platform_api::{AsicInfo, ModuleType};
    use pmon_common::db::{MockKeyspace, MockOpener, MockTable};
    use swss_common::SelectResult;

    use super::*;
    use crate::dpu_updater::tests::key_of;

    /// A scripted subscription: each `wait` and each `take` answers with the
    /// next entry.  A script that runs out is the shutdown: it raises `stop`,
    /// as the poll loop does on the way out, and answers a timeout.
    struct Feed<'a> {
        waits: RefCell<VecDeque<Result<SelectResult, String>>>,
        takes: RefCell<VecDeque<Result<Vec<Change>, String>>>,
        stop: &'a AtomicBool,
    }

    impl<'a> Feed<'a> {
        fn new(
            flags: &'a WatchFlags,
            waits: Vec<Result<SelectResult, String>>,
            takes: Vec<Result<Vec<Change>, String>>,
        ) -> Self {
            Self {
                waits: RefCell::new(waits.into_iter().collect()),
                takes: RefCell::new(takes.into_iter().collect()),
                stop: &flags.stop,
            }
        }
    }

    impl ChangeFeed for Feed<'_> {
        fn wait(&self, _timeout: Duration) -> Result<SelectResult, String> {
            self.waits.borrow_mut().pop_front().unwrap_or_else(|| {
                self.stop.store(true, Ordering::SeqCst);
                Ok(SelectResult::Timeout)
            })
        }
        fn take(&self) -> Result<Vec<Change>, String> {
            self.takes.borrow_mut().pop_front().unwrap_or(Ok(Vec::new()))
        }
    }

    fn change(key: &str, deleted: bool, admin_status: Option<&str>) -> Change {
        Change {
            key: key.to_string(),
            deleted,
            fields: admin_status
                .map(|v| [("admin_status".to_string(), v.to_string())].into_iter().collect())
                .unwrap_or_default(),
        }
    }

    /// Drive the watcher over a feed and collect what it applied.
    fn watched(feed: &Feed<'_>, machine: Machine, flags: &WatchFlags) -> Vec<String> {
        let mut applied = Vec::new();
        watch_config(feed, machine, flags, &mut |key, event| {
            applied.push(format!("{key}:{}", config_updater::admin_state_for(machine, event)));
        });
        applied
    }

    /// A select that fails is a CONFIG_DB this thread has lost: the flag the
    /// poll loop reads goes up and the watcher stops.
    #[test]
    fn a_watcher_whose_select_fails_raises_the_flag() {
        let flags = WatchFlags::default();
        let feed = Feed::new(&flags, vec![Err("connection reset".into())], vec![]);
        assert!(watched(&feed, Machine::Chassis, &flags).is_empty());
        assert!(flags.lost.load(Ordering::SeqCst));
    }

    /// A closed connection stays readable, so the failure can show up only in
    /// `pops`.  Same answer, and the watcher does not spin on it.
    #[test]
    fn a_watcher_whose_pops_fail_raises_the_flag_and_stops() {
        let flags = WatchFlags::default();
        let data = || Ok(SelectResult::Data);
        let feed = Feed::new(
            &flags,
            vec![data(), data(), data()],
            vec![Err("connection reset".into()), Ok(vec![change("LINE-CARD0", false, None)])],
        );
        assert!(
            watched(&feed, Machine::Chassis, &flags).is_empty(),
            "nothing after the failure is applied"
        );
        assert!(flags.lost.load(Ordering::SeqCst));
        assert_eq!(feed.takes.borrow().len(), 1, "stopped at the first failure");
    }

    /// Told to stop, a watcher returns, and a stop is not a lost database.
    #[test]
    fn a_watcher_told_to_stop_returns_and_leaves_the_flag_down() {
        let flags = WatchFlags::default();
        flags.stop.store(true, Ordering::SeqCst);
        let feed = Feed::new(&flags, vec![Ok(SelectResult::Data)], vec![]);
        watched(&feed, Machine::Chassis, &flags);
        assert_eq!(feed.waits.borrow().len(), 1, "it did not even wait");
        assert!(!flags.lost.load(Ordering::SeqCst));
    }

    /// A signal is not a stop.  SIGHUP -- which the daemon ignores -- landing
    /// on the watcher's thread used to end it, leaving the daemon up and deaf
    /// to CONFIG_DB.  The change after the signal is still applied.
    #[test]
    fn a_signal_is_waited_through() {
        let flags = WatchFlags::default();
        let feed = Feed::new(
            &flags,
            vec![Ok(SelectResult::Signal), Ok(SelectResult::Data)],
            vec![Ok(vec![change("LINE-CARD0", true, None)])],
        );
        assert_eq!(watched(&feed, Machine::Chassis, &flags), vec!["LINE-CARD0:true".to_string()]);
        assert!(!flags.lost.load(Ordering::SeqCst));
    }

    /// Timeouts are waited through, every change the machine owns is applied
    /// in order, and one it does not own is skipped.
    #[test]
    fn a_watcher_applies_what_its_machine_owns_across_timeouts() {
        use SelectResult::{Data, Timeout};
        let flags = WatchFlags::default();
        let feed = Feed::new(
            &flags,
            vec![Ok(Timeout), Ok(Data), Ok(Timeout), Ok(Data)],
            vec![
                Ok(vec![change("DPU0", false, Some("up")), change("LINE-CARD1", false, None)]),
                Ok(vec![change("DPU1", true, None), change("DPU2", false, None)]),
            ],
        );
        assert_eq!(watched(&feed, Machine::SmartSwitch, &flags), vec![
            "DPU0:true".to_string(),
            "DPU1:false".to_string(),
            "DPU2:false".to_string(),
        ]);
        assert!(!flags.lost.load(Ordering::SeqCst));
    }

    /// And the poll loop acts on it: the daemon leaves with a non-zero code
    /// instead of carrying on against a redis it has lost.
    #[tokio::test]
    async fn a_raised_flag_stops_the_poll_loop() {
        tokio::time::pause();
        let db = Db::new();
        let mut p = FakePlatform {
            modules: vec![card("LINE-CARD0", 1, ModuleStatus::Online)],
            ..Default::default()
        };
        let mut u = ModuleUpdater::new(1, 16, Duration::from_secs(180), true);
        // Ten cycles are offered; the flag has to end it on the first.
        let mut cycles = Cycles::Fixed { remaining: 10, code: 143 };
        let code = run_chassis(
            &mut p, &mut u, &db.tables(), true, &mut cycles, &AtomicBool::new(true))
            .await;
        assert_eq!(code, ERR_DB_WRITE, "a stopped watcher is a non-zero exit");
        assert_eq!(p.reads, 0, "and it leaves before reading the platform again");
    }

    /// A STATE_DB that refuses a write ends the loop the same way.
    ///
    /// This is the half with hardware evidence behind it: on an SN4280 the
    /// old code logged `Failed to update DPU0..3 to DB` about once a second
    /// for as long as redis stayed down, because it neither reconnected nor
    /// left.  Python's unwrapped `set` leaves, and so does this now.
    #[tokio::test]
    async fn a_refused_write_stops_the_poll_loop() {
        tokio::time::pause();
        let db = Db::new();
        let mut p = FakePlatform {
            modules: vec![card("LINE-CARD0", 1, ModuleStatus::Online)],
            ..Default::default()
        };
        let mut u = ModuleUpdater::new(1, 16, Duration::from_secs(180), true);
        db.module.fail_writes("redis is gone");
        let mut cycles = Cycles::Fixed { remaining: 10, code: 143 };
        let code = run_chassis(
            &mut p, &mut u, &db.tables(), true, &mut cycles, &AtomicBool::new(false))
            .await;
        assert_eq!(code, ERR_DB_WRITE, "a lost STATE_DB is a non-zero exit");
    }

    /// A platform whose module list can be swapped and made to fail.
    #[derive(Default)]
    struct FakePlatform {
        modules: Vec<ModuleInfo>,
        asics: Vec<AsicInfo>,
        fail: bool,
        /// What the ASIC read fails with, if it does.
        asics_fail: Option<PlatformError>,
        reads: usize,
    }

    impl PlatformApi for FakePlatform {
        fn get_modules(&mut self, _cols: ModuleInfoCols) -> Result<Vec<ModuleInfo>, PlatformError> {
            self.reads += 1;
            if self.fail {
                return Err(PlatformError::Backend("midplane down".into()));
            }
            Ok(self.modules.clone())
        }
        fn get_asics(&mut self) -> Result<Vec<AsicInfo>, PlatformError> {
            match self.asics_fail.clone() {
                Some(e) => Err(e),
                None => Ok(self.asics.clone()),
            }
        }
    }

    /// An ASIC read that fails is not "no ASICs": the pass is skipped whole,
    /// as a failed module read skips it, rather than publishing `num_asics` 0
    /// -- the count a supervisor would later clean up after a card by.
    #[test]
    fn a_failed_asic_read_skips_the_pass() {
        let log = pmon_common::logging::capture();
        let db = Db::new();
        let mut p = FakePlatform {
            modules: vec![card("LINE-CARD0", 1, ModuleStatus::Online)],
            asics_fail: Some(PlatformError::Backend("i2c timeout".into())),
            ..Default::default()
        };
        let mut u = ModuleUpdater::new(1, 16, Duration::from_secs(180), true);
        chassis_pass(&mut p, &mut u, &db.tables(), false, &mut Latch::new(), SystemTime::now())
            .unwrap();
        assert!(db.module.is_empty(), "nothing published from half a reading");
        assert!(db.hostname.is_empty());
        assert!(
            log.logged(log::Level::Error, "Failed to read the ASICs: platform error: i2c timeout")
        );
    }

    /// An ASIC getter the platform does not implement *is* "none", as Python's
    /// `try_get` reads it, and the pass goes on.
    #[test]
    fn an_unimplemented_asic_getter_is_no_asics() {
        let db = Db::new();
        let mut p = FakePlatform {
            modules: vec![card("LINE-CARD0", 1, ModuleStatus::Online)],
            asics_fail: Some(PlatformError::NotSupported("get_all_asics".into())),
            ..Default::default()
        };
        let mut u = ModuleUpdater::new(1, 16, Duration::from_secs(180), true);
        chassis_pass(&mut p, &mut u, &db.tables(), false, &mut Latch::new(), SystemTime::now())
            .unwrap();
        assert_eq!(db.module.field("LINE-CARD0", NUM_ASICS_FIELD).as_deref(), Some("0"));
    }

    /// The way out waits for the watchers.  `abort` does nothing to a blocking
    /// task once it runs, so a watcher is told through `stop` -- and waited
    /// for, so none is still applying a change while the tables are cleared.
    #[tokio::test]
    async fn the_watchers_are_stopped_and_waited_for_on_the_way_out() {
        let flags = Arc::new(WatchFlags::default());
        let done = Arc::new(AtomicBool::new(false));
        let (seen, finished) = (Arc::clone(&flags), Arc::clone(&done));
        let watcher = tokio::task::spawn_blocking(move || {
            while !seen.stop.load(Ordering::SeqCst) {
                std::thread::sleep(Duration::from_millis(5));
            }
            finished.store(true, Ordering::SeqCst);
        });
        stop_watchers(&flags, vec![watcher]).await;
        assert!(done.load(Ordering::SeqCst), "the watcher had returned before this did");
    }

    /// One wait for all of them, not one each: supervisord's stop timer is
    /// running, and two stuck watchers must not take twice as long as one.
    #[tokio::test]
    async fn stuck_watchers_share_one_wait() {
        tokio::time::pause();
        let stuck = || tokio::spawn(std::future::pending::<()>());
        let start = tokio::time::Instant::now();
        stop_watchers(&WatchFlags::default(), vec![stuck(), stuck()]).await;
        let waited = start.elapsed();
        assert!(waited >= WATCHER_STOP_WAIT && waited < 2 * WATCHER_STOP_WAIT, "{waited:?}");
    }

    fn card(name: &str, slot: i64, status: ModuleStatus) -> ModuleInfo {
        ModuleInfo {
            name: name.to_string(),
            key: key_of(name),
            parent_key: None,
            parent_name: CHASSIS_PARENT.to_string(),
            presence: true,
            is_replaceable: true,
            slot: Some(slot),
            r#type: Some(ModuleType::LineCard),
            oper_status: Some(status),
            midplane_ip: Some("10.0.0.2".to_string()),
            is_midplane_reachable: Some(true),
            ..Default::default()
        }
    }

    struct Db {
        chassis: MockTable,
        module: MockTable,
        midplane: MockTable,
        entity: MockTable,
        asic: MockTable,
        hostname: MockTable,
        module_reboot: MockTable,
    }

    impl Db {
        fn new() -> Self {
            Self {
                chassis: MockTable::new(), module: MockTable::new(),
                midplane: MockTable::new(), entity: MockTable::new(),
                asic: MockTable::new(), hostname: MockTable::new(),
                module_reboot: MockTable::new(),
            }
        }
        fn tables(&self) -> Tables<'_> {
            Tables {
                chassis: &self.chassis, module: &self.module, midplane: &self.midplane,
                entity: &self.entity, asic: &self.asic, hostname: &self.hostname,
                module_reboot: &self.module_reboot, config: None, device_metadata: None,
            }
        }
    }

    /// The module table lives on STATE_DB and the hostname table on
    /// CHASSIS_STATE_DB, and they have the same name.  Opening one on the
    /// other's database would have a supervisor cleaning up against its own
    /// rows -- which nothing downstream could notice.
    #[test]
    fn the_chassis_tables_are_opened_on_the_right_two_databases() {
        let o = MockOpener::new();
        open_chassis_tables(&|d, t| o.open(d, t), true).expect("opens");
        assert_eq!(o.asked(), vec![
            ("STATE_DB".to_string(), "CHASSIS_TABLE".to_string()),
            ("STATE_DB".to_string(), "CHASSIS_MODULE_TABLE".to_string()),
            ("STATE_DB".to_string(), "CHASSIS_MIDPLANE_TABLE".to_string()),
            ("STATE_DB".to_string(), "PHYSICAL_ENTITY_INFO".to_string()),
            ("CHASSIS_STATE_DB".to_string(), "CHASSIS_FABRIC_ASIC_TABLE".to_string()),
            ("CHASSIS_STATE_DB".to_string(), "CHASSIS_MODULE_TABLE".to_string()),
            ("CHASSIS_STATE_DB".to_string(), "CHASSIS_MODULE_REBOOT_INFO_TABLE".to_string()),
            ("CONFIG_DB".to_string(), "CHASSIS_MODULE".to_string()),
            ("CONFIG_DB".to_string(), "DEVICE_METADATA".to_string()),
        ]);
    }

    /// A line card writes its own ASICs; the supervisor writes the fabric
    /// cards'.  They are different tables and the choice cannot change while
    /// the daemon runs.
    #[test]
    fn a_line_card_opens_the_other_asic_table() {
        let o = MockOpener::new();
        open_chassis_tables(&|d, t| o.open(d, t), false).expect("opens");
        assert!(o.asked().contains(
            &("CHASSIS_STATE_DB".to_string(), "CHASSIS_ASIC_TABLE".to_string())));
        assert!(!o.asked().iter().any(|(_, t)| t == "CHASSIS_FABRIC_ASIC_TABLE"));
    }

    #[test]
    fn the_smartswitch_tables_are_opened_on_the_right_two_databases() {
        let o = MockOpener::new();
        open_dpu_tables(&|d, t| o.open(d, t)).expect("opens");
        assert_eq!(o.asked(), vec![
            ("STATE_DB".to_string(), "CHASSIS_TABLE".to_string()),
            ("STATE_DB".to_string(), "CHASSIS_MODULE_TABLE".to_string()),
            ("STATE_DB".to_string(), "CHASSIS_MIDPLANE_TABLE".to_string()),
            ("CHASSIS_STATE_DB".to_string(), "DPU_STATE".to_string()),
            ("CONFIG_DB".to_string(), "CHASSIS_MODULE".to_string()),
            ("CONFIG_DB".to_string(), "DEVICE_METADATA".to_string()),
        ]);
    }

    /// There is nowhere to publish, so the daemon says which table and stops
    /// rather than running with a handle it cannot write through.
    #[test]
    fn a_table_that_will_not_open_names_its_database() {
        let o = MockOpener::failing("CHASSIS_MIDPLANE_TABLE");
        let e = open_chassis_tables(&|d, t| o.open(d, t), true).err().expect("stops");
        assert!(e.contains("STATE_DB"), "{e}");

        let o = MockOpener::failing("DPU_STATE");
        let e = open_dpu_tables(&|d, t| o.open(d, t)).err().expect("stops");
        assert!(e.contains("CHASSIS_STATE_DB"), "{e}");
    }

    /// A platform that records the order it was called in.
    #[derive(Default)]
    struct RecordingPlatform {
        calls: Vec<String>,
        /// `get_module_index` knows no module: the base class's answer.
        no_modules: bool,
        /// `get_module_index` raises for this module: a vendor's exception.
        lookup_error: Option<&'static str>,
        /// What the transition lock answers when it is held; taken if None.
        refuse_lock: Option<Result<Option<bool>, PlatformError>>,
    }

    impl PlatformApi for RecordingPlatform {
        /// `get_module_index`: every module is where its name's number says.
        fn get_module_key(&mut self, name: &str) -> Result<Option<DeviceKey>, PlatformError> {
            if self.lookup_error == Some(name) {
                return Err(PlatformError::Backend(format!("no index for {name}")));
            }
            Ok((!self.no_modules).then(|| key_of(name)))
        }
        fn set_module_state_transition(
            &mut self,
            m: &DeviceKey,
            t: &str,
        ) -> Result<Option<bool>, PlatformError> {
            if let Some(refusal) = self.refuse_lock.clone() {
                return refusal;
            }
            self.calls.push(format!("lock({m},{t})"));
            Ok(Some(true))
        }
        fn clear_module_state_transition(
            &mut self,
            m: &DeviceKey,
        ) -> Result<Option<bool>, PlatformError> {
            self.calls.push(format!("unlock({m})"));
            Ok(Some(true))
        }
        fn module_pre_shutdown(&mut self, m: &DeviceKey) -> Result<Option<bool>, PlatformError> {
            self.calls.push(format!("pre_shutdown({m})"));
            Ok(Some(true))
        }
        fn module_post_startup(&mut self, m: &DeviceKey) -> Result<Option<bool>, PlatformError> {
            self.calls.push(format!("post_startup({m})"));
            Ok(Some(true))
        }
        fn set_module_admin_state(
            &mut self,
            m: &DeviceKey,
            up: bool,
        ) -> Result<Option<bool>, PlatformError> {
            self.calls.push(format!("admin({m},{up})"));
            Ok(Some(true))
        }
        fn set_module_admin_state_gracefully(&mut self, m: &DeviceKey, up: bool)
            -> Result<Option<bool>, PlatformError>
        {
            self.calls.push(format!("graceful({m},{up})"));
            Ok(Some(true))
        }
        fn clear_module_gnoi_halt(&mut self, m: &DeviceKey) -> Result<Option<bool>, PlatformError> {
            self.calls.push(format!("clear_halt({m})"));
            Ok(Some(true))
        }
    }

    /// The order is the behaviour: a DPU is running an OS, and cutting its
    /// power without telling it loses whatever it had not written out.
    #[test]
    fn a_power_cycle_tells_the_dpu_before_and_after_and_holds_the_lock() {
        let mut p = RecordingPlatform::default();
        assert_eq!(power_cycle_sequence(&mut p, "DPU0", &key_of("DPU0")), Ok(true));
        assert_eq!(p.calls, vec![
            "lock(modules[0],recovery)".to_string(),
            "pre_shutdown(modules[0])".to_string(),
            "admin(modules[0],false)".to_string(),
            "admin(modules[0],true)".to_string(),
            "post_startup(modules[0])".to_string(),
            "unlock(modules[0])".to_string(),
        ]);
    }

    /// Another operation owns the DPU.  Nothing is done to it -- not even the
    /// pre-shutdown, which would tell a DPU somebody else is shutting down
    /// that it is about to be power cycled.
    ///
    /// The base class says the lock is held with a False, not an error
    /// (`module_base.py:648`), and that is the answer this used to read as
    /// taken.  A lock the platform does not implement, or one that answers
    /// nothing, is not taken either: `try_get(..., default=False)`.  A lock
    /// call that raises is handed back as the error it is, for the caller to
    /// tell apart from a busy lock.
    #[test]
    fn a_busy_lock_means_the_dpu_is_not_touched_at_all() {
        let failed = PlatformError::Backend("i2c timeout".into());
        for (refusal, answer) in [
            (Ok(Some(false)), Ok(false)),
            (Ok(None), Ok(false)),
            (Err(PlatformError::NotSupported("set_module_state_transition".into())), Ok(false)),
            (Err(failed.clone()), Err(failed.clone())),
        ] {
            let mut p =
                RecordingPlatform { refuse_lock: Some(refusal.clone()), ..Default::default() };
            let key = key_of("DPU0");
            assert_eq!(power_cycle_sequence(&mut p, "DPU0", &key), answer, "{refusal:?}");
            assert!(p.calls.is_empty(), "{refusal:?}");
        }
    }

    /// Record what the watcher hands off instead of starting a thread, and
    /// return the log in the order the events were applied.
    fn recording_gracefully(log: &RefCell<Vec<String>>) -> impl Fn(&str, &DeviceKey, bool) + '_ {
        |_name: &str, key: &DeviceKey, up: bool| {
            log.borrow_mut().push(format!("graceful({key},{up})"))
        }
    }

    /// A DPU is brought down gracefully and a line card is not: the abrupt
    /// call on a DPU cuts the power under a running SONiC.
    #[test]
    fn a_dpu_is_shut_down_gracefully_and_a_line_card_is_not() {
        let mut p = RecordingPlatform::default();
        let handed = RefCell::new(Vec::new());
        apply_config_event(&mut p, Machine::SmartSwitch, "DPU0",
                           config_updater::Event::Set { admin_status: Some("up") },
                           &recording_gracefully(&handed));
        apply_config_event(&mut p, Machine::Chassis, "LINE-CARD0",
                           config_updater::Event::Set { admin_status: None },
                           &recording_gracefully(&handed));
        assert_eq!(handed.into_inner(), vec!["graceful(modules[0],true)".to_string()]);
        assert_eq!(p.calls, vec!["admin(modules[0],false)".to_string()]);
    }

    /// The DPU half never touches the borrowed platform.  That is the whole
    /// point: the call goes to a thread with its own handle, so the watcher
    /// loop is free to pick up the next event -- four DPUs at once rather than
    /// one after another.
    #[test]
    fn a_dpu_event_is_handed_off_and_does_not_block_the_watcher() {
        let mut p = RecordingPlatform::default();
        let handed = RefCell::new(Vec::new());
        for (key, status) in [("DPU0", "down"), ("DPU1", "down"), ("DPU2", "up")] {
            apply_config_event(&mut p, Machine::SmartSwitch, key,
                               config_updater::Event::Set { admin_status: Some(status) },
                               &recording_gracefully(&handed));
        }
        assert_eq!(handed.into_inner(), vec![
            "graceful(modules[0],false)".to_string(),
            "graceful(modules[1],false)".to_string(),
            "graceful(modules[2],true)".to_string(),
        ]);
        assert!(p.calls.is_empty(), "the shared platform must not be used for a DPU");
    }

    /// A CONFIG_DB key is a module's name, resolved as Python resolves it.  A
    /// name the platform has no module for changes nothing, on either machine,
    /// and says so in Python's words.
    #[test]
    fn a_module_name_the_platform_does_not_know_changes_nothing() {
        let log = pmon_common::logging::capture();
        let mut p = RecordingPlatform { no_modules: true, ..Default::default() };
        let handed = RefCell::new(Vec::new());
        apply_config_event(&mut p, Machine::SmartSwitch, "DPU9",
                           config_updater::Event::Set { admin_status: Some("up") },
                           &recording_gracefully(&handed));
        apply_config_event(&mut p, Machine::Chassis, "LINE-CARD9", config_updater::Event::Del,
                           &recording_gracefully(&handed));
        assert!(handed.into_inner().is_empty());
        assert!(p.calls.is_empty());
        assert!(log.logged(log::Level::Error,
            "Unable to get module-index for key DPU9 to set admin-state 1"));
        assert!(log.logged(log::Level::Error,
            "Unable to get module-index for key LINE-CARD9 to set admin-state 1"));
        assert!(!log.contains("Changing module"));
    }

    /// A lookup that raises is where this departs from Python, whose config
    /// updater ends on the exception.  Here only that event ends: nothing is
    /// changed for it, the error is logged, and the next event is applied as
    /// usual -- on either machine.
    #[test]
    fn a_lookup_that_raises_ends_only_its_own_event() {
        let log = pmon_common::logging::capture();
        let mut p = RecordingPlatform { lookup_error: Some("DPU1"), ..Default::default() };
        let handed = RefCell::new(Vec::new());
        apply_config_event(&mut p, Machine::SmartSwitch, "DPU1",
                           config_updater::Event::Set { admin_status: Some("down") },
                           &recording_gracefully(&handed));
        assert!(handed.borrow().is_empty());
        assert!(log.logged(log::Level::Error,
            "Failed to look up module DPU1 to set admin-state 0: "));
        assert!(log.logged(log::Level::Error, "no index for DPU1"));
        assert!(!log.contains("Changing module"));
        apply_config_event(&mut p, Machine::SmartSwitch, "DPU0",
                           config_updater::Event::Set { admin_status: Some("up") },
                           &recording_gracefully(&handed));
        assert_eq!(handed.into_inner(), vec!["graceful(modules[0],true)".to_string()]);

        p.lookup_error = Some("LINE-CARD1");
        let handed = RefCell::new(Vec::new());
        apply_config_event(&mut p, Machine::Chassis, "LINE-CARD1", config_updater::Event::Del,
                           &recording_gracefully(&handed));
        assert!(p.calls.is_empty());
        assert!(log.logged(log::Level::Error,
            "Failed to look up module LINE-CARD1 to set admin-state 1: "));
        apply_config_event(&mut p, Machine::Chassis, "LINE-CARD0", config_updater::Event::Del,
                           &recording_gracefully(&handed));
        assert_eq!(p.calls, vec!["admin(modules[0],true)".to_string()]);
        assert!(handed.into_inner().is_empty());
    }

    /// And the two machines read a removed row oppositely, which is the thing
    /// that would power a DPU *on* if it were got backwards.
    #[test]
    fn a_removed_row_is_applied_oppositely_on_the_two_machines() {
        let mut p = RecordingPlatform::default();
        let handed = RefCell::new(Vec::new());
        apply_config_event(&mut p, Machine::Chassis, "LINE-CARD0", config_updater::Event::Del,
                           &recording_gracefully(&handed));
        apply_config_event(&mut p, Machine::SmartSwitch, "DPU0", config_updater::Event::Del,
                           &recording_gracefully(&handed));
        assert_eq!(p.calls, vec!["admin(modules[0],true)".to_string()]);
        assert_eq!(handed.into_inner(), vec!["graceful(modules[0],false)".to_string()]);
    }

    /// Start-up helpers: one DPU, and a config table that may or may not hold
    /// a row for it.
    fn startup_fixture(oper: ModuleStatus) -> (Vec<ModuleInfo>, [MockTable; 6]) {
        let dpu = ModuleInfo {
            name: "DPU0".to_string(),
            key: key_of("DPU0"),
            parent_key: None,
            presence: true,
            r#type: Some(ModuleType::Dpu),
            oper_status: Some(oper),
            ..Default::default()
        };
        (vec![dpu], std::array::from_fn(|_| MockTable::new()))
    }

    /// The regression this whole function exists for.  A SmartSwitch whose
    /// CONFIG_DB has no CHASSIS_MODULE row must come up *dark*: without the
    /// shutdown the DPU keeps running while the CLI shows it admin down, which
    /// is a pair nothing downstream accepts.
    #[test]
    fn an_unconfigured_dpu_is_shut_down_at_start_up() {
        let (modules, t) = startup_fixture(ModuleStatus::Online);
        let tables = dpu_updater::Tables {
            chassis: &t[0], module: &t[1], midplane: &t[2],
            dpu_state: &t[3],
            config: Some(&t[5]), device_metadata: None,
        };
        let mut p = RecordingPlatform::default();
        let shut = RefCell::new(Vec::new());

        init_dpu_admin_state(&mut p, &modules, &tables, Path::new("/nonexistent"),
                             &|n: &[(String, DeviceKey)]| shut.borrow_mut().extend_from_slice(n));

        assert_eq!(p.calls,
            vec!["unlock(modules[0])".to_string(), "clear_halt(modules[0])".to_string()],
            "the flags are cleared on the daemon's own handle");
        assert_eq!(*shut.borrow(), vec![("DPU0".to_string(), key_of("DPU0"))],
            "and the DPU with no CONFIG_DB row is handed to the shutdown");
    }

    /// A row that exists is an operator's word, and start-up does not overrule
    /// it -- not even when it says `down`, which would re-shut a DPU somebody
    /// had just started.
    #[test]
    fn a_configured_dpu_is_left_alone_at_start_up() {
        for admin in ["up", "down"] {
            let (modules, t) = startup_fixture(ModuleStatus::Online);
            t[5].set("DPU0", &[(CHASSIS_MODULE_ADMIN_STATUS, admin.to_string())]).unwrap();
            let tables = dpu_updater::Tables {
                chassis: &t[0], module: &t[1], midplane: &t[2],
                dpu_state: &t[3],
                config: Some(&t[5]), device_metadata: None,
            };
            let mut p = RecordingPlatform::default();
            let shut = RefCell::new(Vec::new());

            init_dpu_admin_state(&mut p, &modules, &tables, Path::new("/nonexistent"),
                                 &|n: &[(String, DeviceKey)]| {
                                     shut.borrow_mut().extend_from_slice(n)
                                 });

            assert!(shut.borrow().is_empty(),
                "admin_status={admin} is configured; start-up must not override it");
        }
    }

    /// A database that cannot be read is not evidence of an unconfigured DPU.
    /// Guessing would power one off because CONFIG_DB was briefly away -- the
    /// row read that fails is said in Python's words, and nothing is shut down.
    #[test]
    fn an_unreadable_config_table_shuts_nothing_down() {
        let log = pmon_common::logging::capture();
        let (modules, t) = startup_fixture(ModuleStatus::Online);
        t[5].fail_reads("CONFIG_DB is gone");
        let tables = dpu_updater::Tables {
            chassis: &t[0], module: &t[1], midplane: &t[2],
            dpu_state: &t[3],
            config: Some(&t[5]), device_metadata: None,
        };
        let mut p = RecordingPlatform::default();
        let shut = RefCell::new(Vec::new());

        init_dpu_admin_state(&mut p, &modules, &tables, Path::new("/nonexistent"),
                             &|n: &[(String, DeviceKey)]| shut.borrow_mut().extend_from_slice(n));

        assert!(shut.borrow().is_empty(), "a read that failed is not a missing row");
        assert!(
            log.logged(log::Level::Error, "Error in run: DPU0: cannot read its CHASSIS_MODULE row")
        );
        assert!(
            p.calls.iter().any(|c| c == "unlock(modules[0])"),
            "the flags are still cleared; only the shutdown is withheld"
        );
    }

    /// A DPU_STATE seed that will not write is logged in Python's words and
    /// the sweep goes on: an unconfigured DPU is shut down all the same, as
    /// `chassisd:ChassisdDaemon.set_initial_dpu_admin_state` does it.
    #[test]
    fn a_seed_that_will_not_write_still_shuts_down_an_unconfigured_dpu() {
        let log = pmon_common::logging::capture();
        let (modules, t) = startup_fixture(ModuleStatus::Online);
        t[3].fail_writes("connection reset");
        let tables = dpu_updater::Tables {
            chassis: &t[0], module: &t[1], midplane: &t[2],
            dpu_state: &t[3],
            config: Some(&t[5]), device_metadata: None,
        };
        let mut p = RecordingPlatform::default();
        let shut = RefCell::new(Vec::new());

        init_dpu_admin_state(&mut p, &modules, &tables, Path::new("/nonexistent"),
                             &|n: &[(String, DeviceKey)]| shut.borrow_mut().extend_from_slice(n));

        assert!(log.logged(log::Level::Error, "Unexpected error: connection reset"));
        assert_eq!(*shut.borrow(), vec![("DPU0".to_string(), key_of("DPU0"))]);
    }

    /// DPU_STATE is seeded from the hardware, both ways: the recovery machine's
    /// first cycle reads this row, and an absent one reads as "down".
    #[test]
    fn dpu_state_starts_from_what_the_hardware_says() {
        for (oper, want, planes_down) in [
            (ModuleStatus::Online, "up", false),
            (ModuleStatus::Offline, "down", true),
        ] {
            let (modules, t) = startup_fixture(oper);
            let tables = dpu_updater::Tables {
                chassis: &t[0], module: &t[1], midplane: &t[2],
                dpu_state: &t[3],
                config: Some(&t[5]), device_metadata: None,
            };
            let mut p = RecordingPlatform::default();

            init_dpu_admin_state(
                &mut p,
                &modules,
                &tables,
                Path::new("/nonexistent"),
                &|_: &[(String, DeviceKey)]| {},
            );

            assert_eq!(t[3].field("DPU0", dpu_updater::MP_STATE).as_deref(), Some(want));
            assert_eq!(
                t[3].field("DPU0", dpu_updater::CP_STATE).is_some(), planes_down,
                "a midplane that is down takes the other planes with it");
        }
    }

    /// The SmartSwitch loop end to end: a DPU is published, its midplane state
    /// reaches DPU_STATE, and a transient read failure changes nothing.
    #[tokio::test]
    async fn the_smartswitch_loop_publishes_and_survives_a_read_failure() {
        tokio::time::pause();

        #[derive(Default)]
        struct Dpus {
            modules: Vec<ModuleInfo>,
            fail: bool,
        }
        impl PlatformApi for Dpus {
            fn get_modules(
                &mut self,
                _cols: ModuleInfoCols,
            ) -> Result<Vec<ModuleInfo>, PlatformError> {
                if self.fail {
                    return Err(PlatformError::Backend("midplane down".into()));
                }
                Ok(self.modules.clone())
            }
        }

        let root = tempfile::tempdir().unwrap();
        let (chassis, module, midplane, dpu_state) = (
            MockTable::new(), MockTable::new(), MockTable::new(),
            MockTable::new(),
        );
        let tables = dpu_updater::Tables {
            chassis: &chassis, module: &module, midplane: &midplane,
            dpu_state: &dpu_state,
            config: None, device_metadata: None,
        };
        let modules = vec![ModuleInfo {
            name: "DPU0".to_string(),
            key: key_of("DPU0"),
            parent_key: None,
            presence: true,
            r#type: Some(ModuleType::Dpu),
            oper_status: Some(ModuleStatus::Online),
            is_midplane_reachable: Some(true),
            ..Default::default()
        }];
        let mut p = Dpus { modules: modules.clone(), fail: false };
        let mut u = dpu_updater::DpuUpdater::new(
            &modules, dpu_recovery::Thresholds::default(), true, root.path(), Instant::now());

        let mut cycles = Cycles::Fixed { remaining: 1, code: 143 };
        let code = run_smartswitch_loop(
            &mut p,
            &mut u,
            &tables,
            None,
            &mut cycles,
            &AtomicBool::new(false),
        )
        .await;
        assert_eq!(code, 143);
        assert_eq!(module.field("DPU0", "oper_status").as_deref(), Some("Online"));
        assert_eq!(dpu_state.field("DPU0", dpu_updater::MP_STATE).as_deref(), Some("up"));

        p.fail = true;
        let mut cycles = Cycles::Fixed { remaining: 3, code: 0 };
        run_smartswitch_loop(&mut p, &mut u, &tables, None, &mut cycles, &AtomicBool::new(false))
            .await;
        assert_eq!(module.field("DPU0", "oper_status").as_deref(), Some("Online"),
            "a transient failure is not read as every DPU vanishing");
    }

    /// The DPU-side loop end to end.  Publishing `down` on the way out is part
    /// of it: this daemon stopping is the DPU's SONiC going away, and the NPU
    /// should hear it now rather than infer it from a timeout.
    #[tokio::test]
    async fn the_dpu_loop_reports_its_planes_and_says_down_on_the_way_out() {
        tokio::time::pause();

        struct Answering;
        impl PlatformApi for Answering {
            fn get_chassis_info(
                &mut self,
                _cols: ChassisInfoCols,
            ) -> Result<platform_api::ChassisInfo, PlatformError> {
                Ok(platform_api::ChassisInfo {
                    dataplane_state: Some(true),
                    controlplane_state: Some(true),
                    ..Default::default()
                })
            }
        }

        let t = MockTable::new();
        let mut cycles = Cycles::Fixed { remaining: 2, code: 143 };
        let code =
            run_dpu_loop(&mut Answering, "DPU0", None, Sources::default(), &t, &mut cycles).await;

        assert_eq!(code, 143);
        assert_eq!(t.field("DPU0", dpu_updater::DP_STATE).as_deref(), Some("down"));
        assert_eq!(t.field("DPU0", dpu_updater::CP_STATE).as_deref(), Some("down"));
    }

    /// A platform that cannot answer, with no table to answer from either, is
    /// an error and not two planes down: "down" is what the NPU power cycles a
    /// DPU over.  The daemon leaves, saying both planes down on its way out,
    /// which is the truth about a daemon that has stopped.
    #[tokio::test]
    async fn a_dpu_with_no_source_for_its_planes_stops() {
        let log = pmon_common::logging::capture();
        tokio::time::pause();

        struct Silent;
        impl PlatformApi for Silent {}

        let t = MockTable::new();
        let mut cycles = Cycles::Fixed { remaining: 1, code: 0 };
        let code = run_dpu_loop(&mut Silent, "DPU0", None, Sources::default(), &t, &mut cycles)
            .await;
        assert_eq!(code, ERR_DB_WRITE);
        assert!(log.logged(log::Level::Error, "no PORT table to read the plane state from"));
        assert_eq!(t.field("DPU0", dpu_updater::DP_STATE).as_deref(), Some("down"));
    }

    /// The two planes, from whichever source can answer.  A DPU whose platform
    /// answers for itself must not have the databases consulted -- it is the
    /// authority on its own state.
    #[test]
    fn a_dpu_reads_its_planes_from_the_platform_when_it_can() {
        use platform_api::ChassisInfo;
        let info = ChassisInfo {
            dataplane_state: Some(true),
            controlplane_state: Some(false),
            ..Default::default()
        };
        let p = dpu_planes(Some(&info), Sources::default()).unwrap();
        assert!(p.data && !p.control);
    }

    /// And falls back to the databases when it cannot.
    #[test]
    fn a_dpu_falls_back_to_the_databases() {
        let (cfg, app, ready) = (MockTable::new(), MockTable::new(), MockTable::new());
        cfg.set("Ethernet0", &[("admin_status", "up".to_string())]).unwrap();
        app.set("Ethernet0", &[("oper_status", "up".to_string())]).unwrap();
        ready.set("SYSTEM_STATE", &[("Status", "UP".to_string())]).unwrap();
        let sources = Sources {
            config_ports: Some(&cfg),
            port_table: Some(&app),
            system_ready: Some(&ready),
        };

        let p = dpu_planes(None, sources).unwrap();
        assert!(p.data && p.control);

        app.set("Ethernet0", &[("oper_status", "down".to_string())]).unwrap();
        let p = dpu_planes(None, sources).unwrap();
        assert!(!p.data && p.control, "one port down is the data plane down");
    }

    /// With neither source there is nothing to claim either way.  Claiming
    /// `up` would have the NPU believe a DPU that has said nothing; claiming
    /// `down` would have it power cycle one that may be fine.  It is an error.
    #[test]
    fn a_dpu_that_can_answer_from_neither_source_is_an_error() {
        assert!(dpu_planes(None, Sources::default()).is_err());
    }

    /// A pass over the DPUs, driven end to end: the module row is published,
    /// the midplane state lands in DPU_STATE, and an unconfigured DPU is not
    /// power cycled.
    #[test]
    fn a_smartswitch_pass_publishes_and_leaves_an_unconfigured_dpu_alone() {
        struct NoHw(usize);
        impl dpu_recovery::PowerCycler for NoHw {
            fn power_cycle(&mut self, _: &str, _: &DeviceKey) -> Result<bool, PlatformError> {
                self.0 += 1;
                Ok(true)
            }
        }

        let root = tempfile::tempdir().unwrap();
        let (chassis, module, midplane, dpu_state) = (
            MockTable::new(), MockTable::new(), MockTable::new(),
            MockTable::new(),
        );
        let tables = dpu_updater::Tables {
            chassis: &chassis, module: &module, midplane: &midplane,
            dpu_state: &dpu_state,
            config: None, device_metadata: None,
        };
        let dpu = ModuleInfo {
            name: "DPU0".to_string(),
            key: key_of("DPU0"),
            parent_key: None,
            presence: true,
            r#type: Some(ModuleType::Dpu),
            oper_status: Some(ModuleStatus::Online),
            midplane_ip: Some("169.254.200.1".to_string()),
            is_midplane_reachable: Some(true),
            ..Default::default()
        };
        let modules = [dpu];
        let mut u = dpu_updater::DpuUpdater::new(
            &modules, dpu_recovery::Thresholds::default(), true, root.path(), Instant::now());
        let mut hw = NoHw(0);

        smartswitch_pass(&modules, &mut hw, &mut u, &tables, None, Instant::now()).unwrap();

        assert_eq!(module.field("DPU0", "oper_status").as_deref(), Some("Online"));
        assert_eq!(dpu_state.field("DPU0", dpu_updater::MP_STATE).as_deref(), Some("up"));
        assert_eq!(hw.0, 0, "nothing said this DPU should be up");
    }

    /// A platform that says nothing keeps the defaults, and so does one whose
    /// file will not parse: the alternative is a switch that power cycles its
    /// DPUs on a schedule somebody typo'd.
    #[test]
    fn a_platform_without_thresholds_keeps_the_defaults() {
        let d = dpu_recovery::Thresholds::default();
        for text in [None, Some("{not json"), Some("{}")] {
            let t = thresholds_from(text);
            assert_eq!(t.boot_timeout, d.boot_timeout);
            assert_eq!(t.self_recovery_timeout, d.self_recovery_timeout);
            assert_eq!(t.reset_limit, d.reset_limit);
        }
    }

    #[test]
    fn platform_json_overrides_each_threshold_on_its_own() {
        let t = thresholds_from(Some(r#"{"dpu_boot_timeout": 120}"#));
        assert_eq!(t.boot_timeout, Duration::from_secs(120));
        assert_eq!(t.reset_limit, dpu_recovery::Thresholds::default().reset_limit,
            "the ones it did not name are unchanged");

        let t = thresholds_from(Some(
            r#"{"dpu_boot_timeout": 1, "dpu_self_recovery_timeout": 2, "dpu_reset_limit": 3}"#));
        assert_eq!((t.boot_timeout.as_secs(), t.self_recovery_timeout.as_secs(), t.reset_limit),
                   (1, 2, 3));
    }

    /// A value of the wrong shape is ignored rather than taken as zero -- a
    /// `dpu_reset_limit` of 0 would mark every DPU unrecoverable on its first
    /// failure -- and said so, rather than left for somebody to wonder why the
    /// setting did nothing.
    #[test]
    fn a_threshold_of_the_wrong_type_is_ignored_and_said_so() {
        let log = pmon_common::logging::capture();
        let t = thresholds_from(Some(r#"{"dpu_reset_limit": "two"}"#));
        assert_eq!(t.reset_limit, dpu_recovery::Thresholds::default().reset_limit);
        assert!(log.logged(
            log::Level::Error,
            "dpu_reset_limit is \"two\", not a non-negative whole number"
        ));
    }

    /// Negatives and booleans are refused, though Python's `int()` takes them:
    /// a negative boot timeout times every boot out at once, and `true` is not
    /// a count anybody wrote on purpose.
    #[test]
    fn a_negative_or_boolean_threshold_keeps_its_default() {
        let log = pmon_common::logging::capture();
        let d = dpu_recovery::Thresholds::default();
        let t = thresholds_from(Some(
            r#"{"dpu_boot_timeout": -1, "dpu_self_recovery_timeout": "-5",
                "dpu_reset_limit": true}"#,
        ));
        assert_eq!(t.boot_timeout, d.boot_timeout);
        assert_eq!(t.self_recovery_timeout, d.self_recovery_timeout);
        assert_eq!(t.reset_limit, d.reset_limit);
        assert!(log.logged(log::Level::Error, "dpu_boot_timeout is -1, not a non-negative"));
        assert!(log.logged(log::Level::Error, "dpu_reset_limit is true, not a non-negative"));
    }

    /// What Python's `int()` takes, this takes: a string of digits, and a
    /// number with a fraction, cut to its whole part.
    #[test]
    fn a_threshold_is_read_as_python_int_reads_it() {
        let t = thresholds_from(Some(
            r#"{"dpu_boot_timeout": "900", "dpu_self_recovery_timeout": 450.9,
                "dpu_reset_limit": 3}"#,
        ));
        assert_eq!(t.boot_timeout, Duration::from_secs(900));
        assert_eq!(t.self_recovery_timeout, Duration::from_secs(450));
        assert_eq!(t.reset_limit, 3);
    }

    /// A file that will not parse says why, as Python's message does.
    #[test]
    fn an_unparseable_platform_json_says_why() {
        let log = pmon_common::logging::capture();
        thresholds_from(Some("{not json"));
        assert!(log
            .logged(log::Level::Error, "Error parsing /usr/share/sonic/platform/platform.json: "));
    }

    /// The default is three minutes: how long an expected line-card reboot may
    /// take before the midplane loss stops being expected.
    #[test]
    fn the_reboot_timeout_defaults_to_three_minutes() {
        assert_eq!(DEFAULT_LINECARD_REBOOT_TIMEOUT.as_secs(), 180);
    }

    /// The loop end to end: a card is published, and going offline drops its
    /// ASIC rows so nothing routes to a card that is not there.
    #[tokio::test]
    async fn the_loop_publishes_the_cards_and_follows_them_offline() {
        tokio::time::pause();
        let db = Db::new();
        let mut p = FakePlatform {
            modules: vec![card("LINE-CARD0", 1, ModuleStatus::Online)],
            asics: vec![AsicInfo {
                parent_name: "LINE-CARD0".to_string(),
                asic_id: "0".to_string(),
                pci_address: Some("0000:03:00.0".to_string()),
            }],
            ..Default::default()
        };
        let mut u = ModuleUpdater::new(16, 16, Duration::from_secs(180), true);
        let mut cycles = Cycles::Fixed { remaining: 1, code: 143 };
        let code =
            run_chassis(&mut p, &mut u, &db.tables(), true, &mut cycles, &AtomicBool::new(false))
                .await;

        assert_eq!(code, 143);
        assert_eq!(db.module.field("LINE-CARD0", "oper_status").as_deref(), Some("Online"));
        assert_eq!(db.asic.len(), 1);

        p.modules = vec![card("LINE-CARD0", 1, ModuleStatus::Offline)];
        let mut cycles = Cycles::Fixed { remaining: 1, code: 0 };
        run_chassis(&mut p, &mut u, &db.tables(), true, &mut cycles, &AtomicBool::new(false))
            .await;
        assert!(db.asic.is_empty(), "nothing routes to a card that is not there");
        assert_eq!(u.down_modules().len(), 1);
    }

    /// A transient failure must not be read as "every card was pulled": that
    /// would blank CHASSIS_MODULE_TABLE and drop every ASIC row, which on a
    /// chassis is a forwarding outage.
    #[tokio::test]
    async fn a_read_failure_leaves_the_chassis_tables_alone() {
        tokio::time::pause();
        let db = Db::new();
        let mut p = FakePlatform {
            modules: vec![card("LINE-CARD0", 1, ModuleStatus::Online)],
            asics: vec![AsicInfo {
                parent_name: "LINE-CARD0".to_string(),
                asic_id: "0".to_string(),
                pci_address: None,
            }],
            ..Default::default()
        };
        let mut u = ModuleUpdater::new(16, 16, Duration::from_secs(180), true);
        let mut cycles = Cycles::Fixed { remaining: 1, code: 0 };
        run_chassis(&mut p, &mut u, &db.tables(), true, &mut cycles, &AtomicBool::new(false))
            .await;
        assert_eq!(db.asic.len(), 1);

        p.fail = true;
        let mut cycles = Cycles::Fixed { remaining: 3, code: 0 };
        run_chassis(&mut p, &mut u, &db.tables(), true, &mut cycles, &AtomicBool::new(false))
            .await;
        assert_eq!(db.asic.len(), 1, "the rows the last good pass wrote are still there");
        assert_eq!(db.module.field("LINE-CARD0", "oper_status").as_deref(), Some("Online"));
    }

    /// A line card has no view of the chassis app DB, so nothing is cleaned up
    /// from one however long a peer has been out.
    #[tokio::test]
    async fn a_line_card_cleans_up_nothing() {
        tokio::time::pause();
        let db = Db::new();
        let mut p = FakePlatform {
            modules: vec![card("LINE-CARD0", 1, ModuleStatus::Offline)],
            ..Default::default()
        };
        let mut u = ModuleUpdater::new(1, 16, Duration::from_secs(180), true);
        let mut cycles = Cycles::Fixed { remaining: 2, code: 0 };
        run_chassis(&mut p, &mut u, &db.tables(), false, &mut cycles, &AtomicBool::new(false))
            .await;
        // Nothing to assert on the app DB from here; what matters is that the
        // pass completed without reaching for a redis that is not there.
        assert_eq!(p.reads, 2);
    }

    // ── the three shapes, and the wiring that picks between them ──────────────
    //
    // This is the part that used to live in `main` and therefore could not be
    // driven at all.  What it decides is which daemon the process is going to
    // be, which database each table is opened on, and what code it leaves with
    // -- and getting the first of those wrong is what shipped chassisd FATAL on
    // a SmartSwitch with four live DPUs.

    /// A no-op stand-in for the CONFIG_DB watcher.  The real one builds a
    /// second interpreter and subscribes to a redis; what these tests are
    /// about is whether it is started at all.
    /// The start-up sweep, told to do nothing: these tests drive the loop, not
    /// the hardware the sweep would touch.
    fn no_shutdown(_dpus: &[(String, DeviceKey)]) {}

    fn no_watcher(_w: Watch, _flags: Arc<WatchFlags>) -> tokio::task::JoinHandle<()> {
        tokio::spawn(async {})
    }

    /// A platform that reports whatever chassis row a test hands it.
    #[derive(Default)]
    struct Shaped {
        info: Option<platform_api::ChassisInfo>,
        modules: Vec<ModuleInfo>,
        midplane_ok: bool,
    }

    impl PlatformApi for Shaped {
        fn get_chassis_info(
            &mut self,
            _cols: ChassisInfoCols,
        ) -> Result<platform_api::ChassisInfo, PlatformError> {
            self.info.clone().ok_or(PlatformError::Backend("no chassis".into()))
        }
        fn get_modules(&mut self, _cols: ModuleInfoCols) -> Result<Vec<ModuleInfo>, PlatformError> {
            Ok(self.modules.clone())
        }
        /// A midplane that did not come up answers False, the way the base
        /// class documents it -- not an error, which is what the port used
        /// to wait for, so a False read as initialised.
        fn init_midplane_switch(&mut self) -> Result<Option<bool>, PlatformError> {
            Ok(Some(self.midplane_ok))
        }
    }

    fn supervisor_info() -> platform_api::ChassisInfo {
        platform_api::ChassisInfo {
            my_slot: Some(17),
            supervisor_slot: Some(17),
            is_modular_chassis: true,
            ..Default::default()
        }
    }

    /// The failure that once put chassisd in FATAL on a SmartSwitch.  A
    /// platform that raises must not be read as a fixed switch: the two
    /// answers are "something is wrong" and "there is nothing to do here", and
    /// supervisord restarts on both alike, so the code and the line in the log
    /// are all a reader has to tell them apart by.
    #[tokio::test]
    async fn a_chassis_that_will_not_describe_itself_is_a_load_error() {
        let log = pmon_common::logging::capture();
        let o = MockOpener::new();
        let mut p = Shaped::default();
        let code = dispatch(
            &mut p,
            &|d, t| o.open(d, t),
            &no_watcher,
            &no_shutdown,
            &mut Cycles::Fixed { remaining: 0, code: 0 },
        )
        .await;
        assert_eq!(code, CHASSIS_LOAD_ERROR);
        assert!(o.asked().is_empty(), "nothing is opened before the shape is known");
        // And it says the platform raised, not that the platform is wrong.
        assert!(log.logged(log::Level::Error, "Failed to read chassis info"));
        assert!(!log.contains("Chassisd not supported"), "that is a different thing");
    }

    /// A fixed switch answers neither slot.  Python exits with a code of its
    /// own for it, which says what happened to whoever reads the exit status.
    #[tokio::test]
    async fn a_fixed_switch_is_not_supported_rather_than_broken() {
        let log = pmon_common::logging::capture();
        let o = MockOpener::new();
        let mut p =
            Shaped { info: Some(platform_api::ChassisInfo::default()), ..Default::default() };
        let code = dispatch(
            &mut p,
            &|d, t| o.open(d, t),
            &no_watcher,
            &no_shutdown,
            &mut Cycles::Fixed { remaining: 0, code: 0 },
        )
        .await;
        assert_eq!(code, CHASSIS_NOT_SUPPORTED);
        assert!(log.logged(log::Level::Error, "Chassisd not supported for this platform"));
    }

    /// The shape is told with the getters Python asks, in its order, on the
    /// path it asks them on -- and never with every column, which would run a
    /// getter for a LED and a reboot cause, and end this daemon if either
    /// raised.
    #[test]
    fn the_shape_is_told_with_the_getters_python_asks() {
        use platform_api::ChassisInfo;
        use ChassisInfoCols as C;

        struct Recording {
            info: ChassisInfo,
            asked: Vec<ChassisInfoCols>,
        }
        impl PlatformApi for Recording {
            fn get_chassis_info(&mut self, cols: C) -> Result<ChassisInfo, PlatformError> {
                self.asked.push(cols);
                Ok(self.info.clone())
            }
        }

        let npu = ChassisInfo { is_smartswitch: true, ..Default::default() };
        let dpu = ChassisInfo { is_dpu: true, dpu_id: Some(3), ..npu.clone() };
        // `is_dpu` answering true off a SmartSwitch is not a DPU, and is not
        // asked: `chassisd:main` checks `is_smartswitch()` first.
        let fixed = ChassisInfo { is_dpu: true, ..Default::default() };
        for (info, kind, asked) in [
            (fixed, "modular", vec![C::IS_SMARTSWITCH, SLOT_COLS]),
            (npu, "smartswitch", vec![C::IS_SMARTSWITCH, C::IS_DPU]),
            (dpu, "dpu", vec![C::IS_SMARTSWITCH, C::IS_DPU, C::DPU_ID]),
        ] {
            let mut p = Recording { info, asked: Vec::new() };
            let told = match shape(&mut p) {
                Ok(Shape::Modular(_)) => "modular",
                Ok(Shape::SmartSwitch) => "smartswitch",
                Ok(Shape::Dpu(c)) => {
                    assert_eq!(c.dpu_id, Some(3));
                    "dpu"
                }
                Err(code) => panic!("{kind}: exited {code}"),
            };
            assert_eq!(told, kind);
            assert_eq!(p.asked, asked, "{kind}");
        }
    }

    /// A DPU takes the DPU shape, and the DPU shape opens DPU_STATE on
    /// CHASSIS_STATE_DB -- the database a DPU has no local instance of, which
    /// is why the transport had to be TCP.
    #[tokio::test]
    async fn a_dpu_takes_the_dpu_shape() {
        tokio::time::pause();
        let o = MockOpener::new();
        let mut p = Shaped {
            info: Some(platform_api::ChassisInfo {
                is_smartswitch: true,
                is_dpu: true,
                dpu_id: Some(3),
                ..Default::default()
            }),
            ..Default::default()
        };
        let code = dispatch(
            &mut p,
            &|d, t| o.open(d, t),
            &no_watcher,
            &no_shutdown,
            &mut Cycles::Fixed { remaining: 1, code: 143 },
        )
        .await;
        assert_eq!(code, 143);
        assert_eq!(o.asked()[0], ("CHASSIS_STATE_DB".to_string(), "DPU_STATE".to_string()));
        // The name is the dpu_id the platform answered, not the slot: the NPU
        // addresses its DPUs by that name and a mismatch is a row nobody reads.
        let t = o.table("DPU_STATE").unwrap();
        assert_eq!(t.field("DPU3", dpu_updater::DP_STATE).as_deref(), Some("down"));
    }

    /// And a DPU whose state table will not open stops rather than looping
    /// with nowhere to publish.
    #[tokio::test]
    async fn a_dpu_with_no_state_table_stops() {
        let o = MockOpener::failing("DPU_STATE");
        let code = run_on_dpu(
            &mut Shaped::default(),
            &platform_api::ChassisInfo { dpu_id: Some(0), ..Default::default() },
            &|d, t| o.open(d, t),
            &mut Cycles::Fixed { remaining: 0, code: 0 },
        )
        .await;
        assert_eq!(code, CHASSIS_LOAD_ERROR);
    }

    /// Nor does a fallback table: the platform may stop answering for its
    /// planes at any cycle, and a fallback that is not there read as "down".
    #[tokio::test]
    async fn a_dpu_with_no_fallback_table_stops() {
        let o = MockOpener::failing("SYSTEM_READY");
        let code = run_on_dpu(
            &mut Shaped::default(),
            &platform_api::ChassisInfo { dpu_id: Some(0), ..Default::default() },
            &|d, t| o.open(d, t),
            &mut Cycles::Fixed { remaining: 0, code: 0 },
        )
        .await;
        assert_eq!(code, CHASSIS_LOAD_ERROR);
    }

    /// A DPU that cannot say which DPU it is does not guess `DPU0`: that is
    /// DPU0's own row on the NPU, and writing it would overwrite the planes
    /// and the boot_id of a DPU that is fine.
    #[tokio::test]
    async fn a_dpu_without_an_id_publishes_nothing() {
        let log = pmon_common::logging::capture();
        let o = MockOpener::new();
        let code = run_on_dpu(
            &mut Shaped::default(),
            &platform_api::ChassisInfo::default(),
            &|d, t| o.open(d, t),
            &mut Cycles::Fixed { remaining: 0, code: 0 },
        )
        .await;
        assert_eq!(code, CHASSIS_LOAD_ERROR);
        assert!(o.asked().is_empty(), "no table was even opened");
        assert!(log.logged(log::Level::Error, "Failed to read the DPU id"));
    }

    /// The supervisor shape, end to end: the tables are opened, the module
    /// count is published, the watcher is started, and the rows are cleared on
    /// the way out.
    #[tokio::test]
    async fn the_supervisor_publishes_its_card_count_and_clears_on_the_way_out() {
        tokio::time::pause();
        let o = MockOpener::new();
        let mut p = Shaped {
            info: Some(supervisor_info()),
            modules: vec![card("LINE-CARD0", 1, ModuleStatus::Online)],
            midplane_ok: true,
        };
        let code = run_modular(
            &mut p,
            &supervisor_info(),
            &|d, t| o.open(d, t),
            &no_watcher,
            &mut Cycles::Fixed { remaining: 1, code: 143 },
        )
        .await;
        assert_eq!(code, 143);
        // Both databases were reached, and the module table on each of them:
        // the supervisor writes the card's row to STATE_DB and its hostname to
        // CHASSIS_STATE_DB, and the two tables have the same name.
        let asked = o.asked();
        assert!(asked.contains(&("STATE_DB".to_string(), "CHASSIS_MODULE_TABLE".to_string())));
        assert!(
            asked.contains(&("CHASSIS_STATE_DB".to_string(), "CHASSIS_MODULE_TABLE".to_string()))
        );
        // And nothing is left behind.  A stale CHASSIS_MODULE_TABLE row reads
        // as a card that is still there to everything downstream, and a stale
        // `module_num` to the entity MIB; clearing both is what the Python
        // daemon does from __del__.
        assert!(o.table("CHASSIS_MODULE_TABLE").unwrap().get("LINE-CARD0").unwrap().is_none());
        assert!(o.table("CHASSIS_TABLE").unwrap().get(CHASSIS_INFO_KEY).unwrap().is_none());
    }

    /// A module list that cannot be read at start-up ends the daemon, as
    /// Python's unwrapped `get_num_modules()` does -- on both shapes that
    /// manage modules, before anything is published or any watcher started.
    #[tokio::test]
    async fn a_module_list_that_cannot_be_read_at_start_up_is_a_load_error() {
        let log = pmon_common::logging::capture();
        let spawned = std::cell::Cell::new(0);
        let watcher = |_: Watch, _: Arc<WatchFlags>| {
            spawned.set(spawned.get() + 1);
            tokio::spawn(async {})
        };
        let cycles = || Cycles::Fixed { remaining: 1, code: 0 };

        let o = MockOpener::new();
        let mut p = FakePlatform { fail: true, ..Default::default() };
        let code =
            run_modular(&mut p, &supervisor_info(), &|d, t| o.open(d, t), &watcher, &mut cycles())
                .await;
        assert_eq!(code, CHASSIS_LOAD_ERROR);
        assert!(o.table(CHASSIS_INFO_TABLE).unwrap().is_empty(), "no card count published");

        let o = MockOpener::new();
        let mut p = FakePlatform { fail: true, ..Default::default() };
        let code = run_smartswitch(
            &mut p,
            &|d, t| o.open(d, t),
            None,
            None,
            dpu_recovery::Thresholds::default(),
            &watcher,
            &no_shutdown,
            &mut cycles(),
        )
        .await;
        assert_eq!(code, CHASSIS_LOAD_ERROR);
        assert!(o.table(CHASSIS_INFO_TABLE).unwrap().is_empty(), "no card count published");

        assert_eq!(spawned.get(), 0, "no watcher started");
        assert!(log.logged(log::Level::Error, "Failed to read the modules: platform error: "));
    }

    /// The way out reads the modules again, so a card that came or went is the
    /// one cleared; a read that fails then clears the start-up list instead,
    /// which still names every row this daemon wrote.
    #[test]
    fn the_way_out_clears_the_start_up_modules_when_the_read_fails() {
        let log = pmon_common::logging::capture();
        let names = |m: Vec<ModuleInfo>| m.into_iter().map(|m| m.name).collect::<Vec<_>>();
        let at_start = vec![card("LINE-CARD0", 1, ModuleStatus::Online)];
        let mut p = FakePlatform {
            modules: vec![card("LINE-CARD1", 2, ModuleStatus::Online)],
            ..Default::default()
        };
        assert_eq!(names(modules_for_teardown(&mut p, at_start.clone())), ["LINE-CARD1"]);
        assert!(!log.contains("on the way out"));

        p.fail = true;
        assert_eq!(names(modules_for_teardown(&mut p, at_start)), ["LINE-CARD0"]);
        assert!(log.logged(
            log::Level::Warn,
            "Failed to read the modules on the way out, clearing the start-up list: \
             platform error: midplane down"
        ));
    }

    /// A table that will not open is a load error, not a daemon that runs with
    /// a handle it cannot write through.
    #[tokio::test]
    async fn a_modular_chassis_with_a_missing_table_stops() {
        let o = MockOpener::failing("PHYSICAL_ENTITY_INFO");
        let code = run_modular(
            &mut Shaped { info: Some(supervisor_info()), ..Default::default() },
            &supervisor_info(),
            &|d, t| o.open(d, t),
            &no_watcher,
            &mut Cycles::Fixed { remaining: 0, code: 0 },
        )
        .await;
        assert_eq!(code, CHASSIS_LOAD_ERROR);
    }

    /// The NPU side of a SmartSwitch, end to end.  The seeding of the recovery
    /// state machine is the part worth driving: it is handed the NPU's own last
    /// reboot cause, and reading that wrong power cycles a healthy DPU.
    #[tokio::test]
    async fn the_npu_side_seeds_recovery_from_its_own_reboot_cause() {
        tokio::time::pause();
        let o = MockOpener::new();
        let modules = vec![ModuleInfo {
            name: "DPU0".to_string(),
            key: key_of("DPU0"),
            parent_key: None,
            presence: true,
            r#type: Some(ModuleType::Dpu),
            oper_status: Some(ModuleStatus::Online),
            is_midplane_reachable: Some(true),
            ..Default::default()
        }];
        let mut p = Shaped { info: None, modules: modules.clone(), midplane_ok: true };

        let code = run_smartswitch(
            &mut p,
            &|d, t| o.open(d, t),
            None,
            Some("Kernel Panic"),
            dpu_recovery::Thresholds::default(),
            &no_watcher,
            &no_shutdown,
            &mut Cycles::Fixed { remaining: 1, code: 143 },
        )
        .await;
        assert_eq!(code, 143);
        assert!(
            o.table("CHASSIS_TABLE").unwrap().get(CHASSIS_INFO_KEY).unwrap().is_none(),
            "the card count is published at start-up and cleared on the way out"
        );
        // DEVICE_METADATA has to be opened: `dpu_auto_recovery` lives there,
        // and a daemon without it reads auto-recovery as off -- leaving every
        // DPU of a switch that turned it on for an operator.
        assert!(o.asked().iter().any(|(d, t)| d == CONFIG_DB && t == "DEVICE_METADATA"));
    }

    /// And one whose tables will not open stops with the load error rather
    /// than the not-supported one: the code is what tells a reader which of
    /// the two went wrong.
    #[tokio::test]
    async fn a_smartswitch_with_a_missing_table_stops() {
        let o = MockOpener::failing("DPU_STATE");
        let code = run_smartswitch(
            &mut Shaped::default(),
            &|d, t| o.open(d, t),
            None,
            None,
            dpu_recovery::Thresholds::default(),
            &no_watcher,
            &no_shutdown,
            &mut Cycles::Fixed { remaining: 0, code: 0 },
        )
        .await;
        assert_eq!(code, CHASSIS_LOAD_ERROR);
    }

    /// Both run loops stop their watchers on the way out and wait for them
    /// before clearing: a watcher still at work while the rows are cleared can
    /// write one back after the clear.  Driven through the run loops rather
    /// than `stop_watchers` alone, because the wiring is what can go wrong.
    ///
    /// Real time, not paused: the watchers are blocking threads, and paused
    /// time would run the wait out before they had looked at `stop`.
    #[tokio::test]
    async fn both_run_loops_stop_their_watchers_before_clearing() {
        use std::sync::atomic::AtomicUsize;

        for smartswitch in [false, true] {
            let o = MockOpener::new();
            o.open(db::STATE_DB, CHASSIS_INFO_TABLE).unwrap();
            let chassis = o.table(CHASSIS_INFO_TABLE).unwrap();
            let spawned = AtomicUsize::new(0);
            let stopped_before_the_clear = Arc::new(AtomicUsize::new(0));
            let watcher = |_: Watch, flags: Arc<WatchFlags>| {
                spawned.fetch_add(1, Ordering::SeqCst);
                let (chassis, stopped) = (chassis.clone(), Arc::clone(&stopped_before_the_clear));
                tokio::task::spawn_blocking(move || {
                    // Bounded, so wiring that never raises `stop` fails this
                    // test rather than hanging it.
                    for _ in 0..2000 {
                        if flags.stop.load(Ordering::SeqCst) {
                            if chassis.get(CHASSIS_INFO_KEY).unwrap().is_some() {
                                stopped.fetch_add(1, Ordering::SeqCst);
                            }
                            return;
                        }
                        std::thread::sleep(Duration::from_millis(5));
                    }
                })
            };

            let mut cycles = Cycles::Fixed { remaining: 0, code: 0 };
            if smartswitch {
                let mut p = Shaped {
                    modules: vec![online_dpu0()],
                    midplane_ok: true,
                    ..Default::default()
                };
                run_smartswitch(
                    &mut p,
                    &|d, t| o.open(d, t),
                    None,
                    None,
                    dpu_recovery::Thresholds::default(),
                    &watcher,
                    &no_shutdown,
                    &mut cycles,
                )
                .await;
            } else {
                let mut p = Shaped {
                    info: Some(supervisor_info()),
                    modules: vec![card("LINE-CARD0", 1, ModuleStatus::Online)],
                    midplane_ok: true,
                };
                run_modular(&mut p, &supervisor_info(), &|d, t| o.open(d, t), &watcher, &mut cycles)
                    .await;
            }

            let n = spawned.load(Ordering::SeqCst);
            assert_eq!(n, if smartswitch { 2 } else { 1 }, "smartswitch={smartswitch}");
            assert_eq!(stopped_before_the_clear.load(Ordering::SeqCst), n,
                "smartswitch={smartswitch}: every watcher saw `stop` with the rows still there");
            assert!(chassis.get(CHASSIS_INFO_KEY).unwrap().is_none(), "and then they were cleared");
        }
    }

    /// `linecard_reboot_timeout` reads `platform_env.conf`, which exists on a
    /// switch and nowhere else; the reading of it is what a typo reaches.
    #[test]
    fn a_reboot_timeout_that_is_not_a_number_of_seconds_keeps_the_default() {
        assert_eq!(reboot_timeout_from(Some(" 240 ")), Duration::from_secs(240));
        assert_eq!(reboot_timeout_from(Some("180s")), DEFAULT_LINECARD_REBOOT_TIMEOUT);
        assert_eq!(reboot_timeout_from(Some("")), DEFAULT_LINECARD_REBOOT_TIMEOUT);
        assert_eq!(reboot_timeout_from(None), DEFAULT_LINECARD_REBOOT_TIMEOUT);
    }

    /// A platform that refuses whatever a test tells it to.
    #[derive(Default)]
    struct Refusing {
        refuse: &'static str,
        /// How the refused call answers; an error if None.
        refusal: Option<Result<Option<bool>, PlatformError>>,
        calls: Vec<String>,
    }

    impl PlatformApi for Refusing {
        fn get_module_key(&mut self, name: &str) -> Result<Option<DeviceKey>, PlatformError> {
            Ok(Some(key_of(name)))
        }
        fn set_module_state_transition(
            &mut self,
            m: &DeviceKey,
            _t: &str,
        ) -> Result<Option<bool>, PlatformError> {
            self.calls.push(format!("lock({m})"));
            Ok(Some(true))
        }
        fn clear_module_state_transition(
            &mut self,
            m: &DeviceKey,
        ) -> Result<Option<bool>, PlatformError> {
            self.calls.push(format!("unlock({m})"));
            Ok(Some(true))
        }
        fn module_pre_shutdown(&mut self, m: &DeviceKey) -> Result<Option<bool>, PlatformError> {
            self.calls.push(format!("pre({m})"));
            self.answer("module_pre_shutdown")
        }
        fn module_post_startup(&mut self, m: &DeviceKey) -> Result<Option<bool>, PlatformError> {
            self.calls.push(format!("post({m})"));
            self.answer("module_post_startup")
        }
        fn set_module_admin_state(
            &mut self,
            m: &DeviceKey,
            up: bool,
        ) -> Result<Option<bool>, PlatformError> {
            self.calls.push(format!("admin({m},{up})"));
            self.answer("set_module_admin_state")
        }
        fn set_module_admin_state_gracefully(&mut self, m: &DeviceKey, up: bool)
            -> Result<Option<bool>, PlatformError>
        {
            self.calls.push(format!("graceful({m},{up})"));
            self.answer("set_module_admin_state_gracefully")
        }
    }

    impl Refusing {
        fn answer(&self, what: &str) -> Result<Option<bool>, PlatformError> {
            if self.refuse == what {
                return self
                    .refusal
                    .clone()
                    .unwrap_or(Err(PlatformError::Backend("refused".into())));
            }
            Ok(Some(true))
        }
    }

    /// A hook that answers False has failed, as Python's
    /// `if not try_get(hook, default=True)` reads it, and is warned about; one
    /// that raises is "Power-cycle failed", Python's `except`; one the platform
    /// does not implement, or that answers nothing, had nothing to do and says
    /// nothing.  Every way, the lock is released.
    #[test]
    fn a_hook_is_read_as_python_reads_it() {
        for hook in ["module_pre_shutdown", "module_post_startup"] {
            for (refusal, warned, failed) in [
                (Ok(Some(false)), true, false),
                (Err(PlatformError::Backend("refused".into())), false, true),
                (Ok(None), false, false),
                (Err(PlatformError::NotSupported(hook.into())), false, false),
            ] {
                let log = pmon_common::logging::capture();
                let mut p = Refusing {
                    refuse: hook,
                    refusal: Some(refusal.clone()),
                    ..Default::default()
                };
                assert_eq!(power_cycle_sequence(&mut p, "DPU0", &key_of("DPU0")), Ok(true));
                let said = format!("{hook} answering {refusal:?}");
                let warning = format!("DPU0: {hook}() failed");
                assert_eq!(log.logged(log::Level::Warn, &warning), warned, "{said}");
                let error = "DPU0: Power-cycle failed: platform error: refused";
                assert_eq!(log.logged(log::Level::Error, error), failed, "{said}");
                assert_eq!(p.calls.last().map(String::as_str), Some("unlock(modules[0])"));
            }
        }
    }

    /// A power cycle whose power calls or post-startup hook fail still runs to
    /// the end and still releases the lock.  A lock left held blocks every
    /// later operation on that DPU, including the operator's `module
    /// shutdown`; and the power-up is tried even after a power-down that
    /// failed, because a DPU left powered off is the worst place for a half-run
    /// cycle to end.
    #[test]
    fn a_power_cycle_that_half_fails_still_releases_the_lock() {
        for refuse in ["set_module_admin_state", "module_post_startup"] {
            let mut p = Refusing { refuse, ..Default::default() };
            let key = key_of("DPU0");
            assert_eq!(power_cycle_sequence(&mut p, "DPU0", &key), Ok(true), "the lock was taken");
            assert_eq!(
                p.calls.last().map(String::as_str),
                Some("unlock(modules[0])"),
                "{refuse} failing must not leave the lock held"
            );
            assert_eq!(p.calls.len(), 6, "{refuse} failing must not stop the sequence");
        }
    }

    /// A pre-shutdown hook that raises cuts no power: the DPU's OS was not
    /// told it is going down, so it is not taken down -- Python's `except`
    /// skips the rest of the sequence the same way.  The lock is still
    /// released, and the attempt still counts.
    #[test]
    fn a_pre_shutdown_that_raises_cuts_no_power() {
        let log = pmon_common::logging::capture();
        let mut p = Refusing { refuse: "module_pre_shutdown", ..Default::default() };
        assert_eq!(power_cycle_sequence(&mut p, "DPU0", &key_of("DPU0")), Ok(true));
        assert_eq!(p.calls, vec!["lock(modules[0])", "pre(modules[0])", "unlock(modules[0])"]);
        assert!(log.logged(log::Level::Error, "DPU0: Power-cycle failed: platform error: refused"));
    }

    /// A power call the platform does not implement is Python's
    /// `try_get(set_admin_state, default=False)`: nothing to say about it.
    #[test]
    fn an_unimplemented_power_call_says_nothing() {
        let log = pmon_common::logging::capture();
        let mut p = Refusing {
            refuse: "set_module_admin_state",
            refusal: Some(Err(PlatformError::NotSupported("set_admin_state".into()))),
            ..Default::default()
        };
        assert_eq!(power_cycle_sequence(&mut p, "DPU0", &key_of("DPU0")), Ok(true));
        assert!(!log.contains("Power-cycle failed"));
    }

    /// The hardware refused the admin-state write.  It is logged and the
    /// daemon carries on: the next CONFIG_DB event is still worth applying,
    /// and a watcher that stopped here would leave the switch un-administered
    /// with nothing but one line in the log to say so.
    #[test]
    fn a_refused_admin_state_is_logged_and_not_fatal() {
        let mut p = Refusing { refuse: "set_module_admin_state", ..Default::default() };
        let handed = RefCell::new(Vec::new());
        apply_config_event(&mut p, Machine::Chassis, "LINE-CARD0", config_updater::Event::Del,
                           &recording_gracefully(&handed));
        apply_config_event(&mut p, Machine::Chassis, "LINE-CARD1", config_updater::Event::Del,
                           &recording_gracefully(&handed));
        assert_eq!(p.calls, vec![
            "admin(modules[0],true)".to_string(),
            "admin(modules[1],true)".to_string(),
        ]);
    }

    /// A midplane that does not come up is reported and not fatal: the module
    /// table is still worth publishing on a chassis whose midplane is down,
    /// and refusing to start would take the only view of it with it.
    #[tokio::test]
    async fn a_midplane_that_does_not_come_up_does_not_stop_the_daemon() {
        let log = pmon_common::logging::capture();
        tokio::time::pause();
        let o = MockOpener::new();
        let mut p = Shaped {
            info: Some(supervisor_info()),
            modules: vec![card("LINE-CARD0", 1, ModuleStatus::Online)],
            midplane_ok: false,
        };
        let code = run_modular(
            &mut p,
            &supervisor_info(),
            &|d, t| o.open(d, t),
            &no_watcher,
            &mut Cycles::Fixed { remaining: 1, code: 143 },
        )
        .await;
        assert_eq!(code, 143, "the daemon runs and shuts down normally");
        assert!(log.logged(log::Level::Error, "Chassisd midplane intialization failed"),
                "reported, and the Python daemon's spelling of it kept");
    }

    /// The same on the NPU side, plus a SmartSwitch that reports no DPUs at
    /// all -- which is a switch to say something about, not one to stop on.
    #[tokio::test]
    async fn a_smartswitch_with_no_dpus_runs_and_says_so() {
        let log = pmon_common::logging::capture();
        tokio::time::pause();
        let o = MockOpener::new();
        let code = run_smartswitch(
            &mut Shaped { midplane_ok: false, ..Default::default() },
            &|d, t| o.open(d, t),
            None,
            None,
            dpu_recovery::Thresholds::default(),
            &no_watcher,
            &no_shutdown,
            &mut Cycles::Fixed { remaining: 1, code: 143 },
        )
        .await;
        assert_eq!(code, 143);
        assert!(
            o.table("CHASSIS_TABLE").unwrap().get(CHASSIS_INFO_KEY).unwrap().is_none(),
            "no DPUs means no card count, not a count of zero"
        );
        assert!(log.logged(log::Level::Error, "Chassisd has no modules available"));
    }

    /// The sweep of a shut-down DPU reaches the key space rather than the
    /// table, because the two rows it must leave behind -- DPU_STATE and the
    /// reboot-cause history -- are recognised by their table name, and a
    /// `Table` hands back unqualified keys.
    #[test]
    fn a_shut_down_dpu_is_swept_out_of_the_key_space() {

        struct NoHw;
        impl dpu_recovery::PowerCycler for NoHw {
            fn power_cycle(&mut self, _: &str, _: &DeviceKey) -> Result<bool, PlatformError> {
                Ok(true)
            }
        }

        let root = tempfile::tempdir().unwrap();
        let (chassis, module, midplane, dpu_state) = (
            MockTable::new(), MockTable::new(), MockTable::new(),
            MockTable::new(),
        );
        let tables = dpu_updater::Tables {
            chassis: &chassis, module: &module, midplane: &midplane,
            dpu_state: &dpu_state,
            config: None, device_metadata: None,
        };
        let modules = vec![ModuleInfo {
            name: "DPU0".to_string(),
            key: key_of("DPU0"),
            parent_key: None,
            presence: true,
            r#type: Some(ModuleType::Dpu),
            oper_status: Some(ModuleStatus::Offline),
            ..Default::default()
        }];
        let keys = MockKeyspace::new(&[
            "DPU_STATE|DPU0",
            "REBOOT_CAUSE|DPU0|2026_09_18",
            "TEMPERATURE_INFO|DPU0",
        ]);
        let mut u = dpu_updater::DpuUpdater::new(
            &modules, dpu_recovery::Thresholds::default(), true, root.path(), Instant::now());

        smartswitch_pass(&modules, &mut NoHw, &mut u, &tables, Some(&keys), Instant::now())
            .unwrap();

        let left = keys.remaining();
        assert!(left.iter().any(|k| k.starts_with("DPU_STATE")), "the NPU still needs this one");
        assert!(left.iter().any(|k| k.starts_with("REBOOT_CAUSE")), "and this one");
        assert!(
            !left.iter().any(|k| k.starts_with("TEMPERATURE_INFO")),
            "everything else the DPU left behind goes"
        );
    }

    // ── a database that will not take a write ends the run ────────────────────
    //
    // Each of these is a place Python writes bare, so a lost redis there is an
    // uncaught exception and supervisord's restart.  The Rust daemon returns
    // ERR_DB_WRITE from the same place, and nothing after it runs.

    fn online_dpu0() -> ModuleInfo {
        ModuleInfo {
            name: "DPU0".to_string(),
            key: key_of("DPU0"),
            parent_key: None,
            presence: true,
            r#type: Some(ModuleType::Dpu),
            oper_status: Some(ModuleStatus::Online),
            is_midplane_reachable: Some(true),
            ..Default::default()
        }
    }

    /// The supervisor's card count is the first thing it writes; a STATE_DB
    /// that will not take it stops the daemon before the loop starts.
    #[tokio::test]
    async fn a_supervisor_that_cannot_publish_its_card_count_stops() {
        let log = pmon_common::logging::capture();
        let o = MockOpener::new();
        o.open(db::STATE_DB, CHASSIS_INFO_TABLE).unwrap();
        o.table(CHASSIS_INFO_TABLE).unwrap().fail_writes("connection reset");
        let mut p = Shaped {
            info: Some(supervisor_info()),
            modules: vec![card("LINE-CARD0", 1, ModuleStatus::Online)],
            midplane_ok: true,
        };
        let code = run_modular(
            &mut p,
            &supervisor_info(),
            &|d, t| o.open(d, t),
            &no_watcher,
            &mut Cycles::Fixed { remaining: 5, code: 143 },
        )
        .await;
        assert_eq!(code, ERR_DB_WRITE);
        assert!(log.logged(log::Level::Error, "connection reset"));
        assert!(o.table("CHASSIS_MODULE_TABLE").unwrap().get("LINE-CARD0").unwrap().is_none(),
                "the loop never ran");
    }

    /// The NPU side's card count, the same.
    #[tokio::test]
    async fn a_smartswitch_that_cannot_publish_its_card_count_stops() {
        let o = MockOpener::new();
        o.open(db::STATE_DB, CHASSIS_INFO_TABLE).unwrap();
        o.table(CHASSIS_INFO_TABLE).unwrap().fail_writes("connection reset");
        let mut p =
            Shaped { modules: vec![online_dpu0()], midplane_ok: true, ..Default::default() };
        let code = run_smartswitch(
            &mut p,
            &|d, t| o.open(d, t),
            None,
            None,
            dpu_recovery::Thresholds::default(),
            &no_watcher,
            &no_shutdown,
            &mut Cycles::Fixed { remaining: 5, code: 143 },
        )
        .await;
        assert_eq!(code, ERR_DB_WRITE);
        assert!(o.table(dpu_updater::DPU_STATE_TABLE).unwrap().get("DPU0").unwrap().is_none(),
                "the DPU sweep never ran");
    }

    /// A DPU_STATE that refuses every write: the admin-state seed is only
    /// logged, as `chassisd:SmartSwitchModuleUpdater.update_dpu_state` logs
    /// it, so the unconfigured DPU is still shut down.  The recovery seed after
    /// it has no `try` in Python either, and that one stops the daemon.
    #[tokio::test]
    async fn a_dpu_state_that_will_not_write_still_lets_the_sweep_shut_down() {
        let log = pmon_common::logging::capture();
        let o = MockOpener::new();
        o.open(CHASSIS_STATE_DB, dpu_updater::DPU_STATE_TABLE).unwrap();
        o.table(dpu_updater::DPU_STATE_TABLE).unwrap().fail_writes("connection reset");
        let shut = RefCell::new(Vec::<(String, DeviceKey)>::new());
        let record = |dpus: &[(String, DeviceKey)]| shut.borrow_mut().extend(dpus.iter().cloned());
        let mut p =
            Shaped { modules: vec![online_dpu0()], midplane_ok: true, ..Default::default() };
        let code = run_smartswitch(
            &mut p,
            &|d, t| o.open(d, t),
            None,
            None,
            dpu_recovery::Thresholds::default(),
            &no_watcher,
            &record,
            &mut Cycles::Fixed { remaining: 5, code: 143 },
        )
        .await;
        assert_eq!(code, ERR_DB_WRITE);
        assert!(log.logged(log::Level::Error, "Unexpected error: connection reset"));
        assert_eq!(*shut.borrow(), vec![("DPU0".to_string(), key_of("DPU0"))]);
    }

    /// The recovery machine's seed is the next write to DPU_STATE; one that
    /// is refused stops the daemon before the loop.
    #[tokio::test]
    async fn a_recovery_seed_that_will_not_write_stops() {
        let log = pmon_common::logging::capture();
        let o = MockOpener::new();
        o.open(CHASSIS_STATE_DB, dpu_updater::DPU_STATE_TABLE).unwrap();
        // The admin-state sweep's one row goes through; the recovery seed's
        // first does not.
        o.table(dpu_updater::DPU_STATE_TABLE).unwrap().fail_writes_after(1, "connection reset");
        let mut p =
            Shaped { modules: vec![online_dpu0()], midplane_ok: true, ..Default::default() };
        let code = run_smartswitch(
            &mut p,
            &|d, t| o.open(d, t),
            None,
            None,
            dpu_recovery::Thresholds::default(),
            &no_watcher,
            &no_shutdown,
            &mut Cycles::Fixed { remaining: 5, code: 143 },
        )
        .await;
        assert_eq!(code, ERR_DB_WRITE);
        assert!(log.logged(log::Level::Error, "connection reset"));
        assert!(o.table("CHASSIS_MODULE_TABLE").unwrap().get("DPU0").unwrap().is_none(),
                "the loop never ran");
    }

    /// The pieces of the SmartSwitch loop a test drives directly.
    struct Npu {
        _root: tempfile::TempDir,
        chassis: MockTable,
        module: MockTable,
        midplane: MockTable,
        dpu_state: MockTable,
        updater: dpu_updater::DpuUpdater,
    }

    impl Npu {
        /// One online DPU.
        fn new() -> Self {
            let root = tempfile::tempdir().unwrap();
            let updater = dpu_updater::DpuUpdater::new(
                &[online_dpu0()], dpu_recovery::Thresholds::default(), true, root.path(),
                Instant::now());
            Self {
                _root: root,
                chassis: MockTable::new(),
                module: MockTable::new(),
                midplane: MockTable::new(),
                dpu_state: MockTable::new(),
                updater,
            }
        }

        async fn run(&mut self, lost: bool) -> i32 {
            let tables = dpu_updater::Tables {
                chassis: &self.chassis, module: &self.module, midplane: &self.midplane,
                dpu_state: &self.dpu_state,
                config: None, device_metadata: None,
            };
            let mut p =
                Shaped { modules: vec![online_dpu0()], midplane_ok: true, ..Default::default() };
            run_smartswitch_loop(
                &mut p, &mut self.updater, &tables, None,
                &mut Cycles::Fixed { remaining: 5, code: 143 }, &AtomicBool::new(lost),
            )
            .await
        }
    }

    /// The flag the CONFIG_DB watcher raises stops the SmartSwitch loop before
    /// it writes anything.
    #[tokio::test]
    async fn a_lost_config_db_stops_the_smartswitch_loop() {
        tokio::time::pause();
        let mut npu = Npu::new();
        assert_eq!(npu.run(true).await, ERR_DB_WRITE);
        assert!(npu.module.get("DPU0").unwrap().is_none());
    }

    /// A module table that will not take the pass's write stops the loop.
    #[tokio::test]
    async fn a_refused_module_row_stops_the_smartswitch_loop() {
        tokio::time::pause();
        let mut npu = Npu::new();
        npu.module.fail_writes("connection reset");
        assert_eq!(npu.run(false).await, ERR_DB_WRITE);
        assert!(npu.midplane.get("DPU0").unwrap().is_none(), "nothing is written after it");
    }

    /// And the control: the same DPU, with nothing refused, is published and
    /// the loop runs until it is told to stop.  Its reboots are the boot_id
    /// watcher's to record, not the loop's.
    #[tokio::test]
    async fn the_smartswitch_loop_publishes_and_runs_until_told_to_stop() {
        tokio::time::pause();
        let mut npu = Npu::new();
        assert_eq!(npu.run(false).await, 143);
        assert_eq!(npu.module.field("DPU0", "oper_status").as_deref(), Some("Online"));
        assert!(npu.chassis.get_keys().unwrap().is_empty(), "the loop does not own the card count");
        assert!(npu.midplane.get("DPU0").unwrap().is_some());
        assert!(npu.dpu_state.get("DPU0").unwrap().is_some());
    }

    /// A DPU that cannot read its fallback plane state does not report the
    /// plane down -- the NPU acts on that -- and stops instead.
    #[tokio::test]
    async fn a_dpu_that_cannot_read_its_planes_stops_without_reporting_down() {
        tokio::time::pause();
        struct Silent;
        impl PlatformApi for Silent {}

        let log = pmon_common::logging::capture();
        let (config_ports, port_table, system_ready, t) =
            (MockTable::new(), MockTable::new(), MockTable::new(), MockTable::new());
        config_ports.fail_reads("connection reset");
        let sources = Sources {
            config_ports: Some(&config_ports),
            port_table: Some(&port_table),
            system_ready: Some(&system_ready),
        };
        let mut cycles = Cycles::Fixed { remaining: 5, code: 143 };
        let code = run_dpu_loop(&mut Silent, "DPU0", None, sources, &t, &mut cycles).await;
        assert_eq!(code, ERR_DB_WRITE);
        assert!(
            log.logged(log::Level::Error, "Failed to read the DPU's plane state: connection reset")
        );
        // The shutdown row is still written on the way out: the process is
        // going, and that part the NPU must hear.  It is the only write.
        assert_eq!(t.field("DPU0", dpu_updater::DP_STATE).as_deref(), Some("down"));
        assert_eq!(t.writes().iter().filter(|(_, f)| f == dpu_updater::DP_STATE).count(), 1);
    }

    /// A plane read that fails is answered from the databases rather than
    /// ending the loop, and said once; the platform answering again is said
    /// too.
    #[tokio::test]
    async fn a_dpu_plane_read_that_fails_answers_from_the_databases_until_it_recovers() {
        let log = pmon_common::logging::capture();
        tokio::time::pause();
        struct Flaky(usize);
        impl PlatformApi for Flaky {
            fn get_chassis_info(
                &mut self,
                _cols: ChassisInfoCols,
            ) -> Result<platform_api::ChassisInfo, PlatformError> {
                self.0 += 1;
                if self.0 <= 2 {
                    return Err(PlatformError::Backend("ipc timeout".into()));
                }
                Ok(platform_api::ChassisInfo {
                    dataplane_state: Some(false),
                    controlplane_state: Some(true),
                    ..Default::default()
                })
            }
        }

        let (config_ports, port_table, system_ready, t) =
            (MockTable::new(), MockTable::new(), MockTable::new(), MockTable::new());
        system_ready.set("SYSTEM_STATE", &[("Status", "UP".to_string())]).unwrap();
        let sources = Sources {
            config_ports: Some(&config_ports),
            port_table: Some(&port_table),
            system_ready: Some(&system_ready),
        };
        let mut p = Flaky(0);
        let mut cycles = Cycles::Fixed { remaining: 3, code: 143 };
        let code = run_dpu_loop(&mut p, "DPU0", None, sources, &t, &mut cycles).await;

        assert_eq!(code, 143);
        assert!(p.0 > 2, "the platform was asked again after failing");
        assert!(log.logged(
            log::Level::Error,
            "Failed to read the plane state, answering from the databases: \
             platform error: ipc timeout"
        ));
        assert!(log.contains("plane state read recovered"));
        // The data plane up from the databases -- no ports configured -- and
        // then down, the platform's own answer once it gives one: two writes.
        // A failed read that published nothing would have left one.
        let dp = t.writes().into_iter().filter(|(_, f)| f == dpu_updater::DP_STATE).count();
        assert_eq!(dp, 2);
    }

    /// A DPU_STATE that will not take the planes stops the DPU's loop.
    #[tokio::test]
    async fn a_dpu_whose_state_table_refuses_the_planes_stops() {
        let log = pmon_common::logging::capture();
        tokio::time::pause();
        struct Answering;
        impl PlatformApi for Answering {
            fn get_chassis_info(
                &mut self,
                _cols: ChassisInfoCols,
            ) -> Result<platform_api::ChassisInfo, PlatformError> {
                Ok(platform_api::ChassisInfo {
                    dataplane_state: Some(true),
                    controlplane_state: Some(true),
                    ..Default::default()
                })
            }
        }
        let t = MockTable::new();
        t.fail_writes("connection reset");
        let mut cycles = Cycles::Fixed { remaining: 5, code: 143 };
        let code =
            run_dpu_loop(&mut Answering, "DPU0", None, Sources::default(), &t, &mut cycles).await;
        assert_eq!(code, ERR_DB_WRITE);
        assert!(log.logged(log::Level::Error, "connection reset"));
    }

    // ── boot_ids ─────────────────────────────────────────────────────────────

    fn boot(key: &str, boot_id: Option<&str>, deleted: bool) -> Change {
        Change {
            key: key.to_string(),
            deleted,
            fields: boot_id
                .map(|b| [(dpu_state::BOOT_ID.to_string(), b.to_string())].into_iter().collect())
                .unwrap_or_default(),
        }
    }

    /// Only a row that carries a boot_id is a boot: a plane change without
    /// one, or a row that went, is not.
    #[test]
    fn the_boot_id_watcher_hands_on_only_boot_ids() {
        let flags = WatchFlags::default();
        let data = || Ok(SelectResult::Data);
        let feed = Feed::new(
            &flags,
            vec![data()],
            vec![Ok(vec![
                boot("DPU0", Some("b-1"), false),
                boot("DPU1", None, false),
                boot("DPU2", Some("b-9"), true),
            ])],
        );
        let mut seen = Vec::new();
        watch_boot_ids(&feed, &flags, &mut |name, boot_id| {
            seen.push(format!("{name}:{boot_id}"))
        });
        assert_eq!(seen, vec!["DPU0:b-1".to_string()]);
        assert!(!flags.lost.load(Ordering::SeqCst));
    }

    /// A lost subscription is the same lost redis as the config watcher's,
    /// and raises the same flag.
    #[test]
    fn a_boot_id_watcher_whose_pops_fail_raises_the_flag() {
        let flags = WatchFlags::default();
        let feed = Feed::new(
            &flags,
            vec![Ok(SelectResult::Data)],
            vec![Err("connection reset".into())],
        );
        watch_boot_ids(&feed, &flags, &mut |_, _| panic!("nothing to capture"));
        assert!(flags.lost.load(Ordering::SeqCst));
    }

    /// A SmartSwitch's NPU starts both subscriptions, the config watcher and
    /// the boot_id watcher `chassisd:RebootCauseSubscriberTask` is.
    #[tokio::test]
    async fn the_npu_side_watches_config_and_boot_ids() {
        tokio::time::pause();
        let o = MockOpener::new();
        let modules = vec![ModuleInfo {
            name: "DPU0".to_string(),
            key: key_of("DPU0"),
            parent_key: None,
            presence: true,
            r#type: Some(ModuleType::Dpu),
            oper_status: Some(ModuleStatus::Online),
            is_midplane_reachable: Some(true),
            ..Default::default()
        }];
        let mut p = Shaped { info: None, modules, midplane_ok: true };
        let started = std::sync::Mutex::new(Vec::new());
        let recording = |w: Watch, _flags: Arc<WatchFlags>| {
            started.lock().unwrap().push(w);
            tokio::spawn(async {})
        };
        let code = run_smartswitch(
            &mut p,
            &|d, t| o.open(d, t),
            None,
            None,
            dpu_recovery::Thresholds::default(),
            &recording,
            &no_shutdown,
            &mut Cycles::Fixed { remaining: 1, code: 143 },
        )
        .await;
        assert_eq!(code, 143);
        assert_eq!(*started.lock().unwrap(),
                   vec![Watch::Config(Machine::SmartSwitch), Watch::BootIds]);
    }

    /// The DPU publishes its boot_id before anything else, so the NPU hears
    /// of the boot even when the planes have not changed.
    #[tokio::test]
    async fn the_dpu_loop_publishes_its_boot_id_first() {
        tokio::time::pause();
        struct Silent;
        impl PlatformApi for Silent {}
        let t = MockTable::new();
        let (ports, port_table, ready) = (MockTable::new(), MockTable::new(), MockTable::new());
        let sources = Sources {
            config_ports: Some(&ports),
            port_table: Some(&port_table),
            system_ready: Some(&ready),
        };
        let mut cycles = Cycles::Fixed { remaining: 1, code: 143 };
        let code = run_dpu_loop(&mut Silent, "DPU0", Some("b-1"), sources, &t, &mut cycles).await;
        assert_eq!(code, 143);
        assert_eq!(t.writes().first(), Some(&("DPU0".to_string(), dpu_state::BOOT_ID.to_string())));
        assert_eq!(t.field("DPU0", dpu_state::BOOT_ID).as_deref(), Some("b-1"));
    }

    /// The cycler is how the pass reaches the platform, and it forwards the
    /// midplane question to it.
    #[test]
    fn the_cycler_asks_the_platform_why_a_midplane_went_down() {
        struct Says;
        impl PlatformApi for Says {
            fn get_module_midplane_down_reason(
                &mut self,
                module: &DeviceKey,
            ) -> Result<platform_api::ModuleMidplaneDownReason, PlatformError> {
                Ok(platform_api::ModuleMidplaneDownReason {
                    reason: Some(format!("{module} lost power")),
                    detail: None,
                })
            }
        }
        let mut p = Says;
        let mut hw = PlatformCycler { platform: &mut p };
        let why =
            dpu_recovery::PowerCycler::midplane_down_reason(&mut hw, &key_of("DPU0")).unwrap();
        assert_eq!(why.reason.as_deref(), Some("modules[0] lost power"));
    }

    /// A boot_id that will not publish stops the DPU loop before it starts,
    /// the way a plane that will not publish stops it.
    #[tokio::test]
    async fn a_boot_id_that_will_not_publish_stops_the_dpu_loop() {
        let log = pmon_common::logging::capture();
        tokio::time::pause();
        struct Silent;
        impl PlatformApi for Silent {}
        let t = MockTable::new();
        t.fail_writes("redis is gone");
        let mut cycles = Cycles::Fixed { remaining: 1, code: 143 };
        let code =
            run_dpu_loop(&mut Silent, "DPU0", Some("b-1"), Sources::default(), &t, &mut cycles)
                .await;
        assert_eq!(code, ERR_DB_WRITE);
        assert!(log.logged(log::Level::Error, "Failed to publish the boot_id for DPU0"));
    }
}
