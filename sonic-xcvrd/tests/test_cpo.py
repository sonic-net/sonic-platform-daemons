import sys

if sys.version_info >= (3, 3):
    from unittest.mock import MagicMock, patch
else:
    from mock import MagicMock, patch

from xcvrd.xcvrd_utilities import common


class TestPortDeviceResolver(object):
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


class TestObjDictAccessors(object):
    def _make_port_mapping(self, physical_ports=(0, 1, 2)):
        port_mapping = MagicMock()
        port_mapping.physical_to_logical = {p: ['Ethernet{}'.format(p * 4)] for p in physical_ports}
        return port_mapping

    def _make_obj_dict(self):
        return {0: MagicMock(), 1: MagicMock(), 2: MagicMock()}

    def test_get_cpo_obj_dict(self):
        objs = self._make_obj_dict()
        with patch.object(common, 'is_cpo_port', side_effect=lambda p: p in (1,)), \
             patch.object(common, 'get_port_device', side_effect=lambda p: objs[p]):
            cpo = common.get_cpo_obj_dict(self._make_port_mapping())
        assert set(cpo.keys()) == {1}
        assert cpo[1] is objs[1]

    def test_get_pluggable_obj_dict_excludes_cpo(self):
        objs = self._make_obj_dict()
        with patch.object(common, 'is_cpo_port', side_effect=lambda p: p in (1,)), \
             patch.object(common, 'get_port_device', side_effect=lambda p: objs[p]):
            pluggable = common.get_pluggable_obj_dict(self._make_port_mapping())
        assert set(pluggable.keys()) == {0, 2}
        assert pluggable[0] is objs[0]

    def test_accessors_are_complementary(self):
        objs = self._make_obj_dict()
        port_mapping = self._make_port_mapping()
        with patch.object(common, 'is_cpo_port', side_effect=lambda p: p in (1,)), \
             patch.object(common, 'get_port_device', side_effect=lambda p: objs[p]):
            cpo = common.get_cpo_obj_dict(port_mapping)
            pluggable = common.get_pluggable_obj_dict(port_mapping)
        assert set(cpo) | set(pluggable) == set(objs)
        assert set(cpo) & set(pluggable) == set()

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
