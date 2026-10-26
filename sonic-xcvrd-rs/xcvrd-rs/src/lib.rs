//! xcvrd-rs — a Rust port of the SONiC Python `xcvrd` transceiver daemon.
//!
//! Two environment bindings are wired as dependencies so the daemon never
//! re-solves interop:
//!   - [`platform_bridge`] — the Python `sonic_platform` HAL (xcvr-emu) via PyO3.
//!   - [`swss_common`] — STATE_DB (Redis) via the official sonic-net bindings.
//!
//! ## Layout (mirrors the Python `xcvrd` package)
//!
//! [`daemon`] is the process entry point (argument parsing, boot, the worker-thread
//! supervisor and graceful shutdown); [`env`] holds the platform/DB connection seeds.
//!
//!   - [`hal`] / [`db`] — the two mockable trait seams (HAL over `platform-bridge`,
//!     STATE_DB over `swss-common`); [`mock`] provides the test doubles (the Rust
//!     analogue of `tests/mock_platform.py` / `tests/mock_swsscommon.py`).
//!   - [`error`] — the crate `XcvrdError` + `Result` alias.
//!   - [`xcvrd`] — `DaemonXcvrd` orchestration + `SfpStateUpdateTask` (presence/identity).
//!   - [`cmis`] — `CmisManagerTask` (CMIS datapath bring-up).
//!   - [`dom`] — `DomInfoUpdateTask`/`DomThermalInfoUpdateTask` + the DOM/status/VDM
//!     posters under `dom::utilities`.
//!   - [`sff_mgr`] — optional `SffManagerTask`.
//!   - [`xcvrd_utilities`] — shared helpers (common/utils/error-decode/table-registry/
//!     port-mapping/media+optics SI).
//!
//! The daemon logic runs against the trait seams ([`hal`], [`db`], [`mock`]) so the
//! `#[cfg(test)] mod tests` throughout the tree exercise it under mocks, mirroring the
//! Python `tests/test_xcvrd.py`.

pub mod daemon;
pub mod env;

pub mod db;
pub mod error;
pub mod hal;
pub mod mock;

pub mod cmis;
pub mod dom;
pub mod sff_mgr;
pub mod xcvrd;
pub mod xcvrd_utilities;
