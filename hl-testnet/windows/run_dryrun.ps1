# Dry-run against the Hyperliquid TESTNET: reads testnet candles, account, position and orders,
# logs the orders it WOULD send. Sends nothing and needs no key.
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\hl-testnet\windows\run_dryrun.ps1 -Address 0xMAIN
#   ... -Once       one pass only
param(
    [string]$Address = $env:HL_TESTNET_ADDRESS,
    [switch]$Once
)

. (Join-Path $PSScriptRoot 'hl-testnet-common.ps1')
Assert-TnReady $Address

$a = @($TnTrader, 'run', '--address', $Address, '--state-dir', $TnState)
if ($Once) { $a += '--once' }
& $TnPython @a
exit $LASTEXITCODE
