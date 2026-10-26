//! STATE_DB seam (analysis 3.6) - the mockable boundary in front of the
//! `swss-common` Redis bindings.
//!
//! [`DbTable`] is a direct port of the `mock_swsscommon.Table` surface the Python
//! unit tests build (`set/get/hget/getKeys/_del/hdel/get_size/get_size_for_key`),
//! so Rust tests can assert **field counts** exactly as they do
//! (`dom_tbl.get_size_for_key("Ethernet0") == 27`). Production uses
//! [`RealDbTable`] (wraps a STATE_DB `DbConnector`); tests use
//! `crate::mock::MockDbTable`.
//!
//! Trait-semantics note (intentional, documented): the real swss `Table::set`
//! **merges** fields (an additive `HSET`, so a writer that only touches
//! `status`/`error` never clobbers the `cmis_state` another writer set), while the
//! Python `mock_swsscommon.Table.set` **replaces** the row (`mock_dict[key]=fvs`).
//! [`RealDbTable::set`] therefore merges and `MockDbTable::set` replaces, matching
//! each reference exactly. [`DbTable::hset`] merges a single field in both.

use std::collections::BTreeSet;
use std::sync::{Arc, Mutex, MutexGuard};

use swss_common::{CxxString, DbConnector};

use crate::error::Result;

/// A STATE_DB hash-of-hashes table keyed `TABLE|<logical_port>`. Interior
/// mutability (`&self` writers) matches the Python `Table` object.
pub trait DbTable: Send + Sync {
    /// `Table.set(key, FieldValuePairs)` - write a row (real: merge; mock: replace).
    fn set(&self, key: &str, fvs: &[(String, String)]);
    /// Merge a single field into a row (real `HSET`; used for the `cmis_state`
    /// projection so it never clobbers `status`/`error`).
    fn hset(&self, key: &str, field: &str, value: &str);
    /// `Table.get(key)` -> the row's field/value pairs, if present.
    fn get(&self, key: &str) -> Option<Vec<(String, String)>>;
    /// `Table.hget(key, field)` -> one field's value, if present.
    fn hget(&self, key: &str, field: &str) -> Option<String>;
    /// `Table._del(key)` - delete a whole row.
    fn del(&self, key: &str);
    /// `Table.hdel(key, field)` - delete one field (removing the row if empty).
    fn hdel(&self, key: &str, field: &str);
    /// `Table.getKeys()`.
    fn get_keys(&self) -> Vec<String>;
    /// `Table.get_size()` - number of rows.
    fn get_size(&self) -> usize;
    /// `Table.get_size_for_key(key)` - number of fields in a row.
    fn get_size_for_key(&self, key: &str) -> usize;
    /// [`Self::get_size_for_key`], but distinguishing a definitively absent/empty row
    /// (`Some(0)`) from an *indeterminate* read where the backing STATE_DB access
    /// transiently failed (`None`). The default mirrors [`Self::get_size_for_key`] (never
    /// `None`); [`RealDbTable`] overrides it so a transient `hgetall` error is NOT silently
    /// reported as "row missing". A caller that must not act on a failed read as though the
    /// row were deleted (e.g. the DOM republish hook, which would otherwise re-read a present
    /// port's latched flags off-cadence) uses this instead of the lossy `usize` form.
    fn get_size_for_key_checked(&self, key: &str) -> Option<usize> {
        Some(self.get_size_for_key(key))
    }
    /// Downcast hook so unit tests can reach the concrete mock behind a `&dyn DbTable`
    /// (e.g. to inject a transient read failure). Production never downcasts.
    fn as_any(&self) -> &dyn std::any::Any;
}

/// Real table: a named view over a shared STATE_DB [`DbConnector`].
///
/// The daemon is the sole writer of `TRANSCEIVER_*`, so rows are addressed by the
/// explicit composite key `"<table>|<logical_port>"` (exactly what the bootstrap
/// and the e2e harness read, e.g. `TRANSCEIVER_INFO|Ethernet0`) - no dependency on
/// `Table`'s separator handling. A local `keys` index tracks what we've written so
/// `get_keys`/`get_size` stay meaningful without a Redis `KEYS`/`SCAN`.
///
/// `DbConnector` is `Send` but not `Sync`, so it is held behind an `Arc<Mutex<_>>`
/// (which is `Send + Sync`) to satisfy `DbTable: Send + Sync`; the two transceiver
/// tables share one connection.
pub struct RealDbTable {
    conn: Arc<Mutex<DbConnector>>,
    table_name: String,
    /// Redis key separator between the table name and the row key. STATE_DB /
    /// CONFIG_DB use `"|"` (the default); APPL_DB tables (e.g. `PORT_TABLE`) use
    /// `":"`. See [`RealDbTable::new_with_sep`].
    sep: String,
    keys: Mutex<BTreeSet<String>>,
}

impl RealDbTable {
    pub fn new(conn: Arc<Mutex<DbConnector>>, table_name: impl Into<String>) -> Self {
        Self::new_with_sep(conn, table_name, "|")
    }

    /// Build a table view whose Redis key is `"<table><sep><key>"`. Use `":"` for
    /// APPL_DB tables such as `PORT_TABLE` (`PORT_TABLE:Ethernet0`); STATE_DB and
    /// CONFIG_DB use the `"|"` default via [`RealDbTable::new`].
    pub fn new_with_sep(
        conn: Arc<Mutex<DbConnector>>,
        table_name: impl Into<String>,
        sep: impl Into<String>,
    ) -> Self {
        RealDbTable {
            conn,
            table_name: table_name.into(),
            sep: sep.into(),
            keys: Mutex::new(BTreeSet::new()),
        }
    }

    fn redis_key(&self, key: &str) -> String {
        format!("{}{}{}", self.table_name, self.sep, key)
    }

    fn track(&self, key: &str) {
        self.locked_keys().insert(key.to_string());
    }

    fn untrack(&self, key: &str) {
        self.locked_keys().remove(key);
    }

    /// Lock the shared STATE_DB connection, recovering from a poisoned mutex.
    ///
    /// The `DbConnector` is shared (`Arc<Mutex<_>>`) across the DOM/CMIS/SFP threads.
    /// If one thread panics while holding the lock (e.g. a PyO3 bridge error, or an
    /// `eprintln!` hitting a broken supervisor stderr pipe mid-write), a plain
    /// `.lock().unwrap()` on the other threads would panic too, cascading a single
    /// transient fault into a full `serve` teardown + chassis rebuild — which resets
    /// the emulator change-event baseline and drops in-flight SFP-error transitions.
    /// Python's GIL means one thread's exception never corrupts a shared object, so we
    /// mirror that: recover the guard and carry on. The connection is only read/written
    /// under this lock, so no partially-mutated invariant is exposed.
    fn locked_conn(&self) -> MutexGuard<'_, DbConnector> {
        self.conn.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Lock the per-table key set, recovering from a poisoned mutex (see
    /// [`Self::locked_conn`]).
    fn locked_keys(&self) -> MutexGuard<'_, BTreeSet<String>> {
        self.keys.lock().unwrap_or_else(|e| e.into_inner())
    }
}

impl DbTable for RealDbTable {
    fn set(&self, key: &str, fvs: &[(String, String)]) {
        let rkey = self.redis_key(key);
        let conn = self.locked_conn();
        for (field, value) in fvs {
            if let Err(e) = conn.hset(&rkey, field, &CxxString::new(value)) {
                eprintln!("xcvrd-rs: hset {rkey}.{field} failed: {e}");
            }
        }
        drop(conn);
        self.track(key);
    }

    fn hset(&self, key: &str, field: &str, value: &str) {
        let rkey = self.redis_key(key);
        {
            let conn = self.locked_conn();
            if let Err(e) = conn.hset(&rkey, field, &CxxString::new(value)) {
                eprintln!("xcvrd-rs: hset {rkey}.{field} failed: {e}");
            }
        }
        self.track(key);
    }

    fn get(&self, key: &str) -> Option<Vec<(String, String)>> {
        let rkey = self.redis_key(key);
        let conn = self.locked_conn();
        match conn.hgetall(&rkey) {
            Ok(map) if !map.is_empty() => Some(
                map.into_iter()
                    .map(|(k, v)| (k, v.to_string_lossy().into_owned()))
                    .collect(),
            ),
            Ok(_) => None,
            Err(e) => {
                eprintln!("xcvrd-rs: hgetall {rkey} failed: {e}");
                None
            }
        }
    }

    fn hget(&self, key: &str, field: &str) -> Option<String> {
        let rkey = self.redis_key(key);
        let conn = self.locked_conn();
        match conn.hget(&rkey, field) {
            Ok(Some(v)) => Some(v.to_string_lossy().into_owned()),
            Ok(None) => None,
            Err(e) => {
                eprintln!("xcvrd-rs: hget {rkey}.{field} failed: {e}");
                None
            }
        }
    }

    fn del(&self, key: &str) {
        let rkey = self.redis_key(key);
        {
            let conn = self.locked_conn();
            if let Err(e) = conn.del(&rkey) {
                eprintln!("xcvrd-rs: del {rkey} failed: {e}");
            }
        }
        self.untrack(key);
    }

    fn hdel(&self, key: &str, field: &str) {
        let rkey = self.redis_key(key);
        let conn = self.locked_conn();
        if let Err(e) = conn.hdel(&rkey, field) {
            eprintln!("xcvrd-rs: hdel {rkey}.{field} failed: {e}");
        }
        let empty = matches!(conn.hgetall(&rkey), Ok(map) if map.is_empty());
        drop(conn);
        if empty {
            self.untrack(key);
        }
    }

    fn get_keys(&self) -> Vec<String> {
        // WARNING: this returns only the keys THIS daemon has written (the local
        // write-tracking set maintained by `track`/`untrack`), NOT the rows present in
        // Redis. It is meaningful only for tables the daemon owns and populates (the
        // `TRANSCEIVER_*` STATE_DB tables). Calling it on a table the daemon only ever
        // READS — CONFIG_DB `PORT`, STATE_DB `PORT_TABLE` — yields an empty list, which
        // silently looks like "no rows" rather than failing. Enumerate such tables from an
        // authoritative source instead (e.g. `PortMapping` for the CONFIG_DB port list).
        // Note `MockDbTable::get_keys` returns whatever a test `set()`, so unit tests do
        // NOT reproduce this behaviour — see `get_sibling_port_configs`, which was broken
        // in production for exactly this reason while its unit test passed.
        self.locked_keys().iter().cloned().collect()
    }

    fn get_size(&self) -> usize {
        self.locked_keys().len()
    }

    fn get_size_for_key(&self, key: &str) -> usize {
        self.get_size_for_key_checked(key).unwrap_or(0)
    }

    fn get_size_for_key_checked(&self, key: &str) -> Option<usize> {
        let rkey = self.redis_key(key);
        let conn = self.locked_conn();
        // A transient `hgetall` failure returns `None` (indeterminate) rather than being
        // collapsed to 0, so callers can tell "row absent" apart from "read failed".
        conn.hgetall(&rkey).ok().map(|m| m.len())
    }

    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

/// Open a real STATE_DB-backed table over a shared connection.
pub fn open_state_table(conn: Arc<Mutex<DbConnector>>, name: &str) -> Result<RealDbTable> {
    Ok(RealDbTable::new(conn, name))
}
