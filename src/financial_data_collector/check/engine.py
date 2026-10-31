"""The check on the valuation engine's scores: `scores`.

It reads `valuation_scores_raw` (what the engine wrote, not the view that applies holds) and holds back a score that
cannot show where it came from or does not follow from itself: an `ok` score with no sources, a source link that was
not built the way SEC's are, a percentile outside 0..100, or a band that its percentile, its metric and the `[engine]`
thresholds do not give. A fund's snapshot score that no fund can have (a P/E at or below 0 or above 200, a yield above
25%, an expense ratio above 3%) is held too. The band rule below is this file's own copy: it shares nothing with the code that wrote the
band, so one bug cannot fool both. A finding's field is `score:<metric>`, which the `valuation_scores` view blanks.

It never raises on a bad row: a row it cannot read is a finding on that row (an exception would stop the engine, which
builds only after a full, error-free check run).
"""
from __future__ import annotations

import json
import math
import re

from ..models import ENGINE_CHECK_NAMES, FUND_TYPES, SCORE_METRICS
from . import CHECKS, CheckContext, Finding

ENGINE_CHECKS = ENGINE_CHECK_NAMES      # the checks the engine step runs again: `scores` right after the engine writes, `split` right after the split writes

PREMIUM_HIGH = ("pe", "p_fcf", "ps", "ev_ebit")     # a high percentile of these is dear
CHEAP_HIGH = ("dividend_yield",)                   # a high percentile of the yield is cheap
FUND_MAX_PE = 200.0            # a fund snapshot's P/E must be above 0 and at most this
FUND_MAX_YIELD = 0.25          # fractions: 0.25 = 25%
FUND_MAX_EXPENSE_RATIO = 0.03
_FILING_URL = re.compile(r"https://www\.sec\.gov/Archives/edgar/data/(\d+)/(\d{18})/([^/]+)-index\.htm")


def _number(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _expected_band(metric: str, percentile: float, cheap: float, premium: float) -> str | None:
    """The band the checker's own rule gives: None for a metric that has no band."""
    if metric in PREMIUM_HIGH:
        return "cheap" if percentile < cheap else "premium" if percentile > premium else "usual"
    if metric in CHEAP_HIGH:
        return "cheap" if percentile > 100 - cheap else "premium" if percentile < 100 - premium else "usual"
    return None


def _sources(raw: object) -> list | None:
    """The row's sources as a list, or None when they cannot be read as one."""
    try:
        got = json.loads(raw) if isinstance(raw, (str, bytes)) else None
    except (ValueError, RecursionError):     # RecursionError: a pathologically nested text
        return None
    return got if isinstance(got, list) else None


def _link_problem(source: object) -> str | None:
    """Why a source's link was not built, or None when it is sound."""
    if not isinstance(source, dict):
        return "a source that is not a record"
    url = source.get("url")
    if source.get("kind") == "filing":
        ref = source.get("ref")
        m = _FILING_URL.fullmatch(url) if isinstance(url, str) else None
        if not m or not isinstance(ref, str) or m.group(3) != ref or m.group(2) != ref.replace("-", ""):
            return f"a filing link that is not built from its accession ({ref!r})"
        return None
    if "url" in source and not (isinstance(url, str) and url.startswith("https://")):
        return "a link that is not https"
    return None


def _fund_problem(metric: str, value: object) -> str | None:
    """Why a fund snapshot's value is out of any sane range (yields and expense ratios are fractions: a percent read
    as a fraction lands far above these), or None."""
    if not _number(value):
        return None
    if metric == "pe" and (value <= 0 or value > FUND_MAX_PE):
        return f"fund P/E {value:g} is not above 0 and at most {FUND_MAX_PE:g}"
    if metric == "dividend_yield" and value > FUND_MAX_YIELD:
        return f"fund yield {value:g} is above {FUND_MAX_YIELD:g}"
    if metric == "expense_ratio" and value > FUND_MAX_EXPENSE_RATIO:
        return f"fund expense ratio {value:g} is above {FUND_MAX_EXPENSE_RATIO:g}"
    return None


def scores(ctx: CheckContext) -> list[Finding]:
    """One hold per score row that is unsourced, has an unbuilt link, a percentile out of range, or a band its
    percentile does not give. Several faults in one row are one finding with every message."""
    cheap, premium = ctx.cfg.engine.cheap_percentile, ctx.cfg.engine.premium_percentile
    out: list[Finding] = []
    for r in ctx.store.check_score_rows():
        if r["metric"] not in SCORE_METRICS:
            # A field the frame would refuse is an error that blocks every later engine build; a warn on "*" blanks
            # nothing, and the next build replaces the row.
            out.append(Finding("scores", r["symbol"], "*", r["date"], r["date"], "warn",
                               f"unknown metric {r['metric']}", {"metric": r["metric"]}))
            continue
        problems: list[str] = []
        evidence: dict = {"status": r["status"]}
        listed = _sources(r["sources"])
        if r["status"] == "ok" and not listed:
            problems.append("no sources")
        for s in listed or []:
            why = _link_problem(s)
            if why:
                problems.append("a source link was not built")
                evidence["link"] = why
                break
        p, band, metric = r["percentile_5y"], r["band"], r["metric"]
        valid = _number(p) and 0 <= p <= 100
        if p is not None:
            evidence["percentile_5y"] = p
            if not valid:
                problems.append("percentile out of range")
        expected = _expected_band(metric, p, cheap, premium) if r["status"] == "ok" and valid else None
        if band != expected:        # a band on a row that is not ok or has no percentile, or one the rule does not give
            evidence.update(band=band, expected_band=expected)
            problems.append("band does not follow from its percentile")
        if r["status"] == "ok" and r["asset_type"] in FUND_TYPES:
            why = _fund_problem(metric, r["value"])
            if why:
                problems.append(why)
                evidence["value"] = r["value"]
        if problems:
            seen: list[str] = []
            for m in problems:
                if m not in seen:
                    seen.append(m)
            out.append(Finding("scores", r["symbol"], f"score:{metric}", r["date"], r["date"], "hold",
                               "; ".join(seen), evidence))
    return out


CHECKS["scores"] = scores
