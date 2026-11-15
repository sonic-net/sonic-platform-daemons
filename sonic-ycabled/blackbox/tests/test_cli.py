"""`show/config muxcable` commands, through the XCVRD_* tables (HLD 3.3.9,
3.5.5).

Each test sends its command as sonic-utilities does (dut.cli): it clears the
port's entries in the command's tables, writes the argument row
(XCVRD_*_CMD_ARG) and the command (XCVRD_*_CMD) to APPL_DB, and waits for
ycabled's response in STATE_DB XCVRD_*_RSP and for ycabled to delete the
command key. Some commands also answer with a result row, STATE_DB XCVRD_*_RES.
"""

import time

import pytest

from harness import expect
from harness.dut import FIRMWARE_DIR
from harness.fake_hw import FIRMWARE_DOWNLOAD_STATUS_FAILED, FIRMWARE_DOWNLOAD_STATUS_INPROGRESS, TARGET_TOR_B
from harness.topology import Topology, soc_ip
from harness.util import stays_true, wait_until

SOC = soc_ip("Ethernet0")


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
    """show muxcable hwmode muxdirection reads the read side and the mux
    direction from the cable, and answers active when the mux points at this
    ToR's read side, standby otherwise, with presence True in the result. The
    command key is deleted once answered.

    Cases: read side A or B, with the mux on the same side or the other.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. Ethernet0's cable has the given read side.
    2. Start ycabled, then point the mux of Ethernet0's cable at the given
       side.
    3. Send XCVRD_SHOW_HWMODE_DIR_CMD probe for Ethernet0.
    4. Expect the response {state: active or standby}, the result {presence:
       True}, and the command key deleted.
    """
    cable.read_side = read_side
    dut.start()
    cable.mux_direction = mux
    r = dut.cli("XCVRD_SHOW_HWMODE_DIR_CMD", "probe", "Ethernet0")
    assert r.rsp == {"state": state} and r.res == {"presence": "True"} and r.cmd_deleted


@pytest.mark.parametrize("method", ["get_read_side", "get_mux_direction"])
def test_show_mux_direction_unreadable(dut, cable, method):
    """When the cable's read side or mux direction cannot be read, the mux
    direction is answered unknown.

    Cases: get_read_side() or get_mux_direction() raises OSError.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving.
    2. Start ycabled, then script the case's method of Ethernet0's cable to
       raise OSError.
    3. Send XCVRD_SHOW_HWMODE_DIR_CMD probe for Ethernet0, and expect the
       response {state: unknown}.
    """
    dut.start()
    cable.script(method, raises="OSError: I2C failure")
    assert dut.cli("XCVRD_SHOW_HWMODE_DIR_CMD", "probe", "Ethernet0").rsp == {"state": "unknown"}


@pytest.mark.parametrize("value", [0, 7])
def test_show_mux_direction_invalid(dut, cable, value):
    """A mux direction that points at neither ToR is answered unknown.

    Cases: get_mux_direction() returns 0, the NIC side, or 7.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving.
    2. Start ycabled, then script get_mux_direction() of Ethernet0's cable to
       return the value.
    3. Send XCVRD_SHOW_HWMODE_DIR_CMD probe for Ethernet0, and expect the
       response {state: unknown}.
    """
    dut.start()
    cable.script("get_mux_direction", value=value)
    assert dut.cli("XCVRD_SHOW_HWMODE_DIR_CMD", "probe", "Ethernet0").rsp == {"state": "unknown"}


def test_show_mux_direction_without_driver(dut, cable):
    """For a cable without a driver, the mux direction is answered 'not Y-Cable
    port', with presence True.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. Ethernet0's transceiver info names a vendor
       and model that sonic_y_cable does not map to a driver.
    2. Start ycabled.
    3. Send XCVRD_SHOW_HWMODE_DIR_CMD probe for Ethernet0, and expect the
       response {state: not Y-Cable port} and the result {presence: True}.
    """
    no_driver(dut)
    dut.start()
    r = dut.cli("XCVRD_SHOW_HWMODE_DIR_CMD", "probe", "Ethernet0")
    assert r.rsp == {"state": "not Y-Cable port"} and r.res == {"presence": "True"}


def test_show_mux_direction_cable_gone(dut, cable):
    """For a cable that is gone, before xcvrd has removed it, the mux direction
    is answered unknown, with presence False.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving.
    2. Start ycabled, then make Ethernet0's cable absent in the platform API.
    3. Send XCVRD_SHOW_HWMODE_DIR_CMD probe for Ethernet0, and expect the
       response {state: unknown} and the result {presence: False}.
    """
    dut.start()
    cable.present = False
    r = dut.cli("XCVRD_SHOW_HWMODE_DIR_CMD", "probe", "Ethernet0")
    assert r.rsp == {"state": "unknown"} and r.res == {"presence": "False"}


def test_show_mux_direction_not_mux_port(dut, cable):
    """For a port without a MUX_CABLE entry, the mux direction is answered
    unknown.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. Ethernet8 has no MUX_CABLE entry.
    2. Start ycabled.
    3. Send XCVRD_SHOW_HWMODE_DIR_CMD probe for Ethernet8, and expect the
       response {state: unknown}.
    """
    dut.start()
    assert dut.cli("XCVRD_SHOW_HWMODE_DIR_CMD", "probe", "Ethernet8").rsp == {"state": "unknown"}


def test_show_mux_direction_active_active(dut):
    """Active-active: the state ycabled last published in HW_MUX_CABLE_TABLE,
    not the SoC's; the SoC is not asked.

    Steps:
    1. Configure Ethernet0 as an active-active port of the lower ToR, with a
       SoC double at 192.168.0.3.
    2. Start ycabled, which publishes Ethernet0 active, then set both of the
       SoC's admin states to standby.
    3. Send XCVRD_SHOW_HWMODE_DIR_CMD probe for Ethernet0.
    4. Expect the response {state: active} and the result {presence: True}, and
       no RPC at the SoC since the command.
    """
    dut.configure(Topology().active_active("Ethernet0", soc_ipv4=SOC + "/32"))
    soc = dut.soc(SOC)
    dut.start()
    soc.admin = [False, False]
    mark = soc.mark()
    r = dut.cli("XCVRD_SHOW_HWMODE_DIR_CMD", "probe", "Ethernet0")
    assert r.rsp == {"state": "active"} and r.res == {"presence": "True"}
    assert not soc.rpcs(since=mark)


def test_show_mux_direction_active_active_without_state(dut):
    """Without a published state, the SoC is asked: the mux direction of an
    active-active port whose HW_MUX_CABLE_TABLE row has no state is its admin
    forwarding state on the SoC.

    Steps:
    1. Configure Ethernet0 as an active-active port of the lower ToR, with a
       SoC double at 192.168.0.3.
    2. Start ycabled. Then set the SoC's admin states to standby for port id 0,
       this ToR's, and active for port id 1, and delete the state field of
       Ethernet0's STATE_DB HW_MUX_CABLE_TABLE row.
    3. Send XCVRD_SHOW_HWMODE_DIR_CMD probe for Ethernet0.
    4. Expect the response {state: standby}, and QueryAdminForwardingPortState
       as the only RPC at the SoC since the command.
    """
    dut.configure(Topology().active_active("Ethernet0", soc_ipv4=SOC + "/32"))
    soc = dut.soc(SOC)
    dut.start()
    soc.admin = [False, True]
    dut.state.r.hdel("HW_MUX_CABLE_TABLE|Ethernet0", "state")
    mark = soc.mark()
    assert dut.cli("XCVRD_SHOW_HWMODE_DIR_CMD", "probe", "Ethernet0").rsp == {"state": "standby"}
    assert [r.name for r in soc.rpcs(since=mark)] == ["QueryAdminForwardingPortState"]


def test_show_mux_direction_active_active_without_soc_address(dut):
    """For an active-active port without a SoC address, the mux direction is
    answered unknown, with presence True.

    Steps:
    1. Configure Ethernet0 as active-standby, and Ethernet4 as active-active
       without a SoC address.
    2. Start ycabled.
    3. Send XCVRD_SHOW_HWMODE_DIR_CMD probe for Ethernet4, and expect the
       response {state: unknown} and the result {presence: True}.
    """
    dut.configure(Topology().active_standby("Ethernet0").active_active("Ethernet4"))
    dut.start()
    r = dut.cli("XCVRD_SHOW_HWMODE_DIR_CMD", "probe", "Ethernet4")
    assert r.rsp == {"state": "unknown"} and r.res == {"presence": "True"}


# config muxcable hwmode state ---------------------------------------------------

@pytest.mark.parametrize("read_side,state,method", [
    (1, "active", "toggle_mux_to_tor_a"), (1, "standby", "toggle_mux_to_tor_b"),
    (2, "active", "toggle_mux_to_tor_b"), (2, "standby", "toggle_mux_to_tor_a")])
def test_config_mux_state(dut, cable, read_side, state, method):
    """config muxcable hwmode state reads the read side from the cable and
    toggles the mux to the requested state's side, answering result True. The
    CLI toggles the cable only: HW_MUX_CABLE_TABLE is left alone.

    Cases: read side A or B, set to active or standby, which selects
    toggle_mux_to_tor_a() or toggle_mux_to_tor_b().

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. Ethernet0's cable has the given read side.
    2. Start ycabled.
    3. Send XCVRD_CONFIG_HWMODE_DIR_CMD with the state for Ethernet0.
    4. Expect the response {result: True}; get_read_side() and the toggle
       method, in that order, as the only driver calls; and no
       HW_MUX_CABLE_TABLE write.
    """
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
    """When the toggle fails, config muxcable hwmode state answers result False
    if the driver returned False, or -1 if it raised.

    Cases: read side A or B, set to active or standby; the toggle method
    returns False or raises OSError.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. Ethernet0's cable has the given read side.
    2. Start ycabled, then script the toggle method of Ethernet0's cable to
       fail that way.
    3. Send XCVRD_CONFIG_HWMODE_DIR_CMD with the state for Ethernet0, and
       expect the response {result: False} or {result: -1}.
    """
    cable.read_side = read_side
    dut.start()
    cable.script(method, **script)
    assert dut.cli("XCVRD_CONFIG_HWMODE_DIR_CMD", state, "Ethernet0").rsp == {"result": result}


@pytest.mark.parametrize("case", ["read-side-fails", "read-side-nic", "no-driver", "bad-state"])
def test_config_mux_state_errors(dut, cable, case):
    """config muxcable hwmode state answers result False, without toggling,
    when it cannot toggle: the read side cannot be read or is the NIC side, the
    cable has no driver, or the requested state is neither active nor standby.

    Cases: get_read_side() raises OSError (read-side-fails) or returns 0
    (read-side-nic); no driver (no-driver); the state sideways (bad-state).

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. In the no-driver case, Ethernet0's
       transceiver info names a vendor and model that sonic_y_cable does not
       map to a driver.
    2. Start ycabled, then script get_read_side() of Ethernet0's cable as the
       case says.
    3. Send XCVRD_CONFIG_HWMODE_DIR_CMD active, or sideways in the bad-state
       case, for Ethernet0.
    4. Expect the response {result: False}, and no toggle call to the cable
       since the command.
    """
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
    """show muxcable hwmode switchmode answers the cable's switching mode: auto
    for 1, manual for 0, unknown for any other value.

    Cases: get_switching_mode() returns 1, 0, 5 or -1.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. Ethernet0's cable has the given switching
       mode.
    2. Start ycabled.
    3. Send XCVRD_SHOW_HWMODE_SWMODE_CMD probe for Ethernet0, and expect the
       response {state: auto, manual or unknown}.
    """
    cable.switching_mode = mode
    dut.start()
    assert dut.cli("XCVRD_SHOW_HWMODE_SWMODE_CMD", "probe", "Ethernet0").rsp == {"state": state}


@pytest.mark.parametrize("case,state", [("no-driver", "not Y-Cable port"), ("cable-gone", "cable not present"),
                                        ("driver-fails", "unknown")])
def test_show_switch_mode_errors(dut, cable, case, state):
    """show muxcable hwmode switchmode answers 'not Y-Cable port' for a cable
    without a driver, 'cable not present' for a cable that is gone, and unknown
    when the driver raises.

    Cases: no driver (no-driver); the cable absent in the platform API
    (cable-gone); get_switching_mode() raises OSError (driver-fails).

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. In the no-driver case, Ethernet0's
       transceiver info names a vendor and model that sonic_y_cable does not
       map to a driver.
    2. Start ycabled, then make Ethernet0's cable absent, or script its
       get_switching_mode() to raise, as the case says.
    3. Send XCVRD_SHOW_HWMODE_SWMODE_CMD probe for Ethernet0, and expect the
       case's state in the response.
    """
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
    """Setting the switching mode calls set_switching_mode() once, with 1 for
    auto or 0 for manual, and answers result True.

    Cases: auto or manual.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving.
    2. Start ycabled.
    3. Send XCVRD_CONFIG_HWMODE_SWMODE_CMD with the mode for Ethernet0.
    4. Expect the response {result: True}, set_switching_mode(1 or 0) as the
       only driver call, and the cable in that mode.
    """
    dut.start()
    mark = dut.hw.mark()
    assert dut.cli("XCVRD_CONFIG_HWMODE_SWMODE_CMD", mode, "Ethernet0").rsp == {"result": "True"}
    assert calls(dut, mark) == [("set_switching_mode", (value,))]
    assert cable.switching_mode == value


@pytest.mark.parametrize("mode", ["auto", "manual"])
@pytest.mark.parametrize("case", ["driver-fails", "driver-false", "bad-mode", "no-driver"])
def test_config_switch_mode_errors(dut, cable, case, mode):
    """Setting the switching mode answers result False when the driver raises
    or returns -1, when the mode is neither auto nor manual, or when the cable
    has no driver.

    Cases: auto or manual, with set_switching_mode() raising OSError
    (driver-fails) or returning -1 (driver-false), the mode sometimes instead
    (bad-mode), or no driver (no-driver).

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. In the no-driver case, Ethernet0's
       transceiver info names a vendor and model that sonic_y_cable does not
       map to a driver.
    2. Start ycabled, then script set_switching_mode() of Ethernet0's cable as
       the case says.
    3. Send XCVRD_CONFIG_HWMODE_SWMODE_CMD with the mode for Ethernet0, and
       expect the response {result: False}.
    """
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
    """show muxcable firmware version reads the active, inactive and next
    versions of the NIC and both ToR sides from the cable, and answers status
    True with the nine version fields in the result row: self is the read
    side's, peer the other side's. ycabled writes the result row before the
    response, and deletes the command key last.

    Cases: read side A or B.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. Ethernet0's cable has the given read side.
    2. Start ycabled.
    3. Send XCVRD_SHOW_FW_CMD probe for Ethernet0.
    4. Expect the response {status: True}, and the result with the cable's
       versions, self and peer by the read side.
    5. Expect ycabled's writes in this order: the result row, the response, the
       deletion of the command key.
    """
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
    """When one side's versions cannot be read, show muxcable firmware version
    still answers status True, with N/A for that side's three versions.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving.
    2. Start ycabled, then script get_firmware_version() of Ethernet0's cable
       to raise OSError for ToR B, the peer of read side A.
    3. Send XCVRD_SHOW_FW_CMD probe for Ethernet0.
    4. Expect the response {status: True}, with N/A in version_peer_active,
       version_peer_inactive and version_peer_next only.
    """
    dut.start()
    cable.script("get_firmware_version", func=_firmware_tor_b_fails)
    r = dut.cli("XCVRD_SHOW_FW_CMD", "probe", "Ethernet0")
    assert r.rsp == {"status": "True"}
    assert {k for k, v in r.res.items() if v == "N/A"} == {"version_peer_active", "version_peer_inactive",
                                                           "version_peer_next"}


@pytest.mark.parametrize("case", ["read-side-fails", "no-driver"])
def test_show_firmware_errors(dut, cable, case):
    """When the read side cannot be read, or the cable has no driver, show
    muxcable firmware version answers status False, with N/A in all nine
    version fields.

    Cases: get_read_side() raises OSError (read-side-fails), or no driver
    (no-driver).

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. In the no-driver case, Ethernet0's
       transceiver info names a vendor and model that sonic_y_cable does not
       map to a driver.
    2. Start ycabled, then script get_read_side() of Ethernet0's cable to raise
       in the read-side-fails case.
    3. Send XCVRD_SHOW_FW_CMD probe for Ethernet0.
    4. Expect the response {status: False}, and a result row of nine fields,
       all N/A.
    """
    if case == "no-driver":
        no_driver(dut)
    dut.start()
    if case == "read-side-fails":
        cable.script("get_read_side", raises="OSError")
    r = dut.cli("XCVRD_SHOW_FW_CMD", "probe", "Ethernet0")
    assert r.rsp == {"status": "False"} and set(r.res.values()) == {"N/A"} and len(r.res) == 9


# config muxcable firmware download/activate/rollback ----------------------------

def test_download_firmware(dut, cable):
    """config muxcable firmware download calls the driver's download_firmware()
    with the image's path in the firmware directory, and answers status 0. The
    download runs in its own thread, which waits 5 s after the driver returns.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. Write the firmware image bb-fw.bin to the
       firmware directory, /usr/share/sonic/firmware.
    2. Start ycabled.
    3. Send XCVRD_DOWN_FW_CMD bb-fw.bin for Ethernet0, and wait up to 20 s for
       the response.
    4. Expect the response {status: 0}, one download_firmware() call with the
       image's full path, and the response no sooner than 5 s after the
       command.
    """
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
    reports the last known versions, from MUX_CABLE_INFO, without asking the
    cable.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. Write the firmware image bb-fw.bin to the
       firmware directory, /usr/share/sonic/firmware.
    2. Start ycabled, and note the version_* fields of Ethernet0's
       MUX_CABLE_INFO.
    3. Block download_firmware() of Ethernet0's cable, send XCVRD_DOWN_FW_CMD
       bb-fw.bin without waiting, and wait for the driver call. Expect the
       cable to report the download in progress.
    4. Send XCVRD_SHOW_FW_CMD probe for Ethernet0. Expect the response {status:
       True} with the noted versions, and no get_firmware_version() call.
    5. Send XCVRD_SHOW_HWMODE_SWMODE_CMD probe for Ethernet0, and expect the
       response {state: auto}.
    6. Expect no XCVRD_DOWN_FW_RSP yet. Release the download, and expect
       XCVRD_DOWN_FW_RSP {status: 0} within 15 s.
    """
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
    startup): the versions are N/A while the download runs.

    Steps:
    1. Configure active-standby ports Ethernet0 and Ethernet4. Write the
       firmware image bb-fw.bin to the firmware directory,
       /usr/share/sonic/firmware.
    2. Start ycabled, and expect no MUX_CABLE_INFO for Ethernet4.
    3. Block download_firmware() of Ethernet4's cable, send XCVRD_DOWN_FW_CMD
       bb-fw.bin for Ethernet4 without waiting, and wait for the driver call.
    4. Send XCVRD_SHOW_FW_CMD probe for Ethernet4, and expect the response
       {status: True} with every version N/A.
    5. Release the download, and expect the XCVRD_DOWN_FW_RSP write for
       Ethernet4 within 15 s.
    """
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
    """After a failed download, show muxcable firmware version reads the
    versions from the cable as usual, and logs at ERR that the previous
    download failed.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving.
    2. Start ycabled, then set the download status of Ethernet0's cable to
       failed.
    3. Send XCVRD_SHOW_FW_CMD probe for Ethernet0.
    4. Expect the response {status: True} with the cable's versions, and
       'Firmware Download API failed in the previous run' at ERR in syslog
       within 3 s.
    """
    dut.start()
    cable.download_firmware_status = FIRMWARE_DOWNLOAD_STATUS_FAILED
    mark = dut.syslog.mark()
    r = dut.cli("XCVRD_SHOW_FW_CMD", "probe", "Ethernet0")
    assert r.rsp == {"status": "True"} and r.res == expect.firmware_versions(cable)
    dut.syslog.wait_for(mark, 3, severity="ERR", contains="Firmware Download API failed in the previous run")


@pytest.mark.parametrize("case,status", [("missing-file", "-1"), ("no-driver", "-1"), ("driver-fails", "-1")])
def test_download_firmware_errors(dut, cable, case, status):
    """A download that cannot run, or fails, answers status -1.

    Cases: the image file does not exist (missing-file), the cable has no
    driver (no-driver), or download_firmware() raises OSError (driver-fails).

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. Write the firmware image bb-fw.bin to the
       firmware directory, /usr/share/sonic/firmware, except in the
       missing-file case. In the no-driver case, Ethernet0's transceiver info
       names a vendor and model that sonic_y_cable does not map to a driver; in
       the driver-fails case, script download_firmware() to raise.
    2. Start ycabled.
    3. Send XCVRD_DOWN_FW_CMD bb-fw.bin for Ethernet0, waiting up to 10 s, and
       expect the response {status: -1}.
    """
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
    """config muxcable firmware activate calls activate_firmware() once, with
    the image's path, or None for the image name null, and hitless True, or
    False when the argument row has hitless 1. It answers status 0, and leaves
    the argument row for the CLI to delete.

    Cases: the image bb-fw.bin (file), the same with the argument row {hitless:
    1} (nonhitless), or the image name null (null).

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. Write the firmware image bb-fw.bin to the
       firmware directory, /usr/share/sonic/firmware.
    2. Start ycabled.
    3. Send XCVRD_ACTI_FW_CMD with the case's image name and argument row for
       Ethernet0, waiting up to 20 s.
    4. Expect the response {status: 0}, and activate_firmware(path or None,
       hitless) as the only driver call.
    5. With an argument row, expect it still in APPL_DB.
    """
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
    """Activation answers status False when the image file does not exist or
    the cable has no driver, and -1 when activate_firmware() raises.

    Cases: missing-file, no-driver, or activate_firmware() raises OSError
    (driver-fails).

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. Write the firmware image bb-fw.bin to the
       firmware directory, /usr/share/sonic/firmware, except in the
       missing-file case. In the no-driver case, Ethernet0's transceiver info
       names a vendor and model that sonic_y_cable does not map to a driver; in
       the driver-fails case, script activate_firmware() to raise.
    2. Start ycabled.
    3. Send XCVRD_ACTI_FW_CMD bb-fw.bin for Ethernet0, waiting up to 10 s, and
       expect the case's status.
    """
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
    """config muxcable firmware rollback calls rollback_firmware() once, with
    the image's path, or None for the image name null, and answers status 0.

    Cases: the image bb-fw.bin, or the image name null.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. Write the firmware image bb-fw.bin to the
       firmware directory, /usr/share/sonic/firmware.
    2. Start ycabled.
    3. Send XCVRD_ROLL_FW_CMD with the image name for Ethernet0.
    4. Expect the response {status: 0}, and rollback_firmware(path or None) as
       the only driver call.
    """
    dut.firmware_file("bb-fw.bin")
    dut.start()
    mark = dut.hw.mark()
    assert dut.cli("XCVRD_ROLL_FW_CMD", fwfile, "Ethernet0").rsp == {"status": "0"}
    assert calls(dut, mark) == [("rollback_firmware", (expected,))]


@pytest.mark.parametrize("case,status", [("missing-file", "False"), ("driver-fails", "-1")])
def test_rollback_firmware_errors(dut, cable, case, status):
    """Rollback answers status False when the image file does not exist, and -1
    when rollback_firmware() raises.

    Cases: missing-file, or rollback_firmware() raises OSError (driver-fails).

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. Write the firmware image bb-fw.bin to the
       firmware directory, /usr/share/sonic/firmware, except in the
       missing-file case; in the driver-fails case, script rollback_firmware()
       to raise.
    2. Start ycabled.
    3. Send XCVRD_ROLL_FW_CMD bb-fw.bin for Ethernet0, and expect the case's
       status.
    """
    if case != "missing-file":
        dut.firmware_file("bb-fw.bin")
    if case == "driver-fails":
        cable.script("rollback_firmware", raises="OSError")
    dut.start()
    assert dut.cli("XCVRD_ROLL_FW_CMD", "bb-fw.bin", "Ethernet0").rsp == {"status": status}


@pytest.mark.known_issue("rollback for a port without a driver answers, then kills the CLI thread "
                         "(no lock for the port)")
def test_rollback_firmware_without_driver(dut, cable):
    """A rollback for a port without a driver answers status False, and the CLI
    commands after it are still served.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. Ethernet0's transceiver info names a vendor
       and model that sonic_y_cable does not map to a driver. Write the
       firmware image bb-fw.bin to the firmware directory,
       /usr/share/sonic/firmware.
    2. Start ycabled. On Python ycabled, let the teardown accept the crash of
       the known issue.
    3. Send XCVRD_ROLL_FW_CMD null for Ethernet0, and expect the response
       {status: False}.
    4. Send XCVRD_SHOW_HWMODE_SWMODE_CMD probe for Ethernet4 without waiting,
       and expect its response {state: auto} within 5 s.
    """
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
    ("enable", {"target": "1", "mode_value": "3", "lane_mask": "15", "direction": "2"},
     "enable_prbs_mode", (1, 3, 15, 2)),
    ("enable", {"target": "0", "mode_value": "3", "lane_mask": "15"}, "enable_prbs_mode", (0, 3, 15, 0)),
    ("disable", {"target": "2", "direction": "1"}, "disable_prbs_mode", (2, 1)),
    ("disable", {"target": "2"}, "disable_prbs_mode", (2, 0)),
    ("reset", {"target": "1"}, "reset", (1,)),
    ("anlt", {"target": "1", "mode": "1"}, "set_anlt", (1, 1)),
    ("fec", {"target": "0", "mode": "2"}, "set_fec_mode", (2, 0)),
], ids=["enable", "enable-both-directions", "disable", "disable-both-directions", "reset", "anlt", "fec"])
def test_prbs_family(dut, cable, mode, args, method, margs):
    """Each XCVRD_CONFIG_PRBS_CMD mode (enable and disable PRBS, reset, ANLT
    and FEC) makes one driver call, with the argument row's values as integers,
    and answers status True. set_fec_mode() takes the mode before the target.

    Cases: enable with a direction, or without one, which is 0, both directions
    (enable-both-directions); the same for disable; reset; anlt; fec.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving.
    2. Start ycabled.
    3. Send XCVRD_CONFIG_PRBS_CMD with the mode and its argument row for
       Ethernet0.
    4. Expect the response {status: True}, and the mode's driver method, with
       the expected arguments, as the only driver call.
    """
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
    """An XCVRD_CONFIG_PRBS_CMD command with a missing argument, or an unknown
    mode, answers status False without calling the cable.

    Cases: enable without lane_mask (no-lane-mask) or without target
    (no-target); anlt or fec without mode (anlt-no-mode, fec-no-mode); the mode
    sideways (unknown-mode).

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving.
    2. Start ycabled.
    3. Send XCVRD_CONFIG_PRBS_CMD with the case's mode and argument row for
       Ethernet0.
    4. Expect the response {status: False}, and no driver call since the
       command.
    """
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
    """An XCVRD_CONFIG_PRBS_CMD command answers status -1 when its driver
    method raises, and False when the cable has no driver.

    Cases: each mode (enable, disable, reset, anlt, fec), with its driver
    method raising OSError (driver-fails), or without a driver (no-driver).

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. In the no-driver case, Ethernet0's
       transceiver info names a vendor and model that sonic_y_cable does not
       map to a driver.
    2. Start ycabled, then script the mode's driver method of Ethernet0's cable
       to raise OSError.
    3. Send XCVRD_CONFIG_PRBS_CMD with the mode and its argument row for
       Ethernet0, and expect the case's status.
    """
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
    """XCVRD_CONFIG_LOOP_CMD makes one driver call and answers status True:
    enable_loopback_mode(target, mode, lane_mask), with mode 1 when the
    argument row has no mode_value, or disable_loopback_mode(target).

    Cases: enable with mode_value 2 (enable), enable without mode_value
    (enable-near-end), and disable.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving.
    2. Start ycabled.
    3. Send XCVRD_CONFIG_LOOP_CMD with the mode and its argument row for
       Ethernet0.
    4. Expect the response {status: True}, and the expected driver call as the
       only one.
    """
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
    """XCVRD_CONFIG_LOOP_CMD answers status False for a missing argument or a
    cable without a driver, and -1 when the driver raises.

    Cases: enable without lane_mask (no-lane-mask) or without target
    (no-target); enable or disable with the driver raising OSError
    (driver-fails); enable without a driver (no-driver).

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. In the no-driver case, Ethernet0's
       transceiver info names a vendor and model that sonic_y_cable does not
       map to a driver.
    2. Start ycabled, then script enable_loopback_mode() and
       disable_loopback_mode() of Ethernet0's cable to raise OSError.
    3. Send XCVRD_CONFIG_LOOP_CMD with the case's mode and argument row for
       Ethernet0, and expect the case's status.
    """
    if case == "no-driver":
        no_driver(dut)
    dut.start()
    cable.script("enable_loopback_mode", raises="OSError")
    cable.script("disable_loopback_mode", raises="OSError")
    assert dut.cli("XCVRD_CONFIG_LOOP_CMD", mode, "Ethernet0", args=args).rsp == {"status": status}


# show muxcable event_log, get_fec_anlt_speed -------------------------------------

def test_event_log(dut, cable):
    """The event log command answers status True, with the cable's event log in
    the result row: one field per entry, numbered from 0.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving.
    2. Start ycabled.
    3. Send XCVRD_EVENT_LOG_CMD probe for Ethernet0.
    4. Expect the response {status: True}, and the result {0: first entry, 1:
       second, ...}.
    """
    dut.start()
    r = dut.cli("XCVRD_EVENT_LOG_CMD", "probe", "Ethernet0")
    assert r.rsp == {"status": "True"}
    assert r.res == {str(i): e for i, e in enumerate(cable.event_log)}


@pytest.mark.parametrize("case,status", [("driver-fails", "-1"), ("no-driver", "False"), ("not-a-list", "True")])
def test_event_log_errors(dut, cable, case, status):
    """The event log command answers status -1 when the driver raises, False
    without a driver, and True when the driver returns something other than a
    list, all without a result.

    Cases: get_event_log() raises OSError (driver-fails), no driver
    (no-driver), or get_event_log() returns a string (not-a-list).

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. In the no-driver case, Ethernet0's
       transceiver info names a vendor and model that sonic_y_cable does not
       map to a driver.
    2. Start ycabled, then script get_event_log() of Ethernet0's cable as the
       case says.
    3. Send XCVRD_EVENT_LOG_CMD probe for Ethernet0, and expect the case's
       status and an empty result row.
    """
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
    """XCVRD_GET_FEC_CMD reads the FEC mode and the ANLT state of the NIC and
    both ToR sides, and the speed, and answers status True with them in the
    result row.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving.
    2. Start ycabled.
    3. Send XCVRD_GET_FEC_CMD probe for Ethernet0.
    4. Expect the response {status: True}, and the cable's values in the
       result: fec_nic, fec_tor_a, fec_tor_b, speed, anlt_nic, anlt_tor_a and
       anlt_tor_b.
    """
    dut.start()
    r = dut.cli("XCVRD_GET_FEC_CMD", "probe", "Ethernet0")
    assert r.rsp == {"status": "True"}
    assert r.res == {"fec_nic": "1", "fec_tor_a": "1", "fec_tor_b": "2", "speed": "100000",
                     "anlt_nic": "False", "anlt_tor_a": "True", "anlt_tor_b": "False"}


@pytest.mark.parametrize("case,status", [("driver-fails", "-1"), ("no-driver", "False")])
def test_fec_anlt_speed_errors(dut, cable, case, status):
    """XCVRD_GET_FEC_CMD answers status -1 when the driver raises, and False
    without a driver, without a result.

    Cases: get_speed() raises OSError (driver-fails), or no driver (no-driver).

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. In the no-driver case, Ethernet0's
       transceiver info names a vendor and model that sonic_y_cable does not
       map to a driver.
    2. Start ycabled, then script get_speed() of Ethernet0's cable to raise
       OSError.
    3. Send XCVRD_GET_FEC_CMD probe for Ethernet0, and expect the case's status
       and an empty result row.
    """
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
    """Each XCVRD_GET_BER_CMD mode reads one thing from the cable and answers
    status True with it in the result row: a list as fields numbered from 0, a
    dict as its own fields, a single value under the mode's name.

    Cases: ber, eye, fec_stats and pcs_stats for a target; cable_alive,
    health_check, reset_cause, operation_time and queue_info; debug_dump with
    the option all.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving.
    2. Start ycabled.
    3. Send XCVRD_GET_BER_CMD with the mode and its argument row for Ethernet0.
    4. Expect the response {status: True}, and the cable's values in the
       result.
    5. For debug_dump, expect debug_dump_registers('all') as the only driver
       call.
    """
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
    """An XCVRD_GET_BER_CMD mode that needs a target, sent without one, or an
    unknown mode, answers status False without a result and without calling the
    cable.

    Cases: ber, eye, fec_stats or pcs_stats without a target, and the mode
    sideways.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving.
    2. Start ycabled.
    3. Send XCVRD_GET_BER_CMD with the case's mode and argument row for
       Ethernet0.
    4. Expect the response {status: False}, an empty result row, and no driver
       call since the command.
    """
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
    """Each XCVRD_GET_BER_CMD mode answers status -1 when its driver method
    raises, and False without a driver, without a result.

    Cases: each mode, with its driver method raising OSError (driver-fails), or
    without a driver (no-driver).

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. In the no-driver case, Ethernet0's
       transceiver info names a vendor and model that sonic_y_cable does not
       map to a driver.
    2. Start ycabled, then script the mode's driver method of Ethernet0's cable
       to raise OSError.
    3. Send XCVRD_GET_BER_CMD with the mode and its argument row for Ethernet0,
       and expect the case's status and an empty result row.
    """
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
    """The cable is gone but xcvrd has not removed it yet: every command family
    answers its failure response without calling the cable.

    Cases: one command of each family, with its failure response: 'cable not
    present' for the switching mode, False or -1 for the others.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving. Write the firmware image bb-fw.bin to the
       firmware directory, /usr/share/sonic/firmware.
    2. Start ycabled, then make Ethernet0's cable absent in the platform API.
    3. Send the command for Ethernet0.
    4. Expect the case's response, and no driver call to the cable since the
       command.
    """
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
    """A command row without the command's own field is answered with the
    command's failure response, without calling the cable.
    XCVRD_SHOW_HWMODE_DIR_CMD is the exception
    (test_show_mux_direction_wrong_field_is_ignored).

    Cases: every other command table, with its response table and failure
    response.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving.
    2. Start ycabled.
    3. Write the command table's row for Ethernet0 to APPL_DB with only
       bogus=probe.
    4. Expect ycabled's write to the response table within 5 s, the case's
       failure response, and no driver call.
    """
    dut.start()
    mark = dut.monitor.mark()
    hw_mark = dut.hw.mark()
    dut.appl.set(cmd, "Ethernet0", {"bogus": "probe"})
    dut.monitor.wait_access(mark, 5, kind="write", table=rsp_table, key="Ethernet0")
    assert dut.state.get(rsp_table, "Ethernet0") == rsp
    assert not calls(dut, hw_mark)


def test_show_mux_direction_wrong_field_is_ignored(dut, cable):
    """An XCVRD_SHOW_HWMODE_DIR_CMD row without its command field is ignored:
    no response, and the CLI commands are still served.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving.
    2. Start ycabled.
    3. Write XCVRD_SHOW_HWMODE_DIR_CMD {bogus: probe} for Ethernet0 to APPL_DB.
    4. Expect no XCVRD_SHOW_HWMODE_DIR_RSP write for 1.5 s.
    5. Send XCVRD_SHOW_HWMODE_SWMODE_CMD probe for Ethernet0, and expect the
       response {state: auto}.
    """
    dut.start()
    mark = dut.monitor.mark()
    dut.appl.set("XCVRD_SHOW_HWMODE_DIR_CMD", "Ethernet0", {"bogus": "probe"})
    assert stays_true(lambda: not dut.writes(since=mark, table="XCVRD_SHOW_HWMODE_DIR_RSP"), 1.5)
    # The CLI thread is still serving.
    assert dut.cli("XCVRD_SHOW_HWMODE_SWMODE_CMD", "probe", "Ethernet0").rsp == {"state": "auto"}


def test_commands_for_several_ports(dut):
    """Commands for different ports in flight together are all answered, each
    with its own port's value.

    Steps:
    1. Configure active-standby ports Ethernet0, Ethernet4 and Ethernet8, with
       Ethernet4's cable in manual switching mode.
    2. Start ycabled.
    3. Send XCVRD_SHOW_HWMODE_SWMODE_CMD probe for the three ports, without
       waiting.
    4. Expect each port's response within 5 s, and Ethernet4's {state: manual}.
    """
    dut.configure(Topology().active_standby("Ethernet0", "Ethernet4", "Ethernet8"))
    dut.hw.port(2).switching_mode = 0
    dut.start()
    marks = [dut.cli("XCVRD_SHOW_HWMODE_SWMODE_CMD", "probe", p, wait=False)
             for p in ("Ethernet0", "Ethernet4", "Ethernet8")]
    for port in ("Ethernet0", "Ethernet4", "Ethernet8"):
        dut.monitor.wait_access(marks[0], 5, kind="write", table="XCVRD_SHOW_HWMODE_SWMODE_RSP", key=port)
    assert dut.state.field("XCVRD_SHOW_HWMODE_SWMODE_RSP", "Ethernet4", "state") == "manual"
