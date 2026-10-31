import pytest

from financial_data_collector.derive import sources
from financial_data_collector.models import SCORE_METRICS, PriceMove, ScoreRow


def test_filing_url_is_built_from_cik_and_accession():
    assert sources.filing_url("0000999002", "0000999002-26-000031") == \
        "https://www.sec.gov/Archives/edgar/data/999002/000099900226000031/0000999002-26-000031-index.htm"


@pytest.mark.parametrize("cik, accession", [("0000999002", "../../x"), ("0000999002", "0000999002-26-00003"),
                                            ("99x", "0000999002-26-000031"), ("", "0000999002-26-000031")])
def test_filing_url_refuses_malformed_input(cik, accession):
    with pytest.raises(ValueError):
        sources.filing_url(cik, accession)


@pytest.mark.parametrize("url, kept", [("https://example.com/a", True), ("http://example.com/a", False),
    ("javascript:alert(1)", False), ("https://example.com/a b", False), ('https://x.com/"', False),
    ("https://x.com/<", False), ("https://x.com/>", False), ("https://x.com/a\nb", False), (None, False), (5, False)])
def test_safe_link(url, kept):
    assert sources.safe_link(url) == (url if kept else None)


def test_price_source_shape():
    assert sources.price("tiingo", "2026-10-29", "2026-10-30T01:00:00Z") == \
        {"kind": "price", "label": "tiingo close", "date": "2026-10-29", "ref": "2026-10-30T01:00:00Z"}


def test_filing_source_shape():
    s = sources.filing("0000999002", "10-Q", "0000999002-26-000031", "2026-07-31", "2026-06-30")
    assert s == {"kind": "filing", "label": "10-Q", "date": "2026-07-31", "ref": "0000999002-26-000031",
                 "period": "2026-06-30", "url": sources.filing_url("0000999002", "0000999002-26-000031")}


def test_filing_source_omits_a_missing_period():
    s = sources.filing("0000999002", "8-K", "0000999002-26-000031", "2026-07-31")
    assert "period" not in s and s["url"].startswith("https://www.sec.gov/")


def test_feed_source_keeps_a_safe_link():
    s = sources.feed("news.json", "2026-10-28", "A headline", "https://example.com/a")
    assert s == {"kind": "feed", "label": "news.json", "date": "2026-10-28", "ref": "A headline",
                 "url": "https://example.com/a"}


def test_feed_source_drops_an_unsafe_link():
    assert "url" not in sources.feed("news.json", "2026-10-28", "A headline", "javascript:x")
    assert "url" not in sources.feed("news.json", "2026-10-28", "A headline")


def test_config_source_shape():
    assert sources.config("drop_day", 0.04) == {"kind": "config", "label": "[engine] drop_day", "date": None, "ref": "0.04"}
    assert sources.config("[engine] drop_day", 0.04)["label"] == "[engine] drop_day"


def test_derived_source_shape():
    assert sources.derived("5-year median", "pe", "2026-10-29") == \
        {"kind": "derived", "label": "5-year median", "date": "2026-10-29", "ref": "pe"}
    assert sources.derived("x", "y")["date"] is None


def test_score_row_refuses_an_unknown_metric():
    with pytest.raises(ValueError):
        ScoreRow("AAA", "2026-10-29", "pe_ratio", 1.0, None, None, "ok", None, (), ())


def test_score_row_refuses_an_unknown_status_or_band():
    with pytest.raises(ValueError):
        ScoreRow("AAA", "2026-10-29", "pe", 1.0, None, None, "fine", None, (), ())
    with pytest.raises(ValueError):
        ScoreRow("AAA", "2026-10-29", "pe", 1.0, 50.0, "cheapish", "ok", None, (), ())


def test_score_row_accepts_every_metric_and_a_band():
    for m in SCORE_METRICS:
        ScoreRow("AAA", "2026-10-29", m, 1.0, 10.0, "cheap", "ok", None, ("a",), ({"kind": "config"},))
    assert len(SCORE_METRICS) == 20 and SCORE_METRICS[0] == "pe" and SCORE_METRICS[-1] == "week_vs_sector"


def test_price_move_is_a_frozen_shape():
    m = PriceMove("AAA", "2026-10-29", "1d", "2026-10-28", -0.05, "SPY", -0.01, "XLK", -0.02, -3.1,
                  ("day",), ({"cause": "no cause found"},), ())
    assert m.span == "1d" and m.triggers == ("day",)
