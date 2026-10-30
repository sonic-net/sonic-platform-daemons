//! Mockable daemon HAL over sonic-platform-common's generated platform API.
//! Platform access and Python marshalling belong to the shared provider; this
//! adapter translates DeviceKeys and declared operations into the daemon seams.

use std::collections::BTreeMap;

use platform_api::{DeviceKey, PlatformApi};
use platform_provider::Platform;
use serde_json::Value;

use crate::error::{Result, XcvrdError};

/// The transceiver groups from a shared platform change-event batch.
#[derive(Debug, Clone, Default)]
pub struct ChangeEvent {
    pub status: bool,
    pub sfp: BTreeMap<String, String>,
    pub sfp_error: BTreeMap<String, String>,
}

/// The transceiver plant: `chassis.get_num_sfps()/get_sfp(i)/get_change_event(t)`.
pub trait Hal: Send + Sync {
    /// `chassis.get_num_sfps()`.
    fn num_sfps(&self) -> Result<usize>;
    /// `chassis.get_sfp(index)` → a per-module handle.
    fn sfp(&self, index: usize) -> Result<Box<dyn SfpHandle>>;
    /// `chassis.get_change_event(timeout_ms)`.
    fn get_change_event(&self, timeout_ms: u64) -> Result<ChangeEvent>;
}

/// A single transceiver slot, backed by a DeviceKey in the shared API.
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
    /// Read one of the declared status/DOM/PM/VDM operations as JSON.
    fn call_json(&self, method: &str) -> Result<Value>;
    /// `SfpOptoeBase.read_eeprom(offset, num_bytes)` — read raw transceiver EEPROM at
    /// the flat linear `offset`. Returns `Ok(None)` on a plugin read miss. Used to
    /// decode the module-level DOM flag group (CMIS byte 00h:9) directly from the latched
    /// byte — so the temperature and supply-voltage flags are always published together —
    /// and to read back the CMIS staged-control-set bytes during CMIS datapath bring-up.
    fn read_eeprom(&self, offset: usize, num_bytes: usize) -> Result<Option<Vec<u8>>>;
    /// A declared direct decoder read, avoiding an expensive aggregate read.
    /// Mocks without this seam retain the aggregate-derived fallback.
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
pub struct PlatformHal {
    platform: Platform,
}

impl PlatformHal {
    /// Import the plugin + build the chassis (see `env::open_platform`).
    pub fn new() -> Result<Self> {
        Ok(Self::from_platform(crate::env::open_platform()?))
    }

    /// Adopt an already-opened shared platform provider.
    pub fn from_platform(platform: Platform) -> Self {
        Self { platform }
    }
}

impl Hal for PlatformHal {
    fn num_sfps(&self) -> Result<usize> {
        Ok(self.platform.clone().get_sfps()?.len())
    }

    fn sfp(&self, index: usize) -> Result<Box<dyn SfpHandle>> {
        let mut platform = self.platform.clone();
        let key = platform
            .get_sfps()?
            .get(index)
            .ok_or(XcvrdError::PhysicalPortNotExist)?
            .key
            .clone();
        Ok(Box::new(PlatformSfp { platform, key }))
    }

    fn get_change_event(&self, timeout_ms: u64) -> Result<ChangeEvent> {
        let batch = self
            .platform
            .clone()
            .get_change_event(api_integer(timeout_ms)?)?;
        let mut result = ChangeEvent {
            status: batch.ok,
            ..ChangeEvent::default()
        };
        for event in batch.events {
            match event.device_type.as_str() {
                "sfp" => result.sfp.insert(event.device_id, event.status),
                "sfp_error" => result.sfp_error.insert(event.device_id, event.status),
                _ => None,
            };
        }
        Ok(result)
    }
}

/// Real SFP handle: thin delegation to the shared platform's SFP.
pub struct PlatformSfp {
    platform: Platform,
    key: DeviceKey,
}

fn api_integer(value: impl TryInto<i64>) -> Result<i64> {
    value
        .try_into()
        .map_err(|_| XcvrdError::Other("platform API integer overflow".into()))
}

fn required_bool(value: Option<bool>) -> Result<bool> {
    value.ok_or_else(|| XcvrdError::Bridge("platform returned no boolean value".into()))
}

fn json_value(value: String) -> Result<Value> {
    serde_json::from_str(&value).map_err(|e| XcvrdError::Bridge(e.to_string()))
}

impl SfpHandle for PlatformSfp {
    fn get_presence(&self) -> Result<bool> {
        required_bool(self.platform.clone().sfp_get_presence(&self.key)?)
    }
    fn is_replaceable(&self) -> Result<bool> {
        required_bool(self.platform.clone().sfp_is_replaceable(&self.key)?)
    }
    fn get_reset_status(&self) -> Result<bool> {
        required_bool(self.platform.clone().sfp_get_reset_status(&self.key)?)
    }
    fn sfp_type(&self) -> Result<String> {
        Ok(self.platform.clone().sfp_get_type(&self.key)?)
    }
    fn get_error_description(&self) -> Result<Option<String>> {
        Ok(self.platform.clone().sfp_get_error_description(&self.key)?)
    }
    fn get_transceiver_info(&self) -> Result<Value> {
        json_value(self.platform.clone().sfp_get_transceiver_info(&self.key)?)
    }
    fn get_transceiver_dom_real_value(&self) -> Result<Value> {
        json_value(
            self.platform
                .clone()
                .sfp_get_transceiver_dom_real_value(&self.key)?,
        )
    }
    fn get_transceiver_status(&self) -> Result<Value> {
        json_value(
            self.platform
                .clone()
                .sfp_get_transceiver_status(&self.key)?,
        )
    }
    fn get_transceiver_threshold_info(&self) -> Result<Value> {
        json_value(
            self.platform
                .clone()
                .sfp_get_transceiver_threshold_info(&self.key)?,
        )
    }
    fn get_lpmode(&self) -> Result<bool> {
        required_bool(self.platform.clone().sfp_get_lpmode(&self.key)?)
    }
    fn set_lpmode(&self, on: bool) -> Result<bool> {
        required_bool(self.platform.clone().sfp_set_lpmode(&self.key, on)?)
    }
    fn reset(&self) -> Result<bool> {
        required_bool(self.platform.clone().sfp_reset(&self.key)?)
    }
    fn has_method(&self, method: &str) -> Result<bool> {
        Ok(self.platform.clone().sfp_has_method(&self.key, method)?)
    }
    fn call_json(&self, method: &str) -> Result<Value> {
        let mut api = self.platform.clone();
        let key = &self.key;
        let value = match method {
            "get_transceiver_info_firmware_versions" => {
                api.sfp_get_transceiver_info_firmware_versions(key)?
            }
            "get_transceiver_status_flags" => api.sfp_get_transceiver_status_flags(key)?,
            "get_transceiver_dom_flags" => api.sfp_get_transceiver_dom_flags(key)?,
            "get_temperature" => api.sfp_get_temperature(key)?,
            "get_transceiver_pm" => api.sfp_get_transceiver_pm(key)?,
            "get_transceiver_vdm_real_value_basic" => {
                api.sfp_get_transceiver_vdm_real_value_basic(key)?
            }
            "get_transceiver_vdm_real_value_statistic" => {
                api.sfp_get_transceiver_vdm_real_value_statistic(key)?
            }
            "get_transceiver_vdm_flags" => api.sfp_get_transceiver_vdm_flags(key)?,
            "get_transceiver_vdm_thresholds" => api.sfp_get_transceiver_vdm_thresholds(key)?,
            "is_transceiver_vdm_supported" => {
                return Ok(serde_json::json!(api.sfp_is_transceiver_vdm_supported(key)?))
            }
            "is_vdm_statistic_supported" => {
                return Ok(serde_json::json!(api.sfp_is_vdm_statistic_supported(key)?))
            }
            "get_vdm_freeze_status" => {
                return Ok(serde_json::json!(api.sfp_get_vdm_freeze_status(key)?))
            }
            "get_vdm_unfreeze_status" => {
                return Ok(serde_json::json!(api.sfp_get_vdm_unfreeze_status(key)?))
            }
            "freeze_vdm_stats" => return Ok(serde_json::json!(api.sfp_freeze_vdm_stats(key)?)),
            "unfreeze_vdm_stats" => return Ok(serde_json::json!(api.sfp_unfreeze_vdm_stats(key)?)),
            "remove_xcvr_api" => {
                api.sfp_remove_xcvr_api(key)?;
                return Ok(Value::Null);
            }
            _ => return Err(XcvrdError::NotImplemented),
        };
        json_value(value)
    }
    fn xcvr_api_call_json(&self, method: &str) -> Result<Value> {
        let mut api = self.platform.clone();
        let key = &self.key;
        let value = match method {
            "is_flat_memory" => return Ok(serde_json::json!(api.sfp_is_flat_memory(key)?)),
            "is_coherent_module" => return Ok(serde_json::json!(api.sfp_is_coherent_module(key)?)),
            "get_module_type_abbreviation" => {
                return Ok(serde_json::json!(api.sfp_get_module_type_abbreviation(key)?))
            }
            "get_module_state" => return Ok(serde_json::json!(api.sfp_get_module_state(key)?)),
            "get_cmis_rev" => return Ok(serde_json::json!(api.sfp_get_cmis_rev(key)?)),
            "get_application_advertisement" => api.sfp_get_application_advertisement(key)?,
            "get_datapath_state" => api.sfp_get_datapath_state(key)?,
            "get_config_datapath_hostlane_status" => {
                api.sfp_get_config_datapath_hostlane_status(key)?
            }
            "get_dpinit_pending" => api.sfp_get_dpinit_pending(key)?,
            _ => return Err(XcvrdError::NotImplemented),
        };
        json_value(value)
    }
    fn read_eeprom(&self, offset: usize, num_bytes: usize) -> Result<Option<Vec<u8>>> {
        Ok(self.platform.clone().sfp_read_eeprom(
            &self.key,
            api_integer(offset)?,
            api_integer(num_bytes)?,
        )?)
    }
    fn write_eeprom(&self, offset: usize, data: &[u8]) -> Result<bool> {
        required_bool(self.platform.clone().sfp_write_eeprom(
            &self.key,
            api_integer(offset)?,
            data,
        )?)
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
            let module = PyModule::from_code(
                py,
                pyo3::ffi::c_str!(
                    r#"
class Decoder:
    def __init__(self, generation):
        self.generation = generation
    def thresholds(self):
        return {"temphighalarm": self.generation, "rxpowerlowalarm": float("-inf")}
    def get_module_state(self):
        return "generation %d" % self.generation

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
    def get_transceiver_dom_flags(self):
        return {"temperature": True}
    def get_transceiver_status_flags(self):
        return {"txfault": False}
    def freeze_vdm_stats(self):
        return False
    def remove_xcvr_api(self):
        self._xcvr_api = None

class Chassis:
    def __init__(self):
        self.sfp = Sfp()
    def get_all_sfps(self):
        return [self.sfp]
    def get_change_event(self, timeout):
        self.sfp.generation += 1
        self.sfp._xcvr_api = None
        return False, {"sfp": {0: "0"}, "sfp_error": {0: "vendor error"}, "fan": {1: "1"}}

class Platform:
    instances = 0
    def __init__(self):
        Platform.instances += 1
        self.chassis = Chassis()
    def get_chassis(self):
        return self.chassis
"#
                ),
                pyo3::ffi::c_str!("hal_test.py"),
                pyo3::ffi::c_str!("sonic_platform.platform"),
            )
            .unwrap();
            let modules = py.import("sys").unwrap().getattr("modules").unwrap();
            let overrides = PyDict::new(py);
            overrides.set_item("sonic_platform", &module).unwrap();
            overrides
                .set_item("sonic_platform.platform", &module)
                .unwrap();
            let patch = py
                .import("unittest.mock")
                .unwrap()
                .getattr("patch")
                .unwrap()
                .call_method1("dict", (modules, overrides))
                .unwrap();
            patch.call_method0("__enter__").unwrap();
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                let hal = PlatformHal::new().unwrap();
                assert_eq!(hal.num_sfps().unwrap(), 1);
                assert!(matches!(hal.sfp(1), Err(XcvrdError::PhysicalPortNotExist)));
                let sfp = hal.sfp(0).unwrap();
                let thresholds = sfp.get_transceiver_threshold_info().unwrap();
                assert_eq!(thresholds["temphighalarm"], json!(1));
                assert_eq!(value_to_py_str(&thresholds["rxpowerlowalarm"]), "-inf");
                let events = hal.get_change_event(1).unwrap();
                assert!(!events.status);
                assert_eq!(events.sfp, BTreeMap::from([("0".into(), "0".into())]));
                assert_eq!(
                    events.sfp_error,
                    BTreeMap::from([("0".into(), "vendor error".into())])
                );
                assert_eq!(sfp.get_transceiver_info().unwrap()["generation"], json!(2));
                assert_eq!(
                    sfp.get_transceiver_threshold_info().unwrap()["temphighalarm"],
                    json!(2)
                );
                assert_eq!(
                    sfp.xcvr_api_call_json("get_module_state").unwrap(),
                    json!("generation 2")
                );
                assert_eq!(
                    sfp.call_json("get_transceiver_dom_flags").unwrap(),
                    json!({"temperature": true})
                );
                assert_eq!(
                    sfp.call_json("get_transceiver_status_flags").unwrap(),
                    json!({"txfault": false})
                );
                assert_eq!(sfp.call_json("freeze_vdm_stats").unwrap(), json!(false));
                assert_eq!(sfp.call_json("remove_xcvr_api").unwrap(), Value::Null);
                assert!(matches!(
                    sfp.call_json("unlisted_method"),
                    Err(XcvrdError::NotImplemented)
                ));
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
