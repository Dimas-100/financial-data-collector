import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture(autouse=True)
def no_real_key_store(monkeypatch):
    """No test ever touches this computer's key store or reads a real FDC_* variable: the key store KeyHome
    opens becomes an in-memory one, and the connection variables are cleared."""
    from financial_data_collector.connections import keys as K

    fake = K.MemoryKeyStore()
    monkeypatch.setattr(K, "os_key_store", lambda: fake)
    for name in list(os.environ):
        if name.startswith("FDC_"):
            monkeypatch.delenv(name, raising=False)
    return fake


class _NoOutside:
    """What the suite's outside source knows: nothing."""

    def quote(self, symbol, on):
        return None

    def figures(self, symbol):
        return None


@pytest.fixture(autouse=True)
def no_real_outside_source(monkeypatch):
    """No test reaches Yahoo: the checker's default outside source answers None for everything. A test that
    exercises the outside checks passes its own fake through `outside=`."""
    from financial_data_collector.check import outside

    monkeypatch.setattr(outside, "default_source", lambda: _NoOutside())


class _NoYahoo:
    """What the suite's Yahoo client knows: nothing."""

    def fund_info(self, symbol):
        return None


@pytest.fixture(autouse=True)
def no_real_yahoo(monkeypatch):
    """No test reaches Yahoo through a sync: `collectors.yahoo.default_client` answers None for everything. A test
    that exercises a collector passes its own fake through `yahoo=`."""
    from financial_data_collector.collectors import yahoo

    monkeypatch.setattr(yahoo, "default_client", lambda: _NoYahoo())
