# agos interface spec (v0.1)

This is the contract between the parts of agos. The image, `agentd`, the
deployment scripts and CI all depend on the names, paths and ports below.
Change them here first, then in code.

Background and rationale: `docs/research.md`.

## Product in one paragraph

agos is a Debian 13 (trixie) disk image that boots, configures itself from a
cloud-init seed and presents an X11 desktop that AI agents drive through
`agentd`, with no human input ever required. Humans watch and take over
through the KasmVNC web client, reached over Tailscale (or the LAN in a
weaker mode). The image is VM-first: the hypervisor is the security boundary.

## Repository layout

```
agentd/            Python control daemon (package `agentd`, CLI `agentd`)
image/             mkosi recipe for the guest image (+ builder container)
deploy/proxmox/    Proxmox VE one-liner (agos-proxmox.sh)
deploy/cloud-init/ Example seeds (user-data, meta-data, network-config)
deploy/install.md  Agent-executable install runbook
docs/              spec.md (this file), architecture notes
.github/workflows/ CI: build images, test agentd, lint scripts, release
```

## Versions and naming

- Version: `0.1.0` (single source: `VERSION` file at repo root).
- Architectures use Debian names: `amd64`, `arm64`.
- Release artifacts (GitHub release `v<version>`):
  - `agos-<version>-<arch>.raw.xz`  raw GPT UEFI disk image, xz-compressed
  - `agos-<version>-<arch>.qcow2`   compressed qcow2 (Proxmox, libvirt, Incus, UTM-QEMU)
  - `SHA256SUMS`, `SHA256SUMS.minisig`
- Default release repo: `kroqdotdev/agos` (override with `AGOS_REPO`).
  Download URL pattern:
  `https://github.com/${AGOS_REPO}/releases/download/v${VER}/agos-${VER}-${ARCH}.qcow2`

## Guest image

- Base: Debian 13 trixie, `main contrib non-free-firmware`, plus the
  Tailscale apt repo, Anthropic's signed Claude Code apt repo (stable
  channel; profile `agents`) and the KasmVNC 1.5.0 trixie `.deb` (per arch).
  Kernels arriving through unattended-upgrades get a Boot Loader
  Specification entry (initramfs-tools initrd, `root=PARTLABEL=agos-root`)
  next to the build-time UKI.
- Boot: UEFI only (x86_64 and aarch64), GPT, systemd-boot, Secure Boot off in
  v0.1. Kernel cmdline must include `panic=10 fsck.repair=yes`; boot menu
  timeout 0. Root filesystem ext4 (or btrfs later), grows to fill the disk on
  first boot (systemd-repart / growfs).
- Uninitialized `/etc/machine-id` (systemd's first-boot marker, written as
  `uninitialized` by mkosi); SSH host keys generated on first boot.
- Users:
  - `agent`, uid/gid 1000, home `/home/agent`, shell bash, member of `sudo`,
    passwordless sudo (`/etc/sudoers.d/agos`), password locked. Linger
    enabled (`/var/lib/systemd/linger/agent`).
  - root locked.
- Desktop session runs as **systemd user units of `agent`** (no display
  manager), on display `:1`:
  - `agos-display.service` — KasmVNC `Xvnc :1`, geometry from config
    (default 1280x800, depth 24), client-initiated resizing disabled,
    `Restart=always`.
  - `agos-session.service` — XFCE (`startxfce4` or equivalent) on `DISPLAY=:1`,
    `Requires`/`After` agos-display, `Restart=always`.
  - `agentd.service` — `agentd serve`, `Type=notify`, `WatchdogSec=30`,
    `After` agos-session, `Restart=always`.
  - All three are wanted by `default.target` of the user manager.
- System services: `agos-firstboot.service` (oneshot, after cloud-init's
  config stage so `write_files` has run, before the user manager of `agent`
  starts, i.e. `Before=user@1000.service`), `agos-ready.service` (waits for
  the desktop and finalises `state.json`),
  `tailscaled` (enabled only if configured), `ssh`, `qemu-guest-agent`
  (where virtualised), `cloud-init` (NoCloud, ConfigDrive, VMware, Ec2,
  GCE, Azure, Hetzner, DigitalOcean, OpenStack enabled).
- Browsers: `chromium` and `firefox-esr` with managed policies (no first-run,
  no default-browser check, no sign-in, no password manager, no translate,
  no download prompt, notifications blocked). `/usr/bin/agos-browser`
  launches Chromium with `--no-first-run --no-default-browser-check
  --password-store=basic --force-renderer-accessibility
  --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222
  --user-data-dir=/home/agent/.config/agos-chromium`.
- Accessibility bus enabled for the session (`at-spi2-core`, `python3-gi`,
  `gir1.2-atspi-2.0`; `GTK_A11Y` not disabled; `QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1`).
- Agent CLIs (profile `agents`, on by default): Node.js + Claude Code
  installed for `agent`, pre-seeded so it never prompts
  (`~/.claude/settings.json` with `permissions.defaultMode =
  "bypassPermissions"` and `skipDangerousModePermissionPrompt: true`;
  `~/.claude.json` with `hasCompletedOnboarding: true` and an MCP server
  `desktop` pointing at `/opt/agentd/bin/agentd mcp`).
- Zero-touch hardening: every row of the hardening table in the report that
  applies to a VM guest (see "Zero-touch hardening" there) is implemented in
  the image.

## Configuration contract

Inputs, in priority order (later overrides earlier):

1. Image defaults: `/usr/lib/agos/defaults.toml`.
2. A filesystem labelled `AGOS` (FAT, e.g. a USB stick or a small partition)
   containing `config.toml` and/or `secrets.env`. Copied at first boot.
3. cloud-init `write_files` into `/etc/agos/config.toml` and
   `/etc/agos/secrets.env`.
4. systemd credentials `agos.config` and `agos.secrets` (SMBIOS type 11 /
   `systemd.set_credential`) — QEMU-family hypervisors only.

Never put secrets on the kernel command line.

### `/etc/agos/config.toml` (0644, non-secret)

```toml
hostname = "agos"                # optional; cloud-init hostname wins

[display]
width = 1280
height = 800

[viewer]                         # KasmVNC web client
listen = "127.0.0.1"             # "0.0.0.0" = LAN mode
port = 8444
tls = "auto"                     # auto: off on loopback, self-signed on LAN
user = "agos"                    # basic-auth user; password from VIEWER_PASSWORD
                                 # (generated on first boot if unset and the viewer is
                                 # reachable from outside the VM: LAN mode, or loopback
                                 # published on the tailnet by `tailscale serve`)

[agentd]
listen = "127.0.0.1:8765"

[tailscale]
enabled = "auto"                 # auto = on iff TS_AUTHKEY is set (or already joined)
hostname = ""                    # default: system hostname
tags = []                        # empty: untagged for tskey-auth- keys (forcing a tag
                                 # fails unless the tailnet ACL grants it); OAuth
                                 # client secrets (tskey-client-) need a tag and
                                 # default to ["tag:agos"]
serve = true                     # https://<host>.<tailnet>.ts.net -> viewer
                                 # https://<host>.<tailnet>.ts.net:8765 -> agentd
ssh = false                      # Tailscale SSH

[agents]
claude_code = true
```

### `/etc/agos/secrets.env` (0600 root, `KEY=value` lines)

| Key | Meaning |
|---|---|
| `TS_AUTHKEY` | Tailscale auth key or OAuth client secret (`tskey-client-...`, used with `?preauthorized=true&ephemeral=false` and `--advertise-tags`) |
| `VIEWER_PASSWORD` | KasmVNC basic-auth password |
| `AGENTD_TOKEN` | Admin bearer token for agentd's TCP API (generated if unset) |
| `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `OPENAI_API_KEY` | Exported into the `agent` user's environment (`~/.config/environment.d/50-agos-secrets.conf`, 0600) |

### First boot (`agos-firstboot`)

Idempotent; marker `/var/lib/agos/firstboot.done`; re-runs the "apply"
step on every boot so config edits take effect after a reboot.

1. Merge config sources; validate; write the effective config to
   `/run/agos/config.json`.
2. Generate missing secrets (`AGENTD_TOKEN`; `VIEWER_PASSWORD` when the
   viewer gets basic auth: LAN mode or `tailscale serve`) and write them back
   to `/etc/agos/secrets.env`. With basic auth the viewer user starts
   view-only (agentd's takeover hook `/usr/lib/agos/viewer-perm` switches it
   through KasmVNC's owner API, owner user `agos-admin`); a loopback viewer
   without a password has no users, so every viewer has control.
3. Render: KasmVNC config for `agent`, agentd config + tokens, user
   environment, `/etc/issue.d/agos.issue` (console banner with IP, viewer URL,
   status), Claude Code pre-seed.
4. If Tailscale is enabled: `tailscale up` with the key, tags, hostname; then
   `tailscale serve` as above. Remove the key from disk after a successful
   join only if it is a one-off key (keep OAuth client secrets).
5. Write `/var/lib/agos/state.json` — read by `agos status` and by the
   Proxmox script through the guest agent. `agos-firstboot` writes
   `"state": "starting"`; `agos-ready.service` then waits for Xvnc, the XFCE
   panel, the viewer port and agentd's `/v1/health` and sets `"ready"`, or
   `"error"` with a `"message"` (also for config/Tailscale errors recorded
   in `"errors"`). No secrets in this file.

## Ports

| Port | Bind | Service |
|---|---|---|
| 8444 | 127.0.0.1 (LAN mode: 0.0.0.0) | KasmVNC web client |
| 8765 | 127.0.0.1 | agentd HTTP: REST `/v1`, MCP Streamable HTTP `/mcp`, UI `/ui` |
| 9222 | 127.0.0.1 | Chromium DevTools (when launched via `agos-browser`) |
| 22 | all | OpenSSH (keys only, no root login, no passwords) |

Unix socket: `/run/user/1000/agentd.sock` (peer uid must equal agentd's uid).

## agentd

Python 3.13 package in `agentd/`, installed in the image to a venv at
`/opt/agentd` (created with `--system-site-packages` so Debian's `python3-gi`
is importable). CLI entry point `agentd`. Full reference: `agentd/README.md`.

### Commands

- `agentd serve` — HTTP (+ Unix socket) server: REST, MCP Streamable HTTP, UI.
  Supports `Type=notify` (sends `READY=1`) and `WatchdogSec=` (`WATCHDOG=1`).
- `agentd mcp [--mode auto|local|remote]` — MCP over stdio (for agents inside
  the VM). `remote` forwards every call to `agentd serve` over the Unix
  socket, so the lease, input lock and audit log apply; `local` drives the
  display in-process (workstation use); `auto` = remote if the socket exists
  and answers, else local.
- `agentd status [--json]`, `agentd takeover [--reason] [--ttl]`,
  `agentd handback` — talk to the running server over the Unix socket.
- `agentd token create --scopes observe,input [--name ...] [--stdin]` — mint a
  token (printed once), or with `--stdin` store the hash of a given token
  (e.g. `AGENTD_TOKEN` at first boot); `agentd token list|revoke`.
- Global flags: `--config`, `--display`, `--socket`.

### Config: `~/.config/agentd/config.toml` (rendered by firstboot)

```toml
display = ":1"
listen = "127.0.0.1:8765"
socket = "/run/user/1000/agentd.sock"
audit_log = "/home/agent/.local/state/agentd/audit.jsonl"
max_image_long_edge = 2576       # Anthropic computer_toolset_20260801
max_image_pixels = 3750000       # enforced on the 28-px patch-padded area
default_format = "png"           # png | jpeg | webp
settle_ms = 300                  # quiet period for wait_for_stable
settle_timeout_ms = 3000
on_takeover = ["/usr/lib/agos/viewer-perm", "control"]   # optional hooks
on_handback = ["/usr/lib/agos/viewer-perm", "view"]
viewer_url = ""                  # link shown in /ui (e.g. the tailscale serve URL)
mcp_mode = "remote"              # image: in-VM agents must go through the server
```

Other keys (defaults in `agentd/README.md`): `tokens_file`, `audit_max_bytes`,
`audit_backups`, `audit_text`, `jpeg_quality`, `webp_quality`, `draw_cursor`,
`stable_change_fraction`, `browser_command`, `search_url`, `cdp_url`,
`exec_timeout_max`, `a11y_max_nodes`, `a11y_max_depth`. Precedence: defaults <
file < `AGENTD_<KEY>` environment variables < CLI flags.

Tokens: `~/.config/agentd/tokens.toml` (0600), `[[token]]` entries with
`name`, `sha256` of the token, `scopes`, `created`. Scopes: `observe`,
`input`, `exec`, `files`, `takeover`, `admin` (admin implies all). The file
is re-read when it changes. `AGENTD_TOKEN` in agentd's environment is also
accepted as an admin token (never written to disk).

Auth rules: TCP requires `Authorization: Bearer <token>` always (tailscale
serve proxies from localhost, so loopback is not trusted). Unix socket
requires matching peer uid (`SO_PEERCRED`; the socket file is 0600). MCP
stdio is trusted.

### Canonical actions

Every input action accepts optional `coord_space`
(`"image"` default = pixels of this session's last screenshot,
`"screen"` = physical pixels, `"normalized"` = 0–999 grid) and optional
`expect_frame_id`. If the screen geometry changed since that session's last
screenshot, or `expect_frame_id` is older than the current epoch, the action
is rejected with `STALE_FRAME` and a fresh screenshot is returned (it becomes
the session's new basis). Image and normalized coordinates need a screenshot
in the session first. Input actions without coordinates are also stale when
the session's last screenshot predates the current epoch (a human had
control since). `expect_frame_id` may name any recent frame of the session;
its scale is then used.

```json
{"type":"screenshot"}
{"type":"click","x":10,"y":20,"button":"left|right|middle|back|forward","count":1,"modifiers":["ctrl"]}
{"type":"move","x":10,"y":20}
{"type":"mouse_down","button":"left"}   {"type":"mouse_up","button":"left"}
{"type":"drag","path":[[10,20],[300,400]],"button":"left","modifiers":[]}
{"type":"scroll","x":10,"y":20,"dx":0,"dy":3,"modifiers":[]}
{"type":"type","text":"hello"}
{"type":"key","keys":"ctrl+l","repeat":1}
{"type":"key_down","keys":"shift"}   {"type":"key_up","keys":"shift"}
{"type":"hold_key","keys":"shift","duration":1.5}
{"type":"wait","duration":1.0}
{"type":"wait_for_stable","timeout":3.0,"settle_ms":300}
{"type":"zoom","region":[x0,y0,x1,y1]}
{"type":"cursor_position"}
```

`x`/`y` are optional on `click`, `mouse_down/up` and `scroll` (current
pointer). A one-point `drag` path drags from the pointer. `scroll` `dx`/`dy`
are wheel clicks (`dy > 0` scrolls down). Durations are capped at 300 s.
Keys use xdotool keysym syntax; names are validated (unknown names are
`INVALID_ACTION`). Keys and buttons held via `key_down`/`mouse_down` are
released when a human takes over.

Screenshot metadata (returned with every image):

```json
{"session":"default","frame_id":4182,"epoch":3,"screen":[1280,800],
 "image":[1280,800],"scale":1.0,"coord_space":"image","cursor":[640,412],
 "format":"png"}
```

Batches are validated first (any invalid action rejects the whole batch with
`INVALID_ACTION`), then run in order under one global input lock and stop at
the first failure. The `/actions` response is `{session, ok, results[],
skipped[], error?, screenshot?}`; on failure its HTTP status is the error's.

### Errors

JSON `{"error": {"code": "...", "message": "..."}}` with codes:
`UNAUTHORIZED` (401), `FORBIDDEN` (403, missing scope), `NOT_FOUND` (404),
`HUMAN_IN_CONTROL` (409), `STALE_FRAME` (409), `CONFIRMATION_REQUIRED` (409,
a provider safety check needs a human's acknowledgement), `INVALID_ACTION`
(400), `DISPLAY_UNAVAILABLE` (503), `A11Y_UNAVAILABLE` (503), `INTERNAL` (500).

### REST (prefix `/v1`)

| Method & path | Scope | Purpose |
|---|---|---|
| `GET /health` | none | liveness: `{"ok":true,"display":":1","version":...}` |
| `GET /status` | observe | screen, sessions, lease, version |
| `POST /sessions` | observe | create session → `{"session":"sess_..."}` |
| `POST /screenshot` | observe | body `{session?, format?}` → metadata + base64 image |
| `POST /actions` | input | body `{session?, actions:[...], screenshot_after?: true, format?}` (observe suffices for observation-only batches) |
| `POST /adapters/anthropic` | input | body = Anthropic computer tool_use `input`, a `tool_use` block or a list of blocks → tool_result `content` / block(s) |
| `POST /adapters/openai` | input | body = OpenAI `computer_call` (or action / `actions` list) → `computer_call_output`-shaped object |
| `POST /adapters/gemini` | input | body = Gemini `{name, args}` function call(s) (0–999 coords) → function response(s) + screenshot |
| `GET /windows` | observe | list windows (id, title, class, pid, geometry, active) |
| `POST /windows/{id}/activate` | input | focus/raise |
| `GET /clipboard`, `PUT /clipboard` | files | read/write text clipboard |
| `POST /launch` | exec | start a detached app (argv) in the session |
| `POST /exec` | exec | run a command, return stdout/stderr/exit (timeout) |
| `GET /a11y` | observe | AT-SPI element list (best effort; `window` filter) |
| `GET /takeover` | observe | lease state |
| `POST /takeover` | takeover | human takes control (`{"by": "...", "reason": "...", "ttl"?: s}`) |
| `DELETE /takeover` | takeover | hand back; bumps epoch |
| `POST /display` | admin | set the screen size via xrandr (`{"width", "height"}`) |
| `GET /audit` | takeover | recent audit entries (`?limit=`), used by `/ui` |

Adapters take the session from `?session=` or `X-Agentd-Session` (provider
bodies stay unmodified). They return HTTP 200 with a provider-shaped body
whenever the call was attempted; an action-level failure is expressed in the
provider's own channel (Anthropic `is_error`, Gemini `response.error`) and
in the `X-Agentd-Error-Code` header.

### MCP tools

- `computer` — Anthropic computer-use shaped (`action` enum incl.
  `screenshot, left_click, right_click, middle_click, double_click,
  triple_click, mouse_move, left_click_drag, left_mouse_down, left_mouse_up,
  scroll, type, key, hold_key, wait, cursor_position, zoom`; `coordinate`,
  `text`, `scroll_direction`, `scroll_amount`, `duration`, `region`, plus
  `repeat` and an optional agentd `session`). A drop-in replacement for the
  workstation's `desktop` MCP server.
- `windows`, `clipboard_get`, `clipboard_set`, `launch`, `wait_for_stable`,
  `status`, `a11y_tree`.

Tool calls need the same scopes as the matching REST endpoints. Default
coordinate session: per MCP session for handshake-era clients, per token for
stateless (2026-07-28) clients, one fresh `sess_...` per `agentd mcp`
process.

### Takeover lease

`POST /takeover` (or `agentd takeover`) creates a lease (optional `ttl`
seconds). While it is held, input actions fail with `HUMAN_IN_CONTROL`;
observation works. Taking over releases keys and buttons the agent holds and
stops long `type`/`hold_key` actions early. Handback expires the lease, bumps
the epoch (all earlier frames become stale) and runs `on_handback`. Hooks get
`DISPLAY`, `AGENTD_EVENT`, `AGENTD_LEASE_BY` and `AGENTD_LEASE_REASON`; a
failing or missing hook is audited but does not block the lease change.
Every action, result code, lease change and token name goes to the audit log
(JSONL, one object per line, size-rotated).

## Deployment scripts

`deploy/proxmox/agos-proxmox.sh` — runs on a Proxmox VE 8.x/9.x host as
root. Subcommands `create` (default), `status`, `reset`, `destroy`; flags
`--dry-run`, `--json`, `--yes`, `--vmid`, `--name`, `--storage`, `--bridge`,
`--cores`, `--memory`, `--disk`, `--version`, `--image-url`, `--image-file`,
`--ssh-key-file`, `--secrets-file` (default `/root/agos.secrets`),
`--config-file`, `--isolate`, plus `--iso-storage`, `--cpu`,
`--network-config`, `--image-sha256`, `--require-signature`, `--isolate-dns`,
`--timeout`, `--onboot`, `--wizard`, `--no-wizard`. Every flag has an
`AGOS_*` environment equivalent (e.g. `AGOS_YES=1`, `AGOS_STORAGE`) except
`--wizard`; `AGOS_REPO` selects the release repo. Exit
codes: 0 ok (also "already exists"), 1 usage, confirmation needed without
a TTY, or the guided setup cancelled, 2 preflight failed, 3 download/verify
failed, 4 VM operation failed, 5 timeout waiting for ready.

Guided setup: `bash -c "$(curl -fsSL https://kroq.dev/tools/agos-proxmox.sh)"`
with no arguments (or only `create`), with stdout on a terminal and a usable
`/dev/tty`, opens whiptail dialogs (also on `--wizard`); stdin may be a pipe
(`curl | bash`). It never opens with `--json`, `--yes`, `--dry-run`, another
subcommand, `--no-wizard`/`AGOS_NO_WIZARD=1` or without a terminal, so the
flag behaviour above is unchanged for agents and scripts. Its answers set the
same variables as the flags; typed secrets go to a private temp file that
only feeds the seed. Tailscale tags in the generated `config.toml`:
`["tag:agos"]` for `tskey-client-` secrets, `[]` for `tskey-auth-` keys. `--json` prints exactly one JSON object on
stdout (`vmid`, `name`, `state`, `ip`, `urls.viewer`, `urls.agentd`, ...);
progress goes to stderr. Builds its own NoCloud seed ISO (label `CIDATA`)
instead of a PVE cloud-init drive and attaches it as `ide2` (`scsi1` on arm64,
whose `virt` machine has no IDE bus); config and secrets go in as
`write_files` with `encoding: b64`. Tags the VM `agos`, ejects and deletes the
seed once first boot reports ready, then takes the `golden` snapshot (so a
rollback never references a deleted ISO).

Releases also attach `agos-proxmox.sh`, listed in `SHA256SUMS`, so the
download-verify-run path checks the installer with the same signed sums.

What the script reads from the guest (through the QEMU guest agent), so the
image must provide it:

- `qm agent <vmid> ping` answers once `qemu-guest-agent` runs.
- `/var/lib/agos/state.json`: top-level `"state"`; `"ready"` ends the wait,
  `"failed"` or `"error"` (with an optional `"message"`) fails it with exit 4.
- `/run/agos/config.json`: the effective config with the same structure as
  `config.toml` (`viewer.listen`, `viewer.port`, `viewer.tls`,
  `tailscale.serve`), used to word the access URLs.
- `tailscale status --json` (`BackendState`, `Self.DNSName`) for the tailnet
  URL.
- Generated `VIEWER_PASSWORD` and `AGENTD_TOKEN` are written back to
  `/etc/agos/secrets.env`; the printed credentials hint greps them from there.
- cloud-init `write_files` has run before `agos-firstboot` reads
  `/etc/agos/` (configuration source 3).
