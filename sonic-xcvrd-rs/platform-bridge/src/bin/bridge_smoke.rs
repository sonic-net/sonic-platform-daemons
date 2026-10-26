//! bridge-smoke — prove the PyO3 -> sonic_platform spine on the live DUT.
//!
//! Run inside pmon (where sonic_platform + xcvr-emu exist). It constructs the
//! platform, lists SFPs, prints identity for present modules, and polls one
//! change event. Exit 0 = the whole boundary works: embed CPython, import the
//! plugin, gRPC to the emulator, CMIS decode, marshal back to Rust.

use platform_bridge::{bridge_version, Platform};

fn field<'a>(info: &'a serde_json::Value, key: &str) -> &'a str {
    info.get(key).and_then(|v| v.as_str()).unwrap_or("?")
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    eprintln!("bridge-smoke: platform-bridge v{}", bridge_version());

    let platform = Platform::new()?;
    let n = platform.num_sfps()?;
    println!("num_sfps = {n}");

    let mut present_count = 0;
    for i in 0..n {
        let sfp = platform.sfp(i)?;
        let present = sfp.get_presence()?;
        if !present {
            println!("sfp[{i}] present=false");
            continue;
        }
        present_count += 1;
        match sfp.get_transceiver_info() {
            Ok(info) => println!(
                "sfp[{i}] present=true  type={} manufacturer={} model={} serial={}",
                field(&info, "type"),
                field(&info, "manufacturer"),
                field(&info, "model"),
                field(&info, "serial"),
            ),
            Err(e) => println!("sfp[{i}] present=true  get_transceiver_info ERROR: {e}"),
        }
    }
    println!("present_modules = {present_count}");

    let ev = platform.get_change_event(500)?;
    println!(
        "change_event: status={} sfp={:?} sfp_error={:?}",
        ev.status, ev.sfp, ev.sfp_error
    );

    eprintln!("bridge-smoke: OK");
    Ok(())
}
