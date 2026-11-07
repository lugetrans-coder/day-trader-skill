#!/usr/bin/env python3
"""Collects public Hyperliquid SOL-perps 1-minute candles into SQLite, apart from the backtest.

This is the only file in hl-backtest/ that touches the network. It reads public market data
from Hyperliquid's info endpoint (POST https://api.hyperliquid.xyz/info, no key, no account).
It never signs anything, never sends an order and never reads a key. backtest.py stays offline:
it only reads the JSON this tool exports.

    python3 collect.py meta   --db ~/hl-data/sol.sqlite                 # szDecimals and the price rule
    python3 collect.py fetch  --db ~/hl-data/sol.sqlite                 # new closed candles, small overlap
    python3 collect.py gaps   --db ~/hl-data/sol.sqlite                 # missing minutes in the store
    python3 collect.py status --db ~/hl-data/sol.sqlite                 # range, counts, recent fetches
    python3 collect.py export --db ~/hl-data/sol.sqlite --out ~/hl-data/SOL-1m.json

The database (and the export) must live outside any git checkout: the tool refuses a path
inside one. --db can also come from HL_BACKTEST_DB.

fetch, step by step:
1. as_of = now minus --settle-seconds (2). A candle is closed when t + 60 s <= as_of; the
   candle still forming is never stored.
2. Start: the newest stored candle minus --overlap-minutes (10); on an empty store, as_of
   minus --lookback-minutes (5000, all Hyperliquid serves: the 5000 most recent candles).
3. candleSnapshot from start to as_of. A full answer (5000 candles) that does not cover the
   whole window is split: the part before its first candle and the part after its last
   are asked again, so it works whether the endpoint serves the oldest or the newest 5000.
4. Each candle is checked (grid, 1m, coin, close time, high/low) and upserted on its open time:
   new -> inserted, same values -> unchanged, other values -> changed (counted and listed).
   So overlapping fetches never make duplicates.
5. Every fetch is logged in the fetches table, ok or failed with the error. A failure prints
   FEHLER and exits 1. HTTP 429 and 5xx are retried 3 times with a pause.
6. Gaps: missing minutes inside the store are listed, and a start older than the 5000 candles
   Hyperliquid serves is reported as history that can no longer be fetched.
"""

import argparse
import datetime
import json
import math
import os
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.request

INFO_URL = "https://api.hyperliquid.xyz/info"
VENUE = "hyperliquid"
MARKET_TYPE = "perp"
MINUTE_MS = 60000
SERVED = 5000              # candleSnapshot serves the 5000 most recent candles
MAX_PAGES = 20
RETRIES = 3
PAUSE = 2.0
PERP_MAX_DECIMALS = 6
PERP_SIG_FIGS = 5
UA = "day-trader-skill hl-backtest collector (public market data)"
MODEL_NOTES = {
    "data": "public Hyperliquid candleSnapshot, 1m, trade prices (not the mark price)",
    "spread": "Modellannahme im Backtest (half_spread_bps), nicht aus diesen Daten",
    "slippage": "Modellannahme im Backtest (slippage_*_bps), nicht aus diesen Daten",
    "funding": "nicht eingerechnet",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
  coin TEXT NOT NULL, interval TEXT NOT NULL, t INTEGER NOT NULL, close_t INTEGER NOT NULL,
  o TEXT NOT NULL, h TEXT NOT NULL, l TEXT NOT NULL, c TEXT NOT NULL, v TEXT NOT NULL, n INTEGER,
  first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
  PRIMARY KEY (coin, interval, t));
CREATE TABLE IF NOT EXISTS fetches (
  id INTEGER PRIMARY KEY AUTOINCREMENT, coin TEXT, interval TEXT, started INTEGER, finished INTEGER,
  req_start INTEGER, req_end INTEGER, as_of INTEGER, status TEXT, requests INTEGER, received INTEGER,
  open_dropped INTEGER, invalid INTEGER, inserted INTEGER, unchanged INTEGER, changed INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS meta (
  coin TEXT PRIMARY KEY, sz_decimals INTEGER, max_leverage INTEGER, fetched INTEGER, raw TEXT);
"""


class CollectError(Exception):
    """One plain line: why the collector stopped."""


# ---------------------------------------------------------------- small helpers

def stamp(ms):
    return (datetime.datetime(1970, 1, 1) + datetime.timedelta(milliseconds=ms)).strftime("%Y-%m-%d %H:%M") + "Z"


def iso(ms):
    d = datetime.datetime(1970, 1, 1) + datetime.timedelta(milliseconds=ms)
    return d.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def parse_utc(text):
    if text is None:
        return None
    s = str(text).strip()
    if s.isdigit():
        return int(s)
    s = s.rstrip("Z").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            d = datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
        return int((d - datetime.datetime(1970, 1, 1)).total_seconds() * 1000)
    raise CollectError("need a UTC time like 2026-11-07T12:00Z, got %r" % text)


def now_ms():
    return int(time.time() * 1000)


def inside_checkout(path):
    """The git work tree that holds path, or None."""
    d = os.path.dirname(os.path.abspath(os.path.expanduser(path)))
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def outside_checkout(path, what):
    repo = inside_checkout(path)
    if repo:
        raise CollectError("%s %s is inside the git checkout %s: keep market data outside it" % (what, path, repo))
    return os.path.abspath(os.path.expanduser(path))


def price_decimals(px, sz_decimals):
    """Hyperliquid perps: at most 5 significant figures and 6 - szDecimals decimals (integers always ok)."""
    int_digits = int(math.floor(math.log10(px) + 1e-9)) + 1
    return max(0, min(PERP_MAX_DECIMALS - sz_decimals, PERP_SIG_FIGS - int_digits))


# ---------------------------------------------------------------- the info endpoint

def perp_coin(coin):
    """A main-dex perps coin name like SOL. Spot names (@107, PURR/USDC) and builder dexes (dex:COIN) are refused."""
    c = str(coin or "").strip().upper()
    if not c or not c.isalnum() or len(c) > 15:
        raise CollectError("--coin %r is not a Hyperliquid perps coin: only names like SOL "
                           "(no spot @N or BASE/QUOTE, no dex:COIN)" % coin)
    return c


def http_post(payload, timeout=20):
    """POST JSON to the public info endpoint. Uses HTTPS_PROXY and the system CA store."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(INFO_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    ctx = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def call(post, payload, sleep=time.sleep, log=None):
    """post() with retries on 429, 5xx and network errors. -> (answer, attempts)."""
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            return post(payload), attempt
        except urllib.error.HTTPError as e:
            last = "HTTP %s %s" % (e.code, e.reason)
            if e.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = "%s: %s" % (type(e).__name__, getattr(e, "reason", e))
        if log:
            log("Abruf fehlgeschlagen (Versuch %d von %d): %s" % (attempt, RETRIES, last))
        if attempt < RETRIES:
            sleep(PAUSE * attempt)
    raise CollectError("info %s: %s" % (payload.get("type"), last))


# ---------------------------------------------------------------- the store

def connect(path):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def check_candle(r, coin, interval):
    """Raw candle -> row tuple, or raise ValueError with the reason."""
    t = int(r["t"])
    if t % MINUTE_MS:
        raise ValueError("off the minute grid")
    if str(r.get("s", coin)).upper() != coin or str(r.get("i", interval)) != interval:
        raise ValueError("coin or interval differs (%s %s)" % (r.get("s"), r.get("i")))
    close_t = int(r.get("T", t + MINUTE_MS - 1))
    if close_t not in (t + MINUTE_MS - 1, t + MINUTE_MS):
        raise ValueError("close time is not t + 1 minute")
    o, h, l, c = (float(r[k]) for k in ("o", "h", "l", "c"))
    if min(o, h, l, c) <= 0 or h < max(o, c, l) or l > min(o, c, h):
        raise ValueError("high/low do not contain open and close")
    float(r.get("v", 0))
    return (t, close_t, str(r["o"]), str(r["h"]), str(r["l"]), str(r["c"]), str(r.get("v", "0")),
            int(r.get("n") or 0))


def upsert(db, coin, interval, rows, seen):
    ins = same = 0
    changed = []
    for row in rows:
        t = row[0]
        old = db.execute("SELECT close_t, o, h, l, c, v, n FROM candles WHERE coin=? AND interval=? AND t=?",
                         (coin, interval, t)).fetchone()
        if old is None:
            db.execute("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (coin, interval) + row + (seen, seen))
            ins += 1
        elif tuple(old) == row[1:]:
            db.execute("UPDATE candles SET last_seen=? WHERE coin=? AND interval=? AND t=?", (seen, coin, interval, t))
            same += 1
        else:
            db.execute("UPDATE candles SET close_t=?, o=?, h=?, l=?, c=?, v=?, n=?, last_seen=? "
                       "WHERE coin=? AND interval=? AND t=?", row[1:] + (seen, coin, interval, t))
            changed.append(t)
    return ins, same, changed


def stored_range(db, coin, interval):
    return db.execute("SELECT MIN(t), MAX(t), COUNT(*) FROM candles WHERE coin=? AND interval=?",
                      (coin, interval)).fetchone()


def find_gaps(db, coin, interval, start=None, end=None):
    """Missing minutes between stored candles, as [(first missing t, minutes)]."""
    q = "SELECT t FROM candles WHERE coin=? AND interval=?"
    args = [coin, interval]
    if start is not None:
        q += " AND t>=?"
        args.append(start)
    if end is not None:
        q += " AND t<=?"
        args.append(end)
    ts = [r[0] for r in db.execute(q + " ORDER BY t", args)]
    return [(a + MINUTE_MS, (b - a) // MINUTE_MS - 1) for a, b in zip(ts, ts[1:]) if b - a > MINUTE_MS]


# ---------------------------------------------------------------- commands

def fetch(db, post, coin="SOL", interval="1m", now=None, overlap_minutes=10, lookback_minutes=SERVED,
          settle_seconds=2, sleep=time.sleep, log=print):
    """One fetch. -> summary dict. Raises CollectError after logging a failed fetch."""
    started = now_ms() if now is None else now
    as_of = started - settle_seconds * 1000
    as_of -= as_of % 1000
    first, last, count = stored_range(db, coin, interval)
    if last is None:
        req_start = as_of - lookback_minutes * MINUTE_MS
        req_start -= req_start % MINUTE_MS
    else:
        req_start = last - overlap_minutes * MINUTE_MS
    served_from = as_of - SERVED * MINUTE_MS
    history_lost = last is not None and last + MINUTE_MS < served_from
    s = {"coin": coin, "interval": interval, "as_of": as_of, "req_start": req_start, "req_end": as_of,
         "requests": 0, "received": 0, "open_dropped": 0, "invalid": 0, "inserted": 0, "unchanged": 0,
         "changed": [], "invalid_detail": [], "status": "ok", "error": None, "history_lost": history_lost}
    todo = [(req_start, as_of)]
    pages = 0
    try:
        while todo:
            if pages >= MAX_PAGES:
                raise CollectError("more than %d pages for one fetch; stopped (window %s to %s left)"
                                   % (MAX_PAGES, stamp(todo[0][0]), stamp(todo[0][1])))
            lo_w, hi_w = todo.pop(0)
            pages += 1
            ans, tries = call(post, {"type": "candleSnapshot",
                                     "req": {"coin": coin, "interval": interval, "startTime": lo_w, "endTime": hi_w}},
                              sleep=sleep, log=log)
            s["requests"] += tries
            if not isinstance(ans, list):
                raise CollectError("candleSnapshot answered %s, not a list" % type(ans).__name__)
            s["received"] += len(ans)
            rows = []
            ts = []
            for r in ans:
                try:
                    row = check_candle(r, coin, interval)
                except (KeyError, TypeError, ValueError) as e:
                    s["invalid"] += 1
                    s["invalid_detail"].append("%s: %s" % (r.get("t") if isinstance(r, dict) else r, e))
                    continue
                ts.append(row[0])
                if row[0] + MINUTE_MS > as_of:
                    s["open_dropped"] += 1       # still forming at as_of: never stored
                    continue
                rows.append(row)
            i, u, ch = upsert(db, coin, interval, rows, started)
            s["inserted"] += i
            s["unchanged"] += u
            s["changed"] += ch
            db.commit()
            if ts and len(ans) >= SERVED:
                if min(ts) > lo_w:
                    todo.append((lo_w, min(ts) - MINUTE_MS))
                if max(ts) + MINUTE_MS < hi_w:
                    todo.append((max(ts) + MINUTE_MS, hi_w))
    except CollectError as e:
        s.update(status="failed", error=str(e))
    log_fetch(db, s, started)
    if s["status"] != "ok":
        raise CollectError(s["error"])
    s["gaps"] = find_gaps(db, coin, interval, start=req_start)
    return s


def log_fetch(db, s, started):
    db.execute("INSERT INTO fetches (coin, interval, started, finished, req_start, req_end, as_of, status, requests, "
               "received, open_dropped, invalid, inserted, unchanged, changed, error) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (s["coin"], s["interval"], started, now_ms(), s["req_start"], s["req_end"], s["as_of"], s["status"],
                s["requests"], s["received"], s["open_dropped"], s["invalid"], s["inserted"], s["unchanged"],
                len(s["changed"]), s["error"]))
    db.commit()


def fetch_meta(db, post, coin="SOL", sleep=time.sleep, log=print):
    ans, _ = call(post, {"type": "meta"}, sleep=sleep, log=log)
    uni = ans.get("universe") if isinstance(ans, dict) else None
    if not isinstance(uni, list):
        raise CollectError("meta answered without a universe list")
    row = next((u for u in uni if str(u.get("name")).upper() == coin), None)
    if row is None:
        raise CollectError("%s is not in the perps universe" % coin)
    if "szDecimals" not in row:
        raise CollectError("meta has no szDecimals for %s" % coin)
    db.execute("INSERT OR REPLACE INTO meta VALUES (?,?,?,?,?)",
               (coin, int(row["szDecimals"]), row.get("maxLeverage"), now_ms(), json.dumps(row, sort_keys=True)))
    db.commit()
    return row


def read_meta(db, coin):
    r = db.execute("SELECT sz_decimals, max_leverage, fetched, raw FROM meta WHERE coin=?", (coin,)).fetchone()
    if r is None:
        return None
    return {"szDecimals": r[0], "maxLeverage": r[1], "fetched": iso(r[2])}


def export(db, coin="SOL", interval="1m", start=None, end=None):
    """The JSON backtest.py reads. as_of = the newest successful fetch's as_of."""
    q = "SELECT t, close_t, o, h, l, c, v, n FROM candles WHERE coin=? AND interval=?"
    args = [coin, interval]
    if start is not None:
        q += " AND t>=?"
        args.append(start)
    if end is not None:
        q += " AND t<=?"
        args.append(end)
    rows = db.execute(q + " ORDER BY t", args).fetchall()
    if not rows:
        raise CollectError("no %s %s candles in the store for that range" % (coin, interval))
    as_of = db.execute("SELECT MAX(as_of) FROM fetches WHERE coin=? AND interval=? AND status='ok'",
                       (coin, interval)).fetchone()[0]
    if as_of is None or rows[-1][0] + MINUTE_MS > as_of:
        raise CollectError("no successful fetch covers the newest stored candle; run fetch first")
    failed = db.execute("SELECT COUNT(*) FROM fetches WHERE coin=? AND interval=? AND status!='ok'",
                        (coin, interval)).fetchone()[0]
    gaps = find_gaps(db, coin, interval, start, end)
    meta = read_meta(db, coin)
    if meta is None:
        raise CollectError("no meta for %s in the store: run meta first, so the export can show the coin is "
                           "in the perps universe" % coin)
    doc = {"source": "Hyperliquid info candleSnapshot (public market data) via hl-backtest/collect.py",
           "venue": VENUE, "market_type": MARKET_TYPE, "market": "%s-PERP" % coin,
           "symbol": coin, "interval": interval, "as_of": as_of, "as_of_utc": iso(as_of),
           "first_utc": iso(rows[0][0]), "last_utc": iso(rows[-1][0]), "closed_candles": len(rows),
           "gaps": [{"from_utc": iso(t), "minutes": m} for t, m in gaps], "failed_fetches_logged": failed,
           "model_notes": MODEL_NOTES,
           "candles": [{"t": t, "T": ct, "s": coin, "i": interval, "o": o, "h": h, "l": l, "c": c, "v": v, "n": n}
                       for t, ct, o, h, l, c, v, n in rows]}
    doc["exchange_meta"] = meta
    return doc


# ---------------------------------------------------------------- reports

def say_gaps(gaps, log):
    if not gaps:
        log("Datenlücken: keine")
        return
    log("Datenlücken: %d (%d fehlende Minuten)" % (len(gaps), sum(m for _, m in gaps)))
    for t, m in gaps[:20]:
        log("  ab %s: %d min" % (stamp(t), m))
    if len(gaps) > 20:
        log("  ... und %d weitere" % (len(gaps) - 20))


def cmd_fetch(a, db, post):
    s = fetch(db, post, a.coin, now=parse_utc(a.now), overlap_minutes=a.overlap_minutes,
              lookback_minutes=a.lookback_minutes, settle_seconds=a.settle_seconds)
    print("Abruf %s %s: Fenster %s bis %s, as_of %s" % (a.coin, "1m", stamp(s["req_start"]), stamp(s["req_end"]),
                                                        iso(s["as_of"])))
    print("  %d Anfrage(n), %d Kerzen empfangen: %d neu, %d unverändert, %d geändert, %d noch offen verworfen, "
          "%d ungültig" % (s["requests"], s["received"], s["inserted"], s["unchanged"], len(s["changed"]),
                           s["open_dropped"], s["invalid"]))
    for x in s["changed"][:10]:
        print("  geändert: %s (neuere Werte gespeichert)" % stamp(x))
    for x in s["invalid_detail"][:10]:
        print("  ungültig: %s" % x)
    if s["history_lost"]:
        print("  WARNUNG: zwischen dem letzten gespeicherten Stand und den %d Kerzen, die Hyperliquid noch liefert, "
              "fehlt Historie; sie ist über candleSnapshot nicht mehr abrufbar" % SERVED)
    first, last, count = stored_range(db, a.coin, "1m")
    print("Bestand: %d Kerzen, %s bis %s" % (count, stamp(first), stamp(last)))
    say_gaps(s["gaps"], print)
    return 0


def cmd_meta(a, db, post):
    row = fetch_meta(db, post, a.coin)
    sz = int(row["szDecimals"])
    print("%s perps laut meta: szDecimals %d, maxLeverage %s" % (a.coin, sz, row.get("maxLeverage")))
    print("Mengen: Vielfache von %g (%d Nachkommastellen)" % (10.0 ** -sz, sz))
    print("Preise (dokumentierte Perps-Regel): max. %d signifikante Stellen, max. %d - %d = %d Nachkommastellen, "
          "ganzzahlige Preise immer erlaubt" % (PERP_SIG_FIGS, PERP_MAX_DECIMALS, sz, PERP_MAX_DECIMALS - sz))
    for px in (9.5, 50.0, 150.0, 1500.0):
        d = price_decimals(px, sz)
        print("  bei %-7g Tick %g" % (px, 10.0 ** -d))
    _, last, _ = stored_range(db, a.coin, "1m")
    if last is not None:
        c = float(db.execute("SELECT c FROM candles WHERE coin=? AND interval='1m' AND t=?", (a.coin, last)).fetchone()[0])
        print("  letzter gespeicherter Close %g -> Tick %g" % (c, 10.0 ** -price_decimals(c, sz)))
    return 0


def cmd_gaps(a, db, post):
    first, last, count = stored_range(db, a.coin, "1m")
    if not count:
        raise CollectError("the store holds no %s candles yet: run fetch" % a.coin)
    print("Bestand: %d Kerzen, %s bis %s" % (count, stamp(first), stamp(last)))
    say_gaps(find_gaps(db, a.coin, "1m"), print)
    return 0


def cmd_status(a, db, post):
    cmd_gaps(a, db, post)
    print("Letzte Abrufe:")
    for r in db.execute("SELECT started, status, req_start, req_end, received, inserted, unchanged, changed, error "
                        "FROM fetches WHERE coin=? ORDER BY id DESC LIMIT 10", (a.coin,)):
        print("  %s %-6s %s..%s empfangen %d neu %d unverändert %d geändert %d %s"
              % (iso(r[0]), r[1], stamp(r[2]), stamp(r[3]), r[4] or 0, r[5] or 0, r[6] or 0, r[7] or 0, r[8] or ""))
    return 0


def cmd_export(a, db, post):
    out = outside_checkout(a.out, "the export")
    doc = export(db, a.coin, "1m", parse_utc(a.start), parse_utc(a.end))
    d = os.path.dirname(out)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"), sort_keys=True)
        fh.write("\n")
    print("Export %s: %d abgeschlossene Kerzen, %s bis %s, as_of %s" % (out, doc["closed_candles"], doc["first_utc"],
                                                                       doc["last_utc"], doc["as_of_utc"]))
    print("Datenlücken: %d (%d fehlende Minuten); fehlgeschlagene Abrufe im Log: %d"
          % (len(doc["gaps"]), sum(g["minutes"] for g in doc["gaps"]), doc["failed_fetches_logged"]))
    print("Markt: %s:%s (Perps-Universum laut meta, szDecimals %s)"
          % (doc["venue"], doc["market"], doc["exchange_meta"]["szDecimals"]))
    return 0


def main(argv=None, post=http_post):
    ap = argparse.ArgumentParser(prog="collect.py", description="Public Hyperliquid 1m candles -> SQLite -> backtest JSON")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("fetch", "meta", "gaps", "status", "export"):
        p = sub.add_parser(name)
        p.add_argument("--db", default=os.environ.get("HL_BACKTEST_DB"),
                       help="SQLite file outside any git checkout (or HL_BACKTEST_DB)")
        p.add_argument("--coin", default="SOL")
        if name == "fetch":
            p.add_argument("--overlap-minutes", type=int, default=10)
            p.add_argument("--lookback-minutes", type=int, default=SERVED, help="first fetch only (max 5000)")
            p.add_argument("--settle-seconds", type=int, default=2)
            p.add_argument("--now", help="pin the clock (UTC), for replays and tests")
        if name == "export":
            p.add_argument("--out", required=True, help="JSON file for backtest.py --data")
            p.add_argument("--start", help="first candle, UTC")
            p.add_argument("--end", help="last candle, UTC")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        if not a.db:
            raise CollectError("--db is required (or set HL_BACKTEST_DB)")
        if a.cmd == "fetch" and not 1 <= a.lookback_minutes <= SERVED:
            raise CollectError("--lookback-minutes is 1 to %d" % SERVED)
        if a.cmd == "fetch" and a.overlap_minutes < 1:
            raise CollectError("--overlap-minutes must be at least 1")
        a.coin = perp_coin(a.coin)
        path = outside_checkout(a.db, "the database")
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        db = connect(path)
        try:
            return {"fetch": cmd_fetch, "meta": cmd_meta, "gaps": cmd_gaps, "status": cmd_status,
                    "export": cmd_export}[a.cmd](a, db, post)
        finally:
            db.close()
    except CollectError as e:
        print("FEHLER: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
