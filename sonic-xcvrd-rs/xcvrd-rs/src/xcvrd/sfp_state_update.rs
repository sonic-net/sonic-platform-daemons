//! `xcvrd.py :: SfpStateUpdateTask` → the presence/identity + plug/unplug/error state
//! machine (analysis §1.3, §3.2).
//!
//! A 3-state machine (`STATE_INIT`/`STATE_NORMAL`/`STATE_EXIT`) driven by
//! `get_change_event`: on a `NORMAL_EVENT` it dispatches per physical-port code —
//! `"1"` insert (publish INFO + `STATUS_SW`), `"0"` remove (delete every
//! `TRANSCEIVER_*` row), else an error bitmap (decode → `STATUS_SW.error`).
//!
//! Implements presence/identity/retry/stale + the error branch (which only needs
//! the three trivial `sfp_status_helper` bit decoders). DOM/VDM threshold posting +
//! media-setting notify (the `rc != SFP_EEPROM_NOT_READY` follow-ups) land later.
//! A plug-out drops the plugin's cached `xcvr_api` (`remove_xcvr_api`) so a re-insert
//! re-reads the module EEPROM fresh; the NPU-Si `state_port` sync lands later.
//! `cmis_state` is owned exclusively by [`crate::cmis::cmis_manager_task::CmisManagerTask`]
//! (mirroring `cmis_manager_task.py`, the sole writer in the reference) — this task never
//! writes it, so a boot/plug status update cannot clobber an in-progress CMIS bring-up.
//!
//! Testability seam: the reference `task_worker` calls `self.init()` at entry; here
//! [`SfpStateUpdateTask::init`] is a separate method the production `serve()` runs
//! before [`SfpStateUpdateTask::task_worker`], so the state-machine loop can be unit
//! tested in isolation by injecting crafted change events through the mock HAL.
#![allow(dead_code, unused_variables, unused_imports)]

use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::cmis::cmis_api::{BridgeCmisApi, CmisApi};
use crate::cmis::cmis_manager_task::CMIS_MODULE_TYPES;
use crate::db::DbTable;
use crate::dom::utilities::db::DbCache;
use crate::dom::utilities::dom_sensor::DomDbUtils;
use crate::dom::utilities::vdm::{VdmDbUtils, VdmThresholdCache};
use crate::hal::Hal;
use crate::xcvrd::{build_transceiver_dict, post_port_sfp_info_to_db, SFP_EEPROM_NOT_READY};
use crate::xcvrd_utilities::common::{
    del_port_sfp_dom_info_from_db, update_port_transceiver_status_table_sw, wrapper_get_presence,
};
use crate::xcvrd_utilities::media_settings_parser::{
    self, get_media_settings_key, MediaSettingsKey, PortMediaResolver,
};
use crate::xcvrd_utilities::port_event_helper::{
    read_port_config_change, PortChangeEvent, PortChangeEventType, PortConfigChangeSubscriber,
    PortMapping,
};
use crate::xcvrd_utilities::sfp_status_helper::{
    fetch_generic_error_description, has_vendor_specific_error, is_error_block_eeprom_reading,
    SFP_STATUS_INSERTED, SFP_STATUS_REMOVED,
};
use crate::xcvrd_utilities::xcvr_table_helper::{
    XcvrTableHelper, NPU_SI_SETTINGS_DEFAULT_VALUE, NPU_SI_SETTINGS_SYNC_STATUS_KEY,
    VDM_THRESHOLD_TYPES,
};

// --- event codes (xcvrd.py) -------------------------------------------------------
pub const EVENT_ON_ALL_SFP: &str = "-1";
pub const SYSTEM_NOT_READY: &str = "system_not_ready";
pub const SYSTEM_BECOME_READY: &str = "system_become_ready";
pub const SYSTEM_FAIL: &str = "system_fail";
pub const NORMAL_EVENT: &str = "normal";

// --- state machine constants (xcvrd.py) -------------------------------------------
const STATE_INIT: u8 = 0;
const STATE_NORMAL: u8 = 1;
const STATE_EXIT: u8 = 2;
const RETRY_PERIOD_FOR_SYSTEM_READY_MSECS: u64 = 5000;
const RETRY_TIMES_FOR_SYSTEM_READY: u32 = 24;
const RETRY_PERIOD_FOR_SYSTEM_FAIL_MSECS: u64 = 5000;
const RETRY_TIMES_FOR_SYSTEM_FAIL: u32 = 24;
const STATE_MACHINE_UPDATE_PERIOD_MSECS: u64 = 60000;
const SFP_INSERT_EVENT_POLL_PERIOD_MSECS: u64 = 1000;
/// `MGMT_INIT_TIME_DELAY_SECS` (xcvrd.py:58) — a fresh SFP insert is soaked (withheld from
/// processing) this long so a module has time to complete its management init before the
/// daemon reads it. Threaded unit tests zero this via [`SfpStateUpdateTask::set_fast_timing`].
pub const MGMT_INIT_TIME_DELAY_SECS: u64 = 2;
/// `port_event_helper.SELECT_TIMEOUT_MSECS` — the CONFIG_DB `PORT` config-change select
/// blocks up to this each loop, mirroring `handle_port_config_change`. Also used to CAP
/// the `get_change_event` idle block so the loop re-checks `stop` at least this often —
/// the reference interrupts the blocking read via `raise_exception()` on shutdown, which
/// Rust cannot do across threads, so a bounded poll keeps the daemon responsive to a
/// SIGTERM/SIGINT (graceful `deinit` on a normal shutdown) without changing what a change
/// event reports (the emulator's `get_change_event` already returns early on any change).
const SELECT_TIMEOUT_MSECS: u64 = 1000;

/// The change-event outcomes that drive the `STATE_INIT/NORMAL/EXIT` machine.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SystemEvent {
    SystemNotReady,
    SystemBecomeReady,
    Normal,
    SystemFail,
}

impl SystemEvent {
    pub fn as_str(&self) -> &'static str {
        match self {
            SystemEvent::SystemNotReady => SYSTEM_NOT_READY,
            SystemEvent::SystemBecomeReady => SYSTEM_BECOME_READY,
            SystemEvent::Normal => NORMAL_EVENT,
            SystemEvent::SystemFail => SYSTEM_FAIL,
        }
    }

    /// Parse a system-level `EVENT_ON_ALL_SFP` payload back into an event.
    pub fn from_code(code: &str) -> SystemEvent {
        match code {
            SYSTEM_NOT_READY => SystemEvent::SystemNotReady,
            SYSTEM_BECOME_READY => SystemEvent::SystemBecomeReady,
            NORMAL_EVENT => SystemEvent::Normal,
            _ => SystemEvent::SystemFail,
        }
    }
}

/// `SfpStateUpdateTask` — presence/identity engine + change-event state machine.
pub struct SfpStateUpdateTask {
    namespaces: Vec<String>,
    /// The task's own deep copy of the port mapping (Python `copy.deepcopy`).
    port_mapping: PortMapping,
    hal: Arc<dyn Hal>,
    table_helper: Arc<XcvrTableHelper>,
    /// Logical ports whose identity EEPROM read failed — retried on the 60 s cadence.
    retry_eeprom_set: BTreeSet<String>,
    /// Last retry sweep time; `None` == never (retry immediately, Python's `0`).
    last_retry_eeprom_time: Option<Instant>,
    /// Cached SFP error events keyed by physical-port string (Python `sfp_error_dict`).
    sfp_error_dict: BTreeMap<String, String>,
    /// Pending soaked insert events (Python `sfp_insert_events`): physical-port key →
    /// the [`Instant`] the insert was first seen. An insert is withheld here for
    /// `mgmt_init_time_delay` and re-injected once elapsed; a remove in the window cancels it.
    sfp_insert_events: BTreeMap<String, Instant>,
    /// Post-insert EEPROM settle delay (`TIME_FOR_SFP_READY_SECS`); 0 in tests.
    time_for_sfp_ready: Duration,
    /// SFP insert-event soak delay (`MGMT_INIT_TIME_DELAY_SECS`, 2 s): a fresh insert is
    /// withheld this long before it is acted on. Zeroed in threaded tests via
    /// [`SfpStateUpdateTask::set_fast_timing`] so an insert passes straight through.
    mgmt_init_time_delay: Duration,
    retry_period_ready_ms: u64,
    retry_times_ready: u32,
    retry_period_fail_ms: u64,
    retry_times_fail: u32,
    /// Count of `STATE_EXIT` transitions — the observable stand-in for the reference's
    /// `os.kill(getppid(), SIGTERM)` (which asks the supervisor to shut the daemon
    /// down). Asserted by the EXIT-path unit test.
    parent_kill_count: usize,
    /// ASIC-side media SerDes settings parsed from `media_settings.json` (empty object
    /// when the platform ships no such file). Seeded once at daemon startup via
    /// [`SfpStateUpdateTask::set_media_settings`]; consulted by `notify_media_setting`
    /// on every post-info bring-up. Mirrors the Python module-global `g_media_settings_dict`.
    media_settings: Value,
}

impl SfpStateUpdateTask {
    /// `RETRY_EEPROM_READING_INTERVAL` (60 s cadence).
    pub const RETRY_EEPROM_READING_INTERVAL: Duration = Duration::from_secs(60);
    /// `TIME_FOR_SFP_READY_SECS` soak between the two post-insert read attempts.
    pub const TIME_FOR_SFP_READY_SECS: Duration = Duration::from_secs(1);

    pub fn new(
        namespaces: Vec<String>,
        port_mapping: PortMapping,
        hal: Arc<dyn Hal>,
        table_helper: Arc<XcvrTableHelper>,
    ) -> Self {
        SfpStateUpdateTask {
            namespaces,
            port_mapping,
            hal,
            table_helper,
            retry_eeprom_set: BTreeSet::new(),
            last_retry_eeprom_time: None,
            sfp_error_dict: BTreeMap::new(),
            sfp_insert_events: BTreeMap::new(),
            time_for_sfp_ready: Self::TIME_FOR_SFP_READY_SECS,
            mgmt_init_time_delay: Duration::from_secs(MGMT_INIT_TIME_DELAY_SECS),
            retry_period_ready_ms: RETRY_PERIOD_FOR_SYSTEM_READY_MSECS,
            retry_times_ready: RETRY_TIMES_FOR_SYSTEM_READY,
            retry_period_fail_ms: RETRY_PERIOD_FOR_SYSTEM_FAIL_MSECS,
            retry_times_fail: RETRY_TIMES_FOR_SYSTEM_FAIL,
            parent_kill_count: 0,
            media_settings: json!({}),
        }
    }

    /// Seed the ASIC-side media SI settings (parsed from `media_settings.json`). Called
    /// once at daemon startup; mirrors the Python module-global `g_media_settings_dict`.
    pub fn set_media_settings(&mut self, settings: Value) {
        self.media_settings = settings;
    }

    /// `_mapping_event_from_change_event` — collapse the raw change-event into a
    /// [`SystemEvent`], mutating `port_dict` exactly like the reference (a `status`
    /// timeout with an empty dict is turned into a `SYSTEM_BECOME_READY` all-SFP entry;
    /// a `!status` with no all-SFP key becomes a protective `SYSTEM_FAIL`).
    pub fn mapping_event_from_change_event(
        &self,
        status: bool,
        port_dict: &mut BTreeMap<String, String>,
    ) -> SystemEvent {
        if status {
            if !port_dict.is_empty() {
                SystemEvent::Normal
            } else {
                port_dict.insert(
                    EVENT_ON_ALL_SFP.to_string(),
                    SYSTEM_BECOME_READY.to_string(),
                );
                SystemEvent::SystemBecomeReady
            }
        } else if let Some(code) = port_dict.get(EVENT_ON_ALL_SFP) {
            SystemEvent::from_code(code)
        } else {
            port_dict.insert(EVENT_ON_ALL_SFP.to_string(), SYSTEM_FAIL.to_string());
            SystemEvent::SystemFail
        }
    }

    /// `_post_port_sfp_info_and_dom_thr_to_db_once` — boot-time INFO publish (DOM/VDM
    /// thresholds land later). Records ports whose EEPROM was not ready into the
    /// retry set. Honors the stop flag between ports.
    pub fn post_port_sfp_info_and_dom_thr_to_db_once(&mut self, stop: &AtomicBool) {
        let hal = self.hal.clone();
        let th = self.table_helper.clone();
        // Pre-fetch the per-ASIC warm-start status once (xcvrd.py:314-317): a warm reboot
        // suppresses the boot media SI notify so an in-service datapath is not flapped.
        let warm_start: Vec<bool> = (0..self.namespaces.len())
            .map(|a| th.is_syncd_warm_restore_complete(a))
            .collect();
        let mut retry_eeprom_set = BTreeSet::new();
        for logical_port_name in self.port_mapping.logical_port_list() {
            if stop.load(Ordering::Relaxed) {
                break;
            }
            let Some(asic_index) = self
                .port_mapping
                .get_asic_id_for_logical_port(logical_port_name)
            else {
                continue;
            };
            let rc = post_port_sfp_info_to_db(
                logical_port_name,
                &self.port_mapping,
                th.get_intf_tbl(asic_index),
                hal.as_ref(),
            );
            if rc == SFP_EEPROM_NOT_READY {
                retry_eeprom_set.insert(logical_port_name.to_string());
            } else if !warm_start.get(asic_index).copied().unwrap_or(false) {
                // Publish the ASIC-side media SI + stamp NPU_SI_SETTINGS_NOTIFIED for a
                // module already present at boot (xcvrd.py:336-338).
                notify_media_setting_for_logical_port(
                    logical_port_name,
                    &self.port_mapping,
                    hal.as_ref(),
                    th.as_ref(),
                    &self.media_settings,
                );
            }
        }
        self.retry_eeprom_set = retry_eeprom_set;

        // Second pass: seed the decoded page-02h DOM thresholds *and* the VDM thresholds
        // for every port whose identity EEPROM read succeeded (mirrors
        // `_post_port_sfp_info_and_dom_thr_to_db_once`; xcvrd.py:349-351). Shared per-pass
        // caches avoid re-reading the same module once per breakout subport. Thresholds
        // are *not* refreshed by the DOM poll loop — they are cached here, once, at boot.
        let dom_db = DomDbUtils::new();
        let vdm_db = VdmDbUtils::new();
        let mut dom_thresholds_cache: DbCache = DbCache::new();
        let mut vdm_thresholds_cache: VdmThresholdCache = VdmThresholdCache::new();
        for logical_port_name in self.port_mapping.logical_port_list() {
            if stop.load(Ordering::Relaxed) {
                break;
            }
            if self.retry_eeprom_set.contains(logical_port_name) {
                continue;
            }
            let Some(asic_index) = self
                .port_mapping
                .get_asic_id_for_logical_port(logical_port_name)
            else {
                continue;
            };
            dom_db.post_port_dom_thresholds_to_db(
                stop,
                logical_port_name,
                &self.port_mapping,
                th.get_dom_threshold_tbl(asic_index),
                hal.as_ref(),
                Some(&mut dom_thresholds_cache),
            );
            vdm_db.post_port_vdm_thresholds_to_db(
                stop,
                logical_port_name,
                &self.port_mapping,
                hal.as_ref(),
                th.as_ref(),
                Some(&mut vdm_thresholds_cache),
            );
        }
    }

    /// `_init_port_sfp_status_sw_tbl` — seed `TRANSCEIVER_STATUS_SW`
    /// `status`=`1`/`0` + `error`=`N/A`. `cmis_state` is *not* written here: the
    /// `CmisManagerTask` is its sole owner (matching xcvrd.py), so this boot seed must
    /// not race/clobber the CMIS bring-up state machine.
    pub fn init_port_sfp_status_sw_tbl(&self, stop: &AtomicBool) {
        let th = self.table_helper.clone();
        let hal = self.hal.clone();
        for logical_port_name in self.port_mapping.logical_port_list() {
            if stop.load(Ordering::Relaxed) {
                break;
            }
            let Some(asic_index) = self
                .port_mapping
                .get_asic_id_for_logical_port(logical_port_name)
            else {
                continue;
            };
            let status_sw_tbl = th.get_status_sw_tbl(asic_index);

            let Some(physical_port_list) = self
                .port_mapping
                .logical_port_name_to_physical_port_list(logical_port_name)
            else {
                update_port_transceiver_status_table_sw(
                    logical_port_name,
                    status_sw_tbl,
                    SFP_STATUS_REMOVED,
                    "N/A",
                );
                continue;
            };

            for physical_port in physical_port_list {
                if stop.load(Ordering::Relaxed) {
                    break;
                }
                let present = hal
                    .sfp(physical_port)
                    .ok()
                    .map(|sfp| wrapper_get_presence(sfp.as_ref()).unwrap_or(false))
                    .unwrap_or(false);
                if present {
                    update_port_transceiver_status_table_sw(
                        logical_port_name,
                        status_sw_tbl,
                        SFP_STATUS_INSERTED,
                        "N/A",
                    );
                } else {
                    update_port_transceiver_status_table_sw(
                        logical_port_name,
                        status_sw_tbl,
                        SFP_STATUS_REMOVED,
                        "N/A",
                    );
                }
            }
        }
    }

    /// `init` — the reference `task_worker` boot sequence (post INFO once, then seed
    /// the STATUS_SW table). Called by the production `serve()` before `task_worker`.
    pub fn init(&mut self, stop: &AtomicBool) {
        self.initialize_port_init_control_fields_in_port_table();
        self.post_port_sfp_info_and_dom_thr_to_db_once(stop);
        self.init_port_sfp_status_sw_tbl(stop);
    }

    /// `initialize_port_init_control_fields_in_port_table` (xcvrd.py:941) — seed STATE_DB
    /// `PORT_TABLE|<lport>.NPU_SI_SETTINGS_SYNC_STATUS = NPU_SI_SETTINGS_DEFAULT` for every
    /// logical port whose row does not already carry the field. Only-if-absent so a
    /// `NOTIFIED` value that survived a daemon restart is preserved (the idempotency guard
    /// stays honoured across restarts).
    fn initialize_port_init_control_fields_in_port_table(&self) {
        for lport in self.port_mapping.logical_port_list() {
            let asic_index = self
                .port_mapping
                .get_asic_id_for_logical_port(lport)
                .unwrap_or(0);
            let state_port_tbl = self.table_helper.get_state_port_tbl(asic_index);
            if state_port_tbl
                .hget(lport, NPU_SI_SETTINGS_SYNC_STATUS_KEY)
                .is_none()
            {
                state_port_tbl.set(
                    lport,
                    &[(
                        NPU_SI_SETTINGS_SYNC_STATUS_KEY.to_string(),
                        NPU_SI_SETTINGS_DEFAULT_VALUE.to_string(),
                    )],
                );
            }
        }
    }

    /// `retry_eeprom_reading` — re-attempt a failed identity read on the 60 s cadence;
    /// on success publish INFO, seed the port's DOM + VDM thresholds, notify media SI, and
    /// drop the port from the retry set. `cmis_state` is left to the `CmisManagerTask` (its
    /// sole owner), so a recovered read cannot clobber CMIS.
    ///
    /// The DOM/VDM threshold seeding here mirrors the insert path (`handle_sfp_insert` /
    /// `on_add_logical_port`) and the reference `xcvrd.py:856-858`: page-02h DOM thresholds
    /// and the advertised VDM thresholds are static EEPROM data cached ONCE at identity
    /// read, never re-read on the periodic DOM poll. A module whose FIRST insert read
    /// returned `SFP_EEPROM_NOT_READY` (common under multiport load) only completes its
    /// identity read here, so without this the `TRANSCEIVER_{DOM,VDM_*}_THRESHOLD` rows
    /// would stay empty for the whole plug lifetime.
    pub fn retry_eeprom_reading(&mut self, stop: &AtomicBool) {
        if self.retry_eeprom_set.is_empty() {
            return;
        }
        let now = Instant::now();
        if let Some(last) = self.last_retry_eeprom_time {
            if now.duration_since(last) < Self::RETRY_EEPROM_READING_INTERVAL {
                return;
            }
        }
        self.last_retry_eeprom_time = Some(now);

        let hal = self.hal.clone();
        let th = self.table_helper.clone();
        let mut retry_success: Vec<String> = Vec::new();
        for logical_port in self.retry_eeprom_set.iter() {
            let asic_index = self
                .port_mapping
                .get_asic_id_for_logical_port(logical_port)
                .unwrap_or(0);
            let rc = post_port_sfp_info_to_db(
                logical_port,
                &self.port_mapping,
                th.get_intf_tbl(asic_index),
                hal.as_ref(),
            );
            if rc != SFP_EEPROM_NOT_READY {
                // Seed the static page-02h DOM thresholds and the advertised VDM thresholds
                // now that the identity read finally succeeded — the DOM poll never re-reads
                // them, so this is their only publish for a port that joined the retry set
                // (xcvrd.py:857-858).
                DomDbUtils::new().post_port_dom_thresholds_to_db(
                    stop,
                    logical_port,
                    &self.port_mapping,
                    th.get_dom_threshold_tbl(asic_index),
                    hal.as_ref(),
                    None,
                );
                VdmDbUtils::new().post_port_vdm_thresholds_to_db(
                    stop,
                    logical_port,
                    &self.port_mapping,
                    hal.as_ref(),
                    th.as_ref(),
                    None,
                );
                // Publish the media SI now that the identity read finally succeeded
                // (xcvrd.py:860 runs notify_media_setting on a retry success).
                notify_media_setting_for_logical_port(
                    logical_port,
                    &self.port_mapping,
                    hal.as_ref(),
                    th.as_ref(),
                    &self.media_settings,
                );
                retry_success.push(logical_port.clone());
            }
        }
        for p in retry_success {
            self.retry_eeprom_set.remove(&p);
        }
    }

    /// `on_add_logical_port` (xcvrd.py:770) — a CONFIG_DB logical port was added.
    ///
    /// Reseeds `PORT_TABLE.NPU_SI_SETTINGS_SYNC_STATUS` to the default (a freshly added
    /// port must re-notify its media SI), then repopulates the port's DB rows from the
    /// module. Three cases mirror the reference: (1) SFP present + no blocking error →
    /// publish `TRANSCEIVER_INFO` + DOM/VDM thresholds + media-SI notify (a still-unready
    /// EEPROM joins the retry set); (2) present with a blocking SFP error → only the
    /// decoded `TRANSCEIVER_STATUS_SW.error`; (3) absent → mark `REMOVED`. In every case
    /// the final `update_port_transceiver_status_table_sw` writes the resolved
    /// `{status, error}`. `cmis_state` is left to the `CmisManagerTask` (its sole owner).
    pub fn on_add_logical_port(&mut self, port_change_event: &PortChangeEvent, stop: &AtomicBool) {
        let asic_index = port_change_event.asic_id;
        let hal = self.hal.clone();
        let th = self.table_helper.clone();
        let status_sw_tbl = th.get_status_sw_tbl(asic_index);
        let int_tbl = th.get_intf_tbl(asic_index);
        let port_name = port_change_event.port_name.clone();

        // Initialize the NPU_SI_SETTINGS_SYNC_STATUS to the default value (xcvrd.py:788-796).
        th.get_state_port_tbl(asic_index).set(
            &port_name,
            &[(
                NPU_SI_SETTINGS_SYNC_STATUS_KEY.to_string(),
                NPU_SI_SETTINGS_DEFAULT_VALUE.to_string(),
            )],
        );

        let mut error_description = "N/A".to_string();
        let mut status: Option<String> = None;
        let mut read_eeprom = true;

        // A cached SFP error for this physical port sets the status/error and can block
        // the EEPROM read (xcvrd.py:801-819). The Rust `sfp_error_dict` is keyed by the
        // physical-port string and stores only the error value; the per-port vendor error
        // map is unavailable here, so a vendor-specific bit falls back to the HAL error
        // description exactly like the reference's empty-`error_dict` branch.
        if let Some(phys_idx) = port_change_event.physical_port {
            if let Some(value) = self.sfp_error_dict.get(&phys_idx.to_string()).cloned() {
                status = Some(value.clone());
                if let Ok(error_bits) = value.parse::<u32>() {
                    let mut error_descriptions = fetch_generic_error_description(error_bits);
                    if has_vendor_specific_error(error_bits) {
                        let vendor_specific = hal
                            .sfp(phys_idx)
                            .ok()
                            .and_then(|s| s.get_error_description().ok().flatten())
                            .unwrap_or_default();
                        error_descriptions.push(vendor_specific);
                    }
                    error_description = error_descriptions.join("|");
                    if is_error_block_eeprom_reading(error_bits) {
                        read_eeprom = false;
                    }
                }
            }
        }

        let present = port_change_event
            .physical_port
            .map(|p| {
                hal.sfp(p)
                    .ok()
                    .map(|s| wrapper_get_presence(s.as_ref()).unwrap_or(false))
                    .unwrap_or(false)
            })
            .unwrap_or(false);

        if present && read_eeprom {
            if status.is_none() {
                status = Some(SFP_STATUS_INSERTED.to_string());
            }
            let rc =
                post_port_sfp_info_to_db(&port_name, &self.port_mapping, int_tbl, hal.as_ref());
            if rc == SFP_EEPROM_NOT_READY {
                self.retry_eeprom_set.insert(port_name.clone());
            } else {
                DomDbUtils::new().post_port_dom_thresholds_to_db(
                    stop,
                    &port_name,
                    &self.port_mapping,
                    th.get_dom_threshold_tbl(asic_index),
                    hal.as_ref(),
                    None,
                );
                VdmDbUtils::new().post_port_vdm_thresholds_to_db(
                    stop,
                    &port_name,
                    &self.port_mapping,
                    hal.as_ref(),
                    th.as_ref(),
                    None,
                );
                notify_media_setting_for_logical_port(
                    &port_name,
                    &self.port_mapping,
                    hal.as_ref(),
                    th.as_ref(),
                    &self.media_settings,
                );
            }
        } else if status.is_none() {
            status = Some(SFP_STATUS_REMOVED.to_string());
        }

        update_port_transceiver_status_table_sw(
            &port_name,
            status_sw_tbl,
            status.as_deref().unwrap_or(SFP_STATUS_REMOVED),
            &error_description,
        );
    }

    /// `on_remove_logical_port` (xcvrd.py:731) — a CONFIG_DB logical port was deleted.
    ///
    /// Deletes the port's rows across the *whole* `TRANSCEIVER_*` table set — including
    /// `TRANSCEIVER_INFO` and `TRANSCEIVER_STATUS_SW` (a logical-port delete is stronger
    /// than a physical unplug, which keeps neither the static INFO here but does drop
    /// STATUS_SW) — so no stale rows survive the port going away, and drops the port from
    /// the EEPROM retry set (no point retrying a port that no longer exists).
    pub fn on_remove_logical_port(&mut self, port_change_event: &PortChangeEvent) {
        let th = self.table_helper.clone();
        let tables = logical_port_removal_tables(th.as_ref(), port_change_event.asic_id);
        del_port_sfp_dom_info_from_db(&port_change_event.port_name, &tables);
        self.retry_eeprom_set.remove(&port_change_event.port_name);
    }

    /// `on_port_config_change` (xcvrd.py:723) — dispatch a CONFIG_DB `PORT` add/remove.
    ///
    /// Ordering mirrors the reference: on a remove, delete the port's rows *before*
    /// dropping it from the mapping (the removal set still needs its asic id); on an add,
    /// update the mapping *before* repopulating (the DB posters resolve the physical port
    /// through the freshly-added mapping entry).
    pub fn on_port_config_change(
        &mut self,
        port_change_event: &PortChangeEvent,
        stop: &AtomicBool,
    ) {
        match port_change_event.event_type {
            PortChangeEventType::Remove => {
                self.on_remove_logical_port(port_change_event);
                self.port_mapping
                    .handle_port_change_event(port_change_event);
            }
            PortChangeEventType::Add => {
                self.port_mapping
                    .handle_port_change_event(port_change_event);
                self.on_add_logical_port(port_change_event, stop);
            }
            _ => {}
        }
    }

    /// `handle_port_config_change` (`port_event_helper.py:294`) — poll the CONFIG_DB `PORT`
    /// subscriber (blocking up to `timeout_ms`) and dispatch each resolved add/remove to
    /// [`Self::on_port_config_change`]. Split so the immutable `read_port_config_change`
    /// (borrows `self.port_mapping`) produces owned events *before* the dispatch loop mutates
    /// `self` (the map + DB tables).
    fn handle_port_config_change(
        &mut self,
        sub: &mut PortConfigChangeSubscriber,
        timeout_ms: u64,
        stop: &AtomicBool,
    ) {
        let updates = sub.poll(timeout_ms);
        if updates.is_empty() {
            return;
        }
        let events = read_port_config_change(&updates, &self.port_mapping, sub.asic_id());
        for ev in events {
            self.on_port_config_change(&ev, stop);
        }
    }

    /// `task_worker` — the change-event state machine loop.
    pub fn task_worker(&mut self, stop: &Arc<AtomicBool>, sfp_error_event: &Arc<AtomicBool>) {
        let hal = self.hal.clone();
        let th = self.table_helper.clone();

        let mut retry: u32 = 0;
        let mut timeout = self.retry_period_ready_ms;
        let mut state = STATE_INIT;

        // Subscribe to CONFIG_DB PORT so a logical-port add/remove (deconfigure / DPB /
        // re-add) tears down or repopulates the port's TRANSCEIVER_* table set
        // (xcvrd.py:471 `subscribe_port_config_change`). Non-fatal: if the subscription
        // can't be established the presence/identity engine still runs.
        let mut port_config_sub = match PortConfigChangeSubscriber::new(0) {
            Ok(s) => Some(s),
            Err(e) => {
                eprintln!("xcvrd-rs: CONFIG_DB PORT config-change watch unavailable: {e}");
                None
            }
        };

        while !stop.load(Ordering::Relaxed) {
            // React to a CONFIG_DB PORT add/remove first (xcvrd.py:473), blocking up to
            // SELECT_TIMEOUT_MSECS — this also paces the loop so `stop` is re-checked ~1 s.
            if let Some(sub) = port_config_sub.as_mut() {
                self.handle_port_config_change(sub, SELECT_TIMEOUT_MSECS, stop);
                if stop.load(Ordering::Relaxed) {
                    break;
                }
            }

            // Retry any logical ports whose EEPROM read failed on insertion.
            self.retry_eeprom_reading(stop);

            let mut next_state = state;
            let time_start = Instant::now();

            if !self.sfp_insert_events.is_empty() {
                timeout = SFP_INSERT_EVENT_POLL_PERIOD_MSECS;
            }

            // Cap the idle block so the loop re-checks `stop` promptly on shutdown (the
            // reference interrupts this read via `raise_exception()`, unavailable in Rust).
            // The emulator's `get_change_event` returns early on any change, so a shorter
            // ceiling does not change change-detection latency.
            let ev = match hal.get_change_event(timeout.min(SELECT_TIMEOUT_MSECS)) {
                Ok(ev) => ev,
                Err(_) => {
                    // Transient bridge poll error — the reference emulator swallows it.
                    std::thread::sleep(Duration::from_millis(1));
                    continue;
                }
            };
            let status = ev.status;
            let mut port_dict = ev.sfp.clone();
            let error_dict = ev.sfp_error.clone();

            // Soak SFP insert events across various ports (xcvrd.py:483-485): gated on a
            // successful poll, withhold a fresh insert for `mgmt_init_time_delay` and
            // re-inject it only once elapsed, while a remove arriving in the window cancels
            // the pending insert. This also populates `sfp_insert_events`, which shortens
            // the next poll timeout (checked above) so a pending insert is re-checked ~1 s.
            if status {
                wrapper_soak_sfp_insert_event(
                    &mut self.sfp_insert_events,
                    &mut port_dict,
                    Instant::now(),
                    self.mgmt_init_time_delay,
                );
            }

            if port_dict.is_empty() {
                // Timeout with no change — the real `get_change_event` already blocked
                // for `timeout`; pace the mock-driven loop so it doesn't busy-spin.
                std::thread::sleep(Duration::from_millis(1));
                continue;
            }

            let event = self.mapping_event_from_change_event(status, &mut port_dict);
            match event {
                SystemEvent::SystemNotReady => {
                    if state == STATE_INIT {
                        if retry >= self.retry_times_ready {
                            next_state = STATE_EXIT;
                            sfp_error_event.store(true, Ordering::SeqCst);
                        } else {
                            retry += 1;
                            sleep_compensation(time_start, self.retry_period_ready_ms);
                        }
                    } else {
                        next_state = STATE_EXIT;
                    }
                }
                SystemEvent::SystemBecomeReady => {
                    if state == STATE_INIT {
                        next_state = STATE_NORMAL;
                    } else if state == STATE_NORMAL {
                        // ignored
                    } else {
                        next_state = STATE_EXIT;
                    }
                }
                SystemEvent::Normal => {
                    if state == STATE_NORMAL || state == STATE_INIT {
                        if state == STATE_INIT {
                            next_state = STATE_NORMAL;
                        }
                        for (key, value) in port_dict.iter() {
                            // Cache/clear the per-port SFP error (a plug event clears it).
                            if value != SFP_STATUS_INSERTED && value != SFP_STATUS_REMOVED {
                                self.sfp_error_dict.insert(key.clone(), value.clone());
                            } else {
                                self.sfp_error_dict.remove(key);
                            }

                            let Ok(phys) = key.parse::<usize>() else {
                                continue;
                            };
                            let Some(logical_port_list) =
                                self.port_mapping.get_physical_to_logical(phys)
                            else {
                                // Unknown FP port index — ignored.
                                continue;
                            };
                            for logical_port in logical_port_list {
                                let Some(asic_index) = self
                                    .port_mapping
                                    .get_asic_id_for_logical_port(&logical_port)
                                else {
                                    continue;
                                };
                                if value == SFP_STATUS_INSERTED {
                                    handle_sfp_insert(
                                        &logical_port,
                                        &self.port_mapping,
                                        hal.as_ref(),
                                        th.as_ref(),
                                        asic_index,
                                        self.time_for_sfp_ready,
                                        &mut self.retry_eeprom_set,
                                        &self.media_settings,
                                        stop,
                                    );
                                } else if value == SFP_STATUS_REMOVED {
                                    handle_sfp_remove(
                                        &logical_port,
                                        phys,
                                        hal.as_ref(),
                                        th.as_ref(),
                                        asic_index,
                                    );
                                } else {
                                    handle_sfp_error(
                                        &logical_port,
                                        phys,
                                        key,
                                        hal.as_ref(),
                                        th.as_ref(),
                                        asic_index,
                                        value,
                                        &error_dict,
                                    );
                                }
                            }
                        }
                    } else {
                        next_state = STATE_EXIT;
                    }
                }
                SystemEvent::SystemFail => {
                    if state == STATE_INIT {
                        if retry >= self.retry_times_fail {
                            next_state = STATE_EXIT;
                            sfp_error_event.store(true, Ordering::SeqCst);
                        } else {
                            retry += 1;
                            sleep_compensation(time_start, self.retry_period_fail_ms);
                        }
                    } else if state == STATE_NORMAL {
                        next_state = STATE_INIT;
                        timeout = self.retry_period_fail_ms;
                        retry = 0;
                    } else {
                        next_state = STATE_EXIT;
                    }
                }
            }

            if next_state != state {
                state = next_state;
            }
            if next_state == STATE_EXIT {
                // Reference: os.kill(getppid(), SIGTERM). Ask the supervisor to shut
                // us down; here recorded as an observable count + loop break.
                self.parent_kill_count += 1;
                break;
            } else if next_state == STATE_NORMAL {
                timeout = STATE_MACHINE_UPDATE_PERIOD_MSECS;
            }
        }
    }

    /// Spawn helper: init + run the task to completion on this thread. Mirrors the reference
    /// `SfpStateUpdateTask.run` (xcvrd.py:695), which returns immediately when the stop event
    /// is already set — so a task whose thread is spawned after shutdown began does no work.
    pub fn run(mut self, stop: Arc<AtomicBool>, sfp_error_event: Arc<AtomicBool>) {
        if stop.load(Ordering::Relaxed) {
            return;
        }
        self.init(&stop);
        self.task_worker(&stop, &sfp_error_event);
    }

    /// The task's port mapping (read access for the production wiring/tests).
    pub fn port_mapping(&self) -> &PortMapping {
        &self.port_mapping
    }

    // --- test hooks ---------------------------------------------------------------

    /// Number of `STATE_EXIT` transitions observed (the `os.kill` stand-in).
    pub fn parent_kill_count(&self) -> usize {
        self.parent_kill_count
    }

    #[cfg(test)]
    pub(crate) fn port_mapping_mut(&mut self) -> &mut PortMapping {
        &mut self.port_mapping
    }

    #[cfg(test)]
    pub(crate) fn retry_eeprom_set(&self) -> &BTreeSet<String> {
        &self.retry_eeprom_set
    }

    #[cfg(test)]
    pub(crate) fn insert_retry_port(&mut self, logical_port: &str) {
        self.retry_eeprom_set.insert(logical_port.to_string());
    }

    /// Seed a cached SFP error for a physical-port key (Python `sfp_error_dict[idx]`),
    /// so the `on_add_logical_port` error branch can be unit-tested.
    #[cfg(test)]
    pub(crate) fn set_sfp_error(&mut self, phys_key: &str, value: &str) {
        self.sfp_error_dict
            .insert(phys_key.to_string(), value.to_string());
    }

    /// Zero out every sleep/retry delay so threaded tests finish immediately.
    #[cfg(test)]
    pub(crate) fn set_fast_timing(&mut self) {
        self.time_for_sfp_ready = Duration::ZERO;
        self.mgmt_init_time_delay = Duration::ZERO;
        self.retry_period_ready_ms = 0;
        self.retry_period_fail_ms = 0;
        self.retry_times_ready = 0;
        self.retry_times_fail = 0;
        self.last_retry_eeprom_time = None;
    }

    /// Override the insert-event soak delay (test-only): threaded routing tests set a
    /// non-zero delay to observe a fresh insert being withheld / later re-injected.
    #[cfg(test)]
    pub(crate) fn set_mgmt_init_time_delay(&mut self, delay: Duration) {
        self.mgmt_init_time_delay = delay;
    }

    /// The currently-pending soaked insert events (test-only read access).
    #[cfg(test)]
    pub(crate) fn pending_insert_events(&self) -> &BTreeMap<String, Instant> {
        &self.sfp_insert_events
    }
}

/// Sleep the remainder of `period_ms` since `time_start`, if any
/// (`waiting_time_compensation_with_sleep`).
fn sleep_compensation(time_start: Instant, period_ms: u64) {
    let period = Duration::from_millis(period_ms);
    let elapsed = time_start.elapsed();
    if elapsed < period {
        std::thread::sleep(period - elapsed);
    }
}

/// `_wrapper_soak_sfp_insert_event` (xcvrd.py:127) — debounce SFP insert events until
/// management init settles.
///
/// A fresh insert (`"1"`) is *withheld*: it is stamped into `sfp_insert_events` (keyed by
/// physical-port string) with `now` and dropped from `port_dict`, so the caller does not act
/// on it yet. A remove (`"0"`) arriving while an insert is pending *cancels* it (drops the
/// stamp) and passes through immediately; any error code passes through untouched. Any
/// stamped insert whose age has reached `delay` (`MGMT_INIT_TIME_DELAY_SECS`) is re-injected
/// into `port_dict` as `"1"` and cleared from the buffer.
///
/// `now`/`delay` are injected (the reference uses `time.time()` and the module const) so the
/// 2 s debounce is deterministic under test.
pub(crate) fn wrapper_soak_sfp_insert_event(
    sfp_insert_events: &mut BTreeMap<String, Instant>,
    port_dict: &mut BTreeMap<String, String>,
    now: Instant,
    delay: Duration,
) {
    // Pass 1: stamp & withhold fresh inserts; cancel a pending insert on removal.
    let keys: Vec<String> = port_dict.keys().cloned().collect();
    for key in keys {
        // Clone the value so the immutable borrow ends before we mutate `port_dict`.
        let value = match port_dict.get(&key) {
            Some(v) => v.clone(),
            None => continue,
        };
        if value == SFP_STATUS_INSERTED {
            sfp_insert_events.insert(key.clone(), now);
            port_dict.remove(&key);
        } else if value == SFP_STATUS_REMOVED {
            sfp_insert_events.remove(&key);
        }
    }

    // Pass 2: re-inject any soaked insert whose delay has elapsed, draining the buffer.
    let elapsed: Vec<String> = sfp_insert_events
        .iter()
        .filter(|(_, stamp)| now.saturating_duration_since(**stamp) >= delay)
        .map(|(key, _)| key.clone())
        .collect();
    for key in elapsed {
        port_dict.insert(key.clone(), SFP_STATUS_INSERTED.to_string());
        sfp_insert_events.remove(&key);
    }
}

/// Handle a `SFP_STATUS_INSERTED` code for one logical port: mark STATUS_SW inserted and
/// publish INFO (one immediate retry after a settle delay). `cmis_state` is deliberately
/// left to the `CmisManagerTask` (its sole owner, per xcvrd.py) so this insert path cannot
/// race/clobber an in-progress CMIS datapath bring-up. A still-unready EEPROM joins the
/// retry set.
fn handle_sfp_insert(
    logical_port: &str,
    port_mapping: &PortMapping,
    hal: &dyn Hal,
    th: &XcvrTableHelper,
    asic_index: usize,
    time_for_sfp_ready: Duration,
    retry_eeprom_set: &mut BTreeSet<String>,
    media_settings: &Value,
    stop: &AtomicBool,
) {
    // A plug-in event clears any prior error state.
    update_port_transceiver_status_table_sw(
        logical_port,
        th.get_status_sw_tbl(asic_index),
        SFP_STATUS_INSERTED,
        "N/A",
    );

    let intf_tbl = th.get_intf_tbl(asic_index);
    let mut rc = post_port_sfp_info_to_db(logical_port, port_mapping, intf_tbl, hal);
    if rc == SFP_EEPROM_NOT_READY {
        if !time_for_sfp_ready.is_zero() {
            std::thread::sleep(time_for_sfp_ready);
        }
        rc = post_port_sfp_info_to_db(logical_port, port_mapping, intf_tbl, hal);
        if rc == SFP_EEPROM_NOT_READY {
            retry_eeprom_set.insert(logical_port.to_string());
        }
    }
    if rc != SFP_EEPROM_NOT_READY {
        // Seed the decoded page-02h DOM thresholds *and* the VDM thresholds once, at
        // insert (the DOM poll does NOT re-read them) — mirrors xcvrd.py:569-570's
        // post_port_{dom,vdm}_thresholds_to_db on plug-in.
        DomDbUtils::new().post_port_dom_thresholds_to_db(
            stop,
            logical_port,
            port_mapping,
            th.get_dom_threshold_tbl(asic_index),
            hal,
            None,
        );
        VdmDbUtils::new().post_port_vdm_thresholds_to_db(
            stop,
            logical_port,
            port_mapping,
            hal,
            th,
            None,
        );
        // Publish the ASIC-side media SI + stamp NPU_SI_SETTINGS_NOTIFIED (xcvrd.py:572).
        notify_media_setting_for_logical_port(logical_port, port_mapping, hal, th, media_settings);
    }
}

/// Resolve + publish the media SI for `logical_port` (xcvrd.py's
/// `media_settings_parser.notify_media_setting(logical_port, transceiver_dict, …)`): a
/// no-op when no `media_settings.json` is loaded, else build the present-gated
/// `transceiver_dict` and drive the parser over the HAL-backed [`HalMediaResolver`].
fn notify_media_setting_for_logical_port(
    logical_port: &str,
    port_mapping: &PortMapping,
    hal: &dyn Hal,
    th: &XcvrTableHelper,
    media_settings: &Value,
) {
    if !media_settings_parser::media_settings_present(media_settings) {
        return;
    }
    let transceiver_dict = build_transceiver_dict(logical_port, port_mapping, hal);
    let resolver = HalMediaResolver { hal };
    media_settings_parser::notify_media_setting(
        media_settings,
        logical_port,
        &transceiver_dict,
        th,
        port_mapping,
        &resolver,
    );
}

/// Production [`PortMediaResolver`] over the HAL: presence via `get_presence()` and the
/// media-settings key via a real [`BridgeCmisApi`] (so CMIS modules take the raw
/// compliance-code + application-advertisement lane-speed path, mirroring
/// `get_media_settings_key`'s `sfp.get_xcvr_api()` reads). Unit tests inject their own
/// resolver instead of this Python-backed one.
struct HalMediaResolver<'a> {
    hal: &'a dyn Hal,
}

impl PortMediaResolver for HalMediaResolver<'_> {
    fn is_present(&self, physical_port: usize) -> bool {
        self.hal
            .sfp(physical_port)
            .ok()
            .and_then(|s| s.get_presence().ok())
            .unwrap_or(false)
    }

    fn media_settings_key(
        &self,
        physical_port: usize,
        transceiver_dict: &Value,
        port_speed: i64,
        lane_count: i64,
    ) -> Option<MediaSettingsKey> {
        let sfp = self.hal.sfp(physical_port).ok()?;
        let api = BridgeCmisApi::new(sfp);
        let is_cmis = api
            .get_module_type_abbreviation()
            .as_deref()
            .map(|t| CMIS_MODULE_TYPES.contains(&t))
            .unwrap_or(false);
        let api_opt: Option<&dyn CmisApi> = if is_cmis { Some(&api) } else { None };
        // `is_copper` defaults true — the CmisApi seam has no `is_copper()` and Python
        // defaults True on AttributeError. It only feeds the medium-lane-speed fallback
        // key, never the primary vendor/media key the shipped profiles use.
        Some(get_media_settings_key(
            physical_port,
            transceiver_dict,
            port_speed,
            lane_count,
            api_opt,
            true,
        ))
    }
}

/// Handle a `SFP_STATUS_REMOVED` code: invalidate the plugin's cached `xcvr_api`, mark
/// STATUS_SW removed and delete every dynamic `TRANSCEIVER_*` row (INFO included,
/// mirroring the reference removal set).
fn handle_sfp_remove(
    logical_port: &str,
    phys: usize,
    hal: &dyn Hal,
    th: &XcvrTableHelper,
    asic_index: usize,
) {
    // Drop the plugin's cached xcvr_api for this physical port so a subsequent re-insert
    // rebuilds it and re-reads the module EEPROM fresh. The CMIS advertisement decoders
    // (`is_transceiver_vdm_supported`, `is_vdm_statistic_supported`, …) are
    // `@read_only_cached_api_return`-pinned on the api instance once read, so a module that
    // starts advertising VDM/PM mid-life (the e2e harness provisions it then re-plugs) is
    // only observed after this drop — without it INFO.vdm_supported stays False and the
    // whole VDM/PM poster path is gated off. Mirrors xcvrd.py's `sfp.remove_xcvr_api()` on a
    // plug-out; a missing/erroring method (Python's `NotImplementedError`/`AttributeError`)
    // is swallowed, exactly like the reference `except`.
    if let Ok(sfp) = hal.sfp(phys) {
        let _ = sfp.call_json("remove_xcvr_api");
    }
    // Reset the media-SI idempotency guard so the next plug-in re-publishes
    // (xcvrd.py:582-583 seeds NPU_SI_SETTINGS_DEFAULT on plug-out).
    th.get_state_port_tbl(asic_index).set(
        logical_port,
        &[(
            NPU_SI_SETTINGS_SYNC_STATUS_KEY.to_string(),
            NPU_SI_SETTINGS_DEFAULT_VALUE.to_string(),
        )],
    );
    update_port_transceiver_status_table_sw(
        logical_port,
        th.get_status_sw_tbl(asic_index),
        SFP_STATUS_REMOVED,
        "N/A",
    );
    let tables = full_removal_tables(th, asic_index);
    del_port_sfp_dom_info_from_db(logical_port, &tables);
}

/// Handle an SFP error bitmap: publish the decoded `STATUS_SW.error`, and — when the
/// error blocks the EEPROM — drop the (possibly stale) DOM/VDM/status rows while keeping
/// the static INFO row.
#[allow(clippy::too_many_arguments)]
fn handle_sfp_error(
    logical_port: &str,
    phys: usize,
    key: &str,
    hal: &dyn Hal,
    th: &XcvrTableHelper,
    asic_index: usize,
    value: &str,
    error_dict: &BTreeMap<String, String>,
) {
    let Ok(error_bits) = value.parse::<u32>() else {
        // Unrecognized event — ignored (Python's TypeError/ValueError guard).
        return;
    };

    let mut error_descriptions = fetch_generic_error_description(error_bits);
    if has_vendor_specific_error(error_bits) {
        let vendor_specific = if !error_dict.is_empty() {
            error_dict.get(key).cloned().unwrap_or_default()
        } else {
            hal.sfp(phys)
                .ok()
                .and_then(|s| s.get_error_description().ok().flatten())
                .unwrap_or_default()
        };
        error_descriptions.push(vendor_specific);
    }

    // Any existing error is replaced by the new one.
    update_port_transceiver_status_table_sw(
        logical_port,
        th.get_status_sw_tbl(asic_index),
        value,
        &error_descriptions.join("|"),
    );

    if is_error_block_eeprom_reading(error_bits) {
        let tables = dom_removal_tables(th, asic_index);
        del_port_sfp_dom_info_from_db(logical_port, &tables);
    }
}

/// The full `TRANSCEIVER_*` removal set for a plug-out (INFO + all DOM/VDM/status/PM/
/// firmware rows), mirroring the reference `del_port_sfp_dom_info_from_db` argument list.
fn full_removal_tables(th: &XcvrTableHelper, asic: usize) -> Vec<&dyn DbTable> {
    let mut tables: Vec<&dyn DbTable> = vec![th.get_intf_tbl(asic)];
    tables.extend(dom_removal_tables(th, asic));
    tables
}

/// The removal set for a CONFIG_DB logical-port delete (`on_remove_logical_port`): the
/// full plug-out set *plus* `TRANSCEIVER_STATUS_SW` — a logical-port delete tears down
/// everything the port owned, whereas a physical unplug keeps re-marking STATUS_SW
/// `REMOVED` (so it is deleted here but re-written, not deleted, on a plug-out).
fn logical_port_removal_tables(th: &XcvrTableHelper, asic: usize) -> Vec<&dyn DbTable> {
    let mut tables = full_removal_tables(th, asic);
    tables.push(th.get_status_sw_tbl(asic));
    tables
}

/// The DOM/VDM/status/PM/firmware rows dropped on a blocking error (INFO is kept).
fn dom_removal_tables(th: &XcvrTableHelper, asic: usize) -> Vec<&dyn DbTable> {
    let mut tables: Vec<&dyn DbTable> = vec![
        th.get_dom_tbl(asic),
        th.get_dom_temperature_tbl(asic),
        th.get_dom_flag_tbl(asic),
        th.get_dom_flag_change_count_tbl(asic),
        th.get_dom_flag_set_time_tbl(asic),
        th.get_dom_flag_clear_time_tbl(asic),
        th.get_dom_threshold_tbl(asic),
    ];
    for t in VDM_THRESHOLD_TYPES {
        tables.push(th.get_vdm_threshold_tbl(asic, t));
    }
    tables.push(th.get_vdm_real_value_tbl(asic));
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hal::ChangeEvent;
    use crate::mock::{MockHal, MockSfp};
    use crate::xcvrd_utilities::port_event_helper::{PortChangeEvent, PortChangeEventType};
    use serde_json::json;
    use std::sync::atomic::AtomicBool;

    fn port_mapping(ports: &[(&str, usize)]) -> PortMapping {
        let mut pm = PortMapping::new();
        for (name, index) in ports {
            pm.handle_port_change_event(&PortChangeEvent::new(
                name.to_string(),
                Some(*index),
                0,
                PortChangeEventType::Add,
                "CONFIG_DB".to_string(),
                "PORT".to_string(),
            ));
        }
        pm
    }

    fn cmis_info() -> serde_json::Value {
        json!({
            "cmis_rev": "5.2",
            "manufacturer": "xcvr-emu",
            "model": "EMU-40G-LR4",
            "vendor_rev": "01",
        })
    }

    fn task(hal: MockHal, pm: PortMapping) -> SfpStateUpdateTask {
        let mut t = SfpStateUpdateTask::new(
            vec![String::new()],
            pm,
            Arc::new(hal),
            Arc::new(XcvrTableHelper::with_mock_tables(&[String::new()])),
        );
        t.set_fast_timing();
        t
    }

    fn insert_event(phys: usize) -> ChangeEvent {
        let mut sfp = BTreeMap::new();
        sfp.insert(phys.to_string(), SFP_STATUS_INSERTED.to_string());
        ChangeEvent {
            status: true,
            sfp,
            sfp_error: BTreeMap::new(),
        }
    }

    fn remove_event(phys: usize) -> ChangeEvent {
        let mut sfp = BTreeMap::new();
        sfp.insert(phys.to_string(), SFP_STATUS_REMOVED.to_string());
        ChangeEvent {
            status: true,
            sfp,
            sfp_error: BTreeMap::new(),
        }
    }

    // ← tests/test_xcvrd.py::test_SfpStateUpdateTask_mapping_event_from_change_event
    #[test]
    fn mapping_event_covers_all_transitions() {
        let t = task(MockHal::default(), PortMapping::new());

        // status + non-empty dict → NORMAL.
        let mut d = BTreeMap::from([("0".to_string(), "1".to_string())]);
        assert_eq!(
            t.mapping_event_from_change_event(true, &mut d),
            SystemEvent::Normal
        );

        // status + empty dict → BECOME_READY, and the all-SFP marker is injected.
        let mut d = BTreeMap::new();
        assert_eq!(
            t.mapping_event_from_change_event(true, &mut d),
            SystemEvent::SystemBecomeReady
        );
        assert_eq!(
            d.get(EVENT_ON_ALL_SFP).map(String::as_str),
            Some(SYSTEM_BECOME_READY)
        );

        // !status carrying an all-SFP code → that system event.
        let mut d = BTreeMap::from([(EVENT_ON_ALL_SFP.to_string(), SYSTEM_NOT_READY.to_string())]);
        assert_eq!(
            t.mapping_event_from_change_event(false, &mut d),
            SystemEvent::SystemNotReady
        );

        // !status with no all-SFP code → protective SYSTEM_FAIL (marker injected).
        let mut d = BTreeMap::new();
        assert_eq!(
            t.mapping_event_from_change_event(false, &mut d),
            SystemEvent::SystemFail
        );
        assert_eq!(
            d.get(EVENT_ON_ALL_SFP).map(String::as_str),
            Some(SYSTEM_FAIL)
        );
    }

    // ← tests/test_xcvrd.py::test_SfpStateUpdateTask__post_port_sfp_info_and_dom_thr...
    // and test__init_port_sfp_status_sw_tbl: boot init publishes INFO + STATUS_SW for a
    // present readable port. `cmis_state` is NOT written here — it is owned solely by the
    // CmisManagerTask (mirroring xcvrd.py), so the boot seed cannot clobber CMIS bring-up.
    #[test]
    fn init_publishes_info_and_status_sw() {
        let hal = MockHal::with_sfps(vec![MockSfp::present().with_info(cmis_info())]);
        let mut t = task(hal, port_mapping(&[("Ethernet0", 0)]));
        let stop = AtomicBool::new(false);

        t.init(&stop);

        let th = t.table_helper.clone();
        assert_eq!(
            th.get_intf_tbl(0)
                .hget("Ethernet0", "manufacturer")
                .as_deref(),
            Some("xcvr-emu")
        );
        assert_eq!(
            th.get_status_sw_tbl(0)
                .hget("Ethernet0", "status")
                .as_deref(),
            Some("1")
        );
        assert_eq!(
            th.get_status_sw_tbl(0)
                .hget("Ethernet0", "error")
                .as_deref(),
            Some("N/A")
        );
        assert_eq!(
            th.get_status_sw_tbl(0).hget("Ethernet0", "cmis_state"),
            None
        );
        assert!(t.retry_eeprom_set().is_empty());
    }

    // Boot init for an ABSENT port: no INFO, STATUS_SW.status=0, no cmis_state.
    #[test]
    fn init_absent_port_marks_removed() {
        let hal = MockHal::with_sfps(vec![MockSfp::default()]);
        let mut t = task(hal, port_mapping(&[("Ethernet0", 0)]));
        let stop = AtomicBool::new(false);

        t.init(&stop);

        let th = t.table_helper.clone();
        assert_eq!(th.get_intf_tbl(0).get_size_for_key("Ethernet0"), 0);
        assert_eq!(
            th.get_status_sw_tbl(0)
                .hget("Ethernet0", "status")
                .as_deref(),
            Some("0")
        );
        assert_eq!(
            th.get_status_sw_tbl(0).hget("Ethernet0", "cmis_state"),
            None
        );
    }

    // A present port whose EEPROM is unreadable at boot joins the retry set (no INFO).
    #[test]
    fn init_unreadable_port_joins_retry_set() {
        let hal = MockHal::with_sfps(vec![MockSfp::present()]); // present, null info
        let mut t = task(hal, port_mapping(&[("Ethernet0", 0)]));
        let stop = AtomicBool::new(false);

        t.init(&stop);

        assert!(t.retry_eeprom_set().contains("Ethernet0"));
        assert_eq!(
            t.table_helper.get_intf_tbl(0).get_size_for_key("Ethernet0"),
            0
        );
    }

    // ← tests/test_xcvrd.py::test_initialize_port_init_control_fields_in_port_table (C21):
    // boot init seeds STATE_DB PORT_TABLE|<lport>.NPU_SI_SETTINGS_SYNC_STATUS = DEFAULT
    // for every logical port so the DEFAULT→NOTIFIED lifecycle has a starting point.
    #[test]
    fn init_seeds_npu_si_settings_default() {
        let hal = MockHal::with_sfps(vec![MockSfp::present().with_info(cmis_info())]);
        let mut t = task(hal, port_mapping(&[("Ethernet0", 0), ("Ethernet4", 1)]));
        let stop = AtomicBool::new(false);

        t.init(&stop);

        let th = t.table_helper.clone();
        for lport in ["Ethernet0", "Ethernet4"] {
            assert_eq!(
                th.get_state_port_tbl(0)
                    .hget(lport, NPU_SI_SETTINGS_SYNC_STATUS_KEY)
                    .as_deref(),
                Some(NPU_SI_SETTINGS_DEFAULT_VALUE),
                "{lport}: NPU_SI_SETTINGS_SYNC_STATUS seeded to DEFAULT at init"
            );
        }
    }

    // The init seed is only-if-absent (xcvrd.py:955): a NOTIFIED value that survived a
    // daemon restart is preserved (no media_settings loaded ⇒ the boot notify is a no-op,
    // so it cannot re-stamp it either).
    #[test]
    fn init_preserves_existing_notified_npu_si() {
        let hal = MockHal::with_sfps(vec![MockSfp::present().with_info(cmis_info())]);
        let mut t = task(hal, port_mapping(&[("Ethernet0", 0)]));
        t.table_helper.get_state_port_tbl(0).set(
            "Ethernet0",
            &[(
                NPU_SI_SETTINGS_SYNC_STATUS_KEY.to_string(),
                "NPU_SI_SETTINGS_NOTIFIED".to_string(),
            )],
        );
        let stop = AtomicBool::new(false);

        t.init(&stop);

        assert_eq!(
            t.table_helper
                .get_state_port_tbl(0)
                .hget("Ethernet0", NPU_SI_SETTINGS_SYNC_STATUS_KEY)
                .as_deref(),
            Some("NPU_SI_SETTINGS_NOTIFIED")
        );
    }

    // On a plug-out xcvrd resets the media-SI idempotency guard back to DEFAULT so the
    // next plug-in re-publishes (xcvrd.py:582-583). Drives `handle_sfp_remove` directly.
    #[test]
    fn handle_sfp_remove_resets_npu_si_to_default() {
        let hal = MockHal::with_sfps(vec![MockSfp::present().with_info(cmis_info())]);
        let t = task(hal, port_mapping(&[("Ethernet0", 0)]));
        let th = t.table_helper.clone();
        th.get_state_port_tbl(0).set(
            "Ethernet0",
            &[(
                NPU_SI_SETTINGS_SYNC_STATUS_KEY.to_string(),
                "NPU_SI_SETTINGS_NOTIFIED".to_string(),
            )],
        );

        handle_sfp_remove("Ethernet0", 0, t.hal.as_ref(), th.as_ref(), 0);

        assert_eq!(
            th.get_state_port_tbl(0)
                .hget("Ethernet0", NPU_SI_SETTINGS_SYNC_STATUS_KEY)
                .as_deref(),
            Some(NPU_SI_SETTINGS_DEFAULT_VALUE)
        );
    }

    // ← tests/test_xcvrd.py::test_SfpStateUpdateTask_retry_eeprom_reading
    #[test]
    fn retry_eeprom_reading_recovers_readable_port() {
        let hal = MockHal::with_sfps(vec![MockSfp::present().with_info(cmis_info())]);
        let mut t = task(hal, port_mapping(&[("Ethernet0", 0)]));
        t.insert_retry_port("Ethernet0");

        t.retry_eeprom_reading(&AtomicBool::new(false));

        // INFO published and port dropped from the retry set. `cmis_state` is NOT written
        // by this task (CmisManagerTask owns it), so it stays absent here.
        assert_eq!(
            t.table_helper
                .get_intf_tbl(0)
                .hget("Ethernet0", "manufacturer")
                .as_deref(),
            Some("xcvr-emu")
        );
        assert_eq!(
            t.table_helper
                .get_status_sw_tbl(0)
                .hget("Ethernet0", "cmis_state"),
            None
        );
        assert!(!t.retry_eeprom_set().contains("Ethernet0"));
    }

    // ← A port whose insert EEPROM read returned NOT_READY only completes its
    // identity read here in retry_eeprom_reading. That success path must ALSO seed the
    // static page-02h DOM thresholds and the advertised VDM thresholds (xcvrd.py:857-858) —
    // they are cached once at identity read and never re-read on the DOM poll. Without the
    // seeding a re-plugged port (common under multiport load, where the first read is
    // NOT_READY) leaves TRANSCEIVER_{DOM,VDM_*}_THRESHOLD empty for its whole plug lifetime
    // (the observed test_vdm_threshold_values_published e2e failure).
    #[test]
    fn retry_eeprom_reading_seeds_dom_and_vdm_thresholds() {
        let mut vdm_thr = serde_json::Map::new();
        for fam in ["halarm", "lalarm", "hwarn", "lwarn"] {
            vdm_thr.insert(format!("laser_temperature_media_1_{fam}"), json!(1.0));
        }
        let sfp = MockSfp::present()
            .with_info(cmis_info())
            .with_threshold_info(json!({ "temphighalarm": 75.0 }))
            .with_json(
                "get_transceiver_vdm_thresholds",
                serde_json::Value::Object(vdm_thr),
            );
        let hal = MockHal::with_sfps(vec![sfp]);
        let mut t = task(hal, port_mapping(&[("Ethernet0", 0)]));
        t.insert_retry_port("Ethernet0");

        t.retry_eeprom_reading(&AtomicBool::new(false));

        let th = t.table_helper.clone();
        // Identity read succeeded → dropped from the retry set + INFO published.
        assert!(!t.retry_eeprom_set().contains("Ethernet0"));
        assert_eq!(
            th.get_intf_tbl(0)
                .hget("Ethernet0", "manufacturer")
                .as_deref(),
            Some("xcvr-emu")
        );
        // DOM thresholds seeded (previously skipped entirely on the retry path).
        assert_eq!(
            th.get_dom_threshold_tbl(0)
                .hget("Ethernet0", "temphighalarm")
                .as_deref(),
            Some("75.0")
        );
        // Every VDM threshold family table is seeded — including the HWARN table the
        // failing e2e (TRANSCEIVER_VDM_HWARN_THRESHOLD|<port>) asserts.
        for fam in ["halarm", "lalarm", "hwarn", "lwarn"] {
            assert!(
                th.get_vdm_threshold_tbl(0, fam)
                    .get_size_for_key("Ethernet0")
                    > 0,
                "VDM {fam} threshold table must be seeded on retry success"
            );
        }
    }

    // A still-unreadable port stays in the retry set (the read-retry loop keeps trying).
    #[test]
    fn retry_eeprom_reading_keeps_unreadable_port() {
        let hal = MockHal::with_sfps(vec![MockSfp::present()]); // null info
        let mut t = task(hal, port_mapping(&[("Ethernet0", 0)]));
        t.insert_retry_port("Ethernet0");

        t.retry_eeprom_reading(&AtomicBool::new(false));

        assert!(t.retry_eeprom_set().contains("Ethernet0"));
        assert_eq!(
            t.table_helper.get_intf_tbl(0).get_size_for_key("Ethernet0"),
            0
        );
    }

    // The 60 s interval gate: a fresh sweep timestamp suppresses the next retry.
    #[test]
    fn retry_eeprom_reading_respects_interval() {
        let hal = MockHal::with_sfps(vec![MockSfp::present().with_info(cmis_info())]);
        let mut t = SfpStateUpdateTask::new(
            vec![String::new()],
            port_mapping(&[("Ethernet0", 0)]),
            Arc::new(hal),
            Arc::new(XcvrTableHelper::with_mock_tables(&[String::new()])),
        );
        t.insert_retry_port("Ethernet0");
        t.last_retry_eeprom_time = Some(Instant::now()); // just retried → within 60 s

        t.retry_eeprom_reading(&AtomicBool::new(false));

        // Interval not elapsed → nothing published, port still pending.
        assert_eq!(
            t.table_helper.get_intf_tbl(0).get_size_for_key("Ethernet0"),
            0
        );
        assert!(t.retry_eeprom_set().contains("Ethernet0"));
    }

    // ── SFP insert-event soak (MGMT_INIT debounce + cancel-on-remove) ──────────

    // ← tests/test_xcvrd.py::test_sfp_insert_events: a fresh insert is withheld from
    // port_dict until MGMT_INIT_TIME_DELAY_SECS elapses, then re-appears. An injected
    // clock makes the 2 s debounce deterministic (the reference uses time.time() + sleeps).
    #[test]
    fn soak_withholds_insert_until_delay_elapses() {
        let delay = Duration::from_secs(MGMT_INIT_TIME_DELAY_SECS);
        let mut events: BTreeMap<String, Instant> = BTreeMap::new();
        let expected: BTreeMap<String, String> = (1..=5)
            .map(|i| (i.to_string(), SFP_STATUS_INSERTED.to_string()))
            .collect();
        let mut port_dict = expected.clone();
        let t0 = Instant::now();

        // First soak: every fresh insert is stamped and withheld.
        wrapper_soak_sfp_insert_event(&mut events, &mut port_dict, t0, delay);
        assert!(port_dict.is_empty(), "a fresh insert must be withheld");
        assert_eq!(events.len(), 5);

        // Polls before the delay elapses keep the ports withheld.
        for ms in [1_u64, 500, 1000, 1999] {
            wrapper_soak_sfp_insert_event(
                &mut events,
                &mut port_dict,
                t0 + Duration::from_millis(ms),
                delay,
            );
            assert!(port_dict.is_empty(), "insert must stay withheld at {ms} ms");
            assert_eq!(events.len(), 5);
        }

        // Once the delay elapses the inserts re-appear and the soak buffer drains.
        wrapper_soak_sfp_insert_event(&mut events, &mut port_dict, t0 + delay, delay);
        assert_eq!(
            port_dict, expected,
            "insert must re-appear once the delay elapses"
        );
        assert!(events.is_empty(), "soak buffer drains on re-injection");
    }

    // ← tests/test_xcvrd.py::test_sfp_remove_events: a remove arriving during the soak
    // window cancels the pending insert (nothing is ever re-injected) and passes through.
    #[test]
    fn soak_remove_cancels_pending_insert() {
        let delay = Duration::from_secs(MGMT_INIT_TIME_DELAY_SECS);
        let mut events: BTreeMap<String, Instant> = BTreeMap::new();
        let mut insert: BTreeMap<String, String> = (1..=5)
            .map(|i| (i.to_string(), SFP_STATUS_INSERTED.to_string()))
            .collect();
        let removal: BTreeMap<String, String> = (1..=5)
            .map(|i| (i.to_string(), SFP_STATUS_REMOVED.to_string()))
            .collect();
        let t0 = Instant::now();

        // Insert soaked (withheld) at t0.
        wrapper_soak_sfp_insert_event(&mut events, &mut insert, t0, delay);
        assert!(insert.is_empty());
        assert_eq!(events.len(), 5);

        // A removal 1 s into the window (before the 2 s delay) cancels the pending insert
        // and passes straight through unchanged.
        let mut port_dict = removal.clone();
        wrapper_soak_sfp_insert_event(
            &mut events,
            &mut port_dict,
            t0 + Duration::from_secs(1),
            delay,
        );
        assert!(
            events.is_empty(),
            "a remove in the window cancels the pending insert"
        );
        assert_eq!(port_dict, removal, "the removal passes through immediately");

        // Even past when the original delay would have elapsed, nothing is re-injected.
        let mut later = BTreeMap::new();
        wrapper_soak_sfp_insert_event(
            &mut events,
            &mut later,
            t0 + delay + Duration::from_secs(1),
            delay,
        );
        assert!(later.is_empty(), "a cancelled insert must never re-appear");
    }

    // New: a zero delay (the threaded-test fast-timing path) passes an insert straight
    // through in a single call — stamped then immediately re-injected — so task_worker
    // dispatches it at once. This is why the earlier task_worker tests stay green.
    #[test]
    fn soak_zero_delay_passes_insert_through() {
        let mut events: BTreeMap<String, Instant> = BTreeMap::new();
        let mut port_dict: BTreeMap<String, String> =
            BTreeMap::from([("3".to_string(), SFP_STATUS_INSERTED.to_string())]);

        wrapper_soak_sfp_insert_event(&mut events, &mut port_dict, Instant::now(), Duration::ZERO);

        assert_eq!(
            port_dict.get("3").map(String::as_str),
            Some(SFP_STATUS_INSERTED)
        );
        assert!(events.is_empty(), "zero-delay soak leaves nothing pending");
    }

    // New: within one poll, an insert is withheld while a remove on another port and an
    // error bitmap pass straight through (only the "1"/"0" codes are debounced).
    #[test]
    fn soak_mixed_codes_in_one_poll() {
        let delay = Duration::from_secs(MGMT_INIT_TIME_DELAY_SECS);
        let mut events: BTreeMap<String, Instant> = BTreeMap::new();
        let mut port_dict: BTreeMap<String, String> = BTreeMap::from([
            ("1".to_string(), SFP_STATUS_INSERTED.to_string()),
            ("2".to_string(), SFP_STATUS_REMOVED.to_string()),
            ("3".to_string(), "6".to_string()), // error bitmap
        ]);
        let t0 = Instant::now();

        wrapper_soak_sfp_insert_event(&mut events, &mut port_dict, t0, delay);

        // Insert withheld; remove + error pass straight through untouched.
        assert!(!port_dict.contains_key("1"), "insert is withheld");
        assert!(events.contains_key("1"));
        assert_eq!(
            port_dict.get("2").map(String::as_str),
            Some(SFP_STATUS_REMOVED)
        );
        assert_eq!(port_dict.get("3").map(String::as_str), Some("6"));
    }

    // New (routing): a fresh insert is ROUTED through the soak by task_worker — withheld
    // (buffered in sfp_insert_events, nothing published) for the whole observation window
    // when the delay is long. Proves inserts are no longer dispatched immediately.
    #[test]
    fn task_worker_withholds_fresh_insert() {
        let hal = MockHal::with_sfps(vec![MockSfp::present().with_info(cmis_info())]);
        hal.push_change_event(insert_event(0));
        let mut t = task(hal, port_mapping(&[("Ethernet0", 0)]));
        t.set_mgmt_init_time_delay(Duration::from_secs(30)); // long: withheld for the test

        run_briefly(&mut t, Duration::from_millis(150));

        // The insert was buffered, not dispatched: no INFO / STATUS_SW published yet.
        assert!(
            t.pending_insert_events().contains_key("0"),
            "fresh insert must be soaked into sfp_insert_events, got {:?}",
            t.pending_insert_events().keys().collect::<Vec<_>>()
        );
        assert_eq!(
            t.table_helper.get_intf_tbl(0).get_size_for_key("Ethernet0"),
            0
        );
        assert_eq!(
            t.table_helper
                .get_status_sw_tbl(0)
                .hget("Ethernet0", "status"),
            None
        );
    }

    // New (routing): once the soak delay elapses, the withheld insert is re-injected on a
    // subsequent idle (status=true) poll and THEN dispatched (INFO + STATUS_SW published,
    // buffer drained). A short delay keeps re-injection well inside the run_until deadline.
    #[test]
    fn task_worker_reinjects_insert_after_soak() {
        let hal = MockHal::with_sfps(vec![MockSfp::present().with_info(cmis_info())]);
        hal.push_change_event(insert_event(0));
        hal.set_idle_poll_ready(true); // idle polls report status=true so the soak re-checks
        let mut t = task(hal, port_mapping(&[("Ethernet0", 0)]));
        t.set_mgmt_init_time_delay(Duration::from_millis(60));

        let th = t.table_helper.clone();
        run_until(&mut t, || {
            th.get_intf_tbl(0)
                .hget("Ethernet0", "manufacturer")
                .as_deref()
                == Some("xcvr-emu")
        });

        // The re-injected insert was dispatched and the soak buffer drained.
        assert_eq!(
            th.get_status_sw_tbl(0)
                .hget("Ethernet0", "status")
                .as_deref(),
            Some("1")
        );
        assert!(
            t.pending_insert_events().is_empty(),
            "buffer drains once re-injected"
        );
    }

    // Drive the state-machine loop with a real insert event through the mock HAL.
    // ← tests/test_xcvrd.py::test_SfpStateUpdateTask_task_worker (insert branch): with the insert-event soak
    // the insert is routed through the (zero-delay, via set_fast_timing) soak before dispatch.
    #[test]
    fn task_worker_handles_insert_event() {
        let hal = MockHal::with_sfps(vec![MockSfp::present().with_info(cmis_info())]);
        hal.push_change_event(insert_event(0));
        let mut t = task(hal, port_mapping(&[("Ethernet0", 0)]));

        let th = t.table_helper.clone();
        run_until(&mut t, || {
            th.get_intf_tbl(0)
                .hget("Ethernet0", "manufacturer")
                .as_deref()
                == Some("xcvr-emu")
        });

        assert_eq!(
            th.get_status_sw_tbl(0)
                .hget("Ethernet0", "status")
                .as_deref(),
            Some("1")
        );
        assert_eq!(
            th.get_status_sw_tbl(0).hget("Ethernet0", "cmis_state"),
            None
        );
    }

    // ← tests/test_xcvrd.py::test_SfpStateUpdateTask_task_worker (remove branch): a removal
    // clears INFO + status=0 (a "0" passes straight through the insert-event soak, dispatched at once).
    #[test]
    fn task_worker_handles_remove_event() {
        let hal = MockHal::with_sfps(vec![MockSfp::default()]); // absent
        hal.push_change_event(remove_event(0));
        let mut t = task(hal, port_mapping(&[("Ethernet0", 0)]));
        // Seed a stale INFO row the removal must delete.
        t.table_helper
            .get_intf_tbl(0)
            .hset("Ethernet0", "manufacturer", "xcvr-emu");

        let th = t.table_helper.clone();
        run_until(&mut t, || {
            th.get_status_sw_tbl(0)
                .hget("Ethernet0", "status")
                .as_deref()
                == Some("0")
        });

        assert_eq!(th.get_intf_tbl(0).get_size_for_key("Ethernet0"), 0);
    }

    // ← tests/test_xcvrd.py::test_sfp_removal_from_dict (`mock_sfp.remove_xcvr_api
    // .assert_called_once()`): a plug-out must invalidate the plugin's cached xcvr_api so a
    // later re-insert re-reads the module EEPROM fresh. This is what lets xcvrd notice a
    // VDM/PM advertisement the harness provisions mid-test then re-plugs (e2e test_vdm /
    // test_vdm_statistic): `is_transceiver_vdm_supported` is `@read_only_cached_api_return`-
    // pinned on the api instance, so only dropping the api surfaces the freshly-advertised
    // VDM support — otherwise INFO.vdm_supported stays False and every VDM poster is gated off.
    #[test]
    fn task_worker_remove_invalidates_xcvr_api_cache() {
        let sfp = MockSfp::default(); // absent
                                      // The `call_log` Arc is shared with the clone `MockHal::sfp` hands the daemon, so a
                                      // `call_json("remove_xcvr_api")` issued during removal is observable here.
        let call_log = sfp.call_log.clone();
        let hal = MockHal::with_sfps(vec![sfp]);
        hal.push_change_event(remove_event(0));
        let mut t = task(hal, port_mapping(&[("Ethernet0", 0)]));
        t.table_helper
            .get_intf_tbl(0)
            .hset("Ethernet0", "manufacturer", "xcvr-emu");

        let th = t.table_helper.clone();
        run_until(&mut t, || {
            th.get_status_sw_tbl(0)
                .hget("Ethernet0", "status")
                .as_deref()
                == Some("0")
        });

        // INFO cleared (removal processed) AND the cache-invalidation call was issued.
        assert_eq!(th.get_intf_tbl(0).get_size_for_key("Ethernet0"), 0);
        assert!(
            call_log
                .lock()
                .unwrap()
                .iter()
                .any(|m| m == "remove_xcvr_api"),
            "plug-out must call remove_xcvr_api on the physical port's SFP; call_log={:?}",
            call_log.lock().unwrap()
        );
    }

    // ← tests/test_xcvrd.py::test_SfpStateUpdateTask_task_worker (error branch): an error
    // bitmap decodes to the joined STATUS_SW.error and drops the DOM rows (INFO kept).
    #[test]
    fn task_worker_handles_error_event() {
        let hal = MockHal::with_sfps(vec![MockSfp::present().with_info(cmis_info())]);
        // 0x02 (blocking) | 0x04 (power budget) = 6.
        let mut sfp = BTreeMap::new();
        sfp.insert("0".to_string(), "6".to_string());
        hal.push_change_event(ChangeEvent {
            status: true,
            sfp,
            sfp_error: BTreeMap::new(),
        });
        let mut t = task(hal, port_mapping(&[("Ethernet0", 0)]));
        // Seed INFO (kept) + a DOM row (dropped by the blocking error).
        t.table_helper
            .get_intf_tbl(0)
            .hset("Ethernet0", "manufacturer", "xcvr-emu");
        t.table_helper
            .get_dom_tbl(0)
            .hset("Ethernet0", "temperature", "22.75");

        let th = t.table_helper.clone();
        run_until(&mut t, || {
            th.get_status_sw_tbl(0).hget("Ethernet0", "error").is_some()
        });

        assert_eq!(
            th.get_status_sw_tbl(0)
                .hget("Ethernet0", "error")
                .as_deref(),
            Some("Blocking EEPROM from being read|Power budget exceeded")
        );
        assert_eq!(
            th.get_status_sw_tbl(0)
                .hget("Ethernet0", "status")
                .as_deref(),
            Some("6")
        );
        // Blocking error drops DOM but keeps the static INFO row.
        assert_eq!(th.get_dom_tbl(0).get_size_for_key("Ethernet0"), 0);
        assert_eq!(
            th.get_intf_tbl(0)
                .hget("Ethernet0", "manufacturer")
                .as_deref(),
            Some("xcvr-emu")
        );
    }

    // task_worker stops promptly when the stop flag is set (← test_task_run_stop).
    #[test]
    fn task_worker_stops_on_flag() {
        let hal = MockHal::with_sfps(vec![MockSfp::default()]);
        let mut t = task(hal, port_mapping(&[("Ethernet0", 0)]));
        let stop = Arc::new(AtomicBool::new(false));
        let err = Arc::new(AtomicBool::new(false));

        let s2 = stop.clone();
        let handle = std::thread::spawn(move || {
            t.task_worker(&s2, &err);
            t
        });
        std::thread::sleep(Duration::from_millis(20));
        stop.store(true, Ordering::SeqCst);
        let _t = handle.join().expect("task_worker thread joins after stop");
    }

    // run() returns immediately (does NOT init / publish) when stop is already set before the
    // thread starts — the reference `SfpStateUpdateTask.run` guard (xcvrd.py:697). A task whose
    // thread is spawned after shutdown began must do no STATE_DB work (← test_task_run_stop).
    #[test]
    fn run_returns_immediately_when_stop_preset() {
        let hal = MockHal::with_sfps(vec![MockSfp::present().with_info(cmis_info())]);
        let t = task(hal, port_mapping(&[("Ethernet0", 0)]));
        let th = t.table_helper.clone();
        let stop = Arc::new(AtomicBool::new(true)); // already shutting down
        let err = Arc::new(AtomicBool::new(false));

        // Must return promptly and publish nothing (init is skipped).
        let handle = std::thread::spawn(move || t.run(stop, err));
        std::thread::sleep(Duration::from_millis(50));
        assert!(
            handle.is_finished(),
            "run() must return at once when stop is preset"
        );
        handle.join().expect("run thread joins");
        assert_eq!(
            th.get_intf_tbl(0).get_size_for_key("Ethernet0"),
            0,
            "run() must not publish TRANSCEIVER_INFO when stop was already set"
        );
    }

    /// Spawn `task_worker` on a thread, wait until `cond` holds (or time out), then stop
    /// and join — the Rust analogue of the Python tests pumping events then asserting.
    fn run_until(t: &mut SfpStateUpdateTask, cond: impl Fn() -> bool) {
        // Move the task onto a worker thread; it is handed back on join.
        let owned = std::mem::replace(
            t,
            SfpStateUpdateTask::new(
                Vec::new(),
                PortMapping::new(),
                Arc::new(MockHal::default()),
                Arc::new(XcvrTableHelper::with_mock_tables(&[String::new()])),
            ),
        );
        let stop = Arc::new(AtomicBool::new(false));
        let err = Arc::new(AtomicBool::new(false));
        let s2 = stop.clone();
        let handle = std::thread::spawn(move || {
            let mut owned = owned;
            owned.task_worker(&s2, &err);
            owned
        });

        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline && !cond() {
            std::thread::sleep(Duration::from_millis(2));
        }
        stop.store(true, Ordering::SeqCst);
        let owned = handle.join().expect("task_worker thread joins");
        *t = owned;
        assert!(cond(), "expected task_worker effect within the deadline");
    }

    /// Spawn `task_worker`, let it run for `dur`, then stop + join, handing the task back.
    /// Unlike [`run_until`] this asserts no condition — used to observe a *withheld* insert
    /// (the soak must NOT dispatch it within the window).
    fn run_briefly(t: &mut SfpStateUpdateTask, dur: Duration) {
        let owned = std::mem::replace(
            t,
            SfpStateUpdateTask::new(
                Vec::new(),
                PortMapping::new(),
                Arc::new(MockHal::default()),
                Arc::new(XcvrTableHelper::with_mock_tables(&[String::new()])),
            ),
        );
        let stop = Arc::new(AtomicBool::new(false));
        let err = Arc::new(AtomicBool::new(false));
        let s2 = stop.clone();
        let handle = std::thread::spawn(move || {
            let mut owned = owned;
            owned.task_worker(&s2, &err);
            owned
        });
        std::thread::sleep(dur);
        stop.store(true, Ordering::SeqCst);
        *t = handle.join().expect("task_worker thread joins");
    }

    // --- CONFIG_DB logical-port lifecycle -----------------------------------

    fn config_event(name: &str, phys: usize, ty: PortChangeEventType) -> PortChangeEvent {
        PortChangeEvent::new(
            name.to_string(),
            Some(phys),
            0,
            ty,
            "CONFIG_DB".to_string(),
            "PORT".to_string(),
        )
    }

    // ← tests/test_xcvrd.py::test_SfpStateUpdateTask_on_add_logical_port (case 1):
    // present but the identity EEPROM read fails → status INSERTED/N/A, the port is queued
    // for retry, and neither INFO nor thresholds are published. NPU_SI is reseeded default.
    #[test]
    fn on_add_logical_port_present_eeprom_not_ready_joins_retry() {
        let hal = MockHal::with_sfps(vec![MockSfp::present()]); // present, null info
        let mut t = task(hal, port_mapping(&[("Ethernet0", 0)]));
        let stop = AtomicBool::new(false);

        t.on_add_logical_port(
            &config_event("Ethernet0", 0, PortChangeEventType::Add),
            &stop,
        );

        let th = t.table_helper.clone();
        assert_eq!(
            th.get_status_sw_tbl(0)
                .hget("Ethernet0", "status")
                .as_deref(),
            Some(SFP_STATUS_INSERTED)
        );
        assert_eq!(
            th.get_status_sw_tbl(0)
                .hget("Ethernet0", "error")
                .as_deref(),
            Some("N/A")
        );
        assert!(t.retry_eeprom_set().contains("Ethernet0"));
        assert_eq!(th.get_intf_tbl(0).get_size_for_key("Ethernet0"), 0);
        assert_eq!(th.get_dom_threshold_tbl(0).get_size_for_key("Ethernet0"), 0);
        assert_eq!(
            th.get_state_port_tbl(0)
                .hget("Ethernet0", NPU_SI_SETTINGS_SYNC_STATUS_KEY)
                .as_deref(),
            Some(NPU_SI_SETTINGS_DEFAULT_VALUE)
        );
    }

    // ← test_SfpStateUpdateTask_on_add_logical_port (case 2): present + readable → publish
    // INFO + DOM thresholds, port not queued for retry.
    #[test]
    fn on_add_logical_port_present_readable_publishes_info_and_thresholds() {
        let sfp = MockSfp::present()
            .with_info(cmis_info())
            .with_threshold_info(json!({ "temphighalarm": 75.0 }));
        let hal = MockHal::with_sfps(vec![sfp]);
        let mut t = task(hal, port_mapping(&[("Ethernet0", 0)]));
        let stop = AtomicBool::new(false);

        t.on_add_logical_port(
            &config_event("Ethernet0", 0, PortChangeEventType::Add),
            &stop,
        );

        let th = t.table_helper.clone();
        assert_eq!(
            th.get_status_sw_tbl(0)
                .hget("Ethernet0", "status")
                .as_deref(),
            Some(SFP_STATUS_INSERTED)
        );
        assert_eq!(
            th.get_status_sw_tbl(0)
                .hget("Ethernet0", "error")
                .as_deref(),
            Some("N/A")
        );
        assert_eq!(
            th.get_intf_tbl(0)
                .hget("Ethernet0", "manufacturer")
                .as_deref(),
            Some("xcvr-emu")
        );
        assert!(!t.retry_eeprom_set().contains("Ethernet0"));
        assert_eq!(
            th.get_dom_threshold_tbl(0)
                .hget("Ethernet0", "temphighalarm")
                .as_deref(),
            Some("75.0")
        );
        // cmis_state stays owned by the CmisManagerTask, never written here.
        assert_eq!(
            th.get_status_sw_tbl(0).hget("Ethernet0", "cmis_state"),
            None
        );
    }

    // ← test_SfpStateUpdateTask_on_add_logical_port (case 3): SFP absent → status REMOVED.
    #[test]
    fn on_add_logical_port_absent_marks_removed() {
        let hal = MockHal::with_sfps(vec![MockSfp::absent()]);
        let mut t = task(hal, port_mapping(&[("Ethernet0", 0)]));
        let stop = AtomicBool::new(false);

        t.on_add_logical_port(
            &config_event("Ethernet0", 0, PortChangeEventType::Add),
            &stop,
        );

        let th = t.table_helper.clone();
        assert_eq!(
            th.get_status_sw_tbl(0)
                .hget("Ethernet0", "status")
                .as_deref(),
            Some(SFP_STATUS_REMOVED)
        );
        assert_eq!(
            th.get_status_sw_tbl(0)
                .hget("Ethernet0", "error")
                .as_deref(),
            Some("N/A")
        );
        assert_eq!(th.get_intf_tbl(0).get_size_for_key("Ethernet0"), 0);
    }

    // ← test_SfpStateUpdateTask_on_add_logical_port (case 4): absent + a cached blocking
    // SFP error → the decoded error is recorded and the status is the raw error value.
    #[test]
    fn on_add_logical_port_error_status_records_decoded_error() {
        let hal = MockHal::with_sfps(vec![MockSfp::absent()]);
        let mut t = task(hal, port_mapping(&[("Ethernet0", 0)]));
        // 0x02 (blocking) | 0x04 (power budget) = 6, cached for physical port 0.
        t.set_sfp_error("0", "6");
        let stop = AtomicBool::new(false);

        t.on_add_logical_port(
            &config_event("Ethernet0", 0, PortChangeEventType::Add),
            &stop,
        );

        let th = t.table_helper.clone();
        assert_eq!(
            th.get_status_sw_tbl(0)
                .hget("Ethernet0", "status")
                .as_deref(),
            Some("6")
        );
        assert_eq!(
            th.get_status_sw_tbl(0)
                .hget("Ethernet0", "error")
                .as_deref(),
            Some("Blocking EEPROM from being read|Power budget exceeded")
        );
    }

    // ← tests/test_xcvrd.py::test_sfp_removal_from_dict / on_remove_logical_port: a logical
    // -port delete tears down the WHOLE TRANSCEIVER_* set (INFO + STATUS_SW included) and
    // drops the port from the EEPROM retry set.
    #[test]
    fn on_remove_logical_port_deletes_whole_table_set() {
        let hal = MockHal::with_sfps(vec![MockSfp::present().with_info(cmis_info())]);
        let mut t = task(hal, port_mapping(&[("Ethernet0", 0)]));
        let th = t.table_helper.clone();
        // Seed a row in a representative table from each category.
        th.get_intf_tbl(0)
            .hset("Ethernet0", "manufacturer", "xcvr-emu");
        th.get_dom_tbl(0).hset("Ethernet0", "temperature", "22.0");
        th.get_dom_threshold_tbl(0)
            .hset("Ethernet0", "temphighalarm", "75.0");
        th.get_status_tbl(0).hset("Ethernet0", "status", "1");
        th.get_status_sw_tbl(0).hset("Ethernet0", "status", "1");
        th.get_pm_tbl(0).hset("Ethernet0", "prefec_ber_avg", "0.0");
        th.get_firmware_info_tbl(0)
            .hset("Ethernet0", "active_firmware", "1.0");
        for tt in VDM_THRESHOLD_TYPES {
            th.get_vdm_threshold_tbl(0, tt).hset("Ethernet0", "x", "1");
        }
        t.insert_retry_port("Ethernet0");

        t.on_remove_logical_port(&config_event("Ethernet0", 0, PortChangeEventType::Remove));

        for tbl in [
            th.get_intf_tbl(0),
            th.get_dom_tbl(0),
            th.get_dom_threshold_tbl(0),
            th.get_status_tbl(0),
            th.get_status_sw_tbl(0),
            th.get_pm_tbl(0),
            th.get_firmware_info_tbl(0),
        ] {
            assert_eq!(tbl.get_size_for_key("Ethernet0"), 0);
        }
        for tt in VDM_THRESHOLD_TYPES {
            assert_eq!(
                th.get_vdm_threshold_tbl(0, tt)
                    .get_size_for_key("Ethernet0"),
                0
            );
        }
        assert!(!t.retry_eeprom_set().contains("Ethernet0"));
    }

    // ← tests/test_xcvrd.py::test_handle_port_config_change: a PORT SET then DEL drives the
    // logical-port mapping through on_port_config_change — first populated, then fully cleared.
    #[test]
    fn on_port_config_change_add_then_remove_updates_mapping() {
        let hal = MockHal::with_sfps(vec![MockSfp::absent(), MockSfp::absent()]);
        let mut t = task(hal, PortMapping::new());
        let stop = AtomicBool::new(false);

        t.on_port_config_change(
            &config_event("Ethernet0", 1, PortChangeEventType::Add),
            &stop,
        );
        assert!(t
            .port_mapping()
            .logical_port_list()
            .contains(&"Ethernet0".to_string()));
        assert_eq!(
            t.port_mapping().get_asic_id_for_logical_port("Ethernet0"),
            Some(0)
        );
        assert_eq!(
            t.port_mapping().get_physical_to_logical(1),
            Some(vec!["Ethernet0".to_string()])
        );
        assert_eq!(
            t.port_mapping().get_logical_to_physical("Ethernet0"),
            Some(vec![1])
        );

        t.on_port_config_change(
            &config_event("Ethernet0", 1, PortChangeEventType::Remove),
            &stop,
        );
        assert!(t.port_mapping().logical_port_list().is_empty());
        assert_eq!(t.port_mapping().get_logical_to_physical("Ethernet0"), None);
        assert_eq!(t.port_mapping().get_physical_to_logical(1), None);
        assert_eq!(
            t.port_mapping().get_asic_id_for_logical_port("Ethernet0"),
            None
        );
    }
}
