"""Mr. Buffett's five exposure figures: pure measure math, then the table, the view and the lifecycle.

The panel that reads them is claude-os `bull_panel.py`; the measures are the spec's section 5
(investing/docs/superpowers/specs/2026-11-06-mr-buffett-mr-bayes-design.md).
"""
import dataclasses
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from financial_data_collector import config as C
from financial_data_collector.check.calendar import is_trading_day
from financial_data_collector.config import AllocationSettings
from financial_data_collector.derive import exposure as E
from financial_data_collector.models import AccountRef, CashRow, PositionRow, Snapshot
from financial_data_collector.store import Store

BROK = AccountRef("Sample Brokerage", "brokerage", "fidelity", "brokerage")
ROTH = AccountRef("Sample Roth", "roth", "fidelity", "roth_ira")
WEBULL = AccountRef("Sample Webull", "brokerage", "webull", "brokerage")

# The 13-name plan, as investing/config/sizing-targets.json renders it: seven live `brokerage` rows (59%) and six
# `planned` ones (41%). The top three are SCHD 14 + LLY 9 + V 9 = 32; MSFT + NVDA + AMZN + GOOGL = 29.
PLAN = {"SCHD": 14.0, "MSFT": 8.0, "NVDA": 6.0, "AMZN": 7.0, "LLY": 9.0, "BRK.B": 8.0, "WM": 7.0,
        "GOOGL": 8.0, "AZN": 6.0, "V": 9.0, "CME": 6.0, "TJX": 7.0, "CB": 5.0}
SECTORS = E.Sectors(
    company={"MSFT": "Technology", "NVDA": "Technology", "GOOGL": "Communication Services",
             "LLY": "Health Care", "AZN": "Health Care", "AMZN": "Consumer Discretionary",
             "BRK.B": "Financials", "V": "Financials", "CME": "Financials", "WM": "Industrials"},
    funds={"SCHD": {"Technology": 50.0, "Consumer Staples": 50.0}},
)
SCHD_LOOK = {"MSFT": 0.10, "KO": 0.20}          # a top-ten cache: 30% of the fund
FXAIX_LOOK = {"MSFT": 0.06, "GOOGL": 0.03, "AAPL": 0.07}    # 16% of the fund


def _brokerage() -> E.Book:
    return E.Book(held={"SCHD": 400.0, "MSFT": 300.0, "GOOGL": 200.0, "LLY": 100.0}, cash=0.0,
                  funds=frozenset({"SCHD"}), look_through={"SCHD": SCHD_LOOK}, coverage={"SCHD": 30.0})


def _rolled_up() -> E.Book:
    """The brokerage plus a Roth of FXAIX and VGIT; VGIT has no look-through file at all."""
    return E.Book(held={"SCHD": 400.0, "MSFT": 300.0, "GOOGL": 200.0, "LLY": 100.0, "FXAIX": 1000.0, "VGIT": 500.0},
                  cash=0.0, funds=frozenset({"SCHD", "FXAIX", "VGIT"}),
                  look_through={"SCHD": SCHD_LOOK, "FXAIX": FXAIX_LOOK}, coverage={"SCHD": 30.0, "FXAIX": 16.0})


def _rows(**kw) -> dict[str, E.ExposureRow]:
    args = dict(brokerage=_brokerage(), rolled_up=_rolled_up(), plan=PLAN, sectors=SECTORS, day="2026-11-08")
    args.update(kw)
    return {r.figure: r for r in E.build_exposure(**args)}


# ---------------------------------------------------------------- the contract claude-os reads

def test_the_panel_gets_all_five_figures_in_the_spec_order():
    assert [r.figure for r in E.build_exposure(_brokerage(), _rolled_up(), PLAN, SECTORS, "2026-11-08")] == \
           ["top_three", "sector_spread", "stacked", "ai_capex", "crash_cost"]


def test_every_published_row_names_its_base():
    """Review Focus 4 - a base-less figure gets read against the wrong line."""
    rows = _rows()
    assert all(r.base in {"brokerage", "fidelity_rolled_up"} for r in rows.values())
    assert [rows[k].base for k in ("top_three", "sector_spread", "ai_capex")] == ["brokerage"] * 3
    assert [rows[k].base for k in ("stacked", "crash_cost")] == ["fidelity_rolled_up"] * 2


def test_every_published_row_states_its_completeness():
    """Review Focus 5 - partial look-through must travel with the number."""
    rows = _rows()
    assert all(r.look_through_complete in {0, 1} for r in rows.values())
    partial = [r for r in rows.values() if not r.look_through_complete]
    assert {r.figure for r in partial} == {"stacked", "ai_capex"}       # the two that read constituent weights
    assert all(r.coverage_pct is not None for r in partial)


def test_a_figure_that_reads_no_constituent_weight_is_complete():
    """top_three counts positions as the plan names them, and the crash applies a published fall: neither looks
    through a fund, so neither may claim a coverage figure it did not use."""
    for key in ("top_three", "sector_spread", "crash_cost"):
        row = _rows()[key]
        assert (row.look_through_complete, row.coverage_pct) == (1, None)


def test_plan_lines_use_all_thirteen_names():
    """A line computed from the seven live rows alone has a 59% denominator."""
    rows = _rows()
    assert rows["top_three"].line_pct == pytest.approx(32.0)
    assert rows["ai_capex"].line_pct == pytest.approx(29.0)


def test_the_lines_move_with_the_plan_and_are_never_hard_coded():
    """Goal 6: the lines follow the targets file without a code edit."""
    plan = dict(PLAN, V=12.0, SCHD=16.0, GOOGL=10.0)
    rows = _rows(plan=plan)
    assert rows["top_three"].line_pct == pytest.approx(16.0 + 12.0 + 10.0)
    assert rows["ai_capex"].line_pct == pytest.approx(8.0 + 6.0 + 7.0 + 10.0)


def test_stacked_and_the_crash_publish_no_line():
    """Spec 5: rules 2 and 5 are qualitative and rules 12-15 behavioural, so the owner has drawn NO line here.
    claude-os drops a line sent for these two and says the figure came with one; sending one invents philosophy."""
    rows = _rows()
    assert rows["stacked"].line_pct is None and rows["crash_cost"].line_pct is None
    assert "no line" in (rows["stacked"].note or "") and "no line" in (rows["crash_cost"].note or "")


def test_a_plan_with_no_lines_still_publishes_the_figures_it_can():
    """No targets file: the two measured-against-a-line figures keep their value and lose only the line."""
    rows = _rows(plan={})
    assert rows["top_three"].value == pytest.approx(90.0) and rows["top_three"].line_pct is None
    assert "plan" in (rows["top_three"].note or "")
    assert rows["ai_capex"].line_pct is None


def test_every_row_carries_the_day_and_the_sources_it_was_given():
    src = ({"kind": "derived", "label": "positions_latest", "date": "2026-11-08", "ref": "brokerage"},)
    rows = _rows(sources=src)
    assert all(r.date == "2026-11-08" and r.sources == src for r in rows.values())


# ---------------------------------------------------------------- the measures themselves

def test_top_three_is_the_three_largest_holdings_of_the_brokerage():
    """Position level, as the plan names them: SCHD is one name in the plan, not a basket to look through."""
    row = _rows()["top_three"]
    assert row.value == pytest.approx(90.0)                      # (400 + 300 + 200) / 1000
    assert row.unit == "pct" and "SCHD" in row.note and "LLY" not in row.note


def test_top_three_counts_cash_in_the_base():
    """The plan's percentages are of the brokerage, and uninvested cash is part of the brokerage."""
    book = dataclasses.replace(_brokerage(), cash=1000.0)
    assert _rows(brokerage=book)["top_three"].value == pytest.approx(45.0)


def test_sector_spread_is_the_share_of_the_brokerage_sitting_in_the_wrong_sector():
    """Half the sum of the absolute differences: what share would have to move to match the plan. Zero is target."""
    row = _rows()["sector_spread"]
    assert row.value == pytest.approx(54.0)
    assert row.line_pct == pytest.approx(0.0)
    assert "Technology is furthest from plan: 50.0% held against 21.0% planned" in (row.note or "")


def test_sector_spread_spreads_a_fund_by_its_published_sector_map():
    """SCHD is 40% of this brokerage and half of it is Technology, so Technology reads 50% held, not MSFT's 30%."""
    assert "Technology is furthest from plan: 50.0% held" in (_rows()["sector_spread"].note or "")
    flat = E.Sectors(company=SECTORS.company, funds={})
    assert "SCHD: no sector on record" in (_rows(sectors=flat)["sector_spread"].note or "")


def test_a_plan_name_with_no_sector_on_record_is_counted_not_dropped():
    """TJX 7% and CB 5% have no securities row in this warehouse. Dropping them would shrink the plan's own
    denominator and understate a gap the owner really has - he holds neither name."""
    bare = E.Sectors(company={k: v for k, v in SECTORS.company.items() if k not in ("V", "CME")}, funds=SECTORS.funds)
    row = _rows(sectors=bare)["sector_spread"]
    assert row.value == pytest.approx(54.0)                      # V and CME fall back to Financials, as TJX/CB do
    assert "no sector on record" not in (row.note or "")


def test_a_name_in_neither_the_warehouse_nor_the_fallback_is_named_in_the_note():
    row = _rows(plan=dict(PLAN, ZZZZ=10.0))["sector_spread"]
    assert "ZZZZ" in (row.note or "")


def test_stacked_is_the_largest_company_exposure_across_both_accounts():
    """Direct plus through the funds: MSFT is 300 held and 100 through SCHD and FXAIX, of a 2500 book."""
    row = _rows()["stacked"]
    assert row.value == pytest.approx(16.0)
    assert "MSFT" in (row.note or "") and "12.0" in (row.note or "") and "4.0" in (row.note or "")


def test_stacked_ranks_companies_not_the_funds_that_wrap_them():
    """VGIT is 20% of this book and its largest single line, but a fund is a wrapper, not a stacked name: ranking it
    would answer measure 1's question with measure 3's meter. MSFT's 16% wins, not VGIT's 20%."""
    assert _rows()["stacked"].value == pytest.approx(16.0)


def test_stacked_reports_the_dollar_weighted_coverage_of_the_funds_it_read():
    """SCHD 400 at 30%, FXAIX 1000 at 16%, VGIT 500 with no file at all: 28000/1900."""
    row = _rows()["stacked"]
    assert row.coverage_pct == pytest.approx(14.74, abs=0.01)
    assert row.look_through_complete == 0 and "VGIT" in (row.note or "")


def test_a_complete_look_through_says_so():
    """Once fdc publishes whole holdings files the floor becomes a total, with no code change here."""
    whole = dataclasses.replace(_rolled_up(), coverage={"SCHD": 100.0, "FXAIX": 99.8, "VGIT": 100.0})
    row = _rows(rolled_up=whole)["stacked"]
    assert row.look_through_complete == 1 and row.coverage_pct == pytest.approx(99.89, abs=0.01)


def test_ai_capex_counts_the_weight_inside_a_fund_as_well_as_the_direct_dollars():
    """MSFT 300 direct + 40 through SCHD + GOOGL 200, of 1000."""
    row = _rows()["ai_capex"]
    assert row.value == pytest.approx(54.0) and row.line_pct == pytest.approx(29.0)
    assert row.look_through_complete == 0 and row.coverage_pct == pytest.approx(30.0)


def test_ai_capex_never_reads_as_a_cap():
    """Spec 5: its honest message is "you are early, not reckless" - it falls on its own as the other names fund."""
    note = _rows()["ai_capex"].note or ""
    assert "not a cap" in note and "unfunded" in note


def test_the_crash_applies_a_published_fall_to_the_equity_in_the_book():
    """Measure 5 is published history on his current allocation, not a model of his own prices."""
    row = _rows()["crash_cost"]
    assert row.unit == "usd"
    assert row.value == pytest.approx(2000.0 * 0.5678, abs=0.01)      # VGIT is ballast, not equity
    assert "allocation like yours" in (row.note or "")


def test_the_crash_leaves_cash_out_of_the_fall():
    book = dataclasses.replace(_rolled_up(), cash=5000.0)
    assert _rows(rolled_up=book)["crash_cost"].value == pytest.approx(_rows()["crash_cost"].value)


def test_the_crash_names_its_own_reference_and_the_shallower_ones():
    note = _rows()["crash_cost"].note or ""
    assert "2007" in note and "2020" in note and "Treasur" in note


def test_an_empty_book_publishes_nothing_rather_than_a_zero():
    """A zero on this panel would read as an answer. No dollars means no figure."""
    empty = E.Book(held={}, cash=0.0, funds=frozenset(), look_through={}, coverage={})
    rows = {r.figure: r for r in E.build_exposure(empty, empty, PLAN, SECTORS, None)}
    assert all(r.value is None for r in rows.values())
    assert all(r.note for r in rows.values())


# ---------------------------------------------------------------- the table, the view and the lifecycle

TARGETS_DOC = {
    "last_revised": "2026-11-04",
    "brokerage": [
        {"ticker": "SCHD", "type": "pct_of_brokerage", "target": 14.0, "rebalance": "hold"},
        {"ticker": "MSFT", "type": "pct_of_brokerage", "target": 8.0},
        {"ticker": "LLY", "type": "pct_of_brokerage", "target": 9.0},
    ],
    "roth": [{"ticker": "FXAIX", "type": "pct_of_account", "target": 59.0}],
    "planned": [
        {"ticker": "GOOGL", "type": "pct_of_brokerage", "target": 8.0, "label": "GOOGL - 8%", "size": "8%"},
        {"ticker": "V", "type": "pct_of_brokerage", "target": 9.0, "label": "V - 9%", "size": "9%"},
    ],
}


@pytest.fixture
def store(tmp_path: Path):
    s = Store.open(tmp_path / "w.db")
    yield s
    s.close()


def _cfg(tmp_path: Path, **kw):
    C.init_project(tmp_path)
    cfg = C.load_config(tmp_path)
    targets = tmp_path / "sizing-targets.json"
    targets.write_text(json.dumps(TARGETS_DOC), encoding="utf-8")
    funds = tmp_path / "fund_holdings"
    funds.mkdir(exist_ok=True)
    (funds / "SCHD.csv").write_text("ticker,name,weight_pct\nMSFT,Microsoft Corp,10\nKO,Coca-Cola Co,20\n",
                                    encoding="utf-8")
    (funds / "fund_category_weights.json").write_text(json.dumps(
        {"funds": {"SCHD": {"sector": {"Technology": 50.0, "Consumer Defensive": 50.0}}}}), encoding="utf-8")
    settings = dict(targets=targets, fund_holdings=funds,
                    accounts={"brokerage": "Sample Brokerage", "roth": "Sample Roth"})
    settings.update(kw)
    return dataclasses.replace(cfg, allocation=AllocationSettings(**settings))


def _book(store: Store, day: str = "2026-11-08") -> None:
    """A Fidelity brokerage and Roth, and a Webull account the exposure math must never touch."""
    rows = [PositionRow(BROK, s, None, 1.0, v, v) for s, v in
            (("SCHD", 400.0), ("MSFT", 300.0), ("GOOGL", 200.0), ("LLY", 100.0))]
    rows += [PositionRow(ROTH, "FXAIX", None, 1.0, 1000.0, 1000.0)]
    rows += [PositionRow(WEBULL, "MSFT", None, 1.0, 9000.0, 9000.0)]
    store.write_snapshot(Snapshot(day, "fidelity_csv", rows,
                                  [CashRow(BROK, 0.0), CashRow(ROTH, 0.0), CashRow(WEBULL, 4000.0)]))
    for s, t in (("SCHD", "etf"), ("FXAIX", "mutual_fund"), ("MSFT", "stock"), ("GOOGL", "stock"), ("LLY", "stock")):
        store.upsert_security(s, None, t, day)
        store.set_asset_type(s, t)
    store.conn.execute("UPDATE securities SET sector = 'Technology' WHERE symbol = 'MSFT'")
    store.conn.commit()


def _published(store: Store) -> dict[str, dict]:
    return {r["figure"]: dict(r) for r in store.query("SELECT * FROM exposure_figures")}


def test_the_view_serves_the_columns_the_panel_reads(store: Store, tmp_path: Path):
    _book(store)
    rep = E.run(store, _cfg(tmp_path))
    assert rep.skipped is None and rep.published == 5
    got = _published(store)
    assert set(got) == set(E.FIGURES)
    for name in ("figure", "base", "value", "line_pct", "look_through_complete", "coverage_pct", "note", "date"):
        assert name in got["top_three"], name
    assert got["top_three"]["date"] == "2026-11-08"


def test_the_published_figures_are_the_ones_the_measures_computed(store: Store, tmp_path: Path):
    _book(store)
    E.run(store, _cfg(tmp_path))
    got = _published(store)
    assert got["top_three"]["value"] == pytest.approx(90.0)
    assert got["top_three"]["line_pct"] == pytest.approx(14.0 + 9.0 + 9.0)      # SCHD 14 + LLY 9 + V 9
    assert got["ai_capex"]["line_pct"] == pytest.approx(8.0 + 8.0)              # MSFT 8 + GOOGL 8; no NVDA or AMZN
    assert got["stacked"]["value"] == pytest.approx(17.0)       # MSFT 300 + 40 through SCHD, of 2000
    assert got["stacked"]["look_through_complete"] == 0


def test_webull_dollars_never_reach_a_figure(store: Store, tmp_path: Path):
    """The owner's standing rule (2026-08-26): the household TOTAL may include the Webull book, concentration and
    percentage-holdings math may not. The Webull account here holds 9000 of MSFT - three times the Fidelity book."""
    _book(store)
    E.run(store, _cfg(tmp_path))
    assert _published(store)["top_three"]["value"] == pytest.approx(90.0)
    assert _published(store)["stacked"]["value"] == pytest.approx(17.0)


def test_an_account_that_is_not_fidelity_is_left_out_even_when_it_is_configured(store: Store, tmp_path: Path):
    """A configured account at another broker would put that broker's dollars into a Fidelity-scoped figure."""
    _book(store)
    cfg = _cfg(tmp_path, accounts={"brokerage": "Sample Brokerage", "roth": "Sample Roth",
                                   "webull": "Sample Webull"})
    rep = E.run(store, cfg)
    assert _published(store)["stacked"]["value"] == pytest.approx(17.0)
    assert any("not at fidelity" in n.lower() for n in rep.notes)
    assert all("Sample Webull" not in n for n in rep.notes)      # a note names the plan's key, never a label


def test_rebuild_is_idempotent(store: Store, tmp_path: Path):
    """Derived: a row already right is not rewritten."""
    _book(store)
    cfg = _cfg(tmp_path)
    E.run(store, cfg)
    before = {k: dict(v) for k, v in _published(store).items()}
    writes = store.conn.total_changes
    E.run(store, cfg)
    assert _published(store) == before
    assert store.conn.total_changes == writes            # not one row rewritten
    assert store.query("SELECT COUNT(*) FROM exposure_figures_raw")[0][0] == 5


def test_a_figure_that_can_no_longer_be_computed_is_removed_not_left_stale(store: Store, tmp_path: Path):
    _book(store)
    cfg = _cfg(tmp_path)
    E.run(store, cfg)
    assert len(_published(store)) == 5
    store.replace_exposure_figures([])
    assert _published(store) == {}


def test_no_fidelity_positions_leaves_the_previous_figures_alone(store: Store, tmp_path: Path):
    """Never blank the panel on a read that found nothing: the owner cannot tell a dark meter from a broken one."""
    _book(store)
    cfg = _cfg(tmp_path)
    E.run(store, cfg)
    before = _published(store)
    rep = E.run(store, dataclasses.replace(cfg, allocation=dataclasses.replace(cfg.allocation, accounts={})))
    assert rep.skipped is not None and _published(store) == before


def test_the_base_of_a_figure_is_never_the_webull_book_even_with_no_brokerage_configured(store: Store,
                                                                                         tmp_path: Path):
    """With only the Roth configured there is no brokerage base, so the three brokerage figures have no value -
    they must not quietly fall back to another account."""
    _book(store)
    cfg = _cfg(tmp_path, accounts={"roth": "Sample Roth"})
    E.run(store, cfg)
    got = _published(store)
    assert got["top_three"]["value"] is None and got["ai_capex"]["value"] is None
    assert "no brokerage holdings" in got["top_three"]["note"]
    assert got["crash_cost"]["value"] is not None                  # the Roth alone is still a Fidelity rollup


# ---------------------------------------------------------------- Task 4: the price contract claude-os reads

def _bars(store: Store, rows) -> None:
    store.conn.executemany(
        "INSERT OR REPLACE INTO prices (symbol, date, close, adj_close, source, final) VALUES (?, ?, ?, ?, 'test', ?)",
        [(s, d, 100.0, 100.0, f) for s, d, f in rows])
    store.conn.commit()


def test_trading_days_view_excludes_non_final_bars(store: Store):
    """Review Focus 5 - the newest date is exactly where a partial bar lives."""
    _bars(store, [("SPY", "2026-11-05", 1), ("SPY", "2026-11-06", 1), ("SPY", "2026-11-09", 0)])
    E.publish_trading_days(store)
    days = [r[0] for r in store.query("SELECT day FROM trading_days ORDER BY day DESC")]
    assert "2026-11-09" not in days
    assert days == ["2026-11-06", "2026-11-05"]


def test_a_day_turns_final_as_soon_as_its_bar_does(store: Store):
    _bars(store, [("SPY", "2026-11-05", 1), ("SPY", "2026-11-06", 0)])
    E.publish_trading_days(store)
    assert [r[0] for r in store.query("SELECT day FROM trading_days")] == ["2026-11-05"]
    _bars(store, [("SPY", "2026-11-06", 1)])
    assert [r[0] for r in store.query("SELECT day FROM trading_days")] == ["2026-11-05", "2026-11-06"]


def test_trading_days_view_agrees_with_the_by_rule_calendar(store: Store):
    """The view must not invent a day the NYSE calendar rejects: a weekend or a holiday bar is not a trading day."""
    _bars(store, [("SPY", "2026-11-05", 1), ("SPY", "2026-11-07", 1),        # Saturday
                  ("SPY", "2026-11-26", 1), ("SPY", "2026-11-27", 1)])       # Thanksgiving, then the Friday after
    E.publish_trading_days(store)
    days = {r[0] for r in store.query("SELECT day FROM trading_days")}
    assert days == {"2026-11-05", "2026-11-27"}
    assert all(is_trading_day(date.fromisoformat(d)) for d in days)


def test_a_trading_day_with_no_bar_at_all_is_not_published(store: Store):
    """The calendar is a day axis for closes: a day fdc has not fetched yet would send a consumer looking for one."""
    _bars(store, [("SPY", "2026-11-05", 1)])
    E.publish_trading_days(store)
    assert [r[0] for r in store.query("SELECT day FROM trading_days")] == ["2026-11-05"]


def test_trading_days_counts_the_final_bars_it_found(store: Store):
    _bars(store, [("SPY", "2026-11-05", 1), ("MSFT", "2026-11-05", 1), ("KO", "2026-11-05", 0)])
    E.publish_trading_days(store)
    assert [tuple(r) for r in store.query("SELECT day, final_bars FROM trading_days")] == [("2026-11-05", 2)]


def test_publishing_the_calendar_twice_writes_the_same_days(store: Store):
    _bars(store, [("SPY", "2026-11-05", 1), ("SPY", "2026-11-06", 1)])
    assert E.publish_trading_days(store) == E.publish_trading_days(store) == 2


def test_no_prices_publishes_no_calendar(store: Store):
    assert E.publish_trading_days(store) == 0
    assert store.query("SELECT day FROM trading_days") == []


def test_the_views_are_readable_through_a_read_only_connection(store: Store, tmp_path: Path):
    """claude-os opens this warehouse mode=ro and cannot import any of this repo's Python."""
    _book(store)
    _bars(store, [("SPY", "2026-11-05", 1)])
    E.run(store, _cfg(tmp_path))
    E.publish_trading_days(store)
    store.close()
    conn = sqlite3.connect(f"file:{tmp_path / 'w.db'}?mode=ro", uri=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM exposure_figures").fetchone()[0] == 5
        assert conn.execute("SELECT day FROM trading_days").fetchall() == [("2026-11-05",)]
    finally:
        conn.close()
