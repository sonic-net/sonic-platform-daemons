use std::collections::HashMap;
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::json;
use swss_common::{ConsumerStateTable, CxxString, DbConnector, KeyOperation, SelectResult, Table};
use xcvrd_rs::env;
use xcvrd_rs::xcvrd_utilities::port_event_helper::{
    get_port_mapping, run_port_config_done_gate, MultiPortChangeObserver,
    PortConfigChangeSubscriber, PortConfigGate,
};
use xcvrd_rs::xcvrd_utilities::xcvr_table_helper::{XcvrTableHelper, GEARBOX_TABLE};

struct RedisFixture {
    root: PathBuf,
    servers: Vec<Child>,
}

impl RedisFixture {
    fn new() -> Self {
        assert!(
            std::env::var_os("REDIS_SOCK").is_none(),
            "unset REDIS_SOCK for isolated tests"
        );
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root =
            std::env::temp_dir().join(format!("xcvrd-redis-{}-{unique}", std::process::id()));
        fs::create_dir(&root).unwrap();
        let mut fixture = Self {
            root,
            servers: Vec::new(),
        };
        let mut includes = Vec::new();
        for namespace in ["", "asic1", "asic3"] {
            let label = if namespace.is_empty() {
                "default"
            } else {
                namespace
            };
            let dir = fixture.root.join(label);
            fs::create_dir(&dir).unwrap();
            let socket = fixture.socket(namespace);
            fixture.servers.push(
                Command::new("redis-server")
                    .args([
                        "--port",
                        "0",
                        "--save",
                        "",
                        "--appendonly",
                        "no",
                        "--notify-keyspace-events",
                        "KEA",
                        "--unixsocket",
                    ])
                    .arg(&socket)
                    .arg("--dir")
                    .arg(&dir)
                    .stdout(Stdio::null())
                    .stderr(Stdio::inherit())
                    .spawn()
                    .expect("redis-server must be installed"),
            );
            let mut ready = false;
            for _ in 0..100 {
                if socket.exists() {
                    if let Ok(db) = DbConnector::new_unix(0, socket.to_str().unwrap(), 100) {
                        ready = db.exists("fixture-ready").is_ok();
                    }
                }
                if ready {
                    break;
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            assert!(ready, "Redis did not become ready in {namespace:?}");
            let config = format!("{label}.json");
            fs::write(
                fixture.root.join(&config),
                json!({
                    "INSTANCES": {
                        "redis": {"hostname": "127.0.0.1", "port": 0, "unix_socket_path": socket}
                    },
                    "DATABASES": {
                        "APPL_DB": {"id": 1, "separator": ":", "instance": "redis"},
                        "CONFIG_DB": {"id": 2, "separator": "|", "instance": "redis"},
                        "STATE_DB": {"id": 3, "separator": "|", "instance": "redis"}
                    }
                })
                .to_string(),
            )
            .unwrap();
            includes.push(json!({"namespace": namespace, "container_name": "", "include": config}));
        }
        let global = fixture.root.join("database_global.json");
        fs::write(&global, json!({"INCLUDES": includes}).to_string()).unwrap();
        swss_common::sonic_db_config_initialize_global(global.to_str().unwrap(), false).unwrap();
        fixture
    }

    fn socket(&self, namespace: &str) -> PathBuf {
        self.root
            .join(if namespace.is_empty() {
                "default"
            } else {
                namespace
            })
            .join("redis.sock")
    }

    fn table(&self, namespace: &str, db_name: &str, table: &str) -> Table {
        Table::new(
            DbConnector::new_keyed(db_name, false, 0, "", namespace).unwrap(),
            table,
        )
        .unwrap()
    }
}

impl Drop for RedisFixture {
    fn drop(&mut self) {
        for server in &mut self.servers {
            let _ = server.kill();
            let _ = server.wait();
        }
        let _ = fs::remove_dir_all(&self.root);
    }
}

struct SocketOverride;

impl Drop for SocketOverride {
    fn drop(&mut self) {
        std::env::remove_var("REDIS_SOCK");
    }
}

// SONiC DB configuration is process-global, so exercise all namespaces in one fixture.
#[test]
#[ignore = "requires redis-server and SWSS Lua scripts in /usr/share/swss"]
fn namespace_tables_atomic_rows_and_producer_delivery() {
    let fixture = RedisFixture::new();
    let namespaces = vec!["asic1".into(), "asic3".into()];
    let cfg1 = fixture.table("asic1", "CONFIG_DB", "PORT");
    let cfg3 = fixture.table("asic3", "CONFIG_DB", "PORT");
    let host_cfg = fixture.table("", "CONFIG_DB", "PORT");
    cfg1.set(
        "Ethernet0",
        [("index", "1"), ("speed", "100000"), ("lanes", "0,1,2,3")],
    )
    .unwrap();
    cfg3.set(
        "Ethernet8",
        [("index", "2"), ("speed", "200000"), ("lanes", "4,5,6,7")],
    )
    .unwrap();
    host_cfg.set("Ethernet999", [("index", "99")]).unwrap();

    let mapping = get_port_mapping(&namespaces, 4).unwrap();
    assert_eq!(mapping.logical_port_list().len(), 2);
    assert_eq!(mapping.get_asic_id_for_logical_port("Ethernet0"), Some(1));
    assert_eq!(mapping.get_asic_id_for_logical_port("Ethernet8"), Some(3));
    assert_eq!(mapping.get_logical_to_physical("Ethernet8"), Some(vec![2]));
    assert!(!mapping.is_logical_port("Ethernet999"));
    let host_mapping = get_port_mapping(&[String::new()], 100).unwrap();
    assert_eq!(
        host_mapping.get_asic_id_for_logical_port("Ethernet999"),
        Some(0)
    );

    for (namespace, port, lanes) in [
        ("asic1", "Ethernet0", "0,1"),
        ("asic3", "Ethernet8", "4,5,6,7"),
    ] {
        fixture
            .table(namespace, "APPL_DB", GEARBOX_TABLE)
            .set("interface:0", [("name", port), ("line_lanes", lanes)])
            .unwrap();
    }
    let helper = XcvrTableHelper::new(&namespaces).unwrap();
    assert_eq!(helper.get_cfg_port_tbl(1).get_keys(), ["Ethernet0"]);
    assert_eq!(
        helper.get_gearbox_line_lanes_dict(),
        HashMap::from([("Ethernet0".into(), 2), ("Ethernet8".into(), 4),])
    );

    let row = helper.get_status_sw_tbl(1);
    row.hset("Ethernet0", "cmis_state", "READY");
    let mut socket = UnixStream::connect(fixture.socket("asic1")).unwrap();
    socket
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    socket.write_all(b"MONITOR\r\n").unwrap();
    let mut monitor = BufReader::new(socket);
    let mut line = String::new();
    monitor.read_line(&mut line).unwrap();
    assert_eq!(line, "+OK\r\n");
    assert!(row.set_checked(
        "Ethernet0",
        &[
            ("status".into(), "inserted".into()),
            ("error".into(), "N/A".into()),
            ("generation".into(), "2".into()),
        ]
    ));
    let state1 = env::open_state_db_in_namespace("asic1").unwrap();
    state1.exists("atomic-write-finished").unwrap();
    let mut row_commands = Vec::new();
    loop {
        line.clear();
        assert!(monitor.read_line(&mut line).unwrap() > 0);
        if line.contains("\"atomic-write-finished\"") {
            break;
        }
        if line.contains("\"TRANSCEIVER_STATUS_SW|Ethernet0\"") {
            row_commands.push(line.clone());
        }
    }
    assert_eq!(row_commands.len(), 1, "{row_commands:?}");
    assert!(row_commands[0].contains("\"HSET\"") || row_commands[0].contains("\"HMSET\""));
    drop(monitor);
    assert_eq!(
        row.hget("Ethernet0", "cmis_state").as_deref(),
        Some("READY")
    );
    assert_eq!(row.get_size_for_key("Ethernet0"), 4);
    let state3 = env::open_state_db_in_namespace("asic3").unwrap();
    let host_state = env::open_state_db().unwrap();
    assert!(!state3.exists("TRANSCEIVER_STATUS_SW|Ethernet0").unwrap());
    assert!(!host_state
        .exists("TRANSCEIVER_STATUS_SW|Ethernet0")
        .unwrap());
    state1
        .set(
            "TRANSCEIVER_STATUS_SW|wrongtype",
            &CxxString::new("not a hash"),
        )
        .unwrap();
    assert!(!row.set_checked("wrongtype", &[("field".into(), "new".into())]));
    assert_eq!(row.get_size_for_key_checked("wrongtype"), None);

    let appl1 = fixture.table("asic1", "APPL_DB", "PORT_TABLE");
    let appl3 = fixture.table("asic3", "APPL_DB", "PORT_TABLE");
    appl1.set("Ethernet0", [("flap_count", "1")]).unwrap();
    appl3.set("Ethernet8", [("flap_count", "2")]).unwrap();
    let consumer1 = ConsumerStateTable::new(
        env::open_appl_db_in_namespace("asic1").unwrap(),
        "PORT_TABLE",
        None,
        None,
    )
    .unwrap();
    let consumer3 = ConsumerStateTable::new(
        env::open_appl_db_in_namespace("asic3").unwrap(),
        "PORT_TABLE",
        None,
        None,
    )
    .unwrap();
    assert!(consumer1.pops().unwrap().is_empty());
    assert!(consumer3.pops().unwrap().is_empty());
    assert!(helper
        .get_app_port_tbl(3)
        .set_checked("Ethernet8", &[("preemphasis".into(), "0x1,0x2".into())]));
    assert_eq!(
        consumer3.read_data(Duration::from_secs(1), false).unwrap(),
        SelectResult::Data
    );
    let updates = consumer3.pops().unwrap();
    let update = updates
        .iter()
        .find(|update| update.key == "Ethernet8")
        .unwrap();
    assert_eq!(update.operation, KeyOperation::Set);
    assert_eq!(
        update.field_values["preemphasis"].to_string_lossy(),
        "0x1,0x2"
    );
    assert!(consumer1.pops().unwrap().is_empty());
    assert_eq!(
        helper
            .get_app_port_tbl(3)
            .hget("Ethernet8", "flap_count")
            .as_deref(),
        Some("2")
    );

    let mut dom = MultiPortChangeObserver::for_appl_port_table(&namespaces).unwrap();
    assert!(dom
        .take_initial_snapshot()
        .iter()
        .any(|ev| ev.port_name == "Ethernet8" && ev.asic_id == 3));
    let mut cmis = MultiPortChangeObserver::for_cmis(&namespaces).unwrap();
    assert!(cmis
        .take_initial_snapshot()
        .iter()
        .any(|ev| ev.port_name == "Ethernet0" && ev.asic_id == 1));
    let mut sff = MultiPortChangeObserver::for_sff(&namespaces).unwrap();
    sff.take_initial_snapshot();
    let mut sub1 = PortConfigChangeSubscriber::new("asic1").unwrap();
    let mut sub3 = PortConfigChangeSubscriber::new("asic3").unwrap();
    cfg3.set("Ethernet12", [("index", "3")]).unwrap();
    assert!(sub3
        .poll(1000)
        .iter()
        .any(|update| update.port_name == "Ethernet12"));
    assert_eq!(sub3.asic_id(), 3);
    assert!(sub1.poll(0).is_empty());
    assert!(cmis
        .handle_port_update_event(1000)
        .unwrap()
        .iter()
        .any(|ev| ev.port_name == "Ethernet12" && ev.asic_id == 3));
    Table::new(state3.clone_timeout(0).unwrap(), "TRANSCEIVER_INFO")
        .unwrap()
        .set("Ethernet8", [("type", "QSFP")])
        .unwrap();
    assert!(sff
        .handle_port_update_event(1000)
        .unwrap()
        .iter()
        .any(|ev| ev.port_name == "Ethernet8" && ev.asic_id == 3));
    appl3.set("Ethernet8", [("flap_count", "3")]).unwrap();
    assert!(dom
        .handle_port_update_event(1000)
        .unwrap()
        .iter()
        .any(|ev| ev.port_name == "Ethernet8" && ev.asic_id == 3));
    appl3.set("PortConfigDone", [("count", "1")]).unwrap();
    assert_eq!(
        run_port_config_done_gate("asic1", &|| false),
        PortConfigGate::TimedOut
    );
    assert_eq!(
        run_port_config_done_gate("asic3", &|| false),
        PortConfigGate::Done
    );

    std::env::set_var("REDIS_SOCK", fixture.socket("asic3"));
    let _override = SocketOverride;
    env::open_state_db()
        .unwrap()
        .hset("OVERRIDE|probe", "value", &CxxString::new("override"))
        .unwrap();
    let legacy = DbConnector::new_unix(6, fixture.socket("asic3").to_str().unwrap(), 0).unwrap();
    assert_eq!(
        legacy
            .hget("OVERRIDE|probe", "value")
            .unwrap()
            .unwrap()
            .to_string_lossy(),
        "override"
    );
    assert!(!env::open_state_db_in_namespace("asic1")
        .unwrap()
        .exists("OVERRIDE|probe")
        .unwrap());
    assert!(!env::open_state_db_in_namespace("asic3")
        .unwrap()
        .exists("OVERRIDE|probe")
        .unwrap());
}
