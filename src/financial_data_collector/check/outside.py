"""Second opinions from outside: is the newest close what another provider says, and are our figures near
Yahoo's?

`prices2` compares the newest final close of every held symbol with the OTHER provider than the one the bar
came from (Tiingo bar -> Yahoo; Yahoo bar -> Tiingo). `figures` compares trailing EPS, market cap, revenue and
free cash flow with Yahoo's. Neither ever holds on an outside failure: when a provider cannot answer, the
symbol gets an `info` finding and keeps the holds it already had (not asked is not cleared).

`OutsideSource` is the seam: `YahooSource` is the real one (yfinance, imported only when it is first asked), and
tests pass a fake. The Tiingo side goes through `ctx.fetch` with the configured token.
"""
from __future__ import annotations

import json
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
class Figures:
    trailing_eps: float | None
    market_cap: float | None
    revenue: float | None
    free_cash_flow: float | None


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
    """Yahoo through yfinance, which is imported when first needed: tests and offline runs never load it."""

    def quote(self, symbol: str, on: str) -> Quote | None:
        import yfinance as yf
        end = (date.fromisoformat(on) + timedelta(days=1)).isoformat()
        hist = yf.Ticker(to_yfinance(symbol)).history(start=on, end=end, auto_adjust=False)
        if hist is None or len(hist) == 0:
            return None
        close = _number(hist["Close"].iloc[-1])
        return None if close is None else Quote(close, str(hist.index[-1])[:10])

    def figures(self, symbol: str) -> Figures | None:
        import yfinance as yf
        info = yf.Ticker(to_yfinance(symbol)).info or {}
        figs = Figures(_number(info.get("trailingEps")), _number(info.get("marketCap")),
                       _number(info.get("totalRevenue")), _number(info.get("freeCashflow")))
        return None if figs == Figures(None, None, None, None) else figs


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
    """The newest final close of each held symbol against another provider's. Over `price_hold` apart is a hold
    on every value ("*"); over `price_warn` is a warning on the close."""
    cfg = ctx.cfg.check
    out: list[Finding] = []
    asked = 0
    for symbol in _covered(ctx):
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
        a, b = _split_adjusted(ours, quote.close, factor)
        if b == 0:
            continue
        diff = abs(a - b) / abs(b)
        evidence = {"ours": ours, "theirs": quote.close, "source": source, "other": other.lower(), "date": on,
                    "difference": round(diff, 6), "split_factor": factor}
        text = f"close {_num(ours)} on {on} ({source}) is {_pct(diff)} from {other}'s {_num(quote.close)}"
        if diff > cfg.price_hold:
            out.append(Finding("prices2", symbol, "*", on, None, "hold", text + f" (more than {_pct(cfg.price_hold)})", evidence))
        elif diff > cfg.price_warn:
            out.append(Finding("prices2", symbol, "close", on, None, "warn", text + f" (more than {_pct(cfg.price_warn)})", evidence))
    return out


# ---- figures ---------------------------------------------------------------------------------------

FIELDS = (("eps_ttm", "trailing_eps"), ("market_cap", "market_cap"), ("revenue_ttm", "revenue"),
          ("fcf_ttm", "free_cash_flow"))            # (our valuation column, the outside figure)


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
        for column, name in FIELDS:
            theirs, ours = getattr(figs, name), row[column]
            if theirs is None:
                continue
            evidence = {"ours": ours, "theirs": theirs, "figure": name, "date": row["date"]}
            if ours is None:
                out.append(Finding("figures", symbol, column, row["date"], None, "warn",
                                   f"{column} is not known here, but Yahoo has {_num(theirs)}", evidence))
                continue
            if theirs == 0:
                continue
            diff = abs(ours - theirs) / abs(theirs)
            evidence["difference"] = round(diff, 6)
            text = f"{column} {_num(ours)} is {_pct(diff)} from Yahoo's {_num(theirs)}"
            if diff > cfg.figure_hold:
                out.append(Finding("figures", symbol, column, row["date"], None, "hold", text + f" (more than {_pct(cfg.figure_hold)})", evidence))
            elif diff > cfg.figure_warn:
                out.append(Finding("figures", symbol, column, row["date"], None, "warn", text + f" (more than {_pct(cfg.figure_warn)})", evidence))
    return out


CHECKS["prices2"] = second_price
CHECKS["figures"] = outside_figures
