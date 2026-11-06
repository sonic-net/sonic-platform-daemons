"""What a test configures before ycabled starts: ports, mux cables, ToR
addresses, gRPC client settings, and what xcvrd and the hardware report."""

import os

from .util import clear_dir

PLATFORM = "x86_64-bbfake-r0"
VS_PLATFORM = "x86_64-kvm_x86_64-r0"
HWSKU = "BB-DualToR-SKU"
CONTAINER_PLATFORM_DIR = "/usr/share/sonic/platform"

# Loopback3 addresses ycabled maps to read_side 0 (upper ToR) and 1 (lower ToR).
LOOPBACK3_UPPER = "10.212.64.2/32"
LOOPBACK3_LOWER = "10.212.64.1/32"
LOOPBACK3_UPPER_NIC = "10.1.0.39/32"
LOOPBACK3_LOWER_NIC = "10.1.0.38/32"


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

    def __init__(self, nports=8, platform=PLATFORM, hwsku=HWSKU, loopback3=LOOPBACK3_UPPER):
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
