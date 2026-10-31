"""The checks that need nothing from outside: are the statements fresh enough, is anything impossible, are
there gaps in the prices, and do the stored ratios come out again from the stored inputs.

Every check looks at the newest `valuation_daily` row of each symbol (the raw table, not the view that applies
holds) and at the symbols it covers: those in the latest positions plus those with a valuation row. The numbers
that decide a finding are in `[check]` of config.toml. The recompute uses the row's own inputs and nothing else,
so it cannot share a mistake with the code that made the row.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from . import CHECKS, CheckContext, Finding
from . import calendar as nyse

NEW_YORK = ZoneInfo("America/New_York")
NO_COMPANY_RATIOS = ("etf", "mutual_fund", "money_market", "crypto", "fund", "foreign_filer")
RATIOS = ("pe", "ps", "p_fcf", "market_cap")
LOOKBACK_BARS = 30                 # the impossible-move check reads this many of the newest final bars
GAP_WINDOW_DAYS = 60               # the gap check reads this many trading days, today excluded


def _add_months(d: date, months: int) -> date:
    """Calendar-month arithmetic; the day is clamped when the target month is shorter."""
    index = d.year * 12 + (d.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(d.day, monthrange(year, month)[1]))


def _num(x: float) -> str:
    return f"{x:.6g}"


def _covered(ctx: CheckContext) -> list[str]:
    """Held symbols plus symbols with a valuation row, in order."""
    return sorted(set(ctx.store.check_held_symbols()) | {r["symbol"] for r in ctx.store.check_newest_valuation()})


# ---- fresh -------------------------------------------------------------------------------------

def fresh(ctx: CheckContext) -> list[Finding]:
    """The newest statements must not be too old for the price they are set against: a warning after
    `fresh_warn_months`, a hold on every value after `fresh_hold_months`. One finding per symbol, dated from the
    day the threshold was crossed and still going."""
    warn_months, hold_months = ctx.cfg.check.fresh_warn_months, ctx.cfg.check.fresh_hold_months
    out: list[Finding] = []
    for r in ctx.store.check_newest_valuation():
        if r["status"] not in ("ok", "stale") or not r["ttm_period_end"]:
            continue
        end, on = date.fromisoformat(r["ttm_period_end"]), date.fromisoformat(r["date"])
        age = round((on - end).days / 30.4375)
        for severity, months, advice in (("hold", hold_months, "too old to value on"), ("warn", warn_months, "getting old")):
            crossed = _add_months(end, months)
            if on > crossed:
                out.append(Finding(
                    "fresh", r["symbol"], "*", crossed.isoformat(), None, severity,
                    f"statements end {end}, {age} months before {on}: {advice} (more than {months} months)",
                    {"ttm_period_end": str(end), "price_date": str(on), "months": age, "threshold_months": months}))
                break
    return out


# ---- impossible --------------------------------------------------------------------------------

def impossible(ctx: CheckContext) -> list[Finding]:
    """Values that cannot be right: a company ratio on something that is not a company, negative shares, or a
    one-day move in the close of more than `max_daily_move` that no split explains. Reasons that fall on the same
    symbol and day become one finding."""
    reasons: dict[tuple[str, str], dict] = {}

    def add(symbol: str, day: str, date_to: str | None, message: str, evidence: dict) -> None:
        got = reasons.setdefault((symbol, day), {"date_to": date_to, "messages": [], "evidence": {}})
        got["date_to"] = None if (got["date_to"] is None or date_to is None) else max(got["date_to"], date_to)
        got["messages"].append(message)
        got["evidence"].update(evidence)

    for r in ctx.store.check_newest_valuation():
        kind = r["asset_type"]
        set_ratios = [n for n in RATIOS if r[n] is not None]
        if kind in NO_COMPANY_RATIOS and set_ratios:
            add(r["symbol"], r["date"], None, f"{kind} has no company ratios, but {', '.join(set_ratios)} "
                f"{'is' if len(set_ratios) == 1 else 'are'} set", {"asset_type": kind, "ratios": set_ratios})
        if r["shares"] is not None and r["shares"] < 0:
            add(r["symbol"], r["date"], None, f"shares are negative ({_num(r['shares'])})", {"shares": r["shares"]})
    limit = ctx.cfg.check.max_daily_move
    for symbol in _covered(ctx):
        bars = ctx.store.check_final_bars(symbol, LOOKBACK_BARS)
        for (d0, c0, _), (d1, c1, split) in zip(bars, bars[1:]):
            if split != 1 or not c0 > 0:
                continue                                        # a split day moves the close for a reason
            move = c1 / c0 - 1
            if abs(move) > limit:
                add(symbol, d1, d1, f"close moved {move:+.0%} in one day, {_num(c0)} on {d0} to {_num(c1)} on {d1}, "
                    f"with no split", {"move": round(move, 6), "from_date": d0, "from_close": c0,
                                       "to_date": d1, "to_close": c1})
    return [Finding("impossible", symbol, "*", day, got["date_to"], "hold", "; ".join(got["messages"]), got["evidence"])
            for (symbol, day), got in sorted(reasons.items())]


# ---- gaps --------------------------------------------------------------------------------------

def gaps(ctx: CheckContext) -> list[Finding]:
    """A held symbol should have a final close on every NYSE trading day of the last 60 (today excluded),
    counted from its first price and up to, not including, its newest final bar: a day after that is one no sync
    has fetched yet (yesterday, before the evening sync), not a hole. One warning per symbol naming the first
    missing day."""
    end = ctx.now.astimezone(NEW_YORK).date() - timedelta(days=1)
    window = nyse.trading_days(end - timedelta(days=130), end)[-GAP_WINDOW_DAYS:]
    out: list[Finding] = []
    for symbol in ctx.store.check_held_symbols():
        first = ctx.store.check_first_price_date(symbol)
        newest = ctx.store.check_newest_final_bar(symbol)
        if first is None or newest is None:
            continue
        days = [d.isoformat() for d in window if first <= d.isoformat() < newest[0]]
        if not days:
            continue
        have = ctx.store.check_final_dates(symbol, days[0], days[-1])
        missing = [d for d in days if d not in have]
        if missing:
            n = len(missing)
            out.append(Finding(
                "gaps", symbol, "close", missing[0], missing[-1], "warn",
                f"no final closing price on {n} trading day{'s' if n != 1 else ''} of the last {GAP_WINDOW_DAYS}, "
                f"the first on {missing[0]}", {"missing": missing, "window_start": days[0], "window_end": days[-1]}))
    return out


# ---- recompute ---------------------------------------------------------------------------------

def _ratio(num: float | None, den: float | None) -> float | None:
    return num / den if (num is not None and den is not None and den > 0) else None


def _recomputed(r) -> dict[str, tuple[float | None, dict, str]]:
    """metric -> (value worked out from the row's own inputs, those inputs, the formula in words)."""
    close, shares, eps = r["close"], r["shares"], r["eps_ttm"]
    cap = close * shares if (close is not None and shares is not None) else None
    pick = lambda *names: {n: r[n] for n in names}      # noqa: E731
    return {
        "pe": (_ratio(close, eps), pick("close", "eps_ttm"), "close / EPS"),
        "ps": (_ratio(cap, r["revenue_ttm"]), pick("close", "shares", "revenue_ttm"), "close x shares / revenue"),
        "p_fcf": (_ratio(cap, r["fcf_ttm"]), pick("close", "shares", "fcf_ttm"), "close x shares / free cash flow"),
        "market_cap": (cap, pick("close", "shares"), "close x shares"),
        "dividend_yield": (_ratio(r["dividends_12m"], close), pick("dividends_12m", "close"), "dividends / close"),
    }


def recompute(ctx: CheckContext) -> list[Finding]:
    """Work each stored ratio out again from the row's own inputs. A stored value more than
    `recompute_tolerance` away from it (relative to the recomputed one), or stored when the inputs cannot give
    one, is held. Only rows with status 'ok' are looked at; a value that is not stored is not compared."""
    tolerance = ctx.cfg.check.recompute_tolerance
    out: list[Finding] = []
    for r in ctx.store.check_newest_valuation():
        if r["status"] != "ok":
            continue
        for metric, (value, inputs, formula) in _recomputed(r).items():
            stored = r[metric]
            if stored is None:
                continue
            if value is None:
                message = (f"{metric} is stored as {_num(stored)}, but the row's own inputs ({formula}) cannot give "
                           f"one")
            else:
                apart = abs(stored - value) / abs(value) if value else (0.0 if stored == 0 else float("inf"))
                if apart <= tolerance:
                    continue
                message = (f"{metric} is stored as {_num(stored)}, but {formula} works out to {_num(value)}")
            out.append(Finding("recompute", r["symbol"], metric, r["date"], None, "hold", message,
                               {"stored": stored, "recomputed": value, "inputs": inputs}))
    return out


CHECKS["fresh"] = fresh
CHECKS["impossible"] = impossible
CHECKS["gaps"] = gaps
CHECKS["recompute"] = recompute
