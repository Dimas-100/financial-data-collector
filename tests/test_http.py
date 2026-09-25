import pytest

from financial_data_collector import http


class _Resp:
    def __init__(self, status, content=b"ok"):
        self.status_code = status
        self.content = content


def _get_factory(statuses):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers, timeout))
        return _Resp(statuses.pop(0))

    fake_get.calls = calls
    return fake_get


def test_retries_then_succeeds(monkeypatch):
    fake = _get_factory([429, 503, 200])
    monkeypatch.setattr(http.requests, "get", fake)
    slept = []
    out = http.fetch("https://x/y", {"User-Agent": "t"}, sleep=slept.append)
    assert out == b"ok"
    assert len(fake.calls) == 3 and slept == [1, 2]
    assert fake.calls[0][1] == {"User-Agent": "t"} and fake.calls[0][2] == 30


def test_404_is_final(monkeypatch):
    fake = _get_factory([404, 200])
    monkeypatch.setattr(http.requests, "get", fake)
    with pytest.raises(http.HttpError) as e:
        http.fetch("https://x/y", sleep=lambda s: None)
    assert e.value.status == 404 and len(fake.calls) == 1


def test_gives_up_after_retries(monkeypatch):
    fake = _get_factory([500, 500, 500])
    monkeypatch.setattr(http.requests, "get", fake)
    with pytest.raises(http.HttpError) as e:
        http.fetch("https://x/y", sleep=lambda s: None)
    assert e.value.status == 500 and len(fake.calls) == 3
