//! Crate error type — folds shared platform API and STATE_DB failures into one enum
//! and preserves the Python sentinels the posters branch on.
//!
//! Mirrors `xcvrd`'s error surface (analysis §3.5): `post_port_sfp_info_to_db`
//! returns `SFP_EEPROM_NOT_READY (-2)` / `PHYSICAL_PORT_NOT_EXIST (-1)`, and several
//! posters `sys.exit(NOT_IMPLEMENTED_ERROR=3)` on `NotImplementedError`.
//!
//! The daemon logs+continues per port (Python per-port `try/except`).

use std::fmt;

/// The result alias used throughout the daemon logic + trait seams.
pub type Result<T> = std::result::Result<T, XcvrdError>;

/// Every fallible daemon operation surfaces one of these.
#[derive(Debug)]
pub enum XcvrdError {
    /// A shared platform API failure.
    Bridge(String),
    /// A STATE_DB / `swss-common` failure.
    Db(String),
    /// Python `NotImplementedError` → `sys.exit(NOT_IMPLEMENTED_ERROR=3)`.
    NotImplemented,
    /// `post_port_sfp_info_to_db` sentinel `SFP_EEPROM_NOT_READY (-2)`.
    EepromNotReady,
    /// `PHYSICAL_PORT_NOT_EXIST (-1)`.
    PhysicalPortNotExist,
    /// Anything else, with context.
    Other(String),
}

impl fmt::Display for XcvrdError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            XcvrdError::Bridge(m) => write!(f, "platform API error: {m}"),
            XcvrdError::Db(m) => write!(f, "state-db error: {m}"),
            XcvrdError::NotImplemented => {
                write!(f, "functionality not implemented for this platform")
            }
            XcvrdError::EepromNotReady => write!(f, "sfp eeprom not ready"),
            XcvrdError::PhysicalPortNotExist => write!(f, "physical port does not exist"),
            XcvrdError::Other(m) => write!(f, "{m}"),
        }
    }
}

impl std::error::Error for XcvrdError {}

impl From<platform_api::PlatformError> for XcvrdError {
    fn from(e: platform_api::PlatformError) -> Self {
        XcvrdError::Bridge(e.to_string())
    }
}

impl From<swss_common::Exception> for XcvrdError {
    fn from(e: swss_common::Exception) -> Self {
        XcvrdError::Db(e.to_string())
    }
}
