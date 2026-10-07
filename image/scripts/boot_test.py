#!/usr/bin/env python3
"""Zero-touch boot test for an agos disk image (runs inside the builder container).

Boots the release qcow2 (through a throwaway overlay, grown to test growfs)
under QEMU + UEFI firmware with a generated NoCloud CIDATA seed, then proves
without ever sending keyboard input to the VM:

  * it boots unattended and agos-firstboot/agos-ready report "ready"
  * the KasmVNC web client answers (with the seeded basic-auth password)
  * the XFCE desktop is up with no dialogs (screenshot via agentd, ffmpeg fallback)
  * agos-browser opens Chromium without first-run/keyring/restore dialogs
  * T3 Code runs on workspace 2, never shows up on (or switches) the agent's
    workspace, has no dialogs, and follows [agents] t3code through `agos apply`
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


def agent_workspace(wins: list[str]) -> list[str]:
    """wmctrl -l -x lines visible on the agent's workspace (desktop 0 or sticky),
    minus the panel and the desktop itself."""
    out = []
    for w in wins:
        cols = w.split()
        if len(cols) >= 3 and cols[1] in ("0", "-1") and "xfce4-panel" not in cols[2] \
                and "xfdesktop" not in cols[2]:
            out.append(w)
    return out


def t3_windows(wins: list[str]) -> list[str]:
    # WM_CLASS com.t3tools.t3code.com.t3tools.T3Code (Electron takes it from the
    # desktop entry name)
    return [w for w in wins if len(w.split()) >= 3 and "t3code" in w.split()[2].lower()]


def current_desktop(vm: VM) -> str:
    """wmctrl -d marks the current workspace with '*'."""
    for line in vm.ssh("DISPLAY=:1 wmctrl -d", check=False).stdout.splitlines():
        cols = line.split()
        if len(cols) > 1 and cols[1] == "*":
            return cols[0]
    return "?"


def wait_windows(vm: VM, match: str, timeout: float) -> list[str]:
    """Poll until a window whose wmctrl line contains `match` appears (browsers
    start in seconds under KVM but can take minutes under TCG emulation)."""
    deadline = time.monotonic() + timeout
    while True:
        wins = windows(vm)
        if any(match in w.lower() for w in wins) or time.monotonic() > deadline:
            return wins
        time.sleep(2)


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
    accelerated = vm.kvm and (args.arch == "amd64" or os.uname().machine == "aarch64")
    timeout = args.timeout or (600 if accelerated else 3600)
    app_timeout = 60 if accelerated else 300
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
        # no LLMNR (5355) or mDNS (5353) responder listening on the network
        llmnr = vm.ssh("ss -Hlnut '( sport = :5355 or sport = :5353 )'", check=False)
        check(results, "no_llmnr_mdns_listener", llmnr.returncode == 0 and not llmnr.stdout.strip(),
              llmnr.stdout.strip() or "nothing on 5355/5353")
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

        # desktop: no dialogs, pinned geometry from the credential. T3 Code
        # (on by default) starts with the session: wait for its window so the
        # agent's workspace is checked with it running.
        t0 = time.monotonic()
        wait_windows(vm, "t3code", app_timeout * 2)
        results["t3code_window_s"] = round(time.monotonic() - t0, 1)
        time.sleep(5)
        desk = screenshot(vm, "desktop", out, results)
        geo = vm.ssh("DISPLAY=:1 xdpyinfo | awk '/dimensions/{print $2}'").stdout.strip()
        check(results, "geometry_from_credential", geo == f"{DISPLAY_W}x{DISPLAY_H}", geo)
        wins = windows(vm)
        results["windows_desktop"] = wins
        check(results, "no_dialog_windows", not agent_workspace(wins), wins)

        # T3 Code: running, on workspace 2 (index 1), alone there (no dialogs),
        # the agent's workspace still current and its focus untouched
        t3 = vm.ssh("systemctl --user is-active agos-t3code; pgrep -u agent -x t3code | head -1; "
                    "jq -c '{agents, t3code, t3check: .checks.t3code}' /var/lib/agos/state.json; "
                    "jq -r .t3code /usr/lib/agos/build-info.json; "
                    "systemctl --user show -p Environment agos-t3code", check=False).stdout.strip().splitlines()
        results["t3code_status"] = t3
        check(results, "t3code_running",
              len(t3) >= 4 and t3[0] == "active" and t3[1].isdigit() and '"t3code":true' in t3[2]
              and '"t3check":true' in t3[2] and t3[3] not in ("", "null"), t3)
        st_line = vm.ssh("sudo agos status | grep -E '^  (t3code|check +t3code)'", check=False).stdout.strip()
        check(results, "agos_status_t3code",
              "t3code     on, workspace 2" in st_line and "check      t3code: ok" in st_line, st_line)
        # T3 Connect without the GUI: `agos t3 status` runs T3 Code's own CLI as
        # the agent user (unpaired here: authorization missing)
        t3s = vm.ssh("agos t3 status", timeout=90, check=False)
        check(results, "agos_t3_status_cli",
              t3s.returncode == 0 and "T3 Connect" in t3s.stdout and "Authorization:" in t3s.stdout,
              t3s.stdout.strip().splitlines()[:3] or t3s.stderr.strip()[-200:])
        t3w = t3_windows(wins)
        active = vm.ssh("DISPLAY=:1 xprop -id \"$(DISPLAY=:1 xdotool getactivewindow)\" WM_CLASS",
                        check=False).stdout.strip()
        ws_count = vm.ssh("xfconf-query -c xfwm4 -p /general/workspace_count", check=False).stdout.strip()
        desk_now = current_desktop(vm)
        check(results, "t3code_on_workspace_2",
              len(t3w) == 1 and t3w[0].split()[1] == "1" and desk_now == "0" and ws_count == "2"
              and "t3code" not in active.lower(),
              {"t3code_windows": t3w, "current_desktop": desk_now, "workspaces": ws_count, "active_class": active})
        # pre-seed and zero-touch settings: no welcome wizard, no updates, no telemetry
        pre = vm.ssh("jq -r .onboardingCompletedAt ~/.t3/userdata/client-settings.json; "
                     "xfconf-query -c xfwm4 -p /general/activate_action; "
                     "xfconf-query -c xfwm4 -p /general/scroll_workspaces; "
                     "jq -r .enableProviderUpdateChecks ~/.t3/userdata/settings.json; "
                     "grep -rhoE 'Automatic updates[^\"]*' ~/.t3/userdata/logs 2>/dev/null | sort -u | head -3",
                     check=False).stdout.strip().splitlines()
        results["t3code_preseed"] = pre
        env_line = next((x for x in t3 if x.startswith("Environment=")), "")
        check(results, "t3code_preseed",
              len(pre) >= 4 and pre[0].startswith("20") and pre[1] == "none" and pre[2] == "false" and pre[3] == "false"
              and "T3CODE_DISABLE_AUTO_UPDATE=true" in env_line and "T3CODE_TELEMETRY_ENABLED=false" in env_line,
              {"settings": pre, "environment": env_line})
        # the agent scrolling on the empty desktop must not switch workspaces
        http("POST", f"http://127.0.0.1:{AGENTD_PORT}/v1/actions", {"actions": [
            {"type": "screenshot"}, {"type": "scroll", "x": DISPLAY_W // 2, "y": DISPLAY_H // 2, "dy": 5},
            {"type": "scroll", "x": DISPLAY_W // 2, "y": DISPLAY_H // 2, "dy": -5}],
            "screenshot_after": False}, token=AGENTD_TOKEN)
        time.sleep(1)
        check(results, "agent_scroll_keeps_workspace", current_desktop(vm) == "0", current_desktop(vm))
        # a human's view: switch to workspace 2 (test VM only), look, switch back
        vm.ssh("DISPLAY=:1 wmctrl -s 1", check=False)
        time.sleep(4)
        screenshot(vm, "t3code", out, results)
        seen = current_desktop(vm)
        # T3 Connect needs a one-time human sign-in: Settings -> Connections ->
        # "Sign in to T3 Connect" must open T3 Code's own sign-in dialog (no
        # extra window, nothing on the agent's workspace). Positions are for
        # the pinned release's layout, relative to its window.
        geo = dict(kv.split("=", 1) for kv in vm.ssh(
            "DISPLAY=:1 xdotool search --onlyvisible --classname com.t3tools.t3code getwindowgeometry --shell %@ | tail -6",
            check=False).stdout.split() if "=" in kv)
        signin = {}
        try:
            gx, gy, gh = int(geo["X"]), int(geo["Y"]), int(geo["HEIGHT"])
            api_actions = f"http://127.0.0.1:{AGENTD_PORT}/v1/actions"
            for name, (x, y) in (("settings", (gx + 25, gy + gh - 20)), ("connections", (gx + 84, gy + 391)),
                                 ("sign-in", (gx + 112, gy + gh - 60))):
                code, _ = http("POST", api_actions, {"actions": [
                    {"type": "screenshot"}, {"type": "click", "x": x, "y": y, "coord_space": "screen"},
                    {"type": "wait", "duration": 3}], "screenshot_after": False}, token=AGENTD_TOKEN)
                signin[name] = code
            screenshot(vm, "t3code-signin", out, results)
            wins_signin = windows(vm)
            http("POST", api_actions, {"actions": [{"type": "screenshot"}, {"type": "key", "keys": "Escape"},
                                                   {"type": "wait", "duration": 1},
                                                   {"type": "click", "x": gx + 60, "y": gy + gh - 20,
                                                    "coord_space": "screen"}], "screenshot_after": False},
                 token=AGENTD_TOKEN)
        except (KeyError, ValueError) as exc:
            wins_signin = []
            signin["error"] = repr(exc)
        check(results, "t3code_signin_in_app",
              all(v == 200 for k, v in signin.items() if k != "error") and len(signin) == 3
              and len(t3_windows(wins_signin)) == 1 and not agent_workspace(wins_signin),
              {"clicks": signin, "windows": wins_signin, "geometry": geo})
        vm.ssh("DISPLAY=:1 wmctrl -s 0", check=False)
        time.sleep(1)
        back = current_desktop(vm)
        check(results, "t3code_workspace_switch", seen == "1" and back == "0" and not agent_workspace(windows(vm)),
              {"while_viewing": seen, "after": back})

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
        wait_windows(vm, "chromium", app_timeout)
        time.sleep(5)  # let the first page and any dialog render
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
        wait_windows(vm, "firefox", app_timeout)
        time.sleep(5)
        screenshot(vm, "firefox", out, results)
        results["windows_firefox"] = windows(vm)
        ff = [w for w in results["windows_firefox"] if "firefox" in w.lower()]
        ff_ok = len(ff) == 1 and "Welcome" not in ff[0] and "Privacy" not in ff[0] and "Restore" not in ff[0]
        if not ff_ok:
            results["firefox_journal"] = vm.ssh(
                "journalctl --user -u agos-firefox-test --no-pager -n 80", check=False).stdout[-6000:]
        check(results, "firefox_no_first_run", ff_ok, ff)
        vm.ssh("systemctl --user stop agos-firefox-test", check=False)

        # Claude Code pre-seed
        cc = vm.ssh("claude --version 2>&1; jq -c '{hasCompletedOnboarding, mcp: .mcpServers.desktop}' ~/.claude.json;"
                    " jq -c . ~/.claude/settings.json", check=False).stdout.strip()
        check(results, "claude_code_preseed", "hasCompletedOnboarding\":true" in cc and "bypassPermissions" in cc, cc)

        # [agents] t3code = false -> `agos apply` stops and disables it and the
        # second workspace goes away; true again brings both back
        def t3_state() -> list[str]:
            return vm.ssh("systemctl --user is-active agos-t3code; systemctl --global is-enabled agos-t3code; "
                          "xfconf-query -c xfwm4 -p /general/workspace_count", check=False).stdout.split()
        conf = "/etc/agos/config.toml"
        vm.ssh(f"sudo cp {conf} {conf}.boot-test && printf '\\n[agents]\\nt3code = false\\n' | sudo tee -a {conf} >/dev/null"
               " && sudo agos apply >/dev/null 2>&1", check=False, timeout=180)
        time.sleep(10)
        off = t3_state() + [str(len(t3_windows(windows(vm))))]
        vm.ssh(f"sudo mv {conf}.boot-test {conf} && sudo agos apply >/dev/null 2>&1", check=False, timeout=180)
        wait_windows(vm, "t3code", app_timeout * 2)
        time.sleep(5)
        on_wins = t3_windows(windows(vm))
        on = t3_state() + [on_wins[0].split()[1] if on_wins else "-"]
        check(results, "t3code_config_toggle",
              off == ["inactive", "disabled", "1", "0"] and on == ["active", "enabled", "2", "1"]
              and not agent_workspace(windows(vm)),
              {"t3code=false": off, "t3code=true": on})

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
            wait_windows(vm, "t3code", app_timeout * 2)
            time.sleep(5)
            screenshot(vm, "desktop-after-reboot", out, results)
            results["windows_after_reboot"] = wins = windows(vm)
            t3w = t3_windows(wins)
            check(results, "t3code_after_reboot",
                  len(t3w) == 1 and t3w[0].split()[1] == "1" and current_desktop(vm) == "0"
                  and not agent_workspace(wins), wins)

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
