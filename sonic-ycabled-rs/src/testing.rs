//! Test doubles for the unit tests: a platform with scriptable cables, a SoC
//! and a connector, around [`MemDb`]. Like the black-box suite's fake
//! hardware, a healthy cable answers with known values, and a test scripts
//! a method to return something else or to raise.

use std::collections::{HashMap, HashSet, VecDeque};
use std::net::IpAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;

use crate::actor::{PortActor, Shared};
use crate::bridge::{CallError, YCablePlatform};
use crate::config::CableType;
use crate::db::{Db, DbId, MemDb};
use crate::grpc::{RpcError, RpcResult, SocApi, SocConnector};
use crate::proto::{AdminReply, LinkStateReply, OperationReply, ServerVersionReply};
use crate::pyval::{PyArg, PyVal};
use crate::schema::*;

/// A scripted driver result.
#[derive(Clone, Debug)]
pub enum Reply {
    Value(PyVal),
    Raise(String),
}

pub fn raise(repr: &str) -> Reply {
    Reply::Raise(repr.to_string())
}

pub fn value(v: PyVal) -> Reply {
    Reply::Value(v)
}

/// One cable, as the driver and the platform API see it.
#[derive(Clone, Debug)]
pub struct MockCable {
    pub present: bool,
    /// What load() gives: Some(initialized), or None (no usable driver).
    pub load: Option<bool>,
    /// Scripted replies per method: the front one is used and, if more
    /// follow, removed.
    pub scripts: HashMap<String, VecDeque<Reply>>,
    pub attrs: HashMap<String, PyVal>,
    pub read_side: i64,
    pub mux_direction: i64,
    /// How long a method takes.
    pub delays: HashMap<String, Duration>,
}

impl Default for MockCable {
    fn default() -> Self {
        let mut attrs = HashMap::new();
        attrs.insert("EEPROM_ERROR".to_string(), PyVal::Int(-1));
        attrs.insert("download_firmware_status".to_string(), PyVal::Int(0));
        MockCable {
            present: true,
            load: Some(true),
            scripts: HashMap::new(),
            attrs,
            read_side: 1,
            mux_direction: 1,
            delays: HashMap::new(),
        }
    }
}

fn ints(v: &[i64]) -> PyVal {
    PyVal::list(v.iter().map(|i| PyVal::Int(*i)).collect())
}

impl MockCable {
    pub fn script(&mut self, method: &str, reply: Reply) -> &mut Self {
        self.scripts
            .insert(method.to_string(), VecDeque::from(vec![reply]));
        self
    }

    /// Replies in order; the last one stays.
    pub fn script_seq(&mut self, method: &str, replies: Vec<Reply>) -> &mut Self {
        self.scripts.insert(method.to_string(), replies.into());
        self
    }

    /// What a healthy cable answers.
    fn default_reply(&mut self, method: &str, args: &[PyArg]) -> PyVal {
        let target = match args.first() {
            Some(PyArg::Int(t)) => *t,
            _ => -1,
        };
        match method {
            "get_vendor" => PyVal::str_("BB Vendor"),
            "get_read_side" => PyVal::Int(self.read_side),
            "get_mux_direction" | "get_active_linked_tor_side" => PyVal::Int(self.mux_direction),
            "toggle_mux_to_tor_a" => {
                self.mux_direction = 1;
                PyVal::Bool(true)
            }
            "toggle_mux_to_tor_b" => {
                self.mux_direction = 2;
                PyVal::Bool(true)
            }
            "get_switch_count_total" => match args.first() {
                Some(PyArg::Str(s)) if s == "manual" => PyVal::Int(3),
                _ => PyVal::Int(5),
            },
            "get_eye_heights" => ints(&[600 + 10 * target + 1, 600 + 10 * target + 2]),
            "is_link_active" => PyVal::Bool(target != 2),
            "get_firmware_version" => PyVal::dict(vec![
                (
                    PyVal::str_("version_active"),
                    PyVal::str_(format!("1.{target}.0")),
                ),
                (
                    PyVal::str_("version_inactive"),
                    PyVal::str_(format!("0.{target}.9")),
                ),
                (
                    PyVal::str_("version_next"),
                    PyVal::str_(format!("1.{target}.0")),
                ),
            ]),
            "get_local_temperature" => PyVal::Int(35),
            "get_local_voltage" => PyVal::float(3.3),
            "get_nic_temperature" => PyVal::Int(45),
            "get_nic_voltage" => PyVal::float(5.1),
            "get_target_cursor_values" => {
                let lane = target;
                let t = match args.get(1) {
                    Some(PyArg::Int(t)) => *t,
                    _ => 0,
                };
                let base = lane * 100 + t * 10;
                ints(&[base + 1, base + 2, base + 3, base + 4, base + 5])
            }
            _ => PyVal::None,
        }
    }
}

/// A [`YCablePlatform`] of [`MockCable`]s, by physical index.
#[derive(Default)]
pub struct MockPlatform {
    pub cables: Mutex<HashMap<u32, MockCable>>,
    loaded: Mutex<HashSet<u32>>,
    calls: Mutex<Vec<(u32, String, Vec<PyArg>)>>,
    loads: Mutex<Vec<u32>>,
    in_flight: Mutex<HashMap<u32, usize>>,
    /// Calls that started while another call to the same cable was running.
    pub overlaps: Mutex<usize>,
}

impl MockPlatform {
    pub fn new() -> Arc<MockPlatform> {
        Arc::new(MockPlatform::default())
    }

    pub fn add(&self, physical: u32, cable: MockCable) {
        self.cables.lock().unwrap().insert(physical, cable);
    }

    pub fn with<R>(&self, physical: u32, f: impl FnOnce(&mut MockCable) -> R) -> R {
        let mut cables = self.cables.lock().unwrap();
        f(cables.entry(physical).or_default())
    }

    /// The driver methods called on `physical`, in order.
    pub fn calls(&self, physical: u32) -> Vec<String> {
        self.calls
            .lock()
            .unwrap()
            .iter()
            .filter(|(p, _, _)| *p == physical)
            .map(|(_, m, _)| m.clone())
            .collect()
    }

    pub fn calls_with_args(&self, physical: u32) -> Vec<(String, Vec<PyArg>)> {
        self.calls
            .lock()
            .unwrap()
            .iter()
            .filter(|(p, _, _)| *p == physical)
            .map(|(_, m, a)| (m.clone(), a.clone()))
            .collect()
    }

    pub fn clear_calls(&self) {
        self.calls.lock().unwrap().clear();
    }

    pub fn loads(&self) -> Vec<u32> {
        self.loads.lock().unwrap().clone()
    }

    pub fn is_loaded(&self, physical: u32) -> bool {
        self.loaded.lock().unwrap().contains(&physical)
    }
}

#[async_trait]
impl YCablePlatform for MockPlatform {
    async fn presence(&self, physical: u32) -> bool {
        self.cables
            .lock()
            .unwrap()
            .get(&physical)
            .map(|c| c.present)
            .unwrap_or(false)
    }

    async fn load(&self, _port: &str, physical: u32) -> Option<bool> {
        self.loads.lock().unwrap().push(physical);
        let load = self
            .cables
            .lock()
            .unwrap()
            .get(&physical)
            .and_then(|c| c.load);
        let mut loaded = self.loaded.lock().unwrap();
        match load {
            Some(i) => {
                loaded.insert(physical);
                Some(i)
            }
            None => {
                loaded.remove(&physical);
                None
            }
        }
    }

    async fn unload(&self, physical: u32) {
        self.loaded.lock().unwrap().remove(&physical);
    }

    async fn has_driver(&self, physical: u32) -> bool {
        self.loaded.lock().unwrap().contains(&physical)
    }

    async fn call(
        &self,
        physical: u32,
        method: &str,
        args: Vec<PyArg>,
    ) -> Result<PyVal, CallError> {
        if !self.loaded.lock().unwrap().contains(&physical) {
            return Err(CallError::NoDriver);
        }
        self.calls
            .lock()
            .unwrap()
            .push((physical, method.to_string(), args.clone()));
        {
            let mut f = self.in_flight.lock().unwrap();
            let n = f.entry(physical).or_insert(0);
            *n += 1;
            if *n > 1 {
                *self.overlaps.lock().unwrap() += 1;
            }
        }
        let delay = self
            .cables
            .lock()
            .unwrap()
            .get(&physical)
            .and_then(|c| c.delays.get(method).copied());
        if let Some(d) = delay {
            tokio::time::sleep(d).await;
        }
        let result = self.reply(physical, method, &args);
        *self.in_flight.lock().unwrap().entry(physical).or_insert(1) -= 1;
        result
    }

    async fn attr(&self, physical: u32, name: &str) -> Result<PyVal, CallError> {
        if !self.loaded.lock().unwrap().contains(&physical) {
            return Err(CallError::NoDriver);
        }
        let cables = self.cables.lock().unwrap();
        match cables.get(&physical).and_then(|c| c.attrs.get(name)) {
            Some(v) => Ok(v.clone()),
            None => Err(CallError::Raised(format!("AttributeError('{name}')"))),
        }
    }
}

impl MockPlatform {
    fn reply(&self, physical: u32, method: &str, args: &[PyArg]) -> Result<PyVal, CallError> {
        let mut cables = self.cables.lock().unwrap();
        let cable = cables.entry(physical).or_default();
        if let Some(q) = cable.scripts.get_mut(method) {
            let r = if q.len() > 1 {
                q.pop_front().unwrap()
            } else {
                q.front().cloned().unwrap()
            };
            return match r {
                Reply::Value(v) => Ok(v),
                Reply::Raise(e) => Err(CallError::Raised(e)),
            };
        }
        Ok(cable.default_reply(method, args))
    }
}

/// A scripted SoC: forwarding (admin), link and operation states, index 0
/// the upper ToR's, 1 the lower's.
/// An RPC the SoC got: its name, port ids and states.
pub type RpcCall = (&'static str, Vec<i32>, Vec<bool>);

pub struct MockSoc {
    pub admin: Mutex<Vec<bool>>,
    pub link: Mutex<Vec<bool>>,
    pub oper: Mutex<Vec<bool>>,
    pub version: String,
    /// Per RPC: an error instead of the answer.
    pub errors: Mutex<HashMap<&'static str, RpcError>>,
    /// Per RPC: this reply instead of the default (admin replies).
    pub admin_replies: Mutex<HashMap<&'static str, AdminReply>>,
    pub calls: Mutex<Vec<RpcCall>>,
    pub connectivity: Mutex<String>,
}

impl Default for MockSoc {
    fn default() -> Self {
        MockSoc {
            admin: Mutex::new(vec![true, false]),
            link: Mutex::new(vec![true, true]),
            oper: Mutex::new(vec![true, true]),
            version: "bb-soc-1.0".into(),
            errors: Mutex::new(HashMap::new()),
            admin_replies: Mutex::new(HashMap::new()),
            calls: Mutex::new(vec![]),
            connectivity: Mutex::new("READY".into()),
        }
    }
}

impl MockSoc {
    pub fn new() -> Arc<MockSoc> {
        Arc::new(MockSoc::default())
    }

    pub fn fail(&self, rpc: &'static str, err: RpcError) {
        self.errors.lock().unwrap().insert(rpc, err);
    }

    pub fn reply(&self, rpc: &'static str, reply: AdminReply) {
        self.admin_replies.lock().unwrap().insert(rpc, reply);
    }

    pub fn names(&self) -> Vec<&'static str> {
        self.calls
            .lock()
            .unwrap()
            .iter()
            .map(|(n, _, _)| *n)
            .collect()
    }

    pub fn requests(&self, rpc: &str) -> Vec<(Vec<i32>, Vec<bool>)> {
        self.calls
            .lock()
            .unwrap()
            .iter()
            .filter(|(n, _, _)| *n == rpc)
            .map(|(_, p, s)| (p.clone(), s.clone()))
            .collect()
    }

    fn record(&self, rpc: &'static str, portid: Vec<i32>, state: Vec<bool>) -> Option<RpcError> {
        self.calls.lock().unwrap().push((rpc, portid, state));
        self.errors.lock().unwrap().get(rpc).cloned()
    }
}

#[async_trait]
impl SocApi for MockSoc {
    async fn query_admin_forwarding(&self) -> RpcResult<AdminReply> {
        if let Some(e) = self.record(
            "QueryAdminForwardingPortState",
            vec![0, 1],
            vec![false, false],
        ) {
            return Err(e);
        }
        if let Some(r) = self
            .admin_replies
            .lock()
            .unwrap()
            .get("QueryAdminForwardingPortState")
        {
            return Ok(r.clone());
        }
        Ok(AdminReply {
            portid: vec![0, 1],
            state: self.admin.lock().unwrap().clone(),
        })
    }

    async fn set_admin_forwarding(&self, portid: i32, state: bool) -> RpcResult<AdminReply> {
        if let Some(e) = self.record("SetAdminForwardingPortState", vec![portid], vec![state]) {
            return Err(e);
        }
        if let Some(r) = self
            .admin_replies
            .lock()
            .unwrap()
            .get("SetAdminForwardingPortState")
        {
            return Ok(r.clone());
        }
        let mut admin = self.admin.lock().unwrap();
        if (0..=1).contains(&portid) {
            admin[portid as usize] = state;
        }
        Ok(AdminReply {
            portid: vec![portid],
            state: vec![state],
        })
    }

    async fn query_link_state(&self) -> RpcResult<LinkStateReply> {
        if let Some(e) = self.record("QueryLinkState", vec![0, 1], vec![]) {
            return Err(e);
        }
        Ok(LinkStateReply {
            portid: vec![0, 1],
            state: self.link.lock().unwrap().clone(),
        })
    }

    async fn query_operation_state(&self) -> RpcResult<OperationReply> {
        if let Some(e) = self.record("QueryOperationPortState", vec![0, 1], vec![]) {
            return Err(e);
        }
        Ok(OperationReply {
            portid: vec![0, 1],
            state: self.oper.lock().unwrap().clone(),
        })
    }

    async fn query_server_version(&self) -> RpcResult<ServerVersionReply> {
        if let Some(e) = self.record("QueryServerVersion", vec![], vec![]) {
            return Err(e);
        }
        Ok(ServerVersionReply {
            version: self.version.clone(),
        })
    }

    fn connectivity(&self) -> String {
        self.connectivity.lock().unwrap().clone()
    }
}

/// Hands out one SoC, or none: a configuration that allows no channel.
pub struct MockConnector {
    pub soc: Mutex<Option<Arc<MockSoc>>>,
    pub connects: Mutex<Vec<(String, IpAddr)>>,
}

impl MockConnector {
    pub fn new(soc: Option<Arc<MockSoc>>) -> Arc<MockConnector> {
        Arc::new(MockConnector {
            soc: Mutex::new(soc),
            connects: Mutex::new(vec![]),
        })
    }

    pub fn connects(&self) -> usize {
        self.connects.lock().unwrap().len()
    }
}

#[async_trait]
impl SocConnector for MockConnector {
    async fn connect(&self, port: &str, ip: IpAddr, _db: Arc<dyn Db>) -> Option<Arc<dyn SocApi>> {
        self.connects.lock().unwrap().push((port.to_string(), ip));
        self.soc
            .lock()
            .unwrap()
            .clone()
            .map(|s| s as Arc<dyn SocApi>)
    }
}

/// A namespace with one mux port, Ethernet0 on physical port 1.
pub struct Fixture {
    pub db: Arc<MemDb>,
    pub platform: Arc<MockPlatform>,
    pub soc: Arc<MockSoc>,
    pub connector: Arc<MockConnector>,
    pub shared: Arc<Shared>,
}

pub const TEST_PORT: &str = "Ethernet0";
pub const PHYSICAL: u32 = 1;

impl Default for Fixture {
    fn default() -> Self {
        Fixture::new()
    }
}

impl Fixture {
    pub fn new() -> Fixture {
        Fixture::with(false, 0)
    }

    pub fn with(is_vs: bool, read_side: i32) -> Fixture {
        let db = MemDb::new();
        let platform = MockPlatform::new();
        platform.add(PHYSICAL, MockCable::default());
        let soc = MockSoc::new();
        let connector = MockConnector::new(Some(Arc::clone(&soc)));
        let shared = Shared::new(
            db.clone() as Arc<dyn Db>,
            platform.clone() as Arc<dyn YCablePlatform>,
            connector.clone() as Arc<dyn SocConnector>,
            is_vs,
            read_side,
            Arc::new(std::sync::atomic::AtomicBool::new(false)),
        );
        Fixture {
            db,
            platform,
            soc,
            connector,
            shared,
        }
    }

    /// An active-standby mux port with its cable.
    pub fn standby(&self) -> PortActor {
        self.db
            .put(DbId::Config, MUX_CABLE, TEST_PORT, &[("state", "auto")]);
        PortActor::new(
            TEST_PORT,
            Some(PHYSICAL),
            Some(CableType::ActiveStandby),
            Arc::clone(&self.shared),
        )
    }

    /// An active-active mux port whose cable xcvrd has published.
    pub fn active(&self) -> PortActor {
        self.db.put(
            DbId::Config,
            MUX_CABLE,
            TEST_PORT,
            &[
                ("state", "auto"),
                ("cable_type", "active-active"),
                ("soc_ipv4", "192.168.0.7/32"),
            ],
        );
        self.db.put(
            DbId::State,
            TRANSCEIVER_INFO,
            TEST_PORT,
            &[("type", "QSFP28 or later")],
        );
        PortActor::new(
            TEST_PORT,
            Some(PHYSICAL),
            Some(CableType::ActiveActive),
            Arc::clone(&self.shared),
        )
    }

    pub fn row(&self, db: DbId, table: &str) -> HashMap<String, String> {
        self.db.row(db, table, TEST_PORT).unwrap_or_default()
    }

    pub fn mux_state(&self) -> (String, String, String) {
        let r = self.row(DbId::State, HW_MUX_CABLE_TABLE);
        (
            r.get("state").cloned().unwrap_or_default(),
            r.get("read_side").cloned().unwrap_or_default(),
            r.get("active_side").cloned().unwrap_or_default(),
        )
    }

    pub fn cable<R>(&self, f: impl FnOnce(&mut MockCable) -> R) -> R {
        self.platform.with(PHYSICAL, f)
    }
}

pub fn fm(v: &[(&str, &str)]) -> HashMap<String, String> {
    v.iter()
        .map(|(a, b)| (a.to_string(), b.to_string()))
        .collect()
}

/// (state, read_side, active_side) as owned strings, for assertions.
pub fn row3(s: &str, r: &str, a: &str) -> (String, String, String) {
    (s.to_string(), r.to_string(), a.to_string())
}

/// Serializes the tests that change process-wide settings (log level, the
/// telemetry flag).
pub static GLOBALS: Mutex<()> = Mutex::new(());
