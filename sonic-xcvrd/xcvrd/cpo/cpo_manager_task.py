#!/usr/bin/env python3

try:
    from ..cmis.cmis_manager_task import CmisManagerTask
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")


class CpoManagerTask(CmisManagerTask):
    def __init__(self, namespaces, port_mapping, port_obj_dict, main_thread_stop_event, skip_cpo_mgr=False):
        super().__init__(namespaces, port_mapping, port_obj_dict, main_thread_stop_event,
                         skip_cmis_mgr=skip_cpo_mgr)
        self.name = "CpoManagerTask"
