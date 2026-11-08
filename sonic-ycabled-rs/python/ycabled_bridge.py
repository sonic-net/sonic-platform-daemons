"""Bridge between ycabled-rs and the Python platform API and Y-cable vendor
drivers.

ycabled-rs compiles this module in and runs it in its embedded interpreter.
The module owns every Python object (the chassis and one driver per physical
port); ycabled-rs calls the functions below with plain values, so each
operation crosses the language boundary once. Driver selection is Python
ycabled's: the transceiver's vendor and model, normalized, looked up in
sonic_y_cable's vendor mapping, with the mux simulator override on test
systems, then checked with get_vendor().
"""

import os
import re
from importlib import import_module

MUX_SIMULATOR_CONFIG_FILE = "/etc/sonic/mux_simulator.json"

# Set by init(): a function(level, message) that logs through ycabled-rs.
_log = None
_chassis = None
_is_vs = False
_drivers = {}

ERROR, WARNING, NOTICE, INFO, DEBUG = 3, 4, 5, 6, 7


class Logger(object):
    """The logger the drivers get, in place of sonic_py_common's: it logs
    through ycabled-rs, with its syslog identity."""

    def log_error(self, msg, also_print_to_console=False):
        _log(ERROR, str(msg))

    def log_warning(self, msg, also_print_to_console=False):
        _log(WARNING, str(msg))

    def log_notice(self, msg, also_print_to_console=False):
        _log(NOTICE, str(msg))

    def log_info(self, msg, also_print_to_console=False):
        _log(INFO, str(msg))

    def log_debug(self, msg, also_print_to_console=False):
        _log(DEBUG, str(msg))

    def log(self, priority, msg, also_print_to_console=False):
        _log(int(priority), str(msg))

    # ycabled-rs controls the level (XCVRD_LOG).
    def set_min_log_priority(self, priority):
        pass

    def set_min_log_priority_debug(self):
        pass

    def set_min_log_priority_notice(self):
        pass

    def set_min_log_priority_info(self):
        pass


LOGGER = Logger()


def init(is_vs, log):
    """Loads the platform API, except on VS, which has none."""
    global _log, _chassis, _is_vs
    _log = log
    _is_vs = bool(is_vs)
    if not _is_vs:
        import sonic_platform.platform
        _chassis = sonic_platform.platform.Platform().get_chassis()
        _log(INFO, "chassis loaded {}".format(_chassis))


def format_mapping_identifier(string):
    if not isinstance(string, str):
        _log(WARNING, "Error: mapping identifier is not a string {}".format(string))
        return None
    s = string.lower().strip()
    return re.sub(r'\s+', '_', s)


def presence(physical_port):
    if _chassis is not None:
        try:
            return bool(_chassis.get_sfp(physical_port).get_presence())
        except NotImplementedError:
            pass
    return _is_vs


def transceiver_info(physical_port):
    res = None
    if _chassis is not None:
        try:
            res = _chassis.get_sfp(physical_port).get_transceiver_info()
        except NotImplementedError:
            res = None
    elif _is_vs:
        res = {}
    if res is not None and os.path.exists(MUX_SIMULATOR_CONFIG_FILE):
        res["manufacturer"] = "microsoft"
        res["model"] = "simulated"
    return res


def load(port, physical_port):
    """Loads the driver of the cable on `physical_port`. Returns
    (True, initialized) when a driver is in place, or (False, None) after
    logging why there is none."""
    _drivers.pop(physical_port, None)
    info = transceiver_info(physical_port)
    if info is None:
        _log(WARNING, "Error: Could not get transceiver info dict Y cable port {} while inserting entries".format(port))
        return (False, None)
    vendor = info.get('manufacturer')
    if vendor is None:
        _log(WARNING, "Error: Unable to find Vendor name for Transceiver for Y-Cable initiation {}".format(port))
        return (False, None)
    model = info.get('model')
    if model is None:
        _log(WARNING, "Error: Unable to find model name for Transceiver for Y-Cable initiation {}".format(port))
        return (False, None)
    vendor = format_mapping_identifier(vendor)
    model = format_mapping_identifier(model)
    from sonic_y_cable import y_cable_vendor_mapping
    module_dir = y_cable_vendor_mapping.mapping.get(vendor)
    if module_dir is None:
        _log(WARNING, "Error: Unable to find module dir name from vendor for Y-Cable initiation {}".format(port))
        return (False, None)
    module = module_dir.get(model)
    if module is None:
        _log(WARNING, "Error: Unable to find module name from model for Y-Cable initiation {}".format(port))
        return (False, None)
    try:
        # The module name comes from sonic_y_cable's own vendor mapping.
        # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
        cls = getattr(import_module('sonic_y_cable.' + module), 'YCable')
    except Exception as e:
        _log(WARNING, "Failed to load the attr due to {}".format(repr(e)))
        return (False, None)
    if cls is None:
        _log(WARNING, "Error: Unable to import attr name for Y-Cable initiation {}".format(port))
        return (False, None)
    try:
        driver = cls(physical_port, LOGGER)
    except Exception as e:
        _log(WARNING, "Failed to initialize the Y-cable driver for port {} due to {}".format(physical_port, repr(e)))
        return (False, None)
    try:
        vendor_name_api = driver.get_vendor()
    except Exception as e:
        _log(WARNING, "Failed to call the get_vendor API for port {} due to {}".format(physical_port, repr(e)))
        return (False, None)
    if format_mapping_identifier(vendor_name_api) != vendor:
        _log(WARNING, "Error: Y Cable api does not work for {}, {} actual vendor name {}".format(
            port, vendor_name_api, vendor))
        return (False, None)
    _drivers[physical_port] = driver
    return (True, bool(getattr(driver, '_initialized', True)))


def unload(physical_port):
    _drivers.pop(physical_port, None)


def has_driver(physical_port):
    return physical_port in _drivers


def call(physical_port, method, args):
    """Calls a driver method. Raises KeyError without a driver."""
    return getattr(_drivers[physical_port], method)(*args)


def attr(physical_port, name):
    return getattr(_drivers[physical_port], name)
