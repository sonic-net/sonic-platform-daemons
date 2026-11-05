"""A scriptable stand-in for the NIC SoC's gRPC services.

One `Soc` serves DualToRActive and GracefulRestart on <ip>:50075, as the SoC
of one active-active port does. Tests set the forwarding, link and operation
states, script per-RPC delays, error codes or malformed replies, and read
the log of every RPC ycabled made.
"""

import socket
import threading
import time
from collections import namedtuple
from concurrent import futures

import grpc

GRPC_PORT = 50075

Rpc = namedtuple("Rpc", "ts name request")

# ycabled pings every 4 s, also without calls. A real SoC allows that; grpc's
# default server policy would answer with GOAWAY (too_many_pings).
SERVER_OPTIONS = [
    ("grpc.keepalive_permit_without_calls", 1),
    ("grpc.http2.min_recv_ping_interval_without_data_ms", 1000),
    ("grpc.http2.min_ping_interval_without_data_ms", 1000),
    ("grpc.http2.max_ping_strikes", 0),
]


def _pb():
    from proto_out import linkmgr_grpc_driver_pb2 as pb2
    from proto_out import linkmgr_grpc_driver_pb2_grpc as pb2_grpc
    return pb2, pb2_grpc


def _to_dict(msg):
    out = {}
    for field in msg.DESCRIPTOR.fields:
        value = getattr(msg, field.name)
        repeated = getattr(field, "is_repeated", None)
        if repeated is None:
            repeated = field.label == field.LABEL_REPEATED
        out[field.name] = list(value) if repeated else value
    return out


class RpcScript(object):
    def __init__(self, delay=0.0, code=None, reply=None, times=None):
        self.delay = delay
        self.code = code
        self.reply = reply
        self.times = times


def format_target(ip):
    return "[{}]:{}".format(ip, GRPC_PORT) if ":" in ip else "{}:{}".format(ip, GRPC_PORT)


class Soc(object):
    def __init__(self, ip, tls=None):
        """`tls`: None for plaintext, or dict(cert=, key=, ca=, require_client_auth=)
        with PEM bytes."""
        self.ip = ip
        self.tls = tls
        # Index 0 is the upper ToR's port, index 1 the lower ToR's.
        self.admin = [True, False]
        self.link = [True, True]
        self.oper = [True, True]
        self.version = "bb-soc-1.0"
        # GracefulRestart: responses sent on each stream, then the stream is
        # held open (gr_hold) or ended.
        self.gr_responses = []
        self.gr_hold = True
        self._scripts = {}
        self._rpcs = []
        self._lock = threading.Lock()
        self._server = None
        self._release = threading.Event()

    # Lifecycle --------------------------------------------------------------

    def start(self):
        pb2, pb2_grpc = _pb()
        self._release.clear()
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=32), options=SERVER_OPTIONS)
        pb2_grpc.add_DualToRActiveServicer_to_server(_DualToRActive(self, pb2), server)
        pb2_grpc.add_GracefulRestartServicer_to_server(_GracefulRestart(self, pb2), server)
        target = format_target(self.ip)
        if self.tls:
            creds = grpc.ssl_server_credentials(
                [(self.tls["key"], self.tls["cert"])],
                root_certificates=self.tls.get("ca") if self.tls.get("require_client_auth") else None,
                require_client_auth=bool(self.tls.get("require_client_auth")))
            bound = server.add_secure_port(target, creds)
        else:
            bound = server.add_insecure_port(target)
        if not bound:
            raise RuntimeError("SoC double could not listen on {}".format(target))
        server.start()
        self._server = server
        return self

    def stop(self):
        self._release.set()
        if self._server is not None:
            self._server.stop(grace=None).wait(5)
            self._server = None

    @property
    def running(self):
        return self._server is not None

    # Scripting --------------------------------------------------------------

    def script(self, rpc, delay=0.0, code=None, reply=None, times=None):
        """Make `rpc` sleep `delay` seconds first, fail with grpc.StatusCode
        `code`, or answer `reply(request, pb2)` instead of the default."""
        self._scripts[rpc] = RpcScript(delay, code, reply, times)

    def unscript(self, rpc=None):
        if rpc is None:
            self._scripts.clear()
        else:
            self._scripts.pop(rpc, None)

    # Log --------------------------------------------------------------------

    def mark(self):
        return len(self._rpcs)

    def rpcs(self, name=None, since=0):
        with self._lock:
            return [r for r in self._rpcs[since:] if name is None or r.name == name]

    def wait_rpcs(self, name, count=1, since=0, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            got = self.rpcs(name, since)
            if len(got) >= count:
                return got
            time.sleep(0.02)
        raise AssertionError("SoC {} got {} {} RPCs, expected {} within {}s".format(
            self.ip, len(self.rpcs(name, since)), name, count, timeout))

    # Serving ----------------------------------------------------------------

    def _serve(self, name, request, context, default):
        with self._lock:
            self._rpcs.append(Rpc(time.time(), name, _to_dict(request)))
            script = self._scripts.get(name)
            if script is not None and script.times is not None:
                script.times -= 1
                if script.times < 0:
                    self._scripts.pop(name, None)
                    script = None
        if script is not None:
            if script.delay:
                self._release.wait(script.delay)
            if script.code is not None:
                context.abort(script.code, "scripted by the black-box suite")
            if script.reply is not None:
                return script.reply(request, _pb()[0])
        return default()


class _DualToRActive(object):
    def __init__(self, soc, pb2):
        self.soc = soc
        self.pb2 = pb2

    def QueryAdminForwardingPortState(self, request, context):
        return self.soc._serve("QueryAdminForwardingPortState", request, context,
                               lambda: self.pb2.AdminReply(portid=[0, 1], state=list(self.soc.admin)))

    def SetAdminForwardingPortState(self, request, context):
        def default():
            ids = list(request.portid)
            for i, state in zip(ids, request.state):
                self.soc.admin[i] = bool(state)
            return self.pb2.AdminReply(portid=ids, state=[self.soc.admin[i] for i in ids])
        return self.soc._serve("SetAdminForwardingPortState", request, context, default)

    def QueryOperationPortState(self, request, context):
        return self.soc._serve("QueryOperationPortState", request, context,
                               lambda: self.pb2.OperationReply(portid=[0, 1], state=list(self.soc.oper)))

    def QueryLinkState(self, request, context):
        return self.soc._serve("QueryLinkState", request, context,
                               lambda: self.pb2.LinkStateReply(portid=[0, 1], state=list(self.soc.link)))

    def QueryServerVersion(self, request, context):
        return self.soc._serve("QueryServerVersion", request, context,
                               lambda: self.pb2.ServerVersionReply(version=self.soc.version))


class _GracefulRestart(object):
    def __init__(self, soc, pb2):
        self.soc = soc
        self.pb2 = pb2

    def NotifyGracefulRestartStart(self, request, context):
        soc = self.soc
        with soc._lock:
            soc._rpcs.append(Rpc(time.time(), "NotifyGracefulRestartStart", _to_dict(request)))
            script = soc._scripts.get("NotifyGracefulRestartStart")
        if script is not None and script.code is not None:
            context.abort(script.code, "scripted by the black-box suite")
        for resp in list(soc.gr_responses):
            if soc._release.wait(resp.get("delay", 0)):
                return
            yield self.pb2.GracefulAdminResponse(
                msgtype=resp.get("msgtype", 0), notifytype=resp.get("notifytype", 0),
                guid=resp.get("guid", "bb-guid"), period=resp.get("period", 0))
        if soc.gr_hold:
            while not soc._release.wait(0.2):
                if not context.is_active():
                    return


class Blackhole(object):
    """Accepts TCP connections on <ip>:50075 and never answers: a SoC whose
    gRPC server hangs."""

    def __init__(self, ip):
        self.ip = ip
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        self._sock = socket.socket(family, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((ip, GRPC_PORT))
        self._sock.listen(64)
        self._conns = []
        self._stop = False
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self._conns.append(conn)

    def stop(self):
        self._stop = True
        try:
            # close() alone does not wake the thread blocked in accept(), and the
            # address would stay bound.
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._thread.join(5)
        self._sock.close()
        for conn in self._conns:
            conn.close()
