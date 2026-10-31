//! chassisd against the Python chassisd on a modular chassis (see
//! `pmon_common::parity`).
//!
//! The scenarios are in `scenarios.py`: the cards, this card's slot, and a
//! sequence of passes, CONFIG_DB changes and changes to the cards.  Both
//! halves are driven the same way: the Python half through
//! `chassisd:ModuleUpdater` and `chassisd:ModuleConfigUpdater`, the Rust half
//! through what `run_modular` and the config watcher run -- [`chassis_pass`]
//! and [`apply_config_event`] -- with every table a [`PyTable`] on the
//! harness's store, STATE_DB, CHASSIS_STATE_DB and CONFIG_DB alike.

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
        .unwrap_or_else(|e| panic!("[{name}] Python chassisd: {}", parity::traceback(py, e)));
    parity::python_side(py, harness)
}

fn rust_half(
    py: Python<'_>,
    harness: &Bound<'_, PyModule>,
    sc: &Bound<'_, PyModule>,
    name: &str,
) -> (Published, Vec<Line>) {
    let (build, steps, kind): (Bound<'_, PyAny>, Vec<Bound<'_, PyAny>>, String) = sc
        .call_method1("scenario", (name,))
        .and_then(|s| s.extract())
        .unwrap_or_else(|e| panic!("[{name}] scenario: {}", parity::traceback(py, e)));
    harness.call_method0("reset").unwrap();
    let chassis = build
        .call0()
        .unwrap_or_else(|e| panic!("[{name}] build: {}", parity::traceback(py, e)));
    harness.call_method1("use_chassis", (&chassis,)).unwrap();

    let log = pmon_common::logging::capture();
    let mut platform = platform_provider::open("chassisd", PlatformImpl::Pyo3)
        .unwrap_or_else(|e| panic!("[{name}] open: {e}"));
    if kind == "smartswitch" {
        let root: std::path::PathBuf = sc
            .call_method0("reboot_root")
            .and_then(|r| r.extract())
            .unwrap();
        smartswitch(py, name, &mut platform, &chassis, &steps, &root);
        let (tables, _) = parity::python_side(py, harness);
        return (tables, parity::rust_lines(log.lines()));
    }
    // What `run_modular` does before its loop.
    let slots = platform
        .get_chassis_info(SLOT_COLS)
        .unwrap_or_else(|e| panic!("[{name}] slots: {e}"));
    let my_slot = slots.my_slot.unwrap_or(INVALID_SLOT);
    let supervisor_slot = slots.supervisor_slot.unwrap_or(INVALID_SLOT);
    let is_supervisor = my_slot == supervisor_slot;
    let open = |db: &str, table: &str| -> Result<Box<dyn TableLike>, String> {
        Ok(Box::new(PyTable::new(db, table)))
    };
    let h = open_chassis_tables(&open, is_supervisor).unwrap();
    let tables = Tables {
        chassis: h.chassis.as_ref(),
        module: h.module.as_ref(),
        midplane: h.midplane.as_ref(),
        entity: h.entity.as_ref(),
        asic: h.asic.as_ref(),
        hostname: h.hostname.as_ref(),
        module_reboot: h.module_reboot.as_ref(),
        config: Some(h.config.as_ref()),
        device_metadata: Some(h.device_metadata.as_ref()),
    };
    let midplane_initialized = answered(platform.init_midplane_switch(), false).unwrap_or(false);
    if !midplane_initialized {
        log::error!("Chassisd midplane intialization failed");
    }
    let mut updater = ModuleUpdater::new(
        my_slot,
        supervisor_slot,
        linecard_reboot_timeout(),
        midplane_initialized,
    );
    let modules =
        start_up_modules(&mut platform).unwrap_or_else(|code| panic!("[{name}] modules: {code}"));
    module_updater::publish_module_count(&modules, tables.chassis).unwrap();

    let mut read = Latch::new();
    for step in &steps {
        let kind: String = step.get_item(0).and_then(|k| k.extract()).unwrap();
        match kind.as_str() {
            "pass" => {
                if let Err(e) = chassis_pass(
                    &mut platform,
                    &mut updater,
                    &tables,
                    is_supervisor,
                    &mut read,
                    SystemTime::now(),
                ) {
                    log::error!("{e}");
                }
            }
            "config" => {
                // What the config watcher does with one CHASSIS_MODULE event.
                let key: String = step.get_item(1).and_then(|k| k.extract()).unwrap();
                let op: String = step.get_item(2).and_then(|k| k.extract()).unwrap();
                if config_updater::should_apply(Machine::Chassis, &key) {
                    let event = if op == "DEL" {
                        config_updater::Event::Del
                    } else {
                        config_updater::Event::Set { admin_status: None }
                    };
                    apply_config_event(&mut platform, Machine::Chassis, &key, event, &|_, _, _| {
                        unreachable!("a chassis card is not changed gracefully")
                    });
                }
            }
            _ => {
                step.get_item(1)
                    .and_then(|f| f.call1((&chassis,)))
                    .unwrap_or_else(|e| panic!("[{name}] step: {}", parity::traceback(py, e)));
            }
        }
    }
    let (tables, _) = parity::python_side(py, harness);
    (tables, parity::rust_lines(log.lines()))
}

/// The SmartSwitch half: what `run_smartswitch` does before its loop, a
/// pass's `refresh` and `check_midplane`, and what the reboot-cause watcher
/// does with a boot_id.
fn smartswitch(
    py: Python<'_>,
    name: &str,
    platform: &mut platform_provider::Platform,
    chassis: &Bound<'_, PyAny>,
    steps: &[Bound<'_, PyAny>],
    root: &std::path::Path,
) {
    let open = |db: &str, table: &str| -> Result<Box<dyn TableLike>, String> {
        Ok(Box::new(PyTable::new(db, table)))
    };
    let h = open_dpu_tables(&open).unwrap();
    let tables = dpu_updater::Tables {
        chassis: h.chassis.as_ref(),
        module: h.module.as_ref(),
        midplane: h.midplane.as_ref(),
        dpu_state: h.dpu_state.as_ref(),
        config: Some(h.config.as_ref()),
        device_metadata: Some(h.device_metadata.as_ref()),
    };
    let reboot_cause = PyTable::new(CHASSIS_STATE_DB, "REBOOT_CAUSE");
    let midplane_initialized = answered(platform.init_midplane_switch(), false).unwrap_or(false);
    if !midplane_initialized {
        log::error!("Chassisd midplane intialization failed");
    }
    let modules =
        start_up_modules(platform).unwrap_or_else(|code| panic!("[{name}] modules: {code}"));
    module_updater::publish_module_count(&modules, tables.chassis).unwrap();
    let mut updater = dpu_updater::DpuUpdater::new(
        &modules,
        thresholds(),
        midplane_initialized,
        root,
        Instant::now(),
    );
    for step in steps {
        let kind: String = step.get_item(0).and_then(|k| k.extract()).unwrap();
        match kind.as_str() {
            "pass" => {
                let modules = platform.get_modules(ModuleInfoCols::ALL).unwrap();
                if let Err(e) = updater.refresh(&modules, &tables) {
                    log::error!("{e}");
                }
                let mut hw = PlatformCycler {
                    platform: &mut *platform,
                };
                if let Err(e) = updater.check_midplane(&modules, &tables, &mut hw) {
                    log::error!("{e}");
                }
            }
            "boot" => {
                let module: String = step.get_item(1).and_then(|k| k.extract()).unwrap();
                let boot_id: String = step.get_item(2).and_then(|k| k.extract()).unwrap();
                dpu_reboot_cause::boot_id_update(
                    root,
                    &module,
                    &boot_id,
                    platform,
                    &reboot_cause,
                    &dpu_updater::time_name(),
                    dpu_updater::formatted_from_name,
                );
            }
            _ => {
                step.get_item(1)
                    .and_then(|f| f.call1((chassis,)))
                    .unwrap_or_else(|e| panic!("[{name}] step: {}", parity::traceback(py, e)));
            }
        }
    }
}

#[test]
fn chassisd_publishes_what_the_python_daemon_publishes() {
    let failures = parity::with_harness(|py, harness| {
        let sc = parity::module(py, "chassisd_parity", SCENARIOS);
        sc.call_method1("setup", (harness, ROOT))
            .unwrap_or_else(|e| panic!("scenarios: {}", parity::traceback(py, e)));
        let names: Vec<String> = sc.call_method0("names").and_then(|n| n.extract()).unwrap();
        let mut failures = Vec::new();
        for name in &names {
            let python = python_half(py, harness, &sc, name);
            let rust = rust_half(py, harness, &sc, name);
            // The DPU_STATE times are `date`-style strings of their own.
            failures.extend(parity::differences(name, &python, &rust, &["*_time"]));
        }
        sc.call_method0("teardown")
            .unwrap_or_else(|e| panic!("teardown: {}", parity::traceback(py, e)));
        failures
    });
    parity::assert_parity("chassisd", &failures, KNOWN);
}

/// What the comparison finds and the daemon does not fix, each with the
/// place in Python it comes from and what would remove it.
const KNOWN: &[parity::Known] = &[];
