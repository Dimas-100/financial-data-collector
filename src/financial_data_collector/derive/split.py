"""The weekly split: how cash waiting in an account is shared across holdings.

`holding_band` is this module's first part: one band (`cheap`, `usual` or `premium`) for a holding, read from its
primary valuation scores and leaning careful. Pure: no SQL, no store; the rows are the symbol's `valuation_scores`
view rows as dicts.
"""
from __future__ import annotations

PRIMARY_COMPANY = ("pe", "p_fcf", "ev_ebit", "ps")
PRIMARY_FUND = ("pe", "dividend_yield")
QUALITY = ("fcf_margin_change", "op_margin_change")
MIN_AVAILABLE = 2                     # fewer primary metrics with a band than this say nothing


def _status_code(row: dict, metric: str) -> str:
    """Why a primary row that is not `ok` with a band does not count."""
    status = row.get("status")
    if status == "held":
        return f"held:{metric}"
    if status == "not_enough_history":
        return f"no_history:{metric}"
    return f"unsupported:{metric}"


def _known(row: dict | None) -> bool:
    return row is not None and row.get("status") == "ok" and row.get("value") is not None


def holding_band(rows: list[dict], is_fund: bool) -> tuple[str, tuple[str, ...], tuple[dict, ...]]:
    """A holding's band, the reason codes, and the rows that decided it.

    rows: the symbol's `valuation_scores` rows ({metric, value, band, status, date, ...}). A company is read from
    `pe`, `p_fcf`, `ev_ebit` and `ps`, a fund from `pe` and `dividend_yield`; a metric counts only when its row is
    `ok` with a band. Fewer than two counted -> `usual` (`one_metric` / `no_metrics`). More than half `premium` ->
    `premium`. No `premium` and more than half `cheap` -> `cheap`, unless a company's free-cash-flow and operating
    margins are both known and both falling (-> `usual`, `quality_falling`); when either margin is not known `cheap`
    stands and says `quality_unknown`. Anything else -> `usual`, `disagree`.

    Reasons: primary metrics that did not count first, in PRIMARY order (`held:<m>`, `unsupported:<m>`,
    `no_history:<m>`, `missing:<m>`), then the verdict codes. The rows returned are the counted primary rows, plus the
    two quality rows when they changed or qualified the result.
    """
    primary = PRIMARY_FUND if is_fund else PRIMARY_COMPANY
    by_metric = {r["metric"]: r for r in rows}
    reasons: list[str] = []
    counted: list[tuple[str, dict]] = []
    for metric in primary:
        row = by_metric.get(metric)
        if row is None:
            reasons.append(f"missing:{metric}")
        elif row.get("status") == "ok" and row.get("band") is not None:
            counted.append((metric, row))
        else:
            reasons.append(_status_code(row, metric))
    deciding = tuple(r for _, r in counted)

    n = len(counted)
    if n < MIN_AVAILABLE:
        reasons.append("one_metric" if n == 1 else "no_metrics")
        return "usual", tuple(reasons), deciding

    premium = [m for m, r in counted if r["band"] == "premium"]
    cheap = [m for m, r in counted if r["band"] == "cheap"]
    if 2 * len(premium) > n:
        reasons.append("premium_by:" + ",".join(premium))
        return "premium", tuple(reasons), deciding
    if not premium and 2 * len(cheap) > n:
        if is_fund:
            reasons.append("cheap_by:" + ",".join(cheap))
            return "cheap", tuple(reasons), deciding
        quality = [by_metric.get(m) for m in QUALITY]
        quality_rows = tuple(q for q in quality if q is not None)
        if all(_known(q) for q in quality) and all(q["value"] < 0 for q in quality):
            reasons.append("quality_falling")
            return "usual", tuple(reasons), deciding + quality_rows
        reasons.append("cheap_by:" + ",".join(cheap))
        if not all(_known(q) for q in quality):
            reasons.append("quality_unknown")
            return "cheap", tuple(reasons), deciding + quality_rows
        return "cheap", tuple(reasons), deciding
    reasons.append("disagree")
    return "usual", tuple(reasons), deciding
