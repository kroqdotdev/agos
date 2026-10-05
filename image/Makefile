# agos image: build, convert and boot-test inside the pinned builder container.
# Nothing here installs anything on the host; Docker is the only requirement
# (plus /dev/kvm for boot-test).
#
#   make -C image build ARCH=amd64      # raw disk image (mkosi)
#   make -C image artifacts ARCH=amd64  # .qcow2 + .raw.xz + SHA256SUMS
#   make -C image boot-test ARCH=amd64  # QEMU/OVMF zero-touch boot test
#   make -C image shell                 # interactive shell in the builder
#   make -C image clean                 # remove outputs (root-owned caches too)

ARCH ?= $(shell docker version --format '{{.Server.Arch}}' 2>/dev/null | sed 's/x86_64/amd64/;s/aarch64/arm64/')
VERSION := $(shell cat ../VERSION)
REPO := $(abspath ..)
BUILDER_TAG := $(shell sha256sum builder/Dockerfile | cut -c1-12)
BUILDER_IMAGE ?= agos-builder:$(BUILDER_TAG)

# Passed through to the build (see scripts/build.sh).
AGOS_WITH_AGENTD ?= auto
MKOSI_FORCE ?= -f
MKOSI_ARGS ?=

OUT := mkosi.output
RAW := $(OUT)/agos-$(VERSION)-$(ARCH).raw
QCOW2 := $(OUT)/agos-$(VERSION)-$(ARCH).qcow2
RAWXZ := $(OUT)/agos-$(VERSION)-$(ARCH).raw.xz

TTY := $(shell [ -t 0 ] && echo -t)
DOCKER_ENV := -e HOST_UID=$(shell id -u) -e HOST_GID=$(shell id -g) \
	-e AGOS_WITH_AGENTD=$(AGOS_WITH_AGENTD) -e MKOSI_FORCE=$(MKOSI_FORCE)
# mkosi needs mount namespaces and mount_setattr(); --privileged is the
# reliable way to get them inside Docker (no loop devices are used).
DOCKER_MKOSI := docker run --rm -i $(TTY) --privileged $(DOCKER_ENV) \
	-v agos-mkosi-workspace:/var/tmp -v $(REPO):/src -w /src/image $(BUILDER_IMAGE)
# The boot test only needs KVM (falls back to TCG without it). Host networking
# so QEMU's hostfwd ports (127.0.0.1:10022/18765/18444) are reachable from the
# host while a test VM runs; they are bound to loopback only.
KVM_DEV := $(shell [ -c /dev/kvm ] && echo --device /dev/kvm)
DOCKER_TEST := docker run --rm -i $(TTY) $(KVM_DEV) --network host $(DOCKER_ENV) \
	-v $(REPO):/src -w /src/image $(BUILDER_IMAGE)

.PHONY: all builder sync build qcow2 xz artifacts boot-test shell lint clean distclean

all: artifacts

builder:
	@docker image inspect $(BUILDER_IMAGE) >/dev/null 2>&1 || \
		docker build -t $(BUILDER_IMAGE) builder

sync: builder
	$(DOCKER_MKOSI) scripts/sync.sh $(ARCH)

build: builder
	$(DOCKER_MKOSI) scripts/build.sh $(ARCH) $(MKOSI_ARGS)

$(RAW):
	$(MAKE) build ARCH=$(ARCH)

qcow2: builder $(RAW)
	$(DOCKER_TEST) scripts/convert.sh $(ARCH) qcow2

xz: builder $(RAW)
	$(DOCKER_TEST) scripts/convert.sh $(ARCH) xz

artifacts: builder $(RAW)
	$(DOCKER_TEST) scripts/convert.sh $(ARCH) all

BOOT_TEST_ARGS ?=

boot-test: builder
	@[ -f $(QCOW2) ] || $(MAKE) qcow2 ARCH=$(ARCH)
	$(DOCKER_TEST) scripts/boot-test.sh $(ARCH) $(BOOT_TEST_ARGS)

shell: builder
	$(DOCKER_MKOSI) bash

lint: builder
	docker run --rm -v $(REPO):/mnt -w /mnt/image koalaman/shellcheck:stable \
		-x -P SCRIPTDIR:. scripts/*.sh mkosi.postinst.chroot mkosi.extra/usr/lib/agos/agos-display \
		mkosi.extra/usr/lib/agos/agos-session mkosi.extra/usr/lib/agos/wait-display \
		mkosi.extra/usr/bin/agos-browser mkosi.extra/etc/profile.d/agos.sh
	$(DOCKER_TEST) sh -c 'for f in mkosi.extra/usr/lib/agos/agos-firstboot mkosi.extra/usr/lib/agos/viewer-perm \
		mkosi.extra/usr/bin/agos scripts/boot_test.py; do python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" $$f || exit 1; done'

clean:
	$(DOCKER_MKOSI) bash -c 'rm -rf mkosi.output/* /var/tmp/mkosi-*'

distclean: builder
	$(DOCKER_MKOSI) bash -c 'rm -rf mkosi.output mkosi.cache .cache mkosi.packages mkosi.sandbox/etc/apt/keyrings'
