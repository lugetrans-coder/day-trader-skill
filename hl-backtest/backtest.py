#!/usr/bin/env python3
"""Offline backtest of two 1-minute range setups on Hyperliquid SOL perps.

Everything here is simulated. It reads a saved file of 1-minute candles, never
the network, never a broker, never an API key, and it never places an order.

    python3 backtest.py run --data FILE                       # runs A and B, each on its own
    python3 backtest.py run --data FILE --config config.json  # your costs and limits
    python3 backtest.py run --data FILE --run A --run B --run A+B
    python3 backtest.py run --data FILE --account 250         # every entry: skipped_min_notional
    python3 backtest.py run --data FILE --json                # the whole result as JSON
    python3 backtest.py run --data FILE --out DIR             # report.txt, report.json, trades.csv, skipped.csv

The candle file is Hyperliquid's candleSnapshot shape, either a bare list or
{"symbol": "SOL", "interval": "1m", "as_of": <ms>, "candles": [...]}, each
candle {"t": open ms, "T": close ms, "s": "SOL", "i": "1m", "o", "h", "l", "c", "v", "n"}.
A candle counts only when it is closed: t + 60 s <= as_of. as_of comes from
--as-of, then the file; without either, every candle is taken as closed and
the report says so.

The rules (all times UTC, every number from the config):

Cycle. Anchors T at cycle_anchor_utc + k x cycle_minutes (00:00, 00:30, ...).
  Range = the range_minutes candles opening in [T, T+15m): RH = max high,
  RL = min low, RM = (RH + RL) / 2, W = RH - RL. A missing range candle skips
  the cycle (skipped_range_gap). W under width_min_cost_mult x round-trip cost
  (skipped_width_min) or over width_max_pct of RM (skipped_width_max) skips it.
  Signal window = the signal_minutes candles opening in [T+15m, T+30m).
  The setup EXPIRES when the last window candle (opening T+29m) has closed,
  at T+30m, with no signal. Signals come from closed candles only. A missing
  window candle ends the scan for that cycle (window_gap).

Setup A, range breakout. Long: the first window candle to CLOSE above RH
  (strictly). Short: the first to CLOSE below RL. Stop RM, target
  entry +- target_r x risk.
Setup B, failed breakout back into the range. Short: a window candle b closes
  above RH, then a candle r in b+1..b+fake_reentry_candles closes inside the
  range (RL < close < RH, strictly), with r also in the window. Stop = the
  highest high from b to r plus one tick, target RM. Long is the mirror below
  RL. A close below RL during a short attempt cancels it; an attempt that runs
  out of candles expires and the next close above RH starts a new one.
  Reward:risk under fake_min_rr is skipped_rr.
One signal per setup, side and cycle; a skipped signal still uses it up.

Entry: the signal candle closes at t+60 s; the entry is a simulated taker fill
  at the OPEN of the next candle E. Stop rounds away from the entry, the target
  toward it: to tick_size with price_rule "fixed", or with price_rule
  "hyperliquid" to the documented perps rule (at most 5 significant figures
  and at most 6 - sz_decimals decimals; integer prices always allowed).
Exit, checked candle by candle on E, E+1, ..., E+hold_candles-1:
  open through the stop (long open <= stop, short open >= stop): gap stop at the open;
  open through the target: target at the open;
  stop and target both inside one candle: AMBIGUOUS, the base run counts the stop;
  stop only: stop at the stop price; target only: target at the target price.
  Nothing hit by the close of E+hold_candles-1 (5 candles: exactly 5 minutes
  after the entry): timeout at that close.
  A stop is triggered by trade candles here. Hyperliquid triggers stops on
  the mark price, so every stop is flagged as an approximation.
  A missing management candle makes the outcome UNKNOWN (unknown_data_gap, or
  unknown_data_end past the last candle). Its P&L stays out of the totals and
  the position counts as open until the latest exit the rule allows.
Fills: every fill is a taker fill. Per side: fee taker_fee_bps on the fill
  notional, half the spread (half_spread_bps) and slippage (entry, exit or
  stop bps) on the reference price. Each is charged once and reported on its
  own: net = gross - fees - spread - slippage, exactly.
Sizing: 1x leverage. Position value = position_pct of the account at the
  modelled entry fill; quantity rounds DOWN to sz_decimals; then the value
  must reach min_notional_usd (skipped_min_notional). At most max_positions
  open (skipped_position_open), total exposure at most max_exposure_pct
  (skipped_exposure). The estimated loss at the stop, costs included, is
  reported apart from the position value.
Skip order: entry candle, risk, reward:risk, open position, min notional, exposure.
"""

import argparse
import csv
import datetime
import hashlib
import io
import json
import math
import os
import sys

MINUTE_MS = 60000
PERP_MAX_DECIMALS = 6      # Hyperliquid perps: price decimals <= 6 - szDecimals
PERP_SIG_FIGS = 5          # and at most 5 significant figures (integers always allowed)
EPS = 1e-9
VERSION = "0.1.0"

DEFAULTS = {
    "symbol": "SOL",
    "account_value_usd": 10000.0,
    "leverage": 1,
    "position_pct": 2.0,
    "max_positions": 1,
    "max_exposure_pct": 8.0,
    "min_notional_usd": 10.0,
    "sz_decimals": 2,
    "tick_size": 0.01,
    "price_rule": "fixed",
    "taker_fee_bps": 4.5,
    "half_spread_bps": 1.0,
    "slippage_entry_bps": 1.0,
    "slippage_exit_bps": 1.0,
    "slippage_stop_bps": 3.0,
    "cycle_anchor_utc": "00:00",
    "cycle_minutes": 30,
    "range_minutes": 15,
    "signal_minutes": 15,
    "hold_candles": 5,
    "target_r": 1.0,
    "fake_reentry_candles": 3,
    "fake_min_rr": 1.0,
    "width_min_cost_mult": 3.0,
    "width_max_pct": 1.5,
}

SETUP_NAMES = {"A": "Range-Breakout", "B": "Gescheiterter Ausbruch, Rückkehr in die Range"}
SIDES = ("long", "short")
STOP_NOTE = "Stop auf Handelskerzen ausgelöst: Näherung für den Mark-Preis-Trigger von Hyperliquid"
EXIT_WORDS = {"target": "Ziel", "stop": "Stop", "gap_stop": "Gap-Stop", "gap_target": "Gap-Ziel",
              "timeout": "5-Min-Exit", "unknown_data_gap": "UNBEKANNT (Datenlücke)",
              "unknown_data_end": "UNBEKANNT (Datenende)"}


class BTError(Exception):
    """A plain one-line reason the backtest cannot run."""


# ---------------------------------------------------------------- time helpers

def utc(ms):
    return datetime.datetime(1970, 1, 1) + datetime.timedelta(milliseconds=ms)


def stamp(ms):
    return utc(ms).strftime("%Y-%m-%d %H:%M") + "Z"


def hhmm(ms):
    return utc(ms).strftime("%H:%M")


def parse_as_of(text):
    """'2026-11-02T05:18:30Z', '2026-11-02 05:18:30' (UTC) or epoch ms -> ms."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(text)
    s = str(text).strip()
    if s.isdigit():
        return int(s)
    s = s.rstrip("Z").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            d = datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
        return int((d - datetime.datetime(1970, 1, 1)).total_seconds() * 1000)
    raise BTError("--as-of needs a UTC time like 2026-11-02T05:18:30Z or epoch ms, got %r" % text)


# ---------------------------------------------------------------- config

def load_config(path=None, overrides=None):
    cfg = dict(DEFAULTS)
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            raise BTError("cannot read the config %s: %s" % (path, e))
        if not isinstance(doc, dict):
            raise BTError("the config %s is not a JSON object" % path)
        unknown = sorted(k for k in doc if k not in DEFAULTS and not k.startswith("_"))
        if unknown:
            raise BTError("unknown config keys: %s" % ", ".join(unknown))
        cfg.update({k: v for k, v in doc.items() if not k.startswith("_")})
    cfg.update({k: v for k, v in (overrides or {}).items() if v is not None})
    return check_config(cfg)


def check_config(cfg):
    c = dict(cfg)
    try:
        for k in ("account_value_usd", "position_pct", "max_exposure_pct", "min_notional_usd", "tick_size",
                  "taker_fee_bps", "half_spread_bps", "slippage_entry_bps", "slippage_exit_bps",
                  "slippage_stop_bps", "target_r", "fake_min_rr", "width_min_cost_mult", "width_max_pct"):
            c[k] = float(c[k])
        for k in ("leverage", "max_positions", "sz_decimals", "cycle_minutes", "range_minutes",
                  "signal_minutes", "hold_candles", "fake_reentry_candles"):
            if float(c[k]) != int(float(c[k])):
                raise ValueError(k)
            c[k] = int(float(c[k]))
    except (TypeError, ValueError) as e:
        raise BTError("the config needs a number for %s" % e)
    if c["leverage"] != 1:
        raise BTError("leverage must be 1: this backtest models 1x only (position value = margin)")
    if c["account_value_usd"] <= 0:
        raise BTError("account_value_usd must be above 0")
    if not 0 < c["position_pct"] <= 100:
        raise BTError("position_pct must be above 0 and at most 100")
    if not 0 < c["max_exposure_pct"] <= 100 * c["leverage"]:
        raise BTError("max_exposure_pct must be above 0 and at most 100 at 1x")
    if c["max_positions"] < 1:
        raise BTError("max_positions must be at least 1")
    if c["tick_size"] <= 0 or c["sz_decimals"] < 0:
        raise BTError("tick_size must be above 0 and sz_decimals at least 0")
    if c["price_rule"] not in ("fixed", "hyperliquid"):
        raise BTError("price_rule is fixed or hyperliquid, got %r" % c["price_rule"])
    if c["price_rule"] == "hyperliquid" and c["sz_decimals"] > PERP_MAX_DECIMALS:
        raise BTError("sz_decimals above %d leaves no price decimals" % PERP_MAX_DECIMALS)
    for k in ("taker_fee_bps", "half_spread_bps", "slippage_entry_bps", "slippage_exit_bps",
              "slippage_stop_bps", "min_notional_usd", "width_min_cost_mult"):
        if c[k] < 0:
            raise BTError("%s must not be negative" % k)
    if c["cycle_minutes"] < 2 or 1440 % c["cycle_minutes"]:
        raise BTError("cycle_minutes must divide a day (1440 minutes), got %s" % c["cycle_minutes"])
    if c["range_minutes"] < 1 or c["signal_minutes"] < 1 or \
            c["range_minutes"] + c["signal_minutes"] > c["cycle_minutes"]:
        raise BTError("range_minutes + signal_minutes must fit inside cycle_minutes")
    if c["hold_candles"] < 1 or c["fake_reentry_candles"] < 1:
        raise BTError("hold_candles and fake_reentry_candles must be at least 1")
    if c["target_r"] <= 0 or c["width_max_pct"] <= 0:
        raise BTError("target_r and width_max_pct must be above 0")
    try:
        h, m = str(c["cycle_anchor_utc"]).split(":")
        h, m = int(h), int(m)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError
    except ValueError:
        raise BTError("cycle_anchor_utc must be HH:MM, got %r" % c["cycle_anchor_utc"])
    c["cycle_anchor_utc"] = "%02d:%02d" % (h, m)
    c["symbol"] = str(c["symbol"]).upper()
    return c


def roundtrip_cost_frac(cfg):
    """Cost of a taker round trip that ends at the stop, as a fraction of the price."""
    return (2 * cfg["taker_fee_bps"] + 2 * cfg["half_spread_bps"] + cfg["slippage_entry_bps"]
            + cfg["slippage_stop_bps"]) / 1e4


# ---------------------------------------------------------------- candles

def _price(v, what, t):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise BTError("candle %s: %s is not a number (%r)" % (stamp(t), what, v))
    if not x > 0 or math.isinf(x):
        raise BTError("candle %s: %s must be above 0 (%r)" % (stamp(t), what, v))
    return x


def parse_candles(doc, symbol=None, as_of=None):
    """Raw JSON -> the checked data: closed candles by open time, gaps, notes."""
    meta = {}
    if isinstance(doc, list):
        rows = doc
    elif isinstance(doc, dict) and isinstance(doc.get("candles"), list):
        rows, meta = doc["candles"], doc
    else:
        raise BTError("the data file needs a list of candles or {\"candles\": [...]}")
    if meta.get("interval") not in (None, "1m"):
        raise BTError("1-minute candles only, the file says %r" % meta.get("interval"))
    file_sym = meta.get("symbol")
    if as_of is None and meta.get("as_of") is not None:
        as_of = parse_as_of(meta["as_of"])
    by_t, dup, syms, no_sym = {}, 0, set(), 0
    for i, r in enumerate(rows):
        if not isinstance(r, dict) or "t" not in r:
            raise BTError("candle #%d has no open time t" % (i + 1))
        try:
            t = int(r["t"])
        except (TypeError, ValueError):
            raise BTError("candle #%d: t is not epoch milliseconds (%r)" % (i + 1, r["t"]))
        if t % MINUTE_MS:
            raise BTError("candle %s is off the 1-minute grid (t=%d)" % (stamp(t), t))
        if r.get("i") not in (None, "1m"):
            raise BTError("candle %s is a %r candle, 1m only" % (stamp(t), r.get("i")))
        if r.get("T") is not None and int(r["T"]) not in (t + MINUTE_MS - 1, t + MINUTE_MS):
            raise BTError("candle %s: close time T=%s is not t + 1 minute" % (stamp(t), r["T"]))
        if r.get("s"):
            syms.add(str(r["s"]).upper())
        else:
            no_sym += 1
        o, h, l, c = (_price(r.get(k), k, t) for k in ("o", "h", "l", "c"))
        if h < max(o, c, l) - EPS or l > min(o, c, h) + EPS:
            raise BTError("candle %s: high/low do not contain open and close (o %s h %s l %s c %s)"
                          % (stamp(t), o, h, l, c))
        bar = {"t": t, "o": o, "h": h, "l": l, "c": c, "v": float(r.get("v") or 0)}
        if t in by_t:
            if by_t[t] != bar:
                raise BTError("two different candles for %s" % stamp(t))
            dup += 1
            continue
        by_t[t] = bar
    if file_sym:
        syms.add(str(file_sym).upper())
    if len(syms) > 1:
        raise BTError("the file mixes symbols: %s" % ", ".join(sorted(syms)))
    if symbol and syms and symbol.upper() not in syms:
        raise BTError("the file holds %s, the config says %s" % (", ".join(sorted(syms)), symbol))
    notes = []
    open_dropped = []
    if as_of is None:
        notes.append("Kein as_of in Datei oder --as-of: alle Kerzen als abgeschlossen angenommen.")
    else:
        for t in sorted(by_t):
            if t + MINUTE_MS > as_of:
                open_dropped.append(t)
                del by_t[t]
    if not by_t:
        raise BTError("no closed candles in the file")
    ts = sorted(by_t)
    gaps = []
    for a, b in zip(ts, ts[1:]):
        if b - a > MINUTE_MS:
            gaps.append({"from": a + MINUTE_MS, "minutes": (b - a) // MINUTE_MS - 1})
    em = meta.get("exchange_meta") if isinstance(meta.get("exchange_meta"), dict) else None
    market = {"venue": meta.get("venue"), "market_type": meta.get("market_type"), "market": meta.get("market"),
              "file_symbol": str(file_sym).upper() if file_sym else None, "candle_symbols": sorted(syms - {None}),
              "candles_without_symbol": no_sym}
    return {"market_info": market, "exchange_meta": em, "by_t": by_t, "first": ts[0], "last": ts[-1], "end": ts[-1] + MINUTE_MS, "count": len(ts),
            "duplicates": dup, "open_dropped": open_dropped, "as_of": as_of, "gaps": gaps, "notes": notes,
            "symbol": (sorted(syms)[0] if syms else (symbol or "").upper())}


def read_data(path, symbol=None, as_of=None):
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as e:
        raise BTError("cannot read the data file %s: %s" % (path, e))
    if path.endswith(".gz"):
        import gzip
        raw = gzip.decompress(raw)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise BTError("the data file %s is not JSON: %s" % (path, e))
    data = parse_candles(doc, symbol=symbol, as_of=as_of)
    data["path"] = path
    data["sha256"] = hashlib.sha256(raw).hexdigest()
    return data


# ---------------------------------------------------------------- rounding

def hl_price_decimals(px, sz_decimals):
    """Decimals a Hyperliquid perps price may carry at this price level."""
    if px <= 0:
        raise BTError("a price must be above 0")
    int_digits = int(math.floor(math.log10(px) + EPS)) + 1
    return max(0, min(PERP_MAX_DECIMALS - sz_decimals, PERP_SIG_FIGS - int_digits))


def hl_tick(px, sz_decimals):
    return 10.0 ** -hl_price_decimals(px, sz_decimals)


def round_price(x, cfg, mode):
    """Round a stop or target the way the config says (floor or ceil)."""
    if cfg.get("price_rule") == "hyperliquid":
        tick = hl_tick(x, cfg["sz_decimals"])
        y = to_tick(x, tick, mode)
        if hl_price_decimals(y, cfg["sz_decimals"]) < hl_price_decimals(x, cfg["sz_decimals"]):
            y = to_tick(y, hl_tick(y, cfg["sz_decimals"]), mode)   # crossed a power of ten
        return y
    return to_tick(x, cfg["tick_size"], mode)


def price_step(x, cfg):
    return hl_tick(x, cfg["sz_decimals"]) if cfg.get("price_rule") == "hyperliquid" else cfg["tick_size"]


def to_tick(x, tick, mode):
    n = x / tick
    k = math.floor(n + EPS) if mode == "floor" else math.ceil(n - EPS)
    return round(k * tick, 10)


def floor_qty(x, decimals):
    f = 10 ** decimals
    return math.floor(x * f + EPS) / f


# ---------------------------------------------------------------- cycles and signals

def cycles_of(data, cfg):
    """Every cycle anchor from the first full cycle in the data to the last candle."""
    h, m = (int(x) for x in cfg["cycle_anchor_utc"].split(":"))
    off = (h * 60 + m) * MINUTE_MS
    step = cfg["cycle_minutes"] * MINUTE_MS
    k = -((off - data["first"]) // step)       # ceil((first - off) / step)
    T = off + k * step
    while T < data["end"]:
        yield T
        T += step


def scan_cycle(T, data, cfg):
    """One cycle -> (cycle record, signals). Pure: reads closed candles only."""
    by_t = data["by_t"]
    R, S = cfg["range_minutes"], cfg["signal_minutes"]
    cid = "%s-%s" % (data["symbol"] or cfg["symbol"], utc(T).strftime("%Y-%m-%dT%H:%MZ"))
    cyc = {"id": cid, "T": T, "start": stamp(T), "status": "ok", "detail": "", "range": None, "window_end": None}
    rng = [T + i * MINUTE_MS for i in range(R)]
    missing = [t for t in rng if t not in by_t]
    if missing:
        if all(t >= data["end"] for t in missing):
            cyc.update(status="data_end", detail="Range nicht vollständig vor Datenende")
        else:
            cyc.update(status="skipped_range_gap", detail="Range-Kerze fehlt: %s" % hhmm(missing[0]))
        return cyc, []
    RH = max(by_t[t]["h"] for t in rng)
    RL = min(by_t[t]["l"] for t in rng)
    RM = (RH + RL) / 2.0
    W = RH - RL
    cyc["range"] = {"high": RH, "low": RL, "mid": round(RM, 10), "width": round(W, 10),
                    "width_pct": round(100 * W / RM, 4)}
    min_w = cfg["width_min_cost_mult"] * roundtrip_cost_frac(cfg) * RM
    if W < min_w - EPS:
        cyc.update(status="skipped_width_min",
                   detail="Breite %.4f unter %.1f x Round-Trip-Kosten (%.4f)" % (W, cfg["width_min_cost_mult"], min_w))
        return cyc, []
    if 100 * W / RM > cfg["width_max_pct"] + EPS:
        cyc.update(status="skipped_width_max",
                   detail="Breite %.3f %% über %.3f %%" % (100 * W / RM, cfg["width_max_pct"]))
        return cyc, []
    K = cfg["fake_reentry_candles"]
    signals = []
    used = set()
    att = {"short": None, "long": None}     # B attempts: short = failed break above RH
    w0 = T + R * MINUTE_MS
    window_end = w0 + S * MINUTE_MS
    cyc["window_end"] = stamp(window_end)
    for j in range(S):
        t = w0 + j * MINUTE_MS
        if t not in by_t:
            if t >= data["end"]:
                cyc.update(status="data_end", detail="Signalfenster endet mit den Daten bei %s" % hhmm(t))
            else:
                cyc.update(status="window_gap", detail="Kerze %s fehlt: Scan des Fensters endet" % hhmm(t))
            break
        b = by_t[t]

        def emit(setup, side, **kw):
            used.add((setup, side))
            sig = {"cycle": cid, "setup": setup, "side": side, "signal_t": t, "entry_t": t + MINUTE_MS,
                   "RH": RH, "RL": RL, "RM": RM}
            sig.update(kw)
            signals.append(sig)

        # setup A
        if ("A", "long") not in used and b["c"] > RH:
            emit("A", "long")
        if ("A", "short") not in used and b["c"] < RL:
            emit("A", "short")
        # setup B, short side: a close above RH that comes back inside
        for side in ("short", "long"):
            if ("B", side) in used:
                continue
            a = att[side]
            outside = b["c"] > RH if side == "short" else b["c"] < RL
            through = b["c"] < RL if side == "short" else b["c"] > RH
            if a is not None:
                a["ext"] = max(a["ext"], b["h"]) if side == "short" else min(a["ext"], b["l"])
                if RL < b["c"] < RH and j - a["j"] <= K:
                    tick = price_step(a["ext"], cfg)
                    stop = round_price(a["ext"] + tick, cfg, "ceil") if side == "short" \
                        else round_price(a["ext"] - tick, cfg, "floor")
                    emit("B", side, stop=stop, breakout_t=a["t"], extreme=a["ext"])
                    att[side] = None
                    continue
                if through or j - a["j"] >= K:
                    att[side] = None
                    a = None
            if a is None and outside:
                att[side] = {"j": j, "t": t, "ext": b["h"] if side == "short" else b["l"]}
    signals.sort(key=lambda s: (s["signal_t"], s["setup"], SIDES.index(s["side"])))
    return cyc, signals


def scan(data, cfg):
    cycles, signals = [], []
    for T in cycles_of(data, cfg):
        c, s = scan_cycle(T, data, cfg)
        cycles.append(c)
        signals.extend(s)
    return cycles, signals


# ---------------------------------------------------------------- fills and costs

def fill_costs(side, qty, entry_ref, exit_ref, exit_kind, cfg):
    """Taker in, taker out. Each cost once, on its own; net = gross - fees - spread - slippage."""
    d = 1 if side == "long" else -1
    hs = cfg["half_spread_bps"] / 1e4
    se = cfg["slippage_entry_bps"] / 1e4
    sx = (cfg["slippage_stop_bps"] if exit_kind in ("stop", "gap_stop") else cfg["slippage_exit_bps"]) / 1e4
    fee = cfg["taker_fee_bps"] / 1e4
    entry_fill = entry_ref * (1 + d * (hs + se))
    exit_fill = exit_ref * (1 - d * (hs + sx))
    gross = qty * d * (exit_ref - entry_ref)
    spread = qty * hs * (entry_ref + exit_ref)
    slippage = qty * (se * entry_ref + sx * exit_ref)
    fees = qty * fee * (entry_fill + exit_fill)
    net = gross - spread - slippage - fees
    return {"entry_fill": entry_fill, "exit_fill": exit_fill, "gross": gross, "fees": fees,
            "spread": spread, "slippage": slippage, "net": net}


def entry_fill_price(side, entry_ref, cfg):
    d = 1 if side == "long" else -1
    return entry_ref * (1 + d * (cfg["half_spread_bps"] + cfg["slippage_entry_bps"]) / 1e4)


def manage(side, entry_t, stop, target, data, cfg):
    """Walk the candles from the entry candle on. -> exit dict."""
    by_t = data["by_t"]
    long_ = side == "long"
    hold = cfg["hold_candles"]
    for k in range(hold):
        t = entry_t + k * MINUTE_MS
        b = by_t.get(t)
        if b is None:
            kind = "unknown_data_end" if t >= data["end"] else "unknown_data_gap"
            return {"kind": kind, "t": t, "ref": None, "ambiguous": False, "candles": k}
        o, h, l = b["o"], b["h"], b["l"]
        if k > 0 and (o <= stop if long_ else o >= stop):
            return {"kind": "gap_stop", "t": t, "ref": o, "ambiguous": False, "candles": k + 1}
        if k > 0 and (o >= target if long_ else o <= target):
            return {"kind": "gap_target", "t": t, "ref": o, "ambiguous": False, "candles": k + 1}
        stop_hit = l <= stop if long_ else h >= stop
        target_hit = h >= target if long_ else l <= target
        if stop_hit and target_hit:
            return {"kind": "stop", "t": t, "ref": stop, "ambiguous": True, "candles": k + 1}
        if stop_hit:
            return {"kind": "stop", "t": t, "ref": stop, "ambiguous": False, "candles": k + 1}
        if target_hit:
            return {"kind": "target", "t": t, "ref": target, "ambiguous": False, "candles": k + 1}
    t = entry_t + (hold - 1) * MINUTE_MS
    return {"kind": "timeout", "t": t, "ref": by_t[t]["c"], "ambiguous": False, "candles": hold}


def levels(sig, entry_ref, cfg):
    """(stop, target, risk, reward) or a skip reason."""
    long_ = sig["side"] == "long"
    if sig["setup"] == "A":
        stop = round_price(sig["RM"], cfg, "floor" if long_ else "ceil")
        risk = entry_ref - stop if long_ else stop - entry_ref
        if risk <= EPS:
            return None, ("skipped_invalid_risk", "Entry %.4f liegt nicht jenseits des Stops %.4f" % (entry_ref, stop))
        raw = entry_ref + cfg["target_r"] * risk if long_ else entry_ref - cfg["target_r"] * risk
        target = round_price(raw, cfg, "floor" if long_ else "ceil")
    else:
        stop = sig["stop"]
        risk = entry_ref - stop if long_ else stop - entry_ref
        if risk <= EPS:
            return None, ("skipped_invalid_risk", "Entry %.4f liegt nicht jenseits des Stops %.4f" % (entry_ref, stop))
        target = round_price(sig["RM"], cfg, "floor" if long_ else "ceil")
    reward = target - entry_ref if long_ else entry_ref - target
    if reward <= EPS:
        return None, ("skipped_rr", "Ziel %.4f liegt nicht vor dem Entry %.4f" % (target, entry_ref))
    if sig["setup"] == "B" and reward / risk < cfg["fake_min_rr"] - EPS:
        return None, ("skipped_rr", "Chance:Risiko %.2f unter %.2f" % (reward / risk, cfg["fake_min_rr"]))
    return (round(stop, 10), round(target, 10), risk, reward), None


# ---------------------------------------------------------------- the run

def run(data, cfg, signals, setups):
    """One ledger over the chosen setups, in time order, with the position limits."""
    by_t = data["by_t"]
    K = cfg["account_value_usd"]
    hold_ms = cfg["hold_candles"] * MINUTE_MS
    trades, skipped, open_pos = [], [], []
    chosen = [s for s in signals if s["setup"] in setups]
    chosen.sort(key=lambda s: (s["entry_t"], s["setup"], SIDES.index(s["side"])))
    for sig in chosen:
        base = {"cycle": sig["cycle"], "setup": sig["setup"], "side": sig["side"],
                "signal": stamp(sig["signal_t"]), "entry_time": stamp(sig["entry_t"])}

        def skip(reason, detail):
            skipped.append(dict(base, reason=reason, detail=detail))

        et = sig["entry_t"]
        if et not in by_t:
            if et >= data["end"]:
                skip("skipped_data_end", "Entry-Kerze %s liegt hinter dem Datenende" % hhmm(et))
            else:
                skip("skipped_entry_gap", "Entry-Kerze %s fehlt: kein Fill modellierbar" % hhmm(et))
            continue
        entry_ref = by_t[et]["o"]
        lv, why = levels(sig, entry_ref, cfg)
        if why:
            skip(*why)
            continue
        stop, target, risk, reward = lv
        open_pos = [p for p in open_pos if p["free_at"] > et]
        if len(open_pos) >= cfg["max_positions"]:
            skip("skipped_position_open", "%d von %d Positionen offen (%s)"
                 % (len(open_pos), cfg["max_positions"], ", ".join(p["label"] for p in open_pos)))
            continue
        e_fill = entry_fill_price(sig["side"], entry_ref, cfg)
        budget = K * cfg["position_pct"] / 100.0
        qty = floor_qty(budget / e_fill, cfg["sz_decimals"])
        notional = qty * e_fill
        if qty <= 0 or notional < cfg["min_notional_usd"] - EPS:
            skip("skipped_min_notional", "%.2f %% von %.2f $ = %.2f $ -> Menge %s -> %.2f $ unter Mindestorderwert %.2f $"
                 % (cfg["position_pct"], K, budget, ("%%.%df" % cfg["sz_decimals"]) % qty, notional,
                    cfg["min_notional_usd"]))
            continue
        exposure = sum(p["qty"] * entry_ref for p in open_pos)   # open positions at the current open (mark approx.)
        if exposure + notional > K * cfg["max_exposure_pct"] / 100.0 + EPS:
            skip("skipped_exposure", "Exposure %.2f $ + %.2f $ über %.2f %% von %.2f $"
                 % (exposure, notional, cfg["max_exposure_pct"], K))
            continue
        if exposure + notional > K * cfg["leverage"] + EPS:
            skip("skipped_margin", "Margin bei 1x nicht gedeckt")
            continue
        ex = manage(sig["side"], et, stop, target, data, cfg)
        at_stop = fill_costs(sig["side"], qty, entry_ref, stop, "stop", cfg)
        tr = dict(base, entry_ref=entry_ref, entry_fill=round(e_fill, 6), stop=stop, target=target,
                  risk_per_unit=round(risk, 10), reward_risk=round(reward / risk, 4), qty=qty,
                  position_value=round(notional, 6), position_pct_of_account=round(100 * notional / K, 4),
                  est_loss_at_stop=round(-at_stop["net"], 6),
                  est_loss_at_stop_pct_of_account=round(-100 * at_stop["net"] / K, 6),
                  exit_kind=ex["kind"], exit_candle=stamp(ex["t"]), candles_held=ex["candles"],
                  ambiguous=ex["ambiguous"], stop_trigger_note=None, outcome_known=ex["ref"] is not None,
                  RH=sig["RH"], RL=sig["RL"])
        if sig["setup"] == "B":
            tr["breakout_candle"] = stamp(sig["breakout_t"])
        if ex["kind"] in ("stop", "gap_stop"):
            tr["stop_trigger_note"] = STOP_NOTE
        if ex["ref"] is None:
            tr.update(exit_ref=None, exit_fill=None, gross=None, fees=None, spread=None, slippage=None, net=None,
                      gross_R=None, net_R=None, crosses_funding_hour=None)
            free_at = et + hold_ms
            tr["blocked_until"] = stamp(free_at)
            tr["open_note"] = EXIT_WORDS[ex["kind"]]
        else:
            c = fill_costs(sig["side"], qty, entry_ref, ex["ref"], ex["kind"], cfg)
            unit = qty * risk
            tr.update(exit_ref=ex["ref"], exit_fill=round(c["exit_fill"], 6),
                      gross=c["gross"], fees=c["fees"], spread=c["spread"], slippage=c["slippage"], net=c["net"],
                      gross_R=round(c["gross"] / unit, 4), net_R=round(c["net"] / unit, 4))
            free_at = ex["t"] + MINUTE_MS
            hour = 3600 * 1000
            tr["crosses_funding_hour"] = (free_at - 1) // hour > et // hour
            if ex["ambiguous"]:
                alt = fill_costs(sig["side"], qty, entry_ref, target, "target", cfg)
                tr["alt_net_target_first"] = alt["net"]
        tr["free_at_ms"] = free_at
        trades.append(tr)
        open_pos.append({"free_at": free_at, "qty": qty, "label": "%s %s seit %s" % (sig["setup"], sig["side"], hhmm(et))})
    return trades, skipped


def summarize(trades, skipped, setups):
    groups = []
    for setup in setups:
        for side in SIDES:
            groups.append((setup, side))
    out = []
    def hit(x, key):
        return key[0] in ("*", x["setup"]) and key[1] in ("*", x["side"])

    for key in groups + [("*", "long"), ("*", "short"), ("*", "*")]:
        ts = [t for t in trades if hit(t, key)]
        sk = [s for s in skipped if hit(s, key)]
        known = [t for t in ts if t["outcome_known"]]
        amb = [t for t in known if t["ambiguous"]]
        row = {"setup": key[0], "side": key[1], "trades": len(ts), "known": len(known),
               "unknown": len(ts) - len(known),
               "wins": sum(1 for t in known if t["net"] > 0), "losses": sum(1 for t in known if t["net"] <= 0),
               "exits": {k: sum(1 for t in ts if t["exit_kind"] == k) for k in EXIT_WORDS},
               "ambiguous": len(amb), "gap_stops": sum(1 for t in ts if t["exit_kind"] == "gap_stop")}
        for k in ("gross", "fees", "spread", "slippage", "net"):
            row[k] = round(sum(t[k] for t in known), 6)
        row["avg_gross_R"] = round(sum(t["gross_R"] for t in known) / len(known), 4) if known else None
        row["avg_net_R"] = round(sum(t["net_R"] for t in known) / len(known), 4) if known else None
        row["net_if_ambiguous_target_first"] = round(row["net"] + sum(t["alt_net_target_first"] - t["net"]
                                                                      for t in amb), 6)
        reasons = {}
        for s in sk:
            reasons[s["reason"]] = reasons.get(s["reason"], 0) + 1
        row["skipped"] = dict(sorted(reasons.items()))
        out.append(row)
    return out


def parse_market(spec):
    """'hyperliquid:SOL-PERP' -> ('hyperliquid', 'SOL', 'perp')."""
    try:
        venue, name = str(spec).split(":", 1)
        coin, kind = name.rsplit("-", 1)
    except ValueError:
        raise BTError("--market needs VENUE:COIN-PERP, like hyperliquid:SOL-PERP, got %r" % spec)
    venue, coin, kind = venue.strip().lower(), coin.strip().upper(), kind.strip().lower()
    if venue != "hyperliquid" or kind != "perp" or not coin.isalnum():
        raise BTError("--market supports Hyperliquid perps only (hyperliquid:COIN-PERP), got %r" % spec)
    return venue, coin, kind


def check_market(data, spec, cfg):
    """Stop unless the file is this market's data: venue, market type, symbol on the file and on every candle."""
    venue, coin, kind = parse_market(spec)
    m = data.get("market_info") or {}
    why = []
    if cfg["symbol"] != coin:
        why.append("die Konfiguration nennt %s" % cfg["symbol"])
    if m.get("venue") != venue:
        why.append("Handelsplatz %s statt %s" % (m.get("venue") or "nicht angegeben", venue))
    if m.get("market_type") != kind:
        why.append("Markttyp %s statt %s" % (m.get("market_type") or "nicht angegeben", kind))
    if m.get("market") not in (None, "%s-PERP" % coin):
        why.append("Markt %s statt %s-PERP" % (m.get("market"), coin))
    if m.get("file_symbol") != coin:
        why.append("Datei-Symbol %s statt %s" % (m.get("file_symbol") or "nicht angegeben", coin))
    other = [x for x in m.get("candle_symbols") or [] if x != coin]
    if other:
        why.append("Kerzen mit Symbol %s" % ", ".join(other))
    if m.get("candles_without_symbol"):
        why.append("%d Kerzen ohne Symbol" % m["candles_without_symbol"])
    if why:
        raise BTError("Eingabedaten gehören nicht zum Markt %s:%s-PERP: %s. Abbruch, kein Backtest."
                      % (venue, coin, "; ".join(why)))
    data["market_checked"] = "%s:%s-PERP" % (venue, coin)
    return data["market_checked"]


def price_checks(data, cfg):
    """Notes: the config against the file's exchange meta and the documented perps price rule."""
    notes = []
    em = data.get("exchange_meta") or {}
    sz = em.get("szDecimals")
    if sz is not None and int(sz) != cfg["sz_decimals"]:
        notes.append("sz_decimals %d in der Konfiguration, aber szDecimals %s laut Börsen-Meta der Datei"
                     % (cfg["sz_decimals"], sz))
    lo = min(b["l"] for b in data["by_t"].values())
    hi = max(b["h"] for b in data["by_t"].values())
    ticks = sorted({hl_tick(lo, cfg["sz_decimals"]), hl_tick(hi, cfg["sz_decimals"])})
    if cfg["price_rule"] == "fixed":
        bad = [t for t in ticks if abs(t - cfg["tick_size"]) > EPS]
        if bad:
            notes.append("tick_size %g passt nicht zur Perps-Preisregel bei Preisen %.4f bis %.4f (Regel-Tick %s); "
                         "price_rule \"hyperliquid\" verwenden" % (cfg["tick_size"], lo, hi,
                                                                    " / ".join("%g" % t for t in ticks)))
    return notes, {"price_low": lo, "price_high": hi, "rule_ticks": ticks}


def backtest(data, cfg, runs=("A", "B"), now=None):
    cycles, signals = scan(data, cfg)
    pnotes, pinfo = price_checks(data, cfg)
    result = {"tool": "hl-backtest", "version": VERSION, "simulated": True, "config": cfg,
              "data": {"path": data.get("path"), "sha256": data.get("sha256"), "symbol": data["symbol"],
                       "closed_candles": data["count"], "first": stamp(data["first"]), "last": stamp(data["last"]),
                       "as_of": stamp(data["as_of"]) if data["as_of"] is not None else None,
                       "open_candles_dropped": [stamp(t) for t in data["open_dropped"]],
                       "duplicates_dropped": data["duplicates"],
                       "gaps": [{"from": stamp(g["from"]), "minutes": g["minutes"]} for g in data["gaps"]],
                       "exchange_meta": data.get("exchange_meta"),
                       "market_checked": data.get("market_checked"),
                       "price_rule_check": pinfo,
                       "notes": data["notes"] + pnotes},
              "model_assumptions": {
                  "fills": "alle Fills simuliert, Taker",
                  "spread": "Modellannahme: half_spread_bps je Seite, nicht aus Orderbuchdaten",
                  "slippage": "Modellannahme: slippage_*_bps, nicht aus Orderbuchdaten",
                  "funding": "nicht eingerechnet",
                  "stop_trigger": "Handelskerzen als Näherung für den Mark-Preis"},
              "cycles": cycles, "signals": len(signals), "runs": []}
    for name in runs:
        setups = parse_run(name)
        trades, skipped = run(data, cfg, signals, setups)
        result["runs"].append({"name": name, "setups": setups, "trades": trades, "skipped": skipped,
                               "summary": summarize(trades, skipped, setups),
                               "signal": current_signal(data, cfg, signals, setups, trades, now=now)})
    return result


STALE_GRACE_MS = 60000     # the entry candle may close up to a minute before the run notices


def current_signal(data, cfg, signals, setups, trades, now=None):
    """LONG, SHORT or FLAT for the newest closed candle: a valid setup closes on it and the
    simulated entry would be the next candle's open. Simulated only, never an order."""
    by_t = data["by_t"]
    t_last = data["last"]
    entry_t = t_last + MINUTE_MS
    out = {"status": "FLAT", "signal_candle": stamp(t_last), "entry_candle": stamp(entry_t), "reasons": [],
           "open_position": None, "simulated": True}
    blocking = [t for t in trades if t["free_at_ms"] > entry_t]
    if blocking:
        b = blocking[-1]
        out["open_position"] = "%s %s seit %s (%s, frei ab %s)" % (
            b["setup"], b["side"].upper(), b["entry_time"], b.get("open_note") or EXIT_WORDS.get(b["exit_kind"]),
            stamp(b["free_at_ms"]))
    if data["as_of"] is None:
        out["reasons"].append("kein Datenstand as_of: es ist nicht sicher, dass die Kerze %s die letzte ist"
                              % hhmm(t_last))
        return out
    if now is not None and now >= entry_t + MINUTE_MS + STALE_GRACE_MS:
        out["reasons"].append("veraltet: jetzt %s, die letzte Kerze in den Daten ist %s"
                              % (stamp(now), stamp(t_last)))
        out["stale"] = True
        return out
    if data["as_of"] >= entry_t + MINUTE_MS:
        out["reasons"].append("veraltet: die Kerze %s ist laut as_of %s schon geschlossen, fehlt aber"
                              % (hhmm(entry_t), stamp(data["as_of"])))
        return out
    cands = [s for s in signals if s["setup"] in setups and s["signal_t"] == t_last]
    if not cands:
        out["reasons"].append("kein Setup schließt auf der letzten abgeschlossenen Kerze %s" % stamp(t_last))
        return out
    est = by_t[t_last]["c"]
    K = cfg["account_value_usd"]
    valid = []
    for sig in sorted(cands, key=lambda s: (s["setup"], SIDES.index(s["side"]))):
        tag = "%s %s" % (sig["setup"], sig["side"].upper())
        lv, why = levels(sig, est, cfg)
        if why:
            out["reasons"].append("%s: %s (%s)" % (tag, why[0], why[1]))
            continue
        if len(blocking) >= cfg["max_positions"]:
            out["reasons"].append("%s: skipped_position_open (%s)" % (tag, out["open_position"]))
            continue
        e_fill = entry_fill_price(sig["side"], est, cfg)
        qty = floor_qty(K * cfg["position_pct"] / 100.0 / e_fill, cfg["sz_decimals"])
        notional = qty * e_fill
        if qty <= 0 or notional < cfg["min_notional_usd"] - EPS:
            out["reasons"].append("%s: skipped_min_notional (%.2f $ unter %.2f $)"
                                  % (tag, notional, cfg["min_notional_usd"]))
            continue
        stop, target, risk, reward = lv
        at_stop = fill_costs(sig["side"], qty, est, stop, "stop", cfg)
        valid.append({"setup": sig["setup"], "side": sig["side"], "entry_estimate": est, "stop": stop,
                      "target": target, "qty": qty, "position_value": round(notional, 6),
                      "est_loss_at_stop": round(-at_stop["net"], 6), "reward_risk": round(reward / risk, 4)})
    sides = {v["side"] for v in valid}
    if len(sides) > 1:
        out["reasons"].append("widersprüchliche Signale LONG und SHORT auf derselben Kerze")
    elif valid:
        out.update(valid[0], status=valid[0]["side"].upper())
    return out


def parse_run(name):
    parts = [p.strip().upper() for p in str(name).split("+")]
    if not parts or any(p not in SETUP_NAMES for p in parts) or len(set(parts)) != len(parts):
        raise BTError("--run is A, B or A+B, got %r" % name)
    return parts


# ---------------------------------------------------------------- the report

def usd(x):
    return "n/a" if x is None else ("%+.2f" % x)


def report(res):
    cfg, d = res["config"], res["data"]
    K = cfg["account_value_usd"]
    out = io.StringIO()
    w = out.write
    w("SOL-Perps-Backtest (offline, alle Ausführungen simuliert, keine Orders)\n")
    w("Daten: %s  sha256 %s\n" % (d["path"], (d["sha256"] or "")[:16]))
    w("  %d abgeschlossene 1m-Kerzen %s bis %s, as_of %s\n" % (d["closed_candles"], d["first"], d["last"],
                                                                   d["as_of"] or "unbekannt"))
    if d["open_candles_dropped"]:
        w("  Nicht abgeschlossen und verworfen: %s\n" % ", ".join(d["open_candles_dropped"]))
    if d["gaps"]:
        w("  Datenlücken: %s\n" % ", ".join("%s (%d min)" % (g["from"], g["minutes"]) for g in d["gaps"]))
    for n in d["notes"]:
        w("  Hinweis: %s\n" % n)
    if d.get("market_checked"):
        w("Markt: %s (Handelsplatz, Markttyp und Symbol jeder Kerze geprüft)\n" % d["market_checked"])
    w("Zyklen: Anker %s UTC alle %d min, Range %d Kerzen, Signalfenster %d Kerzen (verfällt %d min nach Anker), "
      "Exit spätestens am Close der %d. Kerze ab Entry\n"
      % (cfg["cycle_anchor_utc"], cfg["cycle_minutes"], cfg["range_minutes"], cfg["signal_minutes"],
         cfg["range_minutes"] + cfg["signal_minutes"], cfg["hold_candles"]))
    w("Kosten je Seite (Taker): Gebühr %.2f bp; Modellannahmen: halber Spread %.2f bp, Slippage Entry %.2f / "
      "Exit %.2f / Stop %.2f bp. Funding nicht eingerechnet.\n"
      % (cfg["taker_fee_bps"], cfg["half_spread_bps"], cfg["slippage_entry_bps"], cfg["slippage_exit_bps"],
         cfg["slippage_stop_bps"]))
    w("Konto %.2f $, Hebel %dx, Positionswert max %.2f %% (%.2f $), Menge abgerundet auf %d Stellen, "
      "Mindestorderwert %.2f $, max %d offene Position(en), Exposure max %.2f %%\n"
      % (K, cfg["leverage"], cfg["position_pct"], K * cfg["position_pct"] / 100, cfg["sz_decimals"],
         cfg["min_notional_usd"], cfg["max_positions"], cfg["max_exposure_pct"]))
    if cfg["price_rule"] == "hyperliquid":
        w("Preisrundung: Perps-Regel von Hyperliquid (max. %d signifikante Stellen, max. %d - %d = %d Nachkommastellen)\n"
          % (PERP_SIG_FIGS, PERP_MAX_DECIMALS, cfg["sz_decimals"], PERP_MAX_DECIMALS - cfg["sz_decimals"]))
    else:
        w("Preisrundung: fester Tick %g\n" % cfg["tick_size"])
    if d.get("exchange_meta"):
        w("Börsen-Meta aus der Datei: %s\n" % ", ".join("%s %s" % kv for kv in sorted(d["exchange_meta"].items())))
    st = {}
    for c in res["cycles"]:
        st[c["status"]] = st.get(c["status"], 0) + 1
    w("Zyklen gesamt %d: %s. Signale gesamt %d.\n" % (len(res["cycles"]),
      ", ".join("%s %d" % (k, v) for k, v in sorted(st.items())), res["signals"]))
    for c in res["cycles"]:
        if c["status"] != "ok":
            w("  %s  %-18s %s\n" % (c["start"], c["status"], c["detail"]))

    for r in res["runs"]:
        w("\n=== Lauf %s: %s ===\n" % (r["name"], " + ".join("%s %s" % (s, SETUP_NAMES[s]) for s in r["setups"])))
        w("Trades (Preise: Referenz = Kerzenpreis, Fill = mit Spread und Slippage)\n")
        for t in r["trades"]:
            w("  %s %-5s Signal-Kerze %s, Entry %s: ref %.2f fill %.4f  Stop %.2f  Ziel %.2f  Menge %s  "
              "Positionswert %.2f $ (%.2f %%)\n"
              % (t["setup"], t["side"], t["signal"][11:16], t["entry_time"], t["entry_ref"], t["entry_fill"],
                 t["stop"], t["target"], ("%%.%df" % cfg["sz_decimals"]) % t["qty"], t["position_value"],
                 t["position_pct_of_account"]))
            w("      geschätzter Verlust am Stop inkl. Kosten %.2f $ (%.4f %% vom Konto, ohne Gap)\n"
              % (t["est_loss_at_stop"], t["est_loss_at_stop_pct_of_account"]))
            if t["outcome_known"]:
                w("      Exit %s in Kerze %s ref %.2f fill %.4f nach %d Kerze(n): brutto %s, Gebühren %.4f, Spread %.4f, "
                  "Slippage %.4f, netto %s $ (%+.3f R netto)\n"
                  % (EXIT_WORDS[t["exit_kind"]], t["exit_candle"][11:16], t["exit_ref"], t["exit_fill"],
                     t["candles_held"], usd(t["gross"]), t["fees"], t["spread"], t["slippage"], usd(t["net"]),
                     t["net_R"]))
            else:
                w("      Exit %s ab Kerze %s: Ausgang nicht bestimmbar, P&L nicht in den Summen, "
                  "Position gesperrt bis %s\n" % (EXIT_WORDS[t["exit_kind"]], t["exit_candle"][11:16],
                                                 t["blocked_until"]))
            if t["ambiguous"]:
                w("      MEHRDEUTIG: Stop und Ziel in derselben Kerze, Basislauf wertet den Stop "
                  "(Ziel zuerst wäre netto %s $)\n" % usd(t["alt_net_target_first"]))
            if t["stop_trigger_note"]:
                w("      Hinweis: %s\n" % t["stop_trigger_note"])
        if not r["trades"]:
            w("  keine\n")
        w("Übersprungene Signale\n")
        for s in r["skipped"]:
            w("  %s %-5s Signal-Kerze %s  %-22s %s\n" % (s["setup"], s["side"], s["signal"], s["reason"], s["detail"]))
        if not r["skipped"]:
            w("  keine\n")
        w("Ergebnis (nur bekannte Ausgänge; $)\n")
        w("  %-7s %-5s %6s %5s %5s %5s %5s %10s %9s %9s %9s %10s %8s\n"
          % ("Setup", "Seite", "Trades", "bek.", "unbek", "Gew.", "Verl.", "brutto", "Gebühr", "Spread", "Slippage",
             "netto", "Ø R net"))
        for row in r["summary"]:
            if row["setup"] == "*" and row["side"] == "long":
                w("  " + "-" * 118 + "\n")
            w("  %-7s %-5s %6d %5d %5d %5d %5d %10s %9.4f %9.4f %9.4f %10s %8s\n"
              % ("gesamt" if row["setup"] == "*" else row["setup"],
                 "" if row["side"] == "*" else (row["side"].upper() if row["setup"] == "*" else row["side"]),
                 row["trades"], row["known"], row["unknown"], row["wins"], row["losses"], usd(row["gross"]),
                 row["fees"], row["spread"], row["slippage"], usd(row["net"]),
                 "n/a" if row["avg_net_R"] is None else "%+.3f" % row["avg_net_R"]))
        tot = r["summary"][-1]
        w("  Mehrdeutig %d, Gap-Stops %d, unbekannt %d. Netto, falls mehrdeutige Trades das Ziel zuerst "
          "erreicht hätten: %s $\n" % (tot["ambiguous"], tot["gap_stops"], tot["unknown"],
                                       usd(tot["net_if_ambiguous_target_first"])))
        if tot["skipped"]:
            w("  Übersprungen: %s\n" % ", ".join("%s %d" % kv for kv in tot["skipped"].items()))
        w(signal_text(r["signal"], cfg))
    w("\nSimulation auf Kerzendaten, keine Prognose. Funding ist nicht modelliert. Stopps werden auf Handelskerzen "
      "ausgelöst (Näherung für den Mark-Preis). Ein Gap kann schlechter füllen als der geschätzte Verlust am Stop.\n")
    return out.getvalue()


def signal_text(sg, cfg):
    head = "  Status (Signal-Kerze %s, simuliert, keine Order): %s" % (sg["signal_candle"], sg["status"])
    if sg["status"] == "FLAT":
        lines = [head + "\n"]
        lines += ["    - %s\n" % x for x in sg["reasons"]]
    else:
        lines = [head + " (Setup %s)\n" % sg["setup"],
                 "    Entry wäre der Open der Kerze %s, geschätzt mit dem letzten Close %s; Stop %s, Ziel %s, "
                 "Menge %s, Positionswert %.2f $, geschätzter Verlust am Stop inkl. Kosten %.2f $\n"
                 % (sg["entry_candle"], sg["entry_estimate"], sg["stop"], sg["target"],
                    ("%%.%df" % cfg["sz_decimals"]) % sg["qty"], sg["position_value"], sg["est_loss_at_stop"])]
    if sg["open_position"]:
        lines.append("    Offene simulierte Position: %s\n" % sg["open_position"])
    return "".join(lines)


TRADE_FIELDS = ["run", "cycle", "setup", "side", "signal", "entry_time", "entry_ref", "entry_fill", "stop", "target",
                "risk_per_unit", "reward_risk", "qty", "position_value", "position_pct_of_account", "est_loss_at_stop",
                "est_loss_at_stop_pct_of_account", "exit_kind", "exit_candle", "candles_held", "outcome_known",
                "ambiguous", "exit_ref", "exit_fill", "gross", "fees", "spread", "slippage", "net", "gross_R", "net_R",
                "crosses_funding_hour", "stop_trigger_note"]
SKIP_FIELDS = ["run", "cycle", "setup", "side", "signal", "entry_time", "reason", "detail"]


def write_out(res, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "report.txt"), "w", encoding="utf-8") as fh:
        fh.write(report(res))
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=1, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    for name, fields, key in (("trades.csv", TRADE_FIELDS, "trades"), ("skipped.csv", SKIP_FIELDS, "skipped")):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="") as fh:
            wr = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            wr.writeheader()
            for r in res["runs"]:
                for row in r[key]:
                    wr.writerow(dict(row, run=r["name"]))


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(prog="backtest.py", description="Offline SOL-perps range backtest (simulated).")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("run", help="run the backtest on a saved candle file")
    p.add_argument("--data", required=True, help="1m candles, Hyperliquid candleSnapshot JSON (.json or .json.gz)")
    p.add_argument("--config", help="JSON with any of the defaults (see config.example.json)")
    p.add_argument("--as-of", help="UTC time the file was saved; later candles count as not closed")
    p.add_argument("--account", type=float, help="override account_value_usd")
    p.add_argument("--price-rule", choices=["fixed", "hyperliquid"], help="override price_rule")
    p.add_argument("--now", help="wall clock for the status (UTC); data older than that is stale")
    p.add_argument("--market", help="stop unless the file is this market's data, e.g. hyperliquid:SOL-PERP")
    p.add_argument("--run", action="append", help="A, B or A+B (repeatable; default: A and B separately)")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    p.add_argument("--out", metavar="DIR", help="also write report.txt, report.json, trades.csv, skipped.csv")
    a = ap.parse_args(argv)
    if a.cmd != "run":
        ap.print_help()
        return 2
    try:
        cfg = load_config(a.config, {"account_value_usd": a.account, "price_rule": a.price_rule})
        if a.market:
            parse_market(a.market)
            data = read_data(a.data, symbol=None, as_of=parse_as_of(a.as_of))
            check_market(data, a.market, cfg)
        else:
            data = read_data(a.data, symbol=cfg["symbol"], as_of=parse_as_of(a.as_of))
        res = backtest(data, cfg, runs=a.run or ["A", "B"], now=parse_as_of(a.now))
    except BTError as e:
        print("backtest.py: %s" % e, file=sys.stderr)
        return 1
    if a.out:
        write_out(res, a.out)
    if a.json:
        print(json.dumps(res, indent=1, sort_keys=True, ensure_ascii=False))
    else:
        sys.stdout.write(report(res))
        if a.out:
            print("Geschrieben: %s (report.txt, report.json, trades.csv, skipped.csv)" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
