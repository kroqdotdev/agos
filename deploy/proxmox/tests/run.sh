#!/usr/bin/env bash
# Run the deploy/ tests: shellcheck, then the fake-Proxmox suite inside a
# debian:trixie container. Needs only Docker on the host.
#   deploy/proxmox/tests/run.sh            # everything
#   deploy/proxmox/tests/run.sh isolate    # only tests whose name contains "isolate"
set -euo pipefail
here=$(cd -- "$(dirname -- "$0")" && pwd)
repo=$(cd -- "$here/../../.." && pwd)

echo "== shellcheck"
docker run --rm -v "$repo:/mnt:ro" -w /mnt koalaman/shellcheck:stable -x \
	deploy/proxmox/agos-proxmox.sh deploy/cloud-init/make-seed.sh \
	deploy/proxmox/tests/run.sh deploy/proxmox/tests/test-proxmox.sh \
	deploy/proxmox/tests/fake-pve/lib.sh deploy/proxmox/tests/fake-pve/bin/*
echo "shellcheck: clean"

echo "== fake-Proxmox tests (debian:trixie)"
docker build -q -t agos-deploy-tests "$here" >/dev/null
docker run --rm --hostname pve1 -v "$repo:/src:ro" agos-deploy-tests \
	bash /src/deploy/proxmox/tests/test-proxmox.sh "$@"
