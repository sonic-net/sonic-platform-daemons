//! Tests of the subscription routing (Python's task workers:
//! test_ycable_helper_table_worker*, test_ycable_helper_cli_worker*,
//! test_ycable_state_update_task*).

use crate::actor::Msg;
use crate::config::CableType;
use crate::daemon::{first_info_port, message_for, mux_tables};
use crate::db::DbId;
use crate::schema::*;
use crate::testing::fm;

/// Python's y_cable_presence at startup (init_ports_status_for_y_cable,
/// test_check_identifier_presence_and_update_mux_table_entry*): the first
/// port that sets it writes MUX_CABLE_INFO, if it is active-standby.
#[test]
fn first_initialized_port_writes_mux_cable_info() {
    use CableType::*;
    assert_eq!(first_info_port(&[]), (None, false));
    assert_eq!(
        first_info_port(&[(ActiveStandby, true), (ActiveStandby, true)]),
        (Some(0), true)
    );
    // The first port's driver could not be loaded: the next port's turn.
    assert_eq!(
        first_info_port(&[(ActiveStandby, false), (ActiveStandby, true)]),
        (Some(1), true)
    );
    // An active-active port before it sets the flag: no port writes it.
    assert_eq!(
        first_info_port(&[(ActiveActive, true), (ActiveStandby, true)]),
        (None, true)
    );
    // One without a SoC address does not.
    assert_eq!(
        first_info_port(&[(ActiveActive, false), (ActiveStandby, true)]),
        (Some(1), true)
    );
    assert_eq!(
        first_info_port(&[(ActiveStandby, false), (ActiveActive, false)]),
        (None, false)
    );
}

#[test]
fn subscribes_to_what_python_subscribes_to() {
    let t = mux_tables();
    assert_eq!(t.len(), 19);
    for table in [
        HW_MUX_CABLE_TABLE,
        MUX_CABLE_COMMAND_TABLE,
        FORWARDING_STATE_COMMAND,
        HW_FORWARDING_STATE_PEER,
        XCVRD_DOWN_FW_STATUS_CMD,
    ] {
        assert!(t.contains(&(DbId::Appl, table.to_string())), "{table}");
    }
    for cli in CLI_COMMAND_TABLES {
        assert!(t.contains(&(DbId::Appl, cli.to_string())), "{cli}");
    }
    assert!(t.contains(&(DbId::Config, XCVRD_LOG.to_string())));
}

#[test]
fn rows_become_actor_messages() {
    let f = fm(&[("state", "active")]);
    assert!(
        matches!(message_for(HW_MUX_CABLE_TABLE, "Ethernet0", f.clone()), Msg::Toggle(x) if x == f)
    );
    assert!(matches!(
        message_for(HW_FORWARDING_STATE_PEER, "Ethernet0", f.clone()),
        Msg::PeerToggle(_)
    ));
    assert!(matches!(
        message_for(MUX_CABLE_COMMAND_TABLE, "Ethernet0", f.clone()),
        Msg::MuxProbe(_)
    ));
    assert!(matches!(
        message_for(FORWARDING_STATE_COMMAND, "Ethernet0", f.clone()),
        Msg::FwdProbe(_)
    ));
    for cli in CLI_COMMAND_TABLES {
        assert!(matches!(message_for(cli, "Ethernet0", f.clone()), Msg::Cli(t, _) if t == cli));
    }
}
