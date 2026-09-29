//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! `config chassis modules shutdown` / `startup`, applied to the hardware.
//!
//! Ports `chassisd:ModuleConfigUpdater` and
//! `chassisd:SmartSwitchModuleConfigUpdater` together with the two tasks that
//! feed them (`chassisd:ConfigManagerTask`,
//! `chassisd:SmartSwitchConfigManagerTask`).  Both watch the same CONFIG_DB
//! table and disagree about what an event means, which is the whole of the
//! difference between them and the reason the decision is a function here.

use platform_api::ModuleType;

/// Which of the two machines this copy of the daemon is running on.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Machine {
    /// A modular chassis: supervisor, line cards, fabric cards.
    Chassis,
    /// A SmartSwitch: DPUs.
    SmartSwitch,
}

impl Machine {
    /// The module names this machine's config table may name.
    fn accepts(self, name: &str) -> bool {
        match self {
            Machine::Chassis => [
                ModuleType::Supervisor,
                ModuleType::LineCard,
                ModuleType::FabricCard,
            ]
            .iter()
            .any(|t| name.starts_with(t.as_str())),
            Machine::SmartSwitch => name.starts_with(ModuleType::Dpu.as_str()),
        }
    }

    fn expected(self) -> String {
        match self {
            Machine::Chassis => format!(
                "{} or {} or {}",
                ModuleType::Supervisor.as_str(),
                ModuleType::LineCard.as_str(),
                ModuleType::FabricCard.as_str()
            ),
            Machine::SmartSwitch => ModuleType::Dpu.as_str().to_string(),
        }
    }
}

/// A CONFIG_DB event, reduced to what this daemon needs of it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Event<'a> {
    /// The row was written.  `admin_status` is whatever it carried.
    Set { admin_status: Option<&'a str> },
    /// The row was removed.
    Del,
}

/// What an event means for the module's admin state: `Some(true)` is up.
///
/// The two machines read the same table oppositely, and it is not a bug in
/// either.  On a chassis the row *is* the shutdown -- `config chassis modules
/// shutdown` creates it and `startup` deletes it, so a SET is down and a DEL is
/// up (`chassisd:ConfigManagerTask.task_worker`).  On a SmartSwitch the row
/// carries an `admin_status` field and is not itself the statement, so a SET is
/// whatever the field says and a DEL is down
/// (`chassisd:SmartSwitchConfigManagerTask.task_worker`).
///
/// Getting this backwards would power a DPU down when an operator brought it
/// up, which is why it is a function with a test rather than a branch inline.
pub fn admin_state_for(machine: Machine, event: Event<'_>) -> bool {
    match (machine, event) {
        (Machine::Chassis, Event::Set { .. }) => false,
        (Machine::Chassis, Event::Del) => true,
        (Machine::SmartSwitch, Event::Set { admin_status }) => admin_status == Some("up"),
        (Machine::SmartSwitch, Event::Del) => false,
    }
}

/// Whether the daemon should act on this key at all.
///
/// A name the machine does not own is a misconfiguration, and applying it would
/// send `set_admin_state` to whatever module index the platform happened to
/// answer with.
pub fn should_apply(machine: Machine, key: &str) -> bool {
    if machine.accepts(key) {
        return true;
    }
    log::error!("Incorrect module-name {key}. Should start with {}", machine.expected());
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    /// On a chassis the row *is* the shutdown.
    #[test]
    fn a_chassis_reads_the_rows_existence_as_the_shutdown() {
        assert!(!admin_state_for(Machine::Chassis, Event::Set { admin_status: None }));
        assert!(admin_state_for(Machine::Chassis, Event::Del));
    }

    /// Even a row that says `up` is a shutdown on a chassis: the field is not
    /// what that daemon reads, and honouring it would make `config chassis
    /// modules shutdown` a no-op on a card somebody had previously started.
    #[test]
    fn a_chassis_ignores_the_admin_status_field() {
        assert!(!admin_state_for(Machine::Chassis, Event::Set { admin_status: Some("up") }));
    }

    /// On a SmartSwitch the field is the statement and the row is not.
    #[test]
    fn a_smartswitch_reads_the_field() {
        assert!(admin_state_for(Machine::SmartSwitch, Event::Set { admin_status: Some("up") }));
        assert!(!admin_state_for(Machine::SmartSwitch, Event::Set { admin_status: Some("down") }));
        assert!(!admin_state_for(Machine::SmartSwitch, Event::Set { admin_status: None }));
    }

    /// And a removed row is down, not up: the two machines are opposite here,
    /// and taking the chassis reading would power a DPU *on* when its
    /// configuration was deleted.
    #[test]
    fn a_removed_row_is_opposite_on_the_two_machines() {
        assert!(admin_state_for(Machine::Chassis, Event::Del));
        assert!(!admin_state_for(Machine::SmartSwitch, Event::Del));
    }

    #[test]
    fn each_machine_owns_its_own_module_names() {
        assert!(should_apply(Machine::Chassis, "LINE-CARD0"));
        assert!(should_apply(Machine::Chassis, "SUPERVISOR0"));
        assert!(should_apply(Machine::Chassis, "FABRIC-CARD0"));
        assert!(!should_apply(Machine::Chassis, "DPU0"));

        assert!(should_apply(Machine::SmartSwitch, "DPU0"));
        assert!(!should_apply(Machine::SmartSwitch, "LINE-CARD0"));
    }

    /// A key that is nobody's is nobody's.
    #[test]
    fn an_unrecognised_key_is_not_applied_anywhere() {
        assert!(!should_apply(Machine::Chassis, "PSU 1"));
        assert!(!should_apply(Machine::SmartSwitch, "PSU 1"));
    }

    /// The message an operator gets for a name this machine does not
    /// administer, which is the only place the daemon says what it *would*
    /// have accepted.  A DPU name typed on a chassis and a line-card name
    /// typed on a SmartSwitch are both silently ignored otherwise: the row
    /// stays in CONFIG_DB and the hardware never hears about it.
    #[test]
    fn a_name_this_machine_does_not_administer_is_named_along_with_what_would_do() {
        let log = pmon_common::logging::capture();

        assert!(!should_apply(Machine::Chassis, "DPU0"));
        assert!(log.logged(log::Level::Error, "Incorrect module-name DPU0"));
        assert!(
            log.contains("SUPERVISOR or LINE-CARD or FABRIC-CARD"),
            "the chassis lists all three types it takes"
        );

        assert!(!should_apply(Machine::SmartSwitch, "LINE-CARD0"));
        assert!(log.logged(log::Level::Error, "Incorrect module-name LINE-CARD0"));
        assert!(log.contains("Should start with DPU"));
    }
}
