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
mod logging;
mod monitor;
mod polling;
mod temp_updater;

use clap::Parser;
use std::time::Duration;
use tokio::signal::unix::{signal, SignalKind};
use tokio::sync::watch;

use platform_traits::PlatformApi;

use db::StateDb;
use monitor::Monitor;
use sonic_platform::Platform;

const SYSLOG_IDENTIFIER: &str = "thermalctld";
const CHASSIS_GET_ERROR: i32 = 2;

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
    about = "SONiC thermal control daemon (native Mellanox sysfs)"
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
}

fn init_logging() {
    logging::init(SYSLOG_IDENTIFIER);
}

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
    let mut platform = match Platform::new() {
        Ok(p) => p,
        Err(e) => {
            log::error!("Failed to initialise platform: {e}");
            std::process::exit(CHASSIS_GET_ERROR);
        }
    };

    let chassis = platform.chassis_info().ok();
    let slot_or_dpu_id = chassis.as_ref().and_then(|i| i.slot_or_dpu_id);
    // The leak thread runs only where the platform has sensors and the flag is
    // set, which is how Python gates it too.
    let leak_enabled = args.enable_liquid_cooling && chassis.as_ref().is_some_and(|i| i.is_liquid_cooled);

    // Open STATE_DB tables.  The leak tables are not among them: the leak
    // thread is their only writer and opens them itself, on its own cadence.
    let db = match StateDb::open(slot_or_dpu_id) {
        Ok(db) => db,
        Err(e) => {
            log::error!("Failed to open STATE_DB due to {e:?}");
            std::process::exit(CHASSIS_GET_ERROR);
        }
    };

    // Log what was read above rather than reading the platform a second time,
    // which could report something other than what gated the two decisions.
    match chassis.as_ref() {
        Some(info) => log::info!(
            "Platform ready (modular={}, smartswitch={}, dpu={}, liquid_cooled={})",
            info.is_modular_chassis,
            info.is_smartswitch,
            info.is_dpu,
            info.is_liquid_cooled
        ),
        None => log::warn!("chassis_info failed"),
    }

    // Obtain and initialise the platform-specific thermal manager.
    // Mirrors Python: `thermal_manager = chassis.get_thermal_manager()`
    //                 `thermal_manager.initialize()`
    let mut thermal_manager = platform.get_thermal_manager();
    if let Err(e) = thermal_manager.initialize() {
        log::warn!("ThermalManager initialize failed: {e}");
    }

    // Signal handling: SIGTERM and SIGINT both trigger a graceful shutdown.
    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    tokio::spawn(async move {
        let mut sigterm = signal(SignalKind::terminate()).expect("failed to install SIGTERM handler");
        let mut sigint = signal(SignalKind::interrupt()).expect("failed to install SIGINT handler");
        tokio::select! {
            _ = sigterm.recv() => log::info!("caught SIGTERM, shutting down"),
            _ = sigint.recv()  => log::info!("caught SIGINT, shutting down"),
        }
        let _ = shutdown_tx.send(true);
    });

    // Leak detection runs on its own thread at its own cadence — 0.5 s on the
    // platforms that set it — because it is a hundred times faster than the
    // poll cycle and a slow sysfs read must not delay either side.
    let leak_thread = if leak_enabled {
        let interval = Duration::from_secs_f64(args.liquid_cooling_update_interval);
        let is_bmc = device_env::is_switch_bmc();
        let leak_shutdown = shutdown_rx.clone();
        // Its own platform handle and its own tables: the monitor holds the
        // other one mutably, and each thread owning its DB connections is what
        // the rest of this daemon already does.
        // Leak detection is a safety function, so a thread that fails to start
        // is reported rather than quietly leaving the platform unmonitored.
        match std::thread::Builder::new()
            .name("leak-updater".into())
            .spawn(move || leak_thread_main(interval, is_bmc, leak_shutdown))
        {
            Ok(handle) => Some(handle),
            Err(e) => {
                log::error!("failed to start the leak updater thread, leak monitoring is off: {e}");
                None
            }
        }
    } else {
        None
    };

    // Main polling loop.
    // Mirrors Python: `ThermalMonitor(ThermalUpdater(), FanUpdater()).task_worker()`
    let mut monitor = Monitor::new(
        args.thermal_monitor_initial_interval,
        args.thermal_monitor_update_interval,
        args.thermal_monitor_update_elapsed_threshold,
    );
    monitor
        .run(&mut platform, &mut *thermal_manager, &db, shutdown_rx)
        .await;

    if let Some(h) = leak_thread {
        let _ = h.join();
    }

    // Cleanup: restores hw-management-tc autonomous mode (Mellanox).
    // Mirrors Python: `ThermalManager.deinitialize()`
    thermal_manager.deinitialize();
}

/// The leak thread: read every sensor, run the state machine, write the tables.
fn leak_thread_main(interval: Duration, is_switch_bmc: bool, shutdown: watch::Receiver<bool>) {
    let platform = match Platform::new() {
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
    leak_loop(&platform, &tables, interval, is_switch_bmc, shutdown);
}

/// The leak thread's body, with the platform and the tables already opened.
///
/// The two steps above it — building a `Platform` and opening the three tables
/// — reach hardware and redis; everything that decides *what is published* is
/// here, where it can be driven.
fn leak_loop(
    platform: &dyn platform_traits::PlatformApi,
    tables: &db::LeakTables,
    interval: Duration,
    is_switch_bmc: bool,
    shutdown: watch::Receiver<bool>,
) {
    let profiles = platform.get_leak_profiles();
    leak_updater::publish_profiles_and_seed(tables, &profiles);

    let mut state = leak_updater::LeakState::new();
    let mut events = event_log::BmcEventLogger::new(is_switch_bmc);

    loop {
        if *shutdown.borrow() {
            break;
        }
        let sensors = platform.get_leak_sensors();
        let outcome = state.refresh(&sensors, &profiles, std::time::Instant::now(), &mut events);
        let unwritten = leak_updater::apply(tables, &outcome);
        state.forget(&unwritten);
        if sleep_or_shutdown(interval, &shutdown) {
            break;
        }
    }
    log::info!("leak updater stopped");
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

// ── Tests ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

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
    use platform_traits::{
        ChassisInfo, FanDrawerInfo, FanInfo, LeakProfile, LeakSensorInfo, PlatformError, ThermalInfo, ThermalManager,
    };

    struct FakePlatform {
        profiles: Vec<LeakProfile>,
        sensors: Vec<LeakSensorInfo>,
    }

    impl platform_traits::PlatformApi for FakePlatform {
        fn chassis_info(&self) -> Result<ChassisInfo, PlatformError> {
            Ok(ChassisInfo::default())
        }
        fn get_thermals(&mut self) -> Result<Vec<ThermalInfo>, PlatformError> {
            Ok(Vec::new())
        }
        fn get_fan_drawers(&self) -> Result<Vec<FanDrawerInfo>, PlatformError> {
            Ok(Vec::new())
        }
        fn get_fans(&self) -> Result<Vec<FanInfo>, PlatformError> {
            Ok(Vec::new())
        }
        fn set_fan_led(&mut self, _: &str, _: &str, _: &str) -> Result<(), PlatformError> {
            Ok(())
        }
        fn get_leak_profiles(&self) -> Vec<LeakProfile> {
            self.profiles.clone()
        }
        fn get_leak_sensors(&self) -> Vec<LeakSensorInfo> {
            self.sensors.clone()
        }
        fn get_thermal_manager(&self) -> Box<dyn ThermalManager> {
            unimplemented!("the leak thread has none")
        }
    }

    fn sensor(name: &str, is_leak: bool) -> LeakSensorInfo {
        LeakSensorInfo {
            name: name.to_string(),
            is_ok: true,
            is_leak,
            severity: None,
            profile_type: None,
            sensor_type: "leakage".to_string(),
            location: "chassis".to_string(),
        }
    }

    /// Before its first poll the leak thread publishes the profiles and seeds
    /// `SYSTEM_LEAK_STATUS|system`, so a consumer reading between start-up and
    /// the first cycle sees a status rather than nothing.  The per-sensor rows
    /// come from the first poll, not from the seeding.
    #[test]
    fn the_leak_loop_seeds_the_system_status_before_its_first_poll() {
        let m = MockLeak::new();
        let p = FakePlatform {
            profiles: vec![LeakProfile {
                profile_type: "chassis".to_string(),
                max_minor_duration_sec: None,
            }],
            sensors: vec![sensor("leakage1", false)],
        };

        // Shut down before the first poll: what is in the tables is exactly
        // what the seeding step put there.
        let (tx, rx) = watch::channel(false);
        tx.send(true).unwrap();
        leak_loop(&p, &m.tables, Duration::from_millis(1), false, rx);

        assert_eq!(
            m.profile.field("chassis", "max_minor_duration_sec").as_deref(),
            Some("inf"),
            "a profile with no escalation timer publishes Python's inf"
        );
        assert_eq!(m.system.field("system", "device_leak_status").as_deref(), Some("None"));
        assert!(m.sensor.is_empty(), "the sensor rows wait for the first poll");
    }

    /// Seeding happens *only* when the status is absent.  A daemon restarting
    /// while a leak is standing must not clear it — the water is still there,
    /// and a cleared status is an alarm that silently went away.
    #[test]
    fn a_restart_does_not_clear_a_standing_leak_status() {
        let m = MockLeak::new();
        crate::db::TableLike::set(&m.system, "system", &[("device_leak_status", "CRITICAL".to_string())]).unwrap();

        let p = FakePlatform {
            profiles: Vec::new(),
            sensors: Vec::new(),
        };
        let (tx, rx) = watch::channel(false);
        tx.send(true).unwrap();
        leak_loop(&p, &m.tables, Duration::from_millis(1), false, rx);

        assert_eq!(
            m.system.field("system", "device_leak_status").as_deref(),
            Some("CRITICAL"),
            "the standing status survived the restart"
        );
    }

    /// One pass reaches the tables with the sensors' actual state.
    #[test]
    fn one_leak_pass_publishes_what_the_sensors_report() {
        let m = MockLeak::new();
        let p = FakePlatform {
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
        leak_loop(&p, &m.tables, Duration::from_millis(1), false, rx);

        assert_eq!(
            m.sensor.field("leakage1", "leaking").as_deref(),
            Some("Yes"),
            "the leak reached the table"
        );
    }

    /// A platform with no leak sensors publishes no rows at all, rather than an
    /// empty-but-present table — an air-cooled box has nothing to say here.
    #[test]
    fn a_platform_with_no_leak_sensors_publishes_nothing() {
        let m = MockLeak::new();
        let p = FakePlatform {
            profiles: Vec::new(),
            sensors: Vec::new(),
        };
        let (tx, rx) = watch::channel(false);
        tx.send(true).unwrap();
        leak_loop(&p, &m.tables, Duration::from_millis(1), false, rx);

        assert!(m.sensor.is_empty());
        assert!(m.profile.is_empty());
        assert!(
            !m.system.is_empty(),
            "the system row is seeded regardless, so the field always exists"
        );
    }
}
