//! Startup, routing and shutdown of ycabled-rs (HLD 4.4).
//!
//! Startup follows Python ycabled's order, with the ports initialized in
//! parallel (N3): the DB configuration and namespaces, CONFIG_DB_INITIALIZED,
//! the XCVRD_LOG level reset, the platform, the port map, Loopback3,
//! grpc_secrets.json, the mux ports. Once every port has published its
//! state, the subscriptions start: their first events are the rows that
//! exist, so requests left while ycabled was down are applied after the
//! ports' state is known.

use std::collections::HashMap;
use std::net::IpAddr;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use anyhow::{bail, Result};
use tokio::signal::unix::{signal, SignalKind};
use tokio::sync::{mpsc, oneshot};

use crate::actor::{Msg, PortActor, Shared};
use crate::bridge::{LazyPlatform, PyBridge, PyPlatform, YCablePlatform};
use crate::config::{self, CableType};
use crate::db::{self, Db, DbConfig, DbId, Event, FieldMap, RedisDb};
use crate::grpc::{self, SocConnector, TonicConnector};
use crate::ports::{self, PortMap};
use crate::schema::*;
use crate::{log_debug, log_error, log_info, log_notice, log_warning};

/// One front-end namespace: its databases and its ports' actors.
pub struct Namespace {
    pub name: String,
    pub asic: usize,
    pub db: Arc<RedisDb>,
    pub shared: Arc<Shared>,
    pub actors: Mutex<HashMap<String, mpsc::Sender<Msg>>>,
    mux_subscribed: Mutex<bool>,
}

pub struct Daemon {
    pub namespaces: Vec<Arc<Namespace>>,
    pub ports: PortMap,
}

fn send(tx: &mpsc::Sender<Msg>, port: &str, msg: Msg) {
    if let Err(e) = tx.try_send(msg) {
        log_warning!("Dropped a request for port {}: {}", port, e);
    }
}

/// The tables a daemon with mux ports subscribes to, besides TRANSCEIVER_INFO.
pub fn mux_tables() -> Vec<(DbId, String)> {
    let mut t: Vec<(DbId, String)> = [
        HW_MUX_CABLE_TABLE,
        MUX_CABLE_COMMAND_TABLE,
        FORWARDING_STATE_COMMAND,
        HW_FORWARDING_STATE_PEER,
    ]
    .iter()
    .map(|t| (DbId::Appl, t.to_string()))
    .collect();
    t.extend(
        CLI_COMMAND_TABLES
            .iter()
            .map(|c| (DbId::Appl, c.to_string())),
    );
    t.push((DbId::Appl, XCVRD_DOWN_FW_STATUS_CMD.to_string()));
    t.push((DbId::Config, XCVRD_LOG.to_string()));
    t
}

fn exit_on_lost_subscription(ns: String) -> impl FnOnce() + Send + 'static {
    move || {
        log_error!(
            "Lost the redis subscription connection of namespace '{}', exiting",
            ns
        );
        std::process::exit(1);
    }
}

impl Namespace {
    fn actor(&self, port: &str) -> Option<mpsc::Sender<Msg>> {
        self.actors.lock().unwrap().get(port).cloned()
    }

    fn has_actors(&self) -> bool {
        !self.actors.lock().unwrap().is_empty()
    }

    fn add_actor(&self, port: &str, physical: Option<u32>, cable: CableType) -> mpsc::Sender<Msg> {
        let actor = PortActor::new(port, physical, Some(cable), Arc::clone(&self.shared));
        let tx = actor.spawn();
        self.actors
            .lock()
            .unwrap()
            .insert(port.to_string(), tx.clone());
        tx
    }
}

impl Daemon {
    /// Subscribes namespace `ns` to the mux tables, once.
    async fn subscribe_mux(self: &Arc<Self>, ns: &Arc<Namespace>) -> Result<()> {
        {
            let mut done = ns.mux_subscribed.lock().unwrap();
            if *done {
                return Ok(());
            }
            *done = true;
        }
        let tables = mux_tables();
        let receivers = ns
            .db
            .subscribe(&tables, exit_on_lost_subscription(ns.name.clone()))
            .await?;
        for ((_, table), rx) in tables.into_iter().zip(receivers) {
            let this = Arc::clone(self);
            let ns = Arc::clone(ns);
            tokio::spawn(this.route(ns, table, rx));
        }
        Ok(())
    }

    async fn subscribe_transceivers(self: &Arc<Self>, ns: &Arc<Namespace>) -> Result<()> {
        let tables = vec![(DbId::State, TRANSCEIVER_INFO.to_string())];
        let mut receivers = ns
            .db
            .subscribe(&tables, exit_on_lost_subscription(ns.name.clone()))
            .await?;
        let rx = receivers.remove(0);
        let this = Arc::clone(self);
        let ns = Arc::clone(ns);
        tokio::spawn(this.route(ns, TRANSCEIVER_INFO.to_string(), rx));
        Ok(())
    }

    /// Delivers the events of one subscribed table.
    fn route(
        self: Arc<Self>,
        ns: Arc<Namespace>,
        table: String,
        rx: mpsc::UnboundedReceiver<Event>,
    ) -> futures::future::BoxFuture<'static, ()> {
        // Boxed: a TRANSCEIVER_INFO event may start the mux subscriptions,
        // which spawn this again.
        Box::pin(self.route_events(ns, table, rx))
    }

    async fn route_events(
        self: Arc<Self>,
        ns: Arc<Namespace>,
        table: String,
        mut rx: mpsc::UnboundedReceiver<Event>,
    ) {
        while let Some(event) = rx.recv().await {
            let port = event.key().to_string();
            match (table.as_str(), event) {
                (TRANSCEIVER_INFO, event) => self.transceiver_event(&ns, &port, event).await,
                (XCVRD_LOG, Event::Set { key, fields }) => config::handle_xcvrd_log(&key, &fields),
                (XCVRD_DOWN_FW_STATUS_CMD, _) => {}
                (_, Event::Del { .. }) => {}
                (t, Event::Set { fields, .. }) => {
                    let msg = message_for(t, &port, fields);
                    match ns.actor(&port) {
                        Some(tx) => send(&tx, &port, msg),
                        None => {
                            if let Msg::Cli(cli, fields) = msg {
                                // Not a mux port: the CLI still gets an answer.
                                let physical = self.ports.get(&port).map(|p| p.physical);
                                let mut actor =
                                    PortActor::new(&port, physical, None, Arc::clone(&ns.shared));
                                actor.handle_cli(&cli, &fields).await;
                            } else {
                                log_debug!(
                                    "Ignoring a {} request for port {}, which is not a mux port",
                                    t,
                                    port
                                );
                            }
                        }
                    }
                }
            }
        }
    }

    async fn transceiver_event(self: &Arc<Self>, ns: &Arc<Namespace>, port: &str, event: Event) {
        if let Some(tx) = ns.actor(port) {
            send(&tx, port, Msg::Transceiver(event));
            return;
        }
        if !matches!(event, Event::Set { .. }) {
            return;
        }
        // A port that was not a mux port at startup may be one now.
        let Some(info) = self.ports.get(port) else {
            return;
        };
        if info.asic != ns.asic {
            return;
        }
        let row = ns.db.get(DbId::Config, MUX_CABLE, port).await;
        let Some(cable) = config::cable_type(port, row.as_ref()) else {
            return;
        };
        log_notice!(
            "Port {} became a {} mux port, initializing it",
            port,
            cable.as_str()
        );
        let tx = ns.add_actor(port, Some(info.physical), cable);
        send(&tx, port, Msg::Init(None));
        if let Err(e) = self.subscribe_mux(ns).await {
            log_error!("Failed to subscribe to the mux tables: {:#}", e);
        }
    }
}

/// The actor message for a row of a subscribed APPL_DB table.
pub fn message_for(table: &str, port: &str, fields: FieldMap) -> Msg {
    match table {
        HW_MUX_CABLE_TABLE => {
            log_debug!(
                "Y_CABLE_DEBUG: received an event for port transition {} {}",
                port,
                "ycabled-rs"
            );
            Msg::Toggle(fields)
        }
        HW_FORWARDING_STATE_PEER => Msg::PeerToggle(fields),
        MUX_CABLE_COMMAND_TABLE => {
            log_debug!(
                "Y_CABLE_DEBUG: received a probe for port status {} {}",
                port,
                "ycabled-rs"
            );
            Msg::MuxProbe(fields)
        }
        FORWARDING_STATE_COMMAND => {
            log_debug!(
                "Y_CABLE_DEBUG: received a probe for Forwarding state using gRPC port status {} {}",
                port,
                "ycabled-rs"
            );
            Msg::FwdProbe(fields)
        }
        cli => Msg::Cli(cli.to_string(), fields),
    }
}

/// Paths and settings, overridable for tests.
pub struct Options {
    pub db_config: String,
    pub db_global: String,
    pub secrets: String,
}

impl Default for Options {
    fn default() -> Self {
        Options {
            db_config: db::DATABASE_CONFIG.to_string(),
            db_global: db::DATABASE_GLOBAL.to_string(),
            secrets: config::SECRETS_PATH.to_string(),
        }
    }
}

pub async fn run(opts: Options) -> Result<()> {
    log_info!("Starting up...");
    let default_cfg = DbConfig::load(Path::new(&opts.db_config))?;
    let default_db = Arc::new(RedisDb::connect("", &default_cfg).await?);
    default_db.wait_for_config_init().await?;
    let device = config::device_info(default_db.as_ref()).await;
    let is_vs = device.is_vs();

    // Namespaces: the default one, or the front-end ASICs.
    let mut dbs: Vec<(String, usize, Arc<RedisDb>)> = Vec::new();
    let num_asics = ports::num_asics(&device.platform);
    if num_asics > 1 {
        let global = db::load_global(Path::new(&opts.db_global))?;
        for asic in 0..num_asics {
            let name = format!("asic{asic}");
            let Some(cfg) = global.get(&name) else {
                log_warning!("Namespace {} is not in {}", name, opts.db_global);
                continue;
            };
            let ns_db = Arc::new(RedisDb::connect(&name, cfg).await?);
            ns_db.wait_for_config_init().await?;
            if ports::is_front_end(ns_db.as_ref()).await {
                dbs.push((name, asic, ns_db));
            }
        }
    } else {
        dbs.push((String::new(), 0, default_db.clone()));
    }

    for (_, _, d) in &dbs {
        d.set(
            DbId::Config,
            XCVRD_LOG,
            "Y_CABLE",
            &[("log_verbosity", "notice".to_string())],
        )
        .await;
    }

    let mut port_map = PortMap::default();
    for (_, asic, d) in &dbs {
        port_map.add_config_db(d.as_ref(), *asic).await;
    }
    if port_map.is_empty() {
        for (_, asic, _) in &dbs {
            let path = ports::port_config_path(&device.hwsku, (num_asics > 1).then_some(*asic));
            port_map.add_port_config(&path, *asic);
        }
    }
    if port_map.is_empty() {
        bail!("Failed to read port info: no CONFIG_DB PORT table and no port_config.ini");
    }

    // Mux ports of each namespace, in port order.
    let mut mux: Vec<Vec<(String, CableType)>> = Vec::new();
    let mut read_sides = Vec::new();
    for (_, asic, d) in &dbs {
        read_sides.push(config::read_side(d.as_ref()).await);
        config::apply_grpc_secrets(d.as_ref(), &opts.secrets).await;
        let mux_keys = d.keys(DbId::Config, MUX_CABLE).await;
        let mut ports_here = Vec::new();
        for name in port_map.names() {
            if port_map.get(&name).map(|p| p.asic) != Some(*asic) || !mux_keys.contains(&name) {
                continue;
            }
            let row = d.get(DbId::Config, MUX_CABLE, &name).await;
            if let Some(cable) = config::cable_type(&name, row.as_ref()) {
                ports_here.push((name, cable));
            }
        }
        mux.push(ports_here);
    }
    let any_standby = mux
        .iter()
        .flatten()
        .any(|(_, c)| *c == CableType::ActiveStandby);
    let any_active = mux
        .iter()
        .flatten()
        .any(|(_, c)| *c == CableType::ActiveActive);

    let platform: Arc<dyn YCablePlatform> = if any_standby {
        match tokio::task::spawn_blocking(move || PyBridge::start(is_vs)).await? {
            Ok(b) => PyPlatform::new(b),
            Err(e) => {
                // Python's level for this message. It then falls back to the
                // legacy sfputil plugin, which ycabled-rs does not support.
                log_warning!("Failed to load chassis due to {}", e);
                bail!("no platform API for the active-standby ports");
            }
        }
    } else {
        LazyPlatform::new(is_vs)
    };
    let soc: Arc<dyn SocConnector> = Arc::new(TonicConnector);

    let ycable_presence = Arc::new(AtomicBool::new(false));
    let mut namespaces = Vec::new();
    for (i, (name, asic, d)) in dbs.into_iter().enumerate() {
        let dyn_db: Arc<dyn Db> = d.clone();
        let shared = Shared::new(
            dyn_db,
            Arc::clone(&platform),
            Arc::clone(&soc),
            is_vs,
            read_sides[i],
            Arc::clone(&ycable_presence),
        );
        namespaces.push(Arc::new(Namespace {
            name,
            asic,
            db: d,
            shared,
            actors: Mutex::new(HashMap::new()),
            mux_subscribed: Mutex::new(false),
        }));
    }
    let daemon = Arc::new(Daemon {
        namespaces,
        ports: port_map,
    });

    // Initialize every mux port at the same time. Python initializes them one
    // by one in port order, and only the port that sets y_cable_presence
    // first writes MUX_CABLE_INFO, if it is active-standby. The actors report
    // whether they set it, and the decision is taken here, in Python's order.
    let order: HashMap<String, usize> = daemon
        .ports
        .names()
        .into_iter()
        .enumerate()
        .map(|(i, n)| (n, i))
        .collect();
    let mut done = Vec::new();
    for (i, ns) in daemon.namespaces.iter().enumerate() {
        for (port, cable) in &mux[i] {
            let physical = daemon.ports.get(port).map(|p| p.physical);
            let tx = ns.add_actor(port, physical, *cable);
            let (dtx, drx) = oneshot::channel();
            send(&tx, port, Msg::Init(Some(dtx)));
            done.push((
                order.get(port).copied().unwrap_or(usize::MAX),
                port.clone(),
                *cable,
                tx,
                drx,
            ));
        }
    }
    done.sort_by_key(|d| d.0);
    let mut results = Vec::new();
    for (_, port, cable, tx, drx) in done {
        results.push((port, cable, tx, drx.await.unwrap_or(false)));
    }
    let flags: Vec<(CableType, bool)> = results.iter().map(|(_, c, _, s)| (*c, *s)).collect();
    let (first, presence) = first_info_port(&flags);
    if let Some((port, _, tx, _)) = first.map(|i| &results[i]) {
        send(tx, port, Msg::PostInfo);
    }
    ycable_presence.store(presence, Ordering::SeqCst);

    for ns in &daemon.namespaces {
        daemon.subscribe_transceivers(ns).await?;
        if ns.has_actors() {
            daemon.subscribe_mux(ns).await?;
        }
    }

    if any_active {
        for (i, ns) in daemon.namespaces.iter().enumerate() {
            start_graceful_restart(ns, &daemon.ports, read_sides[i]).await;
        }
    }

    log_info!("Start daemon main loop");
    wait_for_signals().await;
    Ok(())
}

/// Python's `y_cable_presence` at startup. For each mux port in Python's
/// order, its cable type and whether it sets the flag: the port that writes
/// MUX_CABLE_INFO, which is the first to set it if it is active-standby, and
/// the flag's value afterwards.
pub fn first_info_port(ports: &[(CableType, bool)]) -> (Option<usize>, bool) {
    match ports.iter().position(|(_, sets)| *sets) {
        Some(i) => ((ports[i].0 == CableType::ActiveStandby).then_some(i), true),
        None => (None, false),
    }
}

/// One GracefulRestart subscription per SoC address in MUX_CABLE, whatever
/// the port's cable type, as Python does.
async fn start_graceful_restart(ns: &Arc<Namespace>, ports: &PortMap, read_side: i32) {
    for name in ports.names() {
        if ports.get(&name).map(|p| p.asic) != Some(ns.asic) {
            continue;
        }
        let Some(row) = ns.db.get(DbId::Config, MUX_CABLE, &name).await else {
            continue;
        };
        if !row.contains_key("state") {
            continue;
        }
        let Ok(Some(ip)) = config::soc_ip(&row) else {
            continue;
        };
        let db: Arc<dyn Db> = ns.db.clone();
        tokio::spawn(grpc::graceful_restart_loop(
            name.clone(),
            ip as IpAddr,
            read_side,
            db,
        ));
    }
}

async fn wait_for_signals() {
    let (mut term, mut int, mut hup) = match (
        signal(SignalKind::terminate()),
        signal(SignalKind::interrupt()),
        signal(SignalKind::hangup()),
    ) {
        (Ok(t), Ok(i), Ok(h)) => (t, i, h),
        _ => {
            log_error!("Failed to set up the signal handlers");
            std::process::exit(1);
        }
    };
    loop {
        tokio::select! {
            _ = term.recv() => {
                log_notice!("Caught SIGTERM - exiting...");
                break;
            }
            _ = int.recv() => {
                log_notice!("Caught SIGINT - exiting...");
                break;
            }
            _ = hup.recv() => log_info!("Caught SIGHUP - ignoring..."),
        }
    }
}
