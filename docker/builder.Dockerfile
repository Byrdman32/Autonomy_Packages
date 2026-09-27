# syntax=docker/dockerfile:1
# Package builder: Ubuntu, GCC and CMake matching the images that install these packages, plus the Debian
# packaging tools. Build from the repo root:
#   docker build -f docker/builder.Dockerfile -t autonomy-packages-builder .
ARG UBUNTU_VERSION=24.04
FROM ubuntu:${UBUNTU_VERSION}

ARG DEBIAN_FRONTEND=noninteractive
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

COPY docker/builder.env /etc/autonomy/builder.env

RUN . /etc/autonomy/builder.env && \
    apt-get update && apt-get install --no-install-recommends -y \
        build-essential gcc-${GCC_MAJOR} g++-${GCC_MAJOR} ninja-build git curl ca-certificates pkg-config \
        python3 dpkg-dev file gnupg && \
    rm -rf /var/lib/apt/lists/*

RUN . /etc/autonomy/builder.env && \
    curl -fsSL --retry 5 --retry-all-errors \
        "https://github.com/Kitware/CMake/releases/download/v${CMAKE_VERSION}/cmake-${CMAKE_VERSION}-linux-$(uname -m).tar.gz" \
        | tar -xz --strip-components=1 -C /usr/local && \
    cmake --version

WORKDIR /work
