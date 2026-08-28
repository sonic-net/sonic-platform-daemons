//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! The two halves of the Switch-BMC path.  Ports `thermalctld:944-1000` and
//! `:1300-1383`.
//!
//! The same binary plays one of two mutually exclusive roles, chosen by
//! `platform_env.conf`:
//!
//! * **On the switch host** (`switch_host=1`) it mirrors every
//!   `TEMPERATURE_INFO` row to the BMC's STATE_DB over TCP.  The connection is
//!   rebuilt lazily on the next write after a failure, and the failure is
//!   logged once rather than every cycle, so a dropped link or a BMC redis
//!   restart recovers without flooding the log.
//! * **On the BMC** (`switch_bmc=1`) it polls that mirror and tees a breach of
//!   `critical_high_threshold` or `critical_low_threshold` to the BMC event log
//!   — once, on entry.  Recovery is tracked silently, and a sensor that leaves
//!   the mirror has its state dropped so a later breach is reported again.

use std::collections::{HashMap, HashSet};

use swss_common::{DbConnector, Table};

use crate::db::TableLike;

use crate::event_log::EventLogger;

/// Python's `STATE_DB_ID`.
const STATE_DB_ID: i32 = 6;
/// `daemon_base.db_connect_remote`'s default port; `bmc.json` carries no port.
const BMC_REDIS_PORT: u16 = 6379;
const CONNECT_TIMEOUT_MS: u32 = 0;
/// Python's `GLOBAL_BMC_DATA_FILE`.
const BMC_DATA_FILE: &str = "/etc/sonic/bmc.json";

/// The BMC's address from `bmc.json`, or `None` where there is no BMC.
pub fn bmc_address() -> Option<String> {
    address_from(BMC_DATA_FILE)
}

fn address_from(path: &str) -> Option<String> {
    let text = std::fs::read_to_string(path).ok()?;
    let root: serde_json::Value = serde_json::from_str(&text).ok()?;
    let addr = root.get("bmc_addr")?.as_str()?.trim();
    (!addr.is_empty()).then(|| addr.to_string())
}

// ── Switch host: the mirror ───────────────────────────────────────────────────

/// A lazily (re)connected handle on the BMC's `TEMPERATURE_INFO`.
/// How a mirror opens the remote table.
///
/// The daemon supplies redis-over-TCP to the BMC; a test supplies a table it
/// can read back, and one that refuses to open.  The indirection exists because
/// the interesting behaviour here is not the connection — it is what happens
/// around a connection that keeps failing: reconnect on the next write, and log
/// once rather than once per row per cycle.
pub type OpenTable = Box<dyn FnMut(&str) -> Result<Box<dyn TableLike>, String> + Send>;

/// The real opener: `STATE_DB` on the BMC, over TCP.
fn open_over_tcp(address: &str) -> Result<Box<dyn TableLike>, String> {
    DbConnector::new_tcp(STATE_DB_ID, address.to_string(), BMC_REDIS_PORT, CONNECT_TIMEOUT_MS)
        .and_then(|c| Table::new(c, crate::db::TEMPERATURE_INFO))
        .map(|t| Box::new(t) as Box<dyn TableLike>)
        .map_err(|e| format!("{e:?}"))
}

pub struct BmcMirror {
    address: String,
    table: Option<Box<dyn TableLike>>,
    /// Set once a failure has been logged, so the next one is silent until the
    /// link recovers.
    failed: bool,
    open: OpenTable,
}

impl BmcMirror {
    /// `None` off the switch host, or where `bmc.json` names no address.
    pub fn new(is_switch_host: bool) -> Option<Self> {
        if !is_switch_host {
            return None;
        }
        let address = bmc_address()?;
        Some(Self::with_opener(&address, Box::new(open_over_tcp)))
    }

    /// The same mirror with the connection step supplied.
    pub fn with_opener(address: &str, open: OpenTable) -> Self {
        let mut mirror = Self {
            address: address.to_string(),
            table: None,
            failed: false,
            open,
        };
        mirror.connect();
        mirror
    }

    fn connect(&mut self) {
        let address = self.address.clone();
        match (self.open)(&address) {
            Ok(t) => {
                self.table = Some(t);
                self.failed = false;
                log::info!(
                    "Mirroring {} to BMC STATE_DB at {}",
                    crate::db::TEMPERATURE_INFO,
                    self.address
                );
            }
            Err(e) => {
                self.table = None;
                if !self.failed {
                    self.failed = true;
                    log::warn!("Failed to open remote BMC {} table: {e}", crate::db::TEMPERATURE_INFO);
                }
            }
        }
    }

    /// Tee one row.  A failure drops the handle so the next call reconnects.
    pub fn set(&mut self, key: &str, fvs: &[(&str, String)]) {
        if self.table.is_none() {
            self.connect();
        }
        let Some(table) = self.table.as_ref() else {
            return;
        };
        if let Err(e) = table.set(key, fvs) {
            if !self.failed {
                self.failed = true;
                log::warn!("BMC mirror write for {key} failed: {e}");
            }
            self.table = None;
        }
    }

    /// Remove a row that no longer exists locally.
    pub fn del(&mut self, key: &str) {
        if let Some(table) = self.table.as_ref() {
            let _ = table.del(key);
        }
    }
}

// ── BMC: the critical-thermal watch ───────────────────────────────────────────

/// One row as the watch reads it.
#[derive(Debug, Clone, PartialEq)]
pub struct MirroredThermal {
    pub name: String,
    pub temperature: Option<f64>,
    pub critical_high: Option<f64>,
    pub critical_low: Option<f64>,
}

/// Remembers which sensors are already in breach, so each is logged once.
#[derive(Default)]
pub struct ChassisThermalWatch {
    breached: HashSet<String>,
}

impl ChassisThermalWatch {
    pub fn new() -> Self {
        Self::default()
    }

    /// One pass over the mirror.
    pub fn check(&mut self, rows: &[MirroredThermal], events: &mut dyn EventLogger) {
        let mut seen = HashSet::with_capacity(rows.len());
        for row in rows {
            seen.insert(row.name.clone());
            let Some(temp) = row.temperature else {
                continue;
            };

            let mut reasons = Vec::new();
            if let Some(hi) = row.critical_high {
                if temp >= hi {
                    reasons.push(format!(">= critical_high_threshold {}C", crate::fmt::float(hi)));
                }
            }
            if let Some(lo) = row.critical_low {
                if temp <= lo {
                    reasons.push(format!("<= critical_low_threshold {}C", crate::fmt::float(lo)));
                }
            }
            let breach = !reasons.is_empty();

            if breach {
                // Logged on entry only; a standing breach stays quiet.
                if self.breached.insert(row.name.clone()) {
                    events.error(&format!(
                        "CRITICAL chassis thermal: {} temperature {}C {}",
                        row.name,
                        crate::fmt::float(temp),
                        reasons.join("; ")
                    ));
                }
            } else {
                // Recovery is tracked but not logged: only breaches are.
                self.breached.remove(&row.name);
            }
        }
        // A sensor that left the mirror is forgotten, so a later breach is
        // reported again.
        self.breached.retain(|name| seen.contains(name));
    }
}

/// Read the local mirror.  On the BMC this is the copy the host pushed.
pub fn read_mirror(table: &dyn crate::db::TableLike) -> Vec<MirroredThermal> {
    let keys = table.get_keys();
    let mut out = Vec::with_capacity(keys.len());
    for key in keys {
        let Some(fvs) = table.get(&key) else {
            continue;
        };
        let map: HashMap<String, String> = fvs.into_iter().collect();
        let num = |f: &str| map.get(f).and_then(|s| s.trim().parse::<f64>().ok());
        out.push(MirroredThermal {
            name: key,
            temperature: num("temperature"),
            critical_high: num("critical_high_threshold"),
            critical_low: num("critical_low_threshold"),
        });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[derive(Default)]
    struct Recorder {
        lines: Vec<String>,
    }
    impl EventLogger for Recorder {
        fn error(&mut self, msg: &str) {
            self.lines.push(msg.to_string());
        }
        fn notice(&mut self, _msg: &str) {}
    }

    fn row(name: &str, temp: f64) -> MirroredThermal {
        MirroredThermal {
            name: name.to_string(),
            temperature: Some(temp),
            critical_high: Some(100.0),
            critical_low: Some(-10.0),
        }
    }

    fn json(body: &str) -> tempfile::NamedTempFile {
        let mut f = tempfile::NamedTempFile::new().unwrap();
        f.write_all(body.as_bytes()).unwrap();
        f
    }

    #[test]
    fn the_address_comes_from_bmc_addr() {
        let f = json(r#"{"bmc_if_name": "eth1", "bmc_addr": "10.0.0.2"}"#);
        assert_eq!(address_from(f.path().to_str().unwrap()), Some("10.0.0.2".to_string()));
    }

    #[test]
    fn a_missing_or_empty_address_is_none() {
        assert_eq!(address_from("/nonexistent/bmc.json"), None);
        let f = json(r#"{"bmc_addr": ""}"#);
        assert_eq!(address_from(f.path().to_str().unwrap()), None);
        let f = json(r#"{"other": 1}"#);
        assert_eq!(address_from(f.path().to_str().unwrap()), None);
    }

    #[test]
    fn a_breach_is_logged_once_on_entry() {
        let mut w = ChassisThermalWatch::new();
        let mut ev = Recorder::default();
        w.check(&[row("ASIC", 105.0)], &mut ev);
        w.check(&[row("ASIC", 106.0)], &mut ev);
        assert_eq!(ev.lines.len(), 1, "a standing breach must not be re-logged");
        assert!(ev.lines[0].contains("CRITICAL chassis thermal: ASIC"));
    }

    #[test]
    fn recovery_is_silent_but_rearms() {
        let mut w = ChassisThermalWatch::new();
        let mut ev = Recorder::default();
        w.check(&[row("ASIC", 105.0)], &mut ev);
        w.check(&[row("ASIC", 50.0)], &mut ev);
        assert_eq!(ev.lines.len(), 1, "recovery logs nothing");
        w.check(&[row("ASIC", 105.0)], &mut ev);
        assert_eq!(ev.lines.len(), 2, "a second breach is logged again");
    }

    #[test]
    fn the_low_threshold_counts_too() {
        let mut w = ChassisThermalWatch::new();
        let mut ev = Recorder::default();
        w.check(&[row("ASIC", -20.0)], &mut ev);
        assert_eq!(ev.lines.len(), 1);
        assert!(ev.lines[0].contains("critical_low_threshold"));
    }

    #[test]
    fn a_row_without_a_temperature_is_skipped() {
        let mut w = ChassisThermalWatch::new();
        let mut ev = Recorder::default();
        let mut r = row("ASIC", 0.0);
        r.temperature = None;
        w.check(&[r], &mut ev);
        assert!(ev.lines.is_empty());
    }

    #[test]
    fn a_missing_threshold_does_not_breach() {
        let mut w = ChassisThermalWatch::new();
        let mut ev = Recorder::default();
        let mut r = row("ASIC", 500.0);
        r.critical_high = None;
        r.critical_low = None;
        w.check(&[r], &mut ev);
        assert!(ev.lines.is_empty());
    }

    #[test]
    fn a_sensor_leaving_the_mirror_is_forgotten() {
        let mut w = ChassisThermalWatch::new();
        let mut ev = Recorder::default();
        w.check(&[row("ASIC", 105.0)], &mut ev);
        w.check(&[], &mut ev);
        w.check(&[row("ASIC", 105.0)], &mut ev);
        assert_eq!(ev.lines.len(), 2, "it must be reported again after coming back");
    }

    // ── The mirror's reconnect and log-once behaviour ─────────────────────

    use crate::db::mock::MockTable;
    use std::sync::{Arc, Mutex};

    /// An opener whose answer a test controls, and which counts how often it
    /// was asked.
    fn opener(table: MockTable, fail_until: usize) -> (OpenTable, Arc<Mutex<usize>>) {
        let calls = Arc::new(Mutex::new(0usize));
        let c = calls.clone();
        let open: OpenTable = Box::new(move |_addr: &str| {
            let mut n = c.lock().unwrap();
            *n += 1;
            if *n <= fail_until {
                Err("connection refused".to_string())
            } else {
                Ok(Box::new(table.clone()) as Box<dyn TableLike>)
            }
        });
        (open, calls)
    }

    #[test]
    fn a_mirror_that_opens_tees_every_row() {
        let remote = MockTable::new();
        let (open, calls) = opener(remote.clone(), 0);
        let mut m = BmcMirror::with_opener("10.0.0.1", open);

        m.set("ASIC", &[("temperature", "45.0".to_string())]);
        assert_eq!(remote.field("ASIC", "temperature").as_deref(), Some("45.0"));
        assert_eq!(*calls.lock().unwrap(), 1, "opened once, at construction");
    }

    /// A BMC that is not up yet must not stop the daemon: the mirror opens on
    /// the next write instead, so the link recovers on its own.
    #[test]
    fn a_mirror_that_could_not_open_retries_on_the_next_write() {
        let remote = MockTable::new();
        let (open, calls) = opener(remote.clone(), 1);
        let mut m = BmcMirror::with_opener("10.0.0.1", open);
        assert_eq!(*calls.lock().unwrap(), 1, "the failed attempt at construction");
        assert!(remote.is_empty());

        m.set("ASIC", &[("temperature", "45.0".to_string())]);
        assert_eq!(*calls.lock().unwrap(), 2, "retried");
        assert_eq!(remote.field("ASIC", "temperature").as_deref(), Some("45.0"));
    }

    /// A write that fails drops the handle, so the row after it reconnects
    /// rather than writing into a socket that is gone.
    #[test]
    fn a_failed_write_drops_the_handle_and_the_next_row_reconnects() {
        let remote = MockTable::new();
        let (open, calls) = opener(remote.clone(), 0);
        let mut m = BmcMirror::with_opener("10.0.0.1", open);

        remote.fail_writes("broken pipe");
        m.set("ASIC", &[("temperature", "45.0".to_string())]);
        assert_eq!(
            *calls.lock().unwrap(),
            1,
            "no reconnect yet — the failure just happened"
        );

        m.set("PSU-1 Temp", &[("temperature", "30.0".to_string())]);
        assert_eq!(*calls.lock().unwrap(), 2, "the next row reconnects");
    }

    /// Deleting through a mirror that has no handle is a no-op, not a
    /// reconnect: a row that no longer exists locally does not justify dialling
    /// a BMC that is down.
    #[test]
    fn deleting_without_a_handle_does_not_reconnect() {
        let remote = MockTable::new();
        let (open, calls) = opener(remote.clone(), 1);
        let mut m = BmcMirror::with_opener("10.0.0.1", open);
        m.del("ASIC");
        assert_eq!(*calls.lock().unwrap(), 1, "still just the construction attempt");
    }

    #[test]
    fn a_delete_removes_the_row_from_the_bmc() {
        let remote = MockTable::new();
        let (open, _) = opener(remote.clone(), 0);
        let mut m = BmcMirror::with_opener("10.0.0.1", open);

        m.set("ASIC", &[("temperature", "45.0".to_string())]);
        assert!(!remote.is_empty());
        m.del("ASIC");
        assert!(remote.is_empty());
    }

    /// The mirror only exists on the switch host: on the BMC the same table is
    /// what this daemon reads, and mirroring it back would have the BMC feed
    /// itself.
    #[test]
    fn there_is_no_mirror_off_the_switch_host() {
        assert!(BmcMirror::new(false).is_none());
    }

    // ── read_mirror ───────────────────────────────────────────────────────

    /// The mirror is parsed field by field, and a field that is not a number —
    /// `N/A` is the common one — is absent rather than zero.  A sensor whose
    /// temperature reads as 0.0 would look ice cold to the watch below, and a
    /// threshold that read as 0.0 would make every sensor breach it.
    #[test]
    fn a_mirrored_row_parses_its_three_numbers_and_ignores_the_rest() {
        let t = MockTable::new();
        TableLike::set(
            &t,
            "ASIC",
            &[
                ("temperature", "45.5".to_string()),
                ("critical_high_threshold", "120".to_string()),
                ("critical_low_threshold", "-5".to_string()),
                ("is_replaceable", "False".to_string()),
            ],
        )
        .unwrap();

        let rows = read_mirror(&t);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].name, "ASIC");
        assert_eq!(rows[0].temperature, Some(45.5));
        assert_eq!(rows[0].critical_high, Some(120.0));
        assert_eq!(rows[0].critical_low, Some(-5.0), "a low threshold may be negative");
    }

    #[test]
    fn an_unparsable_or_missing_field_is_absent_not_zero() {
        let t = MockTable::new();
        TableLike::set(
            &t,
            "PSU-1 Temp",
            &[
                ("temperature", "N/A".to_string()),
                ("critical_high_threshold", "  ".to_string()),
            ],
        )
        .unwrap();

        let rows = read_mirror(&t);
        assert_eq!(rows[0].temperature, None);
        assert_eq!(rows[0].critical_high, None);
        assert_eq!(rows[0].critical_low, None, "the field is not in the row at all");
    }

    /// Values are trimmed before parsing, as everything else in this daemon is.
    #[test]
    fn a_mirrored_value_is_trimmed_before_parsing() {
        let t = MockTable::new();
        TableLike::set(&t, "ASIC", &[("temperature", " 45.5\n".to_string())]).unwrap();
        assert_eq!(read_mirror(&t)[0].temperature, Some(45.5));
    }

    /// Every key in the table becomes a row, in the table's order, so the watch
    /// below sees the whole mirror rather than the first sensor.
    #[test]
    fn every_key_becomes_a_row() {
        let t = MockTable::new();
        for name in ["ASIC", "PSU-1 Temp", "Ambient Port Side Temp"] {
            TableLike::set(&t, name, &[("temperature", "40".to_string())]).unwrap();
        }
        let rows = read_mirror(&t);
        assert_eq!(rows.len(), 3);
        let names: Vec<&str> = rows.iter().map(|r| r.name.as_str()).collect();
        assert_eq!(names, ["ASIC", "Ambient Port Side Temp", "PSU-1 Temp"]);
    }

    #[test]
    fn an_empty_mirror_is_no_rows_rather_than_an_error() {
        assert!(read_mirror(&MockTable::new()).is_empty());
    }
}
