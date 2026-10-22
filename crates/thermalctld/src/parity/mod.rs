//! thermalctld against the Python thermalctld, on the mock platform the
//! Python daemon's own tests use (see `pmon_common::parity`).
//!
//! The scenarios are in `scenarios.py`: a platform, and what happens to it
//! between updates.  Each runs twice, from a fresh chassis each time: once
//! through `thermalctld:FanUpdater` and `thermalctld:TemperatureUpdater`, and
//! once through this crate's [`FanUpdater`] and [`TemperatureUpdater`] over
//! the platform API bridge.  Then every table either wrote, and every line
//! either logged at NOTICE or above, are compared.

use std::sync::atomic::AtomicBool;
use std::sync::Arc;
use std::time::{Duration, Instant};

use clap::Parser;
use platform_api::{ChassisInfoCols, PlatformApi};
use platform_provider::PlatformImpl;
use pmon_common::parity::pyo3::prelude::*;
use pmon_common::parity::pyo3::types::PyModule;
use pmon_common::parity::{self, Line, Published};
use tokio::sync::watch;

use crate::db::mock::MockDb;
use crate::db::{FAN_DRAWER_INFO, FAN_INFO, PHYSICAL_ENTITY_INFO, TEMPERATURE_INFO};
use crate::fan_updater::{FanUpdater, UpdateError};
use crate::monitor::Monitor;
use crate::polling::PollingIntervals;
use crate::temp_updater::TemperatureUpdater;

const SCENARIOS: &str = include_str!("scenarios.py");
const ROOT: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../..");

fn python_half(
    py: Python<'_>,
    harness: &Bound<'_, PyModule>,
    sc: &Bound<'_, PyModule>,
    name: &str,
) -> (Published, Vec<Line>) {
    sc.call_method1("run_python", (name,))
        .unwrap_or_else(|e| panic!("[{name}] Python thermalctld: {}", parity::traceback(py, e)));
    parity::python_side(py, harness)
}

fn rust_half(
    py: Python<'_>,
    harness: &Bound<'_, PyModule>,
    sc: &Bound<'_, PyModule>,
    name: &str,
) -> (Published, Vec<Line>) {
    let (build, steps, intervals): (Bound<'_, PyAny>, Vec<Bound<'_, PyAny>>, Bound<'_, PyAny>) = sc
        .call_method1("scenario", (name,))
        .and_then(|s| s.extract())
        .unwrap_or_else(|e| panic!("[{name}] scenario: {}", parity::traceback(py, e)));
    harness.call_method0("reset").unwrap();
    let chassis = build
        .call0()
        .unwrap_or_else(|e| panic!("[{name}] build: {}", parity::traceback(py, e)));
    harness.call_method1("use_chassis", (&chassis,)).unwrap();

    // As `thermalctld:ThermalMonitor.__init__` resolves them: a thermal with
    // no interval of its own runs at the cycle only when another has one.
    let thermals: std::collections::HashMap<String, f64> = intervals
        .get_item("thermals")
        .and_then(|t| t.extract())
        .unwrap();
    let psu: Option<f64> = intervals.get_item("psu").and_then(|p| p.extract()).unwrap();
    let default_thermal = (!thermals.is_empty()).then_some(60.0);
    let intervals = PollingIntervals {
        fan_drawer: None,
        psu,
        thermals,
        fan: None,
        default_thermal,
    };

    let log = pmon_common::logging::capture();
    let mut platform = platform_provider::open("thermalctld", PlatformImpl::Pyo3)
        .unwrap_or_else(|e| panic!("[{name}] open: {e}"));
    let db = MockDb::new(false);
    let mut fans = FanUpdater::new();
    let mut temps = TemperatureUpdater::with_mirror(None);
    let t0 = Instant::now();
    for step in &steps {
        let kind: String = step.get_item(0).and_then(|k| k.extract()).unwrap();
        match kind.as_str() {
            // What `monitor` does with a failed pass, so that one shows up
            // as a line the Python run did not log.
            "fan" => match fans.update(&mut platform, &db.db, &|| false) {
                Ok(()) => {}
                Err(UpdateError::Platform(e)) => log::warn!("fan update failed: {e}"),
                Err(UpdateError::Db(e)) => log::error!("fan update failed: {e}"),
            },
            "temp" => {
                let secs: f64 = step.get_item(1).and_then(|s| s.extract()).unwrap();
                let now = t0 + Duration::from_secs_f64(secs);
                if let Err(e) = temps.update(&mut platform, &db.db, &intervals, now, &|| false) {
                    log::warn!("temperature update failed: {e}");
                }
            }
            _ => {
                step.get_item(1)
                    .and_then(|f| f.call1((&chassis,)))
                    .unwrap_or_else(|e| panic!("[{name}] step: {}", parity::traceback(py, e)));
            }
        }
    }

    (published(&db), parity::rust_lines(log.lines()))
}

/// What the Rust half wrote, as the Python half's tables read.
fn published(db: &MockDb) -> Published {
    let mut out = Published::new();
    parity::put(
        &mut out,
        "STATE_DB",
        TEMPERATURE_INFO,
        parity::rows(&db.temperature),
    );
    parity::put(&mut out, "STATE_DB", FAN_INFO, parity::rows(&db.fan));
    parity::put(
        &mut out,
        "STATE_DB",
        FAN_DRAWER_INFO,
        parity::rows(&db.fan_drawer),
    );
    parity::put(
        &mut out,
        "STATE_DB",
        PHYSICAL_ENTITY_INFO,
        parity::rows(&db.physical_entity),
    );
    out
}

#[test]
fn thermalctld_publishes_what_the_python_daemon_publishes() {
    let failures = parity::with_harness(|py, harness| {
        let sc = parity::module(py, "thermalctld_parity", SCENARIOS);
        sc.call_method1("setup", (harness, ROOT))
            .unwrap_or_else(|e| panic!("scenarios: {}", parity::traceback(py, e)));
        let names: Vec<String> = sc.call_method0("names").and_then(|n| n.extract()).unwrap();
        let mut failures = Vec::new();
        for name in &names {
            let python = python_half(py, harness, &sc, name);
            let rust = rust_half(py, harness, &sc, name);
            failures.extend(parity::differences(name, &python, &rust, &["timestamp"]));
        }
        failures
    });
    parity::assert_parity("thermalctld", &failures, KNOWN);
}

/// What the comparison finds and the daemon does not fix, each with the
/// place in Python it comes from and what would remove it.
const KNOWN: &[parity::Known] = &[];

/// How long either daemon may take from SIGTERM to gone, in seconds; the
/// vendor's wait, which neither may sit out, is several times this.
const PROMPT: f64 = 5.0;

/// The Rust half of `stop_while_starting`: `main::run` as `main` drives it,
/// on a thread of its own so that the init thread's bridge can take the GIL,
/// with the shutdown sent once the manager is waiting and the first pass has
/// published.  What it wrote and logged, and the seconds from the shutdown to
/// `run` returning.
fn stop_while_starting(waiting: Py<PyAny>) -> (Published, Vec<Line>, f64) {
    let log = pmon_common::logging::capture();
    let mut platform = platform_provider::open("thermalctld", PlatformImpl::Pyo3)
        .unwrap_or_else(|e| panic!("open: {e}"));
    let chassis = platform.get_chassis_info(ChassisInfoCols::ALL).ok();
    let db = MockDb::new(false);
    let mut monitor = Monitor::with_env(0.001, 60.0, 30.0, PollingIntervals::default(), false);
    let args = crate::Args::try_parse_from(["thermalctld"]).expect("the defaults parse");

    // What `main`'s signal task does with a SIGTERM, once the daemon is where
    // the scenario wants it.
    let (tx, rx) = watch::channel(false);
    let (fan, temperature) = (db.fan.clone(), db.temperature.clone());
    let signal = std::thread::spawn(move || {
        let waited = Python::with_gil(|py| {
            waiting
                .bind(py)
                .call_method1("wait", (PROMPT,))
                .and_then(|w| w.is_truthy())
        });
        assert!(
            matches!(waited, Ok(true)),
            "the thermal manager never started waiting"
        );
        let deadline = Instant::now() + Duration::from_secs_f64(PROMPT);
        while fan.is_empty() || temperature.is_empty() {
            assert!(Instant::now() < deadline, "the first pass never published");
            std::thread::sleep(Duration::from_millis(10));
        }
        let sent = Instant::now();
        tx.send(true).expect("the daemon is listening");
        sent
    });

    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("a runtime");
    let db_lost = rt.block_on(crate::run(
        &mut platform,
        &db.db,
        &mut monitor,
        &args,
        chassis.as_ref(),
        &|ready: Arc<AtomicBool>| {
            std::thread::Builder::new()
                .name("tm-init".into())
                .spawn(move || crate::tm_init_thread_main(PlatformImpl::Pyo3, ready))
        },
        &|_interval, _rx| panic!("leak detection is off on this chassis"),
        rx,
    ));
    let done = Instant::now();
    let sent = signal
        .join()
        .unwrap_or_else(|_| panic!("the signal never went"));
    assert_eq!(db_lost, None);
    (
        published(&db),
        parity::rust_lines(log.lines()),
        (done - sent).as_secs_f64(),
    )
}

/// SIGTERM while the vendor's thermal manager is still starting: both
/// daemons stop at once and take their rows with them.
///
/// The vendor is mlnx's shape: a handler installed while its chassis is
/// built ends the start-up wait and chains to whatever was installed before
/// it.  Python runs it when the signal arrives, ahead of thermalctld's own;
/// the Rust daemon owns the signal and hands it on (`notify_shutdown`) before
/// it joins the thread the wait is on.  Without that the join sat out the
/// wait -- 300 s on SPC1 -- and supervisord SIGKILLed the daemon after 10,
/// its rows left in STATE_DB.
#[test]
fn thermalctld_stops_while_starting_as_the_python_daemon_does() {
    let mut took = (0.0, 0.0);
    let failures = parity::with_harness(|py, harness| {
        let sc = parity::module(py, "thermalctld_parity", SCENARIOS);
        sc.call_method1("setup", (harness, ROOT))
            .unwrap_or_else(|e| panic!("scenarios: {}", parity::traceback(py, e)));

        took.0 = sc
            .call_method0("stop_while_starting_python")
            .and_then(|t| t.extract())
            .unwrap_or_else(|e| panic!("Python thermalctld: {}", parity::traceback(py, e)));
        let python = parity::python_side(py, harness);

        let waiting: Py<PyAny> = sc
            .call_method0("stop_while_starting_rust")
            .unwrap_or_else(|e| panic!("vendor: {}", parity::traceback(py, e)))
            .unbind();
        let rust = std::thread::spawn(move || stop_while_starting(waiting));
        let (tables, mut lines, secs) = py
            .allow_threads(|| rust.join())
            .unwrap_or_else(|_| panic!("Rust thermalctld panicked"));
        took.1 = secs;
        // The vendor's own lines, logged from Python while the Rust daemon ran.
        lines.extend(parity::python_side(py, harness).1);

        parity::differences(
            "stop_while_starting",
            &python,
            &(tables, lines),
            &["timestamp"],
        )
    });
    parity::assert_parity("thermalctld", &failures, KNOWN);
    assert!(
        took.0 < PROMPT,
        "Python thermalctld took {:.1} s to stop",
        took.0
    );
    assert!(
        took.1 < PROMPT,
        "Rust thermalctld took {:.1} s to stop",
        took.1
    );
}
