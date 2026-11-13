# Build environment for ycabled-rs.
#
# Built on the image ycabled-rs runs in, so that the binary links the same glibc
# and libpython. For the black-box suite that is docker-sonic-vs (Debian
# bookworm, Python 3.11); for pmon it is docker-pmon (Debian trixie, Python 3.13).
#
#   docker build --build-arg BASE_IMAGE=docker-sonic-vs:latest -t ycabled-rs-build sonic-ycabled-rs
#
# See README.md for the build and test commands.
ARG BASE_IMAGE=docker-sonic-vs:latest
FROM ${BASE_IMAGE}

ARG RUST_VERSION=1.99.1

# protoc for tonic-build; libpython for PyO3; a C toolchain for the linker.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential pkg-config protobuf-compiler python3-dev curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:${PATH}
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path --profile minimal \
        --default-toolchain "${RUST_VERSION}" --component clippy,rustfmt,llvm-tools-preview \
    && chmod -R a+w "${RUSTUP_HOME}" "${CARGO_HOME}"

# build.sh runs the build as the calling user (docker run --user).
USER nobody
ENTRYPOINT []
CMD ["bash"]
