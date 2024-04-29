#from unittest.mock import DEFAULT
from xcvrd.xcvrd_utilities.port_event_helper import *
from xcvrd.xcvrd_utilities.sfp_status_helper import *
from xcvrd.xcvrd_utilities.media_settings_parser import *
from xcvrd.xcvrd_utilities.optics_si_parser import *
from xcvrd.xcvrd import *
from xcvrd.sff_mgr import *
from xcvrd.xcvrd_utilities.xcvr_table_helper import *
import pytest
import copy
import os
import sys
import time

if sys.version_info >= (3, 3):
    from unittest.mock import MagicMock, patch
else:
    from mock import MagicMock, patch

from sonic_py_common import daemon_base
from swsscommon import swsscommon
from sonic_platform_base.sfp_base import SfpBase
from .mock_swsscommon import Table


daemon_base.db_connect = MagicMock()
swsscommon.Table = MagicMock()
swsscommon.ProducerStateTable = MagicMock()
swsscommon.SubscriberStateTable = MagicMock()
swsscommon.SonicDBConfig = MagicMock()
#swsscommon.Select = MagicMock()

test_path = os.path.dirname(os.path.abspath(__file__))
modules_path = os.path.dirname(test_path)
scripts_path = os.path.join(modules_path, "xcvrd")
sys.path.insert(0, modules_path)
DEFAULT_NAMESPACE = ['']

os.environ["XCVRD_UNIT_TESTING"] = "1"

with open(os.path.join(test_path, 'media_settings.json'), 'r') as f:
    media_settings_dict = json.load(f)

media_settings_with_comma_dict = copy.deepcopy(media_settings_dict)
global_media_settings = media_settings_with_comma_dict['GLOBAL_MEDIA_SETTINGS'].pop('1-32')
media_settings_with_comma_dict['GLOBAL_MEDIA_SETTINGS']['1-5,6,7-20,21-32'] = global_media_settings

with open(os.path.join(test_path, 'optics_si_settings.json'), 'r') as fn:
    optics_si_settings_dict = json.load(fn)
port_optics_si_settings = {}
optics_si_settings_with_comma_dict = copy.deepcopy(optics_si_settings_dict)
global_optics_si_settings = optics_si_settings_with_comma_dict['GLOBAL_MEDIA_SETTINGS'].pop('0-31')
port_optics_si_settings['PORT_MEDIA_SETTINGS'] = optics_si_settings_with_comma_dict.pop('PORT_MEDIA_SETTINGS')
optics_si_settings_with_comma_dict['GLOBAL_MEDIA_SETTINGS']['0-5,6,7-20,21-31'] = global_optics_si_settings

with open(os.path.join(test_path, 'media_settings_extended_format.json'), 'r') as f:
    media_settings_extended_format_dict = json.load(f)


# Creating instances of media_settings.json for testing purposes
# Each instance represents a different possible structure for media_settings.json.
media_settings_global_range_media_key_lane_speed_si = copy.deepcopy(media_settings_extended_format_dict)

media_settings_global_range_media_key_si = copy.deepcopy(media_settings_extended_format_dict)
media_settings_global_range_media_key_si['GLOBAL_MEDIA_SETTINGS']['0-31']['QSFP-DD-sm_media_interface'] = media_settings_global_range_media_key_si['GLOBAL_MEDIA_SETTINGS']['0-31']['QSFP-DD-sm_media_interface'].pop('speed:400GAUI-8')
media_settings_global_range_media_key_si['GLOBAL_MEDIA_SETTINGS']['0-31']['QSFP-DD-active_cable_media_interface'] = media_settings_global_range_media_key_si['GLOBAL_MEDIA_SETTINGS']['0-31']['QSFP-DD-active_cable_media_interface'].pop('speed:100GAUI-2')

media_settings_global_range_vendor_key_lane_speed_si = copy.deepcopy(media_settings_extended_format_dict)
media_settings_global_range_vendor_key_lane_speed_si['GLOBAL_MEDIA_SETTINGS']['0-31']['AMPHANOL-1234'] = media_settings_global_range_vendor_key_lane_speed_si['GLOBAL_MEDIA_SETTINGS']['0-31'].pop('QSFP-DD-sm_media_interface')
media_settings_global_range_vendor_key_lane_speed_si['GLOBAL_MEDIA_SETTINGS']['0-31']['AMPHANOL-5678'] = media_settings_global_range_vendor_key_lane_speed_si['GLOBAL_MEDIA_SETTINGS']['0-31'].pop('QSFP-DD-active_cable_media_interface')

media_settings_global_range_vendor_key_si = copy.deepcopy(media_settings_global_range_vendor_key_lane_speed_si)
media_settings_global_range_vendor_key_si['GLOBAL_MEDIA_SETTINGS']['0-31']['AMPHANOL-1234'] = media_settings_global_range_vendor_key_si['GLOBAL_MEDIA_SETTINGS']['0-31']['AMPHANOL-1234'].pop('speed:400GAUI-8')
media_settings_global_range_vendor_key_si['GLOBAL_MEDIA_SETTINGS']['0-31']['AMPHANOL-5678'] = media_settings_global_range_vendor_key_si['GLOBAL_MEDIA_SETTINGS']['0-31']['AMPHANOL-5678'].pop('speed:100GAUI-2')

media_settings_global_range_generic_vendor_key_lane_speed_si = copy.deepcopy(media_settings_extended_format_dict)
media_settings_global_range_generic_vendor_key_lane_speed_si['GLOBAL_MEDIA_SETTINGS']['0-31']['AMPHANOL-1234'] = media_settings_global_range_generic_vendor_key_lane_speed_si['GLOBAL_MEDIA_SETTINGS']['0-31'].pop('QSFP-DD-sm_media_interface')
media_settings_global_range_generic_vendor_key_lane_speed_si['GLOBAL_MEDIA_SETTINGS']['0-31']['GENERIC_VENDOR'] = media_settings_global_range_generic_vendor_key_lane_speed_si['GLOBAL_MEDIA_SETTINGS']['0-31'].pop('QSFP-DD-active_cable_media_interface')

media_settings_global_range_generic_vendor_key_si = copy.deepcopy(media_settings_global_range_generic_vendor_key_lane_speed_si)
media_settings_global_range_generic_vendor_key_si['GLOBAL_MEDIA_SETTINGS']['0-31']['AMPHANOL-1234'] = media_settings_global_range_generic_vendor_key_si['GLOBAL_MEDIA_SETTINGS']['0-31']['AMPHANOL-1234'].pop('speed:400GAUI-8')
media_settings_global_range_generic_vendor_key_si['GLOBAL_MEDIA_SETTINGS']['0-31']['GENERIC_VENDOR'] = media_settings_global_range_generic_vendor_key_si['GLOBAL_MEDIA_SETTINGS']['0-31']['GENERIC_VENDOR'].pop('speed:100GAUI-2')

media_settings_global_list_media_key_lane_speed_si = copy.deepcopy(media_settings_extended_format_dict)
new_key = str(','.join([str(i) for i in range(32)]))
media_settings_global_list_media_key_lane_speed_si['GLOBAL_MEDIA_SETTINGS'][new_key] = media_settings_global_list_media_key_lane_speed_si['GLOBAL_MEDIA_SETTINGS'].pop('0-31')

media_settings_global_list_media_key_si = copy.deepcopy(media_settings_global_range_media_key_si)
media_settings_global_list_media_key_si['GLOBAL_MEDIA_SETTINGS'][new_key] = media_settings_global_list_media_key_si['GLOBAL_MEDIA_SETTINGS'].pop('0-31')

media_settings_global_list_of_ranges_media_key_lane_speed_si = copy.deepcopy(media_settings_extended_format_dict)
media_settings_global_list_of_ranges_media_key_lane_speed_si['GLOBAL_MEDIA_SETTINGS']['0-15,16-31'] = media_settings_global_list_of_ranges_media_key_lane_speed_si['GLOBAL_MEDIA_SETTINGS'].pop('0-31')

media_settings_global_list_of_ranges_media_key_si = copy.deepcopy(media_settings_global_range_media_key_si)
media_settings_global_list_of_ranges_media_key_si['GLOBAL_MEDIA_SETTINGS']['0-15,16-31'] = media_settings_global_list_of_ranges_media_key_si['GLOBAL_MEDIA_SETTINGS'].pop('0-31')

media_settings_global_list_of_ranges_media_key_lane_speed_si_with_default_section = copy.deepcopy(media_settings_extended_format_dict)
media_settings_global_list_of_ranges_media_key_lane_speed_si_with_default_section['GLOBAL_MEDIA_SETTINGS']['0-31']['Default'] = {'speed:400GAUI-8': {'idriver': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}, 'pre1': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}, 'ob_m2lp': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}}}

media_settings_port_media_key_lane_speed_si = copy.deepcopy(media_settings_extended_format_dict)
media_settings_port_media_key_lane_speed_si['PORT_MEDIA_SETTINGS'] = {'7': media_settings_port_media_key_lane_speed_si['GLOBAL_MEDIA_SETTINGS'].pop('0-31')}
del media_settings_port_media_key_lane_speed_si['GLOBAL_MEDIA_SETTINGS']

media_settings_port_media_key_si = copy.deepcopy(media_settings_port_media_key_lane_speed_si)
media_settings_port_media_key_si['PORT_MEDIA_SETTINGS']['7']["QSFP-DD-sm_media_interface"] = media_settings_port_media_key_si['PORT_MEDIA_SETTINGS']['7']["QSFP-DD-sm_media_interface"].pop("speed:400GAUI-8")
media_settings_port_media_key_si['PORT_MEDIA_SETTINGS']['7']["QSFP-DD-active_cable_media_interface"] = media_settings_port_media_key_si['PORT_MEDIA_SETTINGS']['7']["QSFP-DD-active_cable_media_interface"].pop("speed:100GAUI-2")

media_settings_port_vendor_key_lane_speed_si = copy.deepcopy(media_settings_port_media_key_lane_speed_si)
media_settings_port_vendor_key_lane_speed_si['PORT_MEDIA_SETTINGS']['7']['AMPHANOL-1234'] = media_settings_port_vendor_key_lane_speed_si['PORT_MEDIA_SETTINGS']['7'].pop('QSFP-DD-sm_media_interface')
media_settings_port_vendor_key_lane_speed_si['PORT_MEDIA_SETTINGS']['7']['AMPHANOL-5678'] = media_settings_port_vendor_key_lane_speed_si['PORT_MEDIA_SETTINGS']['7'].pop('QSFP-DD-active_cable_media_interface')

media_settings_port_vendor_key_si = copy.deepcopy(media_settings_port_vendor_key_lane_speed_si)
media_settings_port_vendor_key_si['PORT_MEDIA_SETTINGS']['7']['AMPHANOL-1234'] = media_settings_port_vendor_key_si['PORT_MEDIA_SETTINGS']['7']['AMPHANOL-1234'].pop('speed:400GAUI-8')
media_settings_port_vendor_key_si['PORT_MEDIA_SETTINGS']['7']['AMPHANOL-5678'] = media_settings_port_vendor_key_si['PORT_MEDIA_SETTINGS']['7']['AMPHANOL-5678'].pop('speed:100GAUI-2')

media_settings_port_media_key_si = copy.deepcopy(media_settings_port_media_key_lane_speed_si)
media_settings_port_media_key_si['PORT_MEDIA_SETTINGS']['7']["QSFP-DD-sm_media_interface"] = media_settings_port_media_key_si['PORT_MEDIA_SETTINGS']['7']["QSFP-DD-sm_media_interface"].pop("speed:400GAUI-8")
media_settings_port_media_key_si['PORT_MEDIA_SETTINGS']['7']["QSFP-DD-active_cable_media_interface"] = media_settings_port_media_key_si['PORT_MEDIA_SETTINGS']['7']["QSFP-DD-active_cable_media_interface"].pop("speed:100GAUI-2")

media_settings_port_generic_vendor_key_lane_speed_si = copy.deepcopy(media_settings_port_media_key_lane_speed_si)
media_settings_port_generic_vendor_key_lane_speed_si['PORT_MEDIA_SETTINGS']['7']['AMPHANOL-1234'] = media_settings_port_generic_vendor_key_lane_speed_si['PORT_MEDIA_SETTINGS']['7'].pop('QSFP-DD-sm_media_interface')
media_settings_port_generic_vendor_key_lane_speed_si['PORT_MEDIA_SETTINGS']['7']['GENERIC_VENDOR'] = media_settings_port_generic_vendor_key_lane_speed_si['PORT_MEDIA_SETTINGS']['7'].pop('QSFP-DD-active_cable_media_interface')

media_settings_port_generic_vendor_key_si = copy.deepcopy(media_settings_port_generic_vendor_key_lane_speed_si)
media_settings_port_generic_vendor_key_si['PORT_MEDIA_SETTINGS']['7']['AMPHANOL-1234'] = media_settings_port_generic_vendor_key_si['PORT_MEDIA_SETTINGS']['7']['AMPHANOL-1234'].pop('speed:400GAUI-8')
media_settings_port_generic_vendor_key_si['PORT_MEDIA_SETTINGS']['7']['GENERIC_VENDOR'] = media_settings_port_generic_vendor_key_si['PORT_MEDIA_SETTINGS']['7']['GENERIC_VENDOR'].pop('speed:100GAUI-2')

media_settings_global_default_port_media_key_lane_speed_si = copy.deepcopy(media_settings_extended_format_dict)
port_media_settings_data = {'7': media_settings_global_default_port_media_key_lane_speed_si['GLOBAL_MEDIA_SETTINGS'].pop('0-31')}
media_settings_global_default_port_media_key_lane_speed_si['GLOBAL_MEDIA_SETTINGS'] = {'0-31': {'Default': {'speed:400GAUI-8': {'idriver': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}, 'pre1': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}, 'ob_m2lp': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}}}}}
media_settings_global_default_port_media_key_lane_speed_si['PORT_MEDIA_SETTINGS'] = port_media_settings_data

media_settings_port_default_media_key_lane_speed_si = copy.deepcopy(media_settings_port_media_key_lane_speed_si)
media_settings_port_default_media_key_lane_speed_si['PORT_MEDIA_SETTINGS']['7']['Default'] = {'speed:400GAUI-8': {'idriver': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}, 'pre1': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}, 'ob_m2lp': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}}}

media_settings_empty = {}


class TestXcvrdThreadException(object):

    @patch('xcvrd.sff_mgr.PortChangeObserver', MagicMock(side_effect=NotImplementedError))
    def test_SffManagerTask_task_run_with_exception(self):
        stop_event = threading.Event()
        sff_mgr = SffManagerTask(DEFAULT_NAMESPACE, stop_event, MagicMock(), helper_logger)
        exception_received = None
        trace = None
        try:
            sff_mgr.start()
            sff_mgr.join()
        except Exception as e1:
            exception_received = e1
            trace = traceback.format_exc()

        assert not sff_mgr.is_alive()
        assert(type(exception_received) == NotImplementedError)
        assert("NotImplementedError" in str(trace) and "effect" in str(trace))
        assert("sonic-xcvrd/xcvrd/sff_mgr.py" in str(trace))
        assert("PortChangeObserver" in str(trace))

    @patch('xcvrd.xcvrd.platform_chassis', MagicMock())
    def test_CmisManagerTask_task_run_with_exception(self):
        port_mapping = PortMapping()
        stop_event = threading.Event()
        cmis_manager = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event)
        cmis_manager.wait_for_port_config_done = MagicMock(side_effect = NotImplementedError)
        exception_received = None
        trace = None
        try:
            cmis_manager.start()
            cmis_manager.join()
        except Exception as e1:
            exception_received = e1
            trace = traceback.format_exc()

        assert not cmis_manager.is_alive()
        assert(type(exception_received) == NotImplementedError)
        assert("NotImplementedError" in str(trace) and "effect" in str(trace))
        assert("sonic-xcvrd/xcvrd/xcvrd.py" in str(trace))
        assert("wait_for_port_config_done" in str(trace))

    @patch('xcvrd.xcvrd_utilities.port_event_helper.subscribe_port_config_change', MagicMock(side_effect = NotImplementedError))
    def test_DomInfoUpdateTask_task_run_with_exception(self):
        port_mapping = PortMapping()
        stop_event = threading.Event()
        mock_cmis_manager = MagicMock()
        dom_info_update = DomInfoUpdateTask(DEFAULT_NAMESPACE, port_mapping, stop_event, mock_cmis_manager)
        exception_received = None
        trace = None
        try:
            dom_info_update.start()
            dom_info_update.join()
        except Exception as e1:
            exception_received = e1
            trace = traceback.format_exc()

        assert not dom_info_update.is_alive()
        assert(type(exception_received) == NotImplementedError)
        assert("NotImplementedError" in str(trace) and "effect" in str(trace))
        assert("sonic-xcvrd/xcvrd/xcvrd.py" in str(trace))
        assert("subscribe_port_config_change" in str(trace))

    @patch('xcvrd.xcvrd.SfpStateUpdateTask.init', MagicMock())
    @patch('xcvrd.xcvrd_utilities.port_event_helper.subscribe_port_config_change', MagicMock(side_effect = NotImplementedError))
    def test_SfpStateUpdateTask_task_run_with_exception(self):
        port_mapping = PortMapping()
        stop_event = threading.Event()
        sfp_error_event = threading.Event()
        sfp_state_update = SfpStateUpdateTask(DEFAULT_NAMESPACE, port_mapping, stop_event, sfp_error_event)
        exception_received = None
        trace = None
        try:
            sfp_state_update.start()
            sfp_state_update.join()
        except Exception as e1:
            exception_received = e1
            trace = traceback.format_exc()

        assert not sfp_state_update.is_alive()
        assert(type(exception_received) == NotImplementedError)
        assert("NotImplementedError" in str(trace) and "effect" in str(trace))
        assert("sonic-xcvrd/xcvrd/xcvrd.py" in str(trace))
        assert("subscribe_port_config_change" in str(trace))

    @patch('xcvrd.xcvrd.SfpStateUpdateTask.is_alive', MagicMock(return_value = False))
    @patch('xcvrd.xcvrd.DomInfoUpdateTask.is_alive', MagicMock(return_value = False))
    @patch('xcvrd.xcvrd.CmisManagerTask.is_alive', MagicMock(return_value = False))
    @patch('xcvrd.xcvrd.SffManagerTask.is_alive', MagicMock(return_value=False))
    @patch('xcvrd.xcvrd.CmisManagerTask.join', MagicMock(side_effect=NotImplementedError))
    @patch('xcvrd.xcvrd.CmisManagerTask.start', MagicMock())
    @patch('xcvrd.xcvrd.SffManagerTask.start', MagicMock())
    @patch('xcvrd.xcvrd.DomInfoUpdateTask.start', MagicMock())
    @patch('xcvrd.xcvrd.SfpStateUpdateTask.start', MagicMock())
    @patch('xcvrd.xcvrd.DaemonXcvrd.deinit', MagicMock())
    @patch('os.kill')
    @patch('xcvrd.xcvrd.DaemonXcvrd.init')
    @patch('xcvrd.xcvrd.DomInfoUpdateTask.join')
    @patch('xcvrd.xcvrd.SfpStateUpdateTask.join')
    @patch('xcvrd.xcvrd.SffManagerTask.join')
    def test_DaemonXcvrd_run_with_exception(self, mock_task_join_sff, mock_task_join_sfp,
                                            mock_task_join_dom, mock_init, mock_os_kill):
        mock_init.return_value = (PortMapping(), set())
        xcvrd = DaemonXcvrd(SYSLOG_IDENTIFIER)
        xcvrd.enable_sff_mgr = True
        xcvrd.load_feature_flags = MagicMock()
        xcvrd.stop_event.wait = MagicMock()
        xcvrd.run()

        assert len(xcvrd.threads) == 4
        assert mock_init.call_count == 1
        assert mock_task_join_sff.call_count == 1
        assert mock_task_join_sfp.call_count == 1
        assert mock_task_join_dom.call_count == 1
        assert mock_os_kill.call_count == 1

class TestXcvrdScript(object):

    from sonic_platform_base.sonic_xcvr.api.public.c_cmis import CCmisApi
    from sonic_platform_base.sonic_xcvr.api.public.sff8636 import Sff8636Api
    from sonic_platform_base.sonic_xcvr.api.public.sff8436 import Sff8436Api
    @pytest.mark.parametrize("mock_class, expected_return_value", [
        (CmisApi, True),
        (CCmisApi, True),
        (Sff8636Api, False),
        (Sff8436Api, False)
    ])
    def test_is_cmis_api(self, mock_class, expected_return_value):
        mock_xcvr_api = MagicMock()
        mock_xcvr_api.__class__ = mock_class
        assert is_cmis_api(mock_xcvr_api) == expected_return_value

    @patch('xcvrd.xcvrd._wrapper_get_sfp_type')
    @patch('xcvrd.xcvrd_utilities.port_event_helper.PortMapping.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd._wrapper_get_presence', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd._wrapper_get_transceiver_dom_info', MagicMock(return_value={'temperature': '22.75',
                                                                                    'voltage': '0.5',
                                                                                    'rx1power': '0.7',
                                                                                    'rx2power': '0.7',
                                                                                    'rx3power': '0.7',
                                                                                    'rx4power': '0.7',
                                                                                    'rx5power': '0.7',
                                                                                    'rx6power': '0.7',
                                                                                    'rx7power': '0.7',
                                                                                    'rx8power': '0.7',
                                                                                    'tx1bias': '0.7',
                                                                                    'tx2bias': '0.7',
                                                                                    'tx3bias': '0.7',
                                                                                    'tx4bias': '0.7',
                                                                                    'tx5bias': '0.7',
                                                                                    'tx6bias': '0.7',
                                                                                    'tx7bias': '0.7',
                                                                                    'tx8bias': '0.7',
                                                                                    'tx1power': '0.7',
                                                                                    'tx2power': '0.7',
                                                                                    'tx3power': '0.7',
                                                                                    'tx4power': '0.7',
                                                                                    'tx5power': '0.7',
                                                                                    'tx6power': '0.7',
                                                                                    'tx7power': '0.7',
                                                                                    'tx8power': '0.7', }))
    def test_post_port_dom_info_to_db(self, mock_get_sfp_type):
        logical_port_name = "Ethernet0"
        port_mapping = PortMapping()
        stop_event = threading.Event()
        dom_tbl = Table("STATE_DB", TRANSCEIVER_DOM_SENSOR_TABLE)
        post_port_dom_info_to_db(logical_port_name, port_mapping, dom_tbl, stop_event)
        mock_get_sfp_type.return_value = 'QSFP_DD'
        post_port_dom_info_to_db(logical_port_name, port_mapping, dom_tbl, stop_event)

    @patch('xcvrd.xcvrd_utilities.port_event_helper.PortMapping.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd._wrapper_get_presence', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd._wrapper_get_transceiver_firmware_info', MagicMock(return_value={'active_firmware': '2.1.1',
                                                                              'inactive_firmware': '1.2.4'}))
    def test_post_port_sfp_firmware_info_to_db(self):
        logical_port_name = "Ethernet0"
        port_mapping = PortMapping()
        stop_event = threading.Event()
        firmware_info_tbl = Table("STATE_DB", TRANSCEIVER_FIRMWARE_INFO_TABLE)
        assert firmware_info_tbl.get_size() == 0
        post_port_sfp_firmware_info_to_db(logical_port_name, port_mapping, firmware_info_tbl, stop_event)
        assert firmware_info_tbl.get_size_for_key(logical_port_name) == 2

    def test_post_port_dom_threshold_info_to_db(self, mock_get_sfp_type):
        logical_port_name = "Ethernet0"
        port_mapping = PortMapping()
        stop_event = threading.Event()
        dom_threshold_tbl = Table("STATE_DB", TRANSCEIVER_DOM_THRESHOLD_TABLE)
        post_port_dom_info_to_db(logical_port_name, port_mapping, dom_threshold_tbl, stop_event)
        mock_get_sfp_type.return_value = 'QSFP_DD'
        post_port_dom_info_to_db(logical_port_name, port_mapping, dom_threshold_tbl, stop_event)

    @patch('xcvrd.xcvrd_utilities.port_event_helper.PortMapping.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd._wrapper_get_presence', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd._wrapper_get_transceiver_pm', MagicMock(return_value={'prefec_ber_avg': '0.0003407240007014899',
                                                                              'prefec_ber_min': '0.0006814479342250317',
                                                                              'prefec_ber_max': '0.0006833674050752236',
                                                                              'uncorr_frames_avg': '0.0',
                                                                              'uncorr_frames_min': '0.0',
                                                                              'uncorr_frames_max': '0.0', }))
    def test_post_port_pm_info_to_db(self):
        logical_port_name = "Ethernet0"
        port_mapping = PortMapping()
        stop_event = threading.Event()
        pm_tbl = Table("STATE_DB", TRANSCEIVER_PM_TABLE)
        assert pm_tbl.get_size() == 0
        post_port_pm_info_to_db(logical_port_name, port_mapping, pm_tbl, stop_event)
        assert pm_tbl.get_size_for_key(logical_port_name) == 6

    @patch('xcvrd.xcvrd_utilities.port_event_helper.PortMapping.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd._wrapper_get_presence', MagicMock(return_value=True))
    def test_del_port_sfp_dom_info_from_db(self):
        logical_port_name = "Ethernet0"
        port_mapping = PortMapping()
        dom_tbl = Table("STATE_DB", TRANSCEIVER_DOM_SENSOR_TABLE)
        dom_threshold_tbl = Table("STATE_DB", TRANSCEIVER_DOM_THRESHOLD_TABLE)
        init_tbl = Table("STATE_DB", TRANSCEIVER_INFO_TABLE)
        pm_tbl = Table("STATE_DB", TRANSCEIVER_PM_TABLE)
        firmware_info_tbl = Table("STATE_DB", TRANSCEIVER_FIRMWARE_INFO_TABLE)
        del_port_sfp_dom_info_from_db(logical_port_name, port_mapping, init_tbl, dom_tbl, dom_threshold_tbl, pm_tbl, firmware_info_tbl)

    @pytest.mark.parametrize("mock_found, mock_status_dict, expected_cmis_state", [
        (True, {'cmis_state': CMIS_STATE_INSERTED}, CMIS_STATE_INSERTED),
        (False, {}, CMIS_STATE_UNKNOWN),
        (True, {'other_key': 'some_value'}, CMIS_STATE_UNKNOWN)
    ])
    def test_get_cmis_state_from_state_db(self, mock_found, mock_status_dict, expected_cmis_state):
        status_tbl = MagicMock()
        status_tbl.get.return_value = (mock_found, mock_status_dict)
        assert get_cmis_state_from_state_db("Ethernet0", status_tbl) == expected_cmis_state

    @patch('xcvrd.xcvrd.get_physical_port_name_dict', MagicMock(return_value={0: 'Ethernet0'}))
    @patch('xcvrd.xcvrd._wrapper_get_presence', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd._wrapper_get_transceiver_status', MagicMock(return_value={'module_state': 'ModuleReady',
                                                                                  'module_fault_cause': 'No Fault detected',
                                                                                  'datapath_firmware_fault': 'False',
                                                                                  'module_firmware_fault': 'False',
                                                                                  'module_state_changed': 'True'}))
    def test_update_port_transceiver_status_table_hw(self):
        logical_port_name = "Ethernet0"
        port_mapping = PortMapping()
        stop_event = threading.Event()
        status_tbl = Table("STATE_DB", TRANSCEIVER_STATUS_TABLE)
        assert status_tbl.get_size() == 0
        update_port_transceiver_status_table_hw(logical_port_name, port_mapping, status_tbl, stop_event)
        assert status_tbl.get_size_for_key(logical_port_name) == 5

    @patch('xcvrd.xcvrd.get_physical_port_name_dict', MagicMock(return_value={0: 'Ethernet0'}))
    def test_delete_port_from_status_table_hw(self):
        logical_port_name = "Ethernet0"
        port_mapping = PortMapping()
        status_tbl = Table("STATE_DB", TRANSCEIVER_STATUS_TABLE)
        status_tbl.set(logical_port_name,
                swsscommon.FieldValuePairs([('status', '1'), ('error', 'N/A'), ('module_state', 'ModuleReady')]))
        assert status_tbl.get_size_for_key(logical_port_name) == 3
        delete_port_from_status_table_hw(logical_port_name, port_mapping, status_tbl)
        assert status_tbl.get_size_for_key(logical_port_name) == 2

    def test_delete_port_from_status_table_sw(self):
        logical_port_name = "Ethernet0"
        status_tbl = Table("STATE_DB", TRANSCEIVER_STATUS_TABLE)
        status_tbl.set(logical_port_name,
                swsscommon.FieldValuePairs([('status', '1'), ('error', 'N/A'), ('module_state', 'ModuleReady')]))
        assert status_tbl.get_size_for_key(logical_port_name) == 3
        delete_port_from_status_table_sw(logical_port_name, status_tbl)
        assert status_tbl.get_size_for_key(logical_port_name) == 1

    @patch('xcvrd.xcvrd_utilities.port_event_helper.PortMapping.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd._wrapper_get_presence', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd._wrapper_get_transceiver_dom_threshold_info', MagicMock(return_value={'temphighalarm': '22.75',
                                                                                              'temphighwarning': '0.5',
                                                                                              'templowalarm': '0.7',
                                                                                              'templowwarning': '0.7',
                                                                                              'vcchighalarm': '0.7',
                                                                                              'vcchighwarning': '0.7',
                                                                                              'vcclowalarm': '0.7',
                                                                                              'vcclowwarning': '0.7',
                                                                                              'txpowerhighalarm': '0.7',
                                                                                              'txpowerlowalarm': '0.7',
                                                                                              'txpowerhighwarning': '0.7',
                                                                                              'txpowerlowwarning': '0.7',
                                                                                              'rxpowerhighalarm': '0.7',
                                                                                              'rxpowerlowalarm': '0.7',
                                                                                              'rxpowerhighwarning': '0.7',
                                                                                              'rxpowerlowwarning': '0.7',
                                                                                              'txbiashighalarm': '0.7',
                                                                                              'txbiaslowalarm': '0.7',
                                                                                              'txbiashighwarning': '0.7',
                                                                                              'txbiaslowwarning': '0.7', }))
    def test_post_port_dom_threshold_info_to_db(self):
        logical_port_name = "Ethernet0"
        port_mapping = PortMapping()
        stop_event = threading.Event()
        dom_threshold_tbl = Table("STATE_DB", TRANSCEIVER_DOM_THRESHOLD_TABLE)
        post_port_dom_threshold_info_to_db(logical_port_name, port_mapping, dom_threshold_tbl, stop_event)

    @patch('xcvrd.xcvrd_utilities.port_event_helper.PortMapping.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd._wrapper_get_presence', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd._wrapper_is_replaceable', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd._wrapper_get_transceiver_info', MagicMock(return_value={'type': '22.75',
                                                                                'vendor_rev': '0.5',
                                                                                'serial': '0.7',
                                                                                'manufacturer': '0.7',
                                                                                'model': '0.7',
                                                                                'vendor_oui': '0.7',
                                                                                'vendor_date': '0.7',
                                                                                'connector': '0.7',
                                                                                'encoding': '0.7',
                                                                                'ext_identifier': '0.7',
                                                                                'ext_rateselect_compliance': '0.7',
                                                                                'cable_type': '0.7',
                                                                                'cable_length': '0.7',
                                                                                'specification_compliance': '0.7',
                                                                                'nominal_bit_rate': '0.7',
                                                                                'application_advertisement': '0.7',
                                                                                'is_replaceable': '0.7',
                                                                                'dom_capability': '0.7',
                                                                                'active_firmware': '1.1',
                                                                                'inactive_firmware': '1.0',
                                                                                'hardware_rev': '1.0',
                                                                                'media_interface_code': '0.1',
                                                                                'host_electrical_interface': '0.1',
                                                                                'host_lane_count': 8,
                                                                                'media_lane_count': 1,
                                                                                'host_lane_assignment_option': 1,
                                                                                'media_lane_assignment_option': 1,
                                                                                'active_apsel_hostlane1': 1,
                                                                                'active_apsel_hostlane2': 1,
                                                                                'active_apsel_hostlane3': 1,
                                                                                'active_apsel_hostlane4': 1,
                                                                                'active_apsel_hostlane5': 1,
                                                                                'active_apsel_hostlane6': 1,
                                                                                'active_apsel_hostlane7': 1,
                                                                                'active_apsel_hostlane8': 1,
                                                                                'media_interface_technology': '1',
                                                                                'cmis_rev': '5.0',
                                                                                'supported_max_tx_power': 1.0,
                                                                                'supported_min_tx_power': -15.0,
                                                                                'supported_max_laser_freq': 196100,
                                                                                'supported_min_laser_freq': 191300}))
    def test_post_port_sfp_info_to_db(self):
        logical_port_name = "Ethernet0"
        port_mapping = PortMapping()
        stop_event = threading.Event()
        dom_tbl = Table("STATE_DB", TRANSCEIVER_DOM_SENSOR_TABLE)
        transceiver_dict = {}
        post_port_sfp_info_to_db(logical_port_name, port_mapping, dom_tbl, transceiver_dict, stop_event)

    @patch('xcvrd.xcvrd_utilities.port_event_helper.PortMapping.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd.platform_sfputil', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd._wrapper_get_presence', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd._wrapper_is_replaceable', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd.XcvrTableHelper', MagicMock())
    @patch('xcvrd.xcvrd._wrapper_get_transceiver_info', MagicMock(return_value={'type': '22.75',
                                                                                'vendor_rev': '0.5',
                                                                                'serial': '0.7',
                                                                                'manufacturer': '0.7',
                                                                                'model': '0.7',
                                                                                'vendor_oui': '0.7',
                                                                                'vendor_date': '0.7',
                                                                                'connector': '0.7',
                                                                                'encoding': '0.7',
                                                                                'ext_identifier': '0.7',
                                                                                'ext_rateselect_compliance': '0.7',
                                                                                'cable_type': '0.7',
                                                                                'cable_length': '0.7',
                                                                                'specification_compliance': '0.7',
                                                                                'nominal_bit_rate': '0.7',
                                                                                'application_advertisement': '0.7',
                                                                                'is_replaceable': '0.7',
                                                                                'dom_capability': '0.7', }))
    @patch('xcvrd.xcvrd._wrapper_get_transceiver_dom_threshold_info', MagicMock(return_value={'temphighalarm': '22.75',
                                                                                              'temphighwarning': '0.5',
                                                                                              'templowalarm': '0.7',
                                                                                              'templowwarning': '0.7',
                                                                                              'vcchighalarm': '0.7',
                                                                                              'vcchighwarning': '0.7',
                                                                                              'vcclowalarm': '0.7',
                                                                                              'vcclowwarning': '0.7',
                                                                                              'txpowerhighalarm': '0.7',
                                                                                              'txpowerlowalarm': '0.7',
                                                                                              'txpowerhighwarning': '0.7',
                                                                                              'txpowerlowwarning': '0.7',
                                                                                              'rxpowerhighalarm': '0.7',
                                                                                              'rxpowerlowalarm': '0.7',
                                                                                              'rxpowerhighwarning': '0.7',
                                                                                              'rxpowerlowwarning': '0.7',
                                                                                              'txbiashighalarm': '0.7',
                                                                                              'txbiaslowalarm': '0.7',
                                                                                              'txbiashighwarning': '0.7',
                                                                                              'txbiaslowwarning': '0.7', }))
    @patch('xcvrd.xcvrd._wrapper_get_transceiver_dom_info', MagicMock(return_value={'temperature': '22.75',
                                                                                    'voltage': '0.5',
                                                                                    'rx1power': '0.7',
                                                                                    'rx2power': '0.7',
                                                                                    'rx3power': '0.7',
                                                                                    'rx4power': '0.7',
                                                                                    'rx5power': '0.7',
                                                                                    'rx6power': '0.7',
                                                                                    'rx7power': '0.7',
                                                                                    'rx8power': '0.7',
                                                                                    'tx1bias': '0.7',
                                                                                    'tx2bias': '0.7',
                                                                                    'tx3bias': '0.7',
                                                                                    'tx4bias': '0.7',
                                                                                    'tx5bias': '0.7',
                                                                                    'tx6bias': '0.7',
                                                                                    'tx7bias': '0.7',
                                                                                    'tx8bias': '0.7',
                                                                                    'tx1power': '0.7',
                                                                                    'tx2power': '0.7',
                                                                                    'tx3power': '0.7',
                                                                                    'tx4power': '0.7',
                                                                                    'tx5power': '0.7',
                                                                                    'tx6power': '0.7',
                                                                                    'tx7power': '0.7',
                                                                                    'tx8power': '0.7', }))
    @patch('swsscommon.swsscommon.WarmStart', MagicMock())
    def test_post_port_sfp_info_and_dom_thr_to_db_once(self):
        port_mapping = PortMapping()
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_ADD)
        port_mapping.handle_port_change_event(port_change_event)
        stop_event = threading.Event()
        xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        sfp_error_event = threading.Event()
        task = SfpStateUpdateTask(DEFAULT_NAMESPACE, port_mapping, stop_event, sfp_error_event)
        task._post_port_sfp_info_and_dom_thr_to_db_once(port_mapping, xcvr_table_helper, stop_event)

    @patch('xcvrd.xcvrd_utilities.port_event_helper.PortMapping.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd.platform_sfputil', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd._wrapper_get_presence', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd._wrapper_is_replaceable', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd.XcvrTableHelper', MagicMock())
    def test_init_port_sfp_status_tbl(self):
        port_mapping = PortMapping()
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_ADD)
        port_mapping.handle_port_change_event(port_change_event)
        stop_event = threading.Event()
        xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        sfp_error_event = threading.Event()
        task = SfpStateUpdateTask(DEFAULT_NAMESPACE, port_mapping, stop_event, sfp_error_event)
        task._init_port_sfp_status_tbl(port_mapping, xcvr_table_helper, stop_event)

    @patch('sonic_py_common.device_info.get_paths_to_platform_and_hwsku_dirs', MagicMock(return_value=('/invalid/path', '/invalid/path')))
    def test_load_media_settings_missing_file(self):
        assert media_settings_parser.load_media_settings() == {}

    @patch('sonic_py_common.device_info.get_paths_to_platform_and_hwsku_dirs', MagicMock(return_value=('/invalid/path', '/invalid/path')))
    def test_load_optical_si_settings_missing_file(self):
        assert optics_si_parser.load_optics_si_settings() == {}

    @patch('xcvrd.xcvrd.platform_chassis')
    @patch('xcvrd.xcvrd.is_cmis_api')
    def test_get_media_settings_key(self, mock_is_cmis_api, mock_chassis):
        mock_sfp = MagicMock()
        mock_chassis.get_sfp = MagicMock(return_value=mock_sfp)
        mock_api = MagicMock()
        mock_sfp.get_xcvr_api = MagicMock(return_value=mock_api)
        mock_is_cmis_api.return_value = False

        xcvr_info_dict = {
            0: {
                'manufacturer': 'Molex',
                'model': '1064141421',
                'cable_type': 'Length Cable Assembly(m)',
                'cable_length': '255',
                'specification_compliance': "{'10/40G Ethernet Compliance Code': '10GBase-SR'}",
                'type_abbrv_name': 'QSFP+'
            }
        }

        # Test a good 'specification_compliance' value
        result = media_settings_parser.get_media_settings_key(0, xcvr_info_dict, 100000, 2)
        assert result == { 'vendor_key': 'MOLEX-1064141421', 'media_key': 'QSFP+-10GBase-SR-255M', 'lane_speed_key': None }

        # Test a bad 'specification_compliance' value
        xcvr_info_dict[0]['specification_compliance'] = 'N/A'
        result = media_settings_parser.get_media_settings_key(0, xcvr_info_dict, 100000, 2)
        assert result == { 'vendor_key': 'MOLEX-1064141421', 'media_key': 'QSFP+-*', 'lane_speed_key': None }
        # TODO: Ensure that error message was logged

        mock_is_cmis_api.return_value = True
        xcvr_info_dict = {
            0: {
                'manufacturer': 'Molex',
                'model': '1064141421',
                'cable_type': 'Length Cable Assembly(m)',
                'cable_length': '255',
                'specification_compliance': "sm_media_interface",
                'type_abbrv_name': 'QSFP-DD'
            }
        }

        mock_app_adv_value ={
        1: {'host_electrical_interface_id': '400G CR8', 'module_media_interface_id': 'Copper cable', 'media_lane_count': 8, 'host_lane_count': 8, 'host_lane_assignment_options': 1},
        2: {'host_electrical_interface_id': '200GBASE-CR4 (Clause 136)', 'module_media_interface_id': 'Copper cable', 'media_lane_count': 4, 'host_lane_count': 4, 'host_lane_assignment_options': 17},
        3: {'host_electrical_interface_id': '100GBASE-CR2 (Clause 136)', 'module_media_interface_id': 'Copper cable', 'media_lane_count': 2, 'host_lane_count': 2, 'host_lane_assignment_options': 85},
        4: {'host_electrical_interface_id': '100GBASE-CR4 (Clause 92)', 'module_media_interface_id': 'Copper cable', 'media_lane_count': 4, 'host_lane_count': 4, 'host_lane_assignment_options': 17},
        5: {'host_electrical_interface_id': '50GBASE-CR (Clause 126)', 'module_media_interface_id': 'Copper cable', 'media_lane_count': 1, 'host_lane_count': 1, 'host_lane_assignment_options': 255},
        6: {'host_electrical_interface_id': '40GBASE-CR4 (Clause 85)', 'module_media_interface_id': 'Copper cable', 'media_lane_count': 4, 'host_lane_count': 4, 'host_lane_assignment_options': 17},
        7: {'host_electrical_interface_id': '25GBASE-CR CA-N (Clause 110)', 'module_media_interface_id': 'Copper cable', 'media_lane_count': 1, 'host_lane_count': 1, 'host_lane_assignment_options': 255},
        8: {'host_electrical_interface_id': '1000BASE -CX(Clause 39)', 'module_media_interface_id': 'Copper cable', 'media_lane_count': 1, 'host_lane_count': 1, 'host_lane_assignment_options': 255}
        }

        mock_api.get_application_advertisement = MagicMock(return_value=mock_app_adv_value)
        result = media_settings_parser.get_media_settings_key(0, xcvr_info_dict, 100000, 2)
        assert result == { 'vendor_key': 'MOLEX-1064141421', 'media_key': 'QSFP-DD-sm_media_interface', 'lane_speed_key': 'speed:100GBASE-CR2' }

    @pytest.mark.parametrize("data_found, data, expected", [
        (True, [('speed', '400000'), ('lanes', '1,2,3,4,5,6,7,8'), ('mtu', '9100')], ('400000', 8)),
        (True, [('lanes', '1,2,3,4,5,6,7,8'), ('mtu', '9100')], ('0', 0)),
        (True, [('speed', '400000'), ('mtu', '9100')], ('0', 0)),
        (False, [], ('0', 0))
    ])
    def test_get_speed_and_lane_count(self, data_found, data, expected):
        cfg_port_tbl = MagicMock()
        cfg_port_tbl.get = MagicMock(return_value=(data_found, data))
        port = MagicMock()

        assert media_settings_parser.get_speed_and_lane_count(port, cfg_port_tbl) == expected

    def test_is_si_per_speed_supported(self):
        media_dict = {
            'speed:400G-GAUI-4':{
            'main':{
                    'lane0': '0x00000000',
                    'lane1': '0x00000000',
                    'lane2': '0x00000000',
                    'lane3': '0x00000000',
                    'lane4': '0x00000000',
                    'lane5': '0x00000000',
                    'lane6': '0x00000000',
                    'lane7': '0x00000000'
                }
            },
            'speed:400GAUI-8':{
                'post1':{
                    'lane0': '0x00000000',
                    'lane1': '0x00000000',
                    'lane2': '0x00000000',
                    'lane3': '0x00000000',
                    'lane4': '0x00000000',
                    'lane5': '0x00000000',
                    'lane6': '0x00000000',
                    'lane7': '0x00000000'
                }
            }
        }
        result = is_si_per_speed_supported(media_dict)
        assert result == True

        media_dict = {
            'main':{
                'lane0': '0x00000000',
                'lane1': '0x00000000',
                'lane2': '0x00000000',
                'lane3': '0x00000000',
                'lane4': '0x00000000',
                'lane5': '0x00000000',
                'lane6': '0x00000000',
                'lane7': '0x00000000'
            },
            'post1':{
                'lane0': '0x00000000',
                'lane1': '0x00000000',
                'lane2': '0x00000000',
                'lane3': '0x00000000',
                'lane4': '0x00000000',
                'lane5': '0x00000000',
                'lane6': '0x00000000',
                'lane7': '0x00000000'
            }
        }
        result = is_si_per_speed_supported(media_dict)
        assert result == False

    @pytest.mark.parametrize("media_settings_dict, port, key, expected", [
    (media_settings_global_range_media_key_lane_speed_si, 7, {'vendor_key': 'UNKOWN', 'media_key': 'QSFP-DD-active_cable_media_interface', 'lane_speed_key': 'speed:100GAUI-2'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_global_range_media_key_si, 7, {'vendor_key': 'UNKOWN', 'media_key': 'QSFP-DD-active_cable_media_interface', 'lane_speed_key': 'UNKOWN'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_global_range_vendor_key_lane_speed_si, 7, {'vendor_key': 'AMPHANOL-5678', 'media_key': 'UNKOWN', 'lane_speed_key': 'speed:100GAUI-2'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_global_range_vendor_key_lane_speed_si, 7, {'vendor_key': 'AMPHANOL-5678', 'media_key': 'UNKOWN', 'lane_speed_key': 'MISSING'}, {}),
    (media_settings_global_range_vendor_key_si, 7, {'vendor_key': 'AMPHANOL-5678', 'media_key': 'UNKOWN', 'lane_speed_key': 'UNKOWN'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_global_range_generic_vendor_key_lane_speed_si, 7, {'vendor_key': 'GENERIC_VENDOR-1234', 'media_key': 'UNKOWN', 'lane_speed_key': 'speed:100GAUI-2'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_global_range_generic_vendor_key_lane_speed_si, 7, {'vendor_key': 'GENERIC_VENDOR-1234', 'media_key': 'UNKOWN', 'lane_speed_key': 'MISSING'}, {}),
    (media_settings_global_range_generic_vendor_key_si, 7, {'vendor_key': 'GENERIC_VENDOR-1234', 'media_key': 'UNKOWN', 'lane_speed_key': 'UNKOWN'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_global_list_media_key_lane_speed_si, 7, {'vendor_key': 'UNKOWN', 'media_key': 'QSFP-DD-active_cable_media_interface', 'lane_speed_key': 'speed:100GAUI-2'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_global_list_media_key_lane_speed_si, 7, {'vendor_key': 'UNKOWN', 'media_key': 'QSFP-DD-active_cable_media_interface', 'lane_speed_key': 'MISSING'}, {}),
    (media_settings_global_list_media_key_si, 7, {'vendor_key': 'UNKOWN', 'media_key': 'QSFP-DD-active_cable_media_interface', 'lane_speed_key': 'UNKOWN'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_global_list_of_ranges_media_key_lane_speed_si, 7, {'vendor_key': 'UNKOWN', 'media_key': 'QSFP-DD-active_cable_media_interface', 'lane_speed_key': 'speed:100GAUI-2'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_global_list_of_ranges_media_key_si, 7, {'vendor_key': 'UNKOWN', 'media_key': 'QSFP-DD-active_cable_media_interface', 'lane_speed_key': 'UNKOWN'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_global_default_port_media_key_lane_speed_si, 6, {'vendor_key': 'AMPHANOL-5678', 'media_key': 'UNKOWN', 'lane_speed_key': 'speed:100GAUI-2'}, {'speed:400GAUI-8': {'idriver': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}, 'pre1': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}, 'ob_m2lp': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}}}),
    (media_settings_port_vendor_key_lane_speed_si, -1, {'vendor_key': 'AMPHANOL-5678', 'media_key': 'UNKOWN', 'lane_speed_key': 'speed:100GAUI-2'}, {}),
    (media_settings_port_media_key_lane_speed_si, 7, {'vendor_key': 'UNKOWN', 'media_key': 'QSFP-DD-active_cable_media_interface', 'lane_speed_key': 'speed:100GAUI-2'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_port_media_key_lane_speed_si, 7, {'vendor_key': 'UNKOWN', 'media_key': 'QSFP-DD-active_cable_media_interface', 'lane_speed_key': 'MISSING'}, {}),
    (media_settings_port_media_key_si, 7, {'vendor_key': 'UNKOWN', 'media_key': 'QSFP-DD-active_cable_media_interface', 'lane_speed_key': 'UNKOWN'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_port_vendor_key_lane_speed_si, 7, {'vendor_key': 'AMPHANOL-5678', 'media_key': 'UNKOWN', 'lane_speed_key': 'speed:100GAUI-2'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_port_vendor_key_lane_speed_si, 7, {'vendor_key': 'AMPHANOL-5678', 'media_key': 'UNKOWN', 'lane_speed_key': 'MISSING'}, {}),
    (media_settings_port_generic_vendor_key_lane_speed_si, 7, {'vendor_key': 'GENERIC_VENDOR-1234', 'media_key': 'UNKOWN', 'lane_speed_key': 'speed:100GAUI-2'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_port_generic_vendor_key_lane_speed_si, 7, {'vendor_key': 'GENERIC_VENDOR-1234', 'media_key': 'UNKOWN', 'lane_speed_key': 'MISSING'}, {}),
    (media_settings_port_vendor_key_si, 7, {'vendor_key': 'AMPHANOL-5678', 'media_key': 'UNKOWN', 'lane_speed_key': 'UNKOWN'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_port_generic_vendor_key_si, 7, {'vendor_key': 'GENERIC_VENDOR-1234', 'media_key': 'UNKOWN', 'lane_speed_key': 'UNKOWN'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_port_default_media_key_lane_speed_si, 7, {'vendor_key': 'MISSING', 'media_key': 'MISSING', 'lane_speed_key': 'MISSING'}, {'speed:400GAUI-8': {'idriver': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}, 'pre1': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}, 'ob_m2lp': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}}}),
    (media_settings_global_default_port_media_key_lane_speed_si, 7, {'vendor_key': 'MISSING', 'media_key': 'MISSING', 'lane_speed_key': 'MISSING'}, {'speed:400GAUI-8': {'idriver': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}, 'pre1': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}, 'ob_m2lp': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}}}),
    (media_settings_global_list_of_ranges_media_key_lane_speed_si_with_default_section, 7, {'vendor_key': 'MISSING', 'media_key': 'MISSING', 'lane_speed_key': 'MISSING'}, {'speed:400GAUI-8': {'idriver': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}, 'pre1': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}, 'ob_m2lp': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}}}),
    (media_settings_empty, 7, {'vendor_key': 'AMPHANOL-5678', 'media_key': 'QSFP-DD-active_cable_media_interface', 'lane_speed_key': 'speed:100GAUI-2'}, {})
    ])
    def test_get_media_settings_value(self, media_settings_dict, port, key, expected):
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.g_dict', media_settings_dict):
            result = media_settings_parser.get_media_settings_value(port, key)
            assert result == expected

    @patch('xcvrd.xcvrd.g_dict', media_settings_dict)
    @patch('xcvrd.xcvrd._wrapper_get_presence', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd.XcvrTableHelper', MagicMock())
    @patch('xcvrd.xcvrd.XcvrTableHelper.get_cfg_port_tbl', MagicMock())
    @patch('xcvrd.xcvrd_utilities.media_settings_parser.get_media_settings_key', MagicMock(return_value={ 'vendor_key': 'MOLEX-1064141421', 'media_key': 'QSFP+-10GBase-SR-255M', 'lane_speed_key': 'speed:100GBASE-CR2' }))
    @patch('xcvrd.xcvrd_utilities.media_settings_parser.get_speed_and_lane_count', MagicMock(return_value=(100000, 2)))
    def test_notify_media_setting(self):
        self._check_notify_media_setting(1)

    @patch('xcvrd.xcvrd.g_dict', media_settings_with_comma_dict)
    @patch('xcvrd.xcvrd._wrapper_get_presence', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd.XcvrTableHelper', MagicMock())
    @patch('xcvrd.xcvrd.XcvrTableHelper.get_cfg_port_tbl', MagicMock())
    @patch('xcvrd.xcvrd_utilities.media_settings_parser.get_media_settings_key', MagicMock(return_value={ 'vendor_key': 'MOLEX-1064141421', 'media_key': 'QSFP+-10GBase-SR-255M', 'lane_speed_key': 'speed:100GBASE-CR2' }))
    @patch('xcvrd.xcvrd_utilities.media_settings_parser.get_speed_and_lane_count', MagicMock(return_value=(100000, 2)))
    def test_notify_media_setting_with_comma(self):
        self._check_notify_media_setting(1)
        self._check_notify_media_setting(6)

    def _check_notify_media_setting(self, index):
        xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        cfg_port_tbl = MagicMock()
        mock_cfg_table = xcvr_table_helper.get_cfg_port_tbl = MagicMock(return_value=cfg_port_tbl)

        logical_port_name = 'Ethernet0'
        xcvr_info_dict = {
            index: {
                'manufacturer': 'Molex',
                'model': '1064141421',
                'cable_type': 'Length Cable Assembly(m)',
                'cable_length': '255',
                'specification_compliance': "{'10/40G Ethernet Compliance Code': '10GBase-SR'}",
                'type_abbrv_name': 'QSFP+'
            }
        }
        app_port_tbl = Table("APPL_DB", 'PORT_TABLE')
        port_mapping = PortMapping()
        port_change_event = PortChangeEvent('Ethernet0', index, 0, PortChangeEvent.PORT_ADD)
        port_mapping.handle_port_change_event(port_change_event)
        media_settings_parser.notify_media_setting(logical_port_name, xcvr_info_dict, app_port_tbl, mock_cfg_table, port_mapping)

    @patch('xcvrd.xcvrd_utilities.optics_si_parser.g_optics_si_dict', optics_si_settings_dict)
    @patch('xcvrd.xcvrd._wrapper_get_presence', MagicMock(return_value=True))
    def test_fetch_optics_si_setting(self):
        self._check_fetch_optics_si_setting(1)

    @patch('xcvrd.xcvrd_utilities.optics_si_parser.g_optics_si_dict', optics_si_settings_with_comma_dict)
    @patch('xcvrd.xcvrd._wrapper_get_presence', MagicMock(return_value=True))
    def test_fetch_optics_si_setting_with_comma(self):
        self._check_fetch_optics_si_setting(1)
        self._check_fetch_optics_si_setting(6)

    @patch('xcvrd.xcvrd_utilities.optics_si_parser.g_optics_si_dict', port_optics_si_settings)
    @patch('xcvrd.xcvrd._wrapper_get_presence', MagicMock(return_value=True))
    def test_fetch_optics_si_setting_with_port(self):
       self._check_fetch_optics_si_setting(1)

    @patch('xcvrd.xcvrd._wrapper_get_presence', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd_utilities.optics_si_parser.get_module_vendor_key', MagicMock(return_value=('CREDO-CAC82X321M','CREDO')))
    def _check_fetch_optics_si_setting(self, index):
        port = 1
        lane_speed = 100
        mock_sfp = MagicMock()
        optics_si_parser.fetch_optics_si_setting(port, lane_speed, mock_sfp)

    def test_get_module_vendor_key(self):
        mock_sfp = MagicMock()
        mock_xcvr_api = MagicMock()
        mock_sfp.get_xcvr_api = MagicMock(return_value=mock_xcvr_api)
        mock_xcvr_api.get_manufacturer = MagicMock(return_value='Credo ')
        mock_xcvr_api.get_model = MagicMock(return_value='CAC82X321HW')
        result = get_module_vendor_key(1, mock_sfp)
        assert result == ('CREDO-CAC82X321HW','CREDO')

    def test_detect_port_in_error_status(self):
        class MockTable:
            def get(self, key):
                pass

        status_tbl = MockTable()
        status_tbl.get = MagicMock(return_value=(True, {'error': 'N/A'}))
        assert not detect_port_in_error_status(None, status_tbl)

        status_tbl.get = MagicMock(return_value=(True, {'error': SfpBase.SFP_ERROR_DESCRIPTION_BLOCKING}))
        assert detect_port_in_error_status(None, status_tbl)

    def test_is_error_sfp_status(self):
        error_values = [7, 11, 19, 35]
        for error_value in error_values:
            assert is_error_block_eeprom_reading(error_value)

        assert not is_error_block_eeprom_reading(int(SFP_STATUS_INSERTED))
        assert not is_error_block_eeprom_reading(int(SFP_STATUS_REMOVED))


    @patch('swsscommon.swsscommon.Select.addSelectable', MagicMock())
    @patch('swsscommon.swsscommon.Table')
    @patch('swsscommon.swsscommon.SubscriberStateTable')
    @patch('swsscommon.swsscommon.Select.select')
    def test_handle_front_panel_filter(self, mock_select, mock_sub_table, mock_swsscommon_table):
        class DummyPortChangeEventHandler:
            def __init__(self):
                self.port_event_cache = []

            def handle_port_change_event(self, port_event):
                self.port_event_cache.append(port_event)

        CONFIG_DB = 'CONFIG_DB'
        PORT_TABLE = swsscommon.CFG_PORT_TABLE_NAME
        port_change_event_handler = DummyPortChangeEventHandler()

        mock_table = MagicMock()
        mock_table.getKeys =  MagicMock(return_value=['Ethernet0', 'Ethernet8', 'Ethernet16'])
        mock_table.get = MagicMock(side_effect=[(True, (('index', 1), )),
                                                (True, (('index', 2), ('role', 'Dpc'))),
                                                (True, (('index', 3), ('role', 'Ext')))
                                                ])
        mock_swsscommon_table.return_value = mock_table

        mock_selectable = MagicMock()
        side_effect_list = [
            ('Ethernet0', swsscommon.SET_COMMAND, (('index', '1'), ('speed', '40000'))),
            ('Ethernet8', swsscommon.SET_COMMAND, (('index', '2'), ('speed', '80000'), ('role', 'Dpc'))),
            ('Ethernet16', swsscommon.SET_COMMAND, (('index', '3'), ('speed', '80000'), ('role', 'Ext'))),
            (None, None, None)
        ]
        mock_selectable.pop = MagicMock(side_effect=side_effect_list)
        mock_select.return_value = (swsscommon.Select.OBJECT, mock_selectable)
        mock_sub_table.return_value = mock_selectable
        logger = MagicMock()
        stop_event = threading.Event()
        stop_event.is_set = MagicMock(return_value=False)

        observer = PortChangeObserver(DEFAULT_NAMESPACE, logger, stop_event,
                                     port_change_event_handler.handle_port_change_event,
                                     [{CONFIG_DB: PORT_TABLE}])

        # Only Ethernet8 is filled in the role map
        assert observer.port_role_map['Ethernet8'] == 'Dpc'
        assert observer.port_role_map['Ethernet16'] == 'Ext'
        assert 'Ethernet0' not in observer.port_role_map

        # Test basic single update event without filtering:
        assert observer.handle_port_update_event()
        assert len(port_change_event_handler.port_event_cache) == 2
        assert list(observer.port_event_cache.keys()) == [('Ethernet0', CONFIG_DB, PORT_TABLE), ('Ethernet16', CONFIG_DB, PORT_TABLE)]


    @patch('swsscommon.swsscommon.Select.addSelectable', MagicMock())
    @patch('swsscommon.swsscommon.SubscriberStateTable')
    @patch('swsscommon.swsscommon.Select.select')
    def test_handle_port_update_event(self, mock_select, mock_sub_table):
        class DummyPortChangeEventHandler:
            def __init__(self):
                self.port_event_cache = []

            def handle_port_change_event(self, port_event):
                self.port_event_cache.append(port_event)

        CONFIG_DB = 'CONFIG_DB'
        PORT_TABLE = swsscommon.CFG_PORT_TABLE_NAME
        port_change_event_handler = DummyPortChangeEventHandler()
        expected_processed_event_count = 0

        mock_selectable = MagicMock()
        side_effect_list = [
            ('Ethernet0', swsscommon.SET_COMMAND, (('index', '1'), ('speed', '40000'), ('fec', 'rs'))),
            (None, None, None)
        ]
        mock_selectable.pop = MagicMock(side_effect=side_effect_list)
        mock_select.return_value = (swsscommon.Select.OBJECT, mock_selectable)
        mock_sub_table.return_value = mock_selectable
        logger = MagicMock()
        stop_event = threading.Event()
        stop_event.is_set = MagicMock(return_value=False)

        observer = PortChangeObserver(DEFAULT_NAMESPACE, logger, stop_event,
                                     port_change_event_handler.handle_port_change_event,
                                     [{CONFIG_DB: PORT_TABLE}])

        # Test basic single update event without filtering:
        assert observer.handle_port_update_event()
        expected_processed_event_count +=1
        assert len(port_change_event_handler.port_event_cache) == expected_processed_event_count
        # 'fec' should not be filtered out
        expected_cache = {
            ('Ethernet0', CONFIG_DB, PORT_TABLE): {
                'port_name': 'Ethernet0',
                'index': '1',
                'op': swsscommon.SET_COMMAND,
                'asic_id': 0,
                'speed': '40000',
                'fec': 'rs'
            }
        }
        assert observer.port_event_cache == expected_cache

        observer = PortChangeObserver(DEFAULT_NAMESPACE, logger, stop_event,
                                     port_change_event_handler.handle_port_change_event,
                                     [{CONFIG_DB: PORT_TABLE, 'FILTER': ['speed']}])
        mock_selectable.pop.side_effect = iter(side_effect_list)

        # Test basic single update event with filtering:
        assert not observer.port_event_cache
        assert observer.handle_port_update_event()
        expected_processed_event_count +=1
        assert len(port_change_event_handler.port_event_cache) == expected_processed_event_count
        # 'fec' should be filtered out
        expected_cache = {
            ('Ethernet0', CONFIG_DB, PORT_TABLE): {
                'port_name': 'Ethernet0',
                'index': '1',
                'op': swsscommon.SET_COMMAND,
                'asic_id': 0,
                'speed': '40000',
            }
        }
        assert observer.port_event_cache == expected_cache
        assert port_change_event_handler.port_event_cache[-1].port_name == 'Ethernet0'
        assert port_change_event_handler.port_event_cache[-1].event_type == PortChangeEvent.PORT_SET
        assert port_change_event_handler.port_event_cache[-1].port_index == 1
        assert port_change_event_handler.port_event_cache[-1].asic_id == 0
        assert port_change_event_handler.port_event_cache[-1].db_name == CONFIG_DB
        assert port_change_event_handler.port_event_cache[-1].table_name == PORT_TABLE
        assert port_change_event_handler.port_event_cache[-1].port_dict == \
            expected_cache[('Ethernet0', CONFIG_DB, PORT_TABLE)]

        # Test duplicate update event on the same key:
        mock_selectable.pop.side_effect = iter(side_effect_list)
        # return False when no new event is processed
        assert not observer.handle_port_update_event()
        assert len(port_change_event_handler.port_event_cache) == expected_processed_event_count
        assert observer.port_event_cache == expected_cache

        # Test soaking multiple different update events on the same key:
        side_effect_list = [
            ('Ethernet0', swsscommon.SET_COMMAND, (('index', '1'), ('speed', '100000'))),
            ('Ethernet0', swsscommon.SET_COMMAND, (('index', '1'), ('speed', '200000'))),
            ('Ethernet0', swsscommon.SET_COMMAND, (('index', '1'), ('speed', '400000'))),
            (None, None, None)
        ]
        mock_selectable.pop.side_effect = iter(side_effect_list)
        assert observer.handle_port_update_event()
        # only the last event should be processed
        expected_processed_event_count +=1
        assert len(port_change_event_handler.port_event_cache) == expected_processed_event_count
        expected_cache = {
            ('Ethernet0', CONFIG_DB, PORT_TABLE): {
                'port_name': 'Ethernet0',
                'index': '1',
                'op': swsscommon.SET_COMMAND,
                'asic_id': 0,
                'speed': '400000',
            }
        }
        assert observer.port_event_cache == expected_cache

        # Test select timeout case:
        mock_select.return_value = (swsscommon.Select.TIMEOUT, None)
        assert not observer.handle_port_update_event()
        assert len(port_change_event_handler.port_event_cache) == expected_processed_event_count
        mock_select.return_value = (swsscommon.Select.OBJECT, None)

        # Test update event for DEL case:
        side_effect_list = [
            ('Ethernet0', swsscommon.DEL_COMMAND, (('index', '1'), ('speed', '400000'))),
            (None, None, None)
        ]
        mock_selectable.pop.side_effect = iter(side_effect_list)
        assert observer.handle_port_update_event()
        expected_processed_event_count +=1
        assert len(port_change_event_handler.port_event_cache) == expected_processed_event_count
        expected_cache = {
            ('Ethernet0', CONFIG_DB, PORT_TABLE): {
                'port_name': 'Ethernet0',
                'index': '1',
                'op': swsscommon.DEL_COMMAND,
                'asic_id': 0,
                'speed': '400000',
            }
        }
        assert observer.port_event_cache == expected_cache

        # Test update event if it's a subset of cached event:
        side_effect_list = [
            ('Ethernet0', swsscommon.DEL_COMMAND, (('index', '1'), )),
            (None, None, None)
        ]
        mock_selectable.pop.side_effect = iter(side_effect_list)
        assert not observer.handle_port_update_event()
        assert len(port_change_event_handler.port_event_cache) == expected_processed_event_count
        expected_cache = {
            ('Ethernet0', CONFIG_DB, PORT_TABLE): {
                'port_name': 'Ethernet0',
                'index': '1',
                'op': swsscommon.DEL_COMMAND,
                'asic_id': 0,
            }
        }
        assert observer.port_event_cache == expected_cache

    @patch('swsscommon.swsscommon.Select.addSelectable', MagicMock())
    @patch('swsscommon.swsscommon.SubscriberStateTable')
    @patch('swsscommon.swsscommon.Select.select')
    def test_handle_port_config_change(self, mock_select, mock_sub_table):
        mock_selectable = MagicMock()
        mock_selectable.pop = MagicMock(
            side_effect=[('Ethernet0', swsscommon.SET_COMMAND, (('index', '1'), )), (None, None, None)])
        mock_select.return_value = (swsscommon.Select.OBJECT, mock_selectable)
        mock_sub_table.return_value = mock_selectable

        sel, asic_context = subscribe_port_config_change(DEFAULT_NAMESPACE)
        port_mapping = PortMapping()
        stop_event = threading.Event()
        stop_event.is_set = MagicMock(return_value=False)
        logger = MagicMock()
        handle_port_config_change(sel, asic_context, stop_event, port_mapping,
                                  logger, port_mapping.handle_port_change_event)

        assert port_mapping.logical_port_list.count('Ethernet0')
        assert port_mapping.get_asic_id_for_logical_port('Ethernet0') == 0
        assert port_mapping.get_physical_to_logical(1) == ['Ethernet0']
        assert port_mapping.get_logical_to_physical('Ethernet0') == [1]

        mock_selectable.pop = MagicMock(
            side_effect=[('Ethernet0', swsscommon.DEL_COMMAND, (('index', '1'), )), (None, None, None)])
        handle_port_config_change(sel, asic_context, stop_event, port_mapping,
                                  logger, port_mapping.handle_port_change_event)
        assert not port_mapping.logical_port_list
        assert not port_mapping.logical_to_physical
        assert not port_mapping.physical_to_logical
        assert not port_mapping.logical_to_asic

    @patch('swsscommon.swsscommon.Table')
    def test_get_port_mapping(self, mock_swsscommon_table):
        mock_table = MagicMock()
        mock_table.getKeys = MagicMock(return_value=['Ethernet0', 'Ethernet4', 'Ethernet-IB0', 'Ethernet8'])
        mock_table.get = MagicMock(side_effect=[(True, (('index', 1), )), (True, (('index', 2), )), 
                        (True, (('index', 3), )), (True, (('index', 4), ('role', 'Dpc')))])
        mock_swsscommon_table.return_value = mock_table
        port_mapping = get_port_mapping(DEFAULT_NAMESPACE)
        assert port_mapping.logical_port_list.count('Ethernet0')
        assert port_mapping.get_asic_id_for_logical_port('Ethernet0') == 0
        assert port_mapping.get_physical_to_logical(1) == ['Ethernet0']
        assert port_mapping.get_logical_to_physical('Ethernet0') == [1]

        assert port_mapping.logical_port_list.count('Ethernet4')
        assert port_mapping.get_asic_id_for_logical_port('Ethernet4') == 0
        assert port_mapping.get_physical_to_logical(2) == ['Ethernet4']
        assert port_mapping.get_logical_to_physical('Ethernet4') == [2]

        assert port_mapping.logical_port_list.count('Ethernet-IB0') == 0
        assert port_mapping.get_asic_id_for_logical_port('Ethernet-IB0') == None
        assert port_mapping.get_physical_to_logical(3) == None
        assert port_mapping.get_logical_to_physical('Ethernet-IB0') == None

        assert port_mapping.logical_port_list.count('Ethernet8') == 0
        assert port_mapping.get_asic_id_for_logical_port('Ethernet8') == None
        assert port_mapping.get_physical_to_logical(4) == None
        assert port_mapping.get_logical_to_physical('Ethernet8') == None

    @patch('swsscommon.swsscommon.Select.addSelectable', MagicMock())
    @patch('swsscommon.swsscommon.SubscriberStateTable')
    @patch('swsscommon.swsscommon.Select.select')
    def test_DaemonXcvrd_wait_for_port_config_done(self, mock_select, mock_sub_table):
        mock_selectable = MagicMock()
        mock_selectable.pop = MagicMock(
            side_effect=[('Ethernet0', swsscommon.SET_COMMAND, (('index', '1'), )), ('PortConfigDone', None, None)])
        mock_select.return_value = (swsscommon.Select.OBJECT, mock_selectable)
        mock_sub_table.return_value = mock_selectable
        xcvrd = DaemonXcvrd(SYSLOG_IDENTIFIER)
        xcvrd.wait_for_port_config_done('')
        assert swsscommon.Select.select.call_count == 2

    @patch('xcvrd.xcvrd.DaemonXcvrd.init')
    @patch('xcvrd.xcvrd.DaemonXcvrd.deinit')
    @patch('xcvrd.xcvrd.DomInfoUpdateTask.start')
    @patch('xcvrd.xcvrd.SfpStateUpdateTask.start')
    @patch('xcvrd.xcvrd.DomInfoUpdateTask.join')
    @patch('xcvrd.xcvrd.SfpStateUpdateTask.join')
    def test_DaemonXcvrd_run(self, mock_task_stop1, mock_task_stop2, mock_task_run1, mock_task_run2, mock_deinit, mock_init):
        mock_init.return_value = (PortMapping(), set())
        xcvrd = DaemonXcvrd(SYSLOG_IDENTIFIER)
        xcvrd.load_feature_flags = MagicMock()
        xcvrd.stop_event.wait = MagicMock()
        xcvrd.run()
        assert mock_task_stop1.call_count == 1
        assert mock_task_stop2.call_count == 1
        assert mock_task_run1.call_count == 1
        assert mock_task_run2.call_count == 1
        assert mock_deinit.call_count == 1
        assert mock_init.call_count == 1

    def test_SffManagerTask_handle_port_change_event(self):
        stop_event = threading.Event()
        task = SffManagerTask(DEFAULT_NAMESPACE, stop_event, MagicMock(), helper_logger)

        port_change_event = PortChangeEvent('PortConfigDone', -1, 0, PortChangeEvent.PORT_SET)
        task.on_port_update_event(port_change_event)
        assert len(task.port_dict) == 0

        port_change_event = PortChangeEvent('PortInitDone', -1, 0, PortChangeEvent.PORT_SET)
        task.on_port_update_event(port_change_event)
        assert len(task.port_dict) == 0

        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_ADD)
        task.on_port_update_event(port_change_event)
        assert len(task.port_dict) == 0

        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_REMOVE)
        task.on_port_update_event(port_change_event)
        assert len(task.port_dict) == 0

        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_DEL)
        task.on_port_update_event(port_change_event)
        assert len(task.port_dict) == 0

        port_dict = {'type': 'QSFP28', 'subport': '0', 'host_tx_ready': 'false'}
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_SET, port_dict)
        task.on_port_update_event(port_change_event)
        assert len(task.port_dict) == 1

        port_change_event = PortChangeEvent('Ethernet0', -1, 0, PortChangeEvent.PORT_DEL, {},
                                            'STATE_DB', 'TRANSCEIVER_INFO')
        task.on_port_update_event(port_change_event)
        assert len(task.port_dict) == 1

        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_DEL, {},
                                            'CONFIG_DB', 'PORT_TABLE')
        task.on_port_update_event(port_change_event)
        assert len(task.port_dict) == 0

    def test_SffManagerTask_get_active_lanes_for_lport(self):
        sff_manager_task = SffManagerTask(DEFAULT_NAMESPACE,
                                 threading.Event(),
                                 MagicMock(),
                                 helper_logger)

        lport = 'Ethernet0'

        subport_idx = 3
        num_lanes_per_lport = 1
        num_lanes_per_pport = 4
        expected_result = [False, False, True, False]
        result = sff_manager_task.get_active_lanes_for_lport(lport, subport_idx, num_lanes_per_lport, num_lanes_per_pport)
        assert result == expected_result

        subport_idx = 1
        num_lanes_per_lport = 2
        num_lanes_per_pport = 4
        expected_result = [True, True, False, False]
        result = sff_manager_task.get_active_lanes_for_lport(lport, subport_idx, num_lanes_per_lport, num_lanes_per_pport)
        assert result == expected_result

        subport_idx = 1
        num_lanes_per_lport = 2
        num_lanes_per_pport = 4
        expected_result = [True, True, False, False]
        result = sff_manager_task.get_active_lanes_for_lport(lport, subport_idx, num_lanes_per_lport, num_lanes_per_pport)
        assert result == expected_result

        subport_idx = 2
        num_lanes_per_lport = 2
        num_lanes_per_pport = 4
        expected_result = [False, False, True, True]
        result = sff_manager_task.get_active_lanes_for_lport(lport, subport_idx, num_lanes_per_lport, num_lanes_per_pport)
        assert result == expected_result

        subport_idx = 0
        num_lanes_per_lport = 4
        num_lanes_per_pport = 4
        expected_result = [True, True, True, True]
        result = sff_manager_task.get_active_lanes_for_lport(lport, subport_idx, num_lanes_per_lport, num_lanes_per_pport)
        assert result == expected_result

        # Test with larger number of lanes per port (not real use case)
        subport_idx = 1
        num_lanes_per_lport = 4
        num_lanes_per_pport = 32
        expected_result = [True, True, True, True, False, False, False, False,
                           False, False, False, False, False, False, False, False,
                           False, False, False, False, False, False, False, False,
                           False, False, False, False, False, False, False, False]
        result = sff_manager_task.get_active_lanes_for_lport(lport, subport_idx, num_lanes_per_lport, num_lanes_per_pport)
        assert result == expected_result

    def test_SffManagerTask_get_active_lanes_for_lport_with_invalid_input(self):
        sff_manager_task = SffManagerTask(DEFAULT_NAMESPACE,
                                 threading.Event(),
                                 MagicMock(),
                                 helper_logger)

        lport = 'Ethernet0'

        subport_idx = -1
        num_lanes_per_lport = 4
        num_lanes_per_pport = 32
        result = sff_manager_task.get_active_lanes_for_lport(lport, subport_idx, num_lanes_per_lport, num_lanes_per_pport)
        assert result is None

        subport_idx = 5
        num_lanes_per_lport = 1
        num_lanes_per_pport = 4
        result = sff_manager_task.get_active_lanes_for_lport(lport, subport_idx, num_lanes_per_lport, num_lanes_per_pport)
        assert result is None

    @patch.object(XcvrTableHelper, 'get_state_port_tbl', return_value=MagicMock())
    def test_SffManagerTask_get_host_tx_status(self, mock_get_state_port_tbl):
        mock_get_state_port_tbl.return_value.hget.return_value = (True, 'true')

        sff_manager_task = SffManagerTask(DEFAULT_NAMESPACE,
                                 threading.Event(),
                                 MagicMock(),
                                 helper_logger)

        lport = 'Ethernet0'
        assert sff_manager_task.get_host_tx_status(lport, 0) == 'true'
        mock_get_state_port_tbl.assert_called_once_with(0)
        mock_get_state_port_tbl.return_value.hget.assert_called_once_with(lport, 'host_tx_ready')

    @patch.object(XcvrTableHelper, 'get_cfg_port_tbl', return_value=MagicMock())
    def test_SffManagerTask_get_admin_status(self, mock_get_cfg_port_tbl):
        mock_get_cfg_port_tbl.return_value.hget.return_value = (True, 'up')

        sff_manager_task = SffManagerTask(DEFAULT_NAMESPACE,
                                 threading.Event(),
                                 MagicMock(),
                                 helper_logger)

        lport = 'Ethernet0'
        assert sff_manager_task.get_admin_status(lport, 0) == 'up'
        mock_get_cfg_port_tbl.assert_called_once_with(0)
        mock_get_cfg_port_tbl.return_value.hget.assert_called_once_with(lport, 'admin_status')

    @patch('xcvrd.xcvrd.platform_chassis')
    @patch('xcvrd.sff_mgr.PortChangeObserver', MagicMock(handle_port_update_event=MagicMock()))
    def test_SffManagerTask_task_worker(self, mock_chassis):
        mock_xcvr_api = MagicMock()
        mock_xcvr_api.tx_disable_channel = MagicMock(return_value=True)
        mock_xcvr_api.is_flat_memory = MagicMock(return_value=False)
        mock_xcvr_api.is_copper = MagicMock(return_value=False)
        mock_xcvr_api.get_tx_disable_support = MagicMock(return_value=True)

        mock_sfp = MagicMock()
        mock_sfp.get_presence = MagicMock(return_value=True)
        mock_sfp.get_xcvr_api = MagicMock(return_value=mock_xcvr_api)

        mock_chassis.get_all_sfps = MagicMock(return_value=[mock_sfp])
        mock_chassis.get_sfp = MagicMock(return_value=mock_sfp)

        task = SffManagerTask(DEFAULT_NAMESPACE,
                              threading.Event(),
                              mock_chassis,
                              helper_logger)

        # TX enable case:
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_SET, {
            'type': 'QSFP28',
            'subport': '0',
            'lanes': '1,2,3,4',
        })
        task.on_port_update_event(port_change_event)
        assert len(task.port_dict) == 1
        task.get_host_tx_status = MagicMock(return_value='true')
        task.get_admin_status = MagicMock(return_value='up')
        mock_xcvr_api.get_tx_disable = MagicMock(return_value=[True, True, True, True])
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert mock_xcvr_api.tx_disable_channel.call_count == 1
        assert task.get_host_tx_status.call_count == 1
        assert task.get_admin_status.call_count == 1

        # TX disable case:
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_SET,
                                            {'host_tx_ready': 'false'})
        task.on_port_update_event(port_change_event)
        assert len(task.port_dict) == 1
        mock_xcvr_api.get_tx_disable = MagicMock(return_value=[False, False, False, False])
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert mock_xcvr_api.tx_disable_channel.call_count == 2

        # No insertion and no change on host_tx_ready
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert task.port_dict == task.port_dict_prev
        assert mock_xcvr_api.tx_disable_channel.call_count == 2

        # flat memory case
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_SET,
                                            {'host_tx_ready': 'true'})
        task.on_port_update_event(port_change_event)
        mock_xcvr_api.is_flat_memory = MagicMock(return_value=True)
        mock_xcvr_api.is_flat_memory.call_count = 0
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert mock_xcvr_api.is_flat_memory.call_count == 1
        assert mock_xcvr_api.tx_disable_channel.call_count == 2
        mock_xcvr_api.is_flat_memory = MagicMock(return_value=False)

        # copper case
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_SET,
                                            {'host_tx_ready': 'false'})
        task.on_port_update_event(port_change_event)
        mock_xcvr_api.is_copper = MagicMock(return_value=True)
        mock_xcvr_api.is_copper.call_count = 0
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert mock_xcvr_api.is_copper.call_count == 1
        assert mock_xcvr_api.tx_disable_channel.call_count == 2
        mock_xcvr_api.is_copper = MagicMock(return_value=False)

        # tx_disable not supported case
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_SET,
                                            {'host_tx_ready': 'true'})
        task.on_port_update_event(port_change_event)
        mock_xcvr_api.get_tx_disable_support = MagicMock(return_value=False)
        mock_xcvr_api.get_tx_disable_support.call_count = 0
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert mock_xcvr_api.get_tx_disable_support.call_count == 1
        assert mock_xcvr_api.tx_disable_channel.call_count == 2
        mock_xcvr_api.get_tx_disable_support = MagicMock(return_value=True)

        # sfp not present case
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_SET,
                                            {'host_tx_ready': 'false'})
        task.on_port_update_event(port_change_event)
        mock_sfp.get_presence = MagicMock(return_value=False)
        mock_sfp.get_presence.call_count = 0
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert mock_sfp.get_presence.call_count == 1
        assert mock_xcvr_api.tx_disable_channel.call_count == 2
        mock_sfp.get_presence = MagicMock(return_value=True)

    def test_CmisManagerTask_update_port_transceiver_status_table_sw_cmis_state(self):
        port_mapping = PortMapping()
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event)
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_SET)
        task.on_port_update_event(port_change_event)

        task.xcvr_table_helper.get_status_tbl = MagicMock(return_value=None)
        task.update_port_transceiver_status_table_sw_cmis_state("Ethernet0", CMIS_STATE_INSERTED)

        mock_get_status_tbl = MagicMock()
        mock_get_status_tbl.set = MagicMock()
        task.xcvr_table_helper.get_status_tbl.return_value = mock_get_status_tbl
        task.update_port_transceiver_status_table_sw_cmis_state("Ethernet0", CMIS_STATE_INSERTED)
        assert mock_get_status_tbl.set.call_count == 1

    @patch('xcvrd.xcvrd._wrapper_get_sfp_type', MagicMock(return_value='QSFP_DD'))
    def test_CmisManagerTask_handle_port_change_event(self):
        port_mapping = PortMapping()
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event)

        assert not task.isPortConfigDone
        port_change_event = PortChangeEvent('PortConfigDone', -1, 0, PortChangeEvent.PORT_SET)
        task.on_port_update_event(port_change_event)
        assert task.isPortConfigDone

        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_ADD)
        task.on_port_update_event(port_change_event)
        assert len(task.port_dict) == 0

        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_REMOVE)
        task.on_port_update_event(port_change_event)
        assert len(task.port_dict) == 0

        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_DEL)
        task.on_port_update_event(port_change_event)
        assert len(task.port_dict) == 1

        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_SET)
        task.on_port_update_event(port_change_event)
        assert len(task.port_dict) == 1


    @patch('xcvrd.xcvrd.XcvrTableHelper')
    def test_CmisManagerTask_get_configured_freq(self, mock_table_helper):
        port_mapping = PortMapping()
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event)
        cfg_port_tbl = MagicMock()
        cfg_port_tbl.get = MagicMock(return_value=(True, (('laser_freq', 193100),)))
        mock_table_helper.get_cfg_port_tbl = MagicMock(return_value=cfg_port_tbl)
        task.xcvr_table_helper.get_cfg_port_tbl = mock_table_helper.get_cfg_port_tbl
        assert task.get_configured_laser_freq_from_db('Ethernet0') == 193100

    @patch('xcvrd.xcvrd.XcvrTableHelper')
    def test_CmisManagerTask_get_configured_tx_power_from_db(self, mock_table_helper):
        port_mapping = PortMapping()
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event)
        cfg_port_tbl = MagicMock()
        cfg_port_tbl.get = MagicMock(return_value=(True, (('tx_power', -10),)))
        mock_table_helper.get_cfg_port_tbl = MagicMock(return_value=cfg_port_tbl)
        task.xcvr_table_helper.get_cfg_port_tbl = mock_table_helper.get_cfg_port_tbl
        assert task.get_configured_tx_power_from_db('Ethernet0') == -10

    @patch('xcvrd.xcvrd.platform_chassis')
    @patch('xcvrd.xcvrd.PortChangeObserver', MagicMock(handle_port_update_event=MagicMock()))
    def test_CmisManagerTask_task_run_stop(self, mock_chassis):
        mock_object = MagicMock()
        mock_object.get_presence = MagicMock(return_value=True)
        mock_chassis.get_all_sfps = MagicMock(return_value=[mock_object, mock_object])

        port_mapping = PortMapping()
        stop_event = threading.Event()
        cmis_manager = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event)
        cmis_manager.wait_for_port_config_done = MagicMock()
        cmis_manager.start()
        cmis_manager.join()
        assert not cmis_manager.is_alive()

    DEFAULT_DP_STATE = {
        'DP1State': 'DataPathActivated',
        'DP2State': 'DataPathActivated',
        'DP3State': 'DataPathActivated',
        'DP4State': 'DataPathActivated',
        'DP5State': 'DataPathActivated',
        'DP6State': 'DataPathActivated',
        'DP7State': 'DataPathActivated',
        'DP8State': 'DataPathActivated'
    }
    DEFAULT_CONFIG_STATUS = {
        'ConfigStatusLane1': 'ConfigSuccess',
        'ConfigStatusLane2': 'ConfigSuccess',
        'ConfigStatusLane3': 'ConfigSuccess',
        'ConfigStatusLane4': 'ConfigSuccess',
        'ConfigStatusLane5': 'ConfigSuccess',
        'ConfigStatusLane6': 'ConfigSuccess',
        'ConfigStatusLane7': 'ConfigSuccess',
        'ConfigStatusLane8': 'ConfigSuccess'
    }
    CONFIG_LANE_8_UNDEFINED = {
        'ConfigStatusLane1': 'ConfigSuccess',
        'ConfigStatusLane2': 'ConfigSuccess',
        'ConfigStatusLane3': 'ConfigSuccess',
        'ConfigStatusLane4': 'ConfigSuccess',
        'ConfigStatusLane5': 'ConfigSuccess',
        'ConfigStatusLane6': 'ConfigSuccess',
        'ConfigStatusLane7': 'ConfigSuccess',
        'ConfigStatusLane8': 'ConfigUndefined'
    }
    @pytest.mark.parametrize("app_new, host_lanes_mask, lane_appl_code, default_dp_state, default_config_status, expected", [
        (1, 0x0F, {0 : 1, 1 : 1, 2 : 1, 3 : 1}, DEFAULT_DP_STATE, DEFAULT_CONFIG_STATUS, False),
        (1, 0x0F, {0 : 1, 1 : 1, 2 : 1, 3 : 0}, DEFAULT_DP_STATE, DEFAULT_CONFIG_STATUS, True),
        (1, 0xF0, {4 : 1, 5 : 1, 6 : 1, 7 : 1}, DEFAULT_DP_STATE, DEFAULT_CONFIG_STATUS, False),
        (1, 0xF0, {4 : 1, 5 : 1, 6 : 1, 7 : 1}, DEFAULT_DP_STATE, CONFIG_LANE_8_UNDEFINED, True),
        (1, 0xF0, {4 : 1, 5 : 7, 6 : 1, 7 : 1}, DEFAULT_DP_STATE, DEFAULT_CONFIG_STATUS, True),
        (4, 0xF0, {4 : 1, 5 : 7, 6 : 1, 7 : 1}, DEFAULT_DP_STATE, DEFAULT_CONFIG_STATUS, True),
        (3, 0xC0, {7 : 3, 8 : 3}, DEFAULT_DP_STATE, DEFAULT_CONFIG_STATUS, False),
        (1, 0x0F, {}, DEFAULT_DP_STATE, DEFAULT_CONFIG_STATUS, True),
        (-1, 0x0F, {}, DEFAULT_DP_STATE, DEFAULT_CONFIG_STATUS, False)
    ])
    def test_CmisManagerTask_is_cmis_application_update_required(self, app_new, host_lanes_mask, lane_appl_code, default_dp_state, default_config_status, expected):

        mock_xcvr_api = MagicMock()
        mock_xcvr_api.is_flat_memory = MagicMock(return_value=False)

        def get_application(lane):
            return lane_appl_code.get(lane, 0)
        mock_xcvr_api.get_application = MagicMock(side_effect=get_application)

        mock_xcvr_api.get_datapath_state = MagicMock(return_value=default_dp_state)
        mock_xcvr_api.get_config_datapath_hostlane_status = MagicMock(return_value=default_config_status)

        port_mapping = PortMapping()
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event)

        assert task.is_cmis_application_update_required(mock_xcvr_api, app_new, host_lanes_mask) == expected

    @pytest.mark.parametrize("ifname, expected", [
        ('800G L C2M', 800000),
        ('400G CR8', 400000),
        ('200GBASE-CR4 (Clause 136)', 200000),
        ('100GBASE-CR2 (Clause 136)', 100000),
        ('CAUI-4 C2M (Annex 83E)', 100000),
        ('50GBASE-CR', 50000),
        ('LAUI-2 C2M (Annex 135C)', 50000),
        ('40GBASE-CR4 (Clause 85)', 40000),
        ('XLAUI C2M (Annex 83B)', 40000),
        ('XLPPI (Annex 86A)', 40000),
        ('25GBASE-CR CA-N (Clause 110)', 25000),
        ('10GBASE-CX4 (Clause 54)', 10000),
        ('SFI (SFF-8431)', 10000),
        ('XFI (SFF INF-8071i)', 10000),
        ('1000BASE -CX(Clause 39)', 1000),
        ('Unknown Interface', 0)
    ])
    def test_get_interface_speed(self, ifname, expected):
        assert get_interface_speed(ifname) == expected

    @patch('xcvrd.xcvrd.is_cmis_api', MagicMock(return_value=True))
    @pytest.mark.parametrize("host_lane_count, speed, subport, expected", [
        (8, 400000, 0, 0xFF),
        (4, 100000, 1, 0xF),
        (4, 100000, 2, 0xF0),
        (4, 100000, 0, 0xF),
        (4, 100000, 9, 0x0),
        (1, 50000, 2, 0x2),
        (1, 200000, 2, 0x0)
    ])
    def test_CmisManagerTask_get_cmis_host_lanes_mask(self, host_lane_count, speed, subport, expected):
        appl_advert_dict = {
            1: {
                'host_electrical_interface_id': '400GAUI-8 C2M (Annex 120E)',
                'module_media_interface_id': '400GBASE-DR4 (Cl 124)',
                'media_lane_count': 4,
                'host_lane_count': 8,
                'host_lane_assignment_options': 1
            },
            2: {
                'host_electrical_interface_id': 'CAUI-4 C2M (Annex 83E)',
                'module_media_interface_id': 'Active Cable assembly with BER < 5x10^-5',
                'media_lane_count': 4,
                'host_lane_count': 4,
                'host_lane_assignment_options': 17
            },
            3: {
                'host_electrical_interface_id': '50GAUI-1 C2M',
                'module_media_interface_id': '50GBASE-SR',
                'media_lane_count': 1,
                'host_lane_count': 1,
                'host_lane_assignment_options': 255
            }
        }
        mock_xcvr_api = MagicMock()
        mock_xcvr_api.get_application_advertisement = MagicMock(return_value=appl_advert_dict)

        def get_host_lane_assignment_option_side_effect(app):
            return appl_advert_dict[app]['host_lane_assignment_options']
        mock_xcvr_api.get_host_lane_assignment_option = MagicMock(side_effect=get_host_lane_assignment_option_side_effect)
        port_mapping = PortMapping()
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event)

        appl = get_cmis_application_desired(mock_xcvr_api, host_lane_count, speed)
        assert task.get_cmis_host_lanes_mask(mock_xcvr_api, appl, host_lane_count, subport) == expected


    def test_CmisManagerTask_post_port_active_apsel_to_db(self):
        mock_xcvr_api = MagicMock()
        mock_xcvr_api.get_active_apsel_hostlane = MagicMock(side_effect=[
            {
             'ActiveAppSelLane1': 1,
             'ActiveAppSelLane2': 1,
             'ActiveAppSelLane3': 1,
             'ActiveAppSelLane4': 1,
             'ActiveAppSelLane5': 1,
             'ActiveAppSelLane6': 1,
             'ActiveAppSelLane7': 1,
             'ActiveAppSelLane8': 1
            },
            {
             'ActiveAppSelLane1': 2,
             'ActiveAppSelLane2': 2,
             'ActiveAppSelLane3': 2,
             'ActiveAppSelLane4': 2,
             'ActiveAppSelLane5': 2,
             'ActiveAppSelLane6': 2,
             'ActiveAppSelLane7': 2,
             'ActiveAppSelLane8': 2
            },
            NotImplementedError
        ])
        mock_xcvr_api.get_application_advertisement = MagicMock(side_effect=[
            {
                1: {
                    'media_lane_count': 4,
                    'host_lane_count': 8
                }
            },
            {
                2: {
                    'media_lane_count': 1,
                    'host_lane_count': 2
                }
            }
        ])

        int_tbl = Table("STATE_DB", TRANSCEIVER_INFO_TABLE)

        port_mapping = PortMapping()
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event)
        task.xcvr_table_helper.get_intf_tbl = MagicMock(return_value=int_tbl)

        # case: partial lanes update
        lport = "Ethernet0"
        host_lanes_mask = 0xc
        ret = task.post_port_active_apsel_to_db(mock_xcvr_api, lport, host_lanes_mask)
        assert int_tbl.getKeys() == ["Ethernet0"]
        assert dict(int_tbl.mock_dict["Ethernet0"]) == {'active_apsel_hostlane3': '1',
                                                        'active_apsel_hostlane4': '1',
                                                        'host_lane_count': '8',
                                                        'media_lane_count': '4'}
        # case: full lanes update
        lport = "Ethernet8"
        host_lanes_mask = 0xff
        task.post_port_active_apsel_to_db(mock_xcvr_api, lport, host_lanes_mask)
        assert int_tbl.getKeys() == ["Ethernet0", "Ethernet8"]
        assert dict(int_tbl.mock_dict["Ethernet0"]) == {'active_apsel_hostlane3': '1',
                                                        'active_apsel_hostlane4': '1',
                                                        'host_lane_count': '8',
                                                        'media_lane_count': '4'}
        assert dict(int_tbl.mock_dict["Ethernet8"]) == {'active_apsel_hostlane1': '2',
                                                        'active_apsel_hostlane2': '2',
                                                        'active_apsel_hostlane3': '2',
                                                        'active_apsel_hostlane4': '2',
                                                        'active_apsel_hostlane5': '2',
                                                        'active_apsel_hostlane6': '2',
                                                        'active_apsel_hostlane7': '2',
                                                        'active_apsel_hostlane8': '2',
                                                        'host_lane_count': '2',
                                                        'media_lane_count': '1'}

        # case: NotImplementedError
        int_tbl = Table("STATE_DB", TRANSCEIVER_INFO_TABLE)     # a new empty table
        lport = "Ethernet0"
        host_lanes_mask = 0xf
        ret = task.post_port_active_apsel_to_db(mock_xcvr_api, lport, host_lanes_mask)
        assert int_tbl.getKeys() == []


    @patch('xcvrd.xcvrd.XcvrTableHelper.get_status_tbl')
    @patch('xcvrd.xcvrd.platform_chassis')
    @patch('xcvrd.xcvrd.PortChangeObserver', MagicMock(handle_port_update_event=MagicMock()))
    @patch('xcvrd.xcvrd._wrapper_get_sfp_type', MagicMock(return_value='QSFP_DD'))
    @patch('xcvrd.xcvrd.CmisManagerTask.wait_for_port_config_done', MagicMock())
    @patch('xcvrd.xcvrd.is_cmis_api', MagicMock(return_value=True))
    def test_CmisManagerTask_task_worker(self, mock_chassis, mock_get_status_tbl):
        mock_get_status_tbl = Table("STATE_DB", TRANSCEIVER_STATUS_TABLE)
        mock_xcvr_api = MagicMock()
        mock_xcvr_api.set_datapath_deinit = MagicMock(return_value=True)
        mock_xcvr_api.set_datapath_init = MagicMock(return_value=True)
        mock_xcvr_api.tx_disable_channel = MagicMock(return_value=True)
        mock_xcvr_api.set_lpmode = MagicMock(return_value=True)
        mock_xcvr_api.set_application = MagicMock(return_value=True)
        mock_xcvr_api.is_flat_memory = MagicMock(return_value=False)
        mock_xcvr_api.is_coherent_module = MagicMock(return_value=True)
        mock_xcvr_api.get_tx_config_power = MagicMock(return_value=0)
        mock_xcvr_api.get_laser_config_freq = MagicMock(return_value=0)
        mock_xcvr_api.get_module_type_abbreviation = MagicMock(return_value='QSFP-DD')
        mock_xcvr_api.get_datapath_init_duration = MagicMock(return_value=60000.0)
        mock_xcvr_api.get_module_pwr_up_duration = MagicMock(return_value=70000.0)
        mock_xcvr_api.get_datapath_deinit_duration = MagicMock(return_value=600000.0)
        mock_xcvr_api.get_cmis_rev = MagicMock(return_value='5.0')
        mock_xcvr_api.get_supported_freq_config = MagicMock(return_value=(0xA0,0,0,191300,196100))
        mock_xcvr_api.get_dpinit_pending = MagicMock(return_value={
            'DPInitPending1': True,
            'DPInitPending2': True,
            'DPInitPending3': True,
            'DPInitPending4': True,
            'DPInitPending5': True,
            'DPInitPending6': True,
            'DPInitPending7': True,
            'DPInitPending8': True
        })
        mock_xcvr_api.get_application_advertisement = MagicMock(return_value={
            1: {
                'host_electrical_interface_id': '400GAUI-8 C2M (Annex 120E)',
                'module_media_interface_id': '400GBASE-DR4 (Cl 124)',
                'media_lane_count': 4,
                'host_lane_count': 8,
                'host_lane_assignment_options': 1,
                'media_lane_assignment_options': 1
            },
            2: {
                'host_electrical_interface_id': '100GAUI-2 C2M (Annex 135G)',
                'module_media_interface_id': '100G-FR/100GBASE-FR1 (Cl 140)',
                'media_lane_count': 1,
                'host_lane_count': 2,
                'host_lane_assignment_options': 85,
                'media_lane_assignment_options': 15
            }
        })
        mock_xcvr_api.get_module_state = MagicMock(return_value='ModuleReady')
        mock_xcvr_api.get_config_datapath_hostlane_status = MagicMock(return_value={
            'ConfigStatusLane1': 'ConfigSuccess',
            'ConfigStatusLane2': 'ConfigSuccess',
            'ConfigStatusLane3': 'ConfigSuccess',
            'ConfigStatusLane4': 'ConfigSuccess',
            'ConfigStatusLane5': 'ConfigSuccess',
            'ConfigStatusLane6': 'ConfigSuccess',
            'ConfigStatusLane7': 'ConfigSuccess',
            'ConfigStatusLane8': 'ConfigSuccess'
        })
        mock_xcvr_api.get_datapath_state = MagicMock(side_effect=[
            {
                'DP1State': 'DataPathDeactivated',
                'DP2State': 'DataPathDeactivated',
                'DP3State': 'DataPathDeactivated',
                'DP4State': 'DataPathDeactivated',
                'DP5State': 'DataPathDeactivated',
                'DP6State': 'DataPathDeactivated',
                'DP7State': 'DataPathDeactivated',
                'DP8State': 'DataPathDeactivated'
            },
            {
                'DP1State': 'DataPathInitialized',
                'DP2State': 'DataPathInitialized',
                'DP3State': 'DataPathInitialized',
                'DP4State': 'DataPathInitialized',
                'DP5State': 'DataPathInitialized',
                'DP6State': 'DataPathInitialized',
                'DP7State': 'DataPathInitialized',
                'DP8State': 'DataPathInitialized'
            },
            {
                'DP1State': 'DataPathActivated',
                'DP2State': 'DataPathActivated',
                'DP3State': 'DataPathActivated',
                'DP4State': 'DataPathActivated',
                'DP5State': 'DataPathActivated',
                'DP6State': 'DataPathActivated',
                'DP7State': 'DataPathActivated',
                'DP8State': 'DataPathActivated'
            }
        ])
        mock_sfp = MagicMock()
        mock_sfp.get_presence = MagicMock(return_value=True)
        mock_sfp.get_xcvr_api = MagicMock(return_value=mock_xcvr_api)

        mock_chassis.get_all_sfps = MagicMock(return_value=[mock_sfp])
        mock_chassis.get_sfp = MagicMock(return_value=mock_sfp)

        port_mapping = PortMapping()
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event)
        task.port_mapping.logical_port_list = ['Ethernet0']
        task.xcvr_table_helper.get_status_tbl.return_value = mock_get_status_tbl
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_tbl(task.port_mapping.get_asic_id_for_logical_port('Ethernet0'))) == CMIS_STATE_UNKNOWN

        task.port_mapping.logical_port_list = MagicMock()
        port_change_event = PortChangeEvent('PortConfigDone', -1, 0, PortChangeEvent.PORT_SET)
        task.on_port_update_event(port_change_event)
        assert task.isPortConfigDone

        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_SET,
                                            {'speed':'400000', 'lanes':'1,2,3,4,5,6,7,8'})
        task.on_port_update_event(port_change_event)
        assert len(task.port_dict) == 1
        assert get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_tbl(task.port_mapping.get_asic_id_for_logical_port('Ethernet0'))) == CMIS_STATE_INSERTED

        task.get_host_tx_status = MagicMock(return_value='true')
        task.get_port_admin_status = MagicMock(return_value='up')
        task.get_configured_tx_power_from_db = MagicMock(return_value=-13)
        task.get_configured_laser_freq_from_db = MagicMock(return_value=193100)
        task.configure_tx_output_power = MagicMock(return_value=1)
        task.configure_laser_frequency = MagicMock(return_value=1)

        # Case 1: Module Inserted --> DP_DEINIT
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_tbl(task.port_mapping.get_asic_id_for_logical_port('Ethernet0'))) == CMIS_STATE_DP_DEINIT
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert mock_xcvr_api.set_datapath_deinit.call_count == 1
        assert mock_xcvr_api.tx_disable_channel.call_count == 1
        assert mock_xcvr_api.set_lpmode.call_count == 1
        assert get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_tbl(task.port_mapping.get_asic_id_for_logical_port('Ethernet0'))) == CMIS_STATE_AP_CONF

        # Case 2: DP_DEINIT --> AP Configured
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert mock_xcvr_api.set_application.call_count == 1
        assert get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_tbl(task.port_mapping.get_asic_id_for_logical_port('Ethernet0'))) == CMIS_STATE_DP_INIT

        # Case 3: AP Configured --> DP_INIT
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert mock_xcvr_api.set_datapath_init.call_count == 1
        assert get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_tbl(task.port_mapping.get_asic_id_for_logical_port('Ethernet0'))) == CMIS_STATE_DP_TXON

        # Case 4: DP_INIT --> DP_TXON
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert mock_xcvr_api.tx_disable_channel.call_count == 2
        assert get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_tbl(task.port_mapping.get_asic_id_for_logical_port('Ethernet0'))) == CMIS_STATE_DP_ACTIVATE

        # Case 5: DP_TXON --> DP_ACTIVATION
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.post_port_active_apsel_to_db = MagicMock()
        task.task_worker()
        assert task.post_port_active_apsel_to_db.call_count == 1
        assert get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_tbl(task.port_mapping.get_asic_id_for_logical_port('Ethernet0'))) == CMIS_STATE_READY

    @pytest.mark.parametrize("lport, expected_dom_polling", [
        ('Ethernet0', 'disabled'),
        ('Ethernet4', 'disabled'),
        ('Ethernet8', 'disabled'),
        ('Ethernet12', 'disabled'),
        ('Ethernet16', 'enabled'),
        ('Ethernet20', 'enabled')
    ])
    def test_DomInfoUpdateTask_get_dom_polling_from_config_db(self, lport, expected_dom_polling):
        # Define the mock_get function inside the test function
        def mock_get(key):
            if key in ['Ethernet4', 'Ethernet8', 'Ethernet12', 'Ethernet16']:
                return (True, [('dom_polling', 'enabled')])
            elif key == 'Ethernet0':
                return (True, [('dom_polling', 'disabled')])
            else:
                return None

        port_mapping = PortMapping()
        stop_event = threading.Event()
        mock_cmis_manager = MagicMock()
        task = DomInfoUpdateTask(DEFAULT_NAMESPACE, port_mapping, stop_event, mock_cmis_manager)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.port_mapping.handle_port_change_event(PortChangeEvent('Ethernet4', 1, 0, PortChangeEvent.PORT_ADD))
        task.port_mapping.handle_port_change_event(PortChangeEvent('Ethernet12', 1, 0, PortChangeEvent.PORT_ADD))
        task.port_mapping.handle_port_change_event(PortChangeEvent('Ethernet8', 1, 0, PortChangeEvent.PORT_ADD))
        task.port_mapping.handle_port_change_event(PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_ADD))
        task.port_mapping.handle_port_change_event(PortChangeEvent('Ethernet16', 2, 0, PortChangeEvent.PORT_ADD))
        cfg_port_tbl = MagicMock()
        cfg_port_tbl.get = MagicMock(side_effect=mock_get)
        task.xcvr_table_helper.get_cfg_port_tbl = MagicMock(return_value=cfg_port_tbl)

        assert task.get_dom_polling_from_config_db(lport) == expected_dom_polling

    @pytest.mark.parametrize("skip_cmis_manager, is_asic_index_none, mock_cmis_state, expected_result", [
        (True, False, None, False),
        (False, False, CMIS_STATE_INSERTED, True),
        (False, False, CMIS_STATE_READY, False),
        (False, False, CMIS_STATE_UNKNOWN, True),
        (False, True, None, False),
    ])
    @patch('xcvrd.xcvrd.get_cmis_state_from_state_db')
    def test_DomInfoUpdateTask_is_port_in_cmis_initialization_process(self, mock_get_cmis_state_from_state_db, skip_cmis_manager, is_asic_index_none, mock_cmis_state, expected_result):
        port_mapping = PortMapping()
        lport = 'Ethernet0'
        port_change_event = PortChangeEvent(lport, 1, 0, PortChangeEvent.PORT_ADD)
        stop_event = threading.Event()
        task = DomInfoUpdateTask(DEFAULT_NAMESPACE, port_mapping, stop_event, skip_cmis_manager)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.on_port_config_change(port_change_event)
        mock_get_cmis_state_from_state_db.return_value = mock_cmis_state
        if is_asic_index_none:
            lport='INVALID_PORT'
        assert task.is_port_in_cmis_initialization_process(lport) == expected_result

    @patch('xcvrd.xcvrd.XcvrTableHelper', MagicMock())
    @patch('xcvrd.xcvrd.delete_port_from_status_table_hw')
    def test_DomInfoUpdateTask_handle_port_change_event(self, mock_del_status_tbl_hw):
        port_mapping = PortMapping()
        stop_event = threading.Event()
        mock_cmis_manager = MagicMock()
        task = DomInfoUpdateTask(DEFAULT_NAMESPACE, port_mapping, stop_event, mock_cmis_manager)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_ADD)
        task.on_port_config_change(port_change_event)
        assert task.port_mapping.logical_port_list.count('Ethernet0')
        assert task.port_mapping.get_asic_id_for_logical_port('Ethernet0') == 0
        assert task.port_mapping.get_physical_to_logical(1) == ['Ethernet0']
        assert task.port_mapping.get_logical_to_physical('Ethernet0') == [1]
        assert mock_del_status_tbl_hw.call_count == 0

        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_REMOVE)
        task.on_port_config_change(port_change_event)
        assert not task.port_mapping.logical_port_list
        assert not task.port_mapping.logical_to_physical
        assert not task.port_mapping.physical_to_logical
        assert not task.port_mapping.logical_to_asic
        assert mock_del_status_tbl_hw.call_count == 1

    @patch('xcvrd.xcvrd_utilities.port_event_helper.subscribe_port_config_change', MagicMock(return_value=(None, None)))
    @patch('xcvrd.xcvrd_utilities.port_event_helper.handle_port_config_change', MagicMock())
    def test_DomInfoUpdateTask_task_run_stop(self):
        port_mapping = PortMapping()
        stop_event = threading.Event()
        mock_cmis_manager = MagicMock()
        task = DomInfoUpdateTask(DEFAULT_NAMESPACE, port_mapping, stop_event, mock_cmis_manager)
        task.start()
        task.join()
        assert not task.is_alive()

    @patch('xcvrd.xcvrd.XcvrTableHelper', MagicMock())
    @patch('xcvrd.xcvrd_utilities.sfp_status_helper.detect_port_in_error_status')
    @patch('xcvrd.xcvrd.post_port_sfp_firmware_info_to_db')
    @patch('xcvrd.xcvrd.post_port_dom_info_to_db')
    @patch('swsscommon.swsscommon.Select.addSelectable', MagicMock())
    @patch('swsscommon.swsscommon.SubscriberStateTable')
    @patch('swsscommon.swsscommon.Select.select')
    @patch('xcvrd.xcvrd.update_port_transceiver_status_table_hw')
    @patch('xcvrd.xcvrd.post_port_pm_info_to_db')
    def test_DomInfoUpdateTask_task_worker(self, mock_post_pm_info, mock_update_status_hw,
                                           mock_select, mock_sub_table,
                                           mock_post_dom_info, mock_post_firmware_info, mock_detect_error):
        mock_selectable = MagicMock()
        mock_selectable.pop = MagicMock(
            side_effect=[('Ethernet0', swsscommon.SET_COMMAND, (('index', '1'), )), (None, None, None), (None, None, None), (None, None, None)])
        mock_select.return_value = (swsscommon.Select.OBJECT, mock_selectable)
        mock_sub_table.return_value = mock_selectable

        port_mapping = PortMapping()
        stop_event = threading.Event()
        mock_cmis_manager = MagicMock()
        task = DomInfoUpdateTask(DEFAULT_NAMESPACE, port_mapping, stop_event, mock_cmis_manager)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.task_stopping_event.wait = MagicMock(side_effect=[False, True])
        task.get_dom_polling_from_config_db = MagicMock(return_value='enabled')
        task.is_port_in_cmis_terminal_state = MagicMock(return_value=False)
        mock_detect_error.return_value = True
        task.task_worker()
        assert task.port_mapping.logical_port_list.count('Ethernet0')
        assert task.port_mapping.get_asic_id_for_logical_port('Ethernet0') == 0
        assert task.port_mapping.get_physical_to_logical(1) == ['Ethernet0']
        assert task.port_mapping.get_logical_to_physical('Ethernet0') == [1]
        assert mock_post_firmware_info.call_count == 0
        assert mock_post_dom_info.call_count == 0
        assert mock_update_status_hw.call_count == 0
        assert mock_post_pm_info.call_count == 0
        mock_detect_error.return_value = False
        task.task_stopping_event.wait = MagicMock(side_effect=[False, False, True])
        task.get_dom_polling_from_config_db = MagicMock(side_effect=('disabled', 'enabled'))
        task.task_worker()
        assert mock_post_firmware_info.call_count == 1
        assert mock_post_dom_info.call_count == 1
        assert mock_update_status_hw.call_count == 1
        assert mock_post_pm_info.call_count == 1

    @patch('xcvrd.xcvrd._wrapper_get_presence', MagicMock(return_value=False))
    @patch('xcvrd.xcvrd.XcvrTableHelper')
    @patch('xcvrd.xcvrd.delete_port_from_status_table_hw')
    def test_SfpStateUpdateTask_handle_port_change_event(self, mock_update_status_hw, mock_table_helper):
        mock_table = MagicMock()
        mock_table.get = MagicMock(return_value=(False, None))
        mock_table_helper.get_status_tbl = MagicMock(return_value=mock_table)
        mock_table_helper.get_int_tbl = MagicMock(return_value=mock_table)
        mock_table_helper.get_dom_tbl = MagicMock(return_value=mock_table)
        mock_table_helper.get_dom_threshold_tbl = MagicMock(return_value=mock_table)
        stop_event = threading.Event()
        sfp_error_event = threading.Event()
        port_mapping = PortMapping()
        task = SfpStateUpdateTask(DEFAULT_NAMESPACE, port_mapping, stop_event, sfp_error_event)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.xcvr_table_helper.get_status_tbl = mock_table_helper.get_status_tbl
        task.xcvr_table_helper.get_intf_tbl = mock_table_helper.get_intf_tbl
        task.xcvr_table_helper.get_dom_tbl = mock_table_helper.get_dom_tbl
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_ADD)
        wait_time = 5
        while wait_time > 0:
            task.on_port_config_change(port_change_event)
            if task.port_mapping.logical_port_list:
                break
            wait_time -= 1
            time.sleep(1)
        assert task.port_mapping.logical_port_list.count('Ethernet0')
        assert task.port_mapping.get_asic_id_for_logical_port('Ethernet0') == 0
        assert task.port_mapping.get_physical_to_logical(1) == ['Ethernet0']
        assert task.port_mapping.get_logical_to_physical('Ethernet0') == [1]
        assert mock_update_status_hw.call_count == 0

        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_REMOVE)
        wait_time = 5
        while wait_time > 0:
            task.on_port_config_change(port_change_event)
            if not task.port_mapping.logical_port_list:
                break
            wait_time -= 1
            time.sleep(1)
        assert not task.port_mapping.logical_port_list
        assert not task.port_mapping.logical_to_physical
        assert not task.port_mapping.physical_to_logical
        assert not task.port_mapping.logical_to_asic
        assert mock_update_status_hw.call_count == 1

    def test_SfpStateUpdateTask_task_run_stop(self):
        def poll_forever(*args, **kwargs):
            while True:
                time.sleep(1)
        # Redefine the XcvrTableHelper function to poll forever so that the task can be stopped by
        # raising an exception in between. Also, XcvrTableHelper is the first function to be called after
        # starting the task, so having the patch here will avoid the task crashing unexpectedly
        # at a different location.
        with patch('xcvrd.xcvrd.XcvrTableHelper', new=poll_forever):
            port_mapping = PortMapping()
            stop_event = threading.Event()
            sfp_error_event = threading.Event()
            task = SfpStateUpdateTask(DEFAULT_NAMESPACE, port_mapping, stop_event, sfp_error_event)
            task.start()
            assert wait_until(5, 1, task.is_alive)
            task.raise_exception()
            task.join()
            assert wait_until(5, 1, lambda: task.is_alive() is False)

    @patch('xcvrd.xcvrd.XcvrTableHelper', MagicMock())
    @patch('xcvrd.xcvrd.post_port_sfp_info_to_db')
    @patch('xcvrd.xcvrd.XcvrTableHelper.get_cfg_port_tbl', MagicMock())
    def test_SfpStateUpdateTask_retry_eeprom_reading(self, mock_post_sfp_info):
        mock_table = MagicMock()
        mock_table.get = MagicMock(return_value=(False, None))

        port_mapping = PortMapping()
        stop_event = threading.Event()
        sfp_error_event = threading.Event()
        task = SfpStateUpdateTask(DEFAULT_NAMESPACE, port_mapping, stop_event, sfp_error_event)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.xcvr_table_helper.get_intf_tbl = MagicMock(return_value=mock_table)
        task.xcvr_table_helper.get_dom_threshold_tbl = MagicMock(return_value=mock_table)
        task.xcvr_table_helper.get_app_port_tbl = MagicMock(return_value=mock_table)
        task.xcvr_table_helper.get_status_tbl = MagicMock(return_value=mock_table)
        task.xcvr_table_helper.get_firmware_info_tbl = MagicMock(return_value=mock_table)
        task.retry_eeprom_reading()
        assert mock_post_sfp_info.call_count == 0

        task.retry_eeprom_set.add('Ethernet0')
        task.last_retry_eeprom_time = time.time()
        task.retry_eeprom_reading()
        assert mock_post_sfp_info.call_count == 0

        task.last_retry_eeprom_time = 0
        mock_post_sfp_info.return_value = SFP_EEPROM_NOT_READY
        task.retry_eeprom_reading()
        assert 'Ethernet0' in task.retry_eeprom_set

        task.last_retry_eeprom_time = 0
        mock_post_sfp_info.return_value = None
        task.retry_eeprom_reading()
        assert 'Ethernet0' not in task.retry_eeprom_set

    def test_SfpStateUpdateTask_mapping_event_from_change_event(self):
        port_mapping = PortMapping()
        stop_event = threading.Event()
        sfp_error_event = threading.Event()
        task = SfpStateUpdateTask(DEFAULT_NAMESPACE, port_mapping, stop_event, sfp_error_event)
        port_dict = {}
        assert task._mapping_event_from_change_event(False, port_dict) == SYSTEM_FAIL
        assert port_dict[EVENT_ON_ALL_SFP] == SYSTEM_FAIL

        port_dict = {EVENT_ON_ALL_SFP: SYSTEM_FAIL}
        assert task._mapping_event_from_change_event(False, port_dict) == SYSTEM_FAIL

        port_dict = {}
        assert task._mapping_event_from_change_event(True, port_dict) == SYSTEM_BECOME_READY
        assert port_dict[EVENT_ON_ALL_SFP] == SYSTEM_BECOME_READY

        port_dict = {1, SFP_STATUS_INSERTED}
        assert task._mapping_event_from_change_event(True, port_dict) == NORMAL_EVENT

    @patch('time.sleep', MagicMock())
    @patch('xcvrd.xcvrd.XcvrTableHelper', MagicMock())
    @patch('xcvrd.xcvrd._wrapper_soak_sfp_insert_event', MagicMock())
    @patch('xcvrd.xcvrd_utilities.port_event_helper.subscribe_port_config_change', MagicMock(return_value=(None, None)))
    @patch('xcvrd.xcvrd_utilities.port_event_helper.handle_port_config_change', MagicMock())
    @patch('xcvrd.xcvrd.SfpStateUpdateTask.init', MagicMock())
    @patch('os.kill')
    @patch('xcvrd.xcvrd.SfpStateUpdateTask._mapping_event_from_change_event')
    @patch('xcvrd.xcvrd._wrapper_get_transceiver_change_event')
    @patch('xcvrd.xcvrd.del_port_sfp_dom_info_from_db')
    @patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting')
    @patch('xcvrd.xcvrd.post_port_sfp_firmware_info_to_db')
    @patch('xcvrd.xcvrd.post_port_dom_threshold_info_to_db')
    @patch('xcvrd.xcvrd.post_port_sfp_info_to_db')
    @patch('xcvrd.xcvrd.update_port_transceiver_status_table_sw')
    @patch('xcvrd.xcvrd.delete_port_from_status_table_hw')
    def test_SfpStateUpdateTask_task_worker(self, mock_del_status_hw,
            mock_update_status, mock_post_sfp_info, mock_post_dom_th, mock_post_firmware_info, mock_update_media_setting,
            mock_del_dom, mock_change_event, mock_mapping_event, mock_os_kill):
        port_mapping = PortMapping()
        stop_event = threading.Event()
        sfp_error_event = threading.Event()
        task = SfpStateUpdateTask(DEFAULT_NAMESPACE, port_mapping, stop_event, sfp_error_event)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        mock_change_event.return_value = (True, {0: 0}, {})
        mock_mapping_event.return_value = SYSTEM_NOT_READY

        # Test state machine: STATE_INIT + SYSTEM_NOT_READY event => STATE_INIT + SYSTEM_NOT_READY event ... => STATE_EXIT
        task.task_worker(stop_event, sfp_error_event)
        assert mock_os_kill.call_count == 1
        assert sfp_error_event.is_set()

        mock_mapping_event.return_value = SYSTEM_FAIL
        mock_os_kill.reset_mock()
        sfp_error_event.clear()
        # Test state machine: STATE_INIT + SYSTEM_FAIL event => STATE_INIT + SYSTEM_FAIL event ... => STATE_EXIT
        task.task_worker(stop_event, sfp_error_event)
        assert mock_os_kill.call_count == 1
        assert sfp_error_event.is_set()

        mock_mapping_event.side_effect = [SYSTEM_BECOME_READY, SYSTEM_NOT_READY]
        mock_os_kill.reset_mock()
        sfp_error_event.clear()
        # Test state machine: STATE_INIT + SYSTEM_BECOME_READY event => STATE_NORMAL + SYSTEM_NOT_READY event ... => STATE_EXIT
        task.task_worker(stop_event, sfp_error_event)
        assert mock_os_kill.call_count == 1
        assert not sfp_error_event.is_set()

        mock_mapping_event.side_effect = [SYSTEM_BECOME_READY, SYSTEM_FAIL] + \
            [SYSTEM_FAIL] * (RETRY_TIMES_FOR_SYSTEM_READY + 1)
        mock_os_kill.reset_mock()
        sfp_error_event.clear()
        # Test state machine: STATE_INIT + SYSTEM_BECOME_READY event => STATE_NORMAL + SYSTEM_FAIL event ... => STATE_INIT
        # + SYSTEM_FAIL event ... => STATE_EXIT
        task.task_worker(stop_event, sfp_error_event)
        assert mock_os_kill.call_count == 1
        assert sfp_error_event.is_set()

        task.port_mapping.handle_port_change_event(PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_ADD))
        mock_change_event.return_value = (True, {1: SFP_STATUS_INSERTED}, {})
        mock_mapping_event.side_effect = None
        mock_mapping_event.return_value = NORMAL_EVENT
        mock_post_sfp_info.return_value = SFP_EEPROM_NOT_READY
        stop_event.is_set = MagicMock(side_effect=[False, True])
        # Test state machine: handle SFP insert event, but EEPROM read failure
        task.task_worker(stop_event, sfp_error_event)
        assert mock_update_status.call_count == 1
        assert mock_post_sfp_info.call_count == 2  # first call and retry call
        assert mock_post_dom_th.call_count == 0
        assert mock_post_firmware_info.call_count == 0
        assert mock_update_media_setting.call_count == 0
        assert 'Ethernet0' in task.retry_eeprom_set
        task.retry_eeprom_set.clear()

        stop_event.is_set = MagicMock(side_effect=[False, True])
        mock_post_sfp_info.return_value = None
        mock_update_status.reset_mock()
        mock_post_sfp_info.reset_mock()
        # Test state machine: handle SFP insert event, and EEPROM read success
        task.task_worker(stop_event, sfp_error_event)
        assert mock_update_status.call_count == 1
        assert mock_post_sfp_info.call_count == 1
        assert mock_post_dom_th.call_count == 1
        assert mock_post_firmware_info.call_count == 0
        assert mock_update_media_setting.call_count == 1

        stop_event.is_set = MagicMock(side_effect=[False, True])
        mock_change_event.return_value = (True, {1: SFP_STATUS_REMOVED}, {})
        mock_update_status.reset_mock()
        # Test state machine: handle SFP remove event
        task.task_worker(stop_event, sfp_error_event)
        assert mock_update_status.call_count == 1
        assert mock_del_dom.call_count == 1
        assert mock_del_status_hw.call_count == 1

        stop_event.is_set = MagicMock(side_effect=[False, True])
        error = int(SFP_STATUS_INSERTED) | SfpBase.SFP_ERROR_BIT_BLOCKING | SfpBase.SFP_ERROR_BIT_POWER_BUDGET_EXCEEDED
        mock_change_event.return_value = (True, {1: error}, {})
        mock_update_status.reset_mock()
        mock_del_dom.reset_mock()
        mock_del_status_hw.reset_mock()
        # Test state machine: handle SFP error event
        task.task_worker(stop_event, sfp_error_event)
        assert mock_update_status.call_count == 1
        assert mock_del_dom.call_count == 1
        assert mock_del_status_hw.call_count == 1

    @patch('xcvrd.xcvrd.XcvrTableHelper')
    @patch('xcvrd.xcvrd._wrapper_get_presence')
    @patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting')
    @patch('xcvrd.xcvrd.post_port_dom_threshold_info_to_db')
    @patch('xcvrd.xcvrd.post_port_sfp_info_to_db')
    @patch('xcvrd.xcvrd.update_port_transceiver_status_table_sw')
    def test_SfpStateUpdateTask_on_add_logical_port(self, mock_update_status, mock_post_sfp_info,
            mock_post_dom_th, mock_update_media_setting, mock_get_presence, mock_table_helper):
        class MockTable:
            pass

        status_tbl = MockTable()
        status_tbl.get = MagicMock(return_value=(True, (('status', SFP_STATUS_INSERTED),)))
        status_tbl.set = MagicMock()
        int_tbl = MockTable()
        int_tbl.get = MagicMock(return_value=(True, (('key2', 'value2'),)))
        int_tbl.set = MagicMock()
        dom_threshold_tbl = MockTable()
        dom_threshold_tbl.get = MagicMock(return_value=(True, (('key4', 'value4'),)))
        dom_threshold_tbl.set = MagicMock()
        mock_table_helper.get_status_tbl = MagicMock(return_value=status_tbl)
        mock_table_helper.get_intf_tbl = MagicMock(return_value=int_tbl)
        mock_table_helper.get_dom_threshold_tbl = MagicMock(return_value=dom_threshold_tbl)

        port_mapping = PortMapping()
        stop_event = threading.Event()
        sfp_error_event = threading.Event()
        task = SfpStateUpdateTask(DEFAULT_NAMESPACE, port_mapping, stop_event, sfp_error_event)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.xcvr_table_helper.get_status_tbl = mock_table_helper.get_status_tbl
        task.xcvr_table_helper.get_intf_tbl = mock_table_helper.get_intf_tbl
        task.xcvr_table_helper.get_dom_threshold_tbl = mock_table_helper.get_dom_threshold_tbl
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_ADD)
        task.port_mapping.handle_port_change_event(port_change_event)

        status_tbl.get.return_value = (False, ())
        mock_get_presence.return_value = True
        mock_post_sfp_info.return_value = SFP_EEPROM_NOT_READY
        # SFP information is not in the DB, and SFP is present, and SFP has no error, but SFP EEPROM reading failed
        task.on_add_logical_port(port_change_event)
        assert mock_update_status.call_count == 1
        mock_update_status.assert_called_with('Ethernet0', status_tbl, SFP_STATUS_INSERTED, 'N/A')
        assert mock_post_sfp_info.call_count == 1
        mock_post_sfp_info.assert_called_with('Ethernet0', task.port_mapping, int_tbl, {})
        assert mock_post_dom_th.call_count == 0
        assert mock_update_media_setting.call_count == 0
        assert 'Ethernet0' in task.retry_eeprom_set
        task.retry_eeprom_set.clear()

        mock_post_sfp_info.return_value = None
        mock_update_status.reset_mock()
        mock_post_sfp_info.reset_mock()
        # SFP information is not in the DB, and SFP is present, and SFP has no error, and SFP EEPROM reading succeed
        task.on_add_logical_port(port_change_event)
        assert mock_update_status.call_count == 1
        mock_update_status.assert_called_with('Ethernet0', status_tbl, SFP_STATUS_INSERTED, 'N/A')
        assert mock_post_sfp_info.call_count == 1
        mock_post_sfp_info.assert_called_with('Ethernet0', task.port_mapping, int_tbl, {})
        assert mock_post_dom_th.call_count == 1
        mock_post_dom_th.assert_called_with('Ethernet0', task.port_mapping, dom_threshold_tbl)
        assert mock_update_media_setting.call_count == 1
        assert 'Ethernet0' not in task.retry_eeprom_set

        mock_get_presence.return_value = False
        mock_update_status.reset_mock()
        # SFP information is not in DB and SFP is not present
        task.on_add_logical_port(port_change_event)
        assert mock_update_status.call_count == 1
        mock_update_status.assert_called_with('Ethernet0', status_tbl, SFP_STATUS_REMOVED, 'N/A')

        task.sfp_error_dict[1] = (str(SfpBase.SFP_ERROR_BIT_BLOCKING | SfpBase.SFP_ERROR_BIT_POWER_BUDGET_EXCEEDED), {})
        mock_update_status.reset_mock()
        # SFP information is not in DB, and SFP is not present, and SFP is in error status
        task.on_add_logical_port(port_change_event)
        assert mock_update_status.call_count == 1
        mock_update_status.assert_called_with(
            'Ethernet0', status_tbl, task.sfp_error_dict[1][0], 'Blocking EEPROM from being read|Power budget exceeded')

    def test_sfp_insert_events(self):
        from xcvrd.xcvrd import _wrapper_soak_sfp_insert_event
        sfp_insert_events = {}
        insert = port_dict = {1: '1', 2: '1', 3: '1', 4: '1', 5: '1'}
        start = time.time()
        while True:
            _wrapper_soak_sfp_insert_event(sfp_insert_events, insert)
            if time.time() - start > MGMT_INIT_TIME_DELAY_SECS:
                break
            assert not bool(insert)
        assert insert == port_dict

    def test_sfp_remove_events(self):
        from xcvrd.xcvrd import _wrapper_soak_sfp_insert_event
        sfp_insert_events = {}
        insert = {1: '1', 2: '1', 3: '1', 4: '1', 5: '1'}
        removal = {1: '0', 2: '0', 3: '0', 4: '0', 5: '0'}
        port_dict = {1: '0', 2: '0', 3: '0', 4: '0', 5: '0'}
        for x in range(5):
            _wrapper_soak_sfp_insert_event(sfp_insert_events, insert)
            time.sleep(1)
            _wrapper_soak_sfp_insert_event(sfp_insert_events, removal)

        assert port_dict == removal

    @patch('xcvrd.xcvrd.platform_chassis')
    @patch('xcvrd.xcvrd.platform_sfputil')
    def test_wrapper_get_presence(self, mock_sfputil, mock_chassis):
        mock_object = MagicMock()
        mock_object.get_presence = MagicMock(return_value=True)
        mock_chassis.get_sfp = MagicMock(return_value=mock_object)
        from xcvrd.xcvrd import _wrapper_get_presence
        assert _wrapper_get_presence(1)

        mock_object.get_presence = MagicMock(return_value=False)
        assert not _wrapper_get_presence(1)

        mock_chassis.get_sfp = MagicMock(side_effect=NotImplementedError)
        mock_sfputil.get_presence = MagicMock(return_value=True)

        assert _wrapper_get_presence(1)

        mock_sfputil.get_presence = MagicMock(return_value=False)
        assert not _wrapper_get_presence(1)

    @patch('xcvrd.xcvrd.platform_chassis')
    def test_wrapper_is_replaceable(self, mock_chassis):
        mock_object = MagicMock()
        mock_object.is_replaceable = MagicMock(return_value=True)
        mock_chassis.get_sfp = MagicMock(return_value=mock_object)
        from xcvrd.xcvrd import _wrapper_is_replaceable
        assert _wrapper_is_replaceable(1)

        mock_object.is_replaceable = MagicMock(return_value=False)
        assert not _wrapper_is_replaceable(1)

        mock_chassis.get_sfp = MagicMock(side_effect=NotImplementedError)
        assert not _wrapper_is_replaceable(1)

    @patch('xcvrd.xcvrd.platform_chassis')
    @patch('xcvrd.xcvrd.platform_sfputil')
    def test_wrapper_get_transceiver_info(self, mock_sfputil, mock_chassis):
        mock_object = MagicMock()
        mock_object.get_transceiver_info = MagicMock(return_value=True)
        mock_chassis.get_sfp = MagicMock(return_value=mock_object)
        from xcvrd.xcvrd import _wrapper_get_transceiver_info
        assert _wrapper_get_transceiver_info(1)

        mock_object.get_transceiver_info = MagicMock(return_value=False)
        assert not _wrapper_get_transceiver_info(1)

        mock_chassis.get_sfp = MagicMock(side_effect=NotImplementedError)
        mock_sfputil.get_transceiver_info_dict = MagicMock(return_value=True)

        assert _wrapper_get_transceiver_info(1)

        mock_sfputil.get_transceiver_info_dict = MagicMock(return_value=False)
        assert not _wrapper_get_transceiver_info(1)

    @patch('xcvrd.xcvrd.platform_chassis')
    @patch('xcvrd.xcvrd.platform_sfputil')
    def test_wrapper_get_transceiver_firmware_info(self, mock_sfputil, mock_chassis):
        mock_object = MagicMock()
        mock_object.get_transceiver_bulk_status = MagicMock(return_value=True)
        mock_chassis.get_sfp = MagicMock(return_value=mock_object)
        from xcvrd.xcvrd import _wrapper_get_transceiver_firmware_info
        assert _wrapper_get_transceiver_firmware_info(1)

        mock_object.get_transceiver_info_firmware_versions = MagicMock(return_value={})
        assert _wrapper_get_transceiver_firmware_info(1) == {}

        mock_chassis.get_sfp = MagicMock(side_effect=NotImplementedError)
        assert _wrapper_get_transceiver_firmware_info(1) == {}

    @patch('xcvrd.xcvrd.platform_chassis')
    @patch('xcvrd.xcvrd.platform_sfputil')
    def test_wrapper_get_transceiver_dom_info(self, mock_sfputil, mock_chassis):
        mock_object = MagicMock()
        mock_object.get_transceiver_bulk_status = MagicMock(return_value=True)
        mock_chassis.get_sfp = MagicMock(return_value=mock_object)
        from xcvrd.xcvrd import _wrapper_get_transceiver_dom_info
        assert _wrapper_get_transceiver_dom_info(1)

        mock_object.get_transceiver_bulk_status = MagicMock(return_value=False)
        assert not _wrapper_get_transceiver_dom_info(1)

        mock_chassis.get_sfp = MagicMock(side_effect=NotImplementedError)
        mock_sfputil.get_transceiver_dom_info_dict = MagicMock(return_value=True)

        assert _wrapper_get_transceiver_dom_info(1)

        mock_sfputil.get_transceiver_dom_info_dict = MagicMock(return_value=False)
        assert not _wrapper_get_transceiver_dom_info(1)

    @patch('xcvrd.xcvrd.platform_chassis')
    @patch('xcvrd.xcvrd.platform_sfputil')
    def test_wrapper_get_transceiver_dom_threshold_info(self, mock_sfputil, mock_chassis):
        mock_object = MagicMock()
        mock_object.get_transceiver_threshold_info = MagicMock(return_value=True)
        mock_chassis.get_sfp = MagicMock(return_value=mock_object)
        from xcvrd.xcvrd import _wrapper_get_transceiver_dom_threshold_info
        assert _wrapper_get_transceiver_dom_threshold_info(1)

        mock_object.get_transceiver_threshold_info = MagicMock(return_value=False)
        assert not _wrapper_get_transceiver_dom_threshold_info(1)

        mock_chassis.get_sfp = MagicMock(side_effect=NotImplementedError)
        mock_sfputil.get_transceiver_dom_threshold_info_dict = MagicMock(return_value=True)

        assert _wrapper_get_transceiver_dom_threshold_info(1)

        mock_sfputil.get_transceiver_dom_threshold_info_dict = MagicMock(return_value=False)
        assert not _wrapper_get_transceiver_dom_threshold_info(1)

    @patch('xcvrd.xcvrd.platform_chassis')
    def test_wrapper_get_transceiver_status(self, mock_chassis):
        mock_object = MagicMock()
        mock_object.get_transceiver_status= MagicMock(return_value=True)
        mock_chassis.get_sfp = MagicMock(return_value=mock_object)
        from xcvrd.xcvrd import _wrapper_get_transceiver_status
        assert _wrapper_get_transceiver_status(1)

        mock_object.get_transceiver_status = MagicMock(return_value=False)
        assert not _wrapper_get_transceiver_status(1)

        mock_chassis.get_sfp = MagicMock(side_effect=NotImplementedError)
        assert _wrapper_get_transceiver_status(1) == {}

    @patch('xcvrd.xcvrd.platform_chassis')
    def test_wrapper_get_transceiver_pm(self, mock_chassis):
        mock_object = MagicMock()
        mock_object.get_transceiver_pm = MagicMock(return_value=True)
        mock_chassis.get_sfp = MagicMock(return_value=mock_object)
        from xcvrd.xcvrd import _wrapper_get_transceiver_pm
        assert _wrapper_get_transceiver_pm(1)

        mock_object.get_transceiver_pm = MagicMock(return_value=False)
        assert not _wrapper_get_transceiver_pm(1)

        mock_chassis.get_sfp = MagicMock(side_effect=NotImplementedError)
        assert _wrapper_get_transceiver_pm(1) == {}

    @patch('xcvrd.xcvrd.platform_chassis')
    @patch('xcvrd.xcvrd.platform_sfputil')
    def test_wrapper_get_transceiver_change_event(self, mock_sfputil, mock_chassis):
        mock_chassis.get_change_event = MagicMock(return_value=(True, {'sfp': 1, 'sfp_error': 'N/A'}))
        from xcvrd.xcvrd import _wrapper_get_transceiver_change_event
        assert _wrapper_get_transceiver_change_event(0) == (True, 1, 'N/A')

        mock_chassis.get_change_event = MagicMock(side_effect=NotImplementedError)
        mock_sfputil.get_transceiver_change_event = MagicMock(return_value=(True, 1))

        assert _wrapper_get_transceiver_change_event(0) == (True, 1, None)

    @patch('xcvrd.xcvrd.platform_chassis')
    def test_wrapper_get_sfp_type(self, mock_chassis):
        mock_object = MagicMock()
        mock_object.sfp_type = 'QSFP'
        mock_chassis.get_sfp = MagicMock(return_value=mock_object)
        from xcvrd.xcvrd import _wrapper_get_sfp_type
        assert _wrapper_get_sfp_type(1) == 'QSFP'

        mock_chassis.get_sfp = MagicMock(side_effect=NotImplementedError)
        assert not _wrapper_get_sfp_type(1)

    @patch('xcvrd.xcvrd.platform_chassis')
    def test_wrapper_get_sfp_error_description(self, mock_chassis):
        mock_object = MagicMock()
        mock_object.get_error_description = MagicMock(return_value='N/A')
        mock_chassis.get_sfp = MagicMock(return_value=mock_object)
        from xcvrd.xcvrd import _wrapper_get_sfp_error_description
        assert _wrapper_get_sfp_error_description(1) == 'N/A'

        mock_chassis.get_sfp = MagicMock(side_effect=NotImplementedError)
        assert not _wrapper_get_sfp_error_description(1)

    @patch('xcvrd.xcvrd.platform_chassis')
    def test_wrapper_is_flat_memory(self, mock_chassis):
        mock_api = MagicMock()
        mock_api.is_flat_memory = MagicMock(return_value=True)
        mock_object = MagicMock()
        mock_object.get_xcvr_api = MagicMock(return_value=mock_api)
        mock_chassis.get_sfp = MagicMock(return_value=mock_object)

        from xcvrd.xcvrd import _wrapper_is_flat_memory
        assert _wrapper_is_flat_memory(1) == True

        mock_chassis.get_sfp = MagicMock(side_effect=NotImplementedError)
        assert not _wrapper_is_flat_memory(1)

    def test_check_port_in_range(self):
        range_str = '1 - 32'
        physical_port = 1
        assert check_port_in_range(range_str, physical_port)

        physical_port = 32
        assert check_port_in_range(range_str, physical_port)

        physical_port = 0
        assert not check_port_in_range(range_str, physical_port)

        physical_port = 33
        assert not check_port_in_range(range_str, physical_port)

    def test_get_media_val_str_from_dict(self):
        media_dict = {'lane0': '1', 'lane1': '2'}
        media_str = media_settings_parser.get_media_val_str_from_dict(media_dict)
        assert media_str == '1,2'

    def test_get_media_val_str(self):
        num_logical_ports = 1
        lane_dict = {'lane0': '1', 'lane1': '2', 'lane2': '3', 'lane3': '4'}
        logical_idx = 1
        media_str = get_media_val_str(num_logical_ports, lane_dict, logical_idx)
        assert media_str == '1,2,3,4'
        num_logical_ports = 2
        logical_idx = 1
        media_str = get_media_val_str(num_logical_ports, lane_dict, logical_idx)
        assert media_str == '3,4'

    class MockPortMapping:
        logical_port_list = [0, 1, 2]
        logical_port_name_to_physical_port_list = MagicMock()
        get_asic_id_for_logical_port = MagicMock()

    @patch('xcvrd.xcvrd.DaemonXcvrd.load_platform_util', MagicMock())
    @patch('xcvrd.xcvrd_utilities.port_event_helper.get_port_mapping', MagicMock(return_value=MockPortMapping))
    @patch('sonic_py_common.device_info.get_paths_to_platform_and_hwsku_dirs', MagicMock(return_value=('/tmp', '/tmp')))
    @patch('swsscommon.swsscommon.WarmStart', MagicMock())
    @patch('xcvrd.xcvrd.DaemonXcvrd.wait_for_port_config_done', MagicMock())
    def test_DaemonXcvrd_init_deinit_fastboot_enabled(self):
        xcvrd = DaemonXcvrd(SYSLOG_IDENTIFIER)
        with patch("subprocess.check_output") as mock_run:
            mock_run.return_value = "true"

            xcvrd.init()

            status_tbl = MagicMock()
            xcvrd.xcvr_table_helper.get_status_tbl = MagicMock(return_value=status_tbl)
            xcvrd.xcvr_table_helper.get_dom_tbl = MagicMock(return_value=MagicMock)
            xcvrd.xcvr_table_helper.get_dom_threshold_tbl = MagicMock(return_value=MagicMock)
            xcvrd.xcvr_table_helper.get_pm_tbl = MagicMock(return_value=MagicMock)
            xcvrd.xcvr_table_helper.get_firmware_info_tbl = MagicMock(return_value=MagicMock)

            xcvrd.deinit()

            status_tbl.hdel.assert_not_called()

    @patch('xcvrd.xcvrd.DaemonXcvrd.load_platform_util', MagicMock())
    @patch('xcvrd.xcvrd_utilities.port_event_helper.get_port_mapping', MagicMock(return_value=MockPortMapping))
    @patch('sonic_py_common.device_info.get_paths_to_platform_and_hwsku_dirs', MagicMock(return_value=('/tmp', '/tmp')))
    @patch('xcvrd.xcvrd.is_warm_reboot_enabled', MagicMock(return_value=False))
    @patch('xcvrd.xcvrd.DaemonXcvrd.wait_for_port_config_done', MagicMock())
    @patch('subprocess.check_output', MagicMock(return_value='false'))
    def test_DaemonXcvrd_init_deinit_cold(self):
        xcvrd.platform_chassis = MagicMock()

        xcvrdaemon = DaemonXcvrd(SYSLOG_IDENTIFIER)
        with patch("subprocess.check_output") as mock_run:
            mock_run.return_value = "false"

            xcvrdaemon.init()

            status_tbl = MagicMock()
            xcvrdaemon.xcvr_table_helper.get_status_tbl = MagicMock(return_value=status_tbl)
            xcvrdaemon.xcvr_table_helper.get_dom_tbl = MagicMock(return_value=MagicMock)
            xcvrdaemon.xcvr_table_helper.get_dom_threshold_tbl = MagicMock(return_value=MagicMock)
            xcvrdaemon.xcvr_table_helper.get_pm_tbl = MagicMock(return_value=MagicMock)
            xcvrdaemon.xcvr_table_helper.get_firmware_info_tbl = MagicMock(return_value=MagicMock)
            xcvrdaemon.xcvr_table_helper.get_intf_tbl = MagicMock(return_value=MagicMock)

            xcvrdaemon.deinit()

            status_tbl.hdel.assert_called()
            
    @patch('sonic_py_common.device_info.get_paths_to_platform_and_hwsku_dirs', MagicMock(return_value=(test_path, '/invalid/path')))
    def test_load_optical_si_file_from_platform_folder(self):
        assert optics_si_parser.load_optics_si_settings() != {}

    @patch('sonic_py_common.device_info.get_paths_to_platform_and_hwsku_dirs', MagicMock(return_value=('/invalid/path', test_path)))
    def test_load_optical_si_file_from_hwsku_folder(self):
        assert optics_si_parser.load_optics_si_settings() != {}

    @patch('sonic_py_common.device_info.get_paths_to_platform_and_hwsku_dirs', MagicMock(return_value=(test_path, '/invalid/path')))
    def test_load_media_settings_file_from_platform_folder(self):
        assert media_settings_parser.load_media_settings() != {}

    @patch('sonic_py_common.device_info.get_paths_to_platform_and_hwsku_dirs', MagicMock(return_value=('/invalid/path', test_path)))
    def test_load_media_settings_file_from_hwsku_folder(self):
        assert media_settings_parser.load_media_settings() != {}
        
    @pytest.mark.parametrize("lport, freq, grid, expected", [
         (1, 193100, 75, True),
         (1, 193100, 100, False),
         (1, 193125, 75, False),
         (1, 193100, 25, False),
         (1, 191295, 75, False),
         (1, 196105, 75, False)
    ])
    def test_CmisManagerTask_validate_frequency_and_grid(self, lport, freq, grid, expected):
        mock_xcvr_api = MagicMock()
        mock_xcvr_api.get_supported_freq_config = MagicMock()
        mock_xcvr_api.get_supported_freq_config.return_value = (0x80, 0, 0, 191300, 196100)
        port_mapping = PortMapping()
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event)
        result = task.validate_frequency_and_grid(mock_xcvr_api, lport, freq, grid)
        assert result == expected


def wait_until(total_wait_time, interval, call_back, *args, **kwargs):
    wait_time = 0
    while wait_time <= total_wait_time:
        try:
            if call_back(*args, **kwargs):
                return True
        except:
            pass
        time.sleep(interval)
        wait_time += interval
    return False
