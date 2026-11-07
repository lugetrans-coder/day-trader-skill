# Hourly task: new closed SOL-perps 1m candles from Hyperliquid into
# %LOCALAPPDATA%\day-trader-skill\sol.sqlite (public market data, no key, no order).
# Log: %LOCALAPPDATA%\day-trader-skill\logs\collect-YYYY-MM.log
# Exit code: 0 ok, 1 the fetch failed (also logged in the database; the daily report shows it).
param([string]$Python)

. (Join-Path $PSScriptRoot 'hl-common.ps1')

$py = Find-HlPython $Python
$log = Join-Path $HlLogs ('collect-' + (Get-Date).ToString('yyyy-MM') + '.log')
$collect = Join-Path $HlTools 'collect.py'
$code = Invoke-HlPython -Py $py -LogFile $log -Arguments @($collect, 'fetch', '--db', $HlDb, '--coin', 'SOL')
if ($code -ne 0) { Write-Host "FEHLER beim stuendlichen Abruf (exit $code), siehe $log" }
exit $code
