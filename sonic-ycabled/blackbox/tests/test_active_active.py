"""Active-active cables: toggles, probes and the SoC connection (HLD 3.4.3 to
3.4.6, 3.5.1; F3, C2, N1, N3)."""

import re
import time

import grpc
import pytest

from harness.certs import SOC_NAME, Pki
from harness.topology import LOOPBACK3_LOWER, LOOPBACK3_UPPER, Topology
from harness.util import TIMESTAMP_RE, stays_true, wait_until

SOC = "127.0.0.2"
SOC2 = "127.0.0.3"
CERT_DIR = "/etc/sonic/bbcerts"

# read_side: index of this ToR in the SoC's port lists.
READ_SIDES = [(LOOPBACK3_UPPER, 0), (LOOPBACK3_LOWER, 1)]


def aa(dut, loopback3=LOOPBACK3_UPPER, soc=True, **kw):
    dut.configure(Topology(loopback3=loopback3).active_active("Ethernet0", soc_ipv4=SOC + "/32", **kw))
    return dut.soc(SOC) if soc else None


@pytest.fixture
def pki(dut):
    p = Pki(CERT_DIR, soc_ips=[SOC])
    dut.files.append(CERT_DIR)
    return p


@pytest.mark.parametrize("loopback3,read_side", READ_SIDES)
@pytest.mark.parametrize("state", ["active", "standby"])
def test_toggle_self(dut, loopback3, read_side, state):
    soc = aa(dut, loopback3)
    soc.admin = [state != "active"] * 2
    dut.start()
    mark = soc.mark()
    row = dut.toggle("Ethernet0", state)
    assert row == {"state": state, "read_side": str(read_side), "active_side": state}
    rpcs = soc.rpcs("SetAdminForwardingPortState", mark)
    assert [r.request for r in rpcs] == [{"portid": [read_side], "state": [state == "active"]}]
    assert soc.admin[read_side] == (state == "active")
    metrics = dut.state.get("MUX_METRICS_TABLE", "Ethernet0")
    for edge in ("start", "end"):
        assert re.match(TIMESTAMP_RE, metrics["xcvrd_switch_self_{}_{}".format(state, edge)])


@pytest.mark.parametrize("loopback3,read_side", READ_SIDES)
@pytest.mark.parametrize("state", ["active", "standby"])
def test_toggle_peer(dut, loopback3, read_side, state):
    soc = aa(dut, loopback3)
    soc.admin = [state != "active"] * 2
    dut.start()
    self_before = dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")
    mark = soc.mark()
    row = dut.peer_toggle("Ethernet0", state)
    assert row == {"state": state, "read_side": str(read_side), "active_side": state}
    assert [r.request for r in soc.rpcs("SetAdminForwardingPortState", mark)] == [
        {"portid": [1 - read_side], "state": [state == "active"]}]
    assert soc.admin[1 - read_side] is (state == "active")
    metrics = dut.state.get("MUX_METRICS_TABLE", "Ethernet0")
    for edge in ("start", "end"):
        assert re.match(TIMESTAMP_RE, metrics["xcvrd_switch_peer_{}_{}".format(state, edge)])
    # The self side is not touched.
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == self_before


def test_toggle_peer_failure(dut):
    soc = aa(dut)
    dut.start()
    soc.script("SetAdminForwardingPortState", code=grpc.StatusCode.UNAVAILABLE)
    row = dut.peer_toggle("Ethernet0", "standby")
    assert row == {"state": "unknown", "read_side": "0", "active_side": "unknown"}
    assert set(dut.state.get("MUX_METRICS_TABLE", "Ethernet0")) == {
        "xcvrd_switch_peer_unknown_start", "xcvrd_switch_peer_unknown_end"}


def _reply_wrong_port(request, pb2):
    return pb2.AdminReply(portid=[7], state=[True])


def _reply_empty(request, pb2):
    return pb2.AdminReply()


@pytest.mark.parametrize("script", [
    {"code": grpc.StatusCode.UNAVAILABLE},
    {"code": grpc.StatusCode.INTERNAL},
    {"code": grpc.StatusCode.CANCELLED},
    {"code": grpc.StatusCode.INVALID_ARGUMENT},
    {"reply": _reply_wrong_port},
    {"reply": _reply_empty},
], ids=["unavailable", "internal", "cancelled", "invalid-argument", "wrong-port", "empty-reply"])
def test_toggle_failure(dut, script):
    soc = aa(dut)
    dut.start()
    soc.script("SetAdminForwardingPortState", **script)
    assert dut.toggle("Ethernet0", "standby") == {"state": "unknown", "read_side": "0", "active_side": "unknown"}
    assert set(dut.state.get("MUX_METRICS_TABLE", "Ethernet0")) == {
        "xcvrd_switch_self_unknown_start", "xcvrd_switch_self_unknown_end"}


def test_toggle_soc_hangs(dut):
    """The SoC does not answer: the request fails at the 0.5 s deadline."""
    soc = aa(dut)
    dut.start()
    soc.script("SetAdminForwardingPortState", delay=3)
    t0 = time.monotonic()
    assert dut.toggle("Ethernet0", "standby")["state"] == "unknown"
    assert 0.4 <= time.monotonic() - t0 < 2.0


def test_toggle_ignores_other_states(dut):
    soc = aa(dut)
    dut.start()
    mark = dut.monitor.mark()
    soc_mark = soc.mark()
    dut.appl.set("HW_MUX_CABLE_TABLE", "Ethernet0", {"state": "init"})
    assert stays_true(lambda: not dut.writes(since=mark, table="HW_MUX_CABLE_TABLE"), 1.5)
    assert not soc.rpcs("SetAdminForwardingPortState", soc_mark)
    # The toggle thread is still serving.
    assert dut.toggle("Ethernet0", "standby")["state"] == "standby"


@pytest.mark.parametrize("loopback3,read_side", READ_SIDES)
def test_forwarding_state_probe(dut, loopback3, read_side):
    soc = aa(dut, loopback3)
    soc.admin = [True, False]
    dut.start()
    mark = soc.mark()
    db_mark = dut.monitor.mark()
    row = dut.fwd_probe("Ethernet0")
    self_state, peer_state = ("active", "standby") if read_side == 0 else ("standby", "active")
    assert row == {"response": self_state, "response_peer": peer_state}
    assert [r.request for r in soc.rpcs("QueryAdminForwardingPortState", mark)] == [
        {"portid": [0, 1], "state": [False, False]}]
    # A probe only answers; HW_MUX_CABLE_TABLE is not touched.
    assert not dut.writes(since=db_mark, table="HW_MUX_CABLE_TABLE")


def _reply_short(request, pb2):
    return pb2.AdminReply(portid=[0], state=[True])


@pytest.mark.parametrize("script", [{"code": grpc.StatusCode.UNAVAILABLE}, {"reply": _reply_short},
                                    {"delay": 3}], ids=["unavailable", "short-reply", "hang"])
def test_forwarding_state_probe_failure(dut, script):
    soc = aa(dut)
    dut.start()
    soc.script("QueryAdminForwardingPortState", **script)
    assert dut.fwd_probe("Ethernet0") == {"response": "unknown", "response_peer": "unknown"}


def test_forwarding_state_probe_ignores_other_commands(dut):
    aa(dut)
    dut.start()
    mark = dut.monitor.mark()
    dut.appl.set("FORWARDING_STATE_COMMAND", "Ethernet0", {"command": "reset"})
    assert stays_true(lambda: not dut.writes(since=mark, table="FORWARDING_STATE_RESPONSE"), 1.5)
    # The probe thread is still serving.
    assert dut.fwd_probe("Ethernet0")["response"] == "active"


def test_probes_of_other_cable_types_do_not_block(dut):
    """A forwarding probe or peer toggle for an active-standby port is
    skipped, and the active-active requests queued with it still run.

    Python stops reading the table at the first such port (`break`, not
    `continue`: HLD 3.8 issue 6) and leaves the entries after it in the
    subscriber's buffer. They still run, one more pass of its loop later: a
    subscriber that swsscommon's Select returns with more than one entry
    pending stays in Select's ready set (SubscriberStateTable::hasCachedData),
    and the next select() returns it without waiting."""
    topo = Topology().active_standby("Ethernet0").active_active("Ethernet4", soc_ipv4=SOC + "/32")
    dut.configure(topo)
    dut.soc(SOC)
    dut.start()
    mark = dut.monitor.mark()
    with dut.appl.r.pipeline(transaction=True) as p:
        p.hset("FORWARDING_STATE_COMMAND:Ethernet0", "command", "probe")
        p.hset("FORWARDING_STATE_COMMAND:Ethernet4", "command", "probe")
        p.hset("HW_FORWARDING_STATE_PEER:Ethernet0", "state", "active")
        p.hset("HW_FORWARDING_STATE_PEER:Ethernet4", "state", "active")
        p.execute()
    dut.monitor.wait_access(mark, 3, kind="write", table="FORWARDING_STATE_RESPONSE", key="Ethernet4")
    dut.monitor.wait_access(mark, 3, kind="write", table="HW_MUX_CABLE_TABLE_PEER", key="Ethernet4")
    assert not dut.monitor.accesses(mark, kind="write", key="Ethernet0")


def test_port_without_soc_address(dut):
    """An active-active port without a SoC address is not served: it gets no
    HW_MUX_CABLE_TABLE entry, and its requests are ignored."""
    topo = Topology().active_standby("Ethernet0").active_active("Ethernet4")
    dut.configure(topo)
    dut.start()
    assert not dut.state.exists("HW_MUX_CABLE_TABLE", "Ethernet4")
    assert dut.logs(contains="No SoC IP address found for gRPC channel setup on port Ethernet4")
    mark = dut.monitor.mark()
    dut.appl.set("HW_MUX_CABLE_TABLE", "Ethernet4", {"state": "standby"})
    dut.appl.set("FORWARDING_STATE_COMMAND", "Ethernet4", {"command": "probe"})
    assert stays_true(lambda: not dut.writes(since=mark, key="Ethernet4"), 1.5)
    # The toggle thread is still serving.
    assert dut.toggle("Ethernet0", "standby")["state"] == "standby"


def test_cable_absent_at_startup(dut):
    """No channel is set up for an absent cable; the first toggle sets it up."""
    soc = aa(dut)
    dut.hw.port(1).present = False
    dut.start()
    assert dut.logs(contains="DAC cable not present while Channel setup Port Ethernet0")
    unknown = {"state": "unknown", "read_side": "0", "active_side": "unknown"}
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == unknown
    assert dut.state.get("HW_MUX_CABLE_TABLE_PEER", "Ethernet0") == unknown
    assert not soc.rpcs("QueryAdminForwardingPortState")
    assert dut.toggle("Ethernet0", "standby")["state"] == "standby"
    assert soc.admin[0] is False


@pytest.mark.parametrize("fields", [{"soc_ipv6": "::1/128"}, {"soc_ipv4": SOC + "/32", "soc_ipv6": "fd00::99/128"}],
                         ids=["ipv6-only", "ipv4-preferred"])
def test_soc_address_family(dut, fields):
    port = Topology().active_active("Ethernet0", **fields)
    dut.configure(port)
    soc = dut.soc("::1" if "soc_ipv4" not in fields else SOC)
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"
    assert soc.rpcs("QueryAdminForwardingPortState")


def test_invalid_soc_address(dut):
    topo = Topology().active_standby("Ethernet0").active_active("Ethernet4", soc_ipv4="10.1.1/32")
    dut.configure(topo)
    dut.start()
    assert dut.logs(contains="Invalid soc_ipv4 address 10.1.1/32")
    assert not dut.state.exists("HW_MUX_CABLE_TABLE", "Ethernet4")


def test_soc_comes_up_later(dut):
    soc = aa(dut, soc=False)
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == {
        "state": "unknown", "read_side": "0", "active_side": "unknown"}
    assert dut.fwd_probe("Ethernet0")["response"] in ("unknown", "failure")
    soc = dut.soc(SOC)
    # gRPC reconnects with backoff; linkmgrd keeps probing.
    wait_until(lambda: dut.fwd_probe("Ethernet0")["response"] == "active", 15)
    assert dut.toggle("Ethernet0", "standby")["state"] == "standby"
    assert soc.admin[0] is False


def test_connection_loss_reports_failure(dut):
    """C2: when the channel fails, FORWARDING_STATE_RESPONSE gets
    response=failure, which linkmgrd treats as a connection failure."""
    soc = aa(dut)
    dut.start()
    assert dut.fwd_probe("Ethernet0")["response"] == "active"
    mark = dut.monitor.mark()
    soc.stop()
    writes = dut.monitor.wait_access(mark, 10, kind="write", table="FORWARDING_STATE_RESPONSE", key="Ethernet0")
    assert [(a.field, a.value) for a in writes][:1] == [("response", "failure")]
    soc.start()
    wait_until(lambda: dut.fwd_probe("Ethernet0")["response"] == "active", 15)


def tls_config(dut, pki, auth_level="server", ca="ca.crt", client=True, target=SOC_NAME, certs_row=True):
    """`client`: True for a client certificate and key, "cert-only", or False."""
    certs = {"ca_crt": pki.paths[ca] if ca in pki.paths else ca}
    if target:
        certs["grpc_ssl_credential"] = target
    if client:
        certs["client_crt"] = pki.paths["client.crt"]
    if client is True:
        certs["client_key"] = pki.paths["client.key"]
    dut.topo.grpcclient = {"config": {"type": "secure", "auth_level": auth_level}}
    if certs_row:
        dut.topo.grpcclient["certs"] = certs
    dut.configure(dut.topo)


@pytest.mark.parametrize("auth_level,require_client_auth", [("server", False), ("mutual", True)])
def test_tls(dut, pki, auth_level, require_client_auth):
    aa(dut, soc=False)
    tls_config(dut, pki, auth_level)
    soc = dut.soc(SOC, tls=pki.server_tls(require_client_auth))
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"
    assert dut.toggle("Ethernet0", "standby")["state"] == "standby"
    assert soc.admin[0] is False


@pytest.mark.parametrize("case", ["wrong-ca", "server-needs-client-cert", "wrong-target-name"])
def test_tls_handshake_failure(dut, pki, case):
    aa(dut, soc=False)
    tls_config(dut, pki, "server", ca="other-ca.crt" if case == "wrong-ca" else "ca.crt",
               target="other.bb.test" if case == "wrong-target-name" else SOC_NAME)
    dut.soc(SOC, tls=pki.server_tls(require_client_auth=case == "server-needs-client-cert"))
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "unknown"
    assert dut.toggle("Ethernet0", "standby")["state"] == "unknown"


@pytest.mark.parametrize("case", ["missing-ca-file", "no-target-name", "mutual-without-client-cert",
                                  "mutual-without-client-key", "no-certs-row"])
def test_tls_misconfiguration(dut, pki, case):
    """No channel can be set up: the port is reported unknown, and every
    toggle or probe tries to set up the channel again."""
    aa(dut, soc=False)
    client = {"mutual-without-client-cert": False, "mutual-without-client-key": "cert-only"}.get(case, True)
    tls_config(dut, pki, "mutual" if case.startswith("mutual") else "server",
               ca="/etc/sonic/bbcerts/missing.crt" if case == "missing-ca-file" else "ca.crt",
               client=client, target=None if case == "no-target-name" else SOC_NAME,
               certs_row=case != "no-certs-row")
    soc = dut.soc(SOC, tls=pki.server_tls())
    dut.tolerate_python_failure(crash=True)  # 3.8-20, see test_tls_misconfiguration_keeps_running
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == {
        "state": "unknown", "read_side": "0", "active_side": "unknown"}
    assert dut.toggle("Ethernet0", "standby")["state"] == "unknown"
    assert set(dut.state.get("MUX_METRICS_TABLE", "Ethernet0")) == {
        "xcvrd_switch_self_unknown_start", "xcvrd_switch_self_unknown_end"}
    assert dut.fwd_probe("Ethernet0") == {"response": "unknown", "response_peer": "unknown"}
    assert not soc.rpcs()
    # Each request sets the channel up again from CONFIG_DB: with a usable
    # configuration, the next toggle goes through.
    dut.config.set("GRPCCLIENT", "config", {"type": "secure", "auth_level": "server"})
    dut.config.set("GRPCCLIENT", "certs", {"ca_crt": pki.paths["ca.crt"], "grpc_ssl_credential": SOC_NAME,
                                           "client_crt": pki.paths["client.crt"],
                                           "client_key": pki.paths["client.key"]})
    assert dut.toggle("Ethernet0", "standby")["state"] == "standby"
    assert soc.admin[0] is False


@pytest.mark.slow
@pytest.mark.known_issue("3.8-20", "without a usable TLS channel the GracefulRestart task dies on a None "
                                    "channel, and ycabled kills itself within 60 s")
def test_tls_misconfiguration_keeps_running(dut, pki):
    aa(dut, soc=False)
    tls_config(dut, pki, "server", target=None)
    dut.tolerate_python_failure(crash=True, exit=True)
    dut.start()
    assert dut.proc.wait_exit(75) is None


@pytest.mark.known_issue("3.8-7", "auth_level 'client' (the YANG value) is not recognized; only 'mutual' "
                                   "enables mutual TLS")
def test_tls_auth_level_client(dut, pki):
    """sonic-grpcclient.yang allows auth_level server or client:
    https://github.com/sonic-net/sonic-buildimage/blob/master/src/sonic-yang-models/yang-models/sonic-grpcclient.yang"""
    aa(dut, soc=False)
    tls_config(dut, pki, "client")
    dut.soc(SOC, tls=pki.server_tls(require_client_auth=True))
    dut.tolerate_python_failure(crash=True)
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"


@pytest.mark.parametrize("config", [{"auth_level": "server"}, {"type": "insecure", "auth_level": "server"}],
                         ids=["no-type", "insecure"])
def test_tls_type_insecure_and_missing(dut, config):
    """GRPCCLIENT config without type, or type insecure: plaintext."""
    aa(dut, soc=False)
    dut.topo.grpcclient = {"config": config}
    dut.configure(dut.topo)
    soc = dut.soc(SOC)
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"
    assert soc.rpcs("QueryAdminForwardingPortState")


@pytest.mark.parametrize("loopback3,tor", [(LOOPBACK3_UPPER, 1), (LOOPBACK3_LOWER, 0)])
def test_graceful_restart_subscription(dut, loopback3, tor):
    """One GracefulRestart stream per SoC: UPPER_TOR (1) from the upper ToR,
    LOWER_TOR (0) from the lower ToR. When the stream ends ycabled subscribes
    again after the `period` of the last response."""
    soc = aa(dut, loopback3)
    soc.gr_responses = [{"period": 1, "guid": "bb-1", "msgtype": 1, "notifytype": 2}]
    soc.gr_hold = False
    dut.start()
    first, second = soc.wait_rpcs("NotifyGracefulRestartStart", 2, timeout=10)[:2]
    assert first.request == second.request == {"tor": tor}
    assert 0.8 <= second.ts - first.ts < 4


def test_graceful_restart_held_stream(dut):
    soc = aa(dut)
    dut.start()
    soc.wait_rpcs("NotifyGracefulRestartStart", 1)
    time.sleep(3)
    assert len(soc.rpcs("NotifyGracefulRestartStart")) == 1


@pytest.mark.slow
@pytest.mark.parametrize("case", ["no-response", "error"])
def test_graceful_restart_retry_after_20s(dut, case):
    soc = aa(dut)
    soc.gr_hold = False
    if case == "error":
        soc.script("NotifyGracefulRestartStart", code=grpc.StatusCode.UNAVAILABLE)
    dut.start()
    first, second = soc.wait_rpcs("NotifyGracefulRestartStart", 2, timeout=30)[:2]
    assert 19 <= second.ts - first.ts < 25


def test_graceful_restart_for_every_soc_address(dut):
    """Python subscribes for every MUX_CABLE port with a SoC address, also
    active-standby ones, once any port is active-active."""
    topo = Topology().active_standby("Ethernet0", soc_ipv4=SOC2 + "/32").active_active("Ethernet4", soc_ipv4=SOC + "/32")
    dut.configure(topo)
    dut.soc(SOC)
    as_soc = dut.soc(SOC2)
    dut.start()
    as_soc.wait_rpcs("NotifyGracefulRestartStart", 1)
    assert not as_soc.rpcs("QueryAdminForwardingPortState")


@pytest.mark.known_issue("N1", "one thread serves every port: a SoC that does not answer delays the "
                                "toggles of the other ports by up to 0.5 s each")
def test_slow_soc_does_not_delay_other_ports(dut):
    """Every port's SoC hangs, so the order the ports are served in does not
    matter: in parallel, all toggles end at the 0.5 s deadline; one after
    another, the last ends after 2 s."""
    ports = ["Ethernet{}".format(4 * i) for i in range(4)]
    topo = Topology()
    for i, port in enumerate(ports):
        topo.active_active(port, soc_ipv4="127.0.2.{}/32".format(i + 1))
    dut.configure(topo)
    socs = [dut.soc("127.0.2.{}".format(i + 1)) for i in range(len(ports))]
    dut.start()
    for soc in socs:
        soc.script("SetAdminForwardingPortState", delay=3)
    mark = dut.monitor.mark()
    t0 = time.monotonic()
    with dut.appl.r.pipeline(transaction=True) as p:
        for port in ports:
            p.hset("HW_MUX_CABLE_TABLE:" + port, "state", "standby")
        p.execute()
    for port in ports:
        dut.monitor.wait_access(mark, 10, kind="write", db="STATE_DB", table="HW_MUX_CABLE_TABLE", key=port)
    assert time.monotonic() - t0 < 1.2


@pytest.mark.known_issue("3.8-30", "without a usable Loopback3 address read_side is -1, and toggles still go to "
                                    "the SoC, with portid [-1] for this ToR and [2] for the peer")
def test_no_toggle_without_read_side(dut):
    """Without Loopback3, ycabled does not know which ToR it is: it toggles
    neither side on the SoC, and answers unknown."""
    soc = aa(dut, loopback3=None)
    dut.start()
    soc_mark = soc.mark()
    assert dut.toggle("Ethernet0", "standby")["state"] == "unknown"
    assert dut.peer_toggle("Ethernet0", "standby")["state"] == "unknown"
    assert not soc.rpcs("SetAdminForwardingPortState", soc_mark)


@pytest.mark.known_issue("N3", "ports are initialized one after another; every unreachable SoC adds 0.5 s")
def test_parallel_startup_with_unreachable_socs(dut):
    topo = Topology()
    for i in range(4):
        topo.active_active("Ethernet{}".format(4 * i), soc_ipv4="127.0.1.{}/32".format(i + 1))
    dut.configure(topo)
    for i in range(4):
        dut.blackhole("127.0.1.{}".format(i + 1))
    dut.start(ready=False)
    # From ycabled's first DB access (redis's clock), so that the
    # interpreter's start does not count, to its last port's first state.
    first = wait_until(lambda: dut.monitor.ops(dut.start_mark), 15)[0]

    def published():
        firsts = {}
        for a in dut.writes(since=dut.start_mark, table="HW_MUX_CABLE_TABLE", field="state"):
            firsts.setdefault(a.key, a)
        return firsts if len(firsts) == 4 else None
    elapsed = max(a.ts for a in wait_until(published, 15).values()) - first.ts
    assert elapsed < 1.5, elapsed
