# Registers (or removes) the two scheduled tasks for the current user, in the folder \day-trader-skill\:
#   "SOL Kerzen stuendlich"   collect_hourly.ps1, every hour at minute -HourlyMinute (default 7)
#   "SOL Tagesbericht"        daily_report.ps1, every day at -DailyTime (default 07:37, local time)
# The tasks run while you are logged on (no password is stored). They collect public market data
# and simulate trades; they never send an order.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\hl-backtest\windows\register_tasks.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\hl-backtest\windows\register_tasks.ps1 -Unregister
param(
    [int]$HourlyMinute = 7,
    [string]$DailyTime = '07:37',
    [string]$Python,
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'
$TaskPath = '\day-trader-skill\'
$Names = @('SOL Kerzen stuendlich', 'SOL Tagesbericht')

if ($Unregister) {
    foreach ($n in $Names) {
        if (Get-ScheduledTask -TaskPath $TaskPath -TaskName $n -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $n -Confirm:$false
            Write-Host "entfernt: $TaskPath$n"
        }
    }
    exit 0
}

if ($HourlyMinute -lt 0 -or $HourlyMinute -gt 59) { throw '-HourlyMinute must be 0 to 59' }
$daily = [datetime]::ParseExact($DailyTime, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
if ($daily.Minute -eq $HourlyMinute) { Write-Host 'Hinweis: beide Aufgaben starten in derselben Minute; die Datenbank wartet dann bis zu 60 s.' }

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$pyArg = ''
if ($Python) { $pyArg = " -Python `"$Python`"" }

function New-HlAction([string]$script) {
    $file = Join-Path $PSScriptRoot $script
    New-ScheduledTaskAction -Execute 'powershell.exe' -WorkingDirectory $repo `
        -Argument ("-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$file`"" + $pyArg)
}

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive

$start = (Get-Date).Date.AddHours((Get-Date).Hour).AddMinutes($HourlyMinute)
if ($start -lt (Get-Date)) { $start = $start.AddHours(1) }
$hourly = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Hours 1)
$day = New-ScheduledTaskTrigger -Daily -At $daily

Register-ScheduledTask -TaskPath $TaskPath -TaskName $Names[0] -Action (New-HlAction 'collect_hourly.ps1') `
    -Trigger $hourly -Settings $settings -Principal $principal -Force `
    -Description 'SOL-Perps 1m-Kerzen von Hyperliquid (oeffentliche Marktdaten) in %LOCALAPPDATA%\day-trader-skill\sol.sqlite. Keine Orders.' | Out-Null
Register-ScheduledTask -TaskPath $TaskPath -TaskName $Names[1] -Action (New-HlAction 'daily_report.ps1') `
    -Trigger $day -Settings $settings -Principal $principal -Force `
    -Description 'SOL-Perps Tagesbericht (250 $ Regelwerk, 1000 $ Szenario), archiviert mit Datum und Git-Commit. Simulation, keine Orders.' | Out-Null

Get-ScheduledTask -TaskPath $TaskPath | Select-Object TaskName, State | Format-Table -AutoSize
Write-Host ("Stuendlich ab {0:HH:mm}, taeglich um {1:HH:mm} (Ortszeit). Repo: {2}" -f $start, $daily, $repo)
