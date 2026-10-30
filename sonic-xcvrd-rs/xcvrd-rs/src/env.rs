//! Environment seed for the daemon: thin, documented constructors for the two
//! bindings so there is a single place to obtain a HAL handle and a STATE_DB
//! connection. This is the real platform/DB layer (per-port SFP handles, table
//! helpers, publish/subscribe loops, …) — start here rather than calling the raw
//! bindings ad hoc.

use platform_provider::{Platform, PlatformImpl};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use swss_common::DbConnector;

use crate::error::{Result, XcvrdError};

const GLOBAL_DB_CONFIG: &str = "/var/run/redis/sonic-db/database_global.json";

/// STATE_DB hash the emulator's test-only SFP-error injection hook reads (mirrors
/// `sonic_platform.chassis.INJECT_TABLE`). Probed by [`init_embedded_db_config`] to prove the
/// embedded interpreter's STATE_DB read path (the emulator's `Chassis._get_statedb`) is warm.
const XCVR_EMU_INJECT: &str = "XCVR_EMU_INJECT";

/// SONiC STATE_DB logical index (Redis db number).
pub const STATE_DB: i32 = 6;

/// SONiC CONFIG_DB logical index (Redis db number).
pub const CONFIG_DB: i32 = 4;

/// SONiC APPL_DB logical index (Redis db number). Source of the `PORT_TABLE`
/// `flap_count` field the DOM task watches for link-change flag re-reads.
pub const APPL_DB: i32 = 0;

/// Redis unix socket path inside pmon (override with the `REDIS_SOCK` env var).
pub fn redis_sock() -> String {
    std::env::var("REDIS_SOCK").unwrap_or_else(|_| "/var/run/redis/redis.sock".to_string())
}

/// Open the transceiver HAL: PyO3 → `sonic_platform.Platform().get_chassis()`.
///
/// The shared provider owns vendor initialization and Python platform access.
pub fn open_platform() -> Result<Platform> {
    Ok(platform_provider::open("xcvrd", PlatformImpl::Pyo3)?)
}

/// Discover front-end namespaces and initialize multi-ASIC DB configuration.
pub fn front_end_namespaces() -> Result<Vec<String>> {
    let (multi_asic, namespaces) = Python::with_gil(|py| -> PyResult<(bool, Vec<String>)> {
        let multi = py.import("sonic_py_common.multi_asic")?;
        let is_multi = multi.call_method0("is_multi_asic")?.extract::<bool>()?;
        if is_multi {
            py.import("swsscommon.swsscommon")?
                .getattr("SonicDBConfig")?
                .call_method0("initializeGlobalConfig")?;
        }
        Ok((
            is_multi,
            multi.call_method0("get_front_end_namespaces")?.extract()?,
        ))
    })
    .map_err(|e| XcvrdError::Bridge(e.to_string()))?;
    if multi_asic {
        swss_common::sonic_db_config_initialize_global(GLOBAL_DB_CONFIG, false)?;
    }
    for namespace in &namespaces {
        namespace_asic_id(namespace)?;
    }
    Ok(namespaces)
}

/// SONiC ASIC index, including non-contiguous front-end ASICs.
pub fn namespace_asic_id(namespace: &str) -> Result<usize> {
    if namespace.is_empty() {
        return Ok(0);
    }
    namespace
        .strip_prefix("asic")
        .and_then(|index| index.parse().ok())
        .ok_or_else(|| XcvrdError::Other(format!("invalid ASIC namespace {namespace:?}")))
}

fn open_db(db_name: &str, db_id: i32, namespace: &str) -> swss_common::Result<DbConnector> {
    if namespace.is_empty() {
        if let Ok(sock) = std::env::var("REDIS_SOCK") {
            return DbConnector::new_unix(db_id, sock, 0);
        }
        DbConnector::new_named(db_name, false, 0)
    } else {
        DbConnector::new_keyed(db_name, false, 0, "", namespace)
    }
}

/// Open the default namespace's STATE_DB.
///
/// Use the returned [`DbConnector`] for direct hash access (`hset`/`hgetall`/…),
/// or wrap it in a `swss_common::Table` / `ProducerStateTable` for table-scoped
/// writes like `TRANSCEIVER_INFO`.
pub fn open_state_db() -> swss_common::Result<DbConnector> {
    open_state_db_in_namespace("")
}

/// Open STATE_DB using the namespace's SONiC database configuration.
pub fn open_state_db_in_namespace(namespace: &str) -> swss_common::Result<DbConnector> {
    open_db("STATE_DB", STATE_DB, namespace)
}

/// Open a CONFIG_DB connection over the Redis unix socket (for the PORT table /
/// logical↔physical port mapping).
pub fn open_config_db() -> swss_common::Result<DbConnector> {
    open_config_db_in_namespace("")
}

/// Open CONFIG_DB using the namespace's SONiC database configuration.
pub fn open_config_db_in_namespace(namespace: &str) -> swss_common::Result<DbConnector> {
    open_db("CONFIG_DB", CONFIG_DB, namespace)
}

/// Open an APPL_DB connection over the Redis unix socket (for the `PORT_TABLE`
/// `flap_count` link-change watch).
pub fn open_appl_db() -> swss_common::Result<DbConnector> {
    open_appl_db_in_namespace("")
}

/// Open APPL_DB using the namespace's SONiC database configuration.
pub fn open_appl_db_in_namespace(namespace: &str) -> swss_common::Result<DbConnector> {
    open_db("APPL_DB", APPL_DB, namespace)
}

/// Force-load the embedded interpreter's process-global `swsscommon.SonicDBConfig` so the
/// emulator's `Chassis._get_statedb` (`SonicV2Connector(use_unix_socket_path=True)`) can
/// resolve STATE_DB's unix socket and surface injected SFP errors
/// (`tests/test_status_error.py`).
///
/// Explicitly initialize the Python bindings as well as the native Rust DB layer
/// (including when `REDIS_SOCK` bypasses named connection initialization), then verify
/// the emulator's exact read path
/// (`SonicV2Connector(use_unix_socket_path=True).connect('STATE_DB').get_all(_, 'XCVR_EMU_INJECT')`)
/// so a `true` return means that identical later call inside `_get_statedb` cannot fail for
/// the `SonicDBConfig` reason. Bounded-retried (a just-restarted xcvrd can briefly race
/// redis/config readiness even though [`open_state_db`] already proved redis is up); every
/// distinct failure is logged. Best-effort and never fatal — a missing `swsscommon`/redis must
/// never take the daemon down; the caller primes the change-event baseline regardless.
pub fn init_embedded_db_config() -> bool {
    // Bounded retry budget for the cold-boot race where `redis` (the `database`
    // container) is not yet reachable the instant xcvrd starts inside `pmon`. The
    // reference daemon's by-name `db_connect(waitForDbInit=true)` (daemon_base) BLOCKS
    // until redis is up, so a too-short budget here is the one runtime way the
    // emulator's first `_get_statedb` fail-caches `False` for the chassis lifetime and
    // every injected SFP error is then silenced (tests/test_status_error.py). Mirror
    // the reference's resilience with a generous *bounded* budget instead of giving up
    // in ~6 s. When redis IS reachable (the steady/e2e case) attempt 1 succeeds
    // immediately, so this NEVER delays a healthy boot. Overridable (attempt count)
    // via `XCVRD_DBCFG_WARM_ATTEMPTS` to tune without a rebuild.
    const DEFAULT_ATTEMPTS: usize = 100; // ~30 s at 300 ms/attempt
    let attempts = std::env::var("XCVRD_DBCFG_WARM_ATTEMPTS")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .filter(|n| *n > 0)
        .unwrap_or(DEFAULT_ATTEMPTS);
    let mut last_err: Option<String> = None;
    for attempt in 1..=attempts {
        let res = Python::with_gil(|py| -> PyResult<()> {
            // Same module the emulator imports: `from swsscommon.swsscommon import ...`.
            let sc = py.import("swsscommon.swsscommon")?;
            // Best-effort: load the process-global `SonicDBConfig` singleton so by-name
            // resolution works. Swallowed inside; the probe below is the authority.
            load_embedded_sonic_db_config(&sc);
            // Authoritative go/no-go: reproduce the emulator's EXACT read path
            // (`chassis.py::_get_statedb` + `_read_injections`). If this succeeds here, the
            // emulator's own first call cannot fail-cache for the `SonicDBConfig` reason.
            let kwargs = PyDict::new(py);
            kwargs.set_item("use_unix_socket_path", true)?;
            let conn = sc.getattr("SonicV2Connector")?.call((), Some(&kwargs))?;
            conn.call_method1("connect", ("STATE_DB",))?;
            conn.call_method1("get_all", ("STATE_DB", XCVR_EMU_INJECT))?;
            Ok(())
        });
        match res {
            Ok(()) => {
                eprintln!(
                    "xcvrd-rs: embedded swsscommon SonicDBConfig loaded; STATE_DB reachable for \
                     the emulator SFP-error injection reader (attempt {attempt}/{attempts})"
                );
                return true;
            }
            Err(e) => {
                // Log every DISTINCT failure (deduplicated) so an e2e run reveals
                // whether — and why — the emulator's STATE_DB read path is not warming (the
                // sole runtime reason an injected SFP error would never surface).
                let msg = e.to_string();
                if last_err.as_deref() != Some(msg.as_str()) {
                    eprintln!(
                        "xcvrd-rs: init_embedded_db_config: STATE_DB read-path probe not yet warm \
                         (attempt {attempt}/{attempts}): {msg}"
                    );
                    last_err = Some(msg);
                    // First failure: dump a one-time env snapshot (is the default
                    // database_config.json where SonicDBConfig::initialize() looks?).
                    if attempt == 1 {
                        log_db_config_environment();
                    }
                }
                if attempt == attempts {
                    eprintln!(
                        "xcvrd-rs: init_embedded_db_config: could not prove the embedded STATE_DB \
                         read path warm after {attempts} attempts; priming the change-event \
                         baseline anyway (best-effort)"
                    );
                } else {
                    std::thread::sleep(std::time::Duration::from_millis(300));
                }
            }
        }
    }
    false
}

/// One-time diagnostic dumped on the first [`init_embedded_db_config`] failure: reports whether
/// the standard SONiC `database_config.json` is present at the default path the C++
/// `SonicDBConfig::initialize()` reads, plus the DB-path env the binary inherited — so a
/// config-load failure is diagnosable at a glance.
fn log_db_config_environment() {
    const DEFAULT_DB_CONFIG: &str = "/var/run/redis/sonic-db/database_config.json";
    let exists = std::path::Path::new(DEFAULT_DB_CONFIG).exists();
    let db_config_path = std::env::var("DB_CONFIG_PATH").unwrap_or_else(|_| "<unset>".into());
    let global_cfg = std::env::var("SONIC_DB_GLOBAL_CONFIG").unwrap_or_else(|_| "<unset>".into());
    eprintln!(
        "xcvrd-rs: init_embedded_db_config: db-config env snapshot — {DEFAULT_DB_CONFIG} \
         exists={exists}, DB_CONFIG_PATH={db_config_path}, SONIC_DB_GLOBAL_CONFIG={global_cfg}"
    );
}

/// Force-load the process-global `swsscommon.SonicDBConfig`. Every step is best-effort (errors
/// swallowed): the goal is that AT LEAST ONE strategy loads the singleton, and the caller's
/// `SonicV2Connector` probe is the authority on whether it worked. `sc` is `swsscommon.swsscommon`.
fn load_embedded_sonic_db_config(sc: &Bound<'_, PyModule>) {
    // Faithful to the reference `DaemonXcvrd.init()` (`xcvrd.py:1046-1048`): load the multi-ASIC
    // namespace map via `SonicDBConfig.initializeGlobalConfig()` ONLY when
    // `multi_asic.is_multi_asic()`. Calling it UNCONDITIONALLY on single-ASIC is NOT a harmless
    // no-op: with `SonicDBConfig` not yet initialized, it flips the singleton into
    // global/namespace-resolution mode WITHOUT a `database_global.json`, so the emulator's later
    // `SonicV2Connector(...)` can no longer resolve STATE_DB and `_get_statedb` fail-caches. Gate
    // it exactly like the reference so single-ASIC takes the clean by-name lazy load below.
    let py = sc.py();
    let is_multi_asic = py
        .import("sonic_py_common.multi_asic")
        .and_then(|m| m.call_method0("is_multi_asic")?.extract::<bool>())
        .unwrap_or(false);
    if is_multi_asic {
        if let Ok(cfg) = sc.getattr("SonicDBConfig") {
            let _ = cfg.call_method0("initializeGlobalConfig");
        }
    }
    // Reproduce the config-load SIDE EFFECT of the reference daemon's by-name `db_connect`
    // (`xcvrd.py:918` → `daemon_base.py:29`, `swsscommon.DBConnector("APPL_DB", 0, true)`):
    // resolving the db NAME force-loads `SonicDBConfig` from the local `database_config.json`
    // exactly as the reference does — the SAME lazy load the emulator's
    // `SonicV2Connector(use_unix_socket_path=True)` performs on its first `_get_statedb`. We pass
    // `waitForDbInit=false` (vs the reference's `true`) because we only need the load side effect,
    // not the connection, so it can never block the daemon at boot on a db not yet flagged
    // "initialized". Targets APPL_DB (as the reference does) and STATE_DB (what the emulator
    // reader actually uses) as independent fallbacks.
    if let Ok(dbc) = sc.getattr("DBConnector") {
        let _ = dbc.call1(("APPL_DB", 0i32, false));
        let _ = dbc.call1(("STATE_DB", 0i32, false));
    }
}

/// Decisive boot diagnostic for the emulator's SFP-error **delivery** path. The injected-error
/// tests (`tests/test_status_error.py`) surface a hardware error only when the emulator
/// `Chassis` reads the `XCVR_EMU_INJECT` STATE_DB hash, and that read has TWO independent
/// gates:
///   1. the process-global `swsscommon.SonicDBConfig` resolves + STATE_DB is reachable, so the
///      emulator's `_get_statedb` (`SonicV2Connector(use_unix_socket_path=True).connect(...)`)
///      connects on its first call. [`init_embedded_db_config`] verifies this path
///      before polling, since a failed first `_get_statedb` is cached for the chassis lifetime and every
///      injected error is silenced;
///   2. the `.test_hooks` MARKER is present next to the `sonic_platform.chassis` module the
///      embedded interpreter actually imported — the deploy drops it and the `Chassis`
///      reads it ONCE at construction into `self._test_hooks`. If the marker is absent (or the
///      interpreter imported `chassis.py` from a different path than the deploy patched),
///      `_read_injections` short-circuits to `{}` with NO STATE_DB access at all — so an
///      injected error can never surface even though gate 1 is green and live presence still
///      works over gRPC.
/// This logs where `chassis.py` was imported from, the marker path it derives, whether that
/// marker exists, and whether `swsscommon` imports — so an e2e run can tell a
/// config/redis failure (gate 1) apart from a marker/import-path failure (gate 2) at a glance.
/// Cheap: module-level attribute reads + one `os.path.exists`, no emulator RPC. Best-effort.
pub fn log_emulator_delivery_preconditions() {
    let _ = Python::with_gil(|py| -> PyResult<()> {
        let chassis_mod = py.import("sonic_platform.chassis")?;
        let file: String = chassis_mod
            .getattr("__file__")
            .and_then(|f| f.extract())
            .unwrap_or_else(|_| "<unknown>".to_string());
        let marker: String = chassis_mod
            .getattr("TEST_HOOKS_MARKER")
            .and_then(|m| m.extract())
            .unwrap_or_else(|_| "<unknown>".to_string());
        let marker_exists = py
            .import("os")
            .and_then(|os| {
                os.getattr("path")?
                    .call_method1("exists", (&marker,))?
                    .extract()
            })
            .unwrap_or(false);
        let swss_importable = py.import("swsscommon.swsscommon").is_ok();
        eprintln!(
            "xcvrd-rs: emulator SFP-error delivery preconditions — sonic_platform.chassis={file}, \
             test_hooks_marker={marker}, marker_exists={marker_exists} (false => injected errors \
             are inert: no STATE_DB access, so tests/test_status_error.py cannot pass — the deploy \
             must drop .test_hooks next to THIS chassis.py), swsscommon_importable={swss_importable}"
        );
        Ok(())
    });
}
