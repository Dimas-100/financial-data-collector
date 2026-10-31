"""fdc sync: run each step independently, log each to sync_runs, never let one failure stop the rest."""
from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from . import check as check_mod, heartbeat, http, sectors as sectors_mod, statements
from .collectors import earnings as earnings_mod, funds as funds_mod, prices as prices_mod, yahoo as yahoo_mod
from .collectors.macro import MacroError, collect_macro, start_date as macro_start
from .collectors.sec_facts import FUND_TYPES, collect_sec
from .config import Config, ConfigError
from .connections.keys import KeyHome
from .connections.service import run as run_connections
from .derive import history, valuation
from .export.cockpit import export_cockpit
from .ingest import ingest_inbox, ingest_snaptrade
from .models import PriceBar
from .store import Store, utcnow
from .universe import build_universe

STEPS = ("connections", "ingest", "prices", "macro", "sec", "funds", "earnings", "derive", "export", "check")
NETWORK_STEPS = {"connections", "prices", "macro", "sec", "funds", "earnings"}
ORIGINS = ("scheduled", "manual", "console")       # who started a run: the task, a hand, fdc console
STALE_HOURS = {"ingest": 48, "prices": 48, "macro": 48, "sec": 336, "funds": 48, "earnings": 48, "derive": 48,
               "export": 48, "check": 48}


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
        network = [s for s in self.steps if s.step in NETWORK_STEPS
                   and not (s.step in ("connections", "macro", "earnings") and s.status == "skipped")]
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


Progress = Callable[[str], None]


def _step_connections(store: Store, cfg: Config, *, fetch, now, progress: Progress, keys=None,
                      **_) -> tuple[int, str, str]:
    home = keys or KeyHome.for_root(cfg.root)
    return run_connections(store, home, min_hours=cfg.connections_min_hours, fetch=fetch, now=now, progress=progress)


def _step_ingest(store: Store, cfg: Config, *, progress: Progress, **_) -> tuple[int, str, str]:
    progress("inbox")
    a = ingest_inbox(store, cfg)
    progress("broker feed")
    b = ingest_snaptrade(store, cfg)
    msgs = a.messages + b.messages
    return a.rows + b.rows, "; ".join(msgs), "ok"


def _universe(store: Store, cfg: Config) -> list[str]:
    """What the collecting steps work on: the benchmarks the config names are marked (and recorded) first, so they
    are collected like any symbol; then the usual universe. The export builds its own, without the benchmarks."""
    store.mark_benchmarks(cfg.benchmarks.symbols)
    return build_universe(store, cfg.watchlist, classify=cfg.classify, investing_dir=cfg.investing_dir)


def _step_prices(store: Store, cfg: Config, *, fetch, now, yf, sleep, progress: Progress, **_) -> tuple[int, str, str]:
    universe = _universe(store, cfg)
    kwargs = {"fetch": fetch, "today": now.date(), "now": now, "sleep": sleep, "progress": progress}
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


def _step_macro(store: Store, cfg: Config, *, fetch, now, progress: Progress, **_) -> tuple[int, str, str]:
    key = cfg.fred_api_key
    if not key:
        raise ConfigError("FRED_API_KEY is not set; get a free key at fred.stlouisfed.org and put it in .env")
    progress("FRED series")
    start = macro_start(now.date(), store.macro_has_rows())
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        obs = collect_macro(key, lambda url: json.loads(fetch(url, None)), start)
        failed = ""
    except MacroError as e:       # the message is already scrubbed; what arrived is still kept
        obs, failed = e.observations, str(e)
    rows = store.write_macro(obs, stamp)
    if failed:
        return rows, failed, "ok" if obs else "error"
    return rows, f"{rows} observations new or revised", "ok"


def _step_sec(store: Store, cfg: Config, *, fetch, now, sleep, progress: Progress, **_) -> tuple[int, str, str]:
    results = collect_sec(store, cfg, fetch=fetch, now=now, sleep=sleep, progress=progress)
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


def _step_funds(store: Store, cfg: Config, *, now, yahoo, sleep, progress: Progress, **_) -> tuple[int, str, str]:
    results = funds_mod.collect_funds(store, _universe(store, cfg), cfg, yahoo=yahoo, now=now, sleep=sleep,
                                      progress=progress)
    written = sum(r.written for r in results)
    nofigures = [r for r in results if r.source is None and not r.failed]
    failed = [r for r in results if r.failed]
    noted = [r for r in results if r.message and not r.failed and r.message != "no figures"]
    msg = f"{written} snapshots written, {len(nofigures)} without figures, {len(failed)} failed"
    if noted:
        msg += "; " + ", ".join(f"{r.symbol} ({r.message})" for r in noted)
    if failed:
        msg += "; failed: " + ", ".join(f"{r.symbol} ({r.message})" for r in failed)
    status = "error" if results and len(failed) == len(results) else "ok"
    return written, msg, status


def _step_earnings(store: Store, cfg: Config, *, fetch, now, yahoo, sleep, progress: Progress,
                   **_) -> tuple[int, str, str]:
    results = earnings_mod.collect_earnings(store, _universe(store, cfg), cfg, fetch=fetch, yahoo=yahoo, now=now,
                                            sleep=sleep, progress=progress)
    changed = sum(r.events for r in results)
    written = sum(r.estimates for r in results)
    failed = [r for r in results if r.failed]
    # Sectors need no network (stored SEC codes and the sibling feed), so the pass runs even when no company was asked.
    sectors_changed = sectors_mod.assign_sectors(store, cfg)
    msg = (f"{len(results)} companies asked, {changed} events changed, {written} estimate rows written, "
           f"{sectors_changed} sectors changed")
    if failed:
        msg += f"; {len(failed)} failed: " + ", ".join(f"{r.symbol} ({r.message})" for r in failed)
    if not cfg.sec_user_agent:       # the Yahoo part still ran; the message says what was left out
        msg = earnings_mod.SEC_SKIPPED + msg
    # No company to ask (all asked today, or none typed a stock yet) is skipped, not ok: it says nothing about the
    # network, so exit_code leaves it out, as it does an unconfigured connections or macro step.
    status = "skipped" if not results else "error" if len(failed) == len(results) else "ok"
    return changed + written + sectors_changed, msg, status


def _step_derive(store: Store, cfg: Config, *, progress: Progress, **_) -> tuple[int, str, str]:
    # Statements are a function of the shaper and the concept map, not of the fetch:
    # reshape every company from its stored facts so a code or seed change applies everywhere.
    # A company whose every symbol is of a type the statements never shape (a fund, a foreign filer) keeps its
    # old facts in sec_facts but is not shaped: its line items are cleared instead, so they cannot come back.
    rules = store.concept_rules()
    reshaped, cleared, failed = 0, 0, []
    kinds: dict[str, set[str]] = {}
    for _symbol, cik, asset_type in store.securities_with_cik():
        kinds.setdefault(cik, set()).add(asset_type)
    ciks = store.ciks_with_facts()
    for i, cik in enumerate(ciks, start=1):
        progress(f"statements {i}/{len(ciks)}")
        if kinds.get(cik) and kinds[cik] <= FUND_TYPES:
            store.replace_line_items(cik, [])
            cleared += 1
            continue
        try:
            store.replace_line_items(cik, statements.rebuild(store.facts_for(cik), rules))
            reshaped += 1
        except Exception as e:  # one odd filer keeps its previous rows; the rest still rebuild
            failed.append(f"{cik} ({type(e).__name__}: {e})")
    counts = {"statements reshaped": reshaped}
    if cleared:
        counts["statements cleared (fund or foreign filer)"] = cleared
    if failed:
        counts[f"statements {len(failed)} failed: " + "; ".join(failed)[:300]] = len(failed)
    progress("replaying history")
    counts.update(history.rebuild(store))
    progress("valuation")
    counts["valuation_daily symbols"] = valuation.rebuild(store, cfg)
    return sum(counts.values()), ", ".join(f"{k} {v}" for k, v in counts.items()), "ok"


def _step_export(store: Store, cfg: Config, *, now, progress: Progress, **_) -> tuple[int, str, str]:
    if not cfg.export_cockpit:
        return 0, "export.cockpit disabled in config.toml", "skipped"
    progress("writing feed files")
    universe = build_universe(store, cfg.watchlist, classify=cfg.classify, investing_dir=cfg.investing_dir,
                              include_benchmarks=False)
    results = export_cockpit(store, cfg, universe, now)
    written = sum(1 for _, s in results if s == "written")
    msg = ", ".join(f"{name} {status}" for name, status in results)
    if all(s == "skipped" for _, s in results):
        return 0, f"target folder missing ({cfg.export_dir}); " + msg, "skipped"
    return written, msg, "ok"


def _step_check(store: Store, cfg: Config, *, fetch, now, sleep, progress: Progress, **_) -> tuple[int, str, str]:
    # The checks' own failures are findings about the checks, not a failed sync: the step is ok unless the
    # frame itself breaks (that raises and run_sync records an error, which exit_code ignores).
    progress("checks")
    rep = check_mod.run_check(cfg, fetch=fetch, now=now, store=store, sleep=sleep)
    msg = f"{rep.holds} holds found, {rep.open_holds} open, {rep.warns} warnings, {len(rep.errors)} errors"
    if rep.errors:
        msg += ": " + "; ".join(rep.errors)
    return rep.holds + rep.warns, msg, "ok"


_RUNNERS: dict[str, Callable] = {
    "connections": _step_connections, "ingest": _step_ingest, "prices": _step_prices, "macro": _step_macro, "sec": _step_sec,
    "funds": _step_funds, "earnings": _step_earnings, "derive": _step_derive, "export": _step_export,
    "check": _step_check,
}


def run_sync(
    cfg: Config,
    *,
    only: list[str] | None = None,
    skip: list[str] | None = None,
    dry_run: bool = False,
    fetch: http.Fetch = http.fetch,
    now: datetime | None = None,
    yf: Callable[[str, str], list[PriceBar]] | None = None,
    yahoo: yahoo_mod.YahooClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[str, str], None] | None = None,
    keys: KeyHome | None = None,
    origin: str = "manual",
) -> SyncReport:
    """Run the selected steps. `progress(step, detail)` is called as work advances (for a live display). `keys`
    is where the connections step finds its keys; by default the key store, or .env, beside config.toml.
    `origin` says who started the run: `scheduled`, `manual` or `console`. `yahoo` is the Yahoo client the collectors
    ask; by default yfinance, looked up when the run starts."""
    now = now or datetime.now(timezone.utc)
    yahoo = yahoo or yahoo_mod.default_client()
    run_id = now.strftime("%Y%m%dT%H%M%SZ")
    steps = _select_steps(only, skip)
    report = SyncReport(run_id=run_id, dry_run=dry_run)
    if dry_run:
        report.steps = [StepResult(s, "dry-run") for s in steps]
        return report
    waited = heartbeat.wait_for_turn(cfg.heartbeat_path, now=now, sleep=sleep)
    beat = heartbeat.Writer(cfg.heartbeat_path, run_id, origin)
    store = Store.open(cfg.db_path, backup_dir=cfg.backup_dir)
    try:
        beat.start()                       # inside the try: a failed open leaves no heartbeat behind
        for step in steps:
            started = utcnow()

            def report_progress(detail: str, _step: str = step) -> None:
                beat.update(_step, detail)
                if progress:
                    progress(_step, detail)

            report_progress("starting")
            try:
                rows, message, status = _RUNNERS[step](store, cfg, fetch=fetch, now=now, yf=yf, yahoo=yahoo,
                                                       sleep=sleep, progress=report_progress, keys=keys)
            except ConfigError as e:
                rows, message, status = 0, str(e), "skipped"
            except Exception as e:  # isolate: log and continue with the next step
                tb = traceback.format_exc().strip().splitlines()
                rows, message, status = 0, f"{type(e).__name__}: {e} | " + " | ".join(tb[-3:]), "error"
            if waited and not report.steps:
                message = f"waited {waited:.0f} s for another sync; {message}"
            finished = utcnow()
            store.log_run(run_id, step, status, rows, message, started, finished, origin)
            report.steps.append(StepResult(step, status, rows, message, started, finished))
    finally:
        store.close()
        beat.finish()
    return report
