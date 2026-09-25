"""investing's SnapTrade export (live-positions.json, snapshots/, live-activity.json).

Owner-only source. Shapes are those written by investing/pipeline/export_positions.mjs:
  live-positions.json  {fetchedAt, accounts[{label, slug, holdings[], cash[], totalMarketValue}]}
  live-activity.json   {fetchedAt, activities[{tradeDate, settlementDate, type, symbol, units,
                        price, amount, fee, accountLabel}]}
Dates are taken from the UTC timestamps as written; never converted to local time.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..models import AccountRef, CashRow, PositionRow, Snapshot, TransactionRow
from ..symbols import canonical, is_money_market
from .base import infer_account_type

SOURCE = "snaptrade"
_ACTIVITY_TYPES = {"BUY": "buy", "SELL": "sell", "DIVIDEND": "dividend", "CONTRIBUTION": "contribution",
                   "REI": "reinvest", "WITHDRAWAL": "withdrawal", "INTEREST": "interest", "FEE": "fee"}


def _account(label: str, slug: str | None = None) -> AccountRef:
    institution = "webull" if "WEBULL" in label.upper() else "fidelity"
    kind = infer_account_type(label)
    return AccountRef(label=label, slug=slug or "other", institution=institution, account_type=kind)


def _num(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _nonzero(v) -> float | None:
    f = _num(v)
    return None if not f else f


def parse_positions(path: Path) -> Snapshot:
    data = json.loads(path.read_text(encoding="utf-8"))
    fetched = data.get("fetchedAt") or ""
    if len(fetched) < 10:
        raise ValueError(f"{path.name}: missing fetchedAt")
    positions: list[PositionRow] = []
    cash: list[CashRow] = []
    for acct in data.get("accounts", []):
        ref = _account(acct.get("label", ""), acct.get("slug"))
        for h in acct.get("holdings", []) or []:
            raw = h.get("symbol") or ""
            desc = h.get("description")
            if not raw or is_money_market(raw, desc):
                continue
            units = _num(h.get("units")) or 0.0
            avg = _num(h.get("averageCost"))
            positions.append(PositionRow(
                account=ref, symbol=canonical(raw), description=desc, quantity=units,
                price=_num(h.get("price")), market_value=_num(h.get("marketValue")),
                cost_basis_total=(avg * units) if avg is not None else None,
                avg_cost=avg, unrealized_pnl=_num(h.get("openPnl")),
            ))
        for c in acct.get("cash", []) or []:
            amount = _num(c.get("amount"))
            if amount is None:
                continue
            cash.append(CashRow(account=ref, amount=amount, currency=c.get("currency") or "USD"))
    return Snapshot(as_of_date=fetched[:10], source=SOURCE, positions=positions, cash=cash, fetched_at=fetched)


def parse_activity(path: Path) -> list[TransactionRow]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows: list[TransactionRow] = []
    for a in data.get("activities", []):
        trade = (a.get("tradeDate") or "")[:10]
        label = a.get("accountLabel") or ""
        if len(trade) < 10 or not label:
            continue
        raw_type = (a.get("type") or "").upper()
        settle = (a.get("settlementDate") or "")[:10] or None
        raw_symbol = a.get("symbol") or ""
        rows.append(TransactionRow(
            account=_account(label),
            trade_date=trade,
            type=_ACTIVITY_TYPES.get(raw_type, "other"),
            symbol=canonical(raw_symbol) or None,
            units=_nonzero(a.get("units")),
            price=_nonzero(a.get("price")),
            amount=_num(a.get("amount")),
            fee=_num(a.get("fee")),
            description=raw_type,
            source=SOURCE,
            settlement_date=settle,
        ))
    return rows


def find_snapshot_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    snaps = directory / "snapshots"
    files = sorted(snaps.glob("live-positions-*.json")) if snaps.is_dir() else []
    live = directory / "live-positions.json"
    if live.is_file():
        files.append(live)
    return files
