"""Point-in-time valuation series: price x the TTM figures that had been filed by that date."""
from __future__ import annotations

from datetime import date, timedelta

from ..store import Store

COLUMNS = (
    "symbol", "date", "close", "shares", "market_cap", "revenue_ttm", "net_income_ttm", "ocf_ttm", "fcf_ttm",
    "eps_ttm", "pe", "ps", "p_fcf", "dividends_12m", "dividend_yield", "ttm_period_end", "available_from",
)
DIVIDEND_WINDOW_DAYS = 365


def _ratio(num: float | None, den: float | None) -> float | None:
    return num / den if (num is not None and den is not None and den > 0) else None


def build(symbol: str, prices: list[tuple[str, float, float]], ttm: list[dict]) -> list[tuple]:
    """One row per price date from the first filing onward.

    prices: ascending (date, close, dividend). ttm: rows of financials_ttm with
    period_end, available_from, revenue, net_income, ocf, fcf, eps_diluted,
    shares_outstanding, shares_diluted. The TTM row in force on a date is the one
    with the greatest available_from <= date.
    """
    if not ttm:
        return []
    ttm = sorted(ttm, key=lambda t: (t["available_from"], t["period_end"]))
    rows: list[tuple] = []
    i = 0
    current: dict | None = None
    window: list[tuple[str, float]] = []
    div_sum = 0.0
    for d, close, dividend in prices:
        while i < len(ttm) and ttm[i]["available_from"] <= d:
            current = ttm[i]
            i += 1
        if dividend:
            window.append((d, dividend))
            div_sum += dividend
        cutoff = (date.fromisoformat(d) - timedelta(days=DIVIDEND_WINDOW_DAYS)).isoformat()
        while window and window[0][0] <= cutoff:
            div_sum -= window.pop(0)[1]
        if current is None:
            continue
        shares = current.get("shares_outstanding") or current.get("shares_diluted")
        market_cap = close * shares if shares else None
        eps = current.get("eps_diluted")
        if eps is None and current.get("net_income") is not None and shares:
            eps = current["net_income"] / shares
        pe = close / eps if (eps is not None and eps > 0) else None
        rows.append((
            symbol, d, close, shares, market_cap,
            current.get("revenue"), current.get("net_income"), current.get("ocf"), current.get("fcf"),
            eps, pe, _ratio(market_cap, current.get("revenue")), _ratio(market_cap, current.get("fcf")),
            round(div_sum, 10), (div_sum / close) if close else None,
            current["period_end"], current["available_from"],
        ))
    return rows


def rebuild(store: Store) -> int:
    """Rebuild valuation_daily for every symbol with TTM statements. Returns symbols written."""
    rows: list[tuple] = []
    symbols = 0
    for symbol, ttm in store.ttm_by_symbol().items():
        built = build(symbol, store.prices_series(symbol), ttm)
        if built:
            symbols += 1
            rows.extend(built)
    store.replace_rows("valuation_daily", COLUMNS, rows)
    return symbols
