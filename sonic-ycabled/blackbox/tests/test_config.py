"""Configuration inputs: Loopback3, GRPCCLIENT and grpc_secrets.json,
XCVRD_LOG, the VS platform, and syslog compatibility (HLD 3.3.10, 3.4.1; C5,
N5)."""

import pytest

from harness.certs import SOC_NAME, Pki
from harness.dut import GRPC_SECRETS
from harness.redisdb import HARNESS_ADDRESS
from harness.topology import (LOOPBACK3_UPPER, LOOPBACK3_LOWER, LOOPBACK3_UPPER_NIC, LOOPBACK3_LOWER_NIC,
                              VS_PLATFORM, Topology, soc_ip)
from harness.util import eventually

SOC = soc_ip("Ethernet0")
CERT_DIR = "/etc/sonic/bbcerts"


@pytest.mark.parametrize("loopback3,read_side", [
    (LOOPBACK3_UPPER, 1), (LOOPBACK3_LOWER, 0), (LOOPBACK3_UPPER_NIC, 1), (LOOPBACK3_LOWER_NIC, 0),
    ("10.212.64.9/32", -1), (None, -1), ("fc00::1/128", -1),
], ids=["upper", "lower", "upper-nic", "lower-nic", "other-address", "none", "ipv6-only"])
def test_read_side_from_loopback3(dut, loopback3, read_side):
    """ycabled takes its read side, this ToR's index in the SoC's port lists,
    from its Loopback3 address: 1 for the upper ToR's addresses, 0 for the
    lower ToR's, -1 for any other address or none. The read side decides which
    of the SoC's admin states is this ToR's and which the peer's, in
    HW_MUX_CABLE_TABLE, HW_MUX_CABLE_TABLE_PEER and the forwarding state probe,
    and the tor of the GracefulRestart request. Without a read side, both
    states are unknown.

    Cases: the upper and the lower ToR's addresses, 10.212.64.1 and 10.212.64.2
    (upper, lower), and the pair sonic-mgmt assigns, 10.1.0.38 and 10.1.0.39
    (upper-nic, lower-nic); another address (other-address); no Loopback3
    (none); an IPv6 address only (ipv6-only).

    Steps:
    1. Configure Ethernet0 as an active-active port with the case's Loopback3,
       and a SoC double at 192.168.0.3 whose admin state is standby for port id
       1, the upper ToR, and active for port id 0, the lower ToR.
    2. Start ycabled.
    3. Expect HW_MUX_CABLE_TABLE {state: this ToR's state, read_side: 0, 1 or
       -1, active_side: the same state}, and HW_MUX_CABLE_TABLE_PEER likewise
       with the peer's state: standby and active on the upper ToR, the reverse
       on the lower ToR, unknown without a read side.
    4. Send a forwarding state probe, and expect the same two states.
    5. Expect the first NotifyGracefulRestartStart request {tor: 1} for read
       side 0, and {tor: 0} otherwise, as Python sends it.
    """
    dut.configure(Topology(loopback3=loopback3).active_active("Ethernet0", soc_ipv4=SOC + "/32"))
    soc = dut.soc(SOC)
    soc.admin = [True, False]
    dut.start()
    if read_side == -1:
        self_state = peer_state = "unknown"
    else:
        self_state, peer_state = ("active", "standby") if read_side == 0 else ("standby", "active")
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0") == {
        "state": self_state, "read_side": str(read_side), "active_side": self_state}
    assert dut.state.get("HW_MUX_CABLE_TABLE_PEER", "Ethernet0") == {
        "state": peer_state, "read_side": str(read_side), "active_side": peer_state}
    assert dut.fwd_probe("Ethernet0") == {"response": self_state, "response_peer": peer_state}
    # GracefulRestart, as Python sends it: UPPER_TOR (1) for read side 0, the lower
    # ToR, and LOWER_TOR (0) otherwise.
    assert soc.wait_rpcs("NotifyGracefulRestartStart")[0].request == {"tor": 1 if read_side == 0 else 0}


@pytest.fixture
def pki(dut):
    p = Pki(CERT_DIR, soc_ips=[SOC])
    dut.files.append(CERT_DIR)
    return p


def secrets(pki, **config):
    return {"GRPCCLIENT": {
        "config": dict({"type": "secure", "auth_level": "server", "log_level": "info"}, **config),
        "certs": {"ca_crt": pki.paths["ca.crt"], "client_crt": pki.paths["client.crt"],
                  "client_key": pki.paths["client.key"], "grpc_ssl_credential": SOC_NAME}}}


def test_grpc_secrets_fill_config_db(dut, pki):
    """/etc/sonic/grpc_secrets.json fills GRPCCLIENT in CONFIG_DB, and the
    channel uses it.

    Steps:
    1. Configure Ethernet0 as an active-active port, its SoC at 192.168.0.3,
       and no GRPCCLIENT in CONFIG_DB. Write grpc_secrets.json with GRPCCLIENT
       config {type: secure, auth_level: server, log_level: info} and certs:
       the test PKI's CA, client certificate and key, and the SoC's name.
    2. Start a TLS SoC double at 192.168.0.3, and start ycabled.
    3. Expect CONFIG_DB GRPCCLIENT config and certs as in the file.
    4. Expect Ethernet0's state active, and QueryAdminForwardingPortState RPCs
       at the SoC: the channel is up, over TLS.
    """
    dut.configure(Topology().active_active("Ethernet0", soc_ipv4=SOC + "/32"))
    dut.write_file(GRPC_SECRETS, secrets(pki))
    soc = dut.soc(SOC, tls=pki.server_tls())
    dut.start()
    assert dut.config.get("GRPCCLIENT", "config") == secrets(pki)["GRPCCLIENT"]["config"]
    assert dut.config.get("GRPCCLIENT", "certs") == secrets(pki)["GRPCCLIENT"]["certs"]
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"
    assert soc.rpcs("QueryAdminForwardingPortState")


def test_grpc_secrets_do_not_override_config_db(dut, pki):
    """Only fields CONFIG_DB lacks are copied; null values are skipped.

    Steps:
    1. Configure Ethernet0 as an active-active port, its SoC at 192.168.0.3,
       with GRPCCLIENT config {type: insecure} in CONFIG_DB. Write
       grpc_secrets.json with GRPCCLIENT config {type: secure, auth_level:
       server, log_level: info} and certs: the test PKI's CA, client
       certificate and key, and the SoC's name, with log_level null instead.
    2. Start a plaintext SoC double at 192.168.0.3, and start ycabled.
    3. Expect CONFIG_DB GRPCCLIENT config {type: insecure, auth_level: server},
       the certs as in the file, and no write of the config's type or
       log_level.
    4. Expect Ethernet0's state active: the channel is still plaintext.
    """
    topo = Topology().active_active("Ethernet0", soc_ipv4=SOC + "/32")
    topo.grpcclient = {"config": {"type": "insecure"}}
    dut.configure(topo)
    data = secrets(pki, log_level=None)
    dut.write_file(GRPC_SECRETS, data)
    dut.soc(SOC)
    dut.start()
    assert dut.config.get("GRPCCLIENT", "config") == {"type": "insecure", "auth_level": "server"}
    assert dut.config.get("GRPCCLIENT", "certs") == data["GRPCCLIENT"]["certs"]
    # Still plaintext.
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"
    writes = {(a.key, a.field) for a in dut.writes(table="GRPCCLIENT")}
    assert ("config", "type") not in writes and ("config", "log_level") not in writes


def test_grpc_secrets_with_empty_section(dut, pki):
    """An empty config section in grpc_secrets.json adds no GRPCCLIENT config
    row. The certs are still copied, and without a config row the channel is
    plaintext.

    Steps:
    1. Configure Ethernet0 as an active-active port, its SoC at 192.168.0.3.
       Write grpc_secrets.json with GRPCCLIENT config {type: secure,
       auth_level: server, log_level: info} and certs: the test PKI's CA,
       client certificate and key, and the SoC's name, with an empty config
       section instead.
    2. Start a plaintext SoC double at 192.168.0.3, and start ycabled.
    3. Expect no GRPCCLIENT config row in CONFIG_DB, the certs as in the file,
       and Ethernet0's state active.
    """
    dut.configure(Topology().active_active("Ethernet0", soc_ipv4=SOC + "/32"))
    data = secrets(pki)
    data["GRPCCLIENT"]["config"] = {}
    dut.write_file(GRPC_SECRETS, data)
    dut.soc(SOC)
    dut.start()
    assert not dut.config.exists("GRPCCLIENT", "config")
    assert dut.config.get("GRPCCLIENT", "certs") == data["GRPCCLIENT"]["certs"]
    # No config row: plaintext.
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"


def test_grpc_secrets_without_active_active_ports(dut, pki):
    """grpc_secrets.json is applied to CONFIG_DB also on a device without
    active-active ports.

    Steps:
    1. Configure Ethernet0 as the only mux port, active-standby. Write
       grpc_secrets.json with GRPCCLIENT config {type: secure, auth_level:
       server, log_level: info} and certs: the test PKI's CA, client
       certificate and key, and the SoC's name.
    2. Start ycabled.
    3. Expect CONFIG_DB GRPCCLIENT certs as in the file.
    """
    dut.configure(Topology().active_standby("Ethernet0"))
    dut.write_file(GRPC_SECRETS, secrets(pki))
    dut.start()
    assert dut.config.get("GRPCCLIENT", "certs") == secrets(pki)["GRPCCLIENT"]["certs"]


def set_log_verbosity(dut, key, level):
    """Set XCVRD_LOG|<key> log_verbosity, then wait until ycabled has read
    the entry, if it reads it at all, and has gone quiet."""
    mark = dut.monitor.mark()
    dut.config.set("XCVRD_LOG", key, {"log_verbosity": level})
    eventually(lambda: dut.monitor.accesses(mark, kind="read", db="CONFIG_DB", table="XCVRD_LOG", key=key), 3)
    dut.monitor.wait_settled()


def probe_logs_debug(dut):
    """Whether a probe logs at DEBUG. ycabled logs the probe before it
    answers, and the syslog barrier makes sure those lines have arrived."""
    mark = dut.syslog.mark()
    dut.probe("Ethernet0")
    dut.syslog.barrier()
    return [m.text for m in dut.syslog.messages(mark, severity="DEBUG")]


def test_log_verbosity(dut):
    """XCVRD_LOG|Y_CABLE log_verbosity sets ycabled's log level while it runs:
    at debug, a probe logs its DEBUG lines; back at notice, it does not.

    Steps:
    1. Configure Ethernet0 as the only active-standby port.
    2. Start ycabled. Probe Ethernet0, and expect no DEBUG line in syslog.
    3. Set XCVRD_LOG|Y_CABLE log_verbosity=debug in CONFIG_DB, and wait until
       ycabled has read it and gone quiet.
    4. Probe Ethernet0, and expect a DEBUG line with 'received a probe for port
       status Ethernet0'.
    5. Set log_verbosity=notice the same way, probe again, and expect no DEBUG
       line.
    """
    dut.configure(Topology().active_standby("Ethernet0"))
    dut.start()
    assert not probe_logs_debug(dut)
    set_log_verbosity(dut, "Y_CABLE", "debug")
    assert any("received a probe for port status Ethernet0" in t for t in probe_logs_debug(dut))
    set_log_verbosity(dut, "Y_CABLE", "notice")
    assert not probe_logs_debug(dut)


@pytest.mark.known_issue("the fields of any XCVRD_LOG key, not only Y_CABLE, set the log level and "
                         "the telemetry flag")
def test_log_verbosity_other_key_is_ignored(dut):
    """Only XCVRD_LOG's Y_CABLE key sets ycabled's log level: debug under
    another key changes nothing.

    Steps:
    1. Configure Ethernet0 as the only active-standby port.
    2. Start ycabled.
    3. Set XCVRD_LOG|OTHER log_verbosity=debug in CONFIG_DB, and wait until
       ycabled has read it, if it does, and gone quiet.
    4. Probe Ethernet0, and expect no DEBUG line in syslog.
    """
    dut.configure(Topology().active_standby("Ethernet0"))
    dut.start()
    set_log_verbosity(dut, "OTHER", "debug")
    assert not probe_logs_debug(dut)


def test_active_active_on_vs(dut):
    """VS: no platform API; active-active cables are always present, so the
    port initializes and its toggles go through without any platform API call.

    Steps:
    1. Configure the KVM platform with Ethernet0 as an active-active port, and
       a SoC double at 192.168.0.3.
    2. Start ycabled.
    3. Expect Ethernet0's state active; toggle it to standby and expect
       standby.
    4. Expect no call to the fake cables or the fake platform API.
    """
    dut.configure(Topology(platform=VS_PLATFORM).active_active("Ethernet0", soc_ipv4=SOC + "/32"))
    dut.soc(SOC)
    dut.start()
    assert dut.state.get("HW_MUX_CABLE_TABLE", "Ethernet0")["state"] == "active"
    assert dut.toggle("Ethernet0", "standby")["state"] == "standby"
    assert not dut.hw.calls(op=None)


# sonic-mgmt's loganalyzer ignores these ERR messages by text, so they must
# keep it (loganalyzer_common_ignore.txt). The identity part of those rules is
# covered by test_lifecycle.py::test_syslog_identity.
def test_messages_matched_by_sonic_mgmt(dut):
    """An ERR message that sonic-mgmt's loganalyzer matches by text keeps its
    text and its severity: here, the one for a cable without a driver.

    Steps:
    1. Configure active-standby ports Ethernet0 and Ethernet4. Ethernet0's
       transceiver info names a vendor and model that sonic_y_cable does not
       map to a driver.
    2. Start ycabled.
    3. Expect 'Error: Could not get port instance for muxcable info for Y cable
       port Ethernet...' in the log, every such line at ERR.
    """
    dut.configure(Topology().active_standby("Ethernet0", "Ethernet4"))
    dut.hw.port(1).transceiver_info = {"manufacturer": "Nobody", "model": "X1"}
    dut.start()
    msgs = dut.logs(regex=r"Error: Could not get port instance for muxcable info for Y cable port Ethernet")
    assert msgs and {m.severity for m in msgs} == {"ERR"}


@pytest.mark.known_issue("redis connections grow with the number of active-active ports")
def test_redis_connections_bounded(dut):
    """The redis connections stay within a bound that does not depend on the
    number of ports: 48 per namespace, the Rust design's bound. Here there are
    16 active-active ports.

    Steps:
    1. Configure 16 active-active ports, Ethernet0 to Ethernet60, each with its
       own SoC double, at 192.168.0.3 to 192.168.0.33.
    2. Start ycabled, waiting up to 60 s until it is ready.
    3. Expect at most 48 redis client connections besides the suite's own.
    """
    topo = Topology(nports=16)
    ports = ["Ethernet{}".format(4 * i) for i in range(16)]
    for port in ports:
        topo.active_active(port, soc_ipv4=soc_ip(port) + "/32")
    dut.configure(topo)
    for port in ports:
        dut.soc(soc_ip(port))
    dut.start(timeout=60)
    clients = [c for c in dut.config.r.client_list() if not c["addr"].startswith(HARNESS_ADDRESS + ":")]
    assert len(clients) <= 48, len(clients)
