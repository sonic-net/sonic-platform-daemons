//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! How long to wait before the next cycle.
//!
//! Every polling pmon daemon spells the same three constants -- a short first
//! interval so STATE_DB fills soon after boot, a long steady one, and a
//! threshold past which a slow cycle is worth a warning -- and the same
//! arithmetic between them (`thermalctld:ThermalMonitor.main`,
//! `sensormond:SensorMonitorDaemon.run`, `psud`, `chassisd`).  It is four
//! lines, and four lines copied seven times is how the seventh comes to differ
//! from the first.

/// Seconds to wait after a cycle that took `elapsed`.
///
/// The steady interval absorbs the cycle's own duration, so the period stays
/// put rather than drifting by however long the platform took.  A cycle that
/// overran falls back to `initial` -- *not* to zero and not to `update`: Python
/// reuses its initial interval here, and on a platform whose `update` has been
/// shrunk below `initial` that means the overrunning cycle waits the longer of
/// the two.  Clamping would read better and would diverge.
pub fn next_wait(elapsed: f64, update: f64, initial: f64) -> f64 {
    if elapsed < update {
        update - elapsed
    } else {
        initial
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_quick_cycle_waits_out_the_rest_of_the_period() {
        assert_eq!(next_wait(10.0, 60.0, 5.0), 50.0);
    }

    /// The case the arithmetic exists for: without subtracting the cycle, a
    /// daemon whose poll takes 20s would publish every 80s, not every 60s.
    #[test]
    fn the_period_does_not_drift_by_the_cycles_own_duration() {
        let (update, initial) = (60.0, 5.0);
        let period = |elapsed: f64| elapsed + next_wait(elapsed, update, initial);
        assert_eq!(period(0.5), 60.0);
        assert_eq!(period(20.0), 60.0);
    }

    /// An overrun falls back to `initial` rather than to zero, which is what
    /// keeps a platform that cannot keep up from spinning.
    #[test]
    fn an_overrunning_cycle_falls_back_to_the_initial_interval() {
        assert_eq!(next_wait(60.0, 60.0, 5.0), 5.0);
        assert_eq!(next_wait(900.0, 60.0, 5.0), 5.0);
    }

    /// `platform.json` can shrink the steady interval below the initial one.
    /// Python never shrinks the initial one to match, so the fallback is then
    /// the *longer* wait -- surprising, reproduced deliberately.
    #[test]
    fn the_fallback_is_not_clamped_to_the_update_interval() {
        assert_eq!(next_wait(3.0, 3.0, 5.0), 5.0);
    }
}
