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
  (issues 26 to 29 of the HLD) cannot happen.
- **Telemetry** (every 60 s per port) runs in a task beside the actor, so a
  toggle never waits for a whole pass. Calls to an active-standby cable take
  the port's call lock, one at a time, so a toggle waits for at most one
  telemetry call. Active-standby passes run one at a time (shared I2C
  buses), active-active ones 16 at a time.
- **Databases** (`src/db.rs`): redis directly, with swsscommon's conventions:
  a row is one `HSET` of all its fields, subscriptions are swsscommon's
  `SubscriberStateTable` (keyspace notifications, then the existing rows).
  Per namespace, one connection per database and two for the
  subscriptions: 5, whatever the number of ports (Python: 125 with 24
  active-standby ports, 199 with 32 active-active ports).
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
  logged.
- **CLI** (`src/cli.rs`): the 13 `XCVRD_*_CMD` tables of `show/config
  muxcable`, answered as Python answers them. A firmware download runs in a
  task of its own, so the port keeps working meanwhile.
- **Logging** (`src/logger.rs`): syslog through `/dev/log`, identity
  `ycable` whatever the drivers do with `openlog`.

The known issues of Python ycabled (HLD section 3.8) that a black-box test
reproduces are fixed; see the HLD for the list, and section 4.5 for the
deliberate differences.

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
image (cargo registry in the `ycabled-rs-cargo` volume). The binary is
`target/release/ycabled-rs`.

## Run

`ycabled-rs` takes no arguments. Like Python ycabled it reads
`/var/run/redis/sonic-db/database_config.json` (and `database_global.json`
on multi-ASIC platforms), `PLATFORM` and CONFIG_DB `DEVICE_METADATA`, and
loads vendor drivers through `sonic_y_cable` on the Python path. SIGTERM and
SIGINT exit within 2 s and keep the tables; SIGHUP is ignored.

## Tests

- **Unit tests** (127): the actors and the CLI against an in-memory
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
| 24 active-standby ports | RSS / PSS | 50.3 / 42.0 MB | 38.2 / 33.6 MB |
| | Threads | 5 | 3 |
| | Redis connections | 125 | 5 |
| | CPU over 65 s, one telemetry pass | 0.14% | 0.15% |
| 32 active-active ports | RSS / PSS | 58.8 / 48.6 MB | 12.2 / 10.7 MB |
| | Threads | 61 | 3 |
| | Redis connections | 199 | 5 |
| | CPU over 65 s, one telemetry pass | 4.31% | 0.12% |
| Startup, SoCs never answer | 8 / 32 ports | 4.16 / 16.30 s | 0.51 / 0.52 s |
| 8 ports toggled, 4 SoCs hang | Healthy ports, slowest answer | 2021 ms | 6.6 ms |
| Active-standby probe | Median / p99 | 1.65 / 2.12 ms | 1.71 / 2.23 ms |
| 24 active-standby ports toggled together | Toggles per second | 936 | 1169 |
| Telemetry pass, 32 active-active ports | SoCs hang / blackholed | 62.3 / 0.04 s | 2.0 / 2.0 s |
