//! `cmis/` — the CMIS datapath bring-up state machine, ported from the Python
//! `xcvrd/cmis/` subpackage (analysis §3.2).
//!
//!   - [`cmis_api`] ← the mockable `CmisApi` control/decode seam the state machine drives
//!   - [`cmis_manager_task`] ← `cmis_manager_task.py` (`CmisManagerTask`)
//!
//! `cmis/__init__.py` re-exports `CmisManagerTask`; mirror that here.

pub mod cmis_api;
pub mod cmis_manager_task;

pub use cmis_manager_task::CmisManagerTask;
