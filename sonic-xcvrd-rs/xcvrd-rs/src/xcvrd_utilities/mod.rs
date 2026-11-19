//! `xcvrd_utilities/` — shared helpers, ported 1:1 from the Python
//! `xcvrd/xcvrd_utilities/` subpackage (analysis §3.2).
//!
//! Directory→module, file→submodule so the port is traceable:
//!   - [`common`]              ← `common.py`              (CMIS_STATE_* consts, DB/CMIS helpers)
//!   - [`utils`]               ← `utils.py`               (`XcvrdUtils`: presence/flat-memory/lpmode)
//!   - [`sfp_status_helper`]   ← `sfp_status_helper.py`   (error masks + bit→description table)
//!   - [`xcvr_table_helper`]   ← `xcvr_table_helper.py`   (TRANSCEIVER_* table-name consts + registry)
//!   - [`port_event_helper`]   ← `port_event_helper.py`   (PortMapping / PortChangeEvent / observer)
//!   - [`media_settings_parser`] ← `media_settings_parser.py`
//!   - [`optics_si_parser`]      ← `optics_si_parser.py`

pub mod common;
pub mod media_settings_parser;
pub mod optics_si_parser;
pub mod port_event_helper;
pub mod sfp_status_helper;
pub mod utils;
pub mod xcvr_table_helper;
