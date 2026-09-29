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
#[cfg(any(test, feature = "testing"))]
use std::thread::ThreadId;

use syslog::{Formatter3164, LoggerBackend};

/// Target marking a record as syslog NOTICE.  Not a module path, so it cannot
/// collide with one.
pub const NOTICE: &str = "@notice";

/// `log::info!` at NOTICE severity — the counterpart of Python's `log_notice`.
#[macro_export]
macro_rules! notice {
    ($($arg:tt)+) => { log::info!(target: $crate::logging::NOTICE, $($arg)+) };
}

struct SyslogLogger {
    inner: Mutex<syslog::Logger<LoggerBackend, Formatter3164>>,
}

/// Which syslog severity a record goes out at.
///
/// `log` has no NOTICE and syslog does, and NOTICE is what several of these
/// daemons announce start-up, recovery and shutdown with -- Python's
/// `log_notice`, which an operator greps for and which sonic-mgmt's
/// loganalyzer matches on.  The mapping from a `log` record to a severity is
/// therefore a decision rather than a projection, and it is the one thing in
/// this file that can be wrong without anything failing to build.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Severity {
    Notice,
    Err,
    Warning,
    Info,
}

fn severity_of(target: &str, level: log::Level) -> Severity {
    match (target, level) {
        (NOTICE, _) => Severity::Notice,
        (_, log::Level::Error) => Severity::Err,
        (_, log::Level::Warn) => Severity::Warning,
        _ => Severity::Info,
    }
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
        let _ = match severity_of(record.target(), record.level()) {
            Severity::Notice => w.notice(msg),
            Severity::Err => w.err(msg),
            Severity::Warning => w.warning(msg),
            Severity::Info => w.info(msg),
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
            let logger = SyslogLogger {
                inner: Mutex::new(writer),
            };
            let _ = log::set_boxed_logger(Box::new(logger)).map(|()| log::set_max_level(log::LevelFilter::Info));
        }
        Err(e) => eprintln!("cannot connect to syslog: {e}"),
    }
}

// Everything from here to `capture` is test scaffolding.  Gated so it cannot
// reach a daemon: nothing in a `main()` calls it -- every call site sits
// behind `#[cfg(test)]` -- but "nothing calls it" is a fact somebody has to
// keep checking, and a `pub fn` that installs a process-wide logger is the
// wrong thing to leave reachable in a shipped binary.  A consumer asks for it
// in dev-dependencies: `pmon-common = { path = "..", features = ["testing"] }`.

#[cfg(any(test, feature = "testing"))]
/// A logger that keeps what was logged, for tests.
///
/// Worth having for two reasons beyond reading it back.  The message text is a
/// contract: sonic-mgmt's loganalyzer matches on exact wording, and these
/// daemons' wording was written to match the Python ones they replace -- so a
/// reworded line is a silent regression in somebody else's test run.  And
/// `log::max_level()` is `Off` in a test binary until something sets it, which
/// means every `log::error!` in the crate is skipped *before* its arguments are
/// evaluated: a test can drive a branch and still never execute the formatting
/// in it, and a format argument that panics would not be found until the daemon
/// was on a switch.
///
/// Installed at most once per test binary; `capture()` is idempotent and safe
/// to call from every test that wants it.
struct Captured {
    /// One entry per thread that is capturing, holding what it logged.  A
    /// list rather than a map because it has to be built in a `static`, and
    /// it never holds more than the tests running at once.
    lines: Mutex<Vec<(ThreadId, Lines)>>,
}

#[cfg(any(test, feature = "testing"))]
/// What one thread logged, at the severity each line would reach syslog with.
type Lines = Vec<(log::Level, String)>;

#[cfg(any(test, feature = "testing"))]
impl log::Log for Captured {
    fn enabled(&self, metadata: &log::Metadata) -> bool {
        metadata.level() <= log::Level::Info
    }

    fn log(&self, record: &log::Record) {
        if !self.enabled(record.metadata()) {
            return;
        }
        // The severity mapping is exercised here too, so a NOTICE that stopped
        // being a NOTICE shows up as a failing assertion rather than as a line
        // an operator no longer greps.
        let level = match severity_of(record.target(), record.level()) {
            Severity::Notice | Severity::Info => log::Level::Info,
            Severity::Err => log::Level::Error,
            Severity::Warning => log::Level::Warn,
        };
        // Formatted whether or not anyone keeps it, for the reason above: a
        // format argument that panics should fail whichever test reaches it.
        let line = record.args().to_string();
        let mut lines = self.lines.lock().unwrap_or_else(|p| p.into_inner());
        // A thread that is not capturing is a test that did not ask; its line
        // goes nowhere rather than into another test's answer.
        let me = std::thread::current().id();
        if let Some((_, mine)) = lines.iter_mut().find(|(t, _)| *t == me) {
            mine.push((level, line));
        }
    }

    fn flush(&self) {}
}

#[cfg(any(test, feature = "testing"))]
static CAPTURED: Captured = Captured { lines: Mutex::new(Vec::new()) };

#[cfg(any(test, feature = "testing"))]
/// What one test's thread has logged since it called [`capture`].
///
/// Capturing stops when this is dropped, which is the end of the test.
pub struct CaptureGuard {
    thread: ThreadId,
}

#[cfg(any(test, feature = "testing"))]
impl CaptureGuard {
    fn any(&self, matches: impl Fn(log::Level, &str) -> bool) -> bool {
        CAPTURED
            .lines
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .iter()
            .find(|(t, _)| *t == self.thread)
            .is_some_and(|(_, mine)| mine.iter().any(|(l, m)| matches(*l, m)))
    }

    /// Whether anything this test logged contains this text.
    ///
    /// Only this test: a negative assertion -- `assert!(!log.contains(..))` --
    /// is answerable because no other test's lines are in here.
    pub fn contains(&self, needle: &str) -> bool {
        self.any(|_, m| m.contains(needle))
    }

    /// The same, requiring the severity as well.
    pub fn logged(&self, level: log::Level, needle: &str) -> bool {
        self.any(|l, m| l == level && m.contains(needle))
    }
}

#[cfg(any(test, feature = "testing"))]
impl Drop for CaptureGuard {
    fn drop(&mut self) {
        CAPTURED
            .lines
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .retain(|(t, _)| *t != self.thread);
    }
}

#[cfg(any(test, feature = "testing"))]
/// Install the capturing logger for this test binary, and start keeping what
/// this thread logs.
///
/// Per thread, because that is what a test is: libtest runs each test on its
/// own thread, and `#[tokio::test]`'s runtime drives its tasks on that same
/// thread.  `log::set_logger` takes a process-wide slot, so a single shared
/// buffer answered for every test that happened to be running -- including
/// the ones that never called this -- and `a_recovery_seed_that_will_not_write_stops`
/// failed on a "Start daemon main loop" logged by a test running beside it.
/// Keeping lines per thread also means capturing tests no longer take turns.
///
/// What this does not see is a line logged from a thread the test spawned
/// itself.  No test here asserts on one; a test that needs to would have to
/// log it from its own thread.
///
/// One per test: a second call on the same thread starts the buffer again,
/// and dropping either guard ends capturing for both.
pub fn capture() -> CaptureGuard {
    static ONCE: std::sync::Once = std::sync::Once::new();
    ONCE.call_once(|| {
        if log::set_logger(&CAPTURED).is_ok() {
            log::set_max_level(log::LevelFilter::Info);
        }
    });
    let thread = std::thread::current().id();
    let mut lines = CAPTURED.lines.lock().unwrap_or_else(|p| p.into_inner());
    lines.retain(|(t, _)| *t != thread);
    lines.push((thread, Vec::new()));
    CaptureGuard { thread }
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

    /// NOTICE outranks the record's own level, and everything else follows it.
    ///
    /// The `notice!` macro is `log::info!` with a target, so a mapping that
    /// looked at the level first would send every one of them out at INFO --
    /// which is a severity `journalctl -p notice` does not show and the
    /// loganalyzer rules do not match.
    #[test]
    fn the_notice_target_outranks_the_records_own_level() {
        assert_eq!(severity_of(NOTICE, log::Level::Info), Severity::Notice);
        assert_eq!(severity_of(NOTICE, log::Level::Error), Severity::Notice);
        assert_eq!(severity_of("thermalctld", log::Level::Error), Severity::Err);
        assert_eq!(severity_of("thermalctld", log::Level::Warn), Severity::Warning);
        assert_eq!(severity_of("thermalctld", log::Level::Info), Severity::Info);
        // Below Info the logger is not enabled at all, so whatever this
        // answers never reaches syslog; it must still not be an error.
        assert_eq!(severity_of("thermalctld", log::Level::Debug), Severity::Info);
    }

    /// And the capturing logger sees what the daemons write, at the severity
    /// they wrote it at.
    #[test]
    fn the_capturing_logger_keeps_the_text_and_the_severity() {
        let log = capture();
        log::error!("a thing that failed");
        crate::notice!("a thing worth announcing");
        assert!(log.logged(log::Level::Error, "a thing that failed"));
        assert!(log.contains("a thing worth announcing"));
        assert!(!log.contains("something nobody logged"));
    }

    /// A line another thread logs -- a test that is not capturing, running
    /// beside this one -- is not in this test's answer.
    #[test]
    fn another_threads_line_is_not_captured() {
        let log = capture();
        std::thread::spawn(|| log::error!("logged by a test running alongside"))
            .join()
            .unwrap();
        assert!(!log.contains("logged by a test running alongside"));
    }

    /// Two capturing tests at once each see their own lines and not the
    /// other's -- they no longer wait for one another.
    #[test]
    fn two_capturing_threads_keep_their_own_lines() {
        let log = capture();
        let (ready_tx, ready_rx) = std::sync::mpsc::channel();
        let (done_tx, done_rx) = std::sync::mpsc::channel::<()>();
        let other = std::thread::spawn(move || {
            let theirs = capture();
            log::warn!("the other test's line");
            ready_tx.send(()).unwrap();
            // Still capturing while the first thread logs.
            done_rx.recv().unwrap();
            (theirs.contains("the other test's line"), theirs.contains("this test's line"))
        });
        ready_rx.recv().unwrap();
        log::warn!("this test's line");
        done_tx.send(()).unwrap();

        assert_eq!(other.join().unwrap(), (true, false));
        assert!(log.logged(log::Level::Warn, "this test's line"));
        assert!(!log.contains("the other test's line"));
    }

    /// A finished test stops capturing: its thread's entry goes with it.
    #[test]
    fn a_dropped_capture_keeps_nothing() {
        let thread = std::thread::current().id();
        drop(capture());
        log::error!("after the test was done");
        assert!(!CAPTURED.lines.lock().unwrap().iter().any(|(t, _)| *t == thread));
    }
}
