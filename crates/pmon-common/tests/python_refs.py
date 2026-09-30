#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
# Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# Apache-2.0
#
"""Hold the Rust crates' references to the Python daemons to what exists.

The Rust daemons cite the Python they reproduce as `file:Symbol`, for example
`thermalctld:TemperatureUpdater._collect_thermals` or `chassisd:try_get`.  A
symbol survives lines being added above it, which a line number does not, so
line numbers are rejected here outright.  Every symbol cited must exist in the
file it names: a rename or removal on the Python side then fails the Rust
build instead of leaving a comment that points at nothing.

What this cannot see is a symbol that still exists but now does something
else.  That still needs a reader.

Files outside this repository -- the vendor platform API and sonic-py-common --
are cited the same way but cannot be checked from here, and are skipped.

Run directly, or through `cargo test -p pmon-common --test python_refs`.
"""

import ast
import pathlib
import re
import sys
import warnings

ROOT = pathlib.Path(__file__).resolve().parents[3]
CRATES = ROOT / "crates"

DAEMONS = ("chassisd", "pcied", "psud", "sensormond", "stormond", "syseepromd", "thermalctld")
# Cited, but not in this repository.
EXTERNAL = ("daemon_base.py", "device_info.py", "eeprom.py", "fan_drawer.py", "module.py")

_FILES = "|".join(re.escape(f) for f in DAEMONS + EXTERNAL)
# Anything in backticks that starts like a reference, whether or not the rest
# is well formed: a list or a range inside one pair of backticks is caught
# here rather than silently skipped.
CANDIDATE = re.compile(r"`((?:%s):[^`]*)`" % _FILES)
REF = re.compile(r"(?P<file>%s):(?P<symbol>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)" % _FILES)
# A line number anywhere, quoted or not, including the `:123` shorthand for
# "the same file again".
LINE_REF = re.compile(r"\b(?:%s):\d|`:\d" % _FILES)


def _self_attributes(func):
    """`self.x = ...` targets anywhere in a method body."""
    for node in ast.walk(func):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for target in targets:
            for t in ast.walk(target):
                if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                        and t.value.id == "self"):
                    yield t.attr


def symbols(path):
    """Every dotted name a reference into `path` may use.

    Module-level functions, classes and assignments; methods and class
    attributes as `Class.name`; attributes a method assigns on `self` as
    `Class.attr`; nested functions as `outer.inner`.  Definitions inside
    `if`/`try` blocks count at the level of the block.
    """
    # The daemons carry a few `'\|'` regexes; their SyntaxWarning is noise here.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(path.read_text(), filename=str(path))
    names = set()

    def walk(body, prefix, cls):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(prefix + node.name)
                if cls is not None:
                    names.update(cls + "." + a for a in _self_attributes(node))
                walk(node.body, prefix + node.name + ".", None)
            elif isinstance(node, ast.ClassDef):
                names.add(prefix + node.name)
                walk(node.body, prefix + node.name + ".", prefix + node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for t in ast.walk(target):
                        if isinstance(t, ast.Name):
                            names.add(prefix + t.id)
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for block in ("body", "orelse", "finalbody"):
                    walk(getattr(node, block, []), prefix, cls)
                for handler in getattr(node, "handlers", []):
                    walk(handler.body, prefix, cls)

    walk(tree.body, "", None)
    return names


def main():
    known = {d: symbols(ROOT / f"sonic-{d}" / "scripts" / d) for d in DAEMONS}
    problems = []
    checked = skipped = 0

    for rs in sorted(CRATES.rglob("*.rs")):
        rel = rs.relative_to(ROOT)
        for lineno, line in enumerate(rs.read_text().splitlines(), 1):
            where = f"{rel}:{lineno}"
            for m in LINE_REF.finditer(line):
                problems.append(f"{where}: cites Python by line number "
                                f"({line[m.start():m.end() + 8].strip()}...); cite a symbol")
            for m in CANDIDATE.finditer(line):
                ref = REF.fullmatch(m.group(1))
                if ref is None:
                    if not LINE_REF.search(m.group(0)):
                        problems.append(f"{where}: `{m.group(1)}` is not one `file:Symbol`")
                    continue
                if ref["file"] in EXTERNAL:
                    skipped += 1
                    continue
                checked += 1
                if ref["symbol"] not in known[ref["file"]]:
                    problems.append(f"{where}: `{m.group(1)}`: no `{ref['symbol']}` "
                                    f"in sonic-{ref['file']}/scripts/{ref['file']}")

    for p in problems:
        print(p)
    print(f"{checked} Python references checked, {skipped} outside this repository skipped, "
          f"{len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
