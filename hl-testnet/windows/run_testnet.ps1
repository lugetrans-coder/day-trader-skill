# SENDS ORDERS to the Hyperliquid TESTNET (never mainnet: the URL is fixed in trader.py).
# Needs the explicit switch -SendTestnetOrders and the key of a separate API wallet:
#   - from -KeyFile (a file outside the checkout, readable only by you), or
#   - typed in when asked (nothing is shown, nothing is stored).
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\hl-testnet\windows\run_testnet.ps1 -Address 0xMAIN -SendTestnetOrders
#   ... -KeyFile "$env:LOCALAPPDATA\day-trader-skill\testnet\agent.key"
# Stop with Ctrl+C. An open testnet position keeps its reduce-only stop and target at the exchange.
# A file named STOP in %LOCALAPPDATA%\day-trader-skill\testnet blocks new entries.
param(
    [string]$Address = $env:HL_TESTNET_ADDRESS,
    [string]$KeyFile,
    [switch]$SendTestnetOrders,
    [switch]$Once
)

. (Join-Path $PSScriptRoot 'hl-testnet-common.ps1')
Assert-TnReady $Address
if (-not $SendTestnetOrders) {
    throw 'Nothing sent: add -SendTestnetOrders to send orders to the testnet, or use run_dryrun.ps1'
}

$a = @($TnTrader, 'run', '--address', $Address, '--state-dir', $TnState, '--send-testnet-orders')
if ($KeyFile) { $a += @('--key-file', $KeyFile) }
if ($Once) { $a += '--once' }
& $TnPython @a
exit $LASTEXITCODE
