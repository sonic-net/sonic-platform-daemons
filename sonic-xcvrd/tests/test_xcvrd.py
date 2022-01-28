import os
import sys
import subprocess

import pytest
import unittest
from imp import load_source
if sys.version_info >= (3, 3):
    from unittest.mock import MagicMock, patch
else:
    from mock import MagicMock, patch

from sonic_py_common import daemon_base
from swsscommon import swsscommon
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

os.environ["XCVRD_UNIT_TESTING"] = "1"
from xcvrd.xcvrd import *


class TestXcvrdScript(object):

    @patch('xcvrd.xcvrd.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
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
    def test_post_port_dom_info_to_db(self):
        logical_port_name = "Ethernet0"
        stop_event = threading.Event()
        dom_tbl = Table("STATE_DB", TRANSCEIVER_DOM_SENSOR_TABLE)
        post_port_dom_info_to_db(logical_port_name, dom_tbl, stop_event)

    @patch('xcvrd.xcvrd.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd._wrapper_get_presence', MagicMock(return_value=True))
    def test_del_port_sfp_dom_info_from_db(self):
        logical_port_name = "Ethernet0"
        stop_event = threading.Event()
        dom_tbl = Table("STATE_DB", TRANSCEIVER_DOM_SENSOR_TABLE)
        init_tbl = Table("STATE_DB", TRANSCEIVER_INFO_TABLE)
        del_port_sfp_dom_info_from_db(logical_port_name, init_tbl, dom_tbl)

    @patch('xcvrd.xcvrd.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
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
        stop_event = threading.Event()
        dom_tbl = Table("STATE_DB", TRANSCEIVER_DOM_SENSOR_TABLE)
        post_port_dom_threshold_info_to_db(logical_port_name, dom_tbl, stop_event)

    @patch('xcvrd.xcvrd.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd._wrapper_get_presence', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd._wrapper_is_replaceable', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd._wrapper_get_transceiver_info', MagicMock(return_value={'type': '22.75',
                                                                                'hardware_rev': '0.5',
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
    def test_post_port_sfp_info_to_db(self):
        logical_port_name = "Ethernet0"
        stop_event = threading.Event()
        dom_tbl = Table("STATE_DB", TRANSCEIVER_DOM_SENSOR_TABLE)
        transceiver_dict = {}
        post_port_sfp_info_to_db(logical_port_name, dom_tbl, transceiver_dict, stop_event)

    @patch('xcvrd.xcvrd.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd.platform_sfputil', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd._wrapper_get_presence', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd._wrapper_is_replaceable', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd._wrapper_get_transceiver_info', MagicMock(return_value={'type': '22.75',
                                                                                'hardware_rev': '0.5',
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
    def test_post_port_sfp_dom_info_to_db(self):
        logical_port_name = "Ethernet0"
        stop_event = threading.Event()
        post_port_sfp_dom_info_to_db(True, stop_event)

    @patch('xcvrd.xcvrd.logical_port_name_to_physical_port_list', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd.platform_sfputil', MagicMock(return_value=[0]))
    @patch('xcvrd.xcvrd._wrapper_get_presence', MagicMock(return_value=True))
    @patch('xcvrd.xcvrd._wrapper_is_replaceable', MagicMock(return_value=True))
    def test_init_port_sfp_status_tbl(self):
        stop_event = threading.Event()
        init_port_sfp_status_tbl(stop_event)

    def test_get_media_settings_key(self):
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
        result = get_media_settings_key(0, xcvr_info_dict)
        assert result == ['MOLEX-1064141421', 'QSFP+-10GBase-SR-255M']

        # Test a bad 'specification_compliance' value
        xcvr_info_dict[0]['specification_compliance'] = 'N/A'
        result = get_media_settings_key(0, xcvr_info_dict)
        assert result == ['MOLEX-1064141421', 'QSFP+-*']
        # TODO: Ensure that error message was logged
