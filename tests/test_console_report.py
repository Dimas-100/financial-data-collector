"""The one-shot report and the row builders the screen shares: every state renders, long messages are cut."""
from datetime import datetime, timezone
from pathlib import Path

from financial_data_collector import heartbeat as H
from financial_data_collector import ui
from financial_data_collector.console import report as R
from financial_data_collector.console.actions import LaunchState
from financial_data_collector.console.model import Overview, RunStep, RunSummary, Running, SourceRow, StepState
from financial_data_collector.console.schedule import Schedule

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
UTC = timezone.utc


def overview(running=Running("idle"), runs=None, schedule=None, reason="no scheduler integration on this platform"):
    steps = [StepState("connections", "skipped", 6.0, False, 0, "no connections"),
             StepState("ingest", "ok", 6.0, False, 19, ""),
             StepState("prices", "ok", 51.0, True, 375, "x" * 200, now="running"),
             StepState("sec", "never", None, False, 0, ""),
             StepState("derive", "error", 6.0, False, 0, "boom"),
             StepState("export", "ok", 6.0, False, 3, "written", now="done")]
    sources = [SourceRow("inbox files", "C:/x/inbox", "never", "2 files waiting"),
               SourceRow("connection: simplefin", "3 accounts", "03-01 06:00", "error: HTTP 503"),
               SourceRow("watchlist", "C:/x/watchlist.txt", "", "missing"),
               SourceRow("-> cockpit feed files", "", "", "off")]
    if runs is None:
        runs = [RunSummary("20260301T110000Z", "scheduled", "2026-03-01T11:00:00Z", "2026-03-01T11:03:05Z", 185.0,
                           ("skipped", "ok", "ok", "ok", "error", "ok"), 400, 1),
                RunSummary("20260301T100000Z", "console", "2026-03-01T10:00:00Z", "2026-03-01T10:00:30Z", 30.0,
                           ("", "", "ok", "", "", ""), 3, 0)]
    return Overview(Path("C:/x"), NOW, steps, sources, 42, runs, running, "2026-02-28", schedule, reason)


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


def test_rows_and_badges():
    assert R.badges(("skipped", "ok", "", "error", "ok", "ok")).plain == "so-eoo"
    row = R.step_row(overview().steps[2])
    assert row[0] == "prices" and row[1].plain == "ok STALE" and row[2] == "2.1 d ago" and row[4].plain == "running"
    assert len(row[5]) == R.MESSAGE_WIDTH
    assert R.source_row(overview().sources[1])[3].plain == "error: HTTP 503"
    assert R.run_row(overview().runs[0], tz=UTC)[:3] == ["03-01 11:00", "scheduled", "3 min 05 s"]
    assert R.run_step_row(RunStep("prices", "ok", 4.0, 5, "m"))[2] == "4 s"
    assert R.universe_text(overview()) == "following 42 symbols"


def test_schedule_text():
    assert R.schedule_text(None, "not here").plain == "schedule: not here"
    s = Schedule("T", "Ready", "2026-10-10T12:42:14", 0, "2026-10-10T20:30:00", ("at logon +20 min", "daily 08:30"),
                 Path("C:/l/sync.log"))
    text = R.schedule_text(s, "").plain
    assert "task T   state Ready   last 10-10 12:42 (exit 0)   next 10-10 20:30" in text
    assert "triggers at logon +20 min · daily 08:30" in text and "log C:" in text
    never = Schedule("T", "Ready", None, 267011, None, (), None)
    plain = R.schedule_text(never, "").plain
    assert "last never   next none" in plain and "exit" not in plain and "triggers none" in plain


def test_render_once_has_every_section():
    text = "".join(ui.to_text(r, width=160) for r in R.render_once(overview()))
    for needle in ("IDLE", "steps", "connections", "ok STALE", "running", "schedule: no scheduler integration",
                   "sources (following 42 symbols)", "2 files waiting", "runs (last 10)", "soooeo", "--o---"):
        assert needle in text, needle


def test_external_strings_are_plain_text_not_markup():
    """Messages, paths and error texts come from outside; Textual parses bare strings as markup, so every such
    cell is a Text whose plain form is the original."""
    from rich.text import Text
    tricky = "[Errno 13] denied [bold]x[/] [/bogus]"
    step = R.step_row(StepState("prices", "error", 1.0, False, 0, tricky))
    assert isinstance(step[5], Text) and step[5].plain == tricky
    src = R.source_row(SourceRow("inbox files", "C:/[x]/inbox", tricky, "error: " + tricky))
    assert all(isinstance(c, Text) for c in src) and src[1].plain == "C:/[x]/inbox" and src[2].plain == tricky
    detail = R.run_step_row(RunStep("prices", "error", 1.0, 0, tricky))
    assert isinstance(detail[4], Text) and detail[4].plain == tricky
    assert R.status_text(overview(Running("running", H.Heartbeat("r", 1, "console", "2026-03-01T11:58:00Z",
                                                                  "2026-03-01T11:59:50Z", "prices", tricky), 1.0)),
                         tz=UTC).plain.count(tricky) == 1
