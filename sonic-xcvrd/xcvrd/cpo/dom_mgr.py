#!/usr/bin/env python3

try:
    from ..dom.dom_mgr import DomInfoUpdateTask
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")


class CpoDomInfoUpdateTask(DomInfoUpdateTask):
    name = "CpoDomInfoUpdateTask"
