"""Requests for one cable from different sources at the same time: toggles,
insertion and removal, and the CLI.

Each test holds a call in the fake cable, sends a second request for the same
port, then releases the call. A daemon that serves a port's requests one at a
time, as the Rust design's port actors do, passes. Python ycabled serves them
from separate threads that share the driver and the port's STATE_DB entry.
"""

import pytest

from harness.fake_hw import TARGET_TOR_A, TARGET_TOR_B
from harness.topology import Topology
from harness.util import eventually, wait_until

# How long the daemon gets to act on the second request while the first call
# is held. A daemon that queues it behind the held call does nothing until the
# call is released, so this wait ends without an error.
HELD = 2


@pytest.fixture
def cable(dut):
    """Ethernet0 is the cable under test; Ethernet4 keeps ycabled serving."""
    dut.configure(Topology().active_standby("Ethernet0", "Ethernet4"))
    return dut.hw.port(1)


def maybe(predicate, timeout=HELD):
    return eventually(predicate, timeout)


def state_writes(dut, since):
    return dut.writes(since=since, table="HW_MUX_CABLE_TABLE", key="Ethernet0", field="state")


def removed(dut, since):
    return [a for a in state_writes(dut, since) if a.value == "unknown"]


def hold_cli_call(dut, cable):
    """`show muxcable hwmode muxdirection` for Ethernet0, held in its first
    call to the cable. Returns the release gate, the call mark and the CLI
    mark."""
    gate = cable.script("get_read_side", block=True, times=1)
    hw_mark = dut.hw.mark()
    cli_mark = dut.cli("XCVRD_SHOW_HWMODE_DIR_CMD", "probe", "Ethernet0", wait=False)
    wait_until(lambda: dut.hw.calls(hw_mark, port=1, method="get_read_side"), 5)
    return gate, hw_mark, cli_mark


def remove_and_wait(dut):
    mark = dut.monitor.mark()
    dut.remove("Ethernet0")
    dut.monitor.wait_access(mark, 5, kind="write", table="HW_MUX_CABLE_TABLE", key="Ethernet0")


@pytest.mark.known_issue("an active-standby toggle calls the cable without the port lock, during "
                         "another thread's call to the same cable")
def test_toggle_waits_for_call_in_progress(dut, cable):
    """A toggle for a cable in the middle of another call waits for that call
    to return: two calls to one cable never overlap. The CLI command and the
    toggle are both answered.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving.
    2. Start ycabled.
    3. Send XCVRD_SHOW_HWMODE_DIR_CMD probe for Ethernet0 (show muxcable hwmode
       muxdirection), with its first call to the cable, get_read_side(), held,
       and wait for that call.
    4. Toggle Ethernet0 to standby without waiting, and give ycabled 2 s to
       call toggle_mux_to_tor_b() meanwhile.
    5. Release the held call.
    6. Expect ycabled's HW_MUX_CABLE_TABLE state write for Ethernet0 and the
       CLI response, each within 10 s, and no two calls to the cable that
       overlapped.
    """
    dut.start()
    gate, hw_mark, cli_mark = hold_cli_call(dut, cable)
    mark = dut.monitor.mark()
    dut.toggle("Ethernet0", "standby", wait=False)
    maybe(lambda: dut.hw.calls(hw_mark, port=1, method="toggle_mux_to_tor_b"))
    gate.set()
    dut.monitor.wait_access(mark, 10, kind="write", table="HW_MUX_CABLE_TABLE", key="Ethernet0", field="state")
    dut.monitor.wait_access(cli_mark, 10, kind="write", table="XCVRD_SHOW_HWMODE_DIR_RSP", key="Ethernet0")
    assert not dut.hw.overlaps(hw_mark, port=1)


@pytest.mark.known_issue("a toggle that finishes after the cable's removal overwrites the "
                         "removal's HW_MUX_CABLE_TABLE state")
def test_removal_during_toggle_leaves_unknown(dut, cable):
    """A cable removed while a toggle is switching it ends up unknown in
    HW_MUX_CABLE_TABLE, whichever request finishes first: the toggle's state
    does not overwrite the removal's.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving.
    2. Start ycabled.
    3. Hold toggle_mux_to_tor_b() of Ethernet0's cable once, toggle Ethernet0
       to standby without waiting, and wait for the held call.
    4. Remove the cable, as xcvrd reports it (TRANSCEIVER_INFO deleted, the
       cable absent), and give ycabled 2 s to write unknown for it.
    5. Release the held call, which succeeds. Wait for two HW_MUX_CABLE_TABLE
       state writes for Ethernet0, one for each request, and wait for ycabled
       to go quiet.
    6. Expect Ethernet0's state unknown.
    """
    dut.start()
    gate = cable.script("toggle_mux_to_tor_b", block=True, times=1)
    hw_mark = dut.hw.mark()
    mark = dut.monitor.mark()
    dut.toggle("Ethernet0", "standby", wait=False)
    wait_until(lambda: dut.hw.calls(hw_mark, port=1, method="toggle_mux_to_tor_b"), 5)
    # The cable is pulled while it switches; the toggle still succeeds.
    dut.remove("Ethernet0")
    maybe(lambda: removed(dut, mark))
    gate.set()
    # One write for the toggle and one for the removal, in either order.
    wait_until(lambda: len(state_writes(dut, mark)) >= 2, 10)
    dut.monitor.wait_settled()
    assert dut.state.field("HW_MUX_CABLE_TABLE", "Ethernet0", "state") == "unknown"


@pytest.mark.known_issue("an insertion that read the mux direction before a toggle writes it after "
                         "the toggle, so HW_MUX_CABLE_TABLE disagrees with the cable")
def test_toggle_during_insertion_keeps_cable_direction(dut, cable):
    """A toggle that arrives while an insertion initializes the cable leaves
    HW_MUX_CABLE_TABLE agreeing with the cable: the state the insertion read
    before the toggle does not overwrite the toggle's.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving.
    2. Start ycabled, then remove Ethernet0's cable, as xcvrd reports it, and
       wait for ycabled's write for the removal.
    3. Script get_mux_direction() of the cable to return ToR A, held, once.
       Insert the cable, and wait for the held call: the insertion has read the
       direction and not yet written it.
    4. Toggle Ethernet0 to standby without waiting, and give ycabled 2 s to
       call toggle_mux_to_tor_b() meanwhile.
    5. Release the held call. Wait for two HW_MUX_CABLE_TABLE state writes for
       Ethernet0, one for each request, and wait for ycabled to go quiet.
    6. Expect the cable's mux on ToR B, and Ethernet0's state standby.
    """
    dut.start()
    remove_and_wait(dut)
    # The insertion reads the direction (ToR A) and is held before it writes it.
    gate = cable.script("get_mux_direction", value=TARGET_TOR_A, block=True, times=1)
    hw_mark = dut.hw.mark()
    dut.insert("Ethernet0")
    wait_until(lambda: dut.hw.calls(hw_mark, port=1, method="get_mux_direction"), 5)
    mark = dut.monitor.mark()
    dut.toggle("Ethernet0", "standby", wait=False)
    maybe(lambda: dut.hw.calls(hw_mark, port=1, method="toggle_mux_to_tor_b"))
    gate.set()
    # One write for the insertion and one for the toggle, in either order.
    wait_until(lambda: len(state_writes(dut, mark)) >= 2, 10)
    dut.monitor.wait_settled()
    assert cable.mux_direction == TARGET_TOR_B
    assert dut.state.field("HW_MUX_CABLE_TABLE", "Ethernet0", "state") == "standby"


@pytest.mark.known_issue("toggles use an inserted cable's driver before get_vendor() has verified it")
def test_no_toggle_through_unverified_driver(dut, cable):
    """A toggle that arrives while an insertion checks the new driver with
    get_vendor() does not use that driver. Here the check fails, so the cable
    has no driver: the toggle is answered unknown, without a toggle call.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving.
    2. Start ycabled, then remove Ethernet0's cable, as xcvrd reports it, and
       wait for ycabled's write for the removal.
    3. Script get_vendor() of the cable to return another vendor, held, once:
       the driver the vendor mapping selects does not work for this cable.
       Insert the cable, and wait for the held call.
    4. Toggle Ethernet0 to standby without waiting, and give ycabled 2 s to
       call toggle_mux_to_tor_b() meanwhile.
    5. Release the held call. Wait for two HW_MUX_CABLE_TABLE state writes for
       Ethernet0, the rejected insertion's and the toggle's, and wait for
       ycabled to go quiet.
    6. Expect no toggle call to the cable since the insertion, and Ethernet0's
       state unknown.
    """
    dut.start()
    remove_and_wait(dut)
    # The driver the vendor mapping selects does not work for this cable.
    gate = cable.script("get_vendor", value="Somebody Else", block=True, times=1)
    hw_mark = dut.hw.mark()
    dut.insert("Ethernet0")
    wait_until(lambda: dut.hw.calls(hw_mark, port=1, method="get_vendor"), 5)
    mark = dut.monitor.mark()
    dut.toggle("Ethernet0", "standby", wait=False)
    maybe(lambda: dut.hw.calls(hw_mark, port=1, method="toggle_mux_to_tor_b"))
    gate.set()
    # The rejected insertion writes unknown, and so does the toggle without a driver.
    wait_until(lambda: len(state_writes(dut, mark)) >= 2, 10)
    dut.monitor.wait_settled()
    assert not [c.method for c in dut.hw.calls(hw_mark, port=1) if c.method.startswith("toggle_mux")]
    assert dut.state.field("HW_MUX_CABLE_TABLE", "Ethernet0", "state") == "unknown"


@pytest.mark.known_issue("re-inserting a cable replaces the port lock while another thread holds it, "
                         "so the insertion's calls overlap that thread's call")
def test_insertion_waits_for_call_in_progress(dut, cable):
    """A cable pulled and plugged in again during another call to it is
    initialized after that call returns: the insertion's calls do not overlap
    it.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving.
    2. Start ycabled.
    3. Send XCVRD_SHOW_HWMODE_DIR_CMD probe for Ethernet0 (show muxcable hwmode
       muxdirection), with its first call to the cable, get_read_side(), held,
       and wait for that call.
    4. Remove the cable and insert it again, and give ycabled 2 s to call
       get_vendor() for the insertion meanwhile.
    5. Release the held call. Wait for ycabled's MUX_CABLE_STATIC_INFO write
       for Ethernet0, which the insertion makes, and for the CLI response,
       within 10 s each, and wait for ycabled to go quiet.
    6. Expect no two calls to the cable that overlapped.
    """
    dut.start()
    gate, hw_mark, cli_mark = hold_cli_call(dut, cable)
    mark = dut.monitor.mark()
    # The cable is pulled and plugged in again during the call.
    dut.remove("Ethernet0")
    dut.insert("Ethernet0")
    maybe(lambda: dut.hw.calls(hw_mark, port=1, method="get_vendor"))
    gate.set()
    dut.monitor.wait_access(mark, 10, kind="write", table="MUX_CABLE_STATIC_INFO", key="Ethernet0")
    dut.monitor.wait_access(cli_mark, 10, kind="write", table="XCVRD_SHOW_HWMODE_DIR_RSP", key="Ethernet0")
    dut.monitor.wait_settled()
    assert not dut.hw.overlaps(hw_mark, port=1)


@pytest.mark.known_issue("a CLI command that fetched the driver before the cable's removal keeps "
                         "calling the cable after the removal")
def test_no_cable_calls_after_removal(dut, cable):
    """Once ycabled has written a cable's removal, it makes no more calls to
    that cable: a CLI command that was in progress does not go on calling it.

    Steps:
    1. Configure active-standby ports Ethernet0, under test, and Ethernet4,
       which keeps ycabled serving.
    2. Start ycabled.
    3. Send XCVRD_SHOW_HWMODE_DIR_CMD probe for Ethernet0 (show muxcable hwmode
       muxdirection), with its first call to the cable, get_read_side(), held,
       and wait for that call.
    4. Remove the cable, as xcvrd reports it, and give ycabled 2 s to write
       unknown for it.
    5. Release the held call. Wait for the removal's unknown state and the CLI
       response, within 10 s each, and wait for ycabled to go quiet.
    6. Expect no call to the cable after the removal's write.
    """
    dut.start()
    gate, hw_mark, cli_mark = hold_cli_call(dut, cable)
    mark = dut.monitor.mark()
    dut.remove("Ethernet0")
    maybe(lambda: removed(dut, mark))
    gate.set()
    removal = wait_until(lambda: removed(dut, mark), 10)[0]
    dut.monitor.wait_access(cli_mark, 10, kind="write", table="XCVRD_SHOW_HWMODE_DIR_RSP", key="Ethernet0")
    dut.monitor.wait_settled()
    late = [c.method for c in dut.hw.calls(hw_mark, port=1) if c.ts > removal.ts]
    assert not late, "calls to the cable after its removal was written: {}".format(late)
