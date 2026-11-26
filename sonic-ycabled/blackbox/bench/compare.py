#!/usr/bin/env python3
"""Puts two benchmark results of bench/test_bench.py side by side.

    bench/compare.py <report dir>                 # bench-python.json and bench-rust.json
    bench/compare.py a.json b.json
"""

import json
import os
import sys


def load(path):
    with open(path) as f:
        data = json.load(f)
    return data["implementation"], {r["scenario"]: r for r in data["results"]}


def main(argv):
    if len(argv) == 2 and os.path.isdir(argv[1]):
        paths = [os.path.join(argv[1], "bench-python.json"), os.path.join(argv[1], "bench-rust.json")]
    elif len(argv) == 3:
        paths = argv[1:]
    else:
        print(__doc__)
        return 2
    (name_a, a), (name_b, b) = load(paths[0]), load(paths[1])
    order = list(dict.fromkeys(list(a) + list(b)))
    print("| Scenario | Metric | {} | {} |".format(name_a, name_b))
    print("| --- | --- | ---: | ---: |")
    for scenario in order:
        metrics = [k for k in list(a.get(scenario, {})) + list(b.get(scenario, {})) if k != "scenario"]
        for m in dict.fromkeys(metrics):
            print("| {} | {} | {} | {} |".format(scenario, m, a.get(scenario, {}).get(m, ""),
                                                 b.get(scenario, {}).get(m, "")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
