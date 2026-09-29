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

pub use pmon_common::db::{open as open_named, TableLike};

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

impl LeakTables {
    pub fn open() -> Result<Self, String> {
        Self::open_with(Box::new(open_named))
    }

    pub fn open_with(mut open: OpenNamed) -> Result<Self, String> {
        Ok(Self {
            sensor: open(STATE_DB, LIQUID_COOLING_INFO)?,
            system: open(STATE_DB, SYSTEM_LEAK_STATUS)?,
            profile: open(STATE_DB, LEAK_PROFILE)?,
        })
    }

    /// Delete every row the leak thread owns.
    ///
    /// Python does it from `thermalctld:LiquidCoolingUpdater.__del__`, which
    /// runs when the interpreter finalises on `sys.exit()`.  The Rust updater
    /// is Rust, so nothing runs on its behalf -- the same gap as
    /// `StateDb::clear`, and it was missed because the leak tables are
    /// deliberately not part of `StateDb`.
    ///
    /// Leaving them is the dangerous direction, not the tidy one.  A
    /// `LIQUID_COOLING_INFO` row still saying `leaking=No` after the daemon
    /// has stopped reads, to `leakageshow`, to `show platform leak status` and
    /// to system-health's `HardwareChecker`, as leak detection running and
    /// reporting dry -- when nothing is monitoring at all.  A switch whose
    /// leak detection has died looks healthy.  `SYSTEM_LEAK_STATUS` is worse
    /// still: `bmcctld` subscribes to it and cuts power to the switch host on
    /// what it says.
    ///
    /// Measured on slm-31 against the Python daemon on the same switch:
    /// stopping thermalctld left 5 rows under Rust and 2 under Python, and the
    /// three extra were `LIQUID_COOLING_INFO|leakage1`, `|leakage2` and
    /// `SYSTEM_LEAK_STATUS|system`.
    ///
    /// Best-effort throughout: this runs while the switch is shutting down and
    /// a database that has already gone away must not stop the daemon exiting.
    pub fn clear(&self) {
        for table in [&self.sensor, &self.system, &self.profile] {
            // Swallowed: `thermalctld:LiquidCoolingUpdater.__del__` reads
            // these inside a `try` and passes on any failure.
            for key in table.get_keys().unwrap_or_default() {
                let _ = table.del(&key);
            }
        }
    }
}

impl StateDb {

    /// Delete every row this daemon owns.
    ///
    /// Python does it from two destructors -- `thermalctld:FanUpdater.__del__`
    /// and `thermalctld:TemperatureUpdater.__del__` -- which run when the
    /// interpreter finalises on `sys.exit()`.  The Rust updaters are Rust, so
    /// nothing runs on their behalf and the rows have to go explicitly.
    /// Measured on an SN5640: stopping the Python daemon left 8 rows behind,
    /// stopping this one left 60, every one of them stale and indistinguishable
    /// from live data to `show platform temperature`, to the entity MIB, and to
    /// system-health.
    ///
    /// Every key in each table is removed, not just the ones this run
    /// published -- that is what the Python destructors do, and a row written
    /// by an earlier run of the same daemon is exactly as stale.
    /// `PHYSICAL_ENTITY_INFO` is keyed the same way, so its matching rows go
    /// with them.
    ///
    /// Best-effort throughout: this runs while the switch is shutting down and
    /// a database that has already gone away must not stop the daemon exiting.
    pub fn clear(&self) {
        for table in [&self.fan, &self.fan_drawer, &self.temperature] {
            // Swallowed: the Python destructors read these inside a `try`
            // (`thermalctld:FanUpdater.__del__`,
            // `thermalctld:TemperatureUpdater.__del__`) and pass on any
            // failure.
            for key in table.get_keys().unwrap_or_default() {
                let _ = table.del(&key);
                let _ = self.physical_entity.del(&key);
                if let Some(chassis) = &self.chassis_temperature {
                    let _ = chassis.del(&key);
                }
            }
        }
    }
    pub fn open(slot_or_dpu_id: Option<i64>) -> Result<Self, String> {
        Self::open_with(slot_or_dpu_id, Box::new(open_named))
    }

    /// The same set of tables with the opening step supplied.
    ///
    /// `Table::new` takes the connector by value and `DbConnector` is not
    /// `Clone`, so each table opens its own redis connection. Python shares
    /// one; the extra sockets are the price, and 12 records why.
    pub fn open_with(slot_or_dpu_id: Option<i64>, mut open: OpenNamed) -> Result<Self, String> {
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
    ///
    /// The error is returned rather than logged because the callers want
    /// different things from it.  The fan path follows Python: a fan's row is
    /// written under the `try` in `thermalctld:FanUpdater._collect_fans` and
    /// only warns, while a drawer's, from
    /// `thermalctld:FanUpdater._refresh_fan_drawer_status`, has no `try` and is
    /// fatal here (see `main::ERR_DB_WRITE`).  The temperature path does not:
    /// Python writes that row with no `try` in
    /// `thermalctld:TemperatureUpdater._collect_thermals`, and the caller here
    /// warns and carries on (see `TemperatureUpdater::refresh`).
    pub fn set_entity_info(
        &self,
        key: &str,
        parent_name: &str,
        position_in_parent: &str,
    ) -> Result<(), String> {
        let fvs = [
            ("position_in_parent", position_in_parent.to_string()),
            ("parent_name", parent_name.to_string()),
        ];
        self.physical_entity
            .set(key, &fvs)
            .map_err(|e| format!("failed to update {PHYSICAL_ENTITY_INFO} for {key}: {e}"))
    }
}

// ── A table that is a HashMap ─────────────────────────────────────────────────

#[cfg(test)]
pub mod mock {
    use super::*;
    // The table itself is shared; what stays here is the scaffolding that
    // assembles this daemon's particular sets of them.
    pub use pmon_common::db::MockTable;

    /// A dictionary-backed table, the equivalent of Python's
    /// `tests/mock_swsscommon.Table`.
    ///
    /// Cloning shares the contents, so a test can keep a handle on what the
    /// daemon wrote after handing the table to a `StateDb`.
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
                chassis_temperature: with_chassis.then(|| Box::new(chassis_temperature.clone()) as Box<dyn TableLike>),
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
                    sensor: Box::new(sensor.clone()),
                    system: Box::new(system.clone()),
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

    /// Clearing takes the chassis copy with it.
    ///
    /// On a modular chassis the line card publishes each thermal twice -- once
    /// locally and once into the chassis-wide table -- so a `clear()` that only
    /// did the local half would leave the supervisor showing temperatures for a
    /// card whose daemon has stopped. `run()`'s own tests all use a `StateDb`
    /// without the chassis table, so this branch is only reachable from here.
    #[test]
    fn clearing_takes_the_chassis_copy_with_it() {
        let m = MockDb::new(true);
        for t in [&m.temperature, &m.fan, &m.fan_drawer] {
            TableLike::set(t, "ASIC", &[("temperature", "45".to_string())]).unwrap();
        }
        TableLike::set(&m.chassis_temperature, "ASIC", &[("temperature", "45".to_string())])
            .unwrap();
        TableLike::set(&m.physical_entity, "ASIC", &[("position_in_parent", "1".to_string())])
            .unwrap();

        m.db.clear();

        assert!(m.temperature.is_empty(), "local rows go");
        assert!(m.fan.is_empty());
        assert!(m.fan_drawer.is_empty());
        assert!(m.physical_entity.is_empty());
        assert!(
            m.chassis_temperature.is_empty(),
            "and so does the copy the supervisor reads"
        );
    }

    #[test]
    fn entity_info_carries_the_parent_and_position() {
        let m = MockDb::new(false);
        m.db.set_entity_info("fan1", "drawer1", "1").unwrap();
        assert_eq!(
            m.physical_entity.field("fan1", "parent_name").as_deref(),
            Some("drawer1")
        );
        assert_eq!(
            m.physical_entity.field("fan1", "position_in_parent").as_deref(),
            Some("1")
        );
    }

    /// Python writes position first and parent second; the order is part of
    /// requirement 1a like every other row.
    #[test]
    fn entity_info_field_order_matches_python() {
        let m = MockDb::new(false);
        m.db.set_entity_info("fan1", "drawer1", "1").unwrap();
        let keys: Vec<String> = m
            .physical_entity
            .row("fan1")
            .unwrap()
            .into_iter()
            .map(|(k, _)| k)
            .collect();
        assert_eq!(keys, ["position_in_parent", "parent_name"]);
    }

    /// A PHYSICAL_ENTITY_INFO write that fails is reported rather than
    /// logged, because its two callers need different things from it -- see
    /// `set_entity_info`.
    #[test]
    fn a_failing_write_is_reported_to_the_caller() {
        let m = MockDb::new(false);
        m.physical_entity.fail_writes("redis is down");
        let e = m
            .db
            .set_entity_info("fan1", "drawer1", "1")
            .expect_err("the caller has to be told");
        assert!(e.contains("fan1"), "the error names the row: {e}");
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
