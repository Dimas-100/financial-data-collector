"""The valuation engine: fdc sync's `engine` step (no network, no AI).

It runs only once a full, error-free check run has covered the newest derive, so it builds from data the checker has
looked at; until then it writes nothing and says what it waits for. It reads the checker's open holds as missing
values (a held close is left out of every window; a held close on the newest day holds every price-shape row), then
writes one `valuation_scores_raw` row per symbol and metric at the symbol's newest final close, each stamped with the
covering check run's finish time. Holds opened after the build reach readers through the `valuation_scores` view.

This module is the orchestrator: the coverage gate, the per-symbol loop, the hold helpers and the write. The metrics
themselves live in pure modules (`shape.py`, ...) that the per-symbol loop calls.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..models import ScoreRow
from ..store import Store
from . import sources
from .shape import INPUTS as PRICE_INPUTS, METRICS as PRICE_METRICS, price_shape

PRICE_CHECKS = ("prices2", "impossible", "closes")      # the checks whose '*' hold is about the close itself
_OPEN_END = "9999-12-31"


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
    """`bars` (date first) without the ones on a date a price hold covers."""
    ph = price_holds(holds)
    return [b for b in bars if not any(_covers(h, b[0]) for h in ph)]


def _held_price_rows(symbol: str, day: str, newest: tuple, on_day: list[dict]) -> list[ScoreRow]:
    reason = "; ".join(h["message"] for h in on_day)
    src = (sources.price(newest[4], newest[0], newest[5]),)
    return [ScoreRow(symbol, day, m, None, None, None, "held", reason, PRICE_INPUTS, src) for m in PRICE_METRICS]


def run(store: Store, cfg: Config) -> EngineReport:
    cov = store.engine_coverage()
    if cov is None:
        derive = store.check_derive_run_id()
        why = (f"waiting for a check run to cover derive {derive}" if derive
               else "waiting for a check run: no derive has finished yet")
        return EngineReport(0, 0, 0, 0, why)

    benches: dict[str, tuple[str, list]] = {}

    def benchmark(symbol: str | None) -> tuple[str, list] | None:
        if not symbol:
            return None
        if symbol not in benches:
            benches[symbol] = (symbol, held_bars(store.final_bars(symbol), store.open_holds(symbol)))
        return benches[symbol]

    symbols = store.engine_symbols()
    rows: list[ScoreRow] = []
    for sec in symbols:
        sym = sec["symbol"]
        bars = store.final_bars(sym)
        day = bars[-1][0]                    # the newest final close, held or not
        holds = store.open_holds(sym)
        on_day = [h for h in price_holds(holds) if _covers(h, day)]
        if on_day:
            rows += _held_price_rows(sym, day, bars[-1], on_day)
        else:
            sector_fund = cfg.benchmarks.sectors.get(sec["sector"]) if sec["sector"] else None
            rows += price_shape(sym, day, held_bars(bars, holds), benchmark(cfg.benchmarks.market),
                                benchmark(sector_fund))
    store.replace_scores(rows, cov["checked_at"])
    return EngineReport(len(symbols), len(rows), sum(1 for r in rows if r.status == "held"), 0, None)
