# agos

An unattended Linux desktop for AI agents. agos is a Debian 13 disk image
that boots, configures itself from a cloud-init seed and gives an agent a
full X11 desktop to drive, with no human input required at any point. When
a human does want in, the desktop is one browser tab away over Tailscale,
with a takeover button that pauses the agent.

> Status: v0.1, under construction. Nothing is released yet.

## What you get

- **A desktop agents can drive.** XFCE on a KasmVNC X server pinned to
  1280×800, Chromium and Firefox ESR with every first-run, keyring and
  "restore pages?" prompt removed by policy.
- **`agentd`, the control daemon.** Screenshots and input over MCP (stdio and
  Streamable HTTP) and REST, with adapters that accept Anthropic, OpenAI and
  Gemini computer-use tool calls as-is. Frame-stamped coordinates reject
  clicks aimed at a stale screen; scoped tokens; a JSONL audit log.
- **Human takeover.** Watch in the KasmVNC web client; take over and the
  agent's input is refused until you hand back.
- **Zero-touch boot.** No boot menu, no login screen, no fsck questions, no
  screensaver, no update prompts. Configuration and secrets arrive at first
  boot through cloud-init, never baked into the image.
- **Claude Code preinstalled** and pre-seeded so it never asks a question,
  with agentd registered as its `desktop` MCP server.

## Quick start (Proxmox VE)

On the Proxmox host, as root:

```bash
curl -fsSLO https://kroq.dev/tools/agos-proxmox.sh
bash agos-proxmox.sh --dry-run          # shows exactly what it will do
bash agos-proxmox.sh --yes
```

The script checks every image against the release signing key
(`RWQeJEg8BJM+C3FntUGlSkbqk9PU0z3FPLJS3z4fsjfXedsmchg2OFu1`) before using it.
To verify the script itself first, use the signed release assets as shown in
[`deploy/README.md`](deploy/README.md).

Put secrets (Tailscale key, API keys) in `/root/agos.secrets` first; see
[`deploy/README.md`](deploy/README.md). Other hypervisors (libvirt, Incus,
UTM, VirtualBox, Hyper-V) use the same image and the same cloud-init seed.

To have an agent do the install for you, point it at
[`deploy/install.md`](deploy/install.md).

## Repository

| Path | What |
|---|---|
| [`docs/spec.md`](docs/spec.md) | The contract: paths, ports, config, APIs |
| [`agentd/`](agentd/) | Control daemon (Python) |
| [`image/`](image/) | mkosi recipe for the guest image, amd64 and arm64 |
| [`deploy/`](deploy/) | Proxmox one-liner, cloud-init examples, install runbook |
| [`docs/research.md`](docs/research.md) | Research report behind the design |

## Security model

Assume the agent will eventually be steered by something it reads. The VM
is the boundary: the agent has passwordless sudo inside it, so anything
valuable (hypervisor access, password managers, long-lived credentials)
stays outside. Run one VM per agent, keep a golden snapshot to roll back to,
and filter its outbound traffic on the host (`agos-proxmox.sh --isolate`).
