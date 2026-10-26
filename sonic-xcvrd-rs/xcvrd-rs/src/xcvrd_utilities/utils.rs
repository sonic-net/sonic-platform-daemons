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

use crate::dom::utilities::db::py_truthy;
use crate::hal::SfpHandle;

/// `XCVRDUtils.get_transceiver_presence` — `sfp.get_presence()`, any error → `false`
/// (Python catches `KeyError`/`NotImplementedError` and logs). REAL (thin delegate).
pub fn get_transceiver_presence(sfp: &dyn SfpHandle) -> bool {
    sfp.get_presence().unwrap_or(false)
}

/// `XCVRDUtils.is_transceiver_flat_memory` — is the module SFF flat-memory (no paged
/// upper memory)? The Python does `sfp.get_xcvr_api().is_flat_memory()` (defaulting to
/// `True` when there is no api or the call raises).
///
/// The `platform-bridge` cannot chain `get_xcvr_api().is_flat_memory()` (§3.5) and no
/// SONiC `SfpOptoeBase` exposes a *direct* `is_flat_memory` (it lives on the chained
/// `CmisApi`/`Sff8636Api`), so `call_json("is_flat_memory")` succeeds only on a mock/
/// bridge that opts in and otherwise errors. On the error path we return **`false`**
/// (paged), NOT the Python source's `True`, because that reproduces the reference
/// daemon's *runtime* behaviour on this platform: every module the emulator/DUT serves
/// is CMIS/SFF-8636 (paged), whose real `is_flat_memory()` returns `False`, so the
/// reference daemon publishes VDM/PM for them. Returning `false` here keeps that parity
/// (VDM/PM proceed); a genuinely flat module still yields empty VDM/PM dicts upstream,
/// so nothing spurious is published. A mock/bridge that *does* answer `is_flat_memory`
/// is honoured verbatim (so the `flat==true` skip path stays testable).
pub fn is_transceiver_flat_memory(sfp: &dyn SfpHandle) -> bool {
    match sfp.call_json("is_flat_memory") {
        Ok(v) => py_truthy(&v),
        Err(_) => false,
    }
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

    // ← tests/test_xcvrd.py::test_wrapper_is_flat_memory (behaviourally): a module that
    // answers `is_flat_memory` is honoured; the un-answerable bridge path → paged (false).
    #[test]
    fn test_is_transceiver_flat_memory() {
        // Module reports flat memory → true.
        let flat = MockSfp::present().with_json("is_flat_memory", json!(true));
        assert!(is_transceiver_flat_memory(&flat));
        // Module reports paged → false.
        let paged = MockSfp::present().with_json("is_flat_memory", json!(false));
        assert!(!is_transceiver_flat_memory(&paged));
        // No answer (bridge can't chain get_xcvr_api) → paged (false), so VDM/PM proceed.
        assert!(!is_transceiver_flat_memory(&MockSfp::present()));
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
