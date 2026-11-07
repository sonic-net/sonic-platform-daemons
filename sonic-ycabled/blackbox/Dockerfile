# Test image for the ycabled black-box suite.
#
# docker-sonic-vs already provides what ycabled needs at run time: redis-server,
# swsscommon, sonic_py_common, sonic_platform_base, sonic_y_cable and the
# sonic-config-engine port config parser. The suite adds the gRPC and protobuf
# versions ycabled pins in setup.py, and the test tools.
ARG BASE_IMAGE=docker-sonic-vs:latest
FROM ${BASE_IMAGE}

ARG GRPC_VERSION=1.71.0
ARG PROTOBUF_VERSION=5.29.6

# pip --target keeps the pinned versions ahead of the base image's newer
# grpcio and protobuf on sys.path, for the suite and for ycabled.
RUN pip3 install --no-cache-dir --target /opt/bb/lib \
        "grpcio==${GRPC_VERSION}" \
        "grpcio-tools==${GRPC_VERSION}" \
        "protobuf==${PROTOBUF_VERSION}" \
        "pytest>=8,<9" \
        "pytest-timeout>=2.3" \
        "coverage>=7.6,<8"

ENV PYTHONPATH=/opt/bb/lib
ENV PATH=/opt/bb/lib/bin:${PATH}
# This container is the suite's to take over (see conftest.py).
ENV VS_ENABLE=1

# The suite runs as a user with sudo, as in the sonic-platform-daemons pipeline,
# and sets up what needs root with sudo (harness/env.py).
RUN useradd --create-home --shell /bin/bash bb \
    && echo "bb ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/bb \
    && chmod 0440 /etc/sudoers.d/bb
USER bb

# The base image starts supervisord and the whole SONiC stack. The suite starts
# only what it needs.
ENTRYPOINT []
CMD ["bash"]
