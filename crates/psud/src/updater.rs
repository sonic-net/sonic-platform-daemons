//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! PSU_INFO, and the FAN_INFO rows that belong to a PSU rather than a drawer.
//!
//! Ports `psud:DaemonPsud._update_single_power_entity_data` and the four
//! `_update_*` helpers around it (`psud:DaemonPsud._update_psu_entity_info`,
//! `psud:DaemonPsud._update_psu_fan_data`, `psud:DaemonPsud._update_led_color`,
//! `psud:DaemonPsud._update_psu_fan_led_status`).  PSUs and PDBs go through one
//! path: `PdbBase` extends `PsuBase`, Python's two methods both call the same
//! body, and the facade hands both back in one row list with a `kind`.

use std::collections::{BTreeMap, BTreeSet};

use platform_api::{FanInfo, FanKind, LedColor, PsuInfo};
use pmon_common::db::TableLike;
use pmon_common::fmt;

use crate::status::PsuStatus;

/// `CHASSIS_INFO_KEY`: what every PSU says its parent is.
pub const CHASSIS_INFO_KEY: &str = "chassis 1";

/// A colour to put on a device, applied by the caller.
///
/// Collected rather than written inline so the pass over the rows stays a pure
/// function of them: an LED write goes to hardware over i2c and cannot be part
/// of something a test drives.
#[derive(Debug, Clone, PartialEq)]
pub struct LedWrite {
    pub psu: String,
    pub color: LedColor,
}

pub struct Tables<'a> {
    pub psu: &'a dyn TableLike,
    pub fan: &'a dyn TableLike,
    pub entity: &'a dyn TableLike,
}

#[derive(Default)]
pub struct Updater {
    status: BTreeMap<String, PsuStatus>,
    /// True until a full pass has run.  Python spells this on the daemon and
    /// reads it in four places; all four mean "nothing has been published yet,
    /// so publish everything and write every LED".
    first_run: bool,
    /// What has been published, so teardown can take it away again.
    published: BTreeSet<String>,
}

impl Updater {
    pub fn new() -> Self {
        Self { first_run: true, ..Default::default() }
    }

    /// One pass over every PSU and PDB.
    ///
    /// Returns the LED writes the caller should apply; see [`LedWrite`].
    pub fn refresh(
        &mut self,
        psus: &[PsuInfo],
        fans: &[FanInfo],
        tables: &Tables<'_>,
    ) -> Vec<LedWrite> {
        // Rebuilt every pass rather than kept: the keys can be removed by
        // something else -- thermalctld's own teardown does it -- and Python
        // moved this out of __init__ for exactly that reason (the
        // `_update_psu_entity_info()` call in `psud:DaemonPsud.run`).
        for row in psus {
            let fvs = [
                // 0, not -1: `psud:DaemonPsud._update_single_psu_entity_info`
                // uses `try_get(entity.get_position_in_parent, 0)`.  Mellanox
                // does not implement the getter for PDBs, so this is the value
                // every PDB row on a liquid-cooled switch actually carries.
                ("position_in_parent", fmt::position(row.position_in_parent, 0)),
                ("parent_name", CHASSIS_INFO_KEY.to_string()),
            ];
            if let Err(e) = tables.entity.set(&row.name, &fvs) {
                log::error!("Failed to update {} entity info to DB: {e}", row.name);
            }
        }

        // One line per cycle, not one per PSU: the condition it reports is the
        // whole system's, and every PSU would otherwise report it.
        let mut threshold_logged = false;
        let mut leds = Vec::new();

        for row in psus {
            if let Some(led) = self.refresh_one(row, psus, fans, tables, &mut threshold_logged) {
                leds.push(led);
            }
            self.published.insert(row.name.clone());
        }
        self.first_run = false;
        leds
    }

    fn refresh_one(
        &mut self,
        row: &PsuInfo,
        all: &[PsuInfo],
        fans: &[FanInfo],
        tables: &Tables<'_>,
        threshold_logged: &mut bool,
    ) -> Option<LedWrite> {
        let name = row.name.as_str();
        let label = match row.kind {
            platform_api::PowerEntityKind::Psu => "PSU",
            platform_api::PowerEntityKind::Pdb => "PDB",
        };

        // Everything but the identity is read only while the device is there.
        // A PSU that has been pulled has no voltage; publishing the last one
        // the driver happened to return would be worse than saying N/A.
        let present = row.presence;
        let power_good = present && row.power_good;
        let reading = |v: Option<f64>| if present { v } else { None };
        let limit = |t| if present { t } else { None };

        let voltage = reading(row.voltage);
        let voltage_high = limit(row.voltage_high_threshold);
        let voltage_low = limit(row.voltage_low_threshold);
        let temperature = reading(row.temperature);
        let temperature_high = limit(row.temperature_high_threshold);
        let power = reading(row.power);

        let first_run = self.first_run;
        let status = self.status.entry(row.name.clone()).or_default();
        let mut set_led = first_run;

        let presence_changed = status.set_presence(present);
        if presence_changed {
            set_led = true;
            if status.presence {
                pmon_common::notice!("{label} absence warning cleared: {name} is inserted back.");
            } else {
                log::warn!("{label} absence warning: {name} is not present.");
                status.check_power_threshold = false;
                log::error!("{name} is not present.");
            }
        } else if first_run && !present {
            log::error!("{name} is not present.");
        }

        // A PSU that just came or went takes its fans with it, and the fan rows
        // have to move at this daemon's three seconds rather than wait for
        // thermalctld's sixty -- otherwise `show platform psustatus` and `show
        // platform fan` disagree for up to a minute
        // (`psud:DaemonPsud._update_single_power_entity_data` calls
        // `_update_psu_fan_data`).
        if presence_changed || first_run {
            publish_psu_fans(name, present, fans, tables.fan);
        }

        let power_good_changed = status.set_power_good(power_good);
        if present && power_good_changed {
            set_led = true;
            if status.power_good {
                pmon_common::notice!("Power absence warning cleared: {name} power is back to normal.");
            } else {
                log::warn!("Power absence warning: {name} is out of power.");
            }
        }

        if (present && power_good_changed) || first_run {
            // Re-armed from scratch: a PSU that has just come back has not yet
            // drawn anything worth judging.
            status.check_power_threshold = status.power_good
                && row.power_critical_threshold.is_some()
                && row.power_warning_suppress_threshold.is_some()
                && power.is_some();
        }

        let mut power_exceeded = status.power_exceeded_threshold;
        if status.check_power_threshold {
            match (
                power,
                row.power_warning_suppress_threshold,
                row.power_critical_threshold,
            ) {
                (Some(_), Some(suppress), Some(critical)) => {
                    // Every supplier's draw, not this one's: the thresholds are
                    // the system's.  Python sums `get_power()` over the others
                    // and adds this one
                    // (`psud:_sum_system_power_from_other_psus_and_pdbs`).
                    let system_power: f64 = all.iter().filter_map(|p| p.power).sum();
                    power_exceeded = if status.power_exceeded_threshold {
                        // Falling edge uses the *suppress* threshold, which sits
                        // below the critical one; one threshold for both would
                        // chatter at the boundary.
                        system_power >= suppress
                    } else {
                        system_power >= critical
                    };

                    if status.set_power_exceed_threshold(power_exceeded) && !*threshold_logged {
                        if status.power_exceeded_threshold {
                            log::warn!(
                                "PSU power warning: system power {} exceeds the critical threshold {}.",
                                fmt::float(system_power), fmt::float(critical));
                        } else {
                            pmon_common::notice!(
                                "PSU power warning cleared: system power {} is back to normal, \
                                 below the warning suppress threshold {}.",
                                fmt::float(system_power), fmt::float(suppress));
                        }
                        *threshold_logged = true;
                    }
                }
                (None, _, _) => {
                    // Python reaches `float('N/A')` here -- the reading went
                    // away with the check still armed -- which raises out of
                    // the whole row and leaves PSU_INFO unwritten for that
                    // cycle
                    // (`psud:DaemonPsud._update_single_power_entity_data`,
                    // caught by `update_psu_data`'s catch-all as "Failed to
                    // update PSU data").  Disarming and publishing the rest of
                    // the row is the difference, and is why this is not the
                    // message below: the thresholds are fine, the reading is
                    // not.
                    log::error!("{name} power reading became unavailable, \
                                 stopped checking it against the power thresholds");
                    status.check_power_threshold = false;
                    status.power_exceeded_threshold = false;
                }
                _ => {
                    log::error!(
                        "PSU power thresholds become invalid: threshold {} critical threshold {}",
                        fmt::opt_float(row.power_warning_suppress_threshold),
                        fmt::opt_float(row.power_critical_threshold));
                    status.check_power_threshold = false;
                    // The local keeps its earlier value, so this cycle still
                    // publishes the alarm that was standing when the daemon
                    // lost the ability to judge it.  It clears on the next
                    // pass, which is a departure from Python.
                    // `psud:DaemonPsud._update_single_power_entity_data` resets
                    // the status too, but its `set_power_exceed_threshold`
                    // call a few lines on sets it straight back from the
                    // stale local and warns "exceeds the critical threshold
                    // N/A".  With the check disarmed nothing clears it again,
                    // so Python reports the overload on every pass until a
                    // presence or power_good change re-arms the check.
                    status.power_exceeded_threshold = false;
                }
            }
        }

        if present
            && status.set_voltage(
                name,
                voltage,
                voltage_high.map(|t| t.as_f64()),
                voltage_low.map(|t| t.as_f64()),
            )
        {
            set_led = true;
            if status.voltage_good {
                pmon_common::notice!("Voltage warning cleared: {name} voltage is back to normal.");
            } else {
                log::warn!(
                    "Voltage warning: {name} voltage out of range, current voltage={}, \
                     valid range=[{}, {}].",
                    fmt::opt_float(voltage),
                    fmt::threshold(voltage_high),
                    fmt::threshold(voltage_low));
            }
        }

        if present
            && status.set_temperature(name, temperature, temperature_high.map(|t| t.as_f64()))
        {
            set_led = true;
            if status.temperature_good {
                pmon_common::notice!("Temperature warning cleared: {name} temperature is back to normal.");
            } else {
                log::warn!(
                    "Temperature warning: {name} temperature too hot, temperature={}, threshold={}.",
                    fmt::opt_float(temperature),
                    fmt::threshold(temperature_high));
            }
        }

        let led = set_led.then(|| LedWrite {
            psu: row.name.clone(),
            color: if status.is_ok() { LedColor::Green } else { LedColor::Red },
        });

        // `presence` and `status` are lower case where `is_replaceable` and
        // `power_overload` beside them are not; see `fmt::lower_bool`.
        let fvs = [
            ("model", fmt::opt_str(&row.model)),
            ("serial", fmt::opt_str(&row.serial)),
            ("revision", fmt::opt_str(&row.revision)),
            ("temp", fmt::opt_float(temperature)),
            ("temp_threshold", fmt::threshold(temperature_high)),
            ("voltage", fmt::opt_float(voltage)),
            ("voltage_min_threshold", fmt::threshold(voltage_low)),
            ("voltage_max_threshold", fmt::threshold(voltage_high)),
            ("current", fmt::opt_float(reading(row.current))),
            ("power", fmt::opt_float(power)),
            ("power_warning_suppress_threshold", fmt::opt_float(row.power_warning_suppress_threshold)),
            ("power_critical_threshold", fmt::opt_float(row.power_critical_threshold)),
            ("power_overload", fmt::bool(power_exceeded)),
            ("is_replaceable", fmt::bool(row.is_replaceable)),
            ("input_current", fmt::opt_float(reading(row.input_current))),
            ("input_voltage", fmt::opt_float(reading(row.input_voltage))),
            ("max_power", fmt::opt_float(reading(row.maximum_supplied_power))),
            ("presence", fmt::lower_bool(present)),
            ("status", fmt::lower_bool(power_good)),
            ("timestamp", fmt::epoch_timestamp()),
        ];
        if let Err(e) = tables.psu.set(name, &fvs) {
            log::error!("Failed to update {name} info to DB: {e}");
        }

        led
    }

    /// The LED colours, published after they have been applied.
    ///
    /// Takes a fresh snapshot rather than the one the pass ran on: Python reads
    /// `get_status_led()` back from the device here
    /// (`psud:DaemonPsud._update_led_color`), so the colour it publishes is the
    /// one the write above just produced.  Reusing the earlier snapshot would
    /// publish the previous colour for one cycle -- three seconds in which
    /// `show platform psustatus` says green about a PSU whose LED is already
    /// red.
    pub fn update_led_color(&self, psus: &[PsuInfo], fans: &[FanInfo], tables: &Tables<'_>) {
        for row in psus {
            if !self.status.contains_key(&row.name) {
                continue;
            }
            let fvs = [("led_status", fmt::opt_str(&row.status_led.map(|c| c.as_str().to_string())))];
            if let Err(e) = tables.psu.set(&row.name, &fvs) {
                log::error!("Failed to update {} LED status to DB: {e}", row.name);
            }
            for fan in psu_fans(&row.name, fans) {
                let fvs = [(
                    "led_status",
                    fmt::opt_str(&fan.status_led.map(|c| c.as_str().to_string())),
                )];
                if let Err(e) = tables.fan.set(&fan.name, &fvs) {
                    log::error!("Failed to update fan {} LED status to DB: {e}", fan.name);
                }
            }
        }
    }

    /// Drop everything published, for shutdown.
    pub fn clear(&mut self, tables: &Tables<'_>) {
        for name in std::mem::take(&mut self.published) {
            let _ = tables.psu.del(&name);
            let _ = tables.entity.del(&name);
        }
        self.status.clear();
    }
}

/// The fans behind one PSU or PDB.
fn psu_fans<'a>(psu: &str, fans: &'a [FanInfo]) -> impl Iterator<Item = &'a FanInfo> {
    let psu = psu.to_string();
    fans.iter()
        .filter(move |f| matches!(f.kind, FanKind::Psu | FanKind::Pdb) && f.parent_name == psu)
}

/// The FAN_INFO fields psud owns for a PSU fan.
///
/// `presence` and `status` are the *PSU's*, not the fan's
/// (`psud:DaemonPsud._update_psu_fan_data`): psud is reporting whether the tray
/// is there, and thermalctld fills in the rest of the row on its own cycle.
fn publish_psu_fans(psu: &str, present: bool, fans: &[FanInfo], table: &dyn TableLike) {
    for fan in psu_fans(psu, fans) {
        let fvs = [
            ("presence", fmt::bool(present)),
            ("status", fmt::bool(present)),
            (
                "direction",
                if present {
                    fan.direction.map_or_else(|| fmt::NOT_AVAILABLE.to_string(), |d| d.as_str().to_string())
                } else {
                    fmt::NOT_AVAILABLE.to_string()
                },
            ),
            ("speed", if present { fmt::opt_u32(fan.speed_pct) } else { fmt::NOT_AVAILABLE.to_string() }),
            ("timestamp", fmt::timestamp()),
        ];
        if let Err(e) = table.set(&fan.name, &fvs) {
            log::error!("Failed to update fan {} info to DB: {e}", fan.name);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use platform_api::{PowerEntityKind, Threshold};
    use pmon_common::db::MockTable;

    fn psu(name: &str) -> PsuInfo {
        PsuInfo {
            name: name.to_string(),
            kind: PowerEntityKind::Psu,
            position_in_parent: Some(1),
            presence: true,
            is_replaceable: true,
            model: Some("MTEF-PSF".to_string()),
            serial: Some("SN1".to_string()),
            revision: Some("A1".to_string()),
            power_good: true,
            status_led: Some(LedColor::Green),
            voltage: Some(12.0),
            current: Some(5.0),
            power: Some(60.0),
            input_voltage: Some(220.0),
            input_current: Some(0.3),
            input_power: None,
            temperature: Some(40.0),
            temperature_high_threshold: Some(Threshold::Float(60.0)),
            voltage_high_threshold: Some(Threshold::Float(13.0)),
            voltage_low_threshold: Some(Threshold::Float(11.0)),
            maximum_supplied_power: Some(1000.0),
            power_warning_suppress_threshold: None,
            power_critical_threshold: None,
        }
    }

    fn fan(name: &str, parent: &str) -> FanInfo {
        FanInfo {
            name: name.to_string(),
            kind: FanKind::Psu,
            parent_name: parent.to_string(),
            drawer_name: "N/A".to_string(),
            position_in_parent: Some(1),
            presence: true,
            status: true,
            is_replaceable: false,
            model: None,
            serial: None,
            speed_pct: Some(60),
            target_speed_pct: Some(60),
            direction: Some(platform_api::FanDirection::Intake),
            is_under_speed: Some(false),
            is_over_speed: Some(false),
            status_led: Some(LedColor::Green),
        }
    }

    struct Db {
        psu: MockTable,
        fan: MockTable,
        entity: MockTable,
    }

    impl Db {
        fn new() -> Self {
            Self { psu: MockTable::new(), fan: MockTable::new(), entity: MockTable::new() }
        }
        fn tables(&self) -> Tables<'_> {
            Tables { psu: &self.psu, fan: &self.fan, entity: &self.entity }
        }
    }

    #[test]
    fn a_healthy_psu_publishes_every_field() {
        let db = Db::new();
        Updater::new().refresh(&[psu("PSU 1")], &[], &db.tables());
        let f = |k| db.psu.field("PSU 1", k);
        assert_eq!(f("model").as_deref(), Some("MTEF-PSF"));
        assert_eq!(f("voltage").as_deref(), Some("12.0"));
        assert_eq!(f("voltage_max_threshold").as_deref(), Some("13.0"));
        assert_eq!(f("temp").as_deref(), Some("40.0"));
        assert_eq!(f("max_power").as_deref(), Some("1000.0"));
        assert_eq!(db.entity.field("PSU 1", "parent_name").as_deref(), Some("chassis 1"));
    }

    /// The one hash carries both spellings.  A consumer that matches `presence`
    /// against `True` finds nothing; one that matches `is_replaceable` against
    /// `true` likewise.  Both are what Python writes.
    #[test]
    fn presence_is_lower_case_and_is_replaceable_is_not() {
        let db = Db::new();
        Updater::new().refresh(&[psu("PSU 1")], &[], &db.tables());
        assert_eq!(db.psu.field("PSU 1", "presence").as_deref(), Some("true"));
        assert_eq!(db.psu.field("PSU 1", "status").as_deref(), Some("true"));
        assert_eq!(db.psu.field("PSU 1", "is_replaceable").as_deref(), Some("True"));
        assert_eq!(db.psu.field("PSU 1", "power_overload").as_deref(), Some("False"));
    }

    /// A PSU that has been pulled has no readings.  The driver may still answer
    /// with the last value it saw; publishing it would show a voltage for a
    /// slot that is empty.
    #[test]
    fn a_removed_psu_publishes_no_readings() {
        let db = Db::new();
        let mut u = Updater::new();
        let mut gone = psu("PSU 1");
        gone.presence = false;
        u.refresh(&[gone], &[], &db.tables());
        for field in ["voltage", "temp", "current", "power", "max_power", "input_voltage"] {
            assert_eq!(db.psu.field("PSU 1", field).as_deref(), Some("N/A"), "{field}");
        }
        assert_eq!(db.psu.field("PSU 1", "presence").as_deref(), Some("false"));
        // Identity is not a reading: the slot still knows which PSU model was
        // last in it, and `show platform psustatus` shows it.
        assert_eq!(db.psu.field("PSU 1", "model").as_deref(), Some("MTEF-PSF"));
    }

    /// An absent PSU cannot be powered, whatever `get_powergood_status` says.
    #[test]
    fn an_absent_psu_is_never_reported_as_powered() {
        let db = Db::new();
        let mut gone = psu("PSU 1");
        gone.presence = false;
        gone.power_good = true;
        Updater::new().refresh(&[gone], &[], &db.tables());
        assert_eq!(db.psu.field("PSU 1", "status").as_deref(), Some("false"));
    }

    #[test]
    fn the_first_pass_writes_every_led() {
        let db = Db::new();
        let leds = Updater::new().refresh(&[psu("PSU 1"), psu("PSU 2")], &[], &db.tables());
        assert_eq!(leds.len(), 2, "nothing has told the hardware a colour yet");
        assert!(leds.iter().all(|l| l.color == LedColor::Green));
    }

    /// After that, only a change writes.  The LED is an i2c write; doing it
    /// every three seconds for every PSU forever is traffic on the bus that a
    /// PSU firmware upgrade has to share.
    #[test]
    fn a_steady_psu_does_not_rewrite_its_led() {
        let db = Db::new();
        let mut u = Updater::new();
        u.refresh(&[psu("PSU 1")], &[], &db.tables());
        assert!(u.refresh(&[psu("PSU 1")], &[], &db.tables()).is_empty());

        let mut hot = psu("PSU 1");
        hot.temperature = Some(90.0);
        let leds = u.refresh(&[hot], &[], &db.tables());
        assert_eq!(leds, vec![LedWrite { psu: "PSU 1".to_string(), color: LedColor::Red }]);
    }

    /// psud owns five FAN_INFO fields on a PSU fan and thermalctld owns the
    /// rest.  `presence` and `status` here are the PSU's, which is why a
    /// present PSU whose fan has stopped still reads `True` in this row.
    #[test]
    fn a_psu_fan_row_carries_the_psus_presence_not_the_fans() {
        let db = Db::new();
        let mut stopped = fan("PSU 1 FAN 1", "PSU 1");
        stopped.presence = false;
        stopped.status = false;
        Updater::new().refresh(&[psu("PSU 1")], &[stopped], &db.tables());
        assert_eq!(db.fan.field("PSU 1 FAN 1", "presence").as_deref(), Some("True"));
        assert_eq!(db.fan.field("PSU 1 FAN 1", "status").as_deref(), Some("True"));
        assert_eq!(db.fan.field("PSU 1 FAN 1", "speed").as_deref(), Some("60"));
    }

    #[test]
    fn pulling_a_psu_blanks_its_fans() {
        let db = Db::new();
        let mut u = Updater::new();
        let f = fan("PSU 1 FAN 1", "PSU 1");
        u.refresh(&[psu("PSU 1")], std::slice::from_ref(&f), &db.tables());
        let mut gone = psu("PSU 1");
        gone.presence = false;
        u.refresh(&[gone], &[f], &db.tables());
        assert_eq!(db.fan.field("PSU 1 FAN 1", "presence").as_deref(), Some("False"));
        assert_eq!(db.fan.field("PSU 1 FAN 1", "direction").as_deref(), Some("N/A"));
        assert_eq!(db.fan.field("PSU 1 FAN 1", "speed").as_deref(), Some("N/A"));
    }

    /// Another PSU's fans are not this one's.  Matching on the name prefix
    /// instead would make `PSU 1` claim `PSU 10`'s fans.
    #[test]
    fn a_psu_only_publishes_its_own_fans() {
        let db = Db::new();
        Updater::new().refresh(
            &[psu("PSU 1")],
            &[fan("f1", "PSU 1"), fan("f10", "PSU 10")],
            &db.tables(),
        );
        assert_eq!(db.fan.keys(), vec!["f1".to_string()]);
    }

    /// The system draw is every supplier's, not this one's: the thresholds are
    /// stated for the chassis.  Judging each PSU on its own would need four
    /// overloaded PSUs before anything was said.
    #[test]
    fn the_power_alarm_is_raised_on_the_sum_over_every_supplier() {
        let db = Db::new();
        let armed = |power: f64| {
            let mut p = psu("PSU 1");
            p.power = Some(power);
            p.power_critical_threshold = Some(100.0);
            p.power_warning_suppress_threshold = Some(80.0);
            p
        };
        let mut a = armed(60.0);
        let mut b = armed(60.0);
        b.name = "PSU 2".to_string();
        a.name = "PSU 1".to_string();

        let mut u = Updater::new();
        u.refresh(&[a.clone(), b.clone()], &[], &db.tables());
        assert_eq!(db.psu.field("PSU 1", "power_overload").as_deref(), Some("True"),
            "120W over a 100W critical threshold, though neither PSU draws 100W");
    }

    /// Coming back down uses the lower suppress threshold, so a system sitting
    /// on the critical figure does not alternate between the two lines.
    #[test]
    fn clearing_the_power_alarm_waits_for_the_lower_threshold() {
        let db = Db::new();
        let armed = |power: f64| {
            let mut p = psu("PSU 1");
            p.power = Some(power);
            p.power_critical_threshold = Some(100.0);
            p.power_warning_suppress_threshold = Some(80.0);
            p
        };
        let mut u = Updater::new();
        u.refresh(&[armed(120.0)], &[], &db.tables());
        assert_eq!(db.psu.field("PSU 1", "power_overload").as_deref(), Some("True"));
        u.refresh(&[armed(90.0)], &[], &db.tables());
        assert_eq!(db.psu.field("PSU 1", "power_overload").as_deref(), Some("True"),
            "still above the suppress threshold");
        u.refresh(&[armed(70.0)], &[], &db.tables());
        assert_eq!(db.psu.field("PSU 1", "power_overload").as_deref(), Some("False"));
    }

    /// A platform that publishes no thresholds is never judged against them.
    #[test]
    fn a_psu_without_thresholds_never_reports_an_overload() {
        let db = Db::new();
        let mut u = Updater::new();
        u.refresh(&[psu("PSU 1")], &[], &db.tables());
        assert_eq!(db.psu.field("PSU 1", "power_overload").as_deref(), Some("False"));
    }

    /// Published after the write, from a fresh read, so the colour in STATE_DB
    /// is the one on the device rather than the one from before the pass.
    #[test]
    fn the_led_colour_published_is_the_one_read_back() {
        let db = Db::new();
        let mut u = Updater::new();
        u.refresh(&[psu("PSU 1")], &[fan("PSU 1 FAN 1", "PSU 1")], &db.tables());

        let mut after = psu("PSU 1");
        after.status_led = Some(LedColor::Red);
        let mut hot_fan = fan("PSU 1 FAN 1", "PSU 1");
        hot_fan.status_led = Some(LedColor::Amber);
        u.update_led_color(&[after], &[hot_fan], &db.tables());
        assert_eq!(db.psu.field("PSU 1", "led_status").as_deref(), Some("red"));
        assert_eq!(db.fan.field("PSU 1 FAN 1", "led_status").as_deref(), Some("amber"));
    }

    /// A refusing table costs that PSU's row; the LED write has already
    /// happened, because the hardware is the thing that matters most.
    #[test]
    fn a_refusing_table_does_not_stop_the_pass() {
        let db = Db::new();
        db.psu.fail_writes("read-only replica");
        let leds = Updater::new().refresh(&[psu("PSU 1"), psu("PSU 2")], &[], &db.tables());
        assert_eq!(leds.len(), 2, "both LEDs are still written");
        assert!(db.psu.is_empty());
    }

    /// A PDB reports itself as a PDB in its own log lines: an operator reading
    /// "PSU PDB 1 is not present" would go looking for a power supply.
    #[test]
    fn a_pdb_is_published_like_a_psu_and_named_like_a_pdb() {
        let db = Db::new();
        let mut pdb = psu("PDB 1");
        pdb.kind = PowerEntityKind::Pdb;
        Updater::new().refresh(&[pdb], &[], &db.tables());
        assert_eq!(db.psu.field("PDB 1", "presence").as_deref(), Some("true"));
    }

    /// A PSU with no readings at all publishes N/A rather than nothing: the
    /// row's shape is what `show platform psustatus` iterates.
    #[test]
    fn a_psu_the_platform_knows_nothing_about_still_has_a_full_row() {
        let db = Db::new();
        let mut blank = psu("PSU 1");
        blank.model = None;
        blank.serial = None;
        blank.revision = None;
        blank.voltage = None;
        blank.temperature = None;
        blank.status_led = None;
        Updater::new().refresh(&[blank], &[], &db.tables());
        for f in ["model", "serial", "revision", "voltage", "temp"] {
            assert_eq!(db.psu.field("PSU 1", f).as_deref(), Some("N/A"), "{f}");
        }
    }

    /// The LED colour of a PSU the platform will not answer for is N/A, not a
    /// guess: `show platform psustatus` prints it.
    #[test]
    fn an_unknown_led_colour_is_not_available() {
        let db = Db::new();
        let mut u = Updater::new();
        u.refresh(&[psu("PSU 1")], &[], &db.tables());
        let mut dark = psu("PSU 1");
        dark.status_led = None;
        u.update_led_color(&[dark], &[], &db.tables());
        assert_eq!(db.psu.field("PSU 1", "led_status").as_deref(), Some("N/A"));
    }

    /// A PSU that was never published is not one this daemon publishes a
    /// colour for: the LED pass follows the rows it wrote, not the platform.
    #[test]
    fn the_led_pass_only_touches_psus_the_daemon_published() {
        let db = Db::new();
        let u = Updater::new();
        u.update_led_color(&[psu("PSU 1")], &[], &db.tables());
        assert!(db.psu.is_empty());
    }

    /// Every state change is one LED write and one log line, and the pairs
    /// have to be the right way round: an operator who reads "cleared" about a
    /// PSU that just failed stops trusting the log.
    #[test]
    fn each_condition_trips_and_clears_exactly_once() {
        let db = Db::new();
        let mut u = Updater::new();
        u.refresh(&[psu("PSU 1")], &[], &db.tables());

        // Pulled, then back.
        let mut gone = psu("PSU 1");
        gone.presence = false;
        assert_eq!(u.refresh(&[gone.clone()], &[], &db.tables()).len(), 1);
        assert!(u.refresh(&[gone], &[], &db.tables()).is_empty(), "still gone, still quiet");
        assert_eq!(u.refresh(&[psu("PSU 1")], &[], &db.tables()).len(), 1, "inserted back");

        // Power lost, then back.
        let mut dead = psu("PSU 1");
        dead.power_good = false;
        assert_eq!(u.refresh(&[dead], &[], &db.tables()).len(), 1);
        assert_eq!(u.refresh(&[psu("PSU 1")], &[], &db.tables()).len(), 1);

        // Out of voltage range, then back.
        let mut high = psu("PSU 1");
        high.voltage = Some(99.0);
        assert_eq!(u.refresh(&[high], &[], &db.tables()).len(), 1);
        assert_eq!(u.refresh(&[psu("PSU 1")], &[], &db.tables()).len(), 1);

        // Too hot, then back.
        let mut hot = psu("PSU 1");
        hot.temperature = Some(90.0);
        assert_eq!(u.refresh(&[hot], &[], &db.tables()).len(), 1);
        assert_eq!(u.refresh(&[psu("PSU 1")], &[], &db.tables()).len(), 1);
    }

    /// A PSU absent on the very first pass is reported then, not only when it
    /// changes: a switch booted with a tray missing must say so.
    #[test]
    fn a_psu_absent_at_startup_is_reported_on_the_first_pass() {
        let db = Db::new();
        let mut gone = psu("PSU 1");
        gone.presence = false;
        let leds = Updater::new().refresh(&[gone], &[], &db.tables());
        assert_eq!(leds, vec![LedWrite { psu: "PSU 1".to_string(), color: LedColor::Red }]);
    }

    /// The system draw crossing and clearing is one line each, and it is the
    /// system's line: every PSU would otherwise report the same condition.
    #[test]
    fn the_power_alarm_is_logged_once_for_the_system_not_once_per_psu() {
        let db = Db::new();
        let armed = |name: &str, power: f64| {
            let mut p = psu(name);
            p.power = Some(power);
            p.power_critical_threshold = Some(100.0);
            p.power_warning_suppress_threshold = Some(80.0);
            p
        };
        let mut u = Updater::new();
        u.refresh(&[armed("PSU 1", 60.0), armed("PSU 2", 60.0)], &[], &db.tables());
        assert_eq!(db.psu.field("PSU 1", "power_overload").as_deref(), Some("True"));
        assert_eq!(db.psu.field("PSU 2", "power_overload").as_deref(), Some("True"));
        u.refresh(&[armed("PSU 1", 30.0), armed("PSU 2", 30.0)], &[], &db.tables());
        assert_eq!(db.psu.field("PSU 1", "power_overload").as_deref(), Some("False"));
    }

    /// The reading goes away with the check armed.  Python reaches
    /// `float('N/A')` here and abandons the whole row; this disarms and
    /// publishes the rest, which is the one deliberate divergence.
    #[test]
    fn a_power_reading_that_vanishes_disarms_the_check_and_still_publishes() {
        let db = Db::new();
        let mut u = Updater::new();
        let armed = |power: Option<f64>| {
            let mut p = psu("PSU 1");
            p.power = power;
            p.power_critical_threshold = Some(100.0);
            p.power_warning_suppress_threshold = Some(80.0);
            p
        };
        u.refresh(&[armed(Some(200.0))], &[], &db.tables());
        assert_eq!(db.psu.field("PSU 1", "power_overload").as_deref(), Some("True"));

        u.refresh(&[armed(None)], &[], &db.tables());
        assert_eq!(db.psu.field("PSU 1", "voltage").as_deref(), Some("12.0"),
            "the rest of the row is still published");
        u.refresh(&[armed(None)], &[], &db.tables());
        assert_eq!(db.psu.field("PSU 1", "power_overload").as_deref(), Some("False"),
            "and the alarm clears on the pass after");
    }

    /// A threshold that goes away while the check is armed is reported, and
    /// the check disarmed: judging against a limit that is not there would be
    /// judging against zero.
    #[test]
    fn thresholds_that_vanish_disarm_the_check() {
        let db = Db::new();
        let mut u = Updater::new();
        let mut armed = psu("PSU 1");
        armed.power = Some(200.0);
        armed.power_critical_threshold = Some(100.0);
        armed.power_warning_suppress_threshold = Some(80.0);
        u.refresh(&[armed], &[], &db.tables());

        let mut lost = psu("PSU 1");
        lost.power = Some(200.0);
        u.refresh(&[lost.clone()], &[], &db.tables());
        u.refresh(&[lost], &[], &db.tables());
        assert_eq!(db.psu.field("PSU 1", "power_overload").as_deref(), Some("False"));
    }

    /// A PDB is the case this was found on: Mellanox implements
    /// `get_position_in_parent` for PSUs and not for PDBs, so the PDB rows are
    /// the ones that take the fallback.
    #[test]
    fn an_entity_with_no_position_is_published_as_zero_not_minus_one() {
        let db = Db::new();
        let mut pdb = psu("PDB 1");
        pdb.kind = PowerEntityKind::Pdb;
        pdb.position_in_parent = None;
        Updater::new().refresh(&[pdb], &[], &db.tables());
        // `psud:DaemonPsud._update_single_psu_entity_info` uses
        // `try_get(entity.get_position_in_parent, 0)`.  The first port
        // published -1 -- the value the base class documents for "cannot
        // determine" and the value no daemon actually uses -- which is what
        // slm-111 caught: Python wrote 0 for both PDBs, the port wrote -1.
        assert_eq!(db.entity.field("PDB 1", "position_in_parent").as_deref(), Some("0"));
    }

    /// A refusing entity table costs that row and nothing else.
    #[test]
    fn a_refusing_entity_table_does_not_stop_the_psu_row() {
        let db = Db::new();
        db.entity.fail_writes("read-only replica");
        Updater::new().refresh(&[psu("PSU 1")], &[], &db.tables());
        assert!(db.psu.field("PSU 1", "voltage").is_some());
    }

    /// Nor does a refusing fan table.
    #[test]
    fn a_refusing_fan_table_does_not_stop_the_pass() {
        let db = Db::new();
        db.fan.fail_writes("read-only replica");
        let mut u = Updater::new();
        u.refresh(&[psu("PSU 1")], &[fan("PSU 1 FAN 1", "PSU 1")], &db.tables());
        assert!(db.psu.field("PSU 1", "voltage").is_some());

        db.psu.fail_writes("read-only replica");
        u.update_led_color(&[psu("PSU 1")], &[fan("PSU 1 FAN 1", "PSU 1")], &db.tables());
    }

    #[test]
    fn teardown_leaves_nothing_behind() {
        let db = Db::new();
        let mut u = Updater::new();
        u.refresh(&[psu("PSU 1"), psu("PSU 2")], &[], &db.tables());
        u.clear(&db.tables());
        assert!(db.psu.is_empty());
        assert!(db.entity.is_empty());
    }
}
