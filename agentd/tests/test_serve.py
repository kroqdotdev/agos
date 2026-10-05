"""The real server: TCP + Unix socket, MCP clients, CLI, stdio bridge."""

import asyncio
import base64
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import httpx2
import pytest
from conftest import make_config
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from agentd import mcp_server
from agentd.auth import TokenStore
from agentd.server import serve
from agentd.stdio import make_factory

pytestmark = pytest.mark.anyio


def write_config(cfg, path: Path) -> Path:
    lines = [
        f"{k} = {json.dumps(getattr(cfg, k))}"
        for k in ("display", "listen", "socket", "tokens_file", "audit_log", "settle_ms")
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


class Running:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ready = threading.Event()
        self.info = {}
        self.loop = None
        self.stop = None
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        assert self.ready.wait(15), self.error
        if self.error:
            raise self.error

    def _run(self):
        async def main():
            self.loop = asyncio.get_running_loop()
            self.stop = asyncio.Event()

            def on_ready(info):
                self.info = info
                self.ready.set()

            await serve(self.cfg, on_ready=on_ready, stop=self.stop)

        try:
            asyncio.run(main())
        except BaseException as exc:  # surface startup failures to the test
            self.error = exc
            self.ready.set()

    @property
    def url(self):
        return f"http://{self.info['tcp']}"

    def close(self):
        self.loop.call_soon_threadsafe(self.stop.set)
        self.thread.join(10)


@pytest.fixture
def server(tmp_path, xvfb):
    cfg = make_config(tmp_path, display=xvfb.display, settle_ms=50)
    token = TokenStore(cfg.tokens_path).create("tester", ["admin"])
    run = Running(cfg)
    run.token = token
    run.config_file = write_config(cfg, tmp_path / "config.toml")
    yield run
    run.close()
    assert not os.path.exists(cfg.socket)  # removed on shutdown


def cli(server, *args, stdin=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTD_")}
    return subprocess.run(
        [sys.executable, "-m", "agentd", "--config", str(server.config_file), *args],
        capture_output=True,
        text=True,
        env=env,
        input=stdin,
        timeout=60,
    )


async def test_mcp_streamable_http_with_bearer(server):
    async with httpx2.AsyncClient() as anon:
        r = await anon.post(server.url + "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {server.token}"}, timeout=30)
    async with Client(streamable_http_client(server.url + "/mcp", http_client=http)) as client:
        tools = await client.list_tools()
        names = sorted(t.name for t in tools.tools)
        assert names == sorted(
            [
                "computer",
                "windows",
                "clipboard_get",
                "clipboard_set",
                "launch",
                "wait_for_stable",
                "status",
                "a11y_tree",
            ]
        )
        computer = next(t for t in tools.tools if t.name == "computer")
        assert "zoom" in computer.input_schema["properties"]["action"]["enum"]
        res = await client.call_tool("computer", {"action": "screenshot"})
        assert not res.is_error
        assert res.content[0].type == "text" and "Screenshot 1280x800" in res.content[0].text
        assert res.content[1].type == "image" and base64.b64decode(res.content[1].data)[:4] == b"\x89PNG"
        res = await client.call_tool("computer", {"action": "mouse_move", "coordinate": [64, 48]})
        assert not res.is_error and res.content[1].type == "image"
        res = await client.call_tool("computer", {"action": "cursor_position"})
        assert res.content[0].text == "X=64, Y=48"
        res = await client.call_tool("status", {})
        assert res.structured_content["display"] == server.cfg.display
        res = await client.call_tool("clipboard_set", {"text": "via mcp"})
        assert not res.is_error
        res = await client.call_tool("clipboard_get", {})
        assert res.structured_content == {"text": "via mcp"}
        res = await client.call_tool("computer", {"action": "key", "text": "ctrl+nosuchkey"})
        assert res.is_error and "INVALID_ACTION" in res.content[0].text
    await http.aclose()


async def test_mcp_scopes_are_enforced_per_tool(server):
    observer = TokenStore(server.cfg.tokens_path).create("watch", ["observe"])
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {observer}"}, timeout=30)
    async with Client(streamable_http_client(server.url + "/mcp", http_client=http)) as client:
        res = await client.call_tool("computer", {"action": "screenshot"})
        assert not res.is_error
        res = await client.call_tool("computer", {"action": "left_click", "coordinate": [1, 1]})
        assert res.is_error and "FORBIDDEN" in res.content[0].text
        res = await client.call_tool("launch", {"argv": ["true"]})
        assert res.is_error and "FORBIDDEN" in res.content[0].text
    await http.aclose()


def test_cli_over_the_unix_socket(server):
    r = cli(server, "status", "--json")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["display"] == server.cfg.display
    r = cli(server, "takeover", "--reason", "testing")
    assert r.returncode == 0 and "paused" in r.stdout
    r = cli(server, "status")
    assert "input: paused" in r.stdout and "testing" in r.stdout
    r = cli(server, "handback")
    assert r.returncode == 0 and "epoch is now 2" in r.stdout
    r = cli(server, "handback")
    assert "no takeover was active" in r.stdout


def test_cli_without_server(tmp_path):
    cfg = make_config(tmp_path)
    cfg_file = write_config(cfg, tmp_path / "c.toml")
    r = subprocess.run(
        [sys.executable, "-m", "agentd", "--config", str(cfg_file), "status"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode != 0 and "cannot reach the server" in r.stderr


def test_cli_tokens(tmp_path):
    cfg = make_config(tmp_path)
    cfg_file = write_config(cfg, tmp_path / "c.toml")
    base = [sys.executable, "-m", "agentd", "--config", str(cfg_file), "token"]
    r = subprocess.run(
        [*base, "create", "--scopes", "observe,input", "--name", "bot"], capture_output=True, text=True, timeout=30
    )
    assert r.returncode == 0 and r.stdout.startswith("agd_")
    token = r.stdout.strip()
    assert token not in Path(cfg.tokens_file).read_text()
    r = subprocess.run(
        [*base, "create", "--scopes", "admin", "--name", "fb", "--stdin"],
        input="agd_given_by_firstboot_0123456789\n",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0 and r.stdout == ""
    assert TokenStore(cfg.tokens_path).verify("agd_given_by_firstboot_0123456789").name == "fb"
    r = subprocess.run([*base, "list"], capture_output=True, text=True, timeout=30)
    assert "bot\tobserve,input" in r.stdout
    assert subprocess.run([*base, "revoke", "bot"], timeout=30).returncode == 0
    assert TokenStore(cfg.tokens_path).verify(token) is None
    r = subprocess.run([*base, "create", "--scopes", "root"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 1


async def test_stdio_bridge_forwards_to_the_server(server):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agentd", "--config", str(server.config_file), "mcp", "--mode", "remote"],
        env={k: v for k, v in os.environ.items() if not k.startswith("AGENTD_")},
    )
    async with Client(params) as client:
        res = await client.call_tool("computer", {"action": "screenshot"})
        assert not res.is_error and res.content[1].type == "image"
        res = await client.call_tool("computer", {"action": "left_click", "coordinate": [30, 40]})
        assert not res.is_error
        # The lease lives in the server, so it applies to the bridge.
        server.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_takeover(server)))
        await asyncio.sleep(0.5)
        res = await client.call_tool("computer", {"action": "type", "text": "blocked"})
        assert res.is_error and "HUMAN_IN_CONTROL" in res.content[0].text
    audit = [json.loads(line) for line in Path(server.cfg.audit_log).read_text().splitlines()]
    clicks = [e for e in audit if e["event"] == "action" and e["action"]["type"] == "click"]
    assert clicks and clicks[-1]["transport"] == "unix" and clicks[-1]["session"].startswith("sess_")


async def _takeover(server):
    from agentd.service import unix_request

    await asyncio.to_thread(unix_request, server.cfg.socket, "POST", "/v1/takeover", {"by": "tester"})


async def test_stdio_local_mode_drives_the_display(tmp_path, xvfb):
    cfg = make_config(tmp_path, display=":2")  # overridden below by --display
    cfg_file = write_config(cfg, tmp_path / "c.toml")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agentd", "--config", str(cfg_file), "--display", xvfb.display, "mcp", "--mode", "local"],
    )
    async with Client(params) as client:
        tools = await client.list_tools()
        assert "computer" in {t.name for t in tools.tools}
        res = await client.call_tool("computer", {"action": "screenshot"})
        assert not res.is_error and "screen 1280x800" in res.content[0].text
        res = await client.call_tool("computer", {"action": "zoom", "region": [0, 0, 640, 400]})
        assert not res.is_error and "shown at 1280x800" in res.content[0].text


async def test_in_memory_mcp_client(tmp_path, xvfb):
    cfg = make_config(tmp_path, display=xvfb.display, mcp_mode="local")
    server = mcp_server.build(mcp_server.StdioProvider(make_factory(cfg)), cfg.display)
    async with Client(server) as client:
        res = await client.call_tool("computer", {"action": "screenshot"})
        assert res.content[1].type == "image"
        res = await client.call_tool("computer", {"action": "wait", "duration": 0.1})
        assert "wait done" in res.content[0].text
        res = await client.call_tool("wait_for_stable", {"timeout": 2, "settle_ms": 100})
        assert res.structured_content["stable"] is True
        res = await client.call_tool("windows", {})
        assert "windows" in res.structured_content
        res = await client.call_tool("a11y_tree", {})
        assert res.is_error and ("A11Y_UNAVAILABLE" in res.content[0].text or "NOT_FOUND" in res.content[0].text)
