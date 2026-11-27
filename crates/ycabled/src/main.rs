//! ycabled-rs: started by supervisord in pmon as program `ycabled`.

use ycabled::daemon::{self, Options};
use ycabled::exit;
use ycabled::log_error;

fn main() {
    exit::mark_start();
    // Before any thread starts: one malloc arena. Otherwise each thread that
    // allocates, such as the threads of Python calls, can get an arena of its
    // own, up to eight per core, and keeps its memory. Python calls take turns
    // and there is one worker thread, so the arena's lock is hardly contended.
    #[cfg(all(target_os = "linux", target_env = "gnu"))]
    // SAFETY: mallopt only sets an allocator parameter.
    unsafe {
        libc::mallopt(libc::M_ARENA_MAX, 1);
    }
    // A panic is a bug: log it and exit, so that supervisord starts the
    // daemon again (N8), but not sooner than exit::MIN_RUN after the start.
    // Nothing goes to stdout or stderr.
    std::panic::set_hook(Box::new(|info| {
        let msg = info
            .payload()
            .downcast_ref::<&str>()
            .map(|s| s.to_string())
            .or_else(|| info.payload().downcast_ref::<String>().cloned())
            .unwrap_or_default();
        let at = info
            .location()
            .map(|l| format!("{}:{}", l.file(), l.line()))
            .unwrap_or_default();
        log_error!("ycabled-rs internal error at {}: {}", at, msg);
        exit::after_failure(1);
    }));
    let runtime = match tokio::runtime::Builder::new_multi_thread()
        // One worker: the async work (redis, gRPC, the port actors) is
        // small, and a second worker only adds wake-ups between the two.
        .worker_threads(1)
        // Python driver calls run on blocking threads, BRIDGE_SLOTS at a
        // time, started when needed and kept for 120 s idle, longer than the
        // 60 s between telemetry passes. A call still running after 10 ms
        // gives its slot back, and one that never returns keeps only its
        // thread. A port has at most three calls in flight (a request, a
        // telemetry call and a firmware download), so hung calls on some
        // ports cannot use up the threads the others need.
        .max_blocking_threads(512)
        .thread_keep_alive(std::time::Duration::from_secs(120))
        .thread_name("ycabled-rs")
        .enable_all()
        .build()
    {
        Ok(r) => r,
        Err(e) => {
            log_error!("Failed to start the runtime: {}", e);
            exit::after_failure(1);
        }
    };
    match runtime.block_on(daemon::run(Options::default())) {
        Ok(()) => std::process::exit(0),
        Err(e) => {
            log_error!("ycabled-rs failed: {:#}", e);
            exit::after_failure(1);
        }
    }
}
