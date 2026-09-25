"""Helpers shared by the file adapters. No SQL here."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

_NULL_TOKENS = {"", "--", "-", "n/a", "na", "none", "null"}
_NUM_STRIP = re.compile(r"[$,%+\s]")


def clean_number(text: str | None) -> float | None:
    """'$1,234.56' -> 1234.56; '--', blank, None -> None."""
    if text is None:
        return None
    s = str(text).strip()
    if s.lower() in _NULL_TOKENS:
        return None
    s = _NUM_STRIP.sub("", s)
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dedupe_key(
    account_label: str,
    trade_date: str,
    type_: str,
    symbol: str | None,
    units: float | None,
    amount: float | None,
) -> str:
    def r(v: float | None) -> str:
        return "" if v is None else f"{round(v, 4):.4f}"

    raw = "|".join([account_label, trade_date, type_, symbol or "", r(units), r(amount)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def read_text_lines(path: Path) -> list[str]:
    """Read a CSV export as lines, tolerant of a UTF-8 BOM and CRLF."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def infer_account_type(label: str) -> str:
    u = (label or "").upper()
    if "CRYPTO" in u:
        return "crypto"
    if "ROTH" in u:
        return "roth_ira"
    if "TRADITIONAL" in u or "ROLLOVER" in u or "SEP" in u:
        return "traditional_ira"
    return "brokerage"
