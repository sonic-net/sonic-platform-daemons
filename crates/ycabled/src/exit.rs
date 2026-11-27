//! How ycabled-rs exits after a failure (HLD 4.3.1, N8).
//!
//! pmon's supervisord starts ycabled with `startsecs=10`: a process that
//! exits sooner has failed to start, and after four such starts in a row
//! supervisord gives up (`FATAL`). A failure soon after a start, such as
//! redis dropping the connection again while it recovers, therefore waits
//! until [`MIN_RUN`] has passed since the start, so that every start counts
//! as successful and supervisord keeps restarting ycabled-rs. A later
//! failure exits at once.

use std::sync::OnceLock;
use std::time::{Duration, Instant};

/// `startsecs`, and a second more as a margin.
pub const MIN_RUN: Duration = Duration::from_secs(11);

/// How long an exit waits for the queued syslog messages to go out.
pub const LOG_FLUSH: Duration = Duration::from_secs(1);

static START: OnceLock<Instant> = OnceLock::new();

/// Takes the start of the process: call it first thing in `main`.
pub fn mark_start() {
    START.get_or_init(Instant::now);
}

/// Exits with `code`, but not before [`MIN_RUN`] has passed since
/// [`mark_start`], and once the syslog messages are out, the failure's
/// included. SIGTERM and SIGINT end the wait: their default action is
/// restored for it.
pub fn after_failure(code: i32) -> ! {
    let left = START.get().map_or(Duration::ZERO, |start| {
        MIN_RUN.saturating_sub(start.elapsed())
    });
    if !left.is_zero() {
        crate::log_notice!(
            "Exiting in {:.1} s, {} s after the start",
            left.as_secs_f64(),
            MIN_RUN.as_secs()
        );
        // SAFETY: only sets the action of two signals, as the process is
        // about to exit.
        unsafe {
            libc::signal(libc::SIGTERM, libc::SIG_DFL);
            libc::signal(libc::SIGINT, libc::SIG_DFL);
        }
        crate::logger::flush(LOG_FLUSH);
        std::thread::sleep(left);
    }
    crate::logger::flush(LOG_FLUSH);
    std::process::exit(code)
}
