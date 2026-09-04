import contextlib
import threading

import pytest
from unittest.mock import MagicMock, patch

from sonic_py_common import daemon_base, device_info
from swsscommon import swsscommon
from xcvrd.cpo import cpo_state_task
from xcvrd.cpo.cpo_state_task import CpoStateUpdateTask
from xcvrd.cpo.db_utils import CPODOMDBUtils, CPOVDMDBUtils
from xcvrd.xcvrd import PHYSICAL_PORT_NOT_EXIST, SFP_EEPROM_NOT_READY
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

        with pytest.raises(AssertionError, match='Ganged ports are not yet supported'):
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
