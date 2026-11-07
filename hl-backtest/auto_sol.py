#!/usr/bin/env python3
"""The automated SOL-perps run: collect public data, check it, simulate both setups.

Pinned to ONE market: SOL perpetual on Hyperliquid (hyperliquid:SOL-PERP). It collects data
and simulates trades. It never places, signs or sends an order and reads no key.

    python3 hl-backtest/auto_sol.py --db ~/hl-data/sol.sqlite --work ~/hl-data/run --account 1000

Steps, each printed as the command it stands for; the first one that fails stops the run:
1. collect.py meta   --coin SOL      SOL must be in the perps universe; szDecimals
2. collect.py fetch  --coin SOL      closed 1m candles, small overlap, no duplicates
3. collect.py export --coin SOL      the backtest's JSON, with venue, market type and as_of
4. backtest.py run --market hyperliquid:SOL-PERP --price-rule hyperliquid --run A+B
   The market check stops the run on any other venue, market type or symbol.
   A+B = both setups (range breakout, failed breakout) long and short on one ledger,
   so at most one open SOL position.
5. The summary: LONG and SHORT apart (trades, gross, net), skipped signals by reason, and
   the status on the newest closed candle: LONG, SHORT, or FLAT when no setup is valid.

Limits are checked before step 1: leverage 1, position_pct at most 2, max_positions 1.
Exit code: 0 when the run finished (whatever the status), 1 when a step failed.
"""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COIN = "SOL"
MARKET = "hyperliquid:SOL-PERP"
RUN = "A+B"
LIMITS = {"leverage": 1, "position_pct": 2.0, "max_positions": 1}


def _load(name):
    spec = importlib.util.spec_from_file_location("hl_" + name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


collect = _load("collect")
backtest = _load("backtest")


class AutoError(Exception):
    """One plain line: why the automated run stopped."""


def shown(tool, argv):
    def q(x):
        return "'%s'" % x if (" " in x or not x) else x
    return "$ python3 hl-backtest/%s %s" % (tool, " ".join(q(x) for x in argv))


def check_limits(cfg):
    bad = []
    if cfg["symbol"] != COIN:
        bad.append("symbol %s statt %s" % (cfg["symbol"], COIN))
    if cfg["leverage"] != LIMITS["leverage"]:
        bad.append("leverage %s statt 1" % cfg["leverage"])
    if cfg["position_pct"] > LIMITS["position_pct"]:
        bad.append("position_pct %s über 2" % cfg["position_pct"])
    if cfg["max_positions"] != LIMITS["max_positions"]:
        bad.append("max_positions %s statt 1" % cfg["max_positions"])
    if bad:
        raise AutoError("die Konfiguration verletzt die Limits des SOL-Laufs: %s" % "; ".join(bad))


def side_rows(res):
    r = res["runs"][0]
    rows = {(x["setup"], x["side"]): x for x in r["summary"]}
    return r, rows


def summary_text(res):
    r, rows = side_rows(res)
    d, c = res["data"], res["config"]
    lines = []
    w = lines.append
    w("Markt %s, Daten %s bis %s, %d abgeschlossene 1m-Kerzen, as_of %s"
      % (d["market_checked"], d["first"], d["last"], d["closed_candles"], d["as_of"]))
    w("Datenlücken: %s" % (", ".join("%s (%d min)" % (g["from"], g["minutes"]) for g in d["gaps"]) or "keine"))
    w("Konto %.2f $, Hebel %dx, Positionswert max %.2f %%, max %d offene SOL-Position, Lauf %s (beide Setups)"
      % (c["account_value_usd"], c["leverage"], c["position_pct"], c["max_positions"], r["name"]))
    for side in ("long", "short"):
        x = rows[("*", side)]
        w("%-5s  Trades %3d (bekannt %d, unbekannt %d)  brutto %+.4f $  netto %+.4f $  (Gebühren %.4f, Spread %.4f, "
          "Slippage %.4f)" % (side.upper(), x["trades"], x["known"], x["unknown"], x["gross"], x["net"], x["fees"],
                              x["spread"], x["slippage"]))
        for s in r["setups"]:
            y = rows[(s, side)]
            w("       Setup %s: Trades %d, brutto %+.4f $, netto %+.4f $" % (s, y["trades"], y["gross"], y["net"]))
    tot = rows[("*", "*")]
    w("Übersprungene Signale: %s" % (", ".join("%s %d" % kv for kv in tot["skipped"].items()) or "keine"))
    for line in backtest.signal_text(r["signal"], c).rstrip("\n").split("\n"):
        w(line[2:] if line.startswith("  ") else line)
    w("Spread und Slippage sind Modellannahmen, Funding ist nicht eingerechnet. Alles simuliert, keine Order.")
    return "\n".join(lines) + "\n"


def run(db, work, account, config=None, post=None, out=print):
    """The whole automated run. -> the backtest result dict. Raises AutoError on a failed step."""
    cfg = backtest.load_config(config, {"account_value_usd": account, "price_rule": "hyperliquid"})
    check_limits(cfg)
    db = collect.outside_checkout(db, "the database")
    work = os.path.dirname(collect.outside_checkout(os.path.join(work, "SOL-1m.json"), "the work folder"))
    data_file = os.path.join(work, "SOL-1m.json")
    rep_dir = os.path.join(work, "report")
    out("SOL-Perps automatisch: nur %s, Daten sammeln und Trades simulieren (keine Orders)" % MARKET)
    steps = [
        ("collect.py", ["meta", "--db", db, "--coin", COIN]),
        ("collect.py", ["fetch", "--db", db, "--coin", COIN]),
        ("collect.py", ["export", "--db", db, "--coin", COIN, "--out", data_file]),
    ]
    for n, (tool, argv) in enumerate(steps, 1):
        out("\n[%d/4] %s" % (n, shown(tool, argv)))
        buf_o, buf_e = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf_o), contextlib.redirect_stderr(buf_e):
            code = collect.main(argv, post=post or collect.http_post)
        for line in (buf_o.getvalue() + buf_e.getvalue()).splitlines():
            out("      " + line)
        if code != 0:
            raise AutoError("Schritt %d (%s) ist fehlgeschlagen: %s" % (n, argv[0], buf_e.getvalue().strip()))
    argv = ["run", "--data", data_file, "--market", MARKET, "--price-rule", "hyperliquid",
            "--account", repr(float(account)), "--run", RUN, "--out", rep_dir]
    if config:
        argv[1:1] = ["--config", config]
    out("\n[4/4] %s" % shown("backtest.py", argv))
    buf_o, buf_e = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf_o), contextlib.redirect_stderr(buf_e):
        code = backtest.main(argv)
    if code != 0:
        raise AutoError("Schritt 4 (backtest) ist fehlgeschlagen: %s" % buf_e.getvalue().strip())
    out("      Bericht: %s" % os.path.join(rep_dir, "report.txt"))
    with open(os.path.join(rep_dir, "report.json"), encoding="utf-8") as fh:
        res = json.load(fh)
    out("\n" + summary_text(res).rstrip("\n"))
    return res


def main(argv=None, post=None):
    ap = argparse.ArgumentParser(prog="auto_sol.py", description="Automated SOL-perps run: collect, check, simulate.")
    ap.add_argument("--db", default=os.environ.get("HL_BACKTEST_DB"), help="SQLite outside the checkout")
    ap.add_argument("--work", default=os.environ.get("HL_BACKTEST_WORK"), help="folder for the export and report")
    ap.add_argument("--account", type=float, default=os.environ.get("HL_ACCOUNT_USD"),
                    help="account value in USD (or HL_ACCOUNT_USD)")
    ap.add_argument("--config", help="backtest config (the limits are checked)")
    a = ap.parse_args(argv)
    try:
        if not a.db or not a.work:
            raise AutoError("--db and --work are required (or HL_BACKTEST_DB, HL_BACKTEST_WORK)")
        if a.account is None:
            raise AutoError("--account is required (or HL_ACCOUNT_USD): the account value the 2 % refers to")
        run(a.db, a.work, float(a.account), a.config, post=post)
    except (AutoError, backtest.BTError, collect.CollectError) as e:
        print("ABBRUCH: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
