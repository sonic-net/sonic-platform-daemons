#!/usr/bin/env python3

import datetime

try:
    from ..dom.dom_mgr import DomInfoUpdateTask, PORT_UPDATE_EVENT_SELECT_TIMEOUT_FAST_MSECS
    from ..xcvrd_utilities import common
    from ..xcvrd_utilities import port_event_helper
    from .db_utils import CPODOMDBUtils
    from .xcvr_table_helper import CpoXcvrTableHelper
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")


class CpoDomInfoUpdateTask(DomInfoUpdateTask):
    name = "CpoDomInfoUpdateTask"

    def create_xcvr_table_helper(self, namespaces) -> CpoXcvrTableHelper:
        return CpoXcvrTableHelper(namespaces)

    def create_dom_db_utils(self, port_obj_dict, port_mapping, xcvr_table_helper,
                            task_stopping_event, logger) -> CPODOMDBUtils:
        return CPODOMDBUtils(port_obj_dict, port_mapping, xcvr_table_helper, task_stopping_event, logger)

    def _read_non_banked_oe_data(self, oe_api):
        """Read the module-scope (non-banked) OE diagnostic data, grouped by category."""
        return {
            'firmware_info': oe_api.get_transceiver_info_firmware_versions() or {},
            'dom': oe_api.get_non_banked_transceiver_dom_real_value() or {},
            'dom_flags': oe_api.get_non_banked_transceiver_dom_flags() or {},
            'status': oe_api.get_non_banked_transceiver_status() or {},
            'status_flags': oe_api.get_non_banked_transceiver_status_flags() or {},
        }

    def _read_banked_oe_data(self, oe_api):
        """Read the lane-scope (banked) OE diagnostic data, grouped by category."""
        return {
            'dom': oe_api.get_banked_transceiver_dom_real_value() or {},
            'dom_flags': oe_api.get_banked_transceiver_dom_flags() or {},
            'status': oe_api.get_banked_transceiver_status() or {},
            'status_flags': oe_api.get_banked_transceiver_status_flags() or {},
        }

    def _read_non_banked_elsfp_data(self, elsfp_api):
        """Read the module-scope (non-banked) ELSFP diagnostic data, grouped by category."""
        return {
            'firmware_info': elsfp_api.get_elsfp_info_firmware_versions() or {},
            'dom': elsfp_api.get_non_banked_elsfp_dom_real_value() or {},
            'dom_flags': elsfp_api.get_non_banked_elsfp_dom_flags() or {},
            'status': elsfp_api.get_non_banked_elsfp_status() or {},
            'status_flags': elsfp_api.get_non_banked_elsfp_status_flags() or {},
        }

    def _read_banked_elsfp_data(self, elsfp_api):
        """Read the lane-scope (banked) ELSFP diagnostic data, grouped by category."""
        return {
            'dom': elsfp_api.get_banked_elsfp_dom_real_value() or {},
            'dom_flags': elsfp_api.get_banked_elsfp_dom_flags() or {},
            'status': elsfp_api.get_banked_elsfp_status() or {},
            'status_flags': elsfp_api.get_banked_elsfp_status_flags() or {},
        }

    def collect_and_publish_data(self, port_change_observer):
        # Module-scope (non-banked) data read so far in this polling pass, keyed
        # by the first physical port of the corresponding device sharing group
        oe_module_data = {}
        elsfp_module_data = {}

        for physical_port in sorted(self.port_obj_dict):
            self.check_port_update(port_change_observer, PORT_UPDATE_EVENT_SELECT_TIMEOUT_FAST_MSECS)

            if self.task_stopping_event.is_set():
                self.log_notice("Stop event generated during CPO DOM monitoring loop")
                break

            port_info = self._validate_and_resolve_port(physical_port)
            if port_info is None:
                continue
            cpo_obj, logical_port_name, asic_index = port_info

            try:
                oe_api = cpo_obj.oe.get_api()
                elsfp_api = cpo_obj.elsfp.get_api()

                # Module-scope (non-banked) OE data: read ONCE per OE. Non-banked
                # registers are bank-independent, so the first sibling pport
                # processed in this pass reads them through its own CmisApi and
                # the remaining siblings re-use the snapshot.
                oe_key = min(common.get_oe_sibling_pports(physical_port))
                if oe_key not in oe_module_data:
                    oe_module_data[oe_key] = self._read_non_banked_oe_data(oe_api)

                # ELSFP module-scope data: read ONCE per ELSFP for the same reason
                # as above.
                elsfp_key = min(common.get_elsfp_sibling_pports(physical_port))
                if elsfp_key not in elsfp_module_data:
                    elsfp_module_data[elsfp_key] = self._read_non_banked_elsfp_data(elsfp_api)

                # Lane-scope (banked) data: unique to this physical port, since
                # the CPO object is bound to the bank covering this port's lanes
                oe_lane_data = self._read_banked_oe_data(oe_api)
                elsfp_lane_data = self._read_banked_elsfp_data(elsfp_api)
            except (KeyError, TypeError) as e:
                self.log_warning("Got exception {} while processing CPO dom info for port {}, ignored".format(repr(e), logical_port_name))
                continue

            oe_module = oe_module_data[oe_key]
            elsfp_module = elsfp_module_data[elsfp_key]

            # Publish to the first subport's logical interface, matching the
            # existing DomInfoUpdateTask convention for breakout groups. Each
            # category merges the device sharing group's module-scope snapshot
            # with this port's lane-scope values.
            # Firmware info's dict is copied since publishing beautifies it in place,
            # so subsequent publishes using this data would be affected. Other call-sites
            # are unaffected by this since they implicitly create a copy through merging
            # the banked and non-banked data into a single dict.
            self.dom_db_utils.post_diagnostic_values_from_dict_to_db(
                logical_port_name, self.xcvr_table_helper.get_firmware_info_tbl(asic_index),
                dict(oe_module['firmware_info']))
            self.dom_db_utils.post_diagnostic_values_from_dict_to_db(
                logical_port_name, self.xcvr_table_helper.get_dom_tbl(asic_index),
                {**oe_module['dom'], **oe_lane_data['dom']})
            self.dom_db_utils.post_flag_values_from_dict_to_db(
                logical_port_name,
                {**oe_module['dom_flags'], **oe_lane_data['dom_flags']},
                self.xcvr_table_helper.get_dom_flag_tables(asic_index),
                "DOM flags")
            self.dom_db_utils.post_diagnostic_values_from_dict_to_db(
                logical_port_name, self.xcvr_table_helper.get_status_tbl(asic_index),
                {**oe_module['status'], **oe_lane_data['status']})
            self.dom_db_utils.post_flag_values_from_dict_to_db(
                logical_port_name,
                {**oe_module['status_flags'], **oe_lane_data['status_flags']},
                self.xcvr_table_helper.get_status_flag_tables(asic_index),
                "Status flags")

            self.dom_db_utils.post_diagnostic_values_from_dict_to_db(
                logical_port_name, self.xcvr_table_helper.get_els_firmware_info_tbl(asic_index),
                dict(elsfp_module['firmware_info']))
            self.dom_db_utils.post_diagnostic_values_from_dict_to_db(
                logical_port_name, self.xcvr_table_helper.get_els_dom_tbl(asic_index),
                {**elsfp_module['dom'], **elsfp_lane_data['dom']})
            self.dom_db_utils.post_flag_values_from_dict_to_db(
                logical_port_name,
                {**elsfp_module['dom_flags'], **elsfp_lane_data['dom_flags']},
                self.xcvr_table_helper.get_els_dom_flag_tables(asic_index),
                "ELS DOM flags")
            self.dom_db_utils.post_diagnostic_values_from_dict_to_db(
                logical_port_name, self.xcvr_table_helper.get_els_status_tbl(asic_index),
                {**elsfp_module['status'], **elsfp_lane_data['status']})
            self.dom_db_utils.post_flag_values_from_dict_to_db(
                logical_port_name,
                {**elsfp_module['status_flags'], **elsfp_lane_data['status_flags']},
                self.xcvr_table_helper.get_els_status_flag_tables(asic_index),
                "ELS Status flags")

            # TODO: Add support for collecting and publishing VDM data.

    def on_port_update_event(self, port_change_event):
        """Called when a port change event is received

        A fault on a shared CPO device can flap every port it drives, and its flag
        registers are clear-on-read, so reacting on a per-port basis would leave 
        all but the first port reading already-cleared registers. Instead, queue
        one read-and-publish attempt per CPO device driving the flapped port, keyed by
        (device type, device id): the device's flags are then read once and
        published for every port it drives.
        """
        if port_change_event.event_type == port_event_helper.PortChangeEvent.PORT_SET and \
                port_change_event.db_name == 'APPL_DB':
            for device_type in (common.CPO_DEVICE_TYPE_OE, common.CPO_DEVICE_TYPE_ELSFP):
                for device_id in common.get_cpo_devices_of_pport(port_change_event.port_index, device_type):
                    self.link_change_affected_ports[(device_type, device_id)] = (
                                    datetime.datetime.now() +
                                    datetime.timedelta(seconds=self.DIAG_DB_UPDATE_TIME_AFTER_LINK_CHANGE))

    def update_port_db_diagnostics_on_link_change(self, device_key):
        """Read and publish the latched flag families of one CPO device.

        Unlike the base class, link_change_affected_ports entries are keyed by
        (device type, device id) rather than physical port (see
        on_port_update_event), so each handler reads only its own device's
        registers and publishes only its own flag tables.
        """
        if self.task_stopping_event.is_set():
            return

        device_type, device_id = device_key
        if device_type == common.CPO_DEVICE_TYPE_OE:
            self._publish_oe_flags_for_device(device_id)
        elif device_type == common.CPO_DEVICE_TYPE_ELSFP:
            self._publish_elsfp_flags_for_device(device_id)

    def _publish_oe_flags_for_device(self, device_id):
        """Read the OE's latched flags and publish them for every port it drives.

        Module-scope flags are clear-on-read for the whole device, so they are
        read once, through the first pollable member port's API, and the snapshot
        is published for every member. Banked flags are read per member port.
        """
        oe_module_flags = None
        for physical_port in sorted(common.get_cpo_device_pports(common.CPO_DEVICE_TYPE_OE, device_id)):
            port_info = self._validate_and_resolve_port(physical_port)
            if port_info is None:
                continue
            cpo_obj, logical_port_name, asic_index = port_info

            try:
                oe_api = cpo_obj.oe.get_api()
                if oe_module_flags is None:
                    oe_module_flags = {
                        'dom_flags': oe_api.get_non_banked_transceiver_dom_flags() or {},
                        'status_flags': oe_api.get_non_banked_transceiver_status_flags() or {},
                    }
                banked_dom_flags = oe_api.get_banked_transceiver_dom_flags() or {}
                banked_status_flags = oe_api.get_banked_transceiver_status_flags() or {}
            except (KeyError, TypeError) as e:
                self.log_warning("Got exception {} while processing OE flags of {} for port {}, ignored".format(repr(e), device_id, logical_port_name))
                continue

            self.dom_db_utils.post_flag_values_from_dict_to_db(
                logical_port_name,
                {**oe_module_flags['dom_flags'], **banked_dom_flags},
                self.xcvr_table_helper.get_dom_flag_tables(asic_index),
                "DOM flags")
            self.dom_db_utils.post_flag_values_from_dict_to_db(
                logical_port_name,
                {**oe_module_flags['status_flags'], **banked_status_flags},
                self.xcvr_table_helper.get_status_flag_tables(asic_index),
                "Status flags")

    def _publish_elsfp_flags_for_device(self, device_id):
        """Read the ELSFP's latched flags and publish them for every port it drives.

        Module-scope flags are clear-on-read for the whole device, so they are
        read once, through the first pollable member port's API, and the snapshot
        is published for every member. Banked flags are read per member port.
        """
        elsfp_module_flags = None
        for physical_port in sorted(common.get_cpo_device_pports(common.CPO_DEVICE_TYPE_ELSFP, device_id)):
            port_info = self._validate_and_resolve_port(physical_port)
            if port_info is None:
                continue
            cpo_obj, logical_port_name, asic_index = port_info

            try:
                elsfp_api = cpo_obj.elsfp.get_api()
                if elsfp_module_flags is None:
                    elsfp_module_flags = {
                        'dom_flags': elsfp_api.get_non_banked_elsfp_dom_flags() or {},
                        'status_flags': elsfp_api.get_non_banked_elsfp_status_flags() or {},
                    }
                banked_dom_flags = elsfp_api.get_banked_elsfp_dom_flags() or {}
                banked_status_flags = elsfp_api.get_banked_elsfp_status_flags() or {}
            except (KeyError, TypeError) as e:
                self.log_warning("Got exception {} while processing ELSFP flags of {} for port {}, ignored".format(repr(e), device_id, logical_port_name))
                continue

            self.dom_db_utils.post_flag_values_from_dict_to_db(
                logical_port_name,
                {**elsfp_module_flags['dom_flags'], **banked_dom_flags},
                self.xcvr_table_helper.get_els_dom_flag_tables(asic_index),
                "ELS DOM flags")
            self.dom_db_utils.post_flag_values_from_dict_to_db(
                logical_port_name,
                {**elsfp_module_flags['status_flags'], **banked_status_flags},
                self.xcvr_table_helper.get_els_status_flag_tables(asic_index),
                "ELS Status flags")
