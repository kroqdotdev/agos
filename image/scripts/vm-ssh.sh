#!/usr/bin/env bash
# SSH into a VM started by the boot test (scripts/boot_test.py --keep).
# Usage (host): image/scripts/vm-ssh.sh [command...]   (runs ssh in the builder container)
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
key=/src/out/boot-test-${ARCH:-amd64}/id_ed25519
tag="$(sha256sum "$here/builder/Dockerfile" | cut -c1-12)"
exec docker run --rm -i --network host -v "$(dirname "$here")":/src "agos-builder:$tag" \
  ssh -p 10022 -i "$key" -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o LogLevel=ERROR agent@127.0.0.1 "$@"
