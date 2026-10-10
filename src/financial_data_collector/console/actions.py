"""Starting a sync from the console: one `fdc` subprocess at a time, its output in data/console-last-run.log.
The launcher only starts and watches the process; what the run did is read back from the database and the
heartbeat like any other run. Quitting the console does not stop a launched sync."""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from ..heartbeat import Heartbeat, parse_stamp

LAUNCH_SLACK = timedelta(seconds=5)     # a heartbeat begun this close before our start is still ours (clock skew)

LOG_NAME = "console-last-run.log"


@dataclass(frozen=True)
class LaunchState:
    kind: str                           # idle | queued | running | finished | failed
    step: str | None = None             # None: the whole sync
    pid: int | None = None
    exit_code: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str = ""

    @property
    def what(self) -> str:
        return "sync" if self.step is None else f"step {self.step}"


class Launcher:
    def __init__(self, root: Path, *, python: str = sys.executable, popen: Callable = subprocess.Popen,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.root = Path(root)
        self._python, self._popen, self._now = python, popen, now
        self._process = None
        self._step: str | None = None
        self._started_at: datetime | None = None
        self._finished_at: datetime | None = None
        self._failed = ""

    @property
    def log_path(self) -> Path:
        return self.root / "data" / LOG_NAME

    def command(self, step: str | None) -> list[str]:
        cmd = [self._python, "-m", "financial_data_collector", "--root", str(self.root), "sync", "--origin", "console"]
        if step:
            cmd += ["--only", step]
        return cmd

    @property
    def busy(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self, step: str | None = None) -> LaunchState:
        if self.busy:
            return LaunchState("failed", step, error="the console's own run is still going")
        self.clear()
        self._step, self._started_at = step, self._now()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            log = open(self.log_path, "w", encoding="utf-8")
        except OSError as e:
            self._failed = f"cannot write {self.log_path}: {e}"
            return self.state(None)
        try:
            kwargs: dict = {"cwd": str(self.root), "stdout": log, "stderr": subprocess.STDOUT,
                            "stdin": subprocess.DEVNULL}
            if sys.platform == "win32":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            else:
                kwargs["start_new_session"] = True
            self._process = self._popen(self.command(step), **kwargs)
        except OSError as e:
            self._failed = f"could not start: {e}"
            log.write(self._failed + "\n")
        finally:
            log.close()
        return self.state(None)

    def state(self, heartbeat: Heartbeat | None) -> LaunchState:
        if self._failed:
            return LaunchState("failed", self._step, started_at=self._started_at, error=self._failed)
        if self._process is None:
            return LaunchState("idle")
        code = self._process.poll()
        if code is None:
            return LaunchState("running" if self._is_ours(heartbeat) else "queued", self._step, self._process.pid,
                               started_at=self._started_at)
        if self._finished_at is None:
            self._finished_at = self._now()
        return LaunchState("finished", self._step, self._process.pid, code, self._started_at, self._finished_at)

    def _is_ours(self, hb: Heartbeat | None) -> bool:
        """The heartbeat of the run this launcher started. By pid when it matches; a venv's python.exe on Windows is
        a launcher whose child carries another pid, so a console-origin heartbeat begun at or after this launch
        counts too (the console starts one run at a time)."""
        if hb is None or self._process is None:
            return False
        if hb.pid == self._process.pid:
            return True
        started = parse_stamp(hb.started_at)
        return (hb.origin == "console" and started is not None and self._started_at is not None
                and started >= self._started_at - LAUNCH_SLACK)

    def clear(self) -> None:
        """Forget a finished or failed launch (a running one stays)."""
        if self.busy:
            return
        self._process, self._step, self._started_at, self._finished_at, self._failed = None, None, None, None, ""
