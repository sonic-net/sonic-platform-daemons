//! psud against the Python psud, on the mock platform the Python daemon's
//! own tests use (see `pmon_common::parity`).
//!
//! The scenarios are in `scenarios.py`.  Each runs twice, from a fresh
//! chassis: once through `psud:DaemonPsud.run`, and once through this
//! daemon's own pass ([`one_pass`]) over the platform API bridge, with the
//! counts published first as `run` publishes them.

use platform_provider::PlatformImpl;
use pmon_common::db::MockTable;
use pmon_common::parity::pyo3::prelude::*;
use pmon_common::parity::pyo3::types::PyModule;
use pmon_common::parity::{self, Line, Published};

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
        .unwrap_or_else(|e| panic!("[{name}] Python psud: {}", parity::traceback(py, e)));
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
    let mut platform = platform_provider::open("psud", PlatformImpl::Pyo3)
        .unwrap_or_else(|e| panic!("[{name}] open: {e}"));
    let (chassis_tbl, psu, fan, entity) = (
        MockTable::new(),
        MockTable::new(),
        MockTable::new(),
        MockTable::new(),
    );
    let tables = Tables {
        psu: &psu,
        fan: &fan,
        entity: &entity,
    };
    let mut updater = Updater::new();
    let mut budget = Budget::default();
    let mut read = Latch::new();
    let mut counts = Counts::default();
    if let Ok(psus) = platform.get_psus() {
        counts.refresh(&psus, &chassis_tbl);
    }
    for step in &steps {
        let kind: String = step.get_item(0).and_then(|k| k.extract()).unwrap();
        if kind == "pass" {
            one_pass(
                &mut platform,
                &mut updater,
                &mut counts,
                &mut budget,
                &tables,
                &chassis_tbl,
                &mut read,
            );
        } else {
            step.get_item(1)
                .and_then(|f| f.call1((&chassis,)))
                .unwrap_or_else(|e| panic!("[{name}] step: {}", parity::traceback(py, e)));
        }
    }

    let mut out = Published::new();
    parity::put(
        &mut out,
        "STATE_DB",
        CHASSIS_INFO_TABLE,
        parity::rows(&chassis_tbl),
    );
    parity::put(&mut out, "STATE_DB", PSU_INFO_TABLE, parity::rows(&psu));
    parity::put(&mut out, "STATE_DB", FAN_INFO_TABLE, parity::rows(&fan));
    parity::put(
        &mut out,
        "STATE_DB",
        PHYSICAL_ENTITY_INFO_TABLE,
        parity::rows(&entity),
    );
    (out, parity::rust_lines(log.lines()))
}

#[test]
fn psud_publishes_what_the_python_daemon_publishes() {
    let failures = parity::with_harness(|py, harness| {
        let sc = parity::module(py, "psud_parity", SCENARIOS);
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
    parity::assert_parity("psud", &failures, KNOWN);
}

/// What the comparison finds and the daemon does not fix, each with the
/// place in Python it comes from and what would remove it.
const KNOWN: &[parity::Known] = &[
    // Unnamed PSUs and PDBs.  psud names one for its rows from an `index`
    // attribute the base class does not have, falling back to 'unknown' for
    // every one of them (`psud:DaemonPsud._update_single_power_entity_data`,
    // `_update_single_psu_entity_info`), and its fans '{} FAN {}'
    // (`psud:DaemonPsud._update_psu_fan_data`) -- not what psud's own
    // `get_psu_key` / `get_pdb_key`, nor thermalctld, call them.  The facade
    // gives the one name everything else uses: 'PSU 1', 'PDB 1', '... fan 1'.
    parity::Known {
        scenario: "unnamed_devices",
        contains: "PSU 1",
        why: "psud's own fallback names for an unnamed PSU disagree with the facade's",
    },
    parity::Known {
        scenario: "unnamed_devices",
        contains: "PSU 2",
        why: "psud's own fallback names for an unnamed PSU disagree with the facade's",
    },
    parity::Known {
        scenario: "unnamed_devices",
        contains: "PDB 1",
        why: "psud's own fallback names for an unnamed PDB disagree with the facade's",
    },
    parity::Known {
        scenario: "unnamed_devices",
        contains: "unknown",
        why: "psud's own fallback names for an unnamed PSU disagree with the facade's",
    },
];
