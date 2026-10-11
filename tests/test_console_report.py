"""The one-shot report and the row builders the screen shares: every state renders, long text is cut in tables and
shown whole in the detail strips, paths are short, numbers carry separators, dates show once per day."""
from datetime import datetime, timezone
from pathlib import Path

from rich.text import Text

from financial_data_collector import heartbeat as H
from financial_data_collector import ui
from financial_data_collector.console import report as R
from financial_data_collector.console.actions import LaunchState
from financial_data_collector.console.model import Overview, RunStep, RunSummary, Running, SourceRow, StepState
from financial_data_collector.console.schedule import Schedule

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
UTC = timezone.utc
ROOT = Path("C:/x/fdc")


def overview(running=Running("idle"), runs=None, schedule=None, reason="no scheduler integration on this platform"):
    steps = [StepState("connections", "skipped", 6.0, False, 0, "no connections"),
             StepState("ingest", "ok", 6.0, False, 19, ""),
             StepState("prices", "ok", 51.0, True, 375, "x" * 200, now="running"),
             StepState("sec", "never", None, False, 0, ""),
             StepState("derive", "error", 6.0, False, 5543, "boom"),
             StepState("export", "ok", 6.0, False, 3, "written", now="done")]
    sources = [SourceRow("inbox files", "C:/x/fdc/inbox", "never", "2 files waiting"),
               SourceRow("connection: simplefin", "3 accounts", "03-01 06:00", "error: HTTP 503"),
               SourceRow("watchlist", "C:/x/fdc/watchlist.txt", "", "missing"),
               SourceRow("-> cockpit feed files", "C:/x/investing/data", "", "off")]
    if runs is None:
        runs = [RunSummary("20260301T110000Z", "scheduled", "2026-03-01T11:00:00Z", "2026-03-01T11:03:05Z", 185.0,
                           ("skipped", "ok", "ok", "ok", "error", "ok"), 400, 1),
                RunSummary("20260301T100000Z", "console", "2026-03-01T10:00:00Z", "2026-03-01T10:00:30Z", 30.0,
                           ("", "", "ok", "", "", ""), 1475730, 0),
                RunSummary("20260228T200000Z", "unknown", "2026-02-28T20:00:00Z", "2026-02-28T20:04:00Z", 240.0,
                           ("ok",) * 6, 5, 0)]
    return Overview(ROOT, NOW, steps, sources, 42, runs, running, "2026-02-28", schedule, reason)


def test_status_text_states():
    assert R.status_text(overview(), tz=UTC).plain == "IDLE  last run 03-01 11:00 (scheduled, 1 error)"
    assert R.status_text(overview(runs=[]), tz=UTC).plain == "IDLE  no runs yet"
    hb = H.Heartbeat("r", 4242, "console", "2026-03-01T11:58:00Z", "2026-03-01T11:59:50Z", "prices", "3/5")
    text = R.status_text(overview(Running("running", hb, 120.0)), tz=UTC).plain
    assert text == "RUNNING  prices · 3/5 · started 03-01 11:58 by console · pid 4242 · 2 min 00 s"
    later = R.status_text(overview(Running("running", hb, 120.0)), now=NOW.replace(minute=5), tz=UTC).plain
    assert later.endswith("7 min 00 s")
    stale = R.status_text(overview(Running("stale", hb, 120.0)), tz=UTC).plain
    assert stale == "DID NOT FINISH  run r started 03-01 11:58 by console (pid 4242 gone)"
    queued = R.status_text(overview(), LaunchState("queued", "prices", 9), tz=UTC).plain
    assert queued.endswith("  ·  console step prices queued")
    done = R.status_text(overview(), LaunchState("finished", None, 9, 0, NOW, NOW), tz=UTC).plain
    assert done.endswith("  ·  FINISHED exit 0 · 12:00:00")
    failed = R.status_text(overview(), LaunchState("failed", None, error="could not start: nope"), tz=UTC).plain
    assert failed.endswith("  ·  LAUNCH FAILED · could not start: nope")


def test_status_text_names_the_next_run_and_the_kind():
    sched = Schedule("T", "Ready", "2026-03-01T08:30:00", 0, "2026-03-01T20:30:00", ("daily 08:30",), None)
    text = R.status_text(overview(schedule=sched), tz=UTC).plain
    assert text == "IDLE  last run 03-01 11:00 (scheduled, 1 error)  ·  next 03-01 20:30"
    assert R.status_kind(overview()) == "idle"
    hb = H.Heartbeat("r", 1, "console", "2026-03-01T11:58:00Z", "2026-03-01T11:59:50Z", "prices", "")
    assert R.status_kind(overview(Running("running", hb, 1.0))) == "running"
    assert R.status_kind(overview(Running("stale", hb, 1.0))) == "stale"
    assert R.status_kind(overview(), LaunchState("queued", None, 9)) == "queued"
    assert R.status_kind(overview(), LaunchState("finished", None, 9, 1, NOW, NOW)) == "failed"
    assert R.status_kind(overview(), LaunchState("finished", None, 9, 0, NOW, NOW)) == "finished"


def test_step_rows_merge_the_marker_cut_the_message_and_format_numbers():
    rows = [R.step_row(s) for s in overview().steps]
    assert [len(r) for r in rows] == [5] * 6
    assert rows[2][1].plain == "running..." and rows[2][2].plain == "2.1 d ago"
    assert rows[4][1].plain == "error" and rows[4][3].plain == "5,543"
    assert rows[5][1].plain == "ok"                      # "done" adds nothing: the status already is this run's
    assert rows[2][4].plain.endswith("...") and len(rows[2][4].plain) == R.SHORT_MESSAGE
    assert rows[0][4].plain == "no connections"
    assert R.shorten("abc", 5) == "abc" and R.shorten("abcdefgh", 5) == "ab..."


def test_short_path():
    assert R.short_path("C:/x/fdc/inbox", ROOT) == "inbox"
    assert R.short_path("C:/x/investing/data", ROOT).replace("\\", "/") == "../investing/data"
    assert R.short_path("D:/elsewhere/file.csv", ROOT) == "D:/elsewhere/file.csv"
    assert R.short_path("Tiingo", ROOT) == "Tiingo" and R.short_path("3 accounts", ROOT) == "3 accounts"
    assert R.short_path("", ROOT) == ""


def test_source_rows_lead_with_state_and_shorten_where():
    row = R.source_row(overview().sources[0], ROOT)
    assert [c.plain for c in row] == ["inbox files", "2 files waiting", "never", "inbox"]
    assert all(isinstance(c, Text) for c in row)
    assert R.source_row(overview().sources[1], ROOT)[1].plain == "error: HTTP 503"
    assert R.source_row(overview().sources[3], ROOT)[3].plain.replace("\\", "/") == "../investing/data"
    long_last = SourceRow("-> cockpit feed files", "C:/x/investing/data", "10-10 16:38: " + "prices.json unchanged, " * 4, "ok")
    cut = R.source_row(long_last, ROOT)[2].plain
    assert len(cut) == R.SHORT_LAST and cut.endswith("...")           # the strip holds the whole text


def test_run_rows_show_the_date_once_per_day_and_dim_unknown():
    rows = R.run_rows(overview().runs, tz=UTC)
    assert [r[0].plain for r in rows] == ["03-01", "", "02-28"]
    assert [r[1].plain for r in rows] == ["11:00", "10:00", "20:00"]
    assert [r[2].plain for r in rows] == ["scheduled", "console", "unknown"]
    assert rows[2][2].style == "dim" and rows[0][2].style != "dim"
    assert [r[3].plain for r in rows] == ["3 min 05 s", "30 s", "4 min 00 s"]
    assert rows[0][4].plain == "soooeo" and rows[1][4].plain == "--o---"
    assert rows[1][5].plain == "1,475,730"
    assert R.badges(("skipped", "ok", "", "error", "ok", "ok")).plain == "so-eoo"


def test_detail_texts_hold_everything():
    step = R.step_detail(overview().steps[2]).plain
    assert step.startswith("prices") and "x" * 200 in step and "2.1 d ago" in step and "STALE" in step
    src = R.source_detail(overview().sources[1]).plain
    assert "connection: simplefin" in src and "3 accounts" in src and "error: HTTP 503" in src and "03-01 06:00" in src
    run = overview().runs[0]
    detail = R.run_detail(run, [RunStep("ingest", "ok", 1.0, 19, "m"), RunStep("prices", "error", 4.0, 0, "boom boom")],
                          tz=UTC).plain
    assert detail.startswith("run 20260301T110000Z") and "scheduled" in detail and "03-01 11:00" in detail
    assert "3 min 05 s" in detail and "ingest" in detail and "boom boom" in detail and "1 error" in detail
    assert R.run_detail(run, [], tz=UTC).plain.endswith("loading...")


def test_schedule_text():
    assert R.schedule_text(None, "not here").plain == "schedule: not here"
    s = Schedule("T", "Ready", "2026-10-10T12:42:14", 0, "2026-10-10T20:30:00", ("at logon +20 min", "daily 08:30"),
                 Path("C:/l/sync.log"))
    text = R.schedule_text(s, "").plain
    assert text.splitlines()[0] == "T   Ready"
    assert text.splitlines()[1] == "at logon +20 min · daily 08:30"
    assert text.splitlines()[2] == "last 10-10 12:42 (exit 0)   next 10-10 20:30   log C:\\l\\sync.log"
    never = Schedule("T", "Ready", None, 267011, None, (), None)
    plain = R.schedule_text(never, "").plain
    assert "last never   next none" in plain and "exit" not in plain and "none" in plain.splitlines()[1]


def test_render_once_has_every_section():
    text = "".join(ui.to_text(r, width=160) for r in R.render_once(overview()))
    for needle in ("IDLE", "steps", "connections", "running...", "schedule: no scheduler integration",
                   "sources (following 42 symbols)", "2 files waiting", "runs (last 10)", "soooeo", "--o---",
                   "1,475,730", "03-01", "02-28"):
        assert needle in text, needle


def test_external_strings_are_plain_text_not_markup():
    """Messages, paths and error texts come from outside; Textual parses bare strings as markup, so every such
    cell is a Text whose plain form is the original."""
    tricky = "[Errno 13] denied [bold]x[/] [/bogus]"
    step = R.step_row(StepState("prices", "error", 1.0, False, 0, tricky))
    assert isinstance(step[4], Text) and step[4].plain == tricky
    src = R.source_row(SourceRow("inbox files", "C:/x/fdc/inbox", tricky, "error: " + tricky), ROOT)
    assert all(isinstance(c, Text) for c in src) and src[3].plain == "inbox" and src[2].plain == tricky
    assert R.step_detail(StepState("prices", "error", 1.0, False, 0, tricky)).plain.count(tricky) == 1
    assert R.run_detail(overview().runs[0], [RunStep("prices", "error", 1.0, 0, tricky)], tz=UTC).plain.count(tricky) == 1
    assert R.status_text(overview(Running("running", H.Heartbeat("r", 1, "console", "2026-03-01T11:58:00Z",
                                                                  "2026-03-01T11:59:50Z", "prices", tricky), 1.0)),
                         tz=UTC).plain.count(tricky) == 1
