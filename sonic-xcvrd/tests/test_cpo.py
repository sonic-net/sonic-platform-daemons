import contextlib
import threading

from unittest.mock import MagicMock, patch

from sonic_py_common import device_info
from xcvrd import xcvrd  # noqa: F401
from xcvrd.cmis import cmis_manager_task
from xcvrd.cmis.cmis_manager_task import CmisManagerTask
from xcvrd.cpo.cpo_manager_task import CpoManagerTask
from xcvrd.xcvrd_utilities import common
from xcvrd.xcvrd_utilities.port_event_helper import PortChangeEvent, PortMapping

DEFAULT_NAMESPACE = ['']


class TestPortDeviceResolver:
    def test_is_cpo_port_no_chassis(self):
        with patch.object(common, 'platform_chassis', None):
            assert common.is_cpo_port(0) is False

    def test_is_cpo_port_true_when_cpo_present(self):
        chassis = MagicMock()
        chassis.get_cpo.return_value = MagicMock()
        with patch.object(common, 'platform_chassis', chassis):
            assert common.is_cpo_port(3) is True
        chassis.get_cpo.assert_called_with(3)

    def test_is_cpo_port_false_when_not_cpo(self):
        chassis = MagicMock()
        chassis.get_cpo.return_value = None
        with patch.object(common, 'platform_chassis', chassis):
            assert common.is_cpo_port(3) is False

    def test_is_cpo_port_swallows_not_implemented(self):
        chassis = MagicMock()
        chassis.get_cpo.side_effect = NotImplementedError
        with patch.object(common, 'platform_chassis', chassis):
            assert common.is_cpo_port(3) is False

    def test_get_port_device_prefers_cpo(self):
        chassis = MagicMock()
        cpo = MagicMock()
        chassis.get_cpo.return_value = cpo
        with patch.object(common, 'platform_chassis', chassis):
            assert common.get_port_device(1) is cpo
        chassis.get_sfp.assert_not_called()

    def test_get_port_device_falls_back_to_sfp(self):
        chassis = MagicMock()
        sfp = MagicMock()
        chassis.get_cpo.return_value = None
        chassis.get_sfp.return_value = sfp
        with patch.object(common, 'platform_chassis', chassis):
            assert common.get_port_device(1) is sfp

    def test_get_port_device_none_when_unavailable(self):
        with patch.object(common, 'platform_chassis', None):
            assert common.get_port_device(1) is None


class TestObjDictAccessors:
    def _make_port_mapping(self, physical_ports=(0, 1, 2)):
        port_mapping = MagicMock()
        port_mapping.physical_to_logical = {p: ['Ethernet{}'.format(p * 4)] for p in physical_ports}
        return port_mapping

    def _make_obj_dict(self):
        return {0: MagicMock(), 1: MagicMock(), 2: MagicMock()}

    def test_accessors_are_complementary(self):
        objs = self._make_obj_dict()
        port_mapping = self._make_port_mapping()
        with patch.object(common, 'is_cpo_port', side_effect=lambda p: p in (1,)), \
             patch.object(common, 'get_port_device', side_effect=lambda p: objs[p]):
            cpo = common.get_cpo_obj_dict(port_mapping)
            pluggable = common.get_pluggable_obj_dict(port_mapping)
        assert set(cpo) | set(pluggable) == set(objs)
        assert set(cpo) & set(pluggable) == set()
        assert set(cpo) == {1}
        assert cpo[1] is objs[1]

    def test_all_pluggable_when_no_cpo(self):
        objs = self._make_obj_dict()
        port_mapping = self._make_port_mapping()
        with patch.object(common, 'is_cpo_port', return_value=False), \
             patch.object(common, 'get_port_device', side_effect=lambda p: objs[p]):
            assert common.get_cpo_obj_dict(port_mapping) == {}
            assert set(common.get_pluggable_obj_dict(port_mapping)) == {0, 1, 2}

    def test_accessors_return_empty_without_port_mapping(self):
        with patch.object(common, 'get_port_device') as mock_get_port_device:
            assert common.get_cpo_obj_dict(None) == {}
            assert common.get_pluggable_obj_dict(None) == {}

            port_mapping = MagicMock()
            port_mapping.physical_to_logical = None
            assert common.get_cpo_obj_dict(port_mapping) == {}
            assert common.get_pluggable_obj_dict(port_mapping) == {}
        mock_get_port_device.assert_not_called()

    def test_accessors_skip_ports_raising_exceptions(self):
        objs = self._make_obj_dict()

        def mock_get_port_device(physical_port):
            if physical_port == 2:
                raise ValueError("Invalid port")
            return objs[physical_port]

        with patch.object(common, 'is_cpo_port', return_value=False), \
             patch.object(common, 'get_port_device', side_effect=mock_get_port_device):
            pluggable = common.get_pluggable_obj_dict(self._make_port_mapping())
        assert set(pluggable.keys()) == {0, 1}


# Ethernet0 and Ethernet8 share OE1, while ELS1 is shared by all three interfaces
CPO_DATA = {
    'devices': {
        'OE1': {'device_type': 'optical_engine', 'max_banks': 2},
        'OE2': {'device_type': 'optical_engine', 'max_banks': 1},
        'ELS1': {'device_type': 'external_laser_source', 'max_banks': 3, 'lasers': 8},
    },
    'interfaces': {
        'Ethernet0': {'associated_devices': [{'device_id': 'OE1', 'bank': 0},
                                             {'device_id': 'ELS1', 'bank': 0}]},
        'Ethernet8': {'associated_devices': [{'device_id': 'OE1', 'bank': 1},
                                             {'device_id': 'ELS1', 'bank': 1}]},
        'Ethernet16': {'associated_devices': [{'device_id': 'OE2', 'bank': 0},
                                              {'device_id': 'ELS1', 'bank': 2}]},
    },
}

PLATFORM_DATA = {
    'interfaces': {
        'Ethernet0': {'index': '1,1,1,1,1,1,1,1'},
        'Ethernet8': {'index': '2,2,2,2,2,2,2,2'},
        'Ethernet16': {'index': '3,3,3,3,3,3,3,3'},
    },
}

# All three interfaces share the very same optical engine
SINGLE_OE_CPO_DATA = {
    'devices': {
        'OE1': {'device_type': 'optical_engine', 'max_banks': 3},
    },
    'interfaces': {
        'Ethernet0': {'associated_devices': [{'device_id': 'OE1', 'bank': 0}]},
        'Ethernet8': {'associated_devices': [{'device_id': 'OE1', 'bank': 1}]},
        'Ethernet16': {'associated_devices': [{'device_id': 'OE1', 'bank': 2}]},
    },
}

# Logical port to physical port mapping matching PLATFORM_DATA
CPO_PORTS = (('Ethernet0', 1), ('Ethernet8', 2), ('Ethernet16', 3))


@contextlib.contextmanager
def patched_topology(cpo_data=CPO_DATA, platform_data=PLATFORM_DATA):
    """Serve the given platform topology, with the memoized topology cleared."""
    common._build_cpo_topology.cache_clear()
    try:
        with patch.object(device_info, 'get_cpo_data', return_value=cpo_data, create=True) as mock_get_cpo_data, \
             patch.object(device_info, 'get_platform_json_data', return_value=platform_data):
            yield mock_get_cpo_data
    finally:
        common._build_cpo_topology.cache_clear()


class TestCpoTopology:
    def test_devices_of_pport_are_grouped_per_device(self):
        with patched_topology():
            assert common.get_cpo_devices_of_pport(1, common.CPO_DEVICE_TYPE_OE) == {'OE1': {1, 2}}
            assert common.get_cpo_devices_of_pport(3, common.CPO_DEVICE_TYPE_OE) == {'OE2': {3}}
            assert common.get_cpo_devices_of_pport(1, common.CPO_DEVICE_TYPE_ELSFP) == {'ELS1': {1, 2, 3}}

            # No device of the requested type, and no device at all
            assert common.get_cpo_devices_of_pport(1, 'no_such_device_type') == {}
            assert common.get_cpo_devices_of_pport(99, common.CPO_DEVICE_TYPE_OE) == {}

    def test_sibling_pports_grouped_per_device(self):
        with patched_topology():
            # OE1 drives physical ports 1 and 2, OE2 drives physical port 3
            assert common.get_oe_sibling_pports(1) == {1, 2}
            assert common.get_oe_sibling_pports(2) == {1, 2}
            assert common.get_oe_sibling_pports(3) == {3}

            # ELS1 provides the lasers for all three physical ports
            assert common.get_elsfp_sibling_pports(1) == {1, 2, 3}
            assert common.get_elsfp_sibling_pports(3) == {1, 2, 3}

    def test_topology_is_memoized(self):
        with patched_topology() as mock_get_cpo_data:
            common.get_oe_sibling_pports(1)
            common.get_elsfp_sibling_pports(1)
            assert mock_get_cpo_data.call_count == 1

    def test_only_self_returned_without_cpo_data(self):
        with patched_topology(cpo_data=None):
            assert common.get_oe_sibling_pports(1) == {1}
            assert common.get_elsfp_sibling_pports(1) == {1}


class TestCpoManager:
    def _make_cpo_obj(self, module_type='CPO', tx_disable_ok=True):
        api = MagicMock()
        api.get_module_type_abbreviation.return_value = module_type
        api.tx_disable_channel.return_value = tx_disable_ok
        cpo = MagicMock()
        cpo.get_xcvr_api.return_value = api
        return cpo

    def _make_cpo_manager_task(self, port_obj_dict, *, ports=CPO_PORTS, skip_cpo_mgr=False):
        port_mapping = PortMapping()
        for lport, pport in ports:
            port_mapping.handle_port_change_event(PortChangeEvent(lport, pport, 0, PortChangeEvent.PORT_ADD))

        with patch.object(cmis_manager_task, 'XcvrTableHelper'), \
             patch.object(common, 'is_fast_reboot_enabled', return_value=False):
            return CpoManagerTask(DEFAULT_NAMESPACE, port_mapping, port_obj_dict,
                                  threading.Event(), skip_cpo_mgr=skip_cpo_mgr)

    def test_returns_false_when_lport_is_unknown(self):
        task = self._make_cpo_manager_task({1: self._make_cpo_obj(), 2: self._make_cpo_obj()})
        with patched_topology():
            assert task.deinit_oe_sibling_pports('Ethernet64') is False

    def test_returns_false_when_physical_port_is_unknown(self):
        cpo, sibling = self._make_cpo_obj(), self._make_cpo_obj()
        task = self._make_cpo_manager_task({1: cpo, 2: sibling})
        task.port_dict['Ethernet0'].pop('index')
        with patched_topology():
            assert task.deinit_oe_sibling_pports('Ethernet0') is False
        sibling.get_xcvr_api.assert_not_called()

    def test_port_alone_on_its_optical_engine_is_a_noop(self):
        # Ethernet16 (physical port 3) is the only interface of OE2
        objs = {1: self._make_cpo_obj(), 2: self._make_cpo_obj(), 3: self._make_cpo_obj()}
        task = self._make_cpo_manager_task(objs)
        with patched_topology():
            assert task.deinit_oe_sibling_pports('Ethernet16') is True
        for cpo in objs.values():
            cpo.get_xcvr_api.return_value.set_datapath_deinit.assert_not_called()
            cpo.get_xcvr_api.return_value.tx_disable_channel.assert_not_called()

    def test_all_lanes_of_sibling_are_deinitialized(self):
        cpo, sibling = self._make_cpo_obj(), self._make_cpo_obj()
        task = self._make_cpo_manager_task({1: cpo, 2: sibling, 3: self._make_cpo_obj()})
        with patched_topology():
            assert task.deinit_oe_sibling_pports('Ethernet0') is True

        sibling_api = sibling.get_xcvr_api.return_value
        sibling_api.set_datapath_deinit.assert_called_once_with(0xff)
        sibling_api.tx_disable_channel.assert_called_once_with(0xff, True)

        # The lanes of the physical port of lport itself are left to the superclass CMIS logic,
        # and physical port 3 belongs to another optical engine
        for untouched in (cpo, task.port_obj_dict[3]):
            untouched.get_xcvr_api.return_value.set_datapath_deinit.assert_not_called()
            untouched.get_xcvr_api.return_value.tx_disable_channel.assert_not_called()

    def test_returns_false_when_sibling_object_is_missing(self):
        task = self._make_cpo_manager_task({1: self._make_cpo_obj()})
        with patched_topology():
            assert task.deinit_oe_sibling_pports('Ethernet0') is False

    def test_returns_false_when_sibling_has_no_api(self):
        sibling = self._make_cpo_obj()
        sibling.get_xcvr_api.return_value = None
        task = self._make_cpo_manager_task({1: self._make_cpo_obj(), 2: sibling})
        with patched_topology():
            assert task.deinit_oe_sibling_pports('Ethernet0') is False

    def test_returns_false_when_tx_disable_fails(self):
        sibling = self._make_cpo_obj(tx_disable_ok=False)
        task = self._make_cpo_manager_task({1: self._make_cpo_obj(), 2: sibling})
        with patched_topology():
            assert task.deinit_oe_sibling_pports('Ethernet0') is False

        # The datapath was still deinitialized before the Tx output failed to turn off
        sibling.get_xcvr_api.return_value.set_datapath_deinit.assert_called_once_with(0xff)

    def test_remaining_siblings_are_deinitialized_after_a_failure(self):
        broken, healthy = self._make_cpo_obj(), self._make_cpo_obj()
        broken.get_xcvr_api.return_value.set_datapath_deinit.side_effect = Exception('I2C error')
        task = self._make_cpo_manager_task({1: self._make_cpo_obj(), 2: broken, 3: healthy})

        with patched_topology(cpo_data=SINGLE_OE_CPO_DATA), \
             patch.object(common, 'log_exception_traceback') as mock_traceback:
            assert task.deinit_oe_sibling_pports('Ethernet0') is False

        mock_traceback.assert_called_once()

        # The deinit of the broken sibling was attempted and raised, so its Tx output
        # was never turned off
        broken_api = broken.get_xcvr_api.return_value
        broken_api.set_datapath_deinit.assert_called_once_with(0xff)
        broken_api.tx_disable_channel.assert_not_called()

        healthy_api = healthy.get_xcvr_api.return_value
        healthy_api.set_datapath_deinit.assert_called_once_with(0xff)
        healthy_api.tx_disable_channel.assert_called_once_with(0xff, True)

    def test_siblings_are_untouched_outside_low_power(self):
        cpo = self._make_cpo_obj()
        api = cpo.get_xcvr_api()
        api.get_module_state.return_value = 'ModuleReady'

        task = self._make_cpo_manager_task({1: cpo, 2: self._make_cpo_obj()})
        task.port_dict['Ethernet0']['api'] = api
        task.deinit_oe_sibling_pports = MagicMock()

        with patch.object(CmisManagerTask, 'handle_cmis_dp_deinit_state',
                          autospec=True, return_value=True) as mock_parent, \
             patched_topology():
            assert task.handle_cmis_dp_deinit_state('Ethernet0') is True

        task.deinit_oe_sibling_pports.assert_not_called()
        mock_parent.assert_called_once_with(task, 'Ethernet0')
        assert 'cmis_retries' not in task.port_dict['Ethernet0']

    def test_low_power_deinitializes_siblings_before_delegating(self):
        cpo, sibling = self._make_cpo_obj(), self._make_cpo_obj()
        api, sibling_api = cpo.get_xcvr_api(), sibling.get_xcvr_api()
        api.get_module_state.return_value = 'ModuleLowPwr'

        task = self._make_cpo_manager_task({1: cpo, 2: sibling})
        task.port_dict['Ethernet0']['api'] = api

        with patch.object(CmisManagerTask, 'handle_cmis_dp_deinit_state',
                          autospec=True, return_value=True) as mock_parent, \
             patched_topology():
            assert task.handle_cmis_dp_deinit_state('Ethernet0') is True

        sibling_api.set_datapath_deinit.assert_called_once_with(0xff)
        sibling_api.tx_disable_channel.assert_called_once_with(0xff, True)
        mock_parent.assert_called_once_with(task, 'Ethernet0')

    def test_sibling_deinit_failure_retries_without_advancing(self):
        # No CPO object for sibling physical port 2, so the deinit of the optical engine fails
        cpo = self._make_cpo_obj()
        api = cpo.get_xcvr_api()
        api.get_module_state.return_value = 'ModuleLowPwr'

        task = self._make_cpo_manager_task({1: cpo})
        task.port_dict['Ethernet0']['api'] = api

        with patch.object(CmisManagerTask, 'handle_cmis_dp_deinit_state',
                          autospec=True, return_value=True) as mock_parent, \
             patched_topology():
            assert task.handle_cmis_dp_deinit_state('Ethernet0') is False

        mock_parent.assert_not_called()
        assert task.port_dict['Ethernet0']['cmis_retries'] == 1

    def test_sibling_deinit_failure_increments_existing_retries(self):
        cpo = self._make_cpo_obj()
        api = cpo.get_xcvr_api()
        api.get_module_state.return_value = 'ModuleLowPwr'

        task = self._make_cpo_manager_task({1: cpo})
        task.port_dict['Ethernet0']['api'] = api
        task.port_dict['Ethernet0']['cmis_retries'] = 2

        with patch.object(CmisManagerTask, 'handle_cmis_dp_deinit_state',
                          autospec=True, return_value=True), \
             patched_topology():
            assert task.handle_cmis_dp_deinit_state('Ethernet0') is False

        assert task.port_dict['Ethernet0']['cmis_retries'] == 3

