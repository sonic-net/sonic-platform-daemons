import os
import sys
import importlib.util
import importlib.machinery
from mock import MagicMock

tests_path = os.path.dirname(os.path.abspath(__file__))

# Add mocked_libs path so that the file under test can load mocked modules from there
mocked_libs_path = os.path.join(tests_path, "mocked_libs")
sys.path.insert(0, mocked_libs_path)
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

# Mock SIGHUP on Windows
import signal
if not hasattr(signal, 'SIGHUP'):
    signal.SIGHUP = 1

# Mock swsscommon using the local mock_swsscommon
sys.path.insert(0, tests_path)
import mock_swsscommon
sys.modules['swsscommon'] = mock_swsscommon
sys.modules['swsscommon.swsscommon'] = mock_swsscommon

from sonic_py_common import daemon_base
daemon_base.db_connect = MagicMock()

# Add path to the file under test so that we can load it
modules_path = os.path.dirname(tests_path)
scripts_path = os.path.join(modules_path, "scripts")
sys.path.insert(0, modules_path)

os.environ["CHASSISD_UNIT_TESTING"] = "1"

# Load chassisd once so all test files share the same module object and mocks apply correctly
if 'chassisd' not in sys.modules:
    loader = importlib.machinery.SourceFileLoader('chassisd', os.path.join(scripts_path, 'chassisd'))
    spec = importlib.util.spec_from_file_location('chassisd', os.path.join(scripts_path, 'chassisd'), loader=loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules['chassisd'] = module
    spec.loader.exec_module(module)
