//! `dom/utilities/vdm/{utils,db_utils}.py` → `VDMUtils` (VDM getters + freeze/unfreeze
//! context) + `VDMDBUtils` (→ `TRANSCEIVER_VDM_*` real/threshold/flag) (analysis §3.2).
//! The Python `contextlib` freeze context becomes an RAII [`VdmFreezeGuard`].
//! VDM diagnostics helpers.
#![allow(dead_code, unused_variables, unused_imports)]

use std::collections::HashMap;
use std::sync::atomic::AtomicBool;
use std::thread::sleep;
use std::time::{Duration, Instant};

use serde_json::{Map, Value};

use crate::db::DbTable;
use crate::dom::utilities::db::{py_truthy, value_to_py_str, DbUtils, Fvs};
use crate::hal::{Hal, SfpHandle};
use crate::xcvrd_utilities::port_event_helper::PortMapping;
use crate::xcvrd_utilities::xcvr_table_helper::{XcvrTableHelper, VDM_THRESHOLD_TYPES};

/// `MAX_tVDMF_TIME_MSECS` (vdm/utils.py) — post-action settle before polling the done bit.
pub const MAX_TVDMF_TIME_MSECS: u64 = 10;
/// `MAX_VDM_FREEZE_UNFREEZE_TIME_MSECS` (vdm/utils.py) — freeze/unfreeze confirm timeout.
pub const MAX_VDM_FREEZE_UNFREEZE_TIME_MSECS: u64 = 1000;
/// `FREEZE_UNFREEZE_DONE_POLLING_INTERVAL_MSECS` (vdm/utils.py) — done-bit poll interval.
pub const FREEZE_UNFREEZE_DONE_POLLING_INTERVAL_MSECS: u64 = 1;

/// Per-pass VDM threshold/flag cache: `physical_port → {threshold_type → {key → value}}`.
/// Mirrors the Python `db_cache[physical_port] = vdm_threshold_type_value_dict` so a
/// breakout group re-uses one EEPROM read (and one metadata update) across its subports.
pub type VdmThresholdCache = HashMap<usize, HashMap<String, Map<String, Value>>>;

/// `VDMUtils` — reads the module VDM dicts + drives the freeze/unfreeze confirm loop.
///
/// The freeze/unfreeze timing (settle / timeout / poll) is held as fields so unit tests
/// can shrink the 1 s confirm timeout via [`VdmUtils::with_timing`]; production uses the
/// real `MAX_*`/`FREEZE_*` constants through [`VdmUtils::new`].
#[derive(Clone, Copy)]
pub struct VdmUtils {
    settle: Duration,
    timeout: Duration,
    poll: Duration,
}

impl VdmUtils {
    pub fn new() -> Self {
        VdmUtils {
            settle: Duration::from_millis(MAX_TVDMF_TIME_MSECS),
            timeout: Duration::from_millis(MAX_VDM_FREEZE_UNFREEZE_TIME_MSECS),
            poll: Duration::from_millis(FREEZE_UNFREEZE_DONE_POLLING_INTERVAL_MSECS),
        }
    }

    /// Construct with explicit freeze/unfreeze timing (tests use a tiny timeout so the
    /// "status never confirms" path returns immediately instead of after 1 s).
    pub fn with_timing(settle: Duration, timeout: Duration, poll: Duration) -> Self {
        VdmUtils {
            settle,
            timeout,
            poll,
        }
    }

    /// `is_transceiver_vdm_supported` — `sfp.is_transceiver_vdm_supported()`;
    /// `NotImplementedError` → `false`.
    pub fn is_transceiver_vdm_supported(&self, sfp: &dyn SfpHandle) -> bool {
        match sfp.call_json("is_transceiver_vdm_supported") {
            Ok(v) => py_truthy(&v),
            Err(_) => false,
        }
    }

    /// `is_vdm_statistic_supported` — `sfp.is_vdm_statistic_supported()`;
    /// `NotImplementedError`/`AttributeError` → `false`.
    pub fn is_vdm_statistic_supported(&self, sfp: &dyn SfpHandle) -> bool {
        match sfp.call_json("is_vdm_statistic_supported") {
            Ok(v) => py_truthy(&v),
            Err(_) => false,
        }
    }

    /// `get_vdm_real_values_basic` — `sfp.get_transceiver_vdm_real_value_basic()`; a
    /// not-implemented/errored read yields `None` (the poster treats `None`/empty alike).
    pub fn get_vdm_real_values_basic(&self, sfp: &dyn SfpHandle) -> Option<Value> {
        sfp.call_json("get_transceiver_vdm_real_value_basic").ok()
    }

    /// `get_vdm_real_values_statistic` — `sfp.get_transceiver_vdm_real_value_statistic()`
    /// (the frozen snapshot). `None` on a not-implemented/errored read.
    pub fn get_vdm_real_values_statistic(&self, sfp: &dyn SfpHandle) -> Option<Value> {
        sfp.call_json("get_transceiver_vdm_real_value_statistic")
            .ok()
    }

    /// `get_vdm_flags` — `sfp.get_transceiver_vdm_flags()` (COR latched flags). `None` on
    /// a not-implemented/errored read.
    pub fn get_vdm_flags(&self, sfp: &dyn SfpHandle) -> Option<Value> {
        sfp.call_json("get_transceiver_vdm_flags").ok()
    }

    /// `get_vdm_thresholds` — `sfp.get_transceiver_vdm_thresholds()`. `None` on a
    /// not-implemented/errored read.
    pub fn get_vdm_thresholds(&self, sfp: &dyn SfpHandle) -> Option<Value> {
        sfp.call_json("get_transceiver_vdm_thresholds").ok()
    }

    /// `_vdm_action_and_confirm` — run a freeze/unfreeze `action`, then poll its
    /// done-status until it confirms or the timeout elapses.
    ///
    /// `action_method`/`status_method` are the no-arg SFP methods (`freeze_vdm_stats` +
    /// `get_vdm_freeze_status`, or the unfreeze pair). Returns `false` if the action
    /// fails, if the status never confirms within [`Self::timeout`], or on any
    /// `KeyError`/`NotImplementedError` (a missing/erroring bridge call).
    pub fn vdm_action_and_confirm(
        &self,
        sfp: &dyn SfpHandle,
        action_method: &str,
        status_method: &str,
        action_name: &str,
    ) -> bool {
        // status = action(); a falsy result or a raise → False.
        let status = match sfp.call_json(action_method) {
            Ok(v) => v,
            Err(_) => return false,
        };
        if !py_truthy(&status) {
            return false;
        }
        // Wait MAX_tVDMF for the module to clear the done bit, then poll for it.
        sleep(self.settle);
        let start = Instant::now();
        while start.elapsed() < self.timeout {
            match sfp.call_json(status_method) {
                Ok(v) => {
                    if py_truthy(&v) {
                        return true;
                    }
                }
                Err(_) => return false,
            }
            sleep(self.poll);
        }
        false
    }

    /// `_freeze_vdm_stats_and_confirm` — freeze + confirm the freeze done bit.
    pub fn freeze_vdm_stats_and_confirm(&self, sfp: &dyn SfpHandle) -> bool {
        self.vdm_action_and_confirm(sfp, "freeze_vdm_stats", "get_vdm_freeze_status", "freeze")
    }

    /// `_unfreeze_vdm_stats_and_confirm` — unfreeze + confirm the unfreeze done bit.
    pub fn unfreeze_vdm_stats_and_confirm(&self, sfp: &dyn SfpHandle) -> bool {
        self.vdm_action_and_confirm(
            sfp,
            "unfreeze_vdm_stats",
            "get_vdm_unfreeze_status",
            "unfreeze",
        )
    }

    /// `vdm_freeze_context(sfp)` — RAII replacement for the Python `@contextmanager`:
    /// freezes VDM stats now (recording whether it confirmed via
    /// [`VdmFreezeGuard::is_frozen`]) and **always** attempts unfreeze on drop.
    pub fn vdm_freeze_context<'a>(&self, sfp: &'a dyn SfpHandle) -> VdmFreezeGuard<'a> {
        let frozen = self.freeze_vdm_stats_and_confirm(sfp);
        if !frozen {
            eprintln!("xcvrd-rs: Failed to freeze VDM stats in contextmanager");
        }
        VdmFreezeGuard {
            sfp,
            utils: *self,
            frozen,
        }
    }
}

impl Default for VdmUtils {
    fn default() -> Self {
        VdmUtils::new()
    }
}

/// RAII guard: freeze-confirmed on construction, `unfreeze_vdm_stats()` + confirm on drop
/// (mirrors the Python `@contextmanager vdm_freeze_context` `finally` unfreeze). The
/// caller reads [`Self::is_frozen`] to decide whether to capture the frozen statistic
/// snapshot (`with vdm_freeze_context(...) as vdm_frozen: if vdm_frozen: …`).
pub struct VdmFreezeGuard<'a> {
    sfp: &'a dyn SfpHandle,
    utils: VdmUtils,
    frozen: bool,
}

impl<'a> VdmFreezeGuard<'a> {
    /// Did the freeze confirm? (The Python `yield True/False`.)
    pub fn is_frozen(&self) -> bool {
        self.frozen
    }
}

impl<'a> Drop for VdmFreezeGuard<'a> {
    fn drop(&mut self) {
        if !self.utils.unfreeze_vdm_stats_and_confirm(self.sfp) {
            eprintln!("xcvrd-rs: Failed to unfreeze VDM stats in contextmanager");
        }
    }
}

/// `VDMDBUtils` — posts the VDM tables (subclass of the shared [`DbUtils`] engine).
pub struct VdmDbUtils {
    base: DbUtils,
    vdm_utils: VdmUtils,
}

impl VdmDbUtils {
    pub fn new() -> Self {
        VdmDbUtils {
            base: DbUtils::new(),
            vdm_utils: VdmUtils::new(),
        }
    }

    /// `post_port_vdm_real_values_from_dict_to_db` → `TRANSCEIVER_VDM_REAL_VALUE`. Posts a
    /// pre-merged (basic + statistic) real-value dict in one row with a single trailing
    /// `last_update_time`. Validates the port (flat-memory gated), skips a `None`/empty
    /// dict, and beautifies (`str()`) before writing.
    pub fn post_port_vdm_real_values_from_dict_to_db(
        &self,
        stop: &AtomicBool,
        logical_port_name: &str,
        port_mapping: &PortMapping,
        hal: &dyn Hal,
        table: &dyn DbTable,
        vdm_real_values: &Value,
    ) {
        // This call is mainly to perform basic validation of the port.
        if self
            .base
            .validate_and_get_physical_port(stop, logical_port_name, port_mapping, hal, true)
            .is_none()
        {
            return;
        }
        if port_mapping
            .get_asic_id_for_logical_port(logical_port_name)
            .is_none()
        {
            eprintln!(
                "xcvrd-rs: Post port vdm real values from dict to db failed for \
                 {logical_port_name} as no asic index found"
            );
            return;
        }
        // `if not vdm_real_values_dict: return` — None / empty / non-object → nothing.
        let mut obj = match vdm_real_values {
            Value::Object(o) if !o.is_empty() => o.clone(),
            _ => return,
        };
        self.base.beautify_info_dict(&mut obj);
        let mut fvs: Fvs = obj
            .iter()
            .map(|(k, v)| (k.clone(), value_to_py_str(v)))
            .collect();
        fvs.push(("last_update_time".to_string(), self.base.get_current_time()));
        table.set(logical_port_name, &fvs);
    }

    /// `post_port_vdm_flags_to_db` → the per-type `TRANSCEIVER_VDM_*_FLAG` tables (+ their
    /// change-count / set-time / clear-time metadata). Flags are COR (Clear On Read).
    pub fn post_port_vdm_flags_to_db(
        &self,
        stop: &AtomicBool,
        logical_port_name: &str,
        port_mapping: &PortMapping,
        hal: &dyn Hal,
        table_helper: &XcvrTableHelper,
        db_cache: Option<&mut VdmThresholdCache>,
    ) {
        self.post_vdm_thresholds_or_flags(
            stop,
            logical_port_name,
            port_mapping,
            hal,
            table_helper,
            true,
            db_cache,
        );
    }

    /// `post_port_vdm_thresholds_to_db` → the four
    /// `TRANSCEIVER_VDM_{HALARM,LALARM,HWARN,LWARN}_THRESHOLD` tables (seeded at boot /
    /// insert, like the DOM thresholds).
    pub fn post_port_vdm_thresholds_to_db(
        &self,
        stop: &AtomicBool,
        logical_port_name: &str,
        port_mapping: &PortMapping,
        hal: &dyn Hal,
        table_helper: &XcvrTableHelper,
        db_cache: Option<&mut VdmThresholdCache>,
    ) {
        self.post_vdm_thresholds_or_flags(
            stop,
            logical_port_name,
            port_mapping,
            hal,
            table_helper,
            false,
            db_cache,
        );
    }

    /// `_post_port_vdm_thresholds_or_flags_to_db` — the shared threshold/flag poster.
    ///
    /// Reads one VDM dict off the module (whose keys embed the threshold family, e.g.
    /// `laser_temperature_media_1_halarm`), splits it per family into
    /// `TRANSCEIVER_VDM_<TYPE>_{THRESHOLD,FLAG}` rows (stripping the `_<type>` suffix from
    /// each key), and — for flags — updates the change-count/set-time/clear-time metadata
    /// **before** overwriting the value row. Honors the per-pass `db_cache` (built + the
    /// metadata updated only on a cache miss). The posting loop mirrors the Python
    /// `else: return`: it stops at the first empty family bucket.
    #[allow(clippy::too_many_arguments)]
    fn post_vdm_thresholds_or_flags(
        &self,
        stop: &AtomicBool,
        logical_port_name: &str,
        port_mapping: &PortMapping,
        hal: &dyn Hal,
        table_helper: &XcvrTableHelper,
        flag_data: bool,
        db_cache: Option<&mut VdmThresholdCache>,
    ) {
        let physical_port = match self.base.validate_and_get_physical_port(
            stop,
            logical_port_name,
            port_mapping,
            hal,
            true,
        ) {
            Some(p) => p,
            None => return,
        };
        let Some(asic_id) = port_mapping.get_asic_id_for_logical_port(logical_port_name) else {
            return;
        };

        let mut db_cache = db_cache;

        // Resolve the per-family split dict: from the cache on a hit, else a fresh EEPROM
        // read (which also runs the flag metadata update + seeds the cache), mirroring the
        // Python cache-miss branch.
        let split: HashMap<String, Map<String, Value>> = if let Some(cached) = db_cache
            .as_ref()
            .and_then(|c| c.get(&physical_port).cloned())
        {
            cached
        } else {
            let sfp = match hal.sfp(physical_port) {
                Ok(s) => s,
                Err(_) => return,
            };
            let raw = if flag_data {
                self.vdm_utils.get_vdm_flags(&*sfp)
            } else {
                self.vdm_utils.get_vdm_thresholds(&*sfp)
            };
            // `if vdm_values_dict is None: return` (a not-implemented/errored read).
            let raw_obj = match raw {
                Some(Value::Object(o)) => o,
                _ => return,
            };
            let update_time = self.base.get_current_time();

            let mut split: HashMap<String, Map<String, Value>> = HashMap::new();
            for t in VDM_THRESHOLD_TYPES {
                split.insert(t.to_string(), Map::new());
            }
            for (key, value) in raw_obj.iter() {
                for t in VDM_THRESHOLD_TYPES {
                    let suffix = format!("_{t}");
                    if key.contains(&suffix) {
                        let new_key = key.replace(&suffix, "");
                        split
                            .get_mut(t)
                            .expect("family bucket seeded")
                            .insert(new_key, value.clone());
                    }
                }
            }

            // Flag update: maintain the change-count/set-time/clear-time metadata for each
            // non-empty family before the value row is overwritten below.
            if flag_data {
                for t in VDM_THRESHOLD_TYPES {
                    let bucket = &split[t];
                    if !bucket.is_empty() {
                        self.base.update_flag_metadata_tables(
                            logical_port_name,
                            bucket,
                            &update_time,
                            table_helper.get_vdm_flag_tbl(asic_id, t),
                            table_helper.get_vdm_flag_change_count_tbl(asic_id, t),
                            table_helper.get_vdm_flag_set_time_tbl(asic_id, t),
                            table_helper.get_vdm_flag_clear_time_tbl(asic_id, t),
                            &format!("VDM {t}"),
                        );
                    }
                }
            }

            if let Some(cache) = db_cache.as_mut() {
                cache.insert(physical_port, split.clone());
            }
            split
        };

        // Post each non-empty family; stop at the first empty one (Python `else: return`).
        for t in VDM_THRESHOLD_TYPES {
            let bucket = split.get(t).cloned().unwrap_or_default();
            if bucket.is_empty() {
                return;
            }
            let mut b = bucket;
            self.base.beautify_info_dict(&mut b);
            let mut fvs: Fvs = b
                .iter()
                .map(|(k, v)| (k.clone(), value_to_py_str(v)))
                .collect();
            fvs.push(("last_update_time".to_string(), self.base.get_current_time()));
            let table = if flag_data {
                table_helper.get_vdm_flag_tbl(asic_id, t)
            } else {
                table_helper.get_vdm_threshold_tbl(asic_id, t)
            };
            table.set(logical_port_name, &fvs);
        }
    }
}

impl Default for VdmDbUtils {
    fn default() -> Self {
        VdmDbUtils::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::mock::{MockDbTable, MockHal, MockSfp};
    use crate::xcvrd_utilities::port_event_helper::{build_port_mapping, PortConfigRow};
    use crate::xcvrd_utilities::xcvr_table_helper::XcvrTableHelper;
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

    /// Fast freeze/unfreeze timing so the "status never confirms" case returns in a few ms
    /// rather than the real 1 s.
    fn fast_vdm() -> VdmUtils {
        VdmUtils::with_timing(
            Duration::from_millis(0),
            Duration::from_millis(5),
            Duration::from_millis(1),
        )
    }

    // ← tests/test_xcvrd.py::test_wrapper_is_transceiver_vdm_supported
    #[test]
    fn test_is_transceiver_vdm_supported() {
        let v = VdmUtils::new();
        assert!(v.is_transceiver_vdm_supported(
            &MockSfp::present().with_json("is_transceiver_vdm_supported", json!(true))
        ));
        assert!(!v.is_transceiver_vdm_supported(
            &MockSfp::present().with_json("is_transceiver_vdm_supported", json!(false))
        ));
        // NotImplementedError (no canned result) → false.
        assert!(!v.is_transceiver_vdm_supported(&MockSfp::present()));
    }

    // ← tests/test_xcvrd.py::test_is_vdm_statistic_supported
    #[test]
    fn test_is_vdm_statistic_supported() {
        let v = VdmUtils::new();
        assert!(v.is_vdm_statistic_supported(
            &MockSfp::present().with_json("is_vdm_statistic_supported", json!(true))
        ));
        assert!(!v.is_vdm_statistic_supported(
            &MockSfp::present().with_json("is_vdm_statistic_supported", json!(false))
        ));
        // NotImplementedError / AttributeError → false.
        assert!(!v.is_vdm_statistic_supported(&MockSfp::present()));
    }

    // ← tests/test_xcvrd.py::test_get_vdm_real_values_basic
    #[test]
    fn test_get_vdm_real_values_basic() {
        let v = VdmUtils::new();
        let sfp = MockSfp::present().with_json(
            "get_transceiver_vdm_real_value_basic",
            json!({"basic_key": "basic_value"}),
        );
        assert_eq!(
            v.get_vdm_real_values_basic(&sfp),
            Some(json!({"basic_key": "basic_value"}))
        );
        // empty dict is returned as-is.
        let empty = MockSfp::present().with_json("get_transceiver_vdm_real_value_basic", json!({}));
        assert_eq!(v.get_vdm_real_values_basic(&empty), Some(json!({})));
        // NotImplementedError / AttributeError → None (treated as {}).
        assert_eq!(v.get_vdm_real_values_basic(&MockSfp::present()), None);
    }

    // ← tests/test_xcvrd.py::test_get_vdm_real_values_statistic
    #[test]
    fn test_get_vdm_real_values_statistic() {
        let v = VdmUtils::new();
        let sfp = MockSfp::present().with_json(
            "get_transceiver_vdm_real_value_statistic",
            json!({"stat_key": "stat_value"}),
        );
        assert_eq!(
            v.get_vdm_real_values_statistic(&sfp),
            Some(json!({"stat_key": "stat_value"}))
        );
        assert_eq!(v.get_vdm_real_values_statistic(&MockSfp::present()), None);
    }

    // ← tests/test_xcvrd.py::test_get_vdm_flags
    #[test]
    fn test_get_vdm_flags() {
        let v = VdmUtils::new();
        let sfp = MockSfp::present().with_json("get_transceiver_vdm_flags", json!(true));
        assert_eq!(v.get_vdm_flags(&sfp), Some(json!(true)));
        let empty = MockSfp::present().with_json("get_transceiver_vdm_flags", json!({}));
        assert_eq!(v.get_vdm_flags(&empty), Some(json!({})));
        assert_eq!(v.get_vdm_flags(&MockSfp::present()), None);
    }

    // ← tests/test_xcvrd.py::test_get_vdm_thresholds
    #[test]
    fn test_get_vdm_thresholds() {
        let v = VdmUtils::new();
        let sfp = MockSfp::present().with_json("get_transceiver_vdm_thresholds", json!(true));
        assert_eq!(v.get_vdm_thresholds(&sfp), Some(json!(true)));
        let empty = MockSfp::present().with_json("get_transceiver_vdm_thresholds", json!({}));
        assert_eq!(v.get_vdm_thresholds(&empty), Some(json!({})));
        assert_eq!(v.get_vdm_thresholds(&MockSfp::present()), None);
    }

    // ← tests/test_xcvrd.py::test_vdm_action_and_confirm (parametrized) + _exception
    #[test]
    fn test_vdm_action_and_confirm() {
        let v = fast_vdm();
        // action ok + status confirms → true.
        let ok = MockSfp::present()
            .with_json("freeze_vdm_stats", json!(true))
            .with_json("get_vdm_freeze_status", json!(true));
        assert!(v.vdm_action_and_confirm(
            &ok,
            "freeze_vdm_stats",
            "get_vdm_freeze_status",
            "freeze"
        ));

        // action ok but status never confirms → false (after the tiny timeout).
        let never = MockSfp::present()
            .with_json("freeze_vdm_stats", json!(true))
            .with_json("get_vdm_freeze_status", json!(false));
        assert!(!v.vdm_action_and_confirm(
            &never,
            "freeze_vdm_stats",
            "get_vdm_freeze_status",
            "freeze"
        ));

        // action returns falsy → false immediately.
        let failed = MockSfp::present().with_json("freeze_vdm_stats", json!(false));
        assert!(!v.vdm_action_and_confirm(
            &failed,
            "freeze_vdm_stats",
            "get_vdm_freeze_status",
            "freeze"
        ));

        // action raises (no canned result → KeyError/NotImplementedError) → false.
        assert!(!v.vdm_action_and_confirm(
            &MockSfp::present(),
            "freeze_vdm_stats",
            "get_vdm_freeze_status",
            "freeze"
        ));
    }

    #[test]
    fn test_vdm_freeze_context_guard_unfreezes_on_drop() {
        let sfp = MockSfp::present()
            .with_json("freeze_vdm_stats", json!(true))
            .with_json("get_vdm_freeze_status", json!(true))
            .with_json("unfreeze_vdm_stats", json!(true))
            .with_json("get_vdm_unfreeze_status", json!(true));
        let log = sfp.call_log.clone();
        {
            let guard = fast_vdm().vdm_freeze_context(&sfp);
            assert!(guard.is_frozen());
            // Only freeze issued so far.
            assert!(log.lock().unwrap().iter().any(|m| m == "freeze_vdm_stats"));
            assert!(!log
                .lock()
                .unwrap()
                .iter()
                .any(|m| m == "unfreeze_vdm_stats"));
        }
        // Drop ran the unfreeze.
        assert!(log
            .lock()
            .unwrap()
            .iter()
            .any(|m| m == "unfreeze_vdm_stats"));
    }

    // ← tests/test_xcvrd.py::test_post_port_vdm_real_values_from_dict_to_db
    #[test]
    fn test_post_port_vdm_real_values_from_dict_to_db() {
        // 16-key merged dict: 8 laser_temperature_media (38 or "N/A") + 8 esnr floats.
        let mut m = Map::new();
        for i in 1..=8 {
            m.insert(
                format!("laser_temperature_media{i}"),
                if i <= 4 { json!(38) } else { json!("N/A") },
            );
        }
        for i in 1..=8 {
            m.insert(format!("esnr_media_input{i}"), json!(23.1171875));
        }
        let vdm_real_values = Value::Object(m);

        let pm = mapping_with(&[("Ethernet0", 0)]);
        let tbl = MockDbTable::new("TRANSCEIVER_VDM_REAL_VALUE");
        let vdm_db = VdmDbUtils::new();
        let stop = AtomicBool::new(false);

        // asic_index None → nothing posted (no port in a bogus mapping / stop set path is
        // covered by validate). Here validate fails when the module is absent:
        let absent = MockHal::with_sfps(vec![MockSfp::absent()]);
        vdm_db.post_port_vdm_real_values_from_dict_to_db(
            &stop,
            "Ethernet0",
            &pm,
            &absent,
            &tbl,
            &vdm_real_values,
        );
        assert_eq!(tbl.get_size(), 0);

        let hal = MockHal::with_sfps(vec![MockSfp::present()]);
        // None / empty dict → nothing posted.
        vdm_db.post_port_vdm_real_values_from_dict_to_db(
            &stop,
            "Ethernet0",
            &pm,
            &hal,
            &tbl,
            &Value::Null,
        );
        vdm_db.post_port_vdm_real_values_from_dict_to_db(
            &stop,
            "Ethernet0",
            &pm,
            &hal,
            &tbl,
            &json!({}),
        );
        assert_eq!(tbl.get_size(), 0);

        // Valid dict → 16 fields + last_update_time == 17.
        vdm_db.post_port_vdm_real_values_from_dict_to_db(
            &stop,
            "Ethernet0",
            &pm,
            &hal,
            &tbl,
            &vdm_real_values,
        );
        assert_eq!(tbl.get_size_for_key("Ethernet0"), 17);
        let row: HashMap<String, String> = tbl.get("Ethernet0").unwrap().into_iter().collect();
        assert_eq!(
            row.get("laser_temperature_media1").map(String::as_str),
            Some("38")
        );
        assert_eq!(
            row.get("laser_temperature_media5").map(String::as_str),
            Some("N/A")
        );
        assert_eq!(
            row.get("esnr_media_input1").map(String::as_str),
            Some("23.1171875")
        );
    }

    // ← tests/test_xcvrd.py::test_post_port_vdm_thresholds_to_db
    #[test]
    fn test_post_port_vdm_thresholds_to_db() {
        // 32-key dict: 8 keys per family (halarm/lalarm/hwarn/lwarn).
        fn thresholds() -> Value {
            let mut m = Map::new();
            for i in 1..=8 {
                m.insert(format!("laser_temperature_media_{i}_halarm"), json!(90.0));
                m.insert(format!("laser_temperature_media_{i}_lalarm"), json!(-5.0));
                m.insert(format!("laser_temperature_media_{i}_hwarn"), json!(85.0));
                m.insert(format!("laser_temperature_media_{i}_lwarn"), json!(0.0));
            }
            Value::Object(m)
        }
        let pm = mapping_with(&[("Ethernet0", 0)]);
        let vdm_db = VdmDbUtils::new();

        // stop set → nothing.
        let stop_set = AtomicBool::new(true);
        let th = XcvrTableHelper::with_mock_tables(&[String::new()]);
        let hal = MockHal::with_sfps(vec![
            MockSfp::present().with_json("get_transceiver_vdm_thresholds", thresholds())
        ]);
        vdm_db.post_port_vdm_thresholds_to_db(&stop_set, "Ethernet0", &pm, &hal, &th, None);
        for t in VDM_THRESHOLD_TYPES {
            assert_eq!(th.get_vdm_threshold_tbl(0, t).get_size(), 0);
        }

        let stop = AtomicBool::new(false);

        // not present → nothing.
        let th = XcvrTableHelper::with_mock_tables(&[String::new()]);
        let absent = MockHal::with_sfps(vec![
            MockSfp::absent().with_json("get_transceiver_vdm_thresholds", thresholds())
        ]);
        vdm_db.post_port_vdm_thresholds_to_db(&stop, "Ethernet0", &pm, &absent, &th, None);
        for t in VDM_THRESHOLD_TYPES {
            assert_eq!(th.get_vdm_threshold_tbl(0, t).get_size(), 0);
        }

        // flat memory → nothing.
        let th = XcvrTableHelper::with_mock_tables(&[String::new()]);
        let flat = MockHal::with_sfps(vec![MockSfp::present()
            .with_json("is_flat_memory", json!(true))
            .with_json("get_transceiver_vdm_thresholds", thresholds())]);
        vdm_db.post_port_vdm_thresholds_to_db(&stop, "Ethernet0", &pm, &flat, &th, None);
        for t in VDM_THRESHOLD_TYPES {
            assert_eq!(th.get_vdm_threshold_tbl(0, t).get_size(), 0);
        }

        // get_vdm_thresholds returns None (no canned result) → nothing.
        let th = XcvrTableHelper::with_mock_tables(&[String::new()]);
        let none = MockHal::with_sfps(vec![MockSfp::present()]);
        vdm_db.post_port_vdm_thresholds_to_db(&stop, "Ethernet0", &pm, &none, &th, None);
        for t in VDM_THRESHOLD_TYPES {
            assert_eq!(th.get_vdm_threshold_tbl(0, t).get_size(), 0);
        }

        // Valid → each family table gets 8 + last_update_time == 9. db_cache populated.
        let th = XcvrTableHelper::with_mock_tables(&[String::new()]);
        let hal = MockHal::with_sfps(vec![
            MockSfp::present().with_json("get_transceiver_vdm_thresholds", thresholds())
        ]);
        let mut cache: VdmThresholdCache = VdmThresholdCache::new();
        vdm_db.post_port_vdm_thresholds_to_db(&stop, "Ethernet0", &pm, &hal, &th, Some(&mut cache));
        for t in VDM_THRESHOLD_TYPES {
            assert_eq!(
                th.get_vdm_threshold_tbl(0, t).get_size_for_key("Ethernet0"),
                9
            );
        }
        assert!(cache.contains_key(&0));
        // Re-post from the cache → still 9 per family.
        vdm_db.post_port_vdm_thresholds_to_db(&stop, "Ethernet0", &pm, &hal, &th, Some(&mut cache));
        for t in VDM_THRESHOLD_TYPES {
            assert_eq!(
                th.get_vdm_threshold_tbl(0, t).get_size_for_key("Ethernet0"),
                9
            );
            // Sanity: the halarm value was stripped of its `_halarm` suffix + stringified.
            assert_eq!(
                th.get_vdm_threshold_tbl(0, "halarm")
                    .hget("Ethernet0", "laser_temperature_media_1"),
                Some("90.0".to_string())
            );
        }
    }

    #[test]
    fn test_post_port_vdm_flags_to_db_seeds_metadata_and_posts() {
        // A single-family flag dict → the halarm flag table + its metadata are seeded.
        let pm = mapping_with(&[("Ethernet0", 0)]);
        let th = XcvrTableHelper::with_mock_tables(&[String::new()]);
        let hal = MockHal::with_sfps(vec![MockSfp::present().with_json(
            "get_transceiver_vdm_flags",
            json!({
                "laser_temperature_media_1_halarm": true,
                "laser_temperature_media_2_halarm": false,
            }),
        )]);
        let vdm_db = VdmDbUtils::new();
        vdm_db.post_port_vdm_flags_to_db(
            &AtomicBool::new(false),
            "Ethernet0",
            &pm,
            &hal,
            &th,
            None,
        );
        // Value row: 2 flags + last_update_time == 3.
        assert_eq!(
            th.get_vdm_flag_tbl(0, "halarm")
                .get_size_for_key("Ethernet0"),
            3
        );
        assert_eq!(
            th.get_vdm_flag_tbl(0, "halarm")
                .hget("Ethernet0", "laser_temperature_media_1"),
            Some("True".to_string())
        );
        // First publish seeds metadata: count 0, times "never".
        assert_eq!(
            th.get_vdm_flag_change_count_tbl(0, "halarm")
                .hget("Ethernet0", "laser_temperature_media_1"),
            Some("0".to_string())
        );
        assert_eq!(
            th.get_vdm_flag_set_time_tbl(0, "halarm")
                .hget("Ethernet0", "laser_temperature_media_1"),
            Some("never".to_string())
        );
        // Other families stay empty (posting stops at the first empty bucket).
        assert_eq!(th.get_vdm_flag_tbl(0, "lalarm").get_size(), 0);
    }
}
