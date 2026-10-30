//! `dom/utilities/db/utils.py` → `DBUtils`, the shared posting engine + the
//! flag-metadata engine (analysis §3.2, §1.3).
//!
//! The Python class holds `sfp_obj_dict`/`port_mapping`/`task_stopping_event` and
//! reads the module through `XCVRDUtils`. In Rust the same context is threaded
//! explicitly through the [`Hal`] seam so the posters stay `&dyn`-mockable: the
//! shared [`DbUtils::post_diagnostic_values_to_db`] validates the port, reads a
//! diagnostic dict from the module (or the per-pass `db_cache`), beautifies it,
//! appends the trailing `last_update_time`, and writes the row.
//!
//! `value_to_py_str` / `strip_unit` are kept **real** (small pure string helpers):
//! STATE_DB field text must equal Python `str(value)`, and the [`crate::hal`] tests
//! assert this against the `-inf/inf/nan` sanitizer.
#![allow(dead_code, unused_variables, unused_imports)]

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{Map, Value};

use crate::db::DbTable;
use crate::hal::{Hal, SfpHandle};
use crate::xcvrd_utilities::port_event_helper::PortMapping;
use crate::xcvrd_utilities::utils::is_transceiver_flat_memory;

/// Field/value row as posted to a STATE_DB table (order preserved).
pub type Fvs = Vec<(String, String)>;

/// Per-poll cache keyed by physical port (`db_cache` in `post_diagnostic_values_to_db`):
/// avoids re-reading the same EEPROM once per breakout subport in a single pass.
/// `None` records "read returned nothing" so a cache hit still short-circuits.
pub type DbCache = HashMap<usize, Option<Value>>;

/// `str(value)` for a STATE_DB field — the exact text the Python daemon posts. REAL:
/// the [`crate::hal`] `-inf/inf/nan` sanitizer tests and every poster depend on this.
/// (DOM values are numbers or already-formatted strings — unlike the CMIS identity
/// strings in `xcvrd::stringify`, they are not NUL-padded, so no trimming here.)
pub fn value_to_py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Bool(b) => if *b { "True" } else { "False" }.to_string(),
        Value::Number(n) => n.to_string(),
        Value::Null => "None".to_string(),
        other => other.to_string(),
    }
}

/// `_strip_unit(value, unit)` — drop a trailing unit suffix from a string value,
/// otherwise `str(value)`. REAL (pure): used by `DOMDBUtils._beautify_dom_info_dict`.
pub fn strip_unit(v: &Value, unit: &str) -> String {
    if let Value::String(s) = v {
        if let Some(stripped) = s.strip_suffix(unit) {
            return stripped.to_string();
        }
    }
    value_to_py_str(v)
}

/// `get_current_time()` — UTC `"%a %b %d %H:%M:%S %Y"`, the `last_update_time` stamp.
pub fn get_current_time() -> String {
    format_utc(SystemTime::now())
}

/// `DBUtils` — validate/read/beautify/post + flag-metadata helpers (the base every
/// DOM/status/VDM poster subclasses).
pub struct DbUtils;

impl DbUtils {
    pub const NEVER: &'static str = "never";
    pub const NOT_AVAILABLE: &'static str = "N/A";

    pub fn new() -> Self {
        DbUtils
    }

    /// `post_diagnostic_values_to_db` — the shared poster: validate the port, read a
    /// dict (or use `db_cache`), beautify, append `last_update_time`, `set`.
    ///
    /// `get_values_func` reads the diagnostic dict for the resolved module handle;
    /// `beautify_func` mutates the dict in place before it is stringified + posted.
    #[allow(clippy::too_many_arguments)]
    pub fn post_diagnostic_values_to_db<G, B>(
        &self,
        stop: &AtomicBool,
        logical_port_name: &str,
        port_mapping: &PortMapping,
        table: &dyn DbTable,
        hal: &dyn Hal,
        get_values_func: G,
        db_cache: Option<&mut DbCache>,
        beautify_func: B,
        enable_flat_memory_check: bool,
    ) where
        G: FnOnce(&dyn SfpHandle) -> Option<Value>,
        B: FnOnce(&mut Map<String, Value>),
    {
        let physical_port = match self.validate_and_get_physical_port(
            stop,
            logical_port_name,
            port_mapping,
            hal,
            enable_flat_memory_check,
        ) {
            Some(p) => p,
            None => return,
        };

        let sfp = match hal.sfp(physical_port) {
            Ok(s) => s,
            Err(_) => return,
        };

        // Read the diagnostic dict, honoring the per-pass cache (mirrors the Python
        // `db_cache` branch: a hit re-uses the value without touching the EEPROM).
        let diagnostic_values: Option<Value> = match db_cache {
            Some(cache) => {
                if let Some(cached) = cache.get(&physical_port) {
                    cached.clone()
                } else {
                    let read = get_values_func(&*sfp);
                    cache.insert(physical_port, read.clone());
                    read
                }
            }
            None => get_values_func(&*sfp),
        };

        // None / empty dict / non-object => nothing to post (matches Python's
        // `if diagnostic_values_dict is not None: if not diagnostic_values_dict: return`).
        let mut obj = match diagnostic_values {
            Some(Value::Object(o)) if !o.is_empty() => o,
            _ => return,
        };

        beautify_func(&mut obj);

        let mut fvs: Fvs = obj
            .iter()
            .map(|(k, v)| (k.clone(), value_to_py_str(v)))
            .collect();
        fvs.push(("last_update_time".to_string(), self.get_current_time()));
        table.set(logical_port_name, &fvs);
    }

    /// `_validate_and_get_physical_port` — stop not set → logical maps to a physical
    /// port → the SFP exists → the transceiver is present (→ optionally not flat
    /// memory). Returns the physical port index on success.
    pub fn validate_and_get_physical_port(
        &self,
        stop: &AtomicBool,
        logical_port_name: &str,
        port_mapping: &PortMapping,
        hal: &dyn Hal,
        enable_flat_memory_check: bool,
    ) -> Option<usize> {
        if stop.load(Ordering::Relaxed) {
            return None;
        }
        let pport_list = port_mapping.get_logical_to_physical(logical_port_name)?;
        let physical_port = *pport_list.first()?;
        let sfp = hal.sfp(physical_port).ok()?;
        if !sfp.get_presence().unwrap_or(false) {
            return None;
        }
        // `enable_flat_memory_check` gates the VDM posters (real values / thresholds /
        // flags), mirroring `_validate_and_get_physical_port(enable_flat_memory_check=
        // True)`: a flat-memory (SFF) module has no paged VDM upper memory, so it is
        // skipped. The DOM sensor / threshold / temperature / status posters pass
        // `false` (no flat gate).
        if enable_flat_memory_check && is_transceiver_flat_memory(&*sfp) {
            return None;
        }
        Some(physical_port)
    }

    /// `beautify_info_dict` — stringify non-string values in place (the default
    /// beautifier). Values already strings are left untouched.
    pub fn beautify_info_dict(&self, info_dict: &mut Map<String, Value>) {
        for (_k, v) in info_dict.iter_mut() {
            if !v.is_string() {
                *v = Value::String(value_to_py_str(v));
            }
        }
    }

    /// `get_current_time` — UTC `"%a %b %d %H:%M:%S %Y"` (e.g. `Thu Jan 01 …`).
    pub fn get_current_time(&self) -> String {
        get_current_time()
    }

    /// Shared flag poster (`post_port_dom_flags_to_db` / `post_port_transceiver_hw_
    /// status_flags_to_db` have byte-identical bodies in Python beyond the reader /
    /// beautifier / tables). Validate the port, read the flag dict off the module
    /// (honoring the per-pass `db_cache`), update the change-count / set-time /
    /// clear-time metadata **before** overwriting the value row, then beautify +
    /// post `<TABLE>|<port>` with the trailing `last_update_time`.
    ///
    /// A `None` read (module raised / returned nothing) posts nothing, matching the
    /// Python `if …_dict is None: return` and the `if not …_dict: return` empty-dict
    /// guard — only a non-empty dict updates metadata and is published.
    ///
    /// Returns `true` iff a value row was actually published, and `false` on any
    /// no-post path (validation/`sfp` failure, or a `None`/empty read). The off-cadence
    /// link-change re-read uses this to distinguish "re-captured" from "transient read
    /// yielded nothing" so it can retry rather than drop the flap (see
    /// [`super::super::dom_mgr::DomInfoUpdateTask::update_port_db_diagnostics_on_link_change`]).
    #[allow(clippy::too_many_arguments)]
    pub fn post_flags_to_db<G, B>(
        &self,
        stop: &AtomicBool,
        logical_port_name: &str,
        port_mapping: &PortMapping,
        hal: &dyn Hal,
        flag_value_table: &dyn DbTable,
        flag_change_count_table: &dyn DbTable,
        flag_last_set_time_table: &dyn DbTable,
        flag_last_clear_time_table: &dyn DbTable,
        get_flags_func: G,
        beautify_func: B,
        table_name_for_logging: &str,
        db_cache: Option<&mut DbCache>,
    ) -> bool
    where
        G: FnOnce(&dyn SfpHandle) -> Option<Value>,
        B: FnOnce(&mut Map<String, Value>),
    {
        let physical_port = match self.validate_and_get_physical_port(
            stop,
            logical_port_name,
            port_mapping,
            hal,
            false,
        ) {
            Some(p) => p,
            None => return false,
        };
        let sfp = match hal.sfp(physical_port) {
            Ok(s) => s,
            Err(_) => return false,
        };

        // Read the flag dict once per pass (a cache hit re-uses it without touching
        // the EEPROM); on a fresh read, a non-empty dict updates the metadata tables
        // before the value row is overwritten. `None` short-circuits (nothing posted).
        let read_and_update = |read: &Option<Value>| {
            if let Some(Value::Object(o)) = read {
                if !o.is_empty() {
                    let update_time = self.get_current_time();
                    self.update_flag_metadata_tables(
                        logical_port_name,
                        o,
                        &update_time,
                        flag_value_table,
                        flag_change_count_table,
                        flag_last_set_time_table,
                        flag_last_clear_time_table,
                        table_name_for_logging,
                    );
                }
            }
        };

        let flags: Option<Value> = match db_cache {
            Some(cache) => {
                if let Some(cached) = cache.get(&physical_port) {
                    cached.clone()
                } else {
                    let read = get_flags_func(&*sfp);
                    if read.is_none() {
                        return false;
                    }
                    read_and_update(&read);
                    cache.insert(physical_port, read.clone());
                    read
                }
            }
            None => {
                let read = get_flags_func(&*sfp);
                if read.is_none() {
                    return false;
                }
                read_and_update(&read);
                read
            }
        };

        let mut obj = match flags {
            Some(Value::Object(o)) if !o.is_empty() => o,
            _ => return false,
        };
        beautify_func(&mut obj);
        let mut fvs: Fvs = obj
            .iter()
            .map(|(k, v)| (k.clone(), value_to_py_str(v)))
            .collect();
        fvs.push(("last_update_time".to_string(), self.get_current_time()));
        flag_value_table.set(logical_port_name, &fvs);
        true
    }

    /// `_update_flag_metadata_tables` — compare the freshly-read flag dict against the
    /// value row already in STATE_DB and maintain the three side tables. On the very
    /// first publish (value row absent) it seeds the metadata (count `0`, times
    /// `never`); afterwards it bumps the per-flag change count and stamps set/clear
    /// time only for flags whose value actually **transitioned**. `"N/A"` values are
    /// skipped (`curr_flag_dict` carries the RAW pre-beautify values).
    #[allow(clippy::too_many_arguments)]
    pub fn update_flag_metadata_tables(
        &self,
        logical_port_name: &str,
        curr_flag_dict: &Map<String, Value>,
        flag_values_dict_update_time: &str,
        flag_value_table: &dyn DbTable,
        flag_change_count_table: &dyn DbTable,
        flag_last_set_time_table: &dyn DbTable,
        flag_last_clear_time_table: &dyn DbTable,
        table_name_for_logging: &str,
    ) {
        // `Table.get` -> None == "row not present" (Python `found == False`).
        let db_flags_value_dict: HashMap<String, String> =
            match flag_value_table.get(logical_port_name) {
                None => {
                    self.initialize_metadata_tables(
                        logical_port_name,
                        curr_flag_dict,
                        flag_change_count_table,
                        flag_last_set_time_table,
                        flag_last_clear_time_table,
                    );
                    return;
                }
                Some(rows) => rows.into_iter().collect(),
            };

        for (flag_key, curr_flag_value) in curr_flag_dict {
            let curr_str = value_to_py_str(curr_flag_value);
            if curr_str.trim() == Self::NOT_AVAILABLE {
                continue; // Skip "N/A" values
            }
            if let Some(db_val) = db_flags_value_dict.get(flag_key) {
                if db_val != &curr_str {
                    self.update_flag_metadata(
                        logical_port_name,
                        flag_key,
                        curr_flag_value,
                        flag_values_dict_update_time,
                        flag_change_count_table,
                        flag_last_set_time_table,
                        flag_last_clear_time_table,
                        table_name_for_logging,
                    );
                }
            }
        }
    }

    /// `_initialize_metadata_tables` — clean-slate seed on the first publish: every
    /// flag key gets change count `0` and set/clear time `never`. Each table is written
    /// in a single `set` call carrying every key: the real (merging) `Table.set` and a
    /// per-key loop are observably identical, but batching also keeps the row intact under
    /// the replace-on-set test mock (mirroring `mock_swsscommon.Table.set`) so a
    /// multi-flag first publish seeds *all* keys, not just the last.
    fn initialize_metadata_tables(
        &self,
        logical_port_name: &str,
        curr_flag_dict: &Map<String, Value>,
        flag_change_count_table: &dyn DbTable,
        flag_last_set_time_table: &dyn DbTable,
        flag_last_clear_time_table: &dyn DbTable,
    ) {
        let count_fvs: Fvs = curr_flag_dict
            .keys()
            .map(|key| (key.clone(), "0".to_string()))
            .collect();
        let never_fvs: Fvs = curr_flag_dict
            .keys()
            .map(|key| (key.clone(), Self::NEVER.to_string()))
            .collect();
        flag_change_count_table.set(logical_port_name, &count_fvs);
        flag_last_set_time_table.set(logical_port_name, &never_fvs);
        flag_last_clear_time_table.set(logical_port_name, &never_fvs);
    }

    /// `_update_flag_metadata` — one transitioned flag: increment its change count and
    /// stamp the set time (flag now truthy) or clear time (flag now falsy).
    #[allow(clippy::too_many_arguments)]
    fn update_flag_metadata(
        &self,
        logical_port_name: &str,
        flag_key: &str,
        curr_flag_value: &Value,
        flag_values_dict_update_time: &str,
        flag_change_count_table: &dyn DbTable,
        flag_last_set_time_table: &dyn DbTable,
        flag_last_clear_time_table: &dyn DbTable,
        table_name_for_logging: &str,
    ) {
        let db_change_count_dict: HashMap<String, String> = match flag_change_count_table
            .get(logical_port_name)
        {
            None => {
                eprintln!(
                        "xcvrd-rs: failed to get change count for {table_name_for_logging} port {logical_port_name}"
                    );
                return;
            }
            Some(rows) => rows.into_iter().collect(),
        };
        let db_change_count = db_change_count_dict
            .get(flag_key)
            .and_then(|s| s.parse::<i64>().ok())
            .unwrap_or(0)
            + 1;
        flag_change_count_table.set(
            logical_port_name,
            &[(flag_key.to_string(), db_change_count.to_string())],
        );

        if py_truthy(curr_flag_value) {
            flag_last_set_time_table.set(
                logical_port_name,
                &[(
                    flag_key.to_string(),
                    flag_values_dict_update_time.to_string(),
                )],
            );
        } else {
            flag_last_clear_time_table.set(
                logical_port_name,
                &[(
                    flag_key.to_string(),
                    flag_values_dict_update_time.to_string(),
                )],
            );
        }
    }
}

/// Python truthiness of a JSON value — the set-vs-clear-time decision keys on the RAW
/// flag value (`if curr_flag_value:`). Module DOM/status flags are booleans, so this
/// is normally just the bool; the full rule keeps parity if a reader ever yields a
/// number/string/None. Also reused by `XCVRDUtils`/`VDMUtils` for the module boolean
/// getters (`is_flat_memory`, `is_transceiver_vdm_supported`, …).
pub(crate) fn py_truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(true),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

impl Default for DbUtils {
    fn default() -> Self {
        DbUtils::new()
    }
}

const WDAY: [&str; 7] = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MON: [&str; 12] = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

/// Render a `SystemTime` as `datetime.utcnow().strftime("%a %b %d %H:%M:%S %Y")`
/// without pulling in `chrono` (the suite only asserts the field's presence, but the
/// byte-exact format keeps parity with the Python daemon). Uses Howard Hinnant's
/// days→civil algorithm.
fn format_utc(t: SystemTime) -> String {
    let secs = t
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0) as i64;
    let days = secs.div_euclid(86400);
    let tod = secs.rem_euclid(86400);
    let (hour, min, sec) = (tod / 3600, (tod % 3600) / 60, tod % 60);
    let (year, month, day) = civil_from_days(days);
    let wday = (days.rem_euclid(7) + 4).rem_euclid(7) as usize; // 1970-01-01 = Thursday
    format!(
        "{} {} {:02} {:02}:{:02}:{:02} {}",
        WDAY[wday],
        MON[(month - 1) as usize],
        day,
        hour,
        min,
        sec,
        year
    )
}

/// Convert a count of days since 1970-01-01 to `(year, month[1-12], day[1-31])`.
fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = doy - (153 * mp + 2) / 5 + 1; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 }; // [1, 12]
    (if m <= 2 { y + 1 } else { y }, m, d)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::mock::{MockDbTable, MockHal, MockSfp};
    use crate::xcvrd_utilities::port_event_helper::{build_port_mapping, PortConfigRow};
    use serde_json::json;

    /// A single-ASIC port mapping over `(logical name, physical index)` pairs.
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

    #[test]
    fn test_value_to_py_str_and_strip_unit() {
        // Pure text fidelity — kept real; the hal.rs sanitizer tests reuse this.
        assert_eq!(value_to_py_str(&json!(true)), "True");
        assert_eq!(value_to_py_str(&json!(false)), "False");
        assert_eq!(value_to_py_str(&Value::Null), "None");
        assert_eq!(value_to_py_str(&json!("QSFP-DD")), "QSFP-DD");
        assert_eq!(value_to_py_str(&json!(75.0)), "75.0");
        assert_eq!(strip_unit(&json!("22.75C"), "C"), "22.75");
        assert_eq!(strip_unit(&json!("3.30Volts"), "Volts"), "3.30");
        assert_eq!(strip_unit(&json!(42), "C"), "42");
    }

    #[test]
    fn test_beautify_info_dict() {
        let mut m: Map<String, Value> = Map::new();
        m.insert("a".into(), json!(5));
        m.insert("b".into(), json!("already"));
        m.insert("c".into(), json!(true));
        DbUtils.beautify_info_dict(&mut m);
        assert_eq!(m["a"], json!("5"));
        assert_eq!(m["b"], json!("already"));
        assert_eq!(m["c"], json!("True"));
    }

    #[test]
    fn test_get_current_time_format() {
        // "%a %b %d %H:%M:%S %Y" — e.g. "Thu Jan 01 00:00:00 1970".
        assert_eq!(format_utc(UNIX_EPOCH), "Thu Jan 01 00:00:00 1970");
        // A known later instant: 2021-01-01 00:00:00 UTC = 1609459200.
        let s2 = format_utc(UNIX_EPOCH + std::time::Duration::from_secs(1_609_459_200));
        assert_eq!(s2, "Fri Jan 01 00:00:00 2021");
        // The live stamp is well-formed (Wkd Mon DD HH:MM:SS YYYY = 24 chars).
        assert_eq!(DbUtils.get_current_time().len(), 24);
    }

    #[test]
    fn test_py_truthy() {
        assert!(!py_truthy(&json!(false)));
        assert!(py_truthy(&json!(true)));
        assert!(!py_truthy(&Value::Null));
        assert!(!py_truthy(&json!(0)));
        assert!(py_truthy(&json!(1)));
        assert!(!py_truthy(&json!("")));
        assert!(py_truthy(&json!("x")));
    }

    fn single(key: &str, v: Value) -> Map<String, Value> {
        let mut m = Map::new();
        m.insert(key.to_string(), v);
        m
    }

    // ← tests/test_xcvrd.py::test_update_flag_metadata_tables (parametrized)
    #[test]
    fn test_update_flag_metadata_tables_first_publish_initializes() {
        let value_tbl = MockDbTable::new("TRANSCEIVER_DOM_FLAG");
        let count_tbl = MockDbTable::new("TRANSCEIVER_DOM_FLAG_CHANGE_COUNT");
        let set_tbl = MockDbTable::new("TRANSCEIVER_DOM_FLAG_SET_TIME");
        let clear_tbl = MockDbTable::new("TRANSCEIVER_DOM_FLAG_CLEAR_TIME");

        // Value row absent → first publish seeds count 0 + set/clear time "never".
        let flags = single("tempHAlarm", json!(false));
        DbUtils.update_flag_metadata_tables(
            "Ethernet0",
            &flags,
            "Thu Jan 01 00:00:00 1970",
            &value_tbl,
            &count_tbl,
            &set_tbl,
            &clear_tbl,
            "DOM flags",
        );
        assert_eq!(count_tbl.hget("Ethernet0", "tempHAlarm"), Some("0".into()));
        assert_eq!(
            set_tbl.hget("Ethernet0", "tempHAlarm"),
            Some("never".into())
        );
        assert_eq!(
            clear_tbl.hget("Ethernet0", "tempHAlarm"),
            Some("never".into())
        );
    }

    #[test]
    fn test_update_flag_metadata_tables_transition_and_noop() {
        let value_tbl = MockDbTable::new("TRANSCEIVER_DOM_FLAG");
        let count_tbl = MockDbTable::new("TRANSCEIVER_DOM_FLAG_CHANGE_COUNT");
        let set_tbl = MockDbTable::new("TRANSCEIVER_DOM_FLAG_SET_TIME");
        let clear_tbl = MockDbTable::new("TRANSCEIVER_DOM_FLAG_CLEAR_TIME");

        // Seed the value row (previous publish, flag False) + count 0.
        value_tbl.set("Ethernet0", &[("tempHAlarm".into(), "False".into())]);
        count_tbl.set("Ethernet0", &[("tempHAlarm".into(), "0".into())]);

        // False → True transition: count bumps to 1, SET_TIME stamped.
        DbUtils.update_flag_metadata_tables(
            "Ethernet0",
            &single("tempHAlarm", json!(true)),
            "Thu Jan 01 00:00:05 1970",
            &value_tbl,
            &count_tbl,
            &set_tbl,
            &clear_tbl,
            "DOM flags",
        );
        assert_eq!(count_tbl.hget("Ethernet0", "tempHAlarm"), Some("1".into()));
        assert_eq!(
            set_tbl.hget("Ethernet0", "tempHAlarm"),
            Some("Thu Jan 01 00:00:05 1970".into())
        );
        assert_eq!(clear_tbl.hget("Ethernet0", "tempHAlarm"), None);

        // Publish the new value (True) so the next compare is against it.
        value_tbl.set("Ethernet0", &[("tempHAlarm".into(), "True".into())]);

        // No-op: same True value → no transition, count stays 1, SET_TIME unchanged.
        DbUtils.update_flag_metadata_tables(
            "Ethernet0",
            &single("tempHAlarm", json!(true)),
            "Thu Jan 01 00:00:10 1970",
            &value_tbl,
            &count_tbl,
            &set_tbl,
            &clear_tbl,
            "DOM flags",
        );
        assert_eq!(count_tbl.hget("Ethernet0", "tempHAlarm"), Some("1".into()));
        assert_eq!(
            set_tbl.hget("Ethernet0", "tempHAlarm"),
            Some("Thu Jan 01 00:00:05 1970".into())
        );

        // True → False transition: count bumps to 2, CLEAR_TIME stamped.
        DbUtils.update_flag_metadata_tables(
            "Ethernet0",
            &single("tempHAlarm", json!(false)),
            "Thu Jan 01 00:00:20 1970",
            &value_tbl,
            &count_tbl,
            &set_tbl,
            &clear_tbl,
            "DOM flags",
        );
        assert_eq!(count_tbl.hget("Ethernet0", "tempHAlarm"), Some("2".into()));
        assert_eq!(
            clear_tbl.hget("Ethernet0", "tempHAlarm"),
            Some("Thu Jan 01 00:00:20 1970".into())
        );
    }

    #[test]
    fn test_update_flag_metadata_tables_skips_na() {
        let value_tbl = MockDbTable::new("TRANSCEIVER_DOM_FLAG");
        let count_tbl = MockDbTable::new("TRANSCEIVER_DOM_FLAG_CHANGE_COUNT");
        let set_tbl = MockDbTable::new("TRANSCEIVER_DOM_FLAG_SET_TIME");
        let clear_tbl = MockDbTable::new("TRANSCEIVER_DOM_FLAG_CLEAR_TIME");
        value_tbl.set("Ethernet0", &[("tempHAlarm".into(), "N/A".into())]);
        count_tbl.set("Ethernet0", &[("tempHAlarm".into(), "0".into())]);

        // A "N/A" current value is skipped — no metadata update.
        DbUtils.update_flag_metadata_tables(
            "Ethernet0",
            &single("tempHAlarm", json!("N/A")),
            "Thu Jan 01 00:00:05 1970",
            &value_tbl,
            &count_tbl,
            &set_tbl,
            &clear_tbl,
            "DOM flags",
        );
        assert_eq!(count_tbl.hget("Ethernet0", "tempHAlarm"), Some("0".into()));
        assert_eq!(set_tbl.hget("Ethernet0", "tempHAlarm"), None);
    }

    // ← tests/test_xcvrd.py::test_validate_and_get_physical_port
    #[test]
    fn test_validate_and_get_physical_port() {
        let pm = mapping_with(&[("Ethernet0", 0)]);
        let hal = MockHal::with_sfps(vec![MockSfp::present()]);
        let stop = AtomicBool::new(false);
        assert_eq!(
            DbUtils.validate_and_get_physical_port(&stop, "Ethernet0", &pm, &hal, false),
            Some(0)
        );
        // Stop set → skip.
        let stopped = AtomicBool::new(true);
        assert_eq!(
            DbUtils.validate_and_get_physical_port(&stopped, "Ethernet0", &pm, &hal, false),
            None
        );
        // Unknown logical port → skip.
        assert_eq!(
            DbUtils.validate_and_get_physical_port(&stop, "Ethernet99", &pm, &hal, false),
            None
        );
        // Absent module → skip.
        let hal_absent = MockHal::with_sfps(vec![MockSfp::absent()]);
        assert_eq!(
            DbUtils.validate_and_get_physical_port(&stop, "Ethernet0", &pm, &hal_absent, false),
            None
        );
    }

    // ← tests/test_xcvrd.py::test_post_diagnostic_values_to_db
    #[test]
    fn test_post_diagnostic_values_to_db_stamps_and_beautifies() {
        let pm = mapping_with(&[("Ethernet0", 0)]);
        let hal =
            MockHal::with_sfps(vec![MockSfp::present()
                .with_dom_real_value(json!({"temperature": 22.5, "voltage": 3.3}))]);
        let table = MockDbTable::new("TRANSCEIVER_DOM_SENSOR");
        let stop = AtomicBool::new(false);
        DbUtils.post_diagnostic_values_to_db(
            &stop,
            "Ethernet0",
            &pm,
            &table,
            &hal,
            |sfp| sfp.get_transceiver_dom_real_value().ok(),
            None,
            |m| DbUtils.beautify_info_dict(m),
            false,
        );
        let row: HashMap<String, String> = table.get("Ethernet0").unwrap().into_iter().collect();
        assert_eq!(row.get("temperature").map(String::as_str), Some("22.5"));
        assert_eq!(row.get("voltage").map(String::as_str), Some("3.3"));
        assert!(row.contains_key("last_update_time"));
    }

    #[test]
    fn test_post_diagnostic_values_to_db_empty_dict_posts_nothing() {
        let pm = mapping_with(&[("Ethernet0", 0)]);
        let hal = MockHal::with_sfps(vec![MockSfp::present().with_dom_real_value(json!({}))]);
        let table = MockDbTable::new("TRANSCEIVER_DOM_SENSOR");
        let stop = AtomicBool::new(false);
        DbUtils.post_diagnostic_values_to_db(
            &stop,
            "Ethernet0",
            &pm,
            &table,
            &hal,
            |sfp| sfp.get_transceiver_dom_real_value().ok(),
            None,
            |m| DbUtils.beautify_info_dict(m),
            false,
        );
        assert_eq!(table.get("Ethernet0"), None);
    }
}
