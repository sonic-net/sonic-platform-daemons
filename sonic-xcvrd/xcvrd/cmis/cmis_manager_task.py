#!/usr/bin/env python3

"""
    cmis_manager_task
    CMIS transceiver management task for SONiC
"""

try:
    import threading
    import time
    import datetime
    from swsscommon import swsscommon
    from sonic_py_common import syslogger, daemon_base
    from sonic_platform_base.sonic_xcvr.api.public.c_cmis import CmisApi

    from ..xcvrd_utilities import common
    from ..xcvrd_utilities.common import (
        CMIS_STATE_UNKNOWN, CMIS_STATE_INSERTED, CMIS_STATE_DP_PRE_INIT_CHECK,
        CMIS_STATE_DP_DEINIT, CMIS_STATE_AP_CONF, CMIS_STATE_DP_ACTIVATE,
        CMIS_STATE_DP_INIT, CMIS_STATE_DP_TXON, CMIS_STATE_READY,
        CMIS_STATE_REMOVED, CMIS_STATE_FAILED, CMIS_TERMINAL_STATES
    )
    from ..xcvrd_utilities.xcvr_table_helper import XcvrTableHelper
    from ..xcvrd_utilities import port_event_helper
    from ..xcvrd_utilities.port_event_helper import PortChangeObserver
    from ..xcvrd_utilities import media_settings_parser
    from ..xcvrd_utilities import optics_si_parser
    from ..xcvrd_utilities import sfp_status_helper

except ImportError as e:
    raise ImportError(str(e) + " - required module not found")

# Constants
SYSLOG_IDENTIFIER_CMIS = "CmisManagerTask"

# Global logger instance
helper_logger = syslogger.SysLogger(SYSLOG_IDENTIFIER_CMIS, enable_runtime_config=True)

# Thread wrapper class for CMIS transceiver management

class CmisManagerTask(threading.Thread):

    CMIS_MAX_RETRIES     = 3
    CMIS_DEF_EXPIRED     = 60 # seconds, default expiration time
    CMIS_MODULE_TYPES    = ['QSFP-DD', 'QSFP_DD', 'OSFP', 'OSFP-8X', 'QSFP+C']
    CMIS_MAX_HOST_LANES    = 8
    CMIS_EXPIRATION_BUFFER_MS = 2
    ALL_LANES_MASK = 0xff

    def __init__(self, namespaces, port_mapping, main_thread_stop_event, skip_cmis_mgr=False, platform_chassis=None):
        threading.Thread.__init__(self)
        self.name = "CmisManagerTask"
        self.exc = None
        self.task_stopping_event = threading.Event()
        self.main_thread_stop_event = main_thread_stop_event
        self.port_dict = {k: {"asic_id": v} for k, v in port_mapping.logical_to_asic.items()}
        self.decomm_pending_dict = {}
        self.isPortInitDone = False
        self.isPortConfigDone = False
        self.skip_cmis_mgr = skip_cmis_mgr
        self.namespaces = namespaces
        self.platform_chassis = platform_chassis
        self.xcvr_table_helper = XcvrTableHelper(self.namespaces)

    def log_debug(self, message):
        helper_logger.log_debug(message)

    def log_notice(self, message):
        helper_logger.log_notice(message)

    def log_error(self, message):
        helper_logger.log_error(message)

    def get_asic_id(self, lport):
        return self.port_dict.get(lport, {}).get("asic_id", -1)

    def update_port_transceiver_status_table_sw_cmis_state(self, lport, cmis_state_to_set):
        status_table = self.xcvr_table_helper.get_status_sw_tbl(self.get_asic_id(lport))
        if status_table is None:
            helper_logger.log_error("status_table is None while updating "
                                    "sw CMIS state for lport {}".format(lport))
            return

        fvs = swsscommon.FieldValuePairs([('cmis_state', cmis_state_to_set)])
        status_table.set(lport, fvs)

    def on_port_update_event(self, port_change_event):
        if port_change_event.event_type not in [port_change_event.PORT_SET, port_change_event.PORT_DEL]:
            return

        lport = port_change_event.port_name
        pport = port_change_event.port_index

        if lport in ['PortInitDone']:
            self.isPortInitDone = True
            return

        if lport in ['PortConfigDone']:
            self.isPortConfigDone = True
            return

        # Skip if it's not a physical port
        if not lport.startswith('Ethernet'):
            return

        # Skip if the physical index is not available
        if pport is None:
            return

        if port_change_event.port_dict is None:
            return

        if port_change_event.event_type == port_change_event.PORT_SET:
            if lport not in self.port_dict:
                self.port_dict[lport] = {"asic_id": port_change_event.asic_id,
                                         "forced_tx_disabled": False}
            if pport >= 0:
                self.port_dict[lport]['index'] = pport
            if 'speed' in port_change_event.port_dict and port_change_event.port_dict['speed'] != 'N/A':
                self.port_dict[lport]['speed'] = port_change_event.port_dict['speed']
            if 'lanes' in port_change_event.port_dict:
                self.port_dict[lport]['lanes'] = port_change_event.port_dict['lanes']
            if 'host_tx_ready' in port_change_event.port_dict:
                self.port_dict[lport]['host_tx_ready'] = port_change_event.port_dict['host_tx_ready']
            if 'admin_status' in port_change_event.port_dict:
                self.port_dict[lport]['admin_status'] = port_change_event.port_dict['admin_status']
            if 'laser_freq' in port_change_event.port_dict:
                self.port_dict[lport]['laser_freq'] = int(port_change_event.port_dict['laser_freq'])
            if 'tx_power' in port_change_event.port_dict:
                self.port_dict[lport]['tx_power'] = float(port_change_event.port_dict['tx_power'])
            if 'subport' in port_change_event.port_dict:
                self.port_dict[lport]['subport'] = int(port_change_event.port_dict['subport'])

            self.force_cmis_reinit(lport, 0)

        elif port_change_event.event_type == port_change_event.PORT_DEL:
            # In handling the DEL event, the following two scenarios must be considered:
            # 1. PORT_DEL event due to transceiver plug-out
            # 2. PORT_DEL event due to Dynamic Port Breakout (DPB)
            #
            # Scenario 1 is simple, as only a STATE_DB|TRANSCEIVER_INFO PORT_DEL event occurs,
            # so we just need to set SW_CMIS_STATE to CMIS_STATE_REMOVED.
            #
            # Scenario 2 is a bit more complex. First, for the port(s) before DPB, a CONFIG_DB|PORT PORT_DEL
            # and a STATE_DB|PORT_TABLE PORT_DEL event occur. Next, for the port(s) after DPB,
            # a CONFIG_DB|PORT PORT_SET and a STATE_DB|PORT_TABLE PORT_SET event occur.
            # After that (after a short delay), a STATE_DB|TRANSCEIVER_INFO PORT_DEL event
            # occurs for the port(s) before DPB, and finally, a STATE_DB|TRANSCEIVER_INFO
            # PORT_SET event occurs for the port(s) after DPB.
            #
            # Below is the event sequence when configuring Ethernet0 from "2x200G" to "1x400G"
            # (based on actual logs).
            #
            # 1.  SET Ethernet0 CONFIG_DB|PORT
            # 2.  DEL Ethernet2 CONFIG_DB|PORT
            # 3.  DEL Ethernet0 CONFIG_DB|PORT
            # 4.  DEL Ethernet0 STATE_DB|PORT_TABLE
            # 5.  DEL Ethernet2 STATE_DB|PORT_TABLE
            # 6.  SET Ethernet0 CONFIG_DB|PORT
            # 7.  SET Ethernet0 STATE_DB|PORT_TABLE
            # 8.  SET Ethernet0 STATE_DB|PORT_TABLE
            # 9.  DEL Ethernet2 STATE_DB|TRANSCEIVER_INFO
            # 10. DEL Ethernet0 STATE_DB|TRANSCEIVER_INFO
            # 11. SET Ethernet0 STATE_DB|TRANSCEIVER_INFO
            #
            # To handle both scenarios, if the lport exists in port_dict for any DEL EVENT,
            # set SW_CMIS_STATE to REMOVED. Additionally, for DEL EVENTS from CONFIG_DB due to DPB,
            # remove the lport from port_dict.
            if lport in self.port_dict:
                self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_REMOVED)

            if port_change_event.db_name == 'CONFIG_DB' and port_change_event.table_name == 'PORT':
                self.clear_decomm_pending(lport)
                self.port_dict.pop(lport)

    def get_cmis_dp_init_duration_secs(self, api):
        return api.get_datapath_init_duration()/1000

    def get_cmis_dp_deinit_duration_secs(self, api):
        return api.get_datapath_deinit_duration()/1000

    def get_cmis_dp_tx_turnoff_duration_secs(self, api):
        return api.get_datapath_tx_turnoff_duration()/1000

    def get_cmis_module_power_up_duration_secs(self, api):
        return api.get_module_pwr_up_duration()/1000

    def get_cmis_module_power_down_duration_secs(self, api):
        return api.get_module_pwr_down_duration()/1000

    def get_host_lane_count(self, lport, port_config_lanes, gearbox_lanes_dict):
        """
        Get host lane count from gearbox configuration if available, otherwise from port config

        Args:
            lport: logical port name (e.g., "Ethernet0")
            port_config_lanes: lanes string from port config (e.g., "25,26,27,28")
            gearbox_lanes_dict: dictionary of gearbox line lanes counts

        Returns:
            Integer: number of host lanes
        """
        # First try to get from gearbox configuration
        gearbox_host_lane_count = gearbox_lanes_dict.get(lport, 0)
        if gearbox_host_lane_count > 0:
            self.log_debug("{}: Using gearbox line lanes count: {}".format(lport, gearbox_host_lane_count))
            return gearbox_host_lane_count

        # Fallback to port config lanes
        host_lane_count = len(port_config_lanes.split(','))
        self.log_debug("{}: Using port config lanes count: {}".format(lport, host_lane_count))
        return host_lane_count

    def get_cmis_host_lanes_mask(self, api, appl, host_lane_count, subport):
        """
        Retrieves mask of active host lanes based on appl, host lane count and subport

        Args:
            api:
                XcvrApi object
            appl:
                Integer, the transceiver-specific application code
            host_lane_count:
                Integer, number of lanes on the host side
            subport:
                Integer, 1-based logical port number of the physical port after breakout
                         0 means port is a non-breakout port

        Returns:
            Integer, a mask of the active lanes on the host side
            e.g. 0x3 for lane 0 and lane 1.
        """
        host_lanes_mask = 0

        if appl is None or host_lane_count <= 0 or subport < 0:
            self.log_error("Invalid input to get host lane mask - appl {} host_lane_count {} "
                            "subport {}!".format(appl, host_lane_count, subport))
            return host_lanes_mask

        host_lane_assignment_option = api.get_host_lane_assignment_option(appl)
        host_lane_start_bit = (host_lane_count * (0 if subport == 0 else subport - 1))
        if host_lane_assignment_option & (1 << host_lane_start_bit):
            host_lanes_mask = ((1 << host_lane_count) - 1) << host_lane_start_bit
        else:
            self.log_error("Unable to find starting host lane - host_lane_assignment_option {}"
                            " host_lane_start_bit {} host_lane_count {} subport {} appl {}!".format(
                            host_lane_assignment_option, host_lane_start_bit, host_lane_count,
                            subport, appl))

        return host_lanes_mask

    def get_cmis_media_lanes_mask(self, api, appl, lport, subport):
        """
        Retrieves mask of active media lanes based on appl, lport and subport

        Args:
            api:
                XcvrApi object
            appl:
                Integer, the transceiver-specific application code
            lport:
                String, logical port name
            subport:
                Integer, 1-based logical port number of the physical port after breakout
                         0 means port is a non-breakout port

        Returns:
            Integer, a mask of the active lanes on the media side
            e.g. 0xf for lane 0, lane 1, lane 2 and lane 3.
        """
        media_lanes_mask = 0
        media_lane_count = self.port_dict[lport]['media_lane_count']
        media_lane_assignment_option = self.port_dict[lport]['media_lane_assignment_options']

        if appl < 1 or media_lane_count <= 0 or subport < 0:
            self.log_error("Invalid input to get media lane mask - appl {} media_lane_count {} "
                            "lport {} subport {}!".format(appl, media_lane_count, lport, subport))
            return media_lanes_mask

        media_lane_start_bit = (media_lane_count * (0 if subport == 0 else subport - 1))
        if media_lane_assignment_option & (1 << media_lane_start_bit):
            media_lanes_mask = ((1 << media_lane_count) - 1) << media_lane_start_bit
        else:
            self.log_error("Unable to find starting media lane - media_lane_assignment_option {}"
                            " media_lane_start_bit {} media_lane_count {} lport {} subport {} appl {}!".format(
                            media_lane_assignment_option, media_lane_start_bit, media_lane_count,
                            lport, subport, appl))

        return media_lanes_mask

    def clear_decomm_pending(self, lport):
        """
        Clear the decommission pending status for the entire physical port this logical port belongs to.

        Args:
            lport:
                String, logical port name
        """
        self.decomm_pending_dict.pop(self.port_dict.get(lport, {}).get('index'), None)

    def set_decomm_pending(self, lport):
        """
        Set the decommission pending status.

        Args:
            lport:
                String, logical port name
        """
        physical_port_idx = self.port_dict[lport]['index']
        if physical_port_idx in self.decomm_pending_dict:
            # only one logical port can be the lead logical port doing the
            # decommission state machine.
            return
        self.decomm_pending_dict[physical_port_idx] = lport
        self.log_notice("{}: DECOMMISSION: setting decomm_pending for physical port "
                        "{}".format(lport, physical_port_idx))

    def is_decomm_lead_lport(self, lport):
        """
        Check if this is the lead logical port doing the decommission state machine.

        Args:
            lport:
                String, logical port name
        Returns:
            Boolean, True if decommission pending, False otherwise
        """
        return self.decomm_pending_dict.get(self.port_dict[lport]['index']) == lport

    def is_decomm_pending(self, lport):
        """
        Get the decommission pending status for the physical port the given logical port belongs to.

        Args:
            lport:
                String, logical port name
        Returns:
            Boolean, True if decommission pending, False otherwise
        """
        return self.port_dict[lport]['index'] in self.decomm_pending_dict

    def is_decomm_failed(self, lport):
        """
        Get the decommission failed status for the physical port the given logical port belongs to.

        Args:
            lport:
                String, logical port name
        Returns:
            Boolean, True if decommission failed, False otherwise
        """
        physical_port_idx = self.port_dict[lport]['index']
        lead_logical_port = self.decomm_pending_dict.get(physical_port_idx)
        if lead_logical_port is None:
            return False
        return (
            common.get_cmis_state_from_state_db(
                lead_logical_port,
                self.xcvr_table_helper.get_status_sw_tbl(
                    self.get_asic_id(lead_logical_port)
                )
            )
            == CMIS_STATE_FAILED
        )

    def is_decommission_required(self, api, app_new):
        """
        Check if the CMIS decommission (i.e. reset appl code to 0 for all lanes
        of the entire physical port) is required

        Args:
            api:
                XcvrApi object
            app_new:
                Integer, the new desired appl code
        Returns:
            True, if decommission is required
            False, if decommission is not required
        """
        for lane in range(self.CMIS_MAX_HOST_LANES):
            app_cur = api.get_application(lane)
            if app_cur != 0 and app_cur != app_new:
                return True
        return False

    def is_cmis_application_update_required(self, api, app_new, host_lanes_mask):
        """
        Check if the CMIS application update is required

        Args:
            api:
                XcvrApi object
            app_new:
                Integer, the transceiver-specific application code for the new application
            host_lanes_mask:
                Integer, a bitmask of the lanes on the host side
                e.g. 0x5 for lane 0 and lane 2.

        Returns:
            Boolean, true if application update is required otherwise false
        """
        if api.is_flat_memory() or app_new <= 0 or host_lanes_mask <= 0:
            self.log_error("Invalid input while checking CMIS update required - is_flat_memory {}"
                            "app_new {} host_lanes_mask {}!".format(
                            api.is_flat_memory(), app_new, host_lanes_mask))
            return False

        app_old = 0
        for lane in range(self.CMIS_MAX_HOST_LANES):
            if ((1 << lane) & host_lanes_mask) == 0:
                continue
            if app_old == 0:
                app_old = api.get_application(lane)
            elif app_old != api.get_application(lane):
                self.log_notice("Not all the lanes are in the same application mode "
                                "app_old {} current app {} lane {} host_lanes_mask {}".format(
                                app_old, api.get_application(lane), lane, host_lanes_mask))
                self.log_notice("Forcing application update...")
                return True

        if app_old == app_new:
            skip = True
            dp_state = api.get_datapath_state()
            conf_state = api.get_config_datapath_hostlane_status()
            for lane in range(self.CMIS_MAX_HOST_LANES):
                if ((1 << lane) & host_lanes_mask) == 0:
                    continue
                name = "DP{}State".format(lane + 1)
                if dp_state[name] != 'DataPathActivated':
                    skip = False
                    break
                name = "ConfigStatusLane{}".format(lane + 1)
                if conf_state[name] != 'ConfigSuccess':
                    skip = False
                    break
            return (not skip)
        return True

    def force_cmis_reinit(self, lport, retries=0):
        """
        Try to force the restart of CMIS state machine
        """
        self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_INSERTED)
        self.port_dict[lport]['cmis_retries'] = retries
        self.port_dict[lport]['cmis_expired'] = None # No expiration

    def check_module_state(self, api, states):
        """
        Check if the CMIS module is in the specified state

        Args:
            api:
                XcvrApi object
            states:
                List, a string list of states

        Returns:
            Boolean, true if it's in the specified state, otherwise false
        """
        return api.get_module_state() in states

    def check_config_error(self, api, host_lanes_mask, states):
        """
        Check if the CMIS configuration states are in the specified state

        Args:
            api:
                XcvrApi object
            host_lanes_mask:
                Integer, a bitmask of the lanes on the host side
                e.g. 0x5 for lane 0 and lane 2.
            states:
                List, a string list of states

        Returns:
            Boolean, true if all lanes are in the specified state, otherwise false
        """
        done = True
        cerr = api.get_config_datapath_hostlane_status()
        for lane in range(self.CMIS_MAX_HOST_LANES):
            if ((1 << lane) & host_lanes_mask) == 0:
                continue
            key = "ConfigStatusLane{}".format(lane + 1)
            if cerr[key] not in states:
                done = False
                break

        return done

    def check_datapath_init_pending(self, api, host_lanes_mask):
        """
        Check if the CMIS datapath init is pending

        Args:
            api:
                XcvrApi object
            host_lanes_mask:
                Integer, a bitmask of the lanes on the host side
                e.g. 0x5 for lane 0 and lane 2.

        Returns:
            Boolean, true if all lanes are pending datapath init, otherwise false
        """
        pending = True
        dpinit_pending_dict = api.get_dpinit_pending()
        for lane in range(self.CMIS_MAX_HOST_LANES):
            if ((1 << lane) & host_lanes_mask) == 0:
                continue
            key = "DPInitPending{}".format(lane + 1)
            if not dpinit_pending_dict[key]:
                pending = False
                break

        return pending

    def check_datapath_state(self, api, host_lanes_mask, states):
        """
        Check if the CMIS datapath states are in the specified state

        Args:
            api:
                XcvrApi object
            host_lanes_mask:
                Integer, a bitmask of the lanes on the host side
                e.g. 0x5 for lane 0 and lane 2.
            states:
                List, a string list of states

        Returns:
            Boolean, true if all lanes are in the specified state, otherwise false
        """
        done = True
        dpstate = api.get_datapath_state()
        for lane in range(self.CMIS_MAX_HOST_LANES):
            if ((1 << lane) & host_lanes_mask) == 0:
                continue
            key = "DP{}State".format(lane + 1)
            if dpstate[key] not in states:
                done = False
                break

        return done

    def get_configured_laser_freq_from_db(self, lport):
        """
           Return the Tx power configured by user in CONFIG_DB's PORT table
        """
        port_tbl = self.xcvr_table_helper.get_cfg_port_tbl(self.get_asic_id(lport))

        found, laser_freq = port_tbl.hget(lport, 'laser_freq')
        return int(laser_freq) if found else 0

    def get_configured_tx_power_from_db(self, lport):
        """
           Return the Tx power configured by user in CONFIG_DB's PORT table
        """
        port_tbl = self.xcvr_table_helper.get_cfg_port_tbl(self.get_asic_id(lport))

        found, power = port_tbl.hget(lport, 'tx_power')
        return float(power) if found else 0

    def get_host_tx_status(self, lport):
        state_port_tbl = self.xcvr_table_helper.get_state_port_tbl(self.get_asic_id(lport))

        found, host_tx_ready = state_port_tbl.hget(lport, 'host_tx_ready')
        return host_tx_ready if found else 'false'

    def get_port_admin_status(self, lport):
        cfg_port_tbl = self.xcvr_table_helper.get_cfg_port_tbl(self.get_asic_id(lport))

        found, admin_status = cfg_port_tbl.hget(lport, 'admin_status')
        return admin_status if found else 'down'

    def configure_tx_output_power(self, api, lport, tx_power):
        min_p, max_p = api.get_supported_power_config()
        if tx_power < min_p:
           self.log_error("{} configured tx power {} < minimum power {} supported".format(lport, tx_power, min_p))
        if tx_power > max_p:
           self.log_error("{} configured tx power {} > maximum power {} supported".format(lport, tx_power, max_p))
        return api.set_tx_power(tx_power)

    def validate_frequency_and_grid(self, api, lport, freq, grid=75):
        supported_grid, _,  _, lowf, highf = api.get_supported_freq_config()
        if freq < lowf:
            self.log_error("{} configured freq:{} GHz is lower than the supported freq:{} GHz".format(lport, freq, lowf))
            return False
        if freq > highf:
            self.log_error("{} configured freq:{} GHz is higher than the supported freq:{} GHz".format(lport, freq, highf))
            return False
        if grid == 75:
            if (supported_grid >> 7) & 0x1 != 1:
                self.log_error("{} configured freq:{}GHz supported grid:{} 75GHz is not supported".format(lport, freq, supported_grid))
                return False
            chan = int(round((freq - 193100)/25))
            if chan % 3 != 0:
                self.log_error("{} configured freq:{}GHz is NOT in 75GHz grid".format(lport, freq))
                return False
        elif grid == 100:
            if (supported_grid >> 5) & 0x1 != 1:
                self.log_error("{} configured freq:{}GHz 100GHz is not supported".format(lport, freq))
                return False
        else:
            self.log_error("{} configured freq:{}GHz {}GHz is not supported".format(lport, freq, grid))
            return False
        return True

    def configure_laser_frequency(self, api, lport, freq, grid=75):
        if api.get_tuning_in_progress():
            self.log_error("{} Tuning in progress, subport selection may fail!".format(lport))
        return api.set_laser_freq(freq, grid)

    def post_port_active_apsel_to_db(self, api, lport, host_lanes_mask, reset_apsel=False):
        if reset_apsel == False:
            try:
                act_apsel = api.get_active_apsel_hostlane()
                appl_advt = api.get_application_advertisement()
            except NotImplementedError:
                helper_logger.log_error("Required feature is not implemented")
                return

        tuple_list = []
        for lane in range(self.CMIS_MAX_HOST_LANES):
            if ((1 << lane) & host_lanes_mask) == 0:
                tuple_list.append(('active_apsel_hostlane{}'.format(lane + 1), 'N/A'))
                continue
            if reset_apsel == False:
                act_apsel_lane = act_apsel.get('ActiveAppSelLane{}'.format(lane + 1), 'N/A')
                tuple_list.append(('active_apsel_hostlane{}'.format(lane + 1),
                                   str(act_apsel_lane)))
            else:
                tuple_list.append(('active_apsel_hostlane{}'.format(lane + 1), 'N/A'))

        # also update host_lane_count and media_lane_count
        if len(tuple_list) > 0:
            if reset_apsel == False:
                appl_advt_act = appl_advt.get(act_apsel_lane)
                host_lane_count = appl_advt_act.get('host_lane_count', 'N/A') if appl_advt_act else 'N/A'
                tuple_list.append(('host_lane_count', str(host_lane_count)))
                media_lane_count = appl_advt_act.get('media_lane_count', 'N/A') if appl_advt_act else 'N/A'
                tuple_list.append(('media_lane_count', str(media_lane_count)))
            else:
                tuple_list.append(('host_lane_count', 'N/A'))
                tuple_list.append(('media_lane_count', 'N/A'))

        intf_tbl = self.xcvr_table_helper.get_intf_tbl(self.get_asic_id(lport))
        if not intf_tbl:
            helper_logger.log_warning("Active ApSel db update: TRANSCEIVER_INFO table not found for {}".format(lport))
            return
        found, _ = intf_tbl.get(lport)
        if not found:
            helper_logger.log_warning("Active ApSel db update: {} not found in INTF_TABLE".format(lport))
            return
        fvs = swsscommon.FieldValuePairs(tuple_list)
        intf_tbl.set(lport, fvs)
        self.log_notice("{}: updated TRANSCEIVER_INFO_TABLE {}".format(lport, tuple_list))

    def wait_for_port_config_done(self, namespace):
        # Connect to APPL_DB and subscribe to PORT table notifications
        appl_db = daemon_base.db_connect("APPL_DB", namespace=namespace)

        sel = swsscommon.Select()
        port_tbl = swsscommon.SubscriberStateTable(appl_db, swsscommon.APP_PORT_TABLE_NAME)
        sel.addSelectable(port_tbl)

        # Make sure this daemon started after all port configured
        while not self.task_stopping_event.is_set():
            (state, c) = sel.select(port_event_helper.SELECT_TIMEOUT_MSECS)
            if state == swsscommon.Select.TIMEOUT:
                continue
            if state != swsscommon.Select.OBJECT:
                self.log_warning("sel.select() did not return swsscommon.Select.OBJECT")
                continue

            (key, op, fvp) = port_tbl.pop()
            if key in ["PortConfigDone", "PortInitDone"]:
                break

    def update_cmis_state_expiration_time(self, lport, duration_seconds):
        """
        Set the CMIS expiration time for the given logical port
        in the port dictionary.
        Args:
            lport: Logical port name
            duration_seconds: Duration in seconds for the expiration
        """
        self.port_dict[lport]['cmis_expired'] = datetime.datetime.now() + \
                                                datetime.timedelta(seconds=duration_seconds) + \
                                                datetime.timedelta(milliseconds=self.CMIS_EXPIRATION_BUFFER_MS)

    def is_timer_expired(self, expired_time, current_time=None):
        """
        Check if the given expiration time has passed.

        Args:
            expired_time (datetime): The expiration time to check.
            current_time (datetime, optional): The current time. Defaults to now.

        Returns:
            bool: True if expired_time is not None and has passed, False otherwise.
        """
        if expired_time is None:
            return False

        if current_time is None:
            current_time = datetime.datetime.now()

        return expired_time <= current_time

    def task_worker(self):
        is_fast_reboot = common.is_fast_reboot_enabled()

        # APPL_DB for CONFIG updates, and STATE_DB for insertion/removal
        port_change_observer = PortChangeObserver(self.namespaces, helper_logger,
                                                  self.task_stopping_event,
                                                  self.on_port_update_event)

        while not self.task_stopping_event.is_set():
            # Handle port change event from main thread
            port_change_observer.handle_port_update_event()

            # Cache gearbox line lanes dictionary for this set of iterations over the port_dict
            gearbox_lanes_dict = self.xcvr_table_helper.get_gearbox_line_lanes_dict()

            for lport, info in self.port_dict.items():
                if self.task_stopping_event.is_set():
                    break

                if lport not in self.port_dict:
                    continue

                state = common.get_cmis_state_from_state_db(lport, self.xcvr_table_helper.get_status_sw_tbl(self.get_asic_id(lport)))
                if state in CMIS_TERMINAL_STATES or state == CMIS_STATE_UNKNOWN:
                    if state != CMIS_STATE_READY:
                        self.port_dict[lport]['appl'] = 0
                        self.port_dict[lport]['host_lanes_mask'] = 0
                    continue

                # Handle the case when Xcvrd was NOT running when 'host_tx_ready' or 'admin_status'
                # was updated or this is the first run so reconcile the above two attributes
                if 'host_tx_ready' not in self.port_dict[lport]:
                   self.port_dict[lport]['host_tx_ready'] = self.get_host_tx_status(lport)

                if 'admin_status' not in self.port_dict[lport]:
                   self.port_dict[lport]['admin_status'] = self.get_port_admin_status(lport)

                pport = int(info.get('index', "-1"))
                speed = int(info.get('speed', "0"))
                lanes = info.get('lanes', "").strip()
                subport = info.get('subport', 0)
                if pport < 0 or speed == 0 or len(lanes) < 1 or subport < 0:
                    continue

                # Desired port speed on the host side
                host_speed = speed
                host_lane_count = self.get_host_lane_count(lport, lanes, gearbox_lanes_dict)

                # double-check the HW presence before moving forward
                sfp = self.platform_chassis.get_sfp(pport)
                if not sfp.get_presence():
                    self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_REMOVED)
                    continue

                try:
                    # Skip if XcvrApi is not supported
                    api = sfp.get_xcvr_api()
                    if api is None:
                        self.log_error("{}: skipping CMIS state machine since no xcvr api!!!".format(lport))
                        self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_READY)
                        continue

                    # Skip if it's not a paged memory device
                    if api.is_flat_memory():
                        self.log_notice("{}: skipping CMIS state machine for flat memory xcvr".format(lport))
                        self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_READY)
                        continue

                    # Skip if it's not a CMIS module
                    type = api.get_module_type_abbreviation()
                    if (type is None) or (type not in self.CMIS_MODULE_TYPES):
                        self.log_notice("{}: skipping CMIS state machine for non-CMIS module with type {}".format(lport, type))
                        self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_READY)
                        continue

                    if api.is_coherent_module():
                       if 'tx_power' not in self.port_dict[lport]:
                           self.port_dict[lport]['tx_power'] = self.get_configured_tx_power_from_db(lport)
                       if 'laser_freq' not in self.port_dict[lport]:
                           self.port_dict[lport]['laser_freq'] = self.get_configured_laser_freq_from_db(lport)
                except AttributeError:
                    # Skip if these essential routines are not available
                    self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_READY)
                    continue
                except Exception as e:
                    self.log_error("{}: Exception in xcvr api: {}".format(lport, e))
                    common.log_exception_traceback()
                    self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_FAILED)
                    continue

                # CMIS expiration and retries
                #
                # A retry should always start over at INSETRTED state, while the
                # expiration will reset the state to INSETRTED and advance the
                # retry counter
                expired = self.port_dict[lport].get('cmis_expired')
                retries = self.port_dict[lport].get('cmis_retries', 0)
                host_lanes_mask = self.port_dict[lport].get('host_lanes_mask', 0)
                appl = self.port_dict[lport].get('appl', 0)
                # appl can be 0 if this lport is in decommission state machine, which should not be considered as failed case.
                if state != CMIS_STATE_INSERTED and not self.is_decomm_lead_lport(lport) and (host_lanes_mask <= 0 or appl < 1):
                    self.log_error("{}: Unexpected value for host_lanes_mask {} or appl {} in "
                                    "{} state".format(lport, host_lanes_mask, appl, state))
                    self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_FAILED)
                    continue

                self.log_notice("{}: {}G, lanemask=0x{:x}, CMIS state={}{}, Module state={}, DP state={}, appl {} host_lane_count {} "
                                "retries={}".format(lport, int(speed/1000), host_lanes_mask, state,
                                "(decommission" + ("*" if self.is_decomm_lead_lport(lport) else "") + ")"
                                    if self.is_decomm_pending(lport) else "",
                                api.get_module_state(), api.get_datapath_state(), appl, host_lane_count, retries))
                if retries > self.CMIS_MAX_RETRIES:
                    self.log_error("{}: FAILED".format(lport))
                    self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_FAILED)
                    continue

                try:
                    # CMIS state transitions
                    if state == CMIS_STATE_INSERTED:
                        self.port_dict[lport]['appl'] = common.get_cmis_application_desired(api, host_lane_count, host_speed)
                        if self.port_dict[lport]['appl'] is None:
                            self.log_error("{}: no suitable app for the port appl {} host_lane_count {} "
                                            "host_speed {}".format(lport, appl, host_lane_count, host_speed))
                            self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_FAILED)
                            continue
                        appl = self.port_dict[lport]['appl']
                        self.log_notice("{}: Setting appl={}".format(lport, appl))

                        self.port_dict[lport]['host_lanes_mask'] = self.get_cmis_host_lanes_mask(api,
                                                                        appl, host_lane_count, subport)
                        if self.port_dict[lport]['host_lanes_mask'] <= 0:
                            self.log_error("{}: Invalid lane mask received - host_lane_count {} subport {} "
                                            "appl {}!".format(lport, host_lane_count, subport, appl))
                            self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_FAILED)
                            continue
                        host_lanes_mask = self.port_dict[lport]['host_lanes_mask']
                        self.log_notice("{}: Setting host_lanemask=0x{:x}".format(lport, host_lanes_mask))

                        self.port_dict[lport]['media_lane_count'] = int(api.get_media_lane_count(appl))
                        self.port_dict[lport]['media_lane_assignment_options'] = int(api.get_media_lane_assignment_option(appl))
                        media_lane_count = self.port_dict[lport]['media_lane_count']
                        media_lane_assignment_options = self.port_dict[lport]['media_lane_assignment_options']
                        self.port_dict[lport]['media_lanes_mask'] = self.get_cmis_media_lanes_mask(api,
                                                                        appl, lport, subport)
                        if self.port_dict[lport]['media_lanes_mask'] <= 0:
                            self.log_error("{}: Invalid media lane mask received - media_lane_count {} "
                                            "media_lane_assignment_options {} subport {}"
                                            " appl {}!".format(lport, media_lane_count, media_lane_assignment_options, subport, appl))
                            self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_FAILED)
                            continue
                        media_lanes_mask = self.port_dict[lport]['media_lanes_mask']
                        self.log_notice("{}: Setting media_lanemask=0x{:x}".format(lport, media_lanes_mask))

                        if self.is_decommission_required(api, appl):
                            self.set_decomm_pending(lport)

                        if self.is_decomm_lead_lport(lport):
                            # Set all the DP lanes AppSel to unused(0) when non default app code needs to be configured
                            self.port_dict[lport]['appl'] = appl = 0
                            self.port_dict[lport]['host_lanes_mask'] = host_lanes_mask = self.ALL_LANES_MASK
                            self.port_dict[lport]['media_lanes_mask'] = self.ALL_LANES_MASK
                            self.log_notice("{}: DECOMMISSION: setting appl={} and "
                                            "host_lanes_mask/media_lanes_mask={:#x}".format(lport, appl, self.ALL_LANES_MASK))
                            # Skip rest of the deinit/pre-init when this is the lead logical port for decommission
                            self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_DP_DEINIT)
                            continue
                        elif self.is_decomm_pending(lport):
                            if self.is_decomm_failed(lport):
                                self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_FAILED)
                                decomm_status_str = "failed"
                            else:
                                decomm_status_str = "waiting for completion"
                            self.log_notice("{}: DECOMMISSION: decommission has already started for this physical port, "
                                            "{}".format(lport, decomm_status_str))
                            continue

                        if self.port_dict[lport]['host_tx_ready'] != 'true' or \
                                self.port_dict[lport]['admin_status'] != 'up':
                           if is_fast_reboot and self.check_datapath_state(api, host_lanes_mask, ['DataPathActivated']):
                               self.log_notice("{} Skip datapath re-init in fast-reboot".format(lport))
                           else:
                               self.log_notice("{} Forcing Tx laser OFF".format(lport))
                               # Force DataPath re-init
                               api.tx_disable_channel(media_lanes_mask, True)
                               self.port_dict[lport]['forced_tx_disabled'] = True
                               txoff_duration = self.get_cmis_dp_tx_turnoff_duration_secs(api)
                               self.port_dict[lport]['txoff_duration'] = txoff_duration
                               self.log_notice("{}: Tx turn off duration {} secs".format(lport, txoff_duration))
                               self.post_port_active_apsel_to_db(api, lport, host_lanes_mask, reset_apsel=True)
                           self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_READY)
                           continue

                        # Arm timer for CMIS_STATE_DP_PRE_INIT_CHECK state
                        if self.port_dict[lport].get('forced_tx_disabled', False):
                            txoff_duration = self.port_dict[lport].get('txoff_duration', 0)
                            self.update_cmis_state_expiration_time(lport, txoff_duration)
                        self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_DP_PRE_INIT_CHECK)
                    if state == CMIS_STATE_DP_PRE_INIT_CHECK:
                        if self.port_dict[lport].get('forced_tx_disabled', False):
                            # Ensure that Tx is OFF
                            # Transceiver will remain in DataPathDeactivated state while it is in Low Power Mode (even if Tx is disabled)
                            # Transceiver will enter DataPathInitialized state if Tx was disabled after CMIS initialization was completed
                            if not self.check_datapath_state(api, host_lanes_mask, ['DataPathDeactivated', 'DataPathInitialized']):
                                if self.is_timer_expired(expired):
                                    self.log_notice("{}: timeout for 'DataPathDeactivated/DataPathInitialized'".format(lport))
                                    self.force_cmis_reinit(lport, retries + 1)
                                continue
                            self.port_dict[lport]['forced_tx_disabled'] = False
                            self.log_notice("{}: Tx laser is successfully turned OFF".format(lport))

                        # Configure the target output power if ZR module
                        if api.is_coherent_module():
                           tx_power = self.port_dict[lport]['tx_power']
                           # Prevent configuring same tx power multiple times
                           if 0 != tx_power and tx_power != api.get_tx_config_power():
                              if 1 != self.configure_tx_output_power(api, lport, tx_power):
                                 self.log_error("{} failed to configure Tx power = {}".format(lport, tx_power))
                              else:
                                 self.log_notice("{} Successfully configured Tx power = {}".format(lport, tx_power))

                        need_update = self.is_cmis_application_update_required(api, appl, host_lanes_mask)

                        # For ZR module, Datapath needs to be re-initialized on new channel selection
                        if api.is_coherent_module():
                            freq = self.port_dict[lport]['laser_freq']
                            # If user requested frequency is NOT the same as configured on the module
                            # force datapath re-initialization
                            if 0 != freq and freq != api.get_laser_config_freq():
                                if self.validate_frequency_and_grid(api, lport, freq) == True:
                                    need_update = True
                                else:
                                    # clear setting of invalid frequency config
                                    self.port_dict[lport]['laser_freq'] = 0

                        if not need_update:
                            # No application updates
                            # As part of xcvrd restart, the TRANSCEIVER_INFO table is deleted and
                            # created with default value of 'N/A' for all the active apsel fields.
                            # The below (post_port_active_apsel_to_db) will ensure that the
                            # active apsel fields are updated correctly in the DB since
                            # the CMIS state remains unchanged during xcvrd restart
                            self.post_port_active_apsel_to_db(api, lport, host_lanes_mask)
                            self.log_notice("{}: no CMIS application update required...READY".format(lport))
                            self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_READY)
                            continue
                        self.log_notice("{}: force Datapath reinit".format(lport))
                        self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_DP_DEINIT)
                    elif state == CMIS_STATE_DP_DEINIT:
                        # D.2.2 Software Deinitialization
                        api.set_datapath_deinit(host_lanes_mask)

                        # D.1.3 Software Configuration and Initialization
                        media_lanes_mask = self.port_dict[lport]['media_lanes_mask']
                        if not api.tx_disable_channel(media_lanes_mask, True):
                            self.log_notice("{}: unable to turn off tx power with host_lanes_mask {}".format(lport, host_lanes_mask))
                            self.port_dict[lport]['cmis_retries'] = retries + 1
                            continue

                        #Sets module to high power mode and doesn't impact datapath if module is already in high power mode
                        api.set_lpmode(False, wait_state_change = False)
                        self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_AP_CONF)
                        dpDeinitDuration = self.get_cmis_dp_deinit_duration_secs(api)
                        modulePwrUpDuration = self.get_cmis_module_power_up_duration_secs(api)
                        self.log_notice("{}: DpDeinit duration {} secs, modulePwrUp duration {} secs".format(lport, dpDeinitDuration, modulePwrUpDuration))
                        self.update_cmis_state_expiration_time(lport, max(modulePwrUpDuration, dpDeinitDuration))

                    elif state == CMIS_STATE_AP_CONF:
                        # Explicit control bit to apply custom Host SI settings. 
                        # It will be set to 1 and applied via set_application if 
                        # custom SI settings is applicable
                        ec = 0

                        # TODO: Use fine grained time when the CMIS memory map is available
                        if not self.check_module_state(api, ['ModuleReady']):
                            if self.is_timer_expired(expired):
                                self.log_notice("{}: timeout for 'ModuleReady'".format(lport))
                                self.force_cmis_reinit(lport, retries + 1)
                            continue

                        if not self.check_datapath_state(api, host_lanes_mask, ['DataPathDeactivated']):
                            if self.is_timer_expired(expired):
                                self.log_notice("{}: timeout for 'DataPathDeactivated state'".format(lport))
                                self.force_cmis_reinit(lport, retries + 1)
                            continue

                        # Skip rest if it's in decommission state machine
                        if not self.is_decomm_pending(lport):
                            if api.is_coherent_module():
                                # For ZR module, configure the laser frequency when Datapath is in Deactivated state
                                freq = self.port_dict[lport]['laser_freq']
                                if 0 != freq:
                                    if 1 != self.configure_laser_frequency(api, lport, freq):
                                        self.log_error("{} failed to configure laser frequency {} GHz".format(lport, freq))
                                    else:
                                        self.log_notice("{} configured laser frequency {} GHz".format(lport, freq))

                            # Stage custom SI settings
                            if optics_si_parser.optics_si_present():
                                optics_si_dict = {}
                                # Apply module SI settings if applicable
                                lane_speed = int(speed/1000)//host_lane_count
                                optics_si_dict = optics_si_parser.fetch_optics_si_setting(pport, lane_speed, sfp)

                                self.log_debug("Read SI parameters for port {} from optics_si_settings.json vendor file:".format(lport))
                                for key, sub_dict in optics_si_dict.items():
                                    self.log_debug("{}".format(key))
                                    for sub_key, value in sub_dict.items():
                                        self.log_debug("{}: {}".format(sub_key, str(value)))

                                if optics_si_dict:
                                    self.log_notice("{}: Apply Optics SI found for Vendor: {}  PN: {} lane speed: {}G".
                                                    format(lport, api.get_manufacturer(), api.get_model(), lane_speed))
                                    if not api.stage_custom_si_settings(host_lanes_mask, optics_si_dict):
                                        self.log_notice("{}: unable to stage custom SI settings ".format(lport))
                                        self.force_cmis_reinit(lport, retries + 1)
                                        continue

                                    # Set Explicit control bit to apply Custom Host SI settings
                                    ec = 1

                        # D.1.3 Software Configuration and Initialization
                        api.set_application(host_lanes_mask, appl, ec)
                        if not api.scs_apply_datapath_init(host_lanes_mask):
                            self.log_notice("{}: unable to set application and stage DP init".format(lport))
                            self.force_cmis_reinit(lport, retries + 1)
                            continue

                        self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_DP_INIT)
                    elif state == CMIS_STATE_DP_INIT:
                        if not self.check_config_error(api, host_lanes_mask, ['ConfigSuccess']):
                            if self.is_timer_expired(expired):
                                self.log_notice("{}: timeout for 'ConfigSuccess', current ConfigStatus: "
                                                "{}".format(lport, list(api.get_config_datapath_hostlane_status().values())))
                                self.force_cmis_reinit(lport, retries + 1)
                            continue

                        # Clear decommission status and invoke CMIS reinit so that normal CMIS initialization can begin
                        if self.is_decomm_pending(lport):
                            self.log_notice("{}: DECOMMISSION: done for physical port {}".format(lport, self.port_dict[lport]['index']))
                            self.clear_decomm_pending(lport)
                            self.force_cmis_reinit(lport)
                            continue

                        if hasattr(api, 'get_cmis_rev'):
                            # Check datapath init pending on module that supports CMIS 5.x
                            majorRev = int(api.get_cmis_rev().split('.')[0])
                            if majorRev >= 5 and not self.check_datapath_init_pending(api, host_lanes_mask):
                                self.log_notice("{}: datapath init not pending".format(lport))
                                self.force_cmis_reinit(lport, retries + 1)
                                continue

                        # Ensure the Datapath is NOT Activated unless the host Tx siganl is good.
                        # NOTE: Some CMIS compliant modules may have 'auto-squelch' feature where
                        # the module won't take datapaths to Activated state if host tries to enable
                        # the datapaths while there is no good Tx signal from the host-side.
                        if self.port_dict[lport]['admin_status'] != 'up' or \
                                self.port_dict[lport]['host_tx_ready'] != 'true':
                            self.log_notice("{} waiting for host tx ready...".format(lport))
                            continue

                        # D.1.3 Software Configuration and Initialization
                        api.set_datapath_init(host_lanes_mask)
                        dpInitDuration = self.get_cmis_dp_init_duration_secs(api)
                        self.log_notice("{}: DpInit duration {} secs".format(lport, dpInitDuration))
                        self.update_cmis_state_expiration_time(lport, dpInitDuration)
                        self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_DP_TXON)
                    elif state == CMIS_STATE_DP_TXON:
                        if not self.check_datapath_state(api, host_lanes_mask, ['DataPathInitialized']):
                            if self.is_timer_expired(expired):
                                self.log_notice("{}: timeout for 'DataPathInitialized'".format(lport))
                                self.force_cmis_reinit(lport, retries + 1)
                            continue

                        # Turn ON the laser
                        media_lanes_mask = self.port_dict[lport]['media_lanes_mask']
                        api.tx_disable_channel(media_lanes_mask, False)
                        self.log_notice("{}: Turning ON tx power".format(lport))
                        self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_DP_ACTIVATE)
                    elif state == CMIS_STATE_DP_ACTIVATE:
                        # Use dpInitDuration instead of MaxDurationDPTxTurnOn because
                        # some modules rely on dpInitDuration to turn on the Tx signal.
                        # This behavior deviates from the CMIS spec but is honored
                        # to prevent old modules from breaking with new sonic
                        if not self.check_datapath_state(api, host_lanes_mask, ['DataPathActivated']):
                            if self.is_timer_expired(expired):
                                self.log_notice("{}: timeout for 'DataPathActivated'".format(lport))
                                self.force_cmis_reinit(lport, retries + 1)
                            continue

                        self.log_notice("{}: READY".format(lport))
                        self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_READY)
                        self.post_port_active_apsel_to_db(api, lport, host_lanes_mask)

                except Exception as e:
                    self.log_error("{}: internal errors due to {}".format(lport, e))
                    common.log_exception_traceback()
                    self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_FAILED)

        self.log_notice("Stopped")

    def run(self):
        if self.platform_chassis is None:
            self.log_notice("Platform chassis is not available, stopping...")
            return

        if self.skip_cmis_mgr:
            self.log_notice("Skipping CMIS Task Manager")
            return

        try:

            self.log_notice("Waiting for PortConfigDone...")
            for namespace in self.namespaces:
                self.wait_for_port_config_done(namespace)

            for lport in self.port_dict.keys():
                self.update_port_transceiver_status_table_sw_cmis_state(lport, CMIS_STATE_UNKNOWN)

            self.task_worker()
        except Exception as e:
            helper_logger.log_error("Exception occured at {} thread due to {}".format(threading.current_thread().name, repr(e)))
            common.log_exception_traceback()
            self.exc = e
            self.main_thread_stop_event.set()

    def join(self):
        self.task_stopping_event.set()
        if not self.skip_cmis_mgr:
            threading.Thread.join(self)
            if self.exc:
                raise self.exc

