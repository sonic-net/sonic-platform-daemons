import os
import sys
import importlib.util
import importlib.machinery
from unittest import mock

tests_path = os.path.dirname(os.path.abspath(__file__))

# Add mocked_libs path so that the file under test can load mocked modules from there
mocked_libs_path = os.path.join(tests_path, "mocked_libs")
sys.path.insert(0, mocked_libs_path)

from sonic_py_common import daemon_base
daemon_base.db_connect = mock.MagicMock()

# Add path to the file under test so that we can load it
modules_path = os.path.dirname(tests_path)
scripts_path = os.path.join(modules_path, "scripts")
sys.path.insert(0, modules_path)

# Load psud once so all test files share the same module object and mocks apply correctly
if 'psud' not in sys.modules:
    loader = importlib.machinery.SourceFileLoader('psud', os.path.join(scripts_path, 'psud'))
    spec = importlib.util.spec_from_file_location('psud', os.path.join(scripts_path, 'psud'), loader=loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules['psud'] = module
    spec.loader.exec_module(module)
