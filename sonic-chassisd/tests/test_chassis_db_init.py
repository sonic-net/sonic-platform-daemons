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

    # Create a mock switch host module at index 0
    switch_host_module = MockModule(
        module_index=0,
        module_name="SWITCH-HOST0",
        module_desc="Switch Host Module",
        module_type=ModuleBase.MODULE_TYPE_SWITCH_HOST,
        module_slot=0,
        module_serial=switch_host_serial
    )
    chassis.module_list.append(switch_host_module)

    with patch.object(chassis, 'is_bmc', return_value=True):
        chassis_table = provision_db(chassis, log)

    fvs = chassis_table.get(CHASSIS_INFO_KEY_TEMPLATE.format(1))
    if isinstance(fvs, list):
        fvs = dict(fvs[-1])
    assert switch_host_serial == fvs[CHASSIS_INFO_SWITCH_HOST_SERIAL_FIELD]

def test_provision_db_bmc_no_module():
    """Test that BMC chassis without module at index 0 raises RuntimeError"""
    chassis = MockChassis()
    log = MagicMock()

    with patch.object(chassis, 'is_bmc', return_value=True):
        try:
            chassis_table = provision_db(chassis, log)
            assert False, "Expected RuntimeError to be raised"
        except RuntimeError as e:
            assert "Switch Host Module must be present" in str(e)

def test_provision_db_bmc_wrong_module_type():
    """Test that BMC chassis with wrong module type at index 0 raises RuntimeError"""
    chassis = MockChassis()
    log = MagicMock()

    # Create a line card module instead of switch host module
    wrong_module = MockModule(
        module_index=0,
        module_name="LINE-CARD0",
        module_desc="Line Card Module",
        module_type=ModuleBase.MODULE_TYPE_LINE,
        module_slot=0,
        module_serial="LC Serial"
    )
    chassis.module_list.append(wrong_module)

    with patch.object(chassis, 'is_bmc', return_value=True):
        try:
            chassis_table = provision_db(chassis, log)
            assert False, "Expected RuntimeError to be raised"
        except RuntimeError as e:
            assert "Switch Host Module must be present" in str(e)

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
