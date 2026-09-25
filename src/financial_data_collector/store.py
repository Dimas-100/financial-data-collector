"""The only module that writes SQL. Everything else hands it dataclasses."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import migrate as _migrate
from .adapters.base import dedupe_key
from .models import (
    AccountRef, ConceptRule, Fact, LineItem, PriceBar, Snapshot, TransactionRow,
)

COUNT_TABLES = (
    "accounts", "securities", "position_snapshots", "cash_balances", "transactions",
    "prices", "sec_facts", "financial_line_items", "sync_runs", "ingested_files",
    "holdings_daily", "cash_daily", "lots", "realized_gains", "reconciliation", "valuation_daily",
)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Store:
    def __init__(self, conn: sqlite3.Connection, path: Path):
        self.conn = conn
        self.path = path

    # ---- lifecycle -------------------------------------------------------
    @classmethod
    def open(cls, path: Path | str, *, migrate: bool = True, backup_dir: Path | None = None) -> "Store":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        store = cls(conn, path)
        if migrate:
            store.migrate(backup_dir)
        return store

    def migrate(self, backup_dir: Path | None = None) -> list[int]:
        backup_to = None
        if backup_dir is not None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_to = backup_dir / f"warehouse-{stamp}.db"
        applied = _migrate.apply_migrations(self.conn, backup_to=backup_to)
        _migrate.seed_concept_map(self.conn)
        _migrate.rebuild_wide_views(self.conn)
        return applied

    def close(self) -> None:
        self.conn.close()

    def query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    # ---- accounts / securities ------------------------------------------
    def _account_id(self, ref: AccountRef, first_seen: str) -> int:
        self.conn.execute(
            """INSERT INTO accounts (label, slug, institution, account_type, first_seen)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(label) DO UPDATE SET
                 slug = excluded.slug,
                 institution = CASE WHEN excluded.institution != 'unknown' THEN excluded.institution ELSE accounts.institution END,
                 account_type = CASE WHEN excluded.account_type != 'other' THEN excluded.account_type ELSE accounts.account_type END,
                 first_seen = MIN(accounts.first_seen, excluded.first_seen)""",
            (ref.label, ref.slug, ref.institution, ref.account_type, first_seen),
        )
        return self.conn.execute("SELECT id FROM accounts WHERE label = ?", (ref.label,)).fetchone()[0]

    def upsert_account(self, ref: AccountRef, first_seen: str) -> int:
        with self.conn:
            return self._account_id(ref, first_seen)

    def _ensure_security(
        self, symbol: str, description: str | None, asset_type: str | None, first_seen: str
    ) -> None:
        self.conn.execute(
            """INSERT INTO securities (symbol, description, asset_type, first_seen)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
                 description = COALESCE(excluded.description, securities.description),
                 asset_type = CASE WHEN excluded.asset_type != 'unknown' THEN excluded.asset_type ELSE securities.asset_type END,
                 first_seen = MIN(securities.first_seen, excluded.first_seen)""",
            (symbol, description, asset_type or "unknown", first_seen),
        )

    def upsert_security(
        self, symbol: str, description: str | None = None, asset_type: str | None = None,
        first_seen: str | None = None,
    ) -> None:
        with self.conn:
            self._ensure_security(symbol, description, asset_type, first_seen or today())

    def securities(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM securities ORDER BY symbol")

    def set_security_cik(self, symbol: str, cik: str | None, sec_name: str | None) -> None:
        with self.conn:
            self.conn.execute("UPDATE securities SET cik = ?, sec_name = ? WHERE symbol = ?", (cik, sec_name, symbol))

    def mark_sec_fetch(self, symbol: str, when: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE securities SET last_sec_fetch = ? WHERE symbol = ?", (when, symbol))

    def set_asset_type(self, symbol: str, asset_type: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE securities SET asset_type = ? WHERE symbol = ?", (asset_type, symbol))

    # ---- positions / cash / transactions --------------------------------
    def _stale_accounts(self, snap: Snapshot) -> set[int]:
        """Account ids in this snapshot that already hold a NEWER same-day, same-source snapshot.

        A snapshot is a full statement of an account on that day, so for every
        other account the older same-source rows are deleted first (a position sold
        between two same-day exports must not linger), and rows of a newer export
        are never overwritten by an older one arriving late.
        """
        refs = {p.account for p in snap.positions} | {c.account for c in snap.cash}
        stale: set[int] = set()
        for ref in refs:
            aid = self._account_id(ref, snap.as_of_date)
            newest = self.conn.execute(
                "SELECT MAX(source_fetched_at) FROM position_snapshots "
                "WHERE as_of_date = ? AND account_id = ? AND source = ?",
                (snap.as_of_date, aid, snap.source),
            ).fetchone()[0]
            if newest and snap.fetched_at and snap.fetched_at < newest:
                stale.add(aid)
                continue
            self.conn.execute(
                "DELETE FROM position_snapshots WHERE as_of_date = ? AND account_id = ? AND source = ?",
                (snap.as_of_date, aid, snap.source),
            )
        return stale

    def write_snapshot(self, snap: Snapshot) -> int:
        n = 0
        with self.conn:
            stale = self._stale_accounts(snap)
            for p in snap.positions:
                aid = self._account_id(p.account, snap.as_of_date)
                if aid in stale:
                    continue
                asset_type = "crypto" if p.account.account_type == "crypto" else None
                self._ensure_security(p.symbol, p.description, asset_type, snap.as_of_date)
                self.conn.execute(
                    """INSERT INTO position_snapshots
                       (as_of_date, account_id, symbol, quantity, price, market_value, cost_basis_total,
                        avg_cost, unrealized_pnl, source, source_fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(as_of_date, account_id, symbol) DO UPDATE SET
                         quantity = excluded.quantity, price = excluded.price,
                         market_value = excluded.market_value, cost_basis_total = excluded.cost_basis_total,
                         avg_cost = excluded.avg_cost, unrealized_pnl = excluded.unrealized_pnl,
                         source = excluded.source, source_fetched_at = excluded.source_fetched_at""",
                    (snap.as_of_date, aid, p.symbol, p.quantity, p.price, p.market_value, p.cost_basis_total,
                     p.avg_cost, p.unrealized_pnl, snap.source, snap.fetched_at),
                )
                n += 1
            for c in snap.cash:
                aid = self._account_id(c.account, snap.as_of_date)
                if aid in stale:
                    continue
                self.conn.execute(
                    """INSERT INTO cash_balances (as_of_date, account_id, currency, amount, source)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(as_of_date, account_id, currency) DO UPDATE SET
                         amount = excluded.amount, source = excluded.source""",
                    (snap.as_of_date, aid, c.currency, c.amount, snap.source),
                )
                n += 1
        return n

    def latest_snapshot_date(self) -> str | None:
        row = self.conn.execute("SELECT MAX(as_of_date) FROM position_snapshots").fetchone()
        return row[0] if row else None

    def _seen_from_other_source(self, aid: int, r: TransactionRow) -> bool:
        """The same trade already stored from a different source (fee rounding may shift the amount a cent)."""
        row = self.conn.execute(
            """SELECT 1 FROM transactions
               WHERE account_id = ? AND trade_date = ? AND type = ? AND source != ?
                 AND COALESCE(symbol, '') = ? AND COALESCE(units, 0) = ?
                 AND ABS(COALESCE(amount, 0) - ?) <= 0.02
               LIMIT 1""",
            (aid, r.trade_date, r.type, r.source, r.symbol or "", r.units or 0, r.amount or 0),
        ).fetchone()
        return row is not None

    def write_transactions(self, rows: Iterable[TransactionRow]) -> int:
        n = 0
        with self.conn:
            for r in rows:
                aid = self._account_id(r.account, r.trade_date)
                if r.symbol:
                    self._ensure_security(r.symbol, None, None, r.trade_date)
                if self._seen_from_other_source(aid, r):
                    continue
                key = dedupe_key(r.account.label, r.trade_date, r.type, r.symbol, r.units, r.amount)
                cur = self.conn.execute(
                    """INSERT OR IGNORE INTO transactions
                       (account_id, trade_date, settlement_date, type, symbol, units, price, amount, fee,
                        description, source, dedupe_key)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (aid, r.trade_date, r.settlement_date, r.type, r.symbol, r.units, r.price, r.amount,
                     r.fee, r.description, r.source, key),
                )
                n += cur.rowcount
        return n

    # ---- prices -----------------------------------------------------------
    def last_price_date(self, symbol: str) -> str | None:
        return self.conn.execute("SELECT MAX(date) FROM prices WHERE symbol = ?", (symbol,)).fetchone()[0]

    def write_prices(self, symbol: str, bars: Iterable[PriceBar], source: str) -> int:
        bars = list(bars)
        with self.conn:
            self.conn.executemany(
                """INSERT INTO prices (symbol, date, close, adj_close, dividend, split_factor, source)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(symbol, date) DO UPDATE SET
                     close = excluded.close, adj_close = excluded.adj_close, dividend = excluded.dividend,
                     split_factor = excluded.split_factor, source = excluded.source""",
                [(symbol, b.date, b.close, b.adj_close, b.dividend, b.split_factor, source) for b in bars],
            )
            if bars:
                self.conn.execute("UPDATE securities SET price_source = ? WHERE symbol = ?", (source, symbol))
        return len(bars)

    # ---- SEC --------------------------------------------------------------
    def write_sec_facts(self, cik: str, facts: Iterable[Fact]) -> int:
        facts = list(facts)
        with self.conn:
            self.conn.executemany(
                """INSERT INTO sec_facts
                   (cik, taxonomy, concept, unit, period_start, period_end, value, fy, fp, form, filed, accn, frame)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(cik, taxonomy, concept, unit, period_start, period_end, accn) DO UPDATE SET
                     value = excluded.value, fy = excluded.fy, fp = excluded.fp, form = excluded.form,
                     filed = excluded.filed, frame = excluded.frame""",
                [(cik, f.taxonomy, f.concept, f.unit, f.period_start, f.period_end, f.value, f.fy, f.fp,
                  f.form, f.filed, f.accn, f.frame) for f in facts],
            )
        return len(facts)

    def ciks_with_facts(self) -> list[str]:
        return [r[0] for r in self.query("SELECT DISTINCT cik FROM sec_facts ORDER BY cik")]

    def facts_for(self, cik: str) -> list[Fact]:
        rows = self.query(
            "SELECT taxonomy, concept, unit, period_start, period_end, value, fy, fp, form, filed, accn, frame "
            "FROM sec_facts WHERE cik = ?", (cik,),
        )
        return [Fact(*tuple(r)) for r in rows]

    def replace_line_items(self, cik: str, items: Iterable[LineItem]) -> int:
        items = list(items)
        with self.conn:
            self.conn.execute("DELETE FROM financial_line_items WHERE cik = ?", (cik,))
            self.conn.executemany(
                """INSERT INTO financial_line_items
                   (cik, line_item, period_kind, period_start, period_end, fiscal_year, fiscal_quarter,
                    value, concept, filed, accn, is_derived, first_filed)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(cik, i.line_item, i.period_kind, i.period_start, i.period_end, i.fiscal_year,
                  i.fiscal_quarter, i.value, i.concept, i.filed, i.accn, int(i.is_derived),
                  i.first_filed or i.filed) for i in items],
            )
        return len(items)

    def concept_rules(self) -> list[ConceptRule]:
        rows = self.query(
            "SELECT line_item, statement, kind, taxonomy, concept, priority FROM concept_map "
            "ORDER BY line_item, priority"
        )
        return [ConceptRule(*tuple(r)) for r in rows]

    # ---- bookkeeping --------------------------------------------------------
    def file_seen(self, sha256: str) -> bool:
        return self.conn.execute("SELECT 1 FROM ingested_files WHERE sha256 = ?", (sha256,)).fetchone() is not None

    def record_file(self, sha256: str, path: str, kind: str, rows: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO ingested_files (sha256, path, kind, ingested_at, rows) VALUES (?,?,?,?,?)",
                (sha256, path, kind, utcnow(), rows),
            )

    def log_run(
        self, run_id: str, step: str, status: str, rows_written: int, message: str,
        started_at: str, finished_at: str,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO sync_runs (run_id, step, started_at, finished_at, status, rows_written, message) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_id, step, started_at, finished_at, status, rows_written, message[:4000]),
            )

    def counts(self) -> dict[str, int]:
        return {t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in COUNT_TABLES}

    # ---- derived data (rebuilt from scratch by the derive step) --------------
    def replace_rows(self, table: str, columns: tuple[str, ...], rows: Iterable[tuple]) -> int:
        rows = list(rows)
        cols, marks = ", ".join(columns), ", ".join("?" for _ in columns)
        with self.conn:
            self.conn.execute(f"DELETE FROM {table}")
            self.conn.executemany(f"INSERT INTO {table} ({cols}) VALUES ({marks})", rows)
        return len(rows)

    def prices_series(self, symbol: str) -> list[tuple[str, float, float, float]]:
        return [tuple(r) for r in self.query(
            "SELECT date, close, dividend, split_factor FROM prices WHERE symbol = ? ORDER BY date", (symbol,))]

    def ttm_by_cik(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for r in self.query(
            "SELECT cik, period_end, available_from, revenue, net_income, ocf, fcf, eps_diluted, "
            "shares_outstanding, shares_diluted FROM financials_ttm ORDER BY cik, available_from, period_end"
        ):
            out.setdefault(r["cik"], []).append(dict(r))
        return out

    def securities_with_cik(self) -> list[tuple[str, str]]:
        return [(r[0], r[1]) for r in self.query(
            "SELECT symbol, cik FROM securities WHERE cik IS NOT NULL ORDER BY symbol")]

    # The SnapTrade feed re-syncs the broker about once a day, early, so whatever time the
    # export ran its positions are start-of-day: report it as a pre-open time. A CSV export
    # is live but carries no time, so it stays None (treated as post-open by the replay).
    _FEED_PRE_OPEN = "1970-01-01T00:00:00Z"

    def snapshot_fetch_times(self) -> dict[tuple[str, int], str | None]:
        out: dict[tuple[str, int], str | None] = {}
        for r in self.query(
            "SELECT as_of_date, account_id, MAX(source) AS source, MAX(source_fetched_at) AS fetched "
            "FROM position_snapshots GROUP BY 1, 2"
        ):
            out[(r[0], r[1])] = self._FEED_PRE_OPEN if r[2] == "snaptrade" else r[3]
        return out

    def transactions_for_replay(self) -> list[dict]:
        return [dict(r) for r in self.query(
            "SELECT id, account_id, trade_date, type, symbol, units, price, amount FROM transactions "
            "ORDER BY trade_date, id")]

    def snapshot_units(self) -> dict[tuple[str, int], dict[str, float]]:
        out: dict[tuple[str, int], dict[str, float]] = {}
        for r in self.query("SELECT as_of_date, account_id, symbol, quantity FROM position_snapshots"):
            out.setdefault((r[0], r[1]), {})[r[2]] = r[3]
        for r in self.query("SELECT DISTINCT as_of_date, account_id FROM cash_balances"):
            out.setdefault((r[0], r[1]), {})  # a cash-only day is still a full statement of the account
        return out

    def snapshot_cash(self) -> dict[tuple[str, int], float]:
        return {(r[0], r[1]): r[2] for r in self.query(
            "SELECT as_of_date, account_id, SUM(amount) FROM cash_balances GROUP BY 1, 2")}

    def trading_calendar(self) -> list[str]:
        dates = {r[0] for r in self.query("SELECT DISTINCT date FROM prices")}
        dates |= {r[0] for r in self.query("SELECT DISTINCT as_of_date FROM position_snapshots")}
        dates |= {r[0] for r in self.query("SELECT DISTINCT as_of_date FROM cash_balances")}
        return sorted(dates)

    # ---- export helpers ---------------------------------------------------------
    def adj_close_series(self, symbol: str, start: str) -> list[tuple[str, float]]:
        return [tuple(r) for r in self.query(
            "SELECT date, adj_close FROM prices WHERE symbol = ? AND date >= ? ORDER BY date", (symbol, start))]

    def price_events(self, symbol: str) -> list[tuple[str, float, float]]:
        return [tuple(r) for r in self.query(
            "SELECT date, dividend, split_factor FROM prices WHERE symbol = ? ORDER BY date", (symbol,))]

    def annual_statement_rows(self, cik: str, limit: int) -> list[dict]:
        rows = self.query("SELECT * FROM financials_annual WHERE cik = ? ORDER BY fiscal_year DESC, period_end DESC LIMIT ?",
                          (cik, limit))
        return [dict(r) for r in reversed(rows)]

    def quarterly_statement_rows(self, cik: str, limit: int) -> list[dict]:
        rows = self.query("SELECT * FROM financials_quarterly WHERE cik = ? ORDER BY period_end DESC LIMIT ?", (cik, limit))
        return [dict(r) for r in reversed(rows)]

    def closes_by_symbol(self) -> dict[str, list[tuple[str, float]]]:
        out: dict[str, list[tuple[str, float]]] = {}
        for r in self.query("SELECT symbol, date, close FROM prices ORDER BY symbol, date"):
            out.setdefault(r[0], []).append((r[1], r[2]))
        return out
