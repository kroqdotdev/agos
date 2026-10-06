# Install agos on this Proxmox VE host

> Creates one VM running agos, an unattended Debian 13 desktop for AI agents, from a checksummed release image. Safe to re-run: an existing agos VM with the same name is reported, never duplicated or changed.

<!--
For the human. On the Proxmox host, as root:

  cd /root
  curl -fsSLo /root/agos-claude-settings.json https://raw.githubusercontent.com/kroqdotdev/agos/main/deploy/claude-settings.example.json
  claude --settings /root/agos-claude-settings.json "$(curl -fsSL https://raw.githubusercontent.com/kroqdotdev/agos/main/deploy/install.md)"

The settings file makes Claude Code itself (not the model) allow the read-only
checks and dry runs, ask you before anything that changes the host, and refuse
`qm destroy`, edits under /etc/pve and piping downloads into a shell. Read
this file first: it is the whole plan.
-->

I want you to install agos on this Proxmox VE host. You will only run the
commands in this runbook, show me a dry-run plan, and create the VM after I
answer "yes".

OBJECTIVE: One running VM tagged `agos` whose first boot reported ready, with a `golden` snapshot, and its desktop URL given to me.

DONE WHEN: `bash /root/agos-proxmox.sh status --json --name <NAME>` prints a JSON object with `"state":"ready"` and `"golden_snapshot":true`.

## Rules

1. Work only on this Proxmox VE host, as root, with the commands in this
   runbook. Do not install packages, edit system files or change any other VM.
2. Never run `qm destroy`, `qm rollback`, `qm delsnapshot`, `pct destroy`,
   `pvesm free`, `pvesm remove`, `pve-firewall`, or the script's `destroy` and
   `reset` subcommands. Never edit anything under `/etc/pve` or
   `/etc/network`, and never enable or change the datacenter firewall.
3. Never pipe a download into a shell (`curl ... | bash`, `bash -c "$(curl ...)"`,
   `bash <(curl ...)`). Run only `/root/agos-proxmox.sh`, and only after its
   checksum verified in step 3.
4. Secrets: never ask me to paste a secret into this chat, never read, print,
   copy or `grep` `/root/agos.secrets`, and never put a secret on a command
   line or in an environment variable. I create that file myself; you may only
   check its owner and mode with `stat`.
5. Always run the script with `--dry-run --json` first, summarise the plan,
   and wait for me to answer exactly "yes". Anything else means no. Use the
   same flags for the real run, plus `--yes`.
6. If a command exits non-zero, stop. Show me its stderr verbatim, tell me
   what the exit code means (table below), and wait. Do not retry with other
   flags, other storages or other versions on your own.
7. Do not add `--isolate` unless I ask for it; it is experimental and only
   takes effect when the datacenter firewall is on.
8. Do not read the generated passwords or tokens yourself. Give me the
   command that shows them.

| Exit code | Meaning | What you do |
|---|---|---|
| 0 | OK (also: the VM already exists) | continue |
| 1 | Usage error, or confirmation needed | show the message; fix only an obvious typo in the flags, after telling me |
| 2 | Preflight failed (host, storage, bridge, secrets file, ...) | stop, show stderr, wait |
| 3 | Download or checksum/signature verification failed | stop, show stderr, wait; never bypass verification |
| 4 | A VM operation failed | stop, show stderr, wait |
| 5 | Timed out waiting for first boot; the VM is kept | stop, show stderr, wait |

## TODO

- [ ] 1. Check the host
- [ ] 2. Agree on the settings with me; I prepare the secrets file
- [ ] 3. Download and verify the installer
- [ ] 4. Dry run, then wait for my "yes"
- [ ] 5. Create the VM
- [ ] 6. Verify and hand over

## 1. Check the host

Run these read-only commands and summarise the results:

```bash
pveversion
dpkg --print-architecture
pvesm status --content images
pvesm status --content iso
ip -br link show type bridge
pvesh get /cluster/resources --type vm --output-format json
```

Stop and tell me if `pveversion` is not `pve-manager/8.x` or `9.x`, if the
architecture is not `amd64` or `arm64` (arm64 needs Proxmox VE 9.2 or later),
or if no storage has `images` content. If a VM named `agos` with the tag `agos`
already exists, tell me; we then either stop or pick another name.

## 2. Agree on the settings

Propose these values and ask me to confirm or change them:

- VM name (`--name`, default `agos`)
- disk storage (`--storage`): the active `images` storage with the most free
  space from step 1
- bridge (`--bridge`, default `vmbr0`)
- size: `--cores 4 --memory 8192 --disk 64G` unless I want more
- my SSH public key file for user `agent` (`--ssh-key-file`, usually
  `/root/.ssh/authorized_keys`, the keys I log in to this host with); optional

Then ask me to create `/root/agos.secrets` myself, in my own shell, owned by
root with mode 0600, containing only the lines I need:

```ini
TS_AUTHKEY=tskey-client-...        # Tailscale OAuth client secret (tag:agos); gives a tailnet URL
VIEWER_PASSWORD=...                # desktop password for user "agos"
ANTHROPIC_API_KEY=sk-ant-...       # or CLAUDE_CODE_OAUTH_TOKEN / OPENAI_API_KEY
```

Suggest `(umask 077; touch /root/agos.secrets); nano /root/agos.secrets`.
Without the file the VM still installs, but without Tailscale or API keys.
When I say it is ready, check it without reading it:

```bash
stat -c '%U %a %s bytes' /root/agos.secrets
```

It must print `root 600` (or `root 400`). Otherwise tell me to run
`chmod 600 /root/agos.secrets` and wait. If the permission settings block even
`stat` on that file, skip this check: the dry run in step 4 checks owner, mode
and format too, and reports only key names.

## 3. Download and verify the installer

Use the pinned release, never the `main` branch:

```bash
curl -fsSL --proto =https --tlsv1.2 -o /root/agos-proxmox.sh https://github.com/kroqdotdev/agos/releases/download/v0.1.0/agos-proxmox.sh
curl -fsSL --proto =https --tlsv1.2 -o /root/agos-SHA256SUMS https://github.com/kroqdotdev/agos/releases/download/v0.1.0/SHA256SUMS
curl -fsSL --proto =https --tlsv1.2 -o /root/agos-SHA256SUMS.minisig https://github.com/kroqdotdev/agos/releases/download/v0.1.0/SHA256SUMS.minisig
cd /root && sha256sum --ignore-missing -c agos-SHA256SUMS
```

The last command must print `agos-proxmox.sh: OK` and exit 0. Anything else:
stop and show me. If `command -v minisign` finds minisign, also run
`minisign -Vm /root/agos-SHA256SUMS -x /root/agos-SHA256SUMS.minisig -P RWQeJEg8BJM+C3FntUGlSkbqk9PU0z3FPLJS3z4fsjfXedsmchg2OFu1`
and stop unless it prints `Signature and comment signature verified`. (The
installer itself checks the image's signature against this key with openssl
if minisign is missing.)

Then look at the installer's options:

```bash
bash /root/agos-proxmox.sh --help
```

## 4. Dry run

With the values from step 2 (leave out flags I did not set):

```bash
bash /root/agos-proxmox.sh --dry-run --json --name agos --storage local-lvm --bridge vmbr0 --cores 4 --memory 8192 --disk 64G --ssh-key-file /root/.ssh/authorized_keys
```

It prints one JSON object and changes nothing. If it reports `"existing":true`,
the VM is already there: show me its `urls` and stop. Otherwise summarise for
me: `vmid`, `name`, `storage`, `iso_storage`, `bridge`, `cores`, `memory_mib`,
`disk`, `image_url`, `secret_keys` (names only), `ssh_keys`, `isolate`, and the
list in `commands`. Then ask:

> Create VM `<vmid>` `<name>` with this plan? Answer "yes" to proceed.

Wait. Proceed only on exactly "yes".

## 5. Create the VM

Same flags, without `--dry-run`, with `--yes`:

```bash
bash /root/agos-proxmox.sh --yes --json --name agos --storage local-lvm --bridge vmbr0 --cores 4 --memory 8192 --disk 64G --ssh-key-file /root/.ssh/authorized_keys
```

This downloads the image (a few GB), verifies it, creates and boots the VM and
waits up to 15 minutes for first boot. Let it finish; do not interrupt or
re-run it. On a non-zero exit code follow rule 6.

## 6. Verify and hand over

```bash
bash /root/agos-proxmox.sh status --json --name agos
```

Check that `state` is `ready` and `golden_snapshot` is `true`. Then tell me:

- `urls.viewer` (the desktop) and `urls.agentd` (the agent API), and what
  `access` means: `tailscale` = open the URL from a device on my tailnet;
  `lan` = open it on my LAN, accept the self-signed certificate, user `agos`;
  `ssh-tunnel` = run `ssh -N -L 8444:127.0.0.1:8444 -L 8765:127.0.0.1:8765 agent@<ip>`
  and open `http://127.0.0.1:8444/`.
- the command to read the generated credentials, to run myself:
  `qm guest exec <vmid> -- grep -E '^(VIEWER_PASSWORD|AGENTD_TOKEN)=' /etc/agos/secrets.env`
- that `bash /root/agos-proxmox.sh reset --name <name> --yes` returns the VM to
  its first-boot state, and that I run that (and `destroy`) myself.

Then tick the last TODO item and stop.

EXECUTE NOW: Start with TODO item 1. After each step, tell me in one or two sentences what you found, and stop wherever this runbook says to wait.
