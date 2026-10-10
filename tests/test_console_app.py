"""The live screen, driven headless: tabs, tables filled from a fixture model, actions refused while a sync
is alive, and a failing refresh that keeps the last good screen."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from textual.widgets import DataTable, TabbedContent

from financial_data_collector import config as C
from financial_data_collector import heartbeat as H
from financial_data_collector.console.actions import LaunchState
from financial_data_collector.console.app import ConsoleApp
from financial_data_collector.console.model import Overview, RunStep, RunSummary, Running, SourceRow, StepState
from financial_data_collector.console.schedule import Schedule, ScheduleResult

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
STEP_NAMES = ("connections", "ingest", "prices", "sec", "derive", "export")


def overview(running=Running("idle")):
    steps = [StepState(s, "ok", 6.0, False, 1, "") for s in STEP_NAMES]
    sources = [SourceRow("inbox files", "x", "never", "empty"), SourceRow("prices", "yfinance", "", "ok")]
    runs = [RunSummary("b", "scheduled", "2026-03-01T11:00:00Z", "2026-03-01T11:03:05Z", 185.0, ("ok",) * 6, 4, 0),
            RunSummary("a", "console", "2026-03-01T10:00:00Z", "2026-03-01T10:00:30Z", 30.0,
                       ("", "", "ok", "", "", ""), 3, 0)]
    return Overview(Path("C:/x"), NOW, steps, sources, 7, runs, running, None)


class FakeLauncher:
    def __init__(self):
        self.started = []
        self.busy = False

    def start(self, step=None):
        self.started.append(step)
        return LaunchState("queued", step, 9, started_at=NOW)

    def state(self, heartbeat):
        return LaunchState("queued", self.started[-1], 9, started_at=NOW) if self.started else LaunchState("idle")

    def clear(self):
        pass


def _cfg(tmp_path: Path):
    C.init_project(tmp_path)
    return C.load_config(tmp_path)


async def _settle(app, pilot, ready, tries=60):
    for _ in range(tries):
        if ready(app):
            return
        await pilot.pause(0.05)
    raise AssertionError("the app never settled")


def _app(cfg, ov=None, load=None, launcher=None):
    ov = ov or overview()
    return ConsoleApp(
        cfg, load=load or (lambda cfg, **kw: ov),
        load_steps=lambda cfg, run_id, **kw: [RunStep("prices", "ok", 4.0, 5, f"steps of {run_id}")],
        launcher=launcher or FakeLauncher(),
        schedule_reader=lambda root: ScheduleResult(Schedule("T", "Ready", None, None, None, ("daily 08:30",), None), ""),
        refresh_seconds=3600, schedule_seconds=3600, now=lambda: NOW)


def test_tabs_tables_and_detail(tmp_path: Path):
    async def go():
        app = _app(_cfg(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(app, pilot, lambda a: a.overview is not None and a.query_one("#steps", DataTable).row_count == 6)
            assert app.status_text.plain.startswith("IDLE")
            await _settle(app, pilot, lambda a: "task T" in a.schedule_text.plain)
            await pilot.press("2")
            assert app.query_one(TabbedContent).active == "sources"
            assert app.query_one("#sources_table", DataTable).row_count == 2
            assert app.universe_line == "following 7 symbols"
            await pilot.press("3")
            assert app.query_one(TabbedContent).active == "runs"
            assert app.query_one("#runs_table", DataTable).row_count == 2
            await _settle(app, pilot, lambda a: a.selected_run == "b" and a.query_one("#run_detail", DataTable).row_count == 1)
            await pilot.press("down")
            await _settle(app, pilot, lambda a: a.selected_run == "a")
            await pilot.press("1")
            assert app.query_one(TabbedContent).active == "overview"
    asyncio.run(go())


def test_run_keys_call_the_launcher_and_are_refused_while_running(tmp_path: Path):
    async def go():
        fake = FakeLauncher()
        app = _app(_cfg(tmp_path), launcher=fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(app, pilot, lambda a: a.overview is not None)
            await pilot.press("r")
            assert fake.started == [None]
            fake.started.clear()
            app.query_one("#steps", DataTable).move_cursor(row=2)
            await pilot.press("s")
            assert fake.started == ["prices"]
            assert "queued" in app.status_text.plain
        hb = H.Heartbeat("r", 1, "scheduled", "2026-03-01T11:58:00Z", "2026-03-01T11:59:50Z", "sec", "")
        fake2 = FakeLauncher()
        app2 = _app(_cfg(tmp_path / "two"), ov=overview(Running("running", hb, 120.0)), launcher=fake2)
        async with app2.run_test(size=(120, 40)) as pilot:
            await _settle(app2, pilot, lambda a: a.overview is not None)
            await pilot.press("r")
            assert fake2.started == []
            assert app2.status_text.plain.startswith("RUNNING")
    asyncio.run(go())


def test_refresh_error_keeps_screen(tmp_path: Path):
    async def go():
        calls = []

        def load(cfg, **kw):
            calls.append(1)
            if len(calls) == 1:
                return overview()
            raise RuntimeError("database is locked")

        app = _app(_cfg(tmp_path), load=load)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(app, pilot, lambda a: a.overview is not None)
            app.refresh_data()
            await _settle(app, pilot, lambda a: a.refresh_error != "")
            assert "refresh failed: database is locked" in app.status_text.plain
            assert app.query_one("#steps", DataTable).row_count == 6
    asyncio.run(go())


def test_bracketed_messages_render_without_markup_errors(tmp_path: Path):
    tricky = "[Errno 13] denied [bold]x[/] [/bogus]"

    def ov_with_markup():
        ov = overview()
        steps = [StepState(s.step, "error", 6.0, False, 0, tricky) for s in ov.steps]
        sources = [SourceRow("inbox files", "C:/[x]/inbox", tricky, "error: " + tricky)]
        return Overview(ov.root, ov.now, steps, sources, 7, ov.runs, ov.running, None)

    async def go():
        app = ConsoleApp(_cfg(tmp_path), load=lambda cfg, **kw: ov_with_markup(),
                         load_steps=lambda cfg, run_id, **kw: [RunStep("prices", "error", 4.0, 0, tricky)],
                         launcher=FakeLauncher(), schedule_reader=lambda root: ScheduleResult(None, tricky),
                         refresh_seconds=3600, schedule_seconds=3600, now=lambda: NOW)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(app, pilot, lambda a: a.overview is not None and a.query_one("#steps", DataTable).row_count == 6)
            await pilot.press("2")
            await pilot.press("3")
            await _settle(app, pilot, lambda a: a.query_one("#run_detail", DataTable).row_count == 1)
            await pilot.pause(0.2)
            assert app.refresh_error == "" and tricky in app.schedule_text.plain
    asyncio.run(go())
