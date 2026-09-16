#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(git -C "$script_dir" rev-parse --show-toplevel)"
image="${XCVRD_TEST_IMAGE:-sonic-xcvrd-test:bookworm}"

docker run --rm --init --pull=never \
    --mount "type=bind,src=$repo_root,dst=/workspace" \
    --workdir /workspace/sonic-xcvrd \
    --env PYTHONDONTWRITEBYTECODE=1 \
    --env PIP_BREAK_SYSTEM_PACKAGES=1 \
    "$image" bash -ec '
        python3 -m pip install -q ".[testing]"
        exec python3 -m pytest -o addopts="-v -ra --strict-markers" \
            -o cache_dir=/tmp/xcvrd-pytest-cache "$@"
    ' -- "$@"
