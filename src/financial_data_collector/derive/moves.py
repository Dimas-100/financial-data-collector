"""Drops of a symbol's final close and their causes (the `price_moves` rows the engine step writes).

Pure: no SQL, no store. `find_moves` finds the drops in a symbol's held adjusted closes (the engine passes
`engine.held_adjusted_bars`: adjusted over every final bar, held dates then dropped, so a split or an ex-dividend day
is never a drop): a day down `drop_day` (4%), 5 trading bars down `drop_week` (8%, recorded on the first day it
crosses), or a day down at all and `excess_sd` (2.5) of the symbol's own standard deviations of daily excess moves
over the market fund below it. `add_causes` explains one from sourced facts only, in this order: an earnings report, an 8-K, the
market fund, the sector fund, up to three headlines; when none applies the one cause is "no cause found", never a
guess.
"""
from __future__ import annotations

import statistics
from dataclasses import replace

from ..config import EngineSettings
from ..models import CurrentReport, EarningsEvent, Headline, PriceMove
from . import sources

# What each 8-K item number means, in words; an item not listed here is shown as "item <number>".
ITEM_LABELS: dict[str, str] = {
    "1.01": "a material agreement",
    "1.02": "an agreement ended",
    "1.03": "bankruptcy",
    "2.01": "an acquisition or sale of assets",
    "2.03": "a new debt obligation",
    "2.04": "a debt obligation accelerated",
    "2.05": "restructuring costs",
    "2.06": "an impairment",
    "3.01": "a delisting notice",
    "3.02": "unregistered share sales",
    "4.01": "an auditor change",
    "4.02": "earlier statements no longer to be relied on",
    "5.02": "a director or officer change",
    "5.07": "a shareholder vote",
    "7.01": "a Regulation FD disclosure",
    "8.01": "other events",
}
NOT_A_CAUSE_ITEMS = ("2.02", "9.01")    # earnings results (the earnings cause) and exhibits say nothing by themselves
MARKET_SHARE = 2 / 3                    # a benchmark explains a drop when it fell at least this share of it
WEEK_BARS = 5                           # the 5d span: 5 held bars back
SD_BARS = 252                           # the deviation of daily excess moves: over the 252 bars before the day
SD_MIN_MOVES = 120                      # ... and needs at least this many of them
NEWS_MAX = 3
NO_CAUSE = {"kind": "none", "text": "no cause found", "sources": []}
_TIME_WORDS = {"after_close": " after the close", "before_open": " before the open", "during": " during the day"}


def _price_source(bar: tuple) -> dict:
    """The price source of a bar: (date, adjusted close, dividend, split, source, fetched_at) as the engine passes it;
    a bare (date, close) pair names no provider."""
    return sources.price(bar[4] if len(bar) > 4 else "adjusted", bar[0], bar[5] if len(bar) > 5 else None)


def _bench_move(bench: tuple[str, list] | None, start: str, day: str):
    """(symbol, move between start and day, the two bars used) of a benchmark; the move is None without both bars."""
    if bench is None:
        return None, None, ()
    sym, bars = bench
    by_date = {b[0]: b for b in bars}
    a, b = by_date.get(start), by_date.get(day)
    if a is None or b is None or not a[1]:
        return sym, None, ()
    return sym, b[1] / a[1] - 1, (a, b)


def find_moves(symbol: str, bars: list[tuple], market: tuple[str, list] | None, sector: tuple[str, list] | None,
               since: str, s: EngineSettings) -> list[PriceMove]:
    """The drops of `symbol` dated after `since`, ascending by date then span ('1d' before '5d'), causes empty.

    `bars` are the symbol's held final bars, ascending, each (date, adjusted close, ...); `market` / `sector` are
    (benchmark symbol, its held bars in the same shape), or None. Bars on or before `since` still feed the
    deviation and the 5-bar return."""
    closes = [(b[0], b[1]) for b in bars]
    mk = {b[0]: b[1] for b in market[1]} if market is not None else {}

    def daily(j: int) -> float | None:
        return closes[j][1] / closes[j - 1][1] - 1 if closes[j - 1][1] else None

    def excess(j: int) -> float | None:
        d0, d1 = closes[j - 1][0], closes[j][0]
        own = daily(j)
        if own is None or not mk.get(d0) or d1 not in mk:
            return None
        return own - (mk[d1] / mk[d0] - 1)

    excesses = [None] + [excess(j) for j in range(1, len(closes))]

    def week(j: int) -> float | None:
        if j < WEEK_BARS or not closes[j - WEEK_BARS][1]:
            return None
        return closes[j][1] / closes[j - WEEK_BARS][1] - 1

    out: list[PriceMove] = []
    for i in range(1, len(closes)):
        day = closes[i][0]
        if day <= since:
            continue
        move = daily(i)
        if move is not None:
            triggers: list[str] = []
            extra: list[dict] = []
            if move <= -s.drop_day:
                triggers.append("drop_day")
            sd_value = None
            if excesses[i] is not None:
                history = [e for e in excesses[max(1, i - SD_BARS):i] if e is not None]
                if len(history) >= SD_MIN_MOVES:
                    sd = statistics.stdev(history)
                    if sd > 0:
                        sd_value = excesses[i] / sd
                        if move < 0 and sd_value <= -s.excess_sd:      # a stock that rose is not a drop
                            triggers.append("excess")
                            extra.append(sources.derived(
                                f"daily excess moves over {market[0]} ({len(history)} before {day})",
                                f"sd {sd:.6f}, excess {excesses[i]:+.6f}", day))
            if triggers:
                out.append(_move(symbol, i - 1, i, "1d", move, sd_value, triggers, extra, bars, market, sector, s))
        now = week(i)
        if now is not None and now <= -s.drop_week:
            before = week(i - 1)
            if before is None or before > -s.drop_week:
                out.append(_move(symbol, i - WEEK_BARS, i, "5d", now, None, ["drop_week"], [], bars, market, sector,
                                 s))
    return out


_SETTING_OF = {"drop_day": "drop_day", "excess": "excess_sd", "drop_week": "drop_week"}


def _move(symbol: str, a: int, b: int, span: str, move: float, sd_value: float | None, triggers: list[str],
          extra: list[dict], bars: list[tuple], market, sector, s: EngineSettings) -> PriceMove:
    start, day = bars[a][0], bars[b][0]
    srcs = [_price_source(bars[a]), _price_source(bars[b])]
    msym, mmove, mbars = _bench_move(market, start, day)
    ssym, smove, sbars = _bench_move(sector, start, day)
    srcs += [_price_source(x) for x in mbars + sbars]
    srcs += [sources.config(_SETTING_OF[t], getattr(s, _SETTING_OF[t])) for t in triggers]
    srcs += extra
    return PriceMove(symbol, day, span, start, move, msym, mmove, ssym, smove, sd_value, tuple(triggers), (),
                     tuple(srcs))


def _eps_text(e: EarningsEvent) -> str:
    if e.eps_reported is None:
        return ""
    text = f": EPS {e.eps_reported:.2f}"
    if e.eps_expected is not None:
        text += f" vs {e.eps_expected:.2f} expected"
    if e.surprise_pct is not None:
        text += f" ({e.surprise_pct:+.1f}%)"
    return text


def _earnings_cause(e: EarningsEvent, cik: str | None) -> dict:
    src: list[dict] = []
    if e.accession and cik:
        try:
            src.append(sources.filing(cik, "8-K", e.accession, e.report_date))
        except ValueError:                               # an accession or CIK that does not validate gets no link
            pass
    if e.date_source != "sec" or e.eps_reported is not None or not src:
        src.append(sources.feed("Yahoo earnings dates", None, e.report_date))
    text = f"reported earnings on {e.report_date}{_TIME_WORDS.get(e.time_of_day or '', '')}{_eps_text(e)}"
    return {"kind": "earnings", "text": text, "sources": src}


def _filing_cause(r: CurrentReport, items: list[str], cik: str) -> dict | None:
    try:
        src = sources.filing(cik, r.form, r.accession, r.filed)
    except ValueError:                                   # no valid link, no sourced cause
        return None
    words = "; ".join(ITEM_LABELS.get(it, f"item {it}") for it in items)
    return {"kind": "filing", "text": f"filed an {r.form} on {r.filed}: {words}", "sources": [src]}


def _bench_cause(kind: str, text: str, bench: str, bench_move: float, start: str, day: str) -> dict:
    return {"kind": kind, "text": text,
            "sources": [sources.derived(f"{bench} adjusted closes {start}..{day}", f"{bench_move:+.6f}", day)]}


def add_causes(move: PriceMove, events: list[EarningsEvent], reports: list[CurrentReport], headlines: list[Headline],
               cik: str | None, sector_name: str | None) -> PriceMove:
    """`move` with its causes, each {kind, text, sources}, in this order: earnings reported in (start_date, date] or on
    start_date after the close or at an unknown time, but not on `date` after the close (that news came after the
    close the move ends on: it is the next move's); 8-Ks filed in [start_date, date] with an item other than 2.02 and
    9.01; the market fund and the sector fund when they fell at least MARKET_SHARE of the move; up to NEWS_MAX
    headlines dated in [start_date, date], newest first. None of these: the one cause "no cause found"."""
    start, day = move.start_date, move.date
    causes: list[dict] = []
    for e in sorted(events, key=lambda x: x.report_date):
        in_span = start < e.report_date < day or (e.report_date == day and e.time_of_day != "after_close")
        evening_before = e.report_date == start and e.time_of_day in ("after_close", None)
        if in_span or evening_before:
            causes.append(_earnings_cause(e, cik))
    if cik:
        for r in sorted(reports, key=lambda x: (x.filed, x.accession)):
            items = [it for it in r.items if it not in NOT_A_CAUSE_ITEMS]
            if start <= r.filed <= day and items:
                c = _filing_cause(r, items, cik)
                if c is not None:
                    causes.append(c)
    if move.move < 0 and move.market_move is not None and move.market_move <= MARKET_SHARE * move.move:
        causes.append(_bench_cause("market", f"the market fell {-move.market_move:.1%} ({move.market_symbol})",
                                   move.market_symbol, move.market_move, start, day))
    if move.move < 0 and move.sector_move is not None and move.sector_move <= MARKET_SHARE * move.move:
        named = f"{move.sector_symbol}, {sector_name}" if sector_name else move.sector_symbol
        causes.append(_bench_cause("sector", f"its sector fell {-move.sector_move:.1%} ({named})",
                                   move.sector_symbol, move.sector_move, start, day))
    news = sorted((h for h in headlines if start <= h.date <= day), key=lambda h: h.date, reverse=True)
    for h in news[:NEWS_MAX]:
        where = f"{h.outlet}, {h.date}" if h.outlet else h.date
        label = f"news.json: {h.outlet}" if h.outlet else "news.json"
        causes.append({"kind": "news", "text": f"{h.title} ({where})",
                       "sources": [sources.feed(label, h.feed_as_of, h.title, h.url)]})
    return replace(move, causes=tuple(causes) if causes else (dict(NO_CAUSE, sources=[]),))
