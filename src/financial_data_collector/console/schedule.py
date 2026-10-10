"""The operating system's registration of the sync task: what the schedule panel shows. On Windows it comes from
Task Scheduler through PowerShell; elsewhere there is nothing to read and the panel says so."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


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


_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def default_log_path(env: Mapping[str, str] = os.environ) -> Path | None:
    """Where scripts/run_sync.ps1 appends its log; None when the file does not exist."""
    base = env.get("LOCALAPPDATA")
    if not base:
        return None
    p = Path(base) / "financial-data-collector" / "sync.log"
    return p if p.is_file() else None


def _duration(text: str) -> str:
    m = _DURATION.match(text or "")
    if not m or not any(m.groups()):
        return ""
    days, hours, minutes, seconds = (int(g) if g else 0 for g in m.groups())
    total = ((days * 24 + hours) * 60 + minutes) * 60 + seconds      # PT90M reads "1 h 30 min"
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} d")
    if hours:
        parts.append(f"{hours} h")
    if minutes:
        parts.append(f"{minutes} min")
    if seconds:
        parts.append(f"{seconds} s")
    return " ".join(parts)


def humanize_trigger(t: Mapping) -> str:
    kind = str(t.get("type", "")).replace("MSFT_Task", "").replace("Trigger", "")
    start = str(t.get("start") or "")
    clock = start[11:16] if len(start) >= 16 else ""
    if kind == "Logon":
        text = "at logon"
    elif kind == "Boot":
        text = "at startup"
    elif kind == "Daily":
        text = f"daily {clock}".strip()
    elif kind == "Weekly":
        text = f"weekly {clock}".strip()
    elif kind == "Time":
        text = f"once {start[:10]} {clock}".strip()
    else:
        text = kind or "trigger"
    delay = _duration(str(t.get("delay") or ""))
    if delay:
        text += f" +{delay}"
    interval = _duration(str(t.get("interval") or ""))
    if interval:
        text += f", every {interval}"
    if t.get("enabled") is False:
        text += " (disabled)"
    return text


def _date(text) -> str | None:
    if not text or not isinstance(text, str) or text[:4] < "2000":
        return None
    return text


def parse_tasks(text: str, log_path: Path | None) -> list[Schedule]:
    try:
        data = json.loads(text) if text.strip() else []
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    out = []
    for t in data:
        if not isinstance(t, dict):
            continue
        out.append(Schedule(
            name=str(t.get("name", "")), state=str(t.get("state", "")),
            last_run=_date(t.get("last_run")),
            last_result=int(t["last_result"]) if t.get("last_result") is not None else None,
            next_run=_date(t.get("next_run")),
            triggers=tuple(humanize_trigger(x) for x in t.get("triggers") or [] if isinstance(x, dict)),
            log_path=log_path,
        ))
    return out


def powershell_command(root: Path) -> str:
    needle = str(Path(root) / "scripts" / "run_sync.ps1").replace("'", "''")
    return (
        f"$needle = '{needle}'; "
        "$tasks = @(Get-ScheduledTask | Where-Object { (($_.Actions | ForEach-Object { $_.Arguments }) -join ' ') "
        "-like ('*' + $needle + '*') }); "
        "$out = @(foreach ($t in $tasks) { $i = $t | Get-ScheduledTaskInfo; [pscustomobject]@{ "
        "name = $t.TaskName; state = [string]$t.State; "
        "last_run = $(if ($i.LastRunTime) { $i.LastRunTime.ToString('s') } else { $null }); "
        "last_result = $i.LastTaskResult; "
        "next_run = $(if ($i.NextRunTime) { $i.NextRunTime.ToString('s') } else { $null }); "
        "triggers = @($t.Triggers | ForEach-Object { [pscustomobject]@{ type = $_.CimClass.CimClassName; "
        "start = [string]$_.StartBoundary; delay = [string]$_.Delay; interval = [string]$_.Repetition.Interval; "
        "enabled = [bool]$_.Enabled } }) } }); "
        "ConvertTo-Json -InputObject $out -Depth 5"
    )


def read_schedule(root: Path, *, run: Callable = subprocess.run, platform: str = sys.platform,
                  env: Mapping[str, str] = os.environ) -> ScheduleResult:
    """The sync task registered for this folder, found by the script its action runs."""
    if platform != "win32":
        return ScheduleResult(None, "no scheduler integration on this platform")
    try:
        done = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", powershell_command(root)],
                   capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return ScheduleResult(None, "scheduler: no answer in 10 s")
    except OSError as e:
        return ScheduleResult(None, f"scheduler: {e}")
    if done.returncode != 0:
        first = (done.stderr or done.stdout or "").strip().splitlines()
        return ScheduleResult(None, f"scheduler: {first[0] if first else 'exit ' + str(done.returncode)}")
    tasks = parse_tasks(done.stdout, default_log_path(env))
    if not tasks:
        return ScheduleResult(None, "no task runs scripts/run_sync.ps1 for this folder: "
                                    "register one with scripts/schedule_sync.ps1")
    return ScheduleResult(tasks[0], "")
