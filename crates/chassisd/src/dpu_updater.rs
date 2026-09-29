//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! The SmartSwitch half: the NPU's view of its DPUs.
//!
//! Ports `chassisd:SmartSwitchModuleUpdater`.  A DPU is a module like a line
//! card is, and almost nothing else carries across: there is no supervisor, no
//! ASIC table, no chassis app DB -- and instead a reboot cause the NPU records
//! on the DPU's behalf and a recovery state machine that power cycles one that
//! will not come back.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::Instant;

use platform_api::{ModuleInfo, ModuleStatus, ModuleType, PlatformError};
use pmon_common::db::{Keyspace, TableLike};
use pmon_common::fmt;

use crate::dpu_recovery::{DpuState, Effect, Inputs, Planes, PowerCycler, Recovery, Thresholds};
use crate::dpu_reboot_cause as rc;
use crate::names::*;

/// CHASSIS_STATE_DB DPU_STATE, `chassisd:DPU_STATE_TABLE`, `chassisd:DP_STATE`,
/// `chassisd:DP_UPDATE_TIME`, `chassisd:CP_STATE`, `chassisd:CP_UPDATE_TIME`
/// and `chassisd:MP_STATE`.
pub const DPU_STATE_TABLE: &str = "DPU_STATE";
pub const DP_STATE: &str = "dpu_data_plane_state";
pub const DP_UPDATE_TIME: &str = "dpu_data_plane_time";
pub const CP_STATE: &str = "dpu_control_plane_state";
pub const CP_UPDATE_TIME: &str = "dpu_control_plane_time";
pub const MP_STATE: &str = "dpu_midplane_link_state";
pub const MP_REASON: &str = "dpu_midplane_link_reason";
pub const MP_UPDATE_TIME: &str = "dpu_midplane_link_time";

/// `chassisd:READY_STATUS`, `chassisd:RECOVERY_STATUS`, `chassisd:RESET_COUNT`,
/// `chassisd:LAST_DOWN_TIME` and `chassisd:LAST_READY_TIME`.
pub const READY_STATUS: &str = "ready_status";
pub const RECOVERY_STATUS: &str = "recovery_status";
pub const RESET_COUNT: &str = "reset_count";
pub const LAST_DOWN_TIME: &str = "last_down_time";
pub const LAST_READY_TIME: &str = "last_ready_time";

/// CONFIG_DB DEVICE_METADATA|localhost, `chassisd:DPU_AUTO_RECOVERY_FIELD` and
/// `chassisd:DPU_AUTO_RECOVERY_ENABLED`.
pub const DEVICE_METADATA_TABLE: &str = "DEVICE_METADATA";
pub const DPU_AUTO_RECOVERY_FIELD: &str = "dpu_auto_recovery";
const DPU_AUTO_RECOVERY_ENABLED: &str = "enable";

pub struct Tables<'a> {
    pub chassis: &'a dyn TableLike,
    pub module: &'a dyn TableLike,
    pub midplane: &'a dyn TableLike,
    pub dpu_state: &'a dyn TableLike,
    /// CONFIG_DB CHASSIS_MODULE.
    pub config: Option<&'a dyn TableLike>,
    /// CONFIG_DB DEVICE_METADATA.
    pub device_metadata: Option<&'a dyn TableLike>,
}

/// `"%a %b %d %I:%M:%S %p UTC %Y"`, the shape `show reboot-cause` prints.
pub fn formatted_time() -> String {
    chrono::Utc::now().format("%a %b %d %I:%M:%S %p UTC %Y").to_string()
}

/// `"%Y_%m_%d_%H_%M_%S"`, which sorts chronologically and is therefore what the
/// history file is named after.
pub fn time_name() -> String {
    chrono::Utc::now().format("%Y_%m_%d_%H_%M_%S").to_string()
}

fn field(row: &Option<Vec<(String, String)>>, name: &str) -> Option<String> {
    row.as_ref()?.iter().find(|(k, _)| k == name).map(|(_, v)| v.clone())
}

/// The human-readable form of a `%Y_%m_%d_%H_%M_%S` name, which is how
/// `chassisd:SmartSwitchModuleUpdater.persist_dpu_reboot_cause` fills a
/// record's `time`: the same moment as its name, not a second later.
pub fn formatted_from_name(name: &str) -> String {
    chrono::NaiveDateTime::parse_from_str(name, "%Y_%m_%d_%H_%M_%S")
        .map(|t| t.and_utc().format("%a %b %d %I:%M:%S %p UTC %Y").to_string())
        .unwrap_or_else(|_| formatted_time())
}

pub struct DpuUpdater {
    recovery: BTreeMap<String, Recovery>,
    thresholds: Thresholds,
    midplane_initialized: bool,
    /// `/host/reboot-cause/module/`, taken as an argument so a test can point
    /// it at a directory it owns.
    reboot_root: PathBuf,
}

impl DpuUpdater {
    pub fn new(
        modules: &[ModuleInfo],
        thresholds: Thresholds,
        midplane_initialized: bool,
        reboot_root: impl Into<PathBuf>,
        now: Instant,
    ) -> Self {
        Self {
            recovery: modules
                .iter()
                .map(|m| (m.name.clone(), Recovery::new(now)))
                .collect(),
            thresholds,
            midplane_initialized,
            reboot_root: reboot_root.into(),
        }
    }

    /// Where one DPU is in its recovery.  Not read by the daemon -- the state
    /// lives in CHASSIS_STATE_DB for anyone who needs it -- but it is what the
    /// tests below assert on, and a state machine whose state cannot be
    /// observed is one whose tests observe something else.
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn state_of(&self, name: &str) -> Option<DpuState> {
        self.recovery.get(name).map(|r| r.state)
    }

    // ── module table ──────────────────────────────────────────────────────────

    /// One pass over the DPUs: publish each one's row.
    ///
    /// `chassisd:SmartSwitchModuleUpdater.module_db_update`.  Nothing here
    /// looks at which way a DPU moved any more: a reboot is recognised by the
    /// DPU's boot_id changing, which [`rc::boot_id_update`] records, and not by
    /// guessing from the operational status, which a DPU that reboots quickly
    /// never shows as Offline.
    pub fn refresh(&mut self, modules: &[ModuleInfo], tables: &Tables<'_>) -> Result<(), String> {
        for m in modules {
            if !m.name.starts_with(ModuleType::Dpu.as_str()) {
                log::error!(
                    "Incorrect module-name {}. Should start with {} ",
                    m.name, ModuleType::Dpu.as_str());
                continue;
            }

            let status = m.oper_status.unwrap_or(ModuleStatus::Offline);
            let fvs = [
                (DESC_FIELD, fmt::opt_str(&m.description)),
                // A DPU has no slot, and Python publishes the sentinel rather
                // than omitting the field so the row shape stays the same.
                (SLOT_FIELD, fmt::NOT_AVAILABLE.to_string()),
                (OPERSTATUS_FIELD, status.as_str().to_string()),
                (SERIAL_FIELD, fmt::opt_str(&m.serial)),
            ];
            tables
                .module
                .set(&m.name, &fvs)
                .map_err(|e| format!("Failed to update {} to DB: {e}", m.name))?;
        }
        Ok(())
    }

    fn root(&self) -> &Path {
        &self.reboot_root
    }

    // ── midplane ──────────────────────────────────────────────────────────────

    /// Every DPU, with no supervisor to exclude.
    ///
    /// `chassisd:SmartSwitchModuleUpdater.check_midplane_reachability`.  A
    /// midplane that goes down is written with the reason it went down, which
    /// `hw` is asked for; one that is up clears any reason kept for it, so the
    /// next time it goes down is a new event.
    pub fn check_midplane(
        &self,
        modules: &[ModuleInfo],
        tables: &Tables<'_>,
        hw: &mut dyn PowerCycler,
    ) -> Result<(), String> {
        if !self.midplane_initialized {
            return Ok(());
        }
        for (index, m) in modules.iter().enumerate() {
            let key = if m.name.is_empty() { format!("MODULE {index}") } else { m.name.clone() };
            let ip = m.midplane_ip.clone().unwrap_or_else(|| INVALID_IP.to_string());
            let reachable = m.is_midplane_reachable.unwrap_or(false);
            let was = field(&tables.midplane.get(&key)?, MIDPLANE_ACCESS_FIELD)
                .unwrap_or_else(|| fmt::bool(false));

            match (reachable, was == fmt::bool(true)) {
                (false, true) => log::warn!("Unexpected: Module {key} lost midplane connectivity"),
                (true, false) => {
                    pmon_common::notice!("Module {key} midplane connectivity is up")
                }
                _ => {}
            }

            // Written only on a change: DPU_STATE is subscribed to by the DPU's
            // own copy of this daemon, and rewriting it every ten seconds would
            // wake every DPU on the switch for nothing.
            let have = field(&tables.dpu_state.get(&key)?, MP_STATE);
            if reachable {
                if have.as_deref() != Some("up") {
                    set_midplane_state(&key, "up", None, tables);
                }
                rc::clear_midplane_down_reason(self.root(), &key);
            } else if have.as_deref() != Some("down") {
                let reason = self.resolve_midplane_down_reason(m, &key, tables, hw);
                set_midplane_state(&key, "down", Some(&reason), tables);
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

    /// Why a DPU's midplane went down, for `dpu_midplane_link_reason`.
    ///
    /// `chassisd:SmartSwitchModuleUpdater._resolve_midplane_down_reason`, in
    /// its order:
    ///
    /// * a reason already kept for this DPU is the one it went down with --
    ///   this daemon restarted, and asking again would answer for a later
    ///   moment;
    /// * a graceful operation in progress holds the transition flag, and the
    ///   reason is `Planned: '<transition_type>'`;
    /// * otherwise the platform is asked, and the reason is
    ///   `Unplanned: '<reason>, <detail>'`, or `Unplanned: 'Unknown'` when it
    ///   has nothing to say.
    ///
    /// Whatever is chosen is kept on disk until the midplane comes back.
    fn resolve_midplane_down_reason(
        &self,
        m: &ModuleInfo,
        name: &str,
        tables: &Tables<'_>,
        hw: &mut dyn PowerCycler,
    ) -> String {
        if let Some(kept) = rc::read_midplane_down_reason(self.root(), name) {
            return kept;
        }

        let mut reason = None;
        match m.state_transition {
            Some(true) => match tables.module.get(&name.to_uppercase()) {
                Ok(row) => {
                    if let Some(t) = field(&row, "transition_type").filter(|t| !t.is_empty()) {
                        reason = Some(format!("Planned: '{t}'"));
                    }
                }
                Err(e) => log::error!("{name}: failed to read transition_type: {e}"),
            },
            Some(false) => {}
            // The column is absent when the getter is not implemented, which
            // is when `chassisd:try_get` logs this.
            None => log::error!("get_module_state_transition is not implemented"),
        }

        let reason = reason.unwrap_or_else(|| {
            let detail = match hw.midplane_down_reason(name) {
                Ok(r) => {
                    let parts: Vec<String> =
                        [r.reason, r.detail].into_iter().flatten().filter(|p| !p.is_empty()).collect();
                    if parts.is_empty() { "Unknown".to_string() } else { parts.join(", ") }
                }
                Err(PlatformError::NotSupported(_)) => {
                    log::error!("get_midplane_down_reason is not implemented");
                    "Unknown".to_string()
                }
                // Departs on purpose: Python's `try_get` catches only
                // NotImplementedError, so any other failure here ends the
                // daemon.  A reason the platform could not give is still an
                // unplanned loss of the midplane, and it is published as one.
                Err(e) => {
                    log::error!("{name}: get_midplane_down_reason failed: {e}");
                    "Unknown".to_string()
                }
            };
            format!("Unplanned: '{detail}'")
        });

        rc::write_midplane_down_reason(self.root(), name, &reason);
        reason
    }

    // ── recovery ──────────────────────────────────────────────────────────────

    fn auto_recovery(&self, tables: &Tables<'_>) -> bool {
        tables
            .device_metadata
            // Swallowed:
            // `chassisd:SmartSwitchModuleUpdater._is_auto_recovery_enabled`
            // wraps this read and falls back to the default on any failure.
            .and_then(|t| field(&t.get("localhost").ok().flatten(), DPU_AUTO_RECOVERY_FIELD))
            .as_deref()
            == Some(DPU_AUTO_RECOVERY_ENABLED)
    }

    fn admin_up(&self, name: &str, tables: &Tables<'_>) -> bool {
        // Unlike a chassis card, an unconfigured DPU is *not* up: Python
        // returns MODULE_STATUS_EMPTY here
        // (`chassisd:SmartSwitchModuleUpdater.get_module_admin_status`), which
        // is not 'up', so a SmartSwitch with no CHASSIS_MODULE rows keeps its
        // DPUs powered down until somebody says otherwise.
        tables
            .config
            .and_then(|c| field(&c.get(name).ok().flatten(), CHASSIS_MODULE_ADMIN_STATUS))
            .as_deref()
            == Some("up")
    }

    /// One cycle of the recovery machine for every DPU.
    pub fn update_recovery(
        &mut self,
        modules: &[ModuleInfo],
        tables: &Tables<'_>,
        hw: &mut dyn PowerCycler,
        now: Instant,
    ) -> Result<(), String> {
        let auto = self.auto_recovery(tables);
        for m in modules {
            let Some(recovery) = self.recovery.get_mut(&m.name) else { continue };
            let planes = {
                let row = tables.dpu_state.get(&m.name)?;
                Planes {
                    midplane: field(&row, MP_STATE),
                    control: field(&row, CP_STATE),
                    data: field(&row, DP_STATE),
                }
            };
            let oper = field(&tables.module.get(&m.name)?, OPERSTATUS_FIELD)
                .unwrap_or_else(|| ModuleStatus::Empty.as_str().to_string());
            let admin_up = tables
                .config
                .and_then(|c| field(&c.get(&m.name).ok().flatten(), CHASSIS_MODULE_ADMIN_STATUS))
                .as_deref()
                == Some("up");
            // A planned shutdown or reboot holds the transition flag; a
            // recovery this daemon started holds it too, and must not suppress
            // itself
            // (`chassisd:SmartSwitchModuleUpdater._is_planned_transition_in_progress`).
            let transition = m.state_transition.unwrap_or(false)
                // Swallowed for the same reason as above: the `try` around the
                // `transition_type` read in
                // `chassisd:SmartSwitchModuleUpdater._is_planned_transition_in_progress`.
                && field(&tables.module.get(&m.name).ok().flatten(), "transition_type").as_deref()
                    != Some("recovery");

            let input = Inputs {
                planes: &planes,
                oper_status: &oper,
                admin_up,
                auto_recovery: auto,
                transition_in_progress: transition,
                now,
            };
            let effects = recovery.step(&m.name, &input, &self.thresholds, hw);
            apply(&m.name, &effects, tables.dpu_state)?;
        }
        Ok(())
    }

    /// The start-up reset: every DPU back to Booting with a clean budget.
    ///
    /// On an NPU kernel panic every admin-up DPU is power cycled regardless of
    /// what it looks like -- the NPU's own memory is what died, so nothing it
    /// believes about the DPUs is trustworthy.
    pub fn init_recovery(
        &mut self,
        modules: &[ModuleInfo],
        tables: &Tables<'_>,
        hw: &mut dyn PowerCycler,
        npu_crashed: bool,
        now: Instant,
    ) -> Result<(), String> {
        if npu_crashed {
            log::warn!("NPU kernel crash detected; will power-cycle all admin-up DPUs");
        }
        let auto = self.auto_recovery(tables);
        for m in modules {
            let admin_up = self.admin_up(&m.name, tables);
            let Some(recovery) = self.recovery.get_mut(&m.name) else { continue };
            let effects = recovery.reset_for_startup(now);
            apply(&m.name, &effects, tables.dpu_state)?;

            if !npu_crashed || !admin_up {
                continue;
            }
            if !auto {
                recovery.state = DpuState::ManualIntervention;
                continue;
            }
            let effects = recovery.force_power_cycle(&m.name, &self.thresholds, now, hw);
            apply(&m.name, &effects, tables.dpu_state)?;
        }
        Ok(())
    }

    /// Mark every DPU not-ready on the way out.
    ///
    /// The row itself stays: `show dpu status` reads it, and a DPU that is
    /// running perfectly well should not vanish because the NPU's daemon
    /// restarted.
    pub fn clear(&self, modules: &[ModuleInfo], tables: &Tables<'_>) {
        for m in modules {
            // Logged, not propagated: the process is already leaving, so there
            // is no connection left to recover by leaving again.
            if let Err(e) =
                apply(&m.name, &[Effect::ReadyStatus(false), Effect::LastDownTime], tables.dpu_state)
            {
                log::warn!("{e}");
            }
            let _ = tables.module.del(&m.name);
            // On the way out; a read that fails here costs nothing.
            if tables.midplane.get(&m.name).ok().flatten().is_some() {
                let _ = tables.midplane.del(&m.name);
            }
        }
        let _ = tables.chassis.del(CHASSIS_INFO_KEY);
    }

    /// Remove a DPU's leftover CHASSIS_STATE_DB rows while it is shut down.
    ///
    /// DPU_STATE and REBOOT_CAUSE are kept: the first is how an operator sees
    /// that the DPU is down, and the second is the history they would consult
    /// to find out why.
    ///
    /// Over the whole database's key space rather than one table, because the
    /// two exceptions are recognised by their table name -- told only the
    /// unqualified keys a `Table` hands back, this would delete exactly the two
    /// rows it must keep.
    pub fn cleanup_shut_down(&self, modules: &[ModuleInfo], tables: &Tables<'_>, rows: &dyn Keyspace) {
        for m in modules {
            if self.admin_up(&m.name, tables) {
                continue;
            }
            for key in rows.keys(&format!("*{}*", m.name)) {
                if !key.contains(DPU_STATE_TABLE) && !key.contains("REBOOT_CAUSE") {
                    let _ = rows.del(&key);
                }
            }
        }
    }
}

/// Write the midplane half of one DPU_STATE row.
///
/// Free rather than a method because start-up writes it too, before the
/// updater has done anything (`init_dpu_state`), and the field layout must not
/// drift between the two callers.
///
/// A refused write is logged and passed over, in Python's words: both callers
/// port `chassisd:SmartSwitchModuleUpdater.update_dpu_state`, whose `try`
/// catches anything and logs it as unexpected.  Nothing is lost by carrying
/// on -- the loop reads the row back before deciding to write it, so a write
/// that did not land is tried again on the next pass.  The recovery fields in
/// the same row are another matter: Python writes those with no `try`, and
/// they still end the daemon here.
///
/// The reason goes with a `down`, and an `up` clears it.  The state is written
/// last, as `chassisd:SmartSwitchModuleUpdater.update_dpu_state` does, so a
/// write cut off halfway leaves the old state and the next pass writes it all
/// again.
pub fn set_midplane_state(key: &str, state: &str, reason: Option<&str>, tables: &Tables<'_>) {
    let mut fvs = vec![
        (MP_REASON, reason.unwrap_or("").to_string()),
        (MP_UPDATE_TIME, formatted_time()),
    ];
    if state == "down" {
        fvs.push((CP_STATE, "down".to_string()));
        fvs.push((DP_STATE, "down".to_string()));
    }
    fvs.push((MP_STATE, state.to_string()));
    if let Err(e) = tables.dpu_state.set(key, &fvs) {
        log::error!("Unexpected error: {e}");
    }
}

/// Seed one DPU's DPU_STATE row from what the hardware says at start-up.
///
/// The `update_dpu_state` call in
/// `chassisd:ChassisdDaemon.set_initial_dpu_admin_state`: the row is written
/// before the loop runs, so the recovery machine's first pass reads planes
/// rather than an absent row and mistakes "never written" for "down".
///
/// A DPU that is down keeps the reason kept for it, so a restart of this
/// daemon does not blank a reason it resolved before.
pub fn init_dpu_state(name: &str, online: bool, reboot_root: &Path, tables: &Tables<'_>) {
    if online {
        set_midplane_state(name, "up", None, tables);
    } else {
        let kept = rc::read_midplane_down_reason(reboot_root, name);
        set_midplane_state(name, "down", kept.as_deref(), tables);
    }
}

/// Write one DPU's effects to CHASSIS_STATE_DB.
fn apply(name: &str, effects: &[Effect], table: &dyn TableLike) -> Result<(), String> {
    for e in effects {
        let fv = match e {
            Effect::ReadyStatus(v) => (READY_STATUS, fmt::lower_bool(*v)),
            Effect::LastDownTime => (LAST_DOWN_TIME, formatted_time()),
            Effect::LastReadyTime => (LAST_READY_TIME, formatted_time()),
            Effect::ResetCount(n) => (RESET_COUNT, n.to_string()),
            Effect::RecoveryStatus(s) => (RECOVERY_STATUS, s.to_string()),
        };
        table
            .set(name, &[fv])
            .map_err(|err| format!("Failed to update DPU_STATE for {name}: {err}"))?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use platform_api::ModuleMidplaneDownReason;
    use pmon_common::db::MockTable;

    struct Db {
        chassis: MockTable,
        module: MockTable,
        midplane: MockTable,
        dpu_state: MockTable,
        config: MockTable,
        device_metadata: MockTable,
    }

    impl Db {
        fn new() -> Self {
            Self {
                chassis: MockTable::new(),
                module: MockTable::new(),
                midplane: MockTable::new(),
                dpu_state: MockTable::new(),
                config: MockTable::new(),
                device_metadata: MockTable::new(),
            }
        }
        fn tables(&self) -> Tables<'_> {
            Tables {
                chassis: &self.chassis,
                module: &self.module,
                midplane: &self.midplane,
                dpu_state: &self.dpu_state,
                config: Some(&self.config),
                device_metadata: Some(&self.device_metadata),
            }
        }
        fn admin_up(&self, name: &str) {
            self.config.set(name, &[(CHASSIS_MODULE_ADMIN_STATUS, "up".to_string())]).unwrap();
        }
    }

    struct Hw(Vec<String>);
    impl PowerCycler for Hw {
        fn power_cycle(&mut self, module: &str) -> bool {
            self.0.push(module.to_string());
            true
        }
    }

    fn dpu(name: &str, status: ModuleStatus) -> ModuleInfo {
        ModuleInfo {
            name: name.to_string(),
            parent_name: CHASSIS_PARENT.to_string(),
            position_in_parent: Some(0),
            presence: true,
            status: Some(true),
            is_replaceable: false,
            model: None,
            serial: Some("SN-DPU".to_string()),
            description: Some("DPU".to_string()),
            slot: None,
            r#type: Some(ModuleType::Dpu),
            oper_status: Some(status),
            base_mac: None,
            dpu_id: Some(0),
            maximum_consumed_power: None,
            midplane_ip: Some("169.254.200.1".to_string()),
            is_midplane_reachable: Some(true),
            state_transition: None,
        }
    }

    fn updater(modules: &[ModuleInfo], root: &Path) -> DpuUpdater {
        DpuUpdater::new(modules, Thresholds::default(), true, root, Instant::now())
    }

    #[test]
    fn a_dpu_is_published_without_a_slot() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        let m = [dpu("DPU0", ModuleStatus::Online)];
        let _ = updater(&m, r.path()).refresh(&m, &db.tables());
        assert_eq!(db.module.field("DPU0", OPERSTATUS_FIELD).as_deref(), Some("Online"));
        assert_eq!(db.module.field("DPU0", SLOT_FIELD).as_deref(), Some("N/A"),
            "a DPU has no slot, and the field shape stays the same");
    }



    /// DPU_STATE is subscribed to by the DPU's own copy of this daemon.
    /// Rewriting it every ten seconds would wake every DPU for nothing.
    #[test]
    fn the_midplane_state_is_written_only_when_it_changes() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        let m = [dpu("DPU0", ModuleStatus::Online)];
        let u = updater(&m, r.path());
        u.check_midplane(&m, &db.tables(), &mut Hw(Vec::new())).unwrap();
        let first = db.dpu_state.field("DPU0", MP_UPDATE_TIME);
        assert_eq!(db.dpu_state.field("DPU0", MP_STATE).as_deref(), Some("up"));
        u.check_midplane(&m, &db.tables(), &mut Hw(Vec::new())).unwrap();
        assert_eq!(db.dpu_state.field("DPU0", MP_UPDATE_TIME), first, "not rewritten");
    }

    /// A midplane that has gone takes the other two planes with it: nothing
    /// else can reach the DPU to find out, and leaving them `up` would have
    /// the recovery machine believe a DPU it cannot talk to.
    #[test]
    fn losing_the_midplane_takes_the_other_planes_down_with_it() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        let mut gone = dpu("DPU0", ModuleStatus::Online);
        gone.is_midplane_reachable = Some(false);
        let m = [gone];
        updater(&m, r.path()).check_midplane(&m, &db.tables(), &mut Hw(Vec::new())).unwrap();
        assert_eq!(db.dpu_state.field("DPU0", MP_STATE).as_deref(), Some("down"));
        assert_eq!(db.dpu_state.field("DPU0", CP_STATE).as_deref(), Some("down"));
        assert_eq!(db.dpu_state.field("DPU0", DP_STATE).as_deref(), Some("down"));
    }

    /// Unlike a chassis card, an unconfigured DPU is *not* up.  Defaulting it
    /// to up would have a SmartSwitch power on every DPU it has before anyone
    /// asked it to.
    #[test]
    fn an_unconfigured_dpu_is_not_administratively_up() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        let m = [dpu("DPU0", ModuleStatus::Online)];
        let mut u = updater(&m, r.path());
        let mut hw = Hw(Vec::new());
        u.update_recovery(&m, &db.tables(), &mut hw, Instant::now()).unwrap();
        assert_eq!(u.state_of("DPU0"), Some(DpuState::AdminDown));
    }

    #[test]
    fn a_configured_dpu_with_every_plane_up_reaches_ready() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        db.admin_up("DPU0");
        db.dpu_state
            .set("DPU0", &[(MP_STATE, "up".into()), (CP_STATE, "up".into()), (DP_STATE, "up".into())])
            .unwrap();
        let m = [dpu("DPU0", ModuleStatus::Online)];
        let mut u = updater(&m, r.path());
        u.update_recovery(&m, &db.tables(), &mut Hw(Vec::new()), Instant::now()).unwrap();
        assert_eq!(u.state_of("DPU0"), Some(DpuState::Ready));
        assert_eq!(db.dpu_state.field("DPU0", READY_STATUS).as_deref(), Some("true"));
    }

    /// A recovery this daemon started holds the transition flag too, and must
    /// not suppress itself -- or the first power cycle would be the last.
    #[test]
    fn a_recovery_transition_does_not_suppress_the_recovery() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        db.admin_up("DPU0");
        db.module.set("DPU0", &[("transition_type", "recovery".to_string())]).unwrap();
        db.dpu_state
            .set("DPU0", &[(MP_STATE, "up".into()), (CP_STATE, "up".into()), (DP_STATE, "up".into())])
            .unwrap();
        let mut m = [dpu("DPU0", ModuleStatus::Online)];
        m[0].state_transition = Some(true);
        let mut u = updater(&m, r.path());
        u.update_recovery(&m, &db.tables(), &mut Hw(Vec::new()), Instant::now()).unwrap();
        assert_eq!(u.state_of("DPU0"), Some(DpuState::Ready), "not suppressed");
    }

    /// A planned shutdown does suppress it.
    #[test]
    fn a_planned_transition_suppresses_the_recovery() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        db.admin_up("DPU0");
        db.dpu_state
            .set("DPU0", &[(MP_STATE, "down".into()), (CP_STATE, "down".into())])
            .unwrap();
        let mut m = [dpu("DPU0", ModuleStatus::Online)];
        m[0].state_transition = Some(true);
        let mut u = updater(&m, r.path());
        let mut hw = Hw(Vec::new());
        u.update_recovery(&m, &db.tables(), &mut hw, Instant::now()).unwrap();
        assert!(hw.0.is_empty());
    }

    /// The NPU's own memory is what died, so nothing it believes about the
    /// DPUs is trustworthy: every admin-up one is power cycled.
    #[test]
    fn an_npu_crash_power_cycles_every_admin_up_dpu() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        db.admin_up("DPU0");
        db.device_metadata
            .set("localhost", &[(DPU_AUTO_RECOVERY_FIELD, "enable".to_string())])
            .unwrap();
        let m = [dpu("DPU0", ModuleStatus::Online), dpu("DPU1", ModuleStatus::Online)];
        let mut u = updater(&m, r.path());
        let mut hw = Hw(Vec::new());
        u.init_recovery(&m, &db.tables(), &mut hw, true, Instant::now()).unwrap();
        assert_eq!(hw.0, vec!["DPU0".to_string()], "DPU1 is not admin-up");
    }

    /// An ordinary start-up does not.
    #[test]
    fn an_ordinary_startup_power_cycles_nothing() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        db.admin_up("DPU0");
        let m = [dpu("DPU0", ModuleStatus::Online)];
        let mut u = updater(&m, r.path());
        let mut hw = Hw(Vec::new());
        u.init_recovery(&m, &db.tables(), &mut hw, false, Instant::now()).unwrap();
        assert!(hw.0.is_empty());
        assert_eq!(db.dpu_state.field("DPU0", READY_STATUS).as_deref(), Some("false"));
        assert_eq!(db.dpu_state.field("DPU0", RESET_COUNT).as_deref(), Some("0"));
    }

    /// DPU_STATE and REBOOT_CAUSE survive: the first is how an operator sees
    /// that the DPU is down, the second is why.  The keys are the database's
    /// own, not a table's -- told only `DPU0` the guard below would match
    /// nothing and the sweep would delete the two rows it exists to keep.
    #[test]
    fn cleaning_up_a_shut_down_dpu_keeps_its_state_and_its_history() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        let rows = pmon_common::db::MockKeyspace::new(&[
            "DPU_STATE|DPU0", "REBOOT_CAUSE|DPU0|x", "TRANSCEIVER_INFO|DPU0|Ethernet0",
            "DPU_STATE|DPU1",
        ]);
        let m = [dpu("DPU0", ModuleStatus::Offline)];
        updater(&m, r.path()).cleanup_shut_down(&m, &db.tables(), &rows);
        assert_eq!(rows.remaining(), vec![
            "DPU_STATE|DPU0".to_string(),
            "REBOOT_CAUSE|DPU0|x".to_string(),
            "DPU_STATE|DPU1".to_string(),
        ]);
    }

    /// A DPU an operator has started is left entirely alone: the sweep is
    /// about a DPU that is *meant* to be off.
    #[test]
    fn an_admin_up_dpu_is_not_swept() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        db.admin_up("DPU0");
        let rows = pmon_common::db::MockKeyspace::new(&["TRANSCEIVER_INFO|DPU0|Ethernet0"]);
        let m = [dpu("DPU0", ModuleStatus::Online)];
        updater(&m, r.path()).cleanup_shut_down(&m, &db.tables(), &rows);
        assert_eq!(rows.remaining().len(), 1);
    }

    /// A name outside the DPU family is a platform bug; publishing it would
    /// put a row in the table `show dpu status` cannot categorise.
    #[test]
    fn a_module_that_is_not_a_dpu_is_not_published() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        let mut odd = dpu("LINE-CARD0", ModuleStatus::Online);
        odd.r#type = Some(ModuleType::LineCard);
        let m = [odd];
        let _ = updater(&m, r.path()).refresh(&m, &db.tables());
        assert!(db.module.is_empty());
    }

    /// A refusing table costs that DPU's row and not the pass.
    #[test]
    fn a_refusing_table_stops_the_pass() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        db.module.fail_writes("read-only replica");
        let m = [dpu("DPU0", ModuleStatus::Online), dpu("DPU1", ModuleStatus::Online)];
        let e = updater(&m, r.path())
            .refresh(&m, &db.tables())
            .expect_err("a table that will not take a write has to be reported");
        assert!(e.contains("DPU0"), "the error names the row it stopped on: {e}");
        assert!(db.module.is_empty());

        let db = Db::new();
        db.midplane.fail_writes("read-only replica");
        db.dpu_state.fail_writes("read-only replica");
        updater(&m, r.path())
            .check_midplane(&m, &db.tables(), &mut Hw(Vec::new()))
            .expect_err("and so does the midplane table");
        assert!(db.midplane.is_empty());
    }

    /// The midplane's loss and return are each said once.
    #[test]
    fn the_dpu_midplane_reports_each_transition_once() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        let with = |up: bool| {
            let mut m = dpu("DPU0", ModuleStatus::Online);
            m.is_midplane_reachable = Some(up);
            m
        };
        let u = updater(&[with(true)], r.path());
        u.check_midplane(&[with(true)], &db.tables(), &mut Hw(Vec::new())).unwrap();
        u.check_midplane(&[with(false)], &db.tables(), &mut Hw(Vec::new())).unwrap();
        assert_eq!(db.dpu_state.field("DPU0", MP_STATE).as_deref(), Some("down"));
        u.check_midplane(&[with(false)], &db.tables(), &mut Hw(Vec::new())).unwrap();
        u.check_midplane(&[with(true)], &db.tables(), &mut Hw(Vec::new())).unwrap();
        assert_eq!(db.dpu_state.field("DPU0", MP_STATE).as_deref(), Some("up"));
    }

    /// An NPU crash with auto-recovery off leaves the DPUs for an operator
    /// rather than power cycling them behind a disabled feature flag.
    #[test]
    fn an_npu_crash_with_auto_recovery_off_asks_for_a_human() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        db.admin_up("DPU0");
        let m = [dpu("DPU0", ModuleStatus::Online)];
        let mut u = updater(&m, r.path());
        let mut hw = Hw(Vec::new());
        u.init_recovery(&m, &db.tables(), &mut hw, true, Instant::now()).unwrap();
        assert!(hw.0.is_empty());
        assert_eq!(u.state_of("DPU0"), Some(DpuState::ManualIntervention));
    }



    /// A midplane row that will not write ends the pass, after the DPU_STATE
    /// half has gone through.
    #[test]
    fn a_refusing_midplane_table_stops_the_dpu_pass() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        let m = [dpu("DPU0", ModuleStatus::Online)];
        db.midplane.fail_writes("redis is gone");
        let e = updater(&m, r.path())
            .check_midplane(&m, &db.tables(), &mut Hw(Vec::new()))
            .expect_err("a midplane row that will not write has to be reported");
        assert!(e.contains("midplane table for DPU0"), "{e}");
        assert_eq!(db.dpu_state.field("DPU0", MP_STATE).as_deref(), Some("up"));
    }

    /// A DPU_STATE that will not take the midplane half is logged, as
    /// `chassisd:SmartSwitchModuleUpdater.update_dpu_state` logs it, and the
    /// pass goes on to the midplane row.
    #[test]
    fn a_refusing_dpu_state_table_is_logged_and_the_pass_goes_on() {
        let log = pmon_common::logging::capture();
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        let m = [dpu("DPU0", ModuleStatus::Online)];
        db.dpu_state.fail_writes("redis is gone");
        updater(&m, r.path())
            .check_midplane(&m, &db.tables(), &mut Hw(Vec::new()))
            .expect("a refused midplane state is not the end of the pass");
        assert!(log.logged(log::Level::Error, "Unexpected error: redis is gone"));
        assert!(!db.midplane.is_empty(), "the midplane row still went");
    }

    /// And the write that did not land is tried again on the next pass: the
    /// pass reads the row back before deciding, so nothing is lost.
    #[test]
    fn a_refused_midplane_state_is_written_on_the_next_pass() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        let m = [dpu("DPU0", ModuleStatus::Online)];
        let u = updater(&m, r.path());
        db.dpu_state.fail_writes("redis is gone");
        u.check_midplane(&m, &db.tables(), &mut Hw(Vec::new())).unwrap();
        assert_eq!(db.dpu_state.field("DPU0", MP_STATE), None);

        db.dpu_state.allow_writes();
        u.check_midplane(&m, &db.tables(), &mut Hw(Vec::new())).unwrap();
        assert_eq!(db.dpu_state.field("DPU0", MP_STATE).as_deref(), Some("up"));
    }


    /// On the way out a refusal is only logged, and the rest of the teardown
    /// still runs.
    #[test]
    fn a_refusing_teardown_is_logged_and_finishes() {
        let log = pmon_common::logging::capture();
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        let m = [dpu("DPU0", ModuleStatus::Online)];
        let mut u = updater(&m, r.path());
        u.refresh(&m, &db.tables()).unwrap();
        assert!(!db.module.is_empty());

        db.dpu_state.fail_writes("redis is gone");
        u.clear(&m, &db.tables());
        assert!(log.logged(log::Level::Warn, "redis is gone"));
        assert!(db.module.is_empty(), "the module row still went");
    }

    #[test]
    fn teardown_takes_the_midplane_row_too() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        let m = [dpu("DPU0", ModuleStatus::Online)];
        let u = updater(&m, r.path());
        u.check_midplane(&m, &db.tables(), &mut Hw(Vec::new())).unwrap();
        assert!(!db.midplane.is_empty());
        u.clear(&m, &db.tables());
        assert!(db.midplane.is_empty());
    }

    /// Teardown marks the DPUs not-ready but leaves the row: a DPU running
    /// perfectly well should not vanish because the NPU's daemon restarted.
    #[test]
    fn teardown_marks_not_ready_without_removing_the_dpu_state_row() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        let m = [dpu("DPU0", ModuleStatus::Online)];
        let u = updater(&m, r.path());
        u.clear(&m, &db.tables());
        assert_eq!(db.dpu_state.field("DPU0", READY_STATUS).as_deref(), Some("false"));
        assert!(db.dpu_state.field("DPU0", LAST_DOWN_TIME).is_some());
        assert!(db.module.is_empty());
    }

    /// The platform's answer for a DPU whose midplane goes down, and who asked.
    struct Reasons {
        answer: Result<ModuleMidplaneDownReason, PlatformError>,
        asked: Vec<String>,
    }
    impl Reasons {
        fn answering(reason: Option<&str>, detail: Option<&str>) -> Self {
            Self {
                answer: Ok(ModuleMidplaneDownReason {
                    reason: reason.map(str::to_string),
                    detail: detail.map(str::to_string),
                }),
                asked: Vec::new(),
            }
        }
    }
    impl PowerCycler for Reasons {
        fn power_cycle(&mut self, _module: &str) -> bool {
            unreachable!("the midplane check does not power cycle")
        }
        fn midplane_down_reason(
            &mut self,
            module: &str,
        ) -> Result<ModuleMidplaneDownReason, PlatformError> {
            self.asked.push(module.to_string());
            self.answer.clone()
        }
    }

    fn unreachable_dpu() -> ModuleInfo {
        let mut m = dpu("DPU0", ModuleStatus::Online);
        m.is_midplane_reachable = Some(false);
        m.state_transition = Some(false);
        m
    }

    /// A DPU that goes offline is published, and that is all: a reboot is
    /// recorded when its boot_id changes, not guessed from the status.
    #[test]
    fn a_dpu_going_offline_is_published_and_records_nothing_itself() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        let m = [dpu("DPU0", ModuleStatus::Online)];
        let mut u = updater(&m, r.path());
        u.refresh(&m, &db.tables()).unwrap();
        u.refresh(&[dpu("DPU0", ModuleStatus::Offline)], &db.tables()).unwrap();
        assert_eq!(db.module.field("DPU0", OPERSTATUS_FIELD).as_deref(), Some("Offline"));
        assert_eq!(rc::previous(r.path(), "DPU0"), None);
    }

    /// An unplanned loss publishes both halves of what the platform said.
    #[test]
    fn an_unplanned_midplane_loss_publishes_the_platforms_reason() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        let m = [unreachable_dpu()];
        let mut hw = Reasons::answering(Some("Power Loss"), Some("DPU power rail dropped"));
        updater(&m, r.path()).check_midplane(&m, &db.tables(), &mut hw).unwrap();
        assert_eq!(db.dpu_state.field("DPU0", MP_REASON).as_deref(),
                   Some("Unplanned: 'Power Loss, DPU power rail dropped'"));
        assert_eq!(hw.asked, vec!["DPU0".to_string()]);
        assert_eq!(rc::read_midplane_down_reason(r.path(), "DPU0").as_deref(),
                   Some("Unplanned: 'Power Loss, DPU power rail dropped'"), "and it is kept");
    }

    /// A platform that answers one string gives the whole reason, and one
    /// that answers nothing gives 'Unknown'.
    #[test]
    fn an_unplanned_reason_takes_what_the_platform_gave() {
        for (reason, detail, want) in [
            (Some("Power Loss"), None, "Unplanned: 'Power Loss'"),
            (None, None, "Unplanned: 'Unknown'"),
            (Some(""), Some(""), "Unplanned: 'Unknown'"),
        ] {
            let r = tempfile::tempdir().unwrap();
            let db = Db::new();
            let m = [unreachable_dpu()];
            let mut hw = Reasons::answering(reason, detail);
            updater(&m, r.path()).check_midplane(&m, &db.tables(), &mut hw).unwrap();
            assert_eq!(db.dpu_state.field("DPU0", MP_REASON).as_deref(), Some(want));
        }
    }

    /// A platform without the getter is 'Unknown', said the way
    /// `chassisd:try_get` says it.
    #[test]
    fn a_platform_without_the_reason_publishes_unknown() {
        let log = pmon_common::logging::capture();
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        let mut m = unreachable_dpu();
        m.state_transition = None;
        let m = [m];
        updater(&m, r.path()).check_midplane(&m, &db.tables(), &mut Hw(Vec::new())).unwrap();
        assert_eq!(db.dpu_state.field("DPU0", MP_REASON).as_deref(), Some("Unplanned: 'Unknown'"));
        assert!(log.logged(log::Level::Error, "get_midplane_down_reason is not implemented"));
        assert!(log.logged(log::Level::Error, "get_module_state_transition is not implemented"));
    }

    /// A graceful operation holds the transition flag, and its type is the
    /// reason; the platform is not asked.
    #[test]
    fn a_planned_transition_publishes_its_type() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        db.module.set("DPU0", &[("transition_type", "shutdown".to_string())]).unwrap();
        let mut m = unreachable_dpu();
        m.state_transition = Some(true);
        let m = [m];
        let mut hw = Reasons::answering(Some("Power Loss"), None);
        updater(&m, r.path()).check_midplane(&m, &db.tables(), &mut hw).unwrap();
        assert_eq!(db.dpu_state.field("DPU0", MP_REASON).as_deref(), Some("Planned: 'shutdown'"));
        assert!(hw.asked.is_empty());
    }

    /// A flag with no type recorded is not a plan anyone can name, so the
    /// platform is asked after all.
    #[test]
    fn a_transition_without_a_type_is_unplanned() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        let mut m = unreachable_dpu();
        m.state_transition = Some(true);
        let m = [m];
        let mut hw = Reasons::answering(Some("Power Loss"), None);
        updater(&m, r.path()).check_midplane(&m, &db.tables(), &mut hw).unwrap();
        assert_eq!(db.dpu_state.field("DPU0", MP_REASON).as_deref(), Some("Unplanned: 'Power Loss'"));
    }

    /// A reason already kept is the one the midplane went down with: after a
    /// restart of this daemon it is republished, not asked for again.
    #[test]
    fn a_kept_reason_is_republished_not_asked_again() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        rc::write_midplane_down_reason(r.path(), "DPU0", "Unplanned: 'Power Loss'");
        let m = [unreachable_dpu()];
        let mut hw = Reasons::answering(Some("Something later"), None);
        updater(&m, r.path()).check_midplane(&m, &db.tables(), &mut hw).unwrap();
        assert_eq!(db.dpu_state.field("DPU0", MP_REASON).as_deref(), Some("Unplanned: 'Power Loss'"));
        assert!(hw.asked.is_empty());
    }

    /// The midplane coming back clears the reason, in DPU_STATE and on disk.
    #[test]
    fn the_midplane_returning_clears_the_reason() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        let down = [unreachable_dpu()];
        let u = updater(&down, r.path());
        u.check_midplane(&down, &db.tables(), &mut Reasons::answering(Some("Power Loss"), None))
            .unwrap();
        let up = [dpu("DPU0", ModuleStatus::Online)];
        u.check_midplane(&up, &db.tables(), &mut Hw(Vec::new())).unwrap();
        assert_eq!(db.dpu_state.field("DPU0", MP_STATE).as_deref(), Some("up"));
        assert_eq!(db.dpu_state.field("DPU0", MP_REASON).as_deref(), Some(""));
        assert_eq!(rc::read_midplane_down_reason(r.path(), "DPU0"), None);
    }

    /// The state goes last, so a write cut off halfway leaves the old state
    /// and is redone on the next pass.
    #[test]
    fn the_midplane_state_is_the_last_field_written() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        let m = [unreachable_dpu()];
        updater(&m, r.path())
            .check_midplane(&m, &db.tables(), &mut Reasons::answering(Some("x"), None))
            .unwrap();
        let fields: Vec<String> = db.dpu_state.writes().into_iter().map(|(_, f)| f).collect();
        assert_eq!(fields.last().map(String::as_str), Some(MP_STATE), "{fields:?}");
        assert!(fields.contains(&MP_REASON.to_string()));
    }

    /// A reason that cannot be kept on disk is logged, and still published.
    #[test]
    fn a_reason_that_cannot_be_kept_is_logged_and_still_published() {
        let log = pmon_common::logging::capture();
        let r = tempfile::tempdir().unwrap();
        // A file where the directory should be: nothing below it can be made.
        let root = r.path().join("not-a-dir");
        std::fs::write(&root, "").unwrap();
        let db = Db::new();
        let m = [unreachable_dpu()];
        updater(&m, &root)
            .check_midplane(&m, &db.tables(), &mut Reasons::answering(Some("Power Loss"), None))
            .unwrap();
        assert_eq!(db.dpu_state.field("DPU0", MP_REASON).as_deref(), Some("Unplanned: 'Power Loss'"));
        assert!(log.logged(log::Level::Error, "DPU0: persist midplane reason failed"));
    }

    /// Start-up seeds a DPU that is down with the reason kept for it, and one
    /// that is up with none.
    #[test]
    fn start_up_seeds_a_down_dpu_with_its_kept_reason() {
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        rc::write_midplane_down_reason(r.path(), "DPU0", "Planned: 'shutdown'");
        init_dpu_state("DPU0", false, r.path(), &db.tables());
        init_dpu_state("DPU1", true, r.path(), &db.tables());
        assert_eq!(db.dpu_state.field("DPU0", MP_STATE).as_deref(), Some("down"));
        assert_eq!(db.dpu_state.field("DPU0", MP_REASON).as_deref(), Some("Planned: 'shutdown'"));
        assert_eq!(db.dpu_state.field("DPU1", MP_STATE).as_deref(), Some("up"));
        assert_eq!(db.dpu_state.field("DPU1", MP_REASON).as_deref(), Some(""));
    }

    /// A record's `time` is the moment its name says, and a name that is not
    /// one is given the current time rather than nothing.
    #[test]
    fn a_records_time_is_the_moment_its_name_says() {
        assert_eq!(formatted_from_name("2026_09_20_04_06_29"), "Sun Sep 20 04:06:29 AM UTC 2026");
        let fallback = formatted_from_name("not a time");
        assert!(fallback.contains(" UTC "), "{fallback}");
    }

    /// A transition type that cannot be read is logged, and the loss is
    /// treated as unplanned: the platform is asked.
    #[test]
    fn a_transition_type_that_cannot_be_read_is_asked_of_the_platform() {
        let log = pmon_common::logging::capture();
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        db.module.fail_reads("redis is gone");
        let mut m = unreachable_dpu();
        m.state_transition = Some(true);
        let m = [m];
        let mut hw = Reasons::answering(Some("Power Loss"), None);
        updater(&m, r.path()).check_midplane(&m, &db.tables(), &mut hw).unwrap();
        assert_eq!(db.dpu_state.field("DPU0", MP_REASON).as_deref(), Some("Unplanned: 'Power Loss'"));
        assert!(log.logged(log::Level::Error, "DPU0: failed to read transition_type: redis is gone"));
    }

    /// A platform that fails to answer is still an unplanned loss, published
    /// as 'Unknown' with the failure logged.
    #[test]
    fn a_platform_that_fails_to_say_why_publishes_unknown() {
        let log = pmon_common::logging::capture();
        let r = tempfile::tempdir().unwrap();
        let db = Db::new();
        let m = [unreachable_dpu()];
        let mut hw = Reasons {
            answer: Err(PlatformError::Backend("i2c timeout".to_string())),
            asked: Vec::new(),
        };
        updater(&m, r.path()).check_midplane(&m, &db.tables(), &mut hw).unwrap();
        assert_eq!(db.dpu_state.field("DPU0", MP_REASON).as_deref(), Some("Unplanned: 'Unknown'"));
        assert!(log.logged(log::Level::Error,
            "DPU0: get_midplane_down_reason failed: platform error: i2c timeout"));
    }
}
