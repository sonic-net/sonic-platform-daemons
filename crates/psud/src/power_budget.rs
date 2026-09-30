//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! What the chassis can supply against what it draws, and the master PSU LED.
//!
//! Ports `psud:PsuChassisInfo`.  Only a modular chassis runs this: on a fixed
//! platform there are no line cards to account for and Python never constructs
//! the object (`is_modular_chassis()` in `psud:DaemonPsud.run`).

use platform_api::{FanDrawerInfo, LedColor, ModuleInfo, PsuInfo};
use pmon_common::db::TableLike;
use pmon_common::fmt;

pub const CHASSIS_POWER_KEY: &str = "chassis_power_budget 1";
const SUPPLIER_FIELD: &str = "Supplied Power";
const CONSUMER_FIELD: &str = "Consumed Power";
const TOTAL_SUPPLIED_FIELD: &str = "Total Supplied Power";
const TOTAL_CONSUMED_FIELD: &str = "Total Consumed Power";

pub struct PowerBudget {
    /// The first pass always writes the LED, because nothing has told the
    /// hardware what colour it should be yet.
    first_run: bool,
    master_status_good: bool,
    total_supplied_power: f64,
    total_consumed_power: f64,
}

impl Default for PowerBudget {
    fn default() -> Self {
        Self {
            first_run: true,
            master_status_good: true,
            total_supplied_power: 0.0,
            total_consumed_power: 0.0,
        }
    }
}

/// One row of the budget: a field name and what to do with it.
enum Entry {
    /// The device is there; publish its figure.
    Set(String, f64),
    /// It is not; stop listing it, without disturbing the other entries.
    Drop(String),
}

fn supplier(row: &PsuInfo) -> Entry {
    let field = format!("{SUPPLIER_FIELD} {}", row.name);
    if !row.presence {
        return Entry::Drop(field);
    }
    // A present PSU that is not powered supplies nothing -- which is not the
    // same as not being there, and is why this is a 0 rather than a delete.
    if !row.power_good {
        return Entry::Set(field, 0.0);
    }
    Entry::Set(field, row.maximum_supplied_power.unwrap_or(0.0))
}

fn consumer(name: &str, presence: bool, power: Option<f64>) -> Entry {
    let field = format!("{CONSUMER_FIELD} {name}");
    if !presence {
        return Entry::Drop(field);
    }
    Entry::Set(field, power.unwrap_or(0.0))
}

impl PowerBudget {
    /// One accounting pass: write every device's figure and the two totals.
    ///
    /// `psus` carries PSUs and PDBs in that order, which is the order Python
    /// walks them in (`psud:PsuChassisInfo.run_power_budget`, two loops over
    /// two getters).
    pub fn run(
        &mut self,
        psus: &[PsuInfo],
        drawers: &[FanDrawerInfo],
        modules: &[ModuleInfo],
        table: &dyn TableLike,
    ) {
        let mut entries: Vec<Entry> = Vec::new();
        let mut total_supplied = 0.0;
        let mut total_consumed = 0.0;

        for row in psus {
            let e = supplier(row);
            // Only a supplier that is both present and powered counts toward
            // the budget; the 0 written for an unpowered one is a statement
            // about it, not a contribution.
            if let Entry::Set(_, w) = &e {
                if row.presence && row.power_good {
                    total_supplied += w;
                }
            }
            entries.push(e);
        }
        for d in drawers {
            let e = consumer(&d.name, d.presence, d.maximum_consumed_power);
            if let Entry::Set(_, w) = &e {
                total_consumed += w;
            }
            entries.push(e);
        }
        for m in modules {
            let e = consumer(&m.name, m.presence, m.maximum_consumed_power);
            if let Entry::Set(_, w) = &e {
                total_consumed += w;
            }
            entries.push(e);
        }

        self.total_supplied_power = total_supplied;
        self.total_consumed_power = total_consumed;

        let mut row: Vec<(String, String)> = Vec::new();
        for e in &entries {
            match e {
                Entry::Set(field, w) => row.push((field.clone(), fmt::float(*w))),
                Entry::Drop(field) => {
                    if let Err(e) = table.hdel(CHASSIS_POWER_KEY, field) {
                        log::error!("Failed to delete {field} power info from DB: {e}");
                    }
                }
            }
        }
        row.push((TOTAL_SUPPLIED_FIELD.to_string(), fmt::float(total_supplied)));
        row.push((TOTAL_CONSUMED_FIELD.to_string(), fmt::float(total_consumed)));

        let fvs: Vec<(&str, String)> =
            row.iter().map(|(k, v)| (k.as_str(), v.clone())).collect();
        if let Err(e) = table.set(CHASSIS_POWER_KEY, &fvs) {
            log::error!("Failed to update chassis power budget to DB: {e}");
        }
    }

    /// The master LED: green while the chassis supplies more than it draws.
    ///
    /// Returns whether the LED was written, which is what the caller uses to
    /// decide there is a colour to apply.
    pub fn master_status(&mut self) -> Option<LedColor> {
        let mut set_led = self.first_run;
        let mut master_status_good = false;

        // Either total being zero means the accounting has not produced an
        // answer -- no supplier is powered, or no consumer is present -- and a
        // budget with a missing side is not a verdict.  Note what Python then
        // does *not* do: `self.master_status_good` is left alone, so the log
        // line below can report "cleared" while the LED is set red.  That is
        // `psud:PsuChassisInfo.update_master_status` as written, and it is
        // visible on the first cycle of a chassis with no line cards in it.
        if self.total_supplied_power != 0.0 && self.total_consumed_power != 0.0 {
            master_status_good = self.total_consumed_power < self.total_supplied_power;
            if master_status_good != self.master_status_good {
                set_led = true;
            }
            self.master_status_good = master_status_good;
        }

        let verdict = if master_status_good { LedColor::Green } else { LedColor::Red };
        let colour = set_led.then_some(verdict);

        if colour.is_some() {
            if self.master_status_good {
                pmon_common::notice!(
                    "PSU supplied power warning cleared: supplied power is back to normal."
                );
            } else {
                log::warn!(
                    "PSU supplied power warning: {}W supplied-power less than {}W consumed-power",
                    fmt::float(self.total_supplied_power),
                    fmt::float(self.total_consumed_power),
                );
            }
        }

        self.first_run = false;
        colour
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use pmon_common::db::MockTable;

    fn psu(name: &str, presence: bool, power_good: bool, supplied: Option<f64>) -> PsuInfo {
        PsuInfo {
            name: name.to_string(),
            kind: platform_api::PowerEntityKind::Psu,
            position_in_parent: Some(1),
            presence,
            is_replaceable: true,
            model: None,
            serial: None,
            revision: None,
            power_good,
            status_led: None,
            voltage: None,
            current: None,
            power: None,
            input_voltage: None,
            input_current: None,
            input_power: None,
            temperature: None,
            temperature_high_threshold: None,
            voltage_high_threshold: None,
            voltage_low_threshold: None,
            maximum_supplied_power: supplied,
            power_warning_suppress_threshold: None,
            power_critical_threshold: None,
        }
    }

    fn drawer(name: &str, presence: bool, consumed: Option<f64>) -> FanDrawerInfo {
        FanDrawerInfo {
            name: name.to_string(),
            presence,
            maximum_consumed_power: consumed,
            ..Default::default()
        }
    }

    #[test]
    fn the_budget_lists_every_device_and_both_totals() {
        let t = MockTable::new();
        PowerBudget::default().run(
            &[psu("PSU 1", true, true, Some(1000.0))],
            &[drawer("drawer1", true, Some(50.0))],
            &[],
            &t,
        );
        assert_eq!(t.field(CHASSIS_POWER_KEY, "Supplied Power PSU 1").as_deref(), Some("1000.0"));
        assert_eq!(t.field(CHASSIS_POWER_KEY, "Consumed Power drawer1").as_deref(), Some("50.0"));
        assert_eq!(t.field(CHASSIS_POWER_KEY, "Total Supplied Power").as_deref(), Some("1000.0"));
        assert_eq!(t.field(CHASSIS_POWER_KEY, "Total Consumed Power").as_deref(), Some("50.0"));
    }

    /// A PSU that is present but has lost power supplies nothing, and says so
    /// with a zero.  Deleting the field instead would read as "no such PSU",
    /// which is what an operator looking at the budget would then believe.
    #[test]
    fn an_unpowered_supplier_reports_zero_rather_than_disappearing() {
        let t = MockTable::new();
        PowerBudget::default().run(&[psu("PSU 1", true, false, Some(1000.0))], &[], &[], &t);
        assert_eq!(t.field(CHASSIS_POWER_KEY, "Supplied Power PSU 1").as_deref(), Some("0.0"));
        assert_eq!(t.field(CHASSIS_POWER_KEY, "Total Supplied Power").as_deref(), Some("0.0"));
    }

    /// A removed one does disappear -- and only its own field does.  Writing
    /// the whole row afresh would be simpler and would silently drop whatever
    /// a device that is still there had published.
    #[test]
    fn a_removed_device_loses_its_field_and_only_its_field() {
        let t = MockTable::new();
        let mut b = PowerBudget::default();
        b.run(
            &[psu("PSU 1", true, true, Some(1000.0)), psu("PSU 2", true, true, Some(1000.0))],
            &[],
            &[],
            &t,
        );
        b.run(
            &[psu("PSU 1", false, false, None), psu("PSU 2", true, true, Some(1000.0))],
            &[],
            &[],
            &t,
        );
        assert_eq!(t.field(CHASSIS_POWER_KEY, "Supplied Power PSU 1"), None);
        assert_eq!(t.field(CHASSIS_POWER_KEY, "Supplied Power PSU 2").as_deref(), Some("1000.0"));
    }

    #[test]
    fn drawing_more_than_is_supplied_reddens_the_master_led() {
        let t = MockTable::new();
        let mut b = PowerBudget::default();
        b.run(
            &[psu("PSU 1", true, true, Some(100.0))],
            &[drawer("d1", true, Some(500.0))],
            &[],
            &t,
        );
        assert_eq!(b.master_status(), Some(LedColor::Red));
    }

    /// The colour is written once and then only on a change: the LED is a
    /// hardware write, and doing it every three seconds forever is noise on
    /// the i2c bus that a `strace` of this daemon would be full of.
    #[test]
    fn the_led_is_written_on_the_first_pass_and_then_only_on_a_change() {
        let t = MockTable::new();
        let mut b = PowerBudget::default();
        let healthy = || (vec![psu("PSU 1", true, true, Some(1000.0))], vec![drawer("d1", true, Some(50.0))]);

        let (p, d) = healthy();
        b.run(&p, &d, &[], &t);
        assert_eq!(b.master_status(), Some(LedColor::Green), "the first pass always writes");
        let (p, d) = healthy();
        b.run(&p, &d, &[], &t);
        assert_eq!(b.master_status(), None, "nothing changed");

        b.run(&[psu("PSU 1", true, true, Some(10.0))], &[drawer("d1", true, Some(50.0))], &[], &t);
        assert_eq!(b.master_status(), Some(LedColor::Red), "the budget went negative");
    }

    /// Line cards draw power too, and a chassis whose budget left them out
    /// would report itself comfortable while it browned out.
    #[test]
    fn modules_count_against_the_budget_as_fan_drawers_do() {
        let t = MockTable::new();
        let module = |name: &str, presence: bool, w: f64| platform_api::ModuleInfo {
            name: name.to_string(),
            presence,
            maximum_consumed_power: Some(w),
            ..Default::default()
        };
        let mut b = PowerBudget::default();
        b.run(
            &[psu("PSU 1", true, true, Some(1000.0))],
            &[drawer("d1", true, Some(50.0))],
            &[module("LINE-CARD0", true, 400.0), module("LINE-CARD1", false, 400.0)],
            &t,
        );
        assert_eq!(t.field(CHASSIS_POWER_KEY, "Consumed Power LINE-CARD0").as_deref(), Some("400.0"));
        assert_eq!(t.field(CHASSIS_POWER_KEY, "Consumed Power LINE-CARD1"), None,
            "a card that is not there draws nothing and is not listed");
        assert_eq!(t.field(CHASSIS_POWER_KEY, "Total Consumed Power").as_deref(), Some("450.0"));
    }

    /// A refusing table is reported and not fatal: the budget is advisory, and
    /// a daemon that stopped over it would also stop publishing PSU_INFO.
    #[test]
    fn a_refusing_table_does_not_stop_the_budget() {
        let t = MockTable::new();
        t.fail_writes("read-only replica");
        let mut b = PowerBudget::default();
        b.run(
            &[psu("PSU 1", false, false, None)],
            &[drawer("d1", false, None)],
            &[],
            &t,
        );
        assert!(t.is_empty());
        assert_eq!(b.master_status(), Some(LedColor::Red), "the LED is still decided");
    }

    /// Coming back from a negative budget is the other edge, and it is a
    /// NOTICE rather than a warning.
    #[test]
    fn the_master_led_goes_green_again_when_the_budget_recovers() {
        let t = MockTable::new();
        let mut b = PowerBudget::default();
        b.run(&[psu("PSU 1", true, true, Some(100.0))], &[drawer("d1", true, Some(500.0))], &[], &t);
        assert_eq!(b.master_status(), Some(LedColor::Red));
        b.run(&[psu("PSU 1", true, true, Some(1000.0))], &[drawer("d1", true, Some(50.0))], &[], &t);
        assert_eq!(b.master_status(), Some(LedColor::Green));
    }

    /// With no consumers the budget has no verdict to give.  Python leaves
    /// `master_status_good` at its previous value while setting the LED from
    /// the local `False`, so the first pass on such a chassis reds the LED and
    /// logs the *cleared* line.  Reproduced rather than tidied: the two are
    /// observable separately and someone's dashboard reads one of them.
    #[test]
    fn a_budget_with_a_missing_side_still_writes_the_led_on_the_first_pass() {
        let t = MockTable::new();
        let mut b = PowerBudget::default();
        b.run(&[psu("PSU 1", true, true, Some(1000.0))], &[], &[], &t);
        assert_eq!(b.master_status(), Some(LedColor::Red));
        assert!(b.master_status_good, "and the flag it logs from is untouched");
        assert_eq!(b.master_status(), None, "not the first pass any more");
    }
}
