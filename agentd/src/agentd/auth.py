"""Bearer tokens (stored as sha256 hashes) and scopes."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import tempfile
import threading
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from agentd.errors import AgentdError

SCOPES = ("observe", "input", "exec", "files", "takeover", "admin")
TOKEN_PREFIX = "agd_"
_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")


@dataclass(frozen=True)
class Principal:
    name: str
    scopes: frozenset[str]
    transport: str  # tcp | unix | stdio | internal

    def has(self, scope: str) -> bool:
        return "admin" in self.scopes or scope in self.scopes

    def require(self, scope: str) -> None:
        if not self.has(scope):
            raise AgentdError("FORBIDDEN", f"token {self.name!r} lacks the {scope!r} scope")


def trusted(transport: str, name: str | None = None) -> Principal:
    """Principal for transports that are trusted by construction (stdio, peer-checked socket)."""
    return Principal(name or transport, frozenset({"admin"}), transport)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def parse_scopes(text: str | list[str]) -> list[str]:
    items = text.split(",") if isinstance(text, str) else list(text)
    scopes = [s.strip() for s in items if s.strip()]
    bad = [s for s in scopes if s not in SCOPES]
    if bad or not scopes:
        raise ValueError(f"invalid scopes {bad or scopes}; choose from {', '.join(SCOPES)}")
    return sorted(set(scopes), key=SCOPES.index)


@dataclass
class TokenEntry:
    name: str
    sha256: str
    scopes: list[str]
    created: str = ""


@dataclass
class TokenStore:
    """tokens.toml holds `[[token]]` tables with name, sha256, scopes, created.

    The file is re-read when it changes (inode/mtime/size), so `agentd token create`
    takes effect without restarting the server. `env_token` (AGENTD_TOKEN)
    is an extra admin token that is never written to disk.
    """

    path: Path
    env_token: str | None = None
    _entries: list[TokenEntry] = field(default_factory=list)
    _mtime: tuple[int, int, int] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _load(self) -> None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._entries, self._mtime = [], None
            return
        # Writes replace the file, so the inode changes even within one mtime tick.
        mtime = (st.st_ino, st.st_mtime_ns, st.st_size)
        if mtime == self._mtime:
            return
        with self.path.open("rb") as fh:
            data = tomllib.load(fh)
        entries = []
        for raw in data.get("token", []):
            try:
                entries.append(
                    TokenEntry(
                        name=str(raw["name"]),
                        sha256=str(raw["sha256"]).lower(),
                        scopes=parse_scopes(raw["scopes"]),
                        created=str(raw.get("created", "")),
                    )
                )
            except (KeyError, ValueError):
                continue  # a malformed entry must not disable the others
        self._entries, self._mtime = entries, mtime

    def entries(self) -> list[TokenEntry]:
        with self._lock:
            self._load()
            return list(self._entries)

    def verify(self, token: str, transport: str = "tcp") -> Principal | None:
        if not token:
            return None
        digest = hash_token(token)
        match: Principal | None = None
        if self.env_token and hmac.compare_digest(digest, hash_token(self.env_token)):
            match = Principal("AGENTD_TOKEN", frozenset({"admin"}), transport)
        for entry in self.entries():
            # Compare against every entry so timing does not reveal which one matched.
            if hmac.compare_digest(digest, entry.sha256) and match is None:
                match = Principal(entry.name, frozenset(entry.scopes), transport)
        return match

    def create(self, name: str, scopes: list[str], token: str | None = None) -> str:
        if not _NAME_RE.match(name):
            raise ValueError("token name must be 1-64 chars of [A-Za-z0-9_.@-]")
        scopes = parse_scopes(scopes)
        token = token or TOKEN_PREFIX + secrets.token_urlsafe(32)
        if len(token) < 16:
            raise ValueError("tokens must be at least 16 characters")
        with self._lock:
            self._mtime = None
            self._load()
            if any(e.name == name for e in self._entries):
                raise ValueError(f"a token named {name!r} already exists; revoke it first")
            created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._entries.append(TokenEntry(name, hash_token(token), scopes, created))
            self._write()
        return token

    def revoke(self, name: str) -> bool:
        with self._lock:
            self._mtime = None
            self._load()
            before = len(self._entries)
            self._entries = [e for e in self._entries if e.name != name]
            if len(self._entries) == before:
                return False
            self._write()
            return True

    def _write(self) -> None:
        def q(s: str) -> str:
            return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

        lines = ["# agentd bearer tokens (sha256 of the token). Managed by `agentd token`.", ""]
        for e in self._entries:
            lines += [
                "[[token]]",
                f"name = {q(e.name)}",
                f"sha256 = {q(e.sha256)}",
                "scopes = [" + ", ".join(q(s) for s in e.scopes) + "]",
                f"created = {q(e.created)}",
                "",
            ]
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tokens.")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write("\n".join(lines))
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self._mtime = None


def bearer_from_header(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()
