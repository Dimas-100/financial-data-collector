"""The price shape of one symbol on its newest final close: how far it sits under its 52-week high and from its 200-day
average, and how its last week compares with the market fund's and its sector fund's.

Pure: no SQL, no store. Every figure is on adjusted closes (`adjust.adjusted_closes` over the bars it is given), so a
split or a dividend does not bend a high or an average. The caller passes bars with held closes already left out.
A bar is (date, close, dividend, split_factor, source, fetched_at), ascending.
"""
from __future__ import annotations

from datetime import date, timedelta

from ..models import ScoreRow
from . import sources
from .adjust import adjusted_closes

METRICS = ("off_high_52w", "vs_200d", "week_vs_market", "week_vs_sector")
INPUTS = ("close",)
HIGH_DAYS = 365          # the 52-week high: the highest adjusted close over the 365 calendar days ending the day
HIGH_MIN_DAYS = 300      # ... and it needs a close at least this many days back
AVERAGE_BARS = 200       # the 200-day average: the mean of the last 200 adjusted closes
WEEK_BARS = 5            # a week: 5 trading bars back


def _row(symbol: str, day: str, metric: str, srcs: list[dict], value: float | None = None, status: str = "ok",
         reason: str | None = None) -> ScoreRow:
    return ScoreRow(symbol, day, metric, value, None, None, status, reason, INPUTS, tuple(srcs))


def _adjusted(bars) -> list[tuple[str, float]]:
    return adjusted_closes([(b[0], b[1], b[2], b[3]) for b in bars])


def _shift(day: str, days: int) -> str:
    return (date.fromisoformat(day) - timedelta(days=days)).isoformat()


def _relative(symbol: str, day: str, metric: str, own: dict, start: str, ret: float, bench, missing: str) -> ScoreRow:
    """The symbol's return from `start` to `day` minus the benchmark's between the same two dates."""
    if bench is None:
        return _row(symbol, day, metric, [own], status="unsupported", reason=missing)
    bsym, bbars = bench
    badj = dict(_adjusted(bbars))
    for d in (day, start):
        if not badj.get(d):
            return _row(symbol, day, metric, [own], status="unsupported", reason=f"no {bsym} close on {d}")
    braw = next(b for b in bbars if b[0] == day)
    bret = badj[day] / badj[start] - 1
    srcs = [own, sources.price(braw[4], day, braw[5]),
            sources.derived(f"adjusted closes {start}..{day} ({WEEK_BARS + 1} bars)",
                            f"{symbol} {ret:+.6f}, {bsym} {bret:+.6f}", day)]
    return _row(symbol, day, metric, srcs, ret - bret)


def price_shape(symbol: str, day: str, bars, market: tuple[str, list] | None,
                sector: tuple[str, list] | None) -> list[ScoreRow]:
    """The four price-shape rows of `symbol` on `day`, in METRICS order. `bars` are the symbol's held final bars;
    `market` / `sector` are (benchmark symbol, its held final bars), or None when there is none to compare with."""
    bars = [b for b in bars if b[0] <= day]
    if not bars:
        return [_row(symbol, day, m, [], status="not_enough_history", reason="no final close") for m in METRICS]
    adj = _adjusted(bars)
    last = bars[-1]
    own = sources.price(last[4], last[0], last[5])
    close = adj[-1][1]
    out: list[ScoreRow] = []

    # off_high_52w
    start = _shift(day, HIGH_DAYS)
    window = [(d, c) for d, c in adj if d >= start]
    if bars[0][0] > _shift(day, HIGH_MIN_DAYS):
        out.append(_row(symbol, day, "off_high_52w", [own], status="not_enough_history",
                        reason=f"needs a close {HIGH_MIN_DAYS} days back; the first is {bars[0][0]}"))
    else:
        hi_date, hi = max(window, key=lambda dc: dc[1])
        out.append(_row(symbol, day, "off_high_52w",
                        [own, sources.derived(f"adjusted closes {start}..{day} ({len(window)} bars)",
                                              f"high {hi:.4f} on {hi_date}", day)],
                        close / hi - 1 if hi > 0 else None,
                        "ok" if hi > 0 else "unsupported", None if hi > 0 else "no positive close"))

    # vs_200d
    if len(adj) < AVERAGE_BARS:
        out.append(_row(symbol, day, "vs_200d", [own], status="not_enough_history",
                        reason=f"needs {AVERAGE_BARS} closes; has {len(adj)}"))
    else:
        w = adj[-AVERAGE_BARS:]
        mean = sum(c for _, c in w) / AVERAGE_BARS
        out.append(_row(symbol, day, "vs_200d",
                        [own, sources.derived(f"adjusted closes {w[0][0]}..{w[-1][0]} ({AVERAGE_BARS} bars)",
                                              f"mean {mean:.4f}", day)],
                        close / mean - 1 if mean > 0 else None,
                        "ok" if mean > 0 else "unsupported", None if mean > 0 else "no positive close"))

    # week_vs_market, week_vs_sector
    if len(adj) < WEEK_BARS + 1:
        reason = f"needs {WEEK_BARS + 1} closes; has {len(adj)}"
        out += [_row(symbol, day, m, [own], status="not_enough_history", reason=reason)
                for m in ("week_vs_market", "week_vs_sector")]
        return out
    w_start, c0 = adj[-(WEEK_BARS + 1)]
    if not c0:
        out += [_row(symbol, day, m, [own], status="unsupported", reason=f"no {symbol} close on {w_start}")
                for m in ("week_vs_market", "week_vs_sector")]
        return out
    ret = close / c0 - 1
    out.append(_relative(symbol, day, "week_vs_market", own, w_start, ret, market, "no market fund"))
    out.append(_relative(symbol, day, "week_vs_sector", own, w_start, ret, sector, "no sector"))
    return out
