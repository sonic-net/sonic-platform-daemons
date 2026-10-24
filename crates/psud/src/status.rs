//! One power entity's health, and when a change in it is worth a log line.
//!
//! Ports `psud:PsuStatus`.  Every field starts *good*: the daemon has not
//! looked yet, and a PSU reported absent on the cycle before anything read it
//! would raise an absence alarm on every boot.

/// Whether one PSU or PDB is present, powered, in voltage range and cool
/// enough, plus the system-wide power-overload latch it carries.
#[derive(Debug, Clone, PartialEq)]
pub struct PsuStatus {
    pub presence: bool,
    pub power_good: bool,
    pub voltage_good: bool,
    pub temperature_good: bool,
    /// Whether the power thresholds are worth evaluating at all: set when a
    /// PSU comes good *and* the platform publishes both thresholds.
    pub check_power_threshold: bool,
    pub power_exceeded_threshold: bool,
}

impl Default for PsuStatus {
    fn default() -> Self {
        Self {
            presence: true,
            power_good: true,
            voltage_good: true,
            temperature_good: true,
            check_power_threshold: false,
            power_exceeded_threshold: false,
        }
    }
}

impl PsuStatus {
    /// Update the flag; true when it changed, which is when the caller logs.
    pub fn set_presence(&mut self, presence: bool) -> bool {
        std::mem::replace(&mut self.presence, presence) != presence
    }

    pub fn set_power_good(&mut self, power_good: bool) -> bool {
        std::mem::replace(&mut self.power_good, power_good) != power_good
    }

    pub fn set_power_exceed_threshold(&mut self, exceeded: bool) -> bool {
        std::mem::replace(&mut self.power_exceeded_threshold, exceeded) != exceeded
    }

    /// Voltage against its band.
    ///
    /// A missing reading or a missing limit is treated as *good*, not as a
    /// breach: without both ends of the band there is nothing to be out of.
    /// Python reports the transition into that state and then returns False
    /// (`psud:PsuStatus.set_voltage`) -- so the LED is not touched and no
    /// "cleared" line is logged, even though the flag just changed.
    /// Reproduced: a PSU whose sensor drops out should not flip the tray LED
    /// green.
    pub fn set_voltage(
        &mut self,
        name: &str,
        voltage: Option<f64>,
        high: Option<f64>,
        low: Option<f64>,
    ) -> bool {
        let (Some(v), Some(hi), Some(lo)) = (voltage, high, low) else {
            if !self.voltage_good {
                log::warn!(
                    "PSU {name} voltage or high_threshold or low_threshold become unavailable, \
                     voltage={}, high_threshold={}, low_threshold={}",
                    pmon_common::fmt::opt_float(voltage),
                    pmon_common::fmt::opt_float(high),
                    pmon_common::fmt::opt_float(low),
                );
                self.voltage_good = true;
            }
            return false;
        };
        let good = lo <= v && v <= hi;
        std::mem::replace(&mut self.voltage_good, good) != good
    }

    /// Temperature against its limit, with the same treatment of a missing one.
    pub fn set_temperature(&mut self, name: &str, temperature: Option<f64>, high: Option<f64>) -> bool {
        let (Some(t), Some(hi)) = (temperature, high) else {
            if !self.temperature_good {
                log::warn!(
                    "PSU {name} temperature or high_threshold become unavailable, \
                     temperature={}, high_threshold={}",
                    pmon_common::fmt::opt_float(temperature),
                    pmon_common::fmt::opt_float(high),
                );
                self.temperature_good = true;
            }
            return false;
        };
        let good = t < hi;
        std::mem::replace(&mut self.temperature_good, good) != good
    }

    /// What decides the PSU's own LED: green only if all four hold.
    pub fn is_ok(&self) -> bool {
        self.presence && self.power_good && self.voltage_good && self.temperature_good
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A fresh status is good on every count.  Starting from `false` would have
    /// every boot log an absence alarm for every healthy PSU, one cycle before
    /// the first read.
    #[test]
    fn nothing_is_wrong_until_something_has_been_read() {
        let s = PsuStatus::default();
        assert!(s.is_ok());
        assert!(!s.check_power_threshold, "and nothing is being watched yet");
    }

    #[test]
    fn a_change_is_reported_once_not_once_per_poll() {
        let mut s = PsuStatus::default();
        assert!(s.set_presence(false), "the removal");
        assert!(!s.set_presence(false), "still gone, still quiet");
        assert!(s.set_presence(true), "the insertion is the other event");
    }

    #[test]
    fn a_voltage_inside_the_band_is_good_and_either_edge_is_not() {
        let mut s = PsuStatus::default();
        assert!(!s.set_voltage("PSU 1", Some(12.0), Some(13.0), Some(11.0)));
        assert!(s.is_ok());
        assert!(s.set_voltage("PSU 1", Some(14.0), Some(13.0), Some(11.0)));
        assert!(!s.voltage_good);
        assert!(s.set_voltage("PSU 1", Some(12.0), Some(13.0), Some(11.0)));
        assert!(s.voltage_good);
        assert!(s.set_voltage("PSU 1", Some(10.0), Some(13.0), Some(11.0)));
        assert!(!s.voltage_good);
    }

    /// The band is inclusive: a PSU sitting exactly on its published limit is
    /// at the limit, not past it.
    #[test]
    fn the_band_includes_its_own_edges() {
        let mut s = PsuStatus::default();
        assert!(!s.set_voltage("PSU 1", Some(13.0), Some(13.0), Some(11.0)));
        assert!(s.voltage_good);
        assert!(!s.set_voltage("PSU 1", Some(11.0), Some(13.0), Some(11.0)));
        assert!(s.voltage_good);
    }

    /// Temperature is the other way round: `<` and not `<=`, so a PSU sitting
    /// on its limit is already too hot.  Python's two comparisons differ and
    /// the difference is visible in syslog.
    #[test]
    fn a_temperature_at_its_limit_is_already_too_hot() {
        let mut s = PsuStatus::default();
        assert!(s.set_temperature("PSU 1", Some(60.0), Some(60.0)));
        assert!(!s.temperature_good);
    }

    /// The reading vanishes while the PSU is in breach.  The flag goes back to
    /// good -- but the call reports *no change*, so the caller neither logs a
    /// "cleared" line nor turns the LED green.  Both halves are load-bearing.
    #[test]
    fn a_vanished_reading_clears_the_flag_without_reporting_a_recovery() {
        let mut s = PsuStatus::default();
        s.set_voltage("PSU 1", Some(14.0), Some(13.0), Some(11.0));
        assert!(!s.voltage_good);
        assert!(!s.set_voltage("PSU 1", None, Some(13.0), Some(11.0)), "no recovery is claimed");
        assert!(s.voltage_good, "but nothing is being reported as wrong either");

        let mut s = PsuStatus::default();
        s.set_temperature("PSU 1", Some(80.0), Some(60.0));
        assert!(!s.set_temperature("PSU 1", Some(80.0), None));
        assert!(s.temperature_good);
    }

    #[test]
    fn any_one_condition_decides_the_led() {
        for break_one in 0..4 {
            let mut s = PsuStatus::default();
            match break_one {
                0 => s.presence = false,
                1 => s.power_good = false,
                2 => s.voltage_good = false,
                _ => s.temperature_good = false,
            }
            assert!(!s.is_ok(), "condition {break_one} should redden the LED");
        }
    }
}
