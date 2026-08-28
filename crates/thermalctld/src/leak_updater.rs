//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! Leak detection.  Ports `LiquidCoolingUpdater` from `thermalctld:590-809`.
//!
//! Runs on its own thread at `liquid_cooling_update_interval` — 0.5 s on the
//! platforms that set it, the fastest loop in the daemon, which is why it does
//! not share the 60 s poll cycle.
//!
//! Three behaviours here are load-bearing:
//!
//! * `SYSTEM_LEAK_STATUS|system` is seeded only when **absent**, so restarting
//!   during a leak does not overwrite the standing status with `None`.
//! * A `MINOR` leak that outlasts its profile's `max_minor_duration_sec`
//!   becomes `CRITICAL`, and `CRITICAL` is logged once per episode however the
//!   sensor got there.
//! * Rows are written **only on change**, and `SYSTEM_LEAK_STATUS` only on a
//!   transition, so a subscriber does not see a write per cycle.

use std::collections::{HashMap, HashSet};
use std::time::Instant;

use platform_traits::{LeakProfile, LeakSensorInfo, LeakSeverity};

use crate::event_log::EventLogger;
use crate::fmt;

/// Fields of one `LIQUID_COOLING_INFO` row, in Python's order.
type SensorRow = Vec<(&'static str, String)>;

/// Everything the updater remembers between cycles.
#[derive(Default)]
pub struct LeakState {
    /// When each sensor started leaking, for the MINOR escalation timer.
    leaking_since: HashMap<String, Instant>,
    /// Sensors already reported faulty, so the error is logged once.
    faulty: HashSet<String>,
    /// Sensors already reported CRITICAL, so it is logged once per episode.
    critical: HashSet<String>,
    /// Last row written per sensor, so unchanged rows are not rewritten.
    last_row: HashMap<String, SensorRow>,
    /// Last aggregate *decided*, so the CRITICAL alarm fires once per episode.
    /// Separate from `status_unpublished` on purpose: this answers "has the
    /// alarm already been raised", which a failed write must not undo.
    last_status: Option<Option<LeakSeverity>>,
    /// Whether that aggregate reached STATE_DB.  A failed write sets this so
    /// the next pass rewrites the same value without re-raising the alarm.
    status_unpublished: bool,
}

/// What one cycle decided.  Returned rather than written so the caller owns the
/// DB, and so the state machine is testable without one.
#[derive(Debug, Default, PartialEq)]
pub struct LeakOutcome {
    /// Rows to write to `LIQUID_COOLING_INFO`, keyed by sensor name.
    pub rows: Vec<(String, SensorRow)>,
    /// The aggregate, when it changed this cycle.
    pub system_status: Option<Option<LeakSeverity>>,
}

impl LeakState {
    pub fn new() -> Self {
        Self::default()
    }

    /// One pass over every sensor.
    ///
    /// `now` is taken once per cycle so that the escalation timer does not
    /// drift within a cycle, as Python's single `datetime.now()` does not.
    pub fn refresh(
        &mut self,
        sensors: &[LeakSensorInfo],
        profiles: &[LeakProfile],
        now: Instant,
        events: &mut dyn EventLogger,
    ) -> LeakOutcome {
        let mut out = LeakOutcome::default();
        let mut status: Option<LeakSeverity> = None;
        // How many sensors are leaking this pass; two or more is critical
        // regardless of what each one reports on its own.
        let mut leaking_count = 0usize;

        for sensor in sensors {
            let name = sensor.name.as_str();

            // Sensor health is tracked separately from what it reports.
            if !sensor.is_ok {
                if self.faulty.insert(name.to_string()) {
                    events.error(&format!("Liquid cooling leakage sensor {name} reported faulty"));
                }
            } else if self.faulty.remove(name) {
                events.notice(&format!("Liquid cooling leaking sensor {name} recovered from fault"));
            }

            let mut severity = sensor.severity;

            if sensor.is_ok {
                if sensor.is_leak {
                    let started = *self.leaking_since.entry(name.to_string()).or_insert_with(|| {
                        events.error(&format!("Liquid cooling leakage sensor {name} reported leaking"));
                        now
                    });

                    // A MINOR leak becomes CRITICAL once it outlasts its
                    // profile's max_minor_duration_sec.
                    let mut escalated_after = None;
                    if severity == Some(LeakSeverity::Minor) {
                        if let Some(limit) = sensor
                            .profile_type
                            .as_deref()
                            .and_then(|ty| profiles.iter().find(|p| p.profile_type == ty))
                            .and_then(|p| p.max_minor_duration_sec)
                        {
                            if now.duration_since(started).as_secs_f64() >= limit {
                                severity = Some(LeakSeverity::Critical);
                                escalated_after = Some(limit);
                            }
                        }
                    }

                    // A platform that downgrades a sensor out of CRITICAL is
                    // forgotten, so a later CRITICAL is logged again.
                    if severity != Some(LeakSeverity::Critical) && self.critical.remove(name) {
                        events.notice(&format!(
                            "Liquid cooling leakage sensor {name} downgraded from CRITICAL to {}",
                            severity.map_or("None", |s| s.as_str())
                        ));
                    }

                    if severity == Some(LeakSeverity::Critical) && self.critical.insert(name.to_string()) {
                        events.error(&match escalated_after {
                            Some(secs) => format!(
                                "Leak on sensor {name} escalated from MINOR to CRITICAL after {}s",
                                fmt::float(secs)
                            ),
                            None => format!("CRITICAL leak reported by sensor {name}"),
                        });
                    }

                    // More than one leaking sensor is critical whatever each
                    // one says on its own.  Count the sensors rather than test
                    // whether the aggregate is already `Some`: a provider that
                    // reports no severity at all would otherwise keep the
                    // aggregate at `None` however many sensors were leaking.
                    leaking_count += 1;
                    status = if leaking_count > 1 {
                        Some(LeakSeverity::Critical)
                    } else {
                        severity
                    };
                } else if self.leaking_since.remove(name).is_some() {
                    if self.critical.remove(name) {
                        events.notice(&format!(
                            "Liquid cooling leakage sensor {name} recovered from CRITICAL leak"
                        ));
                    } else {
                        events.notice(&format!("Liquid cooling leakage sensor {name} recovered from leaking"));
                    }
                }
            }

            let leaking = if !sensor.is_ok {
                "N/A"
            } else if sensor.is_leak {
                "Yes"
            } else {
                "No"
            };
            let row: SensorRow = vec![
                ("name", sensor.name.clone()),
                ("leaking", leaking.to_string()),
                // leak_status duplicates leaking for the system-health checker
                // and the legacy leakageshow CLI, which still read it.
                ("leak_status", leaking.to_string()),
                (
                    "leak_sensor_status",
                    if sensor.is_ok { "Good" } else { "Fault" }.to_string(),
                ),
                ("type", sensor.sensor_type.clone()),
                ("location", sensor.location.clone()),
                (
                    "leak_severity",
                    severity.map_or("None".to_string(), |s| s.as_str().to_string()),
                ),
            ];
            if self.last_row.get(name) != Some(&row) {
                self.last_row.insert(name.to_string(), row.clone());
                out.rows.push((sensor.name.clone(), row));
            }
        }

        let was_critical = matches!(self.last_status, Some(Some(LeakSeverity::Critical)));
        let is_critical = status == Some(LeakSeverity::Critical);
        if is_critical && !was_critical {
            let mut names: Vec<&str> = self.leaking_since.keys().map(|s| s.as_str()).collect();
            names.sort_unstable();
            let list = if names.is_empty() {
                "unknown".to_string()
            } else {
                names.join(", ")
            };
            events.error(&format!("CRITICAL system leak detected (sensors: {list})"));
        } else if was_critical && !is_critical {
            events.notice(&format!(
                "CRITICAL system leak cleared (current status: {})",
                status.map_or("None", |s| s.as_str())
            ));
        }

        if self.last_status != Some(status) || self.status_unpublished {
            self.last_status = Some(status);
            self.status_unpublished = false;
            out.system_status = Some(status);
        }
        out
    }
}

// ── The thread ────────────────────────────────────────────────────────────────

/// Publish the profiles once, and seed `SYSTEM_LEAK_STATUS|system` only when it
/// is absent so that restarting mid-leak does not clobber a standing status.
pub fn publish_profiles_and_seed(tables: &crate::db::LeakTables, profiles: &[LeakProfile]) {
    for p in profiles {
        let fvs = [
            ("type", p.profile_type.clone()),
            (
                "max_minor_duration_sec",
                p.max_minor_duration_sec.map_or("inf".to_string(), fmt::float),
            ),
        ];
        if let Err(e) = tables.profile.set(&p.profile_type, &fvs) {
            log::warn!("failed to publish leak profile {}: {e}", p.profile_type);
        }
    }

    let already_seeded = tables.system.get("system").is_some_and(|fvs| !fvs.is_empty());
    if !already_seeded {
        let fvs = [
            ("device_leak_status", "None".to_string()),
            ("timestamp", fmt::timestamp()),
        ];
        if let Err(e) = tables.system.set("system", &fvs) {
            log::warn!("failed to seed SYSTEM_LEAK_STATUS: {e}");
        }
    }
}

/// Write one cycle's outcome.
impl LeakState {
    /// Drop the cached copies of rows that never reached the database, so the
    /// next pass sees them as changed and republishes them.
    pub fn forget(&mut self, unwritten: &Unwritten) {
        for name in &unwritten.sensors {
            self.last_row.remove(name);
        }
        if unwritten.system {
            // Not `last_status = None`: that is the transition record, and
            // clearing it would make the next pass see the CRITICAL alarm as
            // newly raised and log it a second time for one episode.
            self.status_unpublished = true;
        }
    }
}

/// What `apply` could not write, so the caller can forget it was published.
#[derive(Debug, Default, PartialEq)]
pub struct Unwritten {
    pub sensors: Vec<String>,
    pub system: bool,
}

/// Write the outcome, and report what did not land.
///
/// Python caches a row only *after* the write returns (`thermalctld:751-752`),
/// and its `Table.set` raises rather than returning, so a failed write leaves
/// its cache untouched and the next cycle republishes.  This side caches before
/// writing, so a dropped write has to be undone explicitly — otherwise the next
/// cycle compares against a value that never reached the database and publishes
/// nothing, and STATE_DB holds the stale leak state until some *other* field
/// changes.
#[must_use]
pub fn apply(tables: &crate::db::LeakTables, outcome: &LeakOutcome) -> Unwritten {
    let mut unwritten = Unwritten::default();
    for (name, row) in &outcome.rows {
        let fvs: Vec<(&str, String)> = row.iter().map(|(k, v)| (*k, v.clone())).collect();
        if let Err(e) = tables.sensor.set(name, &fvs) {
            log::warn!("failed to update LIQUID_COOLING_INFO for {name}: {e}");
            unwritten.sensors.push(name.clone());
        }
    }
    if let Some(status) = outcome.system_status {
        let fvs = [
            ("device_leak_status", status.map_or("None", |s| s.as_str()).to_string()),
            ("timestamp", fmt::timestamp()),
        ];
        if let Err(e) = tables.system.set("system", &fvs) {
            log::warn!("failed to update SYSTEM_LEAK_STATUS: {e}");
            unwritten.system = true;
        }
    }
    unwritten
}

// ── Tests ───────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[derive(Default)]
    struct Recorder {
        lines: Vec<String>,
    }
    impl EventLogger for Recorder {
        fn error(&mut self, msg: &str) {
            self.lines.push(format!("E:{msg}"));
        }
        fn notice(&mut self, msg: &str) {
            self.lines.push(format!("N:{msg}"));
        }
    }

    fn sensor(name: &str, leak: bool) -> LeakSensorInfo {
        LeakSensorInfo {
            name: name.to_string(),
            is_ok: true,
            is_leak: leak,
            severity: Some(LeakSeverity::Critical),
            profile_type: None,
            sensor_type: "unknown".to_string(),
            location: "unknown".to_string(),
        }
    }

    fn minor(name: &str, leak: bool) -> LeakSensorInfo {
        LeakSensorInfo {
            severity: Some(LeakSeverity::Minor),
            profile_type: Some("p".to_string()),
            ..sensor(name, leak)
        }
    }

    /// A provider that reports no severity at all.  Unreachable on Mellanox,
    /// which always says Critical, but `LeakSensorInfo` permits it and the
    /// two-sensors-are-critical rule must not depend on the first one having
    /// answered.
    fn unrated(name: &str, leak: bool) -> LeakSensorInfo {
        LeakSensorInfo {
            severity: None,
            ..sensor(name, leak)
        }
    }

    fn profile(secs: f64) -> Vec<LeakProfile> {
        vec![LeakProfile {
            profile_type: "p".to_string(),
            max_minor_duration_sec: Some(secs),
        }]
    }

    fn field<'a>(row: &'a SensorRow, key: &str) -> &'a str {
        row.iter().find(|(k, _)| *k == key).map(|(_, v)| v.as_str()).unwrap()
    }

    #[test]
    fn a_dry_system_reports_no_status_once() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();
        let out = st.refresh(&[sensor("leakage1", false)], &[], t, &mut ev);
        assert_eq!(out.system_status, Some(None), "first cycle establishes the status");
        let out = st.refresh(&[sensor("leakage1", false)], &[], t, &mut ev);
        assert_eq!(out.system_status, None, "unchanged, so not rewritten");
        assert!(out.rows.is_empty(), "unchanged row, so not rewritten");
    }

    #[test]
    fn a_leak_is_reported_once_and_sets_the_aggregate() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();
        let out = st.refresh(&[sensor("leakage1", true)], &[], t, &mut ev);
        assert_eq!(out.system_status, Some(Some(LeakSeverity::Critical)));
        assert_eq!(field(&out.rows[0].1, "leaking"), "Yes");
        assert_eq!(ev.lines.iter().filter(|l| l.contains("reported leaking")).count(), 1);

        st.refresh(&[sensor("leakage1", true)], &[], t, &mut ev);
        assert_eq!(
            ev.lines.iter().filter(|l| l.contains("reported leaking")).count(),
            1,
            "still one: a standing leak is not re-logged"
        );
    }

    #[test]
    fn two_leaking_sensors_are_critical_whatever_each_says() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();
        let out = st.refresh(&[minor("a", true), minor("b", true)], &[], t, &mut ev);
        assert_eq!(out.system_status, Some(Some(LeakSeverity::Critical)));
    }

    #[test]
    fn a_minor_leak_escalates_after_its_profile_duration() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();
        let out = st.refresh(&[minor("a", true)], &profile(10.0), t, &mut ev);
        assert_eq!(out.system_status, Some(Some(LeakSeverity::Minor)));

        let out = st.refresh(&[minor("a", true)], &profile(10.0), t + Duration::from_secs(9), &mut ev);
        assert_eq!(out.system_status, None, "not yet");

        let out = st.refresh(
            &[minor("a", true)],
            &profile(10.0),
            t + Duration::from_secs(10),
            &mut ev,
        );
        assert_eq!(out.system_status, Some(Some(LeakSeverity::Critical)));
        assert!(ev
            .lines
            .iter()
            .any(|l| l.contains("escalated from MINOR to CRITICAL after 10")));
    }

    #[test]
    fn a_minor_leak_without_a_profile_never_escalates() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();
        st.refresh(&[minor("a", true)], &[], t, &mut ev);
        let out = st.refresh(&[minor("a", true)], &[], t + Duration::from_secs(3600), &mut ev);
        assert_eq!(out.system_status, None, "still MINOR");
    }

    #[test]
    fn recovery_clears_the_sensor_and_the_aggregate() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();
        st.refresh(&[sensor("a", true)], &[], t, &mut ev);
        let out = st.refresh(&[sensor("a", false)], &[], t, &mut ev);
        assert_eq!(out.system_status, Some(None));
        assert_eq!(field(&out.rows[0].1, "leaking"), "No");
        assert!(ev.lines.iter().any(|l| l.contains("recovered from CRITICAL leak")));
        assert!(ev.lines.iter().any(|l| l.contains("CRITICAL system leak cleared")));
    }

    #[test]
    fn a_second_episode_is_logged_again() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();
        st.refresh(&[sensor("a", true)], &[], t, &mut ev);
        st.refresh(&[sensor("a", false)], &[], t, &mut ev);
        st.refresh(&[sensor("a", true)], &[], t, &mut ev);
        assert_eq!(
            ev.lines.iter().filter(|l| l.contains("CRITICAL leak reported")).count(),
            2
        );
    }

    #[test]
    fn a_faulty_sensor_is_reported_once_and_does_not_leak() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();
        let mut s = sensor("a", true);
        s.is_ok = false;
        let out = st.refresh(&[s.clone()], &[], t, &mut ev);
        assert_eq!(
            out.system_status,
            Some(None),
            "a faulty sensor does not set the aggregate"
        );
        assert_eq!(field(&out.rows[0].1, "leaking"), "N/A");
        assert_eq!(field(&out.rows[0].1, "leak_sensor_status"), "Fault");
        st.refresh(&[s], &[], t, &mut ev);
        assert_eq!(ev.lines.iter().filter(|l| l.contains("reported faulty")).count(), 1);
    }

    #[test]
    fn a_faulty_sensor_does_not_mask_a_leaking_one() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();
        let mut bad = sensor("a", false);
        bad.is_ok = false;
        let out = st.refresh(&[bad, sensor("b", true)], &[], t, &mut ev);
        assert_eq!(out.system_status, Some(Some(LeakSeverity::Critical)));
    }

    #[test]
    fn a_row_is_rewritten_only_when_a_field_changes() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();
        assert_eq!(st.refresh(&[sensor("a", false)], &[], t, &mut ev).rows.len(), 1);
        assert_eq!(st.refresh(&[sensor("a", false)], &[], t, &mut ev).rows.len(), 0);
        assert_eq!(st.refresh(&[sensor("a", true)], &[], t, &mut ev).rows.len(), 1);
    }

    // ── Landing the outcome in a database ─────────────────────────────────────

    use crate::db::mock::MockLeak;

    #[test]
    fn a_standing_status_is_not_clobbered_on_restart() {
        let m = MockLeak::new();
        let tables = &m.tables;
        // A previous run left a CRITICAL status behind.
        tables
            .system
            .set("system", &[("device_leak_status", "CRITICAL".to_string())])
            .unwrap();

        publish_profiles_and_seed(tables, &[]);
        assert_eq!(
            m.system.field("system", "device_leak_status").as_deref(),
            Some("CRITICAL"),
            "seeding must not overwrite a standing status"
        );
    }

    #[test]
    fn an_empty_table_is_seeded_with_none() {
        let m = MockLeak::new();
        publish_profiles_and_seed(&m.tables, &[]);
        assert_eq!(m.system.field("system", "device_leak_status").as_deref(), Some("None"));
    }

    #[test]
    fn profiles_are_published_once_per_type() {
        let m = MockLeak::new();
        let profiles = vec![
            LeakProfile {
                profile_type: "fast".into(),
                max_minor_duration_sec: Some(30.0),
            },
            LeakProfile {
                profile_type: "slow".into(),
                max_minor_duration_sec: None,
            },
        ];
        publish_profiles_and_seed(&m.tables, &profiles);
        assert_eq!(m.profile.keys(), ["fast", "slow"]);
        assert_eq!(
            m.profile.field("fast", "max_minor_duration_sec").as_deref(),
            Some("30.0")
        );
        assert_eq!(
            m.profile.field("slow", "max_minor_duration_sec").as_deref(),
            Some("inf"),
            "no limit is inf, as Python's float(inf) prints"
        );
    }

    #[test]
    fn an_outcome_lands_in_both_tables() {
        let m = MockLeak::new();
        let tables = &m.tables;
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let out = st.refresh(&[sensor("leakage1", true)], &[], Instant::now(), &mut ev);
        let _ = apply(tables, &out);
        assert_eq!(m.sensor.field("leakage1", "leaking").as_deref(), Some("Yes"));
        assert_eq!(
            m.system.field("system", "device_leak_status").as_deref(),
            Some("CRITICAL")
        );
    }

    /// An unchanged cycle writes nothing at all, which is what keeps a
    /// subscriber from seeing a write per 0.5 s.
    #[test]
    fn an_unchanged_cycle_writes_nothing() {
        let m = MockLeak::new();
        let tables = &m.tables;
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();
        let _ = apply(tables, &st.refresh(&[sensor("leakage1", false)], &[], t, &mut ev));
        let before = m.sensor.row("leakage1");

        m.sensor.fail_writes("must not be written again");
        m.system.fail_writes("must not be written again");
        let _ = apply(tables, &st.refresh(&[sensor("leakage1", false)], &[], t, &mut ev));
        assert_eq!(m.sensor.row("leakage1"), before);
    }

    #[test]
    fn two_leaking_sensors_are_critical_even_without_a_severity() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();
        let out = st.refresh(&[unrated("a", true), unrated("b", true)], &[], t, &mut ev);
        assert_eq!(out.system_status, Some(Some(LeakSeverity::Critical)));
    }

    #[test]
    fn one_unrated_leaking_sensor_keeps_its_own_severity() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();
        let out = st.refresh(&[unrated("a", true)], &[], t, &mut ev);
        assert_eq!(out.system_status, Some(None));
    }

    // ── Recovery notices, and the write failures ──────────────────────────

    fn faulty(name: &str) -> LeakSensorInfo {
        let mut s = sensor(name, false);
        s.is_ok = false;
        s
    }

    fn severe(name: &str, sev: LeakSeverity) -> LeakSensorInfo {
        let mut s = sensor(name, true);
        s.severity = Some(sev);
        s
    }

    /// Every alarm this state machine raises has a matching recovery notice.
    /// One without the other leaves the BMC's event log holding an alarm that
    /// never ends, which is what an operator reads after the fact.
    #[test]
    fn a_faulty_sensor_recovering_is_its_own_notice() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();

        st.refresh(&[faulty("leakage1")], &[], t, &mut ev);
        assert!(ev.lines.iter().any(|l| l.contains("reported faulty")), "{:?}", ev.lines);

        ev.lines.clear();
        st.refresh(&[sensor("leakage1", false)], &[], t, &mut ev);
        assert!(
            ev.lines.iter().any(|l| l.contains("recovered from fault")),
            "{:?}",
            ev.lines
        );
    }

    /// A leak that stops is a recovery, and the notice says which kind it was:
    /// recovering from a CRITICAL leak is a different event from recovering
    /// from a leak that never escalated.
    #[test]
    fn recovering_from_a_leak_says_which_kind_it_was() {
        for (sev, want) in [
            (LeakSeverity::Critical, "recovered from CRITICAL leak"),
            (LeakSeverity::Minor, "recovered from leaking"),
        ] {
            let mut st = LeakState::new();
            let mut ev = Recorder::default();
            let t = Instant::now();

            st.refresh(&[severe("leakage1", sev)], &[], t, &mut ev);
            ev.lines.clear();
            st.refresh(&[sensor("leakage1", false)], &[], t, &mut ev);
            assert!(ev.lines.iter().any(|l| l.contains(want)), "{sev:?}: {:?}", ev.lines);
        }
    }

    /// A platform that downgrades a sensor out of CRITICAL says so, and forgets
    /// it — so a later CRITICAL is a new event rather than a silent repeat.
    #[test]
    fn a_downgrade_out_of_critical_is_announced_and_rearms() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();

        st.refresh(&[severe("leakage1", LeakSeverity::Critical)], &[], t, &mut ev);
        ev.lines.clear();

        st.refresh(&[severe("leakage1", LeakSeverity::Minor)], &[], t, &mut ev);
        assert!(
            ev.lines.iter().any(|l| l.contains("downgraded from CRITICAL to MINOR")),
            "{:?}",
            ev.lines
        );

        ev.lines.clear();
        st.refresh(&[severe("leakage1", LeakSeverity::Critical)], &[], t, &mut ev);
        assert!(
            ev.lines.iter().any(|l| l.contains("CRITICAL")),
            "the second CRITICAL is announced again: {:?}",
            ev.lines
        );
    }

    /// Leak detection is a safety function, so a database that refuses writes
    /// is logged rather than swallowed — but it must not stop the loop either,
    /// or one bad write ends leak monitoring for the life of the daemon.
    /// A failed aggregate write must not re-raise the alarm.
    ///
    /// The row cache and the transition record used to be the same field, so
    /// undoing the first also undid the second: after a dropped
    /// SYSTEM_LEAK_STATUS write the next pass saw CRITICAL as newly reached and
    /// logged it a second time for one episode.  An operator counting alarms
    /// would have counted two leaks.
    #[test]
    fn a_dropped_aggregate_write_does_not_raise_the_alarm_twice() {
        let m = crate::db::mock::MockLeak::new();
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let profiles = [LeakProfile {
            profile_type: "chassis".into(),
            max_minor_duration_sec: Some(0.0),
        }];
        let mut sensor_crit = sensor("leakage1", true);
        sensor_crit.profile_type = Some("chassis".to_string());

        // First pass reaches CRITICAL, but the aggregate write is refused.
        m.system.fail_writes("redis is down");
        let out = st.refresh(&[sensor_crit.clone()], &profiles, Instant::now(), &mut ev);
        let unwritten = apply(&m.tables, &out);
        assert!(unwritten.system);
        st.forget(&unwritten);
        let alarms = ev
            .lines
            .iter()
            .filter(|l| l.contains("CRITICAL system leak detected"))
            .count();
        assert_eq!(alarms, 1, "the episode was announced once");

        // Same state, database healthy: the value has to be rewritten, and the
        // alarm must not fire again.
        m.system.allow_writes();
        let out = st.refresh(&[sensor_crit], &profiles, Instant::now(), &mut ev);
        assert!(out.system_status.is_some(), "the dropped value is rewritten");
        let _ = apply(&m.tables, &out);
        let alarms = ev
            .lines
            .iter()
            .filter(|l| l.contains("CRITICAL system leak detected"))
            .count();
        assert_eq!(alarms, 1, "and only once");
    }

    /// A write that fails must not be remembered as published.  The state
    /// machine caches the row before `apply` writes it, so without
    /// [`LeakState::forget`] the next pass sees no change and STATE_DB keeps
    /// the pre-failure leak state until some other field moves -- which for a
    /// leak that starts inside the failure window means the standing status
    /// stays `None` while the daemon believes it published `CRITICAL`.
    #[test]
    fn a_dropped_write_is_republished_on_the_next_pass() {
        let m = crate::db::mock::MockLeak::new();
        let mut st = LeakState::new();
        let mut ev = Recorder::default();

        m.sensor.fail_writes("redis is down");
        m.system.fail_writes("redis is down");
        let out = st.refresh(&[sensor("leakage1", true)], &[], Instant::now(), &mut ev);
        let dropped_status = out.system_status.expect("the first pass decided a status");
        let unwritten = apply(&m.tables, &out);
        assert_eq!(unwritten.sensors, vec!["leakage1".to_string()]);
        assert!(unwritten.system);
        st.forget(&unwritten);
        assert!(m.sensor.is_empty(), "nothing reached the database");

        // Same sensor, same state, database healthy again: the row has to be
        // written now, which only happens if the cache was undone.
        m.sensor.allow_writes();
        m.system.allow_writes();
        let out = st.refresh(&[sensor("leakage1", true)], &[], Instant::now(), &mut ev);
        let unwritten = apply(&m.tables, &out);
        assert_eq!(unwritten, Unwritten::default());
        assert_eq!(
            m.sensor.field("leakage1", "leaking").as_deref(),
            Some("Yes"),
            "the row the failed pass dropped was republished"
        );
        // The same status the dropped pass decided, republished verbatim.
        assert_eq!(
            m.system.field("system", "device_leak_status").as_deref(),
            Some(dropped_status.map_or("None", |s| s.as_str()))
        );
    }

    #[test]
    fn a_refusing_database_does_not_stop_leak_monitoring() {
        let m = crate::db::mock::MockLeak::new();
        m.sensor.fail_writes("redis is down");
        m.system.fail_writes("redis is down");
        m.profile.fail_writes("redis is down");

        publish_profiles_and_seed(
            &m.tables,
            &[LeakProfile {
                profile_type: "chassis".into(),
                max_minor_duration_sec: None,
            }],
        );
        assert!(m.profile.is_empty());

        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let _ = apply(
            &m.tables,
            &st.refresh(&[sensor("leakage1", true)], &[], Instant::now(), &mut ev),
        );
        assert!(m.sensor.is_empty());
        // The event still reached the BMC log, which is the half that does not
        // depend on redis.
        assert!(!ev.lines.is_empty());
    }
}
