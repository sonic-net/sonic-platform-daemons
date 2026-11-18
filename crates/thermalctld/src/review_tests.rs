//! Regression tests exercising the shipped worker and shutdown paths.
use super::*;
use crate::db::{
    mock::{MockDb, MockLeak},
    TableLike,
};
use platform_api::{
    FanDrawerInfo, FanDrawerInfoCols, FanInfo, FanInfoCols, LeakProfile, LeakSensorInfo,
    LeakSeverity,
};
use pmon_common::db::MockTable;
use std::sync::atomic::AtomicUsize;

struct Policy {
    reads: AtomicUsize,
    fail_second: bool,
    table: MockTable,
}
impl TableLike for Policy {
    fn set(&self, key: &str, values: &[(&str, String)]) -> Result<(), String> {
        self.table.set(key, values)
    }
    fn del(&self, key: &str) -> Result<(), String> {
        self.table.del(key)
    }
    fn hdel(&self, key: &str, field: &str) -> Result<(), String> {
        self.table.hdel(key, field)
    }
    fn get_keys(&self) -> Result<Vec<String>, String> {
        self.table.get_keys()
    }
    fn get(&self, key: &str) -> Result<Option<Vec<(String, String)>>, String> {
        if self.reads.fetch_add(1, Ordering::SeqCst) == 1 && self.fail_second {
            Err("CONFIG_DB temporarily unavailable".into())
        } else {
            self.table.get(key)
        }
    }
}

struct Leaks {
    reads: usize,
    fail_minimum: bool,
}
impl PlatformApi for Leaks {
    fn get_leak_profiles(&mut self) -> Result<Vec<LeakProfile>, PlatformError> {
        Ok(vec![])
    }
    fn get_leak_sensors(&mut self) -> Result<Vec<LeakSensorInfo>, PlatformError> {
        Ok(["a", "b"]
            .into_iter()
            .map(|name| LeakSensorInfo {
                name: name.into(),
                is_ok: Some(true),
                is_leak: Some(true),
                severity: Some(LeakSeverity::Minor),
                ..Default::default()
            })
            .collect())
    }
    fn get_major_leak_num_min_sensors(&mut self) -> Result<Option<i64>, PlatformError> {
        self.reads += 1;
        if self.fail_minimum && self.reads == 2 {
            Err(PlatformError::Backend("minimum unavailable".into()))
        } else {
            Ok(Some(2))
        }
    }
}

#[test]
fn minimum_and_config_read_failures_preserve_major_and_retry() {
    for fail_minimum in [true, false] {
        let mut db = MockLeak::new();
        db.tables.policy = Some(Box::new(Policy {
            reads: AtomicUsize::new(0),
            fail_second: !fail_minimum,
            table: MockTable::new(),
        }));
        let mut platform = Leaks {
            reads: 0,
            fail_minimum,
        };
        let (_shutdown, receiver) = watch::channel(false);
        let mut events = event_log::BmcEventLogger::new(false);
        let mut observed = Vec::new();
        assert_eq!(
            leak_loop_with(
                &mut platform,
                &db.tables,
                Duration::from_secs(3600),
                receiver,
                &mut events,
                Instant::now,
                |_, _| {
                    observed.push(db.system.field("system", "device_leak_status"));
                    observed.len() == 3
                }
            ),
            None
        );
        assert_eq!(
            observed,
            vec![Some("MAJOR".into()); 3],
            "minimum failure: {fail_minimum}"
        );
        assert_eq!(
            platform.reads, 3,
            "the next cycle must retry the failed read"
        );
        assert!(db.sensor.is_empty() && db.system.is_empty() && db.profile.is_empty());
    }
}

#[test]
fn a_sensor_read_failure_can_stop_during_a_long_poll_interval() {
    struct Broken(watch::Sender<bool>);
    impl PlatformApi for Broken {
        fn get_leak_profiles(&mut self) -> Result<Vec<LeakProfile>, PlatformError> {
            Ok(vec![])
        }
        fn get_leak_sensors(&mut self) -> Result<Vec<LeakSensorInfo>, PlatformError> {
            self.0.send(true).unwrap();
            Err(PlatformError::Backend("sensor unavailable".into()))
        }
    }
    let db = MockLeak::new();
    let (shutdown, receiver) = watch::channel(false);
    let begin = Instant::now();
    assert_eq!(
        leak_loop(
            &mut Broken(shutdown),
            &db.tables,
            Duration::from_secs(3600),
            false,
            receiver
        ),
        None
    );
    assert!(begin.elapsed() < Duration::from_secs(1));
    assert!(db.system.is_empty());
}

#[test]
fn a_startup_read_failure_still_clears_the_tables_it_can_reach() {
    let db = MockLeak::new();
    db.profile.set("old", &[("type", "old".into())]).unwrap();
    db.system.fail_reads("STATE_DB unavailable");
    let (_shutdown, receiver) = watch::channel(false);
    assert_eq!(
        leak_loop(
            &mut Leaks {
                reads: 0,
                fail_minimum: false
            },
            &db.tables,
            Duration::from_secs(3600),
            false,
            receiver
        ),
        Some(ERR_DB_WRITE)
    );
    assert!(
        db.profile.is_empty(),
        "startup failures must not bypass cleanup"
    );
}

#[test]
fn ordinary_leak_and_recovery_lines_are_syslog_only_on_the_bmc() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("event.log");
    let mut events = event_log::BmcEventLogger::with_path(true, &path);
    let log = pmon_common::logging::capture();
    let mut state = leak_updater::LeakState::new();
    let mut sensor = LeakSensorInfo {
        name: "a".into(),
        is_ok: Some(true),
        is_leak: Some(true),
        severity: Some(LeakSeverity::Minor),
        ..Default::default()
    };
    state.refresh(&[sensor.clone()], Instant::now(), &mut events);
    sensor.is_leak = Some(false);
    state.refresh(&[sensor], Instant::now(), &mut events);
    assert!(log.contains("reported leaking"));
    assert!(log.contains("recovered from leaking"));
    assert!(
        !path.exists(),
        "ordinary leakage/recovery must not create a BMC event file"
    );
}

#[tokio::test]
async fn lost_state_db_stops_a_running_leak_thread_before_joining_it() {
    state_db_loss_stops_leak_worker(false).await;
}

#[tokio::test]
async fn lost_state_db_stops_a_leak_worker_retrying_persistent_config_db_failure() {
    state_db_loss_stops_leak_worker(true).await;
}

async fn state_db_loss_stops_leak_worker(config_unavailable: bool) {
    struct UnavailablePolicy {
        table: MockTable,
        reads: Arc<AtomicUsize>,
        entered: std::sync::Mutex<Option<std::sync::mpsc::Sender<()>>>,
    }
    impl TableLike for UnavailablePolicy {
        fn set(&self, key: &str, values: &[(&str, String)]) -> Result<(), String> {
            self.table.set(key, values)
        }
        fn del(&self, key: &str) -> Result<(), String> {
            self.table.del(key)
        }
        fn hdel(&self, key: &str, field: &str) -> Result<(), String> {
            self.table.hdel(key, field)
        }
        fn get_keys(&self) -> Result<Vec<String>, String> {
            self.table.get_keys()
        }
        fn get(&self, _: &str) -> Result<Option<Vec<(String, String)>>, String> {
            if self.reads.fetch_add(1, Ordering::SeqCst) == 1 {
                if let Some(entered) = self.entered.lock().unwrap().take() {
                    entered.send(()).unwrap();
                }
            }
            Err("CONFIG_DB persistently unavailable".into())
        }
    }
    struct MonitorPlatform;
    impl PlatformApi for MonitorPlatform {
        fn get_fan_drawers(
            &mut self,
            _: FanDrawerInfoCols,
        ) -> Result<Vec<FanDrawerInfo>, PlatformError> {
            Ok(vec![FanDrawerInfo {
                name: "drawer".into(),
                ..Default::default()
            }])
        }
        fn get_fans(&mut self, _: FanInfoCols, _: bool) -> Result<Vec<FanInfo>, PlatformError> {
            Ok(vec![])
        }
        fn tm_stop(&mut self) -> Result<(), PlatformError> {
            Ok(())
        }
        fn notify_shutdown(&mut self) -> Result<(), PlatformError> {
            Ok(())
        }
    }
    let db = MockDb::new(false);
    db.physical_entity.fail_writes("STATE_DB unavailable");
    let mut monitor = Monitor::with_env(
        0.001,
        0.001,
        30.0,
        polling::PollingIntervals::default(),
        false,
    );
    let (shutdown, _receiver) = watch::channel(false);
    let shutdown = Arc::new(shutdown);
    let fallback = Arc::clone(&shutdown);
    let (complete, completed) = std::sync::mpsc::channel();
    let watchdog = std::thread::spawn(move || {
        if completed.recv_timeout(Duration::from_secs(2)).is_err() {
            fallback.send_replace(true);
        }
    });
    let config_reads = Arc::new(AtomicUsize::new(0));
    let tables = MockLeak::new();
    let sensor = tables.sensor.clone();
    let system = tables.system.clone();
    let profile = tables.profile.clone();
    let sensor_for_thread = sensor.clone();
    let system_for_thread = system.clone();
    let profile_for_thread = profile.clone();
    let args = Args::try_parse_from(["thermalctld", "--enable_liquid_cooling"]).unwrap();
    let begin = Instant::now();
    let result = run(
        &mut MonitorPlatform,
        &db.db,
        &mut monitor,
        &args,
        None,
        &|_shutdown, stopped| Ok(std::thread::spawn(move || stopped.wait())),
        &|_, receiver| {
            let (entered, started) = std::sync::mpsc::channel();
            let policy: Option<Box<dyn TableLike>> = config_unavailable.then(|| {
                Box::new(UnavailablePolicy {
                    table: MockTable::new(),
                    reads: Arc::clone(&config_reads),
                    entered: std::sync::Mutex::new(Some(entered.clone())),
                }) as Box<dyn TableLike>
            });
            let tables = db::LeakTables {
                sensor: Box::new(sensor_for_thread.clone()),
                system: Box::new(system_for_thread.clone()),
                profile: Box::new(profile_for_thread.clone()),
                policy,
            };
            let worker = std::thread::spawn(move || {
                struct Active {
                    entered: Option<std::sync::mpsc::Sender<()>>,
                }
                impl PlatformApi for Active {
                    fn get_leak_profiles(&mut self) -> Result<Vec<LeakProfile>, PlatformError> {
                        Ok(vec![])
                    }
                    fn get_leak_sensors(&mut self) -> Result<Vec<LeakSensorInfo>, PlatformError> {
                        if let Some(entered) = self.entered.take() {
                            entered.send(()).unwrap();
                        }
                        Ok(vec![])
                    }
                }
                assert_eq!(
                    leak_loop(
                        &mut Active {
                            entered: (!config_unavailable).then_some(entered)
                        },
                        &tables,
                        Duration::from_millis(1),
                        false,
                        receiver
                    ),
                    None
                );
            });
            started.recv_timeout(Duration::from_secs(1)).unwrap();
            Ok(worker)
        },
        Arc::clone(&shutdown),
    )
    .await;
    let _ = complete.send(());
    watchdog.join().unwrap();
    assert_eq!(result, Some(ERR_DB_WRITE));
    if config_unavailable {
        assert!(
            config_reads.load(Ordering::SeqCst) >= 2,
            "the worker must have retried the persistent CONFIG_DB failure"
        );
    }
    assert!(
        begin.elapsed() < Duration::from_secs(1),
        "shutdown waited for the fallback watchdog"
    );
    assert!(*shutdown.borrow(), "run must always notify its workers");
    assert!(sensor.is_empty() && system.is_empty() && profile.is_empty());
}

#[test]
fn policy_failures_are_error_logs_and_do_not_stop_later_rounds() {
    struct Manager {
        calls: usize,
        shutdown: watch::Sender<bool>,
    }
    impl PlatformApi for Manager {
        fn tm_initialize(&mut self) -> Result<(), PlatformError> {
            Ok(())
        }
        fn tm_deinitialize(&mut self) -> Result<(), PlatformError> {
            Ok(())
        }
        fn tm_get_interval(&mut self) -> Result<Option<f64>, PlatformError> {
            Ok(Some(0.001))
        }
        fn tm_run_policy(&mut self) -> Result<(), PlatformError> {
            self.calls += 1;
            if self.calls == 1 {
                return Err(PlatformError::Backend("ValueError('bad policy')".into()));
            }
            self.shutdown.send_replace(true);
            Ok(())
        }
    }
    let log = pmon_common::logging::capture();
    let (shutdown, receiver) = watch::channel(false);
    let mut manager = Manager { calls: 0, shutdown };
    let stopped = TmStopped::default();
    stopped.notify();
    tm_policy_with(&mut manager, receiver, &stopped);
    assert_eq!(manager.calls, 2);
    assert!(log.logged(
        log::Level::Error,
        "Caught exception while running thermal policy - ValueError('bad policy')"
    ));
}

#[test]
fn policy_interval_validation_is_tested_on_the_actual_worker() {
    struct Manager(Option<f64>);
    impl PlatformApi for Manager {
        fn tm_get_interval(&mut self) -> Result<Option<f64>, PlatformError> {
            Ok(self.0)
        }
    }
    for value in [
        None,
        Some(f64::NAN),
        Some(f64::INFINITY),
        Some(-1.0),
        Some(f64::MAX),
    ] {
        assert_eq!(
            manager_policy_interval(&mut Manager(value)),
            Duration::from_secs(60)
        );
    }
    assert_eq!(
        manager_policy_interval(&mut Manager(Some(0.0))),
        Duration::ZERO
    );
}

#[tokio::test(start_paused = true)]
async fn policy_runs_after_successful_unsupported_and_failed_initialization() {
    for initialization in [
        Ok(()),
        Err(PlatformError::NotSupported("unsupported".into())),
        Err(PlatformError::Backend("initialization failed".into())),
    ] {
        let expected_first_wait = if initialization.is_ok() {
            Duration::from_millis(1)
        } else {
            Duration::from_secs(60)
        };
        struct Manager {
            initialization: Result<(), PlatformError>,
            initialized: bool,
            ran: bool,
            deinitialized: bool,
            shutdown: watch::Sender<bool>,
        }
        impl PlatformApi for Manager {
            fn tm_initialize(&mut self) -> Result<(), PlatformError> {
                self.initialized = true;
                self.initialization.clone()
            }
            fn tm_get_interval(&mut self) -> Result<Option<f64>, PlatformError> {
                Ok(Some(0.001))
            }
            fn tm_run_policy(&mut self) -> Result<(), PlatformError> {
                assert!(self.initialized);
                self.ran = true;
                self.shutdown.send_replace(true);
                Ok(())
            }
            fn tm_deinitialize(&mut self) -> Result<(), PlatformError> {
                self.deinitialized = true;
                Ok(())
            }
        }
        let (shutdown, receiver) = watch::channel(false);
        let mut manager = Manager {
            initialization,
            initialized: false,
            ran: false,
            deinitialized: false,
            shutdown,
        };
        let stopped = TmStopped::default();
        stopped.notify();
        let begin = tokio::time::Instant::now();
        tm_policy_loop(&mut manager, receiver, &stopped).await;
        assert_eq!(begin.elapsed(), expected_first_wait);
        assert!(
            manager.ran,
            "initialization errors must not disable policy rounds"
        );
        assert!(manager.deinitialized);
    }
}
