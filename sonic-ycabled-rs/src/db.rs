//! The SONiC databases, as ycabled uses them.
//!
//! Python ycabled goes through swsscommon. ycabled-rs speaks to redis
//! directly with the same commands and conventions, so the two are
//! interchangeable for every other SONiC component:
//!
//! - a table row is the hash `<TABLE><separator><key>`, separators and DB ids
//!   from `database_config.json`;
//! - a row is written with one `HSET` of all its fields, as swsscommon's
//!   `Table::set` does, so readers never see half of a row;
//! - a subscription is swsscommon's `SubscriberStateTable`: `PSUBSCRIBE` to the
//!   table's keyspace notifications, then `KEYS` and `HGETALL` of the rows that
//!   exist; on a notification, `del` deletes the row, anything else reads it
//!   again and is skipped if it is gone.
//!
//! Per namespace that is one connection per database and one for all its
//! subscriptions, whatever the number of ports. The events of all the
//! subscribed tables come out of one receiver, in the order redis delivered
//! them.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{anyhow, bail, Context, Result};
use async_trait::async_trait;
use futures::StreamExt;
use redis::aio::ConnectionManager;
use serde::Deserialize;
use tokio::sync::mpsc;

use crate::{log_debug, log_error, log_warning};

pub const DATABASE_CONFIG: &str = "/var/run/redis/sonic-db/database_config.json";
pub const DATABASE_GLOBAL: &str = "/var/run/redis/sonic-db/database_global.json";

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum DbId {
    Appl,
    Config,
    State,
}

impl DbId {
    pub fn name(self) -> &'static str {
        match self {
            DbId::Appl => "APPL_DB",
            DbId::Config => "CONFIG_DB",
            DbId::State => "STATE_DB",
        }
    }

    pub const ALL: [DbId; 3] = [DbId::Appl, DbId::Config, DbId::State];
}

pub type FieldMap = HashMap<String, String>;

/// A table row change, as a subscription delivers it.
#[derive(Clone, Debug, PartialEq)]
pub enum Event {
    Set { key: String, fields: FieldMap },
    Del { key: String },
}

impl Event {
    pub fn key(&self) -> &str {
        match self {
            Event::Set { key, .. } | Event::Del { key } => key,
        }
    }
}

/// Table-level access to one namespace's databases. Errors are logged and
/// read as "no data": a failed read is a missing row, as for Python's
/// `Table.get` returning `(False, ...)`.
#[async_trait]
pub trait Db: Send + Sync {
    async fn get(&self, db: DbId, table: &str, key: &str) -> Option<FieldMap>;
    async fn hget(&self, db: DbId, table: &str, key: &str, field: &str) -> Option<String>;
    /// One `HSET` of all fields, merged into the row.
    async fn set(&self, db: DbId, table: &str, key: &str, fields: &[(&str, String)]);
    async fn del(&self, db: DbId, table: &str, key: &str);
    async fn keys(&self, db: DbId, table: &str) -> Vec<String>;
    async fn exists(&self, db: DbId, table: &str, key: &str) -> bool {
        self.get(db, table, key).await.is_some()
    }
}

// --- database_config.json ---------------------------------------------------

#[derive(Clone, Debug, Deserialize)]
pub struct Instance {
    pub hostname: String,
    pub port: u16,
    #[serde(default)]
    pub unix_socket_path: Option<String>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct DbEntry {
    pub id: i64,
    pub separator: String,
    pub instance: String,
}

#[derive(Clone, Debug, Deserialize)]
pub struct DbConfig {
    #[serde(rename = "INSTANCES")]
    pub instances: HashMap<String, Instance>,
    #[serde(rename = "DATABASES")]
    pub databases: HashMap<String, DbEntry>,
}

/// Where one database of a namespace lives.
#[derive(Clone, Debug, PartialEq)]
pub struct DbLocation {
    pub id: i64,
    pub separator: String,
    pub hostname: String,
    pub port: u16,
}

impl DbConfig {
    pub fn load(path: &Path) -> Result<DbConfig> {
        let text =
            std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
        serde_json::from_str(&text).with_context(|| format!("parsing {}", path.display()))
    }

    pub fn location(&self, db: DbId) -> Result<DbLocation> {
        let entry = self
            .databases
            .get(db.name())
            .ok_or_else(|| anyhow!("{} is not in the DB config", db.name()))?;
        let inst = self.instances.get(&entry.instance).ok_or_else(|| {
            anyhow!(
                "instance {} of {} is not in the DB config",
                entry.instance,
                db.name()
            )
        })?;
        Ok(DbLocation {
            id: entry.id,
            separator: entry.separator.clone(),
            hostname: inst.hostname.clone(),
            port: inst.port,
        })
    }
}

#[derive(Debug, Deserialize)]
struct GlobalInclude {
    #[serde(default)]
    namespace: Option<String>,
    include: String,
}

#[derive(Debug, Deserialize)]
struct GlobalConfig {
    #[serde(rename = "INCLUDES")]
    includes: Vec<GlobalInclude>,
}

/// The DB config of each namespace in `database_global.json`; "" is the
/// default namespace.
pub fn load_global(path: &Path) -> Result<HashMap<String, DbConfig>> {
    let text =
        std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
    let global: GlobalConfig =
        serde_json::from_str(&text).with_context(|| format!("parsing {}", path.display()))?;
    let dir = path.parent().unwrap_or_else(|| Path::new("/"));
    let mut out = HashMap::new();
    for inc in global.includes {
        let p: PathBuf = dir.join(&inc.include);
        out.insert(inc.namespace.unwrap_or_default(), DbConfig::load(&p)?);
    }
    Ok(out)
}

// --- Redis implementation ----------------------------------------------------

struct Conn {
    loc: DbLocation,
    mgr: ConnectionManager,
}

/// One namespace's APPL_DB, CONFIG_DB and STATE_DB.
pub struct RedisDb {
    pub namespace: String,
    conns: HashMap<DbId, Conn>,
}

fn redis_url(loc: &DbLocation) -> String {
    let host = if loc.hostname.contains(':') {
        format!("[{}]", loc.hostname)
    } else {
        loc.hostname.clone()
    };
    format!("redis://{}:{}/{}", host, loc.port, loc.id)
}

fn table_key(loc: &DbLocation, table: &str, key: &str) -> String {
    format!("{}{}{}", table, loc.separator, key)
}

impl RedisDb {
    pub async fn connect(namespace: &str, config: &DbConfig) -> Result<RedisDb> {
        let mut conns = HashMap::new();
        for db in DbId::ALL {
            let loc = config.location(db)?;
            let client = redis::Client::open(redis_url(&loc))?;
            let cfg = redis::aio::ConnectionManagerConfig::new()
                .set_connection_timeout(Duration::from_secs(5))
                .set_response_timeout(Duration::from_secs(5))
                .set_number_of_retries(3);
            let mgr = client
                .get_connection_manager_with_config(cfg)
                .await
                .with_context(|| {
                    format!(
                        "connecting to {} at {}:{}",
                        db.name(),
                        loc.hostname,
                        loc.port
                    )
                })?;
            conns.insert(db, Conn { loc, mgr });
        }
        Ok(RedisDb {
            namespace: namespace.to_string(),
            conns,
        })
    }

    fn conn(&self, db: DbId) -> &Conn {
        &self.conns[&db]
    }

    pub fn location(&self, db: DbId) -> &DbLocation {
        &self.conn(db).loc
    }

    async fn hgetall_raw(&self, db: DbId, full_key: &str) -> Result<FieldMap> {
        let mut mgr = self.conn(db).mgr.clone();
        Ok(redis::cmd("HGETALL")
            .arg(full_key)
            .query_async(&mut mgr)
            .await?)
    }

    /// A plain string key, such as CONFIG_DB_INITIALIZED.
    pub async fn get_string(&self, db: DbId, key: &str) -> Option<String> {
        let mut mgr = self.conn(db).mgr.clone();
        match redis::cmd("GET")
            .arg(key)
            .query_async::<Option<String>>(&mut mgr)
            .await
        {
            Ok(v) => v,
            Err(e) => {
                log_warning!("Failed to read {} {}: {}", db.name(), key, e);
                None
            }
        }
    }

    /// Waits until CONFIG_DB_INITIALIZED is set, as sonic_py_common's
    /// ConfigDBConnector does before a daemon reads CONFIG_DB.
    pub async fn wait_for_config_init(&self) -> Result<()> {
        const KEY: &str = "CONFIG_DB_INITIALIZED";
        if self
            .get_string(DbId::Config, KEY)
            .await
            .is_some_and(|v| !v.is_empty())
        {
            return Ok(());
        }
        let loc = self.location(DbId::Config).clone();
        let client = redis::Client::open(redis_url(&loc))?;
        let mut pubsub = client.get_async_pubsub().await?;
        pubsub
            .psubscribe(format!("__keyspace@{}__:{}", loc.id, KEY))
            .await?;
        let mut stream = pubsub.into_on_message();
        // Set between the GET and the subscription.
        if self
            .get_string(DbId::Config, KEY)
            .await
            .is_some_and(|v| !v.is_empty())
        {
            return Ok(());
        }
        while stream.next().await.is_some() {
            if self
                .get_string(DbId::Config, KEY)
                .await
                .is_some_and(|v| !v.is_empty())
            {
                return Ok(());
            }
        }
        bail!("the CONFIG_DB subscription for {} ended", KEY)
    }

    /// Starts swsscommon-style subscriptions to `tables`, on one connection.
    /// Every table's rows that exist come first, in this order; then the
    /// changes of all tables, in the order redis delivered them, so a port's
    /// requests keep their order across tables. `on_lost` runs if the
    /// connection is lost, or if redis refuses a table added later.
    pub async fn subscribe(
        self: &Arc<Self>,
        tables: &[(DbId, String)],
        on_lost: impl FnOnce() + Send + 'static,
    ) -> Result<(Subscriptions, mpsc::UnboundedReceiver<TableEvent>)> {
        // The pub/sub connection is not tied to a database: the keyspace
        // channels carry the DB id.
        let client = redis::Client::open(redis_url(self.location(DbId::Config)))?;
        let (mut sink, mut stream) = client.get_async_pubsub().await?.split();
        let (tx, rx) = mpsc::unbounded_channel();
        let mut patterns = HashMap::new();
        self.start_tables(&mut sink, &mut patterns, tables, &tx)
            .await?;
        let (add_tx, mut add_rx) = mpsc::unbounded_channel::<Vec<(DbId, String)>>();
        let this = Arc::clone(self);
        // One task serves the connection, so replayed rows and changes are
        // sent in the order they are read.
        tokio::spawn(async move {
            let mut adding = true;
            loop {
                tokio::select! {
                    tables = add_rx.recv(), if adding => match tables {
                        Some(tables) => {
                            if let Err(e) = this.start_tables(&mut sink, &mut patterns, &tables, &tx).await {
                                log_error!("Failed to subscribe to more tables: {:#}", e);
                                break;
                            }
                        }
                        None => adding = false,
                    },
                    msg = stream.next() => match msg {
                        Some(msg) => {
                            if let Some(event) = this.event_of(&msg, &patterns).await {
                                if tx.send(event).is_err() {
                                    log_debug!("No one listens to the subscriptions of namespace '{}' any more", this.namespace);
                                }
                            }
                        }
                        None => break,
                    },
                }
            }
            on_lost();
        });
        Ok((Subscriptions { add: add_tx }, rx))
    }

    /// Subscribes to `tables`, then sends the rows they have.
    async fn start_tables(
        &self,
        sink: &mut redis::aio::PubSubSink,
        patterns: &mut HashMap<String, (DbId, String)>,
        tables: &[(DbId, String)],
        tx: &mpsc::UnboundedSender<TableEvent>,
    ) -> Result<()> {
        for (db, table) in tables {
            let loc = self.location(*db);
            let pattern = format!("__keyspace@{}__:{}{}*", loc.id, table, loc.separator);
            sink.psubscribe(&pattern).await?;
            patterns.insert(pattern, (*db, table.clone()));
        }
        // The rows that exist, once every subscription is in place.
        for (db, table) in tables {
            for key in self.keys(*db, table).await {
                if let Some(fields) = self.get(*db, table, &key).await {
                    let _ = tx.send((table.clone(), Event::Set { key, fields }));
                }
            }
        }
        Ok(())
    }

    /// The event of a keyspace notification: `del` deletes the row, anything
    /// else reads it again. None if the row is gone again, or the
    /// notification is not for a subscribed table.
    async fn event_of(
        &self,
        msg: &redis::Msg,
        patterns: &HashMap<String, (DbId, String)>,
    ) -> Option<TableEvent> {
        let pattern: String = msg.get_pattern().ok()?;
        let (db, table) = patterns.get(&pattern)?;
        let (_, table_entry) = msg.get_channel_name().split_once(':')?;
        let (_, key) = table_entry.split_once(self.location(*db).separator.as_str())?;
        let op: String = msg.get_payload().unwrap_or_default();
        let event = if op == "del" {
            Event::Del {
                key: key.to_string(),
            }
        } else {
            match self.hgetall_raw(*db, table_entry).await {
                Ok(fields) if !fields.is_empty() => Event::Set {
                    key: key.to_string(),
                    fields,
                },
                Ok(_) => {
                    log_debug!("Miss table key {}, possibly outdated", table_entry);
                    return None;
                }
                Err(e) => {
                    log_warning!("Failed to read {} {}: {}", db.name(), table_entry, e);
                    return None;
                }
            }
        };
        Some((table.clone(), event))
    }
}

/// A row change of a subscribed table: the table and the change.
pub type TableEvent = (String, Event);

/// The subscriptions of a namespace, to add tables to.
pub struct Subscriptions {
    add: mpsc::UnboundedSender<Vec<(DbId, String)>>,
}

impl Subscriptions {
    /// Subscribes to more tables: their rows, then their changes, in order
    /// with the other tables' events.
    pub fn add(&self, tables: Vec<(DbId, String)>) {
        let _ = self.add.send(tables);
    }
}

#[async_trait]
impl Db for RedisDb {
    async fn get(&self, db: DbId, table: &str, key: &str) -> Option<FieldMap> {
        let full = table_key(self.location(db), table, key);
        match self.hgetall_raw(db, &full).await {
            Ok(m) if !m.is_empty() => Some(m),
            Ok(_) => None,
            Err(e) => {
                log_warning!("Failed to read {} {}: {}", db.name(), full, e);
                None
            }
        }
    }

    async fn hget(&self, db: DbId, table: &str, key: &str, field: &str) -> Option<String> {
        let full = table_key(self.location(db), table, key);
        let mut mgr = self.conn(db).mgr.clone();
        match redis::cmd("HGET")
            .arg(&full)
            .arg(field)
            .query_async::<Option<String>>(&mut mgr)
            .await
        {
            Ok(v) => v,
            Err(e) => {
                log_warning!("Failed to read {} {} {}: {}", db.name(), full, field, e);
                None
            }
        }
    }

    async fn set(&self, db: DbId, table: &str, key: &str, fields: &[(&str, String)]) {
        if fields.is_empty() {
            return;
        }
        let full = table_key(self.location(db), table, key);
        let mut cmd = redis::cmd("HSET");
        cmd.arg(&full);
        for (f, v) in fields {
            cmd.arg(*f).arg(v);
        }
        let mut mgr = self.conn(db).mgr.clone();
        if let Err(e) = cmd.query_async::<()>(&mut mgr).await {
            log_error!("Failed to write {} {}: {}", db.name(), full, e);
        }
    }

    async fn del(&self, db: DbId, table: &str, key: &str) {
        let full = table_key(self.location(db), table, key);
        let mut mgr = self.conn(db).mgr.clone();
        if let Err(e) = redis::cmd("DEL")
            .arg(&full)
            .query_async::<()>(&mut mgr)
            .await
        {
            log_error!("Failed to delete {} {}: {}", db.name(), full, e);
        }
    }

    async fn keys(&self, db: DbId, table: &str) -> Vec<String> {
        let loc = self.location(db);
        let prefix = format!("{}{}", table, loc.separator);
        let mut mgr = self.conn(db).mgr.clone();
        match redis::cmd("KEYS")
            .arg(format!("{prefix}*"))
            .query_async::<Vec<String>>(&mut mgr)
            .await
        {
            Ok(keys) => keys
                .into_iter()
                .filter_map(|k| k.strip_prefix(&prefix).map(str::to_string))
                .collect(),
            Err(e) => {
                log_warning!("Failed to list {} {}: {}", db.name(), table, e);
                vec![]
            }
        }
    }
}

// --- In-memory implementation, for unit tests ------------------------------

/// What a [`MemDb`] was asked to do.
#[derive(Clone, Debug, PartialEq)]
pub enum MemOp {
    Set {
        db: DbId,
        table: String,
        key: String,
        fields: Vec<(String, String)>,
    },
    Del {
        db: DbId,
        table: String,
        key: String,
    },
}

#[derive(Default)]
pub struct MemDb {
    rows: Mutex<HashMap<(DbId, String, String), FieldMap>>,
    ops: Mutex<Vec<MemOp>>,
}

impl MemDb {
    pub fn new() -> Arc<MemDb> {
        Arc::new(MemDb::default())
    }

    /// Writes a row as another component would: not recorded in `ops`.
    pub fn put(&self, db: DbId, table: &str, key: &str, fields: &[(&str, &str)]) {
        let mut rows = self.rows.lock().unwrap();
        let row = rows
            .entry((db, table.to_string(), key.to_string()))
            .or_default();
        for (f, v) in fields {
            row.insert(f.to_string(), v.to_string());
        }
    }

    pub fn remove(&self, db: DbId, table: &str, key: &str) {
        self.rows
            .lock()
            .unwrap()
            .remove(&(db, table.to_string(), key.to_string()));
    }

    pub fn row(&self, db: DbId, table: &str, key: &str) -> Option<FieldMap> {
        self.rows
            .lock()
            .unwrap()
            .get(&(db, table.to_string(), key.to_string()))
            .cloned()
    }

    pub fn ops(&self) -> Vec<MemOp> {
        self.ops.lock().unwrap().clone()
    }

    pub fn clear_ops(&self) {
        self.ops.lock().unwrap().clear();
    }

    /// The writes to `table`, in order.
    pub fn writes(&self, table: &str) -> Vec<(String, Vec<(String, String)>)> {
        self.ops()
            .into_iter()
            .filter_map(|op| match op {
                MemOp::Set {
                    table: t,
                    key,
                    fields,
                    ..
                } if t == table => Some((key, fields)),
                _ => None,
            })
            .collect()
    }
}

#[async_trait]
impl Db for MemDb {
    async fn get(&self, db: DbId, table: &str, key: &str) -> Option<FieldMap> {
        self.row(db, table, key).filter(|r| !r.is_empty())
    }

    async fn hget(&self, db: DbId, table: &str, key: &str, field: &str) -> Option<String> {
        self.row(db, table, key).and_then(|r| r.get(field).cloned())
    }

    async fn set(&self, db: DbId, table: &str, key: &str, fields: &[(&str, String)]) {
        if fields.is_empty() {
            return;
        }
        {
            let mut rows = self.rows.lock().unwrap();
            let row = rows
                .entry((db, table.to_string(), key.to_string()))
                .or_default();
            for (f, v) in fields {
                row.insert(f.to_string(), v.clone());
            }
        }
        self.ops.lock().unwrap().push(MemOp::Set {
            db,
            table: table.to_string(),
            key: key.to_string(),
            fields: fields
                .iter()
                .map(|(f, v)| (f.to_string(), v.clone()))
                .collect(),
        });
    }

    async fn del(&self, db: DbId, table: &str, key: &str) {
        self.rows
            .lock()
            .unwrap()
            .remove(&(db, table.to_string(), key.to_string()));
        self.ops.lock().unwrap().push(MemOp::Del {
            db,
            table: table.to_string(),
            key: key.to_string(),
        });
    }

    async fn keys(&self, db: DbId, table: &str) -> Vec<String> {
        let mut keys: Vec<String> = self
            .rows
            .lock()
            .unwrap()
            .keys()
            .filter(|(d, t, _)| *d == db && t == table)
            .map(|(_, _, k)| k.clone())
            .collect();
        keys.sort();
        keys
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const CONFIG: &str = r#"{
        "INSTANCES": {"redis": {"hostname": "127.0.0.1", "port": 6379, "unix_socket_path": "/var/run/redis/redis.sock"}},
        "DATABASES": {
            "APPL_DB": {"id": 0, "separator": ":", "instance": "redis"},
            "CONFIG_DB": {"id": 4, "separator": "|", "instance": "redis"},
            "STATE_DB": {"id": 6, "separator": "|", "instance": "redis"}
        },
        "VERSION": "1.0"
    }"#;

    #[test]
    fn locations_from_config() {
        let cfg: DbConfig = serde_json::from_str(CONFIG).unwrap();
        let appl = cfg.location(DbId::Appl).unwrap();
        assert_eq!(
            (appl.id, appl.separator.as_str(), appl.port),
            (0, ":", 6379)
        );
        assert_eq!(cfg.location(DbId::State).unwrap().separator, "|");
        assert_eq!(
            redis_url(&cfg.location(DbId::Config).unwrap()),
            "redis://127.0.0.1:6379/4"
        );
        assert_eq!(
            table_key(&appl, "HW_MUX_CABLE_TABLE", "Ethernet0"),
            "HW_MUX_CABLE_TABLE:Ethernet0"
        );
    }

    #[test]
    fn ipv6_redis_host() {
        let loc = DbLocation {
            id: 6,
            separator: "|".into(),
            hostname: "::1".into(),
            port: 6379,
        };
        assert_eq!(redis_url(&loc), "redis://[::1]:6379/6");
    }

    #[test]
    fn global_config_includes_namespaces() {
        let dir = tempfile::tempdir().unwrap();
        let base = dir.path();
        std::fs::create_dir_all(base.join("redis/sonic-db")).unwrap();
        std::fs::create_dir_all(base.join("redis0/sonic-db")).unwrap();
        std::fs::write(base.join("redis/sonic-db/database_config.json"), CONFIG).unwrap();
        std::fs::write(
            base.join("redis0/sonic-db/database_config.json"),
            CONFIG.replace("6379", "6380"),
        )
        .unwrap();
        let global = base.join("redis/sonic-db/database_global.json");
        std::fs::write(
            &global,
            r#"{"INCLUDES": [{"include": "../../redis/sonic-db/database_config.json"},
                             {"namespace": "asic0", "include": "../../redis0/sonic-db/database_config.json"}],
                "VERSION": "1.0"}"#,
        )
        .unwrap();
        let all = load_global(&global).unwrap();
        assert_eq!(all[""].location(DbId::Appl).unwrap().port, 6379);
        assert_eq!(all["asic0"].location(DbId::Appl).unwrap().port, 6380);
    }

    #[tokio::test]
    async fn memdb_merges_rows_and_records_writes() {
        let db = MemDb::new();
        db.set(DbId::State, "T", "k", &[("a", "1".into())]).await;
        db.set(DbId::State, "T", "k", &[("b", "2".into())]).await;
        let row = db.get(DbId::State, "T", "k").await.unwrap();
        assert_eq!(row.len(), 2);
        assert_eq!(db.writes("T").len(), 2);
        db.del(DbId::State, "T", "k").await;
        assert!(db.get(DbId::State, "T", "k").await.is_none());
        // A row without fields is not created.
        db.set(DbId::State, "T", "k", &[]).await;
        assert_eq!(db.keys(DbId::State, "T").await, Vec::<String>::new());
    }
}
