#!/usr/bin/env python3

try:
    from ..xcvrd import SfpStateUpdateTask
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")


class CpoStateUpdateTask(SfpStateUpdateTask):
    def __init__(self, namespaces, port_mapping, port_obj_dict, main_thread_stop_event, sfp_error_event):
        super().__init__(namespaces, port_mapping, port_obj_dict, main_thread_stop_event, sfp_error_event)
        self.name = "CpoStateUpdateTask"
