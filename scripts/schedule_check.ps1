# Registers (or removes) the "FinancialDataCollector Check" scheduled task: `fdc check` every day at 07:00.
# This is the backstop for the sync's own last step: if the sync did not run (a laptop that was off, a failed
# step), the checker still runs against whatever the warehouse holds, so the holds on valuation_latest stay
# current and `fdc status` shows a recent check. It reads the warehouse and asks SEC, Yahoo and Tiingo
# questions (read-only GETs); it changes nothing but the checker's own two tables. StartWhenAvailable runs a
# missed 07:00 as soon as the laptop is back. run_check.ps1 runs it and logs to check.log beside sync.log.
# check_runs keeps no record of who started a run, so it is plain `fdc check` (no --origin).
# Usage:  .\scripts\schedule_check.ps1        .\scripts\schedule_check.ps1 -Remove
param([switch]$Remove)
$taskName = "FinancialDataCollector Check"
if ($Remove) {
  Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
  Write-Host "removed task '$taskName'"
  exit 0
}
$script = Join-Path $PSScriptRoot "run_check.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$daily = New-ScheduledTaskTrigger -Daily -At "07:00"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $daily -Settings $settings -Force | Out-Null
Write-Host "registered task '$taskName' (daily 07:00) -> $script"
