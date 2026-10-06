#!/usr/bin/env bash
# Real-render smoke test for the guided setup: the real whiptail inside tmux,
# driven with send-keys through the default path to the summary, then to the
# exact-commands screen and out via "Quit". Every screen is captured to $1
# (default /screens) so a human can look at them. Runs in the "render" test
# image (see run.sh); the Proxmox commands are the fakes from fake-pve/bin.
set -euo pipefail
here=$(cd -- "$(dirname -- "$0")" && pwd)
script="$here/../agos-proxmox.sh"
out=${1:-/screens}
T=/tmp/render
rm -rf "$T"
mkdir -p "$T"/state "$T"/root "$T"/run "$T"/cache "$out"
rm -f "$out"/*.txt

export PATH="$here/fake-pve/bin:$PATH"
export FAKE_STATE="$T/state" FAKE_LOG="$T/calls.log" FAKE_ROOT="$T/root" FAKE_HTTP_ROOT="$T/http"
export AGOS_KVM_DEVICE=/dev/null AGOS_CACHE_DIR="$T/cache" AGOS_POLL_INTERVAL=0.05 AGOS_TMPDIR="$T/run"
export AGOS_MINISIGN_PUBKEY=none AGOS_IMAGE_FILE="$T/agos-0.1.0-amd64.qcow2"
head -c 300000 /dev/urandom >"$AGOS_IMAGE_FILE"
: >"$FAKE_LOG"
mkdir -p /root/.ssh
printf '%s\n' 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTheRenderTestOnly00000000000000 admin@laptop' \
	'ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQFakeRsaKeyForTests00000000 root@pve' >/root/.ssh/authorized_keys

key="tskey-client-SMOKEtestKEY-0123456789"
n=0
fails=0

capture() {
	n=$((n + 1))
	tmux capture-pane -p -t wiz >"$out/$(printf '%02d' "$n")-$1.txt"
}

wait_for() {
	local tries=150
	while ((tries-- > 0)); do
		if tmux capture-pane -p -t wiz | grep -qF -- "$1"; then return 0; fi
		sleep 0.1
	done
	printf 'render: timed out waiting for "%s"; screen:\n' "$1" >&2
	tmux capture-pane -p -t wiz >&2
	exit 1
}

# step NAME TEXT KEYS...: wait until TEXT is on screen, capture it, press KEYS.
step() {
	local name=$1 text=$2
	shift 2
	wait_for "$text"
	sleep 0.3
	capture "$name"
	if (($#)); then tmux send-keys -t wiz "$@"; fi
}

check() {
	if ! "$@"; then
		printf 'render: FAIL: %s\n' "$*" >&2
		fails=$((fails + 1))
	fi
}

tmux -f /dev/null new-session -d -s wiz -x 100 -y 32 \
	"env LANG=C.UTF-8 TERM=screen bash '$script'; echo \"[exit \$?]\"; sleep 600"

step welcome "Create a new agos VM?" Enter
step settings "Default settings (recommended)" Enter
step access "How will you reach the agos desktop" Enter
wait_for "Paste a Tailscale key"
tmux send-keys -t wiz -l "$key"
sleep 0.3
capture tailscale-key
tmux send-keys -t wiz Enter
step ssh-keys "Public keys that may log in" Enter
step ssh-paste "Paste one more public key" Enter
step claude "Anthropic API key" Enter
step openai "OpenAI API key for agents" Enter
step summary "NoCloud ISO (label CIDATA)" Enter
step confirm "Nothing has been changed yet" Down Enter
# the command list is longer than the screen: Tab to the button, then Enter
step commands "qm create 100" Tab Enter
step confirm-again "Nothing has been changed yet" Down Down Enter
step aborted "[exit 1]"
tmux kill-server
if grep -qE '^qm (create|set|start)' "$FAKE_LOG"; then
	printf 'render: FAIL: the VM was touched although the user quit\n' >&2
	fails=$((fails + 1))
fi

# Second pass: all the way through "Create" to the final box, which gets the
# generated credentials through a memfd (never argv); the fake guest returns
# test values for them.
export FAKE_TAILSCALE=1
tmux -f /dev/null new-session -d -s wiz -x 100 -y 32 \
	"env LANG=C.UTF-8 TERM=screen bash '$script'; echo \"[exit \$?]\"; sleep 600"
wait_for "Create a new agos VM?"
tmux send-keys -t wiz Enter
wait_for "Default settings (recommended)"
tmux send-keys -t wiz Enter
wait_for "How will you reach the agos desktop"
tmux send-keys -t wiz Enter
wait_for "Paste a Tailscale key"
tmux send-keys -t wiz -l "$key"
tmux send-keys -t wiz Enter
for text in "Public keys that may log in" "Paste one more public key" "Anthropic API key" "OpenAI API key for agents" \
	"NoCloud ISO (label CIDATA)"; do
	wait_for "$text"
	sleep 0.3
	tmux send-keys -t wiz Enter
done
step create "Nothing has been changed yet" Enter
step ready "agos VM 100 (agos) is ready" Enter
step finished "[exit 0]"
tmux kill-server
check grep -qF "password: SENTINELview42" "$out/15-ready.txt"
check grep -qF "token:    agd_SENTINELtok42" "$out/15-ready.txt"
check grep -qF "https://agos.tail1234.ts.net/" "$out/15-ready.txt"
check grep -qF "agos VM 100 (agos): ready" "$out/16-finished.txt"
if grep -qE 'SENTINEL' "$out/16-finished.txt"; then
	printf 'render: FAIL: credentials left on the terminal after the final box\n' >&2
	fails=$((fails + 1))
fi

check grep -qF "Create a new agos VM?" "$out/01-welcome.txt"
check grep -qF "Default settings (recommended)" "$out/02-settings.txt"
check grep -qF "Tailscale: https://<name>.<tailnet>.ts.net (recommended)" "$out/03-access.txt"
# the passwordbox masks what was typed
check grep -qF "***" "$out/04-tailscale-key.txt"
check grep -qF "admin@laptop" "$out/05-ssh-keys.txt"
check grep -qF "Tailscale (OAuth client secret, tags: tag:agos)" "$out/09-summary.txt"
check grep -qF "Show the exact commands first" "$out/10-confirm.txt"
check grep -qF "qm create 100 --name agos" "$out/11-commands.txt"
check grep -qF "aborted, nothing changed" "$out/13-aborted.txt"
check grep -qF "Proxmox VE 9.0.10" "$out/13-aborted.txt"
if grep -lF "$key" "$out"/*.txt "$FAKE_LOG"; then
	printf 'render: FAIL: the Tailscale key is visible in a capture\n' >&2
	fails=$((fails + 1))
fi
if ! grep -q '^qm create 100' "$FAKE_LOG"; then
	printf 'render: FAIL: the second pass did not create the VM\n' >&2
	fails=$((fails + 1))
fi
if [[ -n $(ls -A "$T/run") ]]; then
	printf 'render: FAIL: temp files left in %s\n' "$T/run" >&2
	fails=$((fails + 1))
fi
if [[ -n ${HOST_UID:-} ]]; then chown -R "$HOST_UID:${HOST_GID:-$HOST_UID}" "$out"; fi
if ((fails)); then
	printf 'render: %d check(s) failed; screens in %s\n' "$fails" "$out" >&2
	exit 1
fi
printf 'render: ok, %d screens captured\n' "$n"
