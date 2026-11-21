"""Collect the exact public installations and original project test receipts."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile


PROJECTS = ("chassisd", "ledd", "pcied", "psud", "syseepromd", "thermalctld", "xcvrd",
            "ycabled", "sensormond", "stormond", "bmcctld")


def bazel(directory, root):
    command = ["bazel"]
    if directory != root and os.environ.get("BAZEL_TEST_OUTPUT_BASE"):
        command.append("--output_base=" + os.environ["BAZEL_TEST_OUTPUT_BASE"])
    return command


def info(command, directory, root, key):
    # Filesystem locations do not depend on the application's target platform.
    # Bazel's fast info path does not resolve its Trixie repository alias;
    # use the declared platforms module's host only for these info queries.
    flags = [] if directory == root else ["--platforms=@platforms//host", "--host_platform=@platforms//host"]
    return subprocess.check_output(command + ["info", *flags, key], cwd=directory, text=True).strip()


def retain(source, destination):
    # Bazel outputs may be read-only; snapshots keep their bytes rather than
    # propagating the output-cache mode to a file a later collection replaces.
    destination = Path(destination)
    destination.unlink(missing_ok=True)
    return shutil.copyfile(source, destination)


def main():
    root = Path(__file__).resolve().parents[2]
    test_root = root / "tests/bazel"
    output = root / "bazel-evidence"
    output.mkdir(exist_ok=True)
    # Previous snapshots can inherit read-only Bazel output directory modes.
    for directory in output.rglob("*"):
        if directory.is_dir():
            directory.chmod(directory.stat().st_mode | 0o200)
    artifacts = []
    errors = []
    for scope, directory in (("owner", root), ("tested", test_root)):
        command = bazel(directory, root)
        execution_root = Path(info(command, directory, root, "execution_root"))
        for role in ("wheels", "runtime", "runtime_metadata"):
            label = ("@sonic_platform_daemons" if scope == "tested" else "") + "//:daemons." + role
            paths = subprocess.check_output(command + ["cquery", label, "--output=files"], cwd=directory, text=True).splitlines()
            for name in paths:
                source = execution_root / name
                target = output / (scope + "-" + source.name)
                if role == "runtime_metadata":
                    retain(source, target)
                artifacts.append({"scope": scope, "label": label, "file": name,
                                  "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
        lock = directory / "MODULE.bazel.lock"
        if lock.exists():
            retain(lock, output / (scope + "-MODULE.bazel.lock"))
    command = bazel(test_root, root)
    testlogs = Path(info(command, test_root, root, "bazel-testlogs"))
    receipts = {}
    for name in [project + "_test" for project in PROJECTS] + ["installed_runtime_test"]:
        logs = testlogs / name
        if not (logs / "test.xml").is_file():
            errors.append(name + ": missing Bazel XML")
            continue
        shutil.copytree(logs, output / name, dirs_exist_ok=True, copy_function=retain)
        receipt_name = "wheels.json" if name == "installed_runtime_test" else "source-tests.json"
        outputs = logs / "test.outputs"
        if (outputs / receipt_name).is_file():
            receipt = json.loads((outputs / receipt_name).read_text())
        else:
            with zipfile.ZipFile(outputs / "outputs.zip") as archive:
                receipt = json.loads(archive.read(receipt_name))
        receipts[name] = receipt
        if receipt["status"] != "passed":
            errors.append(name + ": " + receipt["status"])
    provenance = {
        "revision": subprocess.check_output(["git", "-c", "safe.directory=" + str(root), "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "ci_head_sha": os.environ.get("CI_HEAD_SHA", ""),
        "artifacts": artifacts,
        "test_receipts": receipts,
        "errors": errors,
        "scope": "AMD64, declared Trixie userland and Python 3.13; original owner suites and installed interfaces",
    }
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    if errors:
        raise RuntimeError("; ".join(errors))


if __name__ == "__main__":
    main()
