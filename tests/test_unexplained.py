"""Money the records don't explain: the day's change in the replay that no transaction (or split) accounts for."""
import shutil
import sqlite3
from pathlib import Path

from financial_data_collector import migrate
from financial_data_collector.derive import history as H
from financial_data_collector.models import AccountRef, CashRow, PriceBar, Snapshot, TransactionRow
from financial_data_collector.store import Store

DAYS = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]


def tx(id, date, type, symbol, units, price, amount, acct=1):
    return dict(id=id, account_id=acct, trade_date=date, type=type, symbol=symbol, units=units, price=price, amount=amount)


def _cash_only(balances: dict[str, float]):
    """Cash-only statements (no fetch time), one per day."""
    return {(d, 1): {} for d in balances}, {(d, 1): v for d, v in balances.items()}


def _amounts(r):
    return {row[0]: row[4] for row in r.unexplained}


def test_a_deposit_the_balance_shows_and_no_transaction_records_is_unexplained_that_day():
    snaps, cash = _cash_only({"2026-01-05": 100.0, "2026-01-06": 100.0, "2026-01-07": 200.0, "2026-01-08": 200.0})
    r = H.replay([tx(1, "2026-01-05", "contribution", None, None, None, 100.0)], snaps, cash, {}, DAYS, fetch_times={})
    assert r.unexplained == [("2026-01-07", 1, 100.0, 0.0, 100.0)]


def test_once_the_deposit_is_recorded_on_that_day_nothing_is_unexplained():
    snaps, cash = _cash_only({"2026-01-05": 100.0, "2026-01-06": 100.0, "2026-01-07": 200.0, "2026-01-08": 200.0})
    txs = [tx(1, "2026-01-05", "contribution", None, None, None, 100.0),
           tx(2, "2026-01-07", "contribution", None, None, None, 100.0)]
    assert H.replay(txs, snaps, cash, {}, DAYS, fetch_times={}).unexplained == []


def test_a_deposit_recorded_a_day_after_the_balance_showed_it_pairs_with_that_day():
    # the balance had it on 01-07; the feed dates it 01-08: the money is not in transit on top of the balance
    snaps, cash = _cash_only({"2026-01-05": 100.0, "2026-01-06": 100.0, "2026-01-07": 200.0, "2026-01-08": 200.0})
    txs = [tx(1, "2026-01-05", "contribution", None, None, None, 100.0),
           tx(2, "2026-01-08", "contribution", None, None, None, 100.0)]
    r = H.replay(txs, snaps, cash, {}, DAYS, fetch_times={})
    assert _amounts(r) == {"2026-01-07": 100.0, "2026-01-08": -100.0}
    assert {row[0]: row[2] for row in r.cash}["2026-01-08"] == 200.0


WEEKS = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09", "2026-01-12", "2026-01-13",
         "2026-01-14", "2026-01-15", "2026-01-16"]


def test_a_spike_that_reverses_by_itself_within_a_week_is_not_money_moved():
    # a money-market sweep counted twice for a day, then once again: the value was wrong, no money moved
    snaps, cash = _cash_only(dict(zip(WEEKS[:4], [100.0, 100.0, 400.0, 100.0])))
    r = H.replay([tx(1, "2026-01-05", "contribution", None, None, None, 100.0)], snaps, cash, {}, WEEKS[:4],
                 fetch_times={})
    assert r.unexplained == []
    # over three days, give or take a dollar's market move in the spike
    snaps, cash = _cash_only(dict(zip(WEEKS[:5], [100.0, 100.0, 500.0, 450.0, 100.5])))
    r = H.replay([tx(1, "2026-01-05", "contribution", None, None, None, 100.0)], snaps, cash, {}, WEEKS[:5],
                 fetch_times={})
    assert r.unexplained == []


def test_a_reversed_run_stays_open_for_a_late_remainder():
    # four days of sweep spikes nearly cancel (−7.60 left); a reinvestment shown two days late brings the +7.60
    deltas = [-150.25, 412.80, -80.10, -190.05, 7.60]
    balances = [1000.0, 1000.0]
    for x in deltas:
        balances.append(round(balances[-1] + x, 2))
    snaps, cash = _cash_only(dict(zip(WEEKS[:7], balances)))
    r = H.replay([tx(1, "2026-01-05", "contribution", None, None, None, 1000.0)], snaps, cash, {}, WEEKS[:7],
                 fetch_times={})
    assert r.unexplained == []


def test_money_that_stays_past_a_week_is_money_moved_even_if_it_leaves_later():
    balances = [100.0, 100.0, 400.0, 400.0, 400.0, 400.0, 400.0, 400.0, 400.0, 100.0]   # 01-07 in, 01-16 out
    snaps, cash = _cash_only(dict(zip(WEEKS, balances)))
    r = H.replay([tx(1, "2026-01-05", "contribution", None, None, None, 100.0)], snaps, cash, {}, WEEKS,
                 fetch_times={})
    assert _amounts(r) == {"2026-01-07": 300.0, "2026-01-16": -300.0}


def test_less_than_a_dollar_is_rounding_not_money_moved():
    snaps, cash = _cash_only({"2026-01-05": 100.0, "2026-01-06": 100.4, "2026-01-07": 100.4})
    r = H.replay([tx(1, "2026-01-05", "contribution", None, None, None, 100.0)], snaps, cash, {}, DAYS[:3],
                 fetch_times={})
    assert r.unexplained == []


def test_a_late_deposit_pairs_with_earlier_cash_that_a_few_cents_of_income_blur():
    # the balance rose 99.40 (the 100 deposit, less a 0.60 dividend the replay had already counted); the feed then
    # posts the 100 dated a day later: it is that money, not 100 more in transit
    snaps, cash = _cash_only({"2026-01-05": 100.0, "2026-01-06": 100.0, "2026-01-07": 199.40, "2026-01-08": 199.40})
    txs = [tx(1, "2026-01-05", "contribution", None, None, None, 100.0),
           tx(2, "2026-01-08", "contribution", None, None, None, 100.0)]
    r = H.replay(txs, snaps, cash, {}, DAYS, fetch_times={})
    assert {row[0]: row[2] for row in r.cash}["2026-01-08"] == 199.40
    assert _amounts(r) == {"2026-01-07": 99.4, "2026-01-08": -100.0}


def test_a_deposit_in_transit_before_the_balance_shows_it_is_explained():
    snaps, cash = _cash_only({"2026-01-05": 100.0, "2026-01-06": 100.0, "2026-01-07": 300.0, "2026-01-08": 300.0})
    txs = [tx(1, "2026-01-05", "contribution", None, None, None, 100.0),
           tx(2, "2026-01-06", "contribution", None, None, None, 200.0)]
    assert H.replay(txs, snaps, cash, {}, DAYS, fetch_times={}).unexplained == []


def test_an_account_s_first_day_has_nothing_unexplained():
    snaps, cash = _cash_only({"2026-01-05": 500.0, "2026-01-06": 500.0})
    assert H.replay([], snaps, cash, {}, DAYS[:2], fetch_times={}).unexplained == []


def test_a_trade_the_feed_has_not_posted_leaves_only_the_fill_to_close_gap():
    # 01-07's statement holds 2 KO more and 98 less cash; the close is 50, so 2 shares are worth 100
    txs = [tx(1, "2026-01-05", "contribution", None, None, None, 1000.0)]
    snaps = {("2026-01-05", 1): {}, ("2026-01-06", 1): {}, ("2026-01-07", 1): {"KO": 2.0}}
    cash = {("2026-01-05", 1): 1000.0, ("2026-01-06", 1): 1000.0, ("2026-01-07", 1): 902.0}
    r = H.replay(txs, snaps, cash, {"KO": [("2026-01-05", 49.0), ("2026-01-07", 50.0)]}, DAYS[:3], fetch_times={})
    assert r.unexplained == [("2026-01-07", 1, -98.0, 100.0, 2.0)]


def test_a_split_without_a_transaction_is_not_money_moved():
    txs = [tx(1, "2026-01-05", "buy", "KLX", 3, 300.0, -900.0)]
    closes = {"KLX": [("2026-01-05", 300.0), ("2026-01-06", 30.5), ("2026-01-07", 31.0)]}
    splits = {"KLX": [("2026-01-06", 10.0)]}
    on_time = {("2026-01-06", 1): {"KLX": 30.0}, ("2026-01-07", 1): {"KLX": 30.0}}
    r = H.replay(txs, on_time, {}, closes, DAYS[:3], fetch_times={}, splits=splits)
    assert r.unexplained == []
    # a day late: the 01-06 statement still holds 3 and the 27 new shares arrive with 01-07's. The value was wrong
    # for a day (−27 × 30.50), then right again (+27 × 31.00): a spike that reverses, so no money moved
    late = {("2026-01-06", 1): {"KLX": 3.0}, ("2026-01-07", 1): {"KLX": 30.0}}
    r = H.replay(txs, late, {}, closes, DAYS[:3], fetch_times={}, splits=splits)
    assert r.unexplained == []


def test_a_split_the_records_carry_as_a_transaction_is_not_counted_twice():
    # Fidelity files a split's new shares as a distribution, on the ex-date or a few days after
    closes = {"KLX": [("2026-01-05", 300.0), ("2026-01-06", 30.5), ("2026-01-07", 31.0), ("2026-01-08", 31.5)]}
    splits = {"KLX": [("2026-01-06", 10.0)]}
    for when in ("2026-01-06", "2026-01-08"):
        txs = [tx(1, "2026-01-05", "buy", "KLX", 3, 300.0, -900.0), tx(2, when, "other", "KLX", 27, None, None)]
        r = H.replay(txs, {}, {}, closes, DAYS, fetch_times={}, splits=splits)
        assert r.unexplained == [], when


def test_rebuild_writes_the_unexplained_days(tmp_path: Path):
    s = Store.open(tmp_path / "w.db")
    acct = AccountRef("Sample Brokerage", "brokerage", "fidelity", "brokerage")
    s.write_transactions([TransactionRow(acct, "2026-01-05", "contribution", None, None, None, 100.0, None, "ACH",
                                         "snaptrade")])
    s.write_snapshot(Snapshot("2026-01-05", "snaptrade", [], [CashRow(acct, 100.0)], fetched_at="2026-01-05T12:00:00Z"))
    s.write_snapshot(Snapshot("2026-01-06", "snaptrade", [], [CashRow(acct, 175.0)], fetched_at="2026-01-06T12:00:00Z"))
    s.write_prices("AAPL", [PriceBar("2026-01-05", 100.0, 100.0), PriceBar("2026-01-06", 101.0, 101.0)], "tiingo")
    counts = H.rebuild(s)
    assert counts["unexplained_daily"] == 1
    rows = [tuple(r) for r in s.query("SELECT as_of_date, cash, holdings, amount FROM unexplained_daily")]
    assert rows == [("2026-01-06", 75.0, 0.0, 75.0)]
    s.close()


def test_a_version_6_warehouse_gains_the_table_with_its_rows_intact(tmp_path: Path):
    older = tmp_path / "migrations"
    older.mkdir()
    for p in migrate.MIGRATIONS_DIR.glob("*.sql"):
        if int(p.name[:4]) < 7:
            shutil.copy(p, older)
    db = tmp_path / "w.db"
    conn = sqlite3.connect(db)
    migrate.apply_migrations(conn, migrations_dir=older)
    conn.execute("INSERT INTO accounts (label, slug, institution, account_type, first_seen) "
                 "VALUES ('Sample Brokerage', 'brokerage', 'fidelity', 'brokerage', '2026-01-02')")
    conn.commit()
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 6
    conn.close()
    s = Store.open(db)
    try:
        assert s.query("SELECT MAX(version) FROM schema_version")[0][0] == 7
        assert s.query("SELECT label FROM accounts")[0][0] == "Sample Brokerage"
        assert s.counts()["unexplained_daily"] == 0
    finally:
        s.close()


def test_splits_come_from_the_price_feed(tmp_path: Path):
    s = Store.open(tmp_path / "w.db")
    s.write_prices("KLX", [PriceBar("2026-01-05", 300.0, 30.0), PriceBar("2026-01-06", 30.5, 30.5, split_factor=10.0)],
                   "tiingo")
    assert s.splits_by_symbol() == {"KLX": [("2026-01-06", 10.0)]}
    s.close()
