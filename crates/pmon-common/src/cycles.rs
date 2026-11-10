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
//!
//! And it is where a stop reaches the platform.  A vendor may install its own
//! SIGTERM/SIGINT handler while its chassis is built, so that its readiness
//! waits abort on shutdown -- mlnx's `utils.watch_shutdown_signals()`, which
//! ends the 300 s wait SPC1's thermal manager sits in at start-up.  In a
//! Python daemon that handler runs when the signal arrives, before the
//! daemon's own.  Here the daemon owns the signals, so it hands the shutdown
//! on itself ([`notify_shutdown`]) before it joins anything that might be
//! sitting in such a wait.

use std::time::Duration;

use platform_api::PlatformApi;

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
    #[cfg(any(test, feature = "testing"))]
    Paced { remaining: usize, code: i32 },
}

/// The three handlers a pmon daemon installs.
///
/// SIGTERM and SIGINT stop it; SIGHUP is absorbed, because it is what a log
/// rotation sends and dying on one would blank whatever table the daemon owns.
struct SignalSources {
    sigterm: tokio::signal::unix::Signal,
    sigint: tokio::signal::unix::Signal,
    sighup: tokio::signal::unix::Signal,
    log: SignalLog,
}

pub struct Signals {
    receiver: tokio::sync::watch::Receiver<Option<i32>>,
    stop: tokio::sync::watch::Sender<bool>,
    worker: Option<std::thread::JoinHandle<()>>,
}
impl Drop for Signals {
    fn drop(&mut self) {
        let _ = self.stop.send(true);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}
impl Signals {
    async fn next(&mut self, period: Duration) -> Tick {
        if let Some(code) = *self.receiver.borrow() {
            return Tick::Exit(code);
        }
        tokio::select! {
            _ = self.receiver.changed() => Tick::Exit(self.receiver.borrow().unwrap_or(1)),
            _ = tokio::time::sleep(period) => Tick::Cycle,
        }
    }
}

/// How a daemon says it caught a signal: its Python daemon's own
/// `signal_handler`, whose words and severity are not the same in all of them.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SignalLog {
    /// `log_info("Caught signal '<name>' - exiting...")`, `- ignoring...` for
    /// SIGHUP: every daemon's but stormond's (chassisd's adds the exit code).
    /// Logged through `DaemonBase`, which drops INFO, so nothing reaches
    /// syslog -- here neither; see `logging`.
    Info,
    /// stormond's: `log_notice("Caught signal '<name>'")` for SIGTERM and
    /// SIGINT, and `log_notice("Caught signal 'SIGHUP' - ignoring...")`.  The
    /// daemon says `Exiting with <name>` itself, once it has synced.
    Notice,
}

/// 128 + the signal number, which is what a shell reports and what
/// supervisord's `autorestart=unexpected` is configured against.
pub fn exit_code_for(signal: tokio::signal::unix::SignalKind) -> i32 {
    128 + signal.as_raw_value()
}

/// The signal an exit code from [`Tick::Exit`] stands for, by the name Python
/// prints it with -- `None` for a code no signal produced.
pub fn signal_name(code: i32) -> Option<&'static str> {
    use tokio::signal::unix::SignalKind;
    if code == exit_code_for(SignalKind::terminate()) {
        Some("SIGTERM")
    } else if code == exit_code_for(SignalKind::interrupt()) {
        Some("SIGINT")
    } else {
        None
    }
}

impl Cycles {
    /// Install the handlers.  Failing to is fatal and says so: a daemon that
    /// cannot be stopped cleanly would be SIGKILLed on every image upgrade,
    /// losing the teardown that removes its STATE_DB rows.
    pub fn signals() -> std::io::Result<Self> {
        Self::signals_logged(SignalLog::Info)
    }

    /// The same, announcing the signals as `log` says.
    pub fn signals_logged(log: SignalLog) -> std::io::Result<Self> {
        Self::signals_with_shutdown(log, None)
    }

    pub fn signals_with_shutdown(
        log: SignalLog,
        shutdown: Option<(
            std::sync::Arc<tokio::sync::watch::Sender<bool>>,
            std::sync::Arc<std::sync::atomic::AtomicI32>,
        )>,
    ) -> std::io::Result<Self> {
        let (sender, receiver) = tokio::sync::watch::channel(None);
        let (stop, mut stop_rx) = tokio::sync::watch::channel(false);
        let (ready_tx, ready_rx) = std::sync::mpsc::sync_channel(1);
        let worker = std::thread::Builder::new()
            .name("pmon-signals".into())
            .spawn(move || {
                let runtime = match tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                {
                    Ok(runtime) => runtime,
                    Err(e) => {
                        let _ = ready_tx.send(Err(e));
                        return;
                    }
                };
                runtime.block_on(async move {
                    use tokio::signal::unix::{signal, SignalKind};
                    let sources = (|| {
                        Ok::<_, std::io::Error>(SignalSources {
                            sigterm: signal(SignalKind::terminate())?,
                            sigint: signal(SignalKind::interrupt())?,
                            sighup: signal(SignalKind::hangup())?,
                            log,
                        })
                    })();
                    let mut sources = match sources {
                        Ok(s) => s,
                        Err(e) => {
                            let _ = ready_tx.send(Err(e));
                            return;
                        }
                    };
                    let _ = ready_tx.send(Ok(()));
                    tokio::select! {
                        _ = stop_rx.changed() => {},
                        tick = sources.next(Duration::from_secs(u32::MAX as u64)) => {
                            if let Tick::Exit(code) = tick {
                                if let Some((tx, exit)) = shutdown {
                                    exit.store(code, std::sync::atomic::Ordering::SeqCst);
                                    let _ = tx.send(true);
                                }
                                let _ = sender.send(Some(code));
                            }
                        }
                    }
                });
            })?;
        ready_rx.recv().map_err(std::io::Error::other)??;
        Ok(Cycles::Signals(Box::new(Signals {
            receiver,
            stop,
            worker: Some(worker),
        })))
    }

    /// Wait out `period`, unless a signal arrives first.
    ///
    /// On the way out the platform is told ([`notify_shutdown`]) before this
    /// returns, so no daemon can join a thread first: the platform is an
    /// argument here rather than something each daemon remembers to call.  A
    /// fixed source's exit counts as the signal, which is what lets a test
    /// see the call.
    pub async fn next<P: PlatformApi + ?Sized>(
        &mut self,
        period: Duration,
        platform: &mut P,
    ) -> Tick {
        let tick = self.wait(period).await;
        if let Tick::Exit(_) = tick {
            notify_shutdown(platform);
        }
        tick
    }

    /// Observe shutdown without consuming a polling tick. A daemon can race
    /// this against a hardware worker without waiting for that worker to return.
    pub async fn exit_requested(&mut self) -> i32 {
        match self {
            Cycles::Signals(signals) => loop {
                if let Some(code) = *signals.receiver.borrow() {
                    return code;
                }
                if signals.receiver.changed().await.is_err() {
                    return 1;
                }
            },
            #[cfg(any(test, feature = "testing"))]
            Cycles::Fixed { remaining: 0, code } | Cycles::Paced { remaining: 0, code } => *code,
            #[cfg(any(test, feature = "testing"))]
            _ => std::future::pending().await,
        }
    }

    async fn wait(&mut self, period: Duration) -> Tick {
        match self {
            Cycles::Signals(s) => s.next(period).await,
            #[cfg(any(test, feature = "testing"))]
            Cycles::Paced { remaining, code } => {
                if *remaining == 0 {
                    Tick::Exit(*code)
                } else {
                    tokio::time::sleep(period).await;
                    *remaining -= 1;
                    Tick::Cycle
                }
            }
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

/// Tell the platform the daemon is stopping, so the vendor can abort what it
/// is waiting for.
///
/// What a Python daemon's vendor handler does for it when the signal arrives
/// (see the module comment).  Once per process is the platform's business --
/// a second call does nothing -- so a daemon that reaches here twice is fine.
/// A failure is only logged: the shutdown carries on whatever the vendor's
/// handler made of it.
pub fn notify_shutdown<P: PlatformApi + ?Sized>(platform: &mut P) {
    if let Err(e) = platform.notify_shutdown() {
        log::warn!("Failed to notify the platform of the shutdown: {e}");
    }
}

impl SignalSources {
    /// Say that `name` was caught, in this daemon's words.
    fn caught(&self, name: &str, fatal: bool) {
        match (self.log, fatal) {
            (SignalLog::Info, true) => log::info!("Caught signal '{name}' - exiting..."),
            (SignalLog::Info, false) => log::info!("Caught signal '{name}' - ignoring..."),
            (SignalLog::Notice, true) => crate::notice!("Caught signal '{name}'"),
            (SignalLog::Notice, false) => crate::notice!("Caught signal '{name}' - ignoring..."),
        }
    }

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
                    self.caught("SIGTERM", true);
                    return Tick::Exit(exit_code_for(SignalKind::terminate()));
                }
                _ = self.sigint.recv() => {
                    self.caught("SIGINT", true);
                    return Tick::Exit(exit_code_for(SignalKind::interrupt()));
                }
                // Absorbed, and the same wait carries on: returning Cycle here
                // would let a log rotation shorten the poll period, and
                // starting a fresh one would let it lengthen it.
                _ = self.sighup.recv() => {
                    self.caught("SIGHUP", false);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use std::future::Future;
    use super::*;
    use platform_api::PlatformError;
    use tokio::signal::unix::SignalKind;

    /// A platform that counts the shutdowns it is told of, and can refuse.
    #[derive(Default)]
    struct Told {
        notified: usize,
        refuse: bool,
    }

    impl PlatformApi for Told {
        fn notify_shutdown(&mut self) -> Result<(), PlatformError> {
            self.notified += 1;
            if self.refuse {
                return Err(PlatformError::Backend("vendor handler raised".into()));
            }
            Ok(())
        }
    }

    fn signal_waiter(code: Option<i32>) -> (Cycles, tokio::sync::watch::Sender<Option<i32>>) {
        let (sender, receiver) = tokio::sync::watch::channel(code);
        let (stop, _) = tokio::sync::watch::channel(false);
        (Cycles::Signals(Box::new(Signals { receiver, stop, worker: None })), sender)
    }

    #[tokio::test]
    async fn an_exit_wait_returns_a_signal_already_received() {
        let (mut cycles, _sender) = signal_waiter(Some(143));
        assert_eq!(cycles.exit_requested().await, 143);
    }

    #[tokio::test]
    async fn an_exit_wait_receives_a_signal_after_it_starts() {
        let (mut cycles, sender) = signal_waiter(None);
        let mut waiting = Box::pin(cycles.exit_requested());
        assert!(std::future::poll_fn(|context| std::task::Poll::Ready(
            waiting.as_mut().poll(context).is_pending()
        )).await);
        sender.send(Some(130)).unwrap();
        assert_eq!(waiting.await, 130);
    }

    #[tokio::test]
    async fn a_closed_signal_channel_ends_an_exit_wait_with_an_error() {
        let (mut cycles, sender) = signal_waiter(None);
        let mut waiting = Box::pin(cycles.exit_requested());
        assert!(std::future::poll_fn(|context| std::task::Poll::Ready(
            waiting.as_mut().poll(context).is_pending()
        )).await);
        drop(sender);
        assert_eq!(waiting.await, 1);
    }

    #[tokio::test]
    async fn exhausted_test_cycles_return_their_configured_exit_code() {
        for mut cycles in [Cycles::Fixed { remaining: 0, code: 143 },
                           Cycles::Paced { remaining: 0, code: 130 }] {
            let expected = match &cycles {
                Cycles::Fixed { code, .. } | Cycles::Paced { code, .. } => *code,
                _ => unreachable!(),
            };
            assert_eq!(cycles.exit_requested().await, expected);
        }
    }

    #[tokio::test]
    async fn canceling_an_exit_wait_does_not_consume_a_poll_tick() {
        for mut cycles in [Cycles::Fixed { remaining: 2, code: 143 },
                           Cycles::Paced { remaining: 2, code: 143 }] {
            let mut waiting = Box::pin(cycles.exit_requested());
            assert!(std::future::poll_fn(|context| std::task::Poll::Ready(
                waiting.as_mut().poll(context).is_pending()
            )).await);
            drop(waiting);
            let mut platform = Told::default();
            assert_eq!(cycles.next(Duration::ZERO, &mut platform).await, Tick::Cycle);
            assert_eq!(platform.notified, 0);
            match cycles {
                Cycles::Fixed { remaining, .. } | Cycles::Paced { remaining, .. } => assert_eq!(remaining, 1),
                _ => unreachable!(),
            }
        }
    }

    /// supervisord's `autorestart=unexpected` is configured against these, so
    /// they are not free to change.
    #[test]
    fn receives_a_stop_signal_while_the_main_thread_is_blocked() {
        const CHILD: &str = "PMON_SIGNAL_REGRESSION_CHILD";
        if std::env::var_os(CHILD).is_some() {
            let (shutdown, receiver) = tokio::sync::watch::channel(false);
            let code = std::sync::Arc::new(std::sync::atomic::AtomicI32::new(0));
            let _signals = Cycles::signals_with_shutdown(
                SignalLog::Info,
                Some((std::sync::Arc::new(shutdown), std::sync::Arc::clone(&code))),
            )
            .unwrap();
            let sender = std::thread::spawn(|| {
                std::thread::sleep(Duration::from_millis(20));
                assert!(std::process::Command::new("kill")
                    .args(["-TERM", &std::process::id().to_string()])
                    .status()
                    .unwrap()
                    .success());
            });
            let deadline = std::time::Instant::now() + Duration::from_secs(3);
            while !*receiver.borrow() {
                assert!(
                    std::time::Instant::now() < deadline,
                    "signal listener did not wake"
                );
                std::thread::sleep(Duration::from_millis(5));
            }
            assert_eq!(code.load(std::sync::atomic::Ordering::SeqCst), 143);
            sender.join().unwrap();
            return;
        }
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "cycles::tests::receives_a_stop_signal_while_the_main_thread_is_blocked",
            ])
            .env(CHILD, "1")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
    }

    #[test]
    fn the_exit_code_is_the_one_a_shell_reports() {
        assert_eq!(exit_code_for(SignalKind::terminate()), 143);
        assert_eq!(exit_code_for(SignalKind::interrupt()), 130);
    }

    #[test]
    fn an_exit_code_names_the_signal_that_produced_it() {
        assert_eq!(signal_name(143), Some("SIGTERM"));
        assert_eq!(signal_name(130), Some("SIGINT"));
        assert_eq!(signal_name(1), None);
        assert_eq!(signal_name(0), None);
    }

    #[tokio::test]
    async fn a_fixed_source_yields_its_cycles_then_exits() {
        let mut c = Cycles::Fixed {
            remaining: 2,
            code: 0,
        };
        let p = &mut Told::default();
        assert_eq!(c.next(Duration::ZERO, p).await, Tick::Cycle);
        assert_eq!(c.next(Duration::ZERO, p).await, Tick::Cycle);
        assert_eq!(c.next(Duration::ZERO, p).await, Tick::Exit(0));
        assert_eq!(
            c.next(Duration::ZERO, p).await,
            Tick::Exit(0),
            "and stays exited"
        );
    }

    /// The platform hears of the stop when the exit is handed back, and not
    /// before: a cycle is not a shutdown, and a vendor told of one would abort
    /// a wait the daemon still needs.
    #[tokio::test]
    async fn the_platform_is_told_of_the_stop_before_the_exit_is_returned() {
        let mut c = Cycles::Fixed {
            remaining: 1,
            code: 143,
        };
        let mut p = Told::default();
        assert_eq!(c.next(Duration::ZERO, &mut p).await, Tick::Cycle);
        assert_eq!(p.notified, 0, "a cycle is not a shutdown");
        assert_eq!(c.next(Duration::ZERO, &mut p).await, Tick::Exit(143));
        assert_eq!(p.notified, 1);
    }

    /// A vendor handler that failed does not stop the daemon stopping.
    #[tokio::test]
    async fn a_platform_that_fails_to_hear_of_the_stop_is_logged_and_passed() {
        let log = crate::logging::capture();
        let mut c = Cycles::Fixed {
            remaining: 0,
            code: 143,
        };
        let mut p = Told {
            refuse: true,
            ..Default::default()
        };
        assert_eq!(c.next(Duration::ZERO, &mut p).await, Tick::Exit(143));
        assert!(log.logged(
            log::Level::Warn,
            "Failed to notify the platform of the shutdown: platform error: vendor handler raised"
        ));
    }

    #[tokio::test]
    async fn a_timer_cycle_waits_out_the_period() {
        tokio::time::pause();
        let mut c = Cycles::signals().expect("handlers install");
        let start = tokio::time::Instant::now();
        assert_eq!(
            c.next(Duration::from_secs(60), &mut Told::default()).await,
            Tick::Cycle
        );
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
    ///
    /// stormond's words, because they are the ones that reach syslog and so
    /// the ones this test can see.
    #[tokio::test]
    async fn a_sighup_mid_wait_does_not_restart_the_period() {
        tokio::time::pause();
        let log = crate::logging::capture();
        use tokio::signal::unix::{signal, SignalKind};
        let mut c = SignalSources {
            sigterm: signal(SignalKind::terminate()).unwrap(),
            sigint: signal(SignalKind::interrupt()).unwrap(),
            sighup: signal(SignalKind::hangup()).unwrap(),
            log: SignalLog::Notice,
        };
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
