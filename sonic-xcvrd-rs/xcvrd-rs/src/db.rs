//! STATE_DB seam (analysis 3.6) - the mockable boundary in front of the
//! `swss-common` Redis bindings.
//!
//! [`DbTable`] is a direct port of the `mock_swsscommon.Table` surface the Python
//! unit tests build (`set/get/hget/getKeys/_del/hdel/get_size/get_size_for_key`),
//! so Rust tests can assert **field counts** exactly as they do
//! (`dom_tbl.get_size_for_key("Ethernet0") == 27`). Production uses
//! [`RealDbTable`] (wraps native SWSS tables); tests use
//! `crate::mock::MockDbTable`.
//!
//! Trait-semantics note (intentional, documented): the real swss `Table::set`
//! **merges** fields (an additive `HSET`, so a writer that only touches
//! `status`/`error` never clobbers the `cmis_state` another writer set), while the
//! Python `mock_swsscommon.Table.set` **replaces** the row (`mock_dict[key]=fvs`).
//! [`RealDbTable::set`] therefore merges and `MockDbTable::set` replaces, matching
//! each reference exactly. [`DbTable::hset`] merges a single field in both.

use std::sync::{Arc, Mutex, MutexGuard};

use swss_common::{DbConnector, ProducerStateTable, Table};

use crate::error::Result;

/// A STATE_DB hash-of-hashes table keyed `TABLE|<logical_port>`. Interior
/// mutability (`&self` writers) matches the Python `Table` object.
pub trait DbTable: Send + Sync {
    /// `Table.set(key, FieldValuePairs)` - write a row (real: merge; mock: replace).
    fn set(&self, key: &str, fvs: &[(String, String)]);
    /// Write a row, reporting whether it was submitted successfully.
    fn set_checked(&self, key: &str, fvs: &[(String, String)]) -> bool {
        self.set(key, fvs);
        true
    }
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

/// Cached native SWSS table handles. `Table::set` merges a complete row atomically;
/// producer-backed tables also enqueue and publish updates for SWSS consumers.
/// Native tables resolve separators from SONiC DB configuration and enumerate Redis
/// keys, including rows written by other daemons.
pub struct RealDbTable {
    table: Mutex<Table>,
    producer: Option<Mutex<ProducerStateTable>>,
    table_name: String,
}

impl RealDbTable {
    pub fn new(conn: Arc<Mutex<DbConnector>>, table_name: impl Into<String>) -> Result<Self> {
        let table_name = table_name.into();
        let db = conn
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone_timeout(0)?;
        Ok(RealDbTable {
            table: Mutex::new(Table::new(db, &table_name)?),
            producer: None,
            table_name,
        })
    }

    /// Read committed table data normally, but send writes through `ProducerStateTable`.
    pub fn new_producer(
        conn: Arc<Mutex<DbConnector>>,
        table_name: impl Into<String>,
    ) -> Result<Self> {
        let mut table = Self::new(conn.clone(), table_name)?;
        let db = conn
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone_timeout(0)?;
        table.producer = Some(Mutex::new(ProducerStateTable::new(db, &table.table_name)?));
        Ok(table)
    }

    fn locked_table(&self) -> MutexGuard<'_, Table> {
        self.table.lock().unwrap_or_else(|e| e.into_inner())
    }
}

impl DbTable for RealDbTable {
    fn set(&self, key: &str, fvs: &[(String, String)]) {
        self.set_checked(key, fvs);
    }

    fn set_checked(&self, key: &str, fvs: &[(String, String)]) -> bool {
        let fields = fvs.iter().map(|(f, v)| (f.as_str(), v.as_str()));
        let result = if let Some(producer) = &self.producer {
            producer
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .set(key, fields)
        } else {
            self.locked_table().set(key, fields)
        };
        if let Err(e) = result {
            eprintln!("xcvrd-rs: set {}[{key}] failed: {e}", self.table_name);
            return false;
        }
        true
    }

    fn hset(&self, key: &str, field: &str, value: &str) {
        self.set(key, &[(field.to_string(), value.to_string())]);
    }

    fn get(&self, key: &str) -> Option<Vec<(String, String)>> {
        match self.locked_table().get(key) {
            Ok(Some(map)) => Some(
                map.into_iter()
                    .map(|(k, v)| (k, v.to_string_lossy().into_owned()))
                    .collect(),
            ),
            Ok(None) => None,
            Err(e) => {
                eprintln!("xcvrd-rs: get {}[{key}] failed: {e}", self.table_name);
                None
            }
        }
    }

    fn hget(&self, key: &str, field: &str) -> Option<String> {
        match self.locked_table().hget(key, field) {
            Ok(Some(v)) => Some(v.to_string_lossy().into_owned()),
            Ok(None) => None,
            Err(e) => {
                eprintln!(
                    "xcvrd-rs: hget {}[{key}].{field} failed: {e}",
                    self.table_name
                );
                None
            }
        }
    }

    fn del(&self, key: &str) {
        let result = if let Some(producer) = &self.producer {
            producer.lock().unwrap_or_else(|e| e.into_inner()).del(key)
        } else {
            self.locked_table().del(key)
        };
        if let Err(e) = result {
            eprintln!("xcvrd-rs: del {}[{key}] failed: {e}", self.table_name);
        }
    }

    fn hdel(&self, key: &str, field: &str) {
        if let Err(e) = self.locked_table().hdel(key, field) {
            eprintln!(
                "xcvrd-rs: hdel {}[{key}].{field} failed: {e}",
                self.table_name
            );
        }
    }

    fn get_keys(&self) -> Vec<String> {
        match self.locked_table().get_keys() {
            Ok(keys) => keys,
            Err(e) => {
                eprintln!("xcvrd-rs: get keys for {} failed: {e}", self.table_name);
                Vec::new()
            }
        }
    }

    fn get_size(&self) -> usize {
        self.get_keys().len()
    }

    fn get_size_for_key(&self, key: &str) -> usize {
        self.get_size_for_key_checked(key).unwrap_or(0)
    }

    fn get_size_for_key_checked(&self, key: &str) -> Option<usize> {
        self.locked_table()
            .get(key)
            .ok()
            .map(|row| row.map_or(0, |m| m.len()))
    }

    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

/// Open a real STATE_DB-backed table over a shared connection.
pub fn open_state_table(conn: Arc<Mutex<DbConnector>>, name: &str) -> Result<RealDbTable> {
    RealDbTable::new(conn, name)
}
