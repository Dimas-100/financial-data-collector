"""Mr. Buffett's book breakdown: the table and view (migration 0025), the pure build, and the lifecycle.

The page that reads it is claude-os `buffett_book.py`; the design is claude-os
docs/superpowers/specs/2026-11-08-mr-buffett-page-design.md ("What fdc must publish").
"""
import json
import sqlite3
from pathlib import Path

import pytest

from financial_data_collector.models import BreakdownRow
from financial_data_collector.store import Store


@pytest.fixture
def store(tmp_path: Path):
    s = Store.open(tmp_path / "w.db")
    yield s
    s.close()


def _row(**kw) -> BreakdownRow:
    base = dict(dimension="asset", base="fidelity_rolled_up", bucket="stocks", value=100.0, pct=94.0,
                line_pct=None, direct=None, look_through_complete=1, coverage_pct=None, note=None,
                date="2026-11-06", sources=({"kind": "derived", "label": "positions_latest"},))
    base.update(kw)
    return BreakdownRow(**base)


# ---------------------------------------------------------------- the table and the view

def test_replace_breakdown_round_trips(store: Store):
    rows = [_row(), _row(dimension="company", bucket="NVDA", value=764.0, pct=6.7, direct=217.0,
                         look_through_complete=0, coverage_pct=34.0)]
    assert store.replace_exposure_breakdown(rows) == 2
    got = {r["bucket"]: dict(r) for r in store.query("SELECT * FROM exposure_breakdown")}
    assert got["NVDA"]["direct"] == 217.0
    assert got["NVDA"]["look_through_complete"] == 0
    assert got["NVDA"]["coverage_pct"] == 34.0
    assert json.loads(got["stocks"]["sources"]) == [{"kind": "derived", "label": "positions_latest"}]


def test_breakdown_check_constraints(store: Store):
    with pytest.raises(sqlite3.IntegrityError):
        store.replace_exposure_breakdown([_row(dimension="planet")])
    with pytest.raises(sqlite3.IntegrityError):
        store.replace_exposure_breakdown([_row(base="household")])


def test_replace_is_exact(store: Store):
    store.replace_exposure_breakdown([_row(), _row(bucket="bonds")])
    store.replace_exposure_breakdown([_row(bucket="bonds", value=600.0)])
    got = [dict(r) for r in store.query("SELECT bucket, value FROM exposure_breakdown")]
    assert got == [{"bucket": "bonds", "value": 600.0}]


def test_breakdown_counted(store: Store):
    store.replace_exposure_breakdown([_row()])
    assert store.counts()["exposure_breakdown_raw"] == 1


# ---------------------------------------------------------------- the pure build

from financial_data_collector.derive import breakdown as B
from financial_data_collector.derive import exposure as E

SECTORS = E.Sectors(company={"NVDA": "Technology", "LLY": "Health Care"},
                    funds={"FXAIX": {"Technology": 40.0, "Health Care": 60.0},
                           "SCHD": {"Technology": 50.0, "Consumer Staples": 50.0}})
COUNTRIES = B.Countries(company={"NVDA": "United States"},
                        funds={"FXAIX": {"United States": 100.0}, "VXUS": {"International (ex-US)": 100.0}})
PLAN = {"NVDA": 50.0, "LLY": 50.0}


def _rolled() -> E.Book:
    return E.Book(held={"FXAIX": 6000.0, "VGIT": 600.0, "NVDA": 200.0}, cash=3.0,
                  funds=frozenset({"FXAIX", "VGIT"}), look_through={"FXAIX": {"NVDA": 0.08, "AAPL": 0.07}},
                  coverage={"FXAIX": 39.0})


def _brokerage() -> E.Book:
    return E.Book(held={"NVDA": 200.0, "SCHD": 700.0}, cash=0.0, funds=frozenset({"SCHD"}))


def _build(**kw):
    args = dict(brokerage=_brokerage(), rolled_up=_rolled(), plan=PLAN, sectors=SECTORS, countries=COUNTRIES,
                fees={"FXAIX": 0.015}, day="2026-11-06")
    args.update(kw)
    return B.build_breakdown(**args)


def _pick(rows, dimension, bucket, base="fidelity_rolled_up"):
    got = [r for r in rows if (r.dimension, r.bucket, r.base) == (dimension, bucket, base)]
    assert len(got) == 1, (dimension, bucket, base, rows)
    return got[0]


def test_asset_split():
    rows = _build()
    assert _pick(rows, "asset", "stocks").value == 6200.0
    assert _pick(rows, "asset", "bonds").value == 600.0
    assert _pick(rows, "asset", "cash").value == 3.0
    assert round(_pick(rows, "asset", "bonds").pct, 2) == round(100 * 600 / 6803, 2)


def test_company_rows_floor():
    rows = _build()
    nvda = _pick(rows, "company", "NVDA")
    assert nvda.value == pytest.approx(680.0)
    assert nvda.direct == 200.0
    assert nvda.look_through_complete == 0
    assert nvda.coverage_pct == _rolled().look_through_coverage()[0]
    assert _pick(rows, "company", "AAPL").direct == 0.0
    assert not [r for r in rows if r.dimension == "company" and r.bucket in ("FXAIX", "VGIT")]


def test_crash_rows_four_scenarios():
    rows = [r for r in _build() if r.dimension == "crash"]
    assert [r.bucket for r in rows] == ["2007-09", "2000-02", "2020", "2022"]
    assert rows[0].value == round(6200 * 0.5678, 2)
    assert rows[3].pct == pytest.approx(25.43)


def test_fee_names_left_out():
    """A fund with no ratio on record (VGIT, 600) is left out of the average entirely, as the note says: not counted
    at 0%, which would pull the ratio down by a fee nobody knows. The yearly dollars are the known funds' own."""
    fee = _pick(_build(), "fee", "weighted")
    assert fee.pct == pytest.approx(100 * 6000 * 0.015 / 100 / (6803 - 600), abs=1e-6)     # stored to 6 places
    assert fee.value == pytest.approx(6000 * 0.015 / 100, abs=0.01)
    assert "VGIT" in fee.note and "left out" in fee.note.lower()


def test_brokerage_sector_has_plan_line():
    tech = _pick(_build(), "sector", "Technology", base="brokerage")
    assert tech.pct == pytest.approx(100 * (200 + 350) / 900, rel=1e-6)
    assert tech.line_pct == pytest.approx(50.0)


def test_rolled_up_sector_has_no_line():
    tech = _pick(_build(), "sector", "Technology")
    assert tech.line_pct is None
    assert tech.value == pytest.approx(6000 * 0.4 + 200)


def test_country_azn_international_when_unknown():
    book = E.Book(held={"AZN": 100.0, "NVDA": 300.0}, cash=0.0)
    rows = _build(rolled_up=book)
    assert _pick(rows, "country", "International").value == 100.0
    assert _pick(rows, "country", "United States").pct == pytest.approx(75.0)


def test_no_book_gives_no_rows():
    assert _build(rolled_up=None, brokerage=None) == []


# ---------------------------------------------------------------- the readers and the lifecycle

from financial_data_collector import sectors as S
from financial_data_collector.derive import targets as T
from test_exposure import _book, _cfg


def test_read_fund_countries_maps_ex_us(tmp_path: Path):
    (tmp_path / "fund_category_weights.json").write_text(json.dumps({"funds": {
        "VXUS": {"country": {"International (ex-US)": 100.0}}, "FXAIX": {"country": {"United States": 100.0}},
        "BAD": {"country": "nowhere"}}}), encoding="utf-8")
    got = T.read_fund_countries(tmp_path)
    assert got == {"VXUS": {"International (ex-US)": 100.0}, "FXAIX": {"United States": 100.0}}
    assert T.read_fund_countries(tmp_path / "missing") == {}


def test_read_fund_expense_ratios_reads_locked_values(tmp_path: Path):
    (tmp_path / "fund_category_weights.json").write_text(json.dumps({"funds": {
        "FXAIX": {"expense_ratio": 0.015, "expense_ratio_locked": True}, "VGIT": {"sector": {}},
        "ODD": {"expense_ratio": "a lot"}}}), encoding="utf-8")
    assert T.read_fund_expense_ratios(tmp_path) == {"FXAIX": 0.015}


def test_read_feed_countries(tmp_path: Path):
    feed = tmp_path / "classifications.json"
    feed.write_text(json.dumps({"by_ticker": {"NVDA": {"country": "United States"}, "ASML.AS": {"country": "Netherlands"},
                                              "X": {"country": None}}}), encoding="utf-8")
    got = S.read_feed_countries(feed)
    assert got["NVDA"] == "United States" and "X" not in got
    assert S.read_feed_countries(tmp_path / "missing.json") == {}


def test_run_writes_breakdown(store: Store, tmp_path: Path):
    _book(store)
    rep = E.run(store, _cfg(tmp_path))
    assert rep.breakdown > 0
    rows = {(r["dimension"], r["base"], r["bucket"]): dict(r) for r in store.query("SELECT * FROM exposure_breakdown")}
    msft = rows[("company", "fidelity_rolled_up", "MSFT")]
    assert msft["value"] == pytest.approx(340.0)            # 300 direct + 40 through SCHD; Webull's 9000 nowhere
    assert msft["direct"] == pytest.approx(300.0)
    assert rows[("asset", "fidelity_rolled_up", "stocks")]["value"] == pytest.approx(2000.0)
    assert {k[0] for k in rows} == {"asset", "sector", "country", "company", "crash", "fee"}
