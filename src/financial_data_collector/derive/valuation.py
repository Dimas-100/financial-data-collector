"""Point-in-time valuation series: price x the TTM figures that had been filed by that date.

Share counts and per-share figures are stated in the share terms of the statement's
period end; every split between that period end and the price date rescales them, and
trailing dividends are restated in the price date's share terms.
"""
from __future__ import annotations

from bisect import bisect_right
from calendar import monthrange
from datetime import date, timedelta

from ..config import Config, ShareClass
from ..store import Store

COLUMNS = (
    "symbol", "date", "close", "shares", "market_cap", "revenue_ttm", "net_income_ttm", "ocf_ttm", "fcf_ttm",
    "eps_ttm", "pe", "ps", "p_fcf", "dividends_12m", "dividend_yield", "ttm_period_end", "available_from",
    "status", "status_reason",
)
DIVIDEND_WINDOW_DAYS = 365
STALE_MONTHS = 15
UNSUPPORTED = {
    "foreign_filer": "foreign filer: IFRS statements not read yet",
    "fund": "fund: no company statements",
}
NO_SHARE_COUNT = "share count not in its filings"


def _add_months(d: date, months: int) -> date:
    """Calendar-month arithmetic; the day is clamped when the target month is shorter."""
    index = d.year * 12 + (d.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(d.day, monthrange(year, month)[1]))


def _ratio(num: float | None, den: float | None) -> float | None:
    return num / den if (num is not None and den is not None and den > 0) else None


def build(symbol: str, prices: list[tuple[str, float, float, float]], ttm: list[dict],
          share_class: ShareClass | None = None) -> list[tuple]:
    """One row per price date from the first filing onward.

    prices: ascending (date, close, dividend, split_factor). ttm: rows of financials_ttm
    with period_end, available_from, revenue, net_income, ocf, fcf, eps_diluted,
    shares_outstanding, shares_diluted. The TTM row in force on a date is the one with
    the greatest available_from <= date. When that row's period_end is more than STALE_MONTHS
    calendar months before the price date, the row is "stale": close, dividends and the period
    markers stay, every figure that comes from the statements is NULL.

    When the TTM row in force (and not stale) has neither a share count nor EPS, the row is
    "unsupported": the value stays NULL and the status says why.

    share_class: the company states shares and EPS in another class's terms (config [share_classes]).
    EPS is divided by the ratio and shares multiplied by it, before the split scaling.
    """
    if not ttm:
        return []
    ttm = sorted(ttm, key=lambda t: (t["available_from"], t["period_end"]))
    # cumulative split product S(d): product of split factors of bars dated <= d
    split_dates: list[str] = []
    split_cum: list[float] = []
    cum = 1.0
    for d, _, _, split in prices:
        if split and split > 0 and split != 1:
            cum *= split
            split_dates.append(d)
            split_cum.append(cum)

    def s_at(d: str) -> float:
        i = bisect_right(split_dates, d) - 1
        return split_cum[i] if i >= 0 else 1.0

    rows: list[tuple] = []
    i = 0
    current: dict | None = None
    window: list[tuple[str, float]] = []  # (date, dividend x S(date)) so the sum restates cleanly
    div_sum = 0.0
    for d, close, dividend, _ in prices:
        while i < len(ttm) and ttm[i]["available_from"] <= d:
            current = ttm[i]
            i += 1
        s_now = s_at(d)
        if dividend:
            window.append((d, dividend * s_at(d)))
            div_sum += dividend * s_at(d)
        cutoff = (date.fromisoformat(d) - timedelta(days=DIVIDEND_WINDOW_DAYS)).isoformat()
        while window and window[0][0] <= cutoff:
            div_sum -= window.pop(0)[1]
        if current is None:
            continue
        dividends_12m = round(div_sum / s_now, 10)
        yield_ = (dividends_12m / close) if close else None
        if date.fromisoformat(d) > _add_months(date.fromisoformat(current["period_end"]), STALE_MONTHS):
            rows.append((
                symbol, d, close, None, None, None, None, None, None, None, None, None, None,
                dividends_12m, yield_, current["period_end"], current["available_from"],
                "stale", f"newest statements end {current['period_end']}",
            ))
            continue
        shares = current.get("shares_outstanding") or current.get("shares_diluted")
        eps = current.get("eps_diluted")
        if not shares and eps is None:
            rows.append(_unsupported_row(symbol, d, close, NO_SHARE_COUNT))
            continue
        if share_class:
            shares = shares * share_class.ratio if shares else None
            eps = eps / share_class.ratio if eps is not None else None
        scale = s_now / s_at(current["period_end"])  # splits since the statement's period end
        shares = shares * scale if shares else None
        market_cap = close * shares if shares else None
        eps = eps / scale if eps is not None else None
        if eps is None and current.get("net_income") is not None and shares:
            eps = current["net_income"] / shares
        pe = close / eps if (eps is not None and eps > 0) else None
        rows.append((
            symbol, d, close, shares, market_cap,
            current.get("revenue"), current.get("net_income"), current.get("ocf"), current.get("fcf"),
            eps, pe, _ratio(market_cap, current.get("revenue")), _ratio(market_cap, current.get("fcf")),
            dividends_12m, yield_, current["period_end"], current["available_from"], "ok", None,
        ))
    return rows


def _unsupported_row(symbol: str, d: str, close: float, reason: str) -> tuple:
    """A row holding only the close and why no valuation is made (everything between is NULL)."""
    return (symbol, d, close, *((None,) * 14), "unsupported", reason)


def unsupported(symbol: str, prices: list[tuple[str, float, float, float]], reason: str) -> list[tuple]:
    """One row per price date holding only the close and why no valuation is made."""
    return [_unsupported_row(symbol, d, close, reason) for d, close, _, _ in prices]


def rebuild(store: Store, cfg: Config) -> int:
    """Rebuild valuation_daily for every symbol whose company has TTM statements, plus a row per
    price date, status 'unsupported', for foreign filers and funds (which have no TTM rows).
    cfg.share_classes restates a configured symbol's per-share figures. Returns symbols written."""
    ttm_by_cik = store.ttm_by_cik()
    rows: list[tuple] = []
    symbols = 0
    for symbol, cik, asset_type in store.securities_with_cik():
        prices = store.prices_series(symbol, final_only=True)
        if asset_type in UNSUPPORTED:
            built = unsupported(symbol, prices, UNSUPPORTED[asset_type])
        else:
            ttm = ttm_by_cik.get(cik)
            if not ttm:
                continue
            built = build(symbol, prices, ttm, cfg.share_classes.get(symbol))
        if built:
            symbols += 1
            rows.extend(built)
    store.replace_rows("valuation_daily", COLUMNS, rows)
    return symbols
