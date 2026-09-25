"""Feed files for the investing cockpit, in the exact shapes its own pipeline used to write.

prices.json       {as_of, source, lookback_days, sources{T: src}, by_ticker{T: [{d, c}]}, misses[]}
dividends.json    {as_of, source, history_from, by_ticker{T: [{d, amt}]}, misses[]}
fundamentals.json {as_of, source, lookback_years, by_ticker{T: {company_name, annual[], quarterly[]}}, misses[]}
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from ..derive.adjust import adjusted_closes
from ..store import Store

FILES = ("prices.json", "dividends.json", "fundamentals.json")
PRICES_LOOKBACK_DAYS = 1826
FUNDAMENTALS_LOOKBACK_YEARS = 10
QUARTERS_KEPT = 9
FUND_TYPES = {"etf", "mutual_fund", "money_market", "crypto"}
ANNUAL_KEYS = (
    "fy", "revenue", "gross_profit", "operating_income", "net_income", "ocf", "capex", "assets", "liabilities",
    "cash", "shares_diluted", "eps_diluted", "dividends_paid", "buybacks", "fcf",
    "gross_margin", "operating_margin", "net_margin", "fcf_margin",
)
QUARTER_KEYS = ("end", "revenue", "net_income", "eps_diluted", "shares_diluted")
_ANNUAL_SOURCE = {"assets": "total_assets", "liabilities": "total_liabilities"}


def _as_of(now: datetime) -> str:
    return now.isoformat()


def _history_from(now: datetime) -> str:
    return (now.date() - timedelta(days=PRICES_LOOKBACK_DAYS)).isoformat()


def build_prices(store: Store, universe: list[str], now: datetime) -> dict:
    src_map = {r["symbol"]: r["price_source"] for r in store.securities()}
    start = _history_from(now)
    by, sources, misses = {}, {}, []
    for sym in sorted(universe):
        # adjusted locally from close/dividend/split so every bar shares one basis
        # (the stored adj_close column is spliced across incremental fetches)
        rows = [(d, c) for d, c in adjusted_closes(store.prices_series(sym)) if d >= start]
        if not rows:
            misses.append(sym)
            continue
        by[sym] = [{"d": d, "c": c} for d, c in rows]
        sources[sym] = src_map.get(sym) or "unknown"
    return {
        "as_of": _as_of(now),
        "source": "financial-data-collector: Tiingo adjClose (split+div adjusted), yfinance fallback",
        "lookback_days": PRICES_LOOKBACK_DAYS,
        "sources": sources,
        "by_ticker": by,
        "misses": misses,
    }


def build_dividends(store: Store, universe: list[str], now: datetime) -> dict:
    by = {}
    for sym in sorted(universe):
        out: list[dict] = []
        later = 1.0  # product of split factors strictly after the row being visited
        for d, dividend, factor in reversed(store.price_events(sym)):
            if dividend and dividend > 0:
                out.append({"d": d, "amt": round(dividend / later, 6)})
            if factor and factor > 0 and factor != 1:
                later *= factor
        by[sym] = list(reversed(out))
    return {
        "as_of": _as_of(now),
        "source": "financial-data-collector: Tiingo divCash, split-adjusted to today's share terms",
        "history_from": _history_from(now),
        "by_ticker": by,
        "misses": [],
    }


def _annual(row: dict) -> dict:
    out = {}
    for k in ANNUAL_KEYS:
        out[k] = int(row["fiscal_year"]) if k == "fy" else row.get(_ANNUAL_SOURCE.get(k, k))
    return out


def build_fundamentals(store: Store, universe: list[str], now: datetime) -> dict:
    sec = {r["symbol"]: r for r in store.securities()}
    by, misses = {}, []
    for sym in sorted(universe):
        row = sec.get(sym)
        cik = row["cik"] if row else None
        if not cik or row["asset_type"] in FUND_TYPES:
            misses.append(sym)
            continue
        annual = store.annual_statement_rows(cik, FUNDAMENTALS_LOOKBACK_YEARS)
        quarterly = store.quarterly_statement_rows(cik, QUARTERS_KEPT)
        if not annual and not quarterly:
            misses.append(sym)
            continue
        by[sym] = {
            "company_name": row["sec_name"] or sym,
            "annual": [_annual(r) for r in annual],
            "quarterly": [{"end": r["period_end"], **{k: r.get(k) for k in QUARTER_KEYS[1:]}} for r in quarterly],
        }
    return {
        "as_of": _as_of(now),
        "source": "SEC EDGAR Company Facts (us-gaap) via financial-data-collector",
        "lookback_years": FUNDAMENTALS_LOOKBACK_YEARS,
        "by_ticker": by,
        "misses": misses,
    }


def _content(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if k != "as_of"}


def write_if_changed(path: Path, payload: dict) -> bool:
    """Atomically write payload unless the existing file has the same content (ignoring as_of)."""
    path = Path(path)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = None
        if existing is not None and _content(existing) == _content(payload):
            return False
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)
    return True


def export_cockpit(store: Store, cfg, universe: list[str], now: datetime, out_dir: Path | None = None) -> list[tuple[str, str]]:
    target = Path(out_dir) if out_dir else cfg.export_dir
    if target is None or not Path(target).is_dir():
        return [(name, "skipped") for name in FILES]
    target = Path(target)
    payloads = {
        "prices.json": build_prices(store, universe, now),
        "dividends.json": build_dividends(store, universe, now),
        "fundamentals.json": build_fundamentals(store, universe, now),
    }
    return [(name, "written" if write_if_changed(target / name, payload) else "unchanged")
            for name, payload in payloads.items()]
