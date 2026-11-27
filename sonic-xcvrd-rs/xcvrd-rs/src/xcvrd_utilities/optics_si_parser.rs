//! `optics_si_parser.py` → parse `optics_si_settings.json` and apply per-vendor
//! optics Signal-Integrity settings during CMIS bring-up (analysis §3.2).
//! Handles the media/optics SI settings parsing.
//!
//! The Python module keeps the parsed file in a module-level global
//! `g_optics_si_dict`. Here the parsed `serde_json::Value` is threaded as an explicit
//! `&Value` parameter so the parse functions stay pure/testable (the daemon holds the
//! loaded settings on [`crate::cmis::cmis_manager_task::CmisManagerTask`] and passes
//! them in); [`load_optics_si_settings`] is the daemon-facing loader.
#![allow(dead_code, unused_variables, unused_imports)]

use serde_json::{json, Value};

use crate::cmis::cmis_api::CmisApi;
use crate::hal::SfpHandle;
use crate::xcvrd_utilities::common;

const GLOBAL_MEDIA_SETTINGS_KEY: &str = "GLOBAL_MEDIA_SETTINGS";
const PORT_MEDIA_SETTINGS_KEY: &str = "PORT_MEDIA_SETTINGS";
const DEFAULT_KEY: &str = "Default";
const RANGE_SEPARATOR: char = '-';
const COMMA_SEPARATOR: char = ',';

/// `optics_si_settings.json` filename (loaded from the platform / HWSKU dir).
pub const OPTICS_SI_SETTINGS_FILENAME: &str = "optics_si_settings.json";

/// An empty JSON object — the "no settings" result all the lookups converge on.
fn empty() -> Value {
    json!({})
}

/// `Value` truthiness for a dict (Python `if some_dict:`): a non-empty object.
fn is_nonempty_obj(v: &Value) -> bool {
    v.as_object().map(|o| !o.is_empty()).unwrap_or(false)
}

/// `_match_optics_si_key(dict_key, key, vendor_name_str)` — match a settings key against
/// the module vendor key (full string), the vendor name (before `-`), or the vendor name
/// string, using `re.fullmatch`. On an invalid regex the reference falls back to exact
/// string comparison (`re.error` handler).
pub fn match_optics_si_key(dict_key: &str, key: &str, vendor_name_str: &str) -> bool {
    let key_head = key.split('-').next().unwrap_or(key);
    match (
        common::regex_fullmatch_checked(dict_key, key),
        common::regex_fullmatch_checked(dict_key, key_head),
        common::regex_fullmatch_checked(dict_key, vendor_name_str),
    ) {
        (Ok(a), Ok(b), Ok(c)) => a || b || c,
        // re.error on any of the three → exact string comparison fallback.
        _ => dict_key == key || dict_key == key_head || dict_key == vendor_name_str,
    }
}

fn speed_key(lane_speed: u32) -> String {
    format!("{lane_speed}G_SPEED")
}

/// `_get_global_media_settings` → `(explicit_match, default_dict)` from
/// `GLOBAL_MEDIA_SETTINGS`. `explicit_match` is `Some` only on a vendor/name regex hit.
fn get_global_media_settings(
    g_optics_si_dict: &Value,
    physical_port: i64,
    lane_speed: u32,
    key: &str,
    vendor_name_str: &str,
) -> (Option<Value>, Value) {
    let sp = speed_key(lane_speed);
    let mut default_dict = empty();
    let mut optics_si_dict = empty();

    let Some(global) = g_optics_si_dict
        .get(GLOBAL_MEDIA_SETTINGS_KEY)
        .and_then(|v| v.as_object())
    else {
        return (None, default_dict);
    };

    for (keys, val) in global {
        if keys.contains(COMMA_SEPARATOR) {
            for port in keys.split(COMMA_SEPARATOR) {
                if port.contains(RANGE_SEPARATOR) {
                    if common::check_port_in_range(port, physical_port) {
                        optics_si_dict = val.clone();
                        break;
                    }
                } else if physical_port.to_string() == port {
                    optics_si_dict = val.clone();
                    break;
                }
            }
        } else if keys.contains(RANGE_SEPARATOR) && common::check_port_in_range(keys, physical_port)
        {
            optics_si_dict = val.clone();
        }

        if let Some(speed_dict) = optics_si_dict.get(&sp).and_then(|v| v.as_object()) {
            for (dict_key, entry) in speed_dict {
                if match_optics_si_key(dict_key, key, vendor_name_str) {
                    return (Some(entry.clone()), default_dict);
                }
            }
            if let Some(def) = speed_dict.get(DEFAULT_KEY) {
                default_dict = def.clone();
            }
        }
    }

    (None, default_dict)
}

/// `_get_port_media_settings` → the port-specific SI settings (or `default_dict`).
fn get_port_media_settings(
    g_optics_si_dict: &Value,
    physical_port: i64,
    lane_speed: u32,
    key: &str,
    vendor_name_str: &str,
    default_dict: &Value,
) -> Value {
    let sp = speed_key(lane_speed);

    let Some(port_settings) = g_optics_si_dict
        .get(PORT_MEDIA_SETTINGS_KEY)
        .and_then(|v| v.as_object())
    else {
        return default_dict.clone();
    };

    let mut optics_si_dict = empty();
    for (keys, val) in port_settings {
        if keys.parse::<i64>().ok() == Some(physical_port) {
            optics_si_dict = val.clone();
            break;
        }
    }

    if optics_si_dict
        .as_object()
        .map(|o| o.is_empty())
        .unwrap_or(true)
    {
        if is_nonempty_obj(default_dict) {
            return default_dict.clone();
        }
        return empty();
    }

    if let Some(speed_dict) = optics_si_dict.get(&sp).and_then(|v| v.as_object()) {
        for (dict_key, entry) in speed_dict {
            if match_optics_si_key(dict_key, key, vendor_name_str) {
                return entry.clone();
            }
        }
        if let Some(def) = speed_dict.get(DEFAULT_KEY) {
            return def.clone();
        } else if is_nonempty_obj(default_dict) {
            return default_dict.clone();
        }
    }

    default_dict.clone()
}

/// `get_optics_si_settings_value` — GLOBAL explicit match, else port-specific (with the
/// GLOBAL `Default` carried through as the fallback).
pub fn get_optics_si_settings_value(
    g_optics_si_dict: &Value,
    physical_port: i64,
    lane_speed: u32,
    key: &str,
    vendor_name_str: &str,
) -> Value {
    let (global_settings, default_dict) = get_global_media_settings(
        g_optics_si_dict,
        physical_port,
        lane_speed,
        key,
        vendor_name_str,
    );
    if let Some(settings) = global_settings {
        return settings;
    }
    get_port_media_settings(
        g_optics_si_dict,
        physical_port,
        lane_speed,
        key,
        vendor_name_str,
        &default_dict,
    )
}

/// `get_module_vendor_key(physical_port, sfp)` → `(vendor_key, vendor_name)` from the
/// CMIS api (`get_manufacturer()`/`get_model()`), both upper-cased + trimmed. `None`
/// when either identity is empty (the reference returns `None` when the api or vendor
/// name / part-number is `None`).
pub fn get_module_vendor_key(api: &dyn CmisApi) -> Option<(String, String)> {
    let vendor_name = api.get_manufacturer();
    if vendor_name.is_empty() {
        return None;
    }
    let vendor_pn = api.get_model();
    if vendor_pn.is_empty() {
        return None;
    }
    let vn = vendor_name.to_uppercase().trim().to_string();
    let pn = vendor_pn.to_uppercase().trim().to_string();
    Some((format!("{vn}-{pn}"), vn))
}

/// `fetch_optics_si_setting(physical_port, lane_speed, sfp)` — resolve the per-vendor
/// optics SI settings for a present module, or an empty object when there is nothing to
/// apply (no file, module absent, or no vendor key).
pub fn fetch_optics_si_setting(
    g_optics_si_dict: &Value,
    physical_port: i64,
    lane_speed: u32,
    sfp: &dyn SfpHandle,
    api: &dyn CmisApi,
) -> Value {
    if !optics_si_present(g_optics_si_dict) {
        return empty();
    }
    if !common::wrapper_get_presence(sfp).unwrap_or(false) {
        return empty();
    }
    let Some((vendor_key, vendor_name)) = get_module_vendor_key(api) else {
        return empty();
    };
    get_optics_si_settings_value(
        g_optics_si_dict,
        physical_port,
        lane_speed,
        &vendor_key,
        &vendor_name,
    )
}

/// `load_optics_si_settings()` — read `optics_si_settings.json` from the HWSKU dir (else
/// the platform dir), returning the parsed object or an empty object when no file exists.
pub fn load_optics_si_settings() -> Value {
    common::load_json_settings(OPTICS_SI_SETTINGS_FILENAME)
}

/// `optics_si_present()` — is there any optics SI configuration loaded?
pub fn optics_si_present(g_optics_si_dict: &Value) -> bool {
    is_nonempty_obj(g_optics_si_dict)
}

#[cfg(test)]
mod tests {
    // ← tests/test_xcvrd.py::TestOpticSiParser::* + test_fetch_optics_si_setting*
    use super::*;
    use crate::cmis::cmis_api::MockCmisApi;
    use crate::mock::MockSfp;

    // The unit-test optics fixture — the same `optics_si_settings.json` the Python tests
    // load (GLOBAL 0-31 / 100G_SPEED / CREDO-... vendor entries + PORT_MEDIA_SETTINGS).
    fn optics_fixture() -> Value {
        serde_json::from_str(include_str!("testdata/optics_si_settings.json")).unwrap()
    }

    fn api_with_vendor(manufacturer: &str, model: &str) -> MockCmisApi {
        let api = MockCmisApi::new();
        api.set_manufacturer(manufacturer);
        api.set_model(model);
        api
    }

    // ← TestOpticSiParser::test_match_optics_si_key_regex_error
    #[test]
    fn test_match_optics_si_key_regex_error() {
        // Invalid pattern (unclosed bracket) → re.error → exact string comparison.
        let dict_key = "[invalid regex";
        assert!(!match_optics_si_key(dict_key, "VENDOR-1234", "VENDOR"));
        // Exact string match after the regex error still returns true.
        assert!(match_optics_si_key(dict_key, dict_key, "VENDOR"));
    }

    // ← TestOpticSiParser::test_match_optics_si_key_fallback_string_match
    #[test]
    fn test_match_optics_si_key_fallback_string_match() {
        let key = "VENDOR-1234";
        assert!(match_optics_si_key(key, key, "VENDOR")); // exact key match
        assert!(match_optics_si_key("VENDOR", key, "VENDOR")); // vendor-name / split-key head match
    }

    // Regex (non-error) matching: a `VENDOR-(1234|5678)` alternation fullmatches.
    #[test]
    fn match_optics_si_key_regex_alternation() {
        assert!(match_optics_si_key(
            "CREDO-(1234|5678)",
            "CREDO-5678",
            "CREDO"
        ));
        assert!(!match_optics_si_key(
            "CREDO-(1234|5678)",
            "CREDO-9999",
            "CREDO"
        ));
        // Head match: dict_key == vendor name.
        assert!(match_optics_si_key("CREDO", "CREDO-9999", "CREDO"));
    }

    // ← TestOpticSiParser::test_get_port_media_settings_speed_key_missing
    #[test]
    fn test_get_port_media_settings_speed_key_missing() {
        let g = json!({"PORT_MEDIA_SETTINGS": {"5": {}}});
        let default_dict = json!({"default": "value"});
        let result = get_port_media_settings(&g, 5, 25, "VENDOR-1234", "VENDOR", &default_dict);
        assert_eq!(result, default_dict);
    }

    // ← TestOpticSiParser::test_get_port_media_settings_no_values_with_empty_default
    #[test]
    fn test_get_port_media_settings_no_values_with_empty_default() {
        let g = json!({"PORT_MEDIA_SETTINGS": {"5": {}}});
        let result = get_port_media_settings(&g, 5, 25, "VENDOR-1234", "VENDOR", &empty());
        assert_eq!(result, empty());
    }

    // ← TestOpticSiParser::test_get_module_vendor_key_vendor_name_none / _vendor_pn_none
    // (the Python "api is None" case is not representable — the api is passed by value).
    #[test]
    fn test_get_module_vendor_key_missing_identity() {
        // Empty manufacturer → None (vendor name missing).
        assert!(get_module_vendor_key(&api_with_vendor("", "CAC82X321HW")).is_none());
        // Empty model → None (vendor part-number missing).
        assert!(get_module_vendor_key(&api_with_vendor("VENDOR", "")).is_none());
    }

    // ← TestOpticSiParser::test_get_module_vendor_key (upper + strip on both halves).
    #[test]
    fn test_get_module_vendor_key() {
        let (vendor_key, vendor_name) =
            get_module_vendor_key(&api_with_vendor("Credo ", "CAC82X321HW")).unwrap();
        assert_eq!(vendor_key, "CREDO-CAC82X321HW");
        assert_eq!(vendor_name, "CREDO");
    }

    // ← TestOpticSiParser::test_optics_si_present_empty_dict / _with_data
    #[test]
    fn test_optics_si_present() {
        assert!(!optics_si_present(&json!({})));
        assert!(optics_si_present(&json!({"some": "data"})));
    }

    // ← test_xcvrd.py::test_fetch_optics_si_setting (present module + vendor key → no crash,
    // resolves against the real fixture) and its `_check_fetch_optics_si_setting` helper.
    #[test]
    fn test_fetch_optics_si_setting() {
        let g = optics_fixture();
        let sfp = MockSfp::present();
        // get_module_vendor_key → ('CREDO-CAC82X321M', 'CREDO'), matching the Python patch.
        let api = api_with_vendor("Credo", "CAC82X321M");
        // Must not panic and returns a JSON object.
        let out = fetch_optics_si_setting(&g, 1, 100, &sfp, &api);
        assert!(out.is_object());
    }

    // A GLOBAL vendor match resolves to the vendor's SI sub-dict.
    #[test]
    fn fetch_optics_si_setting_global_vendor_match() {
        let g = optics_fixture();
        let sfp = MockSfp::present();
        // Exact vendor key present in the fixture (GLOBAL 0-31 / 100G_SPEED).
        let api = api_with_vendor("CREDO", "CAC82X321M2MC0HW");
        let out = fetch_optics_si_setting(&g, 1, 100, &sfp, &api);
        assert!(out.get("OutputEqPreCursorTargetRx").is_some());
    }

    // ← test_xcvrd.py::test_fetch_optics_si_setting_negative — no vendor key → empty.
    #[test]
    fn test_fetch_optics_si_setting_negative() {
        let g = optics_fixture();
        let sfp = MockSfp::present();
        let api = api_with_vendor("", ""); // get_module_vendor_key → None
        let out = fetch_optics_si_setting(&g, 1, 100, &sfp, &api);
        assert!(!optics_si_present(&out));
    }

    // Absent module → empty (presence gate).
    #[test]
    fn fetch_optics_si_setting_absent_module() {
        let g = optics_fixture();
        let sfp = MockSfp::default(); // not present
        let api = api_with_vendor("CREDO", "CAC82X321M2MC0HW");
        let out = fetch_optics_si_setting(&g, 1, 100, &sfp, &api);
        assert!(!optics_si_present(&out));
    }

    // No settings loaded → fetch is a no-op empty object regardless of module state.
    #[test]
    fn fetch_optics_si_setting_no_file() {
        let sfp = MockSfp::present();
        let api = api_with_vendor("CREDO", "CAC82X321M2MC0HW");
        let out = fetch_optics_si_setting(&json!({}), 1, 100, &sfp, &api);
        assert!(!optics_si_present(&out));
    }
}
