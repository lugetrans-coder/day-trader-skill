# hl-backtest: offline SOL-perps range backtest

A research tool, not a skill: it has no `SKILL.md`, it is not part of the
plugin, and it is not on the path to an order. Two separate programs:

- `collect.py` fetches PUBLIC market data (Hyperliquid's info endpoint, no
  key, no account) into a SQLite file outside the git checkout and exports it.
  It is the only file here that touches the network.
- `backtest.py` reads the exported file and simulates every fill. No network,
  no broker connector, no API key, no orders.

Python 3.8+, standard library only.

## Collect, export, backtest

```bash
export HL_BACKTEST_DB=~/hl-data/sol.sqlite          # outside the checkout (the tools refuse a path inside one)
python3 hl-backtest/collect.py meta                  # szDecimals for SOL and the perps price rule
python3 hl-backtest/collect.py fetch                 # closed 1m candles; run it again any time
python3 hl-backtest/collect.py status                # range, gaps, the last fetches
python3 hl-backtest/collect.py export --out ~/hl-data/SOL-1m.json
python3 hl-backtest/backtest.py run --data ~/hl-data/SOL-1m.json --config hl-backtest/config.example.json \
    --price-rule hyperliquid --account 1000 --run A --run B --run A+B
```

`fetch` (details in the docstring of `collect.py`):

- `as_of` = now minus 2 s. Only candles with `t + 60 s <= as_of` are stored;
  the candle still forming is dropped and counted.
- The first run asks for the last 5000 minutes (all `candleSnapshot` serves).
  Later runs start at the newest stored candle minus `--overlap-minutes` (10).
- Candles are upserted on their open time: new, unchanged, or changed (the
  newer values win, each change is listed). Overlapping runs never duplicate.
- A full answer that does not cover the window is split and asked again on
  both sides, so paging works whichever end the endpoint serves.
- Every fetch is logged (`fetches` table). A failure prints `FEHLER: ...`
  and exits 1; 429 and 5xx are retried 3 times. Gaps are listed after each
  fetch; a store that fell more than 5000 minutes behind is flagged, because
  that history can no longer be fetched from `candleSnapshot`.

`export` writes the backtest's format with `as_of` (the newest successful
fetch, UTC), the gaps, the logged failures, `exchange_meta` (szDecimals from
`meta`) and the model notes.

## Prices and sizes

`meta` gives SOL `szDecimals` 2: sizes in steps of 0.01. Hyperliquid's
documented perps price rule: at most 5 significant figures and at most
6 - szDecimals decimals, integer prices always allowed. For SOL that is a
tick of 0.001 from 10 to 99.999 and 0.01 from 100 to 999.99. Use
`--price-rule hyperliquid` (or `"price_rule": "hyperliquid"`) on real data;
the default `fixed` (tick 0.01) keeps the synthetic test numbers, and the
report notes when a fixed tick does not match the rule at the file's prices.

```bash
python3 hl-backtest/backtest.py run --data tests/fixtures/hl-backtest/SOL-1m-synthetic.json \
    --config hl-backtest/config.example.json --run A --run B --run A+B --out /tmp/hl-report
```

Same file, same config, same output: there is no randomness. The report
prints the data file's sha256.

`--run A` and `--run B` test each setup on its own ledger; `--run A+B` runs
both on one ledger, so they compete for the one open position. Without
`--run` it runs A and B separately. `--account 250` overrides the account
value, `--as-of` the time the file was saved, `--json` prints everything.

## Data

Hyperliquid's `candleSnapshot` shape, a bare list or
`{"symbol": "SOL", "interval": "1m", "as_of": <ms>, "candles": [...]}`, each
candle `{"t", "T", "s", "i", "o", "h", "l", "c", "v", "n"}` with `t` the open
time in epoch ms. Prices may be strings. `.json.gz` works too.

- A candle counts only once it is closed: `t + 60 s <= as_of`. A snapshot
  saved mid-minute carries the minute still forming; it is dropped and
  listed. With no `as_of` every candle is taken as closed and the report says so.
- Off-grid times, a high or low that does not contain open and close, two
  different candles for one minute, or mixed symbols stop the run with one line.
- Gaps are listed and handled in time order (below).

## The rules (UTC, defaults from `config.example.json`)

**Cycle.** Anchors T at `cycle_anchor_utc` + k x `cycle_minutes` (00:00,
00:30, 01:00, ...). Range = the 15 candles opening in [T, T+15m): RH, RL,
RM = (RH+RL)/2, W = RH-RL. Skipped when a range candle is missing
(`skipped_range_gap`), when W < `width_min_cost_mult` x the round-trip cost
(`skipped_width_min`), or W > `width_max_pct` of RM (`skipped_width_max`).

**Signal window and expiry.** The 15 candles opening in [T+15m, T+30m). A
signal is read at a candle's close. **The setup expires at T+30m**, when the
last window candle (opening T+29m) has closed without a signal. A missing
window candle ends the scan of that cycle (`window_gap`); nothing after the
gap counts.

**Setup A, range breakout.** Long: the first window candle to close above RH
(strictly; a wick or a close at RH is not a breakout). Short: the first close
below RL. Stop RM, target entry +- `target_r` x risk.

**Setup B, failed breakout back into the range.** Short: a window candle b
closes above RH, then a candle r among b+1 .. b+`fake_reentry_candles`
(still inside the window) closes inside the range, RL < close < RH. Stop =
the highest high from b to r plus one tick, target RM. Long is the mirror
below RL. A close through the other side cancels the attempt; an attempt
that runs out of candles expires, and the next close outside starts a new one.
Reward:risk under `fake_min_rr` is `skipped_rr`.

One signal per setup, side and cycle; a skipped signal still uses it up.

**Entry.** At the open of the candle after the signal candle (the signal
candle opening 00:15 closes at 00:16, the entry is the 00:16 open). Simulated
taker fill. Stops round away from the entry, targets toward it, to `tick_size`.

**Exit, candle by candle from the entry candle E.**

| in the candle | exit |
| --- | --- |
| opens through the stop (long: open <= stop, short: open >= stop), not on E | `gap_stop` at the open |
| opens through the target, not on E | `gap_target` at the open |
| stop and target both inside | `stop` at the stop, flagged **ambiguous** (the report adds the net if the target had come first) |
| stop only / target only | `stop` / `target` at the level |
| nothing by the close of E+4 | `timeout` at that close: **the 5-minute exit, exactly 5 minutes after the entry** |
| the candle is missing | `unknown_data_gap` (or `unknown_data_end` past the last candle): no P&L in the totals, the position stays blocked until E+5 minutes |

Long and short are mirrors. Stops trigger on trade candles here; Hyperliquid
triggers stops on the mark price, so every stop is flagged as an approximation.

**Costs.** Both fills are taker fills. Per side: the fee (`taker_fee_bps`,
on the fill value), half the spread (`half_spread_bps`) and slippage
(`slippage_entry_bps`, `slippage_exit_bps`, `slippage_stop_bps` for stops
and gap stops), each on the reference price, each charged once and reported
on its own. Spread and slippage are model assumptions, not measured from
order-book data. `net = gross - fees - spread - slippage`, exactly. Gross is the
move between the reference prices (candle open, stop, target, close).
Funding is not included; each trade says whether it crossed a full hour.

**Size and limits.** 1x leverage. Position value = `position_pct` (2 %) of
the account at the modelled entry fill; the quantity rounds down to
`sz_decimals`; then the value must reach `min_notional_usd`
(`skipped_min_notional`; a 250 $ account gives 5 $ and is always skipped).
At most `max_positions` open (`skipped_position_open`, 1 for SOL) and at most
`max_exposure_pct` (8 %) in total (`skipped_exposure`). Each trade also shows
the **estimated loss at the stop, costs included**, in dollars and as a
percent of the account: a different number from the position value. A gap
can fill worse.

**Skip order.** Entry candle missing (`skipped_entry_gap`, `skipped_data_end`),
risk, reward:risk, open position, min notional, exposure.

## Output

The text report lists the data, the non-ok cycles, then per run every trade
(reference and fill prices, size, position value, estimated loss at the stop,
exit, gross, fees, spread, slippage, net, R), every skipped signal with its
reason, and a table per setup and side: trades, known, unknown, wins, losses,
gross, fees, spread, slippage, net, average net R. `--out DIR` also writes
`report.json`, `trades.csv` and `skipped.csv`.

The synthetic test file `tests/fixtures/hl-backtest/SOL-1m-synthetic.json` is
built by `make_sol_synthetic.py` next to it, one case per cycle. It is not
market data. A simulation on candles is not a prediction.
