# Rust daemon executables

Rust daemon executables use the daemon name (for example, `/usr/bin/thermalctld`).
Python scripts remain under `/usr/local/bin`; startup configuration selects the
implementation by absolute path. Rust crate and Debian package names retain
the `-rs` suffix.

The Debian build keeps the committed Cargo.lock as its baseline. When an image
selects in-tree platform/swss crates with SONIC_CARGO_CONFIG, configure resolves
those replacements and rejects changes to selected external dependency versions,
sources or checksums. Native-only external dependencies are seeded and checked
against the selected in-tree component lockfiles. Both release build and tests
then use the checked lock
with --locked. Clean restores the original lock. The lock checker uses the
Python 3.11-or-newer interpreter provided by the supported build environments.
