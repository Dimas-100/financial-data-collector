"""What the console shows, assembled read-only: steps, sources, runs, the heartbeat and the schedule. The screen
and the one-shot report render these dataclasses; neither touches the database or the files itself."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .. import heartbeat as H
from .. import readonly
from ..config import Config
from ..store import Store  # noqa: F401  (the read-only opener returns one; tests spy on it here)
from ..sync import STALE_HOURS, STEPS
from ..universe import read_watchlist, universe_symbols
from .schedule import Schedule


@dataclass(frozen=True)
class StepState:
    step: str
    status: str                 # ok | skipped | error | never
    age_hours: float | None
    stale: bool
    rows: int
    message: str
    now: str = ""               # "running" | "done" | "" from the heartbeat


@dataclass(frozen=True)
class SourceRow:
    name: str
    where: str
    last: str
    state: str


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    origin: str
    started_at: str
    finished_at: str
    seconds: float
    statuses: tuple[str, ...]   # one per STEPS entry; "" when the run did not include the step
    rows: int
    errors: int


@dataclass(frozen=True)
class RunStep:
    step: str
    status: str
    seconds: float
    rows: int
    message: str


@dataclass(frozen=True)
class Running:
    state: str                  # idle | running | stale
    heartbeat: H.Heartbeat | None = None
    elapsed_seconds: float | None = None


@dataclass(frozen=True)
class Overview:
    root: Path
    now: datetime
    steps: list[StepState]
    sources: list[SourceRow]
    universe_size: int
    runs: list[RunSummary]
    running: Running
    latest_snapshot: str | None
    schedule: Schedule | None = None
    schedule_reason: str = ""


# ---- formatting helpers (shared by the screen and the report) ------------------------------------------
def parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def seconds_between(a: str | None, b: str | None) -> float:
    da, db = parse_iso(a), parse_iso(b)
    return (db - da).total_seconds() if da and db else 0.0


def fmt_age(hours: float | None) -> str:
    if hours is None:
        return ""
    if hours < 1:
        return "<1 h ago"
    if hours < 48:
        return f"{hours:.0f} h ago"
    return f"{hours / 24:.1f} d ago"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(round(seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s // 3600} h {(s % 3600) // 60:02d} min"


def local_time(iso: str | None, tz: tzinfo | None = None) -> str:
    dt = parse_iso(iso)
    if dt is None:
        return "never"
    return dt.astimezone(tz).strftime("%m-%d %H:%M")


# ---- assembling ---------------------------------------------------------------------------------------
def summarize_runs(rows: Iterable[Mapping]) -> list[RunSummary]:
    """Group step rows (oldest first) into runs, newest run first."""
    by_run: dict[str, list[Mapping]] = {}
    for r in rows:
        by_run.setdefault(r["run_id"], []).append(r)
    out = []
    for run_id, steps in by_run.items():
        status = {s["step"]: s["status"] for s in steps}
        started = min(s["started_at"] for s in steps)
        finished = max(s["finished_at"] for s in steps)
        out.append(RunSummary(
            run_id=run_id, origin=steps[0]["origin"], started_at=started, finished_at=finished,
            seconds=seconds_between(started, finished),
            statuses=tuple(status.get(step, "") for step in STEPS),
            rows=sum(int(s["rows_written"] or 0) for s in steps),
            errors=sum(1 for s in steps if s["status"] == "error"),
        ))
    out.reverse()
    return out


def _running(hb: H.Heartbeat | None, now: datetime, alive) -> Running:
    if hb is None:
        return Running("idle")
    started = parse_iso(hb.started_at)
    elapsed = (now - started).total_seconds() if started else None
    if H.is_alive(hb, now=now, alive=alive):
        return Running("running", hb, elapsed)
    return Running("stale", hb, elapsed)


def _step_state(step: str, row: Mapping | None, running: Running, now: datetime) -> StepState:
    marker = ""
    if running.state == "running" and running.heartbeat is not None:
        if running.heartbeat.step == step:
            marker = "running"
        elif step in running.heartbeat.steps_done:
            marker = "done"
    if row is None:
        return StepState(step, "never", None, False, 0, "", marker)
    finished = parse_iso(row["finished_at"])           # age from the screen's own clock, not the view's
    age = round((now - finished).total_seconds() / 3600, 1) if finished else None
    stale = age is not None and age > STALE_HOURS.get(step, 48)
    return StepState(step, row["status"], age, stale, int(row["rows_written"] or 0), row["message"] or "", marker)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _companies(n: int) -> str:
    return f"{n} {'company' if n == 1 else 'companies'}"


def _ingested(ing: Mapping, prefix: str) -> str:
    items = [(v["last"], v["count"]) for k, v in ing.items() if k.startswith(prefix) and v["last"]]
    if not items:
        return "never"
    return f"{local_time(max(t for t, _ in items))}, {_count(sum(c for _, c in items), 'file')}"


def _sources(cfg: Config, facts: Mapping, status: Mapping[str, Mapping]) -> list[SourceRow]:
    rows: list[SourceRow] = []
    ing = facts["ingested"]
    if cfg.inbox.is_dir():
        waiting = [p for p in cfg.inbox.iterdir() if p.is_file() and not p.name.startswith(".")]  # .gitkeep is not a file to ingest
        state = f"{_count(len(waiting), 'file')} waiting" if waiting else "empty"
    else:
        state = "missing"
    rows.append(SourceRow("inbox files", str(cfg.inbox), _ingested(ing, "fidelity_"), state))
    if not cfg.snaptrade_enabled or cfg.snaptrade_dir is None:
        rows.append(SourceRow("broker export folder", str(cfg.snaptrade_dir or ""), "", "off"))
    else:
        rows.append(SourceRow("broker export folder", str(cfg.snaptrade_dir), _ingested(ing, "snaptrade_"),
                              "present" if cfg.snaptrade_dir.is_dir() else "missing"))
    for c in facts["connections"]:
        n = facts["accounts_by_origin"].get(c["name"], 0)
        state = f"error: {c['last_error']}" if c["last_error"] else ("ok" if c["last_ok_at"] else "never")
        rows.append(SourceRow(f"connection: {c['name']}", _count(n, "account"), local_time(c["last_ok_at"]), state))
    p, st = facts["prices"], status.get("prices")
    rows.append(SourceRow("prices", "Tiingo" if cfg.tiingo_token else "yfinance",
                          f"{_count(p['symbols'], 'symbol')}, latest {p['latest'] or 'none'}; "
                          f"step {local_time(st['finished_at']) if st else 'never'}",
                          st["status"] if st else "never"))
    s, st = facts["sec"], status.get("sec")
    state = "skipped: SEC_USER_AGENT not set" if not cfg.sec_user_agent else (st["status"] if st else "never")
    rows.append(SourceRow("SEC EDGAR", "companyfacts",
                          f"{_companies(s['companies'])}, newest filing {s['latest_filed'] or 'none'}; "
                          f"step {local_time(st['finished_at']) if st else 'never'}", state))
    if cfg.watchlist.is_file():
        rows.append(SourceRow("watchlist", str(cfg.watchlist), _count(len(read_watchlist(cfg.watchlist)), "symbol"),
                              "present"))
    else:
        rows.append(SourceRow("watchlist", str(cfg.watchlist), "", "missing"))
    if cfg.investing_dir is None:
        rows.append(SourceRow("research tickers", "", "", "off"))
    else:
        rows.append(SourceRow("research tickers", str(cfg.investing_dir), "",
                              "present" if cfg.investing_dir.is_dir() else "missing"))
    st = status.get("export")
    if not cfg.export_cockpit:
        rows.append(SourceRow("-> cockpit feed files", str(cfg.export_dir or ""), "", "off"))
    else:
        folder_ok = cfg.export_dir is not None and cfg.export_dir.is_dir()
        state = (st["status"] if st else "never") if folder_ok else "missing folder"
        rows.append(SourceRow("-> cockpit feed files", str(cfg.export_dir),
                              f"{local_time(st['finished_at'])}: {st['message']}" if st else "never", state))
    return rows


def load_overview(cfg: Config, *, now: datetime | None = None, store_open: Callable = readonly.open_store,
                  heartbeat_read: Callable[[Path], H.Heartbeat | None] = H.read, alive=None,
                  schedule: Schedule | None = None, schedule_reason: str = "", runs_limit: int = 50) -> Overview:
    """One read of everything the screen shows, over a read-only connection: a refresh can never write."""
    now = now or datetime.now(timezone.utc)
    running = _running(heartbeat_read(cfg.heartbeat_path), now, alive)
    store = store_open(cfg.db_path)
    try:
        status = {r["step"]: r for r in store.sync_status()}
        facts = store.source_facts()
        runs = summarize_runs(store.recent_run_rows(runs_limit))
        universe = len(universe_symbols(store, cfg.watchlist, classify=cfg.classify, investing_dir=cfg.investing_dir))
        latest = store.latest_snapshot_date()
    finally:
        store.close()
    return Overview(
        root=cfg.root, now=now,
        steps=[_step_state(step, status.get(step), running, now) for step in STEPS],
        sources=_sources(cfg, facts, status), universe_size=universe, runs=runs, running=running,
        latest_snapshot=latest, schedule=schedule, schedule_reason=schedule_reason,
    )


def load_run_steps(cfg: Config, run_id: str, *, store_open: Callable = readonly.open_store) -> list[RunStep]:
    store = store_open(cfg.db_path)
    try:
        rows = store.run_steps(run_id)
    finally:
        store.close()
    return [RunStep(r["step"], r["status"], seconds_between(r["started_at"], r["finished_at"]),
                    int(r["rows_written"] or 0), r["message"] or "") for r in rows]
