# Deploying agos

agos ships as one UEFI disk image per architecture (`amd64`, `arm64`) and is
configured at first boot by **one cloud-init NoCloud seed**: the same
`user-data` works on every hypervisor. This directory holds:

| Path | What |
|---|---|
| [`proxmox/agos-proxmox.sh`](proxmox/agos-proxmox.sh) | Proxmox VE 8.x/9.x installer: create, status, reset, destroy |
| [`proxmox/tests/`](proxmox/tests/) | Fake-Proxmox test harness (runs in Docker) |
| [`cloud-init/`](cloud-init/) | Example `user-data`, `meta-data`, `network-config`, and `make-seed.sh` |
| [`install.md`](install.md) | Runbook an AI agent (e.g. Claude Code on the Proxmox host) can execute |
| [`claude-settings.example.json`](claude-settings.example.json) | Claude Code permissions for that runbook |

Release assets (GitHub release `v<version>` of `kroqdotdev/agos`, see
[`docs/spec.md`](../docs/spec.md)): `agos-<version>-<arch>.qcow2`,
`agos-<version>-<arch>.raw.xz`, `agos-proxmox.sh`, `SHA256SUMS`,
`SHA256SUMS.minisig`.

> **Status (v0.1.1):** the Proxmox path (guided setup and flags: create,
> status, reset, destroy, `--isolate`) is verified on a real Proxmox VE 9.1
> host, besides the fake-Proxmox test suite. Every other platform section is
> written from vendor documentation and is marked *untested*. Please report
> what works.

## Contents

- [Before you start: secrets, config, SSH key](#before-you-start-secrets-config-ssh-key)
- [Proxmox VE](#proxmox-ve)
- [Building a seed for other hypervisors](#building-a-seed-for-other-hypervisors)
- [QEMU and libvirt](#qemu-and-libvirt) · [Incus](#incus) · [UTM on Apple Silicon](#utm-on-apple-silicon) · [VirtualBox](#virtualbox) · [Hyper-V](#hyper-v) · [TrueNAS and Unraid](#truenas-and-unraid)
- [Reaching the desktop](#reaching-the-desktop)
- [Security notes](#security-notes)
- [Testing this directory](#testing-this-directory)

## Before you start: secrets, config, SSH key

Everything is optional; with nothing at all the VM boots, but you can only
reach it through the hypervisor (on Proxmox: `qm guest exec`).

**Secrets** are `KEY=value` lines. On Proxmox they live in
`/root/agos.secrets` (root-owned, mode 0600); elsewhere they go into the
`/etc/agos/secrets.env` block of [`cloud-init/user-data`](cloud-init/user-data).
Never put them on a command line.

```bash
(umask 077; touch /root/agos.secrets)    # creates it 0600; then edit it:
nano /root/agos.secrets
```

```ini
# Tailscale OAuth client secret (scope auth_keys, tag:agos); does not expire.
# A plain auth key also works but expires after at most 90 days.
TS_AUTHKEY=tskey-client-XXXXXXXXXXXX-XXXXXXXXXXXXXXXXXXXXXXXXXXXX
# KasmVNC password for user "agos" (generated if unset in LAN mode)
VIEWER_PASSWORD=pick-a-long-one
# Bearer token for agentd's API (generated if unset)
# AGENTD_TOKEN=
# Exported into the agent user's environment; use a per-VM key with a spend cap
ANTHROPIC_API_KEY=sk-ant-...
# CLAUDE_CODE_OAUTH_TOKEN=
# OPENAI_API_KEY=
```

For Tailscale, create an OAuth client in the admin console (Settings → OAuth
clients) with the `auth_keys` scope and the tag `tag:agos`, declare the tag
under `tagOwners`, and keep the tailnet policy from letting `tag:agos`
*initiate* connections (see [Security notes](#security-notes)). Per the spec,
first boot joins with `?preauthorized=true&ephemeral=false` and
`--advertise-tags`.

**Config** (`config.toml`, non-secret, optional): every key with its default is
in the `write_files` block of [`cloud-init/user-data`](cloud-init/user-data).
The one most people change is LAN mode:

```toml
[viewer]
listen = "0.0.0.0"     # desktop at https://<vm-ip>:8444 (self-signed), user agos
```

**SSH**: a public key for user `agent` (`--ssh-key-file` on Proxmox, the
`users:` block elsewhere). Password login is off.

## Proxmox VE

Runs on the Proxmox host as root. Supports PVE 8.x and 9.x on amd64, and PVE
9.2+ on arm64 (Grace/Vera and other UEFI Armv9 servers; not Raspberry Pi).

### Guided setup (one line)

Paste this into the Proxmox host's shell (web UI → node → Shell, or SSH) as
root. Everything after that is pickers and inputs; nothing changes until you
confirm the summary, and ESC quits at any point with "aborted, nothing
changed".

```bash
bash -c "$(curl -fsSL https://kroq.dev/tools/agos-proxmox.sh)"
```

The dialogs, in order:

1. Banner and preflight: root, Proxmox VE version, architecture, KVM (✓/✗).
2. "Create a new agos VM?"
3. **Settings**: *Default* (next free VMID, name `agos`, 4 cores, 8 GiB RAM,
   64 GiB disk on the images storage with the most free space, bridge
   `vmbr0`, start at boot) or *Advanced*, which asks for each of those (a menu
   of your storages with free space, a menu of your bridges), the CPU type
   (`host` or `x86-64-v2-AES`), start at boot and the experimental
   [isolation](#isolation---isolate-experimental).
4. If `/root/agos.secrets` exists: use it? Only what it lacks is asked for
   later.
5. **Access**: *Tailscale* (recommended; paste an OAuth client secret or auth
   key, see the tags note below), *LAN* (desktop on `https://<vm-ip>:8444`,
   optional password, blank = generated), or *SSH tunnel only*.
6. **SSH keys** for user `agent`: a checklist of the keys in
   `/root/.ssh/authorized_keys` (type, short fingerprint, comment; all ticked),
   then an optional box to paste one more public key.
7. **Agent apps**: a checklist of *Claude Code* (pre-seeded so it never asks)
   and *T3 Code* (desktop app on the VM's second workspace, see
   [T3 Code](#t3-code)), both ticked; written to the VM's `config.toml` as
   `[agents] claude_code` / `t3code`. Both are in the image either way.
8. Optional **agent credentials**: an Anthropic API key or Claude Code OAuth
   token, and an OpenAI API key (blank = skip).
9. **Summary** (the same plan text as `--dry-run`), then *Create*, *Show the
   exact commands first*, or *Quit*.
10. Progress in the terminal, then a final box with the desktop URL, viewer
   user and password, agentd URL and token, the SSH command and the
   reset/destroy commands. After it closes the terminal shows the same
   summary without the password and token.

Tailscale tags: an OAuth client secret (`tskey-client-…`) can only create
tagged devices, so the wizard tags the VM `tag:agos` (the client must be
allowed to assign it). A plain auth key (`tskey-auth-…`) joins untagged,
because forcing a tag fails unless your tailnet policy's `tagOwners` lets you
use it. *Advanced* lets you edit the tags; they go into the VM's
`config.toml`.

Secrets you type stay in memory and in one private temp file (mode 0600,
deleted on every exit) that only feeds the VM's seed. They are never passed as
command-line arguments, never printed to the terminal or logs, and the final
box receives the generated password and token through an anonymous memory
file. Long boxes that do not fit your terminal scroll: use the arrow keys,
then Tab and Enter.

The wizard opens only for a bare invocation on a terminal (`curl … | bash`
works too, the dialogs are drawn on `/dev/tty`), or with `--wizard`. Any of
`--yes`, `--json`, `--dry-run`, another subcommand, `--no-wizard`, or no
terminal keeps it off and the script behaves exactly as described under
flags below. It needs `whiptail`, which Proxmox VE ships; without it the
script says `apt install whiptail` (or use flags).

### Unattended: flags (scripts and agents)

The same one-liner takes flags after a dummy `$0`; with flags there are no
dialogs:

```bash
bash -c "$(curl -fsSL https://kroq.dev/tools/agos-proxmox.sh)" _ --dry-run --ssh-key-file /root/.ssh/authorized_keys
```

Download, verify, read, run (recommended, and the only form the agent runbook
uses):

```bash
cd /root
VER=0.1.1
BASE=https://github.com/kroqdotdev/agos/releases/download/v$VER
curl -fsSLO --proto '=https' --tlsv1.2 "$BASE/agos-proxmox.sh"
curl -fsSL --proto '=https' --tlsv1.2 -o agos-SHA256SUMS "$BASE/SHA256SUMS"
curl -fsSL --proto '=https' --tlsv1.2 -o agos-SHA256SUMS.minisig "$BASE/SHA256SUMS.minisig"
minisign -Vm agos-SHA256SUMS -x agos-SHA256SUMS.minisig -P RWQeJEg8BJM+C3FntUGlSkbqk9PU0z3FPLJS3z4fsjfXedsmchg2OFu1   # apt install minisign
sha256sum --ignore-missing -c agos-SHA256SUMS          # must print "agos-proxmox.sh: OK"
less agos-proxmox.sh
bash agos-proxmox.sh --dry-run --ssh-key-file /root/.ssh/authorized_keys
bash agos-proxmox.sh --yes     --ssh-key-file /root/.ssh/authorized_keys
```

Release signing key (minisign, key id `0B3E93043C48241E`):

```
RWQeJEg8BJM+C3FntUGlSkbqk9PU0z3FPLJS3z4fsjfXedsmchg2OFu1
```

The same key is built into the script, which checks every image download
against it, using `minisign` when installed and `openssl` otherwise. A
release without a valid `SHA256SUMS.minisig` is refused.

Typical runs:

```bash
bash agos-proxmox.sh --dry-run --json                 # machine-readable plan
bash agos-proxmox.sh --yes --storage local-zfs --bridge vmbr1 --cores 8 --memory 16384 --disk 128G
bash agos-proxmox.sh --yes --name agos-2              # a second VM
bash agos-proxmox.sh status                           # URLs, IP, state of all agos VMs
bash agos-proxmox.sh reset --name agos --yes          # back to the golden snapshot
bash agos-proxmox.sh destroy --name agos --yes        # delete VM, disks, snapshots
```

### What `create` does

1. **Preflight** (nothing changes yet): root; `pveversion` is 8.x or 9.x;
   `dpkg --print-architecture` is amd64 or arm64; `/dev/kvm` exists; the
   bridge exists; picks the active storage with `images` content and the most
   free space (or `--storage`) and one with `iso` content (`local` if possible,
   or `--iso-storage`); next free VMID from `pvesh get /cluster/nextid` (or
   checks `--vmid`); checks the secrets file is root-owned 0600 `KEY=value`
   (reporting only key names); validates config, SSH keys and network-config.
   If an agos-tagged VM with the same name already exists it reports it and
   exits 0.
2. **Image**: downloads `SHA256SUMS` + `SHA256SUMS.minisig` (signature
   checked with minisign or openssl against the built-in key) and `agos-<ver>-<arch>.qcow2` into
   `/var/cache/agos/<ver>/`, and checks the SHA-256. A cached image is reused
   only if it still matches.
3. **VM**: `qm create` with q35 + OVMF (amd64; arm64 uses PVE's `virt` machine
   and AAVMF), Secure Boot off (`pre-enrolled-keys=0`), `cpu host`,
   `virtio-scsi-single` with `iothread`, `discard`, `ssd`, guest agent,
   `--vga virtio`, `--tablet 1` (absolute pointer in noVNC), `--serial0
   socket`, `--onboot 1`, `--tags agos`, `net0` virtio on the bridge. The disk
   is imported with `import-from` (falling back to `qm disk import`) and
   grown to `--disk`.
4. **Seed**: builds its own NoCloud ISO (label `CIDATA`: `user-data`,
   `meta-data` with a fixed `instance-id`, optional `network-config`) with
   `genisoimage`, copies it to the ISO storage (mode 0600) and attaches it as
   a CD-ROM (`ide2`; `scsi1` on arm64, which has no IDE bus). It does **not**
   add a PVE cloud-init drive. The secrets file is streamed into the seed
   base64-encoded; it never appears in a command line, log or JSON.
5. **First boot**: starts the VM and polls `qm agent <vmid> ping`, then
   `qm guest exec <vmid> -- cat /var/lib/agos/state.json` until
   `"state": "ready"` (default timeout 900 s).
6. **Finish**: ejects and deletes the seed ISO, takes the `golden` snapshot,
   prints the URLs and how to read the generated credentials.

If a VM operation fails before the VM starts, the script destroys the VM it
created in that run (never any other). If first boot times out or the guest
reports a failure, the VM is kept for inspection and the seed is still
deleted; destroy it and run `create` again.

The script touches nothing else on the host: no packages, no telemetry, no
cluster or host firewall settings. Files it writes: `/var/cache/agos/`, a
lock in `/run/lock/`, the temporary seed ISO, and `/etc/pve/firewall/<vmid>.fw`
with `--isolate`.

### Flags

| Flag | Env | Default | Meaning |
|---|---|---|---|
| `--wizard` | | on a bare terminal run | open the guided setup |
| `--no-wizard` | `AGOS_NO_WIZARD=1` | | never open it |
| `--dry-run` | `AGOS_DRY_RUN=1` | | check everything, print the plan, change nothing |
| `--json` | `AGOS_JSON=1` | | one JSON object on stdout; progress on stderr |
| `-y`, `--yes` | `AGOS_YES=1` | | no confirmation prompt |
| `--vmid N` | `AGOS_VMID` | next free | VMID |
| `--name NAME` | `AGOS_NAME` | `agos` | VM name and guest hostname |
| `--storage ID` | `AGOS_STORAGE` | most free `images` storage | disk storage |
| `--iso-storage ID` | `AGOS_ISO_STORAGE` | `local` | where the seed ISO sits during first boot |
| `--bridge BR` | `AGOS_BRIDGE` | `vmbr0` | network bridge |
| `--cores N` | `AGOS_CORES` | 4 | vCPUs |
| `--memory MiB` | `AGOS_MEMORY` | 8192 | RAM |
| `--disk SIZE` | `AGOS_DISK` | `64G` | system disk |
| `--cpu TYPE` | `AGOS_CPU` | `host` | `x86-64-v2-AES` if the VM must live-migrate in a mixed cluster |
| `--onboot 0\|1` | `AGOS_ONBOOT` | 1 | start the VM when the host boots |
| `--version VER` | `AGOS_VERSION` | `0.1.1` | release to install |
| `--image-url URL` | `AGOS_IMAGE_URL` | GitHub release | https URL; `SHA256SUMS` must sit next to it |
| `--image-file PATH` | `AGOS_IMAGE_FILE` | | local qcow2, no download |
| `--image-sha256 HEX` | `AGOS_IMAGE_SHA256` | | expected hash instead of `SHA256SUMS` |
| `--require-signature` | `AGOS_REQUIRE_SIGNATURE=1` | | fail unless the minisign signature verifies |
| `--ssh-key-file PATH` | `AGOS_SSH_KEY_FILE` | | public keys for `agent` |
| `--secrets-file PATH` | `AGOS_SECRETS_FILE` | `/root/agos.secrets` | secrets (missing default file = no secrets) |
| `--config-file PATH` | `AGOS_CONFIG_FILE` | | `config.toml` |
| `--agents LIST` | `AGOS_AGENTS` | image defaults (both) | `claude-code,t3code`, one of them, or `none`: appends an `[agents]` table to the VM's `config.toml` (an error if `--config-file` already has one) |
| `--network-config PATH` | `AGOS_NETWORK_CONFIG` | DHCP | cloud-init network-config |
| `--isolate` | `AGOS_ISOLATE=1` | | experimental egress filter, see below |
| `--isolate-dns IP[,IP]` | `AGOS_ISOLATE_DNS` | | extra resolvers the isolated VM may use |
| `--timeout S` | `AGOS_TIMEOUT` | 900 | first-boot wait |

Also: `AGOS_REPO` (default `kroqdotdev/agos`, for forks), `AGOS_CACHE_DIR`
(default `/var/cache/agos`), `AGOS_MINISIGN_PUBKEY` (fork's signing key),
`AGOS_POLL_INTERVAL` (seconds, default 5). Flags override environment.

**Exit codes:** 0 ok (including "already exists"), 1 usage, confirmation
needed (no TTY and no `--yes`) or the guided setup was cancelled, 2 preflight failed, 3 download or
verification failed, 4 VM operation failed, 5 timed out waiting for first
boot. Interactive prompts appear only when stdin and stderr are terminals and
`--yes` is absent; without a terminal the script fails with exit 1 instead of
waiting. `destroy` always needs `--yes` plus `--vmid` or `--name`, and only
touches VMs tagged `agos`.

**JSON** (`--json`): the last line of stdout is one object. Success includes
`ok`, `action`, `vmid`, `name`, `node`, `status` (PVE), `state` (`ready`,
`booting`, `stopped`, `planned` for dry runs, `absent`, `destroyed`), `ip`,
`urls.viewer`, `urls.agentd`, `access` (`tailscale`, `lan`, `ssh-tunnel`),
`golden_snapshot`, `existing`. Failures are `{"ok": false, "exit_code": N,
"error": "..."}`. Dry runs add the full plan (`commands`, storages, secret key
names, firewall rules).

### Isolation (`--isolate`, experimental)

A root agent can flush any firewall inside its own VM, so the filter has to
live on the host. `--isolate` writes `/etc/pve/firewall/<vmid>.fw` and sets
`firewall=1` on `net0`. Outbound traffic from the VM to these destinations is
dropped: RFC 1918 (`10/8`, `172.16/12`, `192.168/16`), link-local and cloud
metadata (`169.254/16`), CGNAT (`100.64/10`), loopback, IPv6 ULA (`fc00::/7`),
IPv6 link-local TCP/UDP, and the host's own public addresses. DNS (port 53)
to the bridge's gateway, private resolvers from the host's `resolv.conf`, and
`--isolate-dns` addresses stays allowed, as do DHCP and NDP. Internet access
is unchanged; inbound is unchanged (`policy_in: ACCEPT`), so LAN mode keeps
working.

It **never** touches `cluster.fw` or host rules, and that matters: Proxmox
enforces VM rules only while the **datacenter firewall** is enabled, and it is
off by default. The script warns loudly when it is off. Enabling it
(Datacenter → Firewall → Options → Firewall: Yes) also switches on the host
firewall with input policy DROP; only the cluster's `local_network` keeps
access to the web UI (8006) and SSH. If you manage the host from elsewhere
(VPN, another subnet, a public IP), first add your addresses to the
`management` IPSet or set the datacenter input policy to ACCEPT, keep a shell
open, then enable it.

What it does not do yet: a separate `vmbr-agent` bridge, a filtering resolver
or a logging proxy (planned, see the report). DNS to the gateway still lets
the VM resolve LAN names, and the tailnet is reachable through the VM's own
Tailscale, so pair this with a tailnet policy that stops `tag:agos` from
initiating connections.

### Troubleshooting

- Exit 2: read the message; every preflight failure names the fix.
- Exit 3: the release or `SHA256SUMS` is missing, or a checksum or signature
  failed. Do not work around a failed signature.
- Exit 5: watch the VM's console in the PVE web UI (or `qm terminal <vmid>`
  if the image has a serial getty) and `qm guest exec <vmid> -- journalctl
  -u agos-firstboot`.
- `qm agent <vmid> ping` failing for minutes means the guest never got as far
  as starting `qemu-guest-agent`.
- Reclaim the image cache with `rm -rf /var/cache/agos`.

## Building a seed for other hypervisors

Copy [`cloud-init/`](cloud-init/), edit `user-data` (SSH key, secrets,
config), give `meta-data` a unique `instance-id`, then:

```bash
./make-seed.sh -o seed.iso                                   # DHCP
./make-seed.sh -o seed.iso --network-config network-config.static
```

It uses whichever of `genisoimage`, `xorriso`, `mkisofs`, `cloud-localds` or
(macOS) `hdiutil` exists, labels the volume `CIDATA`, writes it mode 0600, and
refuses to run while the placeholder SSH key is still in `user-data`. Attach
the ISO as a CD-ROM for the first boot, then detach and delete it.

Keep `instance-id` fixed for the life of a VM: a new one makes cloud-init
treat the VM as new and regenerate SSH host keys.

## QEMU and libvirt

*Untested.* Both images are UEFI-only; libvirt needs OVMF (`ovmf` package) or
AAVMF (`qemu-efi-aarch64`).

virt-install builds and attaches its own NoCloud ISO for the first boot:

```bash
sudo cp agos-0.1.1-amd64.qcow2 /var/lib/libvirt/images/agos.qcow2
sudo qemu-img resize /var/lib/libvirt/images/agos.qcow2 64G
virt-install --name agos --memory 8192 --vcpus 4 --cpu host-passthrough \
  --osinfo detect=on,require=off \
  --import --disk /var/lib/libvirt/images/agos.qcow2,bus=virtio,discard=unmap \
  --boot uefi,secure-boot=off \
  --cloud-init user-data=user-data,meta-data=meta-data \
  --network bridge=br0,model=virtio --graphics vnc --video virtio \
  --channel unix,target.type=virtio,target.name=org.qemu.guest_agent.0 \
  --noautoconsole
```

Older virt-install spells Secure Boot off as
`--boot uefi,firmware.feature0.name=secure-boot,firmware.feature0.enabled=no`.
Use `--network network=default` for NAT. Instead of `--cloud-init` you can
attach a `make-seed.sh` ISO: `--disk seed.iso,device=cdrom`.

Plain QEMU (amd64; arm64 is analogous with `qemu-system-aarch64 -machine
virt` and the `AAVMF_CODE.fd`/`AAVMF_VARS.fd` pair):

```bash
cp /usr/share/OVMF/OVMF_VARS_4M.fd agos-vars.fd
qemu-system-x86_64 -machine q35,accel=kvm -cpu host -smp 4 -m 8192 \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd \
  -drive if=pflash,format=raw,file=agos-vars.fd \
  -drive file=agos.qcow2,if=virtio,discard=unmap \
  -drive file=seed.iso,media=cdrom,readonly=on \
  -nic user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:2222-:22 \
  -device virtio-vga -device qemu-xhci -device usb-tablet
```

With user-mode networking, reach the desktop through the SSH port forward:
`ssh -p 2222 -N -L 8444:127.0.0.1:8444 agent@127.0.0.1`.

## Incus

*Untested.* Incus needs a metadata tarball next to the qcow2, Secure Boot off,
and (because the image has no `incus-agent`) the `cloud-init:config` disk that
carries the seed.

```bash
cat > metadata.yaml <<EOF
architecture: x86_64          # aarch64 for the arm64 image
creation_date: $(date +%s)
properties:
  description: agos 0.1.1
  os: debian
  release: trixie
EOF
tar -cJf agos-metadata.tar.xz metadata.yaml
incus image import agos-metadata.tar.xz agos-0.1.1-amd64.qcow2 --alias agos-0.1.1

incus init agos-0.1.1 agos --vm -c limits.cpu=4 -c limits.memory=8GiB \
  -c security.secureboot=false -d root,size=64GiB
incus config set agos cloud-init.user-data "$(cat user-data)"
incus config device add agos cloud-init disk source=cloud-init:config
incus start agos
incus console agos --type=vga
```

Use `incus init`, not `launch`, so the seed is in place before first boot.
Anything in `cloud-init.user-data` (including secrets) is readable by every
Incus admin through `incus config show`; detach a `make-seed.sh` ISO instead
(`incus config device add agos seed disk source=$PWD/seed.iso`) if that
matters.

## UTM on Apple Silicon

*Untested.* Use the **arm64** qcow2 with UTM's QEMU backend (Apple
Virtualization does not take qcow2). Build the seed on the Mac with
`./make-seed.sh` (uses `hdiutil`).

1. Create a New Virtual Machine → Virtualize → Other → skip the ISO.
2. Hardware: 8 GiB RAM, 4 cores. Storage: any size (you delete it next).
3. Before first start, edit the VM: Drives → delete the new drive → New
   Drive → Import → `agos-0.1.1-arm64.qcow2` (interface VirtIO); resize it if
   you like. New Drive → Removable, interface USB or VirtIO → select
   `seed.iso`.
4. Display: `virtio-gpu-gl-pci` or `virtio-ramfb`. Network: Shared Network.
5. Start. The VM gets a `192.168.64.x` address reachable from the Mac; use LAN
   mode or an SSH tunnel to reach the desktop. Remove the seed drive after
   first boot.

## VirtualBox

*Untested.* amd64 only.

```bash
qemu-img convert -p -O vdi agos-0.1.1-amd64.qcow2 agos.vdi
VBoxManage modifymedium disk agos.vdi --resize 65536
VBoxManage createvm --name agos --ostype Debian_64 --register
VBoxManage modifyvm agos --firmware efi --memory 8192 --cpus 4 \
  --graphicscontroller vmsvga --vram 64 --mouse usbtablet \
  --nic1 nat --natpf1 "ssh,tcp,127.0.0.1,2222,,22"
VBoxManage storagectl agos --name SATA --add sata --controller IntelAhci --portcount 2
VBoxManage storageattach agos --storagectl SATA --port 0 --device 0 --type hdd --medium agos.vdi
VBoxManage storageattach agos --storagectl SATA --port 1 --device 0 --type dvddrive --medium seed.iso
VBoxManage startvm agos --type headless
```

Then `ssh -p 2222 -N -L 8444:127.0.0.1:8444 agent@127.0.0.1`. Eject the seed
afterwards: `VBoxManage storageattach agos --storagectl SATA --port 1
--device 0 --type dvddrive --medium emptydrive`.

## Hyper-V

*Untested.* Generation 2 VM, Secure Boot off (the v0.1 image is not signed),
the seed as a DVD (Gen2 VMs take ISOs on their SCSI DVD drive).

```bash
# on Linux or WSL
qemu-img convert -p -O vhdx -o subformat=dynamic agos-0.1.1-amd64.qcow2 agos.vhdx
```

```powershell
Resize-VHD -Path C:\VMs\agos\agos.vhdx -SizeBytes 64GB
New-VM -Name agos -Generation 2 -MemoryStartupBytes 8GB -VHDPath C:\VMs\agos\agos.vhdx -SwitchName "Default Switch"
Set-VMProcessor -VMName agos -Count 4
Set-VMMemory -VMName agos -DynamicMemoryEnabled $false
Set-VMFirmware -VMName agos -EnableSecureBoot Off
Add-VMDvdDrive -VMName agos -Path C:\VMs\agos\seed.iso
Set-VMFirmware -VMName agos -FirstBootDevice (Get-VMHardDiskDrive -VMName agos)
Start-VM -Name agos
```

If Hyper-V rejects the converted disk, convert again with `Convert-VHD` on
Windows. Remove the DVD after first boot (`Remove-VMDvdDrive`).

## TrueNAS and Unraid

*Untested.* Neither ships ISO tools: build `seed.iso` with `make-seed.sh` on
another machine and copy it over with the qcow2.

- **TrueNAS SCALE 25.10 / 26**: put both files in a dataset under `/mnt/...`.
  Virtual Machines → Add: boot method UEFI with Secure Boot off; on the disk
  step choose to import the qcow2 into a new zvol (64 GiB+); add a CD-ROM
  device pointing at `seed.iso` (path must be under `/mnt/`); NIC type VirtIO
  on your bridge. The API path is `vm.device.convert` + `vm.create` +
  `vm.device.create`.
- **Unraid 7**: copy the qcow2 to `/mnt/user/domains/agos/vdisk1.qcow2`
  (`qemu-img resize` it to 64G). VM Manager → Add VM → Linux: BIOS OVMF,
  Primary vDisk "Manual" pointing at that file with bus VirtIO, a second
  CD-ROM set to `seed.iso`, network `br0` model virtio-net, graphics VNC.

## Reaching the desktop

The desktop is KasmVNC's web client on port 8444; agentd (REST `/v1`, MCP
`/mcp`, UI `/ui`) is on 8765. Both listen on `127.0.0.1` inside the VM unless
you choose LAN mode. Three ways in:

| Mode | When | Desktop | agentd |
|---|---|---|---|
| **Tailscale** (recommended) | `TS_AUTHKEY` set | `https://<hostname>.<tailnet>.ts.net/` | `https://<hostname>.<tailnet>.ts.net:8765/` |
| **LAN** | `[viewer] listen = "0.0.0.0"` | `https://<vm-ip>:8444/` (self-signed cert) | SSH tunnel |
| **SSH tunnel** | neither | `ssh -N -L 8444:127.0.0.1:8444 -L 8765:127.0.0.1:8765 agent@<vm-ip>`, then `http://127.0.0.1:8444/` | `http://127.0.0.1:8765/` through the same tunnel |

`agos-proxmox.sh status` prints the right one for each VM. In Tailscale mode
`tailscale serve` provides HTTPS with a real `*.ts.net` certificate and the
tailnet policy decides who gets in.

**Credentials.** The viewer user is `agos`; its password is
`VIEWER_PASSWORD` (generated at first boot in LAN mode if you did not set
one). Every agentd request needs `Authorization: Bearer <AGENTD_TOKEN>`
(generated if unset). Neither the script nor its JSON ever prints them;
read them from the VM:

```bash
# on the Proxmox host
qm guest exec <vmid> -- grep -E '^(VIEWER_PASSWORD|AGENTD_TOKEN)=' /etc/agos/secrets.env
# anywhere you have SSH
ssh agent@<vm-ip> sudo grep -E '^(VIEWER_PASSWORD|AGENTD_TOKEN)=' /etc/agos/secrets.env
```

### T3 Code

[T3 Code](https://github.com/pingdotgg/t3code) (desktop app for coding
agents) runs on the desktop's **second workspace**, so agents working on
workspace 1 never see it. In the viewer, click the second box of the
workspace switcher in the top panel (it shows T3 Code's icon), and the first
box to go back. It drives the VM's Claude Code (signed in through
`ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN`, or `claude auth login`);
other provider CLIs (Codex, ...) are not preinstalled.

**T3 Connect** (reach the VM's T3 Code from the T3 mobile app or another
machine, no port forwarding) needs one sign-in by a human; nothing can do it
unattended. In the viewer on workspace 2: Settings (gear, bottom left) →
*Connections* → *Sign in to T3 Connect* (Apple, GitHub, Google, Microsoft or
an e-mail code), then switch on *T3 Connect*. T3 Code keeps the link in the
VM (`~/.t3/userdata`), so it should survive reboots (not verified end to end
here, there was no T3 account), but not `agos-proxmox.sh reset`, which rolls
back to the first-boot snapshot: sign in again after a reset. Without
the GUI: `ssh agent@<vm>` and run
`ELECTRON_RUN_AS_NODE=1 /opt/t3code/t3code /opt/t3code/resources/app.asar/apps/server/dist/bin.mjs connect link --headless`,
approve the printed code on any device, then
`systemctl --user restart agos-t3code`.

If the VM is on Tailscale with `serve` on, leave T3 Code's own *Tailscale
HTTPS* switch off or give it another port: agos already serves the desktop
on 443.

Turn it off with `[agents] t3code = false` (or untick it in the guided
setup, or `--agents claude-code`); on a running VM edit
`/etc/agos/config.toml` and run `sudo agos apply`.

## Security notes

- **The VM is the boundary.** The agent has passwordless sudo inside it.
  Never give it hypervisor, password-manager or long-lived credentials; use
  per-VM API keys with spend caps.
- **The seed contains your secrets.** The Proxmox script deletes it after
  first boot; elsewhere, detach and delete it yourself. A copy stays inside
  the VM (`/etc/agos/secrets.env`, `/var/lib/cloud/`, readable by root) and
  therefore in the `golden` snapshot. Rotate by editing
  `/etc/agos/secrets.env` and rebooting, then retake the snapshot.
- **Tailnet policy**: tag the VM (`tag:agos`), let your users reach it, and
  grant `tag:agos` nothing, so a hijacked agent cannot open connections to
  the rest of your tailnet. For example:

  ```json
  {
    "tagOwners": { "tag:agos": ["autogroup:admin"] },
    "grants": [
      { "src": ["autogroup:member"], "dst": ["tag:agos"], "ip": ["443", "8765", "22"] }
    ]
  }
  ```

  This replaces the default allow-all policy; merge it with your existing
  grants rather than pasting it over them, and check it against Tailscale's
  grants documentation (untested here).
- **Egress**: on Proxmox, `--isolate` (experimental) keeps the VM off your LAN.
- **Reset often**: `agos-proxmox.sh reset` returns to the post-first-boot
  state in seconds.

## Testing this directory

```bash
deploy/proxmox/tests/run.sh          # shellcheck, the fake-Proxmox suite and the wizard render test, in Docker
deploy/proxmox/tests/run.sh isolate  # one test
deploy/proxmox/tests/run.sh render   # only the real-whiptail render test
```

The suite runs the real script against stub `qm`, `pvesm`, `pvesh`,
`pveversion`, `ip`, `curl` and `qemu-img` that record every invocation and
keep VM state on disk, in a `debian:trixie` container with the real
`genisoimage`, `xorriso`, `minisign` and `cloud-init` (for schema checks of
the generated and example seeds). It covers dry run, JSON, create (amd64,
arm64, dir storage, import fallback), idempotent re-runs, failure cleanup,
timeouts, status, reset, destroy, no-TTY and TTY confirmation, `--isolate`,
download/checksum/signature verification, truncated downloads, and that no
secret ever reaches a command line, a log or stdout.

The guided setup is tested twice. A fake `whiptail` replays scripted answers
(OK, Cancel, ESC) on a pseudo-terminal through the default, LAN, advanced and
existing-secrets paths, cancels at several points, and checks that the
wizard stays off whenever flags, environment or a missing terminal ask for
the unattended behaviour, and that typed secrets reach only the seed and the
final box (never arguments, logs or the terminal). Then the real `whiptail`
runs inside `tmux`, driven by `send-keys` through the default path to the
summary and on to the final box; every screen is saved under
`out/wizard-screens/`.
