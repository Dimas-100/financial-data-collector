"""Shape raw XBRL facts into financial_line_items rows.

See the design spec section 5.4. Summary: pick per line item and period the
best concept (priority, then latest filed); classify durations as annual /
quarter / half / nine-month by length; group a fiscal year by its start date;
derive missing quarters from year-to-date facts; place instants at period ends.
"""
from __future__ import annotations

from datetime import date, timedelta

from .models import ConceptRule, Fact, LineItem

ANNUAL_DAYS = (350, 380)
QUARTER_DAYS = (80, 100)
HALF_DAYS = (170, 190)
NINE_MONTH_DAYS = (260, 280)
_UNITS = {"duration": {"USD"}, "instant": {"USD"}, "per_share": {"USD/shares"}, "shares": {"shares"}}
_DERIVABLE = {"duration"}


def fiscal_year_of(period_end: str) -> int:
    d = date.fromisoformat(period_end)
    return d.year - 1 if (d.month == 1 and d.day <= 15) else d.year


def _days(f: Fact) -> int:
    return (date.fromisoformat(f.period_end) - date.fromisoformat(f.period_start)).days


def _within(n: int, rng: tuple[int, int]) -> bool:
    return rng[0] <= n <= rng[1]


def _day_after(iso: str) -> str:
    return (date.fromisoformat(iso) + timedelta(days=1)).isoformat()


def _later(a: Fact, b: Fact) -> Fact:
    return b if (b.filed, b.accn) > (a.filed, a.accn) else a


def _pick(cands: list[tuple[int, Fact]]) -> Fact:
    best = min(p for p, _ in cands)
    facts = [f for p, f in cands if p == best]
    out = facts[0]
    for f in facts[1:]:
        out = _later(out, f)
    return out


def _chosen(by_concept: dict[tuple[str, str], list[Fact]], rules: list[ConceptRule]) -> dict[tuple[str, str], Fact]:
    """(period_start, period_end) -> winning fact for one line item."""
    units = _UNITS[rules[0].kind]
    cands: dict[tuple[str, str], list[tuple[int, Fact]]] = {}
    for r in rules:
        for f in by_concept.get((r.taxonomy, r.concept), []):
            # Proxies (DEF 14A) and other non-statement filings repeat figures with no
            # fiscal period; they must never outrank the 10-K/10-Q that reported them.
            if f.unit in units and f.fp in _PERIODS:
                cands.setdefault((f.period_start, f.period_end), []).append((r.priority, f))
    return {k: _pick(v) for k, v in cands.items()}


def _row(item: str, kind: str, f: Fact, fy: int, fq: int | None, *, value: float | None = None,
         start: str | None = None, derived: bool = False) -> LineItem:
    return LineItem(item, kind, f.period_start if start is None else start, f.period_end, fy, fq,
                    f.value if value is None else value, f.concept, f.filed, f.accn, derived)


def _put(rows: dict[tuple[str, str], LineItem], li: LineItem) -> None:
    key = (li.period_kind, li.period_end)
    old = rows.get(key)
    if old is None or (li.filed, li.accn) > (old.filed, old.accn):
        rows[key] = li


def _qnum(start: str, end: str) -> int:
    n = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    return max(1, min(4, round(n / 91.3)))


_PERIODS = {"Q1", "Q2", "Q3", "FY"}


_ANNUAL_FORMS = ("10-K", "20-F", "40-F")  # amendments (10-K/A) start with the same prefix


def fiscal_years_by_end(facts: list[Fact], concepts: set[tuple[str, str]] | None = None) -> dict[str, int]:
    """period_end -> the filer's own fiscal-year label.

    Only annual-report forms define years. A 10-K tags its comparative years with the
    filing's own fy, so the label is taken from the fact whose end is the LATEST
    year-length end in that filing, counting only mapped statement concepts (a
    calendar-year pension disclosure inside a June filer is not a statement). The
    original filer of a year labels it (earliest filing wins), and a label more than
    a year away from the calendar year of the end is discarded as a tagging error.
    """
    def is_year(f: Fact) -> bool:
        return bool(f.period_start) and f.fp == "FY" and f.form.startswith(_ANNUAL_FORMS) \
            and _within(_days(f), ANNUAL_DAYS)

    own_end: dict[str, str] = {}
    for f in facts:
        if is_year(f) and (concepts is None or (f.taxonomy, f.concept) in concepts):
            own_end[f.accn] = max(own_end.get(f.accn, ""), f.period_end)
    labels: dict[str, int] = {}
    for f in sorted(facts, key=lambda f: (f.filed, f.accn)):
        if is_year(f) and f.fy and own_end.get(f.accn) == f.period_end and f.period_end not in labels \
                and abs(f.fy - int(f.period_end[:4])) <= 1:
            labels[f.period_end] = f.fy
    return labels


def _near_fiscal_year_end(end: str, anchors: list[str]) -> bool:
    """True when `end` falls within a week (mod one year) of a known fiscal year end.

    52/53-week filers drift a few days either side; a calendar-year disclosure inside a
    June filer is half a year away and is rejected. No anchors known: accept everything.
    """
    if not anchors:
        return True
    e = date.fromisoformat(end)
    for a in anchors:
        diff = abs((e - date.fromisoformat(a)).days) % 365
        if diff <= 7 or diff >= 358:
            return True
    return False


def _duration_rows(item: str, kind: str, chosen: dict[tuple[str, str], Fact],
                   quarter_number: dict[str, int], quarter_fy: dict[str, int],
                   fy_by_end: dict[str, int]) -> list[LineItem]:
    annuals: dict[tuple[str, str], Fact] = {}
    quarters: dict[tuple[str, str], Fact] = {}
    halves: dict[tuple[str, str], Fact] = {}
    nines: dict[tuple[str, str], Fact] = {}
    anchors = list(fy_by_end)
    for (s, e), f in chosen.items():
        if f.fp not in _PERIODS:  # proxies and other non-statement filings carry no fiscal period
            continue
        n = _days(f)
        if _within(n, ANNUAL_DAYS):
            # a 10-Q's "twelve months ended" figure, or a calendar-year disclosure inside a
            # June filer's 10-K, is not a fiscal year
            if f.fp == "FY" and _near_fiscal_year_end(e, anchors):
                annuals[(s, e)] = f
        elif _within(n, QUARTER_DAYS):
            quarters[(s, e)] = f
        elif _within(n, HALF_DAYS):
            halves[(s, e)] = f
        elif _within(n, NINE_MONTH_DAYS):
            nines[(s, e)] = f
    rows: dict[tuple[str, str], LineItem] = {}
    for (s, e), f in annuals.items():
        _put(rows, _row(item, "annual", f, fy_by_end.get(e, fiscal_year_of(e)), None))
    starts = {s for s, _ in list(annuals) + list(halves) + list(nines)}
    starts |= {s for (s, _), f in quarters.items() if f.fp == "Q1"}
    starts |= {_day_after(e) for _, e in annuals}
    for S in sorted(starts):
        fy_fact = next((f for (s, e), f in annuals.items() if s == S), None)
        fy_end = fy_fact.period_end if fy_fact else (date.fromisoformat(S) + timedelta(days=364)).isoformat()
        fiscal_year = fy_by_end.get(fy_end, fiscal_year_of(fy_end))
        known: dict[int, float] = {}
        ends: dict[int, str] = {}
        in_year = sorted(((k, f) for k, f in quarters.items() if S <= k[0] and k[1] <= fy_end), key=lambda kf: kf[0][1])
        for (s, e), f in in_year:
            n = _qnum(S, e)
            _put(rows, _row(item, "quarter", f, fiscal_year, n))
            known[n], ends[n] = f.value, e
        if kind in _DERIVABLE:
            h1 = next((f for (s, e), f in halves.items() if s == S), None)
            nm = next((f for (s, e), f in nines.items() if s == S), None)
            if h1 and 2 not in known and 1 in known:
                v = h1.value - known[1]
                _put(rows, _row(item, "quarter", h1, fiscal_year, 2, value=v, start=_day_after(ends[1]), derived=True))
                known[2], ends[2] = v, h1.period_end
            if nm and 3 not in known:
                base = known[1] + known[2] if 1 in known and 2 in known else (h1.value if h1 else None)
                if base is not None:
                    prev_end = ends.get(2) or (h1.period_end if h1 else None)
                    if prev_end:
                        v = nm.value - base
                        _put(rows, _row(item, "quarter", nm, fiscal_year, 3, value=v, start=_day_after(prev_end), derived=True))
                        known[3], ends[3] = v, nm.period_end
            if fy_fact and 4 not in known:
                base = known[1] + known[2] + known[3] if {1, 2, 3} <= known.keys() else (nm.value if nm else None)
                if base is not None:
                    prev_end = ends.get(3) or (nm.period_end if nm else None)
                    if prev_end:
                        v = fy_fact.value - base
                        _put(rows, _row(item, "quarter", fy_fact, fiscal_year, 4, value=v, start=_day_after(prev_end), derived=True))
                        known[4], ends[4] = v, fy_fact.period_end
        for n, e in ends.items():
            quarter_number[e], quarter_fy[e] = n, fiscal_year
    return list(rows.values())


def rebuild(facts: list[Fact], rules: list[ConceptRule]) -> list[LineItem]:
    by_concept: dict[tuple[str, str], list[Fact]] = {}
    for f in facts:
        by_concept.setdefault((f.taxonomy, f.concept), []).append(f)
    by_item: dict[str, list[ConceptRule]] = {}
    for r in sorted(rules, key=lambda r: (r.line_item, r.priority)):
        by_item.setdefault(r.line_item, []).append(r)

    fy_by_end = fiscal_years_by_end(facts, {(r.taxonomy, r.concept) for r in rules})
    anchors = list(fy_by_end)
    annual_ends: set[str] = set()
    quarter_ends: set[str] = set()
    for f in facts:
        if f.period_start and f.fp in _PERIODS:
            n = _days(f)
            if _within(n, ANNUAL_DAYS):
                if f.fp == "FY" and _near_fiscal_year_end(f.period_end, anchors):
                    annual_ends.add(f.period_end)
            elif _within(n, QUARTER_DAYS) or _within(n, HALF_DAYS) or _within(n, NINE_MONTH_DAYS):
                quarter_ends.add(f.period_end)

    quarter_number: dict[str, int] = {}
    quarter_fy: dict[str, int] = {}
    out: list[LineItem] = []
    instant_items: list[tuple[str, dict[str, Fact]]] = []
    for item, item_rules in by_item.items():
        chosen = _chosen(by_concept, item_rules)
        durations = {k: f for k, f in chosen.items() if k[0]}
        instants = {k[1]: f for k, f in chosen.items() if not k[0]}
        if instants:
            instant_items.append((item, instants))
        if durations:
            out.extend(_duration_rows(item, item_rules[0].kind, durations, quarter_number, quarter_fy, fy_by_end))
    for item, instants in instant_items:
        for end, f in instants.items():
            if end in annual_ends:
                out.append(_row(item, "annual", f, fy_by_end.get(end, fiscal_year_of(end)), None))
            if end in annual_ends or end in quarter_ends:
                fq = 4 if end in annual_ends else quarter_number.get(end)
                out.append(_row(item, "quarter", f, quarter_fy.get(end, fiscal_year_of(end)), fq))
    # A filer can tag the same concept as an instant and as a duration (equity
    # roll-forwards); keep one row per (line_item, period_kind, period_end), latest filed.
    unique: dict[tuple[str, str, str], LineItem] = {}
    for li in out:
        key = (li.line_item, li.period_kind, li.period_end)
        old = unique.get(key)
        if old is None or (li.filed, li.accn) > (old.filed, old.accn):
            unique[key] = li
    return list(unique.values())
