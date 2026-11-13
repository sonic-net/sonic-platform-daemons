# Rust daemon executables

Rust daemon executables use the daemon name (for example, `/usr/bin/thermalctld`).
Python scripts remain under `/usr/local/bin`; startup configuration selects the
implementation by absolute path. Rust crate and Debian package names retain
the `-rs` suffix.
