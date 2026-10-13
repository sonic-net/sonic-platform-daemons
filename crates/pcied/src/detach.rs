//! Which DPUs are on their way off the bus.
//!
//! Ports `pcied:DaemonPcied.is_dpu_in_detaching_mode`.  A SmartSwitch detaches
//! a DPU by writing PCIE_DETACH_INFO and then removing it from the bus, so for
//! the length of the operation the device is both absent and expected to be --
//! and a daemon that reported it missing would put the switch into FAILED for
//! a planned event.

use std::collections::BTreeSet;

use pmon_common::db::TableLike;

const BUS_INFO_FIELD: &str = "bus_info";
const DPU_STATE_FIELD: &str = "dpu_state";
const DETACHING: &str = "detaching";

/// The `bus_info` values currently in the detaching state.
#[derive(Debug, Default)]
pub struct Detaching(BTreeSet<String>);

impl Detaching {
    /// The devices detaching now, read once per pass rather than once per
    /// absent device.
    ///
    /// Python queries the table inside the loop
    /// (`pcied:DaemonPcied.check_pcie_devices`), so a chassis with several
    /// missing devices scans it once each; the answer is the same for every
    /// device in a pass, and a pass is a minute apart.
    ///
    /// An unreadable table is an error, never an empty set.  Python reads it
    /// unwrapped (`pcied:DaemonPcied.is_dpu_in_detaching_mode`), so a redis it
    /// cannot reach ends the daemon; read as "nothing is detaching" it would
    /// have this daemon report a DPU's PCIe device missing while it is being
    /// detached on purpose.
    pub fn read(is_smartswitch: bool, table: &dyn TableLike) -> Result<Self, String> {
        // Nothing but a SmartSwitch has DPUs, and Python gates on exactly this
        // before it looks (`pcied:DaemonPcied.check_pcie_devices`).
        if !is_smartswitch {
            return Ok(Self::default());
        }
        let mut out = BTreeSet::new();
        for key in table.get_keys()? {
            let Some(row) = table.get(&key)? else { continue };
            let field = |name: &str| {
                row.iter().find(|(k, _)| k == name).map(|(_, v)| v.as_str())
            };
            if field(DPU_STATE_FIELD) == Some(DETACHING) {
                if let Some(bus) = field(BUS_INFO_FIELD) {
                    out.insert(bus.to_string());
                }
            }
        }
        Ok(Self(out))
    }

    pub fn contains(&self, bus_info: &str) -> bool {
        self.0.contains(bus_info)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use pmon_common::db::MockTable;

    fn table() -> MockTable {
        let t = MockTable::new();
        t.set(
            "DPU0",
            &[
                (BUS_INFO_FIELD, "0000:3b:00.0".to_string()),
                (DPU_STATE_FIELD, DETACHING.to_string()),
            ],
        )
        .unwrap();
        t.set(
            "DPU1",
            &[
                (BUS_INFO_FIELD, "0000:3c:00.0".to_string()),
                (DPU_STATE_FIELD, "online".to_string()),
            ],
        )
        .unwrap();
        t
    }

    #[test]
    fn only_the_rows_that_say_detaching_count() {
        let d = Detaching::read(true, &table()).unwrap();
        assert!(d.contains("0000:3b:00.0"));
        assert!(!d.contains("0000:3c:00.0"), "an online DPU that is missing is still missing");
    }

    /// The table exists on a SmartSwitch and nowhere else; reading it anyway
    /// would be a scan per cycle on every fixed platform in the fleet.
    #[test]
    fn a_platform_without_dpus_does_not_read_the_table() {
        let t = table();
        let d = Detaching::read(false, &t).unwrap();
        assert!(!d.contains("0000:3b:00.0"));
        assert_eq!(t.scans(), 0);
    }

    /// One read per pass, however many devices are missing.
    #[test]
    fn the_table_is_read_once_per_pass() {
        let t = table();
        let d = Detaching::read(true, &t).unwrap();
        assert_eq!(t.scans(), 1);
        for _ in 0..10 {
            d.contains("0000:3b:00.0");
        }
        assert_eq!(t.scans(), 1);
    }

    /// A half-written row is not a detach.  Treating a missing `bus_info` as a
    /// match would silence the warning for every device at once.
    #[test]
    fn a_row_without_a_bus_info_matches_nothing() {
        let t = MockTable::new();
        t.set("DPU0", &[(DPU_STATE_FIELD, DETACHING.to_string())]).unwrap();
        let d = Detaching::read(true, &t).unwrap();
        assert!(!d.contains(""));
        assert!(!d.contains("0000:3b:00.0"));
    }
}
