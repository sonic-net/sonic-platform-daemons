# ycabled black-box component tests

These tests run the ycabled daemon as a separate process against a real redis
and stand-ins for everything it talks to, and check what it writes to the
databases, sends to the NIC SoCs, asks of the Y-cable drivers, and logs. They
know nothing about how ycabled is written, so the same tests check Python
ycabled today and the Rust implementation later ([HLD] section 6.3).

## Running

The tests take over the environment they run in: they run redis-server for the
SONiC databases, point `/dev/log` at their own syslog socket, write
`/var/run/redis*`, `/etc/sonic` and `/usr/share/sonic`, and route the dual ToR's
VLAN, 192.168.0.0/21 and fc02:1000::/64, to `lo`, where their SoC doubles listen.
So they run only where `BB_ENABLE=1` says that is fine, in a throwaway container.
Without it, `pytest` in `sonic-ycabled` runs the unit tests only, as on a
developer machine or in a sonic-buildimage build.

### In their container

`run.sh` runs them in a container built on `docker-sonic-vs`, which has
redis-server, swsscommon, sonic_py_common, sonic_platform_base and
sonic_y_cable. Get it from a sonic-buildimage VS build: download
`target/docker-sonic-vs.gz` from the `sonic-buildimage.vs` artifact of the Azure
pipeline, and `docker load -i docker-sonic-vs.gz`, which creates
`docker-sonic-vs:latest`. Use a build of the branch under test: the libraries
ycabled imports (sonic_py_common, sonic_platform_base, sonic_y_cable, swsscommon)
come from it. `run.sh` builds the test image on it and runs pytest in it, as a
user with sudo and with the `NET_ADMIN` capability, like the pipeline does;
arguments go to pytest.

```sh
sonic-ycabled/blackbox/run.sh                     # everything, about 16 minutes
sonic-ycabled/blackbox/run.sh -m "not slow"       # skip the cases that take a minute or more
sonic-ycabled/blackbox/run.sh tests/test_cli.py -k firmware
sonic-ycabled/blackbox/run.sh --bb-coverage                  # with Python ycabled's code coverage
```

### With the unit tests

`pytest` in `sonic-ycabled` collects these tests too (`testpaths` in its
`pytest.ini`). With `BB_ENABLE=1` they run after the unit tests, in the same
results, and the coverage of Python ycabled under them adds to the unit tests' in
pytest-cov's report; the run takes about 15 minutes instead of 15 seconds. The
sonic-platform-daemons pipeline runs them this way, in its sonic-slave container,
which it starts with `--cap-add=NET_ADMIN`, and where libswsscommon,
python3-swsscommon and the SONiC wheels are installed for the unit tests already:

```sh
sudo apt-get install -y redis-server python3-pytest-timeout
cd sonic-ycabled && BB_ENABLE=1 pytest
```

Every black-box test gets a time limit (`--bb-test-timeout`, 300 s by default)
through pytest-timeout, whichever `pytest.ini` the run uses: several known issues
are hangs. With `BB_ENABLE=1`, the suite does not start without pytest-timeout,
unless the limit is 0.

When not root, the suite uses `sudo -n` to make those directories its own, to
point `/dev/log` at its socket, and to route the VLAN to `lo`. The routes need the
`NET_ADMIN` capability, which a container gets with `docker run --cap-add NET_ADMIN`;
without it, the suite does not start.

| Option or variable | Meaning |
| --- | --- |
| `--bb-coverage` | Measure the code coverage of Python ycabled under the suite |
| `--bb-no-require-full-contract` | Do not fail a run of the whole suite that left an entry of the DB contract unexercised (see Reports) |
| `--bb-workdir` | Scratch directory in the container (default `/tmp/ycabled-blackbox`) |
| `--bb-report-dir` | Where the reports go (default: `report` in the scratch directory; `run.sh` sets it) |
| `--bb-test-timeout` | Seconds a test may take, fixtures included (default 300; 0: no limit) |
| `BB_ENABLE` | `1` lets the tests run here (see above); `run.sh`'s image sets it |
| `BB_BASE_IMAGE` | Base image (default `docker-sonic-vs:latest`); pulled if it is not local |
| `BB_IMAGE` | Name of the test image `run.sh` builds (default `ycabled-blackbox:latest`) |
| `BB_REPORT_DIR` | Where the reports go (default `sonic-ycabled/blackbox/report`) |
| `BB_YCABLED_CMD` | Command of another implementation, run instead of Python ycabled from this tree |

Reports:

- `db-contract-coverage.md`: every DB access in the contract and how often the suite saw it.
  A run of the whole suite on Python ycabled fails if an entry was not exercised: either
  no test reaches it any more, or ycabled stopped making that access and the entry can
  go. A partial run (a file, a test, `-k`, `-m`) only reports it, and so does a run of
  another implementation, which need not make every access Python makes.
  `--bb-no-require-full-contract` turns the check off.
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
  port 50075 of the address sonic-mgmt gives the SoC, in the dual ToR's VLAN:
  192.168.0.3 for `Ethernet0`, 192.168.0.5 for `Ethernet4`, and so on
  (`topology.soc_ip`), with TLS when asked. The suite routes the VLAN to `lo`, so a
  SoC double can listen on any of its addresses, and an address where none listens
  refuses connections. A `Blackhole` accepts TCP and never answers.
- **Observation.** Requests wait for ycabled's write in the MONITOR stream rather than
  polling values, so a test sees exactly what ycabled wrote, in order. Readiness after
  start is "ycabled has subscribed to the tables it serves and has gone quiet".
- **Checks on every test.** The `dut` fixture's teardown fails the test if ycabled
  exited unexpectedly, logged a crash, or wrote a table, field or value outside the DB
  contract (`harness/contract.py`). Some values depend on the port's cable type, such
  as `active_side`: a number for active-standby, a state for active-active.
- **Stopping deletes nothing.** The contract allows no deletes besides the CLI's
  command keys, and `test_stop_keeps_tables` checks that the tables survive a stop
  ([HLD] C7). Python's `deinit()` would delete them, but never runs, because SIGTERM
  never completes (a known issue: `test_sigterm_exits_promptly`).

[Design](#design) describes each part of the suite.

## Writing tests

```python
from harness.topology import Topology

def test_toggle(dut):
    """A toggle that the cable rejects is answered unknown.

    Steps:
    1. Configure Ethernet0 as an active-standby port, whose cable rejects
       toggle_mux_to_tor_b().
    2. Start ycabled.
    3. Toggle Ethernet0 to standby, and expect state unknown.
    """
    dut.configure(Topology().active_standby("Ethernet0"))  # CONFIG_DB, files, fake cables
    dut.hw.port(1).script("toggle_mux_to_tor_b", value=False)
    dut.start()                                           # waits until ycabled is ready
    assert dut.toggle("Ethernet0", "standby")["state"] == "unknown"
```

Each test has a docstring: what the test checks and why, then `Cases:` for a parametrized
test, saying what its parameters vary, then the numbered `Steps:`, from the setup to what
it expects. A helper that carries out the steps of several tests, such as
`test_telemetry.py`'s `_telemetry_pass`, lists them in its own docstring.

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

- `slow`: takes a minute or more: waits for one of ycabled's 60 s timers (telemetry,
  thread checks), or sends thousands of requests (`test_scale.py`).
- `known_issue(reason)`: a behavior Python ycabled gets wrong. `reason` says what goes
  wrong, the test's docstring adds what a reader needs beyond it, and each run's summary
  lists the reasons (`-ra`). The test asserts the correct behavior and is a strict xfail
  on Python ycabled, so it starts failing when the issue is fixed, and it must pass on a
  new implementation. Only an `AssertionError` counts as the expected failure. The
  harness raises `harness.util.HarnessError` (a `RuntimeError`) when it cannot do what a
  test asked: the request helpers' waits (`toggle`, `probe`, `cli`,
  `monitor.wait_access`, ...), `start()`, the barriers. So when the issue is a missing
  answer, send with `wait=False` and assert the answer:
  `assert dut.answered("toggle", port, mark)`, or `assert dut.monitor.eventually(...)`,
  `harness.util.eventually(...)`.
- `python_only`: checks a mechanism of Python ycabled, skipped for other implementations.

## What the suite covers

| Function (HLD section) | Tests |
| --- | --- |
| Startup, port map, restart, signals, startup failures (3.2) | `test_lifecycle.py` |
| Active-standby toggle and probe, driver selection, VS and the mux simulator (3.3.1, 3.3.2, 3.4.2) | `test_active_standby.py` |
| Active-active toggles, forwarding state probe, channel, connectivity `failure`, TLS, GracefulRestart (3.3.3 to 3.3.6, 3.4.1) | `test_active_active.py` |
| Cable insertion and removal (3.3.7) | `test_insert_remove.py` |
| Telemetry (3.3.8) | `test_telemetry.py` |
| Every CLI command family and its error paths (3.3.9, 3.5.5) | `test_cli.py` |
| `XCVRD_LOG`, Loopback3, `grpc_secrets.json`, log compatibility, Redis connections (3.3.10, 3.4.1, C5, N5) | `test_config.py` |
| Multi-ASIC namespaces (F1) | `test_multi_asic.py` |
| Requests for one cable from different sources at the same time: toggles, insertion and removal, the CLI | `test_concurrency.py` |
| Many ports toggled many times: 100 active-standby ports, and 100 active-active ports with their peers, toggled together in 100 rounds; every answer, every cable and SoC call in order, no overlapping calls to a cable | `test_scale.py` |
| DB schema (3.5) | the DB contract, checked on every test |

Each known issue that a test can observe has a `known_issue` test, which explains it:
`grep -n -A3 'known_issue(' tests/*.py` lists them with their reasons. Section 3.7 of the
[HLD] analyzes each issue, with where it is in the code and the proposed fix, and names
its test.

## Other implementations

`BB_YCABLED_CMD` runs another binary with the same inputs. The suite expects it to:

- leave the address 127.0.0.2 to the suite: MONITOR tells the suite's redis commands
  from the daemon's by it;
- load vendor drivers through `sonic_y_cable` with the `PYTHONPATH` it is given, and the
  platform API through `sonic_platform`;
- log through `/dev/log`;
- take the platform from `PLATFORM` and `DEVICE_METADATA`, like `sonic_py_common`.

## Design

The suite has three parts. The tests (`tests/`) say what to set up, what to send and
what to expect. The harness (`conftest.py` and `harness/`, in the pytest process) stands
in for everything around ycabled and records what ycabled does. The code in `pylib/`
runs inside ycabled's process and passes the vendor driver's and the platform API's
calls to the harness.

### Principles

- **Only ycabled's interfaces.** Tests act and observe through redis, the files ycabled
  reads, signals, the SoCs' gRPC services, the vendor driver and platform API, and
  syslog. No test imports ycabled, so `BB_YCABLED_CMD` runs the same tests against
  another implementation.
- **Servers per session, daemon per test.** redis-server, the MONITOR recorder, the
  syslog sink and the fake hardware server start once per session (`bb_env`). Each test
  configures its own device and starts its own ycabled (`dut`). Its teardown leaves
  nothing behind: it kills ycabled, stops the test's SoCs and simulators, resets the
  fake hardware, flushes redis, and deletes the files the test wrote.
- **Marks, not resets.** The MONITOR and syslog records keep the whole session; the fake
  hardware's call log and the SoC doubles' RPC logs start empty in each test. A test
  reads what happened after a mark (`mark()`), such as what followed its last request.
  The DB contract coverage report is made from the session's MONITOR record.
- **Wait for events, not for time.** A request waits for ycabled's answer in the MONITOR
  stream. Startup waits until ycabled has subscribed to its tables and been quiet for
  0.3 s. Once ycabled has stopped, barriers make sure that its last redis commands and
  log lines are recorded: an `ECHO` token sent through redis, a datagram sent through
  the syslog socket. A test waits a fixed time only to check that something does not
  happen (`stays_true`).
- **A harness failure is not an assertion failure.** When the harness cannot do what a
  test asked, such as a wait that times out, it raises `HarnessError`. Tests check
  ycabled with `assert`. A known-issue test is a strict xfail that accepts only an
  `AssertionError`, so a harness error, a timeout or a hang cannot pass for the known
  issue.

### Module layout

```text
sonic-ycabled/blackbox/
├── conftest.py           pytest hooks and options, the shared fixtures, the reports
├── pytest.ini            collects tests/ only
├── Dockerfile, run.sh    the test image, and the script that runs the suite in it
├── harness/              stand-ins and recorders, in the pytest process
│   ├── env.py            the session: system setup, gRPC code, driver overlay, servers
│   ├── dut.py            one test's device: requests, the CLI protocol, teardown checks
│   ├── ycabled.py        the daemon's process: command, environment, start and stop
│   ├── cov_main.py       runs Python ycabled under coverage.py
│   ├── topology.py       the device's configuration, and what xcvrd publishes
│   ├── redisdb.py        redis-server, the suite's connections, table access
│   ├── monitor.py        the MONITOR recorder: commands, accesses, waits, barriers
│   ├── contract.py       the DB contract, its check and its coverage report
│   ├── fake_hw.py        fake cables, served to the fake driver and platform API
│   ├── soc.py            the gRPC SoC double, and Blackhole
│   ├── mux_simulator.py  the HTTP mux simulator of KVM testbeds
│   ├── syslog_sink.py    the syslog receiver /dev/log points at
│   ├── certs.py          certificates for the TLS cases
│   ├── expect.py         the rows ycabled should publish for a fake cable
│   └── util.py           DB ids, timestamp format, HarnessError, waits
├── pylib/                on ycabled's PYTHONPATH: runs in the daemon's process
│   ├── bb_hw_client.py   one call to the fake hardware
│   ├── bbfake/           fake vendor driver modules, copied into the driver overlay
│   └── sonic_platform/   fake platform API
├── tests/                the tests, one module per area of ycabled
├── bench/                benchmarks, run on request
└── report/               the reports of run.sh (not in git)
```

### Lifecycle

1. **Collection** (`conftest.py`). Without `BB_ENABLE=1`, `tests/` is not collected.
   With it, `pytest_configure` checks the prerequisites, and
   `pytest_collection_modifyitems` gives each test the time limit, turns `known_issue`
   into a strict xfail on Python ycabled, and skips `python_only` tests on other
   implementations.
2. **Session setup** (`bb_env`, `BlackboxEnv.start`). It empties the work directory,
   makes the system directories the suite's, points `/dev/log` at the syslog sink,
   generates the gRPC code from ycabled's proto, and builds the `sonic_y_cable` overlay.
   Then it starts redis-server, the MONITOR recorder, the syslog sink and the fake
   hardware server.
3. **Test** (`dut`). The `Dut` marks the MONITOR and syslog records.
   `configure(topology)` writes CONFIG_DB and `port_config.ini`, and adds a fake cable
   for every port. `start()` writes `TRANSCEIVER_INFO` for the cables present, starts
   ycabled and waits until it is ready. The test sends requests and asserts on the
   answers and the records.
4. **Teardown** (`Dut.teardown`). It collects the problems: ycabled exited unexpectedly,
   logged a crash, or wrote outside the DB contract, unless `tolerate_python_failure`
   allows it for a known issue. Then it cleans up, and the `dut` fixture fails the test
   with the problems.
5. **Session end** (`pytest_sessionfinish`). It writes the DB contract coverage report,
   and fails a run of the whole suite on Python ycabled that left an entry unexercised.
   With `--bb-coverage`, it also writes the code coverage report.

### Fixtures

| Fixture | Scope | Defined in | What it gives |
| --- | --- | --- | --- |
| `bb_env` | session | `conftest.py` | The started `BlackboxEnv`. Skips the test without `BB_ENABLE=1`. Stops the servers at the end of the session. |
| `dut` | test | `conftest.py` | A `Dut` named after the test. Its teardown fails the test with the problems it found. |
| `module_dut` | module | `conftest.py` | A `Dut` the tests of one module share, for cases that wait for a 60 s timer once and then check several things. |
| `pki` | test | `test_active_active.py`, `test_config.py` | A `Pki` in `/etc/sonic/bbcerts`, deleted at teardown. |
| `cable` | test | `test_cli.py`, `test_concurrency.py` | Active-standby `Ethernet0`, the cable under test, and `Ethernet4`, which keeps Python ycabled's request threads running. Returns the `PortModel` of `Ethernet0`. |
| `multi_asic` | test | `test_multi_asic.py` | Two front-end ASIC namespaces, each with its own redis-server, MONITOR recorder, database config, port config and mux port. Its teardown checks ycabled's accesses in the namespaces against the DB contract, and adds them to the coverage report. |
| `run` | module | `test_telemetry.py` | One telemetry pass over eight ports in different conditions (`PORTS`), recorded on `module_dut` and torn down at once; each test checks a part of the record. |

### conftest.py: hooks and options

| Hook or function | What it does |
| --- | --- |
| `pytest_ignore_collect` | Leaves `tests/` out without `BB_ENABLE=1`, so a `pytest` run of the unit tests is not affected. |
| `pytest_addoption` | The `--bb-*` options. |
| `pytest_configure` | Checks the prerequisites (`env.check_prerequisites`, and pytest-timeout unless `--bb-test-timeout` is 0) and registers the markers. |
| `pytest_collection_modifyitems` | Gives the suite's tests the time limit, and applies `known_issue` and `python_only`. Unit tests collected in the same session are left alone. |
| `pytest_deselected`, `_partial_selection`, `_full_contract_not_required` | Tell a run of the whole suite on Python ycabled, which must exercise every DB contract entry, from other runs, which only report the entries they missed. |
| `_coverage_target` | Where Python ycabled's coverage data goes: the suite's own report with `--bb-coverage`, otherwise pytest-cov's data file, so that it adds to the unit tests' coverage. |
| `pytest_sessionfinish`, `_contract_report`, `_coverage_report` | The reports at the end of the run. |
| `pytest_terminal_summary` | Says why the black-box tests did not run. |
| `BASE_ENV` | The environment when pytest started, before the unit tests of the same session change it. ycabled gets it, without pytest-cov's variables. |
| `ENABLED`, `BLACKBOX_DIR`, `TESTS_DIR` | Whether `BB_ENABLE` is 1; the suite's directory and its `tests/`. |

### harness/env.py: the session

| Name | What it does |
| --- | --- |
| `BlackboxEnv(workdir, base_env, coverage)` | The session's environment: `start()`, `stop()`, and the parts the tests use: `redis`, `dbs`, `monitor`, `syslog`, `hw`. `extra_accesses` holds what other recorders saw ycabled do, such as those of `multi_asic`, for the coverage report. |
| `BlackboxEnv.prepare_system()` | Makes `SYSTEM_DIRS` the suite's, points `/dev/log` at the syslog sink, and routes the dual ToR's VLAN (`topology.VLAN_V4`, and `VLAN_V6` if `lo` has IPv6) to `lo`, as root or with `sudo -n`. |
| `BlackboxEnv.generate_proto()` | Generates the Python gRPC code from ycabled's `linkmgr_grpc_driver.proto` into `gen/`. ycabled imports it, and so do the SoC doubles. |
| `BlackboxEnv.build_overlay()` | Copies the installed `sonic_y_cable`, adds `pylib/bbfake`, and appends `FAKE_MAPPING` to its `y_cable_vendor_mapping`. ycabled's `PYTHONPATH` has the copy first. |
| `FAKE_MAPPING` | The fake vendor's models: the fake driver, a module without a `YCable` class, and a module that does not exist. |
| `mapping_key(s)` | ycabled's normalization of vendor and model names. |
| `check_prerequisites()` | What keeps the suite from running here, or None: no redis-server, neither root nor sudo without a password, no `ip` (iproute2), no `NET_ADMIN` capability for the routes, or a `database_config.json` without the redis instance and database ids the suite uses. |
| `make_local(ip)` | Lets a SoC double listen on an address. The VLAN's routes do for IPv4; IPv6 binds only to an address of an interface, so an IPv6 address goes on `lo`. `Dut.soc` and `Dut.blackhole` call it. |
| `CoverageTarget` | Where Python ycabled's coverage data goes, and whether the suite reports it itself. |
| `BLACKBOX_DIR`, `SRC_DIR` | The suite's directory, and the sonic-ycabled tree whose ycabled it runs. |
| `SYSTEM_DIRS`, `DATABASE_CONFIG` | The directories outside the work directory that the suite writes to, besides `/dev/log`; the `database_config.json` that ycabled and the suite read. |

### harness/dut.py: one test's device

`Dut(env, name)` is what a test works with. Requests wait for ycabled's answer and
return it. With `wait=False` they do not wait: a test that wants to know whether ycabled
answered takes a mark first (`monitor.mark()`) and asks `answered`; `cli` returns that
mark itself.

| Member | What it does |
| --- | --- |
| `configure(topology)` | Applies a `Topology`: CONFIG_DB, `port_config.ini` and a fake cable for every port. |
| `soc(ip, tls, start)`, `blackhole(ip)` | Start a SoC double or a `Blackhole` for a SoC address, after `env.make_local(ip)`. |
| `mux_simulator(side)` | Starts a mux simulator and writes `/etc/sonic/mux_simulator.json` for it. |
| `write_file(path, content)`, `firmware_file(name, content)` | Write a file ycabled reads; it is deleted at teardown. |
| `start(ready, expect_mux, timeout, extra_env, publish_transceivers)` | Writes `TRANSCEIVER_INFO` for the cables present, starts ycabled, and waits until it is ready. |
| `wait_ready(expect_mux, timeout)`, `subscriptions(since)` | Ready: ycabled has subscribed to `TRANSCEIVER_INFO` (`BASE_SUBSCRIPTIONS`), and to the 18 tables of `MUX_SUBSCRIPTIONS`, the ones tests write, if the device has mux cables, then sent nothing for 0.3 s. A daemon that exits meanwhile raises with its output. |
| `stop(sigterm_timeout)` | SIGTERM as supervisord sends it, SIGKILL after `sigterm_timeout`, then the barriers. Returns a `StopResult`: return code, whether SIGTERM ended it, and how long it took. |
| `toggle(port, state)`, `peer_toggle(port, state)` | orchagent's `HW_MUX_CABLE_TABLE` and linkmgrd's `HW_FORWARDING_STATE_PEER`. Return the STATE_DB row ycabled wrote. |
| `probe(port)`, `fwd_probe(port)` | linkmgrd's `MUX_CABLE_COMMAND_TABLE` and `FORWARDING_STATE_COMMAND` probes. Return the response. |
| `answered(request, port, since, timeout)` | Whether ycabled answered a request sent with `wait=False`. `ANSWERS` says where each request is answered. |
| `insert(port, row)`, `remove(port)` | xcvrd's `TRANSCEIVER_INFO` for an inserted or removed cable, with the fake cable's presence to match. |
| `cli(cmd_table, value, port, args, timeout, wait)` | The CLI's table protocol: clear the port's command, argument, response and result entries, write the arguments and the command, then wait for the response and for the deletion of the command key. Returns a `CliResult`: the response row, the result row, and whether the command key was deleted. |
| `writes(since, **match)`, `logs(since, **match)` | ycabled's writes since it started and its log lines since the test started, or both since a mark. |
| `tolerate_python_failure(crash, exit, contract)` | For a known issue: on Python ycabled only, lets the teardown accept the crash, exit or contract violation the issue causes. |
| `crash_evidence()` | The crash markers (`CRASH_MARKERS`) in syslog or in the daemon's output since the test started. |
| `teardown()` | The checks and the cleanup of the lifecycle above. |
| `appl`, `config`, `state`, `hw`, `monitor`, `syslog` | The databases, the fake hardware and the recorders. |
| `topo`, `proc`, `procs`, `socs` | The test's topology, its ycabled processes, and its SoC doubles by address. |
| `CLI_COMMAND_FIELDS`, `CLI_SPECS` | The field each CLI command writes into its command table, and the command's argument, response and result tables, from `contract.CLI_TABLES`. |
| `FIRMWARE_DIR`, `GRPC_SECRETS`, `MUX_SIMULATOR_CONFIG` | Where ycabled reads firmware images, `grpc_secrets.json` and `mux_simulator.json`. The teardown removes them. |

### harness/ycabled.py and cov_main.py: the daemon's process

| Name | What it does |
| --- | --- |
| `implementation()` | `python`, or `custom` when `BB_YCABLED_CMD` is set. |
| `YcabledProcess(env, platform, coverage, extra_env)` | One run of the daemon. `start(name)` writes its output to `ycabled-<test>-<n>.log` in the work directory; `stop(sigterm_timeout)`, `kill()`, `running()`, `returncode()`, `wait_exit(timeout)`, `output()`. |
| `YcabledProcess.command()` | `BB_YCABLED_CMD`; else `cov_main.py` when measuring coverage; else Python ycabled's `main()` from this tree, with `argv[0]` `/usr/local/bin/ycabled`. |
| `YcabledProcess.process_env()` | The session's environment with `PYTHONPATH` set to the overlay, `pylib`, the generated gRPC code and this tree, `PLATFORM` from the topology, `BB_HW_SOCKET`, and the coverage variables. |
| `YcabledProcess.save_coverage()` | Makes Python ycabled save its coverage data before the suite kills it. |
| `COVERAGE_SAVE_PERIOD`, `HARNESS_DIR` | How often a running Python ycabled saves its coverage data, 3 s; where `cov_main.py` is. |
| `cov_main.py` | Starts coverage.py, then ycabled's `main()`. Python ycabled kills itself when a worker thread dies and does not exit on SIGTERM, so an exit hook is not enough: SIGUSR1 saves the data, and the suite sends it every 3 s and before it kills the process. A marker file says when the handler is in place. |

### harness/topology.py: the device's configuration

| Name | What it does |
| --- | --- |
| `Topology(nports, platform, hwsku, loopback3)` | A dual ToR with `nports` front panel ports: `Ethernet0`, `Ethernet4`, and so on, with indexes 1, 2, and so on. It is the lower ToR (`LOOPBACK3_LOWER`, read side 0) unless `loopback3` says otherwise. A test can change its attributes before `configure`: `mux`, `loopback3`, `grpcclient`, `xcvrd_log`, `device_metadata`, `transceiver_info` (the cables xcvrd has published; by default those present), `config_db_initialized` and `port_table`. |
| `active_standby(*names, **kw)`, `active_active(name, soc_ipv4, soc_ipv6, **kw)`, `add_mux(name, **kw)` | Add `MUX_CABLE` entries. A port that is not in the topology raises. |
| `apply(dbs, hw)`, `write_port_config()` | Write CONFIG_DB (`CONFIG_DB_INITIALIZED`, `DEVICE_METADATA`, `PORT`, `MUX_CABLE`, `LOOPBACK_INTERFACE`, `GRPCCLIENT`, `XCVRD_LOG`) and `port_config.ini`, and add a fake cable for every port. |
| `publish_transceivers(dbs, hw)` | Writes `TRANSCEIVER_INFO` as xcvrd does. |
| `cable_types()`, `index(name)` | Port name to cable type, for the DB contract; port name to index. |
| `MuxPort` | One `MUX_CABLE` entry; `fields()` is its CONFIG_DB row. `configured=False` leaves it out of CONFIG_DB. |
| `transceiver_info_row(model)` | The `TRANSCEIVER_INFO` row of a fake cable. |
| `PLATFORM`, `VS_PLATFORM`, `HWSKU`, `CONTAINER_PLATFORM_DIR` | The fake platform and HwSKU, the KVM platform, and where `port_config.ini` goes. |
| `LOOPBACK3_UPPER`, `LOOPBACK3_LOWER`, `LOOPBACK3_UPPER_NIC`, `LOOPBACK3_LOWER_NIC` | The Loopback3 addresses that ycabled maps to read side 1, the upper ToR, and to read side 0, the lower ToR: Python ycabled's `LOOPBACK_INTERFACE_T0` and `LOOPBACK_INTERFACE_LT0` pairs ([HLD] section 3.4.1). The `_NIC` pair is what sonic-mgmt gives dual ToR testbeds, 10.1.0.38 to the upper ToR. |
| `VLAN_V4`, `VLAN_V6`, `soc_ip(port, ipv6)` | sonic-mgmt's dual ToR VLAN, Vlan1000, 192.168.0.1/21 and fc02:1000::1/64, and the SoC address it gives a port's active-active cable: the n-th port (`Ethernet<4n>`) has its server at the VLAN's address + 2n + 1 and its SoC at + 2n + 2, so 192.168.0.3 for `Ethernet0` (sonic-mgmt's `generate_mux_cable_facts`). sonic-mgmt gives SoCs no IPv6 address; the suite's, for `soc_ipv6`, has the same host number, fc02:1000::3 for `Ethernet0`. |

### harness/redisdb.py, monitor.py and contract.py: the databases

| Name | What it does |
| --- | --- |
| `RedisServer(workdir, unix_socket, port)` | redis-server with keyspace notifications, without persistence, and with protected mode off so that the suite can connect from 127.0.0.2. `start()`, `stop()`, `client(db)`, `flushall()`. |
| `harness_client(db, decode_responses, port)`, `HarnessConnection` | Connections from 127.0.0.2, so that MONITOR tells the suite's commands from ycabled's. |
| `Db` | One database, by table and key: `set` merges fields as swsscommon's `Table.set` does; `get`, `field`, `exists`, `delete`, `keys`, `table`. |
| `Dbs` | `appl`, `config` and `state`; `snapshot()` dumps them for diagnostics. |
| `UNIX_SOCKET`, `TCP_PORT`, `HARNESS_ADDRESS` | Where redis-server listens, `/var/run/redis/redis.sock` and port 6379, and the suite's source address, 127.0.0.2. |
| `Monitor(port)` | A MONITOR connection and a thread that reads it. `mark()`; `ops(since)`: ycabled's commands (`Op`); `accesses(since, kind, db, table, key, field)`: the table-level accesses (`Access`), of kind `read`, `write`, `hdel`, `delete`, `set`, `subscribe` or `other:<command>`. |
| `Monitor.wait_access(since, timeout, **match)`, `Monitor.eventually(since, timeout, **match)` | Wait for a matching access: as a step of the test (raises `WaitTimeout`), or for an `assert` (returns an empty list). |
| `Monitor.wait_settled(quiet, timeout)`, `Monitor.barrier(timeout)` | Wait until ycabled has sent nothing for `quiet` seconds; wait until every command redis ran before the call is recorded. |
| `Monitor.error`, `Monitor.check()` | Why the reader stopped. Once it has, every observation raises instead of answering from an incomplete record. |
| `parse_line`, `accesses(op)`, `split_key` | A MONITOR line to a command, a command to accesses, a redis key to table and key. |
| `READ_CMDS`, `IGNORED_CMDS` | The commands that count as reads, and those that make no access, such as `SELECT` and `PING`. |
| `CONTRACT` | The 200 entries (`Entry`: database, table, kind, field, value pattern, note) that ycabled's accesses must match. `_entries()` builds them from `MUX_CABLE_INFO_ACTIVE_STANDBY`, `MUX_CABLE_INFO_ACTIVE_ACTIVE`, `MUX_CABLE_STATIC_INFO` and `CLI_TABLES`; a value pattern can depend on the port's cable type. |
| `CLI_TABLES` | Each CLI command's command, argument, response and result tables, with their fields and values. `Dut.cli` uses them too. |
| `match(access)`, `violations(accesses, cable_types)` | The entries an access falls under; the writes, deletes, values and commands the contract does not allow, which each test's teardown checks. |
| `coverage(accesses)`, `coverage_report(accesses)` | How often the session exercised each entry, for `db-contract-coverage.md`. |
| `ANY`, `STATE`, `READ_SIDE`, `ACTIVE_SIDE`, `UP_DOWN`, `NUM_OR_NA`, `COUNT`, `CLI_STATUS`, `FIRMWARE_FIELDS` | The entries' value patterns, of which `READ_SIDE` and `ACTIVE_SIDE` depend on the cable type, and the firmware version fields. |

### harness/fake_hw.py: the cables

| Name | What it does |
| --- | --- |
| `FakeHardware(socket_path)` | A server on a unix socket that the fake driver and platform API call, one JSON line per call. The request's `op` is `driver` (a driver method), `platform` (presence or transceiver info), `attr` (`download_firmware_status`), `init` (the driver's constructor) or `platform_init` (`Platform()`). |
| `FakeHardware.add_port(index)`, `port(index)`, `reset()` | Add a cable, get its `PortModel`, and forget every cable and call, releasing held calls. |
| `FakeHardware.mark()`, `calls(since, port, method, op)` | The call log (`Call`). |
| `FakeHardware.overlaps(since, port)` | Driver calls that started while another call to the same cable was still running (`Overlap`). |
| `FakeHardware.platform_init_error` | Makes `Platform()` raise, for the startup failure path. |
| `PortModel(index)` | One cable: presence, transceiver info, vendor, read side, mux direction, switch counts, eye heights, firmware versions and so on. By default it is present, read from ToR A, with the mux toward ToR A, so ycabled publishes it active. Its `m_<method>` methods, below, are the driver's default behavior. |
| `PortModel.run(op, method, args)` | Serves one call: presence or transceiver info for `platform` (no transceiver info when the cable is absent), an attribute for `attr`, the `initialized` flag for `init`, and `m_<method>` for `driver`. A driver method without one raises `NotImplementedError`. |
| `m_get_vendor`, `m_get_read_side`, `m_get_mux_direction`, `m_get_active_linked_tor_side` | Return `vendor`, `read_side` and `mux_direction`. The active linked side is `active_linked_side` when a test sets it, otherwise the mux direction. |
| `m_toggle_mux_to_tor_a`, `m_toggle_mux_to_tor_b` | Set the mux direction, clear `active_linked_side`, count a manual switch, and return True. |
| `m_set_switching_mode`, `m_set_anlt`, `m_set_fec_mode`, `m_enable_prbs_mode`, `m_disable_prbs_mode`, `m_enable_loopback_mode`, `m_disable_loopback_mode`, `m_reset` | Record the setting in the model (`switching_mode`, `anlt`, `fec_mode`, `prbs`, `loopback`) and return True. `m_reset` only returns True. |
| `m_download_firmware`, `m_activate_firmware`, `m_rollback_firmware` | Return 0, success. The download also sets the download status back to not in progress. |
| `m_get_switch_count_total`, `m_get_eye_heights`, `m_is_link_active`, `m_get_firmware_version`, `m_get_local_temperature`, `m_get_local_voltage`, `m_get_nic_temperature`, `m_get_nic_voltage`, `m_get_target_cursor_values`, `m_get_switching_mode`, `m_get_anlt`, `m_get_fec_mode`, `m_get_speed`, `m_get_event_log`, `m_get_ber_info`, `m_get_fec_stats`, `m_get_pcs_stats`, `m_get_alive_status`, `m_health_check`, `m_reset_cause`, `m_operation_time`, `m_debug_dump_registers`, `m_queue_info` | Return the model's value for their arguments (counter type, target, lane), with lists and dicts copied. |
| `PortModel.script(method, value, raises, delay, func, times, block)` | Overrides a method: return a value, raise an exception (`"Type: message"`), sleep first, or compute the result with `func(model, args)`. `block=True` returns a gate that holds the calls until it is set, for 120 s at most; a held `download_firmware` also shows the download in progress. `times` limits the calls the script applies to. `unscript(method)` removes it. |
| `Script` | One override. |
| `TARGET_NIC`, `TARGET_TOR_A`, `TARGET_TOR_B`, `SWITCHING_MODE_MANUAL`, `SWITCHING_MODE_AUTO`, `FIRMWARE_DOWNLOAD_STATUS_NOT_INITIATED_OR_FINISHED`, `FIRMWARE_DOWNLOAD_STATUS_INPROGRESS`, `FIRMWARE_DOWNLOAD_STATUS_FAILED` | The values of the Y-cable driver API: targets 0, 1 and 2, the switching modes, and the download states. |
| `VENDOR`, `MODEL` | The fake vendor and model, which the overlay's vendor mapping maps to the fake driver. |

### harness/soc.py and mux_simulator.py: the SoCs and the KVM mux

| Name | What it does |
| --- | --- |
| `Soc(ip, tls)` | A gRPC server on `<ip>:50075` serving `DualToRActive` and `GracefulRestart` from ycabled's proto, with TLS when `tls` is given (`Pki.server_tls`). `start()`, `stop()`, `running`. |
| `Soc.admin`, `link`, `oper`, `version` | What the SoC reports for port ids 0 and 1, the lower and the upper ToR, and its server version. By default port 0 is active and port 1 standby, so the default device, the lower ToR, is active. |
| `Soc.gr_responses`, `gr_hold` | What each GracefulRestart stream sends, and whether it stays open afterwards. |
| `Soc.script(rpc, delay, code, reply, times)`, `unscript(rpc)` | Make an RPC wait, fail with a gRPC status code, or answer with a reply the test builds. |
| `Soc.mark()`, `rpcs(name, since)`, `wait_rpcs(name, count, since, timeout)` | The RPC log (`Rpc`). |
| `RpcScript`, `format_target(ip)`, `GRPC_PORT` | One RPC override; the address a SoC listens on, `<ip>:50075` or `[<ipv6>]:50075`. |
| `_DualToRActive`, `_GracefulRestart` | The servicers. `SetAdminForwardingPortState` rejects port ids other than 0 and 1 with `INVALID_ARGUMENT` rather than use them as list indexes. |
| `SERVER_OPTIONS` | Let ycabled's keepalive pings through, which grpc's default server policy answers with GOAWAY. |
| `Blackhole(ip)` | Accepts TCP connections on `<ip>:50075` and never answers. |
| `MuxSimulator(vm_set)` | The HTTP API of sonic-mgmt's mux simulator that the real `y_cable_simulated` driver uses on KVM. `start()`, `stop()`, `port`; `status(index)`, `set_active_side(index, side)`, and `requests`, the log; `config(side)`: the content of `/etc/sonic/mux_simulator.json`. `UPPER_TOR` and `LOWER_TOR` name the sides. |

### harness/syslog_sink.py, certs.py, expect.py and util.py

| Name | What it does |
| --- | --- |
| `SyslogSink(path)` | A datagram socket that `/dev/log` points at. It parses each line into a `Message` (severity, identity, pid, text). `mark()`, `messages(since, severity, contains, regex)`, `eventually(...)`, `wait_for(...)`, `barrier(timeout)`. |
| `DEV_LOG`, `SEVERITIES` | `/dev/log`, and the syslog severities by number. |
| `Pki(directory, soc_ips)` | A CA, a SoC server certificate for `SOC_NAME` and the SoC addresses, a ToR client certificate and an unrelated CA, written as `ca.crt`, `client.crt`, `client.key` and `other-ca.crt`. `server_tls(require_client_auth)` is the `tls` argument of `Soc`. |
| `mux_cable_info(model)`, `mux_cable_static_info(model)`, `firmware_versions(model)` | The `MUX_CABLE_INFO`, `MUX_CABLE_STATIC_INFO` and `XCVRD_SHOW_FW_RES` rows ycabled should publish for a `PortModel`. |
| `PLACEHOLDER_INFO`, `sides(read_side)`, `subset(row, expected)` | The `MUX_CABLE_INFO` of a port without a usable cable; which targets are self and peer for a read side; the fields that differ, for assertion messages. |
| `HarnessError`, `WaitTimeout` | What the harness raises when it cannot do what a test asked. |
| `wait_until(predicate, timeout, interval, message)`, `eventually(predicate, timeout, interval)`, `stays_true(predicate, duration, interval)` | A wait that is a step of the test (raises); a wait for an `assert` (returns None); a check that something holds for a while. |
| `clear_dir(path)`, `APPL_DB`, `CONFIG_DB`, `STATE_DB`, `DB_IDS`, `DB_NAMES`, `SEPARATORS`, `TIMESTAMP_RE` | Empties a directory but keeps it; the SONiC database ids and key separators; the format of ycabled's timestamps. |

### pylib: in ycabled's process

| Name | What it does |
| --- | --- |
| `bb_hw_client.call(op, port, method, args)` | Sends one call to the fake hardware over the socket in `BB_HW_SOCKET`. Returns the value, or raises the exception the server names: a built-in exception class, otherwise `RuntimeError`. |
| `bbfake/y_cable_bbfake.py` | `YCable`, the fake vendor driver: a `YCableBase` whose constructor and 40 methods (`METHODS`) call the fake hardware. `download_firmware_status` is read from the fake hardware too. |
| `bbfake/y_cable_noclass.py` | A vendor module without a `YCable` class, for the driver-load error path. |
| `sonic_platform/` | `Platform`, whose constructor calls `platform_init`; `Chassis.get_sfp(index)`; and `Sfp.get_presence()` and `get_transceiver_info()`, which call the fake hardware. |

### Test modules

Each module covers one area of ycabled; [What the suite covers](#what-the-suite-covers)
lists them. Most tests use `dut` and build their own `Topology`, since what exists at
startup is often what the test is about; the modules add small helpers for the setups
they repeat. Every SoC is at the address sonic-mgmt gives it, `soc_ip(port)`; module
constants name `Ethernet0`'s, 192.168.0.3 (`SOC`, and `SOC_V6`, fc02:1000::3, in
`test_active_active.py`), and where `pki` writes (`CERT_DIR`).

| Module | Helpers |
| --- | --- |
| `test_lifecycle.py` | `AS_ROW_KEYS`: the STATE_DB tables that stopping ycabled must keep. |
| `test_active_standby.py` | `two_cables(dut, **kw)`: `Ethernet0` under test, with `kw` set on its `PortModel`, and a healthy `Ethernet4`, so that Python ycabled serves requests even when `Ethernet0` does not initialize. `UNKNOWN_TOGGLE_RESULT`: the `HW_MUX_CABLE_TABLE` row of a port whose state is unknown, which a toggle answers and startup publishes for a cable it cannot read. |
| `test_active_active.py` | `aa(dut, loopback3, soc, **kw)`: an active-active `Ethernet0` and its SoC at 192.168.0.3. `pki` and `tls_config(dut, pki, ...)`: the `GRPCCLIENT` rows of the TLS cases. `_reply_wrong_port`, `_reply_empty`, `_reply_short`: malformed replies for `Soc.script`. `READ_SIDES`: each Loopback3 address with its read side. |
| `test_insert_remove.py` | `two_cables(dut)`. `remove_ethernet4(dut)`: a removal that ycabled handles after the events written before it, used as a barrier before checking that something did not happen. |
| `test_telemetry.py` | Marked `slow`. `run` and `_telemetry_pass(dut)`: one pass over `PORTS`, whose comments say each port's condition. `LAST_KNOWN`: the firmware versions left in STATE_DB for the cable being upgraded. `_cursors`, `_short_reply`: failing driver and SoC answers. `PASS_TIMEOUT`: how long `run` waits for the pass, 90 s. |
| `test_cli.py` | `cable`. `no_driver(dut)`: transceiver info that no vendor mapping knows. `calls(dut, mark, method)`: `Ethernet0`'s driver calls. `_firmware_tor_b_fails`. Most tests are parametrized over a command family's variants and failures. |
| `test_config.py` | `pki`. `secrets(pki, **config)`: the content of `grpc_secrets.json`. `set_log_verbosity(dut, key, level)`, `probe_logs_debug(dut)`. |
| `test_multi_asic.py` | `multi_asic`. `_database_config(base, port, unix_socket)`: a namespace's `database_config.json`, pointing at its redis-server. `ENV_CHECK`: what a daemon that supports multi-ASIC does first, run in ycabled's environment to check the setup itself. `NAMESPACES`: each namespace's mux port, port index and redis port. `SONIC_DB_DIR`: where the database configs go. |
| `test_scale.py` | Marked `slow`. `send(dut, requests)`: a round's requests in one transaction, as when many ports switch over at once. `answers(dut, since, table, ports)`: each port's answers after a mark, once every port has one. `check_round(got, r, table, state)`: exactly one answer per port, with the requested state. `round_state(r)`, `opposite(state)`, `port_names(n)`; `PORTS`, `ROUNDS`, `ROUND_TIMEOUT`. |
| `test_concurrency.py` | `cable`. `hold_cli_call(dut, cable)`: a CLI command held in its first call to the cable. `remove_and_wait(dut)`, `state_writes`, `removed`. `maybe(predicate)` and `HELD`: how long a test gives ycabled to act on a second request while the first call is held. |

### Benchmarks

`bench/test_bench.py` measures the implementation under test: memory, threads, Redis
connections and CPU at steady state with 24 active-standby or 32 active-active ports,
startup with unreachable SoCs, toggle latency with hanging SoCs or cables, probe
latency, toggle throughput, 1000 toggles with ycabled's CPU, memory, threads and use of
redis during them, and a telemetry pass with faulty SoCs. It is not part of the suite
(`pytest.ini` collects `tests/` only). Run it once per implementation, with
`run.sh bench/test_bench.py`, and the second time with `BB_YCABLED_CMD` set. Each run
writes `bench-<implementation>.json` to the report directory, and
`bench/compare.py <report dir>` puts two runs side by side. The numbers in
[HLD] section 5.3 come from it.

| Name | What it does |
| --- | --- |
| `test_steady_state_active_standby`, `test_steady_state_active_active` | 24 active-standby ports, or 32 active-active ports with a SoC each: memory, threads and Redis connections after startup, and CPU over 65 s that include a telemetry pass. |
| `test_startup_with_unreachable_socs` | 8 or 32 active-active ports whose SoCs accept TCP and never answer: the time from ycabled's first DB access to every port's first state (N3). |
| `test_toggle_latency_with_hanging_socs` | 8 active-active ports toggled at once, the SoCs of 4 of them hanging: the slowest answers of the healthy and the hanging ports (N1). |
| `test_toggle_latency_with_hanging_cables` | 8 active-standby ports toggled at once, the toggles of 4 of them taking 3 s: the slowest answers of the healthy and the hanging ports (N1). |
| `test_probe_latency_active_standby` | 200 probes in a row: median, p99 and maximum latency. |
| `test_thousand_toggles_burst`, `test_thousand_toggles_sequential` | 1000 toggles of active-standby or active-active ports: 100 ports toggled together in 10 rounds, or one port toggled 1000 times, each toggle sent once the previous one is answered. The time on redis's clock, and ycabled's CPU time, memory, threads and redis commands during the toggles. |
| `test_toggle_throughput_active_standby` | 24 active-standby ports toggled together, 10 rounds, timed on redis's clock. |
| `test_telemetry_pass_faulty_socs` | The first telemetry pass of 32 active-active ports whose SoCs are blackholed or hang: the time from its first `MUX_CABLE_INFO` write to its last. |
| `report`, `record(scenario, **values)`, `RESULTS` | Collect each scenario's numbers, and write `bench-<implementation>.json` when the module is done. |
| `steady_state(dut, scenario, startup)`, `proc_stats(pid)`, `redis_connections(dut)` | The steady-state measurement: RSS, PSS, peak RSS, threads and CPU time from `/proc`, and ycabled's Redis connections. |
| `resources(dut, scenario, toggles)`, `_peak_threads(pid, stop, peak)` | Measure a block: ycabled's CPU time and its share of the block's time, RSS, PSS and peak RSS, the peak thread count sampled every 20 ms, its Redis connections, and its Redis commands per toggle. |
| `toggle_device(dut, cable_type, n)`, `TOGGLES` | The device of the toggle scenarios: `n` ports of one cable type, each active-active one with a SoC, and telemetry off so that only the toggles are measured. |
| `first_writes(dut, since, table, field, keys)`, `request_ts(dut, mark, key)` | Times from the MONITOR record: the first write of a field to each key, and when the suite's request reached redis. |
| `ports(n)`, `impl_name()` | Port names; `python`, `rust` or the command's name, for the results file. |
| `CLK_TCK`, `IDLE_WINDOW`, `TELEMETRY_RPCS` | Clock ticks per second, for CPU time; the 65 s idle window; the four RPCs of an active-active telemetry pass. |
| `compare.py`: `main(argv)`, `load(path)` | Put two results files side by side in a Markdown table; read one. |

[HLD]: https://github.com/sonic-net/SONiC/blob/master/doc/dualtor/ycabled_to_rust_hld.md
