"""Canonical symbol handling.

Canonical form: uppercase, share classes joined with a dot (BRK.B). Adapters
call canonical() on everything they read; providers get their own spelling via
to_tiingo / to_yfinance / to_sec.
"""
from __future__ import annotations

# Fidelity core / money-market positions. These are cash, not holdings.
MONEY_MARKET: frozenset[str] = frozenset(
    {"SPAXX", "FDRXX", "FZFXX", "FCASH", "FDLXX", "SPRXX", "FZDXX", "FZCXX", "CORE"}
)

# Symbols whose provider spelling has no separator at all (SnapTrade style).
_NO_SEPARATOR_ALIASES: dict[str, str] = {
    "BRKB": "BRK.B",
    "BRKA": "BRK.A",
    "BFB": "BF.B",
    "BFA": "BF.A",
}


def canonical(raw: str) -> str:
    """Normalise any provider spelling to the canonical form."""
    sym = (raw or "").strip().upper().rstrip("*")
    if not sym:
        return sym
    sym = sym.replace("/", ".").replace("-", ".")
    return _NO_SEPARATOR_ALIASES.get(sym, sym)


def _dashed(sym: str) -> str:
    return canonical(sym).replace(".", "-")


def to_tiingo(sym: str) -> str:
    return _dashed(sym)


def to_yfinance(sym: str) -> str:
    return _dashed(sym)


def to_sec(sym: str) -> str:
    return _dashed(sym)


def is_money_market(symbol: str, description: str | None = None) -> bool:
    if canonical(symbol) in MONEY_MARKET:
        return True
    return bool(description) and "MONEY MARKET" in description.upper()
