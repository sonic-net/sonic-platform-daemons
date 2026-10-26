//! `CmisApi` seam — the mockable CMIS control/decode surface the datapath bring-up
//! state machine drives (analysis §3.4).
//!
//! The Python `CmisManagerTask` talks to `sonic_platform_base...c_cmis.CmisApi`. That
//! object both *decodes* the module (identity/state/advertisement) and *controls* the
//! CMIS page-10h datapath registers. Here the two halves are split across the crate's
//! seams so unit tests can inject a double:
//!
//!   - **Decode reads** (`is_flat_memory`, `get_module_state`, `get_datapath_state`,
//!     `get_application_advertisement`, …) stay in Python — [`BridgeCmisApi`] sources
//!     them from the bridge's `get_transceiver_status()` / `get_transceiver_info()`
//!     getters (never re-decodes EEPROM in Rust).
//!   - **Control writes** (`set_datapath_deinit`, `set_application`,
//!     `scs_apply_datapath_init`, `set_datapath_init`, `tx_disable_channel`) are the raw
//!     page-10h register writes `CmisApi` performs, issued through
//!     [`crate::hal::SfpHandle::write_eeprom`] with the upstream `c_cmis.py` encodings.
//!     `set_lpmode` delegates to the bridge's own `set_lpmode` (Python decode).
//!
//! [`MockCmisApi`] is the Part-B double (canned decode + settable/sequenced dynamic
//! state + write call-counters), the analogue of the Python tests' `MagicMock()` api.
#![allow(dead_code)]

use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Mutex};

use serde_json::{json, Value};

use crate::error::Result;
use crate::hal::SfpHandle;

/// The CMIS control/decode surface the [`super::cmis_manager_task::CmisManagerTask`]
/// bring-up state machine drives (`api.*` in `cmis_manager_task.py`). Split from
/// [`SfpHandle`] so Part-B tests inject [`MockCmisApi`]; production wraps a bridge
/// handle in [`BridgeCmisApi`].
pub trait CmisApi {
    // --- decode reads (stay in Python via the bridge) ---
    fn is_flat_memory(&self) -> bool;
    fn is_coherent_module(&self) -> bool;
    fn get_module_type_abbreviation(&self) -> Option<String>;
    fn get_module_state(&self) -> String;
    fn get_cmis_rev(&self) -> String;
    /// Application advertisement, a JSON object keyed by the (stringified) app index
    /// (`"1".."15"`) → `{host_electrical_interface_id, host_lane_count, media_lane_count,
    /// host_lane_assignment_options, media_lane_assignment_options, …}`.
    fn get_application_advertisement(&self) -> Value;
    fn get_host_lane_assignment_option(&self, appl: u32) -> u32;
    fn get_media_lane_count(&self, appl: u32) -> u32;
    fn get_media_lane_assignment_option(&self, appl: u32) -> u32;
    /// `{DP{n}State: "DataPathActivated"|…}` for host lanes 1..8.
    fn get_datapath_state(&self) -> Value;
    /// `{ConfigStatusLane{n}: "ConfigSuccess"|…}`.
    fn get_config_datapath_hostlane_status(&self) -> Value;
    /// `{DPInitPending{n}: bool}`.
    fn get_dpinit_pending(&self) -> Value;
    /// `{ActiveAppSelLane{n}: <app>}`. `Err` mirrors the Python `NotImplementedError`.
    fn get_active_apsel_hostlane(&self) -> Result<Value>;
    /// The application code currently applied to host `lane` (0-based).
    fn get_application(&self, lane: u32) -> u32;

    // --- durations, milliseconds (caller divides by 1000 for seconds) ---
    fn get_datapath_init_duration(&self) -> f64;
    fn get_datapath_deinit_duration(&self) -> f64;
    fn get_datapath_tx_turnon_duration(&self) -> f64;
    fn get_datapath_tx_turnoff_duration(&self) -> f64;
    fn get_module_pwr_up_duration(&self) -> f64;
    fn get_module_pwr_down_duration(&self) -> f64;

    // --- coherent/ZR tuning (skipped for non-coherent modules) ---
    fn get_tx_config_power(&self) -> f64;
    fn get_laser_config_freq(&self) -> i64;
    /// `(grid_bitmask, _, _, low_freq, high_freq)` (`c_cmis.get_supported_freq_config`).
    fn get_supported_freq_config(&self) -> (i64, i64, i64, i64, i64);
    fn get_supported_power_config(&self) -> (f64, f64);
    fn get_tuning_in_progress(&self) -> bool;
    fn get_manufacturer(&self) -> String;
    fn get_model(&self) -> String;

    // --- control writes (raw page-10h register writes / bridge set_lpmode) ---
    fn set_datapath_deinit(&self, host_lanes_mask: u32) -> bool;
    fn set_datapath_init(&self, host_lanes_mask: u32) -> bool;
    fn tx_disable_channel(&self, media_lanes_mask: u32, disable: bool) -> bool;
    fn set_lpmode(&self, lpmode: bool, wait_state_change: bool) -> bool;
    fn set_application(&self, host_lanes_mask: u32, appl: u32, ec: u32) -> bool;
    fn scs_apply_datapath_init(&self, host_lanes_mask: u32) -> bool;
    /// Stage per-vendor custom Signal-Integrity settings into the page-10h Staged
    /// Control Set (`c_cmis.stage_custom_si_settings`): split `optics_si_dict` by the
    /// `Tx`/`Rx` key suffix, stage RX controls then TX controls, per-lane, gated by the
    /// module's advertised SI-control support. `true` on success (or nothing to stage).
    fn stage_custom_si_settings(&self, host_lanes_mask: u32, optics_si_dict: &Value) -> bool;
    fn set_tx_power(&self, tx_power: f64) -> bool;
    fn set_laser_freq(&self, freq: i64, grid: u32) -> bool;
}

pub const CMIS_MAX_HOST_LANES_USIZE: usize = 8;

// =====================================================================================
// BridgeCmisApi — production impl over a bridge SfpHandle.
// =====================================================================================

// CMIS page-10h control-register *linear* (optoe) offsets. `linear = page*128 + offset`
// (bank 0), the inverse of `sfp.py:linear_to_bpo`. Page 0x10 (SCS0) upper memory.
const SCS0_PAGE: usize = 0x10;
const DPDEINIT_OFFSET: usize = 128; // 10h:128 DataPathDeinit (1 bit / host lane)
const OUTPUT_DISABLE_TX_OFFSET: usize = 130; // 10h:130 OutputDisableTx (1 bit / lane)
const APPLY_DPINIT_OFFSET: usize = 143; // 10h:143 ApplyDPInitLane trigger
const DPCONFIG_BASE_OFFSET: usize = 145; // 10h:145..152 DPConfigLane (one byte / lane)
const CMIS_REV_LINEAR: usize = 1; // 00h:1 CMIS revision (high nibble = major)
const CMIS_FLAT_MEM_LINEAR: usize = 2; // 00h:2 status; bit7 = FlatMem (c_cmis.is_flat_memory)
const CMIS_FLAT_MEM_BIT: u8 = 0x80;

// Page-10h Staged Control Set 0 Signal-Integrity control offsets (CMIS v5.2 §8.8.1,
// Table 8-121). Each control packs `bits_per_lane` bits per host lane, LSB-first across
// its byte range: host lane L (1-based) → bit range [(L-1)*bpl .. L*bpl). TX controls
// occupy 153-160, RX controls 161-175 (mirrors the e2e `SCS0_SI_CONTROL_RANGE`). These
// are the byte offsets `xcvr_eeprom.write("<Param><lane>", v)` resolves to.
const SI_ADAPTIVE_INPUT_EQ_ENABLE_TX_OFFSET: usize = 153; // 1 bit/lane
const SI_ADAPTIVE_INPUT_EQ_RECALLED_TX_OFFSET: usize = 154; // 2 bits/lane (154-155)
const SI_FIXED_INPUT_EQ_TARGET_TX_OFFSET: usize = 156; // 4 bits/lane (156-159)
const SI_CDR_ENABLE_TX_OFFSET: usize = 160; // 1 bit/lane
const SI_CDR_ENABLE_RX_OFFSET: usize = 161; // 1 bit/lane
const SI_OUTPUT_EQ_PRE_CURSOR_TARGET_RX_OFFSET: usize = 162; // 4 bits/lane (162-165)
const SI_OUTPUT_EQ_POST_CURSOR_TARGET_RX_OFFSET: usize = 166; // 4 bits/lane (166-169)
const SI_OUTPUT_AMPLITUDE_TARGET_RX_OFFSET: usize = 170; // 4 bits/lane (170-173)

// Page-01h Supported Signal-Integrity Controls Advertisement (CMIS §8.4.7). xcvrd only
// stages a control the module advertises support for; the CDR support bits are the ones
// the e2e drives (`SI_ADV_TX_CDR_OFFSET`/`SI_ADV_RX_CDR_OFFSET`, bit 0).
const SI_ADV_TX_CDR_OFFSET: usize = 161; // 01h:161 bit0 TXCDRSupported
const SI_ADV_RX_CDR_OFFSET: usize = 162; // 01h:162 bit0 RxCDRSupported

// `codes_cmis`/`consts` SI parameter names — the `optics_si_settings.json` top-level keys.
const CDR_ENABLE_TX: &str = "CDREnableTx";
const CDR_ENABLE_RX: &str = "CDREnableRx";
const OUTPUT_EQ_PRE_CURSOR_TARGET_RX: &str = "OutputEqPreCursorTargetRx";
const OUTPUT_EQ_POST_CURSOR_TARGET_RX: &str = "OutputEqPostCursorTargetRx";
const OUTPUT_AMPLITUDE_TARGET_RX: &str = "OutputAmplitudeTargetRx";
const FIXED_INPUT_EQ_TARGET_TX: &str = "FixedInputEqTargetTx";
const ADAPTIVE_INPUT_EQ_RECALLED_TX: &str = "AdaptiveInputEqRecalledTx";
const ADAPTIVE_INPUT_EQ_ENABLE_TX: &str = "AdaptiveInputEqEnableTx";

// CMIS page-11h Active Control Set. `get_active_apsel_hostlane`/`get_application` read
// the *provisioned* (active) DPConfigLane bytes (11h:206..213); the AppSelCode is the
// upper nibble (bits 7:4) of each host lane's byte. These are read-only and reflect the
// application the module actually applied (updated by ApplyDPInit), which is what
// `c_cmis.CmisApi.get_active_apsel_hostlane` reads — NOT a `get_transceiver_info` field.
const ACS_PAGE: usize = 0x11;
const ACS_DPCONFIG_BASE_OFFSET: usize = 206; // 11h:206..213 ActiveControlSet DPConfigLane

// CMIS page-01h State Machine Durations Advertising (CMIS v5.2 §8.3.7 / §8.4.7). Each
// max-duration is a 4-bit code (Table 8-43) packed two per byte. `c_cmis.py` decodes the
// code with `CmisCodes.DP_PATH_TIMINGS` (code -> milliseconds); we read the raw page-01h
// byte and re-apply that same fixed lookup — no module-specific interpretation, so CMIS
// decode still effectively lives "in Python". These were previously hard-coded to the CMIS
// spec *maximum* (dp_deinit=600s, pwr_up/down=70s, dp_init=60s, tx_on/off=5s); that made the
// AP_CONF timer (max(pwr_up, dp_deinit)) 600s, so a stalled datapath could not complete the
// CMIS_MAX_RETRIES retries within the e2e budget before latching cmis_state=FAILED. The
// module advertises far shorter durations; read and honour them.
const DURATIONS_PAGE: usize = 0x01;
const DP_INIT_DEINIT_OFFSET: usize = 144; // 01h:144 hi=MaxDurationDPDeinit, lo=MaxDurationDPInit
const MODULE_PWR_OFFSET: usize = 167; // 01h:167 hi=MaxDurationModulePwrDn, lo=MaxDurationModulePwrUp
const DP_TX_TURN_OFFSET: usize = 168; // 01h:168 hi=MaxDurationDPTxTurnOff, lo=MaxDurationDPTxTurnOn

// `c_cmis.get_datapath_init_duration` scales a short (<=1000 ms) advertised DPInit value ×10.
const DATAPATH_INIT_DURATION_MULTIPLIER: f64 = 10.0;
const DATAPATH_INIT_DURATION_OVERRIDE_THRESHOLD: f64 = 1000.0;

// Coherent/ZR (C-CMIS) laser-tuning registers (`CCmisApi`, mem_maps/public/cmis/pages).
// Page 04h Module Configuration Support advertises the tuning CAPABILITY (read-only); the
// tuning WRITES land on page 12h Tunable Laser Control/Status. `linear = page*128 + offset`
// via `cmis_linear`, matching the emulator's `linear_to_bpo`. The byte encodings mirror the
// `NumberRegField` field defs: SUPPORT_GRID/GRID_SPACING are 1-byte, the channel/power fields
// are signed-16-bit big-endian (`format=">h"`), and the power fields carry a ×100 scale.
const CCMIS_CFG_SUPPORT_PAGE: usize = 0x04;
const SUPPORT_GRID_OFFSET: usize = 128; // 04h:128 supported grid bitmap (bit7=75GHz, bit5=100GHz)
const LOW_CHANNEL_OFFSET: usize = 158; // 04h:158 >h lowest supported channel number
const HIGH_CHANNEL_OFFSET: usize = 160; // 04h:160 >h highest supported channel number
const MIN_PROG_POWER_OFFSET: usize = 198; // 04h:198 >h min programmable Tx power, scale 100
const MAX_PROG_POWER_OFFSET: usize = 200; // 04h:200 >h max programmable Tx power, scale 100

const TUNABLE_LASER_PAGE: usize = 0x12;
const GRID_SPACING_OFFSET: usize = 128; // 12h:128 grid spacing selection (bits 4-7)
const LASER_CONFIG_CHANNEL_OFFSET: usize = 136; // 12h:136 >h configured channel number
const TX_CONFIG_POWER_OFFSET: usize = 200; // 12h:200 >h configured Tx power, scale 100

// Frequencies are anchored at 193.1 THz (193100 GHz); a channel steps the grid off that base.
const FREQ_BASE_GHZ: i64 = 193100;
// `set_laser_freq` GRID_SPACING byte values (bits 4-7 code): 75GHz→7 (0x70), 100GHz→5 (0x50),
// 150GHz→8 (0x80) — the raw bytes `c_cmis.set_laser_freq` writes.
const GRID_75GHZ_CODE: u8 = 0x70;
const GRID_100GHZ_CODE: u8 = 0x50;
const GRID_150GHZ_CODE: u8 = 0x80;
// The `NumberRegField` ×100 power scale on the Tx-power registers (dBm → hundredths of dBm).
const POWER_SCALE: f64 = 100.0;

/// `c_cmis.CCmisApi.get_freq_grid` — GRID_SPACING nibble code (bits 4-7 of 12h:128) → grid GHz.
/// `None` for the reserved codes 9..15 (mirrors the Python `else: return None`).
fn freq_grid_ghz(code: u8) -> Option<f64> {
    match code {
        8 => Some(150.0),
        7 => Some(75.0),
        6 => Some(33.0),
        5 => Some(100.0),
        4 => Some(50.0),
        3 => Some(25.0),
        2 => Some(12.5),
        1 => Some(6.25),
        0 => Some(3.125),
        _ => None,
    }
}

/// `CmisCodes.DP_PATH_TIMINGS` (codes_cmis.py) — 4-bit state-machine duration code →
/// milliseconds. Codes 14/15 are reserved (0).
const fn dp_path_timing_ms(code: u8) -> f64 {
    match code & 0x0f {
        0 => 1.0,
        1 => 5.0,
        2 => 10.0,
        3 => 50.0,
        4 => 100.0,
        5 => 500.0,
        6 => 1000.0,
        7 => 5000.0,
        8 => 10000.0,
        9 => 60000.0,
        10 => 300000.0,
        11 => 600000.0,
        12 => 3000000.0,
        13 => 6000000.0,
        _ => 0.0,
    }
}

/// `linear = page*128 + offset` (bank 0) — matches `sfp.py:linear_to_bpo` inverse.
const fn cmis_linear(page: usize, offset: usize) -> usize {
    if page == 0 && offset < 128 {
        offset
    } else {
        page * 128 + offset
    }
}

/// Production [`CmisApi`] backed by a bridge [`SfpHandle`]: decode reads use the
/// module's Python decoder, with aggregate getters as a fallback; control writes are
/// the raw register writes (`c_cmis.py` encodings) via `write_eeprom` — the page-10h
/// datapath control bytes, and the page-12h coherent/ZR laser-tuning registers
/// (TX_CONFIG_POWER 12h:200, GRID_SPACING 12h:128, LASER_CONFIG_CHANNEL 12h:136).
pub struct BridgeCmisApi {
    sfp: Box<dyn SfpHandle>,
    /// Per-instance memo of the two aggregate getters. One `BridgeCmisApi` is built per
    /// port per `process_single_lport` sweep, so the memo's lifetime is exactly one
    /// port-sweep — a *consistent snapshot* for that pass, never a stale one across
    /// passes. Any write through this api clears it (see [`Self::invalidate`]) so a
    /// read-after-write in the same handler still sees the register it just set.
    status_memo: Mutex<Option<Value>>,
    info_memo: Mutex<Option<Value>>,
}

impl BridgeCmisApi {
    pub fn new(sfp: Box<dyn SfpHandle>) -> Self {
        BridgeCmisApi {
            sfp,
            status_memo: Mutex::new(None),
            info_memo: Mutex::new(None),
        }
    }

    /// Try the cheap seam: call the decode method straight on the module's xcvr api
    /// (`SfpHandle::xcvr_api_call_json`). `None` means "unavailable — use the
    /// aggregate-derived fallback below", which is what mocks and any handle without a
    /// live plugin always get, so the unit tests keep covering the fallback path.
    fn api_json(&self, method: &str) -> Option<Value> {
        match self.sfp.xcvr_api_call_json(method) {
            Ok(Value::Null) | Err(_) => None,
            Ok(v) => Some(v),
        }
    }

    fn status(&self) -> Value {
        let mut memo = self.status_memo.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(v) = memo.as_ref() {
            return v.clone();
        }
        let v = self
            .sfp
            .get_transceiver_status()
            .unwrap_or_else(|_| json!({}));
        *memo = Some(v.clone());
        v
    }

    fn info(&self) -> Value {
        let mut memo = self.info_memo.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(v) = memo.as_ref() {
            return v.clone();
        }
        let v = self
            .sfp
            .get_transceiver_info()
            .unwrap_or_else(|_| json!({}));
        *memo = Some(v.clone());
        v
    }

    /// Drop the aggregate memo — called after every register write so the next decode
    /// read re-reads the module rather than answering from a pre-write snapshot.
    fn invalidate(&self) {
        *self.status_memo.lock().unwrap_or_else(|e| e.into_inner()) = None;
        *self.info_memo.lock().unwrap_or_else(|e| e.into_inner()) = None;
    }

    fn read_byte(&self, linear: usize) -> Option<u8> {
        self.sfp
            .read_eeprom(linear, 1)
            .ok()
            .flatten()
            .and_then(|v| v.first().copied())
    }

    /// Read a page-01h duration advertisement nibble and map it to milliseconds via
    /// `DP_PATH_TIMINGS`. `high_nibble` selects bits 7:4 (the odd field of the pair) vs
    /// bits 3:0. `None` mirrors `c_cmis`'s unreadable-advertisement → 0 behaviour.
    fn duration_code_ms(&self, offset: usize, high_nibble: bool) -> Option<f64> {
        let byte = self.read_byte(cmis_linear(DURATIONS_PAGE, offset))?;
        let code = if high_nibble { byte >> 4 } else { byte & 0x0f };
        Some(dp_path_timing_ms(code))
    }

    fn write_byte(&self, linear: usize, byte: u8) -> bool {
        self.invalidate();
        self.sfp.write_eeprom(linear, &[byte]).unwrap_or(false)
    }

    /// Read a signed 16-bit big-endian register (`NumberRegField(format=">h", size=2)`).
    /// `None` on a read miss, mirroring `xcvr_eeprom.read` returning `None`.
    fn read_i16_be(&self, linear: usize) -> Option<i16> {
        let bytes = self.sfp.read_eeprom(linear, 2).ok().flatten()?;
        if bytes.len() < 2 {
            return None;
        }
        Some(i16::from_be_bytes([bytes[0], bytes[1]]))
    }

    /// Write a signed 16-bit big-endian register (`NumberRegField(format=">h", size=2)`) —
    /// the two-byte channel/power tuning writes on page 12h.
    fn write_i16_be(&self, linear: usize, val: i16) -> bool {
        self.invalidate();
        self.sfp
            .write_eeprom(linear, &val.to_be_bytes())
            .unwrap_or(false)
    }

    /// Is a page-01h advertisement bit set (`byte & 0x01`)? Unreadable → not supported.
    fn si_adv_bit0(&self, offset: usize) -> bool {
        self.read_byte(cmis_linear(DURATIONS_PAGE, offset))
            .map(|b| b & 0x01 != 0)
            .unwrap_or(false)
    }

    /// Stage one SI control (`c_cmis.scs_lane_write` + the per-control `stage_*`): for
    /// each masked host lane, pack the vendor value for `<param><lane>` (1-based) into the
    /// control's page-10h byte range at `bits_per_lane` LSB-first, then write the affected
    /// bytes. `false` if a masked lane's value is missing/null (mirrors the reference
    /// `si_param_lane_val is None → return False`).
    fn stage_si_param(
        &self,
        base: usize,
        bits_per_lane: u32,
        host_lanes_mask: u32,
        sub: &Value,
        param: &str,
    ) -> bool {
        let field_mask: u32 = (1u32 << bits_per_lane) - 1;
        let mut bytes: std::collections::BTreeMap<usize, u8> = std::collections::BTreeMap::new();
        for lane in 0..CMIS_MAX_HOST_LANES_USIZE {
            if (host_lanes_mask >> lane) & 1 == 0 {
                continue;
            }
            let key = format!("{param}{}", lane + 1);
            let Some(val) = sub.get(&key).and_then(json_as_u32) else {
                return false;
            };
            let bit_start = lane as u32 * bits_per_lane;
            let off = base + (bit_start / 8) as usize;
            let shift = bit_start % 8;
            let cur = bytes
                .entry(off)
                .or_insert_with(|| self.read_byte(cmis_linear(SCS0_PAGE, off)).unwrap_or(0));
            *cur &= !((field_mask as u8) << shift);
            *cur |= ((val & field_mask) as u8) << shift;
        }
        let mut ok = true;
        for (off, byte) in bytes {
            ok &= self.write_byte(cmis_linear(SCS0_PAGE, off), byte);
        }
        ok
    }

    /// `stage_rx_si_settings` for a single RX control — gated by the module's advertised
    /// support. Unknown RX params return `false` (the reference `else: return False`).
    fn stage_rx_si_param(&self, param: &str, host_lanes_mask: u32, sub: &Value) -> bool {
        match param {
            CDR_ENABLE_RX => {
                if self.si_adv_bit0(SI_ADV_RX_CDR_OFFSET) {
                    self.stage_si_param(SI_CDR_ENABLE_RX_OFFSET, 1, host_lanes_mask, sub, param)
                } else {
                    true
                }
            }
            // The emulator advertises only CDR support; the remaining RX controls
            // (output EQ pre/post, amplitude) are applied when present. Their support
            // advertisement offsets are not exercised by the DUT, so they are staged
            // unconditionally here (documented deviation from the per-control gate).
            OUTPUT_EQ_PRE_CURSOR_TARGET_RX => self.stage_si_param(
                SI_OUTPUT_EQ_PRE_CURSOR_TARGET_RX_OFFSET,
                4,
                host_lanes_mask,
                sub,
                param,
            ),
            OUTPUT_EQ_POST_CURSOR_TARGET_RX => self.stage_si_param(
                SI_OUTPUT_EQ_POST_CURSOR_TARGET_RX_OFFSET,
                4,
                host_lanes_mask,
                sub,
                param,
            ),
            OUTPUT_AMPLITUDE_TARGET_RX => self.stage_si_param(
                SI_OUTPUT_AMPLITUDE_TARGET_RX_OFFSET,
                4,
                host_lanes_mask,
                sub,
                param,
            ),
            _ => false,
        }
    }

    /// `stage_tx_si_settings` for a single TX control — gated (CDR) / applied (others).
    fn stage_tx_si_param(&self, param: &str, host_lanes_mask: u32, sub: &Value) -> bool {
        match param {
            CDR_ENABLE_TX => {
                if self.si_adv_bit0(SI_ADV_TX_CDR_OFFSET) {
                    self.stage_si_param(SI_CDR_ENABLE_TX_OFFSET, 1, host_lanes_mask, sub, param)
                } else {
                    true
                }
            }
            FIXED_INPUT_EQ_TARGET_TX => self.stage_si_param(
                SI_FIXED_INPUT_EQ_TARGET_TX_OFFSET,
                4,
                host_lanes_mask,
                sub,
                param,
            ),
            ADAPTIVE_INPUT_EQ_RECALLED_TX => self.stage_si_param(
                SI_ADAPTIVE_INPUT_EQ_RECALLED_TX_OFFSET,
                2,
                host_lanes_mask,
                sub,
                param,
            ),
            ADAPTIVE_INPUT_EQ_ENABLE_TX => self.stage_si_param(
                SI_ADAPTIVE_INPUT_EQ_ENABLE_TX_OFFSET,
                1,
                host_lanes_mask,
                sub,
                param,
            ),
            _ => false,
        }
    }

    /// CMIS major revision (defaults to 5 — the emulator/testbed is CMIS 5.x — when the
    /// register read fails; picks the v4+ deinit/init bit polarity).
    fn cmis_major(&self) -> u8 {
        self.read_byte(CMIS_REV_LINEAR).map(|b| b >> 4).unwrap_or(5)
    }
}

impl CmisApi for BridgeCmisApi {
    fn is_flat_memory(&self) -> bool {
        // `c_cmis.CmisApi.is_flat_memory` reads CMIS 00h:2 bit 7 (FlatMem). That byte
        // lives in lower memory, which is accessible even on a flat module (one that by
        // definition has no paged upper memory), so read the raw bit through the bridge —
        // the same 00h:2.7 test the flat-memory e2e performs (`emu.read(idx,0,0,2,1) &
        // FLAT_MEM_BIT`). A flat CMIS module still decodes its lower-memory identity, so
        // its `get_transceiver_info()` carries `cmis_rev` and `get_transceiver_status()`
        // may carry `module_state`; the heuristic below therefore cannot distinguish a
        // flat CMIS module from a paged one and is kept only as a fallback for the rare
        // case the register read itself is unavailable.
        if let Some(b) = self.read_byte(CMIS_FLAT_MEM_LINEAR) {
            return b & CMIS_FLAT_MEM_BIT != 0;
        }
        self.status().get("module_state").is_none() && self.info().get("cmis_rev").is_none()
    }

    fn is_coherent_module(&self) -> bool {
        // c_cmis.CCmisApi.is_coherent_module = 'ZR' in get_module_media_interface(). Only a
        // coherent module is served by CCmisApi, whose get_transceiver_info() adds the
        // coherent-only markers (supported_max_laser_freq / supported_min_laser_freq /
        // supported_{max,min}_tx_power). The base CmisApi never emits them, so the bridge's
        // own C-CMIS classification surfaces here as the presence of that marker — the same
        // field the coherent e2e keys on to recognise the module.
        if let Some(b) = self
            .api_json("is_coherent_module")
            .and_then(|v| v.as_bool())
        {
            return b;
        }
        self.info().get("supported_max_laser_freq").is_some()
    }

    fn get_module_type_abbreviation(&self) -> Option<String> {
        if let Some(s) = self
            .api_json("get_module_type_abbreviation")
            .and_then(|v| v.as_str().map(|s| s.to_string()))
        {
            return Some(s);
        }
        self.info()
            .get("type_abbrv_name")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
    }

    fn get_module_state(&self) -> String {
        if let Some(s) = self
            .api_json("get_module_state")
            .and_then(|v| v.as_str().map(|s| s.to_string()))
        {
            return s;
        }
        self.status()
            .get("module_state")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string()
    }

    fn get_cmis_rev(&self) -> String {
        if let Some(s) = self
            .api_json("get_cmis_rev")
            .and_then(|v| v.as_str().map(|s| s.to_string()))
        {
            return s;
        }
        self.info()
            .get("cmis_rev")
            .and_then(|v| v.as_str())
            .unwrap_or("5.0")
            .to_string()
    }

    fn get_application_advertisement(&self) -> Value {
        // TRANSCEIVER_INFO carries the advertisement as a Python-dict *repr* string
        // (`str(get_application_advertisement())`); parse it back to JSON keyed by app
        // index. CMIS decode itself stayed in Python; this only re-inflates its output.
        if let Some(v) = self.api_json("get_application_advertisement") {
            if v.is_object() {
                return v;
            }
        }
        match self.info().get("application_advertisement") {
            Some(Value::String(s)) if s != "N/A" => py_repr_to_json(s).unwrap_or_else(|| json!({})),
            _ => json!({}),
        }
    }

    fn get_host_lane_assignment_option(&self, appl: u32) -> u32 {
        advert_field_u32(
            &self.get_application_advertisement(),
            appl,
            "host_lane_assignment_options",
            0,
        )
    }

    fn get_media_lane_count(&self, appl: u32) -> u32 {
        advert_field_u32(
            &self.get_application_advertisement(),
            appl,
            "media_lane_count",
            1,
        )
    }

    fn get_media_lane_assignment_option(&self, appl: u32) -> u32 {
        advert_field_u32(
            &self.get_application_advertisement(),
            appl,
            "media_lane_assignment_options",
            1,
        )
    }

    fn get_datapath_state(&self) -> Value {
        if let Some(v) = self.api_json("get_datapath_state") {
            if v.is_object() {
                return v;
            }
        }
        let st = self.status();
        let mut m = serde_json::Map::new();
        for n in 1..=CMIS_MAX_HOST_LANES_USIZE {
            let v = st
                .get(format!("DP{n}State"))
                .cloned()
                .unwrap_or(Value::Null);
            m.insert(format!("DP{n}State"), v);
        }
        Value::Object(m)
    }

    fn get_config_datapath_hostlane_status(&self) -> Value {
        if let Some(v) = self.api_json("get_config_datapath_hostlane_status") {
            if v.is_object() {
                return v;
            }
        }
        let st = self.status();
        let mut m = serde_json::Map::new();
        for n in 1..=CMIS_MAX_HOST_LANES_USIZE {
            let v = st
                .get(format!("config_state_hostlane{n}"))
                .cloned()
                .unwrap_or(Value::Null);
            m.insert(format!("ConfigStatusLane{n}"), v);
        }
        Value::Object(m)
    }

    fn get_dpinit_pending(&self) -> Value {
        if let Some(v) = self.api_json("get_dpinit_pending") {
            if v.is_object() {
                return v;
            }
        }
        let st = self.status();
        let mut m = serde_json::Map::new();
        for n in 1..=CMIS_MAX_HOST_LANES_USIZE {
            let v = st
                .get(format!("dpinit_pending_hostlane{n}"))
                .cloned()
                .unwrap_or(Value::Bool(false));
            m.insert(format!("DPInitPending{n}"), v);
        }
        Value::Object(m)
    }

    fn get_active_apsel_hostlane(&self) -> Result<Value> {
        // c_cmis.get_active_apsel_hostlane: read the Active Control Set DPConfigLane bytes
        // (page 11h:206..213); the ApSel code applied to host lane <n> is the upper nibble
        // (bits 7:4). A fresh module already advertises its default application here, so a
        // matching port needs no decommission; after ApplyDPInit these reflect the applied
        // app (→ the golden `active_apsel_hostlaneN`). A failed read defaults to 0 ("no
        // active app"), which is the safe value: it never spuriously forces a decommission.
        let mut m = serde_json::Map::new();
        for lane in 0..CMIS_MAX_HOST_LANES_USIZE {
            let lin = cmis_linear(ACS_PAGE, ACS_DPCONFIG_BASE_OFFSET + lane);
            let apsel = self.read_byte(lin).map(|b| u64::from(b >> 4)).unwrap_or(0);
            m.insert(format!("ActiveAppSelLane{}", lane + 1), Value::from(apsel));
        }
        Ok(Value::Object(m))
    }

    fn get_application(&self, lane: u32) -> u32 {
        // The app applied to host `lane` == the Active Control Set AppSelCode (upper nibble
        // of page 11h:206+lane) — same source as `get_active_apsel_hostlane`.
        let lin = cmis_linear(ACS_PAGE, ACS_DPCONFIG_BASE_OFFSET + lane as usize);
        self.read_byte(lin).map(|b| u32::from(b >> 4)).unwrap_or(0)
    }

    fn get_datapath_init_duration(&self) -> f64 {
        // c_cmis.get_datapath_init_duration: flat memory → 0; else DP_PATH_TIMINGS(01h:144 lo),
        // with a short (<=1000 ms) advertised window scaled ×10 (some modules under-report it).
        if self.is_flat_memory() {
            return 0.0;
        }
        match self.duration_code_ms(DP_INIT_DEINIT_OFFSET, false) {
            None => 0.0,
            Some(v) if v <= DATAPATH_INIT_DURATION_OVERRIDE_THRESHOLD => {
                v * DATAPATH_INIT_DURATION_MULTIPLIER
            }
            Some(v) => v,
        }
    }
    fn get_datapath_deinit_duration(&self) -> f64 {
        if self.is_flat_memory() {
            return 0.0;
        }
        self.duration_code_ms(DP_INIT_DEINIT_OFFSET, true)
            .unwrap_or(0.0)
    }
    fn get_datapath_tx_turnon_duration(&self) -> f64 {
        if self.is_flat_memory() {
            return 0.0;
        }
        self.duration_code_ms(DP_TX_TURN_OFFSET, false)
            .unwrap_or(0.0)
    }
    fn get_datapath_tx_turnoff_duration(&self) -> f64 {
        if self.is_flat_memory() {
            return 0.0;
        }
        self.duration_code_ms(DP_TX_TURN_OFFSET, true)
            .unwrap_or(0.0)
    }
    fn get_module_pwr_up_duration(&self) -> f64 {
        if self.is_flat_memory() {
            return 0.0;
        }
        self.duration_code_ms(MODULE_PWR_OFFSET, false)
            .unwrap_or(0.0)
    }
    fn get_module_pwr_down_duration(&self) -> f64 {
        if self.is_flat_memory() {
            return 0.0;
        }
        self.duration_code_ms(MODULE_PWR_OFFSET, true)
            .unwrap_or(0.0)
    }

    fn get_tx_config_power(&self) -> f64 {
        // c_cmis: read TX_CONFIG_POWER (12h:200) as >h and apply the ×100 scale → dBm.
        self.read_i16_be(cmis_linear(TUNABLE_LASER_PAGE, TX_CONFIG_POWER_OFFSET))
            .map(|raw| raw as f64 / POWER_SCALE)
            .unwrap_or(0.0)
    }
    fn get_laser_config_freq(&self) -> i64 {
        // c_cmis.get_laser_config_freq: decode the configured grid (12h:128 bits 4-7) and
        // channel (12h:136 >h), then freq = 193100 + channel*grid. 75/150GHz use the OIF
        // additive forms; a reserved grid code yields 0 (nothing configured).
        let grid_byte = self
            .read_byte(cmis_linear(TUNABLE_LASER_PAGE, GRID_SPACING_OFFSET))
            .unwrap_or(0);
        let channel = self
            .read_i16_be(cmis_linear(TUNABLE_LASER_PAGE, LASER_CONFIG_CHANNEL_OFFSET))
            .unwrap_or(0) as i64;
        match freq_grid_ghz((grid_byte & 0xF0) >> 4) {
            Some(g) if (g - 75.0).abs() < f64::EPSILON => FREQ_BASE_GHZ + channel * 25,
            Some(g) if (g - 150.0).abs() < f64::EPSILON => FREQ_BASE_GHZ + (channel + 3) * 25,
            Some(g) => FREQ_BASE_GHZ + (channel as f64 * g).round() as i64,
            None => 0,
        }
    }
    fn get_supported_freq_config(&self) -> (i64, i64, i64, i64, i64) {
        // c_cmis.get_supported_freq_config: SUPPORT_GRID (04h:128 byte bitmap), LOW/HIGH
        // channel (04h:158/160 >h), and low/high supported frequency = 193100 + channel*25.
        let grid = self
            .read_byte(cmis_linear(CCMIS_CFG_SUPPORT_PAGE, SUPPORT_GRID_OFFSET))
            .unwrap_or(0) as i64;
        let low_ch = self
            .read_i16_be(cmis_linear(CCMIS_CFG_SUPPORT_PAGE, LOW_CHANNEL_OFFSET))
            .unwrap_or(0) as i64;
        let hi_ch = self
            .read_i16_be(cmis_linear(CCMIS_CFG_SUPPORT_PAGE, HIGH_CHANNEL_OFFSET))
            .unwrap_or(0) as i64;
        let low_freq = FREQ_BASE_GHZ + low_ch * 25;
        let high_freq = FREQ_BASE_GHZ + hi_ch * 25;
        (grid, low_ch, hi_ch, low_freq, high_freq)
    }
    fn get_supported_power_config(&self) -> (f64, f64) {
        // c_cmis.get_supported_power_config: MIN/MAX_PROG_OUTPUT_POWER (04h:198/200 >h, ×100).
        let min_p = self
            .read_i16_be(cmis_linear(CCMIS_CFG_SUPPORT_PAGE, MIN_PROG_POWER_OFFSET))
            .map(|raw| raw as f64 / POWER_SCALE)
            .unwrap_or(0.0);
        let max_p = self
            .read_i16_be(cmis_linear(CCMIS_CFG_SUPPORT_PAGE, MAX_PROG_POWER_OFFSET))
            .map(|raw| raw as f64 / POWER_SCALE)
            .unwrap_or(0.0);
        (min_p, max_p)
    }
    fn get_tuning_in_progress(&self) -> bool {
        false
    }
    fn get_manufacturer(&self) -> String {
        self.info()
            .get("manufacturer")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string()
    }
    fn get_model(&self) -> String {
        self.info()
            .get("model")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string()
    }

    fn set_datapath_deinit(&self, host_lanes_mask: u32) -> bool {
        // c_cmis.set_datapath_deinit: read DataPathDeinit; v4+ SET the masked lane bits
        // (v3 clears). Emulator is CMIS 5.x → v4+.
        let lin = cmis_linear(SCS0_PAGE, DPDEINIT_OFFSET);
        let mut data = self.read_byte(lin).unwrap_or(0);
        for lane in 0..CMIS_MAX_HOST_LANES_USIZE {
            if (host_lanes_mask >> lane) & 1 == 0 {
                continue;
            }
            if self.cmis_major() >= 4 {
                data |= 1 << lane;
            } else {
                data &= !(1 << lane);
            }
        }
        self.write_byte(lin, data)
    }

    fn set_datapath_init(&self, host_lanes_mask: u32) -> bool {
        // c_cmis.set_datapath_init: v4+ CLEARS the masked DataPathDeinit lane bits.
        let lin = cmis_linear(SCS0_PAGE, DPDEINIT_OFFSET);
        let mut data = self.read_byte(lin).unwrap_or(0);
        for lane in 0..CMIS_MAX_HOST_LANES_USIZE {
            if (host_lanes_mask >> lane) & 1 == 0 {
                continue;
            }
            if self.cmis_major() >= 4 {
                data &= !(1 << lane);
            } else {
                data |= 1 << lane;
            }
        }
        self.write_byte(lin, data)
    }

    fn tx_disable_channel(&self, media_lanes_mask: u32, disable: bool) -> bool {
        // c_cmis.tx_disable_channel: read OutputDisableTx, set/clear masked lane bits.
        let lin = cmis_linear(SCS0_PAGE, OUTPUT_DISABLE_TX_OFFSET);
        let mut state = self.read_byte(lin).unwrap_or(0);
        for i in 0..CMIS_MAX_HOST_LANES_USIZE {
            if (media_lanes_mask >> i) & 1 == 0 {
                continue;
            }
            if disable {
                state |= 1 << i;
            } else {
                state &= !(1 << i);
            }
        }
        self.write_byte(lin, state)
    }

    fn set_lpmode(&self, lpmode: bool, _wait_state_change: bool) -> bool {
        // Delegate to the bridge's own set_lpmode (Python CMIS decode owns the
        // MODULE_LEVEL_CONTROL bit math + power-up/down wait).
        self.invalidate();
        self.sfp.set_lpmode(lpmode).unwrap_or(false)
    }

    fn set_application(&self, host_lanes_mask: u32, appl: u32, ec: u32) -> bool {
        // c_cmis.set_application: for each masked host lane, write DPConfigLane =
        // (appl<<4) | (lane_first<<1) | ec, where lane_first is the first masked lane.
        let mut lane_first: i32 = -1;
        let mut ok = true;
        for lane in 0..CMIS_MAX_HOST_LANES_USIZE {
            if (host_lanes_mask >> lane) & 1 == 0 {
                continue;
            }
            if lane_first < 0 {
                lane_first = lane as i32;
            }
            let data = ((appl << 4) | ((lane_first as u32) << 1) | ec) as u8;
            let lin = cmis_linear(SCS0_PAGE, DPCONFIG_BASE_OFFSET + lane);
            ok &= self.write_byte(lin, data);
        }
        ok
    }

    fn scs_apply_datapath_init(&self, host_lanes_mask: u32) -> bool {
        // c_cmis.scs_apply_datapath_init: write ApplyDPInitLane = mask.
        let lin = cmis_linear(SCS0_PAGE, APPLY_DPINIT_OFFSET);
        self.write_byte(lin, host_lanes_mask as u8)
    }

    fn stage_custom_si_settings(&self, host_lanes_mask: u32, optics_si_dict: &Value) -> bool {
        let Some(obj) = optics_si_dict.as_object() else {
            return true;
        };
        // c_cmis.stage_custom_si_settings: split by the Tx/Rx suffix, then stage RX
        // controls before TX controls. Insertion order preserved (serde_json
        // preserve_order) so a vendor file's parameter ordering is honoured.
        for (param, sub) in obj {
            if param.ends_with("Rx") && !self.stage_rx_si_param(param, host_lanes_mask, sub) {
                return false;
            }
        }
        for (param, sub) in obj {
            if param.ends_with("Tx") && !self.stage_tx_si_param(param, host_lanes_mask, sub) {
                return false;
            }
        }
        true
    }

    fn set_tx_power(&self, tx_power: f64) -> bool {
        // c_cmis.set_tx_power: write TX_CONFIG_POWER (12h:200) as a >h value scaled ×100
        // (dBm → hundredths), then a 1s settle while the module latches the target power.
        let raw = (tx_power * POWER_SCALE).round() as i16;
        let status =
            self.write_i16_be(cmis_linear(TUNABLE_LASER_PAGE, TX_CONFIG_POWER_OFFSET), raw);
        std::thread::sleep(std::time::Duration::from_secs(1));
        status
    }
    fn set_laser_freq(&self, freq: i64, grid: u32) -> bool {
        // c_cmis.set_laser_freq: validate the grid is advertised + the channel is on-grid,
        // write GRID_SPACING (12h:128) then LASER_CONFIG_CHANNEL (12h:136 >h). Return false
        // on an unsupported grid / off-grid channel / out-of-range channel (the Python
        // asserts / ValueError paths), leaving the daemon to skip the (already-validated)
        // tuning gracefully rather than panic.
        let (grid_supported, low_ch, hi_ch, _, _) = self.get_supported_freq_config();
        let (freq_grid, channel) = match grid {
            75 => {
                if (grid_supported >> 7) & 0x1 != 1 {
                    return false;
                }
                let ch = ((freq - FREQ_BASE_GHZ) as f64 / 25.0).round() as i64;
                if ch % 3 != 0 {
                    return false;
                }
                (GRID_75GHZ_CODE, ch)
            }
            100 => {
                if (grid_supported >> 5) & 0x1 != 1 {
                    return false;
                }
                let ch = ((freq - FREQ_BASE_GHZ) as f64 / 100.0).round() as i64;
                (GRID_100GHZ_CODE, ch)
            }
            150 => {
                let ch = ((freq - FREQ_BASE_GHZ) as f64 / 25.0).round() as i64 - 3;
                if ch % 6 != 0 {
                    return false;
                }
                (GRID_150GHZ_CODE, ch)
            }
            _ => return false,
        };
        if !self.write_byte(
            cmis_linear(TUNABLE_LASER_PAGE, GRID_SPACING_OFFSET),
            freq_grid,
        ) {
            return false;
        }
        // Range check AFTER the grid write, mirroring the Python (which raises ValueError here).
        if channel > hi_ch || channel < low_ch {
            return false;
        }
        self.write_i16_be(
            cmis_linear(TUNABLE_LASER_PAGE, LASER_CONFIG_CHANNEL_OFFSET),
            channel as i16,
        )
    }
}

fn json_as_u32(v: &Value) -> Option<u32> {
    v.as_u64()
        .map(|n| n as u32)
        .or_else(|| v.as_str().and_then(|s| s.parse::<u32>().ok()))
}

fn advert_field_u32(advert: &Value, appl: u32, field: &str, default: u32) -> u32 {
    advert
        .get(appl.to_string())
        .and_then(|app| app.get(field))
        .and_then(json_as_u32)
        .unwrap_or(default)
}

/// Convert a Python dict *repr* (from `str(get_application_advertisement())`) to JSON.
/// The emulator output is well-formed: single-quoted strings, integer app keys, integer
/// counts, `True`/`False`/`None` literals. Best-effort — used only by [`BridgeCmisApi`]
/// (e2e), never by the mock-driven unit tests.
fn py_repr_to_json(s: &str) -> Option<Value> {
    // 1) single quotes → double quotes.
    let mut dq = String::with_capacity(s.len() + 16);
    for c in s.chars() {
        if c == '\'' {
            dq.push('"');
        } else {
            dq.push(c);
        }
    }
    // 2) Python literals → JSON (safe for the fixed advertisement field set).
    let dq = dq
        .replace("True", "true")
        .replace("False", "false")
        .replace("None", "null");
    // 3) quote bare integer *keys* (`{1:` / `, 2:` → `{"1":` / `, "2":`).
    let json = quote_int_keys(&dq);
    serde_json::from_str(&json).ok()
}

fn quote_int_keys(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = String::with_capacity(s.len() + 16);
    let mut i = 0usize;
    let mut in_str = false;
    while i < bytes.len() {
        let c = bytes[i] as char;
        if c == '"' {
            in_str = !in_str;
            out.push(c);
            i += 1;
            continue;
        }
        if !in_str && c.is_ascii_digit() {
            let mut j = i;
            while j < bytes.len() && (bytes[j] as char).is_ascii_digit() {
                j += 1;
            }
            let mut k = j;
            while k < bytes.len() && (bytes[k] as char) == ' ' {
                k += 1;
            }
            let prev = out.trim_end().chars().last();
            let is_key = matches!(prev, Some('{') | Some(','))
                && k < bytes.len()
                && (bytes[k] as char) == ':';
            if is_key {
                out.push('"');
                out.push_str(&s[i..j]);
                out.push('"');
            } else {
                out.push_str(&s[i..j]);
            }
            i = j;
            continue;
        }
        out.push(c);
        i += 1;
    }
    out
}

// =====================================================================================
// MockCmisApi — Part-B double (canned decode + settable/sequenced state + counters).
// =====================================================================================

struct MockInner {
    is_flat_memory: bool,
    is_coherent_module: bool,
    module_type_abbreviation: Option<String>,
    module_state: String,
    cmis_rev: String,
    application_advertisement: Value,
    datapath_state: Value,
    config_status: Value,
    dpinit_pending: Value,
    active_apsel: Value,
    active_apsel_queue: VecDeque<Result<Value>>,
    application_by_lane: u32,
    application_by_lane_map: HashMap<u32, u32>,
    media_lane_count_override: Option<u32>,
    dp_init_dur: f64,
    dp_deinit_dur: f64,
    dp_txon_dur: f64,
    dp_txoff_dur: f64,
    pwr_up_dur: f64,
    pwr_down_dur: f64,
    tx_config_power: f64,
    laser_config_freq: i64,
    tuning_in_progress: bool,
    supported_freq_config: (i64, i64, i64, i64, i64),
    supported_power_config: (f64, f64),
    manufacturer: String,
    model: String,
    calls: HashMap<String, usize>,
    captured_optics_si: Value,
    stage_custom_si_result: bool,
    last_set_application_ec: u32,
}

impl Default for MockInner {
    fn default() -> Self {
        MockInner {
            is_flat_memory: false,
            is_coherent_module: false,
            module_type_abbreviation: Some("QSFP-DD".to_string()),
            module_state: "ModuleReady".to_string(),
            cmis_rev: "5.0".to_string(),
            application_advertisement: json!({}),
            datapath_state: json!({}),
            config_status: json!({}),
            dpinit_pending: json!({}),
            active_apsel: json!({}),
            active_apsel_queue: VecDeque::new(),
            application_by_lane: 0,
            application_by_lane_map: HashMap::new(),
            media_lane_count_override: None,
            dp_init_dur: 5_000.0,
            dp_deinit_dur: 1.0,
            dp_txon_dur: 1.0,
            dp_txoff_dur: 1.0,
            pwr_up_dur: 1.0,
            pwr_down_dur: 1.0,
            tx_config_power: 0.0,
            laser_config_freq: 0,
            tuning_in_progress: false,
            supported_freq_config: (0, 0, 0, 0, 0),
            supported_power_config: (-40.0, 10.0),
            manufacturer: String::new(),
            model: String::new(),
            calls: HashMap::new(),
            captured_optics_si: json!({}),
            stage_custom_si_result: true,
            last_set_application_ec: 0,
        }
    }
}

/// Part-B mock [`CmisApi`] — the Rust analogue of the Python tests' `MagicMock()` xcvr
/// api. Interior-mutable + `Clone` (shares one `Arc<Mutex>`), so the api the state
/// machine obtains and the handle the test drives observe the same call counters and
/// sequenced/settable return values.
#[derive(Clone)]
pub struct MockCmisApi {
    inner: Arc<Mutex<MockInner>>,
}

impl Default for MockCmisApi {
    fn default() -> Self {
        MockCmisApi {
            inner: Arc::new(Mutex::new(MockInner::default())),
        }
    }
}

impl MockCmisApi {
    pub fn new() -> Self {
        Self::default()
    }

    fn bump(&self, name: &str) {
        let mut g = self.inner.lock().unwrap();
        *g.calls.entry(name.to_string()).or_insert(0) += 1;
    }

    /// How many times `method` was invoked on this api (across clones).
    pub fn call_count(&self, method: &str) -> usize {
        *self.inner.lock().unwrap().calls.get(method).unwrap_or(&0)
    }

    // --- builders / setters (chainable) ---
    pub fn set_flat_memory(&self, v: bool) {
        self.inner.lock().unwrap().is_flat_memory = v;
    }
    pub fn set_coherent_module(&self, v: bool) {
        self.inner.lock().unwrap().is_coherent_module = v;
    }
    pub fn set_module_type_abbreviation(&self, v: Option<&str>) {
        self.inner.lock().unwrap().module_type_abbreviation = v.map(|s| s.to_string());
    }
    pub fn set_module_state(&self, v: &str) {
        self.inner.lock().unwrap().module_state = v.to_string();
    }
    pub fn set_cmis_rev(&self, v: &str) {
        self.inner.lock().unwrap().cmis_rev = v.to_string();
    }
    pub fn set_application_advertisement(&self, v: Value) {
        self.inner.lock().unwrap().application_advertisement = v;
    }
    pub fn set_datapath_state_value(&self, v: Value) {
        self.inner.lock().unwrap().datapath_state = v;
    }
    pub fn set_config_status(&self, v: Value) {
        self.inner.lock().unwrap().config_status = v;
    }
    pub fn set_dpinit_pending(&self, v: Value) {
        self.inner.lock().unwrap().dpinit_pending = v;
    }
    pub fn set_active_apsel(&self, v: Value) {
        self.inner.lock().unwrap().active_apsel = v;
    }
    /// Queue a sequenced `get_active_apsel_hostlane` result (the analogue of a
    /// `MagicMock(side_effect=[...])` entry; an `Err` mirrors `NotImplementedError`).
    pub fn push_active_apsel_result(&self, r: Result<Value>) {
        self.inner.lock().unwrap().active_apsel_queue.push_back(r);
    }
    pub fn set_application_by_lane(&self, appl: u32) {
        self.inner.lock().unwrap().application_by_lane = appl;
    }
    /// Set the per-lane application code returned by `get_application(lane)`.
    /// `is_cmis_application_update_required` inspects each host lane's currently-active
    /// application; this lets a test paint a distinct code per lane. When the map is
    /// empty, `get_application` falls back to the flat `application_by_lane` value so
    /// existing single-value tests are unaffected.
    pub fn set_application_for_lane(&self, lane: u32, appl: u32) {
        self.inner
            .lock()
            .unwrap()
            .application_by_lane_map
            .insert(lane, appl);
    }
    pub fn set_media_lane_count_override(&self, v: Option<u32>) {
        self.inner.lock().unwrap().media_lane_count_override = v;
    }
    pub fn set_supported_freq_config(&self, v: (i64, i64, i64, i64, i64)) {
        self.inner.lock().unwrap().supported_freq_config = v;
    }
    pub fn set_tx_config_power(&self, v: f64) {
        self.inner.lock().unwrap().tx_config_power = v;
    }
    pub fn set_laser_config_freq(&self, v: i64) {
        self.inner.lock().unwrap().laser_config_freq = v;
    }
    pub fn set_manufacturer(&self, v: &str) {
        self.inner.lock().unwrap().manufacturer = v.to_string();
    }
    pub fn set_model(&self, v: &str) {
        self.inner.lock().unwrap().model = v.to_string();
    }
    /// Make the next `stage_custom_si_settings` return `false` (staging failure path).
    pub fn set_stage_custom_si_result(&self, v: bool) {
        self.inner.lock().unwrap().stage_custom_si_result = v;
    }
    /// The `optics_si_dict` captured by the last `stage_custom_si_settings` call.
    pub fn captured_optics_si(&self) -> Value {
        self.inner.lock().unwrap().captured_optics_si.clone()
    }
    /// The explicit-control (`ec`) argument captured by the last `set_application` call.
    pub fn last_set_application_ec(&self) -> u32 {
        self.inner.lock().unwrap().last_set_application_ec
    }
    /// Override the advertised state-machine durations (milliseconds), the way a test would
    /// stub `api.get_*_duration()`. Lets a test drive timer expiry / retry deterministically.
    pub fn set_durations_ms(
        &self,
        dp_init: f64,
        dp_deinit: f64,
        dp_txon: f64,
        dp_txoff: f64,
        pwr_up: f64,
        pwr_down: f64,
    ) {
        let mut g = self.inner.lock().unwrap();
        g.dp_init_dur = dp_init;
        g.dp_deinit_dur = dp_deinit;
        g.dp_txon_dur = dp_txon;
        g.dp_txoff_dur = dp_txoff;
        g.pwr_up_dur = pwr_up;
        g.pwr_down_dur = pwr_down;
    }
}

impl CmisApi for MockCmisApi {
    fn is_flat_memory(&self) -> bool {
        self.inner.lock().unwrap().is_flat_memory
    }
    fn is_coherent_module(&self) -> bool {
        self.inner.lock().unwrap().is_coherent_module
    }
    fn get_module_type_abbreviation(&self) -> Option<String> {
        self.inner.lock().unwrap().module_type_abbreviation.clone()
    }
    fn get_module_state(&self) -> String {
        self.inner.lock().unwrap().module_state.clone()
    }
    fn get_cmis_rev(&self) -> String {
        self.inner.lock().unwrap().cmis_rev.clone()
    }
    fn get_application_advertisement(&self) -> Value {
        self.inner.lock().unwrap().application_advertisement.clone()
    }
    fn get_host_lane_assignment_option(&self, appl: u32) -> u32 {
        advert_field_u32(
            &self.inner.lock().unwrap().application_advertisement,
            appl,
            "host_lane_assignment_options",
            0,
        )
    }
    fn get_media_lane_count(&self, appl: u32) -> u32 {
        let g = self.inner.lock().unwrap();
        if let Some(v) = g.media_lane_count_override {
            return v;
        }
        advert_field_u32(&g.application_advertisement, appl, "media_lane_count", 1)
    }
    fn get_media_lane_assignment_option(&self, appl: u32) -> u32 {
        advert_field_u32(
            &self.inner.lock().unwrap().application_advertisement,
            appl,
            "media_lane_assignment_options",
            1,
        )
    }
    fn get_datapath_state(&self) -> Value {
        self.inner.lock().unwrap().datapath_state.clone()
    }
    fn get_config_datapath_hostlane_status(&self) -> Value {
        self.inner.lock().unwrap().config_status.clone()
    }
    fn get_dpinit_pending(&self) -> Value {
        self.inner.lock().unwrap().dpinit_pending.clone()
    }
    fn get_active_apsel_hostlane(&self) -> Result<Value> {
        self.bump("get_active_apsel_hostlane");
        let mut g = self.inner.lock().unwrap();
        if let Some(r) = g.active_apsel_queue.pop_front() {
            return r;
        }
        Ok(g.active_apsel.clone())
    }
    fn get_application(&self, lane: u32) -> u32 {
        let g = self.inner.lock().unwrap();
        if g.application_by_lane_map.is_empty() {
            g.application_by_lane
        } else {
            g.application_by_lane_map.get(&lane).copied().unwrap_or(0)
        }
    }

    fn get_datapath_init_duration(&self) -> f64 {
        self.inner.lock().unwrap().dp_init_dur
    }
    fn get_datapath_deinit_duration(&self) -> f64 {
        self.inner.lock().unwrap().dp_deinit_dur
    }
    fn get_datapath_tx_turnon_duration(&self) -> f64 {
        self.inner.lock().unwrap().dp_txon_dur
    }
    fn get_datapath_tx_turnoff_duration(&self) -> f64 {
        self.inner.lock().unwrap().dp_txoff_dur
    }
    fn get_module_pwr_up_duration(&self) -> f64 {
        self.inner.lock().unwrap().pwr_up_dur
    }
    fn get_module_pwr_down_duration(&self) -> f64 {
        self.inner.lock().unwrap().pwr_down_dur
    }

    fn get_tx_config_power(&self) -> f64 {
        self.inner.lock().unwrap().tx_config_power
    }
    fn get_laser_config_freq(&self) -> i64 {
        self.inner.lock().unwrap().laser_config_freq
    }
    fn get_supported_freq_config(&self) -> (i64, i64, i64, i64, i64) {
        self.inner.lock().unwrap().supported_freq_config
    }
    fn get_supported_power_config(&self) -> (f64, f64) {
        self.inner.lock().unwrap().supported_power_config
    }
    fn get_tuning_in_progress(&self) -> bool {
        self.inner.lock().unwrap().tuning_in_progress
    }
    fn get_manufacturer(&self) -> String {
        self.inner.lock().unwrap().manufacturer.clone()
    }
    fn get_model(&self) -> String {
        self.inner.lock().unwrap().model.clone()
    }

    fn set_datapath_deinit(&self, _host_lanes_mask: u32) -> bool {
        self.bump("set_datapath_deinit");
        true
    }
    fn set_datapath_init(&self, _host_lanes_mask: u32) -> bool {
        self.bump("set_datapath_init");
        true
    }
    fn tx_disable_channel(&self, _media_lanes_mask: u32, _disable: bool) -> bool {
        self.bump("tx_disable_channel");
        true
    }
    fn set_lpmode(&self, _lpmode: bool, _wait_state_change: bool) -> bool {
        self.bump("set_lpmode");
        true
    }
    fn set_application(&self, _host_lanes_mask: u32, _appl: u32, ec: u32) -> bool {
        self.bump("set_application");
        self.inner.lock().unwrap().last_set_application_ec = ec;
        true
    }
    fn scs_apply_datapath_init(&self, _host_lanes_mask: u32) -> bool {
        self.bump("scs_apply_datapath_init");
        true
    }
    fn stage_custom_si_settings(&self, _host_lanes_mask: u32, optics_si_dict: &Value) -> bool {
        self.bump("stage_custom_si_settings");
        let mut g = self.inner.lock().unwrap();
        g.captured_optics_si = optics_si_dict.clone();
        g.stage_custom_si_result
    }
    fn set_tx_power(&self, _tx_power: f64) -> bool {
        self.bump("set_tx_power");
        true
    }
    fn set_laser_freq(&self, _freq: i64, _grid: u32) -> bool {
        self.bump("set_laser_freq");
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::mock::MockSfp;
    use std::sync::atomic::Ordering;

    #[test]
    fn py_repr_to_json_parses_advertisement() {
        let s = "{1: {'host_electrical_interface_id': '400GAUI-8 C2M (Annex 120E)', \
                 'host_lane_count': 8, 'media_lane_count': 4, 'host_lane_assignment_options': 1}, \
                 2: {'host_electrical_interface_id': 'CAUI-4 C2M (Annex 83E)', 'host_lane_count': 4, \
                 'media_lane_count': 4, 'host_lane_assignment_options': 17}}";
        let v = py_repr_to_json(s).expect("parse");
        assert_eq!(v["1"]["host_lane_count"], json!(8));
        assert_eq!(
            v["1"]["host_electrical_interface_id"],
            json!("400GAUI-8 C2M (Annex 120E)")
        );
        assert_eq!(v["2"]["host_lane_assignment_options"], json!(17));
    }

    #[test]
    fn cmis_linear_matches_optoe_inverse() {
        assert_eq!(cmis_linear(SCS0_PAGE, DPDEINIT_OFFSET), 2176);
        assert_eq!(cmis_linear(SCS0_PAGE, OUTPUT_DISABLE_TX_OFFSET), 2178);
        assert_eq!(cmis_linear(SCS0_PAGE, APPLY_DPINIT_OFFSET), 2191);
        assert_eq!(cmis_linear(SCS0_PAGE, DPCONFIG_BASE_OFFSET), 2193);
        assert_eq!(cmis_linear(ACS_PAGE, ACS_DPCONFIG_BASE_OFFSET), 2382);
        assert_eq!(cmis_linear(0, 26), 26);
    }

    // ---- BridgeCmisApi sources the active AppSel from the module's Active Control Set
    //      (page 11h:206.., AppSelCode = upper nibble), NOT from a TRANSCEIVER_INFO field.
    //      Root-cause fix: an unprovisioned lane reads as 0 (not Null) so
    //      is_decommission_required never errors, and an applied lane reports its real app. ----
    #[test]
    fn bridge_active_apsel_reads_acs_register_upper_nibble() {
        use crate::mock::MockSfp;
        let base = cmis_linear(ACS_PAGE, ACS_DPCONFIG_BASE_OFFSET);
        let mut sfp = MockSfp::present();
        // Host lanes 1-4 provisioned to AppSelCode 1 (upper nibble) with their DataPathID in
        // the low nibble; lanes 5-8 left unseeded -> read miss -> default 0.
        for lane in 0..4u8 {
            sfp = sfp.with_eeprom(base + lane as usize, 0x10 | (lane << 1));
        }
        let api = BridgeCmisApi::new(Box::new(sfp));

        let apsel = api.get_active_apsel_hostlane().expect("acs read");
        assert_eq!(apsel["ActiveAppSelLane1"], json!(1));
        assert_eq!(apsel["ActiveAppSelLane4"], json!(1));
        // Unprovisioned lane -> 0 (never Null), so decommission math stays well-defined.
        assert_eq!(apsel["ActiveAppSelLane5"], json!(0));
        assert_eq!(apsel["ActiveAppSelLane8"], json!(0));
        assert_eq!(api.get_application(0), 1);
        assert_eq!(api.get_application(3), 1);
        assert_eq!(api.get_application(4), 0);
    }

    #[test]
    fn mock_counts_calls_and_pops_sequenced_apsel() {
        let api = MockCmisApi::new();
        assert!(api.set_datapath_deinit(0xff));
        assert!(api.set_datapath_deinit(0xff));
        assert_eq!(api.call_count("set_datapath_deinit"), 2);

        api.set_active_apsel(json!({"ActiveAppSelLane1": 1}));
        api.push_active_apsel_result(Ok(json!({"ActiveAppSelLane1": 2})));
        api.push_active_apsel_result(Err(crate::error::XcvrdError::Other(
            "not implemented".into(),
        )));
        // queued results pop first, then the settable default.
        assert_eq!(
            api.get_active_apsel_hostlane().unwrap()["ActiveAppSelLane1"],
            json!(2)
        );
        assert!(api.get_active_apsel_hostlane().is_err());
        assert_eq!(
            api.get_active_apsel_hostlane().unwrap()["ActiveAppSelLane1"],
            json!(1)
        );
    }

    // ---- BridgeCmisApi decodes the state-machine max-durations from the module's page-01h
    //      advertisement via DP_PATH_TIMINGS (mirrors c_cmis.get_*_duration), instead of the
    //      old hard-coded CMIS spec maximums. The emulator advertises DPInit=BETWEEN_1_AND_5_S
    //      (code 7 → 5000 ms) and every other duration LESS_THAN_1_MS (code 0 → 1 ms); an
    //      AP_CONF timer of max(pwr_up, dp_deinit)=1 ms (not 600 s) lets a stalled datapath
    //      cross CMIS_MAX_RETRIES → FAILED inside the e2e budget. ----
    #[test]
    fn bridge_durations_decode_page01h_via_dp_path_timings() {
        use crate::mock::MockSfp;
        // module_state present → not flat memory, so the advertisement is read.
        let paged = |sfp: MockSfp| sfp.with_status(json!({"module_state": "ModuleReady"}));
        let dpinit_deinit = cmis_linear(DURATIONS_PAGE, DP_INIT_DEINIT_OFFSET); // 272
        let pwr = cmis_linear(DURATIONS_PAGE, MODULE_PWR_OFFSET); // 295
        let tx = cmis_linear(DURATIONS_PAGE, DP_TX_TURN_OFFSET); // 296
        assert_eq!((dpinit_deinit, pwr, tx), (272, 295, 296));

        // Emulator layout: 01h:144 = 0x07 (lo DPInit=7, hi DPDeinit=0); 01h:167 = 0; 01h:168 = 0.
        let sfp = paged(MockSfp::present())
            .with_eeprom(dpinit_deinit, 0x07)
            .with_eeprom(pwr, 0x00)
            .with_eeprom(tx, 0x00);
        let api = BridgeCmisApi::new(Box::new(sfp));
        assert_eq!(api.get_datapath_init_duration(), 5000.0); // code 7 → 5000, >1000 no ×10
        assert_eq!(api.get_datapath_deinit_duration(), 1.0); // code 0 → 1 ms
        assert_eq!(api.get_datapath_tx_turnon_duration(), 1.0);
        assert_eq!(api.get_datapath_tx_turnoff_duration(), 1.0);
        assert_eq!(api.get_module_pwr_up_duration(), 1.0);
        assert_eq!(api.get_module_pwr_down_duration(), 1.0);

        // Nibble split + DPInit ×10 override: 01h:144 = 0x63 → DPInit lo=3 (50 ms ≤ 1000 → 500),
        // DPDeinit hi=6 (1000 ms, no override). 01h:168 = 0x9A → TxOn lo=10 (300000), TxOff hi=9 (60000).
        let sfp2 = paged(MockSfp::present())
            .with_eeprom(dpinit_deinit, 0x63)
            .with_eeprom(tx, 0x9A);
        let api2 = BridgeCmisApi::new(Box::new(sfp2));
        assert_eq!(api2.get_datapath_init_duration(), 500.0);
        assert_eq!(api2.get_datapath_deinit_duration(), 1000.0);
        assert_eq!(api2.get_datapath_tx_turnon_duration(), 300000.0);
        assert_eq!(api2.get_datapath_tx_turnoff_duration(), 60000.0);

        // Unreadable advertisement (unseeded page-01h) → 0, mirroring c_cmis's None → 0.
        let api3 = BridgeCmisApi::new(Box::new(paged(MockSfp::present())));
        assert_eq!(api3.get_datapath_deinit_duration(), 0.0);
        assert_eq!(api3.get_datapath_init_duration(), 0.0);

        // Flat memory → 0 (c_cmis short-circuits before the read).
        let api4 = BridgeCmisApi::new(Box::new(
            MockSfp::present().with_eeprom(dpinit_deinit, 0x07),
        ));
        assert!(api4.is_flat_memory());
        assert_eq!(api4.get_datapath_init_duration(), 0.0);
        assert_eq!(api4.get_datapath_deinit_duration(), 0.0);
    }

    // ---- Coherent/ZR (C-CMIS) laser tuning (root-cause fix). The BridgeCmisApi
    //      previously stubbed every coherent method (is_coherent_module→false disabled the
    //      whole tuning control plane), so the daemon never drove the page-12h tuning writes
    //      the e2e (test_coherent_tuning_writes) asserts. These cover the ported c_cmis
    //      encodings: coherent detection, the page-04h/12h decode getters, and the
    //      set_tx_power / set_laser_freq register writes. ----

    // Two-byte big-endian seed helper for the >h fields (MockSfp seeds one byte at a time).
    fn seed_i16_be(sfp: crate::mock::MockSfp, linear: usize, val: i16) -> crate::mock::MockSfp {
        let b = val.to_be_bytes();
        sfp.with_eeprom(linear, b[0]).with_eeprom(linear + 1, b[1])
    }

    #[test]
    fn bridge_is_coherent_from_supported_max_laser_freq_marker() {
        use crate::mock::MockSfp;
        // Only CCmisApi.get_transceiver_info() emits supported_max_laser_freq; its presence
        // is the coherent signal (mirrors is_coherent_module = 'ZR' in media interface).
        let coherent = BridgeCmisApi::new(Box::new(
            MockSfp::present().with_info(json!({"supported_max_laser_freq": 195600})),
        ));
        assert!(coherent.is_coherent_module());

        let plain = BridgeCmisApi::new(Box::new(
            MockSfp::present().with_info(json!({"manufacturer": "ACME"})),
        ));
        assert!(!plain.is_coherent_module());
    }

    #[test]
    fn bridge_coherent_getters_decode_page04h_and_page12h() {
        use crate::mock::MockSfp;
        let mut sfp = MockSfp::present();
        // Page 04h capability: SUPPORT_GRID bit7 (75GHz), low ch -100, high ch 100,
        // min power -20 dBm (-2000), max power 0 dBm (0).
        sfp = sfp.with_eeprom(
            cmis_linear(CCMIS_CFG_SUPPORT_PAGE, SUPPORT_GRID_OFFSET),
            0x80,
        );
        sfp = seed_i16_be(
            sfp,
            cmis_linear(CCMIS_CFG_SUPPORT_PAGE, LOW_CHANNEL_OFFSET),
            -100,
        );
        sfp = seed_i16_be(
            sfp,
            cmis_linear(CCMIS_CFG_SUPPORT_PAGE, HIGH_CHANNEL_OFFSET),
            100,
        );
        sfp = seed_i16_be(
            sfp,
            cmis_linear(CCMIS_CFG_SUPPORT_PAGE, MIN_PROG_POWER_OFFSET),
            -2000,
        );
        sfp = seed_i16_be(
            sfp,
            cmis_linear(CCMIS_CFG_SUPPORT_PAGE, MAX_PROG_POWER_OFFSET),
            0,
        );
        // Page 12h configured: GRID_SPACING nibble 7 (75GHz) = 0x70, channel 3, tx power -10 dBm.
        sfp = sfp.with_eeprom(cmis_linear(TUNABLE_LASER_PAGE, GRID_SPACING_OFFSET), 0x70);
        sfp = seed_i16_be(
            sfp,
            cmis_linear(TUNABLE_LASER_PAGE, LASER_CONFIG_CHANNEL_OFFSET),
            3,
        );
        sfp = seed_i16_be(
            sfp,
            cmis_linear(TUNABLE_LASER_PAGE, TX_CONFIG_POWER_OFFSET),
            -1000,
        );
        let api = BridgeCmisApi::new(Box::new(sfp));

        // get_supported_freq_config: (grid_bitmap, low_ch, hi_ch, low_freq, high_freq).
        assert_eq!(
            api.get_supported_freq_config(),
            (0x80, -100, 100, 190600, 195600)
        );
        // get_supported_power_config: (min, max) dBm after the ×100 scale.
        assert_eq!(api.get_supported_power_config(), (-20.0, 0.0));
        // get_laser_config_freq: 75GHz grid, channel 3 → 193100 + 3*25 = 193175.
        assert_eq!(api.get_laser_config_freq(), 193175);
        // get_tx_config_power: -1000/100 = -10.0 dBm.
        assert_eq!(api.get_tx_config_power(), -10.0);
    }

    #[test]
    fn bridge_unseeded_coherent_reads_default_to_zero() {
        use crate::mock::MockSfp;
        // A zeroed/unreadable tuning page decodes to the "nothing configured" defaults, so the
        // configure guards (tx_power != get_tx_config_power, freq != get_laser_config_freq)
        // still trip on first bring-up.
        let api = BridgeCmisApi::new(Box::new(MockSfp::present()));
        assert_eq!(api.get_tx_config_power(), 0.0);
        // grid code 0 → 3.125GHz, channel 0 → base frequency.
        assert_eq!(api.get_laser_config_freq(), FREQ_BASE_GHZ);
        assert_eq!(
            api.get_supported_freq_config(),
            (0, 0, 0, FREQ_BASE_GHZ, FREQ_BASE_GHZ)
        );
    }

    #[test]
    fn bridge_set_tx_power_writes_page12h_200_signed_scaled() {
        use crate::mock::MockSfp;
        let sfp = MockSfp::present();
        let writes_handle = sfp.clone();
        let api = BridgeCmisApi::new(Box::new(sfp));

        assert!(api.set_tx_power(-10.0));
        let writes = writes_handle.eeprom_writes();
        // Exactly one write: TX_CONFIG_POWER (12h:200) = round(-10.0*100) = -1000 as >h.
        assert_eq!(writes.len(), 1);
        assert_eq!(
            writes[0].0,
            cmis_linear(TUNABLE_LASER_PAGE, TX_CONFIG_POWER_OFFSET)
        );
        assert_eq!(writes[0].1, (-1000i16).to_be_bytes().to_vec());
    }

    #[test]
    fn bridge_set_laser_freq_writes_grid_spacing_and_channel() {
        use crate::mock::MockSfp;
        // Advertise the 75GHz-grid capability so the write is accepted.
        let mut sfp = MockSfp::present();
        sfp = sfp.with_eeprom(
            cmis_linear(CCMIS_CFG_SUPPORT_PAGE, SUPPORT_GRID_OFFSET),
            0x80,
        );
        sfp = seed_i16_be(
            sfp,
            cmis_linear(CCMIS_CFG_SUPPORT_PAGE, LOW_CHANNEL_OFFSET),
            -100,
        );
        sfp = seed_i16_be(
            sfp,
            cmis_linear(CCMIS_CFG_SUPPORT_PAGE, HIGH_CHANNEL_OFFSET),
            100,
        );
        let writes_handle = sfp.clone();
        let api = BridgeCmisApi::new(Box::new(sfp));

        // 193175 on the 75GHz grid → channel (193175-193100)/25 = 3 (3 % 3 == 0).
        assert!(api.set_laser_freq(193175, 75));
        let writes = writes_handle.eeprom_writes();
        assert_eq!(writes.len(), 2);
        // GRID_SPACING (12h:128) = 0x70 (nibble 7 = 75GHz), one byte, written first.
        assert_eq!(
            writes[0].0,
            cmis_linear(TUNABLE_LASER_PAGE, GRID_SPACING_OFFSET)
        );
        assert_eq!(writes[0].1, vec![GRID_75GHZ_CODE]);
        // LASER_CONFIG_CHANNEL (12h:136) = 3 as >h, written second.
        assert_eq!(
            writes[1].0,
            cmis_linear(TUNABLE_LASER_PAGE, LASER_CONFIG_CHANNEL_OFFSET)
        );
        assert_eq!(writes[1].1, 3i16.to_be_bytes().to_vec());
    }

    #[test]
    fn bridge_set_laser_freq_rejects_unsupported_grid_and_off_grid_channel() {
        use crate::mock::MockSfp;
        // 75GHz not advertised (bit7 clear) → refuse without any register write.
        let no_75 = MockSfp::present();
        let no_75_writes = no_75.clone();
        let api = BridgeCmisApi::new(Box::new(no_75));
        assert!(!api.set_laser_freq(193175, 75));
        assert!(no_75_writes.eeprom_writes().is_empty());

        // 75GHz advertised but the channel is off-grid (193125 → channel 1, 1 % 3 != 0).
        let mut sfp = MockSfp::present();
        sfp = sfp.with_eeprom(
            cmis_linear(CCMIS_CFG_SUPPORT_PAGE, SUPPORT_GRID_OFFSET),
            0x80,
        );
        sfp = seed_i16_be(
            sfp,
            cmis_linear(CCMIS_CFG_SUPPORT_PAGE, LOW_CHANNEL_OFFSET),
            -100,
        );
        sfp = seed_i16_be(
            sfp,
            cmis_linear(CCMIS_CFG_SUPPORT_PAGE, HIGH_CHANNEL_OFFSET),
            100,
        );
        let off_grid_writes = sfp.clone();
        let api2 = BridgeCmisApi::new(Box::new(sfp));
        assert!(!api2.set_laser_freq(193125, 75));
        assert!(off_grid_writes.eeprom_writes().is_empty());
    }

    /// The whole point of the direct seam: a decode read must be answered by
    /// `sfp.get_xcvr_api().<method>()` rather than by re-reading the whole module.
    /// Canned aggregates here carry DELIBERATELY WRONG values, so the assertions can
    /// only pass if the direct route was taken.
    #[test]
    fn bridge_decode_reads_prefer_the_direct_xcvr_api_seam() {
        let mut sfp = MockSfp::present();
        sfp.status = json!({
            "module_state": "ModuleLowPwr",
            "DP1State": "DataPathDeactivated",
            "config_state_hostlane1": "ConfigUndefined",
            "dpinit_pending_hostlane1": true,
        });
        sfp.info = json!({
            "type_abbrv_name": "QSFP28",
            "cmis_rev": "3.0",
            "application_advertisement": "N/A",
        });
        sfp.api_calls = [
            ("get_module_state".to_string(), json!("ModuleReady")),
            ("get_module_type_abbreviation".to_string(), json!("QSFP-DD")),
            ("get_cmis_rev".to_string(), json!("5.0")),
            ("is_coherent_module".to_string(), json!(false)),
            (
                "get_datapath_state".to_string(),
                json!({"DP1State": "DataPathActivated"}),
            ),
            (
                "get_config_datapath_hostlane_status".to_string(),
                json!({"ConfigStatusLane1": "ConfigSuccess"}),
            ),
            (
                "get_dpinit_pending".to_string(),
                json!({"DPInitPending1": false}),
            ),
            (
                "get_application_advertisement".to_string(),
                json!({"1": {"host_lane_count": 8, "media_lane_count": 4}}),
            ),
        ]
        .into_iter()
        .collect();
        let log = sfp.api_call_log.clone();
        let info_reads = sfp.info_reads.clone();
        let status_reads = sfp.status_reads.clone();

        let api = BridgeCmisApi::new(Box::new(sfp));
        assert_eq!(api.get_module_state(), "ModuleReady");
        assert_eq!(
            api.get_module_type_abbreviation().as_deref(),
            Some("QSFP-DD")
        );
        assert_eq!(api.get_cmis_rev(), "5.0");
        assert!(!api.is_coherent_module());
        assert_eq!(
            api.get_datapath_state()["DP1State"],
            json!("DataPathActivated")
        );
        assert_eq!(
            api.get_config_datapath_hostlane_status()["ConfigStatusLane1"],
            json!("ConfigSuccess")
        );
        assert_eq!(api.get_dpinit_pending()["DPInitPending1"], json!(false));
        assert_eq!(api.get_media_lane_count(1), 4);

        let seen = log.lock().unwrap().clone();
        assert_eq!(seen.len(), 8, "every decode read took the seam: {seen:?}");
        for m in [
            "get_module_state",
            "get_module_type_abbreviation",
            "get_cmis_rev",
            "is_coherent_module",
            "get_datapath_state",
            "get_config_datapath_hostlane_status",
            "get_dpinit_pending",
            "get_application_advertisement",
        ] {
            assert!(
                seen.iter().any(|s| s == m),
                "{m} did not use the seam: {seen:?}"
            );
        }
        // and none of them fell back to a whole-module re-read
        assert_eq!(info_reads.load(Ordering::SeqCst), 0);
        assert_eq!(status_reads.load(Ordering::SeqCst), 0);
    }

    /// A mock with no canned seam results (the default, and what every pre-existing test
    /// uses) must still get the aggregate-derived answers -- the seam is additive.
    #[test]
    fn bridge_decode_reads_fall_back_when_the_seam_is_unavailable() {
        let mut sfp = MockSfp::present();
        sfp.status = json!({
            "module_state": "ModuleReady",
            "DP1State": "DataPathActivated",
            "config_state_hostlane1": "ConfigSuccess",
        });
        sfp.info = json!({"type_abbrv_name": "QSFP-DD", "cmis_rev": "5.0"});
        let info_reads = sfp.info_reads.clone();

        let api = BridgeCmisApi::new(Box::new(sfp));
        assert_eq!(api.get_module_state(), "ModuleReady");
        assert_eq!(
            api.get_module_type_abbreviation().as_deref(),
            Some("QSFP-DD")
        );
        assert_eq!(api.get_cmis_rev(), "5.0");
        assert_eq!(
            api.get_datapath_state()["DP1State"],
            json!("DataPathActivated")
        );
        assert_eq!(
            api.get_config_datapath_hostlane_status()["ConfigStatusLane1"],
            json!("ConfigSuccess")
        );
        assert!(
            info_reads.load(Ordering::SeqCst) >= 1,
            "fallback did read the aggregate"
        );
    }

    /// When the fallback IS used, the aggregate must be read once per api instance (one
    /// port-sweep), not once per decode read -- and a register write must invalidate it
    /// so a read-after-write still sees the module.
    #[test]
    fn bridge_aggregate_is_memoised_per_instance_and_dropped_on_write() {
        let mut sfp = MockSfp::present();
        sfp.status = json!({"module_state": "ModuleReady", "DP1State": "DataPathActivated"});
        sfp.info = json!({"type_abbrv_name": "QSFP-DD", "cmis_rev": "5.0"});
        let info_reads = sfp.info_reads.clone();
        let status_reads = sfp.status_reads.clone();

        let api = BridgeCmisApi::new(Box::new(sfp));
        for _ in 0..5 {
            api.get_module_state();
            api.get_cmis_rev();
            api.get_module_type_abbreviation();
            api.get_datapath_state();
        }
        assert_eq!(
            status_reads.load(Ordering::SeqCst),
            1,
            "status read once, not 10x"
        );
        assert_eq!(
            info_reads.load(Ordering::SeqCst),
            1,
            "info read once, not 10x"
        );

        // a page-10h write must drop the snapshot
        api.set_datapath_deinit(0x01);
        api.get_module_state();
        api.get_cmis_rev();
        assert_eq!(
            status_reads.load(Ordering::SeqCst),
            2,
            "re-read after write"
        );
        assert_eq!(info_reads.load(Ordering::SeqCst), 2, "re-read after write");
    }
}
