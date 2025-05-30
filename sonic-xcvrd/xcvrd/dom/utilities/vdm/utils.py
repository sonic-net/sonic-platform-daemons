from contextlib import contextmanager
import time

MAX_tVDMF_TIME_MSECS = 10
MAX_VDM_FREEZE_UNFREEZE_TIME_MSECS = 1000
FREEZE_UNFREEZE_DONE_POLLING_INTERVAL_MSECS = 1

class VDMUtils:
    """
    This class provides utility functions for managing VDM operations on transceivers
    and call the corresponding methods in the SFP object.
    """
    def __init__(self, sfp_obj_dict, logger):
        self.sfp_obj_dict = sfp_obj_dict
        self.logger = logger

    def is_transceiver_vdm_supported(self, physical_port):
        try:
            return self.sfp_obj_dict[physical_port].is_transceiver_vdm_supported()
        except (NotImplementedError):
            return False

    def get_vdm_real_values(self, physical_port):
        try:
            return self.sfp_obj_dict[physical_port].get_transceiver_vdm_real_value()
        except (NotImplementedError):
            self.logger.log_error(f"Failed to get VDM real values for port {physical_port}")
            return {}

    def get_vdm_flags(self, physical_port):
        try:
            return self.sfp_obj_dict[physical_port].get_transceiver_vdm_flags()
        except (NotImplementedError):
            self.logger.log_error(f"Failed to get VDM flags for port {physical_port}")
            return {}

    def get_vdm_thresholds(self, physical_port):
        try:
            return self.sfp_obj_dict[physical_port].get_transceiver_vdm_thresholds()
        except (NotImplementedError):
            return {}

    @contextmanager
    def vdm_freeze_context(self, physical_port):
        try:
            if not self._freeze_vdm_stats_and_confirm(physical_port):
                self.logger.log_error(f"Failed to freeze VDM stats in contextmanager for port {physical_port}")
                yield False
            else:
                yield True
        finally:
            if not self._unfreeze_vdm_stats_and_confirm(physical_port):
                self.logger.log_error(f"Failed to unfreeze VDM stats in contextmanager for port {physical_port}")

    def _vdm_action_and_confirm(self, physical_port, action, status_check, action_name):
        """
        Helper function to perform VDM action (freeze/unfreeze) and confirm the status.
        Args:
            physical_port: The physical port index.
            action: The action to perform (freeze/unfreeze).
            status_check: The function to check the status.
            action_name: The name of the action for logging purposes.
        Returns:
            True if the action is successful, False otherwise.
        """
        try:
            status = action()
            if not status:
                self.logger.log_error(f"Failed to {action_name} VDM stats for port {physical_port}")
                return False

            # Wait for MAX_tVDMF_TIME_MSECS to allow the module to clear the done bit
            time.sleep(MAX_tVDMF_TIME_MSECS / 1000)

            # Poll for the done bit to be set
            start_time = time.time()
            while time.time() - start_time < MAX_VDM_FREEZE_UNFREEZE_TIME_MSECS / 1000:
                if status_check():
                    return True
                time.sleep(FREEZE_UNFREEZE_DONE_POLLING_INTERVAL_MSECS / 1000)

            self.logger.log_error(f"Failed to confirm VDM {action_name} status for port {physical_port}")
        except (KeyError, NotImplementedError) as e:
            # Handle the case where the SFP object does not exist or the method is not implemented
            self.logger.log_error(f"VDM {action_name} failed for port {physical_port} with exception {e}")
            return False

        return False

    def _freeze_vdm_stats_and_confirm(self, physical_port):
        """
        Freezes and confirms the VDM freeze status of the transceiver.
        Args:
            physical_port: The physical port index.
        Returns:
            True if the VDM stats are frozen successfully, False otherwise.
        """
        sfp = self.sfp_obj_dict.get(physical_port)
        if not sfp:
            self.logger.log_error(f"Freeze VDM stats failed: {physical_port} not found in sfp_obj_dict")
            return False

        return self._vdm_action_and_confirm(physical_port, sfp.freeze_vdm_stats,
                                            sfp.get_vdm_freeze_status, "freeze")

    def _unfreeze_vdm_stats_and_confirm(self, physical_port):
        """
        Unfreezes and confirms the VDM unfreeze status of the transceiver.
        Args:
            physical_port: The physical port index.
        Returns:
            True if the VDM stats are unfrozen successfully, False otherwise.
        """
        sfp = self.sfp_obj_dict.get(physical_port)
        if not sfp:
            self.logger.log_error(f"Unfreeze VDM stats failed: {physical_port} not found in sfp_obj_dict")
            return False

        return self._vdm_action_and_confirm(physical_port, sfp.unfreeze_vdm_stats,
                                            sfp.get_vdm_unfreeze_status, "unfreeze")
