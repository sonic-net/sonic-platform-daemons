"""Verify xcvrd's information and DOM publishers against real CMIS bytes."""

import struct
import threading

import pytest

from .helpers import INFO_TABLE, NAMESPACES, configure_port, post_transceiver_info


pytestmark = pytest.mark.integration


@pytest.mark.parametrize('temperature,voltage', [(42.5, 3.3), (-5.25, 3.1)])
def test_dom_values_are_decoded_and_published(
        db, make_chassis, temperature, voltage):
    """Signed temperature and voltage changes reach STATE_DB via real APIs."""
    from xcvrd import xcvrd
    from xcvrd.dom.utilities.dom_sensor.db_utils import DOMDBUtils
    from xcvrd.xcvrd_utilities import port_event_helper
    from xcvrd.xcvrd_utilities.xcvr_table_helper import XcvrTableHelper

    chassis = make_chassis()
    sfp = chassis.get_sfp(1)
    configure_port(db)
    mapping = port_event_helper.get_port_mapping(NAMESPACES)
    post_transceiver_info(db, mapping, 'Ethernet0')
    info = db.get('STATE_DB', INFO_TABLE, 'Ethernet0')
    assert info['manufacturer'] == 'xcvr-emu'
    assert info['model'] == 'EMU-400G-DR4'
    assert info['cmis_rev'] == '5.2'
    assert info['is_replaceable'] == 'True'

    dom = DOMDBUtils(chassis.sfps, mapping, XcvrTableHelper(NAMESPACES),
                     threading.Event(), xcvrd.helper_logger)
    for current_temperature in (temperature, temperature + 2):
        sfp.client.write(0, 14, struct.pack(
            '>hH', round(current_temperature * 256), round(voltage * 10000)))
        dom.post_port_dom_sensor_info_to_db('Ethernet0')
        dom.post_port_dom_temperature_info_to_db('Ethernet0')

        sensor = db.get('STATE_DB', 'TRANSCEIVER_DOM_SENSOR', 'Ethernet0')
        thermal = db.get('STATE_DB', 'TRANSCEIVER_DOM_TEMPERATURE', 'Ethernet0')
        assert float(sensor['temperature']) == pytest.approx(current_temperature)
        assert float(sensor['voltage']) == pytest.approx(voltage)
        assert float(thermal['temperature']) == pytest.approx(current_temperature)
        assert sensor['last_update_time']
    assert sfp.read_count > 0


def test_empty_port_does_not_publish_info_or_dom(db, make_chassis):
    """Absent EEPROM data must not turn into fabricated transceiver records."""
    from xcvrd import xcvrd
    from xcvrd.dom.utilities.dom_sensor.db_utils import DOMDBUtils
    from xcvrd.xcvrd_utilities import port_event_helper
    from xcvrd.xcvrd_utilities.xcvr_table_helper import XcvrTableHelper

    chassis = make_chassis(absent=(1,))
    configure_port(db)
    mapping = port_event_helper.get_port_mapping(NAMESPACES)
    post_transceiver_info(db, mapping, 'Ethernet0')
    dom = DOMDBUtils(chassis.sfps, mapping, XcvrTableHelper(NAMESPACES),
                     threading.Event(), xcvrd.helper_logger)
    dom.post_port_dom_sensor_info_to_db('Ethernet0')

    assert db.get('STATE_DB', INFO_TABLE, 'Ethernet0') == {}
    assert db.get('STATE_DB', 'TRANSCEIVER_DOM_SENSOR', 'Ethernet0') == {}
