"""Exercise CMIS management through real SONiC APIs and xcvr-emu EEPROM."""

import struct

import pytest

from .helpers import INFO_TABLE, configure_port, post_transceiver_info


pytestmark = pytest.mark.integration


def test_initialization_activates_datapath(db, make_chassis, start_cmis):
    """A cold 400G module is programmed and its active application published."""
    sfp = make_chassis().get_sfp(1)
    api = sfp.get_xcvr_api()
    assert api.get_module_state() == 'ModuleLowPwr'
    configure_port(db)

    runner = start_cmis()
    runner.wait_state(info_fields={'active_apsel_hostlane1': '1'})

    assert api.get_module_state() == 'ModuleReady'
    assert [dp.state for dp in sfp.client.get_info().dpsms] == [
        'DPStateHostLaneEnum.DPACTIVATED']
    assert list(api.get_active_apsel_hostlane().values()) == [1] * 4 + [0] * 4
    assert api.get_tx_disable() == [False] * 4 + [True] * 4
    assert runner.task.port_dict['Ethernet0']['host_lanes_mask'] == 0x0f
    assert runner.task.port_dict['Ethernet0']['appl'] == 1

    info = db.get('STATE_DB', INFO_TABLE, 'Ethernet0')
    assert info['manufacturer'] == 'xcvr-emu'
    assert info['host_lane_count'] == info['media_lane_count'] == '4'
    assert info['active_apsel_hostlane4'] == '1'
    assert info['active_apsel_hostlane5'] == 'N/A'
    assert sfp.read_count > 0
    assert any(offset == 2178 for offset, _ in sfp.writes)  # Page 10h TX disable
    states = db.cmis_states('Ethernet0')
    for state in ('INSERTED', 'DP_DEINIT', 'AP_CONFIGURED', 'DP_INIT', 'DP_TXON',
                  'DP_ACTIVATION', 'READY'):
        assert state in states
    assert 'FAILED' not in states


@pytest.mark.parametrize('admin_status,host_tx_ready', [
    ('down', 'true'),
    ('up', 'false'),
])
def test_tx_remains_disabled_until_host_is_ready(
        db, make_chassis, start_cmis, admin_status, host_tx_ready):
    """Either TX gate prevents activation; a live DB update releases it."""
    sfp = make_chassis().get_sfp(1)
    configure_port(db, admin_status=admin_status, host_tx_ready=host_tx_ready)
    runner = start_cmis()
    runner.wait_state(info_fields={'host_lane_count': 'N/A'})

    api = sfp.get_xcvr_api()
    assert api.get_tx_disable()[:4] == [True] * 4
    assert all(state == 'DataPathDeactivated'
               for state in api.get_datapath_state().values())
    assert 'DP_ACTIVATION' not in db.cmis_states('Ethernet0')
    assert db.get('STATE_DB', INFO_TABLE, 'Ethernet0')['active_apsel_hostlane1'] == 'N/A'

    db.set('CONFIG_DB', 'PORT', 'Ethernet0', {'admin_status': 'up'})
    db.set('STATE_DB', 'PORT_TABLE', 'Ethernet0', {'host_tx_ready': 'true'})
    runner.wait_state(info_fields={'active_apsel_hostlane1': '1'})
    assert api.get_tx_disable()[:4] == [False] * 4
    assert [dp.state for dp in sfp.client.get_info().dpsms] == [
        'DPStateHostLaneEnum.DPACTIVATED']


def test_empty_port_is_removed(db, make_chassis, start_cmis):
    """An absent transceiver is not mistaken for a failed or ready module."""
    sfp = make_chassis(absent=(1,)).get_sfp(1)
    configure_port(db)
    runner = start_cmis()
    runner.wait_state(expected='REMOVED')

    assert not sfp.get_presence()
    assert db.get('STATE_DB', INFO_TABLE, 'Ethernet0') == {}
    assert not sfp.writes
    assert 'FAILED' not in db.cmis_states('Ethernet0')


def test_removal_and_reinsertion_reinitialize_module(db, make_chassis, start_cmis):
    """Presence and TRANSCEIVER_INFO notifications re-arm the running task."""
    sfp = make_chassis().get_sfp(1)
    configure_port(db)
    runner = start_cmis()
    runner.wait_state(info_fields={'active_apsel_hostlane1': '1'})
    writes_before_removal = len(sfp.writes)

    sfp.set_present(False)
    db.delete('STATE_DB', INFO_TABLE, 'Ethernet0')
    runner.wait_state(expected='REMOVED')
    assert not sfp.get_presence()

    sfp.set_present(True)
    assert sfp.get_xcvr_api().get_module_state() == 'ModuleLowPwr'
    post_transceiver_info(db, runner.mapping, 'Ethernet0')
    runner.wait_state(info_fields={'active_apsel_hostlane1': '1'})

    assert len(sfp.writes) > writes_before_removal
    assert [dp.state for dp in sfp.client.get_info().dpsms] == [
        'DPStateHostLaneEnum.DPACTIVATED']
    assert 'FAILED' not in db.cmis_states('Ethernet0')


def test_ports_have_independent_eeprom_and_state(db, make_chassis, start_cmis):
    """One manager initializes two physical modules without shared EEPROM."""
    chassis = make_chassis(indices=(1, 2))
    configure_port(db)
    configure_port(db, lport='Ethernet4', index=2, lanes='4,5,6,7')
    runner = start_cmis()

    for lport, index in (('Ethernet0', 1), ('Ethernet4', 2)):
        runner.wait_state(lport, info_fields={'active_apsel_hostlane1': '1'})
        sfp = chassis.get_sfp(index)
        assert [dp.state for dp in sfp.client.get_info().dpsms] == [
            'DPStateHostLaneEnum.DPACTIVATED']
        assert db.get('STATE_DB', INFO_TABLE, lport)['serial'] == 'EMU{:012d}'.format(index)

    first, second = chassis.get_sfp(1), chassis.get_sfp(2)
    assert first.client.process.pid != second.client.process.pid
    assert first.client.port != second.client.port
    before = second.get_temperature()
    first.client.write(0, 14, struct.pack('>h', 45 * 256))
    assert first.get_temperature() == 45
    assert second.get_temperature() == before


def test_unsupported_application_is_reported_failed(db, make_chassis, start_cmis):
    """A speed absent from the real advertisement must not produce READY."""
    sfp = make_chassis().get_sfp(1)
    configure_port(db, speed=25000)
    runner = start_cmis()
    runner.wait_state(expected='FAILED')

    assert not sfp.writes
    assert sfp.get_xcvr_api().get_module_state() == 'ModuleLowPwr'
    assert 'READY' not in db.cmis_states('Ethernet0')


def test_speed_change_reprograms_application(db, make_chassis, start_cmis):
    """A CONFIG_DB change decommissions 400G and stages the advertised 100G app."""
    sfp = make_chassis().get_sfp(1)
    configure_port(db)
    runner = start_cmis()
    runner.wait_state(info_fields={'active_apsel_hostlane1': '1'})

    db.set('CONFIG_DB', 'PORT', 'Ethernet0', {'speed': '100000', 'lanes': '0'})
    runner.wait_state(info_fields={'active_apsel_hostlane1': '2', 'host_lane_count': '1'})

    api = sfp.get_xcvr_api()
    assert list(api.get_active_apsel_hostlane().values()) == [2] + [0] * 7
    assert api.get_tx_disable() == [False] + [True] * 7
    assert [(dp.appsel, dp.state) for dp in sfp.client.get_info().dpsms] == [
        (2, 'DPStateHostLaneEnum.DPACTIVATED')]
    assert runner.task.port_dict['Ethernet0']['appl'] == 2
    assert 'FAILED' not in db.cmis_states('Ethernet0')
