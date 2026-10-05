import json
import os
import stat

import pytest

from agentd.audit import AuditLog, sanitize_action
from agentd.auth import Principal, TokenStore, bearer_from_header, hash_token, parse_scopes
from agentd.config import load_config
from agentd.errors import AgentdError

# ------------------------------------------------------------------ config


def test_config_precedence(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        'display = ":5"\nlisten = "127.0.0.1:9000"\nsettle_ms = 100\n'
        'on_handback = ["/usr/lib/agos/viewer-perm", "view"]\n'
    )
    env = {
        "AGENTD_SETTLE_MS": "250",
        "AGENTD_LISTEN": "0.0.0.0:9100",
        "AGENTD_TOKEN": "ignored",
        "AGENTD_ON_TAKEOVER": '["/bin/hook", "control"]',
        "AGENTD_DRAW_CURSOR": "yes",
    }
    cfg = load_config(cfg_file, {"display": ":7"}, env=env)
    assert cfg.display == ":7"  # CLI beats file
    assert cfg.settle_ms == 250  # env beats file
    assert cfg.listen == "0.0.0.0:9100"
    assert cfg.on_handback == ["/usr/lib/agos/viewer-perm", "view"]
    assert cfg.on_takeover == ["/bin/hook", "control"]
    assert cfg.draw_cursor is True
    assert cfg.max_image_long_edge == 2576 and cfg.max_image_pixels == 3_750_000


def test_config_rejects_unknown_keys_and_bad_values(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text("nope = 1\n")
    with pytest.raises(ValueError):
        load_config(f, env={})
    f.write_text('default_format = "gif"\n')
    with pytest.raises(ValueError):
        load_config(f, env={})
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "missing.toml", env={})


def test_config_defaults_follow_xdg(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path / "cfg")}
    cfg = load_config(None, env=env)
    assert cfg.display == ":1" and cfg.listen == "127.0.0.1:8765"
    assert cfg.socket.endswith("agentd.sock")


# -------------------------------------------------------------------- auth


def test_tokens_are_stored_hashed(tmp_path):
    store = TokenStore(tmp_path / "tokens.toml")
    token = store.create("ci", ["observe", "input"])
    text = (tmp_path / "tokens.toml").read_text()
    assert token not in text
    assert hash_token(token) in text
    assert stat.S_IMODE(os.stat(tmp_path / "tokens.toml").st_mode) == 0o600
    p = store.verify(token)
    assert p is not None and p.name == "ci" and p.scopes == {"observe", "input"}
    assert store.verify(token + "x") is None
    assert store.verify("") is None


def test_scopes_and_admin(tmp_path):
    store = TokenStore(tmp_path / "tokens.toml")
    obs = store.verify(store.create("watcher", ["observe"]))
    adm = store.verify(store.create("root", ["admin"]))
    assert obs.has("observe") and not obs.has("input")
    with pytest.raises(AgentdError) as e:
        obs.require("input")
    assert e.value.code == "FORBIDDEN" and e.value.status == 403
    for scope in ("observe", "input", "exec", "files", "takeover", "admin"):
        assert adm.has(scope)
    with pytest.raises(ValueError):
        parse_scopes("observe,root")


def test_store_reloads_and_revokes(tmp_path):
    path = tmp_path / "tokens.toml"
    server_view = TokenStore(path)
    assert server_view.verify("agd_whatever") is None
    cli_view = TokenStore(path)
    token = cli_view.create("late", ["observe"])
    assert server_view.verify(token) is not None  # picked up without restart
    assert cli_view.revoke("late")
    assert server_view.verify(token) is None
    with pytest.raises(ValueError):
        cli_view.create("dup", ["observe"], "x" * 20)
        cli_view.create("dup", ["observe"], "y" * 20)


def test_env_token_is_admin(tmp_path):
    store = TokenStore(tmp_path / "none.toml", env_token="s3cret-admin-token")
    p = store.verify("s3cret-admin-token")
    assert p is not None and p.has("exec")


def test_import_existing_token(tmp_path):
    store = TokenStore(tmp_path / "t.toml")
    store.create("admin", ["admin"], "agd_from_firstboot_secret_value")
    assert store.verify("agd_from_firstboot_secret_value").name == "admin"


def test_bearer_header_parsing():
    assert bearer_from_header("Bearer abc") == "abc"
    assert bearer_from_header("bearer   abc ") == "abc"
    assert bearer_from_header("Basic abc") is None
    assert bearer_from_header(None) is None


def test_principal_is_hashable():
    assert Principal("a", frozenset({"observe"}), "tcp") == Principal("a", frozenset({"observe"}), "tcp")


# ------------------------------------------------------------------- audit


def test_audit_writes_jsonl_and_rotates(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, max_bytes=4096, backups=2)
    for i in range(200):
        log.write("action", principal="t", i=i, pad="x" * 50)
    assert path.exists() and (tmp_path / "audit.jsonl.1").exists() and (tmp_path / "audit.jsonl.2").exists()
    assert not (tmp_path / "audit.jsonl.3").exists()
    for f in (path, tmp_path / "audit.jsonl.1"):
        assert f.stat().st_size <= 4096
        for line in f.read_text().splitlines():
            entry = json.loads(line)
            assert entry["event"] == "action" and entry["ts"].endswith("Z")
    last = json.loads(path.read_text().splitlines()[-1])
    assert last["i"] == 199
    assert [e["i"] for e in log.tail(3)] == [197, 198, 199]


def test_audit_text_is_truncated_or_dropped():
    long = "p" * 500
    entry = sanitize_action({"type": "type", "text": long})
    assert len(entry["text"]) < 210 and entry["text_len"] == 500
    entry = sanitize_action({"type": "type", "text": "hunter2"}, log_text=False)
    assert "text" not in entry and entry["text_len"] == 7


def test_audit_without_path_keeps_memory_only():
    log = AuditLog(None)
    log.write("x", a=1)
    assert log.tail(1)[0]["a"] == 1
