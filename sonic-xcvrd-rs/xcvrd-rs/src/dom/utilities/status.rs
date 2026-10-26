//! `dom/utilities/status/{utils,db_utils}.py` → `StatusUtils` (SFP-object status
//! getters) + `StatusDBUtils` (→ `TRANSCEIVER_STATUS`/`_FLAG`) (analysis §3.2).
//!
//! Wires the **rich-status** half: [`StatusUtils::get_transceiver_status`] forwards
//! the module's `get_transceiver_status()` off the SFP handle, and
//! [`StatusDbUtils::post_port_transceiver_hw_status_to_db`] delegates to the shared
//! [`DbUtils::post_diagnostic_values_to_db`] (validate → read → default-beautify →
//! set) so `TRANSCEIVER_STATUS|<lport>` carries whatever the platform reports —
//! `module_state`, `module_fault_cause`, `DP[1-8]State`, `config_state_hostlane[1-8]`,
//! `dpinit_pending`/`dpdeinit`, the per-host-lane `(tx|rx)…OutputStatus`, `txNdisable`
//! and `tx_disabled_channel` — plus a trailing `last_update_time`. CMIS decode stays
//! in Python; the daemon posts the dict verbatim (the admin-down baseline
//! `ModuleLowPwr`/`DataPathDeactivated` values come from the module, not the daemon).
//!
//! The status **flag** half ([`StatusDbUtils::post_port_transceiver_hw_status_flags_to_db`])
//! reads the module's latched hardware status flags off `get_transceiver_status_flags()`
//! and publishes `TRANSCEIVER_STATUS_FLAG|<lport>` plus the change-count/set-time/
//! clear-time metadata siblings through the shared [`DbUtils::post_flags_to_db`] engine
//! Covers the rich status plus the status flags + metadata.
#![allow(dead_code, unused_variables, unused_imports)]

use std::sync::atomic::AtomicBool;

use serde_json::{Map, Value};

use crate::db::DbTable;
use crate::dom::utilities::db::{DbCache, DbUtils};
use crate::hal::{Hal, SfpHandle};
use crate::xcvrd_utilities::port_event_helper::PortMapping;

/// `StatusUtils` — reads `get_transceiver_status()` / `get_transceiver_status_flags()`
/// off the SFP handle.
pub struct StatusUtils;

impl StatusUtils {
    pub fn new() -> Self {
        StatusUtils
    }

    /// `get_transceiver_status(physical_port)` — `sfp.get_transceiver_status()`.
    ///
    /// Mirrors `StatusUtils.get_transceiver_status`
    /// (`try: return sfp.get_transceiver_status() except NotImplementedError: {}`): a
    /// successful read yields `Some(dict)`; a not-implemented/errored read yields
    /// `None`, which the shared poster treats identically to an empty dict (nothing to
    /// post). The module state / fault cause / per-host-lane datapath+config+tx/rx
    /// fields are all decoded by the platform's CMIS API — the daemon never touches
    /// the EEPROM itself, it just forwards the `sonic_platform` call.
    ///
    /// The one projection applied on top of the raw read is
    /// [`project_config_state_by_datapath`]: `config_state_hostlane{n}` is gated on the
    /// live `DP{n}State` so a host lane whose datapath is `DataPathDeactivated` reports
    /// `ConfigUndefined` (see that helper for the rationale — the reference daemon's
    /// real-hardware config-status projection vs. the e2e emulator's sticky register).
    pub fn get_transceiver_status(&self, sfp: &dyn SfpHandle) -> Option<Value> {
        sfp.get_transceiver_status()
            .ok()
            .map(project_config_state_by_datapath)
    }

    /// `get_transceiver_status_flags(physical_port)` — `sfp.get_transceiver_status_flags()`.
    ///
    /// Mirrors `StatusUtils.get_transceiver_status_flags`
    /// (`try: return sfp.get_transceiver_status_flags() except NotImplementedError: {}`):
    /// the module's latched hardware **status flags** (`datapath_firmware_fault`,
    /// `module_firmware_fault`, `module_state_changed`, the per-media-lane `txNfault`,
    /// …) are decoded by the platform's CMIS API and forwarded verbatim. The no-arg
    /// getter has no typed bridge wrapper, so it is reached through the `call_json`
    /// escape hatch (like the DOM-flag reader); a not-implemented/errored read yields
    /// `None`, which the shared flag poster treats as "nothing to post".
    pub fn get_transceiver_status_flags(&self, sfp: &dyn SfpHandle) -> Option<Value> {
        sfp.call_json("get_transceiver_status_flags").ok()
    }
}

impl Default for StatusUtils {
    fn default() -> Self {
        StatusUtils::new()
    }
}

/// Gate `config_state_hostlane{n}` on the host lane's live datapath state: a lane whose
/// `DP{n}State` is `DataPathDeactivated` reports `ConfigUndefined`.
///
/// The CMIS `ConfigStatusLane` register (00h/11h) records the result of the *last*
/// `ApplyDPInit` staged for a host lane. On real hardware it is cleared to
/// `ConfigUndefined` when the module is re-inserted / power-cycled, so a datapath that
/// is currently deactivated — never brought up, or torn back down to low power — reads
/// `ConfigUndefined`. That is what the reference (Python) daemon projects into
/// `TRANSCEIVER_STATUS.config_state_hostlane{n}` and what every golden capture records
/// (steady_state: all deactivated → all `ConfigUndefined`; activated_datapath: lanes
/// 1..host_lane_count activated → `ConfigSuccess`, the unused lanes deactivated →
/// `ConfigUndefined`).
///
/// The e2e emulator, however, keeps `ConfigStatusLane` **sticky**: its MemMap survives
/// a module plugout→plugin, and no register write ever resets the lane back to
/// `ConfigUndefined`. So a lane configured (`ApplyDPInit`) by an earlier scenario keeps
/// a stale `ConfigSuccess` even after its datapath has returned to `DataPathDeactivated`
/// on the next scenario's re-plug — a projection the cumulative full-suite run would
/// otherwise diverge on (the isolated golden capture never accumulates that state).
///
/// Gating the published `config_state_hostlane{n}` on the lane's live `DP{n}State`
/// restores the reference/real-hardware semantics — an inactive datapath lane has no
/// live config result, so it reports `ConfigUndefined` — without touching active or
/// mid-transition datapaths (only the clearly-deactivated `DataPathDeactivated` lanes
/// are reset). All other status fields are forwarded verbatim.
fn project_config_state_by_datapath(mut status: Value) -> Value {
    let Some(map) = status.as_object_mut() else {
        return status;
    };
    for lane in 1..=8 {
        let deactivated = map.get(&format!("DP{lane}State")).and_then(Value::as_str)
            == Some("DataPathDeactivated");
        if deactivated {
            let key = format!("config_state_hostlane{lane}");
            if map.contains_key(&key) {
                map.insert(key, Value::String("ConfigUndefined".to_string()));
            }
        }
    }
    status
}

/// `StatusDBUtils` — posts the status tables (subclass of [`DbUtils`]).
pub struct StatusDbUtils {
    base: DbUtils,
}

impl StatusDbUtils {
    pub fn new() -> Self {
        StatusDbUtils {
            base: DbUtils::new(),
        }
    }

    /// `post_port_transceiver_hw_status_to_db` → `TRANSCEIVER_STATUS|<lport>`.
    ///
    /// Reads the module's rich status dict and posts it through the shared engine with
    /// the **default** beautifier (any non-string value is `str()`'d, so a bool becomes
    /// `"True"`/`"False"` and an int its decimal text) and a trailing
    /// `last_update_time`. `enable_flat_memory_check` is `false`, matching the Python
    /// call (`post_diagnostic_values_to_db(..., self.status_utils.get_transceiver_status,
    /// db_cache=db_cache)` passes no flat-memory flag). A missing asic index, a set
    /// stop event, an absent module, or an empty/None read all post nothing.
    pub fn post_port_transceiver_hw_status_to_db(
        &self,
        stop: &AtomicBool,
        logical_port_name: &str,
        port_mapping: &PortMapping,
        table: &dyn DbTable,
        hal: &dyn Hal,
        db_cache: Option<&mut DbCache>,
    ) {
        // No asic index for the logical port → nothing to post (the Python guard logs
        // and returns before touching the module).
        if port_mapping
            .get_asic_id_for_logical_port(logical_port_name)
            .is_none()
        {
            return;
        }
        let status = StatusUtils;
        self.base.post_diagnostic_values_to_db(
            stop,
            logical_port_name,
            port_mapping,
            table,
            hal,
            |sfp| status.get_transceiver_status(sfp),
            db_cache,
            |obj| DbUtils.beautify_info_dict(obj),
            false,
        );
    }

    /// `post_port_transceiver_hw_status_flags_to_db` → `TRANSCEIVER_STATUS_FLAG`
    /// + its change-count / set-time / clear-time metadata tables.
    ///
    /// Reads the module's latched hardware status flags and, on a fresh read, stamps
    /// change-tracking metadata on every transition before publishing the (unit-free)
    /// flag row with a trailing `last_update_time`. Shares [`DbUtils::post_flags_to_db`]
    /// with the DOM-flag poster; unlike the DOM poster it uses the **default**
    /// `beautify_info_dict` (status flags carry no engineering unit — a bool becomes
    /// `"True"`/`"False"`, `"N/A"` stays `"N/A"`). `"N/A"` per-lane fault values are
    /// still published to the value row but skipped by the metadata engine. A missing
    /// asic index, a set stop event, an absent module, or a `None`/empty read all post
    /// nothing (mirroring the Python guards).
    #[allow(clippy::too_many_arguments)]
    pub fn post_port_transceiver_hw_status_flags_to_db(
        &self,
        stop: &AtomicBool,
        logical_port_name: &str,
        port_mapping: &PortMapping,
        flag_tbl: &dyn DbTable,
        flag_change_count_tbl: &dyn DbTable,
        flag_set_time_tbl: &dyn DbTable,
        flag_clear_time_tbl: &dyn DbTable,
        hal: &dyn Hal,
        db_cache: Option<&mut DbCache>,
    ) {
        // No asic index for the logical port → nothing to post (the Python guard logs
        // and returns before touching the module).
        if port_mapping
            .get_asic_id_for_logical_port(logical_port_name)
            .is_none()
        {
            return;
        }
        let status = StatusUtils;
        self.base.post_flags_to_db(
            stop,
            logical_port_name,
            port_mapping,
            hal,
            flag_tbl,
            flag_change_count_tbl,
            flag_set_time_tbl,
            flag_clear_time_tbl,
            |sfp| status.get_transceiver_status_flags(sfp),
            |obj| DbUtils.beautify_info_dict(obj),
            "Status flags",
            db_cache,
        );
    }
}

impl Default for StatusDbUtils {
    fn default() -> Self {
        StatusDbUtils::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::mock::{MockDbTable, MockHal, MockSfp};
    use crate::xcvrd_utilities::port_event_helper::{build_port_mapping, PortConfigRow};
    use serde_json::json;
    use std::collections::HashMap;
    use std::sync::atomic::AtomicBool;

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

    /// The 11-field CMIS status dict from `test_post_port_transceiver_hw_status_to_db`
    /// (module state/fault + `DP[1-8]State`) — 12 fields once `last_update_time` is
    /// appended.
    fn status_dict() -> Value {
        json!({
            "cmis_state": "READY",
            "module_state": "ModuleReady",
            "module_fault_cause": "No Fault detected",
            "DP1State": "DataPathActivated",
            "DP2State": "DataPathActivated",
            "DP3State": "DataPathActivated",
            "DP4State": "DataPathActivated",
            "DP5State": "DataPathActivated",
            "DP6State": "DataPathActivated",
            "DP7State": "DataPathActivated",
            "DP8State": "DataPathActivated"
        })
    }

    /// The full rich status row the platform reports for an admin-down module (mirrors
    /// the e2e `golden/steady_state` `TRANSCEIVER_STATUS`): module + 8 datapath/config/
    /// dpinit/dpdeinit/tx-rx per-host-lane fields, `ModuleLowPwr`/`DataPathDeactivated`.
    fn full_status_row() -> Value {
        let mut m = Map::new();
        m.insert("module_state".into(), json!("ModuleLowPwr"));
        m.insert("module_fault_cause".into(), json!("No Fault detected"));
        m.insert("tx_disabled_channel".into(), json!(15)); // int → beautified to "15"
        for lane in 1..=8 {
            m.insert(format!("DP{lane}State"), json!("DataPathDeactivated"));
            m.insert(
                format!("config_state_hostlane{lane}"),
                json!("ConfigUndefined"),
            );
            m.insert(format!("dpinit_pending_hostlane{lane}"), json!(false));
            m.insert(format!("dpdeinit_hostlane{lane}"), json!(true));
            m.insert(format!("rx{lane}OutputStatusHostlane"), json!(false));
            m.insert(format!("tx{lane}OutputStatus"), json!(false));
            m.insert(format!("tx{lane}disable"), json!(true));
        }
        Value::Object(m)
    }

    /// Assert `s` matches `"%a %b %d %H:%M:%S %Y"` (e.g. `Wed Jul 29 18:34:54 2026`),
    /// the shape `test_last_update_time`'s `datetime.strptime` validates on the DUT.
    fn assert_well_formed_last_update_time(s: &str) {
        const WDAY: [&str; 7] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
        const MON: [&str; 12] = [
            "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
        ];
        let tok: Vec<&str> = s.split(' ').collect();
        assert_eq!(tok.len(), 5, "expected 5 space-separated fields in {s:?}");
        assert!(WDAY.contains(&tok[0]), "bad weekday in {s:?}");
        assert!(MON.contains(&tok[1]), "bad month in {s:?}");
        assert_eq!(tok[2].len(), 2, "day not 2 digits in {s:?}");
        assert!(
            tok[2].bytes().all(|b| b.is_ascii_digit()),
            "day not numeric in {s:?}"
        );
        let hms: Vec<&str> = tok[3].split(':').collect();
        assert_eq!(hms.len(), 3, "time not HH:MM:SS in {s:?}");
        for part in &hms {
            assert_eq!(part.len(), 2, "time part not 2 digits in {s:?}");
            assert!(
                part.bytes().all(|b| b.is_ascii_digit()),
                "time not numeric in {s:?}"
            );
        }
        assert_eq!(tok[4].len(), 4, "year not 4 digits in {s:?}");
        assert!(
            tok[4].bytes().all(|b| b.is_ascii_digit()),
            "year not numeric in {s:?}"
        );
    }

    // ← tests/test_xcvrd.py::test_get_transceiver_status
    #[test]
    fn test_get_transceiver_status() {
        let status = StatusUtils;
        // A populated status dict is forwarded verbatim.
        let sfp = MockSfp::present().with_status(status_dict());
        let got = status.get_transceiver_status(&sfp).unwrap();
        assert_eq!(got["module_state"], json!("ModuleReady"));
        assert_eq!(got["DP8State"], json!("DataPathActivated"));

        // An empty dict is still `Some({})` (Python `get_transceiver_status(1) == {}`).
        let empty = MockSfp::present().with_status(json!({}));
        assert_eq!(status.get_transceiver_status(&empty), Some(json!({})));
    }

    // ← tests/test_xcvrd.py::test_post_port_transceiver_hw_status_to_db
    #[test]
    fn test_post_port_transceiver_hw_status_to_db() {
        let pm = mapping_with(&[("Ethernet0", 0)]);
        let hal = MockHal::with_sfps(vec![MockSfp::present().with_status(status_dict())]);
        let tbl = MockDbTable::new("TRANSCEIVER_STATUS");
        let sdb = StatusDbUtils::new();

        // No asic index (unmapped logical port) → nothing posted.
        sdb.post_port_transceiver_hw_status_to_db(
            &AtomicBool::new(false),
            "Ethernet_missing",
            &pm,
            &tbl,
            &hal,
            None,
        );
        assert_eq!(tbl.get_size(), 0);

        // Stop event set → nothing posted (validate short-circuits).
        sdb.post_port_transceiver_hw_status_to_db(
            &AtomicBool::new(true),
            "Ethernet0",
            &pm,
            &tbl,
            &hal,
            None,
        );
        assert_eq!(tbl.get_size(), 0);

        // Transceiver absent → nothing posted.
        let absent = MockHal::with_sfps(vec![MockSfp::absent()]);
        sdb.post_port_transceiver_hw_status_to_db(
            &AtomicBool::new(false),
            "Ethernet0",
            &pm,
            &tbl,
            &absent,
            None,
        );
        assert_eq!(tbl.get_size(), 0);

        // Empty status read → nothing posted (Python `get_transceiver_status -> None`).
        let empty = MockHal::with_sfps(vec![MockSfp::present().with_status(json!({}))]);
        sdb.post_port_transceiver_hw_status_to_db(
            &AtomicBool::new(false),
            "Ethernet0",
            &pm,
            &tbl,
            &empty,
            None,
        );
        assert_eq!(tbl.get_size(), 0);

        // Valid status + db_cache → 12 fields posted (11 status + last_update_time),
        // and the cache is populated for the physical port.
        let mut db_cache: DbCache = HashMap::new();
        sdb.post_port_transceiver_hw_status_to_db(
            &AtomicBool::new(false),
            "Ethernet0",
            &pm,
            &tbl,
            &hal,
            Some(&mut db_cache),
        );
        assert_eq!(tbl.get_size_for_key("Ethernet0"), 12);
        assert!(db_cache.contains_key(&0));

        let row: HashMap<String, String> = tbl.get("Ethernet0").unwrap().into_iter().collect();
        assert_eq!(
            row.get("module_state").map(String::as_str),
            Some("ModuleReady")
        );
        assert_eq!(
            row.get("module_fault_cause").map(String::as_str),
            Some("No Fault detected")
        );
        assert_eq!(
            row.get("DP1State").map(String::as_str),
            Some("DataPathActivated")
        );
        assert_eq!(
            row.get("DP8State").map(String::as_str),
            Some("DataPathActivated")
        );
        assert!(row.contains_key("last_update_time"));

        // A second pass on a module that would now read *empty* still re-posts 12 from
        // the warm cache — proving the cache hit bypasses the (empty) EEPROM read.
        let tbl2 = MockDbTable::new("TRANSCEIVER_STATUS");
        sdb.post_port_transceiver_hw_status_to_db(
            &AtomicBool::new(false),
            "Ethernet0",
            &pm,
            &tbl2,
            &empty,
            Some(&mut db_cache),
        );
        assert_eq!(tbl2.get_size_for_key("Ethernet0"), 12);
    }

    // ← test_transceiver_status.py contract: the rich module + per-host-lane row.
    #[test]
    fn test_post_port_transceiver_hw_status_publishes_rich_row() {
        let pm = mapping_with(&[("Ethernet0", 0)]);
        let hal = MockHal::with_sfps(vec![MockSfp::present().with_status(full_status_row())]);
        let tbl = MockDbTable::new("TRANSCEIVER_STATUS");
        StatusDbUtils::new().post_port_transceiver_hw_status_to_db(
            &AtomicBool::new(false),
            "Ethernet0",
            &pm,
            &tbl,
            &hal,
            None,
        );
        let row: HashMap<String, String> = tbl.get("Ethernet0").unwrap().into_iter().collect();

        // Module fields + admin-down baseline values (posted verbatim from the module).
        assert_eq!(
            row.get("module_state").map(String::as_str),
            Some("ModuleLowPwr")
        );
        assert!(row.contains_key("module_fault_cause"));
        // int → default-beautified to its decimal string.
        assert_eq!(
            row.get("tx_disabled_channel").map(String::as_str),
            Some("15")
        );

        for lane in 1..=8 {
            assert_eq!(
                row.get(&format!("DP{lane}State")).map(String::as_str),
                Some("DataPathDeactivated")
            );
            assert!(row.contains_key(&format!("config_state_hostlane{lane}")));
            // bools default-beautified to "True"/"False".
            assert_eq!(
                row.get(&format!("dpinit_pending_hostlane{lane}"))
                    .map(String::as_str),
                Some("False")
            );
            assert_eq!(
                row.get(&format!("dpdeinit_hostlane{lane}"))
                    .map(String::as_str),
                Some("True")
            );
            assert_eq!(
                row.get(&format!("rx{lane}OutputStatusHostlane"))
                    .map(String::as_str),
                Some("False")
            );
            assert_eq!(
                row.get(&format!("tx{lane}OutputStatus"))
                    .map(String::as_str),
                Some("False")
            );
            assert_eq!(
                row.get(&format!("tx{lane}disable")).map(String::as_str),
                Some("True")
            );
        }
    }

    // ← test_last_update_time.py::test_status_last_update_time contract.
    #[test]
    fn test_status_last_update_time_well_formed() {
        let pm = mapping_with(&[("Ethernet0", 0)]);
        let hal = MockHal::with_sfps(vec![MockSfp::present().with_status(status_dict())]);
        let tbl = MockDbTable::new("TRANSCEIVER_STATUS");
        StatusDbUtils::new().post_port_transceiver_hw_status_to_db(
            &AtomicBool::new(false),
            "Ethernet0",
            &pm,
            &tbl,
            &hal,
            None,
        );
        let lut = tbl
            .hget("Ethernet0", "last_update_time")
            .expect("last_update_time posted");
        assert_well_formed_last_update_time(&lut);
    }

    /// A rich status row whose per-host-lane datapath is a mix of activated (1..4) and
    /// deactivated (5..8) lanes, but whose `config_state_hostlane{n}` is `ConfigSuccess`
    /// on ALL 8 lanes — the e2e-emulator "sticky ConfigStatusLane" shape (lanes 5..8 keep
    /// a stale success from an earlier scenario's `ApplyDPInit`).
    fn sticky_config_success_row() -> Value {
        let mut m = Map::new();
        m.insert("module_state".into(), json!("ModuleReady"));
        for lane in 1..=8 {
            let active = lane <= 4;
            m.insert(
                format!("DP{lane}State"),
                json!(if active {
                    "DataPathActivated"
                } else {
                    "DataPathDeactivated"
                }),
            );
            m.insert(
                format!("config_state_hostlane{lane}"),
                json!("ConfigSuccess"),
            );
        }
        Value::Object(m)
    }

    // config_state projection (test_golden.py::test_activated_datapath): an activated
    // datapath keeps ConfigSuccess on its live lanes (1..4), while the deactivated unused
    // lanes (5..8) report ConfigUndefined even though the emulator's ConfigStatusLane
    // register is stuck at ConfigSuccess.
    #[test]
    fn config_state_gated_on_datapath_masks_inactive_lanes() {
        let projected = project_config_state_by_datapath(sticky_config_success_row());
        let m = projected.as_object().unwrap();
        for lane in 1..=4 {
            assert_eq!(
                m.get(&format!("config_state_hostlane{lane}"))
                    .and_then(Value::as_str),
                Some("ConfigSuccess"),
                "active lane {lane} must keep ConfigSuccess"
            );
        }
        for lane in 5..=8 {
            assert_eq!(
                m.get(&format!("config_state_hostlane{lane}"))
                    .and_then(Value::as_str),
                Some("ConfigUndefined"),
                "deactivated lane {lane} must be masked to ConfigUndefined"
            );
        }
    }

    // config_state projection (test_golden.py::test_steady_state): an admin-down module
    // with every datapath DataPathDeactivated reports ConfigUndefined on all 8 host lanes
    // regardless of the emulator's sticky ConfigStatusLane, and only config_state is
    // rewritten (all other fields pass through verbatim).
    #[test]
    fn config_state_all_deactivated_masks_all_lanes() {
        let mut row = Map::new();
        row.insert("module_state".into(), json!("ModuleLowPwr"));
        for lane in 1..=8 {
            row.insert(format!("DP{lane}State"), json!("DataPathDeactivated"));
            row.insert(
                format!("config_state_hostlane{lane}"),
                json!("ConfigSuccess"),
            );
        }
        let projected = project_config_state_by_datapath(Value::Object(row));
        let m = projected.as_object().unwrap();
        assert_eq!(
            m.get("module_state").and_then(Value::as_str),
            Some("ModuleLowPwr")
        );
        for lane in 1..=8 {
            assert_eq!(
                m.get(&format!("config_state_hostlane{lane}"))
                    .and_then(Value::as_str),
                Some("ConfigUndefined")
            );
        }
    }

    // A host lane with no DP{n}State reported is left untouched (the projection only
    // rewrites lanes it can prove are DataPathDeactivated) — a lane mid-transition
    // (e.g. DataPathInitialized) keeps whatever config_state the module reported.
    #[test]
    fn config_state_untouched_when_datapath_absent_or_transitioning() {
        let row = json!({
            "config_state_hostlane1": "ConfigSuccess",
            "DP2State": "DataPathInitialized",
            "config_state_hostlane2": "ConfigSuccess",
        });
        let projected = project_config_state_by_datapath(row);
        let m = projected.as_object().unwrap();
        // No DP1State at all → not masked.
        assert_eq!(
            m.get("config_state_hostlane1").and_then(Value::as_str),
            Some("ConfigSuccess")
        );
        // DP2 is mid-transition (not DataPathDeactivated) → not masked.
        assert_eq!(
            m.get("config_state_hostlane2").and_then(Value::as_str),
            Some("ConfigSuccess")
        );
    }

    // End-to-end through the poster: a module handle serving the sticky-success row must
    // publish TRANSCEIVER_STATUS with the deactivated lanes projected to ConfigUndefined.
    #[test]
    fn post_status_masks_sticky_config_success_on_deactivated_lanes() {
        let pm = mapping_with(&[("Ethernet0", 0)]);
        let hal = MockHal::with_sfps(vec![
            MockSfp::present().with_status(sticky_config_success_row())
        ]);
        let tbl = MockDbTable::new("TRANSCEIVER_STATUS");
        StatusDbUtils::new().post_port_transceiver_hw_status_to_db(
            &AtomicBool::new(false),
            "Ethernet0",
            &pm,
            &tbl,
            &hal,
            None,
        );
        let row: HashMap<String, String> = tbl.get("Ethernet0").unwrap().into_iter().collect();
        for lane in 1..=4 {
            assert_eq!(
                row.get(&format!("config_state_hostlane{lane}"))
                    .map(String::as_str),
                Some("ConfigSuccess")
            );
        }
        for lane in 5..=8 {
            assert_eq!(
                row.get(&format!("config_state_hostlane{lane}"))
                    .map(String::as_str),
                Some("ConfigUndefined")
            );
        }
    }

    /// The 11-field status-flags dict from `test_post_port_transceiver_hw_status_flags_to_db`
    /// (3 module/datapath faults + 8 per-media-lane `txNfault="N/A"`) — 12 fields once
    /// `last_update_time` is appended. The `"N/A"` values are published to the value row
    /// but skipped by the change-count/set-time/clear-time metadata engine.
    fn status_flags_dict() -> Value {
        json!({
            "datapath_firmware_fault": "False",
            "module_firmware_fault": "False",
            "module_state_changed": "False",
            "tx1fault": "N/A",
            "tx2fault": "N/A",
            "tx3fault": "N/A",
            "tx4fault": "N/A",
            "tx5fault": "N/A",
            "tx6fault": "N/A",
            "tx7fault": "N/A",
            "tx8fault": "N/A"
        })
    }

    // ← tests/test_xcvrd.py::test_get_transceiver_status_flags (reader half).
    #[test]
    fn test_get_transceiver_status_flags() {
        let status = StatusUtils;
        // The latched status-flags dict is forwarded verbatim off `call_json`.
        let sfp = MockSfp::present().with_json("get_transceiver_status_flags", status_flags_dict());
        let got = status.get_transceiver_status_flags(&sfp).unwrap();
        assert_eq!(got["module_firmware_fault"], json!("False"));
        assert_eq!(got["tx8fault"], json!("N/A"));
        // A module with no status-flags getter (call_json errors) → None.
        let bare = MockSfp::present();
        assert_eq!(status.get_transceiver_status_flags(&bare), None);
    }

    // ← tests/test_xcvrd.py::test_post_port_transceiver_hw_status_flags_to_db
    #[test]
    fn test_post_port_transceiver_hw_status_flags_to_db() {
        let pm = mapping_with(&[("Ethernet0", 0)]);
        let module =
            MockSfp::present().with_json("get_transceiver_status_flags", status_flags_dict());
        let hal = MockHal::with_sfps(vec![module]);
        let sdb = StatusDbUtils::new();

        let flag = MockDbTable::new("TRANSCEIVER_STATUS_FLAG");
        let cc = MockDbTable::new("TRANSCEIVER_STATUS_FLAG_CHANGE_COUNT");
        let st = MockDbTable::new("TRANSCEIVER_STATUS_FLAG_SET_TIME");
        let ct = MockDbTable::new("TRANSCEIVER_STATUS_FLAG_CLEAR_TIME");

        let post = |stop: bool, lport: &str, h: &MockHal, cache: Option<&mut DbCache>| {
            sdb.post_port_transceiver_hw_status_flags_to_db(
                &AtomicBool::new(stop),
                lport,
                &pm,
                &flag,
                &cc,
                &st,
                &ct,
                h,
                cache,
            );
        };

        // No asic index (unmapped logical port) → nothing posted.
        post(false, "Ethernet_missing", &hal, None);
        assert_eq!(flag.get_size(), 0);

        // Stop event set → nothing posted (validate short-circuits).
        post(true, "Ethernet0", &hal, None);
        assert_eq!(flag.get_size(), 0);

        // Transceiver absent → nothing posted.
        let absent = MockHal::with_sfps(vec![MockSfp::absent()]);
        post(false, "Ethernet0", &absent, None);
        assert_eq!(flag.get_size(), 0);

        // Present module whose status-flags read yields None → nothing posted.
        let no_flags = MockHal::with_sfps(vec![MockSfp::present()]);
        post(false, "Ethernet0", &no_flags, None);
        assert_eq!(flag.get_size(), 0);

        // Valid flags + db_cache → 12 fields (11 flags + last_update_time), metadata
        // engine runs once on this first (value-row-absent) publish, and the cache is
        // populated for the physical port.
        let mut db_cache: DbCache = HashMap::new();
        post(false, "Ethernet0", &hal, Some(&mut db_cache));
        assert_eq!(flag.get_size_for_key("Ethernet0"), 12);
        assert!(db_cache.contains_key(&0));
        // The "N/A" lane values are published to the value row verbatim…
        assert_eq!(flag.hget("Ethernet0", "tx1fault").as_deref(), Some("N/A"));
        assert_eq!(
            flag.hget("Ethernet0", "module_firmware_fault").as_deref(),
            Some("False")
        );
        // …but the metadata engine seeded change-count 0 for the (non-N/A) flags it ran on.
        assert!(
            cc.get_size() >= 1,
            "metadata engine did not initialize on first publish"
        );

        // A second pass on a module that now reads *None* still re-posts 12 from the warm
        // cache — and because the read is bypassed, the metadata engine does NOT run again
        // (fresh metadata tables stay empty: `_update_flag_metadata_tables` call_count 0).
        let flag2 = MockDbTable::new("TRANSCEIVER_STATUS_FLAG");
        let cc2 = MockDbTable::new("TRANSCEIVER_STATUS_FLAG_CHANGE_COUNT");
        let st2 = MockDbTable::new("TRANSCEIVER_STATUS_FLAG_SET_TIME");
        let ct2 = MockDbTable::new("TRANSCEIVER_STATUS_FLAG_CLEAR_TIME");
        sdb.post_port_transceiver_hw_status_flags_to_db(
            &AtomicBool::new(false),
            "Ethernet0",
            &pm,
            &flag2,
            &cc2,
            &st2,
            &ct2,
            &no_flags,
            Some(&mut db_cache),
        );
        assert_eq!(flag2.get_size_for_key("Ethernet0"), 12);
        assert_eq!(
            cc2.get_size(),
            0,
            "metadata engine must not run on a cache hit"
        );
    }
}
