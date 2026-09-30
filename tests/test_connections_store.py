from pathlib import Path

import pytest

from financial_data_collector.models import AccountRef, CashRow, PositionRow, Snapshot, TransactionRow
from financial_data_collector.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store.open(tmp_path / "w.db")
    yield s
    s.close()


def _bank(label: str, key: str, kind: str = "checking", confirmed: bool = False) -> AccountRef:
    return AccountRef(label, "other", "example_bank", kind, external_key=key, origin="simplefin",
                      kind_confirmed=confirmed, flows="balance")


def test_a_connected_account_is_known_by_its_key_and_its_label_is_fixed(store: Store):
    a = store.upsert_account(_bank("Example Bank Checking", "k1"), "2026-01-02")
    b = store.upsert_account(_bank("Example Bank Checking", "k2"), "2026-01-02")   # a second account, same name
    assert a != b
    labels = {r["external_key"]: r["label"] for r in store.query("SELECT external_key, label FROM accounts")}
    assert labels == {"k1": "Example Bank Checking", "k2": "Example Bank Checking 2"}
    # listed in the other order, with a renamed account: same rows, same labels
    assert store.upsert_account(_bank("Example Bank Everyday Checking", "k2"), "2026-01-03") == b
    assert store.upsert_account(_bank("Example Bank Checking", "k1"), "2026-01-03") == a
    assert {r["external_key"]: r["label"] for r in store.query("SELECT external_key, label FROM accounts")} == labels
    row = store.query("SELECT origin, flows, kind_confirmed, first_seen FROM accounts WHERE external_key = 'k1'")[0]
    assert (row["origin"], row["flows"], row["kind_confirmed"], row["first_seen"]) == ("simplefin", "balance", 0, "2026-01-02")


def test_a_file_account_with_the_same_label_keeps_it(store: Store):
    store.upsert_account(AccountRef("Sample Brokerage", "brokerage", "fidelity", "brokerage"), "2026-01-02")
    store.upsert_account(AccountRef("Sample Brokerage", external_key="k9", origin="snaptrade"), "2026-01-02")
    assert [r[0] for r in store.query("SELECT label FROM accounts ORDER BY id")] == ["Sample Brokerage", "Sample Brokerage 2"]


def test_a_guess_may_change_until_the_person_confirms(store: Store):
    store.upsert_account(_bank("Example Bank Everyday", "k1", "other"), "2026-01-02")
    store.upsert_account(_bank("Example Bank Everyday", "k1", "credit_card"), "2026-01-03")   # the balance went negative
    assert store.query("SELECT account_type FROM accounts")[0][0] == "credit_card"
    assert store.set_account("Example Bank Everyday", kind="checking")
    store.upsert_account(_bank("Example Bank Everyday", "k1", "credit_card"), "2026-01-04")
    row = store.query("SELECT account_type, kind_confirmed, slug FROM accounts")[0]
    assert (row[0], row[1], row[2]) == ("checking", 1, "other")
    # a kind the service itself supplied counts as confirmed too
    store.upsert_account(_bank("Example Broker Roth", "k2", "roth_ira", confirmed=True), "2026-01-02")
    store.upsert_account(_bank("Example Broker Roth", "k2", "other"), "2026-01-03")
    assert store.query("SELECT account_type, slug FROM accounts WHERE external_key = 'k2'")[0][0] == "roth_ira"


def test_limit_and_rate_survive_a_sync_and_bad_labels_are_reported(store: Store):
    store.upsert_account(_bank("Example Bank Visa", "k1", "credit_card"), "2026-01-02")
    assert store.set_account("Example Bank Visa", credit_limit=5000, rate_pct=24.9)
    store.upsert_account(_bank("Example Bank Visa", "k1", "credit_card"), "2026-01-03")
    row = store.query("SELECT credit_limit, rate_pct FROM accounts")[0]
    assert (row[0], row[1]) == (5000, 24.9)
    assert not store.set_account("Nobody", kind="checking")
    over = [dict(r) for r in store.accounts_overview()]
    assert over[0]["label"] == "Example Bank Visa" and over[0]["credit_limit"] == 5000 and over[0]["origin"] == "simplefin"


def test_cash_available_is_stored_and_a_debt_is_below_zero(store: Store):
    card = _bank("Example Bank Visa", "k1", "credit_card")
    store.write_snapshot(Snapshot("2026-01-02", "simplefin", [], [CashRow(card, -640.0, "USD", 4360.0)]))
    row = store.query("SELECT amount, available FROM cash_balances")[0]
    assert (row[0], row[1]) == (-640.0, 4360.0)
    assert store.query("SELECT total FROM account_values_daily")[0][0] == -640.0
    store.write_snapshot(Snapshot("2026-01-02", "simplefin", [], [CashRow(card, -600.0)]))
    assert [tuple(r) for r in store.query("SELECT amount, available FROM cash_balances")] == [(-600.0, None)]


def test_transactions_of_a_connected_account_are_keyed_by_its_stored_label(store: Store):
    ref = AccountRef("Example Broker Individual", "brokerage", "example_broker", "brokerage",
                     external_key="k1", origin="snaptrade")
    tx = TransactionRow(ref, "2026-01-05", "buy", "AAPL", 2.0, 100.0, -200.0, None, "BUY", "snaptrade")
    assert store.write_transactions([tx]) == 1
    renamed = AccountRef("Example Broker Renamed", "brokerage", "example_broker", "brokerage",
                         external_key="k1", origin="snaptrade")
    assert store.write_transactions([TransactionRow(renamed, "2026-01-05", "buy", "AAPL", 2.0, 100.0, -200.0, None,
                                                    "BUY", "snaptrade")]) == 0
    assert store.newest_activity_dates("snaptrade") == {"k1": "2026-01-05"}
    assert store.newest_activity_dates("simplefin") == {}


def test_connection_rows(store: Store):
    assert store.connection("snaptrade") is None and store.connections() == []
    store.save_connection("snaptrade", "ref1", "2026-01-02T10:00:00Z")
    store.mark_connection("snaptrade", fetched_at="2026-01-02T11:00:00Z", ok=False, error="boom")
    row = store.connection("snaptrade")
    assert row["key_ref"] == "ref1" and row["last_error"] == "boom" and row["last_ok_at"] is None
    store.mark_connection("snaptrade", fetched_at="2026-01-02T12:00:00Z", ok=True)
    row = store.connection("snaptrade")
    assert row["last_ok_at"] == "2026-01-02T12:00:00Z" and row["last_error"] == ""
    assert store.remove_connection("snaptrade", "2026-01-03T00:00:00Z") and not store.remove_connection("snaptrade", "x")
    assert store.connections(active_only=True) == [] and len(store.connections()) == 1
    store.save_connection("snaptrade", "ref2", "2026-01-04T00:00:00Z")   # connected again: active, clean
    row = store.connection("snaptrade")
    assert row["removed_at"] is None and row["key_ref"] == "ref2" and row["last_error"] == ""
    store.upsert_account(_bank("Example Bank Checking", "k1"), "2026-01-02")
    assert store.accounts_of("simplefin") == 1 and store.accounts_of("file") == 0


def test_positions_of_a_connected_account(store: Store):
    ref = AccountRef("Example Broker Individual", "brokerage", "example_broker", "brokerage",
                     external_key="k1", origin="snaptrade")
    snap = Snapshot("2026-01-02", "snaptrade", [PositionRow(ref, "AAPL", "APPLE", 3.0, 10.0, 30.0)], [CashRow(ref, 5.0)],
                    fetched_at="2026-01-02T05:00:00Z")
    assert store.write_snapshot(snap) == 2
    assert store.query("SELECT account, quantity FROM positions_latest")[0][1] == 3.0


def test_a_file_account_cannot_take_a_connected_accounts_label(store: Store):
    from financial_data_collector import ingest
    from financial_data_collector.store import RouteConflict
    connected = AccountRef("Sample Brokerage", "brokerage", "example_broker", "roth_ira", external_key="k1",
                           origin="snaptrade", kind_confirmed=True)
    store.upsert_account(connected, "2026-01-02")
    from_file = AccountRef("Sample Brokerage", "brokerage", "fidelity", "brokerage")
    with pytest.raises(RouteConflict) as e:
        store.write_snapshot(Snapshot("2026-01-03", "fidelity_csv", [], [CashRow(from_file, 1.0)]))
    assert "Sample Brokerage" in str(e.value) and "connection" in str(e.value)
    row = store.query("SELECT account_type, kind_confirmed, origin FROM accounts")[0]
    assert (row[0], row[1], row[2]) == ("roth_ira", 1, "snaptrade")
    assert store.query("SELECT COUNT(*) FROM accounts")[0][0] == 1
    from tests.conftest import FIXTURES
    result = ingest.ingest_file(store, FIXTURES / "History_SampleBrokerage_2026.csv", account="Sample Brokerage")
    assert result.skipped and "connection" in result.skipped
    assert store.query("SELECT COUNT(*) FROM transactions")[0][0] == 0


def test_a_kind_the_person_set_on_a_file_account_survives_an_import(store: Store):
    from_file = AccountRef("My Brokerage", "brokerage", "fidelity", "brokerage")
    store.upsert_account(from_file, "2026-01-02")
    assert store.set_account("My Brokerage", kind="traditional_ira")
    store.upsert_account(from_file, "2026-01-03")
    assert store.query("SELECT account_type FROM accounts")[0][0] == "traditional_ira"
    # an account first seen as 'other' still takes a better kind from a later file
    vague = AccountRef("Vague", "other", "unknown", "other")
    store.upsert_account(vague, "2026-01-02")
    store.upsert_account(AccountRef("Vague", "roth", "fidelity", "roth_ira"), "2026-01-03")
    assert store.query("SELECT account_type FROM accounts WHERE label = 'Vague'")[0][0] == "roth_ira"
