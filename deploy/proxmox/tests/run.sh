#!/usr/bin/env bash
# Run the deploy/ tests: shellcheck, the fake-Proxmox suite and a real-render
# smoke test of the guided setup (whiptail in tmux), all in debian:trixie
# containers. Needs only Docker on the host.
#   deploy/proxmox/tests/run.sh            # everything
#   deploy/proxmox/tests/run.sh isolate    # only tests whose name contains "isolate"
#   deploy/proxmox/tests/run.sh render     # only the real-render smoke test
# The render test saves its screen captures in out/wizard-screens/.
set -euo pipefail
here=$(cd -- "$(dirname -- "$0")" && pwd)
repo=$(cd -- "$here/../../.." && pwd)
filter=${1:-}

echo "== shellcheck"
docker run --rm -v "$repo:/mnt:ro" -w /mnt koalaman/shellcheck:stable -x \
	deploy/proxmox/agos-proxmox.sh deploy/cloud-init/make-seed.sh \
	deploy/proxmox/tests/run.sh deploy/proxmox/tests/test-proxmox.sh deploy/proxmox/tests/wizard-render.sh \
	deploy/proxmox/tests/fake-pve/lib.sh deploy/proxmox/tests/fake-pve/bin/* deploy/proxmox/tests/fake-whiptail/whiptail
echo "shellcheck: clean"

if [[ $filter != render ]]; then
	echo "== fake-Proxmox tests (debian:trixie)"
	docker build -q --target base -t agos-deploy-tests "$here" >/dev/null
	docker run --rm --hostname pve1 -v "$repo:/src:ro" agos-deploy-tests \
		bash /src/deploy/proxmox/tests/test-proxmox.sh "$filter"
fi

if [[ -z $filter || $filter == render || $filter == wizard* ]]; then
	echo "== guided setup rendered by real whiptail in tmux"
	docker build -q --target render -t agos-deploy-render "$here" >/dev/null
	mkdir -p "$repo/out/wizard-screens"
	docker run --rm --hostname pve1 -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
		-v "$repo:/src:ro" -v "$repo/out/wizard-screens:/screens" agos-deploy-render \
		bash /src/deploy/proxmox/tests/wizard-render.sh /screens
	echo "screens: $repo/out/wizard-screens/"
fi
