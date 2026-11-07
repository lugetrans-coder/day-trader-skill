# Shared settings for the testnet trader scripts (dot-sourced). ASCII only for Windows PowerShell 5.1.
# Python with the SDK lives in its own venv OUTSIDE the checkout:
#   %LOCALAPPDATA%\day-trader-skill\venv-testnet
# State, journal and (optionally) the API-wallet key file live in:
#   %LOCALAPPDATA%\day-trader-skill\testnet

$ErrorActionPreference = 'Stop'

$TnRepo   = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$TnTrader = Join-Path $TnRepo 'hl-testnet\trader.py'
$TnHome   = Join-Path $env:LOCALAPPDATA 'day-trader-skill'
$TnState  = Join-Path $TnHome 'testnet'
$TnVenv   = Join-Path $TnHome 'venv-testnet'
$TnPython = Join-Path $TnVenv 'Scripts\python.exe'

if (-not (Test-Path -LiteralPath $TnState)) { New-Item -ItemType Directory -Path $TnState | Out-Null }

$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'

function Assert-TnReady {
    param([string]$Address)
    if (-not (Test-Path -LiteralPath $TnPython)) {
        throw "No venv with the SDK at $TnVenv. Create it first (see hl-testnet\README.md, step 1)."
    }
    if ($Address -notmatch '^0x[0-9a-fA-F]{40}$') {
        throw '-Address must be the 0x... address of your MAIN testnet account (not the API wallet)'
    }
}
