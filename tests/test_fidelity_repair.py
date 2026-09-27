"""Migration 0005: Fidelity history rows stored before the adapter's classification fixes."""
from pathlib import Path

from financial_data_collector.adapters import fidelity_history as fh
from financial_data_collector.adapters.base import dedupe_key
from financial_data_collector.models import AccountRef
from financial_data_collector.store import Store

ROTH = AccountRef("Sample Roth", "roth", "fidelity", "roth_ira")
HEADER = ("Run Date,Action,Symbol,Description,Type,Price ($),Quantity,Commission ($),Fees ($),Accrued Interest ($),"
          "Amount ($),Cash Balance ($),Settlement Date")


def _legacy(store: Store, rows: list[tuple]) -> None:
    """Rows as the first import stored them: the old types, 0 units on cash-only rows, keys from those values."""
    aid = store.upsert_account(ROTH, "2025-01-01")
    with store.conn:
        for date, type_, symbol, units, amount, desc, source, suffix in rows:
            key = dedupe_key(ROTH.label, date, type_, symbol, units, amount) + suffix
            store.conn.execute(
                "INSERT INTO transactions (account_id, trade_date, type, symbol, units, amount, description, source,"
                " dedupe_key) VALUES (?,?,?,?,?,?,?,?,?)", (aid, date, type_, symbol, units, amount, desc, source, key))


def _rerun_0005(store: Store) -> None:
    with store.conn:
        store.conn.execute("DELETE FROM schema_version WHERE version = 5")
    assert store.migrate() == [5]


def test_repair_retypes_fidelity_rows_and_a_fresh_download_then_adds_nothing(tmp_path: Path):
    s = Store.open(tmp_path / "w.db")
    cc = "CASH CONTRIBUTION CURRENT YEAR (Cash)"
    _legacy(s, [
        ("2025-03-03", "other", None, 0.0, 100.0, cc, "fidelity_csv", ""),
        ("2025-03-10", "other", None, 0.0, 50.0, cc, "fidelity_csv", ""),
        ("2025-03-10", "other", None, 0.0, 50.0, cc, "fidelity_csv", "#2"),
        ("2025-11-03", "other", None, 0.0, 200.0, cc, "fidelity_csv", ""),        # the feed has this one too
        ("2025-11-03", "contribution", None, None, 200.0, "CONTRIBUTION", "snaptrade", ""),
        ("2025-03-04", "other", None, 0.0, -0.19, "DIRECT DEBIT BANK ACCTVERIFY (Cash)", "fidelity_csv", ""),
        ("2025-05-27", "withdrawal", "KO", 3.0, 150.0, "DISTRIBUTION COCA COLA CO (KO) (Cash)", "fidelity_csv", ""),
        ("2025-03-05", "dividend", "KO", 0.0, 1.5, "DIVIDEND RECEIVED COCA COLA CO (KO) (Cash)", "fidelity_csv", ""),
        ("2025-03-06", "buy", "KO", 2.0, -100.0, "YOU BOUGHT COCA COLA CO (KO) (Cash)", "fidelity_csv", ""),
    ])
    _rerun_0005(s)
    got = sorted((r["trade_date"], r["type"], r["units"], r["amount"], r["source"])
                 for r in s.query("SELECT * FROM transactions"))
    assert got == [
        ("2025-03-03", "contribution", None, 100.0, "fidelity_csv"),
        ("2025-03-04", "withdrawal", None, -0.19, "fidelity_csv"),
        ("2025-03-05", "dividend", None, 1.5, "fidelity_csv"),
        ("2025-03-06", "buy", 2.0, -100.0, "fidelity_csv"),
        ("2025-03-10", "contribution", None, 50.0, "fidelity_csv"),
        ("2025-03-10", "contribution", None, 50.0, "fidelity_csv"),
        ("2025-05-27", "other", 3.0, None, "fidelity_csv"),
        ("2025-11-03", "contribution", None, 200.0, "snaptrade"),
    ]
    # the same history downloaded again adds nothing: every repaired row keys the way the adapter now does
    csv = tmp_path / "History_SampleRoth_2025.csv"
    csv.write_text("\n".join([HEADER,
        f" 03/03/2025, {cc}, , , Cash, , 0.000, , , , 100.00, 100.00, ",
        " 03/04/2025, DIRECT DEBIT BANK ACCTVERIFY (Cash), , , Cash, , 0.000, , , , -0.19, 99.81, ",
        " 03/05/2025, DIVIDEND RECEIVED COCA COLA CO (KO) (Cash), KO, COCA COLA CO, Cash, , 0.000, , , , 1.50, 101.31, ",
        " 03/06/2025, YOU BOUGHT COCA COLA CO (KO) (Cash), KO, COCA COLA CO, Cash, 50.00, 2, , , , -100.00, 1.31, ",
        f" 03/10/2025, {cc}, , , Cash, , 0.000, , , , 50.00, 51.31, ",
        f" 03/10/2025, {cc}, , , Cash, , 0.000, , , , 50.00, 101.31, ",
        " 05/27/2025, DISTRIBUTION COCA COLA CO (KO) (Cash), KO, COCA COLA CO, Cash, , 3, , , , 150.00, 101.31, ",
        f" 11/03/2025, {cc}, , , Cash, , 0.000, , , , 200.00, 301.31, ",
    ]), encoding="utf-8")
    assert s.write_transactions(fh.parse(csv, account=ROTH.label)) == 0
    s.close()
