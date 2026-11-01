"""The week score: is this the week the waiting cash goes in (spec addendum 2026-10-31, the owner's option A).

Pure: no SQL, no store. `week_score` reads the money's destinations (the account's entries the split could give
money to, short of target, each with its gap, band and newest unexplained drop) and returns a score from 0 to 10,
never a probability, with the parts that moved it, worded here so a reader shows them as given:

- a usual week starts at BASE (7);
- cheap destinations add `round(CHEAP_POINTS x their share of the gap)`, premium ones take `round(PREMIUM_POINTS x
  theirs)`;
- a destination of at least DROP_MIN_SHARE of the gap that dropped with no cause found in the DROP_DAYS to the cash
  date takes 1, at most DROP_MAX in all (an unexplained drop is a reason to wait, never to add);
- "not known" counts as usual and is said; nothing short is said.

The verdict is `go` from `go_from`, `half` from `half_from`, else `hold`. Two guards make it `go` whatever the score:
held-back money that has waited `max_wait_weeks` (`waited`), and the largest-gap destinations that together make up
at least half the gap all cheap (`under_and_cheap`). Never the word "buy".
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

from ..models import VERDICT_FRACTION

BASE = 7
CHEAP_POINTS = 3
PREMIUM_POINTS = 6
DROP_MIN_SHARE = 0.10
DROP_MAX = 2
DROP_DAYS = 14


@dataclass(frozen=True)
class Destination:
    """One entry the split could give money to: its gap (dollars short, > 0), its band, whether the band was judged
    on enough measures (`known`), and its newest unexplained drop in the window ({date, move} or None)."""
    symbol: str
    gap: float
    band: str
    known: bool = True
    drop: dict | None = None


@dataclass(frozen=True)
class WeekScore:
    score: int
    verdict: str
    fraction: float
    parts: tuple[dict, ...]
    forced: str | None


def _round(x: float) -> int:
    """Half up (2.5 -> 3), not Python's half to even."""
    return int(math.floor(x + 0.5))


def _part(sign: str, code: str, text: str, symbol: str | None = None) -> dict:
    return {"sign": sign, "code": code, "symbol": symbol, "text": text}


def _day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%b} {d.day}"


def _signed(n: int) -> str:
    return f"+{n}" if n >= 0 else str(n)


def unexplained_drop(moves: list[dict], cash_date: str) -> dict | None:
    """The newest move ({date, move, first}) dated in the DROP_DAYS up to `cash_date` whose first cause is `none`,
    as {date, move}; None when there is none."""
    start = (date.fromisoformat(cash_date) - timedelta(days=DROP_DAYS)).isoformat()
    found = [m for m in moves if start < m["date"] <= cash_date and m.get("first") == "none"]
    if not found:
        return None
    newest = max(found, key=lambda m: m["date"])
    return {"date": newest["date"], "move": newest["move"]}


def week_score(destinations: list[Destination], *, weeks_waited: int, go_from: int, half_from: int,
               max_wait_weeks: int) -> WeekScore:
    """The score, verdict, fraction, parts and guard of one account's week. `destinations` with a gap of 0 or less
    are left out."""
    dests = sorted((d for d in destinations if d.gap > 0), key=lambda d: (-d.gap, d.symbol))
    total = sum(d.gap for d in dests)
    parts = [_part("neutral", "base", f"A usual week starts at {BASE}.")]
    points = 0
    if not dests:
        parts.append(_part("neutral", "nothing_short", "Nothing in this account is short of its target."))
    else:
        for band, weight, sign, words in (("cheap", CHEAP_POINTS, 1, "Cheaper than its own past"),
                                          ("premium", PREMIUM_POINTS, -1, "Premium to its own past")):
            these = [d for d in dests if d.band == band]
            if not these:
                continue
            share = sum(d.gap for d in these) / total
            n = sign * _round(weight * share)
            points += n
            mood = "neutral" if n == 0 else ("up" if n > 0 else "down")
            names = ", ".join(d.symbol for d in these)
            zero = "+0" if sign > 0 else "-0"
            parts.append(_part(mood, band, f"{words}: {names} ({share:.0%} of what is short): "
                                           f"{_signed(n) if n else zero}."))
        counted = 0
        for d in dests:
            if d.drop is None or d.gap / total < DROP_MIN_SHARE:
                continue
            what = f"{d.symbol} fell {-d.drop['move']:.1%} on {_day(d.drop['date'])} with no cause found"
            if counted < DROP_MAX:
                counted += 1
                points -= 1
                parts.append(_part("down", "drop_no_cause", f"{what}: -1.", d.symbol))
            else:
                parts.append(_part("neutral", "drop_no_cause",
                                   f"{what}: not counted (drops take at most {DROP_MAX}).", d.symbol))
        unknown = [d.symbol for d in dests if not d.known]
        if unknown:
            parts.append(_part("neutral", "not_known", f"Not known yet, counted as usual: {', '.join(unknown)}."))
    score = max(0, min(10, BASE + points))

    forced = None
    if weeks_waited >= max_wait_weeks:
        forced = "waited"
        parts.append(_part("up", "forced_waited", f"Money has waited {weeks_waited} weeks: it goes in this week."))
    elif dests:
        half, run = [], 0.0
        for d in dests:
            half.append(d)
            run += d.gap
            if run >= total / 2:
                break
        if all(d.band == "cheap" for d in half):
            forced = "under_and_cheap"
            names = ", ".join(d.symbol for d in half)
            parts.append(_part("up", "forced_cheap", f"The holdings furthest under target ({names}) are cheaper than "
                                                     f"their own past: it goes in this week."))
    if forced is not None or score >= go_from:
        verdict = "go"
    elif score >= half_from:
        verdict = "half"
    else:
        verdict = "hold"
    return WeekScore(score, verdict, VERDICT_FRACTION[verdict], tuple(parts), forced)
