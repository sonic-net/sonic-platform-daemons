//! stormond against the Python stormond (see `pmon_common::parity`).
//!
//! The scenarios are in `scenarios.py`: the disks, the two baselines a
//! previous run left (the FSIO JSON file and STATE_DB), CONFIG_DB's
//! intervals.  The Rust half runs [`start`] -- what `main` runs -- for a
//! fixed number of polls and its exit sync; the Python half runs what its
//! `main` does for the same number of polls, and its SIGTERM handler's sync.
//! Times are masked: what is compared is every counter, every row and
//! every line.

use std::path::PathBuf;

use platform_provider::PlatformImpl;
use pmon_common::cycles::Cycles;
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
    polls: usize,
) -> (Published, Vec<Line>) {
    sc.call_method1("run_python", (name, polls))
        .unwrap_or_else(|e| panic!("[{name}] Python stormond: {}", parity::traceback(py, e)));
    parity::python_side(py, harness)
}

fn rust_half(
    py: Python<'_>,
    harness: &Bound<'_, PyModule>,
    sc: &Bound<'_, PyModule>,
    name: &str,
    polls: usize,
) -> (Published, Vec<Line>) {
    let (build, _): (Bound<'_, PyAny>, usize) = sc
        .call_method1("scenario", (name,))
        .and_then(|s| s.extract())
        .unwrap_or_else(|e| panic!("[{name}] scenario: {}", parity::traceback(py, e)));
    harness.call_method0("reset").unwrap();
    build
        .call0()
        .unwrap_or_else(|e| panic!("[{name}] build: {}", parity::traceback(py, e)));
    let json: PathBuf = sc
        .call_method0("json_path")
        .and_then(|p| p.extract())
        .unwrap();

    let log = pmon_common::logging::capture();
    // What `main` says before it opens anything.
    pmon_common::notice!("Starting Storage Monitoring Daemon");
    let mut platform = platform_provider::open("stormond", PlatformImpl::Pyo3)
        .unwrap_or_else(|e| panic!("[{name}] open: {e}"));
    let open = |db: &str, table: &str| -> Result<Box<dyn TableLike>, String> {
        Ok(Box::new(PyTable::new(db, table)))
    };
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    runtime.block_on(start(
        &mut platform,
        &open,
        &json,
        &mut Cycles::Fixed {
            remaining: polls,
            code: 0,
        },
    ));
    let (tables, _) = parity::python_side(py, harness);
    (tables, parity::rust_lines(log.lines()))
}

#[test]
fn stormond_publishes_what_the_python_daemon_publishes() {
    let failures = parity::with_harness(|py, harness| {
        let sc = parity::module(py, "stormond_parity", SCENARIOS);
        sc.call_method1("setup", (harness, ROOT))
            .unwrap_or_else(|e| panic!("scenarios: {}", parity::traceback(py, e)));
        let names: Vec<String> = sc.call_method0("names").and_then(|n| n.extract()).unwrap();
        let mut failures = Vec::new();
        for name in &names {
            let (_, polls): (Bound<'_, PyAny>, usize) = sc
                .call_method1("scenario", (name,))
                .and_then(|s| s.extract())
                .unwrap();
            let python = python_half(py, harness, &sc, name, polls);
            let rust = rust_half(py, harness, &sc, name, polls);
            failures.extend(parity::differences(
                name,
                &python,
                &rust,
                &["last_sync_time", "successful_sync_time"],
            ));
        }
        failures
    });
    parity::assert_parity("stormond", &failures, KNOWN);
}

/// What the comparison finds and the daemon does not fix, each with the
/// place in Python it comes from and what would remove it.
const KNOWN: &[parity::Known] = &[];
