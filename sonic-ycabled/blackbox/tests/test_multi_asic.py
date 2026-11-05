"""Multi-ASIC: one redis instance per ASIC namespace (HLD 2.1 F1)."""

import json
import os
import shutil
import subprocess
import sys

import pytest

from harness import contract
from harness.monitor import Monitor
from harness.redisdb import Dbs, RedisServer
from harness.topology import CONTAINER_PLATFORM_DIR, HWSKU, PLATFORM, Topology, transceiver_info_row
from harness.util import wait_until
from harness.ycabled import YcabledProcess

# What a daemon that supports multi-ASIC does first, in ycabled's environment.
ENV_CHECK = """
from swsscommon import swsscommon
swsscommon.SonicDBConfig.initializeGlobalConfig()
from sonic_py_common import multi_asic, device_info
from sonic_platform_base.sonic_sfp.sfputilhelper import SfpUtilHelper
namespaces = multi_asic.get_front_end_namespaces()
sfputil = SfpUtilHelper()
sfputil.read_all_porttab_mappings(device_info.get_paths_to_platform_and_hwsku_dirs()[1], 2)
state = swsscommon.DBConnector("STATE_DB", 0, True, "asic1")
print(namespaces, {p: sfputil.get_asic_id_for_logical_port(p) for p in sfputil.logical},
      swsscommon.Table(state, "TRANSCEIVER_INFO").getKeys())
"""

SONIC_DB_DIR = "/var/run/redis/sonic-db"
NAMESPACES = {"asic0": ("Ethernet0", 1, 6380), "asic1": ("Ethernet4", 2, 6381)}


def _database_config(base, port, unix_socket):
    config = json.load(open(os.path.join(SONIC_DB_DIR, "database_config.json")))
    config["INSTANCES"] = {"redis": {"hostname": "127.0.0.1", "port": port, "unix_socket_path": unix_socket}}
    return config


@pytest.fixture
def multi_asic(dut):
    """Two front-end ASICs, one mux port each."""
    servers = {}
    monitors = {}
    problems = []
    try:
        topo = Topology()
        topo.active_standby("Ethernet0", "Ethernet4")
        topo.mux = {}  # MUX_CABLE lives in the namespaces
        dut.configure(topo)
        with open(os.path.join(CONTAINER_PLATFORM_DIR, "asic.conf"), "w") as f:
            f.write("NUM_ASIC=2\n")
        includes = [{"include": "../../redis/sonic-db/database_config.json"}]
        for ns, (port_name, index, tcp_port) in NAMESPACES.items():
            n = ns[-1]
            run_dir = "/var/run/redis{}".format(n)
            os.makedirs(os.path.join(run_dir, "sonic-db"), exist_ok=True)
            with open(os.path.join(run_dir, "sonic-db", "database_config.json"), "w") as f:
                json.dump(_database_config(SONIC_DB_DIR, tcp_port, run_dir + "/redis.sock"), f)
            includes.append({"namespace": ns, "include": "../../redis{}/sonic-db/database_config.json".format(n)})
            server = RedisServer(os.path.join(dut.env.workdir, "redis-" + ns), unix_socket=run_dir + "/redis.sock",
                                 port=tcp_port)
            servers[ns] = server
            server.start()
            # What the daemon does in the namespace, for the DB contract.
            monitors[ns] = Monitor(port=tcp_port)
            monitors[ns].start()
            dbs = Dbs(server)
            dbs.config.r.set("CONFIG_DB_INITIALIZED", "1")
            dbs.config.set("DEVICE_METADATA", "localhost", dict(topo.device_metadata, sub_role="FrontEnd",
                                                                asic_name=ns))
            dbs.config.set("PORT", port_name, {"index": index, "lanes": str(4 * (index - 1)),
                                               "alias": "etp{}".format(index), "speed": 100000,
                                               "asic_port_name": "Eth{}-ASIC{}".format(index, n), "role": "Ext"})
            dbs.config.set("MUX_CABLE", port_name, {"state": "auto"})
            dbs.config.set("LOOPBACK_INTERFACE", "Loopback3|10.212.64.2/32", {})
            dbs.state.set("TRANSCEIVER_INFO", port_name, transceiver_info_row(dut.hw.port(index)))
            asic_dir = os.path.join(CONTAINER_PLATFORM_DIR, HWSKU, n)
            os.makedirs(asic_dir)
            with open(os.path.join(asic_dir, "port_config.ini"), "w") as f:
                f.write("# name lanes alias index speed\n{} {} etp{} {} 100000\n".format(
                    port_name, 4 * (index - 1), index, index))
        with open(os.path.join(SONIC_DB_DIR, "database_global.json"), "w") as f:
            json.dump({"INCLUDES": includes, "VERSION": "1.0"}, f)
        yield {ns: Dbs(server) for ns, server in servers.items()}
    finally:
        # Stop ycabled before its namespace DBs go away. A daemon that exited on
        # its own is not marked stopped, so the dut teardown still reports it.
        if dut.proc is not None and dut.proc.running():
            dut.stopped = True
            dut.proc.kill()
        for ns, monitor in monitors.items():
            if monitor.error is not None:
                problems.append("{}: redis MONITOR stopped recording: {!r}".format(ns, monitor.error))
            else:
                accesses = monitor.accesses(0)
                dut.env.extra_accesses.extend(accesses)
                if not dut.allow_contract_violations:
                    problems += ["DB contract, {}: {}".format(ns, v) for v in contract.violations(accesses)]
            monitor.stop()
        for server in servers.values():
            server.stop()
        for path in (os.path.join(SONIC_DB_DIR, "database_global.json"),
                     os.path.join(CONTAINER_PLATFORM_DIR, "asic.conf")):
            if os.path.exists(path):
                os.unlink(path)
        for n in ("0", "1"):
            # The directories are the suite's (env.SYSTEM_DIRS); their content is the test's.
            shutil.rmtree("/var/run/redis{}/sonic-db".format(n), ignore_errors=True)
    if problems:
        pytest.fail("\n".join(problems), pytrace=False)


def test_multi_asic_environment(dut, multi_asic):
    """The two-ASIC environment works for a daemon that initializes the
    global DB config first."""
    env = YcabledProcess(dut.env, PLATFORM).process_env()
    out = subprocess.check_output([sys.executable, "-c", ENV_CHECK], env=env, cwd="/", text=True)
    assert out.strip().splitlines()[-1] == "['asic0', 'asic1'] {'Ethernet0': 0, 'Ethernet4': 1} ('Ethernet4',)"


@pytest.mark.known_issue("3.8-23", "ycabled cannot start on a multi-ASIC platform: it opens namespace "
                                    "connections before initializing the global DB config, and resolves "
                                    "the default namespace '' to an ASIC index")
def test_multi_asic(dut, multi_asic):
    dut.tolerate_python_failure(crash=True, exit=True)
    dut.start(ready=False)
    namespaces = multi_asic

    def initialized():
        assert dut.proc.running(), dut.proc.output()[-2000:]
        return all(dbs.state.exists("HW_MUX_CABLE_TABLE", NAMESPACES[ns][0]) for ns, dbs in namespaces.items())

    wait_until(initialized, 15)
    assert namespaces["asic1"].state.get("HW_MUX_CABLE_TABLE", "Ethernet4")["state"] == "active"
    namespaces["asic1"].appl.set("HW_MUX_CABLE_TABLE", "Ethernet4", {"state": "standby"})
    wait_until(lambda: namespaces["asic1"].state.field("HW_MUX_CABLE_TABLE", "Ethernet4", "state") == "standby", 5)
