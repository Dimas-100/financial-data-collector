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


# EXPOSURE ONLY, and never reached from canonical(). Identity and exposure are different questions: canonical()
# governs what a symbol IS for every adapter (positions, prices, securities), so mapping GOOG onto GOOGL there would
# rewrite a real GOOG holding into GOOGL in the warehouse itself. These pairs answer the other question - which US
# line a fund constituent is exposure to - and belong only in the look-through path (derive/targets.py).
# Each was verified against the constituent's own name column in investing/fund_holdings/*.csv:
#   GOOG     Alphabet Inc Class C                        0700.HK  Tencent Holdings Ltd
#   2330.TW  Taiwan Semiconductor Manufacturing Co Ltd   ASML.AS  ASML Holding NV
#   AZN.L    AstraZeneca PLC                             HSBA.L   HSBC Holdings PLC
#   NOVN.SW  Novartis AG                                 SHEL.L   Shell PLC
#   ROP.SW   Roche Holding AG - its ADR is RHHBY, NOT ROP: ROP on the NYSE is Roper Technologies, a company this
#            portfolio does not own, so a suffix-stripping rule would have merged in the wrong name.
# 005930.KQ (Samsung Electronics) and 000660.KQ (SK hynix) stay unmapped on purpose: neither has a US line this
# portfolio could hold, so there is nothing to join them to.
_EXPOSURE_ALIASES: dict[str, str] = {
    "GOOG": "GOOGL",
    "2330.TW": "TSM",
    "ASML.AS": "ASML",
    "AZN.L": "AZN",
    "HSBA.L": "HSBC",
    "NOVN.SW": "NVS",
    "ROP.SW": "RHHBY",
    "SHEL.L": "SHEL",
    "0700.HK": "TCEHY",
}


def canonical(raw: str) -> str:
    """Normalise any provider spelling to the canonical form."""
    sym = (raw or "").strip().upper().rstrip("*")
    if not sym:
        return sym
    sym = sym.replace("/", ".").replace("-", ".")
    return _NO_SEPARATOR_ALIASES.get(sym, sym)


def exposure_alias(raw: str) -> str:
    """The US line a fund constituent is exposure to: canonical form first, then _EXPOSURE_ALIASES.

    For the look-through path ONLY (see the map's comment). A symbol with no entry comes back in canonical form,
    unchanged, so an unmapped foreign listing stays itself rather than being guessed at."""
    sym = canonical(raw)
    return _EXPOSURE_ALIASES.get(sym, sym)


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
