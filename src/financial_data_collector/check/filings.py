"""Is the warehouse caught up with SEC's own list of filings?

SEC's `companyfacts` answer is built from the filings, and it can lack one that SEC's `submissions` list shows
as filed days or months earlier (found in 2026: a 10-Q missing from companyfacts long after it was filed). fdc
dropped nothing; SEC's answer was behind. Values built on statements that miss the newest filing are older than
they look, so this check asks the submissions list, company by company, and holds a company's values from the
day a 10-Q, 10-K or 20-F (or an amendment) was filed until facts carrying its accession number arrive.

Only filings that carry XBRL count (`isXBRL`), only those filed in the last `WINDOW_DAYS` days, and only those
filed more than `GRACE_DAYS` ago (SEC needs a little time to publish the facts). Accession numbers are compared
as SEC writes them, with dashes; `sec_facts.accn` is stored the same way.
"""
from __future__ import annotations

import json
import re
from datetime import date
from zoneinfo import ZoneInfo

from ..connections.redact import redact
from . import CHECKS, CheckContext, Finding
from .findings import still_open

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/"
FORMS = frozenset({"10-Q", "10-K", "20-F", "10-Q/A", "10-K/A", "20-F/A"})
GRACE_DAYS = 3
WINDOW_DAYS = 400
ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}")      # as SEC writes it; anything else is not a filing we can name
PAUSE_SECONDS = 1.0                    # between companies, as the SEC collector does
NEW_YORK = ZoneInfo("America/New_York")


def missing(recent: dict, have_accns: set[str], today: date, grace_days: int = GRACE_DAYS, *,
            window_days: int = WINDOW_DAYS, xbrl_only: bool = True) -> list[tuple[str, str, str]]:
    """The 10-Q / 10-K / 20-F filings (and amendments) in SEC's `filings.recent` block that the warehouse has no
    facts for, as (form, accession, filed), in the order SEC lists them (newest first). `recent` holds parallel
    arrays. A filing counts when it was filed more than `grace_days` and at most `window_days` days before
    `today`, and (with `xbrl_only`) carries XBRL."""
    forms, accns, filed_on = recent.get("form") or [], recent.get("accessionNumber") or [], recent.get("filingDate") or []
    xbrl = recent.get("isXBRL") or []
    out: list[tuple[str, str, str]] = []
    for i, (form, accn, filed) in enumerate(zip(forms, accns, filed_on)):
        if form not in FORMS or not isinstance(accn, str) or not ACCESSION.fullmatch(accn) or accn in have_accns:
            continue
        if xbrl_only and not (i < len(xbrl) and xbrl[i] == 1):
            continue
        try:
            age = (today - date.fromisoformat(filed)).days
        except (TypeError, ValueError):
            continue
        if grace_days < age <= window_days:
            out.append((form, accn, filed))
    return out


def _headers(user_agent: str) -> dict[str, str]:
    return {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate", "Accept": "application/json"}


def filings_caught_up(ctx: CheckContext) -> list[Finding]:
    """One hold per missing filing (field "*", from its filing date, still going); one `info` finding for a
    company SEC could not be asked about. Companies are stocks with a CIK that the SEC collector has fetched; a
    CIK shared by several symbols is asked once."""
    user_agent = ctx.cfg.sec_user_agent
    if not user_agent:
        raise RuntimeError("SEC_USER_AGENT is not set")
    today = ctx.now.astimezone(NEW_YORK).date()
    by_cik: dict[str, list[str]] = {}
    for symbol, cik in ctx.store.check_sec_companies():
        by_cik.setdefault(cik, []).append(symbol)
    out: list[Finding] = []
    for n, (cik, symbols) in enumerate(by_cik.items()):
        if n:
            ctx.sleep(PAUSE_SECONDS)
        found: list[Finding] = []
        try:
            data = json.loads(ctx.fetch(SUBMISSIONS_URL.format(cik=cik), _headers(user_agent)))
            lacking = missing((data.get("filings") or {}).get("recent") or {}, ctx.store.check_sec_accessions(cik), today)
            days: set[str] = set()
            for form, accn, filed in lacking:
                if filed in days:        # a finding is keyed by its day: the newest-listed filing of that day speaks for it
                    continue
                days.add(filed)
                url = ARCHIVE_URL.format(cik=int(cik), accn=accn.replace("-", ""))
                found += [Finding("filings", s, "*", filed, None, "hold",
                                  f"SEC lists {form} {accn} filed {filed}, but the warehouse has no SEC facts from it",
                                  {"form": form, "accession": accn, "filed": filed, "url": url}) for s in symbols]
        except Exception as e:           # one company SEC will not answer must not stop the rest, nor lift its holds
            note = redact(f"{type(e).__name__}: {e}", [user_agent])
            found = []
            for s in symbols:
                found.append(Finding("filings", s, "*", today.isoformat(), None, "info",
                                     f"could not ask SEC for the filings list: {note}", {"cik": cik}))
                found += still_open(ctx.store, "filings", s)      # not asked is not cleared
        out += found
    return out


CHECKS["filings"] = filings_caught_up
