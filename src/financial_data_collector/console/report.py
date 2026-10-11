"""Rich renderables for an Overview: the one-shot `fdc console --once`, and the row builders, detail texts and
status line the live screen shares, so both look the same. Tables show short text and formatted numbers; the
detail texts hold everything about one row."""
from __future__ import annotations

from datetime import datetime, tzinfo
from pathlib import Path

from rich.table import Table
from rich.text import Text

from .. import ui
from ..sync import STEPS
from .actions import LaunchState
from .model import (
    Overview, RunStep, RunSummary, SourceRow, StepState, fmt_age, fmt_duration, local_time, parse_iso,
)
from .schedule import Schedule

SHORT_MESSAGE = 70
SHORT_LAST = 50
STATE_STYLES = {"ok": "green", "present": "green", "empty": "dim", "off": "dim", "never": "dim",
                "missing": "red", "missing folder": "red"}
LETTERS = {"ok": ("o", "green"), "skipped": ("s", "yellow"), "error": ("e", "red"), "dry-run": ("d", "cyan"),
           "": ("-", "dim")}
LEGEND = "steps in order: " + " ".join(STEPS) + "; o ok, s skipped, e error, - not run"
KIND_STYLES = {"idle": "bold green", "running": "bold cyan", "stale": "bold red", "queued": "bold yellow",
               "finished": "bold green", "failed": "bold red"}


def plain(text: str) -> Text:
    """A string from outside (a step's message, a path, a service's error) rendered as it is: Textual and rich
    would otherwise read its brackets as markup."""
    return Text(text)


def shorten(text: str, width: int = SHORT_MESSAGE) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= width else text[: width - 3] + "..."


def short_path(where: str, root: Path) -> str:
    """A path inside the project as its relative form, a sibling as ../name/..., anything else as given."""
    if not where:
        return ""
    p = Path(where)
    if not p.is_absolute():
        return where
    for base, prefix in ((root, None), (root.parent, "..")):
        try:
            rel = p.relative_to(base)
        except ValueError:
            continue
        return str(Path(prefix) / rel) if prefix else str(rel)
    return where


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


def status_cell(s: StepState) -> Text:
    if s.now == "running":
        return Text("running...", style="bold cyan")
    if s.stale:
        return Text(f"{s.status} STALE", style="dark_orange")
    return ui.badge(s.status)


def step_row(s: StepState) -> list:
    age = Text(fmt_age(s.age_hours), style="dark_orange" if s.stale else "dim")
    return [s.step, status_cell(s), age, Text(ui.fmt_number(s.rows), justify="right"), plain(shorten(s.message))]


def source_row(s: SourceRow, root: Path) -> list:
    return [plain(s.name), state_text(s.state), plain(shorten(s.last, SHORT_LAST)), plain(short_path(s.where, root))]


def run_rows(runs: list[RunSummary], tz: tzinfo | None = None) -> list[list]:
    """One row per run: the date once per day, then time, origin (dim when unknown), duration, badges, rows."""
    out: list[list] = []
    last_day = None
    for r in runs:
        dt = parse_iso(r.started_at)
        local = dt.astimezone(tz) if dt else None
        day = local.strftime("%m-%d") if local else "?"
        clock = local.strftime("%H:%M") if local else "?"
        out.append([Text(day if day != last_day else ""), Text(clock),
                    Text(r.origin, style="dim" if r.origin == "unknown" else ""), Text(fmt_duration(r.seconds)),
                    badges(r.statuses), Text(ui.fmt_number(r.rows), justify="right")])
        last_day = day
    return out


def run_step_row(s: RunStep) -> list:
    return [s.step, ui.badge(s.status), fmt_duration(s.seconds), s.rows, plain(s.message)]


# ---- the status band -----------------------------------------------------------------------------------
def status_kind(ov: Overview, launch: LaunchState | None = None) -> str:
    """idle | running | stale | queued | finished | failed: what colors the band."""
    if ov.running.state in ("running", "stale"):
        return ov.running.state
    if launch is not None:
        if launch.kind in ("queued", "running", "failed"):
            return launch.kind
        if launch.kind == "finished":
            return "finished" if launch.exit_code == 0 else "failed"
    return "idle"


def status_text(ov: Overview, launch: LaunchState | None = None, now: datetime | None = None,
                tz: tzinfo | None = None, schedule: Schedule | None = None) -> Text:
    """The band's text. `schedule` overrides the overview's when the screen holds a fresher reading."""
    now = now or ov.now
    schedule = schedule if schedule is not None else ov.schedule
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
    if schedule is not None and schedule.next_run:
        t.append(f"  ·  next {_short(schedule.next_run)}")
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


# ---- the detail strips -----------------------------------------------------------------------------------
def step_detail(s: StepState) -> Text:
    t = Text()
    t.append(s.step, style="bold")
    t.append("  ")
    t.append_text(status_cell(s))
    if s.stale:
        t.append("  ·  STALE", style="dark_orange")
    if s.age_hours is not None:
        t.append(f"  ·  {fmt_age(s.age_hours)}")
    t.append(f"  ·  {ui.fmt_number(s.rows)} rows\n")
    t.append(s.message or "(no message)")
    return t


def source_detail(s: SourceRow) -> Text:
    t = Text()
    t.append(s.name, style="bold")
    t.append("  ")
    t.append_text(state_text(s.state))
    t.append("\n")
    if s.where:
        t.append("where  ", style="dim")
        t.append(s.where)
        t.append("\n")
    t.append("last   ", style="dim")
    t.append(s.last or "never")
    return t


def run_detail(run: RunSummary, steps: list[RunStep], tz: tzinfo | None = None) -> Text:
    t = Text()
    t.append(f"run {run.run_id}", style="bold")
    outcome = f"{run.errors} error{'s' if run.errors != 1 else ''}" if run.errors else "ok"
    t.append(f"  ·  {run.origin}  ·  started {local_time(run.started_at, tz)}  ·  {fmt_duration(run.seconds)}"
             f"  ·  {outcome}\n")
    if not steps:
        t.append("loading...", style="dim")
        return t
    for s in steps:
        t.append(f"{s.step:<12}", style="bold")
        t.append_text(ui.badge(s.status))
        t.append(f"  {fmt_duration(s.seconds):>11}  {ui.fmt_number(s.rows):>9} rows")
        if s.message:
            t.append("\n    ")
            t.append(s.message)
        t.append("\n")
    return t


def _short(iso: str | None) -> str:
    return iso[5:16].replace("T", " ") if iso else ""


def schedule_text(schedule: Schedule | None, reason: str) -> Text:
    """Three lines: task and state; triggers; last, next and log."""
    if schedule is None:
        return Text(f"schedule: {reason or 'unknown'}", style="dim")
    t = Text()
    t.append(schedule.name, style="bold")
    t.append(f"   {schedule.state}\n")
    t.append(" · ".join(schedule.triggers) or "none")
    t.append("\n")
    t.append(f"last {_short(schedule.last_run) if schedule.last_run else 'never'}")
    if schedule.last_run is not None and schedule.last_result is not None:
        t.append(f" (exit {schedule.last_result})", style="green" if schedule.last_result == 0 else "red")
    t.append(f"   next {_short(schedule.next_run) if schedule.next_run else 'none'}")
    if schedule.log_path is not None:
        t.append(f"   log {schedule.log_path}")
    return t


def universe_text(ov: Overview) -> str:
    return f"following {ov.universe_size} symbol{'s' if ov.universe_size != 1 else ''}"


# ---- the one-shot report ---------------------------------------------------------------------------------
def steps_table(ov: Overview) -> Table:
    return ui.table(["step", "status", "age", "rows", "message"], [step_row(s) for s in ov.steps], title="steps")


def sources_table(ov: Overview) -> Table:
    return ui.table(["source", "state", "last", "where / how"], [source_row(s, ov.root) for s in ov.sources],
                    title=f"sources ({universe_text(ov)})")


def runs_table(ov: Overview, limit: int | None = None, tz: tzinfo | None = None) -> Table:
    rows = ov.runs[:limit] if limit else ov.runs
    return ui.table(["date", "time", "origin", "took", "steps", "rows"], run_rows(rows, tz),
                    title=f"runs (last {limit})" if limit else "runs", footer=LEGEND)


def run_steps_table(run: RunSummary, steps: list[RunStep]) -> Table:
    return ui.table(["step", "status", "took", "rows", "message"], [run_step_row(s) for s in steps],
                    title=f"run {run.run_id} ({run.origin})")


def render_once(ov: Overview) -> list:
    return [status_text(ov), steps_table(ov), schedule_text(ov.schedule, ov.schedule_reason), sources_table(ov),
            runs_table(ov, limit=10)]
