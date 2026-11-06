"""Fake Y-cable hardware, served to ycabled's vendor driver and platform API.

ycabled loads the fake vendor driver (`pylib/bbfake/y_cable_bbfake.py`) and the
fake `sonic_platform` package like the real ones. Both forward every call to
this server over a unix socket, so the state of each cable lives in the test
process: tests change it, script failures or delays per method, and read the
log of every call ycabled made. A real cable serves one driver call at a time,
so the server also records a driver call that starts while another call to the
same cable is still running.
"""

import copy
import json
import os
import socketserver
import threading
import time
from collections import namedtuple

TARGET_NIC = 0
TARGET_TOR_A = 1
TARGET_TOR_B = 2

SWITCHING_MODE_MANUAL = 0
SWITCHING_MODE_AUTO = 1

FIRMWARE_DOWNLOAD_STATUS_NOT_INITIATED_OR_FINISHED = 0
FIRMWARE_DOWNLOAD_STATUS_INPROGRESS = 1
FIRMWARE_DOWNLOAD_STATUS_FAILED = 2

# What the fake driver's get_vendor() and the transceiver info return by
# default. The vendor mapping of the overlay maps them to the fake driver.
VENDOR = "BB Vendor"
MODEL = "BB Model"

Call = namedtuple("Call", "ts port op method args")
# A driver call that started while `running` (methods) were still running on
# the same cable. `seq` is the call mark when it started.
Overlap = namedtuple("Overlap", "seq ts port method running")

_UNSET = object()


class Script(object):
    """Scripted behavior of one method of one port."""

    def __init__(self, value=None, raises=None, delay=0.0, func=None, gate=None, times=None, has_value=False):
        self.value = value
        self.raises = raises
        self.delay = delay
        self.func = func
        self.gate = gate
        self.times = times
        self.has_value = has_value


class PortModel(object):
    """One Y-cable as the vendor driver and the platform API see it."""

    def __init__(self, index):
        self.index = index
        self.present = True
        self.transceiver_info = {"manufacturer": VENDOR, "model": MODEL, "type": "QSFP28 or later",
                                 "serial": "BB{:04d}".format(index)}
        self.vendor = VENDOR
        self.initialized = True
        self.read_side = TARGET_TOR_A
        self.mux_direction = TARGET_TOR_A
        self.active_linked_side = None
        self.switch_count = {"manual": 3, "auto": 5}
        self.eye_heights = {TARGET_NIC: [601, 602], TARGET_TOR_A: [611, 612], TARGET_TOR_B: [621, 622]}
        self.link_active = {TARGET_NIC: True, TARGET_TOR_A: True, TARGET_TOR_B: False}
        self.firmware = {
            TARGET_NIC: {"version_active": "1.0.0", "version_inactive": "0.9.0", "version_next": "1.0.0"},
            TARGET_TOR_A: {"version_active": "1.1.0", "version_inactive": "1.0.9", "version_next": "1.1.0"},
            TARGET_TOR_B: {"version_active": "1.2.0", "version_inactive": "1.1.9", "version_next": "1.2.0"},
        }
        self.local_temperature = 35
        self.local_voltage = 3.3
        self.nic_temperature = 45
        self.nic_voltage = 5.1
        self.cursor_values = {}
        for lane in (1, 2):
            for target in (TARGET_NIC, TARGET_TOR_A, TARGET_TOR_B):
                base = lane * 100 + target * 10
                self.cursor_values[(lane, target)] = [base + 1, base + 2, base + 3, base + 4, base + 5]
        self.switching_mode = SWITCHING_MODE_AUTO
        self.fec_mode = {TARGET_NIC: 1, TARGET_TOR_A: 1, TARGET_TOR_B: 2}
        self.anlt = {TARGET_NIC: False, TARGET_TOR_A: True, TARGET_TOR_B: False}
        self.speed = 100000
        self.event_log = ["evt-0 link down", "evt-1 link up", "evt-2 switch"]
        self.ber_info = {t: [1e-9 * (t + 1), 2e-9 * (t + 1)] for t in (TARGET_NIC, TARGET_TOR_A, TARGET_TOR_B)}
        self.fec_stats = {t: {"corrected": 10 + t, "uncorrected": t} for t in (TARGET_NIC, TARGET_TOR_A, TARGET_TOR_B)}
        self.pcs_stats = {t: {"errors": 20 + t} for t in (TARGET_NIC, TARGET_TOR_A, TARGET_TOR_B)}
        self.alive_status = True
        self.health = True
        self.reset_cause_value = 2
        self.operation_time_value = 123456
        self.debug_dump = {"reg_0x10": "0x5a", "reg_0x11": "0xa5"}
        self.queue = {"queue_0": 7, "queue_1": 0}
        self.download_firmware_status = FIRMWARE_DOWNLOAD_STATUS_NOT_INITIATED_OR_FINISHED
        self.prbs = {}
        self.loopback = {}
        self.scripts = {}

    # Scripting --------------------------------------------------------------

    def script(self, method, value=_UNSET, raises=None, delay=0.0, func=None, times=None, block=False):
        """Override `method`: return `value`, raise `raises` (an exception class
        name, or "Type: message"), sleep `delay` first, or compute the result
        with `func(model, args)`. `block` holds the call until the returned
        event is set. `times` limits how many calls the script applies to."""
        gate = threading.Event() if block else None
        self.scripts[method] = Script(None if value is _UNSET else value, raises, delay, func, gate, times,
                                      value is not _UNSET)
        return gate

    def unscript(self, method):
        self.scripts.pop(method, None)

    # Default behavior -------------------------------------------------------

    def _active_linked(self):
        return self.mux_direction if self.active_linked_side is None else self.active_linked_side

    def run(self, op, method, args):
        if op == "platform":
            if method == "get_presence":
                return self.present
            if method == "get_transceiver_info":
                return copy.deepcopy(self.transceiver_info) if self.present else None
            raise AttributeError(method)
        if op == "attr":
            return getattr(self, method)
        if op == "init":
            return {"initialized": self.initialized}
        impl = getattr(self, "m_" + method, None)
        if impl is None:
            raise NotImplementedError(method)
        return impl(*args)

    def m_get_vendor(self):
        return self.vendor

    def m_get_read_side(self):
        return self.read_side

    def m_get_mux_direction(self):
        return self.mux_direction

    def m_get_active_linked_tor_side(self):
        return self._active_linked()

    def _toggle(self, side):
        self.mux_direction = side
        self.active_linked_side = None
        self.switch_count["manual"] += 1
        return True

    def m_toggle_mux_to_tor_a(self):
        return self._toggle(TARGET_TOR_A)

    def m_toggle_mux_to_tor_b(self):
        return self._toggle(TARGET_TOR_B)

    def m_get_switch_count_total(self, switch_count_type, clear_on_read=False):
        return self.switch_count[switch_count_type]

    def m_get_eye_heights(self, target):
        return list(self.eye_heights[target])

    def m_is_link_active(self, target):
        return self.link_active[target]

    def m_get_firmware_version(self, target):
        return dict(self.firmware[target])

    def m_get_local_temperature(self):
        return self.local_temperature

    def m_get_local_voltage(self):
        return self.local_voltage

    def m_get_nic_temperature(self):
        return self.nic_temperature

    def m_get_nic_voltage(self):
        return self.nic_voltage

    def m_get_target_cursor_values(self, lane, target):
        return list(self.cursor_values[(lane, target)])

    def m_get_switching_mode(self):
        return self.switching_mode

    def m_set_switching_mode(self, mode):
        self.switching_mode = mode
        return True

    def m_enable_prbs_mode(self, target, mode_value, lane_mask, direction=0):
        self.prbs[target] = {"mode_value": mode_value, "lane_mask": lane_mask, "direction": direction}
        return True

    def m_disable_prbs_mode(self, target, direction=0):
        self.prbs.pop(target, None)
        return True

    def m_enable_loopback_mode(self, target, mode_value, lane_mask):
        self.loopback[target] = {"mode_value": mode_value, "lane_mask": lane_mask}
        return True

    def m_disable_loopback_mode(self, target):
        self.loopback.pop(target, None)
        return True

    def m_reset(self, target):
        return True

    def m_set_anlt(self, enable, target):
        self.anlt[target] = bool(enable)
        return True

    def m_get_anlt(self, target):
        return self.anlt[target]

    def m_set_fec_mode(self, fec_mode, target):
        self.fec_mode[target] = fec_mode
        return True

    def m_get_fec_mode(self, target):
        return self.fec_mode[target]

    def m_get_speed(self):
        return self.speed

    def m_get_event_log(self, clear_on_read=False):
        return list(self.event_log)

    def m_get_ber_info(self, target):
        return list(self.ber_info[target])

    def m_get_fec_stats(self, target):
        return dict(self.fec_stats[target])

    def m_get_pcs_stats(self, target):
        return dict(self.pcs_stats[target])

    def m_get_alive_status(self):
        return self.alive_status

    def m_health_check(self):
        return self.health

    def m_reset_cause(self):
        return self.reset_cause_value

    def m_operation_time(self):
        return self.operation_time_value

    def m_debug_dump_registers(self, option=None):
        return dict(self.debug_dump)

    def m_queue_info(self):
        return dict(self.queue)

    def m_download_firmware(self, fwfile):
        self.download_firmware_status = FIRMWARE_DOWNLOAD_STATUS_NOT_INITIATED_OR_FINISHED
        return 0

    def m_activate_firmware(self, fwfile=None, hitless=False):
        return 0

    def m_rollback_firmware(self, fwfile=None):
        return 0


class FakeHardware(object):
    def __init__(self, socket_path):
        self.socket_path = socket_path
        self._ports = {}
        self._calls = []
        self._running = {}
        self._overlaps = []
        self._lock = threading.RLock()
        self._server = None
        self.platform_init_error = None

    # Server -----------------------------------------------------------------

    def start(self):
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        hw = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for line in self.rfile:
                    if not line.strip():
                        continue
                    reply = hw._handle(json.loads(line.decode()))
                    self.wfile.write((json.dumps(reply) + "\n").encode())
                    self.wfile.flush()

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        self._server = Server(self.socket_path, Handler)
        threading.Thread(target=self._server.serve_forever, name="fake-hw", daemon=True).start()

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    def _handle(self, req):
        op, port, method = req["op"], req.get("port"), req.get("method")
        if op != "driver":
            return self._serve(req)
        with self._lock:
            running = self._running.setdefault(port, [])
            if running:
                self._overlaps.append(Overlap(len(self._calls), time.time(), port, method,
                                              tuple(m for _, m in running)))
            entry = (object(), method)
            running.append(entry)
        try:
            return self._serve(req)
        finally:
            with self._lock:
                # The list this call joined: a call that ends after reset() must
                # not touch the next test's calls.
                if entry in running:
                    running.remove(entry)

    def _serve(self, req):
        op, port, method, args = req["op"], req.get("port"), req.get("method"), req.get("args") or []
        with self._lock:
            self._calls.append(Call(time.time(), port, op, method, tuple(args)))
            if op == "platform_init":
                script = Script(raises=self.platform_init_error) if self.platform_init_error else None
                model = None
            else:
                model = self._ports.get(port)
                if model is None:
                    if op == "platform":
                        # A port the platform has no transceiver for.
                        return {"value": False if method == "get_presence" else None}
                    return {"raise": "RuntimeError", "msg": "no fake hardware for physical port {}".format(port)}
                script = model.scripts.get(method)
                if script is not None and script.times is not None:
                    script.times -= 1
                    if script.times < 0:
                        model.scripts.pop(method, None)
                        script = None
                if script is not None and script.gate is not None and method == "download_firmware":
                    # Together with the call record: a test that sees the call
                    # sees the download in progress.
                    model.download_firmware_status = FIRMWARE_DOWNLOAD_STATUS_INPROGRESS
        try:
            if script is not None:
                if script.delay:
                    time.sleep(script.delay)
                if script.gate is not None:
                    script.gate.wait(120)
                if script.raises:
                    name, _, msg = script.raises.partition(":")
                    return {"raise": name.strip(), "msg": msg.strip() or "scripted failure"}
                if script.func is not None:
                    with self._lock:
                        return {"value": script.func(model, args)}
                if script.has_value:
                    return {"value": script.value}
            if op == "platform_init":
                return {"value": None}
            with self._lock:
                return {"value": model.run(op, method, args)}
        except Exception as e:
            return {"raise": type(e).__name__, "msg": str(e)}

    # Test API ---------------------------------------------------------------

    def add_port(self, index):
        with self._lock:
            model = PortModel(index)
            self._ports[index] = model
            return model

    def port(self, index):
        return self._ports[index]

    def reset(self):
        with self._lock:
            for model in self._ports.values():
                for script in model.scripts.values():
                    if script.gate is not None:
                        script.gate.set()
            self._ports = {}
            self._calls = []
            self._running = {}
            self._overlaps = []
            self.platform_init_error = None

    def mark(self):
        return len(self._calls)

    def calls(self, since=0, port=None, method=None, op="driver"):
        with self._lock:
            return [c for c in self._calls[since:]
                    if (port is None or c.port == port) and (method is None or c.method == method)
                    and (op is None or c.op == op)]

    def overlaps(self, since=0, port=None):
        """Driver calls that started, after call mark `since`, while another
        call to the same cable was still running."""
        with self._lock:
            return [o for o in self._overlaps if o.seq >= since and (port is None or o.port == port)]
