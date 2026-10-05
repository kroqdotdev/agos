# agos guest image

A Debian 13 (trixie) UEFI disk image, built with [mkosi](https://github.com/systemd/mkosi)
v27.1, that boots, configures itself from cloud-init (or an `AGOS` stick, or
systemd credentials) and presents an XFCE desktop on KasmVNC's Xvnc that AI
agents drive through `agentd`, with no human input. The interface contract
(paths, users, units, ports, config files) is [`docs/spec.md`](../docs/spec.md);
the reasoning behind it is [`docs/research.md`](../docs/research.md).

- [Quick start](#quick-start)
- [How the build works](#how-the-build-works)
- [What is in the image](#what-is-in-the-image)
- [First boot and configuration](#first-boot-and-configuration)
- [Boot test](#boot-test)
- [Measured (amd64, this workstation)](#measured-amd64-this-workstation)
- [Zero-touch hardening](#zero-touch-hardening)
- [arm64 and CI](#arm64-and-ci)
- [Known gaps](#known-gaps)
- [Updating pinned inputs](#updating-pinned-inputs)

## Quick start

Requirements: Docker (no sudo needed), ~25 GB free disk, `/dev/kvm` for a fast
boot test. Nothing is installed on the host; every tool runs in the builder
container.

```sh
make -C image build ARCH=amd64        # mkosi.output/agos-0.1.0-amd64.raw  (~1.5 min warm, ~4 min cold)
make -C image artifacts ARCH=amd64    # + .qcow2, .raw.xz, SHA256SUMS
make -C image boot-test ARCH=amd64    # zero-touch QEMU/OVMF test -> out/
make -C image lint                    # shellcheck + Python syntax
make -C image shell                   # shell in the builder container
make -C image clean | distclean       # outputs | outputs + all caches (root-owned)
```

`ARCH` defaults to the Docker host's architecture. `AGOS_WITH_AGENTD=auto|yes|no`
controls the agentd step (default `auto`: install it if it builds, otherwise
build the image without it and say so in `/usr/lib/agos/build-info.json`; CI
should use `yes`). `MKOSI_FORCE=-ff` also drops mkosi's incremental cache;
`MKOSI_ARGS=...` passes extra mkosi options (e.g. `--incremental=no`).

Boot the result anywhere that does UEFI, with a NoCloud seed for config. For
example with plain QEMU (what `boot-test` does, see `scripts/boot_test.py`):

```sh
qemu-img create -f qcow2 -F qcow2 -b agos-0.1.0-amd64.qcow2 vm.qcow2 40G   # the root fs grows to fill it
qemu-system-x86_64 -machine q35,accel=kvm -cpu host -smp 4 -m 4096 \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd \
  -drive if=pflash,format=raw,file=my_vars.fd \
  -drive file=vm.qcow2,if=virtio -cdrom seed.iso \
  -nic user,hostfwd=tcp:127.0.0.1:10022-:22 -device virtio-vga
```

## How the build works

```
image/
  builder/Dockerfile     pinned toolchain: debian:trixie-slim@sha256, mkosi v27.1 (git, commit-checked),
                         systemd-repart/ukify/bootctl 257, qemu, OVMF/AAVMF, xorriso, uv
  Makefile               docker wrappers; mkosi runs --privileged (mount namespaces, no loop devices)
  pins.env               KasmVNC .deb sha256s, Tailscale + Claude Code apt key hashes/fingerprints
  scripts/sync.sh        fetch + verify pinned inputs into mkosi.packages/ and mkosi.sandbox/
  scripts/build.sh       sync, export agentd's uv.lock + build its wheel, mkosi build, chown outputs
  scripts/convert.sh     raw -> qcow2 (zlib) / raw.xz, SHA256SUMS
  scripts/boot_test.py   zero-touch boot test (boot-test.sh wraps it); vm-ssh.sh, viewer-check.sh helpers
  mkosi.conf             distribution, package list, boot, output (see comments)
  mkosi.conf.d/          per-architecture: kernel package, output name
  mkosi.profiles/agents  Claude Code (profile on by default)
  mkosi.repart/          build-time partitions: ESP (512M, vfat) + root (ext4, minimal, grow flag)
  mkosi.skeleton/        Debian deb822 sources (keeps mkosi from writing -debug/deb-src lists)
  mkosi.sandbox/         Tailscale + Claude Code apt sources for the build (keys fetched by sync.sh)
  mkosi.extra/           every file the image ships (configs, units, agos-firstboot, CLIs)
  mkosi.postinst.chroot  users, kernel cmdline, masks, panel seed, vendor repos, agentd venv, build-info
  mkosi.version          ImageVersion from ../VERSION
```

- **Tools tree:** `ToolsTree=no`. The builder container *is* the pinned tools
  tree, so mkosi needs no second distribution bootstrap. Only the target
  architecture's KasmVNC `.deb` is placed in `mkosi.packages/`, which mkosi
  turns into a local apt repository.
- **Caches:** `mkosi.cache/` (incremental image after package install),
  `.cache/pkgcache/` (debs), workspace in the Docker volume
  `agos-mkosi-workspace` (mkosi refuses a workspace inside a build source).
  Caches keep root ownership on purpose (they hold the image tree);
  `make distclean` removes them from inside the container. Outputs are
  chowned back to the invoking user.
- **Boot chain:** systemd-boot (`timeout 0`), installed by `bootctl
  --all-architectures`, so the removable-media fallback `EFI/BOOT/BOOTX64.EFI`
  / `BOOTAA64.EFI` exists and the image boots from empty EFI variables
  (Proxmox `pre-enrolled-keys=0`). mkosi builds an unsigned UKI with its own
  systemd initrd (`KernelInitrdModules=default` + Hyper-V/VMware/Xen storage
  drivers; 48 MB). Secure Boot is off in v0.1.
- **Kernel updates:** `linux-image-*` depends on initramfs-tools, and
  `/etc/kernel/install.conf` (`layout=bls`), `/etc/kernel/entry-token`
  (`agos`) and `/etc/kernel/cmdline` (`root=PARTLABEL=agos-root ...`) make
  Debian's `zz-systemd-boot` hook write a Type #1 entry (kernel +
  initramfs-tools initrd) to the ESP; it sorts above the build-time UKI.
  Verified by reinstalling the kernel package in a test VM and rebooting into
  the new entry unattended.
- **agentd:** `scripts/build.sh` runs `uv export --frozen` on `agentd/uv.lock`
  (hash-pinned requirements) and `uv build --wheel` (never writes into
  `agentd/`); the postinst creates `/opt/agentd` with
  `python3 -m venv --system-site-packages` and installs exactly the locked set
  with `pip --require-hashes --no-deps --ignore-installed`, then `pip check`.
  Without a lock it falls back to `pip install ./agentd`.
- **Claude Code:** Anthropic's signed apt repository, stable channel
  (`downloads.claude.ai/claude-code/apt/stable`, key fingerprint
  `31DD DE24 DDFA B679 F42D 7BD2 BAA9 29FF 1A7E CACE`), the official
  Linux-package-manager install method; system-wide `/usr/bin/claude`, works
  offline at first boot, and is updated by unattended-upgrades. The repository
  and key are installed into the image for that.

## What is in the image

Debian 13 with `main contrib non-free-firmware`, `WithRecommends=no`, no docs,
and an explicit package list (`mkosi.conf`): kernel, systemd (+resolved,
timesyncd, repart, boot), dbus-user-session, sudo, polkitd, openssh, cloud-init,
qemu-guest-agent, unattended-upgrades; XFCE (session, xfwm4, panel,
xfdesktop, settings, terminal, notifyd, appfinder, thunar), fonts (DejaVu,
Liberation, Noto core + color emoji, Droid fallback for CJK); KasmVNC 1.5.0;
xdotool, xclip, wmctrl, x11-utils, x11-xserver-utils, ffmpeg, at-spi2-core,
python3-gi, gir1.2-atspi-2.0; chromium, firefox-esr; tailscale; python3,
python3-venv/pip, nodejs, npm, git, curl, jq, ripgrep, tmux and friends;
claude-code (profile `agents`). The JSON package manifest lands next to the
image (`agos-<ver>-<arch>.manifest`).

| Path | What |
|---|---|
| `/usr/lib/agos/agos-firstboot` | first boot / apply (Python 3 stdlib), also `--wait-ready` |
| `/usr/lib/agos/defaults.toml` | configuration layer 1 |
| `/usr/lib/agos/agos-display`, `agos-session`, `wait-display` | Xvnc and XFCE launchers for the user units |
| `/usr/lib/agos/viewer-perm {view\|control\|status}` | KasmVNC viewer write toggle (agentd takeover hooks) |
| `/usr/lib/agos/build-info.json` | version, arch, kernel, KasmVNC, Claude Code, agentd status, build time |
| `/usr/bin/agos` | `agos status [--json]`, `agos apply`, `agos viewer view\|control\|status`, `agos version` |
| `/usr/bin/agos-browser` | Chromium with the spec's flags, profile `~/.config/agos-chromium`, CDP 127.0.0.1:9222 |
| `/opt/agentd` | agentd venv (`--system-site-packages`) |
| `/etc/agos/{config.toml,secrets.env}` | configuration layer 3 (cloud-init `write_files`) |
| `/var/lib/agos/state.json` | `starting` → `ready` / `error` (+`message`), no secrets |
| `/run/agos/config.json` | effective merged config, same structure as `config.toml` |

Units (spec "Guest image"): user units `agos-display` (Xvnc, `Upholds=`
the session so XFCE comes back after an Xvnc restart), `agos-session`
(`BindsTo` the display), `agentd` (`Type=notify`, `WatchdogSec=30`), all
`Restart=always` with no start-rate limit, enabled through
`/usr/lib/systemd/user-preset/` and `ConditionUser=agent`. System units
`agos-firstboot` (after cloud-init's config stage, `Before=user@1000.service`),
`agos-ready`, `agos-ssh-keygen`, plus the presets in
`/usr/lib/systemd/system-preset/10-agos.preset` (`tailscaled` disabled until
configured, `ssh.socket` off, `ssh.service` on, ...).

KasmVNC runs without its interactive `vncserver` wrapper: `agos-display`
execs `Xvnc :1` with the arguments the wrapper derives from `kasmvnc.yaml`
(read from the wrapper's source and a live 1.5.0 server), plus
`-AcceptSetDesktopSize 0` (viewers cannot resize the agent's screen),
`-QueryConnect 0`, `-nolisten tcp`, and `-disableBasicAuth` only for a
loopback viewer without a password. Users live in `~/.kasmpasswd`
(`name:$5$kasm$<sha256-crypt>:perms`, exactly what `kasmvncpasswd` writes;
rendered with `openssl passwd -5 -salt kasm`). KasmVNC 1.5 permissions are
`r` (see), `w` (input), `o` (owner/API): the viewer is `r` while agentd
arbitrates control and `rw` otherwise; `agos-admin` (`rwo`, random password
in `/var/lib/agos/private/`) is the owner account `viewer-perm` uses for
`/api/update_user`. The X cookie is written both as FamilyLocal (python-xlib,
used by agentd, only matches that) and FamilyWild.

## First boot and configuration

`agos-firstboot.service` runs on every boot (first-boot-only work is guarded
by `/var/lib/agos/firstboot.done`):

1. Merge `/usr/lib/agos/defaults.toml` < an `AGOS`-labelled filesystem
   (mounted read-only, `config.toml`/`secrets.env` copied to
   `/var/lib/agos/agos-fs/`) < `/etc/agos/{config.toml,secrets.env}` <
   systemd credentials `agos.config`/`agos.secrets` (`ImportCredential=`).
   Validate every key (bad values fall back to the default and are recorded as
   errors), write `/run/agos/config.json`.
2. Generate `AGENTD_TOKEN` (`agd_...`) and, when the viewer is reachable from
   outside the VM (LAN mode or `tailscale serve`), `VIEWER_PASSWORD`; append
   them to `/etc/agos/secrets.env` (0600 root).
3. Render for `agent`: `~/.config/agos/display.env`, a self-signed viewer
   certificate, `~/.kasmpasswd`, `~/.config/agos/kasmvnc-api.json` (0600),
   `~/.config/agentd/config.toml` (only keys the installed agentd accepts,
   `mcp_mode = "remote"`, `viewer_url`, takeover hooks) and `tokens.toml`
   (`[[token]] name = "agos-admin"`, sha256 of `AGENTD_TOKEN`, scope `admin`;
   other tokens are kept), `~/.config/environment.d/50-agos-secrets.conf`
   (also sourced by login shells via `/etc/profile.d/agos.sh`), the Claude Code
   pre-seed (merged, never replaced: `bypassPermissions`,
   `skipDangerousModePermissionPrompt`, `theme`, `hasCompletedOnboarding`,
   trusted `~` and `~/work`, MCP server `desktop` → `/opt/agentd/bin/agentd
   mcp`, pre-approved `ANTHROPIC_API_KEY`), the console banner
   `/etc/issue.d/agos.issue`, and the hostname if cloud-init did not set one.
4. Tailscale when `enabled = true`, or `auto` with `TS_AUTHKEY` (or an earlier
   join): enable `tailscaled`, `tailscale up --auth-key=file:...` (OAuth client
   secrets get `?preauthorized=true&ephemeral=false`, plus `--advertise-tags`,
   `--hostname`, `--ssh`), `tailscale serve --bg --yes` for the viewer (:443)
   and agentd (:8765), all time-bounded with stdin closed. One-off
   `tskey-auth-` keys are removed from disk after the join.
5. `state.json` = `starting`; `agos-ready.service` then waits for Xvnc, the
   XFCE panel, the viewer port and agentd's `/v1/health`, and writes `ready`
   (with `ready_uptime`) or `error` with a `message`.

`sudo agos apply` re-runs the whole thing after editing `/etc/agos/*` and
restarts the display and agentd.

## Boot test

`make -C image boot-test ARCH=amd64` (in the builder container, `/dev/kvm`
passed through, host networking so the loopback-only forwards are reachable)
boots `mkosi.output/agos-<ver>-<arch>.qcow2` through a 20 GiB overlay with
OVMF, user-mode networking (`hostfwd` on 127.0.0.1 only: 10022→22,
18765→8765, 18444→8444), an i6300esb watchdog, a QEMU guest agent channel,
the serial console in `out/boot-test-<arch>/serial.log`, and **no keyboard
input ever**. Config arrives through all four layers: a generated NoCloud
`CIDATA` ISO (SSH key, `/etc/agos/config.toml` in LAN mode, fixed
`AGENTD_TOKEN`/`VIEWER_PASSWORD`), an `AGOS` FAT disk, and an SMBIOS
credential that overrides the display size. It checks: state `ready` (also
read through the guest agent, as the Proxmox script does), `systemctl
is-system-running` = running, config layer precedence, file modes, kernel
cmdline, root growth, unique machine-id/host keys, user units, the viewer
(401 without / 200 with basic auth over TLS), agentd (health, token, 401
without), pinned geometry, no dialog windows, agentd click/type into a
terminal, takeover → `HUMAN_IN_CONTROL` → handback → `STALE_FRAME` until a new
screenshot, the KasmVNC write toggle via the hooks, `agos-browser` (one
Chromium window, CDP up), Firefox ESR without first-run pages, the Claude
Code pre-seed, idle memory, and an unattended reboot back to `ready`.
Results: `out/boot-test-<arch>/results.json`; screenshots `out/<arch>-*.png`.
`BOOT_TEST_ARGS=--keep` leaves the VM running; then `scripts/vm-ssh.sh` opens
a shell and `scripts/viewer-check.sh` opens the KasmVNC web client in a pinned
Playwright Chromium (TLS, basic auth, WebSocket) and saves
`out/<arch>-viewer.png`.

## Measured (amd64, this workstation)

8 vCPU host, KVM, VM with 4 vCPU / 4 GiB, run of 2026-10-05:

| | |
|---|---|
| Raw image | 4.55 GiB (4,889,022,464 B; sparse, 4.1 GiB allocated): minimal ext4 root + 512 MiB ESP |
| qcow2 (zlib) | 1.13 GiB (1,213,661,184 B) |
| raw.xz (`xz -6`) | 789 MiB (827,357,956 B) |
| UKI on the ESP | 48 MB (130 MB before limiting the initrd's modules) |
| Build | 220 s cold (empty caches, all downloads), 84 s warm (incremental cache); qcow2 + xz 192 s |
| First boot | 12.5 s to graphical.target (kernel 3.7 s + initrd 2.3 s + userspace 6.5 s); from QEMU start (OVMF included): SSH after 19.2 s, `state.json` `ready` after 22.4 s (16.9 s guest uptime) |
| Reboot | `ready` 18.3 s after `systemctl reboot` (11.7 s guest uptime) |
| Idle RAM | 525 MiB used (`free`) with Xvnc, XFCE, agentd and no browser; largest RSS: agentd 79 MiB, xfce4-session 74 MiB, Xvnc 65 MiB |
| Root growth | 20 GiB disk -> 19.1 GiB root on first boot (systemd-repart + growfs) |
| Tailscale failure path | bogus `TS_AUTHKEY`: `state: error`, `message: "tailscale up failed: ... invalid key"` after 2 s; desktop unaffected |

Screenshots of the verified run: `out/amd64-desktop.png`,
`amd64-agentd-typed.png`, `amd64-chromium.png`, `amd64-firefox.png`,
`amd64-desktop-after-reboot.png`, `amd64-viewer.png`.

## Zero-touch hardening

Every row of the report's hardening table that applies to a VM guest, plus
the prompts found while testing. Firmware/Secure Boot/LUKS rows are the
hypervisor's job (VM-first, no MOK, no encryption).

| Prompt / failure it prevents | Fix | Where |
|---|---|---|
| Boot menu wait | systemd-boot `timeout 0`, `editor no` | `/efi/loader/loader.conf` (postinst) |
| Empty EFI vars (fresh Proxmox VM) cannot find a loader | fallback `EFI/BOOT/BOOTX64.EFI` / `BOOTAA64.EFI` | `bootctl --all-architectures` (mkosi) |
| fsck questions | `fsck.repair=yes` | `/etc/kernel/cmdline` |
| Kernel panic / oops waits forever | `panic=10 oops=panic` | `/etc/kernel/cmdline` |
| Hard hangs | `RuntimeWatchdogSec=30s`, `RebootWatchdogSec=2min` (verified with i6300esb) | `system.conf.d/90-agos.conf` |
| 90 s "A stop job is running" on reboot | `DefaultTimeoutStopSec=20s` | `system.conf.d/90-agos.conf` |
| Missing ESP drops boot to emergency shell | ESP `nofail,x-systemd.device-timeout=10s,x-systemd.automount` | `/etc/fstab` |
| Console prompts for locale/keymap/timezone/root password on first boot | `systemd-firstboot.service` masked (mkosi sets them at build) | postinst |
| Network without carrier stalls boot 2 min | `systemd-networkd-wait-online --any --timeout=30` | unit drop-in |
| Cloned identity | machine-id `uninitialized`, SSH host keys and the ssl-cert snakeoil key removed at build, regenerated per VM (cloud-init / `agos-ssh-keygen` / firstboot) | `mkosi.conf` `RemoveFiles=` |
| Login screen / DM autologin quirks | no display manager: Xvnc + XFCE as lingering user units, `Restart=always`, `StartLimitIntervalSec=0` | `user/agos-*.service` |
| Xfce panel "first start" chooser | `xfce4-panel.xml` seeded system-wide from `default.xml`, uninstalled plugins dropped ("plugin could not be loaded" dialog) | postinst |
| Screen blanking, screensaver, DPMS | `xset s off -dpms s noblank` at session start; no screensaver/locker/power manager installed; xfconf seeds for power-manager and xfce4-screensaver in case they appear | `agos-session`, `/etc/xdg/xfce4/xfconf/` |
| Lock screen / logout confirmation / restored apps | xfce4-session `LockScreen=false`, `PromptOnLogout=false`, `SaveOnExit=false` | `agos-session` |
| Suspend / hibernate | sleep targets masked, `AllowSuspend=no` etc., logind lid/suspend keys/idle ignored | `sleep.conf.d`, `logind.conf.d`, postinst |
| Polkit "Authentication is required" (colord in VNC, etc.) | JS rule: `agent` passes every action | `/etc/polkit-1/rules.d/49-agos.rules` |
| Autostarted nags (lockers, keyring/polkit agents, updaters, user-dirs, xiccd, nm-applet ...) | `Hidden=true` overrides | `~agent/.config/autostart/` |
| "Unlock keyring" | no gnome-keyring; Chromium `--password-store=basic` for every launch | `/etc/chromium.d/agos`, `agos-browser` |
| exo "choose preferred browser/terminal" | `helpers.rc` + `mimeapps.list` → agos-browser, xfce4-terminal, thunar | `~agent/.config/` |
| Chromium first-run, default browser, sign-in, sync, passwords, autofill, translate, download location, notifications, geolocation, search-engine choice, background mode, tab discarding | managed policy (DoH off, DuckDuckGo default, CDP allowed) + flags | `/etc/chromium/policies/managed/agos.json`, `/etc/chromium.d/agos` |
| Chromium "Restore pages?" after a kill/reset | `--hide-crash-restore-bubble`, profile marked clean before launch | `agos-browser` |
| Chrome ≥136 refuses CDP on the default profile | dedicated `--user-data-dir` | `agos-browser` |
| Firefox Terms of Use, onboarding/welcome, privacy notice, default-browser check, studies, password/autofill prompts, download location, notifications/location, crash "Restore Session" page | enterprise policies (verified: Debian's ESR 153 reads `/etc/firefox/policies/`, **not** `/etc/firefox-esr/policies/`) | `/etc/firefox/policies/policies.json` |
| apt "Do you want to continue?" | `APT::Get::Assume-Yes` | `apt.conf.d/90agos` |
| dpkg conffile questions | `--force-confdef --force-confold` (apt and unattended-upgrades) | `apt.conf.d/90agos`, `52agos-unattended-upgrades` |
| debconf questions | debconf frontend `Noninteractive`, priority critical | postinst |
| needrestart dialogs | not installed; `restart='a'`, no kernel/ucode hints if it ever is | `/etc/needrestart/conf.d/90-agos.conf` |
| Reboots mid-task | unattended-upgrades for Debian-Security (+point releases), Tailscale, Claude Code, `Automatic-Reboot "false"` | `apt.conf.d/20auto-upgrades`, `52agos-unattended-upgrades` |
| cloud-init rewriting apt sources | `apt: preserve_sources_list: true` | `cloud.cfg.d/90-agos.cfg` |
| git credential prompts | `GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never` | `/etc/environment`, `environment.d/60-agos.conf` |
| pip / npm confirmations | `PIP_NO_INPUT=1`, `npm_config_yes=true` | same |
| ssh host-key and password prompts | `StrictHostKeyChecking accept-new`, `BatchMode yes` | `/etc/ssh/ssh_config.d/10-agos.conf` |
| gpg pinentry dialogs | `pinentry-mode loopback` | `~agent/.gnupg/gpg.conf` |
| Claude Code onboarding, theme picker, bypass-mode warning, trust dialog, API key approval | pre-seed (see first boot step 3); runs as non-root `agent` | firstboot |
| KasmVNC "accept connection?" / viewer resizes the agent's screen | `-QueryConnect 0`, `-AcceptSetDesktopSize 0` | `agos-display` |
| Log growth fills the disk | journald persistent, `SystemMaxUse=512M`, `SystemKeepFree=1G` | `journald.conf.d/90-agos.conf` |
| Hung (not crashed) agentd | `Type=notify` + `WatchdogSec=30` | `user/agentd.service` |
| Wrong clock breaks TLS | systemd-timesyncd enabled; firstboot ordered after `network-online.target` | presets |
| NetworkManager captive portal / secret prompts | not applicable: systemd-networkd only (cloud-init `networkd` renderer, DHCP fallback for `en*`/`eth*`) | `network/99-agos-dhcp.network` |
| Group-writable shipped files (Firefox then silently ignores its policies) | postinst strips group/other write from everything in `mkosi.extra/` and `mkosi.skeleton/` | postinst |

Security notes: SSH is keys only, no root login, root and `agent` passwords
locked; agentd always requires a bearer token on TCP; the viewer has TLS and
basic auth whenever it is reachable from outside the VM. `agent` has
passwordless sudo and a polkit allow-all rule by design: the hypervisor is the
boundary. `/etc/agos/secrets.env` is 0600 root, but cloud-init keeps its own
copy of user-data in `/var/lib/cloud` (root-only); both are readable by the
agent through sudo, which the threat model accepts (tier-1 secrets only).

## arm64 and CI

The recipe is architecture-neutral except `mkosi.conf.d/10-arm64.conf`
(`linux-image-arm64`, output name), the KasmVNC `trixie_1.5.0_arm64` package
(pinned in `pins.env`), `console=ttyAMA0` (postinst), the AAVMF firmware in
the boot test, and `EFI/BOOT/BOOTAA64.EFI` from `bootctl
--all-architectures`. `scripts/build.sh` refuses to cross-build: no binfmt or
qemu-user is ever registered. Build each architecture on a native runner.

CI commands (GitHub Actions; `ubuntu-24.04` for amd64, `ubuntu-24.04-arm`
for arm64):

```yaml
# both legs (matrix: {runner: ubuntu-24.04, arch: amd64}, {runner: ubuntu-24.04-arm, arch: arm64})
- uses: actions/checkout@v4
- name: Free disk space (the build needs ~20 GB)
  run: sudo rm -rf /usr/local/lib/android /usr/share/dotnet /opt/ghc /opt/hostedtoolcache
- run: make -C image build ARCH=${{ matrix.arch }} AGOS_WITH_AGENTD=yes MKOSI_ARGS=--incremental=no
- run: make -C image artifacts ARCH=${{ matrix.arch }}
- run: make -C image boot-test ARCH=${{ matrix.arch }}   # amd64: KVM; arm64 runner: TCG (no /dev/kvm), ~10x slower
- uses: actions/upload-artifact@v4
  if: always()
  with:
    name: agos-${{ matrix.arch }}
    path: |
      image/mkosi.output/agos-*-${{ matrix.arch }}.*
      image/mkosi.output/SHA256SUMS
      out/
```

The boot test picks KVM when `/dev/kvm` exists and otherwise TCG with a
3600 s timeout. arm64 runners have no KVM, so the arm64 boot test either runs
there under TCG or on an amd64 runner (download the arm64 qcow2 artifact into
`image/mkosi.output/`, then `make -C image boot-test ARCH=arm64`; the builder
image has `qemu-system-aarch64` and AAVMF). SHA256SUMS signing (minisign) and
release upload belong to the release workflow.

## Known gaps

- **arm64 is unverified**: the configuration resolves (`mkosi
  --architecture=arm64 summary`) and every arm64 input exists (KasmVNC arm64
  deb, `linux-image-arm64`, chromium/firefox-esr/tailscale/claude-code arm64
  packages, AAVMF), but no arm64 image has been built or booted here (no
  native arm64 host, binfmt deliberately not used). Expect the first CI run to
  surface something.
- **Tailscale is untested end to end** (no tailnet here): `tailscale up` with
  an auth key / OAuth secret, `tailscale serve --yes` (requires HTTPS + Serve
  enabled for the tailnet), and KasmVNC's web client behind `serve` (expected
  to work: `https+insecure`/`http` backends, WebSocket). Also: behind `serve`
  every viewer reaches KasmVNC from 127.0.0.1, so its brute-force blacklist
  (5 failures → 10 s) is shared by all tailnet viewers.
- KasmVNC comes from a pinned `.deb`, not an apt repository: it gets no
  automatic security updates; bump `pins.env` and rebuild.
- Debian packages are whatever trixie (+security) serves at build time;
  builds are not bit-for-bit reproducible. `MKOSI_ARGS=--snapshot=<id>`
  (snapshot.debian.org) pins them when needed.
- The build-time kernel's `/boot/vmlinuz-*` is not in the root filesystem
  (mkosi moves boot files to the ESP), so `dpkg-reconfigure` of *that* kernel
  cannot regenerate its entry; `apt-get install --reinstall
  linux-image-$(uname -r)` or any newer kernel works (verified).
- The viewer starts view-only whenever basic auth is on and agentd is
  installed; without agentd's UI or CLI a human gains control with
  `agos viewer control` (or the `agos-admin` owner login).
- A config file with an invalid value boots with the default for that key
  but ends in `state: error` (so the Proxmox script reports it).
- No GPU: Xvnc, Chromium and Firefox render in software.
- `oops=panic` turns any kernel oops into a reboot (report recommendation);
  remove it from `/etc/kernel/cmdline` if a flaky driver causes reboot loops.

## Updating pinned inputs

- KasmVNC: new release → update `KASMVNC_*` in `pins.env` (sha256 = the
  GitHub release asset `digest`), check `agos-display`'s arguments against the
  new `vncserver` wrapper.
- Keys: `TAILSCALE_KEYRING_*`, `CLAUDE_CODE_KEY_*` in `pins.env`
  (`scripts/sync.sh` verifies both sha256 and fingerprint).
- Builder: base image digest, `MKOSI_TAG`/`MKOSI_COMMIT`, `UV_VERSION`/sha256s
  in `builder/Dockerfile` (the Makefile tags the builder by the Dockerfile's
  hash, so a change rebuilds it).
