"""What a test configures before ycabled starts: ports, mux cables, ToR
addresses, gRPC client settings, and what xcvrd and the hardware report."""

import ipaddress
import os

from .util import clear_dir

PLATFORM = "x86_64-bbfake-r0"
VS_PLATFORM = "x86_64-kvm_x86_64-r0"
HWSKU = "BB-DualToR-SKU"
CONTAINER_PLATFORM_DIR = "/usr/share/sonic/platform"

# Loopback3 addresses ycabled maps to read_side 1 (upper ToR) and 0 (lower ToR):
# Python ycabled's LOOPBACK_INTERFACE_T0 and LOOPBACK_INTERFACE_LT0. The _NIC pair is
# what sonic-mgmt gives dual ToR testbeds, 10.1.0.38 to the upper ToR (the first DUT).
LOOPBACK3_UPPER = "10.212.64.1/32"
LOOPBACK3_LOWER = "10.212.64.2/32"
LOOPBACK3_UPPER_NIC = "10.1.0.38/32"
LOOPBACK3_LOWER_NIC = "10.1.0.39/32"

# sonic-mgmt's dual ToR VLAN, Vlan1000, whose hosts are the servers and the SoCs
# behind the mux cables. With active-active cables, the n-th port of the VLAN
# (from 0) has its server at the VLAN's address + 2n + 1 and its SoC at + 2n + 2
# (generate_mux_cable_facts in sonic-mgmt's ansible/module_utils/dualtor_utils.py).
# The suite routes both subnets to lo (env.prepare_system()).
VLAN_V4 = ipaddress.ip_interface("192.168.0.1/21")
VLAN_V6 = ipaddress.ip_interface("fc02:1000::1/64")


def soc_ip(port, ipv6=False):
    """The SoC address sonic-mgmt gives the active-active cable of `port`,
    EthernetN, the (N/4)-th port of a Topology: 192.168.0.3 for Ethernet0,
    192.168.0.5 for Ethernet4. sonic-mgmt gives SoCs no IPv6 address; the
    suite's has the same host number in the IPv6 subnet, fc02:1000::3 for
    Ethernet0."""
    n = int(port[len("Ethernet"):]) // 4
    return str((VLAN_V6 if ipv6 else VLAN_V4).ip + 2 * n + 2)


class MuxPort(object):
    def __init__(self, name, cable_type="active-standby", state="auto", soc_ipv4=None, soc_ipv6=None,
                 server_ipv4=None, extra=None, configured=True):
        self.name = name
        self.cable_type = cable_type
        self.state = state
        self.soc_ipv4 = soc_ipv4
        self.soc_ipv6 = soc_ipv6
        self.server_ipv4 = server_ipv4
        self.extra = dict(extra or {})
        self.configured = configured

    def fields(self):
        f = {"state": self.state} if self.state is not None else {}
        if self.cable_type == "active-active":
            f["cable_type"] = "active-active"
        if self.server_ipv4:
            f["server_ipv4"] = self.server_ipv4
        if self.soc_ipv4:
            f["soc_ipv4"] = self.soc_ipv4
        if self.soc_ipv6:
            f["soc_ipv6"] = self.soc_ipv6
        f.update(self.extra)
        return f


class Topology(object):
    """A dual ToR device. `ports` are all front panel ports, in port config
    order; `mux` the MUX_CABLE entries."""

    def __init__(self, nports=8, platform=PLATFORM, hwsku=HWSKU, loopback3=LOOPBACK3_LOWER):
        self.platform = platform
        self.hwsku = hwsku
        self.ports = [("Ethernet{}".format(4 * i), i + 1) for i in range(nports)]
        self.mux = {}
        self.loopback3 = loopback3
        self.grpcclient = {}
        self.xcvrd_log = None
        self.device_metadata = {"platform": platform, "hwsku": hwsku, "subtype": "DualToR",
                                "type": "ToRRouter", "hostname": "bb-tor"}
        # Ports whose cable xcvrd has published in STATE_DB TRANSCEIVER_INFO.
        self.transceiver_info = None
        self.config_db_initialized = True
        self.port_table = True

    def cable_types(self):
        return {name: port.cable_type for name, port in self.mux.items()}

    def index(self, name):
        return dict(self.ports)[name]

    def add_mux(self, name, **kw):
        # A test that wants MUX_CABLE to name an unknown port writes the row itself.
        if name not in dict(self.ports):
            raise ValueError("{} is not a port of the topology".format(name))
        self.mux[name] = MuxPort(name, **kw)
        return self.mux[name]

    def active_standby(self, *names, **kw):
        for name in names:
            self.add_mux(name, **kw)
        return self

    def active_active(self, name, soc_ipv4=None, soc_ipv6=None, **kw):
        self.add_mux(name, cable_type="active-active", soc_ipv4=soc_ipv4, soc_ipv6=soc_ipv6, **kw)
        return self

    def write_port_config(self):
        hwsku_dir = os.path.join(CONTAINER_PLATFORM_DIR, self.hwsku)
        clear_dir(CONTAINER_PLATFORM_DIR)
        os.makedirs(hwsku_dir)
        with open(os.path.join(hwsku_dir, "port_config.ini"), "w") as f:
            f.write("# name lanes alias index speed\n")
            for name, index in self.ports:
                lanes = ",".join(str(4 * (index - 1) + i) for i in range(4))
                f.write("{} {} etp{} {} 100000\n".format(name, lanes, index, index))

    def apply(self, dbs, hw):
        self.write_port_config()
        cfg = dbs.config
        if self.config_db_initialized:
            cfg.r.set("CONFIG_DB_INITIALIZED", "1")
        if self.device_metadata is not None:
            cfg.set("DEVICE_METADATA", "localhost", self.device_metadata)
        for name, index in self.ports:
            lanes = ",".join(str(4 * (index - 1) + i) for i in range(4))
            if self.port_table:
                cfg.set("PORT", name, {"index": index, "lanes": lanes, "alias": "etp{}".format(index),
                                       "speed": 100000, "admin_status": "up"})
            hw.add_port(index)
        for port in self.mux.values():
            if port.configured:
                cfg.set("MUX_CABLE", port.name, port.fields())
        if self.loopback3:
            cfg.set("LOOPBACK_INTERFACE", "Loopback3", {})
            cfg.set("LOOPBACK_INTERFACE", "Loopback3|{}".format(self.loopback3), {})
        for key, fields in self.grpcclient.items():
            cfg.set("GRPCCLIENT", key, fields)
        if self.xcvrd_log is not None:
            cfg.set("XCVRD_LOG", "Y_CABLE", self.xcvrd_log)

    def publish_transceivers(self, dbs, hw):
        """Write STATE_DB TRANSCEIVER_INFO for the cables present now, as xcvrd does."""
        present = self.transceiver_info
        if present is None:
            present = [name for name in self.mux if hw.port(self.index(name)).present]
        for name in present:
            dbs.state.set("TRANSCEIVER_INFO", name, transceiver_info_row(hw.port(self.index(name))))


def transceiver_info_row(model):
    info = model.transceiver_info or {}
    return {"type": info.get("type", "QSFP28 or later"), "manufacturer": info.get("manufacturer", "N/A"),
            "model": info.get("model", "N/A"), "serial": info.get("serial", "N/A")}
