//! Syslog writer of ycabled-rs.
//!
//! Messages go to `/dev/log` with the identity `ycable`, facility USER and
//! no PID, the way `sonic_py_common`'s Logger sends them for Python ycabled.
//! The identity is fixed: nothing the vendor drivers do with `openlog` can
//! change it.
//!
//! A thread of its own sends them, in order: a syslog that does not take
//! them in would otherwise hold up the caller, which may be the runtime's
//! worker and with it every port. Logging only queues the message; when the
//! queue is full, messages are dropped, counted, and the count logged.

use std::os::unix::net::UnixDatagram;
use std::sync::atomic::{AtomicU64, AtomicU8, AtomicUsize, Ordering};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender};
use std::sync::{Arc, OnceLock};
use std::time::{Duration, Instant};

pub const IDENTITY: &str = "ycable";
const FACILITY_USER: u8 = 1;
const DEV_LOG: &str = "/dev/log";

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
#[repr(u8)]
pub enum Level {
    Error = 3,
    Warning = 4,
    Notice = 5,
    Info = 6,
    Debug = 7,
}

impl Level {
    pub fn from_u8(v: u8) -> Level {
        match v {
            0..=3 => Level::Error,
            4 => Level::Warning,
            5 => Level::Notice,
            6 => Level::Info,
            _ => Level::Debug,
        }
    }
}

/// How many messages may wait for the writer: a burst of debug messages
/// fits, and a syslog that keeps up takes them at once.
const QUEUE: usize = 4096;

#[derive(Default)]
struct Counts {
    /// Messages queued and not sent yet.
    pending: AtomicUsize,
    /// Messages dropped because the queue was full, not logged yet.
    dropped: AtomicU64,
}

struct Logger {
    min: AtomicU8,
    lines: SyncSender<String>,
    counts: Arc<Counts>,
}

impl Logger {
    /// A logger to the syslog socket at `path`, with its writer thread.
    fn new(path: String) -> Logger {
        let (lines, rx) = sync_channel(QUEUE);
        let counts = Arc::new(Counts::default());
        let c = Arc::clone(&counts);
        // Without the thread, messages are dropped and counted.
        let _ = std::thread::Builder::new()
            .name("ycabled-log".into())
            .spawn(move || write_lines(rx, &path, &c));
        Logger {
            min: AtomicU8::new(Level::Notice as u8),
            lines,
            counts,
        }
    }

    /// Queues `line` for the writer. Never waits.
    fn queue(&self, line: String) {
        self.counts.pending.fetch_add(1, Ordering::SeqCst);
        if self.lines.try_send(line).is_err() {
            self.counts.pending.fetch_sub(1, Ordering::SeqCst);
            self.counts.dropped.fetch_add(1, Ordering::Relaxed);
        }
    }

    fn flush(&self, limit: Duration) {
        let end = Instant::now() + limit;
        while self.counts.pending.load(Ordering::SeqCst) > 0 && Instant::now() < end {
            std::thread::sleep(Duration::from_millis(1));
        }
    }
}

static LOGGER: OnceLock<Logger> = OnceLock::new();

fn logger() -> &'static Logger {
    LOGGER.get_or_init(|| {
        Logger::new(std::env::var("YCABLED_RS_SYSLOG").unwrap_or_else(|_| DEV_LOG.to_string()))
    })
}

/// The lowest severity that is sent: NOTICE by default, DEBUG while
/// `XCVRD_LOG|Y_CABLE` `log_verbosity` is `debug`.
pub fn set_min_level(level: Level) {
    logger().min.store(level as u8, Ordering::Relaxed);
}

pub fn min_level() -> Level {
    Level::from_u8(logger().min.load(Ordering::Relaxed))
}

pub fn enabled(level: Level) -> bool {
    level as u8 <= logger().min.load(Ordering::Relaxed)
}

/// What glibc's syslog() sends for `openlog(ident, LOG_NDELAY, LOG_USER)`.
pub fn format_line(level: Level, timestamp: &str, msg: &str) -> String {
    format!(
        "<{}>{} {}: {}",
        FACILITY_USER * 8 + level as u8,
        timestamp,
        IDENTITY,
        msg
    )
}

fn timestamp() -> String {
    // Parsed once: parsing the format string took as long as formatting.
    static ITEMS: OnceLock<Vec<chrono::format::Item<'static>>> = OnceLock::new();
    let items =
        ITEMS.get_or_init(|| chrono::format::StrftimeItems::new("%b %e %H:%M:%S").collect());
    chrono::Local::now()
        .format_with_items(items.iter())
        .to_string()
}

/// Queues `msg` for syslog, if `level` is enabled. Never waits for syslog.
pub fn log(level: Level, msg: &str) {
    if !enabled(level) {
        return;
    }
    #[cfg(test)]
    capture::record(level, msg);
    logger().queue(format_line(level, &timestamp(), msg));
}

/// Waits up to `limit` for the messages queued so far to be sent: before
/// the process exits.
pub fn flush(limit: Duration) {
    if let Some(l) = LOGGER.get() {
        l.flush(limit);
    }
}

/// The writer thread: sends the queued lines in order, and after one, the
/// count of the messages dropped meanwhile.
fn write_lines(rx: Receiver<String>, path: &str, counts: &Counts) {
    let mut sock = None;
    while let Ok(line) = rx.recv() {
        send_line(&mut sock, path, &line);
        let dropped = counts.dropped.swap(0, Ordering::Relaxed);
        if dropped > 0 {
            let note = format!("{dropped} messages were dropped: syslog did not take them in time");
            send_line(
                &mut sock,
                path,
                &format_line(Level::Warning, &timestamp(), &note),
            );
        }
        counts.pending.fetch_sub(1, Ordering::SeqCst);
    }
}

fn send_line(sock: &mut Option<UnixDatagram>, path: &str, line: &str) {
    for _ in 0..2 {
        if sock.is_none() {
            if let Ok(s) = UnixDatagram::unbound() {
                let _ = s.set_write_timeout(Some(Duration::from_millis(500)));
                if s.connect(path).is_ok() {
                    *sock = Some(s);
                }
            }
        }
        match sock.as_ref() {
            Some(s) => {
                if s.send(line.as_bytes()).is_ok() {
                    return;
                }
                // The syslog socket was replaced (rsyslog restart): connect again once.
                *sock = None;
            }
            None => return,
        }
    }
}

#[macro_export]
macro_rules! log_error {
    ($($arg:tt)*) => { $crate::logger::log($crate::logger::Level::Error, &format!($($arg)*)) };
}
#[macro_export]
macro_rules! log_warning {
    ($($arg:tt)*) => { $crate::logger::log($crate::logger::Level::Warning, &format!($($arg)*)) };
}
#[macro_export]
macro_rules! log_notice {
    ($($arg:tt)*) => { $crate::logger::log($crate::logger::Level::Notice, &format!($($arg)*)) };
}
#[macro_export]
macro_rules! log_info {
    ($($arg:tt)*) => {
        if $crate::logger::enabled($crate::logger::Level::Info) {
            $crate::logger::log($crate::logger::Level::Info, &format!($($arg)*))
        }
    };
}
#[macro_export]
macro_rules! log_debug {
    ($($arg:tt)*) => {
        if $crate::logger::enabled($crate::logger::Level::Debug) {
            $crate::logger::log($crate::logger::Level::Debug, &format!($($arg)*))
        }
    };
}

/// Every message sent while the unit tests run. Tests run in parallel, so
/// a test looks only for messages that name its own ports.
#[cfg(test)]
pub mod capture {
    use super::Level;
    use std::sync::Mutex;

    static LINES: Mutex<Vec<(Level, String)>> = Mutex::new(Vec::new());

    pub(super) fn record(level: Level, msg: &str) {
        LINES
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .push((level, msg.to_string()));
    }

    /// The messages that contain `text`.
    pub fn find(text: &str) -> Vec<(Level, String)> {
        LINES
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .iter()
            .filter(|(_, m)| m.contains(text))
            .cloned()
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn line_has_glibc_layout() {
        assert_eq!(
            format_line(Level::Error, "Nov  7 08:31:08", "boom"),
            "<11>Nov  7 08:31:08 ycable: boom"
        );
        assert_eq!(
            format_line(Level::Notice, "Nov 17 08:31:08", "x"),
            "<13>Nov 17 08:31:08 ycable: x"
        );
        assert_eq!(
            format_line(Level::Debug, "Jan  1 00:00:00", "d"),
            "<15>Jan  1 00:00:00 ycable: d"
        );
    }

    #[test]
    fn levels_order_by_severity() {
        assert!(Level::Error < Level::Notice);
        assert_eq!(Level::from_u8(7), Level::Debug);
        assert_eq!(Level::from_u8(0), Level::Error);
    }

    #[test]
    fn writer_sends_the_lines_in_order() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("log.sock");
        let server = UnixDatagram::bind(&path).unwrap();
        server
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let l = Logger::new(path.to_str().unwrap().into());
        for m in ["hello", "world"] {
            l.queue(format_line(Level::Warning, "Nov  7 08:31:08", m));
        }
        l.flush(Duration::from_secs(5));
        let mut buf = [0u8; 256];
        for m in ["hello", "world"] {
            let n = server.recv(&mut buf).unwrap();
            assert_eq!(
                std::str::from_utf8(&buf[..n]).unwrap(),
                format!("<12>Nov  7 08:31:08 ycable: {m}")
            );
        }
    }

    /// Logging never waits for syslog: with a syslog socket that takes
    /// nothing in, the messages are queued at once, and dropped once the
    /// queue is full, rather than holding up the caller (and with it the
    /// runtime's worker) for the send timeout.
    #[test]
    fn logging_does_not_wait_for_a_stuck_syslog() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("log.sock");
        let _server = UnixDatagram::bind(&path).unwrap();
        let l = Logger::new(path.to_str().unwrap().into());
        let start = Instant::now();
        for i in 0..QUEUE + 1000 {
            l.queue(format!("message {i} {}", "x".repeat(200)));
            assert!(
                start.elapsed() < Duration::from_secs(1),
                "message {i} was queued after {:?}",
                start.elapsed()
            );
        }
        assert!(l.counts.dropped.load(Ordering::Relaxed) > 0);
    }
}
