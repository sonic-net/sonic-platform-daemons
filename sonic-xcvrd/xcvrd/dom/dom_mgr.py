"""
DOM Info Update task manager
Updates various transceiver diagnostic information in the DB periodically, running
as a child thread of xcvrd main thread.
"""

from contextlib import contextmanager


try:
    import threading
    import copy
    import sys
    import re

    from natsort import natsorted
    from swsscommon import swsscommon

    from xcvrd import xcvrd
    from xcvrd.xcvrd_utilities import sfp_status_helper
    from xcvrd.xcvrd_utilities.xcvr_table_helper import *
    from xcvrd.xcvrd_utilities import port_event_helper
    from xcvrd.dom.utilities.db.utils import DBUtils
    from xcvrd.dom.utilities.vdm.utils import VDMUtils
    from xcvrd.dom.utilities.vdm.db_utils import VDMDBUtils
except ImportError as e:
    raise ImportError(str(e) + " - required module not found in dom_mgr.py")

class DomInfoUpdateTask(threading.Thread):
    DOM_LOGGER_PREFIX = "DOM-INFO-UPDATE: "
    DOM_INFO_UPDATE_PERIOD_SECS = 60

    def __init__(self, namespaces, port_mapping, sfp_obj_dict, main_thread_stop_event, skip_cmis_mgr, helper_logger):
        threading.Thread.__init__(self)
        self.name = "DomInfoUpdateTask"
        self.exc = None
        self.task_stopping_event = threading.Event()
        self.main_thread_stop_event = main_thread_stop_event
        self.helper_logger = helper_logger
        self.port_mapping = copy.deepcopy(port_mapping)
        self.namespaces = namespaces
        self.skip_cmis_mgr = skip_cmis_mgr
        self.sfp_obj_dict = sfp_obj_dict
        self.xcvr_table_helper = XcvrTableHelper(self.namespaces)
        self.db_utils = DBUtils(self.helper_logger)
        self.vdm_utils = VDMUtils(self.sfp_obj_dict, self.helper_logger)
        self.vdm_db_utils = VDMDBUtils(self.sfp_obj_dict, self.port_mapping, self.xcvr_table_helper, self.task_stopping_event, self.helper_logger)

    def log_debug(self, message):
        self.helper_logger.log_debug("{}{}".format(self.DOM_LOGGER_PREFIX, message))

    def log_info(self, message):
        self.helper_logger.log_info("{}{}".format(self.DOM_LOGGER_PREFIX, message))

    def log_notice(self, message):
        self.helper_logger.log_notice("{}{}".format(self.DOM_LOGGER_PREFIX, message))

    def log_warning(self, message):
        self.helper_logger.log_warning("{}{}".format(self.DOM_LOGGER_PREFIX, message))

    def log_error(self, message):
        self.helper_logger.log_error("{}{}".format(self.DOM_LOGGER_PREFIX, message))

    def get_dom_polling_from_config_db(self, lport):
        """
            Returns the value of dom_polling field from PORT table in CONFIG_DB
            For non-breakout ports, this function will get dom_polling field from PORT table of lport (subport = 0)
            For breakout ports, this function will get dom_polling field from PORT table of the first subport
            of lport's corresponding breakout group (subport = 1)

            Returns:
                'disabled' if dom_polling is set to 'disabled', otherwise 'enabled'
        """
        dom_polling = 'enabled'

        pport_list = self.port_mapping.get_logical_to_physical(lport)
        if not pport_list:
            self.log_warning("Get dom disabled: Got unknown physical port list {} for lport {}".format(pport_list, lport))
            return dom_polling
        pport = pport_list[0]

        logical_port_list = self.port_mapping.get_physical_to_logical(pport)
        if logical_port_list is None:
            self.log_warning("Get dom disabled: Got unknown FP port index {}".format(pport))
            return dom_polling

        # Sort the logical port list to make sure we always get the first subport
        logical_port_list = natsorted(logical_port_list, key=lambda y: y.lower())
        first_logical_port = logical_port_list[0]

        asic_index = self.port_mapping.get_asic_id_for_logical_port(first_logical_port)
        port_tbl = self.xcvr_table_helper.get_cfg_port_tbl(asic_index)

        found, port_info = port_tbl.get(first_logical_port)
        if found and 'dom_polling' in dict(port_info):
            dom_polling = dict(port_info)['dom_polling']

        return dom_polling

    """
    Checks if the port is going through CMIS initialization process
    This API assumes CMIS_STATE_UNKNOWN as a transitional state since it is the
    first state after starting CMIS state machine.
    This assumption allows the DomInfoUpdateTask thread to skip polling on the port
    to allow CMIS initialization to complete if needed.
    Returns:
        True if the port is in CMIS initialization process,
        otherwise False
    """
    def is_port_in_cmis_initialization_process(self, logical_port_name):
        # If CMIS manager is not available for the platform, return False
        if self.skip_cmis_mgr:
            return False

        asic_index = self.port_mapping.get_asic_id_for_logical_port(logical_port_name)
        if asic_index is None:
            self.log_warning("Got invalid asic index for {} while checking cmis init status".format(logical_port_name))
            return False

        cmis_state = xcvrd.get_cmis_state_from_state_db(logical_port_name, self.xcvr_table_helper.get_status_tbl(asic_index))
        if cmis_state not in xcvrd.CMIS_TERMINAL_STATES:
            return True
        else:
            return False

    def is_port_dom_monitoring_disabled(self, logical_port_name):
        return self.get_dom_polling_from_config_db(logical_port_name) == 'disabled' or \
                self.is_port_in_cmis_initialization_process(logical_port_name)

    # Remove unnecessary unit from the raw data
    def beautify_dom_info_dict(self, dom_info_dict, physical_port):
        for k, v in dom_info_dict.items():
            if k == 'temperature':
                dom_info_dict[k] = xcvrd.strip_unit_and_beautify(v, xcvrd.TEMP_UNIT)
            elif k == 'voltage':
                dom_info_dict[k] = xcvrd.strip_unit_and_beautify(v, xcvrd.VOLT_UNIT)
            elif re.match('^(tx|rx)[1-8]power$', k):
                dom_info_dict[k] = xcvrd.strip_unit_and_beautify(v, xcvrd.POWER_UNIT)
            elif re.match('^(tx|rx)[1-8]bias$', k):
                dom_info_dict[k] = xcvrd.strip_unit_and_beautify(v, xcvrd.BIAS_UNIT)
            elif type(v) is not str:
                # For all the other keys:
                dom_info_dict[k] = str(v)

    # Update port sfp firmware info in db
    def post_port_sfp_firmware_info_to_db(self, logical_port_name, port_mapping, table,
                                stop_event=threading.Event(), firmware_info_cache=None):
        for physical_port, physical_port_name in xcvrd.get_physical_port_name_dict(logical_port_name, port_mapping).items():
            if stop_event.is_set():
                break

            if not xcvrd._wrapper_get_presence(physical_port):
                continue

            try:
                if firmware_info_cache is not None and physical_port in firmware_info_cache:
                    # If cache is enabled and firmware information is in cache, just read from cache, no need read from EEPROM
                    transceiver_firmware_info_dict = firmware_info_cache[physical_port]
                else:
                    transceiver_firmware_info_dict = xcvrd._wrapper_get_transceiver_firmware_info(physical_port)
                    if firmware_info_cache is not None:
                        # If cache is enabled, put firmware information to cache
                        firmware_info_cache[physical_port] = transceiver_firmware_info_dict
                if transceiver_firmware_info_dict:
                    fvs = swsscommon.FieldValuePairs([(k, v) for k, v in transceiver_firmware_info_dict.items()])
                    table.set(physical_port_name, fvs)
                else:
                    return xcvrd.SFP_EEPROM_NOT_READY

            except NotImplementedError:
                helper_logger.log_error("Transceiver firmware info functionality is currently not implemented for this platform")
                sys.exit(xcvrd.NOT_IMPLEMENTED_ERROR)

    # Update port dom sensor info in db
    def post_port_dom_info_to_db(self, logical_port_name, port_mapping, table, stop_event=threading.Event(), dom_info_cache=None):
        for physical_port, physical_port_name in xcvrd.get_physical_port_name_dict(logical_port_name, port_mapping).items():
            if stop_event.is_set():
                break

            if not xcvrd._wrapper_get_presence(physical_port):
                continue

            if xcvrd._wrapper_is_flat_memory(physical_port) == True:
                continue

            try:
                if dom_info_cache is not None and physical_port in dom_info_cache:
                    # If cache is enabled and dom information is in cache, just read from cache, no need read from EEPROM
                    dom_info_dict = dom_info_cache[physical_port]
                else:
                    dom_info_dict = xcvrd._wrapper_get_transceiver_dom_info(physical_port)
                    if dom_info_cache is not None:
                        # If cache is enabled, put dom information to cache
                        dom_info_cache[physical_port] = dom_info_dict
                if dom_info_dict is not None:
                    self.beautify_dom_info_dict(dom_info_dict, physical_port)
                    fvs = swsscommon.FieldValuePairs([(k, v) for k, v in dom_info_dict.items()])
                    table.set(physical_port_name, fvs)
                else:
                    return xcvrd.SFP_EEPROM_NOT_READY

            except NotImplementedError:
                helper_logger.log_error("This functionality is currently not implemented for this platform")
                sys.exit(xcvrd.NOT_IMPLEMENTED_ERROR)

    # Update port SFP status table for HW fields
    def update_port_transceiver_status_table_hw(self, logical_port_name, port_mapping,
                                                table, stop_event=threading.Event(), transceiver_status_cache=None):
        for physical_port, physical_port_name in xcvrd.get_physical_port_name_dict(logical_port_name, port_mapping).items():
            if stop_event.is_set():
                break

            if not xcvrd._wrapper_get_presence(physical_port):
                continue

            if transceiver_status_cache is not None and physical_port in transceiver_status_cache:
                # If cache is enabled and status info is in cache, just read from cache, no need read from EEPROM
                transceiver_status_dict = transceiver_status_cache[physical_port]
            else:
                transceiver_status_dict = xcvrd._wrapper_get_transceiver_status(physical_port)
                if transceiver_status_cache is not None:
                    # If cache is enabled, put status info to cache
                    transceiver_status_cache[physical_port] = transceiver_status_dict
            if transceiver_status_dict is not None:
                # Skip if empty (i.e. get_transceiver_status API is not applicable for this xcvr)
                if not transceiver_status_dict:
                    continue
                self.db_utils.beautify_info_dict(transceiver_status_dict)
                fvs = swsscommon.FieldValuePairs([(k, v) for k, v in transceiver_status_dict.items()])
                table.set(physical_port_name, fvs)
            else:
                return xcvrd.SFP_EEPROM_NOT_READY

    # Update port pm info in db
    def post_port_pm_info_to_db(self, logical_port_name, port_mapping, table, stop_event=threading.Event(), pm_info_cache=None):
        for physical_port, physical_port_name in xcvrd.get_physical_port_name_dict(logical_port_name, port_mapping).items():
            if stop_event.is_set():
                break

            if not xcvrd._wrapper_get_presence(physical_port):
                continue

            if xcvrd._wrapper_is_flat_memory(physical_port) == True:
                continue

            if pm_info_cache is not None and physical_port in pm_info_cache:
                # If cache is enabled and pm info is in cache, just read from cache, no need read from EEPROM
                pm_info_dict = pm_info_cache[physical_port]
            else:
                pm_info_dict = xcvrd._wrapper_get_transceiver_pm(physical_port)
                if pm_info_cache is not None:
                    # If cache is enabled, put dom information to cache
                    pm_info_cache[physical_port] = pm_info_dict
            if pm_info_dict is not None:
                # Skip if empty (i.e. get_transceiver_pm API is not applicable for this xcvr)
                if not pm_info_dict:
                    continue
                self.db_utils.beautify_info_dict(pm_info_dict)
                fvs = swsscommon.FieldValuePairs([(k, v) for k, v in pm_info_dict.items()])
                table.set(physical_port_name, fvs)
            else:
                return xcvrd.SFP_EEPROM_NOT_READY

    def task_worker(self):
        self.log_notice("Start DOM monitoring loop")
        firmware_info_cache = {}
        dom_info_cache = {}
        transceiver_status_cache = {}
        vdm_real_value_cache = {}
        vdm_flag_cache = {}
        pm_info_cache = {}
        sel, asic_context = port_event_helper.subscribe_port_config_change(self.namespaces)

        # Start loop to update dom info in DB periodically
        while not self.task_stopping_event.wait(self.DOM_INFO_UPDATE_PERIOD_SECS):
            # Clear the cache at the begin of the loop to make sure it will be clear each time
            firmware_info_cache.clear()
            dom_info_cache.clear()
            transceiver_status_cache.clear()
            vdm_real_value_cache.clear()
            vdm_flag_cache.clear()
            pm_info_cache.clear()

            # Handle port change event from main thread
            port_event_helper.handle_port_config_change(sel, asic_context, self.task_stopping_event, self.port_mapping, self.helper_logger, self.on_port_config_change)
            logical_port_list = self.port_mapping.logical_port_list
            for logical_port_name in logical_port_list:
                if self.is_port_dom_monitoring_disabled(logical_port_name):
                    continue

                if self.task_stopping_event.is_set():
                    self.log_notice("DomInfoUpdateTask stop event generated during DOM monitoring loop")
                    break

                # Get the asic to which this port belongs
                asic_index = self.port_mapping.get_asic_id_for_logical_port(logical_port_name)
                if asic_index is None:
                    self.log_warning("Got invalid asic index for {}, ignored".format(logical_port_name))
                    continue

                physical_port_list = self.port_mapping.get_logical_to_physical(logical_port_name)
                if not physical_port_list:
                    self.log_warning("Got unknown physical port list {} for lport {}".format(physical_port_list, logical_port_name))
                    continue
                physical_port = physical_port_list[0]

                if not sfp_status_helper.detect_port_in_error_status(logical_port_name, self.xcvr_table_helper.get_status_tbl(asic_index)):
                    if not xcvrd._wrapper_get_presence(physical_port):
                        continue

                    try:
                        self.post_port_sfp_firmware_info_to_db(logical_port_name, self.port_mapping, self.xcvr_table_helper.get_firmware_info_tbl(asic_index), self.task_stopping_event, firmware_info_cache=firmware_info_cache)
                    except (KeyError, TypeError) as e:
                        #continue to process next port since execption could be raised due to port reset, transceiver removal
                        self.log_warning("Got exception {} while processing firmware info for port {}, ignored".format(repr(e), logical_port_name))
                        continue
                    try:
                        self.post_port_dom_info_to_db(logical_port_name, self.port_mapping, self.xcvr_table_helper.get_dom_tbl(asic_index), self.task_stopping_event, dom_info_cache=dom_info_cache)
                    except (KeyError, TypeError) as e:
                        #continue to process next port since execption could be raised due to port reset, transceiver removal
                        self.log_warning("Got exception {} while processing dom info for port {}, ignored".format(repr(e), logical_port_name))
                        continue
                    try:
                        self.update_port_transceiver_status_table_hw(logical_port_name,
                                                                self.port_mapping,
                                                                self.xcvr_table_helper.get_status_tbl(asic_index),
                                                                self.task_stopping_event,
                                                                transceiver_status_cache=transceiver_status_cache)
                    except (KeyError, TypeError) as e:
                        #continue to process next port since execption could be raised due to port reset, transceiver removal
                        self.log_warning("Got exception {} while processing transceiver status hw for port {}, ignored".format(repr(e), logical_port_name))
                        continue
                    if self.vdm_utils.is_transceiver_vdm_supported(physical_port):
                        # Freeze VDM stats before reading VDM values
                        with self.vdm_utils.vdm_freeze_context(physical_port) as vdm_frozen:
                            if not vdm_frozen:
                                self.log_error("Failed to freeze VDM stats for port {}".format(physical_port))
                                continue
                            try:
                                # Read and post VDM real values to DB
                                self.vdm_db_utils.post_port_vdm_real_values_to_db(logical_port_name, self.xcvr_table_helper.get_vdm_real_value_tbl(asic_index),
                                                                self.vdm_utils.get_vdm_real_values, db_cache=vdm_real_value_cache)
                            except (KeyError, TypeError) as e:
                                #continue to process next port since execption could be raised due to port reset, transceiver removal
                                self.log_warning("Got exception {} while processing vdm values for port {}, ignored".format(repr(e), logical_port_name))
                                continue
                            try:
                                # Read and post VDM flags and metadata to DB
                                self.vdm_db_utils.post_port_vdm_flags_to_db(logical_port_name, db_cache=vdm_flag_cache)
                            except (KeyError, TypeError) as e:
                                #continue to process next port since execption could be raised due to port reset, transceiver removal
                                self.log_warning("Got exception {} while processing vdm flags for port {}, ignored".format(repr(e), logical_port_name))
                                continue
                            try:
                                self.post_port_pm_info_to_db(logical_port_name, self.port_mapping, self.xcvr_table_helper.get_pm_tbl(asic_index), self.task_stopping_event, pm_info_cache=pm_info_cache)
                            except (KeyError, TypeError) as e:
                                #continue to process next port since execption could be raised due to port reset, transceiver removal
                                self.log_warning("Got exception {} while processing pm info for port {}, ignored".format(repr(e), logical_port_name))
                                continue

        self.log_notice("Stop DOM monitoring loop")

    def run(self):
        if self.task_stopping_event.is_set():
            return
        try:
            self.task_worker()
        except Exception as e:
            self.log_error("Exception occured at {} thread due to {}".format(threading.current_thread().getName(), repr(e)))
            xcvrd.log_exception_traceback()
            self.exc = e
            self.main_thread_stop_event.set()

    def join(self):
        self.task_stopping_event.set()
        threading.Thread.join(self)
        if self.exc:
            raise self.exc

    def on_port_config_change(self, port_change_event):
        if port_change_event.event_type == port_event_helper.PortChangeEvent.PORT_REMOVE:
            self.on_remove_logical_port(port_change_event)
        self.port_mapping.handle_port_change_event(port_change_event)

    def on_remove_logical_port(self, port_change_event):
        """Called when a logical port is removed from CONFIG_DB

        Args:
            port_change_event (object): port change event
        """
        # To avoid race condition, remove the entry TRANSCEIVER_FIRMWARE_INFO, TRANSCEIVER_DOM_SENSOR, TRANSCEIVER_PM and HW section of TRANSCEIVER_STATUS table.
        # This thread only updates TRANSCEIVER_FIRMWARE_INFO, TRANSCEIVER_DOM_SENSOR, TRANSCEIVER_PM and HW section of TRANSCEIVER_STATUS table,
        # so we don't have to remove entries from TRANSCEIVER_INFO, TRANSCEIVER_DOM_THRESHOLD and VDM threshold value tables.
        xcvrd.del_port_sfp_dom_info_from_db(port_change_event.port_name,
                                      self.port_mapping,
                                      [self.xcvr_table_helper.get_dom_tbl(port_change_event.asic_id),
                                      self.xcvr_table_helper.get_vdm_real_value_tbl(port_change_event.asic_id),
                                      *[self.xcvr_table_helper.get_vdm_flag_tbl(port_change_event.asic_id, key) for key in VDM_THRESHOLD_TYPES],
                                      *[self.xcvr_table_helper.get_vdm_flag_change_count_tbl(port_change_event.asic_id, key) for key in VDM_THRESHOLD_TYPES],
                                      *[self.xcvr_table_helper.get_vdm_flag_set_time_tbl(port_change_event.asic_id, key) for key in VDM_THRESHOLD_TYPES],
                                      *[self.xcvr_table_helper.get_vdm_flag_clear_time_tbl(port_change_event.asic_id, key) for key in VDM_THRESHOLD_TYPES],
                                      self.xcvr_table_helper.get_pm_tbl(port_change_event.asic_id),
                                      self.xcvr_table_helper.get_firmware_info_tbl(port_change_event.asic_id)
                                      ])
        xcvrd.delete_port_from_status_table_hw(port_change_event.port_name,
                                      self.port_mapping,
                                      self.xcvr_table_helper.get_status_tbl(port_change_event.asic_id))
