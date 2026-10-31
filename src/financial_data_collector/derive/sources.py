"""Builders for the `sources` lists carried by every engine row (scores and price moves).

Pure: no SQL, no store. A source is a plain dict `{kind, label, date, ref}` plus `url` (a filing's link is built
from its CIK and accession, never copied; a feed's only when it is a safe https link) or `period` (filings).
Kinds: price, filing, feed, config, derived.
"""
from __future__ import annotations

import re

_ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}")
_UNSAFE = re.compile(r"""[\s"'<>]""")


def filing_url(cik: str, accession: str) -> str:
    """SEC's index page of one filing, built from the company's CIK and the accession number."""
    cik_s, acc = str(cik), str(accession)
    if not (cik_s.isascii() and cik_s.isdigit()):
        raise ValueError(f"CIK must be digits: {cik!r}")
    if not _ACCESSION.fullmatch(acc):
        raise ValueError(f"not an accession number: {accession!r}")
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik_s)}/{acc.replace('-', '')}/{acc}-index.htm"


def safe_link(url: object) -> str | None:
    """The link when it is an https URL with no whitespace, quote, `<` or `>`; otherwise None."""
    if isinstance(url, str) and url.startswith("https://") and not _UNSAFE.search(url):
        return url
    return None


def price(source: str, bar_date: str, fetched_at: str | None) -> dict:
    return {"kind": "price", "label": f"{source} close", "date": bar_date, "ref": fetched_at}


def filing(cik: str, form: str, accession: str, filed: str, period: str | None = None) -> dict:
    out = {"kind": "filing", "label": form, "date": filed, "ref": accession}
    if period is not None:
        out["period"] = period
    out["url"] = filing_url(cik, accession)
    return out


def feed(label: str, as_of: str | None, ref: str | None, url: str | None = None) -> dict:
    out = {"kind": "feed", "label": label, "date": as_of, "ref": ref}
    link = safe_link(url)
    if link is not None:
        out["url"] = link
    return out


def config(setting: str, value: object) -> dict:
    label = setting if "[" in setting else f"[engine] {setting}"
    return {"kind": "config", "label": label, "date": None, "ref": str(value)}


def derived(label: str, ref: str, date: str | None = None) -> dict:
    return {"kind": "derived", "label": label, "date": date, "ref": ref}
