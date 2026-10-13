//! What ends a daemon's cycle: the next tick, or a signal.
//!
//! Every daemon here is the same shape -- wait, do a pass, repeat until told to
//! stop -- and each of them spelled the same `tokio::select!` over a sleep and
//! three signal handlers.  A copy per daemon is a chance per daemon to get the
//! exit code wrong, and supervisord reads the exit code.
//!
//! It is also the seam that makes a daemon's loop testable at all: with the
//! ticks coming from here, a test can hand a loop three of them and an exit and
//! watch what it writes, instead of the loop being unreachable inside `main`.

use std::time::Duration;

/// The result of waiting for the next cycle.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tick {
    /// Do another pass.
    Cycle,
    /// Stop, and exit with this code.
    Exit(i32),
}

/// Where a daemon's cycles come from.
pub enum Cycles {
    /// A timer and the three signals, which is what runs on a switch.
    Signals(Box<Signals>),
    /// A fixed number of cycles and then an exit, which is what runs in a test.
    ///
    /// Behind a feature so it cannot be reached by accident from a daemon: the
    /// seam is deliberate, but a daemon that constructed one would quietly stop
    /// after N passes on real hardware.
    #[cfg(any(test, feature = "testing"))]
    Fixed { remaining: usize, code: i32 },
}

/// The three handlers a pmon daemon installs.
///
/// SIGTERM and SIGINT stop it; SIGHUP is absorbed, because it is what a log
/// rotation sends and dying on one would blank whatever table the daemon owns.
pub struct Signals {
    sigterm: tokio::signal::unix::Signal,
    sigint: tokio::signal::unix::Signal,
    sighup: tokio::signal::unix::Signal,
}

/// 128 + the signal number, which is what a shell reports and what
/// supervisord's `autorestart=unexpected` is configured against.
pub fn exit_code_for(signal: tokio::signal::unix::SignalKind) -> i32 {
    128 + signal.as_raw_value()
}

impl Cycles {
    /// Install the handlers.  Failing to is fatal and says so: a daemon that
    /// cannot be stopped cleanly would be SIGKILLed on every image upgrade,
    /// losing the teardown that removes its STATE_DB rows.
    pub fn signals() -> std::io::Result<Self> {
        use tokio::signal::unix::{signal, SignalKind};
        Ok(Cycles::Signals(Box::new(Signals {
            sigterm: signal(SignalKind::terminate())?,
            sigint: signal(SignalKind::interrupt())?,
            sighup: signal(SignalKind::hangup())?,
        })))
    }

    /// Wait out `period`, unless a signal arrives first.
    pub async fn next(&mut self, period: Duration) -> Tick {
        match self {
            Cycles::Signals(s) => s.next(period).await,
            #[cfg(any(test, feature = "testing"))]
            Cycles::Fixed { remaining, code } => {
                // Yields, as the real one does: a loop driven by a tick that
                // never awaits would starve everything else on the runtime,
                // and a test of two interleaved tasks would not interleave.
                tokio::task::yield_now().await;
                if *remaining == 0 {
                    return Tick::Exit(*code);
                }
                *remaining -= 1;
                Tick::Cycle
            }
        }
    }
}

impl Signals {
    async fn next(&mut self, period: Duration) -> Tick {
        use tokio::signal::unix::SignalKind;
        // One deadline for the whole wait, made before the loop rather than in
        // it.  A sleep built inside the `select!` starts the full period again
        // every time a SIGHUP is absorbed, so a log rotation landing late in
        // the wait stretched that cycle to nearly twice its length -- and a
        // SIGHUP every few seconds would have stopped the daemon polling at
        // all.  Python waits on `threading.Event.wait(timeout)`, which a
        // signal handler that returns does not restart: the wait resumes with
        // whatever time was left.
        let deadline = tokio::time::sleep(period);
        tokio::pin!(deadline);
        loop {
            tokio::select! {
                _ = &mut deadline => return Tick::Cycle,
                _ = self.sigterm.recv() => {
                    log::info!("Caught signal 'SIGTERM' - exiting...");
                    return Tick::Exit(exit_code_for(SignalKind::terminate()));
                }
                _ = self.sigint.recv() => {
                    log::info!("Caught signal 'SIGINT' - exiting...");
                    return Tick::Exit(exit_code_for(SignalKind::interrupt()));
                }
                // Absorbed, and the same wait carries on: returning Cycle here
                // would let a log rotation shorten the poll period, and
                // starting a fresh one would let it lengthen it.
                _ = self.sighup.recv() => {
                    log::info!("Caught signal 'SIGHUP' - ignoring...");
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::signal::unix::SignalKind;

    /// supervisord's `autorestart=unexpected` is configured against these, so
    /// they are not free to change.
    #[test]
    fn the_exit_code_is_the_one_a_shell_reports() {
        assert_eq!(exit_code_for(SignalKind::terminate()), 143);
        assert_eq!(exit_code_for(SignalKind::interrupt()), 130);
    }

    #[tokio::test]
    async fn a_fixed_source_yields_its_cycles_then_exits() {
        let mut c = Cycles::Fixed { remaining: 2, code: 0 };
        assert_eq!(c.next(Duration::ZERO).await, Tick::Cycle);
        assert_eq!(c.next(Duration::ZERO).await, Tick::Cycle);
        assert_eq!(c.next(Duration::ZERO).await, Tick::Exit(0));
        assert_eq!(c.next(Duration::ZERO).await, Tick::Exit(0), "and stays exited");
    }

    #[tokio::test]
    async fn a_timer_cycle_waits_out_the_period() {
        tokio::time::pause();
        let mut c = Cycles::signals().expect("handlers install");
        let start = tokio::time::Instant::now();
        assert_eq!(c.next(Duration::from_secs(60)).await, Tick::Cycle);
        // Paused time advances in the timer's own granularity, so the wait
        // lands on or just past the period rather than exactly on it.
        assert!(start.elapsed() >= Duration::from_secs(60));
        assert!(start.elapsed() < Duration::from_secs(61));
    }

    /// Send `sig` to this test process.
    ///
    /// Through the shell's `kill` rather than libc, which this crate does not
    /// depend on.  The handler is tokio's and is installed before any caller
    /// sends, so the signal's default action -- ending the test binary -- never
    /// applies.
    fn raise(sig: &str) {
        let status = std::process::Command::new("sh")
            .args(["-c", &format!("kill -{sig} {}", std::process::id())])
            .status()
            .expect("sh runs");
        assert!(status.success(), "kill -{sig} failed");
        // The handler runs asynchronously to `kill` returning; give it the
        // moment it needs to note the signal before the runtime next parks.
        // Real time, and short: the clock the assertions read is paused.
        std::thread::sleep(Duration::from_millis(50));
    }

    /// An absorbed SIGHUP does not start the period again.
    ///
    /// The wait is 60 s; the SIGHUP lands 40 s in.  Python's
    /// `threading.Event.wait(timeout)` resumes with the 20 s that were left,
    /// and so must this -- a fresh sleep per loop pass would make this cycle
    /// 100 s long, and a SIGHUP every few seconds would mean no cycle at all.
    #[tokio::test]
    async fn a_sighup_mid_wait_does_not_restart_the_period() {
        tokio::time::pause();
        let log = crate::logging::capture();
        let mut c = Cycles::signals().expect("handlers install");
        let start = tokio::time::Instant::now();

        let wait = c.next(Duration::from_secs(60));
        tokio::pin!(wait);
        tokio::select! {
            t = &mut wait => panic!("the wait ended early, with {t:?}"),
            _ = tokio::time::sleep(Duration::from_secs(40)) => {}
        }
        raise("HUP");
        // A second, nearer timer, so the paused clock's next jump stops short
        // of the 60 s deadline: the SIGHUP is then taken while the wait is
        // still in progress, rather than in the same poll as the deadline,
        // where `select!`'s random branch order could pick either.
        tokio::select! {
            t = &mut wait => panic!("the wait ended early, with {t:?}"),
            _ = tokio::time::sleep(Duration::from_secs(1)) => {}
        }
        assert!(
            log.contains("Caught signal 'SIGHUP' - ignoring..."),
            "the SIGHUP has to have been seen for this test to say anything"
        );

        assert_eq!(wait.await, Tick::Cycle, "a SIGHUP is not a reason to stop");
        assert!(start.elapsed() >= Duration::from_secs(60));
        assert!(
            start.elapsed() < Duration::from_secs(61),
            "the period ran from the start of the wait, not from the SIGHUP: {:?}",
            start.elapsed()
        );
    }
}
