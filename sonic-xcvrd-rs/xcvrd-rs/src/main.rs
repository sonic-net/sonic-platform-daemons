//! xcvrd-rs daemon entrypoint.
//!
//! Parses the same command-line arguments as the Python `xcvrd` and runs the
//! transceiver monitor: it populates the `TRANSCEIVER_*` STATE_DB tables from the
//! platform HAL and drives the presence/identity, DOM, CMIS and (optional) SFF worker
//! threads. The logic lives in [`xcvrd_rs::daemon`] (built on the shared platform HAL
//! + swss-common STATE_DB bindings).
//!
//! Intended to run in pmon's existing `xcvrd` supervisor slot.

fn main() {
    xcvrd_rs::daemon::run();
}
