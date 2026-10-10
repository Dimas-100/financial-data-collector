"""fdc console: the live screen. A renderer over console.model whose only side effect is starting `fdc sync`
through console.actions. Keys: 1/2/3 tabs, r run the sync, s run the step under the cursor, ctrl+p palette, q quit."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Callable

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Static, TabbedContent, TabPane
from textual.worker import Worker, WorkerState

from ..config import Config
from ..sync import STEPS
from . import report
from .actions import Launcher
from .model import Overview, RunStep, load_overview, load_run_steps
from .schedule import ScheduleResult, read_schedule

FINISHED_SHOWN_FOR = timedelta(seconds=10)
TAB_TABLES = {"overview": "#steps", "sources": "#sources_table", "runs": "#runs_table"}


class ConsoleCommands(Provider):
    """Palette entries: run the sync, run one step, refresh."""

    def _entries(self):
        app = self.app
        yield "Run sync", "run every step now", app.action_run_sync
        for step in STEPS:
            yield f"Run step: {step}", f"run only the {step} step", partial(app.run_step, step)
        yield "Refresh now", "reload the screen and the schedule", app.action_refresh

    async def discover(self) -> Hits:
        for name, help_, cmd in self._entries():
            yield DiscoveryHit(name, cmd, help=help_)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, help_, cmd in self._entries():
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), cmd, help=help_)


class ConsoleApp(App):
    TITLE = "fdc console"
    CSS = """
    #title { height: 1; padding: 0 1; color: $text-muted; }
    #status { height: 1; padding: 0 1; background: $surface; }
    #schedule { height: auto; padding: 1 1 0 1; }
    #universe { height: 1; padding: 0 1; }
    DataTable { height: 1fr; }
    #runs_table { width: 55%; }
    #run_detail { width: 45%; }
    """
    BINDINGS = [
        Binding("1", "tab('overview')", "Overview"),
        Binding("2", "tab('sources')", "Sources"),
        Binding("3", "tab('runs')", "Runs"),
        Binding("r", "run_sync", "Run sync"),
        Binding("s", "run_step_under_cursor", "Run step"),
        Binding("q", "quit", "Quit"),
    ]
    COMMANDS = App.COMMANDS | {ConsoleCommands}

    def __init__(self, cfg: Config, *, load: Callable[..., Overview] = load_overview,
                 load_steps: Callable[..., list[RunStep]] = load_run_steps, launcher=None,
                 schedule_reader: Callable[[Path], ScheduleResult] = read_schedule,
                 refresh_seconds: float = 2.0, schedule_seconds: float = 300.0,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        super().__init__()
        self.cfg = cfg
        self._load, self._load_steps, self._schedule_reader = load, load_steps, schedule_reader
        self.launcher = launcher if launcher is not None else Launcher(cfg.root)
        self._refresh_seconds, self._schedule_seconds, self._now = refresh_seconds, schedule_seconds, now
        self.overview: Overview | None = None
        self.schedule = ScheduleResult(None, "reading the scheduler...")
        self.refresh_error = ""
        self.selected_run: str | None = None
        self.status_text = Text("loading...")
        self.schedule_text = Text("")
        self.universe_line = ""

    # ---- layout --------------------------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static(Text(f"fdc console  ·  {self.cfg.root}"), id="title")
        yield Static(self.status_text, id="status")
        with TabbedContent(initial="overview"):
            with TabPane("Overview", id="overview"):
                yield DataTable(id="steps", cursor_type="row", zebra_stripes=True)
                yield Static(self.schedule_text, id="schedule")
            with TabPane("Sources", id="sources"):
                yield Static("", id="universe")
                yield DataTable(id="sources_table", cursor_type="row", zebra_stripes=True)
            with TabPane("Runs", id="runs"):
                with Horizontal():
                    yield DataTable(id="runs_table", cursor_type="row", zebra_stripes=True)
                    yield DataTable(id="run_detail", cursor_type="none")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#steps", DataTable).add_columns("step", "status", "age", "rows", "now", "message")
        self.query_one("#sources_table", DataTable).add_columns("source", "where / how", "last", "state")
        self.query_one("#runs_table", DataTable).add_columns("started", "origin", "took", "steps", "rows")
        self.query_one("#run_detail", DataTable).add_columns("step", "status", "took", "rows", "message")
        self._render_schedule()
        self.refresh_data()
        self.refresh_schedule()
        self.set_interval(self._refresh_seconds, self.refresh_data)
        self.set_interval(self._schedule_seconds, self.refresh_schedule)
        self.set_interval(1.0, self._render_status)

    # ---- background reads ----------------------------------------------------------------------------
    def refresh_data(self) -> None:
        cfg, sched, now = self.cfg, self.schedule, self._now()
        self.run_worker(lambda: self._load(cfg, now=now, schedule=sched.schedule, schedule_reason=sched.reason),
                        name="refresh", group="refresh", exclusive=True, thread=True, exit_on_error=False)

    def refresh_schedule(self) -> None:
        root = self.cfg.root
        self.run_worker(lambda: self._schedule_reader(root), name="schedule", group="schedule", exclusive=True,
                        thread=True, exit_on_error=False)

    def _load_detail(self, run_id: str) -> None:
        cfg = self.cfg
        self.run_worker(lambda: (run_id, self._load_steps(cfg, run_id)), name="steps", group="steps",
                        exclusive=True, thread=True, exit_on_error=False)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        w = event.worker
        if w.state == WorkerState.SUCCESS:
            if w.name == "refresh":
                self.refresh_error = ""
                self._apply(w.result)
            elif w.name == "schedule":
                self.schedule = w.result
                self._render_schedule()
            elif w.name == "steps":
                self._fill_detail(*w.result)
        elif w.state == WorkerState.ERROR:
            if w.name == "refresh":
                self.refresh_error = f"refresh failed: {w.error}"
                self._render_status()
            elif w.name == "schedule":
                self.schedule = ScheduleResult(None, f"scheduler: {w.error}")
                self._render_schedule()

    # ---- rendering -----------------------------------------------------------------------------------
    def _apply(self, ov: Overview) -> None:
        self.overview = ov
        steps = self.query_one("#steps", DataTable)
        row = steps.cursor_row
        steps.clear()
        for s in ov.steps:
            steps.add_row(*report.step_row(s))
        steps.move_cursor(row=min(row, max(steps.row_count - 1, 0)))
        sources = self.query_one("#sources_table", DataTable)
        sources.clear()
        for s in ov.sources:
            sources.add_row(*report.source_row(s))
        self.universe_line = report.universe_text(ov)
        self.query_one("#universe", Static).update(Text(self.universe_line))
        runs = self.query_one("#runs_table", DataTable)
        runs.clear()
        for r in ov.runs:
            runs.add_row(*report.run_row(r), key=r.run_id)
        if ov.runs:
            keys = [r.run_id for r in ov.runs]
            target = self.selected_run if self.selected_run in keys else keys[0]
            runs.move_cursor(row=keys.index(target))
            if target != self.selected_run or self.query_one("#run_detail", DataTable).row_count == 0:
                self.selected_run = target
                self._load_detail(target)
        self._render_status()

    def _fill_detail(self, run_id: str, steps: list[RunStep]) -> None:
        if run_id != self.selected_run:
            return
        detail = self.query_one("#run_detail", DataTable)
        detail.clear()
        for s in steps:
            detail.add_row(*report.run_step_row(s))

    def _render_status(self) -> None:
        if self.overview is None:
            text = Text("loading...")
        else:
            now = self._now()
            state = self.launcher.state(self.overview.running.heartbeat)
            if state.kind == "finished" and state.finished_at and now - state.finished_at > FINISHED_SHOWN_FOR:
                self.launcher.clear()
                state = self.launcher.state(self.overview.running.heartbeat)
            text = report.status_text(self.overview, state, now=now)
        if self.refresh_error:
            text.append(f"  ·  {self.refresh_error}", style="red")
        self.status_text = text
        self.query_one("#status", Static).update(text)

    def _render_schedule(self) -> None:
        self.schedule_text = report.schedule_text(self.schedule.schedule, self.schedule.reason)
        self.query_one("#schedule", Static).update(self.schedule_text)

    # ---- actions -------------------------------------------------------------------------------------
    def action_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab
        self.query_one(TAB_TABLES[tab], DataTable).focus()

    def action_run_sync(self) -> None:
        self.run_step(None)

    def action_run_step_under_cursor(self) -> None:
        steps = self.query_one("#steps", DataTable)
        if steps.row_count:
            self.run_step(STEPS[steps.cursor_row])

    def action_refresh(self) -> None:
        self.refresh_data()
        self.refresh_schedule()

    def run_step(self, step: str | None) -> None:
        if self.overview is not None and self.overview.running.state == "running":
            self.notify("a sync is running; wait for it to finish", severity="warning")
            return
        if self.launcher.busy:
            self.notify("the console's own run is still going", severity="warning")
            return
        state = self.launcher.start(step)
        if state.kind == "failed":
            self.notify(state.error, severity="error", markup=False)      # an OS error may hold brackets
        else:
            self.notify(f"started {state.what}", markup=False)
        self._render_status()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "runs_table" or event.row_key is None:
            return
        run_id = event.row_key.value
        if run_id and run_id != self.selected_run:
            self.selected_run = run_id
            self._load_detail(run_id)
