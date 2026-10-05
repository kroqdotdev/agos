"""Typed errors shared by every transport (REST, MCP, CLI)."""

from __future__ import annotations

from typing import Any

STATUS = {
    "UNAUTHORIZED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "HUMAN_IN_CONTROL": 409,
    "STALE_FRAME": 409,
    "CONFIRMATION_REQUIRED": 409,
    "INVALID_ACTION": 400,
    "DISPLAY_UNAVAILABLE": 503,
    "A11Y_UNAVAILABLE": 503,
    "INTERNAL": 500,
}


class AgentdError(Exception):
    """An error with a stable code from the spec's error table.

    `extra` carries structured payload that travels with the error, e.g. the
    fresh screenshot returned alongside STALE_FRAME.
    """

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        if code not in STATUS:
            raise ValueError(f"unknown error code {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    @property
    def status(self) -> int:
        return STATUS[self.code]

    def to_json(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        body.update(self.extra)
        return body

    @classmethod
    def from_json(cls, body: dict[str, Any], status: int = 500) -> AgentdError:
        err = body.get("error") if isinstance(body, dict) else None
        if not isinstance(err, dict) or err.get("code") not in STATUS:
            return cls("INTERNAL", f"unexpected response (HTTP {status}): {str(body)[:200]}")
        extra = {k: v for k, v in body.items() if k != "error"}
        return cls(err["code"], str(err.get("message", "")), **extra)


def invalid(message: str) -> AgentdError:
    return AgentdError("INVALID_ACTION", message)
