//! Build script — make the unit-test binary loadable in the build container.
//!
//! ## Why this exists
//! The daemon links `libswsscommon` (STATE_DB C++ bindings). The unit tests use the
//! crate's MOCK DB/HAL seams and never touch Redis, but the compiled test binary is
//! still *dynamically linked* against `libswsscommon.so.0`, so the loader must
//! resolve it — and its whole transitive closure — when the binary RUNS.
//!
//! The workspace Dockerfile provides a build-only environment. Deployed binaries
//! run inside pmon, which has the runtime libraries, but `cargo test` also runs
//! executables in the build container, which has no `libswsscommon.so.0` on the loader
//! path nor its closure (`libzmq`, `libhiredis`, `libboost_serialization`,
//! `libyang`, `libnl-*`). Without this script the test binary aborts at startup with
//! `libswsscommon.so.0: cannot open shared object file`.
//!
//! ## What this does (all confined to the build container / its mounts)
//! 1. Bakes `/swsslib` into the binary's search path. Container builds mount
//!    pmon's staged `libswsscommon.so` and its runtime dependencies there.
//! 2. Best-effort stages `libswsscommon`'s runtime closure into that same `/swsslib`
//!    dir (installing the trixie packages that provide the exact SONAMEs, then
//!    copying the resolved objects next to `libswsscommon.so`). Because `/swsslib`
//!    is a persistent host mount shared by every tool invocation, this is a one-time
//!    cost: once staged, later `cargo build`/`cargo test` runs skip it.
//!
//! Everything here is best-effort and never fails compilation. The deployed daemon
//! runs in pmon where
//! the closure already exists and `/swsslib` doesn't (so the extra RUNPATH entry is
//! simply skipped by the loader). It only *matters* for `cargo test`'s in-container
//! run.

use std::path::{Path, PathBuf};
use std::process::Command;

/// Trixie packages providing the exact SONAMEs `libswsscommon.so.0` needs.
const CLOSURE_PKGS: &[&str] = &[
    "libzmq5",
    "libhiredis1.1.0",
    "libboost-serialization1.83.0",
    "libyang3",
    "libnl-3-200",
    "libnl-route-3-200",
    "libnl-genl-3-200",
    "libnl-nf-3-200",
];

/// Toolchain/base libraries the container already ships — never copy these into
/// `/swsslib` (avoids glibc/libstdc++ version skew; they always resolve from the
/// default search path).
const BASE_LIBS: &[&str] = &[
    "libc.so.6",
    "libm.so.6",
    "libstdc++.so.6",
    "libgcc_s.so.1",
    "libpthread.so.0",
    "libdl.so.2",
    "librt.so.1",
    "ld-linux-x86-64.so.2",
    "linux-vdso.so.1",
];

const SWSSLIB: &str = "/swsslib";
const SWSSCOMMON_SO: &str = "/swsslib/libswsscommon.so.0";

fn main() {
    // Runtime search dir for libswsscommon.so.0 + the closure we stage beside it.
    //
    // Emit it as a *DT_RPATH* (old dtags), not the modern default DT_RUNPATH.
    // DT_RUNPATH is consulted by the loader only for an object's own *direct*
    // NEEDED entries and is NOT inherited by transitive dependencies. The test
    // binary's only direct swss dep is `libswsscommon.so.0`, so a RUNPATH resolves
    // that — but NOT libswsscommon's own closure (`libzmq.so.5`, `libnl-*`, …),
    // which the loader would then look for on the default path only, aborting with
    // `libzmq.so.5: cannot open shared object file`. DT_RPATH, by contrast, is
    // searched transitively for the whole load chain, so the entire staged closure
    // in `/swsslib` resolves without relying on `LD_LIBRARY_PATH`. `--disable-new-dtags`
    // must precede `-rpath` on the linker line, so it is emitted first.
    //
    // Baked at link time, this is immune to build-script fingerprint caching (it
    // applies on every `cargo test`, not only when build.rs re-runs), and it is
    // inert for the pmon deploy where `/swsslib` does not exist (the loader simply
    // skips a missing RPATH dir and resolves from the system path / ld.so.cache).
    println!("cargo:rustc-link-arg=-Wl,--disable-new-dtags");
    println!("cargo:rustc-link-arg=-Wl,-rpath,{SWSSLIB}");
    // Re-run only if the staged swss-common lib changes (keeps normal edits fast).
    println!("cargo:rerun-if-changed={SWSSCOMMON_SO}");

    stage_runtime_closure();
}

/// Best-effort: ensure `libswsscommon`'s runtime closure sits in `/swsslib`.
/// Any failure is swallowed (a `cargo:warning` is emitted) — this must never break
/// a build that only compiles or that runs in an environment where the libs already
/// exist.
fn stage_runtime_closure() {
    let swsslib = Path::new(SWSSLIB);
    // `/swsslib` only exists inside the build/test container (harness bind-mount).
    if !swsslib.exists() || !Path::new(SWSSCOMMON_SO).exists() {
        return;
    }
    // Already staged (persistent host mount) — nothing to do.
    if swsslib.join("libzmq.so.5").exists() {
        return;
    }

    // Install the closure into the container so the SONAMEs resolve, then persist it.
    let _ = Command::new("apt-get").args(["update", "-qq"]).status();
    let installed = Command::new("apt-get")
        .args(["install", "-y", "--no-install-recommends"])
        .args(CLOSURE_PKGS)
        .status()
        .map(|s| s.success())
        .unwrap_or(false);
    if !installed {
        println!(
            "cargo:warning=xcvrd-rs build.rs: could not apt-install libswsscommon \
             runtime deps; `cargo test` may fail to load libswsscommon.so.0 \
             (compile-only builds and the pmon deploy are unaffected)"
        );
        return;
    }
    stage_closure_beside_swsscommon(swsslib);
}

/// Copy every non-base shared object in `libswsscommon.so.0`'s (recursive) `ldd`
/// closure into `/swsslib`, named by its SONAME, so future `--rm` container runs
/// resolve the whole tree from the RUNPATH without re-installing.
fn stage_closure_beside_swsscommon(swsslib: &Path) {
    let out = match Command::new("ldd").arg(SWSSCOMMON_SO).output() {
        Ok(o) if o.status.success() => o,
        _ => return,
    };
    for line in String::from_utf8_lossy(&out.stdout).lines() {
        // "\tlibzmq.so.5 => /usr/lib/x86_64-linux-gnu/libzmq.so.5 (0x...)"
        let Some((name, rest)) = line.trim().split_once(" => ") else {
            continue;
        };
        let name = name.trim();
        if name.is_empty() || BASE_LIBS.contains(&name) {
            continue;
        }
        let Some(src) = rest.split_whitespace().next() else {
            continue;
        };
        let src = PathBuf::from(src);
        if src.as_os_str().is_empty() || !src.exists() {
            continue;
        }
        let dst = swsslib.join(name);
        if dst.exists() {
            continue;
        }
        // `fs::copy` follows the SONAME symlink and writes a real file whose internal
        // SONAME still matches `name`, so the loader resolves it from the RUNPATH.
        let _ = std::fs::copy(&src, &dst);
    }
}
