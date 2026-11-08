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
