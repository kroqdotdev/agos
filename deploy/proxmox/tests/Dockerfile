# Test images for deploy/.
#
# base: a plain Debian 13 userland plus the tools a Proxmox VE host has (perl,
# genisoimage, openssl) and the ones the tests use to inspect seeds (isoinfo,
# xorriso, cloud-init for schema checks, minisign, python3-yaml). openssl is
# on every Proxmox host and verifies signatures without minisign. The Proxmox
# commands themselves are fakes from fake-pve/bin; whiptail is left out on
# purpose (the suite uses a fake one and checks the "not installed" hint).
FROM debian:trixie AS base
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      perl genisoimage xorriso cloud-init python3-yaml minisign openssl ca-certificates \
      util-linux bsdutils procps \
 && rm -rf /var/lib/apt/lists/*

# render: real whiptail in tmux, to look at the guided setup as a user would.
FROM base AS render
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends whiptail tmux ncurses-bin \
 && rm -rf /var/lib/apt/lists/*
