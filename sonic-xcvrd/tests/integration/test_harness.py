"""Check the transport, swsscommon model, and cleanup used by the integration tests."""

from contextlib import nullcontext
import signal
import socket
import subprocess

import grpc
import pytest
import yaml

from . import emulator, mock_swsscommon as swss
from .emulator import eeprom_segments, running_emulator


pytestmark = pytest.mark.integration


def test_all_swsscommon_imports_use_the_in_memory_module(db, make_chassis):
    """SONiC imports must never fall through to the installed native bindings."""
    from swsscommon import swsscommon
    from swsscommon.swsscommon import DBConnector
    from sonic_py_common import daemon_base, multi_asic, syslogger
    from xcvrd import xcvrd
    from xcvrd.cmis import cmis_manager_task

    assert swsscommon is swss
    assert xcvrd.swsscommon is swss
    assert cmis_manager_task.swsscommon is swss
    assert multi_asic.swsscommon is swss
    assert isinstance(daemon_base.db_connect('STATE_DB'), DBConnector)
    assert DBConnector is swss.DBConnector
    assert (swss.Select.OBJECT, swss.Select.ERROR, swss.Select.TIMEOUT) == (0, 1, 2)
    logger = syslogger.SysLogger('xcvr-emu-test', enable_runtime_config=True)
    assert logger.update_log_level() == (True, '')
    assert db.get('CONFIG_DB', 'LOGGER', 'xcvr-emu-test')['LOGLEVEL'] == 'NOTICE'


def test_table_updates_merge_and_namespace_storage_is_isolated(db):
    """Partial APSel updates retain vendor data and cannot cross namespaces."""
    table = db.table('STATE_DB', 'TRANSCEIVER_INFO', namespace='asic0')
    second_handle = db.table('STATE_DB', 'TRANSCEIVER_INFO', namespace='asic0')
    table.set('Ethernet0', [('manufacturer', 'xcvr-emu'), ('serial', '1')])
    second_handle.set('Ethernet0', [('active_apsel_hostlane1', '2')])

    assert dict(table.get('Ethernet0')[1]) == {
        'manufacturer': 'xcvr-emu', 'serial': '1', 'active_apsel_hostlane1': '2'}
    assert db.table('STATE_DB', 'TRANSCEIVER_INFO', namespace='asic1').getKeys() == []
    assert db.table('CONFIG_DB', 'TRANSCEIVER_INFO', namespace='asic0').getKeys() == []
    second_handle.hdel('Ethernet0', 'serial')
    assert table.hget('Ethernet0', 'serial') == (False, '')
    second_handle._del('Ethernet0')
    assert table.getKeys() == []


def test_subscribers_get_independent_snapshots_and_select_times_out(db):
    """Each observer gets SET/DEL notifications without shared mutable rows."""
    connector = swss.DBConnector('STATE_DB')
    table = swss.Table(connector, 'PORT_TABLE')
    table.set('Ethernet0', [('host_tx_ready', 'false')])
    first = swss.SubscriberStateTable(connector, 'PORT_TABLE')
    second = swss.SubscriberStateTable(connector, 'PORT_TABLE')
    selector = swss.Select()
    selector.addSelectable(first)

    table.set('Ethernet0', [('host_tx_ready', 'true')])
    assert selector.select(0) == (swss.Select.OBJECT, first)
    initial = first.pop()
    initial[2].append(('unrelated', 'value'))
    assert second.pop() == ('Ethernet0', 'SET', [('host_tx_ready', 'false')])
    assert first.pop() == ('Ethernet0', 'SET', [('host_tx_ready', 'true')])
    assert first.pop() == ('', '', [])
    assert selector.select(5) == (swss.Select.TIMEOUT, None)

    table._del('Ethernet0')
    assert selector.select(0) == (swss.Select.OBJECT, first)
    assert first.pop() == ('Ethernet0', 'DEL', [])
    assert second.pop() == ('Ethernet0', 'SET', [('host_tx_ready', 'true')])
    assert second.pop() == ('Ethernet0', 'DEL', [])


@pytest.mark.parametrize('offset,length,expected', [
    (0, 4, [(0, 0, 4)]),
    (126, 4, [(0, 126, 2), (0, 128, 2)]),
    (254, 4, [(0, 254, 2), (1, 128, 2)]),
    (2176, 1, [(16, 128, 1)]),
    (2304, 4, [(17, 128, 4)]),
    (0, 0, []),
])
def test_optoe_address_translation(offset, length, expected):
    """Linear offsets use 128-byte upper pages, not 256-byte RPC windows."""
    assert list(eeprom_segments(offset, length)) == expected


@pytest.mark.parametrize('offset,length', [(-1, 1), (0, -1), (32896, 1)])
def test_invalid_eeprom_ranges_are_rejected(offset, length):
    """Unsupported ranges must not silently wrap into another bank."""
    with pytest.raises(ValueError, match='bank zero'):
        list(eeprom_segments(offset, length))


def test_eeprom_round_trip_across_page_boundary(emulator_factory):
    """Split reads and writes are verified against separate emulator pages."""
    client = emulator_factory()
    offset = 5 * 128 + 254
    data = b'\x12\x34\x56\x78'
    client.write_linear(offset, data)

    assert client.read(5, 254, 2) == data[:2]
    assert client.read(6, 128, 2) == data[2:]
    assert client.read_linear(offset, len(data)) == bytearray(data)


def test_short_eeprom_reads_are_rejected(emulator_factory):
    """An unsplit access beyond the RPC window must not return truncated data."""
    client = emulator_factory()
    with pytest.raises(IOError, match='Short EEPROM read'):
        client.read(0, 254, 4)


def test_transport_errors_are_not_disguised_as_absence(make_chassis):
    """A missing RPC object is an error, distinct from a present=false module."""
    sfp = make_chassis().get_sfp(1)
    sfp.client.stub.Delete(emulator.pb.DeleteRequest(index=1), timeout=3)
    with pytest.raises(grpc.RpcError):
        sfp.get_presence()
    with pytest.raises(grpc.RpcError):
        sfp.read_eeprom(0, 1)


@pytest.mark.parametrize('test_raises', [False, True])
def test_process_is_reaped_and_listener_closed(tmp_path, emulator_config, test_raises):
    """Both normal completion and test failure stop the owned emulator."""
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(yaml.safe_dump(emulator_config), encoding='ascii')
    (tmp_path / 'port').write_text('1', encoding='ascii')
    expectation = pytest.raises(ValueError, match='test body failed') if test_raises else nullcontext()

    with expectation:
        with running_emulator(tmp_path, config_path) as client:
            assert client.process.poll() is None
            assert client.get_info().present
            if test_raises:
                raise ValueError('test body failed')

    assert client.process.returncode == 0
    with socket.socket() as probe:
        probe.settimeout(1)
        assert probe.connect_ex(('127.0.0.1', client.port)) != 0


def test_unresponsive_emulator_is_killed_and_reaped(tmp_path, emulator_config, monkeypatch):
    """Even a stopped child that cannot handle SIGTERM is cleaned up."""
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(yaml.safe_dump(emulator_config), encoding='ascii')
    with pytest.raises(RuntimeError, match='did not stop after SIGTERM'):
        with running_emulator(tmp_path, config_path) as client:
            monkeypatch.setattr(emulator, 'PROCESS_TIMEOUT', 1)
            client.process.send_signal(signal.SIGSTOP)

    assert client.process.returncode == -signal.SIGKILL


def test_startup_failure_reports_logs_and_reaps_process(tmp_path, monkeypatch):
    """Bad configuration must fail visibly and cannot leave a child behind."""
    config_path = tmp_path / 'bad.yaml'
    config_path.write_text('transceivers: [', encoding='ascii')
    real_popen = subprocess.Popen
    processes = []

    def capture_process(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(emulator.subprocess, 'Popen', capture_process)
    with pytest.raises(RuntimeError, match='xcvr-emu failed to start') as error:
        with running_emulator(tmp_path, config_path):
            pytest.fail('Invalid configuration unexpectedly started')

    assert 'ParserError' in str(error.value)
    assert len(processes) == 1
    assert processes[0].returncode is not None
