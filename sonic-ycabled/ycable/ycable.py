#!/usr/bin/env python2

"""
    ycable
    Y-Cable interface/update daemon for SONiC
"""

try:
    import multiprocessing
    import os
    import signal
    import sys
    import threading
    import time
    
    from enum import Enum
    from sonic_py_common import daemon_base, device_info, logger
    from sonic_py_common import multi_asic
    from swsscommon import swsscommon

    from .ycable_utilities import y_cable_helper
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")

#
# Constants ====================================================================
#

SYSLOG_IDENTIFIER = "y_cable"

PLATFORM_SPECIFIC_MODULE_NAME = "sfputil"
PLATFORM_SPECIFIC_CLASS_NAME = "SfpUtil"

SELECT_TIMEOUT_MSECS = 1000

# Mgminit time required as per CMIS spec
MGMT_INIT_TIME_DELAY_SECS = 2

# SFP insert event poll duration
SFP_INSERT_EVENT_POLL_PERIOD_MSECS = 1000

YCABLE_INFO_UPDATE_PERIOD_SECS = 60
TIME_FOR_SFP_READY_SECS = 1
XCVRD_MAIN_THREAD_SLEEP_SECS = 60

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


SFP_STATUS_REMOVED = '0'
SFP_STATUS_INSERTED = '1'

# SFP error code enum, new elements can be added to the enum if new errors need to be supported.
SFP_STATUS_ERR_ENUM = Enum('SFP_STATUS_ERR_ENUM', ['SFP_STATUS_ERR_I2C_STUCK', 'SFP_STATUS_ERR_BAD_EEPROM',
                                                   'SFP_STATUS_ERR_UNSUPPORTED_CABLE', 'SFP_STATUS_ERR_HIGH_TEMP',
                                                   'SFP_STATUS_ERR_BAD_CABLE'], start=2)

# Convert the error code to string and store them in a set for convenience
errors_block_eeprom_reading = set(str(error_code.value) for error_code in SFP_STATUS_ERR_ENUM)

TRANSCEIVER_STATUS_TABLE = 'TRANSCEIVER_STATUS'

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

# Global platform specific sfputil class instance
platform_sfputil = None
# Global chassis object based on new platform api
platform_chassis = None

# Global logger instance for helper functions and classes
# TODO: Refactor so that we only need the logger inherited
# by DaemonYcable
helper_logger = logger.Logger(SYSLOG_IDENTIFIER)

#
# Helper functions =============================================================
#

# Find out the underneath physical port list by logical name


def logical_port_name_to_physical_port_list(port_name):
    try:
        return [int(port_name)]
    except ValueError:
        if platform_sfputil.is_logical_port(port_name):
            return platform_sfputil.get_logical_to_physical(port_name)
        else:
            helper_logger.log_error("Invalid port '{}'".format(port_name))
            return None


def get_physical_port_name(logical_port, physical_port, ganged):
    if logical_port == physical_port:
        return logical_port
    elif ganged:
        return logical_port + ":{} (ganged)".format(physical_port)
    else:
        return logical_port

# Strip units and beautify

def _wrapper_get_presence(physical_port):
    if platform_chassis is not None:
        try:
            return platform_chassis.get_sfp(physical_port).get_presence()
        except NotImplementedError:
            pass
    return platform_sfputil.get_presence(physical_port)


def _wrapper_get_transceiver_info(physical_port):
    if platform_chassis is not None:
        try:
            return platform_chassis.get_sfp(physical_port).get_transceiver_info()
        except NotImplementedError:
            pass
    return platform_sfputil.get_transceiver_info_dict(physical_port)


# Soak SFP insert event until management init completes
def _wrapper_soak_sfp_insert_event(sfp_insert_events, port_dict):
    for key, value in list(port_dict.items()):
        if value == SFP_STATUS_INSERTED:
            sfp_insert_events[key] = time.time()
            del port_dict[key]
        elif value == SFP_STATUS_REMOVED:
            if key in sfp_insert_events:
                del sfp_insert_events[key]

    for key, itime in list(sfp_insert_events.items()):
        if time.time() - itime >= MGMT_INIT_TIME_DELAY_SECS:
            port_dict[key] = SFP_STATUS_INSERTED
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

def detect_port_in_error_status(logical_port_name, status_tbl):
    rec, fvp = status_tbl.get(logical_port_name)
    if rec:
        status_dict = dict(fvp)
        if status_dict['status'] in errors_block_eeprom_reading:
            return True
        else:
            return False
    else:
        return False

#
# Helper classes ===============================================================
#

# Thread wrapper class to update dom info periodically


class YcableInfoUpdateTask(object):
    def __init__(self):
        self.task_thread = None
        self.task_stopping_event = threading.Event()

    def task_worker(self, y_cable_presence):
        helper_logger.log_info("Start Ycable monitoring loop")

        # Connect to STATE_DB and create transceiver dom info table
        state_db = {}
        mux_tbl = {}
        status_tbl = {}

        # Get the namespaces in the platform
        namespaces = multi_asic.get_front_end_namespaces()
        for namespace in namespaces:
            asic_id = multi_asic.get_asic_index_from_namespace(namespace)
            state_db[asic_id] = daemon_base.db_connect("STATE_DB", namespace)
            status_tbl[asic_id] = swsscommon.Table(state_db[asic_id], TRANSCEIVER_STATUS_TABLE)

        # Start loop to update dom info in DB periodically
        while not self.task_stopping_event.wait(YCABLE_INFO_UPDATE_PERIOD_SECS):
            logical_port_list = platform_sfputil.logical
            for logical_port_name in logical_port_list:
                # Get the asic to which this port belongs
                asic_index = platform_sfputil.get_asic_id_for_logical_port(logical_port_name)
                if asic_index is None:
                    logger.log_warning("Got invalid asic index for {}, ignored".format(logical_port_name))
                    continue

                if not detect_port_in_error_status(logical_port_name, status_tbl[asic_index]):
                    if y_cable_presence[0] is True:
                        y_cable_helper.check_identifier_presence_and_update_mux_info_entry(state_db, mux_tbl, asic_index, logical_port_name)

        helper_logger.log_info("Stop DOM monitoring loop")

    def task_run(self, y_cable_presence):
        if self.task_stopping_event.is_set():
            return

        self.task_thread = threading.Thread(target=self.task_worker, args=(y_cable_presence,))
        self.task_thread.start()

    def task_stop(self):
        self.task_stopping_event.set()
        self.task_thread.join()

# Process wrapper class to update sfp state info periodically


class YcableStateUpdateTask(object):
    def __init__(self):
        self.task_process = None
        self.task_stopping_event = multiprocessing.Event()
        self.sfp_insert_events = {}

    def task_worker(self, stopping_event, sfp_error_event, y_cable_presence):
        helper_logger.log_info("Start Ycable monitoring loop")

        # Connect to STATE_DB and create transceiver dom/sfp info tables
        state_db, appl_db, int_tbl, dom_tbl, status_tbl, app_port_tbl = {}, {}, {}, {}, {}, {}

        # Get the namespaces in the platform
        namespaces = multi_asic.get_front_end_namespaces()
        for namespace in namespaces:
            asic_id = multi_asic.get_asic_index_from_namespace(namespace)
            state_db[asic_id] = daemon_base.db_connect("STATE_DB", namespace)

        timeout = RETRY_PERIOD_FOR_SYSTEM_READY_MSECS
        while not stopping_event.is_set():

            # Ensure not to block for any event if sfp insert event is pending
            if self.sfp_insert_events:
                timeout = SFP_INSERT_EVENT_POLL_PERIOD_MSECS
            status, port_dict, error_dict = _wrapper_get_transceiver_change_event(timeout)
            if status:
                # Soak SFP insert events across various ports (updates port_dict)
                _wrapper_soak_sfp_insert_event(self.sfp_insert_events, port_dict)
            if not port_dict:
                continue
            y_cable_helper.change_ports_status_for_y_cable_change_event(
                port_dict, y_cable_presence, stopping_event)

    def task_run(self, sfp_error_event, y_cable_presence):
        if self.task_stopping_event.is_set():
            return

        self.task_process = multiprocessing.Process(target=self.task_worker, args=(
            self.task_stopping_event, sfp_error_event, y_cable_presence))
        self.task_process.start()

    def task_stop(self):
        self.task_stopping_event.set()
        os.kill(self.task_process.pid, signal.SIGKILL)

#
# Daemon =======================================================================
#


class DaemonYcable(daemon_base.DaemonBase):
    def __init__(self, log_identifier):
        super(DaemonYcable, self).__init__(log_identifier)

        self.timeout = XCVRD_MAIN_THREAD_SLEEP_SECS
        self.num_asics = multi_asic.get_num_asics()
        self.stop_event = threading.Event()
        self.sfp_error_event = multiprocessing.Event()
        self.y_cable_presence = [False]

    # Signal handler
    def signal_handler(self, sig, frame):
        if sig == signal.SIGHUP:
            self.log_info("Caught SIGHUP - ignoring...")
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
        sst = swsscommon.SubscriberStateTable(appl_db, swsscommon.APP_PORT_TABLE_NAME)
        sel.addSelectable(sst)

        # Make sure this daemon started after all port configured
        while not self.stop_event.is_set():
            (state, c) = sel.select(SELECT_TIMEOUT_MSECS)
            if state == swsscommon.Select.TIMEOUT:
                continue
            if state != swsscommon.Select.OBJECT:
                self.log_warning("sel.select() did not return swsscommon.Select.OBJECT")
                continue

            (key, op, fvp) = sst.pop()
            if key in ["PortConfigDone", "PortInitDone"]:
                break

    # Initialize daemon
    def init(self):
        global platform_sfputil
        global platform_chassis

        self.log_info("Start daemon init...")

        # Load new platform api class
        try:
            import sonic_platform.platform
            import sonic_platform_base.sonic_sfp.sfputilhelper
            platform_chassis = sonic_platform.platform.Platform().get_chassis()
            self.log_info("chassis loaded {}".format(platform_chassis))
            # we have to make use of sfputil for some features
            # even though when new platform api is used for all vendors.
            # in this sense, we treat it as a part of new platform api.
            # we have already moved sfputil to sonic_platform_base
            # which is the root of new platform api.
            platform_sfputil = sonic_platform_base.sonic_sfp.sfputilhelper.SfpUtilHelper()
        except Exception as e:
            self.log_warning("Failed to load chassis due to {}".format(repr(e)))

        # Load platform specific sfputil class
        if platform_chassis is None or platform_sfputil is None:
            try:
                platform_sfputil = self.load_platform_util(PLATFORM_SPECIFIC_MODULE_NAME, PLATFORM_SPECIFIC_CLASS_NAME)
            except Exception as e:
                self.log_error("Failed to load sfputil: {}".format(str(e)), True)
                sys.exit(SFPUTIL_LOAD_ERROR)

        if multi_asic.is_multi_asic():
            # Load the namespace details first from the database_global.json file.
            swsscommon.SonicDBConfig.initializeGlobalConfig()

        # Load port info
        try:
            if multi_asic.is_multi_asic():
                # For multi ASIC platforms we pass DIR of port_config_file_path and the number of asics
                (platform_path, hwsku_path) = device_info.get_paths_to_platform_and_hwsku_dirs()
                platform_sfputil.read_all_porttab_mappings(hwsku_path, self.num_asics)
            else:
                # For single ASIC platforms we pass port_config_file_path and the asic_inst as 0
                port_config_file_path = device_info.get_path_to_port_config_file()
                platform_sfputil.read_porttab_mappings(port_config_file_path, 0)
        except Exception as e:
            self.log_error("Failed to read port info: {}".format(str(e)), True)
            sys.exit(PORT_CONFIG_LOAD_ERROR)

        # Connect to STATE_DB and create transceiver dom/sfp info tables
        state_db, self.int_tbl, self.dom_tbl, self.status_tbl = {}, {}, {}, {}

        # Get the namespaces in the platform
        namespaces = multi_asic.get_front_end_namespaces()
        for namespace in namespaces:
            asic_id = multi_asic.get_asic_index_from_namespace(namespace)
            state_db[asic_id] = daemon_base.db_connect("STATE_DB", namespace)

        """
        #TODO need to decide if we need warm start capability in this ycabled daemon
        warmstart = swsscommon.WarmStart()
        warmstart.initialize("ycabled", "pmon")
        warmstart.checkWarmStart("ycabled", "pmon", False)
        is_warm_start = warmstart.isWarmStart()
        """

        # Make sure this daemon started after all port configured
        self.log_info("Wait for port config is done")
        #for namespace in namespaces:
            #self.wait_for_port_config_done(namespace)


        # Init port y_cable status table
        y_cable_helper.init_ports_status_for_y_cable(
            platform_sfputil, platform_chassis, self.y_cable_presence, self.stop_event)

    # Deinitialize daemon
    def deinit(self):
        self.log_info("Start daemon deinit...")

        # Delete all the information from DB and then exit
        logical_port_list = platform_sfputil.logical
        for logical_port_name in logical_port_list:
            # Get the asic to which this port belongs
            asic_index = platform_sfputil.get_asic_id_for_logical_port(logical_port_name)
            if asic_index is None:
                logger.log_warning("Got invalid asic index for {}, ignored".format(logical_port_name))
                continue

        if self.y_cable_presence[0] is True:
            y_cable_helper.delete_ports_status_for_y_cable()

        del globals()['platform_chassis']

    # Run daemon

    def run(self):
        self.log_info("Starting up...")

        # Start daemon initialization sequence
        self.init()

        # Start the dom sensor info update thread
        ycable_info_update = YcableInfoUpdateTask()
        ycable_info_update.task_run(self.y_cable_presence)

        # Start the sfp state info update process
        ycable_state_update = YcableStateUpdateTask()
        ycable_state_update.task_run(self.sfp_error_event, self.y_cable_presence)

        # Start the Y-cable state info update process if Y cable presence established
        y_cable_state_worker_update = None
        if self.y_cable_presence[0] is True:
            y_cable_state_worker_update = y_cable_helper.YCableTableUpdateTask()
            y_cable_state_worker_update.task_run()

        # Start main loop
        self.log_info("Start daemon main loop")

        while not self.stop_event.wait(self.timeout):
            self.log_info("Ycable main loop")

        self.log_info("Stop daemon main loop")

        # Stop the ycable periodic info info update thread
        ycable_info_update.task_stop()

        # Stop the ycable update process
        ycable_state_update.task_stop()

        # Stop the Y-cable state info update process
        if self.y_cable_presence[0] is True:
            y_cable_state_worker_update.task_stop()

        # Start daemon deinitialization sequence
        self.deinit()

        self.log_info("Shutting down...")

        if self.sfp_error_event.is_set():
            sys.exit(SFP_SYSTEM_ERROR)

#
# Main =========================================================================
#

# This is our main entry point for ycable script


def main():
    ycable = DaemonYcable(SYSLOG_IDENTIFIER)
    ycable.run()


if __name__ == '__main__':
    main()
