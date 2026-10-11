"""fdc console: the live screen. A renderer over console.model whose only side effect is starting `fdc sync`
through console.actions. Every tab is a table with a detail strip under it: up/down move the highlight, the strip
shows everything about the highlighted row. Keys: 1/2/3 or left/right tabs, r run the sync, s run the step under
the cursor, ? help, ctrl+p palette, q quit."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Callable

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Static, TabbedContent, TabPane
from textual.worker import Worker, WorkerState

from ..config import Config
from ..sync import STEPS
from . import report
from .actions import Launcher
from .model import RUNS_DAYS, Overview, RunStep, load_overview, load_run_steps
from .schedule import ScheduleResult, read_schedule

FINISHED_SHOWN_FOR = timedelta(seconds=10)
TABS = ("overview", "sources", "runs")
TAB_TABLES = {"overview": "#steps", "sources": "#sources_table", "runs": "#runs_table"}

HELP_TEXT = """\
[b]fdc console[/b]  what runs, where its data comes from, when it ran

[b]1[/b] [b]2[/b] [b]3[/b]   Overview, Sources, Runs        [b]left[/b] / [b]right[/b]   previous / next tab
[b]up[/b] / [b]down[/b]   move the highlight; the strip below shows the whole row
[b]tab[/b]         move between the table and its strip (scroll a long detail with up/down)
[b]r[/b]           run the whole sync now           [b]s[/b]   run the step under the cursor
[b]ctrl+p[/b]      command palette (run a step by name, refresh)
[b]?[/b]           this help                        [b]q[/b]   quit (a running sync carries on)

Status band: green idle, cyan running, yellow queued, red did not finish.
Steps: [b]STALE[/b] marks a step whose last run is older than its threshold.
Runs: badges in step order (o ok, s skipped, e error, - not run); unknown origin predates the origin column.
"""


class ConsoleCommands(Provider):
    """Palette entries: run the sync, run one step, refresh."""

    def _entries(self):
        app = self.app
        yield "Run sync", "run every step now", app.action_run_sync
        for step in STEPS:
            yield f"Run step: {step}", f"run only the {step} step", partial(app.run_step, step)
        yield "Refresh now", "reload the screen and the schedule", app.action_refresh
        yield "Help", "every key on one screen", app.action_help

    async def discover(self) -> Hits:
        for name, help_, cmd in self._entries():
            yield DiscoveryHit(name, cmd, help=help_)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, help_, cmd in self._entries():
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), cmd, help=help_)


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Close"), Binding("question_mark", "app.pop_screen", "Close"),
                Binding("q", "app.pop_screen", "Close")]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    #help { width: 96; height: auto; border: round $primary; padding: 1 2; background: $surface; }
    """

    def compose(self) -> ComposeResult:
        yield Static(HELP_TEXT, id="help")


class RowTable(DataTable):
    """A row-cursor table whose left/right keys switch tabs instead of moving a cell cursor."""

    def action_cursor_left(self) -> None:
        self.app.action_prev_tab()

    def action_cursor_right(self) -> None:
        self.app.action_next_tab()


class DetailPane(VerticalScroll):
    """The strip under a table: the whole highlighted row, scrollable when long; left/right switch tabs."""

    def action_scroll_left(self) -> None:
        self.app.action_prev_tab()

    def action_scroll_right(self) -> None:
        self.app.action_next_tab()


class ConsoleApp(App):
    TITLE = "fdc console"
    CSS = """
    #title { height: 1; padding: 0 1; color: $text-muted; }
    #status { height: 1; padding: 0 1; }
    #status.idle { background: $success 25%; }
    #status.finished { background: $success 25%; }
    #status.running { background: $accent 30%; }
    #status.queued { background: $warning 30%; }
    #status.stale { background: $error 35%; }
    #status.failed { background: $error 35%; }
    TabbedContent { height: 1fr; }
    TabbedContent > ContentSwitcher { height: 1fr; }
    TabPane { height: 1fr; padding: 0; }
    RowTable { height: 1fr; border: round $primary; border-title-color: $text; border-title-style: bold; }
    #steps, #sources_table { height: auto; max-height: 50%; }
    DetailPane { height: 1fr; border: round $secondary; border-title-color: $text-muted; padding: 0 1; }
    #run_detail { height: 16; }
    #schedule { height: 5; border: round $secondary; border-title-color: $text-muted; padding: 0 1; }
    """
    BINDINGS = [
        Binding("1", "tab('overview')", "Overview"),
        Binding("2", "tab('sources')", "Sources"),
        Binding("3", "tab('runs')", "Runs"),
        Binding("left", "prev_tab", "Prev tab", show=False),
        Binding("right", "next_tab", "Next tab", show=False),
        Binding("r", "run_sync", "Run sync"),
        Binding("s", "run_step_under_cursor", "Run step"),
        Binding("question_mark", "help", "Help"),
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
        self.step_detail_text = Text("")
        self.source_detail_text = Text("")
        self.run_detail_text = Text("")

    # ---- layout --------------------------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static(Text(f"fdc console  ·  {self.cfg.root}"), id="title")
        yield Static(self.status_text, id="status", classes="idle")
        with TabbedContent(initial="overview"):
            with TabPane("Overview", id="overview"):
                yield RowTable(id="steps", cursor_type="row", zebra_stripes=True)
                with DetailPane(id="step_detail"):
                    yield Static("", id="step_detail_text")
                yield Static(self.schedule_text, id="schedule")
            with TabPane("Sources", id="sources"):
                yield RowTable(id="sources_table", cursor_type="row", zebra_stripes=True)
                with DetailPane(id="source_detail"):
                    yield Static("", id="source_detail_text")
            with TabPane("Runs", id="runs"):
                yield RowTable(id="runs_table", cursor_type="row", zebra_stripes=True)
                with DetailPane(id="run_detail"):
                    yield Static("", id="run_detail_text")
        yield Footer()

    def on_mount(self) -> None:
        steps = self.query_one("#steps", DataTable)
        steps.add_columns("step", "status", "age", "rows", "message")
        steps.border_title = "steps"
        self.query_one("#step_detail").border_title = "step"
        self.query_one("#schedule").border_title = "schedule"
        sources = self.query_one("#sources_table", DataTable)
        sources.add_columns("source", "state", "last", "where / how")
        sources.border_title = "sources"
        self.query_one("#source_detail").border_title = "source"
        runs = self.query_one("#runs_table", DataTable)
        runs.add_columns("date", "time", "origin", "took", "steps", "rows")
        runs.border_title = f"runs · last {RUNS_DAYS} days"
        self.query_one("#run_detail").border_title = "run"
        self._render_schedule()

    def on_ready(self) -> None:
        """The first read starts once every widget, including the strips' nested text, is mounted."""
        self.query_one("#steps", DataTable).focus()       # arrow keys work from the first moment
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
                self._render_status()
            elif w.name == "steps":
                self._fill_run_detail(*w.result)
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
        self._show_step(steps.cursor_row)
        sources = self.query_one("#sources_table", DataTable)
        row = sources.cursor_row
        sources.clear()
        for s in ov.sources:
            sources.add_row(*report.source_row(s, ov.root))
        sources.border_title = f"sources · {report.universe_text(ov)}"
        sources.move_cursor(row=min(row, max(sources.row_count - 1, 0)))
        self._show_source(sources.cursor_row)
        runs = self.query_one("#runs_table", DataTable)
        runs.clear()
        for r, cells in zip(ov.runs, report.run_rows(ov.runs)):
            runs.add_row(*cells, key=r.run_id)
        if ov.runs:
            keys = [r.run_id for r in ov.runs]
            target = self.selected_run if self.selected_run in keys else keys[0]
            runs.move_cursor(row=keys.index(target))
            if target != self.selected_run or not self.run_detail_text.plain:
                self._select_run(target)
        self._render_status()

    def _show_step(self, row: int) -> None:
        if self.overview is None or not 0 <= row < len(self.overview.steps):
            return
        self.step_detail_text = report.step_detail(self.overview.steps[row])
        self.query_one("#step_detail_text", Static).update(self.step_detail_text)

    def _show_source(self, row: int) -> None:
        if self.overview is None or not 0 <= row < len(self.overview.sources):
            return
        self.source_detail_text = report.source_detail(self.overview.sources[row])
        self.query_one("#source_detail_text", Static).update(self.source_detail_text)

    def _select_run(self, run_id: str) -> None:
        self.selected_run = run_id
        self._fill_run_detail(run_id, [])
        self._load_detail(run_id)

    def _fill_run_detail(self, run_id: str, steps: list[RunStep]) -> None:
        if run_id != self.selected_run or self.overview is None:
            return
        run = next((r for r in self.overview.runs if r.run_id == run_id), None)
        if run is None:
            return
        self.run_detail_text = report.run_detail(run, steps)
        self.query_one("#run_detail_text", Static).update(self.run_detail_text)

    def _render_status(self) -> None:
        if self.overview is None:
            text, kind = Text("loading..."), "idle"
        else:
            now = self._now()
            state = self.launcher.state(self.overview.running.heartbeat)
            if state.kind == "finished" and state.finished_at and now - state.finished_at > FINISHED_SHOWN_FOR:
                self.launcher.clear()
                state = self.launcher.state(self.overview.running.heartbeat)
            text = report.status_text(self.overview, state, now=now, schedule=self.schedule.schedule)
            kind = report.status_kind(self.overview, state)
        if self.refresh_error:
            text.append(f"  ·  {self.refresh_error}", style="red")
        self.status_text = text
        status = self.query_one("#status", Static)
        status.set_classes(kind)
        status.update(text)

    def _render_schedule(self) -> None:
        self.schedule_text = report.schedule_text(self.schedule.schedule, self.schedule.reason)
        self.query_one("#schedule", Static).update(self.schedule_text)

    # ---- actions -------------------------------------------------------------------------------------
    def action_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab
        self.query_one(TAB_TABLES[tab], DataTable).focus()

    def action_next_tab(self) -> None:
        current = self.query_one(TabbedContent).active
        self.action_tab(TABS[(TABS.index(current) + 1) % len(TABS)] if current in TABS else TABS[0])

    def action_prev_tab(self) -> None:
        current = self.query_one(TabbedContent).active
        self.action_tab(TABS[(TABS.index(current) - 1) % len(TABS)] if current in TABS else TABS[0])

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

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
        table_id = event.data_table.id
        if table_id == "steps":
            self._show_step(event.cursor_row)
        elif table_id == "sources_table":
            self._show_source(event.cursor_row)
        elif table_id == "runs_table" and event.row_key is not None:
            run_id = event.row_key.value
            if run_id and run_id != self.selected_run:
                self._select_run(run_id)
