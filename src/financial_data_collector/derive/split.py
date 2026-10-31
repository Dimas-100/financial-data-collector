"""The weekly split: how cash waiting in an account is shared across holdings.

`holding_band`: one band (`cheap`, `usual` or `premium`) for a holding, read from its primary valuation scores and
leaning careful; the rows are the symbol's `valuation_scores` view rows as dicts. `household_of` and `plan_split`:
the household's holdings with fund look-through, and the split of one account's waiting cash in whole dollars by
target gap and band, inside every band top, sleeve max and cap. Those are pure: no SQL, no store, no files.

`run` is the lifecycle, called by the engine step after the scores and their re-check (only when its gate is open): for each account
named in config `[allocation.accounts]` it reads the waiting cash, the targets file and the scores (through Store
reads) and opens, replaces or closes that account's proposal. Proposals are a record: a run only adds one or closes
one, never rewrites an open one, and a run with the same cash writes nothing. A proposal, never an order.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Iterable

from ..models import FUND_TYPES, AllocationProposal, SplitLine, TargetEntry, Targets
from . import sources
from .targets import read_look_through, read_targets

if TYPE_CHECKING:
    from ..config import Config
    from ..store import Store

PRIMARY_COMPANY = ("pe", "p_fcf", "ev_ebit", "ps")
PRIMARY_FUND = ("pe", "dividend_yield")
QUALITY = ("fcf_margin_change", "op_margin_change")
MIN_AVAILABLE = 2                     # fewer primary metrics with a band than this say nothing


def _status_code(row: dict, metric: str) -> str:
    """Why a primary row that is not `ok` with a band does not count."""
    status = row.get("status")
    if status == "held":
        return f"held:{metric}"
    if status == "not_enough_history":
        return f"no_history:{metric}"
    return f"unsupported:{metric}"


def _known(row: dict | None) -> bool:
    return row is not None and row.get("status") == "ok" and row.get("value") is not None


def holding_band(rows: list[dict], is_fund: bool) -> tuple[str, tuple[str, ...], tuple[dict, ...]]:
    """A holding's band, the reason codes, and the rows that decided it.

    rows: the symbol's `valuation_scores` rows ({metric, value, band, status, date, ...}). A company is read from
    `pe`, `p_fcf`, `ev_ebit` and `ps`, a fund from `pe` and `dividend_yield`; a metric counts only when its row is
    `ok` with a band. Fewer than two counted -> `usual` (`one_metric` / `no_metrics`). More than half `premium` ->
    `premium`. No `premium` and more than half `cheap` -> `cheap`, unless a company's free-cash-flow and operating
    margins are both known and both falling (-> `usual`, `quality_falling`); when either margin is not known `cheap`
    stands and says `quality_unknown`. Anything else -> `usual`, `disagree`.

    Reasons: primary metrics that did not count first, in PRIMARY order (`held:<m>`, `unsupported:<m>`,
    `no_history:<m>`, `missing:<m>`), then the verdict codes. The rows returned are the counted primary rows, plus the
    two quality rows when they changed or qualified the result. No code holds a comma (a line's reasons are stored
    comma-joined): `cheap_by:` / `premium_by:` join their metrics with `+` (`cheap_by:pe+p_fcf`).
    """
    primary = PRIMARY_FUND if is_fund else PRIMARY_COMPANY
    by_metric = {r["metric"]: r for r in rows}
    reasons: list[str] = []
    counted: list[tuple[str, dict]] = []
    for metric in primary:
        row = by_metric.get(metric)
        if row is None:
            reasons.append(f"missing:{metric}")
        elif row.get("status") == "ok" and row.get("band") is not None:
            counted.append((metric, row))
        else:
            reasons.append(_status_code(row, metric))
    deciding = tuple(r for _, r in counted)

    n = len(counted)
    if n < MIN_AVAILABLE:
        reasons.append("one_metric" if n == 1 else "no_metrics")
        return "usual", tuple(reasons), deciding

    premium = [m for m, r in counted if r["band"] == "premium"]
    cheap = [m for m, r in counted if r["band"] == "cheap"]
    if 2 * len(premium) > n:
        reasons.append("premium_by:" + "+".join(premium))
        return "premium", tuple(reasons), deciding
    if not premium and 2 * len(cheap) > n:
        if is_fund:
            reasons.append("cheap_by:" + "+".join(cheap))
            return "cheap", tuple(reasons), deciding
        quality = [by_metric.get(m) for m in QUALITY]
        quality_rows = tuple(q for q in quality if q is not None)
        if all(_known(q) for q in quality) and all(q["value"] < 0 for q in quality):
            reasons.append("quality_falling")
            return "usual", tuple(reasons), deciding + quality_rows
        reasons.append("cheap_by:" + "+".join(cheap))
        if not all(_known(q) for q in quality):
            reasons.append("quality_unknown")
            return "cheap", tuple(reasons), deciding + quality_rows
        return "cheap", tuple(reasons), deciding
    reasons.append("disagree")
    return "usual", tuple(reasons), deciding


# ---------------------------------------------------------------------------------------------------------------------
# The split itself
# ---------------------------------------------------------------------------------------------------------------------

TOL = 0.005               # dollars: a top, a room or a remainder this close to zero is reached
_FLOOR_EPS = 1e-6         # a share of 149.9999999 dollars is 150 whole dollars, not 149
_FILL_RULES = ("trades", "contributions")


@dataclass(frozen=True)
class Household:
    """Every counted account together: `total` (positions plus cash), each name's `direct` dollars, its `indirect`
    dollars through funds with a look-through file, the look-through itself, which symbols are funds, and the
    dollars held in each fund (`fund_values`, with a file or not) for the caps. `untyped`: symbols whose asset type is
    unknown or missing and that have no look-through file; they are in `funds` (for caps, a fund without a file, at
    the assumed weight) and their lines say `type_unknown`."""
    total: float
    direct: dict[str, float]
    indirect: dict[str, float]
    look_through: dict[str, dict[str, float]]
    funds: frozenset[str]
    fund_values: dict[str, float] = field(default_factory=dict)
    untyped: frozenset[str] = frozenset()


@dataclass(frozen=True)
class SplitPlan:
    """The split of one account's waiting cash: its lines (dollars descending, then symbol), what waits for next
    week, and a note `{"symbol", "reason"}` for every entry of the account that ended with no line."""
    lines: tuple[SplitLine, ...]
    waits: float
    notes: tuple[dict, ...]


def household_of(positions: list[dict], cash: dict[str, float], look_through: dict[str, dict[str, float]],
                 fund_symbols: frozenset[str], untyped: frozenset[str] = frozenset()) -> Household:
    """The household from the counted accounts' positions (`account`, `symbol`, `market_value`) and cash.

    A fund with a look-through file adds value x weight to each name it holds (`indirect`); the rest of the fund adds
    to no name. A fund without a file, and anything that is not a fund, counts as a direct holding of itself. Every
    fund's held dollars are also kept in `fund_values`. `untyped` (a subset of `fund_symbols`) is kept as given."""
    direct: dict[str, float] = {}
    indirect: dict[str, float] = {}
    fund_values: dict[str, float] = {}
    total = sum(float(v or 0.0) for v in cash.values())
    for row in positions:
        symbol, value = row["symbol"], float(row.get("market_value") or 0.0)
        total += value
        if symbol in fund_symbols:
            fund_values[symbol] = fund_values.get(symbol, 0.0) + value
        if symbol in fund_symbols and symbol in look_through:
            for name, weight in look_through[symbol].items():
                indirect[name] = indirect.get(name, 0.0) + value * weight
        else:
            direct[symbol] = direct.get(symbol, 0.0) + value
    return Household(total, direct, indirect, dict(look_through), frozenset(fund_symbols), fund_values,
                     frozenset(untyped))


def with_pending(household: Household, lines: Iterable[tuple[str, float]], *, add_to_total: bool) -> Household:
    """The household with another account's open proposal lines (symbol, dollars) counted as pending holdings, the
    way household_of counts a holding: a fund with a file through its weights (`indirect`), a fund without one as
    itself (`direct`) and in `fund_values` (so a cap charges it at the assumed weight), anything else `direct`. The
    total grows only with `add_to_total`: a line's dollars are that account's cash, already in the total when its cash
    row is counted. Returns a new Household; the one given is not changed."""
    direct, indirect, fund_values = dict(household.direct), dict(household.indirect), dict(household.fund_values)
    total = household.total
    for symbol, dollars in lines:
        d = float(dollars)
        if add_to_total:
            total += d
        if symbol in household.funds:
            fund_values[symbol] = fund_values.get(symbol, 0.0) + d
        if symbol in household.funds and symbol in household.look_through:
            for name, weight in household.look_through[symbol].items():
                indirect[name] = indirect.get(name, 0.0) + d * weight
        else:
            direct[symbol] = direct.get(symbol, 0.0) + d
    return Household(total, direct, indirect, household.look_through, household.funds, fund_values, household.untyped)


def _account_entries(account: str, targets: Targets) -> list[TargetEntry]:
    """The entries that send money to `account`: its own (not `pct_of_brokerage`), and every `pct_of_brokerage`
    entry wherever it is filed when the account is the brokerage."""
    return [e for e in targets.entries
            if (e.kind == "pct_of_brokerage" and account == "brokerage")
            or (e.kind != "pct_of_brokerage" and e.account == account)]


def _assumed_weight(household: Household, name: str) -> float:
    """The largest weight of `name` in any look-through file on record (0 when no file lists it): what a fund
    without a file is taken to hold of it."""
    return max((float(w.get(name, 0.0)) for w in household.look_through.values()), default=0.0)


def cap_used(household: Household, name: str) -> float:
    """Dollars of `name` the household already holds, for a cap: held directly, through funds with a file, its own
    value when `name` is itself a fund with a file (`direct` leaves such a fund out), and every held fund without a
    file taken at the assumed weight of `name` (conservative: missing look-through never makes room)."""
    used = household.direct.get(name, 0.0) + household.indirect.get(name, 0.0)
    if name in household.funds and name in household.look_through:
        used += household.fund_values.get(name, 0.0)
    weight = _assumed_weight(household, name)
    if weight > 0:
        for fund, value in household.fund_values.items():
            if fund != name and fund in household.funds and fund not in household.look_through:
                used += value * weight
    return used


@dataclass
class _Line:
    symbol: str
    gap: float
    top: float
    band: str
    tilt: float
    weight: float
    reasons: list[str]


@dataclass(frozen=True)
class _Group:
    code: str                  # sleeve_cut:<label, any comma as +> | cap_cut:<symbol> (never a comma: reasons are comma-joined)
    room: float                # dollars (a cap's: dollars of the capped name), never below 0
    coef: dict[str, float]     # line symbol -> how much of the room one dollar on that line uses (only > 0)

    def used(self, amounts: dict[str, float]) -> float:
        return sum(c * amounts.get(s, 0.0) for s, c in self.coef.items())


def _groups(account: str, base: float, held: dict[str, float], household: Household, targets: Targets,
            lines: dict[str, _Line]) -> list[_Group]:
    """Each sleeve of this account with a max, then each cap, as `sum(coef x line) <= room`; a group no line is in
    is left out. A fund without a look-through file is taken at the largest weight of the capped name in any file
    on record (its line says `look_through_assumed`); a name that is neither the capped one nor a fund is not in it."""
    groups: list[_Group] = []
    for sleeve in targets.sleeves:
        if sleeve.account != account or sleeve.high is None:
            continue
        coef = {s: 1.0 for s in sorted(lines) if s in sleeve.members}
        if not coef:
            continue
        room = sleeve.high / 100 * base - sum(float(held.get(m, 0.0)) for m in set(sleeve.members))
        groups.append(_Group(f"sleeve_cut:{sleeve.label.replace(',', '+')}", max(room, 0.0), coef))
    for cap in targets.caps:
        name = cap.symbol
        coef = {}
        for s in sorted(lines):
            if s == name:
                c = 1.0
            elif s in household.funds and s in household.look_through:
                c = float(household.look_through[s].get(name, 0.0))
            elif s in household.funds:
                c = _assumed_weight(household, name)
                if c > 0 and "look_through_assumed" not in lines[s].reasons:
                    lines[s].reasons.append("look_through_assumed")
            else:
                c = 0.0
            if c > 0:
                coef[s] = c
        if not coef:
            continue
        room = cap.high / 100 * household.total - cap_used(household, name)
        groups.append(_Group(f"cap_cut:{name}", max(room, 0.0), coef))
    return groups


def _fill(cash: float, lines: dict[str, _Line], groups: list[_Group]) -> tuple[dict[str, float], dict[str, list[str]]]:
    """Progressive filling: every active line grows in proportion to its weight until a top or a group stops one;
    the stopped lines drop out and the rest share what is left. Returns each line's dollars (not yet rounded) and,
    for a stopped line, what stopped it (group codes, then `band_top`)."""
    syms = sorted(lines)
    x = {s: 0.0 for s in syms}
    stops: dict[str, list[str]] = {s: [] for s in syms}
    for s in syms:
        if lines[s].top <= TOL:
            stops[s].append("band_top")
    active = [s for s in syms if lines[s].weight > 0 and lines[s].top > TOL]
    remaining = cash
    for _ in range(len(syms) + 2):          # every round but the last stops at least one line
        if remaining <= TOL or not active:
            break
        total_weight = sum(lines[s].weight for s in active)
        inc = {s: remaining * lines[s].weight / total_weight for s in active}
        k = 1.0
        for s in active:
            k = min(k, (lines[s].top - x[s]) / inc[s])
        for g in groups:
            use = sum(g.coef.get(s, 0.0) * inc[s] for s in active)
            if use > 0:
                k = min(k, (g.room - g.used(x)) / use)
        k = max(k, 0.0)
        for s in active:
            x[s] += k * inc[s]
        remaining -= k * sum(inc.values())
        full = [g for g in groups if g.room - g.used(x) <= TOL]
        still = []
        for s in active:
            why = [g.code for g in full if s in g.coef]
            if lines[s].top - x[s] <= TOL:
                why.append("band_top")
            if why:
                stops[s] = why
            else:
                still.append(s)
        active = still
        if k >= 1.0:
            break
    return x, stops


def _within_cash(dollars: dict[str, int], kept: list[str], cash: float, min_line: int) -> list[str]:
    """Guard: trims the lines (smallest first) until they sum to at most the cash, and drops a line it trims under
    `min_line` (its dollars wait). Floors of shares that sum to at most the cash cannot pass it, so this never cuts
    in practice. Changes `dollars` in place; returns the lines kept."""
    over = sum(dollars[s] for s in kept) - int(math.floor(cash + 1e-9))
    for s in sorted(kept, key=lambda s: (dollars[s], s)):
        if over <= 0:
            break
        cut = min(over, dollars[s])
        dollars[s] -= cut
        over -= cut
    return [s for s in kept if dollars[s] >= min_line]


def plan_split(account: str, cash: float, base: float, held: dict[str, float], household: Household,
               targets: Targets, bands: dict[str, tuple[str, tuple[str, ...]]], tilt: dict[str, float],
               min_line: int) -> SplitPlan:
    """Split `cash` waiting in `account` across its target entries, in whole dollars.

    account: the targets file's account name (`brokerage` also takes every `pct_of_brokerage` entry). base: the
    account's positions plus its cash. held: its positions by symbol. bands: {symbol: (band, reasons)}; a symbol
    without one is `usual` (`no_scores`). tilt: the factor per band. A percent entry's gap is target% x base - held
    and its top max% x base - held; a ratio entry's gap is target x via-funds - direct (household-wide) and its top
    max x via-funds - direct. Money goes by gap x tilt, never past a top, a sleeve's max (its members in this account)
    or a cap (the name household-wide, funds through their look-through; see `cap_used`). Whole dollars (floored);
    a line under `min_line` is folded into the largest line as far as that line's room allows; when no line reaches
    `min_line`, everything placed is folded into the largest share (ties alphabetical) within its room, and waits if
    that is still under `min_line`; the rest waits. A name with any `hold` entry in the account is `hold`, even after
    a duplicate that says otherwise. A name in `household.untyped` says `type_unknown`. Never
    more than the cash, never a `hold` or unknown-rule entry. With no cash (0.005 or less) every entry that could
    have had money is noted `no_cash`.
    """
    cash_in = max(float(cash), 0.0)
    entries = _account_entries(account, targets)
    on_hold = {e.symbol for e in entries if e.rule == "hold"}      # any hold entry of a name in this account wins
    reason_of: dict[int, str] = {}
    seen: set[str] = set()
    lines: dict[str, _Line] = {}
    for i, e in enumerate(entries):
        s = e.symbol
        if s in seen:
            reason_of[i] = "duplicate"
            continue
        seen.add(s)
        if s in on_hold:
            reason_of[i] = "hold"
            continue
        if e.rule not in _FILL_RULES:
            reason_of[i] = "unknown_rule"
            continue
        if e.kind == "direct_passive_ratio":
            via_funds = household.indirect.get(s, 0.0)
            if via_funds <= 0:
                reason_of[i] = "no_look_through"
                continue
            own = household.direct.get(s, 0.0)
            gap, top = max(0.0, e.target * via_funds - own), e.high * via_funds - own
        else:
            own = float(held.get(s, 0.0))
            gap, top = max(0.0, e.target / 100 * base - own), e.high / 100 * base - own
        if gap <= TOL:
            reason_of[i] = "at_or_over_target"
            continue
        reasons: list[str] = []
        if s in bands:
            band, band_reasons = bands[s]
            reasons.extend(band_reasons)
        else:
            band = "usual"
        if e.rule == "trades":
            reasons.append("trades_as_contributions")
        if s not in bands:
            reasons.append("no_scores")
        if s in household.untyped:
            reasons.append("type_unknown")
        factor = float(tilt[band])
        lines[s] = _Line(s, gap, top, band, factor, gap * factor, reasons)

    groups = _groups(account, base, held, household, targets, lines)
    x, stops = _fill(cash_in, lines, groups)

    # Whole dollars; lines under the minimum go and their dollars fold into the largest line within its room.
    floors = {s: int(math.floor(x[s] + _FLOOR_EPS)) for s in sorted(lines)}
    dollars = dict(floors)
    kept = [s for s in sorted(lines) if dollars[s] >= min_line]
    pool = sum(dollars[s] for s in lines if s not in kept)
    folded = None
    if kept and pool > 0:
        r = min(kept, key=lambda s: (-dollars[s], s))
        on_kept = {s: float(dollars[s]) for s in kept}
        room = lines[r].top - dollars[r]
        for g in groups:
            if r in g.coef:
                room = min(room, (g.room - g.used(on_kept)) / g.coef[r])
        add = min(pool, max(0, int(math.floor(room + _FLOOR_EPS))))
        if add > 0:
            dollars[r] += add
            folded = r
    elif not kept and lines:
        # No line reaches the minimum: everything the fill placed goes to the largest share (ties alphabetical), as
        # far as its room allows (its top, every group over it); still under the minimum, everything waits.
        r = min(lines, key=lambda s: (-x[s], s))
        room = lines[r].top
        for g in groups:
            if r in g.coef:
                room = min(room, g.room / g.coef[r])
        amount = min(int(math.floor(sum(x.values()) + _FLOOR_EPS)), max(0, int(math.floor(room + _FLOOR_EPS))))
        if amount >= min_line:
            dollars[r] = amount
            kept, folded = [r], r
    kept = _within_cash(dollars, kept, cash_in, min_line)

    out = []
    for s in kept:
        line = lines[s]
        reasons = line.reasons + stops[s] + (["folded"] if s == folded else [])
        out.append(SplitLine(s, dollars[s], round(line.gap, 2), line.band, line.tilt, tuple(reasons)))
    out.sort(key=lambda line: (-line.dollars, line.symbol))

    notes = []
    for i, e in enumerate(entries):
        s = e.symbol
        if i in reason_of:
            notes.append({"symbol": s, "reason": reason_of[i]})
        elif s not in kept:
            # No cash: that is why. A group or its top left it at nothing: that is why. Else its share was under
            # the minimum.
            if cash_in <= TOL:
                why = "no_cash"
            else:
                why = stops[s][0] if floors[s] == 0 and stops[s] else "under_min"
            notes.append({"symbol": s, "reason": why})
    waits = round(cash_in - sum(line.dollars for line in out), 2)
    return SplitPlan(tuple(out), waits, tuple(notes))


# ---------------------------------------------------------------------------------------------------------------------
# The lifecycle: open, replace and close proposals as each account's waiting cash moves
# ---------------------------------------------------------------------------------------------------------------------

_STAMP = "%Y-%m-%dT%H:%M:%SZ"     # Store.utcnow's format: created_at and closed_at
_CMP_EPS = 1e-6                   # dollars: 525.0 counts as 500 + 25 whatever the float leftovers


@dataclass(frozen=True)
class SplitReport:
    """What one run did: proposals opened (none was open), replaced (more cash over an open one) and closed as
    spent; notes for the step (counts and targets-file account names only, never a label); or why it was skipped."""
    opened: int
    replaced: int
    spent: int
    notes: tuple[str, ...]
    skipped: str | None


def _stamp(now: datetime) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).strftime(_STAMP)


def household_rows(positions: list[dict], cash: dict[str, tuple[str, float]], days: int
                   ) -> tuple[list[dict], dict[str, float], int, int]:
    """The household (Call 9, as ruled): investment accounts only, those with a positions snapshot, kept when their
    newest positions date is within `days` of the newest positions date of any account. A kept account's cash counts
    only when its own cash row is within the same cutoff (else 0). Cash-only accounts (a bank, a card) are not in it.

    Returns the kept accounts' positions, their counted cash by label, how many accounts were left out as old, and how
    many kept accounts' cash rows were too old to count."""
    newest_of: dict[str, str] = {}
    for r in positions:
        if r["as_of_date"] > newest_of.get(r["account"], ""):
            newest_of[r["account"]] = r["as_of_date"]
    if not newest_of:
        return [], {}, 0, 0
    cutoff = (date.fromisoformat(max(newest_of.values())) - timedelta(days=days)).isoformat()
    left_out = {a for a, d in newest_of.items() if d < cutoff}
    kept = [r for r in positions if r["account"] not in left_out]
    kept_cash: dict[str, float] = {}
    stale_cash = 0
    for label in sorted(set(newest_of) - left_out):
        if label not in cash:
            continue
        cash_date, amount = cash[label]
        if cash_date < cutoff:
            stale_cash += 1
        else:
            kept_cash[label] = float(amount or 0.0)
    return kept, kept_cash, len(left_out), stale_cash


def _household_notes(left_out: int, stale_cash: int, days: int) -> list[str]:
    notes = []
    if left_out:
        notes.append(f"{left_out} account{'' if left_out == 1 else 's'} older than {days} days left out")
    if stale_cash:
        what = "account's cash row" if stale_cash == 1 else "accounts' cash rows"
        notes.append(f"{stale_cash} {what} older than {days} days not counted")
    return notes


def _new_proposal(store: Store, cfg: Config, key: str, label: str, cash_date: str, cash: float, targets: Targets,
                  household: Household, positions: list[dict], fund_symbols: frozenset[str],
                  extra_notes: list[dict]) -> AllocationProposal:
    """One account's proposal for all of `cash`: plan_split over its own positions, the household and the bands
    from its entries' scores, with every line's sources (its targets entry, the score rows that decided its band,
    its tilt) and the proposal's (the cash row, the targets file)."""
    alloc = cfg.allocation
    held: dict[str, float] = {}
    for r in positions:
        if r["account"] == label:
            held[r["symbol"]] = held.get(r["symbol"], 0.0) + float(r.get("market_value") or 0.0)
    base = sum(held.values()) + cash
    entries = _account_entries(key, targets)
    entry_of: dict[str, TargetEntry] = {}
    for e in entries:                   # the first entry for a symbol is the one plan_split plans (later: `duplicate`)
        entry_of.setdefault(e.symbol, e)
    symbols = sorted(entry_of)
    rows_of = store.split_scores(symbols)
    bands: dict[str, tuple[str, tuple[str, ...]]] = {}
    deciding: dict[str, tuple[dict, ...]] = {}
    for s in symbols:
        if rows_of.get(s):              # no score row at all: plan_split says `no_scores`
            band, reasons, rows = holding_band(rows_of[s], s in fund_symbols)
            bands[s], deciding[s] = (band, reasons), rows
    plan = plan_split(key, cash, base, held, household, targets, bands, alloc.tilt, alloc.min_line)

    line_sources = []
    for line in plan.lines:
        # The entry's place in the file: the account it is filed under (a pct_of_brokerage entry filed under roth
        # sends money to the brokerage and is cited as roth/<symbol>).
        src = [sources.feed(targets.file_name, targets.last_revised, f"{entry_of[line.symbol].account}/{line.symbol}")]
        src += [sources.derived(f"valuation_scores {r['metric']}", f"{line.symbol} {r['metric']}", r.get("date"))
                for r in deciding.get(line.symbol, ())]
        src.append(sources.config(f"[allocation] tilt.{line.band}", line.tilt))
        line_sources.append(tuple(src))
    notes = (list(plan.notes) + [{"symbol": None, "reason": f"targets: {p}"} for p in targets.problems]
             + extra_notes)
    proposal_sources = (sources.derived("cash_balances", key, cash_date),
                        sources.feed(targets.file_name, targets.last_revised, key))
    placed = sum(line.dollars for line in plan.lines)          # the dollars placed: cash = placed + waits
    return AllocationProposal(key, cash_date, cash, placed, plan.waits, plan.lines, tuple(line_sources),
                              tuple(notes), proposal_sources)


_STAYS_OPEN = "; its open proposal stays open"


def _untyped(asset_type: str | None) -> bool:
    """An asset type the warehouse does not know: missing, empty or `unknown`."""
    return asset_type in (None, "", "unknown")


def run(store: Store, cfg: Config, *, checked_at: str | None, now: datetime) -> SplitReport:
    """Open, replace or close each configured account's proposal as its waiting cash moved (Call 12).

    Called by the engine step after the scores and their re-check, only when the engine's gate is open. Skipped,
    touching nothing, when there is no targets file, it cannot be read, or `[allocation.accounts]` is empty. For each
    account (by targets-file name, sorted), its newest USD cash row: with an open proposal, cash `min_new` or more
    above that proposal's cash makes a new proposal for all of it that replaces it, cash at least $1 below closes it as
    spent (placed, in full or in part), and so does a `buy` of a holding dated on or after its cash_date once a newer
    cash row is in (a placement a deposit hid from the cash; then a new proposal for all the cash opens when it is at
    least `min_new`), anything between changes nothing; with none open, cash `min_new` or more above
    the baseline (0 before any proposal; after a spent one, the lower of its closing cash and the lowest balance since
    that day) opens one. An account named twice on one label, with no entries in the targets file, with no cash row,
    or whose cash row is older than its newest positions (an account with no positions: any row counts) is left alone (an open proposal of it stays open, said in the notes), as is an open proposal whose name is no longer
    configured. An open proposal's lines are never rewritten; the same cash writes nothing. `now` stamps created_at /
    closed_at, `checked_at` is the covering check run's finish. Notes name targets-file account names, never a label."""
    alloc = cfg.allocation
    try:
        targets = read_targets(alloc.targets)
    except ValueError as e:
        return SplitReport(0, 0, 0, (), f"targets file unreadable: {e}")
    if targets is None:
        return SplitReport(0, 0, 0, (), "no targets file")
    if not alloc.accounts:
        return SplitReport(0, 0, 0, (), "no accounts in [allocation.accounts]")

    stamp = _stamp(now)
    positions = store.split_positions()
    cash_rows = store.split_cash()
    kept, kept_cash, n_left_out, n_stale_cash = household_rows(positions, cash_rows, alloc.household_days)
    look_through = read_look_through(alloc.fund_holdings)
    symbols = {r["symbol"] for r in positions} | {e.symbol for e in targets.entries} | set(look_through)
    types = store.split_asset_types(sorted(symbols))
    # A fund: typed one, or a symbol with a look-through file whose type is unknown or missing (the file shows it holds
    # other names). Any other symbol of unknown or missing type is, for caps, a fund without a file (`type_unknown`).
    funds = frozenset(s for s in symbols
                      if types.get(s) in FUND_TYPES or (s in look_through and _untyped(types.get(s))))
    untyped = frozenset(s for s in symbols if _untyped(types.get(s)) and s not in look_through)
    household = household_of(kept, kept_cash, look_through, funds | untyped, untyped)
    kept_labels = {r["account"] for r in kept}
    positions_labels = {r["account"] for r in positions}
    positions_date: dict[str, str] = {}
    for r in positions:
        positions_date[r["account"]] = max(r["as_of_date"], positions_date.get(r["account"], ""))

    hh_notes = _household_notes(n_left_out, n_stale_cash, alloc.household_days)
    run_notes: list[str] = list(hh_notes)
    household_notes = [{"symbol": None, "reason": n} for n in hh_notes]
    open_keys = set(store.open_proposal_keys())
    for key in sorted(open_keys - set(alloc.accounts)):
        run_notes.append(f"{key}: not in [allocation.accounts]{_STAYS_OPEN}")

    # Two targets-file names on one fdc account would each propose the same cash: neither gets a proposal.
    keys_of: dict[str, list[str]] = {}
    for key, label in alloc.accounts.items():
        keys_of.setdefault(label, []).append(key)
    shared = {k for keys in keys_of.values() if len(keys) > 1 for k in keys}
    for keys in sorted(sorted(ks) for ks in keys_of.values() if len(ks) > 1):
        run_notes.append(f"{', '.join(keys)}: one account named more than once in [allocation.accounts]; no proposal")

    opened = replaced = spent = 0
    for key, label in sorted(alloc.accounts.items()):
        stays = _STAYS_OPEN if key in open_keys else ""
        if key in shared:
            if stays:
                run_notes.append(f"{key}: named with another on one account{stays}")
            continue
        if not _account_entries(key, targets):
            run_notes.append(f"no targets for {key}{stays}")
            continue
        if label not in cash_rows:
            run_notes.append(f"{key}: no cash row{stays}")
            continue
        cash_date, amount = cash_rows[label]
        if label in positions_date and cash_date < positions_date[label]:
            # A cash row older than the account's positions may not show what was just placed: it decides nothing.
            run_notes.append(f"{key}: cash row older than its positions")
            continue
        cash = float(amount or 0.0)
        current = store.open_proposal(key)
        if current is not None:
            if cash <= current["cash"] - 1.0 + _CMP_EPS:
                store.close_proposal(current["id"], closed_at=stamp, closed_cash=cash)
                spent += 1
                continue
            if cash_date > current["cash_date"] and store.split_buys_since(label, current["cash_date"]):
                # A buy since the proposal's cash row, seen in a newer row: placed, though a deposit may have hidden it
                # from the cash. Spent; all the cash makes a new proposal from min_new (the baseline is this cash).
                store.close_proposal(current["id"], closed_at=stamp, closed_cash=cash)
                spent += 1
                current = None
                if cash < alloc.min_new - _CMP_EPS:
                    continue
            elif cash < current["cash"] + alloc.min_new - _CMP_EPS:
                continue                                        # under min_new more: the open one stands
        else:
            baseline = 0.0
            last = store.last_spent_proposal(key)
            if last is not None:
                low = store.split_cash_low_since(label, last["closed_at"][:10])
                baseline = float(last["closed_cash"]) if low is None else min(float(last["closed_cash"]), float(low))
            if cash < baseline + alloc.min_new - _CMP_EPS:
                continue
        extra = list(household_notes)
        if label in positions_labels and label not in kept_labels:
            extra.append({"symbol": None, "reason":
                          f"this account is older than {alloc.household_days} days and left out of the household"})
        # Caps are household-wide: every other account's open lines (earlier runs, and earlier in this one) are
        # pending holdings. This account's own open proposal, being replaced, does not count.
        pending = household
        for other in store.open_proposal_keys():
            if other == key:
                continue
            p = store.open_proposal(other)
            counted = alloc.accounts.get(other) in kept_cash
            pending = with_pending(pending, [(l["symbol"], l["dollars"]) for l in p["lines"]], add_to_total=not counted)
        proposal = _new_proposal(store, cfg, key, label, cash_date, cash, targets, pending, positions, funds, extra)
        store.write_proposal(proposal, created_at=stamp, checked_at=checked_at,
                             replaces=current["id"] if current is not None else None)
        if current is not None:
            replaced += 1
        else:
            opened += 1
    return SplitReport(opened, replaced, spent, tuple(run_notes), None)
