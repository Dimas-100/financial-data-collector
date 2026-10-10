"""The running sync's heartbeat file: written atomically and throttled, read defensively, alive only while
its process runs, and the rule a second sync follows to wait its turn."""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from financial_data_collector import heartbeat as H

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _writer(tmp_path: Path, clock: Clock, pid: int = 4242) -> H.Writer:
    return H.Writer(tmp_path / "hb.json", "20260301T120000Z", "console", pid=pid,
                    now=lambda: NOW + timedelta(seconds=clock.t), clock=clock)


def test_round_trip_and_atomic_write(tmp_path: Path):
    clock = Clock()
    w = _writer(tmp_path, clock)
    w.start()
    hb = H.read(tmp_path / "hb.json")
    assert hb is not None and hb.run_id == "20260301T120000Z" and hb.pid == 4242 and hb.origin == "console"
    assert hb.started_at == "2026-03-01T12:00:00Z" and hb.updated_at == "2026-03-01T12:00:00Z"
    assert hb.step == "" and hb.steps_done == []
    assert not list(tmp_path.glob("*.tmp")), "the temp file is replaced, not left behind"
    w.finish()
    assert not (tmp_path / "hb.json").exists()
    w.finish()   # a second finish is harmless


def test_update_throttles_except_on_step_change(tmp_path: Path):
    clock = Clock()
    w = _writer(tmp_path, clock)
    w.start()
    w.update("ingest", "inbox")                 # step change: writes
    assert H.read(tmp_path / "hb.json").step == "ingest"
    clock.t = 0.3
    w.update("ingest", "broker feed")           # same step, 0.3 s later: throttled
    assert H.read(tmp_path / "hb.json").detail == "inbox"
    clock.t = 1.4
    w.update("ingest", "done")                  # 1.1 s after the last write: writes
    hb = H.read(tmp_path / "hb.json")
    assert hb.detail == "done" and hb.updated_at == "2026-03-01T12:00:01Z"
    clock.t = 1.5
    w.update("prices", "1/5 symbols")           # step change: writes at once, previous step is done
    hb = H.read(tmp_path / "hb.json")
    assert hb.step == "prices" and hb.steps_done == ["ingest"]


def test_read_malformed_is_none(tmp_path: Path):
    p = tmp_path / "hb.json"
    assert H.read(p) is None
    p.write_text("{not json", encoding="utf-8")
    assert H.read(p) is None
    p.write_text(json.dumps({"run_id": "x"}), encoding="utf-8")
    assert H.read(p) is None
    p.write_text(json.dumps({"run_id": "x", "pid": "abc", "origin": "manual",
                             "started_at": "2026-03-01T12:00:00Z", "updated_at": "2026-03-01T12:00:00Z"}))
    assert H.read(p) is None


def _hb(pid: int = 4242, updated: str = "2026-03-01T11:59:00Z") -> H.Heartbeat:
    return H.Heartbeat("r", pid, "manual", "2026-03-01T11:00:00Z", updated, "prices", "1/5")


def test_is_alive_needs_live_pid_and_recent_update():
    assert H.is_alive(_hb(), now=NOW, alive=lambda pid: True)
    assert not H.is_alive(_hb(), now=NOW, alive=lambda pid: False)
    old = _hb(updated="2026-03-01T08:59:00Z")             # three hours and a minute ago
    assert not H.is_alive(old, now=NOW, alive=lambda pid: True)
    future = _hb(updated="2026-03-01T12:00:30Z")          # a stamp slightly ahead is still recent
    assert H.is_alive(future, now=NOW, alive=lambda pid: True)


def test_is_alive_unparsable_stamp_is_stale():
    assert not H.is_alive(_hb(updated="yesterday"), now=NOW, alive=lambda pid: True)
    assert not H.is_alive(_hb(updated=""), now=NOW, alive=lambda pid: True)


def test_pid_alive_real_processes():
    assert H.pid_alive(os.getpid())
    gone = subprocess.run([sys.executable, "-c", "pass"], capture_output=True)
    assert gone.returncode == 0
    # the child has exited; its id is free (reuse within milliseconds is not a realistic risk here)
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    assert not H.pid_alive(done.pid)
    assert not H.pid_alive(0) and not H.pid_alive(-1)


def test_wait_for_turn_returns_at_once_when_free_or_stale(tmp_path: Path):
    p = tmp_path / "hb.json"
    slept: list[float] = []
    assert H.wait_for_turn(p, now=NOW, sleep=slept.append, alive=lambda pid: True) == 0.0
    p.write_text(_hb().to_json(), encoding="utf-8")
    assert H.wait_for_turn(p, now=NOW, sleep=slept.append, alive=lambda pid: False) == 0.0
    assert slept == []


def test_wait_for_turn_waits_while_alive_then_proceeds(tmp_path: Path):
    p = tmp_path / "hb.json"
    p.write_text(_hb().to_json(), encoding="utf-8")
    polls: list[float] = []

    def sleep(seconds: float) -> None:
        polls.append(seconds)
        if len(polls) == 3:
            p.unlink()                                  # the other sync finished

    waited = H.wait_for_turn(p, now=NOW, sleep=sleep, alive=lambda pid: True, poll=5.0)
    assert polls == [5.0, 5.0, 5.0] and waited == 15.0


def test_wait_for_turn_gives_up_at_timeout(tmp_path: Path):
    p = tmp_path / "hb.json"
    p.write_text(_hb().to_json(), encoding="utf-8")
    slept: list[float] = []
    waited = H.wait_for_turn(p, now=NOW, sleep=slept.append, alive=lambda pid: True, timeout=20.0, poll=5.0)
    assert waited == 20.0 and len(slept) == 4
    assert p.exists(), "giving up does not delete the other run's heartbeat"


def test_wait_for_turn_ages_the_heartbeat_while_waiting(tmp_path: Path):
    """A heartbeat that is alive now but crosses the three-hour age limit during the wait frees the turn."""
    p = tmp_path / "hb.json"
    p.write_text(_hb(updated="2026-03-01T09:00:10Z").to_json(), encoding="utf-8")   # 2 h 59 min 50 s old
    slept: list[float] = []
    waited = H.wait_for_turn(p, now=NOW, sleep=slept.append, alive=lambda pid: True, poll=5.0)
    assert waited == 10.0


def test_finish_leaves_another_runs_heartbeat_alone(tmp_path: Path):
    clock = Clock()
    mine = _writer(tmp_path, clock, pid=1)
    mine.start()
    other = _writer(tmp_path, clock, pid=2)
    other.start()                                       # took over after a timeout
    mine.finish()
    assert H.read(tmp_path / "hb.json").pid == 2


def test_config_heartbeat_path(tmp_path: Path):
    from financial_data_collector import config as C
    C.init_project(tmp_path)
    cfg = C.load_config(tmp_path)
    assert cfg.heartbeat_path == cfg.db_path.parent / "sync.heartbeat.json"
