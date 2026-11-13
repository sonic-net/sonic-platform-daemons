//! ycabled-rs: started by supervisord in pmon as program `ycabled`.

use ycabled::daemon::{self, Options};
use ycabled::log_error;

fn main() {
    // A panic is a bug: log it and exit, so that supervisord starts the
    // daemon again (N8). Nothing goes to stdout or stderr.
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
        std::process::exit(1);
    }));
    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        // Python driver calls run on blocking threads, started when needed
        // and stopped after 10 s idle. A call that never returns keeps its
        // thread, but a port has at most two calls in flight, one from its
        // actor and one from its telemetry pass, besides firmware downloads,
        // so hung calls on some ports cannot use up the threads the others
        // need.
        .max_blocking_threads(512)
        .thread_name("ycabled-rs")
        .enable_all()
        .build()
    {
        Ok(r) => r,
        Err(e) => {
            log_error!("Failed to start the runtime: {}", e);
            std::process::exit(1);
        }
    };
    match runtime.block_on(daemon::run(Options::default())) {
        Ok(()) => std::process::exit(0),
        Err(e) => {
            log_error!("ycabled-rs failed: {:#}", e);
            std::process::exit(1);
        }
    }
}
