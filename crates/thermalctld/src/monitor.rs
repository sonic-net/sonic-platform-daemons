//! Port of ThermalMonitor's polling loop.

use std::time::{Duration, Instant};

use tokio::sync::watch;

use platform_api::PlatformApi;

use crate::bmc::ChassisThermalWatch;
use crate::db::StateDb;
use crate::event_log::BmcEventLogger;
use crate::fan_updater::FanUpdater;
use crate::polling::{PollingGate, PollingIntervals};
use crate::temp_updater::TemperatureUpdater;
use crate::ERR_DB_WRITE;

pub struct Monitor {
    initial_interval: f64,
    update_interval: f64,
    update_elapsed_threshold: f64,
    fan_updater: FanUpdater,
    temperature_updater: TemperatureUpdater,
    /// Per-component polling intervals from platform.json.
    intervals: PollingIntervals,
    /// Tracks when the fan update was last due.
    fan_gate: PollingGate,
    /// On the BMC, the watch over the mirror the switch host pushes.
    bmc_watch: Option<(ChassisThermalWatch, BmcEventLogger)>,
}

impl Monitor {
    pub fn new(initial_interval: f64, update_interval: f64, update_elapsed_threshold: f64) -> Self {
        Self::with_env(
            initial_interval,
            update_interval,
            update_elapsed_threshold,
            PollingIntervals::load(),
            // Only the copy running on the BMC watches the mirror; the switch
            // host writes it instead.
            crate::device_env::is_switch_bmc(),
        )
    }

    /// The same monitor with the two environment lookups already made.
    ///
    /// `new()` reads `platform.json` and the platform env conf; both are
    /// absolute paths, and taking their results as arguments is what lets the
    /// loop below — the ordering, the gating and the wait arithmetic — be
    /// driven from a test.
    pub fn with_env(
        initial_interval: f64,
        update_interval: f64,
        update_elapsed_threshold: f64,
        intervals: PollingIntervals,
        is_switch_bmc: bool,
    ) -> Self {
        // platform.json can shrink the cycle below the 60 s default; the
        // gates below can only slow a component down, so an interval faster
        // than the cycle is unreachable until this runs.
        let mut intervals = intervals;
        let update_interval = intervals.resolve(update_interval);
        if is_switch_bmc {
            // `thermalctld:ThermalMonitor._init_switch_host_thermal_monitor`,
            // through the same INFO logger.  Nothing to open: the mirror is
            // this BMC's own STATE_DB, which the loop already reads.
            pmon_common::info_logger!(
                "Monitoring chassis thermals (BMC STATE_DB {}) for CRITICAL breaches",
                crate::db::TEMPERATURE_INFO
            );
        }
        Self {
            initial_interval,
            update_interval,
            update_elapsed_threshold,
            fan_updater: FanUpdater::new(),
            temperature_updater: TemperatureUpdater::new(),
            intervals,
            fan_gate: PollingGate::new(),
            bmc_watch: is_switch_bmc
                .then(|| (ChassisThermalWatch::new(), BmcEventLogger::new(true))),
        }
    }

    /// The same monitor with its temperature updater supplied, so a test can
    /// hand it one whose BMC mirror it can read back.
    #[cfg(test)]
    pub fn with_temperature_updater(mut self, updater: TemperatureUpdater) -> Self {
        self.temperature_updater = updater;
        self
    }

    /// Take the rows the temperature updater mirrored to the BMC back off it.
    ///
    /// For `main::run`'s shutdown, beside `StateDb::clear`: the mirror lives
    /// in the updater, and the updater lives here.
    pub fn clear_bmc_mirror(&mut self) {
        self.temperature_updater.clear_mirror();
    }

    /// Run the thermal monitoring loop.
    ///
    /// The first pass fires after the short `initial_interval` so STATE_DB is
    /// populated soon after boot; subsequent waits absorb the cycle's own
    /// duration to keep a steady period.
    ///
    /// An independent policy worker owns policy scheduling in the daemon.
    ///
    /// The manager is reached through the platform rather than held as an
    /// object: what the base class answers is a class, not an instance, and
    /// two of its methods take the chassis back as an argument. Keeping that
    /// on the Python side is what stops a policy round re-entering Rust.
    ///
    /// Telemetry never calls the policy or waits for its initialization.
    /// Returns the exit code to leave with when STATE_DB went away, and
    /// `None` when the loop ended for any other reason.
    ///
    /// Python does not arrive here at all: a write that raises under
    /// `thermalctld:ThermalMonitor.task_worker` ends that thread and leaves
    /// the policy loop running over frozen tables.  `main::ERR_DB_WRITE` says
    /// why this returns a code instead.
    pub async fn run(
        &mut self,
        platform: &mut dyn PlatformApi,
        db: &StateDb,
        mut shutdown: watch::Receiver<bool>,
    ) -> Option<i32> {
        // `ThermalMonitor`'s own logger, which Python sets to INFO: the two
        // INFO lines thermalctld sends.
        pmon_common::info_logger!("Start thermal monitoring loop");

        let mut wait_time = self.initial_interval;

        loop {
            if *shutdown.borrow() {
                break;
            }
            tokio::select! {
                _ = shutdown.changed() => break,
                _ = tokio::time::sleep(Duration::from_secs_f64(wait_time)) => {}
            }

            let begin = Instant::now();

            // Checked between devices inside both updaters, so a shutdown does
            // not wait out a pass.  Read through the receiver rather than a
            // captured flag: `changed()` above only fires once.
            let stopping = {
                let rx = shutdown.clone();
                move || *rx.borrow()
            };

            // platform.json may throttle the fan update to its own interval.
            // Every Mellanox device sets one shorter than the cycle, so this is
            // due every time today.
            if self
                .fan_gate
                .is_due("fan_drawer", self.intervals.fan, begin)
            {
                match self.fan_updater.update(&mut *platform, db, &stopping) {
                    Ok(()) => {}
                    Err(crate::fan_updater::UpdateError::Platform(e)) => {
                        log::warn!("fan update failed: {e}");
                    }
                    Err(crate::fan_updater::UpdateError::Db(e)) => {
                        log::error!("fan update failed: {e}");
                        return Some(ERR_DB_WRITE);
                    }
                }
            }

            // Temperature update: needs &mut self (updates min/max_recorded).
            // Each thermal may carry its own interval.
            if let Err(e) =
                self.temperature_updater
                    .update(platform, db, &self.intervals, begin, &stopping)
            {
                log::warn!("temperature update failed: {e}");
            }

            // On the BMC, check the mirrored chassis thermals for a critical
            // breach and tee it to the BMC event log.  Runs every cycle, right
            // after the temperature update, as Python's main() does.
            if let Some((watch, events)) = self.bmc_watch.as_mut() {
                let rows = crate::bmc::read_mirror(db.temperature.as_ref());
                watch.check(&rows, events);
            }

            let elapsed = begin.elapsed().as_secs_f64();
            // The fallback is not clamped to `update_interval`, though
            // `resolve` may have shrunk that below `initial_interval`; see
            // `pmon_common::cadence` for why that stays as Python has it.
            wait_time = pmon_common::cadence::next_wait(
                elapsed,
                self.update_interval,
                self.initial_interval,
            );

            if elapsed > self.update_elapsed_threshold {
                log::warn!(
                    "Update fan and temperature status took {elapsed} seconds, \
                     there might be performance risk"
                );
            }
        }

        pmon_common::info_logger!("Stop thermal monitoring loop");
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    use platform_api::{
        ChassisInfo, ChassisInfoCols, DeviceKey, FanDrawerInfo, FanDrawerInfoCols, FanInfo,
        FanInfoCols, PlatformError, ThermalInfo, ThermalInfoCols,
    };

    use crate::db::mock::MockDb;

    /// What each telemetry cycle did, in order.
    type Log = Arc<Mutex<Vec<&'static str>>>;

    struct FakePlatform {
        log: Log,
        cycles: Arc<Mutex<usize>>,
        /// Stop the loop once this many cycles have run.
        stop_after: usize,
        shutdown: watch::Sender<bool>,
        fail_fans: bool,
        fail_thermals: bool,
        /// Report one named drawer, so the fan pass has a row to write.
        one_drawer: bool,
    }

    impl PlatformApi for FakePlatform {
        fn get_chassis_info(
            &mut self,
            _cols: ChassisInfoCols,
        ) -> Result<ChassisInfo, PlatformError> {
            Ok(ChassisInfo::default())
        }

        fn get_thermals(
            &mut self,
            _cols: ThermalInfoCols,
            _skip: &[DeviceKey],
        ) -> Result<Vec<ThermalInfo>, PlatformError> {
            self.log.lock().unwrap().push("thermals");
            // One cycle is one temperature update: the fan update may be gated
            // out, this never is.
            let mut n = self.cycles.lock().unwrap();
            *n += 1;
            if *n >= self.stop_after {
                let _ = self.shutdown.send(true);
            }
            drop(n);
            if self.fail_thermals {
                return Err(PlatformError::Backend("thermal read failed".into()));
            }
            Ok(Vec::new())
        }

        fn get_fan_drawers(
            &mut self,
            _cols: FanDrawerInfoCols,
        ) -> Result<Vec<FanDrawerInfo>, PlatformError> {
            if _cols != FanDrawerInfoCols::STATUS_LED {
                self.log.lock().unwrap().push("fans");
            }
            if self.fail_fans {
                return Err(PlatformError::Backend("fan read failed".into()));
            }
            if self.one_drawer {
                return Ok(vec![FanDrawerInfo {
                    name: "drawer1".to_string(),
                    ..Default::default()
                }]);
            }
            Ok(Vec::new())
        }

        fn get_fans(
            &mut self,
            _cols: FanInfoCols,
            _absorb: bool,
        ) -> Result<Vec<FanInfo>, PlatformError> {
            Ok(Vec::new())
        }

        fn set_fan_led(
            &mut self,
            _: &platform_api::DeviceKey,
            _: platform_api::LedColor,
        ) -> Result<Option<bool>, PlatformError> {
            Ok(Some(true))
        }

        fn tm_get_interval(&mut self) -> Result<Option<f64>, PlatformError> {
            panic!("the telemetry handle must not read policy intervals")
        }

        fn tm_run_policy(&mut self) -> Result<(), PlatformError> {
            panic!("the telemetry handle must not execute policy")
        }
    }

    struct Rig {
        platform: FakePlatform,
        log: Log,
        rx: watch::Receiver<bool>,
    }

    fn rig(stop_after: usize) -> Rig {
        let (tx, rx) = watch::channel(false);
        let log: Log = Arc::new(Mutex::new(Vec::new()));
        Rig {
            platform: FakePlatform {
                log: log.clone(),
                cycles: Arc::new(Mutex::new(0)),
                stop_after,
                shutdown: tx,
                fail_fans: false,
                fail_thermals: false,
                one_drawer: false,
            },
            log,
            rx,
        }
    }

    /// `start_paused` freezes tokio's clock but not `std::time::Instant`, which
    /// is what `PollingGate` measures.  `resolve()` hands an unconfigured fan
    /// the cycle length, and ten real seconds never pass here, so the gate is
    /// set afterwards rather than through `platform.json`: zero opts out of it
    /// for the tests that are not about gating, and doing it this way leaves
    /// the cycle itself at the 10 s the wait arithmetic below is written for.
    fn monitor(fan_interval: f64) -> Monitor {
        let mut m = Monitor::with_env(1.0, 10.0, 30.0, PollingIntervals::default(), false);
        m.intervals.fan = Some(fan_interval);
        m
    }

    // ── Ordering ──────────────────────────────────────────────────────────

    /// Telemetry updates fans before temperatures and never runs policy.
    #[tokio::test(start_paused = true)]
    async fn each_cycle_updates_fans_then_temperatures_without_running_policy() {
        let mut r = rig(2);
        let m = MockDb::new(false);
        monitor(0.0).run(&mut r.platform, &m.db, r.rx.clone()).await;

        assert_eq!(
            *r.log.lock().unwrap(),
            ["fans", "thermals", "fans", "thermals"]
        );
    }

    // ── Gating ────────────────────────────────────────────────────────────

    /// `platform.json` throttles the fan update but never the temperature one.
    /// An interval longer than the run keeps the fans to their first pass while
    /// temperatures keep going — collapsing the two would either stall the
    /// temperature feed or ignore the platform's fan interval.
    #[tokio::test(start_paused = true)]
    async fn a_long_fan_interval_skips_fans_but_not_temperatures() {
        let mut r = rig(3);
        let m = MockDb::new(false);
        monitor(3600.0)
            .run(&mut r.platform, &m.db, r.rx.clone())
            .await;

        let log = r.log.lock().unwrap();
        assert_eq!(log.iter().filter(|e| **e == "fans").count(), 1);
        assert_eq!(log.iter().filter(|e| **e == "thermals").count(), 3);
        assert_eq!(log.iter().filter(|e| **e == "policy").count(), 0);
    }

    // ── Wait arithmetic ───────────────────────────────────────────────────

    /// The first pass is due after the short initial interval so STATE_DB is
    /// populated soon after boot; every pass after it waits the full update
    /// interval, less the time the cycle itself took.
    #[tokio::test(start_paused = true)]
    async fn the_first_pass_is_early_and_the_rest_are_on_the_update_interval() {
        let start = tokio::time::Instant::now();
        let mut r = rig(1);
        let m = MockDb::new(false);
        monitor(0.0).run(&mut r.platform, &m.db, r.rx.clone()).await;
        let one = start.elapsed().as_secs_f64();
        assert!(
            (one - 1.0).abs() < 0.5,
            "first pass waited {one}s, expected ~1"
        );

        let start = tokio::time::Instant::now();
        let mut r = rig(3);
        let m = MockDb::new(false);
        monitor(0.0).run(&mut r.platform, &m.db, r.rx.clone()).await;
        let three = start.elapsed().as_secs_f64();
        // 1 (initial) + 10 + 10, the cycles themselves taking no time.
        assert!(
            (three - 21.0).abs() < 0.5,
            "three passes took {three}s, expected ~21"
        );
    }

    // ── Failure handling ──────────────────────────────────────────────────

    /// A platform that cannot be read is logged and the loop carries on: the
    /// alternative is a daemon that exits on one bad sysfs read and stops
    /// feeding hw-management-tc altogether.
    #[tokio::test(start_paused = true)]
    async fn a_failing_fan_read_does_not_stop_the_loop() {
        let mut r = rig(2);
        r.platform.fail_fans = true;
        let m = MockDb::new(false);
        monitor(0.0).run(&mut r.platform, &m.db, r.rx.clone()).await;

        let log = r.log.lock().unwrap();
        assert_eq!(log.iter().filter(|e| **e == "thermals").count(), 2);
        assert_eq!(log.iter().filter(|e| **e == "policy").count(), 0);
    }

    /// A STATE_DB that will not take the fan pass's write is different: that
    /// is a redis this process has lost, and the loop leaves with a non-zero
    /// code before the temperature half or the policy runs.
    #[tokio::test(start_paused = true)]
    async fn a_refused_fan_write_stops_the_loop() {
        let mut r = rig(5);
        r.platform.one_drawer = true;
        let m = MockDb::new(false);
        m.physical_entity.fail_writes("redis is gone");
        let code = monitor(0.0).run(&mut r.platform, &m.db, r.rx.clone()).await;

        assert_eq!(code, Some(crate::ERR_DB_WRITE));
        assert_eq!(
            *r.log.lock().unwrap(),
            ["fans"],
            "nothing after the failed pass ran"
        );
    }

    /// And the control: a monitor told to stop leaves with no code of its own.
    #[tokio::test(start_paused = true)]
    async fn a_monitor_told_to_stop_has_no_code_of_its_own() {
        let mut r = rig(1);
        r.platform.one_drawer = true;
        let m = MockDb::new(false);
        let code = monitor(0.0).run(&mut r.platform, &m.db, r.rx.clone()).await;
        assert_eq!(code, None);
        assert!(m.fan_drawer.row("drawer1").is_some());
    }

    #[tokio::test(start_paused = true)]
    async fn a_failing_temperature_read_does_not_stop_the_loop() {
        let mut r = rig(2);
        r.platform.fail_thermals = true;
        let m = MockDb::new(false);
        monitor(0.0).run(&mut r.platform, &m.db, r.rx.clone()).await;
        assert_eq!(
            r.log
                .lock()
                .unwrap()
                .iter()
                .filter(|e| **e == "policy")
                .count(),
            0
        );
    }

    // ── Shutdown ──────────────────────────────────────────────────────────

    /// Shutdown wins the race against the first sleep, so a daemon told to stop
    /// during start-up writes nothing at all.
    #[tokio::test(start_paused = true)]
    async fn a_shutdown_before_the_first_wait_runs_no_cycle() {
        let (tx, rx) = watch::channel(false);
        tx.send(true).unwrap();
        let mut r = rig(99);
        let m = MockDb::new(false);
        monitor(0.0).run(&mut r.platform, &m.db, rx).await;
        assert!(r.log.lock().unwrap().is_empty());
    }

    // ── The BMC branch ────────────────────────────────────────────────────

    /// Only the copy running on a switch BMC reads the mirrored chassis
    /// thermals; on the switch host the same table is what this daemon *writes*,
    /// and reading it back would have the host watch its own output.
    #[tokio::test(start_paused = true)]
    async fn the_mirror_is_read_on_a_bmc_and_not_on_a_switch() {
        for (is_bmc, expected) in [(true, 2), (false, 0)] {
            let mut r = rig(2);
            let m = MockDb::new(false);
            Monitor::with_env(1.0, 10.0, 30.0, PollingIntervals::default(), is_bmc)
                .run(&mut r.platform, &m.db, r.rx.clone())
                .await;
            assert_eq!(m.temperature.scans(), expected, "is_switch_bmc = {is_bmc}");
        }
    }

    /// A cycle that overruns the update interval waits the short *initial*
    /// interval before the next one, not the remainder of the update interval.
    ///
    /// The daemon is already behind, so it goes again promptly; computing
    /// `update_interval - elapsed` here would be negative, and clamping it to
    /// zero would spin. Crossing the elapsed threshold also warns, which is the
    /// operator's signal that the platform's reads have become slow.
    #[tokio::test(start_paused = true)]
    async fn a_cycle_that_overruns_falls_back_to_the_initial_interval() {
        /// A platform whose reads take real time. The waits around it are
        /// tokio sleeps, which `start_paused` skips, so only this is spent.
        struct SlowPlatform {
            inner: FakePlatform,
        }
        impl PlatformApi for SlowPlatform {
            fn get_chassis_info(
                &mut self,
                cols: ChassisInfoCols,
            ) -> Result<ChassisInfo, PlatformError> {
                self.inner.get_chassis_info(cols)
            }
            fn get_thermals(
                &mut self,
                cols: ThermalInfoCols,
                skip: &[DeviceKey],
            ) -> Result<Vec<ThermalInfo>, PlatformError> {
                std::thread::sleep(std::time::Duration::from_millis(30));
                self.inner.get_thermals(cols, skip)
            }
            fn get_fan_drawers(
                &mut self,
                cols: FanDrawerInfoCols,
            ) -> Result<Vec<FanDrawerInfo>, PlatformError> {
                self.inner.get_fan_drawers(cols)
            }
            fn get_fans(
                &mut self,
                cols: FanInfoCols,
                absorb: bool,
            ) -> Result<Vec<FanInfo>, PlatformError> {
                self.inner.get_fans(cols, absorb)
            }
            fn set_fan_led(
                &mut self,
                a: &platform_api::DeviceKey,
                c: platform_api::LedColor,
            ) -> Result<Option<bool>, PlatformError> {
                self.inner.set_fan_led(a, c)
            }
            fn tm_get_interval(&mut self) -> Result<Option<f64>, PlatformError> {
                self.inner.tm_get_interval()
            }
            fn tm_run_policy(&mut self) -> Result<(), PlatformError> {
                self.inner.tm_run_policy()
            }
        }

        let r = rig(2);
        let mut slow = SlowPlatform { inner: r.platform };
        let m = MockDb::new(false);

        // A 30 ms cycle overruns both a 10 ms update interval and a 5 ms
        // threshold; the fallback is the 5 s initial interval.
        let start = tokio::time::Instant::now();
        Monitor::with_env(5.0, 0.010, 0.005, PollingIntervals::default(), false)
            .run(&mut slow, &m.db, r.rx.clone())
            .await;
        let waited = start.elapsed().as_secs_f64();

        // Two waits of 5 s each: the first is the initial interval by
        // definition, the second is it again because the cycle overran.
        assert!(
            (waited - 10.0).abs() < 0.5,
            "waited {waited}s, expected ~10 — a shorter wait means the overrun \
             was folded into the update interval instead"
        );
        assert_eq!(
            *r.log.lock().unwrap(),
            ["fans", "thermals", "fans", "thermals"]
        );
    }

    // ── The policy's own cadence ──────────────────────────────────────────

    /// The updaters do not wait for the thermal manager.  This is the whole
    /// reason `tm_initialize()` was moved off this path: on SPC1 it sits in
    /// mlnx's 300 s `unlink_hw_mgmt_thermal_files()` wait on every restart,
    /// and a loop that waited with it left FAN_INFO and TEMPERATURE_INFO
    /// unpublished for five minutes -- long enough for system-health to report
    /// the fans missing.
    #[tokio::test(start_paused = true)]
    async fn the_updaters_do_not_wait_for_the_thermal_manager() {
        let mut r = rig(2);
        let m = MockDb::new(false);
        monitor(0.0).run(&mut r.platform, &m.db, r.rx.clone()).await;

        let log = r.log.lock().unwrap();
        assert_eq!(log.iter().filter(|e| **e == "fans").count(), 2);
        assert_eq!(log.iter().filter(|e| **e == "thermals").count(), 2);
        assert_eq!(
            log.iter().filter(|e| **e == "policy").count(),
            0,
            "a policy round against a manager that has not started is a call \
             Python never makes: {log:?}"
        );
    }
}
