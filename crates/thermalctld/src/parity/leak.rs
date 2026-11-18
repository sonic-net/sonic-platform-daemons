//! Run the shipped leak loop and original Python worker against identical inputs.
use super::*;
use crate::db::TableLike;
use std::collections::BTreeMap;

const CASES: &str = include_str!("leak_scenarios.py");
type Snapshot = (Published, Vec<Line>, Vec<Line>);

struct Policy(Py<PyAny>);
impl TableLike for Policy {
    fn set(&self, _: &str, _: &[(&str, String)]) -> Result<(), String> {
        Err("read-only policy".into())
    }
    fn del(&self, _: &str) -> Result<(), String> {
        Err("read-only policy".into())
    }
    fn hdel(&self, _: &str, _: &str) -> Result<(), String> {
        Err("read-only policy".into())
    }
    fn get_keys(&self) -> Result<Vec<String>, String> {
        Ok(vec!["policy".into()])
    }
    fn get(&self, _: &str) -> Result<Option<Vec<(String, String)>>, String> {
        Python::attach(|py| {
            self.0
                .bind(py)
                .call_method0("policy_entry")
                .and_then(|value| value.extract())
                .map_err(|error| error.value(py).repr().unwrap().to_string())
        })
    }
}

fn tables(db: &crate::db::mock::MockLeak) -> Published {
    let mut result = Published::new();
    for (name, table) in [
        ("LIQUID_COOLING_INFO", &db.sensor),
        ("SYSTEM_LEAK_STATUS", &db.system),
        ("LEAK_PROFILE", &db.profile),
    ] {
        parity::put(&mut result, "STATE_DB", name, parity::rows(table));
    }
    result
}

fn events(path: &std::path::Path) -> Vec<Line> {
    std::fs::read_to_string(path)
        .unwrap_or_default()
        .lines()
        .map(|line| {
            let (_, event) = line.split_once("] ").expect("an event has a process id");
            let (level, text) = event.split_once(": ").expect("an event has a severity");
            match (level, text.strip_prefix("NOTICE: ")) {
                ("INFO", Some(text)) => ("NOTICE".into(), text.into()),
                _ => (level.into(), text.into()),
            }
        })
        .collect()
}

fn timestamps_are_fresh(snapshot: &Published) {
    let now = chrono::Local::now().naive_local();
    for (table, rows) in snapshot.get("STATE_DB").into_iter().flatten() {
        if table == "LEAK_PROFILE" {
            continue;
        }
        for (key, fields) in rows {
            let value = fields
                .get("timestamp")
                .unwrap_or_else(|| panic!("{table}|{key} has no timestamp"));
            let timestamp = chrono::NaiveDateTime::parse_from_str(value, "%Y%m%d %H:%M:%S")
                .expect("valid timestamp");
            assert!(
                (now - timestamp).num_seconds().abs() <= 60,
                "stale timestamp in {table}|{key}: {value}"
            );
        }
    }
}

#[test]
fn leak_workers_match_tables_errors_transitions_restart_and_bmc_events() {
    let failures = parity::with_harness(|py, harness| {
        let scenarios = parity::module(py, "thermalctld_leak_scenarios", CASES);
        scenarios.call_method1("setup", (harness, ROOT)).unwrap();
        let cases: Vec<String> = scenarios.call_method0("names").unwrap().extract().unwrap();
        let mut failures = Vec::new();
        for case in cases {
            let expected: Vec<Snapshot> = scenarios
                .call_method1("run_python", (&case,))
                .unwrap_or_else(|error| panic!("Python {case}: {}", parity::traceback(py, error)))
                .extract()
                .unwrap();
            harness.call_method0("reset").unwrap();
            let fixture = scenarios.call_method1("make", (&case,)).unwrap();
            let chassis = fixture.call_method0("chassis").unwrap();
            harness.call_method1("use_chassis", (&chassis,)).unwrap();
            let mut platform = platform_provider::open("thermalctld", PlatformImpl::Pyo3).unwrap();
            let mut db = crate::db::mock::MockLeak::new();
            let initial: BTreeMap<String, parity::Rows> = fixture
                .call_method0("initial_rows")
                .unwrap()
                .extract()
                .unwrap();
            for (table, rows) in initial {
                let target: &dyn TableLike = match table.as_str() {
                    "LIQUID_COOLING_INFO" => &db.sensor,
                    "SYSTEM_LEAK_STATUS" => &db.system,
                    "LEAK_PROFILE" => &db.profile,
                    _ => panic!("unexpected table {table}"),
                };
                for (key, fields) in rows {
                    let values: Vec<(&str, String)> = fields
                        .iter()
                        .map(|(k, v)| (k.as_str(), v.clone()))
                        .collect();
                    target.set(&key, &values).unwrap();
                }
            }
            db.tables.policy = Some(Box::new(Policy(fixture.clone().unbind())));
            let directory = tempfile::tempdir().unwrap();
            let path = directory.path().join("event.log");
            let mut logger = crate::event_log::BmcEventLogger::with_path(true, &path);
            let log = pmon_common::logging::capture();
            let (_shutdown, receiver) = watch::channel(false);
            let begin = Instant::now();
            let mut observed = Vec::new();
            let result = crate::leak_loop_with(
                &mut platform,
                &db.tables,
                Duration::from_millis(1),
                receiver,
                &mut logger,
                || {
                    begin
                        + Duration::from_secs(
                            fixture.call_method0("seconds").unwrap().extract().unwrap(),
                        )
                },
                |_, _| {
                    observed.push((tables(&db), parity::rust_lines(log.lines()), events(&path)));
                    fixture.call_method0("advance").unwrap().extract().unwrap()
                },
            );
            assert_eq!(result, None, "{case}: read failures must be retried");
            assert_eq!(
                observed.len(),
                expected.len(),
                "{case}: every cycle was exercised"
            );
            for (index, (python, rust)) in expected.iter().zip(&observed).enumerate() {
                let label = format!("{case}/{index}");
                timestamps_are_fresh(&python.0);
                timestamps_are_fresh(&rust.0);
                if index > 0 {
                    for (previous, current) in [
                        (&expected[index - 1].0, &python.0),
                        (&observed[index - 1].0, &rust.0),
                    ] {
                        // Empty publications omit the database/table entirely.
                        // Only a sensor present in both snapshots has a leak
                        // start time to preserve. The full comparison below
                        // still rejects missing rows on either provider.
                        let current_rows = current
                            .get("STATE_DB")
                            .and_then(|tables| tables.get("LIQUID_COOLING_INFO"));
                        let previous_rows = previous
                            .get("STATE_DB")
                            .and_then(|tables| tables.get("LIQUID_COOLING_INFO"));
                        for (key, fields) in current_rows.into_iter().flatten() {
                            if let Some(old) = previous_rows.and_then(|rows| rows.get(key)) {
                                if old.get("leaking").map(String::as_str) == Some("Yes")
                                    && fields.get("leaking").map(String::as_str) == Some("Yes")
                                    && old.get("test_leak") == fields.get("test_leak")
                                {
                                    assert_eq!(
                                        old.get("timestamp"),
                                        fields.get("timestamp"),
                                        "{label}: a standing leak keeps its start time"
                                    );
                                }
                            }
                        }
                    }
                }
                failures.extend(parity::differences(
                    &label,
                    &(python.0.clone(), python.1.clone()),
                    &(rust.0.clone(), rust.1.clone()),
                    &["timestamp"],
                ));
                if python.2 != rust.2 {
                    failures.push(format!(
                        "{label}: BMC events differ: Python {:?}; Rust {:?}",
                        python.2, rust.2
                    ));
                }
            }
            assert!(
                db.sensor.is_empty() && db.system.is_empty() && db.profile.is_empty(),
                "{case}: shutdown removes all three owned tables"
            );
        }
        failures
    });
    parity::assert_parity("thermalctld leak worker", &failures, &[]);
}

#[test]
fn every_platform_open_gets_its_own_chassis_over_the_same_mock_hardware() {
    parity::with_harness(|py, harness| {
        let scenarios = parity::module(py, "thermalctld_leak_handles", CASES);
        scenarios.call_method1("setup", (harness, ROOT)).unwrap();
        let fixture = scenarios.call_method1("make", ("transitions",)).unwrap();
        let chassis = fixture.call_method0("chassis").unwrap();
        harness.call_method1("use_chassis", (chassis,)).unwrap();
        let platform = py
            .import("sonic_platform.platform")
            .unwrap()
            .getattr("Platform")
            .unwrap();
        let first = platform
            .call0()
            .unwrap()
            .call_method0("get_chassis")
            .unwrap();
        let second = platform
            .call0()
            .unwrap()
            .call_method0("get_chassis")
            .unwrap();
        assert!(!first.is(&second));
        assert!(first
            .getattr("cooling")
            .unwrap()
            .is(second.getattr("cooling").unwrap()));
    });
}
