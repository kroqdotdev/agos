#!/usr/bin/env bash
# Derive the release artifacts from the raw disk image (spec "Versions and naming"):
#   agos-<ver>-<arch>.qcow2   compressed qcow2 (Proxmox, libvirt, Incus, UTM-QEMU)
#   agos-<ver>-<arch>.raw.xz  raw GPT UEFI disk, xz-compressed
#   SHA256SUMS                checksums of every artifact present
# Runs inside the builder container.
#
# Usage: scripts/convert.sh <amd64|arm64> [qcow2|xz|all]
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here/mkosi.output"

arch="${1:-amd64}"
what="${2:-all}"
version="$(cat "$here/../VERSION")"
base="agos-${version}-${arch}"

[[ -f "$base.raw" ]] || { echo "convert: $base.raw not found; run 'make build' first" >&2; exit 1; }

handback() {
  if [[ -n "${HOST_UID:-}" ]]; then
    chown "${HOST_UID}:${HOST_GID:-$HOST_UID}" ./agos-* SHA256SUMS 2>/dev/null || true
  fi
}
trap handback EXIT

if [[ "$what" == qcow2 || "$what" == all ]]; then
  echo "convert: $base.qcow2" >&2
  # zlib (default) compression: readable by every qemu-img/Proxmox/UTM version
  qemu-img convert -f raw -O qcow2 -c "$base.raw" "$base.qcow2.part"
  mv "$base.qcow2.part" "$base.qcow2"
fi

if [[ "$what" == xz || "$what" == all ]]; then
  echo "convert: $base.raw.xz" >&2
  xz -T0 -6 --keep --force --stdout "$base.raw" > "$base.raw.xz.part"
  mv "$base.raw.xz.part" "$base.raw.xz"
fi

# One SHA256SUMS covering every architecture's artifacts that exist here.
sha256sum agos-*.qcow2 agos-*.raw.xz 2>/dev/null | sort -k2 > SHA256SUMS || true
ls -l --block-size=M "$base".* SHA256SUMS >&2
