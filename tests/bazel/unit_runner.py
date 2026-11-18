"""Run one original Azure project in a private writable fixture directory."""

import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET

import pytest


def main():
    source = Path(sys.argv[1]).absolute().parent
    output = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=os.environ["TEST_TMPDIR"]) as temporary:
        work = Path(temporary) / source.name
        # Original suites create fixture-adjacent files and compiled script
        # caches. Only this project's declared test inputs are made writable.
        shutil.copytree(source, work)
        if source.name == "sonic-ycabled":
            for generated in sys.argv[2:]:
                shutil.copyfile(generated, work / "proto_out" / Path(generated).name)
        os.chdir(work)
        sys.path[:0] = [str(work), str(work / "scripts")]
        report = output / "pytest.xml"
        status = pytest.main(["--junitxml=" + str(report),
                              "--cov-report=xml:" + str(output / "coverage.xml")])
        root = ET.parse(report).getroot()
        cases = root.findall(".//testcase")
        receipt = {
            "status": "passed" if status == 0 else "failed",
            "project": source.name,
            "selection": "pytest (original project pytest.ini and automatic discovery)",
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "tests": len(cases),
            "failures": len(root.findall(".//failure")),
            "errors": len(root.findall(".//error")),
            "skipped": len(root.findall(".//skipped")),
            "inventory": [case.attrib for case in cases],
        }
        (output / "source-tests.json").write_text(json.dumps(receipt, indent=2) + "\n")
        if not cases:
            raise RuntimeError("Original project suite produced no test inventory")
        return int(status)


if __name__ == "__main__":
    raise SystemExit(main())
