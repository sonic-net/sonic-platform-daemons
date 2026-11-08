//! `show/config muxcable` commands, through the XCVRD_* tables (HLD 3.4.9,
//! 3.6.5, 4.3.8).
//!
//! The CLI writes the arguments (APPL_DB `XCVRD_*_CMD_ARG`, the CLI's to
//! delete), then the command (APPL_DB `XCVRD_*_CMD`), and waits for the
//! response field in STATE_DB `XCVRD_*_RSP`. The port's actor runs the
//! command as Python ycabled does and answers in Python's order: the result
//! rows (STATE_DB `XCVRD_*_RES`) first, then the response field, then it
//! deletes the command key. Errors get the answer Python gives them; where
//! Python crashes (a port outside the port map, an argument that is not an
//! integer, rollback without a driver), the command is answered as an error.
//!
//! Driver calls take the port's call lock, as every call to the cable does,
//! but not the shared-bus limit of telemetry passes: as in Python, a command
//! does not wait for other ports' telemetry. A firmware download runs in a
//! task of its own, as in Python's thread per download: the port keeps
//! serving telemetry and other commands while the cable takes the image.

use std::sync::Arc;
use std::time::Duration;

use crate::actor::{py_ints, PortActor, TARGET_NIC, TARGET_TOR_A, TARGET_TOR_B};
use crate::config::CableType;
use crate::db::{Db, DbId, FieldMap};
use crate::grpc::{self, log_rpc_error};
use crate::pyval::{PyArg, PyVal};
use crate::schema::HW_MUX_CABLE_TABLE;
use crate::{log_debug, log_error, log_notice, log_warning};

/// Where the firmware images of `config muxcable firmware` are.
pub const FIRMWARE_DIR: &str = "/usr/share/sonic/firmware";

/// How long Python waits after a firmware download or activation returns
/// before it answers.
const FIRMWARE_SETTLE: Duration = Duration::from_secs(5);

/// The fields of XCVRD_SHOW_FW_RES, in Python's order.
const FIRMWARE_FIELDS: [&str; 9] = [
    "version_self_active",
    "version_self_inactive",
    "version_self_next",
    "version_peer_active",
    "version_peer_inactive",
    "version_peer_next",
    "version_nic_active",
    "version_nic_inactive",
    "version_nic_next",
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Kind {
    ShowMuxDirection,
    ConfigMuxState,
    ShowSwitchMode,
    ConfigSwitchMode,
    ShowFirmware,
    DownloadFirmware,
    ActivateFirmware,
    RollbackFirmware,
    Prbs,
    Loopback,
    EventLog,
    Fec,
    Ber,
}

/// One CLI command and its tables (Python's YcableCliUpdateTableHelper).
#[derive(Debug)]
pub struct Cmd {
    kind: Kind,
    /// APPL_DB command table.
    pub table: &'static str,
    /// The field the CLI writes in the command table.
    pub input: &'static str,
    /// APPL_DB argument table.
    pub arg: Option<&'static str>,
    /// STATE_DB response table and field.
    pub rsp: &'static str,
    pub output: &'static str,
    /// STATE_DB result table.
    pub res: Option<&'static str>,
}

const fn cmd(
    kind: Kind,
    table: &'static str,
    input: &'static str,
    arg: Option<&'static str>,
    rsp: &'static str,
    output: &'static str,
    res: Option<&'static str>,
) -> Cmd {
    Cmd {
        kind,
        table,
        input,
        arg,
        rsp,
        output,
        res,
    }
}

/// Every command, in the order of schema::CLI_COMMAND_TABLES: kind, command
/// table and field, argument table, response table and field, result table.
#[rustfmt::skip]
pub static COMMANDS: [Cmd; 13] = [
    cmd(Kind::ShowMuxDirection, "XCVRD_SHOW_HWMODE_DIR_CMD", "state", None,
        "XCVRD_SHOW_HWMODE_DIR_RSP", "state", Some("XCVRD_SHOW_HWMODE_DIR_RES")),
    cmd(Kind::ConfigMuxState, "XCVRD_CONFIG_HWMODE_DIR_CMD", "config", None,
        "XCVRD_CONFIG_HWMODE_DIR_RSP", "result", None),
    cmd(Kind::ShowSwitchMode, "XCVRD_SHOW_HWMODE_SWMODE_CMD", "state", None,
        "XCVRD_SHOW_HWMODE_SWMODE_RSP", "state", None),
    cmd(Kind::ConfigSwitchMode, "XCVRD_CONFIG_HWMODE_SWMODE_CMD", "config", None,
        "XCVRD_CONFIG_HWMODE_SWMODE_RSP", "result", None),
    cmd(Kind::ShowFirmware, "XCVRD_SHOW_FW_CMD", "firmware_version", None,
        "XCVRD_SHOW_FW_RSP", "status", Some("XCVRD_SHOW_FW_RES")),
    cmd(Kind::DownloadFirmware, "XCVRD_DOWN_FW_CMD", "download_firmware", None,
        "XCVRD_DOWN_FW_RSP", "status", None),
    cmd(Kind::ActivateFirmware, "XCVRD_ACTI_FW_CMD", "activate_firmware", Some("XCVRD_ACTI_FW_CMD_ARG"),
        "XCVRD_ACTI_FW_RSP", "status", None),
    cmd(Kind::RollbackFirmware, "XCVRD_ROLL_FW_CMD", "rollback_firmware", None,
        "XCVRD_ROLL_FW_RSP", "status", None),
    cmd(Kind::Prbs, "XCVRD_CONFIG_PRBS_CMD", "config_prbs", Some("XCVRD_CONFIG_PRBS_CMD_ARG"),
        "XCVRD_CONFIG_PRBS_RSP", "status", None),
    cmd(Kind::Loopback, "XCVRD_CONFIG_LOOP_CMD", "config_loop", Some("XCVRD_CONFIG_LOOP_CMD_ARG"),
        "XCVRD_CONFIG_LOOP_RSP", "status", None),
    cmd(Kind::EventLog, "XCVRD_EVENT_LOG_CMD", "show_event", None,
        "XCVRD_EVENT_LOG_RSP", "status", Some("XCVRD_EVENT_LOG_RES")),
    cmd(Kind::Fec, "XCVRD_GET_FEC_CMD", "get_fec", None,
        "XCVRD_GET_FEC_RSP", "status", Some("XCVRD_GET_FEC_RES")),
    cmd(Kind::Ber, "XCVRD_GET_BER_CMD", "get_ber", Some("XCVRD_GET_BER_CMD_ARG"),
        "XCVRD_GET_BER_RSP", "status", Some("XCVRD_GET_BER_RES")),
];

/// What Python's get_ycable_physical_port_from_logical_port and
/// get_ycable_port_instance_from_logical_port find for the port.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Access {
    /// Not in the port map, or no cable plugged in.
    Absent,
    /// A cable without a driver.
    NoDriver,
    Ready,
}

/// How a result row is made from a driver's return value.
enum Shape {
    /// A list: one field per item, named by its index.
    List,
    /// A dict: its items.
    Dict,
    /// Anything but None: one field.
    Field(&'static str),
}

impl PortActor {
    /// One CLI command for this port: `table` is the XCVRD_*_CMD table.
    pub async fn handle_cli(&mut self, table: &str, fields: &FieldMap) {
        let Some(cmd) = COMMANDS.iter().find(|c| c.table == table) else {
            log_debug!(
                "Ignoring an event of table {} for port {}, which is not a CLI command table",
                table,
                self.port
            );
            return;
        };
        log_debug!(
            "Y_CABLE_DEBUG: received a cli command {} {:?} for port {}",
            table,
            fields,
            self.port
        );
        let input = fields.get(cmd.input).map(String::as_str);
        match cmd.kind {
            Kind::ShowMuxDirection => self.cli_show_mux_direction(cmd, input).await,
            Kind::ConfigMuxState => self.cli_config_mux_state(cmd, input).await,
            Kind::ShowSwitchMode => self.cli_show_switch_mode(cmd, input).await,
            Kind::ConfigSwitchMode => self.cli_config_switch_mode(cmd, input).await,
            Kind::ShowFirmware => self.cli_show_firmware(cmd, input).await,
            Kind::DownloadFirmware => self.cli_download_firmware(cmd, input).await,
            Kind::ActivateFirmware => self.cli_activate_firmware(cmd, input).await,
            Kind::RollbackFirmware => self.cli_rollback_firmware(cmd, input).await,
            Kind::Prbs => self.cli_config_prbs(cmd, input).await,
            Kind::Loopback => self.cli_config_loopback(cmd, input).await,
            Kind::EventLog => self.cli_show_event_log(cmd, input).await,
            Kind::Fec => self.cli_show_fec(cmd, input).await,
            Kind::Ber => self.cli_show_ber(cmd, input).await,
        }
    }

    // --- show/config muxcable hwmode muxdirection/state ------------------------

    /// `show muxcable hwmode muxdirection`: the presence, and the direction of
    /// the cable as this ToR sees it; for active-active, the state ycabled
    /// published.
    async fn cli_show_mux_direction(&mut self, cmd: &'static Cmd, input: Option<&str>) {
        if input.is_none() {
            // Python neither answers nor deletes such a command.
            log_warning!(
                "Error: Wrong input param for cli command show mux hwmode muxdirection logical port {}",
                self.port
            );
            return;
        }
        let present = match self.physical {
            Some(_) => self.cli_present().await,
            None => {
                log_error!("Invalid port '{}'", self.port);
                false
            }
        };
        self.write_result(
            cmd,
            &[("presence".to_string(), py_bool(present).to_string())],
        )
        .await;
        if !present {
            log_error!(
                "Error: Could not get physical port for cli command show mux hwmode muxdirection Y cable port {}",
                self.port
            );
            return self.respond(cmd, "unknown").await;
        }
        let state = match self.cable {
            Some(CableType::ActiveStandby) => self.cli_standby_direction().await.to_string(),
            Some(CableType::ActiveActive) => self.cli_active_direction().await,
            None => {
                log_warning!(
                    "Error: Wrong input param for cli command show mux hwmode muxdirection logical port {}",
                    self.port
                );
                "unknown".to_string()
            }
        };
        self.respond(cmd, &state).await;
    }

    async fn cli_standby_direction(&mut self) -> &'static str {
        if !self.cli_has_driver().await {
            log_error!(
                "Error: Could not get port instance for cli command show mux hwmode muxdirection Y cable port {}",
                self.port
            );
            return "not Y-Cable port";
        }
        let read_side = self
            .drv_or_none("get_read_side", vec![], "get_read_side API")
            .await;
        // Read side 0 (the NIC's) is unknown here too (HLD 3.8 issue 31).
        if self.bad_side(&read_side) || read_side.as_ref().is_some_and(|v| v.eq_int(TARGET_NIC)) {
            log_warning!(
                "Error: Could not get read side for cli command show mux hwmode muxdirection logical port {} and physical port {}",
                self.port,
                self.physical_str()
            );
            return "unknown";
        }
        let active = self
            .drv_or_none("get_mux_direction", vec![], "get_mux_direction API")
            .await;
        if self.bad_side(&active) {
            log_warning!(
                "Error: Could not get active side for cli command show mux hwmode muxdirection logical port {} and physical port {}",
                self.port,
                self.physical_str()
            );
            return "unknown";
        }
        let read = read_side.and_then(|v| v.as_number());
        match active.and_then(|v| v.as_number()) {
            Some(a) if a == TARGET_TOR_A as f64 || a == TARGET_TOR_B as f64 => {
                if read == Some(a) {
                    "active"
                } else {
                    "standby"
                }
            }
            _ => {
                log_warning!(
                    "Error: Could not get valid state for cli command show mux hwmode muxdirection logical port {} and physical port {}",
                    self.port,
                    self.physical_str()
                );
                "unknown"
            }
        }
    }

    /// The state in HW_MUX_CABLE_TABLE; without one, the SoC's. The channel is
    /// not set up again for the CLI, as in Python.
    async fn cli_active_direction(&mut self) -> String {
        let Some(row) = self
            .db()
            .get(DbId::State, HW_MUX_CABLE_TABLE, &self.port)
            .await
        else {
            log_debug!(
                "Could not retreive fieldvalue pairs for {}, inside state_db table while responding to cli cmd show mux status {}",
                self.port,
                HW_MUX_CABLE_TABLE
            );
            return "unknown".to_string();
        };
        if let Some(state) = row.get("state") {
            return state.clone();
        }
        let read_side: i32 = row
            .get("read_side")
            .and_then(|v| v.trim().parse().ok())
            .unwrap_or(-1);
        log_debug!(
            "Y_CABLE_DEBUG:before invoking RPC fwd_state read_side = {}",
            read_side
        );
        let Some(soc) = self.soc.clone() else {
            log_warning!(
                "stub is None for getting forwarding state RPC port for cli query {}",
                self.port
            );
            return "unknown".to_string();
        };
        let reply = soc.query_admin_forwarding().await;
        let (self_state, _) = grpc::parse_forwarding_state(&reply, read_side);
        self.counts.bump(&[
            "mux_direction_probe_count",
            "peer_mux_direction_probe_count",
        ]);
        match &reply {
            Ok(r) => log_debug!(
                "procesing request hwmode forwarding state RPC received response port ids = {} port {} tid {}",
                py_ints(&r.portid),
                self.port,
                "ycabled-rs"
            ),
            Err(e) => {
                log_rpc_error(e);
                log_notice!("response was none cli handle_fwd_state_command_grpc_notification {} ", self.port);
            }
        }
        self_state.to_string()
    }

    /// `config muxcable hwmode state active|standby`: toggles the cable only;
    /// HW_MUX_CABLE_TABLE is left alone.
    async fn cli_config_mux_state(&mut self, cmd: &'static Cmd, input: Option<&str>) {
        let Some(state) = input else {
            log_error!(
                "Error: Wrong input param for cli command config mux hwmode state active/standby logical port {}",
                self.port
            );
            return self.respond(cmd, "False").await;
        };
        match self.access().await {
            Access::Absent => {
                log_error!(
                    "Error: Could not get physical port for cli command config mux hwmode state active/standby Y cable port {}",
                    self.port
                );
                return self.respond(cmd, "False").await;
            }
            Access::NoDriver => {
                log_error!(
                    "Error: Could not get port instance for cli command config mux hwmode state active/standby Y cable port {}",
                    self.port
                );
                return self.respond(cmd, "False").await;
            }
            Access::Ready => {}
        }
        let read_side = self
            .drv_or_none("get_read_side", vec![], "get_read_side API")
            .await;
        if self.bad_side(&read_side) {
            log_error!(
                "Error: Could not get read side for cli command config mux hwmode state active/standby Y cable port {}",
                self.port
            );
            return self.respond(cmd, "False").await;
        }
        // Python compares the read side with `is`: an int, not a bool.
        let to_tor_a = match (&read_side, state) {
            (Some(PyVal::Int(TARGET_TOR_A)), "active")
            | (Some(PyVal::Int(TARGET_TOR_B)), "standby") => true,
            (Some(PyVal::Int(TARGET_TOR_A)), "standby")
            | (Some(PyVal::Int(TARGET_TOR_B)), "active") => false,
            (Some(PyVal::Int(TARGET_TOR_A | TARGET_TOR_B)), _) => {
                log_warning!(
                    "Error: Wrong state {} for cli command config mux hwmode state active/standby Y cable port {}",
                    state,
                    self.port
                );
                return self.respond(cmd, "False").await;
            }
            _ => {
                log_error!(
                    "Error: Could not get valid config read side for cli command config mux hwmode state active/standby Y cable port {}",
                    self.port
                );
                return self.respond(cmd, "False").await;
            }
        };
        let (method, what) = if to_tor_a {
            ("toggle_mux_to_tor_a", "toggle mux ToR A API")
        } else {
            ("toggle_mux_to_tor_b", "toggle mux ToR B API")
        };
        let status = self.status_of_call(method, vec![], what).await;
        self.respond(cmd, &status).await;
    }

    // --- show/config muxcable hwmode switchmode -------------------------------

    async fn cli_show_switch_mode(&mut self, cmd: &'static Cmd, input: Option<&str>) {
        if input.is_none() {
            log_error!(
                "Error: Incorrect input param for cli cmd show mux hwmode switchmode logical port {}",
                self.port
            );
            return self.respond(cmd, "unknown").await;
        }
        match self.access().await {
            Access::Absent => {
                log_error!(
                    "Error: Could not get physical port for cli cmd show mux hwmode switchmode Y cable port {}",
                    self.port
                );
                return self.respond(cmd, "cable not present").await;
            }
            Access::NoDriver => {
                log_error!(
                    "Error: Could not get port instance for cli cmd show mux hwmode switchmode Y cable port {}",
                    self.port
                );
                return self.respond(cmd, "not Y-Cable port").await;
            }
            Access::Ready => {}
        }
        let mode = self
            .drv_or_none("get_switching_mode", vec![], "get_switching_mode")
            .await;
        if self.bad_side(&mode) {
            log_error!(
                "Error: Could not get read side for cli cmd show mux hwmode switchmode logical port {} and physical port {}",
                self.port,
                self.physical_str()
            );
            return self.respond(cmd, "unknown").await;
        }
        let auto = self.driver_const("SWITCHING_MODE_AUTO", 1).await;
        let manual = self.driver_const("SWITCHING_MODE_MANUAL", 0).await;
        let state = match &mode {
            Some(m) if m.eq_int(auto) => "auto",
            Some(m) if m.eq_int(manual) => "manual",
            _ => "unknown",
        };
        self.respond(cmd, state).await;
    }

    async fn cli_config_switch_mode(&mut self, cmd: &'static Cmd, input: Option<&str>) {
        let Some(mode) = input else {
            log_error!(
                "Error: Incorrect input param for cli cmd config mux hwmode setswitchmode logical port {}",
                self.port
            );
            return self.respond(cmd, "False").await;
        };
        match self.access().await {
            Access::Absent => {
                log_error!(
                    "Error: Could not get physical port for cli cmd config mux hwmode setswitchmode Y cable port {}",
                    self.port
                );
                return self.respond(cmd, "False").await;
            }
            Access::NoDriver => {
                log_error!(
                    "Error: Could not get port instance for cli cmd config mux hwmode setswitchmode Y cable port {}",
                    self.port
                );
                return self.respond(cmd, "False").await;
            }
            Access::Ready => {}
        }
        let value = match mode {
            "auto" => self.driver_const("SWITCHING_MODE_AUTO", 1).await,
            "manual" => self.driver_const("SWITCHING_MODE_MANUAL", 0).await,
            _ => {
                log_error!(
                    "Error: Incorrect Config state for cli cmd config mux hwmode setswitchmode logical port {} and physical port {}",
                    self.port,
                    self.physical_str()
                );
                return self.respond(cmd, "False").await;
            }
        };
        let what = format!("set_switching_mode {}", mode);
        let result = self
            .drv_or_none("set_switching_mode", vec![PyArg::Int(value)], &what)
            .await;
        if self.bad_side(&result) {
            log_error!(
                "Error: Could not get read side for cli cmd config mux hwmode setswitchmode logical port {} and physical port {}",
                self.port,
                self.physical_str()
            );
            return self.respond(cmd, "False").await;
        }
        let status = match &result {
            Some(v) => status_of(v, &what, &self.port),
            None => "False".to_string(),
        };
        self.respond(cmd, &status).await;
    }

    // --- show/config muxcable firmware ------------------------------------------

    /// `show muxcable firmware version`: the versions of both ToR sides and
    /// the NIC side. During a download, the last known ones (MUX_CABLE_INFO).
    async fn cli_show_firmware(&mut self, cmd: &'static Cmd, input: Option<&str>) {
        if input.is_none() {
            log_error!("Wrong param for cli cmd show firmware port {}", self.port);
            return self.fail_show_firmware(cmd).await;
        }
        match self.access().await {
            Access::Absent => {
                log_warning!(
                    "Error: Could not get physical port for cli cmd show firmware port {}",
                    self.port
                );
                return self.fail_show_firmware(cmd).await;
            }
            Access::NoDriver => {
                log_warning!(
                    "Error: Could not get port instance for cli cmd show firmware command port {}",
                    self.port
                );
                return self.fail_show_firmware(cmd).await;
            }
            Access::Ready => {}
        }
        let read_side = self
            .drv_or_none("get_read_side", vec![], "get_read_side API")
            .await;
        if self.bad_side(&read_side) {
            log_warning!(
                "Error: Could not get read side for cli cmd show firmware port {}",
                self.port
            );
            return self.fail_show_firmware(cmd).await;
        }
        let nic = self.firmware_versions(TARGET_NIC, "nic").await;
        let (self_v, peer_v) = if read_side.as_ref().is_some_and(|v| v.eq_int(TARGET_TOR_A)) {
            let s = self.firmware_versions(TARGET_TOR_A, "self").await;
            let p = self.firmware_versions(TARGET_TOR_B, "peer").await;
            (s, p)
        } else {
            let p = self.firmware_versions(TARGET_TOR_A, "peer").await;
            let s = self.firmware_versions(TARGET_TOR_B, "self").await;
            (s, p)
        };
        let res: Vec<(String, String)> = FIRMWARE_FIELDS
            .iter()
            .map(|f| f.to_string())
            .zip(self_v.into_iter().chain(peer_v).chain(nic))
            .collect();
        self.write_result(cmd, &res).await;
        self.respond(cmd, "True").await;
    }

    async fn fail_show_firmware(&self, cmd: &'static Cmd) {
        let res: Vec<(String, String)> = FIRMWARE_FIELDS
            .iter()
            .map(|f| (f.to_string(), "N/A".to_string()))
            .collect();
        self.write_result(cmd, &res).await;
        self.respond(cmd, "False").await;
    }

    /// `config muxcable firmware download`: the download runs in a task of
    /// its own, without the port's call lock, which it would hold for
    /// minutes (Python does not take its port lock either); the answer
    /// comes when it ends.
    async fn cli_download_firmware(&mut self, cmd: &'static Cmd, input: Option<&str>) {
        let Some(name) = input else {
            log_error!(
                "Error: Wrong input parameter get for cli cmd download firmware Y cable port {}",
                self.port
            );
            return self.respond(cmd, "-1").await;
        };
        let path = firmware_path(name);
        if !is_file(&path).await {
            log_error!(
                "Error: cli cmd download firmware file does not exist port {} file {}",
                self.port,
                name
            );
            return self.respond(cmd, "-1").await;
        }
        let physical = match (self.access().await, self.physical) {
            (Access::Ready, Some(p)) => p,
            (Access::NoDriver, _) => {
                log_error!(
                    "Error: Could not get port instance for cli cmd download firmware Y cable port {}",
                    self.port
                );
                return self.respond(cmd, "-1").await;
            }
            _ => {
                log_error!(
                    "Error: Could not get physical port for cli cmd download firmware cli Y cable port {}",
                    self.port
                );
                return self.respond(cmd, "-1").await;
            }
        };
        let shared = Arc::clone(&self.shared);
        let port = self.port.clone();
        tokio::spawn(async move {
            log_debug!(
                "Y_CABLE_DEBUG:worker thread launched for downloading physical port {} path {}",
                physical,
                path
            );
            let image = vec![PyArg::Str(path.clone())];
            let status = match shared
                .platform
                .call(physical, "download_firmware", image)
                .await
            {
                Ok(v) => {
                    tokio::time::sleep(FIRMWARE_SETTLE).await;
                    status_of(&v, "download firmware API", &port)
                }
                Err(e) => {
                    log_warning!(
                        "Failed to execute the download firmware API for port {} due to {}",
                        physical,
                        e
                    );
                    "-1".to_string()
                }
            };
            respond(shared.db.as_ref(), cmd, &port, &status).await;
            log_debug!(
                "Y_CABLE_DEBUG:downloading complete {} {} {}",
                physical,
                path,
                status
            );
            log_debug!(
                "Y_CABLE_DEBUG:download thread finished port {} physical_port {}",
                port,
                physical
            );
        });
    }

    /// `config muxcable firmware activate`. The cable restarts: as in Python,
    /// the port waits 5 s before it serves anything else.
    async fn cli_activate_firmware(&mut self, cmd: &'static Cmd, input: Option<&str>) {
        let Some(name) = input else {
            log_error!(
                "Wrong param for cli cmd mux activate firmware port {}",
                self.port
            );
            return self.respond(cmd, "False").await;
        };
        // Python's reading of the argument row: `hitless` asks for a
        // non-hitless activation, whatever its value.
        let hitless = !self.cli_args(cmd).await.contains_key("hitless");
        let path = (name != "null").then(|| firmware_path(name));
        if let Some(p) = &path {
            if !is_file(p).await {
                log_error!(
                    "ERROR: cli cmd mux activate firmware file does not exist port {} file {}",
                    self.port,
                    name
                );
                return self.respond(cmd, "False").await;
            }
        }
        match self.access().await {
            Access::Absent => {
                log_warning!(
                    "Error: Could not get physical port for cli cmd mux activate firmware port {}",
                    self.port
                );
                return self.respond(cmd, "False").await;
            }
            Access::NoDriver => {
                log_warning!(
                    "Error: Could not get port instance for cli cmd mux activate firmware port {}",
                    self.port
                );
                return self.respond(cmd, "False").await;
            }
            Access::Ready => {}
        }
        let result = {
            self.drv(
                "activate_firmware",
                vec![path_arg(&path), PyArg::Bool(hitless)],
            )
            .await
        };
        let status = match result {
            Ok(v) => {
                tokio::time::sleep(FIRMWARE_SETTLE).await;
                status_of(&v, "activate_firmware API", &self.port)
            }
            Err(e) => {
                log_warning!(
                    "Failed to execute the activate_firmware API for port {} due to {}",
                    self.physical_str(),
                    e
                );
                "-1".to_string()
            }
        };
        self.respond(cmd, &status).await;
    }

    /// `config muxcable firmware rollback`.
    async fn cli_rollback_firmware(&mut self, cmd: &'static Cmd, input: Option<&str>) {
        let Some(name) = input else {
            log_error!(
                "Wrong param for cli cmd mux rollback firmware port {}",
                self.port
            );
            return self.respond(cmd, "False").await;
        };
        let path = (name != "null").then(|| firmware_path(name));
        if let Some(p) = &path {
            if !is_file(p).await {
                log_error!(
                    "Error: cli cmd mux rollback firmware file does not exist port {} file {}",
                    self.port,
                    name
                );
                return self.respond(cmd, "False").await;
            }
        }
        match self.access().await {
            Access::Absent => {
                log_warning!(
                    "Error: Could not get physical port for cli cmd mux rollback firmware port {}",
                    self.port
                );
                return self.respond(cmd, "False").await;
            }
            Access::NoDriver => {
                // Python answers, then dies on the missing port lock; here
                // the answer is all.
                log_warning!(
                    "Error: Could not get port instance for cli cmd mux rollback firmware port {}",
                    self.port
                );
                return self.respond(cmd, "False").await;
            }
            Access::Ready => {}
        }
        let status = self
            .status_of_call(
                "rollback_firmware",
                vec![path_arg(&path)],
                "rollback_firmware API",
            )
            .await;
        self.respond(cmd, &status).await;
    }

    // --- config muxcable prbs/reset/anlt/fec, loopback ---------------------------

    /// `config muxcable prbs enable|disable`, `config muxcable reset`,
    /// `config muxcable set-anlt`, `config muxcable set-fec`.
    async fn cli_config_prbs(&mut self, cmd: &'static Cmd, input: Option<&str>) {
        let Some(mode) = input else {
            log_error!(
                "Wrong param for cli cmd enable/disable prbs anlt/reset API port {}",
                self.port
            );
            return self.respond(cmd, "False").await;
        };
        let args = self.cli_args(cmd).await;
        let target = self.int_arg(&args, "target").ok().flatten();
        let access = self.access().await;
        let Some(target) = target.filter(|_| access != Access::Absent) else {
            log_warning!(
                "Error: Could not get physical port or correct args for cli cmd enable/disable prbs anlt/reset port {}",
                self.port
            );
            return self.respond(cmd, "False").await;
        };
        if access == Access::NoDriver {
            log_warning!(
                "Error: Could not get port instance for cli cmd enable/disable prbs anlt/reset port {}",
                self.port
            );
            return self.respond(cmd, "False").await;
        }
        let (method, margs, what) = match mode {
            "enable" => {
                let mode_value = self.int_arg(&args, "mode_value").ok().flatten();
                let lane_mask = self.int_arg(&args, "lane_mask").ok().flatten();
                let Ok(direction) = self.int_arg(&args, "direction") else {
                    return self.respond(cmd, "False").await;
                };
                let (Some(mode_value), Some(lane_mask)) = (mode_value, lane_mask) else {
                    log_warning!(
                        "Error: Could not get correct args lan_mask/mode_value for cli cmd enable prbs port {}",
                        self.port
                    );
                    return self.respond(cmd, "False").await;
                };
                let direction = match direction {
                    Some(d) => d,
                    None => self.driver_const("PRBS_DIRECTION_BOTH", 0).await,
                };
                let margs = vec![
                    PyArg::Int(target),
                    PyArg::Int(mode_value),
                    PyArg::Int(lane_mask),
                    PyArg::Int(direction),
                ];
                ("enable_prbs_mode", margs, "enable prbs API")
            }
            "disable" => {
                let Ok(direction) = self.int_arg(&args, "direction") else {
                    return self.respond(cmd, "False").await;
                };
                let direction = match direction {
                    Some(d) => d,
                    None => self.driver_const("PRBS_DIRECTION_BOTH", 0).await,
                };
                (
                    "disable_prbs_mode",
                    vec![PyArg::Int(target), PyArg::Int(direction)],
                    "disable prbs API",
                )
            }
            "reset" => ("reset", vec![PyArg::Int(target)], "reset API"),
            "anlt" => {
                let Some(enable) = self.int_arg(&args, "mode").ok().flatten() else {
                    log_warning!(
                        "Error: Could not get correct args (enable) for cli cmd set anlt port {}",
                        self.port
                    );
                    return self.respond(cmd, "False").await;
                };
                (
                    "set_anlt",
                    vec![PyArg::Int(enable), PyArg::Int(target)],
                    "set_anlt API",
                )
            }
            "fec" => {
                let Some(fec_mode) = self.int_arg(&args, "mode").ok().flatten() else {
                    log_warning!(
                        "Error: Could not get correct args (enable) for cli cmd set fec port {}",
                        self.port
                    );
                    return self.respond(cmd, "False").await;
                };
                (
                    "set_fec_mode",
                    vec![PyArg::Int(fec_mode), PyArg::Int(target)],
                    "set_fec_mode API",
                )
            }
            _ => {
                log_warning!(
                    "Error: Wrong mode {} for cli cmd enable/disable prbs anlt/reset port {}",
                    mode,
                    self.port
                );
                return self.respond(cmd, "False").await;
            }
        };
        let status = self.status_of_call(method, margs, what).await;
        self.respond(cmd, &status).await;
    }

    /// `config muxcable loopback enable|disable`.
    async fn cli_config_loopback(&mut self, cmd: &'static Cmd, input: Option<&str>) {
        let Some(mode) = input else {
            log_error!(
                "Wrong param for cli cmd enable/disable loopback port {}",
                self.port
            );
            return self.respond(cmd, "False").await;
        };
        let args = self.cli_args(cmd).await;
        let target = self.int_arg(&args, "target").ok().flatten();
        let access = self.access().await;
        let Some(target) = target.filter(|_| access != Access::Absent) else {
            log_warning!(
                "Error: Could not get physical port or correct args for cli cmd enable enable/disable loopback {}",
                self.port
            );
            return self.respond(cmd, "False").await;
        };
        if access == Access::NoDriver {
            log_warning!(
                "Error: Could not get port instance for cli cmd enable/disable loopback mode port {}",
                self.port
            );
            return self.respond(cmd, "False").await;
        }
        let (method, margs) = match mode {
            "enable" => {
                let Ok(mode_value) = self.int_arg(&args, "mode_value") else {
                    return self.respond(cmd, "False").await;
                };
                let Some(lane_mask) = self.int_arg(&args, "lane_mask").ok().flatten() else {
                    log_warning!(
                        "Error: Could not get physical port or correct args for cli cmd enable loopback port {}",
                        self.port
                    );
                    return self.respond(cmd, "False").await;
                };
                let mode_value = match mode_value {
                    Some(m) => m,
                    None => self.driver_const("LOOPBACK_MODE_NEAR_END", 1).await,
                };
                (
                    "enable_loopback_mode",
                    vec![
                        PyArg::Int(target),
                        PyArg::Int(mode_value),
                        PyArg::Int(lane_mask),
                    ],
                )
            }
            "disable" => ("disable_loopback_mode", vec![PyArg::Int(target)]),
            _ => {
                log_warning!(
                    "Error: Wrong mode {} for cli cmd enable/disable loopback port {}",
                    mode,
                    self.port
                );
                return self.respond(cmd, "False").await;
            }
        };
        let status = self
            .status_of_call(method, margs, "enable/disable loopback API")
            .await;
        self.respond(cmd, &status).await;
    }

    // --- show muxcable event_log, get_fec_anlt_speed, ber family -----------------

    /// `show muxcable event-log`: one result field per event.
    async fn cli_show_event_log(&mut self, cmd: &'static Cmd, input: Option<&str>) {
        if input.is_none() {
            log_error!("Wrong param for cli cmd event log API port {}", self.port);
            return self.respond(cmd, "False").await;
        }
        match self.access().await {
            Access::Absent => {
                log_warning!(
                    "Error: Could not get physical port or correct args for cli cmd event log port {}",
                    self.port
                );
                return self.respond(cmd, "False").await;
            }
            Access::NoDriver => {
                log_warning!(
                    "Error: Could not get port instance for cli cmd event log port {}",
                    self.port
                );
                return self.respond(cmd, "False").await;
            }
            Access::Ready => {}
        }
        let Some(events) = self
            .drv_or_none("get_event_log", vec![], "event log API")
            .await
        else {
            return self.respond(cmd, "-1").await;
        };
        let res = result_fields(&events, &Shape::List);
        for (_, event) in &res {
            log_notice!("event log for cable {} port {}", event, self.port);
        }
        self.write_result(cmd, &res).await;
        self.respond(cmd, "True").await;
    }

    /// `show muxcable get-fec-anlt-speed`: all seven values, or none.
    async fn cli_show_fec(&mut self, cmd: &'static Cmd, input: Option<&str>) {
        if input.is_none() {
            log_error!(
                "Wrong param for cli cmd get_fec_eye_anlt port {}",
                self.port
            );
            return self.respond(cmd, "False").await;
        }
        match self.access().await {
            Access::Absent => {
                log_warning!(
                    "Error: Could not get physical port or correct args for cli cmd get_fec_eye_anlt port {}",
                    self.port
                );
                return self.respond(cmd, "False").await;
            }
            Access::NoDriver => {
                log_warning!(
                    "Error: Could not get port instance for cli cmd get_fec_eye_anlt port {}",
                    self.port
                );
                return self.respond(cmd, "False").await;
            }
            Access::Ready => {}
        }
        let reads: [(&str, &str, Option<i64>); 7] = [
            ("fec_nic", "get_fec_mode", Some(TARGET_NIC)),
            ("fec_tor_a", "get_fec_mode", Some(TARGET_TOR_A)),
            ("fec_tor_b", "get_fec_mode", Some(TARGET_TOR_B)),
            ("speed", "get_speed", None),
            ("anlt_nic", "get_anlt", Some(TARGET_NIC)),
            ("anlt_tor_a", "get_anlt", Some(TARGET_TOR_A)),
            ("anlt_tor_b", "get_anlt", Some(TARGET_TOR_B)),
        ];
        let mut res = Vec::new();
        for (field, method, target) in reads {
            let margs = target.map(|t| vec![PyArg::Int(t)]).unwrap_or_default();
            match self.drv(method, margs).await {
                Ok(v) => res.push((field.to_string(), v.py_str())),
                Err(e) => {
                    log_warning!(
                        "Failed to execute the get_fec_eye_anlt API for port {} due to {}",
                        self.physical_str(),
                        e
                    );
                    return self.respond(cmd, "-1").await;
                }
            }
        }
        self.write_result(cmd, &res).await;
        self.respond(cmd, "True").await;
    }

    /// `show muxcable berinfo/eyeinfo/fecstatistics/pcsstatistics/alivecablestatus/
    /// health/resetcause/operationtime/debugdumpregisters/queueinfo`.
    async fn cli_show_ber(&mut self, cmd: &'static Cmd, input: Option<&str>) {
        let Some(mode) = input else {
            log_error!(
                "Wrong param for cli cmd debug_dump/cli_event/fec_stats API port {}",
                self.port
            );
            return self.respond(cmd, "False").await;
        };
        let args = self.cli_args(cmd).await;
        let target = self.int_arg(&args, "target");
        match self.access().await {
            Access::Absent => {
                log_warning!(
                    "Error: Could not get physical port or correct args for cli cmd fec port {}",
                    self.port
                );
                return self.respond(cmd, "False").await;
            }
            Access::NoDriver => {
                log_warning!(
                    "Error: Could not get port instance for cli cmd debug_dump/cli_event/fec_stats {}",
                    self.port
                );
                return self.respond(cmd, "False").await;
            }
            Access::Ready => {}
        }
        let Ok(target) = target else {
            return self.respond(cmd, "False").await;
        };
        let (method, margs, shape, what) = match mode {
            "ber" | "eye" | "fec_stats" | "pcs_stats" => {
                let Some(t) = target else {
                    log_warning!(
                        "Error: Could not get target or correct args for cli cmd {} port {}",
                        mode,
                        self.port
                    );
                    return self.respond(cmd, "False").await;
                };
                let margs = vec![PyArg::Int(t)];
                match mode {
                    "ber" => ("get_ber_info", margs, Shape::List, "get_ber_info API"),
                    "eye" => ("get_eye_heights", margs, Shape::List, "eye_heights API"),
                    "fec_stats" => ("get_fec_stats", margs, Shape::Dict, "get_fec_stats API"),
                    _ => ("get_pcs_stats", margs, Shape::Dict, "get_pcs_stats API"),
                }
            }
            "cable_alive" => (
                "get_alive_status",
                vec![],
                Shape::Field("cable_alive"),
                "get_alive_status API",
            ),
            "health_check" => (
                "health_check",
                vec![],
                Shape::Field("health_check"),
                "get_health API",
            ),
            "reset_cause" => (
                "reset_cause",
                vec![],
                Shape::Field("reset_cause"),
                "reset cause cmd API",
            ),
            "operation_time" => (
                "operation_time",
                vec![],
                Shape::Field("operation_time"),
                "operation time cmd API",
            ),
            "debug_dump" => {
                let option = args
                    .get("option")
                    .map(|o| PyArg::Str(o.clone()))
                    .unwrap_or(PyArg::None);
                (
                    "debug_dump_registers",
                    vec![option],
                    Shape::Dict,
                    "debug_dump API",
                )
            }
            "queue_info" => ("queue_info", vec![], Shape::Dict, "queue_info API"),
            _ => {
                log_warning!(
                    "Error: Wrong mode {} for cli cmd debug_dump/cli_event/fec_stats API port {}",
                    mode,
                    self.port
                );
                return self.respond(cmd, "False").await;
            }
        };
        let Some(value) = self.drv_or_none(method, margs, what).await else {
            return self.respond(cmd, "-1").await;
        };
        self.write_result(cmd, &result_fields(&value, &shape)).await;
        self.respond(cmd, "True").await;
    }

    // --- Shared by the commands ---------------------------------------------------

    /// Python's set_result_and_delete_port.
    async fn respond(&self, cmd: &Cmd, value: &str) {
        respond(self.db(), cmd, &self.port, value).await
    }

    /// The result row, in one write, before the response.
    async fn write_result(&self, cmd: &Cmd, fields: &[(String, String)]) {
        let Some(res) = cmd.res else { return };
        if fields.is_empty() {
            return;
        }
        let fv: Vec<(&str, String)> = fields
            .iter()
            .map(|(k, v)| (k.as_str(), v.clone()))
            .collect();
        self.db().set(DbId::State, res, &self.port, &fv).await
    }

    /// The command's argument row; empty without one.
    async fn cli_args(&self, cmd: &Cmd) -> FieldMap {
        match cmd.arg {
            Some(table) => self
                .db()
                .get(DbId::Appl, table, &self.port)
                .await
                .unwrap_or_default(),
            None => FieldMap::new(),
        }
    }

    /// Python's `int()` of an argument: Ok(None) without it, Err (logged) if
    /// it is not an integer.
    fn int_arg(&self, args: &FieldMap, name: &str) -> Result<Option<i64>, ()> {
        match args.get(name) {
            None => Ok(None),
            Some(v) => match parse_int(v) {
                Some(i) => Ok(Some(i)),
                None => {
                    log_warning!(
                        "Error: cli cmd argument {} of port {} is not an integer: {}",
                        name,
                        self.port,
                        v
                    );
                    Err(())
                }
            },
        }
    }

    /// Python's two lookups before a command calls the cable: the physical
    /// port of a cable that is present, then its driver.
    async fn access(&self) -> Access {
        if self.physical.is_none() {
            log_error!("Invalid port '{}'", self.port);
            return Access::Absent;
        }
        if !self.cli_present().await {
            log_warning!(
                "Error: Could not establish presence for  Y cable port {} while retreiving physical port mapping",
                self.port
            );
            return Access::Absent;
        }
        if !self.cli_has_driver().await {
            log_error!(
                "Error: Could not get port instance from the dict for Y cable port {}",
                self.port
            );
            return Access::NoDriver;
        }
        Access::Ready
    }

    /// Presence as the CLI reports it: the platform API for an active-standby
    /// cable, as in Python; TRANSCEIVER_INFO otherwise (D3), which does not
    /// need the embedded interpreter.
    async fn cli_present(&self) -> bool {
        match self.cable {
            Some(CableType::ActiveStandby) => self.cable_present().await,
            _ => self.physical.is_some() && self.transceiver_present().await,
        }
    }

    /// Only active-standby ports have a driver.
    async fn cli_has_driver(&self) -> bool {
        self.cable == Some(CableType::ActiveStandby) && self.has_driver().await
    }

    /// A constant of the driver, or YCableBase's value if it cannot be read.
    async fn driver_const(&self, name: &str, default: i64) -> i64 {
        let Some(p) = self.physical else {
            return default;
        };
        match self.shared.platform.attr(p, name).await {
            Ok(v) => v.as_int().unwrap_or(default),
            Err(_) => default,
        }
    }

    /// A driver call whose return value is the response: `str()` of it, or
    /// -1 if the driver raised.
    async fn status_of_call(&self, method: &str, args: Vec<PyArg>, what: &str) -> String {
        match self.drv_or_none(method, args, what).await {
            Some(v) => status_of(&v, what, &self.port),
            None => "-1".to_string(),
        }
    }
}

/// Python's set_result_and_delete_port: the response field, then the command
/// key deleted, which tells the CLI the command is done.
async fn respond(db: &dyn Db, cmd: &Cmd, port: &str, value: &str) {
    log_debug!(
        "Y_CABLE_DEBUG: answering cli command {} for port {} with {}={}",
        cmd.table,
        port,
        cmd.output,
        value
    );
    db.set(
        DbId::State,
        cmd.rsp,
        port,
        &[(cmd.output, value.to_string())],
    )
    .await;
    db.del(DbId::Appl, cmd.table, port).await;
}

/// A driver's return value as a response: Python's `str()` of it, whatever
/// its type. Drivers return an int or a bool.
fn status_of(v: &PyVal, what: &str, port: &str) -> String {
    if !v.is_int() {
        log_debug!(
            "Unexpected return value {} of the {} for port {}",
            v.py_str(),
            what,
            port
        );
    }
    v.py_str()
}

/// The result fields of a driver's return value; none if it is not of the
/// expected type.
fn result_fields(v: &PyVal, shape: &Shape) -> Vec<(String, String)> {
    match shape {
        Shape::List if v.is_list() => v
            .items()
            .unwrap_or(&[])
            .iter()
            .enumerate()
            .map(|(i, x)| (i.to_string(), x.py_str()))
            .collect(),
        Shape::Dict => v
            .dict_items()
            .unwrap_or(&[])
            .iter()
            .map(|(k, x)| (k.py_str(), x.py_str()))
            .collect(),
        Shape::Field(name) if !v.is_none() => vec![(name.to_string(), v.py_str())],
        _ => Vec::new(),
    }
}

/// Python's `int()` of a string, for base 10.
fn parse_int(s: &str) -> Option<i64> {
    s.trim().parse::<i64>().ok()
}

fn py_bool(b: bool) -> &'static str {
    if b {
        "True"
    } else {
        "False"
    }
}

fn firmware_path(name: &str) -> String {
    #[cfg(not(test))]
    let dir = FIRMWARE_DIR.to_string();
    #[cfg(test)]
    let dir = test_firmware_dir().to_string_lossy().into_owned();
    format!("{}/{}", dir, name)
}

/// The unit tests' firmware directory, instead of FIRMWARE_DIR.
#[cfg(test)]
pub fn test_firmware_dir() -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("ycabled-rs-test-firmware-{}", std::process::id()));
    let _ = std::fs::create_dir_all(&dir);
    dir
}

/// The image argument of activate and rollback: None for "null".
fn path_arg(path: &Option<String>) -> PyArg {
    match path {
        Some(p) => PyArg::Str(p.clone()),
        None => PyArg::None,
    }
}

/// Python's `os.path.isfile`.
async fn is_file(path: &str) -> bool {
    tokio::fs::metadata(path)
        .await
        .map(|m| m.is_file())
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::CLI_COMMAND_TABLES;

    #[test]
    fn every_command_table_has_a_command() {
        let tables: Vec<&str> = COMMANDS.iter().map(|c| c.table).collect();
        assert_eq!(tables, CLI_COMMAND_TABLES.to_vec());
        for c in &COMMANDS {
            assert!(c.rsp.ends_with("_RSP"), "{}", c.table);
            assert!(c.res.map_or(true, |r| r.ends_with("_RES")), "{}", c.table);
            assert!(
                c.arg.map_or(true, |a| a == format!("{}_ARG", c.table)),
                "{}",
                c.table
            );
        }
    }

    #[test]
    fn int_arguments_as_python_reads_them() {
        assert_eq!(parse_int("15"), Some(15));
        assert_eq!(parse_int(" 2 "), Some(2));
        assert_eq!(parse_int("-1"), Some(-1));
        assert_eq!(parse_int("+3"), Some(3));
        assert_eq!(parse_int("0x10"), None);
        assert_eq!(parse_int("1.0"), None);
        assert_eq!(parse_int(""), None);
    }

    #[test]
    fn status_is_str_of_the_return_value() {
        assert_eq!(status_of(&PyVal::Int(0), "x", "Ethernet0"), "0");
        assert_eq!(status_of(&PyVal::Int(-1), "x", "Ethernet0"), "-1");
        assert_eq!(status_of(&PyVal::Bool(true), "x", "Ethernet0"), "True");
        assert_eq!(status_of(&PyVal::Bool(false), "x", "Ethernet0"), "False");
        assert_eq!(status_of(&PyVal::None, "x", "Ethernet0"), "None");
        assert_eq!(status_of(&PyVal::str_("ok"), "x", "Ethernet0"), "ok");
    }

    #[test]
    fn result_fields_by_shape() {
        let list = PyVal::list(vec![PyVal::float(2e-9), PyVal::float(4e-9)]);
        assert_eq!(
            result_fields(&list, &Shape::List),
            vec![
                ("0".to_string(), "2e-09".to_string()),
                ("1".to_string(), "4e-09".to_string())
            ]
        );
        assert!(result_fields(&PyVal::str_("busy"), &Shape::List).is_empty());
        let dict = PyVal::dict(vec![
            (PyVal::str_("corrected"), PyVal::Int(10)),
            (PyVal::str_("uncorrected"), PyVal::Int(0)),
        ]);
        assert_eq!(
            result_fields(&dict, &Shape::Dict),
            vec![
                ("corrected".to_string(), "10".to_string()),
                ("uncorrected".to_string(), "0".to_string())
            ]
        );
        assert!(result_fields(&list, &Shape::Dict).is_empty());
        assert_eq!(
            result_fields(&PyVal::Bool(true), &Shape::Field("cable_alive")),
            vec![("cable_alive".to_string(), "True".to_string())]
        );
        assert!(result_fields(&PyVal::None, &Shape::Field("cable_alive")).is_empty());
    }
}
