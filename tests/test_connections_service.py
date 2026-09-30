"""fdc connect, disconnect and the sync step, with fake services. No network."""
import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from financial_data_collector.connections import keys as K
from financial_data_collector.connections import service
from financial_data_collector.connections.base import ConnectionFailed
from financial_data_collector.http import HttpError
from financial_data_collector.models import AccountRef
from financial_data_collector.store import Store
from tests.test_simplefin import ACCESS, ANSWER
from tests.test_snaptrade_connection import _fetch as snaptrade_fetch

NOW = datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc)
TOKEN = base64.b64encode(b"https://bridge.example.org/simplefin/claim/abc").decode()


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store.open(tmp_path / "w.db")
    yield s
    s.close()


@pytest.fixture
def home(tmp_path: Path) -> K.KeyHome:
    return K.KeyHome(K.MemoryKeyStore(), K.EnvFile(tmp_path / ".env", environ={}))


def _post(url, headers):
    return 200, ACCESS.encode()


def _both(url, headers):
    """One fake fetch that answers for both services."""
    if "simplefin" in url:
        return json.dumps(ANSWER).encode()
    return snaptrade_fetch([])(url, headers)


def test_connect_snaptrade_checks_saves_and_lists(store: Store, home: K.KeyHome):
    accounts, notes = service.connect_snaptrade(store, home, K.SnapTradeKeys("c", "k"), fetch=_both, now=NOW)
    assert [a.label for a in accounts] == ["Example Brokerage Sample Roth IRA", "Example Brokerage Everyday"]
    row = store.connection("snaptrade")
    assert row and row["key_ref"] != K.ENV_REF and home.load("snaptrade", row["key_ref"]) == K.SnapTradeKeys("c", "k")
    assert store.accounts_of("snaptrade") == 2 and notes == []


def test_a_failed_check_leaves_the_old_key(store: Store, home: K.KeyHome):
    service.connect_snaptrade(store, home, K.SnapTradeKeys("c", "k"), fetch=_both, now=NOW)
    old_ref = store.connection("snaptrade")["key_ref"]

    def refused(url, headers):
        raise HttpError(401, url)
    with pytest.raises(ConnectionFailed):
        service.connect_snaptrade(store, home, K.SnapTradeKeys("c2", "k2"), fetch=refused, now=NOW)
    assert store.connection("snaptrade")["key_ref"] == old_ref
    assert home.load("snaptrade", old_ref) == K.SnapTradeKeys("c", "k")
    # a good replacement forgets the old entry
    service.connect_snaptrade(store, home, K.SnapTradeKeys("c3", "k3"), fetch=_both, now=NOW)
    new_ref = store.connection("snaptrade")["key_ref"]
    assert new_ref != old_ref and home.store.get(old_ref) is None


def test_connect_simplefin_saves_the_address_the_moment_it_has_it(store: Store, home: K.KeyHome):
    def broken_after_claim(url, headers):
        raise HttpError(500, url)
    with pytest.raises(ConnectionFailed) as e:
        service.connect_simplefin(store, home, TOKEN, post=_post, fetch=broken_after_claim, now=NOW)
    assert str(e.value) == "SimpleFIN answered 500"
    row = store.connection("simplefin")
    assert row and home.load("simplefin", row["key_ref"]) == K.SimpleFinKey(ACCESS)   # the token is spent: the address is kept
    accounts, notes = service.connect_simplefin(store, home, TOKEN, post=_post, fetch=_both, now=NOW)
    assert len(accounts) == 3 and any("needs attention" in n for n in notes)   # ACT-1, ACT-2, ACT-4; ACT-3 has no balance


def test_connect_warns_when_file_accounts_exist(store: Store, home: K.KeyHome):
    store.upsert_account(AccountRef("Sample Brokerage", "brokerage", "fidelity", "brokerage"), "2026-01-02")
    _, notes = service.connect_snaptrade(store, home, K.SnapTradeKeys("c", "k"), fetch=_both, now=NOW)
    assert service.DOUBLE_COUNT in notes


def test_disconnect_forgets_the_key_and_keeps_the_rows(store: Store, home: K.KeyHome):
    service.connect_snaptrade(store, home, K.SnapTradeKeys("c", "k"), fetch=_both, now=NOW)
    ref = store.connection("snaptrade")["key_ref"]
    assert service.disconnect(store, home, "snaptrade", now=NOW)
    assert home.store.get(ref) is None and store.connections(active_only=True) == []
    assert store.accounts_of("snaptrade") == 2
    assert not service.disconnect(store, home, "snaptrade", now=NOW) and not service.disconnect(store, home, "nope", now=NOW)


def test_run_fetches_every_connection_and_writes(store: Store, home: K.KeyHome):
    service.connect_snaptrade(store, home, K.SnapTradeKeys("c", "k"), fetch=_both, now=NOW)
    service.connect_simplefin(store, home, TOKEN, post=_post, fetch=_both, now=NOW)
    rows, message, status = service.run(store, home, min_hours=6, fetch=_both, now=NOW)
    assert status == "ok" and rows > 0
    assert message.startswith("simplefin: 3 accounts") and "snaptrade: 2 accounts" in message
    assert "needs attention" in message
    assert store.query("SELECT COUNT(*) FROM position_snapshots")[0][0] == 2
    assert store.query("SELECT COUNT(*) FROM cash_balances")[0][0] == 5     # 2 SnapTrade + 3 SimpleFIN rows
    assert store.query("SELECT COUNT(*) FROM transactions")[0][0] == 2
    for name in ("snaptrade", "simplefin"):
        row = store.connection(name)
        assert row["last_ok_at"] == "2026-01-03T12:00:00Z" and row["last_error"] == ""
    # too soon: nothing fetched, nothing changed, still ok
    later = NOW + timedelta(hours=2)
    rows2, message2, status2 = service.run(store, home, min_hours=6, fetch=_both, now=later)
    assert (rows2, status2) == (0, "ok") and "fetched 2 hours ago" in message2
    # after min_hours: fetched again with nothing new, no data row changes
    counts = store.counts()
    rows3, _, status3 = service.run(store, home, min_hours=6, fetch=_both, now=NOW + timedelta(hours=7))
    assert status3 == "ok"
    same = {k: v for k, v in store.counts().items() if k != "sync_runs"}
    assert same == {k: v for k, v in counts.items() if k != "sync_runs"}


def test_run_isolates_a_failing_connection_and_redacts(store: Store, home: K.KeyHome):
    service.connect_snaptrade(store, home, K.SnapTradeKeys("client-id-value", "consumer-key-value"), fetch=_both, now=NOW)
    service.connect_simplefin(store, home, TOKEN, post=_post, fetch=_both, now=NOW)

    def snaptrade_down(url, headers):
        if "snaptrade" in url:
            raise HttpError(0, url, "ConnectionError: client-id-value consumer-key-value")
        return _both(url, headers)
    rows, message, status = service.run(store, home, min_hours=6, fetch=snaptrade_down, now=NOW)
    assert status == "ok" and rows > 0
    assert "snaptrade: api.snaptrade.com couldn't be reached (ConnectionError)" in message
    assert "client-id-value" not in message and "consumer-key-value" not in message
    row = store.connection("snaptrade")
    assert row["last_error"].startswith("api.snaptrade.com couldn't be reached") and row["last_ok_at"] is None
    assert store.connection("simplefin")["last_ok_at"] == "2026-01-03T12:00:00Z"

    def everything_down(url, headers):
        raise HttpError(0, url, "ConnectionError: x")
    _, _, status = service.run(store, home, min_hours=0, fetch=everything_down, now=NOW + timedelta(hours=1))
    assert status == "error"


def test_run_with_no_connections_is_skipped_and_env_keys_count(store: Store, tmp_path: Path):
    home = K.KeyHome(K.MemoryKeyStore(), K.EnvFile(tmp_path / ".env", environ={}))
    assert service.run(store, home, min_hours=6, fetch=_both, now=NOW) == (
        0, "no connections: run fdc connect snaptrade or fdc connect simplefin", "skipped")
    env_home = K.KeyHome(K.MemoryKeyStore(), K.EnvFile(tmp_path / ".env", environ={"FDC_SIMPLEFIN_ACCESS_URL": ACCESS}))
    rows, message, status = service.run(store, env_home, min_hours=6, fetch=_both, now=NOW)
    assert status == "ok" and store.connection("simplefin")["key_ref"] == K.ENV_REF


def test_a_missing_key_is_a_message_not_a_crash(store: Store, home: K.KeyHome):
    store.save_connection("snaptrade", "gone", "2026-01-01T00:00:00Z")
    rows, message, status = service.run(store, home, min_hours=6, fetch=_both, now=NOW)
    assert status == "error" and message == "snaptrade: no SnapTrade key saved: run fdc connect snaptrade"


def test_an_unexpected_error_is_reported_and_redacted(store: Store, home: K.KeyHome):
    service.connect_snaptrade(store, home, K.SnapTradeKeys("client-id-value", "consumer-key-value"), fetch=_both, now=NOW)

    def odd(url, headers):
        raise ValueError("boom client-id-value")
    rows, message, status = service.run(store, home, min_hours=6, fetch=odd, now=NOW)
    assert status == "error" and message == "snaptrade: ValueError: boom ***"


def test_an_account_that_vanishes_keeps_its_rows(store: Store, home: K.KeyHome):
    service.connect_simplefin(store, home, TOKEN, post=_post, fetch=_both, now=NOW)
    service.run(store, home, min_hours=0, fetch=_both, now=NOW)
    before = store.query("SELECT COUNT(*) FROM cash_balances")[0][0]
    only_first = dict(ANSWER["accounts"][0], **{"balance-date": 1767528000})   # 2026-01-04 12:00Z
    fewer = dict(ANSWER, accounts=[only_first])
    rows, message, status = service.run(store, home, min_hours=0, fetch=lambda u, h: json.dumps(fewer).encode(),
                                        now=NOW + timedelta(days=1))
    assert status == "ok"
    assert store.query("SELECT COUNT(*) FROM cash_balances")[0][0] == before + 1
    assert store.accounts_of("simplefin") == 3   # nothing is deleted


def test_overview(store: Store, home: K.KeyHome):
    service.connect_snaptrade(store, home, K.SnapTradeKeys("c", "k"), fetch=_both, now=NOW)
    ov = service.overview(store)
    assert [o.name for o in ov] == ["snaptrade"] and ov[0].accounts == 2 and ov[0].last_ok_at is None


def test_reconnecting_after_a_failure_fetches_on_the_next_sync(store: Store, home: K.KeyHome):
    service.connect_snaptrade(store, home, K.SnapTradeKeys("c", "k"), fetch=_both, now=NOW)

    def refused(url, headers):
        raise HttpError(401, url)
    _, message, status = service.run(store, home, min_hours=6, fetch=refused, now=NOW)
    assert status == "error" and "refused the key" in message
    # a failed fetch is retried on the next sync, not throttled for min_hours
    calls = []
    def counting(url, headers):
        calls.append(url)
        return _both(url, headers)
    _, message, status = service.run(store, home, min_hours=6, fetch=counting, now=NOW + timedelta(minutes=5))
    assert status == "ok" and calls and "fetched" not in message
    # and a new key is a fresh connection: fetched right away even after a recent success
    service.connect_snaptrade(store, home, K.SnapTradeKeys("c2", "k2"), fetch=_both, now=NOW + timedelta(minutes=6))
    calls.clear()
    _, message, status = service.run(store, home, min_hours=6, fetch=counting, now=NOW + timedelta(minutes=7))
    assert status == "ok" and calls and "fetched" not in message
    # a recent success still throttles
    calls.clear()
    _, message, status = service.run(store, home, min_hours=6, fetch=counting, now=NOW + timedelta(minutes=8))
    assert status == "ok" and not calls and "fetched 1 minute ago" in message
