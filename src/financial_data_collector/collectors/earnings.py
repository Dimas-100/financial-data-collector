"""Earnings history and estimate snapshots: once per New York day, for each company in the universe.

The day a company reported comes from SEC when SEC lists it: an 8-K with item 2.02 ("Results of Operations and
Financial Condition") is the release, and its accession number names it. EPS expected, reported and the surprise come
from Yahoo, which also knows roughly when in the day it was (before the open, during, after the close). `merge` puts the
two together by plain rules, keeping what was stored before: Yahoo's list holds only the last couple of dozen reports
and SEC's `recent` block only its newest filings, so an old event must outlive both. Yahoo's estimates (this and next
quarter, this and next year) are stored once a day; Yahoo keeps no history of them.

SEC refuses anonymous clients: without SEC_USER_AGENT the SEC part is skipped and the dates are Yahoo's."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, time as clock, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from ..config import Config
from ..connections.redact import redact
from ..http import Fetch
from ..models import EarningsEvent, Estimate, YahooEarning
from ..store import Store
from . import yahoo as yahoo_mod
from .yahoo import YahooClient, finite

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}")      # as SEC writes it
PAUSE_SECONDS = 1.0                    # between companies asked of SEC
YAHOO_LIMIT = 24                       # earnings dates asked of Yahoo: six years of quarters
MATCH_DAYS = 3                         # an 8-K filed up to this many days after Yahoo's date is the same report
STOCK_TYPES = ("stock", "foreign_filer")
NEW_YORK = ZoneInfo("America/New_York")
OPEN, CLOSE = clock(9, 30), clock(16, 0)
SOURCE = "yfinance"
SEC_SKIPPED = "SEC part skipped: SEC_USER_AGENT is not set; "


@dataclass
class EarningsResult:
    symbol: str
    events: int                         # earnings_events rows added, removed or changed
    estimates: int                      # estimate_snapshots rows inserted or changed
    message: str = ""
    failed: bool = False                # a part raised: the company is not marked and is asked again


def report_dates(recent: dict) -> list[tuple[str, str]]:
    """(filing date, accession) of each 8-K in SEC's `filings.recent` block whose items include 2.02, in the order SEC
    lists them (newest first). `recent` holds parallel arrays; a row whose accession or date can't be read is
    skipped."""
    forms, accns = recent.get("form") or [], recent.get("accessionNumber") or []
    filed_on, items = recent.get("filingDate") or [], recent.get("items") or []
    out: list[tuple[str, str]] = []
    for i, (form, accn, filed) in enumerate(zip(forms, accns, filed_on)):
        if form != "8-K" or not isinstance(accn, str) or not ACCESSION.fullmatch(accn):
            continue
        listed = items[i] if i < len(items) else ""
        if not isinstance(listed, str) or "2.02" not in {part.strip() for part in listed.split(",")}:
            continue
        try:
            out.append((date.fromisoformat(filed).isoformat(), accn))
        except (TypeError, ValueError):
            continue
    return out


def _new_york(when: datetime) -> datetime:
    return when.replace(tzinfo=NEW_YORK) if when.tzinfo is None else when.astimezone(NEW_YORK)


def time_of_day(when: datetime) -> str | None:
    """When in the New York trading day a report came out. Exactly midnight means Yahoo knows only the day: None."""
    t = _new_york(when).time()
    if t == clock(0, 0):
        return None
    if t < OPEN:
        return "before_open"
    return "after_close" if t >= CLOSE else "during"


def _from_yahoo(symbol: str, row: YahooEarning) -> EarningsEvent:
    figures = (finite(row.eps_expected), finite(row.eps_reported), finite(row.surprise_pct))
    when = time_of_day(row.when)
    return EarningsEvent(symbol, _new_york(row.when).date().isoformat(), when, None, *figures, "yahoo",
                         "yahoo" if when else None, "yahoo" if any(v is not None for v in figures) else None)


def _near(sec_day: str, yahoo_day: str) -> bool:
    """Is an 8-K filed on `sec_day` the report Yahoo dates `yahoo_day`? The same day or up to MATCH_DAYS later (a
    release after the close is often filed the next morning)."""
    s, y = date.fromisoformat(sec_day), date.fromisoformat(yahoo_day)
    return y <= s <= y + timedelta(days=MATCH_DAYS)


def merge(symbol: str, sec: list[tuple[str, str]], yahoo: list[YahooEarning], stored: list[EarningsEvent],
          today: date) -> list[EarningsEvent]:
    """The company's earnings events from SEC's 8-K dates, Yahoo's rows and what was stored before, sorted by date.

    1. SEC events are the stored rows with an accession plus `sec`; an accession counts once (its new date wins), and
       of two on one day the first filed (lowest accession) stands for it.
    2. Yahoo rows dated after `today` (New York) are left out: the next date is not history.
    3. Each SEC event, oldest first, takes the nearest unused Yahoo row it is near (see `_near`): its EPS, and its time
       of day only when they share the day. The candidates are the new Yahoo rows and the stored Yahoo-dated rows (a new
       row wins over a stored one of the same day), so a Yahoo-dated event is absorbed when its 8-K appears later. With
       no match the stored row of that accession keeps its EPS and time, else they are None.
    4. An unused candidate with no SEC event near it is a Yahoo-dated event of its own (a stored one kept as stored)."""
    stored_sec = {e.accession: e for e in stored if e.accession}
    dated = {accn: e.report_date for accn, e in stored_sec.items()}
    dated.update({accn: filed for filed, accn in sec})
    by_day: dict[str, str] = {}
    for accn, day in sorted(dated.items()):
        by_day.setdefault(day, accn)

    candidates = {e.report_date: e for e in stored if e.date_source == "yahoo"}
    last = today.isoformat()
    fresh: dict[str, EarningsEvent] = {}
    for row in yahoo:
        event = _from_yahoo(symbol, row)
        if event.report_date <= last:
            fresh.setdefault(event.report_date, event)
    candidates.update(fresh)

    used: set[str] = set()
    out: list[EarningsEvent] = []
    for day in sorted(by_day):
        accn = by_day[day]
        near = [c for c in candidates if c not in used and _near(day, c)]
        if near:
            match = candidates[max(near)]                       # the latest Yahoo day is the nearest
            used.add(match.report_date)
            same = match.report_date == day
            out.append(EarningsEvent(symbol, day, match.time_of_day if same else None, accn, match.eps_expected,
                                     match.eps_reported, match.surprise_pct, "sec",
                                     match.time_source if same else None, match.eps_source))
            continue
        old = stored_sec.get(accn)
        if old:
            out.append(EarningsEvent(symbol, day, old.time_of_day, accn, old.eps_expected, old.eps_reported,
                                     old.surprise_pct, "sec", old.time_source, old.eps_source))
        else:
            out.append(EarningsEvent(symbol, day, None, accn, None, None, None, "sec", None, None))

    for day, event in candidates.items():
        if day not in used and not any(_near(s, day) for s in by_day):
            out.append(event)
    return sorted(out, key=lambda e: e.report_date)


def _estimates(rows: list[Estimate]) -> list[Estimate]:
    """Only the four periods the table keeps, and only finite numbers (NaN, as pandas gives a gap, becomes None)."""
    out = []
    for e in rows:
        if e.period not in yahoo_mod.PERIODS:
            continue
        analysts = finite(e.analysts)
        out.append(Estimate(e.period, finite(e.eps_avg), finite(e.eps_low), finite(e.eps_high),
                            int(analysts) if analysts is not None else None))
    return out


def _sic(value: object) -> int | None:
    """SEC writes the industry code as text ("3571"); an int is taken as it is, anything else is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _headers(user_agent: str) -> dict[str, str]:
    return {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate", "Accept": "application/json"}


def collect_earnings(
    store: Store,
    universe: list[str],
    cfg: Config,
    *,
    fetch: Fetch,
    yahoo: YahooClient,
    now: datetime,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[str], None] | None = None,
) -> list[EarningsResult]:
    """Ask SEC and Yahoo about each stock or foreign filer in the universe not yet asked about today (a New York day),
    merge the dates and store them with the day's estimates. A company is marked asked only when no part raised (SEC,
    Yahoo or the writes), so an outage is tried again; whatever did arrive is still stored. A CIK shared by several
    symbols is asked of SEC once a run."""
    today = now.astimezone(NEW_YORK).date()
    day = today.isoformat()
    stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    agent = cfg.sec_user_agent
    secrets = (agent, cfg.tiingo_token)
    rows = {row["symbol"]: row for row in store.securities()}
    answers: dict[str, dict | Exception] = {}               # what SEC said this run, by CIK (a failure too)
    sec_asked = yahoo_asked = 0

    def submissions(cik: str) -> dict:
        nonlocal sec_asked
        if cik not in answers:
            if sec_asked:
                sleep(PAUSE_SECONDS)
            sec_asked += 1
            try:
                answers[cik] = json.loads(fetch(SUBMISSIONS_URL.format(cik=cik), _headers(agent)))
            except Exception as e:
                answers[cik] = e
        answer = answers[cik]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def say(part: str, e: Exception) -> str:
        return redact(f"{part}: {type(e).__name__}: {e}", secrets)

    results: list[EarningsResult] = []
    for i, symbol in enumerate(universe, start=1):
        row = rows.get(symbol)
        if row is None or row["asset_type"] not in STOCK_TYPES or row["last_earnings_fetch"] == day:
            continue
        if progress:
            progress(f"{i}/{len(universe)} {symbol}")
        problems: list[str] = []
        sec: list[tuple[str, str]] = []
        if agent and row["cik"]:
            try:
                data = submissions(row["cik"])
                sec = report_dates((data.get("filings") or {}).get("recent") or {})
                store.set_sic(symbol, _sic(data.get("sic")))
            except Exception as e:     # one company SEC will not answer keeps its Yahoo part and is asked again
                problems.append(say("SEC", e))
        if yahoo_asked:
            sleep(yahoo_mod.PAUSE_SECONDS)
        yahoo_asked += 1
        dates: list[YahooEarning] = []
        estimates: list[Estimate] = []
        try:
            dates = yahoo.earnings_dates(symbol, YAHOO_LIMIT)
        except Exception as e:
            problems.append(say("Yahoo earnings dates", e))
        try:
            estimates = _estimates(yahoo.estimates(symbol))
        except Exception as e:
            problems.append(say("Yahoo estimates", e))
        changed = written = 0
        try:
            changed = store.replace_earnings(symbol, merge(symbol, sec, dates, store.earnings_events_of(symbol), today))
            written = store.write_estimates(symbol, day, estimates, stamp, SOURCE)
            if not problems:
                store.mark_earnings_fetch(symbol, day)          # only after the writes: a failed one is retried
        except Exception as e:
            problems.append(say("store", e))
        results.append(EarningsResult(symbol, changed, written, "; ".join(problems), failed=bool(problems)))
    return results
