import csv
import json
from pathlib import Path

import pytest

from financial_data_collector import migrate
from financial_data_collector import statements as S
from financial_data_collector.collectors.sec_facts import parse_company_facts
from financial_data_collector.models import ConceptRule, Fact

FIXTURES = Path(__file__).parent / "fixtures"


def load_rules() -> list[ConceptRule]:
    with open(migrate.SEEDS_DIR / "concept_map.csv", newline="", encoding="utf-8") as f:
        return [ConceptRule(r["line_item"], r["statement"], r["kind"], r["taxonomy"], r["concept"], int(r["priority"]))
                for r in csv.DictReader(f)]


@pytest.fixture(scope="module")
def items():
    facts = parse_company_facts(json.loads((FIXTURES / "sec" / "companyfacts_SAMPLE.json").read_text()))
    return S.rebuild(facts, load_rules())


def _get(items, line_item, kind, end):
    hits = [i for i in items if i.line_item == line_item and i.period_kind == kind and i.period_end == end]
    assert len(hits) <= 1, hits
    return hits[0] if hits else None


def test_fiscal_year_of():
    assert S.fiscal_year_of("2025-12-31") == 2025
    assert S.fiscal_year_of("2026-01-25") == 2026     # NVDA-style late-January year end
    assert S.fiscal_year_of("2026-01-03") == 2025     # 53-week year spilling into January
    assert S.fiscal_year_of("2025-12-28") == 2025


def test_primary_key_is_unique(items):
    keys = [(i.line_item, i.period_kind, i.period_end) for i in items]
    assert len(keys) == len(set(keys))


def test_annual_revenue_latest_filed_wins(items):
    r23 = _get(items, "revenue", "annual", "2023-12-31")
    r24 = _get(items, "revenue", "annual", "2024-12-31")
    r25 = _get(items, "revenue", "annual", "2025-12-31")
    assert (r23.value, r23.fiscal_year) == (900.0, 2023)
    assert (r24.value, r24.accn, r24.fiscal_year) == (1010.0, "k25", 2024)   # restated by the FY2025 10-K
    assert (r25.value, r25.concept, r25.is_derived) == (1100.0, "Revenues", False)


def test_reported_quarters_including_q4_from_10k(items):
    q = {e: _get(items, "revenue", "quarter", e) for e in ("2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31")}
    assert [q[e].value for e in q] == [260.0, 270.0, 280.0, 290.0]
    assert [q[e].fiscal_quarter for e in q] == [1, 2, 3, 4]
    assert all(i.fiscal_year == 2025 and not i.is_derived for i in q.values())
    assert q["2025-12-31"].period_start == "2025-10-01"
    prior = _get(items, "revenue", "quarter", "2024-06-30")
    assert (prior.value, prior.fiscal_year, prior.fiscal_quarter) == (240.0, 2024, 2)


def test_ytd_differencing_for_cash_flow(items):
    q1 = _get(items, "ocf", "quarter", "2025-03-31")
    q2 = _get(items, "ocf", "quarter", "2025-06-30")
    q3 = _get(items, "ocf", "quarter", "2025-09-30")
    q4 = _get(items, "ocf", "quarter", "2025-12-31")
    assert (q1.value, q1.is_derived) == (50.0, False)
    assert (q2.value, q2.is_derived, q2.accn, q2.period_start) == (60.0, True, "q225", "2025-04-01")
    assert (q3.value, q3.is_derived, q3.accn, q3.period_start) == (70.0, True, "q325", "2025-07-01")
    assert (q4.value, q4.is_derived, q4.accn, q4.period_start, q4.fiscal_quarter) == (70.0, True, "k25", "2025-10-01", 4)
    assert _get(items, "ocf", "annual", "2025-12-31").value == 250.0


def test_no_derived_q4_without_siblings(items):
    assert _get(items, "ocf", "annual", "2024-12-31").value == 200.0
    assert _get(items, "ocf", "quarter", "2024-12-31") is None
    assert [i for i in items if i.line_item == "ocf" and i.fiscal_year == 2024 and i.period_kind == "quarter"] == []


def test_per_share_never_derived(items):
    assert _get(items, "eps_diluted", "annual", "2025-12-31").value == 2.0
    assert [_get(items, "eps_diluted", "quarter", e).value for e in ("2025-03-31", "2025-06-30", "2025-09-30")] == [0.5, 0.5, 0.5]
    assert _get(items, "eps_diluted", "quarter", "2025-12-31") is None


def test_instants_at_period_ends(items):
    assert _get(items, "total_assets", "annual", "2024-12-31").value == 5050.0     # restated
    assert _get(items, "total_assets", "annual", "2025-12-31").value == 5500.0
    q = {e: _get(items, "total_assets", "quarter", e) for e in ("2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31")}
    assert [q[e].value for e in q] == [5100.0, 5200.0, 5300.0, 5500.0]
    assert [q[e].fiscal_quarter for e in q] == [1, 2, 3, 4]
    assert _get(items, "total_assets", "quarter", "2024-12-31").fiscal_quarter == 4
    assert all(i.period_start == "" for i in q.values())


def test_priority_and_unmapped_concepts(items):
    so = _get(items, "shares_outstanding", "annual", "2025-12-31")
    assert (so.value, so.concept) == (990.0, "CommonStockSharesOutstanding")     # us-gaap beats dei
    assert _get(items, "shares_outstanding", "quarter", "2026-01-20") is None       # not a period end
    assert not [i for i in items if "OtherAssets" in i.concept]
    assert _get(items, "capex", "annual", "2025-12-31").value == 40.0
    assert [i for i in items if i.line_item == "capex" and i.period_kind == "quarter"] == []


def test_in_progress_year_without_annual():
    rules = [ConceptRule("revenue", "income", "duration", "us-gaap", "Revenues", 1)]
    facts = [
        Fact("us-gaap", "Revenues", "USD", "2026-01-26", "2026-04-26", 10.0, 2027, "Q1", "10-Q", "2026-05-20", "a"),
        Fact("us-gaap", "Revenues", "USD", "2026-04-27", "2026-07-26", 11.0, 2027, "Q2", "10-Q", "2026-08-20", "b"),
    ]
    out = S.rebuild(facts, rules)
    assert sorted((i.fiscal_year, i.fiscal_quarter, i.value) for i in out) == [(2027, 1, 10.0), (2027, 2, 11.0)]


def test_instant_item_with_duration_facts_yields_unique_keys():
    # Some filers tag StockholdersEquity both as an instant (balance sheet) and as a
    # duration (equity roll-forward). The line item must still have one row per period.
    rules = [ConceptRule("stockholders_equity", "balance", "instant", "us-gaap", "StockholdersEquity", 1),
             ConceptRule("revenue", "income", "duration", "us-gaap", "Revenues", 1)]
    facts = [
        Fact("us-gaap", "StockholdersEquity", "USD", "", "2025-12-31", 500.0, 2025, "FY", "10-K", "2026-02-01", "k25"),
        Fact("us-gaap", "StockholdersEquity", "USD", "2025-01-01", "2025-12-31", 500.0, 2025, "FY", "10-K", "2026-02-01", "k25"),
        Fact("us-gaap", "StockholdersEquity", "USD", "2025-10-01", "2025-12-31", 500.0, 2025, "FY", "10-K", "2026-02-01", "k25"),
        Fact("us-gaap", "Revenues", "USD", "2025-01-01", "2025-12-31", 1000.0, 2025, "FY", "10-K", "2026-02-01", "k25"),
    ]
    out = S.rebuild(facts, rules)
    keys = [(i.line_item, i.period_kind, i.period_end) for i in out]
    assert len(keys) == len(set(keys))
    eq = [i for i in out if i.line_item == "stockholders_equity"]
    assert {(i.period_kind, i.period_end, i.value) for i in eq} == {("annual", "2025-12-31", 500.0), ("quarter", "2025-12-31", 500.0)}


def test_ttm_length_facts_in_a_10q_are_not_annual_rows():
    # Amazon-style: a 10-Q reports "twelve months ended June 30". Same length as a
    # fiscal year, but it is not one; only fp = FY facts may become annual rows.
    rules = [ConceptRule("net_income", "income", "duration", "us-gaap", "NetIncomeLoss", 1)]
    facts = [
        Fact("us-gaap", "NetIncomeLoss", "USD", "2025-01-01", "2025-12-31", 100.0, 2025, "FY", "10-K", "2026-02-06", "k25"),
        Fact("us-gaap", "NetIncomeLoss", "USD", "2025-07-01", "2026-06-30", 130.0, 2026, "Q2", "10-Q", "2026-07-31", "q226"),
        Fact("us-gaap", "NetIncomeLoss", "USD", "2026-04-01", "2026-06-30", 40.0, 2026, "Q2", "10-Q", "2026-07-31", "q226"),
        Fact("us-gaap", "NetIncomeLoss", "USD", "2025-01-01", "2025-12-31", 100.0, None, None, "DEF 14A", "2026-04-09", "proxy"),
    ]
    out = S.rebuild(facts, rules)
    annual = [(i.period_end, i.fiscal_year, i.value, i.accn) for i in out if i.period_kind == "annual"]
    assert annual == [("2025-12-31", 2025, 100.0, "k25")]              # the proxy copy and the TTM row are ignored
    assert [(i.period_end, i.value) for i in out if i.period_kind == "quarter"] == [("2026-06-30", 40.0)]


def test_fiscal_year_label_follows_the_filers_own_10k():
    # Home Depot-style: the year ending 2025-02-02 is "fiscal 2024" in its own 10-K.
    rules = [ConceptRule("revenue", "income", "duration", "us-gaap", "Revenues", 1),
             ConceptRule("total_assets", "balance", "instant", "us-gaap", "Assets", 1)]
    facts = [
        Fact("us-gaap", "Revenues", "USD", "2024-02-05", "2025-02-02", 500.0, 2024, "FY", "10-K", "2025-03-20", "k24"),
        Fact("us-gaap", "Revenues", "USD", "2025-02-03", "2026-02-01", 520.0, 2025, "FY", "10-K", "2026-03-19", "k25"),
        Fact("us-gaap", "Revenues", "USD", "2024-02-05", "2025-02-02", 501.0, 2025, "FY", "10-K", "2026-03-19", "k25"),   # restated comparative
        Fact("us-gaap", "Assets", "USD", "", "2025-02-02", 90.0, 2024, "FY", "10-K", "2025-03-20", "k24"),
    ]
    out = S.rebuild(facts, rules)
    rev = {i.period_end: (i.fiscal_year, i.value) for i in out if i.line_item == "revenue" and i.period_kind == "annual"}
    assert rev == {"2025-02-02": (2024, 501.0), "2026-02-01": (2025, 520.0)}
    assets = [i for i in out if i.line_item == "total_assets" and i.period_kind == "annual"][0]
    assert assets.fiscal_year == 2024


def test_calendar_year_disclosures_in_a_june_filer_do_not_create_fiscal_years():
    # ADP-style June fiscal year. Its 10-K also tags a calendar-year pension period
    # with fp = FY; that period is not a fiscal year, and a December balance sheet
    # from a 10-Q must not become an "annual" row.
    rules = [ConceptRule("revenue", "income", "duration", "us-gaap", "Revenues", 1),
             ConceptRule("net_income", "income", "duration", "us-gaap", "NetIncomeLoss", 1),
             ConceptRule("total_assets", "balance", "instant", "us-gaap", "Assets", 1)]
    facts = [
        Fact("us-gaap", "Revenues", "USD", "2024-07-01", "2025-06-30", 200.0, 2025, "FY", "10-K", "2025-08-06", "k25"),
        Fact("us-gaap", "NetIncomeLoss", "USD", "2024-07-01", "2025-06-30", 40.0, 2025, "FY", "10-K", "2025-08-06", "k25"),
        Fact("us-gaap", "DefinedBenefitPlanContributionsByEmployer", "USD", "2025-01-01", "2025-12-31", 3.0, 2025, "FY", "10-K", "2025-08-06", "k25"),
        Fact("us-gaap", "Assets", "USD", "", "2025-06-30", 500.0, 2025, "FY", "10-K", "2025-08-06", "k25"),
        Fact("us-gaap", "Assets", "USD", "", "2025-12-31", 800.0, 2026, "Q2", "10-Q", "2026-02-01", "q226"),
    ]
    out = S.rebuild(facts, rules)
    annual = {(i.line_item, i.period_end): i.fiscal_year for i in out if i.period_kind == "annual"}
    assert annual == {("revenue", "2025-06-30"): 2025, ("net_income", "2025-06-30"): 2025, ("total_assets", "2025-06-30"): 2025}


def test_fiscal_year_labels_come_only_from_annual_report_forms_and_stay_sane():
    rules = [ConceptRule("revenue", "income", "duration", "us-gaap", "Revenues", 1)]
    facts = [
        # the original 10-K labels 2024-12-31 as fiscal 2024
        Fact("us-gaap", "Revenues", "USD", "2024-01-01", "2024-12-31", 100.0, 2024, "FY", "10-K", "2025-02-10", "k24"),
        # an 8-K recast filed later carries fy 2025 for the same period: not an annual report, must not relabel
        Fact("us-gaap", "Revenues", "USD", "2024-01-01", "2024-12-31", 101.0, 2025, "FY", "8-K", "2025-06-01", "recast"),
        # a 10-K whose own year end is 2022-12-31 but whose fy tag is absurdly 2024: label falls back to the calendar year
        Fact("us-gaap", "Revenues", "USD", "2022-01-01", "2022-12-31", 80.0, 2024, "FY", "10-K", "2023-02-10", "k22"),
    ]
    out = {i.period_end: (i.fiscal_year, i.value) for i in S.rebuild(facts, rules) if i.period_kind == "annual"}
    assert out == {"2024-12-31": (2024, 101.0), "2022-12-31": (2022, 80.0)}
