"""REST API through Starlette's TestClient, real X11 backend on a private Xvfb."""

import base64
import importlib.util
import io

import pytest
from conftest import make_config
from PIL import Image
from starlette.testclient import TestClient

from agentd.auth import TokenStore
from agentd.server import Tagged, build


@pytest.fixture
def api(tmp_path, xvfb):
    cfg = make_config(
        tmp_path, display=xvfb.display, settle_ms=50, cdp_url="", viewer_url="https://agos.example.ts.net/"
    )
    store = TokenStore(cfg.tokens_path)
    tokens = {
        name: store.create(name, scopes)
        for name, scopes in {
            "admin": ["admin"],
            "observer": ["observe"],
            "agent": ["observe", "input"],
            "files": ["files"],
            "exec": ["exec"],
            "human": ["observe", "takeover"],
        }.items()
    }
    app, service, _ = build(cfg)
    with TestClient(app) as client:
        client.tokens = tokens  # type: ignore[attr-defined]
        client.app_ = app  # type: ignore[attr-defined]
        yield client


def auth(client, name):
    return {"Authorization": f"Bearer {client.tokens[name]}"}


def test_health_needs_no_token(api):
    r = api.get("/v1/health")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["version"] == "0.1.0"


def test_tcp_always_needs_a_token(api):
    r = api.get("/v1/status")
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"
    assert r.headers["www-authenticate"].startswith("Bearer")
    assert api.get("/v1/status", headers={"Authorization": "Bearer agd_wrong"}).status_code == 401
    r = api.get("/v1/status", headers=auth(api, "observer"))
    assert r.status_code == 200
    body = r.json()
    assert body["screen"] == [1280, 800] and body["display_ok"] and body["lease"] == {"held": False}
    assert body["viewer_url"] == "https://agos.example.ts.net/"


def test_missing_scope_is_forbidden(api):
    h = auth(api, "observer")
    r = api.post("/v1/actions", json={"actions": [{"type": "key", "keys": "a"}]}, headers=h)
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"
    assert api.post("/v1/exec", json={"argv": ["true"]}, headers=h).status_code == 403
    assert api.get("/v1/clipboard", headers=h).status_code == 403
    assert api.post("/v1/display", json={"width": 800, "height": 600}, headers=auth(api, "agent")).status_code == 403
    # Observation-only batches only need observe.
    r = api.post("/v1/actions", json={"actions": [{"type": "cursor_position"}], "screenshot_after": False}, headers=h)
    assert r.status_code == 200


def test_screenshot_endpoint(api):
    r = api.post("/v1/screenshot", json={"format": "jpeg"}, headers=auth(api, "observer"))
    assert r.status_code == 200
    body = r.json()
    for key in ("session", "frame_id", "epoch", "screen", "image", "scale", "coord_space", "cursor", "format"):
        assert key in body
    assert body["mime_type"] == "image/jpeg" and body["session"] == "default"
    assert Image.open(io.BytesIO(base64.b64decode(body["data"]))).size == (1280, 800)
    r = api.post("/v1/screenshot", headers=auth(api, "observer"))
    assert r.status_code == 200 and r.json()["format"] == "png"


def test_sessions_and_actions(api):
    h = auth(api, "agent")
    sid = api.post("/v1/sessions", headers=h).json()["session"]
    assert sid.startswith("sess_")
    # No screenshot in this session yet: rejected with a fresh one.
    r = api.post("/v1/actions", json={"session": sid, "actions": [{"type": "move", "x": 10, "y": 10}]}, headers=h)
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "STALE_FRAME" and body["screenshot"]["session"] == sid
    r = api.post(
        "/v1/actions",
        headers=h,
        json={"session": sid, "actions": [{"type": "move", "x": 10, "y": 10}, {"type": "cursor_position"}]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["results"][1]["x"] == 10 and body["screenshot"]["cursor"] == [10, 10]
    r = api.post("/v1/actions", headers=h, json={"actions": [{"type": "fly"}]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_ACTION"
    r = api.post("/v1/actions", headers=h, content=b"{nope")
    assert r.status_code == 400


def test_adapters(api):
    h = auth(api, "agent")
    api.post("/v1/screenshot", json={"session": "a1"}, headers=h)
    r = api.post("/v1/adapters/anthropic?session=a1", headers=h, json={"action": "left_click", "coordinate": [20, 30]})
    assert r.status_code == 200 and "x-agentd-error-code" not in r.headers
    assert [b["type"] for b in r.json()["content"]] == ["text", "image"]
    r = api.post(
        "/v1/adapters/anthropic?session=never-shot",
        headers=h,
        json={
            "type": "tool_use",
            "id": "toolu_1",
            "name": "left_click",
            "toolset_name": "computer",
            "input": {"coordinate": [1, 1]},
        },
    )
    assert r.status_code == 200 and r.headers["x-agentd-error-code"] == "STALE_FRAME"
    assert r.json()["is_error"] is True and r.json()["tool_use_id"] == "toolu_1"

    r = api.post(
        "/v1/adapters/openai",
        headers={**h, "X-Agentd-Session": "a1"},
        json={"type": "computer_call", "call_id": "call_1", "actions": [{"type": "move", "x": 5, "y": 5}]},
    )
    assert r.status_code == 200
    out = r.json()
    assert out["type"] == "computer_call_output" and out["output"]["image_url"].startswith("data:image/png")
    r = api.post(
        "/v1/adapters/openai",
        headers=h,
        json={
            "type": "computer_call",
            "call_id": "c",
            "actions": [],
            "pending_safety_checks": [{"id": "s1", "code": "x", "message": "y"}],
        },
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "CONFIRMATION_REQUIRED"

    api.post("/v1/screenshot", json={"session": "g"}, headers=h)
    r = api.post(
        "/v1/adapters/gemini?session=g&format=webp", headers=h, json={"name": "hover_at", "args": {"x": 500, "y": 500}}
    )
    assert r.status_code == 200
    assert r.json()["parts"][0]["inlineData"]["mimeType"] == "image/webp"
    assert api.post("/v1/adapters/gemini", headers=auth(api, "observer"), json={"name": "x"}).status_code == 403


def test_windows_endpoints(api, xwindow):
    r = api.get("/v1/windows", headers=auth(api, "observer"))
    assert any(w["id"] == xwindow.id and w["title"] == "agentd-test" for w in r.json()["windows"])
    r = api.post(f"/v1/windows/{xwindow.id}/activate", headers=auth(api, "agent"))
    assert r.status_code == 200
    r = api.post("/v1/windows/123/activate", headers=auth(api, "agent"))
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"


def test_clipboard_endpoints(api):
    h = auth(api, "files")
    assert api.put("/v1/clipboard", json={"text": "from REST ✓"}, headers=h).json() == {"ok": True, "length": 11}
    assert api.get("/v1/clipboard", headers=h).json() == {"text": "from REST ✓"}
    assert api.put("/v1/clipboard", json={"text": 5}, headers=h).status_code == 400


def test_exec_and_launch(api, tmp_path):
    h = auth(api, "exec")
    r = api.post("/v1/exec", headers=h, json={"argv": ["sh", "-c", "echo $DISPLAY; echo err >&2; exit 3"]})
    body = r.json()
    assert r.status_code == 200 and body["exit_code"] == 3 and body["stderr"] == "err\n"
    assert body["stdout"].strip() == api.app_.state.service.display
    r = api.post("/v1/exec", headers=h, json={"command": "cat", "stdin": "piped"})
    assert r.json()["stdout"] == "piped"
    r = api.post("/v1/exec", headers=h, json={"argv": ["sleep", "5"], "timeout": 0.2})
    assert r.json()["timed_out"] is True
    assert api.post("/v1/exec", headers=h, json={"argv": ["/no/such/binary"]}).status_code == 400
    r = api.post("/v1/exec", headers=h, json={"argv": ["true"], "cwd": "/no/such/dir"})
    assert r.status_code == 400 and "not a directory" in r.json()["error"]["message"]
    marker = tmp_path / "launched"
    r = api.post("/v1/launch", headers=h, json={"argv": ["sh", "-c", f"touch {marker}"]})
    assert r.status_code == 200 and r.json()["pid"] > 0
    import time

    for _ in range(50):
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.exists()


def test_a11y_endpoint(api):
    r = api.get("/v1/a11y", headers=auth(api, "observer"))
    if importlib.util.find_spec("gi") is None:
        assert r.status_code == 503 and r.json()["error"]["code"] == "A11Y_UNAVAILABLE"
    else:  # pragma: no cover - depends on the host
        assert r.status_code in (200, 404, 503)


def test_takeover_flow(api):
    agent, human = auth(api, "agent"), auth(api, "human")
    api.post("/v1/screenshot", headers=agent)
    assert api.post("/v1/takeover", json={"by": "alice"}, headers=agent).status_code == 403
    r = api.post("/v1/takeover", json={"by": "alice", "reason": "2FA prompt"}, headers=human)
    assert r.status_code == 200 and r.json()["held"] and r.json()["by"] == "alice"
    assert api.get("/v1/takeover", headers=agent).json()["held"] is True
    r = api.post("/v1/actions", json={"actions": [{"type": "key", "keys": "a"}]}, headers=agent)
    assert r.status_code == 409 and r.json()["error"]["code"] == "HUMAN_IN_CONTROL"
    # Observation still works.
    assert api.post("/v1/screenshot", headers=agent).status_code == 200
    r = api.delete("/v1/takeover", headers=human)
    assert r.status_code == 200 and r.json()["changed"] and r.json()["epoch"] == 2
    entries = api.get("/v1/audit?limit=100", headers=human).json()["entries"]
    changes = [e["change"] for e in entries if e["event"] == "lease"]
    assert changes == ["takeover", "handback"]
    assert any(
        e["event"] == "action" and e["result"] == "HUMAN_IN_CONTROL" and e["principal"] == "agent" for e in entries
    )
    assert api.get("/v1/audit", headers=agent).status_code == 403


def test_ui_page(api):
    r = api.get("/ui")
    assert r.status_code == 200 and "Take over" in r.text and "localStorage" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    r = api.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/ui"


def test_unix_transport_is_trusted(tmp_path, xvfb):
    cfg = make_config(tmp_path, display=xvfb.display)
    app, _, _ = build(cfg)
    with TestClient(Tagged(app, "unix")) as client:
        r = client.get("/v1/status")
        assert r.status_code == 200
        r = client.post("/v1/takeover", json={})
        assert r.json()["by"].startswith("unix:uid=")
        client.delete("/v1/takeover")


def test_unavailable_display_degrades_cleanly(tmp_path):
    cfg = make_config(tmp_path, display=":89")  # nothing listens there
    store = TokenStore(cfg.tokens_path)
    h = {"Authorization": f"Bearer {store.create('a', ['admin'])}"}
    app, _, _ = build(cfg)
    with TestClient(app) as client:
        body = client.get("/v1/status", headers=h).json()
        assert body["display_ok"] is False and body["screen"] is None
        r = client.post("/v1/screenshot", headers=h)
        assert r.status_code == 503 and r.json()["error"]["code"] == "DISPLAY_UNAVAILABLE"
        r = client.post("/v1/actions", headers=h, json={"actions": [{"type": "key", "keys": "a"}]})
        assert r.status_code == 503
