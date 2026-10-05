"""Provider adapters: native tool-call JSON in, canonical actions, native results out.

Each adapter parses a request into `Group`s (one per provider call, holding
one or more canonical actions). The service runs the groups in order as one
batch that stops at the first failure, then the adapter renders the outcome
in the provider's own result shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentd.core import Shot, StepResult
from agentd.errors import AgentdError


@dataclass
class Group:
    actions: list[dict[str, Any]] = field(default_factory=list)
    error: AgentdError | None = None  # the provider call could not be mapped
    special: str | None = None  # handled by the service, e.g. "open_web_browser"
    meta: dict[str, Any] = field(default_factory=dict)  # provider ids and names


@dataclass
class GroupOutcome:
    status: str  # ok | error | skipped
    error: AgentdError | None = None
    steps: list[StepResult] = field(default_factory=list)

    @property
    def last_shot(self) -> Shot | None:
        for step in reversed(self.steps):
            if step.shot is not None:
                return step.shot
        return None


@dataclass
class Outcome:
    groups: list[GroupOutcome]
    final: Shot | None = None  # screenshot after the batch (or the STALE_FRAME refresh)
    error: AgentdError | None = None

    @property
    def last_attempted(self) -> int:
        idx = -1
        for i, g in enumerate(self.groups):
            if g.status != "skipped":
                idx = i
        return idx


def coord(value: Any, what: str = "coordinate") -> tuple[float, float]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
    ):
        raise AgentdError("INVALID_ACTION", f"{what} must be [x, y]")
    return float(value[0]), float(value[1])
