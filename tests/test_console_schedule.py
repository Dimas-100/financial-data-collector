"""Reading the sync task's registration from Task Scheduler: the command selects by this folder's run_sync.ps1,
its JSON is parsed defensively, and other platforms get a reason instead of a schedule."""
import json
import subprocess
from pathlib import Path

from financial_data_collector.console import schedule as S

TASK = {
    "name": "FinancialDataCollector Sync", "state": "Ready",
    "last_run": "2026-10-10T12:42:14", "last_result": 0, "next_run": "2026-10-10T20:30:00",
    "triggers": [
        {"type": "MSFT_TaskLogonTrigger", "start": "", "delay": "PT20M", "interval": "", "enabled": True},
        {"type": "MSFT_TaskDailyTrigger", "start": "2026-09-26T08:30:00", "delay": "", "interval": "", "enabled": True},
        {"type": "MSFT_TaskDailyTrigger", "start": "2026-09-26T20:30:00", "delay": "", "interval": "PT12H", "enabled": False},
    ],
}


def test_humanize_triggers():
    assert S.humanize_trigger(TASK["triggers"][0]) == "at logon +20 min"
    assert S.humanize_trigger(TASK["triggers"][1]) == "daily 08:30"
    assert S.humanize_trigger(TASK["triggers"][2]) == "daily 20:30, every 12 h (disabled)"
    assert S.humanize_trigger({"type": "MSFT_TaskWeeklyTrigger", "start": "2026-09-28T07:00:00", "delay": "",
                               "interval": "PT90M", "enabled": True}) == "weekly 07:00, every 1 h 30 min"
    assert S.humanize_trigger({"type": "MSFT_TaskBootTrigger", "start": "", "delay": "PT5M", "interval": "",
                               "enabled": True}) == "at startup +5 min"
    assert S.humanize_trigger({"type": "MSFT_TaskTimeTrigger", "start": "2026-10-01T09:00:00", "delay": "",
                               "interval": "", "enabled": True}) == "once 2026-10-01 09:00"
    assert S.humanize_trigger({"type": "MSFT_TaskEventTrigger", "start": "", "delay": "", "interval": "",
                               "enabled": True}) == "Event"


def test_parse_tasks_object_or_list(tmp_path: Path):
    log = tmp_path / "sync.log"
    one = S.parse_tasks(json.dumps(TASK), log)
    assert len(one) == 1 and one[0].name == "FinancialDataCollector Sync" and one[0].state == "Ready"
    assert one[0].last_run == "2026-10-10T12:42:14" and one[0].last_result == 0
    assert one[0].next_run == "2026-10-10T20:30:00"
    assert one[0].triggers == ("at logon +20 min", "daily 08:30", "daily 20:30, every 12 h (disabled)")
    assert one[0].log_path == log
    assert S.parse_tasks(json.dumps([TASK, TASK]), None)[1].log_path is None
    assert S.parse_tasks("", None) == [] and S.parse_tasks("[]", None) == [] and S.parse_tasks("not json", None) == []


def test_parse_never_ran_task():
    never = dict(TASK, last_run="1999-11-30T00:00:00", last_result=267011, next_run=None)
    s = S.parse_tasks(json.dumps(never), None)[0]
    assert s.last_run is None and s.last_result == 267011 and s.next_run is None


def test_command_selects_by_this_folders_script(tmp_path: Path):
    cmd = S.powershell_command(tmp_path)
    assert str(tmp_path / "scripts" / "run_sync.ps1") in cmd
    assert "Get-ScheduledTask" in cmd and "ConvertTo-Json" in cmd and "ToString('s')" in cmd
    quoted = S.powershell_command(Path("C:/it's/here"))
    assert "it''s" in quoted


def test_read_schedule_windows_and_elsewhere(tmp_path: Path):
    calls = []

    def run(args, **kw):
        calls.append((args, kw))
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(TASK), stderr="")

    env = {"LOCALAPPDATA": str(tmp_path)}
    (tmp_path / "financial-data-collector").mkdir()
    (tmp_path / "financial-data-collector" / "sync.log").write_text("x")
    res = S.read_schedule(tmp_path, run=run, platform="win32", env=env)
    assert res.schedule is not None and res.schedule.name == "FinancialDataCollector Sync" and res.reason == ""
    assert res.schedule.log_path == tmp_path / "financial-data-collector" / "sync.log"
    assert calls[0][0][0] == "powershell.exe" and "-NonInteractive" in calls[0][0] and calls[0][1]["timeout"] == 10
    assert S.read_schedule(tmp_path, run=run, platform="linux", env=env).reason == \
        "no scheduler integration on this platform"

    def none(args, **kw):
        return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")

    res = S.read_schedule(tmp_path, run=none, platform="win32", env=env)
    assert res.schedule is None and "scripts/schedule_sync.ps1" in res.reason

    def broken(args, **kw):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="Get-ScheduledTask : boom\nmore")

    assert S.read_schedule(tmp_path, run=broken, platform="win32", env=env).reason == "scheduler: Get-ScheduledTask : boom"

    def slow(args, **kw):
        raise subprocess.TimeoutExpired(args, 10)

    assert S.read_schedule(tmp_path, run=slow, platform="win32", env=env).reason == "scheduler: no answer in 10 s"
    assert S.default_log_path({}) is None
