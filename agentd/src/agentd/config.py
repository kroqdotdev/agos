"""Configuration: defaults < config.toml < AGENTD_* env < CLI flags."""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_type_hints

ENV_PREFIX = "AGENTD_"
# Env vars with the prefix that are not config keys.
ENV_RESERVED = {"AGENTD_TOKEN", "AGENTD_CONFIG"}


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / fallback


def default_config_path() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "agentd" / "config.toml"


def _default_socket() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return str(Path(runtime) / "agentd.sock")


def _default_audit() -> str:
    return str(_xdg("XDG_STATE_HOME", ".local/state") / "agentd" / "audit.jsonl")


def _default_tokens() -> str:
    return str(_xdg("XDG_CONFIG_HOME", ".config") / "agentd" / "tokens.toml")


@dataclass
class Config:
    display: str = ":1"
    listen: str = "127.0.0.1:8765"  # "" disables TCP
    socket: str = field(default_factory=_default_socket)  # "" disables the Unix socket
    tokens_file: str = field(default_factory=_default_tokens)
    audit_log: str = field(default_factory=_default_audit)  # "" disables auditing
    audit_max_bytes: int = 10 * 1024 * 1024
    audit_backups: int = 5
    audit_text: bool = True  # log typed text (truncated); False logs only its length
    max_image_long_edge: int = 2576
    max_image_pixels: int = 3_750_000
    default_format: str = "png"
    jpeg_quality: int = 85
    webp_quality: int = 85
    draw_cursor: bool = False
    settle_ms: int = 300
    settle_timeout_ms: int = 3000
    stable_change_fraction: float = 0.0005
    on_takeover: list[str] = field(default_factory=list)
    on_handback: list[str] = field(default_factory=list)
    viewer_url: str = ""
    mcp_mode: str = "auto"  # auto | local | remote (see `agentd mcp`)
    browser_command: list[str] = field(default_factory=lambda: ["agos-browser"])
    search_url: str = "https://www.google.com/"
    cdp_url: str = "http://127.0.0.1:9222"
    exec_timeout_max: float = 600.0
    a11y_max_nodes: int = 400
    a11y_max_depth: int = 12

    def validate(self) -> None:
        if self.default_format not in ("png", "jpeg", "webp"):
            raise ValueError(f"default_format must be png, jpeg or webp, not {self.default_format!r}")
        if self.mcp_mode not in ("auto", "local", "remote"):
            raise ValueError(f"mcp_mode must be auto, local or remote, not {self.mcp_mode!r}")
        if self.max_image_long_edge < 64 or self.max_image_pixels < 64 * 64:
            raise ValueError("image limits are too small")
        if self.listen:
            parse_listen(self.listen)

    @property
    def audit_path(self) -> Path | None:
        return Path(self.audit_log).expanduser() if self.audit_log else None

    @property
    def tokens_path(self) -> Path:
        return Path(self.tokens_file).expanduser()


def parse_listen(listen: str) -> tuple[str, int]:
    host, sep, port = listen.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"listen must be host:port, not {listen!r}")
    return host.strip("[]") or "127.0.0.1", int(port)


def _coerce(name: str, typ: Any, value: Any, source: str) -> Any:
    origin = getattr(typ, "__origin__", None)
    if typ is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("1", "true", "yes", "on"):
            return True
        if isinstance(value, str) and value.lower() in ("0", "false", "no", "off"):
            return False
    elif typ is int and not isinstance(value, bool):
        if isinstance(value, int) or (isinstance(value, str) and value.lstrip("-").isdigit()):
            return int(value)
    elif typ is float and not isinstance(value, bool):
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    elif typ is str:
        if isinstance(value, str):
            return value
    elif origin is list:
        if isinstance(value, str):
            value = value.strip()
            value = json.loads(value) if value.startswith("[") else shlex.split(value)
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return list(value)
    raise ValueError(f"{source}: {name} has an invalid value {value!r}")


def load_config(
    path: str | os.PathLike[str] | None = None,
    overrides: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> Config:
    env = dict(os.environ) if env is None else env
    hints = get_type_hints(Config)
    names = {f.name for f in dataclasses.fields(Config)}
    values: dict[str, Any] = {}

    explicit = path is not None or "AGENTD_CONFIG" in env
    cfg_path = Path(path or env.get("AGENTD_CONFIG") or default_config_path()).expanduser()
    if cfg_path.exists():
        with cfg_path.open("rb") as fh:
            data = tomllib.load(fh)
        for key, value in data.items():
            if key not in names:
                raise ValueError(f"{cfg_path}: unknown key {key!r}")
            values[key] = _coerce(key, hints[key], value, str(cfg_path))
    elif explicit:
        raise FileNotFoundError(f"config file {cfg_path} does not exist")

    for key, value in env.items():
        if not key.startswith(ENV_PREFIX) or key in ENV_RESERVED:
            continue
        name = key[len(ENV_PREFIX) :].lower()
        if name in names:
            values[name] = _coerce(name, hints[name], value, key)

    for key, value in (overrides or {}).items():
        if value is not None:
            values[key] = _coerce(key, hints[key], value, "command line")

    cfg = Config(**values)
    cfg.validate()
    return cfg
