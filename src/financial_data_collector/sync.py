"""fdc sync: run each step independently, log each to sync_runs, never let one failure stop the rest."""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from . import http
from .collectors import prices as prices_mod
from .collectors.sec_facts import collect_sec
from .config import Config, ConfigError
from .ingest import ingest_inbox, ingest_snaptrade
from .models import PriceBar
from .store import Store, utcnow
from .universe import build_universe

STEPS = ("ingest", "prices", "sec")
NETWORK_STEPS = {"prices", "sec"}


@dataclass
class StepResult:
    step: str
    status: str  # ok | skipped | error | dry-run
    rows: int = 0
    message: str = ""
    started_at: str = ""
    finished_at: str = ""


@dataclass
class SyncReport:
    run_id: str
    steps: list[StepResult] = field(default_factory=list)
    dry_run: bool = False

    @property
    def exit_code(self) -> int:
        network = [s for s in self.steps if s.step in NETWORK_STEPS]
        if network and all(s.status == "error" for s in network):
            return 1
        return 0


def _select_steps(only, skip) -> list[str]:
    only = set(only or ())
    skip = set(skip or ())
    unknown = (only | skip) - set(STEPS)
    if unknown:
        raise ConfigError(f"unknown step(s): {', '.join(sorted(unknown))}; valid: {', '.join(STEPS)}")
    return [s for s in STEPS if (not only or s in only) and s not in skip]


def _step_ingest(store: Store, cfg: Config, **_) -> tuple[int, str, str]:
    a = ingest_inbox(store, cfg)
    b = ingest_snaptrade(store, cfg)
    msgs = a.messages + b.messages
    return a.rows + b.rows, "; ".join(msgs), "ok"


def _step_prices(store: Store, cfg: Config, *, fetch, now, yf, sleep) -> tuple[int, str, str]:
    universe = build_universe(store, cfg.watchlist, classify=cfg.classify)
    kwargs = {"fetch": fetch, "today": now.date(), "sleep": sleep}
    if yf is not None:
        kwargs["yf"] = yf
    results = prices_mod.collect_prices(store, universe, cfg, **kwargs)
    updated = [r for r in results if r.rows > 0]
    failed = [r for r in results if r.failed]
    nodata = [r for r in results if r.rows == 0 and not r.failed and r.message not in ("up to date", "no new bars")]
    msg = f"{len(updated)} symbols updated"
    if nodata:
        msg += f", {len(nodata)} without data: " + ", ".join(f"{r.symbol} ({r.message})" for r in nodata)
    if failed:
        msg += f", {len(failed)} failed: " + ", ".join(f"{r.symbol} ({r.message})" for r in failed)
    # Only an outage is an error: every symbol hit a provider failure. Quiet mornings
    # ("no new bars") and unknown tickers ("no bars from any provider") are informational.
    status = "error" if results and all(r.failed for r in results) else "ok"
    return sum(r.rows for r in results), msg, status


def _step_sec(store: Store, cfg: Config, *, fetch, now, sleep, **_) -> tuple[int, str, str]:
    results = collect_sec(store, cfg, fetch=fetch, now=now, sleep=sleep)
    fetched = [r for r in results if r.facts and not r.message]
    failed = [r for r in results if r.message.startswith("fetch failed")]
    fresh = [r for r in results if r.message == "fresh"]
    nocik = [r for r in results if r.cik is None]
    msg = f"{len(fetched)} fetched, {len(fresh)} fresh, {len(nocik)} without CIK"
    if nocik:
        msg += " (" + ", ".join(r.symbol for r in nocik) + ")"
    if failed:
        msg += "; failed: " + ", ".join(f"{r.symbol} ({r.message})" for r in failed)
    status = "error" if results and failed and not fetched and not fresh else "ok"
    return sum(r.facts for r in results), msg, status


_RUNNERS: dict[str, Callable] = {"ingest": _step_ingest, "prices": _step_prices, "sec": _step_sec}


def run_sync(
    cfg: Config,
    *,
    only: list[str] | None = None,
    skip: list[str] | None = None,
    dry_run: bool = False,
    fetch: http.Fetch = http.fetch,
    now: datetime | None = None,
    yf: Callable[[str, str], list[PriceBar]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> SyncReport:
    now = now or datetime.now(timezone.utc)
    run_id = now.strftime("%Y%m%dT%H%M%SZ")
    steps = _select_steps(only, skip)
    report = SyncReport(run_id=run_id, dry_run=dry_run)
    if dry_run:
        report.steps = [StepResult(s, "dry-run") for s in steps]
        return report
    store = Store.open(cfg.db_path, backup_dir=cfg.backup_dir)
    try:
        for step in steps:
            started = utcnow()
            try:
                rows, message, status = _RUNNERS[step](store, cfg, fetch=fetch, now=now, yf=yf, sleep=sleep)
            except ConfigError as e:
                rows, message, status = 0, str(e), "skipped"
            except Exception as e:  # isolate: log and continue with the next step
                tb = traceback.format_exc().strip().splitlines()
                rows, message, status = 0, f"{type(e).__name__}: {e} | " + " | ".join(tb[-3:]), "error"
            finished = utcnow()
            store.log_run(run_id, step, status, rows, message, started, finished)
            report.steps.append(StepResult(step, status, rows, message, started, finished))
    finally:
        store.close()
    return report
