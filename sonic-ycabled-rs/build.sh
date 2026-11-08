#!/bin/bash
# Builds ycabled-rs in its build image (see Dockerfile), into ./target.
#
#   sonic-ycabled-rs/build.sh                       # cargo build --release
#   sonic-ycabled-rs/build.sh cargo test --release  # any command, in the crate directory
#
# Environment:
#   YCABLED_RS_BASE_IMAGE  image ycabled-rs will run in (default docker-sonic-vs:latest)
#   YCABLED_RS_BUILD_IMAGE name of the build image (default ycabled-rs-build:latest)
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/.." && pwd)
base=${YCABLED_RS_BASE_IMAGE:-docker-sonic-vs:latest}
image=${YCABLED_RS_BUILD_IMAGE:-ycabled-rs-build:latest}

if ! docker image inspect "$image" >/dev/null 2>&1; then
    docker build -q --build-arg BASE_IMAGE="$base" -t "$image" "$here" >/dev/null
fi
if [ $# -eq 0 ]; then
    set -- cargo build --release
fi
tty=()
if [ -t 1 ]; then
    tty=(-t)
fi
# The cargo registry persists in a volume; the build runs as the host user so
# that ./target stays the user's.
docker volume create ycabled-rs-cargo >/dev/null
docker run --rm "${tty[@]}" --user "$(id -u):$(id -g)" \
    -e CARGO_HOME=/cargo -e HOME=/tmp \
    -v ycabled-rs-cargo:/cargo -v "$repo:/src" -w /src/sonic-ycabled-rs \
    "$image" bash -c 'cp -n -r /usr/local/cargo/bin /cargo/ 2>/dev/null; export PATH=/cargo/bin:$PATH; "$@"' bash "$@"
