# Test image for deploy/: a plain Debian 13 userland plus the tools a Proxmox
# VE host has (perl, genisoimage) and the ones the tests use to inspect seeds
# (isoinfo, xorriso, cloud-init for schema checks, minisign, python3-yaml).
# openssl is on every Proxmox host and verifies signatures without minisign.
# The Proxmox commands themselves are fakes from fake-pve/bin.
FROM debian:trixie
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      perl genisoimage xorriso cloud-init python3-yaml minisign openssl ca-certificates \
      util-linux bsdutils procps \
 && rm -rf /var/lib/apt/lists/*
