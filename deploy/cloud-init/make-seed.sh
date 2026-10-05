#!/usr/bin/env bash
# make-seed.sh - build a cloud-init NoCloud seed ISO (volume label CIDATA)
# for agos from user-data, meta-data and an optional network-config.
#
# Works on any Linux box with genisoimage, xorriso, mkisofs or cloud-localds
# (and on macOS with hdiutil). Attach the ISO to the VM as a CD-ROM for its
# first boot, then detach and delete it: it contains your secrets.
set -euo pipefail

usage() {
	cat <<'EOF'
Usage: make-seed.sh [-o OUTPUT] [-d DIR] [--user-data FILE] [--meta-data FILE]
                    [--network-config FILE | --no-network-config] [--allow-placeholder]

Builds OUTPUT (default: ./seed.iso, mode 0600) with the volume label CIDATA.
Files default to DIR/user-data, DIR/meta-data and DIR/network-config (if it
exists), where DIR is this script's directory unless -d is given.

Set SEED_TOOL=genisoimage|xorriso|mkisofs|cloud-localds|hdiutil to force a tool.
EOF
}

die() {
	printf 'make-seed: %s\n' "$*" >&2
	exit 1
}
warn() { printf 'make-seed: warning: %s\n' "$*" >&2; }

main() {
	local here dir output="seed.iso" ud="" md="" nc="" no_nc=0 allow_placeholder=0 tool
	here=$(cd -- "$(dirname -- "$0")" && pwd)
	dir=$here
	while (($#)); do
		case $1 in
		-o | --output) output=${2:?-o needs a file}; shift ;;
		-d | --dir) dir=${2:?-d needs a directory}; shift ;;
		--user-data) ud=${2:?--user-data needs a file}; shift ;;
		--meta-data) md=${2:?--meta-data needs a file}; shift ;;
		--network-config) nc=${2:?--network-config needs a file}; shift ;;
		--no-network-config) no_nc=1 ;;
		--allow-placeholder) allow_placeholder=1 ;;
		-h | --help) usage; exit 0 ;;
		*) usage >&2; die "unknown argument: $1" ;;
		esac
		shift
	done
	ud=${ud:-$dir/user-data}
	md=${md:-$dir/meta-data}
	if ((no_nc)); then
		nc=""
	elif [[ -z $nc && -f $dir/network-config ]]; then
		nc=$dir/network-config
	fi

	[[ -f $ud ]] || die "user-data not found: $ud"
	[[ -f $md ]] || die "meta-data not found: $md"
	[[ -z $nc || -f $nc ]] || die "network-config not found: $nc"
	[[ $(head -n1 -- "$ud") == "#cloud-config"* ]] || warn "$ud does not start with '#cloud-config'"
	grep -Eq '^instance-id:[[:space:]]*[^[:space:]]' -- "$md" || die "$md has no instance-id"
	if grep -q 'agos-example-0001' -- "$md"; then
		warn "$md still has the example instance-id; give every VM its own"
	fi
	if grep -q 'REPLACE_WITH_YOUR' -- "$ud" && ((allow_placeholder == 0)); then
		die "$ud still contains the placeholder SSH key; put your public key in (or pass --allow-placeholder)"
	fi
	if command -v python3 >/dev/null 2>&1 && python3 -c 'import yaml' 2>/dev/null; then
		local f
		for f in "$ud" "$md" ${nc:+"$nc"}; do
			python3 -c 'import sys, yaml; yaml.safe_load(open(sys.argv[1]))' "$f" 2>/dev/null ||
				die "$f is not valid YAML"
		done
	fi

	tool=${SEED_TOOL:-}
	if [[ -z $tool ]]; then
		for tool in genisoimage xorriso mkisofs cloud-localds hdiutil ""; do
			if [[ -n $tool ]] && command -v "$tool" >/dev/null 2>&1; then break; fi
		done
	fi
	[[ -n $tool ]] || die "need one of: genisoimage, xorriso, mkisofs, cloud-localds (Linux) or hdiutil (macOS)"
	command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed"

	umask 077
	# Global, not local: the EXIT trap runs after main has returned.
	STAGE=$(mktemp -d)
	trap 'rm -rf -- "$STAGE"' EXIT
	local stage=$STAGE
	mkdir "$stage/cidata"
	cp -- "$ud" "$stage/cidata/user-data"
	cp -- "$md" "$stage/cidata/meta-data"
	if [[ -n $nc ]]; then cp -- "$nc" "$stage/cidata/network-config"; fi
	rm -f -- "$output"

	case $tool in
	genisoimage | mkisofs) "$tool" -quiet -o "$output" -V CIDATA -J -R "$stage/cidata" ;;
	xorriso) xorriso -as mkisofs -quiet -o "$output" -V CIDATA -J -R "$stage/cidata" 2>/dev/null ;;
	cloud-localds)
		# cloud-localds labels the volume "cidata"; cloud-init accepts either case.
		cloud-localds ${nc:+-N "$stage/cidata/network-config"} "$output" \
			"$stage/cidata/user-data" "$stage/cidata/meta-data"
		;;
	hdiutil) hdiutil makehybrid -quiet -o "$output" -iso -joliet -default-volume-name CIDATA "$stage/cidata" ;;
	*) die "unsupported SEED_TOOL: $tool" ;;
	esac
	[[ -s $output ]] || die "$tool did not produce $output"
	chmod 600 -- "$output"
	printf 'wrote %s (label CIDATA, %s): user-data meta-data%s\n' "$output" "$tool" "${nc:+ network-config}"
}

main "$@"
