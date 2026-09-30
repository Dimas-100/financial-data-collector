from financial_data_collector.connections import base
from financial_data_collector.connections.redact import redact
from financial_data_collector.http import HttpError


def test_an_address_loses_its_user_name_and_password():
    text = "HTTP 403 for https://user:p%40ss@bridge.example.org/simplefin/accounts?x=1"
    assert redact(text) == "HTTP 403 for https://bridge.example.org/simplefin/accounts?x=1"


def test_every_form_of_a_secret_goes():
    out = redact("key abc+def/ghi= sent as clientId=abc%2Bdef%2Fghi%3D", ["abc+def/ghi="])
    assert out == "key *** sent as clientId=***"


def test_short_secrets_and_blanks_are_ignored_so_ordinary_words_survive():
    assert redact("the key is ab", ["ab", "", None]) == "the key is ab"


def test_longest_secret_first():
    assert redact("secret-longer secret", ["secret", "secret-longer"]) == "*** ***"


def test_num_and_nonzero():
    assert base.num("12.5") == 12.5 and base.num(None) is None and base.num("n/a") is None
    assert base.num(True) is None and base.num(float("nan")) is None
    assert base.nonzero(0) is None and base.nonzero("0.0") is None and base.nonzero("3") == 3.0


def test_unreachable_names_the_host_and_the_failure_type_only():
    e = HttpError(0, "https://x:secret@api.example.org/api/v1/accounts?clientId=k", "ConnectionError: boom for x")
    msg = str(base.unreachable("api.example.org", e))
    assert msg == "api.example.org couldn't be reached (ConnectionError)"
    assert "secret" not in msg and "clientId" not in msg
