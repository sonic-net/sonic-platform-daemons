#!/usr/bin/env python3

try:
    from ..cmis.cmis_manager_task import CmisManagerTask
    from ..xcvrd_utilities import common
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")


class CpoManagerTask(CmisManagerTask):
    def __init__(self, namespaces, port_mapping, port_obj_dict, main_thread_stop_event, skip_cpo_mgr=False):
        super().__init__(namespaces, port_mapping, port_obj_dict, main_thread_stop_event,
                         skip_cmis_mgr=skip_cpo_mgr)
        self.name = "CpoManagerTask"

    def log_debug(self, message):
        super().log_debug("CPO: {}".format(message))

    def log_notice(self, message):
        super().log_notice("CPO: {}".format(message))

    def log_error(self, message):
        super().log_error("CPO: {}".format(message))

    def deinit_oe_sibling_pports(self, lport):
        """
        Args:
            lport: Logical port name

        Returns:
            Boolean: True if all sibling physical ports were deinitialized,
                     False otherwise
        """
        pport = self.port_dict.get(lport, {}).get('index')
        if pport is None:
            self.log_error("{}: unable to determine physical port to fetch optical "
                           "engine siblings".format(lport))
            return False

        done = True
        for sibling_pport in common.get_oe_sibling_pports(pport):
            if sibling_pport == pport:
                continue

            sibling = self.port_obj_dict.get(sibling_pport)
            if sibling is None:
                self.log_error("{}: no CPO object available for sibling physical port "
                               "{}".format(lport, sibling_pport))
                done = False
                continue

            try:
                sibling_api = sibling.get_xcvr_api()
                if sibling_api is None:
                    self.log_error("{}: no xcvr api available for sibling physical port "
                                   "{}".format(lport, sibling_pport))
                    done = False
                    continue

                lanes_mask = self.get_cmis_max_host_lanes_mask(sibling_api)
                self.log_notice("{}: set datapath deinit and disable Tx output for all lanes "
                                "of sibling physical port {}".format(lport, sibling_pport))
                sibling_api.set_datapath_deinit(lanes_mask)
                if not sibling_api.tx_disable_channel(lanes_mask, True):
                    self.log_error("{}: unable to turn off tx power of sibling physical port "
                                   "{} with lanes_mask {:#x}".format(lport, sibling_pport, lanes_mask))
                    done = False
            except Exception as e:
                self.log_error("{}: failed to deinitialize sibling physical port {} due to "
                               "{}".format(lport, sibling_pport, e))
                common.log_exception_traceback()
                done = False

        return done

    def handle_cmis_dp_deinit_state(self, lport):
        """
        Handle the CMIS_STATE_DP_DEINIT state for a logical port on CPO hardware
        by deinitializing the datapaths and disabling the Tx output of all lanes
        of the physical ports sharing an optical engine with lport.

        The physical port lport itself belongs to is skipped, since its lanes are
        handled by the CmisManagerTask logic for the current logical interface.

        Args:
            lport: Logical port name

        Returns:
            Boolean: True if state machine should continue to next state,
                     False if processing should stop (return from caller)
        """
        api = self.port_dict[lport].get('api')
        if self.check_module_state(api, ['ModuleLowPwr']):
            self.log_notice("{}: ModuleLowPwr detected, deinitializing all lanes of the "
                            "optical engine".format(lport))
            if not self.deinit_oe_sibling_pports(lport):
                self.port_dict[lport]['cmis_retries'] = self.port_dict[lport].get('cmis_retries', 0) + 1
                return False

        # Run the existing logic that handles the physical port associated
        # with the current logical interface
        return super().handle_cmis_dp_deinit_state(lport)
