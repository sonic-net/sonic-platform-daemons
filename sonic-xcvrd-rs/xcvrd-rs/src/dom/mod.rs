//! `dom/` — the periodic DOM/status/VDM/PM/firmware subsystem, ported from the
//! Python `xcvrd/dom/` subpackage (analysis §3.2).
//!
//!   - [`dom_mgr`]   ← `dom_mgr.py`   (`DomInfoUpdateTask` + `DomThermalInfoUpdateTask`)
//!   - [`utilities`] ← `dom/utilities/` (the shared `DBUtils` poster + DOM/status/VDM
//!                     posters split onto it)

pub mod dom_mgr;
pub mod utilities;
