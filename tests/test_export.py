import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from financial_data_collector import statements
from financial_data_collector.collectors.sec_facts import parse_company_facts
from financial_data_collector.export import cockpit as E
from financial_data_collector.models import PriceBar
from financial_data_collector.store import Store

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)
UNIVERSE = ["AAPL", "KO", "VTI"]


@pytest.fixture
def store(tmp_path: Path, fixtures: Path):
    s = Store.open(tmp_path / "w.db")
    facts = parse_company_facts(json.loads((fixtures / "sec" / "companyfacts_SAMPLE.json").read_text()))
    s.upsert_security("AAPL", "APPLE INC", "stock", "2026-01-01")
    s.set_security_cik("AAPL", "0000000001", "Sample Corp")
    s.write_sec_facts("0000000001", facts)
    s.replace_line_items("0000000001", statements.rebuild(facts, s.concept_rules()))
    s.upsert_security("KO", "COCA COLA CO", "stock", "2026-01-01")
    s.upsert_security("VTI", "VANGUARD TOTAL STOCK MARKET ETF", "etf", "2026-01-01")
    s.write_prices("AAPL", [PriceBar("2026-01-02", 100.0, 99.5, 1.0, 1.0), PriceBar("2026-01-05", 51.0, 50.9, 0.0, 2.0),
                            PriceBar("2026-01-06", 52.0, 52.0, 0.25, 1.0)], "tiingo")
    s.write_prices("KO", [PriceBar("2026-01-02", 60.0, 59.0)], "yfinance")
    yield s
    s.close()


def test_prices_payload(store: Store):
    p = E.build_prices(store, UNIVERSE, NOW)
    assert set(p) == {"as_of", "source", "lookback_days", "sources", "by_ticker", "misses"}
    assert p["as_of"] == "2026-03-01T00:00:00+00:00" and p["lookback_days"] == 1826
    assert p["misses"] == ["VTI"] and p["sources"] == {"AAPL": "tiingo", "KO": "yfinance"}
    assert p["by_ticker"]["AAPL"] == [{"d": "2026-01-02", "c": 99.5}, {"d": "2026-01-05", "c": 50.9}, {"d": "2026-01-06", "c": 52.0}]
    assert "VTI" not in p["by_ticker"]


def test_dividends_split_adjusted(store: Store):
    d = E.build_dividends(store, UNIVERSE, NOW)
    assert set(d) == {"as_of", "source", "history_from", "by_ticker", "misses"}
    assert d["history_from"] == "2021-03-01"
    assert d["by_ticker"]["AAPL"] == [{"d": "2026-01-02", "amt": 0.5}, {"d": "2026-01-06", "amt": 0.25}]   # 1.00 before a 2:1 split
    assert d["by_ticker"]["KO"] == [] and d["by_ticker"]["VTI"] == [] and d["misses"] == []


def test_fundamentals_payload(store: Store):
    f = E.build_fundamentals(store, UNIVERSE, NOW)
    assert set(f) == {"as_of", "source", "lookback_years", "by_ticker", "misses"}
    assert f["lookback_years"] == 10 and f["misses"] == ["KO", "VTI"]
    a = f["by_ticker"]["AAPL"]
    assert set(a) == {"company_name", "annual", "quarterly"} and a["company_name"] == "Sample Corp"
    assert [row["fy"] for row in a["annual"]] == [2023, 2024, 2025]
    assert list(a["annual"][0].keys()) == list(E.ANNUAL_KEYS)
    last = a["annual"][-1]
    assert (last["revenue"], last["assets"], last["ocf"], last["capex"], last["fcf"]) == (1100.0, 5500.0, 250.0, 40.0, 210.0)
    assert last["gross_profit"] is None and last["liabilities"] is None and last["gross_margin"] is None
    q = a["quarterly"]
    assert list(q[0].keys()) == list(E.QUARTER_KEYS) and len(q) == 8
    assert [r["end"] for r in q][-4:] == ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]
    assert q[-1]["revenue"] == 290.0 and q[-1]["eps_diluted"] is None


def test_write_if_changed(tmp_path: Path):
    p = tmp_path / "x.json"
    payload = {"as_of": "t1", "by_ticker": {"A": [1]}, "misses": []}
    assert E.write_if_changed(p, payload) is True
    stamp = p.stat().st_mtime_ns
    assert E.write_if_changed(p, dict(payload, as_of="t2")) is False and p.stat().st_mtime_ns == stamp
    assert E.write_if_changed(p, dict(payload, as_of="t3", by_ticker={"A": [2]})) is True
    assert json.loads(p.read_text())["as_of"] == "t3"


def test_export_cockpit_skips_missing_dir_and_reports(store: Store, tmp_path: Path):
    cfg = SimpleNamespace(export_cockpit=True, export_dir=tmp_path / "missing")
    names = ["prices.json", "dividends.json", "fundamentals.json"]
    assert E.export_cockpit(store, cfg, ["AAPL"], NOW) == [(n, "skipped") for n in names]
    out = tmp_path / "out"
    out.mkdir()
    assert E.export_cockpit(store, cfg, ["AAPL"], NOW, out_dir=out) == [(n, "written") for n in names]
    assert json.loads((out / "prices.json").read_text())["by_ticker"]["AAPL"]
    assert E.export_cockpit(store, cfg, ["AAPL"], NOW, out_dir=out) == [(n, "unchanged") for n in names]
