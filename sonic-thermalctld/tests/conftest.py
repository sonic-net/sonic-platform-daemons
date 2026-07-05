import os
import sys
import importlib.util
import importlib.machinery
from mock import MagicMock
import logging
import logging.handlers
class DummySysLogHandler(logging.Handler):
    priority_map = {}
    LOG_USER = 8
    LOG_DAEMON = 24
    def __init__(self, *args, **kwargs):
        super().__init__()
    def emit(self, record):
        pass
logging.handlers.SysLogHandler = DummySysLogHandler
sys.modules['fcntl'] = MagicMock()

tests_path = os.path.dirname(os.path.abspath(__file__))

# Add mocked_libs path so that the file under test can load mocked modules from there
mocked_libs_path = os.path.join(tests_path, "mocked_libs")
sys.path.insert(0, mocked_libs_path)
sys.path.insert(0, r"c:\Users\ayushraj\Important\lfx 2026\sonic-platform-common")

# Mock syslog since it is not available on Windows
class MockSyslog:
    LOG_EMERG = 0
    LOG_ALERT = 1
    LOG_CRIT = 2
    LOG_ERR = 3
    LOG_WARNING = 4
    LOG_NOTICE = 5
    LOG_INFO = 6
    LOG_DEBUG = 7
    LOG_DAEMON = 24
    LOG_USER = 8
    LOG_NDELAY = 8
    LOG_PID = 1

    def openlog(self, *args, **kwargs): pass
    def closelog(self, *args, **kwargs): pass
    def syslog(self, *args, **kwargs): pass

sys.modules['syslog'] = MockSyslog()

# Mock SIGHUP, SIGUSR1, and signal.signal on Windows
import signal
import _signal
if sys.platform == "win32":
    signal.signal = MagicMock()
    _signal.signal = MagicMock()
if not hasattr(signal, 'SIGHUP'):
    signal.SIGHUP = 1
if not hasattr(signal, 'SIGUSR1'):
    signal.SIGUSR1 = 10

# Mock swsscommon using the local mock_swsscommon if not already mocked by sys.path
# Note: tests/test_thermalctld.py manually inserts mocked_libs and imports swsscommon.
# But we can also set it up here.
# Mock swsscommon using local mocks if needed, but let test_thermalctld.py load it from mocked_libs.

from sonic_py_common import daemon_base
daemon_base.db_connect = MagicMock()
