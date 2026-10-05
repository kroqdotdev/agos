# shellcheck shell=bash
# Shared helpers for the fake Proxmox VE commands. Each stub appends its argv
# to $FAKE_LOG and keeps state under $FAKE_STATE, so tests can assert on
# exactly what agos-proxmox.sh ran.
#
# Knobs (all optional):
#   FAKE_FAIL          ERE matched against "<cmd> <args>"; a match exits 255
#   FAKE_NODE          local node name (default: uname -n)
#   FAKE_STORAGES      lines "name type content,content status avail_kib"
#   FAKE_ROOT          root for storage paths (default $FAKE_STATE/root)

: "${FAKE_STATE:?FAKE_STATE must be set}"
: "${FAKE_LOG:?FAKE_LOG must be set}"
FAKE_ROOT=${FAKE_ROOT:-$FAKE_STATE/root}
FAKE_NODE=${FAKE_NODE:-$(uname -n)}
FAKE_NODE=${FAKE_NODE%%.*}
QDIR="$FAKE_STATE/qemu"
mkdir -p "$QDIR"

record() {
	local IFS=' '
	printf '%s\n' "$*" >>"$FAKE_LOG"
}

maybe_fail() {
	local IFS=' '
	if [[ -n ${FAKE_FAIL:-} && "$*" =~ $FAKE_FAIL ]]; then
		printf 'fake %s: forced failure\n' "$1" >&2
		exit 255
	fi
}

default_storages() {
	if [[ -n ${FAKE_STORAGES:-} ]]; then
		printf '%s\n' "$FAKE_STORAGES"
	else
		printf '%s\n' \
			"local dir iso,vztmpl,backup,import active 81089764" \
			"local-lvm lvmthin images,rootdir active 355760512"
	fi
}

# storage_path STORE:iso/NAME -> filesystem path (file storages only)
storage_path() {
	local volid=$1 store rest name type
	store=${volid%%:*}
	rest=${volid#*:}
	while read -r name type _; do
		[[ $name == "$store" ]] || continue
		case $type in
		dir | nfs | cifs | glusterfs | cephfs | btrfs) ;;
		*)
			echo "storage '$store' has no path" >&2
			return 1
			;;
		esac
		case $rest in
		iso/*) printf '%s/%s/template/iso/%s\n' "$FAKE_ROOT" "$store" "${rest#iso/}" ;;
		*) printf '%s/%s/%s\n' "$FAKE_ROOT" "$store" "$rest" ;;
		esac
		return 0
	done < <(default_storages)
	echo "storage '$store' does not exist" >&2
	return 1
}

conf() { printf '%s/%s.conf' "$QDIR" "$1"; }
vm_node() {
	if [[ -f $QDIR/$1.node ]]; then cat "$QDIR/$1.node"; else printf '%s' "$FAKE_NODE"; fi
}

need_local_vm() {
	if [[ ! -f $(conf "$1") || $(vm_node "$1") != "$FAKE_NODE" ]]; then
		echo "Configuration file 'nodes/$FAKE_NODE/qemu-server/$1.conf' does not exist" >&2
		exit 2
	fi
}

conf_get() {
	# vmid key
	local f
	f=$(conf "$1")
	sed -n "s/^$2: //p" "$f" | head -n1
}

conf_set() {
	# vmid key value
	local f tmp
	f=$(conf "$1")
	tmp="$f.tmp"
	grep -v "^$2: " "$f" >"$tmp" || true
	printf '%s: %s\n' "$2" "$3" >>"$tmp"
	mv "$tmp" "$f"
}

conf_del() {
	local f
	f=$(conf "$1")
	grep -v "^$2: " "$f" >"$f.tmp" || true
	mv "$f.tmp" "$f"
}

counter() {
	# vmid name -> increments and prints the new value
	local f="$QDIR/$1.$2" n=0
	if [[ -f $f ]]; then n=$(cat "$f"); fi
	n=$((n + 1))
	printf '%s' "$n" >"$f"
	printf '%s' "$n"
}
