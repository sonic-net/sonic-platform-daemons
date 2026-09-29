//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! Which of the two Switch-BMC roles this copy of the daemon plays.
//!
//! Ports `device_info.py:is_switch_host` / `device_info.py:is_switch_bmc`,
//! and `device_info.py:is_platform_env_key_present` behind them, from
//! `sonic_py_common`.  The same binary runs on the switch host, where it
//! mirrors TEMPERATURE_INFO to the BMC, and on the BMC, where it watches that
//! mirror and writes the event log.  The two roles are mutually exclusive and
//! neither is the default.
//!
//! The file both answers come out of is read by [`pmon_common::platform_env`],
//! which also carries the other reading of it that `sensormond` needs.

use pmon_common::platform_env;

/// True when `switch_host=1`.  The mirror side of the Switch-BMC pair, which
/// the BMC mirror will gate on.
#[allow(dead_code)]
pub fn is_switch_host() -> bool {
    platform_env::flag("switch_host")
}

/// True when `switch_bmc=1`.
pub fn is_switch_bmc() -> bool {
    platform_env::flag("switch_bmc")
}

#[cfg(test)]
mod tests {
    use pmon_common::platform_env::flag_in;
    use std::io::Write;

    fn conf(body: &str) -> tempfile::NamedTempFile {
        let mut f = tempfile::NamedTempFile::new().unwrap();
        f.write_all(body.as_bytes()).unwrap();
        f
    }

    /// The parser itself is tested in `pmon_common::platform_env`.  What is
    /// this daemon's own is that the two roles are separate keys, so a host is
    /// never mistaken for a BMC -- getting that backwards would have the switch
    /// host watching a mirror nobody writes.
    #[test]
    fn the_two_roles_are_independent() {
        let f = conf("switch_host=1\nswitch_bmc=0\n");
        assert!(flag_in(f.path(), "switch_host"));
        assert!(!flag_in(f.path(), "switch_bmc"));
    }
}
