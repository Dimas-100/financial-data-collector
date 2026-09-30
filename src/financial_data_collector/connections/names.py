"""What an account that arrives through a connection is called, what kind it is, and how it is recognised again.

No SQL and no network. Every rule here is the one in the connections spec, section 8.
"""
from __future__ import annotations

import hashlib
import re

KINDS = ("brokerage", "roth_ira", "traditional_ira", "ira", "rollover_ira", "sep_ira", "simple_ira", "401k", "403b",
         "457b", "hsa", "pension", "crypto", "checking", "savings", "cash", "money_market", "credit_card", "loan",
         "mortgage", "line_of_credit", "other")
DEBT_KINDS = frozenset({"credit_card", "loan", "mortgage", "line_of_credit"})
MAX_LABEL = 80

# A run of three or more digits is taken for part of an account number, with the mask in front of it or the
# brackets around it. A run that is part of a word (401k) or names a plan (401(k), 403(b), 457(b), 529) stays.
_KEEP = r"(?:401|403|457|529)"
_MASK = r"(?:[.*#•…\-]+|x+)"
_ENDING = re.compile(rf"(?<![a-z0-9])ending(?:\s+in)?\s*{_MASK}?\s*\d{{3,}}(?![a-z0-9])", re.I)
_BRACKETED = re.compile(rf"[\(\[]\s*{_MASK}?\s*\d{{3,}}\s*[\)\]]", re.I)
_MASKED = re.compile(rf"(?<![a-z0-9]){_MASK}\s*\d{{3,}}(?![a-z0-9])", re.I)
_BARE = re.compile(rf"(?<![a-z0-9])(?!{_KEEP}(?!\d))\d{{3,}}(?![a-z0-9])", re.I)
_EDGES = " \t-–—.,:;#*•…/|("

# Top to bottom, first match wins; a word matches where a word starts, in any case. Checking and savings come
# before the card words, so "Debit Card Checking" is a checking account.
_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("mortgage",), "mortgage"),
    (("line of credit", "heloc"), "line_of_credit"),
    (("loan",), "loan"),
    (("checking",), "checking"),
    (("saving",), "savings"),
    (("money market",), "money_market"),
    (("credit", "card", "visa", "mastercard", "amex", "discover"), "credit_card"),
    (("crypto",), "crypto"),
    (("roth",), "roth_ira"),
    (("ira", "rollover"), "traditional_ira"),
    (("401",), "401k"),
    (("brokerage", "invest", "individual", "joint"), "brokerage"),
)


def clean_name(name: str | None) -> str:
    """The account's name without anything that looks like part of its number; "Account" when nothing is left."""
    text = str(name or "")
    for pattern in (_ENDING, _BRACKETED, _MASKED, _BARE):
        text = pattern.sub(" ", text)
    text = " ".join(text.split()).strip(_EDGES)
    return text or "Account"


def build_label(institution: str | None, name: str | None) -> str:
    """'Example Bank Checking': the institution, then the cleaned name, unless the name already starts with it."""
    inst = " ".join(str(institution or "").split())
    clean = clean_name(name)
    label = clean if not inst or clean.lower().startswith(inst.lower()) else f"{inst} {clean}"
    return label[:MAX_LABEL].rstrip(_EDGES) or "Account"


def institution_code(institution: str | None) -> str:
    """'Example Bank' -> 'example_bank': the form the accounts table already uses ('fidelity')."""
    code = re.sub(r"[^a-z0-9]+", "_", str(institution or "").lower()).strip("_")
    return code or "unknown"


def match_kind(text: str | None) -> str | None:
    """The kind the rules give for a name or a service's own kind text; None when no rule matches."""
    low = " ".join(str(text or "").lower().split())
    for words, kind in _RULES:
        for word in words:
            if re.search(rf"(?<![a-z0-9]){re.escape(word)}", low):
                return kind
    return None


def slug_for(kind: str) -> str:
    """The owner's cockpit groups: roth, brokerage, other."""
    return "roth" if kind == "roth_ira" else "brokerage" if kind == "brokerage" else "other"


def external_key(origin: str, service_id: str) -> str:
    """How the same account is recognised on the next sync. The service's own id is hashed, never stored."""
    return hashlib.sha256(f"{origin}:{service_id}".encode("utf-8")).hexdigest()
