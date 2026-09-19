"""Pytest-owned xcvr-emu processes and the EEPROM gRPC transport."""

from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys
import time

import grpc
from xcvr_emu.proto import emulator_pb2 as pb
from xcvr_emu.proto import emulator_pb2_grpc as rpc


RPC_TIMEOUT = 3
PROCESS_TIMEOUT = 10


def eeprom_segments(offset, length):
    """Split bank-zero SONiC optoe offsets into CMIS page-window accesses."""
    if offset < 0 or length < 0 or offset + length > 128 + 256 * 128:
        raise ValueError('EEPROM range must fit in CMIS bank zero')
    while length:
        if offset < 128:
            page, register = 0, offset
            size = min(length, 128 - offset)
        else:
            page, within = divmod(offset - 128, 128)
            register = 128 + within
            size = min(length, 128 - within)
        yield page, register, size
        offset += size
        length -= size


class EmulatorClient:
    """Access one real transceiver, always index 1 in its own process."""

    def __init__(self, channel, process, port, log_path):
        self.stub = rpc.SfpEmulatorServiceStub(channel)
        self.process = process
        self.port = port
        self.log_path = log_path

    def get_info(self):
        """Get presence and independent emulator-side datapath state."""
        return self.stub.GetInfo(pb.GetInfoRequest(index=1), timeout=RPC_TIMEOUT)

    def set_present(self, present):
        """Insert or remove the transceiver using its real state machine."""
        self.stub.UpdateInfo(
            pb.UpdateInfoRequest(index=1, present=present), timeout=RPC_TIMEOUT)

    def read(self, page, offset, length):
        """Read a CMIS window; short reads are failures, not absent hardware."""
        data = self.stub.Read(pb.ReadRequest(
            index=1, bank=0, page=page, offset=offset, length=length),
            timeout=RPC_TIMEOUT).data
        if len(data) != length:
            raise IOError('Short EEPROM read: expected {}, received {}'.format(
                length, len(data)))
        return data

    def write(self, page, offset, data):
        """Write bytes through the emulator's normal host-write path."""
        self.stub.Write(pb.WriteRequest(
            index=1, bank=0, page=page, offset=offset,
            length=len(data), data=bytes(data)), timeout=RPC_TIMEOUT)

    def read_linear(self, offset, length):
        """Read a linear optoe range, including page-boundary crossings."""
        return bytearray().join(
            self.read(page, register, size)
            for page, register, size in eeprom_segments(offset, length))

    def write_linear(self, offset, data):
        """Write a linear optoe range without crossing a CMIS RPC window."""
        cursor = 0
        for page, register, size in eeprom_segments(offset, len(data)):
            self.write(page, register, data[cursor:cursor + size])
            cursor += size


@contextmanager
def running_emulator(directory, config_path):
    """Start, health-check, and always reap one xcvr-emu subprocess."""
    log_path = directory / 'xcvr-emu.log'
    ready_path = directory / 'port'
    ready_path.unlink(missing_ok=True)
    command = [sys.executable, str(Path(__file__).with_name('emud.py')),
               str(config_path), str(ready_path)]
    with log_path.open('w', encoding='utf-8') as log:
        process = subprocess.Popen(
            command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        started = False
        try:
            deadline = time.monotonic() + PROCESS_TIMEOUT
            while not ready_path.exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('xcvr-emu failed to start (exit={}):\n{}'.format(
                        process.poll(), log_path.read_text(encoding='utf-8')))
                time.sleep(0.05)

            port = int(ready_path.read_text(encoding='ascii'))
            with grpc.insecure_channel('127.0.0.1:{}'.format(port)) as channel:
                grpc.channel_ready_future(channel).result(timeout=RPC_TIMEOUT)
                client = EmulatorClient(channel, process, port, log_path)
                response = client.stub.List(pb.ListRequest(), timeout=RPC_TIMEOUT)
                if [info.index for info in response.infos] != [1]:
                    raise RuntimeError('Expected exactly one emulated transceiver')
                started = True
                yield client
                if process.poll() is not None:
                    raise RuntimeError('xcvr-emu exited during the test:\n{}'.format(
                        log_path.read_text(encoding='utf-8')))
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=PROCESS_TIMEOUT)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=PROCESS_TIMEOUT)
                    raise RuntimeError('xcvr-emu did not stop after SIGTERM:\n{}'.format(
                        log_path.read_text(encoding='utf-8')))
            if started and process.returncode != 0:
                raise RuntimeError('xcvr-emu exited with status {}:\n{}'.format(
                    process.returncode, log_path.read_text(encoding='utf-8')))
