from pathlib import Path

from financial_data_collector.derive import history as H
from financial_data_collector.models import AccountRef, CashRow, PositionRow, PriceBar, Snapshot, TransactionRow
from financial_data_collector.store import Store

CAL = ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]


def tx(id, date, type, symbol, units, price, amount, acct=1):
    return dict(id=id, account_id=acct, trade_date=date, type=type, symbol=symbol, units=units, price=price, amount=amount)


def _h(r):
    return {(row[0], row[2]): row for row in r.holdings}


def test_units_forward_fill_and_zero_suppression():
    txs = [tx(1, "2026-01-02", "buy", "AAPL", 10, 100.0, -1000.0), tx(2, "2026-01-06", "sell", "AAPL", -10, 110.0, 1100.0)]
    closes = {"AAPL": [("2026-01-02", 100.0), ("2026-01-06", 110.0)]}       # no bar on 01-05 or 01-07
    r = H.replay(txs, {}, {}, closes, CAL)
    h = _h(r)
    assert h[("2026-01-02", "AAPL")][3:] == (10.0, 100.0, 1000.0, "reconstructed")
    assert h[("2026-01-05", "AAPL")][4:6] == (100.0, 1000.0)                  # forward-filled close
    assert ("2026-01-06", "AAPL") not in h and ("2026-01-07", "AAPL") not in h  # sold out: no zero rows
    cash = {row[0]: row for row in r.cash}
    assert [cash[d][2] for d in CAL] == [-1000.0, -1000.0, 100.0, 100.0]
    assert all(row[3] == "reconstructed" for row in r.cash)


def test_reanchor_to_snapshot_and_reconcile():
    txs = [tx(1, "2026-01-02", "buy", "AAPL", 12, 100.0, -1200.0), tx(2, "2026-01-02", "buy", "KO", 5, 50.0, -250.0)]
    snaps = {("2026-01-06", 1): {"AAPL": 10.0}}                                # KO gone, unrecorded
    cash = {("2026-01-06", 1): 300.0}
    closes = {"AAPL": [("2026-01-02", 100.0), ("2026-01-06", 100.0)], "KO": [("2026-01-02", 50.0)]}
    r = H.replay(txs, snaps, cash, closes, CAL)
    h = _h(r)
    assert h[("2026-01-05", "AAPL")][3] == 12.0
    assert h[("2026-01-06", "AAPL")][3:] == (10.0, 100.0, 1000.0, "snapshot")
    assert ("2026-01-06", "KO") not in h
    assert h[("2026-01-07", "AAPL")][3] == 10.0 and h[("2026-01-07", "AAPL")][6] == "reconstructed"
    assert sorted(r.recon) == [("2026-01-06", 1, "AAPL", 12.0, 10.0, -2.0), ("2026-01-06", 1, "KO", 5.0, 0.0, -5.0)]
    c = {row[0]: row for row in r.cash}
    assert c["2026-01-05"][2:] == (-1450.0, "reconstructed")
    assert c["2026-01-06"][2:] == (300.0, "snapshot") and c["2026-01-07"][2:] == (300.0, "reconstructed")


def test_weekend_dated_contribution_applies_next_calendar_day():
    txs = [tx(1, "2026-01-03", "contribution", None, None, None, 500.0)]      # a Saturday
    r = H.replay(txs, {}, {}, {}, CAL)
    assert {row[0]: row[2] for row in r.cash} == {"2026-01-05": 500.0, "2026-01-06": 500.0, "2026-01-07": 500.0}
    assert r.holdings == []


def test_fifo_lots_and_realized_gains():
    txs = [tx(1, "2026-01-02", "buy", "AAPL", 10, 100.0, -1000.0), tx(2, "2026-01-05", "buy", "AAPL", 10, 120.0, -1200.0),
           tx(3, "2026-01-06", "sell", "AAPL", -15, 130.0, 1950.0)]
    r = H.replay(txs, {}, {}, {"AAPL": [("2026-01-02", 100.0)]}, CAL)
    lots = sorted(r.lots, key=lambda l: l[2])
    assert [(l[3], l[4], l[5], l[7]) for l in lots] == [(10.0, 0.0, 1000.0, 1), (10.0, 5.0, 1200.0, 1)]
    (g,) = r.gains
    assert g[1:] == ("AAPL", "2026-01-06", 15.0, 1950.0, 1600.0, 350.0, "2026-01-02", 4, 1, 3)


def test_sale_exceeding_lots_is_flagged():
    txs = [tx(1, "2026-01-05", "sell", "AAPL", -5, 100.0, 500.0)]
    r = H.replay(txs, {}, {}, {}, CAL)
    (g,) = r.gains
    assert (g[4], g[5], g[6], g[9]) == (500.0, 0.0, None, 0)
    assert r.lots == []
    assert _h(r)[("2026-01-05", "AAPL")][3:6] == (-5.0, None, None)            # the ledger says short; reported as-is


def test_lot_with_unknown_cost_marks_later_sale():
    txs = [tx(1, "2026-01-02", "transfer", "AAPL", 4, None, None), tx(2, "2026-01-05", "buy", "AAPL", 1, 100.0, -100.0),
           tx(3, "2026-01-06", "sell", "AAPL", -5, 100.0, 500.0)]
    r = H.replay(txs, {}, {}, {}, CAL)
    (g,) = r.gains
    assert g[9] == 0 and g[6] is None and g[5] == 100.0          # only the bought share's cost is known


def test_rebuild_from_store(tmp_path: Path):
    s = Store.open(tmp_path / "w.db")
    acct = AccountRef("Sample Brokerage", "brokerage", "fidelity", "brokerage")
    s.write_transactions([
        TransactionRow(acct, "2026-01-02", "contribution", None, None, None, 2000.0, None, "EFT", "fidelity_csv"),
        TransactionRow(acct, "2026-01-02", "buy", "AAPL", 10.0, 100.0, -1000.0, 0.0, "YOU BOUGHT", "fidelity_csv"),
    ])
    s.write_snapshot(Snapshot("2026-01-06", "snaptrade", [PositionRow(acct, "AAPL", "APPLE INC", 10, 110.0, 1100.0)],
                              [CashRow(acct, 1000.0)], fetched_at="2026-01-06T13:00:00Z"))
    s.write_prices("AAPL", [PriceBar("2026-01-02", 100.0, 100.0), PriceBar("2026-01-05", 105.0, 105.0),
                            PriceBar("2026-01-06", 110.0, 110.0), PriceBar("2026-01-07", 112.0, 112.0)], "tiingo")
    counts = H.rebuild(s)
    assert counts["holdings_daily"] == 4 and counts["cash_daily"] == 4 and counts["lots"] == 1
    full = {r["as_of_date"]: r for r in s.query("SELECT * FROM portfolio_daily_full ORDER BY 1")}
    assert full["2026-01-05"]["total"] == 2050.0 and full["2026-01-05"]["basis"] == "reconstructed"
    assert full["2026-01-06"]["total"] == 2100.0 and full["2026-01-06"]["basis"] == "snapshot"
    assert full["2026-01-07"]["total"] == 2120.0 and full["2026-01-07"]["fidelity_total"] == 2120.0
    s.close()


def test_trade_on_snapshot_day_applies_after_the_anchor():
    # A snapshot is the state at the start of its day (morning fetch, or an evening
    # fetch dated the next UTC day), so a trade dated that day lands on top of it.
    txs = [tx(1, "2026-01-02", "buy", "AAPL", 10, 100.0, -1000.0), tx(2, "2026-01-06", "buy", "AAPL", 5, 100.0, -500.0)]
    snaps = {("2026-01-06", 1): {"AAPL": 10.0}}
    cash = {("2026-01-06", 1): 100.0}
    r = H.replay(txs, snaps, cash, {"AAPL": [("2026-01-02", 100.0)]}, CAL)
    h = _h(r)
    assert h[("2026-01-06", "AAPL")][3] == 15.0 and h[("2026-01-07", "AAPL")][3] == 15.0
    assert r.recon == []
    c = {row[0]: row for row in r.cash}
    assert c["2026-01-06"][2:] == (-400.0, "snapshot")


def test_money_market_transactions_only_move_cash(tmp_path: Path):
    s = Store.open(tmp_path / "w.db")
    acct = AccountRef("Sample Brokerage", "brokerage", "fidelity", "brokerage")
    s.write_transactions([
        TransactionRow(acct, "2026-01-02", "contribution", None, None, None, 500.0, None, "EFT", "fidelity_csv"),
        TransactionRow(acct, "2026-01-02", "buy", "SPAXX", 500.0, 1.0, -500.0, 0.0, "YOU BOUGHT SPAXX", "fidelity_csv"),
        TransactionRow(acct, "2026-01-02", "dividend", "SPAXX", None, None, 2.0, None, "DIVIDEND RECEIVED SPAXX", "fidelity_csv"),
    ])
    s.write_prices("AAPL", [PriceBar("2026-01-02", 100.0, 100.0)], "tiingo")
    H.rebuild(s)
    assert s.query("SELECT COUNT(*) FROM holdings_daily")[0][0] == 0
    assert s.query("SELECT amount FROM cash_daily")[0][0] == 502.0      # the sweep purchase is cash moving into cash; its dividend is income
    assert s.query("SELECT COUNT(*) FROM lots")[0][0] == 0
    s.close()


def test_same_day_round_trip_inserted_sell_first():
    # Fidelity downloads and the SnapTrade feed are newest-first, so the sell can get the lower id.
    txs = [tx(1, "2026-01-05", "sell", "AAPL", -5, 110.0, 550.0), tx(2, "2026-01-05", "buy", "AAPL", 5, 100.0, -500.0)]
    r = H.replay(txs, {}, {}, {"AAPL": [("2026-01-05", 110.0)]}, CAL)
    (g,) = r.gains
    assert (g[5], g[6], g[9]) == (500.0, 50.0, 1)                      # cost known: the buy was applied first
    assert all(l[4] == 0.0 for l in r.lots) and r.holdings == []       # flat position, no open lot


def test_post_open_snapshot_anchors_after_the_days_trades():
    txs = [tx(1, "2026-01-02", "buy", "AAPL", 10, 100.0, -1000.0), tx(2, "2026-01-06", "buy", "AAPL", 5, 100.0, -500.0)]
    snaps = {("2026-01-06", 1): {"AAPL": 15.0}}                          # fetched after the buy, so it already holds 15
    times = {("2026-01-06", 1): "2026-01-06T20:00:00.000Z"}
    r = H.replay(txs, snaps, {("2026-01-06", 1): 100.0}, {"AAPL": [("2026-01-02", 100.0)]}, CAL, fetch_times=times)
    h = _h(r)
    assert h[("2026-01-06", "AAPL")][3] == 15.0 and h[("2026-01-07", "AAPL")][3] == 15.0
    assert r.recon == []
    assert {row[0]: row[2] for row in r.cash}["2026-01-06"] == 100.0     # cash anchored after the trade too
    pre = {("2026-01-06", 1): "2026-01-06T12:54:00.000Z"}
    r2 = H.replay(txs, {("2026-01-06", 1): {"AAPL": 10.0}}, {}, {"AAPL": [("2026-01-02", 100.0)]}, CAL, fetch_times=pre)
    assert _h(r2)[("2026-01-06", "AAPL")][3] == 15.0 and r2.recon == []   # pre-open: anchor first, then the trade


def test_trades_after_the_last_calendar_day_still_reach_lots_and_gains():
    txs = [tx(1, "2026-01-02", "buy", "AAPL", 10, 100.0, -1000.0), tx(2, "2026-01-09", "sell", "AAPL", -10, 120.0, 1200.0)]
    r = H.replay(txs, {}, {}, {"AAPL": [("2026-01-02", 100.0)]}, CAL)   # calendar ends 01-07
    (g,) = r.gains
    assert g[2] == "2026-01-09" and g[6] == 200.0
    assert r.holdings[-1][3] == 10.0                                    # emitted days are unchanged
