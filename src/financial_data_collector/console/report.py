"""Rich renderables for an Overview: the one-shot `fdc console --once`, and the row builders the live screen
shares, so both look the same."""
from __future__ import annotations

from datetime import datetime, tzinfo

from rich.table import Table
from rich.text import Text

from .. import ui
from ..sync import STEPS
from .actions import LaunchState
from .model import (
    Overview, RunStep, RunSummary, SourceRow, StepState, fmt_age, fmt_duration, local_time, parse_iso,
)
from .schedule import Schedule

MESSAGE_WIDTH = 110
STATE_STYLES = {"ok": "green", "present": "green", "empty": "dim", "off": "dim", "never": "dim",
                "missing": "red", "missing folder": "red"}
LETTERS = {"ok": ("o", "green"), "skipped": ("s", "yellow"), "error": ("e", "red"), "dry-run": ("d", "cyan"),
           "": ("-", "dim")}
LEGEND = "steps in order: " + " ".join(STEPS) + "; o ok, s skipped, e error, - not run"


def state_text(state: str) -> Text:
    if state.startswith("error"):
        style = "red"
    elif state.startswith("skipped"):
        style = "yellow"
    elif state.endswith("waiting"):
        style = "dark_orange"
    else:
        style = STATE_STYLES.get(state, "")
    return Text(state, style=style)


def badges(statuses) -> Text:
    t = Text()
    for st in statuses:
        letter, style = LETTERS.get(st, ("?", ""))
        t.append(letter, style=style)
    return t


def step_row(s: StepState) -> list:
    status = Text(f"{s.status} STALE", style="dark_orange") if s.stale else ui.badge(s.status)
    now = {"running": Text("running", style="bold cyan"), "done": Text("done", style="green")}.get(s.now, Text(""))
    return [s.step, status, fmt_age(s.age_hours), s.rows, now, s.message[:MESSAGE_WIDTH]]


def source_row(s: SourceRow) -> list:
    return [s.name, s.where, s.last, state_text(s.state)]


def run_row(r: RunSummary, tz: tzinfo | None = None) -> list:
    return [local_time(r.started_at, tz), r.origin, fmt_duration(r.seconds), badges(r.statuses), r.rows]


def run_step_row(s: RunStep) -> list:
    return [s.step, ui.badge(s.status), fmt_duration(s.seconds), s.rows, s.message]


def status_text(ov: Overview, launch: LaunchState | None = None, now: datetime | None = None,
                tz: tzinfo | None = None) -> Text:
    now = now or ov.now
    r = ov.running
    t = Text()
    if r.state == "running" and r.heartbeat is not None:
        hb = r.heartbeat
        started = parse_iso(hb.started_at)
        elapsed = (now - started).total_seconds() if started else r.elapsed_seconds
        t.append("RUNNING", style="bold cyan")
        t.append(f"  {hb.step} · {hb.detail} · started {local_time(hb.started_at, tz)} by {hb.origin}"
                 f" · pid {hb.pid} · {fmt_duration(elapsed)}")
    elif r.state == "stale" and r.heartbeat is not None:
        hb = r.heartbeat
        t.append("DID NOT FINISH", style="bold red")
        t.append(f"  run {hb.run_id} started {local_time(hb.started_at, tz)} by {hb.origin} (pid {hb.pid} gone)")
    else:
        t.append("IDLE", style="bold green")
        if ov.runs:
            last = ov.runs[0]
            outcome = f"{last.errors} error{'s' if last.errors != 1 else ''}" if last.errors else "ok"
            t.append(f"  last run {local_time(last.started_at, tz)} ({last.origin}, {outcome})")
        else:
            t.append("  no runs yet")
    if launch is not None and launch.kind != "idle":
        if launch.kind == "queued":
            t.append(f"  ·  console {launch.what} queued", style="yellow")
        elif launch.kind == "running":
            t.append(f"  ·  console {launch.what}", style="cyan")
        elif launch.kind == "finished":
            when = launch.finished_at.astimezone(tz).strftime("%H:%M:%S") if launch.finished_at else ""
            t.append(f"  ·  FINISHED exit {launch.exit_code} · {when}",
                     style="green" if launch.exit_code == 0 else "red")
        elif launch.kind == "failed":
            t.append(f"  ·  LAUNCH FAILED · {launch.error}", style="red")
    return t


def _short(iso: str | None) -> str:
    return iso[5:16].replace("T", " ") if iso else ""


def schedule_text(schedule: Schedule | None, reason: str) -> Text:
    if schedule is None:
        return Text(f"schedule: {reason or 'unknown'}", style="dim")
    t = Text()
    t.append("task ", style="dim")
    t.append(schedule.name, style="bold")
    t.append(f"   state {schedule.state}   last {_short(schedule.last_run) if schedule.last_run else 'never'}")
    if schedule.last_run is not None and schedule.last_result is not None:
        t.append(f" (exit {schedule.last_result})", style="green" if schedule.last_result == 0 else "red")
    t.append(f"   next {_short(schedule.next_run) if schedule.next_run else 'none'}\n")
    t.append("triggers ", style="dim")
    t.append(" · ".join(schedule.triggers) or "none")
    if schedule.log_path is not None:
        t.append("\nlog ", style="dim")
        t.append(str(schedule.log_path))
    return t


def universe_text(ov: Overview) -> str:
    return f"following {ov.universe_size} symbol{'s' if ov.universe_size != 1 else ''}"


def steps_table(ov: Overview) -> Table:
    return ui.table(["step", "status", "age", "rows", "now", "message"], [step_row(s) for s in ov.steps],
                    title="steps")


def sources_table(ov: Overview) -> Table:
    return ui.table(["source", "where / how", "last", "state"], [source_row(s) for s in ov.sources],
                    title=f"sources ({universe_text(ov)})")


def runs_table(ov: Overview, limit: int | None = None, tz: tzinfo | None = None) -> Table:
    rows = ov.runs[:limit] if limit else ov.runs
    return ui.table(["started", "origin", "took", "steps", "rows"], [run_row(r, tz) for r in rows],
                    title=f"runs (last {limit})" if limit else "runs", footer=LEGEND)


def run_steps_table(run: RunSummary, steps: list[RunStep]) -> Table:
    return ui.table(["step", "status", "took", "rows", "message"], [run_step_row(s) for s in steps],
                    title=f"run {run.run_id} ({run.origin})")


def render_once(ov: Overview) -> list:
    return [status_text(ov), steps_table(ov), schedule_text(ov.schedule, ov.schedule_reason), sources_table(ov),
            runs_table(ov, limit=10)]
