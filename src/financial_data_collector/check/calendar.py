"""NYSE trading days, by rule.

A trading day is a weekday that is not one of the exchange's full-day holidays: New Year's Day, Martin Luther
King Jr. Day (third Monday of January), Presidents' Day (third Monday of February), Good Friday, Memorial Day
(last Monday of May), Juneteenth (June 19, from 2022), Independence Day, Labor Day (first Monday of
September), Thanksgiving (fourth Thursday of November) and Christmas. A fixed-date holiday on a Saturday is
observed on the Friday before and one on a Sunday on the Monday after, except New Year's Day: a Saturday
January 1 is not moved to the Friday (December 31 trades). Early closes are trading days.

Not modelled: one-off closures (national days of mourning, 9/11, storms). A day like that looks like a
missing day to a check; the gap it makes is a warning, not a hold.
"""
from __future__ import annotations

from datetime import date, timedelta


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th `weekday` (Monday = 0) of the month; n = -1 is the last one."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def easter(year: int) -> date:
    """Western Easter Sunday (the anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date) -> date:
    """A fixed-date holiday: Saturday is taken on the Friday, Sunday on the Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def holidays(year: int) -> set[date]:
    """The days the exchange is closed on a weekday of this year."""
    out = {
        _nth_weekday(year, 1, 0, 3),                  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),                  # Presidents' Day
        easter(year) - timedelta(days=2),             # Good Friday
        _nth_weekday(year, 5, 0, -1),                 # Memorial Day
        _observed(date(year, 7, 4)),                  # Independence Day
        _nth_weekday(year, 9, 0, 1),                  # Labor Day
        _nth_weekday(year, 11, 3, 4),                 # Thanksgiving
        _observed(date(year, 12, 25)),                # Christmas
    }
    jan1 = date(year, 1, 1)
    if jan1.weekday() != 5:                           # a Saturday January 1 is not moved to the Friday
        out.add(jan1 + timedelta(days=1) if jan1.weekday() == 6 else jan1)
    if year >= 2022:
        out.add(_observed(date(year, 6, 19)))         # Juneteenth
    return out


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def trading_days(start: date, end: date) -> list[date]:
    """Every NYSE trading day from `start` to `end`, both included, oldest first."""
    out: list[date] = []
    d = start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out
