"""Everything the suite runs once per session."""

import json
import os
import shutil
import subprocess
import sys
from collections import namedtuple

from .fake_hw import MODEL, VENDOR, FakeHardware
from .monitor import Monitor
from .redisdb import TCP_PORT, UNIX_SOCKET, Dbs, RedisServer
from .syslog_sink import DEV_LOG, SyslogSink
from .util import DB_IDS

BLACKBOX_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.dirname(BLACKBOX_DIR)

DATABASE_CONFIG = "/var/run/redis/sonic-db/database_config.json"
# Where the suite writes outside its workdir, besides /dev/log. It makes them
# its own at the start: as root, or with sudo.
SYSTEM_DIRS = ("/var/run/redis", "/var/run/redis0", "/var/run/redis1", "/etc/sonic", "/usr/share/sonic/platform",
               "/usr/share/sonic/firmware")

# Where Python ycabled's coverage data goes. own_report: the suite reports it
# (--bb-coverage); otherwise pytest-cov combines it into the report of the run.
CoverageTarget = namedtuple("CoverageTarget", "data_file branch own_report")


def check_prerequisites():
    """What keeps the suite from running here, or None."""
    if shutil.which("redis-server") is None:
        return "redis-server is not installed"
    if os.geteuid() != 0 and (shutil.which("sudo") is None or subprocess.call(
            ["sudo", "-n", "true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0):
        return "the suite needs root, or sudo without a password, to set up {}".format(
            ", ".join((DEV_LOG,) + SYSTEM_DIRS))
    if not os.path.exists(DATABASE_CONFIG):
        return "{} is missing (libswsscommon installs it)".format(DATABASE_CONFIG)
    with open(DATABASE_CONFIG) as f:
        config = json.load(f)
    instance = config.get("INSTANCES", {}).get("redis", {})
    expected = {"hostname": "127.0.0.1", "port": TCP_PORT, "unix_socket_path": UNIX_SOCKET}
    if {k: instance.get(k) for k in expected} != expected:
        return "{} has redis instance {}; the suite runs redis at {}".format(DATABASE_CONFIG, instance, expected)
    ids = {name: config.get("DATABASES", {}).get(name, {}).get("id") for name in DB_IDS}
    if ids != DB_IDS:
        return "{} has database ids {}; the suite expects {}".format(DATABASE_CONFIG, ids, DB_IDS)
    return None


def _as_root(*cmd):
    subprocess.check_call((() if os.geteuid() == 0 else ("sudo", "-n")) + cmd)


def mapping_key(s):
    """ycabled's normalization of vendor and model names."""
    return "_".join(s.lower().split())


# Extra vendor mapping entries of the overlay. The second and third model
# exercise the driver-load error paths.
FAKE_MAPPING = {
    mapping_key(VENDOR): {
        mapping_key(MODEL): "bbfake.y_cable_bbfake",
        "bb_model_noclass": "bbfake.y_cable_noclass",
        "bb_model_nomodule": "bbfake.y_cable_missing",
    }
}


class BlackboxEnv(object):
    def __init__(self, workdir, base_env=None, coverage=None):
        self.workdir = workdir
        # Without pytest-cov's variables, which start coverage in every Python
        # subprocess.
        self.base_env = {k: v for k, v in (os.environ if base_env is None else base_env).items()
                         if not k.startswith("COV_CORE_")}
        self.src_dir = SRC_DIR
        self.pylib_dir = os.path.join(BLACKBOX_DIR, "pylib")
        self.overlay_dir = os.path.join(workdir, "overlay")
        self.gen_dir = os.path.join(workdir, "gen")
        self.syslog_socket = os.path.join(workdir, "syslog.sock")
        self.coverage = coverage
        self.coverage_saved_dir = os.path.join(workdir, "coverage-saved")
        self.redis = RedisServer(workdir)
        self.dbs = None
        self.monitor = None
        # What other recorders saw ycabled do, such as the multi-ASIC test's
        # in its namespace databases; part of the contract coverage report.
        self.extra_accesses = []
        self.syslog = None
        self.hw = None

    def start(self):
        if os.path.isdir(self.workdir):
            shutil.rmtree(self.workdir)
        os.makedirs(self.workdir)
        if self.coverage is not None:
            os.makedirs(self.coverage_saved_dir)
            os.makedirs(os.path.dirname(self.coverage.data_file), exist_ok=True)
        self.prepare_system()
        self.generate_proto()
        self.build_overlay()
        if self.gen_dir not in sys.path:
            sys.path.insert(0, self.gen_dir)
        self.redis.start()
        self.dbs = Dbs(self.redis)
        self.monitor = Monitor()
        self.monitor.start()
        self.syslog = SyslogSink(self.syslog_socket)
        self.syslog.start()
        self.hw = FakeHardware(os.path.join(self.workdir, "hw.sock"))
        self.hw.start()

    def stop(self):
        for part in (self.hw, self.syslog, self.monitor):
            if part is not None:
                part.stop()
        self.redis.stop()

    def prepare_system(self):
        """Makes SYSTEM_DIRS the suite's, and points /dev/log at its syslog
        socket. Fine in the throwaway container the suite runs in."""
        _as_root("mkdir", "-p", *SYSTEM_DIRS)
        if os.geteuid() != 0:
            _as_root("chown", "-R", "{}:{}".format(os.getuid(), os.getgid()), *SYSTEM_DIRS)
        _as_root("ln", "-sfn", self.syslog_socket, DEV_LOG)

    def generate_proto(self):
        out = os.path.join(self.gen_dir, "proto_out")
        os.makedirs(out)
        open(os.path.join(out, "__init__.py"), "w").close()
        subprocess.check_call([sys.executable, "-m", "grpc_tools.protoc",
                               "-I", os.path.join(self.src_dir, "proto"),
                               "--python_out", self.gen_dir, "--grpc_python_out", self.gen_dir,
                               "proto_out/linkmgr_grpc_driver.proto"], env=self.base_env)

    def build_overlay(self):
        """A copy of the installed sonic_y_cable with the fake driver added and
        mapped, ahead of the real package on ycabled's PYTHONPATH."""
        # In a subprocess: unit tests in the same session replace sonic_y_cable
        # in sys.modules.
        real = subprocess.check_output(
            [sys.executable, "-c", "import importlib.util; "
             "print(list(importlib.util.find_spec('sonic_y_cable').submodule_search_locations)[0])"],
            env=self.base_env, cwd="/", text=True).strip()
        dst = os.path.join(self.overlay_dir, "sonic_y_cable")
        shutil.copytree(real, dst, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(os.path.join(self.pylib_dir, "bbfake"), os.path.join(dst, "bbfake"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        with open(os.path.join(dst, "y_cable_vendor_mapping.py"), "a") as f:
            f.write("\n# Added by the ycabled black-box suite.\n")
            for vendor, models in FAKE_MAPPING.items():
                f.write("mapping.setdefault({!r}, {{}}).update({!r})\n".format(vendor, models))
