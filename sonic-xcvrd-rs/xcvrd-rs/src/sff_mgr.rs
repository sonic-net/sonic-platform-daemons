//! `sff_mgr.py` → `SffManagerTask` (optional SFF-8472/8636 deterministic link
//! bring-up) + `SffLoggerForPortUpdateEvent` (analysis §1.1, §3.2). Off by default
//! (`--enable_sff_mgr`).
//!
//! The SFF manager makes SFF-compliant (non-CMIS) modules come up deterministically:
//! TX is enabled only once `host_tx_ready` is true AND `admin_status` is up, and
//! disabled otherwise; on insertion it also enables the module's high power class and
//! takes it out of low-power mode. It watches the reference `PORT_TBL_MAP`
//! (`CONFIG_DB PORT` + `STATE_DB TRANSCEIVER_INFO`/`type` + `STATE_DB PORT_TABLE`/
//! `host_tx_ready`) via a [`MultiPortChangeObserver`] and, for each configured logical
//! port, drives the module's [`SffApi`] control surface.
//!
//! The Python `SffManagerTask` calls the concrete `sonic_platform_base` xcvr api
//! (`Sff8636Api`/`Sff8472Api`) directly. As with the CMIS seam, that control surface is
//! abstracted behind the mockable [`SffApi`] trait: production wraps a bridge
//! [`crate::hal::SfpHandle`] in [`BridgeSffApi`] (raw SFF-8636 register reads/writes),
//! while Part-B unit tests inject [`MockSffApi`] (canned/settable returns + call
//! counters), the analogue of the Python tests' `MagicMock()` api.
#![allow(dead_code, unused_variables, unused_imports)]

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use crate::cmis::cmis_manager_task::CMIS_MODULE_TYPES;
use crate::hal::{Hal, SfpHandle};
use crate::xcvrd_utilities::port_event_helper::{
    MultiPortChangeObserver, PortChangeEvent, PortChangeEventType,
};
use crate::xcvrd_utilities::xcvr_table_helper::XcvrTableHelper;

/// Reference `SffManagerTask.SFF_LOGGER_PREFIX`.
const SFF_LOGGER_PREFIX: &str = "SFF-MAIN: ";
/// `SffManagerTask.DEFAULT_NUM_LANES_PER_PPORT` (QSFP28/QSFP+).
const DEFAULT_NUM_LANES_PER_PPORT: i64 = 4;
/// `PortChangeObserver` select timeout (ms) — production `task_worker` cadence.
const PORT_UPDATE_SELECT_TIMEOUT_MS: u64 = 1000;

// =====================================================================================
// SffApi — the mockable SFF control surface (analogue of the CMIS seam).
// =====================================================================================

/// The SFF-8472/8636 control/decode surface the [`SffManagerTask`] bring-up loop drives
/// (`api.*` in `sff_mgr.py`). Split from [`SfpHandle`] so Part-B tests inject
/// [`MockSffApi`]; production wraps a bridge handle in [`BridgeSffApi`]. `Option`/`None`
/// returns model the Python `AttributeError`/`NotImplementedError`/`None` paths the task
/// treats as "skip this port".
pub trait SffApi {
    /// `common.is_cmis_api(api)` — a paged-CMIS module api (the SFF task skips it, the
    /// CMIS manager owns it).
    fn is_cmis(&self) -> bool;
    /// `api.is_copper()` — `None` mirrors `AttributeError`/`NotImplementedError` (skip).
    fn is_copper(&self) -> Option<bool>;
    /// `api.get_tx_disable_support()` — `None` mirrors `AttributeError`/`NotImplementedError`.
    fn get_tx_disable_support(&self) -> Option<bool>;
    /// `api.get_power_class()` — `None` mirrors the Python `None` return (log + skip).
    fn get_power_class(&self) -> Option<i64>;
    /// `api.set_high_power_class(power_class, enable)` — `None` mirrors
    /// `AttributeError`/`NotImplementedError` (the Python `except (...): pass`).
    fn set_high_power_class(&self, power_class: i64, enable: bool) -> Option<bool>;
    /// `api.get_lpmode_support()`.
    fn get_lpmode_support(&self) -> bool;
    /// Take the module out of / into low-power mode. Production routes an `Sff8472Api`
    /// through `sfp.set_lpmode` and every other api through `api.set_lpmode` (the Python
    /// `isinstance(api, Sff8472Api)` branch); [`BridgeSffApi`] owns that decision.
    fn set_lpmode(&self, lpmode: bool) -> bool;
    /// `api.get_tx_disable()` — per-lane tx-disable flags (`True` = disabled), `None` on
    /// a read error (the task then best-effort-forces every interested lane).
    fn get_tx_disable(&self) -> Option<Vec<bool>>;
    /// `api.tx_disable_channel(mask, disable)`.
    fn tx_disable_channel(&self, media_lanes_mask: u32, disable: bool) -> bool;
}

/// Factory that turns a HAL `SfpHandle` into an [`SffApi`] (production = [`BridgeSffApi`];
/// unit tests inject a [`MockSffApi`]). `None` mirrors Python `sfp.get_xcvr_api()`
/// returning `None` (no api for this port → the task logs and skips).
pub type SffApiFactory = Box<dyn Fn(Box<dyn SfpHandle>) -> Option<Box<dyn SffApi>> + Send + Sync>;

// =====================================================================================
// BridgeSffApi — production impl over a bridge SfpHandle (raw SFF-8636 registers).
// =====================================================================================

// SFF-8636 lower-memory (page 00h) control registers — flat linear offset == byte offset.
const SFF_TX_DISABLE_OFFSET: usize = 86; // 00h:86 Tx_Disable (1 bit / lane, lane1=bit0)
const SFF_LPMODE_HP_CTRL_OFFSET: usize = 93; // 00h:93 Power/LPMode + High Power Class Enable
const SFF_POWER_CLASS_OFFSET: usize = 129; // 00h:129 Extended Identifier (power class)
const SFF_DEVICE_TECH_OFFSET: usize = 147; // 00h:147 Device technology (transmitter tech)
const SFF_OPTIONS_OFFSET: usize = 195; // 00h:195 Options (bit4 = Tx_Disable implemented)
const SFF_HIGH_POWER_CLASS_5_7_BIT: u8 = 0x04; // 00h:93 bit2
const SFF_HIGH_POWER_CLASS_8_BIT: u8 = 0x08; // 00h:93 bit3
const SFF_NUM_LANES: usize = 4;

/// Production [`SffApi`] backed by a bridge [`SfpHandle`]: the raw SFF-8636 page-00h
/// register reads/writes the concrete `Sff8636Api` performs (Tx_Disable 00h:86, power
/// class 00h:129, High Power Class Enable 00h:93). Off by default (`--enable_sff_mgr`),
/// so this is a best-effort port of the SFF-8636 control path; the reference testbed
/// never enables the task (there is no e2e parity target), and every method degrades to
/// a safe skip on an unreadable register.
pub struct BridgeSffApi {
    sfp: Box<dyn SfpHandle>,
}

impl BridgeSffApi {
    pub fn new(sfp: Box<dyn SfpHandle>) -> Self {
        BridgeSffApi { sfp }
    }

    fn read_byte(&self, off: usize) -> Option<u8> {
        self.sfp
            .read_eeprom(off, 1)
            .ok()
            .flatten()
            .and_then(|v| v.first().copied())
    }

    fn write_byte(&self, off: usize, byte: u8) -> bool {
        self.sfp.write_eeprom(off, &[byte]).unwrap_or(false)
    }
}

impl SffApi for BridgeSffApi {
    fn is_cmis(&self) -> bool {
        // A paged-CMIS module type (QSFP-DD/OSFP/…) is owned by the CMIS manager. Detect
        // it the same way the CMIS manager does — the decoded `type_abbrv_name`.
        //
        // Ask the module's decode api directly. Sourcing this from the aggregate
        // `get_transceiver_info()` cost 60 ms per port per sweep for an answer the api
        // gives for free, and it is the FIRST thing this task does for every port — so on
        // an all-CMIS box (514/514 here) every sweep burned ~31 s doing nothing but
        // deciding to return. Falls back to the aggregate when the seam is unavailable
        // (mocks), so the existing tests keep covering that path.
        let abbrv = match self.sfp.xcvr_api_call_json("get_module_type_abbreviation") {
            Ok(v) if v.is_string() => v.as_str().map(str::to_string),
            _ => self.sfp.get_transceiver_info().ok().and_then(|info| {
                info.get("type_abbrv_name")
                    .and_then(|v| v.as_str())
                    .map(str::to_string)
            }),
        };
        abbrv
            .map(|t| CMIS_MODULE_TYPES.contains(&t.as_str()))
            .unwrap_or(false)
    }

    fn is_copper(&self) -> Option<bool> {
        // SFF-8636 Table 6-18: transmitter technology (00h:147 bits 7:4) codes >= 0xA are
        // copper cables (unequalized/passive/active). Unreadable → optical (proceed).
        match self.read_byte(SFF_DEVICE_TECH_OFFSET) {
            Some(b) => Some((b >> 4) >= 0x0A),
            None => Some(false),
        }
    }

    fn get_tx_disable_support(&self) -> Option<bool> {
        // 00h:195 bit4 = Tx_Disable implemented. Unreadable → not supported (skip).
        match self.read_byte(SFF_OPTIONS_OFFSET) {
            Some(b) => Some(b & 0x10 != 0),
            None => Some(false),
        }
    }

    fn get_power_class(&self) -> Option<i64> {
        // SFF-8636 §6.2.6 Extended Identifier (00h:129): bits 1:0 encode power classes
        // 5-7, bit2 (0x04) power class 8, bits 7:6 power classes 1-4.
        let b = self.read_byte(SFF_POWER_CLASS_OFFSET)?;
        let class = if b & 0x03 != 0 {
            4 + (b & 0x03) as i64 // 5, 6 or 7
        } else if b & 0x04 != 0 {
            8
        } else {
            ((b >> 6) & 0x03) as i64 + 1 // 1..4
        };
        Some(class)
    }

    fn set_high_power_class(&self, power_class: i64, enable: bool) -> Option<bool> {
        // High Power Class Enable at 00h:93 bit2 (classes 5-7) / bit3 (class 8).
        let mut b = self.read_byte(SFF_LPMODE_HP_CTRL_OFFSET).unwrap_or(0);
        if enable {
            b |= SFF_HIGH_POWER_CLASS_5_7_BIT;
            if power_class >= 8 {
                b |= SFF_HIGH_POWER_CLASS_8_BIT;
            }
        } else {
            b &= !(SFF_HIGH_POWER_CLASS_5_7_BIT | SFF_HIGH_POWER_CLASS_8_BIT);
        }
        Some(self.write_byte(SFF_LPMODE_HP_CTRL_OFFSET, b))
    }

    fn get_lpmode_support(&self) -> bool {
        true
    }

    fn set_lpmode(&self, lpmode: bool) -> bool {
        // The bridge `SfpOptoeBase.set_lpmode` drives the correct register for both
        // SFF-8636 (00h:93) and SFF-8472, matching the Python `sfp.set_lpmode` /
        // `api.set_lpmode` split.
        self.sfp.set_lpmode(lpmode).unwrap_or(false)
    }

    fn get_tx_disable(&self) -> Option<Vec<bool>> {
        let b = self.read_byte(SFF_TX_DISABLE_OFFSET)?;
        Some(
            (0..SFF_NUM_LANES)
                .map(|lane| b & (1 << lane) != 0)
                .collect(),
        )
    }

    fn tx_disable_channel(&self, media_lanes_mask: u32, disable: bool) -> bool {
        let mut b = self.read_byte(SFF_TX_DISABLE_OFFSET).unwrap_or(0);
        for lane in 0..SFF_NUM_LANES {
            if media_lanes_mask & (1 << lane) != 0 {
                if disable {
                    b |= 1 << lane;
                } else {
                    b &= !(1u8 << lane);
                }
            }
        }
        self.write_byte(SFF_TX_DISABLE_OFFSET, b)
    }
}

// =====================================================================================
// SffPortInfo — the Python `port_dict[lport]` sub-dict.
// =====================================================================================

/// Per-logical-port state accumulated from `on_port_update_event`. Field presence
/// (`Option::is_some`) mirrors the Python `'<key>' in port_dict[lport]` checks; the
/// derived `PartialEq` backs the reference `port_dict == port_dict_prev` no-change test.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct SffPortInfo {
    pub asic_id: usize,
    pub index: Option<i64>,
    pub subport: Option<String>,
    pub lanes: Option<Vec<String>>,
    pub host_tx_ready: Option<String>,
    pub admin_status: Option<String>,
    pub xcvr_type: Option<String>,
    pub active_lanes: Option<Vec<bool>>,
}

// =====================================================================================
// SffManagerTask
// =====================================================================================

/// `SffManagerTask` — tx_disable per `host_tx_ready`/`admin_status`, lpmode-disable,
/// high-power-class enable for SFF (non-CMIS) modules.
pub struct SffManagerTask {
    namespaces: Vec<String>,
    hal: Arc<dyn Hal>,
    xcvr_table_helper: Arc<XcvrTableHelper>,
    api_factory: SffApiFactory,
    /// `port_dict` — per logical port, keyed by name; entry removed on CONFIG_DB PORT DEL.
    port_dict: HashMap<String, SffPortInfo>,
    /// `port_dict_prev` — snapshot from the previous sweep for change detection.
    port_dict_prev: HashMap<String, SffPortInfo>,
}

impl SffManagerTask {
    pub fn new(
        namespaces: Vec<String>,
        hal: Arc<dyn Hal>,
        xcvr_table_helper: Arc<XcvrTableHelper>,
        api_factory: SffApiFactory,
    ) -> Self {
        SffManagerTask {
            namespaces,
            hal,
            xcvr_table_helper,
            api_factory,
            port_dict: HashMap::new(),
            port_dict_prev: HashMap::new(),
        }
    }

    fn log_notice(&self, message: &str) {
        eprintln!("{SFF_LOGGER_PREFIX}{message}");
    }

    fn log_warning(&self, message: &str) {
        eprintln!("{SFF_LOGGER_PREFIX}{message}");
    }

    fn log_error(&self, message: &str) {
        eprintln!("{SFF_LOGGER_PREFIX}{message}");
    }

    /// `get_active_lanes_for_lport(lport, subport_idx, num_lanes_per_lport,
    /// num_lanes_per_pport)` — the boolean lane-ownership mask for a (breakout) subport.
    /// `subport_idx` 0 means the port owns all lanes; otherwise lanes
    /// `[(idx-1)*per_lport .. idx*per_lport)`. `None` on an out-of-range subport.
    pub fn get_active_lanes_for_lport(
        &self,
        lport: &str,
        subport_idx: i64,
        num_lanes_per_lport: i64,
        num_lanes_per_pport: i64,
    ) -> Option<Vec<bool>> {
        // Guard against a zero divisor (Python would raise ZeroDivisionError and take the
        // task down; we stay resilient and treat it as an invalid input).
        if num_lanes_per_lport <= 0
            || subport_idx < 0
            || subport_idx > num_lanes_per_pport / num_lanes_per_lport
        {
            self.log_error(&format!(
                "{lport}: Invalid subport_idx {subport_idx} for \
                 num_lanes_per_lport={num_lanes_per_lport}, \
                 num_lanes_per_pport={num_lanes_per_pport}"
            ));
            return None;
        }

        let n = num_lanes_per_pport.max(0) as usize;
        if subport_idx == 0 {
            return Some(vec![true; n]);
        }

        let mut lanes = vec![false; n];
        let start = ((subport_idx - 1) * num_lanes_per_lport) as usize;
        let count = num_lanes_per_lport as usize;
        for lane in lanes.iter_mut().skip(start).take(count) {
            *lane = true;
        }
        Some(lanes)
    }

    /// `get_host_tx_status(lport, asic_index)` — STATE_DB `PORT_TABLE`/`host_tx_ready`
    /// (absent → `"false"`).
    pub fn get_host_tx_status(&self, lport: &str, asic_index: usize) -> String {
        self.xcvr_table_helper
            .get_state_port_tbl(asic_index)
            .hget(lport, "host_tx_ready")
            .unwrap_or_else(|| "false".to_string())
    }

    /// `get_admin_status(lport, asic_index)` — CONFIG_DB `PORT`/`admin_status`
    /// (absent → `"down"`).
    pub fn get_admin_status(&self, lport: &str, asic_index: usize) -> String {
        self.xcvr_table_helper
            .get_cfg_port_tbl(asic_index)
            .hget(lport, "admin_status")
            .unwrap_or_else(|| "down".to_string())
    }

    /// `calculate_tx_disable_delta_array` — per active lane, `True` where the current
    /// tx_disable flag differs from the target (inactive lanes never change).
    pub fn calculate_tx_disable_delta_array(
        &self,
        cur_tx_disable_array: &[bool],
        tx_disable_flag: bool,
        active_lanes: &[bool],
    ) -> Vec<bool> {
        active_lanes
            .iter()
            .zip(cur_tx_disable_array.iter())
            .map(|(&active, &cur)| {
                if active {
                    tx_disable_flag != cur
                } else {
                    false
                }
            })
            .collect()
    }

    /// `convert_bool_array_to_bit_mask` — LSB-first bitmask (item 0 → bit 0).
    pub fn convert_bool_array_to_bit_mask(&self, bool_array: &[bool]) -> u32 {
        let mut mask = 0u32;
        for (i, &flag) in bool_array.iter().enumerate() {
            if flag {
                mask |= 1 << i;
            }
        }
        mask
    }

    /// `enable_high_power_class(xcvr_api, lport)` — SFF-8636 §6.2.6: power classes 5-8 must
    /// have High Power Class Enable set or they cap at class-4 power. No-op for class < 5;
    /// an api that lacks the routines (`None`) is silently skipped.
    pub fn enable_high_power_class(&self, api: &dyn SffApi, lport: &str) {
        let power_class = match api.get_power_class() {
            Some(p) => p,
            None => {
                self.log_error(&format!("{lport}: failed to get power class"));
                return;
            }
        };
        if power_class < 5 {
            return;
        }
        match api.set_high_power_class(power_class, true) {
            Some(true) => self.log_notice(&format!("{lport}: done enabling high power class")),
            Some(false) => self.log_error(&format!("{lport}: failed to enable high power class")),
            // `AttributeError`/`NotImplementedError` (`except (...): pass`).
            None => {}
        }
    }

    /// `on_port_update_event` — soak a CONFIG/STATE PORT `SET`/`DEL` into `port_dict`.
    ///
    /// Unlike the CMIS manager, the SFF task addresses the SFP by the raw CONFIG_DB
    /// `index` the observer resolves into `physical_port` (the Python `port_index`), so no
    /// [`crate::xcvrd_utilities::port_event_helper::PortMapping`] enrichment is used. A
    /// `TRANSCEIVER_INFO` SET/DEL carries no `index` (`physical_port == None`); it must
    /// still update/clear `type`, so — unlike the reference's dead `pport is None` guard
    /// (its `port_index` defaults to `-1`, never `None`) — no early return on a missing
    /// physical port is applied here.
    pub fn on_port_update_event(&mut self, ev: &PortChangeEvent) {
        if !matches!(
            ev.event_type,
            PortChangeEventType::Set | PortChangeEventType::Del
        ) {
            return;
        }
        let lport = &ev.port_name;
        // Skip if it's not a physical (front-panel) port.
        if !lport.starts_with("Ethernet") {
            return;
        }

        if ev.event_type == PortChangeEventType::Set {
            let entry = self.port_dict.entry(lport.clone()).or_default();
            if let Some(p) = ev.physical_port {
                entry.index = Some(p as i64);
            }
            if let Some(v) = ev.port_dict.get("subport") {
                entry.subport = Some(v.clone());
            }
            if let Some(v) = ev.port_dict.get("lanes") {
                entry.lanes = Some(v.split(',').map(|s| s.to_string()).collect());
            }
            if let Some(v) = ev.port_dict.get("host_tx_ready") {
                entry.host_tx_ready = Some(v.clone());
            }
            if let Some(v) = ev.port_dict.get("admin_status") {
                entry.admin_status = Some(v.clone());
            }
            if let Some(v) = ev.port_dict.get("type") {
                entry.xcvr_type = Some(v.clone());
            }
            entry.asic_id = ev.asic_id;
        } else if ev.db_name == "CONFIG_DB" {
            // Only a CONFIG_DB PORT delete removes the whole entry.
            self.port_dict.remove(lport);
        } else if ev.table_name == "TRANSCEIVER_INFO" {
            // A TRANSCEIVER_INFO DEL is a transceiver removal (not a port removal): drop
            // just the `type` field so the port is treated as "no xcvr present".
            if let Some(entry) = self.port_dict.get_mut(lport) {
                entry.xcvr_type = None;
            }
        }
    }

    fn clear_xcvr_type(&mut self, lport: &str) {
        if let Some(entry) = self.port_dict.get_mut(lport) {
            entry.xcvr_type = None;
        }
    }

    /// One logical-port pass — the body of the reference `task_worker` per-port loop
    /// (`sff_mgr.py:367-528`). Split out so unit tests can drive it via
    /// [`Self::process_ports_once`] without a live observer.
    fn process_single_lport(&mut self, lport: &str) {
        let (pport, subport_idx, lanes_list, mut active_lanes, xcvr_type, asic_id) = {
            let d = &self.port_dict[lport];
            (
                d.index.unwrap_or(-1),
                d.subport
                    .as_deref()
                    .and_then(|s| s.trim().parse::<i64>().ok())
                    .unwrap_or(0),
                d.lanes.clone(),
                d.active_lanes.clone(),
                d.xcvr_type.clone(),
                d.asic_id,
            )
        };

        if pport < 0 {
            return;
        }
        let Some(lanes_list) = lanes_list else {
            return;
        };
        // TRANSCEIVER_INFO `type` not ready → xcvr not present.
        if xcvr_type.is_none() {
            return;
        }

        // Double-check the HW presence before moving forward.
        let sfp = match self.hal.sfp(pport as usize) {
            Ok(s) => s,
            Err(_) => {
                self.log_error(&format!("{lport}: module not present!"));
                self.clear_xcvr_type(lport);
                return;
            }
        };
        if !matches!(sfp.get_presence(), Ok(true)) {
            self.log_error(&format!("{lport}: module not present!"));
            self.clear_xcvr_type(lport);
            return;
        }

        // Skip if no xcvr api (Python `get_xcvr_api() is None` / `AttributeError`).
        let api_box = match (self.api_factory)(sfp) {
            Some(a) => a,
            None => {
                self.log_error(&format!("{lport}: skipping sff_mgr since no xcvr api!"));
                return;
            }
        };
        let api: &dyn SffApi = api_box.as_ref();

        // Proceed only for non-CMIS transceivers.
        if api.is_cmis() {
            return;
        }

        // Fill host_tx_ready / admin_status from the DB if the event didn't carry them.
        let (host_in, admin_in) = {
            let d = &self.port_dict[lport];
            (d.host_tx_ready.clone(), d.admin_status.clone())
        };
        let host_tx_ready = match host_in {
            Some(v) => v,
            None => {
                let v = self.get_host_tx_status(lport, asic_id);
                self.port_dict.get_mut(lport).unwrap().host_tx_ready = Some(v.clone());
                self.log_notice(&format!(
                    "{lport}: fetched DB and updated host_tx_ready={v} locally"
                ));
                v
            }
        };
        let admin_status = match admin_in {
            Some(v) => v,
            None => {
                let v = self.get_admin_status(lport, asic_id);
                self.port_dict.get_mut(lport).unwrap().admin_status = Some(v.clone());
                self.log_notice(&format!(
                    "{lport}: fetched DB and updated admin_status={v} locally"
                ));
                v
            }
        };

        // Diff against the previous sweep: insertion / host_tx_ready / admin_status change.
        let prev = self.port_dict_prev.get(lport);
        let xcvr_inserted = match prev {
            None => true,
            Some(p) => p.xcvr_type.is_none(),
        };
        let host_tx_ready_changed = match prev {
            None => true,
            Some(p) => p.host_tx_ready.as_deref() != Some(host_tx_ready.as_str()),
        };
        let admin_status_changed = match prev {
            None => true,
            Some(p) => p.admin_status.as_deref() != Some(admin_status.as_str()),
        };
        if !xcvr_inserted && !host_tx_ready_changed && !admin_status_changed {
            return;
        }
        self.log_notice(&format!(
            "{lport}: xcvr=present(inserted={xcvr_inserted}), \
             host_tx_ready={host_tx_ready}(changed={host_tx_ready_changed}), \
             admin_status={admin_status}(changed={admin_status_changed})"
        ));

        // Skip copper cables / modules that don't support tx_disable (missing routines →
        // `None` → skip).
        match api.is_copper() {
            Some(true) => {
                self.log_notice(&format!("{lport}: skipping sff_mgr for copper cable"));
                return;
            }
            Some(false) => {}
            None => return,
        }
        match api.get_tx_disable_support() {
            Some(false) => {
                self.log_notice(&format!(
                    "{lport}: skipping sff_mgr due to tx_disable not supported"
                ));
                return;
            }
            Some(true) => {}
            None => return,
        }

        // On insertion (or admin coming up) enable high power class + exit low-power mode.
        if xcvr_inserted || (admin_status_changed && admin_status == "up") {
            self.enable_high_power_class(api, lport);
            if api.get_lpmode_support() && !api.set_lpmode(false) {
                self.log_error(&format!(
                    "{lport}: Failed to take module out of low power mode."
                ));
            }
        }

        // Resolve (and cache) the active-lane mask for this logical port.
        let active_lanes = match active_lanes.take() {
            Some(a) => a,
            None => match self.get_active_lanes_for_lport(
                lport,
                subport_idx,
                lanes_list.len() as i64,
                DEFAULT_NUM_LANES_PER_PPORT,
            ) {
                Some(a) => {
                    self.port_dict.get_mut(lport).unwrap().active_lanes = Some(a.clone());
                    a
                }
                None => {
                    self.log_error(&format!(
                        "{lport}: skipping sff_mgr due to failing to get active lanes"
                    ));
                    return;
                }
            },
        };

        // TX is enabled only when host_tx_ready is true AND admin_status is up.
        let target_tx_disable_flag = !(host_tx_ready == "true" && admin_status == "up");
        let cur_tx_disable_array = match api.get_tx_disable() {
            Some(c) => c,
            None => {
                self.log_error(&format!("{lport}: Failed to get current tx_disable value"));
                // Best-effort: force every interested lane by seeding the opposite value.
                vec![!target_tx_disable_flag; DEFAULT_NUM_LANES_PER_PPORT as usize]
            }
        };
        let delta_array = self.calculate_tx_disable_delta_array(
            &cur_tx_disable_array,
            target_tx_disable_flag,
            &active_lanes,
        );
        let mask = self.convert_bool_array_to_bit_mask(&delta_array);
        if mask == 0 {
            self.log_notice(&format!(
                "{lport}: No change is needed for tx_disable value"
            ));
            return;
        }
        if api.tx_disable_channel(mask, target_tx_disable_flag) {
            self.log_notice(&format!(
                "{lport}: TX was {} with lanes mask: {mask:#b}",
                if target_tx_disable_flag {
                    "disabled"
                } else {
                    "enabled"
                }
            ));
        } else {
            self.log_error(&format!(
                "{lport}: Failed to {} TX with lanes mask: {mask:#b}",
                if target_tx_disable_flag {
                    "disable"
                } else {
                    "enable"
                }
            ));
        }
    }

    /// One sweep over every known logical port + snapshot for the next diff — the reference
    /// `task_worker` while-body (minus the blocking observer poll). Unit tests drive this.
    pub fn process_ports_once(&mut self, stop: &AtomicBool) {
        let lports: Vec<String> = self.port_dict.keys().cloned().collect();
        for lport in lports {
            if stop.load(Ordering::Relaxed) {
                break;
            }
            if !self.port_dict.contains_key(&lport) {
                continue;
            }
            self.process_single_lport(&lport);
        }
        // Snapshot for the next sweep's change detection (Python `copy.deepcopy`).
        self.port_dict_prev = self.port_dict.clone();
    }

    /// `task_worker` — subscribe via the observer and drive each port's bring-up. The SFF
    /// task is purely event-driven: it processes only when a watched table changes (a real
    /// SET/DEL), plus once for the boot snapshot (the reference's field replay on restart).
    pub fn task_worker(&mut self, stop: &Arc<AtomicBool>) {
        let mut observer = match MultiPortChangeObserver::for_sff() {
            Ok(o) => Some(o),
            Err(e) => {
                eprintln!("{SFF_LOGGER_PREFIX}SffManagerTask: failed to start MultiPortChangeObserver: {e}");
                None
            }
        };

        // Boot snapshot: seed `port_dict` from every already-configured port, then run one
        // sweep (mirrors the reference "messages replayed for all fields on restart").
        if let Some(obs) = observer.as_mut() {
            let initial = obs.take_initial_snapshot();
            let had_initial = !initial.is_empty();
            for ev in initial {
                self.on_port_update_event(&ev);
            }
            if had_initial {
                self.process_ports_once(stop);
            }
        } else {
            return;
        }

        while !stop.load(Ordering::Relaxed) {
            let mut had_update = false;
            if let Some(obs) = observer.as_mut() {
                match obs.handle_port_update_event(PORT_UPDATE_SELECT_TIMEOUT_MS) {
                    Ok(events) => {
                        if !events.is_empty() {
                            had_update = true;
                        }
                        for ev in events {
                            self.on_port_update_event(&ev);
                        }
                    }
                    Err(e) => eprintln!("{SFF_LOGGER_PREFIX}SffManagerTask: observer error: {e}"),
                }
            }
            // In the case of no real update, go back to the beginning of the loop.
            if had_update {
                self.process_ports_once(stop);
            }
        }
    }

    /// Spawn helper: run the bring-up loop to completion on this thread, wrapping each
    /// pass in `catch_unwind` so a panic restarts the loop rather than tearing the daemon
    /// down (the pmon supervisor must stay RUNNING; per-port errors are non-fatal).
    pub fn run(mut self, stop: Arc<AtomicBool>) {
        if stop.load(Ordering::Relaxed) {
            return;
        }
        while !stop.load(Ordering::Relaxed) {
            let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                self.task_worker(&stop);
            }));
            if outcome.is_ok() {
                break;
            }
            eprintln!("{SFF_LOGGER_PREFIX}SffManagerTask panicked; restarting SFF bring-up loop");
        }
    }
}

// =====================================================================================
// MockSffApi — Part-B double (canned/settable returns + call counters).
// =====================================================================================

struct SffMockInner {
    is_cmis: bool,
    is_copper: Option<bool>,
    tx_disable_support: Option<bool>,
    power_class: Option<i64>,
    set_high_power_class_result: Option<bool>,
    lpmode_support: bool,
    set_lpmode_result: bool,
    tx_disable: Option<Vec<bool>>,
    tx_disable_channel_result: bool,
    calls: HashMap<String, usize>,
    tx_disable_channel_args: Vec<(u32, bool)>,
    set_lpmode_args: Vec<bool>,
}

impl Default for SffMockInner {
    fn default() -> Self {
        SffMockInner {
            is_cmis: false,
            is_copper: Some(false),
            tx_disable_support: Some(true),
            power_class: Some(1),
            set_high_power_class_result: Some(true),
            lpmode_support: false,
            set_lpmode_result: true,
            tx_disable: Some(vec![false; SFF_NUM_LANES]),
            tx_disable_channel_result: true,
            calls: HashMap::new(),
            tx_disable_channel_args: Vec::new(),
            set_lpmode_args: Vec::new(),
        }
    }
}

/// Part-B mock [`SffApi`] — the Rust analogue of the Python tests' `MagicMock()` xcvr api.
/// Interior-mutable + `Clone` (shares one `Arc<Mutex>`), so the api the task obtains and
/// the handle the test drives observe the same counters and settable returns.
#[derive(Clone)]
pub struct MockSffApi {
    inner: Arc<Mutex<SffMockInner>>,
}

impl Default for MockSffApi {
    fn default() -> Self {
        MockSffApi {
            inner: Arc::new(Mutex::new(SffMockInner::default())),
        }
    }
}

impl MockSffApi {
    pub fn new() -> Self {
        Self::default()
    }

    fn bump(&self, name: &str) {
        *self
            .inner
            .lock()
            .unwrap()
            .calls
            .entry(name.to_string())
            .or_insert(0) += 1;
    }

    /// How many times `method` was invoked on this api (across clones).
    pub fn call_count(&self, method: &str) -> usize {
        *self.inner.lock().unwrap().calls.get(method).unwrap_or(&0)
    }

    /// Every `(mask, disable)` passed to `tx_disable_channel`, in order.
    pub fn tx_disable_channel_args(&self) -> Vec<(u32, bool)> {
        self.inner.lock().unwrap().tx_disable_channel_args.clone()
    }

    /// Every `lpmode` value passed to `set_lpmode`, in order.
    pub fn set_lpmode_args(&self) -> Vec<bool> {
        self.inner.lock().unwrap().set_lpmode_args.clone()
    }

    pub fn set_is_cmis(&self, v: bool) {
        self.inner.lock().unwrap().is_cmis = v;
    }
    pub fn set_is_copper(&self, v: Option<bool>) {
        self.inner.lock().unwrap().is_copper = v;
    }
    pub fn set_tx_disable_support(&self, v: Option<bool>) {
        self.inner.lock().unwrap().tx_disable_support = v;
    }
    pub fn set_power_class(&self, v: Option<i64>) {
        self.inner.lock().unwrap().power_class = v;
    }
    pub fn set_high_power_class_result(&self, v: Option<bool>) {
        self.inner.lock().unwrap().set_high_power_class_result = v;
    }
    pub fn set_lpmode_support(&self, v: bool) {
        self.inner.lock().unwrap().lpmode_support = v;
    }
    pub fn set_lpmode_result(&self, v: bool) {
        self.inner.lock().unwrap().set_lpmode_result = v;
    }
    pub fn set_tx_disable(&self, v: Option<Vec<bool>>) {
        self.inner.lock().unwrap().tx_disable = v;
    }
    pub fn set_tx_disable_channel_result(&self, v: bool) {
        self.inner.lock().unwrap().tx_disable_channel_result = v;
    }
}

impl SffApi for MockSffApi {
    fn is_cmis(&self) -> bool {
        self.bump("is_cmis");
        self.inner.lock().unwrap().is_cmis
    }
    fn is_copper(&self) -> Option<bool> {
        self.bump("is_copper");
        self.inner.lock().unwrap().is_copper
    }
    fn get_tx_disable_support(&self) -> Option<bool> {
        self.bump("get_tx_disable_support");
        self.inner.lock().unwrap().tx_disable_support
    }
    fn get_power_class(&self) -> Option<i64> {
        self.bump("get_power_class");
        self.inner.lock().unwrap().power_class
    }
    fn set_high_power_class(&self, _power_class: i64, _enable: bool) -> Option<bool> {
        self.bump("set_high_power_class");
        self.inner.lock().unwrap().set_high_power_class_result
    }
    fn get_lpmode_support(&self) -> bool {
        self.bump("get_lpmode_support");
        self.inner.lock().unwrap().lpmode_support
    }
    fn set_lpmode(&self, lpmode: bool) -> bool {
        self.bump("set_lpmode");
        let mut g = self.inner.lock().unwrap();
        g.set_lpmode_args.push(lpmode);
        g.set_lpmode_result
    }
    fn get_tx_disable(&self) -> Option<Vec<bool>> {
        self.bump("get_tx_disable");
        self.inner.lock().unwrap().tx_disable.clone()
    }
    fn tx_disable_channel(&self, media_lanes_mask: u32, disable: bool) -> bool {
        self.bump("tx_disable_channel");
        let mut g = self.inner.lock().unwrap();
        g.tx_disable_channel_args.push((media_lanes_mask, disable));
        g.tx_disable_channel_result
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::mock::{MockHal, MockSfp};
    use crate::xcvrd_utilities::port_event_helper::PortChangeEvent;
    use std::collections::BTreeMap;

    const NS: &str = "";

    fn helper() -> Arc<XcvrTableHelper> {
        Arc::new(XcvrTableHelper::with_mock_tables(&[NS.to_string()]))
    }

    fn mock_factory(api: MockSffApi) -> SffApiFactory {
        Box::new(move |_sfp| Some(Box::new(api.clone()) as Box<dyn SffApi>))
    }

    fn none_factory() -> SffApiFactory {
        Box::new(|_sfp| None)
    }

    /// A task with a single present SFP (physical index 0) + the given api factory.
    fn task_with(
        hal: Arc<dyn Hal>,
        th: Arc<XcvrTableHelper>,
        factory: SffApiFactory,
    ) -> SffManagerTask {
        SffManagerTask::new(vec![NS.to_string()], hal, th, factory)
    }

    fn bare_task() -> SffManagerTask {
        let hal: Arc<dyn Hal> = Arc::new(MockHal::with_sfps(vec![MockSfp::present()]));
        task_with(hal, helper(), none_factory())
    }

    /// Build a SET/DEL event with an explicit port_dict.
    fn ev(
        name: &str,
        phys: Option<usize>,
        et: PortChangeEventType,
        db: &str,
        tbl: &str,
        dict: &[(&str, &str)],
    ) -> PortChangeEvent {
        let mut m = BTreeMap::new();
        for (k, v) in dict {
            m.insert(k.to_string(), v.to_string());
        }
        PortChangeEvent::new(
            name.to_string(),
            phys,
            0,
            et,
            db.to_string(),
            tbl.to_string(),
        )
        .with_port_dict(m)
    }

    // ---- get_active_lanes_for_lport (test_SffManagerTask_get_active_lanes_for_lport) ----
    #[test]
    fn test_get_active_lanes_for_lport() {
        let task = bare_task();
        let lp = "Ethernet0";
        assert_eq!(
            task.get_active_lanes_for_lport(lp, 3, 1, 4),
            Some(vec![false, false, true, false])
        );
        assert_eq!(
            task.get_active_lanes_for_lport(lp, 1, 2, 4),
            Some(vec![true, true, false, false])
        );
        assert_eq!(
            task.get_active_lanes_for_lport(lp, 2, 2, 4),
            Some(vec![false, false, true, true])
        );
        assert_eq!(
            task.get_active_lanes_for_lport(lp, 0, 4, 4),
            Some(vec![true, true, true, true])
        );
        // Larger (not a real use case): subport 1, 4 lanes/lport, 32 lanes/pport.
        let mut expected = vec![false; 32];
        for e in expected.iter_mut().take(4) {
            *e = true;
        }
        assert_eq!(
            task.get_active_lanes_for_lport(lp, 1, 4, 32),
            Some(expected)
        );
    }

    #[test]
    fn test_get_active_lanes_for_lport_with_invalid_input() {
        let task = bare_task();
        let lp = "Ethernet0";
        assert_eq!(task.get_active_lanes_for_lport(lp, -1, 4, 32), None);
        assert_eq!(task.get_active_lanes_for_lport(lp, 5, 1, 4), None);
    }

    // ---- get_host_tx_status (test_SffManagerTask_get_host_tx_status) ----
    #[test]
    fn test_get_host_tx_status() {
        let th = helper();
        th.get_state_port_tbl(0)
            .hset("Ethernet0", "host_tx_ready", "true");
        let hal: Arc<dyn Hal> = Arc::new(MockHal::with_sfps(vec![MockSfp::present()]));
        let task = task_with(hal, th.clone(), none_factory());
        assert_eq!(task.get_host_tx_status("Ethernet0", 0), "true");
        // Absent → default "false".
        assert_eq!(task.get_host_tx_status("Ethernet4", 0), "false");
    }

    // ---- get_admin_status (test_SffManagerTask_get_admin_status) ----
    #[test]
    fn test_get_admin_status() {
        let th = helper();
        th.get_cfg_port_tbl(0)
            .hset("Ethernet0", "admin_status", "up");
        let hal: Arc<dyn Hal> = Arc::new(MockHal::with_sfps(vec![MockSfp::present()]));
        let task = task_with(hal, th.clone(), none_factory());
        assert_eq!(task.get_admin_status("Ethernet0", 0), "up");
        // Absent → default "down".
        assert_eq!(task.get_admin_status("Ethernet4", 0), "down");
    }

    // ---- enable_high_power_class (test_SffManagerTask_enable_high_power_class) ----
    // Cumulative counters (the Rust mock accumulates; the Python test resets per case)
    // assert the SAME logic: set_high_power_class is invoked only for class>=5 (cases 1,4,5).
    #[test]
    fn test_enable_high_power_class() {
        let api = MockSffApi::new();
        let task = bare_task();
        let lp = "Ethernet0";

        // 1) power_class 5, set succeeds.
        api.set_power_class(Some(5));
        api.set_high_power_class_result(Some(true));
        task.enable_high_power_class(&api, lp);
        assert_eq!(api.call_count("get_power_class"), 1);
        assert_eq!(api.call_count("set_high_power_class"), 1);

        // 2) get_power_class returned None → log + skip.
        api.set_power_class(None);
        task.enable_high_power_class(&api, lp);
        assert_eq!(api.call_count("get_power_class"), 2);
        assert_eq!(api.call_count("set_high_power_class"), 1);

        // 3) power_class 4 (< 5) → nothing to do.
        api.set_power_class(Some(4));
        task.enable_high_power_class(&api, lp);
        assert_eq!(api.call_count("get_power_class"), 3);
        assert_eq!(api.call_count("set_high_power_class"), 1);

        // 4) power_class 5, set fails → logged, still counted.
        api.set_power_class(Some(5));
        api.set_high_power_class_result(Some(false));
        task.enable_high_power_class(&api, lp);
        assert_eq!(api.call_count("get_power_class"), 4);
        assert_eq!(api.call_count("set_high_power_class"), 2);

        // 5) power_class 5, set raises (AttributeError/NotImplementedError) → called, passed.
        api.set_power_class(Some(5));
        api.set_high_power_class_result(None);
        task.enable_high_power_class(&api, lp);
        assert_eq!(api.call_count("get_power_class"), 5);
        assert_eq!(api.call_count("set_high_power_class"), 3);
    }

    // ---- on_port_update_event (test_SffManagerTask_handle_port_change_event) ----
    #[test]
    fn test_handle_port_change_event() {
        let mut task = bare_task();

        // Non-front-panel names are ignored.
        task.on_port_update_event(&ev(
            "PortConfigDone",
            None,
            PortChangeEventType::Set,
            "",
            "",
            &[],
        ));
        assert_eq!(task.port_dict.len(), 0);
        task.on_port_update_event(&ev(
            "PortInitDone",
            None,
            PortChangeEventType::Set,
            "",
            "",
            &[],
        ));
        assert_eq!(task.port_dict.len(), 0);

        // ADD/REMOVE are not handled by on_port_update_event.
        task.on_port_update_event(&ev(
            "Ethernet0",
            Some(1),
            PortChangeEventType::Add,
            "",
            "",
            &[],
        ));
        assert_eq!(task.port_dict.len(), 0);
        task.on_port_update_event(&ev(
            "Ethernet0",
            Some(1),
            PortChangeEventType::Remove,
            "",
            "",
            &[],
        ));
        assert_eq!(task.port_dict.len(), 0);

        // A DEL with no db/table match is a no-op.
        task.on_port_update_event(&ev(
            "Ethernet0",
            Some(1),
            PortChangeEventType::Del,
            "",
            "",
            &[],
        ));
        assert_eq!(task.port_dict.len(), 0);

        // A SET creates the entry.
        task.on_port_update_event(&ev(
            "Ethernet0",
            Some(1),
            PortChangeEventType::Set,
            "",
            "",
            &[
                ("type", "QSFP28"),
                ("subport", "0"),
                ("host_tx_ready", "false"),
            ],
        ));
        assert_eq!(task.port_dict.len(), 1);

        // A TRANSCEIVER_INFO DEL (physical_port None) only clears `type` — entry survives.
        task.on_port_update_event(&ev(
            "Ethernet0",
            None,
            PortChangeEventType::Del,
            "STATE_DB",
            "TRANSCEIVER_INFO",
            &[],
        ));
        assert_eq!(task.port_dict.len(), 1);
        assert!(task.port_dict["Ethernet0"].xcvr_type.is_none());

        // A CONFIG_DB PORT DEL removes the whole entry.
        task.on_port_update_event(&ev(
            "Ethernet0",
            Some(1),
            PortChangeEventType::Del,
            "CONFIG_DB",
            "PORT_TABLE",
            &[],
        ));
        assert_eq!(task.port_dict.len(), 0);
    }

    /// Build a one-port task with a present/absent SFP at physical index 0, seeded
    /// host_tx_ready + admin_status in the mock DB, and the given api.
    fn tw_task(
        api: &MockSffApi,
        present: bool,
        host_tx_ready: &str,
        admin_status: &str,
    ) -> (SffManagerTask, Arc<XcvrTableHelper>) {
        let th = helper();
        th.get_state_port_tbl(0)
            .hset("Ethernet0", "host_tx_ready", host_tx_ready);
        th.get_cfg_port_tbl(0)
            .hset("Ethernet0", "admin_status", admin_status);
        let sfp = if present {
            MockSfp::present()
        } else {
            MockSfp::absent()
        };
        let hal: Arc<dyn Hal> = Arc::new(MockHal::with_sfps(vec![sfp]));
        let task = task_with(hal, th.clone(), mock_factory(api.clone()));
        (task, th)
    }

    fn seed_insert(task: &mut SffManagerTask) {
        // A single CONFIG_DB PORT SET carrying type/subport/lanes at physical index 0.
        task.on_port_update_event(&ev(
            "Ethernet0",
            Some(0),
            PortChangeEventType::Set,
            "CONFIG_DB",
            "PORT",
            &[("type", "QSFP28"), ("subport", "0"), ("lanes", "1,2,3,4")],
        ));
    }

    // ---- task_worker: TX enable (host_tx_ready && admin up) ----
    #[test]
    fn test_task_worker_tx_enable() {
        let api = MockSffApi::new();
        api.set_tx_disable(Some(vec![true, true, true, true])); // currently disabled
        let (mut task, _th) = tw_task(&api, true, "true", "up");
        seed_insert(&mut task);
        let stop = AtomicBool::new(false);
        task.process_ports_once(&stop);
        assert_eq!(api.call_count("tx_disable_channel"), 1);
        // target = enable (false); all 4 active lanes need change → mask 0xF.
        assert_eq!(api.tx_disable_channel_args(), vec![(0xF, false)]);
    }

    // ---- task_worker: TX disable (host_tx_ready false) ----
    #[test]
    fn test_task_worker_tx_disable() {
        let api = MockSffApi::new();
        api.set_tx_disable(Some(vec![false, false, false, false])); // currently enabled
        let (mut task, _th) = tw_task(&api, true, "false", "up");
        seed_insert(&mut task);
        let stop = AtomicBool::new(false);
        task.process_ports_once(&stop);
        assert_eq!(api.call_count("tx_disable_channel"), 1);
        assert_eq!(api.tx_disable_channel_args(), vec![(0xF, true)]);
    }

    // ---- task_worker: no insertion + no host_tx_ready/admin change → no-op ----
    #[test]
    fn test_task_worker_no_change() {
        let api = MockSffApi::new();
        api.set_tx_disable(Some(vec![true, true, true, true]));
        let (mut task, _th) = tw_task(&api, true, "true", "up");
        seed_insert(&mut task);
        let stop = AtomicBool::new(false);
        task.process_ports_once(&stop); // enables TX (count 1)
        assert_eq!(api.call_count("tx_disable_channel"), 1);
        // Second sweep with no field change → skipped, and port_dict == port_dict_prev.
        task.process_ports_once(&stop);
        assert_eq!(api.call_count("tx_disable_channel"), 1);
        assert_eq!(task.port_dict, task.port_dict_prev);
    }

    // ---- task_worker: current == target → mask 0 → no tx_disable_channel ----
    #[test]
    fn test_task_worker_mask_zero() {
        let api = MockSffApi::new();
        api.set_tx_disable(Some(vec![false, false, false, false])); // already enabled
        let (mut task, _th) = tw_task(&api, true, "true", "up"); // target = enable
        seed_insert(&mut task);
        let stop = AtomicBool::new(false);
        task.process_ports_once(&stop);
        assert_eq!(api.call_count("tx_disable_channel"), 0);
    }

    // ---- task_worker: copper cable → skip ----
    #[test]
    fn test_task_worker_copper_skip() {
        let api = MockSffApi::new();
        api.set_is_copper(Some(true));
        let (mut task, _th) = tw_task(&api, true, "true", "up");
        seed_insert(&mut task);
        let stop = AtomicBool::new(false);
        task.process_ports_once(&stop);
        assert_eq!(api.call_count("is_copper"), 1);
        assert_eq!(api.call_count("tx_disable_channel"), 0);
    }

    // ---- task_worker: tx_disable not supported → skip ----
    #[test]
    fn test_task_worker_tx_disable_not_supported() {
        let api = MockSffApi::new();
        api.set_tx_disable_support(Some(false));
        let (mut task, _th) = tw_task(&api, true, "true", "up");
        seed_insert(&mut task);
        let stop = AtomicBool::new(false);
        task.process_ports_once(&stop);
        assert_eq!(api.call_count("get_tx_disable_support"), 1);
        assert_eq!(api.call_count("tx_disable_channel"), 0);
    }

    // ---- task_worker: module not present → clears xcvr_type, no tx_disable ----
    #[test]
    fn test_task_worker_module_not_present() {
        let api = MockSffApi::new();
        let (mut task, _th) = tw_task(&api, false, "true", "up"); // absent SFP
        seed_insert(&mut task);
        let stop = AtomicBool::new(false);
        task.process_ports_once(&stop);
        assert_eq!(api.call_count("tx_disable_channel"), 0);
        assert!(task.port_dict["Ethernet0"].xcvr_type.is_none());
    }

    // ---- task_worker: no xcvr api → graceful skip (port survives) ----
    #[test]
    fn test_task_worker_xcvr_api_none() {
        let th = helper();
        th.get_state_port_tbl(0)
            .hset("Ethernet0", "host_tx_ready", "true");
        th.get_cfg_port_tbl(0)
            .hset("Ethernet0", "admin_status", "up");
        let hal: Arc<dyn Hal> = Arc::new(MockHal::with_sfps(vec![MockSfp::present()]));
        let mut task = task_with(hal, th, none_factory());
        seed_insert(&mut task);
        let stop = AtomicBool::new(false);
        task.process_ports_once(&stop);
        // Present + api None → logged + skipped; the entry (and its type) survives.
        assert!(task.port_dict.contains_key("Ethernet0"));
        assert!(task.port_dict["Ethernet0"].xcvr_type.is_some());
    }

    // ---- task_worker: lpmode disable on insertion; failure logged but TX still driven ----
    #[test]
    fn test_task_worker_lpmode_disabled_on_bringup() {
        let api = MockSffApi::new();
        api.set_lpmode_support(true);
        api.set_lpmode_result(false); // set_lpmode fails
        api.set_power_class(Some(1)); // < 5, no high-power step
        api.set_tx_disable(Some(vec![true, true, true, true]));
        let (mut task, _th) = tw_task(&api, true, "true", "up");
        seed_insert(&mut task);
        let stop = AtomicBool::new(false);
        task.process_ports_once(&stop);
        // lpmode is disabled (set_lpmode(false)) on insertion...
        assert_eq!(api.call_count("set_lpmode"), 1);
        assert_eq!(api.set_lpmode_args(), vec![false]);
        // ...and despite the failure the TX bring-up still proceeds.
        assert_eq!(api.call_count("tx_disable_channel"), 1);
    }

    // ---- task_worker: high power class enabled on insertion (class 5) ----
    #[test]
    fn test_task_worker_high_power_class_enabled() {
        let api = MockSffApi::new();
        api.set_power_class(Some(5));
        api.set_high_power_class_result(Some(true));
        api.set_tx_disable(Some(vec![true, true, true, true]));
        let (mut task, _th) = tw_task(&api, true, "true", "up");
        seed_insert(&mut task);
        let stop = AtomicBool::new(false);
        task.process_ports_once(&stop);
        assert_eq!(api.call_count("get_power_class"), 1);
        assert_eq!(api.call_count("set_high_power_class"), 1);
    }

    // =================================================================================
    // BridgeSffApi register-level tests.
    //
    // The tests above drive the SFF bring-up logic through the canned `MockSffApi`, so
    // they lock the *control flow* (who gets called, when) but not the raw SFF-8636
    // register semantics. The e2e gate `test_sff_control.py::test_sff_high_power_class_
    // enabled` asserts those semantics directly against the emulator: for a power-class
    // >= 5 module it requires xcvrd to set High Power Class Enable (00h:93 bit2). That
    // e2e self-skips on the reference testbed because the emulator hardcodes the SFF
    // module's power-class byte 00h:129 = 0xC0 (class 4) and there is no class-5+ SFF
    // module to exercise it. These unit tests reproduce the exact byte-level behaviour
    // the e2e would verify — over the real `BridgeSffApi` against a seeded `MockSfp` —
    // so the class>=5 path is covered deterministically here regardless of the emulator.
    // =================================================================================

    /// `BridgeSffApi` over an SFP whose EEPROM byte 00h:129 == `ext_id`.
    fn bridge_with_ext_id(ext_id: u8) -> BridgeSffApi {
        BridgeSffApi::new(Box::new(
            MockSfp::present().with_eeprom(SFF_POWER_CLASS_OFFSET, ext_id),
        ))
    }

    // ---- BridgeSffApi::get_power_class — SFF-8636 §6.2.6 Extended Identifier decode ----
    #[test]
    fn test_bridge_get_power_class_decode() {
        // bits 7:6 encode classes 1-4 when the low/class-8 bits are clear.
        assert_eq!(bridge_with_ext_id(0x00).get_power_class(), Some(1)); // 00b
        assert_eq!(bridge_with_ext_id(0x40).get_power_class(), Some(2)); // 01b
        assert_eq!(bridge_with_ext_id(0x80).get_power_class(), Some(3)); // 10b
        assert_eq!(bridge_with_ext_id(0xC0).get_power_class(), Some(4)); // 11b (emulator default)
                                                                         // bits 1:0 raise the class to 5/6/7 (0xC1 == the harness POWER_CLASS_5_VALUE 193).
        assert_eq!(bridge_with_ext_id(0xC1).get_power_class(), Some(5));
        assert_eq!(bridge_with_ext_id(0xC2).get_power_class(), Some(6));
        assert_eq!(bridge_with_ext_id(0xC3).get_power_class(), Some(7));
        // bit2 marks class 8 (max power declared in 00h:107); CLASS_8 -> 0xC4.
        assert_eq!(bridge_with_ext_id(0xC4).get_power_class(), Some(8));
        // Unreadable 00h:129 mirrors the Python `None` return (log + skip).
        assert_eq!(
            BridgeSffApi::new(Box::new(MockSfp::present())).get_power_class(),
            None
        );
    }

    // ---- BridgeSffApi::set_high_power_class — 00h:93 read-modify-write of bit2/bit3 ----
    #[test]
    fn test_bridge_set_high_power_class_registers() {
        let off = SFF_LPMODE_HP_CTRL_OFFSET;

        // class 5-7: set only High Power Class Enable (bit2), preserving other bits.
        let mock = MockSfp::present().with_eeprom(off, 0x00);
        let writes = mock.eeprom_writes.clone();
        assert_eq!(
            BridgeSffApi::new(Box::new(mock)).set_high_power_class(5, true),
            Some(true)
        );
        assert_eq!(
            *writes.lock().unwrap(),
            vec![(off, vec![SFF_HIGH_POWER_CLASS_5_7_BIT])]
        );

        // Existing Power_override (00h:93 bit0) must survive the read-modify-write.
        let mock = MockSfp::present().with_eeprom(off, 0x01);
        let writes = mock.eeprom_writes.clone();
        assert_eq!(
            BridgeSffApi::new(Box::new(mock)).set_high_power_class(5, true),
            Some(true)
        );
        assert_eq!(
            *writes.lock().unwrap(),
            vec![(off, vec![0x01 | SFF_HIGH_POWER_CLASS_5_7_BIT])]
        );

        // class 8: additionally set the class-8 enable (bit3).
        let mock = MockSfp::present().with_eeprom(off, 0x00);
        let writes = mock.eeprom_writes.clone();
        assert_eq!(
            BridgeSffApi::new(Box::new(mock)).set_high_power_class(8, true),
            Some(true)
        );
        assert_eq!(
            *writes.lock().unwrap(),
            vec![(
                off,
                vec![SFF_HIGH_POWER_CLASS_5_7_BIT | SFF_HIGH_POWER_CLASS_8_BIT]
            )]
        );

        // enable=false clears both high-power bits, leaving the rest untouched.
        let mock = MockSfp::present().with_eeprom(off, 0xFF);
        let writes = mock.eeprom_writes.clone();
        assert_eq!(
            BridgeSffApi::new(Box::new(mock)).set_high_power_class(5, false),
            Some(true)
        );
        assert_eq!(
            *writes.lock().unwrap(),
            vec![(
                off,
                vec![0xFF & !(SFF_HIGH_POWER_CLASS_5_7_BIT | SFF_HIGH_POWER_CLASS_8_BIT)]
            )]
        );
    }

    // ---- enable_high_power_class over BridgeSffApi: the e2e assertion, at unit level ----
    #[test]
    fn test_enable_high_power_class_via_bridge_class8_sets_00h93_bit2() {
        // A class-8 SFF module (00h:129 = 0xC4) must get High Power Class Enable set on
        // bring-up — exactly what test_sff_high_power_class_enabled asserts via the
        // Monitor trace (`any(v & HIGH_POWER_CLASS_5_7_BIT for v in vals)`).
        let task = bare_task();
        let mock = MockSfp::present()
            .with_eeprom(SFF_POWER_CLASS_OFFSET, 0xC4)
            .with_eeprom(SFF_LPMODE_HP_CTRL_OFFSET, 0x00);
        let writes = mock.eeprom_writes.clone();
        let api = BridgeSffApi::new(Box::new(mock));
        task.enable_high_power_class(&api, "Ethernet0");
        let w = writes.lock().unwrap();
        assert!(
            w.iter().any(|(off, data)| *off == SFF_LPMODE_HP_CTRL_OFFSET
                && !data.is_empty()
                && data[0] & SFF_HIGH_POWER_CLASS_5_7_BIT != 0),
            "no 00h:93 write set High Power Class Enable (bit2); writes={w:?}"
        );
    }

    #[test]
    fn test_enable_high_power_class_via_bridge_class4_noop() {
        // The reference emulator ships the SFF module as class 4 (00h:129 = 0xC0);
        // enable_high_power_class is a no-op below class 5, so it must NOT touch 00h:93.
        // (This is precisely why the e2e gate self-skips on this testbed.)
        let task = bare_task();
        let mock = MockSfp::present()
            .with_eeprom(SFF_POWER_CLASS_OFFSET, 0xC0)
            .with_eeprom(SFF_LPMODE_HP_CTRL_OFFSET, 0x00);
        let writes = mock.eeprom_writes.clone();
        let api = BridgeSffApi::new(Box::new(mock));
        task.enable_high_power_class(&api, "Ethernet0");
        assert!(
            writes.lock().unwrap().is_empty(),
            "class-4 module must not write the 00h:93 Power Control byte"
        );
    }

    /// `is_cmis` must ask the module's decode api, not re-read the whole module. The
    /// canned aggregate here says QSFP28 (non-CMIS) while the seam says QSFP-DD, so a
    /// `true` result can only come from the seam -- and the aggregate must stay untouched.
    #[test]
    fn sff_is_cmis_prefers_the_direct_xcvr_api_seam() {
        use std::sync::atomic::Ordering;

        let mut mock = MockSfp::present();
        mock.info = serde_json::json!({"type_abbrv_name": "QSFP28"});
        mock.api_calls = [(
            "get_module_type_abbreviation".to_string(),
            serde_json::json!("QSFP-DD"),
        )]
        .into_iter()
        .collect();
        let info_reads = mock.info_reads.clone();

        assert!(BridgeSffApi::new(Box::new(mock)).is_cmis());
        assert_eq!(
            info_reads.load(Ordering::SeqCst),
            0,
            "no whole-module re-read"
        );

        // Without the seam (every pre-existing test), the aggregate still answers.
        let mut plain = MockSfp::present();
        plain.info = serde_json::json!({"type_abbrv_name": "QSFP-DD"});
        let plain_reads = plain.info_reads.clone();
        assert!(BridgeSffApi::new(Box::new(plain)).is_cmis());
        assert!(
            plain_reads.load(Ordering::SeqCst) >= 1,
            "fallback used the aggregate"
        );
    }
}
