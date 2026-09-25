from financial_data_collector import ui


def test_fmt_number():
    assert ui.fmt_number(None) == ""
    assert ui.fmt_number(1234567) == "1,234,567"
    assert ui.fmt_number(1234.5) == "1,234.50"
    assert ui.fmt_number(10.0) == "10"
    assert ui.fmt_number(0.123456) == "0.1235"
    assert ui.fmt_number(-0.5) == "-0.5"
    assert ui.fmt_number(float("nan")) == ""
    assert ui.fmt_number("AAPL") == "AAPL"
    assert ui.fmt_number(True) == "True"


def test_table_formats_and_aligns_numbers():
    t = ui.table(["symbol", "quantity", "market_value", "note"],
                 [["AAPL", 10.0, 1234.5, None], ["KO", 2.5, None, "x"]])
    text = ui.to_text(t, width=80)
    lines = [l for l in text.splitlines() if "AAPL" in l or "KO" in l]
    assert "1,234.50" in lines[0] and "10" in lines[0]
    assert "2.5" in lines[1] and "x" in lines[1]
    # numeric columns are right-aligned: the shorter number ends where the longer one does
    assert lines[0].index("10") + 2 == lines[1].index("2.5") + 3


def test_table_footer_and_empty():
    text = ui.to_text(ui.table(["a"], [], footer="no rows"), width=40)
    assert "no rows" in text and "a" in text


def test_badge_styles():
    assert ui.badge("ok").style == "green"
    assert ui.badge("error").style == "red"
    assert ui.badge("skipped").style == "yellow"
    assert ui.badge("whatever").plain == "whatever"


def test_error_and_hint_go_to_the_right_streams(capsys):
    ui.error("broken")
    ui.hint("try this")
    out, err = capsys.readouterr()
    assert "error: broken" in err and "hint: try this" in out
