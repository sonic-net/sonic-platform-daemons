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
    def _make_obj_dict(self):
        return {0: MagicMock(), 1: MagicMock(), 2: MagicMock()}

    def test_get_cpo_obj_dict(self):
        objs = self._make_obj_dict()
        with patch.object(common, 'is_cpo_port', side_effect=lambda p: p in (1,)):
            cpo = common.get_cpo_obj_dict(objs)
        assert set(cpo.keys()) == {1}
        assert cpo[1] is objs[1]

    def test_get_pluggable_obj_dict_excludes_cpo(self):
        objs = self._make_obj_dict()
        with patch.object(common, 'is_cpo_port', side_effect=lambda p: p in (1,)):
            pluggable = common.get_pluggable_obj_dict(objs)
        assert set(pluggable.keys()) == {0, 2}
        assert pluggable[0] is objs[0]

    def test_accessors_are_complementary(self):
        objs = self._make_obj_dict()
        with patch.object(common, 'is_cpo_port', side_effect=lambda p: p in (1,)):
            cpo = common.get_cpo_obj_dict(objs)
            pluggable = common.get_pluggable_obj_dict(objs)
        assert set(cpo) | set(pluggable) == set(objs)
        assert set(cpo) & set(pluggable) == set()

    def test_all_pluggable_when_no_cpo(self):
        objs = self._make_obj_dict()
        with patch.object(common, 'is_cpo_port', return_value=False):
            assert common.get_cpo_obj_dict(objs) == {}
            assert set(common.get_pluggable_obj_dict(objs)) == {0, 1, 2}
