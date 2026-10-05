#!/usr/bin/env bash
# Zero-touch QEMU boot test; see scripts/boot_test.py. Runs in the builder container.
# Usage: scripts/boot-test.sh <amd64|arm64> [--keep] [--no-reboot] [--timeout S]
set -euo pipefail
exec python3 "$(dirname "${BASH_SOURCE[0]}")/boot_test.py" "$@"
