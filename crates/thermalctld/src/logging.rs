//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! Syslog with a NOTICE level.
//!
//! Python's `sonic_py_common.logger` has eight severities and uses
//! `log_notice` for fourteen of this daemon's messages — a fan drawer coming
//! back, a leak clearing, the polling intervals it settled on.  The `log`
//! crate has five and no NOTICE, so routing those through `log::info!` would
//! publish them a severity below where Python publishes them, and an operator
//! filtering syslog at NOTICE would stop seeing them.
//!
//! A record aimed at NOTICE carries [`NOTICE`] as its target and is routed
//! past the level map; everything else maps as `syslog::BasicLogger` does.

use std::sync::Mutex;

use syslog::{Formatter3164, LoggerBackend};

/// Target marking a record as syslog NOTICE.  Not a module path, so it cannot
/// collide with one.
pub const NOTICE: &str = "@notice";

/// `log::info!` at NOTICE severity — the counterpart of Python's `log_notice`.
macro_rules! notice {
    ($($arg:tt)+) => { log::info!(target: $crate::logging::NOTICE, $($arg)+) };
}
pub(crate) use notice;

struct SyslogLogger {
    inner: Mutex<syslog::Logger<LoggerBackend, Formatter3164>>,
}

impl log::Log for SyslogLogger {
    fn enabled(&self, metadata: &log::Metadata) -> bool {
        metadata.level() <= log::Level::Info
    }

    fn log(&self, record: &log::Record) {
        if !self.enabled(record.metadata()) {
            return;
        }
        // A poisoned lock means another thread panicked mid-write; the panic
        // hook still has to suspend hw-management-tc, so take the lock anyway
        // rather than lose the messages that explain what happened.
        let mut w = match self.inner.lock() {
            Ok(w) => w,
            Err(poisoned) => poisoned.into_inner(),
        };
        let msg = record.args().to_string();
        let _ = match (record.target(), record.level()) {
            (NOTICE, _) => w.notice(msg),
            (_, log::Level::Error) => w.err(msg),
            (_, log::Level::Warn) => w.warning(msg),
            _ => w.info(msg),
        };
    }

    fn flush(&self) {}
}

/// Send `log` records to syslog, honouring [`NOTICE`].
///
/// A syslog that cannot be reached is reported on stderr and left at that:
/// supervisord captures stderr, and a daemon that refused to start because it
/// could not log would be worse than one that runs without logging.
pub fn init(identifier: &str) {
    let formatter = Formatter3164 {
        facility: syslog::Facility::LOG_USER,
        hostname: None,
        process: identifier.into(),
        pid: std::process::id(),
    };

    match syslog::unix(formatter) {
        Ok(writer) => {
            let logger = SyslogLogger { inner: Mutex::new(writer) };
            let _ = log::set_boxed_logger(Box::new(logger))
                .map(|()| log::set_max_level(log::LevelFilter::Info));
        }
        Err(e) => eprintln!("cannot connect to syslog: {e}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The target has to be something no module path can produce, or a stray
    /// `log::info!` from a module called `notice` would be promoted.
    #[test]
    fn the_notice_target_cannot_collide_with_a_module_path() {
        assert!(NOTICE.starts_with('@'));
        assert!(!NOTICE.contains("::"));
    }
}
