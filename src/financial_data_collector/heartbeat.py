"""The running sync's heartbeat: a small JSON file beside the database that says which run is in progress,
how far it is and who started it. `run_sync` keeps it current and removes it when it ends; `fdc console`
reads it, and a sync that wants its turn waits on it. Nothing private is in it."""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

FILENAME = "sync.heartbeat.json"
MAX_AGE = timedelta(hours=3)          # the scheduled task's own execution limit is two
STAMP = "%Y-%m-%dT%H:%M:%SZ"
ATTEMPTS = 3                          # a reader holding the file for a moment (Windows sharing violation) gets retried
RETRY_PAUSE = 0.05


def stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(STAMP)


def parse_stamp(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, STAMP).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


@dataclass
class Heartbeat:
    run_id: str
    pid: int
    origin: str
    started_at: str
    updated_at: str
    step: str = ""
    detail: str = ""
    steps_done: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> "Heartbeat | None":
        try:
            d = json.loads(text)
            return cls(run_id=str(d["run_id"]), pid=int(d["pid"]), origin=str(d["origin"]),
                       started_at=str(d["started_at"]), updated_at=str(d["updated_at"]),
                       step=str(d.get("step", "")), detail=str(d.get("detail", "")),
                       steps_done=[str(s) for s in d.get("steps_done", [])])
        except (ValueError, TypeError, KeyError, AttributeError):
            return None


def pid_alive(pid: int) -> bool:
    """Is a process with this id running? Never os.kill(pid, 0) on Windows: there it terminates the process."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read(path: Path) -> Heartbeat | None:
    """The heartbeat on disk, or None when there is none or it is unreadable (half-written, malformed)."""
    try:
        return Heartbeat.from_json(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return None


def is_alive(hb: Heartbeat, *, now: datetime, alive: Callable[[int], bool] | None = None,
             max_age: timedelta = MAX_AGE) -> bool:
    """Alive: its process runs and it was updated recently. Anything else is a run that died without cleaning up."""
    alive = alive or pid_alive
    updated = parse_stamp(hb.updated_at)
    if updated is None or now - updated >= max_age:
        return False
    return alive(hb.pid)


class Writer:
    """Keeps one run's heartbeat current. Writes are atomic (temp file + replace) and throttled to one a second,
    except a step change, which always writes."""

    def __init__(self, path: Path, run_id: str, origin: str, *, pid: int | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 clock: Callable[[], float] = time.monotonic, min_interval: float = 1.0,
                 pause: Callable[[float], None] = time.sleep):
        self.path = Path(path)
        first = stamp(now())
        self.hb = Heartbeat(run_id=run_id, pid=pid if pid is not None else os.getpid(), origin=origin,
                            started_at=first, updated_at=first)
        self._now, self._clock, self._min, self._pause = now, clock, min_interval, pause
        self._last_write: float | None = None

    def start(self) -> None:
        self._write()

    def update(self, step: str, detail: str) -> None:
        changed = step != self.hb.step
        if changed and self.hb.step:
            self.hb.steps_done.append(self.hb.step)
        self.hb.step, self.hb.detail = step, detail
        if changed or self._last_write is None or self._clock() - self._last_write >= self._min:
            self._write()

    def finish(self) -> None:
        """Remove the file, unless another run has taken it over in the meantime. Never raises: the heartbeat is
        informational and must not turn a finished sync into a failure."""
        current = read(self.path)
        if current is not None and current.pid != self.hb.pid:
            return
        for attempt in range(ATTEMPTS):
            try:
                self.path.unlink()
                return
            except FileNotFoundError:
                return
            except OSError:
                if attempt < ATTEMPTS - 1:
                    self._pause(RETRY_PAUSE)

    def _write(self) -> None:
        """Atomic: temp file then replace. On Windows a reader holding the file for a moment makes the replace
        fail (sharing violation); after ATTEMPTS the write is dropped, never raised into the sync."""
        self.hb.updated_at = stamp(self._now())
        tmp = self.path.with_name(f"{self.path.name}.{self.hb.pid}.tmp")
        for attempt in range(ATTEMPTS):
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(self.hb.to_json(), encoding="utf-8")
                os.replace(tmp, self.path)
                self._last_write = self._clock()
                return
            except OSError:
                if attempt < ATTEMPTS - 1:
                    self._pause(RETRY_PAUSE)
        try:
            tmp.unlink()
        except OSError:
            pass


def wait_for_turn(path: Path, *, now: datetime, sleep: Callable[[float], None],
                  alive: Callable[[int], bool] | None = None, timeout: float = 1800.0, poll: float = 5.0) -> float:
    """Block while another sync's heartbeat is alive. Returns the seconds waited. Gives up after `timeout`
    seconds and lets the caller proceed: a wedged run must never stop syncing for good."""
    alive = alive or pid_alive
    waited = 0.0
    while waited < timeout:
        hb = read(path)
        if hb is None or hb.pid == os.getpid() or not is_alive(hb, now=now + timedelta(seconds=waited), alive=alive):
            return waited
        sleep(poll)
        waited += poll
    return waited
