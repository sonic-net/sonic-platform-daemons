//! The STATE_DB formatter: pmon-common's, re-exported.
//!
//! Python's `str()` semantics for floats, bools, thresholds and timestamps
//! are shared, because every ported daemon writes through the same
//! conventions.  Fan direction used to be thermalctld's own, as one of three
//! spellings; it is now published as the platform spelled it, like any other
//! string.

pub use pmon_common::fmt::{
    bool, event_timestamp, float, number, opt_bool, opt_str, position, threshold,
    timestamp, NOT_AVAILABLE,
};
