//! `dom/utilities/dom_sensor/{utils,db_utils}.py` → `DOMUtils` (reads the module DOM
//! monitors off the SFP handle) + `DOMDBUtils` (posts them to `TRANSCEIVER_DOM_SENSOR`
//! / `_TEMPERATURE` / `_THRESHOLD` / `_FLAG`) (analysis §3.2).
//!
//! The posters delegate to the shared [`DbUtils::post_diagnostic_values_to_db`] /
//! [`DbUtils::post_flags_to_db`] (validate → read → beautify → set); the only
//! DOM-specific piece is [`DomDbUtils::beautify_dom_info_dict`], which strips the
//! engineering units the module reports (`22.0C`/`3.3Volts`/`-1.2dBm`/`7.5mA`) so
//! STATE_DB carries bare numbers, exactly as `_beautify_dom_info_dict` does. CMIS
//! decode stays in Python — the readers only forward the `sonic_platform` calls.
#![allow(dead_code, unused_variables, unused_imports)]

use std::sync::atomic::AtomicBool;

use serde_json::{Map, Value};

use crate::db::DbTable;
use crate::hal::{Hal, SfpHandle};
use crate::xcvrd_utilities::port_event_helper::PortMapping;

use super::db::{strip_unit, value_to_py_str, DbCache, DbUtils};

/// `DOMUtils` — read the module DOM monitor dicts off the SFP handle.
///
/// Each reader mirrors the Python `try: … except NotImplementedError: return {}`: a
/// successful read yields `Some(value)`; a not-implemented/errored read yields `None`.
/// The shared poster treats `None` and an empty dict identically (nothing to post),
/// so the two representations are behaviorally equivalent.
pub struct DomUtils;

impl DomUtils {
    pub fn new() -> Self {
        DomUtils
    }

    /// `get_transceiver_dom_temperature` — `{'temperature': sfp.get_temperature()}`.
    pub fn get_transceiver_dom_temperature(&self, sfp: &dyn SfpHandle) -> Option<Value> {
        match sfp.call_json("get_temperature") {
            Ok(temp) => {
                let mut m = Map::new();
                m.insert("temperature".to_string(), temp);
                Some(Value::Object(m))
            }
            Err(_) => None,
        }
    }

    /// `get_transceiver_dom_sensor_real_value` — `sfp.get_transceiver_dom_real_value()`.
    pub fn get_transceiver_dom_sensor_real_value(&self, sfp: &dyn SfpHandle) -> Option<Value> {
        sfp.get_transceiver_dom_real_value().ok()
    }

    /// `get_transceiver_dom_flags` — `sfp.get_transceiver_dom_flags()`.
    ///
    /// Pure delegate, mirroring `DOMUtils.get_transceiver_dom_flags`
    /// (`try: return sfp.get_transceiver_dom_flags() except NotImplementedError: {}`): a
    /// successful read yields `Some(dict)`; a not-implemented / errored read yields `None`
    /// (which the shared poster treats identically to an empty dict — nothing to post).
    ///
    /// Every latched DOM flag is decoded IN PYTHON by `CmisApi.get_transceiver_dom_flags`
    /// and forwarded here verbatim: the module temp/vcc alarm-warning group
    /// (`tempHAlarm`..`vccLWarn`) is decoded together from the latched 00h:9 byte via
    /// `get_module_level_flag` (`ModuleFlagByte1`), so `tempHAlarm` and `vccHAlarm` always
    /// surface as a pair and settle to their resting `False`. The reader adds/drops NOTHING
    /// and performs no Rust-side EEPROM decode — CMIS decode stays in Python (analysis §3.2).
    pub fn get_transceiver_dom_flags(&self, sfp: &dyn SfpHandle) -> Option<Value> {
        sfp.call_json("get_transceiver_dom_flags").ok()
    }

    /// `get_transceiver_dom_thresholds` — `sfp.get_transceiver_threshold_info()`.
    pub fn get_transceiver_dom_thresholds(&self, sfp: &dyn SfpHandle) -> Option<Value> {
        sfp.get_transceiver_threshold_info().ok()
    }
}

impl Default for DomUtils {
    fn default() -> Self {
        DomUtils::new()
    }
}

/// `DOMDBUtils` — the DOM posters (`TRANSCEIVER_DOM_SENSOR/_TEMPERATURE/_THRESHOLD/
/// _FLAG`), a subclass of the shared [`DbUtils`] engine.
pub struct DomDbUtils {
    base: DbUtils,
}

impl DomDbUtils {
    pub const TEMP_UNIT: &'static str = "C";
    pub const VOLT_UNIT: &'static str = "Volts";
    pub const POWER_UNIT: &'static str = "dBm";
    pub const BIAS_UNIT: &'static str = "mA";

    pub fn new() -> Self {
        DomDbUtils {
            base: DbUtils::new(),
        }
    }

    /// `post_port_dom_sensor_info_to_db` → `TRANSCEIVER_DOM_SENSOR`. Reads the module's
    /// real-value DOM dict (temperature, voltage, the 24 per-lane tx/rx power + tx
    /// bias) and posts it unit-stripped with a trailing `last_update_time`.
    pub fn post_port_dom_sensor_info_to_db(
        &self,
        stop: &AtomicBool,
        logical_port_name: &str,
        port_mapping: &PortMapping,
        table: &dyn DbTable,
        hal: &dyn Hal,
        db_cache: Option<&mut DbCache>,
    ) {
        if port_mapping
            .get_asic_id_for_logical_port(logical_port_name)
            .is_none()
        {
            return;
        }
        let dom = DomUtils;
        self.base.post_diagnostic_values_to_db(
            stop,
            logical_port_name,
            port_mapping,
            table,
            hal,
            |sfp| dom.get_transceiver_dom_sensor_real_value(sfp),
            db_cache,
            Self::beautify_dom_info_dict,
            false,
        );
    }

    /// `post_port_dom_temperature_info_to_db` → `TRANSCEIVER_DOM_TEMPERATURE` (the fast
    /// loop). Not launched by the daemon (Python default
    /// `dom_temperature_poll_interval is None`), but kept faithful + unit-tested.
    pub fn post_port_dom_temperature_info_to_db(
        &self,
        stop: &AtomicBool,
        logical_port_name: &str,
        port_mapping: &PortMapping,
        table: &dyn DbTable,
        hal: &dyn Hal,
        db_cache: Option<&mut DbCache>,
    ) {
        if port_mapping
            .get_asic_id_for_logical_port(logical_port_name)
            .is_none()
        {
            return;
        }
        let dom = DomUtils;
        self.base.post_diagnostic_values_to_db(
            stop,
            logical_port_name,
            port_mapping,
            table,
            hal,
            |sfp| dom.get_transceiver_dom_temperature(sfp),
            db_cache,
            Self::beautify_dom_info_dict,
            false,
        );
    }

    /// `post_port_dom_thresholds_to_db` → `TRANSCEIVER_DOM_THRESHOLD` (seeded once at
    /// boot / at insert — decoded page-02h alarm/warning thresholds).
    pub fn post_port_dom_thresholds_to_db(
        &self,
        stop: &AtomicBool,
        logical_port_name: &str,
        port_mapping: &PortMapping,
        table: &dyn DbTable,
        hal: &dyn Hal,
        db_cache: Option<&mut DbCache>,
    ) {
        if port_mapping
            .get_asic_id_for_logical_port(logical_port_name)
            .is_none()
        {
            return;
        }
        let dom = DomUtils;
        self.base.post_diagnostic_values_to_db(
            stop,
            logical_port_name,
            port_mapping,
            table,
            hal,
            |sfp| dom.get_transceiver_dom_thresholds(sfp),
            db_cache,
            Self::beautify_dom_info_dict,
            false,
        );
    }

    /// `post_port_dom_flags_to_db` → `TRANSCEIVER_DOM_FLAG` + its change-count /
    /// set-time / clear-time metadata tables. Reads the module's latched DOM monitor
    /// flags, stamps change-tracking metadata on every transition, and publishes the
    /// (unit-free) flag row. Shares [`DbUtils::post_flags_to_db`] with the status poster.
    ///
    /// Returns `true` iff a value row was published (see [`DbUtils::post_flags_to_db`]);
    /// a `None`/empty read returns `false` so the off-cadence link-change re-read can retry.
    #[allow(clippy::too_many_arguments)]
    pub fn post_port_dom_flags_to_db(
        &self,
        stop: &AtomicBool,
        logical_port_name: &str,
        port_mapping: &PortMapping,
        hal: &dyn Hal,
        flag_tbl: &dyn DbTable,
        flag_change_count_tbl: &dyn DbTable,
        flag_set_time_tbl: &dyn DbTable,
        flag_clear_time_tbl: &dyn DbTable,
        db_cache: Option<&mut DbCache>,
    ) -> bool {
        if port_mapping
            .get_asic_id_for_logical_port(logical_port_name)
            .is_none()
        {
            return false;
        }
        let dom = DomUtils;
        self.base.post_flags_to_db(
            stop,
            logical_port_name,
            port_mapping,
            hal,
            flag_tbl,
            flag_change_count_tbl,
            flag_set_time_tbl,
            flag_clear_time_tbl,
            |sfp| dom.get_transceiver_dom_flags(sfp),
            Self::beautify_dom_info_dict,
            "DOM flags",
            db_cache,
        )
    }

    /// `_beautify_dom_info_dict` — strip the reported engineering unit from the DOM
    /// monitor values (`temperature`→drop `C`, `voltage`→drop `Volts`,
    /// `(tx|rx)[1-8]power`→drop `dBm`, `(tx|rx)[1-8]bias`→drop `mA`), and `str()` any
    /// remaining non-string value. Keys are preserved (an `N/A` value that lacks the
    /// unit stays `N/A`; a flag key like `tempHAlarm` is not a lane key, so its bool
    /// stringifies to `True`/`False`).
    pub fn beautify_dom_info_dict(dom_info_dict: &mut Map<String, Value>) {
        for (k, v) in dom_info_dict.iter_mut() {
            if k == "temperature" {
                *v = Value::String(strip_unit(v, Self::TEMP_UNIT));
            } else if k == "voltage" {
                *v = Value::String(strip_unit(v, Self::VOLT_UNIT));
            } else if is_lane_key(k, "power") {
                *v = Value::String(strip_unit(v, Self::POWER_UNIT));
            } else if is_lane_key(k, "bias") {
                *v = Value::String(strip_unit(v, Self::BIAS_UNIT));
            } else if !v.is_string() {
                *v = Value::String(value_to_py_str(v));
            }
        }
    }
}

impl Default for DomDbUtils {
    fn default() -> Self {
        DomDbUtils::new()
    }
}

/// `^(tx|rx)[1-8]<suffix>$` — the per-lane DOM key shape (`tx3power`, `rx8bias`, …).
/// Flag keys (`tx1powerHAlarm`, `tempHAlarm`) fail the strict length check, so their
/// booleans stringify instead of being unit-stripped.
fn is_lane_key(k: &str, suffix: &str) -> bool {
    let b = k.as_bytes();
    k.len() == 3 + suffix.len()
        && (b[0] == b't' || b[0] == b'r')
        && b[1] == b'x'
        && (b'1'..=b'8').contains(&b[2])
        && &k[3..] == suffix
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

    /// A full DOM real-value dict: temperature + voltage + the 24 per-lane keys, each
    /// carrying its engineering unit so we can assert the strip.
    fn full_dom_real_value() -> Value {
        let mut m = Map::new();
        m.insert("temperature".into(), json!("42.5C"));
        m.insert("voltage".into(), json!("3.3Volts"));
        for lane in 1..=8 {
            m.insert(format!("tx{lane}power"), json!(format!("-1.5dBm")));
            m.insert(format!("rx{lane}power"), json!(format!("-2.5dBm")));
            m.insert(format!("tx{lane}bias"), json!(format!("7.5mA")));
        }
        Value::Object(m)
    }

    // ← tests/test_xcvrd.py::test_beautify_dom_info_dict
    #[test]
    fn test_beautify_dom_info_dict_strips_units_and_stringifies() {
        let mut m = Map::new();
        m.insert("temperature".into(), json!("22.75C"));
        m.insert("voltage".into(), json!("3.30Volts"));
        m.insert("tx1power".into(), json!("-1.20dBm"));
        m.insert("rx8power".into(), json!("-2.40dBm"));
        m.insert("tx3bias".into(), json!("7.50mA"));
        m.insert("tempHAlarm".into(), json!(false)); // flag key: not a lane key
        m.insert("rx_los".into(), json!(true)); // arbitrary bool: str()'d
        DomDbUtils::beautify_dom_info_dict(&mut m);
        assert_eq!(m["temperature"], json!("22.75"));
        assert_eq!(m["voltage"], json!("3.30"));
        assert_eq!(m["tx1power"], json!("-1.20"));
        assert_eq!(m["rx8power"], json!("-2.40"));
        assert_eq!(m["tx3bias"], json!("7.50"));
        assert_eq!(m["tempHAlarm"], json!("False"));
        assert_eq!(m["rx_los"], json!("True"));
    }

    #[test]
    fn test_is_lane_key_shape() {
        assert!(is_lane_key("tx1power", "power"));
        assert!(is_lane_key("rx8power", "power"));
        assert!(is_lane_key("tx4bias", "bias"));
        assert!(!is_lane_key("tx9power", "power")); // lane out of 1..=8
        assert!(!is_lane_key("tx1powerHAlarm", "power")); // flag key, wrong length
        assert!(!is_lane_key("temperature", "power"));
    }

    // ← tests/test_xcvrd.py::test_post_port_dom_sensor_info_to_db
    #[test]
    fn test_post_port_dom_sensor_info_to_db_full_row() {
        let pm = mapping_with(&[("Ethernet0", 0)]);
        let hal = MockHal::with_sfps(vec![
            MockSfp::present().with_dom_real_value(full_dom_real_value())
        ]);
        let tbl = MockDbTable::new("TRANSCEIVER_DOM_SENSOR");
        DomDbUtils::new().post_port_dom_sensor_info_to_db(
            &AtomicBool::new(false),
            "Ethernet0",
            &pm,
            &tbl,
            &hal,
            None,
        );
        let row: HashMap<String, String> = tbl.get("Ethernet0").unwrap().into_iter().collect();
        assert_eq!(row.get("temperature").map(String::as_str), Some("42.5"));
        assert_eq!(row.get("voltage").map(String::as_str), Some("3.3"));
        // All 24 per-lane keys present + unit-stripped.
        for lane in 1..=8 {
            assert_eq!(
                row.get(&format!("tx{lane}power")).map(String::as_str),
                Some("-1.5")
            );
            assert_eq!(
                row.get(&format!("rx{lane}power")).map(String::as_str),
                Some("-2.5")
            );
            assert_eq!(
                row.get(&format!("tx{lane}bias")).map(String::as_str),
                Some("7.5")
            );
        }
        assert!(row.contains_key("last_update_time"));
    }

    #[test]
    fn test_post_port_dom_sensor_absent_posts_nothing() {
        let pm = mapping_with(&[("Ethernet0", 0)]);
        let hal = MockHal::with_sfps(vec![MockSfp::absent()]);
        let tbl = MockDbTable::new("TRANSCEIVER_DOM_SENSOR");
        DomDbUtils::new().post_port_dom_sensor_info_to_db(
            &AtomicBool::new(false),
            "Ethernet0",
            &pm,
            &tbl,
            &hal,
            None,
        );
        assert_eq!(tbl.get("Ethernet0"), None);
    }

    // ← tests/test_xcvrd.py::test_post_port_dom_thresholds_to_db
    #[test]
    fn test_post_port_dom_thresholds_to_db() {
        let pm = mapping_with(&[("Ethernet0", 0)]);
        let hal = MockHal::with_sfps(vec![MockSfp::present().with_threshold_info(json!({
            "temphighalarm": 75.0,
            "templowalarm": -5.0,
            "vcchighalarm": 3.63,
        }))]);
        let tbl = MockDbTable::new("TRANSCEIVER_DOM_THRESHOLD");
        DomDbUtils::new().post_port_dom_thresholds_to_db(
            &AtomicBool::new(false),
            "Ethernet0",
            &pm,
            &tbl,
            &hal,
            None,
        );
        let row: HashMap<String, String> = tbl.get("Ethernet0").unwrap().into_iter().collect();
        assert_eq!(row.get("temphighalarm").map(String::as_str), Some("75.0"));
        assert_eq!(row.get("templowalarm").map(String::as_str), Some("-5.0"));
        assert_eq!(row.get("vcchighalarm").map(String::as_str), Some("3.63"));
        assert!(row.contains_key("last_update_time"));
    }

    // ← tests/test_xcvrd.py::test_post_port_dom_temperature_info_to_db
    #[test]
    fn test_post_port_dom_temperature_info_to_db() {
        let pm = mapping_with(&[("Ethernet0", 0)]);
        let hal = MockHal::with_sfps(vec![
            MockSfp::present().with_json("get_temperature", json!(42.5))
        ]);
        let tbl = MockDbTable::new("TRANSCEIVER_DOM_TEMPERATURE");
        DomDbUtils::new().post_port_dom_temperature_info_to_db(
            &AtomicBool::new(false),
            "Ethernet0",
            &pm,
            &tbl,
            &hal,
            None,
        );
        let row: HashMap<String, String> = tbl.get("Ethernet0").unwrap().into_iter().collect();
        assert_eq!(row.get("temperature").map(String::as_str), Some("42.5"));
        assert!(row.contains_key("last_update_time"));
    }

    // ← tests/test_xcvrd.py::test_post_port_dom_flags_to_db
    #[test]
    fn test_post_port_dom_flags_to_db_publishes_value_row() {
        // The value row is written in one set() call, so the full multi-flag set is
        // published (booleans stringified) with a trailing timestamp.
        let pm = mapping_with(&[("Ethernet0", 0)]);
        let hal = MockHal::with_sfps(vec![MockSfp::present().with_json(
            "get_transceiver_dom_flags",
            json!({"tempHAlarm": false, "vccHAlarm": true}),
        )]);
        let flag = MockDbTable::new("TRANSCEIVER_DOM_FLAG");
        let count = MockDbTable::new("TRANSCEIVER_DOM_FLAG_CHANGE_COUNT");
        let set_t = MockDbTable::new("TRANSCEIVER_DOM_FLAG_SET_TIME");
        let clear_t = MockDbTable::new("TRANSCEIVER_DOM_FLAG_CLEAR_TIME");
        DomDbUtils::new().post_port_dom_flags_to_db(
            &AtomicBool::new(false),
            "Ethernet0",
            &pm,
            &hal,
            &flag,
            &count,
            &set_t,
            &clear_t,
            None,
        );
        let row: HashMap<String, String> = flag.get("Ethernet0").unwrap().into_iter().collect();
        assert_eq!(row.get("tempHAlarm").map(String::as_str), Some("False"));
        assert_eq!(row.get("vccHAlarm").map(String::as_str), Some("True"));
        assert!(row.contains_key("last_update_time"));
    }

    // The post reports whether a value row was actually published, so the off-cadence
    // link-change re-read can distinguish "recaptured" from "transient read yielded nothing"
    // and retry rather than drop the flap. A good decode returns true and publishes; an
    // empty/`None` decode returns false and publishes nothing (mirrors Python's
    // `if not …_dict: return` guard, now surfaced to the caller).
    #[test]
    fn test_post_port_dom_flags_to_db_reports_publish() {
        let pm = mapping_with(&[("Ethernet0", 0)]);
        let post = |dom_flags: Value| {
            let hal = MockHal::with_sfps(vec![
                MockSfp::present().with_json("get_transceiver_dom_flags", dom_flags)
            ]);
            let flag = MockDbTable::new("TRANSCEIVER_DOM_FLAG");
            let count = MockDbTable::new("TRANSCEIVER_DOM_FLAG_CHANGE_COUNT");
            let set_t = MockDbTable::new("TRANSCEIVER_DOM_FLAG_SET_TIME");
            let clear_t = MockDbTable::new("TRANSCEIVER_DOM_FLAG_CLEAR_TIME");
            let posted = DomDbUtils::new().post_port_dom_flags_to_db(
                &AtomicBool::new(false),
                "Ethernet0",
                &pm,
                &hal,
                &flag,
                &count,
                &set_t,
                &clear_t,
                None,
            );
            (posted, flag.get("Ethernet0").is_some())
        };
        assert_eq!(
            post(json!({"tempHAlarm": false})),
            (true, true),
            "a non-empty decode returns true and publishes the value row"
        );
        assert_eq!(
            post(json!({})),
            (false, false),
            "an empty decode returns false and publishes nothing"
        );
    }

    #[test]
    fn test_post_port_dom_flags_to_db_inits_metadata_first_publish() {
        // Single-flag scenario (the mock's Table.set REPLACES the whole row, mirroring
        // mock_swsscommon; the real swss HSET merges, so multi-flag init accumulates on
        // the DUT — see the db.rs metadata-engine tests). Here we assert the first
        // publish seeds the metadata for the flag: count 0, set/clear time "never".
        let pm = mapping_with(&[("Ethernet0", 0)]);
        let hal =
            MockHal::with_sfps(vec![MockSfp::present()
                .with_json("get_transceiver_dom_flags", json!({"tempHAlarm": false}))]);
        let flag = MockDbTable::new("TRANSCEIVER_DOM_FLAG");
        let count = MockDbTable::new("TRANSCEIVER_DOM_FLAG_CHANGE_COUNT");
        let set_t = MockDbTable::new("TRANSCEIVER_DOM_FLAG_SET_TIME");
        let clear_t = MockDbTable::new("TRANSCEIVER_DOM_FLAG_CLEAR_TIME");
        DomDbUtils::new().post_port_dom_flags_to_db(
            &AtomicBool::new(false),
            "Ethernet0",
            &pm,
            &hal,
            &flag,
            &count,
            &set_t,
            &clear_t,
            None,
        );
        assert_eq!(count.hget("Ethernet0", "tempHAlarm"), Some("0".into()));
        assert_eq!(set_t.hget("Ethernet0", "tempHAlarm"), Some("never".into()));
        assert_eq!(
            clear_t.hget("Ethernet0", "tempHAlarm"),
            Some("never".into())
        );
    }

    #[test]
    fn test_dom_utils_readers_none_on_err() {
        // A bare present SFP has no canned `call_json` results → flags/temperature None.
        let sfp = MockSfp::present();
        let dom = DomUtils;
        assert!(dom.get_transceiver_dom_flags(&sfp).is_none());
        assert!(dom.get_transceiver_dom_temperature(&sfp).is_none());
    }

    // ← e2e test_dom_flag_meta.py::test_dom_flag_groups_temp_and_vcc + tests/test_xcvrd.py
    // The DOM-flag reader is a PURE delegate to the platform's
    // `CmisApi.get_transceiver_dom_flags`, which decodes the module temp/vcc alarm-warning
    // group (00h:9 → tempHAlarm/vccHAlarm/…) together with the per-lane flags. The reader
    // must forward EVERY key verbatim — CMIS decode stays in Python, NO Rust-side EEPROM
    // decode — so both `tempHAlarm` AND `vccHAlarm` surface and settle to their resting
    // `False` (an earlier attempt over-rode temp/vcc from a raw `read_eeprom(9,1)`,
    // which diverged on the DUT's lower-page raw-read path and left `vccHAlarm` unsettled;
    // the faithful delegate never touches the EEPROM directly).
    #[test]
    fn test_get_transceiver_dom_flags_is_pure_delegate() {
        let sfp = MockSfp::present().with_json(
            "get_transceiver_dom_flags",
            json!({"tempHAlarm": false, "vccHAlarm": false, "rx_los": true}),
        );
        let flags = DomUtils.get_transceiver_dom_flags(&sfp).unwrap();
        let m = flags.as_object().unwrap();
        assert_eq!(m.get("tempHAlarm"), Some(&json!(false)));
        assert_eq!(m.get("vccHAlarm"), Some(&json!(false)));
        assert_eq!(m.get("rx_los"), Some(&json!(true)));
        assert_eq!(
            m.len(),
            3,
            "reader forwards the dict verbatim — no keys added or dropped"
        );
    }

    // The value row publishes the FULL flag set the platform returns (booleans stringified)
    // with a trailing timestamp — including the temp/vcc pair — so the combined temp+vcc
    // False baseline is written on the first DOM publish for every managed port.
    #[test]
    fn test_post_port_dom_flags_publishes_full_temp_vcc_group() {
        let pm = mapping_with(&[("Ethernet0", 0)]);
        let hal = MockHal::with_sfps(vec![MockSfp::present().with_json(
            "get_transceiver_dom_flags",
            json!({"tempHAlarm": false, "vccHAlarm": false}),
        )]);
        let flag = MockDbTable::new("TRANSCEIVER_DOM_FLAG");
        let count = MockDbTable::new("TRANSCEIVER_DOM_FLAG_CHANGE_COUNT");
        let set_t = MockDbTable::new("TRANSCEIVER_DOM_FLAG_SET_TIME");
        let clear_t = MockDbTable::new("TRANSCEIVER_DOM_FLAG_CLEAR_TIME");
        DomDbUtils::new().post_port_dom_flags_to_db(
            &AtomicBool::new(false),
            "Ethernet0",
            &pm,
            &hal,
            &flag,
            &count,
            &set_t,
            &clear_t,
            None,
        );
        let row: HashMap<String, String> = flag.get("Ethernet0").unwrap().into_iter().collect();
        assert_eq!(row.get("tempHAlarm").map(String::as_str), Some("False"));
        assert_eq!(row.get("vccHAlarm").map(String::as_str), Some("False"));
        assert!(row.contains_key("last_update_time"));
    }
}
