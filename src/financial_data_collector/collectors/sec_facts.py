"""SEC EDGAR company-facts: every XBRL fact a company ever filed, stored raw."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable

from ..config import Config, ConfigError
from ..http import Fetch, HttpError
from ..models import ConceptRule, Fact, LineItem
from ..store import Store
from ..symbols import canonical, is_money_market
from .sec_cik import load_cik_map, lookup

FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
PAUSE_SECONDS = 1.0
FUND_TYPES = frozenset({"etf", "mutual_fund", "money_market", "crypto", "fund", "foreign_filer"})
# the types the statements never shape: the SEC step skips them and derive clears their line items
_FOREIGN_FORMS = {"20-F", "20-F/A", "40-F", "6-K"}
_FUND_NAME = re.compile(r"\b(trust|fund|etf)\b", re.IGNORECASE)
_REVENUE_ENDINGS = ("LeaseIncome", "LeaseRevenue", "RealEstateRevenueNet")
Rebuild = Callable[[list[Fact], list[ConceptRule]], list[LineItem]]


@dataclass
class SecResult:
    symbol: str
    cik: str | None
    facts: int = 0
    line_items: int = 0
    message: str = ""


def _headers(user_agent: str) -> dict[str, str]:
    return {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate", "Accept": "application/json"}


def parse_company_facts(data: dict) -> list[Fact]:
    out: list[Fact] = []
    for taxonomy, concepts in (data.get("facts") or {}).items():
        for concept, body in concepts.items():
            for unit, observations in (body.get("units") or {}).items():
                for o in observations:
                    val = o.get("val")
                    end = o.get("end")
                    if val is None or not end:
                        continue
                    try:
                        value = float(val)
                    except (TypeError, ValueError):
                        continue
                    out.append(Fact(
                        taxonomy=taxonomy, concept=concept, unit=unit,
                        period_start=o.get("start") or "", period_end=end, value=value,
                        fy=int(o["fy"]) if o.get("fy") is not None else None,
                        fp=o.get("fp"), form=o.get("form") or "", filed=o.get("filed") or "",
                        accn=o.get("accn") or "", frame=o.get("frame"),
                    ))
    return out


def has_operating_facts(facts: list[Fact]) -> bool:
    return any(f.taxonomy == "us-gaap" and f.period_start for f in facts)


def _is_revenue(concept: str, revenue_concepts: frozenset[str]) -> bool:
    return (concept in revenue_concepts or concept.startswith("Revenue") or "InterestAndDividendIncome" in concept
            or concept.endswith(_REVENUE_ENDINGS))


def classify_filer(facts: list[Fact], sec_name: str | None, revenue_concepts: Iterable[str] = ()) -> str | None:
    """"foreign_filer", "fund" or None (an ordinary company the statements can shape).

    A foreign filer's newest filing is a 20-F, 40-F or 6-K, or its only operating facts are IFRS (a company that
    filed 10-Ks until 2012 and 20-Fs since has old us-gaap facts and is still a foreign filer). A fund-like filer
    has "trust", "fund" or "etf" in its name and no revenue in the two years before its newest filing: a bank
    called "... Trust Corp" has revenue and stays a company. Revenue is any us-gaap concept the concept map files
    under `revenue`, one starting with "Revenue", interest and dividend income (banks), or lease and real-estate
    revenue (REITs); `revenue_concepts` is the map's list, passed in so this stays pure."""
    if not facts:
        return None
    newest = max(f.filed for f in facts)
    if any(f.form in _FOREIGN_FORMS for f in facts if f.filed == newest):
        return "foreign_filer"
    us_gaap = any(f.taxonomy == "us-gaap" and f.period_start for f in facts)
    ifrs = any(f.taxonomy == "ifrs-full" and f.period_start for f in facts)
    if ifrs and not us_gaap:
        return "foreign_filer"
    if sec_name and _FUND_NAME.search(sec_name):
        try:
            cutoff = (date.fromisoformat(newest[:10]) - timedelta(days=730)).isoformat()
        except ValueError:
            return None                                    # a malformed date: no fund verdict, never an error
        known = frozenset(revenue_concepts)
        if not any(f.taxonomy == "us-gaap" and f.period_end >= cutoff and _is_revenue(f.concept, known)
                   for f in facts):
            return "fund"
    return None


def _looks_like_mutual_fund(symbol: str) -> bool:
    return len(symbol) == 5 and symbol.endswith("X") and symbol.isalpha()


def _is_fresh(last: str | None, now: datetime, max_age_hours: int) -> bool:
    if not last:
        return False
    try:
        t = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return now - t < timedelta(hours=max_age_hours)


def collect_sec(
    store: Store,
    cfg: Config,
    *,
    fetch: Fetch,
    now: datetime,
    rebuild: Rebuild | None = None,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[str], None] | None = None,
) -> list[SecResult]:
    if not cfg.sec_user_agent:
        raise ConfigError("SEC_USER_AGENT is not set (put 'Your Name you@example.com' in .env); SEC blocks anonymous clients")
    if rebuild is None:
        from ..statements import rebuild as _rebuild
        rebuild = _rebuild
    headers = _headers(cfg.sec_user_agent)
    cik_map = load_cik_map(cfg.cache_dir / "company_tickers.json", fetch, cfg.sec_user_agent, now)
    rules = store.concept_rules()
    revenue_concepts = frozenset(r.concept for r in rules if r.line_item == "revenue" and r.taxonomy == "us-gaap")
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    forced = {canonical(s) for s in cfg.classify}   # [classify] decides these types: nothing here re-types them
    results: list[SecResult] = []
    securities = store.securities()
    for i, row in enumerate(securities, start=1):
        symbol, asset_type, cik = row["symbol"], row["asset_type"], row["cik"]
        sec_name = row["sec_name"]
        if asset_type in FUND_TYPES or is_money_market(symbol, row["description"]) or row["parked_reason"]:
            continue
        if progress:
            progress(f"{i}/{len(securities)} {symbol}")
        if not cik:
            hit = lookup(cik_map, symbol)
            if hit is None:
                if asset_type == "unknown" and _looks_like_mutual_fund(symbol) and symbol not in forced:
                    store.set_asset_type(symbol, "mutual_fund")
                results.append(SecResult(symbol, None, message="no CIK in SEC ticker map"))
                continue
            cik, name = hit
            sec_name = name
            store.set_security_cik(symbol, cik, name)
            if asset_type == "unknown" and symbol not in forced:
                store.set_asset_type(symbol, "stock")
        if _is_fresh(row["last_sec_fetch"], now, cfg.sec_max_age_hours):
            results.append(SecResult(symbol, cik, message="fresh"))
            continue
        try:
            data = json.loads(fetch(FACTS_URL.format(cik=cik), headers))
        except (HttpError, ValueError, json.JSONDecodeError) as e:
            store.mark_sec_fetch(symbol, stamp)
            if isinstance(e, HttpError) and e.status == 404 and symbol in forced:
                results.append(SecResult(symbol, cik, message=f"no company facts (404); kept as {asset_type} by [classify]"))
            elif isinstance(e, HttpError) and e.status == 404:
                # SEC has no XBRL facts for this CIK: a fund or trust, not an operating company
                store.set_asset_type(symbol, "etf")
                results.append(SecResult(symbol, cik, message="no company facts (404); classified as etf"))
            else:
                results.append(SecResult(symbol, cik, message=f"fetch failed: {e}"))
            sleep(PAUSE_SECONDS)
            continue
        facts = parse_company_facts(data)
        kind = None if symbol in forced else classify_filer(facts, sec_name or data.get("entityName"), revenue_concepts)
        if kind:
            # not an operating company the statements can shape: typed, its old lines cleared, skipped by this
            # step from now on (derive clears its lines too, since its old facts stay in sec_facts)
            store.set_asset_type(symbol, kind)
            store.mark_sec_fetch(symbol, stamp)
            store.replace_line_items(cik, [])
            results.append(SecResult(symbol, cik, facts=len(facts), message=f"classified as {kind}"))
            sleep(PAUSE_SECONDS)
            continue
        if not has_operating_facts(facts) and symbol not in forced:
            store.set_asset_type(symbol, "etf")
            store.mark_sec_fetch(symbol, stamp)
            results.append(SecResult(symbol, cik, facts=len(facts), message="no operating facts; classified as etf"))
            sleep(PAUSE_SECONDS)
            continue
        store.write_sec_facts(cik, facts)
        try:
            items = rebuild(facts, rules)
            store.replace_line_items(cik, items)
        except Exception as e:  # one odd filer must not abort the rest; its raw facts are kept
            store.mark_sec_fetch(symbol, stamp)
            results.append(SecResult(symbol, cik, facts=len(facts), message=f"statements failed: {type(e).__name__}: {e}"))
            sleep(PAUSE_SECONDS)
            continue
        store.mark_sec_fetch(symbol, stamp)
        results.append(SecResult(symbol, cik, facts=len(facts), line_items=len(items)))
        sleep(PAUSE_SECONDS)
    return results
