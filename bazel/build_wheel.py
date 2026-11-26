"""Run the daemon's setuptools backend in a private, writable source tree."""

import argparse
import os
from pathlib import Path
import shutil
import tempfile

from setuptools.build_meta import build_wheel


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sources", nargs="+", type=Path, required=True)
    parser.add_argument("--generated", nargs="+", type=Path, default=[])
    args = parser.parse_args()
    output = args.output.absolute()
    os.environ["SOURCE_DATE_EPOCH"] = "315532800"
    with tempfile.TemporaryDirectory() as directory:
        source_root = Path(directory)
        for source in args.sources:
            relative = source.relative_to(args.setup.parent)
            staged = source_root / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, staged)
            staged.chmod(0o755 if relative.parts[0] == "scripts" else 0o644)
        for source in args.generated:
            shutil.copyfile(source, source_root / "proto_out" / source.name)
        os.chdir(source_root)
        filename = build_wheel(str(output.parent))
        if filename != output.name:
            raise ValueError(f"setup.py produced {filename}, expected {output.name}")


if __name__ == "__main__":
    main()
