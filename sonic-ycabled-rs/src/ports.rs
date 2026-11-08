//! Namespaces and the port map: which physical port (the index the platform
//! API and the vendor drivers take) and which ASIC each logical port is on.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use crate::db::{Db, DbId};
use crate::log_warning;
use crate::schema::{DEVICE_METADATA, PORT};

pub const CONTAINER_PLATFORM_PATH: &str = "/usr/share/sonic/platform";
pub const HOST_DEVICE_PATH: &str = "/usr/share/sonic/device";

/// NUM_ASIC from asic.conf, as sonic_py_common's multi_asic reads it: in the
/// container's platform directory, else the device directory of `platform`.
pub fn num_asics(platform: &str) -> usize {
    num_asics_in(&[
        PathBuf::from(CONTAINER_PLATFORM_PATH),
        Path::new(HOST_DEVICE_PATH).join(platform),
    ])
}

pub fn num_asics_in(dirs: &[PathBuf]) -> usize {
    for dir in dirs {
        let path = dir.join("asic.conf");
        if !path.is_file() {
            continue;
        }
        let Ok(text) = std::fs::read_to_string(&path) else {
            return 1;
        };
        for line in text.lines() {
            let mut parts = line.splitn(2, '=');
            if parts.next().map(str::trim) == Some("NUM_ASIC") {
                return parts
                    .next()
                    .and_then(|v| v.trim().parse().ok())
                    .unwrap_or(1);
            }
        }
        return 1;
    }
    1
}

/// A front-end ASIC namespace is `asic<N>` whose DEVICE_METADATA sub_role
/// is FrontEnd.
pub async fn is_front_end(db: &dyn Db) -> bool {
    db.hget(DbId::Config, DEVICE_METADATA, "localhost", "sub_role")
        .await
        .as_deref()
        == Some("FrontEnd")
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PortInfo {
    pub name: String,
    pub physical: u32,
    pub asic: usize,
}

#[derive(Clone, Debug, Default)]
pub struct PortMap {
    ports: BTreeMap<String, PortInfo>,
}

/// Natural order: Ethernet4 before Ethernet12.
fn natural_key(name: &str) -> (String, u64) {
    let split = name
        .find(|c: char| c.is_ascii_digit())
        .unwrap_or(name.len());
    let (prefix, digits) = name.split_at(split);
    (prefix.to_string(), digits.parse().unwrap_or(u64::MAX))
}

impl PortMap {
    pub fn insert(&mut self, name: &str, physical: u32, asic: usize) {
        self.ports.insert(
            name.to_string(),
            PortInfo {
                name: name.to_string(),
                physical,
                asic,
            },
        );
    }

    pub fn get(&self, name: &str) -> Option<&PortInfo> {
        self.ports.get(name)
    }

    pub fn is_empty(&self) -> bool {
        self.ports.is_empty()
    }

    pub fn len(&self) -> usize {
        self.ports.len()
    }

    /// The logical ports, in natural order.
    pub fn names(&self) -> Vec<String> {
        let mut v: Vec<String> = self.ports.keys().cloned().collect();
        v.sort_by_key(|n| natural_key(n));
        v
    }

    /// Ports of CONFIG_DB PORT in one namespace, physical index from `index`.
    pub async fn add_config_db(&mut self, db: &dyn Db, asic: usize) {
        for name in db.keys(DbId::Config, PORT).await {
            if let Some(index) = db.hget(DbId::Config, PORT, &name, "index").await {
                match index.trim().parse::<u32>() {
                    Ok(i) => self.insert(&name, i, asic),
                    Err(_) => log_warning!("Invalid index {} of port {}", index, name),
                }
            }
        }
    }

    /// Ports of a port_config.ini: `# name lanes alias index ...` columns.
    pub fn add_port_config(&mut self, path: &Path, asic: usize) -> bool {
        let Ok(text) = std::fs::read_to_string(path) else {
            return false;
        };
        let mut columns: Vec<String> = vec![
            "name".into(),
            "lanes".into(),
            "alias".into(),
            "index".into(),
        ];
        let mut found = false;
        for (n, line) in text.lines().enumerate() {
            let line = line.trim();
            if line.is_empty() {
                continue;
            }
            if let Some(header) = line.strip_prefix('#') {
                let cols: Vec<String> = header.split_whitespace().map(str::to_string).collect();
                if cols.first().map(String::as_str) == Some("name") {
                    columns = cols;
                }
                continue;
            }
            let values: Vec<&str> = line.split_whitespace().collect();
            let Some(name) = values.first() else { continue };
            let index = columns
                .iter()
                .position(|c| c == "index")
                .and_then(|i| values.get(i))
                .and_then(|v| v.parse::<u32>().ok())
                .unwrap_or(n as u32);
            self.insert(name, index, asic);
            found = true;
        }
        found
    }
}

/// port_config.ini of a HwSKU: `<hwsku dir>/port_config.ini`, or
/// `<hwsku dir>/<asic>/port_config.ini` on a multi-ASIC platform.
pub fn port_config_path(hwsku: &str, asic: Option<usize>) -> PathBuf {
    let dir = Path::new(CONTAINER_PLATFORM_PATH).join(hwsku);
    match asic {
        Some(a) => dir.join(a.to_string()).join("port_config.ini"),
        None => dir.join("port_config.ini"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::MemDb;

    #[test]
    fn asic_conf() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(num_asics_in(&[dir.path().to_path_buf()]), 1);
        std::fs::write(
            dir.path().join("asic.conf"),
            "NUM_ASIC=2\nDEV_ID_ASIC_0=03:00.0\n",
        )
        .unwrap();
        assert_eq!(
            num_asics_in(&[PathBuf::from("/nonexistent"), dir.path().to_path_buf()]),
            2
        );
    }

    #[test]
    fn natural_order() {
        let mut m = PortMap::default();
        for (n, i) in [
            ("Ethernet12", 4),
            ("Ethernet4", 2),
            ("Ethernet0", 1),
            ("Ethernet100", 26),
        ] {
            m.insert(n, i, 0);
        }
        assert_eq!(
            m.names(),
            vec!["Ethernet0", "Ethernet4", "Ethernet12", "Ethernet100"]
        );
    }

    #[test]
    fn port_config_file() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("port_config.ini");
        std::fs::write(&p, "# name lanes alias index speed\nEthernet0 0,1,2,3 etp1 1 100000\nEthernet4 4,5,6,7 etp2 2 100000\n").unwrap();
        let mut m = PortMap::default();
        assert!(m.add_port_config(&p, 0));
        assert_eq!(m.get("Ethernet4").unwrap().physical, 2);
        assert!(!m.add_port_config(&dir.path().join("missing.ini"), 0));
    }

    #[tokio::test]
    async fn config_db_ports() {
        let db = MemDb::new();
        db.put(
            DbId::Config,
            PORT,
            "Ethernet0",
            &[("index", "1"), ("lanes", "0")],
        );
        db.put(DbId::Config, PORT, "Ethernet4", &[("index", "2")]);
        db.put(DbId::Config, PORT, "Ethernet8", &[("lanes", "8")]);
        let mut m = PortMap::default();
        m.add_config_db(db.as_ref(), 1).await;
        assert_eq!(m.len(), 2);
        assert_eq!(
            m.get("Ethernet0"),
            Some(&PortInfo {
                name: "Ethernet0".into(),
                physical: 1,
                asic: 1
            })
        );
    }

    #[test]
    fn port_config_paths() {
        assert_eq!(
            port_config_path("SKU", None),
            PathBuf::from("/usr/share/sonic/platform/SKU/port_config.ini")
        );
        assert_eq!(
            port_config_path("SKU", Some(1)),
            PathBuf::from("/usr/share/sonic/platform/SKU/1/port_config.ini")
        );
    }
}
