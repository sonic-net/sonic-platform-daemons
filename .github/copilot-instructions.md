# Copilot Instructions for sonic-platform-daemons

## Project Overview

sonic-platform-daemons contains the platform monitoring daemons for SONiC. These daemons run as services on the switch, continuously monitoring hardware components (fans, PSUs, thermals, transceivers, LEDs, PCIe, storage) and publishing their state to the SONiC Redis databases. They consume the platform APIs defined in sonic-platform-common.

## Architecture

```
sonic-platform-daemons/
├── sonic-xcvrd/          # Transceiver daemon (SFP/QSFP monitoring)
│   ├── xcvrd/
│   │   ├── xcvrd.py      # Main transceiver monitoring daemon
│   │   └── ...
│   ├── tests/            # xcvrd tests
│   └── setup.py
├── sonic-psud/           # PSU daemon (power supply monitoring)
│   ├── scripts/psud
│   ├── tests/
│   └── setup.py
├── sonic-thermalctld/    # Thermal control daemon
│   ├── scripts/thermalctld
│   ├── tests/
│   │   └── testbed/     # Physical testbed verification scripts
│   └── setup.py
├── sonic-ledd/           # LED daemon
│   ├── scripts/ledd
│   ├── tests/
│   └── setup.py
├── sonic-pcied/          # PCIe monitoring daemon
│   ├── scripts/pcied
│   ├── tests/
│   └── setup.py
├── sonic-syseepromd/     # System EEPROM daemon
│   ├── scripts/syseepromd
│   ├── tests/
│   └── setup.py
├── sonic-chassisd/       # Chassis daemon (modular chassis)
│   ├── scripts/chassisd
│   ├── tests/
│   └── setup.py
├── sonic-ycabled/        # Y-cable daemon (dual-ToR)
│   ├── ycable/
│   ├── tests/
│   └── setup.py
├── sonic-sensormond/     # Sensor monitoring daemon
├── sonic-stormond/       # Storage monitoring daemon
└── .github/              # GitHub configuration
```

### Key Concepts
- **Each daemon is a standalone Python package** with its own `setup.py` and tests
- **Platform API consumer**: Daemons call `sonic-platform-common` base class methods
- **DB publishers**: Daemons write hardware state to STATE_DB, update COUNTERS_DB
- **Event-driven monitoring**: Daemons poll hardware at intervals, detect state changes

## Language & Style

- **Primary language**: Python 3
- **Indentation**: 4 spaces
- **Naming conventions**:
  - Daemon scripts: lowercase (e.g., `psud`, `thermalctld`, `xcvrd`)
  - Functions: `snake_case`
  - Classes: `PascalCase`
  - Constants: `UPPER_CASE`
- **Logging**: Use `sonic_py_common.daemon_base.Logger`
- **Docstrings**: Required for public methods

## Build Instructions

```bash
# Each daemon is a separate Python package
cd sonic-xcvrd
python3 setup.py bdist_wheel

cd sonic-psud
python3 setup.py bdist_wheel
# etc.
```

## Testing

```bash
# Run tests for a specific daemon
cd sonic-xcvrd
pytest tests/ -v

cd sonic-psud
pytest tests/ -v

# Run with coverage
pytest tests/ --cov --cov-report=term-missing
```

- Each daemon has its own `tests/` directory
- Tests use **pytest** with mock objects
- Platform APIs are mocked (no real hardware needed)
- Tests verify state machine logic, DB updates, and error handling

## PR Guidelines

- **Commit format**: `[daemon]: Description` (e.g., `[xcvrd]: Add DOM monitoring support`)
- **Signed-off-by**: REQUIRED (`git commit -s`)
- **CLA**: Sign Linux Foundation EasyCLA
- **Testing**: All changes must include or update unit tests
- **Platform compatibility**: Changes must not break any vendor platform
- **DB schema**: Document any STATE_DB table changes
- **PR description template**: Fill out all sections of the [PR template](.github/pull_request_template.md) when submitting a pull request:
  - **Description**: Describe your changes in detail
  - **Motivation and Context**: Why is this change required? What problem does it solve? Reference issues with "fixes #xxxx"
  - **How Has This Been Tested?**: Describe how you tested your changes, including test environment and tests run
  - **Additional Information**: Any other relevant details (optional)

## Common Patterns

### Daemon Structure
```python
from sonic_py_common.daemon_base import DaemonBase

class MyDaemon(DaemonBase):
    def __init__(self):
        super().__init__('mydaemon')
        self.platform_chassis = load_platform_chassis()
    
    def run(self):
        while True:
            # Monitor hardware
            status = self.platform_chassis.get_fan(0).get_status()
            # Update DB
            self.state_db.set('FAN_INFO|FAN0', {'status': str(status)})
            time.sleep(self.polling_interval)
```

### DB Update Pattern
```python
# Daemons typically write to STATE_DB
# Table format: TABLE_NAME|KEY
# Fields: key-value pairs representing hardware state
fvs = swsscommon.FieldValuePairs([
    ('status', 'OK'),
    ('speed', '12000'),
    ('temperature', '35.0')
])
tbl.set(key, fvs)
```

## Dependencies

- **sonic-platform-common**: Platform base classes (sonic_platform_base)
- **sonic-py-common**: Common Python utilities, DaemonBase
- **python-swsscommon**: Redis database bindings
- **sonic-buildimage**: Packages are built within the buildimage system

## Gotchas

- **Platform plugin loading**: Daemons dynamically load vendor platform plugins — handle `ImportError` gracefully
- **Polling intervals**: Too-frequent polling wastes CPU; too-infrequent misses events
- **Error resilience**: Daemons must not crash on platform API errors — catch, log, continue
- **Signal handling**: Daemons must handle SIGTERM/SIGINT for clean shutdown
- **Multi-ASIC**: xcvrd and other daemons must be namespace-aware for multi-ASIC platforms
- **DB consistency**: Always update all related fields atomically
- **Warm restart**: Consider state preservation during warm restart scenarios
- **Resource leaks**: Ensure file handles and DB connections are properly closed
