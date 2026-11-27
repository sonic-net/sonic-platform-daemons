//! Tests of the subscription routing (Python's task workers:
//! test_ycable_helper_table_worker*, test_ycable_helper_cli_worker*,
//! test_ycable_state_update_task*).

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use tokio::sync::{mpsc, oneshot};

use super::redis::{db_config_json, TestRedis};
use crate::actor::{Global, Msg, Shared};
use crate::bridge::YCablePlatform;
use crate::config::CableType;
use crate::daemon::{
    first_info_port, message_for, mux_tables, run, wait_for_inits, Daemon, Namespace, Options,
    PendingInit, INIT_WAIT,
};
use crate::db::{Db, DbId, RedisDb};
use crate::grpc::SocConnector;
use crate::ports::PortMap;
use crate::schema::*;
use crate::testing::{fm, MockCable, MockConnector, MockPlatform, MockSoc};

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

/// The answer to the last probe of `port`, waited for up to 5 s.
async fn probe_response(db: &RedisDb, port: &str) -> Option<String> {
    for _ in 0..250 {
        if let Some(r) = db
            .hget(DbId::Appl, MUX_CABLE_RESPONSE_TABLE, port, "response")
            .await
        {
            return Some(r);
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    None
}

/// Two ports configured after startup arrive while the mux tables are being
/// subscribed to: Ethernet0's insertion starts the subscription, which
/// replays a probe of Ethernet4 written before it took effect, and
/// Ethernet4's insertion comes after that. The replayed probe initializes
/// Ethernet4 and is answered, rather than dropped as a request for a port
/// without an actor, and the insertion does not initialize the port again.
#[tokio::test]
async fn request_replayed_ahead_of_its_port_insertion_is_answered() {
    let Some(r) = TestRedis::start().await else {
        return;
    };
    let db = r.db().await;
    let platform = MockPlatform::new();
    platform.add(1, MockCable::default());
    platform.add(2, MockCable::default());
    let shared = Shared::new(
        db.clone() as Arc<dyn Db>,
        platform.clone() as Arc<dyn YCablePlatform>,
        MockConnector::new(None) as Arc<dyn SocConnector>,
        false,
        1,
        &Global::default(),
    );
    let ns = Namespace::new(String::new(), 0, Arc::clone(&db), shared);
    let mut ports = PortMap::default();
    ports.insert("Ethernet0", 1, 0);
    ports.insert("Ethernet4", 2, 0);
    let daemon = Arc::new(Daemon {
        namespaces: vec![Arc::clone(&ns)],
        ports,
    });
    // No mux ports at startup: only TRANSCEIVER_INFO is subscribed to.
    let events = daemon.subscribe(&ns, false).await.unwrap();
    daemon.start_routing(&ns, events);
    for port in ["Ethernet0", "Ethernet4"] {
        db.set(DbId::Config, MUX_CABLE, port, &[("state", "auto".into())])
            .await;
    }
    let probe = [("command", "probe".to_string())];
    db.set(DbId::Appl, MUX_CABLE_COMMAND_TABLE, "Ethernet4", &probe)
        .await;
    let transceiver = [("type", "QSFP28 or later".to_string())];
    db.set(DbId::State, TRANSCEIVER_INFO, "Ethernet0", &transceiver)
        .await;
    assert_eq!(
        probe_response(&db, "Ethernet4").await.as_deref(),
        Some("active")
    );

    db.set(DbId::State, TRANSCEIVER_INFO, "Ethernet4", &transceiver)
        .await;
    // A probe after the insertion is answered after it.
    db.del(DbId::Appl, MUX_CABLE_RESPONSE_TABLE, "Ethernet4")
        .await;
    db.set(DbId::Appl, MUX_CABLE_COMMAND_TABLE, "Ethernet4", &probe)
        .await;
    assert_eq!(
        probe_response(&db, "Ethernet4").await.as_deref(),
        Some("active")
    );
    assert_eq!(
        platform.loads().iter().filter(|&&p| p == 2).count(),
        1,
        "{:?}",
        platform.loads()
    );
}

/// A peer toggle that is in HW_FORWARDING_STATE_PEER when the mux tables are
/// subscribed to is not acted on: linkmgrd never clears the row, so it may be
/// stale. A peer toggle written afterwards is. Ethernet0 becomes an
/// active-active port of the lower ToR after startup, which subscribes to the
/// mux tables; a replayed forwarding state probe shows when they are.
#[tokio::test]
async fn peer_toggle_from_before_the_subscription_is_not_acted_on() {
    let Some(r) = TestRedis::start().await else {
        return;
    };
    let db = r.db().await;
    let soc = MockSoc::new();
    let shared = Shared::new(
        db.clone() as Arc<dyn Db>,
        MockPlatform::new() as Arc<dyn YCablePlatform>,
        MockConnector::new(Some(Arc::clone(&soc))) as Arc<dyn SocConnector>,
        false,
        0,
        &Global::default(),
    );
    let ns = Namespace::new(String::new(), 0, Arc::clone(&db), shared);
    let mut ports = PortMap::default();
    ports.insert("Ethernet0", 1, 0);
    let daemon = Arc::new(Daemon {
        namespaces: vec![Arc::clone(&ns)],
        ports,
    });
    let events = daemon.subscribe(&ns, false).await.unwrap();
    daemon.start_routing(&ns, events);
    let mux_cable = [
        ("state", "auto".to_string()),
        ("cable_type", "active-active".to_string()),
        ("soc_ipv4", "192.168.0.3/32".to_string()),
    ];
    db.set(DbId::Config, MUX_CABLE, "Ethernet0", &mux_cable)
        .await;
    let standby = [("state", "standby".to_string())];
    db.set(DbId::Appl, HW_FORWARDING_STATE_PEER, "Ethernet0", &standby)
        .await;
    let probe = [("command", "probe".to_string())];
    db.set(DbId::Appl, FORWARDING_STATE_COMMAND, "Ethernet0", &probe)
        .await;
    let transceiver = [("type", "QSFP28 or later".to_string())];
    db.set(DbId::State, TRANSCEIVER_INFO, "Ethernet0", &transceiver)
        .await;
    let mut answered = false;
    for _ in 0..250 {
        answered = db
            .hget(
                DbId::Appl,
                FORWARDING_STATE_RESPONSE,
                "Ethernet0",
                "response",
            )
            .await
            .is_some();
        if answered {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert!(answered, "the replayed probe was not answered");

    // The port's requests are handled in order: a replayed peer toggle would
    // have reached the SoC before this one.
    let active = [("state", "active".to_string())];
    db.set(DbId::Appl, HW_FORWARDING_STATE_PEER, "Ethernet0", &active)
        .await;
    let sets = || soc.requests("SetAdminForwardingPortState");
    for _ in 0..250 {
        if !sets().is_empty() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert_eq!(sets(), vec![(vec![1], vec![true])]);
}

/// SIGHUP is ignored from the start, as in Python: here while startup waits
/// for CONFIG_DB_INITIALIZED, which is never set. Its default action would
/// end the process, the test's too.
#[tokio::test]
async fn sighup_during_startup_is_ignored() {
    let Some(r) = TestRedis::start().await else {
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("database_config.json");
    std::fs::write(&path, db_config_json(r.port)).unwrap();
    let startup = tokio::spawn(run(Options {
        db_config: path.to_string_lossy().into_owned(),
        db_global: String::new(),
        secrets: String::new(),
    }));
    // The wait for CONFIG_DB_INITIALIZED holds a pattern subscription.
    let mut c4 = r.client(4);
    let mut waiting = false;
    for _ in 0..250 {
        let patterns: i64 = redis::cmd("PUBSUB").arg("NUMPAT").query(&mut c4).unwrap();
        if patterns > 0 {
            waiting = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert!(waiting, "startup did not reach the CONFIG_DB wait");
    // SAFETY: sends SIGHUP to this process.
    unsafe {
        libc::kill(libc::getpid(), libc::SIGHUP);
    }
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert!(!startup.is_finished());
    startup.abort();
}
