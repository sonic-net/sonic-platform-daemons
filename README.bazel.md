# Bazel installation interfaces

Use Bazel 8.5.1 and the registries configured in `.bazelrc`. The published `sonic-build-infra@0.0.39-81476192bf2cdb70bb646e85935c5d698b6de8a0` provides explicit executable payload selection and Python script normalization. Normal builds consume this fixed registry version without an infrastructure source override.

```sh
bazel build //:daemons.runtime //:daemons.runtime_metadata
```

Each original Python distribution exports `//:sonic-<daemon>.runtime` and `//:sonic-<daemon>.runtime_metadata`. The daemon names are the original setup distributions: `ledd`, `pcied`, `psud`, `syseepromd`, `thermalctld`, `sensormond`, `xcvrd`, `ycabled`, `chassisd`, `stormond` and `bmcctld`. The first target delivers one uncompressed installation TAR and `PythonRuntimeInfo`; the second delivers its generated Python runtime JSON. The aggregate targets collect all eleven distributions without repacking their TARs.

The PMON integration in `sonic-buildimage` consumes `@sonic_platform_daemons//:daemons.runtime` through `dockers/docker-platform-monitor/bazel/BUILD.bazel` and its Python runtime plan. The image consumer selects the registered source revision and resolves its runtime dependencies.

The installation layout follows the Python 3.13 SONiC image: packages and dist-info under `/usr/local/lib/python3.13/dist-packages`, original scripts and console entry points under `/usr/local/bin`, and `/usr/bin/python3` shebangs. Package lists, versions, dependencies, extras, entry points and descriptive metadata follow each original `setup.py`. This includes the `tests` packages that the original setup declarations install. No wheel is built or unpacked on this production path. These pure Python distributions use the same installation TAR in runtime and debug images; there is no ELF debug-symbol target.

`sonic-ycabled` declares its original proto and compiles the matching BCR gRPC 1.71.0 generator library and protobuf 29.0 compiler. The small plugin entry point supplies the same version configuration as grpcio-tools. APT provides different versions from the original Make generator. The two generated Python files are compared byte-for-byte with the original `grpcio-tools==1.71.0` output during migration. The test-only runtime lock preserves `grpcio==1.71.0`, `protobuf==5.29.6` and `setuptools==80.9.0`. Refresh it with the lock-maintenance tool selected for the project:

```sh
uv pip compile --generate-hashes --python-version 3.13 \
  --python-platform x86_64-unknown-linux-gnu --no-annotate --no-header \
  --constraint bazel/requirements-codegen-constraints.txt \
  --output-file bazel/requirements-codegen-lock.txt bazel/requirements-codegen.in
```

The reusable production module declares distribution requirements and leaves application runtime dependency selection to the image. It has no dependency back on buildimage. The test-only application in `tests/bazel` pins the real SONiC Python/Common owners used to execute the original suites. Its root explicitly selects the selected infra release's single production APT lock; no private component APT lock is rewritten.

```sh
cd tests/bazel
bazel test //:chassisd_test //:ledd_test //:pcied_test //:psud_test \
  //:syseepromd_test //:thermalctld_test //:xcvrd_test //:ycabled_test \
  //:sensormond_test //:stormond_test //:bmcctld_test //:installed_runtime_test \
  --local_test_jobs=1
```

Run the native tests in the Trixie userland declared by `.github/workflows/bazel.yml`. Each original Azure project runs in its own process, preserving automatic pytest discovery, `pytest.ini`, fixtures, mocks and assertions. Writable copies of declared test fixtures keep the original fixture-adjacent writes away from the checkout. Reports and inventories go to Bazel test outputs. The installation test consumes the public raw TARs, compares all eleven setup contracts and RECORD entries, imports installed interfaces with the owner's source excluded, and exercises the generated gRPC request/reply API. The existing Azure workflow remains intact.

The test application retains a compatible minimum request for `rules_distroless@0.9.4.sonic.20`. The published infrastructure requests `0.9.4.sonic.21`, which the formal owner and test module graphs select. Source builds and registered consumption are verified separately.

The test application also supplies the original Azure suite's registered `sonic-py-swsssdk` 2.0.1 source interface. The installation probe supplies `blkinfo==0.2.0` and the same `PyYAML==6.0.2` used by the selected platform-common runtime. These are test prerequisites, with fixed archive hashes in `tests/bazel/requirements_lock.txt`; they do not change daemon distribution requirements. Preserve existing test pins when refreshing this lock:

```sh
cd tests/bazel
uv pip compile --generate-hashes --python-version 3.13 --no-annotate --no-header \
  --constraint requirements_lock.txt --output-file requirements_lock.txt requirements.in
```

The installed psud import uses only the original suite's `sonic_platform` hardware fixture, while Common bindings and daemon modules execute their real implementations. The original two ycabled channel-creation tests explicitly close their real channels in `finally` blocks, preserving their assertions and releasing the subscribed gRPC resources before interpreter shutdown.
