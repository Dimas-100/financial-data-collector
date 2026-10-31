"""Company scores against their own history: where today's P/E, P/FCF, P/S, EV/EBIT and dividend yield sit among the
company's own daily values of the last `history_years` years (a mid-rank percentile), and the band that puts it in:
`cheap`, `usual` or `premium`.

Pure: no SQL, no store. The engine hands in the symbol's `valuation_daily` rows with the checker's open holds already
applied (`engine.apply_holds`: a held value reads as None and the row lists the hold ids in `_held`), so a held day
is simply missing from the history, and a held newest day makes the score `held`.

The band reads the percentile through the metric's direction: a high P/E, P/FCF, P/S or EV/EBIT is expensive
(`HIGH_IS_PREMIUM`), a high dividend yield is cheap (`HIGH_IS_CHEAP`). With the default thresholds a value under the
25th percentile of its own history is `cheap`, over the 75th `premium`, anything between (both included) `usual`.

Beside them, `fundamental_scores` gives ten company fundamentals with no percentile or band: the growth the price
implies, 5-year free cash flow and revenue growth, the year's change in free cash flow and operating margin, net debt /
EBITDA, the buyback yield, the last earnings surprise, the 30-day change in this year's EPS estimate and net insider
buying. Each one that cannot be known is `unsupported` or `not_enough_history` and says why. Pure too, except
`read_insider`, which reads the sibling insider feed once per engine run.

`fund_scores` gives a fund (ETF, mutual fund) its trailing P/E and dividend yield against its own daily snapshots, banded
only once a year of them exists, and its expense ratio as a value.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

from ..config import EngineSettings
from ..models import EarningsEvent, ScoreRow
from ..symbols import canonical
from . import sources

HIGH_IS_PREMIUM = ("pe", "p_fcf", "ps", "ev_ebit")
HIGH_IS_CHEAP = ("dividend_yield",)
HISTORY_METRICS = ("pe", "p_fcf", "ps", "ev_ebit", "dividend_yield")
FUNDAMENTAL_METRICS = ("implied_growth", "fcf_growth_5y", "revenue_growth_5y", "fcf_margin_change", "op_margin_change",
                       "net_debt_ebitda", "buyback_yield", "last_surprise", "estimate_change_30d", "insider_net_90d")
# Each metric's own column plus the close it is built on, so a `close` hold opened after the build blanks the row in
# the valuation_scores view as the build itself does (engine.statement_holds). A fundamental read from the statements,
# the earnings events, the estimates or the insider feed names no valuation_daily column: only a `*` hold (or one on
# `score:<metric>`) blanks it in the view, and only a `*` hold holds it at build time.
INPUTS: dict[str, tuple[str, ...]] = {
    "pe": ("pe", "close"), "p_fcf": ("p_fcf", "close"), "ps": ("ps", "close"), "ev_ebit": ("market_cap", "close"),
    "dividend_yield": ("dividend_yield", "close"),
    "implied_growth": ("market_cap", "fcf_ttm", "close"), "buyback_yield": ("market_cap", "close"),
    # The margins name the valuation_daily columns the checker's `figures` check holds for their TTM figures (R11).
    "fcf_margin_change": ("revenue_ttm", "fcf_ttm"), "op_margin_change": ("revenue_ttm",),
    "fcf_growth_5y": (), "revenue_growth_5y": (), "net_debt_ebitda": (), "last_surprise": (),
    "estimate_change_30d": (), "insider_net_90d": (),
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


# ---- company fundamentals -------------------------------------------------------------------------------------------

GROWTH_RANGE = (-0.5, 1.0)            # implied growth is searched in this range, a year
GROWTH_YEARS = 10
GROWTH_TOLERANCE = 1e-6
IMPLIED_METHOD = "10 years of FCF growth, then terminal_growth, discounted at discount_rate, = market cap"
OUT_OF_RANGE = "needs more than 100% or less than -50% a year"
CAGR_YEARS = 5
YEAR_EARLIER_DAYS = (350, 380)        # the TTM row a margin is compared with ends this many days earlier
ESTIMATE_DAYS = 30                   # the `then` snapshot is 30-45 days before `now`
ESTIMATE_MAX_DAYS = 45
ESTIMATE_PERIOD = "0y"
SNAPSHOT_MAX_AGE_DAYS = 7            # a feed or snapshot older than this before the row's day is not used (R9)
INSIDER_DAYS = 90
INSIDER_FILE = "insider.json"
REPORT_BETWEEN = "a report fell between the two snapshots; the year may have rolled"


def _present_value(fcf: float, growth: float, discount_rate: float, terminal_growth: float) -> float:
    """Free cash flow growing `growth` a year for GROWTH_YEARS years, then `terminal_growth` forever, discounted."""
    pv, cash = 0.0, fcf
    for year in range(1, GROWTH_YEARS + 1):
        cash *= 1 + growth
        pv += cash / (1 + discount_rate) ** year
    terminal = cash * (1 + terminal_growth) / (discount_rate - terminal_growth)
    return pv + terminal / (1 + discount_rate) ** GROWTH_YEARS


def implied_growth(fcf: float, market_cap: float, discount_rate: float, terminal_growth: float) -> float | None:
    """The yearly free-cash-flow growth over the next GROWTH_YEARS years at which the discounted cash flows equal the
    market cap: bisection on GROWTH_RANGE to GROWTH_TOLERANCE, rounded to 4 places. None when free cash flow is not
    positive, the market cap is not positive, the discount rate does not exceed the terminal growth, or the answer lies
    outside the range."""
    if fcf is None or market_cap is None or fcf <= 0 or market_cap <= 0 or discount_rate <= terminal_growth:
        return None
    lo, hi = GROWTH_RANGE
    if not (_present_value(fcf, lo, discount_rate, terminal_growth) <= market_cap
            <= _present_value(fcf, hi, discount_rate, terminal_growth)):
        return None
    while hi - lo > GROWTH_TOLERANCE:                    # the present value rises with growth
        mid = (lo + hi) / 2
        if _present_value(fcf, mid, discount_rate, terminal_growth) < market_cap:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 4) + 0.0                 # + 0.0: never -0.0


def cagr(new: float | None, old: float | None, years: int) -> float | None:
    """Compound yearly growth from `old` to `new` over `years`; None unless both are positive."""
    if new is None or old is None or new <= 0 or old <= 0 or years <= 0:
        return None
    return (new / old) ** (1 / years) - 1


def _iso_day(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10]).isoformat()
    except ValueError:
        return None


def _text(value: object) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


def read_insider(path: Path) -> tuple[dict[str, list[dict]], str | None]:
    """The sibling insider feed: canonical symbol -> its entries, each {filed, accession, direction} (nothing else is
    read: never the feed's links), and the feed's `as_of`. An entry without a readable filing day, or that is no
    object, is skipped alone; so is a ticker whose entries are no list. Missing or unreadable -> ({}, None)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tickers = list(data["by_ticker"].items())
        as_of = _text(data.get("as_of"))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}, None
    out: dict[str, list[dict]] = {}
    for ticker, entries in tickers:
        symbol = canonical(ticker) if isinstance(ticker, str) else ""
        if not symbol or not isinstance(entries, list):
            continue
        kept = out.setdefault(symbol, [])
        for e in entries:
            if not isinstance(e, dict) or (filed := _iso_day(e.get("filed"))) is None:
                continue
            direction = _text(e.get("direction"))
            kept.append({"filed": filed, "accession": _text(e.get("accession")),
                         "direction": direction.lower() if direction else "other"})
    return out, as_of


def _days_before(day: str, n: int) -> str:
    return (date.fromisoformat(day) - timedelta(days=n)).isoformat()


def _covers(hold: dict, day: str) -> bool:
    return hold["date_from"] <= day <= (hold["date_to"] or "9999-12-31")


def _newest_problem(newest: dict | None, day: str) -> str | None:
    """Why the newest valuation row cannot be used for `day`, or None."""
    if newest is None:
        return f"no valuation row on {day}"
    if newest["date"] != day:
        return f"no valuation row on {day}; the newest is {newest['date']}"
    if newest["status"] != "ok":
        return newest["status_reason"] or newest["status"]
    return None


def _margin(t: dict, item: str) -> float | None:
    revenue, part = t.get("revenue"), t.get(item)
    if revenue is None or revenue <= 0 or part is None:
        return None
    return part / revenue


def _why_no_margin(t: dict, label: str) -> str:
    if t.get("revenue") is None or t["revenue"] <= 0:
        return f"no revenue in the TTM ending {t['period_end']}"
    return f"no {label} figure in the TTM ending {t['period_end']}"


def _year_earlier(ttm: dict[str, dict], period_end: str) -> dict | None:
    """The TTM row whose period ends YEAR_EARLIER_DAYS before `period_end`, the one nearest a year when several do."""
    end = date.fromisoformat(period_end)
    lo, hi = YEAR_EARLIER_DAYS
    found = [(abs(gap - 365), -gap, pe) for pe in ttm if lo <= (gap := (end - date.fromisoformat(pe)).days) <= hi]
    return ttm[min(found)[2]] if found else None


def fundamental_scores(symbol: str, day: str, newest: dict | None, ttm: dict[str, dict], annual: list[dict],
                       events: list[EarningsEvent], estimates: list[dict],
                       insider: tuple[list[dict] | None, str | None] | None, cik: str, s: EngineSettings, *,
                       bar: tuple | None = None, holds: list[dict] | tuple = (),
                       filing_of: Callable[[str], dict | None] | None = None) -> list[ScoreRow]:
    """The ten fundamental rows of a company on `day`, in FUNDAMENTAL_METRICS order; none has a percentile or band.

    newest: the symbol's newest valuation_daily row up to `day` with the holds applied (engine.apply_holds over
    engine.statement_holds), or None. ttm: financials_ttm rows by period_end. annual: financials_annual rows,
    ascending. events: its earnings events. estimates: its `0y` estimate snapshots ({date, eps_avg}, ascending).
    insider: None when the insider feed was not found; (None, as_of) when the feed does not name the symbol;
    (entries, as_of) otherwise. bar: the newest final bar, for the price source. holds: the symbol's open holds as the
    checker wrote them (a `close` hold keeps its field), so a row is held as the valuation_scores view would hold it:
    an `ok` row whose date an open hold covers, with field `*` or one of the row's inputs, is `held` with no value.
    filing_of: period_end -> its statement filing ({form, accn, filed}) or None.
    """
    filing_of = filing_of or (lambda period_end: None)
    holds_by_id = {h["id"]: h for h in holds}
    price_src = (sources.price(bar[4], bar[0], bar[5]),) if bar is not None else ()
    problem = _newest_problem(newest, day)
    period = None if problem else newest.get("ttm_period_end")
    t = ttm.get(period) if period else None
    no_ttm = f"no TTM statements for {period}" if period else "no TTM statements"

    def filing_src(period_end: str | None) -> tuple[dict, ...]:
        f = filing_of(period_end) if period_end else None
        return (sources.filing(cik, f["form"], f["accn"], f["filed"], period_end),) if f else ()

    def row(metric: str, value=None, status="unsupported", reason=None, src=()) -> ScoreRow:
        return ScoreRow(symbol, day, metric, value, None, None, status, reason, INPUTS[metric], tuple(src))

    def held_or(metric: str, why: str, src) -> ScoreRow:
        held = _holding(metric, newest, holds_by_id)
        return row(metric, status="held", reason="; ".join(held), src=src) if held else row(metric, reason=why, src=src)

    out: list[ScoreRow] = []

    # implied growth: the newest market cap and free cash flow
    if problem:
        out.append(row("implied_growth", reason=problem, src=price_src))
    else:
        src = price_src + filing_src(period) + (sources.config("discount_rate", s.discount_rate),
                                                sources.config("terminal_growth", s.terminal_growth),
                                                sources.derived("method", IMPLIED_METHOD))
        mc, fcf = newest.get("market_cap"), newest.get("fcf_ttm")
        if mc is None or fcf is None:
            out.append(held_or("implied_growth", NO_MARKET_CAP if mc is None else "no free cash flow figure", src))
        elif fcf <= 0:
            out.append(row("implied_growth", reason=NOT_COMPUTED["p_fcf"], src=src))
        elif mc <= 0:
            out.append(row("implied_growth", reason=NO_MARKET_CAP, src=src))
        else:
            g = implied_growth(fcf, mc, s.discount_rate, s.terminal_growth)
            out.append(row("implied_growth", g, "ok", src=src) if g is not None
                       else row("implied_growth", reason=OUT_OF_RANGE, src=src))

    # 5-year growth: the newest annual row against the one whose fiscal_year is 5 lower
    new_a = annual[-1] if annual else None
    for metric, item, label in (("fcf_growth_5y", "fcf", "free cash flow"),
                                ("revenue_growth_5y", "revenue", "revenue")):
        if new_a is None or new_a.get("fiscal_year") is None:
            out.append(row(metric, status="not_enough_history", reason="no annual statements"))
            continue
        fy, old_fy = new_a["fiscal_year"], new_a["fiscal_year"] - CAGR_YEARS
        olds = [a for a in annual if a.get("fiscal_year") == old_fy]
        if not olds:
            out.append(row(metric, status="not_enough_history",
                           reason=f"no FY{old_fy} statements to compare FY{fy} with"))
            continue
        old_a = olds[-1]
        src = filing_src(old_a["period_end"]) + filing_src(new_a["period_end"]) + (sources.derived(
            f"financials_annual {item}", f"FY{old_fy} ({old_a['period_end']})..FY{fy} ({new_a['period_end']})"),)
        g = cagr(new_a.get(item), old_a.get(item), CAGR_YEARS)
        if g is not None:
            out.append(row(metric, g, "ok", src=src))
            continue
        missing = [f"FY{y}" for y, a in ((old_fy, old_a), (fy, new_a)) if a.get(item) is None]
        why = (f"no {label} figure in {' or '.join(missing)}" if missing
               else f"{label} not positive in FY{old_fy} or FY{fy}")
        out.append(row(metric, reason=why, src=src))

    # margin changes: the TTM row in force against the one a year earlier
    for metric, item, label in (("fcf_margin_change", "fcf", "free cash flow"),
                                ("op_margin_change", "operating_income", "operating income")):
        if problem or t is None:
            out.append(row(metric, reason=problem or no_ttm))
            continue
        now_m = _margin(t, item)
        if now_m is None:
            out.append(row(metric, reason=_why_no_margin(t, label), src=filing_src(period)))
            continue
        earlier = _year_earlier(ttm, period)
        if earlier is None:
            lo, hi = YEAR_EARLIER_DAYS
            out.append(row(metric, status="not_enough_history", src=filing_src(period),
                           reason=f"no TTM statements {lo}-{hi} days before {period}"))
            continue
        then_m = _margin(earlier, item)
        then_end = earlier["period_end"]
        src = filing_src(then_end) + filing_src(period) + (sources.derived(
            f"financials_ttm {item} / revenue",
            f"{then_end} {'-' if then_m is None else round(then_m, 4)} -> {period} {round(now_m, 4)}"),)
        if then_m is None:
            out.append(row(metric, reason=_why_no_margin(earlier, label), src=src))
            continue
        out.append(row(metric, now_m - then_m, "ok", src=src))

    # net debt / EBITDA of the TTM row in force
    if problem or t is None:
        out.append(row("net_debt_ebitda", reason=problem or no_ttm))
    else:
        src = filing_src(period) + (sources.derived(
            "financials_ttm (long_term_debt - cash - short_term_investments) / "
            "(operating_income + depreciation_amortization)", period),)
        debt, cash = t.get("long_term_debt"), t.get("cash")
        oi, da = t.get("operating_income"), t.get("depreciation_amortization")
        if debt is None or cash is None:
            out.append(row("net_debt_ebitda", reason=NO_DEBT_OR_CASH, src=src))
        elif oi is None:
            out.append(row("net_debt_ebitda", reason="no operating income figure", src=src))
        elif da is None:
            out.append(row("net_debt_ebitda", reason="no depreciation and amortization figure", src=src))
        elif oi + da <= 0:
            out.append(row("net_debt_ebitda", reason="EBITDA not positive", src=src))
        else:
            out.append(row("net_debt_ebitda", (debt - cash - (t.get("short_term_investments") or 0.0)) / (oi + da),
                           "ok", src=src))

    # buyback yield: TTM buybacks over the newest market cap; a missing buybacks line is 0
    if problem:
        out.append(row("buyback_yield", reason=problem, src=price_src))
    else:
        mc = newest.get("market_cap")
        bought = t.get("buybacks") if t is not None else None
        src = price_src + filing_src(period) + (sources.derived(
            "financials_ttm buybacks / valuation_daily market_cap",
            f"{period}" + ("" if bought is not None else "; no buybacks line, counted 0")),)
        if mc is None:
            out.append(held_or("buyback_yield", NO_MARKET_CAP, src))
        elif t is None:
            out.append(row("buyback_yield", reason=no_ttm, src=src))
        elif mc <= 0:
            out.append(row("buyback_yield", reason=NO_MARKET_CAP, src=src))
        else:
            out.append(row("buyback_yield", (bought or 0.0) / mc, "ok", src=src))

    # the last earnings surprise on or before `day`
    known = [e for e in events if e.report_date <= day and e.surprise_pct is not None]
    if not known:
        out.append(row("last_surprise", reason="no earnings surprise on record"))
    else:
        e = max(known, key=lambda x: x.report_date)
        src = []
        if e.accession:
            try:
                src.append(sources.filing(cik, "8-K", e.accession, e.report_date))
            except ValueError:                           # an accession that does not validate gets no link
                pass
        src.append(sources.feed("Yahoo earnings dates", None, e.report_date))
        out.append(row("last_surprise", e.surprise_pct, "ok", src=src))

    # the change in this fiscal year's EPS estimate over 30-45 days, the newer snapshot at most 7 days old
    snaps = [(r["date"], r["eps_avg"]) for r in estimates if r["eps_avg"] is not None and r["date"] <= day]
    label = f"Yahoo EPS estimates ({ESTIMATE_PERIOD})"
    if not snaps:
        out.append(row("estimate_change_30d", reason="no EPS estimate on record"))
    else:
        now_d, now_v = snaps[-1]
        now_src = (sources.feed(label, now_d, f"{now_d} {now_v}"),)
        older = [x for x in snaps if x[0] <= _days_before(now_d, ESTIMATE_DAYS)]
        if _days_before(day, SNAPSHOT_MAX_AGE_DAYS) > now_d:
            out.append(row("estimate_change_30d", reason=f"newest estimate snapshot is from {now_d}", src=now_src))
        elif not older:
            out.append(row("estimate_change_30d", status="not_enough_history",
                           reason=f"estimate snapshots start {snaps[0][0]}", src=now_src))
        elif older[-1][0] < _days_before(now_d, ESTIMATE_MAX_DAYS):
            out.append(row("estimate_change_30d", status="not_enough_history", src=now_src,
                           reason=f"no estimate snapshot {ESTIMATE_DAYS}-{ESTIMATE_MAX_DAYS} days before {now_d}"))
        else:
            then_d, then_v = older[-1]
            src = (sources.feed(label, now_d, f"{then_d} {then_v} -> {now_d} {now_v}"),)
            if any(then_d < ev.report_date <= now_d for ev in events):
                out.append(row("estimate_change_30d", reason=REPORT_BETWEEN, src=src))
            elif then_v == 0:
                out.append(row("estimate_change_30d", reason=f"the estimate on {then_d} was 0", src=src))
            else:
                out.append(row("estimate_change_30d", (now_v - then_v) / abs(then_v), "ok", src=src))

    # net insider buying: buys minus sells filed in the 90 days ending `day`, from a feed at most 7 days old
    feed_day = _iso_day(insider[1]) if insider is not None else None
    if insider is None:
        out.append(row("insider_net_90d", reason="insider feed not found"))
    elif feed_day is None:
        out.append(row("insider_net_90d", reason="insider feed has no date",
                       src=(sources.feed(INSIDER_FILE, insider[1], None),)))
    elif feed_day < _days_before(day, SNAPSHOT_MAX_AGE_DAYS):
        out.append(row("insider_net_90d", reason=f"insider feed is from {feed_day}",
                       src=(sources.feed(INSIDER_FILE, insider[1], None),)))
    elif insider[0] is None:
        out.append(row("insider_net_90d", reason="not in the insider feed",
                       src=(sources.feed(INSIDER_FILE, insider[1], None),)))
    else:
        entries, as_of = insider
        start = (date.fromisoformat(day) - timedelta(days=INSIDER_DAYS)).isoformat()
        counted = sorted((e for e in entries if start < e["filed"] <= day and e["direction"] in ("buy", "sell")),
                         key=lambda e: (e["filed"], e["accession"] or ""))
        buys = sum(1 for e in counted if e["direction"] == "buy")
        sells = len(counted) - buys
        src = [sources.feed(INSIDER_FILE, as_of, f"{buys} buys, {sells} sells")]
        for e in counted:
            if e["accession"]:
                try:
                    src.append(sources.filing(cik, "4", e["accession"], e["filed"]))
                except ValueError:                       # counted, but no link
                    pass
        out.append(row("insider_net_90d", float(buys - sells), "ok", src=src))

    # the holds open now, as the valuation_scores view applies them: an `ok` row is held under a `*` hold covering
    # `day` or one on its inputs
    final: list[ScoreRow] = []
    for r in out:
        msgs = [h["message"] for h in holds if _covers(h, day) and (h["field"] == "*" or h["field"] in r.inputs)]
        final.append(row(r.metric, status="held", reason="; ".join(msgs), src=r.sources)
                     if r.status == "ok" and msgs else r)
    return final


# ---- funds ------------------------------------------------------------------------------------------------------------

FUND_TYPES = ("etf", "mutual_fund", "fund")
FUND_METRICS = ("pe", "dividend_yield", "expense_ratio")
FUND_COLUMNS = {"pe": "trailing_pe", "dividend_yield": "dividend_yield", "expense_ratio": "expense_ratio"}
FUND_LABELS = {"pe": "trailing P/E", "dividend_yield": "dividend yield", "expense_ratio": "expense ratio"}


def fund_scores(symbol: str, day: str, snaps: list[dict], s: EngineSettings, *,
                holds: list[dict] | tuple = ()) -> list[ScoreRow]:
    """The three fund rows on `day` (`pe`, `dividend_yield`, `expense_ratio`), from the fund's own daily snapshots.

    snaps: the symbol's fund_snapshots rows ({date, trailing_pe, dividend_yield, expense_ratio, source, fetched_at}),
    ascending. The newest one dated on or before `day` is the value; one more than SNAPSHOT_MAX_AGE_DAYS older than
    `day` makes every row `unsupported` ("newest snapshot is from <date>"), none at all "no fund snapshot yet".
    `pe` and `dividend_yield` are ranked among the snapshots with the same `source` as the newest within
    `history_years`, and get a band only once those span `min_history_days`: until then `not_enough_history` (a fund
    whose P/E is borrowed from a proxy ETF does not count the proxy's days as its own), with the value and no band.
    `expense_ratio` is a value only. Funds file no statements, so no row names a valuation_daily column; holds: the
    symbol's open holds as the checker wrote them, of which a `*` covering `day` holds an `ok` row (a hold on
    `score:<metric>` reaches the row through the valuation_scores view).
    """
    snaps = [x for x in snaps if x["date"] <= day]
    newest = snaps[-1] if snaps else None

    def row(metric: str, value=None, pct=None, status="unsupported", reason=None, src=()) -> ScoreRow:
        return ScoreRow(symbol, day, metric, value, pct, band(metric, pct, s) if metric in HISTORY_METRICS else None,
                        status, reason, (), tuple(src))

    if newest is None:
        return [row(m, reason="no fund snapshot yet") for m in FUND_METRICS]
    feed = sources.feed(f"fund_snapshots: {newest['source']}", newest["date"], newest["fetched_at"])
    if newest["date"] < _days_before(day, SNAPSHOT_MAX_AGE_DAYS):
        return [row(m, reason=f"newest snapshot is from {newest['date']}", src=(feed,)) for m in FUND_METRICS]

    start = _years_before(day, s.history_years)
    out: list[ScoreRow] = []
    for metric in FUND_METRICS:
        column = FUND_COLUMNS[metric]
        value = newest.get(column)
        if value is None:
            out.append(row(metric, reason=f"no {FUND_LABELS[metric]} in the newest snapshot", src=(feed,)))
        elif metric == "expense_ratio":
            out.append(row(metric, value, status="ok", src=(feed,)))
        else:
            own = [(x["date"], x[column]) for x in snaps
                   if x["source"] == newest["source"] and x["date"] >= start and x[column] is not None]
            first = own[0][0]
            days = (date.fromisoformat(newest["date"]) - date.fromisoformat(first)).days
            derived = sources.derived(f"fund_snapshots {column}", f"{first}..{newest['date']} ({len(own)} days)",
                                      newest["date"])
            if days < s.min_history_days:
                out.append(row(metric, value, status="not_enough_history",
                               reason=f"{days} days of its own snapshots; a band needs {s.min_history_days}",
                               src=(feed, derived, sources.config("min_history_days", s.min_history_days))))
            else:
                out.append(row(metric, value, percentile([v for _, v in own], value), "ok", src=(
                    feed, derived, sources.config("history_years", s.history_years),
                    sources.config("cheap_percentile", s.cheap_percentile),
                    sources.config("premium_percentile", s.premium_percentile))))

    # the holds open now, as the valuation_scores view applies them to an input-less row: a `*` covering `day`
    msgs = [h["message"] for h in holds if _covers(h, day) and h["field"] == "*"]
    if msgs:
        out = [row(r.metric, status="held", reason="; ".join(msgs), src=r.sources) if r.status == "ok" else r
               for r in out]
    return out
