#!/usr/bin/env bash
# Fetch the pinned third-party inputs the mkosi build needs and verify them:
#   mkosi.packages/kasmvncserver_trixie_<ver>_<arch>.deb   (local apt repo)
#   mkosi.sandbox/etc/apt/keyrings/{tailscale,claude-code} (build-time apt keys)
# Idempotent: files that already match their pinned sha256 are not re-fetched.
#
# Usage: scripts/sync.sh <amd64|arm64>
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
arch="${1:-${ARCH:-amd64}}"
# shellcheck source=pins.env
. "$here/pins.env"

case "$arch" in
  amd64|arm64) ;;
  *) echo "sync: unsupported arch '$arch' (amd64|arm64)" >&2; exit 2 ;;
esac

fetch() { # url dest sha256
  local url="$1" dest="$2" sum="$3"
  if [[ -f "$dest" ]] && echo "$sum  $dest" | sha256sum -c --status; then
    return 0
  fi
  echo "sync: fetching $url" >&2
  mkdir -p "$(dirname "$dest")"
  curl --proto '=https' --tlsv1.2 -fsSL --retry 3 -o "$dest.part" "$url"
  if ! echo "$sum  $dest.part" | sha256sum -c --status; then
    echo "sync: checksum mismatch for $url (expected $sum, got $(sha256sum "$dest.part" | cut -d' ' -f1))" >&2
    rm -f "$dest.part"
    exit 3
  fi
  mv "$dest.part" "$dest"
}

check_fpr() { # keyfile fingerprint
  local got
  got="$(gpg --batch --quiet --homedir "$(mktemp -d)" --show-keys --with-colons "$1" 2>/dev/null \
         | awk -F: '/^fpr:/ {print $10; exit}')"
  if [[ "$got" != "$2" ]]; then
    echo "sync: $1 has fingerprint '$got', expected '$2'" >&2
    exit 3
  fi
}

# KasmVNC: only the target architecture's package goes into the local repo,
# so apt in the build sandbox cannot pick the wrong one.
sumvar="KASMVNC_SHA256_${arch}"
deb="kasmvncserver_trixie_${KASMVNC_VERSION}_${arch}.deb"
mkdir -p "$here/mkosi.packages"
find "$here/mkosi.packages" -maxdepth 1 -name 'kasmvncserver_*.deb' ! -name "$deb" -delete
fetch "$KASMVNC_URL_BASE/$deb" "$here/mkosi.packages/$deb" "${!sumvar}"

keydir="$here/mkosi.sandbox/etc/apt/keyrings"
fetch "$TAILSCALE_KEYRING_URL" "$keydir/tailscale-archive-keyring.gpg" "$TAILSCALE_KEYRING_SHA256"
check_fpr "$keydir/tailscale-archive-keyring.gpg" "$TAILSCALE_KEYRING_FPR"
fetch "$CLAUDE_CODE_KEY_URL" "$keydir/claude-code.asc" "$CLAUDE_CODE_KEY_SHA256"
check_fpr "$keydir/claude-code.asc" "$CLAUDE_CODE_KEY_FPR"

echo "sync: ok ($arch)" >&2
