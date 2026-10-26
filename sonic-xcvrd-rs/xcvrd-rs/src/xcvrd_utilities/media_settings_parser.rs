//! `media_settings_parser.py` → parse `media_settings.json`, resolve the ASIC-side
//! SerDes custom SI settings for a port, and drive `notify_media_setting` — publishing
//! the SI settings to APPL_DB `PORT_TABLE` and stamping the STATE_DB
//! `PORT_TABLE.NPU_SI_SETTINGS_SYNC_STATUS` lifecycle (analysis §3.2).
//!
//! Python keeps the parsed file in a module-level global `g_dict`; here it is threaded
//! as an explicit `&Value` so the pure parse/lookup functions stay testable (the daemon
//! holds the loaded settings on the task and passes them in). [`load_media_settings`] is
//! the daemon-facing loader.
#![allow(dead_code, unused_variables, unused_imports)]

use serde_json::{json, Map, Value};

use crate::cmis::cmis_api::CmisApi;
use crate::db::DbTable;
use crate::xcvrd_utilities::common;
use crate::xcvrd_utilities::port_event_helper::PortMapping;
use crate::xcvrd_utilities::xcvr_table_helper::{
    XcvrTableHelper, NPU_SI_SETTINGS_NOTIFIED_VALUE, NPU_SI_SETTINGS_SYNC_STATUS_KEY,
};

// --- Constants (media_settings_parser.py:18) --------------------------------------
const LANE_SPEED_KEY_PREFIX: &str = "speed:";
const DEFAULT_KEY: &str = "Default";
const RANGE_SEPARATOR: char = '-';
const COMMA_SEPARATOR: char = ',';
/// `LANE_SPEED_DEFAULT_KEY` — the `speed:Default` fallback lane-speed entry.
const LANE_SPEED_DEFAULT_KEY: &str = "speed:Default";
const GLOBAL_MEDIA_SETTINGS_KEY: &str = "GLOBAL_MEDIA_SETTINGS";
const PORT_MEDIA_SETTINGS_KEY: &str = "PORT_MEDIA_SETTINGS";
const CUSTOM_MEDIA_SETTINGS_KEY: &str = "CUSTOM_MEDIA_SETTINGS";
const PHYSICAL_PORT_NOT_EXIST: i64 = -1;

const CUSTOM_SERDES_ATTR_PREFIX: &str = "CUSTOM:";
const CUSTOM_SERDES_ATTRS_TOP_LEVEL_KEY: &str = "attributes";
/// APPL_DB `PORT_TABLE` field the serialized custom SerDes attributes are published to.
pub const CUSTOM_SERDES_ATTRS_KEY_IN_DB: &str = "custom_serdes_attrs";

/// `media_settings.json` filename (loaded from the platform / HWSKU dir).
pub const MEDIA_SETTINGS_FILENAME: &str = "media_settings.json";

// =====================================================================================
// Key + small helpers
// =====================================================================================

/// The media-settings lookup key (`get_media_settings_key` result). `lane_speed_key` is
/// `None` when the port has no resolvable lane speed (the reference stores `None`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MediaSettingsKey {
    pub vendor_key: String,
    pub media_key: String,
    pub lane_speed_key: Option<String>,
    pub medium_lane_speed_key: String,
}

fn empty() -> Value {
    json!({})
}

fn is_nonempty_obj(v: &Value) -> bool {
    v.as_object().map(|o| !o.is_empty()).unwrap_or(false)
}

/// `str(value)` — Python stringification of a leaf JSON value (SI values are strings;
/// custom values can be ints; booleans render `True`/`False`).
fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Bool(b) => {
            if *b {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        Value::Null => "None".to_string(),
        other => other.to_string(),
    }
}

/// Python `natsorted(keys)` — natural sort so `lane2 < lane10`. Splits each key into
/// alternating non-digit / digit runs and compares run-wise (digits numerically).
fn natsorted_keys(obj: &Map<String, Value>) -> Vec<String> {
    let mut keys: Vec<String> = obj.keys().cloned().collect();
    keys.sort_by(|a, b| natcmp(a, b));
    keys
}

fn natcmp(a: &str, b: &str) -> std::cmp::Ordering {
    use std::cmp::Ordering;
    let (mut ai, mut bi) = (a.chars().peekable(), b.chars().peekable());
    loop {
        match (ai.peek().copied(), bi.peek().copied()) {
            (None, None) => return Ordering::Equal,
            (None, Some(_)) => return Ordering::Less,
            (Some(_), None) => return Ordering::Greater,
            (Some(ca), Some(cb)) => {
                if ca.is_ascii_digit() && cb.is_ascii_digit() {
                    let mut na = String::new();
                    while ai.peek().map(|c| c.is_ascii_digit()).unwrap_or(false) {
                        na.push(ai.next().unwrap());
                    }
                    let mut nb = String::new();
                    while bi.peek().map(|c| c.is_ascii_digit()).unwrap_or(false) {
                        nb.push(bi.next().unwrap());
                    }
                    let va = na.trim_start_matches('0');
                    let vb = nb.trim_start_matches('0');
                    let ord = va.len().cmp(&vb.len()).then_with(|| va.cmp(vb));
                    if ord != Ordering::Equal {
                        return ord;
                    }
                } else {
                    let ord = ca.cmp(&cb);
                    if ord != Ordering::Equal {
                        return ord;
                    }
                    ai.next();
                    bi.next();
                }
            }
        }
    }
}

// =====================================================================================
// Lane-speed matching (get_media_settings_for_speed / is_si_per_speed_supported)
// =====================================================================================

/// `is_si_per_speed_supported` — is the first key of `media_dict` a `speed:` entry?
/// (`LANE_SPEED_KEY_PREFIX in list(media_dict.keys())[0]`, insertion-order first key.)
pub fn is_si_per_speed_supported(media_dict: &Value) -> bool {
    media_dict
        .as_object()
        .and_then(|o| o.keys().next())
        .map(|first| first.contains(LANE_SPEED_KEY_PREFIX))
        .unwrap_or(false)
}

/// `get_media_settings_for_speed` — resolve the per-lane-speed sub-dict. If the dict is
/// not per-speed, return it as-is; else match `lane_speed_key` (regex `re.fullmatch` on
/// the suffix after `speed:`) against each candidate, falling back to `speed:Default`.
pub fn get_media_settings_for_speed(settings_dict: &Value, lane_speed_key: Option<&str>) -> Value {
    if !is_si_per_speed_supported(settings_dict) {
        return settings_dict.clone();
    }
    let Some(lane_speed_key) = lane_speed_key else {
        return empty();
    };
    let lane_speed_str = lane_speed_key
        .strip_prefix(LANE_SPEED_KEY_PREFIX)
        .unwrap_or(lane_speed_key);
    let Some(obj) = settings_dict.as_object() else {
        return empty();
    };
    for (candidate, value_dict) in obj {
        let pattern = candidate
            .strip_prefix(LANE_SPEED_KEY_PREFIX)
            .unwrap_or(candidate);
        if common::regex_fullmatch(pattern, lane_speed_str) {
            return value_dict.clone();
        }
    }
    obj.get(LANE_SPEED_DEFAULT_KEY)
        .cloned()
        .unwrap_or_else(empty)
}

/// `MediaSettingsParserBase.get_media_settings` — match `media_dict` by vendor key /
/// vendor-name / media key (first loop), else by medium-lane-speed key (second loop).
/// `None` when nothing matches (`Some({})` = matched a key but no lane-speed sub-entry).
fn get_media_settings(key: &MediaSettingsKey, media_dict: &Value) -> Option<Value> {
    let obj = media_dict.as_object()?;
    let vendor_head = key.vendor_key.split('-').next().unwrap_or(&key.vendor_key);
    let lsk = key.lane_speed_key.as_deref();
    for (dict_key, val) in obj {
        if common::regex_match(dict_key, &key.vendor_key)
            || common::regex_match(dict_key, vendor_head)
            || common::regex_match(dict_key, &key.media_key)
        {
            return Some(get_media_settings_for_speed(val, lsk));
        }
    }
    for (dict_key, val) in obj {
        if common::regex_match(dict_key, &key.medium_lane_speed_key) {
            return Some(get_media_settings_for_speed(val, lsk));
        }
    }
    None
}

// =====================================================================================
// Traditional (GLOBAL / PORT) parsers
// =====================================================================================

/// `GlobalMediaSettingsParser.parse` → `(explicit_result, default_fallback)`.
fn global_media_settings_parse(
    settings: &Value,
    physical_port: usize,
    key: &MediaSettingsKey,
) -> (Value, Value) {
    let mut default_dict = empty();
    let lsk = key.lane_speed_key.as_deref();
    let Some(settings) = settings.as_object() else {
        return (empty(), default_dict);
    };

    for (keys, entry) in settings {
        let mut media_dict = empty();
        if keys.contains(COMMA_SEPARATOR) {
            for port in keys.split(COMMA_SEPARATOR) {
                if port.contains(RANGE_SEPARATOR) {
                    if common::check_port_in_range(port, physical_port as i64) {
                        media_dict = entry.clone();
                        break;
                    }
                } else if physical_port.to_string() == port {
                    media_dict = entry.clone();
                    break;
                }
            }
        } else if keys.contains(RANGE_SEPARATOR)
            && common::check_port_in_range(keys, physical_port as i64)
        {
            media_dict = entry.clone();
        }

        if is_nonempty_obj(&media_dict) {
            if let Some(media_settings) = get_media_settings(key, &media_dict) {
                return (media_settings, empty());
            } else if let Some(def) = media_dict.get(DEFAULT_KEY) {
                default_dict = get_media_settings_for_speed(def, lsk);
            }
        }
    }

    (empty(), default_dict)
}

/// `PortMediaSettingsParser.parse` → `(explicit_result, default_fallback)`.
fn port_media_settings_parse(
    settings: &Value,
    physical_port: usize,
    key: &MediaSettingsKey,
) -> (Value, Value) {
    let lsk = key.lane_speed_key.as_deref();
    let Some(settings) = settings.as_object() else {
        return (empty(), empty());
    };

    let mut media_dict = empty();
    for (keys, entry) in settings {
        if keys.parse::<usize>().ok() == Some(physical_port) {
            media_dict = entry.clone();
            break;
        }
    }

    if media_dict.as_object().map(|o| o.is_empty()).unwrap_or(true) {
        return (empty(), empty());
    }

    if let Some(media_settings) = get_media_settings(key, &media_dict) {
        return (media_settings, empty());
    } else if let Some(def) = media_dict.get(DEFAULT_KEY) {
        return (empty(), get_media_settings_for_speed(def, lsk));
    }
    (empty(), empty())
}

/// `MediaSettingsParserBase._get_lane_values_str` — natsort + subport slice + `,`-join.
fn get_lane_values_str(val_dict: &Value, lane_count: usize, subport_num: usize) -> String {
    let Some(obj) = val_dict.as_object() else {
        return String::new();
    };
    let mut start = if subport_num != 0 {
        (subport_num - 1) * lane_count
    } else {
        0
    };
    if start + lane_count > obj.len() {
        start = 0;
    }
    let keys = natsorted_keys(obj);
    keys.iter()
        .skip(start)
        .take(lane_count)
        .map(|k| py_str(&obj[k]))
        .collect::<Vec<_>>()
        .join(",")
}

/// `MediaSettingsParserBase.to_db_value` — traditional media settings → APPL_DB
/// `(field, value)` pairs (gearbox line-side lane width for `gb_line*` keys).
fn to_db_value(
    media_dict: &Value,
    lane_count: usize,
    subport_num: usize,
    gearbox_line_lane_count: Option<usize>,
) -> Vec<(String, String)> {
    let Some(obj) = media_dict.as_object() else {
        return vec![];
    };
    let mut fvs = vec![];
    for (media_key, media_value) in obj {
        let val_str = if media_value.is_object() {
            let mut lane_count_si = lane_count;
            if let Some(gb) = gearbox_line_lane_count {
                if media_key.contains("gb_line") {
                    lane_count_si = gb;
                }
            }
            get_lane_values_str(media_value, lane_count_si, subport_num)
        } else {
            py_str(media_value)
        };
        fvs.push((media_key.clone(), val_str));
    }
    fvs
}

// =====================================================================================
// Custom media settings parser
// =====================================================================================

/// `CustomMediaSettingsParser.is_port_selected` — does `port_selector` (single / range /
/// comma list of ranges) include `physical_port`? Whitespace ignored, bad tokens skipped.
pub fn is_port_selected(port_selector: &str, physical_port: i64) -> bool {
    for token in port_selector.split(COMMA_SEPARATOR) {
        let token = token.trim();
        if token.is_empty() {
            continue;
        }
        let (start_str, end_str) = match token.split_once(RANGE_SEPARATOR) {
            Some((s, e)) => (s.trim(), e.trim()),
            None => (token, token),
        };
        match (start_str.parse::<i64>(), end_str.parse::<i64>()) {
            (Ok(start), Ok(end)) => {
                if start <= physical_port && physical_port <= end {
                    return true;
                }
            }
            _ => continue,
        }
    }
    false
}

/// `CustomMediaSettingsParser._get_lane_values` — natsort + subport slice (raw values).
fn custom_get_lane_values(val_dict: &Value, lane_count: usize, subport_num: usize) -> Vec<Value> {
    let Some(obj) = val_dict.as_object() else {
        return vec![];
    };
    let mut start = if subport_num != 0 {
        (subport_num - 1) * lane_count
    } else {
        0
    };
    if start + lane_count > obj.len() {
        start = 0;
    }
    let keys = natsorted_keys(obj);
    keys.iter()
        .skip(start)
        .take(lane_count)
        .map(|k| obj[k].clone())
        .collect()
}

/// `CustomMediaSettingsParser.to_db_value` — serialize custom SerDes attributes to the
/// compact JSON (`{"attributes":[{name:{"value":[...]}}]}`) published to APPL_DB, or
/// `None` when no `CUSTOM:` attributes are present.
pub fn custom_to_db_value(
    custom_media_dict: &Value,
    lane_count: usize,
    subport_num: usize,
) -> Option<String> {
    let obj = custom_media_dict.as_object()?;
    if obj.is_empty() {
        return None;
    }
    let mut attrs_list: Vec<Value> = vec![];
    for (key, value) in obj {
        let Some(name) = key.strip_prefix(CUSTOM_SERDES_ATTR_PREFIX) else {
            continue;
        };
        let lane_values = custom_get_lane_values(value, lane_count, subport_num);
        let mut attr = Map::new();
        let mut inner = Map::new();
        inner.insert("value".to_string(), Value::Array(lane_values));
        attr.insert(name.to_string(), Value::Object(inner));
        attrs_list.push(Value::Object(attr));
    }
    if attrs_list.is_empty() {
        return None;
    }
    let mut top = Map::new();
    top.insert(
        CUSTOM_SERDES_ATTRS_TOP_LEVEL_KEY.to_string(),
        Value::Array(attrs_list),
    );
    // serde_json's compact writer matches Python `json.dumps(..., separators=(',', ':'))`.
    Some(Value::Object(top).to_string())
}

/// `CustomMediaSettingsParser.parse` → `(explicit_result, default_fallback)`.
fn custom_media_settings_parse(
    settings: &Value,
    physical_port: usize,
    key: &MediaSettingsKey,
) -> (Value, Value) {
    let Some(settings) = settings.as_object() else {
        return (empty(), empty());
    };
    if settings.is_empty() {
        return (empty(), empty());
    }
    let mut default_dict = empty();
    let lsk = key.lane_speed_key.as_deref();
    for (port_selector, media_dict) in settings {
        if !is_port_selected(port_selector, physical_port as i64) {
            continue;
        }
        if let Some(media_settings) = get_media_settings(key, media_dict) {
            if is_nonempty_obj(&media_settings) {
                return (media_settings, empty());
            }
        }
        if !is_nonempty_obj(&default_dict) {
            if let Some(def) = media_dict.get(DEFAULT_KEY) {
                default_dict = get_media_settings_for_speed(def, lsk);
            }
        }
    }
    (empty(), default_dict)
}

// =====================================================================================
// Value resolution (precedence)
// =====================================================================================

/// `get_media_settings_value` — GLOBAL explicit → PORT explicit → PORT Default → GLOBAL
/// Default (analysis §3.2 precedence). Empty object when nothing matches.
pub fn get_media_settings_value(
    g_dict: &Value,
    physical_port: usize,
    key: &MediaSettingsKey,
) -> Value {
    let mut global_default = empty();

    if let Some(global) = g_dict.get(GLOBAL_MEDIA_SETTINGS_KEY) {
        let (result, def) = global_media_settings_parse(global, physical_port, key);
        if is_nonempty_obj(&result) {
            return result;
        }
        global_default = def;
    }

    if let Some(port) = g_dict.get(PORT_MEDIA_SETTINGS_KEY) {
        let (result, port_default) = port_media_settings_parse(port, physical_port, key);
        if is_nonempty_obj(&result) {
            return result;
        }
        if is_nonempty_obj(&port_default) {
            return port_default;
        }
    }

    if is_nonempty_obj(&global_default) {
        return global_default;
    }
    empty()
}

/// `get_custom_media_settings_value` — CUSTOM_MEDIA_SETTINGS lookup (empty if none).
pub fn get_custom_media_settings_value(
    g_dict: &Value,
    physical_port: usize,
    key: &MediaSettingsKey,
) -> Value {
    let Some(custom_settings) = g_dict.get(CUSTOM_MEDIA_SETTINGS_KEY) else {
        return empty();
    };
    if !is_nonempty_obj(custom_settings) {
        return empty();
    }
    let (result, default_dict) = custom_media_settings_parse(custom_settings, physical_port, key);
    if is_nonempty_obj(&result) {
        return result;
    }
    default_dict
}

// =====================================================================================
// Key construction
// =====================================================================================

/// Parse a Python dict *repr* string (`"{'k': 'v', ...}"`) of string→string entries,
/// as `ast.literal_eval` does for the SFF `specification_compliance` field. `None` on a
/// non-dict / malformed value (the reference `ValueError` path). Best-effort: values are
/// assumed to have no embedded quotes (true for the compliance-code strings).
fn parse_py_dict_str(s: &str) -> Option<Map<String, Value>> {
    let t = s.trim();
    if !t.starts_with('{') || !t.ends_with('}') {
        return None;
    }
    let json = t.replace('\'', "\"");
    match serde_json::from_str::<Value>(&json) {
        Ok(Value::Object(m)) => Some(m),
        _ => None,
    }
}

/// `get_lane_speed_key` — the lane-speed key, in host-electrical-interface form for CMIS
/// (`speed:<HEI first token>`) or `speed:<Gbps>G` otherwise. `api = Some` ⇒ CMIS.
pub fn get_lane_speed_key(
    port_speed: i64,
    lane_count: i64,
    api: Option<&dyn CmisApi>,
) -> Option<String> {
    if let Some(api) = api {
        let adv = api.get_application_advertisement();
        let app_id = common::get_cmis_application(lane_count as u32, port_speed as u32, &adv);
        if let Some(app_id) = app_id {
            if let Some(app) = adv.get(app_id.to_string()) {
                if let Some(hei) = app
                    .get("host_electrical_interface_id")
                    .and_then(|v| v.as_str())
                {
                    let first = hei.split_whitespace().next().unwrap_or("");
                    if !first.is_empty() {
                        return Some(format!("{LANE_SPEED_KEY_PREFIX}{first}"));
                    }
                }
            }
        }
        None
    } else if lane_count != 0 {
        Some(format!(
            "{}{}G",
            LANE_SPEED_KEY_PREFIX,
            port_speed / lane_count / 1000
        ))
    } else {
        None
    }
}

fn field_str<'a>(td: &'a Value, physical_port: usize, field: &str) -> &'a str {
    td.get(physical_port.to_string())
        .and_then(|p| p.get(field))
        .and_then(|v| v.as_str())
        .unwrap_or("")
}

fn media_len_nonzero(v: &Value) -> bool {
    match v {
        Value::Number(n) => n.as_f64() != Some(0.0),
        _ => true,
    }
}

/// `get_media_settings_key` — build the vendor/media/lane-speed lookup key from the
/// transceiver info dict. `api = Some` ⇒ CMIS (compliance code is the raw spec string);
/// `is_copper` selects `COPPER`/`OPTICAL` for the medium-lane-speed key.
pub fn get_media_settings_key(
    physical_port: usize,
    transceiver_dict: &Value,
    port_speed: i64,
    lane_count: i64,
    api: Option<&dyn CmisApi>,
    is_copper: bool,
) -> MediaSettingsKey {
    const SUP_LEN_STR: &str = "Length Cable Assembly(m)";
    const SUP_COMPLIANCE_STR: &str = "10/40G Ethernet Compliance Code";
    const EXTENDED_SPEC_COMPLIANCE_STR: &str = "Extended Specification Compliance";
    const SUP_COMPLIANCE_EXTENDED_VALUES: [&str; 2] = ["Extended", "Unknown"];

    let entry = transceiver_dict
        .get(physical_port.to_string())
        .cloned()
        .unwrap_or_else(empty);
    let vendor_name = entry
        .get("manufacturer")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let vendor_pn = entry.get("model").and_then(|v| v.as_str()).unwrap_or("");
    let vendor_key = format!("{}-{}", vendor_name.to_uppercase(), vendor_pn);

    let media_len = if entry.get("cable_type").and_then(|v| v.as_str()) == Some(SUP_LEN_STR) {
        entry
            .get("cable_length")
            .cloned()
            .unwrap_or_else(|| json!(""))
    } else {
        json!("")
    };

    let compliance_str = entry
        .get("specification_compliance")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let is_cmis = api.is_some();
    let mut media_compliance_code = String::new();
    if is_cmis {
        media_compliance_code = compliance_str.to_string();
    } else if let Some(dict) = parse_py_dict_str(compliance_str) {
        if let Some(code) = dict.get(SUP_COMPLIANCE_STR).and_then(|v| v.as_str()) {
            media_compliance_code = code.to_string();
            if SUP_COMPLIANCE_EXTENDED_VALUES.contains(&media_compliance_code.as_str()) {
                if let Some(ext) = dict
                    .get(EXTENDED_SPEC_COMPLIANCE_STR)
                    .and_then(|v| v.as_str())
                {
                    media_compliance_code = ext.to_string();
                }
            }
        }
    }

    let media_type = entry
        .get("type_abbrv_name")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let mut media_key = String::new();
    if !media_type.is_empty() {
        media_key.push_str(media_type);
    }
    if !media_compliance_code.is_empty() {
        media_key.push('-');
        media_key.push_str(&media_compliance_code);
        if is_cmis {
            if media_compliance_code == "passive_copper_media_interface"
                && media_len_nonzero(&media_len)
            {
                media_key.push_str(&format!("-{}M", py_str(&media_len)));
            }
        } else if media_len_nonzero(&media_len) {
            media_key.push_str(&format!("-{}M", py_str(&media_len)));
        }
    } else {
        media_key.push_str("-*");
    }

    let lane_speed_key = get_lane_speed_key(port_speed, lane_count, api);
    let medium = if is_copper { "COPPER" } else { "OPTICAL" };
    let speed = if lane_count != 0 {
        (port_speed / lane_count) / 1000
    } else {
        0
    };
    let medium_lane_speed_key = format!("{medium}{speed}");

    MediaSettingsKey {
        vendor_key,
        media_key,
        lane_speed_key,
        medium_lane_speed_key,
    }
}

// =====================================================================================
// notify_media_setting + NPU_SI lifecycle
// =====================================================================================

/// Per-physical-port HAL seam for [`notify_media_setting`]: presence + media-settings-key
/// resolution (the two module-level things the reference tests patch:
/// `common._wrapper_get_presence` + `get_media_settings_key`). The daemon implements this
/// over the HAL/bridge; unit tests inject a fixed presence + key.
pub trait PortMediaResolver {
    fn is_present(&self, physical_port: usize) -> bool;
    fn media_settings_key(
        &self,
        physical_port: usize,
        transceiver_dict: &Value,
        port_speed: i64,
        lane_count: i64,
    ) -> Option<MediaSettingsKey>;
}

/// `get_speed_lane_count_and_subport` — read `speed`/`lanes`/`subport` from CONFIG_DB
/// `PORT|<port>` (`(0,0,0)` when the row is missing speed/lanes).
pub fn get_speed_lane_count_and_subport(port: &str, cfg_port_tbl: &dyn DbTable) -> (i64, i64, i64) {
    let (mut port_speed, mut lane_count, mut subport_num) = (0i64, 0i64, 0i64);
    if let Some(rows) = cfg_port_tbl.get(port) {
        let map: std::collections::HashMap<String, String> = rows.into_iter().collect();
        if let (Some(speed), Some(lanes)) = (map.get("speed"), map.get("lanes")) {
            port_speed = speed.parse().unwrap_or(0);
            lane_count = lanes.split(',').count() as i64;
            subport_num = map.get("subport").and_then(|s| s.parse().ok()).unwrap_or(0);
        }
    }
    (port_speed, lane_count, subport_num)
}

/// `media_settings_present` — is there any media configuration loaded?
pub fn media_settings_present(g_dict: &Value) -> bool {
    is_nonempty_obj(g_dict)
}

/// `notify_media_setting` — resolve + publish the ASIC-side SI settings for a port to
/// APPL_DB `PORT_TABLE`, then stamp STATE_DB `PORT_TABLE.NPU_SI_SETTINGS_SYNC_STATUS =
/// NPU_SI_SETTINGS_NOTIFIED`. No-op when no settings are loaded or the port was already
/// notified (`is_npu_si_settings_update_required` gate).
pub fn notify_media_setting(
    g_dict: &Value,
    logical_port_name: &str,
    transceiver_dict: &Value,
    xcvr_table_helper: &XcvrTableHelper,
    port_mapping: &PortMapping,
    resolver: &dyn PortMediaResolver,
) {
    if !media_settings_present(g_dict) {
        return;
    }
    if !xcvr_table_helper.is_npu_si_settings_update_required(logical_port_name, port_mapping) {
        return;
    }

    let asic_index = port_mapping
        .get_asic_id_for_logical_port(logical_port_name)
        .unwrap_or(0);
    let (port_speed, lane_count, subport_num) = get_speed_lane_count_and_subport(
        logical_port_name,
        xcvr_table_helper.get_cfg_port_tbl(asic_index),
    );
    let gearbox_lanes_dict = xcvr_table_helper.get_gearbox_line_lanes_dict();

    let mut ganged_port = false;
    let mut ganged_member_num = 1usize;

    let Some(physical_port_list) =
        port_mapping.logical_port_name_to_physical_port_list(logical_port_name)
    else {
        return;
    };
    if physical_port_list.len() > 1 {
        ganged_port = true;
    }

    for physical_port in physical_port_list {
        if !resolver.is_present(physical_port) {
            continue;
        }
        if transceiver_dict.get(physical_port.to_string()).is_none() {
            continue;
        }

        let port_name =
            common::get_physical_port_name(logical_port_name, ganged_member_num, ganged_port);
        ganged_member_num += 1;

        let gearbox_line_lane_count = gearbox_lanes_dict
            .get(logical_port_name)
            .map(|v| *v as usize);
        let key_lane_count = gearbox_line_lane_count
            .map(|v| v as i64)
            .unwrap_or(lane_count);
        let Some(key) = resolver.media_settings_key(
            physical_port,
            transceiver_dict,
            port_speed,
            key_lane_count,
        ) else {
            continue;
        };

        let media_dict = get_media_settings_value(g_dict, physical_port, &key);
        let custom_media_dict = get_custom_media_settings_value(g_dict, physical_port, &key);

        if !is_nonempty_obj(&media_dict) && !is_nonempty_obj(&custom_media_dict) {
            return;
        }

        let mut fvs_list = to_db_value(
            &media_dict,
            lane_count as usize,
            subport_num as usize,
            gearbox_line_lane_count,
        );

        if let Some(custom_db_value) = custom_to_db_value(
            &custom_media_dict,
            lane_count as usize,
            subport_num as usize,
        ) {
            fvs_list.push((CUSTOM_SERDES_ATTRS_KEY_IN_DB.to_string(), custom_db_value));
        }

        if fvs_list.is_empty() {
            return;
        }

        xcvr_table_helper
            .get_app_port_tbl(asic_index)
            .set(&port_name, &fvs_list);
        xcvr_table_helper.get_state_port_tbl(asic_index).set(
            logical_port_name,
            &[(
                NPU_SI_SETTINGS_SYNC_STATUS_KEY.to_string(),
                NPU_SI_SETTINGS_NOTIFIED_VALUE.to_string(),
            )],
        );
    }
}

/// `load_media_settings()` — read `media_settings.json` from the HWSKU dir (else the
/// platform dir); empty object when no file exists.
pub fn load_media_settings() -> Value {
    common::load_json_settings(MEDIA_SETTINGS_FILENAME)
}

#[cfg(test)]
mod tests {
    // ← tests/test_xcvrd.py::test_get_media_settings_key / _value / _for_speed /
    //   is_si_per_speed_supported / notify_media_setting[_with_comma] /
    //   custom_media_settings_to_db_value / is_port_selected
    use super::*;
    use crate::cmis::cmis_api::MockCmisApi;
    use crate::xcvrd_utilities::port_event_helper::{PortChangeEvent, PortChangeEventType};
    use crate::xcvrd_utilities::xcvr_table_helper::XcvrTableHelper;

    fn media_fixture() -> Value {
        serde_json::from_str(include_str!("testdata/media_settings.json")).unwrap()
    }
    fn extended_fixture() -> Value {
        serde_json::from_str(include_str!("testdata/media_settings_extended_format.json")).unwrap()
    }

    fn xcvr_info(fields: &[(&str, &str)]) -> Value {
        let mut m = Map::new();
        for (k, v) in fields {
            m.insert(k.to_string(), json!(v));
        }
        Value::Object(m)
    }

    // ← test_get_media_settings_key (non-CMIS SFF path: good + bad compliance).
    #[test]
    fn test_get_media_settings_key() {
        let td = json!({
            "0": {
                "manufacturer": "Molex",
                "model": "1064141421",
                "cable_type": "Length Cable Assembly(m)",
                "cable_length": "255",
                "specification_compliance": "{'10/40G Ethernet Compliance Code': '10GBase-SR'}",
                "type_abbrv_name": "QSFP+"
            }
        });
        // Good specification_compliance value.
        let k = get_media_settings_key(0, &td, 100000, 2, None, true);
        assert_eq!(k.vendor_key, "MOLEX-1064141421");
        assert_eq!(k.media_key, "QSFP+-10GBase-SR-255M");
        assert_eq!(k.lane_speed_key.as_deref(), Some("speed:50G"));
        assert_eq!(k.medium_lane_speed_key, "COPPER50");

        // Bad specification_compliance value → media_key '-*'.
        let mut td2 = td.clone();
        td2["0"]["specification_compliance"] = json!("N/A");
        let k2 = get_media_settings_key(0, &td2, 100000, 2, None, true);
        assert_eq!(k2.media_key, "QSFP+-*");
        assert_eq!(k2.medium_lane_speed_key, "COPPER50");
    }

    // Extended-compliance promotion (100G modules carry the code under 'Extended ...').
    // Mirrors test_xcvrd.py::test_get_media_settings_key's QSFP28 fixture: non-CMIS,
    // 'Unknown' 10/40G code promoted to the Extended Specification Compliance string, a
    // real cable length (50.0) appended as '-50.0M', and is_copper defaulting truthy.
    #[test]
    fn get_media_settings_key_extended_compliance_optical() {
        let td = json!({
            "0": {
                "type": "QSFP28 or later",
                "type_abbrv_name": "QSFP28",
                "manufacturer": "AVAGO",
                "model": "XXX-YYY-ZZZ",
                "cable_type": "Length Cable Assembly(m)",
                "cable_length": 50.0,
                "specification_compliance": "{'10/40G Ethernet Compliance Code': 'Unknown', 'SONET Compliance Codes': 'Unknown', 'Extended Specification Compliance': '100GBASE-SR4 or 25GBASE-SR'}",
                "application_advertisement": "N/A"
            }
        });
        let k = get_media_settings_key(0, &td, 100000, 4, None, true);
        assert_eq!(k.vendor_key, "AVAGO-XXX-YYY-ZZZ");
        assert_eq!(k.media_key, "QSFP28-100GBASE-SR4 or 25GBASE-SR-50.0M");
        assert_eq!(k.lane_speed_key.as_deref(), Some("speed:25G"));
        assert_eq!(k.medium_lane_speed_key, "COPPER25");
    }

    // CMIS key: compliance is the raw spec string; lane speed from the advertisement HEI.
    #[test]
    fn get_media_settings_key_cmis() {
        let api = MockCmisApi::new();
        api.set_manufacturer("INNOLIGHT");
        api.set_model("X-DDDDD-NNN");
        api.set_application_advertisement(json!({
            "1": {"host_electrical_interface_id": "400GAUI-8 C2M (Annex 120E)", "host_lane_count": 8}
        }));
        let td = json!({
            "3": {
                "manufacturer": "innolight",
                "model": "X-DDDDD-NNN",
                "cable_type": "No separable connector",
                "specification_compliance": "sm_media_interface",
                "type_abbrv_name": "QSFP-DD"
            }
        });
        let k = get_media_settings_key(3, &td, 400000, 8, Some(&api), false);
        assert_eq!(k.vendor_key, "INNOLIGHT-X-DDDDD-NNN");
        assert_eq!(k.media_key, "QSFP-DD-sm_media_interface");
        assert_eq!(k.lane_speed_key.as_deref(), Some("speed:400GAUI-8"));
    }

    // ← test_is_si_per_speed_supported
    #[test]
    fn test_is_si_per_speed_supported() {
        let per_speed = json!({
            "speed:400G-GAUI-4": {"main": {"lane0": "0x0"}},
            "speed:400GAUI-8": {"post1": {"lane0": "0x0"}}
        });
        assert!(is_si_per_speed_supported(&per_speed));
        let flat = json!({"main": {"lane0": "0x0"}, "post1": {"lane0": "0x0"}});
        assert!(!is_si_per_speed_supported(&flat));
    }

    // ← get_media_settings_for_speed: regex lane-speed match, None key, speed:Default.
    #[test]
    fn get_media_settings_for_speed_paths() {
        let d = json!({
            "speed:200GAUI-8|100GAUI-4|25G": {"main": {"lane0": "0x1"}},
            "speed:Default": {"main": {"lane0": "0xd"}}
        });
        // regex fullmatch on the alternation.
        assert_eq!(
            get_media_settings_for_speed(&d, Some("speed:100GAUI-4"))["main"]["lane0"],
            json!("0x1")
        );
        // no match → speed:Default fallback.
        assert_eq!(
            get_media_settings_for_speed(&d, Some("speed:400G"))["main"]["lane0"],
            json!("0xd")
        );
        // None lane speed → {}.
        assert_eq!(get_media_settings_for_speed(&d, None), json!({}));
        // not per-speed → returned as-is.
        let flat = json!({"main": {"lane0": "0x7"}});
        assert_eq!(get_media_settings_for_speed(&flat, Some("speed:x")), flat);
    }

    // ← test_get_media_settings_value: representative precedence/regex/medium-lane cases.
    #[test]
    fn get_media_settings_value_cases() {
        // GLOBAL range + media key + lane speed (extended fixture, port 7).
        let g = extended_fixture();
        let key = MediaSettingsKey {
            vendor_key: "UNKOWN".into(),
            media_key: "QSFP-DD-active_cable_media_interface".into(),
            lane_speed_key: Some("speed:100GAUI-2".into()),
            medium_lane_speed_key: "UNKNOWN".into(),
        };
        let v = get_media_settings_value(&g, 7, &key);
        assert_eq!(v["main"]["lane0"], json!("0x00000020"));
        assert_eq!(v["pre1"]["lane1"], json!("0x00000002"));

        // Lane speed with no match and no speed:Default → {} (matched key, empty speed).
        let key_missing = MediaSettingsKey {
            lane_speed_key: Some("MISSING".into()),
            ..key.clone()
        };
        assert_eq!(get_media_settings_value(&g, 7, &key_missing), json!({}));

        // Vendor key prefix match (GENERIC_VENDOR matches GENERIC_VENDOR-1234).
        let mut g2 = extended_fixture();
        let sm = g2["GLOBAL_MEDIA_SETTINGS"]["0-31"]["QSFP-DD-sm_media_interface"].clone();
        g2["GLOBAL_MEDIA_SETTINGS"]["0-31"] = json!({ "GENERIC_VENDOR": sm });
        let key_vendor = MediaSettingsKey {
            vendor_key: "GENERIC_VENDOR-1234".into(),
            media_key: "UNKOWN".into(),
            lane_speed_key: Some("speed:400GAUI-8".into()),
            medium_lane_speed_key: "UNKNOWN".into(),
        };
        let v2 = get_media_settings_value(&g2, 7, &key_vendor);
        assert_eq!(v2["idriver"]["lane0"], json!("0x0000003c"));
    }

    // Medium-lane-speed regex fallback (COPPER[0-9]+ matches COPPER50; not OPTICAL50).
    #[test]
    fn get_media_settings_value_medium_lane_regex() {
        let mut g = extended_fixture();
        g["GLOBAL_MEDIA_SETTINGS"]["0-31"] = json!({
            "COPPER[0-9]+": {"idriver": {"lane0": "0x11", "lane1": "0x11", "lane2": "0x11", "lane3": "0x11"}}
        });
        let key = |mlsk: &str| MediaSettingsKey {
            vendor_key: "MISSING".into(),
            media_key: "MISSING".into(),
            lane_speed_key: Some("MISSING".into()),
            medium_lane_speed_key: mlsk.into(),
        };
        assert_eq!(
            get_media_settings_value(&g, 7, &key("COPPER50"))["idriver"]["lane0"],
            json!("0x11")
        );
        assert_eq!(
            get_media_settings_value(&g, 7, &key("OPTICAL50")),
            json!({})
        );
    }

    // ← test_custom_media_settings_to_db_value (subport slice + compact JSON, mixed types).
    #[test]
    fn test_custom_media_settings_to_db_value() {
        let md = json!({
            "CUSTOM:XYZ": {"lane0": 10, "lane1": 11, "lane2": 12, "lane3": 13},
            "CUSTOM:ABC": {"lane0": 1, "lane1": 2, "lane2": 3, "lane3": 4},
            "main": {"lane0": "0x11", "lane1": "0x12", "lane2": "0x13", "lane3": "0x14"}
        });
        assert_eq!(
            custom_to_db_value(&md, 2, 2).unwrap(),
            r#"{"attributes":[{"XYZ":{"value":[12,13]}},{"ABC":{"value":[3,4]}}]}"#
        );

        let md2 = json!({
            "CUSTOM:XYZ": {"lane0": "ADAPTIVE", "lane1": "ADAPTIVE", "lane2": "ADAPTIVE", "lane3": "ADAPTIVE"},
            "CUSTOM:ABC": {"lane0": 1, "lane1": 2, "lane2": 3, "lane3": 4}
        });
        assert_eq!(
            custom_to_db_value(&md2, 2, 2).unwrap(),
            r#"{"attributes":[{"XYZ":{"value":["ADAPTIVE","ADAPTIVE"]}},{"ABC":{"value":[3,4]}}]}"#
        );

        // No CUSTOM: attributes → None.
        let md3 =
            json!({"main": {"lane0": "0x11", "lane1": "0x12", "lane2": "0x13", "lane3": "0x14"}});
        assert_eq!(custom_to_db_value(&md3, 2, 2), None);
    }

    // ← CustomMediaSettingsParser.is_port_selected (single / range / list-of-ranges).
    #[test]
    fn test_is_port_selected() {
        assert!(is_port_selected("7", 7));
        assert!(!is_port_selected("7", 8));
        assert!(is_port_selected("1-4", 3));
        assert!(!is_port_selected("1-4", 5));
        assert!(is_port_selected("1,3-4,8", 4));
        assert!(is_port_selected("1,3-4,8", 8));
        assert!(!is_port_selected("1,3-4,8", 2));
        // malformed tokens are skipped, not fatal.
        assert!(is_port_selected("bad, 5-9", 6));
    }

    // Traditional to_db_value: natsort + subport slice + comma-join.
    #[test]
    fn to_db_value_subport_slice() {
        let md = json!({
            "main": {"lane0": "0xa", "lane1": "0xb", "lane2": "0xc", "lane3": "0xd"}
        });
        // subport 2, lane_count 2 → lanes [2,3].
        assert_eq!(
            to_db_value(&md, 2, 2, None),
            vec![("main".to_string(), "0xc,0xd".to_string())]
        );
        // subport 0 → lanes [0,1].
        assert_eq!(
            to_db_value(&md, 2, 0, None),
            vec![("main".to_string(), "0xa,0xb".to_string())]
        );
    }

    // Mock resolver for notify: fixed presence + fixed key (mirrors the Python patches of
    // common._wrapper_get_presence + get_media_settings_key).
    struct FixedResolver {
        present: bool,
        key: MediaSettingsKey,
    }
    impl PortMediaResolver for FixedResolver {
        fn is_present(&self, _physical_port: usize) -> bool {
            self.present
        }
        fn media_settings_key(
            &self,
            _physical_port: usize,
            _transceiver_dict: &Value,
            _port_speed: i64,
            _lane_count: i64,
        ) -> Option<MediaSettingsKey> {
            Some(self.key.clone())
        }
    }

    fn port_mapping_with(index: usize) -> PortMapping {
        let mut pm = PortMapping::new();
        pm.handle_port_change_event(&PortChangeEvent::new(
            "Ethernet0".to_string(),
            Some(index),
            0,
            PortChangeEventType::Add,
            "CONFIG_DB".to_string(),
            "PORT".to_string(),
        ));
        pm
    }

    // ← test_notify_media_setting_with_comma: PORT_MEDIA_SETTINGS Default fallback +
    //   subport slice → APPL_DB preemphasis, and the NPU_SI NOTIFIED stamp.
    #[test]
    fn test_notify_media_setting_with_comma() {
        let mut g = media_fixture();
        // Recreate media_settings_with_comma_dict: rename GLOBAL '1-32' → comma/range list.
        let global = g["GLOBAL_MEDIA_SETTINGS"]
            .as_object_mut()
            .unwrap()
            .remove("1-32")
            .unwrap();
        g["GLOBAL_MEDIA_SETTINGS"]["1-5,6,7-20,21-32"] = global;

        let helper = XcvrTableHelper::with_mock_tables(&["".to_string()]);
        // Seed CONFIG_DB PORT|Ethernet0 so get_speed_lane_count_and_subport → (100000,2,0).
        helper.get_cfg_port_tbl(0).set(
            "Ethernet0",
            &[
                ("speed".to_string(), "100000".to_string()),
                ("lanes".to_string(), "1,2".to_string()),
            ],
        );

        let td = json!({ "1": {"manufacturer": "Molex", "model": "1064141421"} });
        let resolver = FixedResolver {
            present: true,
            key: MediaSettingsKey {
                vendor_key: "MOLEX-1064141421".into(),
                media_key: "QSFP+-10GBase-SR-255M".into(),
                lane_speed_key: Some("speed:100GBASE-CR2".into()),
                medium_lane_speed_key: "UNKNOWN".into(),
            },
        };
        let pm = port_mapping_with(1);
        notify_media_setting(&g, "Ethernet0", &td, &helper, &pm, &resolver);

        let row = helper.get_app_port_tbl(0).get("Ethernet0").unwrap();
        let map: std::collections::HashMap<_, _> = row.into_iter().collect();
        assert_eq!(
            map.get("preemphasis").map(String::as_str),
            Some("0x164509,0x164509")
        );

        // NPU_SI lifecycle stamped to NOTIFIED.
        assert_eq!(
            helper
                .get_state_port_tbl(0)
                .hget("Ethernet0", NPU_SI_SETTINGS_SYNC_STATUS_KEY)
                .as_deref(),
            Some(NPU_SI_SETTINGS_NOTIFIED_VALUE)
        );
    }

    // Port 6 resolves to its own PORT_MEDIA_SETTINGS Default (0x124A08).
    #[test]
    fn notify_media_setting_with_comma_port6() {
        let mut g = media_fixture();
        let global = g["GLOBAL_MEDIA_SETTINGS"]
            .as_object_mut()
            .unwrap()
            .remove("1-32")
            .unwrap();
        g["GLOBAL_MEDIA_SETTINGS"]["1-5,6,7-20,21-32"] = global;

        let helper = XcvrTableHelper::with_mock_tables(&["".to_string()]);
        helper.get_cfg_port_tbl(0).set(
            "Ethernet0",
            &[
                ("speed".to_string(), "100000".to_string()),
                ("lanes".to_string(), "1,2".to_string()),
            ],
        );

        let td = json!({ "6": {"manufacturer": "Molex", "model": "1064141421"} });
        let resolver = FixedResolver {
            present: true,
            key: MediaSettingsKey {
                vendor_key: "MOLEX-1064141421".into(),
                media_key: "QSFP+-10GBase-SR-255M".into(),
                lane_speed_key: Some("speed:100GBASE-CR2".into()),
                medium_lane_speed_key: "UNKNOWN".into(),
            },
        };
        let pm = port_mapping_with(6);
        notify_media_setting(&g, "Ethernet0", &td, &helper, &pm, &resolver);

        let row = helper.get_app_port_tbl(0).get("Ethernet0").unwrap();
        let map: std::collections::HashMap<_, _> = row.into_iter().collect();
        assert_eq!(
            map.get("preemphasis").map(String::as_str),
            Some("0x124A08,0x124A08")
        );
    }

    // No settings loaded → notify is a no-op (no APPL_DB write, no NPU_SI stamp).
    #[test]
    fn notify_media_setting_no_settings_is_noop() {
        let helper = XcvrTableHelper::with_mock_tables(&["".to_string()]);
        let td = json!({ "1": {"manufacturer": "Molex", "model": "1064141421"} });
        let resolver = FixedResolver {
            present: true,
            key: MediaSettingsKey {
                vendor_key: "X".into(),
                media_key: "Y".into(),
                lane_speed_key: None,
                medium_lane_speed_key: "UNKNOWN".into(),
            },
        };
        let pm = port_mapping_with(1);
        notify_media_setting(&json!({}), "Ethernet0", &td, &helper, &pm, &resolver);
        assert!(helper.get_app_port_tbl(0).get("Ethernet0").is_none());
    }

    // Custom-only payload → APPL_DB custom_serdes_attrs (no traditional fields).
    #[test]
    fn notify_media_setting_custom_only() {
        let g = json!({
            "CUSTOM_MEDIA_SETTINGS": {
                "1": {
                    "QSFP-DD-active_cable_media_interface": {
                        "speed:100GAUI-2": {
                            "CUSTOM:XYZ": {"lane0": 10, "lane1": 11, "lane2": 12, "lane3": 13}
                        }
                    }
                }
            }
        });
        let helper = XcvrTableHelper::with_mock_tables(&["".to_string()]);
        helper.get_cfg_port_tbl(0).set(
            "Ethernet0",
            &[
                ("speed".to_string(), "100000".to_string()),
                ("lanes".to_string(), "1,2".to_string()),
                ("subport".to_string(), "1".to_string()),
            ],
        );
        let td = json!({ "1": {"manufacturer": "Molex", "model": "1064141421"} });
        let resolver = FixedResolver {
            present: true,
            key: MediaSettingsKey {
                vendor_key: "MOLEX-1064141421".into(),
                media_key: "QSFP-DD-active_cable_media_interface".into(),
                lane_speed_key: Some("speed:100GAUI-2".into()),
                medium_lane_speed_key: "UNKNOWN".into(),
            },
        };
        let pm = port_mapping_with(1);
        notify_media_setting(&g, "Ethernet0", &td, &helper, &pm, &resolver);

        let row = helper.get_app_port_tbl(0).get("Ethernet0").unwrap();
        let map: std::collections::HashMap<_, _> = row.into_iter().collect();
        assert_eq!(
            map.get(CUSTOM_SERDES_ATTRS_KEY_IN_DB).map(String::as_str),
            Some(r#"{"attributes":[{"XYZ":{"value":[10,11]}}]}"#)
        );
    }
}
