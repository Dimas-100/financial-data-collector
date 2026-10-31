"""The check on the weekly split: `split`.

It reads the open raw proposals (what the split wrote, not the views that apply holds) and holds back one that breaks
a rule the split itself is meant to keep: lines that add up to more than the cash, a line that is not a whole number
of dollars (at least one), a line to a name the targets file marks `hold` in that account, a line to a name that
account has no target for, a line with no sources, a cash that is not the account's own USD cash row on the
proposal's cash_date (half a cent apart; the account named through config `[allocation.accounts]`, read by
`Store.check_split_cash`; no row or no mapping is a warn). The targets file is read here with this file's own code
(the shape is documented in the README); nothing is imported from `derive/`. A finding's field is `proposal:<id>`,
which the `allocation_proposals` / `allocation_lines` views hold on.

One proposal is one finding: every fault it has is in its message, a hold if any is a hold. A proposal that is no
longer open gets no finding, so its old one clears. It never raises on a bad row: a row it cannot read is a finding.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from ..symbols import canonical
from . import CHECKS, CheckContext, Finding

_NOT_ACCOUNTS = ("sleeves", "caps", "planned")
_KINDS = ("pct_of_account", "pct_of_brokerage", "direct_passive_ratio")
_RULES = ("trades", "contributions", "hold")      # a missing `rebalance` is `trades`; any other spelling gets no money
_BROKERAGE = "brokerage"
_HALF_CENT = 0.005


def _finite(x: object) -> bool:
    """A finite number (not a bool, not an integer too large for a float)."""
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return False
    try:
        return math.isfinite(x)
    except OverflowError:
        return False


def _read_targets(path: Path | None) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, set[str]]] | None:
    """(known, hold, unknown-rule symbols) per account name, or None when the file is not configured, missing or
    unreadable (JSON nested too deeply to read, too; an overflow while reading it is caught the same way).

    Known: an entry with a valid type and a finite target. A `pct_of_brokerage` entry is known under `brokerage`
    only (it measures, and takes money from, that account), wherever it is filed; any other entry under the account
    it is filed in. Hold: any entry whose `rebalance` is exactly "hold", under the account it is filed in and also
    under `brokerage` when it is a `pct_of_brokerage` entry (a hold name is held back even when its entry would not
    be used). Unknown rule: a `rebalance` that is present but not trades, contributions or hold (the split gives
    such a name nothing), placed like a hold name."""
    if path is None or not Path(path).is_file():
        return None
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(doc, dict):
        return None
    try:
        return _names(doc)
    except (OverflowError, RecursionError):
        return None


def _names(doc: dict) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, set[str]]]:
    """_read_targets' sets from the parsed file; a number too large for a float is no number (its entry not known)."""
    known: dict[str, set[str]] = {}
    held: dict[str, set[str]] = {}
    odd: dict[str, set[str]] = {}
    for key, value in doc.items():
        if not isinstance(key, str) or key.startswith("_") or key in _NOT_ACCOUNTS or not isinstance(value, list):
            continue
        for raw in value:
            if not isinstance(raw, dict) or not isinstance(raw.get("ticker"), str):
                continue
            symbol = canonical(raw["ticker"])
            if not symbol:
                continue
            kind = raw.get("type", "pct_of_account")
            usable = kind in _KINDS and _finite(raw.get("target"))
            if usable:
                known.setdefault(_BROKERAGE if kind == "pct_of_brokerage" else key, set()).add(symbol)
            rule = raw.get("rebalance")
            if rule == "hold" or (rule is not None and rule not in _RULES):
                homes = [key] + ([_BROKERAGE] if kind == "pct_of_brokerage" and key != _BROKERAGE else [])
                for home in homes:
                    (held if rule == "hold" else odd).setdefault(home, set()).add(symbol)
                    known.setdefault(home, set()).add(symbol)      # on file: one fault, not two
    return known, held, odd


def _source_problem(sources: object) -> bool:
    """True when the sources are not a non-empty JSON list of objects that each have a kind and a label."""
    if not isinstance(sources, list) or not sources:
        return True
    return any(not isinstance(s, dict) or not isinstance(s.get("kind"), str) or not s.get("kind")
               or not isinstance(s.get("label"), str) or not s.get("label") for s in sources)


def split(ctx: CheckContext) -> list[Finding]:
    """One finding per open proposal that has a fault; a hold unless the only fault is that the targets file could
    not be read to check the hold names (a warn)."""
    proposals = ctx.store.check_split_rows()
    if not proposals:
        return []
    names = _read_targets(ctx.cfg.allocation.targets)
    accounts = dict(ctx.cfg.allocation.accounts)
    out: list[Finding] = []
    for p in proposals:
        holds: list[str] = []
        warns: list[str] = []
        evidence: dict = {}
        lines = p.get("lines") or []
        total = 0.0
        bad_amount: list[str] = []
        for line in lines:
            d = line.get("dollars")
            if _finite(d):
                total += d
            if not (_finite(d) and d == int(d) and d >= 1):
                bad_amount.append(str(line.get("symbol")))
        cash = p.get("cash")
        if not _finite(cash):
            holds.append("the cash is unreadable (not a number)")
            evidence["cash"] = str(cash)
        elif total > cash + _HALF_CENT:
            holds.append(f"lines add up to {total:g}, more than the cash {cash:g}")
            evidence.update(cash=cash, lines_total=total)
        if _finite(cash):
            # The cash the proposal was made from must be the account's own cash row of that day: the split's main
            # money rule, checked against the warehouse rather than the split's own reading of it.
            key = p.get("account_key")
            label = accounts.get(key) if isinstance(key, str) else None
            if label is None:
                warns.append("cannot check the proposal's cash: its account is not in [allocation.accounts]")
            else:
                have = ctx.store.check_split_cash(label, str(p.get("cash_date")))
                if have is None or not _finite(have):
                    warns.append("cannot check the proposal's cash: no cash row for its account on "
                                 f"{p.get('cash_date')}")
                elif abs(have - cash) > _HALF_CENT:
                    holds.append(f"the proposal's cash ${cash:,.2f} is not the account's ${have:,.2f} on "
                                 f"{p.get('cash_date')}")
                    evidence.update(cash=cash, account_cash=have)
        if bad_amount:
            holds.append(f"a line is not a whole number of dollars (at least 1): {', '.join(bad_amount)}")
            evidence["fractional_lines"] = bad_amount
        unsourced = [str(line.get("symbol")) for line in lines if _source_problem(line.get("sources"))]
        if unsourced:
            holds.append(f"a line has no sources, or sources that are not records with a kind and a label: "
                         f"{', '.join(unsourced)}")
            evidence["unsourced_lines"] = unsourced
        if names is None:
            warns.append("cannot check hold names: the targets file is missing or unreadable")
        else:
            known, held, odd = names
            key = p.get("account_key")
            on_hold = sorted({str(l.get("symbol")) for l in lines if l.get("symbol") in held.get(key, ())})
            odd_rule = sorted({str(l.get("symbol")) for l in lines if l.get("symbol") in odd.get(key, ())})
            no_target = sorted({str(l.get("symbol")) for l in lines if l.get("symbol") not in known.get(key, ())})
            if on_hold:
                holds.append(f"a line to a name the targets file marks hold: {', '.join(on_hold)}")
                evidence["hold_names"] = on_hold
            if odd_rule:
                holds.append(f"a line to a name whose rebalance rule is not trades, contributions or hold: "
                             f"{', '.join(odd_rule)}")
                evidence["unknown_rule"] = odd_rule
            if no_target:
                holds.append(f"a line to a name with no target in this account: {', '.join(no_target)}")
                evidence["no_target"] = no_target
        if not holds and not warns:
            continue
        severity = "hold" if holds else "warn"
        out.append(Finding("split", str(p["account_key"]), f"proposal:{p['id']}", str(p["cash_date"]), None, severity,
                           "; ".join(holds + warns), evidence))
    return out


CHECKS["split"] = split
