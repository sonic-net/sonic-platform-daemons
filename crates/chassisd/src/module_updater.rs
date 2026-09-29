//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! CHASSIS_MODULE_TABLE, the ASIC tables and the midplane table.
//!
//! Ports `chassisd:ModuleUpdater`, the modular-chassis half of the daemon: what
//! cards are in the chassis, which of them are online, which ASICs they carry,
//! and whether the midplane can reach them.

use std::collections::{BTreeMap, BTreeSet};
use std::time::{Duration, SystemTime};

use platform_api::{AsicInfo, ModuleInfo, ModuleStatus, ModuleType};
use pmon_common::db::TableLike;
use pmon_common::fmt;

use crate::names::*;

/// A module that has gone off-line, and whether its chassis app DB rows have
/// been cleaned up yet.
#[derive(Debug, Clone)]
pub struct DownModule {
    pub down_time: SystemTime,
    pub cleaned: bool,
    pub slot: i64,
}

pub struct Tables<'a> {
    pub chassis: &'a dyn TableLike,
    pub module: &'a dyn TableLike,
    pub midplane: &'a dyn TableLike,
    pub entity: &'a dyn TableLike,
    /// CHASSIS_ASIC_TABLE on a line card, CHASSIS_FABRIC_ASIC_TABLE on the
    /// supervisor.  Which one is the supervisor question, asked once.
    pub asic: &'a dyn TableLike,
    pub hostname: &'a dyn TableLike,
    pub module_reboot: &'a dyn TableLike,
    /// CONFIG_DB CHASSIS_MODULE, for the admin state.
    pub config: Option<&'a dyn TableLike>,
}

pub struct ModuleUpdater {
    my_slot: i64,
    supervisor_slot: i64,
    /// How long after an expected reboot the midplane may stay down before it
    /// is worth a line.  From `platform_env.conf`.
    linecard_reboot_timeout: Duration,
    midplane_initialized: bool,
    /// Keyed `<module>|<hostname>`, as Python keys it: the hostname is the
    /// partition of the chassis app DB, so a module that came back under a
    /// different one is a different thing to clean up.
    down_modules: BTreeMap<String, DownModule>,
}

/// Only these three are cards of a modular chassis.  A name that is none of
/// them is a platform bug, and publishing it would put a row in
/// CHASSIS_MODULE_TABLE that `show chassis modules` cannot categorise.
fn is_chassis_card(name: &str) -> bool {
    [ModuleType::Supervisor, ModuleType::LineCard, ModuleType::FabricCard]
        .iter()
        .any(|t| name.starts_with(t.as_str()))
}

/// One field out of a row, or None.
fn field(row: &Option<Vec<(String, String)>>, name: &str) -> Option<String> {
    row.as_ref()?.iter().find(|(k, _)| k == name).map(|(_, v)| v.clone())
}

impl ModuleUpdater {
    pub fn new(
        my_slot: i64,
        supervisor_slot: i64,
        linecard_reboot_timeout: Duration,
        midplane_initialized: bool,
    ) -> Self {
        Self {
            my_slot,
            supervisor_slot,
            linecard_reboot_timeout,
            midplane_initialized,
            down_modules: BTreeMap::new(),
        }
    }

    pub fn is_supervisor(&self) -> bool {
        self.my_slot == self.supervisor_slot
    }

    pub fn down_modules(&self) -> &BTreeMap<String, DownModule> {
        &self.down_modules
    }

    /// How many cards the chassis has, published once per cycle.
    ///
    /// Zero is reported and not published: a chassis that answers zero has not
    /// enumerated its slots yet, and writing 0 would have `show chassis
    /// modules` print an empty chassis rather than wait.
    pub fn publish_module_count(
        &self,
        modules: &[ModuleInfo],
        table: &dyn TableLike,
    ) -> Result<(), String> {
        if modules.is_empty() {
            log::error!("Chassisd has no modules available");
            return Ok(());
        }
        let fvs = [(CHASSIS_INFO_CARD_NUM_FIELD, modules.len().to_string())];
        table
            .set(CHASSIS_INFO_KEY, &fvs)
            .map_err(|e| format!("Failed to publish the module count: {e}"))
    }

    /// One pass over every module.
    pub fn refresh(
        &mut self,
        modules: &[ModuleInfo],
        asics: &[AsicInfo],
        tables: &Tables<'_>,
    ) -> Result<(), String> {
        let mut went_offline: BTreeSet<String> = BTreeSet::new();
        let mut my_module: Option<&ModuleInfo> = None;

        for (index, m) in modules.iter().enumerate() {
            if m.slot == Some(self.my_slot) {
                my_module = Some(m);
            }
            if !is_chassis_card(&m.name) {
                log::error!(
                    "Incorrect module-name {}. Should start with {} or {} or {}",
                    m.name,
                    ModuleType::Supervisor.as_str(),
                    ModuleType::LineCard.as_str(),
                    ModuleType::FabricCard.as_str());
                continue;
            }

            let slot = m.slot.unwrap_or(INVALID_SLOT);
            let status = m.oper_status.unwrap_or(ModuleStatus::Offline);
            let module_asics: Vec<&AsicInfo> =
                asics.iter().filter(|a| a.parent_name == m.name).collect();

            // Read before the write: this is the previous cycle's answer, and
            // the transitions below are the only thing that logs.
            let prev = tables
                .module
                .get(&m.name)?
                .and_then(|row| row.into_iter().find(|(k, _)| k == OPERSTATUS_FIELD))
                .map(|(_, v)| v)
                .unwrap_or_else(|| ModuleStatus::Empty.as_str().to_string());

            let presence = fmt::bool(m.presence);
            let serial = fmt::opt_str(&m.serial);
            let model = fmt::opt_str(&m.model);
            let replaceable = fmt::bool(m.is_replaceable);
            let fvs = [
                (DESC_FIELD, fmt::opt_str(&m.description)),
                (SLOT_FIELD, slot.to_string()),
                (OPERSTATUS_FIELD, status.as_str().to_string()),
                (NUM_ASICS_FIELD, module_asics.len().to_string()),
                (SERIAL_FIELD, serial.clone()),
                (PRESENCE_FIELD, presence.clone()),
                (REPLACEABLE_FIELD, replaceable.clone()),
                (MODEL_FIELD, model.clone()),
            ];
            tables
                .module
                .set(&m.name, &fvs)
                .map_err(|e| format!("Failed to update {} to DB: {e}", m.name))?;

            if m.presence {
                let entity = [
                    ("position_in_parent", index.to_string()),
                    ("parent_name", CHASSIS_PARENT.to_string()),
                    ("serial", serial),
                    ("model", model),
                    ("is_replaceable", replaceable),
                ];
                tables
                    .entity
                    .set(&m.name, &entity)
                    .map_err(|e| format!("Failed to update the entity row for {}: {e}", m.name))?;
            } else if tables.entity.get(&m.name)?.is_some() {
                // An empty slot is not an entity.  Leaving the row would have
                // the SNMP entity MIB report a card that is not there.
                tables
                    .entity
                    .del(&m.name)
                    .map_err(|e| format!("Failed to drop the entity row for {}: {e}", m.name))?;
            }

            // The hostname partitions the chassis app DB, so a module that
            // came back under a different one is a different thing to clean up.
            let hostname = field(&tables.hostname.get(&m.name)?, HOSTNAME_FIELD).unwrap_or_default();
            let down_key = format!("{}|{}", m.name, hostname);

            if status != ModuleStatus::Online {
                if prev == ModuleStatus::Online.as_str() {
                    went_offline.insert(m.name.clone());
                    // Recorded on the transition only, so a module that has
                    // been out for a week does not restart its 30-minute
                    // cleanup clock on every cycle.
                    self.down_modules.entry(down_key).or_insert_with(|| {
                        log::warn!("Module {} (Slot {slot}) went off-line!", m.name);
                        DownModule { down_time: SystemTime::now(), cleaned: false, slot }
                    });
                }
                continue;
            }

            if self.down_modules.remove(&down_key).is_some() {
                pmon_common::notice!("Module {} (Slot {slot}) recovered on-line!", m.name);
            } else if prev != ModuleStatus::Online.as_str() {
                pmon_common::notice!("Module {} (Slot {slot}) is on-line!", m.name);
            }

            // A card an operator has configured down is online but not in
            // service; publishing its ASICs would have the fabric try to use
            // them (the `module_cfg_status != 'down'` check in
            // `chassisd:ModuleUpdater.module_db_update`).
            if self.admin_status(&m.name, tables) != "down" {
                self.publish_asics(&m.name, &module_asics, tables.asic)?;
            }
        }

        if !self.is_supervisor() {
            self.publish_hostname(my_module, asics, tables)?;
        }
        if !went_offline.is_empty() {
            remove_asics_of(&went_offline, tables.asic)?;
        }
        Ok(())
    }

    /// CONFIG_DB's `admin_status` for one module; `up` when unset.
    ///
    /// Defaulting to up is what makes a chassis work before anyone configures
    /// it: the absence of a row is not a decision to keep the card out.
    fn admin_status(&self, name: &str, tables: &Tables<'_>) -> String {
        tables
            .config
            .and_then(|c| field(&c.get(name).ok().flatten(), CHASSIS_MODULE_ADMIN_STATUS))
            .unwrap_or_else(|| "up".to_string())
    }

    fn publish_asics(
        &self,
        module: &str,
        asics: &[&AsicInfo],
        table: &dyn TableLike,
    ) -> Result<(), String> {
        for (id_in_module, a) in asics.iter().enumerate() {
            // On a line card the key is scoped by module, because every line
            // card writes into the one shared CHASSIS_STATE_DB and `asic0`
            // means a different ASIC on each of them.
            let key = if self.is_supervisor() {
                format!("{ASIC_PREFIX}{}", a.asic_id)
            } else {
                format!("{module}|{ASIC_PREFIX}{}", a.asic_id)
            };
            let fvs = [
                (ASIC_PCI_ADDRESS_FIELD, fmt::opt_str(&a.pci_address)),
                (NAME_FIELD, module.to_string()),
                (ASIC_ID_IN_MODULE_FIELD, id_in_module.to_string()),
            ];
            table
                .set(&key, &fvs)
                .map_err(|e| format!("Failed to update {key} to the ASIC table: {e}"))?;
        }
        Ok(())
    }

    /// A line card publishes its own hostname, which is the key the chassis app
    /// DB is partitioned by and therefore what the supervisor cleans up under.
    fn publish_hostname(
        &self,
        my_module: Option<&ModuleInfo>,
        asics: &[AsicInfo],
        tables: &Tables<'_>,
    ) -> Result<(), String> {
        let num_asics = my_module
            .map(|m| asics.iter().filter(|a| a.parent_name == m.name).count())
            .unwrap_or(0);
        // `LINE-CARD<slot-1>`: the table is zero-based where the slots are not.
        let key = format!("{}{}", ModuleType::LineCard.as_str(), self.my_slot - 1);
        let hostname = hostname();
        let fvs = [
            (SLOT_FIELD, self.my_slot.to_string()),
            (HOSTNAME_FIELD, hostname),
            (NUM_ASICS_FIELD, num_asics.to_string()),
        ];
        tables
            .hostname
            .set(&key, &fvs)
            .map_err(|e| format!("Failed to publish the line card hostname: {e}"))
    }

    // ── midplane ──────────────────────────────────────────────────────────────

    /// Whether the midplane can reach the modules this one cares about.
    ///
    /// A supervisor watches every card but itself; a line card watches only the
    /// supervisor.  Fabric cards are skipped on both -- they have no midplane
    /// address to reach.
    pub fn check_midplane(
        &mut self,
        modules: &[ModuleInfo],
        tables: &Tables<'_>,
    ) -> Result<(), String> {
        if !self.midplane_initialized {
            return Ok(());
        }
        for (index, m) in modules.iter().enumerate() {
            if m.r#type == Some(ModuleType::FabricCard) {
                continue;
            }
            let slot = m.slot.unwrap_or(INVALID_SLOT);
            let mine = slot == self.supervisor_slot;
            if self.is_supervisor() == mine {
                // Supervisor skips itself; a line card skips everything that
                // is not the supervisor.  The two conditions coincide.
                continue;
            }

            let key = if m.name.is_empty() {
                format!("MODULE {index}")
            } else {
                m.name.clone()
            };
            let ip = m.midplane_ip.clone().unwrap_or_else(|| INVALID_IP.to_string());
            let reachable = m.is_midplane_reachable.unwrap_or(false);
            let was = field(&tables.midplane.get(&key)?, MIDPLANE_ACCESS_FIELD)
                .unwrap_or_else(|| fmt::bool(false));

            match (reachable, was == fmt::bool(true)) {
                (false, true) => {
                    // An operator-initiated reboot loses the midplane too.
                    // Saying which it was is the difference between a line
                    // somebody investigates and one they learn to ignore.
                    if self.reboot_expected(&key, tables)? {
                        self.set_reboot_time(&key, tables)?;
                        log::warn!(
                            "Expected: Module {key} (Slot {slot}) lost midplane connectivity");
                    } else {
                        log::warn!(
                            "Unexpected: Module {key} (Slot {slot}) lost midplane connectivity");
                    }
                }
                (true, false) => {
                    pmon_common::notice!(
                        "Module {key} (Slot {slot}) midplane connectivity is up");
                    if tables.module_reboot.get(&key)?.is_some() {
                        tables.module_reboot.del(&key).map_err(|e| {
                            format!("Failed to drop the reboot row for {key}: {e}")
                        })?;
                    }
                }
                (false, false) => {
                    // Still down.  The one line worth printing is that the
                    // expected reboot has taken longer than it should.
                    if self.reboot_deadline_passed(&key, tables)? {
                        log::warn!(
                            "Unexpected: Module {key} (Slot {slot}) midplane connectivity is \
                             not restored in {} seconds",
                            self.linecard_reboot_timeout.as_secs());
                    }
                }
                (true, true) => {}
            }

            let fvs = [
                (MIDPLANE_IP_FIELD, ip),
                (MIDPLANE_ACCESS_FIELD, fmt::bool(reachable)),
            ];
            tables
                .midplane
                .set(&key, &fvs)
                .map_err(|e| format!("Failed to update the midplane table for {key}: {e}"))?;
        }
        Ok(())
    }

    fn reboot_expected(&self, key: &str, tables: &Tables<'_>) -> Result<bool, String> {
        Ok(field(&tables.module_reboot.get(key)?, REBOOT_REBOOT_FIELD).as_deref()
            == Some(REBOOT_EXPECTED))
    }

    fn set_reboot_time(&self, key: &str, tables: &Tables<'_>) -> Result<(), String> {
        let now = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);
        let fvs = [(REBOOT_TIMESTAMP_FIELD, fmt::float(now))];
        tables
            .module_reboot
            .set(key, &fvs)
            .map_err(|e| format!("Failed to record the reboot time for {key}: {e}"))
    }

    /// True once, when an expected reboot has overrun.
    ///
    /// The row is deleted on the way out, which is what makes it once: the
    /// warning is about the deadline passing, not about the module still being
    /// down, and the latter would print every ten seconds forever.
    fn reboot_deadline_passed(&self, key: &str, tables: &Tables<'_>) -> Result<bool, String> {
        let Some(ts) = field(&tables.module_reboot.get(key)?, REBOOT_TIMESTAMP_FIELD) else {
            return Ok(false);
        };
        let Ok(ts) = ts.trim().parse::<f64>() else { return Ok(false) };
        let now = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);
        if now - ts >= self.linecard_reboot_timeout.as_secs_f64() {
            // Propagated, as the rest of this pass is: Python deletes it bare
            // (`chassisd:ModuleUpdater.is_module_reboot_system_up_expired`,
            // called unguarded from
            // `chassisd:ModuleUpdater.check_midplane_reachability`).
            // Swallowing it would also break the "once" above -- a row that
            // stays would report the overrun again every cycle.
            tables
                .module_reboot
                .del(key)
                .map_err(|e| format!("Failed to drop the reboot row for {key}: {e}"))?;
            return Ok(true);
        }
        Ok(false)
    }

    /// Mark a module cleaned, so the 30-minute cleanup runs once.
    pub fn mark_cleaned(&mut self, key: &str) {
        if let Some(m) = self.down_modules.get_mut(key) {
            m.cleaned = true;
        }
    }

    /// Remove everything this updater published.
    pub fn clear(&self, modules: &[ModuleInfo], tables: &Tables<'_>) {
        for m in modules {
            let _ = tables.module.del(&m.name);
            // On the way out; a read that fails here costs nothing.
            if tables.midplane.get(&m.name).ok().flatten().is_some() {
                let _ = tables.midplane.del(&m.name);
            }
            if tables.entity.get(&m.name).ok().flatten().is_some() {
                let _ = tables.entity.del(&m.name);
            }
        }
        let _ = tables.chassis.del(CHASSIS_INFO_KEY);
        // Only a line card clears the ASIC table: on the supervisor it holds
        // the fabric cards' ASICs, which outlive this daemon restarting.
        if !self.is_supervisor() {
            for key in tables.asic.get_keys().unwrap_or_default() {
                let _ = tables.asic.del(&key);
            }
        }
    }
}

/// Drop the ASIC rows of modules that have just gone off-line.
fn remove_asics_of(offline: &BTreeSet<String>, table: &dyn TableLike) -> Result<(), String> {
    for key in table.get_keys()? {
        let owner = table
            .get(&key)?
            .and_then(|row| row.into_iter().find(|(k, _)| k == NAME_FIELD))
            .map(|(_, v)| v);
        if owner.is_some_and(|o| offline.contains(&o)) {
            table
                .del(&key)
                .map_err(|e| format!("Failed to drop the ASIC row {key}: {e}"))?;
        }
    }
    Ok(())
}

/// The switch's hostname, as `device_info.get_hostname` answers it.
fn hostname() -> String {
    std::fs::read_to_string("/etc/hostname")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "None".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use pmon_common::db::MockTable;

    struct Db {
        chassis: MockTable,
        module: MockTable,
        midplane: MockTable,
        entity: MockTable,
        asic: MockTable,
        hostname: MockTable,
        module_reboot: MockTable,
        config: MockTable,
    }

    impl Db {
        fn new() -> Self {
            Self {
                chassis: MockTable::new(),
                module: MockTable::new(),
                midplane: MockTable::new(),
                entity: MockTable::new(),
                asic: MockTable::new(),
                hostname: MockTable::new(),
                module_reboot: MockTable::new(),
                config: MockTable::new(),
            }
        }
        fn tables(&self) -> Tables<'_> {
            Tables {
                chassis: &self.chassis,
                module: &self.module,
                midplane: &self.midplane,
                entity: &self.entity,
                asic: &self.asic,
                hostname: &self.hostname,
                module_reboot: &self.module_reboot,
                config: Some(&self.config),
            }
        }
    }

    fn module(name: &str, slot: i64, status: ModuleStatus) -> ModuleInfo {
        ModuleInfo {
            name: name.to_string(),
            parent_name: CHASSIS_PARENT.to_string(),
            position_in_parent: Some(1),
            presence: true,
            status: Some(true),
            is_replaceable: true,
            model: Some("LC-36".to_string()),
            serial: Some("SN1".to_string()),
            description: Some("Line Card".to_string()),
            slot: Some(slot),
            r#type: Some(ModuleType::LineCard),
            oper_status: Some(status),
            base_mac: None,
            dpu_id: None,
            maximum_consumed_power: None,
            midplane_ip: Some("10.0.0.2".to_string()),
            is_midplane_reachable: Some(true),
            state_transition: None,
        }
    }

    fn asic(module: &str, id: &str, pci: &str) -> AsicInfo {
        AsicInfo {
            parent_name: module.to_string(),
            asic_id: id.to_string(),
            pci_address: Some(pci.to_string()),
        }
    }

    fn supervisor() -> ModuleUpdater {
        ModuleUpdater::new(16, 16, Duration::from_secs(180), true)
    }

    fn line_card() -> ModuleUpdater {
        ModuleUpdater::new(1, 16, Duration::from_secs(180), true)
    }

    #[test]
    fn a_card_is_published_with_its_asic_count() {
        let db = Db::new();
        supervisor().refresh(
            &[module("LINE-CARD0", 1, ModuleStatus::Online)],
            &[asic("LINE-CARD0", "0", "0000:03:00.0"), asic("LINE-CARD0", "1", "0000:04:00.0")],
            &db.tables(),
        ).unwrap();
        assert_eq!(db.module.field("LINE-CARD0", "num_asics").as_deref(), Some("2"));
        assert_eq!(db.module.field("LINE-CARD0", "oper_status").as_deref(), Some("Online"));
        assert_eq!(db.module.field("LINE-CARD0", "slot").as_deref(), Some("1"));
    }

    /// A name outside the three card types is a platform bug.  Publishing it
    /// would put a row in the table `show chassis modules` cannot categorise.
    #[test]
    fn a_module_that_is_not_a_chassis_card_is_not_published() {
        let db = Db::new();
        let mut odd = module("DPU0", 1, ModuleStatus::Online);
        odd.r#type = Some(ModuleType::Dpu);
        supervisor().refresh(&[odd], &[], &db.tables()).unwrap();
        assert!(db.module.is_empty());
    }

    /// An empty slot is not a physical entity.  Leaving the row would have the
    /// entity MIB report a card that has been pulled.
    #[test]
    fn pulling_a_card_removes_its_entity_row() {
        let db = Db::new();
        let mut u = supervisor();
        u.refresh(&[module("LINE-CARD0", 1, ModuleStatus::Online)], &[], &db.tables()).unwrap();
        assert!(db.entity.field("LINE-CARD0", "parent_name").is_some());

        let mut gone = module("LINE-CARD0", 1, ModuleStatus::Empty);
        gone.presence = false;
        u.refresh(&[gone], &[], &db.tables()).unwrap();
        assert!(db.entity.is_empty());
        assert_eq!(db.module.field("LINE-CARD0", "presence").as_deref(), Some("False"));
    }

    /// The supervisor's ASIC keys are bare; a line card's are scoped by module,
    /// because every line card writes into the one shared CHASSIS_STATE_DB and
    /// `asic0` means a different ASIC on each of them.
    #[test]
    fn a_line_card_scopes_its_asic_keys_by_module() {
        let db = Db::new();
        line_card().refresh(
            &[module("LINE-CARD0", 1, ModuleStatus::Online)],
            &[asic("LINE-CARD0", "0", "0000:03:00.0")],
            &db.tables(),
        ).unwrap();
        assert_eq!(db.asic.keys(), vec!["LINE-CARD0|asic0".to_string()]);

        let db2 = Db::new();
        supervisor().refresh(
            &[module("FABRIC-CARD0", 2, ModuleStatus::Online)],
            &[asic("FABRIC-CARD0", "0", "0000:03:00.0")],
            &db2.tables(),
        ).unwrap();
        assert_eq!(db2.asic.keys(), vec!["asic0".to_string()]);
    }

    /// A card configured down is online but not in service.  Publishing its
    /// ASICs would have the fabric try to use them.
    #[test]
    fn a_card_configured_down_publishes_no_asics() {
        let db = Db::new();
        db.config
            .set("LINE-CARD0", &[(CHASSIS_MODULE_ADMIN_STATUS, "down".to_string())])
            .unwrap();
        supervisor().refresh(
            &[module("LINE-CARD0", 1, ModuleStatus::Online)],
            &[asic("LINE-CARD0", "0", "0000:03:00.0")],
            &db.tables(),
        ).unwrap();
        assert!(db.asic.is_empty());
        assert_eq!(db.module.field("LINE-CARD0", "oper_status").as_deref(), Some("Online"),
            "the card is still reported, it is just not carrying traffic");
    }

    /// The absence of a CONFIG_DB row is not a decision to keep the card out.
    #[test]
    fn an_unconfigured_card_is_treated_as_up() {
        let db = Db::new();
        supervisor().refresh(
            &[module("LINE-CARD0", 1, ModuleStatus::Online)],
            &[asic("LINE-CARD0", "0", "0000:03:00.0")],
            &db.tables(),
        ).unwrap();
        assert_eq!(db.asic.len(), 1);
    }

    /// Going off-line drops the card's ASIC rows, so nothing routes to a card
    /// that is not there.
    #[test]
    fn a_card_going_offline_takes_its_asic_rows_with_it() {
        let db = Db::new();
        let mut u = supervisor();
        u.refresh(
            &[module("LINE-CARD0", 1, ModuleStatus::Online)],
            &[asic("LINE-CARD0", "0", "0000:03:00.0")],
            &db.tables(),
        ).unwrap();
        assert_eq!(db.asic.len(), 1);
        u.refresh(&[module("LINE-CARD0", 1, ModuleStatus::Offline)], &[], &db.tables()).unwrap();
        assert!(db.asic.is_empty());
        assert_eq!(u.down_modules().len(), 1);
    }

    /// Recorded on the transition only.  Restarting the clock every cycle
    /// would mean the 30-minute chassis app DB cleanup never fires.
    #[test]
    fn the_down_clock_starts_once_and_does_not_restart() {
        let db = Db::new();
        let mut u = supervisor();
        u.refresh(&[module("LINE-CARD0", 1, ModuleStatus::Online)], &[], &db.tables()).unwrap();
        u.refresh(&[module("LINE-CARD0", 1, ModuleStatus::Offline)], &[], &db.tables()).unwrap();
        let first = u.down_modules()["LINE-CARD0|"].down_time;
        u.refresh(&[module("LINE-CARD0", 1, ModuleStatus::Offline)], &[], &db.tables()).unwrap();
        assert_eq!(u.down_modules()["LINE-CARD0|"].down_time, first);
        assert_eq!(u.down_modules().len(), 1);
    }

    #[test]
    fn coming_back_clears_the_down_record() {
        let db = Db::new();
        let mut u = supervisor();
        u.refresh(&[module("LINE-CARD0", 1, ModuleStatus::Online)], &[], &db.tables()).unwrap();
        u.refresh(&[module("LINE-CARD0", 1, ModuleStatus::Offline)], &[], &db.tables()).unwrap();
        assert_eq!(u.down_modules().len(), 1);
        u.refresh(&[module("LINE-CARD0", 1, ModuleStatus::Online)], &[], &db.tables()).unwrap();
        assert!(u.down_modules().is_empty());
    }

    /// A supervisor watches every card but itself.
    #[test]
    fn a_supervisor_watches_the_cards_and_not_itself() {
        let db = Db::new();
        let mut sup = module("SUPERVISOR0", 16, ModuleStatus::Online);
        sup.r#type = Some(ModuleType::Supervisor);
        supervisor().check_midplane(
            &[sup, module("LINE-CARD0", 1, ModuleStatus::Online)],
            &db.tables(),
        ).unwrap();
        assert_eq!(db.midplane.keys(), vec!["LINE-CARD0".to_string()]);
    }

    /// A line card watches only the supervisor: it has no path to its peers.
    #[test]
    fn a_line_card_watches_only_the_supervisor() {
        let db = Db::new();
        let mut sup = module("SUPERVISOR0", 16, ModuleStatus::Online);
        sup.r#type = Some(ModuleType::Supervisor);
        line_card().check_midplane(
            &[sup, module("LINE-CARD1", 2, ModuleStatus::Online)],
            &db.tables(),
        ).unwrap();
        assert_eq!(db.midplane.keys(), vec!["SUPERVISOR0".to_string()]);
    }

    /// Fabric cards have no midplane address to reach.
    #[test]
    fn fabric_cards_are_skipped() {
        let db = Db::new();
        let mut fab = module("FABRIC-CARD0", 2, ModuleStatus::Online);
        fab.r#type = Some(ModuleType::FabricCard);
        supervisor().check_midplane(&[fab], &db.tables()).unwrap();
        assert!(db.midplane.is_empty());
    }

    /// An operator-initiated reboot loses the midplane too.  Saying which it
    /// was is the difference between a line somebody investigates and one they
    /// learn to ignore -- and the expected one starts a deadline.
    #[test]
    fn an_expected_reboot_records_when_it_started() {
        let db = Db::new();
        let mut u = supervisor();
        u.check_midplane(&[module("LINE-CARD0", 1, ModuleStatus::Online)], &db.tables()).unwrap();
        db.module_reboot
            .set("LINE-CARD0", &[(REBOOT_REBOOT_FIELD, REBOOT_EXPECTED.to_string())])
            .unwrap();

        let mut down = module("LINE-CARD0", 1, ModuleStatus::Online);
        down.is_midplane_reachable = Some(false);
        u.check_midplane(&[down], &db.tables()).unwrap();
        assert!(db.module_reboot.field("LINE-CARD0", REBOOT_TIMESTAMP_FIELD).is_some());
        assert_eq!(db.midplane.field("LINE-CARD0", MIDPLANE_ACCESS_FIELD).as_deref(), Some("False"));
    }

    /// Coming back clears the reboot record, so the next loss is judged afresh.
    #[test]
    fn midplane_coming_back_clears_the_reboot_record() {
        let db = Db::new();
        let mut u = supervisor();
        db.midplane
            .set("LINE-CARD0", &[(MIDPLANE_ACCESS_FIELD, "False".to_string())])
            .unwrap();
        db.module_reboot
            .set("LINE-CARD0", &[(REBOOT_REBOOT_FIELD, REBOOT_EXPECTED.to_string())])
            .unwrap();
        u.check_midplane(&[module("LINE-CARD0", 1, ModuleStatus::Online)], &db.tables()).unwrap();
        assert!(db.module_reboot.is_empty());
    }

    /// The overrun warning fires once: the row is deleted on the way out.
    /// Reporting "still down" every ten seconds is what it would otherwise be.
    #[test]
    fn the_overrun_warning_fires_once() {
        let db = Db::new();
        let u = ModuleUpdater::new(16, 16, Duration::from_secs(0), true);
        db.module_reboot
            .set("LINE-CARD0", &[(REBOOT_TIMESTAMP_FIELD, "1.0".to_string())])
            .unwrap();
        let tables = db.tables();
        assert!(u.reboot_deadline_passed("LINE-CARD0", &tables).unwrap());
        assert!(!u.reboot_deadline_passed("LINE-CARD0", &tables).unwrap());
    }

    /// Without a midplane there is nothing to check, and a table of `False`
    /// would read as a chassis whose every card is unreachable.
    #[test]
    fn an_uninitialised_midplane_publishes_nothing() {
        let db = Db::new();
        ModuleUpdater::new(16, 16, Duration::from_secs(180), false)
            .check_midplane(&[module("LINE-CARD0", 1, ModuleStatus::Online)], &db.tables()).unwrap();
        assert!(db.midplane.is_empty());
    }

    #[test]
    fn the_module_count_is_published_but_never_as_zero() {
        let db = Db::new();
        let u = supervisor();
        u.publish_module_count(&[], &db.chassis).unwrap();
        assert!(db.chassis.is_empty(), "a chassis that has not enumerated is not an empty one");
        u.publish_module_count(&[module("LINE-CARD0", 1, ModuleStatus::Online)], &db.chassis).unwrap();
        assert_eq!(db.chassis.field(CHASSIS_INFO_KEY, "module_num").as_deref(), Some("1"));
    }

    /// A refusing table costs that card's row and nothing else: the pass goes
    /// on to the next card rather than abandoning the chassis.
    #[test]
    fn a_table_that_refuses_a_write_stops_the_pass() {
        let db = Db::new();
        db.module.fail_writes("read-only replica");
        let e = supervisor()
            .refresh(
                &[module("LINE-CARD0", 1, ModuleStatus::Online),
                  module("LINE-CARD1", 2, ModuleStatus::Online)],
                &[],
                &db.tables(),
            )
            .expect_err("a table that will not take a write has to be reported");
        assert!(e.contains("LINE-CARD0"), "it stops on the first card, not the last: {e}");
        assert!(db.module.is_empty());
        assert!(db.entity.is_empty(), "the entity row is not written for a card that failed");
    }

    /// A card the platform has no name for still gets a midplane row, under
    /// the position it was found at -- an unnamed card is still reachable or
    /// not, and that is what the row says.
    #[test]
    fn an_unnamed_module_is_keyed_by_its_position() {
        let db = Db::new();
        let mut anon = module("", 1, ModuleStatus::Online);
        anon.name = String::new();
        supervisor().check_midplane(&[anon], &db.tables()).unwrap();
        assert_eq!(db.midplane.keys(), vec!["MODULE 0".to_string()]);
    }

    /// A card that reports no slot is published with the sentinel rather than
    /// omitted: `show chassis modules` lists it, and a missing row would read
    /// as a slot that is empty.
    #[test]
    fn a_card_with_no_slot_is_still_published() {
        let db = Db::new();
        let mut no_slot = module("LINE-CARD0", 1, ModuleStatus::Online);
        no_slot.slot = None;
        supervisor().refresh(&[no_slot], &[], &db.tables()).unwrap();
        assert_eq!(db.module.field("LINE-CARD0", "slot").as_deref(), Some("-1"));
    }

    /// The hostname a line card publishes is the key the chassis app DB is
    /// partitioned by, and the table is zero-based where the slots are not.
    #[test]
    fn a_line_card_publishes_its_hostname_under_a_zero_based_key() {
        let db = Db::new();
        line_card().refresh(&[module("LINE-CARD0", 1, ModuleStatus::Online)], &[], &db.tables()).unwrap();
        assert_eq!(db.hostname.field("LINE-CARD0", "slot").as_deref(), Some("1"));
        assert!(db.hostname.field("LINE-CARD0", "num_asics").is_some());
    }

    /// A reboot record with an unreadable timestamp is not a deadline: the
    /// alternative is warning about an overrun that never started.
    #[test]
    fn an_unparseable_reboot_timestamp_is_not_a_deadline() {
        let db = Db::new();
        let u = ModuleUpdater::new(16, 16, Duration::from_secs(0), true);
        db.module_reboot.set("LINE-CARD0", &[(REBOOT_TIMESTAMP_FIELD, "soon".to_string())]).unwrap();
        assert!(!u.reboot_deadline_passed("LINE-CARD0", &db.tables()).unwrap());
        db.module_reboot.del("LINE-CARD0").unwrap();
        assert!(!u.reboot_deadline_passed("LINE-CARD0", &db.tables()).unwrap());
    }

    /// The midplane's four transitions, each said once.  "Expected" and
    /// "Unexpected" are the difference between a line somebody investigates
    /// and one they learn to ignore.
    #[test]
    fn the_midplane_reports_each_transition_once() {
        let db = Db::new();
        let mut u = supervisor();
        let with = |up: bool| {
            let mut m = module("LINE-CARD0", 1, ModuleStatus::Online);
            m.is_midplane_reachable = Some(up);
            m
        };
        u.check_midplane(&[with(true)], &db.tables()).unwrap();
        assert_eq!(db.midplane.field("LINE-CARD0", MIDPLANE_ACCESS_FIELD).as_deref(), Some("True"));

        // Unexpected loss, then still lost, then back.
        u.check_midplane(&[with(false)], &db.tables()).unwrap();
        assert_eq!(db.midplane.field("LINE-CARD0", MIDPLANE_ACCESS_FIELD).as_deref(), Some("False"));
        u.check_midplane(&[with(false)], &db.tables()).unwrap();
        u.check_midplane(&[with(true)], &db.tables()).unwrap();
        assert_eq!(db.midplane.field("LINE-CARD0", MIDPLANE_ACCESS_FIELD).as_deref(), Some("True"));
    }

    /// An expected reboot that overruns is reported once, and then the module
    /// stays down quietly: "still down" every ten seconds is noise.
    #[test]
    fn an_expected_reboot_that_overruns_is_reported_once() {
        let db = Db::new();
        let mut u = ModuleUpdater::new(16, 16, Duration::from_secs(0), true);
        let mut down = module("LINE-CARD0", 1, ModuleStatus::Online);
        down.is_midplane_reachable = Some(false);

        db.midplane.set("LINE-CARD0", &[(MIDPLANE_ACCESS_FIELD, "True".to_string())]).unwrap();
        db.module_reboot
            .set("LINE-CARD0", &[(REBOOT_REBOOT_FIELD, REBOOT_EXPECTED.to_string())])
            .unwrap();
        u.check_midplane(&[down.clone()], &db.tables()).unwrap();
        // Still down: the deadline has passed, so this is the one warning.
        u.check_midplane(&[down.clone()], &db.tables()).unwrap();
        assert!(db.module_reboot.is_empty(), "the record is consumed by the warning");
        u.check_midplane(&[down], &db.tables()).unwrap();
    }

    /// A refusing midplane table costs that row and not the pass.
    #[test]
    fn a_refusing_midplane_table_stops_the_pass() {
        let db = Db::new();
        db.midplane.fail_writes("read-only replica");
        supervisor()
            .check_midplane(&[module("LINE-CARD0", 1, ModuleStatus::Online)], &db.tables())
            .expect_err("a midplane table that will not take a write has to be reported");
        assert!(db.midplane.is_empty());
    }

    /// Nor does a refusing ASIC or hostname table.
    #[test]
    fn a_refusing_asic_table_stops_the_pass() {
        let db = Db::new();
        db.asic.fail_writes("read-only replica");
        db.hostname.fail_writes("read-only replica");
        let e = line_card()
            .refresh(
                &[module("LINE-CARD0", 1, ModuleStatus::Online)],
                &[asic("LINE-CARD0", "0", "0000:03:00.0")],
                &db.tables(),
            )
            .expect_err("an ASIC table that will not take a write has to be reported");
        assert!(e.contains("ASIC"), "the error says which table: {e}");
        assert!(db.asic.is_empty());
        // The module row went in before the ASIC row was tried, which is the
        // order Python writes them in too (`module_table.set` then
        // `asic_table.set` in `chassisd:ModuleUpdater.module_db_update`).
        assert_eq!(db.module.field("LINE-CARD0", "oper_status").as_deref(), Some("Online"));
    }

    /// A chassis that has not enumerated its slots is not an empty chassis,
    /// and a refusing table is reported rather than crashing the pass.
    #[test]
    fn a_refusing_chassis_table_is_reported() {
        let db = Db::new();
        db.chassis.fail_writes("read-only replica");
        supervisor()
            .publish_module_count(&[module("LINE-CARD0", 1, ModuleStatus::Online)], &db.chassis)
            .expect_err("a chassis table that will not take the count has to be reported");
        assert!(db.chassis.is_empty());
    }

    /// The 30-minute cleanup runs once; the flag is what stops the supervisor
    /// re-running a LAG-id return for as long as the card stays out.
    #[test]
    fn marking_a_module_cleaned_takes_it_out_of_the_queue() {
        let db = Db::new();
        let mut u = supervisor();
        u.refresh(&[module("LINE-CARD0", 1, ModuleStatus::Online)], &[], &db.tables()).unwrap();
        u.refresh(&[module("LINE-CARD0", 1, ModuleStatus::Offline)], &[], &db.tables()).unwrap();
        assert!(!u.down_modules()["LINE-CARD0|"].cleaned);
        u.mark_cleaned("LINE-CARD0|");
        assert!(u.down_modules()["LINE-CARD0|"].cleaned);
        u.mark_cleaned("NO-SUCH-MODULE|", );
    }

    // ── What a lost STATE_DB does to each write of the pass ──────────────────
    //
    // Every write below is one Python makes unguarded, so each has to end the
    // pass rather than be logged and skipped: `DBConnector` does not
    // reconnect, and a pass that carried on would publish into nothing.

    #[test]
    fn a_refusing_entity_table_stops_the_pass() {
        let db = Db::new();
        db.entity.fail_writes("redis is gone");
        let e = supervisor()
            .refresh(&[module("LINE-CARD0", 1, ModuleStatus::Online)], &[], &db.tables())
            .expect_err("an entity row that will not write has to be reported");
        assert!(e.contains("entity row for LINE-CARD0"), "{e}");
    }

    #[test]
    fn an_entity_row_that_will_not_delete_stops_the_pass() {
        let db = Db::new();
        let mut u = supervisor();
        u.refresh(&[module("LINE-CARD0", 1, ModuleStatus::Online)], &[], &db.tables()).unwrap();

        let mut gone = module("LINE-CARD0", 1, ModuleStatus::Empty);
        gone.presence = false;
        db.entity.fail_writes("redis is gone");
        let e = u
            .refresh(&[gone], &[], &db.tables())
            .expect_err("an entity row that will not go has to be reported");
        assert!(e.contains("drop the entity row"), "{e}");
        assert!(!db.entity.is_empty(), "and the row it could not drop is still there");
    }

    #[test]
    fn a_refusing_hostname_table_stops_the_pass() {
        let db = Db::new();
        db.hostname.fail_writes("redis is gone");
        let e = line_card()
            .refresh(&[module("LINE-CARD0", 1, ModuleStatus::Online)], &[], &db.tables())
            .expect_err("a hostname that will not publish has to be reported");
        assert!(e.contains("hostname"), "{e}");
    }

    #[test]
    fn an_asic_row_that_will_not_delete_stops_the_pass() {
        let db = Db::new();
        let mut u = supervisor();
        u.refresh(
            &[module("LINE-CARD0", 1, ModuleStatus::Online)],
            &[asic("LINE-CARD0", "0", "0000:03:00.0")],
            &db.tables(),
        )
        .unwrap();

        db.asic.fail_writes("redis is gone");
        let e = u
            .refresh(&[module("LINE-CARD0", 1, ModuleStatus::Offline)], &[], &db.tables())
            .expect_err("an ASIC row that will not go has to be reported");
        assert!(e.contains("drop the ASIC row"), "{e}");
        assert_eq!(db.asic.len(), 1, "and the row it could not drop is still there");
    }

    #[test]
    fn a_reboot_time_that_will_not_record_stops_the_pass() {
        let db = Db::new();
        let mut u = supervisor();
        u.check_midplane(&[module("LINE-CARD0", 1, ModuleStatus::Online)], &db.tables()).unwrap();
        db.module_reboot
            .set("LINE-CARD0", &[(REBOOT_REBOOT_FIELD, REBOOT_EXPECTED.to_string())])
            .unwrap();

        let mut down = module("LINE-CARD0", 1, ModuleStatus::Online);
        down.is_midplane_reachable = Some(false);
        db.module_reboot.fail_writes("redis is gone");
        let e = u
            .check_midplane(&[down], &db.tables())
            .expect_err("a reboot time that will not record has to be reported");
        assert!(e.contains("record the reboot time"), "{e}");
    }

    #[test]
    fn a_reboot_row_that_will_not_clear_stops_the_pass() {
        let db = Db::new();
        let mut u = supervisor();
        db.midplane
            .set("LINE-CARD0", &[(MIDPLANE_ACCESS_FIELD, "False".to_string())])
            .unwrap();
        db.module_reboot
            .set("LINE-CARD0", &[(REBOOT_REBOOT_FIELD, REBOOT_EXPECTED.to_string())])
            .unwrap();
        db.module_reboot.fail_writes("redis is gone");
        let e = u
            .check_midplane(&[module("LINE-CARD0", 1, ModuleStatus::Online)], &db.tables())
            .expect_err("a reboot row that will not clear has to be reported");
        assert!(e.contains("drop the reboot row"), "{e}");
    }

    /// The overrun path deletes the row to make its warning once.  A delete
    /// that fails is reported: swallowing it would leave the row, and the
    /// warning would repeat every cycle.
    #[test]
    fn an_overrun_whose_row_will_not_clear_stops_the_pass() {
        let db = Db::new();
        let mut u = ModuleUpdater::new(16, 16, Duration::from_secs(0), true);
        let mut down = module("LINE-CARD0", 1, ModuleStatus::Online);
        down.is_midplane_reachable = Some(false);
        db.module_reboot
            .set("LINE-CARD0", &[(REBOOT_TIMESTAMP_FIELD, "0".to_string())])
            .unwrap();

        db.module_reboot.fail_writes("redis is gone");
        let e = u
            .check_midplane(&[down], &db.tables())
            .expect_err("an overrun whose row will not go has to be reported");
        assert!(e.contains("drop the reboot row"), "{e}");
        assert!(!db.module_reboot.is_empty(), "the row is still there to be retried");
    }

    /// A reboot still inside its window is not an overrun, and its row stays.
    #[test]
    fn a_reboot_inside_its_window_is_not_an_overrun() {
        let db = Db::new();
        let u = ModuleUpdater::new(16, 16, Duration::from_secs(3600), true);
        let now = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap()
            .as_secs_f64();
        db.module_reboot
            .set("LINE-CARD0", &[(REBOOT_TIMESTAMP_FIELD, fmt::float(now))])
            .unwrap();
        assert!(!u.reboot_deadline_passed("LINE-CARD0", &db.tables()).unwrap());
        assert!(!db.module_reboot.is_empty(), "the row stays for the next cycle");
    }

    /// And a table that cannot be read ends the pass before anything is
    /// written: a previous status it could not look up is not "empty".
    #[test]
    fn an_unreadable_table_stops_the_pass_before_any_write() {
        let db = Db::new();
        db.module.fail_reads("redis is gone");
        supervisor()
            .refresh(&[module("LINE-CARD0", 1, ModuleStatus::Online)], &[], &db.tables())
            .expect_err("a module table that cannot be read has to be reported");
        assert!(db.module.is_empty(), "nothing was written on a guess");

        let db = Db::new();
        db.midplane.fail_reads("redis is gone");
        supervisor()
            .check_midplane(&[module("LINE-CARD0", 1, ModuleStatus::Online)], &db.tables())
            .expect_err("a midplane table that cannot be read has to be reported");
    }

    /// Teardown takes the midplane row with the module row: a card nobody is
    /// checking must not be left reported as reachable.
    #[test]
    fn teardown_takes_the_midplane_row_too() {
        let db = Db::new();
        let modules = [module("LINE-CARD0", 1, ModuleStatus::Online)];
        let mut u = supervisor();
        u.refresh(&modules, &[], &db.tables()).unwrap();
        u.check_midplane(&modules, &db.tables()).unwrap();
        assert!(!db.midplane.is_empty());
        u.clear(&modules, &db.tables());
        assert!(db.midplane.is_empty());
        assert!(db.entity.is_empty());
    }

    /// A supervisor's ASIC table holds the fabric cards' ASICs, which outlive
    /// this daemon restarting; a line card's holds only its own.
    #[test]
    fn only_a_line_card_clears_the_asic_table_on_the_way_out() {
        let db = Db::new();
        let modules = [module("LINE-CARD0", 1, ModuleStatus::Online)];
        let asics = [asic("LINE-CARD0", "0", "0000:03:00.0")];

        let mut lc = line_card();
        lc.refresh(&modules, &asics, &db.tables()).unwrap();
        lc.clear(&modules, &db.tables());
        assert!(db.asic.is_empty());
        assert!(db.module.is_empty());

        let db2 = Db::new();
        let mut sup = supervisor();
        sup.refresh(&modules, &asics, &db2.tables()).unwrap();
        sup.clear(&modules, &db2.tables());
        assert_eq!(db2.asic.len(), 1);
    }
}
