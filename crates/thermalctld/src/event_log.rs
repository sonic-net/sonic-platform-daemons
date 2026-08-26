//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! The BMC event log.  Ports `EventLogger` from `thermalctld:41-90`.
//!
//! Every call goes to syslog.  On a Switch-BMC it is *also* appended to
//! `/host/bmc/event.log`; everywhere else that tee is skipped, because the file
//! is only created on the BMC and opening it elsewhere would log a failure
//! every cycle.  Requirement 1d is both halves of that: the same events on the
//! platforms where Python writes them, and nothing anywhere else.

use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};

/// Python's `CRITICAL_EVENT_LOG_FILE`.
pub const CRITICAL_EVENT_LOG_FILE: &str = "/host/bmc/event.log";

/// Somewhere to send an event.  A trait so the leak state machine can be tested
/// without a filesystem.
pub trait EventLogger {
    fn error(&mut self, msg: &str);
    fn notice(&mut self, msg: &str);
}

/// Tees to syslog and, on the BMC, to the event log file.
pub struct BmcEventLogger {
    /// `None` off the BMC, where only syslog is written.
    file: Option<PathBuf>,
}

impl BmcEventLogger {
    /// `is_switch_bmc` decides whether the file is written at all; the caller
    /// reads it from `platform_env.conf`.
    pub fn new(is_switch_bmc: bool) -> Self {
        Self::with_path(is_switch_bmc, Path::new(CRITICAL_EVENT_LOG_FILE))
    }

    pub fn with_path(is_switch_bmc: bool, path: &Path) -> Self {
        Self { file: is_switch_bmc.then(|| path.to_path_buf()) }
    }

    fn tee(&self, level: &str, msg: &str) {
        let Some(path) = self.file.as_ref() else {
            return;
        };
        let line = format!(
            "{} thermalctld[{}] {level}: {msg}\n",
            crate::fmt::event_timestamp(),
            std::process::id()
        );
        match OpenOptions::new().create(true).append(true).open(path) {
            Ok(mut f) => {
                if let Err(e) = f.write_all(line.as_bytes()) {
                    log::error!("EventLogger: failed to write {}: {e}", path.display());
                }
            }
            Err(e) => log::error!(
                "EventLogger: failed to open log file {} ({e}); events will go to syslog only",
                path.display()
            ),
        }
    }
}

impl EventLogger for BmcEventLogger {
    fn error(&mut self, msg: &str) {
        log::error!("{msg}");
        self.tee("ERROR", msg);
    }

    fn notice(&mut self, msg: &str) {
        crate::logging::notice!("{msg}");
        self.tee("NOTICE", msg);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn off_the_bmc_no_file_is_created() {
        let d = tempfile::tempdir().unwrap();
        let path = d.path().join("event.log");
        let mut ev = BmcEventLogger::with_path(false, &path);
        ev.error("boom");
        ev.notice("fine");
        assert!(!path.exists(), "the tee must not run off the BMC");
    }

    #[test]
    fn on_the_bmc_events_are_appended() {
        let d = tempfile::tempdir().unwrap();
        let path = d.path().join("event.log");
        let mut ev = BmcEventLogger::with_path(true, &path);
        ev.error("first");
        ev.notice("second");
        let got = std::fs::read_to_string(&path).unwrap();
        assert_eq!(got.lines().count(), 2, "appended, not truncated");
        assert!(got.contains("ERROR: first"));
        assert!(got.contains("NOTICE: second"));
    }

    #[test]
    fn an_unwritable_path_does_not_panic() {
        let mut ev = BmcEventLogger::with_path(true, Path::new("/nonexistent/dir/event.log"));
        ev.error("boom");
    }

    /// A log file that cannot be opened is reported and the event still reaches
    /// syslog: the BMC event log is a convenience for reading after the fact,
    /// and losing it must not lose the event.
    #[test]
    fn an_unwritable_log_file_does_not_lose_the_event() {
        // A path whose parent is a file, so create() cannot succeed.
        let d = tempfile::tempdir().unwrap();
        let blocker = d.path().join("blocker");
        std::fs::write(&blocker, "").unwrap();
        let mut log = BmcEventLogger::with_path(true, &blocker.join("event.log"));

        log.error("critical thermal breach");
        log.notice("recovered");
        // Nothing was written, and nothing panicked.
        assert!(!blocker.join("event.log").exists());
    }
}
