"""`show/config muxcable` commands, through the XCVRD_* tables (HLD 3.4.9,
3.6.5)."""

import time

import pytest

from harness import expect
from harness.dut import FIRMWARE_DIR
from harness.fake_hw import FIRMWARE_DOWNLOAD_STATUS_FAILED, FIRMWARE_DOWNLOAD_STATUS_INPROGRESS, TARGET_TOR_B
from harness.topology import Topology
from harness.util import stays_true, wait_until

SOC = "127.0.0.2"


@pytest.fixture
def cable(dut):
    """Ethernet0 is the cable under test; Ethernet4 keeps ycabled serving."""
    dut.configure(Topology().active_standby("Ethernet0", "Ethernet4"))
    return dut.hw.port(1)


def no_driver(dut):
    dut.hw.port(1).transceiver_info = {"manufacturer": "Nobody", "model": "X1"}


def calls(dut, mark, method=None):
    return [(c.method, c.args) for c in dut.hw.calls(mark, port=1, method=method)]


# show muxcable hwmode muxdirection --------------------------------------------

@pytest.mark.parametrize("read_side,mux,state", [(1, 1, "active"), (1, 2, "standby"), (2, 2, "active"),
                                                 (2, 1, "standby")])
def test_show_mux_direction(dut, cable, read_side, mux, state):
    cable.read_side = read_side
    dut.start()
    cable.mux_direction = mux
    r = dut.cli("XCVRD_SHOW_HWMODE_DIR_CMD", "probe", "Ethernet0")
    assert r.rsp == {"state": state} and r.res == {"presence": "True"} and r.cmd_deleted


@pytest.mark.parametrize("method", ["get_read_side", "get_mux_direction"])
def test_show_mux_direction_unreadable(dut, cable, method):
    dut.start()
    cable.script(method, raises="OSError: I2C failure")
    assert dut.cli("XCVRD_SHOW_HWMODE_DIR_CMD", "probe", "Ethernet0").rsp == {"state": "unknown"}


@pytest.mark.parametrize("value", [0, 7])
def test_show_mux_direction_invalid(dut, cable, value):
    dut.start()
    cable.script("get_mux_direction", value=value)
    assert dut.cli("XCVRD_SHOW_HWMODE_DIR_CMD", "probe", "Ethernet0").rsp == {"state": "unknown"}


def test_show_mux_direction_without_driver(dut, cable):
    no_driver(dut)
    dut.start()
    r = dut.cli("XCVRD_SHOW_HWMODE_DIR_CMD", "probe", "Ethernet0")
    assert r.rsp == {"state": "not Y-Cable port"} and r.res == {"presence": "True"}


def test_show_mux_direction_cable_gone(dut, cable):
    dut.start()
    cable.present = False
    r = dut.cli("XCVRD_SHOW_HWMODE_DIR_CMD", "probe", "Ethernet0")
    assert r.rsp == {"state": "unknown"} and r.res == {"presence": "False"}


def test_show_mux_direction_not_mux_port(dut, cable):
    dut.start()
    assert dut.cli("XCVRD_SHOW_HWMODE_DIR_CMD", "probe", "Ethernet8").rsp == {"state": "unknown"}


def test_show_mux_direction_active_active(dut):
    """Active-active: the state ycabled last published, not the SoC's."""
    dut.configure(Topology().active_active("Ethernet0", soc_ipv4=SOC + "/32"))
    soc = dut.soc(SOC)
    dut.start()
    soc.admin = [False, False]
    mark = soc.mark()
    r = dut.cli("XCVRD_SHOW_HWMODE_DIR_CMD", "probe", "Ethernet0")
    assert r.rsp == {"state": "active"} and r.res == {"presence": "True"}
    assert not soc.rpcs(since=mark)


def test_show_mux_direction_active_active_without_state(dut):
    """Without a published state, the SoC is asked."""
    dut.configure(Topology().active_active("Ethernet0", soc_ipv4=SOC + "/32"))
    soc = dut.soc(SOC)
    dut.start()
    soc.admin = [False, True]
    dut.state.r.hdel("HW_MUX_CABLE_TABLE|Ethernet0", "state")
    mark = soc.mark()
    assert dut.cli("XCVRD_SHOW_HWMODE_DIR_CMD", "probe", "Ethernet0").rsp == {"state": "standby"}
    assert [r.name for r in soc.rpcs(since=mark)] == ["QueryAdminForwardingPortState"]


def test_show_mux_direction_active_active_without_soc_address(dut):
    dut.configure(Topology().active_standby("Ethernet0").active_active("Ethernet4"))
    dut.start()
    r = dut.cli("XCVRD_SHOW_HWMODE_DIR_CMD", "probe", "Ethernet4")
    assert r.rsp == {"state": "unknown"} and r.res == {"presence": "True"}


# config muxcable hwmode state ---------------------------------------------------

@pytest.mark.parametrize("read_side,state,method", [
    (1, "active", "toggle_mux_to_tor_a"), (1, "standby", "toggle_mux_to_tor_b"),
    (2, "active", "toggle_mux_to_tor_b"), (2, "standby", "toggle_mux_to_tor_a")])
def test_config_mux_state(dut, cable, read_side, state, method):
    cable.read_side = read_side
    dut.start()
    mark, db_mark = dut.hw.mark(), dut.monitor.mark()
    r = dut.cli("XCVRD_CONFIG_HWMODE_DIR_CMD", state, "Ethernet0")
    assert r.rsp == {"result": "True"}
    assert [m for m, _ in calls(dut, mark)] == ["get_read_side", method]
    # The CLI toggles the cable only; HW_MUX_CABLE_TABLE is left alone.
    assert not dut.writes(since=db_mark, table="HW_MUX_CABLE_TABLE")


@pytest.mark.parametrize("read_side,state,method", [
    (1, "active", "toggle_mux_to_tor_a"), (1, "standby", "toggle_mux_to_tor_b"),
    (2, "active", "toggle_mux_to_tor_b"), (2, "standby", "toggle_mux_to_tor_a")])
@pytest.mark.parametrize("script,result", [({"value": False}, "False"), ({"raises": "OSError"}, "-1")])
def test_config_mux_state_toggle_fails(dut, cable, read_side, state, method, script, result):
    cable.read_side = read_side
    dut.start()
    cable.script(method, **script)
    assert dut.cli("XCVRD_CONFIG_HWMODE_DIR_CMD", state, "Ethernet0").rsp == {"result": result}


@pytest.mark.parametrize("case", ["read-side-fails", "read-side-nic", "no-driver", "bad-state"])
def test_config_mux_state_errors(dut, cable, case):
    if case == "no-driver":
        no_driver(dut)
    dut.start()
    if case == "read-side-fails":
        cable.script("get_read_side", raises="OSError")
    if case == "read-side-nic":
        cable.script("get_read_side", value=0)
    mark = dut.hw.mark()
    state = "sideways" if case == "bad-state" else "active"
    assert dut.cli("XCVRD_CONFIG_HWMODE_DIR_CMD", state, "Ethernet0").rsp == {"result": "False"}
    assert not [c for c in calls(dut, mark) if c[0].startswith("toggle")]


# show/config muxcable hwmode switchmode ----------------------------------------

@pytest.mark.parametrize("mode,state", [(1, "auto"), (0, "manual"), (5, "unknown"), (-1, "unknown")])
def test_show_switch_mode(dut, cable, mode, state):
    cable.switching_mode = mode
    dut.start()
    assert dut.cli("XCVRD_SHOW_HWMODE_SWMODE_CMD", "probe", "Ethernet0").rsp == {"state": state}


@pytest.mark.parametrize("case,state", [("no-driver", "not Y-Cable port"), ("cable-gone", "cable not present"),
                                        ("driver-fails", "unknown")])
def test_show_switch_mode_errors(dut, cable, case, state):
    if case == "no-driver":
        no_driver(dut)
    dut.start()
    if case == "cable-gone":
        cable.present = False
    if case == "driver-fails":
        cable.script("get_switching_mode", raises="OSError")
    assert dut.cli("XCVRD_SHOW_HWMODE_SWMODE_CMD", "probe", "Ethernet0").rsp == {"state": state}


@pytest.mark.parametrize("mode,value", [("auto", 1), ("manual", 0)])
def test_config_switch_mode(dut, cable, mode, value):
    dut.start()
    mark = dut.hw.mark()
    assert dut.cli("XCVRD_CONFIG_HWMODE_SWMODE_CMD", mode, "Ethernet0").rsp == {"result": "True"}
    assert calls(dut, mark) == [("set_switching_mode", (value,))]
    assert cable.switching_mode == value


@pytest.mark.parametrize("mode", ["auto", "manual"])
@pytest.mark.parametrize("case", ["driver-fails", "driver-false", "bad-mode", "no-driver"])
def test_config_switch_mode_errors(dut, cable, case, mode):
    if case == "no-driver":
        no_driver(dut)
    dut.start()
    if case == "driver-fails":
        cable.script("set_switching_mode", raises="OSError")
    if case == "driver-false":
        cable.script("set_switching_mode", value=-1)
    mode = "sometimes" if case == "bad-mode" else mode
    assert dut.cli("XCVRD_CONFIG_HWMODE_SWMODE_CMD", mode, "Ethernet0").rsp == {"result": "False"}


# show muxcable firmware version -------------------------------------------------

@pytest.mark.parametrize("read_side", [1, 2])
def test_show_firmware(dut, cable, read_side):
    cable.read_side = read_side
    dut.start()
    db_mark = dut.monitor.mark()
    r = dut.cli("XCVRD_SHOW_FW_CMD", "probe", "Ethernet0")
    assert r.rsp == {"status": "True"}
    assert r.res == expect.firmware_versions(cable)
    # The result rows come before the response, and the command key is
    # deleted last.
    writes = dut.monitor.accesses(db_mark)
    res = max(a.seq for a in writes if a.table == "XCVRD_SHOW_FW_RES")
    rsp = min(a.seq for a in writes if a.table == "XCVRD_SHOW_FW_RSP" and a.kind == "write")
    deleted = min(a.seq for a in writes if a.table == "XCVRD_SHOW_FW_CMD" and a.kind == "delete")
    assert res < rsp < deleted


def _firmware_tor_b_fails(model, args):
    if args[0] == TARGET_TOR_B:
        raise OSError("I2C failure")
    return dict(model.firmware[args[0]])


def test_show_firmware_one_side_fails(dut, cable):
    dut.start()
    cable.script("get_firmware_version", func=_firmware_tor_b_fails)
    r = dut.cli("XCVRD_SHOW_FW_CMD", "probe", "Ethernet0")
    assert r.rsp == {"status": "True"}
    assert {k for k, v in r.res.items() if v == "N/A"} == {"version_peer_active", "version_peer_inactive",
                                                           "version_peer_next"}


@pytest.mark.parametrize("case", ["read-side-fails", "no-driver"])
def test_show_firmware_errors(dut, cable, case):
    if case == "no-driver":
        no_driver(dut)
    dut.start()
    if case == "read-side-fails":
        cable.script("get_read_side", raises="OSError")
    r = dut.cli("XCVRD_SHOW_FW_CMD", "probe", "Ethernet0")
    assert r.rsp == {"status": "False"} and set(r.res.values()) == {"N/A"} and len(r.res) == 9


# config muxcable firmware download/activate/rollback ----------------------------

def test_download_firmware(dut, cable):
    dut.firmware_file("bb-fw.bin")
    dut.start()
    mark = dut.hw.mark()
    t0 = time.monotonic()
    r = dut.cli("XCVRD_DOWN_FW_CMD", "bb-fw.bin", "Ethernet0", timeout=20)
    assert r.rsp == {"status": "0"}
    assert calls(dut, mark, "download_firmware") == [("download_firmware", (FIRMWARE_DIR + "/bb-fw.bin",))]
    # The download runs in its own thread, which waits 5 s after the driver
    # returns.
    assert time.monotonic() - t0 >= 5


def test_download_firmware_in_progress(dut, cable):
    """While a download runs, other commands are served, and `show firmware`
    reports the last known versions without asking the cable."""
    dut.firmware_file("bb-fw.bin")
    dut.start()
    known = {k: v for k, v in dut.state.get("MUX_CABLE_INFO", "Ethernet0").items() if k.startswith("version_")}
    gate = cable.script("download_firmware", block=True)
    mark = dut.cli("XCVRD_DOWN_FW_CMD", "bb-fw.bin", "Ethernet0", wait=False)
    wait_until(lambda: dut.hw.calls(port=1, method="download_firmware"), 5)
    assert cable.download_firmware_status == FIRMWARE_DOWNLOAD_STATUS_INPROGRESS
    hw_mark = dut.hw.mark()
    r = dut.cli("XCVRD_SHOW_FW_CMD", "probe", "Ethernet0")
    assert r.rsp == {"status": "True"} and r.res == known
    assert not calls(dut, hw_mark, "get_firmware_version")
    assert dut.cli("XCVRD_SHOW_HWMODE_SWMODE_CMD", "probe", "Ethernet0").rsp == {"state": "auto"}
    assert not dut.monitor.accesses(mark, kind="write", table="XCVRD_DOWN_FW_RSP")
    gate.set()
    dut.monitor.wait_access(mark, 15, kind="write", table="XCVRD_DOWN_FW_RSP", key="Ethernet0")
    assert dut.state.get("XCVRD_DOWN_FW_RSP", "Ethernet0") == {"status": "0"}


def test_show_firmware_during_download_without_known_versions(dut):
    """No MUX_CABLE_INFO yet for the port (only the first port gets it at
    startup): the versions are N/A while the download runs."""
    dut.configure(Topology().active_standby("Ethernet0", "Ethernet4"))
    dut.firmware_file("bb-fw.bin")
    dut.start()
    assert not dut.state.exists("MUX_CABLE_INFO", "Ethernet4")
    gate = dut.hw.port(2).script("download_firmware", block=True)
    mark = dut.cli("XCVRD_DOWN_FW_CMD", "bb-fw.bin", "Ethernet4", wait=False)
    wait_until(lambda: dut.hw.calls(port=2, method="download_firmware"), 5)
    r = dut.cli("XCVRD_SHOW_FW_CMD", "probe", "Ethernet4")
    assert r.rsp == {"status": "True"} and set(r.res.values()) == {"N/A"}
    gate.set()
    dut.monitor.wait_access(mark, 15, kind="write", table="XCVRD_DOWN_FW_RSP", key="Ethernet4")


def test_show_firmware_after_failed_download(dut, cable):
    dut.start()
    cable.download_firmware_status = FIRMWARE_DOWNLOAD_STATUS_FAILED
    mark = dut.syslog.mark()
    r = dut.cli("XCVRD_SHOW_FW_CMD", "probe", "Ethernet0")
    assert r.rsp == {"status": "True"} and r.res == expect.firmware_versions(cable)
    dut.syslog.wait_for(mark, 3, severity="ERR", contains="Firmware Download API failed in the previous run")


@pytest.mark.parametrize("case,status", [("missing-file", "-1"), ("no-driver", "-1"), ("driver-fails", "-1")])
def test_download_firmware_errors(dut, cable, case, status):
    if case != "missing-file":
        dut.firmware_file("bb-fw.bin")
    if case == "no-driver":
        no_driver(dut)
    if case == "driver-fails":
        cable.script("download_firmware", raises="OSError")
    dut.start()
    assert dut.cli("XCVRD_DOWN_FW_CMD", "bb-fw.bin", "Ethernet0", timeout=10).rsp == {"status": status}


@pytest.mark.parametrize("fwfile,args,expected_args", [
    ("bb-fw.bin", None, (FIRMWARE_DIR + "/bb-fw.bin", True)),
    ("bb-fw.bin", {"hitless": "1"}, (FIRMWARE_DIR + "/bb-fw.bin", False)),
    ("null", None, (None, True)),
], ids=["file", "nonhitless", "null"])
def test_activate_firmware(dut, cable, fwfile, args, expected_args):
    dut.firmware_file("bb-fw.bin")
    dut.start()
    mark = dut.hw.mark()
    r = dut.cli("XCVRD_ACTI_FW_CMD", fwfile, "Ethernet0", args=args, timeout=20)
    assert r.rsp == {"status": "0"}
    assert calls(dut, mark) == [("activate_firmware", expected_args)]
    if args:
        # The argument row is the CLI's to delete.
        assert dut.appl.get("XCVRD_ACTI_FW_CMD_ARG", "Ethernet0") == args


@pytest.mark.parametrize("case,status", [("missing-file", "False"), ("no-driver", "False"), ("driver-fails", "-1")])
def test_activate_firmware_errors(dut, cable, case, status):
    if case != "missing-file":
        dut.firmware_file("bb-fw.bin")
    if case == "no-driver":
        no_driver(dut)
    if case == "driver-fails":
        cable.script("activate_firmware", raises="OSError")
    dut.start()
    assert dut.cli("XCVRD_ACTI_FW_CMD", "bb-fw.bin", "Ethernet0", timeout=10).rsp == {"status": status}


@pytest.mark.parametrize("fwfile,expected", [("bb-fw.bin", FIRMWARE_DIR + "/bb-fw.bin"), ("null", None)])
def test_rollback_firmware(dut, cable, fwfile, expected):
    dut.firmware_file("bb-fw.bin")
    dut.start()
    mark = dut.hw.mark()
    assert dut.cli("XCVRD_ROLL_FW_CMD", fwfile, "Ethernet0").rsp == {"status": "0"}
    assert calls(dut, mark) == [("rollback_firmware", (expected,))]


@pytest.mark.parametrize("case,status", [("missing-file", "False"), ("driver-fails", "-1")])
def test_rollback_firmware_errors(dut, cable, case, status):
    if case != "missing-file":
        dut.firmware_file("bb-fw.bin")
    if case == "driver-fails":
        cable.script("rollback_firmware", raises="OSError")
    dut.start()
    assert dut.cli("XCVRD_ROLL_FW_CMD", "bb-fw.bin", "Ethernet0").rsp == {"status": status}


@pytest.mark.known_issue("3.8-19", "rollback for a port without a driver answers, then kills the CLI thread "
                                    "(no lock for the port)")
def test_rollback_firmware_without_driver(dut, cable):
    dut.firmware_file("bb-fw.bin")
    no_driver(dut)
    dut.start()
    dut.tolerate_python_failure(crash=True)
    assert dut.cli("XCVRD_ROLL_FW_CMD", "null", "Ethernet0").rsp == {"status": "False"}
    # A command sent after it shows whether the CLI thread is still serving.
    mark = dut.cli("XCVRD_SHOW_HWMODE_SWMODE_CMD", "probe", "Ethernet4", wait=False)
    assert dut.monitor.eventually(mark, 5, kind="write", db="STATE_DB", table="XCVRD_SHOW_HWMODE_SWMODE_RSP",
                                  key="Ethernet4", field="state"), "the next command was not answered"
    assert dut.state.get("XCVRD_SHOW_HWMODE_SWMODE_RSP", "Ethernet4") == {"state": "auto"}


# config muxcable prbs/reset/anlt/fec, loopback ----------------------------------

@pytest.mark.parametrize("mode,args,method,margs", [
    ("enable", {"target": "1", "mode_value": "3", "lane_mask": "15", "direction": "2"}, "enable_prbs_mode", (1, 3, 15, 2)),
    ("enable", {"target": "0", "mode_value": "3", "lane_mask": "15"}, "enable_prbs_mode", (0, 3, 15, 0)),
    ("disable", {"target": "2", "direction": "1"}, "disable_prbs_mode", (2, 1)),
    ("disable", {"target": "2"}, "disable_prbs_mode", (2, 0)),
    ("reset", {"target": "1"}, "reset", (1,)),
    ("anlt", {"target": "1", "mode": "1"}, "set_anlt", (1, 1)),
    ("fec", {"target": "0", "mode": "2"}, "set_fec_mode", (2, 0)),
], ids=["enable", "enable-both-directions", "disable", "disable-both-directions", "reset", "anlt", "fec"])
def test_prbs_family(dut, cable, mode, args, method, margs):
    dut.start()
    mark = dut.hw.mark()
    r = dut.cli("XCVRD_CONFIG_PRBS_CMD", mode, "Ethernet0", args=args)
    assert r.rsp == {"status": "True"}
    assert calls(dut, mark) == [(method, margs)]


@pytest.mark.parametrize("mode,args,status", [
    ("enable", {"target": "1", "mode_value": "3"}, "False"),
    ("enable", {"mode_value": "3", "lane_mask": "15"}, "False"),
    ("anlt", {"target": "1"}, "False"),
    ("fec", {"target": "1"}, "False"),
    ("sideways", {"target": "1"}, "False"),
], ids=["no-lane-mask", "no-target", "anlt-no-mode", "fec-no-mode", "unknown-mode"])
def test_prbs_family_bad_arguments(dut, cable, mode, args, status):
    dut.start()
    mark = dut.hw.mark()
    assert dut.cli("XCVRD_CONFIG_PRBS_CMD", mode, "Ethernet0", args=args).rsp == {"status": status}
    assert not calls(dut, mark)


@pytest.mark.parametrize("mode,args,method", [
    ("enable", {"target": "1", "mode_value": "3", "lane_mask": "15"}, "enable_prbs_mode"),
    ("disable", {"target": "1"}, "disable_prbs_mode"),
    ("reset", {"target": "1"}, "reset"),
    ("anlt", {"target": "1", "mode": "1"}, "set_anlt"),
    ("fec", {"target": "1", "mode": "1"}, "set_fec_mode"),
])
@pytest.mark.parametrize("case,status", [("driver-fails", "-1"), ("no-driver", "False")])
def test_prbs_family_errors(dut, cable, case, status, mode, args, method):
    if case == "no-driver":
        no_driver(dut)
    dut.start()
    cable.script(method, raises="OSError")
    assert dut.cli("XCVRD_CONFIG_PRBS_CMD", mode, "Ethernet0", args=args).rsp == {"status": status}


@pytest.mark.parametrize("mode,args,method,margs", [
    ("enable", {"target": "1", "mode_value": "2", "lane_mask": "15"}, "enable_loopback_mode", (1, 2, 15)),
    ("enable", {"target": "1", "lane_mask": "3"}, "enable_loopback_mode", (1, 1, 3)),
    ("disable", {"target": "2"}, "disable_loopback_mode", (2,)),
], ids=["enable", "enable-near-end", "disable"])
def test_loopback(dut, cable, mode, args, method, margs):
    dut.start()
    mark = dut.hw.mark()
    assert dut.cli("XCVRD_CONFIG_LOOP_CMD", mode, "Ethernet0", args=args).rsp == {"status": "True"}
    assert calls(dut, mark) == [(method, margs)]


@pytest.mark.parametrize("case,mode,args,status", [
    ("no-lane-mask", "enable", {"target": "1"}, "False"),
    ("no-target", "enable", {"lane_mask": "3"}, "False"),
    ("driver-fails", "enable", {"target": "1", "lane_mask": "3"}, "-1"),
    ("driver-fails", "disable", {"target": "1"}, "-1"),
    ("no-driver", "enable", {"target": "1", "lane_mask": "3"}, "False"),
])
def test_loopback_errors(dut, cable, case, mode, args, status):
    if case == "no-driver":
        no_driver(dut)
    dut.start()
    cable.script("enable_loopback_mode", raises="OSError")
    cable.script("disable_loopback_mode", raises="OSError")
    assert dut.cli("XCVRD_CONFIG_LOOP_CMD", mode, "Ethernet0", args=args).rsp == {"status": status}


# show muxcable event_log, get_fec_anlt_speed -------------------------------------

def test_event_log(dut, cable):
    dut.start()
    r = dut.cli("XCVRD_EVENT_LOG_CMD", "probe", "Ethernet0")
    assert r.rsp == {"status": "True"}
    assert r.res == {str(i): e for i, e in enumerate(cable.event_log)}


@pytest.mark.parametrize("case,status", [("driver-fails", "-1"), ("no-driver", "False"), ("not-a-list", "True")])
def test_event_log_errors(dut, cable, case, status):
    if case == "no-driver":
        no_driver(dut)
    dut.start()
    if case == "driver-fails":
        cable.script("get_event_log", raises="OSError")
    if case == "not-a-list":
        cable.script("get_event_log", value="busy")
    r = dut.cli("XCVRD_EVENT_LOG_CMD", "probe", "Ethernet0")
    assert r.rsp == {"status": status} and r.res == {}


def test_fec_anlt_speed(dut, cable):
    dut.start()
    r = dut.cli("XCVRD_GET_FEC_CMD", "probe", "Ethernet0")
    assert r.rsp == {"status": "True"}
    assert r.res == {"fec_nic": "1", "fec_tor_a": "1", "fec_tor_b": "2", "speed": "100000",
                     "anlt_nic": "False", "anlt_tor_a": "True", "anlt_tor_b": "False"}


@pytest.mark.parametrize("case,status", [("driver-fails", "-1"), ("no-driver", "False")])
def test_fec_anlt_speed_errors(dut, cable, case, status):
    if case == "no-driver":
        no_driver(dut)
    dut.start()
    cable.script("get_speed", raises="OSError")
    r = dut.cli("XCVRD_GET_FEC_CMD", "probe", "Ethernet0")
    assert r.rsp == {"status": status} and r.res == {}


# show muxcable berinfo/eyeinfo/fecstatistics/pcsstatistics/... ------------------

@pytest.mark.parametrize("mode,args,res", [
    ("ber", {"target": "1"}, {"0": "2e-09", "1": "4e-09"}),
    ("eye", {"target": "2"}, {"0": "621", "1": "622"}),
    ("fec_stats", {"target": "0"}, {"corrected": "10", "uncorrected": "0"}),
    ("pcs_stats", {"target": "1"}, {"errors": "21"}),
    ("cable_alive", None, {"cable_alive": "True"}),
    ("health_check", None, {"health_check": "True"}),
    ("reset_cause", None, {"reset_cause": "2"}),
    ("operation_time", None, {"operation_time": "123456"}),
    ("debug_dump", {"option": "all"}, {"reg_0x10": "0x5a", "reg_0x11": "0xa5"}),
    ("queue_info", None, {"queue_0": "7", "queue_1": "0"}),
])
def test_ber_family(dut, cable, mode, args, res):
    dut.start()
    mark = dut.hw.mark()
    r = dut.cli("XCVRD_GET_BER_CMD", mode, "Ethernet0", args=args)
    assert r.rsp == {"status": "True"}
    assert r.res == res
    if mode == "debug_dump":
        assert calls(dut, mark) == [("debug_dump_registers", ("all",))]


@pytest.mark.parametrize("mode,args,status", [
    ("ber", None, "False"), ("eye", None, "False"), ("fec_stats", None, "False"), ("pcs_stats", None, "False"),
    ("sideways", {"target": "1"}, "False"),
])
def test_ber_family_bad_arguments(dut, cable, mode, args, status):
    dut.start()
    mark = dut.hw.mark()
    r = dut.cli("XCVRD_GET_BER_CMD", mode, "Ethernet0", args=args)
    assert r.rsp == {"status": status} and r.res == {}
    assert not calls(dut, mark)


@pytest.mark.parametrize("mode,args,method", [
    ("ber", {"target": "1"}, "get_ber_info"), ("eye", {"target": "1"}, "get_eye_heights"),
    ("fec_stats", {"target": "1"}, "get_fec_stats"), ("pcs_stats", {"target": "1"}, "get_pcs_stats"),
    ("cable_alive", None, "get_alive_status"), ("health_check", None, "health_check"),
    ("reset_cause", None, "reset_cause"), ("operation_time", None, "operation_time"),
    ("debug_dump", {"option": "all"}, "debug_dump_registers"), ("queue_info", None, "queue_info"),
])
@pytest.mark.parametrize("case,status", [("driver-fails", "-1"), ("no-driver", "False")])
def test_ber_family_errors(dut, cable, case, status, mode, args, method):
    if case == "no-driver":
        no_driver(dut)
    dut.start()
    cable.script(method, raises="OSError")
    r = dut.cli("XCVRD_GET_BER_CMD", mode, "Ethernet0", args=args)
    assert r.rsp == {"status": status} and r.res == {}


@pytest.mark.parametrize("cmd,value,args,rsp", [
    ("XCVRD_SHOW_HWMODE_SWMODE_CMD", "probe", None, {"state": "cable not present"}),
    ("XCVRD_CONFIG_HWMODE_DIR_CMD", "active", None, {"result": "False"}),
    ("XCVRD_CONFIG_HWMODE_SWMODE_CMD", "auto", None, {"result": "False"}),
    ("XCVRD_SHOW_FW_CMD", "probe", None, {"status": "False"}),
    ("XCVRD_DOWN_FW_CMD", "bb-fw.bin", None, {"status": "-1"}),
    ("XCVRD_ACTI_FW_CMD", "bb-fw.bin", None, {"status": "False"}),
    ("XCVRD_ROLL_FW_CMD", "bb-fw.bin", None, {"status": "False"}),
    ("XCVRD_CONFIG_PRBS_CMD", "reset", {"target": "1"}, {"status": "False"}),
    ("XCVRD_CONFIG_LOOP_CMD", "disable", {"target": "1"}, {"status": "False"}),
    ("XCVRD_EVENT_LOG_CMD", "probe", None, {"status": "False"}),
    ("XCVRD_GET_FEC_CMD", "probe", None, {"status": "False"}),
    ("XCVRD_GET_BER_CMD", "health_check", None, {"status": "False"}),
])
def test_command_cable_gone(dut, cable, cmd, value, args, rsp):
    """The cable is gone but xcvrd has not removed it yet."""
    dut.firmware_file("bb-fw.bin")
    dut.start()
    cable.present = False
    mark = dut.hw.mark()
    assert dut.cli(cmd, value, "Ethernet0", args=args).rsp == rsp
    assert not dut.hw.calls(mark, port=1)


# Malformed commands --------------------------------------------------------------

@pytest.mark.parametrize("cmd,rsp_table,rsp", [
    ("XCVRD_SHOW_FW_CMD", "XCVRD_SHOW_FW_RSP", {"status": "False"}),
    ("XCVRD_DOWN_FW_CMD", "XCVRD_DOWN_FW_RSP", {"status": "-1"}),
    ("XCVRD_ACTI_FW_CMD", "XCVRD_ACTI_FW_RSP", {"status": "False"}),
    ("XCVRD_ROLL_FW_CMD", "XCVRD_ROLL_FW_RSP", {"status": "False"}),
    ("XCVRD_CONFIG_HWMODE_DIR_CMD", "XCVRD_CONFIG_HWMODE_DIR_RSP", {"result": "False"}),
    ("XCVRD_SHOW_HWMODE_SWMODE_CMD", "XCVRD_SHOW_HWMODE_SWMODE_RSP", {"state": "unknown"}),
    ("XCVRD_CONFIG_HWMODE_SWMODE_CMD", "XCVRD_CONFIG_HWMODE_SWMODE_RSP", {"result": "False"}),
    ("XCVRD_CONFIG_PRBS_CMD", "XCVRD_CONFIG_PRBS_RSP", {"status": "False"}),
    ("XCVRD_CONFIG_LOOP_CMD", "XCVRD_CONFIG_LOOP_RSP", {"status": "False"}),
    ("XCVRD_EVENT_LOG_CMD", "XCVRD_EVENT_LOG_RSP", {"status": "False"}),
    ("XCVRD_GET_FEC_CMD", "XCVRD_GET_FEC_RSP", {"status": "False"}),
    ("XCVRD_GET_BER_CMD", "XCVRD_GET_BER_RSP", {"status": "False"}),
])
def test_command_with_wrong_field(dut, cable, cmd, rsp_table, rsp):
    dut.start()
    mark = dut.monitor.mark()
    hw_mark = dut.hw.mark()
    dut.appl.set(cmd, "Ethernet0", {"bogus": "probe"})
    dut.monitor.wait_access(mark, 5, kind="write", table=rsp_table, key="Ethernet0")
    assert dut.state.get(rsp_table, "Ethernet0") == rsp
    assert not calls(dut, hw_mark)


def test_show_mux_direction_wrong_field_is_ignored(dut, cable):
    dut.start()
    mark = dut.monitor.mark()
    dut.appl.set("XCVRD_SHOW_HWMODE_DIR_CMD", "Ethernet0", {"bogus": "probe"})
    assert stays_true(lambda: not dut.writes(since=mark, table="XCVRD_SHOW_HWMODE_DIR_RSP"), 1.5)
    # The CLI thread is still serving.
    assert dut.cli("XCVRD_SHOW_HWMODE_SWMODE_CMD", "probe", "Ethernet0").rsp == {"state": "auto"}


def test_commands_for_several_ports(dut):
    """Commands for different ports in flight together are all answered."""
    dut.configure(Topology().active_standby("Ethernet0", "Ethernet4", "Ethernet8"))
    dut.hw.port(2).switching_mode = 0
    dut.start()
    marks = [dut.cli("XCVRD_SHOW_HWMODE_SWMODE_CMD", "probe", p, wait=False)
             for p in ("Ethernet0", "Ethernet4", "Ethernet8")]
    for port in ("Ethernet0", "Ethernet4", "Ethernet8"):
        dut.monitor.wait_access(marks[0], 5, kind="write", table="XCVRD_SHOW_HWMODE_SWMODE_RSP", key=port)
    assert dut.state.field("XCVRD_SHOW_HWMODE_SWMODE_RSP", "Ethernet4", "state") == "manual"
