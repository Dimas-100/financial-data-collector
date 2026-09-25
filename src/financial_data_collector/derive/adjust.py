"""Split- and dividend-adjusted closes computed locally from stored bars.

Providers re-base their whole adjusted series on every dividend or split, but an
incremental fetch only sees new bars, so the stored adj_close column is spliced.
This recomputes the adjustment the way Tiingo does: walking backwards, every bar
before an ex-dividend date is scaled by (prior close - dividend) / prior close, and
every bar before a split is divided by the split factor.
"""
from __future__ import annotations


def adjusted_closes(bars: list[tuple[str, float, float, float]]) -> list[tuple[str, float]]:
    """bars: ascending (date, close, dividend, split_factor). Returns ascending (date, adj_close)."""
    out: list[tuple[str, float]] = []
    factor = 1.0
    for i in range(len(bars) - 1, -1, -1):
        d, close, dividend, split = bars[i]
        out.append((d, round(close * factor, 4)))
        if i > 0:
            prev_close = bars[i - 1][1]
            if dividend and prev_close:
                factor *= (prev_close - dividend) / prev_close
            if split and split > 0 and split != 1:
                factor /= split
    out.reverse()
    return out
