# agos image builder: Debian trixie + mkosi (pinned git tag) + the tools mkosi,
# the artifact conversion and the QEMU boot test need. The container *is* the
# tools tree (mkosi runs with ToolsTree=no), so everything mkosi executes comes
# from this pinned environment rather than from the host.
#
# Build:  docker build -t agos-builder image/builder
# Use:    see image/Makefile (mkosi needs --privileged for its mount namespaces)

# debian:trixie-slim as of 2026-09-18 (Debian 13.7)
FROM debian:trixie-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a

ARG MKOSI_REPO=https://github.com/systemd/mkosi.git
ARG MKOSI_TAG=v27.1
ARG MKOSI_COMMIT=ecf7f5702bb2052b7a69cab3fe7d37bc3b9ad77b

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1

# Package groups:
#  - mkosi runtime for Debian targets (mirrors mkosi's default Debian tools tree)
#  - artifact conversion (qemu-img, xz) and seed ISO creation (xorriso)
#  - boot test (qemu-system-*, OVMF/AAVMF firmware, ssh client, curl, jq)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      apt apt-utils bash ca-certificates coreutils cpio curl debian-archive-keyring \
      dosfstools e2fsprogs erofs-utils fdisk git gnupg kmod libarchive-tools \
      libcryptsetup12 libfido2-1 libseccomp2 mount mtools openssl python3 \
      python3-pefile squashfs-tools systemd systemd-boot-tools systemd-repart \
      systemd-ukify tar uidmap util-linux xz-utils zstd \
      qemu-utils xorriso \
      qemu-system-x86 qemu-system-arm ovmf qemu-efi-aarch64 \
      openssh-client jq procps socat python3-pil \
 && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "${MKOSI_TAG}" "${MKOSI_REPO}" /opt/mkosi \
 && test "$(git -C /opt/mkosi rev-parse HEAD)" = "${MKOSI_COMMIT}" \
 && ln -s /opt/mkosi/bin/mkosi /usr/local/bin/mkosi \
 && mkosi --version

# uv: exports agentd's uv.lock as hash-pinned requirements and builds its wheel,
# so the image gets exactly the locked dependency set.
ARG UV_VERSION=0.12.23
ARG UV_SHA256_amd64=9167d72b3319674b6303c4cbe071854bba13ebdf3d76b1a7cbdc175471fb66d6
ARG UV_SHA256_arm64=6524bd338177ed50d035d39354e12545e993bbeba2ecbddf0480c5b3a81d313f
RUN set -eu; arch="$(dpkg --print-architecture)"; \
    case "$arch" in amd64) triple=x86_64; sum="$UV_SHA256_amd64";; arm64) triple=aarch64; sum="$UV_SHA256_arm64";; esac; \
    curl -fsSL -o /tmp/uv.tgz "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${triple}-unknown-linux-gnu.tar.gz"; \
    echo "$sum  /tmp/uv.tgz" | sha256sum -c -; \
    tar -xzf /tmp/uv.tgz -C /usr/local/bin --strip-components=1; rm /tmp/uv.tgz; uv --version

WORKDIR /src
