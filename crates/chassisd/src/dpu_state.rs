//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! The copy of this daemon that runs *on* a DPU.
//!
//! Ports `chassisd:DpuStateUpdater` and `chassisd:DpuChassisdDaemon`.  A DPU
//! runs its own SONiC, and the only thing its chassisd does is tell the NPU
//! whether its control and data planes are up -- which is what the NPU's
//! recovery machine reads.  Getting this wrong in the quiet direction gets the
//! DPU power cycled.

use std::path::Path;

use pmon_common::db::TableLike;

use crate::dpu_updater::{formatted_time, CP_STATE, CP_UPDATE_TIME, DP_STATE, DP_UPDATE_TIME};

/// `chassisd:BOOT_ID`: the DPU_STATE field the DPU's kernel boot_id goes in.
pub const BOOT_ID: &str = "boot_id";
/// `chassisd:BOOT_ID_PATH`.
pub const BOOT_ID_PATH: &str = "/proc/sys/kernel/random/boot_id";

/// This DPU's kernel boot_id, new on every boot.
///
/// `chassisd:DpuStateUpdater.get_boot_id`: one that cannot be read is warned
/// about and not published, and the NPU then records no reboot for it.
pub fn read_boot_id(path: &Path) -> Option<String> {
    match std::fs::read_to_string(path) {
        Ok(text) => Some(text.trim().to_string()).filter(|s| !s.is_empty()),
        Err(e) => {
            log::warn!("Failed to read boot_id from {}: {e}", path.display());
            None
        }
    }
}

/// What the two planes look like from inside the DPU.
pub struct Planes {
    pub data: bool,
    pub control: bool,
}

/// The data plane, when the platform does not answer for it.
///
/// Every configured port must be up.  One port down is the whole data plane
/// down, which is deliberate: a DPU forwards through all of them.
pub fn data_plane_from_ports(
    config_ports: &dyn TableLike,
    port_table: &dyn TableLike,
) -> Result<bool, String> {
    let ports = config_ports.get_keys()?;
    if ports.is_empty() {
        // Nothing configured is not "everything is up": a DPU whose port
        // configuration has not been pushed yet has no data plane to speak of.
        return Ok(false);
    }
    for p in &ports {
        let up = port_table
            .get(p)?
            .and_then(|row| row.into_iter().find(|(k, _)| k == "oper_status"))
            .is_some_and(|(_, v)| v.eq_ignore_ascii_case("up"));
        if !up {
            return Ok(false);
        }
    }
    Ok(true)
}

/// The control plane, likewise: SYSTEM_READY|SYSTEM_STATE says `UP`.
pub fn control_plane_from_system_ready(system_ready: &dyn TableLike) -> Result<bool, String> {
    Ok(system_ready
        .get("SYSTEM_STATE")?
        .and_then(|row| row.into_iter().find(|(k, _)| k == "Status"))
        .is_some_and(|(_, v)| v.eq_ignore_ascii_case("up")))
}

fn word(up: bool) -> &'static str {
    if up { "up" } else { "down" }
}

/// Publish whichever of the two planes changed, and the boot_id if it did.
///
/// Only on change: the NPU subscribes to this table, and rewriting it every
/// cycle would wake it for nothing -- and, worse, the timestamp beside each
/// state is read as "when it last changed", which a rewrite would falsify.
/// The boot_id is compared the same way
/// (`chassisd:DpuStateUpdater.update_state`), and a new one is the NPU's cue
/// to record why this DPU restarted.
pub fn publish(
    name: &str,
    planes: &Planes,
    boot_id: Option<&str>,
    table: &dyn TableLike,
) -> Result<(), String> {
    let row = table.get(name)?;
    let current = |field: &str| {
        row.as_ref()
            .and_then(|r| r.iter().find(|(k, _)| k == field))
            .map(|(_, v)| v.as_str())
    };

    for (state_field, time_field, want) in [
        (DP_STATE, DP_UPDATE_TIME, word(planes.data)),
        (CP_STATE, CP_UPDATE_TIME, word(planes.control)),
    ] {
        if current(state_field) == Some(want) {
            continue;
        }
        let fvs = [(state_field, want.to_string()), (time_field, formatted_time())];
        table
            .set(name, &fvs)
            .map_err(|e| format!("Failed to publish {state_field} for {name}: {e}"))?;
    }
    if let Some(id) = boot_id {
        if current(BOOT_ID) != Some(id) {
            table
                .set(name, &[(BOOT_ID, id.to_string())])
                .map_err(|e| format!("Failed to publish {BOOT_ID} for {name}: {e}"))?;
        }
    }
    Ok(())
}

/// Both planes down, for shutdown.
///
/// Said explicitly rather than left to time out: this daemon stopping is the
/// DPU's SONiC going away, and the NPU should hear it now rather than infer it.
pub fn shutdown(name: &str, table: &dyn TableLike) {
    // Logged, not propagated: this runs on the way out, where there is no
    // connection left to recover by leaving again.
    if let Err(e) = publish(name, &Planes { data: false, control: false }, None, table) {
        log::warn!("{e}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use pmon_common::db::MockTable;

    fn port_tables(states: &[(&str, &str)]) -> (MockTable, MockTable) {
        let (cfg, app) = (MockTable::new(), MockTable::new());
        for (name, oper) in states {
            cfg.set(name, &[("admin_status", "up".to_string())]).unwrap();
            app.set(name, &[("oper_status", oper.to_string())]).unwrap();
        }
        (cfg, app)
    }

    /// A DPU forwards through all of its ports, so one down is the data plane
    /// down.
    #[test]
    fn one_port_down_is_the_data_plane_down() {
        let (cfg, app) = port_tables(&[("Ethernet0", "up"), ("Ethernet4", "up")]);
        assert!(data_plane_from_ports(&cfg, &app).unwrap());
        let (cfg, app) = port_tables(&[("Ethernet0", "up"), ("Ethernet4", "down")]);
        assert!(!data_plane_from_ports(&cfg, &app).unwrap());
    }

    /// A port the config names and APPL_DB does not is not up.
    #[test]
    fn a_port_with_no_state_yet_is_not_up() {
        let cfg = MockTable::new();
        cfg.set("Ethernet0", &[("admin_status", "up".to_string())]).unwrap();
        assert!(!data_plane_from_ports(&cfg, &MockTable::new()).unwrap());
    }

    /// And no ports at all is not "everything is up": a DPU whose port
    /// configuration has not landed yet has no data plane to report.
    #[test]
    fn an_unconfigured_dpu_does_not_claim_a_data_plane() {
        assert!(!data_plane_from_ports(&MockTable::new(), &MockTable::new()).unwrap());
    }

    #[test]
    fn the_control_plane_follows_system_ready() {
        let t = MockTable::new();
        assert!(!control_plane_from_system_ready(&t).unwrap());
        t.set("SYSTEM_STATE", &[("Status", "DOWN".to_string())]).unwrap();
        assert!(!control_plane_from_system_ready(&t).unwrap());
        t.set("SYSTEM_STATE", &[("Status", "UP".to_string())]).unwrap();
        assert!(control_plane_from_system_ready(&t).unwrap(), "the case is the platform's business");
    }

    /// The NPU subscribes to this table.  Rewriting it every cycle would wake
    /// it for nothing -- and the timestamp beside each state is read as when
    /// it last changed, which a rewrite would falsify.
    #[test]
    fn a_state_that_did_not_change_is_not_rewritten() {
        let t = MockTable::new();
        publish("DPU0", &Planes { data: true, control: true }, None, &t).unwrap();
        let stamp = t.field("DPU0", DP_UPDATE_TIME);
        assert_eq!(t.field("DPU0", DP_STATE).as_deref(), Some("up"));

        publish("DPU0", &Planes { data: true, control: true }, None, &t).unwrap();
        assert_eq!(t.field("DPU0", DP_UPDATE_TIME), stamp);
    }

    /// The two planes move independently: a data plane that drops must not
    /// restamp the control plane.
    #[test]
    fn the_two_planes_are_published_independently() {
        let t = MockTable::new();
        publish("DPU0", &Planes { data: true, control: true }, None, &t).unwrap();
        let cp_stamp = t.field("DPU0", CP_UPDATE_TIME);

        publish("DPU0", &Planes { data: false, control: true }, None, &t).unwrap();
        assert_eq!(t.field("DPU0", DP_STATE).as_deref(), Some("down"));
        assert_eq!(t.field("DPU0", CP_STATE).as_deref(), Some("up"));
        assert_eq!(t.field("DPU0", CP_UPDATE_TIME), cp_stamp, "not restamped");
    }

    /// Said explicitly rather than left to time out: this daemon stopping is
    /// the DPU's SONiC going away.
    #[test]
    fn shutdown_reports_both_planes_down() {
        let t = MockTable::new();
        publish("DPU0", &Planes { data: true, control: true }, None, &t).unwrap();
        shutdown("DPU0", &t);
        assert_eq!(t.field("DPU0", DP_STATE).as_deref(), Some("down"));
        assert_eq!(t.field("DPU0", CP_STATE).as_deref(), Some("down"));
    }

    /// A plane whose source cannot be read is an error, not "down".
    ///
    /// The NPU acts on a DPU reported down; telling it so because this daemon
    /// lost its view of the DPU's own STATE_DB would have it act on nothing.
    #[test]
    fn a_plane_that_cannot_be_read_is_an_error_not_down() {
        let ready = MockTable::new();
        ready.fail_reads("redis is gone");
        assert!(control_plane_from_system_ready(&ready).is_err());

        let cfg = MockTable::new();
        cfg.fail_reads("redis is gone");
        assert!(data_plane_from_ports(&cfg, &MockTable::new()).is_err(), "the port list");

        let cfg = MockTable::new();
        cfg.set("Ethernet0", &[]).unwrap();
        let app = MockTable::new();
        app.fail_reads("redis is gone");
        assert!(data_plane_from_ports(&cfg, &app).is_err(), "and a port's state");
    }

    /// A publish the table refuses is reported, naming the plane, and the
    /// previous state is not what gets read back as current.
    #[test]
    fn a_refused_publish_is_reported() {
        let t = MockTable::new();
        t.fail_writes("redis is gone");
        let e = publish("DPU0", &Planes { data: true, control: true }, None, &t)
            .expect_err("a table that will not take the state has to be reported");
        assert!(e.contains(DP_STATE) && e.contains("DPU0"), "{e}");
    }

    /// And on shutdown the refusal is only logged: the process is leaving
    /// anyway, and there is no connection left to recover by leaving again.
    #[test]
    fn a_refused_shutdown_is_logged_not_raised() {
        let log = pmon_common::logging::capture();
        let t = MockTable::new();
        t.fail_writes("redis is gone");
        shutdown("DPU0", &t);
        assert!(log.logged(log::Level::Warn, "redis is gone"));
    }

    /// The boot_id is published when it is new, and not rewritten after.
    #[test]
    fn a_new_boot_id_is_published_once() {
        let t = MockTable::new();
        let planes = Planes { data: true, control: true };
        publish("DPU0", &planes, Some("b-1"), &t).unwrap();
        assert_eq!(t.field("DPU0", BOOT_ID).as_deref(), Some("b-1"));
        let before = t.writes().len();
        publish("DPU0", &planes, Some("b-1"), &t).unwrap();
        assert_eq!(t.writes().len(), before, "nothing changed, nothing written");
        publish("DPU0", &planes, Some("b-2"), &t).unwrap();
        assert_eq!(t.field("DPU0", BOOT_ID).as_deref(), Some("b-2"));
    }

    /// The boot_id file is read trimmed, and one that cannot be read is
    /// warned about and not published.
    #[test]
    fn the_boot_id_is_read_trimmed_or_not_at_all() {
        let log = pmon_common::logging::capture();
        let d = tempfile::tempdir().unwrap();
        let path = d.path().join("boot_id");
        std::fs::write(&path, "0b8e4f6c-1d2e-4a3b-9c8d-7e6f5a4b3c2d\n").unwrap();
        assert_eq!(read_boot_id(&path).as_deref(), Some("0b8e4f6c-1d2e-4a3b-9c8d-7e6f5a4b3c2d"));
        assert_eq!(read_boot_id(&d.path().join("missing")), None);
        assert!(log.logged(log::Level::Warn, "Failed to read boot_id from"));
    }
}
