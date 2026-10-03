"""Minimal device_info surface used by bmcctld unit tests."""


def is_switch_bmc():
    return False


def get_platform_json_data():
    return None


def get_path_to_platform_dir():
    return ""
