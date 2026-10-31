"""The weekly split: how cash waiting in an account is shared across holdings.

`holding_band`: one band (`cheap`, `usual` or `premium`) for a holding, read from its primary valuation scores and
leaning careful; the rows are the symbol's `valuation_scores` view rows as dicts. `household_of` and `plan_split`:
the household's holdings with fund look-through, and the split of one account's waiting cash in whole dollars by
target gap and band, inside every band top, sleeve max and cap. Pure: no SQL, no store, no files.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..models import SplitLine, TargetEntry, Targets

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
    two quality rows when they changed or qualified the result.
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
        reasons.append("premium_by:" + ",".join(premium))
        return "premium", tuple(reasons), deciding
    if not premium and 2 * len(cheap) > n:
        if is_fund:
            reasons.append("cheap_by:" + ",".join(cheap))
            return "cheap", tuple(reasons), deciding
        quality = [by_metric.get(m) for m in QUALITY]
        quality_rows = tuple(q for q in quality if q is not None)
        if all(_known(q) for q in quality) and all(q["value"] < 0 for q in quality):
            reasons.append("quality_falling")
            return "usual", tuple(reasons), deciding + quality_rows
        reasons.append("cheap_by:" + ",".join(cheap))
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
    dollars through funds with a look-through file, the look-through itself and which symbols are funds."""
    total: float
    direct: dict[str, float]
    indirect: dict[str, float]
    look_through: dict[str, dict[str, float]]
    funds: frozenset[str]


@dataclass(frozen=True)
class SplitPlan:
    """The split of one account's waiting cash: its lines (dollars descending, then symbol), what waits for next
    week, and a note `{"symbol", "reason"}` for every entry of the account that ended with no line."""
    lines: tuple[SplitLine, ...]
    waits: float
    notes: tuple[dict, ...]


def household_of(positions: list[dict], cash: dict[str, float], look_through: dict[str, dict[str, float]],
                 fund_symbols: frozenset[str]) -> Household:
    """The household from the counted accounts' positions (`account`, `symbol`, `market_value`) and cash.

    A fund with a look-through file adds value x weight to each name it holds (`indirect`); the rest of the fund adds
    to no name. A fund without a file, and anything that is not a fund, counts as a direct holding of itself."""
    direct: dict[str, float] = {}
    indirect: dict[str, float] = {}
    total = sum(float(v or 0.0) for v in cash.values())
    for row in positions:
        symbol, value = row["symbol"], float(row.get("market_value") or 0.0)
        total += value
        if symbol in fund_symbols and symbol in look_through:
            for name, weight in look_through[symbol].items():
                indirect[name] = indirect.get(name, 0.0) + value * weight
        else:
            direct[symbol] = direct.get(symbol, 0.0) + value
    return Household(total, direct, indirect, dict(look_through), frozenset(fund_symbols))


def _account_entries(account: str, targets: Targets) -> list[TargetEntry]:
    """The entries that send money to `account`: its own (not `pct_of_brokerage`), and every `pct_of_brokerage`
    entry wherever it is filed when the account is the brokerage."""
    return [e for e in targets.entries
            if (e.kind == "pct_of_brokerage" and account == "brokerage")
            or (e.kind != "pct_of_brokerage" and e.account == account)]


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
    code: str                  # sleeve_cut:<label> | cap_cut:<symbol>
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
        groups.append(_Group(f"sleeve_cut:{sleeve.label}", max(room, 0.0), coef))
    for cap in targets.caps:
        name = cap.symbol
        coef = {}
        for s in sorted(lines):
            if s == name:
                c = 1.0
            elif s in household.funds and s in household.look_through:
                c = float(household.look_through[s].get(name, 0.0))
            elif s in household.funds:
                c = max((float(w.get(name, 0.0)) for w in household.look_through.values()), default=0.0)
                if c > 0 and "look_through_assumed" not in lines[s].reasons:
                    lines[s].reasons.append("look_through_assumed")
            else:
                c = 0.0
            if c > 0:
                coef[s] = c
        if not coef:
            continue
        room = cap.high / 100 * household.total - (household.direct.get(name, 0.0) + household.indirect.get(name, 0.0))
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


def plan_split(account: str, cash: float, base: float, held: dict[str, float], household: Household,
               targets: Targets, bands: dict[str, tuple[str, tuple[str, ...]]], tilt: dict[str, float],
               min_line: int) -> SplitPlan:
    """Split `cash` waiting in `account` across its target entries, in whole dollars.

    account: the targets file's account name (`brokerage` also takes every `pct_of_brokerage` entry). base: the
    account's positions plus its cash. held: its positions by symbol. bands: {symbol: (band, reasons)}; a symbol
    without one is `usual` (`no_scores`). tilt: the factor per band. A percent entry's gap is target% x base - held
    and its top max% x base - held; a ratio entry's gap is target x via-funds - direct (household-wide) and its top
    max x via-funds - direct. Money goes by gap x tilt, never past a top, a sleeve's max (its members in this account)
    or a cap (the name household-wide, funds through their look-through). Whole dollars (floored); a line under
    `min_line` is folded into the largest line as far as that line's room allows; the rest waits. Never more than
    the cash, never a `hold` or unknown-rule entry.
    """
    cash_in = max(float(cash), 0.0)
    entries = _account_entries(account, targets)
    reason_of: dict[int, str] = {}
    seen: set[str] = set()
    lines: dict[str, _Line] = {}
    for i, e in enumerate(entries):
        s = e.symbol
        if s in seen:
            reason_of[i] = "duplicate"
            continue
        seen.add(s)
        if e.rule == "hold":
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
    # Never more than the cash. Floors of shares that sum to at most the cash cannot pass it; this only guards.
    over = sum(dollars[s] for s in kept) - int(math.floor(cash_in + 1e-9))
    for s in sorted(kept, key=lambda s: (dollars[s], s)):
        if over <= 0:
            break
        cut = min(over, dollars[s])
        dollars[s] -= cut
        over -= cut
    kept = [s for s in kept if dollars[s] > 0]

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
            # A group or its top left it at nothing: that is why; else its share was under the minimum.
            why = stops[s][0] if floors[s] == 0 and stops[s] else "under_min"
            notes.append({"symbol": s, "reason": why})
    waits = round(cash_in - sum(line.dollars for line in out), 2)
    return SplitPlan(tuple(out), waits, tuple(notes))
