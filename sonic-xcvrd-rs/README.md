# Rust xcvrd

An opt-in Rust implementation of the SONiC transceiver daemon. The workspace
contains `xcvrd-rs` (the daemon) and `platform-bridge` (PyO3 bindings to the
existing Python `sonic_platform` HAL and transceiver decoders).

The Python package in `../sonic-xcvrd`, its wheel packaging, and pmon's default
supervisor configuration are unchanged. This workspace does not automatically
replace the Python daemon.

## Source and integrated fixes

Imported from `BYGX-wcr/sonic-dev` at commit
`da6e848`, using `recodeAgent/results/result_4/crate` and all eight ordered patches
from `recodeAgent/tools/deploy-patches`. The fixes and their regression tests are
part of the source; no deployment-time patch application is needed.

| Patch | Integrated change |
| --- | --- |
| 0001 | Build the initial port mapping from CONFIG_DB `PORT` indexes. |
| 0002 | Backfill missing CMIS speed/lanes when observer events arrive out of order. |
| 0003 | Find breakout siblings through the authoritative port mapping. |
| 0004 | Use direct CMIS/SFF decoder reads, with cached aggregate fallbacks. |
| 0005 | Cover eight-subport breakout decommission decisions. |
| 0006 | Confirm failed presence reads before declaring a module removed. |
| 0007 | Periodically recover present modules stuck in `REMOVED`. |
| 0008 | Use CONFIG_DB indexes for runtime port changes as well as startup. |

## Build and test

Build on Linux with Rust/Cargo, CPython development headers, clang/libclang,
pkg-config, and SONiC's `libswsscommon` library and runtime dependencies.
`SWSS_COMMON_REPO` must point to a `sonic-swss-common` checkout at
`7faca59cd30367692a8c5406ad5d7c0b5341c7ef`, matching the pinned Cargo dependency.
Bindgen needs the C API headers, not the C++ headers:

```sh
cd sonic-xcvrd-rs
export SWSS_COMMON_REPO=/absolute/path/to/sonic-swss-common
export BINDGEN_EXTRA_CLANG_ARGS="-x c"
cargo fmt --all -- --check
cargo test --locked --workspace
cargo clippy --locked --workspace --all-targets
cargo build --locked --release --bin xcvrd-rs
```

Tests use mock HAL/DB implementations and need neither a switch nor Redis, but
their executables still dynamically link CPython and `libswsscommon`.

The optional Redis integration test starts isolated Unix-socket servers and also
requires `redis-server` and SWSS's Lua scripts in `/usr/share/swss`:

```sh
cargo test --locked -p xcvrd-rs --test redis_tables -- --ignored
```

The included `Dockerfile` supplies the Trixie build dependencies and pinned C API
checkout. Use `Dockerfile.bookworm` instead when targeting a Bookworm pmon.
Build against the same Debian release and Python ABI as the target container:

```sh
docker build -t xcvrd-rs-build .
export SWSSLIB=/absolute/path/to/staged/pmon-libraries
docker run --rm --network host \
    -v "$PWD":/src -v "$SWSSLIB":/swsslib -w /src \
    -e RUSTFLAGS="-L native=/swsslib" \
    xcvrd-rs-build cargo test --locked --workspace
```

The staged directory must contain `libswsscommon.so`, `libswsscommon.so.0`, and
the matching runtime dependency closure. For Trixie container tests, `build.rs`
can best-effort install and stage that closure into the writable `/swsslib`
mount. For a release binary, replace the final command above with
`cargo build --locked --release --bin xcvrd-rs`.

## Runtime

Run `target/release/xcvrd-rs` inside a matching SONiC pmon environment, with
`sonic_platform`, `sonic_platform_base`, `sonic_py_common`, and `swsscommon`
importable by the embedded interpreter. Database endpoints and separators come
from SONiC's database configuration, with front-end ASIC namespaces discovered
through `sonic_py_common.multi_asic`. `REDIS_SOCK` overrides the socket only for
the default (single-ASIC) namespace.

Older images store `status`, `error`, and `cmis_state` together with hardware
status in `TRANSCEIVER_STATUS`. Set `XCVRD_LEGACY_STATUS_TABLE=1` for the Rust
process on those images to preserve that schema. Leave it unset for images
using the separate `TRANSCEIVER_STATUS_SW` table.

The linked SWSS library must export the SWSS C API. Older C++-only libraries
cannot be used directly; any compatibility runtime must remain private to the
Rust process rather than replacing the platform's shared library.

Use the existing `xcvrd` supervisor slot and its usual arguments when explicitly
opting into the Rust daemon. Stop Python xcvrd first: never run both daemons
against the same hardware and databases. Build/test containers alone do not
provide the platform plugin or a running SONiC environment.
