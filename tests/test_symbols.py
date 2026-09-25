import pytest

from financial_data_collector import symbols as S


@pytest.mark.parametrize(
    "raw, want",
    [
        ("brk/b", "BRK.B"),
        ("BRKB", "BRK.B"),
        ("BRK-B", "BRK.B"),
        ("BRK.B", "BRK.B"),
        (" aapl ", "AAPL"),
        ("FXAIX", "FXAIX"),
        ("SPAXX**", "SPAXX"),
    ],
)
def test_canonical(raw, want):
    assert S.canonical(raw) == want


def test_provider_forms():
    assert S.to_tiingo("BRK.B") == "BRK-B"
    assert S.to_yfinance("BRK.B") == "BRK-B"
    assert S.to_sec("BRK.B") == "BRK-B"
    assert S.to_tiingo("AAPL") == "AAPL"


def test_money_market():
    assert S.is_money_market("SPAXX")
    assert S.is_money_market("spaxx**")
    assert S.is_money_market("XYZ", "FIDELITY GOVERNMENT MONEY MARKET")
    assert not S.is_money_market("AAPL", "APPLE INC")
