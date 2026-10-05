"""Process helpers: detached launches, bounded exec, lease hooks."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from typing import Any

from agentd.errors import AgentdError

OUTPUT_LIMIT = 1024 * 1024
_children: set[asyncio.Task[Any]] = set()


async def launch(argv: list[str], env: dict[str, str], cwd: str | None = None) -> int:
    """Start a detached process (own session, no stdio) and reap it in the background."""
    _check_argv(argv, cwd)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=env,
            cwd=cwd or os.path.expanduser("~"),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        raise AgentdError("INVALID_ACTION", f"command not found: {argv[0]}") from None
    except (PermissionError, NotADirectoryError) as exc:
        raise AgentdError("INVALID_ACTION", f"cannot start {argv[0]}: {exc}") from None
    task = asyncio.get_running_loop().create_task(proc.wait())
    _children.add(task)
    task.add_done_callback(_children.discard)
    return proc.pid


async def run(
    argv: list[str],
    env: dict[str, str],
    *,
    timeout: float,
    cwd: str | None = None,
    stdin: str | None = None,
    limit: int = OUTPUT_LIMIT,
) -> dict[str, Any]:
    _check_argv(argv, cwd)
    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=env,
            cwd=cwd or os.path.expanduser("~"),
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError:
        raise AgentdError("INVALID_ACTION", f"command not found: {argv[0]}") from None
    except (PermissionError, NotADirectoryError) as exc:
        raise AgentdError("INVALID_ACTION", f"cannot start {argv[0]}: {exc}") from None
    timed_out = False
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin.encode() if stdin is not None else None), timeout)
    except TimeoutError:
        timed_out = True
        _kill_group(proc.pid)
        out, err = await proc.communicate()
    except BaseException:
        _kill_group(proc.pid)
        raise

    def clip(b: bytes) -> tuple[str, bool]:
        return b[:limit].decode(errors="replace"), len(b) > limit

    stdout, out_trunc = clip(out or b"")
    stderr, err_trunc = clip(err or b"")
    return {
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "truncated": out_trunc or err_trunc,
        "duration_ms": round((time.monotonic() - t0) * 1000),
    }


def _kill_group(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def _check_argv(argv: Any, cwd: Any = None) -> None:
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise AgentdError("INVALID_ACTION", "argv must be a non-empty list of strings")
    if not argv[0]:
        raise AgentdError("INVALID_ACTION", "argv[0] must not be empty")
    if cwd is not None and (not isinstance(cwd, str) or not os.path.isdir(cwd)):
        raise AgentdError("INVALID_ACTION", f"cwd {cwd!r} is not a directory")
