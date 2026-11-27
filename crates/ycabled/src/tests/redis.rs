//! Tests of the redis layer against a private redis-server: the row
//! conventions and swsscommon's SubscriberStateTable semantics.

use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::time::Duration;

use tokio::time::timeout;

use crate::db::{Db, DbConfig, DbId, Event, RedisDb, TableEvent};

/// Set, a missing redis-server fails the tests that need one rather than
/// skipping them. The pipeline sets it.
const REQUIRE_REDIS: &str = "YCABLED_REQUIRE_REDIS";

/// A redis-server on a free port, with keyspace notifications, killed on drop.
pub struct TestRedis {
    child: Child,
    pub port: u16,
    pub config: DbConfig,
}

/// A TCP port that is free now.
pub fn free_port() -> u16 {
    let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    l.local_addr().unwrap().port()
}

/// database_config.json of the databases ycabled uses, on a redis at `port`.
pub fn db_config_json(port: u16) -> String {
    format!(
        r#"{{"INSTANCES": {{"redis": {{"hostname": "127.0.0.1", "port": {port}}}}},
            "DATABASES": {{"APPL_DB": {{"id": 0, "separator": ":", "instance": "redis"}},
                           "CONFIG_DB": {{"id": 4, "separator": "|", "instance": "redis"}},
                           "STATE_DB": {{"id": 6, "separator": "|", "instance": "redis"}}}}}}"#
    )
}

impl TestRedis {
    /// None, after a note, when redis-server is not installed and
    /// [`REQUIRE_REDIS`] is not set: the package build runs the workspace's
    /// tests in sonic-slave, which has no redis-server.
    pub async fn start() -> Option<TestRedis> {
        TestRedis::start_on(free_port()).await
    }

    /// [`TestRedis::start`], on `port`.
    pub async fn start_on(port: u16) -> Option<TestRedis> {
        let child = Command::new("redis-server")
            .args([
                "--port",
                &port.to_string(),
                "--bind",
                "127.0.0.1",
                "--save",
                "",
                "--appendonly",
                "no",
                "--databases",
                "16",
                "--notify-keyspace-events",
                "AKE",
            ])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn();
        let child = match child {
            Ok(child) => child,
            Err(e)
                if e.kind() == std::io::ErrorKind::NotFound
                    && std::env::var_os(REQUIRE_REDIS).is_none() =>
            {
                eprintln!("redis-server is not installed: test skipped ({REQUIRE_REDIS} unset)");
                return None;
            }
            Err(e) => panic!("redis-server: {e}"),
        };
        let config: DbConfig = serde_json::from_str(&db_config_json(port)).unwrap();
        for _ in 0..100 {
            if std::net::TcpStream::connect(("127.0.0.1", port)).is_ok() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        Some(TestRedis {
            child,
            port,
            config,
        })
    }

    pub async fn db(&self) -> Arc<RedisDb> {
        Arc::new(RedisDb::connect("", &self.config).await.unwrap())
    }

    pub fn client(&self, db: i64) -> redis::Connection {
        redis::Client::open(format!("redis://127.0.0.1:{}/{}", self.port, db))
            .unwrap()
            .get_connection()
            .unwrap()
    }
}

impl Drop for TestRedis {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

async fn next(rx: &mut tokio::sync::mpsc::UnboundedReceiver<TableEvent>) -> TableEvent {
    timeout(Duration::from_secs(5), rx.recv())
        .await
        .expect("event")
        .expect("open")
}

fn hset(c: &mut redis::Connection, key: &str, field: &str, value: &str) {
    let _: () = redis::cmd("HSET")
        .arg(key)
        .arg(field)
        .arg(value)
        .query(c)
        .unwrap();
}

#[tokio::test]
async fn rows_use_sonic_keys_and_separators() {
    let Some(r) = TestRedis::start().await else {
        return;
    };
    let db = r.db().await;
    db.set(
        DbId::State,
        "HW_MUX_CABLE_TABLE",
        "Ethernet0",
        &[("state", "active".into()), ("read_side", "1".into())],
    )
    .await;
    db.set(
        DbId::Appl,
        "MUX_CABLE_RESPONSE_TABLE",
        "Ethernet0",
        &[("response", "standby".into())],
    )
    .await;
    let mut c6 = r.client(6);
    let raw: std::collections::HashMap<String, String> = redis::cmd("HGETALL")
        .arg("HW_MUX_CABLE_TABLE|Ethernet0")
        .query(&mut c6)
        .unwrap();
    assert_eq!(raw.len(), 2);
    let mut c0 = r.client(0);
    let resp: String = redis::cmd("HGET")
        .arg("MUX_CABLE_RESPONSE_TABLE:Ethernet0")
        .arg("response")
        .query(&mut c0)
        .unwrap();
    assert_eq!(resp, "standby");
    // set merges fields, as swsscommon's Table::set does.
    db.set(
        DbId::State,
        "HW_MUX_CABLE_TABLE",
        "Ethernet0",
        &[("active_side", "1".into())],
    )
    .await;
    assert_eq!(
        db.get(DbId::State, "HW_MUX_CABLE_TABLE", "Ethernet0")
            .await
            .unwrap()
            .len(),
        3
    );
    assert_eq!(
        db.hget(DbId::State, "HW_MUX_CABLE_TABLE", "Ethernet0", "state")
            .await
            .as_deref(),
        Some("active")
    );
    assert_eq!(
        db.keys(DbId::State, "HW_MUX_CABLE_TABLE").await,
        vec!["Ethernet0"]
    );
    assert!(
        db.exists(DbId::Appl, "MUX_CABLE_RESPONSE_TABLE", "Ethernet0")
            .await
    );
    db.del(DbId::Appl, "MUX_CABLE_RESPONSE_TABLE", "Ethernet0")
        .await;
    assert!(db
        .get(DbId::Appl, "MUX_CABLE_RESPONSE_TABLE", "Ethernet0")
        .await
        .is_none());
    assert!(db
        .get(DbId::Config, "MUX_CABLE", "Ethernet0")
        .await
        .is_none());
}

#[tokio::test]
async fn subscription_replays_rows_then_follows_changes() {
    let Some(r) = TestRedis::start().await else {
        return;
    };
    let db = r.db().await;
    db.set(
        DbId::Appl,
        "HW_MUX_CABLE_TABLE",
        "Ethernet0",
        &[("state", "standby".into())],
    )
    .await;
    let (_subscriptions, mut rx) = db
        .subscribe(
            &[
                (DbId::Appl, "HW_MUX_CABLE_TABLE".into()),
                (DbId::State, "TRANSCEIVER_INFO".into()),
            ],
            &[],
            || {},
        )
        .await
        .unwrap();
    // The existing row first.
    match next(&mut rx).await {
        (t, Event::Set { key, fields }) => assert_eq!(
            (t.as_str(), key.as_str(), fields["state"].as_str()),
            ("HW_MUX_CABLE_TABLE", "Ethernet0", "standby")
        ),
        e => panic!("{e:?}"),
    }
    // Then changes: the whole row, read again.
    let mut c0 = r.client(0);
    let _: () = redis::cmd("HSET")
        .arg("HW_MUX_CABLE_TABLE:Ethernet4")
        .arg("state")
        .arg("active")
        .arg("x")
        .arg("1")
        .query(&mut c0)
        .unwrap();
    match next(&mut rx).await {
        (t, Event::Set { key, fields }) => {
            assert_eq!(
                (t.as_str(), key.as_str()),
                ("HW_MUX_CABLE_TABLE", "Ethernet4")
            );
            assert_eq!(fields.len(), 2);
        }
        e => panic!("{e:?}"),
    }
    let mut c6 = r.client(6);
    hset(&mut c6, "TRANSCEIVER_INFO|Ethernet8", "type", "QSFP");
    assert!(
        matches!(next(&mut rx).await, (t, Event::Set { key, .. }) if t == "TRANSCEIVER_INFO" && key == "Ethernet8")
    );
    let _: () = redis::cmd("DEL")
        .arg("TRANSCEIVER_INFO|Ethernet8")
        .query(&mut c6)
        .unwrap();
    assert_eq!(
        next(&mut rx).await,
        (
            "TRANSCEIVER_INFO".into(),
            Event::Del {
                key: "Ethernet8".into()
            }
        )
    );
    // Written and deleted before it is read: the update is skipped, the deletion delivered.
    let _: () = redis::pipe()
        .atomic()
        .cmd("HSET")
        .arg("TRANSCEIVER_INFO|Ethernet12")
        .arg("type")
        .arg("QSFP")
        .cmd("DEL")
        .arg("TRANSCEIVER_INFO|Ethernet12")
        .query(&mut c6)
        .unwrap();
    assert_eq!(
        next(&mut rx).await,
        (
            "TRANSCEIVER_INFO".into(),
            Event::Del {
                key: "Ethernet12".into()
            }
        )
    );
    // Other tables and other keys of the same prefix are not delivered.
    hset(&mut c6, "HW_MUX_CABLE_TABLE_PEER|Ethernet0", "state", "x");
    assert!(timeout(Duration::from_millis(300), rx.recv())
        .await
        .is_err());
}

/// The events of all tables come out in the order redis delivered them, so
/// a port's requests keep their order across tables (HLD 4.3.3).
#[tokio::test]
async fn events_keep_their_order_across_tables() {
    let Some(r) = TestRedis::start().await else {
        return;
    };
    let db = r.db().await;
    let tables = [
        "TRANSCEIVER_INFO",
        "HW_MUX_CABLE_TABLE",
        "MUX_CABLE_COMMAND_TABLE",
    ];
    let (_subscriptions, mut rx) = db
        .subscribe(
            &[
                (DbId::State, tables[0].into()),
                (DbId::Appl, tables[1].into()),
                (DbId::Appl, tables[2].into()),
            ],
            &[],
            || {},
        )
        .await
        .unwrap();
    let mut c0 = r.client(0);
    let mut c6 = r.client(6);
    for i in 0..20 {
        let v = i.to_string();
        hset(&mut c6, "TRANSCEIVER_INFO|Ethernet0", "type", &v);
        hset(&mut c0, "HW_MUX_CABLE_TABLE:Ethernet0", "state", &v);
        hset(&mut c0, "MUX_CABLE_COMMAND_TABLE:Ethernet0", "command", &v);
    }
    let mut got = Vec::new();
    for _ in 0..60 {
        got.push(next(&mut rx).await.0);
    }
    let want: Vec<String> = (0..20)
        .flat_map(|_| tables.iter().map(|t| t.to_string()))
        .collect();
    assert_eq!(got, want);
}

/// A table added later: its rows, then its changes, in order with the other
/// tables' events, on the same connection.
#[tokio::test]
async fn added_tables_replay_rows_then_follow_changes() {
    let Some(r) = TestRedis::start().await else {
        return;
    };
    let db = r.db().await;
    db.set(
        DbId::Appl,
        "HW_MUX_CABLE_TABLE",
        "Ethernet4",
        &[("state", "active".into())],
    )
    .await;
    let (subscriptions, mut rx) = db
        .subscribe(&[(DbId::State, "TRANSCEIVER_INFO".into())], &[], || {})
        .await
        .unwrap();
    assert!(timeout(Duration::from_millis(300), rx.recv())
        .await
        .is_err());
    subscriptions.add(vec![(DbId::Appl, "HW_MUX_CABLE_TABLE".into())]);
    match next(&mut rx).await {
        (t, Event::Set { key, .. }) => assert_eq!(
            (t.as_str(), key.as_str()),
            ("HW_MUX_CABLE_TABLE", "Ethernet4")
        ),
        e => panic!("{e:?}"),
    }
    let mut c0 = r.client(0);
    let mut c6 = r.client(6);
    hset(&mut c0, "HW_MUX_CABLE_TABLE:Ethernet8", "state", "standby");
    hset(&mut c6, "TRANSCEIVER_INFO|Ethernet8", "type", "QSFP");
    assert_eq!(next(&mut rx).await.0, "HW_MUX_CABLE_TABLE");
    assert_eq!(next(&mut rx).await.0, "TRANSCEIVER_INFO");
    // One subscriber connection, with both patterns.
    let clients: String = redis::cmd("CLIENT").arg("LIST").query(&mut c0).unwrap();
    let subscribers: Vec<&str> = clients
        .lines()
        .filter(|l| !l.contains(" psub=0 "))
        .collect();
    assert_eq!(subscribers.len(), 1, "{clients}");
    assert!(subscribers[0].contains(" psub=2 "), "{clients}");
}

/// A changes-only table gets no rows, when it is subscribed to at first or
/// added later, only its changes; the other tables still get theirs.
#[tokio::test]
async fn changes_only_tables_get_no_rows() {
    let Some(r) = TestRedis::start().await else {
        return;
    };
    let db = r.db().await;
    for table in [
        "HW_FORWARDING_STATE_PEER",
        "HW_MUX_CABLE_TABLE",
        "FORWARDING_STATE_COMMAND",
        "MUX_CABLE_COMMAND_TABLE",
    ] {
        db.set(
            DbId::Appl,
            table,
            "Ethernet0",
            &[("state", "standby".into())],
        )
        .await;
    }
    let (subscriptions, mut rx) = db
        .subscribe(
            &[
                (DbId::Appl, "HW_FORWARDING_STATE_PEER".into()),
                (DbId::Appl, "HW_MUX_CABLE_TABLE".into()),
            ],
            &["HW_FORWARDING_STATE_PEER", "FORWARDING_STATE_COMMAND"],
            || {},
        )
        .await
        .unwrap();
    assert_eq!(next(&mut rx).await.0, "HW_MUX_CABLE_TABLE");
    // The other table's row comes once both added tables are subscribed to.
    subscriptions.add(vec![
        (DbId::Appl, "FORWARDING_STATE_COMMAND".into()),
        (DbId::Appl, "MUX_CABLE_COMMAND_TABLE".into()),
    ]);
    assert_eq!(next(&mut rx).await.0, "MUX_CABLE_COMMAND_TABLE");
    let mut c0 = r.client(0);
    hset(
        &mut c0,
        "HW_FORWARDING_STATE_PEER:Ethernet0",
        "state",
        "active",
    );
    hset(
        &mut c0,
        "FORWARDING_STATE_COMMAND:Ethernet0",
        "command",
        "probe",
    );
    match next(&mut rx).await {
        (t, Event::Set { key, fields }) => assert_eq!(
            (t.as_str(), key.as_str(), fields["state"].as_str()),
            ("HW_FORWARDING_STATE_PEER", "Ethernet0", "active")
        ),
        e => panic!("{e:?}"),
    }
    assert_eq!(next(&mut rx).await.0, "FORWARDING_STATE_COMMAND");
    assert!(timeout(Duration::from_millis(300), rx.recv())
        .await
        .is_err());
}

/// Losing the subscription connection runs `on_lost`, which makes ycabled-rs
/// exit for supervisord to start it again.
#[tokio::test]
async fn losing_the_connection_runs_on_lost() {
    let Some(r) = TestRedis::start().await else {
        return;
    };
    let db = r.db().await;
    let (lost_tx, lost_rx) = tokio::sync::oneshot::channel();
    let (_subscriptions, _rx) = db
        .subscribe(
            &[(DbId::State, "TRANSCEIVER_INFO".into())],
            &[],
            move || {
                let _ = lost_tx.send(());
            },
        )
        .await
        .unwrap();
    drop(r);
    timeout(Duration::from_secs(10), lost_rx)
        .await
        .expect("on_lost")
        .unwrap();
}

/// A changed row that cannot be read ends the subscriptions, as a lost
/// connection does: its change would be lost, and a restart replays the
/// rows. Here the key is not a hash.
#[tokio::test]
async fn unreadable_row_ends_the_subscription() {
    let Some(r) = TestRedis::start().await else {
        return;
    };
    let db = r.db().await;
    let (lost_tx, lost_rx) = tokio::sync::oneshot::channel();
    let (_subscriptions, mut rx) = db
        .subscribe(
            &[(DbId::Appl, "HW_MUX_CABLE_TABLE".into())],
            &[],
            move || {
                let _ = lost_tx.send(());
            },
        )
        .await
        .unwrap();
    let mut c0 = r.client(0);
    let _: () = redis::cmd("SET")
        .arg("HW_MUX_CABLE_TABLE:Ethernet0")
        .arg("active")
        .query(&mut c0)
        .unwrap();
    timeout(Duration::from_secs(5), lost_rx)
        .await
        .expect("on_lost")
        .unwrap();
    assert!(rx.recv().await.is_none());
}

/// A row that cannot be read when a subscription replays the rows fails
/// the subscription: its request would not be notified again. Here the key
/// is not a hash.
#[tokio::test]
async fn unreadable_row_fails_the_replay() {
    let Some(r) = TestRedis::start().await else {
        return;
    };
    let db = r.db().await;
    let mut c0 = r.client(0);
    let _: () = redis::cmd("SET")
        .arg("HW_MUX_CABLE_TABLE:Ethernet0")
        .arg("active")
        .query(&mut c0)
        .unwrap();
    let res = db
        .subscribe(&[(DbId::Appl, "HW_MUX_CABLE_TABLE".into())], &[], || {})
        .await;
    assert!(res.is_err());
}

/// del_if deletes a row only while it holds exactly the given fields, in
/// one script: a changed row, one with a field more or less, and a missing
/// row are kept or left alone.
#[tokio::test]
async fn del_if_deletes_only_the_given_row() {
    let Some(r) = TestRedis::start().await else {
        return;
    };
    let db = r.db().await;
    let t = "XCVRD_CONFIG_PRBS_CMD";
    let row = |pairs: &[(&str, &str)]| -> crate::db::FieldMap {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    };
    db.set(
        DbId::Appl,
        t,
        "Ethernet0",
        &[("config_prbs", "anlt".into())],
    )
    .await;
    for other in [
        row(&[("config_prbs", "fec")]),
        row(&[]),
        row(&[("config_prbs", "anlt"), ("extra", "1")]),
    ] {
        assert!(!db.del_if(DbId::Appl, t, "Ethernet0", &other).await);
    }
    assert!(db.get(DbId::Appl, t, "Ethernet0").await.is_some());
    db.set(DbId::Appl, t, "Ethernet0", &[("extra", "1".into())])
        .await;
    assert!(
        !db.del_if(DbId::Appl, t, "Ethernet0", &row(&[("config_prbs", "anlt")]))
            .await
    );
    let both = row(&[("config_prbs", "anlt"), ("extra", "1")]);
    assert!(db.del_if(DbId::Appl, t, "Ethernet0", &both).await);
    assert!(db.get(DbId::Appl, t, "Ethernet0").await.is_none());
    assert!(!db.del_if(DbId::Appl, t, "Ethernet0", &both).await);
}

/// redis may still be coming up at startup: the connection is tried again
/// every second until it answers (HLD 4.3.1).
#[tokio::test]
async fn connect_waits_for_redis() {
    let port = free_port();
    let config: DbConfig = serde_json::from_str(&db_config_json(port)).unwrap();
    let connect = tokio::spawn(async move { RedisDb::connect_waiting("", &config).await });
    tokio::time::sleep(Duration::from_millis(1500)).await;
    assert!(!connect.is_finished());
    let Some(r) = TestRedis::start_on(port).await else {
        connect.abort();
        return;
    };
    let db = timeout(Duration::from_secs(10), connect)
        .await
        .expect("connected")
        .unwrap()
        .unwrap();
    db.set(
        DbId::Appl,
        "HW_MUX_CABLE_TABLE",
        "Ethernet0",
        &[("state", "active".into())],
    )
    .await;
    let mut c0 = r.client(0);
    let state: String = redis::cmd("HGET")
        .arg("HW_MUX_CABLE_TABLE:Ethernet0")
        .arg("state")
        .query(&mut c0)
        .unwrap();
    assert_eq!(state, "active");
}

#[tokio::test]
async fn waits_for_config_db_initialized() {
    let Some(r) = TestRedis::start().await else {
        return;
    };
    let db = r.db().await;
    let waiter = {
        let db = Arc::clone(&db);
        tokio::spawn(async move { db.wait_for_config_init().await })
    };
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert!(!waiter.is_finished());
    let mut c4 = r.client(4);
    let _: () = redis::cmd("SET")
        .arg("CONFIG_DB_INITIALIZED")
        .arg("1")
        .query(&mut c4)
        .unwrap();
    timeout(Duration::from_secs(5), waiter)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    // Already set: no wait.
    timeout(Duration::from_secs(1), db.wait_for_config_init())
        .await
        .unwrap()
        .unwrap();
}
