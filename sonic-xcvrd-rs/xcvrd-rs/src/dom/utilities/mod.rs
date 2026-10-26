//! `dom/utilities/` — the shared posting engine + the split DOM/status/VDM posters.
//!
//!   - [`db`]         ← `dom/utilities/db/utils.py`         (`DBUtils`: poster + flag-metadata engine)
//!   - [`dom_sensor`] ← `dom/utilities/dom_sensor/*.py`     (`DOMUtils` + `DOMDBUtils`)
//!   - [`status`]     ← `dom/utilities/status/*.py`         (`StatusUtils` + `StatusDBUtils`)
//!   - [`vdm`]        ← `dom/utilities/vdm/*.py`            (`VDMUtils` + `VDMDBUtils`)

pub mod db;
pub mod dom_sensor;
pub mod status;
pub mod vdm;
