//! Unit tests of the port actor, against the mocks of [`crate::testing`].
//! Each test notes the Python unit tests (tests/test_y_cable_helper.py,
//! test_ycable.py) whose behavior it covers.

// Tests that change process-wide settings hold GLOBALS across awaits on
// purpose: it makes them take turns.
#![allow(clippy::await_holding_lock)]

use std::sync::atomic::Ordering;
use std::sync::Arc;

use crate::config::{self, CableType};
use crate::db::{DbId, Event, MemOp};
use crate::grpc::RpcError;
use crate::proto::AdminReply;
use crate::pyval::{PyArg, PyVal};
use crate::schema::*;
use crate::testing::*;

use super::{Msg, PortActor};

fn globals() -> std::sync::MutexGuard<'static, ()> {
    let g = GLOBALS.lock().unwrap_or_else(|p| p.into_inner());
    config::set_telemetry_disabled(false);
    g
}

// --- Active-standby: initialization -----------------------------------------------

/// test_check_identifier_presence_and_update_mux_table_entry*,
/// test_read_y_cable_and_update_statedb_port_tbl*, test_init_port_sfp_status_tbl
#[tokio::test]
async fn standby_init_publishes_cable_state() {
    let f = Fixture::new();
    let mut a = f.standby();
    a.handle(Msg::Init(None)).await;
    assert_eq!(f.mux_state(), row3("active", "1", "1"));
    assert!(a.initialized);
    let info = f.row(DbId::State, MUX_CABLE_INFO);
    assert_eq!(info["tor_active"], "active");
    assert_eq!(info["mux_direction"], "self");
    assert_eq!(info["internal_voltage"], "3.3");
    let st = f.row(DbId::State, MUX_CABLE_STATIC_INFO);
    assert_eq!(st["read_side"], "tor1");
    assert_eq!(st["nic_lane1_precursor1"], "101");
    assert_eq!(st["tor_self_lane2_postcursor2"], "215");
    assert_eq!(st["tor_peer_lane1_maincursor"], "123");
}

/// test_read_y_cable_and_update_statedb_port_tbl_* (read side B: standby)
#[tokio::test]
async fn standby_init_read_side_b() {
    let f = Fixture::new();
    f.cable(|c| c.read_side = 2);
    let mut a = f.standby();
    a.handle(Msg::Init(None)).await;
    assert_eq!(f.mux_state(), row3("standby", "2", "1"));
    let st = f.row(DbId::State, MUX_CABLE_STATIC_INFO);
    assert_eq!(st["read_side"], "tor2");
    // Self is ToR B (target 2), peer ToR A (target 1).
    assert_eq!(st["tor_self_lane1_precursor1"], "121");
    assert_eq!(st["tor_peer_lane1_precursor1"], "111");
}

/// test_check_identifier_presence_and_update_mux_table_entry (no presence),
/// test_create_tables_and_insert_mux_unknown_entries
#[tokio::test]
async fn standby_init_absent_cable() {
    let f = Fixture::new();
    f.cable(|c| c.present = false);
    let mut a = f.standby();
    a.handle(Msg::Init(None)).await;
    assert_eq!(f.mux_state(), row3("unknown", "-1", "-1"));
    let info = f.row(DbId::State, MUX_CABLE_INFO);
    assert_eq!(info["tor_active"], "unknown");
    assert_eq!(info["version_nic_next"], "N/A");
    let st = f.row(DbId::State, MUX_CABLE_STATIC_INFO);
    assert_eq!(st["read_side"], "-1");
    assert!(st
        .iter()
        .filter(|(k, _)| *k != "read_side")
        .all(|(_, v)| v == "N/A"));
    assert!(f.platform.loads().is_empty());
}

/// test_check_identifier_presence_and_update_mux_table_entry_module_dir_none,
/// _module_none, _no_vendor ... (no usable driver): unknown, placeholder, no static info
#[tokio::test]
async fn standby_init_without_driver() {
    let f = Fixture::new();
    f.cable(|c| c.load = None);
    let mut a = f.standby();
    a.handle(Msg::Init(None)).await;
    assert_eq!(f.mux_state(), row3("unknown", "-1", "-1"));
    assert_eq!(
        f.row(DbId::State, MUX_CABLE_INFO)["mux_direction"],
        "unknown"
    );
    assert!(f
        .db
        .row(DbId::State, MUX_CABLE_STATIC_INFO, TEST_PORT)
        .is_none());
    assert!(!a.initialized);
}

/// test_check_identifier_presence_and_update_mux_table_entry with
/// y_cable_presence already set: only the first port Python initializes
/// writes MUX_CABLE_INFO; the others wait for the telemetry pass.
#[tokio::test]
async fn standby_init_after_another_port_writes_no_info() {
    let f = Fixture::new();
    f.shared.ycable_presence.store(true, Ordering::SeqCst);
    let mut a = f.standby();
    a.handle(Msg::Init(None)).await;
    assert_eq!(f.mux_state(), row3("active", "1", "1"));
    assert!(f.db.row(DbId::State, MUX_CABLE_INFO, TEST_PORT).is_none());
    assert_eq!(
        f.row(DbId::State, MUX_CABLE_STATIC_INFO)["read_side"],
        "tor1"
    );
}

/// Startup (test_init_ports_status_for_y_cable): the actor reports whether
/// it sets y_cable_presence and leaves the flag and MUX_CABLE_INFO to the
/// daemon, which decides in Python's port order; it writes MUX_CABLE_INFO
/// when told to.
#[tokio::test]
async fn standby_startup_init_reports_presence() {
    let f = Fixture::new();
    let mut a = f.standby();
    let (tx, rx) = tokio::sync::oneshot::channel();
    a.handle(Msg::Init(Some(tx))).await;
    assert!(rx.await.unwrap());
    assert!(!f.shared.ycable_presence.load(Ordering::SeqCst));
    assert!(f.db.row(DbId::State, MUX_CABLE_INFO, TEST_PORT).is_none());
    a.handle(Msg::PostInfo).await;
    assert_eq!(f.row(DbId::State, MUX_CABLE_INFO)["tor_active"], "active");

    // A cable without a usable driver, or absent, does not set it.
    for setup in [
        |c: &mut MockCable| c.load = None,
        |c: &mut MockCable| c.present = false,
    ] {
        let f = Fixture::new();
        f.cable(setup);
        let mut a = f.standby();
        let (tx, rx) = tokio::sync::oneshot::channel();
        a.handle(Msg::Init(Some(tx))).await;
        assert!(!rx.await.unwrap());
    }
}

/// test_read_y_cable_and_update_statedb_port_tbl_get_read_side_exception, _none, _negative
#[tokio::test]
async fn standby_init_read_side_unreadable() {
    for reply in [
        raise("OSError('EEPROM busy')"),
        value(PyVal::None),
        value(PyVal::Int(-1)),
        value(PyVal::Int(0)),
    ] {
        let f = Fixture::new();
        f.cable(|c| {
            c.script("get_read_side", reply.clone());
        });
        let mut a = f.standby();
        a.handle(Msg::Init(None)).await;
        assert_eq!(f.mux_state(), row3("unknown", "-1", "-1"), "{reply:?}");
    }
}

/// test_read_y_cable_and_update_statedb_port_tbl_get_mux_direction_exception, _status_unknown
#[tokio::test]
async fn standby_init_mux_direction_unreadable() {
    let cases = [
        (raise("OSError('I2C failure')"), row3("unknown", "1", "-1")),
        (value(PyVal::Int(0)), row3("unknown", "1", "0")),
        (value(PyVal::Int(7)), row3("unknown", "1", "-1")),
        (value(PyVal::None), row3("unknown", "1", "-1")),
    ];
    for (reply, want) in cases {
        let f = Fixture::new();
        f.cable(|c| {
            c.script_seq(
                "get_mux_direction",
                vec![reply.clone(), value(PyVal::Int(1))],
            );
        });
        let mut a = f.standby();
        a.handle(Msg::Init(None)).await;
        assert_eq!(f.mux_state(), want, "{reply:?}");
    }
}

/// A driver that reports itself not initialized is loaded again on the next
/// TRANSCEIVER_INFO update (test_is_initialized_y_cable*).
#[tokio::test]
async fn standby_uninitialized_driver_is_loaded_again() {
    let f = Fixture::new();
    f.cable(|c| c.load = Some(false));
    let mut a = f.standby();
    a.handle(Msg::Init(None)).await;
    assert!(!a.initialized);
    f.cable(|c| c.load = Some(true));
    a.handle(Msg::Transceiver(Event::Set {
        key: TEST_PORT.into(),
        fields: fm(&[("serial", "x")]),
    }))
    .await;
    assert_eq!(f.platform.loads(), vec![PHYSICAL, PHYSICAL]);
    assert!(a.initialized);
    // Initialized: the next update changes nothing and calls nothing.
    f.db.clear_ops();
    f.platform.clear_calls();
    a.handle(Msg::Transceiver(Event::Set {
        key: TEST_PORT.into(),
        fields: fm(&[("serial", "y")]),
    }))
    .await;
    assert_eq!(f.platform.loads().len(), 2);
    assert!(f.db.ops().is_empty());
    assert!(f.platform.calls(PHYSICAL).is_empty());
}

// --- Active-standby: removal and insertion ------------------------------------------

/// test_check_identifier_presence_and_delete_mux_table_entry,
/// test_change_ports_status_for_y_cable_change_event_sfp_removed
#[tokio::test]
async fn standby_removal_keeps_read_side() {
    let f = Fixture::new();
    let mut a = f.standby();
    a.handle(Msg::Init(None)).await;
    let info = f.row(DbId::State, MUX_CABLE_INFO);
    a.handle(Msg::Transceiver(Event::Del {
        key: TEST_PORT.into(),
    }))
    .await;
    assert_eq!(f.mux_state(), row3("unknown", "1", "-1"));
    assert!(!f.platform.is_loaded(PHYSICAL));
    assert_eq!(f.row(DbId::State, MUX_CABLE_INFO), info);
}

/// test_change_ports_status_for_y_cable_change_event (insertion after removal)
#[tokio::test]
async fn standby_insertion_after_removal_loads_driver_again() {
    let f = Fixture::new();
    let mut a = f.standby();
    a.handle(Msg::Init(None)).await;
    a.handle(Msg::Transceiver(Event::Del {
        key: TEST_PORT.into(),
    }))
    .await;
    f.cable(|c| c.read_side = 2);
    a.handle(Msg::Transceiver(Event::Set {
        key: TEST_PORT.into(),
        fields: fm(&[("type", "QSFP")]),
    }))
    .await;
    assert_eq!(f.platform.loads().len(), 2);
    assert_eq!(f.mux_state(), row3("standby", "2", "1"));
    assert_eq!(
        f.row(DbId::State, MUX_CABLE_STATIC_INFO)["read_side"],
        "tor2"
    );
}

/// A port without a HW_MUX_CABLE_TABLE row gets none on removal (known issue).
#[tokio::test]
async fn removal_without_row_writes_nothing() {
    let f = Fixture::new();
    let mut a = f.standby();
    a.handle(Msg::Transceiver(Event::Del {
        key: TEST_PORT.into(),
    }))
    .await;
    assert!(f.db.ops().is_empty());
}

// --- Active-standby: toggles ---------------------------------------------------------

async fn initialized_standby(f: &Fixture) -> PortActor {
    let mut a = f.standby();
    a.handle(Msg::Init(None)).await;
    f.platform.clear_calls();
    f.db.clear_ops();
    a
}

/// test_update_tor_active_side_1_active, _1_standby, _2_active, _2_standby,
/// test_y_cable_toggle_mux_torA_update_status_true, _torB_update_status_true
#[tokio::test]
async fn standby_toggle_matrix() {
    let cases = [
        (1, "active", "toggle_mux_to_tor_a", "1"),
        (1, "standby", "toggle_mux_to_tor_b", "2"),
        (2, "active", "toggle_mux_to_tor_b", "2"),
        (2, "standby", "toggle_mux_to_tor_a", "1"),
    ];
    for (read_side, state, method, active_side) in cases {
        let f = Fixture::new();
        f.cable(|c| c.read_side = read_side);
        let mut a = initialized_standby(&f).await;
        a.handle(Msg::Toggle(fm(&[("state", state)]))).await;
        assert_eq!(
            f.mux_state(),
            row3(state, &read_side.to_string(), active_side)
        );
        assert_eq!(f.platform.calls(PHYSICAL), vec![method.to_string()]);
        let m = f.row(DbId::State, MUX_METRICS_TABLE);
        assert!(m.contains_key(&format!("xcvrd_switch_{state}_start")));
        assert!(
            m[&format!("xcvrd_switch_{state}_start")] <= m[&format!("xcvrd_switch_{state}_end")]
        );
    }
}

/// test_y_cable_toggle_mux_torA_update_status_exception, _torB_..._exception,
/// test_update_tor_active_side (toggle returns False)
#[tokio::test]
async fn standby_toggle_failure_writes_unknown() {
    for reply in [
        value(PyVal::Bool(false)),
        raise("OSError('I2C failure')"),
        value(PyVal::Int(1)),
    ] {
        let f = Fixture::new();
        f.cable(|c| {
            c.script("toggle_mux_to_tor_b", reply.clone());
        });
        let mut a = initialized_standby(&f).await;
        a.handle(Msg::Toggle(fm(&[("state", "standby")]))).await;
        assert_eq!(f.mux_state(), row3("unknown", "1", "-1"), "{reply:?}");
        let m = f.row(DbId::State, MUX_METRICS_TABLE);
        assert!(
            m.contains_key("xcvrd_switch_unknown_start")
                && m.contains_key("xcvrd_switch_unknown_end")
        );
    }
}

/// test_update_tor_active_side_with_read_update: an unknown read side is read again.
#[tokio::test]
async fn standby_toggle_reads_side_again() {
    let f = Fixture::new();
    f.cable(|c| {
        c.script_seq(
            "get_read_side",
            vec![raise("OSError('busy')"), value(PyVal::Int(1))],
        );
    });
    let mut a = f.standby();
    a.handle(Msg::Init(None)).await;
    assert_eq!(f.mux_state(), row3("unknown", "-1", "-1"));
    f.platform.clear_calls();
    a.handle(Msg::Toggle(fm(&[("state", "standby")]))).await;
    assert_eq!(f.mux_state(), row3("standby", "1", "2"));
    assert_eq!(
        f.platform.calls(PHYSICAL),
        vec!["get_read_side", "toggle_mux_to_tor_b"]
    );
}

/// test_toggle_mux_tor_direction_and_update_read_side_*: still unreadable.
#[tokio::test]
async fn standby_toggle_read_side_still_unreadable() {
    for reply in [
        raise("OSError('busy')"),
        value(PyVal::Int(0)),
        value(PyVal::None),
    ] {
        let f = Fixture::new();
        f.cable(|c| {
            c.script("get_read_side", reply.clone());
        });
        let mut a = f.standby();
        a.handle(Msg::Init(None)).await;
        f.platform.clear_calls();
        a.handle(Msg::Toggle(fm(&[("state", "standby")]))).await;
        assert_eq!(f.mux_state(), row3("unknown", "-1", "-1"));
        assert_eq!(f.platform.calls(PHYSICAL), vec!["get_read_side"]);
    }
}

/// test_update_tor_active_side_no_cable_presence
#[tokio::test]
async fn standby_toggle_cable_gone() {
    let f = Fixture::new();
    let mut a = initialized_standby(&f).await;
    f.cable(|c| c.present = false);
    a.handle(Msg::Toggle(fm(&[("state", "standby")]))).await;
    assert_eq!(f.mux_state(), row3("unknown", "-1", "-1"));
    assert!(f.platform.calls(PHYSICAL).is_empty());
}

/// test_y_cable_toggle_mux_torA_no_port_instance, _torB_no_port_instance
#[tokio::test]
async fn standby_toggle_without_driver() {
    let f = Fixture::new();
    f.cable(|c| c.load = None);
    let mut a = f.standby();
    a.handle(Msg::Init(None)).await;
    a.handle(Msg::Toggle(fm(&[("state", "active")]))).await;
    assert_eq!(f.mux_state(), row3("unknown", "-1", "-1"));
    // A read side of 1 without a driver: the toggle fails, the read side stays.
    f.db.put(
        DbId::State,
        HW_MUX_CABLE_TABLE,
        TEST_PORT,
        &[("read_side", "1")],
    );
    a.handle(Msg::Toggle(fm(&[("state", "active")]))).await;
    assert_eq!(f.mux_state(), row3("unknown", "1", "-1"));
}

/// test_ycable_helper_table_worker: requests without state, with another state,
/// or for a port without HW_MUX_CABLE_TABLE row change nothing.
#[tokio::test]
async fn standby_toggle_ignores_other_requests() {
    let f = Fixture::new();
    let mut a = initialized_standby(&f).await;
    a.handle(Msg::Toggle(fm(&[("mode", "auto")]))).await;
    a.handle(Msg::Toggle(fm(&[("state", "init")]))).await;
    assert!(f.db.ops().is_empty());
    assert!(f.platform.calls(PHYSICAL).is_empty());
    let f2 = Fixture::new();
    let mut b = f2.standby();
    b.handle(Msg::Toggle(fm(&[("state", "active")]))).await;
    assert!(f2.db.ops().is_empty());
}

// --- Active-standby: probes ----------------------------------------------------------

/// test_update_appdb_port_mux_cable_response_table_active_side_is_read_side,
/// _active_side_not_read_side, test_ycable_helper_table_worker_probe
#[tokio::test]
async fn standby_probe_matrix() {
    for (read_side, mux, want) in [
        (1, 1, "active"),
        (1, 2, "standby"),
        (2, 2, "active"),
        (2, 1, "standby"),
    ] {
        let f = Fixture::new();
        f.cable(|c| c.read_side = read_side);
        let mut a = initialized_standby(&f).await;
        f.cable(|c| c.mux_direction = mux);
        a.handle(Msg::MuxProbe(fm(&[("command", "probe")]))).await;
        assert_eq!(
            f.row(DbId::Appl, MUX_CABLE_RESPONSE_TABLE)["response"],
            want
        );
        assert_eq!(f.platform.calls(PHYSICAL), vec!["get_mux_direction"]);
        assert!(f.db.writes(HW_MUX_CABLE_TABLE).is_empty());
    }
}

/// test_update_appdb_port_mux_cable_response_table_active_side_none,
/// _active_side_status_unknown, _no_presence_status_unknown, _port_instance_none
#[tokio::test]
async fn standby_probe_unknown_cases() {
    for reply in [
        raise("OSError"),
        value(PyVal::Int(-1)),
        value(PyVal::Int(0)),
        value(PyVal::None),
    ] {
        let f = Fixture::new();
        let mut a = initialized_standby(&f).await;
        f.cable(|c| {
            c.script("get_mux_direction", reply.clone());
        });
        a.handle(Msg::MuxProbe(fm(&[("command", "probe")]))).await;
        assert_eq!(
            f.row(DbId::Appl, MUX_CABLE_RESPONSE_TABLE)["response"],
            "unknown",
            "{reply:?}"
        );
    }
    let f = Fixture::new();
    let mut a = initialized_standby(&f).await;
    f.cable(|c| c.present = false);
    a.handle(Msg::MuxProbe(fm(&[("command", "probe")]))).await;
    assert_eq!(
        f.row(DbId::Appl, MUX_CABLE_RESPONSE_TABLE)["response"],
        "unknown"
    );
    let f = Fixture::new();
    f.cable(|c| c.load = None);
    let mut a = f.standby();
    a.handle(Msg::Init(None)).await;
    a.handle(Msg::MuxProbe(fm(&[("command", "probe")]))).await;
    assert_eq!(
        f.row(DbId::Appl, MUX_CABLE_RESPONSE_TABLE)["response"],
        "unknown"
    );
}

/// A read side of 0 (the NIC side) is not valid: unknown (known issue).
#[tokio::test]
async fn standby_read_side_nic_is_unknown() {
    let f = Fixture::new();
    f.cable(|c| c.read_side = 0);
    let mut a = f.standby();
    a.handle(Msg::Init(None)).await;
    assert_eq!(f.mux_state().0, "unknown");
    a.handle(Msg::MuxProbe(fm(&[("command", "probe")]))).await;
    assert_eq!(
        f.row(DbId::Appl, MUX_CABLE_RESPONSE_TABLE)["response"],
        "unknown"
    );
    a.handle(Msg::Toggle(fm(&[("state", "standby")]))).await;
    assert_eq!(f.mux_state().0, "unknown");
}

/// test_handle_ycable_active_standby_probe_notification: other commands are ignored.
#[tokio::test]
async fn standby_probe_ignores_other_commands() {
    let f = Fixture::new();
    let mut a = initialized_standby(&f).await;
    a.handle(Msg::MuxProbe(fm(&[("command", "reset")]))).await;
    a.handle(Msg::FwdProbe(fm(&[("command", "probe")]))).await;
    a.handle(Msg::PeerToggle(fm(&[("state", "active")]))).await;
    assert!(f.db.ops().is_empty());
}

// --- Active-standby: MUX_CABLE_INFO --------------------------------------------------

/// test_get_muxcable_info*, test_post_port_mux_info_to_db,
/// test_check_identifier_presence_and_update_mux_info_entry
#[tokio::test]
async fn standby_telemetry_full_info() {
    let _g = globals();
    let f = Fixture::new();
    let mut a = initialized_standby(&f).await;
    a.handle(Msg::Telemetry).await;
    let info = f.row(DbId::State, MUX_CABLE_INFO);
    let want = [
        ("tor_active", "active"),
        ("mux_direction", "self"),
        ("manual_switch_count", "3"),
        ("auto_switch_count", "5"),
        ("link_status_self", "up"),
        ("link_status_peer", "down"),
        ("link_status_nic", "up"),
        ("self_eye_height_lane1", "611"),
        ("self_eye_height_lane2", "612"),
        ("peer_eye_height_lane1", "621"),
        ("peer_eye_height_lane2", "622"),
        ("nic_eye_height_lane1", "601"),
        ("nic_eye_height_lane2", "602"),
        ("internal_temperature", "35"),
        ("internal_voltage", "3.3"),
        ("nic_temperature", "45"),
        ("nic_voltage", "5.1"),
        ("version_self_active", "1.1.0"),
        ("version_peer_inactive", "0.2.9"),
        ("version_nic_next", "1.0.0"),
    ];
    for (k, v) in want {
        assert_eq!(info[k], v, "{k}");
    }
    assert_eq!(info.len(), 27);
}

/// test_get_muxcable_info_exceptions: values the driver cannot read.
#[tokio::test]
async fn standby_telemetry_driver_failures() {
    let _g = globals();
    let f = Fixture::new();
    f.cable(|c| c.read_side = 2);
    let mut a = initialized_standby(&f).await;
    f.cable(|c| {
        c.script("get_eye_heights", raise("OSError"));
        c.script("get_local_temperature", value(PyVal::Int(-1)));
        c.script("get_nic_voltage", value(PyVal::str_("bad")));
        c.script("get_switch_count_total", raise("OSError"));
        c.script("get_active_linked_tor_side", value(PyVal::Int(-1)));
        c.script("is_link_active", raise("OSError"));
        c.script("get_firmware_version", value(PyVal::None));
        c.script("get_local_voltage", value(PyVal::Int(3)));
    });
    a.handle(Msg::Telemetry).await;
    let info = f.row(DbId::State, MUX_CABLE_INFO);
    for k in [
        "manual_switch_count",
        "auto_switch_count",
        "internal_temperature",
        "nic_voltage",
        "internal_voltage",
        "self_eye_height_lane1",
        "peer_eye_height_lane2",
        "nic_eye_height_lane1",
        "version_self_active",
        "version_nic_next",
    ] {
        assert_eq!(info[k], "N/A", "{k}");
    }
    assert_eq!(info["tor_active"], "unknown");
    assert_eq!(info["link_status_self"], "down");
    assert_eq!(info["mux_direction"], "peer");
    assert_eq!(info["nic_temperature"], "45");
}

/// test_get_firmware_dict (download in progress): the last known versions.
#[tokio::test]
async fn standby_telemetry_during_download() {
    let _g = globals();
    let f = Fixture::new();
    let mut a = initialized_standby(&f).await;
    // What a previous run left, without the peer's versions.
    f.db.remove(DbId::State, MUX_CABLE_INFO, TEST_PORT);
    f.db.put(
        DbId::State,
        MUX_CABLE_INFO,
        TEST_PORT,
        &[
            ("version_self_active", "9.9.0"),
            ("version_nic_next", "9.9.8"),
        ],
    );
    f.cable(|c| {
        c.attrs
            .insert("download_firmware_status".into(), PyVal::Int(1));
    });
    a.handle(Msg::Telemetry).await;
    let info = f.row(DbId::State, MUX_CABLE_INFO);
    assert_eq!(info["version_self_active"], "9.9.0");
    assert_eq!(info["version_nic_next"], "9.9.8");
    assert_eq!(info["version_peer_active"], "None");
    assert!(!f
        .platform
        .calls(PHYSICAL)
        .contains(&"get_firmware_version".to_string()));
}

/// test_get_muxcable_info_without_presence, test_post_port_mux_info_to_db (no presence)
#[tokio::test]
async fn standby_telemetry_absent_cable_placeholder() {
    let _g = globals();
    let f = Fixture::new();
    let mut a = initialized_standby(&f).await;
    f.cable(|c| c.present = false);
    a.handle(Msg::Telemetry).await;
    let info = f.row(DbId::State, MUX_CABLE_INFO);
    assert_eq!(info["tor_active"], "unknown");
    assert_eq!(info["manual_switch_count"], "N/A");
}

/// test_get_muxcable_info (no port instance): nothing written.
#[tokio::test]
async fn standby_telemetry_without_driver_writes_nothing() {
    let _g = globals();
    let f = Fixture::new();
    f.cable(|c| c.load = None);
    let mut a = f.standby();
    a.handle(Msg::Init(None)).await;
    f.db.clear_ops();
    a.handle(Msg::Telemetry).await;
    assert!(f.db.ops().is_empty());
}

/// test_handle_ycable_enable_disable_tel_notification: disabled telemetry writes nothing.
#[tokio::test]
async fn telemetry_disabled_writes_nothing() {
    let _g = globals();
    let f = Fixture::new();
    let mut a = initialized_standby(&f).await;
    config::set_telemetry_disabled(true);
    a.handle(Msg::Telemetry).await;
    config::set_telemetry_disabled(false);
    assert!(f.db.ops().is_empty());
}

/// test_get_muxcable_static_info*: cursor values that cannot be read.
#[tokio::test]
async fn standby_static_info_failures() {
    let f = Fixture::new();
    f.cable(|c| {
        c.script("get_target_cursor_values", raise("OSError('I2C failure')"));
    });
    let mut a = f.standby();
    a.handle(Msg::Init(None)).await;
    let st = f.row(DbId::State, MUX_CABLE_STATIC_INFO);
    assert_eq!(st["read_side"], "tor1");
    assert!(st
        .iter()
        .filter(|(k, _)| *k != "read_side")
        .all(|(_, v)| v == "N/A"));
    assert_eq!(st.len(), 31);
}

// --- Active-active ---------------------------------------------------------------------

/// test_check_identifier_presence_and_setup_channel*, test_put_init_values_for_grpc_states*
#[tokio::test]
async fn active_init_from_soc() {
    for (read_side, own, peer) in [(0, "active", "standby"), (1, "standby", "active")] {
        let f = Fixture::with(false, read_side);
        let mut a = f.active();
        a.handle(Msg::Init(None)).await;
        let rs = read_side.to_string();
        assert_eq!(f.mux_state(), row3(own, &rs, own));
        let p = f.row(DbId::State, HW_MUX_CABLE_TABLE_PEER);
        assert_eq!(
            (p["state"].as_str(), p["active_side"].as_str()),
            (peer, peer)
        );
        assert_eq!(
            f.soc.requests("QueryAdminForwardingPortState"),
            vec![(vec![0, 1], vec![false, false])]
        );
        // Placeholder MUX_CABLE_INFO, no static info.
        assert_eq!(f.row(DbId::State, MUX_CABLE_INFO)["tor_active"], "unknown");
        assert!(f
            .db
            .row(DbId::State, MUX_CABLE_STATIC_INFO, TEST_PORT)
            .is_none());
    }
}

/// test_put_init_values_for_grpc_states (stub None, RPC failure)
#[tokio::test]
async fn active_init_failures() {
    let f = Fixture::new();
    f.soc.fail(
        "QueryAdminForwardingPortState",
        RpcError::Status(tonic::Code::Unavailable),
    );
    let mut a = f.active();
    a.handle(Msg::Init(None)).await;
    assert_eq!(f.mux_state(), row3("unknown", "0", "unknown"));
    // No channel at all: unknown, no RPC.
    let f = Fixture::new();
    *f.connector.soc.lock().unwrap() = None;
    let mut a = f.active();
    a.handle(Msg::Init(None)).await;
    assert_eq!(f.mux_state(), row3("unknown", "0", "unknown"));
    assert_eq!(
        f.row(DbId::State, HW_MUX_CABLE_TABLE_PEER)["state"],
        "unknown"
    );
}

/// test_check_identifier_presence_and_setup_channel (cable absent)
#[tokio::test]
async fn active_init_absent_cable() {
    let f = Fixture::new();
    let mut a = f.active();
    f.db.remove(DbId::State, TRANSCEIVER_INFO, TEST_PORT);
    a.handle(Msg::Init(None)).await;
    assert_eq!(f.mux_state(), row3("unknown", "0", "unknown"));
    assert!(f.soc.names().is_empty());
    assert_eq!(f.connector.connects(), 0);
    // The first toggle sets the channel up.
    a.handle(Msg::Toggle(fm(&[("state", "standby")]))).await;
    assert_eq!(f.mux_state(), row3("standby", "0", "standby"));
}

/// test_get_soc_ip_for_grpc*, test_check_identifier_presence_and_setup_channel (no SoC IP)
#[tokio::test]
async fn active_init_without_soc_address() {
    for fields in [
        vec![("state", "auto"), ("cable_type", "active-active")],
        vec![
            ("state", "auto"),
            ("cable_type", "active-active"),
            ("soc_ipv4", "10.1.1/32"),
        ],
    ] {
        let f = Fixture::new();
        f.db.put(DbId::Config, MUX_CABLE, TEST_PORT, &fields);
        let mut a = PortActor::new(
            TEST_PORT,
            Some(PHYSICAL),
            Some(CableType::ActiveActive),
            Arc::clone(&f.shared),
        );
        a.handle(Msg::Init(None)).await;
        assert!(f.db.ops().is_empty());
        a.handle(Msg::Toggle(fm(&[("state", "standby")]))).await;
        a.handle(Msg::FwdProbe(fm(&[("command", "probe")]))).await;
        a.handle(Msg::Transceiver(Event::Del {
            key: TEST_PORT.into(),
        }))
        .await;
        assert!(f.db.ops().is_empty());
        // Python sets y_cable_presence only for a port with a SoC address.
        assert!(!f.shared.ycable_presence.load(Ordering::SeqCst));
    }
}

/// test_check_identifier_presence_and_setup_channel: an active-active port
/// with a SoC address sets y_cable_presence, cable present or not, so an
/// active-standby port initialized after it writes no MUX_CABLE_INFO.
#[tokio::test]
async fn active_init_sets_presence() {
    for present in [true, false] {
        let f = Fixture::new();
        let mut a = if present {
            f.active()
        } else {
            let row = [
                ("state", "auto"),
                ("cable_type", "active-active"),
                ("soc_ipv4", "192.168.0.7/32"),
            ];
            f.db.put(DbId::Config, MUX_CABLE, TEST_PORT, &row);
            PortActor::new(
                TEST_PORT,
                Some(PHYSICAL),
                Some(CableType::ActiveActive),
                Arc::clone(&f.shared),
            )
        };
        a.handle(Msg::Init(None)).await;
        assert_eq!(a.present, present);
        assert!(
            f.shared.ycable_presence.load(Ordering::SeqCst),
            "present={present}"
        );
    }
    // At startup it only reports it (see standby_startup_init_reports_presence).
    let f = Fixture::new();
    let mut a = f.active();
    let (tx, rx) = tokio::sync::oneshot::channel();
    a.handle(Msg::Init(Some(tx))).await;
    assert!(rx.await.unwrap());
    assert!(!f.shared.ycable_presence.load(Ordering::SeqCst));
}

/// test_soc_ip_for_grpc (IPv6): the SoC address of soc_ipv6.
#[tokio::test]
async fn active_init_ipv6_soc() {
    let f = Fixture::new();
    f.db.put(
        DbId::Config,
        MUX_CABLE,
        TEST_PORT,
        &[
            ("state", "auto"),
            ("cable_type", "active-active"),
            ("soc_ipv6", "fd00::7/128"),
        ],
    );
    f.db.put(DbId::State, TRANSCEIVER_INFO, TEST_PORT, &[("type", "x")]);
    let mut a = PortActor::new(
        TEST_PORT,
        Some(PHYSICAL),
        Some(CableType::ActiveActive),
        Arc::clone(&f.shared),
    );
    a.handle(Msg::Init(None)).await;
    assert_eq!(
        f.connector.connects.lock().unwrap()[0].1,
        "fd00::7".parse::<std::net::IpAddr>().unwrap()
    );
}

/// test_handle_hw_mux_cable_table_grpc_notification*: self and peer toggles.
#[tokio::test]
async fn active_toggle_matrix() {
    for read_side in [0, 1] {
        for state in ["active", "standby"] {
            for peer in [false, true] {
                let f = Fixture::with(false, read_side);
                let mut a = f.active();
                a.handle(Msg::Init(None)).await;
                let msg = if peer {
                    Msg::PeerToggle(fm(&[("state", state)]))
                } else {
                    Msg::Toggle(fm(&[("state", state)]))
                };
                a.handle(msg).await;
                let table = if peer {
                    HW_MUX_CABLE_TABLE_PEER
                } else {
                    HW_MUX_CABLE_TABLE
                };
                let row = f.row(DbId::State, table);
                assert_eq!(
                    (row["state"].as_str(), row["active_side"].as_str()),
                    (state, state)
                );
                let portid = if peer { 1 - read_side } else { read_side };
                assert_eq!(
                    f.soc.requests("SetAdminForwardingPortState"),
                    vec![(vec![portid], vec![state == "active"])]
                );
                let side = if peer { "peer" } else { "self" };
                assert!(f
                    .row(DbId::State, MUX_METRICS_TABLE)
                    .contains_key(&format!("xcvrd_switch_{side}_{state}_end")));
            }
        }
    }
}

/// test_parse_grpc_response_hw_mux_cable_change_state*: failures write unknown.
#[tokio::test]
async fn active_toggle_failures() {
    let errors = [
        RpcError::Status(tonic::Code::Unavailable),
        RpcError::Status(tonic::Code::Internal),
        RpcError::Timeout,
    ];
    for e in errors {
        let f = Fixture::new();
        let mut a = f.active();
        a.handle(Msg::Init(None)).await;
        f.soc.fail("SetAdminForwardingPortState", e.clone());
        a.handle(Msg::Toggle(fm(&[("state", "standby")]))).await;
        assert_eq!(f.mux_state(), row3("unknown", "0", "unknown"), "{e:?}");
        assert!(f
            .row(DbId::State, MUX_METRICS_TABLE)
            .contains_key("xcvrd_switch_self_unknown_start"));
    }
    for reply in [
        AdminReply {
            portid: vec![7],
            state: vec![true],
        },
        AdminReply::default(),
    ] {
        let f = Fixture::new();
        let mut a = f.active();
        a.handle(Msg::Init(None)).await;
        f.soc.reply("SetAdminForwardingPortState", reply);
        a.handle(Msg::Toggle(fm(&[("state", "standby")]))).await;
        assert_eq!(f.mux_state(), row3("unknown", "0", "unknown"));
    }
}

/// Without a read side, neither side is toggled (known issue).
#[tokio::test]
async fn active_toggle_without_read_side() {
    let f = Fixture::with(false, -1);
    let mut a = f.active();
    a.handle(Msg::Init(None)).await;
    a.handle(Msg::Toggle(fm(&[("state", "standby")]))).await;
    a.handle(Msg::PeerToggle(fm(&[("state", "standby")]))).await;
    assert_eq!(f.mux_state(), row3("unknown", "-1", "unknown"));
    assert!(f.soc.requests("SetAdminForwardingPortState").is_empty());
}

/// test_retry_setup_grpc_channel_for_port*: a toggle sets the channel up again.
#[tokio::test]
async fn active_toggle_retries_channel() {
    let f = Fixture::new();
    *f.connector.soc.lock().unwrap() = None;
    let mut a = f.active();
    a.handle(Msg::Init(None)).await;
    a.handle(Msg::Toggle(fm(&[("state", "standby")]))).await;
    assert_eq!(f.mux_state(), row3("unknown", "0", "unknown"));
    assert_eq!(f.connector.connects(), 2);
    *f.connector.soc.lock().unwrap() = Some(Arc::clone(&f.soc));
    a.handle(Msg::Toggle(fm(&[("state", "standby")]))).await;
    assert_eq!(f.mux_state(), row3("standby", "0", "standby"));
}

/// test_handle_fwd_state_command_grpc_notification*, test_parse_grpc_response_forwarding_state*
#[tokio::test]
async fn active_forwarding_probe() {
    for (read_side, own, peer) in [(0, "active", "standby"), (1, "standby", "active")] {
        let f = Fixture::with(false, read_side);
        let mut a = f.active();
        a.handle(Msg::Init(None)).await;
        f.db.clear_ops();
        a.handle(Msg::FwdProbe(fm(&[("command", "probe")]))).await;
        let r = f.row(DbId::Appl, FORWARDING_STATE_RESPONSE);
        assert_eq!(
            (r["response"].as_str(), r["response_peer"].as_str()),
            (own, peer)
        );
        assert!(f.db.writes(HW_MUX_CABLE_TABLE).is_empty());
    }
    let f = Fixture::new();
    let mut a = f.active();
    a.handle(Msg::Init(None)).await;
    f.soc
        .fail("QueryAdminForwardingPortState", RpcError::Timeout);
    a.handle(Msg::FwdProbe(fm(&[("command", "probe")]))).await;
    assert_eq!(
        f.row(DbId::Appl, FORWARDING_STATE_RESPONSE),
        fm(&[("response", "unknown"), ("response_peer", "unknown")])
    );
    f.db.clear_ops();
    a.handle(Msg::FwdProbe(fm(&[("command", "reset")]))).await;
    assert!(f.db.ops().is_empty());
}

/// test_handle_fwd_state_command_grpc_notification (stub None, retry fails)
#[tokio::test]
async fn active_forwarding_probe_without_channel() {
    let f = Fixture::new();
    *f.connector.soc.lock().unwrap() = None;
    let mut a = f.active();
    a.handle(Msg::Init(None)).await;
    a.handle(Msg::FwdProbe(fm(&[("command", "probe")]))).await;
    assert_eq!(
        f.row(DbId::Appl, FORWARDING_STATE_RESPONSE),
        fm(&[("response", "unknown"), ("response_peer", "unknown")])
    );
}

/// test_get_muxcable_info_for_active_active*, test_parse_grpc_response_link_and_oper_state*
#[tokio::test]
async fn active_telemetry() {
    let _g = globals();
    let f = Fixture::new();
    *f.soc.link.lock().unwrap() = vec![true, false];
    *f.soc.oper.lock().unwrap() = vec![false, true];
    let mut a = f.active();
    a.handle(Msg::Init(None)).await;
    a.handle(Msg::FwdProbe(fm(&[("command", "probe")]))).await;
    a.handle(Msg::FwdProbe(fm(&[("command", "probe")]))).await;
    let before = f.soc.names().len();
    a.handle(Msg::Telemetry).await;
    let info = f.row(DbId::State, MUX_CABLE_INFO);
    let want = [
        ("self_link_state", "up"),
        ("peer_link_state", "down"),
        ("self_oper_state", "down"),
        ("peer_oper_state", "up"),
        ("self_mux_direction", "active"),
        ("peer_mux_direction", "standby"),
        ("server_version", "bb-soc-1.0"),
        ("grpc_connection_status", "READY"),
        ("mux_direction_probe_count", "3"),
        ("peer_mux_direction_probe_count", "3"),
        ("link_state_probe_count", "unknown"),
        ("operation_state_probe_count", "unknown"),
    ];
    for (k, v) in want {
        assert_eq!(info[k], v, "{k}");
    }
    assert_eq!(
        f.soc.names()[before..],
        [
            "QueryLinkState",
            "QueryOperationPortState",
            "QueryAdminForwardingPortState",
            "QueryServerVersion"
        ]
    );
    // The placeholder fields from startup stay.
    assert_eq!(info["tor_active"], "unknown");
    // The next pass reports this pass's queries.
    a.handle(Msg::Telemetry).await;
    assert_eq!(
        f.row(DbId::State, MUX_CABLE_INFO)["link_state_probe_count"],
        "1"
    );
}

/// test_get_muxcable_info_for_active_active (no stub): unknowns, no RPCs.
#[tokio::test]
async fn active_telemetry_without_channel() {
    let _g = globals();
    let f = Fixture::new();
    *f.connector.soc.lock().unwrap() = None;
    let mut a = f.active();
    a.handle(Msg::Init(None)).await;
    a.handle(Msg::Telemetry).await;
    let info = f.row(DbId::State, MUX_CABLE_INFO);
    assert_eq!(info["server_version"], "N/A");
    assert_eq!(info["grpc_connection_status"], "unknown");
    assert_eq!(info["self_link_state"], "unknown");
}

/// An active-active port without HW_MUX_CABLE_TABLE row: telemetry writes
/// nothing and does not fail (known issue).
#[tokio::test]
async fn active_telemetry_without_row() {
    let _g = globals();
    let f = Fixture::new();
    let mut a = f.active();
    a.handle(Msg::Telemetry).await;
    assert!(f.db.ops().is_empty());
}

/// Removal keeps the channel; a re-inserted cable is queried again (known issue).
#[tokio::test]
async fn active_removal_and_reinsertion() {
    let f = Fixture::new();
    let mut a = f.active();
    a.handle(Msg::Init(None)).await;
    a.handle(Msg::Transceiver(Event::Del {
        key: TEST_PORT.into(),
    }))
    .await;
    assert_eq!(f.mux_state(), row3("unknown", "0", "-1"));
    a.handle(Msg::Toggle(fm(&[("state", "standby")]))).await;
    assert_eq!(f.mux_state(), row3("standby", "0", "standby"));
    assert_eq!(f.connector.connects(), 1);
    a.handle(Msg::Transceiver(Event::Set {
        key: TEST_PORT.into(),
        fields: fm(&[("type", "x")]),
    }))
    .await;
    assert_eq!(f.soc.requests("QueryAdminForwardingPortState").len(), 2);
    // Present already: another update is ignored.
    a.handle(Msg::Transceiver(Event::Set {
        key: TEST_PORT.into(),
        fields: fm(&[("type", "y")]),
    }))
    .await;
    assert_eq!(f.soc.requests("QueryAdminForwardingPortState").len(), 2);
}

/// test_change_ports_status_for_y_cable_change_event with read side -1: a
/// read side unknown at startup is looked up again in Loopback3 when an
/// active-active cable is initialized.
#[tokio::test]
async fn active_read_side_looked_up_again() {
    let f = Fixture::with(false, -1);
    let mut a = f.active();
    a.handle(Msg::Init(None)).await;
    assert_eq!(f.mux_state().1, "-1");
    // Loopback3 is configured later; the cable is plugged in again.
    f.db.put(
        DbId::Config,
        LOOPBACK_INTERFACE,
        "Loopback3|10.212.64.1/32",
        &[("NULL", "NULL")],
    );
    a.handle(Msg::Transceiver(Event::Del {
        key: TEST_PORT.into(),
    }))
    .await;
    a.handle(Msg::Transceiver(Event::Set {
        key: TEST_PORT.into(),
        fields: fm(&[("type", "QSFP28 or later")]),
    }))
    .await;
    assert_eq!(f.mux_state().1, "1");
    assert_eq!(f.shared.read_side.load(Ordering::SeqCst), 1);
}

/// test_change_ports_status_for_y_cable_change_event: the cable type is read
/// again on each insertion. A port now active-active drops its driver and
/// sets up a channel; back to active-standby, it loads a driver again.
#[tokio::test]
async fn cable_type_read_again_on_insertion() {
    let f = Fixture::new();
    let mut a = f.standby();
    a.handle(Msg::Init(None)).await;
    assert!(f.platform.is_loaded(PHYSICAL));
    let remove = || {
        Msg::Transceiver(Event::Del {
            key: TEST_PORT.into(),
        })
    };
    let insert = || {
        Msg::Transceiver(Event::Set {
            key: TEST_PORT.into(),
            fields: fm(&[("type", "QSFP28 or later")]),
        })
    };
    a.handle(remove()).await;
    f.db.put(
        DbId::Config,
        MUX_CABLE,
        TEST_PORT,
        &[
            ("cable_type", "active-active"),
            ("soc_ipv4", "192.168.0.7/32"),
        ],
    );
    f.db.put(
        DbId::State,
        TRANSCEIVER_INFO,
        TEST_PORT,
        &[("type", "QSFP28 or later")],
    );
    a.handle(insert()).await;
    assert_eq!(a.cable, Some(CableType::ActiveActive));
    assert!(!f.platform.is_loaded(PHYSICAL));
    assert!(a.soc.is_some());
    assert_eq!(f.connector.connects(), 1);

    a.handle(remove()).await;
    f.db.remove(DbId::Config, MUX_CABLE, TEST_PORT);
    f.db.put(DbId::Config, MUX_CABLE, TEST_PORT, &[("state", "auto")]);
    a.handle(insert()).await;
    assert_eq!(a.cable, Some(CableType::ActiveStandby));
    assert!(a.soc.is_none());
    assert!(f.platform.is_loaded(PHYSICAL));
    assert!(a.initialized);
    assert_eq!(f.mux_state().0, "active");
}

/// VS: active-active cables are always present (test_y_cable_wrapper_get_presence on VS).
#[tokio::test]
async fn active_on_vs_is_present() {
    let f = Fixture::with(true, 0);
    let mut a = f.active();
    f.db.remove(DbId::State, TRANSCEIVER_INFO, TEST_PORT);
    a.handle(Msg::Init(None)).await;
    assert_eq!(f.mux_state(), row3("active", "0", "active"));
}

// --- Mailbox order -------------------------------------------------------------------

/// Requests for a port run one at a time, in order (HLD 6.1 mailbox ordering).
#[tokio::test]
async fn mailbox_keeps_order() {
    let f = Fixture::new();
    let a = f.standby();
    let tx = a.spawn();
    tx.send(Msg::Init(None)).unwrap();
    tx.send(Msg::Toggle(fm(&[("state", "standby")]))).unwrap();
    tx.send(Msg::Transceiver(Event::Del {
        key: TEST_PORT.into(),
    }))
    .unwrap();
    let (done_tx, done_rx) = tokio::sync::oneshot::channel();
    tx.send(Msg::Init(Some(done_tx))).unwrap();
    done_rx.await.unwrap();
    let states: Vec<String> =
        f.db.ops()
            .into_iter()
            .filter_map(|op| match op {
                MemOp::Set { table, fields, .. } if table == HW_MUX_CABLE_TABLE => fields
                    .into_iter()
                    .find(|(k, _)| k == "state")
                    .map(|(_, v)| v),
                _ => None,
            })
            .collect();
    assert_eq!(states, vec!["active", "standby", "unknown", "standby"]);
    let _ = PyArg::None;
}

/// A port whose cable is slow keeps every request, in order, however many
/// wait; a growing backlog is logged, and nothing is dropped.
#[tokio::test(start_paused = true)]
async fn slow_port_keeps_every_request() {
    let f = Fixture::new();
    f.cable(|c| {
        for m in ["toggle_mux_to_tor_a", "toggle_mux_to_tor_b"] {
            c.delays
                .insert(m.into(), std::time::Duration::from_millis(100));
        }
    });
    let tx = f.standby().spawn();
    tx.send(Msg::Init(None)).unwrap();
    for i in 0..300 {
        let state = if i % 2 == 0 { "standby" } else { "active" };
        tx.send(Msg::Toggle(fm(&[("state", state)]))).unwrap();
    }
    let (done_tx, done_rx) = tokio::sync::oneshot::channel();
    tx.send(Msg::Init(Some(done_tx))).unwrap();
    done_rx.await.unwrap();
    let toggles: Vec<String> = f
        .platform
        .calls(PHYSICAL)
        .into_iter()
        .filter(|m| m.starts_with("toggle_mux_to_tor"))
        .collect();
    assert_eq!(toggles.len(), 300);
    for (i, m) in toggles.iter().enumerate() {
        let want = if i % 2 == 0 {
            "toggle_mux_to_tor_b"
        } else {
            "toggle_mux_to_tor_a"
        };
        assert_eq!(m, want, "toggle {i}");
    }
    assert_eq!(f.mux_state().0, "active");
    assert!(!crate::logger::capture::find("requests are waiting for port Ethernet0").is_empty());
}

// --- Telemetry beside requests -------------------------------------------------------

fn fast_telemetry(f: &Fixture, period_ms: u64) -> Arc<super::Shared> {
    Arc::new(super::Shared {
        db: f.shared.db.clone(),
        platform: f.shared.platform.clone(),
        soc: f.shared.soc.clone(),
        is_vs: false,
        read_side: std::sync::atomic::AtomicI32::new(0),
        telemetry_period: std::time::Duration::from_millis(period_ms),
        bus: f.shared.bus.clone(),
        aa_telemetry: f.shared.aa_telemetry.clone(),
        ycable_presence: f.shared.ycable_presence.clone(),
    })
}

async fn wait_for(cond: impl Fn() -> bool, ms: u64) -> bool {
    let deadline = std::time::Instant::now() + std::time::Duration::from_millis(ms);
    while std::time::Instant::now() < deadline {
        if cond() {
            return true;
        }
        tokio::time::sleep(std::time::Duration::from_millis(5)).await;
    }
    cond()
}

/// A toggle waits for at most one telemetry call to the cable, not for the
/// whole pass, and the calls never overlap (HLD 4.3.3, Python issue 26).
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn toggle_overtakes_telemetry() {
    let _g = globals();
    let f = Fixture::new();
    f.cable(|c| {
        c.delays.insert(
            "get_eye_heights".into(),
            std::time::Duration::from_millis(300),
        );
    });
    f.db.put(DbId::Config, MUX_CABLE, TEST_PORT, &[("state", "auto")]);
    let a = PortActor::new(
        TEST_PORT,
        Some(PHYSICAL),
        Some(CableType::ActiveStandby),
        fast_telemetry(&f, 200),
    );
    let tx = a.spawn();
    let (done, wait) = tokio::sync::oneshot::channel();
    tx.send(Msg::Init(Some(done))).unwrap();
    wait.await.unwrap();
    // Init reads the eye heights too; wait for the telemetry pass's first one.
    f.platform.clear_calls();
    assert!(
        wait_for(
            || f.platform
                .calls(PHYSICAL)
                .iter()
                .any(|m| m == "get_eye_heights"),
            3000
        )
        .await
    );
    let t0 = std::time::Instant::now();
    tx.send(Msg::Toggle(fm(&[("state", "standby")]))).unwrap();
    assert!(wait_for(|| f.mux_state().0 == "standby", 3000).await);
    let took = t0.elapsed();
    // A whole pass takes 900 ms here (three eye height calls).
    assert!(took < std::time::Duration::from_millis(450), "{took:?}");
    assert_eq!(*f.platform.overlaps.lock().unwrap(), 0);
}

/// A pass that overlapped a removal is not written: its values may be stale.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn telemetry_overlapping_removal_is_discarded() {
    let _g = globals();
    let f = Fixture::new();
    f.cable(|c| {
        c.delays.insert(
            "get_eye_heights".into(),
            std::time::Duration::from_millis(200),
        );
    });
    f.db.put(DbId::Config, MUX_CABLE, TEST_PORT, &[("state", "auto")]);
    let a = PortActor::new(
        TEST_PORT,
        Some(PHYSICAL),
        Some(CableType::ActiveStandby),
        fast_telemetry(&f, 300),
    );
    let tx = a.spawn();
    let (done, wait) = tokio::sync::oneshot::channel();
    tx.send(Msg::Init(Some(done))).unwrap();
    wait.await.unwrap();
    f.platform.clear_calls();
    assert!(
        wait_for(
            || f.platform
                .calls(PHYSICAL)
                .iter()
                .any(|m| m == "get_eye_heights"),
            3000
        )
        .await
    );
    let infos_before = f.db.writes(MUX_CABLE_INFO).len();
    tx.send(Msg::Transceiver(Event::Del {
        key: TEST_PORT.into(),
    }))
    .unwrap();
    tokio::time::sleep(std::time::Duration::from_millis(250)).await;
    // The pass in progress ends without writing; later passes see the absent
    // driver (the cable is present to the platform here): nothing written.
    assert_eq!(f.db.writes(MUX_CABLE_INFO).len(), infos_before);
}

/// A cable removed and inserted again while a telemetry pass is calling it:
/// the old driver is unloaded and the new one loaded between the pass's
/// calls, so no two calls to the cable overlap.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn reinsertion_waits_for_the_telemetry_call() {
    let _g = globals();
    let f = Fixture::new();
    f.cable(|c| {
        c.delays.insert(
            "get_eye_heights".into(),
            std::time::Duration::from_millis(300),
        );
        c.delays
            .insert("load".into(), std::time::Duration::from_millis(50));
    });
    f.db.put(DbId::Config, MUX_CABLE, TEST_PORT, &[("state", "auto")]);
    let a = PortActor::new(
        TEST_PORT,
        Some(PHYSICAL),
        Some(CableType::ActiveStandby),
        fast_telemetry(&f, 200),
    );
    let tx = a.spawn();
    let (done, wait) = tokio::sync::oneshot::channel();
    tx.send(Msg::Init(Some(done))).unwrap();
    wait.await.unwrap();
    f.platform.clear_calls();
    assert!(
        wait_for(
            || f.platform
                .calls(PHYSICAL)
                .iter()
                .any(|m| m == "get_eye_heights"),
            3000
        )
        .await
    );
    let loads = f.platform.loads().len();
    tx.send(Msg::Transceiver(Event::Del {
        key: TEST_PORT.into(),
    }))
    .unwrap();
    tx.send(Msg::Transceiver(Event::Set {
        key: TEST_PORT.into(),
        fields: fm(&[("type", "QSFP28 or later")]),
    }))
    .unwrap();
    assert!(
        wait_for(
            || f.platform.loads().len() > loads && f.mux_state().0 == "active",
            3000
        )
        .await
    );
    assert_eq!(*f.platform.overlaps.lock().unwrap(), 0);
}

/// A port stuck in a request holds up only its own telemetry: the passes of
/// the other active-standby ports, which share the bus with it, go on.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stuck_port_holds_up_only_its_own_telemetry() {
    let _g = globals();
    let f = Fixture::new();
    f.platform.add(2, MockCable::default());
    f.db.put(DbId::Config, MUX_CABLE, TEST_PORT, &[("state", "auto")]);
    f.db.put(DbId::Config, MUX_CABLE, "Ethernet4", &[("state", "auto")]);
    let shared = fast_telemetry(&f, 200);
    let a = PortActor::new(
        TEST_PORT,
        Some(PHYSICAL),
        Some(CableType::ActiveStandby),
        Arc::clone(&shared),
    );
    let b = PortActor::new("Ethernet4", Some(2), Some(CableType::ActiveStandby), shared);
    let (a_tx, b_tx) = (a.spawn(), b.spawn());
    for tx in [&a_tx, &b_tx] {
        let (done, wait) = tokio::sync::oneshot::channel();
        tx.send(Msg::Init(Some(done))).unwrap();
        wait.await.unwrap();
    }
    // Catch A's pass in a call to the cable, then give the call lock to a
    // toggle that does not return for a long time.
    f.cable(|c| {
        c.delays.insert(
            "get_eye_heights".into(),
            std::time::Duration::from_millis(300),
        );
        c.delays.insert(
            "toggle_mux_to_tor_b".into(),
            std::time::Duration::from_secs(30),
        );
    });
    f.platform.clear_calls();
    assert!(
        wait_for(
            || f.platform
                .calls(PHYSICAL)
                .iter()
                .any(|m| m == "get_eye_heights"),
            3000
        )
        .await
    );
    a_tx.send(Msg::Toggle(fm(&[("state", "standby")]))).unwrap();
    assert!(
        wait_for(
            || f.platform
                .calls(PHYSICAL)
                .iter()
                .any(|m| m == "toggle_mux_to_tor_b"),
            3000
        )
        .await
    );
    // A's pass waits for A; B's passes keep writing MUX_CABLE_INFO.
    let b_writes = || {
        f.db.writes(MUX_CABLE_INFO)
            .iter()
            .filter(|(k, _)| k == "Ethernet4")
            .count()
    };
    let before = b_writes();
    assert!(wait_for(|| b_writes() >= before + 2, 3000).await);
}
