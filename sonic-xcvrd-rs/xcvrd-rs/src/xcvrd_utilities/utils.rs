//! `utils.py` → `XCVRDUtils` (presence / flat-memory / lpmode) over the [`Hal`] seam
//! (analysis §3.2).
//!
//! The daemon logic that already resolved a module handle (`hal.sfp(pport)`) calls the
//! free [`get_transceiver_presence`] / [`is_transceiver_flat_memory`] /
//! [`is_transceiver_lpmode_on`] helpers directly (the shared DB engine's flat-memory
//! gate and the DOM task's VDM freeze gate); [`XcvrdUtils`] is the port-index-keyed
//! wrapper mirroring the Python class shape for symmetry + unit tests.
#![allow(dead_code, unused_variables, unused_imports)]

use std::collections::BTreeMap;

use serde_json::Value;

use crate::hal::SfpHandle;

/// `XCVRDUtils.get_transceiver_presence` — `sfp.get_presence()`, any error → `false`
/// (Python catches `KeyError`/`NotImplementedError` and logs). REAL (thin delegate).
pub fn get_transceiver_presence(sfp: &dyn SfpHandle) -> bool {
    sfp.get_presence().unwrap_or(false)
}

/// `XCVRDUtils.is_transceiver_flat_memory` — is the module SFF flat-memory (no paged
/// upper memory)? The Python does `sfp.get_xcvr_api().is_flat_memory()` (defaulting to
/// `True` when there is no api or the call raises). Unusable answers also disable
/// paged-memory accesses conservatively.
pub fn is_transceiver_flat_memory(sfp: &dyn SfpHandle) -> bool {
    sfp.xcvr_api_call_json("is_flat_memory")
        .ok()
        .and_then(|v| v.as_bool())
        .unwrap_or(true)
}

/// `XCVRDUtils.is_transceiver_lpmode_on` — `sfp.get_lpmode()`, any error → `false`
/// (Python catches every `Exception`). Gates the VDM freeze (a module in low-power mode
/// is not frozen). REAL (thin delegate).
pub fn is_transceiver_lpmode_on(sfp: &dyn SfpHandle) -> bool {
    sfp.get_lpmode().unwrap_or(false)
}

/// `XCVRDUtils` — the port-index-keyed helper (mirrors the Python class over its
/// `sfp_obj_dict`). Delegates to the free helpers once the handle is resolved.
pub struct XcvrdUtils<'a> {
    sfp_obj_dict: BTreeMap<usize, &'a dyn SfpHandle>,
}

impl<'a> XcvrdUtils<'a> {
    pub fn new(sfp_obj_dict: BTreeMap<usize, &'a dyn SfpHandle>) -> Self {
        XcvrdUtils { sfp_obj_dict }
    }

    /// `get_transceiver_presence(physical_port)` — missing port → `false`.
    pub fn get_transceiver_presence(&self, physical_port: usize) -> bool {
        match self.sfp_obj_dict.get(&physical_port) {
            Some(sfp) => get_transceiver_presence(*sfp),
            None => false,
        }
    }

    /// `is_transceiver_flat_memory(physical_port)` — missing port → `true` (Python
    /// `KeyError` → `True`), otherwise the module answer.
    pub fn is_transceiver_flat_memory(&self, physical_port: usize) -> bool {
        match self.sfp_obj_dict.get(&physical_port) {
            Some(sfp) => is_transceiver_flat_memory(*sfp),
            None => true,
        }
    }

    /// `is_transceiver_lpmode_on(physical_port)` — missing port → `false`.
    pub fn is_transceiver_lpmode_on(&self, physical_port: usize) -> bool {
        match self.sfp_obj_dict.get(&physical_port) {
            Some(sfp) => is_transceiver_lpmode_on(*sfp),
            None => false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::mock::MockSfp;
    use serde_json::json;

    // ← tests/test_xcvrd.py::test_wrapper_get_presence
    #[test]
    fn test_get_transceiver_presence() {
        assert!(get_transceiver_presence(&MockSfp::present()));
        assert!(!get_transceiver_presence(&MockSfp::absent()));
    }

    #[test]
    fn test_is_transceiver_flat_memory() {
        let flat = MockSfp::present().with_api_json("is_flat_memory", json!(true));
        assert!(is_transceiver_flat_memory(&flat));
        let paged = MockSfp::present().with_api_json("is_flat_memory", json!(false));
        assert!(!is_transceiver_flat_memory(&paged));
        assert_eq!(*paged.api_call_log.lock().unwrap(), ["is_flat_memory"]);
        assert!(paged.call_log.lock().unwrap().is_empty());
    }

    #[test]
    fn unavailable_or_unusable_decoder_defaults_to_flat_memory() {
        let mut unavailable = MockSfp::present().with_json("is_flat_memory", json!(false));
        unavailable.api_calls.clear();
        assert!(is_transceiver_flat_memory(&unavailable));
        for value in [Value::Null, json!("false"), json!(0)] {
            let sfp = MockSfp::present().with_api_json("is_flat_memory", value);
            assert!(is_transceiver_flat_memory(&sfp));
        }
    }

    #[test]
    fn test_is_transceiver_lpmode_on() {
        let on = MockSfp {
            lpmode: true,
            ..MockSfp::present()
        };
        assert!(is_transceiver_lpmode_on(&on));
        // lpmode off.
        assert!(!is_transceiver_lpmode_on(&MockSfp::present()));
        // get_lpmode raises (NotImplementedError) → false.
        let err = MockSfp {
            lpmode_err: true,
            ..MockSfp::present()
        };
        assert!(!is_transceiver_lpmode_on(&err));
    }

    #[test]
    fn test_xcvrd_utils_missing_port_defaults() {
        let utils = XcvrdUtils::new(BTreeMap::new());
        assert!(!utils.get_transceiver_presence(0));
        assert!(utils.is_transceiver_flat_memory(0)); // KeyError → True
        assert!(!utils.is_transceiver_lpmode_on(0));
    }
}
