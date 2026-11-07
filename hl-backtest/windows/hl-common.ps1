# Shared settings for the two SOL-perps tasks (dot-sourced by the other scripts).
# ASCII only on purpose: Windows PowerShell 5.1 reads a script without BOM as ANSI.
# The tasks collect public market data and simulate trades. They never send an order.

$ErrorActionPreference = 'Stop'

$HlRepo    = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$HlTools   = Join-Path $HlRepo 'hl-backtest'
$HlHome    = Join-Path $env:LOCALAPPDATA 'day-trader-skill'
$HlDb      = Join-Path $HlHome 'sol.sqlite'
$HlReports = Join-Path $HlHome 'reports'
$HlLogs    = Join-Path $HlHome 'logs'

foreach ($d in @($HlHome, $HlReports, $HlLogs)) {
    if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d | Out-Null }
}

# Python prints and writes UTF-8 (umlauts in the reports), no .pyc files in the checkout
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'

function Find-HlPython {
    param([string]$Python)
    if ($Python) { return ,@($Python) }
    if (Get-Command py -ErrorAction SilentlyContinue) { return ,@('py', '-3') }
    if (Get-Command python -ErrorAction SilentlyContinue) { return ,@('python') }
    throw 'Python 3.8 or newer not found. Install it, or pass -Python C:\Path\to\python.exe'
}

function Invoke-HlPython {
    # Runs Python, shows its output, appends it to a UTF-8 log file, returns the exit code.
    param([string[]]$Py, [string[]]$Arguments, [string]$LogFile)
    $exe = $Py[0]
    $pre = @()
    if ($Py.Count -gt 1) { $pre = $Py[1..($Py.Count - 1)] }
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $lines = @(& $exe @pre @Arguments 2>&1 | ForEach-Object { "$_" })
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    $cmd = (@($exe) + $pre + $Arguments) -join ' '
    $text = "=== $stamp exit=$code : $cmd`r`n" + ($lines -join "`r`n") + "`r`n"
    [System.IO.File]::AppendAllText($LogFile, $text, (New-Object System.Text.UTF8Encoding($false)))
    foreach ($l in $lines) { Write-Host $l }
    return $code
}
