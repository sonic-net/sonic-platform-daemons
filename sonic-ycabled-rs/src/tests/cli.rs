//! Tests of the `show/config muxcable` commands (HLD 3.4.9, 3.6.5), against
//! the mocks of [`crate::testing`]: Python ycabled's
//! handle_*_cmd_arg_tbl_notification tests and the helpers they use. Each
//! test notes the Python tests whose behavior it covers. A command is run
//! as the CLI runs it: the argument row, then the command row; the answer
//! is the response field, the result row, and the command key deleted.

// The table-driven cases spell out their tuple types.
#![allow(clippy::type_complexity)]

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use crate::actor::{Msg, PortActor};
use crate::cli::{test_firmware_dir, Cmd, COMMANDS};
use crate::config::CableType;
use crate::db::DbId;
use crate::pyval::{PyArg, PyVal};
use crate::schema::*;
use crate::testing::*;

const XCVRD_SHOW_HWMODE_DIR_CMD: &str = "XCVRD_SHOW_HWMODE_DIR_CMD";
const XCVRD_CONFIG_HWMODE_DIR_CMD: &str = "XCVRD_CONFIG_HWMODE_DIR_CMD";
const XCVRD_SHOW_HWMODE_SWMODE_CMD: &str = "XCVRD_SHOW_HWMODE_SWMODE_CMD";
const XCVRD_CONFIG_HWMODE_SWMODE_CMD: &str = "XCVRD_CONFIG_HWMODE_SWMODE_CMD";
const XCVRD_SHOW_FW_CMD: &str = "XCVRD_SHOW_FW_CMD";
const XCVRD_DOWN_FW_CMD: &str = "XCVRD_DOWN_FW_CMD";
const XCVRD_ACTI_FW_CMD: &str = "XCVRD_ACTI_FW_CMD";
const XCVRD_ROLL_FW_CMD: &str = "XCVRD_ROLL_FW_CMD";
const XCVRD_CONFIG_PRBS_CMD: &str = "XCVRD_CONFIG_PRBS_CMD";
const XCVRD_CONFIG_LOOP_CMD: &str = "XCVRD_CONFIG_LOOP_CMD";
const XCVRD_EVENT_LOG_CMD: &str = "XCVRD_EVENT_LOG_CMD";
const XCVRD_GET_FEC_CMD: &str = "XCVRD_GET_FEC_CMD";
const XCVRD_GET_BER_CMD: &str = "XCVRD_GET_BER_CMD";

#[derive(Debug)]
struct Answer {
    rsp: Option<String>,
    res: HashMap<String, String>,
    deleted: bool,
}

impl Answer {
    fn rsp(&self) -> &str {
        self.rsp.as_deref().unwrap_or("<none>")
    }
}

fn command(table: &str) -> &'static Cmd {
    COMMANDS
        .iter()
        .find(|c| c.table == table)
        .expect("a CLI command table")
}

fn answer(f: &Fixture, cmd: &Cmd) -> Answer {
    Answer {
        rsp: f
            .db
            .row(DbId::State, cmd.rsp, TEST_PORT)
            .and_then(|r| r.get(cmd.output).cloned()),
        res: cmd
            .res
            .and_then(|t| f.db.row(DbId::State, t, TEST_PORT))
            .unwrap_or_default(),
        deleted: f.db.row(DbId::Appl, cmd.table, TEST_PORT).is_none(),
    }
}

/// Runs a command: `input` is the value of its field, None for a command
/// row without it.
async fn cli(
    f: &Fixture,
    a: &mut PortActor,
    table: &str,
    input: Option<&str>,
    args: &[(&str, &str)],
) -> Answer {
    let cmd = command(table);
    f.db.remove(DbId::State, cmd.rsp, TEST_PORT);
    if let Some(res) = cmd.res {
        f.db.remove(DbId::State, res, TEST_PORT);
    }
    if let Some(arg) = cmd.arg {
        f.db.remove(DbId::Appl, arg, TEST_PORT);
        if !args.is_empty() {
            f.db.put(DbId::Appl, arg, TEST_PORT, args);
        }
    }
    let row = match input {
        Some(v) => vec![(cmd.input, v)],
        None => vec![("bogus", "1")],
    };
    f.db.put(DbId::Appl, table, TEST_PORT, &row);
    a.handle(Msg::Cli(table.to_string(), fm(&row))).await;
    answer(f, cmd)
}

/// An active-standby port whose driver is loaded.
async fn ready(f: &Fixture) -> PortActor {
    let mut a = f.standby();
    a.handle(Msg::Init(None)).await;
    f.platform.clear_calls();
    a
}

/// The ports a command cannot reach the cable of, with what each is:
/// not in the port map, cable absent, no usable driver.
async fn unreachable_ports() -> Vec<(&'static str, Fixture, PortActor)> {
    let mut out = Vec::new();
    let f = Fixture::new();
    let a = PortActor::new(
        TEST_PORT,
        None,
        Some(CableType::ActiveStandby),
        Arc::clone(&f.shared),
    );
    out.push(("no_port", f, a));
    let f = Fixture::new();
    f.cable(|c| c.present = false);
    let a = ready(&f).await;
    out.push(("absent", f, a));
    let f = Fixture::new();
    f.cable(|c| c.load = None);
    let a = ready(&f).await;
    out.push(("no_instance", f, a));
    out
}

fn calls(f: &Fixture) -> Vec<(String, Vec<PyArg>)> {
    f.platform.calls_with_args(PHYSICAL)
}

fn ints(v: &[i64]) -> Vec<PyArg> {
    v.iter().map(|i| PyArg::Int(*i)).collect()
}

// --- show muxcable hwmode muxdirection ------------------------------------------------

/// test_handle_show_mux_state_cmd_arg_tbl_notification_with_instance_auto_active_standby,
/// _with_instance_manual: active when the cable points at this ToR's side.
#[tokio::test]
async fn show_mux_direction_active_standby() {
    for (read_side, direction, state) in [
        (1, 1, "active"),
        (1, 2, "standby"),
        (2, 2, "active"),
        (2, 1, "standby"),
    ] {
        let f = Fixture::new();
        f.cable(|c| {
            c.read_side = read_side;
            c.mux_direction = direction;
        });
        let mut a = ready(&f).await;
        let r = cli(&f, &mut a, XCVRD_SHOW_HWMODE_DIR_CMD, Some("probe"), &[]).await;
        assert_eq!(
            r.rsp(),
            state,
            "read side {read_side}, direction {direction}"
        );
        assert_eq!(r.res, fm(&[("presence", "True")]));
        assert!(r.deleted);
    }
}

/// _no_port, _no_instance, and a cable whose sides cannot be read.
#[tokio::test]
async fn show_mux_direction_failures() {
    for (case, f, mut a) in unreachable_ports().await {
        let r = cli(&f, &mut a, XCVRD_SHOW_HWMODE_DIR_CMD, Some("probe"), &[]).await;
        let (state, presence) = match case {
            "no_instance" => ("not Y-Cable port", "True"),
            _ => ("unknown", "False"),
        };
        assert_eq!(
            (r.rsp(), r.res["presence"].as_str()),
            (state, presence),
            "{case}"
        );
        assert!(r.deleted, "{case}");
    }
    for method in ["get_read_side", "get_mux_direction"] {
        for reply in [
            raise("OSError('I2C')"),
            value(PyVal::Int(-1)),
            value(PyVal::None),
            value(PyVal::Int(0)),
        ] {
            let f = Fixture::new();
            let mut a = ready(&f).await;
            f.cable(|c| {
                c.script(method, reply.clone());
            });
            let r = cli(&f, &mut a, XCVRD_SHOW_HWMODE_DIR_CMD, Some("probe"), &[]).await;
            // Read side 0, the NIC's, is unknown too (HLD 3.8 issue 31).
            assert_eq!(r.rsp(), "unknown", "{method} {reply:?}");
        }
    }
}

/// _else_condition: a command row without its field is neither answered
/// nor deleted, as in Python.
#[tokio::test]
async fn show_mux_direction_without_input_is_not_answered() {
    let f = Fixture::new();
    let mut a = ready(&f).await;
    let r = cli(&f, &mut a, XCVRD_SHOW_HWMODE_DIR_CMD, None, &[]).await;
    assert_eq!(r.rsp, None);
    assert!(!r.deleted);
}

/// _with_instance_auto_active_active, _active_active_none: the state
/// ycabled published, or unknown without one.
#[tokio::test]
async fn show_mux_direction_active_active() {
    let f = Fixture::new();
    let mut a = f.active();
    a.handle(Msg::Init(None)).await;
    let published = f.row(DbId::State, HW_MUX_CABLE_TABLE)["state"].clone();
    let r = cli(&f, &mut a, XCVRD_SHOW_HWMODE_DIR_CMD, Some("probe"), &[]).await;
    assert_eq!(
        (r.rsp(), r.res["presence"].as_str()),
        (published.as_str(), "True")
    );

    let f = Fixture::new();
    let mut a = f.active();
    let r = cli(&f, &mut a, XCVRD_SHOW_HWMODE_DIR_CMD, Some("probe"), &[]).await;
    assert_eq!(r.rsp(), "unknown");
}

// --- config muxcable hwmode state -------------------------------------------------------

/// test_handle_config_mux_state_cmd_arg_tbl_notification_with_instance_auto,
/// _with_instance_manual, _with_instance_cmd_arg: the cable is toggled to the
/// side that makes this ToR active or standby; HW_MUX_CABLE_TABLE is left alone.
#[tokio::test]
async fn config_mux_state_toggles_the_cable() {
    let cases = [
        (1, "active", "toggle_mux_to_tor_a"),
        (1, "standby", "toggle_mux_to_tor_b"),
        (2, "active", "toggle_mux_to_tor_b"),
        (2, "standby", "toggle_mux_to_tor_a"),
    ];
    for (read_side, state, method) in cases {
        let f = Fixture::new();
        f.cable(|c| c.read_side = read_side);
        let mut a = ready(&f).await;
        let before = f.row(DbId::State, HW_MUX_CABLE_TABLE);
        let r = cli(&f, &mut a, XCVRD_CONFIG_HWMODE_DIR_CMD, Some(state), &[]).await;
        assert_eq!(r.rsp(), "True", "{read_side} {state}");
        assert_eq!(calls(&f).last().map(|c| c.0.as_str()), Some(method));
        assert_eq!(f.row(DbId::State, HW_MUX_CABLE_TABLE), before);
        assert!(r.deleted);
    }
}

/// _no_port, _no_instance, missing state, a state that is neither active
/// nor standby, an unreadable read side, and toggle failures.
#[tokio::test]
async fn config_mux_state_failures() {
    for (case, f, mut a) in unreachable_ports().await {
        let r = cli(&f, &mut a, XCVRD_CONFIG_HWMODE_DIR_CMD, Some("active"), &[]).await;
        assert_eq!(r.rsp(), "False", "{case}");
    }
    let f = Fixture::new();
    let mut a = ready(&f).await;
    assert_eq!(
        cli(&f, &mut a, XCVRD_CONFIG_HWMODE_DIR_CMD, None, &[])
            .await
            .rsp(),
        "False"
    );
    assert_eq!(
        cli(&f, &mut a, XCVRD_CONFIG_HWMODE_DIR_CMD, Some("bogus"), &[])
            .await
            .rsp(),
        "False"
    );
    f.cable(|c| {
        c.script("get_read_side", raise("OSError('I2C')"));
    });
    assert_eq!(
        cli(&f, &mut a, XCVRD_CONFIG_HWMODE_DIR_CMD, Some("active"), &[])
            .await
            .rsp(),
        "False"
    );
    assert!(!calls(&f).iter().any(|c| c.0.starts_with("toggle")));

    for (reply, rsp) in [
        (raise("OSError('I2C')"), "-1"),
        (value(PyVal::Bool(false)), "False"),
    ] {
        let f = Fixture::new();
        let mut a = ready(&f).await;
        f.cable(|c| {
            c.script("toggle_mux_to_tor_a", reply.clone());
        });
        assert_eq!(
            cli(&f, &mut a, XCVRD_CONFIG_HWMODE_DIR_CMD, Some("active"), &[])
                .await
                .rsp(),
            rsp
        );
    }
}

// --- show/config muxcable hwmode switchmode -------------------------------------------

/// test_handle_show_mux_switchmode_cmd_arg_tbl_notification_with_instance_auto,
/// _with_instance_manual, _no_port, _no_instance, _else_condition. The
/// modes are the driver's constants.
#[tokio::test]
async fn show_switch_mode() {
    let f = Fixture::new();
    let mut a = ready(&f).await;
    for (reply, mode) in [
        (value(PyVal::Int(1)), "auto"),
        (value(PyVal::Int(0)), "manual"),
        (value(PyVal::Int(5)), "unknown"),
        (raise("OSError('I2C')"), "unknown"),
    ] {
        f.cable(|c| {
            c.script("get_switching_mode", reply.clone());
        });
        assert_eq!(
            cli(&f, &mut a, XCVRD_SHOW_HWMODE_SWMODE_CMD, Some("probe"), &[])
                .await
                .rsp(),
            mode,
            "{reply:?}"
        );
    }
    f.cable(|c| {
        c.attrs
            .insert("SWITCHING_MODE_MANUAL".into(), PyVal::Int(7));
        c.script("get_switching_mode", value(PyVal::Int(7)));
    });
    assert_eq!(
        cli(&f, &mut a, XCVRD_SHOW_HWMODE_SWMODE_CMD, Some("probe"), &[])
            .await
            .rsp(),
        "manual"
    );
    assert_eq!(
        cli(&f, &mut a, XCVRD_SHOW_HWMODE_SWMODE_CMD, None, &[])
            .await
            .rsp(),
        "unknown"
    );

    for (case, f, mut a) in unreachable_ports().await {
        let r = cli(&f, &mut a, XCVRD_SHOW_HWMODE_SWMODE_CMD, Some("probe"), &[]).await;
        let want = if case == "no_instance" {
            "not Y-Cable port"
        } else {
            "cable not present"
        };
        assert_eq!(r.rsp(), want, "{case}");
    }
}

/// test_handle_config_mux_switchmode_cmd_arg_tbl_notification_with_instance_auto,
/// _with_instance_manual, _with_instance_manual_with_exception,
/// _else_condition, _no_port, _no_instance.
#[tokio::test]
async fn config_switch_mode() {
    for (mode, arg) in [("auto", 1), ("manual", 0)] {
        let f = Fixture::new();
        let mut a = ready(&f).await;
        f.cable(|c| {
            c.script("set_switching_mode", value(PyVal::Bool(true)));
        });
        let r = cli(&f, &mut a, XCVRD_CONFIG_HWMODE_SWMODE_CMD, Some(mode), &[]).await;
        assert_eq!(r.rsp(), "True");
        assert_eq!(
            calls(&f),
            vec![("set_switching_mode".to_string(), ints(&[arg]))]
        );
    }
    let f = Fixture::new();
    let mut a = ready(&f).await;
    f.cable(|c| {
        c.script("set_switching_mode", raise("OSError('I2C')"));
    });
    assert_eq!(
        cli(
            &f,
            &mut a,
            XCVRD_CONFIG_HWMODE_SWMODE_CMD,
            Some("manual"),
            &[]
        )
        .await
        .rsp(),
        "False"
    );
    f.platform.clear_calls();
    assert_eq!(
        cli(
            &f,
            &mut a,
            XCVRD_CONFIG_HWMODE_SWMODE_CMD,
            Some("bogus"),
            &[]
        )
        .await
        .rsp(),
        "False"
    );
    assert_eq!(
        cli(&f, &mut a, XCVRD_CONFIG_HWMODE_SWMODE_CMD, None, &[])
            .await
            .rsp(),
        "False"
    );
    assert!(calls(&f).is_empty());

    for (case, f, mut a) in unreachable_ports().await {
        assert_eq!(
            cli(
                &f,
                &mut a,
                XCVRD_CONFIG_HWMODE_SWMODE_CMD,
                Some("auto"),
                &[]
            )
            .await
            .rsp(),
            "False",
            "{case}"
        );
    }
}

// --- firmware -------------------------------------------------------------------------

const FW_FIELDS: [&str; 9] = [
    "version_self_active",
    "version_self_inactive",
    "version_self_next",
    "version_peer_active",
    "version_peer_inactive",
    "version_peer_next",
    "version_nic_active",
    "version_nic_inactive",
    "version_nic_next",
];

/// The mock cable's versions of `target` (see MockCable).
fn versions(target: i64) -> [String; 3] {
    [
        format!("1.{target}.0"),
        format!("0.{target}.9"),
        format!("1.{target}.0"),
    ]
}

fn firmware_row(self_t: i64, peer_t: i64) -> HashMap<String, String> {
    let values: Vec<String> = versions(self_t)
        .into_iter()
        .chain(versions(peer_t))
        .chain(versions(0))
        .collect();
    FW_FIELDS
        .iter()
        .map(|f| f.to_string())
        .zip(values)
        .collect()
}

/// test_handle_show_firmware_show_cmd_arg_tbl_notification_with_instance,
/// test_set_show_firmware_fields, test_get_firmware_dict: self and peer by
/// read side, then the NIC; a side that cannot be read is N/A.
#[tokio::test]
async fn show_firmware_versions() {
    for (read_side, self_t, peer_t) in [(1, 1, 2), (2, 2, 1)] {
        let f = Fixture::new();
        f.cable(|c| c.read_side = read_side);
        let mut a = ready(&f).await;
        let r = cli(&f, &mut a, XCVRD_SHOW_FW_CMD, Some("probe"), &[]).await;
        assert_eq!(r.rsp(), "True");
        assert_eq!(r.res, firmware_row(self_t, peer_t));
    }
    let f = Fixture::new();
    let mut a = ready(&f).await;
    f.cable(|c| {
        c.script("get_firmware_version", raise("OSError('I2C')"));
    });
    let r = cli(&f, &mut a, XCVRD_SHOW_FW_CMD, Some("probe"), &[]).await;
    assert_eq!(r.rsp(), "True");
    assert!(r.res.values().all(|v| v == "N/A"), "{:?}", r.res);
}

/// test_get_firmware_dict (download in progress): the last known versions,
/// without calling the cable; N/A without any.
#[tokio::test]
async fn show_firmware_during_download() {
    let f = Fixture::new();
    let mut a = ready(&f).await;
    f.cable(|c| {
        c.attrs
            .insert("download_firmware_status".into(), PyVal::Int(1));
    });
    f.db.remove(DbId::State, MUX_CABLE_INFO, TEST_PORT);
    let r = cli(&f, &mut a, XCVRD_SHOW_FW_CMD, Some("probe"), &[]).await;
    assert_eq!(r.rsp(), "True");
    assert!(r.res.values().all(|v| v == "N/A"));
    f.db.put(
        DbId::State,
        MUX_CABLE_INFO,
        TEST_PORT,
        &[
            ("version_self_active", "9.9.0"),
            ("version_nic_next", "9.9.8"),
        ],
    );
    let r = cli(&f, &mut a, XCVRD_SHOW_FW_CMD, Some("probe"), &[]).await;
    assert_eq!(
        (
            r.res["version_self_active"].as_str(),
            r.res["version_nic_next"].as_str()
        ),
        ("9.9.0", "9.9.8")
    );
    assert_eq!(r.res["version_peer_active"], "None");
    assert!(!calls(&f).iter().any(|c| c.0 == "get_firmware_version"));
}

/// _no_port, _no_instance, _else_condition, and an unreadable read side:
/// every version N/A and status False.
#[tokio::test]
async fn show_firmware_failures() {
    let all_na: HashMap<String, String> = FW_FIELDS
        .iter()
        .map(|f| (f.to_string(), "N/A".to_string()))
        .collect();
    for (case, f, mut a) in unreachable_ports().await {
        let r = cli(&f, &mut a, XCVRD_SHOW_FW_CMD, Some("probe"), &[]).await;
        assert_eq!((r.rsp(), &r.res), ("False", &all_na), "{case}");
    }
    let f = Fixture::new();
    let mut a = ready(&f).await;
    let r = cli(&f, &mut a, XCVRD_SHOW_FW_CMD, None, &[]).await;
    assert_eq!((r.rsp(), &r.res), ("False", &all_na));
    f.cable(|c| {
        c.script("get_read_side", raise("OSError('I2C')"));
    });
    let r = cli(&f, &mut a, XCVRD_SHOW_FW_CMD, Some("probe"), &[]).await;
    assert_eq!((r.rsp(), &r.res), ("False", &all_na));
}

fn firmware_file(name: &str) -> String {
    let path = test_firmware_dir().join(name);
    std::fs::write(&path, b"image").unwrap();
    path.to_string_lossy().into_owned()
}

/// Waits for the answer of a command that answers from a task of its own
/// (paused time: the waits cost nothing).
async fn wait_answer(f: &Fixture, table: &str) -> Answer {
    for _ in 0..300 {
        let r = answer(f, command(table));
        if r.rsp.is_some() {
            return r;
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
    panic!("no answer to {table}");
}

/// test_handle_config_firmware_down_cmd_arg_tbl_notification_with_instance,
/// test_task_download_firmware_worker: the download runs beside the port's
/// other work, and its status is the answer.
#[tokio::test(start_paused = true)]
async fn download_firmware() {
    let f = Fixture::new();
    let mut a = ready(&f).await;
    let path = firmware_file("down-ok.bin");
    f.cable(|c| {
        c.script("download_firmware", value(PyVal::Int(0)));
        c.delays
            .insert("download_firmware".into(), Duration::from_secs(60));
    });
    let r = cli(&f, &mut a, XCVRD_DOWN_FW_CMD, Some("down-ok.bin"), &[]).await;
    assert_eq!(r.rsp, None, "answered before the download ended");
    // The port still serves toggles meanwhile.
    f.db.put(
        DbId::Appl,
        HW_MUX_CABLE_TABLE,
        TEST_PORT,
        &[("state", "standby")],
    );
    a.handle(Msg::Toggle(fm(&[("state", "standby")]))).await;
    assert_eq!(f.mux_state().0, "standby");
    let r = wait_answer(&f, XCVRD_DOWN_FW_CMD).await;
    assert_eq!((r.rsp(), r.deleted), ("0", true));
    assert!(calls(&f).contains(&("download_firmware".to_string(), vec![PyArg::Str(path)])));

    // The driver raises: -1.
    let f = Fixture::new();
    let mut a = ready(&f).await;
    firmware_file("down-raise.bin");
    f.cable(|c| {
        c.script("download_firmware", raise("OSError('I2C')"));
    });
    cli(&f, &mut a, XCVRD_DOWN_FW_CMD, Some("down-raise.bin"), &[]).await;
    assert_eq!(wait_answer(&f, XCVRD_DOWN_FW_CMD).await.rsp(), "-1");
}

/// _no_port, _no_instance, _else_condition, and an image that does not
/// exist: -1 at once, nothing downloaded.
#[tokio::test]
async fn download_firmware_failures() {
    firmware_file("down-fail.bin");
    for (case, f, mut a) in unreachable_ports().await {
        assert_eq!(
            cli(&f, &mut a, XCVRD_DOWN_FW_CMD, Some("down-fail.bin"), &[])
                .await
                .rsp(),
            "-1",
            "{case}"
        );
    }
    let f = Fixture::new();
    let mut a = ready(&f).await;
    assert_eq!(
        cli(&f, &mut a, XCVRD_DOWN_FW_CMD, None, &[]).await.rsp(),
        "-1"
    );
    assert_eq!(
        cli(
            &f,
            &mut a,
            XCVRD_DOWN_FW_CMD,
            Some("no-such-image.bin"),
            &[]
        )
        .await
        .rsp(),
        "-1"
    );
    assert!(calls(&f).is_empty());
}

/// test_handle_config_firmware_acti_cmd_arg_tbl_notification_with_instance,
/// _else_condition, _no_port, _no_instance: activate an image, or the
/// inactive one ("null"); `hitless` in the arguments asks for a non-hitless
/// activation.
#[tokio::test(start_paused = true)]
async fn activate_firmware() {
    let path = firmware_file("acti.bin");
    let f = Fixture::new();
    let mut a = ready(&f).await;
    f.cable(|c| {
        c.script("activate_firmware", value(PyVal::Int(0)));
    });
    assert_eq!(
        cli(&f, &mut a, XCVRD_ACTI_FW_CMD, Some("acti.bin"), &[])
            .await
            .rsp(),
        "0"
    );
    assert_eq!(
        cli(
            &f,
            &mut a,
            XCVRD_ACTI_FW_CMD,
            Some("null"),
            &[("hitless", "1")]
        )
        .await
        .rsp(),
        "0"
    );
    assert_eq!(
        calls(&f),
        vec![
            (
                "activate_firmware".to_string(),
                vec![PyArg::Str(path), PyArg::Bool(true)]
            ),
            (
                "activate_firmware".to_string(),
                vec![PyArg::None, PyArg::Bool(false)]
            ),
        ]
    );
    f.cable(|c| {
        c.script("activate_firmware", raise("OSError('I2C')"));
    });
    assert_eq!(
        cli(&f, &mut a, XCVRD_ACTI_FW_CMD, Some("null"), &[])
            .await
            .rsp(),
        "-1"
    );
    assert_eq!(
        cli(&f, &mut a, XCVRD_ACTI_FW_CMD, None, &[]).await.rsp(),
        "False"
    );
    assert_eq!(
        cli(
            &f,
            &mut a,
            XCVRD_ACTI_FW_CMD,
            Some("no-such-image.bin"),
            &[]
        )
        .await
        .rsp(),
        "False"
    );
    for (case, f, mut a) in unreachable_ports().await {
        assert_eq!(
            cli(&f, &mut a, XCVRD_ACTI_FW_CMD, Some("null"), &[])
                .await
                .rsp(),
            "False",
            "{case}"
        );
    }
}

/// test_handle_config_firmware_roll_cmd_arg_tbl_notification_with_instance,
/// _no_port, _no_instance, _no_port_and_instance.
#[tokio::test]
async fn rollback_firmware() {
    let path = firmware_file("roll.bin");
    let f = Fixture::new();
    let mut a = ready(&f).await;
    f.cable(|c| {
        c.script("rollback_firmware", value(PyVal::Int(0)));
    });
    assert_eq!(
        cli(&f, &mut a, XCVRD_ROLL_FW_CMD, Some("roll.bin"), &[])
            .await
            .rsp(),
        "0"
    );
    assert_eq!(
        cli(&f, &mut a, XCVRD_ROLL_FW_CMD, Some("null"), &[])
            .await
            .rsp(),
        "0"
    );
    assert_eq!(
        calls(&f),
        vec![
            ("rollback_firmware".to_string(), vec![PyArg::Str(path)]),
            ("rollback_firmware".to_string(), vec![PyArg::None]),
        ]
    );
    f.cable(|c| {
        c.script("rollback_firmware", raise("OSError('I2C')"));
    });
    assert_eq!(
        cli(&f, &mut a, XCVRD_ROLL_FW_CMD, Some("null"), &[])
            .await
            .rsp(),
        "-1"
    );
    assert_eq!(
        cli(&f, &mut a, XCVRD_ROLL_FW_CMD, None, &[]).await.rsp(),
        "False"
    );
    for (case, f, mut a) in unreachable_ports().await {
        assert_eq!(
            cli(&f, &mut a, XCVRD_ROLL_FW_CMD, Some("null"), &[])
                .await
                .rsp(),
            "False",
            "{case}"
        );
    }
}

// --- prbs, reset, anlt, fec, loopback ---------------------------------------------------

/// test_handle_config_prbs_cmd_arg_tbl_notification_with_instance_enable,
/// _disable, _reset, _anlt_enable, _anlt_disable, _fec_enable, _fec_disable:
/// each mode calls its driver method with the arguments' integers; the
/// direction defaults to the driver's PRBS_DIRECTION_BOTH.
#[tokio::test]
async fn config_prbs_modes() {
    let cases: [(&str, &[(&str, &str)], &str, Vec<PyArg>); 8] = [
        (
            "enable",
            &[("target", "1"), ("mode_value", "3"), ("lane_mask", "15")],
            "enable_prbs_mode",
            ints(&[1, 3, 15, 5]),
        ),
        (
            "enable",
            &[
                ("target", "1"),
                ("mode_value", "3"),
                ("lane_mask", "15"),
                ("direction", "2"),
            ],
            "enable_prbs_mode",
            ints(&[1, 3, 15, 2]),
        ),
        (
            "disable",
            &[("target", "2")],
            "disable_prbs_mode",
            ints(&[2, 5]),
        ),
        (
            "disable",
            &[("target", "2"), ("direction", "1")],
            "disable_prbs_mode",
            ints(&[2, 1]),
        ),
        ("reset", &[("target", "0")], "reset", ints(&[0])),
        (
            "anlt",
            &[("target", "1"), ("mode", "1")],
            "set_anlt",
            ints(&[1, 1]),
        ),
        (
            "anlt",
            &[("target", "1"), ("mode", "0")],
            "set_anlt",
            ints(&[0, 1]),
        ),
        (
            "fec",
            &[("target", "2"), ("mode", "1")],
            "set_fec_mode",
            ints(&[1, 2]),
        ),
    ];
    for (mode, args, method, want) in cases {
        let f = Fixture::new();
        let mut a = ready(&f).await;
        f.cable(|c| {
            c.attrs.insert("PRBS_DIRECTION_BOTH".into(), PyVal::Int(5));
            c.script(method, value(PyVal::Bool(true)));
        });
        let r = cli(&f, &mut a, XCVRD_CONFIG_PRBS_CMD, Some(mode), args).await;
        assert_eq!(r.rsp(), "True", "{mode} {args:?}");
        assert_eq!(
            calls(&f),
            vec![(method.to_string(), want)],
            "{mode} {args:?}"
        );
        assert!(r.deleted);
    }
}

/// _no_port, _no_instance, _else_value, and arguments that are missing or
/// not integers: False without calling the cable; -1 if the driver raises.
#[tokio::test]
async fn config_prbs_failures() {
    for (case, f, mut a) in unreachable_ports().await {
        let r = cli(
            &f,
            &mut a,
            XCVRD_CONFIG_PRBS_CMD,
            Some("reset"),
            &[("target", "0")],
        )
        .await;
        assert_eq!(r.rsp(), "False", "{case}");
    }
    let f = Fixture::new();
    let mut a = ready(&f).await;
    let bad: [(Option<&str>, &[(&str, &str)]); 8] = [
        (None, &[("target", "0")]),
        (Some("reset"), &[]),
        (Some("reset"), &[("target", "x")]),
        (Some("bogus"), &[("target", "0")]),
        (Some("enable"), &[("target", "1"), ("mode_value", "3")]),
        (Some("enable"), &[("target", "1"), ("lane_mask", "15")]),
        (Some("anlt"), &[("target", "1")]),
        (Some("fec"), &[("target", "1")]),
    ];
    for (mode, args) in bad {
        assert_eq!(
            cli(&f, &mut a, XCVRD_CONFIG_PRBS_CMD, mode, args)
                .await
                .rsp(),
            "False",
            "{mode:?} {args:?}"
        );
    }
    assert!(calls(&f)
        .iter()
        .all(|c| c.0 != "reset" && c.0 != "enable_prbs_mode"));
    f.cable(|c| {
        c.script("reset", raise("OSError('I2C')"));
    });
    assert_eq!(
        cli(
            &f,
            &mut a,
            XCVRD_CONFIG_PRBS_CMD,
            Some("reset"),
            &[("target", "0")]
        )
        .await
        .rsp(),
        "-1"
    );
}

/// test_handle_config_loop_cmd_arg_tbl_notification_enable, _disable,
/// _else_value, _no_port, _no_instance: the mode value defaults to the
/// driver's LOOPBACK_MODE_NEAR_END; the lane mask is required.
#[tokio::test]
async fn config_loopback() {
    let cases: [(&str, &[(&str, &str)], &str, Vec<PyArg>); 3] = [
        (
            "enable",
            &[("target", "1"), ("lane_mask", "3")],
            "enable_loopback_mode",
            ints(&[1, 6, 3]),
        ),
        (
            "enable",
            &[("target", "1"), ("lane_mask", "3"), ("mode_value", "2")],
            "enable_loopback_mode",
            ints(&[1, 2, 3]),
        ),
        (
            "disable",
            &[("target", "2")],
            "disable_loopback_mode",
            ints(&[2]),
        ),
    ];
    for (mode, args, method, want) in cases {
        let f = Fixture::new();
        let mut a = ready(&f).await;
        f.cable(|c| {
            c.attrs
                .insert("LOOPBACK_MODE_NEAR_END".into(), PyVal::Int(6));
            c.script(method, value(PyVal::Bool(true)));
        });
        assert_eq!(
            cli(&f, &mut a, XCVRD_CONFIG_LOOP_CMD, Some(mode), args)
                .await
                .rsp(),
            "True",
            "{mode} {args:?}"
        );
        assert_eq!(calls(&f), vec![(method.to_string(), want)]);
    }
    let f = Fixture::new();
    let mut a = ready(&f).await;
    for (mode, args) in [
        (None, &[("target", "1")][..]),
        (Some("enable"), &[("target", "1")][..]),
        (Some("bogus"), &[("target", "1")][..]),
        (Some("disable"), &[][..]),
    ] {
        assert_eq!(
            cli(&f, &mut a, XCVRD_CONFIG_LOOP_CMD, mode, args)
                .await
                .rsp(),
            "False",
            "{mode:?} {args:?}"
        );
    }
    f.cable(|c| {
        c.script("disable_loopback_mode", raise("OSError('I2C')"));
    });
    assert_eq!(
        cli(
            &f,
            &mut a,
            XCVRD_CONFIG_LOOP_CMD,
            Some("disable"),
            &[("target", "1")]
        )
        .await
        .rsp(),
        "-1"
    );
    for (case, f, mut a) in unreachable_ports().await {
        let r = cli(
            &f,
            &mut a,
            XCVRD_CONFIG_LOOP_CMD,
            Some("disable"),
            &[("target", "1")],
        )
        .await;
        assert_eq!(r.rsp(), "False", "{case}");
    }
}

// --- event log, fec/anlt/speed, ber family -------------------------------------------------

/// test_handle_show_event_cmd_arg_tbl_notification_get_log, _get_actual_log,
/// _get_no_log, _else_value, _no_port, _no_instance: one result field per
/// event, by index.
#[tokio::test]
async fn show_event_log() {
    let f = Fixture::new();
    let mut a = ready(&f).await;
    let log = PyVal::list(vec![PyVal::str_("boot"), PyVal::str_("toggle")]);
    f.cable(|c| {
        c.script("get_event_log", value(log.clone()));
    });
    let r = cli(&f, &mut a, XCVRD_EVENT_LOG_CMD, Some("probe"), &[]).await;
    assert_eq!(
        (r.rsp(), r.res.clone()),
        ("True", fm(&[("0", "boot"), ("1", "toggle")]))
    );
    for (reply, rsp) in [
        (value(PyVal::list(vec![])), "True"),
        (value(PyVal::None), "True"),
        (raise("OSError('I2C')"), "-1"),
    ] {
        f.cable(|c| {
            c.script("get_event_log", reply.clone());
        });
        let r = cli(&f, &mut a, XCVRD_EVENT_LOG_CMD, Some("probe"), &[]).await;
        assert_eq!((r.rsp(), r.res.is_empty()), (rsp, true), "{reply:?}");
    }
    assert_eq!(
        cli(&f, &mut a, XCVRD_EVENT_LOG_CMD, None, &[]).await.rsp(),
        "False"
    );
    for (case, f, mut a) in unreachable_ports().await {
        assert_eq!(
            cli(&f, &mut a, XCVRD_EVENT_LOG_CMD, Some("probe"), &[])
                .await
                .rsp(),
            "False",
            "{case}"
        );
    }
}

/// test_handle_get_fec_cmd_arg_tbl_notification (get_fec): FEC mode and
/// AN/LT of the three sides and the speed, all or none; _no_port,
/// _no_instance, _no_status.
#[tokio::test]
async fn show_fec_anlt_speed() {
    let f = Fixture::new();
    let mut a = ready(&f).await;
    f.cable(|c| {
        c.script("get_fec_mode", value(PyVal::Int(1)));
        c.script("get_anlt", value(PyVal::Bool(false)));
        c.script("get_speed", value(PyVal::Int(100000)));
    });
    let r = cli(&f, &mut a, XCVRD_GET_FEC_CMD, Some("probe"), &[]).await;
    assert_eq!(r.rsp(), "True");
    let want = fm(&[
        ("fec_nic", "1"),
        ("fec_tor_a", "1"),
        ("fec_tor_b", "1"),
        ("speed", "100000"),
        ("anlt_nic", "False"),
        ("anlt_tor_a", "False"),
        ("anlt_tor_b", "False"),
    ]);
    assert_eq!(r.res, want);
    let targets: Vec<Vec<PyArg>> = calls(&f)
        .into_iter()
        .filter(|c| c.0 == "get_fec_mode")
        .map(|c| c.1)
        .collect();
    assert_eq!(targets, vec![ints(&[0]), ints(&[1]), ints(&[2])]);

    f.cable(|c| {
        c.script("get_speed", raise("OSError('I2C')"));
    });
    let r = cli(&f, &mut a, XCVRD_GET_FEC_CMD, Some("probe"), &[]).await;
    assert_eq!((r.rsp(), r.res.is_empty()), ("-1", true));
    assert_eq!(
        cli(&f, &mut a, XCVRD_GET_FEC_CMD, None, &[]).await.rsp(),
        "False"
    );
    for (case, f, mut a) in unreachable_ports().await {
        assert_eq!(
            cli(&f, &mut a, XCVRD_GET_FEC_CMD, Some("probe"), &[])
                .await
                .rsp(),
            "False",
            "{case}"
        );
    }
}

/// test_handle_get_fec_cmd_arg_tbl_notification_get_ber*, _get_eye*,
/// _get_fec_stats*, _get_pcs_stats*, _get_alive_status*, _get_cable_health,
/// _get_health_check*, _get_reset_cause*, _get_operation_time*,
/// _get_debug_dump*, _get_queue_info*, _get_result: each mode's driver
/// method and the shape of its result row.
#[tokio::test]
async fn show_ber_family() {
    let list = PyVal::list(vec![PyVal::Int(7), PyVal::float(1e-9)]);
    let dict = PyVal::dict(vec![
        (PyVal::str_("a"), PyVal::Int(1)),
        (PyVal::str_("b"), PyVal::str_("x")),
    ]);
    let cases: [(
        &str,
        &[(&str, &str)],
        &str,
        Vec<PyArg>,
        PyVal,
        HashMap<String, String>,
    ); 11] = [
        (
            "ber",
            &[("target", "1")],
            "get_ber_info",
            ints(&[1]),
            list.clone(),
            fm(&[("0", "7"), ("1", "1e-09")]),
        ),
        (
            "eye",
            &[("target", "0")],
            "get_eye_heights",
            ints(&[0]),
            list.clone(),
            fm(&[("0", "7"), ("1", "1e-09")]),
        ),
        (
            "fec_stats",
            &[("target", "2")],
            "get_fec_stats",
            ints(&[2]),
            dict.clone(),
            fm(&[("a", "1"), ("b", "x")]),
        ),
        (
            "pcs_stats",
            &[("target", "2")],
            "get_pcs_stats",
            ints(&[2]),
            dict.clone(),
            fm(&[("a", "1"), ("b", "x")]),
        ),
        (
            "cable_alive",
            &[],
            "get_alive_status",
            vec![],
            PyVal::Bool(true),
            fm(&[("cable_alive", "True")]),
        ),
        (
            "health_check",
            &[],
            "health_check",
            vec![],
            PyVal::Bool(true),
            fm(&[("health_check", "True")]),
        ),
        (
            "reset_cause",
            &[],
            "reset_cause",
            vec![],
            PyVal::Int(2),
            fm(&[("reset_cause", "2")]),
        ),
        (
            "operation_time",
            &[],
            "operation_time",
            vec![],
            PyVal::Int(3600),
            fm(&[("operation_time", "3600")]),
        ),
        (
            "debug_dump",
            &[("option", "all")],
            "debug_dump_registers",
            vec![PyArg::Str("all".into())],
            dict.clone(),
            fm(&[("a", "1"), ("b", "x")]),
        ),
        (
            "debug_dump",
            &[],
            "debug_dump_registers",
            vec![PyArg::None],
            dict.clone(),
            fm(&[("a", "1"), ("b", "x")]),
        ),
        (
            "queue_info",
            &[],
            "queue_info",
            vec![],
            dict.clone(),
            fm(&[("a", "1"), ("b", "x")]),
        ),
    ];
    for (mode, args, method, want_args, reply, want_res) in cases {
        let f = Fixture::new();
        let mut a = ready(&f).await;
        f.cable(|c| {
            c.script(method, value(reply.clone()));
        });
        let r = cli(&f, &mut a, XCVRD_GET_BER_CMD, Some(mode), args).await;
        assert_eq!((r.rsp(), &r.res), ("True", &want_res), "{mode}");
        assert_eq!(calls(&f), vec![(method.to_string(), want_args)], "{mode}");
        // The driver raises: -1 and no result row.
        f.cable(|c| {
            c.script(method, raise("OSError('I2C')"));
        });
        let r = cli(&f, &mut a, XCVRD_GET_BER_CMD, Some(mode), args).await;
        assert_eq!((r.rsp(), r.res.is_empty()), ("-1", true), "{mode}");
    }
}

/// _get_ber_no_target, _get_eye_with_no_target, _get_fec_stats_with_no_target,
/// _get_pcs_stats_with_no_target, _no_port, _no_instance, and a result of
/// the wrong type (no result row).
#[tokio::test]
async fn show_ber_failures() {
    let f = Fixture::new();
    let mut a = ready(&f).await;
    for mode in ["ber", "eye", "fec_stats", "pcs_stats"] {
        assert_eq!(
            cli(&f, &mut a, XCVRD_GET_BER_CMD, Some(mode), &[])
                .await
                .rsp(),
            "False",
            "{mode}"
        );
    }
    assert_eq!(
        cli(
            &f,
            &mut a,
            XCVRD_GET_BER_CMD,
            Some("ber"),
            &[("target", "x")]
        )
        .await
        .rsp(),
        "False"
    );
    assert_eq!(
        cli(&f, &mut a, XCVRD_GET_BER_CMD, Some("bogus"), &[])
            .await
            .rsp(),
        "False"
    );
    assert_eq!(
        cli(&f, &mut a, XCVRD_GET_BER_CMD, None, &[]).await.rsp(),
        "False"
    );
    assert!(calls(&f).is_empty());
    f.cable(|c| {
        c.script("get_ber_info", value(PyVal::Int(3)));
    });
    let r = cli(
        &f,
        &mut a,
        XCVRD_GET_BER_CMD,
        Some("ber"),
        &[("target", "1")],
    )
    .await;
    assert_eq!((r.rsp(), r.res.is_empty()), ("True", true));
    for (case, f, mut a) in unreachable_ports().await {
        let r = cli(
            &f,
            &mut a,
            XCVRD_GET_BER_CMD,
            Some("ber"),
            &[("target", "1")],
        )
        .await;
        assert_eq!(r.rsp(), "False", "{case}");
    }
}

// --- Routing --------------------------------------------------------------------------

/// test_ycable_helper_cli_worker*: a command for a port without a mux cable
/// (only the CLI asks about it) gets the answer for a port without a Y
/// cable, or without a cable if none is plugged in (TRANSCEIVER_INFO); an
/// event of another table is ignored.
#[tokio::test]
async fn commands_for_other_ports() {
    let f = Fixture::new();
    let mut a = PortActor::new(TEST_PORT, Some(PHYSICAL), None, Arc::clone(&f.shared));
    assert_eq!(
        cli(&f, &mut a, XCVRD_SHOW_HWMODE_SWMODE_CMD, Some("probe"), &[])
            .await
            .rsp(),
        "cable not present"
    );
    f.db.put(
        DbId::State,
        TRANSCEIVER_INFO,
        TEST_PORT,
        &[("type", "QSFP28 or later")],
    );
    assert_eq!(
        cli(&f, &mut a, XCVRD_SHOW_HWMODE_SWMODE_CMD, Some("probe"), &[])
            .await
            .rsp(),
        "not Y-Cable port"
    );
    assert_eq!(
        cli(
            &f,
            &mut a,
            XCVRD_CONFIG_PRBS_CMD,
            Some("reset"),
            &[("target", "0")]
        )
        .await
        .rsp(),
        "False"
    );
    let mut a = ready(&f).await;
    a.handle(Msg::Cli("XCVRD_NO_SUCH_CMD".into(), fm(&[("x", "1")])))
        .await;
    assert!(calls(&f).is_empty());
}
