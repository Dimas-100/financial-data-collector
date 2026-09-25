"""Daily bars: Tiingo when a token is configured, yfinance otherwise (or as fallback)."""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

from ..config import Config
from ..http import Fetch, HttpError
from ..models import PriceBar
from ..store import Store
from ..symbols import to_tiingo, to_yfinance

TIINGO_URL = "https://api.tiingo.com/tiingo/daily/{sym}/prices?startDate={start}&columns=date,close,adjClose,divCash,splitFactor"
TIINGO_PAUSE_SECONDS = 0.5


@dataclass
class PriceResult:
    symbol: str
    source: str | None
    rows: int
    message: str = ""


def next_start(last_date: str | None, today: date, lookback_years: int) -> str:
    if last_date:
        return (date.fromisoformat(last_date) + timedelta(days=1)).isoformat()
    try:
        return today.replace(year=today.year - lookback_years).isoformat()
    except ValueError:  # Feb 29
        return (today - timedelta(days=365 * lookback_years)).isoformat()


def tiingo_bars(symbol: str, start: str, token: str, fetch: Fetch) -> list[PriceBar]:
    url = TIINGO_URL.format(sym=to_tiingo(symbol), start=start)
    raw = fetch(url, {"Authorization": f"Token {token}", "Accept": "application/json"})
    data = json.loads(raw or b"[]")
    if not isinstance(data, list):
        return []
    bars: list[PriceBar] = []
    for o in data:
        if o.get("close") is None or o.get("adjClose") is None:
            continue
        bars.append(PriceBar(
            date=str(o["date"])[:10],
            close=float(o["close"]),
            adj_close=float(o["adjClose"]),
            dividend=float(o.get("divCash") or 0.0),
            split_factor=float(o.get("splitFactor") or 1.0),
        ))
    return bars


def yfinance_bars(symbol: str, start: str) -> list[PriceBar]:
    import yfinance as yf  # imported lazily: slow, and tests never need it

    hist = yf.Ticker(to_yfinance(symbol)).history(start=start, auto_adjust=False, actions=True)
    bars: list[PriceBar] = []
    for idx, row in hist.iterrows():
        close, adj = row.get("Close"), row.get("Adj Close", row.get("Close"))
        if close is None or (isinstance(close, float) and math.isnan(close)):
            continue
        div = row.get("Dividends", 0.0) or 0.0
        split = row.get("Stock Splits", 0.0) or 0.0
        bars.append(PriceBar(
            date=idx.strftime("%Y-%m-%d"),
            close=float(close),
            adj_close=float(adj if adj == adj else close),
            dividend=float(div if div == div else 0.0),
            split_factor=float(split) if split and split == split else 1.0,
        ))
    return bars


def collect_prices(
    store: Store,
    universe: list[str],
    cfg: Config,
    *,
    fetch: Fetch,
    today: date,
    yf: Callable[[str, str], list[PriceBar]] = yfinance_bars,
    sleep: Callable[[float], None] = time.sleep,
) -> list[PriceResult]:
    results: list[PriceResult] = []
    for symbol in universe:
        start = next_start(store.last_price_date(symbol), today, cfg.lookback_years)
        if start > today.isoformat():
            results.append(PriceResult(symbol, None, 0, "up to date"))
            continue
        bars: list[PriceBar] = []
        source: str | None = None
        notes: list[str] = []
        if cfg.tiingo_token:
            try:
                bars = tiingo_bars(symbol, start, cfg.tiingo_token, fetch)
                source = "tiingo" if bars else None
            except (HttpError, ValueError, json.JSONDecodeError) as e:
                notes.append(f"tiingo: {e}")
            sleep(TIINGO_PAUSE_SECONDS)
        if not bars:
            try:
                bars = yf(symbol, start)
                source = "yfinance" if bars else None
            except Exception as e:  # yfinance raises many types; a symbol miss must not stop the run
                notes.append(f"yfinance: {type(e).__name__}: {e}")
        if bars and source:
            n = store.write_prices(symbol, bars, source)
            results.append(PriceResult(symbol, source, n, "; ".join(notes)))
        elif store.last_price_date(symbol):
            # history exists and nothing newer is published yet (e.g. a mutual fund before its NAV posts)
            results.append(PriceResult(symbol, None, 0, "no new bars"))
        else:
            notes.append("no bars from any provider")
            results.append(PriceResult(symbol, None, 0, "; ".join(notes)))
    return results
