"""Shared fixtures. Integration tests use a PRIVATE Xvfb on :90-:99, never :1."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

# Nothing in the test process may default to the developer's live desktop.
os.environ.pop("DISPLAY", None)
for _key in [k for k in os.environ if k.startswith("AGENTD_")]:
    os.environ.pop(_key)

HERE = Path(__file__).parent


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _free_display() -> int:
    for n in range(90, 100):
        if not Path(f"/tmp/.X11-unix/X{n}").exists() and not Path(f"/tmp/.X{n}-lock").exists():
            return n
    raise RuntimeError("no free X display in :90-:99")


class XServer:
    def __init__(self, width: int = 1280, height: int = 800) -> None:
        if shutil.which("Xvfb") is None:
            pytest.skip("Xvfb is not installed")
        self.num = _free_display()
        self.display = f":{self.num}"
        assert self.display != ":1"
        self.proc = subprocess.Popen(
            ["Xvfb", self.display, "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp", "-noreset"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        from Xlib import display as xdisplay

        deadline = time.monotonic() + 10
        while True:
            try:
                xdisplay.Display(self.display).close()
                break
            except Exception:
                if self.proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(f"Xvfb {self.display} did not start") from None
                time.sleep(0.05)

    def env(self) -> dict[str, str]:
        return {**os.environ, "DISPLAY": self.display}

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture(scope="session")
def xvfb() -> Iterator[XServer]:
    server = XServer()
    yield server
    server.stop()


@pytest.fixture
def fresh_xvfb() -> Iterator[XServer]:
    """A throwaway server for tests that change the screen geometry."""
    server = XServer()
    yield server
    server.stop()


class XWindow:
    """A python-xlib window in a helper process that reports keys and clicks."""

    def __init__(self, display: str, title: str = "agentd-test", geometry: str = "100,100,400,300") -> None:
        import queue
        import threading

        self.proc = subprocess.Popen(
            [sys.executable, str(HERE / "xwin_helper.py"), display, title, geometry],
            stdout=subprocess.PIPE,
            text=True,
        )
        self._queue: queue.Queue[str] = queue.Queue()
        # A thread, not select(): buffered readline() would hide lines from select.
        threading.Thread(target=self._pump, daemon=True).start()
        line = self._queue.get(timeout=10)
        assert line.startswith("READY"), line
        self.id = int(line.split()[1])
        self.lines: list[str] = []

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._queue.put(line.rstrip("\n"))

    def read_events(self, until: float = 2.0, stop_after: int | None = None) -> list[str]:
        import queue

        deadline = time.monotonic() + until
        while time.monotonic() < deadline:
            try:
                self.lines.append(self._queue.get(timeout=0.05))
            except queue.Empty:
                continue
            if stop_after is not None and len(self.lines) >= stop_after:
                break
        return self.lines

    def chars(self) -> str:
        return "".join(line.split(" ", 2)[2] for line in self.lines if line.startswith("CHAR") and line.count(" ") >= 2)

    def typed(self, until: float = 3.0, expect: str | None = None) -> str:
        deadline = time.monotonic() + until
        while time.monotonic() < deadline:
            self.read_events(0.2)
            if expect is not None and self.chars() == expect:
                break
        return self.chars()

    def close(self) -> None:
        self.proc.terminate()
        self.proc.wait(5)


@pytest.fixture
def xwindow(xvfb: XServer) -> Iterator[XWindow]:
    win = XWindow(xvfb.display)
    yield win
    win.close()


def make_config(tmp_path: Path, display: str = ":99", **kw: object):
    from agentd.config import Config

    cfg = Config(
        display=display,
        listen="127.0.0.1:0",
        socket=str(tmp_path / "agentd.sock"),
        tokens_file=str(tmp_path / "tokens.toml"),
        audit_log=str(tmp_path / "audit.jsonl"),
    )
    for k, v in kw.items():
        setattr(cfg, k, v)
    cfg.validate()
    return cfg
