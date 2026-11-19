# Reproducible build environment for xcvrd-rs targeting the pmon container.
#
# pmon = Debian 13 (trixie), Python 3.13.5, glibc 2.41. We build in the matching
# base so the binary's glibc and libpython3.13 SONAME resolve at runtime in pmon.
# python3-dev / libpython3.13-dev provide PyO3's build-time headers + link lib.
#
# swss-common (the STATE_DB bindings) additionally needs: clang/libclang (its
# build.rs runs bindgen over the C-API headers) and the C-API headers themselves.
# We provide the headers by cloning sonic-swss-common at the SAME pinned rev the
# xcvrd-rs/Cargo.toml depends on, and point SWSS_COMMON_REPO at it. bindgen
# is forced to C mode (BINDGEN_EXTRA_CLANG_ARGS=-x c) so it only parses the C-API
# (no boost / hiredis / C++ swss headers). The link library libswsscommon.so is
# NOT baked here (it is pmon-specific) -- it is mounted at build time from
# a directory of matching pmon libraries (see README.md).
FROM rust:trixie

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-dev \
        libpython3.13-dev \
        pkg-config \
        clang \
        libclang-dev \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# C-API headers for the swss-common bindgen build, pinned to match
# xcvrd-rs/Cargo.toml's swss-common git rev. Keep these two in sync.
ARG SWSS_COMMON_REV=7faca59cd30367692a8c5406ad5d7c0b5341c7ef
RUN git clone https://github.com/sonic-net/sonic-swss-common /opt/sonic-swss-common \
    && git -C /opt/sonic-swss-common checkout "${SWSS_COMMON_REV}"
ENV SWSS_COMMON_REPO=/opt/sonic-swss-common
ENV BINDGEN_EXTRA_CLANG_ARGS="-x c"

RUN rustup component add rustfmt clippy

WORKDIR /src
