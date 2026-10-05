#!/usr/bin/env bash
# Open the boot-test VM's KasmVNC web client in a real (headless) Chromium, as
# a human viewer would: TLS, basic auth, WebSocket stream. Runs on the host in
# a pinned Playwright container with host networking, while
# `make boot-test BOOT_TEST_ARGS=--keep` holds the VM (hostfwd 127.0.0.1:18444).
#
# Usage: scripts/viewer-check.sh [arch] [resize=scale|remote]
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
arch="${1:-amd64}"
resize="${2:-scale}"
playwright_version=1.63.0
out="$(dirname "$here")/out"
mkdir -p "$out"
docker run --rm --network host -v "$here/tests:/w:ro" -v "$out:/out" -w /tmp \
  "mcr.microsoft.com/playwright:v${playwright_version}-noble" sh -c "
    npm init -y >/dev/null && npm i --silent playwright@${playwright_version} >/dev/null 2>&1 &&
    cp /w/viewer.mjs . && node viewer.mjs 'https://127.0.0.1:18444/?autoconnect=1&resize=${resize}' \
      agos boottest-viewer-pw /out/${arch}-viewer.png"
echo "viewer-check: screenshot in out/${arch}-viewer.png"
