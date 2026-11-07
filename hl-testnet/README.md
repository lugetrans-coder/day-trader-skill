# hl-testnet: SOL perps on the Hyperliquid TESTNET

A separate trading process, not a skill and not part of the plugin. It trades the SOL perpetual on
the Hyperliquid **testnet** (test money) with the official Python SDK, using the LONG / SHORT / FLAT
rules of `hl-backtest/backtest.py` on closed 1-minute testnet candles. There is no mainnet switch:
the base URL is fixed to `https://api.hyperliquid-testnet.xyz`.

- **Dry-run is the default:** it reads the testnet (candles, account, position, orders) and only logs
  the orders it would send. No key needed.
- **Orders are sent only** with `--send-testnet-orders` (the Windows script also needs
  `-SendTestnetOrders`) and the key of a separate API wallet, read locally: from a key file outside
  the checkout, from `HL_TESTNET_AGENT_KEY`, or typed in at the start (hidden, not stored).
  A key that belongs to the main account itself is refused.

Python 3.9+ (the SDK needs it): `pip install -r hl-testnet/requirements.txt` (SDK 0.24.0, the
version this code was checked against).

## What it does every minute (2 s after a candle closes)

1. Reads the exchange: account value, free margin, the SOL position, the open SOL orders. If a read
   fails, nothing is sent in that pass.
2. Reconciles the saved state with the exchange BEFORE any new order. Every order carries a client
   order id (cloid) starting with `0xd7a1`, and the state is written before each order is sent. So
   after a timeout or a restart the bot looks the order up instead of sending it again; the position
   on the exchange is the truth for the size.
3. With an open trade: one reduce-only stop and one reduce-only target (trigger market orders) for
   exactly the filled size, replaced when the size changes (a partial fill, a partial close). At the
   deadline, the close of the fifth candle after the entry candle opened (5 minutes after the
   simulated entry), a reduce-only IOC closes the position; the stop and target are cancelled after
   that, so the position is never unprotected in between. A partial close is retried every minute.
4. Only when flat, with no SOL order open, no halt and no `STOP` file: the signal on the newest closed
   candle (sent within 30 s of its close, else FLAT), then a fresh read of account, position and
   orders, then the limits, then leverage 1x isolated, then ONE IOC entry. Stop and target are worked
   out again from the real fill price.

Limits (hard, not configurable upwards): 1x leverage, position value at most 2 % of the account
value at the entry's limit price, one open SOL position, no order under 10 $. Leverage is checked on
the position after the fill: anything but 1x closes the position at once and halts the bot.
A position or order the bot did not make blocks new entries and is never touched.

State, journal and lock: `%LOCALAPPDATA%\day-trader-skill\testnet\` (`state.json`, `journal.jsonl`
with every decision and every planned or sent order, `trader.lock`). `trader.py status` shows the
account, the position, the orders and the state; `trader.py reset-halt` clears a halt once you have
checked it by hand.

## Known limits

- Testnet prices, liquidity and fills differ from mainnet; partial fills and slippage are likely.
- The exchange triggers stops on the mark price; the backtest used trade candles.
- The process has to keep running. If the PC sleeps, the 5-minute exit comes late; the stop and
  target stay at the exchange in the meantime.
- Tested here with a simulated API (`tests/test_hltestnet.py`) and checked offline against the SDK's
  signatures, wire format and testnet signing. It has not yet run against the live testnet.
