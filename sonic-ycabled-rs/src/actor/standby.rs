//! Active-standby cables: the vendor driver through the Y-cable bridge
//! (HLD 3.4.1, 3.4.2, 3.4.7, 3.4.8).

use std::sync::atomic::Ordering;

use crate::bridge::CallError;
use crate::db::{DbId, FieldMap};
use crate::pyval::{PyArg, PyVal};
use crate::schema::*;
use crate::{log_debug, log_error, log_info, log_notice, log_warning};

use super::PortActor;

pub const TARGET_NIC: i64 = 0;
pub const TARGET_TOR_A: i64 = 1;
pub const TARGET_TOR_B: i64 = 2;
pub const FIRMWARE_DOWNLOAD_STATUS_INPROGRESS: i64 = 1;
pub const FIRMWARE_DOWNLOAD_STATUS_FAILED: i64 = 2;

impl PortActor {
    /// A driver call; an exception is logged with `what` and read as None.
    pub async fn drv(&self, method: &str, args: Vec<PyArg>) -> Result<PyVal, CallError> {
        let Some(p) = self.physical else {
            return Err(CallError::NoDriver);
        };
        let _call = self.call_lock.lock().await;
        self.shared.platform.call(p, method, args).await
    }

    /// Like [`drv`](Self::drv), logging a raised exception as Python does:
    /// "Failed to execute the <what> for port <physical> due to <repr>".
    pub async fn drv_or_none(&self, method: &str, args: Vec<PyArg>, what: &str) -> Option<PyVal> {
        match self.drv(method, args).await {
            Ok(v) => Some(v),
            Err(CallError::Raised(e)) => {
                log_warning!(
                    "Failed to execute the {} for port {} due to {}",
                    what,
                    self.physical_str(),
                    e
                );
                None
            }
            Err(CallError::NoDriver) => None,
        }
    }

    pub fn physical_str(&self) -> String {
        self.physical
            .map(|p| p.to_string())
            .unwrap_or_else(|| "None".to_string())
    }

    pub async fn has_driver(&self) -> bool {
        match self.physical {
            Some(p) => self.shared.platform.has_driver(p).await,
            None => false,
        }
    }

    /// Unusable values: None, the driver's EEPROM_ERROR or below 0.
    pub fn bad_side(&self, v: &Option<PyVal>) -> bool {
        match v {
            None => true,
            Some(v) => {
                v.is_none() || v.eq_int(self.eeprom_error) || v.as_number().is_none() || v.lt_int(0)
            }
        }
    }

    pub async fn download_in_progress_status(&self) -> Option<i64> {
        let p = self.physical?;
        let _call = self.call_lock.lock().await;
        self.shared
            .platform
            .attr(p, "download_firmware_status")
            .await
            .ok()
            .and_then(|v| v.as_int())
    }

    // --- Initialization ---------------------------------------------------------

    /// Returns whether the driver was loaded, which sets Python's
    /// `y_cable_presence`; see [`Msg::Init`](super::Msg::Init) for `defer`.
    pub(super) async fn init_standby(&mut self, defer: bool) -> bool {
        let Some(physical) = self.physical else {
            self.warn_not_in_port_map();
            return false;
        };
        if !self.cable_present().await {
            log_warning!(
                "Error: Could not establish transceiver presence for a Y cable port {} while inserting entries",
                self.port
            );
            self.present = false;
            self.initialized = false;
            self.insert_unknown_entries().await;
            return false;
        }
        self.present = true;
        // Under the call lock: a telemetry pass may still be calling the
        // cable through its previous driver, and the new driver's constructor
        // and get_vendor() talk to the same cable.
        let loaded = {
            let _call = self.call_lock.lock().await;
            match self.shared.platform.load(&self.port, physical).await {
                None => None,
                Some(initialized) => {
                    let eeprom_error =
                        match self.shared.platform.attr(physical, "EEPROM_ERROR").await {
                            Ok(v) => v.as_int().unwrap_or(-1),
                            Err(_) => -1,
                        };
                    Some((initialized, eeprom_error))
                }
            }
        };
        match loaded {
            None => {
                self.initialized = false;
                self.insert_unknown_entries().await;
                false
            }
            Some((initialized, eeprom_error)) => {
                self.initialized = initialized;
                self.eeprom_error = eeprom_error;
                self.read_cable_and_update_state().await;
                if !defer && !self.shared.ycable_presence.swap(true, Ordering::SeqCst) {
                    self.post_info_standby().await;
                }
                self.post_static_info().await;
                true
            }
        }
    }

    /// Python's create_tables_and_insert_mux_unknown_entries.
    async fn insert_unknown_entries(&mut self) {
        self.read_cable_and_update_state().await;
        self.write_placeholder_info().await;
        self.post_static_info().await;
    }

    /// HW_MUX_CABLE_TABLE from the cable: its read side and mux direction.
    async fn read_cable_and_update_state(&mut self) {
        if !self.cable_present().await {
            self.write_mux_state("unknown", "-1", "-1").await;
            log_warning!(
                "Error: Could not establish presence for  Y cable port {} to perform read_y_cable update state db",
                self.port
            );
            return;
        }
        if !self.has_driver().await {
            self.write_mux_state("unknown", "-1", "-1").await;
            log_error!(
                "Error: Could not get port instance to perform read_y_cable update state db for read side for  Y cable port {}",
                self.port
            );
            return;
        }
        let read_side = self
            .drv_or_none("get_read_side", vec![], "get_read_side")
            .await;
        // A ToR never reads the cable from the NIC side: 0 is not a valid read side.
        if self.bad_side(&read_side)
            || !matches!(
                read_side.as_ref().and_then(|v| v.as_int()),
                Some(1) | Some(2)
            )
        {
            self.write_mux_state("unknown", "-1", "-1").await;
            log_error!(
                "Error: Could not establish the read side for Y cable port {} to perform read_y_cable update state db",
                self.port
            );
            return;
        }
        let read_side = read_side.unwrap();
        let rs = read_side.py_str();
        let active = self
            .drv_or_none("get_mux_direction", vec![], "get_mux_direction")
            .await;
        let active_n = active
            .as_ref()
            .filter(|v| !v.is_none())
            .and_then(|v| v.as_number());
        let Some(active_n) = active_n.filter(|n| [0.0, 1.0, 2.0].contains(n)) else {
            self.write_mux_state("unknown", &rs, "-1").await;
            log_error!(
                "Error: Could not establish the active side for Y cable port {} to perform read_y_cable update state db",
                self.port
            );
            return;
        };
        let read_n = read_side.as_number().unwrap_or(-1.0);
        let status = if read_n == active_n && (active_n == 1.0 || active_n == 2.0) {
            "active"
        } else if active_n == 1.0 || active_n == 2.0 {
            "standby"
        } else {
            log_warning!(
                "Error: Could not establish the active status for Y cable port {} to perform read_y_cable update state db",
                self.port
            );
            "unknown"
        };
        let active_s = active.unwrap().py_str();
        self.write_mux_state(status, &rs, &active_s).await;
    }

    // --- Toggle -------------------------------------------------------------------

    pub(super) async fn toggle_standby(&mut self, f: &FieldMap) {
        let time_start = timestamp();
        let Some(new_state) = f.get("state") else {
            log_info!(
                "Got a change event on port {} of table {} that does not contain state",
                self.port,
                HW_MUX_CABLE_TABLE
            );
            return;
        };
        if new_state != "active" && new_state != "standby" {
            log_warning!(
                "Got a change event for toggle the mux-direction for port {} with state {}, which is neither active nor standby; ignoring it",
                self.port,
                new_state
            );
            return;
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
        let old = row.get("state").cloned().unwrap_or_else(|| "None".into());
        let read_side = row.get("read_side").cloned().unwrap_or_else(|| "-1".into());
        log_debug!(
            "Y_CABLE_DEBUG: xcvrd trying to transition port {} from {} to {} read side {}",
            self.port,
            old,
            new_state,
            read_side
        );
        let (active_side, read_side) = self.update_tor_active_side(&read_side, new_state).await;
        let mut new_status = new_state.as_str();
        if active_side == -1 {
            log_warning!(
                "ERR: Got a change event for toggle but could not toggle the mux-direction for port {} state from {} to {}, writing unknown",
                self.port,
                old,
                new_state
            );
            new_status = "unknown";
        }
        log_notice!(
            "Got a change event for toggle the mux-direction active side for port {} state requested {} from old state {} to new state {} read_side  {} thread id {}",
            self.port,
            new_state,
            old,
            new_status,
            read_side,
            "ycabled-rs"
        );
        let time_end = timestamp();
        self.write_metrics("", new_status, &time_start, &time_end)
            .await;
        self.write_mux_state(new_status, &read_side, &active_side.to_string())
            .await;
    }

    /// (active side, read side) after toggling to `state` from the read side
    /// in HW_MUX_CABLE_TABLE, asking the cable for it if that one is not valid.
    async fn update_tor_active_side(&mut self, read_side: &str, state: &str) -> (i64, String) {
        if !self.cable_present().await {
            log_warning!(
                "Error: Could not establish presence for  Y cable port {} while trying to toggle the mux",
                self.port
            );
            return (-1, "-1".into());
        }
        match read_side.trim().parse::<i64>() {
            Ok(rs @ (1 | 2)) => (
                self.toggle_mux_direction(rs, state).await,
                read_side.to_string(),
            ),
            _ => self.toggle_and_update_read_side(state).await,
        }
    }

    async fn toggle_and_update_read_side(&mut self, state: &str) -> (i64, String) {
        if !self.has_driver().await {
            log_error!(
                "Error: Could not get port instance for read side for while processing a toggle Y cable port {} {}",
                self.physical_str(),
                "ycabled-rs"
            );
            return (-1, "-1".into());
        }
        let read_side = match self.drv("get_read_side", vec![]).await {
            Ok(v) => Some(v),
            Err(CallError::Raised(e)) => {
                log_warning!(
                    "Failed to execute the get_read_side API for port {} due to {} from update_read_side",
                    self.port,
                    e
                );
                None
            }
            Err(CallError::NoDriver) => None,
        };
        if self.bad_side(&read_side) {
            log_error!(
                "Error: Could not get read side for toggle command from orchagent Y cable port {}",
                self.port
            );
            return (-1, "-1".into());
        }
        match read_side.and_then(|v| v.as_int()) {
            Some(rs @ (1 | 2)) => (self.toggle_mux_direction(rs, state).await, rs.to_string()),
            _ => (-1, "-1".into()),
        }
    }

    /// Toggles toward `state` for a ToR that reads the cable from `read_side`.
    pub async fn toggle_mux_direction(&mut self, read_side: i64, state: &str) -> i64 {
        let to_a = (read_side == 1) == (state == "active");
        if to_a {
            self.toggle_to("toggle_mux_to_tor_a", "A", 1).await
        } else {
            self.toggle_to("toggle_mux_to_tor_b", "B", 2).await
        }
    }

    /// Python's y_cable_toggle_mux_torA/B: `side` on success, -1 otherwise.
    async fn toggle_to(&mut self, method: &str, tor: &str, side: i64) -> i64 {
        if !self.has_driver().await {
            log_error!(
                "Error: Could not get port instance for read side for  Y cable port {} {}",
                self.physical_str(),
                "ycabled-rs"
            );
            return -1;
        }
        let status = match self.drv(method, vec![]).await {
            Ok(v) => v,
            Err(e) => {
                log_warning!(
                    "Failed to execute the toggle mux ToR {} API for port {} due to {} {}",
                    tor,
                    self.physical_str(),
                    e,
                    "ycabled-rs"
                );
                PyVal::Int(-1)
            }
        };
        log_debug!(
            "Y_CABLE_DEBUG: Status of toggling mux to ToR {} for port {} status {} {}",
            tor,
            self.physical_str(),
            status,
            "ycabled-rs"
        );
        if status.is_true() {
            side
        } else {
            log_warning!(
                "Error: Could not toggle the mux for port {} to tor{} write eeprom failed",
                self.physical_str(),
                tor
            );
            -1
        }
    }

    // --- Probe ------------------------------------------------------------------

    pub(super) async fn probe_standby(&mut self, f: &FieldMap) {
        if f.get("command").map(String::as_str) != Some("probe") {
            return;
        }
        let Some((_, read_side)) = self.stored_read_side().await else {
            log_debug!(
                "Could not retreive fieldvalue pairs for {}, inside state_db table {}",
                self.port,
                HW_MUX_CABLE_TABLE
            );
            return;
        };
        let status = self.probe_status(read_side).await;
        log_debug!(
            "Y_CABLE_DEBUG: notifying a probe for port status {} {}",
            self.port,
            status
        );
        self.db()
            .set(
                DbId::Appl,
                MUX_CABLE_RESPONSE_TABLE,
                &self.port,
                &[("response", status.to_string())],
            )
            .await;
        log_debug!(
            "Y_CABLE_DEBUG: Successful in returning probe port status {}",
            self.port
        );
    }

    async fn probe_status(&mut self, read_side: i64) -> &'static str {
        if !self.cable_present().await {
            log_warning!(
                "Error: Could not establish presence for Y cable port {} while responding to command probe",
                self.port
            );
            return "unknown";
        }
        if !self.has_driver().await {
            log_error!(
                "Error: Could not get port instance to perform update appdb for read side for Y cable port {}",
                self.port
            );
            return "unknown";
        }
        let active = match self.drv("get_mux_direction", vec![]).await {
            Ok(v) => Some(v),
            Err(e) => {
                log_warning!(
                    "Failed to execute the get_mux_direction for port {} due to {}",
                    self.physical_str(),
                    e
                );
                None
            }
        };
        if self.bad_side(&active) {
            log_warning!(
                "Error: Could not get active side to perform update appdb for mux cable port probe command logical port {} and physical port {}",
                self.port,
                self.physical_str()
            );
            return "unknown";
        }
        let active = active.and_then(|v| v.as_number()).unwrap_or(-1.0);
        let read = read_side as f64;
        if !(read_side == 1 || read_side == 2) || !(active == 1.0 || active == 2.0) {
            log_warning!(
                "Error: Could not get state to perform update appdb for mux cable port probe command logical port {} and physical port {}",
                self.port,
                self.physical_str()
            );
            return "unknown";
        }
        if read == active {
            "active"
        } else {
            "standby"
        }
    }

    // --- MUX_CABLE_INFO ----------------------------------------------------------

    pub(super) async fn telemetry_standby(&mut self) {
        if self.physical.is_none() {
            return;
        }
        let epoch = self.current_epoch();
        let row = self.info_row_standby().await;
        // The cable was removed or initialized again meanwhile: what the pass
        // read may be stale.
        if self.current_epoch() != epoch {
            return;
        }
        if let Some(f) = row {
            self.db()
                .set(DbId::State, MUX_CABLE_INFO, &self.port, &f)
                .await;
        }
    }

    /// Python's post_port_mux_info_to_db for an active-standby port.
    pub(super) async fn post_info_standby(&mut self) {
        if let Some(f) = self.info_row_standby().await {
            self.db()
                .set(DbId::State, MUX_CABLE_INFO, &self.port, &f)
                .await;
        }
    }

    /// MUX_CABLE_INFO of the port: the placeholder for an absent cable, the
    /// cable's values, or None (nothing to write).
    async fn info_row_standby(&mut self) -> Option<Vec<(&'static str, String)>> {
        if !self.cable_present().await {
            let mut f: Vec<(&'static str, String)> = vec![("time_post", timestamp())];
            f.extend(super::placeholder_info());
            return Some(f);
        }
        self.mux_info_standby().await
    }

    /// Python's get_muxcable_info: the fields of MUX_CABLE_INFO, in the
    /// order Python writes them, or None (nothing is written).
    async fn mux_info_standby(&mut self) -> Option<Vec<(&'static str, String)>> {
        if !self.has_driver().await {
            log_error!(
                "Error: Could not get port instance for muxcable info for Y cable port {}",
                self.port
            );
            return None;
        }
        if self.download_in_progress_status().await == Some(FIRMWARE_DOWNLOAD_STATUS_INPROGRESS) {
            log_warning!(
                "Warning: posting mux cable info while a download firmware in progress {}",
                self.port
            );
        }
        let Some((_, read_side)) = self.stored_read_side().await else {
            log_debug!(
                "Could not retreive fieldvalue pairs for {}, inside state_db table {}",
                self.port,
                HW_MUX_CABLE_TABLE
            );
            return None;
        };

        let active = self
            .drv_or_none("get_active_linked_tor_side", vec![], "get_active_side API")
            .await;
        let tor_active = if self.bad_side(&active) {
            "unknown"
        } else {
            let a = active.as_ref().and_then(|v| v.as_number()).unwrap_or(-1.0);
            if !(a == 1.0 || a == 2.0) {
                "unknown"
            } else if read_side as f64 == a {
                "active"
            } else {
                "standby"
            }
        };
        let time_post = timestamp();

        let mux_dir = self
            .drv_or_none("get_mux_direction", vec![], "get_mux_direction API")
            .await;
        let mux_direction = if self.bad_side(&mux_dir) || read_side == -1 {
            "unknown"
        } else if mux_dir.as_ref().and_then(|v| v.as_number()) == Some(read_side as f64) {
            "self"
        } else {
            "peer"
        };

        // Both counts in one attempt, as in Python: the first failure leaves both unread.
        let mut manual = None;
        let mut auto = None;
        match self
            .drv("get_switch_count_total", vec![PyArg::from("manual")])
            .await
        {
            Ok(m) => match self
                .drv("get_switch_count_total", vec![PyArg::from("auto")])
                .await
            {
                Ok(a) => {
                    manual = Some(m);
                    auto = Some(a);
                }
                Err(e) => self.warn_failed("get_switch_cnt API", &e),
            },
            Err(e) => self.warn_failed("get_switch_cnt API", &e),
        }
        let count = |v: &Option<PyVal>| match v {
            Some(v) if !self.bad_side(&Some(v.clone())) => v.py_str(),
            _ => "N/A".to_string(),
        };
        let manual_switch_count = count(&manual);
        let auto_switch_count = count(&auto);

        let (self_target, peer_target) = if read_side == 1 {
            (TARGET_TOR_A, TARGET_TOR_B)
        } else {
            (TARGET_TOR_B, TARGET_TOR_A)
        };
        let (mut eye_self, mut eye_peer) = (None, None);
        match self
            .drv("get_eye_heights", vec![PyArg::Int(self_target)])
            .await
        {
            Ok(s) => match self
                .drv("get_eye_heights", vec![PyArg::Int(peer_target)])
                .await
            {
                Ok(p) => {
                    eye_self = Some(s);
                    eye_peer = Some(p);
                }
                Err(e) => self.warn_failed("get_eye_heights API", &e),
            },
            Err(e) => self.warn_failed("get_eye_heights API", &e),
        }
        let eye_nic = self
            .drv_or_none(
                "get_eye_heights",
                vec![PyArg::Int(TARGET_NIC)],
                "get_eye_heights nic side API",
            )
            .await;
        let lanes = |v: &Option<PyVal>| -> (String, String) {
            match v {
                Some(v) if v.is_list() && !v.eq_int(self.eeprom_error) => {
                    let items = v.items().unwrap_or(&[]);
                    let lane = |i: usize| {
                        items
                            .get(i)
                            .map(|x| x.py_str())
                            .unwrap_or_else(|| "N/A".into())
                    };
                    (lane(0), lane(1))
                }
                _ => ("N/A".into(), "N/A".into()),
            }
        };
        let (self1, self2) = lanes(&eye_self);
        let (peer1, peer2) = lanes(&eye_peer);
        let (nic1, nic2) = lanes(&eye_nic);

        let link_self = self
            .link_active(self_target, if read_side == 1 { "TOR A" } else { "TOR B" })
            .await;
        let link_peer = self
            .link_active(peer_target, if read_side == 1 { "TOR B" } else { "TOR A" })
            .await;
        let link_nic = self.link_active(TARGET_NIC, "NIC").await;

        let mut versions: Vec<(&'static str, String)> = Vec::new();
        let nic_v = self.firmware_versions(TARGET_NIC, "nic").await;
        let (self_v, peer_v) = if read_side == 1 {
            let s = self.firmware_versions(TARGET_TOR_A, "self").await;
            let p = self.firmware_versions(TARGET_TOR_B, "peer").await;
            (s, p)
        } else {
            let p = self.firmware_versions(TARGET_TOR_A, "peer").await;
            let s = self.firmware_versions(TARGET_TOR_B, "self").await;
            (s, p)
        };
        for (names, v) in [
            (
                [
                    "version_self_active",
                    "version_self_inactive",
                    "version_self_next",
                ],
                self_v,
            ),
            (
                [
                    "version_peer_active",
                    "version_peer_inactive",
                    "version_peer_next",
                ],
                peer_v,
            ),
            (
                [
                    "version_nic_active",
                    "version_nic_inactive",
                    "version_nic_next",
                ],
                nic_v,
            ),
        ] {
            for (n, val) in names.iter().zip(v) {
                versions.push((n, val));
            }
        }

        let temp = self
            .drv_or_none("get_local_temperature", vec![], "get_local_temperature")
            .await;
        let internal_temperature = self.int_reading(&temp);
        let volt = self
            .drv_or_none("get_local_voltage", vec![], "get_local_voltage")
            .await;
        let internal_voltage = float_reading(&volt);
        let nic_volt = self
            .drv_or_none("get_nic_voltage", vec![], "get_nic_voltage")
            .await;
        let nic_voltage = float_reading(&nic_volt);
        let nic_temp = self
            .drv_or_none("get_nic_temperature", vec![], "get_nic_temperature")
            .await;
        let nic_temperature = self.int_reading(&nic_temp);

        let mut f: Vec<(&'static str, String)> = vec![
            ("tor_active", tor_active.to_string()),
            ("time_post", time_post),
            ("mux_direction", mux_direction.to_string()),
            ("manual_switch_count", manual_switch_count),
            ("auto_switch_count", auto_switch_count),
            ("link_status_self", link_self.to_string()),
            ("link_status_peer", link_peer.to_string()),
            ("link_status_nic", link_nic.to_string()),
            ("self_eye_height_lane1", self1),
            ("self_eye_height_lane2", self2),
            ("peer_eye_height_lane1", peer1),
            ("peer_eye_height_lane2", peer2),
            ("nic_eye_height_lane1", nic1),
            ("nic_eye_height_lane2", nic2),
            ("internal_temperature", internal_temperature),
            ("internal_voltage", internal_voltage),
            ("nic_temperature", nic_temperature),
            ("nic_voltage", nic_voltage),
        ];
        f.extend(versions);
        Some(f)
    }

    fn warn_failed(&self, what: &str, e: &CallError) {
        if let CallError::Raised(r) = e {
            log_warning!(
                "Failed to execute the {} for port {} due to {}",
                what,
                self.physical_str(),
                r
            );
        }
    }

    async fn link_active(&self, target: i64, side: &str) -> &'static str {
        match self.drv("is_link_active", vec![PyArg::Int(target)]).await {
            Ok(v) if v.truthy() => "up",
            Ok(_) => "down",
            Err(e) => {
                if let CallError::Raised(r) = e {
                    log_warning!(
                        "Failed to execute the is_link_active {} side API for port {} due to {}",
                        side,
                        self.physical_str(),
                        r
                    );
                }
                "down"
            }
        }
    }

    /// int readings (temperatures): an int of 0 or more.
    fn int_reading(&self, v: &Option<PyVal>) -> String {
        match v {
            Some(v) if v.is_int() && !v.eq_int(self.eeprom_error) && !v.lt_int(0) => v.py_str(),
            _ => "N/A".into(),
        }
    }

    /// Python's get_firmware_dict: active, inactive and next versions of one
    /// side. During a download the last known versions in MUX_CABLE_INFO.
    pub async fn firmware_versions(&mut self, target: i64, side: &str) -> [String; 3] {
        let names = [
            format!("version_{side}_active"),
            format!("version_{side}_inactive"),
            format!("version_{side}_next"),
        ];
        let status = self.download_in_progress_status().await;
        if status == Some(FIRMWARE_DOWNLOAD_STATUS_INPROGRESS) {
            let Some(row) = self.db().get(DbId::State, MUX_CABLE_INFO, &self.port).await else {
                log_debug!(
                    "Could not retreive fieldvalue pairs for {}, inside state_db table {}",
                    self.port,
                    MUX_CABLE_INFO
                );
                return ["N/A".into(), "N/A".into(), "N/A".into()];
            };
            log_warning!(
                "trying to get/post firmware info while download/toggle in progress returning with last known firmware without execute {}",
                self.physical_str()
            );
            return names.map(|n| row.get(&n).cloned().unwrap_or_else(|| "None".into()));
        }
        if status == Some(FIRMWARE_DOWNLOAD_STATUS_FAILED) {
            log_error!(
                "Firmware Download API failed in the previous run, firmware download status was set to failed;retry required {}",
                self.physical_str()
            );
        }
        let result = match self
            .drv("get_firmware_version", vec![PyArg::Int(target)])
            .await
        {
            Ok(v) => Some(v),
            Err(e) => {
                if let CallError::Raised(r) = e {
                    log_warning!(
                        "Failed to execute the get_firmware_version API for port {} side {} due to {}",
                        self.physical_str(),
                        side,
                        r
                    );
                }
                None
            }
        };
        match result {
            Some(d) if d.is_dict() => {
                ["version_active", "version_inactive", "version_next"].map(|k| {
                    d.dict_get(k)
                        .map(|v| v.py_str())
                        .unwrap_or_else(|| "None".into())
                })
            }
            _ => ["N/A".into(), "N/A".into(), "N/A".into()],
        }
    }

    // --- MUX_CABLE_STATIC_INFO -------------------------------------------------

    /// Python's post_port_mux_static_info_to_db: the cursor values of each lane.
    async fn post_static_info(&mut self) {
        let fields = if !self.cable_present().await {
            log_warning!(
                "Error: trying to post mux static info without presence of port {}",
                self.port
            );
            Some(static_info_without_presence())
        } else {
            self.static_info().await
        };
        if let Some(f) = fields {
            let fv: Vec<(&str, String)> = f.iter().map(|(k, v)| (k.as_str(), v.clone())).collect();
            self.db()
                .set(DbId::State, MUX_CABLE_STATIC_INFO, &self.port, &fv)
                .await;
        }
    }

    async fn static_info(&mut self) -> Option<Vec<(String, String)>> {
        if !self.has_driver().await {
            log_error!(
                "Error: Could not get port instance for muxcable info for Y cable port {}",
                self.port
            );
            return None;
        }
        let Some((_, read_side)) = self.stored_read_side().await else {
            log_debug!(
                "Could not retreive fieldvalue pairs for {}, inside state_db table {}",
                self.port,
                HW_MUX_CABLE_TABLE
            );
            return None;
        };
        let na = || vec!["N/A".to_string(); 5];
        let mut nic = Vec::new();
        let mut tor1 = Vec::new();
        let mut tor2 = Vec::new();
        for lane in 1..=2i64 {
            for (target, what, out) in [
                (TARGET_NIC, "NIC", &mut nic),
                (TARGET_TOR_A, "ToR 1", &mut tor1),
                (TARGET_TOR_B, "ToR 2", &mut tor2),
            ] {
                let v = match self
                    .drv(
                        "get_target_cursor_values",
                        vec![PyArg::Int(lane), PyArg::Int(target)],
                    )
                    .await
                {
                    Ok(v) => Some(v),
                    Err(e) => {
                        if let CallError::Raised(r) = e {
                            log_warning!(
                                "Failed to execute the get_target_cursor_value {} for port {} due to {}",
                                what,
                                self.physical_str(),
                                r
                            );
                        }
                        None
                    }
                };
                let values = match v {
                    Some(v) if v.is_list() && !v.eq_int(self.eeprom_error) => {
                        let items = v.items().unwrap_or(&[]);
                        (0..5)
                            .map(|i| {
                                items
                                    .get(i)
                                    .map(|x| x.py_str())
                                    .unwrap_or_else(|| "N/A".into())
                            })
                            .collect()
                    }
                    _ => na(),
                };
                out.push(values);
            }
        }
        let names = [
            "precursor1",
            "precursor2",
            "maincursor",
            "postcursor1",
            "postcursor2",
        ];
        let mut f = vec![(
            "read_side".to_string(),
            if read_side == 1 { "tor1" } else { "tor2" }.to_string(),
        )];
        let (tor_self, tor_peer) = if read_side == 1 {
            (&tor1, &tor2)
        } else {
            (&tor2, &tor1)
        };
        for (prefix, values) in [
            ("nic", &nic),
            ("tor_self", tor_self),
            ("tor_peer", tor_peer),
        ] {
            for (lane, lane_values) in values.iter().take(2).enumerate() {
                for (n, v) in names.iter().zip(lane_values) {
                    f.push((format!("{}_lane{}_{}", prefix, lane + 1, n), v.clone()));
                }
            }
        }
        Some(f)
    }
}

/// float readings (voltages): only a float.
fn float_reading(v: &Option<PyVal>) -> String {
    match v {
        Some(v) if v.is_float() => v.py_str(),
        _ => "N/A".into(),
    }
}

/// MUX_CABLE_STATIC_INFO of an absent cable.
pub fn static_info_without_presence() -> Vec<(String, String)> {
    let mut f = vec![("read_side".to_string(), "-1".to_string())];
    for prefix in ["nic", "tor_self", "tor_peer"] {
        for lane in 1..=2 {
            for n in [
                "precursor1",
                "precursor2",
                "maincursor",
                "postcursor1",
                "postcursor2",
            ] {
                f.push((format!("{prefix}_lane{lane}_{n}"), "N/A".to_string()));
            }
        }
    }
    f
}
