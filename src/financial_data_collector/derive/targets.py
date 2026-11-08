"""The owner's targets file and the fund look-through folder, read for the weekly split.

Both are optional files named in config [allocation]; nothing here writes, fetches or opens a database. The targets
file is read the way the sibling cockpit reads it: what it cannot use is skipped and said in `Targets.problems`,
never guessed.
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

from ..models import Cap, Sleeve, TargetEntry, Targets
from ..sectors import YAHOO_SECTORS
from ..symbols import canonical, exposure_alias

TARGET_BAND = 0.25                          # the band when an entry gives none: target x (1 - 0.25) .. x (1 + 0.25)
RULES = ("trades", "contributions", "hold")
KINDS = ("pct_of_account", "pct_of_brokerage", "direct_passive_ratio")
_NOT_ACCOUNTS = ("sleeves", "caps", "planned")
PLANNED = "planned"                         # the account name the planned entries carry; never a fundable account
FUND_SECTORS_FILE = "fund_category_weights.json"


def _num(v: object) -> float | None:
    """A finite number as a float, else None (a bool is not a number here, nor an integer too large for a float)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        return float(v) if math.isfinite(v) else None
    except OverflowError:
        return None


def _rule(v: object, where: str, problems: list[str]) -> tuple[str, str | None]:
    """(rule, raw_rule): a missing rule is `trades`; one not exactly spelled is `unknown` with its spelling kept."""
    if v is None:
        return "trades", None
    if isinstance(v, str) and v in RULES:
        return v, None
    problems.append(f"{where}: rebalance {v!r} is not one of {', '.join(RULES)}")
    return "unknown", str(v)


def _entry(account: str, raw: object, problems: list[str]) -> TargetEntry | None:
    if not isinstance(raw, dict):
        problems.append(f"{account}: an entry is not an object")
        return None
    ticker = raw.get("ticker")
    if not isinstance(ticker, str) or not canonical(ticker):
        problems.append(f"{account}: an entry has no ticker")
        return None
    symbol = canonical(ticker)
    where = f"{account}/{symbol}"
    target = _num(raw.get("target"))
    if target is None:
        problems.append(f"{where}: no finite target")
        return None
    kind = raw.get("type", "pct_of_account")
    if kind not in KINDS:
        problems.append(f"{where}: type {kind!r} is not one of {', '.join(KINDS)}")
        return None
    low = _num(raw.get("min"))
    high = _num(raw.get("max"))
    low = round(target * (1 - TARGET_BAND), 6) if low is None else low
    high = round(target * (1 + TARGET_BAND), 6) if high is None else high
    if low > high:
        problems.append(f"{where}: inverted band (min above max)")
        return None
    rule, raw_rule = _rule(raw.get("rebalance"), where, problems)
    return TargetEntry(account, symbol, kind, target, low, high, rule, raw_rule)


def _sleeve(raw: object, problems: list[str]) -> Sleeve | None:
    if not isinstance(raw, dict):
        problems.append("sleeves: an entry is not an object")
        return None
    account, label = raw.get("account"), raw.get("label")
    if not isinstance(account, str) or not account or not isinstance(label, str) or not label:
        problems.append("sleeves: an entry has no account or label")
        return None
    members = raw.get("members")
    members = members if isinstance(members, list) else []
    symbols = tuple(canonical(m) for m in members if isinstance(m, str) and canonical(m))
    rule, _ = _rule(raw.get("rebalance"), f"sleeves/{account}/{label}", problems)
    return Sleeve(account, label, symbols, _num(raw.get("min")), _num(raw.get("max")), rule)


def _cap(raw: object, problems: list[str]) -> Cap | None:
    ticker = raw.get("ticker") if isinstance(raw, dict) else None
    if not isinstance(ticker, str) or not canonical(ticker):
        problems.append("caps: an entry has no ticker")
        return None
    symbol = canonical(ticker)
    high = _num(raw.get("max"))
    if high is None:
        problems.append(f"caps/{symbol}: no finite max")
        return None
    return Cap(symbol, high)


def _list(doc: dict, key: str) -> list:
    value = doc.get(key)
    return value if isinstance(value, list) else []


def read_targets(path: Path | None) -> Targets | None:
    """The targets file as `Targets`; None when no path is configured or the file is not there.

    An unreadable file, bad JSON (nested too deeply to read, too) or a top level that is not an object raises
    ValueError with a short message that does not carry the path."""
    if path is None or not Path(path).is_file():
        return None
    path = Path(path)
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError):
        raise ValueError("the targets file cannot be read") from None
    except json.JSONDecodeError as e:
        raise ValueError(f"the targets file is not valid JSON (line {e.lineno})") from None
    except RecursionError:
        raise ValueError("the targets file is nested too deeply to read") from None
    if not isinstance(doc, dict):
        raise ValueError("the targets file must hold one JSON object at the top level")
    problems: list[str] = []
    entries: list[TargetEntry] = []
    for key, value in doc.items():
        if key.startswith("_") or key in _NOT_ACCOUNTS or not isinstance(value, list):
            continue
        for raw in value:
            entry = _entry(key, raw, problems)
            if entry is not None:
                entries.append(entry)
    sleeves = [s for s in (_sleeve(r, problems) for r in _list(doc, "sleeves")) if s is not None]
    caps = [c for c in (_cap(r, problems) for r in _list(doc, "caps")) if c is not None]
    revised = doc.get("last_revised")
    return Targets(tuple(entries), tuple(sleeves), tuple(caps), revised if isinstance(revised, str) else None,
                   path.name, tuple(problems), tuple(_planned(doc)))


def _planned(doc: dict) -> list[TargetEntry]:
    """The `planned` entries that carry a structured percentage, as entries of the account `planned`.

    They are the names the owner intends to fund and has not: the plan's own lines need them (seven live brokerage
    rows sum to 59%, the thirteen names to 100%), while the weekly split must never see them - which is why they
    come back in their own list and `planned` stays in `_NOT_ACCOUNTS`. An entry that is only prose is skipped
    without a problem: `planned` carried reader's notes long before it carried percentages."""
    out: list[TargetEntry] = []
    ignored: list[str] = []
    for raw in _list(doc, PLANNED):
        if not isinstance(raw, dict) or not isinstance(raw.get("ticker"), str) or _num(raw.get("target")) is None:
            continue
        entry = _entry(PLANNED, raw, ignored)
        if entry is not None:
            out.append(entry)
    return out


def read_look_through(fund_dir: Path | None) -> dict[str, dict[str, float]]:
    """Fund symbol -> {held symbol: weight as a fraction}, from each `<FUND>.csv` (columns ticker, name, weight_pct).

    A row with a weight that is not a finite number is dropped; an empty file is left out; files starting with `_`
    and files that are not CSV are ignored; a missing folder gives {}. Constituents are joined by
    `symbols.exposure_alias`, so a fund's two Alphabet rows are one GOOGL exposure and a foreign listing is the US
    line it is exposure to; two rows that become one symbol ADD."""
    return read_look_through_with_coverage(fund_dir)[0]


def read_look_through_with_coverage(fund_dir: Path | None) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
    """read_look_through's weights, and each fund's measured coverage: the percent of the fund its file accounts for.

    Coverage is the SUM OF THE WEIGHTS AS READ (before aliasing, which cannot change a sum), in percent - never an
    assumption of 100. A top-ten cache measures around 8-47; a complete file around 100 but not exactly; a
    double-counted file over 100, and that is reported as measured, not clamped, because a consumer comparing a
    look-through figure against a plan line needs to know which of the three it has."""
    if fund_dir is None or not Path(fund_dir).is_dir():
        return {}, {}
    out: dict[str, dict[str, float]] = {}
    coverage: dict[str, float] = {}
    for p in sorted(Path(fund_dir).iterdir()):
        if not p.is_file() or p.suffix.lower() != ".csv" or p.name.startswith("_"):
            continue
        weights: dict[str, float] = {}
        total = 0.0
        try:
            with p.open(encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    symbol = exposure_alias(row.get("ticker") or "")
                    try:
                        pct = float(row.get("weight_pct") or "")
                    except ValueError:
                        continue
                    if symbol and math.isfinite(pct):
                        weights[symbol] = weights.get(symbol, 0.0) + pct / 100
                        total += pct
        except (OSError, UnicodeDecodeError, csv.Error):
            continue
        if weights:
            fund = canonical(p.stem)
            out[fund] = weights
            coverage[fund] = total
    return out, coverage


def read_fund_sectors(fund_dir: Path | None) -> dict[str, dict[str, float]]:
    """Fund symbol -> {sector: percent}, from the look-through folder's `fund_category_weights.json`.

    A fund's sector map is COMPLETE where its top-ten holdings cache is not - the publisher's own weights for the
    whole fund, summing to about 100 - which is why the sector figure can be measured against a plan line while the
    constituent figures are floors. The file speaks Yahoo's sector names; they are mapped to this repo's eleven
    (`sectors.YAHOO_SECTORS`) and a name outside that taxonomy is dropped rather than becoming a twelfth sector
    that no plan line could ever match. A fund with no usable map is left out; a missing file gives {}."""
    if fund_dir is None:
        return {}
    path = Path(fund_dir) / FUND_SECTORS_FILE
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return {}
    funds = doc.get("funds") if isinstance(doc, dict) else None
    if not isinstance(funds, dict):
        return {}
    out: dict[str, dict[str, float]] = {}
    for raw_fund, entry in funds.items():
        if not isinstance(raw_fund, str) or not isinstance(entry, dict):
            continue
        weights = entry.get("sector")
        if not isinstance(weights, dict):
            continue
        got: dict[str, float] = {}
        for raw_sector, pct in weights.items():
            sector = YAHOO_SECTORS.get(raw_sector) if isinstance(raw_sector, str) else None
            value = _num(pct)
            if sector and value is not None and value > 0:
                got[sector] = got.get(sector, 0.0) + value
        if got:
            out[canonical(raw_fund)] = got
    return out
