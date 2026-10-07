#!/usr/bin/env python3
"""End-to-end check of a running agos VM, from any machine that can SSH to it.

    tools/agos-check.py agent@192.168.0.170 [--reboot] [--claude] [--viewer]

Reads the agentd token and the viewer password from the VM (with sudo, never
printed), tunnels to agentd over SSH, and exercises what agos promises:
services and zero-touch settings, the viewer, every agentd surface (REST,
provider adapters, MCP over HTTP and stdio), takeover, browsers, the in-VM
Claude Code wiring and, with --reboot, an unattended reboot.

Writes results.json and screenshots to out/check-<host>/ and exits non-zero
if any check failed. Needs: ssh with a key for the VM's `agent` user; uv (for
the MCP client, via ../agentd); docker only for --viewer.
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import re
import shlex
import socket
import ssl
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SSH_OPTS = [
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "ServerAliveInterval=10",
]


class Check:
    def __init__(self, host: str, out: Path, ssh_extra: list[str]):
        self.host = host
        self.ssh_opts = SSH_OPTS + ssh_extra
        self.ip = host.split("@")[-1]
        self.out = out
        self.results: list[dict] = []
        self.secrets: list[str] = []
        self.tunnel: subprocess.Popen | None = None
        self.port = 0
        self.token = ""

    # ------------------------------------------------------------ plumbing
    def log(self, msg: str) -> None:
        for s in self.secrets:
            msg = msg.replace(s, "<redacted>")
        print(msg, flush=True)

    def record(self, name: str, status: str, detail="") -> None:
        if not isinstance(detail, str):
            detail = json.dumps(detail)
        for s in self.secrets:
            detail = detail.replace(s, "<redacted>")
        self.results.append({"check": name, "status": status, "detail": detail})
        mark = {"PASS": "\033[32mPASS\033[0m", "FAIL": "\033[31mFAIL\033[0m"}.get(status, status)
        self.log(f"{mark:>14}  {name:<28} {detail[:150]}")

    def check(self, name: str, ok: bool, detail="") -> bool:
        self.record(name, "PASS" if ok else "FAIL", detail)
        return ok

    def ssh(self, command: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["ssh", *self.ssh_opts, self.host, command], capture_output=True, text=True, timeout=timeout, check=False
        )

    def sh(self, command: str, timeout: float = 60) -> str:
        return self.ssh(command, timeout).stdout.strip()

    def x(self, command: str, timeout: float = 60) -> str:
        return self.sh(f"DISPLAY=:1 {command}", timeout)

    def open_tunnel(self) -> None:
        self.close_tunnel()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.tunnel = subprocess.Popen(
            [
                "ssh",
                *self.ssh_opts,
                "-N",
                "-o",
                "ExitOnForwardFailure=yes",
                "-L",
                f"127.0.0.1:{self.port}:127.0.0.1:8765",
                self.host,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    return
            except OSError:
                time.sleep(0.3)
        raise RuntimeError("SSH tunnel to agentd did not come up")

    def close_tunnel(self) -> None:
        if self.tunnel:
            self.tunnel.terminate()
            self.tunnel.wait(timeout=10)
            self.tunnel = None

    def api(
        self, method: str, path: str, body=None, token: str | None = "default", timeout: float = 60
    ) -> tuple[int, dict | str]:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        tok = self.token if token == "default" else token
        if tok:
            req.add_header("Authorization", f"Bearer {tok}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw, code = r.read(), r.status
        except urllib.error.HTTPError as e:
            raw, code = e.read(), e.code
        try:
            return code, json.loads(raw)
        except ValueError:
            return code, raw.decode(errors="replace")

    def save_png(self, name: str, b64: str) -> Path:
        path = self.out / f"{name}.png"
        path.write_bytes(base64.b64decode(b64))
        return path

    def screenshot(self, name: str | None = None, session: str = "check") -> dict:
        code, body = self.api("POST", "/v1/screenshot", {"session": session})
        if code != 200 or not isinstance(body, dict):
            raise RuntimeError(f"screenshot failed: {code} {str(body)[:200]}")
        if name:
            self.save_png(name, body["data"])
        return body

    def windows(self) -> list[dict]:
        code, body = self.api("GET", "/v1/windows")
        return body.get("windows", []) if code == 200 and isinstance(body, dict) else []

    def wait_window(self, needle: str, timeout: float = 60) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for w in self.windows():
                if needle.lower() in (w.get("title", "") + " " + w.get("class", "")).lower():
                    return w
            time.sleep(1)
        return None

    def actions(self, actions: list[dict], session: str = "check", shot: bool = False) -> tuple[int, dict]:
        code, body = self.api("POST", "/v1/actions", {"session": session, "actions": actions, "screenshot_after": shot})
        return code, body if isinstance(body, dict) else {"raw": body}

    # ------------------------------------------------------------- checks
    def read_secrets(self) -> dict:
        raw = self.sh("sudo -n grep -E '^(AGENTD_TOKEN|VIEWER_PASSWORD)=' /etc/agos/secrets.env")
        sec = {}
        for line in raw.splitlines():
            k, _, v = line.partition("=")
            sec[k] = v.strip().strip("'\"")
        self.secrets = [v for v in sec.values() if v]
        self.token = sec.get("AGENTD_TOKEN", "")
        return sec

    def system(self) -> dict:
        st = self.sh("systemctl is-system-running")
        failed = self.sh("systemctl --failed --no-legend --plain | awk '{print $1}'")
        self.check("system_running", st == "running", st + (f" failed: {failed}" if failed else ""))
        state = json.loads(self.sh("cat /var/lib/agos/state.json") or "{}")
        self.check(
            "agos_state_ready",
            state.get("state") == "ready" and not state.get("errors"),
            {k: state.get(k) for k in ("state", "errors", "checks")},
        )
        units = self.sh("systemctl --user is-active agos-display agos-session agentd").split()
        self.check("user_units_active", units == ["active"] * 3, units)
        cmdline = self.sh("cat /proc/cmdline")
        self.check("kernel_cmdline", "panic=10" in cmdline and "fsck.repair=yes" in cmdline, cmdline)
        loader = self.sh("sudo -n cat /efi/loader/loader.conf 2>/dev/null")
        self.check("boot_menu_timeout_0", "timeout 0" in loader, loader.replace("\n", "; "))
        sleep = self.sh(
            "systemctl is-enabled sleep.target suspend.target hibernate.target hybrid-sleep.target 2>&1"
        ).split()
        self.check("sleep_masked", sleep and all(s == "masked" for s in sleep), sleep)
        self.check("no_needrestart", not self.sh("command -v needrestart || true"), "absent")
        uu = self.sh("systemctl is-enabled unattended-upgrades 2>&1; systemctl is-active apt-daily-upgrade.timer")
        self.check("unattended_upgrades", uu.split() == ["enabled", "active"], uu.split())
        wd = self.sh("systemctl show -p RuntimeWatchdogUSec --value")
        self.check("hardware_watchdog", wd not in ("", "0", "infinity"), wd)
        ntp = self.sh("timedatectl show -p NTPSynchronized --value")
        self.check("clock_synchronized", ntp == "yes", ntp)
        sshd = dict(
            line.split(" ", 1)
            for line in self.sh(
                "sudo -n sshd -T 2>/dev/null | grep -E '^(passwordauthentication|permitrootlogin) '"
            ).splitlines()
            if " " in line
        )
        self.check(
            "sshd_keys_only", sshd.get("passwordauthentication") == "no" and sshd.get("permitrootlogin") == "no", sshd
        )
        self.check("passwordless_sudo", self.ssh("sudo -n true").returncode == 0, "sudo -n true")
        self.check("root_locked", self.sh("sudo -n passwd -S root | awk '{print $2}'") == "L", "passwd -S root")
        cfg = json.loads(self.sh("cat /run/agos/config.json") or "{}")
        listen = self.sh("ss -Htln | awk '{print $4}' | sort -u").split()
        viewer = cfg.get("viewer", {})
        want_viewer = f"{viewer.get('listen', '127.0.0.1')}:{viewer.get('port', 8444)}"
        agentd_ok = "127.0.0.1:8765" in listen and not any(
            a.endswith(":8765") and not a.startswith("127.") for a in listen
        )
        cdp_ok = not any(a.endswith(":9222") and not a.startswith("127.") for a in listen)
        self.check(
            "listening_ports",
            agentd_ok and cdp_ok and want_viewer in listen,
            {"listen": listen, "viewer_expected": want_viewer},
        )
        errs = self.sh("journalctl -b -p err -q --no-pager -o cat | tail -n 15")
        self.record("journal_errors", "INFO", errs.replace("\n", " | ") or "none")
        return cfg

    def desktop(self) -> None:
        dims = self.x("xdpyinfo | awk '/dimensions:/{print $2}'")
        self.check("display_1280x800", dims == "1280x800", dims)
        wins = self.x("wmctrl -l -x").splitlines()
        stray = [w for w in wins if not any(k in w for k in ("xfce4-panel", "xfdesktop", "t3code", "T3"))]
        self.check("no_dialog_windows", not stray, stray or [w.split(None, 3)[-1] for w in wins])
        xset = self.x("xset q")
        saver_off = re.search(r"timeout:\s+0\b", xset) is not None
        # KasmVNC's Xvnc has no DPMS extension at all, which is stricter than "disabled".
        dpms_off = "DPMS is Disabled" in xset or "does not have the DPMS Extension" in xset
        lockers = self.sh(
            "ps -eo comm= | grep -E '^(xfce4-power-man|xfce4-screensav|light-locker|xscreensaver)' || true"
        )
        self.check(
            "no_screensaver_or_dpms",
            saver_off and dpms_off and not lockers,
            {"screensaver_timeout_0": saver_off, "dpms_off_or_absent": dpms_off, "lockers": lockers or "none"},
        )

    def viewer(self, cfg: dict, sec: dict, render: bool) -> None:
        v = cfg.get("viewer", {})
        if v.get("listen") in ("127.0.0.1", "::1"):
            self.record("viewer_auth", "SKIP", "loopback viewer (Tailscale or SSH tunnel mode)")
            return
        scheme = "https" if v.get("tls", True) else "http"
        url = f"{scheme}://{self.ip}:{v.get('port', 8444)}/"
        # LAN mode serves a self-signed certificate generated in the VM at first
        # boot. Read it over the authenticated SSH channel and trust only it, so
        # the viewer password is never sent to anything else on the LAN.
        crt = "~/.config/agos/tls/viewer.crt"
        pem = self.sh(f"cat {crt}")
        spki = self.sh(
            f"openssl x509 -in {crt} -pubkey -noout | openssl pkey -pubin -outform der"
            " | openssl dgst -sha256 -binary | base64"
        )
        if scheme == "https" and "BEGIN CERTIFICATE" not in pem:
            self.check("viewer_auth", False, f"cannot read {crt} to pin the viewer certificate")
            return
        ctx = ssl.create_default_context(cadata=pem) if scheme == "https" else None
        if ctx:
            ctx.check_hostname = False  # pinned to this exact certificate instead

        def status(auth: str | None) -> int:
            req = urllib.request.Request(url)
            if auth:
                req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
            try:
                with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code

        user = v.get("user", "agos")
        no, yes = status(None), status(f"{user}:{sec.get('VIEWER_PASSWORD', '')}")
        self.check("viewer_auth", no == 401 and yes == 200, f"{url} without auth {no}, with auth {yes}")
        if render:
            pw = sec.get("VIEWER_PASSWORD", "")
            res = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "host",
                    "-v",
                    f"{REPO}/image/tests:/w:ro",
                    "-v",
                    f"{self.out}:/out",
                    "-e",
                    f"VPW={pw}",
                    "-e",
                    f"SPKI={spki}",
                    "-w",
                    "/tmp",
                    "mcr.microsoft.com/playwright:v1.63.0-noble",
                    "sh",
                    "-c",
                    (
                        "npm init -y >/dev/null && npm i --silent playwright@1.63.0 >/dev/null 2>&1 && "
                        f"cp /w/viewer.mjs . && node viewer.mjs '{url}?autoconnect=1&resize=scale' "
                        f'{shlex.quote(user)} "$VPW" /out/00-viewer.png "$SPKI"'
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
            ok = res.returncode == 0 and "websockify" in res.stdout
            self.check("viewer_renders", ok, "00-viewer.png" if ok else (res.stdout + res.stderr)[-300:])

    def agentd_basics(self) -> None:
        code, body = self.api("GET", "/v1/health", token=None)
        self.check("agentd_health", code == 200 and isinstance(body, dict) and body.get("ok"), body)
        c1, _ = self.api("GET", "/v1/status", token=None)
        c2, _ = self.api("GET", "/v1/status", token="agd_wrong")
        c3, _ = self.api("GET", "/v1/status")
        self.check("agentd_auth", (c1, c2, c3) == (401, 401, 200), f"no token {c1}, bad token {c2}, token {c3}")
        shot = self.screenshot("01-desktop")
        self.check(
            "screenshot",
            shot.get("screen") == [1280, 800] and shot.get("scale") == 1.0,
            {k: shot.get(k) for k in ("screen", "image", "scale", "frame_id", "format")} | {"file": "01-desktop.png"},
        )
        code, ui = self.api("GET", "/ui", token=None)
        self.check("control_ui", code == 200 and "agentd" in str(ui).lower(), f"/ui {code}")

    def drive_terminal(self) -> None:
        code, _ = self.api("POST", "/v1/launch", {"argv": ["xfce4-terminal", "--title", "agos-check"]})
        win = self.wait_window("agos-check", 30) if code == 200 else None
        if not self.check("launch_app", win is not None, {"launch": code, "window": win and win.get("title")}):
            return
        code, _ = self.api("POST", f"/v1/windows/{win['id']}/activate", {})
        self.check("window_activate", code == 200, code)
        self.screenshot(session="check")
        x, y, w, h = win["geometry"]
        marker = "agos-check-" + "".join(random.choices(string.ascii_lowercase, k=8))
        code, res = self.actions(
            [
                {"type": "click", "x": x + w // 2, "y": y + h // 2},
                {"type": "type", "text": f"echo {marker} > /tmp/agos-check.txt"},
                {"type": "key", "keys": "Return"},
                {"type": "wait_for_stable", "timeout": 3},
            ],
            shot=True,
        )
        if res.get("screenshot"):
            self.save_png("02-typed-in-terminal", res["screenshot"]["data"])
        got = self.sh("cat /tmp/agos-check.txt 2>/dev/null")
        self.check(
            "click_type_key",
            code == 200 and got == marker,
            f"HTTP {code}; file {'matches' if got == marker else repr(got)}; 02-typed-in-terminal.png",
        )
        code, res = self.actions(
            [
                {"type": "move", "x": 500, "y": 500, "coord_space": "normalized"},
                {"type": "cursor_position", "coord_space": "screen"},
            ]
        )
        pos = next((r for r in res.get("results", []) if r.get("type") == "cursor_position"), {})
        self.check(
            "normalized_coords",
            code == 200 and abs(pos.get("x", -9) - 640) <= 2 and abs(pos.get("y", -9) - 400) <= 2,
            {"cursor": [pos.get("x"), pos.get("y")]},
        )
        code, res = self.actions(
            [
                {"type": "scroll", "x": x + w // 2, "y": y + h // 2, "dy": 2},
                {"type": "drag", "path": [[x + 20, y + 60], [x + 200, y + 60]]},
                {"type": "key", "keys": "ctrl+shift+c"},
            ]
        )
        self.check("scroll_drag_combo", code == 200 and res.get("ok"), [r.get("ok") for r in res.get("results", [])])
        code, res = self.actions([{"type": "zoom", "region": [0, 0, 320, 200]}])
        z = (res.get("results") or [{}])[0].get("screenshot") or {}
        self.check("zoom", code == 200 and bool(z.get("data")), {"image": z.get("image")})
        code, res = self.actions([{"type": "wait_for_stable", "timeout": 5}])
        r0 = (res.get("results") or [{}])[0]
        self.check("wait_for_stable", code == 200 and r0.get("stable") is True, r0)
        code, a11y = self.api("GET", "/v1/a11y?window=agos-check&max_nodes=60")
        self.check(
            "a11y_tree",
            code == 200 and isinstance(a11y, dict) and a11y.get("count", 0) > 0,
            {
                "count": a11y.get("count") if isinstance(a11y, dict) else a11y,
                "roles": sorted({e.get("role") for e in (a11y.get("elements", []) if isinstance(a11y, dict) else [])})[
                    :8
                ],
            },
        )
        self.ssh("pkill -f 'xfce4-terminal --title agos-check' || true")

    def clipboard_exec(self) -> None:
        text = "agos clipboard " + "".join(random.choices(string.ascii_letters, k=10))
        c1, _ = self.api("PUT", "/v1/clipboard", {"text": text})
        c2, got = self.api("GET", "/v1/clipboard")
        self.check(
            "clipboard_roundtrip",
            (c1, c2) == (200, 200) and isinstance(got, dict) and got.get("text") == text,
            f"PUT {c1}, GET {c2}",
        )
        code, res = self.api("POST", "/v1/exec", {"argv": ["id", "-un"]})
        self.check(
            "exec",
            code == 200 and isinstance(res, dict) and res.get("stdout", "").strip() == "agent",
            {
                "exit": res.get("exit_code") if isinstance(res, dict) else res,
                "stdout": res.get("stdout", "").strip() if isinstance(res, dict) else "",
            },
        )

    def takeover(self) -> None:
        perm = lambda: self.sh("/usr/lib/agos/viewer-perm status")
        p0 = perm()
        c_take, _ = self.api("POST", "/v1/takeover", {"by": "agos-check", "reason": "runthrough"})
        p1 = perm()
        c_in, b_in = self.actions([{"type": "move", "x": 10, "y": 10}])
        c_obs, _ = self.api("POST", "/v1/screenshot", {"session": "check"})
        c_back, _ = self.api("DELETE", "/v1/takeover", {"by": "agos-check"})
        p2 = perm()
        c_stale, b_stale = self.actions([{"type": "move", "x": 12, "y": 12}])
        self.screenshot(session="check")
        c_ok, _ = self.actions([{"type": "move", "x": 14, "y": 14}])
        code_in = (b_in.get("error") or {}).get("code")
        code_stale = (b_stale.get("error") or {}).get("code")
        self.check(
            "takeover_blocks_agent",
            c_take == 200 and c_in == 409 and code_in == "HUMAN_IN_CONTROL" and c_obs == 200,
            f"take {c_take}; input {c_in} {code_in}; screenshot during lease {c_obs}",
        )
        self.check(
            "handback_stale_frame",
            c_back == 200 and c_stale == 409 and code_stale == "STALE_FRAME" and c_ok == 200,
            f"handback {c_back}; next input {c_stale} {code_stale}; after fresh screenshot {c_ok}",
        )
        self.check("viewer_perm_follows_lease", [p0, p1, p2] == ["view", "control", "view"], [p0, p1, p2])
        code, audit = self.api("GET", "/v1/audit?limit=200")
        events = [e.get("event") for e in audit.get("entries", [])] if isinstance(audit, dict) else []
        leases = (
            [e.get("action") or e.get("lease") for e in audit.get("entries", []) if e.get("event") == "lease"]
            if isinstance(audit, dict)
            else []
        )
        self.check(
            "audit_log",
            code == 200 and "action" in events and "lease" in events,
            {"events": sorted(set(events)), "lease": leases[-2:]},
        )

    def adapters(self) -> None:
        code, a = self.api("POST", "/v1/adapters/anthropic?session=adapt-a", {"action": "screenshot"})
        ok = code == 200 and isinstance(a, dict) and any(c.get("type") == "image" for c in a.get("content", []))
        tool_use = {
            "type": "tool_use",
            "id": "toolu_check",
            "name": "mouse_move",
            "toolset_name": "computer",
            "input": {"coordinate": [200, 200]},
        }
        code2, b = self.api("POST", "/v1/adapters/anthropic?session=adapt-a", tool_use)
        ok2 = code2 == 200 and isinstance(b, dict) and b.get("tool_use_id") == "toolu_check" and not b.get("is_error")
        self.check(
            "adapter_anthropic",
            ok and ok2,
            f"screenshot {code}, toolset mouse_move {code2} {'ok' if ok2 else str(b)[:120]}",
        )
        call = {
            "type": "computer_call",
            "call_id": "call_check",
            "pending_safety_checks": [],
            "actions": [{"type": "screenshot"}],
        }
        code, o = self.api("POST", "/v1/adapters/openai?session=adapt-o", call)
        ok = (
            code == 200
            and isinstance(o, dict)
            and o.get("type") == "computer_call_output"
            and str(o.get("output", {}).get("image_url", "")).startswith("data:image/")
        )
        call2 = {
            "type": "computer_call",
            "call_id": "call_check2",
            "pending_safety_checks": [],
            "actions": [{"type": "move", "x": 300, "y": 300}],
        }
        code2, _ = self.api("POST", "/v1/adapters/openai?session=adapt-o", call2)
        self.check("adapter_openai", ok and code2 == 200, f"screenshot {code}, move {code2}")
        code, g = self.api(
            "POST", "/v1/adapters/gemini?session=adapt-g", {"name": "hover_at", "args": {"x": 500, "y": 500}}
        )
        ok = code == 200 and isinstance(g, dict) and any("inlineData" in p for p in g.get("parts", []))
        self.check("adapter_gemini", ok, f"hover_at {code} {'with screenshot' if ok else str(g)[:120]}")

    def mcp_http(self) -> None:
        script = f"""
import asyncio, base64, httpx2, json
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
async def main():
    http = httpx2.AsyncClient(headers={{"Authorization": "Bearer " + {self.token!r}}}, timeout=60)
    async with Client(streamable_http_client("http://127.0.0.1:{self.port}/mcp", http_client=http)) as c:
        tools = sorted(t.name for t in (await c.list_tools()).tools)
        res = await c.call_tool("computer", {{"action": "screenshot"}})
        img = next((x for x in res.content if x.type == "image"), None)
        print(json.dumps({{"tools": tools, "is_error": res.is_error,
                           "png": bool(img) and base64.b64decode(img.data)[:4] == b"\\x89PNG"}}))
asyncio.run(main())
"""
        res = subprocess.run(
            ["uv", "run", "--quiet", "--project", str(REPO / "agentd"), "python", "-c", script],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        try:
            r = json.loads(res.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            self.check("mcp_streamable_http", False, (res.stdout + res.stderr)[-300:])
            return
        self.check(
            "mcp_streamable_http",
            not r["is_error"] and r["png"] and "computer" in r["tools"] and len(r["tools"]) == 8,
            r,
        )

    def claude(self, live: bool) -> None:
        cc = self.sh("claude --version 2>&1 | head -1")
        pre = self.sh(
            "jq -c '{o: .hasCompletedOnboarding, d: .mcpServers.desktop.command}' ~/.claude.json; "
            "jq -c '{m: .permissions.defaultMode, s: .skipDangerousModePermissionPrompt}' ~/.claude/settings.json"
        )
        self.check(
            "claude_code_preseeded",
            "Claude Code" in cc and '"o":true' in pre and "bypassPermissions" in pre,
            f"{cc}; {pre}",
        )
        mcp = self.sh("cd ~ && timeout 90 claude mcp list 2>&1", timeout=120)
        self.check(
            "claude_sees_desktop_mcp",
            "desktop" in mcp and ("✓" in mcp or "Connected" in mcp),
            mcp.replace("\n", " | ")[-220:],
        )
        has_key = self.sh(
            "systemctl --user show-environment | grep -cE '^(ANTHROPIC_API_KEY|CLAUDE_CODE_OAUTH_TOKEN)=' || true"
        )
        if not live:
            self.record("claude_drives_desktop", "SKIP", "pass --claude to run a real Claude Code turn")
            return
        if has_key in ("", "0"):
            self.record("claude_drives_desktop", "SKIP", "no ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN in the VM")
            return
        prompt = (
            "Use the desktop MCP server's computer tool to take exactly one screenshot of the screen. "
            "Then reply with only the label of the leftmost item in the top panel, nothing else."
        )
        out = self.sh(f"cd ~ && timeout 240 claude -p {shlex.quote(prompt)} --max-turns 4 2>&1", timeout=300)
        self.check("claude_drives_desktop", "applications" in out.lower(), out[-200:])

    def browsers(self) -> None:
        self.ssh(
            "systemctl --user stop agos-check-browser agos-check-firefox 2>/dev/null; "
            "systemd-run --user --unit=agos-check-browser --collect agos-browser https://example.com/"
        )
        win = self.wait_window("example domain", 90)
        time.sleep(3)
        self.screenshot("03-chromium")
        cdp = self.sh("curl -s --max-time 5 http://127.0.0.1:9222/json/version")
        chromium = [w["title"] for w in self.windows() if "chromium" in w.get("class", "").lower()]
        self.check("chromium_no_prompts", win is not None and len(chromium) == 1, chromium)
        self.check("chromium_cdp", '"Browser"' in cdp, cdp[:120])
        self.ssh("systemctl --user stop agos-check-browser")
        self.ssh("systemd-run --user --unit=agos-check-firefox --collect firefox-esr")
        win = self.wait_window("firefox", 90)
        time.sleep(4)
        self.screenshot("04-firefox")
        ff = [w["title"] for w in self.windows() if "firefox" in w.get("class", "").lower()]
        self.check(
            "firefox_no_prompts",
            win is not None
            and len(ff) == 1
            and not any(k in ff[0] for k in ("Welcome", "Privacy", "Terms", "Restore")),
            ff,
        )
        self.ssh("systemctl --user stop agos-check-firefox")

    def reboot(self) -> None:
        boot0 = self.sh("cat /proc/sys/kernel/random/boot_id")
        self.close_tunnel()
        self.ssh("sudo -n systemctl reboot", timeout=20)
        t0 = time.monotonic()
        time.sleep(10)
        state, boot1 = {}, boot0
        while time.monotonic() - t0 < 600:
            try:
                r = self.ssh("cat /proc/sys/kernel/random/boot_id; cat /var/lib/agos/state.json", timeout=15)
            except subprocess.TimeoutExpired:
                continue
            lines = r.stdout.strip().splitlines()
            if r.returncode == 0 and lines and lines[0] != boot0:
                boot1 = lines[0]
                try:
                    state = json.loads("\n".join(lines[1:]))
                except ValueError:
                    state = {}
                if state.get("state") == "ready":
                    break
            time.sleep(3)
        took = time.monotonic() - t0
        if not self.check(
            "reboot_unattended",
            boot1 != boot0 and state.get("state") == "ready",
            f"ready {took:.0f}s after the reboot command",
        ):
            return
        self.open_tunnel()
        code, _ = self.api("GET", "/v1/health", token=None)
        self.screenshot("05-after-reboot")
        self.check("agentd_after_reboot", code == 200, "05-after-reboot.png")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host", help="agent@<vm-ip>")
    ap.add_argument("--reboot", action="store_true", help="also reboot the VM and check it comes back unattended")
    ap.add_argument("--claude", action="store_true", help="run one real Claude Code turn in the VM (uses its API key)")
    ap.add_argument("--viewer", action="store_true", help="render the KasmVNC web client in headless Chromium (docker)")
    ap.add_argument("--out", type=Path, help="output dir (default out/check-<ip>)")
    ap.add_argument("--port", type=int, help="SSH port (e.g. a QEMU hostfwd)")
    ap.add_argument("--identity", type=Path, help="SSH private key for the agent user")
    args = ap.parse_args()

    out = args.out or REPO / "out" / f"check-{args.host.split('@')[-1]}"
    out.mkdir(parents=True, exist_ok=True)
    extra = (["-p", str(args.port)] if args.port else []) + (["-i", str(args.identity)] if args.identity else [])
    c = Check(args.host, out, extra)
    if c.ssh("true", timeout=20).returncode != 0:
        print(f"cannot ssh to {args.host} (is your key in the VM's ~agent/.ssh/authorized_keys?)", file=sys.stderr)
        return 2
    c.log(f"agos check: {args.host} -> {out}")
    sec = c.read_secrets()
    try:
        cfg = c.system()
        c.desktop()
        c.viewer(cfg, sec, args.viewer)
        c.open_tunnel()
        c.agentd_basics()
        c.drive_terminal()
        c.clipboard_exec()
        c.takeover()
        c.adapters()
        c.mcp_http()
        c.claude(args.claude)
        c.browsers()
        if args.reboot:
            c.reboot()
    except Exception as exc:  # noqa: BLE001 -- report it and keep the results so far
        c.record("runner", "FAIL", f"{type(exc).__name__}: {exc}")
    finally:
        c.close_tunnel()
    (out / "results.json").write_text(json.dumps(c.results, indent=2))
    counts = {s: sum(r["status"] == s for r in c.results) for s in ("PASS", "FAIL", "SKIP", "INFO")}
    c.log(f"\n{counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped -> {out}/results.json")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
