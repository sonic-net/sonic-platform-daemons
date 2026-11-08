//! Port actors (HLD 4.3.3): one task per mux port, handling that port's
//! requests one at a time, in order. Different ports run at the same time.

mod active;
mod standby;
#[cfg(test)]
mod tests;

pub(crate) use standby::{TARGET_NIC, TARGET_TOR_A, TARGET_TOR_B};

use std::collections::HashMap;
use std::net::IpAddr;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::sync::{mpsc, oneshot, Semaphore};
use tokio::time::{Instant, MissedTickBehavior};

use crate::bridge::YCablePlatform;
use crate::config::{self, CableType};
use crate::db::{Db, DbId, Event, FieldMap};
use crate::grpc::{SocApi, SocConnector};
use crate::schema::*;
use crate::{fields, log_debug, log_info, log_warning};

/// A port's backlog worth a warning. Nothing is dropped: a port whose cable
/// or SoC does not answer keeps its requests waiting, in order.
pub const BACKLOG_WARNING: usize = 256;
pub const TELEMETRY_PERIOD: Duration = Duration::from_secs(60);

/// What a port actor is asked to do.
#[derive(Debug)]
pub enum Msg {
    /// Load the driver or set up the channel and publish the state. With a
    /// sender (startup), the actor reports whether the port sets Python's
    /// `y_cable_presence` flag ([`Shared::ycable_presence`]) and leaves it
    /// to the daemon, which decides in Python's port order which port writes
    /// MUX_CABLE_INFO ([`Msg::PostInfo`]). Without one, the actor applies
    /// the flag itself, as on an insertion.
    Init(Option<oneshot::Sender<bool>>),
    /// Startup: this is the first port Python would have initialized, so it
    /// writes MUX_CABLE_INFO now; the others wait for the telemetry pass.
    PostInfo,
    /// STATE_DB TRANSCEIVER_INFO: a cable inserted (Set) or removed (Del).
    Transceiver(Event),
    /// APPL_DB HW_MUX_CABLE_TABLE from orchagent.
    Toggle(FieldMap),
    /// APPL_DB HW_FORWARDING_STATE_PEER from linkmgrd (active-active).
    PeerToggle(FieldMap),
    /// APPL_DB MUX_CABLE_COMMAND_TABLE from linkmgrd (active-standby).
    MuxProbe(FieldMap),
    /// APPL_DB FORWARDING_STATE_COMMAND from linkmgrd (active-active).
    FwdProbe(FieldMap),
    /// APPL_DB XCVRD_*_CMD from the CLI: command table and fields.
    Cli(String, FieldMap),
    /// The periodic MUX_CABLE_INFO refresh.
    Telemetry,
}

/// What all actors of a namespace share.
pub struct Shared {
    pub db: Arc<dyn Db>,
    pub platform: Arc<dyn YCablePlatform>,
    pub soc: Arc<dyn SocConnector>,
    pub is_vs: bool,
    /// This ToR's index in the SoCs' port lists, from Loopback3.
    pub read_side: i32,
    pub telemetry_period: Duration,
    /// Active-standby telemetry passes, one at a time, as in Python's serial
    /// pass: cables may share an I2C bus.
    pub bus: Arc<Semaphore>,
    /// Active-active telemetry, at most 16 ports at a time.
    pub aa_telemetry: Arc<Semaphore>,
    /// Python's process-wide `y_cable_presence`: set by the first port
    /// initialized, an active-active port with a SoC address or an
    /// active-standby port whose driver is loaded. Only the active-standby
    /// port that sets it writes MUX_CABLE_INFO when it is initialized.
    pub ycable_presence: Arc<AtomicBool>,
}

impl Shared {
    pub fn new(
        db: Arc<dyn Db>,
        platform: Arc<dyn YCablePlatform>,
        soc: Arc<dyn SocConnector>,
        is_vs: bool,
        read_side: i32,
        ycable_presence: Arc<AtomicBool>,
    ) -> Arc<Shared> {
        Arc::new(Shared {
            db,
            platform,
            soc,
            is_vs,
            read_side,
            telemetry_period: TELEMETRY_PERIOD,
            bus: Arc::new(Semaphore::new(1)),
            aa_telemetry: Arc::new(Semaphore::new(16)),
            ycable_presence,
        })
    }
}

/// Counts of SoC queries, written in MUX_CABLE_INFO (Python's
/// grpc_port_stats). Clones share the counts: the telemetry task counts its
/// queries too.
#[derive(Clone, Debug, Default)]
pub struct ProbeCounts(Arc<Mutex<HashMap<&'static str, u64>>>);

impl ProbeCounts {
    pub fn bump(&self, names: &[&'static str]) {
        let mut m = self.0.lock().unwrap_or_else(|p| p.into_inner());
        for n in names {
            *m.entry(n).or_insert(0) += 1;
        }
    }

    /// The count as written: "unknown" before the first query.
    pub fn get(&self, name: &str) -> String {
        let m = self.0.lock().unwrap_or_else(|p| p.into_inner());
        m.get(name)
            .map(|n| n.to_string())
            .unwrap_or_else(|| "unknown".to_string())
    }
}

pub struct PortActor {
    pub port: String,
    /// The index the platform API and the driver take; None if the port is
    /// not in the port map.
    pub physical: Option<u32>,
    /// None for a port that is not a mux port (only the CLI asks about it).
    pub cable: Option<CableType>,
    pub shared: Arc<Shared>,
    // Active-standby: a driver is loaded and reported itself initialized.
    pub initialized: bool,
    /// The cable is plugged in, as far as ycabled-rs has seen.
    pub present: bool,
    // Active-active.
    pub soc_ip: Option<IpAddr>,
    pub soc: Option<Arc<dyn SocApi>>,
    pub counts: ProbeCounts,
    /// The driver's EEPROM_ERROR, read when it is loaded.
    pub eeprom_error: i64,
    /// Held for each call to the cable: the actor and its telemetry task
    /// never call it at the same time, and a request waits for at most one
    /// telemetry call (the lock is fair).
    pub call_lock: Arc<tokio::sync::Mutex<()>>,
    /// Changes when the cable is initialized or removed: a telemetry pass
    /// that overlapped either is not written.
    pub epoch: Arc<AtomicU64>,
    telemetry_busy: Arc<AtomicBool>,
}

impl PortActor {
    pub fn new(
        port: &str,
        physical: Option<u32>,
        cable: Option<CableType>,
        shared: Arc<Shared>,
    ) -> PortActor {
        PortActor {
            port: port.to_string(),
            physical,
            cable,
            shared,
            initialized: false,
            present: false,
            soc_ip: None,
            soc: None,
            counts: ProbeCounts::default(),
            eeprom_error: -1,
            call_lock: Arc::new(tokio::sync::Mutex::new(())),
            epoch: Arc::new(AtomicU64::new(0)),
            telemetry_busy: Arc::new(AtomicBool::new(false)),
        }
    }

    /// What a telemetry task needs of this port, sharing the call lock, the
    /// probe counts and the epoch.
    fn telemetry_view(&self) -> PortActor {
        PortActor {
            port: self.port.clone(),
            physical: self.physical,
            cable: self.cable,
            shared: Arc::clone(&self.shared),
            initialized: self.initialized,
            present: self.present,
            soc_ip: self.soc_ip,
            soc: self.soc.clone(),
            counts: self.counts.clone(),
            eeprom_error: self.eeprom_error,
            call_lock: Arc::clone(&self.call_lock),
            epoch: Arc::clone(&self.epoch),
            telemetry_busy: Arc::clone(&self.telemetry_busy),
        }
    }

    pub fn bump_epoch(&self) {
        self.epoch.fetch_add(1, Ordering::SeqCst);
    }

    pub fn current_epoch(&self) -> u64 {
        self.epoch.load(Ordering::SeqCst)
    }

    /// The periodic pass, in its own task: toggles, probes and CLI commands
    /// for the port go on meanwhile, between its calls to the cable.
    fn start_telemetry(&self) {
        if config::telemetry_disabled() || self.cable.is_none() {
            return;
        }
        if self.telemetry_busy.swap(true, Ordering::SeqCst) {
            return;
        }
        let mut view = self.telemetry_view();
        tokio::spawn(async move {
            view.telemetry().await;
            view.telemetry_busy.store(false, Ordering::SeqCst);
        });
    }

    pub fn db(&self) -> &dyn Db {
        self.shared.db.as_ref()
    }

    /// Spawns the actor and returns its mailbox.
    pub fn spawn(self) -> mpsc::UnboundedSender<Msg> {
        let (tx, rx) = mpsc::unbounded_channel();
        tokio::spawn(self.run(rx));
        tx
    }

    pub async fn run(mut self, mut rx: mpsc::UnboundedReceiver<Msg>) {
        let period = self.shared.telemetry_period;
        let mut tick = tokio::time::interval_at(Instant::now() + period, period);
        tick.set_missed_tick_behavior(MissedTickBehavior::Delay);
        let mut warn_at = BACKLOG_WARNING;
        loop {
            tokio::select! {
                msg = rx.recv() => match msg {
                    Some(m) => {
                        let backlog = rx.len();
                        if backlog >= warn_at {
                            log_warning!("{} requests are waiting for port {}", backlog, self.port);
                            warn_at = backlog * 2;
                        } else if backlog == 0 {
                            warn_at = BACKLOG_WARNING;
                        }
                        self.handle(m).await
                    }
                    None => break,
                },
                _ = tick.tick() => self.start_telemetry(),
            }
        }
    }

    pub async fn handle(&mut self, msg: Msg) {
        match msg {
            Msg::Init(done) => {
                let claims = self.init(done.is_some()).await;
                if let Some(d) = done {
                    let _ = d.send(claims);
                }
            }
            Msg::PostInfo => {
                if self.cable == Some(CableType::ActiveStandby) {
                    self.post_info_standby().await
                }
            }
            Msg::Transceiver(Event::Set { .. }) => {
                log_info!("Got SFP inserted ycable event");
                self.insertion().await
            }
            Msg::Transceiver(Event::Del { .. }) => {
                log_info!("Got SFP deleted ycable event");
                self.removal().await
            }
            Msg::Toggle(f) => match self.cable {
                Some(CableType::ActiveStandby) => self.toggle_standby(&f).await,
                Some(CableType::ActiveActive) => self.toggle_active(&f, false).await,
                None => {}
            },
            Msg::PeerToggle(f) => {
                if self.cable == Some(CableType::ActiveActive) {
                    self.toggle_active(&f, true).await
                }
            }
            Msg::MuxProbe(f) => {
                if self.cable == Some(CableType::ActiveStandby) {
                    self.probe_standby(&f).await
                }
            }
            Msg::FwdProbe(f) => {
                if self.cable == Some(CableType::ActiveActive) {
                    self.forwarding_probe(&f).await
                }
            }
            Msg::Cli(table, f) => self.handle_cli(&table, &f).await,
            Msg::Telemetry => {
                if !config::telemetry_disabled() {
                    self.telemetry().await
                }
            }
        }
    }

    /// Returns whether the port sets Python's `y_cable_presence`; with
    /// `defer`, the caller applies it (see [`Msg::Init`]).
    async fn init(&mut self, defer: bool) -> bool {
        self.bump_epoch();
        match self.cable {
            Some(CableType::ActiveStandby) => self.init_standby(defer).await,
            Some(CableType::ActiveActive) => self.init_active(defer).await,
            None => false,
        }
    }

    /// TRANSCEIVER_INFO SET: a cable is plugged in, or its row changed.
    async fn insertion(&mut self) {
        match self.cable {
            Some(CableType::ActiveStandby) => {
                if self.initialized {
                    log_debug!(
                        "Ignoring transceiver info update for initialized Y-cable port {}",
                        self.port
                    );
                    return;
                }
                self.bump_epoch();
                self.init_standby(false).await;
            }
            Some(CableType::ActiveActive) => {
                if self.present {
                    log_debug!(
                        "Ignoring transceiver info update for present Y-cable port {}",
                        self.port
                    );
                    return;
                }
                self.bump_epoch();
                self.init_active(false).await;
            }
            None => {}
        }
    }

    /// TRANSCEIVER_INFO DEL: the cable is gone. The state is unknown; the read
    /// side, MUX_CABLE_INFO and MUX_CABLE_STATIC_INFO stay. A port without a
    /// HW_MUX_CABLE_TABLE row gets none.
    async fn removal(&mut self) {
        if self.cable.is_none() {
            return;
        }
        self.bump_epoch();
        self.present = false;
        match self.db().get(DbId::State, HW_MUX_CABLE_TABLE, &self.port).await {
            Some(row) => {
                let read_side = row.get("read_side").cloned().unwrap_or_else(|| "-1".to_string());
                self.write_mux_state("unknown", &read_side, "-1").await;
            }
            None => log_debug!(
                "Could not retreive fieldvalue pairs for {}, inside state_db table {} while deleting mux entry",
                self.port,
                HW_MUX_CABLE_TABLE
            ),
        }
        if self.cable == Some(CableType::ActiveStandby) {
            self.initialized = false;
            if let Some(p) = self.physical {
                // After the telemetry pass's current call, if one is running.
                let _call = self.call_lock.lock().await;
                self.shared.platform.unload(p).await;
            }
        }
    }

    async fn telemetry(&mut self) {
        match self.cable {
            Some(CableType::ActiveStandby) => {
                let bus = Arc::clone(&self.shared.bus);
                let _permit = bus.acquire().await;
                self.telemetry_standby().await
            }
            Some(CableType::ActiveActive) => {
                let limit = Arc::clone(&self.shared.aa_telemetry);
                let _permit = limit.acquire().await;
                self.telemetry_active().await
            }
            None => {}
        }
    }

    // --- Writes shared by both cable types -----------------------------------

    pub async fn write_mux_state(&self, state: &str, read_side: &str, active_side: &str) {
        self.write_mux_state_to(HW_MUX_CABLE_TABLE, state, read_side, active_side)
            .await
    }

    pub async fn write_mux_state_to(
        &self,
        table: &str,
        state: &str,
        read_side: &str,
        active_side: &str,
    ) {
        let f = fields!["state" => state, "read_side" => read_side, "active_side" => active_side];
        self.db().set(DbId::State, table, &self.port, &f).await
    }

    pub async fn write_metrics(&self, side: &str, state: &str, start: &str, end: &str) {
        let f = [
            (
                format!("xcvrd_switch_{side}{state}_start"),
                start.to_string(),
            ),
            (format!("xcvrd_switch_{side}{state}_end"), end.to_string()),
        ];
        let fv: Vec<(&str, String)> = f.iter().map(|(k, v)| (k.as_str(), v.clone())).collect();
        self.db()
            .set(DbId::State, MUX_METRICS_TABLE, &self.port, &fv)
            .await
    }

    /// MUX_CABLE_INFO of a port without a usable cable.
    pub async fn write_placeholder_info(&self) {
        let mut f: Vec<(&str, String)> = vec![("time_post", timestamp())];
        f.extend(placeholder_info());
        self.db()
            .set(DbId::State, MUX_CABLE_INFO, &self.port, &f)
            .await
    }

    /// The read side in HW_MUX_CABLE_TABLE, as Python's `int(...)` of it;
    /// None without a row.
    pub async fn stored_read_side(&self) -> Option<(FieldMap, i64)> {
        let row = self
            .db()
            .get(DbId::State, HW_MUX_CABLE_TABLE, &self.port)
            .await?;
        let rs = row
            .get("read_side")
            .and_then(|v| v.trim().parse::<i64>().ok())
            .unwrap_or(-1);
        Some((row, rs))
    }

    /// Presence of an active-active cable: its TRANSCEIVER_INFO row, which
    /// xcvrd keeps (D3). Always present on VS.
    pub async fn transceiver_present(&self) -> bool {
        if self.shared.is_vs {
            return true;
        }
        self.db()
            .exists(DbId::State, TRANSCEIVER_INFO, &self.port)
            .await
    }

    /// Presence of an active-standby cable: the platform API.
    pub async fn cable_present(&self) -> bool {
        match self.physical {
            Some(p) => self.shared.platform.presence(p).await,
            None => false,
        }
    }

    /// The MUX_CABLE row of this port, read again (it may have changed).
    pub async fn mux_cable_row(&self) -> Option<FieldMap> {
        self.db().get(DbId::Config, MUX_CABLE, &self.port).await
    }

    pub fn warn_not_in_port_map(&self) {
        log_warning!(
            "Could not retreive port inside config_db PORT table {} for Y-Cable initiation",
            self.port
        );
    }
}

/// The MUX_CABLE_INFO fields of an active-standby cable that cannot be read,
/// without time_post.
pub fn placeholder_info() -> Vec<(&'static str, String)> {
    let mut f: Vec<(&'static str, String)> = vec![
        ("tor_active", "unknown".into()),
        ("mux_direction", "unknown".into()),
        ("manual_switch_count", "N/A".into()),
        ("auto_switch_count", "N/A".into()),
        ("link_status_self", "unknown".into()),
        ("link_status_peer", "unknown".into()),
        ("link_status_nic", "unknown".into()),
    ];
    for name in [
        "self_eye_height_lane1",
        "self_eye_height_lane2",
        "peer_eye_height_lane1",
        "peer_eye_height_lane2",
        "nic_eye_height_lane1",
        "nic_eye_height_lane2",
        "internal_temperature",
        "internal_voltage",
        "nic_temperature",
        "nic_voltage",
        "version_self_active",
        "version_self_inactive",
        "version_self_next",
        "version_peer_active",
        "version_peer_inactive",
        "version_peer_next",
        "version_nic_active",
        "version_nic_inactive",
        "version_nic_next",
    ] {
        f.push((name, "N/A".into()));
    }
    f
}

/// Python's `str()` of a list of bools from a protobuf reply.
pub fn py_bools(v: &[bool]) -> String {
    format!(
        "[{}]",
        v.iter()
            .map(|b| if *b { "True" } else { "False" })
            .collect::<Vec<_>>()
            .join(", ")
    )
}

pub fn py_ints(v: &[i32]) -> String {
    format!(
        "[{}]",
        v.iter()
            .map(|i| i.to_string())
            .collect::<Vec<_>>()
            .join(", ")
    )
}
