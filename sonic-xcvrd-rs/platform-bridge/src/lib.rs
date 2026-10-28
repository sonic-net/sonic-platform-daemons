//! platform-bridge — typed Rust access to the Python `sonic_platform` plugin via PyO3.
//!
//! # Why this exists
//! The Rust daemon uses the same `sonic_platform` plugin as Python xcvrd,
//! whether backed by real hardware or the `xcvr-emu` test platform.
//! The existing CMIS/SFF decode stack
//! (`get_transceiver_info`, `get_transceiver_dom_real_value`,
//! `get_transceiver_status`, lpmode/reset, …) already exists in Python, built on
//! three primitive hooks (`read_eeprom`/`write_eeprom`/`get_presence`).
//!
//! This bridge embeds CPython, imports the platform plugin, and exposes both
//! aggregate getters and individual decoder calls to Rust. Complex return values
//! are marshalled as `serde_json::Value`; scalars come back typed.
//!
//! # Runtime requirements
//! Embeds the build environment's libpython (`auto-initialize`). At runtime the
//! matching shared library and an importable `sonic_platform` must be available,
//! as in pmon. When using the emulator platform, `xcvr-emu` must also be running.

use std::collections::BTreeMap;

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use serde_json::Value;
use thiserror::Error;

/// Errors surfaced by the bridge. Python failures include the formatted
/// traceback so import/attribute problems on the DUT are debuggable from logs.
#[derive(Error, Debug)]
pub enum BridgeError {
    #[error("python error: {0}")]
    Py(String),
    #[error("json marshalling error: {0}")]
    Json(String),
    #[error("bridge error: {0}")]
    Other(String),
}

impl From<PyErr> for BridgeError {
    fn from(e: PyErr) -> Self {
        Python::with_gil(|py| {
            let tb = e
                .traceback_bound(py)
                .and_then(|t| t.format().ok())
                .unwrap_or_default();
            BridgeError::Py(format!("{e}\n{tb}"))
        })
    }
}

pub type Result<T> = std::result::Result<T, BridgeError>;

/// Crate version, for smoke logging.
pub fn bridge_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// One transceiver change-event poll: `chassis.get_change_event(timeout)` returns
/// `(status, {'sfp': {port: code}, 'sfp_error': {port: code}})`. Keys are physical
/// port strings; values are xcvrd event codes (`"1"` insert, `"0"` remove, other =
/// SfpBase error bitmap).
#[derive(Debug, Clone, Default)]
pub struct ChangeEvent {
    pub status: bool,
    pub sfp: BTreeMap<String, String>,
    pub sfp_error: BTreeMap<String, String>,
}

/// `json.dumps(obj, default=str)` -> `serde_json::Value`. Robust to arbitrary
/// nesting and future fields; unsupported values and non-finite floats use Python str().
fn py_to_json(py: Python<'_>, obj: &Bound<'_, PyAny>) -> Result<Value> {
    if obj.is_none() {
        return Ok(Value::Null);
    }
    let json = py.import_bound("json")?;
    let builtins = py.import_bound("builtins")?;
    let kwargs = PyDict::new_bound(py);
    kwargs.set_item("default", builtins.getattr("str")?)?;
    let dumped = json.call_method("dumps", (obj,), Some(&kwargs))?;
    let s: String = dumped.extract()?;
    serde_json::from_str(&sanitize_nonfinite_json(&s)).map_err(|e| BridgeError::Json(e.to_string()))
}

/// Quote Python's non-standard JSON number tokens without modifying string contents.
fn sanitize_nonfinite_json(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut in_str = false;
    let mut i = 0;
    while i < s.len() {
        let rest = &s[i..];
        let ch = rest.chars().next().unwrap();
        let ch_len = ch.len_utf8();
        if in_str {
            out.push(ch);
            if ch == '\\' {
                if let Some(next) = rest[ch_len..].chars().next() {
                    out.push(next);
                    i += ch_len + next.len_utf8();
                    continue;
                }
            } else if ch == '"' {
                in_str = false;
            }
            i += ch_len;
            continue;
        }
        if ch == '"' {
            in_str = true;
            out.push('"');
            i += ch_len;
            continue;
        }
        if rest.starts_with("-Infinity") {
            out.push_str("\"-inf\"");
            i += "-Infinity".len();
            continue;
        }
        if rest.starts_with("Infinity") {
            out.push_str("\"inf\"");
            i += "Infinity".len();
            continue;
        }
        if rest.starts_with("NaN") {
            out.push_str("\"nan\"");
            i += "NaN".len();
            continue;
        }
        out.push(ch);
        i += ch_len;
    }
    out
}

/// Pull a `{str: str}` map out of a JSON object value (missing/!object -> empty).
fn json_str_map(v: Option<&Value>) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    if let Some(Value::Object(map)) = v {
        for (k, val) in map {
            let s = match val {
                Value::String(s) => s.clone(),
                other => other.to_string(),
            };
            out.insert(k.clone(), s);
        }
    }
    out
}

/// The transceiver plant: `sonic_platform.platform.Platform().get_chassis()`.
///
/// Cheap to clone-free share across threads is NOT provided; construct once and
/// hand out [`Sfp`] handles. Each call re-acquires the GIL.
pub struct Platform {
    chassis: Py<PyAny>,
}

impl Platform {
    /// Import the plugin and build the chassis. Triggers the emulator `List()`
    /// RPC (falls back to `XCVR_EMU_NUM_SFPS` placeholders if the emulator is not
    /// up yet, exactly like the Python daemon at start-up).
    pub fn new() -> Result<Self> {
        Python::with_gil(|py| {
            // Vendor plugins use argv[0] to select daemon-owned module initialization.
            let sys = py.import_bound("sys")?;
            let argv: Vec<String> = sys.getattr("argv")?.extract()?;
            if argv.is_empty() || argv[0].is_empty() {
                sys.setattr("argv", std::env::args().collect::<Vec<_>>())?;
            }
            let module = py.import_bound("sonic_platform.platform")?;
            let platform = module.getattr("Platform")?.call0()?;
            let chassis = platform.call_method0("get_chassis")?;
            Ok(Platform {
                chassis: chassis.unbind(),
            })
        })
    }

    /// Number of SFP slots the chassis discovered.
    pub fn num_sfps(&self) -> Result<usize> {
        Python::with_gil(|py| {
            let n = self.chassis.bind(py).call_method0("get_num_sfps")?;
            Ok(n.extract()?)
        })
    }

    /// Handle to the SFP at `index` (0-based, == emulator module index). Returns
    /// an error if the chassis has no SFP there.
    pub fn sfp(&self, index: usize) -> Result<Sfp> {
        Python::with_gil(|py| {
            let obj = self.chassis.bind(py).call_method1("get_sfp", (index,))?;
            if obj.is_none() {
                return Err(BridgeError::Other(format!("no sfp at index {index}")));
            }
            Ok(Sfp {
                obj: obj.unbind(),
                index,
            })
        })
    }

    /// Poll for transceiver change events. `timeout_ms == 0` blocks up to the
    /// plugin's poll interval; otherwise blocks up to `timeout_ms`, returning
    /// early on the first change.
    pub fn get_change_event(&self, timeout_ms: u64) -> Result<ChangeEvent> {
        Python::with_gil(|py| {
            let res = self
                .chassis
                .bind(py)
                .call_method1("get_change_event", (timeout_ms,))?;
            let (status, events): (bool, Bound<'_, PyAny>) = res.extract()?;
            let ev = py_to_json(py, &events)?;
            Ok(ChangeEvent {
                status,
                sfp: json_str_map(ev.get("sfp")),
                sfp_error: json_str_map(ev.get("sfp_error")),
            })
        })
    }
}

/// A single transceiver slot. All getters re-acquire the GIL and call straight
/// through to the Python `Sfp` (i.e. `SfpOptoeBase` + the emulator overrides).
pub struct Sfp {
    obj: Py<PyAny>,
    index: usize,
}

impl Sfp {
    /// 0-based physical index this handle wraps.
    pub fn index(&self) -> usize {
        self.index
    }

    /// Discover optional API methods without calling the getter.
    pub fn has_method(&self, method: &str) -> Result<bool> {
        Python::with_gil(|py| Ok(self.obj.bind(py).hasattr(method)?))
    }

    fn call_bool(&self, method: &str) -> Result<bool> {
        Python::with_gil(|py| Ok(self.obj.bind(py).call_method0(method)?.extract()?))
    }

    /// Is a module physically present (emulator `GetInfo().present`).
    pub fn get_presence(&self) -> Result<bool> {
        self.call_bool("get_presence")
    }

    /// Whether the slot is field-replaceable (always true on the emulator).
    pub fn is_replaceable(&self) -> Result<bool> {
        self.call_bool("is_replaceable")
    }

    /// Whether the module is held in reset (always false on the emulator).
    pub fn get_reset_status(&self) -> Result<bool> {
        self.call_bool("get_reset_status")
    }

    /// The `sfp_type` attribute (e.g. `"QSFP_DD"`).
    pub fn sfp_type(&self) -> Result<String> {
        Python::with_gil(|py| Ok(self.obj.bind(py).getattr("sfp_type")?.extract()?))
    }

    /// Human-readable error description, if any.
    pub fn get_error_description(&self) -> Result<Option<String>> {
        Python::with_gil(|py| {
            let r = self.obj.bind(py).call_method0("get_error_description")?;
            if r.is_none() {
                Ok(None)
            } else {
                Ok(Some(r.extract()?))
            }
        })
    }

    /// Full CMIS/SFF identity dict (`TRANSCEIVER_INFO` source). [M1]
    pub fn get_transceiver_info(&self) -> Result<Value> {
        self.call_json("get_transceiver_info", ())
    }

    /// Live DOM readings, including older bulk-status-only platform APIs. [M2]
    pub fn get_transceiver_dom_real_value(&self) -> Result<Value> {
        Python::with_gil(|py| {
            let sfp = self.obj.bind(py);
            let method = if sfp.hasattr("get_transceiver_dom_real_value")? {
                "get_transceiver_dom_real_value"
            } else {
                "get_transceiver_bulk_status"
            };
            let value = sfp.call_method0(method)?;
            py_to_json(py, &value)
        })
    }

    /// Module/datapath status (`TRANSCEIVER_STATUS` source, incl. cmis_state). [M3]
    pub fn get_transceiver_status(&self) -> Result<Value> {
        self.call_json("get_transceiver_status", ())
    }

    /// DOM thresholds (`TRANSCEIVER_DOM_THRESHOLD` source).
    pub fn get_transceiver_threshold_info(&self) -> Result<Value> {
        self.call_json("get_transceiver_threshold_info", ())
    }

    /// Low-power mode state. [M4]
    pub fn get_lpmode(&self) -> Result<bool> {
        self.call_bool("get_lpmode")
    }

    /// Drive low-power mode on/off. [M4]
    pub fn set_lpmode(&self, on: bool) -> Result<bool> {
        Python::with_gil(|py| {
            Ok(self
                .obj
                .bind(py)
                .call_method1("set_lpmode", (on,))?
                .extract()?)
        })
    }

    /// Momentary CMIS software reset. [M4]
    pub fn reset(&self) -> Result<bool> {
        self.call_bool("reset")
    }

    /// Read `num_bytes` of EEPROM at the optoe-linear `offset`. `None` on RPC
    /// failure (matches the plugin's `read_eeprom` contract).
    pub fn read_eeprom(&self, offset: usize, num_bytes: usize) -> Result<Option<Vec<u8>>> {
        Python::with_gil(|py| {
            let r = self
                .obj
                .bind(py)
                .call_method1("read_eeprom", (offset, num_bytes))?;
            if r.is_none() {
                return Ok(None);
            }
            // read_eeprom returns a bytearray; normalise to bytes then to Vec<u8>.
            let as_bytes = py.import_bound("builtins")?.getattr("bytes")?.call1((r,))?;
            Ok(Some(as_bytes.extract()?))
        })
    }

    /// Write `data` to EEPROM at the optoe-linear `offset`. Returns the plugin's
    /// success flag.
    pub fn write_eeprom(&self, offset: usize, data: &[u8]) -> Result<bool> {
        Python::with_gil(|py| {
            let buf = PyBytes::new_bound(py, data);
            let ok = self
                .obj
                .bind(py)
                .call_method1("write_eeprom", (offset, data.len(), buf))?;
            Ok(ok.extract()?)
        })
    }

    /// Escape hatch: call any no-arg Python `Sfp` method and marshal its result as
    /// JSON. Lets the daemon reach methods not yet given a typed wrapper without a
    /// bridge change.
    pub fn call_json(
        &self,
        method: &str,
        args: impl IntoPy<Py<pyo3::types::PyTuple>>,
    ) -> Result<Value> {
        Python::with_gil(|py| {
            let r = self.obj.bind(py).call_method1(method, args)?;
            py_to_json(py, &r)
        })
    }

    /// Call a no-arg method on this slot's **decode api** —
    /// `self.obj.get_xcvr_api().<method>()` — and marshal the result as JSON.
    ///
    /// Every other getter here is an *aggregate* (`get_transceiver_status` /
    /// `get_transceiver_info`), which re-reads and re-decodes the whole module. On
    /// `str43-7060x6-512-c17-u10` those cost 36 ms and 56 ms respectively, while the
    /// individual CMIS decode reads they contain cost 0-4.5 ms. The CMIS bring-up state
    /// machine wants the individual reads, so give it a direct route to them.
    ///
    /// Deliberately resolved through the SAME `Sfp` object the rest of the bridge uses:
    /// `get_xcvr_api()` memoises `_xcvr_api` per SFP, and it is this object that the
    /// change-event path refreshes on a module swap, so the decoder handed back here can
    /// never be staler than the one behind the aggregate getters.
    ///
    /// `Ok(Value::Null)` when the slot has no api (absent module) — the caller then
    /// falls back rather than treating it as a value.
    pub fn xcvr_api_call_json(&self, method: &str) -> Result<Value> {
        Python::with_gil(|py| {
            let api = self.obj.bind(py).call_method0("get_xcvr_api")?;
            if api.is_none() {
                return Ok(Value::Null);
            }
            let r = api.call_method0(method)?;
            py_to_json(py, &r)
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn platform_construction_initializes_placeholder_python_argv() {
        Python::with_gil(|py| {
            let sys = py.import_bound("sys").unwrap();
            let original_argv = sys.getattr("argv").unwrap();
            let module = PyModule::from_code_bound(
                py,
                r#"
import sys
class Platform:
    def get_chassis(self):
        if sys.argv != expected_argv:
            raise RuntimeError("platform startup lost the host process arguments")
        return self
    def get_num_sfps(self):
        return 1
"#,
                "platform_context_test.py",
                "sonic_platform.platform",
            )
            .unwrap();
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
                .call_method1("dict", (sys.getattr("modules").unwrap(), overrides))
                .unwrap();
            patch.call_method0("__enter__").unwrap();
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                let host_argv: Vec<String> = std::env::args().collect();
                for (initial, expected) in [
                    (vec![String::new()], host_argv.clone()),
                    (Vec::new(), host_argv),
                    (
                        vec!["custom-host".into(), "--keep".into()],
                        vec!["custom-host".into(), "--keep".into()],
                    ),
                ] {
                    sys.setattr("argv", initial).unwrap();
                    module.setattr("expected_argv", expected).unwrap();
                    assert_eq!(Platform::new().unwrap().num_sfps().unwrap(), 1);
                }
            }));
            sys.setattr("argv", original_argv).unwrap();
            patch
                .call_method1("__exit__", (py.None(), py.None(), py.None()))
                .unwrap();
            if let Err(payload) = result {
                std::panic::resume_unwind(payload);
            }
        });
    }

    #[test]
    fn nonfinite_values_survive_all_json_getter_paths() {
        Python::with_gil(|py| {
            let module = PyModule::from_code_bound(
                py,
                r#"
class Sfp:
    def get_xcvr_api(self):
        return self
    def get_transceiver_dom_real_value(self):
        return {
            "temperature": 42.5, "voltage": 3.3, "rxpower1": float("-inf"),
            "nested": [float("inf"), float("nan")],
            "note": 'quoted "Infinity" and NaN',
        }
    generic_getter = get_transceiver_dom_real_value
    decoder_getter = get_transceiver_dom_real_value
"#,
                "bridge_test.py",
                "bridge_test",
            )
            .unwrap();
            let sfp = Sfp {
                obj: module.getattr("Sfp").unwrap().call0().unwrap().unbind(),
                index: 0,
            };
            for value in [
                sfp.get_transceiver_dom_real_value().unwrap(),
                sfp.call_json("generic_getter", ()).unwrap(),
                sfp.xcvr_api_call_json("decoder_getter").unwrap(),
            ] {
                assert_eq!(
                    value,
                    json!({
                        "temperature": 42.5, "voltage": 3.3, "rxpower1": "-inf",
                        "nested": ["inf", "nan"], "note": "quoted \"Infinity\" and NaN",
                    })
                );
            }
        });
    }

    #[test]
    fn legacy_bulk_status_remains_a_dom_sensor_source() {
        Python::with_gil(|py| {
            let module = PyModule::from_code_bound(
                py,
                r#"
class Sfp:
    def get_transceiver_bulk_status(self):
        return {"temperature": 42.5, "voltage": 3.3, "rx1power": float("-inf")}
"#,
                "legacy_dom_test.py",
                "legacy_dom_test",
            )
            .unwrap();
            let sfp = Sfp {
                obj: module.getattr("Sfp").unwrap().call0().unwrap().unbind(),
                index: 0,
            };
            assert_eq!(
                sfp.get_transceiver_dom_real_value().unwrap(),
                json!({"temperature": 42.5, "voltage": 3.3, "rx1power": "-inf"})
            );
        });
    }

    #[test]
    fn current_dom_getter_is_preferred_without_masking_empty_reads_or_errors() {
        Python::with_gil(|py| {
            let module = PyModule::from_code_bound(
                py,
                r#"
class Sfp:
    def get_transceiver_dom_real_value(self):
        return {"temperature": 42.5}
    def get_transceiver_bulk_status(self):
        raise AssertionError("legacy getter must not run when the current API exists")

class EmptySfp(Sfp):
    def get_transceiver_dom_real_value(self):
        return None

class BrokenSfp(Sfp):
    def get_transceiver_dom_real_value(self):
        raise RuntimeError("current DOM read failed")
"#,
                "current_dom_test.py",
                "current_dom_test",
            )
            .unwrap();
            for (class, expected) in [
                ("Sfp", json!({"temperature": 42.5})),
                ("EmptySfp", Value::Null),
            ] {
                let sfp = Sfp {
                    obj: module.getattr(class).unwrap().call0().unwrap().unbind(),
                    index: 0,
                };
                assert_eq!(sfp.get_transceiver_dom_real_value().unwrap(), expected);
            }
            let broken = Sfp {
                obj: module
                    .getattr("BrokenSfp")
                    .unwrap()
                    .call0()
                    .unwrap()
                    .unbind(),
                index: 0,
            };
            assert!(broken
                .get_transceiver_dom_real_value()
                .unwrap_err()
                .to_string()
                .contains("current DOM read failed"));
        });
    }

    #[test]
    fn bare_nonfinite_tokens_become_quoted_python_str_form() {
        let raw =
            r#"{"temphighalarm": -Infinity, "vcchighalarm": Infinity, "txbiashighalarm": NaN}"#;
        let v: Value = serde_json::from_str(&sanitize_nonfinite_json(raw)).unwrap();
        assert_eq!(v["temphighalarm"], json!("-inf"));
        assert_eq!(v["vcchighalarm"], json!("inf"));
        assert_eq!(v["txbiashighalarm"], json!("nan"));
    }

    #[test]
    fn quoted_and_escaped_field_text_is_untouched() {
        let raw = r#"{"note": "a \"quoted -Infinity\" and NaN", "x": Infinity}"#;
        let v: Value = serde_json::from_str(&sanitize_nonfinite_json(raw)).unwrap();
        assert_eq!(v["note"], json!("a \"quoted -Infinity\" and NaN"));
        assert_eq!(v["x"], json!("inf"));
    }

    #[test]
    fn finite_json_is_byte_identical() {
        let raw = r#"{"temp": -40.0, "voltage": 3.3, "vendor": "ACME", "ok": true, "n": null}"#;
        assert_eq!(sanitize_nonfinite_json(raw), raw);
    }
}
