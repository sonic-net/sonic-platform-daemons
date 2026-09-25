//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! `platform_env.conf`, which decides which of the two Switch-BMC roles this
//! copy of the daemon plays.
//!
//! Ports `is_switch_host()` / `is_switch_bmc()` from
//! `sonic_py_common/device_info.py:823-845`.  The same binary runs on the
//! switch host, where it mirrors TEMPERATURE_INFO to the BMC, and on the BMC,
//! where it watches that mirror and writes the event log.  The two roles are
//! mutually exclusive and neither is the default.

use std::path::{Path, PathBuf};

/// Python's `CONTAINER_PLATFORM_PATH` then the host device directory.  Inside
/// pmon only the first exists.
const CONTAINER_PLATFORM_PATH: &str = "/usr/share/sonic/platform";
const PLATFORM_ENV_CONF: &str = "platform_env.conf";

/// True when `switch_host=1`.  The mirror side of the Switch-BMC pair, which
/// the BMC mirror will gate on.
#[allow(dead_code)]
pub fn is_switch_host() -> bool {
    key_present("switch_host")
}

/// True when `switch_bmc=1`.
pub fn is_switch_bmc() -> bool {
    key_present("switch_bmc")
}

fn conf_path() -> Option<PathBuf> {
    let p = Path::new(CONTAINER_PLATFORM_PATH).join(PLATFORM_ENV_CONF);
    p.is_file().then_some(p)
}

fn key_present(key: &str) -> bool {
    conf_path().is_some_and(|p| read_key(&p, key))
}

/// `key=value` lines; the key match is case-insensitive and only `1` is true,
/// exactly as Python's loop does.
fn read_key(path: &Path, key: &str) -> bool {
    let Ok(text) = std::fs::read_to_string(path) else {
        return false;
    };
    for line in text.lines() {
        let Some((k, v)) = line.split_once('=') else {
            continue;
        };
        if k.trim().eq_ignore_ascii_case(key.trim()) {
            return v.trim() == "1";
        }
    }
    false
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
        assert!(read_key(f.path(), "switch_bmc"));
        let f = conf("switch_bmc=0\n");
        assert!(!read_key(f.path(), "switch_bmc"));
        let f = conf("switch_bmc=yes\n");
        assert!(!read_key(f.path(), "switch_bmc"));
    }

    #[test]
    fn the_key_match_is_case_insensitive_and_trimmed() {
        let f = conf("  SWITCH_BMC = 1 \n");
        assert!(read_key(f.path(), "switch_bmc"));
    }

    #[test]
    fn an_absent_key_or_file_is_false() {
        let f = conf("switch_host=1\n");
        assert!(!read_key(f.path(), "switch_bmc"));
        assert!(!read_key(Path::new("/nonexistent/platform_env.conf"), "switch_bmc"));
    }

    #[test]
    fn lines_without_an_equals_are_skipped() {
        let f = conf("# a comment\nswitch_host=1\n");
        assert!(read_key(f.path(), "switch_host"));
    }

    /// The two roles are separate keys, so a host is not a BMC.
    #[test]
    fn the_two_roles_are_independent() {
        let f = conf("switch_host=1\nswitch_bmc=0\n");
        assert!(read_key(f.path(), "switch_host"));
        assert!(!read_key(f.path(), "switch_bmc"));
    }
}
