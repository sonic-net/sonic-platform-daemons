//! Configuration ycabled reads: the platform, which ToR this is (Loopback3),
//! the SoC channel settings (GRPCCLIENT, grpc_secrets.json) and XCVRD_LOG.

use std::collections::HashMap;
use std::net::Ipv4Addr;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};

use serde_json::Value;

use crate::db::{Db, DbId, FieldMap};
use crate::logger::{self, Level};
use crate::schema::*;
use crate::{log_debug, log_error, log_notice, log_warning};

pub const VS_PLATFORM: &str = "x86_64-kvm_x86_64-r0";
pub const SECRETS_PATH: &str = "/etc/sonic/grpc_secrets.json";

/// Loopback3 addresses of the upper ToR (read side 0) and the lower ToR (1).
const LOOPBACK_UPPER: [&str; 2] = ["10.212.64.2/32", "10.1.0.39/32"];
const LOOPBACK_LOWER: [&str; 2] = ["10.212.64.1/32", "10.1.0.38/32"];

/// Which ToR this is, from the first IPv4 address of Loopback3 in
/// CONFIG_DB LOOPBACK_INTERFACE: 0 for the upper ToR, 1 for the lower, -1 if
/// unknown. That is the index of this ToR's port in the SoC's port lists.
pub fn read_side_from_loopback_keys(keys: &[String]) -> i32 {
    for key in keys {
        log_debug!("Y_CABLE_DEBUG:Loopback key = {} ", key);
        if !(key.starts_with("Loopback3|") && key.contains('/') && !key.contains("::")) {
            continue;
        }
        let addr = key
            .split('|')
            .nth(1)
            .unwrap_or("")
            .split('/')
            .next()
            .unwrap_or("");
        let Ok(ip) = addr.parse::<Ipv4Addr>() else {
            log_warning!("Could not get any address associated with Loopback3");
            return -1;
        };
        let network = format!("{ip}/32");
        log_debug!("Y_CABLE_DEBUG:Loopback address parsed = {} ", network);
        if LOOPBACK_UPPER.contains(&network.as_str()) {
            return 0;
        }
        if LOOPBACK_LOWER.contains(&network.as_str()) {
            return 1;
        }
        log_warning!("Could not get any address associated with Loopback3");
        return -1;
    }
    -1
}

pub async fn read_side(db: &dyn Db) -> i32 {
    read_side_from_loopback_keys(&db.keys(DbId::Config, LOOPBACK_INTERFACE).await)
}

/// DEVICE_METADATA|localhost: the platform and the HwSKU.
#[derive(Clone, Debug, Default)]
pub struct DeviceInfo {
    pub platform: String,
    pub hwsku: String,
}

impl DeviceInfo {
    pub fn is_vs(&self) -> bool {
        self.platform == VS_PLATFORM
    }
}

pub async fn device_info(db: &dyn Db) -> DeviceInfo {
    let row = db
        .get(DbId::Config, DEVICE_METADATA, "localhost")
        .await
        .unwrap_or_default();
    let env_platform = std::env::var("PLATFORM").unwrap_or_default();
    DeviceInfo {
        platform: row.get("platform").cloned().unwrap_or(env_platform),
        hwsku: row.get("hwsku").cloned().unwrap_or_default(),
    }
}

/// How to reach a SoC, from CONFIG_DB GRPCCLIENT.
#[derive(Clone, Debug, PartialEq)]
pub enum ChannelConfig {
    Insecure,
    Secure(TlsSettings),
}

#[derive(Clone, Debug, PartialEq)]
pub struct TlsSettings {
    pub ca_pem: Vec<u8>,
    /// Certificate and key, for mutual authentication.
    pub identity: Option<(Vec<u8>, Vec<u8>)>,
    /// The name the SoC's certificate is checked against (grpc_ssl_credential).
    pub target_name: String,
}

fn read_file(path: Option<&String>) -> Option<Vec<u8>> {
    let p = path?;
    if !Path::new(p).is_file() {
        return None;
    }
    std::fs::read(p).ok()
}

/// The channel settings in GRPCCLIENT `config` and `certs`, read as Python
/// ycabled reads them when it sets up a channel. None: no channel can be
/// set up (the reason is logged).
pub fn channel_config(
    config: Option<&FieldMap>,
    certs: Option<&FieldMap>,
    port: &str,
) -> Option<ChannelConfig> {
    // Without a config entry the channel is plaintext.
    let (kind, level) = match config {
        None => {
            log_debug!(
                "Could not retreive fieldvalue pairs for {}, inside config_db table kvp config for GRPCCLIENT for setting up channel type",
                port
            );
            (Some("insecure".to_string()), Some("server".to_string()))
        }
        Some(c) => (c.get("type").cloned(), c.get("auth_level").cloned()),
    };
    if kind.as_deref() != Some("secure") {
        return Some(ChannelConfig::Insecure);
    }
    let Some(kvp) = certs else {
        log_debug!(
            "Could not retreive fieldvalue pairs for {}, inside config_db table kvp certs for GRPCCLIENT for setting up channel type",
            port
        );
        return None;
    };
    let Some(ca_pem) = read_file(kvp.get("ca_crt")) else {
        log_error!("grpc credential channel setup no root file in config_db");
        return None;
    };
    let identity = match level.as_deref() {
        // sonic-grpcclient.yang names mutual authentication "client".
        Some("mutual") | Some("client") => {
            let Some(cert) = read_file(kvp.get("client_crt")) else {
                log_error!("grpc credential channel setup no cert file for mutual authentication in config_db");
                return None;
            };
            let Some(key) = read_file(kvp.get("client_key")) else {
                log_error!("grpc credential channel setup no key file for mutual authentication in config_db");
                return None;
            };
            Some((cert, key))
        }
        Some("server") => None,
        _ => {
            log_error!(
                "grpc credential channel setup no type specified for authentication in config_db"
            );
            return None;
        }
    };
    let target_name = kvp.get("grpc_ssl_credential").cloned()?;
    Some(ChannelConfig::Secure(TlsSettings {
        ca_pem,
        identity,
        target_name,
    }))
}

pub async fn read_channel_config(db: &dyn Db, port: &str) -> Option<ChannelConfig> {
    let config = db.get(DbId::Config, GRPCCLIENT, "config").await;
    let certs = db.get(DbId::Config, GRPCCLIENT, "certs").await;
    channel_config(config.as_ref(), certs.as_ref(), port)
}

fn json_to_field(v: &Value) -> Option<String> {
    match v {
        Value::Null => None,
        Value::String(s) => Some(s.clone()),
        Value::Bool(true) => Some("True".into()),
        Value::Bool(false) => Some("False".into()),
        other => Some(other.to_string()),
    }
}

/// The GRPCCLIENT fields to add from grpc_secrets.json: for `config` and
/// `certs`, those CONFIG_DB lacks and the file does not set to null.
pub fn secrets_to_apply(
    secrets: &Value,
    existing: &HashMap<String, FieldMap>,
) -> Vec<(String, Vec<(String, String)>)> {
    let mut out = Vec::new();
    let Some(client) = secrets.get("GRPCCLIENT") else {
        return out;
    };
    for key in ["config", "certs"] {
        let Some(Value::Object(section)) = client.get(key) else {
            continue;
        };
        let configured = existing.get(key);
        let missing: Vec<(String, String)> = section
            .iter()
            .filter(|(f, _)| configured.map_or(true, |c| !c.contains_key(*f)))
            .filter_map(|(f, v)| json_to_field(v).map(|v| (f.clone(), v)))
            .collect();
        if !missing.is_empty() {
            out.push((key.to_string(), missing));
        }
    }
    out
}

/// Copies what CONFIG_DB lacks from /etc/sonic/grpc_secrets.json, if it exists.
pub async fn apply_grpc_secrets(db: &dyn Db, path: &str) {
    if !Path::new(path).is_file() {
        return;
    }
    let secrets: Value = match std::fs::read_to_string(path)
        .map_err(|e| e.to_string())
        .and_then(|t| serde_json::from_str(&t).map_err(|e| e.to_string()))
    {
        Ok(v) => v,
        Err(e) => {
            log_error!("Failed to read {}: {}", path, e);
            return;
        }
    };
    let mut existing = HashMap::new();
    for key in ["config", "certs"] {
        if let Some(row) = db.get(DbId::Config, GRPCCLIENT, key).await {
            existing.insert(key.to_string(), row);
        }
    }
    for (key, fields) in secrets_to_apply(&secrets, &existing) {
        let fv: Vec<(&str, String)> = fields
            .iter()
            .map(|(f, v)| (f.as_str(), v.clone()))
            .collect();
        db.set(DbId::Config, GRPCCLIENT, &key, &fv).await;
    }
}

/// `XCVRD_LOG|Y_CABLE` `disable_telemetry`.
static TELEMETRY_DISABLED: AtomicBool = AtomicBool::new(false);

pub fn telemetry_disabled() -> bool {
    TELEMETRY_DISABLED.load(Ordering::Relaxed)
}

pub fn set_telemetry_disabled(v: bool) {
    TELEMETRY_DISABLED.store(v, Ordering::Relaxed)
}

/// An `XCVRD_LOG` change: `log_verbosity` (debug or notice) and
/// `disable_telemetry` (True or False) of the Y_CABLE entry. Other keys
/// belong to other daemons.
pub fn handle_xcvrd_log(key: &str, fields: &FieldMap) {
    log_notice!("Y_CABLE_DEBUG: trying to enable/disable debug logs");
    if key != "Y_CABLE" {
        return;
    }
    match fields.get("log_verbosity").map(String::as_str) {
        Some("debug") => logger::set_min_level(Level::Debug),
        Some("notice") => logger::set_min_level(Level::Notice),
        _ => {}
    }
    if let Some(v) = fields.get("disable_telemetry") {
        log_notice!(
            "Y_CABLE_DEBUG: trying to enable/disable telemetry flag to {}",
            v
        );
        match v.as_str() {
            "True" => set_telemetry_disabled(true),
            "False" => set_telemetry_disabled(false),
            _ => {}
        }
    }
}

/// The cable type of a MUX_CABLE entry, as Python's
/// check_mux_cable_port_type decides it. None: not a mux port ycabled
/// serves (no entry, no state, or a state outside the YANG enum).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CableType {
    ActiveStandby,
    ActiveActive,
}

impl CableType {
    pub fn as_str(self) -> &'static str {
        match self {
            CableType::ActiveStandby => "active-standby",
            CableType::ActiveActive => "active-active",
        }
    }
}

pub fn cable_type(port: &str, row: Option<&FieldMap>) -> Option<CableType> {
    let row = row?;
    let Some(state) = row.get("state") else {
        log_debug!(
            "Y_CABLE_DEBUG:check_mux_cable_port_type returning False None port {}",
            port
        );
        return None;
    };
    if !CONFIG_MUX_STATES.contains(&state.as_str()) {
        log_warning!(
            "Could not retreive active or auto value for state kvp for {}, inside MUX_CABLE table",
            port
        );
        return None;
    }
    if row.get("cable_type").map(String::as_str) == Some("active-active") {
        Some(CableType::ActiveActive)
    } else {
        Some(CableType::ActiveStandby)
    }
}

/// The SoC address of a MUX_CABLE entry: soc_ipv4, else soc_ipv6, without
/// the prefix length. Err: the address, which is not valid.
pub fn soc_ip(row: &FieldMap) -> Result<Option<std::net::IpAddr>, String> {
    for field in ["soc_ipv4", "soc_ipv6"] {
        let Some(full) = row.get(field) else { continue };
        let ip = full.split('/').next().unwrap_or("");
        return match ip.parse::<std::net::IpAddr>() {
            Ok(a) => Ok(Some(a)),
            Err(_) => {
                log_warning!("Invalid {} address {} for gRPC channel setup", field, full);
                Err(full.clone())
            }
        };
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn keys(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    fn row(v: &[(&str, &str)]) -> FieldMap {
        v.iter()
            .map(|(a, b)| (a.to_string(), b.to_string()))
            .collect()
    }

    #[test]
    fn read_side_mapping() {
        assert_eq!(
            read_side_from_loopback_keys(&keys(&["Loopback3", "Loopback3|10.212.64.2/32"])),
            0
        );
        assert_eq!(
            read_side_from_loopback_keys(&keys(&["Loopback3|10.1.0.39/32"])),
            0
        );
        assert_eq!(
            read_side_from_loopback_keys(&keys(&["Loopback3|10.212.64.1/32"])),
            1
        );
        assert_eq!(
            read_side_from_loopback_keys(&keys(&[
                "Loopback3|FC00:1:0:38::/128",
                "Loopback3|10.1.0.38/32"
            ])),
            1
        );
        assert_eq!(
            read_side_from_loopback_keys(&keys(&["Loopback3|10.212.64.9/32"])),
            -1
        );
        assert_eq!(
            read_side_from_loopback_keys(&keys(&["Loopback3|fc00::1/128"])),
            -1
        );
        assert_eq!(
            read_side_from_loopback_keys(&keys(&["Loopback0|10.212.64.2/32"])),
            -1
        );
        assert_eq!(read_side_from_loopback_keys(&[]), -1);
    }

    #[test]
    fn cable_types() {
        assert_eq!(
            cable_type("E0", Some(&row(&[("state", "auto")]))),
            Some(CableType::ActiveStandby)
        );
        assert_eq!(
            cable_type(
                "E0",
                Some(&row(&[
                    ("state", "active"),
                    ("cable_type", "active-active")
                ]))
            ),
            Some(CableType::ActiveActive)
        );
        assert_eq!(
            cable_type("E0", Some(&row(&[("cable_type", "active-active")]))),
            None
        );
        assert_eq!(cable_type("E0", Some(&row(&[("state", "bogus")]))), None);
        assert_eq!(cable_type("E0", None), None);
        for s in CONFIG_MUX_STATES {
            assert!(
                cable_type("E0", Some(&row(&[("state", s)]))).is_some(),
                "{s}"
            );
        }
    }

    #[test]
    fn soc_addresses() {
        let v4 = row(&[("soc_ipv4", "192.168.0.7/32"), ("soc_ipv6", "fd00::7/128")]);
        assert_eq!(soc_ip(&v4), Ok(Some("192.168.0.7".parse().unwrap())));
        assert_eq!(
            soc_ip(&row(&[("soc_ipv6", "::1/128")])),
            Ok(Some("::1".parse().unwrap()))
        );
        assert_eq!(
            soc_ip(&row(&[("soc_ipv4", "10.1.1/32")])),
            Err("10.1.1/32".to_string())
        );
        assert_eq!(soc_ip(&row(&[("state", "auto")])), Ok(None));
    }

    #[test]
    fn channel_config_variants() {
        assert_eq!(
            channel_config(None, None, "E0"),
            Some(ChannelConfig::Insecure)
        );
        assert_eq!(
            channel_config(Some(&row(&[("auth_level", "server")])), None, "E0"),
            Some(ChannelConfig::Insecure)
        );
        assert_eq!(
            channel_config(Some(&row(&[("type", "insecure")])), None, "E0"),
            Some(ChannelConfig::Insecure)
        );
        // Secure needs certs.
        assert_eq!(
            channel_config(
                Some(&row(&[("type", "secure"), ("auth_level", "server")])),
                None,
                "E0"
            ),
            None
        );
        let dir = tempfile::tempdir().unwrap();
        let ca = dir.path().join("ca.crt");
        std::fs::write(&ca, b"CA").unwrap();
        let crt = dir.path().join("c.crt");
        std::fs::write(&crt, b"CRT").unwrap();
        let key = dir.path().join("c.key");
        std::fs::write(&key, b"KEY").unwrap();
        let ca_s = ca.to_str().unwrap();
        let secure = row(&[("type", "secure"), ("auth_level", "server")]);
        let certs = row(&[("ca_crt", ca_s), ("grpc_ssl_credential", "soc.test")]);
        assert_eq!(
            channel_config(Some(&secure), Some(&certs), "E0"),
            Some(ChannelConfig::Secure(TlsSettings {
                ca_pem: b"CA".to_vec(),
                identity: None,
                target_name: "soc.test".into()
            }))
        );
        // No target name.
        assert_eq!(
            channel_config(Some(&secure), Some(&row(&[("ca_crt", ca_s)])), "E0"),
            None
        );
        // Missing CA file.
        assert_eq!(
            channel_config(
                Some(&secure),
                Some(&row(&[
                    ("ca_crt", "/nonexistent"),
                    ("grpc_ssl_credential", "s")
                ])),
                "E0"
            ),
            None
        );
        // Mutual, as "mutual" and as the YANG value "client".
        for level in ["mutual", "client"] {
            let c = row(&[("type", "secure"), ("auth_level", level)]);
            let full = row(&[
                ("ca_crt", ca_s),
                ("grpc_ssl_credential", "s"),
                ("client_crt", crt.to_str().unwrap()),
                ("client_key", key.to_str().unwrap()),
            ]);
            match channel_config(Some(&c), Some(&full), "E0") {
                Some(ChannelConfig::Secure(t)) => {
                    assert_eq!(t.identity, Some((b"CRT".to_vec(), b"KEY".to_vec())))
                }
                other => panic!("{level}: {other:?}"),
            }
            let no_key = row(&[
                ("ca_crt", ca_s),
                ("grpc_ssl_credential", "s"),
                ("client_crt", crt.to_str().unwrap()),
            ]);
            assert_eq!(channel_config(Some(&c), Some(&no_key), "E0"), None);
        }
        // Unknown level.
        let c = row(&[("type", "secure"), ("auth_level", "other")]);
        assert_eq!(channel_config(Some(&c), Some(&certs), "E0"), None);
    }

    #[test]
    fn secrets_fill_missing_fields_only() {
        let secrets: Value = serde_json::json!({"GRPCCLIENT": {
            "config": {"type": "secure", "auth_level": "server", "log_level": null},
            "certs": {"ca_crt": "/a", "grpc_ssl_credential": "s"}
        }});
        let mut existing = HashMap::new();
        existing.insert("config".to_string(), row(&[("type", "insecure")]));
        let out = secrets_to_apply(&secrets, &existing);
        assert_eq!(out.len(), 2);
        assert_eq!(
            out[0],
            (
                "config".to_string(),
                vec![("auth_level".to_string(), "server".to_string())]
            )
        );
        assert_eq!(out[1].0, "certs");
        assert_eq!(out[1].1.len(), 2);
        // Empty sections are skipped.
        let empty: Value = serde_json::json!({"GRPCCLIENT": {"config": {}}});
        assert!(secrets_to_apply(&empty, &HashMap::new()).is_empty());
        assert!(secrets_to_apply(&serde_json::json!({}), &HashMap::new()).is_empty());
    }

    #[test]
    fn xcvrd_log_only_y_cable() {
        let _g = crate::testing::GLOBALS
            .lock()
            .unwrap_or_else(|p| p.into_inner());
        logger::set_min_level(Level::Notice);
        set_telemetry_disabled(false);
        handle_xcvrd_log(
            "OTHER",
            &row(&[("log_verbosity", "debug"), ("disable_telemetry", "True")]),
        );
        assert_eq!(logger::min_level(), Level::Notice);
        assert!(!telemetry_disabled());
        handle_xcvrd_log(
            "Y_CABLE",
            &row(&[("log_verbosity", "debug"), ("disable_telemetry", "True")]),
        );
        assert_eq!(logger::min_level(), Level::Debug);
        assert!(telemetry_disabled());
        handle_xcvrd_log(
            "Y_CABLE",
            &row(&[("log_verbosity", "notice"), ("disable_telemetry", "False")]),
        );
        assert_eq!(logger::min_level(), Level::Notice);
        assert!(!telemetry_disabled());
    }
}
