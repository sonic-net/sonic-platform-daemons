//! Syslog writer of ycabled-rs.
//!
//! Messages go to `/dev/log` with the identity `ycable`, facility USER and
//! no PID, the way `sonic_py_common`'s Logger sends them for Python ycabled.
//! The identity is fixed: nothing the vendor drivers do with `openlog` can
//! change it.

use std::os::unix::net::UnixDatagram;
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

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

struct Logger {
    min: AtomicU8,
    sock: Mutex<Option<UnixDatagram>>,
    path: String,
}

static LOGGER: OnceLock<Logger> = OnceLock::new();

fn logger() -> &'static Logger {
    LOGGER.get_or_init(|| Logger {
        min: AtomicU8::new(Level::Notice as u8),
        sock: Mutex::new(None),
        path: std::env::var("YCABLED_RS_SYSLOG").unwrap_or_else(|_| DEV_LOG.to_string()),
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

pub fn log(level: Level, msg: &str) {
    if !enabled(level) {
        return;
    }
    #[cfg(test)]
    capture::record(level, msg);
    let l = logger();
    let ts = chrono::Local::now().format("%b %e %H:%M:%S").to_string();
    let line = format_line(level, &ts, msg);
    let mut guard = match l.sock.lock() {
        Ok(g) => g,
        Err(p) => p.into_inner(),
    };
    for _ in 0..2 {
        if guard.is_none() {
            if let Ok(s) = UnixDatagram::unbound() {
                let _ = s.set_write_timeout(Some(Duration::from_millis(500)));
                if s.connect(&l.path).is_ok() {
                    *guard = Some(s);
                }
            }
        }
        match guard.as_ref() {
            Some(s) => {
                if s.send(line.as_bytes()).is_ok() {
                    return;
                }
                // The syslog socket was replaced (rsyslog restart): connect again once.
                *guard = None;
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
    fn sends_to_the_socket() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("log.sock");
        let server = UnixDatagram::bind(&path).unwrap();
        let l = Logger {
            min: AtomicU8::new(Level::Notice as u8),
            sock: Mutex::new(None),
            path: path.to_str().unwrap().into(),
        };
        // Through the same code path as log(), with a private logger.
        let line = format_line(Level::Warning, "Nov  7 08:31:08", "hello");
        let s = UnixDatagram::unbound().unwrap();
        s.connect(&l.path).unwrap();
        s.send(line.as_bytes()).unwrap();
        let mut buf = [0u8; 256];
        let n = server.recv(&mut buf).unwrap();
        assert_eq!(
            std::str::from_utf8(&buf[..n]).unwrap(),
            "<12>Nov  7 08:31:08 ycable: hello"
        );
    }
}
