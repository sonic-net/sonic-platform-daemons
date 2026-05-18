import os
import sys
import importlib.util
import importlib.machinery
def load_source(module_name, module_path):
    loader = importlib.machinery.SourceFileLoader(module_name, module_path)
    spec = importlib.util.spec_from_file_location(module_name, module_path, loader=loader)
    if module_name in sys.modules:
        module = sys.modules[module_name]
    else:
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module

from mock import Mock, MagicMock, patch
from sonic_py_common import daemon_base

from .mock_platform import MockChassis, MockModule
from .mock_module_base import ModuleBase

SYSLOG_IDENTIFIER = 'chassis_db_init_test'
NOT_AVAILABLE = 'N/A'

daemon_base.db_connect = MagicMock()

test_path = os.path.dirname(os.path.abspath(__file__))
modules_path = os.path.dirname(test_path)
scripts_path = os.path.join(modules_path, "scripts")
sys.path.insert(0, modules_path)

os.environ["CHASSIS_DB_INIT_UNIT_TESTING"] = "1"
load_source('chassis_db_init', scripts_path + '/chassis_db_init')
from chassis_db_init import *


def test_provision_db():
    chassis = MockChassis()
    log = MagicMock()
    serial = "Serial No"
    model = "Model A"
    revision = "Rev C"
    switch_host_serial = NOT_AVAILABLE

    chassis_table = provision_db(chassis, log)

    fvs = chassis_table.get(CHASSIS_INFO_KEY_TEMPLATE.format(1))
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert serial == fvs[CHASSIS_INFO_SERIAL_FIELD]
    assert model == fvs[CHASSIS_INFO_MODEL_FIELD]
    assert revision == fvs[CHASSIS_INFO_REV_FIELD]
    assert switch_host_serial == fvs[CHASSIS_INFO_SWITCH_HOST_SERIAL_FIELD]

def test_provision_db_bmc():
    chassis = MockChassis()
    log = MagicMock()
    switch_host_serial = "Switch Host Serial"

    with patch.object(chassis, 'is_bmc', return_value=True), \
         patch.object(chassis, 'get_switch_host_serial', create=True, return_value=switch_host_serial):
        chassis_table = provision_db(chassis, log)

    fvs = chassis_table.get(CHASSIS_INFO_KEY_TEMPLATE.format(1))
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert switch_host_serial == fvs[CHASSIS_INFO_SWITCH_HOST_SERIAL_FIELD]

def test_try_get_timeout_error():
    def raise_timeout():
        raise TimeoutError("timeout")

    result = try_get(raise_timeout)

    assert result == NOT_AVAILABLE

def test_try_get_not_implemented_error():
    def raise_not_implemented():
        raise NotImplementedError("not implemented")

    result = try_get(raise_not_implemented)

    assert result == NOT_AVAILABLE
