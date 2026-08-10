"""Test stub matching sonic_py_common.device_info."""


def get_cpo_data():
    # ChassisBase.__init__ calls this to decide whether to build CPO devices.
    # The real implementation returns None when the platform has no cpo.json,
    # which is the case on a test host.
    return None
