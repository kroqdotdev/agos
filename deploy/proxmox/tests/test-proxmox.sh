#!/usr/bin/env bash
# Tests for agos-proxmox.sh and make-seed.sh against a fake Proxmox VE.
# Runs inside the debian:trixie test container; start it with ./run.sh.
set -uo pipefail

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
REPO=$(cd -- "$HERE/../../.." && pwd)
SCRIPT="$REPO/deploy/proxmox/agos-proxmox.sh"
# The release the script installs by default; VR is the same as a regex.
V=$(sed -n 's/^AGOS_DEFAULT_VERSION="\([^"]*\)"$/\1/p' "$SCRIPT")
VR=${V//./\\.}
export PATH="$HERE/fake-pve/bin:$PATH"

PASS=0
FAILED=()
CUR=""
CUR_OK=1
ONLY=${1:-}

# ------------------------------------------------------------------ harness

fail() {
	CUR_OK=0
	printf '    FAIL: %s\n' "$*"
}

setup() {
	CUR=$1
	CUR_OK=1
	T="/tmp/agos-tests/$1"
	rm -rf "$T"
	mkdir -p "$T"/{state,root,http,cache,run}
	local v
	for v in $(compgen -e); do
		case $v in FAKE_* | AGOS_*) unset "$v" ;; esac
	done
	export FAKE_STATE="$T/state" FAKE_LOG="$T/calls.log" FAKE_ROOT="$T/root" FAKE_HTTP_ROOT="$T/http"
	export AGOS_KVM_DEVICE=/dev/null AGOS_CACHE_DIR="$T/cache" AGOS_POLL_INTERVAL=0.05 AGOS_TMPDIR="$T/run"
	# Fake releases are unsigned; t_signature turns checking back on.
	export AGOS_MINISIGN_PUBKEY=none
	: >"$FAKE_LOG"
	export FAKE_WT_ANSWERS="$T/answers" FAKE_WT_LOG="$T/wt.log" FAKE_WT_SCREENS="$T/screens"
	mkdir -p /etc/pve/firewall
	rm -f /etc/pve/firewall/*.fw /etc/pve/firewall/cluster.fw /root/agos.secrets /root/.ssh/authorized_keys
	# Sentinel secrets: these strings must never show up anywhere but inside the seed.
	printf '%s\n' '# test secrets' 'TS_AUTHKEY=tskey-client-SENTINELts111' \
		'ANTHROPIC_API_KEY=sk-ant-SENTINELak222' 'VIEWER_PASSWORD=SENTINELpw333' >"$T/agos.secrets"
	chmod 600 "$T/agos.secrets"
	head -c 300000 /dev/urandom >"$T/agos-$V-amd64.qcow2"
	head -c 300000 /dev/urandom >"$T/agos-$V-arm64.qcow2"
	(cd "$T" && sha256sum "agos-$V-amd64.qcow2" "agos-$V-arm64.qcow2" >SHA256SUMS)
	printf '%s\n' 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTheTestSuiteOnly0000000000000 tester@example' >"$T/id.pub"
	IMG="$T/agos-$V-amd64.qcow2"
}

finish() {
	if ((CUR_OK)); then
		PASS=$((PASS + 1))
		printf 'ok   %s\n' "$CUR"
	else
		FAILED+=("$CUR")
		printf 'FAIL %s\n' "$CUR"
		printf '    --- stderr (tail)\n'
		tail -n 15 "$T/err" 2>/dev/null | sed 's/^/    | /'
		printf '    --- stdout (tail)\n'
		tail -n 10 "$T/out" 2>/dev/null | sed 's/^/    | /'
	fi
}

# agos <args...>: runs the script with stdin closed (no TTY), capturing output.
agos() {
	bash "$SCRIPT" "$@" </dev/null >"$T/out" 2>"$T/err"
	RC=$?
}

expect_rc() { [[ $RC == "$1" ]] || fail "exit code $RC, expected $1"; }
expect_out() { grep -Eq -- "$1" "$T/out" || fail "stdout lacks /$1/"; }
expect_err() { grep -Eq -- "$1" "$T/err" || fail "stderr lacks /$1/"; }
expect_called() { grep -Eq -- "$1" "$FAKE_LOG" || fail "never called: /$1/"; }
expect_not_called() {
	if grep -Eq -- "$1" "$FAKE_LOG"; then fail "unexpectedly called: /$1/"; fi
}
expect_file() { [[ -e $1 ]] || fail "missing file $1"; }
expect_no_file() { [[ ! -e $1 ]] || fail "file should not exist: $1"; }

# jget KEY... : value from the JSON on stdout.
jget() {
	perl -MJSON::PP -e '
		my $d = decode_json(do { local $/; <STDIN> });
		for my $k (@ARGV) { $d = ref $d eq "ARRAY" ? $d->[$k] : $d->{$k} }
		print !defined $d ? "null" : JSON::PP::is_bool($d) ? ($d ? "true" : "false") : ref $d ? encode_json($d) : $d;
	' "$@" <"$T/out"
}

expect_json() {
	# KEY... VALUE
	local want=${*: -1} got
	got=$(jget "${@:1:$#-1}" 2>/dev/null) || {
		fail "stdout is not one JSON object"
		return
	}
	[[ $got == "$want" ]] || fail "JSON ${*:1:$#-1} = '$got', expected '$want'"
}

expect_single_json() {
	local n
	n=$(grep -c . "$T/out")
	[[ $n == 1 ]] || fail "stdout has $n lines, expected exactly one JSON object"
	perl -MJSON::PP -e 'decode_json(do { local $/; <STDIN> })' <"$T/out" 2>/dev/null || fail "stdout is not valid JSON"
}

expect_no_secrets() {
	local f
	for f in "$T/out" "$T/err" "$FAKE_LOG" "$FAKE_STATE"/qemu/*.conf /etc/pve/firewall/*.fw; do
		[[ -f $f ]] || continue
		if grep -q SENTINEL "$f"; then fail "a secret leaked into $f"; fi
	done
}

vm_count() { find "$FAKE_STATE/qemu" -name '*.conf' | wc -l; }

seed_file() {
	# extract FILE from the seed ISO the fake qm captured for VMID
	isoinfo -i "$FAKE_STATE/artifacts/$1-seed.iso" -J -x "/$2"
}

# ------------------------------------------------------------------ tests

t_help() {
	setup help
	agos --help
	expect_rc 0
	expect_out 'Usage: agos-proxmox.sh'
	expect_out 'Exit codes: 0 ok, 1 usage'
	finish
}

t_usage_errors() {
	setup usage_errors
	agos --bogus
	expect_rc 1
	expect_err 'unknown flag: --bogus'
	agos --memory 512
	expect_rc 1
	agos --name 'bad name!'
	expect_rc 1
	agos create status
	expect_rc 1
	agos --vmid
	expect_rc 1
	agos --json --disk 3G
	expect_rc 1
	expect_single_json
	expect_json ok false
	expect_json exit_code 1
	finish
}

t_not_root() {
	setup not_root
	chmod 755 "$T"
	runuser -u nobody -- env PATH="$PATH" bash "$SCRIPT" --dry-run </dev/null >"$T/out" 2>"$T/err"
	RC=$?
	expect_rc 2
	expect_err 'run as root'
	finish
}

t_preflight_failures() {
	setup preflight_failures
	FAKE_PVE_VERSION=7.4.3 agos --dry-run --image-file "$IMG"
	expect_rc 2
	expect_err 'not supported \(need 8.x or 9.x\)'
	FAKE_PVE_VERSION=8.4.1 agos --dry-run --image-file "$IMG" --secrets-file "$T/agos.secrets"
	expect_rc 0
	FAKE_ARCH=i386 agos --dry-run --image-file "$IMG"
	expect_rc 2
	expect_err "architecture 'i386'"
	AGOS_KVM_DEVICE=/nonexistent agos --dry-run --image-file "$IMG"
	expect_rc 2
	expect_err 'VT-x'
	agos --dry-run --image-file "$IMG" --bridge vmbr9
	expect_rc 2
	expect_err "bridge 'vmbr9' not found"
	agos --dry-run --image-file "$IMG" --storage nosuch
	expect_rc 2
	expect_err "storage 'nosuch'"
	agos --dry-run --image-file "$IMG" --secrets-file "$T/missing.secrets"
	expect_rc 2
	agos --dry-run --image-file "$IMG" --config-file "$HERE/fixtures/bad.toml"
	expect_rc 2
	expect_err 'not valid TOML'
	agos --dry-run --image-file "$IMG" --ssh-key-file "$T/agos.secrets"
	expect_rc 2
	expect_err 'no SSH public keys'
	FAKE_STORAGES="local dir iso,vztmpl active 81089764" agos --dry-run --image-file "$IMG"
	expect_rc 2
	expect_err "no active storage for VM disks"
	expect_not_called '^qm (create|set|start)'
	finish
}

t_secrets_file_checks() {
	setup secrets_file_checks
	chmod 644 "$T/agos.secrets"
	agos --dry-run --image-file "$IMG" --secrets-file "$T/agos.secrets"
	expect_rc 2
	expect_err 'chmod 600'
	chmod 600 "$T/agos.secrets"
	printf 'export TS_AUTHKEY=tskey-client-SENTINELbad\n' >>"$T/agos.secrets"
	agos --dry-run --json --image-file "$IMG" --secrets-file "$T/agos.secrets"
	expect_rc 2
	expect_err 'line\(s\) 5 are not KEY=value'
	expect_single_json
	expect_json exit_code 2
	expect_no_secrets
	# a missing default secrets file is only a warning
	agos --dry-run --image-file "$IMG"
	expect_rc 0
	expect_err 'no secrets file at /root/agos.secrets'
	finish
}

t_dry_run_human() {
	setup dry_run_human
	agos --dry-run --image-file "$IMG" --secrets-file "$T/agos.secrets" --ssh-key-file "$T/id.pub"
	expect_rc 0
	expect_out 'plan \(dry run'
	expect_out 'qm create 100 --name agos --tags agos'
	expect_out 'ANTHROPIC_API_KEY TS_AUTHKEY VIEWER_PASSWORD \(values never shown\)'
	expect_not_called '^qm (create|set|start|snapshot|resize)'
	expect_not_called '^(curl|qemu-img)'
	[[ $(vm_count) == 0 ]] || fail "dry run created a VM"
	[[ -z $(ls -A "$T/run") ]] || fail "dry run left files in the temp dir"
	expect_no_secrets
	finish
}

t_dry_run_json() {
	setup dry_run_json
	agos --dry-run --json --image-file "$IMG" --secrets-file "$T/agos.secrets" --vmid 123 --name agent-box \
		--cores 2 --memory 4096 --disk 32G --cpu x86-64-v2-AES
	expect_rc 0
	expect_single_json
	expect_json ok true
	expect_json dry_run true
	expect_json state planned
	expect_json vmid 123
	expect_json name agent-box
	expect_json ip null
	expect_json urls viewer null
	expect_json storage local-lvm
	expect_json iso_storage local
	expect_json tailscale true
	expect_json secret_keys '["ANTHROPIC_API_KEY","TS_AUTHKEY","VIEWER_PASSWORD"]'
	expect_json agents null
	local cmds
	cmds=$(jget commands)
	[[ $cmds == *'--cpu x86-64-v2-AES --cores 2 --memory 4096'* ]] || fail "plan lacks the hardware flags"
	[[ $cmds == *'qm resize 123 scsi0 32G'* ]] || fail "plan lacks the resize"
	expect_not_called '^qm (create|set|start)'
	expect_no_secrets
	finish
}

t_dry_run_download_checks() {
	setup dry_run_download_checks
	local rel="$FAKE_HTTP_ROOT/github.com/kroqdotdev/agos/releases/download/v$V"
	agos --dry-run --json
	expect_rc 3
	expect_json error "not reachable: https://github.com/kroqdotdev/agos/releases/download/v$V/agos-$V-amd64.qcow2"
	mkdir -p "$rel"
	cp "$IMG" "$T/SHA256SUMS" "$rel/"
	agos --dry-run --json
	expect_rc 0
	expect_json image_url "https://github.com/kroqdotdev/agos/releases/download/v$V/agos-$V-amd64.qcow2"
	expect_called "^curl -fsSL --proto =https --tlsv1.2 --connect-timeout 20 -r 0-0 -o /dev/null https://github.com/kroqdotdev/agos/releases/download/v$V/agos-$V-amd64.qcow2\$"
	expect_no_file "$T/cache/$V/agos-$V-amd64.qcow2"
	finish
}

check_create_flags() {
	expect_called '^qm create 100 --name agos --tags agos --ostype l26 --bios ovmf --cpu host --cores 4 --memory 8192 --balloon 0 --scsihw virtio-scsi-single --agent enabled=1 --net0 virtio,bridge=vmbr0 --vga virtio --tablet 1 --serial0 socket --onboot 1 --description .* --machine q35$'
	expect_called '^qm set 100 --efidisk0 local-lvm:1,efitype=4m,pre-enrolled-keys=0$'
	expect_called "^qm set 100 --scsi0 local-lvm:0,import-from=$IMG,discard=on,ssd=1,iothread=1\$"
	expect_called '^qm resize 100 scsi0 64G$'
	expect_called '^qm set 100 --boot order=scsi0$'
	expect_called '^qm set 100 --ide2 local:iso/agos-seed-100.iso,media=cdrom$'
	expect_called '^qm start 100$'
	expect_called '^qm agent 100 ping$'
	expect_called '^qm guest exec 100 --timeout 15 -- cat /var/lib/agos/state.json$'
	expect_called '^qm set 100 --ide2 none,media=cdrom$'
	expect_called '^qm snapshot 100 golden'
	expect_not_called 'cloudinit|cicustom|--tablet 0'
}

t_create() {
	setup create
	FAKE_TAILSCALE=1 FAKE_AGENT_AFTER=3 FAKE_READY_AFTER=2 \
		agos --yes --json --image-file "$IMG" --secrets-file "$T/agos.secrets" --ssh-key-file "$T/id.pub"
	expect_rc 0
	expect_single_json
	expect_json ok true
	expect_json vmid 100
	expect_json name agos
	expect_json state ready
	expect_json status running
	expect_json ip 192.168.1.57
	expect_json urls viewer 'https://agos.tail1234.ts.net/'
	expect_json urls agentd 'https://agos.tail1234.ts.net:8765/'
	expect_json access tailscale
	expect_json golden_snapshot true
	expect_json image_checksum ok
	check_create_flags
	# the order matters: seed ejected before the snapshot, snapshot last
	local eject snap
	eject=$(grep -n -- '--ide2 none,media=cdrom' "$FAKE_LOG" | cut -d: -f1)
	snap=$(grep -n -- '^qm snapshot' "$FAKE_LOG" | cut -d: -f1)
	((eject < snap)) || fail "seed must be ejected before the golden snapshot"
	expect_no_file "$FAKE_ROOT/local/template/iso/agos-seed-100.iso"
	[[ -z $(ls -A "$T/run") ]] || fail "seed temp dir left behind"
	[[ $(grep -c '^golden$' "$FAKE_STATE/qemu/100.snapshots") == 1 ]] || fail "no golden snapshot"
	grep -q '^tags: agos$' "$FAKE_STATE/qemu/100.conf" || fail "VM not tagged agos"
	expect_no_secrets

	# what the guest would have received
	local iso="$FAKE_STATE/artifacts/100-seed.iso" ud
	expect_file "$iso"
	isoinfo -d -i "$iso" | grep -q '^Volume id: CIDATA$' || fail "seed volume label is not CIDATA"
	seed_file 100 meta-data | grep -Eq '^instance-id: agos-100-[0-9TZ]+$' || fail "meta-data lacks an explicit instance-id"
	seed_file 100 meta-data | grep -q "^local-hostname: 'agos'$" || fail "meta-data lacks local-hostname"
	ud=$(seed_file 100 user-data)
	[[ $ud == '#cloud-config'* ]] || fail "user-data is not #cloud-config"
	grep -q 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKey' <<<"$ud" || fail "SSH key missing from user-data"
	if grep -q SENTINEL <<<"$ud"; then fail "secrets are in user-data in clear text"; fi
	seed_file 100 user-data >"$T/user-data"
	cloud-init schema -c "$T/user-data" >"$T/schema.log" 2>&1 || fail "cloud-init schema rejects the generated user-data: $(tail -n3 "$T/schema.log")"
	python3 - "$T/user-data" "$T/agos.secrets" <<'EOF' || fail "secrets.env in the seed does not match the secrets file"
import base64, sys, yaml
ud = yaml.safe_load(open(sys.argv[1]))
files = {f["path"]: f for f in ud["write_files"]}
f = files["/etc/agos/secrets.env"]
assert f["permissions"] == "0600" and f["owner"] == "root:root" and f["encoding"] == "b64"
assert base64.b64decode(f["content"]) == open(sys.argv[2], "rb").read()
assert ud["users"][0]["name"] == "agent" and ud["users"][0]["lock_passwd"] is True
assert ud["ssh_pwauth"] is False
EOF
	finish
}

t_create_idempotent() {
	setup create_idempotent
	agos --yes --image-file "$IMG" --secrets-file "$T/agos.secrets"
	expect_rc 0
	: >"$FAKE_LOG"
	agos --yes --json --image-file "$IMG" --secrets-file "$T/agos.secrets"
	expect_rc 0
	expect_json existing true
	expect_json vmid 100
	expect_json state ready
	expect_not_called '^qm (create|set|start|snapshot)'
	[[ $(vm_count) == 1 ]] || fail "a second VM was created"
	# a different name makes a second VM
	agos --yes --json --image-file "$IMG" --name agos-2
	expect_rc 0
	expect_json vmid 101
	expect_json existing false
	[[ $(vm_count) == 2 ]] || fail "expected two VMs"
	# a non-agos VM with the requested name is not adopted
	qm create 300 --name web --tags prod >/dev/null
	agos --yes --image-file "$IMG" --name web
	expect_rc 2
	expect_err 'not tagged agos'
	finish
}

t_create_arm64() {
	setup create_arm64
	FAKE_ARCH=arm64 agos --yes --json --image-file "$T/agos-$V-arm64.qcow2" --secrets-file "$T/agos.secrets"
	expect_rc 0
	expect_json state ready
	expect_called '^qm create 100 .*--cpu host'
	expect_not_called '--machine q35'
	expect_called '^qm set 100 --efidisk0 local-lvm:1,pre-enrolled-keys=0$'
	expect_called '^qm set 100 --scsi1 local:iso/agos-seed-100.iso,media=cdrom$'
	expect_called '^qm set 100 --scsi1 none,media=cdrom$'
	expect_not_called '--ide2'
	FAKE_ARCH=arm64 agos --dry-run --image-file "$T/agos-$V-arm64.qcow2" --name other --cpu x86-64-v2-AES
	expect_rc 1
	expect_err 'x86 model'
	finish
}

t_create_dir_storage() {
	setup create_dir_storage
	FAKE_STORAGES=$'local dir iso,vztmpl,images active 81089764\nbig nfs images active 900000000' \
		agos --yes --image-file "$IMG" --storage local
	expect_rc 0
	expect_called '^qm set 100 --efidisk0 local:1,efitype=4m,pre-enrolled-keys=0,format=qcow2$'
	expect_called '^qm set 100 --scsi0 local:0,import-from=.*,iothread=1,format=qcow2$'
	# without --storage the one with most free space wins
	FAKE_STORAGES=$'local dir iso,vztmpl,images active 81089764\nbig nfs images active 900000000' \
		agos --dry-run --json --image-file "$IMG" --name x2
	expect_json storage big
	finish
}

t_create_with_config_and_network() {
	setup create_with_config_and_network
	FAKE_LAN=1 agos --yes --json --image-file "$IMG" --config-file "$HERE/fixtures/config-lan.toml" \
		--network-config "$REPO/deploy/cloud-init/network-config.static"
	expect_rc 0
	expect_json access lan
	expect_json urls viewer 'https://192.168.1.57:8444/'
	seed_file 100 network-config | grep -q '192.168.1.50/24' || fail "network-config missing from the seed"
	seed_file 100 user-data >"$T/user-data"
	python3 - "$T/user-data" "$HERE/fixtures/config-lan.toml" <<'EOF' || fail "config.toml in the seed is wrong"
import base64, sys, yaml
ud = yaml.safe_load(open(sys.argv[1]))
f = {f["path"]: f for f in ud["write_files"]}["/etc/agos/config.toml"]
assert f["permissions"] == "0644"
assert base64.b64decode(f["content"]) == open(sys.argv[2], "rb").read()
assert "/etc/agos/secrets.env" not in {f["path"] for f in ud["write_files"]}
EOF
	cloud-init schema -t network-config -c "$REPO/deploy/cloud-init/network-config.static" >/dev/null 2>&1 ||
		fail "cloud-init rejects network-config.static"
	finish
}

t_agents_flag() {
	setup agents_flag
	agos --dry-run --json --image-file "$IMG" --agents t3code
	expect_rc 0
	expect_json agents t3code true
	expect_json agents claude_code false
	agos --dry-run --image-file "$IMG" --agents claude-code,t3code
	expect_rc 0
	expect_out 'agents     Claude Code, T3 Code'
	agos --dry-run --image-file "$IMG"
	expect_rc 0
	expect_out 'agents     image defaults \(Claude Code, T3 Code\)'
	agos --agents bogus
	expect_rc 1
	expect_err 'agents takes claude-code and/or t3code'
	# a config file that has its own [agents] table and --agents contradict each other
	printf '[agents]\nt3code = false\n' >"$T/agents.toml"
	agos --dry-run --image-file "$IMG" --config-file "$T/agents.toml" --agents t3code
	expect_rc 1
	expect_err 'already has an \[agents\] table'
	agos --dry-run --image-file "$IMG" --config-file "$T/agents.toml"
	expect_rc 0
	expect_out 'agents     as set in the config file'
	# without a config file the seed's config.toml is just the [agents] table
	agos --yes --image-file "$IMG" --agents none
	expect_rc 0
	seed_written 100 /etc/agos/config.toml >"$T/cfg100"
	python3 -c 'import sys, tomllib; c = tomllib.load(open(sys.argv[1], "rb")); assert c == {"agents": {"claude_code": False, "t3code": False}}, c' \
		"$T/cfg100" || fail "--agents none must seed claude_code = false, t3code = false"
	# with one it is appended and the file's own settings stay
	FAKE_LAN=1 AGOS_AGENTS=claude-code agos --yes --image-file "$IMG" --vmid 101 --name agos2 \
		--config-file "$HERE/fixtures/config-lan.toml"
	expect_rc 0
	seed_written 101 /etc/agos/config.toml >"$T/cfg101"
	python3 -c 'import sys, tomllib; c = tomllib.load(open(sys.argv[1], "rb")); assert c["agents"] == {"claude_code": True, "t3code": False} and c["viewer"]["listen"] == "0.0.0.0", c' \
		"$T/cfg101" || fail "AGOS_AGENTS with --config-file must append [agents] and keep the file"
	finish
}

t_create_import_fallback() {
	setup create_import_fallback
	FAKE_FAIL='import-from' agos --yes --image-file "$IMG"
	expect_rc 0
	expect_err "falling back to 'qm disk import'"
	expect_called "^qm disk import 100 $IMG local-lvm\$"
	expect_called '^qm set 100 --scsi0 local-lvm:vm-100-disk-9,discard=on,ssd=1,iothread=1$'
	finish
}

t_create_failure_cleans_up() {
	setup create_failure_cleans_up
	FAKE_FAIL='^qm start' agos --yes --json --image-file "$IMG" --secrets-file "$T/agos.secrets"
	expect_rc 4
	expect_single_json
	expect_json ok false
	expect_json exit_code 4
	expect_json error 'qm start failed'
	expect_called '^qm destroy 100 --purge 1$'
	[[ $(vm_count) == 0 ]] || fail "half-created VM was left behind"
	expect_no_file "$FAKE_ROOT/local/template/iso/agos-seed-100.iso"
	[[ -z $(ls -A "$T/run") ]] || fail "seed temp dir left behind"
	expect_no_secrets
	# a failure before qm create succeeded must not destroy anything
	qm create 100 --name someone-elses --tags other >/dev/null
	: >"$FAKE_LOG"
	FAKE_FAIL='^qm create' agos --yes --image-file "$IMG" --vmid 150
	expect_rc 4
	expect_not_called '^qm destroy'
	[[ -f $FAKE_STATE/qemu/100.conf ]] || fail "an unrelated VM was destroyed"
	finish
}

t_create_timeout() {
	setup create_timeout
	FAKE_NEVER_READY=1 agos --yes --json --image-file "$IMG" --secrets-file "$T/agos.secrets" --timeout 1
	expect_rc 5
	expect_json exit_code 5
	expect_err 'timed out after 1s'
	expect_not_called '^qm destroy'
	expect_not_called '^qm snapshot'
	expect_called '^qm set 100 --ide2 none,media=cdrom$'
	expect_no_file "$FAKE_ROOT/local/template/iso/agos-seed-100.iso"
	[[ -f $FAKE_STATE/qemu/100.conf ]] || fail "timed-out VM should be kept for inspection"
	expect_no_secrets
	finish
}

t_create_guest_failed() {
	setup create_guest_failed
	FAKE_GUEST_STATE=failed agos --yes --image-file "$IMG"
	expect_rc 4
	expect_err "first boot reported 'failed': fake guest"
	[[ -f $FAKE_STATE/qemu/100.conf ]] || fail "VM should be kept for inspection"
	expect_no_file "$FAKE_ROOT/local/template/iso/agos-seed-100.iso"
	finish
}

t_create_vm_died() {
	setup create_vm_died
	FAKE_DIE_AFTER_START=1 agos --yes --image-file "$IMG"
	expect_rc 4
	expect_err "is 'stopped' during first boot"
	[[ -f $FAKE_STATE/qemu/100.conf ]] || fail "VM should be kept for inspection"
	finish
}

t_no_tty_needs_yes() {
	setup no_tty_needs_yes
	agos --image-file "$IMG" --secrets-file "$T/agos.secrets"
	expect_rc 1
	expect_err 'no terminal'
	expect_not_called '^qm (create|set|start)'
	agos --json --image-file "$IMG"
	expect_rc 1
	expect_single_json
	expect_json exit_code 1
	finish
}

t_tty_prompt() {
	setup tty_prompt
	local cmd="env PATH='$PATH' bash '$SCRIPT' --image-file '$IMG' --secrets-file '$T/agos.secrets'"
	printf 'n\n' | script -qec "$cmd" /dev/null >"$T/out" 2>&1
	RC=$?
	grep -q 'Create VM 100' "$T/out" || fail "no confirmation prompt on a TTY"
	grep -q 'aborted; nothing was changed' "$T/out" || fail "answering n did not abort"
	expect_not_called '^qm create'
	printf 'y\n' | script -qec "$cmd" /dev/null >"$T/out" 2>&1
	expect_called '^qm create 100'
	expect_called '^qm snapshot 100 golden'
	if grep -q SENTINEL "$T/out"; then fail "secret printed on the terminal"; fi
	finish
}

t_env_equivalents() {
	setup env_equivalents
	AGOS_NAME=envbox AGOS_YES=1 AGOS_JSON=1 AGOS_CORES=6 AGOS_MEMORY=6144 AGOS_DISK=40G \
		AGOS_IMAGE_FILE="$IMG" AGOS_SECRETS_FILE="$T/agos.secrets" AGOS_VMID=777 agos
	expect_rc 0
	expect_json name envbox
	expect_json vmid 777
	seed_file 777 user-data >"$T/user-data"
	python3 -c 'import sys, yaml; assert yaml.safe_load(open(sys.argv[1]))["hostname"] == "envbox"' "$T/user-data" ||
		fail "hostname is not a YAML string"
	expect_called '^qm create 777 --name envbox .*--cores 6 --memory 6144'
	expect_called '^qm resize 777 scsi0 40G$'
	agos --yes --image-file "$IMG" --name yes
	expect_rc 0
	seed_file 100 user-data >"$T/user-data"
	python3 -c 'import sys, yaml; assert yaml.safe_load(open(sys.argv[1]))["hostname"] == "yes"' "$T/user-data" ||
		fail "a VM named 'yes' got a boolean hostname"
	AGOS_DRY_RUN=1 AGOS_JSON=1 AGOS_NAME=envbox2 AGOS_IMAGE_FILE="$IMG" agos
	expect_rc 0
	expect_json dry_run true
	finish
}

t_vmid_in_use() {
	setup vmid_in_use
	qm create 120 --name other --tags x >/dev/null
	agos --dry-run --image-file "$IMG" --vmid 120
	expect_rc 2
	expect_err 'VMID 120 is already in use'
	finish
}

t_status() {
	setup status
	agos status --json
	expect_rc 0
	expect_json state absent
	agos --yes --image-file "$IMG" --secrets-file "$T/agos.secrets"
	FAKE_TAILSCALE=1 agos status --json
	expect_rc 0
	expect_single_json
	expect_json vmid 100
	expect_json state ready
	expect_json ip 192.168.1.57
	expect_json urls viewer 'https://agos.tail1234.ts.net/'
	expect_json golden_snapshot true
	agos status
	expect_rc 0
	expect_out 'agos VM 100 \(agos\): ready'
	expect_out "qm guest exec 100 -- grep -E"
	agos status --json --vmid 999
	expect_rc 4
	qm stop 100 >/dev/null
	agos status --json --name agos
	expect_json state stopped
	expect_json status stopped
	# several agos VMs
	agos --yes --image-file "$IMG" --name agos-b
	agos status --json
	expect_rc 0
	expect_json state multiple
	expect_json vms 1 name agos-b
	# a VM on another node
	qm create 500 --name remote --tags agos >/dev/null
	echo running >"$FAKE_STATE/qemu/500.status"
	echo pve2 >"$FAKE_STATE/qemu/500.node"
	agos status --json --vmid 500
	expect_rc 0
	expect_json node pve2
	expect_json state running
	expect_no_secrets
	finish
}

t_reset() {
	setup reset
	agos --yes --image-file "$IMG"
	: >"$FAKE_LOG"
	agos reset
	expect_rc 1
	expect_err 'no terminal'
	expect_not_called '^qm (rollback|stop|start)'
	agos reset --dry-run --json
	expect_rc 0
	expect_json commands '["qm stop 100","qm rollback 100 golden","qm start 100"]'
	expect_not_called '^qm (rollback|stop|start)'
	# the rolled-back state.json says "ready" from the previous boot: reset
	# must wait for one written during the new boot
	: >"$FAKE_LOG"
	FAKE_STALE_READS=3 agos reset --yes --json
	expect_rc 0
	expect_json action reset
	expect_json state ready
	expect_called '^qm stop 100$'
	expect_called '^qm rollback 100 golden$'
	expect_called '^qm start 100$'
	[[ $(grep -c 'guest exec 100 .*cat /var/lib/agos/state.json' "$FAKE_LOG") -ge 4 ]] ||
		fail "reset returned on the stale pre-rollback state"
	# no golden snapshot -> refuse
	qm create 200 --name nosnap --tags agos >/dev/null
	agos reset --vmid 200 --yes
	expect_rc 4
	expect_err "no 'golden' snapshot"
	# not an agos VM -> refuse
	qm create 201 --name plain --tags web >/dev/null
	agos reset --vmid 201 --yes
	expect_rc 4
	expect_err 'not tagged agos'
	finish
}

t_destroy() {
	setup destroy
	agos --yes --image-file "$IMG"
	: >"$FAKE_LOG"
	agos destroy --name agos
	expect_rc 1
	expect_err 're-run with --yes'
	expect_not_called '^qm (destroy|stop)'
	[[ -f $FAKE_STATE/qemu/100.conf ]] || fail "destroy without --yes removed the VM"
	agos destroy --yes
	expect_rc 1
	expect_err 'destroy needs --vmid or --name'
	agos destroy --name agos --dry-run --json
	expect_rc 0
	expect_json commands '["qm stop 100","qm destroy 100 --purge 1"]'
	expect_not_called '^qm destroy'
	qm create 300 --name plain --tags web >/dev/null
	agos destroy --vmid 300 --yes
	expect_rc 4
	expect_err 'not tagged agos'
	[[ -f $FAKE_STATE/qemu/300.conf ]] || fail "a non-agos VM was destroyed"
	agos destroy --name agos --yes --json
	expect_rc 0
	expect_json state destroyed
	expect_called '^qm destroy 100 --purge 1$'
	[[ ! -f $FAKE_STATE/qemu/100.conf ]] || fail "VM still exists"
	finish
}

t_isolate() {
	setup isolate
	FAKE_HOST_PUBLIC=203.0.113.5 agos --yes --image-file "$IMG" --isolate --isolate-dns 10.9.9.9
	expect_rc 0
	expect_err 'datacenter firewall is DISABLED'
	expect_err 'lock yourself out'
	local fw=/etc/pve/firewall/100.fw
	expect_file "$fw"
	grep -q '^enable: 1$' "$fw" || fail "VM firewall not enabled in $fw"
	grep -q '^OUT ACCEPT -i net0 -dest 192.168.1.1 -p udp -dport 53' "$fw" || fail "no DNS exception for the gateway"
	grep -q '^OUT ACCEPT -i net0 -dest 10.9.9.9 -p tcp -dport 53' "$fw" || fail "no DNS exception for --isolate-dns"
	grep -q '^OUT DROP -i net0 -dest 10.0.0.0/8,172.16.0.0/12,192.168.0.0/16' "$fw" || fail "RFC 1918 not dropped"
	grep -q '^OUT DROP -i net0 -dest 169.254.0.0/16' "$fw" || fail "link-local/metadata not dropped"
	grep -q '^OUT DROP -i net0 -dest 100.64.0.0/10' "$fw" || fail "CGNAT not dropped"
	grep -q '^OUT DROP -i net0 -dest 127.0.0.0/8' "$fw" || fail "loopback not dropped"
	grep -q '^OUT DROP -i net0 -dest fc00::/7' "$fw" || fail "ULA not dropped"
	grep -q '^OUT DROP -i net0 -dest 203.0.113.5 ' "$fw" || fail "the host's public address is not dropped"
	# DNS accepts must come before the drops
	local acc drop
	acc=$(grep -n '^OUT ACCEPT' "$fw" | tail -n1 | cut -d: -f1)
	drop=$(grep -n '^OUT DROP' "$fw" | head -n1 | cut -d: -f1)
	((acc < drop)) || fail "DNS exceptions must precede the drop rules"
	grep -q 'firewall=1' "$FAKE_STATE/qemu/100.conf" || fail "net0 lacks firewall=1"
	expect_no_file /etc/pve/firewall/cluster.fw
	# with the datacenter firewall on there is no warning
	printf '[OPTIONS]\nenable: 1\n' >/etc/pve/firewall/cluster.fw
	cp /etc/pve/firewall/cluster.fw "$T/cluster.fw.before"
	agos --yes --image-file "$IMG" --isolate --name agos-2
	expect_rc 0
	if grep -q 'DISABLED' "$T/err"; then fail "warned although the datacenter firewall is on"; fi
	cmp -s /etc/pve/firewall/cluster.fw "$T/cluster.fw.before" || fail "cluster.fw was modified"
	# destroy removes the rules with the VM
	agos destroy --vmid 100 --yes
	expect_no_file /etc/pve/firewall/100.fw
	finish
}

t_download_and_verify() {
	setup download_and_verify
	local rel="$FAKE_HTTP_ROOT/github.com/kroqdotdev/agos/releases/download/v$V"
	mkdir -p "$rel"
	cp "$IMG" "$T/SHA256SUMS" "$rel/"
	agos --yes --json
	expect_rc 0
	expect_json image_checksum ok
	expect_json image_signature 'unverified (no public key)'
	expect_called "^curl .*--proto =https --tlsv1.2 .*https://github.com/kroqdotdev/agos/releases/download/v$V/SHA256SUMS\$"
	expect_file "$T/cache/$V/agos-$V-amd64.qcow2"
	expect_called "import-from=$T/cache/$V/agos-$V-amd64.qcow2"
	# cached and verified: no second image download
	: >"$FAKE_LOG"
	agos --yes --json --name second
	expect_rc 0
	expect_not_called "curl .*agos-$V-amd64.qcow2"
	expect_err 'using cached'
	# tampered release: checksum mismatch -> 3, VM not created
	head -c 1000 /dev/urandom >"$rel/agos-$V-amd64.qcow2"
	rm -f "$T/cache/$V/agos-$V-amd64.qcow2"
	agos --yes --json --name third
	expect_rc 3
	expect_json error "SHA-256 mismatch for agos-$V-amd64.qcow2 (download removed)"
	expect_no_file "$T/cache/$V/agos-$V-amd64.qcow2"
	[[ $(vm_count) == 2 ]] || fail "a VM was created from a bad image"
	# missing release -> 3
	agos --yes --version 9.9.9 --name fourth
	expect_rc 3
	finish
}

t_signature() {
	setup signature
	local rel="$FAKE_HTTP_ROOT/github.com/kroqdotdev/agos/releases/download/v$V" pub v
	mkdir -p "$rel"
	cp "$IMG" "$T/SHA256SUMS" "$rel/"
	# no key + --require-signature fails closed
	agos --yes --require-signature
	expect_rc 3
	expect_err 'no release public key'
	# the built-in release key is set: a release without SHA256SUMS.minisig is refused
	unset AGOS_MINISIGN_PUBKEY
	agos --yes --json --name nosig
	expect_rc 3
	expect_err "cannot download https://.*/v$VR/SHA256SUMS\\.minisig \\(unsigned fork"
	minisign -G -W -p "$T/minisign.pub" -s "$T/minisign.key" >/dev/null 2>&1 || fail "minisign -G failed"
	pub=$(tail -n1 "$T/minisign.pub")
	minisign -S -s "$T/minisign.key" -m "$rel/SHA256SUMS" -x "$rel/SHA256SUMS.minisig" >/dev/null 2>&1 </dev/null ||
		fail "minisign -S failed"
	# signed by a key other than the built-in one -> rejected
	rm -rf "$T/cache"
	agos --yes --json --name wrongkey
	expect_rc 3
	expect_json error 'SHA256SUMS signature verification FAILED; do not use this image'
	# right key: both verifiers accept it (openssl is what a stock Proxmox host has)
	for v in minisign openssl; do
		rm -rf "$T/cache"
		AGOS_VERIFIER=$v AGOS_MINISIGN_PUBKEY=$pub agos --yes --json --require-signature --name "ok-$v"
		expect_rc 0
		expect_json image_signature verified
		expect_err "signature verified \($v\)"
	done
	# legacy (non-prehashed) signatures verify with openssl too
	minisign -S -l -s "$T/minisign.key" -m "$rel/SHA256SUMS" -x "$rel/SHA256SUMS.minisig" >/dev/null 2>&1 </dev/null ||
		fail "minisign -S -l failed"
	rm -rf "$T/cache"
	AGOS_VERIFIER=openssl AGOS_MINISIGN_PUBKEY=$pub agos --yes --json --name legacy
	expect_rc 0
	expect_json image_signature verified
	# tampered checksums, a forged trusted comment, or a foreign key: rejected by both
	minisign -S -s "$T/minisign.key" -m "$rel/SHA256SUMS" -x "$rel/SHA256SUMS.minisig" >/dev/null 2>&1 </dev/null
	cp "$rel/SHA256SUMS" "$T/SHA256SUMS.good"
	printf '%s  extra\n' "$(printf '0%.0s' {1..64})" >>"$rel/SHA256SUMS"
	for v in minisign openssl; do
		rm -rf "$T/cache"
		AGOS_VERIFIER=$v AGOS_MINISIGN_PUBKEY=$pub agos --yes --json --name "tampered-$v"
		expect_rc 3
		expect_json error 'SHA256SUMS signature verification FAILED; do not use this image'
	done
	cp "$T/SHA256SUMS.good" "$rel/SHA256SUMS"
	sed -i '3s/.*/trusted comment: forged/' "$rel/SHA256SUMS.minisig"
	rm -rf "$T/cache"
	AGOS_VERIFIER=openssl AGOS_MINISIGN_PUBKEY=$pub agos --yes --json --name forged
	expect_rc 3
	minisign -G -W -p "$T/other.pub" -s "$T/other.key" >/dev/null 2>&1
	minisign -S -s "$T/other.key" -m "$rel/SHA256SUMS" -x "$rel/SHA256SUMS.minisig" >/dev/null 2>&1 </dev/null
	for v in minisign openssl; do
		rm -rf "$T/cache"
		AGOS_VERIFIER=$v AGOS_MINISIGN_PUBKEY=$pub agos --yes --json --name "other-$v"
		expect_rc 3
	done
	finish
}

t_truncated_download_runs_nothing() {
	setup truncated_download_runs_nothing
	local size cut
	size=$(wc -c <"$SCRIPT")
	for cut in 2000 $((size / 2)) $((size - 40)); do
		head -c "$cut" "$SCRIPT" >"$T/partial.sh"
		bash -c "$(cat "$T/partial.sh")" agos-proxmox.sh --yes --image-file "$IMG" </dev/null >"$T/out" 2>"$T/err"
		if [[ -s $FAKE_LOG ]]; then fail "a script truncated at $cut bytes ran commands"; fi
	done
	# the full script works through bash -c "$(...)" exactly like the one-liner
	bash -c "$(cat "$SCRIPT")" agos-proxmox.sh --dry-run --json --image-file "$IMG" </dev/null >"$T/out" 2>"$T/err"
	RC=$?
	expect_rc 0
	expect_json state planned
	finish
}

t_make_seed() {
	setup make_seed
	local d="$T/seed-src" tool
	cp -r "$REPO/deploy/cloud-init" "$d"
	(cd "$T" && bash "$d/make-seed.sh" -o seed.iso) >"$T/out" 2>"$T/err"
	RC=$?
	expect_rc 1
	expect_err 'placeholder SSH key'
	sed -i 's|ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIREPLACE_WITH_YOUR_PUBLIC_KEY you@laptop|ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTheTestSuiteOnly0000000000000 t@e|' "$d/user-data"
	for tool in genisoimage xorriso; do
		rm -f "$T/seed.iso"
		(cd "$T" && SEED_TOOL=$tool bash "$d/make-seed.sh" -o seed.iso) >"$T/out" 2>"$T/err"
		RC=$?
		expect_rc 0
		expect_out "wrote seed.iso \(label CIDATA, $tool\): user-data meta-data network-config"
		isoinfo -d -i "$T/seed.iso" | grep -q '^Volume id: CIDATA$' || fail "$tool: label is not CIDATA"
		isoinfo -i "$T/seed.iso" -J -x /meta-data | grep -q '^instance-id: ' || fail "$tool: meta-data missing"
		isoinfo -i "$T/seed.iso" -J -x /network-config | grep -q 'dhcp4: true' || fail "$tool: network-config missing"
		[[ $(stat -c %a "$T/seed.iso") == 600 ]] || fail "$tool: seed.iso is not mode 600"
	done
	(cd "$T" && bash "$d/make-seed.sh" -o seed2.iso --no-network-config --network-config /nonexistent) >"$T/out" 2>"$T/err"
	RC=$?
	expect_rc 0
	if isoinfo -i "$T/seed2.iso" -J -f | grep -q network-config; then fail "--no-network-config still added it"; fi
	# the shipped examples pass cloud-init's own schema checks
	cloud-init schema -c "$REPO/deploy/cloud-init/user-data" >"$T/schema.log" 2>&1 ||
		fail "cloud-init rejects deploy/cloud-init/user-data: $(tail -n3 "$T/schema.log")"
	cloud-init schema -t network-config -c "$REPO/deploy/cloud-init/network-config" >"$T/schema.log" 2>&1 ||
		fail "cloud-init rejects deploy/cloud-init/network-config: $(tail -n3 "$T/schema.log")"
	# the example config.toml covers every key of the spec and parses as TOML
	python3 - "$REPO/deploy/cloud-init/user-data" <<'EOF' || fail "example user-data does not cover the spec's config/secrets keys"
import sys, tomllib, yaml
ud = yaml.safe_load(open(sys.argv[1]))
files = {f["path"]: f["content"] for f in ud["write_files"]}
cfg = tomllib.loads(files["/etc/agos/config.toml"])
want = {"display": {"width", "height"}, "viewer": {"listen", "port", "tls", "user"},
        "agentd": {"listen"}, "tailscale": {"enabled", "hostname", "tags", "serve", "ssh"},
        "agents": {"claude_code"}}
assert "hostname" in cfg
for sec, keys in want.items():
    assert keys <= set(cfg[sec]), (sec, keys - set(cfg[sec]))
sec = files["/etc/agos/secrets.env"]
for k in ["TS_AUTHKEY", "VIEWER_PASSWORD", "AGENTD_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY"]:
    assert k + "=" in sec, k
EOF
	finish
}

t_docs_consistency() {
	setup docs_consistency
	local ver flag
	ver=$(tr -d '[:space:]' <"$REPO/VERSION")
	grep -q "^AGOS_DEFAULT_VERSION=\"$ver\"$" "$SCRIPT" || fail "script default version differs from VERSION ($ver)"
	grep -q "^AGOS_SCRIPT_VERSION=\"$ver\"$" "$SCRIPT" || fail "script version differs from VERSION ($ver)"
	grep -q "releases/download/v$ver/agos-proxmox.sh" "$REPO/deploy/install.md" || fail "install.md does not pin v$ver"
	# every flag is documented in the spec and the README
	for flag in $(sed -n '/^is_value_flag()/,/^}/p' "$SCRIPT" | grep -oE -- '--[a-z0-9-]+') --dry-run --json --yes --isolate --require-signature --wizard --no-wizard; do
		grep -q -- "\`$flag\`" "$REPO/docs/spec.md" || fail "spec does not mention $flag"
		grep -q -- "$flag" "$REPO/deploy/README.md" || fail "deploy/README.md does not mention $flag"
	done
	python3 - "$REPO" <<'EOF2' || fail "runbook, permissions and llms.txt disagree"
import json, os, re, sys
repo = sys.argv[1]
st = json.load(open(f"{repo}/deploy/claude-settings.example.json"))["permissions"]
allow, ask, deny = set(st["allow"]), set(st["ask"]), set(st["deny"])
for r in ["Bash(qm destroy *)", "Edit(//etc/pve/**)", "Read(//root/agos.secrets)", "Bash(bash -c *)", "Bash(bash)"]:
    assert r in deny, r
for r in ["Bash(bash /root/agos-proxmox.sh --dry-run *)", "Bash(bash /root/agos-proxmox.sh status *)"]:
    assert r in allow, r
for r in ["Bash(bash /root/agos-proxmox.sh create *)", "Bash(bash /root/agos-proxmox.sh reset *)"]:
    assert r in ask, r
assert st["disableBypassPermissionsMode"] == "disable" and st["disableAutoMode"] == "disable"
for r in allow | ask | deny:
    assert re.fullmatch(r"(Bash|Read|Edit|WebFetch)\(.+\)", r), r
# each download in the runbook is pre-approved verbatim, so the agent never
# improvises a different URL
md = open(f"{repo}/deploy/install.md").read()
blocks = re.findall(r"```bash\n(.*?)```", md, re.S)
lines = [l.strip() for b in blocks for l in b.splitlines() if l.strip()]
curls = [l for l in lines if l.startswith("curl ")]
assert len(curls) == 3, curls
for c in curls:
    assert f"Bash({c})" in allow, c
# dry run and status commands in the runbook hit allow rules, the real run hits an ask rule
assert any(l.startswith("bash /root/agos-proxmox.sh --dry-run ") for l in lines)
assert any(l.startswith("bash /root/agos-proxmox.sh --yes ") for l in lines)
assert not any("| bash" in l or "bash -c" in l for l in lines)
# llms.txt links point at files in this repo (agentd/ and image/ READMEs come from other work)
txt = open(f"{repo}/llms.txt").read()
assert txt.startswith("# agos\n\n> ")
for url in re.findall(r"\]\((https://raw\.githubusercontent\.com/kroqdotdev/agos/main/[^)]+)\)", txt):
    path = url.split("/main/", 1)[1].replace("%20", " ")
    if path in ("agentd/README.md", "image/README.md"):
        continue
    assert os.path.exists(f"{repo}/{path}"), path
EOF2
	finish
}

# ------------------------------------------------------------------ guided setup (whiptail) tests

wiz_setup() {
	setup "$1"
	export AGOS_IMAGE_FILE="$IMG"
	mkdir -p /root/.ssh
	printf '%s\n' 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTheTestSuiteOnly0000000000000 admin@laptop' \
		'from="10.0.0.0/8" ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQFakeRsaKeyForTests00000000 root@pve' \
		'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTheTestSuiteOnly0000000000000 duplicate' >/root/.ssh/authorized_keys
}

answers() { printf '%s\n' "$@" >"$FAKE_WT_ANSWERS"; }

# wizard ARGS...: run the script on a pseudo-terminal with the fake whiptail;
# everything the terminal shows lands in $T/term.
wizard() {
	local cmd="bash '$SCRIPT'" a
	for a in "$@"; do cmd+=" '$a'"; done
	env PATH="$HERE/fake-whiptail:$PATH" TERM=xterm LANG=C.UTF-8 \
		script -qec "$cmd" /dev/null </dev/null >"$T/term" 2>&1
	RC=$?
}

expect_term() { grep -Eq -- "$1" "$T/term" || fail "terminal lacks /$1/"; }
expect_dialogs() {
	local got
	got=$(cut -f1 "$FAKE_WT_LOG" 2>/dev/null | tr '\n' ' ')
	[[ ${got% } == "$*" ]] || fail "dialogs were: '${got% }', expected '$*'$(grep 'fake whiptail' "$FAKE_WT_LOG" 2>/dev/null | head -n1 | sed 's/^/ -- /')"
}
expect_no_dialogs() {
	if [[ -s $FAKE_WT_LOG ]]; then fail "the guided setup opened: $(head -n1 "$FAKE_WT_LOG")"; fi
}
screen() { cat "$FAKE_WT_SCREENS"/"$1"-*.txt 2>/dev/null; }
# Whitespace-insensitive: long dialog texts are folded to the box width.
expect_screen() {
	local pat
	pat=$(printf '%s' "$2" | tr -s ' ')
	screen "$1" | tr -s ' \n' '  ' | grep -Eq -- "$pat" || fail "dialog $1 lacks /$2/"
}

# seed_written VMID PATH: the decoded content of that write_files entry.
seed_written() {
	seed_file "$1" user-data >"$T/ud.yaml"
	python3 - "$T/ud.yaml" "$2" <<'EOF'
import base64, sys, yaml
ud = yaml.safe_load(open(sys.argv[1]))
for f in ud.get("write_files") or []:
    if f["path"] == sys.argv[2]:
        sys.stdout.write(base64.b64decode(f["content"] or "").decode())
        sys.exit(0)
sys.exit(1)
EOF
}

seed_ssh_keys() {
	seed_file "$1" user-data >"$T/ud.yaml"
	python3 -c 'import sys, yaml; print("\n".join(yaml.safe_load(open(sys.argv[1]))["users"][0].get("ssh_authorized_keys") or []))' "$T/ud.yaml"
}

# Secrets typed into the wizard may only reach the seed and the final dialog.
wiz_no_leaks() {
	local f
	for f in "$T/term" "$FAKE_LOG" "$FAKE_WT_LOG" "$FAKE_WT_LOG.argv" "$FAKE_STATE"/qemu/*.conf /etc/pve/firewall/*.fw \
		"$FAKE_WT_SCREENS"/*; do
		[[ -f $f && $f != *-textbox.txt ]] || continue
		if grep -q SENTINEL "$f"; then fail "a secret leaked into $f"; fi
	done
	[[ -z $(ls -A "$T/run") ]] || fail "temp files left behind: $(ls -A "$T/run")"
	if find "$FAKE_ROOT" -name 'agos-seed-*' 2>/dev/null | grep -q .; then fail "seed ISO left on storage"; fi
}

t_wizard_default_tailscale() {
	wiz_setup wizard_default_tailscale
	answers "yesno 0" "menu 0 default" "menu 0 tailscale" "passwordbox 0 tskey-client-SENTINELwts1-abc" \
		"checklist 0 1 2" "inputbox 0" "checklist 0 claude_code t3code" "passwordbox 0 sk-ant-api03-SENTINELwak2" \
		"passwordbox 0" "msgbox 0" "menu 0 create" "textbox 0"
	FAKE_TAILSCALE=1 wizard
	expect_rc 0
	expect_dialogs yesno menu menu passwordbox checklist inputbox checklist passwordbox passwordbox msgbox menu textbox
	expect_term "agos $VR - an unattended desktop"
	expect_term '✓.* Proxmox VE 9\.0\.10'
	expect_term '✓.* KVM available'
	expect_term 'agos VM 100 \(agos\): ready'
	expect_called '^qm create 100 --name agos --tags agos .*--onboot 1 '
	expect_called '^qm snapshot 100 golden'
	seed_written 100 /etc/agos/config.toml >"$T/cfg"
	grep -q '^tags = \["tag:agos"\]$' "$T/cfg" || fail "config.toml lacks tags = [\"tag:agos\"]"
	python3 -c 'import sys, tomllib; c = tomllib.load(open(sys.argv[1], "rb")); assert c["agents"] == {"claude_code": True, "t3code": True}, c' \
		"$T/cfg" || fail "config.toml lacks [agents] claude_code = true, t3code = true"
	seed_written 100 /etc/agos/secrets.env >"$T/sec"
	grep -qx 'TS_AUTHKEY=tskey-client-SENTINELwts1-abc' "$T/sec" || fail "TS_AUTHKEY not in the seed"
	grep -qx 'ANTHROPIC_API_KEY=sk-ant-api03-SENTINELwak2' "$T/sec" || fail "ANTHROPIC_API_KEY not in the seed"
	seed_ssh_keys 100 >"$T/keys"
	[[ $(grep -c . "$T/keys") == 2 ]] || fail "expected the 2 distinct authorized_keys entries in the seed"
	grep -qx 'ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQFakeRsaKeyForTests00000000 root@pve' "$T/keys" || fail "key options were not stripped"
	expect_screen 05 'items: 1 ssh-ed25519 SHA256:[A-Za-z0-9+/]{10} admin@laptop ON 2 ssh-rsa SHA256:'
	expect_screen 07 'items: claude_code Claude Code: .* ON t3code T3 Code: .* ON'
	expect_screen 07 'T3 Code opens on workspace 2'
	expect_screen 10 'access     Tailscale \(OAuth client secret, tags: tag:agos\)'
	expect_screen 10 'secrets from this setup: ANTHROPIC_API_KEY TS_AUTHKEY'
	expect_screen 10 "VM         100 'agos' \(tag agos, starts at boot\)"
	expect_screen 10 'agents     Claude Code, T3 Code'
	expect_screen 12 'Desktop https://agos\.tail1234\.ts\.net/'
	expect_screen 12 'user: agos password: SENTINELview42'
	expect_screen 12 'agentd https://agos\.tail1234\.ts\.net:8765/ token: agd_SENTINELtok42'
	expect_screen 12 'bash .*agos-proxmox\.sh reset --vmid 100 --yes'
	expect_screen 12 'bash .*agos-proxmox\.sh destroy --vmid 100 --yes'
	grep -q -- '--textbox /dev/fd/' "$FAKE_WT_LOG.argv" || fail "the final dialog was not fed through /dev/fd"
	seed_file 100 user-data >"$T/ud.yaml"
	cloud-init schema -c "$T/ud.yaml" >"$T/schema.log" 2>&1 || fail "cloud-init rejects the wizard's user-data: $(tail -n2 "$T/schema.log")"
	wiz_no_leaks
	finish
}

t_wizard_auth_key() {
	wiz_setup wizard_auth_key
	answers "yesno 0" "menu 0 default" "menu 0 tailscale" "passwordbox 0 not-a-key" "msgbox 0" \
		"passwordbox 0 tskey-auth-SENTINELwau1-x" "checklist 0 1" "inputbox 0" "checklist 0 claude_code t3code" \
		"passwordbox 0" "passwordbox 0" "msgbox 0" "menu 0 create" "textbox 0"
	wizard
	expect_rc 0
	expect_screen 05 'must start with tskey-'
	seed_written 100 /etc/agos/config.toml | grep -q '^tags = \[\]$' || fail "auth keys must get tags = []"
	seed_written 100 /etc/agos/secrets.env | grep -qx 'TS_AUTHKEY=tskey-auth-SENTINELwau1-x' || fail "TS_AUTHKEY not in the seed"
	expect_screen 12 'Tailscale \(auth key, tags: none\)'
	wiz_no_leaks
	finish
}

t_wizard_lan() {
	wiz_setup wizard_lan
	answers "yesno 0" "menu 0 default" "menu 0 lan" "passwordbox 0 short" "msgbox 0" \
		"passwordbox 0 SENTINELlanpw-123" "checklist 0 1" "inputbox 0" "checklist 0 claude_code" "passwordbox 0" \
		"passwordbox 0" "msgbox 0" "menu 0 create" "textbox 0"
	FAKE_LAN=1 wizard
	expect_rc 0
	seed_written 100 /etc/agos/config.toml >"$T/cfg"
	grep -q '^listen = "0.0.0.0"$' "$T/cfg" || fail "LAN mode not in config.toml"
	grep -q '^enabled = "false"$' "$T/cfg" || fail "Tailscale should be off in LAN mode"
	python3 -c 'import sys, tomllib; c = tomllib.load(open(sys.argv[1], "rb")); assert c["agents"] == {"claude_code": True, "t3code": False}, c' \
		"$T/cfg" || fail "unticking T3 Code must write t3code = false"
	seed_written 100 /etc/agos/secrets.env | grep -qx 'VIEWER_PASSWORD=SENTINELlanpw-123' || fail "VIEWER_PASSWORD not in the seed"
	expect_screen 12 'LAN: https://<vm-ip>:8444, user agos, password set'
	expect_screen 12 'agents     Claude Code \(T3 Code off\)'
	expect_screen 14 'Desktop https://192\.168\.1\.57:8444/ \(self-signed certificate\)'
	wiz_no_leaks
	finish
}

t_wizard_advanced() {
	wiz_setup wizard_advanced
	export FAKE_STORAGES=$'local dir iso,vztmpl active 81089764\nlocal-lvm lvmthin images,rootdir active 355760512\nbig zfspool images,rootdir active 900000000'
	export FAKE_BRIDGES="vmbr0 vmbr1"
	answers "yesno 0" "menu 0 advanced" "inputbox 0 abc" "msgbox 0" "inputbox 0 150" "inputbox 0 bad_name" "msgbox 0" \
		"inputbox 0 box1" "menu 0 big" "menu 0 vmbr1" "inputbox 0 2" "inputbox 0 4096" "inputbox 0 32" \
		"menu 0 x86-64-v2-AES" "yesno 1" "yesno 0" "yesno 0" "menu 0 ssh" "checklist 0" "inputbox 0" "yesno 0" \
		"checklist 0" "passwordbox 0" "passwordbox 0" "msgbox 0" "menu 0 commands" "msgbox 0" "menu 0 create" \
		"textbox 0"
	wizard
	expect_rc 0
	expect_called '^qm create 150 --name box1 --tags agos .*--cpu x86-64-v2-AES --cores 2 --memory 4096 .*--net0 virtio,bridge=vmbr1,firewall=1 .*--onboot 0 '
	expect_called '^qm set 150 --efidisk0 big:1,efitype=4m,pre-enrolled-keys=0$'
	expect_called '^qm resize 150 scsi0 32G$'
	expect_file /etc/pve/firewall/150.fw
	seed_written 150 /etc/agos/config.toml >"$T/cfg"
	grep -q '^enabled = "false"$' "$T/cfg" || fail "SSH-only access should turn Tailscale off"
	python3 -c 'import sys, tomllib; c = tomllib.load(open(sys.argv[1], "rb")); assert c["agents"] == {"claude_code": False, "t3code": False}, c' \
		"$T/cfg" || fail "no agent app ticked must write both as false"
	[[ -z $(seed_ssh_keys 150) ]] || fail "no SSH key was selected"
	expect_screen 03 'default: 100'
	expect_screen 09 'items: local-lvm lvmthin, [0-9]+ GiB free big zfspool, [0-9]+ GiB free'
	expect_screen 10 'items: vmbr0 Linux bridge vmbr1 Linux bridge'
	if screen 10 | grep -q fwbr; then fail "firewall bridges must not be offered"; fi
	expect_screen 17 'datacenter firewall is DISABLED'
	expect_screen 21 'Continue without an SSH key'
	expect_screen 25 "VM         150 'box1' \(tag agos, not started at boot\)"
	expect_screen 25 'agents     none \(T3 Code off, Claude Code not pre-seeded\)'
	expect_screen 27 'qm create 150 --name box1'
	expect_screen 29 'SSH no key added'
	expect_screen 27 'Firewall rules for /etc/pve/firewall/150\.fw: OUT ACCEPT'
	expect_screen 25 'isolate EXPERIMENTAL: drops traffic to LAN'
	# no secrets at all: secrets.env must be an empty string, not YAML null
	seed_file 150 user-data >"$T/ud.yaml"
	cloud-init schema -c "$T/ud.yaml" >"$T/schema.log" 2>&1 || fail "cloud-init rejects user-data without secrets: $(tail -n2 "$T/schema.log")"
	grep -q "^    content: ''$" "$T/ud.yaml" || fail "an empty secrets file must be written as content: ''"
	wiz_no_leaks
	finish
}

t_wizard_advanced_tailscale_tags() {
	wiz_setup wizard_advanced_tailscale_tags
	answers "yesno 0" "menu 0 advanced" "inputbox 0 100" "inputbox 0 agos" "menu 0 local-lvm" "menu 0 vmbr0" \
		"inputbox 0 4" "inputbox 0 8192" "inputbox 0 64" "menu 0 host" "yesno 0" "yesno 1" \
		"menu 0 tailscale" "passwordbox 0 tskey-client-SENTINELtag1-y" "inputbox 0 tag:bad!" "msgbox 0" \
		"inputbox 0" "msgbox 0" "inputbox 0 tag:agos,tag:lab" "checklist 0 1" "inputbox 0" \
		"checklist 0 claude_code t3code" "passwordbox 0 foo bar" "msgbox 0" "passwordbox 0 sk-ant-oat01-SENTINELoat1" \
		"passwordbox 0 sk-proj-SENTINELoai1" "msgbox 0" "menu 0 create" "textbox 0"
	wizard
	expect_rc 0
	expect_screen 15 'default: tag:agos'
	expect_screen 16 'Tags look like tag:agos'
	expect_screen 18 'OAuth client secret can only create tagged devices'
	seed_written 100 /etc/agos/config.toml | grep -q '^tags = \["tag:agos", "tag:lab"\]$' || fail "edited tags not in config.toml"
	seed_written 100 /etc/agos/secrets.env >"$T/sec"
	grep -qx 'CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-SENTINELoat1' "$T/sec" || fail "an sk-ant-oat token must be CLAUDE_CODE_OAUTH_TOKEN"
	grep -qx 'OPENAI_API_KEY=sk-proj-SENTINELoai1' "$T/sec" || fail "OPENAI_API_KEY not in the seed"
	if grep -q ANTHROPIC_API_KEY "$T/sec"; then fail "the OAuth token was stored as ANTHROPIC_API_KEY"; fi
	wiz_no_leaks
	finish
}

t_wizard_existing_secrets() {
	wiz_setup wizard_existing_secrets
	printf '%s\n' 'TS_AUTHKEY=tskey-auth-SENTINELexist1' 'ANTHROPIC_API_KEY=sk-ant-api03-SENTINELexist2' >/root/agos.secrets
	chmod 600 /root/agos.secrets
	cp /root/agos.secrets "$T/secrets.before"
	answers "yesno 0" "menu 0 default" "yesno 0" "menu 0 tailscale" "msgbox 0" "checklist 0 1 2" "inputbox 0" \
		"checklist 0 claude_code t3code" "passwordbox 0" "msgbox 0" "menu 0 create" "textbox 0"
	wizard
	expect_rc 0
	expect_dialogs yesno menu yesno menu msgbox checklist inputbox checklist passwordbox msgbox menu textbox
	expect_screen 03 'ANTHROPIC_API_KEY TS_AUTHKEY'
	expect_screen 05 'Using TS_AUTHKEY from /root/agos.secrets'
	expect_screen 10 'secrets from this setup and /root/agos.secrets: ANTHROPIC_API_KEY TS_AUTHKEY'
	seed_written 100 /etc/agos/secrets.env >"$T/sec"
	grep -qx 'TS_AUTHKEY=tskey-auth-SENTINELexist1' "$T/sec" || fail "existing TS_AUTHKEY not in the seed"
	grep -qx 'ANTHROPIC_API_KEY=sk-ant-api03-SENTINELexist2' "$T/sec" || fail "existing ANTHROPIC_API_KEY not in the seed"
	seed_written 100 /etc/agos/config.toml | grep -q '^tags = \[\]$' || fail "an existing auth key must get tags = []"
	cmp -s /root/agos.secrets "$T/secrets.before" || fail "/root/agos.secrets was modified"
	wiz_no_leaks
	finish
}

t_wizard_existing_secrets_unsafe() {
	wiz_setup wizard_existing_secrets_unsafe
	printf '%s\n' 'TS_AUTHKEY=tskey-auth-SENTINELloose1' >/root/agos.secrets
	chmod 644 /root/agos.secrets
	answers "yesno 0" "menu 0 default" "msgbox 0" "menu 0 ssh" "checklist 0 1" "inputbox 0" \
		"checklist 0 claude_code t3code" "passwordbox 0" "passwordbox 0" "msgbox 0" "menu 0 create" "textbox 0"
	wizard
	expect_rc 0
	expect_screen 03 'cannot use it'
	expect_screen 03 'chmod 600 /root/agos.secrets'
	if seed_written 100 /etc/agos/secrets.env | grep -q TS_AUTHKEY; then fail "a world-readable secrets file was used"; fi
	wiz_no_leaks
	finish
}

t_wizard_cancel() {
	local c
	local -a cases=(
		"yesno 1"
		"yesno 0|menu 255"
		"yesno 0|menu 0 default|menu 0 tailscale|passwordbox 1"
		"yesno 0|menu 0 default|menu 0 tailscale|passwordbox 0 tskey-client-SENTINELcan1|checklist 0 1|inputbox 0|checklist 0 t3code|passwordbox 0 sk-ant-api03-SENTINELcan2|passwordbox 0|msgbox 255"
		"yesno 0|menu 0 default|menu 0 tailscale|passwordbox 0 tskey-client-SENTINELcan3|checklist 0 1|inputbox 0|checklist 0 claude_code t3code|passwordbox 0|passwordbox 0|msgbox 0|menu 0 quit"
		"yesno 0|menu 0 default|menu 0 tailscale|passwordbox 0 tskey-client-SENTINELcan4|checklist 0 1|inputbox 0|checklist 255"
		"yesno 0|menu 0 advanced|inputbox 1"
	)
	wiz_setup wizard_cancel
	for c in "${cases[@]}"; do
		rm -rf "$FAKE_WT_SCREENS" "$FAKE_WT_LOG" "$FAKE_WT_LOG.n" "$FAKE_WT_LOG.argv"
		: >"$FAKE_LOG"
		IFS='|' read -ra lines <<<"$c"
		answers "${lines[@]}"
		wizard
		[[ $RC == 1 ]] || fail "[$c] exit code $RC, expected 1"
		# it must stop at the scripted Cancel/ESC/Quit, not run out of answers later
		if grep -q 'fake whiptail' "$FAKE_WT_LOG"; then fail "[$c] $(grep 'fake whiptail' "$FAKE_WT_LOG" | head -n1)"; fi
		[[ $(grep -c . "$FAKE_WT_LOG") == "${#lines[@]}" ]] || fail "[$c] $(grep -c . "$FAKE_WT_LOG") dialogs for ${#lines[@]} answers"
		grep -q 'aborted, nothing changed' "$T/term" || fail "[$c] no 'aborted, nothing changed'"
		expect_not_called '^qm (create|set|start|snapshot)'
		[[ $(vm_count) == 0 ]] || fail "[$c] a VM was created"
		wiz_no_leaks
	done
	finish
}

t_wizard_off_conditions() {
	wiz_setup wizard_off_conditions
	answers "yesno 0"
	# a terminal, but flags or env ask for the unattended behaviour
	wizard --no-wizard
	expect_rc 1
	expect_term 'Create VM 100'
	expect_no_dialogs
	AGOS_NO_WIZARD=1 wizard
	expect_rc 1
	expect_no_dialogs
	wizard --dry-run
	expect_rc 0
	expect_term 'plan \(dry run'
	expect_no_dialogs
	AGOS_DRY_RUN=1 AGOS_JSON=1 wizard
	expect_rc 0
	expect_term '"state":"planned"'
	expect_no_dialogs
	wizard --yes
	expect_rc 0
	expect_called '^qm create 100'
	expect_no_dialogs
	AGOS_YES=1 wizard
	expect_rc 0
	expect_term 'already exists'
	expect_no_dialogs
	wizard status
	expect_rc 0
	expect_no_dialogs
	# a terminal exists but stdout is captured (an agent harness): no dialogs
	env PATH="$HERE/fake-whiptail:$PATH" TERM=xterm \
		script -qec "AGOS_NAME=other bash '$SCRIPT' >'$T/captured'" /dev/null </dev/null >"$T/term" 2>&1
	RC=$?
	expect_rc 1
	expect_no_dialogs
	# no terminal: exactly the old behaviour
	AGOS_NAME=other agos
	expect_rc 1
	expect_err 'no terminal'
	agos --wizard
	expect_rc 1
	expect_err '--wizard needs a terminal'
	expect_no_dialogs
	wizard --wizard --json
	expect_rc 1
	expect_term 'cannot be combined with --json'
	wizard --wizard status
	expect_rc 1
	wizard --wizard --no-wizard
	expect_rc 1
	wizard --wizard --config-file "$HERE/fixtures/config-lan.toml"
	expect_rc 1
	expect_term 'drop --config-file'
	expect_no_dialogs
	finish
}

t_wizard_needs_whiptail() {
	wiz_setup wizard_needs_whiptail
	env TERM=xterm script -qec "bash '$SCRIPT'" /dev/null </dev/null >"$T/term" 2>&1
	RC=$?
	expect_rc 2
	expect_term 'apt install whiptail'
	finish
}

t_wizard_curl_pipe() {
	wiz_setup wizard_curl_pipe
	answers "yesno 0" "menu 0 default" "menu 255"
	# `curl ... | bash`: stdin is the script, the dialogs still reach the terminal
	env PATH="$HERE/fake-whiptail:$PATH" TERM=xterm LANG=C.UTF-8 \
		script -qec "cat '$SCRIPT' | bash" /dev/null </dev/null >"$T/term" 2>&1
	RC=$?
	expect_rc 1
	expect_dialogs yesno menu menu
	expect_term 'aborted, nothing changed'
	finish
}

t_wizard_flags_preseed() {
	wiz_setup wizard_flags_preseed
	answers "yesno 0" "menu 255"
	wizard --wizard --cores 2 --name preset
	expect_rc 1
	expect_screen 02 "Default: VM 'preset', 2 cores"
	# --secrets-file and --ssh-key-file become what the wizard offers
	printf '%s\n' 'OPENAI_API_KEY=sk-proj-SENTINELflag1' >"$T/flag.secrets"
	chmod 600 "$T/flag.secrets"
	rm -rf "$FAKE_WT_SCREENS" "$FAKE_WT_LOG" "$FAKE_WT_LOG.n"
	answers "yesno 0" "menu 0 default" "yesno 0" "menu 255"
	wizard --wizard --secrets-file "$T/flag.secrets" --ssh-key-file "$T/id.pub"
	expect_rc 1
	expect_screen 03 "Found $T/flag.secrets with these keys"
	# --agents preselects the "Agent apps" boxes
	rm -rf "$FAKE_WT_SCREENS" "$FAKE_WT_LOG" "$FAKE_WT_LOG.n"
	answers "yesno 0" "menu 0 default" "menu 0 ssh" "checklist 0 1" "inputbox 0" "checklist 255"
	wizard --wizard --agents t3code
	expect_rc 1
	expect_screen 06 'items: claude_code Claude Code: .* OFF t3code T3 Code: .* ON'
	finish
}

# ------------------------------------------------------------------ main

TESTS=(t_help t_usage_errors t_not_root t_preflight_failures t_secrets_file_checks
	t_dry_run_human t_dry_run_json t_dry_run_download_checks t_create t_create_idempotent
	t_create_arm64 t_create_dir_storage t_create_with_config_and_network t_agents_flag t_create_import_fallback
	t_create_failure_cleans_up t_create_timeout t_create_guest_failed t_create_vm_died t_no_tty_needs_yes
	t_tty_prompt t_env_equivalents t_vmid_in_use t_status t_reset t_destroy t_isolate
	t_download_and_verify t_signature t_truncated_download_runs_nothing t_make_seed
	t_docs_consistency t_wizard_default_tailscale t_wizard_auth_key t_wizard_lan t_wizard_advanced
	t_wizard_advanced_tailscale_tags t_wizard_existing_secrets t_wizard_existing_secrets_unsafe t_wizard_cancel
	t_wizard_off_conditions t_wizard_needs_whiptail t_wizard_curl_pipe t_wizard_flags_preseed)

for t in "${TESTS[@]}"; do
	if [[ -n $ONLY && $t != *"$ONLY"* ]]; then continue; fi
	"$t"
done

printf '\n%d passed, %d failed\n' "$PASS" "${#FAILED[@]}"
if ((${#FAILED[@]})); then
	printf 'failed: %s\n' "${FAILED[*]}"
	exit 1
fi
