//! Every crate's comments cite the Python daemon they reproduce as
//! `file:Symbol`.  This fails the build when one of those symbols no longer
//! exists, or when a comment cites Python by line number.
//!
//! The check is `python_refs.py` beside this file: reading Python is Python's
//! job, and python3 is already a build dependency (`python3-dev`).  It lives
//! here only so `cargo test --workspace`, which `debian/rules` runs, runs it.

use std::process::Command;

#[test]
fn every_python_reference_names_something_that_exists() {
    let script = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/python_refs.py");
    let out = Command::new("python3")
        .arg(script)
        .output()
        .expect("python3 is a build dependency");
    assert!(
        out.status.success(),
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
}
