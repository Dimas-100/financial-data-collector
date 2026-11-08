"""Mr. Buffett's book breakdown: the rolled-up Fidelity book cut six ways, for claude-os's "The book" page.

Pure, like `exposure.build_exposure`: no SQL, no store, no files. It reuses `exposure`'s `Book`, `Sectors`, the sector
rule and the crash definitions, so a figure on the page and a meter on the panel can never be built two ways.

What each dimension is (the design, claude-os docs/superpowers/specs/2026-11-08-mr-buffett-page-design.md):
  asset    stocks / bonds / cash. Bonds are the Treasury ballast (`exposure.TREASURY_FUNDS`), cash the accounts' cash.
  sector   every sector, rolled up (no plan line: the plan sets sector weights for the brokerage only), and the
           brokerage's sectors with the plan's weight as `line_pct`. Complete: the funds' published maps are whole.
  country  United States / International, of the STOCK dollars only (bonds and cash have no country here).
  company  the largest companies by dollars held directly plus through the funds' top holdings: FLOORS, carrying the
           measured coverage, because a top-ten holdings cache sees only part of each fund.
  crash    each published S&P 500 fall applied to the stock dollars: "an allocation like yours", not this book's past.
  fee      the dollar-weighted expense ratio and what it costs a year; a fund with no ratio on record is named.
The Webull book is in no row: the caller hands over Fidelity books only (the owner's rule of 2026-08-26).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..models import BreakdownRow
from ..symbols import is_money_market
from .exposure import (_COMPLETE, BROKERAGE, CRASHES, ROLLED_UP, TREASURY_FUNDS, UNCLASSIFIED, Book, Sectors,
                       _by_sector, _listed, _pct)

# 2022: S&P 500 4796.56 on 2022-01-03 to 3577.03 on 2022-10-12, -25.43% by closing level (the same tables 0024 cites).
CRASHES_ALL: tuple[tuple[str, float], ...] = CRASHES + (("2022", 0.2543),)
TOP_COMPANIES = 12
US = "United States"
INTERNATIONAL = "International"
# Used ONLY where neither the warehouse nor the classifications feed knows a company's country, like
# `exposure.PLAN_SECTORS`: AstraZeneca has no row in the feed, and it is the book's one direct foreign company.
KNOWN_COUNTRIES: dict[str, str] = {"AZN": INTERNATIONAL}


def _region(name: str | None) -> str | None:
    """A country as one of the two buckets the page shows: the US, or anywhere else."""
    if not name:
        return None
    return US if name.strip().lower() in ("united states", "us", "usa") else INTERNATIONAL


@dataclass(frozen=True)
class Countries:
    """Where a country comes from: a company's own country, or a fund's published country map in percent."""
    company: dict[str, str] = field(default_factory=dict)
    funds: dict[str, dict[str, float]] = field(default_factory=dict)

    def of(self, symbol: str) -> str | None:
        return _region(self.company.get(symbol)) or KNOWN_COUNTRIES.get(symbol)


def _stocks(book: Book) -> dict[str, float]:
    """The book's stock dollars by symbol: everything but the Treasury ballast and money market."""
    return {s: v for s, v in book.held.items() if s not in TREASURY_FUNDS and not is_money_market(s)}


def _row(dimension: str, base: str, bucket: str, value: float | None, pct: float | None, day: str | None,
         src: tuple[dict, ...], *, line: float | None = None, direct: float | None = None, complete: int = 1,
         coverage: float | None = None, note: str | None = None, digits: int = 4) -> BreakdownRow:
    return BreakdownRow(dimension, base, bucket, None if value is None else round(value, 2),
                        None if pct is None else round(pct, digits), None if line is None else round(line, 4),
                        None if direct is None else round(direct, 2), complete, coverage, note, day, src)


def _asset(book: Book, day, src) -> list[BreakdownRow]:
    stocks = sum(_stocks(book).values())
    bonds = sum(v for s, v in book.held.items() if s in TREASURY_FUNDS)
    note = "Bonds are your Treasury ballast (VGIT); cash is what both accounts hold uninvested."
    return [_row("asset", ROLLED_UP, name, value, _pct(value, book.total), day, src, note=note)
            for name, value in (("stocks", stocks), ("bonds", bonds), ("cash", book.cash))]


def _sectors(book: Book, base: str, sectors: Sectors, plan: dict[str, float] | None, day, src) -> list[BreakdownRow]:
    held = _stocks(book) if base == ROLLED_UP else dict(book.held)
    actual, _ = _by_sector(held, book.funds, sectors)
    if base == BROKERAGE and book.cash > 0:
        actual[UNCLASSIFIED] = actual.get(UNCLASSIFIED, 0.0) + book.cash
    planned: dict[str, float] = {}
    if plan:
        by_plan, _ = _by_sector(plan, frozenset(sectors.funds), sectors)
        whole = sum(by_plan.values())
        planned = {k: _pct(v, whole) for k, v in by_plan.items()} if whole > 0 else {}
    note = ("Your stocks across both accounts by sector, funds split by their published sector maps; bonds and cash "
            "are in no sector. No line in your plan: it sets sector weights for the brokerage only."
            if base == ROLLED_UP else
            "The brokerage by sector against your plan's weights, funds split by their published sector maps.")
    names = sorted(set(actual) | set(planned))
    return [_row("sector", base, name, actual.get(name, 0.0), _pct(actual.get(name, 0.0), book.total), day, src,
                 line=planned.get(name, 0.0) if base == BROKERAGE and plan else None, note=note)
            for name in names]


def _country(book: Book, countries: Countries, day, src) -> list[BreakdownRow]:
    out: dict[str, float] = {}
    stocks = _stocks(book)
    for symbol, value in stocks.items():
        weights = countries.funds.get(symbol) if symbol in book.funds or symbol in countries.funds else None
        if weights and sum(weights.values()) > 0:
            whole = sum(weights.values())
            for name, pct in weights.items():
                key = _region(name) or UNCLASSIFIED
                out[key] = out.get(key, 0.0) + value * pct / whole
        else:
            key = countries.of(symbol) or UNCLASSIFIED
            out[key] = out.get(key, 0.0) + value
    total = sum(stocks.values())
    note = "Your stock dollars only, funds split by their published country maps; bonds and cash are left out."
    return [_row("country", ROLLED_UP, name, value, _pct(value, total) if total > 0 else None, day, src, note=note)
            for name, value in sorted(out.items())]


def _companies(book: Book, day, src) -> list[BreakdownRow]:
    direct, indirect = book.split()
    coverage, no_file = book.look_through_coverage()
    complete = 1 if coverage is None or coverage >= _COMPLETE else 0
    names = [(s, direct.get(s, 0.0), indirect.get(s, 0.0)) for s in set(direct) | set(indirect)
             if s not in book.funds and not is_money_market(s)]
    names.sort(key=lambda t: (-(t[1] + t[2]), t[0]))
    note = "Held directly plus through your funds' published top holdings: at least this much."
    if no_file:
        note += f" {_listed(no_file)}: no holdings on record, so nothing inside them is counted."
    return [_row("company", ROLLED_UP, s, d + i, _pct(d + i, book.total), day, src, direct=d, complete=complete,
                 coverage=coverage, note=note)
            for s, d, i in names[:TOP_COMPANIES]]


def _crashes(book: Book, day, src) -> list[BreakdownRow]:
    equity = sum(_stocks(book).values())
    note = ("A published S&P 500 fall, peak to bottom, applied to your stock dollars: an allocation like yours, not "
            "your book's own history. Treasury ballast and cash held flat.")
    return [_row("crash", ROLLED_UP, name, equity * fall, 100 * fall, day, src, note=note)
            for name, fall in CRASHES_ALL]


def _fee(book: Book, fees: dict[str, float], day, src) -> list[BreakdownRow]:
    funds = sorted(book.fund_values)
    known = [f for f in funds if f in fees]
    missing = [f for f in funds if f not in fees]
    # a fund with no ratio on record is left out of the average entirely (its dollars too), never counted at 0%:
    # that would pull the ratio down by a fee nobody knows. The yearly dollars are the known funds' own.
    base = book.total - sum(book.held[f] for f in missing)
    yearly = sum(book.held[f] * fees[f] for f in known) / 100
    pct = 100 * yearly / base if base > 0 else None
    note = "Each fund's expense ratio weighted by its dollars, over both accounts; stocks cost nothing to hold."
    if missing:
        note += f" Left out: {_listed(missing)} (no expense ratio on record), from the average and the dollars."
    value = None if pct is None else yearly
    return [_row("fee", ROLLED_UP, "weighted", value, pct, day, src, note=note, digits=6)]   # a ratio near 0.04%


def build_breakdown(brokerage: Book | None, rolled_up: Book | None, plan: dict[str, float], sectors: Sectors,
                    countries: Countries, fees: dict[str, float], day: str | None,
                    sources: tuple[dict, ...] = ()) -> list[BreakdownRow]:
    """Every row of the breakdown. `rolled_up` None (no Fidelity holdings read) gives nothing at all: a read that found
    nothing is not a book that holds nothing. `brokerage` None leaves out only the brokerage's sector rows."""
    if rolled_up is None or rolled_up.total <= 0:
        return []
    rows = _asset(rolled_up, day, sources)
    rows += _sectors(rolled_up, ROLLED_UP, sectors, None, day, sources)
    if brokerage is not None and brokerage.total > 0:
        rows += _sectors(brokerage, BROKERAGE, sectors, plan, day, sources)
    rows += _country(rolled_up, countries, day, sources)
    rows += _companies(rolled_up, day, sources)
    rows += _crashes(rolled_up, day, sources)
    rows += _fee(rolled_up, fees, day, sources)
    return rows
