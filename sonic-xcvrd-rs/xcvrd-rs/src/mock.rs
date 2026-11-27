//! Test seam mocks (analysis §3.6, Part B) — the Rust analogue of the Python
//! `tests/mock_swsscommon.py` (`Table`) and `tests/mock_platform.py` +
//! `@patch('..._wrapper_*', MagicMock(...))` injection.
//!
//! [`MockDbTable`] is an in-memory [`crate::db::DbTable`] so unit tests assert field
//! counts exactly like the Python tests (`get_size_for_key == 27`). [`MockHal`] /
//! [`MockSfp`] are a [`crate::hal::Hal`]/[`crate::hal::SfpHandle`] returning canned
//! `serde_json::Value`s. Public (not `#[cfg(test)]`) so crate-level integration
//! tests under `tests/` can use them too.
#![allow(dead_code)]

use std::collections::{BTreeMap, HashMap};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use serde_json::Value;

use crate::db::DbTable;
use crate::error::{Result, XcvrdError};
use crate::hal::{ChangeEvent, Hal, SfpHandle};

// --- MockDbTable — port of tests/mock_swsscommon.py:Table -----------------------

struct MockDbInner {
    keys: Vec<String>,
    rows: HashMap<String, Vec<(String, String)>>,
    /// When set, `get_size_for_key_checked` returns `None` (indeterminate) to simulate a
    /// transient STATE_DB read failure — mirrors the `RealDbTable::hgetall` error path so
    /// tests can exercise callers that must not treat a failed read as a deleted row.
    fail_size_reads: bool,
    fail_writes: bool,
}

/// In-memory STATE_DB table: `set/get/hget/del/hdel/getKeys/get_size/
/// get_size_for_key`, mirroring `mock_swsscommon.Table`.
pub struct MockDbTable {
    pub table_name: String,
    inner: Mutex<MockDbInner>,
}

impl MockDbTable {
    pub fn new(table_name: impl Into<String>) -> Self {
        MockDbTable {
            table_name: table_name.into(),
            inner: Mutex::new(MockDbInner {
                keys: Vec::new(),
                rows: HashMap::new(),
                fail_size_reads: false,
                fail_writes: false,
            }),
        }
    }

    /// Test-only: drop the tracked key list while KEEPING every row readable by key —
    /// models failed/incomplete key enumeration without deleting the underlying rows.
    pub fn clear_tracked_keys_for_test(&self) {
        self.inner.lock().unwrap().keys.clear();
    }

    /// Test-only: make `get_size_for_key_checked` report an indeterminate read (`None`),
    /// simulating a transient STATE_DB failure without deleting any row.
    #[cfg(test)]
    pub fn set_fail_size_reads(&self, fail: bool) {
        self.inner.lock().unwrap().fail_size_reads = fail;
    }

    /// Test-only: reject row sets without changing the table.
    #[cfg(test)]
    pub fn set_fail_writes(&self, fail: bool) {
        self.inner.lock().unwrap().fail_writes = fail;
    }
}

impl DbTable for MockDbTable {
    fn set(&self, key: &str, fvs: &[(String, String)]) {
        self.set_checked(key, fvs);
    }

    fn set_checked(&self, key: &str, fvs: &[(String, String)]) -> bool {
        let mut g = self.inner.lock().unwrap();
        if g.fail_writes {
            return false;
        }
        if !g.keys.iter().any(|k| k == key) {
            g.keys.push(key.to_string());
        }
        g.rows.insert(key.to_string(), fvs.to_vec());
        true
    }

    fn hset(&self, key: &str, field: &str, value: &str) {
        let mut g = self.inner.lock().unwrap();
        if !g.keys.iter().any(|k| k == key) {
            g.keys.push(key.to_string());
        }
        let row = g.rows.entry(key.to_string()).or_default();
        if let Some(pair) = row.iter_mut().find(|(k, _)| k == field) {
            pair.1 = value.to_string();
        } else {
            row.push((field.to_string(), value.to_string()));
        }
    }

    fn get(&self, key: &str) -> Option<Vec<(String, String)>> {
        self.inner.lock().unwrap().rows.get(key).cloned()
    }

    fn hget(&self, key: &str, field: &str) -> Option<String> {
        let g = self.inner.lock().unwrap();
        g.rows
            .get(key)
            .and_then(|row| row.iter().find(|(k, _)| k == field).map(|(_, v)| v.clone()))
    }

    fn del(&self, key: &str) {
        let mut g = self.inner.lock().unwrap();
        if g.rows.remove(key).is_some() {
            g.keys.retain(|k| k != key);
        }
    }

    fn hdel(&self, key: &str, field: &str) {
        let mut g = self.inner.lock().unwrap();
        let empty = if let Some(row) = g.rows.get_mut(key) {
            row.retain(|(k, _)| k != field);
            row.is_empty()
        } else {
            return;
        };
        if empty {
            g.rows.remove(key);
            g.keys.retain(|k| k != key);
        }
    }

    fn get_keys(&self) -> Vec<String> {
        self.inner.lock().unwrap().keys.clone()
    }

    fn get_size(&self) -> usize {
        self.inner.lock().unwrap().rows.len()
    }

    fn get_size_for_key(&self, key: &str) -> usize {
        self.get_size_for_key_checked(key).unwrap_or(0)
    }

    fn get_size_for_key_checked(&self, key: &str) -> Option<usize> {
        let g = self.inner.lock().unwrap();
        if g.fail_size_reads {
            return None;
        }
        Some(g.rows.get(key).map(|r| r.len()).unwrap_or(0))
    }

    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

// --- MockSfp / MockHal — port of the patched platform wrappers ------------------

/// A canned transceiver. Test builders set the fields a behavior reads (identity,
/// DOM, status, presence, lpmode); the rest default to sensible empties.
#[derive(Clone, Default)]
pub struct MockSfp {
    pub presence: bool,
    pub replaceable: bool,
    pub reset_status: bool,
    pub sfp_type: String,
    pub error_description: Option<String>,
    pub info: Value,
    pub dom_real_value: Value,
    pub status: Value,
    pub threshold_info: Value,
    pub lpmode: bool,
    /// When set, `get_lpmode()` returns an `Err` — the analogue of the Python
    /// `get_lpmode` raising `NotImplementedError`, so `XCVRDUtils` unit tests can
    /// exercise the exception → `false` path.
    pub lpmode_err: bool,
    /// Canned results for `call_json(method)` (dom/status flags, temperature, PM,
    /// firmware, VDM getters, …).
    pub json_calls: BTreeMap<String, Value>,
    /// Shared log of every `call_json(method)` invocation. `MockHal::sfp` clones the
    /// slot's `MockSfp`, but this `Arc` is shared with the clone, so a call issued on
    /// the handle the daemon obtained (e.g. `remove_xcvr_api` on plug-out) is
    /// observable from the original `MockHal::sfps[i]` in the test.
    pub call_log: Arc<Mutex<Vec<String>>>,
    /// Canned raw EEPROM bytes keyed by flat linear offset, served by `read_eeprom`.
    /// Empty by default, so the DOM-flag voltage-group supplement is a strict no-op in
    /// every test that doesn't opt in via [`MockSfp::with_eeprom`].
    pub eeprom: BTreeMap<usize, u8>,
    /// Shared log of every `write_eeprom(offset, data)` invocation, in order. Like
    /// `call_log`, this `Arc` is shared with the clone `MockHal::sfp` hands the daemon,
    /// so the CMIS register writes the manager issues on its handle are observable from
    /// the original `MockHal::sfps[i]` in the test (write-order / mask assertions).
    pub eeprom_writes: Arc<Mutex<Vec<(usize, Vec<u8>)>>>,
    /// Shared counter of every `get_presence()` call. Shared across `MockHal::sfp`
    /// clones (like `call_log`) so a test can assert how often the CMIS task polls HW
    /// presence over the bridge — e.g. that a genuinely-terminal port stops polling
    /// (the fix that keeps the CMIS task off the bridge so the concurrent DOM byte-9
    /// read that sources vccHAlarm is not starved).
    pub presence_calls: Arc<AtomicUsize>,
    /// Number of LEADING `get_presence()` calls that report absent before the slot starts
    /// answering `presence` honestly -- a flaky I2C read. Lets a test drive the
    /// confirm-before-believing path without any timing dependency.
    pub presence_misses: Arc<AtomicUsize>,
    /// Canned results for the direct decode seam `xcvr_api_call_json(method)`
    /// (`sfp.get_xcvr_api().<method>()`). Present mocks report paged memory; other
    /// decoder methods default to unavailable so aggregate fallbacks remain testable.
    pub api_calls: BTreeMap<String, Value>,
    /// Shared log of every `xcvr_api_call_json(method)` invocation, so a test can assert
    /// the CMIS manager actually took the cheap route.
    pub api_call_log: Arc<Mutex<Vec<String>>>,
    /// Shared counters of the two EXPENSIVE aggregate getters, so a test can assert the
    /// per-sweep memo really collapses N reads into one.
    pub info_reads: Arc<AtomicUsize>,
    pub status_reads: Arc<AtomicUsize>,
}

impl MockSfp {
    pub fn present() -> Self {
        MockSfp {
            presence: true,
            replaceable: true,
            api_calls: BTreeMap::from([("is_flat_memory".to_string(), Value::Bool(false))]),
            ..Default::default()
        }
    }

    /// An absent slot (`get_presence() == false`) — the poster/gate skip path.
    pub fn absent() -> Self {
        MockSfp::default()
    }

    pub fn with_info(mut self, info: Value) -> Self {
        self.info = info;
        self
    }

    /// Seed the typed DOM real-value dict returned by `get_transceiver_dom_real_value`.
    pub fn with_dom_real_value(mut self, dom: Value) -> Self {
        self.dom_real_value = dom;
        self
    }

    /// Seed the typed threshold dict returned by `get_transceiver_threshold_info`.
    pub fn with_threshold_info(mut self, thr: Value) -> Self {
        self.threshold_info = thr;
        self
    }

    /// Seed the typed rich-status dict returned by `get_transceiver_status`
    /// (module/datapath/config/tx-rx fields the `TRANSCEIVER_STATUS` poster reads).
    pub fn with_status(mut self, status: Value) -> Self {
        self.status = status;
        self
    }

    pub fn with_json(mut self, method: &str, value: Value) -> Self {
        self.json_calls.insert(method.to_string(), value);
        self
    }

    /// Seed a method on `get_xcvr_api()`, separately from generic SFP methods.
    pub fn with_api_json(mut self, method: &str, value: Value) -> Self {
        self.api_calls.insert(method.to_string(), value);
        self
    }

    /// Seed a raw EEPROM byte at flat linear `offset`, served by `read_eeprom`.
    pub fn with_eeprom(mut self, offset: usize, byte: u8) -> Self {
        self.eeprom.insert(offset, byte);
        self
    }

    /// Every `(offset, bytes)` written via `write_eeprom`, in order — for asserting the
    /// CMIS bring-up register writes (page-10h control bytes) a test drove.
    pub fn eeprom_writes(&self) -> Vec<(usize, Vec<u8>)> {
        self.eeprom_writes.lock().unwrap().clone()
    }

    /// How many times `get_presence()` has been called on this SFP (across all
    /// `MockHal::sfp` clones sharing this handle) — for asserting the CMIS task's
    /// per-tick HW presence polling.
    pub fn presence_calls(&self) -> usize {
        self.presence_calls.load(Ordering::SeqCst)
    }
}

impl SfpHandle for MockSfp {
    fn get_presence(&self) -> Result<bool> {
        self.presence_calls.fetch_add(1, Ordering::SeqCst);
        if self
            .presence_misses
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_sub(1))
            .is_ok()
        {
            return Ok(false);
        }
        Ok(self.presence)
    }
    fn is_replaceable(&self) -> Result<bool> {
        Ok(self.replaceable)
    }
    fn get_reset_status(&self) -> Result<bool> {
        Ok(self.reset_status)
    }
    fn sfp_type(&self) -> Result<String> {
        Ok(self.sfp_type.clone())
    }
    fn get_error_description(&self) -> Result<Option<String>> {
        Ok(self.error_description.clone())
    }
    fn get_transceiver_info(&self) -> Result<Value> {
        self.info_reads.fetch_add(1, Ordering::SeqCst);
        Ok(self.info.clone())
    }
    fn get_transceiver_dom_real_value(&self) -> Result<Value> {
        Ok(self.dom_real_value.clone())
    }
    fn get_transceiver_status(&self) -> Result<Value> {
        self.status_reads.fetch_add(1, Ordering::SeqCst);
        Ok(self.status.clone())
    }
    fn xcvr_api_call_json(&self, method: &str) -> Result<Value> {
        self.api_call_log.lock().unwrap().push(method.to_string());
        match self.api_calls.get(method) {
            Some(v) => Ok(v.clone()),
            None => Err(crate::error::XcvrdError::Bridge(format!(
                "mock: no canned xcvr api result for {method}"
            ))),
        }
    }
    fn get_transceiver_threshold_info(&self) -> Result<Value> {
        Ok(self.threshold_info.clone())
    }
    fn get_lpmode(&self) -> Result<bool> {
        if self.lpmode_err {
            return Err(XcvrdError::Other(
                "MockSfp: get_lpmode not implemented".into(),
            ));
        }
        Ok(self.lpmode)
    }
    fn set_lpmode(&self, _on: bool) -> Result<bool> {
        Ok(true)
    }
    fn reset(&self) -> Result<bool> {
        Ok(true)
    }
    fn has_method(&self, method: &str) -> Result<bool> {
        Ok(self.json_calls.contains_key(method))
    }
    fn call_json(&self, method: &str) -> Result<Value> {
        self.call_log.lock().unwrap().push(method.to_string());
        self.json_calls
            .get(method)
            .cloned()
            .ok_or_else(|| XcvrdError::Other(format!("MockSfp: no canned result for {method}")))
    }
    fn read_eeprom(&self, offset: usize, num_bytes: usize) -> Result<Option<Vec<u8>>> {
        let mut out = Vec::with_capacity(num_bytes);
        for o in offset..offset + num_bytes {
            match self.eeprom.get(&o) {
                Some(byte) => out.push(*byte),
                None => return Ok(None),
            }
        }
        Ok(Some(out))
    }
    fn write_eeprom(&self, offset: usize, data: &[u8]) -> Result<bool> {
        self.eeprom_writes
            .lock()
            .unwrap()
            .push((offset, data.to_vec()));
        Ok(true)
    }
}

/// A canned chassis: a slot table of [`MockSfp`] plus a queue of change events.
#[derive(Default)]
pub struct MockHal {
    pub sfps: Vec<MockSfp>,
    pub change_events: Mutex<Vec<ChangeEvent>>,
    /// Number of leading `get_change_event` calls that should return a transient
    /// `Err` (simulating a flaky PyO3/bridge poll the real emulator would swallow)
    /// before the queued events are served. Drives the daemon's resilient loop test.
    pub poll_errors: Mutex<usize>,
    /// When set, an idle (empty-queue) `get_change_event` reports `status = true` (an
    /// empty timeout poll, like the real emulator) instead of `status = false`. Lets the
    /// insert-soak re-inject a pending event on a subsequent idle poll (routing test).
    pub idle_poll_ready: AtomicBool,
}

impl MockHal {
    pub fn with_sfps(sfps: Vec<MockSfp>) -> Self {
        MockHal {
            sfps,
            change_events: Mutex::new(Vec::new()),
            poll_errors: Mutex::new(0),
            idle_poll_ready: AtomicBool::new(false),
        }
    }

    pub fn push_change_event(&self, ev: ChangeEvent) {
        self.change_events.lock().unwrap().push(ev);
    }

    /// Queue `n` transient `get_change_event` failures before events are served.
    pub fn fail_next_polls(&self, n: usize) {
        *self.poll_errors.lock().unwrap() += n;
    }

    /// Make idle (empty-queue) polls report `status = true` (an empty timeout poll, like
    /// the real emulator), so a soaked insert can be re-injected on a later idle poll.
    pub fn set_idle_poll_ready(&self, ready: bool) {
        self.idle_poll_ready.store(ready, Ordering::SeqCst);
    }
}

impl Hal for MockHal {
    fn num_sfps(&self) -> Result<usize> {
        Ok(self.sfps.len())
    }

    fn sfp(&self, index: usize) -> Result<Box<dyn SfpHandle>> {
        self.sfps
            .get(index)
            .cloned()
            .map(|s| Box::new(s) as Box<dyn SfpHandle>)
            .ok_or(XcvrdError::PhysicalPortNotExist)
    }

    fn get_change_event(&self, _timeout_ms: u64) -> Result<ChangeEvent> {
        {
            let mut pending = self.poll_errors.lock().unwrap();
            if *pending > 0 {
                *pending -= 1;
                return Err(XcvrdError::Bridge(
                    "mock transient change-event read error".into(),
                ));
            }
        }
        let mut q = self.change_events.lock().unwrap();
        if q.is_empty() {
            Ok(ChangeEvent {
                status: self.idle_poll_ready.load(Ordering::SeqCst),
                ..ChangeEvent::default()
            })
        } else {
            Ok(q.remove(0))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Mirrors tests/mock_swsscommon.py usage: build a table, set a row, assert
    // field counts/values exactly as the Python DOM/status posters' tests do.
    #[test]
    fn mock_db_table_set_get_and_field_count() {
        let tbl = MockDbTable::new("TRANSCEIVER_DOM_SENSOR");
        let fvs = vec![
            ("temperature".to_string(), "22.75".to_string()),
            ("voltage".to_string(), "3.30".to_string()),
            (
                "last_update_time".to_string(),
                "Thu Jan 01 00:00:00 1970".to_string(),
            ),
        ];
        tbl.set("Ethernet0", &fvs);

        assert_eq!(tbl.get_size(), 1);
        assert_eq!(tbl.get_size_for_key("Ethernet0"), 3);
        assert_eq!(
            tbl.hget("Ethernet0", "temperature").as_deref(),
            Some("22.75")
        );
        assert_eq!(tbl.get_keys(), vec!["Ethernet0".to_string()]);
        assert!(tbl.get("Ethernet0").is_some());
    }

    #[test]
    fn mock_db_table_del_and_hdel() {
        let tbl = MockDbTable::new("TRANSCEIVER_INFO");
        tbl.set(
            "Ethernet0",
            &[
                ("type".to_string(), "QSFP-DD".to_string()),
                ("vendor_rev".to_string(), "A1".to_string()),
            ],
        );
        tbl.hdel("Ethernet0", "vendor_rev");
        assert_eq!(tbl.get_size_for_key("Ethernet0"), 1);
        tbl.del("Ethernet0");
        assert_eq!(tbl.get_size(), 0);
        assert!(tbl.get("Ethernet0").is_none());
    }

    #[test]
    fn mock_sfp_and_hal_return_canned_values() {
        let sfp = MockSfp::present()
            .with_info(serde_json::json!({"type": "QSFP-DD", "vendor_rev": "A1"}))
            .with_json(
                "get_temperature",
                serde_json::json!({"temperature": "22.75"}),
            );
        let hal = MockHal::with_sfps(vec![sfp]);

        assert_eq!(hal.num_sfps().unwrap(), 1);
        let handle = hal.sfp(0).unwrap();
        assert!(handle.get_presence().unwrap());
        assert_eq!(handle.get_transceiver_info().unwrap()["type"], "QSFP-DD");
        assert_eq!(
            handle.call_json("get_temperature").unwrap()["temperature"],
            "22.75"
        );
        assert!(hal.sfp(9).is_err());
    }
}
