//! Leak detection.  Ports `thermalctld:LiquidCoolingUpdater`.
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

use platform_api::{LeakProfile, LeakSensorInfo, LeakSeverity};

use crate::event_log::EventLogger;
use crate::fmt;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SystemSeverity {
    Minor,
    Major,
    Critical,
}
impl SystemSeverity {
    fn as_str(self) -> &'static str {
        match self {
            Self::Minor => "MINOR",
            Self::Major => "MAJOR",
            Self::Critical => "CRITICAL",
        }
    }
    fn sensor(value: LeakSeverity) -> Self {
        match value {
            LeakSeverity::Minor => Self::Minor,
            LeakSeverity::Critical => Self::Critical,
        }
    }
}

/// Fields of one `LIQUID_COOLING_INFO` row, in Python's order.
type SensorRow = Vec<(&'static str, String)>;

/// Everything the updater remembers between cycles.
#[derive(Default)]
pub struct LeakState {
    /// When each sensor started leaking, for the MINOR escalation timer.
    leaking_since: HashMap<String, Instant>,
    start_timestamps: HashMap<String, String>,
    restored_critical: HashSet<String>,
    escalated: HashSet<String>,
    major_minimum: usize,
    /// Sensors already reported faulty, so the error is logged once.
    faulty: HashSet<String>,
    /// Sensors already reported CRITICAL, so it is logged once per episode.
    critical: HashSet<String>,
    /// Last row written per sensor, so unchanged rows are not rewritten.
    last_row: HashMap<String, SensorRow>,
    /// Last aggregate *decided*, so the CRITICAL alarm fires once per episode.
    /// Separate from `status_unpublished` on purpose: this answers "has the
    /// alarm already been raised", which a failed write must not undo.
    last_status: Option<Option<SystemSeverity>>,
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
    pub system_status: Option<Option<SystemSeverity>>,
}

impl LeakState {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn set_major_minimum(&mut self, minimum: usize) {
        self.major_minimum = minimum;
    }

    pub fn restore(&mut self, tables: &crate::db::LeakTables) -> Result<(), String> {
        self.restore_at(tables, chrono::Local::now().naive_local(), Instant::now())
    }

    fn restore_at(
        &mut self,
        tables: &crate::db::LeakTables,
        local_now: chrono::NaiveDateTime,
        now: Instant,
    ) -> Result<(), String> {
        for name in tables.sensor.get_keys()? {
            let row = tables.sensor.get(&name)?.unwrap_or_default();
            let field = |key: &str| row.iter().find(|(k, _)| k == key).map(|(_, v)| v.as_str());
            if field("leaking") != Some("Yes") {
                continue;
            }
            let timestamp = field("timestamp").unwrap_or("");
            let elapsed = chrono::NaiveDateTime::parse_from_str(timestamp, "%Y%m%d %H:%M:%S")
                .ok()
                // fmt::timestamp and Python's writer both store local time.
                .map(|t| (local_now - t).num_seconds().max(0) as u64)
                .unwrap_or(0);
            self.leaking_since.insert(
                name.clone(),
                now.checked_sub(std::time::Duration::from_secs(elapsed))
                    .unwrap_or(now),
            );
            self.start_timestamps.insert(
                name.clone(),
                if timestamp.is_empty() {
                    fmt::timestamp()
                } else {
                    timestamp.to_string()
                },
            );
            if field("leak_severity") == Some("CRITICAL") {
                self.restored_critical.insert(name.clone());
                self.critical.insert(name);
            }
        }
        if let Some(row) = tables.system.get("system")? {
            let status = row
                .iter()
                .find(|(k, _)| k == "device_leak_status")
                .map(|(_, v)| v.as_str());
            self.last_status = Some(match status {
                Some("CRITICAL") => Some(SystemSeverity::Critical),
                Some("MAJOR") => Some(SystemSeverity::Major),
                Some("MINOR") => Some(SystemSeverity::Minor),
                _ => None,
            });
        }
        Ok(())
    }

    /// One pass over every sensor.
    ///
    /// `now` is taken once per cycle so that the escalation timer does not
    /// drift within a cycle, as Python's single `datetime.now()` does not.
    pub fn refresh(
        &mut self,
        sensors: &[LeakSensorInfo],
        now: Instant,
        events: &mut dyn EventLogger,
    ) -> LeakOutcome {
        let mut out = LeakOutcome::default();
        let mut status: Option<SystemSeverity> = None;
        // How many sensors are leaking this pass; two or more is critical
        // regardless of what each one reports on its own.
        let mut leaking_count = 0usize;
        let mut major_escalated = false;

        for sensor in sensors {
            let name = sensor.name.as_str();

            // Sensor health is tracked separately from what it reports.
            //
            // A sensor that cannot say whether it is healthy counts as faulty.
            // This is a leak detector: "I do not know" must not read as "fine",
            // and publishing Fault makes the gap visible instead of silent.
            let is_ok = sensor.is_ok.unwrap_or(false);
            let is_leak = sensor.is_leak.unwrap_or(false);
            if !is_ok {
                if self.faulty.insert(name.to_string()) {
                    events.error(&format!(
                        "Liquid cooling leakage sensor {name} reported faulty"
                    ));
                }
            } else if self.faulty.remove(name) {
                events.notice(&format!(
                    "Liquid cooling leaking sensor {name} recovered from fault"
                ));
            }

            let mut severity = sensor.severity;

            if is_ok {
                if is_leak {
                    self.start_timestamps
                        .entry(name.to_string())
                        .or_insert_with(fmt::timestamp);
                    if self.restored_critical.contains(name) {
                        severity = Some(LeakSeverity::Critical);
                    }
                    let started =
                        *self
                            .leaking_since
                            .entry(name.to_string())
                            .or_insert_with(|| {
                                events.error(&format!(
                                    "Liquid cooling leakage sensor {name} reported leaking"
                                ));
                                now
                            });

                    // A MINOR leak becomes CRITICAL once it outlasts its
                    // profile's max_minor_duration_sec.
                    let mut escalated_after = None;
                    if severity == Some(LeakSeverity::Minor) {
                        if let Some(limit) = sensor
                            .profile_max_minor_duration_sec
                            .map(|secs| secs as f64)
                        {
                            if limit > 0.0
                                && limit.is_finite()
                                && now.duration_since(started).as_secs_f64() >= limit
                            {
                                major_escalated = true;
                                if self.escalated.insert(name.to_string()) {
                                    escalated_after = Some(limit);
                                }
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

                    if let Some(secs) = escalated_after {
                        events.warning(&format!(
                            "Leak on sensor {name} escalated from MINOR to MAJOR after {}s",
                            secs as i64
                        ));
                    }
                    if severity == Some(LeakSeverity::Critical)
                        && self.critical.insert(name.to_string())
                    {
                        events.error(&match escalated_after {
                            Some(secs) => format!(
                                "Leak on sensor {name} escalated from MINOR to CRITICAL after {}s",
                                fmt::float(secs)
                            ),
                            None => format!("CRITICAL leak reported by sensor {name}"),
                        });
                    }

                    if severity == Some(LeakSeverity::Minor) {
                        leaking_count += 1;
                    }
                    if severity == Some(LeakSeverity::Critical)
                        || (severity == Some(LeakSeverity::Minor)
                            && status != Some(SystemSeverity::Critical))
                    {
                        status = severity.map(SystemSeverity::sensor);
                    }
                } else if self.leaking_since.remove(name).is_some() {
                    self.start_timestamps.remove(name);
                    self.restored_critical.remove(name);
                    self.escalated.remove(name);
                    if self.critical.remove(name) {
                        events.notice(&format!(
                            "Liquid cooling leakage sensor {name} recovered from CRITICAL leak"
                        ));
                    } else {
                        events.notice(&format!(
                            "Liquid cooling leakage sensor {name} recovered from leaking"
                        ));
                    }
                }
            }

            let leaking = if !is_ok {
                "N/A"
            } else if is_leak {
                "Yes"
            } else {
                "No"
            };
            let mut row: SensorRow = vec![
                ("name", sensor.name.clone()),
                ("leaking", leaking.to_string()),
                // leak_status duplicates leaking for the system-health checker
                // and the legacy leakageshow CLI, which still read it.
                ("leak_status", leaking.to_string()),
                (
                    "leak_sensor_status",
                    if is_ok { "Good" } else { "Fault" }.to_string(),
                ),
                ("type", fmt::opt_str(&sensor.sensor_type)),
                ("location", fmt::opt_str(&sensor.location)),
                (
                    "leak_severity",
                    severity.map_or("None".to_string(), |s| s.as_str().to_string()),
                ),
            ];
            if let Some(timestamp) = self.start_timestamps.get(name) {
                row.push(("timestamp", timestamp.clone()));
            }
            if self.last_row.get(name) != Some(&row) {
                self.last_row.insert(name.to_string(), row.clone());
                out.rows.push((sensor.name.clone(), row));
            }
        }

        if status != Some(SystemSeverity::Critical)
            && (major_escalated || (self.major_minimum >= 2 && leaking_count >= self.major_minimum))
        {
            status = Some(SystemSeverity::Major);
        }
        let was_major = matches!(self.last_status, Some(Some(SystemSeverity::Major)));
        if status == Some(SystemSeverity::Major) && !was_major {
            events.warning("MAJOR system leak detected");
        }
        if was_major && status != Some(SystemSeverity::Major) {
            events.notice("MAJOR system leak cleared");
        }
        let was_critical = matches!(self.last_status, Some(Some(SystemSeverity::Critical)));
        let is_critical = status == Some(SystemSeverity::Critical);
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
pub fn publish_profiles_and_seed(
    tables: &crate::db::LeakTables,
    profiles: &[LeakProfile],
) -> Result<(), String> {
    for p in profiles {
        let fvs = [
            ("type", p.r#type.clone()),
            (
                "max_minor_duration_sec",
                p.max_minor_duration_sec
                    .map_or("None".to_string(), |secs| secs.to_string()),
            ),
        ];
        if let Err(e) = tables.profile.set(&p.r#type, &fvs) {
            log::warn!("failed to publish leak profile {}: {e}", p.r#type);
        }
    }

    // Propagated, unlike the destructors' reads: Python reads this one bare
    // (`self.system_table.get('system')` in
    // `thermalctld:LiquidCoolingUpdater.__init__`), and for a reason -- an
    // unreadable table read as "not seeded yet" would overwrite a standing
    // leak alarm with `None`.
    let already_seeded = tables
        .system
        .get("system")
        .map_err(|e| format!("failed to read SYSTEM_LEAK_STATUS: {e}"))?
        .is_some_and(|fvs| !fvs.is_empty());
    if !already_seeded {
        let fvs = [
            ("device_leak_status", "None".to_string()),
            ("timestamp", fmt::timestamp()),
        ];
        if let Err(e) = tables.system.set("system", &fvs) {
            log::warn!("failed to seed SYSTEM_LEAK_STATUS: {e}");
        }
    }
    Ok(())
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

impl Unwritten {
    /// Whether every row landed.
    ///
    /// A non-empty one means STATE_DB refused a write, which is the signal the
    /// leak thread leaves on: `DbConnector` does not reconnect, so the rows it
    /// could not write this cycle it will not write next cycle either.
    pub fn is_empty(&self) -> bool {
        self.sensors.is_empty() && !self.system
    }
}

/// Write the outcome, and report what did not land.
///
/// Python caches a row only *after* the write returns
/// (`thermalctld:LiquidCoolingUpdater._refresh_leak_status`), and its
/// `Table.set` raises rather than returning, so a failed write leaves its cache
/// untouched and the next cycle republishes.  This side caches before writing,
/// so a dropped write has to be undone explicitly — otherwise the next cycle
/// compares against a value that never reached the database and publishes
/// nothing, and STATE_DB holds the stale leak state until some *other* field
/// changes.
#[must_use]
pub fn apply(tables: &crate::db::LeakTables, outcome: &LeakOutcome) -> Unwritten {
    let mut unwritten = Unwritten::default();
    for (name, row) in &outcome.rows {
        // The timestamp is appended here rather than built into the row, so
        // that it stays out of the change comparison: Python does the same
        // (`thermalctld:LiquidCoolingUpdater._refresh_leak_status`,
        // `fvs_tuple` vs `fvs_with_ts`).  Carrying it in the row would make
        // every cycle look changed and rewrite the table at the polling rate;
        // leaving it out altogether loses when the state last moved, which is
        // the one thing an operator reads this row for.
        let mut fvs: Vec<(&str, String)> = row.iter().map(|(k, v)| (*k, v.clone())).collect();
        if !fvs.iter().any(|(key, _)| *key == "timestamp") {
            fvs.push(("timestamp", fmt::timestamp()));
        }
        if let Err(e) = tables.sensor.set(name, &fvs) {
            log::warn!("failed to update LIQUID_COOLING_INFO for {name}: {e}");
            unwritten.sensors.push(name.clone());
        }
    }
    if let Some(status) = outcome.system_status {
        let fvs = [
            (
                "device_leak_status",
                status.map_or("None", |s| s.as_str()).to_string(),
            ),
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
    use pmon_common::db::TableLike;
    use std::time::Duration;

    #[test]
    fn restore_uses_local_clock_in_non_utc_processes() {
        if std::env::var_os("SONIC_LEAK_CLOCK_TEST_CHILD").is_none() {
            for (timezone, offset) in [("XXX-2", 7200), ("XXX+5", -18000)] {
                let output = std::process::Command::new(std::env::current_exe().unwrap())
                    .args([
                        "--exact",
                        "leak_updater::tests::restore_uses_local_clock_in_non_utc_processes",
                    ])
                    .env("SONIC_LEAK_CLOCK_TEST_CHILD", "1")
                    .env("TZ", timezone)
                    .env("SONIC_LEAK_CLOCK_OFFSET", offset.to_string())
                    .output()
                    .unwrap();
                assert!(
                    output.status.success(),
                    "{timezone}: {}{}",
                    String::from_utf8_lossy(&output.stdout),
                    String::from_utf8_lossy(&output.stderr)
                );
            }
            return;
        }
        assert_eq!(
            chrono::Local::now().offset().local_minus_utc(),
            std::env::var("SONIC_LEAK_CLOCK_OFFSET")
                .unwrap()
                .parse::<i32>()
                .unwrap()
        );
        for (age, expected) in [(60, SystemSeverity::Minor), (600, SystemSeverity::Major)] {
            let mock = MockLeak::new();
            let timestamp = (chrono::Local::now() - chrono::Duration::seconds(age))
                .format("%Y%m%d %H:%M:%S")
                .to_string();
            mock.sensor
                .set(
                    "a",
                    &[
                        ("leaking", "Yes".into()),
                        ("leak_severity", "MINOR".into()),
                        ("timestamp", timestamp),
                    ],
                )
                .unwrap();
            let mut state = LeakState::new();
            state.restore(&mock.tables).unwrap();
            let result = state.refresh(
                &[timed_minor("a", true, 300)],
                Instant::now(),
                &mut Recorder::default(),
            );
            assert_eq!(result.system_status, Some(Some(expected)));
        }
    }

    #[test]
    fn restored_leak_age_and_max_t_are_independent_of_utc_offset() {
        use chrono::TimeZone;
        for offset in [2 * 3600, -5 * 3600, 0] {
            for (age, expected) in [(60, SystemSeverity::Minor), (600, SystemSeverity::Major)] {
                let zone = chrono::FixedOffset::east_opt(offset).unwrap();
                let local_now = zone.with_ymd_and_hms(2026, 11, 9, 12, 0, 0).unwrap();
                let timestamp = (local_now - chrono::Duration::seconds(age))
                    .format("%Y%m%d %H:%M:%S")
                    .to_string();
                let mock = MockLeak::new();
                mock.sensor
                    .set(
                        "a",
                        &[
                            ("leaking", "Yes".to_string()),
                            ("leak_severity", "MINOR".to_string()),
                            ("timestamp", timestamp.clone()),
                        ],
                    )
                    .unwrap();
                let now = Instant::now();
                let mut state = LeakState::new();
                state
                    .restore_at(&mock.tables, local_now.naive_local(), now)
                    .unwrap();
                assert_eq!(
                    now.duration_since(state.leaking_since["a"]),
                    Duration::from_secs(age as u64)
                );
                let result = state.refresh(
                    &[timed_minor("a", true, 300)],
                    now,
                    &mut Recorder::default(),
                );
                assert_eq!(
                    result.system_status,
                    Some(Some(expected)),
                    "offset={offset}, age={age}"
                );
                assert_eq!(state.start_timestamps["a"], timestamp);
            }
        }
    }

    #[test]
    fn zero_duration_does_not_escalate_a_minor_leak() {
        let mut state = LeakState::new();
        let mut events = Recorder::default();
        let now = Instant::now();
        let result = state.refresh(&[timed_minor("a", true, 0)], now, &mut events);
        assert_eq!(result.system_status, Some(Some(SystemSeverity::Minor)));
        assert_eq!(
            state
                .refresh(
                    &[timed_minor("a", true, 0)],
                    now + Duration::from_secs(600),
                    &mut events
                )
                .system_status,
            None
        );
    }

    #[test]
    fn restarting_during_a_critical_leak_preserves_its_severity_and_start_time() {
        let mock = MockLeak::new();
        let mut state = LeakState::new();
        let mut events = Recorder::default();
        let result = state.refresh(
            &[severe("a", LeakSeverity::Critical)],
            Instant::now(),
            &mut events,
        );
        let _ = apply(&mock.tables, &result);
        let timestamp = mock.sensor.field("a", "timestamp");
        let mut restored = LeakState::new();
        restored.restore(&mock.tables).unwrap();
        let result = restored.refresh(&[minor("a", true)], Instant::now(), &mut events);
        let _ = apply(&mock.tables, &result);
        assert_eq!(
            mock.system.field("system", "device_leak_status").as_deref(),
            Some("CRITICAL")
        );
        assert_eq!(mock.sensor.field("a", "timestamp"), timestamp);
    }

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
            parent_name: "chassis 1".to_string(),
            is_ok: Some(true),
            is_leak: Some(leak),
            severity: Some(LeakSeverity::Critical),
            profile_type: None,
            profile_max_minor_duration_sec: None,
            sensor_type: Some("unknown".to_string()),
            location: Some("unknown".to_string()),
        }
    }

    fn minor(name: &str, leak: bool) -> LeakSensorInfo {
        LeakSensorInfo {
            severity: Some(LeakSeverity::Minor),
            profile_type: Some("p".to_string()),
            ..sensor(name, leak)
        }
    }

    fn timed_minor(name: &str, leak: bool, seconds: i64) -> LeakSensorInfo {
        LeakSensorInfo {
            profile_max_minor_duration_sec: Some(seconds),
            ..minor(name, leak)
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
            r#type: "p".to_string(),
            max_minor_duration_sec: Some(secs as i64),
        }]
    }

    fn field<'a>(row: &'a SensorRow, key: &str) -> &'a str {
        row.iter()
            .find(|(k, _)| *k == key)
            .map(|(_, v)| v.as_str())
            .unwrap()
    }

    #[test]
    fn a_dry_system_reports_no_status_once() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();
        let out = st.refresh(&[sensor("leakage1", false)], t, &mut ev);
        assert_eq!(
            out.system_status,
            Some(None),
            "first cycle establishes the status"
        );
        let out = st.refresh(&[sensor("leakage1", false)], t, &mut ev);
        assert_eq!(out.system_status, None, "unchanged, so not rewritten");
        assert!(out.rows.is_empty(), "unchanged row, so not rewritten");
    }

    #[test]
    fn a_leak_is_reported_once_and_sets_the_aggregate() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();
        let out = st.refresh(&[sensor("leakage1", true)], t, &mut ev);
        assert_eq!(out.system_status, Some(Some(SystemSeverity::Critical)));
        assert_eq!(field(&out.rows[0].1, "leaking"), "Yes");
        assert_eq!(
            ev.lines
                .iter()
                .filter(|l| l.contains("reported leaking"))
                .count(),
            1
        );

        st.refresh(&[sensor("leakage1", true)], t, &mut ev);
        assert_eq!(
            ev.lines
                .iter()
                .filter(|l| l.contains("reported leaking"))
                .count(),
            1,
            "still one: a standing leak is not re-logged"
        );
    }

    #[test]
    fn two_minor_leaks_are_major_when_the_minimum_is_two() {
        let mut st = LeakState::new();
        st.set_major_minimum(2);
        let mut ev = Recorder::default();
        let t = Instant::now();
        let out = st.refresh(&[minor("a", true), minor("b", true)], t, &mut ev);
        assert_eq!(out.system_status, Some(Some(SystemSeverity::Major)));
    }

    #[test]
    fn a_minor_leak_escalates_after_its_profile_duration() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();
        let out = st.refresh(&[timed_minor("a", true, 10)], t, &mut ev);
        assert_eq!(out.system_status, Some(Some(SystemSeverity::Minor)));

        let out = st.refresh(
            &[timed_minor("a", true, 10)],
            t + Duration::from_secs(9),
            &mut ev,
        );
        assert_eq!(out.system_status, None, "not yet");

        let out = st.refresh(
            &[timed_minor("a", true, 10)],
            t + Duration::from_secs(10),
            &mut ev,
        );
        assert_eq!(out.system_status, Some(Some(SystemSeverity::Major)));
        assert!(ev
            .lines
            .iter()
            .any(|l| l.contains("escalated from MINOR to MAJOR after 10")));
    }

    #[test]
    fn a_minor_leak_without_a_profile_never_escalates() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();
        st.refresh(&[minor("a", true)], t, &mut ev);
        let out = st.refresh(&[minor("a", true)], t + Duration::from_secs(3600), &mut ev);
        assert_eq!(out.system_status, None, "still MINOR");
    }

    #[test]
    fn recovery_clears_the_sensor_and_the_aggregate() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();
        st.refresh(&[sensor("a", true)], t, &mut ev);
        let out = st.refresh(&[sensor("a", false)], t, &mut ev);
        assert_eq!(out.system_status, Some(None));
        assert_eq!(field(&out.rows[0].1, "leaking"), "No");
        assert!(ev
            .lines
            .iter()
            .any(|l| l.contains("recovered from CRITICAL leak")));
        assert!(ev
            .lines
            .iter()
            .any(|l| l.contains("CRITICAL system leak cleared")));
    }

    #[test]
    fn a_second_episode_is_logged_again() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();
        st.refresh(&[sensor("a", true)], t, &mut ev);
        st.refresh(&[sensor("a", false)], t, &mut ev);
        st.refresh(&[sensor("a", true)], t, &mut ev);
        assert_eq!(
            ev.lines
                .iter()
                .filter(|l| l.contains("CRITICAL leak reported"))
                .count(),
            2
        );
    }

    #[test]
    fn a_faulty_sensor_is_reported_once_and_does_not_leak() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();
        let mut s = sensor("a", true);
        s.is_ok = Some(false);
        let out = st.refresh(&[s.clone()], t, &mut ev);
        assert_eq!(
            out.system_status,
            Some(None),
            "a faulty sensor does not set the aggregate"
        );
        assert_eq!(field(&out.rows[0].1, "leaking"), "N/A");
        assert_eq!(field(&out.rows[0].1, "leak_sensor_status"), "Fault");
        st.refresh(&[s], t, &mut ev);
        assert_eq!(
            ev.lines
                .iter()
                .filter(|l| l.contains("reported faulty"))
                .count(),
            1
        );
    }

    #[test]
    fn a_faulty_sensor_does_not_mask_a_leaking_one() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();
        let mut bad = sensor("a", false);
        bad.is_ok = Some(false);
        let out = st.refresh(&[bad, sensor("b", true)], t, &mut ev);
        assert_eq!(out.system_status, Some(Some(SystemSeverity::Critical)));
    }

    #[test]
    fn a_row_is_rewritten_only_when_a_field_changes() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();
        assert_eq!(st.refresh(&[sensor("a", false)], t, &mut ev).rows.len(), 1);
        assert_eq!(st.refresh(&[sensor("a", false)], t, &mut ev).rows.len(), 0);
        assert_eq!(st.refresh(&[sensor("a", true)], t, &mut ev).rows.len(), 1);
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

        publish_profiles_and_seed(tables, &[]).unwrap();
        assert_eq!(
            m.system.field("system", "device_leak_status").as_deref(),
            Some("CRITICAL"),
            "seeding must not overwrite a standing status"
        );
    }

    /// Python appends the timestamp only when it writes, and deliberately
    /// keeps it out of the tuple it compares against
    /// (`thermalctld:LiquidCoolingUpdater._refresh_leak_status`).  Both halves
    /// matter: without the field an operator cannot tell when the state last
    /// moved, and with it inside the comparison the row would be rewritten
    /// every half second.
    #[test]
    fn a_sensor_row_carries_a_timestamp_that_does_not_drive_rewrites() {
        let m = MockLeak::new();
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();

        let out = st.refresh(&[sensor("leakage1", false)], t, &mut ev);
        assert!(apply(&m.tables, &out).sensors.is_empty());
        assert!(
            m.sensor.field("leakage1", "timestamp").is_some(),
            "the row an operator reads must say when it was written"
        );
        assert_eq!(
            m.sensor.field("leakage1", "leak_severity").as_deref(),
            Some("CRITICAL")
        );

        let out = st.refresh(&[sensor("leakage1", false)], t, &mut ev);
        assert!(
            out.rows.is_empty(),
            "nothing about the sensor changed, so the timestamp must not make it look like it did"
        );
    }

    /// A seed read that fails is reported, and the standing status is left
    /// alone.
    ///
    /// Python reads this bare (`self.system_table.get('system')` in
    /// `thermalctld:LiquidCoolingUpdater.__init__`).  Treating an unreadable
    /// table as "not seeded yet" would write `None` over a CRITICAL that is
    /// still true.
    #[test]
    fn an_unreadable_status_is_reported_and_not_overwritten() {
        let m = MockLeak::new();
        pmon_common::db::TableLike::set(
            &m.system,
            "system",
            &[("device_leak_status", "CRITICAL".to_string())],
        )
        .unwrap();
        m.system.fail_reads("redis is gone");

        let e = publish_profiles_and_seed(&m.tables, &[])
            .expect_err("a status that cannot be read has to be reported");
        assert!(
            e.contains("SYSTEM_LEAK_STATUS"),
            "the error names the table: {e}"
        );
        assert_eq!(
            m.system.field("system", "device_leak_status").as_deref(),
            Some("CRITICAL"),
            "the standing alarm was not overwritten"
        );
    }

    #[test]
    fn an_empty_table_is_seeded_with_none() {
        let m = MockLeak::new();
        publish_profiles_and_seed(&m.tables, &[]).unwrap();
        assert_eq!(
            m.system.field("system", "device_leak_status").as_deref(),
            Some("None")
        );
    }

    #[test]
    fn profiles_are_published_once_per_type() {
        let m = MockLeak::new();
        let profiles = vec![
            LeakProfile {
                r#type: "fast".into(),
                max_minor_duration_sec: Some(30),
            },
            LeakProfile {
                r#type: "slow".into(),
                max_minor_duration_sec: None,
            },
        ];
        publish_profiles_and_seed(&m.tables, &profiles).unwrap();
        assert_eq!(m.profile.keys(), ["fast", "slow"]);
        assert_eq!(
            m.profile.field("fast", "max_minor_duration_sec").as_deref(),
            Some("30")
        );
        assert_eq!(
            m.profile.field("slow", "max_minor_duration_sec").as_deref(),
            Some("None"),
            "no limit is inf, as Python's float(inf) prints"
        );
    }

    #[test]
    fn an_outcome_lands_in_both_tables() {
        let m = MockLeak::new();
        let tables = &m.tables;
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let out = st.refresh(&[sensor("leakage1", true)], Instant::now(), &mut ev);
        let _ = apply(tables, &out);
        assert_eq!(
            m.sensor.field("leakage1", "leaking").as_deref(),
            Some("Yes")
        );
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
        let _ = apply(
            tables,
            &st.refresh(&[sensor("leakage1", false)], t, &mut ev),
        );
        let before = m.sensor.row("leakage1");

        m.sensor.fail_writes("must not be written again");
        m.system.fail_writes("must not be written again");
        let _ = apply(
            tables,
            &st.refresh(&[sensor("leakage1", false)], t, &mut ev),
        );
        assert_eq!(m.sensor.row("leakage1"), before);
    }

    #[test]
    fn unrated_leaks_do_not_manufacture_a_critical_alarm() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();
        let out = st.refresh(&[unrated("a", true), unrated("b", true)], t, &mut ev);
        assert_eq!(out.system_status, Some(None));
    }

    #[test]
    fn one_unrated_leaking_sensor_keeps_its_own_severity() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();
        let out = st.refresh(&[unrated("a", true)], t, &mut ev);
        assert_eq!(out.system_status, Some(None));
    }

    // ── Recovery notices, and the write failures ──────────────────────────

    fn faulty(name: &str) -> LeakSensorInfo {
        let mut s = sensor(name, false);
        s.is_ok = Some(false);
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

        st.refresh(&[faulty("leakage1")], t, &mut ev);
        assert!(
            ev.lines.iter().any(|l| l.contains("reported faulty")),
            "{:?}",
            ev.lines
        );

        ev.lines.clear();
        st.refresh(&[sensor("leakage1", false)], t, &mut ev);
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

            st.refresh(&[severe("leakage1", sev)], t, &mut ev);
            ev.lines.clear();
            st.refresh(&[sensor("leakage1", false)], t, &mut ev);
            assert!(
                ev.lines.iter().any(|l| l.contains(want)),
                "{sev:?}: {:?}",
                ev.lines
            );
        }
    }

    /// A platform that downgrades a sensor out of CRITICAL says so, and forgets
    /// it — so a later CRITICAL is a new event rather than a silent repeat.
    #[test]
    fn a_downgrade_out_of_critical_is_announced_and_rearms() {
        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let t = Instant::now();

        st.refresh(&[severe("leakage1", LeakSeverity::Critical)], t, &mut ev);
        ev.lines.clear();

        st.refresh(&[severe("leakage1", LeakSeverity::Minor)], t, &mut ev);
        assert!(
            ev.lines
                .iter()
                .any(|l| l.contains("downgraded from CRITICAL to MINOR")),
            "{:?}",
            ev.lines
        );

        ev.lines.clear();
        st.refresh(&[severe("leakage1", LeakSeverity::Critical)], t, &mut ev);
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
        let mut sensor_crit = sensor("leakage1", true);
        sensor_crit.profile_type = Some("chassis".to_string());

        // First pass reaches CRITICAL, but the aggregate write is refused.
        m.system.fail_writes("redis is down");
        let out = st.refresh(&[sensor_crit.clone()], Instant::now(), &mut ev);
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
        let out = st.refresh(&[sensor_crit], Instant::now(), &mut ev);
        assert!(
            out.system_status.is_some(),
            "the dropped value is rewritten"
        );
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
        let out = st.refresh(&[sensor("leakage1", true)], Instant::now(), &mut ev);
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
        let out = st.refresh(&[sensor("leakage1", true)], Instant::now(), &mut ev);
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

        // Writes only: the seed *read* answers, so the profile and seed writes
        // are the ones refused -- and those are logged, not fatal.
        publish_profiles_and_seed(
            &m.tables,
            &[LeakProfile {
                r#type: "chassis".into(),
                max_minor_duration_sec: None,
            }],
        )
        .expect("refused writes here are logged, not returned");
        assert!(m.profile.is_empty());

        let mut st = LeakState::new();
        let mut ev = Recorder::default();
        let _ = apply(
            &m.tables,
            &st.refresh(&[sensor("leakage1", true)], Instant::now(), &mut ev),
        );
        assert!(m.sensor.is_empty());
        // The event still reached the BMC log, which is the half that does not
        // depend on redis.
        assert!(!ev.lines.is_empty());
    }

    #[test]
    fn current_sensor_duration_is_independent_of_the_published_registry() {
        let mock = crate::db::mock::MockLeak::new();
        publish_profiles_and_seed(&mock.tables, &profile(300.0)).unwrap();
        let mut state = LeakState::new();
        let mut events = Recorder::default();
        let now = Instant::now();
        state.refresh(&[timed_minor("a", true, 30)], now, &mut events);
        let outcome = state.refresh(
            &[timed_minor("a", true, 5)],
            now + Duration::from_secs(5),
            &mut events,
        );
        assert_eq!(outcome.system_status, Some(Some(SystemSeverity::Major)));
        assert_eq!(
            mock.profile.field("p", "max_minor_duration_sec").as_deref(),
            Some("300")
        );
    }
}
