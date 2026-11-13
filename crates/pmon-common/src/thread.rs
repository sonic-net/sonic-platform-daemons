//! What a panic on one of a daemon's own threads does to the daemon.
//!
//! The workspace unwinds on panic (`[profile.release]`), so left alone a panic
//! ends only the thread it happens on.  For none of the threads these daemons
//! start is that the right answer, so each one says which of these it wants:
//!
//! - [`abort_on_panic`]: the thread is half of the daemon, and a daemon that
//!   carried on without it would look healthy while doing half its job --
//!   nothing in pmon would notice, its `critical_processes` is empty.  The
//!   panic takes the whole process down instead, and supervisord starts it
//!   again (`autorestart=unexpected`).
//! - [`log_on_panic`]: the thread does one job for one caller, and the daemon
//!   is whole without it.  The panic is logged and the thread ends, which is
//!   what an exception does on a Python `threading.Thread`.
//!
//! Nothing can make that choice per thread under `panic = "abort"`: there a
//! panic ends the process before anything can catch it, and cargo takes the
//! panic strategy per build, not per crate.

use std::any::Any;
use std::panic::{self, AssertUnwindSafe};

/// Run `f`, and abort the process if it panics.
///
/// `thread` names the thread in the log line, which is all that reaches syslog
/// before the process is gone.  The process aborts rather than exits: no
/// destructor and no exit handler runs on the way out, as under the abort
/// strategy, so a daemon half torn down cannot write anything, and it is the
/// restarted one that puts its tables right.
pub fn abort_on_panic<R>(thread: &str, f: impl FnOnce() -> R) -> R {
    // Unwind safety does not matter here: if `f` panics, nothing that saw its
    // state survives the abort below.
    match panic::catch_unwind(AssertUnwindSafe(f)) {
        Ok(r) => r,
        Err(payload) => {
            log::error!("{thread} panicked: {}; aborting", message(&*payload));
            std::process::abort()
        }
    }
}

/// Run `f`, and log it if it panics.
///
/// `None` is the panic; the caller is a thread that ends either way.  Whatever
/// `f` had borrowed or locked is released as it unwinds, and a lock it held is
/// left poisoned for the next thread to see.
pub fn log_on_panic<R>(thread: &str, f: impl FnOnce() -> R) -> Option<R> {
    // `f` belongs to the thread that is about to end; what it shares with the
    // rest of the daemon is behind locks, which poisoning reports.
    match panic::catch_unwind(AssertUnwindSafe(f)) {
        Ok(r) => Some(r),
        Err(payload) => {
            log::error!("{thread} panicked: {}", message(&*payload));
            None
        }
    }
}

/// The text a panic was raised with: `panic!("literal")` carries a `&str`,
/// `panic!("{x}")` a `String`, and `panic_any` anything at all.
fn message(payload: &(dyn Any + Send)) -> &str {
    payload
        .downcast_ref::<&str>()
        .copied()
        .or_else(|| payload.downcast_ref::<String>().map(String::as_str))
        .unwrap_or("a panic with no message")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::logging;

    #[test]
    fn a_closure_that_returns_is_passed_through() {
        assert_eq!(abort_on_panic("t", || 7), 7);
        assert_eq!(log_on_panic("t", || 7), Some(7));
    }

    #[test]
    fn a_panic_is_logged_and_the_thread_carries_on() {
        let log = logging::capture();
        let r = log_on_panic("admin-state DPU0", || -> u8 { panic!("lost {}", "DPU0") });
        assert_eq!(r, None);
        assert!(log.logged(log::Level::Error, "admin-state DPU0 panicked: lost DPU0"));
    }

    #[test]
    fn the_message_is_found_whatever_the_panic_carried() {
        assert_eq!(message(&"literal"), "literal");
        assert_eq!(message(&String::from("formatted")), "formatted");
        assert_eq!(message(&42_u32), "a panic with no message");
    }

    /// Set in the child [`a_panic_takes_the_process_down`] starts.
    const CHILD: &str = "PMON_COMMON_ABORT_ON_PANIC_CHILD";

    /// The abort can only be seen from outside, so the test runs itself again
    /// with [`CHILD`] set, and the child is the one that panics.
    #[test]
    fn a_panic_takes_the_process_down() {
        use std::os::unix::process::ExitStatusExt;

        if std::env::var_os(CHILD).is_some() {
            abort_on_panic("leak-updater", || panic!("on purpose"));
            // Reached only if the abort did not happen; the parent sees a
            // normal exit and fails.
            return;
        }
        let out = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "thread::tests::a_panic_takes_the_process_down",
                "--nocapture",
            ])
            .env(CHILD, "1")
            .output()
            .unwrap();
        // SIGABRT, which supervisord reads as an unexpected exit and restarts.
        assert_eq!(out.status.signal(), Some(6), "{out:?}");
    }
}
