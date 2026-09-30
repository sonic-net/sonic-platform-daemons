//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! The two STATE_DB formatters that are this daemon's own.
//!
//! Everything else -- Python's `str()` semantics for floats, bools, thresholds
//! and timestamps -- is shared, because every ported daemon writes through the
//! same conventions.  Only fan direction and LED colour are thermalctld's.

pub use pmon_common::fmt::{
    bool, event_timestamp, float, opt_bool, opt_float, opt_str, opt_u32, position, threshold,
    timestamp, NOT_AVAILABLE,
};

use platform_api::FanDirection;

pub trait LedName {
    fn led_name(&self) -> &str;
}

impl LedName for platform_api::LedColor {
    fn led_name(&self) -> &str {
        self.as_str()
    }
}

/// Format fan direction as SONiC platform API expects it: `"intake"`, `"exhaust"`, `"N/A"`.
///
/// The platform base class defines `FAN_DIRECTION_INTAKE = "intake"` (lowercase),
/// so we match that convention.
pub fn direction(d: Option<FanDirection>) -> String {
    match d {
        // FAN_DIRECTION_NOT_APPLICABLE is the string 'N/A', so a fan that
        // answers it has answered -- it is a value, not a missing reading, and
        // it happens to spell the same as the sentinel for one.
        Some(d) => d.as_str().to_string(),
        None => NOT_AVAILABLE.to_string(),
    }
}

/// Format an optional LED colour; `None` -> `"N/A"`.
///
/// The colour crosses as one of the four the base class declares rather than
/// as a free string, so this is the one place that turns it back into the
/// spelling STATE_DB carries.
pub fn led<T: LedName>(v: &Option<T>) -> String {
    match v {
        Some(c) => c.led_name().to_string(),
        None => NOT_AVAILABLE.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn direction_strings_match_sonic_convention() {
        assert_eq!(direction(Some(FanDirection::Intake)), "intake");
        assert_eq!(direction(Some(FanDirection::Exhaust)), "exhaust");
        assert_eq!(direction(None), "N/A");
    }

    /// `FAN_DIRECTION_NOT_APPLICABLE` is literally the string `N/A`, so a fan
    /// that answers it renders the same as one that did not answer.  That is
    /// the platform API's spelling, not a collision this module introduced.
    #[test]
    fn a_fan_that_says_not_applicable_reads_the_same_as_one_that_said_nothing() {
        assert_eq!(direction(Some(FanDirection::NA)), NOT_AVAILABLE);
    }

    #[test]
    fn a_colour_crosses_as_the_spelling_state_db_carries() {
        assert_eq!(led(&Some(platform_api::LedColor::Green)), "green");
        assert_eq!(led::<platform_api::LedColor>(&None), "N/A");
    }
}
