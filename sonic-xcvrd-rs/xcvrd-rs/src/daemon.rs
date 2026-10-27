//! xcvrd-rs daemon — process entry, boot orchestration and graceful shutdown.
//!
//! Parses the command-line arguments, then builds the real daemon over the mockable trait
//! seams ([`Hal`] + [`XcvrTableHelper`] over [`crate::db::DbTable`]) and runs the worker
//! threads — the same code paths the Part-B unit tests exercise under mocks. Boot mirrors
//! the reference `DaemonXcvrd.init` → task `start`:
//!   1. Build the logical→physical port map from CONFIG_DB (`get_port_mapping`).
//!   2. Purge stale `TRANSCEIVER_INFO` for absent ports (`remove_stale_transceiver_info`).
//!   3. `SfpStateUpdateTask::init` — publish `TRANSCEIVER_INFO` + seed
//!      `TRANSCEIVER_STATUS_SW` (`status`/`error`/`cmis_state=READY`).
//!   4. Spawn the DOM, CMIS, SFF and SFP-state worker threads.
//!
//! CMIS strings are NUL-padded fixed-width; values are written trimmed of trailing
//! NULs (see [`crate::xcvrd::stringify`]), matching the reference xcvrd whose outputs
//! the e2e suite reads with NULs stripped.

use std::error::Error;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use crate::cmis::cmis_api::{BridgeCmisApi, CmisApi};
use crate::cmis::cmis_manager_task::{CmisApiFactory, CmisManagerTask};
use crate::dom::dom_mgr::{DomInfoUpdateTask, DomThermalInfoUpdateTask};
use crate::env;
use crate::hal::{BridgeHal, Hal};
use crate::sff_mgr::{BridgeSffApi, SffApi, SffApiFactory, SffManagerTask};
use crate::xcvrd::deinit_transceiver_tables;
use crate::xcvrd::remove_stale_transceiver_info;
use crate::xcvrd::sfp_state_update::SfpStateUpdateTask;
use crate::xcvrd_utilities::common::is_fast_reboot_enabled;
use crate::xcvrd_utilities::port_event_helper::get_port_mapping;
use crate::xcvrd_utilities::xcvr_table_helper::XcvrTableHelper;
use crate::xcvrd_utilities::{media_settings_parser, optics_si_parser};

/// Reference `sys.exit(SFP_SYSTEM_ERROR)` (xcvrd.py:83, 1235): the exit code the daemon
/// returns when the SFP state machine gives up (STATE_EXIT with `sfp_error_event` set), so
/// the supervisor restarts it. A graceful SIGTERM/SIGINT shutdown returns 0.
const SFP_SYSTEM_ERROR: i32 = 4;

/// supervisord SIGTERMs `xcvrd` on stop/restart and — if it has not exited after `stopwaitsecs`
/// (10s on this pmon) — escalates to SIGKILL. A shutdown that overruns that window is exactly the
/// SIGKILL crash-loop: the SIGKILL leaves `xcvrd` perpetually STOPPING/STARTING, so the `_pretest`
/// fixture never finds it RUNNING. These two budgets keep every graceful stop comfortably inside
/// the 10s window:
///
/// * [`SHUTDOWN_JOIN_GRACE`] bounds how long the normal shutdown path waits to *join* the worker
///   threads. The common case joins in well under this; a worker wedged in a slow platform/bridge
///   (PyO3) call — the observed hang: unbounded `join()` blocked past 10s → SIGKILL — is left for
///   process-exit to reclaim rather than stalling the stop.
/// * [`HARD_SHUTDOWN_GRACE`] is the ultimate backstop: once a SIGTERM/SIGINT arrives the daemon
///   forces `process::exit(0)` after this delay no matter where it is (main loop, a join, or
///   `deinit`). The reference gets a prompt stop by injecting an async exception into its sleeping
///   worker threads (`raise_exception`); Rust cannot interrupt a thread blocked in a native/PyO3
///   call, so this hard deadline stands in for it. Ordering invariant (unit-tested):
///   `SHUTDOWN_JOIN_GRACE < HARD_SHUTDOWN_GRACE < stopwaitsecs (10s)`.
const SHUTDOWN_JOIN_GRACE: Duration = Duration::from_secs(2);
const HARD_SHUTDOWN_GRACE: Duration = Duration::from_secs(6);

/// Set by the SIGTERM/SIGINT handler so the daemon can shut down gracefully (run `deinit`)
/// instead of being SIGKILLed. The reference `DaemonXcvrd` installs signal handlers that
/// set its stop event and then calls `deinit`; we mirror that here.
static SHUTDOWN_REQUESTED: AtomicBool = AtomicBool::new(false);

/// Why `serve()` returned. The reference `DaemonXcvrd.run` runs the daemon exactly once and
/// then the process EXITS (`main` returns, or `sys.exit(SFP_SYSTEM_ERROR)`); it never loops
/// back into a fresh boot. Distinguishing the outcomes lets [`run`] pick the right exit code
/// and — critically — NOT restart the daemon in-process on a `supervisorctl stop`/`restart`
/// (which would keep the process alive past `stopwaitsecs` and force the supervisor to
/// escalate to SIGKILL, so `xcvrd` never holds a stable RUNNING state).
enum ServeOutcome {
    /// A SIGTERM/SIGINT (e.g. `supervisorctl stop`/`restart`) drove a clean shutdown. Exit 0
    /// so the supervisor sees a graceful stop and (re)starts a fresh process promptly.
    Shutdown,
    /// The presence/identity state machine hit an unrecoverable `STATE_EXIT` with
    /// `sfp_error_event` set. Mirror the reference `sys.exit(SFP_SYSTEM_ERROR)` so the
    /// supervisor restarts the daemon.
    SfpSystemError,
}

/// Async-signal-safe handler: only touches an `AtomicBool` (no allocation / no locks).
extern "C" fn handle_shutdown_signal(_sig: libc::c_int) {
    SHUTDOWN_REQUESTED.store(true, Ordering::SeqCst);
}

/// Install the SIGTERM/SIGINT handler (idempotent). `supervisorctl stop xcvrd` sends
/// SIGTERM; a graceful shutdown must run `deinit` (which clears TRANSCEIVER_STATUS on a
/// normal shutdown) before the supervisor's stopwaitsecs elapses and SIGKILL lands.
fn install_signal_handlers() {
    // SAFETY: `handle_shutdown_signal` is async-signal-safe (atomic store only). The fn
    // item is coerced to an `extern "C"` fn pointer, then to `sighandler_t` (a usize).
    unsafe {
        let handler = handle_shutdown_signal as extern "C" fn(libc::c_int) as libc::sighandler_t;
        libc::signal(libc::SIGTERM, handler);
        libc::signal(libc::SIGINT, handler);
    }
}

/// Hard shutdown backstop. Once a SIGTERM/SIGINT has arrived (`SHUTDOWN_REQUESTED`), force
/// `process::exit(0)` after [`HARD_SHUTDOWN_GRACE`] regardless of where the graceful path is —
/// the main event loop, a worker `join`, or `deinit`. This guarantees the process is gone before
/// supervisord's `stopwaitsecs` SIGKILL lands, which is the root fix for the SIGKILL crash-loop: the
/// observed failure was `stop` overrunning 10s (an unbounded `join()` blocked on a worker wedged
/// in a slow PyO3/bridge call) → SIGKILL → `xcvrd` stuck STOPPING/STARTING. The reference daemon
/// gets a prompt stop by injecting an async exception into its sleeping threads (`raise_exception`);
/// Rust cannot interrupt a thread blocked in a native call, so this hard deadline stands in for it.
///
/// Armed exactly once from [`run`]; it is inert (a cheap 50 ms poll) until a stop signal sets the
/// flag, so it never interferes with normal operation and never fires without a real SIGTERM/SIGINT.
fn spawn_shutdown_enforcer() {
    let _ = std::thread::Builder::new()
        .name("shutdown-enforcer".to_string())
        .spawn(|| {
            while !SHUTDOWN_REQUESTED.load(Ordering::SeqCst) {
                std::thread::sleep(Duration::from_millis(50));
            }
            std::thread::sleep(HARD_SHUTDOWN_GRACE);
            eprintln!("xcvrd-rs: shutdown grace elapsed; forcing exit(0) to beat SIGKILL");
            std::process::exit(0);
        });
}

/// Best-effort join bounded by `deadline`. Returns `true` if the thread finished and was joined,
/// `false` if the deadline elapsed first (the thread is left running for process-exit to reclaim).
///
/// Shutdown must never block unboundedly on `join()`: a worker wedged in a native/PyO3 bridge call
/// would hold the stop past supervisord's `stopwaitsecs` and force a SIGKILL (the SIGKILL crash-loop).
/// All worker joins share one absolute `deadline` so the *total* stop wait is bounded by
/// [`SHUTDOWN_JOIN_GRACE`], not multiplied per thread.
fn join_before(handle: std::thread::JoinHandle<()>, deadline: Instant) -> bool {
    loop {
        if handle.is_finished() {
            let _ = handle.join();
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

/// Decide what [`run`] should do with a `serve()` result. Pure so the lifecycle contract is
/// unit-testable without the real PyO3/swss-common bridges (Part B: the Rust-idiom stand-in
/// for the reference `signal_handler` / `run` shutdown behaviour).
///
/// - A clean shutdown (`Ok(Shutdown)`) or an SFP-system error (`Ok(SfpSystemError)`) means the
///   daemon actually RAN — the process must EXIT (mirroring `DaemonXcvrd.run` returning /
///   `sys.exit`), never loop back into a fresh boot (which is what kept the process alive on
///   `supervisorctl stop` and forced the supervisor SIGKILL crash-loop).
/// - A setup `Err` before the daemon came up is retried, so a briefly-unavailable Redis /
///   emulator at boot does not fail the deploy-smoke — UNLESS a shutdown was requested in
///   the meantime, in which case we exit cleanly.
#[cfg_attr(test, derive(Debug, PartialEq))]
enum RunAction {
    Exit(i32),
    Retry,
}

fn decide_run_action(
    result: &Result<ServeOutcome, Box<dyn Error>>,
    shutdown_requested: bool,
) -> RunAction {
    match result {
        Ok(ServeOutcome::Shutdown) => RunAction::Exit(0),
        Ok(ServeOutcome::SfpSystemError) => RunAction::Exit(SFP_SYSTEM_ERROR),
        Err(_) if shutdown_requested => RunAction::Exit(0),
        Err(_) => RunAction::Retry,
    }
}

/// Name of the platform flag that starts [`SffManagerTask`] (xcvrd forwards it from the
/// pmon supervisor command line to the daemon).
const ENABLE_SFF_MGR_FLAG: &str = "--enable_sff_mgr";

/// The command-line arguments the reference `xcvrd` accepts (`xcvrd.py:main`, `argparse`).
/// The Rust daemon parses the exact same inputs so it is a drop-in replacement:
///
/// * `--skip_cmis_mgr` — do not start the CMIS datapath manager (`store_true`, default off).
/// * `--enable_sff_mgr` — start the optional SFF-8636 manager (`store_true`, default off).
/// * `--dom_temperature_poll_interval N` — run the fast DOM-temperature poll every N seconds;
///   absent (the default) leaves the DOM-thermal task unstarted.
/// * `--dom_update_interval N` — DOM sensor poll period in seconds; absent (the default) uses
///   the 60 s [`DomInfoUpdateTask`] default.
#[derive(Debug, Default, PartialEq)]
pub struct Args {
    pub skip_cmis_mgr: bool,
    pub enable_sff_mgr: bool,
    pub dom_temperature_poll_interval: Option<i64>,
    pub dom_update_interval: Option<i64>,
}

/// Outcome of parsing argv, mirroring `argparse` (which exits 0 on `-h`, 2 on a usage error).
#[cfg_attr(test, derive(Debug, PartialEq))]
enum ArgsParse {
    Parsed(Args),
    Help,
    Error(String),
}

const ARGS_USAGE: &str = "usage: xcvrd-rs [-h] [--skip_cmis_mgr] [--enable_sff_mgr] \
     [--dom_temperature_poll_interval DOM_TEMPERATURE_POLL_INTERVAL] \
     [--dom_update_interval DOM_UPDATE_INTERVAL]";

impl Args {
    /// Parse from the process argv (skipping argv[0]); exit like `argparse` on `-h`/error.
    pub fn from_env() -> Args {
        match Self::parse(std::env::args().skip(1)) {
            ArgsParse::Parsed(a) => a,
            ArgsParse::Help => {
                println!("{ARGS_USAGE}");
                std::process::exit(0);
            }
            ArgsParse::Error(msg) => {
                eprintln!("{ARGS_USAGE}\nxcvrd-rs: error: {msg}");
                std::process::exit(2);
            }
        }
    }

    /// Pure parser (unit-testable) mirroring the reference `argparse` grammar: two `store_true`
    /// flags plus two integer options that accept both `--opt N` and `--opt=N` forms. An unknown
    /// argument, a missing integer value, or a non-integer value is a usage error, exactly like
    /// `argparse type=int`.
    fn parse<I: Iterator<Item = String>>(args: I) -> ArgsParse {
        let mut out = Args::default();
        let mut it = args.into_iter();
        while let Some(arg) = it.next() {
            match arg.as_str() {
                "-h" | "--help" => return ArgsParse::Help,
                "--skip_cmis_mgr" => out.skip_cmis_mgr = true,
                "--enable_sff_mgr" => out.enable_sff_mgr = true,
                _ => {
                    if let Some(inline) = int_option(&arg, "--dom_temperature_poll_interval") {
                        match resolve_int("--dom_temperature_poll_interval", inline, &mut it) {
                            Ok(v) => out.dom_temperature_poll_interval = Some(v),
                            Err(e) => return ArgsParse::Error(e),
                        }
                    } else if let Some(inline) = int_option(&arg, "--dom_update_interval") {
                        match resolve_int("--dom_update_interval", inline, &mut it) {
                            Ok(v) => out.dom_update_interval = Some(v),
                            Err(e) => return ArgsParse::Error(e),
                        }
                    } else {
                        return ArgsParse::Error(format!("unrecognized arguments: {arg}"));
                    }
                }
            }
        }
        ArgsParse::Parsed(out)
    }
}

/// Match an integer option: returns `Some(Some(v))` for `--name=v`, `Some(None)` for a bare
/// `--name` (its value is the next argv token), and `None` if `arg` is not this option.
fn int_option(arg: &str, name: &str) -> Option<Option<String>> {
    if arg == name {
        Some(None)
    } else {
        arg.strip_prefix(name)
            .and_then(|rest| rest.strip_prefix('='))
            .map(|v| Some(v.to_string()))
    }
}

/// Resolve an integer option's value (inline `=v` or the following argv token) and parse it,
/// mirroring `argparse type=int`: a missing or non-integer value is a usage error.
fn resolve_int<I: Iterator<Item = String>>(
    name: &str,
    inline: Option<String>,
    it: &mut I,
) -> Result<i64, String> {
    let raw = match inline {
        Some(v) => v,
        None => it
            .next()
            .ok_or_else(|| format!("argument {name}: expected one argument"))?,
    };
    raw.trim()
        .parse::<i64>()
        .map_err(|_| format!("argument {name}: invalid int value: '{raw}'"))
}

/// Entry point. Runs the daemon and then EXITS — the reference `DaemonXcvrd.run` blocks on
/// `stop_event.wait()`, joins its worker threads, runs `deinit`, and the process exits (0, or
/// `SFP_SYSTEM_ERROR`); it does not loop back into a fresh boot. We only retry `serve()` when
/// its *setup* fails before the daemon is up (transient Redis/emulator unavailability at boot).
/// Signal handlers are installed up front so a SIGTERM at any point — including during a
/// setup-retry sleep — drives a clean exit instead of leaving a zombie the supervisor SIGKILLs.
pub fn run() -> ! {
    let args = Args::from_env();
    eprintln!("xcvrd-rs: starting (presence, identity, DOM, CMIS, SFF)");
    install_signal_handlers();
    // Arm the hard shutdown backstop up front so a SIGTERM at ANY point — during boot, the event
    // loop, a worker join, or a setup-retry sleep — is guaranteed to exit the process within
    // HARD_SHUTDOWN_GRACE, before supervisord escalates to SIGKILL.
    spawn_shutdown_enforcer();
    loop {
        let result = serve(&args);
        match decide_run_action(&result, SHUTDOWN_REQUESTED.load(Ordering::SeqCst)) {
            RunAction::Exit(0) => {
                eprintln!("xcvrd-rs: graceful shutdown complete; exiting");
                std::process::exit(0);
            }
            RunAction::Exit(code) => {
                eprintln!("xcvrd-rs: exiting ({code}) for supervisor restart");
                std::process::exit(code);
            }
            RunAction::Retry => {
                if let Err(e) = &result {
                    eprintln!("xcvrd-rs: serve setup error: {e}; retrying in 3s");
                }
                std::thread::sleep(Duration::from_secs(3));
            }
        }
    }
}

fn serve(args: &Args) -> Result<ServeOutcome, Box<dyn Error>> {
    let namespaces = env::front_end_namespaces()?;

    let hal: Arc<dyn Hal> = Arc::new(BridgeHal::new()?); // PyO3 -> sonic_platform -> xcvr-emu
    let table_helper = Arc::new(XcvrTableHelper::new(&namespaces)?); // swss-common -> STATE_DB

    let num_sfps = hal.num_sfps()?;

    // Boot readiness gate (xcvrd.py:1062-1068): consult CONFIG_DB/APPL_DB for PortConfigDone
    // before building the port mapping and starting the producers — the reference's "start
    // only after the ports are configured". Fast + best-effort: on the pre-populated pmon
    // testbed no portsyncd writes the sentinel, so the gate drains the PORT_TABLE snapshot
    // once and PROCEEDS immediately (a non-blocking no-op) rather than hanging or adding boot
    // latency, which would break the e2e gate. A SIGTERM during the gate ends
    // the wait via SHUTDOWN_REQUESTED.
    for namespace in &namespaces {
        crate::xcvrd_utilities::port_event_helper::run_port_config_done_gate(namespace, &|| {
            SHUTDOWN_REQUESTED.load(Ordering::SeqCst)
        });
    }

    let port_mapping = get_port_mapping(&namespaces, num_sfps)?;
    eprintln!(
        "xcvrd-rs: {} configured ports discovered",
        port_mapping.logical_port_list().len()
    );

    // Cold-start correctness: purge TRANSCEIVER_INFO left in STATE_DB for a module that
    // was unplugged while xcvrd was down (STATE_DB survives the daemon).
    remove_stale_transceiver_info(hal.as_ref(), table_helper.as_ref(), &port_mapping);

    let stop = Arc::new(AtomicBool::new(false));
    let sfp_error_event = Arc::new(AtomicBool::new(false));

    // Graceful shutdown: a SIGTERM/SIGINT (e.g. `supervisorctl stop xcvrd`) sets a global
    // flag; a lightweight watcher propagates it to `stop` so every task loop exits and the
    // main thread runs `deinit` before the supervisor SIGKILLs us. Mirrors the reference
    // `DaemonXcvrd.signal_handler` -> stop_event -> `deinit()`.
    install_signal_handlers();
    let sig_stop = stop.clone();
    let sig_watcher = std::thread::Builder::new()
        .name("shutdown-watcher".to_string())
        .spawn(move || {
            while !sig_stop.load(Ordering::Relaxed) {
                if SHUTDOWN_REQUESTED.load(Ordering::SeqCst) {
                    sig_stop.store(true, Ordering::SeqCst);
                    break;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
        })?;

    // The DOM poll task shares the HAL / table-helper / port-map with the SFP task
    // (SfpStateUpdateTask::new consumes the originals, so clone first). It carries the
    // `--skip_cmis_mgr` flag so that, when the CMIS manager is disabled, the CMIS-init gate
    // is a no-op; otherwise DOM polling defers while a port is still in CMIS init
    // (non-terminal `cmis_state`) and only flows once the module reaches READY. The
    // `--dom_update_interval` argument sets the poll period (default 60 s).
    let dom_task = DomInfoUpdateTask::new(
        port_mapping.clone(),
        hal.clone(),
        table_helper.clone(),
        args.skip_cmis_mgr,
        args.dom_update_interval,
    );

    // Optional fast DOM-temperature poll → TRANSCEIVER_DOM_TEMPERATURE, started only when
    // `--dom_temperature_poll_interval N` is given (xcvrd.py:1171 `if ... is not None`).
    // Built here (before the SFP task consumes `hal`/`port_mapping`) and spawned below with
    // the other workers. A negative interval is nonsensical for a poll period, so it is
    // ignored with a warning rather than started.
    let dom_thermal_task = match args.dom_temperature_poll_interval {
        Some(secs) if secs >= 0 => Some(DomThermalInfoUpdateTask::new(
            port_mapping.clone(),
            hal.clone(),
            table_helper.clone(),
            Duration::from_secs(secs as u64),
        )),
        Some(secs) => {
            eprintln!(
                "xcvrd-rs: invalid dom_temperature_poll_interval {secs}; \
                 DOM-temperature task not started"
            );
            None
        }
        None => None,
    };

    // Load media_settings.json + optics_si_settings.json once at startup, skipped on a
    // fast-reboot (xcvrd.py:1056-1060) — the ASIC/module SI state survives a fast-reboot,
    // so re-publishing it would be redundant and could disrupt an in-service datapath.
    let (media_settings, optics_si_settings) = if table_helper
        .asic_ids()
        .all(|asic| is_fast_reboot_enabled(table_helper.get_fast_restart_enable_tbl(asic)))
    {
        eprintln!(
            "xcvrd-rs: fast-reboot — skip loading media_settings.json / optics_si_settings.json"
        );
        (serde_json::json!({}), serde_json::json!({}))
    } else {
        (
            media_settings_parser::load_media_settings(),
            optics_si_parser::load_optics_si_settings(),
        )
    };

    // CMIS datapath bring-up state machine, started unless `--skip_cmis_mgr` is set
    // (xcvrd.py:1159 `if not self.skip_cmis_mgr:`). Production wires each port's
    // control/decode surface to a `BridgeCmisApi` over the real SfpHandle; unit tests
    // inject MockCmisApi.
    let cmis_factory: CmisApiFactory =
        Box::new(|sfp| Some(Box::new(BridgeCmisApi::new(sfp)) as Box<dyn CmisApi>));
    let mut cmis_task = CmisManagerTask::new(
        namespaces.clone(),
        port_mapping.clone(),
        hal.clone(),
        table_helper.clone(),
        cmis_factory,
        args.skip_cmis_mgr,
    );
    cmis_task.set_optics_si_settings(optics_si_settings);

    // Optional SFF (non-CMIS) deterministic link bring-up, gated on the `--enable_sff_mgr`
    // flag (xcvrd.py:1150 forwards it to the SffManagerTask). Spawned before the CMIS
    // manager, mirroring the reference thread start order (xcvrd.py:1148). Production wires
    // each port's SFF-8636 control surface to a `BridgeSffApi` over the real SfpHandle; unit
    // tests inject MockSffApi.
    let sff_handle = if args.enable_sff_mgr {
        eprintln!("xcvrd-rs: {ENABLE_SFF_MGR_FLAG} set; starting SffManagerTask");
        let sff_factory: SffApiFactory =
            Box::new(|sfp| Some(Box::new(BridgeSffApi::new(sfp)) as Box<dyn SffApi>));
        let sff_task = SffManagerTask::new(
            namespaces.clone(),
            hal.clone(),
            table_helper.clone(),
            sff_factory,
        );
        let sff_stop = stop.clone();
        Some(
            std::thread::Builder::new()
                .name("SffManagerTask".to_string())
                .spawn(move || {
                    // `run` wraps each bring-up sweep in catch_unwind so a per-port panic
                    // restarts the loop rather than tearing the daemon down.
                    sff_task.run(sff_stop);
                })?,
        )
    } else {
        None
    };

    let mut task = SfpStateUpdateTask::new(namespaces, port_mapping, hal, table_helper.clone());
    task.set_media_settings(media_settings);

    // Boot publish: INFO + STATUS_SW (status/error/cmis_state) + DOM thresholds.
    task.init(&stop);
    eprintln!("xcvrd-rs: initial sync complete; watching for change events");

    // No boot-prime DOM pass. The reference `DomInfoUpdateTask` delays its FIRST poll a
    // full interval (`next_periodic_db_update_time = loop_start + period`) and never
    // publishes the DOM/STATUS diagnostic tables at boot; `task_worker` below matches
    // that (`next = now + interval`). Priming a pass here would diverge from that cadence
    // and, critically, pre-populate the *latched* flag tables (TRANSCEIVER_DOM_FLAG /
    // TRANSCEIVER_STATUS_FLAG) at boot. An off-cadence boot publish makes a freshly-raised
    // latched flag observable before any link-change re-read, breaking the link-change
    // contract (a raised flag must surface only on the ~60s periodic pass or a genuine
    // flap re-read — see `on_port_update_event`). TRANSCEIVER_INFO / STATUS_SW / DOM
    // thresholds still appear promptly: they are published by `SfpStateUpdateTask::init`
    // above, not this loop.

    let dom_stop = stop.clone();
    let dom_handle = std::thread::Builder::new()
        .name("DomInfoUpdateTask".to_string())
        .spawn(move || {
            // `task_worker` now owns a CONFIG_DB PORT watch and mutates the port mapping on a
            // runtime add/remove, so it takes `&mut self` — rebind the moved task as mutable.
            let mut dom_task = dom_task;
            // Keep the DOM loop resilient: a panic in one pass restarts the loop rather
            // than tearing the daemon down (the supervisor must stay RUNNING).
            while !dom_stop.load(Ordering::Relaxed) {
                let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    dom_task.task_worker(&dom_stop);
                }));
                if outcome.is_ok() {
                    break; // clean exit — stop was set
                }
                eprintln!("xcvrd-rs: DOM task panicked; restarting DOM monitoring loop");
            }
        })?;

    // Started unless `--skip_cmis_mgr` is set (xcvrd.py:1159). `run` seeds every port to
    // cmis_state=UNKNOWN then drives the bring-up sweeps, wrapping each pass in catch_unwind
    // so a panic restarts the loop rather than killing the daemon.
    let cmis_handle = if !args.skip_cmis_mgr {
        let cmis_stop = stop.clone();
        Some(
            std::thread::Builder::new()
                .name("CmisManagerTask".to_string())
                .spawn(move || {
                    cmis_task.run(cmis_stop);
                })?,
        )
    } else {
        eprintln!("xcvrd-rs: --skip_cmis_mgr set; skipping CmisManagerTask");
        None
    };

    // Optional DOM-temperature poll thread (started only when `--dom_temperature_poll_interval`
    // was given, xcvrd.py:1171).
    let dom_thermal_handle = if let Some(dom_thermal_task) = dom_thermal_task {
        let t_stop = stop.clone();
        Some(
            std::thread::Builder::new()
                .name("DomThermalInfoUpdateTask".to_string())
                .spawn(move || {
                    dom_thermal_task.run(t_stop);
                })?,
        )
    } else {
        None
    };

    // Presence/identity/error state machine — runs until a SIGTERM/SIGINT sets `stop`
    // (via the shutdown watcher) or a fatal system-not-ready timeout (STATE_EXIT, which
    // sets `sfp_error_event`).
    task.task_worker(&stop, &sfp_error_event);

    // Shutdown: stop every task, join them (bounded), then run the graceful table teardown. On a
    // NORMAL shutdown this deletes TRANSCEIVER_STATUS/_STATUS_SW; a warm/fast reboot preserves them
    // (deinit_transceiver_tables gates on the reboot flags). Use the task's LIVE port mapping
    // (mutated by CONFIG_DB add/remove) so a port added/removed at runtime is handled correctly.
    //
    // Joins are BOUNDED by a single shared deadline: the workers make PyO3/bridge calls, and an
    // unbounded `join()` on one wedged in a slow call was exactly what overran supervisord's
    // stopwaitsecs and got the daemon SIGKILLed (the SIGKILL crash-loop). A worker that has not
    // unwound by the deadline is left running and reclaimed by process exit; the `shutdown-enforcer`
    // is the ultimate backstop if even this path (or the main loop above) is wedged.
    stop.store(true, Ordering::Relaxed);
    let join_deadline = Instant::now() + SHUTDOWN_JOIN_GRACE;
    if !join_before(dom_handle, join_deadline) {
        eprintln!("xcvrd-rs: DOM task did not stop within grace; leaving it for process exit");
    }
    if let Some(cmis_handle) = cmis_handle {
        if !join_before(cmis_handle, join_deadline) {
            eprintln!("xcvrd-rs: CMIS task did not stop within grace; leaving it for process exit");
        }
    }
    if let Some(dom_thermal_handle) = dom_thermal_handle {
        if !join_before(dom_thermal_handle, join_deadline) {
            eprintln!(
                "xcvrd-rs: DOM-temperature task did not stop within grace; leaving it for process exit"
            );
        }
    }
    if let Some(sff_handle) = sff_handle {
        if !join_before(sff_handle, join_deadline) {
            eprintln!("xcvrd-rs: SFF task did not stop within grace; leaving it for process exit");
        }
    }
    let _ = join_before(sig_watcher, join_deadline);
    deinit_transceiver_tables(table_helper.as_ref(), task.port_mapping());

    // The daemon RAN and is now torn down; the process must exit (the reference
    // `DaemonXcvrd.run` returns here and the process ends — it does NOT loop into a fresh
    // boot). A SIGTERM/SIGINT graceful stop exits 0; an unrecoverable SFP state-machine exit
    // (sfp_error_event) mirrors the reference `sys.exit(SFP_SYSTEM_ERROR)`.
    if sfp_error_event.load(Ordering::SeqCst) {
        Ok(ServeOutcome::SfpSystemError)
    } else {
        Ok(ServeOutcome::Shutdown)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Part B — CLI argument parsing (tests/test_xcvrd.py::main argparse contract). The Rust
    // daemon must accept the exact same four arguments the reference `xcvrd` does.
    fn parse(argv: &[&str]) -> ArgsParse {
        Args::parse(argv.iter().map(|s| s.to_string()))
    }
    fn parsed(argv: &[&str]) -> Args {
        match parse(argv) {
            ArgsParse::Parsed(a) => a,
            other => panic!("expected Parsed, got {other:?}"),
        }
    }

    // Every flag defaults off / absent, exactly like argparse's defaults.
    #[test]
    fn args_default_when_empty() {
        assert_eq!(parsed(&[]), Args::default());
        assert!(!parsed(&[]).skip_cmis_mgr);
        assert!(!parsed(&[]).enable_sff_mgr);
        assert_eq!(parsed(&[]).dom_temperature_poll_interval, None);
        assert_eq!(parsed(&[]).dom_update_interval, None);
    }

    // The two `store_true` flags, individually and together.
    #[test]
    fn args_store_true_flags() {
        assert!(parsed(&["--skip_cmis_mgr"]).skip_cmis_mgr);
        assert!(parsed(&["--enable_sff_mgr"]).enable_sff_mgr);
        let a = parsed(&["--skip_cmis_mgr", "--enable_sff_mgr"]);
        assert!(a.skip_cmis_mgr && a.enable_sff_mgr);
    }

    // The two integer options accept both the `--opt N` and `--opt=N` forms (argparse).
    #[test]
    fn args_int_options_both_forms() {
        assert_eq!(
            parsed(&["--dom_update_interval", "30"]).dom_update_interval,
            Some(30)
        );
        assert_eq!(
            parsed(&["--dom_update_interval=45"]).dom_update_interval,
            Some(45)
        );
        assert_eq!(
            parsed(&["--dom_temperature_poll_interval", "5"]).dom_temperature_poll_interval,
            Some(5)
        );
        assert_eq!(
            parsed(&["--dom_temperature_poll_interval=7"]).dom_temperature_poll_interval,
            Some(7)
        );
        // 0 is a valid value (distinct from absent).
        assert_eq!(
            parsed(&["--dom_update_interval", "0"]).dom_update_interval,
            Some(0)
        );
        // A negative int parses (argparse type=int allows it); the task layer applies the guard.
        assert_eq!(
            parsed(&["--dom_update_interval", "-5"]).dom_update_interval,
            Some(-5)
        );
    }

    // The full argument set, in one launch — the shape a real pmon supervisor line uses.
    #[test]
    fn args_full_launch_line() {
        let a = parsed(&[
            "--skip_cmis_mgr",
            "--enable_sff_mgr",
            "--dom_temperature_poll_interval",
            "10",
            "--dom_update_interval",
            "120",
        ]);
        assert_eq!(
            a,
            Args {
                skip_cmis_mgr: true,
                enable_sff_mgr: true,
                dom_temperature_poll_interval: Some(10),
                dom_update_interval: Some(120),
            }
        );
    }

    // Usage errors mirror argparse (which exits 2): a non-integer value, a missing value, and
    // an unrecognized argument all fail to parse.
    #[test]
    fn args_usage_errors() {
        assert!(matches!(
            parse(&["--dom_update_interval", "abc"]),
            ArgsParse::Error(_)
        ));
        assert!(matches!(
            parse(&["--dom_update_interval=abc"]),
            ArgsParse::Error(_)
        ));
        assert!(matches!(
            parse(&["--dom_update_interval"]),
            ArgsParse::Error(_)
        ));
        assert!(matches!(parse(&["--bogus"]), ArgsParse::Error(_)));
        assert!(matches!(parse(&["Ethernet0"]), ArgsParse::Error(_)));
    }

    // `-h`/`--help` short-circuits to Help (argparse exits 0 after printing usage).
    #[test]
    fn args_help_flag() {
        assert!(matches!(parse(&["-h"]), ArgsParse::Help));
        assert!(matches!(parse(&["--help"]), ArgsParse::Help));
    }

    // Part B — lifecycle contract for the `run()` loop: the daemon must EXIT after it RAN
    // (never loop back into a fresh boot) so `supervisorctl stop`/`restart` completes cleanly
    // instead of forcing the supervisor to SIGKILL the process and leaving xcvrd perpetually
    // STOPPING/STARTING.

    // A graceful SIGTERM/SIGINT shutdown -> exit(0). The process ends; the supervisor starts a
    // fresh one and reports RUNNING promptly.
    #[test]
    fn graceful_shutdown_exits_zero() {
        let r: Result<ServeOutcome, Box<dyn Error>> = Ok(ServeOutcome::Shutdown);
        assert_eq!(decide_run_action(&r, false), RunAction::Exit(0));
        // The shutdown_requested flag is irrelevant once serve() reported a clean run.
        assert_eq!(decide_run_action(&r, true), RunAction::Exit(0));
    }

    // An unrecoverable SFP state-machine exit (sfp_error_event) -> exit(SFP_SYSTEM_ERROR),
    // mirroring the reference `sys.exit(SFP_SYSTEM_ERROR)` so the supervisor restarts us.
    #[test]
    fn sfp_system_error_exits_nonzero() {
        let r: Result<ServeOutcome, Box<dyn Error>> = Ok(ServeOutcome::SfpSystemError);
        assert_eq!(
            decide_run_action(&r, false),
            RunAction::Exit(SFP_SYSTEM_ERROR)
        );
        assert_eq!(SFP_SYSTEM_ERROR, 4);
    }

    // A transient SETUP error before the daemon came up -> retry, so a briefly-unavailable
    // Redis/emulator at boot does not fail the deploy-smoke.
    #[test]
    fn setup_error_retries_when_no_shutdown() {
        let r: Result<ServeOutcome, Box<dyn Error>> = Err("redis not ready".into());
        assert_eq!(decide_run_action(&r, false), RunAction::Retry);
    }

    // ...but if a SIGTERM/SIGINT arrived during the setup-retry window, exit cleanly instead of
    // looping forever (so `supervisorctl stop` during a boot flap still stops the daemon).
    #[test]
    fn setup_error_exits_when_shutdown_requested() {
        let r: Result<ServeOutcome, Box<dyn Error>> = Err("redis not ready".into());
        assert_eq!(decide_run_action(&r, true), RunAction::Exit(0));
    }

    // The shutdown-signal handler flips the shared flag (async-signal-safe atomic store), the
    // observable the shutdown watcher polls to set `stop` — the reference `signal_handler`
    // setting `stop_event`.
    #[test]
    fn signal_handler_sets_shutdown_flag() {
        SHUTDOWN_REQUESTED.store(false, Ordering::SeqCst);
        assert!(!SHUTDOWN_REQUESTED.load(Ordering::SeqCst));
        handle_shutdown_signal(libc::SIGTERM);
        assert!(SHUTDOWN_REQUESTED.load(Ordering::SeqCst));
        // Reset so the flag doesn't leak into other tests in the same process.
        SHUTDOWN_REQUESTED.store(false, Ordering::SeqCst);
    }

    // The shutdown timing budgets keep every graceful stop inside supervisord's stopwaitsecs (10s)
    // so a `supervisorctl stop`/`restart` never escalates to SIGKILL (the SIGKILL crash-loop), while
    // still leaving the hard `shutdown-enforcer` room to fire before that: the join grace must be
    // shorter than the hard grace, which must be shorter than stopwaitsecs.
    #[test]
    fn shutdown_budgets_stay_within_supervisor_stopwaitsecs() {
        assert!(SHUTDOWN_JOIN_GRACE < HARD_SHUTDOWN_GRACE);
        assert!(HARD_SHUTDOWN_GRACE < Duration::from_secs(10));
    }

    // Common graceful-stop path: a worker that has already noticed `stop` and returned is joined
    // immediately (well before the deadline), so `deinit` runs promptly and the process exits clean.
    #[test]
    fn join_before_joins_a_finished_worker() {
        let h = std::thread::spawn(|| {});
        // Let it run to completion first.
        while !h.is_finished() {
            std::thread::sleep(Duration::from_millis(5));
        }
        let deadline = Instant::now() + Duration::from_secs(5);
        assert!(join_before(h, deadline));
    }

    // The fix for the SIGKILL crash-loop: a worker wedged past the deadline (here, stuck in a long
    // sleep standing in for a slow/blocked PyO3 bridge call) is NOT waited on unboundedly —
    // join_before returns false at the deadline so shutdown proceeds and the process can exit
    // (the stuck thread is reclaimed by process exit). It must return in ~the grace window, nowhere
    // near the thread's own runtime.
    #[test]
    fn join_before_gives_up_on_a_wedged_worker_at_the_deadline() {
        let h = std::thread::spawn(|| {
            std::thread::sleep(Duration::from_secs(10));
        });
        let start = Instant::now();
        let deadline = start + Duration::from_millis(120);
        assert!(!join_before(h, deadline));
        assert!(start.elapsed() < Duration::from_secs(2));
    }
}
