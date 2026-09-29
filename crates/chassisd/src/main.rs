//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

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

use std::collections::BTreeMap;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime};

use platform_api::{ChassisInfoCols, ModuleInfo, ModuleInfoCols, ModuleStatus, PlatformApi};
use clap::Parser;
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

/// `chassisd:CHASSIS_LOAD_ERROR` and `chassisd:CHASSIS_NOT_SUPPORTED`.
/// supervisord reads the code.
const CHASSIS_LOAD_ERROR: i32 = 1;

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
const CHASSIS_NOT_SUPPORTED: i32 = 2;

/// `chassisd:DEFAULT_LINECARD_REBOOT_TIMEOUT`, overridable from
/// `platform_env.conf`.
const DEFAULT_LINECARD_REBOOT_TIMEOUT: Duration = Duration::from_secs(180);
const LINECARD_REBOOT_TIMEOUT_KEY: &str = "linecard_reboot_timeout";

fn linecard_reboot_timeout() -> Duration {
    reboot_timeout_from(platform_env::value(LINECARD_REBOOT_TIMEOUT_KEY).as_deref())
}

/// The same, from whatever the conf file held.
///
/// Anything that is not a whole number of seconds keeps the default.  A switch
/// that read `180s` as zero would report every line card as down-for-long-
/// enough-to-clean-up-after one cycle after it booted, and the cleanup drops
/// that card's rows out of the chassis app database.
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
    let modules = match platform.get_modules(ModuleInfoCols::ALL) {
        Ok(m) => {
            pmon_common::recovered!(read, "module read recovered");
            m
        }
        Err(e) => {
            pmon_common::fail_once!(read, "Failed to read the modules: {e}");
            // Not treated as "every card was pulled": that would blank
            // CHASSIS_MODULE_TABLE and drop every ASIC row on a transient
            // failure, which on a chassis is a forwarding outage.
            //
            // And not an error either, which is a departure from Python:
            // `chassisd:try_get` turns only `NotImplementedError` into a
            // default, so any other failure reading the modules ends the
            // Python daemon and supervisord restarts it.  Skipping the cycle
            // keeps the rows that are there.
            return Ok(());
        }
    };
    let asics = platform.get_asics().unwrap_or_default();

    updater.refresh(&modules, &asics, tables)?;
    updater.check_midplane(&modules, tables)?;

    for key in chassis_app_db::due(is_supervisor, updater.down_modules().iter(), now) {
        let slot = updater.down_modules().get(&key).map(|m| m.slot).unwrap_or(INVALID_SLOT);
        let (module, _) = chassis_app_db::split_down_key(&key);
        pmon_common::notice!(
            "Module {module} (Slot {slot}) is down for long time. \
             Initiating chassis app db clean up");
        if chassis_app_db::cleanup(&key, tables) {
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
    db_lost: &AtomicBool,
) -> i32 {
    let mut read = Latch::new();
    log::info!("Start daemon main loop");
    loop {
        match cycles.next(UPDATE_PERIOD).await {
            Tick::Exit(code) => return code,
            Tick::Cycle => {}
        }
        // The config watcher shares this redis, so when it loses the
        // subscription the writes below are about to fail too.  Checked here
        // rather than acted on over there because leaving is the poll loop's
        // to do -- the rows still have to be cleared on the way out.
        if db_lost.load(Ordering::SeqCst) {
            log::error!("config watcher lost CONFIG_DB; stopping the daemon");
            return ERR_DB_WRITE;
        }
        // A STATE_DB that will not take a write is a redis this process has
        // lost for good, so it leaves and supervisord brings it back onto a
        // fresh one -- exactly what Python's unwrapped `set` does by letting
        // the exception through `try`/`finally` and out of `main`.
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

/// How the daemon starts a subscription.
///
/// Injected rather than called directly because the real ones build a second
/// interpreter and subscribe to a redis, and under `cargo test` there is
/// neither -- while everything they are wired into is worth driving.
type Watcher<'a> = &'a dyn Fn(Watch, Arc<AtomicBool>) -> tokio::task::JoinHandle<()>;
/// Shut this batch of DPUs down and wait for all of them.  Injected for the
/// same reason as [`Watcher`], and so a test can record the batch instead.
type Shutter<'a> = &'a dyn Fn(&[String]);

/// Start one DPU's graceful admin-state change and return without waiting for
/// it.  Injected for the same reason as [`Shutter`]: the real one hands the
/// call to a thread that loads its own vendor module, which a test neither can
/// nor wants to do.
type Gracefully<'a> = &'a dyn Fn(&str, bool);

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
    let spawn_watcher = move |watch, db_lost| match watch {
        Watch::Config(machine) => spawn_config_watcher(machine, args.platform_api, db_lost),
        Watch::BootIds => spawn_boot_id_watcher(args.platform_api, db_lost),
    };
    // And the start-up sweep opens one per DPU, on threads of its own, for the
    // same reason and by the same route.
    let shut_down = move |names: &[String]| shut_down_in_parallel(names, args.platform_api);

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
/// decision.  Which shape to take is the decision that went wrong on
/// r-bobcat-01, so it is made here rather than in `main`.
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
    // platform API raised.  On r-bobcat-01, a SmartSwitch with four live DPUs,
    // that is exactly what happened: `get_dpu_id()` raised TypeError, chassisd
    // exited 2 within a second with nothing in the log, supervisord marked it
    // FATAL, and nothing alarmed -- pmon's critical_processes is empty, so
    // system-health checks none of these daemons and the exit listener does
    // not subscribe to PROCESS_STATE_FATAL.  A switch would have shipped with
    // no chassisd and no indication of it.
    let chassis = match platform.get_chassis_info(ChassisInfoCols::ALL) {
        Ok(c) => Some(c),
        Err(e) => {
            log::error!("Failed to read chassis info: {e}");
            return CHASSIS_LOAD_ERROR;
        }
    };
    let smartswitch = chassis.as_ref().is_some_and(|c| c.is_smartswitch);
    log::info!("smartswitch: {smartswitch}");

    // A DPU runs its own copy of this daemon, and it does something else
    // entirely: it reports its own two planes to the NPU rather than managing
    // anything.  `chassisd:main` picks between the two here.
    if smartswitch && chassis.as_ref().is_some_and(|c| c.is_dpu) {
        return run_on_dpu(platform, chassis.as_ref(), open, cycles).await;
    }
    if smartswitch {
        // The three things the SmartSwitch path reads from outside this
        // process, read here so the loop below takes them as arguments and can
        // be driven without a switch under it.
        let sweep = db::Db::connect(CHASSIS_STATE_DB)
            .map_err(|e| log::warn!("Cannot sweep {CHASSIS_STATE_DB}: {e}"))
            .ok();
        return run_smartswitch(
            platform,
            open,
            sweep.as_ref().map(|d| d as &dyn db::Keyspace),
            std::fs::read_to_string(REBOOT_CAUSE_FILE).ok().as_deref(),
            thresholds(),
            spawn_watcher,
            shut_down,
            cycles,
        )
        .await;
    }
    run_modular(platform, chassis.as_ref(), open, spawn_watcher, cycles).await
}

/// The modular-chassis shape: this card is a supervisor or a line card.
async fn run_modular<P: PlatformApi>(
    platform: &mut P,
    chassis: Option<&platform_api::ChassisInfo>,
    open: db::Opener<'_>,
    spawn_watcher: Watcher<'_>,
    cycles: &mut Cycles,
) -> i32 {
    let my_slot = chassis.and_then(|c| c.my_slot).unwrap_or(INVALID_SLOT);
    let supervisor_slot = chassis.and_then(|c| c.supervisor_slot).unwrap_or(INVALID_SLOT);

    // A fixed switch answers neither slot, and there is nothing for this daemon
    // to do on one.  Python exits with its own code so supervisord reports it
    // as configured-off rather than crashed (`chassisd:ChassisdDaemon.run`).
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
    let config_tbl = open(CONFIG_DB, CHASSIS_CFG_TABLE).ok();

    let tables = Tables {
        chassis: chassis_tbl.as_ref(),
        module: module_tbl.as_ref(),
        midplane: midplane_tbl.as_ref(),
        entity: entity_tbl.as_ref(),
        asic: asic_tbl.as_ref(),
        hostname: hostname_tbl.as_ref(),
        module_reboot: reboot_tbl.as_ref(),
        config: config_tbl.as_deref(),
    };

    // The midplane has to be up before anything is asked about reachability;
    // failing it is reported and not fatal, because the module table is still
    // worth publishing on a chassis whose midplane did not come up.
    let midplane_initialized = platform.init_midplane_switch().is_ok();
    if !midplane_initialized {
        log::error!("Chassisd midplane intialization failed");
    }

    let mut updater = ModuleUpdater::new(
        my_slot,
        supervisor_slot,
        linecard_reboot_timeout(),
        midplane_initialized,
    );

    let modules = platform.get_modules(ModuleInfoCols::ALL).unwrap_or_default();
    if let Err(e) = updater.publish_module_count(&modules, chassis_tbl.as_ref()) {
        log::error!("{e}");
        return ERR_DB_WRITE;
    }

    // The CONFIG_DB watcher runs on the supervisor only: a line card does not
    // administer its peers, and two writers would fight over the hardware.
    let db_lost = Arc::new(AtomicBool::new(false));
    let config_watcher =
        is_supervisor.then(|| spawn_watcher(Watch::Config(Machine::Chassis), Arc::clone(&db_lost)));

    let code = run_chassis(platform, &mut updater, &tables, is_supervisor, cycles, &db_lost).await;

    if let Some(h) = config_watcher {
        h.abort();
    }

    let modules = platform.get_modules(ModuleInfoCols::ALL).unwrap_or_default();
    updater.clear(&modules, &tables);
    code
}

/// Every handle the modular-chassis path writes through.
struct ChassisHandles {
    chassis: Box<dyn TableLike>,
    module: Box<dyn TableLike>,
    midplane: Box<dyn TableLike>,
    entity: Box<dyn TableLike>,
    asic: Box<dyn TableLike>,
    hostname: Box<dyn TableLike>,
    module_reboot: Box<dyn TableLike>,
}

/// Open the modular-chassis tables, naming the one that refused.
///
/// Which database each lives on is the part worth asserting: the module table
/// is STATE_DB's and the hostname table is CHASSIS_STATE_DB's, and they have
/// the same name.  Opening one on the other's database would have a supervisor
/// cleaning up against its own rows.
fn open_chassis_tables(open: db::Opener<'_>, is_supervisor: bool) -> Result<ChassisHandles, String> {
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
    })
}

/// Every handle the SmartSwitch path writes through.
struct DpuHandles {
    chassis: Box<dyn TableLike>,
    module: Box<dyn TableLike>,
    midplane: Box<dyn TableLike>,
    dpu_state: Box<dyn TableLike>,
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
/// measured on bobcat-10, four DPUs down took 12m44s and back up 7m27s against
/// sonic-mgmt's 375s and 200s budgets, which are sized for the concurrent
/// behaviour, and the deploy failed every time.
fn apply_config_event(
    platform: &mut dyn PlatformApi,
    machine: Machine,
    key: &str,
    event: config_updater::Event<'_>,
    gracefully: Gracefully<'_>,
) {
    let up = config_updater::admin_state_for(machine, event);
    log::info!("Changing module {key} to admin {} state", if up { "UP" } else { "DOWN" });
    match machine {
        Machine::SmartSwitch => gracefully(key, up),
        Machine::Chassis => {
            if let Err(err) = platform.set_module_admin_state(key, up) {
                log::error!("Failed to set admin state of {key}: {err}");
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
fn spawn_config_watcher(
    machine: Machine,
    which: PlatformImpl,
    db_lost: Arc<AtomicBool>,
) -> tokio::task::JoinHandle<()> {
    tokio::task::spawn_blocking(move || config_watch_loop(machine, which, &db_lost))
}

fn config_watch_loop(machine: Machine, which: PlatformImpl, db_lost: &AtomicBool) {
    use swss_common::{DbConnector, SubscriberStateTable};

    // Its own platform handle and its own connection: the poll loop holds the
    // other one mutably, and each thread owning its own is what the rest of
    // these daemons already do.
    let mut platform = match platform_provider::open(SYSLOG_IDENTIFIER, which) {
        Ok(p) => p,
        Err(e) => {
            log::error!("config watcher: failed to load chassis: {e}");
            return;
        }
    };

    // Detached on purpose: Python never joins these
    // (`chassisd:SmartSwitchModuleConfigUpdater.module_config_update`), and
    // joining is what serialised four DPUs into twenty minutes.
    let gracefully = move |key: &str, up: bool| {
        drop(spawn_admin_state_change(key, up, which));
    };

    let sst = match DbConnector::new_named(CONFIG_DB, true, 0)
        .and_then(|db| SubscriberStateTable::new(db, CHASSIS_CFG_TABLE, None, None))
    {
        Ok(s) => s,
        Err(e) => {
            log::error!("config watcher: cannot subscribe to {CHASSIS_CFG_TABLE}: {e:?}");
            db_lost.store(true, Ordering::SeqCst);
            return;
        }
    };

    watch_config(&sst, machine, db_lost, &mut |key, event| {
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
        self.read_data(timeout, true).map_err(|e| format!("{e:?}"))
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
/// Losing the redis is reported and left on, not retried.
///
/// The watchers and the poll loop share one redis, so a subscription a watcher
/// cannot hold is one the poll loop is about to fail a write on -- retrying
/// here would only decide which of the two noticed first.  And
/// `SubscriberStateTable` does not reconnect: a socket whose server is gone
/// stays readable, so `read_data` keeps answering while `pops` keeps failing.
/// Measured on bobcat-1254 by restarting redis under the daemon, that put 6642
/// `poll_descriptors: readData error` lines into one second.
///
/// Python's watchers are separate processes whose `pop()` raises, and they
/// never come back -- leaving the daemon up but deaf.  Taking the process down
/// instead is the one place this port is deliberately stricter, because a
/// restart repairs the watcher too.
fn watch(
    feed: &dyn ChangeFeed,
    what: &str,
    db_lost: &AtomicBool,
    on_change: &mut dyn FnMut(&Change),
) {
    use swss_common::SelectResult;

    // The same one-second timeout Python uses, so a shutdown is not waited out.
    const SELECT_TIMEOUT: Duration = Duration::from_millis(1000);

    loop {
        match feed.wait(SELECT_TIMEOUT) {
            Ok(SelectResult::Data) => {}
            Ok(SelectResult::Timeout) => continue,
            Ok(SelectResult::Signal) => return,
            Err(e) => {
                log::error!("{what}: select failed: {e}");
                db_lost.store(true, Ordering::SeqCst);
                return;
            }
        }
        let changes = match feed.take() {
            Ok(changes) => changes,
            // A closed connection stays readable, so `wait` can keep
            // answering `Data` with only this call failing.
            Err(e) => {
                log::error!("{what}: pops failed: {e}");
                db_lost.store(true, Ordering::SeqCst);
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
    db_lost: &AtomicBool,
    apply: &mut dyn FnMut(&str, config_updater::Event<'_>),
) {
    watch(feed, "config watcher", db_lost, &mut |c| {
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
    db_lost: &AtomicBool,
    capture: &mut dyn FnMut(&str, &str),
) {
    watch(feed, "reboot-cause watcher", db_lost, &mut |c| {
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
fn spawn_boot_id_watcher(
    which: PlatformImpl,
    db_lost: Arc<AtomicBool>,
) -> tokio::task::JoinHandle<()> {
    tokio::task::spawn_blocking(move || boot_id_watch_loop(which, &db_lost))
}

fn boot_id_watch_loop(which: PlatformImpl, db_lost: &AtomicBool) {
    use swss_common::{DbConnector, SubscriberStateTable};

    let mut platform = match platform_provider::open(SYSLOG_IDENTIFIER, which) {
        Ok(p) => p,
        Err(e) => {
            log::error!("reboot-cause watcher: failed to load chassis: {e}");
            return;
        }
    };
    let reboot_cause = match db::open(CHASSIS_STATE_DB, "REBOOT_CAUSE") {
        Ok(t) => t,
        Err(e) => {
            log::error!("reboot-cause watcher: cannot open REBOOT_CAUSE: {e}");
            db_lost.store(true, Ordering::SeqCst);
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
                dpu_updater::DPU_STATE_TABLE);
            db_lost.store(true, Ordering::SeqCst);
            return;
        }
    };

    let root = Path::new(dpu_reboot_cause::MODULE_REBOOT_CAUSE_DIR);
    watch_boot_ids(&sst, db_lost, &mut |name, boot_id| {
        dpu_reboot_cause::boot_id_update(
            root,
            name,
            boot_id,
            || platform.get_module_reboot_cause(name),
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
        if let Err(e) = platform.clear_module_state_transition(&m.name) {
            log::warn!("{}: clear_module_state_transition() failed: {e}", m.name);
        }
        if let Err(e) = platform.clear_module_gnoi_halt(&m.name) {
            log::warn!("{}: clear_module_gnoi_halt() failed: {e}", m.name);
        }

        // A seed that will not write is logged and the sweep goes on, as in
        // Python: the write is `update_dpu_state`, which catches it.  Stopping
        // here would leave every unconfigured DPU powered because
        // CHASSIS_STATE_DB was briefly unavailable.
        dpu_updater::init_dpu_state(
            &m.name,
            m.oper_status == Some(ModuleStatus::Online),
            reboot_root,
            tables,
        );

        // A DPU whose CONFIG_DB row cannot be read at all is left alone:
        // Python reaches that case through an exception and shuts nothing down
        // (the `except Exception` in
        // `chassisd:ChassisdDaemon.set_initial_dpu_admin_state`).  Guessing
        // "unconfigured" would power a DPU off because a database was briefly
        // unavailable.
        let Some(config) = tables.config else { continue };
        if config.get(&m.name).ok().flatten().is_none() {
            unconfigured.push(m.name.clone());
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
/// measured rather than stylistic: on an SN4280 one graceful shutdown takes
/// **~3m20s** -- the vendor call spends over three minutes waiting for the
/// DPU's OS to go down before it so much as logs -- so four in a row would hold
/// up the daemon's first publish by a quarter of an hour and refuse every
/// `config chassis modules startup` in the meantime.  Run together the four
/// finished in 3m24s, the slowest DPU rather than their sum.
///
/// `finalize` is deliberately *not* called on these handles: it runs the
/// interpreter's `atexit` handlers, which belong to the process's exit and not
/// to a worker thread that happens to have finished.
fn shut_down_in_parallel(names: &[String], which: PlatformImpl) {
    let threads: Vec<_> = names
        .iter()
        .map(|name| {
            log::info!("Changing module {name} to admin DOWN state");
            spawn_admin_state_change(name, false, which)
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
fn spawn_admin_state_change(
    name: &str,
    up: bool,
    which: PlatformImpl,
) -> std::thread::JoinHandle<()> {
    let name = name.to_string();
    std::thread::spawn(move || match platform_provider::open(SYSLOG_IDENTIFIER, which) {
        Ok(mut p) => {
            if let Err(e) = p.set_module_admin_state_gracefully(&name, up) {
                log::error!("Failed to set admin state of {name}: {e}");
            }
        }
        Err(e) => log::error!("{name}: failed to load chassis: {e}"),
    })
}

// ── SmartSwitch ───────────────────────────────────────────────────────────────

/// `/host/reboot-cause/reboot-cause.txt`, the NPU's own last reboot.
const REBOOT_CAUSE_FILE: &str = "/host/reboot-cause/reboot-cause.txt";
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
/// this DPU and *nothing* was done to it.
fn power_cycle_sequence(platform: &mut dyn PlatformApi, module: &str) -> bool {
    if platform.set_module_state_transition(module, TRANSITION_TYPE_RECOVERY).is_err() {
        return false;
    }
    if let Err(e) = platform.module_pre_shutdown(module) {
        log::warn!("{module}: module_pre_shutdown() failed: {e}");
    }
    if let Err(e) = platform.set_module_admin_state(module, false) {
        log::error!("{module}: Power-cycle failed: {e}");
    }
    if let Err(e) = platform.set_module_admin_state(module, true) {
        log::error!("{module}: Power-cycle failed: {e}");
    }
    if let Err(e) = platform.module_post_startup(module) {
        log::warn!("{module}: module_post_startup() failed: {e}");
    }
    // Released whatever happened above: a lock left held blocks every later
    // operation on this DPU, including the operator's.
    let _ = platform.clear_module_state_transition(module);
    true
}

impl<P: PlatformApi> dpu_recovery::PowerCycler for PlatformCycler<'_, P> {
    fn power_cycle(&mut self, module: &str) -> bool {
        power_cycle_sequence(self.platform, module)
    }

    fn midplane_down_reason(
        &mut self,
        module: &str,
    ) -> Result<platform_api::ModuleMidplaneDownReason, platform_api::PlatformError> {
        self.platform.get_module_midplane_down_reason(module)
    }
}

/// Read the four recovery thresholds out of `platform.json`.
fn thresholds() -> dpu_recovery::Thresholds {
    thresholds_from(std::fs::read_to_string(PLATFORM_JSON_FILE).ok().as_deref())
}

/// The same, from the file's contents.
///
/// A platform that says nothing keeps the defaults, and so does one whose file
/// will not parse: the alternative is a switch that power cycles its DPUs on a
/// schedule somebody typo'd.
fn thresholds_from(text: Option<&str>) -> dpu_recovery::Thresholds {
    let mut t = dpu_recovery::Thresholds::default();
    let Some(text) = text else { return t };
    let Ok(v) = serde_json::from_str::<serde_json::Value>(text) else {
        log::error!("Error parsing {PLATFORM_JSON_FILE}");
        return t;
    };
    let secs = |k: &str| v.get(k).and_then(|x| x.as_u64()).map(Duration::from_secs);
    if let Some(d) = secs("dpu_boot_timeout") {
        t.boot_timeout = d;
    }
    if let Some(d) = secs("dpu_self_recovery_timeout") {
        t.self_recovery_timeout = d;
    }
    if let Some(n) = v.get("dpu_reset_limit").and_then(|x| x.as_u64()) {
        t.reset_limit = n as u32;
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
    db_lost: &AtomicBool,
) -> i32 {
    let mut read = Latch::new();
    log::info!("Start daemon main loop");
    loop {
        match cycles.next(UPDATE_PERIOD).await {
            Tick::Exit(code) => return code,
            Tick::Cycle => {}
        }
        // Same reasoning as the modular loop: one redis, one decision.
        if db_lost.load(Ordering::SeqCst) {
            log::error!("config watcher lost CONFIG_DB; stopping the daemon");
            return ERR_DB_WRITE;
        }
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
    let config_tbl = open(CONFIG_DB, CHASSIS_CFG_TABLE).ok();
    let metadata_tbl = open(CONFIG_DB, dpu_updater::DEVICE_METADATA_TABLE).ok();

    let tables = dpu_updater::Tables {
        chassis: chassis_tbl.as_ref(),
        module: module_tbl.as_ref(),
        midplane: midplane_tbl.as_ref(),
        dpu_state: dpu_state_tbl.as_ref(),
        config: config_tbl.as_deref(),
        device_metadata: metadata_tbl.as_deref(),
    };

    let midplane_initialized = platform.init_midplane_switch().is_ok();
    if !midplane_initialized {
        log::error!("Chassisd midplane intialization failed");
    }

    let modules = platform.get_modules(ModuleInfoCols::ALL).unwrap_or_default();
    if modules.is_empty() {
        log::error!("Chassisd has no modules available");
    } else {
        let fvs = [(CHASSIS_INFO_CARD_NUM_FIELD, modules.len().to_string())];
        if let Err(e) = chassis_tbl.set(CHASSIS_INFO_KEY, &fvs) {
            log::error!("Failed to publish the module count: {e}");
            return ERR_DB_WRITE;
        }
    }

    // Before anything else looks at the DPUs, and before the recovery machine
    // is seeded -- the order `chassisd:ChassisdDaemon.run` runs them in.
    init_dpu_admin_state(
        platform, &modules, &tables, Path::new(dpu_reboot_cause::MODULE_REBOOT_CAUSE_DIR), shut_down);

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
        if let Err(e) = updater.init_recovery(&modules, &tables, &mut hw, npu_crashed, Instant::now())
        {
            log::error!("{e}");
            return ERR_DB_WRITE;
        }
    }

    let db_lost = Arc::new(AtomicBool::new(false));
    let config_watcher = spawn_watcher(Watch::Config(Machine::SmartSwitch), Arc::clone(&db_lost));
    // `chassisd:RebootCauseSubscriberTask`, started beside the config manager
    // in `chassisd:ChassisdDaemon.run` and stopped with it.
    let boot_id_watcher = spawn_watcher(Watch::BootIds, Arc::clone(&db_lost));

    let code =
        run_smartswitch_loop(platform, &mut updater, &tables, chassis_state, cycles, &db_lost)
            .await;

    config_watcher.abort();
    boot_id_watcher.abort();
    let modules = platform.get_modules(ModuleInfoCols::ALL).unwrap_or_default();
    updater.clear(&modules, &tables);
    code
}

// ── the copy that runs on a DPU ───────────────────────────────────────────────

/// Which of the two sources answers for a plane.
///
/// A platform that implements `get_dataplane_state` answers for itself; one
/// that does not falls back to the databases.  Asked per cycle rather than at
/// start-up, because a platform can begin answering once its own services are
/// up -- and a daemon that decided once would keep reading the fallback
/// forever.
fn plane_state(platform_says: Option<bool>, fallback: impl FnOnce() -> bool) -> bool {
    match platform_says {
        Some(v) => v,
        None => fallback(),
    }
}

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
    let code = loop {
        // Read afresh each cycle: a platform that implements these answers for
        // itself, and one that does not falls back to the databases -- which is
        // a per-cycle question, not a start-up one, because a platform can
        // begin answering once its own services are up.
        let info = platform.get_chassis_info(ChassisInfoCols::ALL).ok();
        let planes = match dpu_planes(
            info.as_ref(), sources.config_ports, sources.port_table, sources.system_ready)
        {
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

        match cycles.next(UPDATE_PERIOD).await {
            Tick::Exit(code) => break code,
            Tick::Cycle => {}
        }
    };
    dpu_state::shutdown(name, table);
    code
}

/// This DPU's two planes, from the platform or from the databases.
fn dpu_planes(
    info: Option<&platform_api::ChassisInfo>,
    config_ports: Option<&dyn TableLike>,
    port_table: Option<&dyn TableLike>,
    system_ready: Option<&dyn TableLike>,
) -> Result<dpu_state::Planes, String> {
    // The fallbacks read STATE_DB, and Python reads it bare here
    // (`chassisd:DpuStateUpdater._get_data_plane_state_common`,
    // `chassisd:DpuStateUpdater._get_control_plane_state_common`): a plane it
    // cannot determine must not be reported as down, because the NPU acts on
    // that.
    let data = match info.and_then(|c| c.dataplane_state) {
        Some(state) => plane_state(Some(state), || false),
        None => match (config_ports, port_table) {
            (Some(c), Some(p)) => dpu_state::data_plane_from_ports(c, p)?,
            _ => false,
        },
    };
    let control = match info.and_then(|c| c.controlplane_state) {
        Some(state) => plane_state(Some(state), || false),
        None => match system_ready {
            Some(t) => dpu_state::control_plane_from_system_ready(t)?,
            None => false,
        },
    };
    Ok(dpu_state::Planes { data, control })
}

/// Report this DPU's own two planes to the NPU.
async fn run_on_dpu<P: PlatformApi>(
    platform: &mut P,
    chassis: Option<&platform_api::ChassisInfo>,
    open: db::Opener<'_>,
    cycles: &mut Cycles,
) -> i32 {
    let name = format!("DPU{}", chassis.and_then(|c| c.dpu_id).unwrap_or(0));

    let Ok(dpu_state_tbl) = open(CHASSIS_STATE_DB, dpu_updater::DPU_STATE_TABLE) else {
        log::error!("Failed to connect to {CHASSIS_STATE_DB}");
        return CHASSIS_LOAD_ERROR;
    };
    // Only needed when the platform does not answer for its own planes.
    let config_ports = open(CONFIG_DB, "PORT").ok();
    let port_table = open("APPL_DB", "PORT_TABLE").ok();
    let system_ready = open(db::STATE_DB, "SYSTEM_READY").ok();

    let boot_id = dpu_state::read_boot_id(Path::new(dpu_state::BOOT_ID_PATH));
    run_dpu_loop(
        platform,
        &name,
        boot_id.as_deref(),
        Sources {
            config_ports: config_ports.as_deref(),
            port_table: port_table.as_deref(),
            system_ready: system_ready.as_deref(),
        },
        dpu_state_tbl.as_ref(),
        cycles,
    )
    .await
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A scripted CONFIG_DB subscription: each `wait` and each `take` answers
    /// with the next entry, and a script that runs out is a signal.
    #[derive(Default)]
    struct Feed {
        waits: std::cell::RefCell<std::collections::VecDeque<Result<swss_common::SelectResult, String>>>,
        takes: std::cell::RefCell<std::collections::VecDeque<Result<Vec<Change>, String>>>,
    }

    impl Feed {
        fn new(
            waits: Vec<Result<swss_common::SelectResult, String>>,
            takes: Vec<Result<Vec<Change>, String>>,
        ) -> Self {
            Self {
                waits: std::cell::RefCell::new(waits.into_iter().collect()),
                takes: std::cell::RefCell::new(takes.into_iter().collect()),
            }
        }
    }

    impl ChangeFeed for Feed {
        fn wait(&self, _timeout: Duration) -> Result<swss_common::SelectResult, String> {
            self.waits.borrow_mut().pop_front().unwrap_or(Ok(swss_common::SelectResult::Signal))
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
    fn watched(feed: &Feed, machine: Machine, db_lost: &AtomicBool) -> Vec<String> {
        let mut applied = Vec::new();
        watch_config(feed, machine, db_lost, &mut |key, event| {
            applied.push(format!("{key}:{}", config_updater::admin_state_for(machine, event)));
        });
        applied
    }

    /// A select that fails is a CONFIG_DB this thread has lost: the flag the
    /// poll loop reads goes up and the watcher stops.
    #[test]
    fn a_watcher_whose_select_fails_raises_the_flag() {
        let db_lost = AtomicBool::new(false);
        let feed = Feed::new(vec![Err("connection reset".into())], vec![]);
        assert!(watched(&feed, Machine::Chassis, &db_lost).is_empty());
        assert!(db_lost.load(Ordering::SeqCst));
    }

    /// A closed connection stays readable, so the failure can show up only in
    /// `pops`.  Same answer, and the watcher does not spin on it.
    #[test]
    fn a_watcher_whose_pops_fail_raises_the_flag_and_stops() {
        let db_lost = AtomicBool::new(false);
        let data = || Ok(swss_common::SelectResult::Data);
        let feed = Feed::new(
            vec![data(), data(), data()],
            vec![Err("connection reset".into()), Ok(vec![change("LINE-CARD0", false, None)])],
        );
        assert!(watched(&feed, Machine::Chassis, &db_lost).is_empty(),
                "nothing after the failure is applied");
        assert!(db_lost.load(Ordering::SeqCst));
        assert_eq!(feed.takes.borrow().len(), 1, "stopped at the first failure");
    }

    /// A signal is a shutdown, not a lost database.
    #[test]
    fn a_watcher_told_to_stop_leaves_the_flag_down() {
        let db_lost = AtomicBool::new(false);
        let feed = Feed::new(vec![Ok(swss_common::SelectResult::Signal)], vec![]);
        watched(&feed, Machine::Chassis, &db_lost);
        assert!(!db_lost.load(Ordering::SeqCst));
    }

    /// Timeouts are waited through, every change the machine owns is applied
    /// in order, and one it does not own is skipped.
    #[test]
    fn a_watcher_applies_what_its_machine_owns_across_timeouts() {
        use swss_common::SelectResult::{Data, Timeout};
        let db_lost = AtomicBool::new(false);
        let feed = Feed::new(
            vec![Ok(Timeout), Ok(Data), Ok(Timeout), Ok(Data)],
            vec![
                Ok(vec![change("DPU0", false, Some("up")), change("LINE-CARD1", false, None)]),
                Ok(vec![change("DPU1", true, None), change("DPU2", false, None)]),
            ],
        );
        assert_eq!(watched(&feed, Machine::SmartSwitch, &db_lost), vec![
            "DPU0:true".to_string(),
            "DPU1:false".to_string(),
            "DPU2:false".to_string(),
        ]);
        assert!(!db_lost.load(Ordering::SeqCst));
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
        assert_eq!(code, ERR_DB_WRITE, "a lost CONFIG_DB is a non-zero exit");
        assert_eq!(p.reads, 0, "and it leaves before reading the platform again");
    }

    /// A STATE_DB that refuses a write ends the loop the same way.
    ///
    /// This is the half with hardware evidence behind it: on bobcat-1254 the
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

    use platform_api::{AsicInfo, ChassisInfoCols, ModuleInfo, ModuleInfoCols, ModuleStatus, ModuleType, PlatformError};
    use pmon_common::db::MockTable;

    /// A platform whose module list can be swapped and made to fail.
    #[derive(Default)]
    struct FakePlatform {
        modules: Vec<ModuleInfo>,
        asics: Vec<AsicInfo>,
        fail: bool,
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
            Ok(self.asics.clone())
        }
    }

    fn card(name: &str, slot: i64, status: ModuleStatus) -> ModuleInfo {
        ModuleInfo {
            name: name.to_string(),
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
                module_reboot: &self.module_reboot, config: None,
            }
        }
    }

    /// The module table lives on STATE_DB and the hostname table on
    /// CHASSIS_STATE_DB, and they have the same name.  Opening one on the
    /// other's database would have a supervisor cleaning up against its own
    /// rows -- which nothing downstream could notice.
    #[test]
    fn the_chassis_tables_are_opened_on_the_right_two_databases() {
        let o = pmon_common::db::MockOpener::new();
        open_chassis_tables(&|d, t| o.open(d, t), true).expect("opens");
        assert_eq!(o.asked(), vec![
            ("STATE_DB".to_string(), "CHASSIS_TABLE".to_string()),
            ("STATE_DB".to_string(), "CHASSIS_MODULE_TABLE".to_string()),
            ("STATE_DB".to_string(), "CHASSIS_MIDPLANE_TABLE".to_string()),
            ("STATE_DB".to_string(), "PHYSICAL_ENTITY_INFO".to_string()),
            ("CHASSIS_STATE_DB".to_string(), "CHASSIS_FABRIC_ASIC_TABLE".to_string()),
            ("CHASSIS_STATE_DB".to_string(), "CHASSIS_MODULE_TABLE".to_string()),
            ("CHASSIS_STATE_DB".to_string(), "CHASSIS_MODULE_REBOOT_INFO_TABLE".to_string()),
        ]);
    }

    /// A line card writes its own ASICs; the supervisor writes the fabric
    /// cards'.  They are different tables and the choice cannot change while
    /// the daemon runs.
    #[test]
    fn a_line_card_opens_the_other_asic_table() {
        let o = pmon_common::db::MockOpener::new();
        open_chassis_tables(&|d, t| o.open(d, t), false).expect("opens");
        assert!(o.asked().contains(
            &("CHASSIS_STATE_DB".to_string(), "CHASSIS_ASIC_TABLE".to_string())));
        assert!(!o.asked().iter().any(|(_, t)| t == "CHASSIS_FABRIC_ASIC_TABLE"));
    }

    #[test]
    fn the_smartswitch_tables_are_opened_on_the_right_two_databases() {
        let o = pmon_common::db::MockOpener::new();
        open_dpu_tables(&|d, t| o.open(d, t)).expect("opens");
        assert_eq!(o.asked(), vec![
            ("STATE_DB".to_string(), "CHASSIS_TABLE".to_string()),
            ("STATE_DB".to_string(), "CHASSIS_MODULE_TABLE".to_string()),
            ("STATE_DB".to_string(), "CHASSIS_MIDPLANE_TABLE".to_string()),
            ("CHASSIS_STATE_DB".to_string(), "DPU_STATE".to_string()),
        ]);
    }

    /// There is nowhere to publish, so the daemon says which table and stops
    /// rather than running with a handle it cannot write through.
    #[test]
    fn a_table_that_will_not_open_names_its_database() {
        let o = pmon_common::db::MockOpener::failing("CHASSIS_MIDPLANE_TABLE");
        let e = open_chassis_tables(&|d, t| o.open(d, t), true).err().expect("stops");
        assert!(e.contains("STATE_DB"), "{e}");

        let o = pmon_common::db::MockOpener::failing("DPU_STATE");
        let e = open_dpu_tables(&|d, t| o.open(d, t)).err().expect("stops");
        assert!(e.contains("CHASSIS_STATE_DB"), "{e}");
    }

    /// A platform that records the order it was called in.
    #[derive(Default)]
    struct RecordingPlatform {
        calls: Vec<String>,
        refuse_lock: bool,
    }

    impl PlatformApi for RecordingPlatform {
        fn set_module_state_transition(&mut self, m: &str, t: &str) -> Result<(), PlatformError> {
            if self.refuse_lock {
                return Err(PlatformError::Backend("busy".into()));
            }
            self.calls.push(format!("lock({m},{t})"));
            Ok(())
        }
        fn clear_module_state_transition(&mut self, m: &str) -> Result<(), PlatformError> {
            self.calls.push(format!("unlock({m})"));
            Ok(())
        }
        fn module_pre_shutdown(&mut self, m: &str) -> Result<(), PlatformError> {
            self.calls.push(format!("pre_shutdown({m})"));
            Ok(())
        }
        fn module_post_startup(&mut self, m: &str) -> Result<(), PlatformError> {
            self.calls.push(format!("post_startup({m})"));
            Ok(())
        }
        fn set_module_admin_state(&mut self, m: &str, up: bool) -> Result<(), PlatformError> {
            self.calls.push(format!("admin({m},{up})"));
            Ok(())
        }
        fn set_module_admin_state_gracefully(&mut self, m: &str, up: bool)
            -> Result<(), PlatformError>
        {
            self.calls.push(format!("graceful({m},{up})"));
            Ok(())
        }
        fn clear_module_gnoi_halt(&mut self, m: &str) -> Result<(), PlatformError> {
            self.calls.push(format!("clear_halt({m})"));
            Ok(())
        }
    }

    /// The order is the behaviour: a DPU is running an OS, and cutting its
    /// power without telling it loses whatever it had not written out.
    #[test]
    fn a_power_cycle_tells_the_dpu_before_and_after_and_holds_the_lock() {
        let mut p = RecordingPlatform::default();
        assert!(power_cycle_sequence(&mut p, "DPU0"));
        assert_eq!(p.calls, vec![
            "lock(DPU0,recovery)".to_string(),
            "pre_shutdown(DPU0)".to_string(),
            "admin(DPU0,false)".to_string(),
            "admin(DPU0,true)".to_string(),
            "post_startup(DPU0)".to_string(),
            "unlock(DPU0)".to_string(),
        ]);
    }

    /// Another operation owns the DPU.  Nothing is done to it -- not even the
    /// pre-shutdown, which would tell a DPU somebody else is shutting down
    /// that it is about to be power cycled.
    #[test]
    fn a_busy_lock_means_the_dpu_is_not_touched_at_all() {
        let mut p = RecordingPlatform { refuse_lock: true, ..Default::default() };
        assert!(!power_cycle_sequence(&mut p, "DPU0"));
        assert!(p.calls.is_empty());
    }

    /// Record what the watcher hands off instead of starting a thread, and
    /// return the log in the order the events were applied.
    fn recording_gracefully(log: &std::cell::RefCell<Vec<String>>) -> impl Fn(&str, bool) + '_ {
        |key: &str, up: bool| log.borrow_mut().push(format!("graceful({key},{up})"))
    }

    /// A DPU is brought down gracefully and a line card is not: the abrupt
    /// call on a DPU cuts the power under a running SONiC.
    #[test]
    fn a_dpu_is_shut_down_gracefully_and_a_line_card_is_not() {
        let mut p = RecordingPlatform::default();
        let handed = std::cell::RefCell::new(Vec::new());
        apply_config_event(&mut p, Machine::SmartSwitch, "DPU0",
                           config_updater::Event::Set { admin_status: Some("up") },
                           &recording_gracefully(&handed));
        apply_config_event(&mut p, Machine::Chassis, "LINE-CARD0",
                           config_updater::Event::Set { admin_status: None },
                           &recording_gracefully(&handed));
        assert_eq!(handed.into_inner(), vec!["graceful(DPU0,true)".to_string()]);
        assert_eq!(p.calls, vec!["admin(LINE-CARD0,false)".to_string()]);
    }

    /// The DPU half never touches the borrowed platform.  That is the whole
    /// point: the call goes to a thread with its own handle, so the watcher
    /// loop is free to pick up the next event -- four DPUs at once rather than
    /// one after another.
    #[test]
    fn a_dpu_event_is_handed_off_and_does_not_block_the_watcher() {
        let mut p = RecordingPlatform::default();
        let handed = std::cell::RefCell::new(Vec::new());
        for (key, status) in [("DPU0", "down"), ("DPU1", "down"), ("DPU2", "up")] {
            apply_config_event(&mut p, Machine::SmartSwitch, key,
                               config_updater::Event::Set { admin_status: Some(status) },
                               &recording_gracefully(&handed));
        }
        assert_eq!(handed.into_inner(), vec![
            "graceful(DPU0,false)".to_string(),
            "graceful(DPU1,false)".to_string(),
            "graceful(DPU2,true)".to_string(),
        ]);
        assert!(p.calls.is_empty(), "the shared platform must not be used for a DPU");
    }

    /// And the two machines read a removed row oppositely, which is the thing
    /// that would power a DPU *on* if it were got backwards.
    #[test]
    fn a_removed_row_is_applied_oppositely_on_the_two_machines() {
        let mut p = RecordingPlatform::default();
        let handed = std::cell::RefCell::new(Vec::new());
        apply_config_event(&mut p, Machine::Chassis, "LINE-CARD0", config_updater::Event::Del,
                           &recording_gracefully(&handed));
        apply_config_event(&mut p, Machine::SmartSwitch, "DPU0", config_updater::Event::Del,
                           &recording_gracefully(&handed));
        assert_eq!(p.calls, vec!["admin(LINE-CARD0,true)".to_string()]);
        assert_eq!(handed.into_inner(), vec!["graceful(DPU0,false)".to_string()]);
    }

    /// Start-up helpers: one DPU, and a config table that may or may not hold
    /// a row for it.
    fn startup_fixture(oper: ModuleStatus) -> (Vec<ModuleInfo>, [pmon_common::db::MockTable; 6]) {
        use pmon_common::db::MockTable;
        let dpu = ModuleInfo {
            name: "DPU0".to_string(),
            presence: true,
            r#type: Some(platform_api::ModuleType::Dpu),
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
        let shut = std::cell::RefCell::new(Vec::new());

        init_dpu_admin_state(&mut p, &modules, &tables, Path::new("/nonexistent"),
                             &|n: &[String]| shut.borrow_mut().extend_from_slice(n));

        assert_eq!(p.calls, vec!["unlock(DPU0)".to_string(), "clear_halt(DPU0)".to_string()],
            "the flags are cleared on the daemon's own handle");
        assert_eq!(*shut.borrow(), vec!["DPU0".to_string()],
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
            let shut = std::cell::RefCell::new(Vec::new());

            init_dpu_admin_state(&mut p, &modules, &tables, Path::new("/nonexistent"),
                                 &|n: &[String]| shut.borrow_mut().extend_from_slice(n));

            assert!(shut.borrow().is_empty(),
                "admin_status={admin} is configured; start-up must not override it");
        }
    }

    /// A database that cannot be read is not evidence of an unconfigured DPU.
    /// Guessing would power one off because CONFIG_DB was briefly away.
    #[test]
    fn an_unreadable_config_table_shuts_nothing_down() {
        let (modules, t) = startup_fixture(ModuleStatus::Online);
        let tables = dpu_updater::Tables {
            chassis: &t[0], module: &t[1], midplane: &t[2],
            dpu_state: &t[3],
            config: None, device_metadata: None,
        };
        let mut p = RecordingPlatform::default();
        let shut = std::cell::RefCell::new(Vec::new());

        init_dpu_admin_state(&mut p, &modules, &tables, Path::new("/nonexistent"),
                             &|n: &[String]| shut.borrow_mut().extend_from_slice(n));

        assert!(shut.borrow().is_empty());
        assert!(p.calls.iter().any(|c| c == "unlock(DPU0)"),
            "the flags are still cleared; only the shutdown is withheld");
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
        let shut = std::cell::RefCell::new(Vec::new());

        init_dpu_admin_state(&mut p, &modules, &tables, Path::new("/nonexistent"),
                             &|n: &[String]| shut.borrow_mut().extend_from_slice(n));

        assert!(log.logged(log::Level::Error, "Unexpected error: connection reset"));
        assert_eq!(*shut.borrow(), vec!["DPU0".to_string()]);
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

            init_dpu_admin_state(&mut p, &modules, &tables, Path::new("/nonexistent"), &|_: &[String]| {});

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
        use platform_api::{ModuleInfoCols, ModuleStatus, ModuleType};
        use pmon_common::db::MockTable;
        tokio::time::pause();

        #[derive(Default)]
        struct Dpus {
            modules: Vec<ModuleInfo>,
            fail: bool,
        }
        impl PlatformApi for Dpus {
            fn get_modules(&mut self, _cols: ModuleInfoCols) -> Result<Vec<ModuleInfo>, PlatformError> {
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
        let code =
            run_smartswitch_loop(&mut p, &mut u, &tables, None, &mut cycles, &AtomicBool::new(false))
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
        use pmon_common::db::MockTable;
        tokio::time::pause();

        struct Answering;
        impl PlatformApi for Answering {
            fn get_chassis_info(&mut self, _cols: ChassisInfoCols) -> Result<platform_api::ChassisInfo, PlatformError> {
                Ok(platform_api::ChassisInfo {
                    dataplane_state: Some(true),
                    controlplane_state: Some(true),
                    ..Default::default()
                })
            }
        }

        let t = MockTable::new();
        let mut cycles = Cycles::Fixed { remaining: 2, code: 143 };
        let code = run_dpu_loop(&mut Answering, "DPU0", None, Sources::default(), &t, &mut cycles).await;

        assert_eq!(code, 143);
        assert_eq!(t.field("DPU0", dpu_updater::DP_STATE).as_deref(), Some("down"));
        assert_eq!(t.field("DPU0", dpu_updater::CP_STATE).as_deref(), Some("down"));
    }

    /// A platform that cannot answer at all reports both planes down rather
    /// than nothing: the NPU reads the absence of a row as a DPU it has never
    /// heard from, which is a different thing from one that is failing.
    #[tokio::test]
    async fn a_dpu_whose_platform_says_nothing_still_reports() {
        use pmon_common::db::MockTable;
        tokio::time::pause();

        struct Silent;
        impl PlatformApi for Silent {}

        let t = MockTable::new();
        let mut cycles = Cycles::Fixed { remaining: 1, code: 0 };
        run_dpu_loop(&mut Silent, "DPU0", None, Sources::default(), &t, &mut cycles).await;
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
        let p = dpu_planes(Some(&info), None, None, None).unwrap();
        assert!(p.data && !p.control);
    }

    /// And falls back to the databases when it cannot.
    #[test]
    fn a_dpu_falls_back_to_the_databases() {
        use pmon_common::db::MockTable;
        let (cfg, app, ready) = (MockTable::new(), MockTable::new(), MockTable::new());
        cfg.set("Ethernet0", &[("admin_status", "up".to_string())]).unwrap();
        app.set("Ethernet0", &[("oper_status", "up".to_string())]).unwrap();
        ready.set("SYSTEM_STATE", &[("Status", "UP".to_string())]).unwrap();

        let p = dpu_planes(None, Some(&cfg), Some(&app), Some(&ready)).unwrap();
        assert!(p.data && p.control);

        app.set("Ethernet0", &[("oper_status", "down".to_string())]).unwrap();
        let p = dpu_planes(None, Some(&cfg), Some(&app), Some(&ready)).unwrap();
        assert!(!p.data && p.control, "one port down is the data plane down");
    }

    /// With neither source there is nothing to claim, and claiming `up` would
    /// have the NPU believe a DPU that has said nothing.
    #[test]
    fn a_dpu_that_can_answer_from_neither_source_reports_down() {
        let p = dpu_planes(None, None, None, None).unwrap();
        assert!(!p.data && !p.control);
    }

    /// A pass over the DPUs, driven end to end: the module row is published,
    /// the midplane state lands in DPU_STATE, and an unconfigured DPU is not
    /// power cycled.
    #[test]
    fn a_smartswitch_pass_publishes_and_leaves_an_unconfigured_dpu_alone() {
        use platform_api::{ModuleInfo, ModuleStatus, ModuleType};
        use pmon_common::db::MockTable;

        struct NoHw(usize);
        impl dpu_recovery::PowerCycler for NoHw {
            fn power_cycle(&mut self, _m: &str) -> bool {
                self.0 += 1;
                true
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

    /// A value of the wrong shape is ignored rather than taken as zero: a
    /// `dpu_reset_limit` of 0 would mark every DPU unrecoverable on its first
    /// failure.
    #[test]
    fn a_threshold_of_the_wrong_type_is_ignored() {
        let t = thresholds_from(Some(r#"{"dpu_reset_limit": "two"}"#));
        assert_eq!(t.reset_limit, dpu_recovery::Thresholds::default().reset_limit);
    }

    /// The platform answers for itself when it can.  Asked per cycle, because
    /// one that could not answer at start-up may be able to later.
    #[test]
    fn a_platform_that_answers_for_a_plane_is_believed() {
        assert!(plane_state(Some(true), || panic!("the fallback must not run")));
        assert!(!plane_state(Some(false), || panic!("the fallback must not run")));
        assert!(plane_state(None, || true));
        assert!(!plane_state(None, || false));
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
        let code = run_chassis(&mut p, &mut u, &db.tables(), true, &mut cycles, &AtomicBool::new(false))
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
    fn no_shutdown(_names: &[String]) {}

    fn no_watcher(_w: Watch, _db_lost: Arc<AtomicBool>) -> tokio::task::JoinHandle<()> {
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
        fn get_chassis_info(&mut self, _cols: ChassisInfoCols) -> Result<platform_api::ChassisInfo, PlatformError> {
            self.info.clone().ok_or(PlatformError::Backend("no chassis".into()))
        }
        fn get_modules(&mut self, _cols: ModuleInfoCols) -> Result<Vec<ModuleInfo>, PlatformError> {
            Ok(self.modules.clone())
        }
        fn init_midplane_switch(&mut self) -> Result<(), PlatformError> {
            if self.midplane_ok {
                Ok(())
            } else {
                Err(PlatformError::Backend("midplane".into()))
            }
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

    /// The failure that put chassisd in FATAL on r-bobcat-01.  A platform that
    /// raises must not be read as a fixed switch: the two answers are
    /// "something is wrong" and "there is nothing to do here", and supervisord
    /// treats them differently.
    #[tokio::test]
    async fn a_chassis_that_will_not_describe_itself_is_a_load_error() {
        let log = pmon_common::logging::capture();
        let o = pmon_common::db::MockOpener::new();
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

    /// A fixed switch answers neither slot.  Python exits with its own code so
    /// supervisord reports it as configured-off rather than crashed.
    #[tokio::test]
    async fn a_fixed_switch_is_not_supported_rather_than_broken() {
        let log = pmon_common::logging::capture();
        let o = pmon_common::db::MockOpener::new();
        let mut p = Shaped { info: Some(platform_api::ChassisInfo::default()), ..Default::default() };
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

    /// A DPU takes the DPU shape, and the DPU shape opens DPU_STATE on
    /// CHASSIS_STATE_DB -- the database a DPU has no local instance of, which
    /// is why the transport had to be TCP.
    #[tokio::test]
    async fn a_dpu_takes_the_dpu_shape() {
        tokio::time::pause();
        let o = pmon_common::db::MockOpener::new();
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
        let o = pmon_common::db::MockOpener::failing("DPU_STATE");
        let code = run_on_dpu(
            &mut Shaped::default(),
            None,
            &|d, t| o.open(d, t),
            &mut Cycles::Fixed { remaining: 0, code: 0 },
        )
        .await;
        assert_eq!(code, CHASSIS_LOAD_ERROR);
    }

    /// The supervisor shape, end to end: the tables are opened, the module
    /// count is published, the watcher is started, and the rows are cleared on
    /// the way out.
    #[tokio::test]
    async fn the_supervisor_publishes_its_card_count_and_clears_on_the_way_out() {
        tokio::time::pause();
        let o = pmon_common::db::MockOpener::new();
        let mut p = Shaped {
            info: Some(supervisor_info()),
            modules: vec![card("LINE-CARD0", 1, ModuleStatus::Online)],
            midplane_ok: true,
        };
        let code = run_modular(
            &mut p,
            Some(&supervisor_info()),
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
        assert!(asked.contains(&("CHASSIS_STATE_DB".to_string(), "CHASSIS_MODULE_TABLE".to_string())));
        // And nothing is left behind.  A stale CHASSIS_MODULE_TABLE row reads
        // as a card that is still there to everything downstream, and a stale
        // `module_num` to the entity MIB; clearing both is what the Python
        // daemon does from __del__.
        assert!(o.table("CHASSIS_MODULE_TABLE").unwrap().get("LINE-CARD0").unwrap().is_none());
        assert!(o.table("CHASSIS_TABLE").unwrap().get(CHASSIS_INFO_KEY).unwrap().is_none());
    }

    /// A table that will not open is a load error, not a daemon that runs with
    /// a handle it cannot write through.
    #[tokio::test]
    async fn a_modular_chassis_with_a_missing_table_stops() {
        let o = pmon_common::db::MockOpener::failing("PHYSICAL_ENTITY_INFO");
        let code = run_modular(
            &mut Shaped { info: Some(supervisor_info()), ..Default::default() },
            Some(&supervisor_info()),
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
        use platform_api::ModuleType;
        tokio::time::pause();
        let o = pmon_common::db::MockOpener::new();
        let modules = vec![ModuleInfo {
            name: "DPU0".to_string(),
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
        // Both CONFIG_DB reads are optional, but they have to be asked for:
        // `dpu_auto_recovery` lives in DEVICE_METADATA and a daemon that never
        // opened it would recover DPUs on a switch that turned it off.
        assert!(o.asked().iter().any(|(d, t)| d == CONFIG_DB && t == "DEVICE_METADATA"));
    }

    /// And one whose tables will not open stops with the load error rather
    /// than the not-supported one -- supervisord restarts on one and not the
    /// other.
    #[tokio::test]
    async fn a_smartswitch_with_a_missing_table_stops() {
        let o = pmon_common::db::MockOpener::failing("DPU_STATE");
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
        calls: Vec<String>,
    }

    impl PlatformApi for Refusing {
        fn set_module_state_transition(&mut self, m: &str, _t: &str) -> Result<(), PlatformError> {
            self.calls.push(format!("lock({m})"));
            Ok(())
        }
        fn clear_module_state_transition(&mut self, m: &str) -> Result<(), PlatformError> {
            self.calls.push(format!("unlock({m})"));
            Ok(())
        }
        fn module_pre_shutdown(&mut self, m: &str) -> Result<(), PlatformError> {
            self.calls.push(format!("pre({m})"));
            self.answer("module_pre_shutdown")
        }
        fn module_post_startup(&mut self, m: &str) -> Result<(), PlatformError> {
            self.calls.push(format!("post({m})"));
            self.answer("module_post_startup")
        }
        fn set_module_admin_state(&mut self, m: &str, up: bool) -> Result<(), PlatformError> {
            self.calls.push(format!("admin({m},{up})"));
            self.answer("set_module_admin_state")
        }
        fn set_module_admin_state_gracefully(&mut self, m: &str, up: bool)
            -> Result<(), PlatformError>
        {
            self.calls.push(format!("graceful({m},{up})"));
            self.answer("set_module_admin_state_gracefully")
        }
    }

    impl Refusing {
        fn answer(&self, what: &str) -> Result<(), PlatformError> {
            if self.refuse == what {
                return Err(PlatformError::Backend("refused".into()));
            }
            Ok(())
        }
    }

    /// A power cycle that half fails still finishes and still releases the
    /// lock.  A lock left held blocks every later operation on that DPU,
    /// including the operator's `module shutdown` -- so the failure of one step
    /// must not cost the release, and the sequence must not stop at the first
    /// error either: the DPU has already had its power cut by then.
    #[test]
    fn a_power_cycle_that_half_fails_still_releases_the_lock() {
        for refuse in ["module_pre_shutdown", "set_module_admin_state", "module_post_startup"] {
            let mut p = Refusing { refuse, ..Default::default() };
            assert!(power_cycle_sequence(&mut p, "DPU0"), "the lock was taken, so this is true");
            assert_eq!(
                p.calls.last().map(String::as_str),
                Some("unlock(DPU0)"),
                "{refuse} failing must not leave the lock held"
            );
            assert_eq!(p.calls.len(), 6, "{refuse} failing must not stop the sequence");
        }
    }

    /// The hardware refused the admin-state write.  It is logged and the
    /// daemon carries on: the next CONFIG_DB event is still worth applying,
    /// and a watcher that stopped here would leave the switch un-administered
    /// with nothing but one line in the log to say so.
    #[test]
    fn a_refused_admin_state_is_logged_and_not_fatal() {
        let mut p = Refusing { refuse: "set_module_admin_state", ..Default::default() };
        let handed = std::cell::RefCell::new(Vec::new());
        apply_config_event(&mut p, Machine::Chassis, "LINE-CARD0", config_updater::Event::Del,
                           &recording_gracefully(&handed));
        apply_config_event(&mut p, Machine::Chassis, "LINE-CARD1", config_updater::Event::Del,
                           &recording_gracefully(&handed));
        assert_eq!(p.calls, vec![
            "admin(LINE-CARD0,true)".to_string(),
            "admin(LINE-CARD1,true)".to_string(),
        ]);
    }

    /// A midplane that does not come up is reported and not fatal: the module
    /// table is still worth publishing on a chassis whose midplane is down,
    /// and refusing to start would take the only view of it with it.
    #[tokio::test]
    async fn a_midplane_that_does_not_come_up_does_not_stop_the_daemon() {
        let log = pmon_common::logging::capture();
        tokio::time::pause();
        let o = pmon_common::db::MockOpener::new();
        let mut p = Shaped {
            info: Some(supervisor_info()),
            modules: vec![card("LINE-CARD0", 1, ModuleStatus::Online)],
            midplane_ok: false,
        };
        let code = run_modular(
            &mut p,
            Some(&supervisor_info()),
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
        let o = pmon_common::db::MockOpener::new();
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
        use platform_api::ModuleType;
        use pmon_common::db::{MockKeyspace, MockTable};

        struct NoHw;
        impl dpu_recovery::PowerCycler for NoHw {
            fn power_cycle(&mut self, _m: &str) -> bool {
                true
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

        smartswitch_pass(&modules, &mut NoHw, &mut u, &tables, Some(&keys), Instant::now()).unwrap();

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
            presence: true,
            r#type: Some(platform_api::ModuleType::Dpu),
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
        let o = pmon_common::db::MockOpener::new();
        o.open(db::STATE_DB, CHASSIS_INFO_TABLE).unwrap();
        o.table(CHASSIS_INFO_TABLE).unwrap().fail_writes("connection reset");
        let mut p = Shaped {
            info: Some(supervisor_info()),
            modules: vec![card("LINE-CARD0", 1, ModuleStatus::Online)],
            midplane_ok: true,
        };
        let code = run_modular(
            &mut p,
            Some(&supervisor_info()),
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
        let o = pmon_common::db::MockOpener::new();
        o.open(db::STATE_DB, CHASSIS_INFO_TABLE).unwrap();
        o.table(CHASSIS_INFO_TABLE).unwrap().fail_writes("connection reset");
        let mut p = Shaped { modules: vec![online_dpu0()], midplane_ok: true, ..Default::default() };
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
        let o = pmon_common::db::MockOpener::new();
        o.open(CHASSIS_STATE_DB, dpu_updater::DPU_STATE_TABLE).unwrap();
        o.table(dpu_updater::DPU_STATE_TABLE).unwrap().fail_writes("connection reset");
        let shut = std::cell::RefCell::new(Vec::<String>::new());
        let record = |names: &[String]| shut.borrow_mut().extend(names.iter().cloned());
        let mut p = Shaped { modules: vec![online_dpu0()], midplane_ok: true, ..Default::default() };
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
        assert_eq!(*shut.borrow(), vec!["DPU0".to_string()]);
    }

    /// The recovery machine's seed is the next write to DPU_STATE; one that
    /// is refused stops the daemon before the loop.
    #[tokio::test]
    async fn a_recovery_seed_that_will_not_write_stops() {
        let log = pmon_common::logging::capture();
        let o = pmon_common::db::MockOpener::new();
        o.open(CHASSIS_STATE_DB, dpu_updater::DPU_STATE_TABLE).unwrap();
        // The admin-state sweep's one row goes through; the recovery seed's
        // first does not.
        o.table(dpu_updater::DPU_STATE_TABLE).unwrap().fail_writes_after(1, "connection reset");
        let mut p = Shaped { modules: vec![online_dpu0()], midplane_ok: true, ..Default::default() };
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
        chassis: pmon_common::db::MockTable,
        module: pmon_common::db::MockTable,
        midplane: pmon_common::db::MockTable,
        dpu_state: pmon_common::db::MockTable,
        updater: dpu_updater::DpuUpdater,
    }

    impl Npu {
        /// One online DPU.
        fn new() -> Self {
            use pmon_common::db::MockTable;
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
            let mut p = Shaped { modules: vec![online_dpu0()], midplane_ok: true, ..Default::default() };
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
        use pmon_common::db::MockTable;
        tokio::time::pause();
        struct Silent;
        impl PlatformApi for Silent {}

        let (config_ports, port_table, t) = (MockTable::new(), MockTable::new(), MockTable::new());
        config_ports.fail_reads("connection reset");
        let sources = Sources {
            config_ports: Some(&config_ports),
            port_table: Some(&port_table),
            system_ready: None,
        };
        let mut cycles = Cycles::Fixed { remaining: 5, code: 143 };
        let code = run_dpu_loop(&mut Silent, "DPU0", None, sources, &t, &mut cycles).await;
        assert_eq!(code, ERR_DB_WRITE);
        // The shutdown row is still written on the way out: the process is
        // going, and that part the NPU must hear.
        assert_eq!(t.field("DPU0", dpu_updater::DP_STATE).as_deref(), Some("down"));
    }

    /// A DPU_STATE that will not take the planes stops the DPU's loop.
    #[tokio::test]
    async fn a_dpu_whose_state_table_refuses_the_planes_stops() {
        let log = pmon_common::logging::capture();
        tokio::time::pause();
        struct Answering;
        impl PlatformApi for Answering {
            fn get_chassis_info(&mut self, _cols: ChassisInfoCols) -> Result<platform_api::ChassisInfo, PlatformError> {
                Ok(platform_api::ChassisInfo {
                    dataplane_state: Some(true),
                    controlplane_state: Some(true),
                    ..Default::default()
                })
            }
        }
        let t = pmon_common::db::MockTable::new();
        t.fail_writes("connection reset");
        let mut cycles = Cycles::Fixed { remaining: 5, code: 143 };
        let code = run_dpu_loop(&mut Answering, "DPU0", None, Sources::default(), &t, &mut cycles).await;
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
        let db_lost = AtomicBool::new(false);
        let data = || Ok(swss_common::SelectResult::Data);
        let feed = Feed::new(
            vec![data()],
            vec![Ok(vec![
                boot("DPU0", Some("b-1"), false),
                boot("DPU1", None, false),
                boot("DPU2", Some("b-9"), true),
            ])],
        );
        let mut seen = Vec::new();
        watch_boot_ids(&feed, &db_lost, &mut |name, boot_id| seen.push(format!("{name}:{boot_id}")));
        assert_eq!(seen, vec!["DPU0:b-1".to_string()]);
        assert!(!db_lost.load(Ordering::SeqCst));
    }

    /// A lost subscription is the same lost redis as the config watcher's,
    /// and raises the same flag.
    #[test]
    fn a_boot_id_watcher_whose_pops_fail_raises_the_flag() {
        let db_lost = AtomicBool::new(false);
        let feed = Feed::new(
            vec![Ok(swss_common::SelectResult::Data)],
            vec![Err("connection reset".into())],
        );
        watch_boot_ids(&feed, &db_lost, &mut |_, _| panic!("nothing to capture"));
        assert!(db_lost.load(Ordering::SeqCst));
    }

    /// A SmartSwitch's NPU starts both subscriptions, the config watcher and
    /// the boot_id watcher `chassisd:RebootCauseSubscriberTask` is.
    #[tokio::test]
    async fn the_npu_side_watches_config_and_boot_ids() {
        use platform_api::ModuleType;
        tokio::time::pause();
        let o = pmon_common::db::MockOpener::new();
        let modules = vec![ModuleInfo {
            name: "DPU0".to_string(),
            presence: true,
            r#type: Some(ModuleType::Dpu),
            oper_status: Some(ModuleStatus::Online),
            is_midplane_reachable: Some(true),
            ..Default::default()
        }];
        let mut p = Shaped { info: None, modules, midplane_ok: true };
        let started = std::sync::Mutex::new(Vec::new());
        let recording = |w: Watch, _db_lost: Arc<AtomicBool>| {
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
        use pmon_common::db::MockTable;
        tokio::time::pause();
        struct Silent;
        impl PlatformApi for Silent {}
        let t = MockTable::new();
        let mut cycles = Cycles::Fixed { remaining: 1, code: 143 };
        let code = run_dpu_loop(&mut Silent, "DPU0", Some("b-1"), Sources::default(), &t, &mut cycles)
            .await;
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
                module: &str,
            ) -> Result<platform_api::ModuleMidplaneDownReason, platform_api::PlatformError> {
                Ok(platform_api::ModuleMidplaneDownReason {
                    reason: Some(format!("{module} lost power")),
                    detail: None,
                })
            }
        }
        let mut p = Says;
        let mut hw = PlatformCycler { platform: &mut p };
        let why = dpu_recovery::PowerCycler::midplane_down_reason(&mut hw, "DPU0").unwrap();
        assert_eq!(why.reason.as_deref(), Some("DPU0 lost power"));
    }

    /// A boot_id that will not publish stops the DPU loop before it starts,
    /// the way a plane that will not publish stops it.
    #[tokio::test]
    async fn a_boot_id_that_will_not_publish_stops_the_dpu_loop() {
        use pmon_common::db::MockTable;
        let log = pmon_common::logging::capture();
        tokio::time::pause();
        struct Silent;
        impl PlatformApi for Silent {}
        let t = MockTable::new();
        t.fail_writes("redis is gone");
        let mut cycles = Cycles::Fixed { remaining: 1, code: 143 };
        let code = run_dpu_loop(&mut Silent, "DPU0", Some("b-1"), Sources::default(), &t, &mut cycles)
            .await;
        assert_eq!(code, ERR_DB_WRITE);
        assert!(log.logged(log::Level::Error, "Failed to publish the boot_id for DPU0"));
    }
}
