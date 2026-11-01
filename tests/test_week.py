"""The week score (spec addendum 2026-10-31): pure, from the money's destinations. Starts at 7; cheap destinations
add up to 3 and premium ones take up to 6, by their share of the gap; an unexplained drop on a destination of 10% or
more of the gap takes 1, at most 2; the guards force `go`."""
import re

import pytest

from financial_data_collector.derive import week as W

D = W.Destination


def _score(dests, weeks=0, go_from=7, half_from=4, max_wait=4) -> W.WeekScore:
    return W.week_score(dests, weeks_waited=weeks, go_from=go_from, half_from=half_from, max_wait_weeks=max_wait)


def _codes(ws: W.WeekScore) -> list[str]:
    return [p["code"] for p in ws.parts]


def test_a_usual_week_scores_7_and_goes():
    ws = _score([D("VOO", 300.0, "usual"), D("KO", 100.0, "usual")])
    assert (ws.score, ws.verdict, ws.fraction, ws.forced) == (7, "go", 1.0, None)
    assert ws.parts[0] == {"sign": "neutral", "code": "base", "symbol": None, "text": "A usual week starts at 7."}


def test_cheap_share_adds_up_to_3():
    ws = _score([D("AAPL", 200.0, "cheap"), D("VOO", 200.0, "usual")])          # half the gap cheap: +1.5 -> +2
    assert ws.score == 9
    [cheap] = [p for p in ws.parts if p["code"] == "cheap"]
    assert cheap["sign"] == "up" and cheap["text"] == "Cheaper than its own past: AAPL (50% of what is short): +2."
    assert _score([D("AAPL", 100.0, "cheap")]).score == 10


def test_premium_share_takes_up_to_6():
    assert _score([D("VOO", 100.0, "premium"), D("KO", 200.0, "usual")]).score == 5     # a third: -2 -> half
    ws = _score([D("VOO", 200.0, "premium"), D("KO", 100.0, "usual")])                   # two thirds: -4
    assert (ws.score, ws.verdict, ws.fraction) == (3, "hold", 0.0)
    [prem] = [p for p in ws.parts if p["code"] == "premium"]
    assert prem["sign"] == "down" and prem["text"] == "Premium to its own past: VOO (67% of what is short): -4."
    assert _score([D("VOO", 100.0, "premium")]).score == 1


def test_thresholds_are_settings():
    dests = [D("VOO", 100.0, "premium"), D("KO", 200.0, "usual")]                      # 5
    assert _score(dests).verdict == "half" and _score(dests).fraction == 0.5
    assert _score(dests, go_from=5).verdict == "go"
    assert _score(dests, half_from=6).verdict == "hold"


def test_an_unexplained_drop_takes_one_at_most_two():
    drop = {"date": "2026-10-30", "move": -0.035}
    dests = [D("QQQM", 100.0, "usual", drop=drop), D("FXAIX", 300.0, "usual", drop=drop),
             D("AVUV", 100.0, "usual", drop=drop)]
    ws = _score(dests)
    assert ws.score == 5
    drops = [p for p in ws.parts if p["code"] == "drop_no_cause"]
    assert [(p["symbol"], p["sign"]) for p in drops] == [("FXAIX", "down"), ("AVUV", "down"), ("QQQM", "neutral")]
    assert drops[0]["text"] == "FXAIX fell 3.5% on Oct 30 with no cause found: -1."
    assert drops[2]["text"] == "QQQM fell 3.5% on Oct 30 with no cause found: not counted (drops take at most 2)."


def test_a_drop_on_a_small_destination_does_not_count():
    drop = {"date": "2026-10-30", "move": -0.05}
    ws = _score([D("VOO", 950.0, "usual"), D("KO", 50.0, "usual", drop=drop)])      # 5% of the gap
    assert ws.score == 7 and "drop_no_cause" not in _codes(ws)


def test_not_known_is_usual_and_said():
    ws = _score([D("FXAIX", 300.0, "usual", known=False), D("QQQM", 100.0, "usual", known=False)])
    assert ws.score == 7
    [nk] = [p for p in ws.parts if p["code"] == "not_known"]
    assert nk == {"sign": "neutral", "code": "not_known", "symbol": None,
                  "text": "Not known yet, counted as usual: FXAIX, QQQM."}


def test_nothing_short_is_said_and_changes_nothing():
    ws = _score([])
    assert (ws.score, ws.verdict) == (7, "go")
    assert _codes(ws) == ["base", "nothing_short"]


def test_money_that_waited_max_weeks_goes_whatever_the_score():
    dests = [D("VOO", 100.0, "premium")]                                             # 1: hold
    assert _score(dests, weeks=3).verdict == "hold"
    ws = _score(dests, weeks=4)
    assert (ws.score, ws.verdict, ws.fraction, ws.forced) == (1, "go", 1.0, "waited")
    assert ws.parts[-1] == {"sign": "up", "code": "forced_waited", "symbol": None,
                            "text": "Money has waited 4 weeks: it goes in this week."}


def test_the_largest_gaps_all_cheap_force_go():
    # AAPL 300 + KO 250 make half of 1000; both cheap: go, though VOO and JNJ are premium (score 6: half).
    dests = [D("AAPL", 300.0, "cheap"), D("KO", 250.0, "cheap"), D("VOO", 225.0, "premium"), D("JNJ", 225.0, "premium")]
    ws = _score(dests)
    assert ws.score == 7 + 2 - 3 and (ws.verdict, ws.forced) == ("go", "under_and_cheap")
    assert ws.parts[-1]["text"] == ("The holdings furthest under target (AAPL, KO) are cheaper than their own past: "
                                    "it goes in this week.")
    # KO usual instead: the half is not all cheap.
    dests[1] = D("KO", 250.0, "usual")
    assert _score(dests).forced is None


def test_ties_in_gap_go_by_symbol():
    ws = _score([D("BBB", 100.0, "cheap"), D("AAA", 100.0, "premium")])     # AAA first: half reached, not cheap
    assert ws.forced is None


def test_score_is_7_plus_the_parts_points_clamped():
    for dests in ([D("A", 1.0, "cheap")], [D("A", 1.0, "premium"), D("B", 1.0, "premium", drop={"date": "2026-10-30", "move": -0.1})]):
        ws = _score(dests)
        points = sum(int(m.group(1)) for p in ws.parts for m in [re.search(r": ([+-]\d+)\.$", p["text"])] if m)
        assert ws.score == max(0, min(10, 7 + points))


def test_no_text_says_buy_and_no_percent_chance():
    drop = {"date": "2026-10-30", "move": -0.05}
    dests = [D("AAPL", 300.0, "cheap"), D("KO", 200.0, "cheap", known=False), D("VOO", 250.0, "premium", drop=drop)]
    for weeks in (0, 4):
        for p in _score(dests, weeks=weeks).parts:
            assert not re.search(r"\bbuy", p["text"], re.I) and "chance" not in p["text"] and "probab" not in p["text"]
            assert set(p) == {"sign", "code", "symbol", "text"} and p["sign"] in ("up", "down", "neutral")


def test_unexplained_drop_picks_the_newest_none_in_the_window():
    moves = [{"date": "2026-10-10", "move": -0.06, "first": "none"},           # before the 14 days
             {"date": "2026-10-22", "move": -0.04, "first": "market"},
             {"date": "2026-10-24", "move": -0.05, "first": "none"},
             {"date": "2026-10-28", "move": -0.045, "first": "none"}]
    assert W.unexplained_drop(moves, "2026-10-31") == {"date": "2026-10-28", "move": -0.045}
    assert W.unexplained_drop(moves[:2], "2026-10-31") is None
    assert W.unexplained_drop([{"date": "2026-11-02", "move": -0.1, "first": "none"}], "2026-10-31") is None


@pytest.mark.parametrize("weeks,verdict", [(0, "hold"), (3, "hold"), (4, "go"), (9, "go")])
def test_guard_weeks(weeks, verdict):
    assert _score([D("VOO", 1.0, "premium")], weeks=weeks).verdict == verdict
