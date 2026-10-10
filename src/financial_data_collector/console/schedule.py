"""The operating system's registration of the sync task: what the schedule panel shows. On Windows it comes from
Task Scheduler through PowerShell; elsewhere there is nothing to read and the panel says so."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Schedule:
    name: str
    state: str
    last_run: str | None          # ISO local time, or None when the task never ran
    last_result: int | None
    next_run: str | None
    triggers: tuple[str, ...]     # humanized: "at logon +20 min", "daily 08:30", "every 12 h"
    log_path: Path | None


@dataclass(frozen=True)
class ScheduleResult:
    schedule: Schedule | None
    reason: str = ""              # why there is no schedule, when there is none
