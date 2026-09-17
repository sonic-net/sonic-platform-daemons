import contextlib
import threading

import pytest
from unittest.mock import MagicMock, patch

from sonic_py_common import daemon_base, device_info
from swsscommon import swsscommon
from xcvrd import xcvrd  # noqa: F401
from xcvrd.cmis import cmis_manager_task
from xcvrd.cmis.cmis_manager_task import CmisManagerTask
from xcvrd.cpo import cpo_state_task
from xcvrd.cpo.cpo_manager_task import CpoManagerTask
from xcvrd.cpo.cpo_state_task import CpoStateUpdateTask
from xcvrd.cpo.db_utils import CPODOMDBUtils, CPOVDMDBUtils
from xcvrd.cpo.dom_mgr import CpoDomInfoUpdateTask
from xcvrd.cpo.xcvr_table_helper import CpoXcvrTableHelper
from xcvrd.xcvrd import PHYSICAL_PORT_NOT_EXIST, SFP_EEPROM_NOT_READY
from xcvrd.xcvrd_utilities import common, sfp_status_helper
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


class TestCpoStateUpdateTask:
    OE_INFO = {'manufacturer': 'FAKE_MANUFACTURER', 'model': 'FAKE_MODEL', 'host_lane_count': 8}
    ELSFP_INFO = {'type': 'OIF-ELSP', 'serial': 'SN0123456789', 'max_optical_power': 10.0}

    @contextlib.contextmanager
    def mocked_db_tables(self):
        def new_table(*args, **kwargs):
            return MagicMock()

        with patch.object(daemon_base, 'db_connect', MagicMock()), \
             patch.object(swsscommon, 'Table', MagicMock(side_effect=new_table)), \
             patch.object(swsscommon, 'ProducerStateTable', MagicMock(side_effect=new_table)):
            yield

    def make_port_mapping(self, logical_port='Ethernet0', physical_port=1, asic_id=0):
        port_mapping = PortMapping()
        port_mapping.handle_port_change_event(
            PortChangeEvent(logical_port, physical_port, asic_id, PortChangeEvent.PORT_ADD))
        return port_mapping

    def make_task(self, port_mapping=None, port_obj_dict=None):
        with self.mocked_db_tables():
            return CpoStateUpdateTask(DEFAULT_NAMESPACE,
                                      self.make_port_mapping() if port_mapping is None else port_mapping,
                                      {} if port_obj_dict is None else port_obj_dict,
                                      threading.Event(), threading.Event())

    def make_cpo_device(self, oe_info=None, elsfp_info=None, is_replaceable=True):
        device = MagicMock()
        device.oe.get_api.return_value.get_transceiver_info.return_value = oe_info
        device.elsfp.get_api.return_value.get_elsfp_info.return_value = elsfp_info
        device.is_replaceable.return_value = is_replaceable
        return device

    def published_info(self, info, is_replaceable):
        """The field/value pairs the task is expected to publish for the given info dict."""
        published = {field: str(value) for field, value in info.items()}
        published['is_replaceable'] = str(is_replaceable)
        return published

    def test_get_port_change_event_reads_elsfp_events(self):
        task = self.make_task()
        chassis = MagicMock()
        chassis.get_elsfp_change_event.return_value = (True, {'elsfp': {'1': '1'},
                                                              'elsfp_error': {'2': '4'}})
        with patch.object(cpo_state_task, 'platform_chassis', chassis):
            status, events, errors = task._get_port_change_event(1000)
        chassis.get_elsfp_change_event.assert_called_once_with(1000)
        assert (status, events, errors) == (True, {'1': '1'}, {'2': '4'})

    def test_get_port_change_event_without_events(self):
        task = self.make_task()
        chassis = MagicMock()
        chassis.get_elsfp_change_event.return_value = (False, {'elsfp': {}})
        with patch.object(cpo_state_task, 'platform_chassis', chassis):
            assert task._get_port_change_event(0) == (False, {}, None)

    def test_get_port_error_description(self):
        task = self.make_task()
        device = MagicMock()
        device.oe.get_api.return_value.get_error_description.return_value = 'Blocking Error|High Temp'
        with patch.object(common, 'get_port_device', return_value=device) as mock_get_port_device:
            assert task._get_port_error_description(1) == 'Blocking Error|High Temp'
        mock_get_port_device.assert_called_once_with(1)

    @pytest.mark.parametrize('is_replaceable', [True, False])
    def test_wrapper_is_replaceable(self, is_replaceable):
        task = self.make_task()
        device = self.make_cpo_device(is_replaceable=is_replaceable)
        with patch.object(common, 'get_port_device', return_value=device):
            assert task._wrapper_is_replaceable(1) is is_replaceable
        device.is_replaceable.assert_called_once_with()

    def test_post_port_info_to_db(self):
        port_mapping = self.make_port_mapping()
        task = self.make_task(port_mapping)
        device = self.make_cpo_device(self.OE_INFO, self.ELSFP_INFO, is_replaceable=True)
        intf_tbl = MagicMock()
        transceiver_dict = {}

        with patch.object(common, '_wrapper_get_presence', return_value=True), \
             patch.object(common, 'get_port_device', return_value=device):
            assert task.post_port_info_to_db('Ethernet0', port_mapping, intf_tbl, transceiver_dict) is None

        # The OE goes to the caller-supplied table, and is never reported as replaceable
        intf_tbl.set.assert_called_once()
        port_name, fvs = intf_tbl.set.call_args.args
        assert port_name == 'Ethernet0'
        assert dict(fvs) == self.published_info(self.OE_INFO, False)

        # The ELSFP goes to the ELS info table of the port's asic, with its real replaceability
        els_tbl = task.xcvr_table_helper.get_els_info_tbl(0)
        els_tbl.set.assert_called_once()
        port_name, fvs = els_tbl.set.call_args.args
        assert port_name == 'Ethernet0'
        assert dict(fvs) == self.published_info(self.ELSFP_INFO, True)

        # The freshly read OE info is cached for the caller's next logical port
        assert transceiver_dict == {1: self.OE_INFO}

    def test_cached_oe_info_is_reused(self):
        port_mapping = self.make_port_mapping()
        task = self.make_task(port_mapping)
        device = self.make_cpo_device(self.OE_INFO, self.ELSFP_INFO)
        cached_oe_info = {'model': 'FAKE_CACHED_MODEL'}
        intf_tbl = MagicMock()

        with patch.object(common, '_wrapper_get_presence', return_value=True), \
             patch.object(common, 'get_port_device', return_value=device):
            task.post_port_info_to_db('Ethernet0', port_mapping, intf_tbl, {1: cached_oe_info})

        device.oe.get_api.return_value.get_transceiver_info.assert_not_called()
        assert dict(intf_tbl.set.call_args.args[1]) == self.published_info(cached_oe_info, False)

    def test_no_physical_port(self):
        port_mapping = self.make_port_mapping()
        port_mapping.logical_port_name_to_physical_port_list = MagicMock(return_value=None)
        task = self.make_task(port_mapping)
        intf_tbl = MagicMock()

        assert task.post_port_info_to_db('Ethernet0', port_mapping, intf_tbl, {}) == PHYSICAL_PORT_NOT_EXIST
        intf_tbl.set.assert_not_called()

    def test_ganged_ports_are_rejected(self):
        port_mapping = self.make_port_mapping()
        port_mapping.logical_port_name_to_physical_port_list = MagicMock(return_value=[1, 2])
        task = self.make_task(port_mapping)

        with pytest.raises(NotImplementedError, match='Ganged ports are not yet supported'):
            task.post_port_info_to_db('Ethernet0', port_mapping, MagicMock(), {})

    def test_non_present_elsfp_is_skipped(self):
        port_mapping = self.make_port_mapping()
        task = self.make_task(port_mapping)
        intf_tbl = MagicMock()
        transceiver_dict = {}

        with patch.object(common, '_wrapper_get_presence', return_value=False) as mock_get_presence, \
             patch.object(common, 'get_port_device') as mock_get_port_device:
            assert task.post_port_info_to_db('Ethernet0', port_mapping, intf_tbl, transceiver_dict) is None

        mock_get_presence.assert_called_once_with(1)
        mock_get_port_device.assert_not_called()
        intf_tbl.set.assert_not_called()
        task.xcvr_table_helper.get_els_info_tbl(0).set.assert_not_called()
        assert transceiver_dict == {}

    def test_post_port_info_set_stop_event_publishes_nothing(self):
        port_mapping = self.make_port_mapping()
        task = self.make_task(port_mapping)
        intf_tbl = MagicMock()
        stop_event = threading.Event()
        stop_event.set()

        with patch.object(common, '_wrapper_get_presence') as mock_get_presence:
            assert task.post_port_info_to_db('Ethernet0', port_mapping, intf_tbl, {}, stop_event) is None

        mock_get_presence.assert_not_called()
        intf_tbl.set.assert_not_called()

    @pytest.mark.parametrize('oe_info, elsfp_info', [
        (None, ELSFP_INFO),
        (OE_INFO, None),
        (None, None),
    ])
    def test_post_port_info_unreadable_eeprom(self, oe_info, elsfp_info):
        port_mapping = self.make_port_mapping()
        task = self.make_task(port_mapping)
        device = self.make_cpo_device(oe_info, elsfp_info)
        intf_tbl = MagicMock()

        with patch.object(common, '_wrapper_get_presence', return_value=True), \
             patch.object(common, 'get_port_device', return_value=device):
            assert task.post_port_info_to_db('Ethernet0', port_mapping,
                                             intf_tbl, {}) == SFP_EEPROM_NOT_READY

        intf_tbl.set.assert_not_called()
        task.xcvr_table_helper.get_els_info_tbl(0).set.assert_not_called()

    def test_post_port_thresholds_to_db(self):
        task = self.make_task()
        task.dom_db_utils = MagicMock()
        task.vdm_db_utils = MagicMock()
        dom_db_cache = {1: {'temperature': '30.0'}}
        vdm_db_cache = {1: {'laser_temperature': '40.0'}}

        task.post_port_thresholds_to_db('Ethernet0', dom_db_cache=dom_db_cache, vdm_db_cache=vdm_db_cache)

        task.dom_db_utils.post_port_dom_thresholds_to_db.assert_called_once_with(
            'Ethernet0', db_cache=dom_db_cache)
        task.vdm_db_utils.post_port_vdm_thresholds_to_db.assert_called_once_with(
            'Ethernet0', db_cache=vdm_db_cache)


class TestCpoDomInfoUpdateTask:
    @contextlib.contextmanager
    def mocked_db_tables(self):
        def new_table(*args, **kwargs):
            return MagicMock()

        with patch.object(daemon_base, 'db_connect', MagicMock()), \
             patch.object(swsscommon, 'Table', MagicMock(side_effect=new_table)), \
             patch.object(swsscommon, 'ProducerStateTable', MagicMock(side_effect=new_table)):
            yield

    def make_port_mapping(self):
        # Matches CPO_DATA: pports 1 and 2 share OE1, ELS1 is shared by pports 1-3
        port_mapping = PortMapping()
        for logical_port, physical_port in (('Ethernet0', 1), ('Ethernet8', 2), ('Ethernet16', 3)):
            port_mapping.handle_port_change_event(
                PortChangeEvent(logical_port, physical_port, 0, PortChangeEvent.PORT_ADD))
        return port_mapping

    def make_cpo_device(self, p):
        """Mock CPO device whose module-scope data is tagged 'module<p>' and
        lane-scope data 'lane<p>', so published values reveal which device
        performed each read."""
        device = MagicMock()
        oe_api = device.oe.get_api.return_value
        oe_api.get_transceiver_info_firmware_versions.return_value = {'active_firmware': 'module{}'.format(p)}
        oe_api.get_non_banked_transceiver_dom_real_value.return_value = {'temperature': 40.0 + p}
        oe_api.get_banked_transceiver_dom_real_value.return_value = {'rx1power': float(p)}
        oe_api.get_non_banked_transceiver_dom_flags.return_value = {'tempHAlarm': 'module{}'.format(p)}
        oe_api.get_banked_transceiver_dom_flags.return_value = {'rx1powerHAlarm': 'lane{}'.format(p)}
        oe_api.get_non_banked_transceiver_status.return_value = {'module_state': 'module{}'.format(p)}
        oe_api.get_banked_transceiver_status.return_value = {'DP1State': 'lane{}'.format(p)}
        oe_api.get_non_banked_transceiver_status_flags.return_value = {'module_state_changed': 'module{}'.format(p)}
        oe_api.get_banked_transceiver_status_flags.return_value = {'rx1los': 'lane{}'.format(p)}
        elsfp_api = device.elsfp.get_api.return_value
        elsfp_api.get_elsfp_info_firmware_versions.return_value = {'active_firmware': 'els_module{}'.format(p)}
        elsfp_api.get_non_banked_elsfp_dom_real_value.return_value = {'temperature': 50.0 + p}
        elsfp_api.get_banked_elsfp_dom_real_value.return_value = {'laser_bias_current_lane1': float(p)}
        elsfp_api.get_non_banked_elsfp_dom_flags.return_value = {'temperature_alarm_high': 'els_module{}'.format(p)}
        elsfp_api.get_banked_elsfp_dom_flags.return_value = {'laser_bias_alarm_high_lane1': 'els_lane{}'.format(p)}
        elsfp_api.get_non_banked_elsfp_status.return_value = {'module_state': 'els_module{}'.format(p)}
        elsfp_api.get_banked_elsfp_status.return_value = {'state_lane1': 'els_lane{}'.format(p)}
        elsfp_api.get_non_banked_elsfp_status_flags.return_value = {'lane_summary_fault': 'els_module{}'.format(p)}
        elsfp_api.get_banked_elsfp_status_flags.return_value = {'fault_flag_lane1': 'els_lane{}'.format(p)}
        return device

    def make_devices(self):
        return {p: self.make_cpo_device(p) for p in (1, 2, 3)}

    def make_task(self, port_obj_dict):
        with self.mocked_db_tables():
            task = CpoDomInfoUpdateTask(DEFAULT_NAMESPACE, self.make_port_mapping(), port_obj_dict,
                                        threading.Event(), True)
        task.check_port_update = MagicMock()
        task.is_port_dom_monitoring_disabled = MagicMock(return_value=False)
        return task

    def run_collect_and_publish(self, task):
        task.dom_db_utils = MagicMock()
        with patched_topology(), \
             patch.object(common, '_wrapper_get_presence', return_value=True), \
             patch.object(sfp_status_helper, 'detect_port_in_error_status', return_value=False):
            task.collect_and_publish_data(MagicMock())

        # Map (logical port, table) -> published values dict
        published = {}
        for call_args in task.dom_db_utils.post_diagnostic_values_from_dict_to_db.call_args_list:
            logical_port, table, values = call_args.args
            published[(logical_port, table)] = values
        for call_args in task.dom_db_utils.post_flag_values_from_dict_to_db.call_args_list:
            logical_port, values, flag_tables = call_args.args[:3]
            published[(logical_port, flag_tables.flag_tbl)] = values
        return published

    def test_collect_and_publish_data(self):
        devices = self.make_devices()
        task = self.make_task(devices)
        published = self.run_collect_and_publish(task)

        # Non-banked data is read once per device: OE1 and ELS1 through pport 1
        # (the first sibling processed), OE2 through pport 3
        devices[1].oe.get_api.return_value.get_non_banked_transceiver_dom_real_value.assert_called_once()
        devices[2].oe.get_api.return_value.get_non_banked_transceiver_dom_real_value.assert_not_called()
        devices[3].oe.get_api.return_value.get_non_banked_transceiver_dom_real_value.assert_called_once()
        devices[1].elsfp.get_api.return_value.get_non_banked_elsfp_dom_real_value.assert_called_once()
        devices[2].elsfp.get_api.return_value.get_non_banked_elsfp_dom_real_value.assert_not_called()
        devices[3].elsfp.get_api.return_value.get_non_banked_elsfp_dom_real_value.assert_not_called()

        # Banked data is read once per physical port
        for device in devices.values():
            device.oe.get_api.return_value.get_banked_transceiver_dom_real_value.assert_called_once()
            device.elsfp.get_api.return_value.get_banked_elsfp_dom_real_value.assert_called_once()

        # Every interface gets the module-scope values of its device sharing group
        # merged with its own lane-scope values
        dom_tbl = task.xcvr_table_helper.get_dom_tbl(0)
        els_dom_tbl = task.xcvr_table_helper.get_els_dom_tbl(0)
        assert published[('Ethernet0', dom_tbl)] == {'temperature': 41.0, 'rx1power': 1.0}
        assert published[('Ethernet8', dom_tbl)] == {'temperature': 41.0, 'rx1power': 2.0}
        assert published[('Ethernet16', dom_tbl)] == {'temperature': 43.0, 'rx1power': 3.0}
        assert published[('Ethernet0', els_dom_tbl)] == {'temperature': 51.0, 'laser_bias_current_lane1': 1.0}
        assert published[('Ethernet8', els_dom_tbl)] == {'temperature': 51.0, 'laser_bias_current_lane1': 2.0}
        assert published[('Ethernet16', els_dom_tbl)] == {'temperature': 51.0, 'laser_bias_current_lane1': 3.0}

    def test_collect_and_publish_flags_and_status(self):
        devices = self.make_devices()
        task = self.make_task(devices)
        published = self.run_collect_and_publish(task)

        # Non-banked flag/status reads are deduplicated per device like the DOM values
        devices[1].oe.get_api.return_value.get_non_banked_transceiver_status_flags.assert_called_once()
        devices[2].oe.get_api.return_value.get_non_banked_transceiver_status_flags.assert_not_called()
        devices[2].oe.get_api.return_value.get_banked_transceiver_status_flags.assert_called_once()

        # Ethernet8 publishes OE1's module-scope snapshot (read via pport 1) merged
        # with its own lane-scope values
        assert published[('Ethernet8', task.xcvr_table_helper.get_dom_flag_tbl(0))] == \
            {'tempHAlarm': 'module1', 'rx1powerHAlarm': 'lane2'}
        assert published[('Ethernet8', task.xcvr_table_helper.get_status_tbl(0))] == \
            {'module_state': 'module1', 'DP1State': 'lane2'}
        assert published[('Ethernet8', task.xcvr_table_helper.get_status_flag_tbl(0))] == \
            {'module_state_changed': 'module1', 'rx1los': 'lane2'}
        assert published[('Ethernet16', task.xcvr_table_helper.get_status_flag_tbl(0))] == \
            {'module_state_changed': 'module3', 'rx1los': 'lane3'}

        # Firmware info is module-scope only: every sibling publishes its device
        # sharing group's snapshot
        devices[2].oe.get_api.return_value.get_transceiver_info_firmware_versions.assert_not_called()
        assert published[('Ethernet8', task.xcvr_table_helper.get_firmware_info_tbl(0))] == \
            {'active_firmware': 'module1'}
        assert published[('Ethernet16', task.xcvr_table_helper.get_firmware_info_tbl(0))] == \
            {'active_firmware': 'module3'}

        # ELS1 drives all three ports, so every interface publishes ELS1's
        # module-scope snapshot (read via pport 1) merged with its own lane-scope
        # values; ELSFP status flags are module-scope only
        devices[2].elsfp.get_api.return_value.get_non_banked_elsfp_dom_flags.assert_not_called()
        devices[2].elsfp.get_api.return_value.get_non_banked_elsfp_status_flags.assert_not_called()
        devices[2].elsfp.get_api.return_value.get_banked_elsfp_dom_flags.assert_called_once()
        devices[2].elsfp.get_api.return_value.get_banked_elsfp_status_flags.assert_called_once()
        assert published[('Ethernet8', task.xcvr_table_helper.get_els_firmware_info_tbl(0))] == \
            {'active_firmware': 'els_module1'}
        assert published[('Ethernet8', task.xcvr_table_helper.get_els_dom_flag_tbl(0))] == \
            {'temperature_alarm_high': 'els_module1', 'laser_bias_alarm_high_lane1': 'els_lane2'}
        assert published[('Ethernet8', task.xcvr_table_helper.get_els_status_tbl(0))] == \
            {'module_state': 'els_module1', 'state_lane1': 'els_lane2'}
        assert published[('Ethernet16', task.xcvr_table_helper.get_els_status_flag_tbl(0))] == \
            {'lane_summary_fault': 'els_module1', 'fault_flag_lane1': 'els_lane3'}

    def run_link_change_update(self, task, device_key):
        task.dom_db_utils = MagicMock()
        with patched_topology(), \
             patch.object(common, '_wrapper_get_presence', return_value=True), \
             patch.object(sfp_status_helper, 'detect_port_in_error_status', return_value=False):
            task.update_port_db_diagnostics_on_link_change(device_key)

        published = {}
        for call_args in task.dom_db_utils.post_flag_values_from_dict_to_db.call_args_list:
            logical_port, values, flag_tables = call_args.args[:3]
            published[(logical_port, flag_tables.flag_tbl)] = values
        return published

    def test_on_port_update_event_queues_device_keys(self):
        task = self.make_task({})
        with patched_topology():
            task.on_port_update_event(PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_SET,
                                                      {}, 'APPL_DB', 'PORT_TABLE'))
            assert set(task.link_change_affected_ports) == {
                (common.CPO_DEVICE_TYPE_OE, 'OE1'),
                (common.CPO_DEVICE_TYPE_ELSFP, 'ELS1'),
            }

            # A sibling flap coalesces into the same entries, a flap on another
            # OE adds only that OE's entry
            task.on_port_update_event(PortChangeEvent('Ethernet16', 3, 0, PortChangeEvent.PORT_SET,
                                                      {}, 'APPL_DB', 'PORT_TABLE'))
            assert set(task.link_change_affected_ports) == {
                (common.CPO_DEVICE_TYPE_OE, 'OE1'),
                (common.CPO_DEVICE_TYPE_OE, 'OE2'),
                (common.CPO_DEVICE_TYPE_ELSFP, 'ELS1'),
            }

    def test_link_change_publishes_oe_flags_per_device(self):
        devices = self.make_devices()
        task = self.make_task(devices)
        published = self.run_link_change_update(task, (common.CPO_DEVICE_TYPE_OE, 'OE1'))

        # Module-scope flags are read once, through the first member port's API;
        # banked flags are read per member
        devices[1].oe.get_api.return_value.get_non_banked_transceiver_dom_flags.assert_called_once()
        devices[2].oe.get_api.return_value.get_non_banked_transceiver_dom_flags.assert_not_called()
        devices[2].oe.get_api.return_value.get_banked_transceiver_dom_flags.assert_called_once()

        # Every member port publishes the module snapshot merged with its own
        # lane flags; only OE1's members and only the OE flag tables are touched
        dom_flag_tbl = task.xcvr_table_helper.get_dom_flag_tbl(0)
        status_flag_tbl = task.xcvr_table_helper.get_status_flag_tbl(0)
        assert published == {
            ('Ethernet0', dom_flag_tbl): {'tempHAlarm': 'module1', 'rx1powerHAlarm': 'lane1'},
            ('Ethernet8', dom_flag_tbl): {'tempHAlarm': 'module1', 'rx1powerHAlarm': 'lane2'},
            ('Ethernet0', status_flag_tbl): {'module_state_changed': 'module1', 'rx1los': 'lane1'},
            ('Ethernet8', status_flag_tbl): {'module_state_changed': 'module1', 'rx1los': 'lane2'},
        }

        # Value tables are not republished on link change
        task.dom_db_utils.post_diagnostic_values_from_dict_to_db.assert_not_called()

    def test_link_change_publishes_elsfp_flags_per_device(self):
        devices = self.make_devices()
        task = self.make_task(devices)
        published = self.run_link_change_update(task, (common.CPO_DEVICE_TYPE_ELSFP, 'ELS1'))

        # Module-scope flags are read once, through the first member port's API;
        # banked flags are read per member
        devices[1].elsfp.get_api.return_value.get_non_banked_elsfp_dom_flags.assert_called_once()
        devices[1].elsfp.get_api.return_value.get_non_banked_elsfp_status_flags.assert_called_once()
        for p in (2, 3):
            devices[p].elsfp.get_api.return_value.get_non_banked_elsfp_dom_flags.assert_not_called()
            devices[p].elsfp.get_api.return_value.get_non_banked_elsfp_status_flags.assert_not_called()
        for device in devices.values():
            device.elsfp.get_api.return_value.get_banked_elsfp_dom_flags.assert_called_once()
            device.elsfp.get_api.return_value.get_banked_elsfp_status_flags.assert_called_once()

        # All of ELS1's member ports publish the module snapshot merged with their
        # own lane flags; the OE flag tables are untouched
        els_dom_flag_tbl = task.xcvr_table_helper.get_els_dom_flag_tbl(0)
        els_status_flag_tbl = task.xcvr_table_helper.get_els_status_flag_tbl(0)
        assert published == {
            ('Ethernet0', els_dom_flag_tbl): {'temperature_alarm_high': 'els_module1', 'laser_bias_alarm_high_lane1': 'els_lane1'},
            ('Ethernet8', els_dom_flag_tbl): {'temperature_alarm_high': 'els_module1', 'laser_bias_alarm_high_lane1': 'els_lane2'},
            ('Ethernet16', els_dom_flag_tbl): {'temperature_alarm_high': 'els_module1', 'laser_bias_alarm_high_lane1': 'els_lane3'},
            ('Ethernet0', els_status_flag_tbl): {'lane_summary_fault': 'els_module1', 'fault_flag_lane1': 'els_lane1'},
            ('Ethernet8', els_status_flag_tbl): {'lane_summary_fault': 'els_module1', 'fault_flag_lane1': 'els_lane2'},
            ('Ethernet16', els_status_flag_tbl): {'lane_summary_fault': 'els_module1', 'fault_flag_lane1': 'els_lane3'},
        }
        for device in devices.values():
            device.oe.get_api.return_value.get_non_banked_transceiver_dom_flags.assert_not_called()
