"""The console's launcher: one fdc sync subprocess at a time, queued until the heartbeat shows its pid, finished
with its exit code, and a failure to start reported instead of raised."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from financial_data_collector.console.actions import Launcher, LaunchState
from financial_data_collector.heartbeat import Heartbeat

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


class FakeProcess:
    def __init__(self, pid: int):
        self.pid, self.code = pid, None

    def poll(self):
        return self.code


def test_command_and_log_path(tmp_path: Path):
    l = Launcher(tmp_path, python="py")
    assert l.command(None) == ["py", "-m", "financial_data_collector", "--root", str(tmp_path), "sync",
                               "--origin", "console"]
    assert l.command("prices")[-2:] == ["--only", "prices"]
    assert l.log_path == tmp_path / "data" / "console-last-run.log"
    assert LaunchState("queued").what == "sync" and LaunchState("queued", "sec").what == "step sec"


def test_launch_lifecycle(tmp_path: Path):
    started = []
    proc = FakeProcess(777)

    def popen(cmd, **kw):
        started.append((cmd, kw))
        return proc

    clock = [NOW]
    l = Launcher(tmp_path, python="py", popen=popen, now=lambda: clock[0])
    assert l.state(None) == LaunchState("idle")
    st = l.start("prices")
    assert st.kind == "queued" and st.pid == 777 and st.step == "prices" and st.started_at == NOW
    assert started[0][0][-2:] == ["--only", "prices"] and started[0][1]["cwd"] == str(tmp_path)
    assert l.log_path.exists()
    assert l.busy
    assert l.start().kind == "failed"            # one at a time
    hb = Heartbeat("r", 777, "console", "2026-03-01T12:00:00Z", "2026-03-01T12:00:01Z", "prices", "1/5")
    assert l.state(hb).kind == "running"
    other = Heartbeat("r0", 1, "scheduled", "2026-03-01T11:59:00Z", "2026-03-01T12:00:01Z", "sec", "")
    assert l.state(other).kind == "queued"
    proc.code = 1
    clock[0] = NOW + timedelta(seconds=30)
    st = l.state(None)
    assert st.kind == "finished" and st.exit_code == 1 and st.finished_at == NOW + timedelta(seconds=30)
    clock[0] = NOW + timedelta(seconds=45)
    assert l.state(None).finished_at == NOW + timedelta(seconds=30)   # the end is recorded once
    assert not l.busy
    l.clear()
    assert l.state(None).kind == "idle"


def test_failed_start_is_reported(tmp_path: Path):
    def popen(cmd, **kw):
        raise OSError("no such python")

    l = Launcher(tmp_path, python="py", popen=popen, now=lambda: NOW)
    st = l.start()
    assert st.kind == "failed" and "no such python" in st.error and st.what == "sync"
    assert "no such python" in l.log_path.read_text(encoding="utf-8")
    l.clear()
    assert l.state(None).kind == "idle"
