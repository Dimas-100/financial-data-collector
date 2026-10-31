# Runs one fdc check from the repo root and appends to check.log, beside sync.log. Called by the scheduled task
# (schedule_check.ps1), so a crash before the checker records a run still leaves a line here.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $env:LOCALAPPDATA "financial-data-collector"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir "check.log"
$python = Join-Path $root ".venv\Scripts\python.exe"
$stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
Add-Content -Path $log -Value "[$stamp] check start"
Set-Location $root
& $python -m financial_data_collector --root $root check 2>&1 | ForEach-Object { Add-Content -Path $log -Value "  $_" }
$code = $LASTEXITCODE
$stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
Add-Content -Path $log -Value "[$stamp] check end (exit $code)"
exit $code
