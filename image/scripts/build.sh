#!/usr/bin/env bash
# Build the agos disk image with mkosi. Runs inside the builder container
# (image/builder/Dockerfile), as root, with the repository mounted at /src.
#
# Usage: scripts/build.sh <amd64|arm64> [extra mkosi args...]
# Env:   AGOS_WITH_AGENTD=auto|yes|no  (default auto: install agentd if it builds)
#        HOST_UID/HOST_GID             (hand outputs back to the invoking user)
#        MKOSI_FORCE=-f|-ff            (default -f; -ff also drops the package cache)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"

arch="${1:-amd64}"
shift || true

case "$arch" in
  amd64) mkosi_arch=x86-64 ;;
  arm64) mkosi_arch=arm64 ;;
  *) echo "build: unsupported arch '$arch'" >&2; exit 2 ;;
esac

host_arch="$(dpkg --print-architecture)"
if [[ "$host_arch" != "$arch" ]]; then
  # Foreign builds need qemu-user binfmt registered on the host. We do not do
  # that implicitly; build each architecture on a native runner instead.
  echo "build: refusing to cross-build $arch on $host_arch (build natively, see README)" >&2
  exit 2
fi

AGOS_VERSION="$(cat ../VERSION)"
export AGOS_VERSION
export AGOS_WITH_AGENTD="${AGOS_WITH_AGENTD:-auto}"

handback() {
  if [[ -n "${HOST_UID:-}" ]]; then
    # Only outputs: the incremental cache must keep the image's real ownership.
    chown -R "${HOST_UID}:${HOST_GID:-$HOST_UID}" mkosi.output mkosi.packages \
      mkosi.sandbox/etc/apt/keyrings 2>/dev/null || true
  fi
}
trap handback EXIT

scripts/sync.sh "$arch"

# agentd: a wheel plus hash-pinned requirements exported from its uv.lock, both
# consumed by mkosi.postinst.chroot. Never writes into ../agentd. Failures are
# left for the postinst step to judge (AGOS_WITH_AGENTD=auto|yes|no).
dist=.cache/agentd
rm -rf "$dist"
mkdir -p "$dist"
if [[ "$AGOS_WITH_AGENTD" != no && -f ../agentd/pyproject.toml ]]; then
  if [[ -f ../agentd/uv.lock ]]; then
    uv export --project ../agentd --frozen --no-dev --no-emit-project --no-header \
      --format requirements-txt --quiet --output-file "$dist/requirements.txt" \
      || { echo "build: uv export of agentd's lock failed" >&2; rm -f "$dist/requirements.txt"; }
  fi
  uv build --wheel --out-dir "$dist" --quiet ../agentd \
    || echo "build: agentd wheel build failed" >&2
fi

mkdir -p mkosi.output mkosi.cache .cache/pkgcache
start=$(date +%s)
mkosi --architecture="$mkosi_arch" "${MKOSI_FORCE:--f}" "$@" build
echo "build: $arch image built in $(( $(date +%s) - start ))s" >&2
ls -l mkosi.output/ >&2
