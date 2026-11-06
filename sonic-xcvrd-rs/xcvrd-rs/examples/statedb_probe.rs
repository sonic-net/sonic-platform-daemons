//! Example: drive STATE_DB through `swss-common` (no HAL). Proves the swss-common
//! half of the bootstrap. Run inside pmon (STATE_DB live):
//!   cargo build --release --example statedb_probe   # then ship + run in pmon
//! or simply `bash tools/env_check.sh`.

use swss_common::CxxString;
use xcvrd_rs::env;

const KEY: &str = "TRANSCEIVER_INFO|RECODE_STATEDB_PROBE";

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let db = env::open_state_db()?;
    eprintln!(
        "statedb_probe: connected STATE_DB (id={}) via {}",
        env::STATE_DB,
        env::redis_sock()
    );

    db.hset(KEY, "manufacturer", &CxxString::from("recode-probe"))?;
    db.hset(KEY, "model", &CxxString::from("EMU-40G-LR4"))?;

    let all = db.hgetall(KEY)?;
    println!("wrote {} fields to {KEY}:", all.len());
    let mut fields: Vec<_> = all.keys().cloned().collect();
    fields.sort();
    for f in &fields {
        println!("  {f} = {}", all[f].to_string_lossy());
    }

    db.del(KEY)?;
    println!("statedb_probe: OK (key_gone={})", !db.exists(KEY)?);
    Ok(())
}
