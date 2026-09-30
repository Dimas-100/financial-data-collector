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
