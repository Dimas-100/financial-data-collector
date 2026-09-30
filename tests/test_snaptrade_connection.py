"""The SnapTrade fetcher, against synthetic answers. No network."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from financial_data_collector.connections import snaptrade as st
from financial_data_collector.connections.base import ConnectionFailed
from financial_data_collector.connections.keys import SnapTradeKeys
from financial_data_collector.http import HttpError

NOW = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)   # 1767225600
KEYS = SnapTradeKeys("synthetic-client", "synthetic-consumer-key")
AUTH = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ROTH = "11111111-1111-4111-8111-111111111111"
EVERY = "22222222-2222-4222-8222-222222222222"
ACCOUNTS = [
    {"id": ROTH, "brokerage_authorization": AUTH, "name": "Sample Roth IRA (4321)", "number": "SYNTHETIC-NUMBER-0001",
     "institution_name": "Example Brokerage", "raw_type": "Roth IRA", "meta": {"type": "Roth IRA"}},
    {"id": EVERY, "brokerage_authorization": AUTH, "name": "Everyday ...9876", "number": "SYNTHETIC-NUMBER-0002",
     "institution_name": "Example Brokerage", "raw_type": None, "meta": {}},
]
AUTHS = [{"id": AUTH, "disabled": False, "brokerage": {"name": "Example Brokerage"}}]
POSITIONS = {
    ROTH: [
        {"symbol": {"symbol": {"symbol": "AAPL", "raw_symbol": "AAPL", "description": "APPLE INC",
                               "currency": {"code": "USD"}}}, "units": 3, "price": 100.0,
         "average_purchase_price": 90.0, "open_pnl": 30.0},
        {"symbol": {"symbol": {"symbol": "SPAXX", "raw_symbol": "SPAXX", "description": "FIDELITY GOVERNMENT MONEY MARKET"}},
         "units": 12, "price": 1.0},
        {"symbol": {"symbol": {"symbol": "BRKB", "raw_symbol": "BRK.B", "description": "BERKSHIRE"}}, "units": 1, "price": 400},
    ],
    EVERY: [],
}
BALANCES = {ROTH: [{"currency": {"code": "USD"}, "cash": 12.5}], EVERY: []}
ACTIVITY = {
    ROTH: [{"trade_date": "2025-12-30T00:00:00Z", "settlement_date": "2026-01-02", "type": "BUY",
            "symbol": {"symbol": "AAPL", "raw_symbol": "AAPL"}, "units": 1, "price": 99.0, "amount": -99.0, "fee": 0},
           {"trade_date": "2025-12-15T00:00:00Z", "type": "CONTRIBUTION", "amount": 500.0},
           {"trade_date": None, "type": "BUY"}],
    EVERY: [],
}


def _fetch(calls, *, fail=None, pages=None):
    def fetch(url, headers):
        parts = urlsplit(url)
        calls.append((parts.path, parse_qs(parts.query), headers))
        assert parts.scheme == "https" and parts.netloc == st.HOST
        if fail and fail(parts.path):
            raise fail(parts.path)
        path = parts.path.removeprefix(st.API)
        if path == "/accounts":
            return json.dumps(ACCOUNTS).encode()
        if path == "/authorizations":
            return json.dumps(AUTHS).encode()
        m = re.fullmatch(r"/accounts/([^/]+)/(positions|balances|activities)", path)
        assert m, path
        acct, what = m.group(1), m.group(2)
        if what == "positions":
            return json.dumps(POSITIONS[acct]).encode()
        if what == "balances":
            return json.dumps(BALANCES[acct]).encode()
        offset = int(parse_qs(parts.query).get("offset", ["0"])[0])
        rows = (pages or {}).get(acct, ACTIVITY[acct])
        return json.dumps({"data": rows[offset:offset + st.PAGE], "pagination": {"offset": offset}}).encode()
    return fetch


def test_signature_is_the_documented_one():
    query = "clientId=synthetic-client&timestamp=1767225600"
    assert st.sign("/api/v1/accounts", query, "synthetic-consumer-key") == "lB+X4H9882KWtumdGiKt8MjTCIW5fW1u1PfSdxfzNlk="


def test_every_request_is_a_signed_get_to_a_read_path_with_no_user_for_a_personal_key():
    calls = []
    st.fetch_all(KEYS, fetch=_fetch(calls), now=NOW)
    assert calls, "no requests were made"
    for path, query, headers in calls:
        assert path.startswith(st.API)
        assert query["clientId"] == ["synthetic-client"] and query["timestamp"] == ["1767225600"]
        assert "userId" not in query and "userSecret" not in query
        assert headers["Signature"] and headers["Accept"] == "application/json"
    paths = {re.sub(r"/accounts/[^/]+/", "/accounts/{id}/", p.removeprefix(st.API)) for p, _, _ in calls}
    assert paths <= set(st.READ_PATHS)


def test_a_key_with_a_registered_user_sends_it():
    calls = []
    st.fetch_accounts(SnapTradeKeys("c", "k", "user-1", "user-secret"), fetch=_fetch(calls), now=NOW)
    assert calls[0][1]["userId"] == ["user-1"] and calls[0][1]["userSecret"] == ["user-secret"]


def test_the_module_only_names_read_paths():
    src = Path(st.__file__).read_text(encoding="utf-8")
    literal = {re.sub(r"\{[a-z_]+\}", "{id}", m) for m in re.findall(r'"(/(?:accounts|authorizations)[^"]*)"', src)}
    assert literal == set(st.READ_PATHS)
    assert "post" not in dir(st) and "orders" not in src and "trade/" not in src


def test_accounts_positions_cash_and_activity_are_mapped():
    out = st.fetch_all(KEYS, fetch=_fetch([]), now=NOW)
    assert [a.label for a in out.accounts] == ["Example Brokerage Sample Roth IRA", "Example Brokerage Everyday"]
    roth, every = out.accounts
    assert roth.account_type == "roth_ira" and roth.kind_confirmed and roth.slug == "roth" and roth.origin == "snaptrade"
    assert every.account_type == "brokerage" and not every.kind_confirmed and every.flows == "transactions"
    assert roth.institution == "example_brokerage" and roth.external_key != every.external_key
    assert "SYNTHETIC-NUMBER" not in repr(out) and ROTH not in repr(out)
    snap = out.snapshots[0]
    assert snap.source == "snaptrade" and snap.as_of_date == "2026-01-01" and snap.fetched_at == "2026-01-01T00:00:00Z"
    by_symbol = {p.symbol: p for p in snap.positions}
    assert set(by_symbol) == {"AAPL", "BRK.B"}                       # the money market fund is cash, not a holding
    assert by_symbol["AAPL"].market_value == 300.0 and by_symbol["AAPL"].cost_basis_total == 270.0
    assert by_symbol["AAPL"].description == "APPLE INC" and by_symbol["AAPL"].unrealized_pnl == 30.0
    cash = {(c.account.label, c.amount) for c in snap.cash}
    assert cash == {("Example Brokerage Sample Roth IRA", 12.5), ("Example Brokerage Everyday", 0.0)}  # no balance: a 0 row
    assert [(t.trade_date, t.type, t.symbol, t.amount) for t in out.transactions] == [
        ("2025-12-30", "buy", "AAPL", -99.0), ("2025-12-15", "contribution", None, 500.0)]
    assert out.transactions[0].settlement_date == "2026-01-02" and out.transactions[0].source == "snaptrade"
    assert out.notes == []


def test_activity_starts_seven_days_before_the_newest_stored_row_and_pages():
    calls = []
    roth_key = st.fetch_accounts(KEYS, fetch=_fetch([]), now=NOW)[0].external_key
    many = [{"trade_date": "2025-12-01T00:00:00Z", "type": "DIVIDEND", "amount": 1.0}] * (st.PAGE + 5)
    out = st.fetch_all(KEYS, fetch=_fetch(calls, pages={ROTH: many}), now=NOW, since={roth_key: "2025-12-20"})
    activity_calls = [(p, q) for p, q, _ in calls if p.endswith("/activities") and ROTH in p]
    assert [q.get("startDate") for _, q in activity_calls] == [["2025-12-13"], ["2025-12-13"]]
    assert [q["offset"] for _, q in activity_calls] == [["0"], [str(st.PAGE)]]
    assert len([t for t in out.transactions if t.type == "dividend"]) == st.PAGE + 5
    first_calls = [(p, q) for p, q, _ in calls if p.endswith("/activities") and EVERY in p]
    assert "startDate" not in first_calls[0][1]


def test_a_refused_key_and_an_unreachable_host_are_worded_without_the_key():
    def refused(path):
        return HttpError(401, "https://x?clientId=synthetic-client&userSecret=s3cret")
    with pytest.raises(ConnectionFailed) as e:
        st.fetch_accounts(KEYS, fetch=_fetch([], fail=refused), now=NOW)
    assert str(e.value) == st.REFUSED and "s3cret" not in str(e.value)

    def down(path):
        return HttpError(0, "https://x?clientId=synthetic-client", "ConnectionError: boom")
    with pytest.raises(ConnectionFailed) as e:
        st.fetch_accounts(KEYS, fetch=_fetch([], fail=down), now=NOW)
    assert str(e.value) == "api.snaptrade.com couldn't be reached (ConnectionError)"

    def other(path):
        return HttpError(500, "https://x")
    with pytest.raises(ConnectionFailed) as e:
        st.fetch_accounts(KEYS, fetch=_fetch([], fail=other), now=NOW)
    assert str(e.value) == "SnapTrade answered 500"


def test_not_json_is_reported():
    with pytest.raises(ConnectionFailed) as e:
        st.fetch_accounts(KEYS, fetch=lambda url, headers: b"<html>sign in</html>", now=NOW)
    assert str(e.value) == "SnapTrade sent something that isn't JSON"


def test_one_account_failing_skips_only_it():
    def flaky(path):
        return HttpError(500, "https://x") if path.endswith(f"/accounts/{ROTH}/positions") else None
    out = st.fetch_all(KEYS, fetch=_fetch([], fail=flaky), now=NOW)
    assert [a.label for a in out.accounts] == ["Example Brokerage Sample Roth IRA", "Example Brokerage Everyday"]
    assert {c.account.label for c in out.snapshots[0].cash} == {"Example Brokerage Everyday"}
    assert out.notes == ["Example Brokerage Sample Roth IRA: SnapTrade answered 500; skipped"]


def test_a_disabled_login_is_named():
    def fetch(url, headers):
        if url.endswith("/authorizations") or "/authorizations?" in url:
            return json.dumps([{"id": AUTH, "disabled": True, "brokerage": {"name": "Example Brokerage"}}]).encode()
        return _fetch([])(url, headers)
    out = st.fetch_all(KEYS, fetch=fetch, now=NOW)
    assert "Example Brokerage needs reconnecting on SnapTrade's site" in out.notes
