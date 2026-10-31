"""Ticker -> CIK map from SEC's company_tickers.json, and SEC's fund ticker list, each cached for a week."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..http import Fetch
from ..symbols import to_sec

CIK_URL = "https://www.sec.gov/files/company_tickers.json"
# every share class of a registered fund: mutual funds and the ETFs that are a series of a trust (most of them),
# which company_tickers.json leaves out; it lists only the trust-structured ones, such as SPY
FUND_TICKERS_URL = "https://www.sec.gov/files/company_tickers_mf.json"


def _headers(user_agent: str) -> dict[str, str]:
    return {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate", "Accept": "application/json"}


def _cached(cache_path: Path, url: str, fetch: Fetch, user_agent: str, now: datetime, max_age_days: int) -> bytes:
    if cache_path.is_file():
        age = now - datetime.fromtimestamp(cache_path.stat().st_mtime, timezone.utc)
        if age <= timedelta(days=max_age_days):
            return cache_path.read_bytes()
    raw = fetch(url, _headers(user_agent))
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(raw)
    return raw


def load_cik_map(
    cache_path: Path, fetch: Fetch, user_agent: str, now: datetime, max_age_days: int = 7
) -> dict[str, tuple[str, str]]:
    data = json.loads(_cached(cache_path, CIK_URL, fetch, user_agent, now, max_age_days))
    rows = data.values() if isinstance(data, dict) else data
    out: dict[str, tuple[str, str]] = {}
    for r in rows:
        ticker = str(r.get("ticker", "")).upper()
        if ticker:
            out[ticker] = (f"{int(r['cik_str']):010d}", str(r.get("title", "")))
    return out


def load_fund_tickers(
    cache_path: Path, fetch: Fetch, user_agent: str, now: datetime, max_age_days: int = 7
) -> frozenset[str]:
    """The tickers on SEC's fund list ({"fields": [..., "symbol"], "data": [[...], ...]}), in SEC's spelling."""
    data = json.loads(_cached(cache_path, FUND_TICKERS_URL, fetch, user_agent, now, max_age_days))
    at = data["fields"].index("symbol")
    return frozenset(str(r[at]).upper() for r in data["data"] if len(r) > at and r[at])


def lookup(cik_map: dict[str, tuple[str, str]], symbol: str) -> tuple[str, str] | None:
    return cik_map.get(to_sec(symbol))
