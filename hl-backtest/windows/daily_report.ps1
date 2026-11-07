# Daily task: the archived SOL-perps report with daily_report.py.
#   250 $  = comparison for the current rule set ("regelwerk")
#   1000 $ = separate simulation scenario ("szenario")
# Same database as the hourly task: %LOCALAPPDATA%\day-trader-skill\sol.sqlite
# Reports: %LOCALAPPDATA%\day-trader-skill\reports\<date>_<time>Z_<commit>\bericht.txt (kept for good),
#          reports\index.csv (one line per account and day), reports\letzter-bericht.txt,
#          reports\ACHTUNG-DATENZUSTAND.txt (only while data is stale, has gaps or fetches failed)
# Exit code: 0 clean, 2 finished with a data warning, 1 aborted. Collects data, simulates trades, no order.
param([string]$Python)

. (Join-Path $PSScriptRoot 'hl-common.ps1')

$py = Find-HlPython $Python
$log = Join-Path $HlLogs ('daily-' + (Get-Date).ToString('yyyy-MM') + '.log')
$daily = Join-Path $HlTools 'daily_report.py'
$code = Invoke-HlPython -Py $py -LogFile $log -Arguments @(
    $daily, '--db', $HlDb, '--archive', $HlReports,
    '--account', '250=regelwerk', '--account', '1000=szenario')
if ($code -eq 2) { Write-Host "WARNUNG Datenzustand: siehe $(Join-Path $HlReports 'ACHTUNG-DATENZUSTAND.txt')" }
if ($code -eq 1) { Write-Host "ABBRUCH des Tagesberichts, siehe $log" }
exit $code
