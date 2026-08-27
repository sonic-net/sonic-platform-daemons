#!/usr/bin/env python3

import threading

try:
    from swsscommon import swsscommon
    from ..xcvrd import (SfpStateUpdateTask, helper_logger,
                         PHYSICAL_PORT_NOT_EXIST, SFP_EEPROM_NOT_READY)
    from ..xcvrd_utilities import common
    from .db_utils import CPODOMDBUtils, CPOVDMDBUtils
    from .xcvr_table_helper import CpoXcvrTableHelper
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")

platform_chassis = None


class CpoStateUpdateTask(SfpStateUpdateTask):
    def __init__(self, namespaces, port_mapping, port_obj_dict, main_thread_stop_event, sfp_error_event):
        super().__init__(namespaces, port_mapping, port_obj_dict, main_thread_stop_event, sfp_error_event)
        self.name = "CpoStateUpdateTask"

    def create_xcvr_table_helper(self, namespaces) -> CpoXcvrTableHelper:
        return CpoXcvrTableHelper(namespaces)

    def create_dom_db_utils(self, port_obj_dict, port_mapping, xcvr_table_helper,
                            task_stopping_event, logger) -> CPODOMDBUtils:
        return CPODOMDBUtils(port_obj_dict, port_mapping, xcvr_table_helper, task_stopping_event, logger)

    def create_vdm_db_utils(self, port_obj_dict, port_mapping, xcvr_table_helper,
                            task_stopping_event, logger) -> CPOVDMDBUtils:
        return CPOVDMDBUtils(port_obj_dict, port_mapping, xcvr_table_helper, task_stopping_event, logger)

    def _get_port_change_event(self, timeout):
        status, events = platform_chassis.get_elsfp_change_event(timeout)
        elsfp_events = events.get('elsfp')
        elsfp_errors = events.get('elsfp_error')
        return status, elsfp_events, elsfp_errors

    def _get_port_error_description(self, physical_port):
        port_device = common.get_port_device(physical_port)
        oe_api = port_device.oe.get_api()
        return oe_api.get_error_description()

    def post_port_info_to_db(self, logical_port_name, port_mapping, table, transceiver_dict,
                             stop_event=threading.Event()):
        physical_port_list = port_mapping.logical_port_name_to_physical_port_list(logical_port_name)
        if physical_port_list is None:
            helper_logger.log_error("No physical ports found for logical port '{}'".format(logical_port_name))
            return PHYSICAL_PORT_NOT_EXIST

        assert len(physical_port_list) == 1, "Ganged ports are not yet supported on CPO"

        for physical_port in physical_port_list:
            if stop_event.is_set():
                break

            if not common._wrapper_get_presence(physical_port):
                helper_logger.log_notice("Transceiver not present in port {}".format(logical_port_name))
                continue

            port_name = common.get_physical_port_name(logical_port_name, physical_port, False)

            port_device = common.get_port_device(physical_port)
            if physical_port in transceiver_dict:
                oe_info = transceiver_dict[physical_port]
            else:
                oe_info = port_device.oe.get_api().get_transceiver_info()
                transceiver_dict[physical_port] = oe_info
            elsfp_info = port_device.elsfp.get_api().get_elsfp_info()

            if oe_info is None or elsfp_info is None:
                return SFP_EEPROM_NOT_READY

            # Publish OE info
            fvs = swsscommon.FieldValuePairs(
                [(field, str(value)) for field, value in oe_info.items()] +
                [('is_replaceable', str(False))]
            )
            table.set(port_name, fvs)

            # Publish ELSFP info
            is_replaceable = self._wrapper_is_replaceable(physical_port)
            asic_index = port_mapping.get_asic_id_for_logical_port(logical_port_name)
            els_tbl = self.xcvr_table_helper.get_els_info_tbl(asic_index)
            fvs = swsscommon.FieldValuePairs(
                [(field, str(value)) for field, value in elsfp_info.items()] +
                [('is_replaceable', str(is_replaceable))]
            )
            els_tbl.set(port_name, fvs)
