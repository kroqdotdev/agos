# Working on agos

agos is an unattended Debian 13 desktop image for AI agents. Read
`docs/spec.md` first: it is the contract between `agentd/`, `image/`,
`deploy/` and CI. If you must change a name, path or port, change the spec
in the same change.

## Ground rules

- **Do not touch the developer's live desktop.** This workstation runs its
  own KasmVNC desktop on `DISPLAY=:1` (user unit `kasmvnc.service`) and a
  `desktop` MCP server in `~/.claude/mcp-servers/desktop/`. Never send input
  to `:1`, never stop/modify those units or files. Tests start their own
  private `Xvfb` on displays `:90`–`:99`.
- **Keep the host clean.** Do not `apt install` on the host. Run build tools
  (mkosi, qemu, shellcheck, xorriso, ...) inside Docker containers; Docker
  works without sudo. `/dev/kvm` exists and may be passed to containers.
- Do not commit, push, create GitHub repos or publish anything; the
  maintainer does that.
- No secrets in the repo or in images. Secrets arrive at first boot.

## Conventions

- Python: 3.12+ compatible (image runs 3.13), managed with `uv`;
  `uv run pytest` must pass. Keep dependencies few and pinned with ranges.
- Shell: bash, `set -euo pipefail`, shellcheck-clean
  (`docker run --rm -v "$PWD:/mnt" koalaman/shellcheck:stable <files>`).
- Match the surrounding style; comments explain why, not what.
- Every zero-touch tweak in the image gets a one-line comment naming the
  prompt or dialog it prevents.
