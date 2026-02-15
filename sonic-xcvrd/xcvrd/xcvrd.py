#!/usr/bin/env python3

"""
    xcvrd
    Transceiver information update daemon for SONiC
"""

try:
    import ast
    import copy
    import json
    import os
    import signal
    import sys
    import threading
    import time
    import datetime
    import subprocess
    import argparse
    import re
    import traceback
    import ctypes

    from natsort import natsorted
    from sonic_py_common import daemon_base, syslogger
    from sonic_py_common import multi_asic
    from swsscommon import swsscommon

    from .xcvrd_utilities import sfp_status_helper
    from .sff_mgr import SffManagerTask
    from .dom.dom_mgr import DomThermalInfoUpdateTask, DomInfoUpdateTask
    from .cmis.cmis_manager_task import CmisManagerTask
    from .xcvrd_utilities.xcvr_table_helper import *
    from .xcvrd_utilities import port_event_helper
    from .xcvrd_utilities.port_event_helper import PortChangeObserver
    from .xcvrd_utilities import media_settings_parser
    from .xcvrd_utilities import optics_si_parser
    from .xcvrd_utilities import common
    from xcvrd.dom.utilities.dom_sensor.db_utils import DOMDBUtils
    from xcvrd.dom.utilities.vdm.db_utils import VDMDBUtils
    
    from sonic_platform_base.sonic_xcvr.api.public.c_cmis import CmisApi

except ImportError as e:
    raise ImportError(str(e) + " - required module not found")

#
# Constants ====================================================================
#

SYSLOG_IDENTIFIER = "xcvrd"
SYSLOG_IDENTIFIER_SFPSTATEUPDATETASK = "SfpStateUpdateTask"

PLATFORM_SPECIFIC_MODULE_NAME = "sfputil"
PLATFORM_SPECIFIC_CLASS_NAME = "SfpUtil"

# Mgminit time required as per CMIS spec
MGMT_INIT_TIME_DELAY_SECS = 2

# SFP insert event poll duration
SFP_INSERT_EVENT_POLL_PERIOD_MSECS = 1000

STATE_MACHINE_UPDATE_PERIOD_MSECS = 60000
TIME_FOR_SFP_READY_SECS = 1

EVENT_ON_ALL_SFP = '-1'
# events definition
SYSTEM_NOT_READY = 'system_not_ready'
SYSTEM_BECOME_READY = 'system_become_ready'
SYSTEM_FAIL = 'system_fail'
NORMAL_EVENT = 'normal'
# states definition
STATE_INIT = 0
STATE_NORMAL = 1
STATE_EXIT = 2

PHYSICAL_PORT_NOT_EXIST = -1
SFP_EEPROM_NOT_READY = -2

SFPUTIL_LOAD_ERROR = 1
PORT_CONFIG_LOAD_ERROR = 2
NOT_IMPLEMENTED_ERROR = 3
SFP_SYSTEM_ERROR = 4

RETRY_TIMES_FOR_SYSTEM_READY = 24
RETRY_PERIOD_FOR_SYSTEM_READY_MSECS = 5000

RETRY_TIMES_FOR_SYSTEM_FAIL = 24
RETRY_PERIOD_FOR_SYSTEM_FAIL_MSECS = 5000

g_dict = {}
# Global platform specific sfputil class instance
platform_sfputil = None
# Global chassis object based on new platform api
platform_chassis = None

# Global logger instance for helper functions and classes
# TODO: Refactor so that we only need the logger inherited
# by DaemonXcvrd
helper_logger = syslogger.SysLogger(SYSLOG_IDENTIFIER, enable_runtime_config=True)

#
# Helper functions =============================================================
#
def _wrapper_is_replaceable(physical_port):
    if platform_chassis is not None:
        try:
            return platform_chassis.get_sfp(physical_port).is_replaceable()
        except NotImplementedError:
            pass
    return False


def _wrapper_get_transceiver_info(physical_port):
    if platform_chassis is not None:
        try:
            return platform_chassis.get_sfp(physical_port).get_transceiver_info()
        except NotImplementedError:
            pass
        except Exception as e:
            helper_logger.log_error("Failed to get transceiver info for physical port {}. Exception: {}".format(physical_port, e))
            common.log_exception_traceback()
            return None
    return platform_sfputil.get_transceiver_info_dict(physical_port)

# Soak SFP insert event until management init completes
def _wrapper_soak_sfp_insert_event(sfp_insert_events, port_dict):
    for key, value in list(port_dict.items()):
        if value == sfp_status_helper.SFP_STATUS_INSERTED:
            sfp_insert_events[key] = time.time()
            del port_dict[key]
        elif value == sfp_status_helper.SFP_STATUS_REMOVED:
            if key in sfp_insert_events:
                del sfp_insert_events[key]

    for key, itime in list(sfp_insert_events.items()):
        if time.time() - itime >= MGMT_INIT_TIME_DELAY_SECS:
            port_dict[key] = sfp_status_helper.SFP_STATUS_INSERTED
            del sfp_insert_events[key]

def _wrapper_get_transceiver_change_event(timeout):
    if platform_chassis is not None:
        try:
            status, events = platform_chassis.get_change_event(timeout)
            sfp_events = events.get('sfp')
            sfp_errors = events.get('sfp_error')
            return status, sfp_events, sfp_errors
        except NotImplementedError:
            pass
    status, events = platform_sfputil.get_transceiver_change_event(timeout)
    return status, events, None


def _wrapper_get_sfp_type(physical_port):
    if platform_chassis:
        try:
            sfp = platform_chassis.get_sfp(physical_port)
        except (NotImplementedError, AttributeError):
            return None
        try:
            return sfp.sfp_type
        except (NotImplementedError, AttributeError):
            pass
    return None


def _wrapper_get_sfp_error_description(physical_port):
    if platform_chassis:
        try:
            return platform_chassis.get_sfp(physical_port).get_error_description()
        except NotImplementedError:
            pass
    return None

# Update port sfp info in db


def post_port_sfp_info_to_db(logical_port_name, port_mapping, table, transceiver_dict,
                             stop_event=threading.Event()):
    ganged_port = False
    ganged_member_num = 1

    physical_port_list = port_mapping.logical_port_name_to_physical_port_list(logical_port_name)
    if physical_port_list is None:
        helper_logger.log_error("No physical ports found for logical port '{}'".format(logical_port_name))
        return PHYSICAL_PORT_NOT_EXIST

    if len(physical_port_list) > 1:
        ganged_port = True

    for physical_port in physical_port_list:
        if stop_event.is_set():
            break

        if not common._wrapper_get_presence(physical_port):
            helper_logger.log_notice("Transceiver not present in port {}".format(logical_port_name))
            continue

        port_name = common.get_physical_port_name(logical_port_name, ganged_member_num, ganged_port)
        ganged_member_num += 1

        try:
            if physical_port in transceiver_dict:
                port_info_dict = transceiver_dict[physical_port]
            else:
                port_info_dict = _wrapper_get_transceiver_info(physical_port)
                transceiver_dict[physical_port] = port_info_dict
            if port_info_dict is not None:
                is_replaceable = _wrapper_is_replaceable(physical_port)
                # if cmis is supported by the module
                if 'cmis_rev' in port_info_dict:
                    fvs = swsscommon.FieldValuePairs(
                        [(field, str(value)) for field, value in port_info_dict.items()] +
                        [('is_replaceable', str(is_replaceable))]
                    )
                # else cmis is not supported by the module
                else:
                    fvs = swsscommon.FieldValuePairs([
                        ('type', port_info_dict['type']),
                        ('vendor_rev', port_info_dict['vendor_rev']),
                        ('serial', port_info_dict['serial']),
                        ('manufacturer', port_info_dict['manufacturer']),
                        ('model', port_info_dict['model']),
                        ('vendor_oui', port_info_dict['vendor_oui']),
                        ('vendor_date', port_info_dict['vendor_date']),
                        ('connector', port_info_dict['connector']),
                        ('encoding', port_info_dict['encoding']),
                        ('ext_identifier', port_info_dict['ext_identifier']),
                        ('ext_rateselect_compliance', port_info_dict['ext_rateselect_compliance']),
                        ('cable_type', port_info_dict['cable_type']),
                        ('cable_length', str(port_info_dict['cable_length'])),
                        ('specification_compliance', port_info_dict['specification_compliance']),
                        ('nominal_bit_rate', str(port_info_dict['nominal_bit_rate'])),
                        ('application_advertisement', port_info_dict['application_advertisement']
                        if 'application_advertisement' in port_info_dict else 'N/A'),
                        ('is_replaceable', str(is_replaceable)),
                        ('dom_capability', port_info_dict['dom_capability']
                        if 'dom_capability' in port_info_dict else 'N/A')
                    ])
                table.set(port_name, fvs)
            else:
                return SFP_EEPROM_NOT_READY

        except NotImplementedError:
            helper_logger.log_error("This functionality is currently not implemented for this platform")
            sys.exit(NOT_IMPLEMENTED_ERROR)

def waiting_time_compensation_with_sleep(time_start, time_to_wait):
    time_now = time.time()
    time_diff = time_now - time_start
    if time_diff < time_to_wait:
        time.sleep(time_to_wait - time_diff)

# Delete port from SFP status table

# Thread wrapper class to update sfp state info periodically


class SfpStateUpdateTask(threading.Thread):
    RETRY_EEPROM_READING_INTERVAL = 60
    def __init__(self, namespaces, port_mapping, sfp_obj_dict, main_thread_stop_event, sfp_error_event):
        threading.Thread.__init__(self)
        self.name = "SfpStateUpdateTask"
        self.exc = None
        self.task_stopping_event = threading.Event()
        self.main_thread_stop_event = main_thread_stop_event
        self.sfp_error_event = sfp_error_event
        self.port_mapping = copy.deepcopy(port_mapping)
        # A set to hold those logical port name who fail to read EEPROM
        self.retry_eeprom_set = set()
        # To avoid retry EEPROM read too fast, record the last EEPROM read timestamp in this member
        self.last_retry_eeprom_time = 0
        # A dict to hold SFP error event, for SFP insert/remove event, it is not necessary to cache them
        # because _wrapper_get_presence returns the SFP presence status
        self.sfp_error_dict = {}
        self.sfp_insert_events = {}
        self.namespaces = namespaces
        self.sfp_obj_dict = sfp_obj_dict
        self.logger = syslogger.SysLogger(SYSLOG_IDENTIFIER_SFPSTATEUPDATETASK, enable_runtime_config=True)
        self.xcvr_table_helper = XcvrTableHelper(self.namespaces)
        self.dom_db_utils = DOMDBUtils(sfp_obj_dict, self.port_mapping, self.xcvr_table_helper, self.task_stopping_event, self.logger)
        self.vdm_db_utils = VDMDBUtils(sfp_obj_dict, self.port_mapping, self.xcvr_table_helper, self.task_stopping_event, self.logger)

    def _mapping_event_from_change_event(self, status, port_dict):
        """
        mapping from what get_transceiver_change_event returns to event defined in the state machine
        the logic is pretty straightforword
        """
        if status:
            if bool(port_dict):
                event = NORMAL_EVENT
            else:
                event = SYSTEM_BECOME_READY
                # here, a simple timeout event whose port_dict is empty is mapped
                # into a SYSTEM_BECOME_READY event so that it can be handled
                port_dict[EVENT_ON_ALL_SFP] = SYSTEM_BECOME_READY
        else:
            if EVENT_ON_ALL_SFP in port_dict.keys():
                event = port_dict[EVENT_ON_ALL_SFP]
            else:
                # this should not happen. just for protection
                event = SYSTEM_FAIL
                port_dict[EVENT_ON_ALL_SFP] = SYSTEM_FAIL

        helper_logger.log_debug("mapping from {} {} to {}".format(status, port_dict, event))
        return event

    # Update port sfp info and dom threshold in db during xcvrd bootup
    def _post_port_sfp_info_and_dom_thr_to_db_once(self, port_mapping, xcvr_table_helper, stop_event=threading.Event()):
        # Connect to STATE_DB and create transceiver dom/sfp info tables
        transceiver_dict = {}
        retry_eeprom_set = set()

        # Pre-fetch warm start status for all namespaces/ASICs
        warm_start_status = {}
        for namespace in self.namespaces:
            warm_start_status[namespace] = common.is_syncd_warm_restore_complete(namespace)

        # Post all the current interface sfp/dom threshold info to STATE_DB
        logical_port_list = port_mapping.logical_port_list
        for logical_port_name in logical_port_list:
            if stop_event.is_set():
                break

            # Get the asic to which this port belongs
            asic_index = port_mapping.get_asic_id_for_logical_port(logical_port_name)
            if asic_index is None:
                helper_logger.log_warning("Got invalid asic index for {}, ignored while posting SFP info during boot-up".format(logical_port_name))
                continue

            # Get warm start status for this ASIC's namespace
            namespace = common.get_namespace_from_asic_id(asic_index)
            is_warm_start = warm_start_status.get(namespace, False)

            rc = post_port_sfp_info_to_db(logical_port_name, port_mapping, xcvr_table_helper.get_intf_tbl(asic_index), transceiver_dict, stop_event)
            if rc != SFP_EEPROM_NOT_READY:
                if is_warm_start == False:
                    media_settings_parser.notify_media_setting(logical_port_name, transceiver_dict, xcvr_table_helper, port_mapping)
            else:
                retry_eeprom_set.add(logical_port_name)
        
        dom_thresholds_cache = {}
        vdm_thresholds_cache = {}
        for logical_port_name in logical_port_list:
            if stop_event.is_set():
                break
            
            if logical_port_name not in retry_eeprom_set:
                self.dom_db_utils.post_port_dom_thresholds_to_db(logical_port_name, db_cache=dom_thresholds_cache)
                # Read the VDM thresholds and post them to the DB
                self.vdm_db_utils.post_port_vdm_thresholds_to_db(logical_port_name, db_cache=vdm_thresholds_cache)

        return retry_eeprom_set

    # Init TRANSCEIVER_STATUS_SW table
    def _init_port_sfp_status_sw_tbl(self, port_mapping, xcvr_table_helper, stop_event=threading.Event()):
        # Init TRANSCEIVER_STATUS_SW table
        logical_port_list = port_mapping.logical_port_list
        for logical_port_name in logical_port_list:
            if stop_event.is_set():
                break

            # Get the asic to which this port belongs
            asic_index = port_mapping.get_asic_id_for_logical_port(logical_port_name)
            if asic_index is None:
                helper_logger.log_warning("Got invalid asic index for {}, ignored during sfp status table init".format(logical_port_name))
                continue

            physical_port_list = port_mapping.logical_port_name_to_physical_port_list(logical_port_name)
            if physical_port_list is None:
                helper_logger.log_error("No physical ports found for logical port '{}' during sfp status table init".format(logical_port_name))
                common.update_port_transceiver_status_table_sw(logical_port_name, xcvr_table_helper.get_status_sw_tbl(asic_index), sfp_status_helper.SFP_STATUS_REMOVED)

            for physical_port in physical_port_list:
                if stop_event.is_set():
                    break

                if not common._wrapper_get_presence(physical_port):
                    common.update_port_transceiver_status_table_sw(logical_port_name, xcvr_table_helper.get_status_sw_tbl(asic_index), sfp_status_helper.SFP_STATUS_REMOVED)
                else:
                    common.update_port_transceiver_status_table_sw(logical_port_name, xcvr_table_helper.get_status_sw_tbl(asic_index), sfp_status_helper.SFP_STATUS_INSERTED)

    def init(self):
        port_mapping_data = port_event_helper.get_port_mapping(self.namespaces)

        # Post all the current interface sfp/dom threshold info to STATE_DB
        self.retry_eeprom_set = self._post_port_sfp_info_and_dom_thr_to_db_once(port_mapping_data, self.xcvr_table_helper, self.main_thread_stop_event)
        helper_logger.log_notice("SfpStateUpdateTask: Posted all port DOM/SFP info to DB")

        # Init port sfp status sw table
        self._init_port_sfp_status_sw_tbl(port_mapping_data, self.xcvr_table_helper, self.main_thread_stop_event)
        helper_logger.log_notice("SfpStateUpdateTask: Initialized port sfp status table")

    def task_worker(self, stopping_event, sfp_error_event):

        helper_logger.log_info("Start SFP monitoring loop")

        transceiver_dict = {}
        # Start main loop to listen to the SFP change event.
        # The state migrating sequence:
        # 1. When the system starts, it is in "INIT" state, calling get_transceiver_change_event
        #    with RETRY_PERIOD_FOR_SYSTEM_READY_MSECS as timeout for before reach RETRY_TIMES_FOR_SYSTEM_READY
        #    times, otherwise it will transition to "EXIT" state
        # 2. Once 'system_become_ready' returned, the system enters "SYSTEM_READY" state and starts to monitor
        #    the insertion/removal event of all the SFP modules.
        #    In this state, receiving any system level event will be treated as an error and cause transition to
        #    "INIT" state
        # 3. When system back to "INIT" state, it will continue to handle system fail event, and retry until reach
        #    RETRY_TIMES_FOR_SYSTEM_READY times, otherwise it will transition to "EXIT" state

        # states definition
        # - Initial state: INIT, before received system ready or a normal event
        # - Final state: EXIT
        # - other state: NORMAL, after has received system-ready or a normal event

        # events definition
        # - SYSTEM_NOT_READY
        # - SYSTEM_BECOME_READY
        #   -
        # - NORMAL_EVENT
        #   - sfp insertion/removal
        #   - timeout returned by sfputil.get_change_event with status = true
        # - SYSTEM_FAIL

        # State transition:
        # 1. SYSTEM_NOT_READY
        #     - INIT
        #       - retry < RETRY_TIMES_FOR_SYSTEM_READY
        #             retry ++
        #       - else
        #             max retry reached, treat as fatal, transition to EXIT
        #     - NORMAL
        #         Treat as an error, transition to INIT
        # 2. SYSTEM_BECOME_READY
        #     - INIT
        #         transition to NORMAL
        #     - NORMAL
        #         log the event
        #         nop
        # 3. NORMAL_EVENT
        #     - INIT (for the vendors who don't implement SYSTEM_BECOME_READY)
        #         transition to NORMAL
        #         handle the event normally
        #     - NORMAL
        #         handle the event normally
        # 4. SYSTEM_FAIL
        #     - INIT
        #       - retry < RETRY_TIMES_FOR_SYSTEM_READY
        #             retry ++
        #       - else
        #             max retry reached, treat as fatal, transition to EXIT
        #     - NORMAL
        #         Treat as an error, transition to INIT

        # State           event               next state
        # INIT            SYSTEM NOT READY    INIT / EXIT
        # INIT            SYSTEM FAIL         INIT / EXIT
        # INIT            SYSTEM BECOME READY NORMAL
        # NORMAL          SYSTEM BECOME READY NORMAL
        # NORMAL          SYSTEM FAIL         INIT
        # INIT/NORMAL     NORMAL EVENT        NORMAL
        # NORMAL          SYSTEM NOT READY    INIT
        # EXIT            -

        retry = 0
        timeout = RETRY_PERIOD_FOR_SYSTEM_READY_MSECS
        state = STATE_INIT
        self.init()

        sel, asic_context = port_event_helper.subscribe_port_config_change(self.namespaces)
        while not stopping_event.is_set():
            port_event_helper.handle_port_config_change(sel, asic_context, stopping_event, self.port_mapping, helper_logger, self.on_port_config_change)

            # Retry those logical ports whose EEPROM reading failed or timeout when the SFP is inserted
            self.retry_eeprom_reading()
            next_state = state
            time_start = time.time()
            # Ensure not to block for any event if sfp insert event is pending
            if self.sfp_insert_events:
                timeout = SFP_INSERT_EVENT_POLL_PERIOD_MSECS
            status, port_dict, error_dict = _wrapper_get_transceiver_change_event(timeout)
            if status:
                # Soak SFP insert events across various ports (updates port_dict)
                _wrapper_soak_sfp_insert_event(self.sfp_insert_events, port_dict)
            if not port_dict:
                continue
            helper_logger.log_debug("Got event {} {} in state {}".format(status, port_dict, state))
            event = self._mapping_event_from_change_event(status, port_dict)
            if event == SYSTEM_NOT_READY:
                if state == STATE_INIT:
                    # system not ready, wait and retry
                    if retry >= RETRY_TIMES_FOR_SYSTEM_READY:
                        helper_logger.log_error("System failed to get ready in {} secs or received system error. Exiting...".format(
                            (RETRY_PERIOD_FOR_SYSTEM_READY_MSECS/1000)*RETRY_TIMES_FOR_SYSTEM_READY))
                        next_state = STATE_EXIT
                        sfp_error_event.set()
                    else:
                        retry = retry + 1

                        # get_transceiver_change_event may return immediately,
                        # we want the retry expired in expected time period,
                        # So need to calc the time diff,
                        # if time diff less that the pre-defined waiting time,
                        # use sleep() to complete the time.
                        time_now = time.time()
                        time_diff = time_now - time_start
                        if time_diff < RETRY_PERIOD_FOR_SYSTEM_READY_MSECS/1000:
                            time.sleep(RETRY_PERIOD_FOR_SYSTEM_READY_MSECS/1000 - time_diff)
                elif state == STATE_NORMAL:
                    helper_logger.log_error("Got system_not_ready in normal state, treat as fatal. Exiting...")
                    next_state = STATE_EXIT
                else:
                    next_state = STATE_EXIT
            elif event == SYSTEM_BECOME_READY:
                if state == STATE_INIT:
                    next_state = STATE_NORMAL
                    helper_logger.log_info("Got system_become_ready in init state, transition to normal state")
                elif state == STATE_NORMAL:
                    helper_logger.log_info("Got system_become_ready in normal state, ignored")
                else:
                    next_state = STATE_EXIT
            elif event == NORMAL_EVENT:
                if state == STATE_NORMAL or state == STATE_INIT:
                    if state == STATE_INIT:
                        next_state = STATE_NORMAL
                    # this is the originally logic that handled the transceiver change event
                    # this can be reached in two cases:
                    #   1. the state has been normal before got the event
                    #   2. the state was init and transition to normal after got the event.
                    #      this is for the vendors who don't implement "system_not_ready/system_becom_ready" logic
                    logical_port_dict = {}
                    for key, value in port_dict.items():
                        # SFP error event should be cached because: when a logical port is created, there is no way to
                        # detect the SFP error by platform API.
                        if value != sfp_status_helper.SFP_STATUS_INSERTED and value != sfp_status_helper.SFP_STATUS_REMOVED:
                            self.sfp_error_dict[key] = (value, error_dict)
                        else:
                            self.sfp_error_dict.pop(key, None)
                        logical_port_list = self.port_mapping.get_physical_to_logical(int(key))
                        if logical_port_list is None:
                            helper_logger.log_warning("Got unknown FP port index {}, ignored".format(key))
                            continue
                        for logical_port in logical_port_list:
                            logical_port_dict[logical_port] = value
                            # Get the asic to which this port belongs
                            asic_index = self.port_mapping.get_asic_id_for_logical_port(logical_port)
                            if asic_index is None:
                                helper_logger.log_warning("Got invalid asic index for {}, ignored".format(logical_port))
                                continue

                            if value == sfp_status_helper.SFP_STATUS_INSERTED:
                                helper_logger.log_notice("{}: Got SFP inserted event".format(logical_port))
                                # A plugin event will clear the error state.
                                common.update_port_transceiver_status_table_sw(
                                    logical_port, self.xcvr_table_helper.get_status_sw_tbl(asic_index), sfp_status_helper.SFP_STATUS_INSERTED)
                                helper_logger.log_notice("{}: received plug in and update port sfp status table.".format(logical_port))
                                rc = post_port_sfp_info_to_db(logical_port, self.port_mapping, self.xcvr_table_helper.get_intf_tbl(asic_index), transceiver_dict)
                                # If we didn't get the sfp info, assuming the eeprom is not ready, give a try again.
                                if rc == SFP_EEPROM_NOT_READY:
                                    helper_logger.log_warning("{}: SFP EEPROM is not ready. One more try...".format(logical_port))
                                    time.sleep(TIME_FOR_SFP_READY_SECS)
                                    rc = post_port_sfp_info_to_db(logical_port, self.port_mapping, self.xcvr_table_helper.get_intf_tbl(asic_index), transceiver_dict)
                                    if rc == SFP_EEPROM_NOT_READY:
                                        # If still failed to read EEPROM, put it to retry set
                                        self.retry_eeprom_set.add(logical_port)

                                if rc != SFP_EEPROM_NOT_READY:
                                    self.dom_db_utils.post_port_dom_thresholds_to_db(logical_port)
                                    self.vdm_db_utils.post_port_vdm_thresholds_to_db(logical_port)

                                    media_settings_parser.notify_media_setting(logical_port, transceiver_dict, self.xcvr_table_helper, self.port_mapping)
                                    transceiver_dict.clear()
                            elif value == sfp_status_helper.SFP_STATUS_REMOVED:
                                # Remove the SFP API object for this physical port
                                try:
                                    sfp = platform_chassis.get_sfp(int(key))
                                    sfp.remove_xcvr_api()
                                except (NotImplementedError, AttributeError) as e:
                                    helper_logger.log_error(f"Failed to remove xcvr api for port {key}: {str(e)}")
                                helper_logger.log_notice("{}: Got SFP removed event".format(logical_port))
                                state_port_table = self.xcvr_table_helper.get_state_port_tbl(asic_index)
                                state_port_table.set(logical_port, [(NPU_SI_SETTINGS_SYNC_STATUS_KEY, NPU_SI_SETTINGS_DEFAULT_VALUE)])
                                common.update_port_transceiver_status_table_sw(
                                    logical_port, self.xcvr_table_helper.get_status_sw_tbl(asic_index), sfp_status_helper.SFP_STATUS_REMOVED)
                                helper_logger.log_notice("{}: received plug out and update port sfp status table.".format(logical_port))
                                common.del_port_sfp_dom_info_from_db(logical_port, self.port_mapping, [
                                                              self.xcvr_table_helper.get_intf_tbl(asic_index),
                                                              self.xcvr_table_helper.get_dom_tbl(asic_index),
                                                              self.xcvr_table_helper.get_dom_temperature_tbl(asic_index),
                                                              self.xcvr_table_helper.get_dom_flag_tbl(asic_index),
                                                              self.xcvr_table_helper.get_dom_flag_change_count_tbl(asic_index),
                                                              self.xcvr_table_helper.get_dom_flag_set_time_tbl(asic_index),
                                                              self.xcvr_table_helper.get_dom_flag_clear_time_tbl(asic_index),
                                                              self.xcvr_table_helper.get_dom_threshold_tbl(asic_index),
                                                              *[self.xcvr_table_helper.get_vdm_threshold_tbl(asic_index, key) for key in VDM_THRESHOLD_TYPES],
                                                              self.xcvr_table_helper.get_vdm_real_value_tbl(asic_index),
                                                              *[self.xcvr_table_helper.get_vdm_flag_tbl(asic_index, key) for key in VDM_THRESHOLD_TYPES],
                                                              *[self.xcvr_table_helper.get_vdm_flag_change_count_tbl(asic_index, key) for key in VDM_THRESHOLD_TYPES],
                                                              *[self.xcvr_table_helper.get_vdm_flag_set_time_tbl(asic_index, key) for key in VDM_THRESHOLD_TYPES],
                                                              *[self.xcvr_table_helper.get_vdm_flag_clear_time_tbl(asic_index, key) for key in VDM_THRESHOLD_TYPES],
                                                              self.xcvr_table_helper.get_status_tbl(asic_index),
                                                              self.xcvr_table_helper.get_status_flag_tbl(asic_index),
                                                              self.xcvr_table_helper.get_status_flag_change_count_tbl(asic_index),
                                                              self.xcvr_table_helper.get_status_flag_set_time_tbl(asic_index),
                                                              self.xcvr_table_helper.get_status_flag_clear_time_tbl(asic_index),
                                                              self.xcvr_table_helper.get_pm_tbl(asic_index),
                                                              self.xcvr_table_helper.get_firmware_info_tbl(asic_index)
                                                              ])
                            else:
                                try:
                                    error_bits = int(value)
                                    helper_logger.log_error("{}: Got SFP error event {}".format(logical_port, value))

                                    error_descriptions = sfp_status_helper.fetch_generic_error_description(error_bits)

                                    if sfp_status_helper.has_vendor_specific_error(error_bits):
                                        if error_dict:
                                            vendor_specific_error_description = error_dict.get(key)
                                        else:
                                            vendor_specific_error_description = _wrapper_get_sfp_error_description(key)
                                        error_descriptions.append(vendor_specific_error_description)

                                    # Add error info to database
                                    # Any existing error will be replaced by the new one.
                                    common.update_port_transceiver_status_table_sw(logical_port, self.xcvr_table_helper.get_status_sw_tbl(asic_index), value, '|'.join(error_descriptions))
                                    helper_logger.log_notice("{}: Receive error update port sfp status table.".format(logical_port))
                                    # In this case EEPROM is not accessible. The DOM info will be removed since it can be out-of-date.
                                    # The interface info remains in the DB since it is static.
                                    if sfp_status_helper.is_error_block_eeprom_reading(error_bits):
                                        common.del_port_sfp_dom_info_from_db(logical_port,
                                                                      self.port_mapping, [
                                                                      self.xcvr_table_helper.get_dom_tbl(asic_index),
                                                                      self.xcvr_table_helper.get_dom_temperature_tbl(asic_index),
                                                                      self.xcvr_table_helper.get_dom_flag_tbl(asic_index),
                                                                      self.xcvr_table_helper.get_dom_flag_change_count_tbl(asic_index),
                                                                      self.xcvr_table_helper.get_dom_flag_set_time_tbl(asic_index),
                                                                      self.xcvr_table_helper.get_dom_flag_clear_time_tbl(asic_index),
                                                                      self.xcvr_table_helper.get_dom_threshold_tbl(asic_index),
                                                                      *[self.xcvr_table_helper.get_vdm_threshold_tbl(asic_index, key) for key in VDM_THRESHOLD_TYPES],
                                                                      self.xcvr_table_helper.get_vdm_real_value_tbl(asic_index),
                                                                      *[self.xcvr_table_helper.get_vdm_flag_tbl(asic_index, key) for key in VDM_THRESHOLD_TYPES],
                                                                      *[self.xcvr_table_helper.get_vdm_flag_change_count_tbl(asic_index, key) for key in VDM_THRESHOLD_TYPES],
                                                                      *[self.xcvr_table_helper.get_vdm_flag_set_time_tbl(asic_index, key) for key in VDM_THRESHOLD_TYPES],
                                                                      *[self.xcvr_table_helper.get_vdm_flag_clear_time_tbl(asic_index, key) for key in VDM_THRESHOLD_TYPES],
                                                                      self.xcvr_table_helper.get_status_tbl(asic_index),
                                                                      self.xcvr_table_helper.get_status_flag_tbl(asic_index),
                                                                      self.xcvr_table_helper.get_status_flag_change_count_tbl(asic_index),
                                                                      self.xcvr_table_helper.get_status_flag_set_time_tbl(asic_index),
                                                                      self.xcvr_table_helper.get_status_flag_clear_time_tbl(asic_index),
                                                                      self.xcvr_table_helper.get_pm_tbl(asic_index),
                                                                      self.xcvr_table_helper.get_firmware_info_tbl(asic_index)
                                                                      ])
                                except (TypeError, ValueError) as e:
                                    helper_logger.log_error("{}: Got unrecognized event {}, ignored".format(logical_port, value))

                else:
                    next_state = STATE_EXIT
            elif event == SYSTEM_FAIL:
                if state == STATE_INIT:
                    # To overcome a case that system is only temporarily not available,
                    # when get system fail event will wait and retry for a certain period,
                    # if system recovered in this period xcvrd will transit to INIT state
                    # and continue run, if can not recover then exit.
                    if retry >= RETRY_TIMES_FOR_SYSTEM_FAIL:
                        helper_logger.log_error("System failed to recover in {} secs. Exiting...".format(
                            (RETRY_PERIOD_FOR_SYSTEM_FAIL_MSECS/1000)*RETRY_TIMES_FOR_SYSTEM_FAIL))
                        next_state = STATE_EXIT
                        sfp_error_event.set()
                    else:
                        retry = retry + 1
                        waiting_time_compensation_with_sleep(time_start, RETRY_PERIOD_FOR_SYSTEM_FAIL_MSECS/1000)
                elif state == STATE_NORMAL:
                    helper_logger.log_error("Got system_fail in normal state, treat as error, transition to INIT...")
                    next_state = STATE_INIT
                    timeout = RETRY_PERIOD_FOR_SYSTEM_FAIL_MSECS
                    retry = 0
                else:
                    next_state = STATE_EXIT
            else:
                helper_logger.log_warning("Got unknown event {} on state {}.".format(event, state))

            if next_state != state:
                helper_logger.log_debug("State transition from {} to {}".format(state, next_state))
                state = next_state

            if next_state == STATE_EXIT:
                os.kill(os.getppid(), signal.SIGTERM)
                break
            elif next_state == STATE_NORMAL:
                timeout = STATE_MACHINE_UPDATE_PERIOD_MSECS

        helper_logger.log_info("Stop SFP monitoring loop")

    def run(self):
        self.thread_id = threading.current_thread().ident
        if self.task_stopping_event.is_set():
            return
        try:
            self.task_worker(self.task_stopping_event, self.sfp_error_event)
        except Exception as e:
            helper_logger.log_error("Exception occured at {} thread due to {}".format(threading.current_thread().name, repr(e)))
            common.log_exception_traceback()
            self.exc = e
            self.main_thread_stop_event.set()

    # SfpStateUpdateTask thread has a call to an API which could potentially sleep in the order of seconds and hence,
    # could block the xcvrd daemon graceful shutdown process for a prolonged time. Raising an exception will allow us to
    # interrupt the SfpStateUpdateTask thread while sleeping and will allow graceful shutdown of the thread
    def raise_exception(self):
        res = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(self.thread_id),
              ctypes.py_object(SystemExit))
        if res > 1:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(self.thread_id), 0)
            helper_logger.log_error('Exception raise failure for SfpStateUpdateTask')

    def join(self):
        self.task_stopping_event.set()
        threading.Thread.join(self)
        if self.exc:
            raise self.exc

    def on_port_config_change(self , port_change_event):
        if port_change_event.event_type == port_event_helper.PortChangeEvent.PORT_REMOVE:
            self.on_remove_logical_port(port_change_event)
            self.port_mapping.handle_port_change_event(port_change_event)
        elif port_change_event.event_type == port_event_helper.PortChangeEvent.PORT_ADD:
            self.port_mapping.handle_port_change_event(port_change_event)
            self.on_add_logical_port(port_change_event)

    def on_remove_logical_port(self, port_change_event):
        """Called when a logical port is removed from CONFIG_DB.

        Args:
            port_change_event (object): port change event
        """
        # To avoid race condition, remove the entry TRANSCEIVER_DOM_INFO, TRANSCEIVER_STATUS_INFO and TRANSCEIVER_INFO table.
        # The operation to remove entry from TRANSCEIVER_DOM_INFO is duplicate with DomInfoUpdateTask.on_remove_logical_port,
        # but it is necessary because TRANSCEIVER_DOM_INFO is also updated in this thread when a new SFP is inserted.
        common.del_port_sfp_dom_info_from_db(port_change_event.port_name,
                                      self.port_mapping, [
                                      self.xcvr_table_helper.get_intf_tbl(port_change_event.asic_id),
                                      self.xcvr_table_helper.get_dom_tbl(port_change_event.asic_id),
                                      self.xcvr_table_helper.get_dom_temperature_tbl(port_change_event.asic_id),
                                      self.xcvr_table_helper.get_dom_flag_tbl(port_change_event.asic_id),
                                      self.xcvr_table_helper.get_dom_flag_change_count_tbl(port_change_event.asic_id),
                                      self.xcvr_table_helper.get_dom_flag_set_time_tbl(port_change_event.asic_id),
                                      self.xcvr_table_helper.get_dom_flag_clear_time_tbl(port_change_event.asic_id),
                                      self.xcvr_table_helper.get_dom_threshold_tbl(port_change_event.asic_id),
                                      *[self.xcvr_table_helper.get_vdm_threshold_tbl(port_change_event.asic_id, key) for key in VDM_THRESHOLD_TYPES],
                                      self.xcvr_table_helper.get_vdm_real_value_tbl(port_change_event.asic_id),
                                      *[self.xcvr_table_helper.get_vdm_flag_tbl(port_change_event.asic_id, key) for key in VDM_THRESHOLD_TYPES],
                                      *[self.xcvr_table_helper.get_vdm_flag_change_count_tbl(port_change_event.asic_id, key) for key in VDM_THRESHOLD_TYPES],
                                      *[self.xcvr_table_helper.get_vdm_flag_set_time_tbl(port_change_event.asic_id, key) for key in VDM_THRESHOLD_TYPES],
                                      *[self.xcvr_table_helper.get_vdm_flag_clear_time_tbl(port_change_event.asic_id, key) for key in VDM_THRESHOLD_TYPES],
                                      self.xcvr_table_helper.get_status_tbl(port_change_event.asic_id),
                                      self.xcvr_table_helper.get_status_flag_tbl(port_change_event.asic_id),
                                      self.xcvr_table_helper.get_status_flag_change_count_tbl(port_change_event.asic_id),
                                      self.xcvr_table_helper.get_status_flag_set_time_tbl(port_change_event.asic_id),
                                      self.xcvr_table_helper.get_status_flag_clear_time_tbl(port_change_event.asic_id),
                                      self.xcvr_table_helper.get_status_sw_tbl(port_change_event.asic_id),
                                      self.xcvr_table_helper.get_pm_tbl(port_change_event.asic_id),
                                      self.xcvr_table_helper.get_firmware_info_tbl(port_change_event.asic_id)
                                      ])

        # The logical port has been removed, no need retry EEPROM reading
        if port_change_event.port_name in self.retry_eeprom_set:
            self.retry_eeprom_set.remove(port_change_event.port_name)

    def on_add_logical_port(self, port_change_event):
        """Called when a logical port is added

        Args:
            port_change_event (object): port change event

        Returns:
            dict: key is logical port name, value is SFP status
        """
        # A logical port is created. There could be 3 cases:
        #  1. SFP is present with no SFP error. Need query the SFP status by platform API and
        #     insert the data to DB.
        #  2. SFP is present with SFP error. If the SFP error does not block EEPROM reading,
        #     just query transceiver information and DOM sensor information via platform API and update the data to DB; otherwise,
        #     just update TRANSCEIVER_STATUS_SW table with the error.
        #  3. SFP is not present. Only update TRANSCEIVER_STATUS_INFO table.
        status_sw_tbl = self.xcvr_table_helper.get_status_sw_tbl(port_change_event.asic_id)
        int_tbl = self.xcvr_table_helper.get_intf_tbl(port_change_event.asic_id)
        # Initialize the NPU_SI_SETTINGS_SYNC_STATUS to default value
        state_port_table = self.xcvr_table_helper.get_state_port_tbl(port_change_event.asic_id)
        found, state_port_table_fvs = state_port_table.get(port_change_event.port_name)
        if not found:
            helper_logger.log_notice("Add logical port: Creating STATE_DB PORT_TABLE as unable to find for lport {}".format(port_change_event.port_name))
            state_port_table_fvs = []
        state_port_table.set(port_change_event.port_name, [(NPU_SI_SETTINGS_SYNC_STATUS_KEY,
                                                      NPU_SI_SETTINGS_DEFAULT_VALUE)])
        helper_logger.log_notice("Add logical port: Initialized NPU_SI_SETTINGS_SYNC_STATUS for lport {}".format(port_change_event.port_name))

        error_description = 'N/A'
        status = None
        read_eeprom = True
        if port_change_event.port_index in self.sfp_error_dict:
            value, error_dict = self.sfp_error_dict[port_change_event.port_index]
            status = value
            error_bits = int(value)
            helper_logger.log_info("Got SFP error event {}".format(value))

            error_descriptions = sfp_status_helper.fetch_generic_error_description(error_bits)

            if sfp_status_helper.has_vendor_specific_error(error_bits):
                if error_dict:
                    vendor_specific_error_description = error_dict.get(port_change_event.port_index)
                else:
                    vendor_specific_error_description = _wrapper_get_sfp_error_description(port_change_event.port_index)
                error_descriptions.append(vendor_specific_error_description)

            error_description = '|'.join(error_descriptions)
            helper_logger.log_info("Receive error update port sfp status table.")
            if sfp_status_helper.is_error_block_eeprom_reading(error_bits):
                read_eeprom = False

        # SFP information not in DB
        if common._wrapper_get_presence(port_change_event.port_index) and read_eeprom:
            transceiver_dict = {}
            status = sfp_status_helper.SFP_STATUS_INSERTED if not status else status
            rc = post_port_sfp_info_to_db(port_change_event.port_name, self.port_mapping, int_tbl, transceiver_dict)
            if rc == SFP_EEPROM_NOT_READY:
                # Failed to read EEPROM, put it to retry set
                self.retry_eeprom_set.add(port_change_event.port_name)
            else:
                self.dom_db_utils.post_port_dom_thresholds_to_db(port_change_event.port_name)
                self.vdm_db_utils.post_port_vdm_thresholds_to_db(port_change_event.port_name)
                media_settings_parser.notify_media_setting(port_change_event.port_name, transceiver_dict, self.xcvr_table_helper, self.port_mapping)
        else:
            status = sfp_status_helper.SFP_STATUS_REMOVED if not status else status
        common.update_port_transceiver_status_table_sw(port_change_event.port_name, status_sw_tbl, status, error_description)

    def retry_eeprom_reading(self):
        """Retry EEPROM reading, if retry succeed, remove the logical port from the retry set
        """
        if not self.retry_eeprom_set:
            return

        # Retry eeprom with an interval RETRY_EEPROM_READING_INTERVAL. No need to put sleep here
        # because _wrapper_get_transceiver_change_event has a timeout argument.
        now = time.time()
        if now - self.last_retry_eeprom_time < self.RETRY_EEPROM_READING_INTERVAL:
            return

        self.last_retry_eeprom_time = now

        transceiver_dict = {}
        retry_success_set = set()
        for logical_port in self.retry_eeprom_set:
            asic_index = self.port_mapping.get_asic_id_for_logical_port(logical_port)
            rc = post_port_sfp_info_to_db(logical_port, self.port_mapping, self.xcvr_table_helper.get_intf_tbl(asic_index), transceiver_dict)
            if rc != SFP_EEPROM_NOT_READY:
                self.dom_db_utils.post_port_dom_thresholds_to_db(logical_port)
                self.vdm_db_utils.post_port_vdm_thresholds_to_db(logical_port)

                media_settings_parser.notify_media_setting(logical_port, transceiver_dict, self.xcvr_table_helper, self.port_mapping)
                transceiver_dict.clear()
                retry_success_set.add(logical_port)
        # Update retry EEPROM set
        self.retry_eeprom_set -= retry_success_set

    def update_log_level(self):
        """Call the logger's update log level method.
        """
        return self.logger.update_log_level()


#
# Daemon =======================================================================
#


class DaemonXcvrd(daemon_base.DaemonBase):
    def __init__(self, log_identifier, skip_cmis_mgr=False, enable_sff_mgr=False, dom_temperature_poll_interval=None):
        super(DaemonXcvrd, self).__init__(log_identifier, enable_runtime_log_config=True)
        self.stop_event = threading.Event()
        self.sfp_error_event = threading.Event()
        self.skip_cmis_mgr = skip_cmis_mgr
        self.enable_sff_mgr = enable_sff_mgr
        self.dom_temperature_poll_interval = dom_temperature_poll_interval
        self.namespaces = ['']
        self.threads = []
        self.sfp_obj_dict = {}

    def update_loggers_log_level(self):
        """
        Update log level for all loggers
        """
        helper_logger.update_log_level()
        self.logger_instance.update_log_level()
        for thread in self.threads:
            update_log_level = getattr(thread, 'update_log_level', None)
            if update_log_level and callable(update_log_level):
                thread.update_log_level()

    # Signal handler
    def signal_handler(self, sig, frame):
        if sig == signal.SIGHUP:
            self.log_notice("Caught SIGHUP...")
            self.update_loggers_log_level()
        elif sig == signal.SIGINT:
            self.log_info("Caught SIGINT - exiting...")
            self.stop_event.set()
        elif sig == signal.SIGTERM:
            self.log_info("Caught SIGTERM - exiting...")
            self.stop_event.set()
        else:
            self.log_warning("Caught unhandled signal '" + sig + "'")

    # Wait for port config is done
    def wait_for_port_config_done(self, namespace):
        # Connect to APPL_DB and subscribe to PORT table notifications
        appl_db = daemon_base.db_connect("APPL_DB", namespace=namespace)

        sel = swsscommon.Select()
        port_tbl = swsscommon.SubscriberStateTable(appl_db, swsscommon.APP_PORT_TABLE_NAME)
        sel.addSelectable(port_tbl)

        # Make sure this daemon started after all port configured
        while not self.stop_event.is_set():
            (state, c) = sel.select(port_event_helper.SELECT_TIMEOUT_MSECS)
            if state == swsscommon.Select.TIMEOUT:
                continue
            if state != swsscommon.Select.OBJECT:
                self.log_warning("sel.select() did not return swsscommon.Select.OBJECT")
                continue

            (key, op, fvp) = port_tbl.pop()
            if key in ["PortConfigDone", "PortInitDone"]:
                break

    """
    Initialize NPU_SI_SETTINGS_SYNC_STATUS_KEY field in STATE_DB PORT_TABLE|<lport>
    if not already present for a port.
    """
    def initialize_port_init_control_fields_in_port_table(self, port_mapping_data):
        logical_port_list = port_mapping_data.logical_port_list
        for lport in logical_port_list:
            asic_index = port_mapping_data.get_asic_id_for_logical_port(lport)
            state_port_table  = self.xcvr_table_helper.get_state_port_tbl(asic_index)
            if state_port_table is None:
                helper_logger.log_error("Port init control: state_port_tbl is None for lport {}".format(lport))
                continue

            found, state_port_table_fvs = state_port_table.get(lport)
            if not found:
                self.log_notice("Port init control: Creating STATE_DB PORT_TABLE as unable to find for lport {}".format(lport))
                state_port_table_fvs = []
            state_port_table_fvs_dict = dict(state_port_table_fvs)
            if NPU_SI_SETTINGS_SYNC_STATUS_KEY not in state_port_table_fvs_dict:
                state_port_table.set(lport, [(NPU_SI_SETTINGS_SYNC_STATUS_KEY,
                                              NPU_SI_SETTINGS_DEFAULT_VALUE)])
                self.log_notice("Port init control: Initialized NPU_SI_SETTINGS_SYNC_STATUS for lport {}".format(lport))

        self.log_notice("XCVRD INIT: Port init control fields initialized in STATE_DB PORT_TABLE")

    def initialize_sfp_obj_dict(self, port_mapping_data):
        """
        Create a dictionary mapping physical ports to their corresponding SFP objects.

        Args:
            port_mapping_data (PortMapping): The port mapping data.

        Returns:
            Dict[int, Sfp]: A dictionary mapping physical ports to SFP objects.
        """
        if port_mapping_data is None or port_mapping_data.physical_to_logical is None:
            self.log_error("SFP OBJ INIT: Failed to get port mapping data")
            return {}

        physical_port_list = port_mapping_data.physical_to_logical.keys()
        sfp_obj_dict = {}
        for physical_port in physical_port_list:
            try:
                sfp_obj_dict[physical_port] = platform_chassis.get_sfp(physical_port)
            except Exception as e:
                self.log_error(f"SFP OBJ INIT: Failed to get SFP object for port {physical_port} due to {repr(e)}")

        return sfp_obj_dict

    def remove_stale_transceiver_info(self, port_mapping_data):
        """
        Remove stale entries from the TRANSCEIVER_INFO table for ports where the transceiver is no longer present.

        This function iterates through all logical ports in the provided port mapping data. For each port:
        - It checks if the TRANSCEIVER_INFO table entry exists.
        - If the entry exists and the transceiver is absent, the entry is removed from the table.

        Args:
            port_mapping_data (PortMapping): The port mapping data containing logical-to-physical port mappings.

        Returns:
            None
        """
        logical_port_list = port_mapping_data.logical_port_list
        for lport in logical_port_list:
            asic_index = port_mapping_data.get_asic_id_for_logical_port(lport)
            intf_tbl = self.xcvr_table_helper.get_intf_tbl(asic_index)
            if not intf_tbl:
                continue

            found, _ = intf_tbl.get(lport)
            if found:
                # If transceiver is absent, remove the entry from TRANSCEIVER_INFO table
                pport_list = port_mapping_data.get_logical_to_physical(lport)
                if not pport_list:
                    self.log_error(f"Remove stale transceiver info: No physical port found for lport {lport}")
                    continue
                pport = pport_list[0]

                if not common._wrapper_get_presence(pport):
                    self.log_notice(f"Remove stale transceiver info: Transceiver is absent for lport {lport}")
                    common.del_port_sfp_dom_info_from_db(lport, port_mapping_data, [intf_tbl])

    # Initialize daemon
    def init(self):
        global platform_sfputil
        global platform_chassis

        self.log_notice("XCVRD INIT: Start daemon init...")

        # Load new platform api class
        try:
            import sonic_platform.platform
            platform_chassis = sonic_platform.platform.Platform().get_chassis()
            self.log_info("chassis loaded {}".format(platform_chassis))
        except Exception as e:
            self.log_warning("Failed to load chassis due to {}".format(repr(e)))

        # Load platform specific sfputil class
        if platform_chassis is None:
            try:
                platform_sfputil = self.load_platform_util(PLATFORM_SPECIFIC_MODULE_NAME, PLATFORM_SPECIFIC_CLASS_NAME)
            except Exception as e:
                self.log_error("Failed to load sfputil: {}".format(str(e)), True)
                sys.exit(SFPUTIL_LOAD_ERROR)

        # Initialize shared utilities with platform objects
        common.init_globals(platform_chassis, platform_sfputil)

        if multi_asic.is_multi_asic():
            # Load the namespace details first from the database_global.json file.
            swsscommon.SonicDBConfig.initializeGlobalConfig()
        # To prevent race condition in get_all_namespaces() we cache the namespaces before
        # creating any worker threads
        self.namespaces = multi_asic.get_front_end_namespaces()

        # Initialize xcvr table helper
        self.xcvr_table_helper = XcvrTableHelper(self.namespaces)

        if common.is_fast_reboot_enabled():
            self.log_info("Skip loading media_settings.json and optics_si_settings.json in case of fast-reboot")
        else:
            media_settings_parser.load_media_settings()
            optics_si_parser.load_optics_si_settings()

        # Make sure this daemon started after all port configured
        self.log_notice("XCVRD INIT: Wait for port config is done")
        for namespace in self.namespaces:
            self.wait_for_port_config_done(namespace)

        self.log_notice("XCVRD INIT: After port config is done")
        port_mapping_data = port_event_helper.get_port_mapping(self.namespaces)

        self.initialize_port_init_control_fields_in_port_table(port_mapping_data)
        self.sfp_obj_dict = self.initialize_sfp_obj_dict(port_mapping_data)

        # Remove the TRANSCEIVER_INFO table if the transceiver is absent.
        # This ensures stale entries are cleaned up when a transceiver is removed while xcvrd is not running.
        # Performed in the init() method to ensure the table is cleared before starting child threads.
        # Note: Other transceiver-related tables are cleared during xcvrd deinitialization.
        self.remove_stale_transceiver_info(port_mapping_data)

        return port_mapping_data

    # Deinitialize daemon
    def deinit(self):
        self.log_info("Start daemon deinit...")

        # Pre-fetch warm/fast reboot status for all namespaces/ASICs
        is_fast_reboot = common.is_fast_reboot_enabled()
        warm_fast_reboot_status = {}
        for namespace in self.namespaces:
            warm_fast_reboot_status[namespace] = common.is_syncd_warm_restore_complete(namespace) or is_fast_reboot

        # Delete all the information from DB and then exit
        port_mapping_data = port_event_helper.get_port_mapping(self.namespaces)
        logical_port_list = port_mapping_data.logical_port_list
        for logical_port_name in logical_port_list:
            # Get the asic to which this port belongs
            asic_index = port_mapping_data.get_asic_id_for_logical_port(logical_port_name)
            if asic_index is None:
                helper_logger.log_warning("Got invalid asic index for {}, ignored".format(logical_port_name))
                continue

            # Get warm/fast reboot status for this ASIC's namespace
            namespace = common.get_namespace_from_asic_id(asic_index)
            is_warm_fast_reboot = warm_fast_reboot_status.get(namespace, False)

            # Skip deleting intf_tbl for avoiding OA to trigger Tx disable signal
            # due to TRANSCEIVER_INFO table deletion during xcvrd shutdown/crash
            intf_tbl = None

            common.del_port_sfp_dom_info_from_db(logical_port_name, port_mapping_data, [
                                          intf_tbl,
                                          self.xcvr_table_helper.get_dom_tbl(asic_index),
                                          self.xcvr_table_helper.get_dom_temperature_tbl(asic_index),
                                          self.xcvr_table_helper.get_dom_flag_tbl(asic_index),
                                          self.xcvr_table_helper.get_dom_flag_change_count_tbl(asic_index),
                                          self.xcvr_table_helper.get_dom_flag_set_time_tbl(asic_index),
                                          self.xcvr_table_helper.get_dom_flag_clear_time_tbl(asic_index),
                                          self.xcvr_table_helper.get_dom_threshold_tbl(asic_index),
                                          *[self.xcvr_table_helper.get_vdm_threshold_tbl(asic_index, key) for key in VDM_THRESHOLD_TYPES],
                                          self.xcvr_table_helper.get_vdm_real_value_tbl(asic_index),
                                          *[self.xcvr_table_helper.get_vdm_flag_tbl(asic_index, key) for key in VDM_THRESHOLD_TYPES],
                                          *[self.xcvr_table_helper.get_vdm_flag_change_count_tbl(asic_index, key) for key in VDM_THRESHOLD_TYPES],
                                          *[self.xcvr_table_helper.get_vdm_flag_set_time_tbl(asic_index, key) for key in VDM_THRESHOLD_TYPES],
                                          *[self.xcvr_table_helper.get_vdm_flag_clear_time_tbl(asic_index, key) for key in VDM_THRESHOLD_TYPES],
                                          self.xcvr_table_helper.get_status_flag_tbl(asic_index),
                                          self.xcvr_table_helper.get_status_flag_change_count_tbl(asic_index),
                                          self.xcvr_table_helper.get_status_flag_set_time_tbl(asic_index),
                                          self.xcvr_table_helper.get_status_flag_clear_time_tbl(asic_index),
                                          self.xcvr_table_helper.get_pm_tbl(asic_index),
                                          self.xcvr_table_helper.get_firmware_info_tbl(asic_index)
                                          ])

            if not is_warm_fast_reboot:
                common.del_port_sfp_dom_info_from_db(logical_port_name, port_mapping_data, [
                                          self.xcvr_table_helper.get_status_tbl(asic_index),
                                          self.xcvr_table_helper.get_status_sw_tbl(asic_index),
                                          ])

        del globals()['platform_chassis']

    # Run daemon

    def run(self):
        self.log_notice("Starting up...")

        # Start daemon initialization sequence
        port_mapping_data = self.init()

        # Start the SFF manager
        sff_manager = None
        if self.enable_sff_mgr:
            sff_manager = SffManagerTask(self.namespaces, self.stop_event, platform_chassis, helper_logger)
            sff_manager.start()
            self.threads.append(sff_manager)
        else:
            self.log_notice("Skipping SFF Task Manager")

        # Start the CMIS manager
        cmis_manager = None
        if not self.skip_cmis_mgr:
            cmis_manager = CmisManagerTask(self.namespaces, port_mapping_data, self.stop_event, skip_cmis_mgr=self.skip_cmis_mgr, platform_chassis=platform_chassis)
            cmis_manager.start()
            self.threads.append(cmis_manager)

        # Start the dom sensor info update thread
        dom_info_update = DomInfoUpdateTask(self.namespaces, port_mapping_data, self.sfp_obj_dict, self.stop_event, self.skip_cmis_mgr)
        dom_info_update.start()
        self.threads.append(dom_info_update)

        # Start the dom thermal sensor info update thread
        dom_thermal_info_update = None
        if self.dom_temperature_poll_interval is not None:
            dom_thermal_info_update = DomThermalInfoUpdateTask(self.namespaces, port_mapping_data, self.sfp_obj_dict, self.stop_event,
                                                               self.dom_temperature_poll_interval)
            dom_thermal_info_update.start()
            self.threads.append(dom_thermal_info_update)

        # Start the sfp state info update thread
        sfp_state_update = SfpStateUpdateTask(self.namespaces, port_mapping_data, self.sfp_obj_dict, self.stop_event, self.sfp_error_event)
        sfp_state_update.start()
        self.threads.append(sfp_state_update)

        # Start main loop
        self.log_notice("Start daemon main loop with thread count {}".format(len(self.threads)))
        for thread in self.threads:
            self.log_notice("Started thread {}".format(thread.name))

        self.stop_event.wait()

        self.log_notice("Stop daemon main loop")

        generate_sigkill = False
        # check all threads are alive
        for thread in self.threads:
            if thread.is_alive() is False:
                try:
                    thread.join()
                except Exception as e:
                    self.log_error("Xcvrd: exception found at child thread {} due to {}".format(thread.name, repr(e)))
                    generate_sigkill = True

        if generate_sigkill is True:
            self.log_error("Exiting main loop as child thread raised exception!")
            os.kill(os.getpid(), signal.SIGKILL)

        # Stop the SFF manager
        if sff_manager is not None:
            if sff_manager.is_alive():
                sff_manager.join()

        # Stop the CMIS manager
        if cmis_manager is not None:
            if cmis_manager.is_alive():
                cmis_manager.join()

        # Stop the dom sensor info update thread
        if dom_info_update.is_alive():
            dom_info_update.join()

        # Stop the dom thermal sensor info update thread
        if dom_thermal_info_update is not None:
            if dom_thermal_info_update.is_alive():
                dom_thermal_info_update.join()

        # Stop the sfp state info update thread
        if sfp_state_update.is_alive():
            sfp_state_update.raise_exception()
            sfp_state_update.join()

        # Start daemon deinitialization sequence
        self.deinit()

        self.log_info("Shutting down...")

        if self.sfp_error_event.is_set():
            sys.exit(SFP_SYSTEM_ERROR)


#
# Main =========================================================================
#

# This is our main entry point for xcvrd script


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--skip_cmis_mgr', action='store_true')
    parser.add_argument('--enable_sff_mgr', action='store_true')
    parser.add_argument('--dom_temperature_poll_interval', default=None, type=int)

    args = parser.parse_args()
    xcvrd = DaemonXcvrd(SYSLOG_IDENTIFIER, args.skip_cmis_mgr, args.enable_sff_mgr,
                        args.dom_temperature_poll_interval)
    xcvrd.run()


if __name__ == '__main__':
    main()
