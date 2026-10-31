"""Fund snapshots: once per New York day, Yahoo's valuation figures for each fund in the universe.

A fund (ETF, mutual fund) files no statements, so the warehouse has no P/E or yield for it; Yahoo does. History starts
the day this first runs. Some funds have none of their own (a mutual fund Yahoo gives only an expense ratio): the config's
`[fund_proxies]` names an ETF that tracks the same index, whose P/E and yield stand in, and the row says so in `source`.
Figures are stored as fractions: a yield or expense ratio of 1.21% is 0.0121."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from ..config import Config
from ..connections.redact import redact
from ..models import FundSnapshot
from ..store import Store
from .yahoo import PAUSE_SECONDS, YahooClient

FUND_TYPES = ("etf", "mutual_fund", "fund")
NEW_YORK = ZoneInfo("America/New_York")
YIELD_RANGE = (0.0, 0.25)               # a fraction: 0 up to, not including, 25%
EXPENSE_RANGE = (0.0, 0.05)             # 0 up to, not including, 5%
SOURCE = "yfinance"


@dataclass
class FundResult:
    symbol: str
    source: str | None                  # None: nothing was written because Yahoo had no figures, or the ask failed
    written: int                        # 1 when a row was inserted or changed, else 0
    message: str = ""
    failed: bool = False                # Yahoo (or the proxy's ask) raised: the fund is not marked and is asked again


def _number(value: object) -> float | None:
    """A finite number, or None: NaN, infinity, text and booleans are not figures."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _in_range(value: float | None, bounds: tuple[float, float], name: str, notes: list[str]) -> float | None:
    if value is None:
        return None
    low, high = bounds
    if low <= value < high:
        return value
    notes.append(f"{name} {value:g} out of range")
    return None


def figures_from_info(info: dict) -> tuple[dict, list[str]]:
    """The four figures from Yahoo's `info`, as fractions, plus a note for each value refused as out of range.
    Yahoo's `yield` is already a fraction (`dividendYield` is in percent and is never read); `netExpenseRatio` is in
    percent, `annualReportExpenseRatio` a fraction. A yield in percent would pass for a huge one, so it is refused."""
    notes: list[str] = []
    net = _number(info.get("netExpenseRatio"))
    expense = net / 100 if net is not None else _number(info.get("annualReportExpenseRatio"))
    figures = {
        "trailing_pe": _number(info.get("trailingPE")),
        "forward_pe": _number(info.get("forwardPE")),
        "dividend_yield": _in_range(_number(info.get("yield")), YIELD_RANGE, "yield", notes),
        "expense_ratio": _in_range(expense, EXPENSE_RANGE, "expense ratio", notes),
    }
    return figures, notes


def _valued(figures: dict) -> bool:
    """Own figures count as the fund's when it has a P/E or a yield; an expense ratio alone does not stand for them."""
    return figures["trailing_pe"] is not None or figures["dividend_yield"] is not None


def collect_funds(
    store: Store,
    universe: list[str],
    cfg: Config,
    *,
    yahoo: YahooClient,
    now: datetime,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[str], None] | None = None,
) -> list[FundResult]:
    """Ask Yahoo about each fund in the universe not yet asked about today (a New York day), and store a snapshot.
    A fund is marked asked only when the ask did not raise, so an outage is tried again; a fund with no figures is
    marked too, so a day's run makes one call per fund. A fund whose own figures lack a P/E and a yield takes its
    `[fund_proxies]` ETF's, keeping its own expense ratio."""
    day = now.astimezone(NEW_YORK).date().isoformat()
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    secrets = (cfg.sec_user_agent, cfg.tiingo_token)
    kinds = {row["symbol"]: row for row in store.securities()}
    answers: dict[str, dict | None] = {}                    # what Yahoo said this run, so a proxy is asked once
    asked = 0

    def ask(symbol: str) -> dict | None:
        nonlocal asked
        if symbol not in answers:
            if asked:
                sleep(PAUSE_SECONDS)
            asked += 1
            answers[symbol] = yahoo.fund_info(symbol)
        return answers[symbol]

    results: list[FundResult] = []
    for i, symbol in enumerate(universe, start=1):
        row = kinds.get(symbol)
        if row is None or row["asset_type"] not in FUND_TYPES or row["last_fund_fetch"] == day:
            continue
        if progress:
            progress(f"{i}/{len(universe)} {symbol}")
        try:
            figures, notes = figures_from_info(ask(symbol) or {})
            source = SOURCE
            proxy = cfg.fund_proxies.get(symbol)
            if not _valued(figures) and proxy:
                stand_in, proxy_notes = figures_from_info(ask(proxy) or {})
                if _valued(stand_in):
                    figures = {**stand_in, "expense_ratio": figures["expense_ratio"]}       # the fund's own, never the proxy's
                    source = f"proxy: {proxy} via {SOURCE}"
                    notes += proxy_notes
        except Exception as e:      # one fund Yahoo will not answer must not stop the rest
            results.append(FundResult(symbol, None, 0, redact(f"{type(e).__name__}: {e}", secrets), failed=True))
            continue
        store.mark_fund_fetch(symbol, day)
        if all(v is None for v in figures.values()):
            results.append(FundResult(symbol, None, 0, "; ".join(["no figures", *notes])))
            continue
        written = store.write_fund_snapshot(FundSnapshot(symbol, day, source=source, **figures), stamp)
        results.append(FundResult(symbol, source, written, "; ".join(notes)))
    return results
