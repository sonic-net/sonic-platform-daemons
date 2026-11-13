"""A minimal mux simulator, for the real `y_cable_simulated` driver.

sonic-mgmt's mux simulator serves the mux state of each port of a KVM dual
ToR testbed over HTTP; the simulated driver reads and toggles it. This serves
the requests the driver makes.
"""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPPER_TOR = "upper_tor"
LOWER_TOR = "lower_tor"


class MuxSimulator(object):
    def __init__(self, vm_set="bb-vms"):
        self.vm_set = vm_set
        self.ports = {}
        self.requests = []
        self._lock = threading.Lock()
        self._server = None

    def status(self, index):
        with self._lock:
            return dict(self.ports.setdefault(index, {"active_side": UPPER_TOR, "flap_counter": 0,
                                                       "port_index": index}))

    def set_active_side(self, index, side):
        with self._lock:
            port = self.ports.setdefault(index, {"active_side": UPPER_TOR, "flap_counter": 0, "port_index": index})
            if port["active_side"] != side:
                port["flap_counter"] += 1
            port["active_side"] = side

    def start(self):
        sim = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _route(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                with sim._lock:
                    sim.requests.append((method, self.path, body))
                m = re.match(r"^/mux/([^/]+)/(\d+)(/reset)?$", self.path)
                if m and m.group(1) == sim.vm_set:
                    index = int(m.group(2))
                    if method == "POST" and not m.group(3) and "active_side" in body:
                        sim.set_active_side(index, body["active_side"])
                    return self._reply(200, sim.status(index))
                if re.match(r"^/mux/[^/]+/clear_flap_counter$", self.path):
                    return self._reply(200, {})
                return self._reply(404, {"err_msg": "not found"})

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    @property
    def port(self):
        return self._server.server_address[1]

    def config(self, side=UPPER_TOR):
        """Content of /etc/sonic/mux_simulator.json."""
        return {"server_ip": "127.0.0.1", "server_port": self.port, "vm_set": self.vm_set, "side": side}

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
