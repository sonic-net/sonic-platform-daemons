//! pcied against the Python pcied (see `pmon_common::parity`).
//!
//! The scenarios are in `scenarios.py`: the vendor's Pcie object, a PCI
//! devices directory, the detach table.  The Rust half's tables are
//! [`PyTable`]s on the harness's store, because the scenario writes the
//! detach table there before the daemon reads it.

use std::path::PathBuf;

use platform_provider::PlatformImpl;
use pmon_common::parity::pyo3::prelude::*;
use pmon_common::parity::pyo3::types::PyModule;
use pmon_common::parity::{self, Line, Published, PyTable};

use super::*;

const SCENARIOS: &str = include_str!("scenarios.py");
const ROOT: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../..");

fn python_half(
    py: Python<'_>,
    harness: &Bound<'_, PyModule>,
    sc: &Bound<'_, PyModule>,
    name: &str,
) -> (Published, Vec<Line>) {
    sc.call_method1("run_python", (name,))
        .unwrap_or_else(|e| panic!("[{name}] Python pcied: {}", parity::traceback(py, e)));
    parity::python_side(py, harness)
}

fn rust_half(
    py: Python<'_>,
    harness: &Bound<'_, PyModule>,
    sc: &Bound<'_, PyModule>,
    name: &str,
) -> (Published, Vec<Line>) {
    let (build, steps): (Bound<'_, PyAny>, Vec<Bound<'_, PyAny>>) = sc
        .call_method1("scenario", (name,))
        .and_then(|s| s.extract())
        .unwrap_or_else(|e| panic!("[{name}] scenario: {}", parity::traceback(py, e)));
    harness.call_method0("reset").unwrap();
    let chassis = build
        .call0()
        .unwrap_or_else(|e| panic!("[{name}] build: {}", parity::traceback(py, e)));
    harness.call_method1("use_chassis", (&chassis,)).unwrap();
    let sysfs: PathBuf = sc.call_method0("sysfs").and_then(|p| p.extract()).unwrap();

    let log = pmon_common::logging::capture();
    let mut platform = platform_provider::open("pcied", PlatformImpl::Pyo3)
        .unwrap_or_else(|e| panic!("[{name}] open: {e}"));
    let (device, status, detach) = (
        PyTable::new("STATE_DB", PCIE_DEVICE_TABLE),
        PyTable::new("STATE_DB", PCIE_STATUS_TABLE),
        PyTable::new("STATE_DB", PCIE_DETACH_INFO_TABLE),
    );
    let tables = Tables {
        device: &device,
        status: &status,
        detach: &detach,
    };
    let smartswitch = is_smartswitch(&mut platform);
    let mut read = Latch::new();
    for step in &steps {
        let kind: String = step.get_item(0).and_then(|k| k.extract()).unwrap();
        if kind == "pass" {
            if let Err(e) = one_pass(&mut platform, smartswitch, &sysfs, &tables, &mut read) {
                log::error!("{e}");
            }
        } else {
            step.get_item(1)
                .and_then(|f| f.call1((&chassis,)))
                .unwrap_or_else(|e| panic!("[{name}] step: {}", parity::traceback(py, e)));
        }
    }
    let (tables, _) = parity::python_side(py, harness);
    (tables, parity::rust_lines(log.lines()))
}

#[test]
fn pcied_publishes_what_the_python_daemon_publishes() {
    let failures = parity::with_harness(|py, harness| {
        let sc = parity::module(py, "pcied_parity", SCENARIOS);
        sc.call_method1("setup", (harness, ROOT))
            .unwrap_or_else(|e| panic!("scenarios: {}", parity::traceback(py, e)));
        let names: Vec<String> = sc.call_method0("names").and_then(|n| n.extract()).unwrap();
        let mut failures = Vec::new();
        for name in &names {
            let python = python_half(py, harness, &sc, name);
            let rust = rust_half(py, harness, &sc, name);
            failures.extend(parity::differences(name, &python, &rust, &[]));
        }
        failures
    });
    parity::assert_parity("pcied", &failures, KNOWN);
}

/// What the comparison finds and the daemon does not fix, each with the
/// place in Python it comes from and what would remove it.
const KNOWN: &[parity::Known] = &[];
