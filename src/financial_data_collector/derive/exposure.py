"""Mr. Buffett's instrument panel: five exposure figures, each carrying the book it is measured on.

The panel is claude-os's page for the owner's two Fidelity accounts; the measures are the investing spec's
section 5 (docs/superpowers/specs/2026-11-06-mr-buffett-mr-bayes-design.md, read with its plan). This repo
computes and publishes them, as it does every valuation number, and claude-os only reads: a share, a gap or a
dollar loss on that page is whatever is stored here.

  top_three      brokerage           percent of the brokerage in its three largest holdings, against the plan's
                                     three largest targets.
  sector_spread  brokerage           the share of the brokerage that would have to move to match the plan's
                                     sector weights. The gap IS the line: zero is target.
  stacked        fidelity_rolled_up  the largest single company across both accounts, direct and through funds.
  ai_capex       brokerage           MSFT + NVDA + AMZN + GOOGL, direct and through funds, against their
                                     combined plan target.
  crash_cost     fidelity_rolled_up  dollars a published market fall would take off this allocation.

Four rules hold all five together:

- **Fidelity only.** The owner's standing rule (2026-08-26) lets the household TOTAL include his Webull book and
  keeps concentration and percentage-holdings math out of it. Every figure here is built from the configured
  accounts that are AT FIDELITY, checked against the warehouse's own institution, so a Webull account that
  someone adds to [allocation.accounts] cannot leak into a figure.
- **A base never mixes.** The same holdings read ~55% of the brokerage and ~17% rolled up. Each row says which
  book it is, and the panel shows it against that base's line only.
- **A floor says it is one.** Only the two figures that read fund constituent weights can be partial. Both always
  publish the coverage they measured, and set `look_through_complete = 0` while it is short of a whole fund, which
  is what tells a reader the value is a floor. The other three read no constituent weight at all, so they are
  complete and carry no coverage figure: borrowing one they never used would be a number about nothing.
- **No invented line.** `line_pct` comes from the owner's targets file, so it moves as he revises the plan and no
  code edit follows. Where his rulebook draws no line - stacked exposure, the crash - the column is NULL and the
  note says so. Inventing one there would create philosophy, which the spec forbids.

Nothing here fetches, and nothing here is a recommendation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from ..check.calendar import trading_days
from ..models import FUND_TYPES, ExposureRow
from ..symbols import is_money_market
from . import sources
from .targets import read_fund_sectors, read_look_through_with_coverage, read_targets

if TYPE_CHECKING:
    from ..config import Config
    from ..store import Store

FIGURES = ("top_three", "sector_spread", "stacked", "ai_capex", "crash_cost")
BROKERAGE = "brokerage"
ROLLED_UP = "fidelity_rolled_up"
FIDELITY = "fidelity"                  # accounts.institution of the two accounts this panel is about
BROKERAGE_TYPE = "brokerage"           # accounts.account_type of the one account the plan sets per-name targets for
TOP_N = 3
# Spec 5 measure 4 names this factor: these four, direct and through the funds. Not a cap, and never a gate on new
# money - rule 6 (a name over its target gets nothing) already blocks MSFT and NVDA.
AI_CAPEX = ("MSFT", "NVDA", "AMZN", "GOOGL")
UNCLASSIFIED = "unclassified"          # dollars, or plan weight, with no sector on record
NO_LINE = "Your rulebook draws no line here, so this one shows a reading, not a verdict."

# Published S&P 500 peak-to-trough falls, by closing level, deepest first. Spec decision 4: measure 5 applies
# published history to the CURRENT allocation and is labelled "an allocation like yours" - it is not a model of
# this book's own prices, and no price backfill stands behind it (`prices` starts 2021-09-27 and is not being
# extended; the only long history on this machine is the trading book's, which this page must not touch).
#   2007-09  1565.15 (2007-10-09) to  676.53 (2009-03-09)  -56.78%  (Wikipedia's bear-market table and an
#                                                                    Atlanta Fed note both give 56.8%)
#   2000-02  1527.46 (2000-03-24) to  776.76 (2002-10-09)  -49.19%
#   2020     3386.15 (2020-02-19) to 2237.40 (2020-03-23)  -33.92%  (reported as a ~34% fall in five weeks)
CRASHES: tuple[tuple[str, float], ...] = (("2007-09", 0.5678), ("2000-02", 0.4919), ("2020", 0.3392))
# Treasury funds: ballast, not equity, so a crash does not take the same bite out of them. VGIT is the owner's
# permanent sleeve (decisions.md 2026-09-21, reaffirmed by rule 13 as the rebalancing counterweight). Anything not
# named here counts as equity, which errs toward the larger loss rather than the comfortable one.
TREASURY_FUNDS = frozenset({"VGIT"})
# The sector of each name of the plan, from the spec's own section 5 table - used ONLY where the warehouse has no
# sector for the name. It has none for a name it has never collected: TJX and CB have no securities row at all,
# and dropping their 12% of the plan would shrink the plan's own denominator and understate a gap the owner really
# has, since he holds neither. A sector the warehouse knows always wins, so an entry here falls out of use by
# itself as the plan funds.
PLAN_SECTORS: dict[str, str] = {
    "V": "Financials", "BRK.B": "Financials", "CME": "Financials", "CB": "Financials",
    "LLY": "Health Care", "AZN": "Health Care",
    "MSFT": "Technology", "NVDA": "Technology",
    "AMZN": "Consumer Discretionary", "TJX": "Consumer Discretionary",
    "GOOGL": "Communication Services", "WM": "Industrials",
}
_COMPLETE = 99.5                       # a complete holdings file sums to about 100, never exactly


def _pct(part: float, whole: float) -> float:
    return round(100 * part / whole, 4)


def _money(amount: float) -> str:
    return f"${amount:,.0f}" if abs(amount) >= 1000 else f"${amount:,.2f}"


def _listed(names: list[str]) -> str:
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + f" and {names[-1]}"


@dataclass(frozen=True)
class Book:
    """One base's holdings: invested dollars by symbol, its uninvested cash, which of the symbols are funds, the
    look-through of the funds that have a file and each fund's measured coverage.

    A fund WITHOUT a file counts as a holding of itself, the way `split.household_of` counts one - its dollars are
    real and belong in the base, and pretending to know what is inside it would be worse than saying it is a floor.
    """
    held: dict[str, float]
    cash: float = 0.0
    funds: frozenset[str] = frozenset()
    look_through: dict[str, dict[str, float]] = field(default_factory=dict)
    coverage: dict[str, float] = field(default_factory=dict)

    @property
    def invested(self) -> float:
        return sum(self.held.values())

    @property
    def total(self) -> float:
        """Positions plus cash: the base the owner's percentages are of."""
        return self.invested + self.cash

    @property
    def fund_values(self) -> dict[str, float]:
        return {s: v for s, v in self.held.items() if s in self.funds}

    def split(self) -> tuple[dict[str, float], dict[str, float]]:
        """(direct, indirect): dollars held as themselves, and dollars reaching a name through a fund's weights."""
        direct: dict[str, float] = {}
        indirect: dict[str, float] = {}
        for symbol, value in self.held.items():
            weights = self.look_through.get(symbol) if symbol in self.funds else None
            if weights:
                for name, weight in weights.items():
                    indirect[name] = indirect.get(name, 0.0) + value * weight
            else:
                direct[symbol] = direct.get(symbol, 0.0) + value
        return direct, indirect

    def look_through_coverage(self) -> tuple[float | None, list[str]]:
        """(the dollar-weighted coverage of every fund held, the funds with no file at all).

        A fund with no file counts at 0: it is exactly why a figure built on this book is a floor, and leaving it
        out of the average would make the look-through look better than it is."""
        values = self.fund_values
        if not values:
            return None, []
        total = sum(values.values())
        if total <= 0:
            return None, sorted(s for s in values if s not in self.look_through)
        weighted = sum(v * self.coverage.get(s, 0.0) for s, v in values.items())
        return round(weighted / total, 4), sorted(s for s in values if s not in self.look_through)


@dataclass(frozen=True)
class Sectors:
    """Where a sector comes from: `company` is the warehouse's own sector per symbol, `funds` a fund's published
    sector map in percent (complete, unlike a top-ten holdings cache, which is why the sector figure can be
    measured against a line while the constituent figures are floors)."""
    company: dict[str, str] = field(default_factory=dict)
    funds: dict[str, dict[str, float]] = field(default_factory=dict)

    def of(self, symbol: str) -> str | None:
        return self.company.get(symbol) or PLAN_SECTORS.get(symbol)


@dataclass(frozen=True)
class ExposureReport:
    """What one publish did: how many figures it wrote, notes for the step's log (plan keys and counts, never an
    account label), or why it wrote nothing."""
    published: int
    notes: tuple[str, ...] = ()
    skipped: str | None = None


# ---------------------------------------------------------------- the measures

def _row(figure: str, base: str, unit: str, value: float | None, line: float | None, note: str | None,
         day: str | None, sources_: tuple[dict, ...], complete: int = 1,
         coverage: float | None = None) -> ExposureRow:
    return ExposureRow(figure, base, value, line, unit, complete, coverage, note, day, sources_)


def _plan_top_three(plan: dict[str, float]) -> float | None:
    """The plan's own three largest targets. Computed, never written down: the line follows the owner's file."""
    if len(plan) < TOP_N:
        return None
    return round(sum(sorted(plan.values(), reverse=True)[:TOP_N]), 4)


def _plan_note(plan: dict[str, float]) -> str:
    """What to add when the plan's named targets do not add up to the whole book."""
    total = sum(plan.values())
    if not plan or abs(total - 100) <= 0.5:
        return ""
    return f" The plan's {len(plan)} named targets add up to {total:g}%, not 100%."


def _top_three(book: Book | None, plan: dict[str, float], day: str | None, src: tuple[dict, ...]) -> ExposureRow:
    """Measure 1: the three largest holdings as a share of the brokerage, against the plan's three largest targets.

    Position level, as the plan names them - SCHD is one name with one target, not a basket to look through."""
    base_line = _plan_top_three(plan)
    if book is None or book.total <= 0:
        return _row("top_three", BROKERAGE, "pct", None, base_line,
                    "no brokerage holdings to measure", day, src)
    top = sorted(book.held.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_N]
    note = (f"{_listed([s for s, _ in top])}: your {len(top)} largest holdings, as a share of the brokerage "
            f"including its cash.")
    note += (" The line is the plan's own three largest targets." + _plan_note(plan)) if base_line is not None \
        else " No plan line: the targets file names fewer than three brokerage targets."
    return _row("top_three", BROKERAGE, "pct", _pct(sum(v for _, v in top), book.total), base_line, note, day, src)


def _by_sector(dollars: dict[str, float], funds: frozenset[str], sectors: Sectors
               ) -> tuple[dict[str, float], list[str]]:
    """(dollars by sector, the symbols that had no sector anywhere).

    A company goes to its own sector; a fund is spread by its published sector map, normalised by that map's own
    sum; anything with neither lands in `unclassified`, which keeps the total whole. Both sides of the gap are
    built this way, so a name that cannot be placed cancels instead of distorting."""
    out: dict[str, float] = {}
    unplaced: list[str] = []
    for symbol, value in dollars.items():
        sector = sectors.of(symbol)
        weights = sectors.funds.get(symbol) if symbol in funds or symbol in sectors.funds else None
        if sector and symbol not in funds:
            out[sector] = out.get(sector, 0.0) + value
        elif weights and sum(weights.values()) > 0:
            whole = sum(weights.values())
            for name, pct in weights.items():
                out[name] = out.get(name, 0.0) + value * pct / whole
        else:
            out[UNCLASSIFIED] = out.get(UNCLASSIFIED, 0.0) + value
            unplaced.append(symbol)
    return out, sorted(unplaced)


def _sector_spread(book: Book | None, plan: dict[str, float], sectors: Sectors, day: str | None,
                   src: tuple[dict, ...]) -> ExposureRow:
    """Measure 2: half the sum of the absolute sector differences - the share of the brokerage that would have to
    move to match the plan. The gap is the line, and zero is target.

    Not a floor: both sides use the funds' COMPLETE published sector maps, and the dollars neither side can place
    sit in `unclassified` on both, where they count as the gap they are rather than vanishing."""
    if book is None or book.invested <= 0 or not plan:
        why = "no brokerage holdings to measure" if book is None or book.invested <= 0 \
            else "no plan to measure against: the targets file names no brokerage targets"
        return _row("sector_spread", BROKERAGE, "pct", None, 0.0 if plan else None, why, day, src)
    actual, unplaced_held = _by_sector(book.held, book.funds, sectors)
    if book.cash > 0:                       # uninvested cash is in no sector, and being off-plan is the point
        actual[UNCLASSIFIED] = actual.get(UNCLASSIFIED, 0.0) + book.cash
    planned, unplaced_plan = _by_sector(plan, frozenset(sectors.funds), sectors)
    a_total, p_total = sum(actual.values()), sum(planned.values())
    worst, gap = None, 0.0
    for sector in set(actual) | set(planned):
        diff = _pct(actual.get(sector, 0.0), a_total) - _pct(planned.get(sector, 0.0), p_total)
        gap += abs(diff)
        if worst is None or abs(diff) > abs(worst[1]):
            worst = (sector, diff)
    note = "The share of the brokerage that would have to move to match your plan's sectors; zero is on plan."
    if worst is not None:
        where, diff = worst
        note += (f" {where} is furthest from plan: {_pct(actual.get(where, 0.0), a_total):.1f}% held against "
                 f"{_pct(planned.get(where, 0.0), p_total):.1f}% planned.")
    unplaced = sorted(set(unplaced_held) | set(unplaced_plan))
    if unplaced:
        note += f" {_listed(unplaced)}: no sector on record, counted as unclassified on both sides."
    return _row("sector_spread", BROKERAGE, "pct", round(gap / 2, 4), 0.0, note + _plan_note(plan), day, src)


def _stacked(book: Book | None, day: str | None, src: tuple[dict, ...]) -> ExposureRow:
    """Measure 3: the largest single company across both Fidelity accounts, held directly and through the funds.

    Funds are ranked out: a fund is a wrapper, not a stacked name, and its own weight is measure 1's question.
    A floor - more look-through can only add exposure, never take it away."""
    coverage, no_file = book.look_through_coverage() if book is not None else (None, [])
    complete = 1 if coverage is None or coverage >= _COMPLETE else 0
    if book is None or book.total <= 0:
        return _row("stacked", ROLLED_UP, "pct", None, None, "no Fidelity holdings to measure", day, src,
                    complete, coverage)
    direct, indirect = book.split()
    names = [(s, direct.get(s, 0.0) + indirect.get(s, 0.0)) for s in set(direct) | set(indirect)
             if s not in book.funds and not is_money_market(s)]
    if not names:
        return _row("stacked", ROLLED_UP, "pct", None, None, "no company holdings to measure", day, src,
                    complete, coverage)
    names.sort(key=lambda kv: (-kv[1], kv[0]))
    top, value = names[0]
    note = (f"{top} is your largest single company across both accounts: {_pct(value, book.total):.1f}% of them - "
            f"{_pct(direct.get(top, 0.0), book.total):.1f}% held directly and "
            f"{_pct(indirect.get(top, 0.0), book.total):.1f}% through your funds.")
    if len(names) > 1:
        note += " Then " + _listed([f"{s} {_pct(v, book.total):.1f}%" for s, v in names[1:3]]) + "."
    if complete == 0:
        note += (f" A floor: the holdings on file cover {coverage:.1f}% of your funds"
                 + (f", and {_listed(no_file)} none at all" if no_file else "") + ".")
    note += " " + NO_LINE
    return _row("stacked", ROLLED_UP, "pct", _pct(value, book.total), None, note, day, src, complete,
                coverage)


def _ai_capex(book: Book | None, plan: dict[str, float], day: str | None, src: tuple[dict, ...]) -> ExposureRow:
    """Measure 4: the AI-capex names as a share of the brokerage, direct and through its funds, against the sum of
    their plan targets.

    It reads high while the other nine names are unfunded, and falls on its own as they fund. Not a cap."""
    line = round(sum(plan.get(s, 0.0) for s in AI_CAPEX), 4) if any(s in plan for s in AI_CAPEX) else None
    coverage, no_file = book.look_through_coverage() if book is not None else (None, [])
    complete = 1 if coverage is None or coverage >= _COMPLETE else 0
    if book is None or book.total <= 0:
        return _row("ai_capex", BROKERAGE, "pct", None, line, "no brokerage holdings to measure", day, src,
                    complete, coverage)
    direct, indirect = book.split()
    value = sum(direct.get(s, 0.0) + indirect.get(s, 0.0) for s in AI_CAPEX)
    note = (f"{_listed(list(AI_CAPEX))}, held directly and through your funds, as a share of the brokerage. "
            f"It reads high because nine of the plan's names are still unfunded, and it falls on its own as they "
            f"fund: a reading, not a cap, and never a reason to hold money back.")
    if line is None:
        note += " No plan line: the targets file names none of these four."
    if complete == 0:
        note += (f" A floor: the holdings on file cover {coverage:.1f}% of the brokerage's funds"
                 + (f", and {_listed(no_file)} none at all" if no_file else "") + ".")
    return _row("ai_capex", BROKERAGE, "pct", _pct(value, book.total), line, note + _plan_note(plan), day, src,
                complete, coverage)


def _crash_cost(book: Book | None, day: str | None, src: tuple[dict, ...]) -> ExposureRow:
    """Measure 5: what a published market fall would take off this allocation, in dollars.

    The deepest fall on record is the figure; the shallower two are in the note. Cash does not fall and Treasury
    ballast is not treated as equity. Labelled "an allocation like yours", because that is what it is: published
    history applied to today's weights, not this book's own past."""
    if book is None or book.total <= 0:
        return _row("crash_cost", ROLLED_UP, "usd", None, None, "no Fidelity holdings to measure", day, src)
    equity = sum(v for s, v in book.held.items() if s not in TREASURY_FUNDS and not is_money_market(s))
    if equity <= 0:
        return _row("crash_cost", ROLLED_UP, "usd", None, None, "no equity in the book to fall", day, src)
    ballast = book.total - equity
    deepest, fall = CRASHES[0]
    others = ", ".join(f"{name} {_money(equity * share)}" for name, share in CRASHES[1:])
    note = (f"If the {deepest} fall of {100 * fall:.0f}% in the S&P 500 happened again to an allocation like "
            f"yours, not to your book itself: {_money(equity * fall)} off {_money(book.total)}. "
            f"The shallower falls on record: {others}. "
            f"{_money(ballast)} is Treasury ballast and cash, held flat here. {NO_LINE}")
    return _row("crash_cost", ROLLED_UP, "usd", round(equity * fall, 2), None, note, day, src)


def build_exposure(brokerage: Book | None, rolled_up: Book | None, plan: dict[str, float], sectors: Sectors,
                   day: str | None, sources: tuple[dict, ...] = ()) -> list[ExposureRow]:
    """The five figures, in the spec's own order. Pure: no SQL, no store, no files.

    `brokerage` is None when no configured Fidelity account is a brokerage - then its three figures have no value
    and say so, rather than falling back to another account and answering a question about a book nobody asked
    about. Every row gets `day` and `sources` as given."""
    return [
        _top_three(brokerage, plan, day, sources),
        _sector_spread(brokerage, plan, sectors, day, sources),
        _stacked(rolled_up, day, sources),
        _ai_capex(brokerage, plan, day, sources),
        _crash_cost(rolled_up, day, sources),
    ]


# ---------------------------------------------------------------- the lifecycle

def plan_of(targets, account_key: str) -> dict[str, float]:
    """The whole 13-name plan for the brokerage: every entry that sends money there, live or planned.

    `pct_of_brokerage` entries wherever they are filed, plus that account's own `pct_of_account` ones, plus the
    `planned` names - the plan's lines need all thirteen, because the live rows alone sum to 59% and every line
    built on that denominator would be wrong. A ratio entry has no percentage and is left out. This is the ONLY
    place `planned` is read: it never reaches the weekly split, which would send real money to a name the owner
    deliberately withheld it from."""
    if targets is None:
        return {}
    out: dict[str, float] = {}
    for entry in list(targets.entries) + list(targets.planned):
        if entry.kind == "pct_of_brokerage" or (entry.kind == "pct_of_account" and entry.account == account_key):
            out[entry.symbol] = out.get(entry.symbol, 0.0) + entry.target
    return out


def _books(store: Store, labels: dict[str, str], brokerage_key: str | None, look_through: dict[str, dict[str, float]],
           coverage: dict[str, float]) -> tuple[Book | None, Book | None, str | None]:
    """(the brokerage's book, the rolled-up Fidelity book, the newest positions date), from the warehouse.

    Money market holdings are left out of the holdings and the cash row stands for them instead: SnapTrade reports
    the sweep twice, once as a position and once as cash, and counting both would inflate every base."""
    wanted = set(labels.values())
    positions = [r for r in store.split_positions() if r["account"] in wanted]
    cash_rows = {k: v for k, v in store.split_cash().items() if k in wanted}
    if not positions:
        return None, None, None
    types = store.split_asset_types(sorted({r["symbol"] for r in positions} | set(look_through)))
    day = max(r["as_of_date"] for r in positions)

    def book(for_labels: set[str]) -> Book:
        held: dict[str, float] = {}
        for row in positions:
            symbol = row["symbol"]
            if row["account"] not in for_labels:
                continue
            if row.get("asset_type") == "money_market" or types.get(symbol) == "money_market" \
                    or is_money_market(symbol):
                continue
            held[symbol] = held.get(symbol, 0.0) + float(row["market_value"] or 0.0)
        cash = sum(float(cash_rows[label][1] or 0.0) for label in for_labels if label in cash_rows)
        funds = frozenset(s for s in held if types.get(s) in FUND_TYPES or s in look_through)
        return Book(held, cash, funds, {f: w for f, w in look_through.items() if f in funds},
                    {f: c for f, c in coverage.items() if f in funds})

    brokerage = book({labels[brokerage_key]}) if brokerage_key else None
    return brokerage, book(wanted), day


def run(store: Store, cfg: Config) -> ExposureReport:
    """Rebuild `exposure_figures_raw` from the warehouse, the targets file and the funds' published weights.

    Derived, like the split's own tables: rebuilt from scratch, a row already right not rewritten. It runs in the
    derive step rather than under the engine's gate because no figure here reads a valuation score - the panel
    must light up on a sync whose scores were held back, and the owner cannot tell a dark meter from a broken one.

    Writes nothing, keeping whatever was published before, when no configured Fidelity account has positions: a
    read that found nothing is not the same as a book that holds nothing."""
    alloc = cfg.allocation
    notes: list[str] = []
    institutions = store.account_institutions()
    labels: dict[str, str] = {}
    for key, label in sorted(alloc.accounts.items()):
        if institutions.get(label) == FIDELITY:
            labels[key] = label
        else:
            notes.append(f"{key}: not at fidelity, left out of every figure")
    if not labels:
        return ExposureReport(0, tuple(notes), "no Fidelity account in [allocation.accounts]")

    types = store.split_account_types()
    brokerages = [k for k, label in labels.items() if types.get(label) == BROKERAGE_TYPE]
    brokerage_key = brokerages[0] if len(brokerages) == 1 else None
    if brokerage_key is None:
        notes.append(f"{len(brokerages)} brokerage accounts configured, not one: the brokerage figures have no base")

    try:
        targets = read_targets(alloc.targets)
    except ValueError as e:
        targets = None
        notes.append(f"targets file unreadable: {e}")
    look_through, coverage = read_look_through_with_coverage(alloc.fund_holdings)
    fund_sectors = read_fund_sectors(alloc.fund_holdings)
    brokerage, rolled_up, day = _books(store, labels, brokerage_key, look_through, coverage)
    if rolled_up is None:
        return ExposureReport(0, tuple(notes), "no positions in any configured Fidelity account")

    plan = plan_of(targets, brokerage_key) if brokerage_key else {}
    held = sorted(set(rolled_up.held) | set(plan))
    sectors = Sectors(store.company_sectors(held), fund_sectors)
    src: list[dict] = [sources.derived("positions_latest", "fidelity", day)]
    if targets is not None:
        src.append(sources.feed(targets.file_name, targets.last_revised, "brokerage"))
    for fund in sorted(rolled_up.fund_values):
        if fund in look_through:
            src.append(sources.feed(f"{fund}.csv", None, fund))
    rows = build_exposure(brokerage, rolled_up, plan, sectors, day, tuple(src))
    return ExposureReport(store.replace_exposure_figures(rows), tuple(notes))


def publish_trading_days(store: Store) -> int:
    """Rebuild `trading_days_raw`: every NYSE trading day by rule within the span of the prices on record.

    The `trading_days` view then serves the days of these that have a final bar. Both halves are needed: a
    consumer in another repo cannot import `check/calendar.py`, and the rule is what keeps a bad vendor row on a
    holiday out, while the join on `final = 1` is what keeps a bar fetched before its close out. Returns how many
    days the view can serve."""
    span = store.price_date_span()
    if span is None:
        store.replace_trading_days([])
        return 0
    first, last = span
    try:
        days = trading_days(date.fromisoformat(first), date.fromisoformat(last))
    except ValueError:
        return 0
    store.replace_trading_days([d.isoformat() for d in days])
    return store.trading_day_count()
