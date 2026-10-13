//! The table, key and field names chassisd publishes under.
//!
//! Gathered in one place because they are spread over four databases and two
//! quite different machines -- a modular chassis and a SmartSwitch -- and
//! because every one of them is read by something else: `show chassis modules`,
//! the chassis app DB cleanup, the DPU health checks.

// ── STATE_DB ──────────────────────────────────────────────────────────────────

pub const CHASSIS_INFO_TABLE: &str = "CHASSIS_TABLE";
pub const CHASSIS_INFO_KEY: &str = "CHASSIS 1";
pub const CHASSIS_INFO_CARD_NUM_FIELD: &str = "module_num";

pub const CHASSIS_MODULE_INFO_TABLE: &str = "CHASSIS_MODULE_TABLE";
pub const CHASSIS_MIDPLANE_INFO_TABLE: &str = "CHASSIS_MIDPLANE_TABLE";
pub const PHYSICAL_ENTITY_INFO_TABLE: &str = "PHYSICAL_ENTITY_INFO";

// ── CHASSIS_STATE_DB ──────────────────────────────────────────────────────────

pub const CHASSIS_STATE_DB: &str = "CHASSIS_STATE_DB";
/// On a supervisor the fabric cards' ASICs; on a line card its own.
pub const CHASSIS_ASIC_INFO_TABLE: &str = "CHASSIS_ASIC_TABLE";
pub const CHASSIS_FABRIC_ASIC_INFO_TABLE: &str = "CHASSIS_FABRIC_ASIC_TABLE";
/// Same name as the STATE_DB table, different database and different contents:
/// this one maps a module to the hostname its containers run under, which is
/// the key the chassis app DB is partitioned by.
pub const CHASSIS_MODULE_HOSTNAME_TABLE: &str = "CHASSIS_MODULE_TABLE";
pub const CHASSIS_MODULE_REBOOT_INFO_TABLE: &str = "CHASSIS_MODULE_REBOOT_INFO_TABLE";

// ── CONFIG_DB ─────────────────────────────────────────────────────────────────

pub const CONFIG_DB: &str = "CONFIG_DB";
pub const CHASSIS_CFG_TABLE: &str = "CHASSIS_MODULE";
pub const CHASSIS_MODULE_ADMIN_STATUS: &str = "admin_status";

// ── Fields ────────────────────────────────────────────────────────────────────

pub const NAME_FIELD: &str = "name";
pub const DESC_FIELD: &str = "desc";
pub const SLOT_FIELD: &str = "slot";
pub const OPERSTATUS_FIELD: &str = "oper_status";
pub const NUM_ASICS_FIELD: &str = "num_asics";
pub const SERIAL_FIELD: &str = "serial";
pub const PRESENCE_FIELD: &str = "presence";
pub const MODEL_FIELD: &str = "model";
pub const REPLACEABLE_FIELD: &str = "is_replaceable";
pub const HOSTNAME_FIELD: &str = "hostname";

pub const ASIC_PREFIX: &str = "asic";
pub const ASIC_PCI_ADDRESS_FIELD: &str = "asic_pci_address";
pub const ASIC_ID_IN_MODULE_FIELD: &str = "asic_id_in_module";

pub const MIDPLANE_IP_FIELD: &str = "ip_address";
pub const MIDPLANE_ACCESS_FIELD: &str = "access";

pub const REBOOT_TIMESTAMP_FIELD: &str = "timestamp";
pub const REBOOT_REBOOT_FIELD: &str = "reboot";
pub const REBOOT_EXPECTED: &str = "expected";

/// `ModuleBase.MODULE_INVALID_SLOT`.
pub const INVALID_SLOT: i64 = -1;
/// What a module with no midplane address reports.
pub const INVALID_IP: &str = "0.0.0.0";

/// The parent every module hangs off in PHYSICAL_ENTITY_INFO.
pub const CHASSIS_PARENT: &str = "chassis 1";
