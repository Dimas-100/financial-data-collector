"""Fidelity "Portfolio Positions" CSV export -> Snapshot.

Fidelity.com -> Accounts -> Positions -> Download. The file has a BOM, a header
row, one row per position (plus a "Pending Activity" row per account), then a
blank line and a quoted disclaimer block ending in a "Date downloaded" line.
"""
from __future__ import annotations

import csv
import re
from datetime import datetime, timezone
from pathlib import Path

from ..models import AccountRef, CashRow, PositionRow, Snapshot
from ..symbols import canonical, is_money_market
from .base import clean_number, infer_account_type, read_text_lines

HEADER_PREFIX = "Account Number,Account Name,Symbol,Description,Quantity,Last Price"
SOURCE = "fidelity_csv"

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_FILENAME_DATE = re.compile(r"Portfolio_Positions_([A-Za-z]{3})-(\d{1,2})-(\d{4})")
_FOOTER_DATE = re.compile(r"Date downloaded\s+([A-Za-z]{3})-(\d{1,2})-(\d{4})")


def detect(lines: list[str]) -> bool:
    for line in lines:
        if line.strip():
            return line.lstrip("﻿").startswith(HEADER_PREFIX)
    return False


def _iso(mon: str, day: str, year: str) -> str | None:
    m = _MONTHS.get(mon.lower())
    if not m:
        return None
    return f"{int(year):04d}-{m:02d}-{int(day):02d}"


def snapshot_date(path: Path, lines: list[str]) -> tuple[str, str | None]:
    """Date the snapshot from the filename, else the footer, else file mtime."""
    m = _FILENAME_DATE.search(path.name)
    if m:
        d = _iso(*m.groups())
        if d:
            return d, None
    for line in reversed(lines):
        m = _FOOTER_DATE.search(line)
        if m:
            d = _iso(*m.groups())
            if d:
                return d, None
    mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d")
    return mtime, f"{path.name}: no date in filename or footer; used file mtime {mtime}"


def _account(label: str) -> AccountRef:
    kind = infer_account_type(label)
    slug = {"roth_ira": "roth", "brokerage": "brokerage"}.get(kind, "other")
    return AccountRef(label=label, slug=slug, institution="fidelity", account_type=kind)


def parse(path: Path) -> Snapshot:
    lines = read_text_lines(path)
    if not detect(lines):
        raise ValueError(f"{path.name}: not a Fidelity positions export")
    start = next(i for i, l in enumerate(lines) if l.lstrip("﻿").startswith(HEADER_PREFIX))
    body: list[str] = []
    for line in lines[start:]:
        if not line.strip():
            break
        body.append(line)
    as_of, warning = snapshot_date(path, lines)
    warnings = [warning] if warning else []
    positions: list[PositionRow] = []
    cash: dict[tuple[str, str], tuple[AccountRef, float]] = {}
    for row in csv.DictReader(body):
        raw_symbol = (row.get("Symbol") or "").strip()
        if not raw_symbol or raw_symbol.lower() == "pending activity":
            continue
        label = (row.get("Account Name") or "").strip()
        if not label:
            warnings.append(f"row for {raw_symbol} has no account name; skipped")
            continue
        acct = _account(label)
        desc = (row.get("Description") or "").strip() or None
        value = clean_number(row.get("Current Value"))
        if is_money_market(raw_symbol, desc):
            key = (label, "USD")
            prev = cash.get(key, (acct, 0.0))[1]
            cash[key] = (acct, prev + (value or 0.0))
            continue
        qty = clean_number(row.get("Quantity"))
        if qty is None:
            warnings.append(f"{raw_symbol} in {label}: no quantity; skipped")
            continue
        positions.append(PositionRow(
            account=acct,
            symbol=canonical(raw_symbol),
            description=desc,
            quantity=qty,
            price=clean_number(row.get("Last Price")),
            market_value=value,
            cost_basis_total=clean_number(row.get("Cost Basis Total")),
            avg_cost=clean_number(row.get("Average Cost Basis")),
            unrealized_pnl=clean_number(row.get("Total Gain/Loss Dollar")),
        ))
    cash_rows = [CashRow(acct, amount) for (acct, amount) in cash.values()]
    return Snapshot(as_of_date=as_of, source=SOURCE, positions=positions, cash=cash_rows,
                    fetched_at=None, warnings=warnings)
