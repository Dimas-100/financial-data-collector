"""Company scores against their own history: where today's P/E, P/FCF, P/S, EV/EBIT and dividend yield sit among the
company's own daily values of the last `history_years` years (a mid-rank percentile), and the band that puts it in:
`cheap`, `usual` or `premium`.

Pure: no SQL, no store. The engine hands in the symbol's `valuation_daily` rows with the checker's open holds already
applied (`engine.apply_holds`: a held value reads as None and the row lists the hold ids in `_held`), so a held day
is simply missing from the history, and a held newest day makes the score `held`.

The band reads the percentile through the metric's direction: a high P/E, P/FCF, P/S or EV/EBIT is expensive
(`HIGH_IS_PREMIUM`), a high dividend yield is cheap (`HIGH_IS_CHEAP`). With the default thresholds a value under the
25th percentile of its own history is `cheap`, over the 75th `premium`, anything between (both included) `usual`.
"""
from __future__ import annotations

from datetime import date

from ..config import EngineSettings
from ..models import ScoreRow
from . import sources

HIGH_IS_PREMIUM = ("pe", "p_fcf", "ps", "ev_ebit")
HIGH_IS_CHEAP = ("dividend_yield",)
HISTORY_METRICS = ("pe", "p_fcf", "ps", "ev_ebit", "dividend_yield")
INPUTS: dict[str, tuple[str, ...]] = {
    "pe": ("pe",), "p_fcf": ("p_fcf",), "ps": ("ps",), "ev_ebit": ("market_cap",),
    "dividend_yield": ("dividend_yield",),
}
NOT_COMPUTED = {                      # why an `ok` row has no value when no hold blanked it
    "pe": "earnings not positive",
    "p_fcf": "free cash flow not positive",
    "ps": "no revenue",
    "dividend_yield": "no dividend",
}
NO_DEBT_OR_CASH = "no debt or cash figure in its filings"
NO_OPERATING_INCOME = "operating income not positive"
NO_MARKET_CAP = "no market cap"


def percentile(history: list[float], value: float) -> float:
    """Mid-rank percentile of `value` in `history` (which counts `value` itself): 100 * (below + equal / 2) / n,
    rounded to one decimal."""
    if not history:
        raise ValueError("percentile of an empty history")
    below = sum(1 for h in history if h < value)
    equal = sum(1 for h in history if h == value)
    return round(100 * (below + 0.5 * equal) / len(history), 1)


def band(metric: str, pct: float | None, s: EngineSettings) -> str | None:
    """`cheap`, `usual` or `premium` for a percentile of `metric`; None without a percentile."""
    if metric not in HIGH_IS_PREMIUM and metric not in HIGH_IS_CHEAP:
        raise ValueError(f"{metric!r} has no band direction")
    if pct is None:
        return None
    if metric in HIGH_IS_PREMIUM:
        if pct < s.cheap_percentile:
            return "cheap"
        if pct > s.premium_percentile:
            return "premium"
        return "usual"
    if pct > 100 - s.cheap_percentile:
        return "cheap"
    if pct < 100 - s.premium_percentile:
        return "premium"
    return "usual"


def ev_ebit(market_cap: float | None, debt: float | None, cash: float | None, short_term: float | None,
            operating_income: float | None) -> float | None:
    """(market cap + long-term debt - cash - short-term investments) / operating income. None without a market cap,
    a debt or a cash figure, or when operating income is missing or not positive; missing short-term investments
    count 0."""
    if market_cap is None or debt is None or cash is None:
        return None
    if operating_income is None or operating_income <= 0:
        return None
    return (market_cap + debt - cash - (short_term or 0.0)) / operating_income


def _years_before(day: str, years: int) -> str:
    d = date.fromisoformat(day)
    try:
        return d.replace(year=d.year - years).isoformat()
    except ValueError:                                   # 29 February
        return d.replace(year=d.year - years, day=28).isoformat()


def _ttm_of(row: dict, ttm: dict[str, dict]) -> dict:
    return ttm.get(row.get("ttm_period_end") or "") or {}


def _value(metric: str, row: dict, ttm: dict[str, dict]) -> float | None:
    if metric == "ev_ebit":
        t = _ttm_of(row, ttm)
        return ev_ebit(row.get("market_cap"), t.get("long_term_debt"), t.get("cash"), t.get("short_term_investments"),
                       t.get("operating_income"))
    return row.get(metric)


def _why_none(metric: str, row: dict, ttm: dict[str, dict]) -> str:
    if metric != "ev_ebit":
        return NOT_COMPUTED[metric]
    t = _ttm_of(row, ttm)
    if row.get("market_cap") is None:
        return NO_MARKET_CAP
    if t.get("long_term_debt") is None or t.get("cash") is None:
        return NO_DEBT_OR_CASH
    return NO_OPERATING_INCOME


def _holding(metric: str, row: dict, holds_by_id: dict[int, dict]) -> list[str]:
    """The messages of the holds that blanked this metric's input on `row` (a hold id the caller did not describe
    counts, so an unknown hold errs toward held)."""
    out = []
    for hid in row.get("_held") or ():
        h = holds_by_id.get(hid)
        if h is None:
            out.append(f"hold #{hid}")
        elif h["field"] == "*" or h["field"] in INPUTS[metric]:
            out.append(h["message"])
    return out


def _label(metric: str) -> str:
    if metric == "ev_ebit":
        return "valuation_daily market_cap, financials_ttm debt, cash, operating income"
    return f"valuation_daily {metric}"


def history_scores(symbol: str, day: str, rows: list[dict], ttm: dict[str, dict], filing: dict | None, cik: str,
                   s: EngineSettings, *, bar: tuple | None = None, holds: list[dict] | tuple = ()) -> list[ScoreRow]:
    """The five history rows of a company on `day`, in HISTORY_METRICS order.

    rows: the symbol's held valuation_daily rows, ascending (rows after `day` are ignored). ttm: its company's
    financials_ttm rows by period_end. filing: the statement filing ({form, accn, filed}) of the newest row's
    ttm_period_end, or None. bar: the newest final bar (date, close, dividend, split_factor, source, fetched_at), for
    the price source. holds: the open holds that were applied (id, field, message), for a held row's reason.
    """
    rows = [r for r in rows if r["date"] <= day]
    newest = rows[-1] if rows else None
    base: list[dict] = []
    if bar is not None:
        base.append(sources.price(bar[4], bar[0], bar[5]))
    if filing is not None and newest is not None and newest.get("ttm_period_end"):
        base.append(sources.filing(cik, filing["form"], filing["accn"], filing["filed"], newest["ttm_period_end"]))
    holds_by_id = {h["id"]: h for h in holds}
    start = _years_before(day, s.history_years)

    def row(metric: str, value=None, pct=None, status="unsupported", reason=None, extra=()) -> ScoreRow:
        return ScoreRow(symbol, day, metric, value, pct, band(metric, pct, s), status, reason, INPUTS[metric],
                        tuple(base) + tuple(extra))

    out: list[ScoreRow] = []
    for metric in HISTORY_METRICS:
        if newest is None or newest["date"] != day:
            why = f"no valuation row on {day}" + (f"; the newest is {newest['date']}" if newest else "")
            out.append(row(metric, reason=why))
            continue
        if newest["status"] != "ok":
            out.append(row(metric, reason=newest["status_reason"]))
            continue
        value = _value(metric, newest, ttm)
        if value is None:
            held = _holding(metric, newest, holds_by_id)
            out.append(row(metric, status="held", reason="; ".join(held)) if held
                       else row(metric, reason=_why_none(metric, newest, ttm)))
            continue
        history = [(r["date"], v) for r in rows
                   if r["date"] >= start and r["status"] == "ok" and (v := _value(metric, r, ttm)) is not None]
        first = history[0][0]
        span = f"{first}..{day} ({len(history)} days)"
        derived = sources.derived(_label(metric), span, day)
        days = (date.fromisoformat(day) - date.fromisoformat(first)).days
        if days < s.min_history_days:
            out.append(row(metric, value, status="not_enough_history",
                           reason=f"{days} days of its own history; a band needs {s.min_history_days}",
                           extra=(derived, sources.config("min_history_days", s.min_history_days))))
            continue
        pct = percentile([v for _, v in history], value)
        out.append(row(metric, value, pct, "ok", extra=(
            derived, sources.config("history_years", s.history_years),
            sources.config("cheap_percentile", s.cheap_percentile),
            sources.config("premium_percentile", s.premium_percentile))))
    return out
