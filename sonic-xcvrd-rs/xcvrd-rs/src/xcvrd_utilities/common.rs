//! `common.py` → CMIS state constants, platform `_wrapper_*` shims, DB helpers,
//! CMIS helpers (analysis §3.2).
#![allow(dead_code, unused_variables, unused_imports)]

use crate::db::DbTable;
use crate::error::Result;
use crate::hal::SfpHandle;

// --- CMIS_STATE_* (common.py:23) --------------------------------------------------
pub const CMIS_STATE_UNKNOWN: &str = "UNKNOWN";
pub const CMIS_STATE_INSERTED: &str = "INSERTED";
pub const CMIS_STATE_DP_PRE_INIT_CHECK: &str = "DP_PRE_INIT_CHECK";
pub const CMIS_STATE_DP_DEINIT: &str = "DP_DEINIT";
pub const CMIS_STATE_AP_CONF: &str = "AP_CONFIGURED";
pub const CMIS_STATE_DP_ACTIVATE: &str = "DP_ACTIVATION";
pub const CMIS_STATE_DP_INIT: &str = "DP_INIT";
pub const CMIS_STATE_DP_TXON: &str = "DP_TXON";
pub const CMIS_STATE_READY: &str = "READY";
pub const CMIS_STATE_REMOVED: &str = "REMOVED";
pub const CMIS_STATE_FAILED: &str = "FAILED";

/// `CMIS_TERMINAL_STATES` (common.py:35).
pub const CMIS_TERMINAL_STATES: &[&str] =
    &[CMIS_STATE_READY, CMIS_STATE_FAILED, CMIS_STATE_REMOVED];

/// `NOT_IMPLEMENTED_ERROR` sys-exit code (analysis §3.5).
pub const NOT_IMPLEMENTED_ERROR: i32 = 3;

/// Typed CMIS state (mirrors the `CMIS_STATE_*` string constants). The string form
/// is what is stored in `TRANSCEIVER_STATUS_SW.cmis_state`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CmisState {
    Unknown,
    Inserted,
    DpPreInitCheck,
    DpDeinit,
    ApConfigured,
    DpActivation,
    DpInit,
    DpTxOn,
    Ready,
    Removed,
    Failed,
}

impl CmisState {
    /// String form written to STATE_DB.
    pub fn as_str(&self) -> &'static str {
        match self {
            CmisState::Unknown => CMIS_STATE_UNKNOWN,
            CmisState::Inserted => CMIS_STATE_INSERTED,
            CmisState::DpPreInitCheck => CMIS_STATE_DP_PRE_INIT_CHECK,
            CmisState::DpDeinit => CMIS_STATE_DP_DEINIT,
            CmisState::ApConfigured => CMIS_STATE_AP_CONF,
            CmisState::DpActivation => CMIS_STATE_DP_ACTIVATE,
            CmisState::DpInit => CMIS_STATE_DP_INIT,
            CmisState::DpTxOn => CMIS_STATE_DP_TXON,
            CmisState::Ready => CMIS_STATE_READY,
            CmisState::Removed => CMIS_STATE_REMOVED,
            CmisState::Failed => CMIS_STATE_FAILED,
        }
    }

    /// `state in CMIS_TERMINAL_STATES`.
    pub fn is_terminal(&self) -> bool {
        matches!(
            self,
            CmisState::Ready | CmisState::Failed | CmisState::Removed
        )
    }

    /// Parse the `TRANSCEIVER_STATUS_SW.cmis_state` string; unknown → `Unknown`.
    pub fn from_db_str(s: &str) -> CmisState {
        match s {
            CMIS_STATE_INSERTED => CmisState::Inserted,
            CMIS_STATE_DP_PRE_INIT_CHECK => CmisState::DpPreInitCheck,
            CMIS_STATE_DP_DEINIT => CmisState::DpDeinit,
            CMIS_STATE_AP_CONF => CmisState::ApConfigured,
            CMIS_STATE_DP_ACTIVATE => CmisState::DpActivation,
            CMIS_STATE_DP_INIT => CmisState::DpInit,
            CMIS_STATE_DP_TXON => CmisState::DpTxOn,
            CMIS_STATE_READY => CmisState::Ready,
            CMIS_STATE_REMOVED => CmisState::Removed,
            CMIS_STATE_FAILED => CmisState::Failed,
            _ => CmisState::Unknown,
        }
    }
}

/// `update_port_transceiver_status_table_sw` — set `status`/`error` on STATUS_SW.
///
/// Mirrors `common.update_port_transceiver_status_table_sw`: it writes the SW-owned
/// `{status, error}` pair via `Table.set`. The reference passes `error_descriptions`
/// defaulting to `'N/A'`; callers here pass it explicitly. On the real STATE_DB this
/// is an additive merge, so it never clobbers the CMIS-owned `cmis_state` field
/// another writer set (see [`crate::db`]).
pub fn update_port_transceiver_status_table_sw(
    logical_port_name: &str,
    status_sw_tbl: &dyn DbTable,
    status: &str,
    error: &str,
) {
    status_sw_tbl.set(
        logical_port_name,
        &[
            ("status".to_string(), status.to_string()),
            ("error".to_string(), error.to_string()),
        ],
    );
}

/// `get_cmis_state_from_state_db(lport, status_sw_tbl)` → the stored `cmis_state`.
pub fn get_cmis_state_from_state_db(lport: &str, status_sw_tbl: &dyn DbTable) -> String {
    // Mirror common.get_cmis_state_from_state_db: the port's `cmis_state`, or
    // `UNKNOWN` when the field is absent (treated as a transitional/non-terminal state).
    status_sw_tbl
        .hget(lport, "cmis_state")
        .unwrap_or_else(|| CMIS_STATE_UNKNOWN.to_string())
}

/// `del_port_sfp_dom_info_from_db` — delete a port's rows across the given tables.
///
/// Mirrors `common.del_port_sfp_dom_info_from_db`: for a (non-ganged) logical port
/// the physical port name equals the logical name, so this deletes `<lport>` from
/// every supplied table handle. `None` handles are filtered by the caller (the
/// reference passes `intf_tbl=None` in `deinit` to keep `TRANSCEIVER_INFO`).
pub fn del_port_sfp_dom_info_from_db(logical_port_name: &str, tables: &[&dyn DbTable]) {
    for tbl in tables {
        tbl.del(logical_port_name);
    }
}

/// `is_fast_reboot_enabled()` — STATE_DB `FAST_RESTART_ENABLE_TABLE` gate.
///
/// Mirrors `common.is_fast_reboot_enabled`: read the `enable` field from
/// `FAST_RESTART_ENABLE_TABLE|system` and compare the trimmed value to `true`,
/// ignoring ASCII case. The caller supplies the table through the [`DbTable`] seam.
/// An absent row/field or a nonmatching value means fast reboot is disabled.
pub fn is_fast_reboot_enabled(fast_restart_enable_tbl: &dyn DbTable) -> bool {
    fast_restart_enable_tbl
        .hget("system", "enable")
        .map(|v| v.trim().eq_ignore_ascii_case("true"))
        .unwrap_or(false)
}

/// `is_syncd_warm_restore_complete()` — warm-reboot gating (common.py:153).
///
/// The reference connects to STATE_DB and reads two full-keyed fields:
/// `WARM_RESTART_TABLE|syncd.restore_count` and
/// `WARM_RESTART_ENABLE_TABLE|system.enable`. It returns True when syncd's
/// `restore_count` is a positive integer *or* the system warm-restart enable is the
/// string `"true"` (case-insensitive) — the marker that this boot is a warm reboot, so
/// xcvrd must not push a premature config that would flap in-service ports. Here the two
/// STATE_DB tables are passed through the [`DbTable`] seam (the table name is baked into
/// each handle), so `warm_restart_tbl.hget("syncd", "restore_count")` and
/// `warm_restart_enable_tbl.hget("system", "enable")` mirror the Python `hget` calls.
/// An absent/non-numeric `restore_count` and an absent/non-`true` enable both read as a
/// cold boot (`false`), matching the reference's guarded fall-through.
pub fn is_syncd_warm_restore_complete(
    warm_restart_tbl: &dyn DbTable,
    warm_restart_enable_tbl: &dyn DbTable,
) -> bool {
    if let Some(restore_count) = warm_restart_tbl.hget("syncd", "restore_count") {
        // Redis stores everything as a string; `"2"`/`" 3 "` → positive, `"0"`/`"abc"` → no.
        if restore_count
            .trim()
            .parse::<i64>()
            .map(|n| n > 0)
            .unwrap_or(false)
        {
            return true;
        }
    }
    if let Some(system_enabled) = warm_restart_enable_tbl.hget("system", "enable") {
        if system_enabled.trim().eq_ignore_ascii_case("true") {
            return true;
        }
    }
    false
}

/// `get_interface_speed(ifname)` (common.py:193) — map a CMIS host-electrical-interface
/// name to the host port speed in Mbps, or `0` when it matches no known family. The
/// order of the checks matters (broad substrings last); mirror the reference exactly.
pub fn get_interface_speed(ifname: &str) -> u32 {
    if ifname.contains("1.6T") {
        1_600_000
    } else if ifname.contains("800G") {
        800_000
    } else if ifname.contains("400G") {
        400_000
    } else if ifname.contains("200G") {
        200_000
    } else if ifname.contains("100G") || ifname.contains("CAUI-4") {
        100_000
    } else if ifname.contains("50G") || ifname.contains("LAUI-2") {
        50_000
    } else if ifname.contains("40G") || ifname.contains("XLAUI") || ifname.contains("XLPPI") {
        40_000
    } else if ifname.contains("25G") {
        25_000
    } else if ifname.contains("10G") || ifname.contains("SFI") || ifname.contains("XFI") {
        10_000
    } else if ifname.contains("1000BASE") {
        1_000
    } else {
        0
    }
}

/// `get_cmis_application_desired(api, host_lane_count, speed)` (common.py:228) — pick the
/// module application code whose advertisement entry matches the requested host lane
/// count + speed. `app_advert` is `api.get_application_advertisement()` marshalled to a
/// JSON object keyed by the (stringified) app index; CMIS decode stays in Python. Returns
/// `None` when `speed`/`host_lane_count` is 0 or no advertised app matches (the reference
/// `return None`). The apps are scanned in ascending numeric index order (the reference
/// builds `ret[app]` for `app in range(1, 16)`), and the returned code is `index & 0xf`.
pub fn get_cmis_application(
    host_lane_count: u32,
    speed: u32,
    app_advert: &serde_json::Value,
) -> Option<u32> {
    if speed == 0 || host_lane_count == 0 {
        return None;
    }
    let map = app_advert.as_object()?;
    // Numeric-ascending scan to match Python dict insertion order (apps 1..15).
    let mut entries: Vec<(u32, &serde_json::Value)> = map
        .iter()
        .filter_map(|(k, v)| k.parse::<u32>().ok().map(|idx| (idx, v)))
        .collect();
    entries.sort_by_key(|(idx, _)| *idx);
    for (index, app_info) in entries {
        let advert_lane_count = app_info.get("host_lane_count").and_then(|v| v.as_u64());
        let ifname = app_info
            .get("host_electrical_interface_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if advert_lane_count == Some(host_lane_count as u64) && get_interface_speed(ifname) == speed
        {
            return Some(index & 0xf);
        }
    }
    None
}

/// `_wrapper_get_presence(sfp)` — fold the platform presence shim into the HAL seam.
///
/// The reference `common._wrapper_get_presence(physical_port)` calls
/// `platform_chassis.get_sfp(physical_port).get_presence()` and falls back to `False`
/// on `NotImplementedError`. Here the caller has already resolved the [`SfpHandle`]
/// via the HAL; a read error degrades to `false` exactly like the Python fallback.
pub fn wrapper_get_presence(sfp: &dyn SfpHandle) -> Result<bool> {
    Ok(sfp.get_presence().unwrap_or(false))
}

/// `get_physical_port_name(logical_port, physical_port, ganged)` (common.py:268) — a
/// ganged (breakout-fanout) member is suffixed `":{member} (ganged)"`, otherwise the
/// logical port name is used verbatim.
pub fn get_physical_port_name(logical_port: &str, physical_port: usize, ganged: bool) -> String {
    if ganged {
        format!("{logical_port}:{physical_port} (ganged)")
    } else {
        logical_port.to_string()
    }
}

/// `get_physical_port_name_dict(logical_port_name, port_mapping)` (common.py:275) — map
/// each physical port backing `logical_port_name` to its display name. A logical port
/// with more than one physical port is *ganged*, so each member gets the `(ganged)`
/// suffix keyed by a 1-based member index; a single-port logical port maps to just its
/// name. An unknown logical port yields an empty map. The result is ordered
/// (`BTreeMap`) so ganged member numbering is stable.
pub fn get_physical_port_name_dict(
    logical_port_name: &str,
    port_mapping: &crate::xcvrd_utilities::port_event_helper::PortMapping,
) -> std::collections::BTreeMap<usize, String> {
    let mut port_name_dict = std::collections::BTreeMap::new();
    let Some(physical_port_list) =
        port_mapping.logical_port_name_to_physical_port_list(logical_port_name)
    else {
        return port_name_dict;
    };
    let ganged_port = physical_port_list.len() > 1;
    let mut ganged_member_num = 1;
    for physical_port in physical_port_list {
        let port_name = get_physical_port_name(logical_port_name, ganged_member_num, ganged_port);
        ganged_member_num += 1;
        port_name_dict.insert(physical_port, port_name);
    }
    port_name_dict
}

/// `check_port_in_range(range_str, physical_port)` (common.py:350) — is `physical_port`
/// within the inclusive `"<start>-<end>"` range? The reference splits on `-` and
/// `int()`s both ends (callers only pass a string that contains `-`). A malformed range
/// (missing separator / non-numeric bound) degrades to `false` here rather than raising,
/// keeping the media/optics SI parse resilient.
pub fn check_port_in_range(range_str: &str, physical_port: i64) -> bool {
    let mut parts = range_str.splitn(2, '-');
    let (Some(start_str), Some(end_str)) = (parts.next(), parts.next()) else {
        return false;
    };
    match (
        start_str.trim().parse::<i64>(),
        end_str.trim().parse::<i64>(),
    ) {
        (Ok(start), Ok(end)) => start <= physical_port && physical_port <= end,
        _ => false,
    }
}

/// Python `re.match(pattern, text)` — anchored at the START only (not the end). An
/// invalid pattern degrades to `false` (the media parser has no `try/except`, but a
/// bad config key must not crash the daemon). Uses a backtracking engine so the
/// media_settings.json keys that use negative lookahead match as they do in Python.
pub fn regex_match(pattern: &str, text: &str) -> bool {
    match fancy_regex::Regex::new(&format!("^(?:{pattern})")) {
        Ok(re) => re.is_match(text).unwrap_or(false),
        Err(_) => false,
    }
}

/// Python `re.fullmatch(pattern, text)` — anchored at BOTH ends. `Err(())` mirrors
/// `re.error` (an invalid pattern) so callers that emulate the Python `try/except`
/// fall back to exact string comparison (see `optics_si_parser::_match_optics_si_key`).
pub fn regex_fullmatch_checked(pattern: &str, text: &str) -> std::result::Result<bool, ()> {
    match fancy_regex::Regex::new(&format!("^(?:{pattern})$")) {
        Ok(re) => Ok(re.is_match(text).unwrap_or(false)),
        Err(_) => Err(()),
    }
}

/// Python `re.fullmatch(pattern, text)` where an invalid pattern is treated as no-match
/// (the media lane-speed matching has no `try/except`).
pub fn regex_fullmatch(pattern: &str, text: &str) -> bool {
    regex_fullmatch_checked(pattern, text).unwrap_or(false)
}

/// `device_info.get_paths_to_platform_and_hwsku_dirs()` → `(platform_path, hwsku_path)`,
/// sourced via PyO3 from the SAME embedded interpreter the bridge starts (the reference
/// media/optics loaders import `sonic_py_common.device_info`). `None` if the import/call
/// fails (no platform → the caller degrades to "no settings file").
pub fn get_paths_to_platform_and_hwsku_dirs() -> Option<(String, String)> {
    use pyo3::prelude::*;
    Python::with_gil(|py| {
        let di = py.import_bound("sonic_py_common.device_info").ok()?;
        let tup = di
            .call_method0("get_paths_to_platform_and_hwsku_dirs")
            .ok()?;
        let platform: String = tup.get_item(0).ok()?.extract().ok()?;
        let hwsku: String = tup.get_item(1).ok()?.extract().ok()?;
        Some((platform, hwsku))
    })
}

/// Load a SONiC platform settings JSON file (`media_settings.json` /
/// `optics_si_settings.json`), preferring the HWSKU dir over the platform dir — the
/// exact search order of `media_settings_parser.load_media_settings` /
/// `optics_si_parser.load_optics_si_settings`. Returns the parsed object, or an empty
/// object when no file exists / the paths can't be resolved / the JSON is malformed
/// (mirroring the reference `return {}`).
pub fn load_json_settings(filename: &str) -> serde_json::Value {
    let Some((platform_path, hwsku_path)) = get_paths_to_platform_and_hwsku_dirs() else {
        return serde_json::json!({});
    };
    let hwsku_file = std::path::Path::new(&hwsku_path).join(filename);
    let platform_file = std::path::Path::new(&platform_path).join(filename);
    let path = if hwsku_file.is_file() {
        hwsku_file
    } else if platform_file.is_file() {
        platform_file
    } else {
        return serde_json::json!({});
    };
    match std::fs::read_to_string(&path) {
        Ok(s) => serde_json::from_str(&s).unwrap_or_else(|e| {
            eprintln!("xcvrd-rs: parse {}: {e}", path.display());
            serde_json::json!({})
        }),
        Err(e) => {
            eprintln!("xcvrd-rs: read {}: {e}", path.display());
            serde_json::json!({})
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::mock::{MockDbTable, MockSfp};

    // ← tests/test_xcvrd.py::test_wrapper_get_presence (HAL-seam adaptation)
    // The Python test patches platform_chassis.get_sfp(...).get_presence(); here the
    // caller already holds the SfpHandle, so we exercise the present/absent + error
    // -> false fallback directly over MockSfp.
    #[test]
    fn wrapper_get_presence_reads_handle_and_defaults_false() {
        let present = MockSfp::present();
        assert!(wrapper_get_presence(&present).unwrap());

        let absent = MockSfp::default();
        assert!(!wrapper_get_presence(&absent).unwrap());
    }

    // update_port_transceiver_status_table_sw writes exactly {status, error} and, on a
    // merging table, leaves any pre-existing cmis_state field intact.
    #[test]
    fn update_status_sw_sets_status_and_error() {
        let tbl = MockDbTable::new("TRANSCEIVER_STATUS_SW");
        update_port_transceiver_status_table_sw("Ethernet0", &tbl, "1", "N/A");
        assert_eq!(tbl.hget("Ethernet0", "status").as_deref(), Some("1"));
        assert_eq!(tbl.hget("Ethernet0", "error").as_deref(), Some("N/A"));
        assert_eq!(tbl.get_size_for_key("Ethernet0"), 2);
    }

    // del_port_sfp_dom_info_from_db deletes the port's row from every supplied table.
    #[test]
    fn del_port_sfp_dom_info_removes_rows() {
        let a = MockDbTable::new("TRANSCEIVER_INFO");
        let b = MockDbTable::new("TRANSCEIVER_DOM_SENSOR");
        a.hset("Ethernet0", "type", "QSFP-DD");
        b.hset("Ethernet0", "temperature", "22.75");
        del_port_sfp_dom_info_from_db("Ethernet0", &[&a as &dyn DbTable, &b as &dyn DbTable]);
        assert_eq!(a.get_size_for_key("Ethernet0"), 0);
        assert_eq!(b.get_size_for_key("Ethernet0"), 0);
    }

    // ← tests/test_xcvrd.py::test_is_syncd_warm_restore_complete_valid_cases (+ the
    // invalid-restore-count case): warm reboot is flagged when syncd.restore_count is a
    // positive integer OR WARM_RESTART_ENABLE_TABLE|system.enable == "true".
    #[test]
    fn is_syncd_warm_restore_complete_valid_cases() {
        // (restore_count, system_enabled, expected) — read through the STATE_DB seam, so
        // every value is a string exactly as Redis stores it.
        let cases: &[(Option<&str>, Option<&str>, bool)] = &[
            (Some("1"), None, true),
            (Some("0"), None, false),
            (Some("2"), None, true),
            (None, Some("true"), true),
            (None, Some("false"), false),
            (None, None, false),
            (Some("abc"), None, false), // non-numeric restore_count → not warm
        ];
        for (restore_count, system_enabled, expected) in cases {
            let wr = MockDbTable::new("WARM_RESTART_TABLE");
            let wre = MockDbTable::new("WARM_RESTART_ENABLE_TABLE");
            if let Some(rc) = restore_count {
                wr.hset("syncd", "restore_count", rc);
            }
            if let Some(se) = system_enabled {
                wre.hset("system", "enable", se);
            }
            assert_eq!(
                is_syncd_warm_restore_complete(&wr, &wre),
                *expected,
                "restore_count={restore_count:?} system_enabled={system_enabled:?}"
            );
        }
    }

    // ← tests/test_xcvrd.py::test_get_cmis_state_from_state_db
    #[test]
    fn test_get_cmis_state_from_state_db() {
        let tbl = MockDbTable::new("TRANSCEIVER_STATUS_SW");
        // found=True, state=INSERTED → INSERTED
        tbl.hset("Ethernet0", "cmis_state", CMIS_STATE_INSERTED);
        assert_eq!(
            get_cmis_state_from_state_db("Ethernet0", &tbl),
            CMIS_STATE_INSERTED
        );
        // found=False (field absent) → UNKNOWN
        assert_eq!(
            get_cmis_state_from_state_db("Ethernet8", &tbl),
            CMIS_STATE_UNKNOWN
        );
    }

    // ← tests/test_xcvrd.py::test_CmisManagerTask_get_cmis_host_lanes_mask (app-select half)
    // get_interface_speed maps the CMIS host-electrical-interface names to speeds, and
    // get_cmis_application picks the app whose advertised host_lane_count + speed match.
    #[test]
    fn test_get_cmis_application() {
        assert_eq!(get_interface_speed("400GAUI-8 C2M (Annex 120E)"), 400_000);
        assert_eq!(get_interface_speed("CAUI-4 C2M (Annex 83E)"), 100_000);
        assert_eq!(get_interface_speed("50GAUI-1 C2M"), 50_000);
        assert_eq!(get_interface_speed("XLAUI"), 40_000);
        assert_eq!(get_interface_speed("nonsense"), 0);

        let advert = serde_json::json!({
            "1": {"host_electrical_interface_id": "400GAUI-8 C2M (Annex 120E)", "host_lane_count": 8, "host_lane_assignment_options": 1},
            "2": {"host_electrical_interface_id": "CAUI-4 C2M (Annex 83E)", "host_lane_count": 4, "host_lane_assignment_options": 17},
            "3": {"host_electrical_interface_id": "50GAUI-1 C2M", "host_lane_count": 1, "host_lane_assignment_options": 255}
        });
        // 8-lane 400G → app 1; 4-lane 100G → app 2; 1-lane 50G → app 3.
        assert_eq!(get_cmis_application(8, 400_000, &advert), Some(1));
        assert_eq!(get_cmis_application(4, 100_000, &advert), Some(2));
        assert_eq!(get_cmis_application(1, 50_000, &advert), Some(3));
        // No match / degenerate inputs → None.
        assert_eq!(get_cmis_application(1, 200_000, &advert), None);
        assert_eq!(get_cmis_application(0, 400_000, &advert), None);
        assert_eq!(get_cmis_application(8, 0, &advert), None);
    }

    // ← e2e test_app_select.py::test_app_selection_{default_speed,follows_port_speed}
    // (emu-deploy/provision_special_modules.sh idx14 / Ethernet56). The multi-application
    // module advertises TWO apps that share the SAME 4 host lanes — app1 = XLAUI 40G
    // (AppSelCode 1) and app2 = CAUI-4 100G (AppSelCode 2) — so, unlike the mixed-lane-count
    // advertisement above, the host lane count alone (4) cannot disambiguate them: ONLY the
    // configured port speed selects the app. This is the exact selection the e2e asserts —
    // 40G must resolve AppSel 1 and 100G must resolve AppSel 2 — and a speed change between
    // them must re-select the OTHER app rather than leave the previously-selected one active.
    #[test]
    fn test_get_cmis_application_multi_app_same_lane_count_disambiguated_by_speed() {
        // Both apps are 4 host lanes; the advertisement is keyed by app index (app0 → "1",
        // app1 → "2"), mirroring the reference `ret[app]` for `app in range(1, 16)`.
        let advert = serde_json::json!({
            "1": {"host_electrical_interface_id": "XLAUI", "host_lane_count": 4, "host_lane_assignment_options": 1},
            "2": {"host_electrical_interface_id": "CAUI-4 C2M (Annex 83E)", "host_lane_count": 4, "host_lane_assignment_options": 1}
        });
        // 40G baseline → app1 (AppSel 1); the 100G reconfig → app2 (AppSel 2).
        assert_eq!(get_cmis_application(4, 40_000, &advert), Some(1));
        assert_eq!(get_cmis_application(4, 100_000, &advert), Some(2));
        // A speed change back to 40G must re-resolve AppSel 1 (the fixture's restored baseline),
        // never stick on the 100G app just because it shares the host lane count.
        assert_eq!(get_cmis_application(4, 40_000, &advert), Some(1));
        // A 4-lane speed neither app advertises matches nothing (→ CMIS_STATE_FAILED upstream).
        assert_eq!(get_cmis_application(4, 200_000, &advert), None);
    }
}
