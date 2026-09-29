//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! Saying a thing went wrong once, and saying when it came back.
//!
//! Every polling daemon here reads the platform on a timer and has to survive
//! the read failing.  Logging the failure each cycle floods syslog at the
//! polling rate -- on thermalctld that is twenty lines a second -- and logging
//! it only once loses the fact that it recovered, which is the half an operator
//! actually needs.
//!
//! Six copies of the same `let mut read_failed = false` is how the sixth comes
//! to differ from the first, so it is a type.

/// A condition that is worth reporting on its edges and not in between.
#[derive(Debug, Default)]
pub struct Latch {
    failed: bool,
}

impl Latch {
    pub fn new() -> Self {
        Self::default()
    }

    /// Report a failure, if this is the first one.
    ///
    /// Returns whether it was reported, which is what a caller with a more
    /// elaborate message than `{e}` uses.
    pub fn fail(&mut self, message: std::fmt::Arguments<'_>) -> bool {
        if self.failed {
            return false;
        }
        self.failed = true;
        log::error!("{message}");
        true
    }

    /// The same, at WARNING.
    ///
    /// See `fail_notice` for why the level is a parameter and not a house
    /// style: psud logs a PSU it could not update with `log_warning`
    /// (`psud:DaemonPsud.update_psu_data`), and a port that shouted ERROR there
    /// would be read as a harder failure than the Python daemon reports.
    pub fn fail_warn(&mut self, message: std::fmt::Arguments<'_>) -> bool {
        if self.failed {
            return false;
        }
        self.failed = true;
        log::warn!("{message}");
        true
    }

    /// The same, at NOTICE.
    ///
    /// Which level a first failure gets is the Python daemon's choice, not a
    /// house style: stormond's dynamic pass logs its exceptions with
    /// `log_notice`
    /// (`stormond:DaemonStorage.get_dynamic_fields_update_state_db`) while its
    /// static pass uses `log_error`
    /// (`stormond:DaemonStorage.get_static_fields_update_state_db`).  A latch
    /// that could only do ERROR would have this port shouting where Python
    /// murmurs, and LogAnalyzer reads the level.
    pub fn fail_notice(&mut self, message: std::fmt::Arguments<'_>) -> bool {
        if self.failed {
            return false;
        }
        self.failed = true;
        crate::notice!("{message}");
        true
    }

    /// Report recovery, if something had failed.
    pub fn recover(&mut self, message: std::fmt::Arguments<'_>) -> bool {
        if !self.failed {
            return false;
        }
        self.failed = false;
        crate::notice!("{message}");
        true
    }

    /// Whether the condition is currently failing.
    pub fn is_failing(&self) -> bool {
        self.failed
    }
}

/// `latch.fail(...)` with the ergonomics of a log macro.
#[macro_export]
macro_rules! fail_once {
    ($latch:expr, $($arg:tt)*) => {
        $latch.fail(format_args!($($arg)*))
    };
}

/// `latch.fail_warn(...)`, for the points Python logs at WARNING.
#[macro_export]
macro_rules! fail_once_warn {
    ($latch:expr, $($arg:tt)*) => {
        $latch.fail_warn(format_args!($($arg)*))
    };
}

/// `latch.fail_notice(...)`, for the points Python logs at NOTICE.
#[macro_export]
macro_rules! fail_once_notice {
    ($latch:expr, $($arg:tt)*) => {
        $latch.fail_notice(format_args!($($arg)*))
    };
}

/// `latch.recover(...)`, likewise.
#[macro_export]
macro_rules! recovered {
    ($latch:expr, $($arg:tt)*) => {
        $latch.recover(format_args!($($arg)*))
    };
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The whole point: a platform whose i2c tree has gone would otherwise
    /// produce one line per poll for as long as it stays gone.
    #[test]
    fn a_standing_failure_is_reported_once() {
        let mut l = Latch::new();
        assert!(l.fail(format_args!("cannot read")));
        assert!(!l.fail(format_args!("cannot read")));
        assert!(!l.fail(format_args!("cannot read")));
        assert!(l.is_failing());
    }

    /// And the recovery is the other edge.  Reporting only the failure leaves
    /// an operator reading a log that says the switch is broken when it is not.
    #[test]
    fn recovery_is_reported_once_too() {
        let mut l = Latch::new();
        l.fail(format_args!("cannot read"));
        assert!(l.recover(format_args!("recovered")));
        assert!(!l.recover(format_args!("recovered")));
        assert!(!l.is_failing());
    }

    /// All three levels latch together: whichever reports first, the others
    /// stay quiet until a recovery clears it.  A daemon that changed level
    /// mid-outage must not get a second first-failure line out of it.
    #[test]
    fn every_level_shares_one_latch() {
        for first in 0..3 {
            let mut l = Latch::new();
            let armed = match first {
                0 => l.fail(format_args!("x")),
                1 => l.fail_warn(format_args!("x")),
                _ => l.fail_notice(format_args!("x")),
            };
            assert!(armed, "the first report goes out whichever level it is");
            assert!(!l.fail(format_args!("x")));
            assert!(!l.fail_warn(format_args!("x")));
            assert!(!l.fail_notice(format_args!("x")));
            assert!(l.recover(format_args!("back")), "and recovery still clears it");
        }
    }

    /// Nothing failed, so nothing recovered: a daemon that logged "recovered"
    /// on its first successful poll would say it on every start-up.
    #[test]
    fn a_daemon_that_never_failed_never_recovers() {
        let mut l = Latch::new();
        assert!(!l.recover(format_args!("recovered")));
        assert!(!l.is_failing());
    }

    /// Flapping is two edges per cycle, which is the honest report.
    #[test]
    fn a_flapping_condition_reports_both_edges_each_time() {
        let mut l = Latch::new();
        for _ in 0..3 {
            assert!(l.fail(format_args!("gone")));
            assert!(l.recover(format_args!("back")));
        }
    }

    #[test]
    fn the_macros_forward_their_arguments() {
        let mut l = Latch::new();
        let e = "no such device";
        assert!(crate::fail_once!(l, "cannot read sensors: {e}"));
        assert!(!crate::fail_once!(l, "cannot read sensors: {e}"));
        assert!(crate::recovered!(l, "sensor read recovered"));
    }
}
