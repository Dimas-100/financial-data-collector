"""Ticker -> CIK map from SEC's company_tickers.json, cached for a week."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..http import Fetch
from ..symbols import to_sec

CIK_URL = "https://www.sec.gov/files/company_tickers.json"


def _headers(user_agent: str) -> dict[str, str]:
    return {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate", "Accept": "application/json"}


def load_cik_map(
    cache_path: Path, fetch: Fetch, user_agent: str, now: datetime, max_age_days: int = 7
) -> dict[str, tuple[str, str]]:
    fresh = False
    if cache_path.is_file():
        age = now - datetime.fromtimestamp(cache_path.stat().st_mtime, timezone.utc)
        fresh = age <= timedelta(days=max_age_days)
    if fresh:
        raw = cache_path.read_bytes()
    else:
        raw = fetch(CIK_URL, _headers(user_agent))
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(raw)
    data = json.loads(raw)
    rows = data.values() if isinstance(data, dict) else data
    out: dict[str, tuple[str, str]] = {}
    for r in rows:
        ticker = str(r.get("ticker", "")).upper()
        if ticker:
            out[ticker] = (f"{int(r['cik_str']):010d}", str(r.get("title", "")))
    return out


def lookup(cik_map: dict[str, tuple[str, str]], symbol: str) -> tuple[str, str] | None:
    return cik_map.get(to_sec(symbol))
