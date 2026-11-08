# Registers (or removes) the "FinancialDataCollector Sync" scheduled task:
# at logon (delayed 20 min) and daily at 08:30, 17:00 and 20:30, running scripts\run_sync.ps1.
# 17:00 is the first sync after the close: until a sync finishes after 16:00 New York, the day's
# prices count as partial. Daily starts are plain local time: an offset would drift when the clocks change.
# It runs on battery too (a laptop left unplugged would otherwise never sync), and a start
# that arrives while a sync is running waits its turn instead of being dropped, so another
# job can start the task right after it writes new files: Start-ScheduledTask "FinancialDataCollector Sync".
# Usage:  .\scripts\schedule_sync.ps1        .\scripts\schedule_sync.ps1 -Remove
param([switch]$Remove)
$taskName = "FinancialDataCollector Sync"
if ($Remove) {
  Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
  Write-Host "removed task '$taskName'"
  exit 0
}
$script = Join-Path $PSScriptRoot "run_sync.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$logon.Delay = "PT20M"
$daily = foreach ($at in "08:30", "17:00", "20:30") {
  $t = New-ScheduledTaskTrigger -Daily -At $at
  $t.StartBoundary = "2026-11-05T${at}:00"
  $t
}
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances Queue -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger (@($logon) + $daily) -Settings $settings -Force | Out-Null
Write-Host "registered task '$taskName' (logon +20min, daily 08:30, 17:00, 20:30) -> $script"
