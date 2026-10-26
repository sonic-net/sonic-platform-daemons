//! `port_event_helper.py` → `PortMapping`, `PortChangeEvent`,
//! `PortChangeObserver`, `get_port_mapping` (analysis §3.2). `PortMapping` is used
//! throughout; the `PortChangeObserver` (soak/filter/dedup over `SubscriberStateTable`) drives
//! the DOM APPL_DB `PORT_TABLE` link-change watch alongside CMIS/DOM.
#![allow(dead_code, unused_variables, unused_imports)]

use std::collections::{BTreeMap, HashMap};
use std::os::fd::{AsRawFd, RawFd};
use std::time::Duration;

use swss_common::{
    DbConnector, KeyOpFieldValues, KeyOperation, SelectResult, SubscriberStateTable, Table,
};

use crate::error::{Result, XcvrdError};

/// `PortChangeEvent` types (`PORT_ADD/REMOVE/SET/DEL`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PortChangeEventType {
    Add,
    Remove,
    Set,
    Del,
}

/// A CONFIG_DB/APPL_DB/STATE_DB PORT change, deduplicated by the observer.
#[derive(Debug, Clone)]
pub struct PortChangeEvent {
    pub port_name: String,
    pub physical_port: Option<usize>,
    pub asic_id: usize,
    pub event_type: PortChangeEventType,
    pub db_name: String,
    pub table_name: String,
    /// The soaked+filtered field-value map that produced this event (the reference
    /// `PortChangeEvent.port_dict`). Empty for map-mutation events synthesised by
    /// `build_port_mapping`; the observer fills it in for real `SET`/`DEL` notifications.
    pub port_dict: BTreeMap<String, String>,
}

impl PortChangeEvent {
    pub fn new(
        port_name: String,
        physical_port: Option<usize>,
        asic_id: usize,
        event_type: PortChangeEventType,
        db_name: String,
        table_name: String,
    ) -> Self {
        PortChangeEvent {
            port_name,
            physical_port,
            asic_id,
            event_type,
            db_name,
            table_name,
            port_dict: BTreeMap::new(),
        }
    }

    /// Attach the soaked/filtered `port_dict` (the reference `PortChangeEvent.port_dict`).
    pub fn with_port_dict(mut self, port_dict: BTreeMap<String, String>) -> Self {
        self.port_dict = port_dict;
        self
    }
}

/// `is_front_panel_port(port, role)` — single-ASIC approximation (analysis §2:
/// `Ethernet*` front-panel). Front-panel ports are the `Ethernet<N>` optics ports;
/// the internal/inband/recycle ports (`Ethernet-IB0`, `Ethernet-Rec0`, …) carry a
/// `-`-suffixed name and are excluded, mirroring `multi_asic.is_front_panel_port`
/// well enough for the emulator testbed. A `Dpc` role (DPU-connect) is also excluded,
/// matching the reference's `role != Dpc` guard.
pub fn is_front_panel_port(port: &str, role: Option<&str>) -> bool {
    if matches!(role, Some(r) if r == "Dpc") {
        return false;
    }
    port.starts_with("Ethernet") && !port.starts_with("Ethernet-")
}

/// `PortMapping` — logical↔physical map (analysis §1.3).
#[derive(Default, Clone)]
pub struct PortMapping {
    /// The logical ports in insertion order (`PortMapping.logical_port_list`).
    logical_port_list: Vec<String>,
    logical_to_physical: BTreeMap<String, Vec<usize>>,
    physical_to_logical: BTreeMap<usize, Vec<String>>,
    logical_to_asic: BTreeMap<String, usize>,
}

impl PortMapping {
    pub fn new() -> Self {
        PortMapping::default()
    }

    /// `handle_port_change_event` — apply a `PORT_ADD`/`PORT_REMOVE` to the map
    /// (`SET`/`DEL` are handled by the observer, which emits add/remove).
    pub fn handle_port_change_event(&mut self, event: &PortChangeEvent) {
        match event.event_type {
            PortChangeEventType::Add => self.handle_port_add(event),
            PortChangeEventType::Remove => self.handle_port_remove(event),
            _ => {}
        }
    }

    fn handle_port_add(&mut self, event: &PortChangeEvent) {
        let Some(index) = event.physical_port else {
            return;
        };
        let name = event.port_name.clone();
        self.logical_port_list.push(name.clone());
        self.logical_to_physical.insert(name.clone(), vec![index]);
        let breakout = self.physical_to_logical.entry(index).or_default();
        breakout.push(name.clone());
        // Keep each physical port's logical list in natural (breakout) order, e.g.
        // Ethernet0 < Ethernet4 < Ethernet12 — matching Python `natsorted(_, key=lower)`.
        breakout.sort_by(|a, b| natural_cmp(&a.to_lowercase(), &b.to_lowercase()));
        self.logical_to_asic.insert(name, event.asic_id);
    }

    fn handle_port_remove(&mut self, event: &PortChangeEvent) {
        let name = &event.port_name;
        self.logical_port_list.retain(|p| p != name);
        self.logical_to_physical.remove(name);
        if let Some(index) = event.physical_port {
            if let Some(breakout) = self.physical_to_logical.get_mut(&index) {
                breakout.retain(|p| p != name);
                if breakout.is_empty() {
                    self.physical_to_logical.remove(&index);
                }
            }
        }
        self.logical_to_asic.remove(name);
    }

    /// `PortMapping.logical_port_list` — the configured logical ports, in order.
    pub fn logical_port_list(&self) -> &[String] {
        &self.logical_port_list
    }

    /// `get_asic_id_for_logical_port`.
    pub fn get_asic_id_for_logical_port(&self, port_name: &str) -> Option<usize> {
        self.logical_to_asic.get(port_name).copied()
    }

    /// `logical_to_physical(port)`.
    pub fn get_logical_to_physical(&self, port_name: &str) -> Option<Vec<usize>> {
        self.logical_to_physical.get(port_name).cloned()
    }

    /// `physical_to_logical(physical_port)` (natsorted breakout list).
    pub fn get_physical_to_logical(&self, physical_port: usize) -> Option<Vec<String>> {
        self.physical_to_logical.get(&physical_port).cloned()
    }

    /// Iterate `(physical_port, breakout logical ports)` in physical-port order —
    /// mirrors the reference `port_mapping.physical_to_logical.items()` sweep the DOM
    /// poll loop walks (subport-0 = `logical_ports[0]`).
    pub fn iter_physical_to_logical(&self) -> impl Iterator<Item = (usize, &[String])> {
        self.physical_to_logical
            .iter()
            .map(|(p, l)| (*p, l.as_slice()))
    }

    /// `is_logical_port(port)`.
    pub fn is_logical_port(&self, port_name: &str) -> bool {
        self.logical_to_physical.contains_key(port_name)
    }

    /// `logical_port_name_to_physical_port_list(port_name)` — the reference first
    /// tries to interpret the name as a bare physical index (`int(port_name)`), then
    /// falls back to the logical→physical map, else `None`. Used by
    /// `post_port_sfp_info_to_db` / `_init_port_sfp_status_sw_tbl`.
    pub fn logical_port_name_to_physical_port_list(&self, port_name: &str) -> Option<Vec<usize>> {
        if let Ok(index) = port_name.parse::<usize>() {
            return Some(vec![index]);
        }
        if self.is_logical_port(port_name) {
            self.get_logical_to_physical(port_name)
        } else {
            None
        }
    }
}

/// A CONFIG_DB `PORT` row as far as the mapping cares: the logical name plus the two
/// fields the front-panel filter + physical-index assignment read (`index`, `role`).
pub struct PortConfigRow {
    pub name: String,
    pub index: Option<usize>,
    pub role: Option<String>,
}

/// Build a [`PortMapping`] from CONFIG_DB `PORT` rows (the testable core of
/// [`get_port_mapping`]). Mirrors the reference loop: skip non-front-panel ports
/// (internal/inband/recycle names or a role such as `Dpc`), then emit a `PORT_ADD`
/// carrying the row's physical `index`.
pub fn build_port_mapping(
    rows: impl IntoIterator<Item = PortConfigRow>,
    asic_id: usize,
) -> PortMapping {
    let mut port_mapping = PortMapping::new();
    for row in rows {
        if !is_front_panel_port(&row.name, row.role.as_deref()) {
            continue;
        }
        let Some(index) = row.index else {
            continue;
        };
        port_mapping.handle_port_change_event(&PortChangeEvent::new(
            row.name,
            Some(index),
            asic_id,
            PortChangeEventType::Add,
            "CONFIG_DB".to_string(),
            "PORT".to_string(),
        ));
    }
    port_mapping
}

/// `get_port_mapping(namespaces)` — build the map from CONFIG_DB `PORT` (front-panel).
///
/// Mirrors the reference: read every `PORT|*` key and take the physical port from that
/// row's own `index` field (skipping non-front-panel rows by name/`role`). That is the
/// only mapping that is correct on real hardware, where a *breakout* switch has many
/// logical ports per cage — e.g. an Arista 7060x6 publishes 464 `PORT` rows over 66
/// SFPs, with `Ethernet0..Ethernet7` all carrying `index=1` — and where logical port
/// names are dense (`Ethernet0,Ethernet1,…`) rather than one-per-cage.
///
/// `num_sfps` is retained only for the legacy fallback below.
pub fn get_port_mapping(config: &DbConnector, num_sfps: usize) -> Result<PortMapping> {
    match read_port_config_rows(config) {
        Ok(rows) if !rows.is_empty() => {
            let mapping = build_port_mapping(rows, 0);
            if !mapping.logical_port_list().is_empty() {
                return Ok(mapping);
            }
            eprintln!(
                "xcvrd-rs: CONFIG_DB PORT has no front-panel rows; \
                 falling back to SFP-index port synthesis"
            );
        }
        Ok(_) => {
            eprintln!("xcvrd-rs: CONFIG_DB PORT is empty; falling back to SFP-index port synthesis")
        }
        Err(e) => eprintln!(
            "xcvrd-rs: cannot read the CONFIG_DB PORT table ({e}); \
             falling back to SFP-index port synthesis"
        ),
    }
    Ok(build_port_mapping(
        synthesised_port_rows(config, num_sfps),
        0,
    ))
}

/// The reference read: every CONFIG_DB `PORT` row, with its declared physical `index`
/// and `role`. A row without a parsable `index` is skipped with a log line (the
/// reference would raise `KeyError` and take the daemon down; skipping keeps the rest
/// of the plant monitored).
fn read_port_config_rows(config: &DbConnector) -> Result<Vec<PortConfigRow>> {
    // `Table` takes ownership of a connection, so hand it a clone of ours (same
    // DB/socket, same no-timeout policy as `env::open_config_db`).
    let table = Table::new(config.clone_timeout(0)?, "PORT")?;
    let mut rows = Vec::new();
    for name in table.get_keys()? {
        let Some(fvs) = table.get(&name)? else {
            continue;
        };
        let field = |f: &str| fvs.get(f).map(|v| v.to_string_lossy().into_owned());
        let Some(index) = field("index").and_then(|v| v.trim().parse::<usize>().ok()) else {
            eprintln!("xcvrd-rs: CONFIG_DB PORT|{name} has no usable 'index'; skipping");
            continue;
        };
        rows.push(PortConfigRow {
            name,
            index: Some(index),
            role: field("role"),
        });
    }
    Ok(rows)
}

/// Legacy emulator-shaped synthesis, kept only as a fallback for the case where the
/// CONFIG_DB `PORT` table cannot be read at all: the xcvr-emu bridge names SFP `i` as
/// the logical port `Ethernet{i*4}` (see `lib/emu.py:index_to_port`) and uses the
/// 0-based SFP index as the physical port, so a testbed with an unreadable PORT table
/// still comes up instead of monitoring nothing.
fn synthesised_port_rows(config: &DbConnector, num_sfps: usize) -> Vec<PortConfigRow> {
    let mut rows = Vec::new();
    for i in 0..num_sfps {
        let name = format!("Ethernet{}", i * 4);
        if config.exists(&format!("PORT|{name}")).unwrap_or(false) {
            rows.push(PortConfigRow {
                name,
                index: Some(i),
                role: None,
            });
        }
    }
    rows
}

/// Natural-order comparison of two strings (digit runs compared numerically), the
/// `natsort`-equivalent used to keep a physical port's breakout logical list in the
/// same order the reference daemon produces (`Ethernet4` before `Ethernet12`). The
/// caller lowercases first, matching `natsorted(_, key=lambda x: x.lower())`.
fn natural_cmp(a: &str, b: &str) -> std::cmp::Ordering {
    use std::cmp::Ordering;
    let mut ai = a.as_bytes().iter().peekable();
    let mut bi = b.as_bytes().iter().peekable();
    loop {
        match (ai.peek().copied(), bi.peek().copied()) {
            (None, None) => return Ordering::Equal,
            (None, Some(_)) => return Ordering::Less,
            (Some(_), None) => return Ordering::Greater,
            (Some(ca), Some(cb)) => {
                if ca.is_ascii_digit() && cb.is_ascii_digit() {
                    // Compare the two maximal digit runs numerically (skip leading
                    // zeros, then longer run wins, else lexical over equal length).
                    let ra = take_digits(&mut ai);
                    let rb = take_digits(&mut bi);
                    let ta = ra.trim_start_matches('0');
                    let tb = rb.trim_start_matches('0');
                    let ord = ta.len().cmp(&tb.len()).then_with(|| ta.cmp(tb));
                    if ord != Ordering::Equal {
                        return ord;
                    }
                } else {
                    let ord = ca.cmp(&cb);
                    if ord != Ordering::Equal {
                        return ord;
                    }
                    ai.next();
                    bi.next();
                }
            }
        }
    }
}

/// Consume and return the maximal leading run of ASCII digits from `it`.
fn take_digits(it: &mut std::iter::Peekable<std::slice::Iter<'_, u8>>) -> String {
    let mut run = String::new();
    while let Some(&&c) = it.peek() {
        if c.is_ascii_digit() {
            run.push(c as char);
            it.next();
        } else {
            break;
        }
    }
    run
}

/// A raw popped port event — the swss-common `(key, op, field_values)` tuple flattened
/// to owned strings so the soak/filter/dedup core ([`process_port_update_batch`]) is
/// testable without a live `SubscriberStateTable`.
pub struct PortUpdate {
    pub port_name: String,
    pub op: PortOp,
    pub fields: Vec<(String, String)>,
}

/// The `SET`/`DEL` operation carried by a [`PortUpdate`] (swss `SET_COMMAND`/`DEL_COMMAND`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PortOp {
    Set,
    Del,
}

impl PortOp {
    /// The swss-common command string (`"SET"`/`"DEL"`) stored in the soaked `port_dict`.
    pub fn as_str(self) -> &'static str {
        match self {
            PortOp::Set => "SET",
            PortOp::Del => "DEL",
        }
    }
}

/// `apply_filter_to_fvp` — when a `FILTER` is configured, drop every field that is
/// neither a filter field nor one of the always-kept core keys
/// (`index`, `port_name`, `asic_id`, `op`). Mirrors `PortChangeObserver.apply_filter_to_fvp`.
fn apply_filter_to_fvp(filter: Option<&[String]>, fvp: &mut BTreeMap<String, String>) {
    let Some(filter) = filter else {
        return;
    };
    const CORE: [&str; 4] = ["index", "port_name", "asic_id", "op"];
    fvp.retain(|k, _| CORE.contains(&k.as_str()) || filter.iter().any(|f| f == k));
}

/// The soak/filter/dedup core of [`PortChangeObserver::handle_port_update_event`] for a
/// single subscribed table — the testable analogue of the reference inner loop. For the
/// batch of `updates` popped this cycle it:
///   1. drops non-front-panel ports (tracking `role` across notifications like the
///      reference `port_role_map`);
///   2. augments each fvp with `index` (default `-1`), `port_name`, `asic_id`, `op`;
///   3. **soaks** duplicate keys within the batch, keeping only the last event *per
///      contiguous same-op run* (an op flip `DEL`<->`SET` finalizes the prior run so an
///      insert/remove transition is never swallowed);
///   4. applies the `FILTER`, then **dedups** against the persistent `port_event_cache`
///      (an unchanged event refreshes the cache but emits nothing);
///   5. emits a [`PortChangeEvent`] (`PORT_SET`/`PORT_DEL`) carrying the soaked
///      `port_dict` for every event that actually changed.
pub fn process_port_update_batch(
    updates: &[PortUpdate],
    db_name: &str,
    table_name: &str,
    asic_id: usize,
    filter: Option<&[String]>,
    role_map: &mut HashMap<String, String>,
    port_event_cache: &mut HashMap<(String, String, String), BTreeMap<String, String>>,
) -> Vec<PortChangeEvent> {
    // Soak within this batch, but PRESERVE op-transition boundaries. Consecutive updates
    // to the same (port_name, db, table) key with the SAME op collapse to last-write-wins
    // (the reference `port_event_cache[key] = fvp`), yet when a key's op FLIPS mid-batch
    // (`DEL` -> `SET` or `SET` -> `DEL`) the prior run is finalized as its own event instead
    // of being overwritten. The reference's `swsscommon.Select` wakes on each keyspace
    // notification separately, so a `TRANSCEIVER_INFO` DEL (transceiver unplug) and its later
    // SET (re-plug) are delivered in TWO distinct Select cycles and never soaked together.
    // A single `SubscriberStateTable::pops()` here can return BOTH in one batch; collapsing
    // them last-wins would yield one SET that dedups against the pre-unplug row -> no event,
    // so the CMIS / sfp-state manager never observes the remove+insert and never re-runs
    // datapath bring-up (the port stays stuck at its stale `cmis_state`). Finalizing on
    // op-change reproduces the reference's per-notification delivery.
    let mut runs: Vec<(PortOp, BTreeMap<String, String>)> = Vec::new();
    let mut last_run: HashMap<(String, String, String), usize> = HashMap::new();

    for u in updates {
        // Track/inherit the port role exactly like the reference: a role field on this
        // notification updates the cache; otherwise fall back to the cached role.
        let role = u
            .fields
            .iter()
            .find(|(k, _)| k == "role")
            .map(|(_, v)| v.clone());
        let role = match role {
            Some(r) => {
                role_map.insert(u.port_name.clone(), r.clone());
                Some(r)
            }
            None => role_map.get(&u.port_name).cloned(),
        };
        if !is_front_panel_port(&u.port_name, role.as_deref()) {
            continue;
        }

        let mut fvp: BTreeMap<String, String> = u.fields.iter().cloned().collect();
        fvp.entry("index".to_string())
            .or_insert_with(|| "-1".to_string());
        fvp.insert("port_name".to_string(), u.port_name.clone());
        fvp.insert("asic_id".to_string(), asic_id.to_string());
        fvp.insert("op".to_string(), u.op.as_str().to_string());

        let key = (
            u.port_name.clone(),
            db_name.to_string(),
            table_name.to_string(),
        );
        match last_run.get(&key).copied() {
            // Same op as the current open run for this key -> last-write-wins merge.
            Some(idx) if runs[idx].0 == u.op => {
                runs[idx].1 = fvp;
            }
            // First sight, or the op flipped -> open a new run so the transition is kept.
            _ => {
                last_run.insert(key, runs.len());
                runs.push((u.op, fvp));
            }
        }
    }

    let mut events = Vec::new();
    for (op, mut fvp) in runs {
        let key = (
            fvp.get("port_name").cloned().unwrap_or_default(),
            db_name.to_string(),
            table_name.to_string(),
        );
        let port_index: i64 = fvp.get("index").and_then(|s| s.parse().ok()).unwrap_or(-1);
        apply_filter_to_fvp(filter, &mut fvp);

        // Dedup against the last event on this key. The reference computes the *asymmetric*
        // difference `diff = set(fvp.items()) - set(cache[key].items())` and only re-emits
        // when `diff` is non-empty (`port_event_helper.py:178-184`). That is deliberately
        // one-directional: an event that merely *drops* fields (its fvp is a subset of the
        // cached one) yields an EMPTY diff and is NOT re-emitted. A plain `prev == fvp`
        // equality check breaks this — it treats a field removal as a change and re-emits.
        // For the CMIS `CONFIG_DB PORT` watch (no FILTER, so every field is kept) that means
        // an operator `hdel PORT|Ethernet<n> dom_polling` (e.g. the link-change test fixture
        // restoring the DOM-polling default) would surface a spurious PORT_SET and drive
        // `force_cmis_reinit`, restarting an already-READY port's datapath bring-up and
        // re-gating the DOM link-change re-read. Mirror the reference: re-emit only when some
        // (key, value) in `fvp` is absent-or-different in `prev`.
        if let Some(prev) = port_event_cache.get(&key) {
            let has_new_or_changed = fvp.iter().any(|(k, v)| prev.get(k) != Some(v));
            if !has_new_or_changed {
                port_event_cache.insert(key.clone(), fvp);
                continue;
            }
        }
        port_event_cache.insert(key.clone(), fvp.clone());

        let event_type = match op {
            PortOp::Set => PortChangeEventType::Set,
            PortOp::Del => PortChangeEventType::Del,
        };
        let physical_port = if port_index >= 0 {
            Some(port_index as usize)
        } else {
            None
        };
        events.push(
            PortChangeEvent::new(
                fvp.get("port_name").cloned().unwrap_or_default(),
                physical_port,
                asic_id,
                event_type,
                db_name.to_string(),
                table_name.to_string(),
            )
            .with_port_dict(fvp),
        );
    }
    events
}

/// Convert a batch of swss `KeyOpFieldValues` (as returned by
/// [`SubscriberStateTable::pops`]) into the owned [`PortUpdate`]s consumed by
/// [`process_port_update_batch`]. Shared by the live notification path
/// ([`PortChangeObserver::handle_port_update_event`]) and the initial-snapshot priming
/// ([`PortChangeObserver::prime_initial_snapshot`]).
fn pops_to_updates(pops: Vec<KeyOpFieldValues>) -> Vec<PortUpdate> {
    pops.into_iter()
        .map(|kfv| PortUpdate {
            port_name: kfv.key,
            op: match kfv.operation {
                KeyOperation::Set => PortOp::Set,
                KeyOperation::Del => PortOp::Del,
            },
            fields: kfv
                .field_values
                .into_iter()
                .map(|(k, v)| (k, v.to_string_lossy().into_owned()))
                .collect(),
        })
        .collect()
}

/// `PortChangeObserver` — subscribe to a single `{DB: table[, FILTER]}` and soak/dedup
/// its notifications into [`PortChangeEvent`]s. The DOM task watches
/// `{APPL_DB: PORT_TABLE, FILTER: ['flap_count']}` so a link-change flap triggers an
/// off-cadence diagnostic-flag re-read ([`crate::dom::dom_mgr`]). The reference watches a
/// *list* of tables via a `swsscommon.Select`; the DOM watch needs exactly one table, so a lone
/// `SubscriberStateTable` (whose `read_data` self-selects) suffices.
pub struct PortChangeObserver {
    sub: SubscriberStateTable,
    db_name: String,
    table_name: String,
    filter: Option<Vec<String>>,
    asic_id: usize,
    /// Persistent dedup cache keyed by `(port_name, db_name, table_name)` → last fvp,
    /// mirroring `PortChangeObserver.port_event_cache`.
    port_event_cache: HashMap<(String, String, String), BTreeMap<String, String>>,
    role_map: HashMap<String, String>,
    /// The `PortChangeEvent`s produced while folding the `SubscriberStateTable` **initial
    /// snapshot** into `port_event_cache`. These are intentionally NOT reported as live
    /// link-changes (they are the boot baseline), but a consumer may drain them via
    /// [`Self::take_initial_snapshot`] to seed its own per-port baseline so its dedup is
    /// independent of this observer's cache priming.
    initial_snapshot: Vec<PortChangeEvent>,
}

impl PortChangeObserver {
    /// Subscribe to `APPL_DB` `PORT_TABLE` filtered on `flap_count` — the DOM
    /// link-change watch (`DomInfoUpdateTask.DOM_PORT_CHG_OBSERVER_TBL_MAP`).
    pub fn for_appl_port_table() -> Result<Self> {
        let db = crate::env::open_appl_db()
            .map_err(|e| XcvrdError::Db(format!("open APPL_DB for PORT_TABLE watch: {e}")))?;
        Self::subscribe(
            db,
            "APPL_DB",
            "PORT_TABLE",
            Some(vec!["flap_count".to_string()]),
            0,
        )
    }

    /// Subscribe to one table on an already-open DB connection.
    pub fn subscribe(
        db: DbConnector,
        db_name: &str,
        table_name: &str,
        filter: Option<Vec<String>>,
        asic_id: usize,
    ) -> Result<Self> {
        let sub = SubscriberStateTable::new(db, table_name, None, None)
            .map_err(|e| XcvrdError::Db(format!("subscribe {db_name}.{table_name}: {e}")))?;
        let mut observer = PortChangeObserver {
            sub,
            db_name: db_name.to_string(),
            table_name: table_name.to_string(),
            filter,
            asic_id,
            port_event_cache: HashMap::new(),
            role_map: HashMap::new(),
            initial_snapshot: Vec::new(),
        };
        observer.prime_initial_snapshot();
        Ok(observer)
    }

    /// Fold the `SubscriberStateTable` **initial snapshot** into the dedup cache as the
    /// *baseline*, WITHOUT emitting any [`PortChangeEvent`].
    ///
    /// When a `SubscriberStateTable` is constructed, the underlying C++ ctor buffers every
    /// row that already exists in the watched table (here APPL_DB `PORT_TABLE`) and hands
    /// it back as one discrete `SET` batch on the very first `pops()` — *before* any
    /// keyspace notification. If those snapshot rows were surfaced like live notifications,
    /// the DOM task would treat each already-present `flap_count` as a fresh link-change and
    /// fire an off-cadence `TRANSCEIVER_DOM_FLAG` re-read at daemon start. The Python
    /// reference never does this: it keys `link_change_affected_ports` by the event's
    /// `index` field, which APPL_DB `PORT_TABLE` does not carry, so its snapshot re-reads
    /// resolve to no physical port and are dropped. To match that net behaviour we consume
    /// the snapshot here and prime `port_event_cache`/`role_map` with it, so only a genuine
    /// *post-subscription* `flap_count` change (a real flap) is reported downstream.
    fn prime_initial_snapshot(&mut self) {
        let pops = match self.sub.pops() {
            Ok(pops) => pops,
            Err(e) => {
                // A redis hiccup here only means the first genuine change re-establishes the
                // baseline; never take the DOM task down for a priming failure.
                eprintln!(
                    "xcvrd-rs: could not prime {}.{} initial snapshot ({e}); \
                     first post-subscribe change will re-baseline",
                    self.db_name, self.table_name
                );
                return;
            }
        };
        if pops.is_empty() {
            return;
        }
        let updates = pops_to_updates(pops);
        // Prime the cache/role map as a side effect. The baseline events are NOT reported
        // as live link-changes (so the initial snapshot never schedules a re-read), but we
        // retain them so a consumer can seed its own per-port baseline (see
        // `take_initial_snapshot`) and dedup independently of this cache.
        self.initial_snapshot = process_port_update_batch(
            &updates,
            &self.db_name,
            &self.table_name,
            self.asic_id,
            self.filter.as_deref(),
            &mut self.role_map,
            &mut self.port_event_cache,
        );
    }

    /// Drain the baseline events captured while priming the initial snapshot (see
    /// [`Self::prime_initial_snapshot`]). Returns them once — subsequent calls are empty.
    /// A consumer uses these to seed its own per-port dedup baseline so it rejects a
    /// re-delivered boot snapshot even if this observer's cache priming is bypassed.
    pub fn take_initial_snapshot(&mut self) -> Vec<PortChangeEvent> {
        std::mem::take(&mut self.initial_snapshot)
    }

    /// `handle_port_update_event(timeout_ms)` → the batch of deduplicated events. Blocks
    /// up to `timeout_ms` for a notification (returns an empty batch on select
    /// timeout/signal), then pops + soaks + filters + dedups the pending updates.
    pub fn handle_port_update_event(&mut self, timeout_ms: u64) -> Result<Vec<PortChangeEvent>> {
        match self
            .sub
            .read_data(Duration::from_millis(timeout_ms), false)
            .map_err(|e| {
                XcvrdError::Db(format!("read {}.{}: {e}", self.db_name, self.table_name))
            })? {
            SelectResult::Data => {}
            SelectResult::Signal | SelectResult::Timeout => return Ok(Vec::new()),
        }

        let pops = self.sub.pops().map_err(|e| {
            XcvrdError::Db(format!("pop {}.{}: {e}", self.db_name, self.table_name))
        })?;
        let updates = pops_to_updates(pops);

        Ok(process_port_update_batch(
            &updates,
            &self.db_name,
            &self.table_name,
            self.asic_id,
            self.filter.as_deref(),
            &mut self.role_map,
            &mut self.port_event_cache,
        ))
    }

    /// The raw fd of the underlying `SubscriberStateTable`'s redis connection. Adding this
    /// to a `poll()` set is the Rust-binding equivalent of `swsscommon.Select.addSelectable`
    /// — it lets [`MultiPortChangeObserver`] block on several tables at once (the binding
    /// exposes no `Select`). The fd stays valid while `self.sub` is alive.
    pub fn raw_fd(&self) -> Result<RawFd> {
        self.sub.get_fd().map(|fd| fd.as_raw_fd()).map_err(|e| {
            XcvrdError::Db(format!("get_fd {}.{}: {e}", self.db_name, self.table_name))
        })
    }
}

/// `MultiPortChangeObserver` — the CMIS-side analogue of the reference `PortChangeObserver`
/// watching `DEFAULT_PORT_TBL_MAP` (`port_event_helper.py:7`): a *list* of
/// `{DB: table[, FILTER]}` entries polled together. The reference registers every
/// `SubscriberStateTable` on one `swsscommon.Select` and, on any wake-up, drains ALL of
/// them. The Rust binding exposes no `Select`, so here each table is a standalone
/// [`PortChangeObserver`] and [`Self::handle_port_update_event`] slices the block across the
/// tables with each one's own swss-native `read_data(slice)` — waking on the FIRST table to
/// change and then draining them all. This matches the reference: an idle loop still blocks
/// ≈ the full select timeout, but a `SET`/`DEL` on ANY watched table is picked up promptly
/// and soaked/filtered/deduped into [`PortChangeEvent`]s in its own batch (so a rapid
/// unplug+re-plug is two events, not one deduped no-op). It deliberately does NOT `libc::poll`
/// the raw redis fd: that fd is readable at the socket level even without a new keyspace
/// notification, so polling it never blocks and the CMIS task busy-spins.
///
/// The CMIS manager needs `CONFIG_DB PORT` (`index`/`speed`/`lanes`/`subport`/
/// `admin_status` → the datapath bring-up trigger), `STATE_DB TRANSCEIVER_INFO` (xcvr
/// insert/remove), and `STATE_DB PORT_TABLE` filtered on `host_tx_ready`. The DOM task keeps
/// its single-table [`PortChangeObserver::for_appl_port_table`] (APPL_DB `flap_count`) — this
/// type does not affect it.
pub struct MultiPortChangeObserver {
    observers: Vec<PortChangeObserver>,
}

/// Per-table wait slice for the round-robin idle block in
/// [`MultiPortChangeObserver::handle_port_update_event`]. The total block over `n_tables`
/// swss-native `read_data(slice)` calls is `≈ timeout_ms`, so the CMIS bring-up keeps its
/// ~1 s per-state cadence. Guards two edges: `n_tables == 0` must not divide by zero
/// (degrade to the full timeout), and the slice is floored at 1 ms so a tiny timeout still
/// makes a real (non-busy) blocking call per table.
fn multi_observer_slice_ms(timeout_ms: u64, n_tables: usize) -> u64 {
    (timeout_ms / (n_tables.max(1) as u64)).max(1)
}

impl MultiPortChangeObserver {
    /// Subscribe to the reference `DEFAULT_PORT_TBL_MAP` for the CMIS manager:
    /// `[{CONFIG_DB: PORT}, {STATE_DB: TRANSCEIVER_INFO}, {STATE_DB: PORT_TABLE,
    /// FILTER:[host_tx_ready]}]`.
    pub fn for_cmis() -> Result<Self> {
        let cfg_db = crate::env::open_config_db()
            .map_err(|e| XcvrdError::Db(format!("open CONFIG_DB for PORT watch: {e}")))?;
        let state_db_info = crate::env::open_state_db().map_err(|e| {
            XcvrdError::Db(format!("open STATE_DB for TRANSCEIVER_INFO watch: {e}"))
        })?;
        let state_db_port = crate::env::open_state_db()
            .map_err(|e| XcvrdError::Db(format!("open STATE_DB for PORT_TABLE watch: {e}")))?;
        let observers = vec![
            PortChangeObserver::subscribe(cfg_db, "CONFIG_DB", "PORT", None, 0)?,
            PortChangeObserver::subscribe(state_db_info, "STATE_DB", "TRANSCEIVER_INFO", None, 0)?,
            PortChangeObserver::subscribe(
                state_db_port,
                "STATE_DB",
                "PORT_TABLE",
                Some(vec!["host_tx_ready".to_string()]),
                0,
            )?,
        ];
        Ok(Self { observers })
    }

    /// Subscribe to the reference SFF `PORT_TBL_MAP` (`sff_mgr.py:73`): `[{CONFIG_DB:
    /// PORT}, {STATE_DB: TRANSCEIVER_INFO, FILTER:[type]}, {STATE_DB: PORT_TABLE,
    /// FILTER:[host_tx_ready]}]`. The SFF manager needs `CONFIG_DB PORT`
    /// (`index`/`subport`/`lanes`/`admin_status`), the transceiver insert/remove signalled
    /// by `TRANSCEIVER_INFO`'s `type`, and `host_tx_ready` from STATE_DB `PORT_TABLE` (the
    /// `type`/`host_tx_ready` filters also drop the noisy sibling fields, and — per the
    /// reference comment — the `PORT_TABLE` filter keeps STATE_DB's stale `admin_status`
    /// out so only CONFIG_DB drives admin_status). The `op` core key is always retained,
    /// so a filtered `TRANSCEIVER_INFO` DEL still surfaces (never deduped against its SET).
    pub fn for_sff() -> Result<Self> {
        let cfg_db = crate::env::open_config_db()
            .map_err(|e| XcvrdError::Db(format!("open CONFIG_DB for PORT watch: {e}")))?;
        let state_db_info = crate::env::open_state_db().map_err(|e| {
            XcvrdError::Db(format!("open STATE_DB for TRANSCEIVER_INFO watch: {e}"))
        })?;
        let state_db_port = crate::env::open_state_db()
            .map_err(|e| XcvrdError::Db(format!("open STATE_DB for PORT_TABLE watch: {e}")))?;
        let observers = vec![
            PortChangeObserver::subscribe(cfg_db, "CONFIG_DB", "PORT", None, 0)?,
            PortChangeObserver::subscribe(
                state_db_info,
                "STATE_DB",
                "TRANSCEIVER_INFO",
                Some(vec!["type".to_string()]),
                0,
            )?,
            PortChangeObserver::subscribe(
                state_db_port,
                "STATE_DB",
                "PORT_TABLE",
                Some(vec!["host_tx_ready".to_string()]),
                0,
            )?,
        ];
        Ok(Self { observers })
    }

    /// Drain every subscribed table's boot snapshot, concatenated in table order. The CMIS
    /// task folds these into `port_dict` to seed each already-configured port at startup —
    /// `CONFIG_DB PORT` carries `index`/`speed`/`lanes`/`subport` so the datapath state
    /// machine starts at `INSERTED` for every port present at boot.
    pub fn take_initial_snapshot(&mut self) -> Vec<PortChangeEvent> {
        let mut all = Vec::new();
        for obs in &mut self.observers {
            all.extend(obs.take_initial_snapshot());
        }
        all
    }

    /// Poll all subscribed tables once, blocking up to `timeout_ms` for a notification on
    /// ANY of them — the reference `swsscommon.Select` "wait on every selectable, drain all
    /// on wake". The Rust binding exposes no `Select`, so:
    ///
    ///   1. **Fast path** — drain every table non-blocking first (`read_data(0)` reports
    ///      already-pending *and* cached data, like `Select`'s `hasData`); return if any
    ///      produced an event. This is what keeps a rapid unplug (`TRANSCEIVER_INFO` DEL) +
    ///      re-plug (SET) that both already landed from soaking into a single deduped no-op:
    ///      the DEL is drained now, the SET on the next call, each in its own batch.
    ///   2. Otherwise **block up to `timeout_ms`** by *slicing* the wait across the tables
    ///      round-robin — each table's own swss-native [`PortChangeObserver::handle_port_update_event`]
    ///      (`read_data(slice)`) blocks that table for its slice and returns promptly when it
    ///      wakes. An idle loop therefore sleeps ≈ the full `timeout_ms` (so CMIS bring-up
    ///      keeps its ~1 s per-state cadence), while a change on any table is picked up within
    ///      ~one slice.
    ///   3. On the first table to wake, drain the siblings (`0`) too, matching `Select`'s
    ///      "drain ALL on wake".
    ///
    /// This deliberately replaces an earlier `libc::poll(raw_fd)` block: a `SubscriberStateTable`'s
    /// redis connection fd stays readable at the *socket* level (buffered protocol bytes) even
    /// with no NEW keyspace notification, so `libc::poll` returned `POLLIN` immediately on every
    /// call and never actually blocked. The CMIS `task_worker` then busy-looped — racing the
    /// datapath state machine through all its intermediate `cmis_state`s far faster than the e2e
    /// poller samples them (only the terminal `READY` was ever observed), and pinning a core /
    /// hammering STATE_DB so the concurrent DOM poll that sources the `vccHAlarm` flag group was
    /// starved and never settled its baseline. swss `read_data` consults the library's own
    /// `Select`, which correctly reports `Timeout` when there is genuinely no new event.
    pub fn handle_port_update_event(&mut self, timeout_ms: u64) -> Result<Vec<PortChangeEvent>> {
        let ready = self.drain_all();
        if !ready.is_empty() {
            return Ok(ready);
        }
        // Slice the idle wait across the tables so the total block ≈ `timeout_ms` (no spin)
        // and any single table still wakes within ~one slice.
        let slice = multi_observer_slice_ms(timeout_ms, self.observers.len());
        for i in 0..self.observers.len() {
            let events = self.observers[i].handle_port_update_event(slice)?;
            if !events.is_empty() {
                // A table woke us — drain the others (non-blocking) so a change that landed on
                // a sibling while we were blocked on this one is delivered in the same wake.
                let mut all = events;
                all.extend(self.drain_all());
                return Ok(all);
            }
        }
        Ok(Vec::new())
    }

    /// Drain every subscribed table non-blocking (`read_data(0)`), concatenated in table
    /// (then soak) order. Each table's own soak/filter/dedup still applies per batch.
    fn drain_all(&mut self) -> Vec<PortChangeEvent> {
        let mut all = Vec::new();
        for obs in &mut self.observers {
            match obs.handle_port_update_event(0) {
                Ok(events) => all.extend(events),
                Err(e) => eprintln!("xcvrd-rs: CMIS multi-observer table drain error: {e}"),
            }
        }
        all
    }
}

/// Legacy emulator fallback when an update has no usable CONFIG_DB index.
/// Real hardware must use the row's own index, not this naming convention.
fn physical_index_from_name(name: &str) -> Option<usize> {
    name.strip_prefix("Ethernet")
        .and_then(|n| n.parse::<usize>().ok())
        .map(|n| n / 4)
}

/// The physical SFP index for a CONFIG_DB `PORT` update: the row's own `index` field
/// (what the reference `read_port_config_change` uses, `int(fvp['index'])`), falling back
/// to the name heuristic only when the row does not carry one.
///
/// `get_port_mapping` was moved onto CONFIG_DB `index` by
/// `0001-port-mapping-from-config-db` because the `Ethernet{i*4}` naming convention only
/// holds on the emulator; on a real breakout box it is simply wrong. This RUNTIME path was
/// left behind on the old heuristic, so the boot mapping and every later CONFIG_DB update
/// disagreed.
///
/// On `str43-7060x6-512-c17-u10` (514 ports over 66 cages, 8 subports each) `Ethernet268`
/// has `index` 34, but `268 / 4` = 67 -- past the last cage. `config interface
/// shutdown/startup` rewrites every PORT row, so a mass port flap re-added all 512 ports
/// at name-derived indexes: `Ethernet0..267` landed on 0..66 (valid-looking but the WRONG
/// cage) and `Ethernet268..511` on 67..127, which do not exist. Those ports then read
/// absent, the sfp-state task deleted their `TRANSCEIVER_INFO`, and the daemon logged
/// `SFP index 123 out of range (0-65)`. Measured after one flap: `INFO: 270` of 514, with
/// all 514 links up.
///
/// That is what the sonic-mgmt transceiver suite reported as "244/514 transceivers NOT
/// present", and it is the single root cause of all eight xcvrd-rs-only failures on
/// ptp-7060x6-6.
fn physical_index_for_update(u: &PortUpdate) -> Option<usize> {
    u.fields
        .iter()
        .find(|(k, _)| k == "index")
        .and_then(|(_, v)| v.trim().parse::<usize>().ok())
        .or_else(|| physical_index_from_name(&u.port_name))
}

/// `read_port_config_change` (`port_event_helper.py:307`) — translate a batch of raw
/// CONFIG_DB `PORT` `(key, op, fields)` updates into the `PORT_ADD`/`PORT_REMOVE` events
/// the `SfpStateUpdateTask` acts on, resolved against the CURRENT [`PortMapping`]:
///   * `SET` on a name not yet in the map (and carrying an `index` field, i.e. a
///     fully-formed row) → `PORT_ADD` (a freshly configured logical port);
///   * `SET` on a known name whose physical index changed → `PORT_REMOVE` then `PORT_ADD`
///     (a dynamic-port-breakout re-map);
///   * `DEL` on a known name → `PORT_REMOVE` (the logical port was deconfigured).
/// Non-front-panel names (internal/inband/recycle, or a `role` such as `Dpc`) are dropped.
/// The physical index comes from CONFIG_DB, consistent with [`get_port_mapping`].
/// The emulator naming convention is only a fallback for an unusable index.
pub fn read_port_config_change(
    updates: &[PortUpdate],
    port_mapping: &PortMapping,
    asic_id: usize,
) -> Vec<PortChangeEvent> {
    let mk = |name: &str, phys: usize, ev: PortChangeEventType| {
        PortChangeEvent::new(
            name.to_string(),
            Some(phys),
            asic_id,
            ev,
            "CONFIG_DB".to_string(),
            "PORT".to_string(),
        )
    };
    let mut events = Vec::new();
    for u in updates {
        let role = u
            .fields
            .iter()
            .find(|(k, _)| k == "role")
            .map(|(_, v)| v.as_str());
        if !is_front_panel_port(&u.port_name, role) {
            continue;
        }
        match u.op {
            PortOp::Set => {
                // The reference gates on a present `index` field — only a fully-formed PORT
                // row (not a half-written key) is treated as an add/re-map.
                if !u.fields.iter().any(|(k, _)| k == "index") {
                    continue;
                }
                let Some(new_phys) = physical_index_for_update(u) else {
                    continue;
                };
                if !port_mapping.is_logical_port(&u.port_name) {
                    events.push(mk(&u.port_name, new_phys, PortChangeEventType::Add));
                } else {
                    let current = port_mapping
                        .get_logical_to_physical(&u.port_name)
                        .and_then(|v| v.first().copied());
                    if current != Some(new_phys) {
                        if let Some(cur) = current {
                            events.push(mk(&u.port_name, cur, PortChangeEventType::Remove));
                        }
                        events.push(mk(&u.port_name, new_phys, PortChangeEventType::Add));
                    }
                }
            }
            PortOp::Del => {
                if port_mapping.is_logical_port(&u.port_name) {
                    let current = port_mapping
                        .get_logical_to_physical(&u.port_name)
                        .and_then(|v| v.first().copied())
                        .or_else(|| physical_index_for_update(u))
                        .unwrap_or(0);
                    events.push(mk(&u.port_name, current, PortChangeEventType::Remove));
                }
            }
        }
    }
    events
}

/// `subscribe_port_config_change` (`port_event_helper.py:283`) — a `SubscriberStateTable`
/// over CONFIG_DB `PORT`, drained each `SfpStateUpdateTask` loop by
/// [`PortConfigChangeSubscriber::poll`] and translated by [`read_port_config_change`].
///
/// The reference wraps this in a `swsscommon.Select`; a single table needs only the
/// table's own `read_data` self-select. The boot snapshot (every already-configured PORT
/// row) is primed-and-discarded in [`PortConfigChangeSubscriber::new`]: those ports are
/// already in the boot [`PortMapping`], so re-processing them would be a no-op — draining them here keeps the
/// first live `poll` to genuine post-boot config changes.
pub struct PortConfigChangeSubscriber {
    sub: SubscriberStateTable,
    asic_id: usize,
}

impl PortConfigChangeSubscriber {
    /// Subscribe to CONFIG_DB `PORT` on the given asic id (0 on this single-ASIC testbed).
    pub fn new(asic_id: usize) -> Result<Self> {
        let db = crate::env::open_config_db()
            .map_err(|e| XcvrdError::Db(format!("open CONFIG_DB for PORT config watch: {e}")))?;
        let sub = SubscriberStateTable::new(db, "PORT", None, None)
            .map_err(|e| XcvrdError::Db(format!("subscribe CONFIG_DB.PORT: {e}")))?;
        let s = Self { sub, asic_id };
        // Prime + discard the boot snapshot (already reflected in the boot PortMapping).
        if let Err(e) = s.sub.pops() {
            eprintln!(
                "xcvrd-rs: could not drain CONFIG_DB PORT boot snapshot ({e}); \
                 first live config change re-baselines"
            );
        }
        Ok(s)
    }

    /// Block up to `timeout_ms` for a CONFIG_DB `PORT` change; return the raw popped
    /// updates (empty on timeout/signal/error — never fatal). Mirrors
    /// `handle_port_config_change`'s `sel.select(SELECT_TIMEOUT_MSECS)` + drain.
    pub fn poll(&mut self, timeout_ms: u64) -> Vec<PortUpdate> {
        match self.sub.read_data(Duration::from_millis(timeout_ms), false) {
            Ok(SelectResult::Data) => {}
            Ok(SelectResult::Signal) | Ok(SelectResult::Timeout) => return Vec::new(),
            Err(e) => {
                eprintln!("xcvrd-rs: CONFIG_DB PORT read error: {e}");
                return Vec::new();
            }
        }
        match self.sub.pops() {
            Ok(pops) => pops_to_updates(pops),
            Err(e) => {
                eprintln!("xcvrd-rs: CONFIG_DB PORT pop error: {e}");
                Vec::new()
            }
        }
    }

    /// The asic id this subscriber tags its events with.
    pub fn asic_id(&self) -> usize {
        self.asic_id
    }
}

// --- boot readiness gate (wait_for_port_config_done) --------------------------

/// The reference `port_event_helper.SELECT_TIMEOUT_MSECS` (`port_event_helper.py:6`) —
/// the per-`Select.select()` block for the boot readiness gate and the observer loops.
pub const SELECT_TIMEOUT_MSECS: u64 = 1000;

/// The `PORT_TABLE` sentinel keys portsyncd/portmgrd write once every front-panel port
/// has been provisioned. `wait_for_port_config_done` blocks until one is popped
/// (`xcvrd.py:934`, `cmis_manager_task.py:814`).
pub const PORT_CONFIG_DONE_KEYS: [&str; 2] = ["PortConfigDone", "PortInitDone"];

/// Whether `key` is a `PortConfigDone`/`PortInitDone` boot-readiness sentinel.
pub fn is_port_config_done_key(key: &str) -> bool {
    PORT_CONFIG_DONE_KEYS.contains(&key)
}

/// One `Select.select(SELECT_TIMEOUT_MSECS)` wake + `port_tbl.pop()` drain for the boot
/// readiness gate. `Keys(v)` = the key(s) popped this cycle (the reference pops one key per
/// select, but a batched drain is accepted); `Timeout` = the select timed out (or a
/// transient read/pop error) with no key this cycle.
pub enum PortConfigPoll {
    Keys(Vec<String>),
    Timeout,
}

/// Source of `PORT_TABLE` notifications for [`wait_for_port_config_done_gate`]. Each
/// [`Self::poll`] models one reference `Select.select()` + `port_tbl.pop()` cycle.
/// Production wraps a real `PORT_TABLE` `SubscriberStateTable`
/// ([`PortConfigDoneSubscriber`]); unit tests inject a scripted mock — the Rust analogue of
/// the Python test's `mock_selectable.pop` side_effect.
pub trait PortConfigNotifier {
    fn poll(&mut self) -> PortConfigPoll;
}

/// Outcome of the boot readiness gate ([`wait_for_port_config_done_gate`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PortConfigGate {
    /// A `PortConfigDone`/`PortInitDone` sentinel was popped — port config is done.
    Done,
    /// The stop flag was set before the sentinel arrived (a shutdown during boot).
    Stopped,
    /// The bounded poll budget elapsed before the sentinel arrived. On the emulated pmon
    /// testbed no portsyncd ever writes the sentinel (CONFIG_DB is pre-populated instead),
    /// so the daemon proceeds anyway — the gate is a bounded best-effort that must never
    /// hang the daemon, which would break the e2e gate.
    TimedOut,
}

/// `wait_for_port_config_done` loop (`xcvrd.py:916`, `cmis_manager_task.py:796`), abstracted
/// over its notification source + stop flag so it is unit-testable and shared by
/// [`crate::xcvrd::DaemonXcvrd`] and
/// [`crate::cmis::cmis_manager_task::CmisManagerTask`].
///
/// Polls `notifier` until a `PortConfigDone`/`PortInitDone` key is popped
/// (→ [`PortConfigGate::Done`], the reference `break`), `stop()` becomes true
/// (→ [`PortConfigGate::Stopped`], the reference `while not stop_event.is_set()`), or
/// `max_polls` cycles elapse without the sentinel (→ [`PortConfigGate::TimedOut`]). The
/// reference loop is unbounded because portsyncd always eventually signals; `max_polls`
/// bounds it for the emulated testbed where no such signal exists, guaranteeing the daemon
/// always proceeds to build the port mapping and start producers.
///
/// Returns the outcome and the number of poll cycles performed — the analogue of the Python
/// test's `swsscommon.Select.select.call_count`.
pub fn wait_for_port_config_done_gate(
    notifier: &mut dyn PortConfigNotifier,
    stop: &dyn Fn() -> bool,
    max_polls: usize,
) -> (PortConfigGate, usize) {
    let mut polls = 0usize;
    loop {
        if stop() {
            return (PortConfigGate::Stopped, polls);
        }
        if polls >= max_polls {
            return (PortConfigGate::TimedOut, polls);
        }
        polls += 1;
        if let PortConfigPoll::Keys(keys) = notifier.poll() {
            if keys.iter().any(|k| is_port_config_done_key(k)) {
                return (PortConfigGate::Done, polls);
            }
        }
    }
}

/// Production [`PortConfigNotifier`] over a real `PORT_TABLE` `SubscriberStateTable`,
/// mirroring the reference `wait_for_port_config_done`'s
/// `SubscriberStateTable(appl_db, APP_PORT_TABLE_NAME)` + `Select` (`xcvrd.py:918-922`).
///
/// Unlike [`PortChangeObserver`] it does NOT front-panel-filter: the reference gate pops
/// EVERY key and breaks on the `PortConfigDone`/`PortInitDone` sentinel, which is not an
/// `Ethernet<N>` name and would be filtered out by the observer. The first [`Self::poll`]
/// drains the `SubscriberStateTable` boot snapshot directly (so a pre-populated sentinel is
/// a fast no-op); later polls block up to `timeout_ms` on `read_data` before draining.
pub struct PortConfigDoneSubscriber {
    sub: SubscriberStateTable,
    timeout_ms: u64,
    primed: bool,
}

impl PortConfigDoneSubscriber {
    /// Subscribe to APPL_DB `PORT_TABLE` for the boot readiness gate (the reference
    /// `daemon_base.db_connect("APPL_DB")` + `SubscriberStateTable(_, APP_PORT_TABLE_NAME)`,
    /// `xcvrd.py:918-921`).
    pub fn for_appl_port_table(timeout_ms: u64) -> Result<Self> {
        let db = crate::env::open_appl_db()
            .map_err(|e| XcvrdError::Db(format!("open APPL_DB for PortConfigDone gate: {e}")))?;
        Self::subscribe(db, "PORT_TABLE", timeout_ms)
    }

    fn subscribe(db: DbConnector, table_name: &str, timeout_ms: u64) -> Result<Self> {
        let sub = SubscriberStateTable::new(db, table_name, None, None).map_err(|e| {
            XcvrdError::Db(format!(
                "subscribe {table_name} for PortConfigDone gate: {e}"
            ))
        })?;
        Ok(Self {
            sub,
            timeout_ms,
            primed: false,
        })
    }
}

impl PortConfigNotifier for PortConfigDoneSubscriber {
    fn poll(&mut self) -> PortConfigPoll {
        // The first poll drains the boot snapshot directly (a pre-populated sentinel returns
        // immediately — the "fast no-op on the pre-populated testbed"); subsequent polls
        // block for a live notification first, mirroring the reference select loop.
        if self.primed {
            match self
                .sub
                .read_data(Duration::from_millis(self.timeout_ms), false)
            {
                Ok(SelectResult::Data) => {}
                Ok(SelectResult::Signal) | Ok(SelectResult::Timeout) => {
                    return PortConfigPoll::Timeout
                }
                Err(e) => {
                    eprintln!("xcvrd-rs: PortConfigDone gate read error: {e}");
                    return PortConfigPoll::Timeout;
                }
            }
        }
        self.primed = true;
        match self.sub.pops() {
            Ok(pops) => PortConfigPoll::Keys(pops.into_iter().map(|kfv| kfv.key).collect()),
            Err(e) => {
                eprintln!("xcvrd-rs: PortConfigDone gate pop error: {e}");
                PortConfigPoll::Timeout
            }
        }
    }
}

/// Default boot readiness gate budget: how many poll cycles to consult before proceeding.
/// `1` makes the production gate a true FAST NO-OP on the pre-populated pmon testbed — the
/// first poll drains the `PORT_TABLE` snapshot NON-BLOCKINGLY (so a pre-populated
/// `PortConfigDone` opens the gate immediately) and, absent the sentinel, the gate proceeds
/// at once WITHOUT a blocking `SELECT_TIMEOUT_MSECS` wait. This is deliberate: the sentinel
/// never arrives on the testbed (no portsyncd — CONFIG_DB is pre-populated), and a blocking
/// wait here would delay/stall the CMIS + main boot threads, pushing datapath bring-up past
/// the DomInfoUpdateTask publish cadence and breaking the e2e gate. This can be
/// widened via `XCVRD_PORT_CONFIG_GATE_MAX_POLLS` to actually wait for a
/// live sentinel on real hardware; unit tests pass an explicit budget to exercise the loop.
const DEFAULT_PORT_CONFIG_GATE_MAX_POLLS: usize = 1;

/// The boot readiness gate poll budget, overridable via `XCVRD_PORT_CONFIG_GATE_MAX_POLLS`
/// (overridable without a rebuild). See [`DEFAULT_PORT_CONFIG_GATE_MAX_POLLS`].
pub fn port_config_gate_max_polls() -> usize {
    std::env::var("XCVRD_PORT_CONFIG_GATE_MAX_POLLS")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .filter(|n| *n > 0)
        .unwrap_or(DEFAULT_PORT_CONFIG_GATE_MAX_POLLS)
}

/// Run the production boot readiness gate for one namespace: subscribe to APPL_DB
/// `PORT_TABLE` and poll (bounded) for the `PortConfigDone`/`PortInitDone` sentinel, using
/// `stop` so a SIGTERM during boot ends the wait. Best-effort — a subscribe failure, or the
/// bounded timeout on the pre-populated testbed, PROCEEDS rather than hanging (the shared
/// implementation behind [`crate::xcvrd::DaemonXcvrd::wait_for_port_config_done`],
/// [`crate::daemon::serve`], and [`crate::cmis::cmis_manager_task::CmisManagerTask::wait_for_port_config_done`]).
/// Returns the gate outcome for logging/gating.
pub fn run_port_config_done_gate(namespace: &str, stop: &dyn Fn() -> bool) -> PortConfigGate {
    let mut notifier = match PortConfigDoneSubscriber::for_appl_port_table(SELECT_TIMEOUT_MSECS) {
        Ok(n) => n,
        Err(e) => {
            eprintln!(
                "xcvrd-rs: PortConfigDone gate: could not subscribe APPL_DB PORT_TABLE for \
                 namespace {namespace:?} ({e}); proceeding (CONFIG_DB is pre-populated on the \
                 pmon testbed)"
            );
            return PortConfigGate::TimedOut;
        }
    };
    let (outcome, polls) =
        wait_for_port_config_done_gate(&mut notifier, stop, port_config_gate_max_polls());
    match outcome {
        PortConfigGate::Done => eprintln!(
            "xcvrd-rs: PortConfigDone gate: port config done for namespace {namespace:?} after \
             {polls} poll(s)"
        ),
        PortConfigGate::Stopped => eprintln!(
            "xcvrd-rs: PortConfigDone gate: shutdown requested during boot (namespace \
             {namespace:?})"
        ),
        PortConfigGate::TimedOut => eprintln!(
            "xcvrd-rs: PortConfigDone gate: no PortConfigDone after {polls} poll(s) for namespace \
             {namespace:?}; proceeding (the pre-populated pmon testbed has no portsyncd sentinel)"
        ),
    }
    outcome
}

#[cfg(test)]
mod tests {
    use super::*;

    fn add(pm: &mut PortMapping, name: &str, index: usize, asic: usize) {
        pm.handle_port_change_event(&PortChangeEvent::new(
            name.to_string(),
            Some(index),
            asic,
            PortChangeEventType::Add,
            "CONFIG_DB".to_string(),
            "PORT".to_string(),
        ));
    }

    fn remove(pm: &mut PortMapping, name: &str, index: usize) {
        pm.handle_port_change_event(&PortChangeEvent::new(
            name.to_string(),
            Some(index),
            0,
            PortChangeEventType::Remove,
            "CONFIG_DB".to_string(),
            "PORT".to_string(),
        ));
    }

    // Mirrors tests/test_xcvrd.py::test_get_port_mapping's PortMapping assertions:
    // a PORT_ADD populates the three maps + logical_port_list.
    #[test]
    fn handle_port_add_populates_maps() {
        let mut pm = PortMapping::new();
        add(&mut pm, "Ethernet0", 0, 0);
        add(&mut pm, "Ethernet4", 1, 0);

        assert_eq!(pm.logical_port_list(), &["Ethernet0", "Ethernet4"]);
        assert!(pm.is_logical_port("Ethernet0"));
        assert_eq!(pm.get_logical_to_physical("Ethernet0"), Some(vec![0]));
        assert_eq!(
            pm.get_physical_to_logical(1),
            Some(vec!["Ethernet4".to_string()])
        );
        assert_eq!(pm.get_asic_id_for_logical_port("Ethernet4"), Some(0));
        // Absent port → None everywhere.
        assert_eq!(pm.get_asic_id_for_logical_port("Ethernet8"), None);
        assert_eq!(pm.get_logical_to_physical("Ethernet8"), None);
        assert!(!pm.is_logical_port("Ethernet8"));
    }

    // A PORT_REMOVE (physical unplug / PORT_DEL) reverses the add and cleans up the
    // physical breakout entry when its last logical port leaves.
    #[test]
    fn handle_port_remove_reverses_add() {
        let mut pm = PortMapping::new();
        add(&mut pm, "Ethernet0", 0, 0);
        add(&mut pm, "Ethernet4", 1, 0);
        remove(&mut pm, "Ethernet0", 0);

        assert_eq!(pm.logical_port_list(), &["Ethernet4"]);
        assert!(!pm.is_logical_port("Ethernet0"));
        assert_eq!(pm.get_physical_to_logical(0), None);
        assert_eq!(pm.get_asic_id_for_logical_port("Ethernet0"), None);
    }

    // Breakout: several logical subports share one physical index, kept in natural
    // (natsorted) order, not lexicographic — Ethernet4 before Ethernet12.
    #[test]
    fn breakout_physical_to_logical_is_natural_sorted() {
        let mut pm = PortMapping::new();
        add(&mut pm, "Ethernet12", 3, 0);
        add(&mut pm, "Ethernet0", 3, 0);
        add(&mut pm, "Ethernet4", 3, 0);
        assert_eq!(
            pm.get_physical_to_logical(3),
            Some(vec![
                "Ethernet0".to_string(),
                "Ethernet4".to_string(),
                "Ethernet12".to_string()
            ])
        );
    }

    #[test]
    fn natural_cmp_orders_numeric_suffixes() {
        use std::cmp::Ordering;
        assert_eq!(natural_cmp("ethernet4", "ethernet12"), Ordering::Less);
        assert_eq!(natural_cmp("ethernet12", "ethernet4"), Ordering::Greater);
        assert_eq!(natural_cmp("ethernet0", "ethernet0"), Ordering::Equal);
        assert_eq!(natural_cmp("ethernet8", "ethernet10"), Ordering::Less);
    }

    #[test]
    fn is_front_panel_port_excludes_internal_ports() {
        assert!(is_front_panel_port("Ethernet0", None));
        assert!(is_front_panel_port("Ethernet128", Some("Ext")));
        assert!(!is_front_panel_port("Ethernet-IB0", None));
        assert!(!is_front_panel_port("Cpu0", None));
    }

    // ← tests/test_xcvrd.py::test_get_port_mapping (CONFIG_DB PORT read path). The real
    // get_port_mapping needs a live DbConnector, so we exercise its pure core
    // (build_port_mapping): front-panel filtering + physical-index assignment.
    #[test]
    fn build_port_mapping_filters_front_panel_and_assigns_index() {
        let rows = vec![
            PortConfigRow {
                name: "Ethernet0".into(),
                index: Some(0),
                role: None,
            },
            PortConfigRow {
                name: "Ethernet4".into(),
                index: Some(1),
                role: None,
            },
            // internal/inband + DPU-connect ports are dropped.
            PortConfigRow {
                name: "Ethernet-IB0".into(),
                index: Some(2),
                role: None,
            },
            PortConfigRow {
                name: "Ethernet8".into(),
                index: Some(2),
                role: Some("Dpc".into()),
            },
            // a front-panel row missing its index is skipped.
            PortConfigRow {
                name: "Ethernet12".into(),
                index: None,
                role: None,
            },
        ];
        let pm = build_port_mapping(rows, 0);
        assert_eq!(pm.logical_port_list(), &["Ethernet0", "Ethernet4"]);
        assert_eq!(pm.get_logical_to_physical("Ethernet0"), Some(vec![0]));
        assert_eq!(pm.get_logical_to_physical("Ethernet4"), Some(vec![1]));
        assert!(!pm.is_logical_port("Ethernet-IB0"));
        assert!(!pm.is_logical_port("Ethernet8"));
        assert!(!pm.is_logical_port("Ethernet12"));
    }

    // logical_port_name_to_physical_port_list: numeric name → itself; known logical
    // port → its physical list; unknown → None (mirrors the reference fallback chain).
    #[test]
    fn logical_port_name_to_physical_port_list_resolves() {
        let mut pm = PortMapping::new();
        add(&mut pm, "Ethernet4", 1, 0);
        assert_eq!(
            pm.logical_port_name_to_physical_port_list("Ethernet4"),
            Some(vec![1])
        );
        assert_eq!(
            pm.logical_port_name_to_physical_port_list("7"),
            Some(vec![7])
        );
        assert_eq!(
            pm.logical_port_name_to_physical_port_list("Ethernet99"),
            None
        );
    }

    // ← tests/test_xcvrd.py::test_handle_port_update_event. The real
    // handle_port_update_event needs a live SubscriberStateTable, so we exercise its
    // soak/filter/dedup/emit core (process_port_update_batch) exactly as the reference
    // asserts: front-panel filtering, field filtering, duplicate suppression, soaking
    // multiple events on a key, and SET/DEL emission.
    #[test]
    fn test_handle_port_update_event() {
        const CONFIG_DB: &str = "CONFIG_DB";
        const PORT_TABLE: &str = "PORT_TABLE";

        fn su(name: &str, op: PortOp, fields: &[(&str, &str)]) -> PortUpdate {
            PortUpdate {
                port_name: name.to_string(),
                op,
                fields: fields
                    .iter()
                    .map(|(k, v)| (k.to_string(), v.to_string()))
                    .collect(),
            }
        }
        let run =
            |updates: &[PortUpdate],
             filter: Option<&[String]>,
             role_map: &mut HashMap<String, String>,
             cache: &mut HashMap<(String, String, String), BTreeMap<String, String>>| {
                process_port_update_batch(
                    updates, CONFIG_DB, PORT_TABLE, 0, filter, role_map, cache,
                )
            };
        let key = || {
            (
                String::from("Ethernet0"),
                CONFIG_DB.to_string(),
                PORT_TABLE.to_string(),
            )
        };

        // --- Basic single update, NO filter: 'fec' is NOT filtered out. ---
        let mut role_map = HashMap::new();
        let mut cache = HashMap::new();
        let events = run(
            &[su(
                "Ethernet0",
                PortOp::Set,
                &[("index", "1"), ("speed", "40000"), ("fec", "rs")],
            )],
            None,
            &mut role_map,
            &mut cache,
        );
        assert_eq!(events.len(), 1);
        let expected: BTreeMap<String, String> = [
            ("port_name", "Ethernet0"),
            ("index", "1"),
            ("op", "SET"),
            ("asic_id", "0"),
            ("speed", "40000"),
            ("fec", "rs"),
        ]
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect();
        assert_eq!(cache.get(&key()), Some(&expected));
        assert_eq!(events[0].port_dict, expected);

        // --- Basic single update WITH filter ['speed']: 'fec' IS filtered out. ---
        let mut cache = HashMap::new(); // fresh observer, like the Python test
        let events = run(
            &[su(
                "Ethernet0",
                PortOp::Set,
                &[("index", "1"), ("speed", "40000"), ("fec", "rs")],
            )],
            Some(&["speed".to_string()]),
            &mut role_map,
            &mut cache,
        );
        assert_eq!(events.len(), 1);
        let filtered: BTreeMap<String, String> = [
            ("port_name", "Ethernet0"),
            ("index", "1"),
            ("op", "SET"),
            ("asic_id", "0"),
            ("speed", "40000"),
        ]
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect();
        assert_eq!(cache.get(&key()), Some(&filtered));
        assert_eq!(events[0].port_name, "Ethernet0");
        assert_eq!(events[0].event_type, PortChangeEventType::Set);
        assert_eq!(events[0].physical_port, Some(1));
        assert_eq!(events[0].asic_id, 0);
        assert_eq!(events[0].db_name, CONFIG_DB);
        assert_eq!(events[0].table_name, PORT_TABLE);
        assert_eq!(events[0].port_dict, filtered);

        // --- Duplicate update on the same key: nothing new is emitted. ---
        let events = run(
            &[su(
                "Ethernet0",
                PortOp::Set,
                &[("index", "1"), ("speed", "40000"), ("fec", "rs")],
            )],
            Some(&["speed".to_string()]),
            &mut role_map,
            &mut cache,
        );
        assert!(events.is_empty());
        assert_eq!(cache.get(&key()), Some(&filtered));

        // --- Soak multiple different updates on the same key: only the last wins. ---
        let events = run(
            &[
                su(
                    "Ethernet0",
                    PortOp::Set,
                    &[("index", "1"), ("speed", "100000")],
                ),
                su(
                    "Ethernet0",
                    PortOp::Set,
                    &[("index", "1"), ("speed", "200000")],
                ),
                su(
                    "Ethernet0",
                    PortOp::Set,
                    &[("index", "1"), ("speed", "400000")],
                ),
            ],
            Some(&["speed".to_string()]),
            &mut role_map,
            &mut cache,
        );
        assert_eq!(events.len(), 1);
        assert_eq!(
            cache.get(&key()).unwrap().get("speed").map(String::as_str),
            Some("400000")
        );

        // --- DEL case: op transition emits a PORT_DEL event. ---
        let events = run(
            &[su(
                "Ethernet0",
                PortOp::Del,
                &[("index", "1"), ("speed", "400000")],
            )],
            Some(&["speed".to_string()]),
            &mut role_map,
            &mut cache,
        );
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].event_type, PortChangeEventType::Del);

        // --- Non-front-panel ports are dropped (no event, no cache entry). ---
        let mut cache = HashMap::new();
        let events = run(
            &[su(
                "Ethernet-IB0",
                PortOp::Set,
                &[("index", "9"), ("flap_count", "3")],
            )],
            Some(&["flap_count".to_string()]),
            &mut role_map,
            &mut cache,
        );
        assert!(events.is_empty());
        assert!(cache.is_empty());
    }

    // Regression: the observer dedup must mirror the reference's *asymmetric* diff
    // (`diff = set(fvp.items()) - set(cache[key].items())`, port_event_helper.py:178). A
    // SET that only *drops* a field (its fvp is a subset of the cached one) has an empty
    // diff and MUST NOT be re-emitted. An exact-equality dedup would re-emit it, which on
    // the CMIS `CONFIG_DB PORT` watch spuriously `force_cmis_reinit`s an already-READY port
    // — exactly what breaks the link-change flag re-capture when the test fixture removes
    // `dom_polling` (`test_link_change_flags`). A genuine field addition/change still emits.
    #[test]
    fn test_field_removal_is_not_reemitted() {
        const CONFIG_DB: &str = "CONFIG_DB";
        const PORT: &str = "PORT";

        fn su(name: &str, op: PortOp, fields: &[(&str, &str)]) -> PortUpdate {
            PortUpdate {
                port_name: name.to_string(),
                op,
                fields: fields
                    .iter()
                    .map(|(k, v)| (k.to_string(), v.to_string()))
                    .collect(),
            }
        }
        let run =
            |updates: &[PortUpdate],
             role_map: &mut HashMap<String, String>,
             cache: &mut HashMap<(String, String, String), BTreeMap<String, String>>| {
                process_port_update_batch(updates, CONFIG_DB, PORT, 0, None, role_map, cache)
            };
        let key = || {
            (
                String::from("Ethernet48"),
                CONFIG_DB.to_string(),
                PORT.to_string(),
            )
        };

        let mut role_map = HashMap::new();
        let mut cache = HashMap::new();

        // Baseline SET carrying `dom_polling` (+ the CMIS bring-up trigger fields).
        let events = run(
            &[su(
                "Ethernet48",
                PortOp::Set,
                &[
                    ("index", "12"),
                    ("speed", "40000"),
                    ("admin_status", "up"),
                    ("dom_polling", "disabled"),
                ],
            )],
            &mut role_map,
            &mut cache,
        );
        assert_eq!(events.len(), 1);
        assert_eq!(
            cache
                .get(&key())
                .unwrap()
                .get("dom_polling")
                .map(String::as_str),
            Some("disabled")
        );

        // `hdel PORT|Ethernet48 dom_polling`: the popped entry keeps every other field, so its
        // fvp is a strict subset of the cached one. Reference diff is empty -> NO event, and
        // the cache is refreshed to the smaller fvp (so `dom_polling` is now absent).
        let events = run(
            &[su(
                "Ethernet48",
                PortOp::Set,
                &[("index", "12"), ("speed", "40000"), ("admin_status", "up")],
            )],
            &mut role_map,
            &mut cache,
        );
        assert!(
            events.is_empty(),
            "a pure field removal (fvp subset of cache) must not re-emit -> no spurious \
             force_cmis_reinit; got {events:?}"
        );
        assert!(cache.get(&key()).unwrap().get("dom_polling").is_none());

        // A genuine field change still emits (admin_status up -> down).
        let events = run(
            &[su(
                "Ethernet48",
                PortOp::Set,
                &[
                    ("index", "12"),
                    ("speed", "40000"),
                    ("admin_status", "down"),
                ],
            )],
            &mut role_map,
            &mut cache,
        );
        assert_eq!(events.len(), 1, "a changed field value must still re-emit");
        assert_eq!(events[0].event_type, PortChangeEventType::Set);

        // A genuine field addition still emits (add `host_tx_ready`).
        let events = run(
            &[su(
                "Ethernet48",
                PortOp::Set,
                &[
                    ("index", "12"),
                    ("speed", "40000"),
                    ("admin_status", "down"),
                    ("host_tx_ready", "true"),
                ],
            )],
            &mut role_map,
            &mut cache,
        );
        assert_eq!(events.len(), 1, "a newly-added field must still re-emit");
    }

    // The link-change re-read must fire *exactly once per genuine flap_count bump*.
    // `PortChangeObserver::prime_initial_snapshot` folds the SubscriberStateTable initial
    // snapshot into `port_event_cache` as a baseline and discards its events; only a real
    // post-subscription flap_count change is then reported. This models that flow through
    // the shared `process_port_update_batch` core (a primed cache + subsequent batches).
    #[test]
    fn test_initial_snapshot_primes_baseline_without_reread() {
        const APPL_DB: &str = "APPL_DB";
        const PORT_TABLE: &str = "PORT_TABLE";
        let filter = ["flap_count".to_string()];

        fn su(name: &str, flap: &str) -> PortUpdate {
            PortUpdate {
                port_name: name.to_string(),
                op: PortOp::Set,
                fields: vec![("flap_count".to_string(), flap.to_string())],
            }
        }
        let mut role_map = HashMap::new();
        let mut cache: HashMap<(String, String, String), BTreeMap<String, String>> = HashMap::new();
        let run =
            |updates: &[PortUpdate],
             role_map: &mut HashMap<String, String>,
             cache: &mut HashMap<(String, String, String), BTreeMap<String, String>>| {
                process_port_update_batch(
                    updates,
                    APPL_DB,
                    PORT_TABLE,
                    0,
                    Some(&filter),
                    role_map,
                    cache,
                )
            };

        // Boot snapshot: PORT_TABLE already carries flap_count=5. `prime_initial_snapshot`
        // runs this batch to seed the cache and DISCARDS the events — no re-read at boot.
        let _baseline = run(&[su("Ethernet48", "5")], &mut role_map, &mut cache);
        assert!(
            cache.contains_key(&(
                "Ethernet48".to_string(),
                APPL_DB.to_string(),
                PORT_TABLE.to_string()
            )),
            "initial snapshot must prime the dedup cache as the baseline"
        );

        // A redelivered snapshot / PORT_SET re-emit carrying the SAME flap_count must NOT
        // schedule a second re-read (this is the off-cadence read the guard test catches).
        let redelivered = run(&[su("Ethernet48", "5")], &mut role_map, &mut cache);
        assert!(
            redelivered.is_empty(),
            "an unchanged flap_count must emit nothing (no off-cadence re-read)"
        );

        // A genuine flap (flap_count bump) is reported exactly once → one re-read.
        let flap = run(&[su("Ethernet48", "6")], &mut role_map, &mut cache);
        assert_eq!(
            flap.len(),
            1,
            "a real flap_count bump must emit exactly one event"
        );
        assert_eq!(flap[0].port_name, "Ethernet48");

        // The next genuine flap emits again (the fast second-flap path the test relies on).
        let flap2 = run(&[su("Ethernet48", "7")], &mut role_map, &mut cache);
        assert_eq!(flap2.len(), 1, "each subsequent flap_count bump emits once");
    }

    // (CMIS re-plug bring-up). Models the STATE_DB TRANSCEIVER_INFO unplug(DEL) +
    // re-plug(SET) that the CMIS `MultiPortChangeObserver` watches. These two tests pin the
    // exact reason `handle_port_update_event` must wake on ANY table (via `poll` over every
    // fd) rather than block one table and drain the rest afterwards.
    //
    // The emulator re-plug re-writes the SAME identity row that was present before the
    // unplug, so whether the re-plug is seen as a change depends entirely on WHEN it is
    // drained relative to the DEL.
    fn ti(op: PortOp, mfr: &str) -> PortUpdate {
        // A representative TRANSCEIVER_INFO row (identity + physical index).
        PortUpdate {
            port_name: "Ethernet0".to_string(),
            op,
            fields: vec![
                ("index".to_string(), "0".to_string()),
                ("manufacturer".to_string(), mfr.to_string()),
            ],
        }
    }

    // GOOD PATH (what the poll fix guarantees): the DEL and the re-plug SET are drained in
    // SEPARATE batches (the observer woke promptly on the DEL). The DEL updates the cache to
    // op=DEL, so the later identical-identity SET differs from the cache and IS emitted —
    // the CMIS manager gets its PORT_SET and re-runs bring-up. This is the reference
    // `swsscommon.Select` behaviour (wake on the DEL notification, then again on the SET).
    #[test]
    fn test_replug_del_then_set_separate_batches_reemit() {
        const STATE_DB: &str = "STATE_DB";
        const TI: &str = "TRANSCEIVER_INFO";
        let mut role_map = HashMap::new();
        let mut cache = HashMap::new();
        let run =
            |u: &[PortUpdate],
             role_map: &mut HashMap<String, String>,
             cache: &mut HashMap<(String, String, String), BTreeMap<String, String>>| {
                process_port_update_batch(u, STATE_DB, TI, 0, None, role_map, cache)
            };

        // Present at boot (primes the cache with the identity row).
        let boot = run(&[ti(PortOp::Set, "xcvr-emu")], &mut role_map, &mut cache);
        assert_eq!(boot.len(), 1);

        // Unplug: TRANSCEIVER_INFO DEL, drained on its own (prompt wake).
        let del = run(&[ti(PortOp::Del, "xcvr-emu")], &mut role_map, &mut cache);
        assert_eq!(del.len(), 1, "unplug must emit a PORT_DEL");
        assert_eq!(del[0].event_type, PortChangeEventType::Del);

        // Re-plug: identical identity row, drained in a SEPARATE batch. Because the cache now
        // holds the DEL, the SET differs and IS re-emitted → bring-up re-runs.
        let set = run(&[ti(PortOp::Set, "xcvr-emu")], &mut role_map, &mut cache);
        assert_eq!(
            set.len(),
            1,
            "re-plug must re-emit a PORT_SET so CMIS re-inits"
        );
        assert_eq!(set[0].event_type, PortChangeEventType::Set);
    }

    // DEFENSE-IN-DEPTH (what the soak op-transition boundary guarantees): even if the
    // unplug(DEL) and re-plug(SET) both land in ONE drained batch (e.g. the observer was
    // busy and drained TRANSCEIVER_INFO once, catching both keyspace notifications), the
    // soak MUST NOT collapse them into a single last-wins SET — doing so would dedup against
    // the identical pre-unplug row and emit nothing, so the CMIS manager would never see the
    // remove+insert and `cmis_state` would stay stuck (the exact
    // test_cmis_state_progression e2e failure). Because the op flips DEL->SET, the soak
    // finalizes the DEL as its own run first, so BOTH a PORT_DEL and a PORT_SET are emitted
    // in order — runtime-equivalent to the reference `swsscommon.Select` delivering each
    // keyspace notification in its own wake.
    #[test]
    fn test_replug_del_and_set_same_batch_not_collapsed() {
        const STATE_DB: &str = "STATE_DB";
        const TI: &str = "TRANSCEIVER_INFO";
        let mut role_map = HashMap::new();
        let mut cache = HashMap::new();
        let run =
            |u: &[PortUpdate],
             role_map: &mut HashMap<String, String>,
             cache: &mut HashMap<(String, String, String), BTreeMap<String, String>>| {
                process_port_update_batch(u, STATE_DB, TI, 0, None, role_map, cache)
            };

        let _boot = run(&[ti(PortOp::Set, "xcvr-emu")], &mut role_map, &mut cache);

        // DEL then SET in ONE batch: the op flips, so the DEL run is finalized before the SET
        // run → both are emitted (remove then insert), driving CMIS force_cmis_reinit.
        let events = run(
            &[ti(PortOp::Del, "xcvr-emu"), ti(PortOp::Set, "xcvr-emu")],
            &mut role_map,
            &mut cache,
        );
        assert_eq!(
            events.len(),
            2,
            "DEL+SET in one batch must emit both the remove and the re-insert, not collapse to a no-op"
        );
        assert_eq!(events[0].event_type, PortChangeEventType::Del);
        assert_eq!(events[1].event_type, PortChangeEventType::Set);
    }

    // A pure duplicate SET run within one batch still soaks last-wins and dedups to nothing
    // (no spurious re-emit), preserving the reference soak for the common no-op case.
    #[test]
    fn test_duplicate_set_run_still_soaks_to_noop() {
        const STATE_DB: &str = "STATE_DB";
        const TI: &str = "TRANSCEIVER_INFO";
        let mut role_map = HashMap::new();
        let mut cache = HashMap::new();
        let run =
            |u: &[PortUpdate],
             role_map: &mut HashMap<String, String>,
             cache: &mut HashMap<(String, String, String), BTreeMap<String, String>>| {
                process_port_update_batch(u, STATE_DB, TI, 0, None, role_map, cache)
            };

        let _boot = run(&[ti(PortOp::Set, "xcvr-emu")], &mut role_map, &mut cache);
        let dup = run(
            &[ti(PortOp::Set, "xcvr-emu"), ti(PortOp::Set, "xcvr-emu")],
            &mut role_map,
            &mut cache,
        );
        assert!(
            dup.is_empty(),
            "a same-op SET run soaks last-wins and dedups against the cache → no event"
        );
    }

    // --- read_port_config_change (CONFIG_DB PORT add/remove translation) --------

    fn pu(name: &str, op: PortOp, fields: &[(&str, &str)]) -> PortUpdate {
        PortUpdate {
            port_name: name.to_string(),
            op,
            fields: fields
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect(),
        }
    }

    #[test]
    fn test_physical_index_from_name_matches_get_port_mapping() {
        // Ethernet{i*4} -> i (lib/emu.py::port_to_index).
        assert_eq!(physical_index_from_name("Ethernet0"), Some(0));
        assert_eq!(physical_index_from_name("Ethernet60"), Some(15));
        assert_eq!(physical_index_from_name("Ethernet100"), Some(25));
        assert_eq!(physical_index_from_name("PortConfigDone"), None);
        assert_eq!(physical_index_from_name("Ethernetxyz"), None);
    }

    // ← tests/test_xcvrd.py::test_handle_port_config_change (SET path): a PORT SET for a
    // name not yet in the mapping (carrying an `index` field) yields a single PORT_ADD with
    // the CONFIG_DB physical index (consistent with get_port_mapping / hal.sfp(i)).
    #[test]
    fn test_read_port_config_change_set_new_port_emits_add() {
        let pm = PortMapping::new();
        let events = read_port_config_change(
            &[pu(
                "Ethernet60",
                PortOp::Set,
                &[("index", "99"), ("admin_status", "up")],
            )],
            &pm,
            0,
        );
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].event_type, PortChangeEventType::Add);
        assert_eq!(events[0].port_name, "Ethernet60");
        // The row's own CONFIG_DB `index` (99), NOT the name-derived 60/4=15. The
        // `Ethernet{i*4}` convention holds only on the emulator; `0001` moved the boot
        // mapping onto CONFIG_DB `index` and this runtime path now agrees with it.
        assert_eq!(events[0].physical_port, Some(99));
    }

    // A SET on a name ALREADY in the mapping at the same physical index is a no-op (no
    // spurious remove/add) — the emulator re-write of an existing PORT row.
    #[test]
    fn test_read_port_config_change_set_existing_same_index_noop() {
        let mut pm = PortMapping::new();
        add(&mut pm, "Ethernet60", 15, 0);
        let events = read_port_config_change(
            &[pu(
                "Ethernet60",
                PortOp::Set,
                &[("index", "15"), ("admin_status", "up")],
            )],
            &pm,
            0,
        );
        assert!(
            events.is_empty(),
            "same-index SET on a known port emits nothing"
        );
    }

    // A SET missing the `index` field (a half-written PORT row) is ignored — mirrors the
    // reference `if 'index' not in fvp: continue`.
    #[test]
    fn test_read_port_config_change_set_without_index_ignored() {
        let pm = PortMapping::new();
        let events = read_port_config_change(
            &[pu("Ethernet60", PortOp::Set, &[("admin_status", "up")])],
            &pm,
            0,
        );
        assert!(
            events.is_empty(),
            "a SET without an index field is not an add"
        );
    }

    // ← test_handle_port_config_change (DEL path): a PORT DEL for a configured logical port
    // yields a PORT_REMOVE carrying its current (mapped) physical index — the trigger for
    // the full TRANSCEIVER_* table teardown.
    #[test]
    fn test_read_port_config_change_del_known_port_emits_remove() {
        let mut pm = PortMapping::new();
        add(&mut pm, "Ethernet60", 15, 0);
        let events = read_port_config_change(&[pu("Ethernet60", PortOp::Del, &[])], &pm, 0);
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].event_type, PortChangeEventType::Remove);
        assert_eq!(events[0].port_name, "Ethernet60");
        assert_eq!(events[0].physical_port, Some(15));
    }

    // A DEL for a port not in the mapping is ignored (nothing to tear down).
    #[test]
    fn test_read_port_config_change_del_unknown_port_ignored() {
        let pm = PortMapping::new();
        let events = read_port_config_change(&[pu("Ethernet60", PortOp::Del, &[])], &pm, 0);
        assert!(
            events.is_empty(),
            "a DEL for an unconfigured port emits nothing"
        );
    }

    // Non-front-panel names (internal/inband/recycle) are dropped by the front-panel filter.
    #[test]
    fn test_read_port_config_change_non_front_panel_ignored() {
        let pm = PortMapping::new();
        let events = read_port_config_change(
            &[pu("Ethernet-BP0", PortOp::Set, &[("index", "0")])],
            &pm,
            0,
        );
        assert!(
            events.is_empty(),
            "an internal/backplane port is not a front-panel add"
        );
    }

    // ← tests/test_xcvrd.py::test_handle_port_config_change: a CONFIG_DB PORT SET for a
    // not-yet-configured front-panel port then a DEL for it mutate the PortMapping through
    // `handle_port_change_event` — first populating all three maps + the logical list, then
    // fully clearing them. This is the seam the DOM task (and the SFP task) reuse to keep the
    // poll mapping current as logical ports are added/removed at runtime. The physical index
    // is name-derived on this testbed (Ethernet0 -> 0), not the raw CONFIG_DB `index` field —
    // see `test_read_port_config_change_set_new_port_emits_add`.
    #[test]
    fn test_handle_port_config_change_mutates_mapping() {
        let mut pm = PortMapping::new();

        // SET on a not-yet-configured port -> a single PORT_ADD applied to the map.
        let set = read_port_config_change(
            &[pu(
                "Ethernet0",
                PortOp::Set,
                &[("index", "1"), ("admin_status", "up")],
            )],
            &pm,
            0,
        );
        for ev in &set {
            pm.handle_port_change_event(ev);
        }
        assert!(pm.logical_port_list().contains(&"Ethernet0".to_string()));
        assert_eq!(pm.get_asic_id_for_logical_port("Ethernet0"), Some(0));
        // index=1 in the row, so the port lands on SFP 1 (not name-derived 0/4=0).
        assert_eq!(
            pm.get_physical_to_logical(1),
            Some(vec!["Ethernet0".to_string()])
        );
        assert_eq!(pm.get_logical_to_physical("Ethernet0"), Some(vec![1]));

        // DEL on the now-configured port -> a PORT_REMOVE that fully clears the map.
        let del = read_port_config_change(&[pu("Ethernet0", PortOp::Del, &[])], &pm, 0);
        for ev in &del {
            pm.handle_port_change_event(ev);
        }
        assert!(pm.logical_port_list().is_empty());
        assert_eq!(pm.get_logical_to_physical("Ethernet0"), None);
        assert_eq!(pm.get_physical_to_logical(0), None);
        assert_eq!(pm.get_asic_id_for_logical_port("Ethernet0"), None);
    }

    // --- MultiPortChangeObserver idle-block slicing (no-spin pacing) ------
    //
    // The CMIS `task_worker` blocks `handle_port_update_event(1000)` once per bring-up
    // step, so intermediate `cmis_state`s persist ~1 s and the e2e poller observes each
    // transition. The multi-table observer honours that timeout by slicing it into one
    // swss-native `read_data(slice)` per watched table (round-robin), instead of a
    // `libc::poll` on the redis fd that never blocks (the fd is readable at the socket
    // level with no new keyspace notification) and would busy-spin the CMIS task — racing
    // the state machine (test_cmis_state_progression) and starving the concurrent DOM
    // flag publish (test_dom_flag_groups_temp_and_vcc). This guards the slice contract:
    // total ≈ timeout, div-by-zero-safe, and floored at 1 ms so every table still blocks.
    #[test]
    fn test_multi_observer_slice_paces_not_spins() {
        // CMIS default: 1000 ms split across the 3 DEFAULT_PORT_TBL_MAP tables ≈ 333 ms
        // each → ~1 s idle block per bring-up step (reference cadence), never 0 (no spin).
        assert_eq!(multi_observer_slice_ms(1000, 3), 333);
        assert!(
            multi_observer_slice_ms(1000, 3) * 3 <= 1000,
            "the summed per-table slices must not overrun the caller's select timeout"
        );
        // Degenerate: no tables must not divide by zero — degrade to the full timeout.
        assert_eq!(multi_observer_slice_ms(1000, 0), 1000);
        // A tiny timeout is floored at 1 ms so each read_data still makes a real (blocking,
        // non-busy) call rather than a zero-timeout poll that would spin the caller.
        assert_eq!(multi_observer_slice_ms(2, 5), 1);
        assert_eq!(multi_observer_slice_ms(0, 3), 1);
        // Single table gets the whole budget.
        assert_eq!(multi_observer_slice_ms(500, 1), 500);
    }

    // --- boot readiness gate (wait_for_port_config_done) ----------------------

    /// Scripted [`PortConfigNotifier`] — the Rust analogue of the Python test's
    /// `mock_selectable.pop` side_effect. Each `poll()` yields the next scripted batch
    /// (an empty batch models a `Select.TIMEOUT`); once exhausted it returns `Timeout`
    /// forever so a gate that never sees the sentinel falls back to its bounded budget.
    struct ScriptedNotifier {
        batches: std::collections::VecDeque<Vec<String>>,
        polls: usize,
    }

    impl ScriptedNotifier {
        fn new(batches: Vec<Vec<&str>>) -> Self {
            Self {
                batches: batches
                    .into_iter()
                    .map(|b| b.into_iter().map(String::from).collect())
                    .collect(),
                polls: 0,
            }
        }
    }

    impl PortConfigNotifier for ScriptedNotifier {
        fn poll(&mut self) -> PortConfigPoll {
            self.polls += 1;
            match self.batches.pop_front() {
                Some(keys) => PortConfigPoll::Keys(keys),
                None => PortConfigPoll::Timeout,
            }
        }
    }

    fn never_stop() -> impl Fn() -> bool {
        || false
    }

    // Only the PortConfigDone/PortInitDone sentinels open the gate.
    #[test]
    fn is_port_config_done_key_matches_only_sentinels() {
        assert!(is_port_config_done_key("PortConfigDone"));
        assert!(is_port_config_done_key("PortInitDone"));
        assert!(!is_port_config_done_key("Ethernet0"));
        assert!(!is_port_config_done_key(""));
    }

    // Mirrors tests/test_xcvrd.py::test_DaemonXcvrd_wait_for_port_config_done: the gate
    // keeps polling until the PortConfigDone sentinel is popped. The Python test scripts
    // pop() to return ('Ethernet0', SET, ...) then ('PortConfigDone', None, None) and
    // asserts Select.select was called twice — i.e. exactly two poll cycles, returning
    // only AFTER the sentinel.
    #[test]
    fn gate_returns_done_only_after_the_sentinel() {
        let mut n = ScriptedNotifier::new(vec![vec!["Ethernet0"], vec!["PortConfigDone"]]);
        let stop = never_stop();
        let (outcome, polls) = wait_for_port_config_done_gate(&mut n, &stop, 16);
        assert_eq!(outcome, PortConfigGate::Done);
        assert_eq!(
            polls, 2,
            "the reference Select.select is polled twice (call_count == 2)"
        );
        assert_eq!(n.polls, 2);
    }

    // PortInitDone is equally an accepted sentinel (xcvrd.py:934 `in [PortConfigDone,
    // PortInitDone]`), and a select TIMEOUT (empty batch) is skipped, not counted as done.
    #[test]
    fn gate_accepts_port_init_done_and_skips_timeouts() {
        let mut n = ScriptedNotifier::new(vec![
            vec![],
            vec!["Ethernet4"],
            vec![],
            vec!["PortInitDone"],
        ]);
        let stop = never_stop();
        let (outcome, polls) = wait_for_port_config_done_gate(&mut n, &stop, 16);
        assert_eq!(outcome, PortConfigGate::Done);
        assert_eq!(polls, 4);
    }

    // On the pre-populated pmon testbed no portsyncd writes the sentinel, so the gate must
    // fall back to its bounded budget and PROCEED (never hang the daemon). The budget caps
    // the number of poll cycles.
    #[test]
    fn gate_times_out_after_the_bounded_budget_when_sentinel_never_arrives() {
        let mut n = ScriptedNotifier::new(vec![vec!["Ethernet0"]]); // then Timeout forever
        let stop = never_stop();
        let (outcome, polls) = wait_for_port_config_done_gate(&mut n, &stop, 3);
        assert_eq!(outcome, PortConfigGate::TimedOut);
        assert_eq!(
            polls, 3,
            "the loop stops at the poll budget, not spinning forever"
        );
    }

    // A SIGTERM during boot (stop flag) ends the wait immediately with Stopped — the
    // reference `while not self.stop_event.is_set()`.
    #[test]
    fn gate_stops_when_stop_requested() {
        let mut n = ScriptedNotifier::new(vec![vec!["Ethernet0"]]);
        let stop = || true; // already shutting down
        let (outcome, polls) = wait_for_port_config_done_gate(&mut n, &stop, 8);
        assert_eq!(outcome, PortConfigGate::Stopped);
        assert_eq!(polls, 0, "stop is checked before the first poll");
    }

    // A pre-populated sentinel in the very first (snapshot) poll returns immediately — the
    // fast no-op required on the pre-populated testbed.
    #[test]
    fn gate_is_a_fast_no_op_when_sentinel_is_already_present() {
        let mut n = ScriptedNotifier::new(vec![vec!["Ethernet0", "PortConfigDone", "Ethernet4"]]);
        let stop = never_stop();
        let (outcome, polls) = wait_for_port_config_done_gate(&mut n, &stop, 4);
        assert_eq!(outcome, PortConfigGate::Done);
        assert_eq!(
            polls, 1,
            "a sentinel in the boot snapshot opens the gate on poll 1"
        );
    }

    /// A CONFIG_DB PORT update must resolve to the row's OWN `index`, not to the
    /// `Ethernet{i*4}` naming heuristic. On a 8-subports-per-cage breakout box the two
    /// disagree wildly: Ethernet268 has index 34, while 268/4 = 67 -- a cage that does not
    /// exist on a 66-cage chassis. A mass port flap re-added every port at the bogus index,
    /// which made 244 of 514 ports read absent and lose their TRANSCEIVER_INFO.
    #[test]
    fn config_db_index_wins_over_the_name_heuristic() {
        let pm = PortMapping::new();
        let upd = PortUpdate {
            port_name: "Ethernet268".to_string(),
            op: PortOp::Set,
            fields: vec![
                ("index".to_string(), "34".to_string()),
                ("speed".to_string(), "100000".to_string()),
                ("lanes".to_string(), "273".to_string()),
            ],
        };
        let events = read_port_config_change(&[upd], &pm, 0);
        assert_eq!(events.len(), 1);
        assert_eq!(
            events[0].physical_port,
            Some(34),
            "must use CONFIG_DB index, not 268/4=67"
        );
    }

    /// With no `index` field the row is not fully formed and is ignored, exactly as before
    /// (the reference gates the add on a present `index`).
    #[test]
    fn update_without_index_is_ignored() {
        let pm = PortMapping::new();
        let upd = PortUpdate {
            port_name: "Ethernet268".to_string(),
            op: PortOp::Set,
            fields: vec![("admin_status".to_string(), "up".to_string())],
        };
        assert!(read_port_config_change(&[upd], &pm, 0).is_empty());
    }
}
