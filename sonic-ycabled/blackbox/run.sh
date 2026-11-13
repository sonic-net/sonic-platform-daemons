#!/bin/bash
# Runs the ycabled black-box suite in its container. Arguments go to pytest.
#
#   sonic-ycabled/blackbox/run.sh                      # whole suite, Python ycabled
#   sonic-ycabled/blackbox/run.sh -m "not slow" -x     # skip the 60 s timer cases
#   sonic-ycabled/blackbox/run.sh --bb-coverage        # with Python ycabled's code coverage
#
# Environment:
#   BB_BASE_IMAGE   docker-sonic-vs image to build on (default docker-sonic-vs:latest,
#                   what `docker load -i docker-sonic-vs.gz` creates; see README.md)
#   BB_IMAGE        name of the test image (default ycabled-blackbox:latest)
#   BB_REPORT_DIR   where the reports go (default sonic-ycabled/blackbox/report)
#   BB_YCABLED_CMD  command of another ycabled implementation, run instead of
#                   Python ycabled from this tree; must exist in the image
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
image=${BB_IMAGE:-ycabled-blackbox:latest}
base=${BB_BASE_IMAGE:-docker-sonic-vs:latest}
report=${BB_REPORT_DIR:-$here/report}

if ! docker image inspect "$base" >/dev/null 2>&1 && ! docker pull -q "$base" >/dev/null; then
    echo "Base image $base not found. Load docker-sonic-vs.gz from a sonic-buildimage VS build" >&2
    echo "(docker load -i docker-sonic-vs.gz), or set BB_BASE_IMAGE. See $here/README.md." >&2
    exit 2
fi

docker build -q --build-arg BASE_IMAGE="$base" -t "$image" "$here" >/dev/null
mkdir -p "$report"
# Absolute: docker takes a bare name for a named volume.
report=$(cd "$report" && pwd)

extra=()
if [ -n "${BB_YCABLED_CMD:-}" ]; then
    extra+=(-e "BB_YCABLED_CMD=$BB_YCABLED_CMD")
fi
tty=()
if [ -t 1 ]; then
    tty=(-t)
fi

docker run --rm --init "${tty[@]}" "${extra[@]}" \
    --sysctl net.ipv6.conf.all.disable_ipv6=0 \
    --sysctl net.ipv6.conf.lo.disable_ipv6=0 \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
    -v "$repo:/src:ro" -v "$report:/report" \
    -w /src/sonic-ycabled/blackbox \
    "$image" bash -c 'python3 -m pytest -c pytest.ini --bb-report-dir=/tmp/bb-report "$@"; rc=$?
                      # The suite runs as the image user bb; the report goes to the host user.
                      sudo cp -a /tmp/bb-report/. /report/ 2>/dev/null
                      sudo chown -R "$HOST_UID:$HOST_GID" /report; exit $rc' bash "$@"
