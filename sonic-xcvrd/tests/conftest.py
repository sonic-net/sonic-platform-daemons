from unittest.mock import patch

from sonic_py_common import syslogger


# Runtime logger configuration uses Redis. Isolate it before importing xcvrd,
# since session fixtures run after collection-time module initialization.
_runtime_log_config = patch.object(
    syslogger.SysLogger, "update_log_level", return_value=(True, "")
)


def pytest_configure(config):
    _runtime_log_config.start()


def pytest_unconfigure(config):
    _runtime_log_config.stop()
