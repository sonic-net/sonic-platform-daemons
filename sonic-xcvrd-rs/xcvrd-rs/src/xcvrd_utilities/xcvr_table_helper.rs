//! `xcvr_table_helper.py` → TRANSCEIVER_* table-name constants + a table registry
//! over the [`DbTable`] seam (analysis §3.2). Names/consts are real data; the
//! registry ([`XcvrTableHelper`]) holds one [`DbTable`] handle per TRANSCEIVER_*
//! table (plus the CONFIG_DB/APPL_DB/STATE_DB port tables) per ASIC, wired at boot
//! Production builds [`RealDbTable`] handles over `swss-common` connections;
//! unit tests build [`crate::mock::MockDbTable`] handles via
//! [`XcvrTableHelper::with_mock_tables`].
#![allow(dead_code, unused_variables, unused_imports)]

use std::collections::{BTreeMap, HashMap};
use std::sync::{Arc, Mutex};

use swss_common::DbConnector;

use crate::db::{DbTable, RealDbTable};
use crate::env;
use crate::error::{Result, XcvrdError};
use crate::xcvrd_utilities::port_event_helper::PortMapping;

// --- TRANSCEIVER_* table names (real data) ----------------------------------------
pub const TRANSCEIVER_INFO_TABLE: &str = "TRANSCEIVER_INFO";
pub const TRANSCEIVER_FIRMWARE_INFO_TABLE: &str = "TRANSCEIVER_FIRMWARE_INFO";
pub const TRANSCEIVER_DOM_SENSOR_TABLE: &str = "TRANSCEIVER_DOM_SENSOR";
pub const TRANSCEIVER_DOM_FLAG_TABLE: &str = "TRANSCEIVER_DOM_FLAG";
pub const TRANSCEIVER_DOM_FLAG_CHANGE_COUNT_TABLE: &str = "TRANSCEIVER_DOM_FLAG_CHANGE_COUNT";
pub const TRANSCEIVER_DOM_FLAG_SET_TIME_TABLE: &str = "TRANSCEIVER_DOM_FLAG_SET_TIME";
pub const TRANSCEIVER_DOM_FLAG_CLEAR_TIME_TABLE: &str = "TRANSCEIVER_DOM_FLAG_CLEAR_TIME";
pub const TRANSCEIVER_DOM_THRESHOLD_TABLE: &str = "TRANSCEIVER_DOM_THRESHOLD";
pub const TRANSCEIVER_DOM_TEMPERATURE_TABLE: &str = "TRANSCEIVER_DOM_TEMPERATURE";
pub const TRANSCEIVER_STATUS_TABLE: &str = "TRANSCEIVER_STATUS";
pub const TRANSCEIVER_STATUS_FLAG_TABLE: &str = "TRANSCEIVER_STATUS_FLAG";
pub const TRANSCEIVER_STATUS_FLAG_CHANGE_COUNT_TABLE: &str = "TRANSCEIVER_STATUS_FLAG_CHANGE_COUNT";
pub const TRANSCEIVER_STATUS_FLAG_SET_TIME_TABLE: &str = "TRANSCEIVER_STATUS_FLAG_SET_TIME";
pub const TRANSCEIVER_STATUS_FLAG_CLEAR_TIME_TABLE: &str = "TRANSCEIVER_STATUS_FLAG_CLEAR_TIME";
pub const TRANSCEIVER_STATUS_SW_TABLE: &str = "TRANSCEIVER_STATUS_SW";
pub const TRANSCEIVER_VDM_REAL_VALUE_TABLE: &str = "TRANSCEIVER_VDM_REAL_VALUE";
pub const TRANSCEIVER_PM_TABLE: &str = "TRANSCEIVER_PM";

// --- STATE_DB PORT_TABLE NPU_SI_SETTINGS_* (media/optics SI) -------------------
pub const NPU_SI_SETTINGS_SYNC_STATUS_KEY: &str = "NPU_SI_SETTINGS_SYNC_STATUS";
pub const NPU_SI_SETTINGS_DEFAULT_VALUE: &str = "NPU_SI_SETTINGS_DEFAULT";
pub const NPU_SI_SETTINGS_NOTIFIED_VALUE: &str = "NPU_SI_SETTINGS_NOTIFIED";

/// STATE_DB `FAST_RESTART_ENABLE_TABLE` — the system fast-reboot flag the CMIS manager
/// consults (`common.is_fast_reboot_enabled`, keyed `system`, field `enable`).
pub const FAST_RESTART_ENABLE_TABLE: &str = "FAST_RESTART_ENABLE_TABLE";

/// STATE_DB `WARM_RESTART_TABLE` — per-process warm-restart state; `syncd.restore_count`
/// > 0 marks a warm reboot (`common.is_syncd_warm_restore_complete`).
pub const WARM_RESTART_TABLE: &str = "WARM_RESTART_TABLE";

/// STATE_DB `WARM_RESTART_ENABLE_TABLE` — the system warm-reboot enable flag
/// (`system.enable == "true"`), the other half of `is_syncd_warm_restore_complete`.
pub const WARM_RESTART_ENABLE_TABLE: &str = "WARM_RESTART_ENABLE_TABLE";

/// `VDM_THRESHOLD_TYPES` (xcvr_table_helper.py).
pub const VDM_THRESHOLD_TYPES: [&str; 4] = ["halarm", "lalarm", "hwarn", "lwarn"];

/// `TRANSCEIVER_VDM_<TYPE>_THRESHOLD` (e.g. `TRANSCEIVER_VDM_HALARM_THRESHOLD`).
pub fn vdm_threshold_table_name(threshold_type: &str) -> String {
    format!(
        "TRANSCEIVER_VDM_{}_THRESHOLD",
        threshold_type.to_uppercase()
    )
}

/// `TRANSCEIVER_VDM_<TYPE>_FLAG`.
pub fn vdm_flag_table_name(threshold_type: &str) -> String {
    format!("TRANSCEIVER_VDM_{}_FLAG", threshold_type.to_uppercase())
}

/// `TRANSCEIVER_VDM_<TYPE>_FLAG_CHANGE_COUNT`.
pub fn vdm_flag_change_count_table_name(threshold_type: &str) -> String {
    format!("{}_CHANGE_COUNT", vdm_flag_table_name(threshold_type))
}

/// `TRANSCEIVER_VDM_<TYPE>_FLAG_SET_TIME`.
pub fn vdm_flag_set_time_table_name(threshold_type: &str) -> String {
    format!("{}_SET_TIME", vdm_flag_table_name(threshold_type))
}

/// `TRANSCEIVER_VDM_<TYPE>_FLAG_CLEAR_TIME`.
pub fn vdm_flag_clear_time_table_name(threshold_type: &str) -> String {
    format!("{}_CLEAR_TIME", vdm_flag_table_name(threshold_type))
}

// --- Port tables (non-TRANSCEIVER_*), keyed like the swss `*_PORT_TABLE_NAME`s ------
/// STATE_DB `PORT_TABLE` (`swsscommon.STATE_PORT_TABLE_NAME`) — NPU SI sync status.
pub const STATE_PORT_TABLE: &str = "PORT_TABLE";
/// CONFIG_DB `PORT` (`swsscommon.CFG_PORT_TABLE_NAME`) — the logical port config.
pub const CFG_PORT_TABLE: &str = "PORT";
/// APPL_DB `PORT_TABLE` (`swsscommon.APP_PORT_TABLE_NAME`, `:`-separated).
pub const APP_PORT_TABLE: &str = "PORT_TABLE";
/// APPL_DB gearbox interfaces and PHY configuration.
pub const GEARBOX_TABLE: &str = "_GEARBOX_TABLE";

/// Which Redis DB a registry table lives in — selects the connection + key separator
/// the production builder uses (STATE/CONFIG use `|`, APPL uses `:`).
#[derive(Clone, Copy)]
enum Db {
    State,
    Config,
    Appl,
}

/// One ASIC's namespace and table handles.
struct AsicTables {
    namespace: String,
    int_tbl: Arc<dyn DbTable>,
    dom_tbl: Arc<dyn DbTable>,
    dom_flag_tbl: Arc<dyn DbTable>,
    dom_flag_change_count_tbl: Arc<dyn DbTable>,
    dom_flag_set_time_tbl: Arc<dyn DbTable>,
    dom_flag_clear_time_tbl: Arc<dyn DbTable>,
    dom_threshold_tbl: Arc<dyn DbTable>,
    dom_temperature_tbl: Arc<dyn DbTable>,
    status_tbl: Arc<dyn DbTable>,
    status_flag_tbl: Arc<dyn DbTable>,
    status_flag_change_count_tbl: Arc<dyn DbTable>,
    status_flag_set_time_tbl: Arc<dyn DbTable>,
    status_flag_clear_time_tbl: Arc<dyn DbTable>,
    status_sw_tbl: Arc<dyn DbTable>,
    pm_tbl: Arc<dyn DbTable>,
    firmware_info_tbl: Arc<dyn DbTable>,
    vdm_real_value_tbl: Arc<dyn DbTable>,
    vdm_threshold_tbl: BTreeMap<String, Arc<dyn DbTable>>,
    vdm_flag_tbl: BTreeMap<String, Arc<dyn DbTable>>,
    vdm_flag_change_count_tbl: BTreeMap<String, Arc<dyn DbTable>>,
    vdm_flag_set_time_tbl: BTreeMap<String, Arc<dyn DbTable>>,
    vdm_flag_clear_time_tbl: BTreeMap<String, Arc<dyn DbTable>>,
    state_port_tbl: Arc<dyn DbTable>,
    cfg_port_tbl: Arc<dyn DbTable>,
    app_port_tbl: Arc<dyn DbTable>,
    gearbox_tbl: Arc<dyn DbTable>,
    fast_restart_enable_tbl: Arc<dyn DbTable>,
    warm_restart_tbl: Arc<dyn DbTable>,
    warm_restart_enable_tbl: Arc<dyn DbTable>,
}

/// `XcvrTableHelper` — one [`DbTable`] handle per TRANSCEIVER_* table per ASIC, plus the
/// CONFIG_DB / APPL_DB / STATE_DB port tables. Keyed by actual ASIC index, which
/// need not be contiguous when backend ASICs are excluded.
pub struct XcvrTableHelper {
    asics: BTreeMap<usize, AsicTables>,
}

impl XcvrTableHelper {
    /// Build cached native tables using each namespace's DB configuration.
    pub fn new(namespaces: &[String]) -> Result<Self> {
        struct Conns {
            state: Arc<Mutex<DbConnector>>,
            config: Arc<Mutex<DbConnector>>,
            appl: Arc<Mutex<DbConnector>>,
        }
        let mut conns = BTreeMap::new();
        for namespace in namespaces {
            conns.insert(
                env::namespace_asic_id(namespace)?,
                Conns {
                    state: Arc::new(Mutex::new(env::open_state_db_in_namespace(namespace)?)),
                    config: Arc::new(Mutex::new(env::open_config_db_in_namespace(namespace)?)),
                    appl: Arc::new(Mutex::new(env::open_appl_db_in_namespace(namespace)?)),
                },
            );
        }
        Self::build(namespaces, |asic, db, name| {
            let c = &conns[&asic];
            let table = match db {
                Db::State => RealDbTable::new(c.state.clone(), name)?,
                Db::Config => RealDbTable::new(c.config.clone(), name)?,
                Db::Appl if name == APP_PORT_TABLE => {
                    RealDbTable::new_producer(c.appl.clone(), name)?
                }
                Db::Appl => RealDbTable::new(c.appl.clone(), name)?,
            };
            Ok(Arc::new(table) as Arc<dyn DbTable>)
        })
    }

    /// Test constructor: every handle is an in-memory [`crate::mock::MockDbTable`]
    /// (the analogue of the Python tests swapping `swsscommon.Table` for
    /// `mock_swsscommon.Table`), so registry consumers run without Redis.
    #[cfg(test)]
    pub fn with_mock_tables(namespaces: &[String]) -> Self {
        Self::build(namespaces, |_asic, _db, name| {
            Ok(Arc::new(crate::mock::MockDbTable::new(name)) as Arc<dyn DbTable>)
        })
        .expect("valid mock ASIC namespaces")
    }

    /// Build the per-ASIC table set via a factory that turns a `(asic, db, name)`
    /// triple into a [`DbTable`] handle — real (`swss-common`) or mock.
    fn build<F>(namespaces: &[String], mut make: F) -> Result<Self>
    where
        F: FnMut(usize, Db, &str) -> Result<Arc<dyn DbTable>>,
    {
        let mut asics = BTreeMap::new();
        for namespace in namespaces {
            let asic_id = env::namespace_asic_id(namespace)?;
            if asics.contains_key(&asic_id) {
                return Err(XcvrdError::Other(format!("duplicate ASIC index {asic_id}")));
            }
            let mut vdm_threshold_tbl = BTreeMap::new();
            let mut vdm_flag_tbl = BTreeMap::new();
            let mut vdm_flag_change_count_tbl = BTreeMap::new();
            let mut vdm_flag_set_time_tbl = BTreeMap::new();
            let mut vdm_flag_clear_time_tbl = BTreeMap::new();
            for t in VDM_THRESHOLD_TYPES {
                vdm_threshold_tbl.insert(
                    t.to_string(),
                    make(asic_id, Db::State, &vdm_threshold_table_name(t))?,
                );
                vdm_flag_tbl.insert(
                    t.to_string(),
                    make(asic_id, Db::State, &vdm_flag_table_name(t))?,
                );
                vdm_flag_change_count_tbl.insert(
                    t.to_string(),
                    make(asic_id, Db::State, &vdm_flag_change_count_table_name(t))?,
                );
                vdm_flag_set_time_tbl.insert(
                    t.to_string(),
                    make(asic_id, Db::State, &vdm_flag_set_time_table_name(t))?,
                );
                vdm_flag_clear_time_tbl.insert(
                    t.to_string(),
                    make(asic_id, Db::State, &vdm_flag_clear_time_table_name(t))?,
                );
            }
            asics.insert(
                asic_id,
                AsicTables {
                    namespace: namespace.clone(),
                    int_tbl: make(asic_id, Db::State, TRANSCEIVER_INFO_TABLE)?,
                    dom_tbl: make(asic_id, Db::State, TRANSCEIVER_DOM_SENSOR_TABLE)?,
                    dom_flag_tbl: make(asic_id, Db::State, TRANSCEIVER_DOM_FLAG_TABLE)?,
                    dom_flag_change_count_tbl: make(
                        asic_id,
                        Db::State,
                        TRANSCEIVER_DOM_FLAG_CHANGE_COUNT_TABLE,
                    )?,
                    dom_flag_set_time_tbl: make(
                        asic_id,
                        Db::State,
                        TRANSCEIVER_DOM_FLAG_SET_TIME_TABLE,
                    )?,
                    dom_flag_clear_time_tbl: make(
                        asic_id,
                        Db::State,
                        TRANSCEIVER_DOM_FLAG_CLEAR_TIME_TABLE,
                    )?,
                    dom_threshold_tbl: make(asic_id, Db::State, TRANSCEIVER_DOM_THRESHOLD_TABLE)?,
                    dom_temperature_tbl: make(
                        asic_id,
                        Db::State,
                        TRANSCEIVER_DOM_TEMPERATURE_TABLE,
                    )?,
                    status_tbl: make(asic_id, Db::State, TRANSCEIVER_STATUS_TABLE)?,
                    status_flag_tbl: make(asic_id, Db::State, TRANSCEIVER_STATUS_FLAG_TABLE)?,
                    status_flag_change_count_tbl: make(
                        asic_id,
                        Db::State,
                        TRANSCEIVER_STATUS_FLAG_CHANGE_COUNT_TABLE,
                    )?,
                    status_flag_set_time_tbl: make(
                        asic_id,
                        Db::State,
                        TRANSCEIVER_STATUS_FLAG_SET_TIME_TABLE,
                    )?,
                    status_flag_clear_time_tbl: make(
                        asic_id,
                        Db::State,
                        TRANSCEIVER_STATUS_FLAG_CLEAR_TIME_TABLE,
                    )?,
                    status_sw_tbl: make(asic_id, Db::State, TRANSCEIVER_STATUS_SW_TABLE)?,
                    pm_tbl: make(asic_id, Db::State, TRANSCEIVER_PM_TABLE)?,
                    firmware_info_tbl: make(asic_id, Db::State, TRANSCEIVER_FIRMWARE_INFO_TABLE)?,
                    vdm_real_value_tbl: make(asic_id, Db::State, TRANSCEIVER_VDM_REAL_VALUE_TABLE)?,
                    vdm_threshold_tbl,
                    vdm_flag_tbl,
                    vdm_flag_change_count_tbl,
                    vdm_flag_set_time_tbl,
                    vdm_flag_clear_time_tbl,
                    state_port_tbl: make(asic_id, Db::State, STATE_PORT_TABLE)?,
                    cfg_port_tbl: make(asic_id, Db::Config, CFG_PORT_TABLE)?,
                    app_port_tbl: make(asic_id, Db::Appl, APP_PORT_TABLE)?,
                    gearbox_tbl: make(asic_id, Db::Appl, GEARBOX_TABLE)?,
                    fast_restart_enable_tbl: make(asic_id, Db::State, FAST_RESTART_ENABLE_TABLE)?,
                    warm_restart_tbl: make(asic_id, Db::State, WARM_RESTART_TABLE)?,
                    warm_restart_enable_tbl: make(asic_id, Db::State, WARM_RESTART_ENABLE_TABLE)?,
                },
            );
        }
        Ok(XcvrTableHelper { asics })
    }

    fn asic(&self, asic_id: usize) -> &AsicTables {
        &self.asics[&asic_id]
    }

    /// Actual front-end ASIC indices.
    pub fn asic_ids(&self) -> impl Iterator<Item = usize> + '_ {
        self.asics.keys().copied()
    }

    /// Namespaces corresponding to the registered front-end ASICs.
    pub fn namespaces(&self) -> Vec<String> {
        self.asics
            .values()
            .map(|asic| asic.namespace.clone())
            .collect()
    }

    pub fn get_intf_tbl(&self, asic_id: usize) -> &dyn DbTable {
        self.asic(asic_id).int_tbl.as_ref()
    }
    pub fn get_dom_tbl(&self, asic_id: usize) -> &dyn DbTable {
        self.asic(asic_id).dom_tbl.as_ref()
    }
    pub fn get_dom_flag_tbl(&self, asic_id: usize) -> &dyn DbTable {
        self.asic(asic_id).dom_flag_tbl.as_ref()
    }
    pub fn get_dom_flag_change_count_tbl(&self, asic_id: usize) -> &dyn DbTable {
        self.asic(asic_id).dom_flag_change_count_tbl.as_ref()
    }
    pub fn get_dom_flag_set_time_tbl(&self, asic_id: usize) -> &dyn DbTable {
        self.asic(asic_id).dom_flag_set_time_tbl.as_ref()
    }
    pub fn get_dom_flag_clear_time_tbl(&self, asic_id: usize) -> &dyn DbTable {
        self.asic(asic_id).dom_flag_clear_time_tbl.as_ref()
    }
    pub fn get_dom_threshold_tbl(&self, asic_id: usize) -> &dyn DbTable {
        self.asic(asic_id).dom_threshold_tbl.as_ref()
    }
    pub fn get_dom_temperature_tbl(&self, asic_id: usize) -> &dyn DbTable {
        self.asic(asic_id).dom_temperature_tbl.as_ref()
    }
    pub fn get_status_tbl(&self, asic_id: usize) -> &dyn DbTable {
        self.asic(asic_id).status_tbl.as_ref()
    }
    pub fn get_status_flag_tbl(&self, asic_id: usize) -> &dyn DbTable {
        self.asic(asic_id).status_flag_tbl.as_ref()
    }
    pub fn get_status_flag_change_count_tbl(&self, asic_id: usize) -> &dyn DbTable {
        self.asic(asic_id).status_flag_change_count_tbl.as_ref()
    }
    pub fn get_status_flag_set_time_tbl(&self, asic_id: usize) -> &dyn DbTable {
        self.asic(asic_id).status_flag_set_time_tbl.as_ref()
    }
    pub fn get_status_flag_clear_time_tbl(&self, asic_id: usize) -> &dyn DbTable {
        self.asic(asic_id).status_flag_clear_time_tbl.as_ref()
    }
    pub fn get_status_sw_tbl(&self, asic_id: usize) -> &dyn DbTable {
        self.asic(asic_id).status_sw_tbl.as_ref()
    }
    pub fn get_pm_tbl(&self, asic_id: usize) -> &dyn DbTable {
        self.asic(asic_id).pm_tbl.as_ref()
    }
    pub fn get_firmware_info_tbl(&self, asic_id: usize) -> &dyn DbTable {
        self.asic(asic_id).firmware_info_tbl.as_ref()
    }
    pub fn get_vdm_real_value_tbl(&self, asic_id: usize) -> &dyn DbTable {
        self.asic(asic_id).vdm_real_value_tbl.as_ref()
    }
    pub fn get_vdm_threshold_tbl(&self, asic_id: usize, threshold_type: &str) -> &dyn DbTable {
        self.asic(asic_id).vdm_threshold_tbl[threshold_type].as_ref()
    }
    pub fn get_vdm_flag_tbl(&self, asic_id: usize, threshold_type: &str) -> &dyn DbTable {
        self.asic(asic_id).vdm_flag_tbl[threshold_type].as_ref()
    }
    pub fn get_vdm_flag_change_count_tbl(
        &self,
        asic_id: usize,
        threshold_type: &str,
    ) -> &dyn DbTable {
        self.asic(asic_id).vdm_flag_change_count_tbl[threshold_type].as_ref()
    }
    pub fn get_vdm_flag_set_time_tbl(&self, asic_id: usize, threshold_type: &str) -> &dyn DbTable {
        self.asic(asic_id).vdm_flag_set_time_tbl[threshold_type].as_ref()
    }
    pub fn get_vdm_flag_clear_time_tbl(
        &self,
        asic_id: usize,
        threshold_type: &str,
    ) -> &dyn DbTable {
        self.asic(asic_id).vdm_flag_clear_time_tbl[threshold_type].as_ref()
    }
    pub fn get_state_port_tbl(&self, asic_id: usize) -> &dyn DbTable {
        self.asic(asic_id).state_port_tbl.as_ref()
    }
    pub fn get_cfg_port_tbl(&self, asic_id: usize) -> &dyn DbTable {
        self.asic(asic_id).cfg_port_tbl.as_ref()
    }
    pub fn get_app_port_tbl(&self, asic_id: usize) -> &dyn DbTable {
        self.asic(asic_id).app_port_tbl.as_ref()
    }
    pub fn get_fast_restart_enable_tbl(&self, asic_id: usize) -> &dyn DbTable {
        self.asic(asic_id).fast_restart_enable_tbl.as_ref()
    }
    pub fn get_warm_restart_tbl(&self, asic_id: usize) -> &dyn DbTable {
        self.asic(asic_id).warm_restart_tbl.as_ref()
    }
    pub fn get_warm_restart_enable_tbl(&self, asic_id: usize) -> &dyn DbTable {
        self.asic(asic_id).warm_restart_enable_tbl.as_ref()
    }

    /// `common.is_syncd_warm_restore_complete()` — is this boot a warm reboot? True when
    /// STATE_DB `WARM_RESTART_TABLE|syncd.restore_count > 0` or
    /// `WARM_RESTART_ENABLE_TABLE|system.enable == "true"`. The boot-time media SI notify
    /// is skipped on a warm reboot so it does not flap an in-service datapath
    /// (xcvrd.py:337). Absent/non-numeric fields read as "not warm" (a cold boot).
    pub fn is_syncd_warm_restore_complete(&self, asic_id: usize) -> bool {
        let a = self.asic(asic_id);
        crate::xcvrd_utilities::common::is_syncd_warm_restore_complete(
            a.warm_restart_tbl.as_ref(),
            a.warm_restart_enable_tbl.as_ref(),
        )
    }

    /// `get_state_db_port_table_val_by_key` — read a field from STATE_DB
    /// `PORT_TABLE|<lport>`; `None` if the port mapping/asic/row/key is missing.
    pub fn get_state_db_port_table_val_by_key(
        &self,
        lport: &str,
        port_mapping: Option<&PortMapping>,
        key: &str,
    ) -> Option<String> {
        let asic_index = port_mapping?.get_asic_id_for_logical_port(lport)?;
        let tbl = self.asics.get(&asic_index)?.state_port_tbl.as_ref();
        tbl.hget(lport, key)
    }

    /// `is_npu_si_settings_update_required(lport, port_mapping)` — true when the
    /// `NPU_SI_SETTINGS_SYNC_STATUS` field is absent or still `NPU_SI_SETTINGS_DEFAULT`.
    pub fn is_npu_si_settings_update_required(
        &self,
        lport: &str,
        port_mapping: &PortMapping,
    ) -> bool {
        let val = self.get_state_db_port_table_val_by_key(
            lport,
            Some(port_mapping),
            NPU_SI_SETTINGS_SYNC_STATUS_KEY,
        );
        val.is_none() || val.as_deref() == Some(NPU_SI_SETTINGS_DEFAULT_VALUE)
    }

    /// `get_gearbox_line_lanes_dict()` — the per-logical-port gearbox *line* (media-side)
    /// lane count, read from the APPL_DB `_GEARBOX_TABLE` `interface:*` rows the CMIS
    /// manager consults to pick the host lane count on gearbox platforms.
    pub fn get_gearbox_line_lanes_dict(&self) -> HashMap<String, u32> {
        let mut lanes = HashMap::new();
        for asic in self.asics.values() {
            for key in asic.gearbox_tbl.get_keys() {
                if !key.starts_with("interface:") {
                    continue;
                }
                let Some(row) = asic.gearbox_tbl.get(&key) else {
                    continue;
                };
                let fields: HashMap<_, _> = row.into_iter().collect();
                let Some(name) = fields.get("name").filter(|name| !name.is_empty()) else {
                    continue;
                };
                let Some(line_lanes) = fields.get("line_lanes") else {
                    continue;
                };
                let count = line_lanes
                    .split(',')
                    .filter(|lane| !lane.trim().is_empty())
                    .count();
                if count > 0 {
                    lanes.insert(name.clone(), count as u32);
                }
            }
        }
        lanes
    }
}

/// Open a real STATE_DB-backed table over a shared connection (registry helper).
pub fn open_table(conn: Arc<Mutex<DbConnector>>, name: &str) -> Result<RealDbTable> {
    RealDbTable::new(conn, name)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::xcvrd_utilities::port_event_helper::{PortChangeEvent, PortChangeEventType};

    #[test]
    fn vdm_threshold_and_flag_table_names() {
        // Pure table-name data — kept real so the registry/tests are traceable.
        assert_eq!(
            vdm_threshold_table_name("halarm"),
            "TRANSCEIVER_VDM_HALARM_THRESHOLD"
        );
        assert_eq!(vdm_flag_table_name("lwarn"), "TRANSCEIVER_VDM_LWARN_FLAG");
        assert_eq!(
            vdm_flag_change_count_table_name("hwarn"),
            "TRANSCEIVER_VDM_HWARN_FLAG_CHANGE_COUNT"
        );
    }

    // The registry hands out an independent table per name: a write to one handle is
    // not visible from another (the analogue of distinct `swsscommon.Table`s).
    #[test]
    fn registry_getters_return_independent_tables() {
        let th = XcvrTableHelper::with_mock_tables(&[String::new()]);
        th.get_dom_tbl(0).hset("Ethernet0", "temperature", "22.75");
        assert_eq!(th.get_dom_tbl(0).get_size_for_key("Ethernet0"), 1);
        // A different table for the same key is empty → distinct handles.
        assert_eq!(th.get_status_tbl(0).get_size_for_key("Ethernet0"), 0);
        assert_eq!(th.get_intf_tbl(0).get_size_for_key("Ethernet0"), 0);
    }

    #[test]
    fn registry_preserves_sparse_asic_indices() {
        let th = XcvrTableHelper::with_mock_tables(&["asic1".into(), "asic3".into()]);
        th.get_dom_tbl(1).hset("Ethernet0", "temperature", "22");
        th.get_dom_tbl(3).hset("Ethernet0", "temperature", "33");
        assert_eq!(
            th.get_dom_tbl(1)
                .hget("Ethernet0", "temperature")
                .as_deref(),
            Some("22")
        );
        assert_eq!(
            th.get_dom_tbl(3)
                .hget("Ethernet0", "temperature")
                .as_deref(),
            Some("33")
        );
        assert_eq!(th.asic_ids().collect::<Vec<_>>(), [1, 3]);
    }

    #[test]
    fn gearbox_lanes_are_read_across_namespaces_and_invalid_rows_are_skipped() {
        let th = XcvrTableHelper::with_mock_tables(&["asic1".into(), "asic3".into()]);
        for (asic, key, fields) in [
            (
                1,
                "interface:0",
                vec![("name", "Ethernet0"), ("line_lanes", "0,1")],
            ),
            (
                3,
                "interface:1",
                vec![("name", "Ethernet8"), ("line_lanes", "4,5,6,7")],
            ),
            (
                1,
                "phy:0",
                vec![("name", "Ethernet16"), ("line_lanes", "0,1")],
            ),
            (1, "interface:missing-name", vec![("line_lanes", "0,1")]),
            (3, "interface:missing-lanes", vec![("name", "Ethernet24")]),
            (
                3,
                "interface:empty-lanes",
                vec![("name", "Ethernet32"), ("line_lanes", "")],
            ),
        ] {
            let fields: Vec<_> = fields
                .into_iter()
                .map(|(f, v)| (f.into(), v.into()))
                .collect();
            th.asic(asic).gearbox_tbl.set(key, &fields);
        }
        assert_eq!(
            th.get_gearbox_line_lanes_dict(),
            HashMap::from([("Ethernet0".into(), 2), ("Ethernet8".into(), 4),])
        );
    }

    // Each VDM threshold type gets its own four handles.
    #[test]
    fn registry_vdm_getters_are_per_type() {
        let th = XcvrTableHelper::with_mock_tables(&[String::new()]);
        for t in VDM_THRESHOLD_TYPES {
            th.get_vdm_threshold_tbl(0, t).hset("Ethernet0", "f", "v");
            th.get_vdm_flag_tbl(0, t).hset("Ethernet0", "f", "v");
        }
        // halarm's flag handle only holds what we wrote to it.
        assert_eq!(
            th.get_vdm_flag_tbl(0, "halarm")
                .get_size_for_key("Ethernet0"),
            1
        );
        assert_eq!(
            th.get_vdm_flag_change_count_tbl(0, "halarm")
                .get_size_for_key("Ethernet0"),
            0
        );
    }

    // ← tests/test_xcvrd.py::test_is_npu_si_settings_update_required
    #[test]
    fn is_npu_si_settings_update_required_matches_python() {
        let th = XcvrTableHelper::with_mock_tables(&[String::new()]);
        let mut pm = PortMapping::new();
        pm.handle_port_change_event(&PortChangeEvent::new(
            "Ethernet0".into(),
            Some(0),
            0,
            PortChangeEventType::Add,
            "CONFIG_DB".into(),
            "PORT".into(),
        ));

        // No PORT_TABLE row at all → update required (key absent).
        assert!(th.is_npu_si_settings_update_required("Ethernet0", &pm));

        // Seeded with the DEFAULT sentinel → still required.
        th.get_state_port_tbl(0).hset(
            "Ethernet0",
            NPU_SI_SETTINGS_SYNC_STATUS_KEY,
            NPU_SI_SETTINGS_DEFAULT_VALUE,
        );
        assert!(th.is_npu_si_settings_update_required("Ethernet0", &pm));

        // NOTIFIED → no longer required.
        th.get_state_port_tbl(0).hset(
            "Ethernet0",
            NPU_SI_SETTINGS_SYNC_STATUS_KEY,
            NPU_SI_SETTINGS_NOTIFIED_VALUE,
        );
        assert!(!th.is_npu_si_settings_update_required("Ethernet0", &pm));
    }

    // ← common.is_syncd_warm_restore_complete: warm reboot detection from STATE_DB.
    #[test]
    fn is_syncd_warm_restore_complete_matches_python() {
        let th = XcvrTableHelper::with_mock_tables(&[String::new()]);
        // Cold boot: neither warm-restart table populated → not warm.
        assert!(!th.is_syncd_warm_restore_complete(0));

        // syncd.restore_count > 0 → warm.
        th.asic(0)
            .warm_restart_tbl
            .hset("syncd", "restore_count", "2");
        assert!(th.is_syncd_warm_restore_complete(0));

        // restore_count 0 alone is not warm...
        let th2 = XcvrTableHelper::with_mock_tables(&[String::new()]);
        th2.asic(0)
            .warm_restart_tbl
            .hset("syncd", "restore_count", "0");
        assert!(!th2.is_syncd_warm_restore_complete(0));
        // ...but system.enable == "true" is.
        th2.asic(0)
            .warm_restart_enable_tbl
            .hset("system", "enable", "true");
        assert!(th2.is_syncd_warm_restore_complete(0));
    }
}
