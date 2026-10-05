#!/usr/bin/env python3
"""Zero-touch boot test for an agos disk image (runs inside the builder container).

Boots the release qcow2 (through a throwaway overlay, grown to test growfs)
under QEMU + UEFI firmware with a generated NoCloud CIDATA seed, then proves
without ever sending keyboard input to the VM:

  * it boots unattended and agos-firstboot/agos-ready report "ready"
  * the KasmVNC web client answers (with the seeded basic-auth password)
  * the XFCE desktop is up with no dialogs (screenshot via agentd, ffmpeg fallback)
  * agos-browser opens Chromium without first-run/keyring/restore dialogs
  * the viewer permission hook works, the root fs grew, the guest agent answers
  * a reboot comes back to "ready" on its own

Results go to out/boot-test-<arch>/ (results.json, serial.log, screenshots) and
screenshots are also copied to out/<arch>-*.png.

Usage: boot_test.py <amd64|arm64> [--keep] [--no-reboot] [--timeout S]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent          # image/
REPO = HERE.parent
VERSION = (REPO / "VERSION").read_text().strip()
SSH_PORT, AGENTD_PORT, VIEWER_PORT = 10022, 18765, 18444
AGENTD_TOKEN = "agd_boottest_0123456789abcdef0123456789abcdef"
VIEWER_PASSWORD = "boottest-viewer-pw"
DISPLAY_W, DISPLAY_H = 1280, 800

T0 = time.monotonic()


def log(msg: str) -> None:
    print(f"[{time.monotonic() - T0:7.1f}s] {msg}", flush=True)


class Fail(Exception):
    pass


def run(cmd, **kw) -> subprocess.CompletedProcess:
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return subprocess.run(cmd, **kw)


class VM:
    def __init__(self, arch: str, work: Path, timeout: float):
        self.arch, self.work, self.timeout = arch, work, timeout
        self.proc: subprocess.Popen | None = None
        self.key = work / "id_ed25519"
        self.kvm = os.access("/dev/kvm", os.W_OK)

    # --- setup -------------------------------------------------------------
    def prepare(self) -> None:
        w = self.work
        if w.exists():
            shutil.rmtree(w)
        w.mkdir(parents=True)
        base = HERE / "mkosi.output" / f"agos-{VERSION}-{self.arch}.qcow2"
        if not base.exists():
            raise Fail(f"{base} missing (make qcow2)")
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "agos-boot-test", "-f", str(self.key)],
            check=True)
        # throwaway overlay; 20G virtual size proves the root fs grows on first boot
        run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", str(base),
             str(w / "disk.qcow2"), "20G"], check=True)
        if self.arch == "amd64":
            shutil.copy("/usr/share/OVMF/OVMF_VARS_4M.fd", w / "vars.fd")
        else:
            shutil.copy("/usr/share/AAVMF/AAVMF_VARS.fd", w / "vars.fd")
        self.make_seed()

    def make_seed(self) -> None:
        seed = self.work / "seed"
        seed.mkdir()
        pub = (self.key.with_suffix(".pub")).read_text().strip()
        (seed / "meta-data").write_text("instance-id: agos-boot-test-1\nlocal-hostname: agos-test\n")
        # Viewer and agentd listen on all interfaces so QEMU's hostfwd (which
        # arrives from 10.0.2.2) reaches them; display size comes from the
        # systemd credential below to prove its precedence over /etc/agos.
        config = (
            "[display]\nwidth = 1024\nheight = 768\n"
            "[viewer]\nlisten = \"0.0.0.0\"\nport = 8444\ntls = \"auto\"\nuser = \"agos\"\n"
            "[agentd]\nlisten = \"0.0.0.0:8765\"\n")
        secrets_env = f"AGENTD_TOKEN={AGENTD_TOKEN}\nVIEWER_PASSWORD={VIEWER_PASSWORD}\n"
        b64 = lambda text: base64.b64encode(text.encode()).decode()  # noqa: E731
        # Same shape as deploy/proxmox/agos-proxmox.sh's seed: explicit agent
        # user with keys, config and secrets as base64 write_files.
        (seed / "user-data").write_text(f"""#cloud-config
hostname: agos-test
ssh_pwauth: false
disable_root: true
users:
  - name: agent
    lock_passwd: true
    ssh_authorized_keys:
      - '{pub}'
write_files:
  - path: /etc/agos/config.toml
    owner: root:root
    permissions: "0644"
    encoding: b64
    content: {b64(config)}
  - path: /etc/agos/secrets.env
    owner: root:root
    permissions: "0600"
    encoding: b64
    content: {b64(secrets_env)}
""")
        run(["xorriso", "-as", "mkisofs", "-quiet", "-output", str(self.work / "seed.iso"),
             "-volid", "CIDATA", "-joliet", "-rock", str(seed / "user-data"), str(seed / "meta-data")],
            check=True)
        # Layer 2: an AGOS-labelled FAT filesystem. Its display size is
        # overridden by /etc/agos (layer 3) and the credential (layer 4); its
        # tailscale.tags is set nowhere else, so it must survive the merge.
        agos_dir = self.work / "agos-fs"
        agos_dir.mkdir()
        (agos_dir / "config.toml").write_text('[display]\nwidth = 800\nheight = 600\n'
                                              '[tailscale]\ntags = ["tag:from-agos-fs"]\n')
        img = self.work / "agos-fs.img"
        run(["truncate", "-s", "8M", str(img)], check=True)
        run(["mkfs.vfat", "-n", "AGOS", str(img)], check=True)
        run(["mcopy", "-i", str(img), str(agos_dir / "config.toml"), "::config.toml"], check=True)

    # --- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        w = self.work
        cred = base64.b64encode(f"[display]\nwidth = {DISPLAY_W}\nheight = {DISPLAY_H}\n".encode()).decode()
        fwd = (f"hostfwd=tcp:127.0.0.1:{SSH_PORT}-:22,hostfwd=tcp:127.0.0.1:{AGENTD_PORT}-:8765,"
               f"hostfwd=tcp:127.0.0.1:{VIEWER_PORT}-:8444")
        common = [
            "-smp", "4", "-m", "4096",
            "-drive", f"file={w / 'disk.qcow2'},if=virtio,format=qcow2,discard=unmap",
            "-drive", f"file={w / 'agos-fs.img'},if=virtio,format=raw",
            "-netdev", f"user,id=n0,{fwd}", "-device", "virtio-net-pci,netdev=n0",
            "-device", "virtio-rng-pci",
            "-chardev", f"file,id=ser0,path={w / 'serial.log'}", "-serial", "chardev:ser0",
            "-chardev", f"socket,id=qga0,path={w / 'qga.sock'},server=on,wait=off",
            "-device", "virtio-serial", "-device", "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0",
            "-qmp", f"unix:{w / 'qmp.sock'},server=on,wait=off",
            "-smbios", f"type=11,value=io.systemd.credential.binary:agos.config={cred}",
            "-display", "none", "-no-user-config", "-nodefaults",
        ]
        if self.arch == "amd64":
            cmd = ["qemu-system-x86_64", "-machine", "q35" + (",accel=kvm" if self.kvm else ""),
                   "-cpu", "host" if self.kvm else "max",
                   "-drive", "if=pflash,format=raw,unit=0,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd",
                   "-drive", f"if=pflash,format=raw,unit=1,file={w / 'vars.fd'}",
                   "-drive", f"file={w / 'seed.iso'},media=cdrom,readonly=on,if=ide",
                   "-device", "virtio-vga", "-device", "i6300esb", "-action", "watchdog=reset"] + common
        else:
            native = os.uname().machine == "aarch64" and self.kvm
            cmd = ["qemu-system-aarch64", "-machine", "virt" + (",accel=kvm" if native else ""),
                   "-cpu", "host" if native else "max",
                   "-drive", "if=pflash,format=raw,unit=0,readonly=on,file=/usr/share/AAVMF/AAVMF_CODE.no-secboot.fd",
                   "-drive", f"if=pflash,format=raw,unit=1,file={w / 'vars.fd'}",
                   "-drive", f"file={w / 'seed.iso'},media=cdrom,readonly=on,if=none,id=cd0",
                   "-device", "virtio-scsi-pci", "-device", "scsi-cd,drive=cd0",
                   "-device", "virtio-gpu-pci"] + common
        log(f"starting VM ({'KVM' if self.kvm else 'TCG'}): {' '.join(cmd[:3])} ...")
        (w / "qemu-cmdline.txt").write_text(" ".join(cmd) + "\n")
        self.proc = subprocess.Popen(cmd, stdout=open(w / "qemu.log", "w"), stderr=subprocess.STDOUT)
        self.started = time.monotonic()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.alive():
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    # --- access ------------------------------------------------------------
    def ssh(self, command: str, timeout: float = 60, check: bool = True) -> subprocess.CompletedProcess:
        cmd = ["ssh", "-p", str(SSH_PORT), "-i", str(self.key), "-o", "BatchMode=yes",
               "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
               "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=5", "agent@127.0.0.1", command]
        res = run(cmd, timeout=timeout)
        if check and res.returncode != 0:
            raise Fail(f"ssh {command!r} failed ({res.returncode}): {res.stderr.strip()[-400:]}")
        return res

    def wait_ssh(self, timeout: float) -> float:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.alive():
                raise Fail("QEMU exited while waiting for SSH")
            try:
                if self.ssh("true", timeout=15, check=False).returncode == 0:
                    return time.monotonic() - self.started
            except subprocess.TimeoutExpired:
                pass
            time.sleep(3)
        raise Fail(f"no SSH after {timeout}s")

    def state(self) -> dict:
        res = self.ssh("cat /var/lib/agos/state.json", check=False)
        try:
            return json.loads(res.stdout)
        except ValueError:
            return {}

    def wait_ready(self, timeout: float, boot_id: str | None = None) -> dict:
        deadline = time.monotonic() + timeout
        st: dict = {}
        while time.monotonic() < deadline:
            st = self.state()
            if st.get("state") in ("ready", "degraded") and st.get("checks") is not None \
                    and (boot_id is None or st.get("boot_id") != boot_id):
                return st
            time.sleep(3)
        raise Fail(f"state.json not ready after {timeout}s: {st.get('state')!r} {st.get('errors')}")

    def qga(self, request: dict, timeout: float = 10) -> dict:
        """One request to qemu-guest-agent (how the Proxmox script reads state.json)."""
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(timeout)
            s.connect(str(self.work / "qga.sock"))
            sync = int(time.time()) & 0xFFFF
            s.sendall(json.dumps({"execute": "guest-sync", "arguments": {"id": sync}}).encode() + b"\n")
            s.sendall(json.dumps(request).encode() + b"\n")
            buf = b""
            replies = []
            while len(replies) < 2:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        replies.append(json.loads(line))
            return replies[-1] if replies else {}

    def qga_read(self, path: str) -> str:
        h = self.qga({"execute": "guest-file-open", "arguments": {"path": path, "mode": "r"}})["return"]
        try:
            r = self.qga({"execute": "guest-file-read", "arguments": {"handle": h, "count": 1 << 20}})["return"]
            return base64.b64decode(r["buf-b64"]).decode()
        finally:
            self.qga({"execute": "guest-file-close", "arguments": {"handle": h}})


def http(method: str, url: str, body: dict | None = None, token: str | None = None,
         auth: tuple[str, str] | None = None, timeout: float = 30) -> tuple[int, bytes]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
    data = json.dumps(body).encode() if body is not None else None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def screenshot(vm: VM, name: str, out: Path, results: dict) -> Path:
    path = vm.work / f"{name}.png"
    code, body = http("POST", f"http://127.0.0.1:{AGENTD_PORT}/v1/screenshot", {"format": "png"},
                      token=AGENTD_TOKEN, timeout=60)
    if code == 200:
        shot = json.loads(body)
        path.write_bytes(base64.b64decode(shot["data"]))
        results.setdefault("screenshots", {})[name] = {"via": "agentd", "screen": shot.get("screen"),
                                                       "image": shot.get("image")}
    else:
        # fallback without agentd: grab the X display with ffmpeg over SSH
        res = subprocess.run(
            ["ssh", "-p", str(SSH_PORT), "-i", str(vm.key), "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
             "agent@127.0.0.1",
             "ffmpeg -loglevel error -f x11grab -i :1 -frames:v 1 -f image2 -vcodec png -"],
            capture_output=True, timeout=60)
        if res.returncode != 0 or not res.stdout:
            raise Fail(f"screenshot {name} failed: agentd {code}, ffmpeg {res.stderr.decode()[-300:]}")
        path.write_bytes(res.stdout)
        results.setdefault("screenshots", {})[name] = {"via": "ffmpeg", "agentd_http": code}
    out.mkdir(parents=True, exist_ok=True)
    final = out / f"{vm.arch}-{name}.png"
    shutil.copy(path, final)
    log(f"screenshot {name} -> {final}")
    return final


def windows(vm: VM) -> list[str]:
    return vm.ssh("DISPLAY=:1 wmctrl -l -x", check=False).stdout.strip().splitlines()


def check(results: dict, name: str, ok: bool, detail="") -> None:
    results.setdefault("checks", {})[name] = {"ok": bool(ok), "detail": detail}
    log(f"{'PASS' if ok else 'FAIL'} {name} {detail if isinstance(detail, str) else json.dumps(detail)}"[:300])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("arch", choices=["amd64", "arm64"])
    ap.add_argument("--keep", action="store_true", help="leave the VM running at the end")
    ap.add_argument("--no-reboot", action="store_true")
    ap.add_argument("--timeout", type=float, default=0, help="boot timeout (default 600 KVM / 3600 TCG)")
    args = ap.parse_args()

    out = REPO / "out"
    work = out / f"boot-test-{args.arch}"
    vm = VM(args.arch, work, args.timeout)
    timeout = args.timeout or (600 if vm.kvm and (args.arch == "amd64" or os.uname().machine == "aarch64")
                               else 3600)
    results: dict = {"arch": args.arch, "version": VERSION, "kvm": vm.kvm,
                     "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    rc = 1
    try:
        vm.prepare()
        img = HERE / "mkosi.output" / f"agos-{VERSION}-{args.arch}"
        results["sizes_bytes"] = {ext: (img.with_name(img.name + ext)).stat().st_size
                                  for ext in (".raw", ".qcow2", ".raw.xz")
                                  if img.with_name(img.name + ext).exists()}
        vm.start()

        t_ssh = vm.wait_ssh(timeout)
        log(f"SSH up after {t_ssh:.1f}s")
        st = vm.wait_ready(timeout)
        t_ready = time.monotonic() - vm.started
        results["boot1"] = {"ssh_s": round(t_ssh, 1), "ready_wall_s": round(t_ready, 1),
                            "ready_uptime_s": st.get("ready_uptime"),
                            "firstboot_s": st.get("firstboot_seconds"), "state": st.get("state")}
        results["boot1"]["systemd_analyze"] = vm.ssh("systemd-analyze || true", check=False).stdout.strip()
        check(results, "state_ready", st.get("state") == "ready",
              {"state": st.get("state"), "errors": st.get("errors"), "checks": st.get("checks")})
        results["state_json"] = st

        # guest agent: the channel the Proxmox script uses
        try:
            via_qga = json.loads(vm.qga_read("/var/lib/agos/state.json"))
            check(results, "qemu_guest_agent_state", via_qga.get("state") == st.get("state"),
                  f"state via QGA: {via_qga.get('state')}")
        except (OSError, KeyError, ValueError) as exc:
            check(results, "qemu_guest_agent_state", False, repr(exc))

        sysrun = vm.ssh("systemctl is-system-running; systemctl --failed --no-legend --plain", check=False)
        check(results, "system_running", sysrun.stdout.strip().splitlines()[:1] == ["running"],
              sysrun.stdout.strip())
        cmdline = vm.ssh("cat /proc/cmdline").stdout.strip()
        eff = json.loads(vm.ssh("cat /run/agos/config.json").stdout)
        check(results, "config_layers",
              st.get("config_sources") == ["defaults", "agos-fs", "etc", "credentials"]
              and eff["tailscale"]["tags"] == ["tag:from-agos-fs"]
              and (eff["display"]["width"], eff["display"]["height"]) == (DISPLAY_W, DISPLAY_H)
              and eff["viewer"]["listen"] == "0.0.0.0",
              {"sources": st.get("config_sources"), "display": eff["display"], "tags": eff["tailscale"]["tags"]})
        modes = vm.ssh("stat -c '%n %a' / /etc /usr /usr/lib/agos /etc/sudoers.d/agos /home/agent").stdout.split("\n")
        check(results, "file_modes", modes[:6] == ["/ 755", "/etc 755", "/usr 755", "/usr/lib/agos 755",
                                                     "/etc/sudoers.d/agos 440", "/home/agent 700"], modes)
        secrets_mode = vm.ssh("sudo stat -c '%a %U' /etc/agos/secrets.env").stdout.strip()
        check(results, "secrets_file", secrets_mode == "600 root", secrets_mode)
        check(results, "kernel_cmdline", all(t in cmdline.split() for t in ("panic=10", "fsck.repair=yes")),
              cmdline)
        df = vm.ssh("df -B1 --output=size / | tail -1").stdout.strip()
        check(results, "root_grown", int(df) > 15 * 1024**3, f"/ is {int(df) / 1024**3:.1f} GiB on a 20 GiB disk")
        ident = vm.ssh("cat /etc/machine-id; ls /etc/ssh/ssh_host_*_key.pub | wc -l; hostname").stdout.split()
        check(results, "identity", len(ident) >= 3 and len(ident[0]) == 32 and int(ident[1]) >= 1,
              {"machine_id": ident[0] if ident else None, "host_keys": ident[1:2], "hostname": ident[2:3]})
        units = vm.ssh("systemctl --user is-active agos-display agos-session agentd; "
                       "systemctl is-active agos-firstboot agos-ready ssh qemu-guest-agent; "
                       "systemctl is-enabled tailscaled || true", check=False).stdout.split()
        results["units"] = units
        check(results, "user_units_active", units[:2] == ["active", "active"], units)

        # viewer: TLS + basic auth in LAN mode
        code_noauth, _ = http("GET", f"https://127.0.0.1:{VIEWER_PORT}/")
        code_auth, page = http("GET", f"https://127.0.0.1:{VIEWER_PORT}/", auth=("agos", VIEWER_PASSWORD))
        check(results, "kasmvnc_viewer", code_noauth == 401 and code_auth == 200 and b"<html" in page.lower(),
              f"https without auth {code_noauth}, with auth {code_auth}")

        # agentd
        code_h, body_h = http("GET", f"http://127.0.0.1:{AGENTD_PORT}/v1/health")
        code_s, body_s = http("GET", f"http://127.0.0.1:{AGENTD_PORT}/v1/status", token=AGENTD_TOKEN)
        code_u, _ = http("GET", f"http://127.0.0.1:{AGENTD_PORT}/v1/status")
        check(results, "agentd_api", code_h == 200 and code_s == 200 and code_u == 401,
              f"health {code_h} {body_h[:120]!r}, status(token) {code_s}, status(no token) {code_u}")

        # desktop: no dialogs, pinned geometry from the credential
        time.sleep(5)
        desk = screenshot(vm, "desktop", out, results)
        geo = vm.ssh("DISPLAY=:1 xdpyinfo | awk '/dimensions/{print $2}'").stdout.strip()
        check(results, "geometry_from_credential", geo == f"{DISPLAY_W}x{DISPLAY_H}", geo)
        wins = windows(vm)
        results["windows_desktop"] = wins
        check(results, "no_dialog_windows", not any(w for w in wins if "xfce4-panel" not in w
                                                    and "xfdesktop" not in w), wins)

        # agentd end to end: click/type into a terminal, then takeover -> input
        # refused (HUMAN_IN_CONTROL) with the KasmVNC viewer switched to
        # control by the on_takeover hook -> handback -> viewer view-only again
        api = f"http://127.0.0.1:{AGENTD_PORT}/v1"
        perm = lambda: vm.ssh("/usr/lib/agos/viewer-perm status", check=False).stdout.strip()  # noqa: E731
        http("POST", f"{api}/launch", {"argv": ["xfce4-terminal", "--geometry=90x20+200+150"]}, token=AGENTD_TOKEN)
        time.sleep(4)
        code_a, body_a = http("POST", f"{api}/actions", {"actions": [
            {"type": "screenshot"},
            {"type": "click", "x": 500, "y": 300},
            {"type": "type", "text": "echo agos-e2e-$((6*7))"},
            {"type": "key", "keys": "Return"},
            {"type": "wait", "duration": 1.0}], "screenshot_after": False}, token=AGENTD_TOKEN)
        time.sleep(1)
        typed = vm.ssh("DISPLAY=:1 xdotool search --class xfce4-terminal | head -1", check=False).stdout.strip()
        screenshot(vm, "agentd-typed", out, results)
        check(results, "agentd_click_type", code_a == 200 and json.loads(body_a).get("ok") is True,
              f"actions HTTP {code_a}, terminal window {typed or 'missing'}")
        perms = [perm()]
        code_t, body_t = http("POST", f"{api}/takeover", {"by": "boot-test", "reason": "e2e"}, token=AGENTD_TOKEN)
        time.sleep(1)
        perms.append(perm())
        code_r, body_r = http("POST", f"{api}/actions", {"actions": [
            {"type": "click", "x": 500, "y": 300, "coord_space": "screen"}], "screenshot_after": False},
            token=AGENTD_TOKEN)
        refused = code_r == 409 and b"HUMAN_IN_CONTROL" in body_r
        code_h, body_h = http("DELETE", f"{api}/takeover", {"by": "boot-test"}, token=AGENTD_TOKEN)
        time.sleep(1)
        perms.append(perm())
        # after a handback earlier observations are stale: input is refused with
        # STALE_FRAME (plus a fresh screenshot) until the agent looks again
        key = {"type": "key", "keys": "ctrl+l"}
        code_s, body_s = http("POST", f"{api}/actions", {"actions": [key], "screenshot_after": False},
                              token=AGENTD_TOKEN)
        code_b, _ = http("POST", f"{api}/actions", {"actions": [{"type": "screenshot"}, key],
                                                    "screenshot_after": False}, token=AGENTD_TOKEN)
        check(results, "takeover_lease",
              code_t == 200 and refused and code_h == 200 and code_s == 409 and b"STALE_FRAME" in body_s
              and code_b == 200,
              f"takeover {code_t}; input during lease {code_r} HUMAN_IN_CONTROL={refused}; handback {code_h}; "
              f"input after handback {code_s} STALE_FRAME={b'STALE_FRAME' in body_s}; "
              f"screenshot+input {code_b}")
        check(results, "viewer_perm_hooks", perms == ["view", "control", "view"], perms)
        vm.ssh("pkill -x xfce4-terminal", check=False)

        # agos-browser: Chromium without first-run/keyring/restore dialogs
        vm.ssh("systemd-run --user --unit=agos-browser-test --collect agos-browser https://example.com/", check=False)
        time.sleep(15)
        screenshot(vm, "chromium", out, results)
        wins = windows(vm)
        results["windows_chromium"] = wins
        cdp = vm.ssh("curl -s http://127.0.0.1:9222/json/version", check=False).stdout
        chromium_wins = [w for w in wins if "chromium" in w.lower()]
        check(results, "chromium_single_window", len(chromium_wins) == 1, chromium_wins)
        check(results, "chromium_cdp", '"Browser"' in cdp, cdp.strip()[:160])
        vm.ssh("systemctl --user stop agos-browser-test", check=False)

        # Firefox ESR policies (no Terms of Use / onboarding / default-browser prompt)
        vm.ssh("systemd-run --user --unit=agos-firefox-test --collect firefox-esr", check=False)
        time.sleep(15)
        screenshot(vm, "firefox", out, results)
        results["windows_firefox"] = windows(vm)
        ff = [w for w in results["windows_firefox"] if "firefox" in w.lower()]
        check(results, "firefox_no_first_run", len(ff) == 1 and "Welcome" not in ff[0]
              and "Privacy" not in ff[0] and "Restore" not in ff[0], ff)
        vm.ssh("systemctl --user stop agos-firefox-test", check=False)

        # Claude Code pre-seed
        cc = vm.ssh("claude --version 2>&1; jq -c '{hasCompletedOnboarding, mcp: .mcpServers.desktop}' ~/.claude.json;"
                    " jq -c . ~/.claude/settings.json", check=False).stdout.strip()
        check(results, "claude_code_preseed", "hasCompletedOnboarding\":true" in cc and "bypassPermissions" in cc, cc)

        time.sleep(20)
        results["idle_memory"] = vm.ssh("free -m; echo; ps -eo rss,comm --sort=-rss | head -12", check=False).stdout
        mem = vm.ssh("free -m | awk '/^Mem:/{print $3}'", check=False).stdout.strip()
        results["idle_used_mib"] = int(mem) if mem.isdigit() else None
        log(f"idle memory used: {mem} MiB")

        if not args.no_reboot:
            boot_id = st.get("boot_id")
            log("rebooting (sudo systemctl reboot)")
            vm.ssh("sudo systemctl reboot", check=False, timeout=30)
            t_reboot = time.monotonic()
            time.sleep(10)
            vm.started = t_reboot
            vm.wait_ssh(timeout)
            st2 = vm.wait_ready(timeout, boot_id=boot_id)
            results["boot2"] = {"ready_wall_s": round(time.monotonic() - t_reboot, 1),
                                "ready_uptime_s": st2.get("ready_uptime"), "state": st2.get("state")}
            check(results, "reboot_ready", st2.get("state") == "ready" and st2.get("boot_id") != boot_id,
                  {"state": st2.get("state"), "errors": st2.get("errors")})
            time.sleep(5)
            screenshot(vm, "desktop-after-reboot", out, results)
            results["windows_after_reboot"] = windows(vm)

        failed = [k for k, v in results["checks"].items() if not v["ok"]]
        results["failed"] = failed
        rc = 0 if not failed else 1
        if not args.keep:
            vm.ssh("sudo systemctl poweroff", check=False, timeout=30)
            try:
                vm.proc.wait(120)
            except subprocess.TimeoutExpired:
                pass
    except (Fail, subprocess.SubprocessError, OSError) as exc:
        results["fatal"] = repr(exc)
        log(f"FATAL: {exc}")
        rc = 2
    finally:
        if not args.keep:
            vm.stop()
        results["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        work.mkdir(parents=True, exist_ok=True)
        (work / "results.json").write_text(json.dumps(results, indent=2) + "\n")
        uid, gid = os.environ.get("HOST_UID"), os.environ.get("HOST_GID")
        if uid:
            run(["chown", "-R", f"{uid}:{gid or uid}", str(out)])
        log(f"results: {work / 'results.json'} -> {'PASS' if rc == 0 else 'FAIL'}"
            + (f" (failed: {results.get('failed') or results.get('fatal')})" if rc else ""))
    if args.keep and vm.alive():
        log("VM left running (--keep); Ctrl-C to stop")
        try:
            vm.proc.wait()
        except KeyboardInterrupt:
            vm.stop()
    return rc


if __name__ == "__main__":
    sys.exit(main())
