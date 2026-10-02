"""Replay transactions into a daily history, re-anchored on real snapshots.

See spec v0.2 section 4. Everything here works on plain rows so it can be tested
without a database; rebuild() wires it to the store.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta

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
UNEXPLAINED_COLUMNS = ("as_of_date", "account_id", "cash", "holdings", "amount")


@dataclass
class ReplayResult:
    holdings: list[tuple] = field(default_factory=list)
    cash: list[tuple] = field(default_factory=list)
    lots: list[tuple] = field(default_factory=list)
    gains: list[tuple] = field(default_factory=list)
    recon: list[tuple] = field(default_factory=list)
    unexplained: list[tuple] = field(default_factory=list)


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
TRANSIT_DAYS = 7           # a deposit the statements never show stops counting after a week
CASH_TOLERANCE = 0.05      # dollars: what "the cash change is explained by these rows" allows for rounding
MAX_TRANSIT = 12           # more unexplained cash rows than this at one statement: fall back to the clock


def _post_open(fetched_at: str | None) -> bool:
    """A snapshot fetched at/after the open (or of unknown time) already contains that day's trades."""
    return fetched_at is None or len(fetched_at) < 16 or fetched_at[11:16] >= MARKET_OPEN_UTC


@dataclass(eq=False)
class _Row:
    """A transaction applied to the replay but not yet confirmed by a statement."""
    date: str
    symbol: str | None      # set when the row moves units
    units: float
    cash: float


def _held_today(rows: list[_Row], want: float, by_clock: bool) -> int:
    """How many of a symbol's same-day rows (in replay order) the statement already holds: the prefix whose units
    explain the statement, nearest to the clock's guess; the clock's guess when none does."""
    guess = len(rows) if by_clock else 0
    sums = [0.0]
    for row in rows:
        sums.append(sums[-1] + row.units)
    fits = [k for k, s in enumerate(sums) if abs(s - want) <= 1e-6]
    return min(fits, key=lambda k: (abs(k - guess), -k)) if fits else guess


def _fit(rows: list[_Row], unexplained: float, guess: list[bool], share: float = 0.0) -> list[bool] | None:
    """The subset of cash-only rows that explains `unexplained` (to CASH_TOLERANCE, or `share` of it when that is
    more), closest to the clock's guess (earlier rows land first on a tie); None when no subset does, or when there
    are too many rows to try."""
    n = len(rows)
    if n > MAX_TRANSIT:
        return None
    tolerance = max(CASH_TOLERANCE, share * abs(unexplained))
    best: tuple | None = None
    for mask in range(1 << n):
        held = [bool(mask >> i & 1) for i in range(n)]
        if abs(sum(r.cash for r, h in zip(rows, held) if h) - unexplained) > tolerance:
            continue
        key = (sum(h != g for h, g in zip(held, guess)), [not h for h in held])
        if best is None or key < best[0]:
            best = (key, held)
    return best[1] if best else None


PAIR_SHARE = 0.01             # a deposit claiming cash a statement showed earlier may differ by 1% (income beside it)
MIN_UNEXPLAINED = 1.0         # dollars: less is rounding or a fill's gap to the close, and stays growth
REVERSAL_SHARE = 0.01         # a spike "reverses" when the days after cancel it to a dollar, or 1% of its size


def _without_reversals(found: list[tuple]) -> list[tuple]:
    """Leave out unexplained changes that undo each other within TRANSIT_DAYS: a value that was wrong for a day or
    two (a money-market sweep counted twice) and then right again moved no money. `found` rows are (day, cash,
    holdings, amount, posting) in day order; a posting (the day a transaction records money a statement showed
    earlier) is real money and never closes a spike."""
    drop: set[int] = set()
    # runs still open to the days after: (members, total, size, last day). A run that reversed stays open as one,
    # so a remainder a statement shows a day or two late (a reinvestment) can still join it
    open_: list[tuple[list[int], float, float, str]] = []
    for i, (day, _, _, amount, posting) in enumerate(found):
        if posting:
            continue
        cutoff = (date.fromisoformat(day) - timedelta(days=TRANSIT_DAYS)).isoformat()
        open_ = [run for run in open_ if run[3] >= cutoff]
        for k in range(len(open_)):
            members = [j for run in open_[k:] for j in run[0]] + [i]
            total = sum(run[1] for run in open_[k:]) + amount
            size = sum(run[2] for run in open_[k:]) + abs(amount)
            if abs(total) <= max(MIN_UNEXPLAINED, REVERSAL_SHARE * size):
                drop.update(members)
                open_ = [*open_[:k], (members, total, size, day)]
                break
        else:
            open_.append(([i], amount, abs(amount), day))
    return [f for i, f in enumerate(found) if i not in drop]


SPLIT_DAYS = 7                # a split's new shares filed as a transaction land within a week of the ex-date


def _split_recorded(txs: list[dict], symbol: str, ex_date: str, new_shares: float) -> bool:
    """Whether the records carry a split's new shares as a row of their own (Fidelity files them as a
    distribution), dated within SPLIT_DAYS of the ex-date."""
    ex = date.fromisoformat(ex_date)
    for t in txs:
        units = _signed_units(t)
        if (t.get("symbol") == symbol and t["type"] in UNIT_TYPES and units is not None
                and abs((date.fromisoformat(t["trade_date"]) - ex).days) <= SPLIT_DAYS
                and abs(units - new_shares) <= max(1e-6, 1e-4 * abs(new_shares))):
            return True
    return False


def _landed(rows: list[_Row], unexplained: float, guess: list[bool]) -> list[bool]:
    """Which cash-only rows the statement's cash already holds: the subset that explains the cash change; the
    clock's guess when no subset does. A deposit is dated when it is sent, but a broker's balance can show it a day
    or two later, or before that day's trades."""
    held = _fit(rows, unexplained, guess)
    return guess if held is None else held


def replay(transactions: list[dict], snapshots: dict[tuple[str, int], dict[str, float]],
           cash_snapshots: dict[tuple[str, int], float], closes: dict[str, list[tuple[str, float]]],
           calendar: list[str], fetch_times: dict[tuple[str, int], str | None] | None = None,
           splits: dict[str, list[tuple[str, float]]] | None = None) -> ReplayResult:
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

    def split_since(sym: str, after: str, through: str) -> float:
        """How many shares one share became from the day after `after` through `through`."""
        factor = 1.0
        for d, f in (splits or {}).get(sym, []):
            if after < d <= through:
                factor *= f
        return factor

    out = ReplayResult()
    for acct in sorted(accounts):
        mine = [t for t in txs if t["account_id"] == acct]
        ptr = 0
        # what the last statement said, plus the rows applied since that no statement has confirmed yet
        confirmed: dict[str, float] = {}
        confirmed_cash = 0.0
        pending: list[_Row] = []
        applied: list[_Row] = []      # the rows the current day applied
        # cash a statement showed that no row explained, (day, amount): a deposit the feed dates after the balance
        # shows it is that money, not more in transit on top of it
        unclaimed: list[tuple[str, float]] = []
        prev: tuple[str, float, dict[str, float]] | None = None   # the last emitted day, its cash and its units
        found: list[tuple] = []       # each day's unexplained change, before spikes that reverse are left out
        open_lots: dict[str, list[_Lot]] = {}
        closed_lots: dict[str, list[_Lot]] = {}

        def apply(t: dict) -> None:
            sym = t.get("symbol")
            su = _signed_units(t)
            moves = bool(sym) and su is not None and t["type"] in UNIT_TYPES
            row = _Row(t["trade_date"], sym if moves else None, su if moves else 0.0,
                       t["amount"] if t.get("amount") is not None else 0.0)
            pending.append(row)
            applied.append(row)
            if moves:
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

        def apply_through(day: str) -> None:
            nonlocal ptr
            while ptr < len(mine) and mine[ptr]["trade_date"] <= day:
                apply(mine[ptr])
                ptr += 1

        for d in cal:
            if d < first[acct]:
                continue
            key = (d, acct)
            applied.clear()
            apply_through(d)
            basis = cbasis = "reconstructed"
            if key in snapshots or key in cash_snapshots:
                # Which applied rows does this statement already hold? Its fetch time is only a guess (a feed stamped
                # start-of-day can be live, a cash-only statement has no time), so the statement's own units and cash
                # decide, and the clock breaks ties. Without fetch times (tests, CSV-only setups) the guess is pre-open.
                by_clock = fetch_times is not None and key in snapshots and _post_open(fetch_times.get(key))
                trades = [r for r in pending if r.symbol]
                held = {id(r) for r in trades if r.date < d}          # a trade is in every statement after its day
                today: dict[str, list[_Row]] = {}
                for r in trades:
                    if r.date == d:
                        today.setdefault(r.symbol, []).append(r)
                if key in snapshots:
                    snap = snapshots[key]
                    for sym, rows in today.items():
                        base = confirmed.get(sym, 0.0) + sum(r.units for r in trades if r.symbol == sym and r.date < d)
                        held |= {id(r) for r in rows[:_held_today(rows, snap.get(sym, 0.0) - base, by_clock)]}
                    projected = dict(confirmed)
                    for r in trades:
                        if id(r) in held:
                            projected[r.symbol] = projected.get(r.symbol, 0.0) + r.units
                    for sym in sorted(set(projected) | set(snap)):
                        proj, actual = projected.get(sym, 0.0), snap.get(sym, 0.0)
                        if abs(proj - actual) > 1e-6:
                            out.recon.append((d, acct, sym, proj, actual, actual - proj))
                    confirmed = dict(snap)
                    basis = "snapshot"
                else:
                    held |= {id(r) for rows in today.values() for r in rows if by_clock}
                    for r in trades:
                        if id(r) in held:
                            confirmed[r.symbol] = confirmed.get(r.symbol, 0.0) + r.units
                if key in cash_snapshots:
                    cutoff = (date.fromisoformat(d) - timedelta(days=TRANSIT_DAYS)).isoformat()
                    others = [r for r in pending if not r.symbol]
                    held |= {id(r) for r in others if r.date < cutoff}
                    recent = [r for r in others if r.date >= cutoff]
                    unexplained = (cash_snapshots[key] - confirmed_cash
                                   - sum(r.cash for r in pending if id(r) in held))
                    guess = [r.date < d or by_clock for r in recent]
                    unclaimed = [(day, amount) for day, amount in unclaimed if day >= cutoff]
                    claimed = unexplained + sum(a for _, a in unclaimed)
                    paired = _fit(recent, claimed, guess, PAIR_SHARE) if unclaimed else None
                    landed = paired if paired is not None else _landed(recent, unexplained, guess)
                    left = unexplained - sum(r.cash for r, h in zip(recent, landed) if h)
                    if paired is not None:
                        unclaimed = []
                    elif abs(left) > CASH_TOLERANCE:
                        unclaimed.append((d, left))
                    held |= {id(r) for r, h in zip(recent, landed) if h}
                    confirmed_cash = cash_snapshots[key]
                    cbasis = "snapshot"
                else:
                    confirmed_cash += sum(r.cash for r in pending if id(r) in held)   # only trades are held here
                pending = [r for r in pending if id(r) not in held]
            units = dict(confirmed)
            for r in pending:
                if r.symbol:
                    units[r.symbol] = units.get(r.symbol, 0.0) + r.units
            cash = confirmed_cash + sum(r.cash for r in pending)
            for sym in sorted(units):
                u = units[sym]
                if abs(u) <= EPS:
                    continue
                c = close_on(sym, d)
                out.holdings.append((d, acct, sym, u, c, (u * c) if c is not None else None, basis))
            out.cash.append((d, acct, cash, cbasis))
            if prev is not None:
                # what changed since the last day that the rows applied today (and any split) don't explain, valued
                # the way the account's value is: at the day's close, nothing for a share without one
                day_before, cash_before, units_before = prev
                moved_cash = cash - cash_before - sum(r.cash for r in applied)
                moved_holdings = 0.0
                for sym in set(units) | set(units_before):
                    before = units_before.get(sym, 0.0)
                    factor = split_since(sym, day_before, d)
                    if factor != 1.0 and _split_recorded(mine, sym, d, before * (factor - 1)):
                        factor = 1.0      # the records carry the new shares themselves; that row explains them
                    expected = before * factor + sum(r.units for r in applied if r.symbol == sym)
                    extra = units.get(sym, 0.0) - expected
                    if abs(extra) > 1e-6:
                        moved_holdings += extra * (close_on(sym, d) or 0.0)
                amount = round(moved_cash + moved_holdings, 2)
                if amount != 0:
                    # the day a transaction records money a statement already showed: the change undoes it exactly
                    recorded = sum(r.cash for r in applied if not r.symbol)
                    posting = recorded != 0 and abs(amount + recorded) <= max(CASH_TOLERANCE, PAIR_SHARE * abs(recorded))
                    found.append((d, round(moved_cash, 2), round(moved_holdings, 2), amount, posting))
            prev = (d, cash, units)
        out.unexplained.extend((d, acct, c, h, a) for d, c, h, a, _ in _without_reversals(found)
                               if abs(a) >= MIN_UNEXPLAINED)
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
                    store.closes_by_symbol(), store.trading_calendar(), fetch_times=store.snapshot_fetch_times(),
                    splits=store.splits_by_symbol())
    return {
        "holdings_daily": store.replace_rows("holdings_daily", HOLDINGS_COLUMNS, result.holdings),
        "cash_daily": store.replace_rows("cash_daily", CASH_COLUMNS, result.cash),
        "lots": store.replace_rows("lots", LOT_COLUMNS, result.lots),
        "realized_gains": store.replace_rows("realized_gains", GAIN_COLUMNS, result.gains),
        "reconciliation": store.replace_rows("reconciliation", RECON_COLUMNS, result.recon),
        "unexplained_daily": store.replace_rows("unexplained_daily", UNEXPLAINED_COLUMNS, result.unexplained),
    }
