# Runs one fdc sync from the repo root and appends to the log. Called by the scheduled task.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $env:LOCALAPPDATA "financial-data-collector"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir "sync.log"
$python = Join-Path $root ".venv\Scripts\python.exe"
$stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
Add-Content -Path $log -Value "[$stamp] sync start"
Set-Location $root
& $python -m financial_data_collector --root $root sync 2>&1 | ForEach-Object { Add-Content -Path $log -Value "  $_" }
$code = $LASTEXITCODE
$stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
Add-Content -Path $log -Value "[$stamp] sync end (exit $code)"
exit $code
