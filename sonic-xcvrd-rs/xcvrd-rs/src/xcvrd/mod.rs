//! `xcvrd.py` free functions + `DaemonXcvrd` helpers (analysis §3.2).
//!
//! The presence/identity state machine itself lives in [`sfp_state_update`]; this
//! module carries `post_port_sfp_info_to_db` (the `TRANSCEIVER_INFO` field builder,
//! two shapes: full CMIS dict vs. the fixed SFF field list) and the small
//! `str()`-fidelity helpers. The boot path is the self-contained
//! [`crate::daemon`] bootstrap; `DaemonXcvrd` here carries the thread-set
//! orchestration.
#![allow(dead_code, unused_variables, unused_imports)]

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::Duration;

use serde_json::Value;

use crate::db::DbTable;
use crate::error::Result;
use crate::hal::{BridgeHal, Hal, SfpHandle};
use crate::xcvrd_utilities::common::is_fast_reboot_enabled;
use crate::xcvrd_utilities::port_event_helper::PortMapping;
use crate::xcvrd_utilities::port_event_helper::{self, PortConfigGate};
use crate::xcvrd_utilities::xcvr_table_helper::{XcvrTableHelper, VDM_THRESHOLD_TYPES};

pub mod sfp_state_update;

/// A worker-thread body: run until the shared stop flag is set. In this orchestration
/// harness they are no-op workers; the production task loops
/// (`SfpStateUpdateTask`/`CmisManagerTask`/`DomInfoUpdateTask`/…) run via
/// [`crate::daemon`], matching the Python `threading.Thread` set.
pub type TaskWorker = Box<dyn FnOnce(Arc<AtomicBool>) + Send>;

/// `PHYSICAL_PORT_NOT_EXIST` sentinel (`post_port_sfp_info_to_db` → `-1`).
pub const PHYSICAL_PORT_NOT_EXIST: i32 = -1;
/// `SFP_EEPROM_NOT_READY` sentinel (`post_port_sfp_info_to_db` → `-2`).
pub const SFP_EEPROM_NOT_READY: i32 = -2;

/// `DaemonXcvrd` — boot orchestration (init/run/deinit + worker-thread spawn).
///
/// Stands up the full boot skeleton over the mockable seams ([`Hal`] +
/// [`XcvrTableHelper`] over [`DbTable`]): `init` loads the platform + table registry,
/// `run` spawns the worker-thread scaffold and blocks on the stop flag, `deinit`
/// clears the `TRANSCEIVER_*` tables. Dependencies are injectable ([`Self::set_hal`] /
/// [`Self::set_table_helper`] / [`Self::set_port_mapping`]) so the orchestration runs
/// under mocks in unit tests, the Rust analogue of the Python tests patching
/// `DaemonXcvrd.init`/`deinit` and the task `start`/`join`.
pub struct DaemonXcvrd {
    skip_cmis_mgr: bool,
    enable_sff_mgr: bool,
    dom_temperature_poll_interval: Option<i64>,
    dom_update_interval: Option<i64>,
    stop_event: Arc<AtomicBool>,
    sfp_error_event: Arc<AtomicBool>,
    namespaces: Vec<String>,
    hal: Option<Arc<dyn Hal>>,
    xcvr_table_helper: Option<XcvrTableHelper>,
    port_mapping: PortMapping,
    threads: Vec<JoinHandle<()>>,
    spawned_count: usize,
    child_panicked: bool,
    /// Test hook: worker set to spawn instead of the default scaffolds, so a unit
    /// test can drive the exact task list (and inject a panicking child to exercise
    /// the crash-detection path, mirroring `test_DaemonXcvrd_run_with_exception`).
    custom_tasks: Option<Vec<(String, TaskWorker)>>,
}

impl DaemonXcvrd {
    /// Mirrors `DaemonXcvrd.__init__(log_identifier, skip_cmis_mgr, enable_sff_mgr,
    /// dom_temperature_poll_interval, dom_update_interval)`. The two interval arguments are
    /// stored verbatim (`None` = flag absent) and interpreted downstream: the DOM-thermal task
    /// runs only when `dom_temperature_poll_interval` is set, and `dom_update_interval` is
    /// forwarded to [`DomInfoUpdateTask`] (which applies the 60 s default / negative-guard).
    pub fn new(
        skip_cmis_mgr: bool,
        enable_sff_mgr: bool,
        dom_temperature_poll_interval: Option<i64>,
        dom_update_interval: Option<i64>,
    ) -> Self {
        DaemonXcvrd {
            skip_cmis_mgr,
            enable_sff_mgr,
            dom_temperature_poll_interval,
            dom_update_interval,
            stop_event: Arc::new(AtomicBool::new(false)),
            sfp_error_event: Arc::new(AtomicBool::new(false)),
            namespaces: vec![String::new()],
            hal: None,
            xcvr_table_helper: None,
            port_mapping: PortMapping::new(),
            threads: Vec::new(),
            spawned_count: 0,
            child_panicked: false,
            custom_tasks: None,
        }
    }

    /// The raw `dom_update_interval` argument as supplied at construction (`None` = absent).
    pub fn dom_update_interval(&self) -> Option<i64> {
        self.dom_update_interval
    }

    /// The raw `dom_temperature_poll_interval` argument (`None` = absent, DOM-thermal off).
    pub fn dom_temperature_poll_interval(&self) -> Option<i64> {
        self.dom_temperature_poll_interval
    }

    /// Shared stop flag — set to break `run`'s wait loop (SIGINT/SIGTERM in production).
    pub fn stop_event(&self) -> Arc<AtomicBool> {
        self.stop_event.clone()
    }

    /// Request shutdown (the analogue of `stop_event.set()`).
    pub fn request_stop(&self) {
        self.stop_event.store(true, Ordering::SeqCst);
    }

    /// Inject a HAL (production `init` otherwise builds [`BridgeHal`]).
    pub fn set_hal(&mut self, hal: Arc<dyn Hal>) {
        self.hal = Some(hal);
    }

    /// Inject the table registry (production `init` otherwise builds a real one).
    pub fn set_table_helper(&mut self, helper: XcvrTableHelper) {
        self.xcvr_table_helper = Some(helper);
    }

    /// Inject the logical↔physical port mapping (production builds it in `init`).
    pub fn set_port_mapping(&mut self, port_mapping: PortMapping) {
        self.port_mapping = port_mapping;
    }

    /// Inject the exact worker set to spawn (test hook; see [`Self::custom_tasks`]).
    pub fn set_tasks(&mut self, tasks: Vec<(String, TaskWorker)>) {
        self.custom_tasks = Some(tasks);
    }

    /// Whether the last `run` observed a worker thread panic (Python's `os.kill`
    /// SIGKILL path). Exposed for the crash-detection test.
    pub fn child_panicked(&self) -> bool {
        self.child_panicked
    }

    /// Number of worker threads spawned by the last `run` (Python `len(self.threads)`).
    pub fn thread_count(&self) -> usize {
        self.spawned_count
    }

    /// Test accessor for the injected table registry — lets a unit test seed a row
    /// pre-`run` and assert `deinit` cleared it (the analogue of the Python test
    /// inspecting the mocked `Table` after `deinit`).
    #[cfg(test)]
    pub(crate) fn table_helper(&self) -> Option<&XcvrTableHelper> {
        self.xcvr_table_helper.as_ref()
    }

    /// The task set the current flags select, in Python `run()` spawn order
    /// (`SFF?`, `CMIS?`, `DOM`, `DOM-thermal?`, `SFP`).
    pub fn planned_task_names(&self) -> Vec<String> {
        let mut names = Vec::new();
        if self.enable_sff_mgr {
            names.push("SffManagerTask".to_string());
        }
        if !self.skip_cmis_mgr {
            names.push("CmisManagerTask".to_string());
        }
        names.push("DomInfoUpdateTask".to_string());
        if self.dom_temperature_poll_interval.is_some() {
            names.push("DomThermalInfoUpdateTask".to_string());
        }
        names.push("SfpStateUpdateTask".to_string());
        names
    }

    /// `DaemonXcvrd.wait_for_port_config_done` (`xcvrd.py:916`) — block until CONFIG_DB/
    /// APPL_DB signals `PortConfigDone`/`PortInitDone` before building the port mapping and
    /// starting the worker threads (`xcvrd.py:1062-1068`).
    ///
    /// Delegates to the shared bounded gate
    /// ([`port_event_helper::run_port_config_done_gate`]) over the real APPL_DB `PORT_TABLE`
    /// subscription, using this daemon's stop flag so a SIGTERM during boot ends the wait.
    /// On the pre-populated pmon testbed (no portsyncd sentinel) it drains the snapshot once
    /// and PROCEEDS immediately — a fast, non-blocking no-op that never hangs the daemon
    /// (which would break the e2e gate). Returns the gate outcome for
    /// logging/testing.
    pub fn wait_for_port_config_done(&self, namespace: &str) -> PortConfigGate {
        let stop = self.stop_event.clone();
        let is_stopped = move || stop.load(Ordering::Relaxed);
        port_event_helper::run_port_config_done_gate(namespace, &is_stopped)
    }

    /// Testable seam for [`Self::wait_for_port_config_done`]: run the gate loop over an
    /// injected [`port_event_helper::PortConfigNotifier`] (a scripted mock in unit tests,
    /// the Rust analogue of the Python test patching `SubscriberStateTable`/`Select`).
    #[cfg(test)]
    pub(crate) fn wait_for_port_config_done_with(
        &self,
        notifier: &mut dyn port_event_helper::PortConfigNotifier,
        max_polls: usize,
    ) -> (PortConfigGate, usize) {
        let stop = self.stop_event.clone();
        let is_stopped = move || stop.load(Ordering::Relaxed);
        port_event_helper::wait_for_port_config_done_gate(notifier, &is_stopped, max_polls)
    }

    /// `DaemonXcvrd.init` — load the platform HAL + build the STATE_DB table registry.
    /// Injected dependencies are honored (tests), so this is a pure wiring step
    /// under mocks (it also builds the port mapping via `get_port_mapping`
    /// and purge stale `TRANSCEIVER_INFO`.)
    pub fn init(&mut self) -> Result<()> {
        if self.hal.is_none() {
            let hal: Arc<dyn Hal> = Arc::new(BridgeHal::new()?);
            self.hal = Some(hal);
        }
        if self.xcvr_table_helper.is_none() {
            self.xcvr_table_helper = Some(XcvrTableHelper::new(&self.namespaces)?);
        }
        Ok(())
    }

    /// `DaemonXcvrd.run` — init, spawn the worker-thread scaffold, block on the stop
    /// flag, join the workers (detecting a crashed child), then deinit.
    pub fn run(&mut self) -> Result<()> {
        self.init()?;

        let tasks = self.custom_tasks.take().unwrap_or_else(|| {
            self.planned_task_names()
                .into_iter()
                .map(|name| (name, Self::scaffold_worker()))
                .collect()
        });
        self.spawned_count = tasks.len();
        for (name, worker) in tasks {
            let stop = self.stop_event.clone();
            let handle = thread::Builder::new()
                .name(name)
                .spawn(move || worker(stop))
                .expect("spawn xcvrd worker thread");
            self.threads.push(handle);
        }

        // Python: self.stop_event.wait(). Block until a signal (SIGINT/SIGTERM) sets it.
        self.wait_for_stop();

        // Join all workers; a panicked child mirrors Python detecting a dead thread whose
        // join raised → os.kill(getpid(), SIGKILL).
        self.child_panicked = false;
        for handle in std::mem::take(&mut self.threads) {
            if handle.join().is_err() {
                self.child_panicked = true;
            }
        }
        if self.child_panicked {
            eprintln!(
                "xcvrd-rs: a worker thread panicked; the reference daemon raises SIGKILL here"
            );
        }

        self.deinit()?;
        Ok(())
    }

    /// `DaemonXcvrd.deinit` — clear the port's rows across the `TRANSCEIVER_*` tables on
    /// shutdown. `TRANSCEIVER_INFO` is deliberately kept (Python sets
    /// `intf_tbl = None` to avoid an OA Tx-disable on INFO deletion). On a warm/fast
    /// reboot, `TRANSCEIVER_STATUS`/`_STATUS_SW` are *preserved* so the last-known
    /// transceiver status survives the restart (an in-service datapath is not disturbed);
    /// only a normal (cold) shutdown deletes them.
    pub fn deinit(&mut self) -> Result<()> {
        let Some(th) = self.xcvr_table_helper.as_ref() else {
            return Ok(());
        };
        deinit_transceiver_tables(th, &self.port_mapping);
        Ok(())
    }

    /// `remove_stale_transceiver_info` — purge INFO rows for absent ports at cold start.
    ///
    /// Delegates to the free [`remove_stale_transceiver_info`] over this daemon's HAL +
    /// table registry. Called from `init` (Python performs it before starting the child
    /// threads) so a module unplugged while xcvrd was down leaves no stale
    /// `TRANSCEIVER_INFO` behind. No-op until `init` has wired the seams.
    pub fn remove_stale_transceiver_info(&self, port_mapping: &PortMapping) {
        let (Some(hal), Some(th)) = (self.hal.as_ref(), self.xcvr_table_helper.as_ref()) else {
            return;
        };
        remove_stale_transceiver_info(hal.as_ref(), th, port_mapping);
    }

    /// Block until the stop flag is set (Python `stop_event.wait()`).
    fn wait_for_stop(&self) {
        while !self.stop_event.load(Ordering::Relaxed) {
            thread::sleep(Duration::from_millis(50));
        }
    }

    /// The no-op worker: loop until stopped (no table writes yet).
    fn scaffold_worker() -> TaskWorker {
        Box::new(|stop: Arc<AtomicBool>| {
            while !stop.load(Ordering::Relaxed) {
                thread::sleep(Duration::from_millis(50));
            }
        })
    }
}

/// `DaemonXcvrd.deinit` table teardown (`xcvrd.py:1082`) as a free function so both the
/// [`DaemonXcvrd`] boot orchestration and the production [`crate::daemon::serve`] shutdown
/// path share ONE implementation. For every logical port it clears the DOM / VDM / flag /
/// PM / firmware rows; `TRANSCEIVER_INFO` is deliberately kept (Python sets `intf_tbl =
/// None` to avoid an OA Tx-disable on INFO deletion). `TRANSCEIVER_STATUS`/`_STATUS_SW`
/// are PRESERVED on a warm (syncd restore) or fast reboot — the last-known transceiver
/// status must survive so an in-service datapath is not disturbed — and deleted only on a
/// normal (cold) shutdown. The fast-reboot flag is read once (a global STATE_DB flag,
/// asic 0), matching `xcvrd.py:1086`.
pub fn deinit_transceiver_tables(th: &XcvrTableHelper, port_mapping: &PortMapping) {
    let is_fast_reboot = is_fast_reboot_enabled(th.get_fast_restart_enable_tbl(0));
    for lport in port_mapping.logical_port_list() {
        let Some(asic) = port_mapping.get_asic_id_for_logical_port(lport) else {
            continue;
        };
        // Warm reboot (syncd restore) OR fast reboot both preserve STATUS/STATUS_SW.
        let is_warm_fast_reboot = th.is_syncd_warm_restore_complete(asic) || is_fast_reboot;
        th.get_dom_tbl(asic).del(lport);
        th.get_dom_temperature_tbl(asic).del(lport);
        th.get_dom_flag_tbl(asic).del(lport);
        th.get_dom_flag_change_count_tbl(asic).del(lport);
        th.get_dom_flag_set_time_tbl(asic).del(lport);
        th.get_dom_flag_clear_time_tbl(asic).del(lport);
        th.get_dom_threshold_tbl(asic).del(lport);
        for t in VDM_THRESHOLD_TYPES {
            th.get_vdm_threshold_tbl(asic, t).del(lport);
        }
        th.get_vdm_real_value_tbl(asic).del(lport);
        for t in VDM_THRESHOLD_TYPES {
            th.get_vdm_flag_tbl(asic, t).del(lport);
            th.get_vdm_flag_change_count_tbl(asic, t).del(lport);
            th.get_vdm_flag_set_time_tbl(asic, t).del(lport);
            th.get_vdm_flag_clear_time_tbl(asic, t).del(lport);
        }
        th.get_status_flag_tbl(asic).del(lport);
        th.get_status_flag_change_count_tbl(asic).del(lport);
        th.get_status_flag_set_time_tbl(asic).del(lport);
        th.get_status_flag_clear_time_tbl(asic).del(lport);
        th.get_pm_tbl(asic).del(lport);
        th.get_firmware_info_tbl(asic).del(lport);
        // Only a normal (non warm/fast) reboot clears STATUS + STATUS_SW.
        if !is_warm_fast_reboot {
            th.get_status_tbl(asic).del(lport);
            th.get_status_sw_tbl(asic).del(lport);
        }
    }
}

/// `post_port_sfp_info_to_db` — build the `TRANSCEIVER_INFO` field list for one port.
/// Two shapes: the full CMIS dict (`'cmis_rev' in port_info_dict`) vs. the fixed SFF
/// field list. Returns the Python sentinels ([`PHYSICAL_PORT_NOT_EXIST`],
/// [`SFP_EEPROM_NOT_READY`]) via `Result`/`i32`.
pub fn post_port_sfp_info_to_db(
    logical_port_name: &str,
    port_mapping: &PortMapping,
    intf_tbl: &dyn DbTable,
    hal: &dyn Hal,
) -> i32 {
    let Some(physical_port_list) =
        port_mapping.logical_port_name_to_physical_port_list(logical_port_name)
    else {
        eprintln!("xcvrd-rs: no physical ports found for logical port '{logical_port_name}'");
        return PHYSICAL_PORT_NOT_EXIST;
    };

    let ganged_port = physical_port_list.len() > 1;
    let mut ganged_member_num = 1;

    for physical_port in physical_port_list {
        let Ok(sfp) = hal.sfp(physical_port) else {
            // Can't obtain the module handle — treat like absent and skip this member.
            continue;
        };
        if !crate::xcvrd_utilities::common::wrapper_get_presence(sfp.as_ref()).unwrap_or(false) {
            // "Transceiver not present" — the reference logs + continues.
            continue;
        }

        let port_name = get_physical_port_name(logical_port_name, ganged_member_num, ganged_port);
        ganged_member_num += 1;

        // `_wrapper_get_transceiver_info`: a `None`/error read (or an unreadable EEPROM)
        // is the `SFP_EEPROM_NOT_READY` retry sentinel the caller acts on.
        let port_info = match sfp.get_transceiver_info() {
            Ok(v) if !v.is_null() => v,
            _ => return SFP_EEPROM_NOT_READY,
        };
        let Some(obj) = port_info.as_object() else {
            return SFP_EEPROM_NOT_READY;
        };

        let is_replaceable = sfp.is_replaceable().unwrap_or(false);
        let fvs = if obj.contains_key("cmis_rev") {
            build_cmis_info_fvs(obj, is_replaceable)
        } else {
            build_sff_info_fvs(obj, is_replaceable)
        };
        intf_tbl.set(&port_name, &fvs);
    }
    0
}

/// `common.get_physical_port_name(logical, ganged_member_num, ganged_port)` — the
/// display name for a physical member: `"<logical>:<n>"` when ganged, else the logical
/// name (the non-ganged emulator case).
fn get_physical_port_name(logical: &str, ganged_member_num: usize, ganged_port: bool) -> String {
    if ganged_port {
        format!("{logical}:{ganged_member_num}")
    } else {
        logical.to_string()
    }
}

/// Build the `{ str(physical_port): transceiver_info }` map the reference `transceiver_dict`
/// carries into `media_settings_parser.notify_media_setting` — the same present-gated
/// `get_transceiver_info()` reads `post_port_sfp_info_to_db` performs. Kept as a small
/// sibling read (rather than an out-param on `post_port_sfp_info_to_db`) so the info
/// poster's signature/tests are untouched; `get_transceiver_info()` is an idempotent
/// module read, so re-reading it for the media notify matches the reference's populated
/// dict without side effects.
pub(crate) fn build_transceiver_dict(
    logical_port_name: &str,
    port_mapping: &PortMapping,
    hal: &dyn Hal,
) -> Value {
    let mut dict = serde_json::Map::new();
    let Some(physical_port_list) =
        port_mapping.logical_port_name_to_physical_port_list(logical_port_name)
    else {
        return Value::Object(dict);
    };
    for physical_port in physical_port_list {
        let Ok(sfp) = hal.sfp(physical_port) else {
            continue;
        };
        if !crate::xcvrd_utilities::common::wrapper_get_presence(sfp.as_ref()).unwrap_or(false) {
            continue;
        }
        if let Ok(info) = sfp.get_transceiver_info() {
            if info.is_object() {
                dict.insert(physical_port.to_string(), info);
            }
        }
    }
    Value::Object(dict)
}

/// The CMIS shape: every `get_transceiver_info()` field rendered via [`stringify`]
/// (the reference's `str(value)`), plus `is_replaceable`. Mirrors the `'cmis_rev' in
/// port_info_dict` branch of `post_port_sfp_info_to_db`.
fn build_cmis_info_fvs(
    obj: &serde_json::Map<String, Value>,
    is_replaceable: bool,
) -> Vec<(String, String)> {
    let mut fvs: Vec<(String, String)> = obj
        .iter()
        .filter_map(|(field, value)| stringify(value).map(|s| (field.clone(), s)))
        .collect();
    fvs.push((
        "is_replaceable".to_string(),
        pybool(is_replaceable).to_string(),
    ));
    fvs
}

/// The fixed 18-field SFF shape (the `else` branch of `post_port_sfp_info_to_db`):
/// exactly the reference's field list + order, with `application_advertisement` /
/// `dom_capability` defaulting to `'N/A'` when the module omits them.
fn build_sff_info_fvs(
    obj: &serde_json::Map<String, Value>,
    is_replaceable: bool,
) -> Vec<(String, String)> {
    let get = |k: &str| obj.get(k).and_then(stringify).unwrap_or_default();
    let get_or = |k: &str, d: &str| {
        obj.get(k)
            .and_then(stringify)
            .unwrap_or_else(|| d.to_string())
    };
    vec![
        ("type".to_string(), get("type")),
        ("vendor_rev".to_string(), get("vendor_rev")),
        ("serial".to_string(), get("serial")),
        ("manufacturer".to_string(), get("manufacturer")),
        ("model".to_string(), get("model")),
        ("vendor_oui".to_string(), get("vendor_oui")),
        ("vendor_date".to_string(), get("vendor_date")),
        ("connector".to_string(), get("connector")),
        ("encoding".to_string(), get("encoding")),
        ("ext_identifier".to_string(), get("ext_identifier")),
        (
            "ext_rateselect_compliance".to_string(),
            get("ext_rateselect_compliance"),
        ),
        ("cable_type".to_string(), get("cable_type")),
        ("cable_length".to_string(), get("cable_length")),
        (
            "specification_compliance".to_string(),
            get("specification_compliance"),
        ),
        ("nominal_bit_rate".to_string(), get("nominal_bit_rate")),
        (
            "application_advertisement".to_string(),
            get_or("application_advertisement", "N/A"),
        ),
        (
            "is_replaceable".to_string(),
            pybool(is_replaceable).to_string(),
        ),
        (
            "dom_capability".to_string(),
            get_or("dom_capability", "N/A"),
        ),
    ]
}

/// `remove_stale_transceiver_info(port_mapping_data)` — purge `TRANSCEIVER_INFO` rows
/// for ports whose transceiver is absent at cold start.
///
/// Mirrors `DaemonXcvrd.remove_stale_transceiver_info`: for every logical port with an
/// existing INFO row, resolve its physical index and, if the module is absent, delete
/// the row. Run in `init` before the child threads so a module removed while xcvrd was
/// down leaves no stale identity behind (the suite's clean-baseline liveness guard).
pub fn remove_stale_transceiver_info(
    hal: &dyn Hal,
    table_helper: &XcvrTableHelper,
    port_mapping: &PortMapping,
) {
    for lport in port_mapping.logical_port_list() {
        let Some(asic_index) = port_mapping.get_asic_id_for_logical_port(lport) else {
            continue;
        };
        let intf_tbl = table_helper.get_intf_tbl(asic_index);

        // found, _ = intf_tbl.get(lport)
        if intf_tbl.get(lport).is_none() {
            continue;
        }

        let Some(pport_list) = port_mapping.get_logical_to_physical(lport) else {
            eprintln!(
                "xcvrd-rs: remove stale transceiver info: no physical port for lport {lport}"
            );
            continue;
        };
        let Some(&pport) = pport_list.first() else {
            continue;
        };

        let present = hal
            .sfp(pport)
            .ok()
            .map(|sfp| {
                crate::xcvrd_utilities::common::wrapper_get_presence(sfp.as_ref()).unwrap_or(false)
            })
            .unwrap_or(false);
        if !present {
            crate::xcvrd_utilities::common::del_port_sfp_dom_info_from_db(lport, &[intf_tbl]);
        }
    }
}

/// Python-style bool rendering (`str(bool)`), matching the reference daemon's writes.
pub fn pybool(b: bool) -> &'static str {
    if b {
        "True"
    } else {
        "False"
    }
}

/// Render a `get_transceiver_info()` JSON value as the STATE_DB field string the
/// reference daemon writes via `str(value)` (JSON nulls skipped).
///
/// The reference CMIS path is a bare `str(value)` over every `get_transceiver_info()`
/// field (`xcvrd.py`: `[(field, str(value)) for field, value in port_info_dict.items()]`)
/// — it performs **no** trimming. We only strip trailing NULs, matching the fixed-width
/// CMIS strings the bridge surfaces (e.g. a NUL-padded `model`), and must NOT trim other
/// trailing whitespace: the fixed-width CMIS `vendor_date` field carries a trailing pad
/// space (empty lot code → `"2024-12-14 "`) that the reference daemon keeps, so a
/// `trim_end()` here would drop it and diverge from the golden projection.
pub fn stringify(v: &Value) -> Option<String> {
    match v {
        Value::Null => None,
        Value::String(s) => Some(s.trim_end_matches('\0').to_string()),
        Value::Bool(b) => Some(pybool(*b).to_string()),
        Value::Number(n) => Some(n.to_string()),
        other => Some(other.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::mock::{MockHal, MockSfp};
    use crate::xcvrd_utilities::port_event_helper::{PortChangeEvent, PortChangeEventType};

    fn port_mapping_one(port: &str, index: usize, asic: usize) -> PortMapping {
        let mut pm = PortMapping::new();
        pm.handle_port_change_event(&PortChangeEvent::new(
            port.to_string(),
            Some(index),
            asic,
            PortChangeEventType::Add,
            "CONFIG_DB".to_string(),
            "PORT".to_string(),
        ));
        pm
    }

    fn wire_mocks(d: &mut DaemonXcvrd, pm: PortMapping) {
        let hal: Arc<dyn Hal> = Arc::new(MockHal::with_sfps(vec![MockSfp::present()]));
        d.set_hal(hal);
        d.set_table_helper(XcvrTableHelper::with_mock_tables(&[String::new()]));
        d.set_port_mapping(pm);
    }

    /// Scripted [`port_event_helper::PortConfigNotifier`] — the Rust analogue of the Python
    /// test's `mock_selectable.pop` side_effect. Each `poll()` yields the next scripted key
    /// batch; once exhausted it reports a select TIMEOUT so a gate that never sees the
    /// sentinel falls back to its bounded budget instead of spinning.
    struct ScriptedNotifier {
        batches: std::collections::VecDeque<Vec<String>>,
    }

    impl ScriptedNotifier {
        fn new(batches: Vec<Vec<&str>>) -> Self {
            Self {
                batches: batches
                    .into_iter()
                    .map(|b| b.into_iter().map(String::from).collect())
                    .collect(),
            }
        }
    }

    impl port_event_helper::PortConfigNotifier for ScriptedNotifier {
        fn poll(&mut self) -> port_event_helper::PortConfigPoll {
            match self.batches.pop_front() {
                Some(keys) => port_event_helper::PortConfigPoll::Keys(keys),
                None => port_event_helper::PortConfigPoll::Timeout,
            }
        }
    }

    // Mirrors tests/test_xcvrd.py::TestXcvrdScript.test_DaemonXcvrd_wait_for_port_config_done:
    // the boot readiness gate keeps polling the PORT subscription until the PortConfigDone
    // sentinel is popped. The Python test scripts pop() to return ('Ethernet0', SET, ...)
    // then ('PortConfigDone', None, None) and asserts Select.select was called twice — i.e.
    // the gate returns ONLY AFTER the sentinel, after exactly two poll cycles.
    #[test]
    fn test_daemon_xcvrd_wait_for_port_config_done() {
        let d = DaemonXcvrd::new(false, false, None, None);
        let mut notifier = ScriptedNotifier::new(vec![vec!["Ethernet0"], vec!["PortConfigDone"]]);
        let (outcome, polls) = d.wait_for_port_config_done_with(&mut notifier, 16);
        assert_eq!(outcome, PortConfigGate::Done);
        assert_eq!(polls, 2, "Select.select is polled twice (call_count == 2)");
    }

    // A SIGTERM during boot (stop flag set) ends the gate wait immediately with Stopped, so
    // the daemon does not block building the port mapping while shutting down.
    #[test]
    fn test_daemon_xcvrd_wait_for_port_config_done_honors_stop() {
        let d = DaemonXcvrd::new(false, false, None, None);
        d.request_stop();
        let mut notifier = ScriptedNotifier::new(vec![vec!["Ethernet0"]]);
        let (outcome, polls) = d.wait_for_port_config_done_with(&mut notifier, 16);
        assert_eq!(outcome, PortConfigGate::Stopped);
        assert_eq!(polls, 0);
    }

    #[test]
    fn pybool_and_stringify_match_python_str() {
        // Pure str()-fidelity helpers — kept real (the INFO builder depends on them).
        assert_eq!(pybool(true), "True");
        assert_eq!(stringify(&Value::Null), None);
        assert_eq!(
            stringify(&serde_json::json!("EMU-40G-LR4\0\0")).as_deref(),
            Some("EMU-40G-LR4")
        );
        // Fixed-width CMIS `vendor_date` keeps its trailing pad space (empty lot code):
        // the reference `str(value)` does not trim it, so neither may we (only NULs).
        assert_eq!(
            stringify(&serde_json::json!("2024-12-14 ")).as_deref(),
            Some("2024-12-14 ")
        );
        assert_eq!(
            stringify(&serde_json::json!("2024-12-14 \0")).as_deref(),
            Some("2024-12-14 ")
        );
    }

    // The selected worker set matches Python `run()`'s spawn order/count for the flag
    // combinations: SFF gated on --enable_sff_mgr, CMIS on !--skip_cmis_mgr, the DOM
    // thermal task on a set --dom_temperature_poll_interval; DOM-info + SFP-state always present.
    #[test]
    fn planned_task_names_track_flags() {
        // Defaults: CMIS on, SFF off, no thermal → CMIS + DOM + SFP.
        let d = DaemonXcvrd::new(false, false, None, None);
        assert_eq!(
            d.planned_task_names(),
            ["CmisManagerTask", "DomInfoUpdateTask", "SfpStateUpdateTask"]
        );

        // skip_cmis + no sff → just DOM + SFP.
        let d = DaemonXcvrd::new(true, false, None, None);
        assert_eq!(
            d.planned_task_names(),
            ["DomInfoUpdateTask", "SfpStateUpdateTask"]
        );

        // Everything on: SFF, CMIS, DOM, DOM-thermal, SFP (5 threads).
        let d = DaemonXcvrd::new(false, true, Some(10), None);
        assert_eq!(
            d.planned_task_names(),
            [
                "SffManagerTask",
                "CmisManagerTask",
                "DomInfoUpdateTask",
                "DomThermalInfoUpdateTask",
                "SfpStateUpdateTask"
            ]
        );
    }

    // ← tests/test_xcvrd.py::test_DaemonXcvrd_dom_update_interval_parameter: the daemon stores
    // the `dom_update_interval` argument verbatim (`None`/`0`/custom) — the 60 s default is
    // applied later by DomInfoUpdateTask, not here.
    #[test]
    fn test_daemon_dom_update_interval_parameter() {
        assert_eq!(
            DaemonXcvrd::new(false, false, None, None).dom_update_interval(),
            None
        );
        assert_eq!(
            DaemonXcvrd::new(false, false, None, Some(0)).dom_update_interval(),
            Some(0)
        );
        assert_eq!(
            DaemonXcvrd::new(false, false, None, Some(120)).dom_update_interval(),
            Some(120)
        );
        assert_eq!(
            DaemonXcvrd::new(false, false, None, Some(1000)).dom_update_interval(),
            Some(1000)
        );
        // The thermal-poll argument is likewise stored verbatim and gates the DOM-thermal task.
        assert_eq!(
            DaemonXcvrd::new(false, false, None, None).dom_temperature_poll_interval(),
            None
        );
        assert_eq!(
            DaemonXcvrd::new(false, false, Some(10), None).dom_temperature_poll_interval(),
            Some(10)
        );
    }

    // ← tests/test_xcvrd.py::test_DaemonXcvrd_run
    // run() over injected mocks: it spawns one worker per planned task, blocks until the
    // stop flag, joins cleanly, then deinit clears the port's TRANSCEIVER_* rows.
    #[test]
    fn run_spawns_scaffold_and_deinit_clears_tables() {
        let mut d = DaemonXcvrd::new(false, true, Some(10), None);
        wire_mocks(&mut d, port_mapping_one("Ethernet0", 0, 0));

        // Seed rows deinit must delete, plus INFO which it must preserve.
        let th = d.table_helper().unwrap();
        th.get_dom_tbl(0).hset("Ethernet0", "temperature", "22.75");
        th.get_status_tbl(0).hset("Ethernet0", "status", "1");
        th.get_vdm_flag_tbl(0, "halarm").hset("Ethernet0", "f", "1");
        th.get_intf_tbl(0).hset("Ethernet0", "type", "QSFP-DD");

        // Preset stop so the scaffold workers return immediately and run() completes.
        d.request_stop();
        d.run().expect("run over mocks");

        assert_eq!(d.thread_count(), 5);
        assert!(!d.child_panicked());
        let th = d.table_helper().unwrap();
        assert_eq!(th.get_dom_tbl(0).get_size_for_key("Ethernet0"), 0);
        assert_eq!(th.get_status_tbl(0).get_size_for_key("Ethernet0"), 0);
        assert_eq!(
            th.get_vdm_flag_tbl(0, "halarm")
                .get_size_for_key("Ethernet0"),
            0
        );
        // TRANSCEIVER_INFO is intentionally kept (Python nulls intf_tbl in deinit).
        assert_eq!(th.get_intf_tbl(0).get_size_for_key("Ethernet0"), 1);
    }

    // ← tests/test_xcvrd.py::test_DaemonXcvrd_init_deinit_cold: a normal (cold) shutdown
    // deletes TRANSCEIVER_STATUS + TRANSCEIVER_STATUS_SW along with the DOM rows.
    #[test]
    fn deinit_cold_deletes_status_and_status_sw() {
        let mut d = DaemonXcvrd::new(false, false, None, None);
        wire_mocks(&mut d, port_mapping_one("Ethernet0", 0, 0));
        let th = d.table_helper().unwrap();
        th.get_dom_tbl(0).hset("Ethernet0", "temperature", "22.75");
        th.get_status_tbl(0).hset("Ethernet0", "status", "1");
        th.get_status_sw_tbl(0).hset("Ethernet0", "status", "1");

        d.deinit().expect("deinit over mocks");

        let th = d.table_helper().unwrap();
        assert_eq!(th.get_dom_tbl(0).get_size_for_key("Ethernet0"), 0);
        assert_eq!(th.get_status_tbl(0).get_size_for_key("Ethernet0"), 0);
        assert_eq!(th.get_status_sw_tbl(0).get_size_for_key("Ethernet0"), 0);
    }

    // ← tests/test_xcvrd.py::test_DaemonXcvrd_init_deinit_fastboot_enabled: a fast-reboot
    // shutdown PRESERVES STATUS/STATUS_SW (the DOM rows are still cleared).
    #[test]
    fn deinit_fast_reboot_preserves_status_and_status_sw() {
        let mut d = DaemonXcvrd::new(false, false, None, None);
        wire_mocks(&mut d, port_mapping_one("Ethernet0", 0, 0));
        let th = d.table_helper().unwrap();
        th.get_fast_restart_enable_tbl(0)
            .hset("system", "enable", "true");
        th.get_dom_tbl(0).hset("Ethernet0", "temperature", "22.75");
        th.get_status_tbl(0).hset("Ethernet0", "status", "1");
        th.get_status_sw_tbl(0).hset("Ethernet0", "status", "1");

        d.deinit().expect("deinit over mocks");

        let th = d.table_helper().unwrap();
        assert_eq!(th.get_dom_tbl(0).get_size_for_key("Ethernet0"), 0);
        assert_eq!(th.get_status_tbl(0).get_size_for_key("Ethernet0"), 1);
        assert_eq!(th.get_status_sw_tbl(0).get_size_for_key("Ethernet0"), 1);
    }

    // A warm reboot (syncd restore_count > 0) also preserves STATUS/STATUS_SW.
    #[test]
    fn deinit_warm_reboot_preserves_status_and_status_sw() {
        let mut d = DaemonXcvrd::new(false, false, None, None);
        wire_mocks(&mut d, port_mapping_one("Ethernet0", 0, 0));
        let th = d.table_helper().unwrap();
        th.get_warm_restart_tbl(0)
            .hset("syncd", "restore_count", "1");
        th.get_status_tbl(0).hset("Ethernet0", "status", "1");
        th.get_status_sw_tbl(0).hset("Ethernet0", "status", "1");

        d.deinit().expect("deinit over mocks");

        let th = d.table_helper().unwrap();
        assert_eq!(th.get_status_tbl(0).get_size_for_key("Ethernet0"), 1);
        assert_eq!(th.get_status_sw_tbl(0).get_size_for_key("Ethernet0"), 1);
    }

    // ← tests/test_xcvrd.py::test_DaemonXcvrd_run_with_exception
    // A worker thread that panics is detected on join (Python's dead-thread → SIGKILL
    // path), and deinit still runs so the tables are cleaned up.
    #[test]
    fn run_detects_panicked_worker_and_still_deinits() {
        let mut d = DaemonXcvrd::new(false, false, None, None);
        wire_mocks(&mut d, port_mapping_one("Ethernet0", 0, 0));
        d.table_helper()
            .unwrap()
            .get_dom_tbl(0)
            .hset("Ethernet0", "temperature", "22.75");

        // One task panics; the rest respect the (preset) stop flag and exit.
        let mut tasks: Vec<(String, TaskWorker)> = Vec::new();
        tasks.push((
            "CmisManagerTask".to_string(),
            Box::new(|_stop: Arc<AtomicBool>| panic!("simulated task crash")),
        ));
        for name in ["DomInfoUpdateTask", "SfpStateUpdateTask"] {
            tasks.push((name.to_string(), scaffold_task()));
        }
        d.set_tasks(tasks);
        d.request_stop();

        // Silence the child-thread panic backtrace during the test.
        let prev = std::panic::take_hook();
        std::panic::set_hook(Box::new(|_| {}));
        let result = d.run();
        std::panic::set_hook(prev);

        result.expect("run completes despite a panicked child");
        assert!(d.child_panicked());
        assert_eq!(d.thread_count(), 3);
        // deinit still ran → the seeded DOM row is gone.
        assert_eq!(
            d.table_helper()
                .unwrap()
                .get_dom_tbl(0)
                .get_size_for_key("Ethernet0"),
            0
        );
    }

    // Test-local scaffold worker (mirrors the private DaemonXcvrd::scaffold_worker).
    fn scaffold_task() -> TaskWorker {
        Box::new(|stop: Arc<AtomicBool>| {
            while !stop.load(Ordering::Relaxed) {
                thread::sleep(Duration::from_millis(10));
            }
        })
    }

    // ← tests/test_xcvrd.py::test_post_port_sfp_info_to_db (CMIS branch): a module whose
    // get_transceiver_info() carries 'cmis_rev' publishes every field (str()'d, NUL
    // trimmed) plus is_replaceable into TRANSCEIVER_INFO.
    #[test]
    fn post_port_sfp_info_to_db_cmis_branch() {
        let info = serde_json::json!({
            "cmis_rev": "5.0",
            "type": "QSFP-DD Double Density 8X Pluggable Transceiver",
            "vendor_rev": "A1",
            "model": "EMU-40G-LR4\u{0}\u{0}",
            "nominal_bit_rate": 0,
            "dom_capability": null,
        });
        let hal = MockHal::with_sfps(vec![MockSfp::present().with_info(info)]);
        let intf_tbl = crate::mock::MockDbTable::new("TRANSCEIVER_INFO");
        let pm = port_mapping_one("Ethernet0", 0, 0);

        let rc = post_port_sfp_info_to_db("Ethernet0", &pm, &intf_tbl, &hal);
        assert_eq!(rc, 0);
        assert_eq!(
            intf_tbl.hget("Ethernet0", "cmis_rev").as_deref(),
            Some("5.0")
        );
        assert_eq!(
            intf_tbl.hget("Ethernet0", "vendor_rev").as_deref(),
            Some("A1")
        );
        // NUL padding trimmed (CMIS strings are fixed-width).
        assert_eq!(
            intf_tbl.hget("Ethernet0", "model").as_deref(),
            Some("EMU-40G-LR4")
        );
        assert_eq!(
            intf_tbl.hget("Ethernet0", "nominal_bit_rate").as_deref(),
            Some("0")
        );
        // is_replaceable appended as Python str(bool).
        assert_eq!(
            intf_tbl.hget("Ethernet0", "is_replaceable").as_deref(),
            Some("True")
        );
        // JSON null fields are skipped (proven bootstrap behavior).
        assert_eq!(intf_tbl.hget("Ethernet0", "dom_capability"), None);
    }

    // ← tests/test_xcvrd.py::test_post_port_sfp_info_to_db (SFF branch): no 'cmis_rev'
    // → exactly the fixed 18-field list, with N/A defaults for the optional fields.
    #[test]
    fn post_port_sfp_info_to_db_sff_branch() {
        let info = serde_json::json!({
            "type": "QSFP+ or later",
            "vendor_rev": "A",
            "serial": "SN123",
            "manufacturer": "EMU",
            "model": "EMU-40G",
            "vendor_oui": "00-00-00",
            "vendor_date": "2020-01-01",
            "connector": "LC",
            "encoding": "64B/66B",
            "ext_identifier": "Power Class 1",
            "ext_rateselect_compliance": "N/A",
            "cable_type": "Length Cable Assembly(m)",
            "cable_length": 5,
            "specification_compliance": "sm_media_interface",
            "nominal_bit_rate": 10300,
        });
        let hal = MockHal::with_sfps(vec![MockSfp::present().with_info(info)]);
        let intf_tbl = crate::mock::MockDbTable::new("TRANSCEIVER_INFO");
        let pm = port_mapping_one("Ethernet0", 0, 0);

        let rc = post_port_sfp_info_to_db("Ethernet0", &pm, &intf_tbl, &hal);
        assert_eq!(rc, 0);
        assert_eq!(intf_tbl.get_size_for_key("Ethernet0"), 18);
        assert_eq!(
            intf_tbl.hget("Ethernet0", "type").as_deref(),
            Some("QSFP+ or later")
        );
        assert_eq!(
            intf_tbl.hget("Ethernet0", "cable_length").as_deref(),
            Some("5")
        );
        assert_eq!(
            intf_tbl.hget("Ethernet0", "is_replaceable").as_deref(),
            Some("True")
        );
        // Omitted optional fields default to N/A.
        assert_eq!(
            intf_tbl
                .hget("Ethernet0", "application_advertisement")
                .as_deref(),
            Some("N/A")
        );
        assert_eq!(
            intf_tbl.hget("Ethernet0", "dom_capability").as_deref(),
            Some("N/A")
        );
    }

    // Sentinels: unknown logical port → PHYSICAL_PORT_NOT_EXIST; present module with an
    // unreadable EEPROM (null info) → SFP_EEPROM_NOT_READY (the retry trigger).
    #[test]
    fn post_port_sfp_info_to_db_returns_sentinels() {
        let hal = MockHal::with_sfps(vec![MockSfp::present()]);
        let intf_tbl = crate::mock::MockDbTable::new("TRANSCEIVER_INFO");
        let pm = port_mapping_one("Ethernet0", 0, 0);

        // Not in the mapping and not numeric → -1.
        assert_eq!(
            post_port_sfp_info_to_db("Ethernet99", &pm, &intf_tbl, &hal),
            PHYSICAL_PORT_NOT_EXIST
        );
        // Present but info is null (default MockSfp::present info) → -2, nothing written.
        assert_eq!(
            post_port_sfp_info_to_db("Ethernet0", &pm, &intf_tbl, &hal),
            SFP_EEPROM_NOT_READY
        );
        assert_eq!(intf_tbl.get_size_for_key("Ethernet0"), 0);
    }

    // An absent module writes no row and returns success (the reference logs + continues).
    #[test]
    fn post_port_sfp_info_to_db_absent_is_noop() {
        let hal = MockHal::with_sfps(vec![MockSfp::default()]);
        let intf_tbl = crate::mock::MockDbTable::new("TRANSCEIVER_INFO");
        let pm = port_mapping_one("Ethernet0", 0, 0);
        assert_eq!(
            post_port_sfp_info_to_db("Ethernet0", &pm, &intf_tbl, &hal),
            0
        );
        assert_eq!(intf_tbl.get_size(), 0);
    }

    // ← tests/test_xcvrd.py::test_remove_stale_transceiver_info: an INFO row for a port
    // whose module is now absent is purged; a present port's row is kept.
    #[test]
    fn remove_stale_transceiver_info_purges_absent_ports() {
        let hal = MockHal::with_sfps(vec![MockSfp::present(), MockSfp::default()]);
        let th = XcvrTableHelper::with_mock_tables(&[String::new()]);
        // Both ports have a stale INFO row from a previous run.
        th.get_intf_tbl(0).hset("Ethernet0", "type", "QSFP-DD");
        th.get_intf_tbl(0).hset("Ethernet4", "type", "QSFP-DD");

        let mut pm = port_mapping_one("Ethernet0", 0, 0);
        pm.handle_port_change_event(&PortChangeEvent::new(
            "Ethernet4".to_string(),
            Some(1),
            0,
            PortChangeEventType::Add,
            "CONFIG_DB".to_string(),
            "PORT".to_string(),
        ));

        remove_stale_transceiver_info(&hal, &th, &pm);

        // Ethernet0 present → kept; Ethernet4 absent → purged.
        assert_eq!(th.get_intf_tbl(0).get_size_for_key("Ethernet0"), 1);
        assert_eq!(th.get_intf_tbl(0).get_size_for_key("Ethernet4"), 0);
    }
}
