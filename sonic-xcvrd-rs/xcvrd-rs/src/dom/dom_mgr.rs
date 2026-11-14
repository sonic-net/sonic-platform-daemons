//! `dom/dom_mgr.py` → `DomInfoUpdateTask` (+ `DomThermalInfoUpdateTask`) — the periodic
//! DOM poll thread (analysis §1.3, §3.2).
//!
//! The `DomInfoUpdateTask` loop: on each pass it walks the physical→logical
//! map and, for every present, polling-enabled, non-error port, republishes
//! `TRANSCEIVER_DOM_SENSOR` (temperature, voltage, the 24 per-lane tx/rx power + tx
//! bias keys — unit-stripped, with a trailing `last_update_time`) and
//! `TRANSCEIVER_DOM_FLAG` (+ its change-count / set-time / clear-time metadata). It also
//! layers the rich `TRANSCEIVER_STATUS` poster (`get_transceiver_status()` — module
//! state/fault + per-host-lane datapath/config/tx-rx) onto the same pass, plus the
//! latched `TRANSCEIVER_STATUS_FLAG` poster (+ its metadata siblings) and an APPL_DB
//! `PORT_TABLE` link-change watch: a `flap_count` flap schedules an off-cadence re-read
//! of the DOM + status flag tables (`update_port_db_diagnostics_on_link_change`) so the
//! latched-flag snapshot reflects the module's post-flap state without waiting for the
//! next periodic pass. The `dom_polling=disabled` CONFIG_DB gate and the CMIS-init gate
//! mirror the Python `is_port_dom_monitoring_disabled`. The pass also layers
//! firmware/VDM/PM posting, and a CONFIG_DB `PORT` watch
//! (`subscribe_port_config_change`) so a logical port added/removed at runtime is
//! synced into / out of the DOM poll mapping via `on_port_config_change` →
//! `PortMapping::handle_port_change_event` (a runtime remove also tears down the port's
//! DOM-owned `TRANSCEIVER_*` rows, `on_remove_logical_port`).
//!
//! The `TRANSCEIVER_DOM_THRESHOLD` table is *not* refreshed on this loop — like the
//! Python daemon it is seeded once at boot / at insert by `SfpStateUpdateTask`.
#![allow(dead_code, unused_variables, unused_imports)]

use std::collections::{BTreeMap, HashMap};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};

use crate::db::DbTable;
use crate::dom::utilities::db::{value_to_py_str, DbCache, DbUtils, Fvs};
use crate::dom::utilities::dom_sensor::DomDbUtils;
use crate::dom::utilities::status::StatusDbUtils;
use crate::dom::utilities::vdm::{VdmDbUtils, VdmUtils};
use crate::hal::Hal;
use crate::xcvrd_utilities::common::{
    del_port_sfp_dom_info_from_db, get_cmis_state_from_state_db, get_physical_port_name_dict,
    CMIS_STATE_REMOVED, CMIS_TERMINAL_STATES,
};
use crate::xcvrd_utilities::port_event_helper::{
    read_port_config_change, MultiPortChangeObserver, PortChangeEvent, PortChangeEventType,
    PortConfigChangeSubscriber, PortMapping, SELECT_TIMEOUT_MSECS,
};
use crate::xcvrd_utilities::sfp_status_helper::detect_port_in_error_status;
use crate::xcvrd_utilities::utils::{
    get_transceiver_presence, is_transceiver_flat_memory, is_transceiver_lpmode_on,
};
use crate::xcvrd_utilities::xcvr_table_helper::{XcvrTableHelper, VDM_THRESHOLD_TYPES};

/// `DEFAULT_DOM_INFO_UPDATE_PERIOD_SECS` (dom_mgr.py).
pub const DEFAULT_DOM_INFO_UPDATE_PERIOD_SECS: u64 = 60;

/// `DIAG_DB_UPDATE_TIME_AFTER_LINK_CHANGE` (dom_mgr.py) — the grace delay after an
/// APPL_DB link-change flap before the diagnostic-flag tables are re-captured, so the
/// module has time to update its real-time latched-flag status first.
pub const DIAG_DB_UPDATE_TIME_AFTER_LINK_CHANGE_SECS: u64 = 1;

/// `PORT_UPDATE_EVENT_SELECT_TIMEOUT_MSECS` (dom_mgr.py) — the cap on the APPL_DB
/// port-update select wait so the DOM loop stays responsive to link-change flaps
/// between periodic passes.
pub const PORT_UPDATE_EVENT_SELECT_TIMEOUT_MSECS: u64 = 1000;

/// `PORT_UPDATE_EVENT_SELECT_TIMEOUT_FAST_MSECS` (dom_mgr.py) — the short select wait
/// used when link-change is serviced *inside* the periodic DOM poll pass (once per port,
/// dom_mgr.py:326). Keeping the per-port service quick means a multi-second poll pass
/// still re-reads a mid-pass flap within ~1 s rather than deferring it to the next pass.
pub const PORT_UPDATE_EVENT_SELECT_TIMEOUT_FAST_MSECS: u64 = 10;

/// `get_dom_polling_from_config_db(lport)` — the CONFIG_DB `PORT.dom_polling` field
/// for `lport`'s breakout group (read off the group's *first* subport), defaulting to
/// `"enabled"` when absent. Shared by both DOM tasks (a free fn so the fast-temperature
/// task can reuse it without the CMIS-init gate).
pub fn get_dom_polling_from_config_db(
    port_mapping: &PortMapping,
    table_helper: &XcvrTableHelper,
    lport: &str,
) -> String {
    let default = "enabled".to_string();

    let Some(pport_list) = port_mapping.get_logical_to_physical(lport) else {
        return default;
    };
    let Some(pport) = pport_list.first().copied() else {
        return default;
    };
    let Some(logical_port_list) = port_mapping.get_physical_to_logical(pport) else {
        return default;
    };
    // The first logical port corresponds to the first subport of the breakout group.
    let Some(first_logical_port) = logical_port_list.first() else {
        return default;
    };
    let Some(asic_index) = port_mapping.get_asic_id_for_logical_port(first_logical_port) else {
        return default;
    };
    let port_tbl = table_helper.get_cfg_port_tbl(asic_index);
    if let Some(row) = port_tbl.get(first_logical_port) {
        if let Some((_, v)) = row.iter().find(|(k, _)| k == "dom_polling") {
            return v.clone();
        }
    }
    default
}

/// `{**basic, **statistic}` — merge the basic and statistic VDM real-value dicts, with
/// the statistic values overriding the basic ones on a key clash (dict-unpack order).
/// Non-object inputs contribute nothing.
fn merge_vdm_values(basic: &Value, statistic: &Value) -> Value {
    let mut merged = Map::new();
    if let Value::Object(b) = basic {
        for (k, v) in b {
            merged.insert(k.clone(), v.clone());
        }
    }
    if let Value::Object(s) = statistic {
        for (k, v) in s {
            merged.insert(k.clone(), v.clone());
        }
    }
    Value::Object(merged)
}

/// The `TRANSCEIVER_*` table set the DOM task tears down when a logical port is removed
/// from CONFIG_DB — `DomInfoUpdateTask.on_remove_logical_port` (dom_mgr.py:495-524).
///
/// This is the subset of transceiver tables the DOM task itself *writes*: the DOM sensor
/// row + its temperature/flag/metadata siblings, the VDM real-value + per-threshold-type
/// flag/metadata rows, the `TRANSCEIVER_STATUS` HW section + status-flag/metadata rows,
/// and the PM + firmware-info rows. Deliberately excludes `TRANSCEIVER_INFO`,
/// `TRANSCEIVER_DOM_THRESHOLD`, the VDM *threshold-value* tables (all owned by
/// `SfpStateUpdateTask`) and `TRANSCEIVER_STATUS_SW` — the reference comment notes those
/// are managed elsewhere, so the DOM removal must not touch them (avoids a race that would
/// wipe rows another task owns).
fn dom_logical_port_removal_tables(th: &XcvrTableHelper, asic: usize) -> Vec<&dyn DbTable> {
    let mut tables: Vec<&dyn DbTable> = vec![
        th.get_dom_tbl(asic),
        th.get_dom_temperature_tbl(asic),
        th.get_dom_flag_tbl(asic),
        th.get_dom_flag_change_count_tbl(asic),
        th.get_dom_flag_set_time_tbl(asic),
        th.get_dom_flag_clear_time_tbl(asic),
        th.get_vdm_real_value_tbl(asic),
    ];
    for t in VDM_THRESHOLD_TYPES {
        tables.push(th.get_vdm_flag_tbl(asic, t));
    }
    for t in VDM_THRESHOLD_TYPES {
        tables.push(th.get_vdm_flag_change_count_tbl(asic, t));
    }
    for t in VDM_THRESHOLD_TYPES {
        tables.push(th.get_vdm_flag_set_time_tbl(asic, t));
    }
    for t in VDM_THRESHOLD_TYPES {
        tables.push(th.get_vdm_flag_clear_time_tbl(asic, t));
    }
    tables.push(th.get_status_tbl(asic));
    tables.push(th.get_status_flag_tbl(asic));
    tables.push(th.get_status_flag_change_count_tbl(asic));
    tables.push(th.get_status_flag_set_time_tbl(asic));
    tables.push(th.get_status_flag_clear_time_tbl(asic));
    tables.push(th.get_pm_tbl(asic));
    tables.push(th.get_firmware_info_tbl(asic));
    tables
}

/// A pending off-cadence link-change flag re-read. `fire_at` is when the debounced re-read
/// next runs; it is consumed once it publishes (or the port is *permanently* ineligible),
/// mirroring the reference's unconditional `del self.link_change_affected_ports[lport]`
/// (dom_mgr.py:282). But because this daemon detects flaps notification-independently (see
/// [`LinkChangeReread::DeferredCmisInit`]), a re-read can fire while a just-plugged port is
/// only *transiently* ineligible; such an attempt publishes nothing and is **re-armed**
/// rather than dropped, so the flap's flag re-capture is not lost.
///
/// `giveup_at` bounds how long a flap whose re-read keeps hitting a *transient* ineligibility
/// — the just-plugged port is still mid-CMIS-init (`DeferredCmisInit`), or its module DOM read
/// momentarily yields nothing (`TransientRead`) — stays re-armed before it is dropped and the
/// periodic DOM pass takes over. Bounding the retry to one `dom_update_interval` keeps a port
/// stuck transient forever from leaking a pending entry.
#[derive(Debug, Clone, Copy)]
struct PendingReread {
    fire_at: Instant,
    giveup_at: Instant,
}

/// Outcome of an off-cadence link-change flag re-read
/// ([`DomInfoUpdateTask::update_port_db_diagnostics_on_link_change`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LinkChangeReread {
    /// The flag tables were re-captured, OR the port is *permanently* ineligible for this
    /// flap (stop event, unknown/absent port, blocking error status, invalid asic, or a
    /// `dom_polling=disabled` operator gate). The pending re-read is consumed and dropped —
    /// exactly the reference's unconditional `del self.link_change_affected_ports[...]`
    /// (dom_mgr.py:282).
    Settled,
    /// The port is only *transiently* ineligible: DOM polling is enabled but its
    /// `cmis_state` is still non-terminal (mid-CMIS datapath bring-up), so publishing now
    /// would violate the DOM-gating contract (`test_dom_gating`). Nothing was published
    /// this attempt. Unlike the reference — whose keyspace observer only *delivers* a flap
    /// event once the module has already settled, so its single fire-once re-read
    /// (dom_mgr.py:282) always lands on a terminal datapath — this daemon *also* detects
    /// flaps notification-independently by reconciling APPL_DB `flap_count` on a ~1 s
    /// cadence ([`DomInfoUpdateTask::reconcile_link_change_flap_counts`]), which can fire a
    /// re-read while a just-plugged port is still mid-CMIS-init. Dropping the re-read after
    /// that one premature attempt would silently lose the flap's flag re-capture whenever
    /// the `TRANSCEIVER_DOM_FLAG` row already exists (so the republish hook, which only
    /// re-establishes a *missing* row, cannot cover it) — exactly the
    /// `test_link_change_triggers_fast_flag_recapture` failure. So this attempt re-arms the
    /// pending re-read (bounded by `PendingReread::giveup_at`) instead: it retries on the
    /// ~1 s cadence and publishes the latched state the moment the datapath reaches a
    /// terminal state, well inside the e2e fast window. The retry cannot linger past a
    /// caller's post-baseline guard window because the re-read is consumed the instant it
    /// [`Self::Settled`]s (publishes), and the republish hook likewise consumes it once the
    /// resting baseline lands — so by the time a caller raises a fresh alarm (after the
    /// baseline it waited on), no pending re-read remains to surface it without a new flap.
    DeferredCmisInit,
    /// The re-read ran with all gates open but the module's DOM-flag read transiently yielded
    /// nothing (`get_transceiver_dom_flags` returned `None`/`{}` — a gRPC/EEPROM hiccup on this
    /// emulator testbed), so no `TRANSCEIVER_DOM_FLAG` row was published this attempt. As with
    /// [`Self::DeferredCmisInit`], the flap re-read is re-armed (bounded by
    /// `PendingReread::giveup_at`) rather than dropped, so a genuine flap's flag re-capture is
    /// retried on the ~1 s cadence until the module answers — never lost to a single
    /// transiently-empty read when the `TRANSCEIVER_DOM_FLAG` row already exists.
    TransientRead,
}

/// `DomInfoUpdateTask` — the periodic DOM poll thread. It posts the DOM sensor + flag
/// rows, the rich `TRANSCEIVER_STATUS` row, and the `TRANSCEIVER_STATUS_FLAG`
/// row (+ metadata) and the APPL_DB link-change flag re-capture, plus Firmware/VDM/PM
/// posting layered onto the same pass.
pub struct DomInfoUpdateTask {
    port_mapping: PortMapping,
    hal: Arc<dyn Hal>,
    table_helper: Arc<XcvrTableHelper>,
    /// `skip_cmis_mgr` — when no CMIS manager runs, the CMIS-init gate is a no-op.
    skip_cmis_mgr: bool,
    dom_update_interval: u64,
    dom_db: DomDbUtils,
    status_db: StatusDbUtils,
    /// `link_change_affected_ports` (dom_mgr.py) — physical port → the pending off-cadence
    /// diagnostic-flag re-read scheduled after an APPL_DB link-change flap (see
    /// [`PendingReread`]). Interior-mutable so the off-cadence link-change handler runs
    /// behind `&self` (the periodic `poll_once` pass takes `&self` too). Also consolidates
    /// the flaps of every breakout subport of a physical port into a single pending re-read.
    link_change_affected_ports: Mutex<HashMap<usize, PendingReread>>,
    /// Last APPL_DB `PORT_TABLE` `flap_count` seen per physical port. A link-change
    /// re-read is scheduled only when a delivered event carries a `flap_count` that
    /// actually **changed** for that port — mirroring the reference's FILTER
    /// `['flap_count']` subscriber + `PortChangeEvent` soak/dedup (dom_mgr.py:147,
    /// port_event_helper.py:178), whose *net* effect is to surface an event only on a
    /// genuine link flap. This keeps a re-delivered snapshot (or an unrelated
    /// `PORT_TABLE` write that still carries the current `flap_count`) from triggering an
    /// off-cadence flag re-capture that would publish a freshly-raised latched flag
    /// outside the poll / flap cadence.
    last_flap_count: Mutex<HashMap<usize, String>>,
    /// `vdm_utils` (dom_mgr.py) — the VDM HAL helper (freeze/capture/unfreeze +
    /// supported/statistic-supported probes). `vdm_db` posts the VDM real-value /
    /// threshold / flag tables. Both stateless, cloned into the poll pass.
    vdm_utils: VdmUtils,
    vdm_db: VdmDbUtils,
}

impl DomInfoUpdateTask {
    pub fn new(
        port_mapping: PortMapping,
        hal: Arc<dyn Hal>,
        table_helper: Arc<XcvrTableHelper>,
        skip_cmis_mgr: bool,
        dom_update_interval: Option<i64>,
    ) -> Self {
        // Mirror the Python ctor: an absent interval falls back to the 60 s default; a
        // NEGATIVE interval is invalid, so warn and use the default; any non-negative
        // value (including 0) is honored verbatim.
        let dom_update_interval = match dom_update_interval {
            None => DEFAULT_DOM_INFO_UPDATE_PERIOD_SECS,
            Some(secs) if secs < 0 => {
                eprintln!(
                    "xcvrd-rs: invalid dom_update_interval {secs} provided; using default \
                     {DEFAULT_DOM_INFO_UPDATE_PERIOD_SECS} seconds instead"
                );
                DEFAULT_DOM_INFO_UPDATE_PERIOD_SECS
            }
            Some(secs) => secs as u64,
        };
        DomInfoUpdateTask {
            port_mapping,
            hal,
            table_helper,
            skip_cmis_mgr,
            dom_update_interval,
            dom_db: DomDbUtils::new(),
            status_db: StatusDbUtils::new(),
            link_change_affected_ports: Mutex::new(HashMap::new()),
            last_flap_count: Mutex::new(HashMap::new()),
            vdm_utils: VdmUtils::new(),
            vdm_db: VdmDbUtils::new(),
        }
    }

    /// `get_dom_polling_from_config_db(lport)`.
    pub fn get_dom_polling_from_config_db(&self, lport: &str) -> String {
        get_dom_polling_from_config_db(&self.port_mapping, &self.table_helper, lport)
    }

    /// `is_port_in_cmis_initialization_process(lport)` — a port whose STATUS_SW
    /// `cmis_state` is not one of the CMIS *terminal* states is still bringing up its
    /// datapath, so DOM polling is deferred. `skip_cmis_mgr` short-circuits to `false`
    /// (the daemon runs no CMIS manager; the module is always treated ready).
    pub fn is_port_in_cmis_initialization_process(&self, logical_port_name: &str) -> bool {
        if self.skip_cmis_mgr {
            return false;
        }
        let Some(asic_index) = self
            .port_mapping
            .get_asic_id_for_logical_port(logical_port_name)
        else {
            return false;
        };
        let status_sw = self.table_helper.get_status_sw_tbl(asic_index);
        let cmis_state = get_cmis_state_from_state_db(logical_port_name, status_sw);
        !CMIS_TERMINAL_STATES.contains(&cmis_state.as_str())
    }

    /// `is_port_dom_monitoring_disabled(lport)` — CONFIG_DB `dom_polling=disabled`
    /// **or** the port is mid-CMIS-init.
    pub fn is_port_dom_monitoring_disabled(&self, logical_port_name: &str) -> bool {
        self.get_dom_polling_from_config_db(logical_port_name) == "disabled"
            || self.is_port_in_cmis_initialization_process(logical_port_name)
    }

    /// `post_port_sfp_firmware_info_to_db(logical_port_name, port_mapping, table, stop,
    /// firmware_info_cache)` (dom_mgr.py:203) — publish `TRANSCEIVER_FIRMWARE_INFO`
    /// (active/inactive firmware versions). The firmware row is posted to **every**
    /// logical port backing the physical port (firmware is a per-module property, so all
    /// breakout subports share it), carries **no** beautify and **no** `last_update_time`,
    /// and an empty/`None` read aborts the remaining ports (EEPROM not ready). A
    /// `NotImplementedError` over the bridge folds to an empty dict, exactly like
    /// `common._wrapper_get_transceiver_firmware_info`.
    pub fn post_port_sfp_firmware_info_to_db(
        &self,
        stop: &AtomicBool,
        logical_port_name: &str,
        table: &dyn DbTable,
        mut firmware_info_cache: Option<&mut DbCache>,
    ) {
        for (physical_port, _physical_port_name) in
            get_physical_port_name_dict(logical_port_name, &self.port_mapping)
        {
            if stop.load(Ordering::Relaxed) {
                break;
            }
            let Ok(sfp) = self.hal.sfp(physical_port) else {
                continue;
            };
            if !get_transceiver_presence(&*sfp) {
                continue;
            }
            let fw_dict: Value = match firmware_info_cache
                .as_mut()
                .and_then(|c| c.get(&physical_port).cloned())
            {
                Some(cached) => cached.unwrap_or(Value::Null),
                None => {
                    let read = match sfp.call_json("get_transceiver_info_firmware_versions") {
                        Ok(v) => v,
                        Err(_) => Value::Object(Map::new()),
                    };
                    if let Some(c) = firmware_info_cache.as_mut() {
                        c.insert(physical_port, Some(read.clone()));
                    }
                    read
                }
            };
            match fw_dict {
                Value::Object(obj) if !obj.is_empty() => {
                    let fvs: Fvs = obj
                        .iter()
                        .map(|(k, v)| (k.clone(), value_to_py_str(v)))
                        .collect();
                    // Firmware applies to the whole module — update every logical subport.
                    let Some(logical_port_list) =
                        self.port_mapping.get_physical_to_logical(physical_port)
                    else {
                        // Unknown physical port index — warn + skip (dom_mgr.py:225-227).
                        continue;
                    };
                    for logical_port in logical_port_list {
                        table.set(&logical_port, &fvs);
                    }
                }
                // Empty or None read → EEPROM not ready; stop this pass (SFP_EEPROM_NOT_READY).
                _ => return,
            }
        }
    }

    /// `post_port_pm_info_to_db(logical_port_name, port_mapping, table, stop,
    /// pm_info_cache)` (dom_mgr.py:238) — publish `TRANSCEIVER_PM` for a coherent (paged)
    /// module. Flat-memory modules are skipped (the PM page is CMIS-coherent-only); an
    /// absent module is skipped; a `None` read (EEPROM not ready) aborts the remaining
    /// ports; an empty dict just skips the port (`get_transceiver_pm` N/A). Unlike the
    /// diagnostic posters this row carries **no** `last_update_time` and is keyed by the
    /// *physical* port display name.
    pub fn post_port_pm_info_to_db(
        &self,
        stop: &AtomicBool,
        logical_port_name: &str,
        table: &dyn DbTable,
        mut pm_info_cache: Option<&mut DbCache>,
    ) {
        for (physical_port, physical_port_name) in
            get_physical_port_name_dict(logical_port_name, &self.port_mapping)
        {
            if stop.load(Ordering::Relaxed) {
                break;
            }
            let Ok(sfp) = self.hal.sfp(physical_port) else {
                continue;
            };
            if !get_transceiver_presence(&*sfp) {
                continue;
            }
            // Flat-memory modules have no PM page — skip (dom_mgr.py:246, only `== True`).
            if is_transceiver_flat_memory(&*sfp) {
                continue;
            }
            let pm_info_dict: Value = match pm_info_cache
                .as_mut()
                .and_then(|c| c.get(&physical_port).cloned())
            {
                Some(cached) => cached.unwrap_or(Value::Null),
                None => {
                    let read = match sfp.call_json("get_transceiver_pm") {
                        Ok(v) => v,
                        Err(_) => Value::Object(Map::new()),
                    };
                    if let Some(c) = pm_info_cache.as_mut() {
                        c.insert(physical_port, Some(read.clone()));
                    }
                    read
                }
            };
            match pm_info_dict {
                // A genuine `None` read means the EEPROM is not ready — stop this pass.
                Value::Null => return,
                Value::Object(obj) => {
                    // Empty means `get_transceiver_pm` is N/A for this xcvr — skip.
                    if obj.is_empty() {
                        continue;
                    }
                    let mut obj = obj;
                    DbUtils::new().beautify_info_dict(&mut obj);
                    let fvs: Fvs = obj
                        .iter()
                        .map(|(k, v)| (k.clone(), value_to_py_str(v)))
                        .collect();
                    table.set(&physical_port_name, &fvs);
                }
                _ => continue,
            }
        }
    }

    /// One DOM poll pass: for every present, polling-enabled, non-error port publish
    /// `TRANSCEIVER_DOM_SENSOR`, `TRANSCEIVER_DOM_FLAG` (+ metadata), the rich
    /// `TRANSCEIVER_STATUS` row, then the latched `TRANSCEIVER_STATUS_FLAG` row (+
    /// metadata), plus firmware/VDM/PM posting. Mirrors the per-port body of
    /// `DomInfoUpdateTask.task_worker`. Never propagates a
    /// per-port error — a transient read failure just skips that port, keeping the loop
    /// resilient.
    pub fn poll_once(&self, stop: &AtomicBool) {
        for (physical_port, logical_ports) in self.port_mapping.iter_physical_to_logical() {
            if stop.load(Ordering::Relaxed) {
                break;
            }
            self.poll_port(stop, physical_port, logical_ports);
        }
    }

    /// The `poll_once` variant driven by [`Self::task_worker`]: it services pending APPL_DB
    /// `PORT_TABLE` link-change flaps **between every port** of the pass, mirroring
    /// dom_mgr.py:326 (which calls `check_port_update` at the top of each poll-loop
    /// iteration). A full DOM poll walks every port's EEPROM and can run for many seconds on
    /// this emulator testbed; without this interleave a `flap_count` bump that lands mid-pass
    /// would not be re-read until the whole pass finished — past the e2e fast window
    /// (`test_link_change_flags::test_link_change_triggers_fast_flag_recapture`, which asserts
    /// the re-read lands well under the ~60 s poll). The notification-independent `flap_count`
    /// reconcile stays on its ~1 s cadence via the shared `next_reconcile` deadline (so it is
    /// not re-scanned for every port), while any *due* re-read is drained each port. The plain
    /// [`Self::poll_once`] (no observer / reconcile deadline) is kept for the unit tests.
    fn poll_once_interleaved(
        &self,
        stop: &AtomicBool,
        mut observer: Option<&mut MultiPortChangeObserver>,
        next_reconcile: &mut Instant,
    ) {
        for (physical_port, logical_ports) in self.port_mapping.iter_physical_to_logical() {
            if stop.load(Ordering::Relaxed) {
                break;
            }
            // dom_mgr.py:326 — service link-change before each port so a flap during the
            // (multi-second) poll pass is re-read within ~1 s, not deferred to the next
            // periodic pass. Reconcile only fires on its ~1 s deadline (shared with
            // `task_worker`); `check_port_update` drains any due re-read every port.
            let now = Instant::now();
            if now >= *next_reconcile {
                self.reconcile_link_change_flap_counts();
                self.republish_missing_flag_baseline_after_cmis_bringup(stop);
                *next_reconcile =
                    now + Duration::from_secs(DIAG_DB_UPDATE_TIME_AFTER_LINK_CHANGE_SECS);
            }
            self.check_port_update(
                stop,
                observer.as_deref_mut(),
                PORT_UPDATE_EVENT_SELECT_TIMEOUT_FAST_MSECS,
            );

            if stop.load(Ordering::Relaxed) {
                break;
            }
            self.poll_port(stop, physical_port, logical_ports);
        }
    }

    /// One port's DOM poll body — the per-port block of `DomInfoUpdateTask.task_worker`
    /// (dom_mgr.py:325-417): publish `TRANSCEIVER_FIRMWARE_INFO`, `TRANSCEIVER_DOM_SENSOR`,
    /// `TRANSCEIVER_DOM_FLAG` (+ metadata), the rich `TRANSCEIVER_STATUS` row, the latched
    /// `TRANSCEIVER_STATUS_FLAG` row (+ metadata), then the VDM tables — gated by
    /// `dom_polling` / CMIS-init, error status and presence. A transient per-port read
    /// failure just skips the port (early `return`), keeping the pass resilient.
    fn poll_port(&self, stop: &AtomicBool, physical_port: usize, logical_ports: &[String]) {
        // The first logical port corresponds to the first subport of the breakout.
        let Some(logical_port_name) = logical_ports.first() else {
            return;
        };

        if self.is_port_dom_monitoring_disabled(logical_port_name) {
            return;
        }

        let Some(asic_index) = self
            .port_mapping
            .get_asic_id_for_logical_port(logical_port_name)
        else {
            return;
        };

        // A port stuck in a blocking error state is skipped entirely (no posting).
        if detect_port_in_error_status(
            logical_port_name,
            self.table_helper.get_status_sw_tbl(asic_index),
        ) {
            return;
        }

        // Poll module presence over the HAL (mirrors `_wrapper_get_presence`); an
        // absent module is skipped before any EEPROM read.
        let present = self
            .hal
            .sfp(physical_port)
            .ok()
            .and_then(|s| s.get_presence().ok())
            .unwrap_or(false);
        if !present {
            return;
        }

        // Publish TRANSCEIVER_FIRMWARE_INFO first (mirrors task_worker order).
        self.post_port_sfp_firmware_info_to_db(
            stop,
            logical_port_name,
            self.table_helper.get_firmware_info_tbl(asic_index),
            None,
        );

        self.dom_db.post_port_dom_sensor_info_to_db(
            stop,
            logical_port_name,
            &self.port_mapping,
            self.table_helper.get_dom_tbl(asic_index),
            self.hal.as_ref(),
            None,
        );

        // Publish the latched TRANSCEIVER_DOM_FLAG row + its change-count / set-time /
        // clear-time metadata off `get_transceiver_dom_flags()` on EVERY periodic pass,
        // exactly like the reference `DomInfoUpdateTask.task_worker` (dom_mgr.py:361-364),
        // which re-reads the flag tables unconditionally each poll. The off-cadence
        // link-change re-read (`update_port_db_diagnostics_on_link_change`) is an ADDITIONAL
        // fast path (dom_mgr.py:440-493), never a replacement — so a byte-9 temp/vcc change
        // always settles back to its STATE_DB baseline within one DOM cadence.
        self.dom_db.post_port_dom_flags_to_db(
            stop,
            logical_port_name,
            &self.port_mapping,
            self.hal.as_ref(),
            self.table_helper.get_dom_flag_tbl(asic_index),
            self.table_helper.get_dom_flag_change_count_tbl(asic_index),
            self.table_helper.get_dom_flag_set_time_tbl(asic_index),
            self.table_helper.get_dom_flag_clear_time_tbl(asic_index),
            None,
        );

        // Publish the rich TRANSCEIVER_STATUS row (module state/fault +
        // per-host-lane datapath/config/tx/rx) read off `get_transceiver_status()`.
        self.status_db.post_port_transceiver_hw_status_to_db(
            stop,
            logical_port_name,
            &self.port_mapping,
            self.table_helper.get_status_tbl(asic_index),
            self.hal.as_ref(),
            None,
        );

        // Publish the latched TRANSCEIVER_STATUS_FLAG row + its change-count /
        // set-time / clear-time metadata off `get_transceiver_status_flags()`, every poll
        // (dom_mgr.py:373-377).
        self.status_db.post_port_transceiver_hw_status_flags_to_db(
            stop,
            logical_port_name,
            &self.port_mapping,
            self.table_helper.get_status_flag_tbl(asic_index),
            self.table_helper
                .get_status_flag_change_count_tbl(asic_index),
            self.table_helper.get_status_flag_set_time_tbl(asic_index),
            self.table_helper.get_status_flag_clear_time_tbl(asic_index),
            self.hal.as_ref(),
            None,
        );

        // VDM freeze→capture→unfreeze + real-value / flag posting (COR flags last).
        self.post_port_vdm_diagnostics(stop, logical_port_name, physical_port, asic_index);
    }

    /// The VDM per-port body of `task_worker` (dom_mgr.py:381-417). If the module supports
    /// VDM: (a) when *statistic* observables are supported **and the module is not in
    /// low-power mode**, freeze VDM, capture the statistic real values + `TRANSCEIVER_PM`,
    /// then unfreeze (RAII guard); (b) capture the *basic* real values, merge
    /// `{**basic, **statistic}` (statistic wins on conflict) and post
    /// `TRANSCEIVER_VDM_REAL_VALUE`; (c) post the COR `TRANSCEIVER_VDM_*_FLAG` tables
    /// **last** so the latched snapshot is the freshest. Legacy PM-only APIs are polled
    /// independently when the VDM capability probe is absent.
    ///
    /// Low-power gate: the freeze (and thus the statistic capture + `TRANSCEIVER_PM`
    /// refresh) is gated on `not is_transceiver_lpmode_on` (dom_mgr.py:386-387). An
    /// activated coherent module put into low power (`ModuleLowPwr`) must stop having its PM
    /// refreshed — `test_dom_lpmode` deletes the PM row while in low power and asserts it is
    /// not republished. The `CmisManagerTask` drives an admin-up module out of low
    /// power to `ModuleReady`, so a normally-operating module reports `lpmode == false` and
    /// the statistic capture still runs (`test_pm` / `test_vdm_statistic` unaffected).
    ///
    fn post_port_vdm_diagnostics(
        &self,
        stop: &AtomicBool,
        logical_port_name: &str,
        physical_port: usize,
        asic_index: usize,
    ) {
        let Ok(sfp) = self.hal.sfp(physical_port) else {
            return;
        };
        if !self.vdm_utils.is_transceiver_vdm_supported(&*sfp) {
            // Older platforms publish PM independently of the newer VDM capability API.
            if matches!(sfp.has_method("is_transceiver_vdm_supported"), Ok(false))
                && matches!(sfp.has_method("get_transceiver_pm"), Ok(true))
            {
                self.post_port_pm_info_to_db(
                    stop,
                    logical_port_name,
                    self.table_helper.get_pm_tbl(asic_index),
                    None,
                );
            }
            return;
        }

        // Step (a): statistic freeze → capture statistic real values + PM → unfreeze.
        // `need_freeze` mirrors the reference gate `is_vdm_statistic_supported(...) and not
        // is_transceiver_lpmode_on(...)` (dom_mgr.py:386-387): a module in low power is not
        // frozen, so its statistic real values + `TRANSCEIVER_PM` stop refreshing until it
        // leaves low power (the coherent low-power gate — `test_dom_lpmode`).
        let mut vdm_statistic_values = Value::Object(Map::new());
        let need_freeze =
            self.vdm_utils.is_vdm_statistic_supported(&*sfp) && !is_transceiver_lpmode_on(&*sfp);
        if need_freeze {
            let guard = self.vdm_utils.vdm_freeze_context(&*sfp);
            if guard.is_frozen() {
                vdm_statistic_values = self
                    .vdm_utils
                    .get_vdm_real_values_statistic(&*sfp)
                    .unwrap_or_else(|| Value::Object(Map::new()));
                self.post_port_pm_info_to_db(
                    stop,
                    logical_port_name,
                    self.table_helper.get_pm_tbl(asic_index),
                    None,
                );
            } else {
                eprintln!("xcvrd-rs: Failed to freeze VDM stats for port {physical_port}");
            }
            // `guard` drops here → unfreeze (always attempted, even on capture failure).
        }

        // Step (b): capture basic real values, merge with statistic, post to DB.
        let vdm_basic_values = self
            .vdm_utils
            .get_vdm_real_values_basic(&*sfp)
            .unwrap_or_else(|| Value::Object(Map::new()));
        let vdm_merged_values = merge_vdm_values(&vdm_basic_values, &vdm_statistic_values);
        self.vdm_db.post_port_vdm_real_values_from_dict_to_db(
            stop,
            logical_port_name,
            &self.port_mapping,
            self.hal.as_ref(),
            self.table_helper.get_vdm_real_value_tbl(asic_index),
            &vdm_merged_values,
        );

        // Step (c): post the COR VDM flag tables last (freshest latched state), every poll
        // (dom_mgr.py:409-414). The off-cadence link-change re-read is additive, not a
        // replacement for the periodic re-read.
        self.vdm_db.post_port_vdm_flags_to_db(
            stop,
            logical_port_name,
            &self.port_mapping,
            self.hal.as_ref(),
            &self.table_helper,
            None,
        );
    }

    /// `on_port_update_event(port_change_event)` — record an APPL_DB link-change flap so
    /// its diagnostic-flag tables are re-captured off-cadence. Only a `PORT_SET` on
    /// `APPL_DB` schedules a re-read, and only when the event carries a `flap_count` that
    /// **changed** vs the last value seen for this physical port. The reference keeps
    /// `on_port_update_event` itself dumb (it schedules on any delivered `APPL_DB`
    /// `PORT_SET`) because the flap gating lives upstream in the observer — the
    /// `{APPL_DB: PORT_TABLE, FILTER: ['flap_count']}` subscription plus the
    /// `PortChangeEvent` soak/dedup only *deliver* an event when `flap_count` actually
    /// increments (dom_mgr.py:146-148, port_event_helper.py:175-184). We reproduce that
    /// *net* behaviour here so a re-delivered snapshot, or an unrelated `PORT_TABLE` write
    /// that still carries the current `flap_count`, cannot schedule an off-cadence re-read
    /// that would surface a freshly-raised latched flag before the module genuinely
    /// flapped. The re-read is queued `DIAG_DB_UPDATE_TIME_AFTER_LINK_CHANGE_SECS` in the
    /// future (giving the module time to update its latched flags); breakout subports
    /// collapse onto their shared physical port, so a group flap is a single pending
    /// re-read.
    pub fn on_port_update_event(&self, event: &PortChangeEvent) {
        let Some(physical_port) = self.record_flap_transition(event) else {
            return;
        };
        let now = Instant::now();
        let pending = PendingReread {
            fire_at: now + Duration::from_secs(DIAG_DB_UPDATE_TIME_AFTER_LINK_CHANGE_SECS),
            // Bound how long a flap whose re-read keeps hitting a transient ineligibility
            // (port mid-CMIS-init, or a momentarily-empty module read) stays re-armed: one
            // DOM update interval, after which the periodic pass republishes and the pending
            // entry is dropped.
            giveup_at: now + Duration::from_secs(self.dom_update_interval),
        };
        self.link_change_affected_ports
            .lock()
            .unwrap()
            .insert(physical_port, pending);
    }

    /// Seed the per-physical-port `flap_count` baseline from the observer's **initial
    /// snapshot** (see [`MultiPortChangeObserver::take_initial_snapshot`]) at task start,
    /// WITHOUT scheduling any re-read. The observer already folds the boot snapshot into
    /// its own dedup cache, so a re-delivered snapshot is normally suppressed one layer up;
    /// seeding here makes the daemon-level `last_flap_count` dedup *independent* of that
    /// priming, so a boot-snapshot event that ever reaches [`Self::on_port_update_event`]
    /// still cannot fire a spurious off-cadence flag re-capture. Only a genuine
    /// *post-boot* `flap_count` increment is then treated as a real flap.
    pub fn seed_link_change_baseline(&self, event: &PortChangeEvent) {
        self.seed_flap_baseline_if_absent(event);
    }

    /// Non-destructively record a port's current `flap_count` as its dedup baseline:
    /// insert it only when no baseline exists yet, NEVER overwriting one already present.
    ///
    /// This differs from [`Self::record_flap_transition`] (the *live* detection path,
    /// which must overwrite so each new flap is seen exactly once) because the DOM task
    /// is wrapped in a `catch_unwind` restart loop in `daemon.rs`: the
    /// [`DomInfoUpdateTask`] — and with it the `last_flap_count` map — persists across a
    /// task restart, yet [`Self::task_worker`] re-runs [`Self::seed_flap_count_baseline_from_db`]
    /// on every (re)entry. A *destructive* seed would re-baseline `last_flap_count` to
    /// the live APPL_DB `flap_count` on restart, silently swallowing a flap that landed
    /// after the last reconcile but before the restart — the next reconcile would then
    /// see no change and never fire the off-cadence flag re-capture. Seeding only when
    /// absent keeps any pre-restart baseline intact, so a genuine pending flap is still
    /// detected after a restart, while a cold start (empty map) seeds exactly as before.
    fn seed_flap_baseline_if_absent(&self, event: &PortChangeEvent) {
        if event.event_type != PortChangeEventType::Set || event.db_name != "APPL_DB" {
            return;
        }
        let Some(physical_port) = self.resolve_physical_for_event(event) else {
            return;
        };
        let Some(flap_count) = event.port_dict.get("flap_count") else {
            return;
        };
        self.last_flap_count
            .lock()
            .unwrap()
            .entry(physical_port)
            .or_insert_with(|| flap_count.clone());
    }

    /// Read APPL_DB `PORT_TABLE.flap_count` directly for every physical port (off its
    /// first breakout subport) and hand each value to `sink` as the synthetic APPL_DB
    /// `PORT_SET` event the observer would have delivered — the notification-independent
    /// source of link-change flaps.
    ///
    /// The reference watches `flap_count` via a keyspace `SubscriberStateTable`
    /// ([`Self::on_port_update_event`]). On this emulator testbed the daemon's single
    /// APPL_DB subscription can miss the keyspace wake, so the off-cadence flap re-read
    /// never fired; the CMIS manager tolerates the same fragility only because it *also*
    /// reconciles its trigger fields (e.g. `host_tx_ready`) straight from the DB. Mirroring
    /// that pattern, the DOM task reads `flap_count` from APPL_DB on its fast inter-poll
    /// cadence and routes each through the SAME [`Self::record_flap_transition`] dedup — so
    /// a genuine per-port change still schedules exactly one debounced re-read, while an
    /// unchanged `flap_count` (or a freshly raised module flag, which never bumps
    /// `flap_count`) is ignored, keeping the re-capture strictly off the 60 s sensor
    /// cadence.
    fn foreach_appl_flap_count(&self, mut sink: impl FnMut(&PortChangeEvent)) {
        for (physical_port, logical_ports) in self.port_mapping.iter_physical_to_logical() {
            // The first logical port is the first subport of the breakout group; the
            // reference reads the flap off that group representative too.
            let Some(first_logical) = logical_ports.first() else {
                continue;
            };
            let Some(asic_index) = self
                .port_mapping
                .get_asic_id_for_logical_port(first_logical)
            else {
                continue;
            };
            let Some(flap_count) = self
                .table_helper
                .get_app_port_tbl(asic_index)
                .hget(first_logical, "flap_count")
            else {
                continue;
            };
            let event = PortChangeEvent::new(
                first_logical.clone(),
                Some(physical_port),
                asic_index,
                PortChangeEventType::Set,
                "APPL_DB".to_string(),
                "PORT_TABLE".to_string(),
            )
            .with_port_dict(BTreeMap::from([("flap_count".to_string(), flap_count)]));
            sink(&event);
        }
    }

    /// Seed the per-physical-port `flap_count` baseline from a direct APPL_DB read at task
    /// start, WITHOUT scheduling any re-read. Complements
    /// [`Self::seed_link_change_baseline`] (which seeds off the observer's boot snapshot):
    /// the direct read is authoritative even when the observer is unavailable or its
    /// snapshot is empty, so the very first [`Self::reconcile_link_change_flap_counts`]
    /// pass does not mistake an already-present boot `flap_count` for a fresh flap and
    /// publish the latched flags off-cadence at start.
    fn seed_flap_count_baseline_from_db(&self) {
        self.foreach_appl_flap_count(|event| self.seed_link_change_baseline(event));
    }

    /// Reconcile the per-physical-port `flap_count` against APPL_DB on the fast inter-poll
    /// cadence, scheduling a debounced diagnostic-flag re-read for each port whose
    /// `flap_count` genuinely changed since the last read — the notification-independent
    /// twin of the observer's [`Self::on_port_update_event`] path. Both share the
    /// `last_flap_count` dedup, so whichever observes a given flap first records it and the
    /// other is a no-op (no double re-read).
    fn reconcile_link_change_flap_counts(&self) {
        self.foreach_appl_flap_count(|event| self.on_port_update_event(event));
    }

    /// Publish a port's latched-flag baseline PROMPTLY once its CMIS datapath bring-up
    /// completes, instead of waiting up to a full DOM poll cadence.
    ///
    /// The reference re-reads the flag tables off-cadence via
    /// [`Self::update_port_db_diagnostics_on_link_change`], triggered by an APPL_DB
    /// `PORT_TABLE` `flap_count` bump. On real hardware a (re)inserted module's datapath
    /// activation raises the host-side link, and the orchestration agent bumps `flap_count`
    /// — so that flap is exactly the event that re-reads the flags the moment the port
    /// becomes ready. This emulator testbed has no such agent, so a physical re-plug drives
    /// a full CMIS re-init WITHOUT any `flap_count` change: nothing schedules the fast-path
    /// re-read, and the deleted `TRANSCEIVER_DOM_FLAG` row (dropped by the unplug's
    /// `del_port_sfp_dom_info`) only reappears on the next periodic pass — up to a full
    /// `dom_update_interval` PLUS this port's position in the multi-second, 32-port pass
    /// after the datapath settled. That tips a late breakout port's flag baseline past the
    /// e2e's one-cadence budget (`test_dom_flag_groups_temp_and_vcc`, which re-plugs a
    /// high-index module then waits on the DOM_FLAG baseline reappearing within one cadence).
    ///
    /// We reproduce the reference's observable outcome — the flag tables re-read right after
    /// the datapath settles — by PUBLISHING the baseline INLINE, through the SAME
    /// [`Self::update_port_db_diagnostics_on_link_change`] re-capture a genuine flap uses
    /// (DOM_FLAG + STATUS_FLAG + VDM_FLAG), for any port that is DOM-enabled, physically
    /// present, has reached a settled terminal datapath, and whose `TRANSCEIVER_DOM_FLAG`
    /// row is currently **absent**. Publishing inline — rather than scheduling a debounced
    /// re-read drained later by [`Self::check_port_update`] — removes every intermediate
    /// latency/liveness dependency (debounce grace, drain cadence), so the deleted baseline
    /// reappears within one ~1 s hook cycle of the datapath settling, deterministically
    /// inside the e2e budget even for a late-index re-plug.
    ///
    /// The terminal gate is `READY` **or** `FAILED` — the same "the periodic poll would
    /// publish this port" condition (`!is_port_in_cmis_initialization_process`) — but
    /// EXCLUDES the transient plug-out `REMOVED` stamp. A re-plug latches `REMOVED` for
    /// ~1-2 s (until `force_cmis_reinit` writes the non-terminal `INSERTED`); publishing at
    /// `REMOVED` would leave a `DOM_FLAG` row that outlives the terminal state into the
    /// following non-terminal `INSERTED` window, breaking the DOM-gating contract
    /// (`test_dom_gated_during_cmis_init` forbids a `DOM_FLAG` row while `cmis_state` is
    /// non-terminal). `READY` (healthy) and `FAILED` (bring-up gave up) are the stable
    /// terminal states a re-plug rests at, and any later re-init deletes the row first
    /// (re-gating it as missing), so neither leaks a row into a non-terminal window. Because
    /// the hook runs on the fast ~1 s cadence, excluding `REMOVED` is what keeps it safe — a
    /// present port left at terminal `REMOVED`/`FAILED` by the periodic poll is a rare,
    /// once-per-cadence event, whereas this hook would otherwise catch the `REMOVED` window
    /// almost every re-plug.
    ///
    /// Restricting to a **missing** row keeps the fix faithful and non-intrusive:
    ///   * an intact live-port baseline is never re-read off-cadence — a freshly raised alarm
    ///     can't surface inside a caller's pre-flap guard window
    ///     (`test_link_change_triggers_fast_flag_recapture`); and
    ///   * once the inline publish writes the row the trigger self-clears (row present).
    ///
    /// A port with an outstanding genuine-flap re-read is **still (re)published here** — and
    /// its pending re-read is then CONSUMED (removed) once the baseline lands. A flap that
    /// arrives *before* the port reaches a terminal datapath (the isolation cold-boot case:
    /// the caller bumps `flap_count` while the just-plugged module is still mid-CMIS-init)
    /// queues a re-read that fires mid-init, publishes nothing, and is re-armed (bounded by
    /// `PendingReread::giveup_at`) rather than dropped; either that re-armed re-read OR this
    /// republish hook — whichever runs first once the datapath is terminal — establishes the
    /// baseline. Publishing here on the ~1 s republish cadence the moment the datapath is
    /// terminal — through the SAME re-capture the re-read would run, so it writes the SAME
    /// latched state — makes the baseline appear as reliably as the periodic poll, INDEPENDENT
    /// of when the re-read happens to next fire. Consuming the pending re-read afterwards is
    /// what keeps the guard safe: with the row now present and no re-read left queued, a
    /// subsequently raised alarm can't be surfaced off-cadence inside the caller's guard window
    /// (the exact race the earlier "defer to the pending re-read" guard was protecting — but
    /// that guard sacrificed the baseline's liveness; publish-then-consume protects the guard
    /// WITHOUT that sacrifice). The cheap row-missing gate runs FIRST so the STATUS_SW /
    /// CONFIG_DB / PyO3 `get_presence` reads are reached only for the brief
    /// "re-plugged/flapped, datapath terminal, flag not yet republished" window — never in
    /// steady state (every row present) and never for an absent port. A no-CMIS-manager build
    /// never gates DOM on `cmis_state`,
    /// so this hook stays inert (the periodic pass remains the sole publisher).
    fn republish_missing_flag_baseline_after_cmis_bringup(&self, stop: &AtomicBool) {
        if self.skip_cmis_mgr {
            return;
        }
        for (physical_port, logical_ports) in self.port_mapping.iter_physical_to_logical() {
            if stop.load(Ordering::Relaxed) {
                break;
            }
            let Some(first_logical_port) = logical_ports.first() else {
                continue;
            };
            let Some(asic_index) = self
                .port_mapping
                .get_asic_id_for_logical_port(first_logical_port)
            else {
                continue;
            };
            // Cheapest + most selective gate first: in steady state every DOM_FLAG row is
            // present, so this single `HLEN`-equivalent filters out all ports before any
            // STATUS_SW / CONFIG_DB / EEPROM read. Only a baseline actually DELETED by a
            // physical unplug's `del_port_sfp_dom_info` is republished here (an empty read is
            // no live row: Redis auto-drops empty hashes; the mock removes the key on `del`).
            //
            // Use the *checked* size read so a transient STATE_DB read failure (`None`,
            // indeterminate) is NOT mistaken for a deleted row: only a definitive `Some(0)` is
            // "missing". Misreading a failed read as missing would let this ~1 s hook re-read a
            // *present* port's latched byte-9 flags off the ~60 s poll cadence and surface a
            // freshly-raised alarm inside the link-change isolation guard
            // (`test_link_change_triggers_fast_flag_recapture`). On an indeterminate read we skip
            // the port; the next republish cycle (~1 s) or the periodic poll retries once the read
            // succeeds, so a genuinely-deleted baseline is still recovered well within budget.
            let dom_flag_size = self
                .table_helper
                .get_dom_flag_tbl(asic_index)
                .get_size_for_key_checked(first_logical_port);
            let dom_flag_row_missing = matches!(dom_flag_size, Some(0));
            if !dom_flag_row_missing {
                continue;
            }
            // Defer NOTHING to a pending link-change re-read here: on the isolation cold-boot
            // path the caller bumps `flap_count` while the just-plugged module is still
            // mid-CMIS-init, so a re-read is queued that fires mid-init, publishes nothing and
            // is re-armed (bounded by giveup_at). Skipping the port while that re-read is
            // pending (as an earlier revision did, to keep the flap re-read the sole publisher
            // and protect the caller's guard) makes the entire baseline hinge on that re-read
            // landing exactly as the datapath settles — the single point of failure behind
            // `test_link_change_triggers_fast_flag_recapture` timing out on the baseline in
            // isolation. Instead we (re)publish below on the ~1 s republish cadence the moment
            // the datapath is terminal and CONSUME the pending re-read afterwards, which protects
            // the guard just as well (row present + no re-read left queued ⇒ nothing surfaces a
            // freshly raised alarm off-cadence) while making the baseline as reliable as the
            // periodic poll.
            // The datapath must have reached a settled TERMINAL state — the same condition
            // under which the periodic poll would publish this port
            // (`!is_port_in_cmis_initialization_process`) — but EXCLUDING the transient
            // plug-out `REMOVED` stamp. A re-plug latches `REMOVED` for ~1-2 s (until the CMIS
            // manager writes the non-terminal `INSERTED`); publishing then would leave a
            // `DOM_FLAG` row that outlives the terminal state into the following non-terminal
            // `INSERTED` window, breaking the DOM-gating contract
            // (`test_dom_gated_during_cmis_init`). `READY` (healthy) and `FAILED` (bring-up
            // gave up) are the stable terminal states a re-plug rests at, and any later re-init
            // deletes the row first (re-gating it), so neither leaks a row into a non-terminal
            // window. Reading `cmis_state` once here also feeds the exclusion below.
            let cmis_state = get_cmis_state_from_state_db(
                first_logical_port,
                self.table_helper.get_status_sw_tbl(asic_index),
            );
            if !CMIS_TERMINAL_STATES.contains(&cmis_state.as_str())
                || cmis_state == CMIS_STATE_REMOVED
            {
                continue;
            }
            if self.get_dom_polling_from_config_db(first_logical_port) == "disabled" {
                continue;
            }
            // Only a physically-present module can (re)publish a baseline; an absent port's
            // empty row is correct. Reached only for the transient re-plug window (row missing
            // + datapath at READY + DOM-enabled), so no steady-state / absent-port PyO3 cost.
            let present = self
                .hal
                .sfp(physical_port)
                .ok()
                .and_then(|s| s.get_presence().ok())
                .unwrap_or(false);
            if !present {
                continue;
            }
            // Re-establish the missing baseline INLINE through the SAME flag re-capture a
            // genuine link-change flap uses (`update_port_db_diagnostics_on_link_change`
            // re-reads DOM_FLAG + STATUS_FLAG + VDM_FLAG and their change-tracking metadata).
            // The row is missing, so this is a fresh baseline RE-ESTABLISH, never an
            // off-cadence re-read of an intact row (`test_link_change_flags` stays honoured —
            // the row-missing gate already excluded its steady, present-row port above).
            // Publishing INLINE (not scheduling a debounced re-read drained later) makes the
            // baseline reappear within this ~1 s hook cycle — deterministically inside the
            // e2e's one-cadence budget for a late-index re-plug AND for a flap that landed
            // before the port reached READY — with no dependency on when the flap re-read
            // next fires. The re-capture re-checks its own gates: a tiny race where the port
            // slips back to mid-init between the check above and the call just no-ops
            // (`LinkChangeReread::DeferredCmisInit`), and the next hook cycle retries (the row
            // is still missing).
            let outcome = self.update_port_db_diagnostics_on_link_change(physical_port, stop);
            // Baseline (re)published (or the port turned out permanently ineligible for THIS
            // flap — error/absent, which a drained re-read would `Settle` on too): CONSUME any
            // pending flap re-read for this port. The row now carries the latched state this
            // republish just read, so a later drained re-read would only risk surfacing a
            // freshly-raised alarm inside a caller's post-baseline guard window
            // (`test_link_change_triggers_fast_flag_recapture`). A publish that itself deferred
            // (`DeferredCmisInit`/`TransientRead`) leaves the pending re-read to retry.
            if matches!(outcome, LinkChangeReread::Settled) {
                self.link_change_affected_ports
                    .lock()
                    .unwrap()
                    .remove(&physical_port);
            }
        }
    }

    /// Record the physical port + `flap_count` an APPL_DB `PORT_SET` carries, returning
    /// the physical port **only when the `flap_count` actually changed** for it (a genuine
    /// flap). A non-`SET`/non-APPL_DB event, an event with no resolvable physical port or
    /// no `flap_count`, or a re-delivery of the current `flap_count` all return `None`.
    /// The resolve + dedup-check + record run under one lock so concurrent flaps stay
    /// consistent. This is the shared core of [`Self::on_port_update_event`] (which
    /// schedules a re-read on `Some`) and [`Self::seed_link_change_baseline`] (which only
    /// records the baseline and discards the result).
    fn record_flap_transition(&self, event: &PortChangeEvent) -> Option<usize> {
        if event.event_type != PortChangeEventType::Set || event.db_name != "APPL_DB" {
            return None;
        }
        let physical_port = self.resolve_physical_for_event(event)?;
        // A PORT_SET without a `flap_count` is not a link flap (a non-flap PORT_TABLE
        // write, or a filtered-empty snapshot). An event carrying the `flap_count` already
        // seen for this port is a re-delivery, not a new flap.
        let flap_count = event.port_dict.get("flap_count")?;
        let mut seen = self.last_flap_count.lock().unwrap();
        if seen.get(&physical_port).map(String::as_str) == Some(flap_count.as_str()) {
            return None;
        }
        seen.insert(physical_port, flap_count.clone());
        Some(physical_port)
    }

    /// Resolve the physical port a link-change event targets. The reference keys on the
    /// APPL_DB `PORT_TABLE` `index` field directly; on this emulator testbed that field
    /// may be absent, so we prefer the (always-present) logical-name→physical mapping and
    /// fall back to the event's own index — either way yielding a valid
    /// `physical_to_logical` key for [`Self::update_port_db_diagnostics_on_link_change`].
    fn resolve_physical_for_event(&self, event: &PortChangeEvent) -> Option<usize> {
        if let Some(list) = self.port_mapping.get_logical_to_physical(&event.port_name) {
            if let Some(p) = list.first().copied() {
                return Some(p);
            }
        }
        event.physical_port
    }

    /// `update_port_db_diagnostics_on_link_change(physical_port)` — after a link-change
    /// flap, re-capture the port's DOM + status flag tables (and their metadata) so the
    /// latched-flag snapshot reflects the module's post-flap state. Guards mirror the
    /// reference exactly: stop event, unknown physical port, DOM monitoring disabled,
    /// invalid asic, a blocking error status, and module absence all skip the re-read.
    /// (VDM flag re-capture is included.)
    ///
    /// Returns [`LinkChangeReread`] describing whether this attempt published. The caller
    /// ([`Self::check_port_update`]) consumes the pending entry once the re-read
    /// [`LinkChangeReread::Settled`]s (dom_mgr.py:282), but re-arms it (bounded by
    /// [`PendingReread::giveup_at`]) on a [`LinkChangeReread::DeferredCmisInit`] (DOM polling
    /// enabled but `cmis_state` still non-terminal — publishes nothing, honouring the closed
    /// DOM gate) or [`LinkChangeReread::TransientRead`] (module read momentarily empty), so a
    /// flap detected while a just-plugged port is transiently ineligible still re-captures the
    /// latched flags the moment the datapath settles / the module answers.
    pub fn update_port_db_diagnostics_on_link_change(
        &self,
        physical_port: usize,
        stop: &AtomicBool,
    ) -> LinkChangeReread {
        if stop.load(Ordering::Relaxed) {
            return LinkChangeReread::Settled;
        }

        let Some(logical_port_list) = self.port_mapping.get_physical_to_logical(physical_port)
        else {
            eprintln!(
                "xcvrd-rs: Update DB diagnostics during link change: Unknown physical port \
                 index {physical_port}"
            );
            return LinkChangeReread::Settled;
        };
        // First logical port corresponds to the first subport of the breakout group.
        let Some(first_logical_port) = logical_port_list.first() else {
            return LinkChangeReread::Settled;
        };

        // `is_port_dom_monitoring_disabled` folds two distinct conditions: an operator
        // `dom_polling=disabled` gate (permanent for this flap → settle) and a *transient*
        // non-terminal `cmis_state` (mid-datapath-bring-up → defer + retry). Publishing the
        // flag row now while cmis_state is non-terminal would break the DOM-gating contract
        // (`test_dom_gating`), so only the operator gate settles; the CMIS-init gate defers.
        if self.get_dom_polling_from_config_db(first_logical_port) == "disabled" {
            return LinkChangeReread::Settled;
        }
        if self.is_port_in_cmis_initialization_process(first_logical_port) {
            return LinkChangeReread::DeferredCmisInit;
        }

        let Some(asic_index) = self
            .port_mapping
            .get_asic_id_for_logical_port(first_logical_port)
        else {
            eprintln!(
                "xcvrd-rs: Update DB diagnostics during link change: Got invalid asic index \
                 for {first_logical_port}, ignored"
            );
            return LinkChangeReread::Settled;
        };

        if detect_port_in_error_status(
            first_logical_port,
            self.table_helper.get_status_sw_tbl(asic_index),
        ) {
            return LinkChangeReread::Settled;
        }

        let present = self
            .hal
            .sfp(physical_port)
            .ok()
            .and_then(|s| s.get_presence().ok())
            .unwrap_or(false);
        if !present {
            return LinkChangeReread::Settled;
        }

        // Re-capture TRANSCEIVER_DOM_FLAG + metadata. `posted` is false iff the module's
        // DOM-flag read transiently yielded nothing (validation already passed above, so a
        // false here is *only* an empty/`None` read, not a bad port) — surface that so the
        // caller can retry the flap rather than drop it.
        let dom_flags_posted = self.dom_db.post_port_dom_flags_to_db(
            stop,
            first_logical_port,
            &self.port_mapping,
            self.hal.as_ref(),
            self.table_helper.get_dom_flag_tbl(asic_index),
            self.table_helper.get_dom_flag_change_count_tbl(asic_index),
            self.table_helper.get_dom_flag_set_time_tbl(asic_index),
            self.table_helper.get_dom_flag_clear_time_tbl(asic_index),
            None,
        );

        // Re-capture TRANSCEIVER_STATUS_FLAG + metadata.
        self.status_db.post_port_transceiver_hw_status_flags_to_db(
            stop,
            first_logical_port,
            &self.port_mapping,
            self.table_helper.get_status_flag_tbl(asic_index),
            self.table_helper
                .get_status_flag_change_count_tbl(asic_index),
            self.table_helper.get_status_flag_set_time_tbl(asic_index),
            self.table_helper.get_status_flag_clear_time_tbl(asic_index),
            self.hal.as_ref(),
            None,
        );

        // Re-capture the COR TRANSCEIVER_VDM_*_FLAG tables + metadata (dom_mgr.py:485-493).
        if let Ok(sfp) = self.hal.sfp(physical_port) {
            if self.vdm_utils.is_transceiver_vdm_supported(&*sfp) {
                self.vdm_db.post_port_vdm_flags_to_db(
                    stop,
                    first_logical_port,
                    &self.port_mapping,
                    self.hal.as_ref(),
                    &self.table_helper,
                    None,
                );
            }
        }
        if dom_flags_posted {
            LinkChangeReread::Settled
        } else {
            LinkChangeReread::TransientRead
        }
    }
    /// `check_port_update` — drain any pending APPL_DB `PORT_TABLE` link-change
    /// port-update notifications (feeding each into [`Self::on_port_update_event`]) then
    /// re-capture diagnostics for every physical port whose grace delay has elapsed.
    /// A due re-read is consumed once it [`LinkChangeReread::Settled`]s (published, or was
    /// permanently ineligible) — the reference's unconditional `del` (dom_mgr.py:282) — but
    /// a re-read that fired while the port was *transiently* ineligible
    /// ([`LinkChangeReread::DeferredCmisInit`] mid-CMIS-init, or
    /// [`LinkChangeReread::TransientRead`] empty module read) published nothing and is
    /// re-armed on the ~1 s cadence, bounded by [`PendingReread::giveup_at`], so a genuine
    /// flap's flag re-capture still lands the moment the datapath settles / the module
    /// answers (`test_link_change_triggers_fast_flag_recapture`) — never dropped when the
    /// `TRANSCEIVER_DOM_FLAG` row already exists and the missing-row republish hook cannot
    /// cover it. The retry stays guard-safe: it is consumed the instant it publishes, and
    /// [`Self::republish_missing_flag_baseline_after_cmis_bringup`] consumes it once the
    /// resting baseline lands, so no pending re-read survives past the baseline a caller
    /// waits on to surface a later-raised flag without a new flap. With no live observer it
    /// just paces the loop (nothing is ever scheduled), keeping the periodic pass the sole
    /// publisher.
    fn check_port_update(
        &self,
        stop: &AtomicBool,
        observer: Option<&mut MultiPortChangeObserver>,
        timeout_ms: u64,
    ) {
        match observer {
            Some(obs) => match obs.handle_port_update_event(timeout_ms) {
                Ok(events) => {
                    for ev in &events {
                        self.on_port_update_event(ev);
                    }
                }
                Err(e) => eprintln!("xcvrd-rs: DOM link-change port-update read error: {e}"),
            },
            // No observer: pace the wait loop so it does not spin.
            None => std::thread::sleep(Duration::from_millis(
                timeout_ms.min(PORT_UPDATE_EVENT_SELECT_TIMEOUT_MSECS),
            )),
        }

        // Process each pending link-changed port whose grace delay has elapsed.
        let now = Instant::now();
        let due: Vec<usize> = {
            let map = self.link_change_affected_ports.lock().unwrap();
            map.iter()
                .filter(|(_, p)| p.fire_at <= now)
                .map(|(&p, _)| p)
                .collect()
        };
        for physical_port in due {
            if stop.load(Ordering::Relaxed) {
                break;
            }
            // Fire the re-read, then decide by outcome whether to consume or re-arm it.
            // A `Settled` attempt published (or the port was *permanently* ineligible for
            // this flap) — consume it, mirroring the reference's unconditional
            // `del self.link_change_affected_ports[...]` (dom_mgr.py:282). A `DeferredCmisInit`
            // (port still mid-CMIS-init) or `TransientRead` (module read momentarily empty)
            // published NOTHING: because this daemon detects flaps notification-independently
            // (`reconcile_link_change_flap_counts`), a re-read can fire while a just-plugged
            // port is transiently ineligible — and if the `TRANSCEIVER_DOM_FLAG` row already
            // exists, the republish hook (missing-row only) cannot re-capture it. Dropping the
            // re-read after that one premature attempt silently loses the flap's flag
            // re-capture (`test_link_change_triggers_fast_flag_recapture`). So re-arm it on the
            // ~1 s cadence, bounded by `giveup_at`, until the datapath settles / the module
            // answers and it `Settled`s. The retry stays guard-safe: it is consumed the instant
            // it publishes, and the republish hook consumes it once the resting baseline lands,
            // so no pending re-read survives past the baseline a caller waits on.
            match self.update_port_db_diagnostics_on_link_change(physical_port, stop) {
                LinkChangeReread::Settled => {
                    self.link_change_affected_ports
                        .lock()
                        .unwrap()
                        .remove(&physical_port);
                }
                LinkChangeReread::DeferredCmisInit | LinkChangeReread::TransientRead => {
                    let now = Instant::now();
                    let mut map = self.link_change_affected_ports.lock().unwrap();
                    match map.get(&physical_port) {
                        Some(pending) if now < pending.giveup_at => {
                            let giveup_at = pending.giveup_at;
                            map.insert(
                                physical_port,
                                PendingReread {
                                    fire_at: now
                                        + Duration::from_secs(
                                            DIAG_DB_UPDATE_TIME_AFTER_LINK_CHANGE_SECS,
                                        ),
                                    giveup_at,
                                },
                            );
                        }
                        // Past the give-up bound (or the entry vanished): drop it and let the
                        // periodic pass / republish hook take over.
                        _ => {
                            map.remove(&physical_port);
                        }
                    }
                }
            }
        }
    }

    /// `on_remove_logical_port(port_change_event)` — a CONFIG_DB logical port was
    /// deconfigured (`DomInfoUpdateTask.on_remove_logical_port`, dom_mgr.py:495).
    ///
    /// Tears down only the tables the DOM task itself publishes — DOM sensor/temperature,
    /// DOM flags + metadata, VDM real-values + per-type flags/metadata, the
    /// `TRANSCEIVER_STATUS` HW section + status-flags/metadata, PM and firmware-info — using
    /// the event's own `asic_id` (the mapping entry may already be gone by the time
    /// [`Self::on_port_config_change`] drops it). `TRANSCEIVER_INFO`,
    /// `TRANSCEIVER_DOM_THRESHOLD`, the VDM threshold-value tables and `TRANSCEIVER_STATUS_SW`
    /// are owned by `SfpStateUpdateTask`, so the DOM task leaves them alone (see
    /// [`dom_logical_port_removal_tables`]).
    pub fn on_remove_logical_port(&self, port_change_event: &PortChangeEvent) {
        let tables = dom_logical_port_removal_tables(&self.table_helper, port_change_event.asic_id);
        del_port_sfp_dom_info_from_db(&port_change_event.port_name, &tables);
    }

    /// `on_port_config_change(port_change_event)` — dispatch a CONFIG_DB `PORT` add/remove
    /// (`DomInfoUpdateBase.on_port_config_change`, dom_mgr.py:68).
    ///
    /// On a `PORT_REMOVE` the port's DOM-owned rows are deleted *first*
    /// ([`Self::on_remove_logical_port`], which still needs the mapping/asic in scope), then
    /// the mapping drops the logical port; on a `PORT_ADD` the mapping simply gains it. Either
    /// way `PortMapping::handle_port_change_event` keeps the DOM poll set
    /// (`physical_to_logical`) current so a runtime-added logical port is polled on the next
    /// pass and a removed one is skipped.
    pub fn on_port_config_change(&mut self, port_change_event: &PortChangeEvent) {
        if port_change_event.event_type == PortChangeEventType::Remove {
            self.on_remove_logical_port(port_change_event);
        }
        self.port_mapping
            .handle_port_change_event(port_change_event);
    }

    /// `handle_port_config_change` (`port_event_helper.py:294`) — drain the CONFIG_DB `PORT`
    /// subscriber (blocking up to `timeout_ms`) and route each resolved add/remove through
    /// [`Self::on_port_config_change`]. The immutable `read_port_config_change` (which borrows
    /// `self.port_mapping` to classify SET→ADD / DEL→REMOVE) produces owned events *before*
    /// the dispatch loop mutates the map + DB tables.
    fn handle_port_config_change(&mut self, sub: &mut PortConfigChangeSubscriber, timeout_ms: u64) {
        let updates = sub.poll(timeout_ms);
        if updates.is_empty() {
            return;
        }
        let events = read_port_config_change(&updates, &self.port_mapping, sub.asic_id());
        for ev in events {
            self.on_port_config_change(&ev);
        }
    }

    /// `task_worker` — the periodic loop. The first periodic pass is delayed one full
    /// interval ("to allow xcvrd to initialize ports"); the next pass is scheduled from
    /// each pass's *start* for a consistent cadence. At the top of every outer pass it
    /// drains the CONFIG_DB `PORT` subscriber so a logical port added/removed at runtime is
    /// added to / dropped from the DOM poll set ([`Self::handle_port_config_change`] →
    /// [`Self::on_port_config_change`], dom_mgr.py:286,303). Between passes it services
    /// APPL_DB `PORT_TABLE` link-change flaps on a ~1 s cadence — both via a best-effort
    /// [`MultiPortChangeObserver`] and, notification-independently, by reconciling `flap_count`
    /// straight from APPL_DB ([`Self::reconcile_link_change_flap_counts`]) — so a flap's
    /// latched-flag re-capture lands well inside the e2e fast-timeout without waiting for
    /// the next 60 s periodic pass, even if the keyspace observer misses the wake.
    pub fn task_worker(&mut self, stop: &Arc<AtomicBool>) {
        // Best-effort APPL_DB PORT_TABLE observer for fast link-change flag re-reads. If
        // the subscription can't be built (redis not ready), fall back to periodic-only
        // polling rather than taking the DOM task down.
        let namespaces = self.table_helper.namespaces();
        let mut observer = match MultiPortChangeObserver::for_appl_port_table(&namespaces) {
            Ok(obs) => Some(obs),
            Err(e) => {
                eprintln!(
                    "xcvrd-rs: DOM link-change observer unavailable ({e}); periodic DOM \
                     polling only"
                );
                None
            }
        };

        // Best-effort CONFIG_DB PORT subscriber (dom_mgr.py:286
        // `subscribe_port_config_change`). Drained once per outer pass so a runtime
        // logical-port add/remove keeps the DOM poll mapping current; non-fatal, so the
        // periodic poll still runs if the subscription can't be established.
        let mut port_config_subs = PortConfigChangeSubscriber::for_namespaces(&namespaces);
        let config_timeout_ms = SELECT_TIMEOUT_MSECS / port_config_subs.len().max(1) as u64;

        // Seed the per-port flap_count baseline from the observer's boot snapshot so the
        // daemon-level dedup independently rejects a re-delivered snapshot (no spurious
        // off-cadence flag re-read at start); only a genuine post-boot flap re-reads.
        if let Some(obs) = observer.as_mut() {
            for ev in obs.take_initial_snapshot() {
                self.seed_link_change_baseline(&ev);
            }
        }
        // Also seed the baseline from a direct APPL_DB read: authoritative even when the
        // observer snapshot is empty or the observer is unavailable, so the first reconcile
        // pass below does not mistake an already-present boot flap_count for a fresh flap.
        self.seed_flap_count_baseline_from_db();

        let interval = Duration::from_secs(self.dom_update_interval);
        let reconcile_period = Duration::from_secs(DIAG_DB_UPDATE_TIME_AFTER_LINK_CHANGE_SECS);
        let mut next = Instant::now() + interval;
        let mut next_reconcile = Instant::now() + reconcile_period;
        while !stop.load(Ordering::Relaxed) {
            // React to a CONFIG_DB PORT add/remove at the top of each pass (dom_mgr.py:303),
            // routing add/drop through the mapping so the DOM poll set below is current.
            for sub in &mut port_config_subs {
                self.handle_port_config_change(sub, config_timeout_ms);
                if stop.load(Ordering::Relaxed) {
                    return;
                }
            }

            // Service link-change events until the next scheduled poll, capping each select
            // wait so shutdown, the periodic cadence, AND the ~1 s flap_count reconcile all
            // stay responsive.
            loop {
                if stop.load(Ordering::Relaxed) {
                    return;
                }
                let now = Instant::now();
                let remaining = next.saturating_duration_since(now);
                if remaining.is_zero() {
                    break;
                }
                // Notification-independent flap detection: reconcile APPL_DB flap_count on
                // the ~1 s cadence so a link flap schedules its debounced re-read even if
                // the keyspace observer misses the wake.
                if now >= next_reconcile {
                    self.reconcile_link_change_flap_counts();
                    self.republish_missing_flag_baseline_after_cmis_bringup(stop);
                    next_reconcile = now + reconcile_period;
                }
                let wait = remaining.min(next_reconcile.saturating_duration_since(now));
                let timeout_ms =
                    (wait.as_millis() as u64).clamp(1, PORT_UPDATE_EVENT_SELECT_TIMEOUT_MSECS);
                self.check_port_update(stop, observer.as_mut(), timeout_ms);
            }
            if stop.load(Ordering::Relaxed) {
                break;
            }
            let loop_start = Instant::now();
            // Interleave APPL_DB link-change servicing across the poll pass (dom_mgr.py:326)
            // so a flap landing mid-pass is re-read within ~1 s instead of waiting for the
            // whole (multi-second) pass to finish. Shares `next_reconcile` with the wait loop
            // above so the ~1 s reconcile cadence is continuous across pass boundaries.
            self.poll_once_interleaved(stop, observer.as_mut(), &mut next_reconcile);
            next = loop_start + interval;
        }
    }

    /// Run the task to completion on the calling thread (spawn helper).
    pub fn run(mut self, stop: Arc<AtomicBool>) {
        if stop.load(Ordering::Relaxed) {
            return;
        }
        self.task_worker(&stop);
    }
}

/// `DomThermalInfoUpdateTask` — the fast temperature poll → `TRANSCEIVER_DOM_TEMPERATURE`.
///
/// Not launched by the daemon (the Python default `dom_temperature_poll_interval is
/// None` leaves it unstarted, and no e2e gate reads `TRANSCEIVER_DOM_TEMPERATURE`), but
/// implemented + unit-tested for parity. Its gate is CONFIG_DB `dom_polling` only (it
/// inherits the base gate, without the CMIS-init check).
pub struct DomThermalInfoUpdateTask {
    port_mapping: PortMapping,
    hal: Arc<dyn Hal>,
    table_helper: Arc<XcvrTableHelper>,
    poll_interval: Duration,
    dom_db: DomDbUtils,
}

impl DomThermalInfoUpdateTask {
    pub fn new(
        port_mapping: PortMapping,
        hal: Arc<dyn Hal>,
        table_helper: Arc<XcvrTableHelper>,
        poll_interval: Duration,
    ) -> Self {
        DomThermalInfoUpdateTask {
            port_mapping,
            hal,
            table_helper,
            poll_interval,
            dom_db: DomDbUtils::new(),
        }
    }

    /// `is_port_dom_monitoring_disabled` (base) — CONFIG_DB `dom_polling=disabled` only.
    pub fn is_port_dom_monitoring_disabled(&self, logical_port_name: &str) -> bool {
        get_dom_polling_from_config_db(&self.port_mapping, &self.table_helper, logical_port_name)
            == "disabled"
    }

    /// One fast temperature pass: publish `TRANSCEIVER_DOM_TEMPERATURE` for every
    /// present, polling-enabled, non-error port.
    pub fn poll_once(&self, stop: &AtomicBool) {
        for (physical_port, logical_ports) in self.port_mapping.iter_physical_to_logical() {
            if stop.load(Ordering::Relaxed) {
                break;
            }
            let Some(logical_port_name) = logical_ports.first() else {
                continue;
            };
            if self.is_port_dom_monitoring_disabled(logical_port_name) {
                continue;
            }
            let Some(asic_index) = self
                .port_mapping
                .get_asic_id_for_logical_port(logical_port_name)
            else {
                continue;
            };
            if !detect_port_in_error_status(
                logical_port_name,
                self.table_helper.get_status_sw_tbl(asic_index),
            ) {
                let present = self
                    .hal
                    .sfp(physical_port)
                    .ok()
                    .and_then(|s| s.get_presence().ok())
                    .unwrap_or(false);
                if !present {
                    continue;
                }
            }
            self.dom_db.post_port_dom_temperature_info_to_db(
                stop,
                logical_port_name,
                &self.port_mapping,
                self.table_helper.get_dom_temperature_tbl(asic_index),
                self.hal.as_ref(),
                None,
            );
        }
    }

    /// `task_worker` — poll as soon as possible, then every `poll_interval`.
    pub fn task_worker(&self, stop: &Arc<AtomicBool>) {
        let mut next = Instant::now();
        while !stop.load(Ordering::Relaxed) {
            let now = Instant::now();
            if next > now {
                let remaining = next.saturating_duration_since(now);
                std::thread::sleep(remaining.min(Duration::from_secs(1)));
                continue;
            }
            self.poll_once(stop);
            next = Instant::now() + self.poll_interval;
        }
    }

    pub fn run(self, stop: Arc<AtomicBool>) {
        if stop.load(Ordering::Relaxed) {
            return;
        }
        self.task_worker(&stop);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::mock::{MockDbTable, MockHal, MockSfp};
    use crate::xcvrd_utilities::port_event_helper::{build_port_mapping, PortConfigRow};
    use serde_json::json;
    use std::collections::{BTreeMap, HashMap};

    fn mapping_with(ports: &[(&str, usize)]) -> PortMapping {
        build_port_mapping(
            ports.iter().map(|(name, idx)| PortConfigRow {
                name: name.to_string(),
                index: Some(*idx),
                role: None,
            }),
            0,
        )
    }

    fn full_dom_real_value() -> serde_json::Value {
        let mut m = serde_json::Map::new();
        m.insert("temperature".into(), json!("42.5C"));
        m.insert("voltage".into(), json!("3.3Volts"));
        for lane in 1..=8 {
            m.insert(format!("tx{lane}power"), json!("-1.5dBm"));
            m.insert(format!("rx{lane}power"), json!("-2.5dBm"));
            m.insert(format!("tx{lane}bias"), json!("7.5mA"));
        }
        serde_json::Value::Object(m)
    }

    fn dom_module() -> MockSfp {
        MockSfp::present()
            .with_dom_real_value(full_dom_real_value())
            .with_threshold_info(json!({"temphighalarm": 75.0}))
            // The platform's `CmisApi.get_transceiver_dom_flags` decodes the module
            // temp/vcc alarm-warning group (00h:9) atomically, so `tempHAlarm` and
            // `vccHAlarm` ALWAYS surface as a pair — the reader never returns temp
            // without vcc. Serve the full resting group here so every integration test
            // that drives the poll chain exercises the `false`-valued vcc key end to end
            // (guards e2e test_dom_flag_meta::test_dom_flag_groups_temp_and_vcc, whose
            // baseline needs vccHAlarm to reach STATE_DB as "False" alongside tempHAlarm).
            .with_json(
                "get_transceiver_dom_flags",
                json!({"tempHAlarm": false, "vccHAlarm": false}),
            )
            .with_json(
                "get_transceiver_status_flags",
                json!({
                    "datapath_firmware_fault": false,
                    "module_firmware_fault": false,
                    "module_state_changed": true
                }),
            )
            .with_status(json!({
                "module_state": "ModuleReady",
                "module_fault_cause": "No Fault detected",
                "DP1State": "DataPathActivated",
                "config_state_hostlane1": "ConfigSuccess",
            }))
    }

    /// A `dom_module` that also advertises VDM support: statistic observables are
    /// *unsupported* (so `poll_once` skips the freeze/PM path — no `sleep`, keeping the
    /// test fast) but basic real values + COR flags are served.
    fn vdm_module() -> MockSfp {
        dom_module()
            .with_json("is_transceiver_vdm_supported", json!(true))
            .with_json("is_vdm_statistic_supported", json!(false))
            .with_json(
                "get_transceiver_vdm_real_value_basic",
                json!({
                    "laser_temperature_media1": 38,
                    "esnr_media_input1": 23.1171875,
                }),
            )
            .with_json(
                "get_transceiver_vdm_flags",
                json!({
                    "laser_temperature_media_1_halarm": false,
                    "laser_temperature_media_2_halarm": true,
                }),
            )
    }

    fn task_with(
        ports: &[(&str, usize)],
        sfps: Vec<MockSfp>,
        skip_cmis_mgr: bool,
    ) -> DomInfoUpdateTask {
        DomInfoUpdateTask::new(
            mapping_with(ports),
            Arc::new(MockHal::with_sfps(sfps)),
            Arc::new(XcvrTableHelper::with_mock_tables(&[String::new()])),
            skip_cmis_mgr,
            Some(60),
        )
    }

    /// A [`dom_module`] whose atomic 00h:9 temp/vcc group latches are set to the given
    /// pair — the mock's flag dict is fixed at construction, so a raise/clear *sequence* is
    /// modelled by building a fresh module per phase against the SAME STATE_DB.
    fn dom_module_flags(temp: bool, vcc: bool) -> MockSfp {
        dom_module().with_json(
            "get_transceiver_dom_flags",
            json!({"tempHAlarm": temp, "vccHAlarm": vcc}),
        )
    }

    /// Like [`task_with`] but binds the task to a CALLER-OWNED `Arc<XcvrTableHelper>` so a
    /// sequence of tasks (each a fresh mock HAL) writes/reads one shared STATE_DB — the seam
    /// used to replay a multi-phase e2e flow (baseline → raise → clear) deterministically.
    fn task_sharing_th(
        ports: &[(&str, usize)],
        sfps: Vec<MockSfp>,
        skip_cmis_mgr: bool,
        th: Arc<XcvrTableHelper>,
    ) -> DomInfoUpdateTask {
        DomInfoUpdateTask::new(
            mapping_with(ports),
            Arc::new(MockHal::with_sfps(sfps)),
            th,
            skip_cmis_mgr,
            Some(60),
        )
    }

    /// Root-cause lock for the e2e `test_link_change_triggers_fast_flag_recapture`
    /// pre-flap guard race: the periodic DOM/flag poll cadence MUST NOT be shorter than
    /// the Python reference (`dom_mgr.py` `DEFAULT_DOM_INFO_UPDATE_PERIOD_SECS = 60`). The
    /// daemon constructs `DomInfoUpdateTask` with `dom_update_interval = None`
    /// (`daemon.rs`), so a background poll runs every 60 s; anything shorter would let a
    /// routine poll republish a freshly-raised latched flag inside the test's ~8 s guard
    /// window, surfacing it off the flap trigger. Lock both the constant and the
    /// `None -> default` fallback the daemon relies on so no later edit can quietly
    /// tighten the cadence below the reference `T_DOM`.
    #[test]
    fn test_dom_poll_cadence_defaults_to_reference_60s() {
        assert_eq!(DEFAULT_DOM_INFO_UPDATE_PERIOD_SECS, 60);
        let task = DomInfoUpdateTask::new(
            mapping_with(&[("Ethernet0", 1)]),
            Arc::new(MockHal::with_sfps(vec![dom_module()])),
            Arc::new(XcvrTableHelper::with_mock_tables(&[String::new()])),
            false,
            None,
        );
        assert_eq!(
            task.dom_update_interval,
            DEFAULT_DOM_INFO_UPDATE_PERIOD_SECS
        );
        assert_eq!(task.dom_update_interval, 60);
    }

    // ← tests/test_xcvrd.py::test_DomInfoUpdateTask_dom_update_interval_parameter:
    // `None` → the 60 s default; `0` and any positive value are honored verbatim; a
    // negative value is invalid and falls back to the default (with a warning).
    #[test]
    fn test_dom_update_interval_parameter() {
        let make = |interval: Option<i64>| {
            DomInfoUpdateTask::new(
                mapping_with(&[("Ethernet0", 1)]),
                Arc::new(MockHal::with_sfps(vec![dom_module()])),
                Arc::new(XcvrTableHelper::with_mock_tables(&[String::new()])),
                false,
                interval,
            )
            .dom_update_interval
        };
        assert_eq!(make(None), DEFAULT_DOM_INFO_UPDATE_PERIOD_SECS);
        assert_eq!(make(Some(0)), 0);
        assert_eq!(make(Some(120)), 120);
        assert_eq!(make(Some(1000)), 1000);
        // Negative → invalid → default.
        assert_eq!(make(Some(-5)), DEFAULT_DOM_INFO_UPDATE_PERIOD_SECS);
        // The default constant itself is never mutated.
        assert_eq!(DEFAULT_DOM_INFO_UPDATE_PERIOD_SECS, 60);
    }

    /// An APPL_DB `PORT_TABLE` `PORT_SET` carrying a `flap_count` in its soaked/filtered
    /// `port_dict` — the shape the observer emits for a link flap.
    fn appl_flap_event(port: &str, phys: usize, flap_count: &str) -> PortChangeEvent {
        PortChangeEvent::new(
            port.to_string(),
            Some(phys),
            0,
            PortChangeEventType::Set,
            "APPL_DB".to_string(),
            "PORT_TABLE".to_string(),
        )
        .with_port_dict(BTreeMap::from([(
            "flap_count".to_string(),
            flap_count.to_string(),
        )]))
    }

    // ← tests/test_xcvrd.py::test_DomInfoUpdateTask_get_dom_polling_from_config_db
    #[test]
    fn test_get_dom_polling_from_config_db() {
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], true);
        // Default (no CONFIG_DB row) → "enabled".
        assert_eq!(task.get_dom_polling_from_config_db("Ethernet0"), "enabled");
        assert!(!task.is_port_dom_monitoring_disabled("Ethernet0"));
        // dom_polling=disabled on the group's first subport → gate closes.
        task.table_helper
            .get_cfg_port_tbl(0)
            .hset("Ethernet0", "dom_polling", "disabled");
        assert_eq!(task.get_dom_polling_from_config_db("Ethernet0"), "disabled");
        assert!(task.is_port_dom_monitoring_disabled("Ethernet0"));
    }

    // ← tests/test_xcvrd.py::test_DomInfoUpdateTask_is_port_in_cmis_initialization_process
    #[test]
    fn test_is_port_in_cmis_initialization_process() {
        // skip_cmis_mgr=false so the gate is live.
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], false);
        let status_sw = task.table_helper.get_status_sw_tbl(0);
        // Non-terminal cmis_state → mid-init → disabled.
        status_sw.hset("Ethernet0", "cmis_state", "INSERTED");
        assert!(task.is_port_in_cmis_initialization_process("Ethernet0"));
        assert!(task.is_port_dom_monitoring_disabled("Ethernet0"));
        // Terminal (READY) → not in init.
        status_sw.hset("Ethernet0", "cmis_state", "READY");
        assert!(!task.is_port_in_cmis_initialization_process("Ethernet0"));
        assert!(!task.is_port_dom_monitoring_disabled("Ethernet0"));
        // skip_cmis_mgr short-circuits regardless of state.
        let skipping = task_with(&[("Ethernet0", 0)], vec![dom_module()], true);
        skipping
            .table_helper
            .get_status_sw_tbl(0)
            .hset("Ethernet0", "cmis_state", "INSERTED");
        assert!(!skipping.is_port_in_cmis_initialization_process("Ethernet0"));
    }

    // Once a re-plugged port's CMIS datapath reaches a
    // settled TERMINAL state (READY or FAILED — a re-plug rests at one of these), its
    // latched-flag baseline (DELETED by the unplug) must be re-established PROMPTLY rather than
    // waiting a full DOM poll cadence. On real hardware the datapath activation raises the link
    // and the orchestration agent bumps APPL_DB flap_count, firing the reference's off-cadence
    // flag re-read; this emulator emits no such flap on a physical re-plug, so
    // `republish_missing_flag_baseline_after_cmis_bringup` re-establishes the baseline INLINE
    // (through the same `update_port_db_diagnostics_on_link_change` a genuine flap uses) for any
    // present, DOM-enabled port at a settled terminal state whose TRANSCEIVER_DOM_FLAG row is
    // ABSENT. Publishing inline (not scheduling a debounced re-read drained later) makes the
    // deleted baseline reappear within one ~1 s hook cycle. It NEVER touches an intact (present)
    // row, so a freshly raised alarm can't surface off-cadence (test_link_change_flags).
    #[test]
    fn test_cmis_bringup_complete_republishes_missing_flag_baseline() {
        let stop = AtomicBool::new(false);
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], false);
        let status_sw = task.table_helper.get_status_sw_tbl(0);

        // Port mid-CMIS-init (non-terminal cmis_state): the datapath is still bringing up, so the
        // DOM gate is closed — the baseline is NOT republished even though the row is absent
        // (publishing while non-terminal would break test_dom_gating).
        status_sw.hset("Ethernet0", "cmis_state", "INSERTED");
        task.republish_missing_flag_baseline_after_cmis_bringup(&stop);
        assert_eq!(
            task.table_helper
                .get_dom_flag_tbl(0)
                .get_size_for_key("Ethernet0"),
            0,
            "a port still mid-CMIS-init must not republish its flag baseline"
        );

        // Datapath reached a settled terminal state (READY) while the DOM_FLAG row is ABSENT — the
        // post-unplug deleted state after the re-plug's datapath fully activates. The hook
        // re-establishes the both-False baseline INLINE in this same pass — the full atomic 00h:9
        // temp/vcc group.
        status_sw.hset("Ethernet0", "cmis_state", "READY");
        task.republish_missing_flag_baseline_after_cmis_bringup(&stop);
        let row: HashMap<String, String> = task
            .table_helper
            .get_dom_flag_tbl(0)
            .get("Ethernet0")
            .expect("a settled (READY) port with a DELETED DOM_FLAG row is republished inline")
            .into_iter()
            .collect();
        assert_eq!(row.get("tempHAlarm").map(String::as_str), Some("False"));
        assert_eq!(
            row.get("vccHAlarm").map(String::as_str),
            Some("False"),
            "both halves of the atomic 00h:9 temp/vcc group settle to their resting baseline"
        );

        // Once the baseline is published (row INTACT) the trigger self-clears: a subsequent
        // settled pass must NOT re-read the intact row off-cadence — a freshly raised alarm can't
        // surface inside a caller's pre-flap guard window (test_link_change_flags). Seed a SENTINEL
        // into the (present) row; republish must leave it untouched (row not missing → no re-read).
        task.table_helper.get_dom_flag_tbl(0).set(
            "Ethernet0",
            &[("tempHAlarm".to_string(), "SENTINEL".to_string())],
        );
        task.republish_missing_flag_baseline_after_cmis_bringup(&stop);
        assert_eq!(
            task.table_helper
                .get_dom_flag_tbl(0)
                .hget("Ethernet0", "tempHAlarm"),
            Some("SENTINEL".to_string()),
            "a settled port with an INTACT (non-deleted) DOM_FLAG row must not be re-read \
             off-cadence"
        );
    }

    // test_dom_gating safety guard: the inline baseline re-publish gates on a settled
    // TERMINAL cmis_state — the SAME condition under which the periodic poll would publish this
    // port (`!is_port_in_cmis_initialization_process`) — but EXCLUDES the transient plug-out
    // REMOVED stamp. Non-terminal (mid-re-init) states must NOT publish (test_dom_gating forbids a
    // DOM_FLAG row while cmis_state is non-terminal). REMOVED must NOT publish either: a re-plug
    // latches REMOVED for ~1-2 s before INSERTED, and a row published then would outlive the
    // terminal state into the following non-terminal INSERTED window (a test_dom_gating leak). The
    // stable terminal states a re-plug rests at — READY (healthy) and FAILED (bring-up gave up) —
    // DO publish inline; excluding REMOVED is what keeps the ~1 s hook safe.
    #[test]
    fn test_cmis_bringup_republish_gated_on_terminal_excluding_removed() {
        let stop = AtomicBool::new(false);
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], false);
        let status_sw = task.table_helper.get_status_sw_tbl(0);

        // Neither the mid-re-init (non-terminal) states NOR the transient terminal REMOVED stamp
        // publish a baseline: publishing at any of these would break test_dom_gating.
        for st in [
            "INSERTED",
            "DP_PRE_INIT_CHECK",
            "DP_DEINIT",
            "DP_INIT",
            "REMOVED",
        ] {
            status_sw.hset("Ethernet0", "cmis_state", st);
            task.republish_missing_flag_baseline_after_cmis_bringup(&stop);
            assert_eq!(
                task.table_helper
                    .get_dom_flag_tbl(0)
                    .get_size_for_key("Ethernet0"),
                0,
                "cmis_state={st} (non-terminal, or transient terminal REMOVED) must not republish"
            );
        }

        // FAILED is a STABLE terminal state a re-plug can rest at (bring-up gave up) — the periodic
        // poll publishes it, so the hook accelerates it too. Present + row absent → inline publish.
        status_sw.hset("Ethernet0", "cmis_state", "FAILED");
        task.republish_missing_flag_baseline_after_cmis_bringup(&stop);
        let row: HashMap<String, String> = task
            .table_helper
            .get_dom_flag_tbl(0)
            .get("Ethernet0")
            .expect("a present, row-missing port at terminal FAILED is republished inline")
            .into_iter()
            .collect();
        assert_eq!(row.get("tempHAlarm").map(String::as_str), Some("False"));
        assert_eq!(row.get("vccHAlarm").map(String::as_str), Some("False"));

        // READY (healthy terminal) with the row deleted again likewise publishes inline — THIS is
        // the primary trigger (a re-plug whose datapath fully activated but whose deleted
        // DOM_FLAG baseline has not yet been republished).
        task.table_helper.get_dom_flag_tbl(0).del("Ethernet0");
        status_sw.hset("Ethernet0", "cmis_state", "READY");
        task.republish_missing_flag_baseline_after_cmis_bringup(&stop);
        let row: HashMap<String, String> = task
            .table_helper
            .get_dom_flag_tbl(0)
            .get("Ethernet0")
            .expect("a present, row-missing port at terminal READY is republished inline")
            .into_iter()
            .collect();
        assert_eq!(row.get("tempHAlarm").map(String::as_str), Some("False"));
        assert_eq!(row.get("vccHAlarm").map(String::as_str), Some("False"));
    }

    // End-to-end DELETE -> republish transition, mirroring the exact e2e sequence that
    // regressed: `test_dom`'s last case unplugs+replugs Ethernet100, whose unplug drops
    // TRANSCEIVER_DOM_FLAG (del_port_sfp_dom_info) — then `test_dom_flag_meta` waits <T_DOM for
    // the both-False baseline to REAPPEAR. Here we publish a live baseline, DELETE it (the
    // unplug), settle cmis_state (the re-plug's datapath activation), and assert the hook
    // re-establishes the FULL 00h:9 temp+vcc group (both False) INLINE on the next ~1 s pass —
    // without waiting a whole DOM cadence and without any APPL_DB flap.
    #[test]
    fn test_deleted_flag_baseline_reestablished_after_replug_settles() {
        let stop = AtomicBool::new(false);
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], false);
        // Datapath at READY so the periodic DOM gate is open.
        task.table_helper
            .get_status_sw_tbl(0)
            .hset("Ethernet0", "cmis_state", "READY");

        // A live baseline exists (steady state) — both halves of the group present as False.
        task.poll_once(&stop);
        assert_eq!(
            task.table_helper
                .get_dom_flag_tbl(0)
                .hget("Ethernet0", "vccHAlarm"),
            Some("False".to_string()),
            "precondition: the resting baseline is published before the unplug"
        );

        // Unplug: the DOM_FLAG row is dropped (del_port_sfp_dom_info), leaving it MISSING.
        task.table_helper.get_dom_flag_tbl(0).del("Ethernet0");
        assert_eq!(
            task.table_helper
                .get_dom_flag_tbl(0)
                .get_size_for_key("Ethernet0"),
            0,
            "the unplug deletes the baseline row"
        );

        // Re-plug: the CMIS datapath re-inits and reaches a settled terminal state but — on this
        // emulator — with no APPL_DB flap. The hook re-establishes the deleted baseline INLINE
        // rather than waiting for the slow periodic pass.
        task.republish_missing_flag_baseline_after_cmis_bringup(&stop);
        let row: HashMap<String, String> = task
            .table_helper
            .get_dom_flag_tbl(0)
            .get("Ethernet0")
            .expect("the deleted baseline is re-established once the re-plugged datapath settles")
            .into_iter()
            .collect();
        assert_eq!(row.get("tempHAlarm").map(String::as_str), Some("False"));
        assert_eq!(
            row.get("vccHAlarm").map(String::as_str),
            Some("False"),
            "the re-established baseline carries the FULL temp+vcc group, both settled to False"
        );
    }

    // The inline baseline re-publish is gated on physical presence and on `dom_polling` before it
    // writes anything: an absent module (its row correctly empty) and an operator-disabled port
    // must both be skipped, so the fast path never re-establishes a baseline that should stay
    // absent.
    #[test]
    fn test_cmis_bringup_republish_gated_by_presence_and_dom_polling() {
        let stop = AtomicBool::new(false);
        // Absent module, READY, row absent → no publish (presence gate). On the real testbed an
        // unplugged port's deleted STATUS_SW reads back non-terminal and is filtered earlier;
        // forcing READY here exercises the presence gate directly.
        let absent = task_with(&[("Ethernet0", 0)], vec![MockSfp::absent()], false);
        absent
            .table_helper
            .get_status_sw_tbl(0)
            .hset("Ethernet0", "cmis_state", "READY");
        absent.republish_missing_flag_baseline_after_cmis_bringup(&stop);
        assert_eq!(
            absent
                .table_helper
                .get_dom_flag_tbl(0)
                .get_size_for_key("Ethernet0"),
            0,
            "an absent module must not republish a flag baseline"
        );

        // Present + terminal + row absent but dom_polling=disabled → no publish (operator gate).
        let disabled = task_with(&[("Ethernet0", 0)], vec![dom_module()], false);
        disabled
            .table_helper
            .get_status_sw_tbl(0)
            .hset("Ethernet0", "cmis_state", "READY");
        disabled
            .table_helper
            .get_cfg_port_tbl(0)
            .hset("Ethernet0", "dom_polling", "disabled");
        disabled.republish_missing_flag_baseline_after_cmis_bringup(&stop);
        assert_eq!(
            disabled
                .table_helper
                .get_dom_flag_tbl(0)
                .get_size_for_key("Ethernet0"),
            0,
            "a dom_polling=disabled port must not republish a flag baseline"
        );
    }

    // A missing-row, datapath-settled port with a PENDING link-change re-read is (re)published
    // INLINE by the hook, and its pending re-read is then CONSUMED. This is the isolation
    // cold-boot fix for `test_link_change_triggers_fast_flag_recapture`: the caller bumps
    // `flap_count` while the just-plugged module is still mid-CMIS-init, queuing a re-read that
    // can only fire once the datapath settles. Rather than leave that re-read the SOLE
    // post-terminal publisher (a single point of failure that timed the baseline out in
    // isolation), the hook publishes the baseline on its ~1 s cadence the moment the datapath
    // is terminal and removes the pending re-read — which protects the caller's guard just as
    // well (row present + no re-read left queued ⇒ nothing surfaces a freshly raised alarm
    // off-cadence) while making the baseline as reliable as the periodic poll.
    #[test]
    fn test_cmis_bringup_republish_publishes_and_consumes_pending_link_change_reread() {
        let stop = AtomicBool::new(false);
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], false);
        // Datapath settled + DOM-enabled + present + row absent: the post-flap-before-READY
        // window this fix targets.
        task.table_helper
            .get_status_sw_tbl(0)
            .hset("Ethernet0", "cmis_state", "READY");

        // Queue a flap re-read for this physical port, exactly as a genuine flap_count bump would.
        task.on_port_update_event(&appl_flap_event("Ethernet0", 0, "1"));
        assert!(
            task.link_change_affected_ports
                .lock()
                .unwrap()
                .contains_key(&0),
            "precondition: the flap scheduled a pending off-cadence re-read"
        );

        // The hook publishes the baseline INLINE (does NOT wait for the pending re-read to be
        // drained) so the baseline is as reliable as the periodic poll ...
        task.republish_missing_flag_baseline_after_cmis_bringup(&stop);
        let row: HashMap<String, String> = task
            .table_helper
            .get_dom_flag_tbl(0)
            .get("Ethernet0")
            .expect("a settled, missing-row port is republished inline even with a pending re-read")
            .into_iter()
            .collect();
        assert_eq!(row.get("tempHAlarm").map(String::as_str), Some("False"));
        assert_eq!(row.get("vccHAlarm").map(String::as_str), Some("False"));
        // ... and CONSUMES the pending re-read, so no drained re-read can later surface a
        // freshly-raised alarm inside a caller's post-baseline guard window.
        assert!(
            !task
                .link_change_affected_ports
                .lock()
                .unwrap()
                .contains_key(&0),
            "publishing the baseline must consume the port's pending link-change re-read"
        );
    }

    // The FULL isolation flow of
    // test_link_change_flags::test_link_change_triggers_fast_flag_recapture, exercising the
    // publish-then-consume republish fix end-to-end. In isolation the caller flaps a
    // just-plugged module while it is still mid-CMIS-init, so the baseline row is MISSING and a
    // pending re-read is queued that can only fire once the datapath settles. Reproduce that
    // exact ordering and prove: (1) once terminal, the republish hook publishes the resting
    // baseline "False" AND consumes the pending re-read [the e2e line-78 baseline]; (2) a
    // subsequently RAISED alarm without a flap stays isolated across the inter-poll seams [the
    // e2e ~8 s guard]; (3) a genuine flap still recaptures the raised group "True" [the e2e
    // fast recapture]. Distinct from the sibling guard test, which starts from an already-
    // PRESENT baseline; here the baseline is first established through the pending-re-read path.
    #[test]
    fn test_link_change_isolation_flap_before_ready_publishes_then_guard_then_recaptures() {
        let th = Arc::new(XcvrTableHelper::with_mock_tables(&[String::new()]));
        let stop = AtomicBool::new(false);
        let ports = &[("Ethernet0", 0)];
        // Datapath terminal so the flag gate is OPEN — isolation must hold via seam logic.
        th.get_status_sw_tbl(0)
            .hset("Ethernet0", "cmis_state", "READY");

        // Step 1 — flap landed BEFORE the port was terminal (isolation cold boot): a pending
        // re-read is queued and the DOM_FLAG row is still MISSING (never baselined).
        let flapped = task_sharing_th(
            ports,
            vec![dom_module_flags(false, false)],
            false,
            th.clone(),
        );
        flapped.on_port_update_event(&appl_flap_event("Ethernet0", 0, "1"));
        assert_eq!(
            th.get_dom_flag_tbl(0).get_size_for_key("Ethernet0"),
            0,
            "precondition: no baseline row yet (flap arrived before the datapath settled)"
        );

        // Step 2 — the republish hook (now terminal) publishes the resting baseline "False"
        // [e2e line 78] and CONSUMES the pending re-read, so nothing is left to fire off-cadence.
        flapped.republish_missing_flag_baseline_after_cmis_bringup(&stop);
        assert_eq!(
            th.get_dom_flag_tbl(0).hget("Ethernet0", "tempHAlarm"),
            Some("False".to_string()),
            "the flap-before-READY baseline is published by the republish hook (e2e baseline)"
        );
        assert!(
            !flapped
                .link_change_affected_ports
                .lock()
                .unwrap()
                .contains_key(&0),
            "the pending re-read is consumed once the baseline is published (guard-safe)"
        );

        // Step 3 GUARD — the module RAISES both alarms with NO flap; no inter-poll seam may
        // surface them inside the caller's guard window [e2e line 84].
        let raised = task_sharing_th(ports, vec![dom_module_flags(true, true)], false, th.clone());
        raised.reconcile_link_change_flap_counts(); // no APPL flap_count → schedules nothing
        raised.republish_missing_flag_baseline_after_cmis_bringup(&stop); // row present → skip
        raised.check_port_update(&stop, None, 1); // nothing pending → no off-cadence re-read
        assert_eq!(
            th.get_dom_flag_tbl(0).hget("Ethernet0", "tempHAlarm"),
            Some("False".to_string()),
            "a raised alarm WITHOUT a flap must stay baselined through the guard window"
        );

        // Step 4 FAST — a genuine flap recaptures the now-raised group "True" [e2e line 90].
        raised.on_port_update_event(&appl_flap_event("Ethernet0", 0, "2"));
        raised.link_change_affected_ports.lock().unwrap().insert(
            0,
            PendingReread {
                fire_at: Instant::now(),
                giveup_at: Instant::now() + Duration::from_secs(60),
            },
        );
        raised.check_port_update(&stop, None, 1);
        assert_eq!(
            th.get_dom_flag_tbl(0).hget("Ethernet0", "tempHAlarm"),
            Some("True".to_string()),
            "the post-flap fast re-read recaptures the raised latch (e2e fast recapture)"
        );
    }

    // ROOT-CAUSE lock for the e2e regression
    // test_link_change_flags::test_link_change_triggers_fast_flag_recapture. This reproduces
    // the exact step-4 mechanism the sibling full-flow test does NOT: a flap whose fast re-read
    // fires while the re-plugged port is *transiently* mid-CMIS-init AND whose TRANSCEIVER_DOM_FLAG
    // row is already PRESENT (a resting "False" baseline). Two facts make the re-arm essential
    // here: (a) the emulator holds the raised temp-alarm as a STABLE stimulus (no clear-on-read,
    // lib/cmis.py) so a single successful read publishes "True" permanently — hence a dropped
    // re-read loses the recapture until the slow ~60 s poll; and (b) the republish hook only
    // (re)establishes a MISSING row, so it cannot recapture a raised flag on this present row.
    // Therefore the re-armed (retried) re-read is the SOLE fast publisher. A fire-once/drop
    // re-read (the regression) leaves "False" through the fast window; the bounded retry
    // publishes "True" the instant the datapath reaches READY.
    #[test]
    fn test_link_change_present_row_flap_reread_rearms_through_cmis_init_then_recaptures() {
        let th = Arc::new(XcvrTableHelper::with_mock_tables(&[String::new()]));
        let stop = AtomicBool::new(false);
        let ports = &[("Ethernet0", 0)];

        // Establish the resting "False" baseline exactly as the e2e does (flap-before-READY →
        // republish publishes the missing baseline once terminal), so the DOM_FLAG row is PRESENT.
        th.get_status_sw_tbl(0)
            .hset("Ethernet0", "cmis_state", "READY");
        let baseline = task_sharing_th(
            ports,
            vec![dom_module_flags(false, false)],
            false,
            th.clone(),
        );
        baseline.on_port_update_event(&appl_flap_event("Ethernet0", 0, "1"));
        baseline.republish_missing_flag_baseline_after_cmis_bringup(&stop);
        assert_eq!(
            th.get_dom_flag_tbl(0).hget("Ethernet0", "tempHAlarm"),
            Some("False".to_string()),
            "precondition: a PRESENT resting False baseline (row exists)"
        );

        // The module now RAISES the temp alarm (a stable latch). A genuine flap arrives, but the
        // re-plugged port is transiently mid-CMIS-init when the fast re-read first fires.
        let raised = task_sharing_th(
            ports,
            vec![dom_module_flags(true, false)],
            false,
            th.clone(),
        );
        th.get_status_sw_tbl(0)
            .hset("Ethernet0", "cmis_state", "INSERTED");
        // A genuine flap (flap_count 1 -> 2) schedules the fast re-read; compress its ~1 s
        // debounce to a due entry so the drain is deterministic.
        raised.on_port_update_event(&appl_flap_event("Ethernet0", 0, "2"));
        raised.link_change_affected_ports.lock().unwrap().insert(
            0,
            PendingReread {
                fire_at: Instant::now(),
                giveup_at: Instant::now() + Duration::from_secs(60),
            },
        );

        // First drain — mid-CMIS-init: the re-read is gated (`DeferredCmisInit`), publishes
        // NOTHING (row stays "False"), and is RE-ARMED. A fire-once drop here would strand
        // "False" through the fast window — the reported defect.
        raised.check_port_update(&stop, None, 1);
        assert_eq!(
            th.get_dom_flag_tbl(0).hget("Ethernet0", "tempHAlarm"),
            Some("False".to_string()),
            "mid-CMIS-init: the raised latch is NOT published yet (DOM gate closed)"
        );
        assert!(
            raised
                .link_change_affected_ports
                .lock()
                .unwrap()
                .contains_key(&0),
            "the fast re-read is re-armed while mid-CMIS-init (a drop would lose the recapture)"
        );

        // The republish hook CANNOT recover this: the DOM_FLAG row is PRESENT, so the
        // missing-row hook skips the port — it neither publishes "True" nor consumes the re-arm.
        // This is why the re-armed re-read, not republish, must be the fast publisher.
        th.get_status_sw_tbl(0)
            .hset("Ethernet0", "cmis_state", "READY");
        raised.republish_missing_flag_baseline_after_cmis_bringup(&stop);
        assert_eq!(
            th.get_dom_flag_tbl(0).hget("Ethernet0", "tempHAlarm"),
            Some("False".to_string()),
            "republish (missing-row only) cannot recapture a raised flag on a PRESENT row"
        );
        assert!(
            raised
                .link_change_affected_ports
                .lock()
                .unwrap()
                .contains_key(&0),
            "republish skipped the present row, so the re-arm is still pending"
        );

        // Datapath now terminal: compress the re-armed debounce and drain again — the re-read
        // Settles, publishes the raised latch "True" fast, and is consumed.
        raised.link_change_affected_ports.lock().unwrap().insert(
            0,
            PendingReread {
                fire_at: Instant::now(),
                giveup_at: Instant::now() + Duration::from_secs(60),
            },
        );
        raised.check_port_update(&stop, None, 1);
        assert_eq!(
            th.get_dom_flag_tbl(0).hget("Ethernet0", "tempHAlarm"),
            Some("True".to_string()),
            "the re-armed re-read recaptures the raised latch once terminal (fast, present-row)"
        );
        assert!(
            raised.link_change_affected_ports.lock().unwrap().is_empty(),
            "the re-read is consumed once it publishes (Settled)"
        );
    }
    #[test]
    fn test_cmis_bringup_hook_inert_when_skip_cmis_mgr() {
        let stop = AtomicBool::new(false);
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], true);
        let status_sw = task.table_helper.get_status_sw_tbl(0);
        status_sw.hset("Ethernet0", "cmis_state", "INSERTED");
        task.republish_missing_flag_baseline_after_cmis_bringup(&stop);
        status_sw.hset("Ethernet0", "cmis_state", "READY");
        task.republish_missing_flag_baseline_after_cmis_bringup(&stop);
        assert_eq!(
            task.table_helper
                .get_dom_flag_tbl(0)
                .get_size_for_key("Ethernet0"),
            0,
            "skip_cmis_mgr must leave the bring-up re-publish hook inert (nothing published)"
        );
    }

    // Root-cause hardening for test_link_change_flags::test_link_change_triggers_fast_flag_
    // recapture. The ~1 s republish hook gates on the DOM_FLAG row being ABSENT before it re-reads
    // and republishes a port. It must distinguish a *definitively empty* row (Some(0) → a genuine
    // delete, republish) from an *indeterminate* STATE_DB read (None → a transient hgetall failure)
    // — collapsing the latter to 0 (the old `get_size_for_key == 0`) would let a failed read on a
    // PRESENT port masquerade as "missing", re-reading its latched byte-9 flags off the ~60 s poll
    // cadence and surfacing a freshly-raised alarm inside the test's ~8 s isolation guard. On an
    // indeterminate read the hook must SKIP the port (retry next cycle); once the read succeeds and
    // the row is truly absent it republishes as before.
    #[test]
    fn test_cmis_bringup_republish_skips_on_indeterminate_flag_read() {
        let stop = AtomicBool::new(false);
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], false);
        task.table_helper
            .get_status_sw_tbl(0)
            .hset("Ethernet0", "cmis_state", "READY");

        // Downcast the hook's DOM_FLAG table to the concrete mock to inject a transient read
        // failure (the analogue of RealDbTable's `hgetall` erroring).
        let dom_flag_mock = task
            .table_helper
            .get_dom_flag_tbl(0)
            .as_any()
            .downcast_ref::<MockDbTable>()
            .expect("with_mock_tables backs every handle with a MockDbTable");

        // Indeterminate read (size read fails → None): a present, settled, row-absent port that
        // would otherwise be republished must be SKIPPED — a failed read is NOT a deleted row, so
        // the hook does not re-read byte-9 off-cadence.
        dom_flag_mock.set_fail_size_reads(true);
        task.republish_missing_flag_baseline_after_cmis_bringup(&stop);
        dom_flag_mock.set_fail_size_reads(false); // restore reads to observe the row honestly
        assert_eq!(
            task.table_helper
                .get_dom_flag_tbl(0)
                .get_size_for_key("Ethernet0"),
            0,
            "an indeterminate STATE_DB read must NOT be treated as a deleted row — the hook skips \
             the port instead of re-reading and republishing off-cadence"
        );

        // Read now succeeds and the row is genuinely absent (Some(0)) → the hook republishes the
        // resting baseline on the next cycle, so a real delete is still recovered.
        task.republish_missing_flag_baseline_after_cmis_bringup(&stop);
        let row: HashMap<String, String> = task
            .table_helper
            .get_dom_flag_tbl(0)
            .get("Ethernet0")
            .expect(
                "once the read succeeds and the row is truly absent the baseline is republished",
            )
            .into_iter()
            .collect();
        assert_eq!(row.get("tempHAlarm").map(String::as_str), Some("False"));
        assert_eq!(row.get("vccHAlarm").map(String::as_str), Some("False"));
    }

    // ← tests/test_xcvrd.py::test_DomInfoUpdateTask_task_worker (one pass)
    #[test]
    fn test_poll_once_publishes_dom_sensor_and_flags() {
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], true);
        task.poll_once(&AtomicBool::new(false));

        // DOM_SENSOR: temperature + voltage + all 24 per-lane keys, unit-stripped.
        let sensor: HashMap<String, String> = task
            .table_helper
            .get_dom_tbl(0)
            .get("Ethernet0")
            .unwrap()
            .into_iter()
            .collect();
        assert_eq!(sensor.get("temperature").map(String::as_str), Some("42.5"));
        assert_eq!(sensor.get("voltage").map(String::as_str), Some("3.3"));
        for lane in 1..=8 {
            assert!(sensor.contains_key(&format!("tx{lane}power")));
            assert!(sensor.contains_key(&format!("rx{lane}power")));
            assert!(sensor.contains_key(&format!("tx{lane}bias")));
        }
        assert!(sensor.contains_key("last_update_time"));

        // DOM_FLAG value row + first-publish metadata init.
        let flag: HashMap<String, String> = task
            .table_helper
            .get_dom_flag_tbl(0)
            .get("Ethernet0")
            .unwrap()
            .into_iter()
            .collect();
        assert_eq!(flag.get("tempHAlarm").map(String::as_str), Some("False"));
        assert_eq!(
            task.table_helper
                .get_dom_flag_change_count_tbl(0)
                .hget("Ethernet0", "tempHAlarm"),
            Some("0".into())
        );
        assert_eq!(
            task.table_helper
                .get_dom_flag_set_time_tbl(0)
                .hget("Ethernet0", "tempHAlarm"),
            Some("never".into())
        );
    }

    #[test]
    fn test_poll_once_skips_when_dom_polling_disabled() {
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], true);
        task.table_helper
            .get_cfg_port_tbl(0)
            .hset("Ethernet0", "dom_polling", "disabled");
        task.poll_once(&AtomicBool::new(false));
        assert_eq!(task.table_helper.get_dom_tbl(0).get("Ethernet0"), None);
        assert_eq!(task.table_helper.get_dom_flag_tbl(0).get("Ethernet0"), None);
        assert_eq!(task.table_helper.get_status_tbl(0).get("Ethernet0"), None);
    }

    // ← tests/test_xcvrd.py::test_DomInfoUpdateTask_task_worker (STATUS row on a pass)
    #[test]
    fn test_poll_once_publishes_transceiver_status() {
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], true);
        task.poll_once(&AtomicBool::new(false));

        let status: HashMap<String, String> = task
            .table_helper
            .get_status_tbl(0)
            .get("Ethernet0")
            .unwrap()
            .into_iter()
            .collect();
        assert_eq!(
            status.get("module_state").map(String::as_str),
            Some("ModuleReady")
        );
        assert_eq!(
            status.get("module_fault_cause").map(String::as_str),
            Some("No Fault detected")
        );
        assert_eq!(
            status.get("DP1State").map(String::as_str),
            Some("DataPathActivated")
        );
        assert_eq!(
            status.get("config_state_hostlane1").map(String::as_str),
            Some("ConfigSuccess")
        );
        assert!(status.contains_key("last_update_time"));
    }

    // ← tests/test_xcvrd.py::test_DomInfoUpdateTask_task_worker (STATUS_FLAG row on a pass)
    #[test]
    fn test_poll_once_publishes_status_flags() {
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], true);
        task.poll_once(&AtomicBool::new(false));

        // STATUS_FLAG value row: 3 flags + last_update_time (bools default-beautified).
        let flag: HashMap<String, String> = task
            .table_helper
            .get_status_flag_tbl(0)
            .get("Ethernet0")
            .unwrap()
            .into_iter()
            .collect();
        assert_eq!(
            flag.get("datapath_firmware_fault").map(String::as_str),
            Some("False")
        );
        assert_eq!(
            flag.get("module_state_changed").map(String::as_str),
            Some("True")
        );
        assert!(flag.contains_key("last_update_time"));

        // First-publish metadata init: change count 0, set/clear time "never".
        assert_eq!(
            task.table_helper
                .get_status_flag_change_count_tbl(0)
                .hget("Ethernet0", "module_state_changed"),
            Some("0".into())
        );
        assert_eq!(
            task.table_helper
                .get_status_flag_set_time_tbl(0)
                .hget("Ethernet0", "module_state_changed"),
            Some("never".into())
        );
    }

    // ← tests/test_xcvrd.py::test_update_port_db_diagnostics_on_link_change
    #[test]
    fn test_update_port_db_diagnostics_on_link_change() {
        // Case 1: valid, present, non-error port → DOM + STATUS flag tables re-captured.
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], true);
        task.update_port_db_diagnostics_on_link_change(0, &AtomicBool::new(false));
        assert!(
            task.table_helper
                .get_dom_flag_tbl(0)
                .get("Ethernet0")
                .is_some(),
            "DOM flags must be re-captured on link change"
        );
        assert!(
            task.table_helper
                .get_status_flag_tbl(0)
                .get("Ethernet0")
                .is_some(),
            "status flags must be re-captured on link change"
        );

        // Case 2: unknown physical port (not in the map) → nothing posted, no panic.
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], true);
        task.update_port_db_diagnostics_on_link_change(2, &AtomicBool::new(false));
        assert!(task
            .table_helper
            .get_dom_flag_tbl(0)
            .get("Ethernet0")
            .is_none());
        assert!(task
            .table_helper
            .get_status_flag_tbl(0)
            .get("Ethernet0")
            .is_none());

        // Case 4: port in a blocking error status → skip (no re-capture).
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], true);
        task.table_helper.get_status_sw_tbl(0).hset(
            "Ethernet0",
            "error",
            "Blocking EEPROM from being read",
        );
        task.update_port_db_diagnostics_on_link_change(0, &AtomicBool::new(false));
        assert!(task
            .table_helper
            .get_dom_flag_tbl(0)
            .get("Ethernet0")
            .is_none());
        assert!(task
            .table_helper
            .get_status_flag_tbl(0)
            .get("Ethernet0")
            .is_none());

        // Case 5: transceiver absent → skip.
        let task = task_with(&[("Ethernet0", 0)], vec![MockSfp::absent()], true);
        task.update_port_db_diagnostics_on_link_change(0, &AtomicBool::new(false));
        assert!(task
            .table_helper
            .get_dom_flag_tbl(0)
            .get("Ethernet0")
            .is_none());

        // Stop event set → immediate return (no re-capture).
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], true);
        task.update_port_db_diagnostics_on_link_change(0, &AtomicBool::new(true));
        assert!(task
            .table_helper
            .get_status_flag_tbl(0)
            .get("Ethernet0")
            .is_none());
    }

    // ← tests/test_xcvrd.py::test_DomInfoUpdateTask on_port_update_event scheduling.
    // A link-change re-read is scheduled only for a genuine APPL_DB flap: a PORT_SET
    // carrying a flap_count that CHANGED for the resolved physical port. This mirrors the
    // reference's FILTER['flap_count'] + PortChangeEvent dedup net behaviour and, per the
    // Stops a re-delivered/unchanged flap_count (or a non-flap PORT_TABLE
    // write) from re-capturing the latched flags off-cadence.
    #[test]
    fn test_on_port_update_event_schedules_link_change() {
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], true);

        // A PORT_SET on APPL_DB carrying a NEW flap_count schedules a re-read (keyed by
        // physical port).
        task.on_port_update_event(&appl_flap_event("Ethernet0", 0, "1"));
        assert!(task
            .link_change_affected_ports
            .lock()
            .unwrap()
            .contains_key(&0));

        // Re-delivery of the SAME flap_count is not a new flap → no re-schedule. Clear
        // the pending set so a fresh insert (if any) would be observable.
        task.link_change_affected_ports.lock().unwrap().clear();
        task.on_port_update_event(&appl_flap_event("Ethernet0", 0, "1"));
        assert!(
            task.link_change_affected_ports.lock().unwrap().is_empty(),
            "an unchanged flap_count must not re-schedule a re-read"
        );

        // A CHANGED flap_count is a genuine flap → schedule again.
        task.on_port_update_event(&appl_flap_event("Ethernet0", 0, "2"));
        assert!(task
            .link_change_affected_ports
            .lock()
            .unwrap()
            .contains_key(&0));

        // An APPL_DB PORT_SET WITHOUT a flap_count is not a link flap → no schedule.
        let task2 = task_with(&[("Ethernet0", 0)], vec![dom_module()], true);
        let appl_no_flap = PortChangeEvent::new(
            "Ethernet0".to_string(),
            Some(0),
            0,
            PortChangeEventType::Set,
            "APPL_DB".to_string(),
            "PORT_TABLE".to_string(),
        );
        task2.on_port_update_event(&appl_no_flap);
        assert!(task2.link_change_affected_ports.lock().unwrap().is_empty());

        // A non-APPL_DB SET does not schedule anything (even carrying a flap_count).
        let cfg_set = PortChangeEvent::new(
            "Ethernet0".to_string(),
            Some(0),
            0,
            PortChangeEventType::Set,
            "CONFIG_DB".to_string(),
            "PORT_TABLE".to_string(),
        )
        .with_port_dict(BTreeMap::from([(
            "flap_count".to_string(),
            "1".to_string(),
        )]));
        task2.on_port_update_event(&cfg_set);
        assert!(task2.link_change_affected_ports.lock().unwrap().is_empty());

        // A PORT_DEL on APPL_DB does not schedule anything (only link-up SETs do).
        let appl_del = PortChangeEvent::new(
            "Ethernet0".to_string(),
            Some(0),
            0,
            PortChangeEventType::Del,
            "APPL_DB".to_string(),
            "PORT_TABLE".to_string(),
        )
        .with_port_dict(BTreeMap::from([(
            "flap_count".to_string(),
            "3".to_string(),
        )]));
        task2.on_port_update_event(&appl_del);
        assert!(task2.link_change_affected_ports.lock().unwrap().is_empty());
    }

    // The flap_count dedup is tracked per physical port: independent ports flap
    // independently, and a re-delivered flap_count on one port leaves the other alone.
    // Mirrors the failing e2e scenario (raise a flag with no flap → no re-read) at the
    // unit level.
    #[test]
    fn test_on_port_update_event_flap_count_tracked_per_port() {
        let task = task_with(
            &[("Ethernet0", 0), ("Ethernet8", 1)],
            vec![dom_module(), dom_module()],
            true,
        );

        // Both ports genuinely flap → both scheduled.
        task.on_port_update_event(&appl_flap_event("Ethernet0", 0, "5"));
        task.on_port_update_event(&appl_flap_event("Ethernet8", 1, "5"));
        {
            let m = task.link_change_affected_ports.lock().unwrap();
            assert!(m.contains_key(&0) && m.contains_key(&1));
        }
        task.link_change_affected_ports.lock().unwrap().clear();

        // Re-deliver port 0's current flap_count (no change) → nothing scheduled.
        task.on_port_update_event(&appl_flap_event("Ethernet0", 0, "5"));
        assert!(
            task.link_change_affected_ports.lock().unwrap().is_empty(),
            "a re-delivered flap_count on one port must not schedule anything"
        );

        // Only port 1 flaps again → only port 1 is (re)scheduled.
        task.on_port_update_event(&appl_flap_event("Ethernet8", 1, "6"));
        let m = task.link_change_affected_ports.lock().unwrap();
        assert!(m.contains_key(&1) && !m.contains_key(&0));
    }

    // Seeding the per-port flap_count baseline from the
    // observer's boot snapshot must (a) NOT schedule any re-read, and (b) make the
    // daemon-level dedup independently reject a re-delivered boot snapshot — even if it
    // ever reaches on_port_update_event (bypassing the observer's own cache priming). Only
    // a genuine POST-boot flap_count increment then re-captures the latched flags.
    #[test]
    fn test_seed_link_change_baseline_suppresses_boot_snapshot_reread() {
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], true);

        // Boot snapshot carries flap_count=7 → record the baseline, schedule NOTHING.
        task.seed_link_change_baseline(&appl_flap_event("Ethernet0", 0, "7"));
        assert!(
            task.link_change_affected_ports.lock().unwrap().is_empty(),
            "seeding the boot baseline must never schedule an off-cadence re-read"
        );

        // A re-delivered boot snapshot (same flap_count) reaching on_port_update_event is
        // now rejected by the seeded daemon-level dedup → still no re-read.
        task.on_port_update_event(&appl_flap_event("Ethernet0", 0, "7"));
        assert!(
            task.link_change_affected_ports.lock().unwrap().is_empty(),
            "a re-delivered boot flap_count must not re-capture the flag tables"
        );

        // A genuine post-boot flap (incremented flap_count) is a real link-change → one
        // scheduled re-read.
        task.on_port_update_event(&appl_flap_event("Ethernet0", 0, "8"));
        assert!(
            task.link_change_affected_ports
                .lock()
                .unwrap()
                .contains_key(&0),
            "a real post-boot flap_count bump must schedule a re-read"
        );
    }

    // The notification-independent flap detector. Reconciling `flap_count`
    // straight from APPL_DB (not via the keyspace observer) schedules a debounced re-read
    // on a genuine change, and — sharing the `last_flap_count` dedup with the observer
    // path — does NOT re-schedule for an unchanged flap_count. This mirrors the failing
    // e2e (Ethernet48 flap → off-cadence flag re-capture) at the unit level, on the direct
    // read that guarantees detection even when the emulator drops the keyspace wake.
    #[test]
    fn test_reconcile_flap_count_change_schedules_link_change() {
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], true);
        let app = task.table_helper.get_app_port_tbl(0);

        // First flap_count seen for the port is a genuine change → schedule a re-read.
        app.hset("Ethernet0", "flap_count", "1");
        task.reconcile_link_change_flap_counts();
        assert!(
            task.link_change_affected_ports
                .lock()
                .unwrap()
                .contains_key(&0),
            "a first/changed APPL_DB flap_count must schedule a link-change re-read"
        );

        // Re-reading the SAME flap_count is not a new flap → nothing re-scheduled.
        task.link_change_affected_ports.lock().unwrap().clear();
        task.reconcile_link_change_flap_counts();
        assert!(
            task.link_change_affected_ports.lock().unwrap().is_empty(),
            "an unchanged flap_count must not re-schedule off the direct-read path"
        );

        // A bumped flap_count is a real flap again → schedule.
        app.hset("Ethernet0", "flap_count", "2");
        task.reconcile_link_change_flap_counts();
        assert!(task
            .link_change_affected_ports
            .lock()
            .unwrap()
            .contains_key(&0));
    }

    // Seeding the flap_count baseline directly from APPL_DB at task start must suppress a
    // spurious boot re-read: an already-present flap_count is recorded but NOT scheduled,
    // and only a genuine post-seed increment then re-captures the latched flags. This
    // enforces the daemon's deliberate "no boot-prime DOM pass" invariant on the
    // direct-read path (daemon.rs) so the e2e 8 s guard is never violated at start.
    #[test]
    fn test_seed_flap_count_baseline_from_db_suppresses_boot_reread() {
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], true);
        let app = task.table_helper.get_app_port_tbl(0);

        // Boot: port already carries flap_count=5. Seeding records the baseline …
        app.hset("Ethernet0", "flap_count", "5");
        task.seed_flap_count_baseline_from_db();
        // … and the first reconcile pass sees the same value → schedules NOTHING.
        task.reconcile_link_change_flap_counts();
        assert!(
            task.link_change_affected_ports.lock().unwrap().is_empty(),
            "an already-present boot flap_count must not fire an off-cadence re-read"
        );

        // A genuine post-boot flap (incremented flap_count) is a real link-change → one
        // scheduled re-read.
        app.hset("Ethernet0", "flap_count", "6");
        task.reconcile_link_change_flap_counts();
        assert!(
            task.link_change_affected_ports
                .lock()
                .unwrap()
                .contains_key(&0),
            "a real post-boot flap_count bump must schedule a re-read"
        );
    }

    // A PORT_TABLE row without a flap_count (or an empty APPL_DB) is not a flap: the
    // direct-read reconcile schedules nothing.
    #[test]
    fn test_reconcile_absent_flap_count_no_schedule() {
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], true);
        task.reconcile_link_change_flap_counts();
        assert!(task.link_change_affected_ports.lock().unwrap().is_empty());

        // A PORT_TABLE write of an unrelated field (no flap_count) is still not a flap.
        task.table_helper
            .get_app_port_tbl(0)
            .hset("Ethernet0", "admin_status", "up");
        task.reconcile_link_change_flap_counts();
        assert!(task.link_change_affected_ports.lock().unwrap().is_empty());
    }

    // The direct-read dedup is per physical port: with a seeded baseline, flapping one
    // port leaves the other alone — mirroring the e2e "raise a flag on an unrelated port /
    // without a flap → no re-read" isolation.
    #[test]
    fn test_reconcile_flap_count_tracked_per_port() {
        let task = task_with(
            &[("Ethernet0", 0), ("Ethernet8", 1)],
            vec![dom_module(), dom_module()],
            true,
        );
        let app = task.table_helper.get_app_port_tbl(0);
        app.hset("Ethernet0", "flap_count", "3");
        app.hset("Ethernet8", "flap_count", "3");
        task.seed_flap_count_baseline_from_db();
        task.reconcile_link_change_flap_counts();
        assert!(
            task.link_change_affected_ports.lock().unwrap().is_empty(),
            "seeded baselines must not schedule anything"
        );

        // Only Ethernet0 flaps → only physical port 0 is scheduled.
        app.hset("Ethernet0", "flap_count", "4");
        task.reconcile_link_change_flap_counts();
        let m = task.link_change_affected_ports.lock().unwrap();
        assert!(m.contains_key(&0) && !m.contains_key(&1));
    }

    // A due link-change re-read is drained by check_port_update (no live observer): the
    // scheduled port is re-captured and removed from the pending set.
    #[test]
    fn test_check_port_update_drains_due_link_change() {
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], true);
        // Schedule a re-read whose deadline is already in the past.
        task.link_change_affected_ports.lock().unwrap().insert(
            0,
            PendingReread {
                fire_at: Instant::now(),
                giveup_at: Instant::now() + Duration::from_secs(60),
            },
        );
        task.check_port_update(&AtomicBool::new(false), None, 1);
        assert!(
            task.link_change_affected_ports.lock().unwrap().is_empty(),
            "due port must be removed after processing"
        );
        assert!(task
            .table_helper
            .get_status_flag_tbl(0)
            .get("Ethernet0")
            .is_some());
    }

    // ROOT CAUSE of the e2e test_link_change_triggers_fast_flag_recapture: the
    // periodic poll must service APPL_DB link-change flaps *between every port* of the pass
    // (dom_mgr.py:326), not only between passes. A full DOM poll walks every port's EEPROM
    // and runs for many seconds on the emulator, so the previous un-interleaved poll_once
    // left a flap that landed mid-pass unserviced until the whole pass finished — past the
    // e2e fast window (T_FAST=15 s) — falling back to the next ~60 s poll. Here a due
    // re-read (a flap serviced just as a multi-port pass begins) is drained DURING the pass
    // by poll_once_interleaved, exactly as check_port_update would between passes;
    // `next_reconcile` in the far future isolates the drain from the reconcile.
    #[test]
    fn test_poll_once_interleaved_drains_due_link_change_mid_pass() {
        let task = task_with(
            &[("Ethernet0", 0), ("Ethernet8", 1)],
            vec![dom_module(), dom_module()],
            true,
        );
        // A re-read whose deadline already elapsed, for the FIRST port walked in the pass.
        task.link_change_affected_ports.lock().unwrap().insert(
            0,
            PendingReread {
                fire_at: Instant::now(),
                giveup_at: Instant::now() + Duration::from_secs(60),
            },
        );
        // Keep reconcile out of this pass so the assertion isolates the mid-pass drain.
        let mut next_reconcile = Instant::now() + Duration::from_secs(3600);
        task.poll_once_interleaved(&AtomicBool::new(false), None, &mut next_reconcile);

        // The due re-read was serviced DURING the poll pass (the fix). Before it, poll_once
        // ran the entire multi-second pass without ever draining a pending re-read.
        assert!(
            task.link_change_affected_ports.lock().unwrap().is_empty(),
            "a due link-change re-read must be serviced during the poll pass, not deferred \
             to the next periodic interval"
        );
        // The poll itself still ran end to end: DOM sensor rows posted for both ports.
        assert!(task.table_helper.get_dom_tbl(0).get("Ethernet0").is_some());
        assert!(task.table_helper.get_dom_tbl(0).get("Ethernet8").is_some());
    }

    // The notification-independent flap_count reconcile also runs on its ~1 s cadence
    // WITHIN the poll pass: a flap present when a (long) pass starts is detected and a
    // debounced re-read scheduled during the pass, so it fires ~1 s later rather than
    // waiting for the next ~60 s interval — the direct-read twin of the drain above.
    #[test]
    fn test_poll_once_interleaved_reconciles_flap_mid_pass() {
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], true);
        task.table_helper
            .get_app_port_tbl(0)
            .hset("Ethernet0", "flap_count", "1");
        // Reconcile is due at the very start of the pass.
        let mut next_reconcile = Instant::now();
        task.poll_once_interleaved(&AtomicBool::new(false), None, &mut next_reconcile);

        // The flap was reconciled mid-pass and a debounced re-read scheduled (fire_at ~1 s
        // out, so not yet drained in this pass) — link-change detection is not starved by a
        // long poll.
        assert!(
            task.link_change_affected_ports
                .lock()
                .unwrap()
                .contains_key(&0),
            "a flap_count change must be reconciled during the poll pass"
        );
        // The ~1 s reconcile deadline was advanced past the pass so it is not re-scanned for
        // every remaining port of the same pass.
        assert!(next_reconcile > Instant::now());
    }

    // With the CMIS manager live
    // (skip_cmis_mgr=false), a flap re-read that fires while the flapped port is transiently
    // mid-CMIS-init must NOT publish while the DOM gate is closed (that would break
    // test_dom_gating). But because this daemon detects flaps notification-independently
    // (reconcile), the re-read can fire before the just-plugged port settles — so a
    // `DeferredCmisInit` attempt is RE-ARMED (bounded by giveup_at), not dropped: it retries
    // on the ~1 s cadence and publishes the latched state the moment cmis_state reaches a
    // terminal state. Dropping it after one premature attempt (an earlier revision) silently
    // lost the flap's flag re-capture whenever the TRANSCEIVER_DOM_FLAG row already existed —
    // the reported e2e defect.
    #[test]
    fn test_check_port_update_rearms_reread_when_cmis_init_then_publishes_on_terminal() {
        let stop = AtomicBool::new(false);
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], false);
        let status_sw = task.table_helper.get_status_sw_tbl(0);
        // Port is mid-datapath-bring-up: non-terminal cmis_state → re-read must not publish yet.
        status_sw.hset("Ethernet0", "cmis_state", "INSERTED");
        // A due re-read (fire_at already elapsed) with a future give-up bound.
        task.link_change_affected_ports.lock().unwrap().insert(
            0,
            PendingReread {
                fire_at: Instant::now(),
                giveup_at: Instant::now() + Duration::from_secs(60),
            },
        );

        task.check_port_update(&stop, None, 1);
        // Gated by CMIS-init: nothing published this attempt, but the re-read is RE-ARMED (not
        // dropped) so the flap's flag re-capture survives until the datapath settles.
        assert!(
            task.table_helper
                .get_dom_flag_tbl(0)
                .get("Ethernet0")
                .is_none(),
            "must not publish DOM flags while the port is mid-CMIS-init"
        );
        assert!(
            task.table_helper
                .get_status_flag_tbl(0)
                .get("Ethernet0")
                .is_none(),
            "must not publish status flags while the port is mid-CMIS-init"
        );
        assert!(
            task.link_change_affected_ports.lock().unwrap().contains_key(&0),
            "a re-read gated by CMIS-init is re-armed, not dropped, so the flap re-capture survives"
        );

        // Datapath settles (terminal cmis_state). Compress the re-armed ~1 s debounce so the
        // drain is deterministic, then service it: the re-read now Settles, publishes the
        // latched flag baselines, and is consumed.
        status_sw.hset("Ethernet0", "cmis_state", "READY");
        task.link_change_affected_ports.lock().unwrap().insert(
            0,
            PendingReread {
                fire_at: Instant::now(),
                giveup_at: Instant::now() + Duration::from_secs(60),
            },
        );
        task.check_port_update(&stop, None, 1);
        assert!(
            task.table_helper
                .get_dom_flag_tbl(0)
                .get("Ethernet0")
                .is_some(),
            "the re-armed re-read publishes the DOM flag baseline once terminal"
        );
        assert!(
            task.table_helper
                .get_status_flag_tbl(0)
                .get("Ethernet0")
                .is_some(),
            "the re-armed re-read publishes the status flag baseline once terminal"
        );
        assert!(
            task.link_change_affected_ports.lock().unwrap().is_empty(),
            "the re-read is consumed once it Settles (publishes)"
        );
    }

    // Guard-safety of the re-armed re-read. With the re-read RE-ARMED (retried)
    // instead of dropped mid-CMIS-init, guard-safety no longer comes from the drop; it comes
    // from the republish hook CONSUMING the pending re-read once it (re)establishes the resting
    // DOM_FLAG baseline on a terminal datapath. So when the module LATER raises a latched flag
    // WITHOUT a new flap, a subsequent check_port_update pass has nothing pending and does NOT
    // surface it — exactly the e2e ~8 s post-baseline guard
    // (test_link_change_triggers_fast_flag_recapture): no lingering off-cadence re-read may leak
    // a freshly-raised alarm.
    #[test]
    fn test_republish_consumes_rearmed_reread_guard_safe() {
        let stop = AtomicBool::new(false);
        let task = task_with(
            &[("Ethernet0", 0)],
            vec![dom_module_flags(false, false)],
            false,
        );
        let status_sw = task.table_helper.get_status_sw_tbl(0);
        // Mid-CMIS-init when the scheduled re-read first fires → re-armed, publishes nothing,
        // DOM_FLAG row stays missing.
        status_sw.hset("Ethernet0", "cmis_state", "INSERTED");
        task.link_change_affected_ports.lock().unwrap().insert(
            0,
            PendingReread {
                fire_at: Instant::now(),
                giveup_at: Instant::now() + Duration::from_secs(60),
            },
        );
        task.check_port_update(&stop, None, 1);
        assert!(
            task.link_change_affected_ports
                .lock()
                .unwrap()
                .contains_key(&0),
            "the re-read is re-armed mid-CMIS-init, not dropped"
        );
        assert!(
            task.table_helper
                .get_dom_flag_tbl(0)
                .get("Ethernet0")
                .is_none(),
            "nothing published while mid-CMIS-init (row still missing)"
        );

        // Datapath settles: the republish hook re-establishes the missing baseline AND consumes
        // the pending re-read, closing the guard window.
        status_sw.hset("Ethernet0", "cmis_state", "READY");
        task.republish_missing_flag_baseline_after_cmis_bringup(&stop);
        assert!(
            task.table_helper
                .get_dom_flag_tbl(0)
                .get("Ethernet0")
                .is_some(),
            "the republish hook re-establishes the missing DOM_FLAG baseline once terminal"
        );
        assert!(
            task.link_change_affected_ports.lock().unwrap().is_empty(),
            "the republish hook consumes the re-armed re-read once the baseline lands (guard-safe)"
        );

        // A later raised alarm WITHOUT a new flap has no pending re-read to surface it
        // off-cadence: another drain leaves the map empty, so nothing leaks into the guard window.
        task.check_port_update(&stop, None, 1);
        assert!(
            task.link_change_affected_ports.lock().unwrap().is_empty(),
            "no lingering re-read survives the baseline, so a later-raised flag cannot leak"
        );
    }

    // The DOM
    // task is wrapped in a `catch_unwind` restart loop in daemon.rs, and the task (with its
    // `last_flap_count` dedup) persists across a restart while `task_worker` re-seeds from
    // APPL_DB on every (re)entry. If that seed were DESTRUCTIVE, a flap that landed after the
    // last reconcile but before a restart would be re-baselined away — the next reconcile
    // would see no change and the off-cadence flag re-capture would never fire, exactly the
    // reported "re-read not effective" symptom. The seed is now non-destructive, so a pending
    // flap survives a restart and is still detected + scheduled.
    #[test]
    fn test_restart_reseed_preserves_pending_flap() {
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], true);
        let app = task.table_helper.get_app_port_tbl(0);

        // Boot: flap_count=1, seeded as the baseline (no schedule).
        app.hset("Ethernet0", "flap_count", "1");
        task.seed_flap_count_baseline_from_db();
        assert_eq!(
            task.last_flap_count
                .lock()
                .unwrap()
                .get(&0)
                .map(String::as_str),
            Some("1"),
            "boot seed records the current flap_count as the baseline"
        );

        // A genuine flap lands (flap_count 1 -> 2) but the task RESTARTS before the next
        // reconcile observes it; task_worker re-seeds from APPL_DB on re-entry.
        app.hset("Ethernet0", "flap_count", "2");
        task.seed_flap_count_baseline_from_db();

        // The pre-restart baseline must be intact: re-seeding must NOT overwrite "1" with the
        // live "2", which would swallow the pending flap.
        assert_eq!(
            task.last_flap_count
                .lock()
                .unwrap()
                .get(&0)
                .map(String::as_str),
            Some("1"),
            "restart re-seed must be non-destructive so a pending flap is not swallowed"
        );

        // The first post-restart reconcile therefore still sees 2 != 1 and schedules the
        // off-cadence re-read the e2e depends on.
        task.reconcile_link_change_flap_counts();
        assert!(
            task.link_change_affected_ports
                .lock()
                .unwrap()
                .contains_key(&0),
            "a flap that landed before a restart must still schedule a re-read afterwards"
        );
    }

    // End-to-end unit mirror of the e2e step 3 (flap -> tempHAlarm surfaces "True" within
    // T_FAST) via the notification-independent direct-read trigger: seed the flap_count
    // baseline, bump it (a genuine flap), reconcile from APPL_DB (schedules a debounced
    // re-read), then let the deadline elapse and drain it through check_port_update. With a
    // module whose 00h:9 temp latch is RAISED and a terminal CMIS state, the drained re-read
    // must publish TRANSCEIVER_DOM_FLAG|Ethernet0 tempHAlarm == "True" — the exact STATE_DB
    // field/value the failing e2e asserts, produced by the flap path (not a periodic poll).
    #[test]
    fn test_flap_reconcile_surfaces_raised_dom_flag_true() {
        let task = task_with(
            &[("Ethernet0", 0)],
            vec![dom_module_flags(true, false)],
            false,
        );
        // Datapath settled so the re-read is not gated by CMIS-init.
        task.table_helper
            .get_status_sw_tbl(0)
            .hset("Ethernet0", "cmis_state", "READY");

        // Baseline the current flap_count (no schedule) …
        let app = task.table_helper.get_app_port_tbl(0);
        app.hset("Ethernet0", "flap_count", "1");
        task.seed_flap_count_baseline_from_db();

        // … then a genuine flap bumps it; the direct-read reconcile schedules the re-read.
        app.hset("Ethernet0", "flap_count", "2");
        task.reconcile_link_change_flap_counts();
        assert!(
            task.link_change_affected_ports
                .lock()
                .unwrap()
                .contains_key(&0),
            "a real flap must schedule the off-cadence re-read"
        );

        // Force the 1 s debounce deadline to have elapsed, then drain it.
        task.link_change_affected_ports.lock().unwrap().insert(
            0,
            PendingReread {
                fire_at: Instant::now(),
                giveup_at: Instant::now() + Duration::from_secs(60),
            },
        );
        task.check_port_update(&AtomicBool::new(false), None, 1);

        // The flap path (not a periodic poll) surfaces the raised temp-high alarm.
        assert_eq!(
            task.table_helper
                .get_dom_flag_tbl(0)
                .hget("Ethernet0", "tempHAlarm"),
            Some("True".to_string()),
            "a flap must re-capture TRANSCEIVER_DOM_FLAG tempHAlarm as True off-cadence"
        );
        assert!(
            task.link_change_affected_ports.lock().unwrap().is_empty(),
            "the drained re-read must be consumed"
        );
    }

    #[test]
    fn test_poll_once_skips_absent_module() {
        let task = task_with(&[("Ethernet0", 0)], vec![MockSfp::absent()], true);
        task.poll_once(&AtomicBool::new(false));
        assert_eq!(task.table_helper.get_dom_tbl(0).get("Ethernet0"), None);
    }

    #[test]
    fn test_poll_once_skips_error_status_port() {
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], true);
        // A blocking error description on STATUS_SW gates the whole port.
        task.table_helper.get_status_sw_tbl(0).hset(
            "Ethernet0",
            "error",
            "Blocking EEPROM from being read",
        );
        task.poll_once(&AtomicBool::new(false));
        assert_eq!(task.table_helper.get_dom_tbl(0).get("Ethernet0"), None);
    }

    #[test]
    fn test_thermal_task_poll_once_publishes_temperature() {
        let task = DomThermalInfoUpdateTask::new(
            mapping_with(&[("Ethernet0", 0)]),
            Arc::new(MockHal::with_sfps(vec![
                MockSfp::present().with_json("get_temperature", json!(42.5))
            ])),
            Arc::new(XcvrTableHelper::with_mock_tables(&[String::new()])),
            Duration::from_secs(1),
        );
        task.poll_once(&AtomicBool::new(false));
        let row: HashMap<String, String> = task
            .table_helper
            .get_dom_temperature_tbl(0)
            .get("Ethernet0")
            .unwrap()
            .into_iter()
            .collect();
        assert_eq!(row.get("temperature").map(String::as_str), Some("42.5"));
    }

    // ← tests/test_xcvrd.py::test_post_port_pm_info_to_db
    #[test]
    fn test_post_port_pm_info_to_db() {
        // Coherent (paged) module with a 6-key PM dict → TRANSCEIVER_PM keyed by the
        // physical port name (Ethernet0, non-breakout), no last_update_time.
        let pm_sfp = MockSfp::present().with_json(
            "get_transceiver_pm",
            json!({
                "prefec_ber_avg": "0.0003407240007014899",
                "prefec_ber_min": "0.0006814479342250317",
                "prefec_ber_max": "0.0006833674050752236",
                "uncorr_frames_avg": "0.0",
                "uncorr_frames_min": "0.0",
                "uncorr_frames_max": "0.0",
            }),
        );
        let task = task_with(&[("Ethernet0", 0)], vec![pm_sfp], true);
        let pm_tbl = task.table_helper.get_pm_tbl(0);
        assert_eq!(pm_tbl.get_size(), 0);
        task.post_port_pm_info_to_db(&AtomicBool::new(false), "Ethernet0", pm_tbl, None);
        assert_eq!(pm_tbl.get_size_for_key("Ethernet0"), 6);

        // Flat-memory module → skipped (no PM page on flat/SFF memory).
        let flat = task_with(
            &[("Ethernet0", 0)],
            vec![MockSfp::present()
                .with_api_json("is_flat_memory", json!(true))
                .with_json("get_transceiver_pm", json!({"prefec_ber_avg": "0.1"}))],
            true,
        );
        let flat_tbl = flat.table_helper.get_pm_tbl(0);
        flat.post_port_pm_info_to_db(&AtomicBool::new(false), "Ethernet0", flat_tbl, None);
        assert_eq!(flat_tbl.get_size(), 0);

        // Absent module → skipped.
        let absent = task_with(&[("Ethernet0", 0)], vec![MockSfp::absent()], true);
        let absent_tbl = absent.table_helper.get_pm_tbl(0);
        absent.post_port_pm_info_to_db(&AtomicBool::new(false), "Ethernet0", absent_tbl, None);
        assert_eq!(absent_tbl.get_size(), 0);
    }

    // ← tests/test_xcvrd.py::test_post_port_sfp_firmware_info_to_db
    #[test]
    fn test_post_port_sfp_firmware_info_to_db() {
        let fw = || {
            MockSfp::present().with_json(
                "get_transceiver_info_firmware_versions",
                json!({"active_firmware": "2.1.1", "inactive_firmware": "1.2.4"}),
            )
        };

        // Physical 0 backs BOTH Ethernet0 and Ethernet4 (breakout) → firmware (a per-module
        // property) is posted to every logical subport.
        let task = task_with(&[("Ethernet0", 0), ("Ethernet4", 0)], vec![fw()], true);
        let tbl = task.table_helper.get_firmware_info_tbl(0);

        // Test 1: stop set → no update.
        task.post_port_sfp_firmware_info_to_db(&AtomicBool::new(true), "Ethernet0", tbl, None);
        assert_eq!(tbl.get_size(), 0);

        // Test 3: present → posts to both logical ports (2 fields each), 2 keys total.
        task.post_port_sfp_firmware_info_to_db(&AtomicBool::new(false), "Ethernet0", tbl, None);
        assert_eq!(tbl.get_size_for_key("Ethernet0"), 2);
        assert_eq!(tbl.get_size_for_key("Ethernet4"), 2);
        assert_eq!(tbl.get_size(), 2);

        // Test 2: not present → no update.
        let absent = task_with(
            &[("Ethernet0", 0), ("Ethernet4", 0)],
            vec![MockSfp::absent()],
            true,
        );
        let absent_tbl = absent.table_helper.get_firmware_info_tbl(0);
        absent.post_port_sfp_firmware_info_to_db(
            &AtomicBool::new(false),
            "Ethernet0",
            absent_tbl,
            None,
        );
        assert_eq!(absent_tbl.get_size(), 0);
    }

    // ← tests/test_xcvrd.py::test_post_port_sfp_firmware_info_to_db_lport_list_None
    #[test]
    fn test_post_port_sfp_firmware_info_to_db_lport_list_none() {
        // Logical name "5" resolves to physical 5 (numeric-name path) which has no
        // physical→logical mapping (empty PortMapping) → get_physical_to_logical is None →
        // firmware is not posted for that unknown physical port.
        let mut sfps: Vec<MockSfp> = (0..5).map(|_| MockSfp::absent()).collect();
        sfps.push(MockSfp::present().with_json(
            "get_transceiver_info_firmware_versions",
            json!({"active_firmware": "2.1.1", "inactive_firmware": "1.2.4"}),
        ));
        let task = task_with(&[], sfps, true);
        let tbl = task.table_helper.get_firmware_info_tbl(0);
        task.post_port_sfp_firmware_info_to_db(&AtomicBool::new(false), "5", tbl, None);
        assert_eq!(tbl.get_size(), 0);
    }

    // ← tests/test_xcvrd.py::test_DomInfoUpdateTask_task_worker (VDM subset)
    #[test]
    fn test_poll_once_publishes_vdm_real_values_and_flags() {
        let task = task_with(&[("Ethernet0", 0)], vec![vdm_module()], true);
        task.poll_once(&AtomicBool::new(false));

        // TRANSCEIVER_VDM_REAL_VALUE: 2 basic fields + last_update_time == 3.
        let rv = task.table_helper.get_vdm_real_value_tbl(0);
        assert_eq!(rv.get_size_for_key("Ethernet0"), 3);
        assert_eq!(
            rv.hget("Ethernet0", "laser_temperature_media1"),
            Some("38".to_string())
        );
        assert_eq!(
            rv.hget("Ethernet0", "esnr_media_input1"),
            Some("23.1171875".to_string())
        );

        // TRANSCEIVER_VDM_HALARM_FLAG: 2 flags + last_update_time == 3, with metadata seeded.
        let flag = task.table_helper.get_vdm_flag_tbl(0, "halarm");
        assert_eq!(flag.get_size_for_key("Ethernet0"), 3);
        assert_eq!(
            flag.hget("Ethernet0", "laser_temperature_media_2"),
            Some("True".to_string())
        );
        assert_eq!(
            task.table_helper
                .get_vdm_flag_change_count_tbl(0, "halarm")
                .hget("Ethernet0", "laser_temperature_media_1"),
            Some("0".to_string())
        );

        // Statistic observables unsupported → no freeze → TRANSCEIVER_PM stays empty.
        assert_eq!(task.table_helper.get_pm_tbl(0).get_size(), 0);
    }

    #[test]
    fn legacy_pm_is_polled_without_vdm_capability_hooks() {
        for lpmode in [false, true] {
            let mut sfp =
                dom_module().with_json("get_transceiver_pm", json!({"prefec_ber_avg": "0.001"}));
            sfp.lpmode = lpmode;
            let calls = sfp.call_log.clone();
            let task = task_with(&[("Ethernet0", 0)], vec![sfp], true);
            task.poll_once(&AtomicBool::new(false));
            assert_eq!(
                task.table_helper
                    .get_pm_tbl(0)
                    .hget("Ethernet0", "prefec_ber_avg"),
                Some("0.001".to_string())
            );
            assert!(!calls
                .lock()
                .unwrap()
                .iter()
                .any(|method| method == "freeze_vdm_stats" || method == "unfreeze_vdm_stats"));
        }
    }

    #[test]
    fn existing_vdm_probe_is_not_bypassed_for_pm() {
        for supported in [json!(false), Value::Null] {
            let sfp = dom_module()
                .with_json("is_transceiver_vdm_supported", supported)
                .with_json("get_transceiver_pm", json!({"prefec_ber_avg": "0.001"}));
            let calls = sfp.call_log.clone();
            let task = task_with(&[("Ethernet0", 0)], vec![sfp], true);
            task.poll_once(&AtomicBool::new(false));
            assert_eq!(task.table_helper.get_pm_tbl(0).get_size(), 0);
            assert!(!calls
                .lock()
                .unwrap()
                .iter()
                .any(|method| method == "get_transceiver_pm"));
        }
    }

    // A VDM-supported module with statistic observables → poll_once freezes, captures the
    // statistic snapshot + PM, unfreezes, and merges statistic over basic real values.
    #[test]
    fn test_poll_once_vdm_statistic_freeze_captures_pm_and_merges() {
        let sfp = dom_module()
            .with_json("is_transceiver_vdm_supported", json!(true))
            .with_json("is_vdm_statistic_supported", json!(true))
            // freeze/unfreeze confirm immediately.
            .with_json("freeze_vdm_stats", json!(true))
            .with_json("get_vdm_freeze_status", json!(true))
            .with_json("unfreeze_vdm_stats", json!(true))
            .with_json("get_vdm_unfreeze_status", json!(true))
            .with_json(
                "get_transceiver_vdm_real_value_basic",
                json!({"esnr_media_input1": 10.0}),
            )
            .with_json(
                "get_transceiver_vdm_real_value_statistic",
                json!({"esnr_media_input1": 99.0, "laser_temperature_media1": 40}),
            )
            .with_json("get_transceiver_pm", json!({"prefec_ber_avg": "0.001"}))
            .with_json(
                "get_transceiver_vdm_flags",
                json!({"esnr_media_input1_halarm": false}),
            );
        let task = task_with(&[("Ethernet0", 0)], vec![sfp], true);
        task.poll_once(&AtomicBool::new(false));

        // PM captured during the freeze window.
        assert_eq!(
            task.table_helper
                .get_pm_tbl(0)
                .get_size_for_key("Ethernet0"),
            1
        );

        // Merged real values: statistic esnr (99.0) overrides basic (10.0); the
        // statistic-only laser_temperature key is present. 2 fields + last_update_time.
        let rv = task.table_helper.get_vdm_real_value_tbl(0);
        assert_eq!(rv.get_size_for_key("Ethernet0"), 3);
        assert_eq!(
            rv.hget("Ethernet0", "esnr_media_input1"),
            Some("99.0".to_string())
        );
        assert_eq!(
            rv.hget("Ethernet0", "laser_temperature_media1"),
            Some("40".to_string())
        );
    }

    // The VDM statistic
    // freeze — and thus the statistic real-value capture + TRANSCEIVER_PM refresh — must be
    // SKIPPED while the module is in low power. This intentionally REVERSES the earlier
    // reality this test previously locked: no CmisManagerTask previously existed to drive an
    // admin-up module out of low power, so gating would have suppressed every capture;
    // the CmisManagerTask now drives an admin-up module to ModuleReady (lpmode == false), so
    // a normally-operating module still freezes (the companion assertion) while a module an
    // operator has put into low power stops refreshing its PM (test_dom_lpmode deletes the
    // PM row in low power and asserts it is not republished).
    #[test]
    fn test_poll_once_vdm_statistic_freeze_gated_by_low_power() {
        let make = || {
            dom_module()
                .with_json("is_transceiver_vdm_supported", json!(true))
                .with_json("is_vdm_statistic_supported", json!(true))
                .with_json("freeze_vdm_stats", json!(true))
                .with_json("get_vdm_freeze_status", json!(true))
                .with_json("unfreeze_vdm_stats", json!(true))
                .with_json("get_vdm_unfreeze_status", json!(true))
                .with_json(
                    "get_transceiver_vdm_real_value_basic",
                    json!({"esnr_media_input1": 10.0}),
                )
                .with_json(
                    "get_transceiver_vdm_real_value_statistic",
                    json!({"prefec_ber_min_media_input1": 1.0e-6}),
                )
                .with_json("get_transceiver_pm", json!({"prefec_ber_avg": "0.001"}))
                .with_json("get_transceiver_vdm_flags", json!({}))
        };

        // Low power → freeze gated: TRANSCEIVER_PM is not refreshed and the statistic-only
        // real value is not captured (only the basic real value is posted).
        let mut low = make();
        low.lpmode = true;
        let task = task_with(&[("Ethernet0", 0)], vec![low], true);
        task.poll_once(&AtomicBool::new(false));
        assert_eq!(
            task.table_helper
                .get_pm_tbl(0)
                .get_size_for_key("Ethernet0"),
            0,
            "TRANSCEIVER_PM must not refresh while the module is in low power"
        );
        let rv = task.table_helper.get_vdm_real_value_tbl(0);
        assert!(
            rv.hget("Ethernet0", "prefec_ber_min_media_input1")
                .is_none(),
            "the statistic real value must not be captured while the freeze is lpmode-gated"
        );
        assert!(
            rv.hget("Ethernet0", "esnr_media_input1").is_some(),
            "the basic real values still refresh (only the statistic freeze is gated)"
        );

        // Not in low power (lpmode defaults false) → freeze runs: PM captured + statistic
        // real value published.
        let task2 = task_with(&[("Ethernet0", 0)], vec![make()], true);
        task2.poll_once(&AtomicBool::new(false));
        assert_eq!(
            task2
                .table_helper
                .get_pm_tbl(0)
                .get_size_for_key("Ethernet0"),
            1,
            "TRANSCEIVER_PM refreshes on an admin-up, non-lpmode module"
        );
        assert!(
            task2
                .table_helper
                .get_vdm_real_value_tbl(0)
                .hget("Ethernet0", "prefec_ber_min_media_input1")
                .is_some(),
            "the statistic real value is captured when not in low power"
        );
    }

    // Faithful to the reference `DomInfoUpdateTask.task_worker` (dom_mgr.py:361-414): the
    // periodic pass re-reads and republishes the latched DOM/STATUS/VDM flag tables on
    // EVERY poll. There is no "poll hold" in the reference — a recent link-change flap adds
    // an off-cadence FAST re-read *on top of* the periodic one, it never suppresses it. So
    // even a just-flapped port has its byte-9 temp/vcc (and status/VDM) flags refreshed each
    // poll, which is exactly what lets a cleared alarm settle back to its `False` baseline
    // within one DOM cadence (guards e2e
    // test_dom_flag_meta::test_dom_flag_groups_temp_and_vcc, whose vcc/temp group must reach
    // the resting `False` state within T_DOM).
    #[test]
    fn test_periodic_poll_always_republishes_latched_flags() {
        // A VDM-supported module whose module temp/vcc group is currently MIXED: the
        // temp-high latch is RAISED (tempHAlarm true) while vcc rests (vccHAlarm false) —
        // exactly the atomic 00h:9 group the platform decodes. This is the strongest
        // discriminator for the multiport e2e: it proves the periodic poll republishes the WHOLE
        // group and does NOT drop the `false`-valued vccHAlarm key (the exact regression
        // that left the e2e vcc baseline unsettled).
        let sfp = vdm_module().with_json(
            "get_transceiver_dom_flags",
            json!({"tempHAlarm": true, "vccHAlarm": false}),
        );
        let task = task_with(&[("Ethernet0", 0)], vec![sfp], true);

        // A recent genuine flap schedules an off-cadence re-read, but it must NOT gate the
        // periodic poll's latched-flag publish.
        task.on_port_update_event(&appl_flap_event("Ethernet0", 0, "1"));
        task.poll_once(&AtomicBool::new(false));

        // Every latched-flag table is (re)published by the periodic pass regardless of the flap.
        assert_eq!(
            task.table_helper
                .get_dom_flag_tbl(0)
                .hget("Ethernet0", "tempHAlarm"),
            Some("True".to_string()),
            "periodic poll must always republish TRANSCEIVER_DOM_FLAG (reference has no hold)"
        );
        // The resting `false` half of the atomic temp/vcc group must ride the SAME publish
        // and reach STATE_DB as the string "False" — never dropped because it is falsey.
        assert_eq!(
            task.table_helper
                .get_dom_flag_tbl(0)
                .hget("Ethernet0", "vccHAlarm"),
            Some("False".to_string()),
            "periodic poll must republish the full temp/vcc group incl. the resting vccHAlarm"
        );
        assert!(
            task.table_helper
                .get_status_flag_tbl(0)
                .get("Ethernet0")
                .is_some(),
            "periodic poll must always republish TRANSCEIVER_STATUS_FLAG"
        );
        assert!(
            task.table_helper
                .get_vdm_flag_tbl(0, "halarm")
                .get_size_for_key("Ethernet0")
                > 0,
            "periodic poll must always republish TRANSCEIVER_VDM_*_FLAG"
        );
    }

    // Direct integration mirror of the FAILING e2e
    // test_dom_flag_meta::test_dom_flag_groups_temp_and_vcc baseline: drive the production
    // `poll_once` on a resting module and assert BOTH halves of the atomic 00h:9 temp/vcc
    // group reach TRANSCEIVER_DOM_FLAG as "False". Exercises the whole chain
    // (poll_once -> poll_port -> post_port_dom_flags_to_db -> db::post_flags_to_db) with a
    // `None` cache, so a regression that drops the resting vccHAlarm key anywhere on the
    // periodic publish path fails here at the unit layer.
    #[test]
    fn test_periodic_poll_publishes_full_temp_vcc_baseline_group() {
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], true);
        task.poll_once(&AtomicBool::new(false));

        let row: HashMap<String, String> = task
            .table_helper
            .get_dom_flag_tbl(0)
            .get("Ethernet0")
            .expect("DOM_FLAG row is published for a present module")
            .into_iter()
            .collect();
        assert_eq!(
            row.get("tempHAlarm").map(String::as_str),
            Some("False"),
            "tempHAlarm settles to its resting baseline"
        );
        assert_eq!(
            row.get("vccHAlarm").map(String::as_str),
            Some("False"),
            "vccHAlarm settles to its resting baseline in the SAME publish as tempHAlarm"
        );
    }

    // Per-module isolation for the DOM temp/vcc flag group: with all ports polled in one
    // `poll_once` pass, each port's TRANSCEIVER_DOM_FLAG row reflects ONLY its own module's
    // 00h:9 group — a raised alarm on one port never bleeds the group onto a resting
    // neighbour, and a resting neighbour never suppresses the raised port. Guards against
    // cross-talk / stale-flag leakage on the multiport publish path.
    #[test]
    fn test_multiport_dom_flag_groups_isolated_per_port() {
        // Ethernet0 rests (both False); Ethernet4 has BOTH temp+vcc raised.
        let resting = dom_module();
        let raised = dom_module().with_json(
            "get_transceiver_dom_flags",
            json!({"tempHAlarm": true, "vccHAlarm": true}),
        );
        let task = task_with(
            &[("Ethernet0", 0), ("Ethernet4", 1)],
            vec![resting, raised],
            true,
        );
        task.poll_once(&AtomicBool::new(false));

        let e0: HashMap<String, String> = task
            .table_helper
            .get_dom_flag_tbl(0)
            .get("Ethernet0")
            .expect("resting port publishes its DOM_FLAG row")
            .into_iter()
            .collect();
        assert_eq!(e0.get("tempHAlarm").map(String::as_str), Some("False"));
        assert_eq!(e0.get("vccHAlarm").map(String::as_str), Some("False"));

        let e4: HashMap<String, String> = task
            .table_helper
            .get_dom_flag_tbl(0)
            .get("Ethernet4")
            .expect("raised port publishes its DOM_FLAG row")
            .into_iter()
            .collect();
        assert_eq!(
            e4.get("tempHAlarm").map(String::as_str),
            Some("True"),
            "the raised neighbour keeps its own temp latch — no cross-port suppression"
        );
        assert_eq!(
            e4.get("vccHAlarm").map(String::as_str),
            Some("True"),
            "the raised neighbour keeps its own vcc latch — no cross-port suppression"
        );
    }

    // A genuine flap schedules an off-cadence flag re-read (the faithful part of the
    // link-change path we keep — dom_mgr.py on_port_update_event +
    // update_port_db_diagnostics_on_link_change): only an APPL_DB PORT_SET carrying a
    // CHANGED flap_count enqueues a pending re-read; a re-delivered/unchanged flap_count is
    // not a flap and enqueues nothing. The periodic poll never suppresses the flag publish.
    #[test]
    fn test_flap_schedules_offcadence_reread() {
        // A genuine APPL_DB flap enqueues a pending off-cadence re-read for the port.
        let task = task_with(&[("Ethernet0", 0)], vec![dom_module()], true);
        assert!(!task
            .link_change_affected_ports
            .lock()
            .unwrap()
            .contains_key(&0));
        task.on_port_update_event(&appl_flap_event("Ethernet0", 0, "1"));
        assert!(
            task.link_change_affected_ports
                .lock()
                .unwrap()
                .contains_key(&0),
            "a genuine flap schedules an off-cadence flag re-read"
        );

        // A re-delivered flap_count (not a genuine flap) schedules nothing.
        let task2 = task_with(&[("Ethernet8", 1)], vec![dom_module()], true);
        task2.seed_link_change_baseline(&appl_flap_event("Ethernet8", 1, "5"));
        task2.on_port_update_event(&appl_flap_event("Ethernet8", 1, "5"));
        assert!(
            !task2
                .link_change_affected_ports
                .lock()
                .unwrap()
                .contains_key(&1),
            "a re-delivered flap_count must not schedule an off-cadence re-read"
        );
    }

    // Direct integration mirror of the FULL
    // test_dom_flag_meta::test_dom_flag_groups_temp_and_vcc raise/clear cycle (the existing
    // test_periodic_poll_publishes_full_temp_vcc_baseline_group covers only its resting
    // baseline). A SHARED STATE_DB (`th`) is driven through three periodic polls whose module
    // presents the atomic 00h:9 temp/vcc group as, in turn, resting -> BOTH raised -> resting.
    // Each phase is a fresh MockHal (the mock's flag dict is fixed at construction) writing the
    // SAME TRANSCEIVER_DOM_FLAG row, exactly modelling the physical alarm rising then clearing
    // between polls. Asserts tempHAlarm AND vccHAlarm transition TOGETHER every time — the
    // group is never split and the resting `False` half is never dropped on any transition.
    #[test]
    fn test_dom_flag_group_raise_then_clear_cycle_temp_and_vcc() {
        let th = Arc::new(XcvrTableHelper::with_mock_tables(&[String::new()]));
        let stop = AtomicBool::new(false);
        let ports = &[("Ethernet0", 0)];

        let assert_group = |temp: &str, vcc: &str, phase: &str| {
            let row: HashMap<String, String> = th
                .get_dom_flag_tbl(0)
                .get("Ethernet0")
                .unwrap_or_else(|| panic!("DOM_FLAG row present in phase {phase}"))
                .into_iter()
                .collect();
            assert_eq!(
                row.get("tempHAlarm").map(String::as_str),
                Some(temp),
                "tempHAlarm in phase {phase}"
            );
            assert_eq!(
                row.get("vccHAlarm").map(String::as_str),
                Some(vcc),
                "vccHAlarm rides the SAME publish as tempHAlarm in phase {phase}"
            );
        };

        // Phase 1 — resting baseline: both halves settle to "False" together.
        task_sharing_th(
            ports,
            vec![dom_module_flags(false, false)],
            true,
            th.clone(),
        )
        .poll_once(&stop);
        assert_group("False", "False", "baseline");

        // Phase 2 — the module raises BOTH temp+vcc: the next poll republishes the whole
        // group as "True".
        task_sharing_th(ports, vec![dom_module_flags(true, true)], true, th.clone())
            .poll_once(&stop);
        assert_group("True", "True", "raised");

        // Phase 3 — the alarms clear: the next poll settles BOTH back to "False" together.
        task_sharing_th(
            ports,
            vec![dom_module_flags(false, false)],
            true,
            th.clone(),
        )
        .poll_once(&stop);
        assert_group("False", "False", "cleared");
    }

    // Deterministic mirror of the FAILING
    // test_link_change_flags::test_link_change_triggers_fast_flag_recapture steps 2-3 (the
    // pre-flap GUARD and the post-flap fast recapture). A SHARED STATE_DB carries the
    // post-flap `False` baseline (step 1, published by a resting module's flap re-read); then
    // the module's alarm rises to True (a fresh MockHal, since the mock's flags are fixed at
    // construction). GUARD (step 2): raising the flag WITHOUT a flap and running the FULL
    // inter-poll servicing (reconcile + inline republisher + check_port_update) must leave the
    // published baseline "False" — no seam the daemon controls may surface a no-flap alarm
    // inside the caller's guard window. FAST (step 3): a genuine APPL_DB flap schedules an
    // off-cadence re-read that, once due, republishes the now-raised group as "True" — the
    // fast recapture the e2e asserts. NB the *periodic poll* is deliberately NOT run in the
    // guard window: it legitimately WOULD surface the flag (the inherent ~8/60 poll-vs-guard
    // race a faithful 60 s-cadence daemon cannot avoid); the seams asserted here are the ones
    // the daemon CAN keep isolated (republish skips an intact row; reconcile dedups; no
    // pending re-read fires without a flap).
    #[test]
    fn test_link_change_guard_isolates_no_flap_then_flap_recaptures() {
        let th = Arc::new(XcvrTableHelper::with_mock_tables(&[String::new()]));
        let stop = AtomicBool::new(false);
        let ports = &[("Ethernet0", 0)];
        // Datapath genuinely settled so the flag gate stays OPEN throughout — the guard's
        // isolation must hold because of the seam logic, NOT because a closed CMIS gate
        // happens to suppress the publish.
        th.get_status_sw_tbl(0)
            .hset("Ethernet0", "cmis_state", "READY");

        // Step 1 — a post-flap re-read publishes the resting baseline: DOM_FLAG both "False".
        let baseline = task_sharing_th(
            ports,
            vec![dom_module_flags(false, false)],
            false,
            th.clone(),
        );
        assert!(matches!(
            baseline.update_port_db_diagnostics_on_link_change(0, &stop),
            LinkChangeReread::Settled
        ));
        assert_eq!(
            th.get_dom_flag_tbl(0).hget("Ethernet0", "tempHAlarm"),
            Some("False".to_string())
        );

        // The module now RAISES both temp+vcc (fresh HAL, SAME STATE_DB) — a physical alarm.
        let raised = task_sharing_th(ports, vec![dom_module_flags(true, true)], false, th.clone());

        // Step 2 GUARD — no flap occurred: none of the inter-poll seams may surface the alarm.
        raised.reconcile_link_change_flap_counts(); // no APPL flap_count → schedules nothing
        raised.republish_missing_flag_baseline_after_cmis_bringup(&stop); // row present → skip
        raised.check_port_update(&stop, None, 1); // nothing pending → no off-cadence re-read
        assert_eq!(
            th.get_dom_flag_tbl(0).hget("Ethernet0", "tempHAlarm"),
            Some("False".to_string()),
            "a raised alarm WITHOUT a flap must NOT surface via any inter-poll seam (e2e guard)"
        );
        assert_eq!(
            th.get_dom_flag_tbl(0).hget("Ethernet0", "vccHAlarm"),
            Some("False".to_string()),
            "the resting vcc half likewise stays baselined inside the guard window"
        );

        // Step 3 FAST — a genuine flap schedules an off-cadence re-read...
        raised.on_port_update_event(&appl_flap_event("Ethernet0", 0, "1"));
        assert!(
            raised
                .link_change_affected_ports
                .lock()
                .unwrap()
                .contains_key(&0),
            "a genuine flap schedules the fast re-read"
        );
        // ...compress its ~1 s debounce so the drain is deterministic, then service it exactly
        // as the wait loop / poll pass would.
        raised.link_change_affected_ports.lock().unwrap().insert(
            0,
            PendingReread {
                fire_at: Instant::now(),
                giveup_at: Instant::now() + Duration::from_secs(60),
            },
        );
        raised.check_port_update(&stop, None, 1);
        assert_eq!(
            th.get_dom_flag_tbl(0).hget("Ethernet0", "tempHAlarm"),
            Some("True".to_string()),
            "the post-flap fast re-read recaptures the now-raised temp latch (e2e fast recapture)"
        );
        assert_eq!(
            th.get_dom_flag_tbl(0).hget("Ethernet0", "vccHAlarm"),
            Some("True".to_string()),
            "and the whole atomic temp/vcc group is recaptured together on the flap re-read"
        );
    }

    // HARDENS test_link_change_flags::
    // test_link_change_triggers_fast_flag_recapture's pre-flap GUARD against the observer's
    // REAL event stream. The sibling guard test above services an EMPTY stream
    // (`check_port_update(.., None, ..)`); on the DUT the APPL_DB PORT_TABLE subscription also
    // re-delivers soaked PORT_SETs — a re-sent boot snapshot, or an unrelated PORT_TABLE write
    // — that still carry the CURRENT `flap_count`, plus PORT_SETs with no `flap_count` at all.
    // None of those is a genuine flap, so — with the module's alarm already RAISED — none may
    // schedule an off-cadence re-read nor surface the alarm inside the guard window; only a
    // real `flap_count` INCREMENT may. This locks `record_flap_transition`'s per-port dedup as
    // the seam that keeps the guard isolated from a chatty observer, not merely from silence
    // (the inherent ~8/60 periodic-poll-vs-guard race a faithful 60 s daemon cannot avoid is
    // out of scope here, exactly as in the sibling test).
    #[test]
    fn test_link_change_guard_holds_against_spurious_observer_events() {
        let th = Arc::new(XcvrTableHelper::with_mock_tables(&[String::new()]));
        let stop = AtomicBool::new(false);
        let ports = &[("Ethernet0", 0)];
        // Datapath settled so the flag gate stays OPEN — isolation must come from the dedup
        // seam, not a closed CMIS gate incidentally suppressing the publish.
        th.get_status_sw_tbl(0)
            .hset("Ethernet0", "cmis_state", "READY");
        // A boot `flap_count` is already in APPL_DB and folded into the daemon's dedup baseline
        // (observer initial snapshot / direct read), so RE-delivering it is not a flap.
        th.get_app_port_tbl(0).hset("Ethernet0", "flap_count", "7");

        // Step 1 — resting module: a post-flap re-read publishes DOM_FLAG both "False".
        let baseline = task_sharing_th(
            ports,
            vec![dom_module_flags(false, false)],
            false,
            th.clone(),
        );
        baseline.seed_flap_count_baseline_from_db();
        assert!(matches!(
            baseline.update_port_db_diagnostics_on_link_change(0, &stop),
            LinkChangeReread::Settled
        ));
        assert_eq!(
            th.get_dom_flag_tbl(0).hget("Ethernet0", "tempHAlarm"),
            Some("False".to_string())
        );

        // The module now RAISES both temp+vcc (fresh HAL, SAME STATE_DB). Re-seed the dedup
        // baseline from the same APPL_DB: the production daemon is one long-lived task whose
        // `last_flap_count` persists, whereas each phase here is a fresh task sharing only the
        // STATE_DB, so re-seeding reconstructs "flap_count 7 already seen".
        let raised = task_sharing_th(ports, vec![dom_module_flags(true, true)], false, th.clone());
        raised.seed_flap_count_baseline_from_db();

        // Step 2 GUARD — the observer re-delivers non-flaps: a soaked PORT_SET carrying the
        // SAME flap_count (7), then a PORT_SET with NO flap_count. Neither is a genuine flap.
        raised.on_port_update_event(&appl_flap_event("Ethernet0", 0, "7"));
        let no_flap = PortChangeEvent::new(
            "Ethernet0".to_string(),
            Some(0),
            0,
            PortChangeEventType::Set,
            "APPL_DB".to_string(),
            "PORT_TABLE".to_string(),
        )
        .with_port_dict(BTreeMap::from([(
            "admin_status".to_string(),
            "up".to_string(),
        )]));
        raised.on_port_update_event(&no_flap);
        raised.reconcile_link_change_flap_counts(); // APPL flap_count still 7 → dedup
        raised.republish_missing_flag_baseline_after_cmis_bringup(&stop); // row present → skip
        raised.check_port_update(&stop, None, 1); // nothing pending → no off-cadence re-read

        assert!(
            raised.link_change_affected_ports.lock().unwrap().is_empty(),
            "no spurious observer event may schedule an off-cadence re-read inside the guard"
        );
        assert_eq!(
            th.get_dom_flag_tbl(0).hget("Ethernet0", "tempHAlarm"),
            Some("False".to_string()),
            "a raised alarm must stay baselined 'False' under a chatty (non-flap) observer"
        );
        assert_eq!(
            th.get_dom_flag_tbl(0).hget("Ethernet0", "vccHAlarm"),
            Some("False".to_string()),
            "the vcc half likewise stays baselined inside the guard window"
        );

        // Step 3 — a GENUINE flap_count increment (7 → 8) DOES schedule the fast re-read, so
        // the dedup suppresses only re-deliveries, never a real flap.
        th.get_app_port_tbl(0).hset("Ethernet0", "flap_count", "8");
        raised.reconcile_link_change_flap_counts();
        assert!(
            raised
                .link_change_affected_ports
                .lock()
                .unwrap()
                .contains_key(&0),
            "a real flap_count increment must still schedule the fast re-read"
        );
    }

    // A scheduled off-cadence re-read whose module DOM-flag read TRANSIENTLY
    // yields nothing (a gRPC/EEPROM hiccup on the emulator testbed — modelled here as an empty
    // `{}` decode) publishes no row this attempt. Because this daemon detects flaps
    // notification-independently (reconcile), such a re-read can fire before the just-plugged
    // module answers; dropping it after one attempt silently loses the flap's flag re-capture
    // when the TRANSCEIVER_DOM_FLAG row already exists (the reported e2e defect). So
    // check_port_update RE-ARMS a `TransientRead` re-read (bounded by giveup_at) instead of
    // dropping it — it retries until the module answers and Settles. Past the give-up bound it
    // IS dropped, so a wedged module cannot pin a re-read forever. Asserts: (1) the re-read
    // reports `TransientRead` and publishes NO row, (2) check_port_update RE-ARMS the entry
    // while within giveup_at, and (3) DROPS it past giveup_at.
    #[test]
    fn test_link_change_transient_dom_read_rearmed_until_giveup() {
        let th = Arc::new(XcvrTableHelper::with_mock_tables(&[String::new()]));
        let stop = AtomicBool::new(false);
        let ports = &[("Ethernet0", 0)];
        // Datapath settled so the flag gate stays OPEN — a `TransientRead` here is caused by
        // the empty decode, NOT by a closed CMIS gate (which would be `DeferredCmisInit`).
        th.get_status_sw_tbl(0)
            .hset("Ethernet0", "cmis_state", "READY");

        // The scheduled re-read fires but the module returns an EMPTY dom-flags decode.
        let transient = task_sharing_th(
            ports,
            vec![dom_module().with_json("get_transceiver_dom_flags", json!({}))],
            false,
            th.clone(),
        );
        assert!(
            matches!(
                transient.update_port_db_diagnostics_on_link_change(0, &stop),
                LinkChangeReread::TransientRead
            ),
            "an empty DOM-flag decode on the off-cadence re-read must report TransientRead"
        );
        assert_eq!(
            th.get_dom_flag_tbl(0).hget("Ethernet0", "tempHAlarm"),
            None,
            "a transient empty DOM read publishes no flag row"
        );

        // (2) A due entry within its give-up bound is RE-ARMED (not dropped) so the flap's flag
        // re-capture survives until the module answers.
        transient.link_change_affected_ports.lock().unwrap().insert(
            0,
            PendingReread {
                fire_at: Instant::now(),
                giveup_at: Instant::now() + Duration::from_secs(60),
            },
        );
        transient.check_port_update(&stop, None, 1);
        assert!(
            transient
                .link_change_affected_ports
                .lock()
                .unwrap()
                .contains_key(&0),
            "a transient DOM read within giveup_at is re-armed, not dropped"
        );

        // (3) Past the give-up bound the re-arm IS dropped — a wedged module cannot pin a
        // re-read forever; the republish hook (row-missing) then owns baseline recovery.
        transient.link_change_affected_ports.lock().unwrap().insert(
            0,
            PendingReread {
                fire_at: Instant::now(),
                giveup_at: Instant::now() - Duration::from_secs(1),
            },
        );
        transient.check_port_update(&stop, None, 1);
        assert!(
            transient
                .link_change_affected_ports
                .lock()
                .unwrap()
                .is_empty(),
            "past giveup_at a transient DOM read is dropped, not re-armed forever"
        );
    }

    // The FAITHFUL replacement for the removed re-arm's retry purpose: the isolation
    // cold-boot baseline for a still-MISSING TRANSCEIVER_DOM_FLAG row is (re)established by the
    // republish hook, which retries on its ~1 s cadence while the row stays missing. A transient
    // empty decode publishes nothing that cycle; once the module answers, the very next hook
    // cycle publishes the baseline — no dependency on a lingering per-flap re-read.
    #[test]
    fn test_republish_retries_transient_dom_read_until_module_answers() {
        let th = Arc::new(XcvrTableHelper::with_mock_tables(&[String::new()]));
        let stop = AtomicBool::new(false);
        let ports = &[("Ethernet0", 0)];
        // Terminal datapath + missing row → the republish hook is eligible.
        th.get_status_sw_tbl(0)
            .hset("Ethernet0", "cmis_state", "READY");

        // Cycle 1: module read transiently empty → hook publishes nothing, row stays missing.
        let transient = task_sharing_th(
            ports,
            vec![dom_module().with_json("get_transceiver_dom_flags", json!({}))],
            false,
            th.clone(),
        );
        transient.republish_missing_flag_baseline_after_cmis_bringup(&stop);
        assert_eq!(
            th.get_dom_flag_tbl(0).get_size_for_key("Ethernet0"),
            0,
            "a transient empty read leaves the baseline row missing for the next hook cycle"
        );

        // Cycle 2: module now answers (both alarms resting False) → the hook establishes the
        // baseline, exactly as the reference's flap re-read would once the read succeeds.
        let answered = task_sharing_th(
            ports,
            vec![dom_module_flags(false, false)],
            false,
            th.clone(),
        );
        answered.republish_missing_flag_baseline_after_cmis_bringup(&stop);
        assert_eq!(
            th.get_dom_flag_tbl(0).hget("Ethernet0", "tempHAlarm"),
            Some("False".to_string()),
            "once the module answers, the republish hook establishes the missing baseline"
        );
    }

    // ---- concurrent per-port DOM isolation (e2e mirror: test_multiport::
    //      test_concurrent_dom_no_crosstalk). Each logical port maps to its OWN physical
    //      index -> its OWN SfpHandle -> its OWN TRANSCEIVER_DOM_SENSOR|<port> row. A single
    //      poll pass that reads distinct per-module temperatures must publish EACH port's own
    //      value under EACH port's own key — no shared/mis-keyed physical-port index that would
    //      cross-publish one port's DOM sensor under another (the reported crosstalk symptom).
    #[test]
    fn test_concurrent_dom_no_crosstalk_per_port_isolation() {
        let ports = &[
            ("Ethernet0", 0),
            ("Ethernet8", 1),
            ("Ethernet16", 2),
            ("Ethernet24", 3),
        ];
        // Distinct per-port temperatures — each module reports only its own value.
        let temps = ["30.5", "35.5", "40.5", "45.5"];
        let sfps: Vec<MockSfp> = temps
            .iter()
            .map(|t| {
                let mut dom = full_dom_real_value();
                dom["temperature"] = json!(format!("{t}C"));
                dom_module().with_dom_real_value(dom)
            })
            .collect();

        let task = task_with(ports, sfps, true);
        task.poll_once(&AtomicBool::new(false));

        // Every port's row carries ITS OWN temperature — no value published under the wrong key.
        for ((lport, _), want) in ports.iter().zip(temps.iter()) {
            let sensor: HashMap<String, String> = task
                .table_helper
                .get_dom_tbl(0)
                .get(lport)
                .unwrap_or_else(|| panic!("{lport}: DOM_SENSOR row must be published"))
                .into_iter()
                .collect();
            assert_eq!(
                sensor.get("temperature").map(String::as_str),
                Some(*want),
                "{lport}: TRANSCEIVER_DOM_SENSOR temperature must be this port's own value \
                 (no crosstalk from a shared/mis-keyed physical-port index)"
            );
        }
    }

    // ← tests/test_xcvrd.py::test_DomInfoUpdateTask_handle_port_change_event
    //
    // A runtime CONFIG_DB PORT add is added to the DOM poll mapping (so the next poll pass
    // walks it) and a PORT_DEL both drops it from the mapping AND tears down the port's
    // DOM-owned TRANSCEIVER_* rows (del_port_sfp_dom_info_from_db). The add must NOT delete
    // anything (the Python test's `call_count == 0`); only the remove does (`== 1`).
    #[test]
    fn test_on_port_config_change_add_then_remove_syncs_mapping_and_deletes_rows() {
        let mut task = task_with(&[], vec![dom_module()], true);

        // Pre-seed a representative slice of the DOM-owned tables for Ethernet0, plus two
        // tables the DOM removal MUST NOT touch (owned by SfpStateUpdateTask):
        // TRANSCEIVER_INFO and TRANSCEIVER_DOM_THRESHOLD.
        task.table_helper.get_dom_tbl(0).set(
            "Ethernet0",
            &[("temperature".to_string(), "42.5".to_string())],
        );
        task.table_helper.get_status_tbl(0).set(
            "Ethernet0",
            &[("module_state".to_string(), "ModuleReady".to_string())],
        );
        task.table_helper.get_pm_tbl(0).set(
            "Ethernet0",
            &[("prefec_ber_avg".to_string(), "0.0".to_string())],
        );
        task.table_helper.get_firmware_info_tbl(0).set(
            "Ethernet0",
            &[("active_firmware".to_string(), "1.0".to_string())],
        );
        task.table_helper.get_vdm_real_value_tbl(0).set(
            "Ethernet0",
            &[("laser_temperature_media1".to_string(), "38".to_string())],
        );
        task.table_helper.get_vdm_flag_tbl(0, "halarm").set(
            "Ethernet0",
            &[("x_halarm".to_string(), "False".to_string())],
        );
        // NOT DOM-owned — must survive the remove.
        task.table_helper
            .get_intf_tbl(0)
            .set("Ethernet0", &[("type".to_string(), "QSFP-DD".to_string())]);
        task.table_helper.get_dom_threshold_tbl(0).set(
            "Ethernet0",
            &[("temphighalarm".to_string(), "75.0".to_string())],
        );

        // PORT_ADD (physical index 1, matching the Python test): mapping gains Ethernet0.
        let add = PortChangeEvent::new(
            "Ethernet0".to_string(),
            Some(1),
            0,
            PortChangeEventType::Add,
            "CONFIG_DB".to_string(),
            "PORT".to_string(),
        );
        task.on_port_config_change(&add);
        assert!(task
            .port_mapping
            .logical_port_list()
            .contains(&"Ethernet0".to_string()));
        assert_eq!(
            task.port_mapping.get_asic_id_for_logical_port("Ethernet0"),
            Some(0)
        );
        assert_eq!(
            task.port_mapping.get_physical_to_logical(1),
            Some(vec!["Ethernet0".to_string()])
        );
        assert_eq!(
            task.port_mapping.get_logical_to_physical("Ethernet0"),
            Some(vec![1])
        );
        // The add must NOT delete any row (del_port_sfp_dom_info_from_db.call_count == 0).
        assert_eq!(
            task.table_helper
                .get_dom_tbl(0)
                .get_size_for_key("Ethernet0"),
            1,
            "PORT_ADD must not delete the DOM sensor row"
        );

        // PORT_REMOVE: mapping fully cleared AND DOM-owned rows deleted; INFO/THRESHOLD kept.
        let remove = PortChangeEvent::new(
            "Ethernet0".to_string(),
            Some(1),
            0,
            PortChangeEventType::Remove,
            "CONFIG_DB".to_string(),
            "PORT".to_string(),
        );
        task.on_port_config_change(&remove);
        assert!(task.port_mapping.logical_port_list().is_empty());
        assert_eq!(
            task.port_mapping.get_asic_id_for_logical_port("Ethernet0"),
            None
        );
        assert_eq!(task.port_mapping.get_physical_to_logical(1), None);
        assert_eq!(task.port_mapping.get_logical_to_physical("Ethernet0"), None);

        // del_port_sfp_dom_info_from_db tore down every DOM-owned table row.
        let dom_owned: [&dyn DbTable; 6] = [
            task.table_helper.get_dom_tbl(0),
            task.table_helper.get_status_tbl(0),
            task.table_helper.get_pm_tbl(0),
            task.table_helper.get_firmware_info_tbl(0),
            task.table_helper.get_vdm_real_value_tbl(0),
            task.table_helper.get_vdm_flag_tbl(0, "halarm"),
        ];
        for tbl in dom_owned {
            assert_eq!(
                tbl.get_size_for_key("Ethernet0"),
                0,
                "PORT_REMOVE must delete the port's DOM-owned rows"
            );
        }
        // INFO + DOM_THRESHOLD are owned by SfpStateUpdateTask — the DOM removal leaves them.
        assert_eq!(
            task.table_helper
                .get_intf_tbl(0)
                .get_size_for_key("Ethernet0"),
            1,
            "DOM removal must not touch TRANSCEIVER_INFO (owned by SfpStateUpdateTask)"
        );
        assert_eq!(
            task.table_helper
                .get_dom_threshold_tbl(0)
                .get_size_for_key("Ethernet0"),
            1,
            "DOM removal must not touch TRANSCEIVER_DOM_THRESHOLD (owned by SfpStateUpdateTask)"
        );
    }

    // A runtime-added logical port must actually flow through the periodic DOM poll pass:
    // after on_port_config_change adds Ethernet0 to the mapping, poll_once walks the
    // freshly-added port and publishes its DOM sensor row (the mapping sync is what puts a
    // runtime port into the DOM poll set).
    #[test]
    fn test_on_port_config_change_add_enters_dom_poll_set() {
        let mut task = task_with(&[], vec![dom_module()], true);
        // Empty mapping → a poll publishes nothing.
        task.poll_once(&AtomicBool::new(false));
        assert_eq!(
            task.table_helper
                .get_dom_tbl(0)
                .get_size_for_key("Ethernet0"),
            0,
            "with no mapped ports the poll pass publishes nothing"
        );

        // Runtime add (physical index 0 → the mock HAL's single SFP) enters the poll set.
        let add = PortChangeEvent::new(
            "Ethernet0".to_string(),
            Some(0),
            0,
            PortChangeEventType::Add,
            "CONFIG_DB".to_string(),
            "PORT".to_string(),
        );
        task.on_port_config_change(&add);
        task.poll_once(&AtomicBool::new(false));
        assert_eq!(
            task.table_helper
                .get_dom_tbl(0)
                .get("Ethernet0")
                .and_then(|r| r
                    .into_iter()
                    .find(|(k, _)| k == "temperature")
                    .map(|(_, v)| v))
                .as_deref(),
            Some("42.5"),
            "a runtime-added logical port must be polled on the next DOM pass"
        );
    }
}
