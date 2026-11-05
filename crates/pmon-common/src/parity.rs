//! Python daemon against Rust daemon, on one mock platform.
//!
//! Each daemon's unit tests pin down what its authors thought of.  These pin
//! down something else: that it publishes what the Python daemon it replaces
//! publishes, on the same platform, for whatever a scenario does to it.  The
//! platform is the mock the Python daemon's own tests use; the Python half is
//! the daemon script, driven through its own update code; the Rust half is
//! the daemon crate's code over the platform API bridge.  Neither is a model
//! of the other, so a difference is a difference between the two daemons --
//! or between the facade and what the Python daemon makes of the platform,
//! which is the part no test of either alone can see.
//!
//! The Python half is `parity/harness.py`; this is the Rust half's share:
//! loading it, reading back what each side published, and the comparison.
//! One interpreter per test binary, shared with the bridge, so the harness
//! runs one scenario at a time.
//!
//! The tests are behind each daemon crate's `parity` feature, off by default:
//!
//! ```text
//! cargo test --workspace --features parity
//! ```
//!
//! with `PYTHONPATH` reaching the sonic-platform-common checkout the platform
//! API crates come from, and sonic_py_common installed or on `PYTHONPATH`.
//! The deb build runs a plain `cargo test --workspace` (debian/rules) where
//! none of that is, so it must not compile them; the repo's CI turns the
//! feature on.  Nothing is skipped with it on: a module the harness or a
//! Python daemon cannot import fails the test, with the traceback.

use std::collections::BTreeMap;
use std::ffi::CString;
use std::sync::Mutex;

pub use pyo3;
use pyo3::prelude::*;
use pyo3::types::PyModule;

use crate::db::MockTable;

/// What a daemon published: `{database: {table: {key: {field: value}}}}`.
pub type Published = BTreeMap<String, BTreeMap<String, Rows>>;
/// One table's rows: `{key: {field: value}}`.
pub type Rows = BTreeMap<String, BTreeMap<String, String>>;
/// A logged line: (`ERROR` | `WARNING` | `NOTICE`, text).
pub type Line = (String, String);

const HARNESS: &str = include_str!("../parity/harness.py");
const HARNESS_MODULE: &str = "pmon_parity_harness";

/// The scenario currently using the interpreter's global state: the mock
/// chassis `sonic_platform` hands out, the tables, the log.
static ONE_AT_A_TIME: Mutex<()> = Mutex::new(());

fn cstr(s: &str) -> CString {
    CString::new(s).expect("no NUL in Python source")
}

/// Run `f` with the harness installed and the interpreter to itself.
///
/// `PYTHONPATH` has to reach `sonic_platform_base`, `sonic_platform_api` and
/// `sonic_py_common`, as it does for the bridge's own tests.
pub fn with_harness<R>(f: impl FnOnce(Python<'_>, &Bound<'_, PyModule>) -> R) -> R {
    let _one = ONE_AT_A_TIME.lock().unwrap_or_else(|p| p.into_inner());
    Python::with_gil(|py| {
        let harness = match py.import(HARNESS_MODULE) {
            Ok(m) => m,
            Err(_) => PyModule::from_code(
                py,
                &cstr(HARNESS),
                &cstr("harness.py"),
                &cstr(HARNESS_MODULE),
            )
            .unwrap_or_else(|e| panic!("parity harness: {e}")),
        };
        harness
            .call_method0("install")
            .unwrap_or_else(|e| panic!("parity harness install: {}", traceback(py, e)));
        f(py, &harness)
    })
}

/// A daemon's scenario module, from its source.
pub fn module<'py>(py: Python<'py>, name: &str, code: &str) -> Bound<'py, PyModule> {
    PyModule::from_code(py, &cstr(code), &cstr(&format!("{name}.py")), &cstr(name))
        .unwrap_or_else(|e| panic!("{name}: {}", traceback(py, e)))
}

/// An exception with its traceback, for a panic message.
pub fn traceback(py: Python<'_>, e: PyErr) -> String {
    let tb = e
        .traceback(py)
        .and_then(|t| t.format().ok())
        .unwrap_or_default();
    format!("{tb}{e}")
}

/// What the Python half published, and what it logged.
pub fn python_side(py: Python<'_>, harness: &Bound<'_, PyModule>) -> (Published, Vec<Line>) {
    let tables: Published = harness
        .call_method0("tables")
        .and_then(|t| t.extract())
        .unwrap_or_else(|e| panic!("harness.tables(): {}", traceback(py, e)));
    let logs: Vec<Line> = harness
        .call_method0("logs")
        .and_then(|l| l.extract())
        .unwrap_or_else(|e| panic!("harness.logs(): {}", traceback(py, e)));
    (tables, logs)
}

/// A mock table's rows.
pub fn rows(table: &MockTable) -> Rows {
    table
        .keys()
        .into_iter()
        .map(|k| {
            let row = table.row(&k).unwrap_or_default().into_iter().collect();
            (k, row)
        })
        .collect()
}

/// Add one table to `out`, unless it is empty -- an empty table is one
/// nobody wrote, on either side.
pub fn put(out: &mut Published, db: &str, table: &str, rows: Rows) {
    if !rows.is_empty() {
        out.entry(db.to_string())
            .or_default()
            .insert(table.to_string(), rows);
    }
}

/// The Rust half's log, at the severities that reach syslog by default.
pub fn rust_lines(lines: Vec<(log::Level, String)>) -> Vec<Line> {
    lines
        .into_iter()
        .filter_map(|(level, text)| {
            let level = match level {
                log::Level::Error => "ERROR",
                log::Level::Warn => "WARNING",
                log::Level::Info => "NOTICE",
                _ => return None,
            };
            Some((level.to_string(), text))
        })
        .collect()
}

/// Every way the two halves differ, readably; empty when they agree.
///
/// Fields named in `mask` are compared for presence only (a timestamp).
/// Logs are compared as multisets: the order two daemons interleave their
/// lines in is not something either promises.
pub fn differences(
    scenario: &str,
    python: &(Published, Vec<Line>),
    rust: &(Published, Vec<Line>),
    mask: &[&str],
) -> Vec<String> {
    let mut out = Vec::new();
    // A field named in `mask` -- or ending in what a `*name` entry names --
    // is compared for presence only; times anywhere else are masked out of
    // the text.
    let hidden = |f: &str| {
        mask.iter().any(|m| {
            m.strip_prefix('*')
                .map_or(*m == f, |suffix| f.ends_with(suffix))
        })
    };
    let masked = |row: &BTreeMap<String, String>| -> BTreeMap<String, String> {
        row.iter()
            .map(|(f, v)| {
                (
                    f.clone(),
                    if hidden(f) {
                        "<masked>".into()
                    } else {
                        mask_times(v)
                    },
                )
            })
            .collect()
    };
    let python = (mask_keys(&python.0), python.1.clone());
    let rust = (mask_keys(&rust.0), rust.1.clone());
    let empty = BTreeMap::new();
    let dbs: std::collections::BTreeSet<&String> = python.0.keys().chain(rust.0.keys()).collect();
    for db in dbs {
        let (p, r) = (
            python.0.get(db).unwrap_or(&empty),
            rust.0.get(db).unwrap_or(&empty),
        );
        let names: std::collections::BTreeSet<&String> = p.keys().chain(r.keys()).collect();
        for table in names {
            let none = Rows::new();
            let (pt, rt) = (p.get(table).unwrap_or(&none), r.get(table).unwrap_or(&none));
            let keys: std::collections::BTreeSet<&String> = pt.keys().chain(rt.keys()).collect();
            for key in keys {
                match (pt.get(key), rt.get(key)) {
                    (Some(a), None) => out.push(format!(
                        "[{scenario}] {db} {table}|{key}: only Python wrote it: {a:?}"
                    )),
                    (None, Some(b)) => out.push(format!(
                        "[{scenario}] {db} {table}|{key}: only Rust wrote it: {b:?}"
                    )),
                    (Some(a), Some(b)) => {
                        let (a, b) = (masked(a), masked(b));
                        if a != b {
                            let fields: std::collections::BTreeSet<&String> =
                                a.keys().chain(b.keys()).collect();
                            for f in fields {
                                if a.get(f) != b.get(f) {
                                    out.push(format!(
                                        "[{scenario}] {db} {table}|{key} {f}: Python {:?}, Rust {:?}",
                                        a.get(f),
                                        b.get(f)
                                    ));
                                }
                            }
                        }
                    }
                    (None, None) => {}
                }
            }
        }
    }
    // A line that prints the time printed a different one on each side.
    let masked_lines = |lines: &[Line]| -> Vec<Line> {
        let mut v: Vec<Line> = lines
            .iter()
            .map(|(l, t)| (l.clone(), mask_times(t)))
            .collect();
        v.sort();
        v
    };
    let a = masked_lines(&python.1);
    let mut b = masked_lines(&rust.1);
    for line in &a {
        if let Some(i) = b.iter().position(|l| l == line) {
            b.remove(i);
        } else {
            out.push(format!(
                "[{scenario}] only Python logged: {} {}",
                line.0, line.1
            ));
        }
    }
    for line in b {
        out.push(format!(
            "[{scenario}] only Rust logged: {} {}",
            line.0, line.1
        ));
    }
    out
}

/// `text` with every `%Y-%m-%d %H:%M:%S`, `%Y%m%d %H:%M:%S` and
/// `%Y_%m_%d_%H_%M_%S` in it -- the spellings the daemons log the time in,
/// and name reboot-cause records by -- replaced by `<time>`.
pub fn mask_times(text: &str) -> String {
    const SHAPES: [&str; 3] = [
        "dddd-dd-dd dd:dd:dd",
        "dddddddd dd:dd:dd",
        "dddd_dd_dd_dd_dd_dd",
    ];
    let bytes = text.as_bytes();
    let fits = |at: usize, shape: &str| {
        shape.len() <= bytes.len() - at
            && shape.bytes().zip(&bytes[at..]).all(|(s, b)| {
                if s == b'd' {
                    b.is_ascii_digit()
                } else {
                    s == *b
                }
            })
    };
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    while i < bytes.len() {
        if let Some(shape) = SHAPES.iter().find(|s| fits(i, s)) {
            out.push_str("<time>");
            i += shape.len();
        } else {
            let c = text[i..].chars().next().unwrap();
            out.push(c);
            i += c.len_utf8();
        }
    }
    out
}

/// The same tables with the times taken out of every key: a reboot-cause
/// record is keyed by when it was taken.
fn mask_keys(p: &Published) -> Published {
    p.iter()
        .map(|(db, tables)| {
            let tables = tables
                .iter()
                .map(|(t, rows)| {
                    (
                        t.clone(),
                        rows.iter()
                            .map(|(k, v)| (mask_times(k), v.clone()))
                            .collect(),
                    )
                })
                .collect();
            (db.clone(), tables)
        })
        .collect()
}

/// A difference the comparison is expected to find, and why it is not
/// fixed in the daemon: a scenario, text its report line contains, and the
/// reason -- the place in the Python it reproduces, and what would remove it.
pub struct Known {
    pub scenario: &'static str,
    pub contains: &'static str,
    pub why: &'static str,
}

/// Fail on any difference not in `known`, and on any entry in `known` that
/// no longer happens -- one that has been fixed has to come out of the list,
/// or the list stops saying what is true.
pub fn assert_parity(daemon: &str, failures: &[String], known: &[Known]) {
    let mut matched = vec![false; known.len()];
    let mut unexplained = Vec::new();
    for f in failures {
        let hit = known
            .iter()
            .position(|k| f.starts_with(&format!("[{}] ", k.scenario)) && f.contains(k.contains));
        match hit {
            Some(i) => matched[i] = true,
            None => unexplained.push(f.as_str()),
        }
    }
    let gone: Vec<String> = known
        .iter()
        .zip(&matched)
        .filter(|(_, m)| !**m)
        .map(|(k, _)| format!("[{}] {:?} ({})", k.scenario, k.contains, k.why))
        .collect();
    assert!(
        unexplained.is_empty() && gone.is_empty(),
        "{daemon} and the Python daemon differ:\n{}\nknown differences that no longer occur:\n{}",
        unexplained.join("\n"),
        gone.join("\n")
    );
}

/// A table in the harness's store -- the one the Python half's stand-in
/// `swsscommon` writes -- for the Rust half to use in place of a
/// [`MockTable`].
///
/// Needed where the platform writes the daemon's table itself:
/// syseepromd's `EEPROM_INFO` is written by the vendor's
/// `update_eeprom_db`, which over the bridge is Python code writing through
/// `swsscommon`, so the daemon has to read back and clear the same rows.
/// Using it everywhere is also the stricter comparison: both halves then
/// write through the same semantics.
pub struct PyTable {
    db: String,
    table: String,
}

impl PyTable {
    pub fn new(db: &str, table: &str) -> Self {
        Self {
            db: db.to_string(),
            table: table.to_string(),
        }
    }

    fn call<R>(
        &self,
        f: impl for<'py> FnOnce(&Bound<'py, PyAny>) -> PyResult<R>,
    ) -> Result<R, String> {
        Python::with_gil(|py| {
            let h = py.import(HARNESS_MODULE)?;
            let db = h.getattr("Db")?.call1((self.db.as_str(),))?;
            let table = h.getattr("Table")?.call1((db, self.table.as_str()))?;
            f(&table)
        })
        .map_err(|e| e.to_string())
    }

    /// Its rows, as [`rows`] gives a mock's.
    pub fn rows(&self) -> Rows {
        self.call(|t| t.getattr("rows")?.extract())
            .unwrap_or_default()
    }
}

impl crate::db::TableLike for PyTable {
    fn set(&self, key: &str, fvs: &[(&str, String)]) -> Result<(), String> {
        let pairs: Vec<(String, String)> = fvs
            .iter()
            .map(|(f, v)| (f.to_string(), v.clone()))
            .collect();
        self.call(|t| t.call_method1("set", (key, pairs)).map(|_| ()))
    }

    fn del(&self, key: &str) -> Result<(), String> {
        self.call(|t| t.call_method1("_del", (key,)).map(|_| ()))
    }

    fn hdel(&self, key: &str, field: &str) -> Result<(), String> {
        self.call(|t| t.call_method1("hdel", (key, field)).map(|_| ()))
    }

    fn get(&self, key: &str) -> Result<Option<Vec<(String, String)>>, String> {
        self.call(|t| {
            let answer = t.call_method1("get", (key,))?;
            let found: bool = answer.get_item(0)?.extract()?;
            let fvs: Vec<(String, String)> = answer.get_item(1)?.extract()?;
            Ok(found.then_some(fvs))
        })
    }

    fn get_keys(&self) -> Result<Vec<String>, String> {
        self.call(|t| t.call_method0("getKeys")?.extract())
    }
}
