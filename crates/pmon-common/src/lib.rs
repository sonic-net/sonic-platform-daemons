//! What every pmon daemon needs and none of them should own a copy of.
//!
//! Every daemon in this workspace opens STATE_DB tables, logs to syslog under
//! its own identifier, and shuts down on SIGTERM.  Written once per daemon
//! that is a copy of the same hundred lines in each, and one more place per
//! daemon for the next syslog-identifier bug to hide; written here, it is one.
//!
//! What a daemon keeps for itself is what only it has: thermalctld's `db`, for
//! instance, is its table set built on [`db`], not a second implementation.

pub mod cadence;
pub mod cycles;
pub mod db;
pub mod fmt;
pub mod logging;
pub mod platform_env;
pub mod report_once;
