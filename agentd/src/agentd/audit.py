"""Append-only JSONL audit log with size-based rotation."""

from __future__ import annotations

import json
import os
import sys
import threading
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TEXT_LIMIT = 200


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class AuditLog:
    """One JSON object per line. When the file would exceed `max_bytes` it is
    rotated to `.1` (older copies shift up to `.<backups>`).

    The last few hundred entries are also kept in memory for `/v1/audit`.
    """

    def __init__(self, path: Path | None, max_bytes: int = 10 * 1024 * 1024, backups: int = 5, keep: int = 500) -> None:
        self.path = path
        self.max_bytes = max(4096, max_bytes)
        self.backups = max(0, backups)
        self.recent: deque[dict[str, Any]] = deque(maxlen=keep)
        self._lock = threading.Lock()
        self._warned = False
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def write(self, event: str, **fields: Any) -> dict[str, Any]:
        entry = {"ts": now_iso(), "event": event, **{k: v for k, v in fields.items() if v is not None}}
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
        with self._lock:
            self.recent.append(entry)
            if self.path is None:
                return entry
            try:
                self._rotate_if_needed(len(line.encode()))
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.write(fd, line.encode())
                finally:
                    os.close(fd)
            except OSError as exc:
                # Auditing must not take the daemon down; complain once on stderr.
                if not self._warned:
                    self._warned = True
                    print(f"agentd: audit log {self.path} unwritable: {exc}", file=sys.stderr, flush=True)
        return entry

    def _rotate_if_needed(self, incoming: int) -> None:
        assert self.path is not None
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size + incoming <= self.max_bytes:
            return
        if self.backups == 0:
            self.path.unlink(missing_ok=True)
            return
        for i in range(self.backups - 1, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{i}")
            if src.exists():
                os.replace(src, self.path.with_name(f"{self.path.name}.{i + 1}"))
        os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self.recent)
        return items[-max(0, limit) :]


def sanitize_action(action: dict[str, Any], log_text: bool = True) -> dict[str, Any]:
    """Copy of a canonical action that is safe and compact to log."""
    out: dict[str, Any] = {}
    for key, value in action.items():
        if key == "text" and isinstance(value, str):
            if log_text:
                out["text"] = value[:TEXT_LIMIT] + ("…" if len(value) > TEXT_LIMIT else "")
            out["text_len"] = len(value)
        elif key == "path" and isinstance(value, list) and len(value) > 8:
            out["path"] = [*value[:4], "…", *value[-2:]]
        else:
            out[key] = value
    return out
