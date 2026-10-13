//! A STATE_DB table, behind a trait so the write path can be tested.
//!
//! The Python daemons test the same way: `tests/mock_swsscommon.py` shadows the
//! whole `swsscommon` package with a dictionary-backed stand-in.  This is that,
//! expressed as a trait, so a daemon's table logic can be exercised with no
//! redis and no container.
//!
//! This lives beside the daemons rather than inside one of them because every
//! pmon daemon opens tables and none of them should carry its own copy.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use swss_common::{CxxString, DbConnector, SonicV2Connector, Table};

pub const STATE_DB: &str = "STATE_DB";
const CONNECT_TIMEOUT_MS: u32 = 0;

pub trait TableLike: Send {
    fn set(&self, key: &str, fvs: &[(&str, String)]) -> Result<(), String>;
    fn del(&self, key: &str) -> Result<(), String>;
    /// Drop one field, leaving the rest of the row.  psud's power budget needs
    /// it: a supplier that went away has to stop being listed without taking
    /// the other suppliers' entries with it.
    fn hdel(&self, key: &str, field: &str) -> Result<(), String>;
    /// One row, or `Ok(None)` when there is no such row.
    ///
    /// The error is separate from the absence on purpose: a read that failed
    /// and a row that is not there are different facts, and folding them
    /// together is how a daemon decides a table is empty because redis went
    /// away.  syseepromd did exactly that -- its integrity check compared an
    /// empty read against an empty cache, matched, and carried on publishing
    /// nothing while Python's `getKeys()` raised and took the process down.
    fn get(&self, key: &str) -> Result<Option<Vec<(String, String)>>, String>;

    /// Every key in the table.  Same reasoning as `get`.
    fn get_keys(&self) -> Result<Vec<String>, String>;
}

impl TableLike for Table {
    fn set(&self, key: &str, fvs: &[(&str, String)]) -> Result<(), String> {
        Table::set(self, key, fvs.to_vec()).map_err(|e| format!("{e:?}"))
    }

    fn del(&self, key: &str) -> Result<(), String> {
        Table::del(self, key).map_err(|e| format!("{e:?}"))
    }

    fn hdel(&self, key: &str, field: &str) -> Result<(), String> {
        Table::hdel(self, key, field).map_err(|e| format!("{e:?}"))
    }

    fn get(&self, key: &str) -> Result<Option<Vec<(String, String)>>, String> {
        let Some(fvs) = Table::get(self, key).map_err(|e| format!("{e:?}"))? else {
            return Ok(None);
        };
        Ok(Some(decode_row(fvs)))
    }

    fn get_keys(&self) -> Result<Vec<String>, String> {
        Table::get_keys(self).map_err(|e| format!("{e:?}"))
    }
}

/// A row as redis returned it, with its values as text.
///
/// A value that is not UTF-8 keeps its field, with the bytes that do not
/// decode replaced.  Dropping the field instead made a row with one mangled
/// value read as a row without that field at all -- the same answer as one
/// that was never written, which is the kind of folding `TableLike::get`
/// exists to prevent.  Python keeps such a value too: its swsscommon binding
/// decodes with `surrogateescape` rather than dropping what will not decode.
fn decode_row(fvs: impl IntoIterator<Item = (String, CxxString)>) -> Vec<(String, String)> {
    fvs.into_iter()
        .map(|(k, v)| (k, v.to_string_lossy().into_owned()))
        .collect()
}

/// A whole database's key space, which a `Table` cannot express.
///
/// A `Table` addresses rows *within* one table and hands back unqualified keys;
/// some sweeps are over every key of a database and have to see the table name
/// to know what to leave alone.  chassisd's cleanup of a shut-down DPU is one:
/// it removes everything the DPU left behind except its DPU_STATE and its
/// reboot-cause history, and told only unqualified keys it would delete exactly
/// the two it must keep.
pub trait Keyspace: Send {
    /// Keys matching a redis glob.
    ///
    /// A failed scan is an error, not an empty answer, for the reason
    /// `TableLike::get` gives: the caller is about to decide what to delete
    /// from it, and "redis did not answer" must not read as "nothing there".
    fn keys(&self, pattern: &str) -> Result<Vec<String>, String>;
    fn del(&self, key: &str) -> Result<(), String>;
}

/// The real thing, over one database.
pub struct Db {
    conn: SonicV2Connector,
    name: String,
}

impl Db {
    pub fn connect(name: &str) -> Result<Self, String> {
        let conn = SonicV2Connector::new(false, None).map_err(|e| format!("{e:?}"))?;
        conn.connect(name, true).map_err(|e| format!("{e:?}"))?;
        Ok(Self { conn, name: name.to_string() })
    }
}

impl Keyspace for Db {
    fn keys(&self, pattern: &str) -> Result<Vec<String>, String> {
        self.conn.keys(&self.name, Some(pattern), false).map_err(|e| format!("{e:?}"))
    }

    fn del(&self, key: &str) -> Result<(), String> {
        self.conn.del(&self.name, key, false).map(|_| ()).map_err(|e| format!("{e:?}"))
    }
}

/// A key space that lives in a map, for tests.
#[derive(Clone, Default)]
pub struct MockKeyspace {
    keys: Arc<Mutex<Vec<String>>>,
    /// What a scan fails with, once `fail_reads` has armed it.
    read_fault: Arc<Mutex<Option<String>>>,
}

impl MockKeyspace {
    pub fn new(keys: &[&str]) -> Self {
        Self {
            keys: Arc::new(Mutex::new(keys.iter().map(|k| k.to_string()).collect())),
            ..Self::default()
        }
    }

    /// Make every subsequent scan fail.  Deletes still go through, so a
    /// caller that deleted anyway would show in `remaining`.
    pub fn fail_reads(&self, why: &str) {
        *self.read_fault.lock().unwrap() = Some(why.to_string());
    }

    pub fn remaining(&self) -> Vec<String> {
        self.keys.lock().unwrap().clone()
    }
}

impl Keyspace for MockKeyspace {
    /// Only `*` wildcards, which is all any caller here uses.
    fn keys(&self, pattern: &str) -> Result<Vec<String>, String> {
        if let Some(why) = self.read_fault.lock().unwrap().clone() {
            return Err(why);
        }
        let parts: Vec<&str> = pattern.split('*').collect();
        Ok(self
            .keys
            .lock()
            .unwrap()
            .iter()
            .filter(|k| matches_glob(k, &parts))
            .cloned()
            .collect())
    }

    fn del(&self, key: &str) -> Result<(), String> {
        self.keys.lock().unwrap().retain(|k| k != key);
        Ok(())
    }
}

fn matches_glob(key: &str, parts: &[&str]) -> bool {
    let mut rest = key;
    for (i, part) in parts.iter().enumerate() {
        if part.is_empty() {
            continue;
        }
        match rest.find(part) {
            Some(at) if i > 0 || at == 0 => rest = &rest[at + part.len()..],
            _ => return false,
        }
    }
    parts.last().is_none_or(|last| last.is_empty() || key.ends_with(last))
}

/// How a daemon opens its tables.
///
/// A function rather than a direct call so the set of tables a daemon opens --
/// which database each lives on, and what it is called -- is something a test
/// can assert on.  A typo in a table name is otherwise invisible until the
/// daemon is on a switch, where it shows up as a table nobody is writing.
pub type Opener<'a> = &'a dyn Fn(&str, &str) -> Result<Box<dyn TableLike>, String>;

/// Open one table on one database.
///
/// TCP, not the unix socket, because that is what every Python pmon daemon
/// does: `daemon_base.py:db_connect` is
/// `DBConnector(db_name, REDIS_TIMEOUT_MSECS, True, namespace)`, and
/// `isTcpConn` is a hard either/or in swss-common -- `true` uses the instance's
/// hostname and port, `false` its socket path, with no fallback either way
/// (`dbconnector.cpp:706-722`).
///
/// Passing `false` worked on a switch, where every database instance is local
/// and has a socket, and failed on the first machine where one is not: a
/// SmartSwitch DPU runs no `redis_chassis` of its own -- `/var/run/redis-chassis/`
/// is an empty directory -- and reaches the NPU's over TCP at the hostname its
/// `database_config.json` already names.  On r-bobcat-01-dpu-0 the Rust chassisd
/// died with `Failed to connect to CHASSIS_STATE_DB` on every start until
/// supervisord gave up, while the Python daemon on the same DPU had been
/// publishing `DPU_STATE` to that database all along.
///
/// This is the only place any of the daemons opens a table, so the wrong
/// transport here is the wrong transport everywhere.
pub fn open(db: &str, table: &str) -> Result<Box<dyn TableLike>, String> {
    DbConnector::new_named(db, true, CONNECT_TIMEOUT_MS)
        .and_then(|c| Table::new(c, table))
        .map(|t| Box::new(t) as Box<dyn TableLike>)
        .map_err(|e| format!("{e:?}"))
}

/// An opener that records what was asked for and hands back mock tables.
///
/// The recording is the point: the assertion is on the `(database, table)`
/// pairs, which is the thing that cannot be checked any other way short of a
/// switch.
#[derive(Default)]
pub struct MockOpener {
    asked: Arc<Mutex<Vec<(String, String)>>>,
    tables: Arc<Mutex<BTreeMap<String, MockTable>>>,
    /// A table name that fails to open, for the error path.
    fails: Option<String>,
}

impl MockOpener {
    pub fn new() -> Self {
        Self::default()
    }

    /// The same, with one table refusing to open.
    pub fn failing(table: &str) -> Self {
        Self { fails: Some(table.to_string()), ..Self::default() }
    }

    /// `(database, table)` in the order they were asked for.
    pub fn asked(&self) -> Vec<(String, String)> {
        self.asked.lock().unwrap().clone()
    }

    /// The mock handed back for a table, so a test can read what was written.
    pub fn table(&self, name: &str) -> Option<MockTable> {
        self.tables.lock().unwrap().get(name).cloned()
    }

    pub fn open(&self, db: &str, table: &str) -> Result<Box<dyn TableLike>, String> {
        self.asked.lock().unwrap().push((db.to_string(), table.to_string()));
        if self.fails.as_deref() == Some(table) {
            return Err(format!("no such database: {db}"));
        }
        let mut tables = self.tables.lock().unwrap();
        let t = tables.entry(table.to_string()).or_default().clone();
        Ok(Box::new(t))
    }
}

/// A table that lives in a map, for tests.
///
/// `tests/mock_swsscommon.Table` is what the Python daemons test against; this
/// is the same idea.  Cloning shares the contents, so a test can keep a handle
/// on what the daemon wrote after handing the table away.
/// The shared row store behind a `MockTable`.
type Rows = Arc<Mutex<BTreeMap<String, Vec<(String, String)>>>>;

/// The failures a `MockTable` has been armed with.
///
/// Reads and writes are armed separately because the daemons treat them
/// separately: most passes read a row before writing it, so a table that
/// could only fail both at once would report every lost redis on the read
/// and leave every write-failure branch untested.
#[derive(Default)]
struct Faults {
    /// Writes (`set`, `del`, `hdel`) fail with this, once `writes_left` is
    /// spent.
    writes: Option<String>,
    /// How many more writes go through before `writes` bites; `None` means
    /// it bites at once.  For a pass that must get part-way and then lose
    /// its table -- a drawer row written, its LED row refused.
    writes_left: Option<usize>,
    /// Reads (`get`, `get_keys`) fail with this.
    reads: Option<String>,
}

#[derive(Clone, Default)]
pub struct MockTable {
    rows: Rows,
    /// What the table has been told to refuse; see `Faults`.
    faults: Arc<Mutex<Faults>>,
    /// How many times the table has been enumerated, so a test can tell
    /// whether a read path ran at all.
    scans: Arc<Mutex<usize>>,
    /// Every `(key, field)` this table was ever asked to write, in order.
    ///
    /// Every one of these daemons clears its own rows on the way out, so a
    /// test that looks only at what is left cannot tell "published and then
    /// cleaned up" from "never published at all" -- and those are the two
    /// halves of the contract.
    writes: Arc<Mutex<Vec<(String, String)>>>,
}

impl MockTable {
    pub fn new() -> Self {
        Self::default()
    }

    /// Make every subsequent write fail.  Reads still answer.
    pub fn fail_writes(&self, why: &str) {
        let mut f = self.faults.lock().unwrap();
        f.writes = Some(why.to_string());
        f.writes_left = None;
    }

    /// Let `n` more writes through, then fail every one after.
    pub fn fail_writes_after(&self, n: usize, why: &str) {
        let mut f = self.faults.lock().unwrap();
        f.writes = Some(why.to_string());
        f.writes_left = Some(n);
    }

    /// Make every subsequent read fail.  Writes still go through.
    pub fn fail_reads(&self, why: &str) {
        self.faults.lock().unwrap().reads = Some(why.to_string());
    }

    /// Fail reads and writes both: the redis went away.
    pub fn disconnect(&self, why: &str) {
        self.fail_writes(why);
        self.fail_reads(why);
    }

    /// Let writes through again, for the recovery half of a failure test.
    pub fn allow_writes(&self) {
        let mut f = self.faults.lock().unwrap();
        f.writes = None;
        f.writes_left = None;
    }

    /// The error a write gets now, spending one of `writes_left` if armed.
    fn write_fault(&self) -> Option<String> {
        let mut f = self.faults.lock().unwrap();
        let why = f.writes.clone()?;
        match f.writes_left.as_mut() {
            Some(n) if *n > 0 => {
                *n -= 1;
                None
            }
            _ => Some(why),
        }
    }

    fn read_fault(&self) -> Option<String> {
        self.faults.lock().unwrap().reads.clone()
    }

    pub fn keys(&self) -> Vec<String> {
        self.rows.lock().unwrap().keys().cloned().collect()
    }

    /// How many times `get_keys` has been called on this table.
    pub fn scans(&self) -> usize {
        *self.scans.lock().unwrap()
    }

    /// Every `(key, field)` written, in order, including rows since deleted.
    pub fn writes(&self) -> Vec<(String, String)> {
        self.writes.lock().unwrap().clone()
    }

    /// Whether one field of one row was ever written.
    pub fn wrote(&self, key: &str, field: &str) -> bool {
        self.writes.lock().unwrap().iter().any(|(k, f)| k == key && f == field)
    }

    pub fn row(&self, key: &str) -> Option<Vec<(String, String)>> {
        self.rows.lock().unwrap().get(key).cloned()
    }

    /// The value of one field, for terse assertions.
    pub fn field(&self, key: &str, field: &str) -> Option<String> {
        self.row(key)?.into_iter().find(|(k, _)| k == field).map(|(_, v)| v)
    }

    pub fn len(&self) -> usize {
        self.rows.lock().unwrap().len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

impl TableLike for MockTable {
    /// Merges the given fields into the row, as redis HSET does — it does
    /// *not* replace it.  Getting this wrong would make a partial write,
    /// such as the led_status-only refresh, look like it erased the row.
    fn set(&self, key: &str, fvs: &[(&str, String)]) -> Result<(), String> {
        if let Some(why) = self.write_fault() {
            return Err(why);
        }
        let mut rows = self.rows.lock().unwrap();
        let row = rows.entry(key.to_string()).or_default();
        let mut seen = self.writes.lock().unwrap();
        for (k, v) in fvs {
            seen.push((key.to_string(), (*k).to_string()));
            match row.iter_mut().find(|(existing, _)| existing == k) {
                Some((_, slot)) => *slot = v.clone(),
                None => row.push((k.to_string(), v.clone())),
            }
        }
        Ok(())
    }

    fn del(&self, key: &str) -> Result<(), String> {
        // A delete is a write: letting `del` succeed while `set` fails would
        // model a redis that exists only for removals, and no daemon's
        // failure path looks like that.
        if let Some(why) = self.write_fault() {
            return Err(why);
        }
        self.rows.lock().unwrap().remove(key);
        Ok(())
    }

    fn hdel(&self, key: &str, field: &str) -> Result<(), String> {
        if let Some(why) = self.write_fault() {
            return Err(why);
        }
        if let Some(row) = self.rows.lock().unwrap().get_mut(key) {
            row.retain(|(k, _)| k != field);
        }
        Ok(())
    }

    fn get(&self, key: &str) -> Result<Option<Vec<(String, String)>>, String> {
        if let Some(why) = self.read_fault() {
            return Err(why);
        }
        Ok(self.row(key))
    }

    fn get_keys(&self) -> Result<Vec<String>, String> {
        if let Some(why) = self.read_fault() {
            return Err(why);
        }
        *self.scans.lock().unwrap() += 1;
        Ok(self.keys())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The recording is what makes a daemon's table set assertable: a typo in
    /// one is otherwise invisible until the daemon is on a switch.
    #[test]
    fn an_opener_records_which_table_on_which_database() {
        let o = MockOpener::new();
        o.open(STATE_DB, "FAN_INFO").unwrap();
        o.open("CHASSIS_STATE_DB", "DPU_STATE").unwrap();
        assert_eq!(o.asked(), vec![
            ("STATE_DB".to_string(), "FAN_INFO".to_string()),
            ("CHASSIS_STATE_DB".to_string(), "DPU_STATE".to_string()),
        ]);
    }

    /// The same handle comes back twice, so what a daemon wrote through the
    /// first is visible through the second -- as it is on a switch.
    #[test]
    fn two_opens_of_one_table_share_its_contents() {
        let o = MockOpener::new();
        let a = o.open(STATE_DB, "FAN_INFO").unwrap();
        a.set("fan1", &[("speed", "50".to_string())]).unwrap();
        let b = o.open(STATE_DB, "FAN_INFO").unwrap();
        assert!(b.get("fan1").unwrap().is_some());
        assert_eq!(o.table("FAN_INFO").unwrap().len(), 1);
    }

    /// A read that failed and a row that is not there are different answers.
    ///
    /// This is the distinction syseepromd needed: with both folded into
    /// `None`, a table it could not reach looked identical to one that was
    /// genuinely empty, so its integrity check matched and the daemon carried
    /// on publishing nothing.
    #[test]
    fn a_failed_read_is_not_an_absent_row() {
        let t = MockTable::new();
        t.set("0x21", &[("Value", "MSN4700".to_string())]).unwrap();

        assert!(t.get("nope").unwrap().is_none(), "an absent row is Ok(None)");
        assert!(t.get("0x21").unwrap().is_some(), "a present one is Ok(Some)");
        assert_eq!(t.get_keys().unwrap(), vec!["0x21".to_string()]);

        t.disconnect("redis is gone");
        assert!(t.get("0x21").is_err(), "a lost redis is Err, not Ok(None)");
        assert!(t.get("nope").is_err(), "and so is a lookup that would have missed");
        assert!(t.get_keys().is_err(), "the key scan too -- not an empty table");
    }

    /// The four failure modes are independent: a test that arms one must not
    /// get the other for free, or a write-failure branch would be reported
    /// by the read in front of it and never run.
    #[test]
    fn writes_and_reads_fail_independently() {
        let t = MockTable::new();
        t.set("k", &[("f", "v".to_string())]).unwrap();

        t.fail_writes("no writes");
        assert_eq!(t.set("k", &[("f", "w".to_string())]), Err("no writes".to_string()));
        assert_eq!(t.del("k"), Err("no writes".to_string()));
        assert_eq!(t.hdel("k", "f"), Err("no writes".to_string()));
        assert!(t.get("k").unwrap().is_some(), "reads still answer");
        assert_eq!(t.get_keys().unwrap(), vec!["k".to_string()]);

        t.allow_writes();
        t.fail_reads("no reads");
        assert!(t.get("k").is_err());
        assert!(t.get_keys().is_err());
        assert_eq!(t.set("k", &[("f", "x".to_string())]), Ok(()), "writes still go through");
        assert_eq!(t.field("k", "f").as_deref(), Some("x"));
    }

    /// `disconnect` is both at once, which is what a redis that went away is.
    #[test]
    fn a_disconnected_table_refuses_everything() {
        let t = MockTable::new();
        t.disconnect("gone");
        assert!(t.set("k", &[]).is_err());
        assert!(t.del("k").is_err());
        assert!(t.hdel("k", "f").is_err());
        assert!(t.get("k").is_err());
        assert!(t.get_keys().is_err());
    }

    /// `fail_writes_after` lets exactly `n` writes through, counting every
    /// kind of write, and then refuses the rest.
    #[test]
    fn fail_writes_after_counts_every_kind_of_write() {
        let t = MockTable::new();
        t.fail_writes_after(2, "spent");
        assert!(t.set("a", &[("f", "1".to_string())]).is_ok(), "first");
        assert!(t.hdel("a", "f").is_ok(), "second, a different kind");
        assert_eq!(t.del("a"), Err("spent".to_string()), "third is refused");
        assert_eq!(t.set("b", &[]), Err("spent".to_string()), "and every one after");
        assert!(t.get("a").is_ok(), "reads are not counted and not refused");
    }

    /// Zero means "refuse from the next write", the same as `fail_writes`.
    #[test]
    fn fail_writes_after_zero_refuses_the_next_write() {
        let t = MockTable::new();
        t.fail_writes_after(0, "now");
        assert!(t.set("a", &[]).is_err());
    }

    #[test]
    fn a_failing_open_names_the_database() {
        let o = MockOpener::failing("PSU_INFO");
        assert!(o.open(STATE_DB, "FAN_INFO").is_ok());
        let err = o.open(STATE_DB, "PSU_INFO").err().expect("this one refuses");
        assert!(err.contains("STATE_DB"), "the message names the database: {err}");
    }

    /// A value that is not UTF-8 keeps its field.  Filtering it out made the
    /// row read as one that never had the field, which is a different fact.
    #[test]
    fn a_value_that_is_not_utf8_keeps_its_field() {
        let row = decode_row([
            ("model".to_string(), CxxString::new(b"MSN\xff4700")),
            ("serial".to_string(), CxxString::new("MT1234")),
        ]);
        assert_eq!(row.len(), 2, "both fields survive");
        assert_eq!(row[0].0, "model");
        assert_eq!(row[0].1, "MSN\u{fffd}4700", "the bad byte is replaced, not the field dropped");
        assert_eq!(row[1], ("serial".to_string(), "MT1234".to_string()));
    }

    /// The sweep sees full keys, which is the whole reason this is not a
    /// `Table`: told only `DPU0` it could not tell DPU_STATE apart from
    /// anything else and would delete the row it must keep.
    #[test]
    fn a_keyspace_matches_on_the_whole_key() {
        let k = MockKeyspace::new(&[
            "DPU_STATE|DPU0", "REBOOT_CAUSE|DPU0|x", "TRANSCEIVER_INFO|DPU0|Ethernet0",
            "DPU_STATE|DPU1",
        ]);
        assert_eq!(k.keys("*DPU0*").unwrap().len(), 3);
        assert_eq!(k.keys("DPU_STATE*").unwrap().len(), 2);
        assert_eq!(k.keys("*Ethernet0").unwrap().len(), 1);
        assert!(k.keys("*NOSUCH*").unwrap().is_empty());
    }

    /// A scan that failed is an error, not an empty key space -- the same
    /// distinction `a_failed_read_is_not_an_absent_row` draws for a table.
    #[test]
    fn a_failed_scan_is_not_an_empty_keyspace() {
        let k = MockKeyspace::new(&["DPU_STATE|DPU0"]);
        k.fail_reads("redis is gone");
        assert_eq!(k.keys("*DPU0*"), Err("redis is gone".to_string()));
        assert_eq!(k.remaining().len(), 1, "a failed scan deletes nothing by itself");
    }

    #[test]
    fn deleting_from_a_keyspace_removes_only_that_key() {
        let k = MockKeyspace::new(&["a|1", "a|2"]);
        k.del("a|1").unwrap();
        assert_eq!(k.remaining(), vec!["a|2".to_string()]);
    }

    /// `hdel` takes one field and leaves the rest, which is what psud's power
    /// budget depends on: a supplier that went away must not take the other
    /// suppliers' entries with it.
    #[test]
    fn deleting_one_field_leaves_the_others() {
        let t = MockTable::new();
        t.set("k", &[("a", "1".to_string()), ("b", "2".to_string())]).unwrap();
        t.hdel("k", "a").unwrap();
        assert_eq!(t.field("k", "a"), None);
        assert_eq!(t.field("k", "b").as_deref(), Some("2"));
        assert!(t.hdel("nosuchkey", "a").is_ok(), "a row that is not there is not an error");
    }

    /// Writes merge, as redis HSET does: a partial write such as the
    /// led_status-only refresh must not read as having erased the row.
    #[test]
    fn a_partial_write_merges_rather_than_replacing() {
        let t = MockTable::new();
        t.set("k", &[("a", "1".to_string())]).unwrap();
        t.set("k", &[("b", "2".to_string())]).unwrap();
        assert_eq!(t.field("k", "a").as_deref(), Some("1"));
        t.set("k", &[("a", "9".to_string())]).unwrap();
        assert_eq!(t.field("k", "a").as_deref(), Some("9"));
    }

    #[test]
    fn a_refusing_table_reports_why_and_can_be_let_through_again() {
        let t = MockTable::new();
        t.fail_writes("read-only replica");
        assert_eq!(t.set("k", &[]).unwrap_err(), "read-only replica");
        assert_eq!(t.hdel("k", "a").unwrap_err(), "read-only replica");
        t.allow_writes();
        assert!(t.set("k", &[("a", "1".to_string())]).is_ok());
    }
}
