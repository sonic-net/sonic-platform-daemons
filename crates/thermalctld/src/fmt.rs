//! The STATE_DB formatter: pmon-common's, re-exported.
//!
//! Python's `str()` semantics for floats, bools, thresholds and timestamps
//! are shared, because every ported daemon writes through the same
//! conventions.  Fan direction used to be thermalctld's own, as one of three
//! spellings; it is now published as the platform spelled it, like any other
//! string.

pub use pmon_common::fmt::{
    bool, event_timestamp, float, number, opt_bool, opt_str, position, threshold, timestamp,
    NOT_AVAILABLE,
};

/// Python's quoting for an invalid ASCII configuration value.
/// Non-ASCII text is retained, like Python's repr of printable Unicode.
pub fn config_repr(value: &str) -> String {
    let quote = if value.contains('\'') && !value.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::from(quote);
    for c in value.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if c.is_ascii_control() => out.push_str(&format!("\\x{:02x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

#[cfg(test)]
mod config_tests {
    #[test]
    fn malformed_values_use_python_quotes_and_escape_controls() {
        assert_eq!(super::config_repr("x"), "'x'");
        assert_eq!(super::config_repr("can't"), "\"can't\"");
        assert_eq!(super::config_repr("a\n"), "'a\\n'");
    }
}
