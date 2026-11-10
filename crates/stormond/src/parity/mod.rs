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
        .start_paused(true)
        .build()
        .unwrap();
    runtime.block_on(start(
        &mut platform,
        &open,
        &json,
        &mut Cycles::Paced {
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
            let python_file = persisted_counters(&sc);
            let rust = rust_half(py, harness, &sc, name, polls);
            if matches!(
                name.as_str(),
                "fresh_start" | "statedb_baseline" | "json_baseline" | "short_intervals"
            ) {
                assert_eq!(
                    python_file,
                    persisted_counters(&sc),
                    "[{name}] persisted FSIO counters"
                );
            }
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

fn persisted_counters(scenarios: &Bound<'_, PyModule>) -> serde_json::Value {
    let path: PathBuf = scenarios
        .call_method0("json_path")
        .unwrap()
        .extract()
        .unwrap();
    let text = std::fs::read_to_string(path).unwrap();
    let mut document: serde_json::Value = serde_json::from_str(&text).unwrap();
    // New boot identity is intentionally additive metadata. Compare every
    // persisted disk/counter field, excluding only the sync timestamp and ID.
    let fields = document.as_object_mut().unwrap();
    fields.remove("successful_sync_time");
    fields.remove("boot_id");
    for row in fields.values_mut() {
        if let Some(row) = row.as_object_mut() {
            row.remove("boot_id");
            row.remove("baseline_valid");
        }
    }
    document
}

#[test]
fn new_json_boot_metadata_is_accepted_by_the_unchanged_python_loader() {
    parity::with_harness(|py, harness| {
        let scenarios = parity::module(py, "stormond_json_compatibility", SCENARIOS);
        scenarios.call_method1("setup", (harness, ROOT)).unwrap();
        let table = pmon_common::db::MockTable::new();
        for field in fsio::SYNC_FIELDS {
            table.set("sda", &[(field, "100".into())]).unwrap();
        }
        let document = fsio::sync_document(&["sda".into()], &table, "now").unwrap();
        let (loaded, fields): (bool, std::collections::BTreeMap<String, String>) = scenarios
            .call_method1("load_new_json_document", (document,))
            .unwrap()
            .extract()
            .unwrap();
        assert!(
            loaded,
            "Python must accept files written by the Rust daemon"
        );
        assert!(fields.contains_key("boot_id"));
        for field in fsio::SYNC_FIELDS {
            assert_eq!(fields.get(field).map(String::as_str), Some("100"));
        }
    });
}

/// What the comparison finds and the daemon does not fix, each with the
/// place in Python it comes from and what would remove it.
const KNOWN: &[parity::Known] = &[
    parity::Known {
        scenario: "disk_getter_raises",
        contains: "only Rust logged: WARNING sda: no current FSIO counters; retaining its previous baseline while syncing healthy disks",
        why: "healthy disks persist; Rust explicitly reports the failed disk's retained or unknown baseline instead of writing null counters",
    },
];
