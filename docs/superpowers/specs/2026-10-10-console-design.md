# fdc console: a live screen for what the collector runs, from where, and when

Date: 2026-10-10. Status: approach A (Textual) and the scope below were agreed in conversation.

## Purpose

The collector runs unattended: a scheduled task, a sibling project that starts that task after its
own refresh, and the occasional `fdc sync` by hand. Today the only windows into that are `fdc status`
(the last run per step) and a log file. Nothing shows that a sync is in progress, who started a run,
how often runs actually happen, or where each kind of data comes from. On 2026-10-10 the sync ran
three times in sixteen minutes and nothing surfaced it.

`fdc console` is a terminal screen that answers three questions at a glance and keeps answering them
while it stays open:

1. **What runs here, and what state is it in?** Each sync step, its last result, how stale it is, and
   whether a sync is running right now and how far along it is.
2. **Where does the information come from?** Every source the configuration names (inbox files, a
   broker export folder, connections, the price provider, SEC EDGAR, the watchlist, a research tickers
   folder) and every output (the cockpit feed files), each with its last activity.
3. **When did it run, and who started it?** The history of runs with their origin (scheduled, manual,
   console), duration and per-step results, down to the full message of any step.

It also carries the first action: run the whole sync or one step from the screen and watch it finish.
The action path is designed so the next spec ("collect a symbol I name") adds a command without
changing the screen's machinery.

## Who it is for

Anyone running the collector, with the owner as one instance. A stranger who installed `fdc`, dropped
a Fidelity CSV in `inbox/` and registered the scheduled task with `scripts/schedule_sync.ps1` sees a
complete, correct screen. Nothing in it names the owner's sibling projects, task names beyond what the
repo's own script registers, or account labels.

## Scope

In:

- `fdc console`: a Textual app. Three tabs (Overview, Sources, Runs), a status bar that always shows
  the running sync, key bindings and a command palette.
- `fdc console --once`, and `fdc console` without a terminal: the same content printed once as rich
  tables, then exit.
- A heartbeat file a running sync keeps current, so the screen (and any other process) can see a sync in
  progress, its step, its progress line, its process id and its origin.
- An `origin` on every logged run: `scheduled`, `manual` or `console` (`fdc sync --origin`);
  `scripts/run_sync.ps1` passes `scheduled`.
- A sync that finds another sync alive waits its turn (up to 30 minutes) instead of contending for the
  database, mirroring what the scheduled task already does for its own instances.
- A schedule panel that reads the operating system's registration of the sync task on Windows.
- Actions: run the full sync, run one step. Disabled while a sync is alive.

Out (non-goals):

- Any money view: balances, holdings, performance. Kestrel is the warehouse's one dashboard for that.
- Research or orchestration of work outside this project.
- Editing configuration, connections or accounts from the screen (`fdc connect`, `fdc accounts set`
  already exist).
- A log viewer. The schedule panel shows where the task's log is; a pager is better at reading it.
- Scheduler integration beyond Windows Task Scheduler. On other platforms the panel says so.
- "Collect a symbol I name": the next spec. This one only leaves the hook.

## Approach

A Textual application. Textual is the terminal framework from the authors of rich, which already
renders all of `fdc`'s output; it provides focusable tables, timers, workers for background reads and
subprocesses, a command palette, and a headless test driver (`App.run_test()`) that runs under pytest
without a terminal. One new dependency: `textual>=8`.

Rejected: a `rich.Live` loop with hand-written key reading (no dependency, but every interaction the
owner wants later, a prompt, focus, scrolling, a text input, is built by hand); a one-shot report under
a watch loop (cannot take input); a local web page (duplicates kestrel's role).

The screen is a renderer. Everything it shows comes from one read-only **model** module that returns
dataclasses; the one-shot report renders the same dataclasses with rich. The screen never writes to the
database; the only writer is the `fdc sync` subprocess it launches.

## Architecture

```
src/financial_data_collector/
  heartbeat.py            Heartbeat: write/read/clear of data/sync.heartbeat.json; pid liveness; wait_for_turn()
  sync.py                 run_sync gains origin=, keeps the heartbeat current, waits for a live sync; STALE_HOURS
  store.py                recent_runs(), run_steps(), source_facts() (every SQL the console needs)
  migrations/0008_run_origin.sql   ALTER TABLE sync_runs ADD COLUMN origin TEXT NOT NULL DEFAULT 'unknown'
  console/
    __init__.py
    model.py              Overview dataclasses + load_overview(cfg, now, store_open, heartbeat_read, schedule)
    schedule.py           read_schedule(root, run): Windows Task Scheduler -> Schedule | None
    actions.py            Launcher: start `fdc sync [--only step] --origin console` as a subprocess, track it
    report.py             rich renderables for an Overview (one-shot mode and tests)
    app.py                ConsoleApp(Textual): tabs, status bar, bindings, palette commands, timers
  cli.py                  `console` subcommand; `sync --origin`
```

Responsibilities:

- **heartbeat.py** owns the file format and the liveness rule. It knows nothing about the screen.
- **model.py** is the only place that assembles an `Overview`. It takes its collaborators as parameters
  (a store opener, a heartbeat reader, a schedule reader, `now`) so tests build an `Overview` from a
  fixture database without a terminal, a scheduler or a clock.
- **schedule.py** shells out to PowerShell on Windows through an injected `run` callable; tests pass a
  callable that returns canned JSON. Elsewhere it returns `None` with a reason.
- **actions.py** starts the subprocess and reports its state (`idle`, `starting`, `queued`, `running`,
  `finished(exit)`), nothing more. It does not interpret results; the model reads them from the database.
- **report.py** turns an `Overview` into rich tables using `ui.table` and `ui.badge`, so one-shot output
  looks like every other `fdc` command.
- **app.py** polls the model on a timer, renders, binds keys, calls the launcher. It holds no SQL, no
  file formats, no process logic.

Rules kept: only `store.py` executes SQL (the model calls `Store` methods). Collectors are untouched.
`data/` stays gitignored; the heartbeat and the console's run log live there. No key or label reaches the
screen's code or tests: fixtures stay synthetic.

## Data

### Heartbeat file

`data/sync.heartbeat.json`, beside the database. Written by `run_sync` when a real (non-dry) run
starts, rewritten as the run progresses, deleted when it ends (in a `finally`). Content:

```json
{"run_id": "20261010T164217Z", "pid": 12345, "origin": "scheduled",
 "started_at": "2026-10-10T16:42:17Z", "updated_at": "2026-10-10T16:43:02Z",
 "step": "prices", "detail": "34/75 symbols", "steps_done": ["connections", "ingest"]}
```

Writes are throttled to one per second except on a step change, which always writes. A write is a
temp file plus `os.replace`, so a reader never sees a half-written file.

Liveness: a heartbeat is **alive** when its `pid` is a running process and `updated_at` is under three
hours old (the scheduled task's own execution limit is two). Process liveness on Windows uses
`OpenProcess` + `GetExitCodeProcess` through `ctypes` (never `os.kill(pid, 0)`, which terminates the
process on Windows); on POSIX, `os.kill(pid, 0)` catching `ProcessLookupError`. A heartbeat that is not
alive is **stale**: the run it describes died without cleaning up. The screen shows a stale heartbeat as
"a run started at … did not finish"; the next `run_sync` overwrites it.

`Config.heartbeat_path` is a property: `db_path.parent / "sync.heartbeat.json"`.

### Waiting for a turn

`run_sync` (not dry-run) calls `heartbeat.wait_for_turn(path, timeout=30 min, poll=5 s)` before
opening the store. While an alive heartbeat belongs to another process it sleeps and polls; when the
heartbeat is gone or stale it claims the file and proceeds. On timeout it proceeds anyway (as today) so a
wedged run can never block syncing for good; the message of the first step logged notes the wait. The
`sleep` and `now` parameters already on `run_sync` drive this in tests, so no test sleeps.

### Origin

Migration `0008_run_origin.sql`: `ALTER TABLE sync_runs ADD COLUMN origin TEXT NOT NULL DEFAULT 'unknown'`.
Rows written before the migration read `unknown`. `Store.log_run` gains `origin`. `run_sync` gains
`origin: str = "manual"` and `fdc sync --origin {scheduled,manual,console}` (default `manual`).
`scripts/run_sync.ps1` passes `--origin scheduled`. The console passes `--origin console`. The
`sync_status` view is unchanged; the console takes origin from the runs it lists.

### Store methods

- `recent_runs(limit=50)`: one row per `run_id`, newest first: `run_id, origin, started_at (min),
  finished_at (max), steps (count), errors (count of status='error'), skipped (count), rows (sum)`.
- `run_steps(run_id)`: the run's `sync_runs` rows in order.
- `source_facts()`: one query batch for the Sources tab: last ingested time and count per
  `ingested_files.kind`, symbol count and max date in `prices`, companies with `sec_facts` and their
  newest `filed` date, accounts per `origin`, active connections with `last_ok_at` / `last_error`.

## The screen

Textual app, dark theme, minimum workable size 100 x 30. Layout:

```
 fdc console  ·  C:\…\financial-data-collector                            refreshed 12:48:02
 ● RUNNING  prices · 34/75 symbols · started 12:46:03 by scheduled · pid 12345 · 00:42
 ┌ Overview ┐┌ Sources ┐┌ Runs ┐
 │ STEPS                                                                                     │
 │ step         status      age       rows   message                                         │
 │ connections  skipped     6 h ago      0   no connections: run fdc connect …               │
 │ ingest       ok          6 h ago     19                                                   │
 │ prices       ok STALE   51 h ago    375   75 symbols updated, 1 without data: …           │
 │ …                                                                                         │
 │ SCHEDULE                                                                                  │
 │ task    FinancialDataCollector Sync   state Ready   last 12:42 exit 0   next 20:30        │
 │ triggers  at logon +20 min · daily 08:30 · daily 20:30                                    │
 │ log     %LOCALAPPDATA%\financial-data-collector\sync.log                                  │
 └───────────────────────────────────────────────────────────────────────────────────────────┘
  r run sync   s run step   1/2/3 tabs   ctrl+p commands   q quit
```

**Status bar** (always visible): `IDLE · last run 12:42 (scheduled, exit 0)`, or `RUNNING step ·
detail · started … by origin · elapsed`, or `QUEUED · waiting for the running sync` (the console's own
subprocess exists but owns no heartbeat yet), or `DID NOT FINISH · run started … (pid gone)` for a
stale heartbeat, or `FINISHED exit N · 12:51` for ten seconds after the console's subprocess ends.

**Overview tab**: the steps table (as `fdc status`, plus a "now" marker on the step the heartbeat
names and a check on `steps_done`) and the schedule panel. Staleness thresholds move from `cli.py` to
`sync.STALE_HOURS` and are shared.

**Sources tab**: one table, a row per source and output (see below).

**Runs tab**: left, the recent runs table (started as local time, origin, duration, six step badges in
step order using the first letter and color of each status, rows); right, the selected run's steps
with status, duration, rows and the full message, wrapping. Up/down moves the selection; the detail
follows. The newest run is selected by default and the selection sticks across refreshes.

**Refresh**: a timer every 2 seconds runs `load_overview` in a thread worker (exclusive, so a slow
read never queues). The schedule is read in its own worker at start and every 5 minutes, because
`Get-ScheduledTask` can take seconds; until it returns the panel says "reading the scheduler…".

**Keys**: `1` `2` `3` switch tabs; `r` runs the full sync; `s` runs the step under the cursor in the
steps table; `enter` on a run opens its detail (Runs tab); `ctrl+p` opens the palette with "Run sync",
"Run step: connections" … "Run step: export", "Refresh now"; `q` quits. Quitting does not stop a
launched sync; it runs to completion on its own and logs to `sync_runs` like any other.

## Sources tab

One row per source the configuration names, in this order. A row the configuration does not enable is
still shown with state `off` so the person sees what the collector could use.

| row | where / how | last | state |
|---|---|---|---|
| inbox files | `inbox/` path | newest `ingested_files` time for `fidelity_*` kinds, and file count | `N files waiting` if files sit in the inbox root, else `empty` |
| broker export folder | `[sources.snaptrade].dir` | newest `snaptrade_*` ingested time | `missing` if the folder is absent, `off` if disabled |
| connection: *name* | one row per connection in `connections` (active); its account count from `accounts.origin` | `last_ok_at` | `ok` / `error: <last_error>` (already redacted) / `never` |
| prices | `Tiingo` when a token is set else `yfinance` | last `prices` step time; newest price date; symbols with prices | last step status |
| SEC EDGAR | `companyfacts` | last `sec` step time; companies with facts | `ok` / `skipped: SEC_USER_AGENT not set` |
| watchlist | `watchlist.txt` | symbol count | `missing` if absent |
| research tickers | `[investing].content_dir` | present / missing | `off` when not configured |
| → cockpit feed files | `[export.cockpit].dir` | last `export` step time and its message | `off` when disabled, `missing folder` when the dir is absent |

The first line above the table states the universe size: "following N symbols".

## Schedule adapter

`schedule.read_schedule(root, run=subprocess.run)`. On Windows it runs one PowerShell command that
selects every task whose action arguments contain this root's `scripts\run_sync.ps1` (so a renamed task
is still found and another project's task is not), and emits JSON with `name, state, last_run (ISO),
last_result, next_run (ISO or null), triggers[] {type, start, delay, interval, enabled}`. Dates are
formatted in PowerShell with `.ToString("s")` because `ConvertTo-Json` on Windows PowerShell 5.1 writes
`\/Date(…)\/` otherwise. Trigger types are humanized: logon trigger → `at logon` plus `+20 min` when a
delay is set; daily trigger → `daily HH:MM`; a repetition interval `PT12H` → `every 12 h`.

Results: `Schedule(name, state, last_run, last_result, next_run, triggers, log_path)` or `None` with a
`reason` ("not Windows", "no task runs scripts/run_sync.ps1 for this folder: register one with
scripts/schedule_sync.ps1", or the PowerShell error's first line). The log path is the one
`scripts/run_sync.ps1` writes, derived the same way (`%LOCALAPPDATA%\financial-data-collector\sync.log`),
shown only when that file exists.

## Actions

`actions.Launcher(root, python=sys.executable)` starts
`python -m financial_data_collector --root <root> sync --origin console [--only <step>]` with cwd
`root`, stdout and stderr appended to `data/console-last-run.log` (truncated at each launch). The app
tracks one launch at a time.

Rules:

- The keys and palette commands are disabled while any heartbeat is alive or the console's own launch is
  still running. The status bar says why.
- A launch whose process is alive but owns no heartbeat is `QUEUED` (it is waiting its turn); when the
  heartbeat shows its pid it is `RUNNING`; when the process exits it is `FINISHED exit N` and the next
  refresh shows the run in the Runs tab.
- A launch that fails to start (no python, no module) shows the error in the status bar and leaves a line
  in the console's run log.

This is the whole action contract: "start an `fdc` subcommand, watch the heartbeat and the database".
"Collect a symbol" later becomes `fdc collect <SYMBOL>` plus one palette command and one prompt.

## One-shot mode

`fdc console --once`, or `fdc console` when stdout is not a terminal, prints with rich: the status line,
the steps table, the schedule panel, the sources table and the ten most recent runs, then exits 0. Exit
2 with the usual hint when the database does not exist. This is what tests of the CLI entry exercise, and
what a scheduled job or a pipe gets.

## Error handling

- Database missing: the app does not start; the CLI prints `no database yet` and the `fdc init` hint, exit 2.
- Database locked or mid-migration during a refresh: the refresh keeps the previous `Overview` and the
  status bar shows `refresh failed: <reason>` until the next success. The app never crashes on a read error.
- Heartbeat unreadable (half-written, malformed): treated as absent for that refresh.
- Scheduler query fails or times out (10 s): the panel shows the reason; retried at the next 5-minute tick.
- Subprocess cannot start: status bar error; nothing else changes.
- Terminal too small: Textual's own handling; the one-shot mode is the fallback.

## Testing

No network, no real scheduler, no real key store (the existing autouse fixture), no sleeping.

- `test_heartbeat.py`: write/read round trip; throttle writes once per second but always on step change;
  atomic replace; stale when pid dead (a fake `pid_alive`); `wait_for_turn` returns at once when absent or
  stale, waits while alive and returns when it clears, proceeds at timeout (fake `sleep`/`now`).
- `test_sync.py` additions: a run writes and removes the heartbeat; `origin` lands in `sync_runs`;
  migration 0008 gives old rows `unknown`.
- `test_store.py` additions: `recent_runs` groups by run and counts statuses; `run_steps` order;
  `source_facts` shapes.
- `test_console_model.py`: `load_overview` against a project built from the fixtures (the same way
  `test_sync` does), with a fake heartbeat reader and a fake schedule: steps staleness, sources rows for
  every configuration branch (inbox waiting, folder missing, connections present, export off), runs list.
- `test_console_schedule.py`: parse canned PowerShell JSON (two daily triggers plus a logon trigger with
  a delay; a null next run); not Windows → `None` with reason; the command's text selects by the root's
  `run_sync.ps1` path.
- `test_console_report.py`: one-shot rendering of a fixture `Overview` through `ui.to_text`.
- `test_console_app.py`: Textual `run_test()` driven with `asyncio.run` (no pytest plugin): the app
  mounts with a fixture `Overview` provider; `2` and `3` switch tabs; the runs table has the fixture's
  rows; `r` calls the injected fake launcher and is refused while the fixture heartbeat is alive; the
  status bar text for each state.
- `test_cli.py` additions: `console --once` on a fresh project prints the steps and sources; `sync
  --origin scheduled` is accepted and recorded.
- `test_privacy.py` unchanged and still green: fixtures stay synthetic, no label or key in the new code.

## Documentation and operations changes

- `README.md`: a "Watch it run" section: `fdc console`, `fdc console --once`, what the three tabs
  show, the keys, and that quitting does not stop a launched sync.
- `AGENTS.md`: layout entries for `console/`, `heartbeat.py`, migration 0008, `--origin`; the rule that
  the console never writes; `sync_runs.origin` in the tables list.
- `CHANGELOG.md` Unreleased: console, heartbeat, origin, wait-for-turn, `run_sync.ps1` passing
  `--origin scheduled`.
- `scripts/run_sync.ps1`: add `--origin scheduled`. The owner's registered task runs this script by
  path, so no re-registration is needed.
- `pyproject.toml`: `textual>=8` in dependencies. Version stays; the release note goes under Unreleased.
- `docs/QUERIES.md`: a query for runs per day by origin.

## Future hook: collect a symbol I name

Next spec. It will add `fdc collect <SYMBOL>` (adds the symbol to the watchlist, fetches its prices and
SEC facts, reshapes statements and valuation for it) and, in the console, a palette command "Collect a
symbol…" with a one-line prompt that calls the launcher with that subcommand. The launcher, the
heartbeat and the Runs tab need no change: the run appears with origin `console`, its steps and messages
like any other.
