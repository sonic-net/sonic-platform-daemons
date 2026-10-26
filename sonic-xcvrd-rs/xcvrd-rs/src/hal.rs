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

use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::PyDict;
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

/// A daemon-owned `sonic_platform` chassis used ONLY for the DOM-threshold read.
///
/// The thick `platform-bridge` marshals every complex getter through
/// `json.dumps(obj, default=str)` + `serde_json::from_str`. A DEFAULT (never-enriched)
/// transceiver decodes its zero-power page-02h thresholds to Python `float('-inf')`,
/// which `json.dumps` emits as the bare token `-Infinity` — NOT valid strict JSON, so
/// `serde_json` rejects it and `get_transceiver_threshold_info()` fails outright.
/// `TRANSCEIVER_DOM_THRESHOLD` is then never published for such a module (e.g. the
/// spare admin-up logical port the port-lifecycle e2e exercises, which is never
/// threshold-enriched), and — because recovery treats a threshold-missing port as
/// un-baselined — it re-posts DOM/VDM thresholds for every such port each interval,
/// flooding the PyO3 bridge and starving the DOM flag poll.
///
/// The bridge is a fixed dependency, so the daemon re-reads the thresholds itself via
/// PyO3 and sanitizes the non-finite floats to their Python `str()` form
/// (`"-inf"`/`"inf"`/`"nan"`) — exactly what the reference daemon posts — before
/// parsing. Used for the threshold read only; every other getter and the change-event
/// poll stay on the bridge unchanged, so this cannot regress those paths.
struct ThrChassis {
    chassis: Py<PyAny>,
}

impl ThrChassis {
    fn new() -> Result<Self> {
        Python::with_gil(|py| {
            let module = py
                .import_bound("sonic_platform.platform")
                .map_err(|e| XcvrdError::Bridge(format!("import sonic_platform.platform: {e}")))?;
            let platform = module
                .getattr("Platform")
                .and_then(|p| p.call0())
                .map_err(|e| XcvrdError::Bridge(format!("Platform(): {e}")))?;
            let chassis = platform
                .call_method0("get_chassis")
                .map_err(|e| XcvrdError::Bridge(format!("get_chassis(): {e}")))?;
            Ok(ThrChassis {
                chassis: chassis.unbind(),
            })
        })
    }

    /// Read the `TRANSCEIVER_DOM_THRESHOLD` source dict for physical `index`,
    /// sanitizing non-finite floats so the result is strict JSON. `Value::Null` when
    /// the slot is absent/None (the poster then no-ops, exactly as for an absent
    /// module or an empty dict).
    fn read_threshold_json(&self, index: usize) -> Result<Value> {
        Python::with_gil(|py| {
            let sfp = self
                .chassis
                .bind(py)
                .call_method1("get_sfp", (index,))
                .map_err(|e| XcvrdError::Bridge(format!("get_sfp({index}): {e}")))?;
            if sfp.is_none() {
                return Ok(Value::Null);
            }
            let raw = sfp
                .call_method0("get_transceiver_threshold_info")
                .map_err(|e| XcvrdError::Bridge(format!("get_transceiver_threshold_info: {e}")))?;
            if raw.is_none() {
                return Ok(Value::Null);
            }
            // Same shape as the bridge's `py_to_json` (json.dumps default=str), but the
            // resulting string is sanitized before parsing so the `-Infinity`/`NaN`
            // tokens a default module produces don't defeat `serde_json`.
            let json = py
                .import_bound("json")
                .map_err(|e| XcvrdError::Bridge(format!("import json: {e}")))?;
            let str_fn = py
                .import_bound("builtins")
                .and_then(|b| b.getattr("str"))
                .map_err(|e| XcvrdError::Bridge(format!("builtins.str: {e}")))?;
            let kwargs = PyDict::new_bound(py);
            kwargs
                .set_item("default", str_fn)
                .map_err(|e| XcvrdError::Bridge(format!("set default kwarg: {e}")))?;
            let dumped = json
                .call_method("dumps", (raw,), Some(&kwargs))
                .map_err(|e| XcvrdError::Bridge(format!("json.dumps: {e}")))?;
            let s: String = dumped
                .extract()
                .map_err(|e| XcvrdError::Bridge(format!("extract dumped json: {e}")))?;
            serde_json::from_str(&sanitize_nonfinite_json(&s))
                .map_err(|e| XcvrdError::Bridge(format!("parse threshold json: {e}")))
        })
    }
}

/// Rewrite the non-finite JSON number literals Python's `json.dumps` emits
/// (`-Infinity`/`Infinity`/`NaN`, all invalid in strict JSON) to their Python `str()`
/// string form (`"-inf"`/`"inf"`/`"nan"`), leaving everything else — including any
/// text that happens to contain those words INSIDE a JSON string — untouched. This
/// lets `serde_json` parse a default transceiver's zero/`-inf` DOM thresholds, and the
/// resulting strings render through `value_to_py_str` to the exact field text the
/// Python daemon posts (`str(float('-inf')) == "-inf"`).
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
                // Copy the escaped char verbatim so an escaped quote can't end the string.
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
        // Order matters: `-Infinity` must be tried before `Infinity`.
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

/// Real HAL: wraps `platform_bridge::Platform`, plus a daemon-owned side chassis for
/// the inf-safe DOM-threshold read (see [`ThrChassis`]).
pub struct BridgeHal {
    platform: platform_bridge::Platform,
    thr: Option<Arc<ThrChassis>>,
}

impl BridgeHal {
    /// Import the plugin + build the chassis (see `env::open_platform`).
    pub fn new() -> Result<Self> {
        Ok(Self::from_platform(platform_bridge::Platform::new()?))
    }

    /// Adopt an already-constructed bridge `Platform` and build the side chassis used
    /// for the inf-safe DOM-threshold read. If that side chassis can't be built, the
    /// daemon degrades to the bridge's (inf-fragile) threshold read — never worse than
    /// before — and logs once.
    pub fn from_platform(platform: platform_bridge::Platform) -> Self {
        let thr = match ThrChassis::new() {
            Ok(t) => Some(Arc::new(t)),
            Err(e) => {
                eprintln!(
                    "xcvrd-rs: inf-safe DOM-threshold chassis unavailable ({e}); \
                     DOM_THRESHOLD for default zero/-inf modules may not publish"
                );
                None
            }
        };
        BridgeHal { platform, thr }
    }
}

impl Hal for BridgeHal {
    fn num_sfps(&self) -> Result<usize> {
        Ok(self.platform.num_sfps()?)
    }

    fn sfp(&self, index: usize) -> Result<Box<dyn SfpHandle>> {
        Ok(Box::new(BridgeSfp {
            sfp: self.platform.sfp(index)?,
            thr: self.thr.clone(),
            index,
        }))
    }

    fn get_change_event(&self, timeout_ms: u64) -> Result<ChangeEvent> {
        Ok(self.platform.get_change_event(timeout_ms)?)
    }
}

/// Real SFP handle: wraps `platform_bridge::Sfp` (thin delegation). Holds the
/// daemon-owned threshold chassis + physical index so the DOM-threshold read can go
/// through the inf-safe path (see [`ThrChassis`]); every other getter delegates to
/// the bridge unchanged.
pub struct BridgeSfp {
    sfp: platform_bridge::Sfp,
    thr: Option<Arc<ThrChassis>>,
    index: usize,
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
        // Read via the daemon-owned chassis with non-finite sanitization so a default
        // module's `-inf` zero-power thresholds still publish TRANSCEIVER_DOM_THRESHOLD
        // (the thick bridge's serde_json marshal rejects the `-Infinity` token and
        // fails the whole read). Fall back to the bridge on any error, so this is never
        // worse than the bridge alone.
        if let Some(thr) = &self.thr {
            match thr.read_threshold_json(self.index) {
                Ok(v) => return Ok(v),
                Err(e) => {
                    eprintln!(
                        "xcvrd-rs: inf-safe DOM-threshold read (index {}) failed ({e}); \
                         falling back to bridge",
                        self.index
                    );
                }
            }
        }
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
    use super::sanitize_nonfinite_json;
    use crate::dom::utilities::db::value_to_py_str;
    use serde_json::Value;

    fn parse(s: &str) -> Value {
        serde_json::from_str(&sanitize_nonfinite_json(s))
            .unwrap_or_else(|e| panic!("sanitized json must parse: {e}\ninput: {s}"))
    }

    #[test]
    fn bare_nonfinite_tokens_become_quoted_python_str_form() {
        // Exactly what Python `json.dumps({'temp': float('-inf'), ...}, default=str)`
        // emits for a default module's zero-power thresholds.
        let raw =
            r#"{"temphighalarm": -Infinity, "vcchighalarm": Infinity, "txbiashighalarm": NaN}"#;
        let v = parse(raw);
        assert_eq!(v["temphighalarm"], Value::String("-inf".into()));
        assert_eq!(v["vcchighalarm"], Value::String("inf".into()));
        assert_eq!(v["txbiashighalarm"], Value::String("nan".into()));
    }

    #[test]
    fn quoted_field_text_is_untouched() {
        // The words must only be rewritten as bare number tokens, never inside strings.
        let raw = r#"{"note": "value is -Infinity or NaN", "x": -Infinity}"#;
        let v = parse(raw);
        assert_eq!(v["note"], Value::String("value is -Infinity or NaN".into()));
        assert_eq!(v["x"], Value::String("-inf".into()));
    }

    #[test]
    fn escaped_quote_inside_string_does_not_end_it_early() {
        let raw = r#"{"note": "a \"quoted -Infinity\" word", "x": Infinity}"#;
        let v = parse(raw);
        assert_eq!(
            v["note"],
            Value::String(r#"a "quoted -Infinity" word"#.into())
        );
        assert_eq!(v["x"], Value::String("inf".into()));
    }

    #[test]
    fn finite_and_ordinary_json_is_byte_identical() {
        // Enriched / test-written thresholds are finite and must be preserved exactly.
        let raw = r#"{"temphighalarm": 75.0, "temphighwarning": 70, "vendor": "ACME", "ok": true, "n": null}"#;
        assert_eq!(sanitize_nonfinite_json(raw), raw);
        let v = parse(raw);
        assert_eq!(v["temphighalarm"], serde_json::json!(75.0));
        assert_eq!(v["temphighwarning"], serde_json::json!(70));
        assert_eq!(v["vendor"], Value::String("ACME".into()));
    }

    #[test]
    fn negative_finite_numbers_are_not_confused_with_neg_infinity() {
        let raw = r#"{"a": -40.0, "b": -Infinity}"#;
        let v = parse(raw);
        assert_eq!(v["a"], serde_json::json!(-40.0));
        assert_eq!(v["b"], Value::String("-inf".into()));
    }

    #[test]
    fn sanitized_nonfinite_renders_to_python_str_field_text() {
        // The posted STATE_DB field text must match the Python daemon's `str(value)`.
        let raw = r#"{"temphighalarm": -Infinity, "vcchighalarm": Infinity, "txpower": NaN}"#;
        let v = parse(raw);
        let obj = v.as_object().unwrap();
        assert_eq!(value_to_py_str(&obj["temphighalarm"]), "-inf");
        assert_eq!(value_to_py_str(&obj["vcchighalarm"]), "inf");
        assert_eq!(value_to_py_str(&obj["txpower"]), "nan");
    }

    #[test]
    fn empty_and_no_token_inputs_are_stable() {
        assert_eq!(sanitize_nonfinite_json(""), "");
        assert_eq!(sanitize_nonfinite_json("{}"), "{}");
        assert_eq!(sanitize_nonfinite_json(r#"{"a": 1}"#), r#"{"a": 1}"#);
    }
}
