"""Client side of the suite's fake hardware (harness/fake_hw.py).

Imported inside ycabled by the fake vendor driver and the fake platform API.
Every call goes to the suite's server over the unix socket in BB_HW_SOCKET.
"""

import builtins
import json
import os
import socket


def _exception(name, msg):
    cls = getattr(builtins, name, None)
    if not (isinstance(cls, type) and issubclass(cls, Exception)):
        cls = RuntimeError
    return cls(msg)


def call(op, port, method, args=()):
    path = os.environ.get("BB_HW_SOCKET", "/run/bb/hw.sock")
    request = json.dumps({"op": op, "port": port, "method": method, "args": list(args)}) + "\n"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(path)
        sock.sendall(request.encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                raise RuntimeError("fake hardware closed the connection")
            data += chunk
    finally:
        sock.close()
    reply = json.loads(data.decode())
    if "raise" in reply:
        raise _exception(reply["raise"], reply.get("msg", ""))
    return reply.get("value")
