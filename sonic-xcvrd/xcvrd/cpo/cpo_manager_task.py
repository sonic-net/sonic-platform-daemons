#!/usr/bin/env python3

try:
    from ..cmis.cmis_manager_task import CmisManagerTask
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")


class CpoManagerTask(CmisManagerTask):
    def __init__(self, namespaces, port_mapping, sfp_obj_dict, main_thread_stop_event, skip_cpo_mgr=False):
        super().__init__(namespaces, port_mapping, sfp_obj_dict, main_thread_stop_event,
                         skip_cmis_mgr=skip_cpo_mgr)
        self.name = "CpoManagerTask"

    def log_debug(self, message):
        super().log_debug("CPO: {}".format(message))

    def log_notice(self, message):
        super().log_notice("CPO: {}".format(message))

    def log_error(self, message):
        super().log_error("CPO: {}".format(message))
