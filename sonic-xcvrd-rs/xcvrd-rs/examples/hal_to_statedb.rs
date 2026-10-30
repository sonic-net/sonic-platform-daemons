//! Example: the interop pattern the translation agents implement for real --
//! read a transceiver through the shared platform API and publish it to
//! STATE_DB (`swss-common`). This is `SfpStateUpdateTask` in miniature.
//!
//! Run inside pmon (xcvr-emu + STATE_DB live):
//!   cargo build --release --example hal_to_statedb   # then ship + run in pmon
//! or simply `bash tools/env_check.sh`.

use swss_common::CxxString;
use xcvrd_rs::env;
use xcvrd_rs::hal::{Hal, PlatformHal};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Bootstrap both bindings from the shared env seed.
    let platform = PlatformHal::from_platform(env::open_platform()?);
    let db = env::open_state_db()?; // swss-common -> libswsscommon -> STATE_DB

    println!("num_sfps = {}", platform.num_sfps()?);

    let idx = 0usize;
    let sfp = platform.sfp(idx)?;
    if !sfp.get_presence()? {
        println!("sfp[{idx}] not present; nothing to publish");
        eprintln!("hal_to_statedb: OK (no module)");
        return Ok(());
    }

    // Read identity via the bridge, then project the fields xcvrd publishes to
    // TRANSCEIVER_INFO. CMIS strings are NUL-padded; strip like the Python original.
    let info = sfp.get_transceiver_info()?;
    let key = format!("TRANSCEIVER_INFO|RECODE_HAL2DB_{idx}");
    let mut wrote = 0usize;
    for field in [
        "type",
        "manufacturer",
        "model",
        "serial",
        "vendor_rev",
        "cmis_rev",
    ] {
        if let Some(v) = info.get(field).and_then(|x| x.as_str()) {
            db.hset(&key, field, &CxxString::from(v.trim_end_matches('\0')))?;
            wrote += 1;
        }
    }
    println!("bridge -> swss: wrote {wrote} fields to {key}");

    let all = db.hgetall(&key)?;
    let mut fields: Vec<_> = all.keys().cloned().collect();
    fields.sort();
    for f in &fields {
        println!("  {f} = {}", all[f].to_string_lossy());
    }

    db.del(&key)?;
    println!("cleaned up STATE_DB key");
    eprintln!("hal_to_statedb: OK");
    Ok(())
}
