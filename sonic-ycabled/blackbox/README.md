# ycabled black-box component tests

These tests run the ycabled daemon as a separate process against a real redis
and stand-ins for everything it talks to, and check what it writes to the
databases, sends to the NIC SoCs, asks of the Y-cable drivers, and logs. They
know nothing about how ycabled is written, so the same tests check Python
ycabled today and the Rust implementation later ([HLD] section 6.3, WI-3).

## Running

The tests take over the environment they run in: they run redis-server for the
SONiC databases, point `/dev/log` at their own syslog socket, and write
`/var/run/redis*`, `/etc/sonic` and `/usr/share/sonic`. So they run only where
`VS_ENABLE=1` says that is fine, in a throwaway container. Without it, `pytest` in
`sonic-ycabled` runs the unit tests only, as on a developer machine or in a
sonic-buildimage build.

### In their container

`run.sh` runs them in a container built on `docker-sonic-vs`, which has
redis-server, swsscommon, sonic_py_common, sonic_platform_base and
sonic_y_cable. Get it from a sonic-buildimage VS build: download
`target/docker-sonic-vs.gz` from the `sonic-buildimage.vs` artifact of the Azure
pipeline, and `docker load -i docker-sonic-vs.gz`, which creates
`docker-sonic-vs:latest`. Use a build of the branch under test: the libraries
ycabled imports (sonic_py_common, sonic_platform_base, sonic_y_cable, swsscommon)
come from it. `run.sh` builds the test image on it and runs pytest in it, as a
user with sudo like the pipeline does; arguments go to pytest.

```sh
sonic-ycabled/blackbox/run.sh                     # everything, about 15 minutes
sonic-ycabled/blackbox/run.sh -m "not slow"       # skip the cases that wait for 60 s timers
sonic-ycabled/blackbox/run.sh tests/test_cli.py -k firmware
sonic-ycabled/blackbox/run.sh --bb-coverage --bb-require-full-contract
```

### With the unit tests

`pytest` in `sonic-ycabled` collects these tests too (`testpaths` in its
`pytest.ini`). With `VS_ENABLE=1` they run after the unit tests, in the same
results, and the coverage of Python ycabled under them adds to the unit tests' in
pytest-cov's report; the run takes about 14 minutes instead of 15 seconds. The
sonic-platform-daemons pipeline runs them this way, in its sonic-slave container,
where libswsscommon, python3-swsscommon and the SONiC wheels are installed for the
unit tests already:

```sh
sudo apt-get install -y redis-server python3-pytest-timeout
cd sonic-ycabled && VS_ENABLE=1 pytest
```

Every black-box test gets a time limit (`--bb-test-timeout`, 300 s by default)
through pytest-timeout, whichever `pytest.ini` the run uses: several known issues
are hangs. With `VS_ENABLE=1`, the suite does not start without pytest-timeout,
unless the limit is 0.

When not root, the suite uses `sudo -n` to make those directories its own and to
point `/dev/log` at its socket.

| Option or variable | Meaning |
| --- | --- |
| `--bb-coverage` | Measure the code coverage of Python ycabled under the suite |
| `--bb-require-full-contract` | Fail the run if an entry of the DB contract was not exercised |
| `--bb-workdir` | Scratch directory in the container (default `/tmp/ycabled-blackbox`) |
| `--bb-report-dir` | Where the reports go (default: `report` in the scratch directory; `run.sh` sets it) |
| `--bb-test-timeout` | Seconds a test may take, fixtures included (default 300; 0: no limit) |
| `VS_ENABLE` | `1` lets the tests run here (see above); `run.sh`'s image and the pipeline's ycabled step set it |
| `BB_BASE_IMAGE` | Base image (default `docker-sonic-vs:latest`); pulled if it is not local |
| `BB_IMAGE` | Name of the test image `run.sh` builds (default `ycabled-blackbox:latest`) |
| `BB_REPORT_DIR` | Where the reports go (default `sonic-ycabled/blackbox/report`) |
| `BB_YCABLED_CMD` | Command of another implementation, run instead of Python ycabled from this tree |

Reports:

- `db-contract-coverage.md`: every DB access in the contract and how often the suite saw it
- `ycabled-coverage.txt`, `ycabled-coverage-html/`: line and branch coverage of Python ycabled (`--bb-coverage`;
  without it, in a pytest-cov run, the coverage goes into pytest-cov's report)

## How it works

```text
 pytest process (the test)                         ycabled process (under test)
 ---------------------------------------           -----------------------------------
 Dut: orchagent, linkmgrd, xcvrd, CLI  --redis-->   subscriptions, tables
 Monitor: redis MONITOR of ycabled's   <--------    (the suite connects from 127.0.0.2,
          commands, DB contract check                so MONITOR tells them apart)
 FakeHardware server  <--unix socket JSON--------   fake vendor driver (sonic_y_cable
   per-port cable model, scripts, call log           overlay) and fake sonic_platform
 Soc doubles (grpcio) on <ip>:50075    <--gRPC--    DualToRActive, GracefulRestart
 MuxSimulator (HTTP)                   <--HTTP--    real y_cable_simulated driver (VS)
 SyslogSink (/dev/log points at it)    <--syslog--  logs
```

- **Inputs** are what a dual ToR gives ycabled: CONFIG_DB (`CONFIG_DB_INITIALIZED`, `DEVICE_METADATA`,
  `PORT`, `MUX_CABLE`, `LOOPBACK_INTERFACE`, `GRPCCLIENT`, `XCVRD_LOG`), STATE_DB
  `TRANSCEIVER_INFO`, requests in APPL_DB, the files `/etc/sonic/grpc_secrets.json`,
  `/etc/sonic/mux_simulator.json` and `/usr/share/sonic/firmware/*`, signals, and the
  answers of the cables and SoCs. `harness/topology.py` builds them.
- **Cables.** ycabled loads drivers through `sonic_y_cable.y_cable_vendor_mapping`, as
  on a switch. The suite puts a copy of the installed `sonic_y_cable` first on ycabled's
  `PYTHONPATH`, with a fake driver (`pylib/bbfake/y_cable_bbfake.py`) added to the
  mapping. The driver and the fake `sonic_platform` (`pylib/sonic_platform`) forward
  every call to `harness/fake_hw.py` in the test process, where each cable is a
  `PortModel`. Tests change the model, script a method to return a value, raise, delay
  or block, and read the log of calls. A real cable serves one call at a time, so the
  server also records a driver call that starts while another call to the same cable
  is still running (`hw.overlaps()`).
- **SoCs.** `harness/soc.py` serves both gRPC services from ycabled's own proto on
  127.0.0.x:50075 or [::1]:50075, with TLS when asked. A `Blackhole` accepts TCP and
  never answers.
- **Observation.** Requests wait for ycabled's write in the MONITOR stream rather than
  polling values, so a test sees exactly what ycabled wrote, in order. Readiness after
  start is "ycabled has subscribed to the tables it serves and has gone quiet".
- **Checks on every test.** The `dut` fixture's teardown fails the test if ycabled
  exited unexpectedly, logged a crash, or wrote a table, field or value outside the DB
  contract (`harness/contract.py`). Some values depend on the port's cable type, such
  as `active_side`: a number for active-standby, a state for active-active.
- **Stopping deletes nothing.** The contract allows no deletes besides the CLI's
  command keys, and `test_stop_keeps_tables` checks that the tables survive a stop
  ([HLD] C7). Python's `deinit()` would delete them, but never runs (3.8 issue 1).

## Writing tests

```python
from harness.topology import Topology

def test_toggle(dut):
    dut.configure(Topology().active_standby("Ethernet0"))  # CONFIG_DB, files, fake cables
    dut.hw.port(1).script("toggle_mux_to_tor_b", value=False)
    dut.start()                                           # waits until ycabled is ready
    assert dut.toggle("Ethernet0", "standby")["state"] == "unknown"
```

| `dut` member | Stands in for |
| --- | --- |
| `toggle`, `peer_toggle` | orchagent and linkmgrd writing `HW_MUX_CABLE_TABLE`, `HW_FORWARDING_STATE_PEER` |
| `probe`, `fwd_probe` | linkmgrd's `MUX_CABLE_COMMAND_TABLE` and `FORWARDING_STATE_COMMAND` probes |
| `insert`, `remove` | xcvrd's `TRANSCEIVER_INFO` |
| `answered` | whether ycabled answered a request sent with `wait=False` |
| `cli` | `show/config muxcable`, with the CLI's table protocol |
| `soc`, `blackhole`, `mux_simulator` | NIC SoCs and the KVM mux simulator |
| `hw` | the cables |
| `appl`, `config`, `state`, `monitor`, `syslog` | the databases, ycabled's DB accesses, its log |

Markers:

- `slow`: waits for one of ycabled's 60 s timers (telemetry, thread checks).
- `known_issue(ref, reason)`: a behavior Python ycabled gets wrong ([HLD] section 3.8). The
  test asserts the correct behavior and is a strict xfail on Python ycabled, so it starts
  failing when the issue is fixed, and it must pass on a new implementation. Only an
  `AssertionError` counts as the expected failure. The harness raises
  `harness.util.HarnessError` (a `RuntimeError`) when it cannot do what a test asked:
  the request helpers' waits (`toggle`, `probe`, `cli`, `monitor.wait_access`, ...),
  `start()`, the barriers. So when the issue is a missing answer, send with
  `wait=False` and assert the answer: `assert dut.answered("toggle", port, mark)`, or
  `assert dut.monitor.eventually(...)`, `harness.util.eventually(...)`.
- `python_only`: checks a mechanism of Python ycabled, skipped for other implementations.

## What the suite covers

| Function (HLD section) | Tests |
| --- | --- |
| Startup, port map, restart, signals, startup failures (3.3) | `test_lifecycle.py` |
| Active-standby toggle and probe, driver selection, VS and the mux simulator (3.4.1, 3.4.2, 3.5.2) | `test_active_standby.py` |
| Active-active toggles, forwarding state probe, channel, connectivity `failure`, TLS, GracefulRestart (3.4.3 to 3.4.6, 3.5.1) | `test_active_active.py` |
| Cable insertion and removal (3.4.7) | `test_insert_remove.py` |
| Telemetry (3.4.8) | `test_telemetry.py` |
| Every CLI command family and its error paths (3.4.9, 3.6.5) | `test_cli.py` |
| `XCVRD_LOG`, Loopback3, `grpc_secrets.json`, log compatibility, Redis connections (3.4.10, 3.5.1, C5, N5) | `test_config.py` |
| Multi-ASIC namespaces (F1) | `test_multi_asic.py` |
| Requests for one cable from different sources at the same time: toggles, insertion and removal, the CLI (3.8 issues 26 to 29) | `test_concurrency.py` |
| DB schema (3.6) | the DB contract, checked on every test |

Each known issue of [HLD] section 3.8 that a test can observe has a `known_issue` test:
1, 2, 32 (`test_lifecycle.py`); 3, 4 (`test_telemetry.py`); 7, 20, 30
(`test_active_active.py`); 13, 25 (`test_config.py`); 5, 15, 16, 21, 22
(`test_insert_remove.py`); 17, 18, 24, 31 (`test_active_standby.py`); 19
(`test_cli.py`); 23 (`test_multi_asic.py`); 26 to 29 (`test_concurrency.py`). The
requirements N1 and N3 have one each in `test_active_active.py`.

## Other implementations

`BB_YCABLED_CMD` runs another binary with the same inputs. The suite expects it to:

- leave the address 127.0.0.2 to the suite: MONITOR tells the suite's redis commands
  from the daemon's by it;
- load vendor drivers through `sonic_y_cable` with the `PYTHONPATH` it is given, and the
  platform API through `sonic_platform`;
- log through `/dev/log`;
- take the platform from `PLATFORM` and `DEVICE_METADATA`, like `sonic_py_common`.

[HLD]: https://github.com/sonic-net/SONiC/blob/master/doc/dualtor/ycabled_to_rust_hld.md
