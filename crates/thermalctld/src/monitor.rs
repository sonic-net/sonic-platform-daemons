//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! Port of ThermalMonitor's polling loop.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio::sync::watch;

use platform_api::PlatformApi;

use crate::bmc::ChassisThermalWatch;
use crate::db::StateDb;
use crate::ERR_DB_WRITE;
use crate::event_log::BmcEventLogger;
use crate::fan_updater::FanUpdater;
use crate::polling::{PollingGate, PollingIntervals};
use crate::temp_updater::TemperatureUpdater;

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
    /// And when the platform's policy was.
    ///
    /// Python runs the policy on a second loop, on the main thread, at the
    /// interval the manager reports rather than the one the updaters use
    /// (`thermalctld:ThermalControlDaemon.run`).  One loop here, gated — see 12
    /// for what that costs — but the cadence is the manager's.
    policy_gate: PollingGate,
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
        Self {
            initial_interval,
            update_interval,
            update_elapsed_threshold,
            fan_updater: FanUpdater::new(),
            temperature_updater: TemperatureUpdater::new(),
            intervals,
            fan_gate: PollingGate::new(),
            policy_gate: PollingGate::new(),
            bmc_watch: is_switch_bmc.then(|| (ChassisThermalWatch::new(), BmcEventLogger::new(true))),
        }
    }

    /// Run the thermal monitoring loop.
    ///
    /// The first pass fires after the short `initial_interval` so STATE_DB is
    /// populated soon after boot; subsequent waits absorb the cycle's own
    /// duration to keep a steady period.
    ///
    /// Calls `tm_run_policy()` on each cycle (Mellanox: no-op; other platforms
    /// may implement fan-speed policy logic here).
    ///
    /// The manager is reached through the platform rather than held as an
    /// object: what the base class answers is a class, not an instance, and
    /// two of its methods take the chassis back as an argument. Keeping that
    /// on the Python side is what stops a policy round re-entering Rust.
    ///
    /// `tm_ready` goes true once the thermal manager has been started, on the
    /// thread `main::run` put it on.  The updaters below do not wait for it —
    /// that is the whole point of starting them first — but the policy does.
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
        tm_ready: Arc<AtomicBool>,
        mut shutdown: watch::Receiver<bool>,
    ) -> Option<i32> {
        log::info!("Start thermal monitoring loop");

        // Learned once the manager is up, not here.  Python reads the interval
        // immediately after `initialize()` and `load()` in
        // `thermalctld:ThermalControlDaemon.__init__`, so it reads what
        // thermal_policy.json says; this loop now starts before either (see
        // `main::run`), and reading at this point would get the base-class
        // default instead.
        let mut policy_interval: Option<f64> = None;

        // Where the policy gate gets seeded from once the interval is known.
        //
        // A manager that was already up when the loop started is Python with
        // `initialize()` returning at once: its policy loop begins waiting
        // when the daemon starts, so the gate has to be seeded from here or
        // the first policy round slips by a whole cycle.  A manager that comes
        // up later is Python with `initialize()` blocked -- its policy loop
        // has not started waiting yet -- and the gate is seeded from the cycle
        // that first saw it up, further down.
        let started_ready = tm_ready.load(Ordering::Acquire);
        let loop_start = Instant::now();

        let mut wait_time = self.initial_interval;

        loop {
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
            if self.fan_gate.is_due("fan_drawer", self.intervals.fan, begin) {
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
            if let Err(e) = self
                .temperature_updater
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

            // Platform-specific fan-speed policy (Mellanox: no-op), on the
            // manager's own cadence rather than this loop's.
            //
            // Not before the manager is up.  `tm_initialize()` runs on its own
            // thread now, and a policy round against a half-started manager is
            // a call Python never makes: there the policy loop *is* the main
            // thread, which has already returned from `initialize()`.
            if tm_ready.load(Ordering::Acquire) {
                match policy_interval {
                    // First cycle with a manager: learn the cadence and seed
                    // the gate.  Python's policy loop waits a full interval
                    // before its first run, where the updaters go after the
                    // short initial one; seeding rather than firing is what
                    // reproduces that.  60 is the base class default, and what
                    // a platform whose thermal_policy.json omits the key gets.
                    None => {
                        let v = platform.tm_get_interval().ok().flatten().unwrap_or(60.0);
                        self.policy_gate
                            .is_due("policy", Some(v), if started_ready { loop_start } else { begin });
                        policy_interval = Some(v);
                        // Seeded, then asked: a zero interval is due on the
                        // cycle it was seeded from, and Python runs it there
                        // too -- `stop_event.wait(0)` returns at once.
                        if self.policy_gate.is_due("policy", Some(v), begin) {
                            if let Err(e) = platform.tm_run_policy() {
                                log::warn!("run_policy failed: {e}");
                            }
                        }
                    }
                    Some(v) => {
                        if self.policy_gate.is_due("policy", Some(v), begin) {
                            if let Err(e) = platform.tm_run_policy() {
                                log::warn!("run_policy failed: {e}");
                            }
                        }
                    }
                }
            }

            let elapsed = begin.elapsed().as_secs_f64();
            // The fallback is not clamped to `update_interval`, though
            // `resolve` may have shrunk that below `initial_interval`; see
            // `pmon_common::cadence` for why that stays as Python has it.
            wait_time =
                pmon_common::cadence::next_wait(elapsed, self.update_interval, self.initial_interval);

            if elapsed > self.update_elapsed_threshold {
                log::warn!(
                    "Update fan and temperature status took {elapsed} seconds, \
                     there might be performance risk"
                );
            }
        }

        log::info!("Stop thermal monitoring loop");
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    use platform_api::{ChassisInfo, ChassisInfoCols, FanDrawerInfo, FanDrawerInfoCols, FanInfo, FanInfoCols, PlatformError, ThermalInfo};

    use crate::db::mock::MockDb;

    /// What each cycle did, in order.  The platform and the thermal manager
    /// share it, which is the only way to see that the policy runs *after* the
    /// two updaters rather than merely the same number of times.
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
        /// The policy is reached through the platform now, so what used to be
        /// a separate fake manager lives here.
        fail_policy: bool,
        /// What `tm_get_interval()` reports; the policy runs on this, not on
        /// the monitor's update interval.
        policy_interval: f64,
    }

    impl PlatformApi for FakePlatform {
        fn get_chassis_info(&mut self, _cols: ChassisInfoCols) -> Result<ChassisInfo, PlatformError> {
            Ok(ChassisInfo::default())
        }

        fn get_thermals(&mut self) -> Result<Vec<ThermalInfo>, PlatformError> {
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

        fn get_fan_drawers(&mut self, _cols: FanDrawerInfoCols) -> Result<Vec<FanDrawerInfo>, PlatformError> {
            self.log.lock().unwrap().push("fans");
            if self.fail_fans {
                return Err(PlatformError::Backend("fan read failed".into()));
            }
            if self.one_drawer {
                return Ok(vec![FanDrawerInfo { name: "drawer1".to_string(), ..Default::default() }]);
            }
            Ok(Vec::new())
        }

        fn get_fans(&mut self, _cols: FanInfoCols) -> Result<Vec<FanInfo>, PlatformError> {
            Ok(Vec::new())
        }

        fn set_fan_led(&mut self, _: &str, _: platform_api::LedColor) -> Result<(), PlatformError> {
            Ok(())
        }

        fn tm_get_interval(&mut self) -> Result<Option<f64>, PlatformError> {
            Ok(Some(self.policy_interval))
        }

        fn tm_run_policy(&mut self) -> Result<(), PlatformError> {
            self.log.lock().unwrap().push("policy");
            if self.fail_policy {
                return Err(PlatformError::Backend("policy failed".into()));
            }
            Ok(())
        }
    }


    struct Rig {
        platform: FakePlatform,
        log: Log,
        rx: watch::Receiver<bool>,
    }

    /// A manager that is already up, which is what every test below but the
    /// two about the flag itself is about.
    fn ready() -> Arc<AtomicBool> {
        Arc::new(AtomicBool::new(true))
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
                fail_policy: false,
                // Zero means due every cycle.  `PollingGate` measures in real
                // time, which `start_paused` does not advance, so a non-zero
                // interval here would never come due.
                policy_interval: 0.0,
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

    /// Fans, then temperatures, then the platform's policy — every cycle.
    /// Python runs them in that order and the policy is what a vendor hangs
    /// fan-speed control off, so it must see the readings this cycle took.
    #[tokio::test(start_paused = true)]
    async fn each_cycle_updates_fans_then_temperatures_then_runs_the_policy() {
        let mut r = rig(2);
        let m = MockDb::new(false);
        monitor(0.0)
            .run(&mut r.platform, &m.db, ready(), r.rx.clone())
            .await;

        assert_eq!(
            *r.log.lock().unwrap(),
            ["fans", "thermals", "policy", "fans", "thermals", "policy"]
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
            .run(&mut r.platform, &m.db, ready(), r.rx.clone())
            .await;

        let log = r.log.lock().unwrap();
        assert_eq!(log.iter().filter(|e| **e == "fans").count(), 1);
        assert_eq!(log.iter().filter(|e| **e == "thermals").count(), 3);
        assert_eq!(log.iter().filter(|e| **e == "policy").count(), 3);
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
        monitor(0.0)
            .run(&mut r.platform, &m.db, ready(), r.rx.clone())
            .await;
        let one = start.elapsed().as_secs_f64();
        assert!((one - 1.0).abs() < 0.5, "first pass waited {one}s, expected ~1");

        let start = tokio::time::Instant::now();
        let mut r = rig(3);
        let m = MockDb::new(false);
        monitor(0.0)
            .run(&mut r.platform, &m.db, ready(), r.rx.clone())
            .await;
        let three = start.elapsed().as_secs_f64();
        // 1 (initial) + 10 + 10, the cycles themselves taking no time.
        assert!((three - 21.0).abs() < 0.5, "three passes took {three}s, expected ~21");
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
        monitor(0.0)
            .run(&mut r.platform, &m.db, ready(), r.rx.clone())
            .await;

        let log = r.log.lock().unwrap();
        assert_eq!(log.iter().filter(|e| **e == "thermals").count(), 2);
        assert_eq!(log.iter().filter(|e| **e == "policy").count(), 2);
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
        let code = monitor(0.0)
            .run(&mut r.platform, &m.db, ready(), r.rx.clone())
            .await;

        assert_eq!(code, Some(crate::ERR_DB_WRITE));
        assert_eq!(*r.log.lock().unwrap(), ["fans"], "nothing after the failed pass ran");
    }

    /// And the control: a monitor told to stop leaves with no code of its own.
    #[tokio::test(start_paused = true)]
    async fn a_monitor_told_to_stop_has_no_code_of_its_own() {
        let mut r = rig(1);
        r.platform.one_drawer = true;
        let m = MockDb::new(false);
        let code = monitor(0.0)
            .run(&mut r.platform, &m.db, ready(), r.rx.clone())
            .await;
        assert_eq!(code, None);
        assert!(m.fan_drawer.row("drawer1").is_some());
    }

    #[tokio::test(start_paused = true)]
    async fn a_failing_temperature_read_does_not_stop_the_loop() {
        let mut r = rig(2);
        r.platform.fail_thermals = true;
        let m = MockDb::new(false);
        monitor(0.0)
            .run(&mut r.platform, &m.db, ready(), r.rx.clone())
            .await;
        assert_eq!(r.log.lock().unwrap().iter().filter(|e| **e == "policy").count(), 2);
    }

    #[tokio::test(start_paused = true)]
    async fn a_failing_policy_does_not_stop_the_loop() {
        let mut r = rig(2);
        r.platform.fail_policy = true;
        let m = MockDb::new(false);
        monitor(0.0)
            .run(&mut r.platform, &m.db, ready(), r.rx.clone())
            .await;
        assert_eq!(r.log.lock().unwrap().iter().filter(|e| **e == "thermals").count(), 2);
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
        monitor(0.0).run(&mut r.platform, &m.db, ready(), rx).await;
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
                .run(&mut r.platform, &m.db, ready(), r.rx.clone())
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
            fn get_chassis_info(&mut self, cols: ChassisInfoCols) -> Result<ChassisInfo, PlatformError> {
                self.inner.get_chassis_info(cols)
            }
            fn get_thermals(&mut self) -> Result<Vec<ThermalInfo>, PlatformError> {
                std::thread::sleep(std::time::Duration::from_millis(30));
                self.inner.get_thermals()
            }
            fn get_fan_drawers(&mut self, cols: FanDrawerInfoCols) -> Result<Vec<FanDrawerInfo>, PlatformError> {
                self.inner.get_fan_drawers(cols)
            }
            fn get_fans(&mut self, cols: FanInfoCols) -> Result<Vec<FanInfo>, PlatformError> {
                self.inner.get_fans(cols)
            }
            fn set_fan_led(&mut self, a: &str, c: platform_api::LedColor) -> Result<(), PlatformError> {
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
            .run(&mut slow, &m.db, ready(), r.rx.clone())
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
            ["fans", "thermals", "policy", "fans", "thermals", "policy"]
        );
    }

    // ── The policy's own cadence ──────────────────────────────────────────

    /// The policy runs at the interval its manager reports, not at the one the
    /// fan and temperature updaters use.  Python keeps them apart by running
    /// the policy on a second loop on the main thread; folding it into this one
    /// is only correct while the cadence is still the manager's.
    #[tokio::test(start_paused = true)]
    async fn the_policy_runs_on_its_own_interval_and_not_the_monitors() {
        let mut r = rig(3);
        r.platform.policy_interval = 3600.0;
        let m = MockDb::new(false);
        monitor(0.0)
            .run(&mut r.platform, &m.db, ready(), r.rx.clone())
            .await;

        let log = r.log.lock().unwrap();
        assert_eq!(log.iter().filter(|e| **e == "thermals").count(), 3);
        assert_eq!(
            log.iter().filter(|e| **e == "policy").count(),
            0,
            "an hour has not passed: {log:?}"
        );
    }

    /// Python waits a full policy interval before the first run, where the
    /// updaters go after the short initial one.  A policy that ran on the first
    /// cycle would act on readings the box has only just started producing.
    #[tokio::test(start_paused = true)]
    async fn the_first_cycle_does_not_run_the_policy() {
        let mut r = rig(1);
        r.platform.policy_interval = 3600.0;
        let m = MockDb::new(false);
        monitor(0.0)
            .run(&mut r.platform, &m.db, ready(), r.rx.clone())
            .await;
        assert_eq!(*r.log.lock().unwrap(), ["fans", "thermals"], "no policy on cycle 1");
    }

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
        let not_yet = Arc::new(AtomicBool::new(false));
        monitor(0.0)
            .run(&mut r.platform, &m.db, Arc::clone(&not_yet), r.rx.clone())
            .await;

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

    /// And the policy starts once it is up, not before and not late.  The
    /// interval is read at that point rather than at loop start, because that
    /// is where Python reads it -- right after `initialize()` and `load()` --
    /// and reading earlier would get the base-class default instead of what
    /// thermal_policy.json says.
    #[tokio::test(start_paused = true)]
    async fn the_policy_starts_once_the_manager_is_up() {
        /// Announces the manager partway through, as the init thread does.
        struct LateManager {
            inner: FakePlatform,
            ready: Arc<AtomicBool>,
            cycles: usize,
        }
        impl PlatformApi for LateManager {
            fn get_chassis_info(&mut self, cols: ChassisInfoCols) -> Result<ChassisInfo, PlatformError> {
                self.inner.get_chassis_info(cols)
            }
            fn get_thermals(&mut self) -> Result<Vec<ThermalInfo>, PlatformError> {
                self.cycles += 1;
                // Up from the third cycle on.
                if self.cycles == 3 {
                    self.ready.store(true, Ordering::Release);
                }
                self.inner.get_thermals()
            }
            fn get_fan_drawers(&mut self, cols: FanDrawerInfoCols) -> Result<Vec<FanDrawerInfo>, PlatformError> {
                self.inner.get_fan_drawers(cols)
            }
            fn get_fans(&mut self, cols: FanInfoCols) -> Result<Vec<FanInfo>, PlatformError> {
                self.inner.get_fans(cols)
            }
            fn tm_get_interval(&mut self) -> Result<Option<f64>, PlatformError> {
                self.inner.tm_get_interval()
            }
            fn tm_run_policy(&mut self) -> Result<(), PlatformError> {
                self.inner.tm_run_policy()
            }
        }

        let mut r = rig(5);
        // Due every cycle once the gate has been seeded, so the count shows
        // exactly which cycles were allowed to run it.
        r.platform.policy_interval = 0.0;
        let log = r.log.clone();
        let not_yet = Arc::new(AtomicBool::new(false));
        let mut late =
            LateManager { inner: r.platform, ready: Arc::clone(&not_yet), cycles: 0 };
        let m = MockDb::new(false);
        monitor(0.0)
            .run(&mut late, &m.db, not_yet, r.rx.clone())
            .await;

        let log = log.lock().unwrap();
        assert_eq!(log.iter().filter(|e| **e == "thermals").count(), 5, "{log:?}");
        assert_eq!(
            log.iter().filter(|e| **e == "policy").count(),
            3,
            "cycles 1-2 had no manager, 3 seeds and -- at a zero interval -- \
             also runs, then 4 and 5: {log:?}"
        );
    }

    /// The default is 60 seconds — `ThermalManagerBase._interval`, which is
    /// what a platform whose `thermal_policy.json` omits the key gets, and
    /// every Mellanox file omits it.
    ///
    /// The default used to live on the manager object. It lives in the monitor
    /// now, because a platform that has no manager at all answers None rather
    /// than an interval, and the loop still needs a period to gate on.
    #[tokio::test(start_paused = true)]
    async fn a_platform_with_no_policy_interval_gets_pythons_default() {
        struct Silent(FakePlatform);
        impl PlatformApi for Silent {
            fn get_chassis_info(&mut self, cols: ChassisInfoCols) -> Result<ChassisInfo, PlatformError> {
                self.0.get_chassis_info(cols)
            }
            fn get_thermals(&mut self) -> Result<Vec<ThermalInfo>, PlatformError> {
                self.0.get_thermals()
            }
            fn get_fan_drawers(&mut self, cols: FanDrawerInfoCols) -> Result<Vec<FanDrawerInfo>, PlatformError> {
                self.0.get_fan_drawers(cols)
            }
            fn get_fans(&mut self, cols: FanInfoCols) -> Result<Vec<FanInfo>, PlatformError> {
                self.0.get_fans(cols)
            }
            fn tm_run_policy(&mut self) -> Result<(), PlatformError> {
                self.0.tm_run_policy()
            }
            // tm_get_interval is left to the trait's default, which answers
            // None -- a platform with no thermal manager.
        }

        let r = rig(1);
        let log = r.log.clone();
        let mut silent = Silent(r.platform);
        let m = MockDb::new(false);
        monitor(0.0).run(&mut silent, &m.db, ready(), r.rx.clone()).await;

        // 60 s has not passed in one cycle, so the policy has not run.
        assert_eq!(*log.lock().unwrap(), ["fans", "thermals"]);
    }
}
