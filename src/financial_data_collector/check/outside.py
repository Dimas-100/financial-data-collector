"""Second opinions from outside: is the newest close what another provider says, and are our figures near
Yahoo's?

`prices2` compares the newest final close of every held symbol with the OTHER provider than the one the bar
came from (Tiingo bar -> Yahoo; Yahoo bar -> Tiingo). `figures` compares trailing EPS, market cap and revenue with
Yahoo's, and our free cash flow with the four quarters of Yahoo's cash flow statement that end on our TTM's period end
(operating cash flow less purchases of property and equipment, as ours is; Yahoo's `freeCashflow` is a levered figure
and is never compared). Neither ever holds on an outside failure: when a provider cannot answer, the
symbol gets an `info` finding and keeps the holds it already had (not asked is not cleared).

`OutsideSource` is the seam: `YahooSource` is the real one (yfinance, imported only when it is first asked), and
tests pass a fake. The Tiingo side goes through `ctx.fetch` with the configured token.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

from ..connections.redact import redact
from ..symbols import to_tiingo, to_yfinance
from . import CHECKS, CheckContext, Finding
from .findings import still_open

TIINGO_DAY_URL = "https://api.tiingo.com/tiingo/daily/{sym}/prices?startDate={on}&endDate={on}"
PAUSE_SECONDS = 0.5                     # between symbols that are asked about
NOT_COVERED = frozenset({"money_market", "crypto"})
SPLIT_TOLERANCE = 0.10                  # the two closes differ by the split factor to within this fraction


@dataclass(frozen=True)
class Quote:
    close: float                        # that day's raw (unadjusted) close
    date: str


@dataclass(frozen=True)
class CashFlowQuarter:
    """One quarter of Yahoo's cash flow statement as it states it (spending negative); None where it is blank."""
    period_end: str
    operating: float | None
    ppe: float | None                   # "Purchase Of PPE"
    capex: float | None                 # "Capital Expenditure": may hold more than PPE (leased equipment, intangibles)


@dataclass(frozen=True)
class Figures:
    trailing_eps: float | None
    market_cap: float | None
    revenue: float | None
    free_cash_flow: float | None        # info's freeCashflow: levered (after interest), kept but never compared
    quarters: tuple[CashFlowQuarter, ...] = ()     # newest first


class OutsideSource(Protocol):
    def quote(self, symbol: str, on: str) -> Quote | None:
        """The raw close on `on` (YYYY-MM-DD), or None when the provider has no bar for that day."""

    def figures(self, symbol: str) -> Figures | None:
        """The provider's headline figures, or None when it has none."""


def _number(x: object) -> float | None:
    try:
        v = float(x)                   # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


class YahooSource:
    """Yahoo through yfinance, which is imported when first needed: tests and offline runs never load it.
    With `auto_adjust=False` the Close is split-adjusted but not dividend-adjusted, so it equals the day's
    printed close for the newest bar, the only one the checks ask about. yfinance logs each failed call as an
    error of its own; those lines are silenced, since a failure comes back to the check as an exception or None."""

    def quote(self, symbol: str, on: str) -> Quote | None:
        import yfinance as yf
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        end = (date.fromisoformat(on) + timedelta(days=1)).isoformat()
        hist = yf.Ticker(to_yfinance(symbol)).history(start=on, end=end, auto_adjust=False)
        if hist is None or len(hist) == 0:
            return None
        close = _number(hist["Close"].iloc[-1])
        return None if close is None else Quote(close, hist.index[-1].strftime("%Y-%m-%d"))

    def figures(self, symbol: str) -> Figures | None:
        import yfinance as yf
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        ticker = yf.Ticker(to_yfinance(symbol))
        info = ticker.info or {}
        figs = Figures(_number(info.get("trailingEps")), _number(info.get("marketCap")),
                       _number(info.get("totalRevenue")), _number(info.get("freeCashflow")),
                       _quarters_of(ticker.quarterly_cashflow))
        return None if figs == Figures(None, None, None, None) else figs


def _quarters_of(frame) -> tuple[CashFlowQuarter, ...]:
    """Yahoo's quarterly cash flow frame (rows by line name, a column per quarter end) as quarters, newest first."""
    if frame is None or len(getattr(frame, "columns", ())) == 0:
        return ()

    def cell(line: str, col) -> float | None:
        return _number(frame.at[line, col]) if line in frame.index else None

    out = [CashFlowQuarter(col.strftime("%Y-%m-%d"), cell("Operating Cash Flow", col), cell("Purchase Of PPE", col),
                           cell("Capital Expenditure", col)) for col in frame.columns]
    return tuple(sorted(out, key=lambda q: q.period_end, reverse=True))


def default_source() -> OutsideSource:
    return YahooSource()


# ---- helpers ---------------------------------------------------------------------------------------

def _num(x: float) -> str:
    return f"{x:.6g}"


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _failure(ctx: CheckContext, check_name: str, symbol: str, field: str, date_from: str, why: object) -> list[Finding]:
    """An outside source could not answer: one `info` finding, plus the symbol's open holds and warnings of
    this check so recording the run leaves them open. An info never takes the key of a finding kept open."""
    note = redact(why, [ctx.cfg.tiingo_token])
    kept = still_open(ctx.store, check_name, symbol)
    info = Finding(check_name, symbol, field, date_from, None, "info", note, {})
    return kept if any((k.field, k.date_from) == (field, date_from) for k in kept) else kept + [info]


def _covered(ctx: CheckContext) -> list[str]:
    types = ctx.store.check_asset_types()
    return [s for s in ctx.store.check_held_symbols() if types.get(s) not in NOT_COVERED]


def _price_covered(ctx: CheckContext) -> list[str]:
    """Held symbols plus the benchmarks (a wrong SPY close would skew every measure built on it), each once."""
    types = ctx.store.check_asset_types()
    symbols = set(ctx.store.check_held_symbols()) | set(ctx.store.check_benchmark_symbols())
    return sorted(s for s in symbols if types.get(s) not in NOT_COVERED)


def _split_adjusted(ours: float, theirs: float, factor: float) -> tuple[float, float]:
    """On a split day one provider may still show the pre-split price: divide the pre-split side by the factor.
    Which side that is shows in the ratio, whichever way the split went."""
    if factor != 1 and factor > 0 and ours > 0 and theirs > 0:
        r = ours / theirs
        if abs(r / factor - 1) <= SPLIT_TOLERANCE:
            return ours / factor, theirs
        if abs(1 / r / factor - 1) <= SPLIT_TOLERANCE:
            return ours, theirs / factor
    return ours, theirs


def _difference(ours: float, theirs: float, factor: float, warn: float) -> tuple[float, bool]:
    """How far apart two closes are, as a fraction of `theirs`, and whether a split explains part of it. The raw
    closes are compared first; the split is allowed in only when the raw difference is over `warn` and the
    split-adjusted one is smaller (a 1.05 stock dividend must not turn two equal closes into a 5% difference)."""
    raw = abs(ours - theirs) / abs(theirs)
    if raw <= warn or factor == 1:
        return raw, False
    a, b = _split_adjusted(ours, theirs, factor)
    adjusted = abs(a - b) / abs(b)
    return (adjusted, True) if adjusted < raw else (raw, False)


def _tiingo_quote(ctx: CheckContext, symbol: str, on: str) -> Quote | None:
    raw = ctx.fetch(TIINGO_DAY_URL.format(sym=to_tiingo(symbol), on=on),
                    {"Authorization": f"Token {ctx.cfg.tiingo_token}", "Accept": "application/json"})
    data = json.loads(raw or b"[]")
    for o in data if isinstance(data, list) else []:
        close = _number(o.get("close")) if isinstance(o, dict) else None
        if close is not None:
            return Quote(close, str(o.get("date"))[:10])
    return None


# ---- prices2 ---------------------------------------------------------------------------------------

def second_price(ctx: CheckContext) -> list[Finding]:
    """The newest final close of each held symbol and benchmark against another provider's. Over `price_hold` apart
    is a hold on every value ("*"); over `price_warn` is a warning on the close."""
    cfg = ctx.cfg.check
    out: list[Finding] = []
    asked = 0
    for symbol in _price_covered(ctx):
        bar = ctx.store.check_newest_final_bar(symbol)
        if bar is None:
            continue
        on, ours, factor, source = bar
        if source == "yfinance" and not ctx.cfg.tiingo_token:
            out += _failure(ctx, "prices2", symbol, "close", on,
                            "no second source: the close came from Yahoo and there is no Tiingo token to check it with")
            continue
        if asked:
            ctx.sleep(PAUSE_SECONDS)
        asked += 1
        other = "Tiingo" if source == "yfinance" else "Yahoo"
        try:
            quote = _tiingo_quote(ctx, symbol, on) if source == "yfinance" else ctx.outside.quote(symbol, on)
            if quote is None:
                raise LookupError(f"{other} has no close for {on}")
            if quote.date != on:
                raise LookupError(f"{other} answered for {quote.date}, not {on}")
        except Exception as e:           # one symbol an outside source will not answer must not stop the rest
            out += _failure(ctx, "prices2", symbol, "close", on, f"could not ask {other} for the close: {type(e).__name__}: {e}")
            continue
        if quote.close == 0:
            out += _failure(ctx, "prices2", symbol, "close", on, f"not compared: {other}'s close for {on} is 0")
            continue
        diff, split_used = _difference(ours, quote.close, factor, cfg.price_warn)
        evidence = {"ours": ours, "theirs": quote.close, "source": source, "other": other.lower(), "date": on,
                    "difference": round(diff, 6), "split_factor": factor, "split_adjusted": split_used}
        text = f"close {_num(ours)} on {on} ({source}) is {_pct(diff)} from {other}'s {_num(quote.close)}"
        if diff > cfg.price_hold:
            out.append(Finding("prices2", symbol, "*", on, None, "hold", text + f" (more than {_pct(cfg.price_hold)})", evidence))
        elif diff > cfg.price_warn:
            out.append(Finding("prices2", symbol, "close", on, None, "warn", text + f" (more than {_pct(cfg.price_warn)})", evidence))
    return out


# ---- figures ---------------------------------------------------------------------------------------

FIELDS = (("eps_ttm", "trailing_eps"), ("market_cap", "market_cap"), ("revenue_ttm", "revenue"))
# (our valuation column, the outside figure); fcf_ttm is compared with Yahoo's quarters (`_yahoo_fcf`)
DEPENDENTS = {"eps_ttm": ("pe",), "revenue_ttm": ("ps",), "market_cap": ("shares", "ps", "p_fcf"),
              "fcf_ttm": ("p_fcf",)}
# what is built on a figure, or it on: a hold on the figure holds them too, or they would show a number built on
# it. Our market cap is close x shares, so a market cap far from Yahoo's means the share count is suspect too.
SMALL_EPS = 0.25                # EPS this small on both sides: a few cents of rounding is a big fraction, warn only
QUARTER_MATCH_DAYS = 10         # Yahoo labels a quarter by month end; SEC's period end of a 52-53 week year is days off
QUARTER_DAYS = (75, 105)        # consecutive quarters are this many days apart
PPE_LINE, CAPEX_LINE = "Purchase Of PPE", "Capital Expenditure"


def _warn_only(column: str, ours: float, theirs: float, capex_line: str | None = None) -> str | None:
    """Why a comparison may warn but never hold, or None. Yahoo's Capital Expenditure may hold more than purchases of
    property and equipment (ours), so a free cash flow built on it can differ by definition alone."""
    if column == "fcf_ttm" and capex_line != PPE_LINE:
        return "Yahoo's capital expenditure may count more than purchases of property and equipment"
    if column == "eps_ttm" and max(abs(ours), abs(theirs)) < SMALL_EPS:
        return "EPS this small is mostly rounding"
    return None


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(a) - date.fromisoformat(b)).days


def _yahoo_fcf(quarters: tuple[CashFlowQuarter, ...], period_end: str | None) -> tuple[float | None, str, list[str]]:
    """Yahoo's free cash flow over the four quarters ending on our TTM's period end (within QUARTER_MATCH_DAYS):
    operating cash flow less purchases of property and equipment when it states them for all four, else less its
    capital expenditure. Returns (value, the spending line used, the quarter ends) or (None, why not, [])."""
    if not period_end:
        return None, "our TTM has no period end", []
    match = [i for i, q in enumerate(quarters) if abs(_days(q.period_end, period_end)) <= QUARTER_MATCH_DAYS]
    if not match:
        return None, f"Yahoo's newest quarter ends {quarters[0].period_end}, ours {period_end}", []
    four = quarters[match[0]:match[0] + 4]
    ends = [q.period_end for q in four]
    if len(four) < 4 or not all(QUARTER_DAYS[0] <= _days(a, b) <= QUARTER_DAYS[1] for a, b in zip(ends, ends[1:])):
        return None, f"Yahoo does not have the four quarters to {period_end}", []
    if any(q.operating is None for q in four):
        return None, f"Yahoo leaves an operating cash flow blank in the four quarters to {period_end}", []
    if all(q.ppe is not None for q in four):
        line, spent = PPE_LINE, [q.ppe for q in four]
    elif all(q.capex is not None for q in four):
        line, spent = CAPEX_LINE, [q.capex for q in four]
    else:
        return None, f"Yahoo leaves capital spending blank in the four quarters to {period_end}", []
    return sum(q.operating for q in four) - sum(abs(x) for x in spent), line, ends


def _not_compared(ctx: CheckContext, symbol: str, column: str, on: str, why: str,
                  holds_only: bool = False) -> list[Finding]:
    """A figure that could not be compared (Yahoo's value is 0): an info saying so, and the open findings on that
    figure and the ratios built on it, which this run could not re-examine. With `holds_only` (Yahoo has no quarters
    to set against ours: a warning made from other quarters says nothing now) only the holds are kept."""
    mine = {column, *DEPENDENTS.get(column, ())}
    kept = [k for k in still_open(ctx.store, "figures", symbol)
            if k.field in mine and (k.severity == "hold" or not holds_only)]
    info = Finding("figures", symbol, column, on, None, "info", f"not compared: {why}", {})
    return kept if any((k.field, k.date_from) == (column, on) for k in kept) else kept + [info]


def outside_figures(ctx: CheckContext) -> list[Finding]:
    """Held stocks whose newest valuation row is ok: each figure against Yahoo's. A figure we lack that Yahoo
    has is a warning on that field; one more than `figure_warn` away is a warning, more than `figure_hold` a
    hold on that field alone."""
    cfg = ctx.cfg.check
    held = set(_covered(ctx))
    out: list[Finding] = []
    asked = 0
    for row in ctx.store.check_newest_valuation():
        symbol = row["symbol"]
        if symbol not in held or row["asset_type"] != "stock" or row["status"] != "ok":
            continue
        if asked:
            ctx.sleep(PAUSE_SECONDS)
        asked += 1
        try:
            figs = ctx.outside.figures(symbol)
            if figs is None:
                raise LookupError("Yahoo has no figures")
        except Exception as e:
            out += _failure(ctx, "figures", symbol, "*", row["date"], f"could not ask Yahoo for figures: {type(e).__name__}: {e}")
            continue
        compared = [(column, getattr(figs, name), {"figure": name}, "") for column, name in FIELDS]
        if figs.quarters:
            theirs, line, ends = _yahoo_fcf(figs.quarters, row["ttm_period_end"])
            if theirs is None:
                out += _not_compared(ctx, symbol, "fcf_ttm", row["date"], line, holds_only=True)
            else:
                spend = "purchases of property and equipment" if line == PPE_LINE else "capital expenditure"
                compared.append(("fcf_ttm", theirs, {"figure": "operating cash flow less " + spend,
                                                     "capex_line": line, "quarters": ends},
                                 f" (four quarters to {ends[0]}, less {spend})"))
        for column, theirs, extra, what in compared:
            ours = row[column]
            if theirs is None:
                continue
            evidence = {"ours": ours, "theirs": theirs, **extra, "date": row["date"]}
            if ours is None:
                out.append(Finding("figures", symbol, column, row["date"], None, "warn",
                                   f"{column} is not known here, but Yahoo has {_num(theirs)}", evidence))
                continue
            if theirs == 0:
                out += _not_compared(ctx, symbol, column, row["date"], f"Yahoo's {extra['figure']} is 0")
                continue
            diff = abs(ours - theirs) / abs(theirs)
            evidence["difference"] = round(diff, 6)
            text = f"{column} {_num(ours)} is {_pct(diff)} from Yahoo's {_num(theirs)}{what}"
            warn_only = _warn_only(column, ours, theirs, extra.get("capex_line"))
            if diff > cfg.figure_hold and warn_only is None:
                out.append(Finding("figures", symbol, column, row["date"], None, "hold", text + f" (more than {_pct(cfg.figure_hold)})", evidence))
                out += [Finding("figures", symbol, dep, row["date"], None, "hold",
                                f"{'behind' if dep == 'shares' else 'built on'} {column}: {text} "
                                f"(more than {_pct(cfg.figure_hold)})", evidence)
                        for dep in DEPENDENTS.get(column, ())]
            elif diff > cfg.figure_warn:
                out.append(Finding("figures", symbol, column, row["date"], None, "warn", text + f" (more than {_pct(cfg.figure_warn)})", evidence))
    return out


CHECKS["prices2"] = second_price
CHECKS["figures"] = outside_figures
