#from unittest.mock import DEFAULT
from xcvrd.xcvrd_utilities.port_event_helper import *
from xcvrd.xcvrd_utilities.sfp_status_helper import *
from xcvrd.xcvrd_utilities.media_settings_parser import *
from xcvrd.xcvrd_utilities.optics_si_parser import *
from xcvrd.xcvrd_utilities import common
from xcvrd.dom.dom_mgr import *
from xcvrd.xcvrd import *
from xcvrd.cmis import CmisManagerTask
from xcvrd.xcvrd_utilities.common import (
    CMIS_STATE_UNKNOWN, CMIS_STATE_INSERTED, CMIS_STATE_DP_PRE_INIT_CHECK,
    CMIS_STATE_DP_DEINIT, CMIS_STATE_AP_CONF, CMIS_STATE_DP_ACTIVATE,
    CMIS_STATE_DP_INIT, CMIS_STATE_DP_TXON, CMIS_STATE_READY,
    CMIS_STATE_REMOVED, CMIS_STATE_FAILED, is_syncd_warm_restore_complete
)
from xcvrd.sff_mgr import *
from xcvrd.xcvrd_utilities.xcvr_table_helper import *
from xcvrd.dom.utilities.db.utils import DBUtils
from xcvrd.dom.utilities.dom_sensor.utils import DOMUtils
from xcvrd.dom.utilities.status.utils import StatusUtils
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

media_settings_with_regular_expression_dict = copy.deepcopy(media_settings_dict)
media_settings_with_regular_expression_dict['GLOBAL_MEDIA_SETTINGS']['1-32'] = {}
# Generate regular expression patterns for QSFP28-40GBASE-CR4-xxM and QSFP+-40GBASE-CR4-xxM that have the same pre-emphasis value
media_settings_with_regular_expression_dict['GLOBAL_MEDIA_SETTINGS']['1-32']['QSFP(\\+|28)-40GBASE-CR4-1M'] = global_media_settings['QSFP28-40GBASE-CR4-1M']
media_settings_with_regular_expression_dict['GLOBAL_MEDIA_SETTINGS']['1-32']['QSFP(\\+|28)-40GBASE-CR4-2M'] = global_media_settings['QSFP28-40GBASE-CR4-2M']
media_settings_with_regular_expression_dict['GLOBAL_MEDIA_SETTINGS']['1-32']['QSFP(\\+|28)-40GBASE-CR4-(3|4|5|7|10)M'] = global_media_settings['QSFP28-40GBASE-CR4-3M']

with open(os.path.join(test_path, 'optics_si_settings.json'), 'r') as fn:
    optics_si_settings_dict = json.load(fn)
port_optics_si_settings = {}
optics_si_settings_with_comma_dict = copy.deepcopy(optics_si_settings_dict)
global_optics_si_settings = optics_si_settings_with_comma_dict['GLOBAL_MEDIA_SETTINGS'].pop('0-31')
port_optics_si_settings['PORT_MEDIA_SETTINGS'] = optics_si_settings_with_comma_dict.pop('PORT_MEDIA_SETTINGS')
optics_si_settings_with_comma_dict['GLOBAL_MEDIA_SETTINGS']['0-5,6,7-20,21-31'] = global_optics_si_settings

with open(os.path.join(test_path, 'media_settings_extended_format.json'), 'r') as f:
    media_settings_extended_format_dict = json.load(f)


# Define some example keys/values of media_settings.json for testing purposes
asic_serdes_si_value_dict = {'lane' + str(i): '0x0000000d' for i in range(4)}
asic_serdes_si_value_dict2 = {'lane' + str(i): '0x0000000a' for i in range(4)}
asic_serdes_si_value_dict3 = {'lane' + str(i): '0x0000000b' for i in range(8)}
asic_serdes_si_value_dict4 = {'lane' + str(i): '0x00000003' for i in range(8)}
asic_serdes_si_value_dict5 = {'lane' + str(i): '0x00000004' for i in range(8)}
asic_serdes_si_settings_example = {
    'idriver': asic_serdes_si_value_dict,
    'pre1': asic_serdes_si_value_dict,
    'ob_m2lp': asic_serdes_si_value_dict,
}
asic_serdes_si_settings_example2 = {'idriver': asic_serdes_si_value_dict2}
asic_serdes_si_settings_example3 = {'main': asic_serdes_si_value_dict3}
asic_serdes_si_settings_example4 = {'main': asic_serdes_si_value_dict4}
asic_serdes_si_settings_example5 = {'idriver': asic_serdes_si_value_dict5}
asic_serdes_si_settings_example3_expected_value_in_db = \
    {attr: ','.join(value_dict.values()) for attr, value_dict in asic_serdes_si_settings_example3.items()}
asic_serdes_si_settings_example3_expected_value_in_db_4_lanes = \
    {attr: ','.join(list(value_dict.values())[:4]) for attr, value_dict in asic_serdes_si_settings_example3.items()}
asic_serdes_si_settings_example4_expected_value_in_db = \
    {attr: ','.join(list(value_dict.values())) for attr, value_dict in asic_serdes_si_settings_example4.items()}
asic_serdes_si_settings_example4_expected_value_in_db_4_lanes = \
    {attr: ','.join(list(value_dict.values())[:4]) for attr, value_dict in asic_serdes_si_settings_example4.items()}
asic_serdes_si_settings_example5_expected_value_in_db = \
    {attr: ','.join(value_dict.values()) for attr, value_dict in asic_serdes_si_settings_example5.items()}

# Creating instances of media_settings.json for testing purposes
# Each instance represents a different possible structure for media_settings.json.
media_settings_global_range_media_key_lane_speed_si = copy.deepcopy(media_settings_extended_format_dict)

media_settings_global_medium_lane_key = copy.deepcopy(media_settings_extended_format_dict)
media_settings_global_medium_lane_key['GLOBAL_MEDIA_SETTINGS']['0-31']['COPPER50'] = {'idriver': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}, 'pre1': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}, 'ob_m2lp': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}}

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
media_settings_global_list_of_ranges_media_key_lane_speed_si_with_default_section['GLOBAL_MEDIA_SETTINGS']['0-31']['Default'] = asic_serdes_si_settings_example

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

media_settings_port_medium_lane_key = copy.deepcopy(media_settings_port_vendor_key_lane_speed_si)
media_settings_port_medium_lane_key['PORT_MEDIA_SETTINGS']['7']['COPPER25'] = {'idriver': {'lane0': '0x0000000f', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}, 'pre1': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}, 'ob_m2lp': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}}

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
media_settings_global_default_port_media_key_lane_speed_si['GLOBAL_MEDIA_SETTINGS'] = {'0-31': {'Default': asic_serdes_si_settings_example}}
media_settings_global_default_port_media_key_lane_speed_si['PORT_MEDIA_SETTINGS'] = port_media_settings_data

media_settings_port_default_media_key_lane_speed_si = copy.deepcopy(media_settings_port_media_key_lane_speed_si)
media_settings_port_default_media_key_lane_speed_si['PORT_MEDIA_SETTINGS']['7']['Default'] = {
    LANE_SPEED_DEFAULT_KEY: asic_serdes_si_settings_example,
    'speed:400GAUI-8': asic_serdes_si_settings_example2,
}

media_settings_optic_copper_si = {
    'GLOBAL_MEDIA_SETTINGS': {
        '0-31': {
            '(SFP|QSFP(\\+|28|-DD)*)-(?!.*((40|100)GBASE-CR|100G ACC|Active Copper Cable|passive_copper_media_interface)).*': {
                'speed:400GAUI-8': asic_serdes_si_settings_example4,
                'speed:200GAUI-8|100GAUI-4|50GAUI-2|25G': asic_serdes_si_settings_example3,
            },
            '(SFP|QSFP(\\+|28|-DD)*)-((40|100)GBASE-CR|100G ACC|Active Copper Cable|passive_copper_media_interface).*': {
                'speed:400GAUI-8|200GAUI-4|100GAUI-2': asic_serdes_si_settings_example3,
                'speed:25G': asic_serdes_si_settings_example4,
                LANE_SPEED_DEFAULT_KEY: asic_serdes_si_settings_example5,
            },
        },
        '32-63': {
            'INNOLIGHT': asic_serdes_si_settings_example5,
            'Default': {
                'speed:400GAUI-8': asic_serdes_si_settings_example3,
                LANE_SPEED_DEFAULT_KEY: asic_serdes_si_settings_example4,
            }
        },
    }
}

media_settings_empty = {}

def gen_cmis_lanes_dict(key_format_str, value, one_based=True):
    start_idx = 1 if one_based else 0
    lanes_dict = {}
    for lane_idx in range(start_idx, start_idx + CmisManagerTask.CMIS_MAX_HOST_LANES):
        lanes_dict[key_format_str.format(lane_idx)] = value
    return lanes_dict

def gen_cmis_dp_state_dict(value):
    return gen_cmis_lanes_dict('DP{}State', value)

def gen_cmis_config_status_dict(value):
    return gen_cmis_lanes_dict('ConfigStatusLane{}', value)

def gen_cmis_dpinit_pending_dict(value):
    return gen_cmis_lanes_dict('DPInitPending{}', value)

def gen_cmis_active_app_sel_dict(value):
    return gen_cmis_lanes_dict('ActiveAppSelLane{}', value)

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
        cmis_manager = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=MagicMock())
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
        assert("sonic-xcvrd/xcvrd/cmis/cmis_manager_task.py" in str(trace))
        assert("wait_for_port_config_done" in str(trace))

        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_ADD)
        port_mapping.handle_port_change_event(port_change_event)
        cmis_manager = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=MagicMock())
        cmis_manager.wait_for_port_config_done = MagicMock() #no-op
        cmis_manager.update_port_transceiver_status_table_sw_cmis_state = MagicMock(side_effect = NotImplementedError)
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
        assert("sonic-xcvrd/xcvrd/cmis/cmis_manager_task.py" in str(trace))
        assert("update_port_transceiver_status_table_sw_cmis_state" in str(trace))

    @patch('xcvrd.cmis.cmis_manager_task.PortChangeObserver', MagicMock(handle_port_update_event=MagicMock()))
    @patch('xcvrd.cmis.CmisManagerTask.wait_for_port_config_done', MagicMock())
    @patch('xcvrd.xcvrd_utilities.common.is_fast_reboot_enabled', MagicMock(return_value=False))
    @patch('xcvrd.xcvrd_utilities.common.get_cmis_application_desired', MagicMock(side_effect=KeyError))
    @patch('xcvrd.xcvrd_utilities.common.log_exception_traceback')
    @patch('xcvrd.xcvrd.XcvrTableHelper.get_status_sw_tbl')
    @patch('xcvrd.xcvrd.XcvrTableHelper.get_state_port_tbl')
    @patch('xcvrd.xcvrd.platform_chassis')
    def test_CmisManagerTask_get_xcvr_api_exception(self, mock_platform_chassis, mock_get_state_port_tbl, mock_get_status_sw_tbl, mock_log_exception_traceback):
        mock_get_status_sw_tbl = Table("STATE_DB", TRANSCEIVER_STATUS_SW_TABLE)
        mock_get_state_port_tbl.return_value = Table("APPL_DB", 'PORT_TABLE')
        mock_sfp = MagicMock()
        mock_sfp.get_presence.return_value = True
        mock_platform_chassis.get_sfp = MagicMock(return_value=mock_sfp)
        port_mapping = PortMapping()
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=mock_platform_chassis)
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.get_cfg_port_tbl = MagicMock()
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.xcvr_table_helper.get_status_sw_tbl.return_value = mock_get_status_sw_tbl
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_SET,
                                            {'speed':'400000', 'lanes':'1,2,3,4,5,6,7,8', 
                                             'admin_status':'up', 'host_tx_status':'true'})

        # Case 1: get_xcvr_api() raises an exception
        task.on_port_update_event(port_change_event)
        mock_sfp.get_xcvr_api = MagicMock(side_effect=NotImplementedError)
        task.task_worker()
        assert mock_log_exception_traceback.call_count == 1
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_FAILED

        # Case 2: is_flat_memory() raises AttributeError. In this case, CMIS SM should transition to READY state
        mock_xcvr_api = MagicMock()
        mock_sfp.get_xcvr_api = MagicMock(return_value=mock_xcvr_api)
        mock_xcvr_api.is_flat_memory = MagicMock(side_effect=AttributeError)
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.on_port_update_event(port_change_event)
        task.task_worker()
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_READY

        # Case 2.5: get_module_type_abbreviation() returns unsupported module type. In this case, CMIS SM should transition to READY state
        mock_xcvr_api.is_flat_memory = MagicMock(return_value=False)
        mock_xcvr_api.get_module_type_abbreviation = MagicMock(return_value='SFP')
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.on_port_update_event(port_change_event)
        task.task_worker()
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_READY

        # Case 3: get_cmis_application_desired() raises an exception
        mock_xcvr_api.is_flat_memory = MagicMock(return_value=False)
        mock_xcvr_api.is_coherent_module = MagicMock(return_value=False)
        mock_xcvr_api.get_module_type_abbreviation = MagicMock(return_value='QSFP-DD')
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.on_port_update_event(port_change_event)
        task.get_cmis_host_lanes_mask = MagicMock()
        task.task_worker()
        assert mock_log_exception_traceback.call_count == 2
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_FAILED
        assert task.get_cmis_host_lanes_mask.call_count == 0

    @patch('xcvrd.xcvrd_utilities.port_event_helper.subscribe_port_config_change', MagicMock(side_effect = NotImplementedError))
    def test_DomInfoUpdateTask_task_run_with_exception(self):
        port_mapping = PortMapping()
        mock_sfp_obj_dict = MagicMock()
        stop_event = threading.Event()
        mock_cmis_manager = MagicMock()
        dom_info_update = DomInfoUpdateTask(DEFAULT_NAMESPACE, port_mapping, mock_sfp_obj_dict, stop_event, mock_cmis_manager)
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
        assert("sonic-xcvrd/xcvrd/dom/dom_mgr.py" in str(trace))
        assert("subscribe_port_config_change" in str(trace))

    @patch('xcvrd.xcvrd.SfpStateUpdateTask.init', MagicMock())
    @patch('xcvrd.xcvrd_utilities.port_event_helper.subscribe_port_config_change', MagicMock(side_effect = NotImplementedError))
    def test_SfpStateUpdateTask_task_run_with_exception(self):
        port_mapping = PortMapping()
        mock_sfp_obj_dict = MagicMock()
        stop_event = threading.Event()
        sfp_error_event = threading.Event()
        sfp_state_update = SfpStateUpdateTask(DEFAULT_NAMESPACE, port_mapping, mock_sfp_obj_dict, stop_event, sfp_error_event)
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
    @patch('xcvrd.xcvrd.DomThermalInfoUpdateTask.is_alive', MagicMock(return_value = False))
    @patch('xcvrd.cmis.CmisManagerTask.is_alive', MagicMock(return_value = False))
    @patch('xcvrd.xcvrd.SffManagerTask.is_alive', MagicMock(return_value=False))
    @patch('xcvrd.cmis.CmisManagerTask.join', MagicMock(side_effect=NotImplementedError))
    @patch('xcvrd.cmis.CmisManagerTask.start', MagicMock())
    @patch('xcvrd.xcvrd.SffManagerTask.start', MagicMock())
    @patch('xcvrd.xcvrd.DomInfoUpdateTask.start', MagicMock())
    @patch('xcvrd.xcvrd.DomThermalInfoUpdateTask.start', MagicMock())
    @patch('xcvrd.xcvrd.SfpStateUpdateTask.start', MagicMock())
    @patch('xcvrd.xcvrd.DaemonXcvrd.deinit', MagicMock())
    @patch('os.kill')
    @patch('xcvrd.xcvrd.DaemonXcvrd.init')
    @patch('xcvrd.xcvrd.DomInfoUpdateTask.join')
    @patch('xcvrd.xcvrd.DomThermalInfoUpdateTask.join')
    @patch('xcvrd.xcvrd.SfpStateUpdateTask.join')
    @patch('xcvrd.xcvrd.SffManagerTask.join')
    def test_DaemonXcvrd_run_with_exception(self, mock_task_join_sff, mock_task_join_sfp,
                                            mock_task_join_dom, mock_task_join_dom_thermal,
                                            mock_init, mock_os_kill):
        mock_init.return_value = PortMapping()
        xcvrd = DaemonXcvrd(SYSLOG_IDENTIFIER)
        xcvrd.enable_sff_mgr = True
        xcvrd.dom_temperature_poll_interval = 10
        xcvrd.load_feature_flags = MagicMock()
        xcvrd.stop_event.wait = MagicMock()
        xcvrd.run()

        assert len(xcvrd.threads) == 5
        assert mock_init.call_count == 1
        assert mock_task_join_sff.call_count == 1
        assert mock_task_join_sfp.call_count == 1
        assert mock_task_join_dom.call_count == 1
        assert mock_task_join_dom_thermal.call_count == 1
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
        assert common.is_cmis_api(mock_xcvr_api) == expected_return_value

    def test_get_state_db_port_table_val_by_key(self):
        xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        port_mapping = PortMapping()

        assert xcvr_table_helper.get_state_db_port_table_val_by_key("Ethernet0", None, NPU_SI_SETTINGS_SYNC_STATUS_KEY) == None

        port_mapping.get_asic_id_for_logical_port = MagicMock(return_value=0)
        xcvr_table_helper.get_state_port_tbl = MagicMock(return_value=None)
        assert xcvr_table_helper.get_state_db_port_table_val_by_key("Ethernet0", port_mapping, NPU_SI_SETTINGS_SYNC_STATUS_KEY) == None

        mock_state_port_table = MagicMock()
        xcvr_table_helper.get_state_port_tbl = MagicMock(return_value=mock_state_port_table)
        mock_state_port_table.get = MagicMock(return_value=(None, None))
        assert xcvr_table_helper.get_state_db_port_table_val_by_key("Ethernet0", port_mapping, NPU_SI_SETTINGS_SYNC_STATUS_KEY) == None

        mock_state_port_table.get = MagicMock(return_value=(True, {'A' : 'B'}))
        assert xcvr_table_helper.get_state_db_port_table_val_by_key("Ethernet0", port_mapping, NPU_SI_SETTINGS_SYNC_STATUS_KEY) == None
        mock_state_port_table.get = MagicMock(return_value=(True, {NPU_SI_SETTINGS_SYNC_STATUS_KEY : NPU_SI_SETTINGS_DEFAULT_VALUE}))
        assert xcvr_table_helper.get_state_db_port_table_val_by_key("Ethernet0", port_mapping, NPU_SI_SETTINGS_SYNC_STATUS_KEY) == NPU_SI_SETTINGS_DEFAULT_VALUE

    def test_is_npu_si_settings_update_required(self):
        xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        port_mapping = PortMapping()
        xcvr_table_helper.get_state_db_port_table_val_by_key = MagicMock(side_effect=[None, NPU_SI_SETTINGS_NOTIFIED_VALUE])
        assert xcvr_table_helper.is_npu_si_settings_update_required("Ethernet0", port_mapping)
        assert not xcvr_table_helper.is_npu_si_settings_update_required("Ethernet0", port_mapping)

    @patch('xcvrd.xcvrd_utilities.port_event_helper.PortMapping.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd_utilities.port_event_helper.PortMapping.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_transceiver_firmware_info', MagicMock(return_value={'active_firmware': '2.1.1',
                                                                              'inactive_firmware': '1.2.4'}))
    @patch('xcvrd.xcvrd_utilities.common._wrapper_is_flat_memory', MagicMock(return_value=False))
    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence')
    def test_post_port_sfp_firmware_info_to_db(self, mock_get_presence):
        logical_port_name = "Ethernet0"
        port_mapping = PortMapping()
        port_mapping.get_physical_to_logical = MagicMock(return_value=["Ethernet0", "Ethernet4"])
        mock_sfp_obj_dict = MagicMock()
        stop_event = threading.Event()
        mock_cmis_manager = MagicMock()
        dom_info_update = DomInfoUpdateTask(DEFAULT_NAMESPACE, port_mapping, mock_sfp_obj_dict, stop_event, mock_cmis_manager)
        firmware_info_tbl = Table("STATE_DB", TRANSCEIVER_FIRMWARE_INFO_TABLE)

        # Test 1: stop_event is set - should not update table
        stop_event.set()
        dom_info_update.post_port_sfp_firmware_info_to_db(logical_port_name, port_mapping, firmware_info_tbl, stop_event)
        assert firmware_info_tbl.get_size() == 0

        # Test 2: transceiver not present - should not update table
        stop_event.clear()
        mock_get_presence.return_value = False
        dom_info_update.post_port_sfp_firmware_info_to_db(logical_port_name, port_mapping, firmware_info_tbl, stop_event)
        assert firmware_info_tbl.get_size() == 0

        # Test 3: transceiver present - should update table for both logical ports
        mock_get_presence.return_value = True
        dom_info_update.post_port_sfp_firmware_info_to_db(logical_port_name, port_mapping, firmware_info_tbl, stop_event)
        # Verify firmware info is posted for Ethernet0 (2 entries: active + inactive firmware)
        assert firmware_info_tbl.get_size_for_key(logical_port_name) == 2
        # Verify firmware info is also posted for Ethernet4 (2 entries: active + inactive firmware)
        assert firmware_info_tbl.get_size_for_key("Ethernet4") == 2
        # Verify total table has 2 logical ports (keys)
        assert firmware_info_tbl.get_size() == 2

    @patch('xcvrd.xcvrd_utilities.port_event_helper.PortMapping.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_transceiver_firmware_info', MagicMock(return_value={'active_firmware': '2.1.1',
                                                                              'inactive_firmware': '1.2.4'}))
    @patch('xcvrd.xcvrd_utilities.common._wrapper_is_flat_memory', MagicMock(return_value=False))
    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence')
    def test_post_port_sfp_firmware_info_to_db_lport_list_None(self, mock_get_presence):
        logical_port_name = "Ethernet0"
        port_mapping = PortMapping()
        port_mapping.get_physical_to_logical = MagicMock(return_value=None)
        port_mapping.logical_port_name_to_physical_port_list = MagicMock(return_value=[0])
        mock_sfp_obj_dict = MagicMock()
        stop_event = threading.Event()
        mock_cmis_manager = MagicMock()
        dom_info_update = DomInfoUpdateTask(DEFAULT_NAMESPACE, port_mapping, mock_sfp_obj_dict, stop_event, mock_cmis_manager)
        firmware_info_tbl = MagicMock()
        firmware_info_tbl.get_size.return_value = 0
        stop_event.set()
        dom_info_update.post_port_sfp_firmware_info_to_db(logical_port_name, port_mapping, firmware_info_tbl, stop_event)
        assert firmware_info_tbl.get_size() == 0
        stop_event.clear()
        mock_get_presence.return_value = False
        dom_info_update.post_port_sfp_firmware_info_to_db(logical_port_name, port_mapping, firmware_info_tbl, stop_event)
        assert firmware_info_tbl.get_size() == 0
        mock_get_presence.return_value = True
        dom_info_update.post_port_sfp_firmware_info_to_db(logical_port_name, port_mapping, firmware_info_tbl, stop_event)
        assert firmware_info_tbl.set.call_count == 0

    @pytest.mark.parametrize(
        "restore_count, system_enabled, expected",
        [
            (1, None, True),
            (0, None, False),
            ("2", None, True),
            ("0", None, False),
            (None, "true", True),
            (None, "false", False),
            (None, None, False),
        ]
    )
    def test_is_syncd_warm_restore_complete_valid_cases(self, restore_count, system_enabled, expected):
        mock_db = MagicMock()
        mock_db.hget.side_effect = lambda table, key: (
            restore_count if "WARM_RESTART_TABLE|syncd" in table else system_enabled
        )

        with patch("xcvrd.xcvrd_utilities.common.daemon_base.db_connect", return_value=mock_db):
            assert is_syncd_warm_restore_complete() == expected

    def test_is_syncd_warm_restore_complete_invalid_restore_count(self):
        # restore_count = "abc" triggers ValueError in int("abc")
        mock_db = MagicMock()
        mock_db.hget.side_effect = lambda table, key: (
            "abc" if "WARM_RESTART_TABLE|syncd" in table else None
        )

        with patch("xcvrd.xcvrd_utilities.common.daemon_base.db_connect", return_value=mock_db):
            result = is_syncd_warm_restore_complete()
            assert result is False

    @pytest.mark.parametrize(
        "namespace, restore_count, expected",
        [
            ('', 1, True),              # Default namespace
            ('asic0', 1, True),         # Multi-ASIC namespace asic0
            ('asic1', 1, True),         # Multi-ASIC namespace asic1
            ('asic0', 0, False),        # No warm restore for asic0
            ('asic1', 0, False),        # No warm restore for asic1
        ]
    )
    def test_is_syncd_warm_restore_complete_with_namespace(self, namespace, restore_count, expected):
        """Test is_syncd_warm_restore_complete with different namespaces for multi-ASIC support"""
        mock_db = MagicMock()
        mock_db.hget.side_effect = lambda table, key: (
            restore_count if "WARM_RESTART_TABLE|syncd" in table else None
        )

        with patch("xcvrd.xcvrd_utilities.common.daemon_base.db_connect", return_value=mock_db) as mock_connect:
            result = is_syncd_warm_restore_complete(namespace)
            assert result == expected
            # Verify db_connect was called with the correct namespace
            mock_connect.assert_called_with("STATE_DB", namespace=namespace)

    def test_post_port_dom_sensor_info_to_db(self):
        def mock_get_transceiver_dom_sensor_real_value(physical_port):
            return {
                'temperature': '22.75',
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
                'tx8power': '0.7',
            }

        logical_port_name = "Ethernet0"
        port_mapping = PortMapping()
        port_mapping.get_logical_to_physical = MagicMock(return_value=[0])
        xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        stop_event = threading.Event()
        mock_sfp_obj_dict = {0 : MagicMock()}

        dom_db_utils = DOMDBUtils(mock_sfp_obj_dict, port_mapping, xcvr_table_helper, stop_event, helper_logger)
        dom_db_utils.dom_utils = MagicMock()
        dom_db_utils.xcvrd_utils.get_transceiver_presence = MagicMock(return_value=False)
        dom_db_utils.xcvrd_utils.is_transceiver_flat_memory = MagicMock(return_value=False)
        dom_tbl = Table("STATE_DB", TRANSCEIVER_DOM_SENSOR_TABLE)
        dom_db_utils.xcvr_table_helper.get_dom_tbl = MagicMock(return_value=dom_tbl)
        dom_db_utils.dom_utils.get_transceiver_dom_sensor_real_value = MagicMock(return_value=None)
        assert dom_tbl.get_size() == 0

        # Ensure table is empty asic_index is None
        port_mapping.get_asic_id_for_logical_port = MagicMock(return_value=None)
        dom_db_utils.post_port_dom_sensor_info_to_db(logical_port_name)
        assert dom_tbl.get_size() == 0

        # Set asic_index to 0
        port_mapping.get_asic_id_for_logical_port = MagicMock(return_value=0)

        # Ensure table is empty if stop_event is set
        stop_event.set()
        dom_db_utils.post_port_dom_sensor_info_to_db(logical_port_name)
        assert dom_tbl.get_size() == 0
        stop_event.clear()

        # Ensure table is empty if transceiver is not present
        dom_db_utils.post_port_dom_sensor_info_to_db(logical_port_name)
        assert dom_tbl.get_size() == 0
        dom_db_utils.return_value = True

        # Ensure table is empty if get_values_func returns None
        dom_db_utils.xcvrd_utils.get_transceiver_presence = MagicMock(return_value=True)
        dom_db_utils.post_port_dom_sensor_info_to_db(logical_port_name)
        assert dom_tbl.get_size() == 0

        # Ensure table is populated if get_values_func returns valid values
        db_cache = {}
        dom_db_utils.dom_utils.get_transceiver_dom_sensor_real_value = MagicMock(side_effect=mock_get_transceiver_dom_sensor_real_value)
        dom_db_utils.post_port_dom_sensor_info_to_db(logical_port_name, db_cache=db_cache)
        assert dom_tbl.get_size_for_key(logical_port_name) == 27

        # Ensure db_cache is populated correctly
        assert db_cache.get(0) is not None
        dom_db_utils.dom_utils.get_transceiver_dom_sensor_real_value = MagicMock(return_value=None)
        dom_db_utils.post_port_dom_sensor_info_to_db(logical_port_name, db_cache=db_cache)
        assert dom_tbl.get_size_for_key(logical_port_name) == 27

    def test_post_port_dom_temperature_info_to_db(self):
        def mock_get_transceiver_dom_temperature(physical_port):
            return {
                'temperature': '68.75',
            }

        logical_port_name = "Ethernet0"
        port_mapping = PortMapping()
        port_mapping.get_logical_to_physical = MagicMock(return_value=[0])
        xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        stop_event = threading.Event()
        mock_sfp_obj_dict = {0 : MagicMock()}

        dom_db_utils = DOMDBUtils(mock_sfp_obj_dict, port_mapping, xcvr_table_helper, stop_event, helper_logger)
        dom_db_utils.dom_utils = MagicMock()
        dom_db_utils.xcvrd_utils.get_transceiver_presence = MagicMock(return_value=False)
        dom_db_utils.xcvrd_utils.is_transceiver_flat_memory = MagicMock(return_value=False)
        dom_tbl = Table("STATE_DB", TRANSCEIVER_DOM_TEMPERATURE_TABLE)
        dom_db_utils.xcvr_table_helper.get_dom_temperature_tbl = MagicMock(return_value=dom_tbl)
        dom_db_utils.dom_utils.get_transceiver_dom_temperature = MagicMock(return_value=None)
        assert dom_tbl.get_size() == 0

        # Ensure table is empty asic_index is None
        port_mapping.get_asic_id_for_logical_port = MagicMock(return_value=None)
        dom_db_utils.post_port_dom_temperature_info_to_db(logical_port_name)
        assert dom_tbl.get_size() == 0

        # Set asic_index to 0
        port_mapping.get_asic_id_for_logical_port = MagicMock(return_value=0)

        # Ensure table is empty if stop_event is set
        stop_event.set()
        dom_db_utils.post_port_dom_temperature_info_to_db(logical_port_name)
        assert dom_tbl.get_size() == 0
        stop_event.clear()

        # Ensure table is empty if transceiver is not present
        dom_db_utils.post_port_dom_temperature_info_to_db(logical_port_name)
        assert dom_tbl.get_size() == 0
        dom_db_utils.return_value = True

        # Ensure table is empty if get_values_func returns None
        dom_db_utils.xcvrd_utils.get_transceiver_presence = MagicMock(return_value=True)
        dom_db_utils.post_port_dom_temperature_info_to_db(logical_port_name)
        assert dom_tbl.get_size() == 0

        # Ensure table is populated if get_values_func returns valid values
        db_cache = {}
        dom_db_utils.dom_utils.get_transceiver_dom_temperature = MagicMock(side_effect=mock_get_transceiver_dom_temperature)
        dom_db_utils.post_port_dom_temperature_info_to_db(logical_port_name, db_cache=db_cache)
        assert dom_tbl.get_size_for_key(logical_port_name) == 2

        # Ensure db_cache is populated correctly
        assert db_cache.get(0) is not None
        dom_db_utils.dom_utils.get_transceiver_dom_temperature = MagicMock(return_value=None)
        dom_db_utils.post_port_dom_temperature_info_to_db(logical_port_name, db_cache=db_cache)
        assert dom_tbl.get_size_for_key(logical_port_name) == 2

    def test_post_port_dom_flags_to_db(self):
        def mock_get_transceiver_dom_flags(physical_port):
            return {
                "temphighalarm": "False",
                "templowalarm": "False",
                "temphighwarning": "False",
                "templowwarning": "False",
                "vcchighalarm": "False",
                "vcclowalarm": "False",
                "vcchighwarning": "False",
                "vcclowwarning": "False",
                "lasertemphighalarm": "False",
                "lasertemplowalarm": "False",
                "lasertemphighwarning": "False",
                "lasertemplowwarning": "False",
                "tx1powerHAlarm": "False",
                "tx1powerLAlarm": "False",
                "tx1powerHWarning": "False",
                "tx1powerLWarning": "False",
                "tx2powerHAlarm": "False",
                "tx2powerLAlarm": "False",
                "tx2powerHWarning": "False",
                "tx2powerLWarning": "False"
            }

        logical_port_name = "Ethernet0"
        port_mapping = PortMapping()
        port_mapping.get_logical_to_physical = MagicMock(return_value=[0])
        xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        stop_event = threading.Event()
        mock_sfp_obj_dict = {0 : MagicMock()}

        dom_db_utils = DOMDBUtils(mock_sfp_obj_dict, port_mapping, xcvr_table_helper, stop_event, helper_logger)
        dom_db_utils.dom_utils = MagicMock()
        dom_db_utils.xcvrd_utils.get_transceiver_presence = MagicMock(return_value=False)
        dom_db_utils.xcvrd_utils.is_transceiver_flat_memory = MagicMock(return_value=False)
        dom_tbl = Table("STATE_DB", TRANSCEIVER_DOM_FLAG_TABLE)
        dom_db_utils.xcvr_table_helper.get_dom_flag_tbl = MagicMock(return_value=dom_tbl)
        dom_db_utils.dom_utils.get_transceiver_dom_flags = MagicMock(return_value=None)
        dom_db_utils._update_flag_metadata_tables = MagicMock()
        assert dom_tbl.get_size() == 0

        # Ensure table is empty asic_index is None
        port_mapping.get_asic_id_for_logical_port = MagicMock(return_value=None)
        dom_db_utils.post_port_dom_flags_to_db(logical_port_name)
        assert dom_tbl.get_size() == 0

        # Set asic_index to 0
        port_mapping.get_asic_id_for_logical_port = MagicMock(return_value=0)

        # Ensure table is empty if stop_event is set
        stop_event.set()
        dom_db_utils.post_port_dom_flags_to_db(logical_port_name)
        assert dom_tbl.get_size() == 0
        stop_event.clear()

        # Ensure table is empty if transceiver is not present
        dom_db_utils.post_port_dom_flags_to_db(logical_port_name)
        assert dom_tbl.get_size() == 0
        dom_db_utils.return_value = True

        # Ensure table is empty if get_values_func returns None
        dom_db_utils.xcvrd_utils.get_transceiver_presence = MagicMock(return_value=True)
        dom_db_utils.post_port_dom_flags_to_db(logical_port_name)
        assert dom_tbl.get_size() == 0

        # Ensure table is populated if get_values_func returns valid values
        db_cache = {}
        dom_db_utils.dom_utils.get_transceiver_dom_flags = MagicMock(side_effect=mock_get_transceiver_dom_flags)
        dom_db_utils.post_port_dom_flags_to_db(logical_port_name, db_cache=db_cache)
        assert dom_tbl.get_size_for_key(logical_port_name) == 21
        assert dom_db_utils._update_flag_metadata_tables.call_count == 1

        # Reset the mock to clear the call count
        dom_db_utils._update_flag_metadata_tables.reset_mock()

        # Ensure db_cache is populated correctly
        assert db_cache.get(0) is not None
        dom_db_utils.dom_utils.get_transceiver_dom_flags = MagicMock(return_value=None)
        dom_db_utils.post_port_dom_flags_to_db(logical_port_name, db_cache=db_cache)
        assert dom_tbl.get_size_for_key(logical_port_name) == 21
        assert dom_db_utils._update_flag_metadata_tables.call_count == 0

    @pytest.mark.parametrize("flag_value_table, flag_value_table_found, current_value, expected_change_count, expected_set_time, expected_clear_time", [
        (None, False, 'N/A', None, None, None),
        (MagicMock(), False, 'N/A', '0', 'never', 'never'),
        (MagicMock(), False, True, '0', 'never', 'never'),
        (MagicMock(), False, False, '0', 'never', 'never'),
        (MagicMock(), True, 'N/A', 0, 'never', 'never'),
        (MagicMock(), True, True, '2', 'Thu Jan 09 21:50:24 2025', None),
        (MagicMock(), True, False, '2', None, 'Thu Jan 09 21:50:24 2025')
    ])
    @patch('xcvrd.xcvrd.helper_logger')
    def test_update_flag_metadata_tables(self, mock_logger, flag_value_table, flag_value_table_found, current_value, expected_change_count, expected_set_time, expected_clear_time):
        def field_value_pairs_to_dict(fvp):
            return {k: v for k, v in fvp}

        logical_port_name = "Ethernet0"
        field_name = "test_field"
        flag_values_dict_update_time = "Thu Jan 09 21:50:24 2025"
        table_name_for_logging = "test_table"

        # Mock the tables
        flag_change_count_table = MagicMock()
        flag_last_set_time_table = MagicMock()
        flag_last_clear_time_table = MagicMock()

        if flag_value_table is not None:
            # Mock the return values for get
            flag_value_table.get.return_value = (flag_value_table_found, {field_name: '0'} if flag_value_table_found else {})
        flag_change_count_table.get.return_value = (True, {field_name: '1'})
        mock_curr_flag_dict = {field_name: current_value}

        mock_sfp_obj_dict = MagicMock()
        port_mapping = PortMapping()
        stop_event = threading.Event()
        db_utils = DBUtils(mock_sfp_obj_dict, port_mapping, stop_event, mock_logger)
        # Call the function
        db_utils._update_flag_metadata_tables(logical_port_name, mock_curr_flag_dict,
                                            flag_values_dict_update_time, flag_value_table,
                                            flag_change_count_table, flag_last_set_time_table,
                                            flag_last_clear_time_table, table_name_for_logging)

        if flag_value_table is None:
            mock_logger.log_error.assert_called_once_with(f"flag_value_table {table_name_for_logging} is None for port {logical_port_name}")
        elif not flag_value_table_found:
            flag_change_count_table.set.assert_called_once()
            flag_last_set_time_table.set.assert_called_once()
            flag_last_clear_time_table.set.assert_called_once()
            assert field_value_pairs_to_dict(flag_change_count_table.set.call_args[0][1]) == {field_name: '0'}
            assert field_value_pairs_to_dict(flag_last_set_time_table.set.call_args[0][1]) == {field_name: 'never'}
            assert field_value_pairs_to_dict(flag_last_clear_time_table.set.call_args[0][1]) == {field_name: 'never'}
        else:
            if current_value == 'N/A':
                flag_change_count_table.set.assert_not_called()
                flag_last_set_time_table.set.assert_not_called()
                flag_last_clear_time_table.set.assert_not_called()
            else:
                flag_change_count_table.set.assert_called_once()
                if current_value:
                    flag_last_set_time_table.set.assert_called_once()
                    assert field_value_pairs_to_dict(flag_change_count_table.set.call_args[0][1]) == {field_name: expected_change_count}
                    assert field_value_pairs_to_dict(flag_last_set_time_table.set.call_args[0][1]) == {field_name: expected_set_time}
                else:
                    flag_last_clear_time_table.set.assert_called_once()
                    assert field_value_pairs_to_dict(flag_change_count_table.set.call_args[0][1]) == {field_name: expected_change_count}
                    assert field_value_pairs_to_dict(flag_last_clear_time_table.set.call_args[0][1]) == {field_name: expected_clear_time}

    def test_post_port_dom_thresholds_to_db(self):
        def mock_get_transceiver_dom_thresholds(physical_port):
            return {
                "temphighalarm": "75.0",
                "templowalarm": "-5.0",
                "temphighwarning": "72.0",
                "templowwarning": "-2.0",
                "vcchighalarm": "3.63",
                "vcclowalarm": "2.97",
                "vcchighwarning": "3.465",
                "vcclowwarning": "3.135",
                "rxpowerhighalarm": "6.2",
                "rxpowerlowalarm": "-11.198",
                "rxpowerhighwarning": "4.2",
                "rxpowerlowwarning": "-9.201",
            }

        logical_port_name = "Ethernet0"
        port_mapping = PortMapping()
        port_mapping.get_logical_to_physical = MagicMock(return_value=[0])
        xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        stop_event = threading.Event()
        mock_sfp_obj_dict = {0 : MagicMock()}

        dom_db_utils = DOMDBUtils(mock_sfp_obj_dict, port_mapping, xcvr_table_helper, stop_event, helper_logger)
        dom_db_utils.dom_utils = MagicMock()
        dom_db_utils.xcvrd_utils.get_transceiver_presence = MagicMock(return_value=False)
        dom_db_utils.xcvrd_utils.is_transceiver_flat_memory = MagicMock(return_value=False)
        dom_threshold_tbl = Table("STATE_DB", TRANSCEIVER_DOM_THRESHOLD_TABLE)
        dom_db_utils.xcvr_table_helper.get_dom_threshold_tbl = MagicMock(return_value=dom_threshold_tbl)
        dom_db_utils.dom_utils.get_transceiver_dom_thresholds = MagicMock(return_value=None)
        assert dom_threshold_tbl.get_size() == 0

        # Ensure table is empty asic_index is None
        port_mapping.get_asic_id_for_logical_port = MagicMock(return_value=None)
        dom_db_utils.post_port_dom_thresholds_to_db(logical_port_name)
        assert dom_threshold_tbl.get_size() == 0

        # Set asic_index to 0
        port_mapping.get_asic_id_for_logical_port = MagicMock(return_value=0)

        # Ensure table is empty if stop_event is set
        stop_event.set()
        dom_db_utils.post_port_dom_thresholds_to_db(logical_port_name)
        assert dom_threshold_tbl.get_size() == 0
        stop_event.clear()

        # Ensure table is empty if transceiver is not present
        dom_db_utils.post_port_dom_thresholds_to_db(logical_port_name)
        assert dom_threshold_tbl.get_size() == 0
        dom_db_utils.return_value = True

        # Ensure table is empty if get_values_func returns None
        dom_db_utils.xcvrd_utils.get_transceiver_presence = MagicMock(return_value=True)
        dom_db_utils.post_port_dom_thresholds_to_db(logical_port_name)
        assert dom_threshold_tbl.get_size() == 0

        # Ensure table is populated if get_values_func returns valid values
        db_cache = {}
        dom_db_utils.dom_utils.get_transceiver_dom_thresholds = MagicMock(side_effect=mock_get_transceiver_dom_thresholds)
        dom_db_utils.post_port_dom_thresholds_to_db(logical_port_name, db_cache=db_cache)
        assert dom_threshold_tbl.get_size_for_key(logical_port_name) == 13

        # Ensure db_cache is populated correctly
        assert db_cache.get(0) is not None
        dom_db_utils.dom_utils.get_transceiver_dom_thresholds = MagicMock(return_value=None)
        dom_db_utils.post_port_dom_thresholds_to_db(logical_port_name, db_cache=db_cache)
        assert dom_threshold_tbl.get_size_for_key(logical_port_name) == 13

    def test_post_port_vdm_thresholds_to_db(self):
        def mock_get_vdm_threshold_values_func(physical_port):
            return {
                f'laser_temperature_media_{i}_halarm': 90.0 for i in range(1, 9)
            } | {
                f'laser_temperature_media_{i}_lalarm': -5.0 for i in range(1, 9)
            } | {
                f'laser_temperature_media_{i}_hwarn': 85.0 for i in range(1, 9)
            } | {
                f'laser_temperature_media_{i}_lwarn': 0.0 for i in range(1, 9)
            }

        VDM_THRESHOLD_TABLES = {f'vdm_{t}_threshold_tbl': {} for t in VDM_THRESHOLD_TYPES}
        for t in VDM_THRESHOLD_TYPES:
            VDM_THRESHOLD_TABLES[f'vdm_{t}_threshold_tbl'][0] = Table("STATE_DB", f'TRANSCEIVER_VDM_{t.upper()}_THRESHOLD')
        def mock_get_vdm_threshold_table_func(asic_id, threshold_type):
            return VDM_THRESHOLD_TABLES[f'vdm_{threshold_type}_threshold_tbl'][0]

        logical_port_name = "Ethernet0"
        port_mapping = PortMapping()
        port_mapping.get_logical_to_physical = MagicMock(return_value=[0])
        xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        stop_event = threading.Event()
        for t in VDM_THRESHOLD_TYPES:
            assert VDM_THRESHOLD_TABLES[f'vdm_{t}_threshold_tbl'][0].get_size() == 0

        # Ensure table is empty if stop_event is set
        stop_event.set()
        mock_sfp_obj_dict = {0 : MagicMock()}
        vdm_db_utils = VDMDBUtils(mock_sfp_obj_dict, port_mapping, xcvr_table_helper, stop_event, helper_logger)
        vdm_db_utils.vdm_utils = MagicMock()  # Ensure vdm_utils is a mock object
        vdm_db_utils.xcvr_table_helper.get_vdm_threshold_tbl = MagicMock(side_effect=mock_get_vdm_threshold_table_func)
        vdm_db_utils.xcvrd_utils.get_transceiver_presence = MagicMock(return_value=False)

        vdm_db_utils.post_port_vdm_thresholds_to_db(logical_port_name)
        for t in VDM_THRESHOLD_TYPES:
            assert VDM_THRESHOLD_TABLES[f'vdm_{t}_threshold_tbl'][0].get_size() == 0

        stop_event.clear()

        # Ensure table is empty if transceiver is not present
        vdm_db_utils.post_port_vdm_thresholds_to_db(logical_port_name)
        for t in VDM_THRESHOLD_TYPES:
            assert VDM_THRESHOLD_TABLES[f'vdm_{t}_threshold_tbl'][0].get_size() == 0

        vdm_db_utils.xcvrd_utils.get_transceiver_presence = MagicMock(return_value=True)

        # Ensure table is empty if transceiver is flat memory
        vdm_db_utils.xcvrd_utils.is_transceiver_flat_memory = MagicMock(return_value=True)
        vdm_db_utils.post_port_vdm_thresholds_to_db(logical_port_name)
        for t in VDM_THRESHOLD_TYPES:
            assert VDM_THRESHOLD_TABLES[f'vdm_{t}_threshold_tbl'][0].get_size() == 0
        vdm_db_utils.xcvrd_utils.is_transceiver_flat_memory = MagicMock(return_value=False)

        # Ensure table is empty if get_vdm_values_func returns None
        vdm_db_utils.vdm_utils.get_vdm_thresholds = MagicMock(return_value=None)
        vdm_db_utils.post_port_vdm_thresholds_to_db(logical_port_name)
        for t in VDM_THRESHOLD_TYPES:
            assert VDM_THRESHOLD_TABLES[f'vdm_{t}_threshold_tbl'][0].get_size() == 0

        # Ensure table is populated if get_vdm_values_func returns valid values
        db_cache = {}
        vdm_db_utils.vdm_utils.get_vdm_thresholds = MagicMock(side_effect=mock_get_vdm_threshold_values_func)
        vdm_db_utils.post_port_vdm_thresholds_to_db(logical_port_name, db_cache=db_cache)
        for t in VDM_THRESHOLD_TYPES:
           assert VDM_THRESHOLD_TABLES[f'vdm_{t}_threshold_tbl'][0].get_size_for_key(logical_port_name) == 9

        # Ensure db_cache is populated correctly
        assert db_cache.get(0) is not None
        vdm_db_utils.post_port_vdm_thresholds_to_db(logical_port_name, db_cache=db_cache)
        for t in VDM_THRESHOLD_TYPES:
            assert VDM_THRESHOLD_TABLES[f'vdm_{t}_threshold_tbl'][0].get_size_for_key(logical_port_name) == 9

    def test_post_port_vdm_real_values_to_db(self):
        def mock_get_transceiver_diagnostic_values(physical_port):
            return {
                f'laser_temperature_media{i}': 38 if i <= 4 else 'N/A' for i in range(1, 9)
            } | {
                f'esnr_media_input{i}': 23.1171875 for i in range(1, 9)
            }

        logical_port_name = "Ethernet0"
        port_mapping = PortMapping()
        port_mapping.get_logical_to_physical = MagicMock(return_value=[0])
        xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        stop_event = threading.Event()
        mock_sfp_obj_dict = {0 : MagicMock()}

        vdm_db_utils = VDMDBUtils(mock_sfp_obj_dict, port_mapping, xcvr_table_helper, stop_event, helper_logger)
        vdm_db_utils.vdm_utils = MagicMock()  # Ensure vdm_utils is a mock object
        vdm_db_utils.xcvrd_utils.get_transceiver_presence = MagicMock(return_value=False)
        vdm_db_utils.xcvrd_utils.is_transceiver_flat_memory = MagicMock(return_value=False)
        diagnostic_tbl = Table("STATE_DB", TRANSCEIVER_VDM_REAL_VALUE_TABLE)
        vdm_db_utils.xcvr_table_helper.get_vdm_real_value_tbl = MagicMock(return_value=diagnostic_tbl)
        vdm_db_utils.vdm_utils.get_vdm_real_values = MagicMock(return_value=None)
        assert diagnostic_tbl.get_size() == 0

        # Ensure table is empty asic_index is None
        port_mapping.get_asic_id_for_logical_port = MagicMock(return_value=None)
        vdm_db_utils.post_port_vdm_real_values_to_db(logical_port_name)
        assert diagnostic_tbl.get_size() == 0

        # Set asic_index to 0
        port_mapping.get_asic_id_for_logical_port = MagicMock(return_value=0)

        # Ensure table is empty if stop_event is set
        stop_event.set()
        vdm_db_utils.post_port_vdm_real_values_to_db(logical_port_name)
        assert diagnostic_tbl.get_size() == 0
        stop_event.clear()

        # Ensure table is empty if transceiver is not present
        vdm_db_utils.post_port_vdm_real_values_to_db(logical_port_name)
        assert diagnostic_tbl.get_size() == 0
        vdm_db_utils.return_value = True

        # Ensure table is empty if get_values_func returns None
        vdm_db_utils.xcvrd_utils.get_transceiver_presence = MagicMock(return_value=True)
        vdm_db_utils.post_port_vdm_real_values_to_db(logical_port_name)
        assert diagnostic_tbl.get_size() == 0

        # Ensure table is populated if get_values_func returns valid values
        db_cache = {}
        vdm_db_utils.vdm_utils.get_vdm_real_values = MagicMock(side_effect=mock_get_transceiver_diagnostic_values)
        vdm_db_utils.post_port_vdm_real_values_to_db(logical_port_name, db_cache=db_cache)
        assert diagnostic_tbl.get_size_for_key(logical_port_name) == 17

        # Ensure db_cache is populated correctly
        assert db_cache.get(0) is not None
        vdm_db_utils.vdm_utils.get_vdm_real_values = MagicMock(return_value=None)
        vdm_db_utils.post_port_vdm_real_values_to_db(logical_port_name, db_cache)
        assert diagnostic_tbl.get_size_for_key(logical_port_name) == 17

    def test_post_port_transceiver_hw_status_to_db(self):
        def mock_get_transceiver_status(physical_port):
            return {
                "cmis_state": "READY",
                "module_state": "ModuleReady",
                "module_fault_cause": "No Fault detected",
                "DP1State": "DataPathActivated",
                "DP2State": "DataPathActivated",
                "DP3State": "DataPathActivated",
                "DP4State": "DataPathActivated",
                "DP5State": "DataPathActivated",
                "DP6State": "DataPathActivated",
                "DP7State": "DataPathActivated",
                "DP8State": "DataPathActivated"
            }

        logical_port_name = "Ethernet0"
        port_mapping = PortMapping()
        port_mapping.get_logical_to_physical = MagicMock(return_value=[0])
        xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        stop_event = threading.Event()
        mock_sfp_obj_dict = {0 : MagicMock()}

        status_db_utils = StatusDBUtils(mock_sfp_obj_dict, port_mapping, xcvr_table_helper, stop_event, helper_logger)
        status_db_utils.status_utils = MagicMock()
        status_db_utils.xcvrd_utils.get_transceiver_presence = MagicMock(return_value=False)
        status_tbl = Table("STATE_DB", TRANSCEIVER_STATUS_TABLE)
        status_db_utils.xcvr_table_helper.get_status_tbl = MagicMock(return_value=status_tbl)
        status_db_utils.status_utils.get_transceiver_status = MagicMock(return_value=None)
        assert status_tbl.get_size() == 0

        # Ensure table is empty asic_index is None
        port_mapping.get_asic_id_for_logical_port = MagicMock(return_value=None)
        status_db_utils.post_port_transceiver_hw_status_to_db(logical_port_name)
        assert status_tbl.get_size() == 0

        # Set asic_index to 0
        port_mapping.get_asic_id_for_logical_port = MagicMock(return_value=0)

        # Ensure table is empty if stop_event is set
        stop_event.set()
        status_db_utils.post_port_transceiver_hw_status_to_db(logical_port_name)
        assert status_tbl.get_size() == 0
        stop_event.clear()

        # Ensure table is empty if transceiver is not present
        status_db_utils.post_port_transceiver_hw_status_to_db(logical_port_name)
        assert status_tbl.get_size() == 0
        status_db_utils.return_value = True

        # Ensure table is empty if get_values_func returns None
        status_db_utils.xcvrd_utils.get_transceiver_presence = MagicMock(return_value=True)
        status_db_utils.post_port_transceiver_hw_status_to_db(logical_port_name)
        assert status_tbl.get_size() == 0

        # Ensure table is populated if get_values_func returns valid values
        db_cache = {}
        status_db_utils.status_utils.get_transceiver_status = MagicMock(side_effect=mock_get_transceiver_status)
        status_db_utils.xcvrd_utils.is_transceiver_flat_memory = MagicMock(return_value=False)
        status_db_utils.post_port_transceiver_hw_status_to_db(logical_port_name, db_cache=db_cache)
        assert status_db_utils.xcvrd_utils.is_transceiver_flat_memory.call_count == 0
        assert status_tbl.get_size_for_key(logical_port_name) == 12

        # Ensure db_cache is populated correctly
        assert db_cache.get(0) is not None
        status_db_utils.status_utils.get_transceiver_status = MagicMock(return_value=None)
        status_db_utils.post_port_transceiver_hw_status_to_db(logical_port_name, db_cache=db_cache)
        assert status_tbl.get_size_for_key(logical_port_name) == 12

    def test_post_port_transceiver_hw_status_flags_to_db(self):
        def mock_get_transceiver_status_flags(physical_port):
            return {
                "datapath_firmware_fault": "False",
                "module_firmware_fault": "False",
                "module_state_changed": "False",
                "tx1fault": "N/A",
                "tx2fault": "N/A",
                "tx3fault": "N/A",
                "tx4fault": "N/A",
                "tx5fault": "N/A",
                "tx6fault": "N/A",
                "tx7fault": "N/A",
                "tx8fault": "N/A"
            }

        logical_port_name = "Ethernet0"
        port_mapping = PortMapping()
        port_mapping.get_logical_to_physical = MagicMock(return_value=[0])
        xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        stop_event = threading.Event()
        mock_sfp_obj_dict = {0 : MagicMock()}

        status_db_utils = StatusDBUtils(mock_sfp_obj_dict, port_mapping, xcvr_table_helper, stop_event, helper_logger)
        status_db_utils.status_utils = MagicMock()
        status_db_utils.xcvrd_utils.get_transceiver_presence = MagicMock(return_value=False)
        status_db_utils.xcvrd_utils.is_transceiver_flat_memory = MagicMock(return_value=False)
        status_flag_tbl = Table("STATE_DB", TRANSCEIVER_STATUS_FLAG_TABLE)
        status_db_utils.xcvr_table_helper.get_status_flag_tbl = MagicMock(return_value=status_flag_tbl)
        status_db_utils.status_utils.get_transceiver_status_flags = MagicMock(return_value=None)
        status_db_utils._update_flag_metadata_tables = MagicMock()
        assert status_flag_tbl.get_size() == 0

        # Ensure table is empty asic_index is None
        port_mapping.get_asic_id_for_logical_port = MagicMock(return_value=None)
        status_db_utils.post_port_transceiver_hw_status_flags_to_db(logical_port_name)
        assert status_flag_tbl.get_size() == 0

        # Set asic_index to 0
        port_mapping.get_asic_id_for_logical_port = MagicMock(return_value=0)

        # Ensure table is empty if stop_event is set
        stop_event.set()
        status_db_utils.post_port_transceiver_hw_status_flags_to_db(logical_port_name)
        assert status_flag_tbl.get_size() == 0
        stop_event.clear()

        # Ensure table is empty if transceiver is not present
        status_db_utils.post_port_transceiver_hw_status_flags_to_db(logical_port_name)
        assert status_flag_tbl.get_size() == 0
        status_db_utils.return_value = True

        # Ensure table is empty if get_values_func returns None
        status_db_utils.xcvrd_utils.get_transceiver_presence = MagicMock(return_value=True)
        status_db_utils.post_port_transceiver_hw_status_flags_to_db(logical_port_name)
        assert status_flag_tbl.get_size() == 0

        # Ensure table is populated if get_values_func returns valid values
        db_cache = {}
        status_db_utils.status_utils.get_transceiver_status_flags = MagicMock(side_effect=mock_get_transceiver_status_flags)
        status_db_utils.post_port_transceiver_hw_status_flags_to_db(logical_port_name, db_cache=db_cache)
        assert status_flag_tbl.get_size_for_key(logical_port_name) == 12
        assert status_db_utils._update_flag_metadata_tables.call_count == 1

        # Reset the mock to clear the call count
        status_db_utils._update_flag_metadata_tables.reset_mock()

        # Ensure db_cache is populated correctly
        assert db_cache.get(0) is not None
        status_db_utils.status_utils.get_transceiver_status_flags = MagicMock(return_value=None)
        status_db_utils.post_port_transceiver_hw_status_flags_to_db(logical_port_name, db_cache=db_cache)
        assert status_flag_tbl.get_size_for_key(logical_port_name) == 12
        assert status_db_utils._update_flag_metadata_tables.call_count == 0

    @patch('xcvrd.xcvrd_utilities.port_event_helper.PortMapping.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_transceiver_pm', MagicMock(return_value={'prefec_ber_avg': '0.0003407240007014899',
                                                                              'prefec_ber_min': '0.0006814479342250317',
                                                                              'prefec_ber_max': '0.0006833674050752236',
                                                                              'uncorr_frames_avg': '0.0',
                                                                              'uncorr_frames_min': '0.0',
                                                                              'uncorr_frames_max': '0.0', }))
    def test_post_port_pm_info_to_db(self):
        logical_port_name = "Ethernet0"
        port_mapping = PortMapping()
        mock_sfp_obj_dict = MagicMock()
        stop_event = threading.Event()
        mock_cmis_manager = MagicMock()
        dom_info_update = DomInfoUpdateTask(DEFAULT_NAMESPACE, port_mapping, mock_sfp_obj_dict, stop_event, mock_cmis_manager)
        pm_tbl = Table("STATE_DB", TRANSCEIVER_PM_TABLE)
        assert pm_tbl.get_size() == 0
        dom_info_update.post_port_pm_info_to_db(logical_port_name, port_mapping, pm_tbl, stop_event)
        assert pm_tbl.get_size_for_key(logical_port_name) == 6

    @patch('xcvrd.xcvrd_utilities.port_event_helper.PortMapping.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True))
    def test_del_port_sfp_dom_info_from_db(self):
        logical_port_name = "Ethernet0"
        port_mapping = PortMapping()
        dom_tbl = Table("STATE_DB", TRANSCEIVER_DOM_SENSOR_TABLE)
        dom_threshold_tbl = Table("STATE_DB", TRANSCEIVER_DOM_THRESHOLD_TABLE)
        init_tbl = Table("STATE_DB", TRANSCEIVER_INFO_TABLE)
        pm_tbl = Table("STATE_DB", TRANSCEIVER_PM_TABLE)
        firmware_info_tbl = Table("STATE_DB", TRANSCEIVER_FIRMWARE_INFO_TABLE)
        common.del_port_sfp_dom_info_from_db(logical_port_name, port_mapping, [init_tbl, dom_tbl, dom_threshold_tbl, pm_tbl, firmware_info_tbl])
        assert dom_tbl.get_size() == 0

    @pytest.mark.parametrize("mock_found, mock_state, expected_cmis_state", [
        (True, CMIS_STATE_INSERTED, CMIS_STATE_INSERTED),
        (False, None, CMIS_STATE_UNKNOWN)
    ])
    def test_get_cmis_state_from_state_db(self, mock_found, mock_state, expected_cmis_state):
        status_tbl = MagicMock()
        status_tbl.hget.return_value = (mock_found, mock_state)
        assert common.get_cmis_state_from_state_db("Ethernet0", status_tbl) == expected_cmis_state

    @patch('xcvrd.xcvrd_utilities.port_event_helper.PortMapping.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True))
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
    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=False))
    def test_post_port_sfp_info_to_db_with_sfp_not_present(self):
        logical_port_name = "Ethernet0"
        port_mapping = PortMapping()
        stop_event = threading.Event()
        intf_tbl = Table("STATE_DB", TRANSCEIVER_INFO_TABLE)
        transceiver_dict = {}
        post_port_sfp_info_to_db(logical_port_name, port_mapping, intf_tbl , transceiver_dict, stop_event)
        assert common._wrapper_get_presence.call_count == 1

    @patch('xcvrd.xcvrd_utilities.port_event_helper.PortMapping.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd.platform_sfputil', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True))
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
    @patch('swsscommon.swsscommon.WarmStart', MagicMock())
    def test_post_port_sfp_info_and_dom_thr_to_db_once(self):
        port_mapping = PortMapping()
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_ADD)
        port_mapping.handle_port_change_event(port_change_event)
        mock_sfp_obj_dict = MagicMock()
        stop_event = threading.Event()
        xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        sfp_error_event = threading.Event()
        task = SfpStateUpdateTask(DEFAULT_NAMESPACE, port_mapping, mock_sfp_obj_dict, stop_event, sfp_error_event)
        task._post_port_sfp_info_and_dom_thr_to_db_once(port_mapping, xcvr_table_helper, stop_event)

    @patch('xcvrd.xcvrd_utilities.port_event_helper.PortMapping.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd.platform_sfputil', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd._wrapper_is_replaceable', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd.XcvrTableHelper', MagicMock())
    def test_init_port_sfp_status_sw_tbl(self):
        port_mapping = PortMapping()
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_ADD)
        port_mapping.handle_port_change_event(port_change_event)
        mock_sfp_obj_dict = MagicMock()
        stop_event = threading.Event()
        xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        sfp_error_event = threading.Event()
        task = SfpStateUpdateTask(DEFAULT_NAMESPACE, port_mapping, mock_sfp_obj_dict, stop_event, sfp_error_event)
        task._init_port_sfp_status_sw_tbl(port_mapping, xcvr_table_helper, stop_event)

    @patch('sonic_py_common.device_info.get_paths_to_platform_and_hwsku_dirs', MagicMock(return_value=('/invalid/path', '/invalid/path')))
    def test_load_media_settings_missing_file(self):
        assert media_settings_parser.load_media_settings() == {}

    @patch('sonic_py_common.device_info.get_paths_to_platform_and_hwsku_dirs', MagicMock(return_value=('/invalid/path', '/invalid/path')))
    def test_load_optical_si_settings_missing_file(self):
        assert optics_si_parser.load_optics_si_settings() == {}

    @patch('xcvrd.xcvrd.platform_chassis')
    @patch('xcvrd.xcvrd_utilities.common.is_cmis_api')
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
        assert result == { 'vendor_key': 'MOLEX-1064141421', 'media_key': 'QSFP+-10GBase-SR-255M', 'lane_speed_key': 'speed:50G', 'medium_lane_speed_key': 'COPPER50'}

        # Test a bad 'specification_compliance' value
        xcvr_info_dict[0]['specification_compliance'] = 'N/A'
        result = media_settings_parser.get_media_settings_key(0, xcvr_info_dict, 100000, 2)
        assert result == { 'vendor_key': 'MOLEX-1064141421', 'media_key': 'QSFP+-*', 'lane_speed_key': 'speed:50G', 'medium_lane_speed_key': 'COPPER50'}
        # TODO: Ensure that error message was logged

        xcvr_info_dict_for_qsfp28 = {
            0: {
                "type": "QSFP28 or later",
                "type_abbrv_name": "QSFP28",
                "vendor_rev": "05",
                "serial": "AAABBBCCCDDD",
                "manufacturer": "AVAGO",
                "model": "XXX-YYY-ZZZ",
                "connector": "MPO 1x12",
                "encoding": "64B/66B",
                "ext_identifier": "Power Class 4 Module (3.5W max.), CLEI code present in Page 02h, CDR present in TX, CDR present in RX",
                "ext_rateselect_compliance": "Unknown",
                "cable_type": "Length Cable Assembly(m)",
                "cable_length": 50.0,
                "nominal_bit_rate": 255,
                "specification_compliance": "{'10/40G Ethernet Compliance Code': 'Unknown', 'SONET Compliance Codes': 'Unknown', 'SAS/SATA Compliance Codes': 'Unknown', 'Gigabit Ethernet Compliant Codes': 'Unknown', 'Fibre Channel Link Length': 'Unknown', 'Fibre Channel Transmitter Technology': 'Unknown', 'Fibre Channel Transmission Media': 'Unknown', 'Fibre Channel Speed': 'Unknown', 'Extended Specification Compliance': '100GBASE-SR4 or 25GBASE-SR'}",
                "vendor_date": "2020-11-11",
                "vendor_oui": "00-77-7a",
                "application_advertisement": "N/A",
            }
        }
        result = media_settings_parser.get_media_settings_key(
            0, xcvr_info_dict_for_qsfp28, 100000, 4
        )
        assert result == {
            "vendor_key": "AVAGO-XXX-YYY-ZZZ",
            "media_key": "QSFP28-100GBASE-SR4 or 25GBASE-SR-50.0M",
            "lane_speed_key": "speed:25G",
            "medium_lane_speed_key": "COPPER25",
        }

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
        assert result == { 'vendor_key': 'MOLEX-1064141421', 'media_key': 'QSFP-DD-sm_media_interface', 'lane_speed_key': 'speed:100GBASE-CR2', 'medium_lane_speed_key': 'COPPER50' }

    @pytest.mark.parametrize("data_found, data, expected", [
        (True, [('speed', '400000'), ('lanes', '1,2,3,4,5,6,7,8'), ('mtu', '9100')], (400000, 8, 0)),
        (True, [('speed', '25000'), ('lanes', '1'), ('mtu', '9100'), ('subport', '1')], (25000, 1, 1)),
        (True, [('lanes', '1,2,3,4,5,6,7,8'), ('mtu', '9100')], (0, 0, 0)),
        (True, [('speed', '400000'), ('mtu', '9100')], (0, 0, 0)),
        (False, [], (0, 0, 0))
    ])
    def test_get_speed_lane_count_and_subport(self, data_found, data, expected):
        cfg_port_tbl = MagicMock()
        cfg_port_tbl.get = MagicMock(return_value=(data_found, data))
        port = MagicMock()

        assert media_settings_parser.get_speed_lane_count_and_subport(port, cfg_port_tbl) == expected

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
    (media_settings_global_range_media_key_lane_speed_si, 7, {'vendor_key': 'UNKOWN', 'media_key': 'QSFP-DD-active_cable_media_interface', 'lane_speed_key': 'speed:100GAUI-2', 'medium_lane_speed_key': 'UNKNOWN'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_global_range_media_key_si, 7, {'vendor_key': 'UNKOWN', 'media_key': 'QSFP-DD-active_cable_media_interface', 'lane_speed_key': 'UNKOWN', 'medium_lane_speed_key': 'UNKNOWN'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_global_range_vendor_key_lane_speed_si, 7, {'vendor_key': 'AMPHANOL-5678', 'media_key': 'UNKOWN', 'lane_speed_key': 'speed:100GAUI-2', 'medium_lane_speed_key': 'UNKNOWN'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_global_range_vendor_key_lane_speed_si, 7, {'vendor_key': 'AMPHANOL-5678', 'media_key': 'UNKOWN', 'lane_speed_key': 'MISSING', 'medium_lane_speed_key': 'UNKNOWN'}, {}),
    (media_settings_global_range_vendor_key_si, 7, {'vendor_key': 'AMPHANOL-5678', 'media_key': 'UNKOWN', 'lane_speed_key': 'UNKOWN', 'medium_lane_speed_key': 'UNKNOWN'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_global_range_generic_vendor_key_lane_speed_si, 7, {'vendor_key': 'GENERIC_VENDOR-1234', 'media_key': 'UNKOWN', 'lane_speed_key': 'speed:100GAUI-2', 'medium_lane_speed_key': 'UNKNOWN'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_global_range_generic_vendor_key_lane_speed_si, 7, {'vendor_key': 'GENERIC_VENDOR-1234', 'media_key': 'UNKOWN', 'lane_speed_key': 'MISSING', 'medium_lane_speed_key': 'UNKNOWN'}, {}),
    (media_settings_global_range_generic_vendor_key_si, 7, {'vendor_key': 'GENERIC_VENDOR-1234', 'media_key': 'UNKOWN', 'lane_speed_key': 'UNKOWN', 'medium_lane_speed_key': 'UNKNOWN'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_global_list_media_key_lane_speed_si, 7, {'vendor_key': 'UNKOWN', 'media_key': 'QSFP-DD-active_cable_media_interface', 'lane_speed_key': 'speed:100GAUI-2', 'medium_lane_speed_key': 'UNKNOWN'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_global_list_media_key_lane_speed_si, 7, {'vendor_key': 'UNKOWN', 'media_key': 'QSFP-DD-active_cable_media_interface', 'lane_speed_key': 'MISSING', 'medium_lane_speed_key': 'UNKNOWN'}, {}),
    (media_settings_global_list_media_key_si, 7, {'vendor_key': 'UNKOWN', 'media_key': 'QSFP-DD-active_cable_media_interface', 'lane_speed_key': 'UNKOWN', 'medium_lane_speed_key': 'UNKNOWN'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_global_list_of_ranges_media_key_lane_speed_si, 7, {'vendor_key': 'UNKOWN', 'media_key': 'QSFP-DD-active_cable_media_interface', 'lane_speed_key': 'speed:100GAUI-2', 'medium_lane_speed_key': 'UNKNOWN'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_global_list_of_ranges_media_key_si, 7, {'vendor_key': 'UNKOWN', 'media_key': 'QSFP-DD-active_cable_media_interface', 'lane_speed_key': 'UNKOWN', 'medium_lane_speed_key': 'UNKNOWN'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_global_default_port_media_key_lane_speed_si, 6, {'vendor_key': 'AMPHANOL-5678', 'media_key': 'UNKOWN', 'lane_speed_key': 'speed:100GAUI-2', 'medium_lane_speed_key': 'UNKNOWN'}, asic_serdes_si_settings_example),
    (media_settings_port_vendor_key_lane_speed_si, -1, {'vendor_key': 'AMPHANOL-5678', 'media_key': 'UNKOWN', 'lane_speed_key': 'speed:100GAUI-2'}, {}),
    (media_settings_port_media_key_lane_speed_si, 7, {'vendor_key': 'UNKOWN', 'media_key': 'QSFP-DD-active_cable_media_interface', 'lane_speed_key': 'speed:100GAUI-2', 'medium_lane_speed_key': 'UNKNOWN'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_port_media_key_lane_speed_si, 7, {'vendor_key': 'UNKOWN', 'media_key': 'QSFP-DD-active_cable_media_interface', 'lane_speed_key': 'MISSING', 'medium_lane_speed_key': 'UNKNOWN'}, {}),
    (media_settings_port_media_key_si, 7, {'vendor_key': 'UNKOWN', 'media_key': 'QSFP-DD-active_cable_media_interface', 'lane_speed_key': 'UNKOWN', 'medium_lane_speed_key': 'UNKNOWN'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_port_vendor_key_lane_speed_si, 7, {'vendor_key': 'AMPHANOL-5678', 'media_key': 'UNKOWN', 'lane_speed_key': 'speed:100GAUI-2', 'medium_lane_speed_key': 'UNKNOWN'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_port_vendor_key_lane_speed_si, 7, {'vendor_key': 'AMPHANOL-5678', 'media_key': 'UNKOWN', 'lane_speed_key': 'MISSING', 'medium_lane_speed_key': 'UNKNOWN'}, {}),
    (media_settings_port_generic_vendor_key_lane_speed_si, 7, {'vendor_key': 'GENERIC_VENDOR-1234', 'media_key': 'UNKOWN', 'lane_speed_key': 'speed:100GAUI-2', 'medium_lane_speed_key': 'UNKNOWN'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_port_generic_vendor_key_lane_speed_si, 7, {'vendor_key': 'GENERIC_VENDOR-1234', 'media_key': 'UNKOWN', 'lane_speed_key': 'MISSING', 'medium_lane_speed_key': 'UNKNOWN'}, {}),
    (media_settings_port_vendor_key_si, 7, {'vendor_key': 'AMPHANOL-5678', 'media_key': 'UNKOWN', 'lane_speed_key': 'UNKOWN','medium_lane_speed_key': 'UNKNOWN'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_port_generic_vendor_key_si, 7, {'vendor_key': 'GENERIC_VENDOR-1234', 'media_key': 'UNKOWN', 'lane_speed_key': 'UNKOWN', 'medium_lane_speed_key': 'UNKNOWN'}, {'pre1': {'lane0': '0x00000002', 'lane1': '0x00000002'}, 'main': {'lane0': '0x00000020', 'lane1': '0x00000020'}, 'post1': {'lane0': '0x00000006', 'lane1': '0x00000006'}, 'regn_bfm1n': {'lane0': '0x000000aa', 'lane1': '0x000000aa'}}),
    (media_settings_port_default_media_key_lane_speed_si, 7, {'vendor_key': 'MISSING', 'media_key': 'MISSING', 'lane_speed_key': 'MISSING', 'medium_lane_speed_key': 'COPPER50'}, asic_serdes_si_settings_example),
    (media_settings_global_default_port_media_key_lane_speed_si, 7, {'vendor_key': 'MISSING', 'media_key': 'MISSING', 'lane_speed_key': 'MISSING', 'medium_lane_speed_key': 'UNKNOWN'}, asic_serdes_si_settings_example),
    (media_settings_global_list_of_ranges_media_key_lane_speed_si_with_default_section, 7, {'vendor_key': 'MISSING', 'media_key': 'MISSING', 'lane_speed_key': 'MISSING', 'medium_lane_speed_key': 'COPPER50'}, asic_serdes_si_settings_example),
    (media_settings_empty, 7, {'vendor_key': 'AMPHANOL-5678', 'media_key': 'QSFP-DD-active_cable_media_interface', 'lane_speed_key': 'speed:100GAUI-2', 'medium_lane_speed_key': 'COPPER50'}, {}),
    (media_settings_with_regular_expression_dict, 7, {'vendor_key': 'UNKOWN', 'media_key': 'QSFP28-40GBASE-CR4-1M', 'lane_speed_key': 'UNKOWN', 'medium_lane_speed_key': 'UNKNOWN'}, {'preemphasis': {'lane0': '0x16440A', 'lane1': '0x16440A', 'lane2': '0x16440A', 'lane3': '0x16440A'}}),
    (media_settings_with_regular_expression_dict, 7, {'vendor_key': 'UNKOWN', 'media_key': 'QSFP+-40GBASE-CR4-2M', 'lane_speed_key': 'UNKOWN','medium_lane_speed_key': 'COPPER50'}, {'preemphasis': {'lane0': '0x18420A', 'lane1': '0x18420A', 'lane2': '0x18420A', 'lane3': '0x18420A'}}),
    (media_settings_with_regular_expression_dict, 7, {'vendor_key': 'UNKOWN', 'media_key': 'QSFP+-40GBASE-CR4-10M', 'lane_speed_key': 'UNKOWN', 'medium_lane_speed_key': 'COPPER50'}, {'preemphasis': {'lane0': '0x1A400A', 'lane1': '0x1A400A', 'lane2': '0x1A400A', 'lane3': '0x1A400A'}}),
    (media_settings_global_medium_lane_key, 7, {'vendor_key': 'MISSING', 'media_key': 'MISSING', 'lane_speed_key': 'MISSING', 'medium_lane_speed_key': 'COPPER50'}, {'idriver': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}, 'pre1': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}, 'ob_m2lp': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}}),
   (media_settings_port_medium_lane_key, 7, {'vendor_key': 'MISSING', 'media_key': 'MISSING', 'lane_speed_key': 'MISSING', 'medium_lane_speed_key': 'COPPER25'}, {'idriver': {'lane0': '0x0000000f', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}, 'pre1': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}, 'ob_m2lp': {'lane0': '0x0000000d', 'lane1': '0x0000000d', 'lane2': '0x0000000d', 'lane3': '0x0000000d'}}),
    ])
    def test_get_media_settings_value(self, media_settings_dict, port, key, expected):
        with patch('xcvrd.xcvrd_utilities.media_settings_parser.g_dict', media_settings_dict):
            result = media_settings_parser.get_media_settings_value(port, key)
            assert result == expected

    @patch('xcvrd.xcvrd.platform_chassis')
    def test_get_is_copper_exception(self, mock_chassis):
        mock_sfp = MagicMock()
        mock_chassis.get_sfp = MagicMock(return_value=mock_sfp, side_effect=AttributeError)
        result = media_settings_parser.get_is_copper(0)
        assert result == True

    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd.XcvrTableHelper', MagicMock())
    @patch('xcvrd.xcvrd.XcvrTableHelper.get_cfg_port_tbl', MagicMock())
    @patch('xcvrd.xcvrd_utilities.media_settings_parser.g_dict', media_settings_optic_copper_si)
    @patch('xcvrd.xcvrd_utilities.media_settings_parser.get_media_settings_key',
           MagicMock(return_value={'vendor_key': 'INNOLIGHT-X-DDDDD-NNN', 'media_key': 'QSFP-DD-sm_media_interface', 'lane_speed_key': 'speed:400GAUI-8', 'medium_lane_speed_key': 'UNKNOWN'}))
    @patch('xcvrd.xcvrd_utilities.media_settings_parser.get_speed_lane_count_and_subport', MagicMock(return_value=(400000, 8, 0)))
    def test_notify_media_setting(self):
        # Test matching 400G optical transceiver (lane speed 50G)
        self._check_notify_media_setting(1, True, asic_serdes_si_settings_example4_expected_value_in_db)

        # Test matching 100G optical transceiver (lane speed 25G), via regular expression lane speed pattern
        with patch.multiple('xcvrd.xcvrd_utilities.media_settings_parser',
                            get_media_settings_key=MagicMock(return_value={'vendor_key':'INNOLIGHT-X-DDDDD-NNN', 'media_key': 'QSFP28-100GBASE-SR4', 'lane_speed_key': 'speed:25G', 'medium_lane_speed_key': 'UNKNOWN'}),
                            get_speed_lane_count_and_subport=MagicMock(return_value=(100000, 4, 0))):
            self._check_notify_media_setting(1, True, asic_serdes_si_settings_example3_expected_value_in_db_4_lanes)

        # Test matching 100G copper transceiver (lane speed 25G)
        with patch.multiple('xcvrd.xcvrd_utilities.media_settings_parser',
                            get_media_settings_key=MagicMock(return_value={'vendor_key':'INNOLIGHT-X-DDDDD-NNN', 'media_key': 'QSFP28-100GBASE-CR4, 25GBASE-CR CA-25G-L or 50GBASE-CR2 with RS-1.0M', 'lane_speed_key': 'speed:25G', 'medium_lane_speed_key': 'UNKNOWN'}),
                            get_speed_lane_count_and_subport=MagicMock(return_value=(100000, 4, 0))):
            self._check_notify_media_setting(1, True, asic_serdes_si_settings_example4_expected_value_in_db_4_lanes)

        # Test with lane speed None
        with patch.multiple('xcvrd.xcvrd_utilities.media_settings_parser',
                            get_media_settings_key=MagicMock(return_value={'vendor_key':'INNOLIGHT-X-DDDDD-NNN', 'media_key': 'QSFP28-100GBASE-CR4', 'lane_speed_key': None, 'medium_lane_speed_key': 'UNKNOWN'}),
                            get_speed_lane_count_and_subport=MagicMock(return_value=(100000, 4, 0))):
            self._check_notify_media_setting(1)

        # Test default value in the case of no matched lane speed for 800G copper transceiver (lane speed 100G)
        with patch.multiple('xcvrd.xcvrd_utilities.media_settings_parser',
                            get_media_settings_key=MagicMock(return_value={'vendor_key':'INNOLIGHT-X-DDDDD-NNN', 'media_key': 'QSFP-DD-passive_copper_media_interface', 'lane_speed_key': 'speed:800G-ETC-CR8', 'medium_lane_speed_key': 'UNKNOWN'}),
                            get_speed_lane_count_and_subport=MagicMock(return_value=(800000, 8, 0))):
            self._check_notify_media_setting(1, True, asic_serdes_si_settings_example5_expected_value_in_db)

        # Test lane speed matching under 'Default' vendor/media for 400G transceiver (lane speed 50G)
        with patch.multiple('xcvrd.xcvrd_utilities.media_settings_parser',
                            get_media_settings_key=MagicMock(return_value={'vendor_key':'Molex', 'media_key': 'QSFP-DD-passive_copper_media_interface', 'lane_speed_key': 'speed:400GAUI-8', 'medium_lane_speed_key': 'UNKNOWN'}),
                            get_speed_lane_count_and_subport=MagicMock(return_value=(400000, 8, 0))):
            self._check_notify_media_setting(41, True, asic_serdes_si_settings_example3_expected_value_in_db)

        # Test with empty xcvr_info_dict
        self._check_notify_media_setting(1, False, None, {})

        # Test with sfp not present
        with patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=False)):
            self._check_notify_media_setting(1)

    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd.XcvrTableHelper', MagicMock())
    @patch('xcvrd.xcvrd.XcvrTableHelper.get_cfg_port_tbl', MagicMock())
    @patch('xcvrd.xcvrd_utilities.media_settings_parser.g_dict', media_settings_with_comma_dict)
    @patch('xcvrd.xcvrd_utilities.media_settings_parser.get_media_settings_key', MagicMock(return_value={ 'vendor_key': 'MOLEX-1064141421', 'media_key': 'QSFP+-10GBase-SR-255M', 'lane_speed_key': 'speed:100GBASE-CR2', 'medium_lane_speed_key': 'UNKNOWN' }))
    @patch('xcvrd.xcvrd_utilities.media_settings_parser.get_speed_lane_count_and_subport', MagicMock(return_value=(100000, 2, 0)))
    def test_notify_media_setting_with_comma(self):
        self._check_notify_media_setting(1, True, {'preemphasis': ','.join(['0x164509'] * 2)})
        self._check_notify_media_setting(6, True, {'preemphasis': ','.join(['0x124A08'] * 2)})

    def _check_notify_media_setting(self, index, expected_found=False, expected_value=None, xcvr_info_dict=None):
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
        } if xcvr_info_dict is None else xcvr_info_dict
        app_port_tbl = Table("APPL_DB", 'PORT_TABLE')
        xcvr_table_helper.get_app_port_tbl = MagicMock(return_value=app_port_tbl)
        xcvr_table_helper.is_npu_si_settings_update_required = MagicMock(return_value=True)
        port_mapping = PortMapping()
        port_change_event = PortChangeEvent('Ethernet0', index, 0, PortChangeEvent.PORT_ADD)
        port_mapping.handle_port_change_event(port_change_event)
        media_settings_parser.notify_media_setting(logical_port_name, xcvr_info_dict, xcvr_table_helper, port_mapping)
        found, result = app_port_tbl.get(logical_port_name)
        result_dict = dict(result) if result else None
        assert found == expected_found
        assert result_dict == expected_value

    @patch('xcvrd.xcvrd_utilities.optics_si_parser.g_optics_si_dict', optics_si_settings_dict)
    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True))
    def test_fetch_optics_si_setting(self):
        self._check_fetch_optics_si_setting(1)

    @patch('xcvrd.xcvrd_utilities.optics_si_parser.g_optics_si_dict', optics_si_settings_with_comma_dict)
    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True))
    def test_fetch_optics_si_setting_with_comma(self):
        self._check_fetch_optics_si_setting(1)
        self._check_fetch_optics_si_setting(6)

    @patch('xcvrd.xcvrd_utilities.optics_si_parser.g_optics_si_dict', port_optics_si_settings)
    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True))
    def test_fetch_optics_si_setting_with_port(self):
        self._check_fetch_optics_si_setting(1)

    @patch('xcvrd.xcvrd_utilities.optics_si_parser.g_optics_si_dict', port_optics_si_settings)
    @patch('xcvrd.xcvrd_utilities.optics_si_parser.get_module_vendor_key', MagicMock(return_value=(None, None)))
    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True))
    def test_fetch_optics_si_setting_negative(self):
        port = 1
        lane_speed = 100
        mock_sfp = MagicMock()
        assert not optics_si_parser.fetch_optics_si_setting(port, lane_speed, mock_sfp)

    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True))
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

    def test_DaemonXcvrd_initialize_port_init_control_fields_in_port_table(self):
        port_mapping = PortMapping()
        xcvrd = DaemonXcvrd(SYSLOG_IDENTIFIER)

        port_mapping.logical_port_list = ['Ethernet0']
        port_mapping.get_asic_id_for_logical_port = MagicMock(return_value=0)
        mock_xcvrd_table_helper = MagicMock()
        mock_xcvrd_table_helper.get_state_port_tbl = MagicMock(return_value=None)
        xcvrd.xcvr_table_helper = mock_xcvrd_table_helper
        xcvrd.initialize_port_init_control_fields_in_port_table(port_mapping)

        mock_state_db = MagicMock()
        mock_xcvrd_table_helper.get_state_port_tbl = MagicMock(return_value=mock_state_db)
        mock_state_db.get = MagicMock(return_value=(False, {}))

        xcvrd.initialize_port_init_control_fields_in_port_table(port_mapping)
        mock_state_db.set.call_count = 2

    @patch('xcvrd.xcvrd.platform_chassis')
    def test_initialize_sfp_obj_dict(self, mock_platform_chassis):
        mock_sfp_obj_1 = MagicMock()
        mock_sfp_obj_2 = MagicMock()
        def mock_get_sfp(port):
            if port == 1:
                return mock_sfp_obj_1
            elif port == 2:
                return mock_sfp_obj_2
            else:
                raise ValueError("Invalid port")

        # Create a mock port mapping data
        mock_port_mapping_data = MagicMock()
        mock_port_mapping_data.physical_to_logical = {1: 'Ethernet0', 2: 'Ethernet1', 3: 'Ethernet2'}

        # Create an instance of DaemonXcvrd
        daemon_xcvrd = DaemonXcvrd(SYSLOG_IDENTIFIER)

        # port_mapping is None
        sfp_obj_dict = daemon_xcvrd.initialize_sfp_obj_dict(None)
        assert len(sfp_obj_dict) == 0
        assert mock_platform_chassis.get_sfp.call_count == 0

        # Mock the get_sfp method to return a MagicMock object
        # Call the method to test
        mock_platform_chassis.get_sfp.side_effect = mock_get_sfp
        sfp_obj_dict = daemon_xcvrd.initialize_sfp_obj_dict(mock_port_mapping_data)

        # Verify the  and the below also ensures that physical port 3 is not included since it is not in the port mapping
        assert len(sfp_obj_dict) == 2
        assert 1 in sfp_obj_dict
        assert 2 in sfp_obj_dict
        assert sfp_obj_dict[1] == mock_sfp_obj_1
        assert sfp_obj_dict[2] == mock_sfp_obj_2

    @pytest.mark.parametrize(
        "logical_ports, transceiver_presence, expected_removed_ports",
        [
            # Test case 1: No transceivers are present
            (["Ethernet0", "Ethernet1"], [False, False], ["Ethernet0", "Ethernet1"]),
            # Test case 2: Some transceivers are present
            (["Ethernet0", "Ethernet1"], [True, False], ["Ethernet1"]),
            # Test case 3: All transceivers are present
            (["Ethernet0", "Ethernet1"], [True, True], []),
            # Test case 4: No logical ports
            ([], [], []),
        ],
    )
    @patch('xcvrd.xcvrd_utilities.common.del_port_sfp_dom_info_from_db')
    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence')
    def test_remove_stale_transceiver_info(self, mock_get_presence, mock_del_port_sfp_dom_info_from_db,
                                           logical_ports, transceiver_presence, expected_removed_ports):
        # Mock the DaemonXcvrd class and its dependencies
        mock_xcvrd = DaemonXcvrd(SYSLOG_IDENTIFIER)
        mock_port_mapping_data = MagicMock()
        mock_xcvrd.xcvr_table_helper = MagicMock()
        mock_xcvrd.xcvr_table_helper.get_intf_tbl.return_value = MagicMock()

        # Mock logical ports and their mappings
        mock_port_mapping_data.logical_port_list = logical_ports
        mock_port_mapping_data.get_asic_id_for_logical_port.side_effect = lambda port: 0
        mock_port_mapping_data.get_logical_to_physical.side_effect = lambda port: [logical_ports.index(port)]

        mock_get_presence.side_effect = lambda physical_port: transceiver_presence[physical_port]

        # Mock the interface table
        mock_intf_tbl = mock_xcvrd.xcvr_table_helper.get_intf_tbl.return_value
        mock_intf_tbl.get.side_effect = lambda port: (port in logical_ports, None)

        # Call the function
        mock_xcvrd.remove_stale_transceiver_info(mock_port_mapping_data)

        # Verify that the correct ports were removed
        for port in logical_ports:
            if port in expected_removed_ports:
                mock_del_port_sfp_dom_info_from_db.assert_any_call(port, mock_port_mapping_data, [mock_intf_tbl])
            else:
                assert (port, mock_port_mapping_data, [mock_intf_tbl]) not in mock_del_port_sfp_dom_info_from_db.call_args_list

    @patch('xcvrd.cmis.CmisManagerTask.join')
    @patch('xcvrd.cmis.CmisManagerTask.start')
    @patch('xcvrd.xcvrd.DaemonXcvrd.init')
    @patch('xcvrd.xcvrd.DaemonXcvrd.deinit')
    @patch('xcvrd.xcvrd.DomInfoUpdateTask.start')
    @patch('xcvrd.xcvrd.SfpStateUpdateTask.start')
    @patch('xcvrd.xcvrd.DomInfoUpdateTask.join')
    @patch('xcvrd.xcvrd.SfpStateUpdateTask.join')
    def test_DaemonXcvrd_run(self, mock_task_stop1, mock_task_stop2, mock_task_run1, mock_task_run2, mock_deinit, mock_init, mock_cmis_join, mock_cmis_start):
        mock_init.return_value = PortMapping()
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

    @patch('xcvrd.xcvrd.helper_logger')
    @patch('xcvrd.xcvrd.platform_chassis')
    @patch('xcvrd.sff_mgr.PortChangeObserver')
    def test_SffManagerTask_xcvr_api_none_in_task_worker(self, mock_observer, mock_chassis, mock_logger):
        """Test the full task_worker flow when xcvr API is None"""
        mock_observer_instance = MagicMock()
        mock_observer_instance.handle_port_update_event = MagicMock(side_effect=[True, False])
        mock_observer.return_value = mock_observer_instance

        # Setup mock SFP that returns None for get_xcvr_api
        mock_sfp = MagicMock()
        mock_sfp.get_presence = MagicMock(return_value=True)
        mock_sfp.get_xcvr_api = MagicMock(return_value=None)
        mock_chassis.get_sfp = MagicMock(return_value=mock_sfp)

        sff_manager_task = SffManagerTask(DEFAULT_NAMESPACE,
                                          threading.Event(),
                                          mock_chassis,
                                          mock_logger)

        # Setup port_dict with necessary data
        sff_manager_task.port_dict['Ethernet0'] = {
            'index': 1,
            'type': 'QSFP28',
            'subport': '0',
            'lanes': ['1', '2', '3', '4'],
            'host_tx_ready': 'true',
            'admin_status': 'up',
            'asic_id': 0
        }

        # Mock task_stopping_event to stop after processing once
        sff_manager_task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])

        # Run task_worker - it should handle the None API gracefully
        sff_manager_task.task_worker()

        # Verify error was logged
        assert any("skipping sff_mgr since no xcvr api!" in str(call)
                   for call in mock_logger.log_error.call_args_list)

    def test_SffManagerTask_enable_high_power_class(self):
        mock_xcvr_api = MagicMock()
        mock_xcvr_api.get_power_class = MagicMock(return_value=5)
        mock_xcvr_api.set_high_power_class = MagicMock(return_value=True)
        lport = 'Ethernet0'

        sff_manager_task = SffManagerTask(DEFAULT_NAMESPACE,
                                          threading.Event(),
                                          MagicMock(),
                                          helper_logger)

        # Test with normal case
        sff_manager_task.enable_high_power_class(mock_xcvr_api, lport)
        assert mock_xcvr_api.get_power_class.call_count == 1
        assert mock_xcvr_api.set_high_power_class.call_count == 1

        # Test with get_power_class failed
        mock_xcvr_api.get_power_class.return_value = None
        sff_manager_task.enable_high_power_class(mock_xcvr_api, lport)
        assert mock_xcvr_api.get_power_class.call_count == 2
        assert mock_xcvr_api.set_high_power_class.call_count == 1

        # Test for no need to set high power class
        mock_xcvr_api.get_power_class.return_value = 4
        sff_manager_task.enable_high_power_class(mock_xcvr_api, lport)
        assert mock_xcvr_api.get_power_class.call_count == 3
        assert mock_xcvr_api.set_high_power_class.call_count == 1

        # Test for set_high_power_class failed
        mock_xcvr_api.get_power_class.return_value = 5
        mock_xcvr_api.set_high_power_class.return_value = False
        sff_manager_task.enable_high_power_class(mock_xcvr_api, lport)
        assert mock_xcvr_api.get_power_class.call_count == 4
        assert mock_xcvr_api.set_high_power_class.call_count == 2

        # Test for set_high_power_class not supported
        mock_xcvr_api.get_power_class.return_value = 5
        mock_xcvr_api.set_high_power_class = MagicMock(side_effect=AttributeError("Attribute not found"))
        sff_manager_task.enable_high_power_class(mock_xcvr_api, lport)
        assert mock_xcvr_api.get_power_class.call_count == 5
        assert mock_xcvr_api.set_high_power_class.call_count == 1

    @patch('xcvrd.xcvrd.helper_logger')
    @patch('xcvrd.xcvrd.platform_chassis')
    @patch('xcvrd.sff_mgr.PortChangeObserver', MagicMock(handle_port_update_event=MagicMock()))
    def test_SffManagerTask_task_worker(self, mock_chassis, mock_logger):
        mock_xcvr_api = MagicMock()
        mock_xcvr_api.tx_disable_channel = MagicMock(return_value=True)
        mock_xcvr_api.is_flat_memory = MagicMock(return_value=False)
        mock_xcvr_api.is_copper = MagicMock(return_value=False)
        mock_xcvr_api.get_tx_disable_support = MagicMock(return_value=True)
        mock_xcvr_api.get_power_class = MagicMock(return_value=1)

        mock_sfp = MagicMock()
        mock_sfp.get_presence = MagicMock(return_value=True)
        mock_sfp.get_xcvr_api = MagicMock(return_value=mock_xcvr_api)

        mock_chassis.get_all_sfps = MagicMock(return_value=[mock_sfp])
        mock_chassis.get_sfp = MagicMock(return_value=mock_sfp)

        task = SffManagerTask(DEFAULT_NAMESPACE,
                              threading.Event(),
                              mock_chassis,
                              mock_logger)

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
        mock_logger.log_error.assert_called_once_with(
            "SFF-MAIN: Ethernet0: module not present!")
        mock_sfp.get_presence = MagicMock(return_value=True)

        # lpmode setting case
        # 1. error logged when lpmode is suppoted but unsuccessful
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_SET,
                                            {'type': 'QSFP28'})
        task.on_port_update_event(port_change_event)
        mock_xcvr_api.set_lpmode = MagicMock(return_value=False)
        mock_xcvr_api.get_lpmode_support = MagicMock(return_value=True)
        task.port_dict_prev = {}
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert len(mock_logger.log_error.call_args_list) == 2
        mock_logger.log_error.assert_called_with(
            "SFF-MAIN: Ethernet0: Failed to take module out of low power mode.")

        # 2. no error logged when lpmode is suppoted and successful
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_SET,
                                            {'type': 'QSFP28'})
        task.on_port_update_event(port_change_event)
        mock_xcvr_api.set_lpmode = MagicMock(return_value=True)
        mock_xcvr_api.get_lpmode_support = MagicMock(return_value=True)
        task.port_dict_prev = {}
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert len(mock_logger.log_error.call_args_list) == 2
        mock_logger.log_error.assert_called_with(
            "SFF-MAIN: Ethernet0: Failed to take module out of low power mode.")

        # 3. no error logged when lpmode is not suppoted
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_SET,
                                            {'type': 'QSFP28'})
        task.on_port_update_event(port_change_event)
        mock_xcvr_api.set_lpmode = MagicMock(return_value=False)
        mock_xcvr_api.get_lpmode_support = MagicMock(return_value=False)
        task.port_dict_prev = {}
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert len(mock_logger.log_error.call_args_list) == 2
        mock_logger.log_error.assert_called_with(
            "SFF-MAIN: Ethernet0: Failed to take module out of low power mode.")
        mock_xcvr_api.set_lpmode = MagicMock(return_value=True)
        mock_xcvr_api.get_lpmode_support = MagicMock(return_value=True)

    def test_CmisManagerTask_update_port_transceiver_status_table_sw_cmis_state(self):
        port_mapping = PortMapping()
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=MagicMock())
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_SET)
        task.on_port_update_event(port_change_event)

        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.xcvr_table_helper.get_status_sw_tbl = MagicMock(return_value=None)
        task.update_port_transceiver_status_table_sw_cmis_state("Ethernet0", CMIS_STATE_INSERTED)

        mock_get_status_tbl = MagicMock()
        mock_get_status_tbl.set = MagicMock()
        task.xcvr_table_helper.get_status_sw_tbl.return_value = mock_get_status_tbl
        task.update_port_transceiver_status_table_sw_cmis_state("Ethernet0", CMIS_STATE_INSERTED)
        assert mock_get_status_tbl.set.call_count == 1

    @patch('xcvrd.xcvrd._wrapper_get_sfp_type', MagicMock(return_value='QSFP_DD'))
    def test_CmisManagerTask_handle_port_change_event(self):
        port_mapping = PortMapping()
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=MagicMock())

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
        assert len(task.port_dict) == 0

        port_dict = {'speed':'400000', 'lanes':'1,2,3,4,5,6,7,8'}
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_SET, port_dict)
        task.on_port_update_event(port_change_event)
        assert len(task.port_dict) == 1

        # STATE_DB DEL event doesn't remove port from port_dict
        # this happens when transceiver is plugged-out or DPB is used
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_DEL, {}, db_name='STATE_DB')
        task.on_port_update_event(port_change_event)
        assert len(task.port_dict) == 1

        # CONFIG_DB DEL event removes port from port_dict
        # this happens when DPB is used
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_DEL, {}, db_name='CONFIG_DB', table_name='PORT')
        task.on_port_update_event(port_change_event)
        assert len(task.port_dict) == 0

    @patch('xcvrd.xcvrd.XcvrTableHelper')
    def test_CmisManagerTask_get_configured_freq(self, mock_table_helper):
        port_mapping = PortMapping()
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=MagicMock())
        cfg_port_tbl = MagicMock()
        cfg_port_tbl.hget = MagicMock(return_value=(True, 193100))
        mock_table_helper.get_cfg_port_tbl = MagicMock(return_value=cfg_port_tbl)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.xcvr_table_helper.get_cfg_port_tbl = mock_table_helper.get_cfg_port_tbl
        assert task.get_configured_laser_freq_from_db('Ethernet0') == 193100

    @patch('xcvrd.xcvrd.XcvrTableHelper')
    def test_CmisManagerTask_get_configured_tx_power_from_db(self, mock_table_helper):
        port_mapping = PortMapping()
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=MagicMock())
        cfg_port_tbl = MagicMock()
        cfg_port_tbl.hget = MagicMock(return_value=(True, -10))
        mock_table_helper.get_cfg_port_tbl = MagicMock(return_value=cfg_port_tbl)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.xcvr_table_helper.get_cfg_port_tbl = mock_table_helper.get_cfg_port_tbl
        assert task.get_configured_tx_power_from_db('Ethernet0') == -10

    @patch('xcvrd.xcvrd.XcvrTableHelper.get_status_sw_tbl')
    @patch('xcvrd.xcvrd.platform_chassis')
    @patch('xcvrd.xcvrd_utilities.common.is_fast_reboot_enabled', MagicMock(return_value=False))
    @patch('xcvrd.xcvrd_utilities.common.get_cmis_application_desired', MagicMock(return_value=1))
    def test_CmisManagerTask_process_single_lport_invalid_host_lanes_mask(self, mock_chassis, mock_get_status_sw_tbl):
        """Test process_single_lport when get_cmis_host_lanes_mask returns invalid value (<=0)"""
        mock_get_status_sw_tbl = Table("STATE_DB", TRANSCEIVER_STATUS_SW_TABLE)

        # Setup mock SFP and API
        mock_xcvr_api = MagicMock()
        mock_xcvr_api.get_presence = MagicMock(return_value=True)
        mock_xcvr_api.is_flat_memory = MagicMock(return_value=False)
        mock_xcvr_api.get_module_type_abbreviation = MagicMock(return_value='QSFP-DD')
        mock_xcvr_api.is_coherent_module = MagicMock(return_value=False)
        mock_xcvr_api.get_module_state = MagicMock(return_value='ModuleReady')
        mock_xcvr_api.get_datapath_state = MagicMock(return_value={
            'DP1State': 'DataPathDeactivated',
            'DP2State': 'DataPathDeactivated',
            'DP3State': 'DataPathDeactivated',
            'DP4State': 'DataPathDeactivated',
            'DP5State': 'DataPathDeactivated',
            'DP6State': 'DataPathDeactivated',
            'DP7State': 'DataPathDeactivated',
            'DP8State': 'DataPathDeactivated'
        })
        mock_xcvr_api.get_media_lane_count = MagicMock(return_value=8)
        mock_xcvr_api.get_media_lane_assignment_option = MagicMock(return_value=1)

        mock_sfp = MagicMock()
        mock_sfp.get_presence = MagicMock(return_value=True)
        mock_sfp.get_xcvr_api = MagicMock(return_value=mock_xcvr_api)
        mock_chassis.get_sfp = MagicMock(return_value=mock_sfp)

        # Setup port mapping and task
        port_mapping = PortMapping()
        port_mapping.handle_port_change_event(PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_ADD))
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=mock_chassis)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.xcvr_table_helper.get_status_sw_tbl.return_value = mock_get_status_sw_tbl

        # Properly set up the port via port change event
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_SET,
                                            {'speed':'400000', 'lanes':'1,2,3,4,5,6,7,8'})
        task.on_port_update_event(port_change_event)

        # Set port to INSERTED state
        task.update_port_transceiver_status_table_sw_cmis_state('Ethernet0', CMIS_STATE_INSERTED)
        task.port_dict['Ethernet0']['host_tx_ready'] = 'true'
        task.port_dict['Ethernet0']['admin_status'] = 'up'

        # Mock get_cmis_host_lanes_mask to return invalid value (0)
        task.get_cmis_host_lanes_mask = MagicMock(return_value=0)

        # Create port info
        info = task.port_dict['Ethernet0']

        # Process the port - should fail due to invalid host_lanes_mask
        task.process_single_lport('Ethernet0', info, {})

        # Verify state transitioned to FAILED
        assert common.get_cmis_state_from_state_db('Ethernet0', mock_get_status_sw_tbl) == CMIS_STATE_FAILED

    @patch('xcvrd.xcvrd.XcvrTableHelper.get_status_sw_tbl')
    @patch('xcvrd.xcvrd.platform_chassis')
    @patch('xcvrd.xcvrd_utilities.common.is_fast_reboot_enabled', MagicMock(return_value=False))
    def test_CmisManagerTask_process_single_lport_tx_power_config_failure(self, mock_chassis, mock_get_status_sw_tbl):
        """Test process_single_lport when configure_tx_output_power fails for coherent module"""
        mock_get_status_sw_tbl = Table("STATE_DB", TRANSCEIVER_STATUS_SW_TABLE)

        # Setup mock coherent module API
        mock_xcvr_api = MagicMock()
        mock_xcvr_api.get_presence = MagicMock(return_value=True)
        mock_xcvr_api.is_flat_memory = MagicMock(return_value=False)
        mock_xcvr_api.get_module_type_abbreviation = MagicMock(return_value='QSFP-DD')
        mock_xcvr_api.is_coherent_module = MagicMock(return_value=True)
        mock_xcvr_api.get_module_state = MagicMock(return_value='ModuleReady')
        mock_xcvr_api.get_datapath_state = MagicMock(return_value={
            'DP1State': 'DataPathDeactivated',
            'DP2State': 'DataPathDeactivated',
            'DP3State': 'DataPathDeactivated',
            'DP4State': 'DataPathDeactivated',
            'DP5State': 'DataPathDeactivated',
            'DP6State': 'DataPathDeactivated',
            'DP7State': 'DataPathDeactivated',
            'DP8State': 'DataPathDeactivated'
        })
        mock_xcvr_api.get_tx_config_power = MagicMock(return_value=-5)  # Different from configured
        mock_xcvr_api.get_laser_config_freq = MagicMock(return_value=193100)
        mock_xcvr_api.set_lpmode = MagicMock(return_value=True)
        mock_xcvr_api.get_datapath_deinit_duration = MagicMock(return_value=600000.0)
        mock_xcvr_api.get_module_pwr_up_duration = MagicMock(return_value=70000.0)

        mock_sfp = MagicMock()
        mock_sfp.get_presence = MagicMock(return_value=True)
        mock_sfp.get_xcvr_api = MagicMock(return_value=mock_xcvr_api)
        mock_chassis.get_sfp = MagicMock(return_value=mock_sfp)

        # Setup port mapping and task
        port_mapping = PortMapping()
        port_mapping.handle_port_change_event(PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_ADD))
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=mock_chassis)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.xcvr_table_helper.get_status_sw_tbl.return_value = mock_get_status_sw_tbl

        # Properly set up the port via port change event
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_SET,
                                            {'speed':'400000', 'lanes':'1,2,3,4,5,6,7,8'})
        task.on_port_update_event(port_change_event)

        # Set port to DP_PRE_INIT_CHECK state with coherent module settings
        task.update_port_transceiver_status_table_sw_cmis_state('Ethernet0', CMIS_STATE_DP_PRE_INIT_CHECK)
        task.port_dict['Ethernet0']['host_tx_ready'] = 'true'
        task.port_dict['Ethernet0']['admin_status'] = 'up'
        task.port_dict['Ethernet0']['appl'] = 1
        task.port_dict['Ethernet0']['host_lanes_mask'] = 0xff
        task.port_dict['Ethernet0']['media_lanes_mask'] = 0xff
        task.port_dict['Ethernet0']['tx_power'] = -10  # Configured tx power
        task.port_dict['Ethernet0']['laser_freq'] = 193100

        # Mock configure_tx_output_power to return failure (not 1)
        task.configure_tx_output_power = MagicMock(return_value=0)

        # Mock is_cmis_application_update_required to return True so we proceed
        task.is_cmis_application_update_required = MagicMock(return_value=True)

        # Create port info
        info = task.port_dict['Ethernet0']

        # Process the port - should log error when tx power config fails
        task.process_single_lport('Ethernet0', info, {})

        # Verify configure_tx_output_power was called
        assert task.configure_tx_output_power.called
        # The state should still progress (error is logged but not fatal)
        # Verify we moved to DP_DEINIT state (the state machine continues despite tx power config failure)
        assert common.get_cmis_state_from_state_db('Ethernet0', mock_get_status_sw_tbl) == CMIS_STATE_DP_DEINIT

    @patch('xcvrd.xcvrd.platform_chassis')
    @patch('xcvrd.xcvrd_utilities.common.is_fast_reboot_enabled', MagicMock(return_value=(False)))
    @patch('xcvrd.cmis.cmis_manager_task.PortChangeObserver', MagicMock(handle_port_update_event=MagicMock()))
    def test_CmisManagerTask_task_run_stop(self, mock_chassis):
        mock_object = MagicMock()
        mock_object.get_presence = MagicMock(return_value=True)
        mock_chassis.get_all_sfps = MagicMock(return_value=[mock_object, mock_object])

        port_mapping = PortMapping()
        stop_event = threading.Event()
        cmis_manager = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=MagicMock())
        cmis_manager.wait_for_port_config_done = MagicMock()
        cmis_manager.start()
        cmis_manager.join()
        assert not cmis_manager.is_alive()

    @pytest.mark.parametrize("app_new, lane_appl_code, expected", [
        (2, {0 : 1, 1 : 1, 2 : 1, 3 : 1, 4 : 2, 5 : 2, 6 : 2, 7 : 2}, True),
        (0, {0 : 1, 1 : 1, 2 : 1, 3 : 1}, True),
        (1, {0 : 0, 1 : 0, 2 : 0, 3 : 0, 4 : 0, 5 : 0, 6 : 0, 7 : 0}, False)
     ])
    def test_CmisManagerTask_is_decommission_required(self, app_new, lane_appl_code, expected):
        mock_xcvr_api = MagicMock()
        def get_application(lane):
            return lane_appl_code.get(lane, 0)
        mock_xcvr_api.get_application = MagicMock(side_effect=get_application)
        port_mapping = PortMapping()
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=MagicMock())
        assert task.is_decommission_required(mock_xcvr_api, app_new) == expected

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
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=MagicMock())

        assert task.is_cmis_application_update_required(mock_xcvr_api, app_new, host_lanes_mask) == expected

    @pytest.mark.parametrize("ifname, expected", [
        ('1.6TBASE-CR8 (Clause179)', 1600000),
        ('1.6TAUI-8 (Annex176E)', 1600000),
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
        assert common.get_interface_speed(ifname) == expected

    @patch('xcvrd.xcvrd_utilities.common.is_cmis_api', MagicMock(return_value=True))
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
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=MagicMock())

        appl = common.get_cmis_application_desired(mock_xcvr_api, host_lane_count, speed)
        assert task.get_cmis_host_lanes_mask(mock_xcvr_api, appl, host_lane_count, subport) == expected

    @pytest.mark.parametrize("gearbox_data, expected_dict", [
        # Test case 1: Gearbox port with 2 line lanes
        ({
            "interface:0": {
                "name": "Ethernet0",
                "index": "0",
                "phy_id": "1",
                "system_lanes": "300,301,302,303",
                "line_lanes": "304,305"
            }
        }, {"Ethernet0": 2}),
        # Test case 2: Multiple gearbox ports
        ({
            "interface:0": {
                "name": "Ethernet0",
                "index": "0",
                "phy_id": "1",
                "system_lanes": "300,301,302,303",
                "line_lanes": "304,305,306,307"
            },
            "interface:200": {
                "name": "Ethernet200",
                "index": "200",
                "phy_id": "2",
                "system_lanes": "400,401",
                "line_lanes": "404,405"
            }
        }, {"Ethernet0": 4, "Ethernet200": 2}),
        # Test case 3: Empty gearbox data
        ({}, {}),
        # Test case 4: Gearbox interface with empty line_lanes
        ({
            "interface:0": {
                "name": "Ethernet0",
                "index": "0",
                "phy_id": "1",
                "system_lanes": "300,301,302,303",
                "line_lanes": ""
            }
        }, {}),
        # Test case 5: Non-interface keys (should be ignored)
        ({
            "interface:0": {
                "name": "Ethernet0",
                "index": "0",
                "phy_id": "1",
                "system_lanes": "300,301,302,303",
                "line_lanes": "304,305"
            },
            "phy:1": {
                "name": "phy1",
                "some_field": "some_value"
            }
        }, {"Ethernet0": 2})
    ])
    def test_XcvrTableHelper_get_gearbox_line_lanes_dict(self, gearbox_data, expected_dict):
        # Mock the XcvrTableHelper and APPL_DB access
        mock_appl_db = MagicMock()
        mock_gearbox_table = MagicMock()

        # Mock table.getKeys() to return gearbox interface keys
        mock_gearbox_table.getKeys.return_value = list(gearbox_data.keys())

        # Mock table.get() to return gearbox interface data
        def mock_get_side_effect(key):
            if key in gearbox_data:
                # Convert dict to list of tuples for fvs format
                interface_data = gearbox_data[key]
                fvs_list = [(k, v) for k, v in interface_data.items()]
                return (True, fvs_list)
            return (False, [])

        mock_gearbox_table.get.side_effect = mock_get_side_effect

        # Mock swsscommon.Table constructor to return our mock table
        with patch('xcvrd.xcvrd_utilities.xcvr_table_helper.swsscommon.Table', return_value=mock_gearbox_table):
            # Mock the helper_logger to avoid logging during tests
            with patch('xcvrd.xcvrd_utilities.xcvr_table_helper.helper_logger'):
                helper = XcvrTableHelper(DEFAULT_NAMESPACE)
                helper.appl_db = {0: mock_appl_db}  # Mock the appl_db dict

                result = helper.get_gearbox_line_lanes_dict()
                assert result == expected_dict

    @pytest.mark.parametrize("gearbox_lanes_dict, lport, port_config_lanes, expected_count", [
        # Test case 1: Gearbox data available, should use gearbox count
        ({"Ethernet0": 2}, "Ethernet0", "25,26,27,28", 2),
        # Test case 2: Gearbox data available with 4 lanes
        ({"Ethernet0": 4}, "Ethernet0", "29,30", 4),
        # Test case 3: No gearbox data for this port, should use port config
        ({"Ethernet4": 2}, "Ethernet0", "33,34,35,36", 4),
        # Test case 4: Empty gearbox dict, should use port config
        ({}, "Ethernet0", "37,38", 2),
        # Test case 5: Multiple ports in gearbox dict
        ({"Ethernet0": 2, "Ethernet4": 4}, "Ethernet0", "25,26,27,28", 2),
        # Test case 6: Port not in gearbox dict
        ({"Ethernet4": 4}, "Ethernet8", "41,42,43", 3)
    ])
    def test_CmisManagerTask_get_host_lane_count(self, gearbox_lanes_dict, lport, port_config_lanes, expected_count):
        port_mapping = PortMapping()
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=MagicMock())

        result = task.get_host_lane_count(lport, port_config_lanes, gearbox_lanes_dict)
        assert result == expected_count

    def test_CmisManagerTask_gearbox_integration_end_to_end(self):
        """Test end-to-end integration of gearbox line lanes with CMIS application selection"""
        port_mapping = PortMapping()
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=MagicMock())

        # Mock gearbox lanes dictionary - port has 4 system lanes but only 2 line lanes
        gearbox_lanes_dict = {"Ethernet0": 2}  # 2 line lanes from gearbox

        # Mock port config - would normally give 4 lanes
        port_config_lanes = "25,26,27,28"  # 4 lanes from port config

        # Mock CMIS API with application advertisement
        mock_xcvr_api = MagicMock()
        mock_xcvr_api.get_application_advertisement.return_value = {
            1: {
                'host_electrical_interface_id': '100GAUI-2 C2M (Annex 135G)',
                'module_media_interface_id': '100G-FR/100GBASE-FR1 (Cl 140)',
                'media_lane_count': 1,
                'host_lane_count': 2,  # Matches our gearbox line lanes
                'host_lane_assignment_options': 85
            },
            2: {
                'host_electrical_interface_id': 'CAUI-4 C2M (Annex 83E)',
                'module_media_interface_id': 'Active Cable assembly',
                'media_lane_count': 4,
                'host_lane_count': 4,  # Would match port config lanes
                'host_lane_assignment_options': 17
            }
        }

        # Test the integration: should use gearbox line lanes (2) not port config lanes (4)
        host_lane_count = task.get_host_lane_count("Ethernet0", port_config_lanes, gearbox_lanes_dict)
        assert host_lane_count == 2  # Should use gearbox line lanes, not port config

        # Test that this leads to correct CMIS application selection
        with patch('xcvrd.xcvrd_utilities.common.is_cmis_api', return_value=True):
            appl = common.get_cmis_application_desired(mock_xcvr_api, host_lane_count, 100000)
            assert appl == 1  # Should select application 1 (2 lanes) not application 2 (4 lanes)

    def test_CmisManagerTask_gearbox_caching_integration(self):
        """Test that gearbox lanes dictionary is properly cached and used in task worker"""
        port_mapping = PortMapping()
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=MagicMock())

        # Mock the XcvrTableHelper to return a gearbox lanes dictionary
        mock_gearbox_lanes_dict = {"Ethernet0": 2, "Ethernet4": 4}
        task.xcvr_table_helper = MagicMock()
        task.xcvr_table_helper.get_gearbox_line_lanes_dict.return_value = mock_gearbox_lanes_dict

        # Test that get_host_lane_count uses the cached dictionary correctly
        result1 = task.get_host_lane_count("Ethernet0", "25,26,27,28", mock_gearbox_lanes_dict)
        assert result1 == 2  # Should use gearbox count

        result2 = task.get_host_lane_count("Ethernet4", "29,30", mock_gearbox_lanes_dict)
        assert result2 == 4  # Should use gearbox count

        result3 = task.get_host_lane_count("Ethernet8", "33,34,35", mock_gearbox_lanes_dict)
        assert result3 == 3  # Should fall back to port config count

    @patch('swsscommon.swsscommon.FieldValuePairs')
    def test_CmisManagerTask_post_port_active_apsel_to_db_error_cases(self, mock_field_value_pairs):
        mock_xcvr_api = MagicMock()
        mock_xcvr_api.get_active_apsel_hostlane = MagicMock()
        mock_xcvr_api.get_application_advertisement = MagicMock()

        port_mapping = PortMapping()
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=MagicMock())
        lport = "Ethernet0"
        host_lanes_mask = 0xff

        # Case: table does not exist
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.xcvr_table_helper.get_intf_tbl = MagicMock(return_value=None)
        task.post_port_active_apsel_to_db(mock_xcvr_api, lport, host_lanes_mask)
        assert mock_field_value_pairs.call_count == 0

        # Case: lport is not in the table
        int_tbl = MagicMock()
        int_tbl.get = MagicMock(return_value=(False, dict))
        task.xcvr_table_helper.get_intf_tbl = MagicMock(return_value=int_tbl)
        task.post_port_active_apsel_to_db(mock_xcvr_api, lport, host_lanes_mask)
        assert mock_field_value_pairs.call_count == 0

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
        int_tbl.get = MagicMock(return_value=(True, dict))

        port_mapping = PortMapping()
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=MagicMock())
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.xcvr_table_helper.get_intf_tbl = MagicMock(return_value=int_tbl)

        # case: partial lanes update
        lport = "Ethernet0"
        host_lanes_mask = 0xc
        ret = task.post_port_active_apsel_to_db(mock_xcvr_api, lport, host_lanes_mask)
        assert int_tbl.getKeys() == ["Ethernet0"]
        assert dict(int_tbl.mock_dict["Ethernet0"]) == {'active_apsel_hostlane1': 'N/A',
                                                        'active_apsel_hostlane2': 'N/A',
                                                        'active_apsel_hostlane3': '1',
                                                        'active_apsel_hostlane4': '1',
                                                        'active_apsel_hostlane5': 'N/A',
                                                        'active_apsel_hostlane6': 'N/A',
                                                        'active_apsel_hostlane7': 'N/A',
                                                        'active_apsel_hostlane8': 'N/A',
                                                        'host_lane_count': '8',
                                                        'media_lane_count': '4'}
        # case: full lanes update
        lport = "Ethernet8"
        host_lanes_mask = 0xff
        task.post_port_active_apsel_to_db(mock_xcvr_api, lport, host_lanes_mask)
        assert int_tbl.getKeys() == ["Ethernet0", "Ethernet8"]
        assert dict(int_tbl.mock_dict["Ethernet0"]) == {'active_apsel_hostlane1': 'N/A',
                                                        'active_apsel_hostlane2': 'N/A',
                                                        'active_apsel_hostlane3': '1',
                                                        'active_apsel_hostlane4': '1',
                                                        'active_apsel_hostlane5': 'N/A',
                                                        'active_apsel_hostlane6': 'N/A',
                                                        'active_apsel_hostlane7': 'N/A',
                                                        'active_apsel_hostlane8': 'N/A',
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

        # case: partial lanes update (reset to 'N/A')
        lport = "Ethernet16"
        host_lanes_mask = 0xc
        ret = task.post_port_active_apsel_to_db(mock_xcvr_api, lport, host_lanes_mask, reset_apsel=True)
        assert int_tbl.getKeys() == ["Ethernet0", "Ethernet8", "Ethernet16"]
        assert dict(int_tbl.mock_dict["Ethernet16"]) == {'active_apsel_hostlane1': 'N/A',
                                                        'active_apsel_hostlane2': 'N/A',
                                                        'active_apsel_hostlane3': 'N/A',
                                                        'active_apsel_hostlane4': 'N/A',
                                                        'active_apsel_hostlane5': 'N/A',
                                                        'active_apsel_hostlane6': 'N/A',
                                                        'active_apsel_hostlane7': 'N/A',
                                                        'active_apsel_hostlane8': 'N/A',
                                                        'host_lane_count': 'N/A',
                                                        'media_lane_count': 'N/A'}

        # case: full lanes update (reset to 'N/A')
        lport = "Ethernet32"
        host_lanes_mask = 0xff
        task.post_port_active_apsel_to_db(mock_xcvr_api, lport, host_lanes_mask, reset_apsel=True)
        assert int_tbl.getKeys() == ["Ethernet0", "Ethernet8", "Ethernet16", "Ethernet32"]
        assert dict(int_tbl.mock_dict["Ethernet32"]) == {'active_apsel_hostlane1': 'N/A',
                                                        'active_apsel_hostlane2': 'N/A',
                                                        'active_apsel_hostlane3': 'N/A',
                                                        'active_apsel_hostlane4': 'N/A',
                                                        'active_apsel_hostlane5': 'N/A',
                                                        'active_apsel_hostlane6': 'N/A',
                                                        'active_apsel_hostlane7': 'N/A',
                                                        'active_apsel_hostlane8': 'N/A',
                                                        'host_lane_count': 'N/A',
                                                        'media_lane_count': 'N/A'}

        # case: NotImplementedError
        int_tbl = Table("STATE_DB", TRANSCEIVER_INFO_TABLE)     # a new empty table
        lport = "Ethernet0"
        host_lanes_mask = 0xf
        ret = task.post_port_active_apsel_to_db(mock_xcvr_api, lport, host_lanes_mask)
        assert int_tbl.getKeys() == []

    @pytest.mark.parametrize(
        "expired_time, current_time, expected_result",
        [
            (None, datetime.datetime(2025, 3, 26, 12, 0, 0), False),  # Case 1: expired_time is None
            (datetime.datetime(2025, 3, 26, 12, 10, 0), datetime.datetime(2025, 3, 26, 12, 0, 0), False),  # Case 2: expired_time is in the future
            (datetime.datetime(2025, 3, 26, 11, 50, 0), datetime.datetime(2025, 3, 26, 12, 0, 0), True),  # Case 3: expired_time is in the past
            (datetime.datetime(2025, 3, 26, 12, 0, 0), datetime.datetime(2025, 3, 26, 12, 0, 0), True),  # Case 4: expired_time is exactly now
            (datetime.datetime(2025, 2, 26, 12, 0, 0), None, True),  # Case 5: current_time is None
        ],
    )
    def test_CmisManagerTask_test_is_timer_expired(self, expired_time, current_time, expected_result):
        port_mapping = PortMapping()
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=MagicMock())

        # Call the is_timer_expired function
        result = task.is_timer_expired(expired_time, current_time)

        # Assert the result matches the expected output
        assert result == expected_result

    @patch('xcvrd.xcvrd.XcvrTableHelper.get_status_sw_tbl')
    @patch('xcvrd.xcvrd.platform_chassis')
    @patch('xcvrd.xcvrd_utilities.common.is_fast_reboot_enabled', MagicMock(return_value=(False)))
    @patch('xcvrd.cmis.cmis_manager_task.PortChangeObserver', MagicMock(handle_port_update_event=MagicMock()))
    @patch('xcvrd.xcvrd._wrapper_get_sfp_type', MagicMock(return_value='QSFP_DD'))
    @patch('xcvrd.cmis.CmisManagerTask.wait_for_port_config_done', MagicMock())
    @patch('xcvrd.cmis.CmisManagerTask.is_decommission_required', MagicMock(return_value=False))
    @patch('xcvrd.xcvrd_utilities.common.is_cmis_api', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd_utilities.optics_si_parser.optics_si_present', MagicMock(return_value=(True)))
    @patch('xcvrd.xcvrd_utilities.optics_si_parser.fetch_optics_si_setting', MagicMock())
    def test_CmisManagerTask_task_worker(self, mock_chassis, mock_get_status_sw_tbl):
        mock_get_status_sw_tbl = Table("STATE_DB", TRANSCEIVER_STATUS_SW_TABLE)
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
        port_mapping.handle_port_change_event(PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_ADD))
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=mock_chassis)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.xcvr_table_helper.get_status_sw_tbl.return_value = mock_get_status_sw_tbl
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_UNKNOWN

        port_change_event = PortChangeEvent('PortConfigDone', -1, 0, PortChangeEvent.PORT_SET)
        task.on_port_update_event(port_change_event)
        assert task.isPortConfigDone

        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_SET,
                                            {'speed':'400000', 'lanes':'1,2,3,4,5,6,7,8'})
        task.on_port_update_event(port_change_event)
        assert len(task.port_dict) == 1
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_INSERTED

        task.get_host_tx_status = MagicMock(return_value='true')
        task.get_port_admin_status = MagicMock(return_value='up')
        task.get_configured_tx_power_from_db = MagicMock(return_value=-13)
        task.get_configured_laser_freq_from_db = MagicMock(return_value=193100)
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()

        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_DP_PRE_INIT_CHECK
        task.configure_tx_output_power = MagicMock(return_value=1)
        task.configure_laser_frequency = MagicMock(return_value=1)

        # Case 1: CMIS_STATE_DP_PRE_INIT_CHECK --> DP_DEINIT
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_DP_DEINIT
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert mock_xcvr_api.set_datapath_deinit.call_count == 1
        assert mock_xcvr_api.tx_disable_channel.call_count == 1
        assert mock_xcvr_api.set_lpmode.call_count == 1
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_AP_CONF

        # Case 2: DP_DEINIT --> AP Configured
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert mock_xcvr_api.set_application.call_count == 1
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_DP_INIT

        # Case 3: AP Configured --> DP_INIT
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert mock_xcvr_api.set_datapath_init.call_count == 1
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_DP_TXON

        # Case 4: DP_INIT --> DP_TXON
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert mock_xcvr_api.tx_disable_channel.call_count == 2
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_DP_ACTIVATE

        # Case 5: DP_TXON --> DP_ACTIVATION
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.post_port_active_apsel_to_db = MagicMock()
        task.task_worker()
        assert task.post_port_active_apsel_to_db.call_count == 1
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_READY

        # Fail test coverage - Module Inserted state failing to reach DP_DEINIT
        port_mapping = PortMapping()
        port_mapping.handle_port_change_event(PortChangeEvent('Ethernet1', 1, 0, PortChangeEvent.PORT_ADD))
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=mock_chassis)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.xcvr_table_helper.get_status_sw_tbl.return_value = mock_get_status_sw_tbl
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert common.get_cmis_state_from_state_db('Ethernet1', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet1'))) == CMIS_STATE_UNKNOWN

        port_change_event = PortChangeEvent('PortConfigDone', -1, 0, PortChangeEvent.PORT_SET)
        task.on_port_update_event(port_change_event)
        assert task.isPortConfigDone

        port_change_event = PortChangeEvent('Ethernet1', 1, 0, PortChangeEvent.PORT_SET,
                                            {'speed':'400000', 'lanes':'1,2,3,4,5,6,7,8'})
        task.on_port_update_event(port_change_event)
        assert len(task.port_dict) == 1
        assert common.get_cmis_state_from_state_db('Ethernet1', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet1'))) == CMIS_STATE_INSERTED

        task.get_host_tx_status = MagicMock(return_value='true')
        task.get_port_admin_status = MagicMock(return_value='up')
        task.get_configured_tx_power_from_db = MagicMock(return_value=-13)
        task.get_configured_laser_freq_from_db = MagicMock(return_value=193100)
        task.configure_tx_output_power = MagicMock(return_value=1)
        task.configure_laser_frequency = MagicMock(return_value=1)

    @patch('xcvrd.xcvrd.XcvrTableHelper.get_status_sw_tbl')
    @patch('xcvrd.xcvrd.platform_chassis')
    @patch('xcvrd.xcvrd_utilities.common.is_fast_reboot_enabled', MagicMock(return_value=(True)))
    @patch('xcvrd.cmis.cmis_manager_task.PortChangeObserver', MagicMock(handle_port_update_event=MagicMock()))
    @patch('xcvrd.xcvrd._wrapper_get_sfp_type', MagicMock(return_value='QSFP_DD'))
    @patch('xcvrd.cmis.CmisManagerTask.wait_for_port_config_done', MagicMock())
    @patch('xcvrd.cmis.CmisManagerTask.is_decommission_required', MagicMock(return_value=False))
    @patch('xcvrd.xcvrd_utilities.common.is_cmis_api', MagicMock(return_value=True))
    def test_CmisManagerTask_task_worker_fastboot(self, mock_chassis, mock_get_status_sw_tbl):
        mock_get_status_sw_tbl = Table("STATE_DB", TRANSCEIVER_STATUS_SW_TABLE)
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
        mock_xcvr_api.get_datapath_tx_turnoff_duration = MagicMock(return_value=500.0)
        mock_xcvr_api.get_datapath_init_duration = MagicMock(return_value=60000.0)
        mock_xcvr_api.get_module_pwr_up_duration = MagicMock(return_value=70000.0)
        mock_xcvr_api.get_datapath_deinit_duration = MagicMock(return_value=600000.0)
        mock_xcvr_api.get_cmis_rev = MagicMock(return_value='5.0')
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
        port_mapping.handle_port_change_event(PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_ADD))
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=mock_chassis)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.xcvr_table_helper.get_status_sw_tbl.return_value = mock_get_status_sw_tbl
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_UNKNOWN

        port_change_event = PortChangeEvent('PortConfigDone', -1, 0, PortChangeEvent.PORT_SET)
        task.on_port_update_event(port_change_event)
        assert task.isPortConfigDone

        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_SET,
                                            {'speed':'400000', 'lanes':'1,2,3,4,5,6,7,8'})
        task.on_port_update_event(port_change_event)
        assert len(task.port_dict) == 1
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_INSERTED

        task.get_host_tx_status = MagicMock(return_value='false')
        task.get_port_admin_status = MagicMock(return_value='up')
        task.get_configured_tx_power_from_db = MagicMock(return_value=-13)
        task.get_configured_laser_freq_from_db = MagicMock(return_value=193100)
        task.configure_tx_output_power = MagicMock(return_value=1)
        task.configure_laser_frequency = MagicMock(return_value=1)
        task.post_port_active_apsel_to_db = MagicMock()

        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()

        assert mock_xcvr_api.tx_disable_channel.call_count == 1
        assert task.post_port_active_apsel_to_db.call_count == 1
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_READY

    @patch('xcvrd.xcvrd.XcvrTableHelper.get_status_sw_tbl')
    @patch('xcvrd.xcvrd.platform_chassis')
    @patch('xcvrd.xcvrd_utilities.common.is_fast_reboot_enabled', MagicMock(return_value=(False)))
    @patch('xcvrd.cmis.cmis_manager_task.PortChangeObserver', MagicMock(handle_port_update_event=MagicMock()))
    @patch('xcvrd.xcvrd._wrapper_get_sfp_type', MagicMock(return_value='QSFP_DD'))
    @patch('xcvrd.cmis.CmisManagerTask.wait_for_port_config_done', MagicMock())
    @patch('xcvrd.cmis.CmisManagerTask.is_decommission_required', MagicMock(return_value=False))
    @patch('xcvrd.xcvrd_utilities.common.is_cmis_api', MagicMock(return_value=True))
    def test_CmisManagerTask_task_worker_host_tx_ready_false_to_true(self, mock_chassis, mock_get_status_sw_tbl):
        mock_get_status_sw_tbl = Table("STATE_DB", TRANSCEIVER_STATUS_TABLE)
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
        mock_xcvr_api.get_datapath_tx_turnoff_duration = MagicMock(return_value=500.0)
        mock_xcvr_api.get_datapath_init_duration = MagicMock(return_value=60000.0)
        mock_xcvr_api.get_module_pwr_up_duration = MagicMock(return_value=70000.0)
        mock_xcvr_api.get_datapath_deinit_duration = MagicMock(return_value=600000.0)
        mock_xcvr_api.get_cmis_rev = MagicMock(return_value='5.0')
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
                'DP1State': 'DataPathInitialized',
                'DP2State': 'DataPathInitialized',
                'DP3State': 'DataPathInitialized',
                'DP4State': 'DataPathInitialized',
                'DP5State': 'DataPathInitialized',
                'DP6State': 'DataPathInitialized',
                'DP7State': 'DataPathInitialized',
                'DP8State': 'DataPathInitialized'
            },
        ])
        mock_sfp = MagicMock()
        mock_sfp.get_presence = MagicMock(return_value=True)
        mock_sfp.get_xcvr_api = MagicMock(return_value=mock_xcvr_api)

        mock_chassis.get_all_sfps = MagicMock(return_value=[mock_sfp])
        mock_chassis.get_sfp = MagicMock(return_value=mock_sfp)

        port_mapping = PortMapping()
        port_mapping.handle_port_change_event(PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_ADD))
        stop_event = threading.Event()
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=mock_chassis)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.xcvr_table_helper.get_status_sw_tbl.return_value = mock_get_status_sw_tbl
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_UNKNOWN

        port_change_event = PortChangeEvent('PortConfigDone', -1, 0, PortChangeEvent.PORT_SET)
        task.on_port_update_event(port_change_event)
        assert task.isPortConfigDone

        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_SET,
                                            {'speed':'400000', 'lanes':'1,2,3,4,5,6,7,8'})
        task.on_port_update_event(port_change_event)
        assert len(task.port_dict) == 1
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_INSERTED

        task.get_host_tx_status = MagicMock(return_value='false')
        task.get_port_admin_status = MagicMock(return_value='up')
        task.get_configured_tx_power_from_db = MagicMock(return_value=-13)
        task.get_configured_laser_freq_from_db = MagicMock(return_value=193100)
        task.configure_tx_output_power = MagicMock(return_value=1)
        task.configure_laser_frequency = MagicMock(return_value=1)
        task.post_port_active_apsel_to_db = MagicMock()

        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()

        assert task.post_port_active_apsel_to_db.call_count == 1
        assert mock_xcvr_api.tx_disable_channel.call_count == 1
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_READY
        assert task.port_dict['Ethernet0']['forced_tx_disabled'] == True

        task.port_dict['Ethernet0']['host_tx_ready'] = 'true'
        task.force_cmis_reinit('Ethernet0', 0)
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_DP_PRE_INIT_CHECK

        # Failure scenario wherein DP state is still DataPathActivated in the first attempt post enabling host_tx_ready
        # This doesn't allow the CMIS state to proceed to DP_DEINIT
        task.is_timer_expired = MagicMock(return_value=(True))
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, False, False, True])
        task.task_worker()
        assert task.port_dict['Ethernet0']['cmis_retries'] == 1
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_DP_PRE_INIT_CHECK

        # Ensures that CMIS state is set to DP_DEINIT in the second attempt
        mock_sfp = MagicMock()
        mock_sfp.get_xcvr_api = MagicMock(return_value=mock_xcvr_api)
        mock_xcvr_api.is_coherent_module = MagicMock(return_value=False)
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()

        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_DP_DEINIT
        assert task.port_dict['Ethernet0']['forced_tx_disabled'] == False
        assert task.port_dict['Ethernet0']['cmis_retries'] == 1

    @patch('xcvrd.xcvrd.XcvrTableHelper.get_status_sw_tbl')
    @patch('xcvrd.xcvrd.platform_chassis')
    @patch('xcvrd.xcvrd_utilities.common.is_fast_reboot_enabled', MagicMock(return_value=(False)))
    @patch('xcvrd.cmis.cmis_manager_task.PortChangeObserver', MagicMock(handle_port_update_event=MagicMock()))
    @patch('xcvrd.xcvrd._wrapper_get_sfp_type', MagicMock(return_value='QSFP_DD'))
    @patch('xcvrd.cmis.CmisManagerTask.wait_for_port_config_done', MagicMock())
    @patch('xcvrd.xcvrd_utilities.common.is_cmis_api', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd_utilities.common.get_cmis_application_desired', MagicMock(return_value=1))
    def test_CmisManagerTask_task_worker_decommission(self, mock_chassis, mock_get_status_sw_tbl):
        mock_get_status_sw_tbl = Table("STATE_DB", TRANSCEIVER_STATUS_TABLE)
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
        mock_xcvr_api.get_dpinit_pending = MagicMock(return_value=gen_cmis_dpinit_pending_dict(True))
        mock_xcvr_api.get_module_state = MagicMock(return_value='ModuleReady')
        mock_xcvr_api.get_config_datapath_hostlane_status = MagicMock(return_value=gen_cmis_config_status_dict('ConfigSuccess'))
        mock_xcvr_api.get_datapath_state = MagicMock(return_value=gen_cmis_dp_state_dict('DataPathDeactivated'))
        mock_xcvr_api.get_active_apsel_hostlane.return_value = gen_cmis_active_app_sel_dict(1)
        mock_xcvr_api.get_application.return_value = 1

        stop_event = threading.Event()
        mock_sfp = MagicMock()
        mock_sfp.get_presence = MagicMock(return_value=True)
        mock_sfp.get_xcvr_api = MagicMock(return_value=mock_xcvr_api)
        mock_chassis.get_all_sfps = MagicMock(return_value=[mock_sfp])
        mock_chassis.get_sfp = MagicMock(return_value=mock_sfp)

        port_mapping = PortMapping()

        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=mock_chassis)
        task.is_decommission_required = MagicMock(side_effect=[True]*2 + [False]*10)
        task.xcvr_table_helper.get_status_sw_tbl.return_value = mock_get_status_sw_tbl
        task.get_host_tx_status = MagicMock(return_value='true')
        task.get_port_admin_status = MagicMock(return_value='up')
        task.get_configured_tx_power_from_db = MagicMock(return_value=-13)
        task.get_configured_laser_freq_from_db = MagicMock(return_value=193100)
        task.configure_tx_output_power = MagicMock(return_value=1)
        task.configure_laser_frequency = MagicMock(return_value=1)
        task.get_cmis_host_lanes_mask = MagicMock(return_value=1)
        task.get_cmis_media_lanes_mask = MagicMock(return_value=1)
        task.post_port_active_apsel_to_db = MagicMock()

        physical_port_idx = 0

        # ===== Test successful decommission case =====

        # Insert 1st subport event
        port_change_event = PortChangeEvent('Ethernet0', physical_port_idx, 0, PortChangeEvent.PORT_SET, {'speed':'100000', 'lanes':'1,2', 'subport': '1'})
        task.on_port_update_event(port_change_event)
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_INSERTED

        # 1st subport (as the lead) starting decommission state machine
        task.task_stopping_event.is_set = MagicMock(side_effect=[False]*2 + [True])
        task.task_worker()
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_DP_DEINIT
        assert task.is_decomm_lead_lport('Ethernet0')

        # Insert 2nd subport event
        port_change_event = PortChangeEvent('Ethernet2', physical_port_idx, 0, PortChangeEvent.PORT_SET, {'speed':'100000', 'lanes':'3,4', 'subport': '2'})
        task.on_port_update_event(port_change_event)
        task.task_stopping_event.is_set = MagicMock(side_effect=[False]*3 + [True])
        task.task_worker()
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_AP_CONF
        # 2nd subport should not start decommission state machine as 1st subport already started decommission for the entire physical port
        assert common.get_cmis_state_from_state_db('Ethernet2', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet2'))) == CMIS_STATE_INSERTED
        assert task.is_decomm_pending('Ethernet0')
        assert not task.is_decomm_failed('Ethernet0')

        task.task_stopping_event.is_set = MagicMock(side_effect=[False]*3 + [True])
        task.task_worker()
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_DP_INIT
        # 2nd subport is waiting for decommission to complete
        assert common.get_cmis_state_from_state_db('Ethernet2', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet2'))) == CMIS_STATE_INSERTED
        assert task.is_decomm_lead_lport('Ethernet0')

        task.task_stopping_event.is_set = MagicMock(side_effect=[False]*3 + [True])
        task.task_worker()
        # 1st subport completed decommission state machine and proceed to normal state machine, entire physical port is done on decommission
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_INSERTED
        # 2nd subport is unblocked from decommission and continue on normal state machine
        assert common.get_cmis_state_from_state_db('Ethernet2', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet2'))) == CMIS_STATE_DP_PRE_INIT_CHECK
        assert not task.is_decomm_lead_lport('Ethernet0')
        assert not task.is_decomm_failed('Ethernet0')
        assert not task.is_decomm_pending('Ethernet0')

        # Eventually both subports should reach ready state
        task.task_stopping_event.is_set = MagicMock(side_effect=[False]*30 + [True]*2)
        task.task_worker()
        mock_xcvr_api.get_datapath_state = MagicMock(return_value=gen_cmis_dp_state_dict('DataPathInitialized'))
        task.task_stopping_event.is_set = MagicMock(side_effect=[False]*10 + [True]*2)
        task.task_worker()
        mock_xcvr_api.get_datapath_state = MagicMock(return_value=gen_cmis_dp_state_dict('DataPathActivated'))
        task.task_stopping_event.is_set = MagicMock(side_effect=[False]*10 + [True]*2)
        task.task_worker()
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_READY
        assert common.get_cmis_state_from_state_db('Ethernet2', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet2'))) == CMIS_STATE_READY

        # Delete the config for all subports
        port_change_event = PortChangeEvent('Ethernet0', physical_port_idx, 0, PortChangeEvent.PORT_DEL, {}, db_name='CONFIG_DB', table_name='PORT')
        task.on_port_update_event(port_change_event)
        port_change_event = PortChangeEvent('Ethernet2', physical_port_idx, 0, PortChangeEvent.PORT_DEL, {}, db_name='CONFIG_DB', table_name='PORT')
        task.on_port_update_event(port_change_event)
        assert not task.port_dict

        # Reset is_decommission_required() to start decommission from scratch
        task.is_decommission_required = MagicMock(side_effect=[True]*2 + [False]*10)

        # ===== Test failed decommission case =====

        # Force config status check to failed
        mock_xcvr_api.get_config_datapath_hostlane_status.return_value = gen_cmis_config_status_dict('ConfigRejected')
        mock_xcvr_api.get_datapath_state = MagicMock(return_value=gen_cmis_dp_state_dict('DataPathDeactivated'))
        task.is_timer_expired = MagicMock(return_value=True)

        # Insert 1st subport event
        port_change_event = PortChangeEvent('Ethernet0', physical_port_idx, 0, PortChangeEvent.PORT_SET, {'speed':'100000', 'lanes':'1,2', 'subport': '1'})
        task.on_port_update_event(port_change_event)
        # Make sure to give enough iterations so that task worker runs to the end
        task.task_stopping_event.is_set = MagicMock(side_effect=[False]*50 + [True]*2)
        task.task_worker()
        assert task.is_decomm_lead_lport('Ethernet0')
        # 1st subport should fail on 'ConfigSuccess' check and fall into failed state after retries
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_FAILED
        assert task.is_decomm_failed('Ethernet0')

        # Insert 2nd subport event
        port_change_event = PortChangeEvent('Ethernet2', physical_port_idx, 0, PortChangeEvent.PORT_SET, {'speed':'100000', 'lanes':'3,4', 'subport': '2'})
        task.on_port_update_event(port_change_event)
        task.task_stopping_event.is_set = MagicMock(side_effect=[False]*10 + [True]*2)
        task.task_worker()
        # 1st subport should stay in failed state
        assert common.get_cmis_state_from_state_db('Ethernet0', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet0'))) == CMIS_STATE_FAILED
        assert task.is_decomm_failed('Ethernet0')
        assert task.is_decomm_lead_lport('Ethernet0')
        # 2nd subport is waiting for decommission to complete, and should also fall into failed state
        assert common.get_cmis_state_from_state_db('Ethernet2', task.xcvr_table_helper.get_status_sw_tbl(task.get_asic_id('Ethernet2'))) == CMIS_STATE_FAILED
        assert task.is_decomm_pending('Ethernet2')
        assert task.is_decomm_failed('Ethernet2')

        # Delete the config for 1st subport
        port_change_event = PortChangeEvent('Ethernet0', physical_port_idx, 0, PortChangeEvent.PORT_DEL, {}, db_name='CONFIG_DB', table_name='PORT')
        task.on_port_update_event(port_change_event)
        # 1st subport is removed from port_dict
        assert 'Ethernet0' not in task.port_dict
        assert len(task.port_dict) == 1
        # physical port should also be removed from decomm_pending_dict
        assert physical_port_idx not in task.decomm_pending_dict
        assert not task.is_decomm_pending('Ethernet2')

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
        mock_sfp_obj_dict = MagicMock()
        stop_event = threading.Event()
        mock_cmis_manager = MagicMock()
        task = DomInfoUpdateTask(DEFAULT_NAMESPACE, port_mapping, mock_sfp_obj_dict, stop_event, mock_cmis_manager)
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
    @patch('xcvrd.xcvrd_utilities.common.get_cmis_state_from_state_db')
    def test_DomInfoUpdateTask_is_port_in_cmis_initialization_process(self, mock_get_cmis_state_from_state_db, skip_cmis_manager, is_asic_index_none, mock_cmis_state, expected_result):
        port_mapping = PortMapping()
        lport = 'Ethernet0'
        port_change_event = PortChangeEvent(lport, 1, 0, PortChangeEvent.PORT_ADD)
        mock_sfp_obj_dict = MagicMock()
        stop_event = threading.Event()
        task = DomInfoUpdateTask(DEFAULT_NAMESPACE, port_mapping, mock_sfp_obj_dict, stop_event, skip_cmis_manager)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.on_port_config_change(port_change_event)
        mock_get_cmis_state_from_state_db.return_value = mock_cmis_state
        if is_asic_index_none:
            lport='INVALID_PORT'
        assert task.is_port_in_cmis_initialization_process(lport) == expected_result

    def test_beautify_dom_info_dict(self):
        dom_info_dict = {
            'temperature': '0C',
            'eSNR' : 1.1,
        }
        expected_dom_info_dict = {
            'temperature': '0',
            'eSNR' : '1.1',
        }
        mock_sfp_obj_dict = MagicMock()
        port_mapping = PortMapping()
        xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        stop_event = threading.Event()
        mock_logger = MagicMock()
        dom_db_utils = DOMDBUtils(mock_sfp_obj_dict, port_mapping, xcvr_table_helper, stop_event, mock_logger)

        dom_db_utils._beautify_dom_info_dict(dom_info_dict)
        assert dom_info_dict == expected_dom_info_dict

        # Ensure that the method handles None input gracefully and logs a warning
        dom_db_utils._beautify_dom_info_dict(None)
        mock_logger.log_warning.assert_called_once_with("DOM info dict is None while beautifying")

    def test_beautify_info_dict(self):
        dom_info_dict = {
            'eSNR' : 1.1,
        }
        expected_dom_info_dict = {
            'eSNR' : '1.1',
        }
        mock_sfp_obj_dict = MagicMock()
        port_mapping = PortMapping()
        stop_event = threading.Event()
        db_utils = DBUtils(mock_sfp_obj_dict, port_mapping, stop_event, helper_logger)

        db_utils.beautify_info_dict(dom_info_dict)
        assert dom_info_dict == expected_dom_info_dict

    @patch('xcvrd.xcvrd.XcvrTableHelper', MagicMock())
    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd_utilities.sfp_status_helper.detect_port_in_error_status')
    @patch('time.sleep', MagicMock())
    def test_DomThermalInfoUpdateTask_task_worker(self, mock_detect_error):
        poll_interval = 10
        port_mapping = PortMapping()
        port_mapping.physical_to_logical = {
            1: ['Ethernet0'],
            2: ['Ethernet4'],
            3: ['Ethernet8'],
        }
        port_mapping.logical_to_asic = {
            'Ethernet0': 0,
            'Ethernet4': 0,
            'Ethernet8': None,
        }
        dom_monitoring_disabled = {
            'Ethernet0': False,
            'Ethernet4': True,
            'Ethernet8': False,
        }
        mock_sfp_obj_dict = MagicMock()
        stop_event = threading.Event()
        task = DomThermalInfoUpdateTask(DEFAULT_NAMESPACE, port_mapping, mock_sfp_obj_dict, stop_event, poll_interval)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, False, False, False, False, True])
        mock_detect_error.return_value = False
        task.is_port_dom_monitoring_disabled = lambda p: dom_monitoring_disabled[p]
        task.task_worker()

    @patch('xcvrd.xcvrd.XcvrTableHelper', MagicMock())
    @patch('xcvrd.xcvrd_utilities.common.del_port_sfp_dom_info_from_db')
    def test_DomInfoUpdateTask_handle_port_change_event(self, mock_del_port_sfp_dom_info_from_db):
        port_mapping = PortMapping()
        mock_sfp_obj_dict = MagicMock()
        stop_event = threading.Event()
        mock_cmis_manager = MagicMock()
        task = DomInfoUpdateTask(DEFAULT_NAMESPACE, port_mapping, mock_sfp_obj_dict, stop_event, mock_cmis_manager)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_ADD)
        task.on_port_config_change(port_change_event)
        assert task.port_mapping.logical_port_list.count('Ethernet0')
        assert task.port_mapping.get_asic_id_for_logical_port('Ethernet0') == 0
        assert task.port_mapping.get_physical_to_logical(1) == ['Ethernet0']
        assert task.port_mapping.get_logical_to_physical('Ethernet0') == [1]
        assert mock_del_port_sfp_dom_info_from_db.call_count == 0

        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_REMOVE)
        task.on_port_config_change(port_change_event)
        assert not task.port_mapping.logical_port_list
        assert not task.port_mapping.logical_to_physical
        assert not task.port_mapping.physical_to_logical
        assert not task.port_mapping.logical_to_asic
        assert mock_del_port_sfp_dom_info_from_db.call_count == 1

    @patch('xcvrd.xcvrd_utilities.port_event_helper.subscribe_port_config_change', MagicMock(return_value=(None, None)))
    @patch('xcvrd.xcvrd_utilities.port_event_helper.handle_port_config_change', MagicMock())
    def test_DomInfoUpdateTask_task_run_stop(self):
        port_mapping = PortMapping()
        mock_sfp_obj_dict = MagicMock()
        stop_event = threading.Event()
        mock_cmis_manager = MagicMock()
        task = DomInfoUpdateTask(DEFAULT_NAMESPACE, port_mapping, mock_sfp_obj_dict, stop_event, mock_cmis_manager)
        task.task_stopping_event.is_set = MagicMock(return_value=True)
        task.start()
        task.join()
        assert not task.is_alive()

    @patch('xcvrd.xcvrd.XcvrTableHelper', MagicMock())
    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd_utilities.port_event_helper.PortChangeObserver', MagicMock(handle_port_update_event=MagicMock()))
    @patch('xcvrd.xcvrd_utilities.sfp_status_helper.detect_port_in_error_status')
    @patch('xcvrd.dom.dom_mgr.DomInfoUpdateTask.post_port_sfp_firmware_info_to_db')
    @patch('swsscommon.swsscommon.Select.addSelectable', MagicMock())
    @patch('swsscommon.swsscommon.SubscriberStateTable')
    @patch('swsscommon.swsscommon.Select.select')
    @patch('xcvrd.dom.dom_mgr.DomInfoUpdateTask.post_port_pm_info_to_db')
    def test_DomInfoUpdateTask_task_worker(self, mock_post_pm_info,
                                           mock_select, mock_sub_table,
                                           mock_post_firmware_info, mock_detect_error):
        mock_selectable = MagicMock()
        mock_selectable.pop = MagicMock(
            side_effect=[('Ethernet0', swsscommon.SET_COMMAND, (('index', '1'), )), (None, None, None), (None, None, None), (None, None, None)])
        mock_select.return_value = (swsscommon.Select.OBJECT, mock_selectable)
        mock_sub_table.return_value = mock_selectable

        port_mapping = PortMapping()
        mock_sfp_obj_dict = MagicMock()
        stop_event = threading.Event()
        mock_cmis_manager = MagicMock()
        task = DomInfoUpdateTask(DEFAULT_NAMESPACE, port_mapping, mock_sfp_obj_dict, stop_event, mock_cmis_manager)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, False, False, False, False, True])
        task.get_dom_polling_from_config_db = MagicMock(return_value='enabled')
        task.is_port_in_cmis_terminal_state = MagicMock(return_value=False)
        mock_detect_error.return_value = True
        task.DOM_INFO_UPDATE_PERIOD_SECS = 0
        task.dom_db_utils = MagicMock()
        task.dom_db_utils.post_port_dom_sensor_info_to_db = MagicMock()
        task.dom_db_utils.post_port_dom_flags_to_db.return_value = MagicMock()
        task.status_db_utils = MagicMock()
        task.status_db_utils.post_port_transceiver_hw_status_to_db = MagicMock()
        task.status_db_utils.post_port_transceiver_hw_status_flags_to_db = MagicMock()
        task.vdm_utils.is_transceiver_vdm_supported = MagicMock(return_value=True)
        task.xcvrd_utils.is_transceiver_lpmode_on = MagicMock(return_value=False)
        task.vdm_db_utils = MagicMock()
        task.vdm_db_utils.post_port_vdm_real_values_to_db = MagicMock()
        task.task_worker()
        assert task.port_mapping.logical_port_list.count('Ethernet0')
        assert task.port_mapping.get_asic_id_for_logical_port('Ethernet0') == 0
        assert task.port_mapping.get_physical_to_logical(1) == ['Ethernet0']
        assert task.port_mapping.get_logical_to_physical('Ethernet0') == [1]
        assert mock_post_firmware_info.call_count == 0
        assert task.dom_db_utils.post_port_dom_sensor_info_to_db.call_count == 0
        assert task.dom_db_utils.post_port_dom_flags_to_db.call_count == 0
        assert task.status_db_utils.post_port_transceiver_hw_status_to_db.call_count == 0
        assert task.status_db_utils.post_port_transceiver_hw_status_flags_to_db.call_count == 0
        assert task.vdm_db_utils.post_port_vdm_real_values_to_db.call_count == 0
        assert task.vdm_db_utils.post_port_vdm_flags_to_db.call_count == 0
        assert mock_post_pm_info.call_count == 0
        mock_detect_error.return_value = False
        mock_select.return_value = (swsscommon.Select.TIMEOUT, None)
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, False, False, False, False, True])
        task.port_mapping.physical_to_logical = {'1': ['Ethernet0']}
        task.port_mapping.get_asic_id_for_logical_port = MagicMock(return_value=0)
        task.get_dom_polling_from_config_db = MagicMock(side_effect=('disabled', 'enabled'))
        task.task_worker()
        assert mock_post_firmware_info.call_count == 1
        assert task.dom_db_utils.post_port_dom_sensor_info_to_db.call_count == 1
        assert task.dom_db_utils.post_port_dom_flags_to_db.call_count == 1
        assert task.status_db_utils.post_port_transceiver_hw_status_to_db.call_count == 1
        assert task.status_db_utils.post_port_transceiver_hw_status_flags_to_db.call_count == 1
        assert task.vdm_db_utils.post_port_vdm_real_values_to_db.call_count == 1
        assert task.vdm_db_utils.post_port_vdm_flags_to_db.call_count == 1
        assert mock_post_pm_info.call_count == 1

    @patch('xcvrd.xcvrd.XcvrTableHelper', MagicMock())
    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd_utilities.sfp_status_helper.detect_port_in_error_status', MagicMock(return_value=False))
    @patch('xcvrd.dom.dom_mgr.DomInfoUpdateTask.post_port_sfp_firmware_info_to_db', MagicMock(return_value=True))
    @patch('swsscommon.swsscommon.Select.addSelectable', MagicMock())
    @patch('xcvrd.xcvrd_utilities.port_event_helper.PortChangeObserver', MagicMock(handle_port_update_event=MagicMock()))
    @patch('xcvrd.xcvrd_utilities.port_event_helper.subscribe_port_config_change', MagicMock(return_value=(None, None)))
    @patch('xcvrd.xcvrd_utilities.port_event_helper.handle_port_config_change', MagicMock())
    @patch('xcvrd.dom.dom_mgr.DomInfoUpdateTask.post_port_pm_info_to_db')
    def test_DomInfoUpdateTask_task_worker_vdm_failure(self, mock_post_pm_info):
        port_mapping = PortMapping()
        mock_sfp_obj_dict = MagicMock()
        stop_event = threading.Event()
        mock_cmis_manager = MagicMock()
        task = DomInfoUpdateTask(DEFAULT_NAMESPACE, port_mapping, mock_sfp_obj_dict, stop_event, mock_cmis_manager)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.DOM_INFO_UPDATE_PERIOD_SECS = 0
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.port_mapping.logical_port_list = ['Ethernet0']
        task.port_mapping.physical_to_logical = {'1': ['Ethernet0']}
        task.port_mapping.get_asic_id_for_logical_port = MagicMock(return_value=0)
        task.get_dom_polling_from_config_db = MagicMock(return_value='enabled')
        task.is_port_in_cmis_terminal_state = MagicMock(return_value=False)
        task.dom_db_utils = MagicMock()
        task.dom_db_utils.post_port_dom_sensor_info_to_db = MagicMock()
        task.dom_db_utils.post_port_dom_flags_to_db.return_value = MagicMock()
        task.status_db_utils = MagicMock()
        task.status_db_utils.post_port_transceiver_hw_status_to_db = MagicMock()
        task.status_db_utils.post_port_transceiver_hw_status_flags_to_db = MagicMock()
        task.vdm_utils.is_transceiver_vdm_supported = MagicMock(return_value=True)
        task.vdm_utils._freeze_vdm_stats_and_confirm = MagicMock(return_value=False)
        task.vdm_utils._unfreeze_vdm_stats_and_confirm = MagicMock(return_value=True)
        task.vdm_db_utils.post_port_vdm_real_values_to_db = MagicMock()
        task.vdm_db_utils.post_port_vdm_flags_to_db = MagicMock()
        task.xcvrd_utils.is_transceiver_lpmode_on = MagicMock(return_value=False)
        task.task_worker()
        assert task.vdm_utils._unfreeze_vdm_stats_and_confirm.call_count == 1
        assert task.vdm_db_utils.post_port_vdm_real_values_to_db.call_count == 0
        assert task.vdm_db_utils.post_port_vdm_flags_to_db.call_count == 0
        assert mock_post_pm_info.call_count == 0

        # clear the call count
        task.vdm_utils._freeze_vdm_stats_and_confirm.reset_mock()
        task.vdm_utils._unfreeze_vdm_stats_and_confirm.reset_mock()
        task.vdm_db_utils.post_port_vdm_real_values_to_db.reset_mock()
        task.vdm_db_utils.post_port_vdm_flags_to_db.reset_mock()
        mock_post_pm_info.reset_mock()

        # Test the case where the VDM stats are successfully frozen but the VDM stats are not successfully unfrozen
        task.vdm_utils._freeze_vdm_stats_and_confirm.return_value = True
        task.vdm_utils._unfreeze_vdm_stats_and_confirm.return_value = False
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert task.vdm_utils._freeze_vdm_stats_and_confirm.call_count == 1
        assert task.vdm_utils._unfreeze_vdm_stats_and_confirm.call_count == 1
        assert task.vdm_db_utils.post_port_vdm_real_values_to_db.call_count == 1
        assert task.vdm_db_utils.post_port_vdm_flags_to_db.call_count == 1
        assert mock_post_pm_info.call_count == 1

        # clear the call count
        task.vdm_utils._freeze_vdm_stats_and_confirm.reset_mock()
        task.vdm_utils._unfreeze_vdm_stats_and_confirm.reset_mock()
        task.vdm_db_utils.post_port_vdm_real_values_to_db.reset_mock()
        task.vdm_db_utils.post_port_vdm_flags_to_db.reset_mock()
        mock_post_pm_info.reset_mock()

        # mock_post_diagnostic_value raises an exception
        task.vdm_utils._unfreeze_vdm_stats_and_confirm.return_value = True
        task.vdm_db_utils.post_port_vdm_real_values_to_db.side_effect = TypeError
        task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
        task.task_worker()
        assert task.vdm_utils._freeze_vdm_stats_and_confirm.call_count == 1
        assert task.vdm_utils._unfreeze_vdm_stats_and_confirm.call_count == 1
        assert task.vdm_db_utils.post_port_vdm_real_values_to_db.call_count == 1
        assert task.vdm_db_utils.post_port_vdm_flags_to_db.call_count == 0
        assert mock_post_pm_info.call_count == 0

    @pytest.mark.parametrize(
        "physical_port, logical_port_list, asic_index, transceiver_presence, port_in_error_status, vdm_supported, expected_logs",
        [
            # Case 1: Valid port, all updates succeed
            (1, ["Ethernet0"], 0, True, False, True, []),

            # Case 2: Invalid physical port (logical_port_list is None)
            (2, None, None, False, False, False, ["Update DB diagnostics during link change: Unknown physical port index 2"]),

            # Case 3: Invalid ASIC index
            (3, ["Ethernet1"], None, False, False, False, ["Update DB diagnostics during link change: Got invalid asic index for Ethernet1, ignored"]),

            # Case 4: Port in error status
            (4, ["Ethernet2"], 1, True, True, False, []),

            # Case 5: Transceiver not present
            (5, ["Ethernet3"], 1, False, False, False, []),

            # Case 6: VDM not supported
            (6, ["Ethernet4"], 1, True, False, False, []),
        ],
    )
    def test_update_port_db_diagnostics_on_link_change(
        self,
        physical_port,
        logical_port_list,
        asic_index,
        transceiver_presence,
        port_in_error_status,
        vdm_supported,
        expected_logs,
    ):
        port_mapping = PortMapping()
        mock_sfp_obj_dict = MagicMock()
        stop_event = threading.Event()
        mock_cmis_manager = MagicMock()
        task = DomInfoUpdateTask(DEFAULT_NAMESPACE, port_mapping, mock_sfp_obj_dict, stop_event, mock_cmis_manager)

        # Mock dependencies
        task.task_stopping_event.is_set = MagicMock(return_value=False)
        task.port_mapping.get_physical_to_logical = MagicMock(return_value=logical_port_list)
        task.port_mapping.get_asic_id_for_logical_port = MagicMock(return_value=asic_index)
        task.xcvrd_utils.get_transceiver_presence = MagicMock(return_value=transceiver_presence)
        task.is_port_dom_monitoring_disabled = MagicMock(return_value=False)
        task.vdm_utils.is_transceiver_vdm_supported = MagicMock(return_value=vdm_supported)
        task.xcvr_table_helper.get_status_sw_tbl = MagicMock()
        task.dom_db_utils.post_port_dom_flags_to_db = MagicMock()
        task.status_db_utils.post_port_transceiver_hw_status_flags_to_db = MagicMock()
        task.vdm_db_utils.post_port_vdm_flags_to_db = MagicMock()
        task.log_warning = MagicMock()

        # Mock sfp_status_helper
        with patch("xcvrd.xcvrd_utilities.sfp_status_helper.detect_port_in_error_status", return_value=port_in_error_status):
            # Call the function
            task.update_port_db_diagnostics_on_link_change(physical_port)

        # Verify logs
        for log in expected_logs:
            task.log_warning.assert_any_call(log)

        # Verify function calls
        if asic_index and transceiver_presence and logical_port_list and not port_in_error_status:
            assert task.dom_db_utils.post_port_dom_flags_to_db.call_count == 1
            assert task.status_db_utils.post_port_transceiver_hw_status_flags_to_db.call_count == 1
            if vdm_supported:
                assert task.vdm_db_utils.post_port_vdm_flags_to_db.call_count == 1

    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence', MagicMock(return_value=False))
    @patch('xcvrd.xcvrd.XcvrTableHelper')
    @patch('xcvrd.xcvrd_utilities.common.del_port_sfp_dom_info_from_db')
    def test_SfpStateUpdateTask_handle_port_change_event(self, mock_del_port_sfp_dom_info_from_db, mock_table_helper):
        mock_table = MagicMock()
        mock_table.get = MagicMock(return_value=(False, None))
        mock_table_helper.get_status_tbl = MagicMock(return_value=mock_table)
        mock_table_helper.get_int_tbl = MagicMock(return_value=mock_table)
        mock_table_helper.get_dom_tbl = MagicMock(return_value=mock_table)
        mock_table_helper.get_dom_threshold_tbl = MagicMock(return_value=mock_table)
        mock_table_helper.get_state_port_tbl = MagicMock(return_value=mock_table)
        stop_event = threading.Event()
        sfp_error_event = threading.Event()
        port_mapping = PortMapping()
        mock_sfp_obj_dict = MagicMock()
        task = SfpStateUpdateTask(DEFAULT_NAMESPACE, port_mapping, mock_sfp_obj_dict, stop_event, sfp_error_event)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.xcvr_table_helper.get_status_tbl = mock_table_helper.get_status_tbl
        task.xcvr_table_helper.get_intf_tbl = mock_table_helper.get_intf_tbl
        task.xcvr_table_helper.get_dom_tbl = mock_table_helper.get_dom_tbl
        task.xcvr_table_helper.get_state_port_tbl = mock_table_helper.get_state_port_tbl
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
        assert mock_del_port_sfp_dom_info_from_db.call_count == 0

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
        assert mock_del_port_sfp_dom_info_from_db.call_count == 1

    def test_SfpStateUpdateTask_task_run_stop(self):
        def poll_forever(*args, **kwargs):
            while True:
                time.sleep(1)
        # Redefine the SfpStateUpdateTask.init function to poll forever so that the task can be stopped by
        # raising an exception in between. Also, XcvrTableHelper is the first function to be called after
        # starting the task, so having the patch here will avoid the task crashing unexpectedly
        # at a different location.
        with patch('xcvrd.xcvrd.SfpStateUpdateTask.init', new=poll_forever):
            port_mapping = PortMapping()
            mock_sfp_obj_dict = MagicMock()
            stop_event = threading.Event()
            sfp_error_event = threading.Event()
            task = SfpStateUpdateTask(DEFAULT_NAMESPACE, port_mapping, mock_sfp_obj_dict, stop_event, sfp_error_event)
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
        mock_sfp_obj_dict = MagicMock()
        stop_event = threading.Event()
        sfp_error_event = threading.Event()
        task = SfpStateUpdateTask(DEFAULT_NAMESPACE, port_mapping, mock_sfp_obj_dict, stop_event, sfp_error_event)
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
        mock_sfp_obj_dict = MagicMock()
        stop_event = threading.Event()
        sfp_error_event = threading.Event()
        task = SfpStateUpdateTask(DEFAULT_NAMESPACE, port_mapping, mock_sfp_obj_dict, stop_event, sfp_error_event)
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
    @patch('xcvrd.xcvrd_utilities.common.del_port_sfp_dom_info_from_db')
    @patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting')
    @patch('xcvrd.dom.dom_mgr.DomInfoUpdateTask.post_port_sfp_firmware_info_to_db')
    @patch('xcvrd.xcvrd.post_port_sfp_info_to_db')
    @patch('xcvrd.xcvrd_utilities.common.update_port_transceiver_status_table_sw')
    def test_SfpStateUpdateTask_task_worker(self, mock_update_status, mock_post_sfp_info,
                                            mock_post_firmware_info, mock_update_media_setting,
                                            mock_del_dom, mock_change_event, mock_mapping_event, mock_os_kill):
        port_mapping = PortMapping()
        mock_sfp_obj_dict = MagicMock()
        stop_event = threading.Event()
        sfp_error_event = threading.Event()
        task = SfpStateUpdateTask(DEFAULT_NAMESPACE, port_mapping, mock_sfp_obj_dict, stop_event, sfp_error_event)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.dom_db_utils.post_port_dom_thresholds_to_db = MagicMock()
        task.vdm_db_utils.post_port_vdm_thresholds_to_db = MagicMock()
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
        assert task.dom_db_utils.post_port_dom_thresholds_to_db.call_count == 0
        assert task.vdm_db_utils.post_port_vdm_thresholds_to_db.call_count == 0
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
        assert task.dom_db_utils.post_port_dom_thresholds_to_db.call_count == 1
        assert task.vdm_db_utils.post_port_vdm_thresholds_to_db.call_count == 1
        assert mock_post_firmware_info.call_count == 0
        assert mock_update_media_setting.call_count == 1

        stop_event.is_set = MagicMock(side_effect=[False, True])
        mock_change_event.return_value = (True, {1: SFP_STATUS_REMOVED}, {})
        mock_update_status.reset_mock()
        # Test state machine: handle SFP remove event
        task.task_worker(stop_event, sfp_error_event)
        assert mock_update_status.call_count == 1
        assert mock_del_dom.call_count == 1

        stop_event.is_set = MagicMock(side_effect=[False, True])
        error = int(SFP_STATUS_INSERTED) | SfpBase.SFP_ERROR_BIT_BLOCKING | SfpBase.SFP_ERROR_BIT_POWER_BUDGET_EXCEEDED
        mock_change_event.return_value = (True, {1: error}, {})
        mock_update_status.reset_mock()
        mock_del_dom.reset_mock()
        # Test state machine: handle SFP error event
        task.task_worker(stop_event, sfp_error_event)
        assert mock_update_status.call_count == 1
        assert mock_del_dom.call_count == 1

    @patch('xcvrd.xcvrd.XcvrTableHelper')
    @patch('xcvrd.xcvrd_utilities.common._wrapper_get_presence')
    @patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting')
    @patch('xcvrd.xcvrd.post_port_sfp_info_to_db')
    @patch('xcvrd.xcvrd_utilities.common.update_port_transceiver_status_table_sw')
    def test_SfpStateUpdateTask_on_add_logical_port(self, mock_update_status, mock_post_sfp_info,
            mock_update_media_setting, mock_get_presence, mock_table_helper):
        class MockTable:
            pass

        status_sw_tbl = MockTable()
        status_sw_tbl.get = MagicMock(return_value=(True, (('status', SFP_STATUS_INSERTED),)))
        status_sw_tbl.set = MagicMock()
        int_tbl = MockTable()
        int_tbl.get = MagicMock(return_value=(True, (('key2', 'value2'),)))
        int_tbl.set = MagicMock()
        dom_threshold_tbl = MockTable()
        dom_threshold_tbl.get = MagicMock(return_value=(True, (('key4', 'value4'),)))
        dom_threshold_tbl.set = MagicMock()
        state_port_tbl = MockTable()
        state_port_tbl.get = MagicMock(return_value=(True, (('key5', 'value5'),)))
        state_port_tbl.set = MagicMock()
        mock_table_helper.get_status_sw_tbl = MagicMock(return_value=status_sw_tbl)
        mock_table_helper.get_intf_tbl = MagicMock(return_value=int_tbl)
        mock_table_helper.get_dom_threshold_tbl = MagicMock(return_value=dom_threshold_tbl)
        mock_table_helper.get_state_port_tbl = MagicMock(return_value=state_port_tbl)

        port_mapping = PortMapping()
        mock_sfp_obj_dict = MagicMock()
        stop_event = threading.Event()
        sfp_error_event = threading.Event()
        task = SfpStateUpdateTask(DEFAULT_NAMESPACE, port_mapping, mock_sfp_obj_dict, stop_event, sfp_error_event)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.xcvr_table_helper.get_status_sw_tbl = mock_table_helper.get_status_sw_tbl
        task.xcvr_table_helper.get_intf_tbl = mock_table_helper.get_intf_tbl
        task.xcvr_table_helper.get_dom_threshold_tbl = mock_table_helper.get_dom_threshold_tbl
        task.xcvr_table_helper.get_state_port_tbl = mock_table_helper.get_state_port_tbl
        task.dom_db_utils.post_port_dom_thresholds_to_db = MagicMock()
        task.vdm_db_utils.post_port_vdm_thresholds_to_db = MagicMock()
        port_change_event = PortChangeEvent('Ethernet0', 1, 0, PortChangeEvent.PORT_ADD)
        task.port_mapping.handle_port_change_event(port_change_event)

        status_sw_tbl.get.return_value = (False, ())
        mock_get_presence.return_value = True
        mock_post_sfp_info.return_value = SFP_EEPROM_NOT_READY
        # SFP information is not in the DB, and SFP is present, and SFP has no error, but SFP EEPROM reading failed
        task.on_add_logical_port(port_change_event)
        assert mock_update_status.call_count == 1
        mock_update_status.assert_called_with('Ethernet0', status_sw_tbl, SFP_STATUS_INSERTED, 'N/A')
        assert mock_post_sfp_info.call_count == 1
        mock_post_sfp_info.assert_called_with('Ethernet0', task.port_mapping, int_tbl, {})
        assert task.dom_db_utils.post_port_dom_thresholds_to_db.call_count == 0
        assert task.vdm_db_utils.post_port_vdm_thresholds_to_db.call_count == 0
        assert mock_update_media_setting.call_count == 0
        assert 'Ethernet0' in task.retry_eeprom_set
        task.retry_eeprom_set.clear()

        mock_post_sfp_info.return_value = None
        mock_update_status.reset_mock()
        mock_post_sfp_info.reset_mock()
        # SFP information is not in the DB, and SFP is present, and SFP has no error, and SFP EEPROM reading succeed
        task.on_add_logical_port(port_change_event)
        assert mock_update_status.call_count == 1
        mock_update_status.assert_called_with('Ethernet0', status_sw_tbl, SFP_STATUS_INSERTED, 'N/A')
        assert mock_post_sfp_info.call_count == 1
        mock_post_sfp_info.assert_called_with('Ethernet0', task.port_mapping, int_tbl, {})
        assert task.dom_db_utils.post_port_dom_thresholds_to_db.call_count == 1
        assert task.vdm_db_utils.post_port_vdm_thresholds_to_db.call_count == 1
        task.dom_db_utils.post_port_dom_thresholds_to_db.assert_called_with('Ethernet0')
        task.vdm_db_utils.post_port_vdm_thresholds_to_db.assert_called_with('Ethernet0')
        assert mock_update_media_setting.call_count == 1
        assert 'Ethernet0' not in task.retry_eeprom_set

        mock_get_presence.return_value = False
        mock_update_status.reset_mock()
        # SFP information is not in DB and SFP is not present
        task.on_add_logical_port(port_change_event)
        assert mock_update_status.call_count == 1
        mock_update_status.assert_called_with('Ethernet0', status_sw_tbl, SFP_STATUS_REMOVED, 'N/A')

        task.sfp_error_dict[1] = (str(SfpBase.SFP_ERROR_BIT_BLOCKING | SfpBase.SFP_ERROR_BIT_POWER_BUDGET_EXCEEDED), {})
        mock_update_status.reset_mock()
        # SFP information is not in DB, and SFP is not present, and SFP is in error status
        task.on_add_logical_port(port_change_event)
        assert mock_update_status.call_count == 1
        mock_update_status.assert_called_with(
            'Ethernet0', status_sw_tbl, task.sfp_error_dict[1][0], 'Blocking EEPROM from being read|Power budget exceeded')

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

    @patch('xcvrd.xcvrd_utilities.common.platform_chassis')
    @patch('xcvrd.xcvrd_utilities.common.platform_sfputil')
    def test_wrapper_get_presence(self, mock_sfputil, mock_chassis):
        mock_object = MagicMock()
        mock_object.get_presence = MagicMock(return_value=True)
        mock_chassis.get_sfp = MagicMock(return_value=mock_object)
        from xcvrd.xcvrd_utilities.common import _wrapper_get_presence
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

        mock_chassis.get_sfp = MagicMock(side_effect=Exception)
        assert not _wrapper_get_transceiver_info(1)

    @pytest.mark.parametrize("mock_sfp, expected", [
        (MagicMock(is_transceiver_vdm_supported=MagicMock(side_effect=NotImplementedError)), False),
        (MagicMock(is_transceiver_vdm_supported=MagicMock(return_value=False)), False),
        (MagicMock(is_transceiver_vdm_supported=MagicMock(return_value=True)), True)
    ])
    def test_wrapper_is_transceiver_vdm_supported(self, mock_sfp, expected):
        mock_sfp_obj_dict = {1: mock_sfp}
        vdm_utils = VDMUtils(mock_sfp_obj_dict, helper_logger)

        result = vdm_utils.is_transceiver_vdm_supported(1)
        assert result == expected

    @pytest.mark.parametrize("action_return, status_return, time_side_effect, expected", [
        (True, True, [0, 0.1, 0.2, 0.3], True), # action completed successfully within timeout
        (True, False, [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0], False), # action completed successfully but status check failed until timeout
        (False, False, [], False), # action failed
    ])
    @patch('xcvrd.xcvrd.helper_logger')
    @patch('xcvrd.xcvrd.time.sleep', MagicMock())
    @patch('xcvrd.xcvrd.time.time')
    def test_vdm_action_and_confirm(self, mock_time, mock_logger,
                                    action_return, status_return, time_side_effect, expected):
        mock_sfp = MagicMock()
        mock_sfp.freeze_vdm_stats.return_value = action_return
        mock_sfp.get_vdm_freeze_status.return_value = status_return
        mock_sfp_obj_dict = {1: mock_sfp}
        vdm_utils = VDMUtils(mock_sfp_obj_dict, mock_logger)

        mock_time.side_effect = time_side_effect

        result = vdm_utils._vdm_action_and_confirm(1, mock_sfp.freeze_vdm_stats, mock_sfp.get_vdm_freeze_status, "freeze")
        assert result == expected

    def test_vdm_action_and_confirm_exception(self):
        mock_action = MagicMock()
        mock_action.side_effect = NotImplementedError
        vdm_utils = VDMUtils({}, helper_logger)

        result = vdm_utils._vdm_action_and_confirm(1, mock_action, None, "freeze")
        assert not result

    def test_get_vdm_thresholds(self):
        mock_sfp = MagicMock()
        vdm_utils = VDMUtils({1 : mock_sfp}, helper_logger)

        mock_sfp.get_transceiver_vdm_thresholds.return_value = True
        assert vdm_utils.get_vdm_thresholds(1)

        mock_sfp.get_transceiver_vdm_thresholds.return_value = {}
        assert vdm_utils.get_vdm_thresholds(1) == {}

        mock_sfp.get_transceiver_vdm_thresholds.side_effect = NotImplementedError
        assert vdm_utils.get_vdm_thresholds(1) == {}

    def test_get_vdm_real_values(self):
        mock_sfp = MagicMock()
        vdm_utils = VDMUtils({1 : mock_sfp}, helper_logger)

        mock_sfp.get_transceiver_vdm_real_value.return_value = True
        assert vdm_utils.get_vdm_real_values(1)

        mock_sfp.get_transceiver_vdm_real_value.return_value = {}
        assert vdm_utils.get_vdm_real_values(1) == {}

        mock_sfp.get_transceiver_vdm_real_value.side_effect = NotImplementedError
        assert vdm_utils.get_vdm_real_values(1) == {}

    def test_get_vdm_flags(self):
        mock_sfp = MagicMock()
        vdm_utils = VDMUtils({1 : mock_sfp}, helper_logger)

        mock_sfp.get_transceiver_vdm_flags.return_value = True
        assert vdm_utils.get_vdm_flags(1)

        mock_sfp.get_transceiver_vdm_flags.return_value = {}
        assert vdm_utils.get_vdm_flags(1) == {}

        mock_sfp.get_sfp.side_effect = NotImplementedError
        assert vdm_utils.get_vdm_flags(1) == {}

    @patch('xcvrd.xcvrd_utilities.common.platform_chassis')
    @patch('xcvrd.xcvrd_utilities.common.platform_sfputil')
    def test_wrapper_get_transceiver_firmware_info(self, mock_sfputil, mock_chassis):
        mock_object = MagicMock()
        mock_object.get_transceiver_dom_real_value = MagicMock(return_value=True)
        mock_chassis.get_sfp = MagicMock(return_value=mock_object)
        from xcvrd.xcvrd_utilities.common import _wrapper_get_transceiver_firmware_info
        assert common._wrapper_get_transceiver_firmware_info(1)

        mock_object.get_transceiver_info_firmware_versions = MagicMock(return_value={})
        assert common._wrapper_get_transceiver_firmware_info(1) == {}

        mock_chassis.get_sfp = MagicMock(side_effect=NotImplementedError)
        assert common._wrapper_get_transceiver_firmware_info(1) == {}

    def test_get_transceiver_dom_temperature(self):
        mock_sfp = MagicMock()
        dom_utils = DOMUtils({1 : mock_sfp}, helper_logger)

        mock_sfp.get_temperature.return_value = 42.
        assert 'temperature' in dom_utils.get_transceiver_dom_temperature(1)

        mock_sfp.get_temperature.side_effect = NotImplementedError
        assert dom_utils.get_transceiver_dom_temperature(1) == {}

    def test_get_transceiver_dom_sensor_real_value(self):
        mock_sfp = MagicMock()
        dom_utils = DOMUtils({1 : mock_sfp}, helper_logger)

        mock_sfp.get_transceiver_dom_real_value.return_value = True
        assert dom_utils.get_transceiver_dom_sensor_real_value(1)

        mock_sfp.get_transceiver_dom_real_value.return_value = {}
        assert dom_utils.get_transceiver_dom_sensor_real_value(1) == {}

        mock_sfp.get_transceiver_dom_real_value.side_effect = NotImplementedError
        assert dom_utils.get_transceiver_dom_sensor_real_value(1) == {}

    def test_get_transceiver_dom_flags(self):
        mock_sfp = MagicMock()
        dom_utils = DOMUtils({1 : mock_sfp}, helper_logger)

        mock_sfp.get_transceiver_dom_flags.return_value = True
        assert dom_utils.get_transceiver_dom_flags(1)

        mock_sfp.get_transceiver_dom_flags.return_value = {}
        assert dom_utils.get_transceiver_dom_flags(1) == {}

        mock_sfp.get_transceiver_dom_flags.side_effect = NotImplementedError
        assert dom_utils.get_transceiver_dom_flags(1) == {}

    def test_get_transceiver_dom_thresholds(self):
        mock_sfp = MagicMock()
        dom_utils = DOMUtils({1 : mock_sfp}, helper_logger)

        mock_sfp.get_transceiver_threshold_info.return_value = True
        assert dom_utils.get_transceiver_dom_thresholds(1)

        mock_sfp.get_transceiver_threshold_info.return_value = {}
        assert dom_utils.get_transceiver_dom_thresholds(1) == {}

        mock_sfp.get_transceiver_threshold_info.side_effect = NotImplementedError
        assert dom_utils.get_transceiver_dom_thresholds(1) == {}

    def test_get_transceiver_status(self):
        mock_sfp = MagicMock()
        status_utils = StatusUtils({1 : mock_sfp}, helper_logger)

        mock_sfp.get_transceiver_status.return_value = True
        assert status_utils.get_transceiver_status(1)

        mock_sfp.get_transceiver_status.return_value = {}
        assert status_utils.get_transceiver_status(1) == {}

        mock_sfp.get_transceiver_status.side_effect = NotImplementedError
        assert status_utils.get_transceiver_status(1) == {}

    def test_get_transceiver_status_flags(self):
        mock_sfp = MagicMock()
        status_utils = StatusUtils({1 : mock_sfp}, helper_logger)

        mock_sfp.get_transceiver_status_flags.return_value = True
        assert status_utils.get_transceiver_status_flags(1)

        mock_sfp.get_transceiver_status_flags.return_value = {}
        assert status_utils.get_transceiver_status_flags(1) == {}

        mock_sfp.get_transceiver_status_flags.side_effect = NotImplementedError
        assert status_utils.get_transceiver_status_flags(1) == {}

    @patch('xcvrd.xcvrd_utilities.common.platform_chassis')
    def test_wrapper_get_transceiver_pm(self, mock_chassis):
        mock_object = MagicMock()
        mock_object.get_transceiver_pm = MagicMock(return_value=True)
        mock_chassis.get_sfp = MagicMock(return_value=mock_object)
        from xcvrd.xcvrd_utilities.common import _wrapper_get_transceiver_pm
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

    @patch('xcvrd.xcvrd_utilities.common.platform_chassis')
    def test_wrapper_is_flat_memory(self, mock_chassis):
        mock_api = MagicMock()
        mock_api.is_flat_memory = MagicMock(return_value=True)
        mock_object = MagicMock()
        mock_object.get_xcvr_api = MagicMock(return_value=mock_api)
        mock_chassis.get_sfp = MagicMock(return_value=mock_object)

        from xcvrd.xcvrd_utilities.common import _wrapper_is_flat_memory
        assert _wrapper_is_flat_memory(1) == True

        mock_chassis.get_sfp = MagicMock(side_effect=NotImplementedError)
        assert not _wrapper_is_flat_memory(1)

    @patch('xcvrd.xcvrd_utilities.common.platform_chassis')
    def test_wrapper_is_flat_memory_no_xcvr_api(self, mock_chassis):
        mock_object = MagicMock()
        mock_object.get_xcvr_api = MagicMock(return_value=None)
        mock_chassis.get_sfp = MagicMock(return_value=mock_object)

        from xcvrd.xcvrd_utilities.common import _wrapper_is_flat_memory
        assert _wrapper_is_flat_memory(1) == True

    def test_check_port_in_range(self):
        range_str = '1 - 32'
        physical_port = 1
        assert common.check_port_in_range(range_str, physical_port)

        physical_port = 32
        assert common.check_port_in_range(range_str, physical_port)

        physical_port = 0
        assert not common.check_port_in_range(range_str, physical_port)

        physical_port = 33
        assert not common.check_port_in_range(range_str, physical_port)

    def test_get_serdes_si_setting_val_str(self):
        lane_dict = {'lane0': '1', 'lane1': '2', 'lane2': '3', 'lane3': '4'}
        # non-breakout case
        lane_count = 4
        subport_num = 0
        media_str = get_serdes_si_setting_val_str(lane_dict, lane_count, subport_num)
        assert media_str == '1,2,3,4'
        # breakout case
        lane_count = 2
        subport_num = 2
        media_str = get_serdes_si_setting_val_str(lane_dict, lane_count, subport_num)
        assert media_str == '3,4'
        # breakout case without subport number specified in config
        lane_count = 2
        subport_num = 0
        media_str = get_serdes_si_setting_val_str(lane_dict, lane_count, subport_num)
        assert media_str == '1,2'
        # breakout case with out-of-range subport number
        lane_count = 2
        subport_num = 3
        media_str = get_serdes_si_setting_val_str(lane_dict, lane_count, subport_num)
        assert media_str == '1,2'
        # breakout case with smaler lane_dict
        lane_dict = {'lane0': '1', 'lane1': '2'}
        lane_count = 2
        subport_num = 2
        media_str = get_serdes_si_setting_val_str(lane_dict, lane_count, subport_num)
        assert media_str == '1,2'
        # lane key-value pair inserted in non-asceding order
        lane_dict = {'lane0': 'a', 'lane2': 'c', 'lane1': 'b', 'lane3': 'd'}
        lane_count = 2
        subport_num = 2
        media_str = get_serdes_si_setting_val_str(lane_dict, lane_count, subport_num)
        assert media_str == 'c,d'

    class MockPortMapping:
        logical_port_list = [0, 1, 2]
        logical_port_name_to_physical_port_list = MagicMock()
        get_asic_id_for_logical_port = MagicMock()

    @patch('xcvrd.xcvrd.DaemonXcvrd.load_platform_util', MagicMock())
    @patch('xcvrd.xcvrd_utilities.port_event_helper.get_port_mapping', MagicMock(return_value=MockPortMapping))
    @patch('xcvrd.xcvrd.DaemonXcvrd.initialize_sfp_obj_dict', MagicMock())
    @patch('sonic_py_common.device_info.get_paths_to_platform_and_hwsku_dirs', MagicMock(return_value=('/tmp', '/tmp')))
    @patch('swsscommon.swsscommon.WarmStart', MagicMock())
    @patch('xcvrd.xcvrd.DaemonXcvrd.wait_for_port_config_done', MagicMock())
    @patch('xcvrd.xcvrd_utilities.common.del_port_sfp_dom_info_from_db')
    def test_DaemonXcvrd_init_deinit_fastboot_enabled(self, mock_del_port_sfp_dom_info_from_db):
        xcvrd = DaemonXcvrd(SYSLOG_IDENTIFIER)
        with patch("subprocess.check_output") as mock_run:
            mock_run.return_value = "true"
            xcvrd.initialize_port_init_control_fields_in_port_table = MagicMock()
            xcvrd.remove_stale_transceiver_info = MagicMock()

            xcvrd.init()

            status_tbl = MagicMock()
            xcvrd.xcvr_table_helper.get_status_tbl = MagicMock(return_value=status_tbl)
            status_sw_tbl = MagicMock()
            xcvrd.xcvr_table_helper.get_status_sw_tbl = MagicMock(return_value=status_sw_tbl)
            xcvrd.xcvr_table_helper.get_dom_tbl = MagicMock(return_value=MagicMock)
            xcvrd.xcvr_table_helper.get_dom_temperature_tbl = MagicMock(return_value=MagicMock)
            xcvrd.xcvr_table_helper.get_dom_flag_tbl = MagicMock()
            xcvrd.xcvr_table_helper.get_dom_flag_change_count_tbl = MagicMock()
            xcvrd.xcvr_table_helper.get_dom_flag_set_time_tbl = MagicMock()
            xcvrd.xcvr_table_helper.get_dom_flag_clear_time_tbl = MagicMock()
            xcvrd.xcvr_table_helper.get_dom_threshold_tbl = MagicMock(return_value=MagicMock)
            xcvrd.xcvr_table_helper.get_vdm_threshold_tbl = MagicMock(return_value=MagicMock)
            xcvrd.xcvr_table_helper.get_vdm_real_value_tbl = MagicMock(return_value=MagicMock)
            xcvrd.xcvr_table_helper.get_vdm_flag_tbl = MagicMock()
            xcvrd.xcvr_table_helper.get_vdm_flag_change_count_tbl = MagicMock()
            xcvrd.xcvr_table_helper.get_vdm_flag_set_time_tbl = MagicMock()
            xcvrd.xcvr_table_helper.get_vdm_flag_clear_time_tbl = MagicMock()
            xcvrd.xcvr_table_helper.get_status_flag_tbl = MagicMock()
            xcvrd.xcvr_table_helper.get_status_flag_change_count_tbl = MagicMock()
            xcvrd.xcvr_table_helper.get_status_flag_set_time_tbl = MagicMock()
            xcvrd.xcvr_table_helper.get_status_flag_clear_time_tbl = MagicMock()
            xcvrd.xcvr_table_helper.get_pm_tbl = MagicMock(return_value=MagicMock)
            xcvrd.xcvr_table_helper.get_firmware_info_tbl = MagicMock(return_value=MagicMock)

            xcvrd.deinit()

            assert (status_tbl, status_sw_tbl) not in mock_del_port_sfp_dom_info_from_db.call_args_list


    @patch('xcvrd.xcvrd.DaemonXcvrd.load_platform_util', MagicMock())
    @patch('xcvrd.xcvrd_utilities.port_event_helper.get_port_mapping', MagicMock(return_value=MockPortMapping))
    @patch('sonic_py_common.device_info.get_paths_to_platform_and_hwsku_dirs', MagicMock(return_value=('/tmp', '/tmp')))
    @patch('xcvrd.xcvrd_utilities.common.is_warm_reboot_enabled', MagicMock(return_value=False))
    @patch('xcvrd.xcvrd.DaemonXcvrd.wait_for_port_config_done', MagicMock())
    @patch('xcvrd.xcvrd.DaemonXcvrd.initialize_sfp_obj_dict', MagicMock())
    @patch('subprocess.check_output', MagicMock(return_value='false'))
    @patch('xcvrd.xcvrd_utilities.common.del_port_sfp_dom_info_from_db')
    def test_DaemonXcvrd_init_deinit_cold(self, mock_del_port_sfp_dom_info_from_db):
        xcvrd.platform_chassis = MagicMock()

        xcvrdaemon = DaemonXcvrd(SYSLOG_IDENTIFIER)
        with patch("subprocess.check_output") as mock_run:
            mock_run.return_value = "false"
            xcvrdaemon.initialize_port_init_control_fields_in_port_table = MagicMock()
            xcvrdaemon.remove_stale_transceiver_info = MagicMock()

            xcvrdaemon.init()

            status_tbl = MagicMock()
            xcvrdaemon.xcvr_table_helper.get_status_tbl = MagicMock(return_value=status_tbl)
            status_sw_tbl = MagicMock()
            xcvrdaemon.xcvr_table_helper.get_status_sw_tbl = MagicMock(return_value=status_sw_tbl)
            xcvrdaemon.xcvr_table_helper.get_dom_tbl = MagicMock(return_value=MagicMock)
            xcvrdaemon.xcvr_table_helper.get_dom_temperature_tbl = MagicMock(return_value=MagicMock)
            xcvrdaemon.xcvr_table_helper.get_dom_threshold_tbl = MagicMock(return_value=MagicMock)
            xcvrdaemon.xcvr_table_helper.get_dom_flag_tbl = MagicMock()
            xcvrdaemon.xcvr_table_helper.get_dom_flag_change_count_tbl = MagicMock()
            xcvrdaemon.xcvr_table_helper.get_dom_flag_set_time_tbl = MagicMock()
            xcvrdaemon.xcvr_table_helper.get_dom_flag_clear_time_tbl = MagicMock()
            xcvrdaemon.xcvr_table_helper.get_vdm_threshold_tbl = MagicMock(return_value=MagicMock)
            xcvrdaemon.xcvr_table_helper.get_vdm_real_value_tbl = MagicMock(return_value=MagicMock)
            xcvrdaemon.xcvr_table_helper.get_vdm_flag_tbl = MagicMock()
            xcvrdaemon.xcvr_table_helper.get_vdm_flag_change_count_tbl = MagicMock()
            xcvrdaemon.xcvr_table_helper.get_vdm_flag_set_time_tbl = MagicMock()
            xcvrdaemon.xcvr_table_helper.get_vdm_flag_clear_time_tbl = MagicMock()
            xcvrdaemon.xcvr_table_helper.get_status_flag_tbl = MagicMock()
            xcvrdaemon.xcvr_table_helper.get_status_flag_change_count_tbl = MagicMock()
            xcvrdaemon.xcvr_table_helper.get_status_flag_set_time_tbl = MagicMock()
            xcvrdaemon.xcvr_table_helper.get_status_flag_clear_time_tbl = MagicMock()
            xcvrdaemon.xcvr_table_helper.get_pm_tbl = MagicMock(return_value=MagicMock)
            xcvrdaemon.xcvr_table_helper.get_firmware_info_tbl = MagicMock(return_value=MagicMock)
            xcvrdaemon.xcvr_table_helper.get_intf_tbl = MagicMock(return_value=MagicMock)

            xcvrdaemon.deinit()
            assert mock_del_port_sfp_dom_info_from_db.call_any_with(status_tbl, status_sw_tbl)

    def test_DaemonXcvrd_signal_handler(self):
        xcvrd.platform_chassis = MagicMock()
        xcvrdaemon = DaemonXcvrd(SYSLOG_IDENTIFIER)
        xcvrdaemon.update_loggers_log_level = MagicMock()
        xcvrdaemon.signal_handler(signal.SIGHUP, None)
        xcvrdaemon.update_loggers_log_level.assert_called()

    @patch('xcvrd.xcvrd.helper_logger')
    def test_DaemonXcvrd_update_loggers_log_level(self, mock_helper_logger):
        """Test update_loggers_log_level method updates all logger instances"""
        # Setup
        xcvrd.platform_chassis = MagicMock()
        xcvrdaemon = DaemonXcvrd(SYSLOG_IDENTIFIER)

        # Mock the logger_instance
        mock_logger_instance = MagicMock()
        xcvrdaemon.logger_instance = mock_logger_instance

        # Create mock threads with and without update_log_level method
        mock_thread_with_update = MagicMock()
        mock_thread_with_update.update_log_level = MagicMock()

        mock_thread_without_update = MagicMock()
        # This thread doesn't have update_log_level method
        del mock_thread_without_update.update_log_level

        mock_thread_with_non_callable = MagicMock()
        mock_thread_with_non_callable.update_log_level = "not_callable"

        # Add threads to the daemon
        xcvrdaemon.threads = [
            mock_thread_with_update,
            mock_thread_without_update,
            mock_thread_with_non_callable
        ]

        # Execute
        xcvrdaemon.update_loggers_log_level()

        # Verify helper_logger.update_log_level() was called
        mock_helper_logger.update_log_level.assert_called_once()

        # Verify logger_instance.update_log_level() was called
        mock_logger_instance.update_log_level.assert_called_once()

        # Verify only the thread with callable update_log_level was called
        mock_thread_with_update.update_log_level.assert_called_once()

        # Verify threads without callable update_log_level were not called
        # (no assertion needed for mock_thread_without_update since it doesn't have the method)
        # mock_thread_with_non_callable.update_log_level should not be called since it's not callable

    @patch('xcvrd.xcvrd.helper_logger')
    def test_DaemonXcvrd_update_loggers_log_level_empty_threads(self, mock_helper_logger):
        """Test update_loggers_log_level method with no threads"""
        # Setup
        xcvrd.platform_chassis = MagicMock()
        xcvrdaemon = DaemonXcvrd(SYSLOG_IDENTIFIER)

        # Mock the logger_instance
        mock_logger_instance = MagicMock()
        xcvrdaemon.logger_instance = mock_logger_instance

        # No threads
        xcvrdaemon.threads = []

        # Execute
        xcvrdaemon.update_loggers_log_level()

        # Verify helper_logger.update_log_level() was called
        mock_helper_logger.update_log_level.assert_called_once()

        # Verify logger_instance.update_log_level() was called
        mock_logger_instance.update_log_level.assert_called_once()

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
        task = CmisManagerTask(DEFAULT_NAMESPACE, port_mapping, stop_event, platform_chassis=MagicMock())
        result = task.validate_frequency_and_grid(mock_xcvr_api, lport, freq, grid)
        assert result == expected

    def test_xcvrd_utils_get_transceiver_presence(self):
        from xcvrd.xcvrd_utilities.utils import XCVRDUtils
        mock_sfp = MagicMock()
        xcvrd_util = XCVRDUtils({1 : mock_sfp}, helper_logger)
        mock_sfp.get_presence = MagicMock(return_value=True)
        assert xcvrd_util.get_transceiver_presence(1)

        mock_sfp.get_presence = MagicMock(return_value=False)
        assert not xcvrd_util.get_transceiver_presence(1)

        mock_sfp.get_presence = MagicMock(side_effect=NotImplementedError)
        assert not xcvrd_util.get_transceiver_presence(1)

    def test_is_transceiver_flat_memory(self):
        from xcvrd.xcvrd_utilities.utils import XCVRDUtils
        mock_sfp = MagicMock()
        xcvrd_util = XCVRDUtils({1: mock_sfp}, MagicMock())

        # Test case where get_xcvr_api returns None
        mock_sfp.get_xcvr_api = MagicMock(return_value=None)
        assert xcvrd_util.is_transceiver_flat_memory(1)

        # Test case where is_flat_memory returns True
        mock_api = MagicMock()
        mock_api.is_flat_memory = MagicMock(return_value=True)
        mock_sfp.get_xcvr_api = MagicMock(return_value=mock_api)
        assert xcvrd_util.is_transceiver_flat_memory(1)

        # Test case where is_flat_memory returns False
        mock_api.is_flat_memory = MagicMock(return_value=False)
        assert not xcvrd_util.is_transceiver_flat_memory(1)

        # Test case where get_xcvr_api raises KeyError
        xcvrd_util.sfp_obj_dict = {}
        assert xcvrd_util.is_transceiver_flat_memory(1)

        # Test case where is_flat_memory raises NotImplementedError
        xcvrd_util.sfp_obj_dict = {1: mock_sfp}
        mock_api.is_flat_memory = MagicMock(side_effect=NotImplementedError)
        assert xcvrd_util.is_transceiver_flat_memory(1)

    def test_is_transceiver_lpmode_on(self):
        from xcvrd.xcvrd_utilities.utils import XCVRDUtils
        mock_sfp = MagicMock()
        xcvrd_util = XCVRDUtils({1: mock_sfp}, MagicMock())

        # Test case where get_xcvr_api returns None
        mock_sfp.get_lpmode = MagicMock(return_value=None)
        assert not xcvrd_util.is_transceiver_lpmode_on(1)

        # Test case where get_lpmode returns True
        mock_sfp.get_lpmode = MagicMock(return_value=True)
        assert xcvrd_util.is_transceiver_lpmode_on(1)

        # Test case where get_lpmode returns False

        mock_sfp.get_lpmode = MagicMock(return_value=False)
        assert not xcvrd_util.is_transceiver_lpmode_on(1)

        # Test case where get_xcvr_api raises KeyError
        xcvrd_util.sfp_obj_dict = {}
        assert not xcvrd_util.is_transceiver_lpmode_on(1)

        # Test case where is_flat_memory raises NotImplementedError
        xcvrd_util.sfp_obj_dict = {1: mock_sfp}
        mock_sfp.get_lpmode = MagicMock(side_effect=NotImplementedError)
        assert not xcvrd_util.is_transceiver_lpmode_on(1)

    @patch('time.sleep', MagicMock())
    @patch('xcvrd.xcvrd.XcvrTableHelper', MagicMock())
    @patch('xcvrd.xcvrd._wrapper_soak_sfp_insert_event', MagicMock())
    @patch('xcvrd.xcvrd_utilities.port_event_helper.subscribe_port_config_change', MagicMock(return_value=(None, None)))
    @patch('xcvrd.xcvrd_utilities.port_event_helper.handle_port_config_change', MagicMock())
    @patch('xcvrd.xcvrd.SfpStateUpdateTask.init', MagicMock())
    @patch('os.kill')
    @patch('xcvrd.xcvrd.SfpStateUpdateTask._mapping_event_from_change_event')
    @patch('xcvrd.xcvrd._wrapper_get_transceiver_change_event')
    @patch('xcvrd.xcvrd_utilities.common.del_port_sfp_dom_info_from_db')
    @patch('xcvrd.xcvrd_utilities.media_settings_parser.notify_media_setting')
    @patch('xcvrd.dom.dom_mgr.DomInfoUpdateTask.post_port_sfp_firmware_info_to_db')
    @patch('xcvrd.xcvrd.post_port_sfp_info_to_db')
    @patch('xcvrd.xcvrd_utilities.common.update_port_transceiver_status_table_sw')
    @patch('xcvrd.xcvrd.platform_chassis')
    def test_sfp_removal_from_dict(self, mock_platform_chassis, mock_update_status, mock_post_sfp_info,
                                            mock_post_firmware_info, mock_update_media_setting,
                                            mock_del_dom, mock_change_event, mock_mapping_event, mock_os_kill):
        port_mapping = PortMapping()
        mock_sfp = MagicMock()
        mock_sfp.remove_xcvr_api = MagicMock(return_value=None)
        mock_platform_chassis.get_sfp.return_value = mock_sfp
        mock_sfp_obj_dict = MagicMock()
        stop_event = threading.Event()
        sfp_error_event = threading.Event()
        task = SfpStateUpdateTask(DEFAULT_NAMESPACE, port_mapping, mock_sfp_obj_dict, stop_event, sfp_error_event)
        task.xcvr_table_helper = XcvrTableHelper(DEFAULT_NAMESPACE)
        task.dom_db_utils.post_port_dom_thresholds_to_db = MagicMock()
        task.vdm_db_utils.post_port_vdm_thresholds_to_db = MagicMock()
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
        assert task.dom_db_utils.post_port_dom_thresholds_to_db.call_count == 0
        assert task.vdm_db_utils.post_port_vdm_thresholds_to_db.call_count == 0
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
        assert task.dom_db_utils.post_port_dom_thresholds_to_db.call_count == 1
        assert task.vdm_db_utils.post_port_vdm_thresholds_to_db.call_count == 1
        assert mock_post_firmware_info.call_count == 0
        assert mock_update_media_setting.call_count == 1

        stop_event.is_set = MagicMock(side_effect=[False, True])
        mock_change_event.return_value = (True, {1: SFP_STATUS_REMOVED}, {})
        mock_update_status.reset_mock()
        # Test state machine: handle SFP remove event
        task.task_worker(stop_event, sfp_error_event)
        assert mock_update_status.call_count == 1
        assert mock_del_dom.call_count == 1
        mock_sfp.remove_xcvr_api.assert_called_once()

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

class TestOpticSiParser(object):
    def test_match_optics_si_key_regex_error(self):
        """Test _match_optics_si_key with invalid regex pattern (lines 31-32, 34)"""
        from xcvrd.xcvrd_utilities.optics_si_parser import _match_optics_si_key

        # Test with invalid regex pattern that causes re.error
        dict_key = "[invalid regex"  # Unclosed bracket causes regex error
        key = "VENDOR-1234"
        vendor_name_str = "VENDOR"

        # Should fall back to string comparison and return True for exact match
        result = _match_optics_si_key(dict_key, key, vendor_name_str)
        assert result == False  # No exact string match

        # Test with exact string match after regex error
        result = _match_optics_si_key(dict_key, dict_key, vendor_name_str)
        assert result == True  # Exact string match

    def test_match_optics_si_key_fallback_string_match(self):
        """Test _match_optics_si_key fallback string comparison (line 37)"""
        from xcvrd.xcvrd_utilities.optics_si_parser import _match_optics_si_key

        # Test with invalid regex that falls back to string comparison
        dict_key = "[invalid"
        key = "VENDOR-1234"
        vendor_name_str = "VENDOR"

        # Test exact key match
        result = _match_optics_si_key(key, key, vendor_name_str)
        assert result == True

        # Test vendor name match
        result = _match_optics_si_key(vendor_name_str, key, vendor_name_str)
        assert result == True

        # Test split key match
        result = _match_optics_si_key("VENDOR", key, vendor_name_str)
        assert result == True

    def test_get_port_media_settings_speed_key_missing(self):
        """Test _get_port_media_settings when SPEED_KEY not in optics_si_dict (line 126)"""
        from xcvrd.xcvrd_utilities.optics_si_parser import _get_port_media_settings
        import xcvrd.xcvrd_utilities.optics_si_parser as parser

        original_dict = parser.g_optics_si_dict
        parser.g_optics_si_dict = {
            'PORT_MEDIA_SETTINGS': {
                '5': {
                    # Missing SPEED_KEY (25G_SPEED)
                }
            }
        }

        try:
            result = _get_port_media_settings(5, 25, "VENDOR-1234", "VENDOR", {'default': 'value'})
            assert result == {'default': 'value'}
        finally:
            parser.g_optics_si_dict = original_dict

    def test_get_module_vendor_key_api_none(self):
        """Test get_module_vendor_key when API is None (line 152)"""
        from xcvrd.xcvrd_utilities.optics_si_parser import get_module_vendor_key

        # Mock SFP with None API
        mock_sfp = MagicMock()
        mock_sfp.get_xcvr_api.return_value = None

        result = get_module_vendor_key(1, mock_sfp)
        assert result is None

    def test_get_module_vendor_key_vendor_name_none(self):
        """Test get_module_vendor_key when vendor name is None"""
        from xcvrd.xcvrd_utilities.optics_si_parser import get_module_vendor_key

        # Mock API with None vendor name
        mock_api = MagicMock()
        mock_api.get_manufacturer.return_value = None
        mock_sfp = MagicMock()
        mock_sfp.get_xcvr_api.return_value = mock_api

        result = get_module_vendor_key(1, mock_sfp)
        assert result is None

    def test_get_module_vendor_key_vendor_pn_none(self):
        """Test get_module_vendor_key when vendor part number is None"""
        from xcvrd.xcvrd_utilities.optics_si_parser import get_module_vendor_key

        # Mock API with None vendor part number
        mock_api = MagicMock()
        mock_api.get_manufacturer.return_value = "VENDOR"
        mock_api.get_model.return_value = None
        mock_sfp = MagicMock()
        mock_sfp.get_xcvr_api.return_value = mock_api

        result = get_module_vendor_key(1, mock_sfp)
        assert result is None

    def test_get_port_media_settings_no_values_with_empty_default(self):
        """Test _get_port_media_settings logging when port exists but has empty config and no default values"""
        from xcvrd.xcvrd_utilities.optics_si_parser import _get_port_media_settings
        import xcvrd.xcvrd_utilities.optics_si_parser as parser

        original_dict = parser.g_optics_si_dict

        # Set up scenario where:
        # 1. Port exists in PORT_MEDIA_SETTINGS but has empty configuration
        # 2. This makes len(optics_si_dict) == 0
        # 3. Default dict is empty (len(default_dict) == 0)
        parser.g_optics_si_dict = {
            'PORT_MEDIA_SETTINGS': {
                '5': {}  # Port exists but is empty - this triggers len(optics_si_dict) == 0
            }
        }

        try:
            # This should trigger the log_info line at lines 119-121
            # since len(optics_si_dict) == 0 and len(default_dict) == 0
            result = _get_port_media_settings(5, 25, "VENDOR-1234", "VENDOR", {})

            # Should return empty dict when no values found and no defaults
            assert result == {}
        finally:
            parser.g_optics_si_dict = original_dict

    def test_load_optics_si_settings_no_file(self):
        """Test load_optics_si_settings when no file exists"""
        from xcvrd.xcvrd_utilities.optics_si_parser import load_optics_si_settings

        with patch('sonic_py_common.device_info.get_paths_to_platform_and_hwsku_dirs', 
                return_value=('/nonexistent/platform', '/nonexistent/hwsku')):
            with patch('os.path.isfile', return_value=False):
                result = load_optics_si_settings()
                assert result == {}

    def test_optics_si_present_empty_dict(self):
        """Test optics_si_present when global dict is empty"""
        from xcvrd.xcvrd_utilities.optics_si_parser import optics_si_present
        import xcvrd.xcvrd_utilities.optics_si_parser as parser

        original_dict = parser.g_optics_si_dict
        parser.g_optics_si_dict = {}

        try:
            result = optics_si_present()
            assert result == False
        finally:
            parser.g_optics_si_dict = original_dict

    def test_optics_si_present_with_data(self):
        """Test optics_si_present when global dict has data"""
        from xcvrd.xcvrd_utilities.optics_si_parser import optics_si_present
        import xcvrd.xcvrd_utilities.optics_si_parser as parser

        original_dict = parser.g_optics_si_dict
        parser.g_optics_si_dict = {'some': 'data'}

        try:
            result = optics_si_present()
            assert result == True
        finally:
            parser.g_optics_si_dict = original_dict
