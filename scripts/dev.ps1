# Animatter Podcast Studio — dev launcher (Windows PowerShell)
#
#   .\scripts\dev.ps1            # install deps if needed, migrate, run with reload
#   .\scripts\dev.ps1 -NoInstall # skip the pip step
#   .\scripts\dev.ps1 -Check     # only verify the toolchain

param(
    [switch]$NoInstall,
    [switch]$Check
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".venv")) {
    Write-Host "creating .venv ..." -ForegroundColor Cyan
    python -m venv .venv
}

$py = Join-Path $root ".venv\Scripts\python.exe"

if (-not $NoInstall) {
    & $py -m pip install -q -e "apps/api[dev]"
}

& $py -m animatter check
if ($LASTEXITCODE -ne 0) {
    Write-Host "toolchain check failed; fix the issues above before starting." -ForegroundColor Red
    exit 1
}

if ($Check) { exit 0 }

& $py -m animatter db-upgrade
& $py -m animatter serve --reload
