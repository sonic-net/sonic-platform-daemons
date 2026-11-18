//! Tests of the subscription routing (Python's task workers:
//! test_ycable_helper_table_worker*, test_ycable_helper_cli_worker*,
//! test_ycable_state_update_task*).

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use tokio::sync::{mpsc, oneshot};

use crate::actor::Msg;
use crate::config::CableType;
use crate::daemon::{
    first_info_port, message_for, mux_tables, wait_for_inits, PendingInit, INIT_WAIT,
};
use crate::db::DbId;
use crate::schema::*;
use crate::testing::fm;

fn pending(
    port: &str,
    cable: CableType,
) -> (
    PendingInit,
    oneshot::Sender<bool>,
    mpsc::UnboundedReceiver<Msg>,
) {
    let (tx, rx) = mpsc::unbounded_channel();
    let (done_tx, done) = oneshot::channel();
    let p = PendingInit {
        port: port.into(),
        cable,
        tx,
        done,
    };
    (p, done_tx, rx)
}

/// test_init_ports_status_for_y_cable: startup waits for the ports'
/// initialization; the first port that sets y_cable_presence, in port order,
/// then writes MUX_CABLE_INFO.
#[tokio::test(start_paused = true)]
async fn startup_waits_for_initialization() {
    let (a, a_done, mut a_rx) = pending("Ethernet0", CableType::ActiveStandby);
    let (b, b_done, mut b_rx) = pending("Ethernet4", CableType::ActiveStandby);
    // Ethernet0's driver could not be loaded; Ethernet4's could.
    b_done.send(true).unwrap();
    a_done.send(false).unwrap();
    let presence = Arc::new(AtomicBool::new(false));
    assert!(wait_for_inits(vec![a, b], INIT_WAIT, Arc::clone(&presence)).await);
    assert!(presence.load(Ordering::SeqCst));
    assert!(a_rx.try_recv().is_err());
    assert!(matches!(b_rx.try_recv(), Ok(Msg::PostInfo)));
}

/// A port whose initialization does not end holds up only itself: startup
/// goes on after INIT_WAIT, and the MUX_CABLE_INFO decision follows the
/// port's initialization when it ends.
#[tokio::test(start_paused = true)]
async fn startup_does_not_wait_for_a_hung_port() {
    let (a, a_done, mut a_rx) = pending("Ethernet0", CableType::ActiveStandby);
    let presence = Arc::new(AtomicBool::new(false));
    let t0 = tokio::time::Instant::now();
    assert!(!wait_for_inits(vec![a], INIT_WAIT, Arc::clone(&presence)).await);
    assert!(t0.elapsed() >= INIT_WAIT);
    assert!(!presence.load(Ordering::SeqCst));
    a_done.send(true).unwrap();
    let msg = tokio::time::timeout(Duration::from_secs(1), a_rx.recv())
        .await
        .unwrap();
    assert!(matches!(msg, Some(Msg::PostInfo)));
    assert!(presence.load(Ordering::SeqCst));
}

/// A port inserted while startup waited set y_cable_presence first: no other
/// port writes MUX_CABLE_INFO.
#[tokio::test(start_paused = true)]
async fn presence_set_meanwhile_posts_no_info() {
    let (a, a_done, mut a_rx) = pending("Ethernet0", CableType::ActiveStandby);
    a_done.send(true).unwrap();
    let presence = Arc::new(AtomicBool::new(true));
    assert!(wait_for_inits(vec![a], INIT_WAIT, presence).await);
    assert!(a_rx.try_recv().is_err());
}

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
