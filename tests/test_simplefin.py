"""The SimpleFIN fetcher, against synthetic answers. No network."""
import base64
import json
from datetime import datetime, timezone
from urllib.parse import urlsplit

import pytest

from financial_data_collector.connections import simplefin as sf
from financial_data_collector.connections.base import ConnectionFailed
from financial_data_collector.connections.keys import SimpleFinKey
from financial_data_collector.http import HttpError

NOW = datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc)
ACCESS = "https://user%40x:p%40ss@bridge.example.org/simplefin"
KEY = SimpleFinKey(ACCESS)
ANSWER = {
    "errlist": [{"code": "con.auth", "msg": "Example Credit Union needs your attention", "conn_id": "C2"}],
    "connections": [{"conn_id": "C1", "name": "Example Bank", "org_id": "o1", "sfin_url": "https://x"},
                    {"conn_id": "C2", "name": "Example Credit Union", "org_id": "o2", "sfin_url": "https://y"}],
    "accounts": [
        {"id": "ACT-1", "conn_id": "C1", "name": "Everyday Checking ...1234", "currency": "USD", "balance": "1500.25",
         "available-balance": "1450.00", "balance-date": 1767355200},       # 2026-01-02 12:00Z
        {"id": "ACT-2", "conn_id": "C1", "name": "Cash Rewards Visa ...5678", "currency": "USD", "balance": "-640.00",
         "available-balance": "4360.00", "balance-date": 1767441600},       # 2026-01-03 12:00Z
        {"id": "ACT-3", "conn_id": "C2", "name": "Share Savings", "currency": "USD", "balance": None, "balance-date": 1767441600},
        {"id": "ACT-4", "conn_id": "C1", "name": "Everyday Checking ...9999", "currency": "usd", "balance": "10", "balance-date": 0},
    ],
}


def _fetch(calls, answer=ANSWER, *, fail=None):
    def fetch(url, headers):
        calls.append((url, headers))
        if fail:
            raise fail
        return json.dumps(answer).encode()
    return fetch


def test_claim_exchanges_a_token_once():
    posts = []

    def post(url, headers):
        posts.append((url, headers))
        return 200, b"https://u:p@bridge.example.org/simplefin\n"

    token = base64.b64encode(b"https://bridge.example.org/simplefin/claim/abc").decode()
    assert sf.claim(f"  {token}\n", post=post) == "https://u:p@bridge.example.org/simplefin"
    assert posts == [("https://bridge.example.org/simplefin/claim/abc", {"Content-Length": "0"})]


@pytest.mark.parametrize("token, message", [
    ("not base64!!", "this doesn't look like a SimpleFIN setup token: copy it again from SimpleFIN's site"),
    (base64.b64encode(b"http://bridge.example.org/claim/abc").decode(), "this doesn't look like a SimpleFIN setup token: copy it again from SimpleFIN's site"),
])
def test_claim_refuses_a_bad_token_before_spending_anything(token, message):
    with pytest.raises(ConnectionFailed) as e:
        sf.claim(token, post=lambda url, headers: (200, b"https://u:p@h/x"))
    assert str(e.value) == message


def test_claim_reports_a_used_token_and_a_bad_answer_without_retrying():
    token = base64.b64encode(b"https://bridge.example.org/simplefin/claim/abc").decode()
    calls = []

    def used(url, headers):
        calls.append(url)
        return 403, b"used"
    with pytest.raises(ConnectionFailed) as e:
        sf.claim(token, post=used)
    assert str(e.value) == sf.USED and len(calls) == 1
    with pytest.raises(ConnectionFailed) as e:
        sf.claim(token, post=lambda url, headers: (200, b"https://bridge.example.org/no-credentials"))
    assert str(e.value) == "SimpleFIN's answer wasn't an access address"
    with pytest.raises(ConnectionFailed) as e:
        sf.claim(token, post=lambda url, headers: (500, b""))
    assert str(e.value) == "SimpleFIN answered 500 to the setup token"

    def down(url, headers):
        raise HttpError(0, url, "ConnectionError")
    with pytest.raises(ConnectionFailed) as e:
        sf.claim(token, post=down)
    assert str(e.value) == "bridge.example.org couldn't be reached (ConnectionError)"


def test_one_balances_only_request_with_basic_auth_and_no_credentials_in_the_address():
    calls = []
    sf.fetch_all(KEY, fetch=_fetch(calls), now=NOW)
    assert len(calls) == 1
    url, headers = calls[0]
    parts = urlsplit(url)
    assert parts.username is None and parts.password is None
    assert url == "https://bridge.example.org/simplefin/accounts?balances-only=1&version=2"
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"user@x:p@ss").decode()


def test_accounts_become_balances_by_day_with_kinds_guessed():
    out = sf.fetch_all(KEY, fetch=_fetch([]), now=NOW)
    by_label = {a.label: a for a in out.accounts}
    assert set(by_label) == {"Example Bank Everyday Checking", "Example Bank Cash Rewards Visa"}
    checking, visa = by_label["Example Bank Everyday Checking"], by_label["Example Bank Cash Rewards Visa"]
    assert checking.account_type == "checking" and visa.account_type == "credit_card"
    assert not checking.kind_confirmed and checking.flows == "balance" and checking.origin == "simplefin"
    assert checking.institution == "example_bank" and checking.external_key != visa.external_key
    assert "ACT-1" not in repr(out) and "1234" not in repr(out)
    days = {s.as_of_date: s for s in out.snapshots}
    assert set(days) == {"2026-01-02", "2026-01-03"}
    assert days["2026-01-02"].source == "simplefin" and days["2026-01-02"].positions == []
    c = days["2026-01-02"].cash[0]
    assert (c.account.label, c.amount, c.available, c.currency) == ("Example Bank Everyday Checking", 1500.25, 1450.0, "USD")
    v = days["2026-01-03"].cash
    assert [(x.account.label, x.amount, x.available) for x in v] == [("Example Bank Cash Rewards Visa", -640.0, 4360.0),
                                                                     ("Example Bank Everyday Checking", 10.0, None)]
    assert out.notes == ["Example Credit Union Share Savings: no balance in SimpleFIN's answer; skipped",
                         "Example Credit Union needs attention on SimpleFIN's site: Example Credit Union needs your attention"]


def test_two_accounts_with_one_name_are_two_accounts():
    out = sf.fetch_all(KEY, fetch=_fetch([]), now=NOW)
    keys = [a.external_key for a in out.accounts if a.label == "Example Bank Everyday Checking"]
    assert len(keys) == 2 and keys[0] != keys[1]   # the store gives the second its " 2"


def test_a_v1_answer_still_reads():
    v1 = {"errors": ["Example Bank: please sign in again"],
          "accounts": [{"id": "A", "org": {"name": "Example Bank", "domain": "bank.example"}, "name": "Savings",
                        "currency": "USD", "balance": "5", "balance-date": 1767441600}]}
    out = sf.fetch_all(KEY, fetch=_fetch([], v1), now=NOW)
    assert out.accounts[0].label == "Example Bank Savings" and out.accounts[0].account_type == "savings"
    assert out.notes == ["SimpleFIN: Example Bank: please sign in again"]


def test_no_kind_in_the_name_falls_back_on_the_sign():
    answer = {"accounts": [{"id": "A", "conn_id": "C", "name": "Everyday", "currency": "USD", "balance": "-5", "balance-date": 1767441600},
                           {"id": "B", "conn_id": "C", "name": "Everyday", "currency": "EUR", "balance": "5", "balance-date": 1767441600}],
              "connections": [{"conn_id": "C", "name": "Example Bank"}], "errlist": []}
    out = sf.fetch_all(KEY, fetch=_fetch([], answer), now=NOW)
    assert [a.account_type for a in out.accounts] == ["credit_card", "other"]
    assert out.snapshots[0].cash[1].currency == "EUR"


def test_refused_lapsed_unreachable_and_not_json():
    for fail, message in [(HttpError(403, "https://u:p@h/x"), sf.REFUSED), (HttpError(402, "https://h"), sf.LAPSED),
                          (HttpError(0, "https://u:p@h/x", "ConnectionError: x"), "bridge.example.org couldn't be reached (ConnectionError)"),
                          (HttpError(500, "https://h"), "SimpleFIN answered 500")]:
        with pytest.raises(ConnectionFailed) as e:
            sf.fetch_all(KEY, fetch=_fetch([], fail=fail), now=NOW)
        assert str(e.value) == message
    with pytest.raises(ConnectionFailed) as e:
        sf.fetch_all(KEY, fetch=lambda url, headers: b"<html>", now=NOW)
    assert str(e.value) == "SimpleFIN sent something that isn't JSON"
