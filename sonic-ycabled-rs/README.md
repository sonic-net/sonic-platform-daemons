# ycabled-rs

The Y-cable daemon of SONiC dual ToR, in Rust: a drop-in replacement for
Python ycabled (`sonic-ycabled`), as designed in
[ycabled_to_rust_hld.md](https://github.com/sonic-net/SONiC/blob/master/doc/dualtor/ycabled_to_rust_hld.md).
It serves the same tables with the same fields and values, so orchagent,
linkmgrd, xcvrd and `show/config muxcable` work with either.

## Design in short

- **One actor per mux port** (`src/actor/`): a tokio task that handles the
  port's requests one at a time and in order: initialization, cable
  insertion and removal, toggles, probes, CLI commands. Ports run at the
  same time, so a slow SoC or cable delays only its own port (N1), ports
  initialize in parallel (N3), and the cross-thread races of Python ycabled
  (issues 26 to 29 of the HLD) cannot happen. No request is dropped: a port
  that cannot keep up queues them, and a backlog of 256 is logged. Startup
  waits at most 30 s for the ports to initialize before it subscribes, so a
  driver call that never returns holds up only its own port.
- **Telemetry** (every 60 s per port) runs in a task beside the actor, so a
  toggle never waits for a whole pass. Calls to an active-standby cable,
  presence checks and loading and unloading its driver included, take the
  port's call lock: a request holds it while it runs, a telemetry pass for
  one call at a time, so a toggle waits for at most one telemetry call.
  Active-standby telemetry calls also run one at a time across the ports
  (shared I2C buses). A call takes the bus only once it holds its port's
  lock, so a port stuck in a request holds up only its own pass.
  Active-active passes run 16 at a time.
- **Databases** (`src/db.rs`): redis directly, with swsscommon's conventions:
  a row is one `HSET` of all its fields, subscriptions are swsscommon's
  `SubscriberStateTable` (keyspace notifications, then the existing rows).
  A namespace's subscriptions share one connection, and their events are
  routed in the order redis delivered them, so a port's requests keep their
  order across tables. Per namespace, that and one connection per database
  make 4, whatever the number of ports (Python: 125 with 24 active-standby
  ports, 199 with 32 active-active ports).
- **Active-active** (`src/grpc.rs`): tonic, one lazily connected channel per
  SoC, 0.5 s deadlines, keepalive 4 s/8 s, TLS from GRPCCLIENT (rustls). A
  connector wrapper turns connection events into gRPC connectivity states,
  so `FORWARDING_STATE_RESPONSE` gets `response=failure` when the channel
  fails, as linkmgrd expects. GracefulRestart subscriptions as in Python.
  No Python on this path.
- **Active-standby** (`src/bridge.rs`, `python/ycabled_bridge.py`): the
  vendor drivers stay Python. An embedded CPython (PyO3), started only if an
  active-standby port needs it, runs a bridge module that owns the platform
  API and the driver objects; Rust calls it with plain values. Driver
  selection is Python ycabled's. A driver call still running after 5 s is
  logged. Calls run on blocking threads started when needed; a port has at
  most two in flight besides firmware downloads, so calls that never return
  on some ports leave threads for the others.
- **CLI** (`src/cli.rs`): the 13 `XCVRD_*_CMD` tables of `show/config
  muxcable`, answered as Python answers them. A firmware download runs in a
  task of its own, so the port keeps working meanwhile.
- **Logging** (`src/logger.rs`): syslog through `/dev/log`, identity
  `ycable` whatever the drivers do with `openlog`.

The known issues of Python ycabled (HLD section 3.8) that a black-box test
reproduces are fixed; see the HLD for the list, and section 4.5 for the
deliberate differences.

Limitations (HLD section 4.8):

- Active-standby ports need the platform API (`sonic_platform`). The legacy
  `sfputil` plugin, which Python ycabled falls back to when the chassis
  cannot be loaded, is not supported: ycabled-rs exits.
- Like Python ycabled (`daemon_base.db_connect`), ycabled-rs reaches redis
  over TCP, at each namespace's host and port in its DB config.
- A Python driver call cannot be cancelled. One that holds the GIL without
  returning stops every active-standby port, as it does in Python.

## Build

ycabled-rs links the libpython of the image it runs in, so it is built in an
image derived from it (`Dockerfile`): docker-sonic-vs (Debian bookworm,
Python 3.11) for the black-box suite, docker-pmon (trixie, Python 3.13) for a
DUT.

```bash
sonic-ycabled-rs/build.sh                          # cargo build --release, in ycabled-rs-build:latest
YCABLED_RS_BASE_IMAGE=docker-sonic-vs:latest YCABLED_RS_BUILD_IMAGE=ycabled-rs-build:vs \
    sonic-ycabled-rs/build.sh cargo build --release
```

`build.sh <command>` runs any command in the crate directory in the build
image, as the calling user. The image is rebuilt from
`YCABLED_RS_BASE_IMAGE` each time (from docker's cache unless the base
changed), and the cargo registry persists in the `ycabled-rs-cargo-<uid>`
volume (`YCABLED_RS_CARGO_VOLUME`). The binary is
`target/release/ycabled-rs`.

## Run

`ycabled-rs` takes no arguments. Like Python ycabled it reads
`/var/run/redis/sonic-db/database_config.json` (and `database_global.json`
on multi-ASIC platforms), `PLATFORM` and CONFIG_DB `DEVICE_METADATA`, and
loads vendor drivers through `sonic_y_cable` on the Python path. SIGTERM and
SIGINT exit within 2 s and keep the tables; SIGHUP is ignored.

## Tests

- **Unit tests** (139): the actors and the CLI against an in-memory
  database and mocks of the cables and SoCs (`src/testing.rs`), the redis
  layer against a private `redis-server`, the gRPC client against an
  in-process SoC, the bridge in the embedded interpreter with fake drivers.
  They cover the behaviors the Python unit tests cover;
  [doc/python-ut-coverage.md](doc/python-ut-coverage.md) maps them.

  ```bash
  sonic-ycabled-rs/build.sh cargo test --release --lib
  ```

- **Black-box tests** (`sonic-ycabled/blackbox`): the suite Python ycabled
  passes, run against ycabled-rs. Known-issue tests are normal tests for it,
  and the two tests of Python-only mechanisms are skipped. All 334 others
  pass, in about 10 minutes (Python: about 14).

  ```bash
  cd sonic-ycabled/blackbox
  BB_YCABLED_CMD=/src/sonic-ycabled-rs/target/release/ycabled-rs ./run.sh
  ```

## Benchmark

`sonic-ycabled/blackbox/bench` measures either implementation on the
suite's fakes: memory, threads, Redis connections and idle CPU, startup and
toggle latency with SoCs that do not answer, probe latency, toggle
throughput, the telemetry pass.

```bash
cd sonic-ycabled/blackbox
BB_REPORT_DIR=/tmp/bench ./run.sh bench/test_bench.py
BB_REPORT_DIR=/tmp/bench BB_YCABLED_CMD=/src/sonic-ycabled-rs/target/release/ycabled-rs ./run.sh bench/test_bench.py
bench/compare.py /tmp/bench
```

Results, one run after the other on a shared 16-core host (HLD section 5.3
explains them). They compare the daemons on the same fakes, not hardware.

| Scenario | Metric | Python | Rust |
| --- | --- | ---: | ---: |
| 24 active-standby ports | RSS / PSS | 50.3 / 42.0 MB | 38.3 / 33.8 MB |
| | Threads | 5 | 3 |
| | Redis connections | 125 | 4 |
| | CPU over 65 s, one telemetry pass | 0.14% | 0.14% |
| 32 active-active ports | RSS / PSS | 58.8 / 48.6 MB | 12.1 / 10.6 MB |
| | Threads | 61 | 3 |
| | Redis connections | 199 | 4 |
| | CPU over 65 s, one telemetry pass | 4.31% | 0.14% |
| Startup, SoCs never answer | 8 / 32 ports | 4.16 / 16.30 s | 0.51 / 0.52 s |
| 8 ports toggled, 4 SoCs hang | Healthy ports, slowest answer | 2021 ms | 6.9 ms |
| Active-standby probe | Median / p99 | 1.65 / 2.12 ms | 1.88 / 2.55 ms |
| 24 active-standby ports toggled together | Toggles per second | 936 | 1063 |
| Telemetry pass, 32 active-active ports | SoCs hang / blackholed | 62.3 / 0.04 s | 2.0 / 2.0 s |
