"""Replay transactions into a daily history, re-anchored on real snapshots.

See spec v0.2 section 4. Everything here works on plain rows so it can be tested
without a database; rebuild() wires it to the store.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date

from ..store import Store
from ..symbols import is_money_market

UNIT_TYPES = {"buy", "sell", "reinvest", "transfer", "other"}
EPS = 1e-9

HOLDINGS_COLUMNS = ("as_of_date", "account_id", "symbol", "units", "close", "market_value", "basis")
CASH_COLUMNS = ("as_of_date", "account_id", "amount", "basis")
LOT_COLUMNS = ("account_id", "symbol", "open_date", "units_open", "units_left", "cost_total", "cost_per_unit",
               "cost_known", "source_tx_id")
GAIN_COLUMNS = ("account_id", "symbol", "sell_date", "units", "proceeds", "cost_basis", "gain", "first_lot_date",
                "holding_days", "cost_known", "source_tx_id")
RECON_COLUMNS = ("as_of_date", "account_id", "symbol", "projected_units", "snapshot_units", "diff")


@dataclass
class ReplayResult:
    holdings: list[tuple] = field(default_factory=list)
    cash: list[tuple] = field(default_factory=list)
    lots: list[tuple] = field(default_factory=list)
    gains: list[tuple] = field(default_factory=list)
    recon: list[tuple] = field(default_factory=list)


@dataclass(eq=False)
class _Lot:
    open_date: str
    units_open: float
    units_left: float
    cost_total: float
    cost_known: bool
    tx_id: int | None

    @property
    def cost_per_unit(self) -> float:
        return self.cost_total / self.units_open if self.units_open else 0.0


def _signed_units(t: dict) -> float | None:
    u = t.get("units")
    if u is None:
        return None
    if t["type"] == "sell":
        return -abs(u)
    if t["type"] in ("buy", "reinvest"):
        return abs(u)
    return float(u)


def _open_lot(t: dict, units: float) -> _Lot:
    amount, price = t.get("amount"), t.get("price")
    if amount is not None and amount < 0:
        return _Lot(t["trade_date"], units, units, -amount, True, t.get("id"))
    if price:
        return _Lot(t["trade_date"], units, units, units * price, True, t.get("id"))
    return _Lot(t["trade_date"], units, units, 0.0, False, t.get("id"))


def _consume(lots: list[_Lot], need: float) -> tuple[float, str | None, bool, list[_Lot]]:
    """Take `need` units FIFO. Returns (cost taken, first lot date, cost fully known, lots emptied)."""
    cost, first, known, emptied = 0.0, None, True, []
    while need > EPS and lots:
        lot = lots[0]
        take = min(lot.units_left, need)
        cost += take * lot.cost_per_unit
        known = known and lot.cost_known
        first = first or lot.open_date
        lot.units_left -= take
        need -= take
        if lot.units_left <= EPS:
            emptied.append(lots.pop(0))
    if need > EPS:
        known = False
    return cost, first, known, emptied


MARKET_OPEN_UTC = "13:30"  # 09:30 New York in summer; a fetch stamped earlier is a pre-open snapshot


def _post_open(fetched_at: str | None) -> bool:
    """A snapshot fetched at/after the open (or of unknown time) already contains that day's trades."""
    return fetched_at is None or len(fetched_at) < 16 or fetched_at[11:16] >= MARKET_OPEN_UTC


def replay(transactions: list[dict], snapshots: dict[tuple[str, int], dict[str, float]],
           cash_snapshots: dict[tuple[str, int], float], closes: dict[str, list[tuple[str, float]]],
           calendar: list[str], fetch_times: dict[tuple[str, int], str | None] | None = None) -> ReplayResult:
    # Within a day, rows that add units come before rows that remove them: both Fidelity
    # downloads and the SnapTrade feed are newest-first, so a same-day round trip would
    # otherwise replay sell-before-buy and look like a short sale plus an open lot.
    txs = sorted(transactions, key=lambda t: (t["trade_date"], 1 if (_signed_units(t) or 0) < 0 else 0, t.get("id") or 0))
    accounts = {t["account_id"] for t in txs} | {a for _, a in snapshots} | {a for _, a in cash_snapshots}
    first: dict[int, str] = {}
    for t in txs:
        first[t["account_id"]] = min(first.get(t["account_id"], t["trade_date"]), t["trade_date"])
    for d, a in list(snapshots) + list(cash_snapshots):
        first[a] = min(first.get(a, d), d)
    cal = sorted(set(calendar))
    close_dates = {s: [d for d, _ in series] for s, series in closes.items()}
    close_vals = {s: [c for _, c in series] for s, series in closes.items()}

    def close_on(sym: str, d: str) -> float | None:
        dates = close_dates.get(sym)
        if not dates:
            return None
        i = bisect_right(dates, d) - 1
        return close_vals[sym][i] if i >= 0 else None

    out = ReplayResult()
    for acct in sorted(accounts):
        mine = [t for t in txs if t["account_id"] == acct]
        ptr = 0
        units: dict[str, float] = {}
        cash = 0.0
        open_lots: dict[str, list[_Lot]] = {}
        closed_lots: dict[str, list[_Lot]] = {}
        state = {"cash": 0.0}

        def apply(t: dict) -> None:
            sym = t.get("symbol")
            su = _signed_units(t)
            if sym and su is not None and t["type"] in UNIT_TYPES:
                units[sym] = units.get(sym, 0.0) + su
                book = open_lots.setdefault(sym, [])
                if su > 0:
                    book.append(_open_lot(t, su))
                elif su < 0:
                    cost, first_lot, known, emptied = _consume(book, -su)
                    closed_lots.setdefault(sym, []).extend(emptied)
                    if t["type"] == "sell":
                        proceeds = t.get("amount")
                        if proceeds is None and t.get("price"):
                            proceeds = -su * t["price"]
                        gain = (proceeds - cost) if (proceeds is not None and known) else None
                        days = ((date.fromisoformat(t["trade_date"]) - date.fromisoformat(first_lot)).days
                                if first_lot else None)
                        out.gains.append((acct, sym, t["trade_date"], -su, proceeds, cost, gain, first_lot,
                                          days, int(known), t.get("id")))
            if t.get("amount") is not None:
                state["cash"] += t["amount"]

        def apply_through(day: str, inclusive: bool) -> None:
            nonlocal ptr
            while ptr < len(mine) and (mine[ptr]["trade_date"] <= day if inclusive else mine[ptr]["trade_date"] < day):
                apply(mine[ptr])
                ptr += 1

        for d in cal:
            if d < first[acct]:
                continue
            key = (d, acct)
            # A pre-open snapshot is the state at the START of its day (anchor, then the
            # day's trades); one fetched after the open already contains them (trades, then anchor).
            # Without fetch times (tests, CSV-only setups) every snapshot is treated as pre-open.
            after_trades = fetch_times is not None and key in snapshots and _post_open(fetch_times.get(key))
            apply_through(d, inclusive=after_trades)
            basis = "reconstructed"
            if key in snapshots:
                snap = snapshots[key]
                for sym in sorted(set(units) | set(snap)):
                    proj, actual = units.get(sym, 0.0), snap.get(sym, 0.0)
                    if abs(proj - actual) > 1e-6:
                        out.recon.append((d, acct, sym, proj, actual, actual - proj))
                units.clear()
                units.update(snap)
                basis = "snapshot"
            cbasis = "reconstructed"
            if key in cash_snapshots:
                state["cash"] = cash_snapshots[key]
                cbasis = "snapshot"
            apply_through(d, inclusive=True)
            cash = state["cash"]
            for sym in sorted(units):
                u = units[sym]
                if abs(u) <= EPS:
                    continue
                c = close_on(sym, d)
                out.holdings.append((d, acct, sym, u, c, (u * c) if c is not None else None, basis))
            out.cash.append((d, acct, cash, cbasis))
        # trades dated after the last calendar day (today, before its bar exists) still shape lots and gains
        while ptr < len(mine):
            apply(mine[ptr])
            ptr += 1
        for sym in sorted(set(open_lots) | set(closed_lots)):
            for lot in closed_lots.get(sym, []) + open_lots.get(sym, []):
                out.lots.append((acct, sym, lot.open_date, lot.units_open, lot.units_left, lot.cost_total,
                                 lot.cost_per_unit, int(lot.cost_known), lot.tx_id))
    return out


def rebuild(store: Store) -> dict[str, int]:
    # Money-market sweeps ARE cash: buying or selling one moves cash into cash, so those
    # rows vanish; their dividends and interest are income and keep their amount.
    txs: list[dict] = []
    for t in store.transactions_for_replay():
        if t.get("symbol") and is_money_market(t["symbol"]):
            if t["type"] in ("buy", "sell", "reinvest"):
                continue
            t = dict(t, symbol=None)
        txs.append(t)
    result = replay(txs, store.snapshot_units(), store.snapshot_cash(),
                    store.closes_by_symbol(), store.trading_calendar(), fetch_times=store.snapshot_fetch_times())
    return {
        "holdings_daily": store.replace_rows("holdings_daily", HOLDINGS_COLUMNS, result.holdings),
        "cash_daily": store.replace_rows("cash_daily", CASH_COLUMNS, result.cash),
        "lots": store.replace_rows("lots", LOT_COLUMNS, result.lots),
        "realized_gains": store.replace_rows("realized_gains", GAIN_COLUMNS, result.gains),
        "reconciliation": store.replace_rows("reconciliation", RECON_COLUMNS, result.recon),
    }
