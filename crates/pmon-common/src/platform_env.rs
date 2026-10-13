//! `/usr/share/sonic/platform/platform_env.conf`, read the two ways Python
//! reads it.
//!
//! It is one `key=value` file, but the daemons do not agree on how to parse it,
//! and the disagreement is visible from outside:
//!
//!  * `device_info.py:is_platform_env_key_present` matches the key
//!    case-insensitively and treats only the literal `1` as true.  That is what
//!    decides the Switch-BMC role (`device_info.py:is_switch_bmc`).
//!  * `sensormond:SensorMonitorDaemon.__init__` matches the key exactly and
//!    takes the value as a number.  A key spelled `sensormond_warning_time` is
//!    not the key it wants.
//!
//! Both are reproduced rather than reconciled: a platform shipping a file with
//! an oddly-cased key gets whatever it got before, and unifying the two would
//! silently start or stop honouring somebody's conf.

use std::path::{Path, PathBuf};

/// Inside pmon this is the only platform directory that exists; on the host
/// Python falls back to the device tree, which no daemon here runs from.
const CONTAINER_PLATFORM_PATH: &str = "/usr/share/sonic/platform";
const PLATFORM_ENV_CONF: &str = "platform_env.conf";

fn conf_path() -> Option<PathBuf> {
    let p = Path::new(CONTAINER_PLATFORM_PATH).join(PLATFORM_ENV_CONF);
    p.is_file().then_some(p)
}

/// `device_info.py`'s reading: case-insensitive key, true only for `1`.
pub fn flag(key: &str) -> bool {
    conf_path().is_some_and(|p| flag_in(&p, key))
}

/// The same, against a given file.
pub fn flag_in(path: &Path, key: &str) -> bool {
    for (k, v) in entries(path) {
        if k.eq_ignore_ascii_case(key.trim()) {
            return v == "1";
        }
    }
    false
}

/// `sensormond`'s reading: exact key, value returned as written.
pub fn value(key: &str) -> Option<String> {
    conf_path().and_then(|p| value_in(&p, key))
}

/// The same, against a given file.
pub fn value_in(path: &Path, key: &str) -> Option<String> {
    entries(path).into_iter().find(|(k, _)| k == key).map(|(_, v)| v)
}

/// The `key=value` lines, both sides trimmed.
///
/// A missing or unreadable file yields nothing rather than an error: the file
/// is optional on every platform that does not need it, and a daemon that
/// refused to start without it would be refusing on most of the fleet.
fn entries(path: &Path) -> Vec<(String, String)> {
    let Ok(text) = std::fs::read_to_string(path) else {
        return Vec::new();
    };
    text.lines()
        .filter_map(|line| line.split_once('='))
        .map(|(k, v)| (k.trim().to_string(), v.trim().to_string()))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn conf(body: &str) -> tempfile::NamedTempFile {
        let mut f = tempfile::NamedTempFile::new().unwrap();
        f.write_all(body.as_bytes()).unwrap();
        f
    }

    #[test]
    fn one_is_true_and_anything_else_is_false() {
        let f = conf("switch_bmc=1\n");
        assert!(flag_in(f.path(), "switch_bmc"));
        assert!(!flag_in(conf("switch_bmc=0\n").path(), "switch_bmc"));
        assert!(!flag_in(conf("switch_bmc=yes\n").path(), "switch_bmc"));
    }

    #[test]
    fn a_flag_key_matches_regardless_of_case() {
        let f = conf("  SWITCH_BMC = 1 \n");
        assert!(flag_in(f.path(), "switch_bmc"));
    }

    /// The other half of the same file, and the reason there are two readers:
    /// `sensormond` compares the key with `==`, so this must *not* match.
    #[test]
    fn a_value_key_must_match_exactly() {
        let f = conf("sensormond_warning_time = 45\n");
        assert_eq!(value_in(f.path(), "SENSORMOND_WARNING_TIME"), None);
        let f = conf("SENSORMOND_WARNING_TIME = 45\n");
        assert_eq!(value_in(f.path(), "SENSORMOND_WARNING_TIME").as_deref(), Some("45"));
    }

    #[test]
    fn an_absent_key_or_file_yields_nothing() {
        let f = conf("switch_host=1\n");
        assert!(!flag_in(f.path(), "switch_bmc"));
        assert_eq!(value_in(f.path(), "SENSORMOND_WARNING_TIME"), None);
        let missing = Path::new("/nonexistent/platform_env.conf");
        assert!(!flag_in(missing, "switch_bmc"));
        assert_eq!(value_in(missing, "SENSORMOND_WARNING_TIME"), None);
    }

    /// A line with no `=` is skipped rather than ending the scan: the file is
    /// hand-maintained per platform and a stray comment is likelier than not.
    #[test]
    fn a_line_without_a_separator_does_not_stop_the_scan() {
        let f = conf("# a comment\n\nSENSORMOND_WARNING_TIME=45\n");
        assert_eq!(value_in(f.path(), "SENSORMOND_WARNING_TIME").as_deref(), Some("45"));
    }
}
