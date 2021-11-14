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

sys.modules['sonic_y_cable'] = MagicMock()
sys.modules['sonic_y_cable.y_cable'] = MagicMock()

test_path = os.path.dirname(os.path.abspath(__file__))
modules_path = os.path.dirname(test_path)
scripts_path = os.path.join(modules_path, "ycable")
sys.path.insert(0, modules_path)

os.environ["YCABLE_UNIT_TESTING"] = "1"
from ycable.ycable import *
from ycable.ycable_utilities.y_cable_helper import *
from ycable.ycable_utilities.sfp_status_helper import *

class TestYcableScript(object):
    def test_ycable_helper_class_run(self):
        Y_cable_task = YCableTableUpdateTask()

    @patch('ycable.ycable_utilities.y_cable_helper.y_cable_platform_sfputil', MagicMock(return_value=[0]))
    @patch('ycable.ycable_utilities.y_cable_helper.y_cable_wrapper_get_presence', MagicMock(return_value=True))
    @patch('ycable.ycable_utilities.y_cable_helper.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
    @patch('ycable.ycable_utilities.y_cable_helper.get_muxcable_info', MagicMock(return_value={'tor_active': 'self',
                                                                       'mux_direction': 'self',
                                                                       'manual_switch_count': '7',
                                                                       'auto_switch_count': '71',
                                                                       'link_status_self': 'up',
                                                                       'link_status_peer': 'up',
                                                                       'link_status_nic': 'up',
                                                                       'nic_lane1_active': 'True',
                                                                       'nic_lane2_active': 'True',
                                                                       'nic_lane3_active': 'True',
                                                                       'nic_lane4_active': 'True',
                                                                       'self_eye_height_lane1': '500',
                                                                       'self_eye_height_lane2': '510',
                                                                       'peer_eye_height_lane1': '520',
                                                                       'peer_eye_height_lane2': '530',
                                                                       'nic_eye_height_lane1': '742',
                                                                       'nic_eye_height_lane2': '750',
                                                                       'internal_temperature': '28',
                                                                       'internal_voltage': '3.3',
                                                                       'nic_temperature': '20',
                                                                       'nic_voltage': '2.7',
                                                                       'version_nic_active': '1.6MS',
                                                                       'version_nic_inactive': '1.7MS',
                                                                       'version_nic_next': '1.7MS',
                                                                       'version_self_active': '1.6MS',
                                                                       'version_self_inactive': '1.7MS',
                                                                       'version_self_next': '1.7MS',
                                                                       'version_peer_active': '1.6MS',
                                                                       'version_peer_inactive': '1.7MS',
                                                                       'version_peer_next': '1.7MS'}))
    def test_post_port_mux_info_to_db(self):
        logical_port_name = "Ethernet0"
        mux_tbl = Table("STATE_DB", y_cable_helper.MUX_CABLE_INFO_TABLE)
        rc = post_port_mux_info_to_db(logical_port_name, mux_tbl)
        assert(rc != -1)


    @patch('ycable.ycable_utilities.y_cable_helper.y_cable_platform_sfputil', MagicMock(return_value=[0]))
    @patch('ycable.ycable_utilities.y_cable_helper.y_cable_wrapper_get_presence', MagicMock(return_value=True))
    @patch('ycable.ycable_utilities.y_cable_helper.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
    @patch('ycable.ycable_utilities.y_cable_helper.get_muxcable_static_info', MagicMock(return_value={'read_side': 'self',
                                                                              'nic_lane1_precursor1': '1',
                                                                              'nic_lane1_precursor2': '-7',
                                                                              'nic_lane1_maincursor': '-1',
                                                                              'nic_lane1_postcursor1': '11',
                                                                              'nic_lane1_postcursor2': '11',
                                                                              'nic_lane2_precursor1': '12',
                                                                              'nic_lane2_precursor2': '7',
                                                                              'nic_lane2_maincursor': '7',
                                                                              'nic_lane2_postcursor1': '7',
                                                                              'nic_lane2_postcursor2': '7',
                                                                              'tor_self_lane1_precursor1': '17',
                                                                              'tor_self_lane1_precursor2': '17',
                                                                              'tor_self_lane1_maincursor': '17',
                                                                              'tor_self_lane1_postcursor1': '17',
                                                                              'tor_self_lane1_postcursor2': '17',
                                                                              'tor_self_lane2_precursor1': '7',
                                                                              'tor_self_lane2_precursor2': '7',
                                                                              'tor_self_lane2_maincursor': '7',
                                                                              'tor_self_lane2_postcursor1': '7',
                                                                              'tor_self_lane2_postcursor2': '7',
                                                                              'tor_peer_lane1_precursor1': '7',
                                                                              'tor_peer_lane1_precursor2': '7',
                                                                              'tor_peer_lane1_maincursor': '17',
                                                                              'tor_peer_lane1_postcursor1': '7',
                                                                              'tor_peer_lane1_postcursor2': '17',
                                                                              'tor_peer_lane2_precursor1': '7',
                                                                              'tor_peer_lane2_precursor2': '7',
                                                                              'tor_peer_lane2_maincursor': '17',
                                                                              'tor_peer_lane2_postcursor1': '7',
                                                                              'tor_peer_lane2_postcursor2': '17'}))
    def test_post_port_mux_static_info_to_db(self):
        logical_port_name = "Ethernet0"
        mux_tbl = Table("STATE_DB", y_cable_helper.MUX_CABLE_STATIC_INFO_TABLE)
        rc = post_port_mux_static_info_to_db(logical_port_name, mux_tbl)
        assert(rc != -1)

    def test_y_cable_helper_format_mapping_identifier1(self):
        rc = format_mapping_identifier("ABC        ")
        assert(rc == "abc")

    def test_y_cable_wrapper_get_transceiver_info(self):
        with patch('ycable.ycable_utilities.y_cable_helper.y_cable_platform_sfputil') as patched_util:
            patched_util.get_transceiver_info_dict.return_value = {'manufacturer': 'Microsoft',
                                                                              'model': 'model1'}

            transceiver_dict = y_cable_wrapper_get_transceiver_info(1)
            vendor = transceiver_dict.get('manufacturer')
            model = transceiver_dict.get('model')

        assert(vendor == "Microsoft")
        assert(model == "model1")

    def test_y_cable_wrapper_get_presence(self):
        with patch('ycable.ycable_utilities.y_cable_helper.y_cable_platform_sfputil') as patched_util:
            patched_util.get_presence.return_value = True

            presence = y_cable_wrapper_get_presence(1)

        assert(presence == True)

    @patch('ycable.ycable_utilities.y_cable_helper.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
    @patch('ycable.ycable_utilities.y_cable_helper.y_cable_wrapper_get_presence', MagicMock(return_value=True))
    def test_get_ycable_physical_port_from_logical_port(self):
        instance = get_ycable_physical_port_from_logical_port("Ethernet0")

        assert(instance == 0)

    @patch('ycable.ycable_utilities.y_cable_helper.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
    @patch('ycable.ycable_utilities.y_cable_helper.y_cable_wrapper_get_presence', MagicMock(return_value=True))
    def test_get_ycable_port_instance_from_logical_port(self):

        with patch('ycable.ycable_utilities.y_cable_helper.y_cable_port_instances') as patched_util:
            patched_util.get.return_value = 0
            instance = get_ycable_port_instance_from_logical_port("Ethernet0")

        assert(instance == 0)

    def test_set_show_firmware_fields(self):

        mux_info_dict = {}
        ycable_show_fw_res_tbl = Table("STATE_DB", "XCVRD_SHOW_FW_RES")
        mux_info_dict['version_self_active'] = '0.8'
        mux_info_dict['version_self_inactive'] = '0.7'
        mux_info_dict['version_self_next'] = '0.7'
        mux_info_dict['version_peer_active'] = '0.8'
        mux_info_dict['version_peer_inactive'] = '0.7'
        mux_info_dict['version_peer_next'] = '0.7'
        mux_info_dict['version_nic_active'] = '0.8'
        mux_info_dict['version_nic_inactive'] = '0.7'
        mux_info_dict['version_nic_next'] = '0.7'
        rc = set_show_firmware_fields("Ethernet0", mux_info_dict, ycable_show_fw_res_tbl)

        assert(rc == 0)

    @patch('sonic_py_common.device_info.get_paths_to_platform_and_hwsku_dirs', MagicMock(return_value=('/tmp', None)))
    @patch('swsscommon.swsscommon.WarmStart', MagicMock())    
    @patch('ycable.ycable.DaemonYcable.wait_for_port_config_done', MagicMock())
    @patch('ycable.ycable.platform_sfputil', MagicMock())
    @patch('ycable.ycable.DaemonYcable.load_platform_util', MagicMock())
    def test_DaemonYcable_init_deinit(self):
        ycable = DaemonYcable(SYSLOG_IDENTIFIER)
        ycable.init()
        ycable.deinit()
        # TODO: fow now we only simply call ycable.init/deinit without any further check, it only makes sure that
        # ycable.init/deinit will not raise unexpected exception. In future, probably more check will be added



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
