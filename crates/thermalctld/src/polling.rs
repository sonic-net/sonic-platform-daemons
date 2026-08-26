//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! Per-component polling intervals from `platform.json`.
//!
//! Ports `_parse_platform_json_polling_intervals()` and
//! `_should_update_thermal()` from `thermalctld:128-190`, `:1048-1062`.
//!
//! A value *below* the cycle is the interesting case, not one above it: Python
//! shrinks its own cycle to the fastest interval any component asks for rather
//! than comparing against a fixed 60 seconds, because a gate can only make a
//! component slower.  Every Mellanox device asks for 3 seconds on the ASIC, so
//! this is live on all of them, and [`PollingIntervals::resolve`] is where it
//! happens.

use std::collections::HashMap;
use std::path::Path;
use std::time::{Duration, Instant};

pub const PLATFORM_JSON_FILE: &str = "/usr/share/sonic/platform/platform.json";

/// What `platform.json` asks for, in seconds.
#[derive(Debug, Default, Clone, PartialEq)]
pub struct PollingIntervals {
    /// Throttles the whole fan update.
    pub fan_drawer: Option<f64>,
    /// Throttles PSU thermal collection.
    pub psu: Option<f64>,
    /// Per-thermal, by sensor name.
    pub thermals: HashMap<String, f64>,
    /// What the fan update runs at, once [`resolve`](Self::resolve) has been
    /// called.  Not read from the file.
    pub fan: Option<f64>,
    /// What a thermal with no interval of its own runs at, once
    /// [`resolve`](Self::resolve) has been called.  Not read from the file.
    pub default_thermal: Option<f64>,
}

impl PollingIntervals {
    pub fn load() -> Self {
        Self::load_from(Path::new(PLATFORM_JSON_FILE))
    }

    /// A missing, unreadable or malformed file means "no throttling", which is
    /// what Python's bare `except` amounts to.
    pub fn load_from(path: &Path) -> Self {
        let Ok(text) = std::fs::read_to_string(path) else {
            return Self::default();
        };
        let Ok(root) = serde_json::from_str::<serde_json::Value>(&text) else {
            log::warn!("Ignoring unparsable {}", path.display());
            return Self::default();
        };
        // platform.json nests components under "chassis"; fall back to the root
        // so that a flattened file still works, as Python does.
        let chassis = root.get("chassis").unwrap_or(&root);

        Self {
            fan_drawer: config_entry_interval(chassis, "fan_drawers"),
            psu: config_entry_interval(chassis, "psus"),
            thermals: named_intervals(chassis, "thermals"),
            fan: None,
            default_thermal: None,
        }
    }

    /// Resolve these against the monitor's own cycle, and return the cycle it
    /// should actually run at (`thermalctld:1261-1291`).
    ///
    /// Two things happen, and they are inseparable.  The cycle shrinks to the
    /// fastest interval anything asks for, because [`PollingGate`] can only
    /// slow a component down: a 3 second thermal against a 60 second cycle is
    /// unreachable otherwise, and reads as 60.  Shrinking it would then speed
    /// *everything* up, so components that asked for nothing are pinned to the
    /// original cycle — the fan update always, and a thermal only when some
    /// other thermal named an interval, which is Python's condition and not a
    /// symmetric one.
    pub fn resolve(&mut self, update_interval: f64) -> f64 {
        // Python's fan update takes the faster of the two, and falls back to
        // the cycle it had before any shrinking.
        self.fan = match (self.fan_drawer, self.psu) {
            (Some(a), Some(b)) => Some(a.min(b)),
            (a, b) => a.or(b),
        }
        .or(Some(update_interval));

        self.default_thermal = (!self.thermals.is_empty()).then_some(update_interval);

        let fastest = self
            .fan_drawer
            .into_iter()
            .chain(self.psu)
            .chain(self.thermals.values().copied())
            .fold(f64::INFINITY, f64::min);

        // Python logs the adjustment first and what it parsed second, both at
        // NOTICE (`thermalctld:1288-1298`).
        let adjusted = if fastest < update_interval {
            crate::logging::notice!(
                "Adjusting update interval from {update_interval}s to {}s \
                 based on platform.json polling intervals",
                float(fastest)
            );
            fastest
        } else {
            update_interval
        };

        if self.fan_drawer.is_some() || self.psu.is_some() || !self.thermals.is_empty() {
            crate::logging::notice!(
                "Platform polling intervals: fan_drawer={}, psu={}, thermals={}",
                py_opt(self.fan_drawer),
                py_opt(self.psu),
                py_dict(&self.thermals)
            );
        }
        adjusted
    }
}

use crate::fmt::float;

/// Python prints an absent interval as `None`.
fn py_opt(v: Option<f64>) -> String {
    v.map_or_else(|| "None".to_string(), float)
}

/// Python prints the map as a dict, or the word `default` when it is empty.
///
/// Python's order is `platform.json`'s; a `HashMap` has none, so the keys are
/// sorted.  Every shipped file names one thermal, so the two agree today.
fn py_dict(m: &HashMap<String, f64>) -> String {
    if m.is_empty() {
        return "default".to_string();
    }
    let mut keys: Vec<&String> = m.keys().collect();
    keys.sort();
    let body: Vec<String> =
        keys.iter().map(|k| format!("'{k}': {}", float(m[*k]))).collect();
    format!("{{{}}}", body.join(", "))
}

/// The first entry *without* a `name` is configuration rather than a device;
/// entries after it describe real hardware.  Python stops at the first such
/// entry whether or not it yielded a value.
fn config_entry_interval(chassis: &serde_json::Value, key: &str) -> Option<f64> {
    let entries = chassis.get(key)?.as_array()?;
    for entry in entries {
        if entry.get("name").is_some() {
            continue;
        }
        let val = entry.get("polling_interval")?;
        return as_seconds(val);
    }
    None
}

fn named_intervals(chassis: &serde_json::Value, key: &str) -> HashMap<String, f64> {
    let mut out = HashMap::new();
    let Some(entries) = chassis.get(key).and_then(|v| v.as_array()) else {
        return out;
    };
    for entry in entries {
        let Some(name) = entry.get("name").and_then(|v| v.as_str()) else {
            continue;
        };
        if let Some(secs) = entry.get("polling_interval").and_then(as_seconds) {
            out.insert(name.to_string(), secs);
        }
    }
    out
}

/// `polling_interval` is a number or a string in the wild.  Zero and negatives
/// mean "unset", matching Python's `if val:` test.
///
/// Infinity and NaN mean "unset" too, which Python does not need to say:
/// `f64::from_str` accepts `"inf"`, and `Duration::from_secs_f64` panics on it,
/// so a typo in one vendor `platform.json` would take the daemon down where
/// Python would merely never poll that sensor.  The module doc promises that a
/// malformed file means no throttling, so reject the value here.
fn as_seconds(val: &serde_json::Value) -> Option<f64> {
    let secs = match val {
        serde_json::Value::Number(n) => n.as_f64()?,
        serde_json::Value::String(s) => s.trim().parse().ok()?,
        _ => return None,
    };
    (secs.is_finite() && secs > 0.0 && secs <= u32::MAX as f64).then_some(secs)
}

/// Tracks when each component was last refreshed, and answers whether it is due.
#[derive(Debug, Default)]
pub struct PollingGate {
    last: HashMap<String, Instant>,
}

impl PollingGate {
    pub fn new() -> Self {
        Self::default()
    }

    /// Whether `key` is due at `now`, given its interval.
    ///
    /// `None` means unthrottled — always due, and no timestamp is kept.  A due
    /// component's timestamp is advanced, so this must be called once per cycle
    /// per component, as Python's `_should_update_thermal` is.
    pub fn is_due(&mut self, key: &str, interval: Option<f64>, now: Instant) -> bool {
        let Some(secs) = interval else {
            return true;
        };
        let period = Duration::from_secs_f64(secs);
        match self.last.get(key) {
            // First sight of a component is always due, as Python's default
            // last-update time of 0 makes it.
            None => {
                self.last.insert(key.to_string(), now);
                true
            }
            Some(&last) if now.duration_since(last) >= period => {
                self.last.insert(key.to_string(), now);
                true
            }
            _ => false,
        }
    }

    /// Drop state for components that no longer exist.
    pub fn retain(&mut self, keep: impl Fn(&str) -> bool) {
        self.last.retain(|k, _| keep(k));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn write(json: &str) -> tempfile::NamedTempFile {
        let mut f = tempfile::NamedTempFile::new().unwrap();
        f.write_all(json.as_bytes()).unwrap();
        f
    }

    // ── Python's own formatting ───────────────────────────────────────────

    /// The literal line Python logged on an untouched SN5640:
    /// `Platform polling intervals: fan_drawer=None, psu=None, thermals={'ASIC': 3.0}`
    /// Rust has one float type where Python has `int` and `float`, so `3`
    /// would print as `3` without [`float`] and the two would diverge on a
    /// line an operator greps for.
    #[test]
    fn the_intervals_line_matches_pythons_character_for_character() {
        let i = PollingIntervals {
            thermals: HashMap::from([("ASIC".to_string(), 3.0)]),
            ..Default::default()
        };
        assert_eq!(
            format!(
                "Platform polling intervals: fan_drawer={}, psu={}, thermals={}",
                py_opt(i.fan_drawer),
                py_opt(i.psu),
                py_dict(&i.thermals)
            ),
            "Platform polling intervals: fan_drawer=None, psu=None, thermals={'ASIC': 3.0}"
        );
    }

    /// An empty map is the word `default`, not `{}` — Python picks the string
    /// explicitly rather than letting an empty dict print itself.
    #[test]
    fn no_thermal_intervals_print_as_default() {
        assert_eq!(py_dict(&HashMap::new()), "default");
        assert_eq!(py_opt(Some(12.5)), "12.5");
    }

    /// Several thermals print as one dict, comma separated, as Python's does.
    #[test]
    fn several_thermals_print_as_one_dict() {
        let m = HashMap::from([("ASIC".to_string(), 3.0), ("Ambient".to_string(), 12.5)]);
        assert_eq!(py_dict(&m), "{'ASIC': 3.0, 'Ambient': 12.5}");
    }

    // ── resolve ───────────────────────────────────────────────────────────

    /// The whole point of the pass.  A sensor asking for less than the cycle is
    /// unreachable through the gate alone — it can only slow a component down —
    /// so Python shrinks the cycle instead.  Every Mellanox platform.json asks
    /// for 3 seconds on the ASIC against a 60 second cycle, which is what made
    /// `TEMPERATURE_INFO|ASIC` refresh once a minute on hardware.
    #[test]
    fn a_thermal_faster_than_the_cycle_shrinks_the_cycle() {
        let mut i = PollingIntervals {
            thermals: HashMap::from([("ASIC".to_string(), 3.0)]),
            ..Default::default()
        };
        assert_eq!(i.resolve(60.0), 3.0);
    }

    /// Shrinking the cycle must not speed everything else up with it, so the
    /// fan keeps the cycle the daemon had before the shrink.
    #[test]
    fn shrinking_the_cycle_leaves_the_fan_on_the_original_one() {
        let mut i = PollingIntervals {
            thermals: HashMap::from([("ASIC".to_string(), 3.0)]),
            ..Default::default()
        };
        i.resolve(60.0);
        assert_eq!(i.fan, Some(60.0));
        assert_eq!(i.default_thermal, Some(60.0));
    }

    /// The fan takes the faster of the two entries that can throttle it.
    #[test]
    fn the_fan_takes_the_faster_of_its_two_intervals() {
        let mut i =
            PollingIntervals { fan_drawer: Some(30.0), psu: Some(15.0), ..Default::default() };
        assert_eq!(i.resolve(60.0), 15.0);
        assert_eq!(i.fan, Some(15.0));
    }

    /// A thermal falls back to the original cycle only when *another* thermal
    /// named an interval.  Python's condition is the thermals map alone, not
    /// "anything was configured", so a fan-only file leaves thermals running
    /// every cycle — including a cycle the fan itself shrank.
    #[test]
    fn a_fan_only_file_does_not_pin_thermals() {
        let mut i = PollingIntervals { fan_drawer: Some(5.0), ..Default::default() };
        assert_eq!(i.resolve(60.0), 5.0);
        assert_eq!(i.default_thermal, None);
    }

    /// Nothing configured leaves the cycle alone; the fan is still handed the
    /// cycle rather than nothing, which is a no-op in real time and is what
    /// keeps it on 60 s once some *other* component shrinks the cycle.
    #[test]
    fn an_empty_file_leaves_the_cycle_alone() {
        let mut i = PollingIntervals::default();
        assert_eq!(i.resolve(60.0), 60.0);
        assert_eq!(i.fan, Some(60.0));
        assert_eq!(i.default_thermal, None);
    }

    /// An interval slower than the cycle throttles its own component and
    /// nothing else — the cycle does not grow to meet it.
    #[test]
    fn an_interval_slower_than_the_cycle_does_not_stretch_it() {
        let mut i = PollingIntervals {
            thermals: HashMap::from([("Ambient".to_string(), 300.0)]),
            ..Default::default()
        };
        assert_eq!(i.resolve(60.0), 60.0);
    }

    #[test]
    fn a_missing_file_throttles_nothing() {
        let got = PollingIntervals::load_from(Path::new("/nonexistent/platform.json"));
        assert_eq!(got, PollingIntervals::default());
    }

    #[test]
    fn malformed_json_throttles_nothing() {
        let f = write("{ not json");
        assert_eq!(PollingIntervals::load_from(f.path()), PollingIntervals::default());
    }

    #[test]
    fn config_entry_is_the_one_without_a_name() {
        let f = write(r#"{"chassis": {"fan_drawers": [
            {"polling_interval": 5},
            {"name": "drawer1", "polling_interval": 99}
        ]}}"#);
        assert_eq!(PollingIntervals::load_from(f.path()).fan_drawer, Some(5.0));
    }

    #[test]
    fn a_device_only_list_yields_no_config_interval() {
        let f = write(r#"{"chassis": {"psus": [{"name": "PSU 1", "polling_interval": 7}]}}"#);
        assert_eq!(PollingIntervals::load_from(f.path()).psu, None);
    }

    #[test]
    fn thermals_are_keyed_by_name_and_accept_strings() {
        let f = write(r#"{"chassis": {"thermals": [
            {"name": "ASIC", "polling_interval": "3"},
            {"name": "Ambient", "polling_interval": 12.5},
            {"name": "NoInterval"}
        ]}}"#);
        let got = PollingIntervals::load_from(f.path()).thermals;
        assert_eq!(got.get("ASIC"), Some(&3.0));
        assert_eq!(got.get("Ambient"), Some(&12.5));
        assert!(!got.contains_key("NoInterval"));
    }

    /// `f64::from_str` accepts "inf" and `Duration::from_secs_f64` panics on
    /// it, so a typo in a vendor platform.json would take the daemon down at
    /// the first poll.  An unusable interval means no throttling instead.
    #[test]
    fn a_non_finite_or_absurd_interval_is_unset() {
        for bad in ["inf", "-inf", "NaN", "1e30"] {
            let body = format!(
                r#"{{"chassis": {{"thermals": [{{"name": "ASIC", "polling_interval": "{bad}"}}]}}}}"#
            );
            let f = write(&body);
            assert!(
                PollingIntervals::load_from(f.path()).thermals.is_empty(),
                "{bad} must not become an interval"
            );
        }
    }

    #[test]
    fn zero_means_unset() {
        let f = write(r#"{"chassis": {"thermals": [{"name": "ASIC", "polling_interval": 0}]}}"#);
        assert!(PollingIntervals::load_from(f.path()).thermals.is_empty());
    }

    #[test]
    fn a_flattened_file_still_works() {
        let f = write(r#"{"thermals": [{"name": "ASIC", "polling_interval": 4}]}"#);
        assert_eq!(PollingIntervals::load_from(f.path()).thermals.get("ASIC"), Some(&4.0));
    }

    #[test]
    fn no_interval_is_always_due() {
        let mut gate = PollingGate::new();
        let t = Instant::now();
        assert!(gate.is_due("ASIC", None, t));
        assert!(gate.is_due("ASIC", None, t));
    }

    #[test]
    fn a_throttled_component_is_due_once_per_interval() {
        let mut gate = PollingGate::new();
        let t0 = Instant::now();
        assert!(gate.is_due("ASIC", Some(10.0), t0), "first sight is always due");
        assert!(!gate.is_due("ASIC", Some(10.0), t0 + Duration::from_secs(9)));
        assert!(gate.is_due("ASIC", Some(10.0), t0 + Duration::from_secs(10)));
        assert!(!gate.is_due("ASIC", Some(10.0), t0 + Duration::from_secs(19)));
    }

    /// The case no device exercises today: an interval longer than the cycle.
    #[test]
    fn an_interval_longer_than_the_cycle_skips_cycles() {
        let mut gate = PollingGate::new();
        let t0 = Instant::now();
        let cycle = Duration::from_secs(60);
        assert!(gate.is_due("Slow", Some(180.0), t0));
        assert!(!gate.is_due("Slow", Some(180.0), t0 + cycle));
        assert!(!gate.is_due("Slow", Some(180.0), t0 + cycle * 2));
        assert!(gate.is_due("Slow", Some(180.0), t0 + cycle * 3));
    }

    #[test]
    fn components_are_tracked_separately() {
        let mut gate = PollingGate::new();
        let t0 = Instant::now();
        gate.is_due("a", Some(10.0), t0);
        assert!(gate.is_due("b", Some(10.0), t0));
        assert!(!gate.is_due("a", Some(10.0), t0 + Duration::from_secs(1)));
    }

    #[test]
    fn retain_drops_vanished_components() {
        let mut gate = PollingGate::new();
        let t0 = Instant::now();
        gate.is_due("gone", Some(10.0), t0);
        gate.retain(|k| k != "gone");
        // Forgotten, so due again immediately.
        assert!(gate.is_due("gone", Some(10.0), t0 + Duration::from_secs(1)));
    }

    /// An interval that is neither a number nor a string is not an interval.
    /// platform.json is vendor-authored and a list or an object here would
    /// otherwise be coerced into something.
    #[test]
    fn a_value_that_is_not_a_number_or_a_string_is_no_interval() {
        use serde_json::json;
        assert_eq!(as_seconds(&json!(60)), Some(60.0));
        assert_eq!(as_seconds(&json!("60")), Some(60.0));
        assert_eq!(as_seconds(&json!([60])), None);
        assert_eq!(as_seconds(&json!({"secs": 60})), None);
        assert_eq!(as_seconds(&json!(null)), None);
        assert_eq!(as_seconds(&json!(true)), None);
    }
}
