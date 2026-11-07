#!/usr/bin/env python3
"""Builds SOL-1m-synthetic.json: hand-made SOL 1-minute candles, one case per 30-minute cycle.
Not market data. Run it to rebuild the file; tests/test_hlbacktest.py checks it is unchanged.

Every range is 99.50 to 100.50 (mid 100.00) unless said otherwise. Times UTC, Mon 2026-11-02.
  C0  00:00  A long hits the target
  C1  00:30  A short gaps through its stop (opens above it); B long is skipped_rr (target already passed)
  C2  01:00  A long: stop and target in one candle, ambiguous, counted as the stop
  C3  01:30  A long stopped; B short (failed break above) hits its target; together: skipped_position_open
  C4  02:00  A long, neither: the 5-minute exit at the close of the fifth candle
  C5  02:30  A long, a candle is missing during the trade: outcome unknown (data gap)
  C6  03:00  a range candle is missing: cycle skipped_range_gap
  C7  03:30  range too narrow for the costs: skipped_width_min
  C8  04:00  A short hits the target
  C9  04:30  A short stopped; B long (failed break below) hits its target; together: skipped_position_open
  C10 05:00  A long, the data ends during the trade (the 05:18 candle is still open at as_of): unknown
"""

import json
import os
import sys

START = 1793577600000          # 2026-11-02T00:00:00Z
MIN = 60000
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "SOL-1m-synthetic.json")


def flat(p, w=0.05):
    return (p, round(p + w, 2), round(p - w, 2), p)


def std_range():
    rows = [(100.0, 100.2, 99.8, 100.0)] * 15
    rows = list(rows)
    rows[3] = (100.0, 100.5, 99.8, 100.0)
    rows[9] = (100.0, 100.2, 99.5, 100.0)
    return rows


def window(first, fill):
    rows = list(first)
    while len(rows) < 15:
        rows.append(flat(fill))
    return rows


BREAK_UP = (100.40, 100.65, 100.35, 100.60)
BREAK_DOWN = (99.90, 99.95, 99.35, 99.40)

CYCLES = [
    (std_range(), window([BREAK_UP, (100.60, 100.90, 100.40, 100.80), (100.80, 101.30, 100.70, 101.10)], 101.10)),
    (std_range(), window([BREAK_DOWN, (99.40, 99.70, 99.30, 99.60), (100.20, 100.40, 100.10, 100.30)], 100.30)),
    (std_range(), window([BREAK_UP, (100.60, 101.30, 99.90, 100.80)], 100.80)),
    (std_range(), window([(100.45, 100.58, 100.40, 100.55), (100.55, 100.57, 100.40, 100.45),
                          (100.45, 100.50, 100.20, 100.25), (100.25, 100.30, 99.95, 100.05)], 100.05)),
    (std_range(), window([BREAK_UP, (100.60, 100.90, 100.40, 100.70)] + [(100.70, 100.90, 100.40, 100.70)] * 4,
                         100.70)),
    (std_range(), window([BREAK_UP, (100.60, 100.80, 100.50, 100.70), None], 100.70)),
    ([r if i != 5 else None for i, r in enumerate(std_range())], window([], 100.0)),
    ([(100.0, 100.05, 99.95, 100.0)] * 15, window([BREAK_UP], 100.60)),
    (std_range(), window([BREAK_DOWN, (99.40, 99.60, 99.10, 99.20), (99.20, 99.30, 98.70, 98.90)], 98.90)),
    (std_range(), window([(99.55, 99.60, 99.42, 99.45), (99.45, 99.60, 99.43, 99.55),
                          (99.55, 99.80, 99.50, 99.75), (99.75, 100.05, 99.70, 99.95)], 99.95)),
    (std_range(), [BREAK_UP, (100.60, 100.80, 100.40, 100.70), (100.70, 100.90, 100.50, 100.80),
                   (100.80, 101.50, 100.70, 101.40)]),   # 05:18 is still open at as_of: never used
]
AS_OF = START + (10 * 30 + 18) * MIN + 30000   # 05:18:30Z


def fmt(x):
    return "%.2f" % x


def build():
    candles = []
    for k, (rng, win) in enumerate(CYCLES):
        T = START + k * 30 * MIN
        for i, row in enumerate(list(rng) + list(win)):
            if row is None:
                continue
            o, h, l, c = row
            t = T + i * MIN
            candles.append({"t": t, "T": t + MIN - 1, "s": "SOL", "i": "1m", "o": fmt(o), "c": fmt(c),
                            "h": fmt(h), "l": fmt(l), "v": "1000.0", "n": 100})
    return {"source": "synthetic test data, not market data (tests/fixtures/hl-backtest/make_sol_synthetic.py)",
            "symbol": "SOL", "interval": "1m", "as_of": AS_OF, "candles": candles}


def text():
    return json.dumps(build(), indent=0, sort_keys=True) + "\n"


if __name__ == "__main__":
    with open(sys.argv[1] if len(sys.argv) > 1 else OUT, "w", encoding="utf-8") as fh:
        fh.write(text())
