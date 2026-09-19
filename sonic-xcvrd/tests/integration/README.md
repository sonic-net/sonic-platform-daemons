# xcvrd integration tests with xcvr-emu

These tests run the real `CmisManagerTask` thread, `PortChangeObserver`,
transceiver information publisher, and DOM publishers against
[az-pz/xcvr-emu](https://github.com/az-pz/xcvr-emu).

`EmulatedSfp` inherits SONiC's `SfpOptoeBase`. Its real transceiver API factory,
CMIS parsers and control methods read and write the emulator's EEPROM over
gRPC. CMIS methods, application advertisements, state transitions and telemetry
decoding are **not mocked**.

All required `swsscommon` imports, including those in `sonic-py-common`, are
replaced before importing xcvrd. The in-memory implementation supplies shared,
namespace-aware tables, field merging, subscriber snapshots, SET/DEL events,
blocking selects and logger configuration access. No Redis instance, native
swsscommon library, database configuration file or host SONiC database is used.
This tests the daemon/database boundary, not Redis or swsscommon itself.

## Running the tests

Use Python 3.10+ with `sonic-platform-common`, `sonic-py-common`, and xcvrd's
other runtime dependencies installed.

From the repository root, install the `testing` extra from
[setup.py](../../setup.py) and run pytest:

```bash
cd sonic-xcvrd
python3 -m pip install ".[testing]"
python3 -m pytest
```

Ordinary xcvrd `pytest` discovery includes all integration tests alongside the
unit tests. The integration fixtures isolate their SONiC imports and swsscommon
replacement from the unit tests' import-time mocks, so both suites can run in
the same pytest process. Use `python3 -m pytest -m integration` to select only
integration tests. Missing integration dependencies are failures, not silently
skipped tests.

Additional pytest arguments can narrow the selection, for example:

```bash
python3 -m pytest -m integration -k reinsertion
```

## Coverage

- Cold 400G initialization through the real CMIS state machine, with assertions
  on both STATE_DB and emulator-side datapaths, active APSel and TX-disable bits.
- Admin-down and host-TX-not-ready gating, followed by live DB updates that
  enable the datapath.
- Empty ports and removal/reinsertion while the CMIS thread runs. The test
  producer publishes the same TRANSCEIVER_INFO notifications as the SFP task;
  it does not mock the CMIS event handler.
- Multiple independent physical modules and 400G-to-100G application changes.
- Unsupported applications reaching FAILED rather than false READY.
- EEPROM identity and signed temperature/voltage decoding, repeated DOM
  updates, and absence handling in the actual xcvrd publishers.
- Harness checks for mock DB isolation/notifications, EEPROM page boundaries,
  surfaced transport errors, startup failure, and cleanup after test failures.

## Process lifecycle and emulator limits

Pytest starts and stops all emulator processes and CMIS threads. Each emulator
binds an OS-assigned loopback port; startup requires a successful gRPC health
check. The small [emud.py](./emud.py) launcher uses xcvr-emu's unchanged servicer
because the stock CLI binds all interfaces and does not report the allocated
port when passed `--port 0`. Every RPC and process wait has a deadline. Teardown
sends SIGTERM, waits for graceful shutdown, and kills/reaps a stuck child while
reporting an error. Logs and generated configs are retained in pytest's
per-test temporary directory for diagnosis.

The dependency is pinned to fork commit
`3aca04f89de6dfecf33bea29509234164d917a81`, which provides package-qualified
protobuf imports and SONiC-compatible gRPC/protobuf constraints.

At that revision all transceivers in one emulator process share EEPROM storage,
so each emulated physical port gets a separate process containing index 1.
[emu_config.yaml](./emu_config.yaml) advertises bank zero, 400G/4-lane and
100G/1-lane applications with matching host/media lane counts. Identity strings
are space-padded as required by CMIS. The adapter explicitly rejects other banks.
This suite does not claim to model optical links, real hardware timing,
coherent modules, CPO devices, or the complete xcvrd service startup.
