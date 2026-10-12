# Opens the live console (fdc console) in its own PowerShell window and returns at once.
# Run it from anywhere: .\scripts\open_console.ps1 (or double-click it). The window closes when you quit the
# console with q; if the console fails, the window stays open so you can read why.
param([switch]$InWindow)
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not $InWindow) {
    if (-not (Test-Path $python)) { Write-Error "no virtual environment at $python"; exit 2 }
    $argList = @("-NoLogo", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-InWindow")
    Start-Process -FilePath "powershell.exe" -ArgumentList $argList -WorkingDirectory $root
    exit 0
}
$Host.UI.RawUI.WindowTitle = "fdc console"
Set-Location $root
& $python -m financial_data_collector --root $root console
$code = $LASTEXITCODE
if ($code -ne 0) {
    Write-Host ""
    Write-Host "fdc console exited with code $code. Press Enter to close this window."
    Read-Host | Out-Null
}
exit $code
