"""Shared transceiver readers must resolve CPO and pluggable ports separately."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from xcvrd import xcvrd
from xcvrd.xcvrd_utilities import common, media_settings_parser


def install_chassis(monkeypatch, chassis):
    monkeypatch.setattr(common, 'platform_chassis', chassis)
    monkeypatch.setattr(xcvrd, 'platform_chassis', chassis)


def make_device(cmis):
    api = Mock(spec=common.CmisApi) if cmis else Mock(spec=['is_copper', 'is_flat_memory'])
    api.is_copper.return_value = not cmis
    api.is_flat_memory.return_value = not cmis
    if cmis:
        api.get_application_advertisement.return_value = {
            1: {'host_lane_count': 4, 'host_electrical_interface_id': '400GAUI-4 C2M'},
        }
    device = Mock(spec=['get_xcvr_api', 'get_transceiver_info',
                        'get_transceiver_info_firmware_versions', 'get_transceiver_pm'])
    device.get_xcvr_api.return_value = api
    device.get_transceiver_info.return_value = {
        'manufacturer': 'TEST', 'model': 'TEST-1',
        'cable_type': 'Length Cable Assembly(m)', 'cable_length': 0,
        'specification_compliance': 'sm_media_interface' if cmis else
                                    "{'10/40G Ethernet Compliance Code': '10GBase-SR'}",
        'type_abbrv_name': 'QSFP-DD' if cmis else 'QSFP+',
    }
    device.get_transceiver_info_firmware_versions.return_value = {'active': 'test'}
    device.get_transceiver_pm.return_value = {'test_monitor': 1}
    return device


@pytest.fixture
def mixed_chassis(monkeypatch):
    cpo, sfp = make_device(True), make_device(False)
    chassis = SimpleNamespace(
        get_cpo=Mock(side_effect=lambda port: cpo if port == 1 else None),
        get_sfp=Mock(side_effect=lambda port: sfp if port == 2 else None),
    )
    install_chassis(monkeypatch, chassis)
    return chassis, cpo, sfp


@pytest.mark.parametrize('port', [1, 2])
def test_shared_readers_select_the_requested_port(mixed_chassis, port):
    chassis, cpo, sfp = mixed_chassis
    device = cpo if port == 1 else sfp
    assert xcvrd._wrapper_get_transceiver_info(port) == device.get_transceiver_info.return_value
    assert common._wrapper_get_transceiver_firmware_info(port) == {'active': 'test'}
    assert common._wrapper_get_transceiver_pm(port) == {'test_monitor': 1}
    assert common.is_copper(port) is (port == 2)
    assert common._wrapper_is_flat_memory(port) is (port == 2)
    if port == 1:
        chassis.get_sfp.assert_not_called()
        sfp.get_xcvr_api.assert_not_called()
    else:
        cpo.get_xcvr_api.assert_not_called()


@pytest.mark.parametrize('port,expected', [
    (1, {'vendor_key': 'TEST-TEST-1', 'media_key': 'QSFP-DD-sm_media_interface',
         'lane_speed_key': 'speed:400GAUI-4', 'medium_lane_speed_key': 'OPTICAL100'}),
    (2, {'vendor_key': 'TEST-TEST-1', 'media_key': 'QSFP+-10GBase-SR',
         'lane_speed_key': 'speed:100G', 'medium_lane_speed_key': 'COPPER100'}),
])
def test_media_settings_select_cpo_or_sfp_api(mixed_chassis, port, expected):
    chassis, cpo, sfp = mixed_chassis
    device = cpo if port == 1 else sfp
    info = {port: device.get_transceiver_info.return_value}
    assert media_settings_parser.get_media_settings_key(port, info, 400000, 4) == expected
    if port == 1:
        chassis.get_sfp.assert_not_called()


@pytest.mark.parametrize('error', [NotImplementedError, AttributeError, IndexError])
def test_legacy_sfp_chassis_retains_shared_readers(monkeypatch, error):
    sfp = make_device(False)
    chassis = SimpleNamespace(get_cpo=Mock(side_effect=error), get_sfp=Mock(return_value=sfp))
    install_chassis(monkeypatch, chassis)
    assert common._wrapper_get_transceiver_firmware_info(2) == {'active': 'test'}
    assert common._wrapper_get_transceiver_pm(2) == {'test_monitor': 1}
    assert common._wrapper_is_flat_memory(2) is True
    assert common.is_copper(2) is True
    assert xcvrd._wrapper_get_transceiver_info(2) == sfp.get_transceiver_info.return_value


def test_chassis_without_cpo_accessor(monkeypatch):
    sfp = make_device(False)
    install_chassis(monkeypatch, SimpleNamespace(get_sfp=Mock(return_value=sfp)))
    assert common._wrapper_get_transceiver_firmware_info(2) == {'active': 'test'}
    assert xcvrd._wrapper_get_transceiver_info(2) == sfp.get_transceiver_info.return_value


@pytest.mark.parametrize('no_chassis', [False, True])
def test_missing_device_retains_defaults_and_legacy_info(monkeypatch, no_chassis):
    chassis = None if no_chassis else SimpleNamespace(
        get_cpo=Mock(return_value=None), get_sfp=Mock(return_value=None))
    install_chassis(monkeypatch, chassis)
    legacy = SimpleNamespace(get_transceiver_info_dict=Mock(return_value={'legacy': 'info'}))
    monkeypatch.setattr(xcvrd, 'platform_sfputil', legacy)
    assert xcvrd._wrapper_get_transceiver_info(3) == {'legacy': 'info'}
    legacy.get_transceiver_info_dict.assert_called_once_with(3)
    assert common._wrapper_get_transceiver_firmware_info(3) == {}
    assert common._wrapper_get_transceiver_pm(3) == {}
    assert common._wrapper_is_flat_memory(3) is None
    assert common.is_copper(3) is True


@pytest.mark.parametrize('method,reader', [
    ('get_transceiver_info_firmware_versions', common._wrapper_get_transceiver_firmware_info),
    ('get_transceiver_pm', common._wrapper_get_transceiver_pm),
])
@pytest.mark.parametrize('missing_method', [False, True])
def test_optional_cpo_readers_keep_unsupported_defaults(monkeypatch, method, reader, missing_method):
    device = SimpleNamespace() if missing_method else SimpleNamespace(
        **{method: Mock(side_effect=NotImplementedError)})
    install_chassis(monkeypatch, SimpleNamespace(
        get_cpo=Mock(return_value=device), get_sfp=Mock(side_effect=AssertionError)))
    assert reader(1) == {}


def test_no_api_retains_flat_memory_and_lane_speed_defaults(monkeypatch):
    device = SimpleNamespace(get_xcvr_api=Mock(return_value=None))
    install_chassis(monkeypatch, SimpleNamespace(
        get_cpo=Mock(return_value=device), get_sfp=Mock(side_effect=AssertionError)))
    assert common._wrapper_is_flat_memory(1) is True
    assert common.is_copper(1) is True
    assert media_settings_parser.get_lane_speed_key(1, 100000, 4) == 'speed:25G'
