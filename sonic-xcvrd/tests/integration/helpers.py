"""Test producers and bounded lifecycle management for real xcvrd workers."""

import threading
import time


NAMESPACES = ['']
INFO_TABLE = 'TRANSCEIVER_INFO'
STATUS_TABLE = 'TRANSCEIVER_STATUS_SW'


def wait_until(predicate, timeout=45, message='Condition was not satisfied'):
    """Poll without suppressing errors, and report diagnostics on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError(message() if callable(message) else message)


def configure_port(db, lport='Ethernet0', index=1, speed=400000, lanes='0,1,2,3',
                   admin_status='up', host_tx_ready='true'):
    """Publish the CONFIG_DB and STATE_DB input consumed by the CMIS task."""
    db.set('CONFIG_DB', 'PORT', lport, {
        'index': index, 'lanes': lanes, 'speed': speed,
        'admin_status': admin_status, 'subport': 0,
    })
    db.set('STATE_DB', 'PORT_TABLE', lport, {'host_tx_ready': host_tx_ready})


def post_transceiver_info(db, mapping, lport):
    """Publish real EEPROM-derived information, as the SFP state task does."""
    from xcvrd import xcvrd

    return xcvrd.post_port_sfp_info_to_db(
        lport, mapping, db.table('STATE_DB', INFO_TABLE), {})


class TaskRunner:
    """Run a real CMIS manager thread and fail on crashes or leaked workers."""

    def __init__(self, task, db, mapping):
        self.task = task
        self.db = db
        self.mapping = mapping

    def __enter__(self):
        self.task.daemon = True
        self.task.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.task.task_stopping_event.set()
        threading.Thread.join(self.task, timeout=15)
        if self.task.is_alive():
            raise RuntimeError('CMIS manager did not stop: {}'.format(
                self.task.port_dict))
        if self.task.exc is not None:
            raise RuntimeError('CMIS manager crashed') from self.task.exc

    def wait_state(self, lport='Ethernet0', expected='READY', info_fields=None):
        """Wait for a published state, optionally with matching active APSel."""
        def matches():
            if self.task.exc is not None:
                raise RuntimeError('CMIS manager crashed') from self.task.exc
            if not self.task.is_alive():
                raise RuntimeError('CMIS manager exited unexpectedly')
            state = self.db.get('STATE_DB', STATUS_TABLE, lport).get('cmis_state')
            if state == 'FAILED' and expected != 'FAILED':
                raise AssertionError('CMIS failed for {}: {}'.format(
                    lport, self.task.port_dict[lport]))
            info = self.db.get('STATE_DB', INFO_TABLE, lport)
            return state == expected and all(
                info.get(key) == value for key, value in (info_fields or {}).items())

        wait_until(matches, message=lambda: '{} did not reach {}. States: {}; port: {}'.format(
            lport, expected, self.db.cmis_states(lport), self.task.port_dict[lport]))
