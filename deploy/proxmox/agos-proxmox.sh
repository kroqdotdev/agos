#!/usr/bin/env bash
# agos-proxmox.sh - create and manage agos VMs on a Proxmox VE 8.x/9.x host.
#
# One-liner (shows the plan and asks before changing anything):
#   bash -c "$(curl -fsSL --proto '=https' --tlsv1.2 https://raw.githubusercontent.com/kroqdotdev/agos/main/deploy/proxmox/agos-proxmox.sh)"
#
# Run with --help for subcommands, flags and exit codes. Human docs:
# deploy/README.md; agent runbook: deploy/install.md; contract: docs/spec.md.
#
# Everything below is function definitions; main runs on the last line, so a
# truncated download (bash -c "$(curl ...)" never sees curl's exit status)
# executes nothing.

set -Eeuo pipefail

AGOS_SCRIPT_VERSION="0.1.0"
AGOS_DEFAULT_VERSION="0.1.0"
AGOS_DEFAULT_REPO="kroqdotdev/agos"
# Release signing key (the second line of minisign.pub, key id
# 0B3E93043C48241E). Forks override it with AGOS_MINISIGN_PUBKEY, or set it
# to "none" if they publish unsigned releases.
AGOS_MINISIGN_PUBKEY_DEFAULT="RWQeJEg8BJM+C3FntUGlSkbqk9PU0z3FPLJS3z4fsjfXedsmchg2OFu1"

EX_USAGE=1
EX_PREFLIGHT=2
EX_DOWNLOAD=3
EX_VMOP=4
EX_TIMEOUT=5

usage() {
	cat <<EOF
agos-proxmox.sh $AGOS_SCRIPT_VERSION - run agos (an unattended desktop for AI agents) as a Proxmox VE VM

Usage: agos-proxmox.sh [create|status|reset|destroy] [flags]

Subcommands:
  create    (default) download + verify the image, create the VM, boot it with a
            NoCloud seed, wait for first boot, take the 'golden' snapshot
  status    show agos VMs (state, IP, URLs)
  reset     roll a VM back to its 'golden' snapshot and start it
  destroy   delete an agos VM (needs --yes and --vmid or --name; agos-tagged VMs only)

Flags (environment variable in brackets):
  --dry-run              check everything, print the plan, change nothing [AGOS_DRY_RUN=1]
  --json                 one JSON object on stdout; progress goes to stderr [AGOS_JSON=1]
  -y, --yes              do not ask for confirmation [AGOS_YES=1]
  --vmid N               VMID (default: next free) [AGOS_VMID]
  --name NAME            VM name and guest hostname (default: agos) [AGOS_NAME]
  --storage ID           storage for the disks (default: active 'images' storage with most space) [AGOS_STORAGE]
  --iso-storage ID       storage for the seed ISO (default: 'local' if it holds ISOs) [AGOS_ISO_STORAGE]
  --bridge BR            network bridge (default: vmbr0) [AGOS_BRIDGE]
  --cores N              vCPUs (default: 4) [AGOS_CORES]
  --memory MiB           RAM in MiB (default: 8192) [AGOS_MEMORY]
  --disk SIZE            system disk size, e.g. 64G (default: 64G) [AGOS_DISK]
  --cpu TYPE             CPU type (default: host; x86-64-v2-AES for mixed clusters) [AGOS_CPU]
  --version VER          agos release (default: $AGOS_DEFAULT_VERSION) [AGOS_VERSION]
  --image-url URL        image URL; SHA256SUMS must sit next to it [AGOS_IMAGE_URL]
  --image-file PATH      local qcow2, no download (testing) [AGOS_IMAGE_FILE]
  --image-sha256 HEX     expected SHA-256 instead of SHA256SUMS [AGOS_IMAGE_SHA256]
  --require-signature    fail unless SHA256SUMS.minisig verifies [AGOS_REQUIRE_SIGNATURE=1]
  --ssh-key-file PATH    public keys for user 'agent' [AGOS_SSH_KEY_FILE]
  --secrets-file PATH    KEY=value secrets, mode 0600 (default: /root/agos.secrets) [AGOS_SECRETS_FILE]
  --config-file PATH     config.toml for /etc/agos/config.toml [AGOS_CONFIG_FILE]
  --network-config PATH  cloud-init network-config (default: DHCP) [AGOS_NETWORK_CONFIG]
  --isolate              EXPERIMENTAL: host firewall drops VM traffic to LAN, link-local/
                         metadata, CGNAT, loopback and ULA addresses [AGOS_ISOLATE=1]
  --isolate-dns IP[,IP]  extra resolvers the isolated VM may query on port 53 [AGOS_ISOLATE_DNS]
  --timeout SECONDS      wait for first boot (default: 900) [AGOS_TIMEOUT]
  -h, --help             this help

Other environment: AGOS_REPO (default $AGOS_DEFAULT_REPO), AGOS_CACHE_DIR (default
/var/cache/agos), AGOS_MINISIGN_PUBKEY (release key; "none" for unsigned forks),
AGOS_VERIFIER (auto|minisign|openssl), AGOS_POLL_INTERVAL (seconds, default 5).

The SHA256SUMS signature is checked with minisign if installed, otherwise
with openssl (present on every Proxmox host).

Exit codes: 0 ok, 1 usage/needs confirmation, 2 preflight failed, 3 download or
verification failed, 4 VM operation failed, 5 timed out waiting for first boot.

Secrets are read only from --secrets-file, never from the command line, and are
never printed. Examples:
  agos-proxmox.sh --dry-run                 # plan only
  agos-proxmox.sh --yes --ssh-key-file /root/.ssh/authorized_keys   # the keys you log in to this host with
  agos-proxmox.sh status --json
  agos-proxmox.sh reset --name agos --yes
EOF
}

# ------------------------------------------------------------------ output

setup_colors() {
	C_RED="" C_YEL="" C_BLU="" C_BLD="" C_OFF=""
	if [[ -t 2 && -z ${NO_COLOR:-} && ${TERM:-dumb} != dumb ]]; then
		C_RED=$'\e[31m' C_YEL=$'\e[33m' C_BLU=$'\e[34m' C_BLD=$'\e[1m' C_OFF=$'\e[0m'
	fi
}

# Progress and problems go to stderr; reports go to stdout (or JSON with --json).
log() { printf '%s\n' "$*" >&2; }
info() { log "${C_BLU}==>${C_OFF} $*"; }
warn() { log "${C_YEL}warning:${C_OFF} $*"; }
err() { log "${C_RED}error:${C_OFF} $*"; }
out() { printf '%s\n' "$*"; }

die() {
	local code=$1
	shift
	err "$*"
	FAIL_MSG="$*"
	DIED=1
	exit "$code"
}

usage_error() {
	err "$*"
	log "Run with --help for usage."
	FAIL_MSG="$*"
	DIED=1
	exit "$EX_USAGE"
}

# The phase decides the exit code of an unexpected failure (a command that
# fails under set -e without an explicit die).
phase() { PHASE_CODE=$1; }

json_str() {
	local s=$1
	s=${s//\\/\\\\}
	s=${s//\"/\\\"}
	s=${s//$'\n'/\\n}
	s=${s//$'\r'/\\r}
	s=${s//$'\t'/\\t}
	s=$(printf '%s' "$s" | tr -d '\000-\010\013\014\016-\037')
	printf '"%s"' "$s"
}

json_str_or_null() {
	if [[ -n $1 ]]; then json_str "$1"; else printf 'null'; fi
}

json_arr() {
	local res="" v
	for v in "$@"; do res+="${res:+,}$(json_str "$v")"; done
	printf '[%s]' "$res"
}

jf_reset() { JF=(); }
jf() { JF+=("$(json_str "$1"):$2"); }
jfs() { jf "$1" "$(json_str_or_null "$2")"; }
jfn() {
	if [[ $2 =~ ^-?[0-9]+$ ]]; then jf "$1" "$2"; else jf "$1" null; fi
}
jfb() {
	if [[ $2 == 1 ]]; then jf "$1" true; else jf "$1" false; fi
}
jf_emit() {
	local IFS=,
	printf '{%s}\n' "${JF[*]}"
	JSON_DONE=1
}

# Commands are shown quoted so they can be copied; they never contain secrets.
quote_cmd() {
	local a res=""
	for a in "$@"; do
		if [[ $a =~ ^[A-Za-z0-9_./:=,+@%-]+$ ]]; then
			res+="$a "
		else
			res+="'${a//\'/\'\\\'\'}' "
		fi
	done
	printf '%s' "${res% }"
}

# Mutating command: recorded in the plan, skipped in --dry-run.
run() {
	PLAN+=("$(quote_cmd "$@")")
	if ((DRY_RUN)); then
		return 0
	fi
	log "  + $(quote_cmd "$@")"
	"$@"
}

# ------------------------------------------------------------------ JSON (perl ships with every PVE host)

# json_get KEY... : print the value at that path of the JSON on stdin.
json_get() {
	# shellcheck disable=SC2016
	perl -MJSON::PP -e '
		my $d = eval { JSON::PP->new->allow_nonref->decode(do { local $/; <STDIN> }) };
		exit 2 if $@;
		for my $k (@ARGV) {
			if (ref $d eq "HASH") { exit 1 unless exists $d->{$k}; $d = $d->{$k} }
			elsif (ref $d eq "ARRAY") { exit 1 unless $k =~ /^\d+$/ && $k < @$d; $d = $d->[$k] }
			else { exit 1 }
		}
		exit 1 unless defined $d;
		if (JSON::PP::is_bool($d)) { print $d ? "true" : "false" }
		elsif (ref $d) { print JSON::PP->new->canonical->encode($d) }
		else { print $d }
	' "$@"
}

# Stdout of a finished `qm guest exec`, or failure.
guest_out_data() {
	# shellcheck disable=SC2016
	perl -MJSON::PP -e '
		my $d = eval { decode_json(do { local $/; <STDIN> }) } or exit 2;
		exit 1 unless ref $d eq "HASH" && defined $d->{exitcode} && $d->{exitcode} == 0;
		print $d->{"out-data"} // "";
	'
}

# First routable address from `qm guest cmd <vmid> network-get-interfaces`.
guest_pick_ip() {
	# shellcheck disable=SC2016
	perl -MJSON::PP -e '
		my $d = eval { decode_json(do { local $/; <STDIN> }) } or exit 2;
		$d = $d->{result} if ref $d eq "HASH";
		my (@v4, @v6);
		for my $if (@$d) {
			my $n = $if->{name} // "";
			next if $n eq "lo" || $n =~ /^(tailscale|docker|br-|veth|virbr|cni|zt)/;
			for my $a (@{ $if->{"ip-addresses"} // [] }) {
				my $ip = $a->{"ip-address"} // next;
				if (($a->{"ip-address-type"} // "") eq "ipv4") { push @v4, $ip unless $ip =~ /^(127\.|169\.254\.)/ }
				elsif ($ip !~ /^(fe80:|::1$)/i) { push @v6, $ip }
			}
		}
		print $v4[0] // $v6[0] // "";
	'
}

# TSV rows "vmid name node status is_agos" from /cluster/resources.
cluster_vm_rows() {
	# shellcheck disable=SC2016
	perl -MJSON::PP -e '
		my $d = eval { decode_json(do { local $/; <STDIN> }) } or exit 2;
		for my $r (sort { $a->{vmid} <=> $b->{vmid} } grep { ($_->{type} // "") eq "qemu" } @$d) {
			my $agos = (grep { $_ eq "agos" } split /[;, ]+/, ($r->{tags} // "")) ? 1 : 0;
			printf "%s\t%s\t%s\t%s\t%d\n", $r->{vmid}, $r->{name} // "-", $r->{node} // "-", $r->{status} // "unknown", $agos;
		}
	'
}

# ------------------------------------------------------------------ settings

init_settings() {
	ACTION="create"
	ACTION_SET=0
	DRY_RUN=${AGOS_DRY_RUN:-0}
	JSON=${AGOS_JSON:-0}
	YES=${AGOS_YES:-0}
	ISOLATE=${AGOS_ISOLATE:-0}
	REQUIRE_SIG=${AGOS_REQUIRE_SIGNATURE:-0}
	VMID_OPT=${AGOS_VMID:-}
	NAME_OPT=${AGOS_NAME:-}
	STORAGE_OPT=${AGOS_STORAGE:-}
	ISO_STORAGE_OPT=${AGOS_ISO_STORAGE:-}
	BRIDGE=${AGOS_BRIDGE:-vmbr0}
	CORES=${AGOS_CORES:-4}
	MEMORY=${AGOS_MEMORY:-8192}
	DISK=${AGOS_DISK:-64G}
	CPU_TYPE=${AGOS_CPU:-host}
	VERSION=${AGOS_VERSION:-$AGOS_DEFAULT_VERSION}
	IMAGE_URL_OPT=${AGOS_IMAGE_URL:-}
	IMAGE_FILE_OPT=${AGOS_IMAGE_FILE:-}
	IMAGE_SHA256_OPT=${AGOS_IMAGE_SHA256:-}
	SSH_KEY_FILE=${AGOS_SSH_KEY_FILE:-}
	SECRETS_FILE=${AGOS_SECRETS_FILE:-/root/agos.secrets}
	SECRETS_EXPLICIT=0
	if [[ -n ${AGOS_SECRETS_FILE:-} ]]; then SECRETS_EXPLICIT=1; fi
	CONFIG_FILE=${AGOS_CONFIG_FILE:-}
	NETCFG_FILE=${AGOS_NETWORK_CONFIG:-}
	ISOLATE_DNS=${AGOS_ISOLATE_DNS:-}
	TIMEOUT=${AGOS_TIMEOUT:-900}
	REPO=${AGOS_REPO:-$AGOS_DEFAULT_REPO}
	CACHE_DIR=${AGOS_CACHE_DIR:-/var/cache/agos}
	MINISIGN_PUBKEY=${AGOS_MINISIGN_PUBKEY:-$AGOS_MINISIGN_PUBKEY_DEFAULT}
	POLL_INTERVAL=${AGOS_POLL_INTERVAL:-5}
	# Undocumented knobs for the test harness and odd nested setups.
	KVM_DEVICE=${AGOS_KVM_DEVICE:-/dev/kvm}
	TMP_BASE=${AGOS_TMPDIR:-/run}

	PLAN=()
	JF=()
	DIED=0
	JSON_DONE=0
	FAIL_MSG=""
	PHASE_CODE=$EX_USAGE
	CREATED_VMID=""
	KEEP_VM=0
	SEED_TMPDIR=""
	SEED_ISO_PATH=""
	SEED_SLOT=""
	SEED_ATTACHED=0
	VMID=""
	NAME=""
	LAN_MODE=0
	IMAGE_CACHED=0
	NODE=$(uname -n)
	NODE=${NODE%%.*}
}

is_value_flag() {
	case $1 in
	--vmid | --name | --storage | --iso-storage | --bridge | --cores | --memory | --disk | --cpu | \
		--version | --image-url | --image-file | --image-sha256 | --ssh-key-file | --secrets-file | \
		--config-file | --network-config | --isolate-dns | --timeout) return 0 ;;
	esac
	return 1
}

set_opt() {
	case $1 in
	--vmid) VMID_OPT=$2 ;;
	--name) NAME_OPT=$2 ;;
	--storage) STORAGE_OPT=$2 ;;
	--iso-storage) ISO_STORAGE_OPT=$2 ;;
	--bridge) BRIDGE=$2 ;;
	--cores) CORES=$2 ;;
	--memory) MEMORY=$2 ;;
	--disk) DISK=$2 ;;
	--cpu) CPU_TYPE=$2 ;;
	--version) VERSION=$2 ;;
	--image-url) IMAGE_URL_OPT=$2 ;;
	--image-file) IMAGE_FILE_OPT=$2 ;;
	--image-sha256) IMAGE_SHA256_OPT=$2 ;;
	--ssh-key-file) SSH_KEY_FILE=$2 ;;
	--secrets-file)
		SECRETS_FILE=$2
		SECRETS_EXPLICIT=1
		;;
	--config-file) CONFIG_FILE=$2 ;;
	--network-config) NETCFG_FILE=$2 ;;
	--isolate-dns) ISOLATE_DNS=$2 ;;
	--timeout) TIMEOUT=$2 ;;
	esac
}

parse_args() {
	local a
	while (($#)); do
		a=$1
		case $a in
		create | status | reset | destroy)
			if ((ACTION_SET)); then usage_error "more than one subcommand ($ACTION, $a)"; fi
			ACTION=$a
			ACTION_SET=1
			;;
		help | -h | --help) ACTION=help ;;
		--dry-run) DRY_RUN=1 ;;
		--json) JSON=1 ;;
		-y | --yes) YES=1 ;;
		--isolate) ISOLATE=1 ;;
		--require-signature) REQUIRE_SIG=1 ;;
		--*=*)
			is_value_flag "${a%%=*}" || usage_error "unknown flag: ${a%%=*}"
			[[ -n ${a#*=} ]] || usage_error "${a%%=*} needs a value"
			set_opt "${a%%=*}" "${a#*=}"
			;;
		--*)
			is_value_flag "$a" || usage_error "unknown flag: $a"
			[[ $# -ge 2 && -n $2 && $2 != --* ]] || usage_error "$a needs a value"
			set_opt "$a" "$2"
			shift
			;;
		*) usage_error "unexpected argument: $a" ;;
		esac
		shift
	done
}

validate_settings() {
	local b ip
	for b in DRY_RUN JSON YES ISOLATE REQUIRE_SIG; do
		[[ ${!b} =~ ^[01]$ ]] || usage_error "$b must be 0 or 1 (got '${!b}')"
	done
	[[ -z $VMID_OPT || $VMID_OPT =~ ^[1-9][0-9]{2,8}$ ]] || usage_error "--vmid must be a number between 100 and 999999999"
	if [[ -n $NAME_OPT && ! $NAME_OPT =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$ ]]; then
		usage_error "--name must be a DNS label (letters, digits, '-', at most 63)"
	fi
	[[ $CORES =~ ^[1-9][0-9]{0,2}$ ]] || usage_error "--cores must be a positive integer"
	if [[ ! $MEMORY =~ ^[1-9][0-9]{3,6}$ ]] || ((MEMORY < 2048)); then
		usage_error "--memory is in MiB and must be at least 2048"
	fi
	if [[ ! $DISK =~ ^[1-9][0-9]{0,4}G$ ]] || ((${DISK%G} < 16)); then
		usage_error "--disk must look like 64G (at least 16G)"
	fi
	[[ $CPU_TYPE =~ ^[A-Za-z0-9._-]+$ ]] || usage_error "--cpu must be a CPU model such as host or x86-64-v2-AES"
	[[ $VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.]+)?$ ]] || usage_error "--version must look like 0.1.0"
	[[ $TIMEOUT =~ ^[1-9][0-9]*$ ]] || usage_error "--timeout must be a positive number of seconds"
	[[ $POLL_INTERVAL =~ ^[0-9]+(\.[0-9]+)?$ ]] || usage_error "AGOS_POLL_INTERVAL must be a number"
	[[ $BRIDGE =~ ^[A-Za-z0-9._-]{1,15}$ ]] || usage_error "--bridge must be an interface name"
	[[ -z $STORAGE_OPT || $STORAGE_OPT =~ ^[A-Za-z][A-Za-z0-9._-]*$ ]] || usage_error "--storage must be a storage ID"
	[[ -z $ISO_STORAGE_OPT || $ISO_STORAGE_OPT =~ ^[A-Za-z][A-Za-z0-9._-]*$ ]] || usage_error "--iso-storage must be a storage ID"
	[[ $REPO =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || usage_error "AGOS_REPO must look like owner/repo"
	[[ -z $IMAGE_URL_OPT || $IMAGE_URL_OPT =~ ^https://[^[:space:]]+$ ]] || usage_error "--image-url must be an https:// URL"
	[[ -z $IMAGE_SHA256_OPT || $IMAGE_SHA256_OPT =~ ^[0-9a-fA-F]{64}$ ]] || usage_error "--image-sha256 must be 64 hex digits"
	[[ -z $IMAGE_URL_OPT || -z $IMAGE_FILE_OPT ]] || usage_error "use either --image-url or --image-file, not both"
	if [[ -n $IMAGE_SHA256_OPT && $REQUIRE_SIG == 1 ]]; then
		usage_error "--require-signature checks SHA256SUMS; it cannot be combined with --image-sha256"
	fi
	for ip in ${ISOLATE_DNS//,/ }; do
		valid_ip "$ip" || usage_error "--isolate-dns: '$ip' is not an IP address"
	done
	NAME=${NAME_OPT:-agos}
}

valid_ip() {
	[[ $1 =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ || $1 =~ ^[0-9A-Fa-f:]+:[0-9A-Fa-f:.]*$ ]]
}

# ------------------------------------------------------------------ exit handling

on_err() {
	# Subshells (command substitutions) report through their parent.
	if [[ $BASHPID == "$$" ]]; then
		err "command failed (line $1): $2"
	fi
}

on_signal() {
	FAIL_MSG="interrupted"
	exit "$PHASE_CODE"
}

on_exit() {
	local rc=$?
	trap - ERR INT TERM
	set +e
	if ((rc != 0 && DIED == 0)); then
		rc=$PHASE_CODE
		FAIL_MSG=${FAIL_MSG:-"unexpected failure (see messages above)"}
	fi
	cleanup "$rc"
	if ((JSON && !JSON_DONE && rc != 0)); then
		jf_reset
		jfb ok 0
		jfs action "$ACTION"
		jfn exit_code "$rc"
		jfs error "$FAIL_MSG"
		jfn vmid "${VMID:-}"
		jfs name "${NAME:-}"
		jfb dry_run "$DRY_RUN"
		jf_emit
	fi
	exit "$rc"
}

cleanup() {
	local rc=$1
	if [[ -n $CREATED_VMID && $rc -ne 0 && $KEEP_VM -eq 0 ]]; then
		warn "removing the half-created VM $CREATED_VMID"
		qm stop "$CREATED_VMID" >/dev/null 2>&1
		if ! qm destroy "$CREATED_VMID" --purge 1 >/dev/null 2>&1; then
			err "could not destroy VM $CREATED_VMID; remove it by hand: qm destroy $CREATED_VMID --purge 1"
		fi
		SEED_ATTACHED=0
	fi
	seed_remove
	if [[ -n $SEED_TMPDIR && -d $SEED_TMPDIR ]]; then
		rm -rf -- "$SEED_TMPDIR"
	fi
}

# ------------------------------------------------------------------ preflight

require_root() {
	((EUID == 0)) || die "$EX_PREFLIGHT" "run as root on the Proxmox VE host (uid is $EUID)"
}

check_tools() {
	local t
	for t in qm pvesm pvesh pveversion perl; do
		command -v "$t" >/dev/null 2>&1 ||
			die "$EX_PREFLIGHT" "'$t' not found: this does not look like a Proxmox VE host"
	done
	perl -MJSON::PP -e 1 2>/dev/null || die "$EX_PREFLIGHT" "perl JSON::PP is missing"
}

check_pve_version() {
	local v
	v=$(pveversion 2>/dev/null) || die "$EX_PREFLIGHT" "pveversion failed"
	[[ $v =~ pve-manager/([0-9]+)\.([0-9]+)(\.[0-9]+)? ]] ||
		die "$EX_PREFLIGHT" "cannot parse pveversion output: $v"
	PVE_MAJOR=${BASH_REMATCH[1]}
	PVE_VERSION="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}${BASH_REMATCH[3]}"
	case $PVE_MAJOR in
	8 | 9) ;;
	*) die "$EX_PREFLIGHT" "Proxmox VE $PVE_VERSION is not supported (need 8.x or 9.x)" ;;
	esac
}

check_arch() {
	HOST_ARCH=$(dpkg --print-architecture 2>/dev/null) || die "$EX_PREFLIGHT" "dpkg --print-architecture failed"
	case $HOST_ARCH in
	amd64) ;;
	arm64)
		if [[ $CPU_TYPE =~ ^(x86|kvm64|qemu64|Epyc|EPYC|Skylake|Haswell|Broadwell|Cascadelake|Icelake) ]]; then
			die "$EX_USAGE" "--cpu $CPU_TYPE is an x86 model; use host (default) or max on arm64"
		fi
		;;
	*) die "$EX_PREFLIGHT" "host architecture '$HOST_ARCH' is not supported (agos ships amd64 and arm64)" ;;
	esac
}

check_kvm() {
	[[ -c $KVM_DEVICE ]] ||
		die "$EX_PREFLIGHT" "$KVM_DEVICE is missing: enable VT-x/AMD-V (or nested virtualisation) for this host"
}

check_bridge() {
	ip -o link show dev "$BRIDGE" >/dev/null 2>&1 ||
		die "$EX_PREFLIGHT" "bridge '$BRIDGE' not found (list bridges: ip -br link show type bridge)"
	if ! ip -d -o link show dev "$BRIDGE" 2>/dev/null | grep -qwE 'bridge|openvswitch'; then
		warn "'$BRIDGE' does not look like a Linux or OVS bridge"
	fi
}

# Sets ST_NAME ST_TYPE ST_STATUS ST_AVAIL (KiB) for storages with content $1.
load_storages() {
	local res name type status avail
	res=$(pvesm status --content "$1" 2>/dev/null) || die "$EX_PREFLIGHT" "pvesm status --content $1 failed"
	ST_NAME=() ST_TYPE=() ST_STATUS=() ST_AVAIL=()
	# Columns: Name Type Status Total(KiB) Used(KiB) Available(KiB) %
	while read -r name type status _ _ avail _; do
		[[ -n $name && $avail =~ ^[0-9]+$ ]] || continue
		ST_NAME+=("$name") ST_TYPE+=("$type") ST_STATUS+=("$status") ST_AVAIL+=("$avail")
	done < <(tail -n +2 <<<"$res")
}

select_storages() {
	local i best=-1
	load_storages images
	for i in "${!ST_NAME[@]}"; do
		if [[ -n $STORAGE_OPT ]]; then
			if [[ ${ST_NAME[i]} == "$STORAGE_OPT" ]]; then
				[[ ${ST_STATUS[i]} == active ]] || die "$EX_PREFLIGHT" "storage '$STORAGE_OPT' is ${ST_STATUS[i]}"
				best=$i
			fi
		elif [[ ${ST_STATUS[i]} == active ]] && { ((best < 0)) || ((ST_AVAIL[i] > ST_AVAIL[best])); }; then
			best=$i
		fi
	done
	if ((best < 0)); then
		if [[ -n $STORAGE_OPT ]]; then
			die "$EX_PREFLIGHT" "storage '$STORAGE_OPT' does not exist or holds no VM disks (see: pvesm status --content images)"
		fi
		die "$EX_PREFLIGHT" "no active storage for VM disks (see: pvesm status --content images)"
	fi
	STORAGE=${ST_NAME[best]} STORAGE_TYPE=${ST_TYPE[best]} STORAGE_AVAIL_KIB=${ST_AVAIL[best]}

	local need_kib=$((${DISK%G} * 1024 * 1024)) floor_kib=$((10 * 1024 * 1024))
	((STORAGE_AVAIL_KIB >= floor_kib)) ||
		die "$EX_PREFLIGHT" "storage '$STORAGE' has only $((STORAGE_AVAIL_KIB / 1024 / 1024)) GiB free (need at least 10 GiB)"
	if ((STORAGE_AVAIL_KIB < need_kib)); then
		warn "storage '$STORAGE' has $((STORAGE_AVAIL_KIB / 1024 / 1024)) GiB free, less than --disk $DISK (fine if thin-provisioned)"
	fi

	# File storages default to raw, which cannot be snapshotted; the golden
	# snapshot needs qcow2 there (efidisk included).
	DISK_FORMAT_OPT=""
	case $STORAGE_TYPE in
	dir | nfs | cifs | glusterfs) DISK_FORMAT_OPT=",format=qcow2" ;;
	lvm | iscsi | iscsidirect) warn "storage '$STORAGE' ($STORAGE_TYPE) may not support snapshots; 'reset' needs the golden snapshot" ;;
	esac

	ISO_STORAGE=""
	load_storages iso
	for i in "${!ST_NAME[@]}"; do
		[[ ${ST_STATUS[i]} == active ]] || continue
		if [[ -n $ISO_STORAGE_OPT ]]; then
			if [[ ${ST_NAME[i]} == "$ISO_STORAGE_OPT" ]]; then ISO_STORAGE=${ST_NAME[i]}; fi
		elif [[ ${ST_NAME[i]} == local ]]; then
			ISO_STORAGE=local
		elif [[ -z $ISO_STORAGE ]]; then
			ISO_STORAGE=${ST_NAME[i]}
		fi
	done
	if [[ -z $ISO_STORAGE && -n $ISO_STORAGE_OPT ]]; then
		die "$EX_PREFLIGHT" "storage '$ISO_STORAGE_OPT' does not exist, is not active or holds no ISOs (see: pvesm status --content iso)"
	fi
	[[ -n $ISO_STORAGE ]] || die "$EX_PREFLIGHT" "no active storage with 'iso' content for the seed ISO (see: pvesm status --content iso)"
	if [[ $ISO_STORAGE != local ]]; then
		warn "the seed ISO (it contains your secrets) sits on storage '$ISO_STORAGE' until first boot finishes"
	fi
}

pick_vmid() {
	if [[ -n $VMID_OPT ]]; then
		pvesh get /cluster/nextid --vmid "$VMID_OPT" >/dev/null 2>&1 ||
			die "$EX_PREFLIGHT" "VMID $VMID_OPT is already in use"
		VMID=$VMID_OPT
	else
		VMID=$(pvesh get /cluster/nextid 2>/dev/null | tr -dc '0-9') || true
		[[ -n $VMID ]] || die "$EX_PREFLIGHT" "pvesh get /cluster/nextid returned no VMID"
	fi
}

load_vms() {
	local res
	res=$(pvesh get /cluster/resources --type vm --output-format json 2>/dev/null) ||
		die "$EX_PREFLIGHT" "cannot list VMs (pvesh get /cluster/resources failed)"
	VM_ROWS=$(cluster_vm_rows <<<"$res") || die "$EX_PREFLIGHT" "cannot parse the VM list"
	mapfile -t AGOS_IDS < <(awk -F'\t' '$5 == 1 { print $1 }' <<<"$VM_ROWS")
}

# find_vm [vmid] [name]: sets F_VMID F_NAME F_NODE F_STATUS F_AGOS (first match).
find_vm() {
	local want_id=$1 want_name=$2 id name node status agos
	F_VMID="" F_NAME="" F_NODE="" F_STATUS="" F_AGOS=0
	while IFS=$'\t' read -r id name node status agos; do
		[[ -n $id ]] || continue
		if [[ -n $want_id && $id == "$want_id" ]] || [[ -z $want_id && $name == "$want_name" ]]; then
			F_VMID=$id F_NAME=$name F_NODE=$node F_STATUS=$status F_AGOS=$agos
			return 0
		fi
	done <<<"$VM_ROWS"
	return 1
}

check_secrets_file() {
	SECRET_KEYS=()
	HAS_TS=0
	if [[ ! -e $SECRETS_FILE ]]; then
		((SECRETS_EXPLICIT == 0)) || die "$EX_PREFLIGHT" "secrets file $SECRETS_FILE not found"
		warn "no secrets file at $SECRETS_FILE: the VM boots without Tailscale or API keys"
		SECRETS_FILE=""
		return 0
	fi
	[[ -f $SECRETS_FILE && -r $SECRETS_FILE ]] || die "$EX_PREFLIGHT" "$SECRETS_FILE is not a readable file"
	local owner mode bad dup k
	owner=$(stat -c %u -- "$SECRETS_FILE")
	mode=$(stat -c %a -- "$SECRETS_FILE")
	[[ $owner == 0 ]] || die "$EX_PREFLIGHT" "$SECRETS_FILE must be owned by root (run: chown root:root $SECRETS_FILE)"
	(((8#$mode & 8#077) == 0)) ||
		die "$EX_PREFLIGHT" "$SECRETS_FILE is readable by group or others (mode $mode); run: chmod 600 $SECRETS_FILE"
	if grep -q $'\r' -- "$SECRETS_FILE"; then
		die "$EX_PREFLIGHT" "$SECRETS_FILE has CRLF line endings; fix with: sed -i 's/\\r\$//' $SECRETS_FILE"
	fi
	# Line numbers only, never content.
	bad=$(awk '!/^[[:space:]]*(#|$)/ && !/^[A-Za-z_][A-Za-z0-9_]*=/ { printf "%s%d", s, NR; s = "," }' "$SECRETS_FILE")
	[[ -z $bad ]] || die "$EX_PREFLIGHT" "$SECRETS_FILE: line(s) $bad are not KEY=value (values are not shown)"
	mapfile -t SECRET_KEYS < <(sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "$SECRETS_FILE" | sort -u)
	for k in "${SECRET_KEYS[@]}"; do
		case $k in
		TS_AUTHKEY | VIEWER_PASSWORD | AGENTD_TOKEN | ANTHROPIC_API_KEY | CLAUDE_CODE_OAUTH_TOKEN | OPENAI_API_KEY) ;;
		*) warn "$SECRETS_FILE: unknown key $k (passed through anyway)" ;;
		esac
	done
	dup=$(sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "$SECRETS_FILE" | sort | uniq -d | tr '\n' ' ')
	if [[ -n $dup ]]; then warn "$SECRETS_FILE: duplicate keys: $dup"; fi
	if grep -Eq '^TS_AUTHKEY=[^[:space:]]' -- "$SECRETS_FILE"; then HAS_TS=1; fi
	return 0
}

check_config_file() {
	LAN_MODE=0
	[[ -n $CONFIG_FILE ]] || return 0
	[[ -f $CONFIG_FILE && -r $CONFIG_FILE ]] || die "$EX_PREFLIGHT" "config file $CONFIG_FILE not found"
	(($(stat -c %s -- "$CONFIG_FILE") <= 65536)) || die "$EX_PREFLIGHT" "config file $CONFIG_FILE is larger than 64 KiB"
	if command -v python3 >/dev/null 2>&1 && python3 -c 'import tomllib' 2>/dev/null; then
		python3 -c 'import sys, tomllib; tomllib.load(open(sys.argv[1], "rb"))' "$CONFIG_FILE" 2>/dev/null ||
			die "$EX_PREFLIGHT" "config file $CONFIG_FILE is not valid TOML"
	fi
	if grep -Eq 'tskey-|sk-ant-|sk-proj-' -- "$CONFIG_FILE"; then
		warn "$CONFIG_FILE looks like it contains a secret; config.toml is world-readable in the VM, use --secrets-file"
	fi
	# Only words the access hints; the guest's effective config decides.
	if awk '/^\[/ { s = $0 } s ~ /^\[viewer\]/ && /^[[:space:]]*listen[[:space:]]*=[[:space:]]*"0\.0\.0\.0"/ { f = 1 } END { exit !f }' "$CONFIG_FILE"; then
		LAN_MODE=1
	fi
}

check_ssh_keys() {
	SSH_KEYS=()
	[[ -n $SSH_KEY_FILE ]] || return 0
	[[ -f $SSH_KEY_FILE && -r $SSH_KEY_FILE ]] ||
		die "$EX_PREFLIGHT" "SSH key file $SSH_KEY_FILE not found (the keys you log in to this host with are in /root/.ssh/authorized_keys)"
	if grep -q 'PRIVATE KEY' -- "$SSH_KEY_FILE"; then
		die "$EX_PREFLIGHT" "$SSH_KEY_FILE is a private key; pass the .pub file"
	fi
	mapfile -t SSH_KEYS < <(tr -d '\r' <"$SSH_KEY_FILE" |
		grep -E '^(ssh-(ed25519|rsa|dss)|ecdsa-sha2-nistp[0-9]+|sk-(ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com) [A-Za-z0-9+/=]+' || true)
	((${#SSH_KEYS[@]} > 0)) || die "$EX_PREFLIGHT" "no SSH public keys found in $SSH_KEY_FILE"
}

check_netcfg() {
	[[ -n $NETCFG_FILE ]] || return 0
	[[ -f $NETCFG_FILE && -r $NETCFG_FILE ]] || die "$EX_PREFLIGHT" "network-config file $NETCFG_FILE not found"
	grep -Eq '^[[:space:]]*(network:|version:)' -- "$NETCFG_FILE" ||
		die "$EX_PREFLIGHT" "$NETCFG_FILE does not look like a cloud-init network-config (no 'version:')"
}

check_iso_tool() {
	local t
	ISO_TOOL=""
	for t in genisoimage xorriso mkisofs; do
		if command -v "$t" >/dev/null 2>&1; then
			ISO_TOOL=$t
			break
		fi
	done
	[[ -n $ISO_TOOL ]] || die "$EX_PREFLIGHT" "need genisoimage (a qemu-server dependency), xorriso or mkisofs to build the seed ISO"
}

free_kib_at() {
	local d=$1
	while [[ ! -d $d ]]; do d=$(dirname -- "$d"); done
	df -Pk -- "$d" | awk 'NR == 2 { print $4 }'
}

check_firewall_state() {
	DC_FIREWALL=0
	if [[ -r /etc/pve/firewall/cluster.fw ]] &&
		awk '/^\[/ { s = toupper($0) } s == "[OPTIONS]" && /^[[:space:]]*enable:[[:space:]]*[1-9]/ { f = 1 } END { exit !f }' /etc/pve/firewall/cluster.fw; then
		DC_FIREWALL=1
	fi
	if [[ -e /etc/pve/firewall/$VMID.fw ]]; then
		die "$EX_PREFLIGHT" "/etc/pve/firewall/$VMID.fw already exists (left over from an old VM?); remove it or pick another --vmid"
	fi
}

# ------------------------------------------------------------------ image

resolve_image() {
	local base
	IMAGE_NAME="agos-${VERSION}-${HOST_ARCH}.qcow2"
	IMAGE_URL="" SUMS_URL="" SIG_URL="" CACHE_SUB="" SUMS_PATH="" SIG_PATH=""
	SIG_STATUS="not checked" CHECKSUM_STATUS="not checked"
	if [[ -n $IMAGE_FILE_OPT ]]; then
		[[ -f $IMAGE_FILE_OPT ]] || die "$EX_PREFLIGHT" "image file $IMAGE_FILE_OPT not found"
		IMAGE_PATH=$(realpath -e -- "$IMAGE_FILE_OPT")
		IMAGE_NAME=$(basename -- "$IMAGE_PATH")
		IMAGE_SOURCE="file"
		return 0
	fi
	if [[ -n $IMAGE_URL_OPT ]]; then
		IMAGE_URL=$IMAGE_URL_OPT
		base=${IMAGE_URL%%[?#]*}
		IMAGE_NAME=${base##*/}
		base=${base%/*}
		CACHE_SUB="$CACHE_DIR/custom"
	else
		base="https://github.com/${REPO}/releases/download/v${VERSION}"
		IMAGE_URL="$base/$IMAGE_NAME"
		CACHE_SUB="$CACHE_DIR/$VERSION"
	fi
	[[ $IMAGE_NAME =~ ^[A-Za-z0-9._+-]+$ ]] || die "$EX_USAGE" "cannot derive a safe file name from $IMAGE_URL"
	SUMS_URL="$base/SHA256SUMS"
	SIG_URL="$base/SHA256SUMS.minisig"
	IMAGE_PATH="$CACHE_SUB/$IMAGE_NAME"
	SUMS_PATH="$CACHE_SUB/SHA256SUMS"
	SIG_PATH="$CACHE_SUB/SHA256SUMS.minisig"
	IMAGE_SOURCE="download"
}

curl_get() {
	# url dest [progress]
	local progress=(-sS)
	if [[ ${3:-} == progress && -t 2 && $JSON -eq 0 ]]; then progress=(--progress-bar); fi
	curl -fL --proto '=https' --tlsv1.2 --retry 3 --retry-delay 3 --connect-timeout 20 \
		"${progress[@]}" -o "$2.part" "$1" && mv -f -- "$2.part" "$2"
}

sha256_of() { sha256sum -- "$1" | awk '{ print tolower($1) }'; }

expected_sha256() {
	# sums-file name
	awk -v f="$2" '{ n = $2; sub(/^\*/, "", n) } n == f { print tolower($1); exit }' "$1"
}

# minisign_verify_openssl FILE SIGFILE PUBKEY: check a minisign signature with
# openssl alone, because Proxmox hosts ship openssl but not minisign. Handles
# prehashed ("ED", BLAKE2b-512) and legacy ("Ed") signatures and the global
# signature over the trusted comment. Call it only as an if-condition, so a
# failing step returns 1 instead of tripping errexit.
minisign_verify_openssl() {
	local file=$1 sigfile=$2 pub=$3 d tc rc=1
	d=$(mktemp -d) || return 1
	if printf '%s' "$pub" | base64 -d >"$d/pub" 2>/dev/null &&
		[[ $(stat -c %s "$d/pub") == 42 ]] &&
		sed -n 2p "$sigfile" | base64 -d >"$d/sig" 2>/dev/null &&
		[[ $(stat -c %s "$d/sig") == 74 ]] &&
		tc=$(sed -n 3p "$sigfile") && [[ $tc == "trusted comment: "* ]] &&
		sed -n 4p "$sigfile" | base64 -d >"$d/gsig" 2>/dev/null &&
		[[ $(stat -c %s "$d/gsig") == 64 ]] &&
		[[ $(head -c 10 "$d/pub" | tail -c 8 | od -An -tx1) == "$(head -c 10 "$d/sig" | tail -c 8 | od -An -tx1)" ]]; then
		# Ed25519 SubjectPublicKeyInfo: fixed 12-byte DER prefix + the raw key.
		{ printf '\x30\x2a\x30\x05\x06\x03\x2b\x65\x70\x03\x21\x00'; tail -c 32 "$d/pub"; } >"$d/pub.der"
		tail -c 64 "$d/sig" >"$d/sig.bin"
		case $(head -c 2 "$d/sig") in
		ED) openssl dgst -blake2b512 -binary "$file" >"$d/msg" 2>/dev/null ;;
		Ed) cp -- "$file" "$d/msg" ;;
		*) : >"$d/msg"; rm -f "$d/pub.der" ;;
		esac
		{ cat "$d/sig.bin"; printf '%s' "${tc#trusted comment: }"; } >"$d/gmsg"
		if [[ -s $d/pub.der ]] &&
			openssl pkeyutl -verify -pubin -keyform DER -inkey "$d/pub.der" -rawin \
				-in "$d/msg" -sigfile "$d/sig.bin" >/dev/null 2>&1 &&
			openssl pkeyutl -verify -pubin -keyform DER -inkey "$d/pub.der" -rawin \
				-in "$d/gmsg" -sigfile "$d/gsig" >/dev/null 2>&1; then
			rc=0
		fi
	fi
	rm -rf -- "$d"
	return "$rc"
}

verify_signature() {
	local verifier=${AGOS_VERIFIER:-auto}
	if [[ -z $MINISIGN_PUBKEY || $MINISIGN_PUBKEY == none ]]; then
		((REQUIRE_SIG == 0)) || die "$EX_DOWNLOAD" "--require-signature: no release public key (AGOS_MINISIGN_PUBKEY=none)"
		warn "signature checking is off (AGOS_MINISIGN_PUBKEY=none): the SHA256SUMS signature is NOT verified"
		SIG_STATUS="unverified (no public key)"
		return 0
	fi
	if [[ $verifier == auto ]]; then
		if command -v minisign >/dev/null 2>&1; then
			verifier=minisign
		elif command -v openssl >/dev/null 2>&1; then
			verifier=openssl
		else
			verifier=none
		fi
	fi
	if [[ $verifier == none ]]; then
		((REQUIRE_SIG == 0)) || die "$EX_DOWNLOAD" "--require-signature: neither minisign nor openssl is installed"
		warn "neither minisign nor openssl is installed: the SHA256SUMS signature is NOT verified"
		SIG_STATUS="unverified (no verifier)"
		return 0
	fi
	# Releases signed with this key always carry a signature, so a missing one
	# means tampering or a broken release, not an old one.
	curl_get "$SIG_URL" "$SIG_PATH" ||
		die "$EX_DOWNLOAD" "cannot download $SIG_URL (unsigned fork? set AGOS_MINISIGN_PUBKEY=none)"
	if [[ $verifier == minisign ]]; then
		minisign -V -q -m "$SUMS_PATH" -x "$SIG_PATH" -P "$MINISIGN_PUBKEY" >/dev/null 2>&1 ||
			die "$EX_DOWNLOAD" "SHA256SUMS signature verification FAILED; do not use this image"
	elif ! minisign_verify_openssl "$SUMS_PATH" "$SIG_PATH" "$MINISIGN_PUBKEY"; then
		die "$EX_DOWNLOAD" "SHA256SUMS signature verification FAILED; do not use this image"
	fi
	SIG_STATUS="verified"
	info "SHA256SUMS signature verified ($verifier)"
}

# Dry run: prove the release exists without downloading it.
check_image_reachable() {
	local u urls=() need=$((8 * 1024 * 1024)) have
	[[ $IMAGE_SOURCE == download ]] || return 0
	if [[ -f $IMAGE_PATH ]]; then
		IMAGE_CACHED=1
		return 0
	fi
	command -v curl >/dev/null 2>&1 || die "$EX_PREFLIGHT" "curl is missing"
	urls=("$IMAGE_URL")
	if [[ -z $IMAGE_SHA256_OPT ]]; then urls+=("$SUMS_URL"); fi
	# A 1-byte ranged GET rather than HEAD: presigned redirect targets may
	# refuse HEAD.
	for u in "${urls[@]}"; do
		curl -fsSL --proto '=https' --tlsv1.2 --connect-timeout 20 -r 0-0 -o /dev/null "$u" ||
			die "$EX_DOWNLOAD" "not reachable: $u"
	done
	have=$(free_kib_at "$CACHE_SUB")
	((have >= need)) || die "$EX_PREFLIGHT" "need 8 GiB free under $CACHE_SUB for the image (have $((have / 1024 / 1024)) GiB)"
}

fetch_image() {
	local want="" sums need=$((8 * 1024 * 1024)) have
	phase "$EX_DOWNLOAD"
	if [[ $IMAGE_SOURCE == file ]]; then
		sums="$(dirname -- "$IMAGE_PATH")/SHA256SUMS"
		if [[ -n $IMAGE_SHA256_OPT ]]; then
			want=${IMAGE_SHA256_OPT,,}
		elif [[ -f $sums ]]; then
			want=$(expected_sha256 "$sums" "$IMAGE_NAME")
		fi
		if [[ -n $want ]]; then
			[[ $(sha256_of "$IMAGE_PATH") == "$want" ]] || die "$EX_DOWNLOAD" "SHA-256 mismatch for $IMAGE_PATH"
			CHECKSUM_STATUS="ok"
			info "local image checksum OK"
		else
			CHECKSUM_STATUS="unverified"
			warn "local image $IMAGE_PATH is not verified (no SHA256SUMS next to it, no --image-sha256)"
		fi
		return 0
	fi
	command -v curl >/dev/null 2>&1 || die "$EX_PREFLIGHT" "curl is missing"
	mkdir -p -- "$CACHE_SUB"
	if [[ -n $IMAGE_SHA256_OPT ]]; then
		want=${IMAGE_SHA256_OPT,,}
		warn "using --image-sha256; SHA256SUMS and its signature are not checked"
	else
		info "fetching $SUMS_URL"
		curl_get "$SUMS_URL" "$SUMS_PATH" || die "$EX_DOWNLOAD" "cannot download $SUMS_URL"
		verify_signature
		want=$(expected_sha256 "$SUMS_PATH" "$IMAGE_NAME")
	fi
	[[ $want =~ ^[0-9a-f]{64}$ ]] || die "$EX_DOWNLOAD" "$IMAGE_NAME is not listed in $SUMS_URL"
	if [[ -f $IMAGE_PATH ]]; then
		if [[ $(sha256_of "$IMAGE_PATH") == "$want" ]]; then
			CHECKSUM_STATUS="ok"
			info "using cached $IMAGE_PATH (checksum OK)"
			return 0
		fi
		warn "cached $IMAGE_PATH does not match the expected checksum; downloading it again"
		rm -f -- "$IMAGE_PATH"
	fi
	have=$(free_kib_at "$CACHE_SUB")
	((have >= need)) || die "$EX_PREFLIGHT" "need 8 GiB free under $CACHE_SUB for the image (have $((have / 1024 / 1024)) GiB)"
	info "downloading $IMAGE_URL"
	curl_get "$IMAGE_URL" "$IMAGE_PATH" progress || die "$EX_DOWNLOAD" "cannot download $IMAGE_URL"
	if [[ $(sha256_of "$IMAGE_PATH") != "$want" ]]; then
		rm -f -- "$IMAGE_PATH"
		die "$EX_DOWNLOAD" "SHA-256 mismatch for $IMAGE_NAME (download removed)"
	fi
	CHECKSUM_STATUS="ok"
	info "image checksum OK"
}

image_virtual_size() {
	command -v qemu-img >/dev/null 2>&1 || return 1
	qemu-img info --output=json "$IMAGE_PATH" 2>/dev/null | json_get virtual-size
}

# ------------------------------------------------------------------ seed ISO

seed_volid() { printf '%s:iso/agos-seed-%s.iso' "$ISO_STORAGE" "$VMID"; }

write_meta_data() {
	# An explicit, never-changing instance-id: cloud-init runs per-instance
	# modules (SSH host keys, users) exactly once.
	printf 'instance-id: agos-%s-%s\n' "$VMID" "$(date -u +%Y%m%dT%H%M%SZ)"
	# Quoted: names like "yes", "on" or "100" would otherwise be YAML bools/ints.
	printf "local-hostname: '%s'\n" "$NAME"
}

write_user_data() {
	local k
	printf '%s\n' "#cloud-config" \
		"# Generated by agos-proxmox.sh $AGOS_SCRIPT_VERSION for VM $VMID ($NAME)." \
		"# Contains secrets: the Proxmox host deletes this seed once first boot is ready." \
		"hostname: '$NAME'" \
		"ssh_pwauth: false" \
		"disable_root: true" \
		"users:" \
		"  - name: agent" \
		"    lock_passwd: true"
	if ((${#SSH_KEYS[@]})); then
		printf '    ssh_authorized_keys:\n'
		for k in "${SSH_KEYS[@]}"; do
			printf "      - '%s'\n" "${k//\'/\'\'}"
		done
	fi
	if [[ -z $CONFIG_FILE && -z $SECRETS_FILE ]]; then
		return 0
	fi
	printf 'write_files:\n'
	if [[ -n $CONFIG_FILE ]]; then
		printf '%s\n' "  - path: /etc/agos/config.toml" "    owner: root:root" \
			"    permissions: \"0644\"" "    encoding: b64"
		printf '    content: '
		base64 -w0 -- "$CONFIG_FILE"
		printf '\n'
	fi
	if [[ -n $SECRETS_FILE ]]; then
		# Streamed straight from the file: secrets never pass through a shell
		# variable or a command line, and b64 rules out YAML injection.
		printf '%s\n' "  - path: /etc/agos/secrets.env" "    owner: root:root" \
			"    permissions: \"0600\"" "    encoding: b64"
		printf '    content: '
		base64 -w0 -- "$SECRETS_FILE"
		printf '\n'
	fi
}

build_seed() {
	local base=$TMP_BASE d
	[[ -d $base && -w $base ]] || base=${TMPDIR:-/tmp}
	SEED_TMPDIR=$(mktemp -d -- "$base/agos-seed.XXXXXX")
	d="$SEED_TMPDIR/cidata"
	mkdir -m 0700 -- "$d"
	write_meta_data >"$d/meta-data"
	write_user_data >"$d/user-data"
	if [[ -n $NETCFG_FILE ]]; then cp -- "$NETCFG_FILE" "$d/network-config"; fi
	case $ISO_TOOL in
	genisoimage | mkisofs) "$ISO_TOOL" -quiet -o "$SEED_TMPDIR/seed.iso" -V CIDATA -J -R "$d" ;;
	xorriso) xorriso -as mkisofs -quiet -o "$SEED_TMPDIR/seed.iso" -V CIDATA -J -R "$d" 2>/dev/null ;;
	esac
	[[ -s $SEED_TMPDIR/seed.iso ]] || die "$EX_VMOP" "building the seed ISO with $ISO_TOOL failed"
}

upload_seed() {
	local volid path
	volid=$(seed_volid)
	path=$(pvesm path "$volid" 2>/dev/null) || die "$EX_VMOP" "cannot resolve a path for $volid (is '$ISO_STORAGE' a file storage?)"
	mkdir -p -- "$(dirname -- "$path")"
	SEED_ISO_PATH=$path
	install -m 0600 -- "$SEED_TMPDIR/seed.iso" "$path"
	rm -rf -- "$SEED_TMPDIR"
	SEED_TMPDIR=""
}

# Eject (PVE swaps CD media on a running VM live) and delete the seed.
seed_remove() {
	if ((SEED_ATTACHED)) && [[ -n $VMID ]]; then
		if ! qm set "$VMID" "--$SEED_SLOT" none,media=cdrom >/dev/null 2>&1; then
			warn "could not eject the seed ISO from VM $VMID"
		fi
		SEED_ATTACHED=0
	fi
	if [[ -n $SEED_ISO_PATH && -e $SEED_ISO_PATH ]]; then
		rm -f -- "$SEED_ISO_PATH" || err "could not delete $SEED_ISO_PATH; it contains secrets, delete it by hand"
	fi
	SEED_ISO_PATH=""
}

# ------------------------------------------------------------------ isolation (experimental)

is_private_v4() {
	[[ $1 =~ ^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|169\.254\.|127\.|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.) ]]
}

is_private_v6() {
	[[ ${1,,} =~ ^(f[cd]|fe[89ab]|::1$) ]]
}

detect_isolate_allow() {
	local gw4 gw6 ip a
	ISO_DNS4=() ISO_DNS6=() HOST_PUBLIC4=() HOST_PUBLIC6=()
	gw4=$(ip -4 route show default dev "$BRIDGE" 2>/dev/null | awk '{ for (i = 1; i < NF; i++) if ($i == "via") { print $(i + 1); exit } }') || true
	if [[ -z $gw4 ]]; then
		# Routed/NAT bridge: the host is the VM's gateway.
		gw4=$(ip -4 -o addr show dev "$BRIDGE" 2>/dev/null | awk '{ split($4, a, "/"); print a[1]; exit }') || true
	fi
	gw6=$(ip -6 route show default dev "$BRIDGE" 2>/dev/null | awk '{ for (i = 1; i < NF; i++) if ($i == "via") { print $(i + 1); exit } }') || true
	if [[ -n $gw4 ]]; then ISO_DNS4+=("$gw4"); fi
	if [[ -n $gw6 ]]; then ISO_DNS6+=("$gw6"); fi
	if [[ -r /etc/resolv.conf ]]; then
		while read -r a ip _; do
			[[ $a == nameserver && -n $ip ]] || continue
			# A resolver on the host's loopback (systemd-resolved) is not the VM's.
			[[ $ip == 127.* || $ip == ::1 ]] && continue
			if [[ $ip == *:* ]]; then
				if is_private_v6 "$ip"; then ISO_DNS6+=("$ip"); fi
			elif is_private_v4 "$ip"; then
				ISO_DNS4+=("$ip")
			fi
		done </etc/resolv.conf
	fi
	for ip in ${ISOLATE_DNS//,/ }; do
		if [[ $ip == *:* ]]; then ISO_DNS6+=("$ip"); else ISO_DNS4+=("$ip"); fi
	done
	while read -r ip; do
		[[ -n $ip ]] || continue
		if [[ $ip == *:* ]]; then
			if ! is_private_v6 "$ip"; then HOST_PUBLIC6+=("$ip"); fi
		elif ! is_private_v4 "$ip"; then
			HOST_PUBLIC4+=("$ip")
		fi
	done < <(ip -o addr show scope global 2>/dev/null | awk '{ split($4, a, "/"); print a[1] }')
	mapfile -t ISO_DNS4 < <(printf '%s\n' "${ISO_DNS4[@]}" | awk 'NF && !seen[$0]++')
	mapfile -t ISO_DNS6 < <(printf '%s\n' "${ISO_DNS6[@]}" | awk 'NF && !seen[$0]++')
}

firewall_rules() {
	local ip
	cat <<EOF
# agos --isolate egress rules for VM $VMID ($NAME), written by agos-proxmox.sh $AGOS_SCRIPT_VERSION.
# EXPERIMENTAL. Enforced only while the datacenter firewall is enabled
# (Datacenter > Firewall > Options > Firewall: Yes). 'qm destroy' removes this file.
# Rules apply in order; anything not dropped here may leave (policy_out ACCEPT).
[OPTIONS]
enable: 1
policy_in: ACCEPT
policy_out: ACCEPT
dhcp: 1
ndp: 1

[RULES]
EOF
	for ip in "${ISO_DNS4[@]}" "${ISO_DNS6[@]}"; do
		printf 'OUT ACCEPT -i net0 -dest %s -p udp -dport 53 # DNS via gateway/resolver\n' "$ip"
		printf 'OUT ACCEPT -i net0 -dest %s -p tcp -dport 53 # DNS via gateway/resolver\n' "$ip"
	done
	cat <<'EOF'
OUT DROP -i net0 -dest 10.0.0.0/8,172.16.0.0/12,192.168.0.0/16 -log nolog # RFC 1918 LAN
OUT DROP -i net0 -dest 169.254.0.0/16 -log nolog # link-local and cloud metadata
OUT DROP -i net0 -dest 100.64.0.0/10 -log nolog # CGNAT (incl. Tailscale addresses on the LAN)
OUT DROP -i net0 -dest 127.0.0.0/8 -log nolog # loopback
OUT DROP -i net0 -dest fc00::/7 -log nolog # IPv6 ULA
OUT DROP -i net0 -dest ::1/128 -log nolog # IPv6 loopback
OUT DROP -i net0 -dest fe80::/10 -p tcp -log nolog # IPv6 link-local (ICMPv6/NDP stays allowed)
OUT DROP -i net0 -dest fe80::/10 -p udp -log nolog # IPv6 link-local
EOF
	local IFS=,
	if ((${#HOST_PUBLIC4[@]})); then
		printf 'OUT DROP -i net0 -dest %s -log nolog # this Proxmox host\n' "${HOST_PUBLIC4[*]}"
	fi
	if ((${#HOST_PUBLIC6[@]})); then
		printf 'OUT DROP -i net0 -dest %s -log nolog # this Proxmox host\n' "${HOST_PUBLIC6[*]}"
	fi
}

write_firewall() {
	local f="/etc/pve/firewall/$VMID.fw"
	PLAN+=("write $f (VM firewall rules shown above)")
	if ((DRY_RUN)); then
		return 0
	fi
	[[ ! -e $f ]] || die "$EX_VMOP" "$f already exists; refusing to overwrite it"
	mkdir -p /etc/pve/firewall
	firewall_rules >"$f"
	log "  + wrote $f"
}

isolate_warnings() {
	warn "--isolate is EXPERIMENTAL: it writes firewall rules for this VM only (never cluster.fw or host rules)."
	if ((DC_FIREWALL == 0)); then
		warn "${C_BLD}The datacenter firewall is DISABLED, so these rules are NOT ENFORCED until an admin enables it.${C_OFF}"
		warn "Enabling it (Datacenter > Firewall > Options) also turns on the host firewall with input policy DROP:"
		warn "  only the cluster's local network keeps the GUI (8006) and SSH (22). If you manage the host from"
		warn "  elsewhere (VPN, another subnet, a public IP) you can lock yourself out. Add your admin IPs to the"
		warn "  'management' IPSet or set the input policy to ACCEPT first, keep a shell open, then enable it."
	fi
}

# ------------------------------------------------------------------ VM state

vm_status() {
	local res
	if ! res=$(qm status "$1" 2>/dev/null); then
		printf 'absent'
		return 0
	fi
	awk '/^status:/ { print $2; exit }' <<<"$res"
}

vm_has_golden() {
	qm listsnapshot "$1" 2>/dev/null | grep -qE '(^|[[:space:]])golden([[:space:]]|$)'
}

guest_exec_out() {
	# vmid cmd... -> the command's stdout, if it exited 0
	local vmid=$1 res
	shift
	res=$(timeout 40 qm guest exec "$vmid" --timeout 15 -- "$@" 2>/dev/null) || true
	[[ -n $res ]] || return 1
	guest_out_data <<<"$res"
}

read_guest_state() {
	local js
	GUEST_STATE="" GUEST_MSG=""
	js=$(guest_exec_out "$1" cat /var/lib/agos/state.json) || return 0
	GUEST_STATE=$(json_get state <<<"$js" 2>/dev/null) || GUEST_STATE="unknown"
	GUEST_MSG=$(json_get message <<<"$js" 2>/dev/null) || GUEST_MSG=$(json_get error <<<"$js" 2>/dev/null) || GUEST_MSG=""
}

clear_info() {
	I_STATUS=${1:-unknown} I_STATE=${1:-unknown} I_IP="" I_TSDNS="" I_VIEWER_URL="" I_AGENTD_URL="" I_ACCESS="" I_GOLDEN=0
}

# Fills I_* for a VM on this node. Never fails.
gather_info() {
	local vmid=$1 cfg ts scheme=https listen=127.0.0.1 port=8444 tls=auto serve=true
	clear_info
	I_STATUS=$(vm_status "$vmid")
	if vm_has_golden "$vmid"; then I_GOLDEN=1; fi
	if [[ $I_STATUS != running ]]; then
		I_STATE=$I_STATUS
		return 0
	fi
	if ! timeout 20 qm agent "$vmid" ping >/dev/null 2>&1; then
		I_STATE="booting"
		return 0
	fi
	read_guest_state "$vmid"
	I_STATE=${GUEST_STATE:-booting}
	I_IP=$(timeout 30 qm guest cmd "$vmid" network-get-interfaces 2>/dev/null | guest_pick_ip 2>/dev/null) || I_IP=""
	if cfg=$(guest_exec_out "$vmid" cat /run/agos/config.json); then
		listen=$(json_get viewer listen <<<"$cfg" 2>/dev/null) || listen=127.0.0.1
		port=$(json_get viewer port <<<"$cfg" 2>/dev/null) || port=8444
		tls=$(json_get viewer tls <<<"$cfg" 2>/dev/null) || tls=auto
		serve=$(json_get tailscale serve <<<"$cfg" 2>/dev/null) || serve=true
	elif ((LAN_MODE)); then
		listen=0.0.0.0
	fi
	if ts=$(guest_exec_out "$vmid" tailscale status --json) &&
		[[ $(json_get BackendState <<<"$ts" 2>/dev/null) == Running ]]; then
		I_TSDNS=$(json_get Self DNSName <<<"$ts" 2>/dev/null) || I_TSDNS=""
		I_TSDNS=${I_TSDNS%.}
	fi
	I_PORT=$port
	if [[ -n $I_TSDNS && $serve != false ]]; then
		I_ACCESS="tailscale"
		I_VIEWER_URL="https://$I_TSDNS/"
		I_AGENTD_URL="https://$I_TSDNS:8765/"
	elif [[ $listen == 0.0.0.0 && -n $I_IP ]]; then
		I_ACCESS="lan"
		if [[ $tls == off || $tls == false ]]; then scheme=http; fi
		I_VIEWER_URL="$scheme://$I_IP:$port/"
		I_AGENTD_URL="http://127.0.0.1:8765/"
	else
		I_ACCESS="ssh-tunnel"
		I_VIEWER_URL="http://127.0.0.1:$port/"
		I_AGENTD_URL="http://127.0.0.1:8765/"
	fi
}

print_access() {
	local vmid=$1 name=$2 ip=${I_IP:-<vm-ip>}
	out ""
	out "agos VM $vmid ($name): $I_STATE"
	if [[ -n $I_IP ]]; then out "  VM address:     $I_IP"; fi
	case $I_ACCESS in
	tailscale)
		out "  Desktop:        $I_VIEWER_URL   (over your tailnet)"
		out "  agentd API/MCP: $I_AGENTD_URL   (Authorization: Bearer <AGENTD_TOKEN>)"
		;;
	lan)
		out "  Desktop:        $I_VIEWER_URL   (LAN mode, self-signed certificate, user 'agos')"
		out "  agentd API/MCP: ssh -N -L 8765:127.0.0.1:8765 agent@$ip   then $I_AGENTD_URL"
		;;
	ssh-tunnel)
		out "  Desktop:        ssh -N -L $I_PORT:127.0.0.1:$I_PORT -L 8765:127.0.0.1:8765 agent@$ip"
		out "                  then open $I_VIEWER_URL (agentd: $I_AGENTD_URL)"
		out "                  (needs --ssh-key-file; or set TS_AUTHKEY for a tailnet URL)"
		;;
	esac
	if [[ $I_STATE == ready || $I_STATE == running ]]; then
		out "  Credentials (generated in the VM; this script never prints them):"
		out "    qm guest exec $vmid -- grep -E '^(VIEWER_PASSWORD|AGENTD_TOKEN)=' /etc/agos/secrets.env"
		out "    or: ssh agent@$ip sudo grep -E '^(VIEWER_PASSWORD|AGENTD_TOKEN)=' /etc/agos/secrets.env"
	fi
	if ((I_GOLDEN)); then
		out "  Reset to the first-boot state: agos-proxmox.sh reset --vmid $vmid --yes"
	else
		out "  No 'golden' snapshot: reset is not available for this VM."
	fi
}

emit_vm_json() {
	# action vmid name node existing
	jf_reset
	jfb ok 1
	jfs action "$1"
	jfn vmid "$2"
	jfs name "$3"
	jfs node "$4"
	jfs status "$I_STATUS"
	jfs state "$I_STATE"
	jfs ip "$I_IP"
	jf urls "{\"viewer\":$(json_str_or_null "$I_VIEWER_URL"),\"agentd\":$(json_str_or_null "$I_AGENTD_URL")}"
	jfs access "$I_ACCESS"
	jfs tailnet_name "$I_TSDNS"
	jfb golden_snapshot "$I_GOLDEN"
	jfb existing "${5:-0}"
	jfs credentials_cmd "qm guest exec $2 -- grep -E '^(VIEWER_PASSWORD|AGENTD_TOKEN)=' /etc/agos/secrets.env"
	if [[ $1 == create && ${5:-0} == 0 ]]; then
		jfs version "$VERSION"
		jfs image_checksum "$CHECKSUM_STATUS"
		jfs image_signature "$SIG_STATUS"
		jfb isolate "$ISOLATE"
	fi
	jfb dry_run "$DRY_RUN"
	jf_emit
}

# ------------------------------------------------------------------ confirmation

confirm() {
	local reply=""
	if ((YES)); then
		return 0
	fi
	if [[ -t 0 && -t 2 ]]; then
		printf '%s [y/N] ' "$1" >&2
		read -r reply || true
		[[ $reply =~ ^[Yy]([Ee][Ss])?$ ]] || die "$EX_USAGE" "aborted; nothing was changed"
		return 0
	fi
	die "$EX_USAGE" "confirmation needed but there is no terminal: review the plan (--dry-run), then re-run with --yes"
}

# ------------------------------------------------------------------ create

plan_vm_commands() {
	local efi scsi0 net0="virtio,bridge=$BRIDGE"
	if ((ISOLATE)); then net0+=",firewall=1"; fi
	CMD_CREATE=(qm create "$VMID" --name "$NAME" --tags agos --ostype l26 --bios ovmf
		--cpu "$CPU_TYPE" --cores "$CORES" --memory "$MEMORY" --balloon 0
		--scsihw virtio-scsi-single --agent enabled=1 --net0 "$net0"
		--vga virtio --tablet 1 --serial0 socket --onboot 1
		--description "agos $VERSION ($HOST_ARCH), created by agos-proxmox.sh $AGOS_SCRIPT_VERSION on $(date -u +%Y-%m-%d). Manage with: agos-proxmox.sh status|reset|destroy --vmid $VMID")
	if [[ $HOST_ARCH == amd64 ]]; then
		CMD_CREATE+=(--machine q35)
		# pre-enrolled-keys=0 keeps Secure Boot off (v0.1 images are unsigned).
		efi="$STORAGE:1,efitype=4m,pre-enrolled-keys=0$DISK_FORMAT_OPT"
		SEED_SLOT=ide2
	else
		# arm64: PVE's default 'virt' machine and AAVMF vars (64 MiB, sized by
		# PVE from the firmware template; efitype is ignored). 'virt' has no
		# IDE bus, so the seed CD goes on SCSI.
		efi="$STORAGE:1,pre-enrolled-keys=0$DISK_FORMAT_OPT"
		SEED_SLOT=scsi1
	fi
	scsi0="$STORAGE:0,import-from=$IMAGE_PATH,discard=on,ssd=1,iothread=1$DISK_FORMAT_OPT"
	CMD_EFI=(qm set "$VMID" --efidisk0 "$efi")
	CMD_DISK=(qm set "$VMID" --scsi0 "$scsi0")
	CMD_RESIZE=(qm resize "$VMID" scsi0 "$DISK")
	CMD_BOOT=(qm set "$VMID" --boot order=scsi0)
	CMD_SEED=(qm set "$VMID" "--$SEED_SLOT" "$(seed_volid),media=cdrom")
	CMD_START=(qm start "$VMID")
	CMD_EJECT=(qm set "$VMID" "--$SEED_SLOT" "none,media=cdrom")
	CMD_SNAP=(qm snapshot "$VMID" golden --description "agos $VERSION after first boot")
}

record_plan() {
	run "${CMD_CREATE[@]}"
	run "${CMD_EFI[@]}"
	run "${CMD_DISK[@]}"
	run "${CMD_RESIZE[@]}"
	run "${CMD_BOOT[@]}"
	PLAN+=("build seed ISO (user-data, meta-data${NETCFG_FILE:+, network-config}; label CIDATA) -> $(seed_volid)")
	run "${CMD_SEED[@]}"
	if ((ISOLATE)); then write_firewall; fi
	run "${CMD_START[@]}"
	PLAN+=("wait: qm agent $VMID ping; qm guest exec $VMID -- cat /var/lib/agos/state.json until state is ready")
	run "${CMD_EJECT[@]}"
	PLAN+=("delete the seed ISO")
	run "${CMD_SNAP[@]}"
}

print_plan() {
	local c rules=()
	if ((JSON)); then
		jf_reset
		jfb ok 1
		jfs action create
		jfb dry_run 1
		jfs state planned
		jfn vmid "$VMID"
		jfs name "$NAME"
		jfs node "$NODE"
		jf ip null
		jf urls '{"viewer":null,"agentd":null}'
		jfs pve_version "$PVE_VERSION"
		jfs arch "$HOST_ARCH"
		jfs storage "$STORAGE"
		jfs storage_type "$STORAGE_TYPE"
		jfs iso_storage "$ISO_STORAGE"
		jfs bridge "$BRIDGE"
		jfn cores "$CORES"
		jfn memory_mib "$MEMORY"
		jfs disk "$DISK"
		jfs cpu "$CPU_TYPE"
		jfs version "$VERSION"
		jfs image_source "$IMAGE_SOURCE"
		jfs image "$IMAGE_PATH"
		jfs image_url "$IMAGE_URL"
		jfb image_cached "$IMAGE_CACHED"
		jfs secrets_file "$SECRETS_FILE"
		jf secret_keys "$(json_arr "${SECRET_KEYS[@]}")"
		jfb tailscale "$HAS_TS"
		jfs config_file "$CONFIG_FILE"
		jfs network_config "$NETCFG_FILE"
		jfn ssh_keys "${#SSH_KEYS[@]}"
		jfn timeout_s "$TIMEOUT"
		jfb isolate "$ISOLATE"
		if ((ISOLATE)); then
			jfb datacenter_firewall "$DC_FIREWALL"
			mapfile -t rules < <(firewall_rules | grep -E '^(OUT|IN) ')
			jf firewall_rules "$(json_arr "${rules[@]}")"
		fi
		jf commands "$(json_arr "${PLAN[@]}")"
		jf_emit
		return 0
	fi
	out ""
	out "agos-proxmox.sh $AGOS_SCRIPT_VERSION - plan (dry run: nothing will be changed)"
	out "  host       Proxmox VE $PVE_VERSION, $HOST_ARCH, node $NODE"
	out "  VM         $VMID '$NAME' (tag agos, starts at boot)"
	out "  hardware   $CORES cores (cpu $CPU_TYPE), $MEMORY MiB, $DISK disk on $STORAGE ($STORAGE_TYPE), bridge $BRIDGE"
	if [[ $IMAGE_SOURCE == file ]]; then
		out "  image      $IMAGE_PATH (local file)"
	else
		out "  image      $IMAGE_URL"
		out "             cached at $IMAGE_PATH$( ((IMAGE_CACHED)) && printf ' (already there)'), checked against SHA256SUMS"
	fi
	out "  seed       NoCloud ISO (label CIDATA) on '$ISO_STORAGE', deleted after first boot"
	if [[ -n $SECRETS_FILE ]]; then
		out "             secrets from $SECRETS_FILE: ${SECRET_KEYS[*]:-(no keys)} (values never shown)"
	else
		out "             no secrets file"
	fi
	out "             config: ${CONFIG_FILE:-image defaults}; network: ${NETCFG_FILE:-DHCP}; SSH keys for 'agent': ${#SSH_KEYS[@]}"
	if ((ISOLATE)); then
		out "  isolate    EXPERIMENTAL egress rules in /etc/pve/firewall/$VMID.fw (datacenter firewall: $( ((DC_FIREWALL)) && printf enabled || printf 'DISABLED, not enforced'))"
		while IFS= read -r c; do out "               $c"; done < <(firewall_rules | grep -E '^(OUT|IN) ')
	else
		out "  isolate    no: the VM can reach your LAN (see --isolate)"
	fi
	out "  then       start, wait up to ${TIMEOUT}s for first boot, remove the seed, snapshot 'golden'"
	out ""
	out "Commands:"
	for c in "${PLAN[@]}"; do out "  $c"; done
	out ""
}

print_plan_brief() {
	log ""
	log "${C_BLD}About to create VM $VMID '$NAME'${C_OFF}: $CORES cores, $MEMORY MiB, $DISK on $STORAGE, bridge $BRIDGE, $HOST_ARCH, agos $VERSION"
	log "  secrets: ${SECRETS_FILE:-none}${SECRETS_FILE:+ (${SECRET_KEYS[*]:-no keys})}; isolate: $( ((ISOLATE)) && printf yes || printf no)"
	log "  (run with --dry-run to see every command)"
	log ""
}

import_disk() {
	local vol fmt=()
	if run "${CMD_DISK[@]}"; then
		return 0
	fi
	warn "import-from failed; falling back to 'qm disk import'"
	if [[ -n $DISK_FORMAT_OPT ]]; then fmt=(--format qcow2); fi
	if ! run qm disk import "$VMID" "$IMAGE_PATH" "$STORAGE" "${fmt[@]}"; then
		run qm importdisk "$VMID" "$IMAGE_PATH" "$STORAGE" "${fmt[@]}" || die "$EX_VMOP" "disk import failed"
	fi
	vol=$(qm config "$VMID" | awk -F': ' '/^unused[0-9]+:/ { print $2; exit }')
	[[ -n $vol ]] || die "$EX_VMOP" "imported disk not found in the VM config"
	run qm set "$VMID" --scsi0 "$vol,discard=on,ssd=1,iothread=1" || die "$EX_VMOP" "attaching the imported disk failed"
}

resize_disk() {
	local want=$((${DISK%G} * 1024 * 1024 * 1024)) have
	if have=$(image_virtual_size) && [[ $have =~ ^[0-9]+$ ]] && ((have >= want)); then
		warn "the image is already $((have / 1024 / 1024 / 1024)) GiB; not resizing to $DISK"
		return 0
	fi
	run "${CMD_RESIZE[@]}" || die "$EX_VMOP" "resizing the disk failed"
}

wait_ready() {
	local deadline=$((SECONDS + TIMEOUT)) st agent=0 agent_txt
	phase "$EX_TIMEOUT"
	info "waiting up to ${TIMEOUT}s for first boot (guest agent, then /var/lib/agos/state.json)"
	GUEST_STATE=""
	while ((SECONDS < deadline)); do
		st=$(vm_status "$VMID")
		if [[ $st != running ]]; then
			KEEP_VM=1
			die "$EX_VMOP" "VM $VMID is '$st' during first boot; it is kept for inspection (console: qm terminal $VMID)"
		fi
		if timeout 20 qm agent "$VMID" ping >/dev/null 2>&1; then
			if ((agent == 0)); then info "guest agent is up"; fi
			agent=1
			read_guest_state "$VMID"
			case $GUEST_STATE in
			ready) return 0 ;;
			failed | error)
				KEEP_VM=1
				die "$EX_VMOP" "first boot reported '$GUEST_STATE'${GUEST_MSG:+: $GUEST_MSG}; VM $VMID is kept for inspection"
				;;
			esac
		fi
		sleep "$POLL_INTERVAL"
	done
	KEEP_VM=1
	agent_txt="not answering"
	if ((agent)); then agent_txt="up"; fi
	die "$EX_TIMEOUT" "timed out after ${TIMEOUT}s (guest agent: $agent_txt, state: ${GUEST_STATE:-none}). VM $VMID is kept and its seed ISO removed; inspect it (qm terminal $VMID), then 'destroy --vmid $VMID --yes' and run create again."
}

report_existing() {
	local vmid=$1 name=$2 node=$3 status=$4
	info "an agos VM named '$name' already exists (VMID $vmid on node $node); nothing to do"
	if [[ $node == "$NODE" ]]; then
		gather_info "$vmid"
	else
		clear_info "$status"
		info "it runs on node $node; run status there for details"
	fi
	if ((JSON)); then
		emit_vm_json create "$vmid" "$name" "$node" 1
	else
		if [[ $node == "$NODE" ]]; then print_access "$vmid" "$name"; fi
		log "To create another one, pass a different --name."
	fi
	exit 0
}

cmd_create() {
	local lockdir=/run/lock
	phase "$EX_PREFLIGHT"
	require_root
	check_tools
	check_pve_version
	check_arch
	check_kvm
	load_vms
	if find_vm "" "$NAME"; then
		((F_AGOS)) || die "$EX_PREFLIGHT" "a VM named '$NAME' (VMID $F_VMID) exists but is not tagged agos; choose another --name"
		report_existing "$F_VMID" "$F_NAME" "$F_NODE" "$F_STATUS"
	fi
	check_bridge
	check_secrets_file
	check_config_file
	check_ssh_keys
	check_netcfg
	check_iso_tool
	command -v base64 >/dev/null 2>&1 || die "$EX_PREFLIGHT" "base64 is missing"
	select_storages
	pick_vmid
	resolve_image
	if ((ISOLATE)); then
		check_firewall_state
		detect_isolate_allow
	fi
	if [[ $HAS_TS -eq 0 && $LAN_MODE -eq 0 && ${#SSH_KEYS[@]} -eq 0 ]]; then
		warn "no TS_AUTHKEY, no LAN-mode config and no SSH key: you will only reach the VM through 'qm guest exec'"
	fi
	if ((ISOLATE)); then isolate_warnings; fi
	plan_vm_commands

	if ((DRY_RUN)); then
		check_image_reachable
		record_plan
		print_plan
		if ((JSON == 0)); then
			info "dry run complete; nothing was changed. Re-run without --dry-run (add --yes when unattended) to create the VM."
		fi
		return 0
	fi

	if ((JSON == 0)); then print_plan_brief; fi
	confirm "Create VM $VMID '$NAME' on $STORAGE?"

	if [[ ! -d $lockdir || ! -w $lockdir ]]; then lockdir=/tmp; fi
	exec 9>"$lockdir/agos-proxmox.lock"
	if command -v flock >/dev/null 2>&1 && ! flock -n 9; then
		die "$EX_PREFLIGHT" "another agos-proxmox.sh run is in progress"
	fi

	fetch_image

	phase "$EX_VMOP"
	info "creating VM $VMID"
	run "${CMD_CREATE[@]}" || die "$EX_VMOP" "qm create failed"
	CREATED_VMID=$VMID
	run "${CMD_EFI[@]}" || die "$EX_VMOP" "adding the EFI disk failed"
	import_disk
	resize_disk
	run "${CMD_BOOT[@]}" || die "$EX_VMOP" "setting the boot order failed"
	info "building the NoCloud seed"
	build_seed
	upload_seed
	run "${CMD_SEED[@]}" || die "$EX_VMOP" "attaching the seed ISO failed"
	SEED_ATTACHED=1
	if ((ISOLATE)); then write_firewall; fi
	run "${CMD_START[@]}" || die "$EX_VMOP" "qm start failed"

	wait_ready
	KEEP_VM=1
	info "first boot is ready; removing the seed ISO"
	seed_remove
	phase "$EX_VMOP"
	run "${CMD_SNAP[@]}" ||
		die "$EX_VMOP" "VM $VMID is ready but the 'golden' snapshot failed (does storage '$STORAGE' support snapshots?); 'reset' will not work"
	CREATED_VMID=""
	gather_info "$VMID"
	if ((JSON)); then
		emit_vm_json create "$VMID" "$NAME" "$NODE" 0
	else
		print_access "$VMID" "$NAME"
		out "  Image: agos $VERSION, checksum $CHECKSUM_STATUS, signature $SIG_STATUS"
	fi
}

# ------------------------------------------------------------------ status / reset / destroy

# Target VM for status/reset/destroy -> T_VMID T_NAME T_NODE T_STATUS.
# Returns 1 when no selector was given and there is no agos VM at all.
select_target() {
	local mode=$1
	if [[ -n $VMID_OPT ]]; then
		find_vm "$VMID_OPT" "" || die "$EX_VMOP" "no VM with VMID $VMID_OPT"
	elif [[ -n $NAME_OPT ]]; then
		find_vm "" "$NAME_OPT" || die "$EX_VMOP" "no VM named '$NAME_OPT'"
	else
		if [[ $mode == destroy ]]; then die "$EX_USAGE" "destroy needs --vmid or --name"; fi
		((${#AGOS_IDS[@]} > 0)) || return 1
		((${#AGOS_IDS[@]} == 1)) || die "$EX_USAGE" "there are ${#AGOS_IDS[@]} agos VMs (${AGOS_IDS[*]}); pass --vmid or --name"
		find_vm "${AGOS_IDS[0]}" ""
	fi
	((F_AGOS)) || die "$EX_VMOP" "VM $F_VMID ('$F_NAME') is not tagged agos; refusing to touch it"
	T_VMID=$F_VMID T_NAME=$F_NAME T_NODE=$F_NODE T_STATUS=$F_STATUS
	VMID=$T_VMID NAME=$T_NAME
}

cmd_status() {
	local id items=() row
	phase "$EX_PREFLIGHT"
	require_root
	check_tools
	load_vms
	if [[ -z $VMID_OPT && -z $NAME_OPT && ${#AGOS_IDS[@]} -gt 1 ]]; then
		if ((JSON == 0)); then
			out "$(printf '%-8s %-20s %-10s %-9s %-10s %s' VMID NAME NODE STATUS STATE IP)"
		fi
		for id in "${AGOS_IDS[@]}"; do
			find_vm "$id" ""
			if [[ $F_NODE == "$NODE" ]]; then gather_info "$id"; else clear_info "$F_STATUS"; fi
			if ((JSON)); then
				row=$(emit_vm_json status "$id" "$F_NAME" "$F_NODE" 0)
				items+=("$row")
			else
				out "$(printf '%-8s %-20s %-10s %-9s %-10s %s' "$id" "$F_NAME" "$F_NODE" "$I_STATUS" "$I_STATE" "${I_IP:--}")"
			fi
		done
		if ((JSON)); then
			local IFS=,
			printf '{"ok":true,"action":"status","state":"multiple","vms":[%s]}\n' "${items[*]}"
			JSON_DONE=1
		fi
		return 0
	fi
	if ! select_target status; then
		if ((JSON)); then
			jf_reset
			jfb ok 1
			jfs action status
			jf vmid null
			jf name null
			jfs state absent
			jf ip null
			jf urls '{"viewer":null,"agentd":null}'
			jf_emit
		else
			out "No agos VMs in this cluster."
		fi
		return 0
	fi
	if [[ $T_NODE == "$NODE" ]]; then
		gather_info "$T_VMID"
	else
		clear_info "$T_STATUS"
		info "VM $T_VMID runs on node $T_NODE; run status there for details"
	fi
	if ((JSON)); then
		emit_vm_json status "$T_VMID" "$T_NAME" "$T_NODE" 0
	elif [[ $T_NODE == "$NODE" ]]; then
		print_access "$T_VMID" "$T_NAME"
	else
		out "agos VM $T_VMID ($T_NAME) on node $T_NODE: $I_STATUS"
	fi
}

emit_plan_only() {
	# action text
	local c
	if ((JSON)); then
		jf_reset
		jfb ok 1
		jfs action "$1"
		jfb dry_run 1
		jfn vmid "$T_VMID"
		jfs name "$T_NAME"
		jfs state planned
		jf ip null
		jf urls '{"viewer":null,"agentd":null}'
		jf commands "$(json_arr "${PLAN[@]}")"
		jf_emit
	else
		out "Plan (dry run: nothing will be changed): $2"
		for c in "${PLAN[@]}"; do out "  $c"; done
	fi
}

cmd_reset() {
	local st deadline
	phase "$EX_PREFLIGHT"
	require_root
	check_tools
	load_vms
	select_target reset || die "$EX_VMOP" "no agos VM found"
	[[ $T_NODE == "$NODE" ]] || die "$EX_VMOP" "VM $T_VMID is on node $T_NODE; run reset there"
	vm_has_golden "$T_VMID" || die "$EX_VMOP" "VM $T_VMID has no 'golden' snapshot to roll back to"
	st=$(vm_status "$T_VMID")
	if ((DRY_RUN)); then
		if [[ $st == running ]]; then run qm stop "$T_VMID"; fi
		run qm rollback "$T_VMID" golden
		run qm start "$T_VMID"
		emit_plan_only reset "roll VM $T_VMID ('$T_NAME') back to 'golden'; everything since first boot is lost"
		return 0
	fi
	confirm "Roll VM $T_VMID '$T_NAME' back to 'golden'? Everything since first boot is lost."
	phase "$EX_VMOP"
	info "resetting VM $T_VMID to 'golden'"
	if [[ $st == running ]]; then
		run qm stop "$T_VMID" || die "$EX_VMOP" "qm stop failed"
	fi
	run qm rollback "$T_VMID" golden || die "$EX_VMOP" "qm rollback failed"
	if [[ $(vm_status "$T_VMID") != running ]]; then
		run qm start "$T_VMID" || die "$EX_VMOP" "qm start failed"
	fi
	phase "$EX_TIMEOUT"
	deadline=$((SECONDS + TIMEOUT))
	until timeout 20 qm agent "$T_VMID" ping >/dev/null 2>&1; do
		((SECONDS < deadline)) || die "$EX_TIMEOUT" "VM $T_VMID did not answer on the guest agent within ${TIMEOUT}s after the reset"
		sleep "$POLL_INTERVAL"
	done
	gather_info "$T_VMID"
	if ((JSON)); then
		emit_vm_json reset "$T_VMID" "$T_NAME" "$T_NODE" 0
	else
		print_access "$T_VMID" "$T_NAME"
	fi
}

cmd_destroy() {
	local st i path seeds=()
	phase "$EX_PREFLIGHT"
	require_root
	check_tools
	load_vms
	select_target destroy
	[[ $T_NODE == "$NODE" ]] || die "$EX_VMOP" "VM $T_VMID is on node $T_NODE; run destroy there"
	if ((YES == 0 && DRY_RUN == 0)); then
		die "$EX_USAGE" "destroy deletes VM $T_VMID ('$T_NAME') with its disks and snapshots; re-run with --yes to confirm"
	fi
	load_storages iso
	for i in "${!ST_NAME[@]}"; do
		path=$(pvesm path "${ST_NAME[i]}:iso/agos-seed-$T_VMID.iso" 2>/dev/null) || continue
		if [[ -e $path ]]; then seeds+=("$path"); fi
	done
	st=$(vm_status "$T_VMID")
	if ((DRY_RUN)); then
		if [[ $st == running ]]; then run qm stop "$T_VMID"; fi
		run qm destroy "$T_VMID" --purge 1
		for path in "${seeds[@]}"; do PLAN+=("rm -f $path"); done
		emit_plan_only destroy "delete VM $T_VMID ('$T_NAME') with all its disks and snapshots"
		return 0
	fi
	phase "$EX_VMOP"
	info "destroying VM $T_VMID ('$T_NAME')"
	if [[ $st == running ]]; then
		run qm stop "$T_VMID" || die "$EX_VMOP" "qm stop failed"
	fi
	run qm destroy "$T_VMID" --purge 1 || die "$EX_VMOP" "qm destroy failed"
	for path in "${seeds[@]}"; do rm -f -- "$path"; done
	if ((JSON)); then
		jf_reset
		jfb ok 1
		jfs action destroy
		jfn vmid "$T_VMID"
		jfs name "$T_NAME"
		jfs state destroyed
		jf ip null
		jf urls '{"viewer":null,"agentd":null}'
		jf_emit
	else
		out "VM $T_VMID ('$T_NAME') destroyed."
	fi
}

# ------------------------------------------------------------------ main

main() {
	local a
	umask 077
	setup_colors
	init_settings
	for a in "$@"; do
		if [[ $a == --json ]]; then JSON=1; fi
	done
	trap on_exit EXIT
	trap 'on_err "$LINENO" "$BASH_COMMAND"' ERR
	trap on_signal INT TERM
	parse_args "$@"
	if [[ $ACTION == help ]]; then
		usage
		exit 0
	fi
	validate_settings
	case $ACTION in
	create) cmd_create ;;
	status) cmd_status ;;
	reset) cmd_reset ;;
	destroy) cmd_destroy ;;
	esac
}

main "$@"
