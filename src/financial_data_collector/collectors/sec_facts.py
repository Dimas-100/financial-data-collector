"""SEC EDGAR company-facts: every XBRL fact a company ever filed, stored raw."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..config import Config, ConfigError
from ..http import Fetch, HttpError
from ..models import ConceptRule, Fact, LineItem
from ..store import Store
from ..symbols import is_money_market
from .sec_cik import load_cik_map, lookup

FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
PAUSE_SECONDS = 1.0
_FUND_TYPES = {"etf", "mutual_fund", "money_market", "crypto"}
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
) -> list[SecResult]:
    if not cfg.sec_user_agent:
        raise ConfigError("SEC_USER_AGENT is not set (put 'Your Name you@example.com' in .env); SEC blocks anonymous clients")
    if rebuild is None:
        from ..statements import rebuild as _rebuild
        rebuild = _rebuild
    headers = _headers(cfg.sec_user_agent)
    cik_map = load_cik_map(cfg.cache_dir / "company_tickers.json", fetch, cfg.sec_user_agent, now)
    rules = store.concept_rules()
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    results: list[SecResult] = []
    for row in store.securities():
        symbol, asset_type, cik = row["symbol"], row["asset_type"], row["cik"]
        if asset_type in _FUND_TYPES or is_money_market(symbol, row["description"]):
            continue
        if not cik:
            hit = lookup(cik_map, symbol)
            if hit is None:
                if asset_type == "unknown" and _looks_like_mutual_fund(symbol):
                    store.set_asset_type(symbol, "mutual_fund")
                results.append(SecResult(symbol, None, message="no CIK in SEC ticker map"))
                continue
            cik, name = hit
            store.set_security_cik(symbol, cik, name)
            if asset_type == "unknown":
                store.set_asset_type(symbol, "stock")
        if _is_fresh(row["last_sec_fetch"], now, cfg.sec_max_age_hours):
            results.append(SecResult(symbol, cik, message="fresh"))
            continue
        try:
            data = json.loads(fetch(FACTS_URL.format(cik=cik), headers))
        except (HttpError, ValueError, json.JSONDecodeError) as e:
            store.mark_sec_fetch(symbol, stamp)
            if isinstance(e, HttpError) and e.status == 404:
                # SEC has no XBRL facts for this CIK: a fund or trust, not an operating company
                store.set_asset_type(symbol, "etf")
                results.append(SecResult(symbol, cik, message="no company facts (404); classified as etf"))
            else:
                results.append(SecResult(symbol, cik, message=f"fetch failed: {e}"))
            sleep(PAUSE_SECONDS)
            continue
        facts = parse_company_facts(data)
        if not has_operating_facts(facts):
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
