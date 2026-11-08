//! Active-active cables: the NIC SoC over gRPC (HLD 3.4.3 to 3.4.6, 3.4.8).

use std::sync::atomic::Ordering;

use crate::config;
use crate::db::{DbId, FieldMap};
use crate::grpc::{self, log_rpc_error};
use crate::schema::*;
use crate::{log_debug, log_notice, log_warning};

use super::{py_bools, py_ints, PortActor};

impl PortActor {
    /// The SoC address in MUX_CABLE; logs why there is none.
    pub(crate) async fn soc_address(&mut self) -> Option<std::net::IpAddr> {
        let row = self.mux_cable_row().await?;
        if row.contains_key("state") {
            if let Ok(Some(ip)) = config::soc_ip(&row) {
                self.soc_ip = Some(ip);
                return Some(ip);
            }
        }
        log_warning!(
            "No SoC IP address found for gRPC channel setup on port {}",
            self.port
        );
        None
    }

    pub(crate) async fn setup_channel(&mut self, ip: std::net::IpAddr) -> bool {
        match self
            .shared
            .soc
            .connect(&self.port, ip, self.shared.db.clone())
            .await
        {
            Some(soc) => {
                self.soc = Some(soc);
                true
            }
            None => false,
        }
    }

    /// A request found no channel: set it up again from CONFIG_DB, as
    /// Python's retry_setup_grpc_channel_for_port does.
    pub(crate) async fn retry_channel(&mut self) -> bool {
        let Some(ip) = self.soc_address().await else {
            return false;
        };
        if self.setup_channel(ip).await {
            true
        } else {
            log_notice!(
                "stub is None, while reattempt setting up channels did not work {}",
                self.port
            );
            false
        }
    }

    /// Returns true once the port has a SoC address, which sets Python's
    /// `y_cable_presence`; see [`Msg::Init`](super::Msg::Init) for `defer`.
    pub(super) async fn init_active(&mut self, defer: bool) -> bool {
        let Some(ip) = self.soc_address().await else {
            return false;
        };
        if !defer {
            self.shared.ycable_presence.store(true, Ordering::SeqCst);
        }
        let read_side = self.shared.read_side;
        if self.physical.is_none() {
            log_warning!(
                "DAC cable logical to physical port mapping returned more than one physical ports while Channel setup Port {}",
                self.port
            );
            return true;
        }
        if self.transceiver_present().await {
            self.present = true;
            if self.soc.is_none() {
                self.setup_channel(ip).await;
                self.write_placeholder_info().await;
                if self.soc.is_some() {
                    log_notice!(
                        "channel is not None, Cable-Insert or daemon init, daemon able to set up channel for gRPC SOC IP {}, port {}",
                        ip,
                        self.port
                    );
                    log_notice!(
                        "stub is not None, Cable-Insert or daemon init, daemon able to set up channel for gRPC SOC IP {}, port {}",
                        ip,
                        self.port
                    );
                }
            }
        } else {
            self.present = false;
            log_warning!(
                "DAC cable not present while Channel setup Port {} for gRPC channel initiation",
                self.port
            );
        }
        self.put_initial_states(read_side).await;
        true
    }

    /// HW_MUX_CABLE_TABLE and HW_MUX_CABLE_TABLE_PEER from the SoC's
    /// forwarding states.
    async fn put_initial_states(&mut self, read_side: i32) {
        let rs = read_side.to_string();
        let Some(soc) = self.soc.clone() else {
            log_notice!(
                "stub is None for getting admin port forwarding state RPC port {} tid {} writing unknown",
                self.port,
                "ycabled-rs"
            );
            self.write_mux_state_to(HW_MUX_CABLE_TABLE, "unknown", &rs, "unknown")
                .await;
            self.write_mux_state_to(HW_MUX_CABLE_TABLE_PEER, "unknown", &rs, "unknown")
                .await;
            return;
        };
        let reply = soc.query_admin_forwarding().await;
        if let Err(e) = &reply {
            log_rpc_error(e);
        }
        let (self_state, peer_state) = grpc::parse_forwarding_state(&reply, read_side);
        self.counts.bump(&[
            "mux_direction_probe_count",
            "peer_mux_direction_probe_count",
        ]);
        match &reply {
            Ok(r) => {
                log_notice!(
                    "initial forwarding state RPC received response port ids = {} port {} tid {}",
                    py_ints(&r.portid),
                    self.port,
                    "ycabled-rs"
                );
                log_notice!(
                    "initial forwarding state RPC received response state values = {} port {} tid {}",
                    py_bools(&r.state),
                    self.port,
                    "ycabled-rs"
                );
            }
            Err(_) => log_warning!(
                "response was none while doing init config state for gRPC HW_MUX_CABLE_TABLE {} ",
                self.port
            ),
        }
        self.write_mux_state_to(HW_MUX_CABLE_TABLE, self_state, &rs, self_state)
            .await;
        self.write_mux_state_to(HW_MUX_CABLE_TABLE_PEER, peer_state, &rs, peer_state)
            .await;
    }

    /// orchagent (self) or linkmgrd (peer) asks for a forwarding state: set it
    /// on the SoC and write the result to HW_MUX_CABLE_TABLE(_PEER).
    pub(super) async fn toggle_active(&mut self, f: &FieldMap, peer: bool) {
        let time_start = timestamp();
        let (table, side) = if peer {
            (HW_MUX_CABLE_TABLE_PEER, "peer_")
        } else {
            (HW_MUX_CABLE_TABLE, "self_")
        };
        log_debug!("Y_CABLE_DEBUG:recevied the notification mux hw state");
        let Some(new_state) = f.get("state") else {
            log_notice!(
                "Got a change event on port {} of table {} that does not contain state",
                self.port,
                HW_MUX_CABLE_TABLE
            );
            return;
        };
        if new_state != "active" && new_state != "standby" {
            return;
        }
        let Some(row) = self.db().get(DbId::State, table, &self.port).await else {
            log_debug!(
                "Could not retreive fieldvalue pairs for {}, inside state_db table {}",
                self.port,
                table
            );
            return;
        };
        let old_state = row.get("state").cloned().unwrap_or_else(|| "None".into());
        let read_side = row.get("read_side").cloned().unwrap_or_else(|| "-1".into());
        let rs: i32 = read_side.trim().parse().unwrap_or(-1);
        let portid = if peer { 1 - rs } else { rs };
        log_notice!(
            "calling RPC for hw mux_cable set state ispeer = {} port {} portid {} read_side {} state requested {}",
            if peer { "True" } else { "False" },
            self.port,
            portid,
            read_side,
            new_state
        );

        let active_side: &str = if !(rs == 0 || rs == 1) {
            // Without Loopback3 this ToR does not know its index in the SoC's
            // lists, so it toggles neither side.
            log_warning!(
                "Could not toggle the mux-direction of port {}: the read side {} is not known",
                self.port,
                read_side
            );
            "unknown"
        } else {
            if self.soc.is_none() {
                log_debug!(
                    "Y_CABLE_DEBUG:stub is None for performing hw mux RPC port {}",
                    self.port
                );
                self.retry_channel().await;
            }
            match self.soc.clone() {
                None => {
                    log_warning!(
                        "gRPC channel was initially not setup for performing hw mux set state RPC port {}, trying to set gRPC channel again also did not work, posting unknown state for stateDB:HW_MUX_CABLE_TABLE",
                        self.port
                    );
                    "unknown"
                }
                Some(soc) => {
                    let reply = soc
                        .set_admin_forwarding(portid, new_state == "active")
                        .await;
                    match &reply {
                        Ok(r) => {
                            log_notice!(
                                "Set admin state RPC received response port {} port ids = {} curr_read_side {} read_side {} tid {}",
                                self.port,
                                py_ints(&r.portid),
                                portid,
                                read_side,
                                "ycabled-rs"
                            );
                            log_notice!(
                                "Set admin state RPC received response port {} state values = {} curr_read_side {} read_side {} tid {}",
                                self.port,
                                py_bools(&r.state),
                                portid,
                                read_side,
                                "ycabled-rs"
                            );
                        }
                        Err(e) => {
                            log_rpc_error(e);
                            log_notice!(
                                "response was none hw_mux_cable_table_grpc_notification {} tid {}",
                                self.port,
                                "ycabled-rs"
                            );
                        }
                    }
                    grpc::parse_set_reply(&reply, portid, &self.port)
                }
            }
        };
        let new_state = if active_side == "unknown" {
            log_warning!(
                "ERR: Got a change event for updating gRPC but could not toggle the mux-direction for port {} state from {} to {}, writing unknown",
                self.port,
                old_state,
                new_state
            );
            "unknown"
        } else {
            new_state.as_str()
        };
        let time_end = timestamp();
        self.write_metrics(side, new_state, &time_start, &time_end)
            .await;
        self.write_mux_state_to(table, new_state, &read_side, active_side)
            .await;
    }

    /// linkmgrd's forwarding state probe: both sides' states from the SoC, in
    /// FORWARDING_STATE_RESPONSE.
    pub(super) async fn forwarding_probe(&mut self, f: &FieldMap) {
        log_debug!(
            "Y_CABLE_DEBUG:recevied the notification fwd state port {}",
            self.port
        );
        match f.get("command").map(String::as_str) {
            Some("probe") => {}
            Some(_) => {
                log_warning!(
                    "probe val not present in the notification fwd state handling port {}",
                    self.port
                );
                return;
            }
            None => {
                log_warning!(
                    "command key not present in the notification fwd state handling port {}",
                    self.port
                );
                return;
            }
        }
        let Some(row) = self
            .db()
            .get(DbId::State, HW_MUX_CABLE_TABLE, &self.port)
            .await
        else {
            log_debug!(
                "Could not retreive fieldvalue pairs for {}, inside state_db table {}",
                self.port,
                HW_MUX_CABLE_TABLE
            );
            return;
        };
        let read_side: i32 = row
            .get("read_side")
            .and_then(|v| v.trim().parse().ok())
            .unwrap_or(-1);
        if self.soc.is_none() {
            log_notice!(
                "stub is None for getting admin port forwarding state RPC port {} tid ",
                self.port
            );
            self.retry_channel().await;
        }
        let Some(soc) = self.soc.clone() else {
            log_warning!(
                "stub was None for performing fwd mux RPC port {}, setting it up again did not work",
                self.port
            );
            self.write_forwarding_response("unknown", "unknown").await;
            return;
        };
        let reply = soc.query_admin_forwarding().await;
        if let Err(e) = &reply {
            log_rpc_error(e);
            log_notice!(
                "response was none handle_fwd_state_command_grpc_notification {} ",
                self.port
            );
        }
        let (self_state, peer_state) = grpc::parse_forwarding_state(&reply, read_side);
        self.counts.bump(&[
            "mux_direction_probe_count",
            "peer_mux_direction_probe_count",
        ]);
        self.write_forwarding_response(self_state, peer_state).await;
        log_debug!("Y_CABLE_DEBUG:processed the notification fwd state cleanly");
    }

    async fn write_forwarding_response(&self, response: &str, response_peer: &str) {
        let f = vec![
            ("response", response.to_string()),
            ("response_peer", response_peer.to_string()),
        ];
        self.db()
            .set(DbId::Appl, FORWARDING_STATE_RESPONSE, &self.port, &f)
            .await
    }

    /// MUX_CABLE_INFO of an active-active port: link, operation and
    /// forwarding states, the server version and the channel state.
    pub(super) async fn telemetry_active(&mut self) {
        let epoch = self.current_epoch();
        if !self.transceiver_present().await {
            self.write_placeholder_info().await;
            return;
        }
        let time_post = timestamp();
        let Some((_, read_side)) = self.stored_read_side().await else {
            log_debug!(
                "Could not retreive fieldvalue pairs for {}, inside state_db table {}",
                self.port,
                HW_MUX_CABLE_TABLE
            );
            return;
        };
        let read_side = read_side as i32;
        // The counts before this pass.
        let count_names = [
            "mux_direction_probe_count",
            "peer_mux_direction_probe_count",
            "link_state_probe_count",
            "peer_link_state_probe_count",
            "operation_state_probe_count",
            "peer_operation_state_probe_count",
        ];
        let counts: Vec<String> = count_names.iter().map(|n| self.counts.get(n)).collect();
        let (mut self_link, mut peer_link, mut self_oper, mut peer_oper) =
            ("unknown", "unknown", "unknown", "unknown");
        let (mut self_mux, mut peer_mux) = ("unknown", "unknown");
        let mut version = "N/A".to_string();
        let mut status = "unknown".to_string();
        if let Some(soc) = self.soc.clone() {
            let link = soc.query_link_state().await;
            if let Err(e) = &link {
                log_rpc_error(e);
            }
            (self_link, peer_link) = grpc::parse_link_state(
                link.as_ref()
                    .ok()
                    .map(|r| (r.portid.as_slice(), r.state.as_slice())),
                read_side,
                "link_state",
            );
            self.counts
                .bump(&["link_state_probe_count", "peer_link_state_probe_count"]);
            let oper = soc.query_operation_state().await;
            if let Err(e) = &oper {
                log_rpc_error(e);
            }
            (self_oper, peer_oper) = grpc::parse_link_state(
                oper.as_ref()
                    .ok()
                    .map(|r| (r.portid.as_slice(), r.state.as_slice())),
                read_side,
                "oper_state",
            );
            self.counts.bump(&[
                "operation_state_probe_count",
                "peer_operation_state_probe_count",
            ]);
            let admin = soc.query_admin_forwarding().await;
            if let Err(e) = &admin {
                log_rpc_error(e);
            }
            (self_mux, peer_mux) = grpc::parse_forwarding_state(&admin, read_side);
            self.counts.bump(&[
                "mux_direction_probe_count",
                "peer_mux_direction_probe_count",
            ]);
            match soc.query_server_version().await {
                Ok(v) => version = v.version,
                Err(e) => log_rpc_error(&e),
            }
            status = soc.connectivity();
        }
        let f: Vec<(&str, String)> = vec![
            ("self_link_state", self_link.into()),
            ("peer_link_state", peer_link.into()),
            ("self_oper_state", self_oper.into()),
            ("peer_oper_state", peer_oper.into()),
            ("server_version", version),
            ("time_post", time_post),
            ("self_mux_direction", self_mux.into()),
            ("peer_mux_direction", peer_mux.into()),
            ("peer_mux_direction_probe_count", counts[1].clone()),
            ("mux_direction_probe_count", counts[0].clone()),
            ("link_state_probe_count", counts[2].clone()),
            ("peer_link_state_probe_count", counts[3].clone()),
            ("operation_state_probe_count", counts[4].clone()),
            ("peer_operation_state_probe_count", counts[5].clone()),
            ("grpc_connection_status", status),
        ];
        if self.current_epoch() != epoch {
            return;
        }
        self.db()
            .set(DbId::State, MUX_CABLE_INFO, &self.port, &f)
            .await;
    }
}
