//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! thermalctld, in Rust, reading hardware via a native platform crate.
//!
//! On Mellanox/NVIDIA: reads thermal and fan state directly from
//! hw-management sysfs, writing the same TEMPERATURE_INFO, FAN_INFO,
//! FAN_DRAWER_INFO and PHYSICAL_ENTITY_INFO entries to STATE_DB as the
//! Python implementation.  No Python interpreter, no gRPC server, no
//! separate platform-api-server supervisord program.
//!
//! Other vendors keep running the Python thermalctld unchanged: this package
//! names no vendor, and is built and installed only for a platform whose own
//! `rules.mk` opts in.

mod bmc;
mod db;
mod device_env;
mod event_log;
mod fan_updater;
mod fmt;
mod leak_updater;
mod monitor;
mod polling;
mod temp_updater;

use clap::Parser;
use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::signal::unix::{signal, SignalKind};
use tokio::sync::watch;

use platform_api::{ChassisInfoCols, PlatformApi, PlatformError};

use db::StateDb;
use monitor::Monitor;
use platform_provider::PlatformImpl;

const SYSLOG_IDENTIFIER: &str = "thermalctld";
const CHASSIS_GET_ERROR: i32 = 2;

/// What the daemon leaves with when STATE_DB went away.
///
/// Python has no constant for it because it never leaves.  A write that
/// raises under `thermalctld:ThermalMonitor.task_worker` ends that thread and
/// nothing else.  One that raises under `thermalctld:LiquidCoolingUpdater.run`
/// is stored for `thermalctld:LiquidCoolingUpdater.join`, which
/// `thermalctld:ThermalControlDaemon.deinit` only calls on a thread that is
/// still alive, so it is never re-raised.  The Python daemon stays up with
/// FAN_INFO and TEMPERATURE_INFO frozen, or with leak detection gone, until
/// something restarts it.
///
/// Leaving instead is a deliberate departure.  Non-zero is the part that
/// matters -- supervisord's `autorestart=unexpected` reads the code, and a
/// restart is the only way
/// back to a working connection because `DBConnector` never reconnects.
pub const ERR_DB_WRITE: i32 = 1;

/// An interval in seconds, rejected here rather than in `Duration`.
///
/// Every one of these flags is filled in from `pmon_daemon_control.json` by the
/// supervisord template, so a typo in a device profile reaches the command line
/// unfiltered.  `Duration::from_secs_f64` panics on a negative, NaN or
/// overflowing value, which would abort the daemon at the first sleep instead of
/// naming the bad argument.  `polling.rs` rejects the same shapes for the same
/// reason.
fn positive_secs(s: &str) -> Result<f64, String> {
    let v: f64 = s.parse().map_err(|_| format!("`{s}` is not a number"))?;
    if v.is_finite() && v > 0.0 && v <= u32::MAX as f64 {
        Ok(v)
    } else {
        Err(format!("`{s}` is not a usable interval in seconds"))
    }
}

/// As [`positive_secs`], but zero is allowed: this one is compared against, not
/// slept on, and zero means "warn about every cycle".
fn non_negative_secs(s: &str) -> Result<f64, String> {
    let v: f64 = s.parse().map_err(|_| format!("`{s}` is not a number"))?;
    if v.is_finite() && v >= 0.0 && v <= u32::MAX as f64 {
        Ok(v)
    } else {
        Err(format!("`{s}` is not a usable threshold in seconds"))
    }
}

/// Flags mirror the Python daemon's argparse so the supervisord command line
/// works unchanged.
#[derive(Parser, Debug)]
#[command(
    name = "thermalctld-rs",
    about = "SONiC thermal control daemon, in Rust"
)]
struct Args {
    /// Seconds before the first poll, and the fallback period when a cycle
    /// overruns its budget.
    #[arg(long = "thermal-monitor-initial-interval", default_value_t = 5.0, value_parser = positive_secs)]
    thermal_monitor_initial_interval: f64,

    /// Steady-state polling period in seconds.
    #[arg(long = "thermal-monitor-update-interval", default_value_t = 60.0, value_parser = positive_secs)]
    thermal_monitor_update_interval: f64,

    /// Warn when one cycle takes longer than this many seconds.
    #[arg(long = "thermal-monitor-update-elapsed-threshold", default_value_t = 30.0, value_parser = non_negative_secs)]
    thermal_monitor_update_elapsed_threshold: f64,

    /// Whether this platform has leak sensors; set from pmon_daemon_control.json.
    #[arg(long = "enable_liquid_cooling", default_value_t = false)]
    enable_liquid_cooling: bool,

    /// How often the leak thread polls, in seconds.
    #[arg(long = "liquid_cooling_update_interval", default_value_t = 0.5, value_parser = positive_secs)]
    liquid_cooling_update_interval: f64,

    /// Which platform API implementation to use: `pyo3` or `native`.
    ///
    /// Filled in from `platform_api_thermalctld` in `pmon_daemon_control.json`
    /// by the supervisord template, like every other flag here.  Absent means
    /// `pyo3`, which is what every platform runs today -- an unset switch has
    /// to leave a platform on the implementation it has always had.
    #[arg(long, default_value_t = PlatformImpl::Pyo3)]
    platform_api: PlatformImpl,
}

fn init_logging() {
    pmon_common::logging::init(SYSLOG_IDENTIFIER);
}

/// How the leak thread is started.
///
/// Injected because the real one builds a second interpreter and reads sysfs,
/// and what is worth driving is the wiring around it: which platforms start
/// one at all, what happens when it cannot start, and that it is joined before
/// the thermal manager is put back -- a join skipped would leave a thread
/// writing STATE_DB after the rows had been cleared.
type LeakStarter<'a> =
    &'a dyn Fn(Duration, watch::Receiver<bool>) -> std::io::Result<std::thread::JoinHandle<()>>;

/// How the thermal manager's start is put on its own thread.
///
/// Injected for the same reason as `LeakStarter`: the real one opens a second
/// platform handle, and what is worth driving is the wiring around it -- that
/// the poll loop does not wait for it, that the policy does wait for it, and
/// that it is joined before the manager is put back.
type TmInitStarter<'a> =
    &'a dyn Fn(Arc<AtomicBool>) -> std::io::Result<std::thread::JoinHandle<()>>;

// Single-threaded runtime: the workload is ~1 sysfs read/s plus a handful of
// redis writes, and every extra worker thread costs stack that shows up in
// the RSS numbers this daemon exists to improve.
#[tokio::main(flavor = "current_thread")]
async fn main() {
    let args = Args::parse();
    init_logging();

    // Initialise the platform first: the slot or DPU id it reports decides
    // whether there is a suffixed table to open below.
    // Mirrors Python: `chassis = Platform().get_chassis()`
    let mut platform = match platform_provider::open(SYSLOG_IDENTIFIER, args.platform_api) {
        Ok(p) => p,
        Err(e) => {
            log::error!("Failed to initialise platform: {e}");
            std::process::exit(CHASSIS_GET_ERROR);
        }
    };

    let chassis = platform.get_chassis_info(ChassisInfoCols::ALL).ok();
    let slot_or_dpu_id = chassis.as_ref().and_then(slot_or_dpu_id_of);

    // Open STATE_DB tables.  The leak tables are not among them: the leak
    // thread is their only writer and opens them itself, on its own cadence.
    let db = match StateDb::open(slot_or_dpu_id) {
        Ok(db) => db,
        Err(e) => {
            log::error!("Failed to open STATE_DB due to {e:?}");
            std::process::exit(CHASSIS_GET_ERROR);
        }
    };

    // Signal handling: SIGTERM and SIGINT both trigger a graceful shutdown.
    //
    // The signal is remembered, not just the fact of it, because it decides the
    // exit code.  Python sets `exit_code = 128 + sig` in
    // `thermalctld:ThermalControlDaemon.signal_handler` and exits with it
    // (`thermalctld:main`).  Returning zero instead is not cosmetic:
    // supervisord's `autorestart=unexpected` with the default `exitcodes={0}`
    // reads a zero exit as deliberate and does not restart -- and because
    // pmon's `critical_processes` file is empty, nothing reports it either.
    // On hardware that showed up as thermalctld going to `EXITED` on a plain
    // SIGTERM and staying there, silently, with the fans left to hw-management.
    let exit_code = Arc::new(AtomicI32::new(0));
    let signal_code = Arc::clone(&exit_code);
    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    // Shared rather than moved: the signal handler is no longer its only
    // user -- the leak thread needs it to stop the rest of the daemon when
    // STATE_DB goes away.
    let shutdown_tx = Arc::new(shutdown_tx);
    let signal_tx = Arc::clone(&shutdown_tx);
    tokio::spawn(async move {
        let mut sigterm = signal(SignalKind::terminate()).expect("failed to install SIGTERM handler");
        let mut sigint = signal(SignalKind::interrupt()).expect("failed to install SIGINT handler");
        let kind = tokio::select! {
            _ = sigterm.recv() => {
                log::info!("caught SIGTERM, shutting down");
                SignalKind::terminate()
            }
            _ = sigint.recv() => {
                log::info!("caught SIGINT, shutting down");
                SignalKind::interrupt()
            }
        };
        signal_code.store(pmon_common::cycles::exit_code_for(kind), Ordering::SeqCst);
        let _ = signal_tx.send(true);
    });

    // Mirrors Python: `ThermalMonitor(ThermalUpdater(), FanUpdater()).task_worker()`
    let mut monitor = Monitor::new(
        args.thermal_monitor_initial_interval,
        args.thermal_monitor_update_interval,
        args.thermal_monitor_update_elapsed_threshold,
    );
    let is_bmc = device_env::is_switch_bmc();
    // Copied out because `args` is borrowed below and the leak thread's
    // closure has to own what it captures.
    let which = args.platform_api;

    let leak_exit_code = Arc::clone(&exit_code);
    let leak_shutdown_tx = Arc::clone(&shutdown_tx);
    let db_lost = run(
        &mut platform,
        &db,
        &mut monitor,
        &args,
        chassis.as_ref(),
        // Its own platform handle, for the same reason the leak thread has
        // one: the monitor holds this one mutably for the whole run.
        &|ready| {
            std::thread::Builder::new()
                .name("tm-init".into())
                .spawn(move || tm_init_thread_main(which, ready))
        },
        // Its own platform handle and its own tables: the monitor holds the
        // other one mutably, and each thread owning its DB connections is what
        // the rest of this daemon already does.
        &|interval, rx| {
            let exit_code = Arc::clone(&leak_exit_code);
            let shutdown_tx = Arc::clone(&leak_shutdown_tx);
            std::thread::Builder::new()
                .name("leak-updater".into())
                .spawn(move || {
                    leak_thread_main(interval, is_bmc, which, rx, exit_code, shutdown_tx)
                })
        },
        shutdown_rx,
    )
    .await;

    // Exit with the signal's code rather than falling off the end of main.
    // Mirrors Python: `thermalctld:main`.
    let code = final_exit_code(exit_code.load(Ordering::SeqCst), db_lost);
    log::info!("Shutting down with exit code {code}...");
    // Whatever the implementation set up, released before the process goes
    // away.  The PyO3 bridge runs Python's `atexit` handlers here; `main` does
    // not need to know that is what it holds.
    platform.finalize();
    std::process::exit(code);
}

/// The daemon between "the platform is up and the tables are open" and "the
/// process exits", with both of those handed in.
///
/// What is here is the ordering, and the ordering is the behaviour: the
/// thermal manager is started before the poll loop and put back after it, the
/// leak thread is joined before either, and the rows go last.  Getting the
/// last two the other way round leaves a thread writing STATE_DB after the
/// rows have been cleared, which is how a stopped daemon leaves live-looking
/// temperatures behind.
#[allow(clippy::too_many_arguments)]
async fn run(
    platform: &mut dyn PlatformApi,
    db: &StateDb,
    monitor: &mut Monitor,
    args: &Args,
    chassis: Option<&platform_api::ChassisInfo>,
    start_tm_init: TmInitStarter<'_>,
    start_leak: LeakStarter<'_>,
    shutdown_rx: watch::Receiver<bool>,
) -> Option<i32> {
    // Log what was read by the caller rather than reading the platform a
    // second time, which could report something other than what gated the two
    // decisions below.
    match chassis {
        Some(info) => log::info!(
            "Platform ready (modular={}, smartswitch={}, dpu={}, liquid_cooled={})",
            info.is_modular_chassis,
            info.is_smartswitch,
            info.is_dpu,
            info.is_liquid_cooled
        ),
        None => log::warn!("chassis_info failed"),
    }

    // Start the platform's thermal manager -- on its own thread, because
    // Python starts its monitor thread first and only then initializes
    // (`self.thermal_monitor.task_run()` then
    // `self.thermal_manager.initialize()`, both in
    // `thermalctld:ThermalControlDaemon.__init__`).
    //
    // That order is the behaviour, not a detail.  On SPC1
    // `ThermalManager.initialize()` reaches mlnx's
    // `unlink_hw_mgmt_thermal_files()`, which waits for the sysfs links its own
    // previous run unlinked and so spends the whole 300 s timeout on every
    // restart.  Doing it inline left FAN_INFO, FAN_DRAWER_INFO and
    // TEMPERATURE_INFO unpublished for five minutes after every
    // `supervisorctl restart thermalctld`, which is long enough for
    // system-health to report no fans at all.  Python is unaffected only
    // because its updaters are already on another thread by then.
    //
    // A second platform handle rather than this one: the poll loop below
    // borrows this one for the rest of the run.  `ThermalManager`'s methods are
    // all @classmethod, so both handles drive the same Python class object and
    // the `tm_deinitialize()` at the end still pairs with the
    // `tm_initialize()` over there.  The wait releases the GIL -- mlnx's
    // `wait_until_conditions` sleeps on a `threading.Event` -- so the loop
    // keeps its cadence throughout.
    let tm_ready = Arc::new(AtomicBool::new(false));
    let tm_init_thread = match start_tm_init(Arc::clone(&tm_ready)) {
        Ok(handle) => Some(handle),
        Err(e) => {
            // The manager never starting is survivable -- most platforms have
            // none, and the updaters are what fill STATE_DB -- but the policy
            // would then wait forever on a flag nobody sets.
            log::error!("failed to start the thermal manager init thread: {e}");
            tm_ready.store(true, Ordering::Release);
            None
        }
    };

    // Leak detection runs on its own thread at its own cadence — 0.5 s on the
    // platforms that set it — because it is a hundred times faster than the
    // poll cycle and a slow sysfs read must not delay either side.
    let leak_thread = if leak_enabled(args.enable_liquid_cooling, chassis) {
        let interval = Duration::from_secs_f64(args.liquid_cooling_update_interval);
        // Leak detection is a safety function, so a thread that fails to start
        // is reported rather than quietly leaving the platform unmonitored.
        match start_leak(interval, shutdown_rx.clone()) {
            Ok(handle) => {
                // Python announces this at NOTICE
                // (`thermalctld:ThermalControlDaemon.__init__`) and an operator
                // reads the pair as "leak detection is on".  Saying only that
                // it stopped leaves the far more important half -- that it ever
                // ran -- to be inferred from silence.
                pmon_common::notice!("Started thread for liquid cooling updater");
                Some(handle)
            }
            Err(e) => {
                log::error!("failed to start the leak updater thread, leak monitoring is off: {e}");
                None
            }
        }
    } else {
        None
    };

    let db_lost = monitor.run(platform, db, Arc::clone(&tm_ready), shutdown_rx).await;

    if let Some(h) = leak_thread {
        let _ = h.join();
        pmon_common::notice!("Joined thread for liquid cooling updater");
    }

    // Joined before the manager is put back, so the two calls stay paired: a
    // `deinitialize()` racing an `initialize()` still inside its 300 s wait
    // would stop a manager that had not started.  Python cannot reach its
    // `deinit()` at all while `initialize()` is blocked, so waiting here is
    // the same behaviour rather than a stall this port introduces.
    if let Some(h) = tm_init_thread {
        let _ = h.join();
    }

    // Cleanup: restores hw-management-tc autonomous mode (Mellanox).
    // Mirrors Python: `ThermalManager.stop()` then `.deinitialize()`.
    if let Err(e) = platform.tm_deinitialize() {
        log::warn!("ThermalManager deinitialize failed: {e}");
    }

    // Rows this daemon owns go with it.  Mirrors the two Python destructors;
    // see `StateDb::clear`.
    db.clear();

    db_lost
}

/// The thermal-manager thread: open a handle, start the manager, say it is up.
///
/// Writes nothing to STATE_DB and holds nothing the poll loop needs, so it is
/// free to take as long as the platform makes it take.
fn tm_init_thread_main(which: PlatformImpl, ready: Arc<AtomicBool>) {
    match platform_provider::open(SYSLOG_IDENTIFIER, which) {
        Ok(mut platform) => tm_init_with(&mut platform, &ready),
        Err(e) => {
            log::error!("thermal manager init: cannot initialise platform: {e}");
            // Same reason as the one in `tm_init_with`: a handle that could
            // not be opened is still a manager that will never come up.
            ready.store(true, Ordering::Release);
        }
    }
    // No `finalize()` here: that runs Python's atexit handlers, which is a
    // process-wide, once-only thing `main` does on the way out.  The leak
    // thread leaves its handle alone for the same reason.
}

/// Start the manager, then release the policy -- whatever the manager said.
///
/// Split from the thread entry point above so this half can be driven without
/// a platform handle: the release is the part with teeth, and it has to happen
/// on the failing path too.
fn tm_init_with(platform: &mut dyn PlatformApi, ready: &AtomicBool) {
    // The two `except` clauses in `thermalctld:ThermalControlDaemon.__init__`,
    // word for word and at their levels.  Python formats the exception with
    // `repr()`; the platform error carries only its message, so the exception's
    // type name is the one part not reproduced.
    match platform.tm_initialize() {
        Ok(()) => {}
        Err(PlatformError::NotSupported(_)) => {
            log::warn!("Thermal manager is not supported on this platform");
        }
        Err(PlatformError::Backend(what) | PlatformError::NotFound(what)) => {
            log::error!("Caught exception while initializing thermal manager - {what}");
        }
    }
    // Released even when initialize failed.  Python runs its policy loop
    // either way -- it logs the failure and carries on, in the `except`
    // clauses of `thermalctld:ThermalControlDaemon.__init__` -- and a platform
    // with no thermal manager at all is the common case, not the exception.
    // Leaving the flag false would park the policy for the life of the process.
    ready.store(true, Ordering::Release);
}

/// The leak thread: read every sensor, run the state machine, write the tables.
fn leak_thread_main(
    interval: Duration,
    is_switch_bmc: bool,
    which: PlatformImpl,
    shutdown: watch::Receiver<bool>,
    exit_code: Arc<AtomicI32>,
    shutdown_tx: Arc<watch::Sender<bool>>,
) {
    let mut platform = match platform_provider::open(SYSLOG_IDENTIFIER, which) {
        Ok(p) => p,
        Err(e) => {
            log::error!("leak updater: cannot initialise platform: {e}");
            return;
        }
    };
    // Only the three leak tables: this thread writes nothing else, and leak
    // detection is a safety function, so a failure to open them is an error
    // rather than a silent return.
    let tables = match db::LeakTables::open() {
        Ok(t) => t,
        Err(e) => {
            log::error!(
                "leak updater: cannot open the leak tables, \
                         leak monitoring is off: {e:?}"
            );
            return;
        }
    };
    // A STATE_DB this thread cannot write is one the poll loop is about to
    // fail on too -- same redis, same dead connection -- so it takes the whole
    // daemon with it rather than going quiet on its own.  Python's leak thread
    // does the same through `task_stopping_event`, which every other loop in
    // the process is also waiting on.
    let lost = leak_loop(&mut platform, &tables, interval, is_switch_bmc, shutdown);
    report_leak_exit(lost, &exit_code, &shutdown_tx);
}

/// Hand the leak thread's verdict to the rest of the daemon.
///
/// A code is stored only if nothing got there first -- a signal that arrived
/// while the thread was failing is still the reason the daemon is going --
/// and the shutdown is sent either way, so the monitor stops too.
fn report_leak_exit(lost: Option<i32>, exit_code: &AtomicI32, shutdown_tx: &watch::Sender<bool>) {
    if let Some(code) = lost {
        exit_code
            .compare_exchange(0, code, Ordering::SeqCst, Ordering::SeqCst)
            .ok();
        let _ = shutdown_tx.send(true);
    }
}

/// The code the process leaves with.
///
/// A lost STATE_DB takes precedence over the signal's code, and only over a
/// zero one: a daemon that was shut down deliberately and could not write on
/// the way out was still shut down deliberately, while one whose writes
/// started failing has to leave non-zero so supervisord brings it back onto a
/// fresh connection.
fn final_exit_code(signalled: i32, db_lost: Option<i32>) -> i32 {
    match signalled {
        0 => db_lost.unwrap_or(0),
        signalled => signalled,
    }
}

/// The leak thread's body, with the platform and the tables already opened.
///
/// The two steps above it — building a `Platform` and opening the three tables
/// — reach hardware and redis; everything that decides *what is published* is
/// here, where it can be driven.
/// Which slot the daemon publishes under, if any.
///
/// Mirrors the slot choice in `thermalctld:TemperatureUpdater.__init__`: a
/// modular chassis publishes its own slot, a SmartSwitch DPU publishes its DPU
/// id, and anything else publishes neither.  The base class hands back the
/// two separately, so the choice is made here rather than by a platform
/// deciding what "slot" means.
fn slot_or_dpu_id_of(info: &platform_api::ChassisInfo) -> Option<i64> {
    let smartswitch_dpu = info.is_smartswitch && info.is_dpu;
    if info.is_modular_chassis {
        info.my_slot
    } else if smartswitch_dpu {
        info.dpu_id
    } else {
        None
    }
}

fn leak_loop(
    platform: &mut dyn platform_api::PlatformApi,
    tables: &db::LeakTables,
    interval: Duration,
    is_switch_bmc: bool,
    shutdown: watch::Receiver<bool>,
) -> Option<i32> {
    let profiles = match platform.get_leak_profiles() {
        Ok(p) => p,
        Err(e) => {
            log::error!("leak updater: cannot read leak profiles, escalation timers disabled: {e}");
            Vec::new()
        }
    };
    // A seed read that failed is a STATE_DB this thread has lost, and the
    // leak tables are the one thing it exists to keep current.
    if let Err(e) = leak_updater::publish_profiles_and_seed(tables, &profiles) {
        log::error!("leak updater: {e}");
        return Some(ERR_DB_WRITE);
    }

    let mut state = leak_updater::LeakState::new();
    let mut events = event_log::BmcEventLogger::new(is_switch_bmc);
    // Logged once, then again only after a read succeeds: this loop runs every
    // half second on a liquid-cooled box, and a per-cycle error would bury the
    // log.  Same shape as the BMC mirror's `failed` flag.
    let mut read_failed = false;
    let mut db_lost: Option<i32> = None;

    loop {
        if *shutdown.borrow() {
            break;
        }
        // Reading no sensors and failing to read them are different events, and
        // treating the second as the first is how a leak detector goes quiet
        // without anyone noticing -- LIQUID_COOLING_INFO simply stays empty.
        let sensors = match platform.get_leak_sensors() {
            Ok(s) => {
                if read_failed {
                    pmon_common::notice!("leak updater: sensor read recovered");
                    read_failed = false;
                }
                s
            }
            Err(e) => {
                if !read_failed {
                    log::error!("leak updater: cannot read leak sensors, leak detection is not running: {e}");
                    read_failed = true;
                }
                Vec::new()
            }
        };
        let outcome = state.refresh(&sensors, &profiles, std::time::Instant::now(), &mut events);
        let unwritten = leak_updater::apply(tables, &outcome);
        // Forgotten before leaving, not instead of leaving: the cache has to
        // agree with what actually reached STATE_DB either way, and a future
        // change that made this path recoverable would otherwise republish
        // nothing.
        state.forget(&unwritten);
        if !unwritten.is_empty() {
            // Python does not reach a second cycle here either, but it does
            // not leave: the `set` in
            // `thermalctld:LiquidCoolingUpdater._refresh_leak_status` is
            // unwrapped, the exception ends the updater thread, and leak
            // detection goes quiet while the daemon stays up (see
            // `ERR_DB_WRITE`).  Leaving is what gets it restarted onto a
            // working connection.
            log::error!("leak updater: STATE_DB refused a write, stopping the daemon");
            db_lost = Some(ERR_DB_WRITE);
            break;
        }
        if sleep_or_shutdown(interval, &shutdown) {
            break;
        }
    }
    // The rows this thread owns go with it.  See `LeakTables::clear` for what
    // leaving them behind reads as downstream -- the short version is that a
    // stale `leaking=No` is indistinguishable from a live one.
    tables.clear();
    log::info!("leak updater stopped");
    db_lost
}

/// Sleep for `interval`, returning early — and `true` — if shutdown is asked
/// for meanwhile.
///
/// `main` joins this thread *before* it calls `deinitialize()`, so an
/// uninterruptible sleep here holds up the `hw-management-tc` restore for as
/// long as the interval.  `liquid_cooling_update_interval` is validated only as
/// `0 < v <= u32::MAX`, so a value from `pmon_daemon_control.json` could hold it
/// up for hours; slicing the wait bounds that by `SLICE` whatever the interval.
fn sleep_or_shutdown(interval: Duration, shutdown: &watch::Receiver<bool>) -> bool {
    const SLICE: Duration = Duration::from_millis(100);
    let deadline = std::time::Instant::now() + interval;
    loop {
        if *shutdown.borrow() {
            return true;
        }
        let now = std::time::Instant::now();
        if now >= deadline {
            return false;
        }
        std::thread::sleep(SLICE.min(deadline - now));
    }
}

/// Whether to run the leak thread.
///
/// Either the flag from pmon_daemon_control.json or the platform saying so is
/// enough, which is how Python gates it in
/// `thermalctld:ThermalControlDaemon.__init__`: the flag is checked first and
/// the platform is only asked when it is unset.  An `and` here would silently
/// disable leak detection on a box whose flag is set but whose
/// `is_liquid_cooled()` answers False -- SN6600_LD is such a box, and leak
/// detection is a safety function.
fn leak_enabled(flag: bool, chassis: Option<&platform_api::ChassisInfo>) -> bool {
    flag || chassis.is_some_and(|i| i.is_liquid_cooled)
}

// ── Tests ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    /// SN6600_LD sets the flag in pmon_daemon_control.json and answers False
    /// to is_liquid_cooled(), so an `and` here disables leak detection on a
    /// liquid-cooled switch.  Either source alone has to be enough.
    #[test]
    fn either_the_flag_or_the_platform_turns_leak_detection_on() {
        let cooled = ChassisInfo { is_liquid_cooled: true, ..Default::default() };
        let plain = ChassisInfo { is_liquid_cooled: false, ..Default::default() };

        assert!(leak_enabled(true, Some(&plain)), "the flag alone is enough");
        assert!(leak_enabled(false, Some(&cooled)), "the platform alone is enough");
        assert!(leak_enabled(true, Some(&cooled)));
        assert!(leak_enabled(true, None), "the flag stands without a chassis");

        assert!(!leak_enabled(false, Some(&plain)), "neither source says so");
        assert!(!leak_enabled(false, None));
    }

    /// The supervisord template copies these values out of
    /// pmon_daemon_control.json without checking them, so a bad one must be
    /// named at startup rather than panicking inside Duration on the first
    /// sleep.
    #[test]
    fn an_unusable_interval_is_rejected_by_name() {
        for bad in ["-1", "0", "inf", "NaN", "1e30", "abc", ""] {
            assert!(positive_secs(bad).is_err(), "{bad} must be rejected");
        }
        assert_eq!(positive_secs("0.5"), Ok(0.5));
        assert_eq!(positive_secs("60"), Ok(60.0));
    }

    /// The elapsed-time threshold is compared against, not slept on, so zero is
    /// a usable value meaning "warn about every cycle".
    #[test]
    fn zero_is_a_usable_threshold_but_not_a_usable_interval() {
        assert_eq!(non_negative_secs("0"), Ok(0.0));
        assert!(non_negative_secs("-1").is_err());
        assert!(positive_secs("0").is_err());
    }

    /// clap must actually apply the parsers, not just define them.
    #[test]
    fn clap_rejects_a_bad_interval_on_the_command_line() {
        use clap::Parser;
        assert!(Args::try_parse_from(["thermalctld-rs", "--liquid_cooling_update_interval", "-0.5"]).is_err());
        let ok = Args::try_parse_from([
            "thermalctld-rs",
            "--liquid_cooling_update_interval",
            "0.5",
            "--thermal-monitor-update-interval",
            "60",
        ])
        .unwrap();
        assert_eq!(ok.liquid_cooling_update_interval, 0.5);
        assert_eq!(ok.thermal_monitor_update_interval, 60.0);
    }

    // ── The leak thread's body ────────────────────────────────────────────

    use crate::db::mock::MockLeak;
    use platform_api::{ChassisInfo, ChassisInfoCols, FanDrawerInfo, FanDrawerInfoCols, FanInfo, FanInfoCols, LeakProfile, LeakSensorInfo, PlatformError, ThermalInfo};

    struct FakePlatform {
        profiles: Vec<LeakProfile>,
        sensors: Vec<LeakSensorInfo>,
    }

    impl platform_api::PlatformApi for FakePlatform {
        fn get_chassis_info(&mut self, _cols: ChassisInfoCols) -> Result<ChassisInfo, PlatformError> {
            Ok(ChassisInfo::default())
        }
        fn get_thermals(&mut self) -> Result<Vec<ThermalInfo>, PlatformError> {
            Ok(Vec::new())
        }
        fn get_fan_drawers(&mut self, _cols: FanDrawerInfoCols) -> Result<Vec<FanDrawerInfo>, PlatformError> {
            Ok(Vec::new())
        }
        fn get_fans(&mut self, _cols: FanInfoCols) -> Result<Vec<FanInfo>, PlatformError> {
            Ok(Vec::new())
        }
        fn set_fan_led(&mut self, _: &str, _: platform_api::LedColor) -> Result<(), PlatformError> {
            Ok(())
        }
        fn get_leak_profiles(&mut self) -> Result<Vec<LeakProfile>, PlatformError> {
            Ok(self.profiles.clone())
        }
        fn get_leak_sensors(&mut self) -> Result<Vec<LeakSensorInfo>, PlatformError> {
            Ok(self.sensors.clone())
        }
    }

    fn sensor(name: &str, is_leak: bool) -> LeakSensorInfo {
        LeakSensorInfo {
            name: name.to_string(),
            parent_name: "chassis 1".to_string(),
            is_ok: Some(true),
            is_leak: Some(is_leak),
            severity: None,
            profile_type: None,
            profile_max_minor_duration_sec: None,
            sensor_type: Some("leakage".to_string()),
            location: Some("chassis".to_string()),
        }
    }

    /// Before its first poll the leak thread publishes the profiles and seeds
    /// `SYSTEM_LEAK_STATUS|system`, so a consumer reading between start-up and
    /// the first cycle sees a status rather than nothing.  The per-sensor rows
    /// come from the first poll, not from the seeding.
    #[test]
    fn the_leak_loop_seeds_the_system_status_before_its_first_poll() {
        let m = MockLeak::new();
        let mut p = FakePlatform {
            profiles: vec![LeakProfile {
                r#type: "chassis".to_string(),
                max_minor_duration_sec: None,
            }],
            sensors: vec![sensor("leakage1", false)],
        };

        // Shut down before the first poll: what is in the tables is exactly
        // what the seeding step put there.
        let (tx, rx) = watch::channel(false);
        tx.send(true).unwrap();
        leak_loop(&mut p, &m.tables, Duration::from_millis(1), false, rx);

        // Against the write history rather than the surviving rows: the thread
        // clears its tables on the way out (bug 16), so what is left says
        // nothing about what the seeding step published.
        assert!(
            m.profile.wrote("chassis", "max_minor_duration_sec"),
            "a profile with no escalation timer publishes Python's inf"
        );
        assert!(m.system.wrote("system", "device_leak_status"));
        assert!(m.sensor.writes().is_empty(), "the sensor rows wait for the first poll");
    }

    /// Seeding happens *only* when the status is absent.  A daemon restarting
    /// while a leak is standing must not clear it — the water is still there,
    /// and a cleared status is an alarm that silently went away.
    #[test]
    fn a_restart_does_not_clear_a_standing_leak_status() {
        let m = MockLeak::new();
        crate::db::TableLike::set(&m.system, "system", &[("device_leak_status", "CRITICAL".to_string())]).unwrap();

        let mut p = FakePlatform {
            profiles: Vec::new(),
            sensors: Vec::new(),
        };
        // The setup above is itself a write, so the question is whether the
        // seed added one -- not whether the field was ever written.
        let before = m.system.writes().len();

        let (tx, rx) = watch::channel(false);
        tx.send(true).unwrap();
        leak_loop(&mut p, &m.tables, Duration::from_millis(1), false, rx);

        // The invariant is about *seeding*, not teardown: start-up must not
        // overwrite a standing alarm.  A clean stop does clear the row -- that
        // is what `thermalctld:LiquidCoolingUpdater.__del__` does too -- so
        // the check is that the seed wrote nothing, not that the row survived.
        assert_eq!(
            m.system.writes().len(),
            before,
            "seeding must not overwrite a standing CRITICAL"
        );
    }

    /// One pass reaches the tables with the sensors' actual state.
    #[test]
    fn one_leak_pass_publishes_what_the_sensors_report() {
        let m = MockLeak::new();
        let mut p = FakePlatform {
            profiles: Vec::new(),
            sensors: vec![sensor("leakage1", true)],
        };

        // A receiver that reports "keep going" once and then shuts down: the
        // loop checks the flag at the top, so sending after construction lets
        // exactly one pass run.
        let (tx, rx) = watch::channel(false);
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(20));
            let _ = tx.send(true);
        });
        leak_loop(&mut p, &m.tables, Duration::from_millis(1), false, rx);

        assert!(
            m.sensor.wrote("leakage1", "leaking"),
            "the leak reached the table"
        );
    }

    /// A leak table that refuses a write stops the thread and hands back the
    /// code the process is to leave with.
    ///
    /// Leak detection is a safety function, so going quiet is the one thing
    /// this thread must not do -- and quiet is what carrying on would be, with
    /// `DbConnector` never reconnecting.  Quiet is what Python does: the `set`
    /// in `thermalctld:LiquidCoolingUpdater._refresh_leak_status` is unwrapped
    /// and ends the thread, and nothing ends the daemon.
    #[test]
    fn a_leak_table_that_refuses_a_write_stops_the_daemon() {
        let m = MockLeak::new();
        let mut p = FakePlatform { profiles: Vec::new(), sensors: vec![sensor("leakage1", true)] };
        m.sensor.fail_writes("redis is gone");

        // No shutdown is sent: the loop has to leave on the write failure
        // alone, which is what a hung test here would be telling us.
        let (_tx, rx) = watch::channel(false);
        let code = leak_loop(&mut p, &m.tables, Duration::from_millis(1), false, rx);

        assert_eq!(code, Some(ERR_DB_WRITE), "a lost STATE_DB is a non-zero exit");
    }

    /// A seed read that fails is a STATE_DB this thread has lost, and it stops
    /// before the loop: reading the failure as "not seeded yet" would
    /// overwrite a standing leak alarm with `None`.
    #[test]
    fn an_unreadable_leak_status_stops_the_daemon_before_the_loop() {
        let m = MockLeak::new();
        let mut p = FakePlatform { profiles: Vec::new(), sensors: vec![sensor("leakage1", true)] };
        m.system.fail_reads("redis is gone");
        let (_tx, rx) = watch::channel(false);
        let code = leak_loop(&mut p, &m.tables, Duration::from_millis(1), false, rx);
        assert_eq!(code, Some(ERR_DB_WRITE));
        assert!(m.sensor.writes().is_empty(), "no sensor was published");
    }

    /// The leak thread's verdict reaches the rest of the daemon: its code when
    /// nothing else has set one, and a shutdown the monitor is waiting on.
    #[test]
    fn a_leak_thread_that_lost_its_tables_stops_the_daemon() {
        let code = AtomicI32::new(0);
        let (tx, rx) = watch::channel(false);
        report_leak_exit(Some(ERR_DB_WRITE), &code, &tx);
        assert_eq!(code.load(Ordering::SeqCst), ERR_DB_WRITE);
        assert!(*rx.borrow(), "the monitor is told to stop");
    }

    /// A signal that got there first is still the reason the daemon leaves.
    #[test]
    fn a_leak_thread_does_not_overwrite_a_signal() {
        let code = AtomicI32::new(143);
        let (tx, _rx) = watch::channel(false);
        report_leak_exit(Some(ERR_DB_WRITE), &code, &tx);
        assert_eq!(code.load(Ordering::SeqCst), 143);
    }

    /// A leak thread that stopped because it was told to says nothing.
    #[test]
    fn a_leak_thread_that_was_told_to_stop_leaves_everything_alone() {
        let code = AtomicI32::new(0);
        let (tx, rx) = watch::channel(false);
        report_leak_exit(None, &code, &tx);
        assert_eq!(code.load(Ordering::SeqCst), 0);
        assert!(!*rx.borrow());
    }

    /// The exit code: a signal's own code wins, and a lost database turns
    /// what would have been a clean exit into one supervisord restarts.
    #[test]
    fn a_lost_database_turns_a_clean_exit_into_a_restart() {
        assert_eq!(final_exit_code(0, None), 0);
        assert_eq!(final_exit_code(0, Some(ERR_DB_WRITE)), ERR_DB_WRITE);
        assert_eq!(final_exit_code(143, Some(ERR_DB_WRITE)), 143);
        assert_eq!(final_exit_code(143, None), 143);
    }

    /// A platform with no leak sensors publishes no rows at all, rather than an
    /// empty-but-present table — an air-cooled box has nothing to say here.
    #[test]
    fn a_platform_with_no_leak_sensors_publishes_nothing() {
        let m = MockLeak::new();
        let mut p = FakePlatform {
            profiles: Vec::new(),
            sensors: Vec::new(),
        };
        let (tx, rx) = watch::channel(false);
        tx.send(true).unwrap();
        leak_loop(&mut p, &m.tables, Duration::from_millis(1), false, rx);

        assert!(m.sensor.writes().is_empty());
        assert!(m.profile.writes().is_empty());
        assert!(
            !m.system.writes().is_empty(),
            "the system row is seeded regardless, so the field always exists"
        );
    }

    // ── the daemon body, which used to be inside main ────────────────────────

    use crate::db::mock::MockDb;
    use pmon_common::db::TableLike;

    /// A platform that records the thermal-manager calls and can refuse them.
    #[derive(Default)]
    struct Tm {
        calls: std::sync::Arc<std::sync::Mutex<Vec<&'static str>>>,
        refuse: bool,
    }

    /// A thermal-manager start that runs inline on a second handle sharing the
    /// first one's call log.  That is what the real one does -- two platform
    /// handles over one @classmethod-backed manager -- so `initialize` still
    /// lands in the same list as the `deinitialize` the daemon makes on its own
    /// handle, and the pairing stays visible to the assertions below.
    fn tm_init_sharing(
        calls: std::sync::Arc<std::sync::Mutex<Vec<&'static str>>>,
        refuse: bool,
    ) -> impl Fn(Arc<AtomicBool>) -> std::io::Result<std::thread::JoinHandle<()>> {
        move |ready| {
            let mut second = Tm { calls: std::sync::Arc::clone(&calls), refuse };
            let _ = second.tm_initialize();
            ready.store(true, Ordering::Release);
            std::thread::Builder::new().spawn(|| {})
        }
    }

    /// A manager that comes up without being watched, for the tests that are
    /// about something else.
    fn tm_up(ready: Arc<AtomicBool>) -> std::io::Result<std::thread::JoinHandle<()>> {
        ready.store(true, Ordering::Release);
        std::thread::Builder::new().spawn(|| {})
    }

    impl PlatformApi for Tm {
        fn tm_initialize(&mut self) -> Result<(), PlatformError> {
            self.calls.lock().unwrap().push("initialize");
            self.answer()
        }
        fn tm_deinitialize(&mut self) -> Result<(), PlatformError> {
            self.calls.lock().unwrap().push("deinitialize");
            self.answer()
        }
        fn tm_get_interval(&mut self) -> Result<Option<f64>, PlatformError> {
            Err(PlatformError::NotSupported("tm_get_interval".into()))
        }
        fn tm_run_policy(&mut self) -> Result<(), PlatformError> {
            self.answer()
        }
    }

    impl Tm {
        fn answer(&self) -> Result<(), PlatformError> {
            if self.refuse {
                return Err(PlatformError::Backend("no thermal manager".into()));
            }
            Ok(())
        }
    }

    fn args_from(extra: &[&str]) -> Args {
        let mut argv = vec!["thermalctld"];
        argv.extend_from_slice(extra);
        Args::try_parse_from(argv).expect("the daemon's own defaults must parse")
    }

    /// Already stopping, so the poll loop returns on its first select and what
    /// is left to observe is the order of everything around it.
    fn stopped() -> watch::Receiver<bool> {
        let (tx, rx) = watch::channel(false);
        tx.send(true).unwrap();
        rx
    }

    /// The order is the behaviour.  The manager is started before the loop and
    /// put back after it, and the rows go last -- a `clear()` before the leak
    /// thread is joined leaves that thread republishing into a table the
    /// daemon has already emptied, which reads downstream as live data from a
    /// daemon that is no longer running.
    #[tokio::test]
    async fn the_daemon_starts_the_thermal_manager_and_puts_it_back() {
        let mut p = Tm::default();
        let calls = std::sync::Arc::clone(&p.calls);
        let m = MockDb::new(false);
        m.temperature.set("ASIC", &[("temperature", "42".to_string())]).unwrap();

        let mut monitor = Monitor::with_env(
            0.001, 0.001, 30.0, crate::polling::PollingIntervals::default(), false);

        let start_tm = tm_init_sharing(std::sync::Arc::clone(&calls), false);
        run(
            &mut p,
            &m.db,
            &mut monitor,
            &args_from(&[]),
            None,
            &start_tm,
            &|_i, _rx| panic!("leak detection is off by default"),
            stopped(),
        )
        .await;

        assert_eq!(*calls.lock().unwrap(), vec!["initialize", "deinitialize"]);
        assert!(
            m.temperature.get("ASIC").unwrap().is_none(),
            "the rows this daemon owns go with it -- 60 were left behind on an SN5640 before"
        );
    }

    /// A platform with no thermal manager is not an error.  Most platforms
    /// have none, and refusing to run the poll loop without one would leave
    /// them with no temperature or fan table at all.
    #[tokio::test]
    async fn a_platform_with_no_thermal_manager_still_polls_and_still_clears() {
        let mut p = Tm { refuse: true, ..Default::default() };
        let calls = std::sync::Arc::clone(&p.calls);
        let m = MockDb::new(false);
        m.temperature.set("ASIC", &[("temperature", "42".to_string())]).unwrap();
        let mut monitor = Monitor::with_env(
            0.001, 0.001, 30.0, crate::polling::PollingIntervals::default(), false);

        let start_tm = tm_init_sharing(std::sync::Arc::clone(&calls), true);
        run(&mut p, &m.db, &mut monitor, &args_from(&[]), None, &start_tm,
            &|_i, _rx| panic!("leak detection is off"), stopped()).await;

        assert_eq!(*calls.lock().unwrap(), vec!["initialize", "deinitialize"]);
        assert!(m.temperature.get("ASIC").unwrap().is_none());
    }

    /// The thermal manager is started off the poll path and joined before it
    /// is put back.
    ///
    /// Both halves matter.  On SPC1 `tm_initialize()` sits in mlnx's 300 s
    /// `unlink_hw_mgmt_thermal_files()` wait on every restart -- it waits for
    /// sysfs links its own previous run unlinked -- so starting it inline left
    /// FAN_INFO, FAN_DRAWER_INFO and TEMPERATURE_INFO unpublished for five
    /// minutes after each `supervisorctl restart thermalctld`.  And a
    /// `deinitialize()` that raced a still-running `initialize()` would stop a
    /// manager that had not started.
    #[tokio::test]
    async fn the_thermal_manager_starts_off_the_poll_path_and_is_joined() {
        let mut p = Tm::default();
        let calls = std::sync::Arc::clone(&p.calls);
        let m = MockDb::new(false);
        let mut monitor = Monitor::with_env(
            0.001, 0.001, 30.0, crate::polling::PollingIntervals::default(), false);

        let joined = std::sync::Arc::new(AtomicBool::new(false));
        let did_join = std::sync::Arc::clone(&joined);
        let seen = std::sync::Arc::clone(&calls);
        let start_tm = move |ready: Arc<AtomicBool>| {
            let mut second = Tm { calls: std::sync::Arc::clone(&seen), refuse: false };
            let f = std::sync::Arc::clone(&did_join);
            std::thread::Builder::new().spawn(move || {
                let _ = second.tm_initialize();
                ready.store(true, Ordering::Release);
                f.store(true, Ordering::SeqCst);
            })
        };

        run(
            &mut p,
            &m.db,
            &mut monitor,
            &args_from(&[]),
            None,
            &start_tm,
            &|_i, _rx| panic!("leak detection is off by default"),
            stopped(),
        )
        .await;

        assert!(joined.load(Ordering::SeqCst), "the init thread has to be joined");
        // Still paired, though the two calls were made on different handles:
        // `ThermalManager`'s methods are @classmethod, so both reach the same
        // Python class object.
        assert_eq!(*calls.lock().unwrap(), vec!["initialize", "deinitialize"]);
    }

    /// A manager that refused still releases the policy.
    ///
    /// This is the half with teeth.  Most platforms have no thermal manager at
    /// all, so `tm_initialize()` answering an error is the common case, not the
    /// exception -- and a flag left false there would park `run_policy` for the
    /// life of the process on every one of them.
    #[test]
    fn a_manager_that_refused_still_releases_the_policy() {
        let mut refusing = Tm { refuse: true, ..Default::default() };
        let calls = std::sync::Arc::clone(&refusing.calls);
        let ready = AtomicBool::new(false);

        tm_init_with(&mut refusing, &ready);

        assert_eq!(*calls.lock().unwrap(), vec!["initialize"], "it still has to be asked");
        assert!(
            ready.load(Ordering::Acquire),
            "a manager that refused still has to let the policy run"
        );
    }

    /// A refusal is logged as Python logs it: an exception at ERR, a platform
    /// that does not implement the manager at WARNING, each in the words of
    /// its `except` in `thermalctld:ThermalControlDaemon.__init__`.
    #[test]
    fn a_refused_manager_is_logged_in_pythons_words() {
        let log = pmon_common::logging::capture();
        let mut refusing = Tm { refuse: true, ..Default::default() };
        tm_init_with(&mut refusing, &AtomicBool::new(false));
        assert!(log.logged(
            log::Level::Error,
            "Caught exception while initializing thermal manager - no thermal manager"
        ));

        struct Unsupported;
        impl PlatformApi for Unsupported {
            fn tm_initialize(&mut self) -> Result<(), PlatformError> {
                Err(PlatformError::NotSupported("tm_initialize".into()))
            }
        }
        tm_init_with(&mut Unsupported, &AtomicBool::new(false));
        assert!(log.logged(log::Level::Warn, "Thermal manager is not supported on this platform"));
    }

    /// And so does one that started.
    #[test]
    fn a_manager_that_started_releases_the_policy() {
        let mut started = Tm::default();
        let ready = AtomicBool::new(false);

        tm_init_with(&mut started, &ready);

        assert!(ready.load(Ordering::Acquire));
    }

    /// An init thread that will not start leaves the daemon running rather
    /// than parking the policy on a flag nobody will ever set.
    #[tokio::test]
    async fn an_init_thread_that_will_not_start_does_not_park_the_policy() {
        let log = pmon_common::logging::capture();
        let m = MockDb::new(false);
        let mut monitor = Monitor::with_env(
            0.001, 0.001, 30.0, crate::polling::PollingIntervals::default(), false);
        m.temperature.set("ASIC", &[("temperature", "42".to_string())]).unwrap();

        run(
            &mut Tm::default(),
            &m.db,
            &mut monitor,
            &args_from(&[]),
            None,
            &|_ready| Err(std::io::Error::other("no threads left")),
            &|_i, _rx| panic!("leak detection is off by default"),
            stopped(),
        )
        .await;

        assert!(log.logged(log::Level::Error, "thermal manager init thread"));
        assert!(m.temperature.get("ASIC").unwrap().is_none(), "the loop still ran and still cleared");
    }

    /// The leak thread is started only where leak detection is on, it is
    /// handed the interval the control file asked for, and it is joined before
    /// the daemon puts the platform back.
    #[tokio::test]
    async fn the_leak_thread_is_started_with_its_own_interval_and_joined() {
        let log = pmon_common::logging::capture();
        let started = std::sync::Arc::new(std::sync::Mutex::new(Vec::<Duration>::new()));
        let seen = std::sync::Arc::clone(&started);
        let joined = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let did_join = std::sync::Arc::clone(&joined);

        let m = MockDb::new(false);
        let mut monitor = Monitor::with_env(
            0.001, 0.001, 30.0, crate::polling::PollingIntervals::default(), false);

        run(
            &mut Tm::default(),
            &m.db,
            &mut monitor,
            &args_from(&["--enable_liquid_cooling", "--liquid_cooling_update_interval", "0.25"]),
            None,
            &tm_up,
            &|interval, _rx| {
                seen.lock().unwrap().push(interval);
                let f = std::sync::Arc::clone(&did_join);
                std::thread::Builder::new().spawn(move || {
                    f.store(true, std::sync::atomic::Ordering::SeqCst);
                })
            },
            stopped(),
        )
        .await;

        assert_eq!(*started.lock().unwrap(), vec![Duration::from_millis(250)]);
        assert!(joined.load(std::sync::atomic::Ordering::SeqCst), "the thread must be joined");
        // Python announces both at NOTICE and an operator reads the pair as
        // "leak detection is on"; saying only that it stopped leaves the far
        // more important half to be inferred from silence.
        assert!(log.contains("Started thread for liquid cooling updater"));
        assert!(log.contains("Joined thread for liquid cooling updater"));
    }

    /// Leak detection is a safety function, so a thread that will not start is
    /// reported and the rest of the daemon carries on: temperature and fan
    /// monitoring are still worth having on a switch whose leak thread failed.
    #[tokio::test]
    async fn a_leak_thread_that_will_not_start_does_not_stop_the_daemon() {
        let log = pmon_common::logging::capture();
        let mut p = Tm::default();
        let calls = std::sync::Arc::clone(&p.calls);
        let m = MockDb::new(false);
        let mut monitor = Monitor::with_env(
            0.001, 0.001, 30.0, crate::polling::PollingIntervals::default(), false);

        run(
            &mut p,
            &m.db,
            &mut monitor,
            &args_from(&["--enable_liquid_cooling"]),
            None,
            &tm_init_sharing(std::sync::Arc::clone(&calls), false),
            &|_i, _rx| Err(std::io::Error::other("no threads left")),
            stopped(),
        )
        .await;

        assert_eq!(*calls.lock().unwrap(), vec!["initialize", "deinitialize"]);
        assert!(
            log.logged(log::Level::Error, "leak monitoring is off"),
            "a safety function that did not start has to say so"
        );
    }

    /// And a liquid-cooled platform turns it on without the flag: SN6600_LD
    /// sets the flag and answers False, and other platforms will do the
    /// reverse, so either source alone has to be enough all the way through
    /// the wiring and not only in `leak_enabled`.
    #[tokio::test]
    async fn the_platform_alone_turns_the_leak_thread_on() {
        let started = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let n = std::sync::Arc::clone(&started);
        let m = MockDb::new(false);
        let mut monitor = Monitor::with_env(
            0.001, 0.001, 30.0, crate::polling::PollingIntervals::default(), false);
        let cooled = ChassisInfo { is_liquid_cooled: true, ..Default::default() };

        run(
            &mut Tm::default(),
            &m.db,
            &mut monitor,
            &args_from(&[]),
            Some(&cooled),
            &tm_up,
            &|_i, _rx| {
                n.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                std::thread::Builder::new().spawn(|| {})
            },
            stopped(),
        )
        .await;

        assert_eq!(started.load(std::sync::atomic::Ordering::SeqCst), 1);
    }

    // ── the leak thread, and which id the daemon publishes under ─────────────

    /// A modular chassis publishes its own slot, a SmartSwitch DPU publishes
    /// its DPU id, and anything else publishes neither.  The base class hands
    /// the two back separately, so getting this wrong opens
    /// `TEMPERATURE_INFO_{n}` under the other machine's number -- a table
    /// nobody reads, on a database the daemon then keeps writing to.
    #[test]
    fn the_suffix_is_the_slot_on_a_chassis_and_the_dpu_id_on_a_dpu() {
        let chassis = ChassisInfo {
            is_modular_chassis: true,
            my_slot: Some(4),
            dpu_id: Some(9),
            ..Default::default()
        };
        assert_eq!(slot_or_dpu_id_of(&chassis), Some(4), "a chassis is its slot");

        let dpu = ChassisInfo {
            is_smartswitch: true,
            is_dpu: true,
            dpu_id: Some(3),
            my_slot: Some(7),
            ..Default::default()
        };
        assert_eq!(slot_or_dpu_id_of(&dpu), Some(3), "a DPU is its dpu id");

        // An NPU is a SmartSwitch but not a DPU, and a fixed switch is neither.
        let npu = ChassisInfo { is_smartswitch: true, my_slot: Some(7), ..Default::default() };
        assert_eq!(slot_or_dpu_id_of(&npu), None);
        assert_eq!(slot_or_dpu_id_of(&ChassisInfo::default()), None);
    }

    /// A platform whose leak reads can be made to fail and then recover.
    ///
    /// Given a `stop` sender it runs the whole sequence itself, on the loop's
    /// own thread: the first read fails and clears its own failure, the second
    /// succeeds and asks the loop to stop.  That ordering used to come from a
    /// ticker thread which watched `reads` and had to land its store between
    /// read one and read two -- a window one poll interval wide.  On a loaded
    /// arm64 builder the thread was not scheduled inside it, both reads failed,
    /// and the recovery the test is named after was never logged.  A sequence
    /// that has to be ordered is ordered here rather than raced for.
    #[derive(Default)]
    struct Leaky {
        profiles_fail: bool,
        sensors_fail: std::sync::Arc<std::sync::atomic::AtomicBool>,
        reads: std::sync::Arc<std::sync::atomic::AtomicUsize>,
        stop: Option<watch::Sender<bool>>,
    }

    impl PlatformApi for Leaky {
        fn get_leak_profiles(
            &mut self,
        ) -> Result<Vec<platform_api::LeakProfile>, platform_api::PlatformError> {
            if self.profiles_fail {
                return Err(platform_api::PlatformError::Backend("no profiles".into()));
            }
            Ok(Vec::new())
        }
        fn get_leak_sensors(
            &mut self,
        ) -> Result<Vec<platform_api::LeakSensorInfo>, platform_api::PlatformError> {
            self.reads.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            if self.sensors_fail.load(std::sync::atomic::Ordering::SeqCst) {
                // Recover on the next call rather than on another thread's
                // timing.  Only when a `stop` was given: without one this is
                // the plain mock the other tests use.
                if self.stop.is_some() {
                    self.sensors_fail
                        .store(false, std::sync::atomic::Ordering::SeqCst);
                }
                return Err(platform_api::PlatformError::Backend("sysfs gone".into()));
            }
            if let Some(tx) = self.stop.take() {
                let _ = tx.send(true);
            }
            Ok(Vec::new())
        }
    }

    fn leak_tables() -> db::LeakTables {
        db::LeakTables::open_with(Box::new(|_db: &str, _t: &str| {
            Ok(Box::new(pmon_common::db::MockTable::new()) as Box<dyn TableLike>)
        }))
        .unwrap()
    }

    /// Reading no sensors and failing to read them are different events, and
    /// treating the second as the first is how a leak detector goes quiet
    /// without anyone noticing: LIQUID_COOLING_INFO simply stays empty.  The
    /// failure is said once and the recovery is said once -- this loop runs
    /// twice a second on a liquid-cooled box, and a per-cycle error would bury
    /// the log it is supposed to stand out in.
    #[test]
    fn a_leak_read_that_fails_is_said_once_and_its_recovery_is_said_once() {
        let log = pmon_common::logging::capture();
        let tables = leak_tables();
        let (tx, rx) = watch::channel(false);
        let reads = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let mut p = Leaky {
            profiles_fail: true,
            sensors_fail: std::sync::Arc::new(std::sync::atomic::AtomicBool::new(true)),
            reads: std::sync::Arc::clone(&reads),
            stop: Some(tx),
        };

        leak_loop(&mut p, &tables, Duration::from_millis(1), false, rx);

        // The sequence itself, not just its log: one read that failed and one
        // that did not. Without this the assertions below could all pass on a
        // run that never reached the recovery.
        assert_eq!(
            reads.load(std::sync::atomic::Ordering::SeqCst),
            2,
            "one failed read, then one that recovered"
        );
        assert!(log.logged(log::Level::Error, "cannot read leak profiles"),
                "profiles that cannot be read disable the escalation timers, loudly");
        assert!(log.logged(log::Level::Error, "leak detection is not running"));
        assert!(log.contains("sensor read recovered"));
        assert!(log.contains("leak updater stopped"));
    }

    /// The wait is sliced so a shutdown is not held up by it.  `run` joins this
    /// thread before it calls `deinitialize()`, and the interval comes from
    /// pmon_daemon_control.json validated only as `0 < v <= u32::MAX` -- an
    /// uninterruptible sleep here could hold the hw-management-tc restore for
    /// hours on a file somebody typo'd.
    #[test]
    fn a_shutdown_does_not_wait_out_the_leak_interval() {
        let (tx, rx) = watch::channel(false);
        tx.send(true).unwrap();
        let began = std::time::Instant::now();
        assert!(sleep_or_shutdown(Duration::from_secs(3600), &rx), "it stops, and says so");
        assert!(began.elapsed() < Duration::from_secs(1), "and it does not wait an hour first");

        // And with no shutdown asked for it sleeps the interval out and says
        // it did not stop.
        let (_tx, rx) = watch::channel(false);
        assert!(!sleep_or_shutdown(Duration::from_millis(1), &rx));
    }

    /// Bug 16: the leak thread must take its rows with it.
    ///
    /// `StateDb::clear` covers the fan and temperature tables; the three leak
    /// tables are deliberately not part of `StateDb` -- the leak thread is
    /// their only writer -- and so nothing cleared them.  Python does, from
    /// `thermalctld:LiquidCoolingUpdater.__del__`.
    ///
    /// Measured on slm-31: stopping thermalctld left `LIQUID_COOLING_INFO|
    /// leakage1`, `|leakage2` and `SYSTEM_LEAK_STATUS|system` behind under
    /// Rust and nothing under Python.  A stale `leaking=No` reads exactly like
    /// a live one to `leakageshow`, to `show platform leak status`, and to
    /// system-health -- so a switch whose leak detection has stopped looks
    /// like one that is monitoring and finding nothing.
    #[tokio::test]
    async fn the_leak_thread_takes_its_rows_with_it() {
        use pmon_common::db::MockTable;

        let (sensor, system, profile) = (MockTable::new(), MockTable::new(), MockTable::new());
        let (s2, sy2, p2) = (sensor.clone(), system.clone(), profile.clone());
        let mut made = 0;
        let tables = db::LeakTables::open_with(Box::new(move |_db: &str, _t: &str| {
            made += 1;
            Ok(match made {
                1 => Box::new(s2.clone()) as Box<dyn TableLike>,
                2 => Box::new(sy2.clone()) as Box<dyn TableLike>,
                _ => Box::new(p2.clone()) as Box<dyn TableLike>,
            })
        }))
        .unwrap();

        sensor.set("leakage1", &[("leaking", "No".to_string())]).unwrap();
        sensor.set("leakage2", &[("leaking", "No".to_string())]).unwrap();
        system.set("system", &[("device_leak_status", "None".to_string())]).unwrap();
        profile.set("rack", &[("type", "rack".to_string())]).unwrap();

        // A shutdown already asked for, so the loop leaves on its first check.
        let (tx, rx) = watch::channel(false);
        tx.send(true).unwrap();
        leak_loop(&mut Leaky::default(), &tables, Duration::from_millis(1), false, rx);

        assert!(sensor.is_empty(), "LIQUID_COOLING_INFO must not outlive the thread");
        assert!(system.is_empty(), "SYSTEM_LEAK_STATUS is what bmcctld cuts power on");
        assert!(profile.is_empty(), "LEAK_PROFILE goes with them");
    }
}
