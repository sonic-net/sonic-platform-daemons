import contextlib

from unittest.mock import MagicMock, patch

from sonic_py_common import device_info
from xcvrd.xcvrd_utilities import common


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

