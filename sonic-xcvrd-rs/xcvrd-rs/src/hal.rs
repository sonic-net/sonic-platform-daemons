//! HAL seam (analysis §3.6) — the mockable boundary in front of the thick
//! `platform-bridge` (PyO3 → the real `sonic_platform` plugin).
//!
//! The daemon logic and posters are written against [`Hal`]/[`SfpHandle`] (as
//! `&dyn`), so production wraps `platform-bridge` ([`BridgeHal`]) while unit tests
//! inject canned values via `crate::mock::{MockHal, MockSfp}` — the Rust analogue
//! of the Python tests' `@patch('..._wrapper_get_transceiver_info', MagicMock(...))`
//! and `MagicMock()` SFP objects.
//!
//! No CMIS/SFF decode lives here: complex results are `serde_json::Value` straight
//! from the bridge; scalars are typed.

use serde_json::Value;

use crate::error::{Result, XcvrdError};

/// One transceiver change-event poll, re-exported from the bridge so the daemon
/// (and the mock) share one shape: `{sfp: {phys: code}, sfp_error: {phys: code}}`.
pub use platform_bridge::ChangeEvent;

/// The transceiver plant: `chassis.get_num_sfps()/get_sfp(i)/get_change_event(t)`.
pub trait Hal: Send + Sync {
    /// `chassis.get_num_sfps()`.
    fn num_sfps(&self) -> Result<usize>;
    /// `chassis.get_sfp(index)` → a per-module handle.
    fn sfp(&self, index: usize) -> Result<Box<dyn SfpHandle>>;
    /// `chassis.get_change_event(timeout_ms)`.
    fn get_change_event(&self, timeout_ms: u64) -> Result<ChangeEvent>;
}

/// A single transceiver slot (`SfpOptoeBase` + emulator overrides). All the
/// identity/DOM/status/VDM getters xcvrd calls, plus the `call_json` escape hatch
/// for the no-arg methods without a typed bridge wrapper (analysis §3.5 table).
pub trait SfpHandle {
    fn get_presence(&self) -> Result<bool>;
    fn is_replaceable(&self) -> Result<bool>;
    fn get_reset_status(&self) -> Result<bool>;
    fn sfp_type(&self) -> Result<String>;
    fn get_error_description(&self) -> Result<Option<String>>;
    fn get_transceiver_info(&self) -> Result<Value>;
    fn get_transceiver_dom_real_value(&self) -> Result<Value>;
    fn get_transceiver_status(&self) -> Result<Value>;
    fn get_transceiver_threshold_info(&self) -> Result<Value>;
    fn get_lpmode(&self) -> Result<bool>;
    fn set_lpmode(&self, on: bool) -> Result<bool>;
    fn reset(&self) -> Result<bool>;
    /// Optional API discovery; unavailable discovery does not enable legacy reads.
    fn has_method(&self, _method: &str) -> Result<bool> {
        Ok(false)
    }
    /// Call any no-arg Python `Sfp` method, marshalling the result as JSON
    /// (`get_transceiver_status_flags`, `get_transceiver_dom_flags`,
    /// `get_temperature`, `get_transceiver_pm`, VDM getters, …).
    fn call_json(&self, method: &str) -> Result<Value>;
    /// `SfpOptoeBase.read_eeprom(offset, num_bytes)` — read raw transceiver EEPROM at
    /// the flat linear `offset`. Returns `Ok(None)` on a plugin read miss. Used to
    /// decode the module-level DOM flag group (CMIS byte 00h:9) directly from the latched
    /// byte — so the temperature and supply-voltage flags are always published together —
    /// and to read back the CMIS staged-control-set bytes during CMIS datapath bring-up.
    fn read_eeprom(&self, offset: usize, num_bytes: usize) -> Result<Option<Vec<u8>>>;
    /// Call a no-arg method on this slot's **decode api object**
    /// (`chassis.get_sfp(i).get_xcvr_api().<method>()`), marshalled as JSON.
    ///
    /// This is the cheap seam for the CMIS decode reads. The thick bridge only exposes
    /// the *aggregate* getters (`get_transceiver_status()` / `get_transceiver_info()`),
    /// so [`crate::cmis::cmis_api::BridgeCmisApi`] used to answer every single decode
    /// read — `get_datapath_state`, `get_cmis_rev`, `is_coherent_module`, … — by
    /// re-reading a whole module. Measured on `str43-7060x6-512-c17-u10`, that is
    /// **36 ms** (`get_transceiver_status`) / **56 ms** (`get_transceiver_info`) per
    /// call, against **0–4.5 ms** for the individual read each one stands in for, and it
    /// is the entire reason CMIS bring-up took ~40 min where the Python daemon takes 80 s.
    ///
    /// The default implementation returns `Err`, so mocks (and any handle without a
    /// live plugin) transparently fall back to the aggregate-derived answers and unit
    /// tests keep exercising that path unchanged.
    fn xcvr_api_call_json(&self, _method: &str) -> Result<Value> {
        Err(XcvrdError::Bridge("xcvr api seam unavailable".to_string()))
    }
    /// `SfpOptoeBase.write_eeprom(offset, num_bytes, buf)` — write raw transceiver
    /// EEPROM at the flat linear `offset`. The CMIS manager drives the real page-10h
    /// datapath control bytes (DataPathDeinit 10h:128, OutputDisableTx 10h:130,
    /// DPConfigLane 10h:145-152, ApplyDPInitLane 10h:143) through this seam — the same
    /// register writes `CmisApi` performs; CMIS decode itself stays in Python.
    fn write_eeprom(&self, offset: usize, data: &[u8]) -> Result<bool>;
}

/// Real HAL: all reads and change events share one platform chassis and its SFP caches.
pub struct BridgeHal {
    platform: platform_bridge::Platform,
}

impl BridgeHal {
    /// Import the plugin + build the chassis (see `env::open_platform`).
    pub fn new() -> Result<Self> {
        Ok(Self::from_platform(platform_bridge::Platform::new()?))
    }

    /// Adopt an already-constructed bridge `Platform`.
    pub fn from_platform(platform: platform_bridge::Platform) -> Self {
        BridgeHal { platform }
    }
}

impl Hal for BridgeHal {
    fn num_sfps(&self) -> Result<usize> {
        Ok(self.platform.num_sfps()?)
    }

    fn sfp(&self, index: usize) -> Result<Box<dyn SfpHandle>> {
        Ok(Box::new(BridgeSfp {
            sfp: self.platform.sfp(index)?,
        }))
    }

    fn get_change_event(&self, timeout_ms: u64) -> Result<ChangeEvent> {
        Ok(self.platform.get_change_event(timeout_ms)?)
    }
}

/// Real SFP handle: thin delegation to the shared platform's SFP.
pub struct BridgeSfp {
    sfp: platform_bridge::Sfp,
}

impl SfpHandle for BridgeSfp {
    fn get_presence(&self) -> Result<bool> {
        Ok(self.sfp.get_presence()?)
    }
    fn is_replaceable(&self) -> Result<bool> {
        Ok(self.sfp.is_replaceable()?)
    }
    fn get_reset_status(&self) -> Result<bool> {
        Ok(self.sfp.get_reset_status()?)
    }
    fn sfp_type(&self) -> Result<String> {
        Ok(self.sfp.sfp_type()?)
    }
    fn get_error_description(&self) -> Result<Option<String>> {
        Ok(self.sfp.get_error_description()?)
    }
    fn get_transceiver_info(&self) -> Result<Value> {
        Ok(self.sfp.get_transceiver_info()?)
    }
    fn get_transceiver_dom_real_value(&self) -> Result<Value> {
        Ok(self.sfp.get_transceiver_dom_real_value()?)
    }
    fn get_transceiver_status(&self) -> Result<Value> {
        Ok(self.sfp.get_transceiver_status()?)
    }
    fn get_transceiver_threshold_info(&self) -> Result<Value> {
        Ok(self.sfp.get_transceiver_threshold_info()?)
    }
    fn get_lpmode(&self) -> Result<bool> {
        Ok(self.sfp.get_lpmode()?)
    }
    fn set_lpmode(&self, on: bool) -> Result<bool> {
        Ok(self.sfp.set_lpmode(on)?)
    }
    fn reset(&self) -> Result<bool> {
        Ok(self.sfp.reset()?)
    }
    fn has_method(&self, method: &str) -> Result<bool> {
        Ok(self.sfp.has_method(method)?)
    }
    fn call_json(&self, method: &str) -> Result<Value> {
        Ok(self.sfp.call_json(method, ())?)
    }
    fn xcvr_api_call_json(&self, method: &str) -> Result<Value> {
        Ok(self.sfp.xcvr_api_call_json(method)?)
    }
    fn read_eeprom(&self, offset: usize, num_bytes: usize) -> Result<Option<Vec<u8>>> {
        Ok(self.sfp.read_eeprom(offset, num_bytes)?)
    }
    fn write_eeprom(&self, offset: usize, data: &[u8]) -> Result<bool> {
        Ok(self.sfp.write_eeprom(offset, data)?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dom::utilities::db::value_to_py_str;
    use pyo3::prelude::*;
    use pyo3::types::PyDict;
    use serde_json::json;

    #[test]
    fn thresholds_follow_the_shared_sfp_decoder_after_module_replacement() {
        Python::with_gil(|py| {
            let module = PyModule::from_code_bound(
                py,
                r#"
class Decoder:
    def __init__(self, generation):
        self.generation = generation
    def thresholds(self):
        return {"temphighalarm": self.generation, "rxpowerlowalarm": float("-inf")}

class Sfp:
    def __init__(self):
        self.generation = 1
        self._xcvr_api = None
    def get_xcvr_api(self):
        if self._xcvr_api is None:
            self._xcvr_api = Decoder(self.generation)
        return self._xcvr_api
    def get_transceiver_threshold_info(self):
        return self.get_xcvr_api().thresholds()
    def get_transceiver_info(self):
        return {"generation": self.get_xcvr_api().generation}

class Chassis:
    def __init__(self):
        self.sfp = Sfp()
    def get_sfp(self, index):
        return self.sfp
    def get_change_event(self, timeout):
        self.sfp.generation += 1
        self.sfp._xcvr_api = None
        return True, {"sfp": {"0": "0"}}

class Platform:
    instances = 0
    def __init__(self):
        Platform.instances += 1
        self.chassis = Chassis()
    def get_chassis(self):
        return self.chassis
"#,
                "hal_test.py",
                "sonic_platform.platform",
            )
            .unwrap();
            let modules = py.import_bound("sys").unwrap().getattr("modules").unwrap();
            let overrides = PyDict::new_bound(py);
            overrides.set_item("sonic_platform", &module).unwrap();
            overrides
                .set_item("sonic_platform.platform", &module)
                .unwrap();
            let patch = py
                .import_bound("unittest.mock")
                .unwrap()
                .getattr("patch")
                .unwrap()
                .call_method1("dict", (modules, overrides))
                .unwrap();
            patch.call_method0("__enter__").unwrap();
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                let hal = BridgeHal::new().unwrap();
                let sfp = hal.sfp(0).unwrap();
                let thresholds = sfp.get_transceiver_threshold_info().unwrap();
                assert_eq!(thresholds["temphighalarm"], json!(1));
                assert_eq!(value_to_py_str(&thresholds["rxpowerlowalarm"]), "-inf");
                hal.get_change_event(1).unwrap();
                assert_eq!(sfp.get_transceiver_info().unwrap()["generation"], json!(2));
                assert_eq!(
                    sfp.get_transceiver_threshold_info().unwrap()["temphighalarm"],
                    json!(2)
                );
                assert_eq!(
                    module
                        .getattr("Platform")
                        .unwrap()
                        .getattr("instances")
                        .unwrap()
                        .extract::<usize>()
                        .unwrap(),
                    1
                );
            }));
            patch
                .call_method1("__exit__", (py.None(), py.None(), py.None()))
                .unwrap();
            if let Err(payload) = result {
                std::panic::resume_unwind(payload);
            }
        });
    }
}
