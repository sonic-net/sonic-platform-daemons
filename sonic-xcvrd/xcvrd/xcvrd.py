#!/usr/bin/env python3

"""
    xcvrd
    Transceiver information update daemon for SONiC
"""

try:
    import ast
    import collections
    import copy
    import json
    import os
    import queue
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
    from .cpo.cpo_manager_task import CpoManagerTask
    from .cpo.dom_mgr import CpoDomInfoUpdateTask
    from .cpo.xcvr_table_helper import CpoXcvrTableHelper
    from .xcvrd_utilities.xcvr_table_helper import *
    from .xcvrd_utilities import port_event_helper
    from .xcvrd_utilities.port_event_helper import PortChangeObserver
    from .xcvrd_utilities import media_settings_parser
    from .xcvrd_utilities import optics_si_parser
    from .xcvrd_utilities import common
    from .xcvrd_utilities.asic_readiness import AsicReadiness
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

# Per-namespace ASIC readiness: portsyncd publishes these marker keys into APPL_DB PORT_TABLE
# of each namespace. Readiness = a SET of either key was observed (replayed at subscription or live).
PORT_READY_KEYS = ("PortConfigDone", "PortInitDone")
ASIC_CHECKER_FAILED = object()          # ready_asic_queue sentinel: unrecoverable checker failure
ASIC_STILL_WAITING_LOG_SECS = 300       # heartbeat while any namespace is pending
ASIC_CHECKER_MAX_SELECT_ERRORS = 10     # consecutive select() results that are neither OBJECT nor TIMEOUT
ASIC_CHECKER_JOIN_TIMEOUT_SECS = 5      # shutdown join budget for the checker thread (supervisord allows 10 s)

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


def waiting_time_compensation_with_sleep(time_start, time_to_wait):
    time_now = time.time()
    time_diff = time_now - time_start
    if time_diff < time_to_wait:
        time.sleep(time_to_wait - time_diff)

# Delete port from SFP status table

# Thread wrapper class to update sfp state info periodically


class SfpStateUpdateTask(threading.Thread):
    RETRY_EEPROM_READING_INTERVAL = 60
    SYSLOG_IDENTIFIER = SYSLOG_IDENTIFIER_SFPSTATEUPDATETASK
    PUBLISHER_NAME = "SfpStateUpdateTask"

    def __init__(self, namespaces, port_mapping, port_obj_dict, main_thread_stop_event, sfp_error_event,
                 asic_readiness=None):
        threading.Thread.__init__(self)
        self.name = self.PUBLISHER_NAME
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
        self.port_obj_dict = port_obj_dict
        self.logger = syslogger.SysLogger(self.SYSLOG_IDENTIFIER, enable_runtime_config=True)
        self.xcvr_table_helper = self.create_xcvr_table_helper(self.namespaces)
        # Per-namespace readiness: the start-up pass of a namespace runs once it is ready, in init() for a
        # namespace ready then and at its admission in the worker loop otherwise; the NPU media settings of a
        # pending namespace are published by that pass. None means "every namespace ready" (today's behaviour).
        self.asic_readiness = asic_readiness if asic_readiness is not None \
                              else AsicReadiness.all_ready_registry(namespaces)
        self.warm_fast_reboot_status = self.initialize_warm_fast_reboot_status()
        self.dom_db_utils = self.create_dom_db_utils(port_obj_dict, self.port_mapping, self.xcvr_table_helper,
                                                     self.task_stopping_event, self.logger)
        self.vdm_db_utils = self.create_vdm_db_utils(port_obj_dict, self.port_mapping, self.xcvr_table_helper,
                                                     self.task_stopping_event, self.logger)
        # Pending at construction: such a namespace gets its start-up pass, with the cleanup of its absent ports, at
        # its admission in task_worker(). The daemon's stale-row cleanup skipped exactly these namespaces; a later
        # snapshot in init() would give a namespace registered in between neither cleanup
        self._pending_namespaces = {ns for ns in self.namespaces if not self.asic_readiness.is_ready(ns)}
        self._deferred_logged = set()       # namespaces for which MEDIA_SETTINGS_DEFERRED was logged
        # Declared here, where the pending snapshot is taken, so that the two agree: the namespaces ready now are
        # exempt from the gate, as at the daemon's start today; a late namespace is marked after its admission pass
        self.asic_readiness.register_publisher(self.PUBLISHER_NAME, set(self.namespaces) - self._pending_namespaces)
        self._pass_deadline = {}            # ns -> monotonic time after which its deferred start-up pass runs anyway
        self._reconciled = set()            # namespaces whose CONFIG_DB reconcile ran (once, at the first admission attempt)

    def create_xcvr_table_helper(self, namespaces):
        return XcvrTableHelper(namespaces)

    def create_dom_db_utils(self, port_obj_dict, port_mapping, xcvr_table_helper, task_stopping_event, logger):
        return DOMDBUtils(port_obj_dict, port_mapping, xcvr_table_helper, task_stopping_event, logger)

    def create_vdm_db_utils(self, port_obj_dict, port_mapping, xcvr_table_helper, task_stopping_event, logger):
        return VDMDBUtils(port_obj_dict, port_mapping, xcvr_table_helper, task_stopping_event, logger)

    def initialize_warm_fast_reboot_status(self):
        warm_fast_reboot_status = {}
        for namespace in self.namespaces:
            # A namespace not ready yet is sampled at the worker's first publication into it, when its databases
            # are populated
            warm_fast_reboot_status[namespace] = self._sample_boot_policy(namespace) \
                if self.asic_readiness.is_ready(namespace) else None
        return warm_fast_reboot_status

    @staticmethod
    def _sample_boot_policy(namespace):
        """True while the namespace is in a warm or fast reboot: the NPU media settings are not published then."""
        return bool(common.is_syncd_warm_restore_complete(namespace) or common.is_fast_reboot_enabled(namespace))

    def _boot_policy(self, namespace):
        """The namespace's sample, taken once: in the constructor for a namespace ready then, otherwise at its pass or
        at the first publication into it, whichever comes first."""
        if namespace not in self.warm_fast_reboot_status:
            return False
        if self.warm_fast_reboot_status[namespace] is None:
            self.warm_fast_reboot_status[namespace] = self._sample_boot_policy(namespace)
        return self.warm_fast_reboot_status[namespace]

    def _recorded_error(self, lport, port_mapping):
        """(recorded, blocking): a platform error recorded for one of lport's physical ports in this worker's error
        cache."""
        recorded = blocking = False
        for pport in port_mapping.logical_port_name_to_physical_port_list(lport):
            entry = self.sfp_error_dict.get(pport)
            if entry is None:
                continue
            try:
                blocks = sfp_status_helper.is_error_block_eeprom_reading(int(entry[0]))
            except (TypeError, ValueError):
                continue                        # not a status value: nothing recorded for this member
            recorded = True
            blocking = blocking or blocks
        return recorded, blocking

    def _namespace_of(self, lport, port_mapping):
        """Managed namespace of lport; None for a port without an ASIC index or outside the managed namespaces."""
        asic_index = port_mapping.get_asic_id_for_logical_port(lport)
        return self.asic_readiness.namespace_for_asic(asic_index) if asic_index is not None else None

    def _publish_media_settings(self, lport, transceiver_dict, port_mapping, xcvr_table_helper=None):
        """Publish NPU media settings for lport now, or defer them to its namespace's admission pass.

        This is the only caller of media_settings_parser.notify_media_setting() in this class; the
        per-namespace warm/fast boot policy is applied here.
        """
        helper = xcvr_table_helper if xcvr_table_helper is not None else self.xcvr_table_helper
        asic_index = port_mapping.get_asic_id_for_logical_port(lport)
        if asic_index is None:
            helper_logger.log_warning("{}: no ASIC index for port, media settings not published".format(lport))
            return
        ns = self.asic_readiness.namespace_for_asic(asic_index)
        if ns in self._pending_namespaces:
            # Nothing is recorded: the namespace's start-up pass runs at its admission and covers every port
            if ns not in self._deferred_logged:
                self._deferred_logged.add(ns)
                helper_logger.log_notice("[{}] MEDIA_SETTINGS_DEFERRED ({}): namespace pending, "
                                         "first pending port {}".format(ns, self.name, lport))
            return
        # Namespace published by this worker, or an ASIC id outside the managed namespaces: the gated workers hold
        # such a port until every namespace is admitted, the publisher has no namespace to defer it to and publishes now
        policy_ns = ns if ns is not None else common.get_namespace_from_asic_id(asic_index)
        if self._boot_policy(policy_ns):
            return
        media_settings_parser.notify_media_setting(lport, transceiver_dict, helper, port_mapping)

    def _admit_pending_namespaces(self):
        """Run the start-up pass for every pending namespace that is now ready, and mark the namespace admitted for
        the module-writing workers. Called once per task_worker() iteration, after the previous iteration's platform
        poll was answered and its events were processed: the platform's event baseline exists before a namespace's
        first read (a module transition after that read is reported as an event) and the errors it reported are in
        the cache. The EEPROM retry runs afterwards."""
        for ns in sorted(n for n in self._pending_namespaces if self.asic_readiness.is_ready(n)):
            if self.task_stopping_event.is_set():
                return
            if ns not in self._reconciled:
                # A port deleted from CONFIG_DB before the worker subscribed had no removal event: it is handled as
                # that event would be, so the pass does not post rows for a port that no longer exists. Once per
                # namespace: later deletions arrive through the subscription
                self._reconciled.add(ns)
                configured = port_event_helper.get_port_mapping([ns])
                for lport in [p for p in self.port_mapping.logical_port_list
                              if self._namespace_of(p, self.port_mapping) == ns and not configured.is_logical_port(p)]:
                    helper_logger.log_notice("[{}] {}: {} deleted from CONFIG_DB before the subscription, removed".format(
                        ns, self.name, lport))
                    self.on_port_config_change(port_event_helper.PortChangeEvent(
                        lport, self.port_mapping.get_logical_to_physical(lport)[0],
                        self.port_mapping.get_asic_id_for_logical_port(lport), port_event_helper.PortChangeEvent.PORT_REMOVE))
            ports = [lport for lport in self.port_mapping.logical_port_list if self._namespace_of(lport, self.port_mapping) == ns]
            # A module inserted moments ago is read by its insertion event first, as at runtime: the pass waits while
            # the namespace has a soaking insertion event, up to two soak periods after its first deferral (a soak
            # does not mature on a platform whose idle polls answer no status)
            soaking = {int(key) for key in self.sfp_insert_events} & \
                      {pport for lport in ports for pport in self.port_mapping.logical_port_name_to_physical_port_list(lport)}
            if soaking:
                if ns not in self._pass_deadline:
                    self._pass_deadline[ns] = time.monotonic() + 2 * MGMT_INIT_TIME_DELAY_SECS
                    helper_logger.log_notice("[{}] {}: start-up pass deferred, {} physical port(s) with a soaking insertion event".format(
                        ns, self.name, len(soaking)))
                if time.monotonic() < self._pass_deadline[ns]:
                    continue
            self._pass_deadline.pop(ns, None)
            self._pending_namespaces.discard(ns)        # the pass publishes into the namespace; so do the paths after it
            self._boot_policy(ns)           # sampled now at the latest: the first publication may be far away
            # Status rows and the cleanup of absent ports before the publication, as the daemon's start cleans before
            # the workers publish: a module that stops answering after its info read is not taken for absent by the
            # same pass
            self._init_port_sfp_status_sw_tbl(self.port_mapping, self.xcvr_table_helper, self.task_stopping_event,
                                              logical_ports=ports, delete_absent=True)
            t_pass = time.monotonic()
            retry = self._post_port_sfp_info_and_dom_thr_to_db_once(self.port_mapping, self.xcvr_table_helper,
                                                                    self.task_stopping_event, logical_ports=ports)
            if self.task_stopping_event.is_set():
                return                                      # interrupted: not marked admitted, no notice
            if retry and not self.retry_eeprom_set:
                self.last_retry_eeprom_time = time.time()   # the pass's entries get the ordinary interval before their first retry
            # The pass's entries join the set: a port the pass read gets one more retry, as after an insertion event
            self.retry_eeprom_set |= retry
            self.asic_readiness.mark_admitted(self.PUBLISHER_NAME, ns)
            helper_logger.log_notice("[{}] {}: Posted all port DOM/SFP info to DB in {:.1f}s ({} ports, {} to retry{})".format(
                ns, self.name, time.monotonic() - t_pass, len(ports), len(retry),
                ", {} physical port(s) still soaking".format(len(soaking)) if soaking else ""))

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

    def _get_port_error_description(self, physical_port):
        if platform_chassis:
            try:
                return platform_chassis.get_sfp(physical_port).get_error_description()
            except NotImplementedError:
                pass
        return None

    def _get_port_change_event(self, timeout):
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

    def _wrapper_is_replaceable(self, physical_port):
        port_device = common.get_port_device(physical_port)
        if port_device is None:
            helper_logger.log_debug("No port device found for port {}".format(physical_port))
            return False

        try:
            return port_device.is_replaceable()
        except NotImplementedError:
            return False

    # Update port sfp info in db
    def post_port_info_to_db(self, logical_port_name, port_mapping, table, transceiver_dict,
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
                    is_replaceable = self._wrapper_is_replaceable(physical_port)
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

    def _is_owned_physical_port(self, physical_port):
        """Return whether this task is responsible for physical_port"""
        return physical_port in self.port_obj_dict

    def _owns_all_physical_ports(self, logical_port_name, physical_port_list):
        """Return whether this task is responsible for every physical port in physical_port_list"""
        physical_port_ownership = {physical_port: self._is_owned_physical_port(physical_port) for physical_port in physical_port_list}
        # All ports owned. This task owns the logical port.
        if all(physical_port_ownership.values()):
            return True
        # Some ports owned, some unowned. This should not happen, report an error.
        if any(physical_port_ownership.values()):
            unowned_physical_ports = [physical_port for physical_port, owned in physical_port_ownership.items() if not owned]
            helper_logger.log_error("{}: This task only owns some of the physical ports for logical port '{}', unowned: {}".format(
                self.name, logical_port_name, unowned_physical_ports))
        # Not all ports owned. This task does not own the logical port.
        return False

    def _get_owned_physical_ports(self, logical_port_name, port_mapping):
        """Return the physical ports of logical_port_name if this task is responsible for every one of them, else None"""
        physical_port_list = port_mapping.logical_port_name_to_physical_port_list(logical_port_name)
        if not physical_port_list:
            helper_logger.log_error("{}: No physical ports found for logical port '{}', ignored".format(
                self.name, logical_port_name))
            return None

        if not self._owns_all_physical_ports(logical_port_name, physical_port_list):
            return None
        return physical_port_list

    def post_port_thresholds_to_db(self, logical_port_name, dom_db_cache=None, vdm_db_cache=None):
        """Post the DOM and VDM thresholds for a port to the DB.

        Args:
            logical_port_name (str): logical port name
            dom_db_cache (dict, optional): per-physical-port cache for the DOM thresholds
            vdm_db_cache (dict, optional): per-physical-port cache for the VDM thresholds
        """
        self.dom_db_utils.post_port_dom_thresholds_to_db(logical_port_name, db_cache=dom_db_cache)
        self.vdm_db_utils.post_port_vdm_thresholds_to_db(logical_port_name, db_cache=vdm_db_cache)

    # Update port sfp info and dom threshold in db during xcvrd bootup
    def _post_port_sfp_info_and_dom_thr_to_db_once(self, port_mapping, xcvr_table_helper, stop_event=threading.Event(),
                                                   logical_ports=None):
        # Connect to STATE_DB and create transceiver dom/sfp info tables
        transceiver_dict = {}
        retry_eeprom_set = set()

        # Post all the current interface sfp/dom threshold info to STATE_DB
        logical_port_list = port_mapping.logical_port_list if logical_ports is None else logical_ports
        for logical_port_name in logical_port_list:
            if stop_event.is_set():
                break

            if self._get_owned_physical_ports(logical_port_name, port_mapping) is None:
                continue

            # Get the asic to which this port belongs
            asic_index = port_mapping.get_asic_id_for_logical_port(logical_port_name)
            if asic_index is None:
                helper_logger.log_warning("Got invalid asic index for {}, ignored while posting SFP info during boot-up".format(logical_port_name))
                continue

            # No EEPROM access behind a recorded blocking error, as on the error event. The port is retried like one
            # whose EEPROM was not ready: an error that clears without an insertion event still gets its rows
            if self._recorded_error(logical_port_name, port_mapping)[1]:
                retry_eeprom_set.add(logical_port_name)
                continue

            rc = self.post_port_info_to_db(logical_port_name, port_mapping, xcvr_table_helper.get_intf_tbl(asic_index), transceiver_dict, stop_event)
            if rc != SFP_EEPROM_NOT_READY:
                # Gated per namespace; the boot policy is applied inside the helper
                self._publish_media_settings(logical_port_name, transceiver_dict, port_mapping,
                                             xcvr_table_helper=xcvr_table_helper)
            else:
                retry_eeprom_set.add(logical_port_name)

        dom_thresholds_cache = {}
        vdm_thresholds_cache = {}
        for logical_port_name in logical_port_list:
            if stop_event.is_set():
                break

            if self._get_owned_physical_ports(logical_port_name, port_mapping) is None:
                continue

            if logical_port_name not in retry_eeprom_set:
                self.post_port_thresholds_to_db(logical_port_name,
                                                dom_db_cache=dom_thresholds_cache,
                                                vdm_db_cache=vdm_thresholds_cache)

        return retry_eeprom_set

    # Init TRANSCEIVER_STATUS_SW table
    def _init_port_sfp_status_sw_tbl(self, port_mapping, xcvr_table_helper, stop_event=threading.Event(), logical_ports=None,
                                     delete_absent=False):
        # Init TRANSCEIVER_STATUS_SW table
        logical_port_list = port_mapping.logical_port_list if logical_ports is None else logical_ports
        for logical_port_name in logical_port_list:
            if stop_event.is_set():
                break

            # Get the asic to which this port belongs
            asic_index = port_mapping.get_asic_id_for_logical_port(logical_port_name)
            if asic_index is None:
                helper_logger.log_warning("Got invalid asic index for {}, ignored during sfp status table init".format(logical_port_name))
                continue

            physical_port_list = port_mapping.logical_port_name_to_physical_port_list(logical_port_name)
            if not physical_port_list:
                # No physical port means nothing can be plugged into this logical port, so mark it
                # REMOVED regardless of which task owns it.
                helper_logger.log_error("No physical ports found for logical port '{}' during sfp status table init".format(logical_port_name))
                common.update_port_transceiver_status_table_sw(logical_port_name, xcvr_table_helper.get_status_sw_tbl(asic_index), sfp_status_helper.SFP_STATUS_REMOVED)
                continue

            if not self._owns_all_physical_ports(logical_port_name, physical_port_list):
                continue

            for physical_port in physical_port_list:
                if stop_event.is_set():
                    break

                # No presence read behind a recorded blocking error (on some platforms it is an EEPROM read, which
                # answers "absent" for a present module): the error row stands until the port's next event
                if self._recorded_error(logical_port_name, port_mapping)[1]:
                    continue
                if common._wrapper_get_presence(physical_port):
                    # The status row an error event wrote stands while the module is present
                    if self._recorded_error(logical_port_name, port_mapping)[0]:
                        continue
                    common.update_port_transceiver_status_table_sw(logical_port_name, xcvr_table_helper.get_status_sw_tbl(asic_index), sfp_status_helper.SFP_STATUS_INSERTED)
                    continue
                if delete_absent:
                    # Admission pass: a module gone while the namespace was pending had no removal event handled; its
                    # info rows are removed as the daemon's start removes stale ones, the other rows stand
                    common.del_port_sfp_dom_info_from_db(logical_port_name, port_mapping, xcvr_table_helper.get_info_tables(asic_index))
                common.update_port_transceiver_status_table_sw(logical_port_name, xcvr_table_helper.get_status_sw_tbl(asic_index), sfp_status_helper.SFP_STATUS_REMOVED)

    def init(self):
        port_mapping_data = port_event_helper.get_port_mapping(self.namespaces)

        # The constructor's readiness snapshot decides both lists: the start-up pass covers the ports of the
        # namespaces ready then, every other namespace gets the same pass at its admission in task_worker()
        ports = None
        if self._pending_namespaces:
            by_ns = collections.Counter(self._namespace_of(lport, port_mapping_data)
                                        for lport in port_mapping_data.logical_port_list)
            for ns in sorted(self._pending_namespaces):
                helper_logger.log_notice("[{}] PORTS_DEFERRED ({}): namespace not ready, start-up pass deferred ({} ports)".format(
                    ns, self.name, by_ns.get(ns, 0)))
            ports = [lport for lport in port_mapping_data.logical_port_list
                     if self._namespace_of(lport, port_mapping_data) not in self._pending_namespaces]

        # Post all the current interface sfp/dom threshold info to STATE_DB
        t_pass = time.monotonic()
        self.retry_eeprom_set = self._post_port_sfp_info_and_dom_thr_to_db_once(port_mapping_data, self.xcvr_table_helper,
                                                                                self.main_thread_stop_event, logical_ports=ports)
        helper_logger.log_notice("{}: Posted all port DOM/SFP info to DB in {:.1f}s ({} ports, {} to retry)".format(
            self.name, time.monotonic() - t_pass, len(port_mapping_data.logical_port_list if ports is None else ports), len(self.retry_eeprom_set)))

        # Init port sfp status sw table
        t_status = time.monotonic()
        self._init_port_sfp_status_sw_tbl(port_mapping_data, self.xcvr_table_helper, self.main_thread_stop_event, logical_ports=ports)
        helper_logger.log_notice("{}: Initialized port sfp status table in {:.1f}s".format(self.name, time.monotonic() - t_status))

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
        platform_polled = False     # the previous poll was answered (no system event): the event baseline exists
        while not stopping_event.is_set():
            port_event_helper.handle_port_config_change(sel, asic_context, stopping_event, self.port_mapping, helper_logger, self.on_port_config_change)

            # Start-up pass for the namespaces that became ready since init() or the previous iteration, once the
            # platform answered a poll (its event baseline exists before a namespace's first read) and that poll's
            # events were processed (their errors are in the cache); the EEPROM retry runs afterwards
            if platform_polled:
                self._admit_pending_namespaces()

            # Retry those logical ports whose EEPROM reading failed or timeout when the SFP is inserted
            self.retry_eeprom_reading()
            next_state = state
            time_start = time.time()
            # Ensure not to block for any event if sfp insert event is pending
            if self.sfp_insert_events:
                timeout = SFP_INSERT_EVENT_POLL_PERIOD_MSECS
            # A pending namespace is admitted at most one poll after its marker: poll at the start-up period meanwhile
            poll_timeout = min(timeout, RETRY_PERIOD_FOR_SYSTEM_READY_MSECS) if self._pending_namespaces else timeout
            status, port_dict, error_dict = self._get_port_change_event(poll_timeout)
            # Answered: a successful poll, or one that reported no system event (handled below as no event)
            platform_polled = bool(status) or not port_dict
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
                    # The error cache is keyed by the integer port index, its description dictionary included: a
                    # platform may key its events as str or int, and on_add_logical_port() looks both up by index
                    cached_error_dict = {}
                    for k, v in (error_dict or {}).items():
                        try:
                            cached_error_dict[int(k)] = v
                        except (TypeError, ValueError):
                            cached_error_dict[k] = v
                    for key, value in port_dict.items():
                        # Validate that this event belongs to the set of ports this task is responsible for.
                        if int(key) not in self.port_obj_dict:
                            helper_logger.log_error("Got event for port {} not owned by {}, ignored".format(key, self.name))
                            continue
                        # SFP error event should be cached because: when a logical port is created, there is no way to
                        # detect the SFP error by platform API.
                        error_key = int(key)
                        if value != sfp_status_helper.SFP_STATUS_INSERTED and value != sfp_status_helper.SFP_STATUS_REMOVED:
                            self.sfp_error_dict[error_key] = (value, cached_error_dict)
                        else:
                            self.sfp_error_dict.pop(error_key, None)
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
                                rc = self.post_port_info_to_db(logical_port, self.port_mapping, self.xcvr_table_helper.get_intf_tbl(asic_index), transceiver_dict)
                                # If we didn't get the sfp info, assuming the eeprom is not ready, give a try again.
                                if rc == SFP_EEPROM_NOT_READY:
                                    helper_logger.log_warning("{}: SFP EEPROM is not ready. One more try...".format(logical_port))
                                    time.sleep(TIME_FOR_SFP_READY_SECS)
                                    rc = self.post_port_info_to_db(logical_port, self.port_mapping, self.xcvr_table_helper.get_intf_tbl(asic_index), transceiver_dict)
                                    if rc == SFP_EEPROM_NOT_READY:
                                        # If still failed to read EEPROM, put it to retry set
                                        self.retry_eeprom_set.add(logical_port)

                                if rc != SFP_EEPROM_NOT_READY:
                                    self.post_port_thresholds_to_db(logical_port)

                                    self._publish_media_settings(logical_port, transceiver_dict, self.port_mapping)
                                    transceiver_dict.clear()
                            elif value == sfp_status_helper.SFP_STATUS_REMOVED:
                                # Remove the API object for this physical port
                                try:
                                    device = common.get_port_device(int(key))
                                    device.remove_xcvr_api()
                                except (NotImplementedError, AttributeError) as e:
                                    helper_logger.log_error(f"Failed to remove xcvr api for port {key}: {str(e)}")
                                helper_logger.log_notice("{}: Got SFP removed event".format(logical_port))
                                state_port_table = self.xcvr_table_helper.get_state_port_tbl(asic_index)
                                state_port_table.set(logical_port, [(NPU_SI_SETTINGS_SYNC_STATUS_KEY, NPU_SI_SETTINGS_DEFAULT_VALUE)])
                                common.update_port_transceiver_status_table_sw(
                                    logical_port, self.xcvr_table_helper.get_status_sw_tbl(asic_index), sfp_status_helper.SFP_STATUS_REMOVED)
                                helper_logger.log_notice("{}: received plug out and update port sfp status table.".format(logical_port))
                                self.delete_port_data_from_db(logical_port, asic_index,
                                                             delete_intf_tbl=True)
                            else:
                                try:
                                    error_bits = int(value)
                                    helper_logger.log_error("{}: Got SFP error event {}".format(logical_port, value))

                                    error_descriptions = sfp_status_helper.fetch_generic_error_description(error_bits)

                                    if sfp_status_helper.has_vendor_specific_error(error_bits):
                                        if error_dict:
                                            vendor_specific_error_description = error_dict.get(key)
                                        else:
                                            vendor_specific_error_description = self._get_port_error_description(int(key))
                                        error_descriptions.append(vendor_specific_error_description)

                                    # Add error info to database
                                    # Any existing error will be replaced by the new one.
                                    common.update_port_transceiver_status_table_sw(logical_port, self.xcvr_table_helper.get_status_sw_tbl(asic_index), value, '|'.join(error_descriptions))
                                    helper_logger.log_notice("{}: Receive error update port sfp status table.".format(logical_port))
                                    # In this case EEPROM is not accessible. The DOM info will be removed since it can be out-of-date.
                                    # The interface info remains in the DB since it is static.
                                    if sfp_status_helper.is_error_block_eeprom_reading(error_bits):
                                        self.delete_port_data_from_db(logical_port, asic_index)
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
        # Thread.ident is set as soon as start() returns, before the thread body runs
        res = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(self.ident),
              ctypes.py_object(SystemExit))
        if res > 1:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(self.ident), 0)
            helper_logger.log_error('Exception raise failure for SfpStateUpdateTask')

    def join(self):
        self.task_stopping_event.set()
        threading.Thread.join(self)
        if self.exc:
            raise self.exc

    def delete_port_data_from_db(self, logical_port_name, asic_index,
                                 delete_intf_tbl=False, delete_status_sw_tbl=False):
        """Delete data of a logical port from the DB.

        The DOM, VDM, status, PM and firmware info tables are always deleted, since their
        content is only meaningful while the transceiver is present and its EEPROM readable.

        Args:
            logical_port_name (str): logical port name
            asic_index (int): the asic index the logical port belongs to
            delete_intf_tbl (bool): also delete TRANSCEIVER_INFO. It is permitted to keep it
                in some circumstances, since it contains static data only.
            delete_status_sw_tbl (bool): also delete TRANSCEIVER_STATUS_SW. It is kept while
                the logical port exists, since the SFP status/error is recorded there.
        """
        tbl_to_del_list = [
            *self.xcvr_table_helper.get_dom_tables(asic_index),
            *self.xcvr_table_helper.get_vdm_tables(asic_index),
            *self.xcvr_table_helper.get_status_tables(asic_index, include_sw=delete_status_sw_tbl),
        ]
        if delete_intf_tbl:
            tbl_to_del_list.extend(self.xcvr_table_helper.get_info_tables(asic_index))
        common.del_port_sfp_dom_info_from_db(logical_port_name, self.port_mapping, tbl_to_del_list)

    def on_port_config_change(self , port_change_event):
        # The port mapping contains all ports on the switch, so it is kept up to date for every port.
        # The DB updates are only performed for the ports this task is responsible for.
        is_owned_port = self._is_owned_physical_port(port_change_event.port_index)

        if port_change_event.event_type == port_event_helper.PortChangeEvent.PORT_REMOVE:
            if is_owned_port:
                self.on_remove_logical_port(port_change_event)
            self.port_mapping.handle_port_change_event(port_change_event)
        elif port_change_event.event_type == port_event_helper.PortChangeEvent.PORT_ADD:
            self.port_mapping.handle_port_change_event(port_change_event)
            if is_owned_port:
                self.on_add_logical_port(port_change_event)

    def on_remove_logical_port(self, port_change_event):
        """Called when a logical port is removed from CONFIG_DB.

        Args:
            port_change_event (object): port change event
        """
        # To avoid race condition, remove the entry TRANSCEIVER_DOM_INFO, TRANSCEIVER_STATUS_INFO and TRANSCEIVER_INFO table.
        # The operation to remove entry from TRANSCEIVER_DOM_INFO is duplicate with DomInfoUpdateTask.on_remove_logical_port,
        # but it is necessary because TRANSCEIVER_DOM_INFO is also updated in this thread when a new SFP is inserted.
        self.delete_port_data_from_db(port_change_event.port_name,
                                      port_change_event.asic_id,
                                      delete_intf_tbl=True,
                                      delete_status_sw_tbl=True)

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
                    vendor_specific_error_description = self._get_port_error_description(port_change_event.port_index)
                error_descriptions.append(vendor_specific_error_description)

            error_description = '|'.join(error_descriptions)
            helper_logger.log_info("Receive error update port sfp status table.")
            if sfp_status_helper.is_error_block_eeprom_reading(error_bits):
                read_eeprom = False

        # SFP information not in DB
        if common._wrapper_get_presence(port_change_event.port_index) and read_eeprom:
            transceiver_dict = {}
            status = sfp_status_helper.SFP_STATUS_INSERTED if not status else status
            rc = self.post_port_info_to_db(port_change_event.port_name, self.port_mapping, int_tbl, transceiver_dict)
            if rc == SFP_EEPROM_NOT_READY:
                # Failed to read EEPROM, put it to retry set
                self.retry_eeprom_set.add(port_change_event.port_name)
            else:
                self.post_port_thresholds_to_db(port_change_event.port_name)
                self._publish_media_settings(port_change_event.port_name, transceiver_dict, self.port_mapping)
        else:
            status = sfp_status_helper.SFP_STATUS_REMOVED if not status else status
        common.update_port_transceiver_status_table_sw(port_change_event.port_name, status_sw_tbl, status, error_description)

    def retry_eeprom_reading(self):
        """Retry EEPROM reading, if retry succeed, remove the logical port from the retry set
        """
        if not self.retry_eeprom_set:
            return

        # Retry eeprom with an interval RETRY_EEPROM_READING_INTERVAL. No need to put sleep here
        # because _get_port_change_event has a timeout argument.
        now = time.time()
        if now - self.last_retry_eeprom_time < self.RETRY_EEPROM_READING_INTERVAL:
            return

        self.last_retry_eeprom_time = now

        transceiver_dict = {}
        retry_success_set = set()
        for logical_port in self.retry_eeprom_set:
            asic_index = self.port_mapping.get_asic_id_for_logical_port(logical_port)
            rc = self.post_port_info_to_db(logical_port, self.port_mapping, self.xcvr_table_helper.get_intf_tbl(asic_index), transceiver_dict)
            if rc != SFP_EEPROM_NOT_READY:
                self.post_port_thresholds_to_db(logical_port)

                self._publish_media_settings(logical_port, transceiver_dict, self.port_mapping)
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
    def __init__(self, log_identifier, skip_cmis_mgr=False, enable_sff_mgr=False, dom_temperature_poll_interval=None, dom_update_interval=None, skip_cpo_mgr=False):
        super(DaemonXcvrd, self).__init__(log_identifier, enable_runtime_log_config=True)
        self.stop_event = threading.Event()
        self.sfp_error_event = threading.Event()
        self.skip_cmis_mgr = skip_cmis_mgr
        self.skip_cpo_mgr = skip_cpo_mgr
        self.enable_sff_mgr = enable_sff_mgr
        self.dom_temperature_poll_interval = dom_temperature_poll_interval
        self.dom_update_interval = dom_update_interval
        self.namespaces = ['']
        self.threads = []
        self.sfp_obj_dict = {}
        self.cpo_obj_dict = {}
        # Per-namespace readiness registry, built in init() from the discovered namespaces
        self.asic_readiness = None
        # Readiness checker thread -> main thread: namespace names, or ASIC_CHECKER_FAILED
        self.ready_asic_queue = queue.Queue()
        # Set by the checker after its first sweep over every namespace (or when it exits)
        self.initial_scan_done = threading.Event()
        # The checker thread once started (init()); run() joins it at shutdown
        self.readiness_thread = None

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

    def _asic_readiness_checker(self):
        """Watch APPL_DB PORT_TABLE of every front-end namespace for its readiness marker.

        One Select, one SubscriberStateTable per namespace. Each OBJECT result sweeps every pending
        table: the table is drained completely (a partially drained table keeps the Select ready),
        and once a SET of a marker key was popped the table is removed from the Select and released
        so that the namespace's subscription connection goes away and its later PORT_TABLE traffic
        no longer wakes this thread. Ready namespaces are handed to the main thread through
        ready_asic_queue; the thread exits when every namespace is ready or stop_event is set.
        Any exception, or a persistent non-OBJECT/non-TIMEOUT select() result, ends in the
        ASIC_CHECKER_FAILED sentinel, which the main thread turns into a fatal shutdown.
        """
        try:
            sel = swsscommon.Select()
            pending = {}                                    # table -> (namespace, connector): released together
            for ns in self.namespaces:
                if self.stop_event.is_set():
                    return
                appl_db = daemon_base.db_connect("APPL_DB", namespace=ns)
                tbl = swsscommon.SubscriberStateTable(appl_db, swsscommon.APP_PORT_TABLE_NAME)
                sel.addSelectable(tbl)
                pending[tbl] = (ns, appl_db)
                self.log_notice("[{}] ASIC_WAITING: waiting for {} in APPL_DB PORT_TABLE".format(ns, "/".join(PORT_READY_KEYS)))
            del appl_db, tbl                                # only `pending` references them (namespaces is non-empty)
            t0 = last_report = time.monotonic()
            select_errors = 0
            while pending and not self.stop_event.is_set():
                state = sel.select(port_event_helper.SELECT_TIMEOUT_MSECS)[0]    # do not keep the returned selectable alive
                now = time.monotonic()
                if now - last_report >= ASIC_STILL_WAITING_LOG_SECS:   # heartbeat, independent of `state`
                    self.log_notice("ASIC_STILL_WAITING: {} not ready after {}s".format(
                        sorted(ns for ns, _ in pending.values()), int(now - t0)))
                    last_report = now
                if state == swsscommon.Select.TIMEOUT:
                    select_errors = 0
                    self.initial_scan_done.set()
                    continue
                if state != swsscommon.Select.OBJECT:
                    select_errors += 1
                    self.log_warning("ASIC readiness: select() returned {} ({} consecutive)".format(state, select_errors))
                    if select_errors >= ASIC_CHECKER_MAX_SELECT_ERRORS:
                        raise RuntimeError("select() failed {} times in a row".format(select_errors))
                    self.stop_event.wait(1)
                    continue
                select_errors = 0
                sweep = collections.deque(pending)          # sweep every pending table (in namespace order), not only the signalled one
                while sweep:
                    tbl = sweep.popleft()                   # the sweep holds no reference to tables already visited
                    ns = pending[tbl][0]
                    seen = None
                    while not self.stop_event.is_set():     # drain completely; do not stop at the marker
                        key, op, _ = tbl.pop()
                        if not key:
                            break
                        if op == swsscommon.SET_COMMAND and key in PORT_READY_KEYS and seen is None:
                            seen = key
                    if seen:
                        sel.removeSelectable(tbl)           # stop listening ...
                        del pending[tbl]                    # ... and drop the table and connector references
                        self.log_notice("[{}] ASIC_READY: {} received after {:.1f}s".format(ns, seen, now - t0))
                        self.ready_asic_queue.put(ns)
                    del tbl                                 # released here, before the next table is drained
                self.initial_scan_done.set()
        except Exception:
            self.log_error("ASIC_CHECKER_FAILED: {}".format(traceback.format_exc()))
            self.ready_asic_queue.put(ASIC_CHECKER_FAILED)
        finally:
            self.initial_scan_done.set()

    def _next_ready_asic(self, timeout):
        """Next item from the checker (a namespace or ASIC_CHECKER_FAILED), or None.

        timeout=None: wait until an item arrives or stop_event is set (polled every second).
        timeout=t (0 < t <= 1): wait at most t seconds, then None.
        """
        while not self.stop_event.is_set():
            try:
                return self.ready_asic_queue.get(timeout=1 if timeout is None else timeout)
            except queue.Empty:
                if timeout is not None:
                    return None
        return None

    def _drain_ready_queue(self):
        """Everything currently queued, without waiting."""
        items = []
        while True:
            try:
                items.append(self.ready_asic_queue.get_nowait())
            except queue.Empty:
                return items

    def on_asic_ready(self, namespace):
        """Register a namespace as ready: from now on the publishing workers may publish into it and run its
        start-up pass. Logged before the registry changes."""
        self.log_notice("[{}] ASIC_ACTIVE: registering; {}/{} namespace(s) ready".format(
            namespace, len(self.asic_readiness.ready_namespaces()) + 1, len(self.namespaces)))
        self.asic_readiness.mark_ready(namespace)

    def _exit_checker_failed(self):
        """The checker failed while init() was still waiting: no worker exists yet, so the daemon exits for a
        supervisord restart without the worker shutdown sequence of run()."""
        self.log_error("ASIC readiness checker failed; shutting down for restart")
        self._join_checker()
        sys.exit(SFP_SYSTEM_ERROR)

    def _join_checker(self):
        """Join the readiness checker with a bounded budget. It is a daemon thread that returns within a second
        of stop_event; one stuck in a native call cannot block the process exit and does not turn an intentional
        stop into a restart, so a timeout is logged, not fatal."""
        self.readiness_thread.join(ASIC_CHECKER_JOIN_TIMEOUT_SECS)
        if self.readiness_thread.is_alive():
            self.log_error("readiness checker did not stop within {}s".format(ASIC_CHECKER_JOIN_TIMEOUT_SECS))

    def _describe_obj_dicts(self, port_mapping_data, elapsed):
        """One-line summary for the OBJ_DICT_BUILT notice."""
        per_ns = dict.fromkeys(self.namespaces, 0)      # a namespace without CONFIG_DB ports shows as 0
        for pport, lports in port_mapping_data.physical_to_logical.items():
            per_ns[self.asic_readiness.namespace_for_asic(port_mapping_data.get_asic_id_for_logical_port(lports[0]))] += 1
        missing = sum(1 for pport in port_mapping_data.physical_to_logical
                      if self.sfp_obj_dict.get(pport) is None and self.cpo_obj_dict.get(pport) is None)
        return "{:.1f}s; ports per namespace {}; ports without device object {}".format(elapsed, per_ns, missing)

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
            if not self.asic_readiness.is_asic_ready(asic_index):
                continue                        # a pending namespace is read, and cleaned, at its admission
            info_tables = self.xcvr_table_helper.get_info_tables(asic_index)
            if not any(tbl.get(lport)[0] for tbl in info_tables):
                continue

            # If transceiver is absent, remove the entry from the info tables
            pport_list = port_mapping_data.get_logical_to_physical(lport)
            if not pport_list:
                self.log_error(f"Remove stale transceiver info: No physical port found for lport {lport}")
                continue
            pport = pport_list[0]

            if not common._wrapper_get_presence(pport):
                self.log_notice(f"Remove stale transceiver info: Transceiver is absent for lport {lport}")
                common.del_port_sfp_dom_info_from_db(lport, port_mapping_data, info_tables)

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

        # Imported here rather than at module scope because cpo_state_task imports from this module
        # Initialize shared chassis global in cpo_state_task.
        from .cpo import cpo_state_task
        cpo_state_task.platform_chassis = platform_chassis

        if multi_asic.is_multi_asic():
            # Load the namespace details first from the database_global.json file.
            swsscommon.SonicDBConfig.initializeGlobalConfig()
        # To prevent race condition in get_all_namespaces() we cache the namespaces before
        # creating any worker threads
        self.namespaces = multi_asic.get_front_end_namespaces()
        if not self.namespaces:
            self.log_error("XCVRD INIT: no front-end namespaces found")
            sys.exit(PORT_CONFIG_LOAD_ERROR)
        self.log_notice("ASIC_DISCOVERED: {} namespace(s): {}".format(len(self.namespaces), self.namespaces))

        if all(common.is_fast_reboot_enabled(ns) for ns in self.namespaces):
            self.log_notice("Skip loading media_settings.json and optics_si_settings.json in case of fast-reboot")
        else:
            media_settings_parser.load_media_settings()
            optics_si_parser.load_optics_si_settings()

        # Per-namespace readiness: one checker thread watches APPL_DB PORT_TABLE of every namespace for the
        # readiness markers (PORT_READY_KEYS). init() continues as soon as the first namespace is ready and
        # the checker's first sweep is complete; run() registers the remaining namespaces as they become ready.
        self.asic_readiness = AsicReadiness(self.namespaces)
        self.log_notice("XCVRD INIT: Wait for port config is done")
        checker = threading.Thread(target=self._asic_readiness_checker, name="AsicReadinessChecker", daemon=True)
        checker.start()
        self.readiness_thread = checker                     # assigned once started: run() joins it at shutdown

        first = self._next_ready_asic(timeout=None)
        if first is None:
            return None                                     # stopping
        if first is ASIC_CHECKER_FAILED:
            self._exit_checker_failed()
        self.on_asic_ready(first)
        while not self.initial_scan_done.wait(timeout=1):   # cancellable, no give-up timeout
            if self.stop_event.is_set():
                return None
        for item in self._drain_ready_queue():              # namespaces that were ready in the first sweep
            if item is ASIC_CHECKER_FAILED:
                self._exit_checker_failed()
            self.on_asic_ready(item)
        if self.stop_event.is_set():
            return None
        self.log_notice("XCVRD INIT: After port config is done")

        port_mapping_data = port_event_helper.get_port_mapping(self.namespaces)

        t_build = time.monotonic()
        self.sfp_obj_dict = common.get_pluggable_obj_dict(port_mapping_data)
        self.cpo_obj_dict = common.get_cpo_obj_dict(port_mapping_data)
        self.log_notice("OBJ_DICT_BUILT: " + self._describe_obj_dicts(port_mapping_data, time.monotonic() - t_build))
        if self.stop_event.is_set():
            return None

        # Initialize xcvr table helper. CPO platforms have ELS tables in addition to the common ones.
        if self.cpo_obj_dict:
            self.xcvr_table_helper = CpoXcvrTableHelper(self.namespaces)
        else:
            self.xcvr_table_helper = XcvrTableHelper(self.namespaces)

        self.initialize_port_init_control_fields_in_port_table(port_mapping_data)
        if self.stop_event.is_set():
            return None

        # Remove the TRANSCEIVER_INFO table if the transceiver is absent.
        # This ensures stale entries are cleaned up when a transceiver is removed while xcvrd is not running.
        # Performed in the init() method to ensure the table is cleared before starting child threads.
        # Note: Other transceiver-related tables are cleared during xcvrd deinitialization.
        self.remove_stale_transceiver_info(port_mapping_data)
        if self.stop_event.is_set():
            return None

        return port_mapping_data

    # Deinitialize daemon
    def deinit(self):
        self.log_info("Start daemon deinit...")

        # Pre-fetch warm/fast reboot status for all namespaces/ASICs
        warm_fast_reboot_status = {}
        for namespace in self.namespaces:
            warm_fast_reboot_status[namespace] = common.is_syncd_warm_restore_complete(namespace) or common.is_fast_reboot_enabled(namespace)

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

            # Skip deleting the info tables for avoiding OA to trigger Tx disable signal
            # due to TRANSCEIVER_INFO table deletion during xcvrd shutdown/crash
            common.del_port_sfp_dom_info_from_db(logical_port_name, port_mapping_data, [
                                          *self.xcvr_table_helper.get_dom_tables(asic_index),
                                          *self.xcvr_table_helper.get_vdm_tables(asic_index),
                                          self.xcvr_table_helper.get_status_flag_tbl(asic_index),
                                          self.xcvr_table_helper.get_status_flag_change_count_tbl(asic_index),
                                          self.xcvr_table_helper.get_status_flag_set_time_tbl(asic_index),
                                          self.xcvr_table_helper.get_status_flag_clear_time_tbl(asic_index),
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
        if port_mapping_data is None:
            # Stop requested before or during init(): no worker exists; the checker is joined and deinit()
            # runs, as they do today when a stop arrives during the marker wait
            self._join_checker()
            self.deinit()
            return

        # Start the SFF manager
        sff_manager = None
        if self.enable_sff_mgr:
            sff_manager = SffManagerTask(self.namespaces, self.stop_event, platform_chassis, helper_logger,
                                         asic_readiness=self.asic_readiness)
            sff_manager.start()
            self.threads.append(sff_manager)
        else:
            self.log_notice("Skipping SFF Task Manager")

        # Start the CMIS manager
        cmis_manager = None
        if self.sfp_obj_dict and not self.skip_cmis_mgr:
            cmis_manager = CmisManagerTask(self.namespaces, port_mapping_data, self.sfp_obj_dict, self.stop_event, skip_cmis_mgr=self.skip_cmis_mgr,
                                           asic_readiness=self.asic_readiness)
            cmis_manager.start()
            self.threads.append(cmis_manager)

        # Start the CPO manager
        cpo_manager = None
        if self.cpo_obj_dict and not self.skip_cpo_mgr:
            cpo_manager = CpoManagerTask(self.namespaces, port_mapping_data, self.cpo_obj_dict, self.stop_event, skip_cpo_mgr=self.skip_cpo_mgr,
                                         asic_readiness=self.asic_readiness)
            cpo_manager.start()
            self.threads.append(cpo_manager)

        # Start the dom sensor info update thread
        dom_info_update = DomInfoUpdateTask(self.namespaces, port_mapping_data, self.sfp_obj_dict, self.stop_event, self.skip_cmis_mgr, self.dom_update_interval,
                                            asic_readiness=self.asic_readiness)
        dom_info_update.start()
        self.threads.append(dom_info_update)

        # Start the CPO dom sensor info update thread
        cpo_dom_info_update = None
        if self.cpo_obj_dict:
            cpo_dom_info_update = CpoDomInfoUpdateTask(self.namespaces, port_mapping_data, self.cpo_obj_dict, self.stop_event, False, self.dom_update_interval,
                                                       asic_readiness=self.asic_readiness)
            cpo_dom_info_update.start()
            self.threads.append(cpo_dom_info_update)

        # Start the dom thermal sensor info update thread
        dom_thermal_info_update = None
        if self.dom_temperature_poll_interval is not None:
            dom_thermal_info_update = DomThermalInfoUpdateTask(self.namespaces, port_mapping_data, self.sfp_obj_dict, self.stop_event,
                                                               self.dom_temperature_poll_interval)
            dom_thermal_info_update.start()
            self.threads.append(dom_thermal_info_update)

        # Start the sfp state info update thread
        sfp_state_update = None
        if self.sfp_obj_dict:
            sfp_state_update = SfpStateUpdateTask(self.namespaces, port_mapping_data, self.sfp_obj_dict, self.stop_event, self.sfp_error_event,
                                                  asic_readiness=self.asic_readiness)
            sfp_state_update.start()
            self.threads.append(sfp_state_update)
        else:
            self.log_notice("Skipping SfpStateUpdateTask as no pluggable modules are present")

        # Start the CPO state info update thread
        cpo_state_update = None
        if self.cpo_obj_dict:
            from .cpo.cpo_state_task import CpoStateUpdateTask
            cpo_state_update = CpoStateUpdateTask(self.namespaces, port_mapping_data, self.cpo_obj_dict, self.stop_event, self.sfp_error_event,
                                                  asic_readiness=self.asic_readiness)
            cpo_state_update.start()
            self.threads.append(cpo_state_update)

        # Start main loop
        self.log_notice("Start daemon main loop with thread count {}".format(len(self.threads)))
        for thread in self.threads:
            self.log_notice("Started thread {}".format(thread.name))

        # Register the remaining namespaces as they become ready, then wait for the stop request. A checker
        # failure ends the daemon with a non-zero status (below) so that supervisord restarts it; the
        # steady-state wait is never entered with a dead checker.
        checker_failed = False
        while not self.stop_event.is_set():
            if self.asic_readiness.all_ready():
                self.log_notice("ASIC_ALL_READY: {} namespace(s) ready".format(len(self.namespaces)))
                self.stop_event.wait()
                break
            item = self._next_ready_asic(timeout=1)
            if item is ASIC_CHECKER_FAILED:
                self.log_error("ASIC readiness checker failed; shutting down for restart")
                checker_failed = True
                self.stop_event.set()
                break
            if item is not None:
                self.on_asic_ready(item)

        self.log_notice("Stop daemon main loop")

        # Ask every worker to stop before joining any of them: the joins below run one worker at a time, and
        # a worker still between start() and its loop must find its flag set on entry
        for thread in self.threads:
            thread.task_stopping_event.set()

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

        # Stop the CPO manager
        if cpo_manager is not None:
            if cpo_manager.is_alive():
                cpo_manager.join()

        # Stop the dom sensor info update thread
        if dom_info_update.is_alive():
            dom_info_update.join()

        # Stop the CPO dom sensor info update thread
        if cpo_dom_info_update is not None:
            if cpo_dom_info_update.is_alive():
                cpo_dom_info_update.join()

        # Stop the dom thermal sensor info update thread
        if dom_thermal_info_update is not None:
            if dom_thermal_info_update.is_alive():
                dom_thermal_info_update.join()

        # Stop the sfp state info update thread
        if sfp_state_update is not None:
            if sfp_state_update.is_alive():
                sfp_state_update.raise_exception()
                sfp_state_update.join()

        # Stop the CPO state info update thread
        if cpo_state_update is not None:
            if cpo_state_update.is_alive():
                cpo_state_update.raise_exception()
                cpo_state_update.join()

        # Stop the readiness checker
        self._join_checker()

        # Start daemon deinitialization sequence
        self.deinit()

        self.log_info("Shutting down...")

        if self.sfp_error_event.is_set() or checker_failed:
            sys.exit(SFP_SYSTEM_ERROR)


#
# Main =========================================================================
#

# This is our main entry point for xcvrd script


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--skip_cmis_mgr', action='store_true')
    parser.add_argument('--skip_cpo_mgr', action='store_true')
    parser.add_argument('--enable_sff_mgr', action='store_true')
    parser.add_argument('--dom_temperature_poll_interval', default=None, type=int)
    parser.add_argument('--dom_update_interval', default=None, type=int)

    args = parser.parse_args()
    xcvrd = DaemonXcvrd(SYSLOG_IDENTIFIER, args.skip_cmis_mgr, args.enable_sff_mgr,
                        args.dom_temperature_poll_interval, args.dom_update_interval,
                        args.skip_cpo_mgr)
    xcvrd.run()


if __name__ == '__main__':
    main()
