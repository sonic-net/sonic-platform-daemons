//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! Port of FanStatus / FanUpdater.
//!
//! Reads fan and drawer state directly from the `PlatformApi` trait object
//! (Mellanox: sysfs) instead of a gRPC client, then writes FAN_INFO,
//! FAN_DRAWER_INFO and PHYSICAL_ENTITY_INFO to STATE_DB.

use std::collections::HashMap;

use platform_traits::{FanDrawerInfo, FanInfo, FanKind, PlatformApi};

use crate::db::{StateDb, CHASSIS_INFO_KEY};
use crate::fmt;

const LED_GREEN: &str = "green";
const LED_RED:   &str = "red";

/// Per-cycle tallies — reset at the top of each `update()` call.
#[derive(Default)]
struct BadFanCounters {
    absent: usize,
    faulty: usize,
}

impl BadFanCounters {
    fn total(&self) -> usize {
        self.absent + self.faulty
    }
}

struct FanStatus {
    presence: bool,
    status: bool,
    under_speed: bool,
    over_speed: bool,
    invalid_direction: bool,
    led_initialized: bool,
}

impl Default for FanStatus {
    /// Optimistic defaults so the first bad reading is reported as a change.
    fn default() -> Self {
        Self {
            presence: true,
            status: true,
            under_speed: false,
            over_speed: false,
            invalid_direction: false,
            led_initialized: false,
        }
    }
}

impl FanStatus {
    fn set_presence(&mut self, presence: bool, kind: FanKind, counters: &mut BadFanCounters) -> bool {
        // Only drawer fans count toward "insufficient working fans".
        if !presence && kind == FanKind::Drawer {
            counters.absent += 1;
        }
        if presence == self.presence { return false; }
        self.presence = presence;
        true
    }

    fn set_fault_status(&mut self, status: bool, counters: &mut BadFanCounters) -> bool {
        if !status { counters.faulty += 1; }
        if status == self.status { return false; }
        self.status = status;
        true
    }

    fn set_under_speed(&mut self, is_under_speed: Option<bool>) -> bool {
        let v = match is_under_speed {
            Some(v) => v,
            None => {
                if self.under_speed {
                    log::warn!("Fan under speed threshold check became unavailable");
                }
                false
            }
        };
        let old = self.under_speed;
        self.under_speed = v;
        old != v
    }

    fn set_over_speed(&mut self, is_over_speed: Option<bool>) -> bool {
        let v = match is_over_speed {
            Some(v) => v,
            None => {
                if self.over_speed {
                    log::warn!("Fan over speed threshold check became unavailable");
                }
                false
            }
        };
        let old = self.over_speed;
        self.over_speed = v;
        old != v
    }

    fn is_ok(&self) -> bool {
        self.presence && self.status && !self.under_speed && !self.over_speed && !self.invalid_direction
    }
}

/// The `FAN_DRAWER_INFO` row.  Field names and order are part of requirement
/// 1a, so this is built in one place and asserted in the tests.
fn drawer_row(drawer: &FanDrawerInfo) -> [(&'static str, String); 5] {
    [
        ("presence",      fmt::bool(drawer.presence)),
        ("model",         fmt::opt_str(&drawer.model)),
        ("serial",        fmt::opt_str(&drawer.serial)),
        // Python calls get_status() here and gets "N/A" only because
        // DeviceBase raises NotImplementedError on Mellanox.  Pass the
        // vendor's answer through so a platform that has one is not silently
        // dropped; a platform without reports None, which formats as "N/A".
        ("status",        fmt::opt_bool(drawer.status)),
        ("is_replaceable", fmt::bool(drawer.is_replaceable)),
    ]
}

/// The `FAN_INFO` row.
///
/// An absent fan reports `N/A` for everything it could not be asked: Python
/// initialises those six fields to NOT_AVAILABLE and only fills them in under
/// `if presence:` (`thermalctld:464-478`), and only aggregates `status` when
/// the fan answered at all (`:517-518`).  Writing the vendor's values anyway
/// would put `False` in `status` where Python leaves `N/A`.
///
/// For a *present* fan, `status` is the aggregate health rather than the raw
/// fault bit, so one that is unbroken but over speed reports false.
fn fan_row(
    fan: &FanInfo,
    status: &FanStatus,
    drawer_name: String,
) -> [(&'static str, String); 12] {
    let na = || fmt::NOT_AVAILABLE.to_string();
    [
        ("presence",     fmt::bool(fan.presence)),
        ("drawer_name",  drawer_name),
        ("model",        fmt::opt_str(&fan.model)),
        ("serial",       fmt::opt_str(&fan.serial)),
        ("status",       if fan.presence { fmt::bool(status.is_ok()) } else { na() }),
        ("direction",    if fan.presence { fmt::direction(fan.direction) } else { na() }),
        ("speed",        if fan.presence { fmt::opt_u32(fan.speed_pct) } else { na() }),
        ("speed_target", if fan.presence { fmt::opt_u32(fan.target_speed_pct) } else { na() }),
        ("is_under_speed", if fan.presence { fmt::opt_bool(fan.is_under_speed) } else { na() }),
        ("is_over_speed",  if fan.presence { fmt::opt_bool(fan.is_over_speed) } else { na() }),
        ("is_replaceable", fmt::bool(fan.is_replaceable)),
        ("timestamp",    fmt::timestamp()),
    ]
}

/// A pending LED write, collected during the pass and flushed afterwards.
struct LedWrite {
    fan_name: String,
    drawer_name: String,
    color: &'static str,
}

pub struct FanUpdater {
    status: HashMap<String, FanStatus>,
    previous_bad_fan_count: usize,
}

impl FanUpdater {
    pub fn new() -> Self {
        Self {
            status: HashMap::new(),
            previous_bad_fan_count: 0,
        }
    }

    /// Synchronous update — called once per polling cycle from `Monitor`.
    ///
    /// `stop` is checked between devices, at the four points Python checks its
    /// stopping event (`thermalctld:379`, `:397`, `:560`, `:574`), so a
    /// shutdown does not have to wait out a pass over every drawer, fan and
    /// PSU on a modular chassis.  An interrupted pass stops where it is and
    /// reports nothing further — including the bad-fan count, which would
    /// otherwise be a total over the devices it happened to reach.
    pub fn update(
        &mut self,
        platform: &mut dyn PlatformApi,
        db: &StateDb,
        stop: &dyn Fn() -> bool,
    ) -> Result<(), Box<dyn std::error::Error>> {
        let drawers = platform.get_fan_drawers()?;
        let fans    = platform.get_fans()?;
        let mut counters  = BadFanCounters::default();
        let mut led_writes: Vec<LedWrite> = Vec::new();

        for drawer in &drawers {
            if stop() {
                return Ok(());
            }
            self.refresh_drawer(db, drawer);
        }
        for fan in &fans {
            if stop() {
                return Ok(());
            }
            self.refresh_fan(db, fan, &mut counters, &mut led_writes);
        }

        // Python re-reads get_status_led() after setting it so led_status
        // reflects what the hardware actually accepted.  Keep that behaviour:
        // take a fresh snapshot only when something was written, which in
        // steady state (after the first cycle) is never.
        if led_writes.is_empty() {
            self.update_led_color(db, &fans, &drawers, stop);
        } else {
            for write in &led_writes {
                if let Err(e) = platform.set_fan_led(
                    &write.fan_name,
                    &write.drawer_name,
                    write.color,
                ) {
                    // NotSupported is expected for Phase-1 Mellanox (no SW LED).
                    log::warn!(
                        "Failed to set status LED for fan {}, set_status_led not implemented: {}",
                        write.fan_name,
                        e
                    );
                }
            }
            let fresh_fans    = platform.get_fans().unwrap_or_default();
            let fresh_drawers = platform.get_fan_drawers().unwrap_or_default();
            self.update_led_color(db, &fresh_fans, &fresh_drawers, stop);
        }

        let bad_fan_count = counters.total();
        if bad_fan_count > 0 && self.previous_bad_fan_count != bad_fan_count {
            log::warn!(
                "Insufficient number of working fans warning: {} fan{} not working",
                bad_fan_count,
                if bad_fan_count == 1 { " is" } else { "s are" }
            );
        } else if self.previous_bad_fan_count > 0 && bad_fan_count == 0 {
            crate::logging::notice!(
                "Insufficient number of working fans warning cleared: all fans are back to normal"
            );
        }
        self.previous_bad_fan_count = bad_fan_count;

        Ok(())
    }

    fn refresh_drawer(&mut self, db: &StateDb, drawer: &FanDrawerInfo) {
        // A drawer that cannot name itself is not published at all: Python
        // returns before writing either table (`thermalctld:425-427`).  On
        // Mellanox that is every virtual drawer, whose name is literally "N/A"
        // (`fan_drawer.py:118-119`); writing them would create a row Python
        // never creates, and every virtual drawer would overwrite the last.
        if drawer.name == fmt::NOT_AVAILABLE {
            return;
        }

        db.set_entity_info(
            &drawer.name,
            CHASSIS_INFO_KEY,
            &fmt::position(drawer.position_in_parent),
        );

        let fvs = drawer_row(drawer);
        if let Err(e) = db.fan_drawer.set(&drawer.name, &fvs) {
            log::warn!("failed to update FAN_DRAWER_INFO for {}: {e}", drawer.name);
        }
    }

    fn refresh_fan(
        &mut self,
        db: &StateDb,
        fan: &FanInfo,
        counters: &mut BadFanCounters,
        led_writes: &mut Vec<LedWrite>,
    ) {
        let name = fan.name.as_str();
        db.set_entity_info(
            name,
            &fan.parent_name,
            &fmt::position(fan.position_in_parent),
        );

        let status = self.status.entry(name.to_string()).or_default();
        let mut set_led = !status.led_initialized;

        if status.set_presence(fan.presence, fan.kind, counters) {
            set_led = true;
            log_status_change(
                status.presence,
                &format!("Fan removed warning cleared: {name} was inserted"),
                &format!(
                    "Fan removed warning: {name} was removed from the system, \
                     potential overheat hazard"
                ),
            );
        }

        if fan.presence {
            if status.set_fault_status(fan.status, counters) {
                set_led = true;
                log_status_change(
                    status.status,
                    &format!("Fan fault warning cleared: {name} is back to normal"),
                    &format!("Fan fault warning: {name} is broken"),
                );
            }

            if status.set_under_speed(fan.is_under_speed) {
                set_led = true;
                log_status_change(
                    !status.under_speed,
                    &format!("Fan low speed warning cleared: {name} speed is back to normal"),
                    &format!(
                        "Fan low speed warning: {} current speed={}, target speed={}",
                        name,
                        fmt::opt_u32(fan.speed_pct),
                        fmt::opt_u32(fan.target_speed_pct)
                    ),
                );
            }

            if status.set_over_speed(fan.is_over_speed) {
                set_led = true;
                log_status_change(
                    !status.over_speed,
                    &format!("Fan high speed warning cleared: {name} speed is back to normal"),
                    &format!(
                        "Fan high speed warning: {} current speed={}, target speed={}",
                        name,
                        fmt::opt_u32(fan.speed_pct),
                        fmt::opt_u32(fan.target_speed_pct)
                    ),
                );
            }
        }

        // PSU LEDs are managed by psud; module LEDs by their module owner.
        if set_led && fan.kind == FanKind::Drawer {
            let color = if status.is_ok() { LED_GREEN } else { LED_RED };
            led_writes.push(LedWrite {
                fan_name:    name.to_string(),
                drawer_name: fan.drawer_name.clone(),
                color,
            });
            // Mark initialized so we only retry on state changes, not every cycle.
            status.led_initialized = true;
        }

        let drawer_name_str = if fan.drawer_name.is_empty() {
            fmt::NOT_AVAILABLE.to_string()
        } else {
            fan.drawer_name.clone()
        };

        let fvs = fan_row(fan, status, drawer_name_str);
        if let Err(e) = db.fan.set(name, &fvs) {
            log::warn!("failed to update FAN_INFO for {name}: {e}");
        }
    }

    fn update_led_color(
        &self,
        db: &StateDb,
        fans: &[FanInfo],
        drawers: &[FanDrawerInfo],
        stop: &dyn Fn() -> bool,
    ) {
        for fan in fans {
            if stop() {
                return;
            }
            let fvs = [("led_status", fmt::opt_str(&fan.status_led))];
            if let Err(e) = db.fan.set(&fan.name, &fvs) {
                log::warn!("Failed to get status LED state for fan {} - {e}", fan.name);
            }
        }
        for drawer in drawers {
            if stop() {
                return;
            }
            // Same skip as refresh_drawer, and Python spells it out again here
            // rather than relying on the earlier one (`thermalctld:576-578`).
            if drawer.name == fmt::NOT_AVAILABLE {
                continue;
            }
            let fvs = [("led_status", fmt::opt_str(&drawer.status_led))];
            if let Err(e) = db.fan_drawer.set(&drawer.name, &fvs) {
                log::warn!(
                    "Failed to get status LED state for fan drawer {} - {e}",
                    drawer.name
                );
            }
        }
    }
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
    use platform_traits::{FanDirection, PlatformError};

    #[test]
    fn absent_drawer_fans_counted_every_cycle() {
        let mut status = FanStatus::default();
        let mut counters = BadFanCounters::default();
        assert!(status.set_presence(false, FanKind::Drawer, &mut counters));
        assert!(!status.set_presence(false, FanKind::Drawer, &mut counters));
        assert_eq!(counters.absent, 2);
    }

    #[test]
    fn psu_fans_do_not_count_toward_bad_fans() {
        let mut status = FanStatus::default();
        let mut counters = BadFanCounters::default();
        status.set_presence(false, FanKind::Psu, &mut counters);
        assert_eq!(counters.absent, 0);
    }

    #[test]
    fn unavailable_speed_check_clears_alarm() {
        let mut status = FanStatus::default();
        assert!(status.set_under_speed(Some(true)));
        assert!(status.under_speed);
        assert!(status.set_under_speed(None));
        assert!(!status.under_speed);
    }

    #[test]
    fn is_ok_requires_every_condition() {
        let mut status = FanStatus::default();
        assert!(status.is_ok());
        status.over_speed = true;
        assert!(!status.is_ok());
    }

    fn drawer(name: &str) -> FanDrawerInfo {
        FanDrawerInfo {
            name: name.to_string(),
            position_in_parent: 1,
            presence: true,
            status: None,
            is_replaceable: true,
            model: None,
            serial: None,
            status_led: None,
        }
    }

    fn fan(name: &str) -> FanInfo {
        FanInfo {
            name: name.to_string(),
            kind: FanKind::Drawer,
            drawer_name: "drawer1".to_string(),
            parent_name: "drawer1".to_string(),
            position_in_parent: 1,
            presence: true,
            status: true,
            direction: Some(FanDirection::Intake),
            speed_pct: Some(50),
            target_speed_pct: Some(50),
            is_under_speed: Some(false),
            is_over_speed: Some(false),
            is_replaceable: true,
            model: None,
            serial: None,
            status_led: None,
        }
    }

    fn keys<const N: usize>(row: &[(&'static str, String); N]) -> Vec<&'static str> {
        row.iter().map(|(k, _)| *k).collect()
    }

    fn val<const N: usize>(row: &[(&'static str, String); N], key: &str) -> String {
        row.iter().find(|(k, _)| *k == key).unwrap().1.clone()
    }

    /// Requirement 1a is field names *and* order, so both are asserted.
    #[test]
    fn the_drawer_row_has_the_expected_fields_in_order() {
        assert_eq!(
            keys(&drawer_row(&drawer("drawer1"))),
            ["presence", "model", "serial", "status", "is_replaceable"]
        );
    }

    #[test]
    fn the_fan_row_has_the_expected_fields_in_order() {
        let f = fan("fan1");
        assert_eq!(
            keys(&fan_row(&f, &FanStatus::default(), "drawer1".into())),
            [
                "presence", "drawer_name", "model", "serial", "status", "direction",
                "speed", "speed_target", "is_under_speed", "is_over_speed",
                "is_replaceable", "timestamp"
            ]
        );
    }

    /// A platform that reports no drawer health writes N/A, which is what
    /// Mellanox does - DeviceBase raises NotImplementedError there and Python
    /// falls back to try_get's default.  A platform that does report one has
    /// it published rather than dropped.
    #[test]
    fn the_drawer_status_is_reported_or_not_available() {
        let mut d = drawer("drawer1");
        assert_eq!(val(&drawer_row(&d), "status"), "N/A");
        d.status = Some(false);
        assert_eq!(val(&drawer_row(&d), "status"), "False");
    }

    /// The row a healthy, fully-populated fan publishes — every field, by
    /// value.
    ///
    /// Each case below pins one field going to `N/A`; this pins the baseline
    /// they deviate from, and it is the assertion the others cannot replace. A
    /// field that quietly stopped carrying the hardware's answer — publishing
    /// `N/A` where there was a value — leaves every N/A case green, which is
    /// exactly how the LED colour stayed `N/A` for the whole of development.
    #[test]
    fn a_healthy_fan_publishes_every_field_by_value() {
        let mut f = fan("fan1");
        f.model = Some("MTEF-FANF-A".to_string());
        f.serial = Some("MT1234X00001".to_string());
        f.speed_pct = Some(50);
        f.target_speed_pct = Some(60);
        let row = fan_row(&f, &FanStatus::default(), "drawer1".to_string());

        assert_eq!(val(&row, "presence"), "True");
        assert_eq!(val(&row, "drawer_name"), "drawer1");
        assert_eq!(val(&row, "model"), "MTEF-FANF-A");
        assert_eq!(val(&row, "serial"), "MT1234X00001");
        assert_eq!(val(&row, "status"), "True");
        assert_eq!(val(&row, "direction"), "intake");
        assert_eq!(val(&row, "speed"), "50", "a percentage, with no unit or decimal");
        assert_eq!(val(&row, "speed_target"), "60");
        assert_eq!(val(&row, "is_under_speed"), "False");
        assert_eq!(val(&row, "is_over_speed"), "False");
        assert_eq!(val(&row, "is_replaceable"), "True");
        // The clock moves, so the shape is what can be pinned: Python's
        // `strftime('%Y%m%d %H:%M:%S')`, which downstream parses.
        let ts = val(&row, "timestamp");
        assert_eq!(ts.len(), 17, "{ts:?}");
        assert_eq!(&ts[8..9], " ");
        assert!(ts[..8].chars().all(|c| c.is_ascii_digit()), "{ts:?}");
    }

    /// Booleans are Python's `str(bool)` — capitalised — and not `true`/`1`.
    /// Downstream `show platform fan` compares the strings.
    #[test]
    fn booleans_are_written_the_way_python_spells_them() {
        let mut f = fan("fan1");
        f.is_replaceable = false;
        f.direction = Some(FanDirection::Exhaust);
        let row = fan_row(&f, &FanStatus::default(), "d".to_string());
        assert_eq!(val(&row, "is_replaceable"), "False");
        assert_eq!(val(&row, "direction"), "exhaust", "lower case, unlike the booleans");
    }

    /// The drawer's baseline, for the same reason.
    #[test]
    fn a_healthy_drawer_publishes_every_field_by_value() {
        let mut d = drawer("drawer1");
        d.model = Some("MTEF-FAND".to_string());
        d.serial = Some("MT9999X00002".to_string());
        d.status = Some(true);
        let row = drawer_row(&d);

        assert_eq!(val(&row, "presence"), "True");
        assert_eq!(val(&row, "model"), "MTEF-FAND");
        assert_eq!(val(&row, "serial"), "MT9999X00002");
        assert_eq!(val(&row, "status"), "True");
        assert_eq!(val(&row, "is_replaceable"), "True");
        assert_eq!(keys(&row).len(), 5);
    }

    #[test]
    fn absent_model_and_serial_are_not_available() {
        let row = drawer_row(&drawer("drawer1"));
        assert_eq!(val(&row, "model"), "N/A");
        assert_eq!(val(&row, "serial"), "N/A");
    }

    /// For a *present* fan the status field is the aggregate, so one that is
    /// unbroken but over speed reports false.
    #[test]
    fn a_present_fan_reports_the_aggregate_not_the_fault_bit() {
        let f = fan("fan1");
        let mut st = FanStatus::default();
        assert_eq!(val(&fan_row(&f, &st, "d".into()), "status"), "True");
        st.over_speed = true;
        assert_eq!(val(&fan_row(&f, &st, "d".into()), "status"), "False");
    }

    /// An absent fan reports N/A for everything it could not be asked.  Python
    /// leaves those six at NOT_AVAILABLE because they sit under `if presence:`;
    /// writing the vendor's values would put False in `status` where Python
    /// leaves N/A.
    #[test]
    fn an_absent_fan_reports_nothing_it_could_not_be_asked() {
        let mut f = fan("fan1");
        f.presence = false;
        let row = fan_row(&f, &FanStatus::default(), "drawer1".into());
        for field in ["status", "direction", "speed", "speed_target", "is_under_speed", "is_over_speed"] {
            assert_eq!(val(&row, field), "N/A", "{field} should be N/A on an absent fan");
        }
        // The six that do not depend on the fan answering are still written.
        assert_eq!(val(&row, "presence"), "False");
        assert_eq!(val(&row, "drawer_name"), "drawer1");
        assert_eq!(val(&row, "is_replaceable"), "True");
        assert_eq!(keys(&row).len(), 12, "the field set does not change");
    }

    #[test]
    fn an_unknown_speed_check_is_not_available() {
        let mut f = fan("fan1");
        f.is_under_speed = None;
        f.speed_pct = None;
        let row = fan_row(&f, &FanStatus::default(), "d".into());
        assert_eq!(val(&row, "is_under_speed"), "N/A");
        assert_eq!(val(&row, "speed"), "N/A");
    }

    #[test]
    fn a_faulty_fan_is_counted_once_per_cycle() {
        let mut status = FanStatus::default();
        let mut counters = BadFanCounters::default();
        assert!(status.set_fault_status(false, &mut counters));
        assert!(!status.set_fault_status(false, &mut counters), "no second transition");
        assert_eq!(counters.faulty, 2, "but it is counted every cycle it is faulty");
    }

    #[test]
    fn over_speed_behaves_like_under_speed_when_unavailable() {
        let mut status = FanStatus::default();
        assert!(status.set_over_speed(Some(true)));
        assert!(status.set_over_speed(None));
        assert!(!status.over_speed);
    }

    #[test]
    fn recovering_presence_is_a_transition_too() {
        let mut status = FanStatus::default();
        let mut counters = BadFanCounters::default();
        status.set_presence(false, FanKind::Drawer, &mut counters);
        assert!(status.set_presence(true, FanKind::Drawer, &mut counters));
        assert!(status.presence);
    }

    // ── The write path, against a dictionary-backed database ──────────────────

    use crate::db::mock::MockDb;

    struct FakePlatform {
        drawers: Vec<FanDrawerInfo>,
        fans: Vec<FanInfo>,
        led_calls: std::cell::RefCell<Vec<(String, String)>>,
        /// A platform with no software-controllable LED, which is what
        /// Mellanox reports for a virtual drawer.
        led_fails: bool,
    }

    impl platform_traits::PlatformApi for FakePlatform {
        fn chassis_info(&self) -> Result<platform_traits::ChassisInfo, PlatformError> {
            Ok(platform_traits::ChassisInfo {
                is_modular_chassis: false,
                is_smartswitch: false,
                is_dpu: false,
                is_liquid_cooled: false,
                slot_or_dpu_id: None,
            })
        }
        fn get_thermals(&mut self) -> Result<Vec<platform_traits::ThermalInfo>, PlatformError> {
            Ok(Vec::new())
        }
        fn get_fan_drawers(&self) -> Result<Vec<FanDrawerInfo>, PlatformError> {
            Ok(self.drawers.clone())
        }
        fn get_fans(&self) -> Result<Vec<FanInfo>, PlatformError> {
            Ok(self.fans.clone())
        }
        fn set_fan_led(&mut self, fan: &str, _drawer: &str, color: &str) -> Result<(), PlatformError> {
            self.led_calls.borrow_mut().push((fan.to_string(), color.to_string()));
            if self.led_fails {
                return Err(PlatformError::NotSupported("no LED on this drawer".into()));
            }
            Ok(())
        }
        fn get_thermal_manager(&self) -> Box<dyn platform_traits::ThermalManager> {
            unimplemented!("not needed by these tests")
        }
    }

    /// A pass that is never told to stop, which is every case but the three
    /// at the end of this module.
    fn never_stop() -> bool {
        false
    }

    fn platform(drawers: Vec<FanDrawerInfo>, fans: Vec<FanInfo>) -> FakePlatform {
        FakePlatform {
            drawers,
            fans,
            led_calls: std::cell::RefCell::new(Vec::new()),
            led_fails: false,
        }
    }

    #[test]
    fn a_fan_and_its_drawer_are_written_with_entity_info() {
        let m = MockDb::new(false);
        let mut u = FanUpdater::new();
        let mut p = platform(vec![drawer("drawer1")], vec![fan("fan1")]);
        u.update(&mut p, &m.db, &never_stop).unwrap();

        assert_eq!(m.fan.field("fan1", "presence").as_deref(), Some("True"));
        assert_eq!(m.fan_drawer.field("drawer1", "status").as_deref(), Some("N/A"));
        assert_eq!(m.physical_entity.field("fan1", "parent_name").as_deref(), Some("drawer1"));
        assert_eq!(m.physical_entity.field("drawer1", "parent_name").as_deref(), Some("chassis 1"));
    }

    /// What the platform reports the LED is showing has to reach `FAN_INFO`
    /// and `FAN_DRAWER_INFO`, for the fan and for its drawer.
    ///
    /// The case above asserts that `set_fan_led` was *called*; this asserts
    /// what was *published*, and only the second one fails when the vendor
    /// stops reporting a colour.  It did stop, for the whole of development:
    /// every `status_led` was hard-coded `None` and every row said `N/A` while
    /// Python published the real colour.
    #[test]
    fn the_led_colour_the_platform_reports_reaches_the_database() {
        let m = MockDb::new(false);
        let mut u = FanUpdater::new();

        let mut d = drawer("drawer1");
        d.status_led = Some("green".to_string());
        let mut f = fan("fan1");
        f.status_led = Some("green".to_string());

        let mut p = platform(vec![d], vec![f]);
        u.update(&mut p, &m.db, &never_stop).unwrap();

        assert_eq!(m.fan.field("fan1", "led_status").as_deref(), Some("green"));
        assert_eq!(m.fan_drawer.field("drawer1", "led_status").as_deref(), Some("green"));
    }

    /// A platform with no software-readable LED reports nothing, which is
    /// `N/A` — the same string Python's `try_get` produces.
    #[test]
    fn a_platform_with_no_led_publishes_not_available() {
        let m = MockDb::new(false);
        let mut u = FanUpdater::new();
        let mut p = platform(vec![drawer("drawer1")], vec![fan("fan1")]);
        u.update(&mut p, &m.db, &never_stop).unwrap();
        assert_eq!(m.fan.field("fan1", "led_status").as_deref(), Some("N/A"));
    }

    /// A failed fan turns its drawer red; a healthy one asks for green.
    #[test]
    fn the_led_colour_follows_the_fan_health() {
        let m = MockDb::new(false);
        let mut u = FanUpdater::new();
        let mut bad = fan("fan1");
        bad.status = false;
        let mut p = platform(vec![drawer("drawer1")], vec![bad]);
        u.update(&mut p, &m.db, &never_stop).unwrap();
        let calls = p.led_calls.borrow().clone();
        assert!(calls.iter().any(|(f, c)| f == "fan1" && c == "red"), "{calls:?}");
    }

    /// Unlike TemperatureUpdater, the fan path does not sweep stale keys each
    /// cycle - Python only clears the tables in __del__, because the fan
    /// inventory does not change while the daemon runs.  Reproducing that
    /// matters: a per-cycle sweep here would delete rows Python leaves.
    #[test]
    fn a_vanished_fan_is_left_in_place_as_python_leaves_it() {
        let m = MockDb::new(false);
        let mut u = FanUpdater::new();
        let mut p = platform(vec![drawer("drawer1")], vec![fan("fan1"), fan("fan2")]);
        u.update(&mut p, &m.db, &never_stop).unwrap();
        assert_eq!(m.fan.len(), 2);

        let mut p = platform(vec![drawer("drawer1")], vec![fan("fan1")]);
        u.update(&mut p, &m.db, &never_stop).unwrap();
        assert_eq!(m.fan.keys(), ["fan1", "fan2"]);
    }

    /// A virtual drawer names itself "N/A", and Python returns before writing
    /// either table (`thermalctld:425-427`, and again at `:576-578` for the LED
    /// refresh).  Writing it would create a row Python never creates, and on a
    /// platform with several virtual drawers each would overwrite the last.
    #[test]
    fn a_drawer_that_cannot_name_itself_is_not_published() {
        let m = MockDb::new(false);
        let mut u = FanUpdater::new();
        let mut virtual_drawer = drawer("N/A");
        virtual_drawer.status_led = Some("green".to_string());
        let mut p = platform(vec![virtual_drawer], vec![fan("fan1")]);
        u.update(&mut p, &m.db, &never_stop).unwrap();

        assert!(m.fan_drawer.is_empty(), "no FAN_DRAWER_INFO row");
        assert_eq!(m.physical_entity.row("N/A"), None, "no PHYSICAL_ENTITY_INFO row");
        // The fan itself is still published.
        assert_eq!(m.fan.field("fan1", "presence").as_deref(), Some("True"));
    }

    /// Two virtual drawers must not collapse into one row either.
    #[test]
    fn several_virtual_drawers_do_not_overwrite_each_other() {
        let m = MockDb::new(false);
        let mut u = FanUpdater::new();
        let mut p = platform(vec![drawer("N/A"), drawer("N/A")], vec![fan("fan1")]);
        u.update(&mut p, &m.db, &never_stop).unwrap();
        assert!(m.fan_drawer.is_empty());
    }

    #[test]
    fn a_failing_write_does_not_stop_the_pass() {
        let m = MockDb::new(false);
        m.fan.fail_writes("redis is down");
        let mut u = FanUpdater::new();
        let mut p = platform(vec![drawer("drawer1")], vec![fan("fan1")]);
        u.update(&mut p, &m.db, &never_stop).unwrap();
        assert!(m.fan.is_empty());
        assert!(!m.fan_drawer.is_empty(), "the drawer is still written");
    }

    // ── Transitions ───────────────────────────────────────────────────────

    /// A fan's LED is driven on the cycle its state *changes*, and not on the
    /// cycles either side.  Rewriting it every cycle would put a sysfs write
    /// per fan per cycle on the hot path for no change in what is displayed;
    /// never rewriting it leaves a red LED on a fan that has recovered.
    #[test]
    fn the_led_is_driven_on_a_change_and_not_on_a_steady_cycle() {
        let m = MockDb::new(false);
        let mut u = FanUpdater::new();
        let mut p = platform(vec![drawer("drawer1")], vec![fan("fan1")]);

        u.update(&mut p, &m.db, &never_stop).unwrap();
        let after_first = p.led_calls.borrow().len();
        assert!(after_first > 0, "the first cycle initialises the LED");

        u.update(&mut p, &m.db, &never_stop).unwrap();
        u.update(&mut p, &m.db, &never_stop).unwrap();
        assert_eq!(
            p.led_calls.borrow().len(),
            after_first,
            "nothing changed, so nothing was written"
        );

        // Now break it.
        p.fans[0].status = false;
        u.update(&mut p, &m.db, &never_stop).unwrap();
        let calls = p.led_calls.borrow().clone();
        assert!(calls.len() > after_first, "the fault is a change");
        assert_eq!(calls.last().unwrap().1, "red");
    }

    /// Under speed, over speed and presence are each their own transition, and
    /// each drives the LED.  Python logs a warning on the way in and clears it
    /// on the way out; the LED write is the observable half of that.
    #[test]
    fn each_of_the_three_alarms_is_its_own_transition() {
        for change in ["under", "over", "absent"] {
            let m = MockDb::new(false);
            let mut u = FanUpdater::new();
            let mut p = platform(vec![drawer("drawer1")], vec![fan("fan1")]);
            u.update(&mut p, &m.db, &never_stop).unwrap();
            let baseline = p.led_calls.borrow().len();

            match change {
                "under" => p.fans[0].is_under_speed = Some(true),
                "over" => p.fans[0].is_over_speed = Some(true),
                _ => p.fans[0].presence = false,
            }
            u.update(&mut p, &m.db, &never_stop).unwrap();
            assert!(
                p.led_calls.borrow().len() > baseline,
                "{change} should be a transition"
            );

            // And recovering is a transition too, not a silent return.
            let after = p.led_calls.borrow().len();
            match change {
                "under" => p.fans[0].is_under_speed = Some(false),
                "over" => p.fans[0].is_over_speed = Some(false),
                _ => p.fans[0].presence = true,
            }
            u.update(&mut p, &m.db, &never_stop).unwrap();
            assert!(
                p.led_calls.borrow().len() > after,
                "{change} recovery should be a transition"
            );
        }
    }

    /// An absent fan's speed alarms are not evaluated at all — Python guards
    /// them behind `if presence:` — so a fan that is pulled while under speed
    /// does not keep raising the alarm.
    #[test]
    fn an_absent_fan_raises_no_speed_alarm() {
        let m = MockDb::new(false);
        let mut u = FanUpdater::new();
        let mut f = fan("fan1");
        f.presence = false;
        f.is_under_speed = Some(true);
        f.is_over_speed = Some(true);
        let mut p = platform(vec![drawer("drawer1")], vec![f]);
        u.update(&mut p, &m.db, &never_stop).unwrap();

        assert_eq!(m.fan.field("fan1", "is_under_speed").as_deref(), Some("N/A"));
        assert_eq!(m.fan.field("fan1", "is_over_speed").as_deref(), Some("N/A"));
    }

    /// A fan in no drawer — a PSU fan — publishes `N/A` for `drawer_name`
    /// rather than an empty string, because that is the field a consumer reads
    /// to decide whether to look for a drawer at all.
    #[test]
    fn a_fan_with_no_drawer_reports_not_available() {
        let m = MockDb::new(false);
        let mut u = FanUpdater::new();
        let mut f = fan("psu1_fan1");
        f.kind = platform_traits::FanKind::Psu;
        f.drawer_name = String::new();
        f.parent_name = "PSU 1".to_string();

        let mut p = platform(Vec::new(), vec![f]);
        u.update(&mut p, &m.db, &never_stop).unwrap();
        assert_eq!(m.fan.field("psu1_fan1", "drawer_name").as_deref(), Some("N/A"));
    }

    /// A drawer row that cannot be written is logged and the fans behind it are
    /// still published: one unwritable row must not cost the whole cycle.
    #[test]
    fn a_refusing_drawer_table_does_not_stop_the_fans() {
        let m = MockDb::new(false);
        m.fan_drawer.fail_writes("redis is down");

        let mut u = FanUpdater::new();
        let mut p = platform(vec![drawer("drawer1")], vec![fan("fan1")]);
        u.update(&mut p, &m.db, &never_stop)
            .expect("a write failure is not an error the caller sees");

        assert!(m.fan_drawer.is_empty());
        assert!(!m.fan.is_empty(), "the fan behind it was still published");
    }

    /// A platform whose LED cannot be driven says so once per attempt and keeps
    /// going: Mellanox returns NotSupported for a virtual drawer, and that is
    /// the normal case rather than a failure of the cycle.
    #[test]
    fn a_platform_that_refuses_the_led_does_not_stop_the_pass() {
        let m = MockDb::new(false);
        let mut u = FanUpdater::new();
        let mut bad = fan("fan1");
        bad.status = false;
        let mut p = platform(vec![drawer("drawer1")], vec![bad]);
        p.led_fails = true;

        u.update(&mut p, &m.db, &never_stop).expect("still not an error");
        assert_eq!(m.fan.field("fan1", "status").as_deref(), Some("False"));
    }

    // ── An interrupted pass ───────────────────────────────────────────────

    /// Told to stop, a pass stops where it is.  Python checks its stopping
    /// event between devices for the same reason: on a modular chassis a pass
    /// over every drawer, fan and PSU is not something a shutdown should have
    /// to wait out.
    #[test]
    fn a_pass_told_to_stop_publishes_nothing() {
        let m = MockDb::new(false);
        let mut u = FanUpdater::new();
        let mut p = platform(vec![drawer("drawer1")], vec![fan("fan1")]);
        u.update(&mut p, &m.db, &|| true).unwrap();

        assert!(m.fan.is_empty());
        assert!(m.fan_drawer.is_empty());
    }

    /// And it stops *without* running the LED refresh, which would otherwise
    /// read back colours for devices this pass never looked at.
    #[test]
    fn an_interrupted_pass_does_not_refresh_leds() {
        let m = MockDb::new(false);
        let mut u = FanUpdater::new();
        let mut f = fan("fan1");
        f.status_led = Some("green".to_string());
        let mut p = platform(vec![drawer("drawer1")], vec![f]);

        u.update(&mut p, &m.db, &|| true).unwrap();
        assert_eq!(m.fan.field("fan1", "led_status"), None);
    }

    /// Stopping partway leaves what was already published in place: the rows
    /// this pass wrote before the signal are still the truth about those
    /// devices.
    #[test]
    fn a_pass_interrupted_after_the_first_cycle_leaves_its_rows() {
        let m = MockDb::new(false);
        let mut u = FanUpdater::new();
        let mut p = platform(vec![drawer("drawer1")], vec![fan("fan1")]);

        u.update(&mut p, &m.db, &never_stop).unwrap();
        assert_eq!(m.fan.field("fan1", "presence").as_deref(), Some("True"));

        u.update(&mut p, &m.db, &|| true).unwrap();
        assert_eq!(
            m.fan.field("fan1", "presence").as_deref(),
            Some("True"),
            "the interrupted pass changed nothing, it did not erase"
        );
    }
}
