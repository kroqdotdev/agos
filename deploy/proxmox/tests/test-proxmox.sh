#!/usr/bin/env bash
# Tests for agos-proxmox.sh and make-seed.sh against a fake Proxmox VE.
# Runs inside the debian:trixie test container; start it with ./run.sh.
set -uo pipefail

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
REPO=$(cd -- "$HERE/../../.." && pwd)
SCRIPT="$REPO/deploy/proxmox/agos-proxmox.sh"
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
	mkdir -p /etc/pve/firewall
	rm -f /etc/pve/firewall/*.fw /etc/pve/firewall/cluster.fw
	# Sentinel secrets: these strings must never show up anywhere but inside the seed.
	printf '%s\n' '# test secrets' 'TS_AUTHKEY=tskey-client-SENTINELts111' \
		'ANTHROPIC_API_KEY=sk-ant-SENTINELak222' 'VIEWER_PASSWORD=SENTINELpw333' >"$T/agos.secrets"
	chmod 600 "$T/agos.secrets"
	head -c 300000 /dev/urandom >"$T/agos-0.1.0-amd64.qcow2"
	head -c 300000 /dev/urandom >"$T/agos-0.1.0-arm64.qcow2"
	(cd "$T" && sha256sum agos-0.1.0-amd64.qcow2 agos-0.1.0-arm64.qcow2 >SHA256SUMS)
	printf '%s\n' 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTheTestSuiteOnly0000000000000 tester@example' >"$T/id.pub"
	IMG="$T/agos-0.1.0-amd64.qcow2"
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
	local rel="$FAKE_HTTP_ROOT/github.com/kroqdotdev/agos/releases/download/v0.1.0"
	agos --dry-run --json
	expect_rc 3
	expect_json error 'not reachable: https://github.com/kroqdotdev/agos/releases/download/v0.1.0/agos-0.1.0-amd64.qcow2'
	mkdir -p "$rel"
	cp "$IMG" "$T/SHA256SUMS" "$rel/"
	agos --dry-run --json
	expect_rc 0
	expect_json image_url 'https://github.com/kroqdotdev/agos/releases/download/v0.1.0/agos-0.1.0-amd64.qcow2'
	expect_called '^curl -fsSL --proto =https --tlsv1.2 --connect-timeout 20 -r 0-0 -o /dev/null https://github.com/kroqdotdev/agos/releases/download/v0.1.0/agos-0.1.0-amd64.qcow2$'
	expect_no_file "$T/cache/0.1.0/agos-0.1.0-amd64.qcow2"
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
	FAKE_ARCH=arm64 agos --yes --json --image-file "$T/agos-0.1.0-arm64.qcow2" --secrets-file "$T/agos.secrets"
	expect_rc 0
	expect_json state ready
	expect_called '^qm create 100 .*--cpu host'
	expect_not_called '--machine q35'
	expect_called '^qm set 100 --efidisk0 local-lvm:1,pre-enrolled-keys=0$'
	expect_called '^qm set 100 --scsi1 local:iso/agos-seed-100.iso,media=cdrom$'
	expect_called '^qm set 100 --scsi1 none,media=cdrom$'
	expect_not_called '--ide2'
	FAKE_ARCH=arm64 agos --dry-run --image-file "$T/agos-0.1.0-arm64.qcow2" --name other --cpu x86-64-v2-AES
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
	agos reset --yes --json
	expect_rc 0
	expect_json action reset
	expect_json state ready
	expect_called '^qm stop 100$'
	expect_called '^qm rollback 100 golden$'
	expect_called '^qm start 100$'
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
	local rel="$FAKE_HTTP_ROOT/github.com/kroqdotdev/agos/releases/download/v0.1.0"
	mkdir -p "$rel"
	cp "$IMG" "$T/SHA256SUMS" "$rel/"
	agos --yes --json
	expect_rc 0
	expect_json image_checksum ok
	expect_json image_signature 'unverified (no public key)'
	expect_called '^curl .*--proto =https --tlsv1.2 .*https://github.com/kroqdotdev/agos/releases/download/v0.1.0/SHA256SUMS$'
	expect_file "$T/cache/0.1.0/agos-0.1.0-amd64.qcow2"
	expect_called "import-from=$T/cache/0.1.0/agos-0.1.0-amd64.qcow2"
	# cached and verified: no second image download
	: >"$FAKE_LOG"
	agos --yes --json --name second
	expect_rc 0
	expect_not_called 'curl .*agos-0.1.0-amd64.qcow2'
	expect_err 'using cached'
	# tampered release: checksum mismatch -> 3, VM not created
	head -c 1000 /dev/urandom >"$rel/agos-0.1.0-amd64.qcow2"
	rm -f "$T/cache/0.1.0/agos-0.1.0-amd64.qcow2"
	agos --yes --json --name third
	expect_rc 3
	expect_json error 'SHA-256 mismatch for agos-0.1.0-amd64.qcow2 (download removed)'
	expect_no_file "$T/cache/0.1.0/agos-0.1.0-amd64.qcow2"
	[[ $(vm_count) == 2 ]] || fail "a VM was created from a bad image"
	# missing release -> 3
	agos --yes --version 9.9.9 --name fourth
	expect_rc 3
	finish
}

t_signature() {
	setup signature
	local rel="$FAKE_HTTP_ROOT/github.com/kroqdotdev/agos/releases/download/v0.1.0" pub v
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
	expect_err 'cannot download https://.*/v0\.1\.0/SHA256SUMS\.minisig \(unsigned fork'
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
	for flag in $(sed -n '/^is_value_flag()/,/^}/p' "$SCRIPT" | grep -oE -- '--[a-z0-9-]+') --dry-run --json --yes --isolate --require-signature; do
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

# ------------------------------------------------------------------ main

TESTS=(t_help t_usage_errors t_not_root t_preflight_failures t_secrets_file_checks
	t_dry_run_human t_dry_run_json t_dry_run_download_checks t_create t_create_idempotent
	t_create_arm64 t_create_dir_storage t_create_with_config_and_network t_create_import_fallback
	t_create_failure_cleans_up t_create_timeout t_create_guest_failed t_create_vm_died t_no_tty_needs_yes
	t_tty_prompt t_env_equivalents t_vmid_in_use t_status t_reset t_destroy t_isolate
	t_download_and_verify t_signature t_truncated_download_runs_nothing t_make_seed
	t_docs_consistency)

for t in "${TESTS[@]}"; do
	if [[ -n $ONLY && $t != *"$ONLY"* ]]; then continue; fi
	"$t"
done

printf '\n%d passed, %d failed\n' "$PASS" "${#FAILED[@]}"
if ((${#FAILED[@]})); then
	printf 'failed: %s\n' "${FAILED[*]}"
	exit 1
fi
