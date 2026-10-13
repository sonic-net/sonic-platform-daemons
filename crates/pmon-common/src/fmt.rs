//! STATE_DB field formatters matching Python's `str()` semantics.
//!
//! Every TEMPERATURE_INFO and FAN_INFO field that thermalctld writes goes
//! through `str(value)` in Python; this module reproduces that output so
//! that `show platform temperature`, `show platform fan`, and any downstream
//! telemetry consumers see identical strings from both the Python and Rust
//! daemons -- for every value a sensor reports.  [`float`] says where the
//! match ends.
//!
//! Two Python quirks we must reproduce:
//!  * `str(45.0)` → `"45.0"` (always has a decimal point for floats)
//!  * `str(True)` → `"True"` (capital-T, not `"true"`)
//!
//! Additionally, `platform_api::Threshold` preserves whether the original
//! Python value was `int` or `float`:
//!  * `Threshold::Int(105)` → `"105"`   (matching `str(int(105))`)
//!  * `Threshold::Float(105.0)` → `"105.0"` (matching `str(float(105.0))`)

use platform_api::Threshold;

/// The sentinel written whenever a platform API call yields nothing useful.
pub const NOT_AVAILABLE: &str = "N/A";

// ── Threshold ─────────────────────────────────────────────────────────────────

/// Format an optional threshold for STATE_DB.
///
/// `None` → `"N/A"`, `Int(105)` → `"105"`, `Float(105.0)` → `"105.0"`.
pub fn threshold(t: Option<Threshold>) -> String {
    match t {
        None => NOT_AVAILABLE.to_string(),
        Some(Threshold::Int(v)) => v.to_string(),
        Some(Threshold::Float(v)) => float(v),
    }
}

// ── Optional reading ──────────────────────────────────────────────────────────

/// Format an optional reading -- a temperature, a voltage, a current.
///
/// Whatever the quantity, Python holds it as a `float` and writes `str()` of
/// it, so an integral reading carries its point: `Some(45.0)` → `"45.0"`.
pub fn opt_float(v: Option<f64>) -> String {
    v.map(float).unwrap_or_else(|| NOT_AVAILABLE.to_string())
}

// ── Float / int formatting ────────────────────────────────────────────────────

/// Format like Python's `str(float)`.
///
/// The key invariant: integral floats always carry a decimal point.
///  * `str(45.0)` = `"45.0"` (not `"45"`)
///  * `str(36.5)` = `"36.5"`
///
/// Identical to Python for zero, NaN, the infinities and every
/// `1e-4 <= |v| < 1e16` -- both print the shortest digits that read back as
/// the same float, and that range covers any temperature, speed, voltage or
/// threshold a platform reports.  Outside it Python switches to exponent form
/// (`str(1e16)` is `"1e+16"`, `str(5e-05)` is `"5e-05"`) and this does not:
/// Rust's `Display` never emits an exponent, so those come out as
/// `"10000000000000000"` and `"0.00005"`.  Not reproduced, because nothing
/// published goes there.
pub fn float(v: f64) -> String {
    // Python spells these in lower case and Rust's Display does not, and the
    // contract here is exact `str()` parity: a provider that hands back a NaN
    // temperature would otherwise publish `NaN` where Python publishes `nan`.
    if v.is_nan() {
        return "nan".to_string();
    }
    if v.is_infinite() {
        return if v > 0.0 { "inf".to_string() } else { "-inf".to_string() };
    }
    if v.fract() == 0.0 && v.abs() < 1e16 {
        format!("{v:.1}")
    } else {
        format!("{v}")
    }
}

// ── Boolean ───────────────────────────────────────────────────────────────────

/// Format like Python's `str(bool)`.
pub fn bool(v: bool) -> String {
    if v {
        "True".to_string()
    } else {
        "False".to_string()
    }
}

/// A bool the way psud's PSU_INFO row spells it: `"true"` / `"false"`.
///
/// The same row also carries `is_replaceable` and `power_overload` through
/// [`bool`], which spells them `"True"` / `"False"` (`str()` against
/// `'true' if ... else 'false'`, both in
/// `psud:DaemonPsud._update_single_power_entity_data`).  Two spellings in one
/// hash is not a thing worth defending, but `show platform psustatus` and the
/// PSU sensors in sonic-snmpagent both parse what is there today.
pub fn lower_bool(v: bool) -> String {
    if v { "true" } else { "false" }.to_string()
}

/// Format an optional bool; `None` → `"N/A"`.
pub fn opt_bool(v: Option<bool>) -> String {
    v.map(bool).unwrap_or_else(|| NOT_AVAILABLE.to_string())
}

// ── Integer / speed ───────────────────────────────────────────────────────────

/// Format an optional `u32` (fan speed %, target speed %).
pub fn opt_u32(v: Option<u32>) -> String {
    v.map(|n| n.to_string()).unwrap_or_else(|| NOT_AVAILABLE.to_string())
}

/// Format a position, falling back to what the calling daemon would have used.
///
/// `fallback` is not a nicety: `DeviceBase.get_position_in_parent` documents -1
/// for "cannot determine" and no daemon uses it.  psud passes 0
/// (`psud:DaemonPsud._update_single_psu_entity_info`); thermalctld and
/// sensormond pass the 1-based index of the device in the loop publishing it
/// (`thermalctld:update_entity_info`,
/// `thermalctld:FanUpdater._refresh_fan_drawer_status`,
/// `thermalctld:FanUpdater._refresh_fan_status`,
/// `thermalctld:TemperatureUpdater._collect_thermals`,
/// `sensormond:update_entity_info`).  Baking any one of those into the bridge
/// would be wrong for the other two, so the caller supplies it and the row only
/// says whether the platform answered.
///
/// Signed because the -1 the base class documents needs to stay expressible for
/// a caller that does want it.
pub fn position(p: Option<i32>, fallback: i32) -> String {
    p.unwrap_or(fallback).to_string()
}

// ── String / Option<String> ───────────────────────────────────────────────────

/// Format an optional string; `None` → `"N/A"`.
pub fn opt_str(v: &Option<String>) -> String {
    v.clone().unwrap_or_else(|| NOT_AVAILABLE.to_string())
}

// ── Timestamp ─────────────────────────────────────────────────────────────────

/// The timestamp every pmon table carries.
///
/// Matches the `strftime("%Y%m%d %H:%M:%S")` the Python daemons write --
/// thermalctld through `datetime.now()`, sensormond through `time.strftime()`
/// -- e.g. `"20260811 14:30:00"`.
pub fn timestamp() -> String {
    chrono::Local::now().format("%Y%m%d %H:%M:%S").to_string()
}

/// PSU_INFO's timestamp, which is a Unix epoch rather than a date.
///
/// `psud:DaemonPsud._update_single_power_entity_data` writes
/// `str(datetime.now().timestamp())` where every other pmon table writes a
/// formatted date.  Both Python's `str(float)` and Rust's `Display` print the
/// shortest string that reads back as the same double, so the two agree without
/// rounding either.
pub fn epoch_timestamp() -> String {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    float(now)
}

/// Timestamp for the BMC event log, matching Python's
/// `datefmt="%Y-%m-%dT%H:%M:%S"`.
pub fn event_timestamp() -> String {
    chrono::Local::now().format("%Y-%m-%dT%H:%M:%S").to_string()
}

// ── Tests ───────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use platform_api::Threshold;

    /// Python spells the non-finite values in lower case; Rust's `Display`
    /// does not.  A provider handing back a NaN temperature would otherwise
    /// publish `NaN` where the Python daemon publishes `nan`, and this module's
    /// whole contract is that the two strings match.
    #[test]
    fn non_finite_floats_are_spelled_the_way_python_spells_them() {
        assert_eq!(float(f64::NAN), "nan");
        assert_eq!(float(f64::INFINITY), "inf");
        assert_eq!(float(f64::NEG_INFINITY), "-inf");
    }

    #[test]
    fn integral_floats_keep_decimal_point() {
        assert_eq!(float(45.0), "45.0");
        assert_eq!(float(0.0), "0.0");
        assert_eq!(float(-5.0), "-5.0");
    }

    #[test]
    fn fractional_floats_round_trip() {
        assert_eq!(float(45.25), "45.25");
        assert_eq!(float(36.5), "36.5");
    }

    #[test]
    fn threshold_int_no_decimal() {
        assert_eq!(threshold(Some(Threshold::Int(105))), "105");
        assert_eq!(threshold(Some(Threshold::Int(120))), "120");
    }

    #[test]
    fn threshold_float_has_decimal() {
        assert_eq!(threshold(Some(Threshold::Float(63.0))), "63.0");
        assert_eq!(threshold(Some(Threshold::Float(52.5))), "52.5");
    }

    #[test]
    fn threshold_none_is_na() {
        assert_eq!(threshold(None), "N/A");
    }

    #[test]
    fn bool_is_python_cased() {
        assert_eq!(bool(true), "True");
        assert_eq!(bool(false), "False");
    }

    /// psud writes one hash with both spellings in it, and a consumer of
    /// either would break on the other.
    #[test]
    fn a_psu_row_carries_two_spellings_of_a_bool_and_both_matter() {
        assert_eq!(lower_bool(true), "true");
        assert_eq!(lower_bool(false), "false");
        assert_eq!(bool(true), "True");
    }

    /// A date would parse; an epoch is what is there.
    #[test]
    fn the_psu_timestamp_is_an_epoch_not_a_date() {
        let t = epoch_timestamp();
        let secs: f64 = t.parse().expect("str(float) reads back as a float");
        assert!(secs > 1_700_000_000.0, "{t} is not a plausible Unix time");
    }

    /// `N/A` is one string in Python and has to stay one here: a consumer
    /// matching on it cannot tell "unreadable" from "not applicable".
    #[test]
    fn every_absent_value_renders_as_the_same_sentinel() {
        assert_eq!(opt_float(None), NOT_AVAILABLE);
        assert_eq!(threshold(None), NOT_AVAILABLE);
        assert_eq!(opt_bool(None), NOT_AVAILABLE);
        assert_eq!(opt_u32(None), NOT_AVAILABLE);
        assert_eq!(opt_str(&None), NOT_AVAILABLE);
    }

    #[test]
    fn a_position_the_platform_will_not_give_falls_back_to_the_caller() {
        // Each daemon spells the fallback differently and the bridge cannot
        // pick for them: psud publishes 0
        // (`psud:DaemonPsud._update_single_psu_entity_info`) while thermalctld
        // and sensormond publish the device's 1-based index in the loop
        // (`thermalctld:update_entity_info`, `sensormond:update_entity_info`).
        // A single -1 baked in here -- which is what the base class documents
        // and what the first port did -- was wrong for all three, and showed up
        // on slm-111 as PDB rows reading -1 where Python wrote 0.
        assert_eq!(position(None, 0), "0");
        assert_eq!(position(None, 3), "3");
        assert_eq!(position(None, -1), "-1");
    }

    #[test]
    fn a_position_the_platform_does_give_is_used_as_is() {
        assert_eq!(position(Some(2), 99), "2");
        assert_eq!(position(Some(-1), 99), "-1");
    }
}
