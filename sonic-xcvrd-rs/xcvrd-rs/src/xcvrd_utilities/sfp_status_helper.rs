//! `sfp_status_helper.py` → error masks + `SfpBase.SFP_ERROR_BIT_TO_DESCRIPTION_DICT`.
//!
//! The bit→description table is NOT on the `platform-bridge`, so it is replicated
//! here as a Rust const (analysis §3.4) — error decode must be byte-identical to the
//! e2e assertions (`test_status_error.py`). The masks + decode helpers
//! (`is_error_block_eeprom_reading`, `fetch_generic_error_description`,
//! `detect_port_in_error_status`) drive the `SfpStateUpdateTask` error branch and
//! the DOM error-status gate.
#![allow(dead_code, unused_variables, unused_imports)]

use crate::db::DbTable;

// --- SFP_STATUS_* -----------------------------------------------------------------
pub const SFP_STATUS_REMOVED: &str = "0";
pub const SFP_STATUS_INSERTED: &str = "1";

// --- error masks (sfp_status_helper.py) -------------------------------------------
pub const SFP_ERRORS_BLOCKING_MASK: u32 = 0x02;
pub const SFP_ERRORS_GENERIC_MASK: u32 = 0x0000_FFFE;
pub const SFP_ERRORS_VENDOR_SPECIFIC_MASK: u32 = 0xFFFF_0000;

/// The blocking sentinel string `detect_port_in_error_status` looks for in
/// `TRANSCEIVER_STATUS_SW.error`.
pub const SFP_ERROR_DESCRIPTION_BLOCKING: &str = "Blocking EEPROM from being read";

/// `SfpBase.SFP_ERROR_BIT_TO_DESCRIPTION_DICT` (bit → description). Real data — the
/// e2e error assertions read these exact strings.
///
/// TODO: confirm the full bit set against the emulator + upstream
/// `SfpBase` and extend as error scenarios are exercised.
pub const SFP_ERROR_BIT_TO_DESCRIPTION: &[(u32, &str)] = &[
    (0x0002, "Blocking EEPROM from being read"),
    (0x0004, "Power budget exceeded"),
    (0x0008, "Bus stuck (I2C data or clock shorted)"),
    (0x0010, "Bad or unsupported EEPROM"),
    (0x0020, "Unsupported cable"),
    (0x0040, "High temperature"),
    (0x0080, "Bad cable (module/cable is shorted)"),
];

/// `is_error_block_eeprom_reading(error_bits)` — does this bitmap block the EEPROM read
/// (→ delete DOM rows but keep TRANSCEIVER_INFO)?
pub fn is_error_block_eeprom_reading(error_bits: u32) -> bool {
    (error_bits & SFP_ERRORS_BLOCKING_MASK) != 0
}

/// `has_vendor_specific_error(error_bits)`.
pub fn has_vendor_specific_error(error_bits: u32) -> bool {
    (error_bits & SFP_ERRORS_VENDOR_SPECIFIC_MASK) != 0
}

/// `fetch_generic_error_description(error_bits)` → the `|`-joinable description list.
///
/// Mirrors `sfp_status_helper.fetch_generic_error_description`: mask to the generic
/// bits, then append each matching description in `SFP_ERROR_BIT_TO_DESCRIPTION` order
/// (the same insertion order as `SfpBase.SFP_ERROR_BIT_TO_DESCRIPTION_DICT`), so the
/// joined string is byte-identical to the reference (`test_status_error.py`).
pub fn fetch_generic_error_description(error_bits: u32) -> Vec<String> {
    let generic_error_bits = error_bits & SFP_ERRORS_GENERIC_MASK;
    let mut descriptions = Vec::new();
    if generic_error_bits != 0 {
        for (error_bit, description) in SFP_ERROR_BIT_TO_DESCRIPTION {
            if error_bit & generic_error_bits != 0 {
                descriptions.push((*description).to_string());
            }
        }
    }
    descriptions
}

/// `detect_port_in_error_status(logical_port, status_sw_tbl)` — gate DOM polling on a
/// blocking error present in `TRANSCEIVER_STATUS_SW.error`.
pub fn detect_port_in_error_status(logical_port_name: &str, status_sw_tbl: &dyn DbTable) -> bool {
    // Mirror sfp_status_helper.detect_port_in_error_status: a missing row / missing
    // `error` field is not an error; otherwise the port is blocked iff its `error`
    // description contains the blocking sentinel.
    match status_sw_tbl.get(logical_port_name) {
        Some(row) => row
            .iter()
            .find(|(k, _)| k == "error")
            .map(|(_, error)| error.contains(SFP_ERROR_DESCRIPTION_BLOCKING))
            .unwrap_or(false),
        None => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // ← tests/test_xcvrd.py::test_detect_port_in_error_status
    #[test]
    fn test_detect_port_in_error_status() {
        use crate::db::DbTable;
        use crate::mock::MockDbTable;
        let status_sw = MockDbTable::new("TRANSCEIVER_STATUS_SW");
        // No row → not in error.
        assert!(!detect_port_in_error_status("Ethernet0", &status_sw));
        // Row without an `error` field → not in error.
        status_sw.set("Ethernet0", &[("status".into(), "1".into())]);
        assert!(!detect_port_in_error_status("Ethernet0", &status_sw));
        // `error` = "N/A" (no blocking sentinel) → not blocked.
        status_sw.set("Ethernet0", &[("error".into(), "N/A".into())]);
        assert!(!detect_port_in_error_status("Ethernet0", &status_sw));
        // `error` contains the blocking sentinel → blocked.
        status_sw.set(
            "Ethernet0",
            &[("error".into(), SFP_ERROR_DESCRIPTION_BLOCKING.into())],
        );
        assert!(detect_port_in_error_status("Ethernet0", &status_sw));
        // Blocking sentinel joined with another description still blocks.
        status_sw.set(
            "Ethernet0",
            &[(
                "error".into(),
                format!("{}|Power budget exceeded", SFP_ERROR_DESCRIPTION_BLOCKING),
            )],
        );
        assert!(detect_port_in_error_status("Ethernet0", &status_sw));
    }

    // Error-bit decode (needed early by the SfpStateUpdateTask error branch): the
    // masks + the |-joinable generic description list must be byte-identical to the
    // reference SfpBase.SFP_ERROR_BIT_TO_DESCRIPTION_DICT ordering.
    #[test]
    fn error_masks_classify_bits() {
        assert!(is_error_block_eeprom_reading(0x02));
        assert!(!is_error_block_eeprom_reading(0x04));
        assert!(has_vendor_specific_error(0x0001_0000));
        assert!(!has_vendor_specific_error(0x0000_00FF));
    }

    // ← tests/test_xcvrd.py::test_is_error_sfp_status. Bitmaps carrying the blocking bit
    // (0x02) block the EEPROM read; the bare INSERTED/REMOVED presence codes do not.
    #[test]
    fn test_is_error_sfp_status() {
        for error_value in [7u32, 11, 19, 35] {
            assert!(
                is_error_block_eeprom_reading(error_value),
                "{error_value} carries the blocking bit and must block the EEPROM read"
            );
        }
        let inserted: u32 = SFP_STATUS_INSERTED.parse().unwrap();
        let removed: u32 = SFP_STATUS_REMOVED.parse().unwrap();
        assert!(!is_error_block_eeprom_reading(inserted));
        assert!(!is_error_block_eeprom_reading(removed));
    }

    #[test]
    fn fetch_generic_error_description_orders_like_reference() {
        // Blocking (0x02) | Power budget exceeded (0x04) → the exact string the
        // reference joins with '|', in insertion order.
        let descs = fetch_generic_error_description(0x02 | 0x04);
        assert_eq!(
            descs.join("|"),
            "Blocking EEPROM from being read|Power budget exceeded"
        );
        assert!(fetch_generic_error_description(0).is_empty());
    }
}
