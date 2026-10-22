//! syseepromd against the Python syseepromd (see `pmon_common::parity`).
//!
//! The table here is the vendor's to write: over the bridge,
//! `eeprom_update_db` runs the base class's `TlvInfoDecoder.update_eeprom_db`,
//! which writes through `swsscommon`.  So the Rust half's table is a
//! [`PyTable`] on the harness's store, where that writer's rows land, and
//! what is compared is this daemon's part: when it posts, when it notices the
//! table changed, what it clears, and what it logs.

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
        .unwrap_or_else(|e| panic!("[{name}] Python syseepromd: {}", parity::traceback(py, e)));
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

    let log = pmon_common::logging::capture();
    let mut platform = platform_provider::open("syseepromd", PlatformImpl::Pyo3)
        .unwrap_or_else(|e| panic!("[{name}] open: {e}"));
    let table = PyTable::new("STATE_DB", EEPROM_TABLE);
    // What `run` does before its loop, and then in it.
    let mut published = match publish(&mut platform, &table, POST_FAILED) {
        Ok(keys) => keys.unwrap_or_default(),
        Err(e) => panic!("[{name}] publish: {e}"),
    };
    for step in &steps {
        let kind: String = step.get_item(0).and_then(|k| k.extract()).unwrap();
        if kind == "pass" {
            if let Err(e) = republish_if_changed(&mut platform, &table, &mut published) {
                log::error!("{e}");
            }
        } else {
            step.get_item(1)
                .and_then(|f| f.call1((&chassis,)))
                .unwrap_or_else(|e| panic!("[{name}] step: {}", parity::traceback(py, e)));
        }
    }
    // The store holds what the platform wrote and what this daemon did to it.
    let (tables, _) = parity::python_side(py, harness);
    (tables, parity::rust_lines(log.lines()))
}

#[test]
fn syseepromd_publishes_what_the_python_daemon_publishes() {
    let failures = parity::with_harness(|py, harness| {
        let sc = parity::module(py, "syseepromd_parity", SCENARIOS);
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
    parity::assert_parity("syseepromd", &failures, KNOWN);
}

/// What the comparison finds and the daemon does not fix, each with the
/// place in Python it comes from and what would remove it.
const KNOWN: &[parity::Known] = &[];
