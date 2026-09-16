"""Isolated integration fixtures; neither Redis nor native swsscommon is used."""

from contextlib import ExitStack
import copy
from itertools import count
import logging
from logging.handlers import SysLogHandler
from pathlib import Path
import sys
import threading
from types import ModuleType

import pytest

from . import mock_swsscommon
from .helpers import NAMESPACES, TaskRunner, post_transceiver_info


INTEGRATION_DIR = Path(__file__).parent


def pytest_configure(config):
    """Register the integration marker without changing shared import state."""
    config.addinivalue_line(
        'markers',
        'integration: real xcvr-emu process and SONiC platform APIs, mocked swsscommon')


@pytest.fixture(scope='module', autouse=True)
def isolated_sonic_modules():
    """Keep unit-test import-time mocks separate from real integration APIs."""
    module_roots = ('xcvrd', 'sonic_py_common', 'swsscommon')
    with pytest.MonkeyPatch.context() as patch:
        for name in list(sys.modules):
            if name.split('.', 1)[0] in module_roots:
                patch.delitem(sys.modules, name)

        package = ModuleType('swsscommon')
        package.__path__ = []
        package.swsscommon = mock_swsscommon
        patch.setitem(sys.modules, 'swsscommon', package)
        patch.setitem(sys.modules, 'swsscommon.swsscommon', mock_swsscommon)
        patch.delenv('XCVRD_UNIT_TESTING', raising=False)
        # SysLogger still propagates to pytest's logging capture, without /dev/log.
        patch.setattr(SysLogHandler, 'emit', logging.NullHandler.emit)
        try:
            yield
        finally:
            for name in list(sys.modules):
                if name.split('.', 1)[0] in module_roots:
                    sys.modules.pop(name)


@pytest.fixture
def db(monkeypatch):
    """Provide fresh databases and a single-ASIC platform for each test."""
    from sonic_py_common import multi_asic

    mock_swsscommon.DATABASE.reset()
    monkeypatch.setattr(multi_asic, 'is_multi_asic', lambda: False)
    monkeypatch.setattr(multi_asic, 'get_asic_index_from_namespace', lambda namespace: 0)
    return mock_swsscommon.DATABASE


@pytest.fixture
def emulator_config():
    """Load a paged CMIS profile advertising 400G and single-lane 100G apps."""
    import yaml

    return yaml.safe_load((INTEGRATION_DIR / 'emu_config.yaml').read_text())


@pytest.fixture
def emulator_factory(tmp_path, emulator_config):
    """Start one isolated emulator process per port, stopping all on teardown."""
    import yaml
    from .emulator import running_emulator

    with ExitStack() as processes:
        process_numbers = count()

        def create(present=True, serial='EMU000000000001'):
            directory = tmp_path / 'emulator-{}'.format(next(process_numbers))
            directory.mkdir()
            config = copy.deepcopy(emulator_config)
            config['transceivers'][1]['present'] = present
            config['transceivers'][1]['defaults']['VendorSN'] = serial.ljust(16)
            config_path = directory / 'config.yaml'
            config_path.write_text(yaml.safe_dump(config), encoding='ascii')
            client = processes.enter_context(running_emulator(directory, config_path))
            return client

        yield create


@pytest.fixture
def make_chassis(db, emulator_factory, monkeypatch):
    """Inject real platform API objects into xcvrd's hardware lookup seams."""
    from xcvrd import xcvrd
    from xcvrd.xcvrd_utilities import common
    from .emulated_platform import EmulatedChassis, EmulatedSfp

    def create(indices=(1,), absent=()):
        sfps = {
            index: EmulatedSfp(index, emulator_factory(
                present=index not in absent, serial='EMU{:012d}'.format(index)))
            for index in indices
        }
        chassis = EmulatedChassis(sfps)
        monkeypatch.setattr(xcvrd, 'platform_chassis', chassis)
        monkeypatch.setattr(common, 'platform_chassis', chassis)
        monkeypatch.setattr(common, 'platform_sfputil', None)
        return chassis

    return create


@pytest.fixture
def start_cmis(db, make_chassis):
    """Start real CMIS threads after seeding portsyncd's startup notification."""
    from xcvrd.cmis import CmisManagerTask
    from xcvrd.xcvrd_utilities import common, port_event_helper

    with ExitStack() as workers:
        def start():
            mapping = port_event_helper.get_port_mapping(NAMESPACES)
            for lport in mapping.logical_port_list:
                post_transceiver_info(db, mapping, lport)
            db.set('APPL_DB', 'PORT_TABLE', 'PortConfigDone', {'count': '1'})
            task = CmisManagerTask(
                NAMESPACES, mapping, common.get_pluggable_obj_dict(mapping),
                threading.Event())
            return workers.enter_context(TaskRunner(task, db, mapping))

        yield start
