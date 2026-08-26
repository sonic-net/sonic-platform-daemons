//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! STATE_DB tables written by thermalctld.
//!
//! The tables sit behind [`TableLike`] rather than being `swss_common::Table`
//! directly, so the write path can be tested without a redis.  The Python
//! daemon does the same thing by shadowing the whole `swsscommon` package with
//! a dictionary-backed stand-in (`tests/mock_swsscommon.py`); this is that,
//! expressed as a trait.

use swss_common::{DbConnector, Table};

pub const TEMPERATURE_INFO: &str = "TEMPERATURE_INFO";
pub const FAN_INFO: &str = "FAN_INFO";
pub const FAN_DRAWER_INFO: &str = "FAN_DRAWER_INFO";
pub const PHYSICAL_ENTITY_INFO: &str = "PHYSICAL_ENTITY_INFO";

/// Key under which chassis-level devices are parented.
pub const CHASSIS_INFO_KEY: &str = "chassis 1";

const STATE_DB: &str = "STATE_DB";
const CHASSIS_STATE_DB: &str = "CHASSIS_STATE_DB";
const LIQUID_COOLING_INFO: &str = "LIQUID_COOLING_INFO";
const SYSTEM_LEAK_STATUS: &str = "SYSTEM_LEAK_STATUS";
const LEAK_PROFILE: &str = "LEAK_PROFILE";
const CONNECT_TIMEOUT_MS: u32 = 0;

/// The slice of a STATE_DB table this daemon uses.
pub trait TableLike: Send {
    fn set(&self, key: &str, fvs: &[(&str, String)]) -> Result<(), String>;
    fn del(&self, key: &str) -> Result<(), String>;
    fn get(&self, key: &str) -> Option<Vec<(String, String)>>;
    fn get_keys(&self) -> Vec<String>;
}

impl TableLike for Table {
    fn set(&self, key: &str, fvs: &[(&str, String)]) -> Result<(), String> {
        Table::set(self, key, fvs.to_vec()).map_err(|e| format!("{e:?}"))
    }

    fn del(&self, key: &str) -> Result<(), String> {
        Table::del(self, key).map_err(|e| format!("{e:?}"))
    }

    fn get(&self, key: &str) -> Option<Vec<(String, String)>> {
        let fvs = Table::get(self, key).ok()??;
        Some(
            fvs.into_iter()
                .filter_map(|(k, v)| v.to_str().ok().map(|s| (k, s.to_string())))
                .collect(),
        )
    }

    fn get_keys(&self) -> Vec<String> {
        Table::get_keys(self).unwrap_or_default()
    }
}

pub struct StateDb {
    pub temperature: Box<dyn TableLike>,
    /// TEMPERATURE_INFO_{slot} on CHASSIS_STATE_DB, on a modular chassis or a
    /// SmartSwitch DPU.  Note the database: Python opens CHASSIS_STATE_DB for
    /// this one, not STATE_DB.
    pub chassis_temperature: Option<Box<dyn TableLike>>,
    pub fan: Box<dyn TableLike>,
    pub fan_drawer: Box<dyn TableLike>,
    pub physical_entity: Box<dyn TableLike>,
}

/// The three tables the leak thread writes.
///
/// Deliberately not part of [`StateDb`]: the leak thread is the only writer and
/// it runs on its own thread, so it opens these and nothing else.  Folding them
/// in would hand that thread four table connections it never reads, on exactly
/// the liquid-cooled platforms where it is the fastest loop in the daemon.
pub struct LeakTables {
    pub sensor: Box<dyn TableLike>,
    pub system: Box<dyn TableLike>,
    pub profile: Box<dyn TableLike>,
}

/// How a table is opened: the database it lives in, and its name.
///
/// The daemon supplies redis; a test supplies tables it can read back, and one
/// that refuses to open.  What is worth driving here is not the connection but
/// the shape around it — which tables each half opens, which database each one
/// lives in, and which failures are fatal.
pub type OpenNamed = Box<dyn FnMut(&str, &str) -> Result<Box<dyn TableLike>, String>>;

/// The real opener.
fn open_named(db_name: &str, table: &str) -> Result<Box<dyn TableLike>, String> {
    DbConnector::new_named(db_name, false, CONNECT_TIMEOUT_MS)
        .and_then(|c| Table::new(c, table))
        .map(|t| Box::new(t) as Box<dyn TableLike>)
        .map_err(|e| format!("{e:?}"))
}

impl LeakTables {
    pub fn open() -> Result<Self, String> {
        Self::open_with(Box::new(open_named))
    }

    pub fn open_with(mut open: OpenNamed) -> Result<Self, String> {
        Ok(Self {
            sensor:  open(STATE_DB, LIQUID_COOLING_INFO)?,
            system:  open(STATE_DB, SYSTEM_LEAK_STATUS)?,
            profile: open(STATE_DB, LEAK_PROFILE)?,
        })
    }
}

impl StateDb {
    pub fn open(slot_or_dpu_id: Option<u32>) -> Result<Self, String> {
        Self::open_with(slot_or_dpu_id, Box::new(open_named))
    }

    /// The same set of tables with the opening step supplied.
    ///
    /// `Table::new` takes the connector by value and `DbConnector` is not
    /// `Clone`, so each table opens its own redis connection. Python shares
    /// one; the extra sockets are the price, and 12 records why.
    pub fn open_with(slot_or_dpu_id: Option<u32>, mut open: OpenNamed) -> Result<Self, String> {
        Ok(Self {
            temperature: open(STATE_DB, TEMPERATURE_INFO)?,
            // A modular chassis need not have CHASSIS_STATE_DB at all, so a
            // failure here is not fatal — Python catches and ignores it too,
            // and the daemon carries on writing the unsuffixed table.
            chassis_temperature: slot_or_dpu_id.and_then(|slot| {
                let name = format!("{TEMPERATURE_INFO}_{slot}");
                match open(CHASSIS_STATE_DB, &name) {
                    Ok(t) => Some(t),
                    Err(e) => {
                        log::warn!("no {CHASSIS_STATE_DB} {name}: {e}");
                        None
                    }
                }
            }),
            fan: open(STATE_DB, FAN_INFO)?,
            fan_drawer: open(STATE_DB, FAN_DRAWER_INFO)?,
            physical_entity: open(STATE_DB, PHYSICAL_ENTITY_INFO)?,
        })
    }

    /// PHYSICAL_ENTITY_INFO carries the parent/position of every device, and is
    /// refreshed alongside the device's own table. Mirrors update_entity_info().
    pub fn set_entity_info(&self, key: &str, parent_name: &str, position_in_parent: &str) {
        let fvs = [
            ("position_in_parent", position_in_parent.to_string()),
            ("parent_name", parent_name.to_string()),
        ];
        if let Err(e) = self.physical_entity.set(key, &fvs) {
            log::warn!("failed to update {PHYSICAL_ENTITY_INFO} for {key}: {e}");
        }
    }
}

// ── A table that is a HashMap ─────────────────────────────────────────────────

#[cfg(test)]
pub mod mock {
    use super::*;
    use std::collections::BTreeMap;
    use std::sync::{Arc, Mutex};

    /// A dictionary-backed table, the equivalent of Python's
    /// `tests/mock_swsscommon.Table`.
    ///
    /// Cloning shares the contents, so a test can keep a handle on what the
    /// daemon wrote after handing the table to a `StateDb`.
    /// The shared row store behind a `MockTable`.
    type Rows = Arc<Mutex<BTreeMap<String, Vec<(String, String)>>>>;

    #[derive(Clone, Default)]
    pub struct MockTable {
        rows: Rows,
        /// When set, every `set` fails with this message.
        fail: Arc<Mutex<Option<String>>>,
        /// How many times the table has been enumerated, so a test can tell
        /// whether a read path ran at all.
        scans: Arc<Mutex<usize>>,
    }

    impl MockTable {
        pub fn new() -> Self {
            Self::default()
        }

        /// Make every subsequent write fail, to exercise the error paths.
        pub fn fail_writes(&self, why: &str) {
            *self.fail.lock().unwrap() = Some(why.to_string());
        }

        /// Let writes through again, for the recovery half of a failure test.
        pub fn allow_writes(&self) {
            *self.fail.lock().unwrap() = None;
        }

        pub fn keys(&self) -> Vec<String> {
            self.rows.lock().unwrap().keys().cloned().collect()
        }

        /// How many times `get_keys` has been called on this table.
        pub fn scans(&self) -> usize {
            *self.scans.lock().unwrap()
        }

        pub fn row(&self, key: &str) -> Option<Vec<(String, String)>> {
            self.rows.lock().unwrap().get(key).cloned()
        }

        /// The value of one field, for terse assertions.
        pub fn field(&self, key: &str, field: &str) -> Option<String> {
            self.row(key)?
                .into_iter()
                .find(|(k, _)| k == field)
                .map(|(_, v)| v)
        }

        pub fn len(&self) -> usize {
            self.rows.lock().unwrap().len()
        }

        pub fn is_empty(&self) -> bool {
            self.len() == 0
        }
    }

    impl TableLike for MockTable {
        /// Merges the given fields into the row, as redis HSET does — it does
        /// *not* replace it.  Getting this wrong would make a partial write,
        /// such as the led_status-only refresh, look like it erased the row.
        fn set(&self, key: &str, fvs: &[(&str, String)]) -> Result<(), String> {
            if let Some(why) = self.fail.lock().unwrap().as_ref() {
                return Err(why.clone());
            }
            let mut rows = self.rows.lock().unwrap();
            let row = rows.entry(key.to_string()).or_default();
            for (k, v) in fvs {
                match row.iter_mut().find(|(existing, _)| existing == k) {
                    Some((_, slot)) => *slot = v.clone(),
                    None => row.push((k.to_string(), v.clone())),
                }
            }
            Ok(())
        }

        fn del(&self, key: &str) -> Result<(), String> {
            self.rows.lock().unwrap().remove(key);
            Ok(())
        }

        fn get(&self, key: &str) -> Option<Vec<(String, String)>> {
            self.row(key)
        }

        fn get_keys(&self) -> Vec<String> {
            *self.scans.lock().unwrap() += 1;
            self.keys()
        }
    }

    /// A `StateDb` made entirely of `MockTable`s, plus handles on each.
    pub struct MockDb {
        pub db: StateDb,
        pub temperature: MockTable,
        pub chassis_temperature: MockTable,
        pub fan: MockTable,
        pub fan_drawer: MockTable,
        pub physical_entity: MockTable,
    }

    impl MockDb {
        pub fn new(with_chassis: bool) -> Self {
            let temperature = MockTable::new();
            let chassis_temperature = MockTable::new();
            let fan = MockTable::new();
            let fan_drawer = MockTable::new();
            let physical_entity = MockTable::new();
            let db = StateDb {
                temperature: Box::new(temperature.clone()),
                chassis_temperature: with_chassis
                    .then(|| Box::new(chassis_temperature.clone()) as Box<dyn TableLike>),
                fan: Box::new(fan.clone()),
                fan_drawer: Box::new(fan_drawer.clone()),
                physical_entity: Box::new(physical_entity.clone()),
            };
            Self {
                db,
                temperature,
                chassis_temperature,
                fan,
                fan_drawer,
                physical_entity,
            }
        }
    }

    /// The leak tables on their own, as the leak thread opens them.
    pub struct MockLeak {
        pub tables: LeakTables,
        pub sensor: MockTable,
        pub system: MockTable,
        pub profile: MockTable,
    }

    impl Default for MockLeak {
        fn default() -> Self {
            Self::new()
        }
    }

    impl MockLeak {
        pub fn new() -> Self {
            let sensor = MockTable::new();
            let system = MockTable::new();
            let profile = MockTable::new();
            Self {
                tables: LeakTables {
                    sensor:  Box::new(sensor.clone()),
                    system:  Box::new(system.clone()),
                    profile: Box::new(profile.clone()),
                },
                sensor,
                system,
                profile,
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::mock::*;
    use super::*;

    #[test]
    fn entity_info_carries_the_parent_and_position() {
        let m = MockDb::new(false);
        m.db.set_entity_info("fan1", "drawer1", "1");
        assert_eq!(m.physical_entity.field("fan1", "parent_name").as_deref(), Some("drawer1"));
        assert_eq!(m.physical_entity.field("fan1", "position_in_parent").as_deref(), Some("1"));
    }

    /// Python writes position first and parent second; the order is part of
    /// requirement 1a like every other row.
    #[test]
    fn entity_info_field_order_matches_python() {
        let m = MockDb::new(false);
        m.db.set_entity_info("fan1", "drawer1", "1");
        let keys: Vec<String> =
            m.physical_entity.row("fan1").unwrap().into_iter().map(|(k, _)| k).collect();
        assert_eq!(keys, ["position_in_parent", "parent_name"]);
    }

    #[test]
    fn a_failing_write_is_logged_and_not_fatal() {
        let m = MockDb::new(false);
        m.physical_entity.fail_writes("redis is down");
        m.db.set_entity_info("fan1", "drawer1", "1");
        assert!(m.physical_entity.is_empty());
    }

    #[test]
    fn the_chassis_table_is_absent_unless_asked_for() {
        assert!(MockDb::new(false).db.chassis_temperature.is_none());
        assert!(MockDb::new(true).db.chassis_temperature.is_some());
    }

    // ── Which tables are opened, and where ────────────────────────────────

    use std::sync::{Arc, Mutex};

    /// What was asked for, in order: (database, table).
    type Asked = Arc<Mutex<Vec<(String, String)>>>;

    /// Records every (database, table) asked for, and can be told to refuse one.
    fn recording(refuse: Option<&str>) -> (OpenNamed, Asked) {
        let log = Arc::new(Mutex::new(Vec::new()));
        let l = log.clone();
        let refuse = refuse.map(str::to_string);
        let open: OpenNamed = Box::new(move |db: &str, table: &str| {
            l.lock().unwrap().push((db.to_string(), table.to_string()));
            if refuse.as_deref() == Some(table) {
                return Err("no such database".to_string());
            }
            Ok(Box::new(MockTable::new()) as Box<dyn TableLike>)
        });
        (open, log)
    }

    /// A plain device opens four tables, all on STATE_DB, and no chassis table.
    #[test]
    fn a_plain_device_opens_four_state_db_tables() {
        let (open, log) = recording(None);
        let db = StateDb::open_with(None, open).unwrap();
        assert!(db.chassis_temperature.is_none());

        let opened = log.lock().unwrap().clone();
        assert_eq!(
            opened,
            vec![
                ("STATE_DB".into(), "TEMPERATURE_INFO".into()),
                ("STATE_DB".into(), "FAN_INFO".into()),
                ("STATE_DB".into(), "FAN_DRAWER_INFO".into()),
                ("STATE_DB".into(), "PHYSICAL_ENTITY_INFO".into()),
            ]
        );
    }

    /// The slot-suffixed table lives on **CHASSIS_STATE_DB**, not STATE_DB, and
    /// carries the slot in its name.  Opening it on the wrong database writes a
    /// table no chassis consumer reads.
    #[test]
    fn the_slot_table_is_named_for_its_slot_and_lives_on_the_chassis_database() {
        let (open, log) = recording(None);
        let db = StateDb::open_with(Some(3), open).unwrap();
        assert!(db.chassis_temperature.is_some());

        let opened = log.lock().unwrap().clone();
        assert!(
            opened.contains(&("CHASSIS_STATE_DB".into(), "TEMPERATURE_INFO_3".into())),
            "{opened:?}"
        );
    }

    /// A modular chassis need not have CHASSIS_STATE_DB at all, so failing to
    /// open the slot table is not fatal: the daemon carries on writing the
    /// unsuffixed one.  Python catches and ignores it for the same reason.
    #[test]
    fn a_missing_chassis_database_is_not_fatal() {
        let (open, _) = recording(Some("TEMPERATURE_INFO_3"));
        let db = StateDb::open_with(Some(3), open).expect("the rest still opens");
        assert!(db.chassis_temperature.is_none());
    }

    /// A STATE_DB table that cannot be opened *is* fatal — there is nowhere to
    /// publish, and carrying on would leave the daemon running blind.
    #[test]
    fn a_missing_state_db_table_stops_the_daemon() {
        let (open, _) = recording(Some("FAN_INFO"));
        assert!(StateDb::open_with(None, open).is_err());
    }

    /// The leak thread opens its own three tables and nothing else — it runs on
    /// its own thread at the fastest cadence in the daemon, and four table
    /// handles it never reads would be four connections wasted on exactly the
    /// liquid-cooled platforms where that matters.
    #[test]
    fn the_leak_thread_opens_only_its_own_three_tables() {
        let (open, log) = recording(None);
        LeakTables::open_with(open).unwrap();
        let opened = log.lock().unwrap().clone();
        assert_eq!(
            opened,
            vec![
                ("STATE_DB".into(), "LIQUID_COOLING_INFO".into()),
                ("STATE_DB".into(), "SYSTEM_LEAK_STATUS".into()),
                ("STATE_DB".into(), "LEAK_PROFILE".into()),
            ]
        );
    }
}
