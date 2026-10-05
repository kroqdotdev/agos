"""a11y conversion and availability, sd_notify, Unix-socket peer checks."""

import asyncio
import os
import socket
import sys

import pytest
import uvicorn
from uvicorn.server import ServerState

from agentd import a11y
from agentd.audit import AuditLog
from agentd.errors import AgentdError
from agentd.geometry import Frame
from agentd.server import peer_checked_protocol, peer_uid, sd_notify, watchdog_interval

# -------------------------------------------------------------------- a11y


def test_a11y_bounds_convert_into_the_session_image_space():
    nodes = [
        a11y.Node("frame", "Editor", ["active", "showing"], (0, 0, 2560, 1600), 0, None),
        a11y.Node("push button", "Save", ["enabled", "showing"], (200, 100, 400, 160), 1, 0),
        a11y.Node("label", "no extents", ["showing"], None, 1, 0),
    ]
    frame = Frame(7, 1, (2560, 1600), (1280, 800))
    out = a11y.convert(nodes, "image", (2560, 1600), frame)
    assert out[0]["bbox"] == [0, 0, 1280, 800]
    assert out[1] == {
        "id": 1,
        "parent": 0,
        "depth": 1,
        "role": "push button",
        "name": "Save",
        "states": ["enabled", "showing"],
        "bbox": [100, 50, 200, 80],
        "bbox_screen": [200, 100, 400, 160],
    }
    assert out[2]["bbox"] is None
    norm = a11y.convert(nodes[1:2], "normalized", (2560, 1600), None)
    assert norm[0]["bbox"] == [78, 62, 157, 100]
    scr = a11y.convert(nodes[1:2], "screen", (2560, 1600), None)
    assert scr[0]["bbox"] == [200, 100, 400, 160]


def test_a11y_unavailable_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(a11y, "_atspi", None)
    monkeypatch.setitem(sys.modules, "gi", None)  # import gi -> ImportError
    with pytest.raises(AgentdError) as e:
        a11y.load()
    assert e.value.code == "A11Y_UNAVAILABLE" and e.value.status == 503
    assert "gir1.2-atspi-2.0" in e.value.message


# ---------------------------------------------------------------- sd_notify


def test_sd_notify_and_watchdog(tmp_path, monkeypatch):
    path = str(tmp_path / "notify")
    rx = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    rx.bind(path)
    rx.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    assert sd_notify("READY=1")
    assert rx.recv(100) == b"READY=1"
    rx.close()
    monkeypatch.delenv("NOTIFY_SOCKET")
    assert sd_notify("READY=1") is False

    monkeypatch.setenv("WATCHDOG_USEC", "10000000")
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
    assert watchdog_interval() == 5.0
    monkeypatch.setenv("WATCHDOG_PID", "1")
    assert watchdog_interval() is None
    monkeypatch.delenv("WATCHDOG_USEC")
    assert watchdog_interval() is None


def test_sd_notify_abstract_socket(monkeypatch):
    name = f"agentd-test-{os.getpid()}"
    rx = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    rx.bind("\0" + name)
    rx.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", "@" + name)
    assert sd_notify("WATCHDOG=1")
    assert rx.recv(100) == b"WATCHDOG=1"
    rx.close()


# ---------------------------------------------------------- peer credentials


def test_peer_uid_of_a_socketpair():
    a, b = socket.socketpair()
    try:
        assert peer_uid(a) == os.getuid()
    finally:
        a.close()
        b.close()


async def _app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"2")]})
    await send({"type": "http.response.body", "body": b"ok"})


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
@pytest.mark.parametrize("uid_offset,expect_ok", [(0, True), (1, False)])
async def test_unix_socket_rejects_other_uids(tmp_path, anyio_backend, uid_offset, expect_ok):
    audit = AuditLog(tmp_path / "audit.jsonl")
    proto_cls = peer_checked_protocol(audit, os.getuid() + uid_offset)
    config = uvicorn.Config(_app, http=proto_cls, lifespan="off", log_level="error")
    config.load()
    state = ServerState()
    loop = asyncio.get_running_loop()
    path = str(tmp_path / "s.sock")
    server = await loop.create_unix_server(
        lambda: proto_cls(config=config, server_state=state, app_state={}), path=path
    )
    try:
        reader, writer = await asyncio.open_unix_connection(path)
        try:
            writer.write(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            await writer.drain()
            data = await asyncio.wait_for(reader.read(), 5)
        except ConnectionResetError:
            data = b""  # closed with our request unread: also a rejection
        writer.close()
    finally:
        server.close()
        await server.wait_closed()
    if expect_ok:
        assert data.startswith(b"HTTP/1.1 200") and data.endswith(b"ok")
    else:
        assert data == b""
        assert audit.tail(1)[0]["peer_uid"] == os.getuid()
