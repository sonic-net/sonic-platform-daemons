"""Launch the real xcvr-emu servicer on an ephemeral loopback port."""

import asyncio
import logging
from pathlib import Path
import signal
import sys

import grpc
from xcvr_emu.proto import emulator_pb2_grpc
from xcvr_emu.server import EmulatorServer


async def serve(config_path, ready_path):
    """Serve until pytest sends SIGTERM, then stop all emulator tasks."""
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)

    # The stock CLI binds all interfaces and does not report the port for -p 0.
    # Only the listener bootstrap differs; EEPROM and state machines are upstream.
    emulator = EmulatorServer(config_path)
    server = grpc.aio.server()
    try:
        emulator_pb2_grpc.add_SfpEmulatorServiceServicer_to_server(emulator, server)
        port = server.add_insecure_port('127.0.0.1:0')
        if not port:
            raise RuntimeError('Unable to bind xcvr-emu to loopback')
        await server.start()
        ready_path = Path(ready_path)
        temporary_path = ready_path.with_suffix('.tmp')
        temporary_path.write_text(str(port), encoding='ascii')
        temporary_path.replace(ready_path)
        await stopping.wait()
    finally:
        try:
            await server.stop(grace=0)
        finally:
            await emulator.stop()


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(serve(*sys.argv[1:]))
