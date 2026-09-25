# Registers (or removes) the "FinancialDataCollector Sync" scheduled task:
# at logon (delayed 20 min) and then every 12 hours, running scripts\run_sync.ps1.
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
$logon.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 12)).Repetition
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $logon -Settings $settings -Force | Out-Null
Write-Host "registered task '$taskName' (logon +20min, every 12h) -> $script"
