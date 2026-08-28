//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! Port of TemperatureStatus / TemperatureUpdater.
//!
//! Reads thermal data directly from `PlatformApi` (Mellanox: sysfs) instead
//! of a gRPC client, then writes TEMPERATURE_INFO and PHYSICAL_ENTITY_INFO
//! to STATE_DB.

use std::collections::{HashMap, HashSet};

use platform_traits::{PlatformApi, ThermalInfo};

use std::time::Instant;

use crate::bmc::BmcMirror;
use crate::db::StateDb;
use crate::fmt;
use crate::polling::{PollingGate, PollingIntervals};

/// A jump larger than this between two polls is reported as suspect hardware.
const TEMPERATURE_DIFF_THRESHOLD: f64 = 10.0;

#[derive(Default)]
struct TemperatureStatus {
    temperature: Option<f64>,
    over_temperature: bool,
    under_temperature: bool,
}

impl TemperatureStatus {
    fn set_temperature(&mut self, name: &str, temperature: Option<f64>) {
        let Some(temperature) = temperature else {
            if self.temperature.is_some() {
                log::warn!("Temperature of {name} became unavailable");
                self.temperature = None;
            }
            return;
        };

        if let Some(previous) = self.temperature {
            let diff = (temperature - previous).abs();
            if diff > TEMPERATURE_DIFF_THRESHOLD {
                log::warn!(
                    "Temperature of {} changed too fast, from {} to {}, \
                     please check your hardware",
                    name,
                    fmt::float(previous),
                    fmt::float(temperature)
                );
            }
        }
        self.temperature = Some(temperature);
    }

    fn check_available(&self, temperature: Option<f64>, threshold: Option<f64>, current: bool) -> bool {
        if temperature.is_none() || threshold.is_none() {
            if current {
                log::warn!(
                    "Thermal temperature or threshold became unavailable, \
                     temperature={}, threshold={}",
                    fmt::temp(temperature),
                    fmt::temp(threshold)
                );
            }
            return false;
        }
        true
    }

    fn set_over_temperature(&mut self, temperature: Option<f64>, threshold: Option<f64>) -> bool {
        if !self.check_available(temperature, threshold, self.over_temperature) {
            let old = self.over_temperature;
            self.over_temperature = false;
            return old;
        }
        let status = temperature.unwrap() > threshold.unwrap();
        if status == self.over_temperature {
            return false;
        }
        self.over_temperature = status;
        true
    }

    fn set_under_temperature(&mut self, temperature: Option<f64>, threshold: Option<f64>) -> bool {
        if !self.check_available(temperature, threshold, self.under_temperature) {
            let old = self.under_temperature;
            self.under_temperature = false;
            return old;
        }
        let status = temperature.unwrap() < threshold.unwrap();
        if status == self.under_temperature {
            return false;
        }
        self.under_temperature = status;
        true
    }
}

pub struct TemperatureUpdater {
    status: HashMap<String, TemperatureStatus>,
    /// Names seen in the last cycle; used to remove stale STATE_DB entries.
    known: HashSet<String>,
    /// Tracks when each thermal was last refreshed, for platform.json's
    /// per-thermal polling_interval.
    gate: PollingGate,
    /// On the switch host, every row is teed to the BMC's STATE_DB.
    mirror: Option<BmcMirror>,
}

impl TemperatureUpdater {
    pub fn new() -> Self {
        Self::with_mirror(BmcMirror::new(crate::device_env::is_switch_host()))
    }

    /// The same updater with the BMC mirror supplied.
    ///
    /// `new()` decides whether to build one by reading the platform env conf,
    /// an absolute path; taking the answer as an argument is what lets the tee
    /// below — which rows reach the BMC, and when they are removed — be driven
    /// from a test.
    pub fn with_mirror(mirror: Option<BmcMirror>) -> Self {
        Self {
            status: HashMap::new(),
            known: HashSet::new(),
            gate: PollingGate::new(),
            mirror,
        }
    }

    /// Synchronous update — called once per polling cycle from `Monitor`.
    ///
    /// `stop` is checked between sensors, as Python checks its stopping event
    /// (`thermalctld:1075`), so a shutdown does not have to wait out a pass.
    /// An interrupted pass returns **before** the stale-row sweep below: the
    /// sensors it never reached are not in `available`, and deleting them would
    /// empty TEMPERATURE_INFO of everything after the interruption point.
    pub fn update(
        &mut self,
        platform: &mut dyn PlatformApi,
        db: &StateDb,
        intervals: &PollingIntervals,
        now: Instant,
        stop: &dyn Fn() -> bool,
    ) -> Result<(), Box<dyn std::error::Error>> {
        let thermals = platform.get_thermals()?;

        // PSU *and* PDB thermals share one interval, keyed by parent rather
        // than by sensor: Python decides once per cycle and passes the same
        // `should_update_psu` to both loops.
        let psus_due = self.gate.is_due("psu", intervals.psu, now);

        let mut available = HashSet::with_capacity(thermals.len());
        for thermal in &thermals {
            if stop() {
                return Ok(());
            }
            // A thermal with its own polling_interval is refreshed only when it
            // is due; the rest are refreshed every cycle.  Either way the name
            // stays in `available`, so a throttled sensor is not deleted as
            // stale between refreshes.
            let due = if thermal.parent_name.starts_with("PSU ") || thermal.parent_name.starts_with("PDB ") {
                psus_due
            } else {
                // A thermal that named no interval falls back to the cycle
                // the daemon had before platform.json shrank it, but only when
                // some other thermal did name one (`thermalctld:1275`).
                let interval = intervals
                    .thermals
                    .get(&thermal.name)
                    .copied()
                    .or(intervals.default_thermal);
                self.gate.is_due(&thermal.name, interval, now)
            };
            if due {
                self.refresh(db, thermal);
            }
            available.insert(thermal.name.clone());
        }

        for stale in self.known.difference(&available) {
            if let Err(e) = db.temperature.del(stale) {
                log::warn!("failed to remove {stale} from TEMPERATURE_INFO: {e}");
            }
            if let Some(chassis) = db.chassis_temperature.as_ref() {
                let _ = chassis.del(stale);
            }
            if let Some(m) = self.mirror.as_mut() {
                m.del(stale);
            }
            self.status.remove(stale);
        }
        self.gate.retain(|name| name == "psu" || available.contains(name));
        self.known = available;

        Ok(())
    }

    fn refresh(&mut self, db: &StateDb, thermal: &ThermalInfo) {
        let name = thermal.name.as_str();
        let status = self.status.entry(name.to_string()).or_default();

        let temperature = thermal.temperature;
        let high_threshold = thermal.high_threshold.map(|t| t.as_f64());
        let low_threshold = thermal.low_threshold.map(|t| t.as_f64());

        let mut warning = false;

        // Python guards the whole block the same way (`thermalctld:1123`), which
        // makes the `None` arm of `set_temperature` -- and the "became
        // unavailable" warning inside it -- unreachable there too, and leaves
        // the cached reading standing across a gap.  Reproduced rather than
        // corrected: the contract is that the two daemons publish the same
        // rows, and reaching that arm here would be a seventh divergence.
        if temperature.is_some() {
            status.set_temperature(name, temperature);

            if status.set_over_temperature(temperature, high_threshold) {
                log_status_change(
                    !status.over_temperature,
                    &format!(
                        "High temperature warning cleared: {} temperature restored to {}C, \
                         high threshold {}C",
                        name,
                        fmt::temp(temperature),
                        fmt::temp(high_threshold)
                    ),
                    &format!(
                        "High temperature warning: {} current temperature {}C, \
                         high threshold {}C",
                        name,
                        fmt::temp(temperature),
                        fmt::temp(high_threshold)
                    ),
                );
            }
            warning |= status.over_temperature;

            if status.set_under_temperature(temperature, low_threshold) {
                log_status_change(
                    !status.under_temperature,
                    &format!(
                        "Low temperature warning cleared: {} temperature restored to {}C, \
                         low threshold {}C",
                        name,
                        fmt::temp(temperature),
                        fmt::temp(low_threshold)
                    ),
                    &format!(
                        "Low temperature warning: {} current temperature {}C, \
                         low threshold {}C",
                        name,
                        fmt::temp(temperature),
                        fmt::temp(low_threshold)
                    ),
                );
            }
            warning |= status.under_temperature;
        }

        db.set_entity_info(name, &thermal.parent_name, &fmt::position(thermal.position_in_parent));

        let fvs = thermal_row(thermal, temperature, warning);

        if let Err(e) = db.temperature.set(name, &fvs) {
            log::warn!("Failed to update thermal status for {name} - {e}");
        }

        // On the switch host the same row is teed to the BMC's STATE_DB.
        if let Some(m) = self.mirror.as_mut() {
            m.set(name, &fvs);
        }

        // On a modular chassis or a SmartSwitch DPU the same row also goes to
        // the slot-suffixed table, alongside the unsuffixed one rather than
        // instead of it.
        if let Some(chassis) = db.chassis_temperature.as_ref() {
            if let Err(e) = chassis.set(name, &fvs) {
                log::warn!("Failed to update chassis thermal status for {name} - {e}");
            }
        }
    }
}

/// The `TEMPERATURE_INFO` row.  Field names and order are part of requirement
/// 1a, and so is the int-versus-float threshold formatting.
///
/// A sensor whose temperature could not be read reports `N/A` for everything
/// derived from it.  Python reads the recorded extremes and all four
/// thresholds only under `if temperature != NOT_AVAILABLE:`
/// (`thermalctld:1159-1181`), so publishing thresholds beside an unreadable
/// temperature would be a row Python never writes.
fn thermal_row(thermal: &ThermalInfo, temperature: Option<f64>, warning: bool) -> [(&'static str, String); 10] {
    let na = || fmt::NOT_AVAILABLE.to_string();
    let readable = temperature.is_some();
    let when = |v: String| if readable { v } else { na() };
    [
        ("temperature", fmt::temp(temperature)),
        ("minimum_temperature", when(fmt::temp(thermal.min_recorded))),
        ("maximum_temperature", when(fmt::temp(thermal.max_recorded))),
        ("high_threshold", when(fmt::threshold(thermal.high_threshold))),
        ("low_threshold", when(fmt::threshold(thermal.low_threshold))),
        ("warning_status", fmt::bool(warning)),
        (
            "critical_high_threshold",
            when(fmt::threshold(thermal.high_critical_threshold)),
        ),
        (
            "critical_low_threshold",
            when(fmt::threshold(thermal.low_critical_threshold)),
        ),
        ("is_replaceable", fmt::bool(thermal.is_replaceable)),
        ("timestamp", fmt::timestamp()),
    ]
}

fn log_status_change(normal: bool, normal_log: &str, abnormal_log: &str) {
    if normal {
        crate::logging::notice!("{normal_log}");
    } else {
        log::warn!("{abnormal_log}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn over_temperature_trips_and_clears_once() {
        let mut status = TemperatureStatus::default();
        assert!(status.set_over_temperature(Some(80.0), Some(70.0)));
        assert!(status.over_temperature);
        assert!(!status.set_over_temperature(Some(85.0), Some(70.0)));
        assert!(status.set_over_temperature(Some(60.0), Some(70.0)));
        assert!(!status.over_temperature);
    }

    #[test]
    fn missing_threshold_clears_rather_than_holds() {
        let mut status = TemperatureStatus::default();
        status.set_over_temperature(Some(80.0), Some(70.0));
        assert!(status.over_temperature);
        assert!(status.set_over_temperature(Some(80.0), None));
        assert!(!status.over_temperature);
    }

    #[test]
    fn unavailable_temperature_resets_cached_reading() {
        let mut status = TemperatureStatus::default();
        status.set_temperature("t", Some(40.0));
        assert_eq!(status.temperature, Some(40.0));
        status.set_temperature("t", None);
        assert_eq!(status.temperature, None);
    }

    use platform_traits::Threshold;

    fn thermal(name: &str) -> ThermalInfo {
        ThermalInfo {
            name: name.to_string(),
            parent_name: "chassis 1".to_string(),
            position_in_parent: 1,
            temperature: Some(45.0),
            min_recorded: Some(40.0),
            max_recorded: Some(50.0),
            high_threshold: Some(Threshold::Int(105)),
            low_threshold: None,
            high_critical_threshold: Some(Threshold::Int(120)),
            low_critical_threshold: None,
            is_replaceable: false,
        }
    }

    fn keys(row: &[(&'static str, String); 10]) -> Vec<&'static str> {
        row.iter().map(|(k, _)| *k).collect()
    }

    fn val(row: &[(&'static str, String); 10], key: &str) -> String {
        row.iter().find(|(k, _)| *k == key).unwrap().1.clone()
    }

    #[test]
    fn the_thermal_row_has_the_expected_fields_in_order() {
        assert_eq!(
            keys(&thermal_row(&thermal("ASIC"), Some(45.0), false)),
            [
                "temperature",
                "minimum_temperature",
                "maximum_temperature",
                "high_threshold",
                "low_threshold",
                "warning_status",
                "critical_high_threshold",
                "critical_low_threshold",
                "is_replaceable",
                "timestamp"
            ]
        );
    }

    /// Python's str() distinguishes int from float, and downstream consumers
    /// see the difference: an ASIC default writes "105", a sysfs-derived
    /// threshold "43.0".
    /// The row a healthy sensor publishes — every field, by value.
    ///
    /// The N/A cases below each pin one field going away; this pins what they
    /// deviate from.  It is also where the int/float distinction shows: Python
    /// writes `str(int(105))` as `"105"` and `str(float(45.0))` as `"45.0"`, so
    /// a threshold and a temperature that happen to be equal are still spelled
    /// differently.
    #[test]
    fn a_healthy_sensor_publishes_every_field_by_value() {
        let th = thermal("ASIC");
        let row = thermal_row(&th, Some(45.0), false);

        assert_eq!(val(&row, "temperature"), "45.0", "a float keeps its point");
        assert_eq!(val(&row, "minimum_temperature"), "40.0");
        assert_eq!(val(&row, "maximum_temperature"), "50.0");
        assert_eq!(val(&row, "high_threshold"), "105", "an int does not gain one");
        assert_eq!(val(&row, "low_threshold"), "N/A");
        assert_eq!(val(&row, "critical_high_threshold"), "120");
        assert_eq!(val(&row, "critical_low_threshold"), "N/A");
        assert_eq!(val(&row, "warning_status"), "False");
        assert_eq!(val(&row, "is_replaceable"), "False");
        let ts = val(&row, "timestamp");
        assert_eq!(ts.len(), 17, "{ts:?}");
        assert_eq!(keys(&row).len(), 10);
    }

    /// A whole degree still carries its decimal point, because Python's
    /// `str(float)` does.  `"45"` and `"45.0"` are different strings to
    /// `show platform temperature`.
    #[test]
    fn a_whole_degree_keeps_its_decimal_point() {
        let mut th = thermal("ASIC");
        th.temperature = Some(45.0);
        th.min_recorded = Some(0.0);
        let row = thermal_row(&th, Some(45.0), false);
        assert_eq!(val(&row, "temperature"), "45.0");
        assert_eq!(val(&row, "minimum_temperature"), "0.0", "not \"0\"");
    }

    #[test]
    fn an_int_threshold_is_not_written_as_a_float() {
        let mut th = thermal("ASIC");
        assert_eq!(val(&thermal_row(&th, Some(45.0), false), "high_threshold"), "105");
        th.high_threshold = Some(Threshold::Float(43.0));
        assert_eq!(val(&thermal_row(&th, Some(45.0), false), "high_threshold"), "43.0");
    }

    /// A sensor whose temperature could not be read reports N/A for everything
    /// derived from it, even where the threshold files are perfectly readable:
    /// Python reads them only under `if temperature != NOT_AVAILABLE:`.
    #[test]
    fn an_unreadable_sensor_publishes_no_thresholds() {
        let th = thermal("ASIC"); // thresholds and min/max all present
        let row = thermal_row(&th, None, false);
        for field in [
            "temperature",
            "minimum_temperature",
            "maximum_temperature",
            "high_threshold",
            "low_threshold",
            "critical_high_threshold",
            "critical_low_threshold",
        ] {
            assert_eq!(val(&row, field), "N/A", "{field} should be N/A without a temperature");
        }
        assert_eq!(val(&row, "is_replaceable"), "False", "and the rest is still written");
        assert_eq!(keys(&row).len(), 10, "the field set does not change");
    }

    #[test]
    fn an_absent_reading_is_not_available() {
        let mut th = thermal("ASIC");
        th.min_recorded = None;
        let row = thermal_row(&th, None, false);
        assert_eq!(val(&row, "temperature"), "N/A");
        assert_eq!(val(&row, "minimum_temperature"), "N/A");
        assert_eq!(val(&row, "low_threshold"), "N/A");
    }

    #[test]
    fn the_warning_status_comes_from_the_caller_not_the_thresholds() {
        let th = thermal("ASIC");
        assert_eq!(val(&thermal_row(&th, Some(45.0), false), "warning_status"), "False");
        assert_eq!(val(&thermal_row(&th, Some(200.0), true), "warning_status"), "True");
    }

    // ── The write path, against a dictionary-backed database ──────────────────

    use crate::db::mock::MockDb;

    /// A platform that reports exactly the thermals it is given.
    struct FakePlatform {
        thermals: Vec<ThermalInfo>,
    }

    impl platform_traits::PlatformApi for FakePlatform {
        fn chassis_info(&self) -> Result<platform_traits::ChassisInfo, platform_traits::PlatformError> {
            Ok(platform_traits::ChassisInfo {
                is_modular_chassis: false,
                is_smartswitch: false,
                is_dpu: false,
                is_liquid_cooled: false,
                slot_or_dpu_id: None,
            })
        }
        fn get_thermals(&mut self) -> Result<Vec<ThermalInfo>, platform_traits::PlatformError> {
            Ok(self.thermals.clone())
        }
        fn get_fan_drawers(&self) -> Result<Vec<platform_traits::FanDrawerInfo>, platform_traits::PlatformError> {
            Ok(Vec::new())
        }
        fn get_fans(&self) -> Result<Vec<platform_traits::FanInfo>, platform_traits::PlatformError> {
            Ok(Vec::new())
        }
        fn set_fan_led(&mut self, _: &str, _: &str, _: &str) -> Result<(), platform_traits::PlatformError> {
            Ok(())
        }
        fn get_thermal_manager(&self) -> Box<dyn platform_traits::ThermalManager> {
            unimplemented!("not needed by these tests")
        }
    }

    fn run(
        u: &mut TemperatureUpdater,
        m: &MockDb,
        thermals: Vec<ThermalInfo>,
        intervals: &PollingIntervals,
        now: Instant,
    ) {
        let mut p = FakePlatform { thermals };
        u.update(&mut p, &m.db, intervals, now, &never_stop).unwrap();
    }

    #[test]
    fn a_thermal_is_written_with_its_entity_info() {
        let m = MockDb::new(false);
        let mut u = TemperatureUpdater::new();
        run(
            &mut u,
            &m,
            vec![thermal("ASIC")],
            &PollingIntervals::default(),
            Instant::now(),
        );
        assert_eq!(m.temperature.field("ASIC", "temperature").as_deref(), Some("45.0"));
        assert_eq!(
            m.physical_entity.field("ASIC", "parent_name").as_deref(),
            Some("chassis 1")
        );
    }

    /// A sensor that stops being reported is removed, not left stale.
    #[test]
    fn a_vanished_thermal_is_deleted() {
        let m = MockDb::new(false);
        let mut u = TemperatureUpdater::new();
        let now = Instant::now();
        run(
            &mut u,
            &m,
            vec![thermal("ASIC"), thermal("Ambient")],
            &PollingIntervals::default(),
            now,
        );
        assert_eq!(m.temperature.len(), 2);
        run(&mut u, &m, vec![thermal("ASIC")], &PollingIntervals::default(), now);
        assert_eq!(m.temperature.keys(), ["ASIC"]);
    }

    /// The slot-suffixed table gets the same row, alongside the unsuffixed one.
    #[test]
    fn the_chassis_table_gets_the_same_row_and_the_same_deletes() {
        let m = MockDb::new(true);
        let mut u = TemperatureUpdater::new();
        let now = Instant::now();
        run(
            &mut u,
            &m,
            vec![thermal("ASIC"), thermal("Ambient")],
            &PollingIntervals::default(),
            now,
        );
        assert_eq!(m.chassis_temperature.row("ASIC"), m.temperature.row("ASIC"));
        run(&mut u, &m, vec![thermal("ASIC")], &PollingIntervals::default(), now);
        assert_eq!(m.chassis_temperature.keys(), ["ASIC"]);
    }

    /// A throttled sensor is not refreshed, but it must not be deleted either.
    #[test]
    fn a_throttled_thermal_is_kept_but_not_refreshed() {
        let m = MockDb::new(false);
        let mut u = TemperatureUpdater::new();
        let mut intervals = PollingIntervals::default();
        intervals.thermals.insert("ASIC".to_string(), 100.0);
        let t0 = Instant::now();

        run(&mut u, &m, vec![thermal("ASIC")], &intervals, t0);
        assert_eq!(m.temperature.field("ASIC", "temperature").as_deref(), Some("45.0"));

        let mut hot = thermal("ASIC");
        hot.temperature = Some(90.0);
        run(
            &mut u,
            &m,
            vec![hot],
            &intervals,
            t0 + std::time::Duration::from_secs(1),
        );
        assert_eq!(
            m.temperature.field("ASIC", "temperature").as_deref(),
            Some("45.0"),
            "not due, so the row is untouched"
        );
        assert_eq!(m.temperature.len(), 1, "and not deleted as stale");
    }

    /// Python passes the same `should_update_psu` to the PDB loop, so a PDB
    /// sensor is throttled by the PSU interval rather than by its own.
    #[test]
    fn pdb_thermals_share_the_psu_interval() {
        let m = MockDb::new(false);
        let mut u = TemperatureUpdater::new();
        let intervals = PollingIntervals {
            psu: Some(100.0),
            ..Default::default()
        };
        let t0 = Instant::now();
        let mut pdb = thermal("PDB-1 Temp");
        pdb.parent_name = "PDB 1".to_string();

        run(&mut u, &m, vec![pdb.clone()], &intervals, t0);
        let mut hot = pdb.clone();
        hot.temperature = Some(90.0);
        run(
            &mut u,
            &m,
            vec![hot],
            &intervals,
            t0 + std::time::Duration::from_secs(1),
        );
        assert_eq!(
            m.temperature.field("PDB-1 Temp", "temperature").as_deref(),
            Some("45.0")
        );
    }

    #[test]
    fn psu_thermals_share_one_interval() {
        let m = MockDb::new(false);
        let mut u = TemperatureUpdater::new();
        let intervals = PollingIntervals {
            psu: Some(100.0),
            ..Default::default()
        };
        let t0 = Instant::now();
        let mut psu = thermal("PSU 1 Temp");
        psu.parent_name = "PSU 1".to_string();

        run(&mut u, &m, vec![psu.clone()], &intervals, t0);
        assert_eq!(
            m.temperature.field("PSU 1 Temp", "temperature").as_deref(),
            Some("45.0")
        );

        let mut hot = psu.clone();
        hot.temperature = Some(90.0);
        run(
            &mut u,
            &m,
            vec![hot],
            &intervals,
            t0 + std::time::Duration::from_secs(1),
        );
        assert_eq!(
            m.temperature.field("PSU 1 Temp", "temperature").as_deref(),
            Some("45.0")
        );
    }

    /// A thermal that named no interval of its own runs on the cycle the daemon
    /// had before `platform.json` shrank it — not on the shrunken cycle.  Every
    /// Mellanox device asks for 3 s on the ASIC, so without this every other
    /// sensor would be republished twenty times a minute.
    #[test]
    fn a_thermal_without_its_own_interval_falls_back_to_the_default() {
        let m = MockDb::new(false);
        let mut u = TemperatureUpdater::new();
        let intervals = PollingIntervals {
            thermals: std::collections::HashMap::from([("ASIC".to_string(), 3.0)]),
            default_thermal: Some(60.0),
            ..Default::default()
        };
        let t0 = Instant::now();

        run(&mut u, &m, vec![thermal("Ambient")], &intervals, t0);
        assert_eq!(m.temperature.field("Ambient", "temperature").as_deref(), Some("45.0"));

        // Ten seconds on: due under the 3 s the ASIC asked for, not due under
        // the 60 s this sensor inherits.
        let mut hot = thermal("Ambient");
        hot.temperature = Some(90.0);
        run(
            &mut u,
            &m,
            vec![hot],
            &intervals,
            t0 + std::time::Duration::from_secs(10),
        );
        assert_eq!(m.temperature.field("Ambient", "temperature").as_deref(), Some("45.0"));
    }

    // ── Transitions and the BMC tee ───────────────────────────────────────

    use crate::bmc::{BmcMirror, OpenTable};
    use crate::db::mock::MockTable;
    use crate::db::TableLike;

    /// A mirror wired to a table the test can read back.
    fn mirror_to(remote: MockTable) -> BmcMirror {
        let open: OpenTable = Box::new(move |_| Ok(Box::new(remote.clone()) as Box<dyn TableLike>));
        BmcMirror::with_opener("10.0.0.1", open)
    }

    /// Under temperature is its own alarm with its own threshold, not the
    /// inverse of the over-temperature one: a sensor can be below its low
    /// threshold while nowhere near its high one.
    #[test]
    fn an_under_temperature_alarm_tracks_the_low_threshold() {
        let mut st = TemperatureStatus::default();
        // 20 degrees against a low threshold of 25 is under.
        assert!(st.set_under_temperature(Some(20.0), Some(25.0)), "entering is a change");
        assert!(st.under_temperature);
        assert!(!st.set_under_temperature(Some(20.0), Some(25.0)), "staying is not");

        assert!(st.set_under_temperature(Some(30.0), Some(25.0)), "leaving is a change");
        assert!(!st.under_temperature);
    }

    /// A threshold that goes away clears the alarm rather than holding it: the
    /// daemon cannot tell whether the sensor is still cold.
    #[test]
    fn losing_the_low_threshold_clears_the_under_temperature_alarm() {
        let mut st = TemperatureStatus::default();
        st.set_under_temperature(Some(20.0), Some(25.0));
        assert!(st.under_temperature);
        assert!(st.set_under_temperature(Some(20.0), None), "clearing is a change");
        assert!(!st.under_temperature);
    }

    /// On the switch host every row is teed to the BMC's STATE_DB, with the
    /// same fields — the BMC has no sensors of its own and reads this mirror.
    #[test]
    fn every_row_is_teed_to_the_bmc_mirror() {
        let remote = MockTable::new();
        let m = MockDb::new(false);
        let mut u = TemperatureUpdater::with_mirror(Some(mirror_to(remote.clone())));
        let mut p = FakePlatform {
            thermals: vec![thermal("ASIC")],
        };

        u.update(&mut p, &m.db, &PollingIntervals::default(), Instant::now(), &never_stop)
            .unwrap();

        assert_eq!(remote.field("ASIC", "temperature").as_deref(), Some("45.0"));
        assert_eq!(
            remote.field("ASIC", "high_threshold").as_deref(),
            Some("105"),
            "the mirrored row is the same row, not a summary of it"
        );
    }

    /// A sensor that disappears is removed from the mirror too.  Leaving it
    /// there would have the BMC watching a temperature nothing refreshes —
    /// the same hazard as leaving hw-management-tc running after a stop.
    #[test]
    fn a_vanished_sensor_is_removed_from_the_mirror_too() {
        let remote = MockTable::new();
        let m = MockDb::new(false);
        let mut u = TemperatureUpdater::with_mirror(Some(mirror_to(remote.clone())));
        let mut p = FakePlatform {
            thermals: vec![thermal("ASIC"), thermal("PSU-1 Temp")],
        };
        let now = Instant::now();

        u.update(&mut p, &m.db, &PollingIntervals::default(), now, &never_stop)
            .unwrap();
        assert_eq!(remote.len(), 2);

        p.thermals.retain(|t| t.name == "ASIC");
        u.update(&mut p, &m.db, &PollingIntervals::default(), now, &never_stop)
            .unwrap();
        assert_eq!(remote.keys(), vec!["ASIC"], "the PSU sensor is gone from the BMC too");
    }

    /// A platform that is not the switch host has no mirror at all, and the
    /// update path must not care.
    #[test]
    fn an_updater_without_a_mirror_still_writes_locally() {
        let m = MockDb::new(false);
        let mut u = TemperatureUpdater::with_mirror(None);
        let mut p = FakePlatform {
            thermals: vec![thermal("ASIC")],
        };
        u.update(&mut p, &m.db, &PollingIntervals::default(), Instant::now(), &never_stop)
            .unwrap();
        assert_eq!(m.temperature.field("ASIC", "temperature").as_deref(), Some("45.0"));
    }

    // ── Transitions through the updater, and the write failures ───────────

    /// `warning_status` is the field a consumer watches, and it is the OR of
    /// both alarms: a sensor below its low threshold warns just as one above
    /// its high threshold does.
    #[test]
    fn crossing_either_threshold_raises_the_warning_status() {
        let m = MockDb::new(false);
        let mut u = TemperatureUpdater::with_mirror(None);
        let mut th = thermal("ASIC");
        th.low_threshold = Some(Threshold::Int(10));
        let now = Instant::now();

        let mut p = FakePlatform {
            thermals: vec![th.clone()],
        };
        u.update(&mut p, &m.db, &PollingIntervals::default(), now, &never_stop)
            .unwrap();
        assert_eq!(m.temperature.field("ASIC", "warning_status").as_deref(), Some("False"));

        // Above the high threshold.
        p.thermals[0].temperature = Some(110.0);
        u.update(&mut p, &m.db, &PollingIntervals::default(), now, &never_stop)
            .unwrap();
        assert_eq!(m.temperature.field("ASIC", "warning_status").as_deref(), Some("True"));

        // Back into the band.
        p.thermals[0].temperature = Some(45.0);
        u.update(&mut p, &m.db, &PollingIntervals::default(), now, &never_stop)
            .unwrap();
        assert_eq!(m.temperature.field("ASIC", "warning_status").as_deref(), Some("False"));

        // Below the low threshold.
        p.thermals[0].temperature = Some(5.0);
        u.update(&mut p, &m.db, &PollingIntervals::default(), now, &never_stop)
            .unwrap();
        assert_eq!(
            m.temperature.field("ASIC", "warning_status").as_deref(),
            Some("True"),
            "a cold sensor warns too"
        );
    }

    /// A sensor whose reading jumps further than the sanity threshold in one
    /// cycle is flagged but still published: the daemon does not know which of
    /// the two readings is wrong, and dropping either would hide a real event.
    #[test]
    fn an_implausible_jump_is_reported_and_the_reading_still_published() {
        let m = MockDb::new(false);
        let mut u = TemperatureUpdater::with_mirror(None);
        let now = Instant::now();
        let mut p = FakePlatform {
            thermals: vec![thermal("ASIC")],
        };

        u.update(&mut p, &m.db, &PollingIntervals::default(), now, &never_stop)
            .unwrap();
        p.thermals[0].temperature = Some(200.0);
        u.update(&mut p, &m.db, &PollingIntervals::default(), now, &never_stop)
            .unwrap();

        assert_eq!(m.temperature.field("ASIC", "temperature").as_deref(), Some("200.0"));
    }

    /// A reading that goes away publishes `N/A`, and a reading that comes back
    /// publishes the new value.
    ///
    /// What it does *not* assert is that the cached reading was cleared: the
    /// guard above (`refresh`, around the `temperature.is_some()` test) means
    /// `set_temperature` never sees `None`, so the pre-gap value survives and
    /// the "changed too fast" check compares against it.  Python does the same,
    /// which is why that is left alone -- but the field this test reads is the
    /// published one, not the cache, and the name used to claim otherwise.
    #[test]
    fn losing_a_reading_publishes_not_available_and_regaining_it_publishes_the_value() {
        let m = MockDb::new(false);
        let mut u = TemperatureUpdater::with_mirror(None);
        let now = Instant::now();
        let mut p = FakePlatform {
            thermals: vec![thermal("ASIC")],
        };

        u.update(&mut p, &m.db, &PollingIntervals::default(), now, &never_stop)
            .unwrap();
        p.thermals[0].temperature = None;
        u.update(&mut p, &m.db, &PollingIntervals::default(), now, &never_stop)
            .unwrap();
        assert_eq!(m.temperature.field("ASIC", "temperature").as_deref(), Some("N/A"));

        p.thermals[0].temperature = Some(45.0);
        u.update(&mut p, &m.db, &PollingIntervals::default(), now, &never_stop)
            .unwrap();
        assert_eq!(m.temperature.field("ASIC", "temperature").as_deref(), Some("45.0"));
    }

    /// A database that refuses writes is logged and the pass carries on: one
    /// unwritable row must not stop the sensors behind it from being published,
    /// and it must not stop the cycle that feeds hw-management-tc.
    #[test]
    fn a_refusing_database_does_not_stop_the_pass() {
        let m = MockDb::new(true);
        m.temperature.fail_writes("redis is down");
        m.chassis_temperature.fail_writes("redis is down");

        let mut u = TemperatureUpdater::with_mirror(None);
        let mut p = FakePlatform {
            thermals: vec![thermal("ASIC"), thermal("PSU-1 Temp")],
        };
        u.update(&mut p, &m.db, &PollingIntervals::default(), Instant::now(), &never_stop)
            .expect("a write failure is not an error the caller sees");

        assert!(m.temperature.is_empty());
        // The entity info goes to a different table, which is still writable.
        assert!(!m.physical_entity.is_empty());
    }

    /// Deleting a sensor that has gone is best-effort for the same reason.
    #[test]
    fn a_failing_delete_does_not_stop_the_pass() {
        let m = MockDb::new(false);
        let mut u = TemperatureUpdater::with_mirror(None);
        let now = Instant::now();
        let mut p = FakePlatform {
            thermals: vec![thermal("ASIC"), thermal("PSU-1 Temp")],
        };
        u.update(&mut p, &m.db, &PollingIntervals::default(), now, &never_stop)
            .unwrap();

        m.temperature.fail_writes("redis is down");
        p.thermals.retain(|t| t.name == "ASIC");
        u.update(&mut p, &m.db, &PollingIntervals::default(), now, &never_stop)
            .expect("a failed delete is not an error either");
    }

    /// A pass that is never told to stop.
    fn never_stop() -> bool {
        false
    }

    // ── An interrupted pass ───────────────────────────────────────────────

    /// The sweep that deletes vanished sensors runs off what this pass reached.
    /// An interrupted pass has reached almost nothing, so running the sweep
    /// would delete every sensor after the interruption point — Python returns
    /// before it for exactly that reason (`thermalctld:1095`).
    #[test]
    fn an_interrupted_pass_does_not_delete_the_sensors_it_never_reached() {
        let m = MockDb::new(false);
        let mut u = TemperatureUpdater::with_mirror(None);
        let now = Instant::now();
        let mut p = FakePlatform {
            thermals: vec![thermal("ASIC"), thermal("PSU-1 Temp"), thermal("Ambient")],
        };

        u.update(&mut p, &m.db, &PollingIntervals::default(), now, &never_stop)
            .unwrap();
        assert_eq!(m.temperature.len(), 3);

        u.update(&mut p, &m.db, &PollingIntervals::default(), now, &|| true)
            .unwrap();
        assert_eq!(
            m.temperature.len(),
            3,
            "all three are still there: {:?}",
            m.temperature.keys()
        );
    }

    #[test]
    fn a_pass_told_to_stop_publishes_nothing() {
        let m = MockDb::new(false);
        let mut u = TemperatureUpdater::with_mirror(None);
        let mut p = FakePlatform {
            thermals: vec![thermal("ASIC")],
        };
        u.update(&mut p, &m.db, &PollingIntervals::default(), Instant::now(), &|| true)
            .unwrap();
        assert!(m.temperature.is_empty());
    }
}
