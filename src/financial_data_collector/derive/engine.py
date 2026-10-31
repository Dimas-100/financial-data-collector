"""The valuation engine: fdc sync's `engine` step (no network, no AI).

It runs only once a full, error-free check run has covered the newest derive, so it builds from data the checker has
looked at; until then it writes nothing and says what it waits for. It reads the checker's open holds as missing
values (closes are adjusted over every final bar, then a held close is left out of every window; a held close on
the newest day holds every price-shape row), then
writes one `valuation_scores_raw` row per symbol and metric at the symbol's newest final close, and the symbol's drops
of the last `moves_days` with their causes into `price_moves` (`moves.py`), each stamped with the covering check run's
finish time. Holds opened after the build reach readers through the `valuation_scores` view.

This module is the orchestrator: the coverage gate, the per-symbol loop, the hold helpers and the write. The metrics
themselves live in pure modules (`shape.py`, `scores.py`, ...) that the per-symbol loop calls.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from ..config import Config
from ..models import PriceMove, ScoreRow
from ..store import Store
from . import moves, scores, sources
from .adjust import adjusted_closes
from .shape import INPUTS as PRICE_INPUTS, METRICS as PRICE_METRICS, price_shape
from .valuation import COLUMNS as VALUATION_COLUMNS

PRICE_CHECKS = ("prices2", "impossible", "closes")      # the checks whose '*' hold is about the close itself
_OPEN_END = "9999-12-31"
# The valuation_daily columns a hold can blank: every value, not the keys, period markers or status.
VALUE_FIELDS = tuple(c for c in VALUATION_COLUMNS
                     if c not in ("symbol", "date", "ttm_period_end", "available_from", "status", "status_reason"))


@dataclass(frozen=True)
class EngineReport:
    symbols: int
    scores: int
    held: int
    moves: int
    skipped: str | None


def _covers(hold: dict, day: str) -> bool:
    return hold["date_from"] <= day <= (hold["date_to"] or _OPEN_END)


def apply_holds(rows: list[dict], holds: list[dict], fields: tuple[str, ...]) -> list[dict]:
    """Copies of `rows` (each with a "date") where a value field is None on a date an open hold covers when the
    hold's field is that field or '*'; each copy gains "_held", the ids of the holds that blanked something."""
    out: list[dict] = []
    for row in rows:
        copy = dict(row)
        held: list[int] = []
        for h in holds:
            if (h["field"] == "*" or h["field"] in fields) and _covers(h, row["date"]):
                held.append(h["id"])
                for f in (fields if h["field"] == "*" else (h["field"],)):
                    if f in copy:
                        copy[f] = None
        copy["_held"] = held
        out.append(copy)
    return out


def price_holds(holds: list[dict]) -> list[dict]:
    """The holds that touch a close: field `close`, or '*' from a PRICE_CHECKS check. A '*' hold from fresh, filings
    or figures is about statements and leaves prices alone, as valuation_latest always shows `close`."""
    return [h for h in holds if h["field"] == "close" or (h["field"] == "*" and h["check_name"] in PRICE_CHECKS)]


def held_bars(bars, holds: list[dict]) -> list[tuple]:
    """`bars` (date first) without the ones on a date a price hold covers. Raw bars: a window over closes reads
    `held_adjusted_bars` instead, which adjusts before it drops, so a held ex-date keeps its split."""
    ph = price_holds(holds)
    return [b for b in bars if not any(_covers(h, b[0]) for h in ph)]


def held_adjusted_bars(bars, holds: list[dict]) -> list[tuple]:
    """The series every price window reads: `bars` (ascending (date, close, dividend, split_factor, source,
    fetched_at), every final bar) adjusted for splits and dividends over ALL of them first, then the dates a price hold
    covers dropped. Each kept bar comes back as (date, adjusted close, 0.0, 1.0, source, fetched_at), so adjusting it
    again is a no-op. Adjusting before dropping keeps a held ex-date's split or dividend applied to every earlier close."""
    adjusted = adjusted_closes([(b[0], b[1], b[2], b[3]) for b in bars])
    ph = price_holds(holds)
    return [(b[0], adj, 0.0, 1.0, b[4], b[5]) for b, (_, adj) in zip(bars, adjusted)
            if not any(_covers(h, b[0]) for h in ph)]


def statement_holds(holds: list[dict]) -> list[dict]:
    """The holds as they apply to valuation_daily rows: every hold (a '*' from any check, statements included, as
    valuation_latest applies them), with a `close` hold widened to '*', since every valuation value of a day is built
    on its close."""
    return [dict(h, field="*") if h["field"] == "close" else h for h in holds]


ANNUAL_ROWS = 12                     # the newest annual rows read for 5-year growth (FY and FY - 5, with room)


def _company(store: Store, cfg: Config, symbol: str, cik: str, day: str, bar: tuple, holds: list[dict],
             insider: tuple[list[dict] | None, str | None] | None) -> list[ScoreRow]:
    """A company's scores from its held valuation_daily rows up to `day`: P/E, P/FCF, P/S, EV/EBIT and yield against
    its own history (scores.history_scores; nothing when it has no rows) and the ten fundamentals
    (scores.fundamental_scores, always)."""
    vrows = [r for r in store.valuation_rows(symbol) if r["date"] <= day]
    sh = statement_holds(holds)
    held = apply_holds(vrows, sh, VALUE_FIELDS)
    ttm = store.ttm_rows(cik)
    filings: dict[str, dict | None] = {}

    def filing_of(period_end: str) -> dict | None:
        if period_end not in filings:
            filings[period_end] = store.statement_filing(cik, period_end)
        return filings[period_end]

    out: list[ScoreRow] = []
    if vrows:
        period_end = vrows[-1]["ttm_period_end"]
        out += scores.history_scores(symbol, day, held, ttm, filing_of(period_end) if period_end else None, cik,
                                     cfg.engine, bar=bar, holds=sh)
    out += scores.fundamental_scores(
        symbol, day, held[-1] if held else None, ttm, store.annual_statement_rows(cik, ANNUAL_ROWS),
        store.earnings_events_of(symbol), store.estimate_rows(symbol, scores.ESTIMATE_PERIOD), insider, cik,
        cfg.engine, bar=bar, holds=holds, filing_of=filing_of)
    return out


def _insider_feed(cfg: Config):
    """The insider feed, read once per run: a function symbol -> what scores.fundamental_scores takes as `insider`
    (None when the feed is missing or unreadable, (None, as_of) when it does not name the symbol)."""
    path = cfg.investing_dir / "data" / scores.INSIDER_FILE if cfg.investing_dir is not None else None
    by_symbol, as_of = scores.read_insider(path) if path is not None else ({}, None)
    found = bool(by_symbol) or as_of is not None
    return lambda symbol: (by_symbol.get(symbol), as_of) if found else None


def _held_price_rows(symbol: str, day: str, newest: tuple, on_day: list[dict]) -> list[ScoreRow]:
    reason = "; ".join(h["message"] for h in on_day)
    src = (sources.price(newest[4], newest[0], newest[5]),)
    return [ScoreRow(symbol, day, m, None, None, None, "held", reason, PRICE_INPUTS, src) for m in PRICE_METRICS]


def _moves(store: Store, cfg: Config, sec, day: str, series: list[tuple], market, sector,
           sector_name: str | None) -> list[PriceMove]:
    """The symbol's drops dated in the `moves_days` calendar days up to `day` (moves.find_moves over its held adjusted
    closes and the benchmarks'), each with its causes from the stored earnings events, 8-Ks and headlines."""
    since = (date.fromisoformat(day) - timedelta(days=cfg.engine.moves_days)).isoformat()
    found = moves.find_moves(sec["symbol"], series, market, sector, since, cfg.engine)
    if not found:
        return []
    start = min(m.start_date for m in found)
    cik = sec["cik"]
    events = store.earnings_events_of(sec["symbol"])
    reports = store.current_reports_of(cik, start, day) if cik else []
    headlines = store.headlines_of(sec["symbol"], start, day)
    return [moves.add_causes(m, events, reports, headlines, cik, sector_name) for m in found]


def run(store: Store, cfg: Config) -> EngineReport:
    cov = store.engine_coverage()
    if cov is None:
        derive = store.engine_newest_derive()
        why = (f"waiting for a check run to cover derive {derive}" if derive
               else "waiting for a check run: no derive has finished yet")
        return EngineReport(0, 0, 0, 0, why)

    benches: dict[str, tuple[str, list]] = {}

    def benchmark(symbol: str | None) -> tuple[str, list] | None:
        if not symbol:
            return None
        if symbol not in benches:
            benches[symbol] = (symbol, held_adjusted_bars(store.final_bars(symbol), store.open_holds(symbol)))
        return benches[symbol]

    insider_of = _insider_feed(cfg)
    symbols = store.engine_symbols()
    rows: list[ScoreRow] = []
    found: list[PriceMove] = []
    for sec in symbols:
        sym = sec["symbol"]
        bars = store.final_bars(sym)
        day = bars[-1][0]                    # the newest final close, held or not
        holds = store.open_holds(sym)
        series = held_adjusted_bars(bars, holds)
        sector_fund = cfg.benchmarks.sectors.get(sec["sector"]) if sec["sector"] else None
        on_day = [h for h in price_holds(holds) if _covers(h, day)]
        if on_day:
            rows += _held_price_rows(sym, day, bars[-1], on_day)
        else:
            rows += price_shape(sym, day, series, benchmark(cfg.benchmarks.market), benchmark(sector_fund))
        if sec["asset_type"] in scores.FUND_TYPES:
            rows += scores.fund_scores(sym, day, store.fund_snapshot_rows(sym), cfg.engine, holds=holds)
        if sec["asset_type"] == "stock" and sec["cik"]:
            rows += _company(store, cfg, sym, sec["cik"], day, bars[-1], holds, insider_of(sym))
        found += _moves(store, cfg, sec, day, series, benchmark(cfg.benchmarks.market), benchmark(sector_fund),
                        sec["sector"] if sector_fund else None)
    store.replace_scores(rows, cov["checked_at"])
    store.replace_moves(found, cov["checked_at"])
    return EngineReport(len(symbols), len(rows), sum(1 for r in rows if r.status == "held"), len(found), None)
