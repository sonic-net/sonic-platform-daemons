//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! STATE_DB field formatters matching Python's `str()` semantics.
//!
//! Every TEMPERATURE_INFO and FAN_INFO field that thermalctld writes goes
//! through `str(value)` in Python; this module replicates the exact output so
//! that `show platform temperature`, `show platform fan`, and any downstream
//! telemetry consumers see identical strings from both the Python and Rust
//! daemons.
//!
//! Two Python quirks we must reproduce:
//!  * `str(45.0)` → `"45.0"` (always has a decimal point for floats)
//!  * `str(True)` → `"True"` (capital-T, not `"true"`)
//!
//! Additionally, `platform_traits::Threshold` preserves whether the original
//! Python value was `int` or `float`:
//!  * `Threshold::Int(105)` → `"105"`   (matching `str(int(105))`)
//!  * `Threshold::Float(105.0)` → `"105.0"` (matching `str(float(105.0))`)

use platform_traits::{FanDirection, Threshold};

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

// ── Temperature ───────────────────────────────────────────────────────────────

/// Format an optional temperature reading.
///
/// Temperature is always stored as a Python `float`, so integral temperatures
/// get `.0`: e.g. `Some(45.0)` → `"45.0"`.
pub fn temp(v: Option<f64>) -> String {
    v.map(float).unwrap_or_else(|| NOT_AVAILABLE.to_string())
}

// ── Float / int formatting ────────────────────────────────────────────────────

/// Format like Python's `str(float)`.
///
/// The key invariant: integral floats always carry a decimal point.
///  * `str(45.0)` = `"45.0"` (not `"45"`)
///  * `str(36.5)` = `"36.5"`
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

/// Format an optional bool; `None` → `"N/A"`.
pub fn opt_bool(v: Option<bool>) -> String {
    v.map(bool).unwrap_or_else(|| NOT_AVAILABLE.to_string())
}

// ── Fan direction ─────────────────────────────────────────────────────────────

/// Format fan direction as SONiC platform API expects it: `"intake"`, `"exhaust"`, `"N/A"`.
///
/// The platform base class defines `FAN_DIRECTION_INTAKE = "intake"` (lowercase),
/// so we match that convention.
pub fn direction(d: Option<FanDirection>) -> String {
    match d {
        Some(FanDirection::Intake) => "intake".to_string(),
        Some(FanDirection::Exhaust) => "exhaust".to_string(),
        None => NOT_AVAILABLE.to_string(),
    }
}

// ── Integer / speed ───────────────────────────────────────────────────────────

/// Format an optional `u32` (fan speed %, target speed %).
pub fn opt_u32(v: Option<u32>) -> String {
    v.map(|n| n.to_string()).unwrap_or_else(|| NOT_AVAILABLE.to_string())
}

/// Format a `u32` position as the unsigned string `"1"`, `"2"`, etc.
pub fn position(p: u32) -> String {
    p.to_string()
}

// ── String / Option<String> ───────────────────────────────────────────────────

/// Format an optional string; `None` → `"N/A"`.
pub fn opt_str(v: &Option<String>) -> String {
    v.clone().unwrap_or_else(|| NOT_AVAILABLE.to_string())
}

// ── Timestamp ─────────────────────────────────────────────────────────────────

/// The timestamp format shared by FAN_INFO and TEMPERATURE_INFO.
///
/// Matches Python `datetime.now().strftime("%Y%m%d %H:%M:%S")` used by the
/// Python thermalctld: `"20260811 14:30:00"`.
pub fn timestamp() -> String {
    chrono::Local::now().format("%Y%m%d %H:%M:%S").to_string()
}

// ── Tests ─────────────────────────────────────────────────────────────────────

/// Timestamp for the BMC event log, matching Python's
/// `datefmt="%Y-%m-%dT%H:%M:%S"`.
pub fn event_timestamp() -> String {
    chrono::Local::now().format("%Y-%m-%dT%H:%M:%S").to_string()
}

// ── Tests ───────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    /// Python spells the non-finite values in lower case; Rust's `Display`
    /// does not.  A provider handing back a NaN temperature would otherwise
    /// publish `NaN` where the Python daemon publishes `nan`, and this module's
    /// whole contract is that the two strings match.
    #[test]
    fn non_finite_floats_are_spelled_the_way_python_spells_them() {
        assert_eq!(float(f64::NAN), "nan");
        assert_eq!(float(f64::INFINITY), "inf");
        assert_eq!(float(f64::NEG_INFINITY), "-inf");
        // The finite cases are unchanged by that.
        assert_eq!(float(45.0), "45.0");
        assert_eq!(float(36.5), "36.5");
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

    #[test]
    fn direction_strings_match_sonic_convention() {
        assert_eq!(direction(Some(FanDirection::Intake)), "intake");
        assert_eq!(direction(Some(FanDirection::Exhaust)), "exhaust");
        assert_eq!(direction(None), "N/A");
    }
}
