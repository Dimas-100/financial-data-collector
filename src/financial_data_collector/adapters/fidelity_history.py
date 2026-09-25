"""Fidelity "Accounts -> Activity & Orders -> Download" history CSV -> transactions.

Per-account downloads have no Account column, so the label comes from the
--account flag, else from the filename (History_<Label>_<anything>.csv), else
the file is refused with AccountUnknown.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from ..models import AccountRef, TransactionRow
from ..symbols import canonical
from .base import clean_number, infer_account_type, read_text_lines

HEADER_PREFIX = "Run Date,Action,Symbol,Description,Type"
SOURCE = "fidelity_csv"

_ACTION_TYPES: tuple[tuple[str, str], ...] = (
    ("YOU BOUGHT", "buy"),
    ("YOU SOLD", "sell"),
    ("DIVIDEND RECEIVED", "dividend"),
    ("REINVESTMENT", "reinvest"),
    ("ELECTRONIC FUNDS TRANSFER RECEIVED", "contribution"),
    ("CONTRIBUTION", "contribution"),
    ("DIRECT DEPOSIT", "contribution"),
    ("ELECTRONIC FUNDS TRANSFER PAID", "withdrawal"),
    ("DISTRIBUTION", "withdrawal"),
    ("INTEREST EARNED", "interest"),
    ("ADVISOR FEE", "fee"),
    ("FEE", "fee"),
    ("TRANSFERRED", "transfer"),
)
_FILENAME_ACCOUNT = re.compile(r"^History_([^_]+)_")
_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
# Fidelity account numbers look like one letter + 8-9 digits and can appear in transfer actions.
_ACCOUNT_NUMBER = re.compile(r"\b[A-Z]\d{8,9}\b")


class AccountUnknown(ValueError):
    """Raised when a history file carries no account label and none was given."""


def _is_header(line: str) -> bool:
    s = line.lstrip("﻿").strip()
    return s.startswith(HEADER_PREFIX) or s.startswith("Account," + HEADER_PREFIX)


def detect(lines: list[str]) -> bool:
    return any(_is_header(l) for l in lines[:10])


def classify_action(action: str, units: float | None = None) -> str:
    u = (action or "").strip().upper()
    if u.startswith("EXCHANGE"):  # fund exchange: shares in (buy) or out (sell) of this fund
        if units:
            return "buy" if units > 0 else "sell"
        return "other"
    for prefix, kind in _ACTION_TYPES:
        if u.startswith(prefix):
            return kind
    return "other"


def scrub_account_numbers(text: str) -> str:
    return _ACCOUNT_NUMBER.sub("<acct>", text or "")


def account_from_filename(path: Path) -> str | None:
    m = _FILENAME_ACCOUNT.match(path.name)
    return m.group(1).strip() if m else None


def _iso(text: str | None) -> str | None:
    s = (text or "").strip()
    m = _DATE.match(s)
    if not m:
        return None
    mo, d, y = m.groups()
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


def _account(label: str) -> AccountRef:
    kind = infer_account_type(label)
    slug = {"roth_ira": "roth", "brokerage": "brokerage"}.get(kind, "other")
    return AccountRef(label=label, slug=slug, institution="fidelity", account_type=kind)


def parse(path: Path, account: str | None = None) -> list[TransactionRow]:
    lines = read_text_lines(path)
    if not detect(lines):
        raise ValueError(f"{path.name}: not a Fidelity history export")
    start = next(i for i, l in enumerate(lines) if _is_header(l))
    body: list[str] = []
    for line in lines[start:]:
        if not line.strip():
            break
        body.append(line.lstrip("﻿"))
    reader = csv.DictReader(body, skipinitialspace=True)
    has_account_col = "Account" in (reader.fieldnames or [])
    fallback = account or account_from_filename(path)
    if not has_account_col and not fallback:
        raise AccountUnknown(
            f"{path.name}: no Account column and no label; re-run: fdc import {path.name} --account \"<label>\""
        )
    rows: list[TransactionRow] = []
    for row in reader:
        date = _iso(row.get("Run Date"))
        if not date:
            continue  # disclaimer / footer lines never reach here, but be safe
        label = (row.get("Account") or "").strip() if has_account_col else ""
        label = label or fallback or ""
        if not label:
            raise AccountUnknown(f"{path.name}: row dated {date} has an empty Account cell")
        action = (row.get("Action") or "").strip()
        symbol_raw = (row.get("Symbol") or "").strip()
        commission = clean_number(row.get("Commission ($)"))
        fees = clean_number(row.get("Fees ($)"))
        fee = None if commission is None and fees is None else (commission or 0.0) + (fees or 0.0)
        units = clean_number(row.get("Quantity")) or None  # Fidelity writes 0 on cash-only rows; store NULL like SnapTrade
        rows.append(TransactionRow(
            account=_account(label),
            trade_date=date,
            type=classify_action(action, units),
            symbol=canonical(symbol_raw) or None,
            units=units,
            price=clean_number(row.get("Price ($)")),
            amount=clean_number(row.get("Amount ($)")),
            fee=fee,
            description=scrub_account_numbers(action),
            source=SOURCE,
            settlement_date=_iso(row.get("Settlement Date")),
        ))
    return rows
