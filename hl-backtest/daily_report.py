#!/usr/bin/env python3
"""The daily SOL-perps report, archived: one folder per run, named by date, time and git commit.

    python3 hl-backtest/daily_report.py --db %LOCALAPPDATA%\\day-trader-skill\\sol.sqlite ^
        --archive %LOCALAPPDATA%\\day-trader-skill\\reports

It runs auto_sol.py for each account on the same exported data (default: 250 $ as the comparison for
the current rule set, 1,000 $ as a separate simulation scenario), checks the data, and writes

    <archive>/<YYYY-MM-DD>_<HHMM>Z_<commit>[-dirty]/
        bericht.txt          the readable report: data state first, then LONG and SHORT per account
        run-info.json        date, commit, accounts, data health, every number in bericht.txt
        SOL-1m.json.gz       the exact candles both accounts used
        account-250/report/  report.txt, report.json, trades.csv, skipped.csv
        account-1000/report/ the same
    <archive>/index.csv      one line per account and run, appended, never rewritten
    <archive>/letzter-bericht.txt          a copy of the newest bericht.txt
    <archive>/ACHTUNG-DATENZUSTAND.txt     only while the newest run had a warning

A folder is never overwritten: a second run in the same minute gets a -2 suffix.

Data health, shown as "!!" lines at the top of bericht.txt:
  VERALTET      the newest candle closed more than --max-age-minutes (90) ago, or a fetch failed today
  DATENLÜCKEN   missing minutes in the exported window
  ABRUFE        failed fetches in the last 24 hours (the hourly task logs them in the database),
                or fewer successful fetches than --min-fetches-24h (20)
Exit code: 0 clean, 2 finished with a warning above, 1 aborted. It collects data and simulates
trades; it never sends an order.
"""

import argparse
import csv
import datetime
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import auto_sol  # noqa: E402  (same folder; loads collect.py and backtest.py)

collect = auto_sol.collect
backtest = auto_sol.backtest
MIN = 60000
DAY = 24 * 60 * MIN
ROLES = {"regelwerk": "Vergleich: aktuelles Regelwerk", "szenario": "separates Simulationsszenario"}
DEFAULT_ACCOUNTS = ["250=regelwerk", "1000=szenario"]
INDEX_FIELDS = ["run_utc", "commit", "dirty", "account_usd", "role", "data_first", "data_last", "as_of",
                "stale", "gap_minutes", "failed_fetches_24h", "ok_fetches_24h",
                "long_trades", "long_gross", "long_net", "short_trades", "short_gross", "short_net",
                "skipped", "status", "report_dir"]


class ReportError(Exception):
    """One plain line: why the daily report stopped."""


def git_info(repo=REPO, run=subprocess.run):
    """(short commit, dirty) of the checkout the tools run from; ('nogit', None) without git."""
    try:
        head = run(["git", "-C", repo, "rev-parse", "--short=7", "HEAD"], capture_output=True, text=True, timeout=20)
        if head.returncode != 0:
            return "nogit", None
        st = run(["git", "-C", repo, "status", "--porcelain", "--untracked-files=no"], capture_output=True,
                 text=True, timeout=20)
        return head.stdout.strip(), bool(st.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return "nogit", None


def parse_accounts(items):
    out = []
    for it in items:
        amount, _, role = str(it).partition("=")
        try:
            usd = float(amount)
        except ValueError:
            raise ReportError("--account needs USD=ROLE, like 250=regelwerk, got %r" % it)
        if usd <= 0:
            raise ReportError("--account must be above 0, got %r" % it)
        role = role or "szenario"
        if role not in ROLES:
            raise ReportError("--account role is %s, got %r" % (" or ".join(ROLES), role))
        out.append((usd, role))
    if len(set(u for u, _ in out)) != len(out):
        raise ReportError("each account amount once")
    return out


def run_folder(archive, now, commit, dirty):
    d = datetime.datetime(1970, 1, 1) + datetime.timedelta(milliseconds=now)
    base = "%s_%sZ_%s%s" % (d.strftime("%Y-%m-%d"), d.strftime("%H%M"), commit, "-dirty" if dirty else "")
    path = os.path.join(archive, base)
    n = 2
    while os.path.exists(path):
        path = os.path.join(archive, "%s-%d" % (base, n))
        n += 1
    os.makedirs(path)
    return path


def health(db_path, data, now, max_age_minutes, min_fetches):
    """Data state from the database and the exported data: stale, gaps, fetches of the last 24 h."""
    db = collect.connect(db_path)
    try:
        rows = db.execute("SELECT started, status, error FROM fetches WHERE coin=? AND started>=? ORDER BY started",
                          (auto_sol.COIN, now - DAY)).fetchall()
    finally:
        db.close()
    failed = [{"utc": collect.iso(r[0]), "error": r[2]} for r in rows if r[1] != "ok"]
    ok = sum(1 for r in rows if r[1] == "ok")
    last_close = data["last"] + MIN
    age = (now - last_close) / 60000.0
    h = {"newest_candle": backtest.stamp(data["last"]), "age_minutes": round(age, 1),
         "stale": age > max_age_minutes, "max_age_minutes": max_age_minutes,
         "gaps": [{"from": backtest.stamp(g["from"]), "minutes": g["minutes"]} for g in data["gaps"]],
         "gap_minutes": sum(g["minutes"] for g in data["gaps"]),
         "ok_fetches_24h": ok, "failed_fetches_24h": failed, "min_fetches_24h": min_fetches, "warnings": []}
    if h["stale"]:
        h["warnings"].append("VERALTET: die neueste Kerze %s ist %.0f min alt (Grenze %d min)"
                             % (h["newest_candle"], age, max_age_minutes))
    if h["gaps"]:
        h["warnings"].append("DATENLÜCKEN: %d, zusammen %d min: %s" % (
            len(h["gaps"]), h["gap_minutes"], ", ".join("%s (%d min)" % (g["from"], g["minutes"]) for g in h["gaps"][:10])
            + (" ..." if len(h["gaps"]) > 10 else "")))
    if failed:
        h["warnings"].append("ABRUFE: %d fehlgeschlagen in den letzten 24 h, zuletzt %s: %s"
                             % (len(failed), failed[-1]["utc"], failed[-1]["error"]))
    if ok < min_fetches:
        h["warnings"].append("ABRUFE: nur %d erfolgreiche Abrufe in den letzten 24 h (erwartet mindestens %d, "
                             "läuft die stündliche Aufgabe?)" % (ok, min_fetches))
    return h


def side(res, key):
    r = res["runs"][0]
    return next(x for x in r["summary"] if (x["setup"], x["side"]) == key)


def account_block(usd, role, res):
    r = res["runs"][0]
    lines = ["", "=== Konto %s $ (%s) ===" % ("{:,.0f}".format(usd).replace(",", "."), ROLES[role])]
    for s in ("long", "short"):
        x = side(res, ("*", s))
        lines.append("%-5s  Trades %3d (bekannt %d, unbekannt %d)  brutto %+.4f $  netto %+.4f $  "
                     "(Gebühren %.4f, Spread %.4f, Slippage %.4f)" % (s.upper(), x["trades"], x["known"], x["unknown"],
                                                                      x["gross"], x["net"], x["fees"], x["spread"],
                                                                      x["slippage"]))
        for st in r["setups"]:
            y = side(res, (st, s))
            lines.append("       Setup %s: Trades %d, brutto %+.4f $, netto %+.4f $" % (st, y["trades"], y["gross"],
                                                                                    y["net"]))
    tot = side(res, ("*", "*"))
    lines.append("Übersprungene Signale: %s" % (", ".join("%s %d" % kv for kv in tot["skipped"].items()) or "keine"))
    for line in backtest.signal_text(r["signal"], res["config"]).rstrip("\n").split("\n"):
        lines.append(line[2:] if line.startswith("  ") else line)
    return lines


def append_index(path, rows):
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=INDEX_FIELDS, delimiter=";")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def daily(db, archive, accounts, config=None, post=None, now=None, max_age_minutes=90, min_fetches=20,
          git=git_info, out=print):
    """One archived daily run. -> (folder, exit code 0 or 2)."""
    db = collect.outside_checkout(db, "the database")
    archive = os.path.dirname(collect.outside_checkout(os.path.join(archive, "index.csv"), "the archive"))
    now = collect.now_ms() if now is None else now
    commit, dirty = git()
    folder = run_folder(archive, now, commit, dirty)
    data_file = os.path.join(folder, "SOL-1m.json")
    log = []

    def say(line=""):
        log.append(line)
        out(line)

    results = []
    try:
        for i, (usd, role) in enumerate(accounts):
            say("\n##### Konto %s $ (%s)" % (usd, ROLES[role]))
            res = auto_sol.run(db, os.path.join(folder, "account-%g" % usd), usd, config, post=post, out=say,
                               collect_data=(i == 0), export=(i == 0), data_file=data_file, allow_stale=True, now=now)
            results.append((usd, role, res))
    except Exception as e:
        with open(os.path.join(folder, "ABGEBROCHEN.txt"), "w", encoding="utf-8") as fh:
            fh.write("Lauf %s UTC, Git-Commit %s: ABBRUCH: %s\n\n%s\n" % (collect.iso(now), commit, e, "\n".join(log)))
        raise
    data = backtest.read_data(data_file, symbol=auto_sol.COIN)
    h = health(db, data, now, max_age_minutes, min_fetches)
    fetch_warn = results[0][2]["auto"]["warnings"]
    for wn in fetch_warn:
        h["warnings"].insert(0, "VERALTET: heute fehlgeschlagen, Bericht auf gespeicherten Daten: %s" % wn)
    with open(data_file, "rb") as fh:
        raw = fh.read()
    sha = hashlib.sha256(raw).hexdigest()
    with gzip.open(data_file + ".gz", "wb") as fh:
        fh.write(raw)
    os.remove(data_file)

    d0 = results[0][2]["data"]
    head = ["SOL-Perps Tagesbericht (%s), Daten sammeln und Trades simulieren, keine Orders" % auto_sol.MARKET,
            "Lauf %s UTC, Git-Commit %s%s" % (collect.iso(now), commit,
                                              " (mit lokalen Änderungen)" if dirty else ("" if dirty is not None
                                                                                       else " (git nicht gefunden)")),
            "Daten %s bis %s, %d abgeschlossene 1m-Kerzen, as_of %s, neueste Kerze %.0f min alt"
            % (d0["first"], d0["last"], d0["closed_candles"], d0["as_of"], h["age_minutes"]),
            "Abrufe der letzten 24 h: %d erfolgreich, %d fehlgeschlagen" % (h["ok_fetches_24h"],
                                                                           len(h["failed_fetches_24h"]))]
    head += ["!! %s" % wn for wn in h["warnings"]] or ["Datenzustand: aktuell, keine Lücken, keine Fehler"]
    body = []
    for usd, role, res in results:
        body += account_block(usd, role, res)
    foot = ["", "Spread und Slippage sind Modellannahmen, Funding ist nicht eingerechnet. Alles simuliert, keine Order.",
            "Kerzen: SOL-1m.json.gz (sha256 der entpackten Datei %s)" % sha]
    text = "\n".join(head + body + foot) + "\n"
    with open(os.path.join(folder, "bericht.txt"), "w", encoding="utf-8") as fh:
        fh.write(text)
    with open(os.path.join(folder, "lauf.log"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(log) + "\n")

    info = {"run_utc": collect.iso(now), "commit": commit, "dirty": dirty, "market": auto_sol.MARKET,
            "data": {"first": d0["first"], "last": d0["last"], "closed_candles": d0["closed_candles"],
                     "as_of": d0["as_of"], "sha256_uncompressed": sha, "file": "SOL-1m.json.gz"},
            "health": h, "accounts": []}
    index_rows = []
    for usd, role, res in results:
        sg = res["runs"][0]["signal"]
        lo, sh, tot = side(res, ("*", "long")), side(res, ("*", "short")), side(res, ("*", "*"))
        acc = {"account_usd": usd, "role": role, "role_text": ROLES[role], "status": sg["status"],
               "status_reasons": sg["reasons"],
               "long": {k: lo[k] for k in ("trades", "known", "unknown", "gross", "fees", "spread", "slippage", "net")},
               "short": {k: sh[k] for k in ("trades", "known", "unknown", "gross", "fees", "spread", "slippage", "net")},
               "skipped": tot["skipped"], "report_dir": os.path.relpath(res["auto"]["report_dir"], folder)}
        info["accounts"].append(acc)
        index_rows.append({"run_utc": info["run_utc"], "commit": commit, "dirty": dirty, "account_usd": usd,
                           "role": role, "data_first": d0["first"], "data_last": d0["last"], "as_of": d0["as_of"],
                           "stale": h["stale"] or bool(fetch_warn), "gap_minutes": h["gap_minutes"],
                           "failed_fetches_24h": len(h["failed_fetches_24h"]), "ok_fetches_24h": h["ok_fetches_24h"],
                           "long_trades": lo["trades"], "long_gross": round(lo["gross"], 6), "long_net": round(lo["net"], 6),
                           "short_trades": sh["trades"], "short_gross": round(sh["gross"], 6),
                           "short_net": round(sh["net"], 6),
                           "skipped": " ".join("%s=%d" % kv for kv in tot["skipped"].items()),
                           "status": sg["status"], "report_dir": os.path.basename(folder)})
    with open(os.path.join(folder, "run-info.json"), "w", encoding="utf-8") as fh:
        json.dump(info, fh, indent=1, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    append_index(os.path.join(archive, "index.csv"), index_rows)
    with open(os.path.join(archive, "letzter-bericht.txt"), "w", encoding="utf-8") as fh:
        fh.write("Ordner: %s\n\n%s" % (os.path.basename(folder), text))
    alarm = os.path.join(archive, "ACHTUNG-DATENZUSTAND.txt")
    if h["warnings"]:
        with open(alarm, "w", encoding="utf-8") as fh:
            fh.write("Lauf %s UTC (%s):\n%s\n" % (collect.iso(now), os.path.basename(folder),
                                                   "\n".join("!! %s" % wn for wn in h["warnings"])))
    elif os.path.exists(alarm):
        os.remove(alarm)
    out("\n" + text.rstrip("\n"))
    out("\nGespeichert: %s" % folder)
    return folder, (2 if h["warnings"] else 0)


def main(argv=None, post=None):
    ap = argparse.ArgumentParser(prog="daily_report.py", description="Archived daily SOL-perps report (simulated).")
    ap.add_argument("--db", default=os.environ.get("HL_BACKTEST_DB"), help="the SQLite the hourly task fills")
    ap.add_argument("--archive", default=os.environ.get("HL_BACKTEST_ARCHIVE"), help="folder for all daily reports")
    ap.add_argument("--account", action="append", help="USD=ROLE, ROLE regelwerk or szenario "
                                                       "(default 250=regelwerk and 1000=szenario)")
    ap.add_argument("--config", help="backtest config (the SOL limits are checked)")
    ap.add_argument("--max-age-minutes", type=int, default=90)
    ap.add_argument("--min-fetches-24h", type=int, default=20)
    a = ap.parse_args(argv)
    try:
        if not a.db or not a.archive:
            raise ReportError("--db and --archive are required (or HL_BACKTEST_DB, HL_BACKTEST_ARCHIVE)")
        _, code = daily(a.db, a.archive, parse_accounts(a.account or DEFAULT_ACCOUNTS), a.config, post=post,
                        max_age_minutes=a.max_age_minutes, min_fetches=a.min_fetches_24h)
    except (ReportError, auto_sol.AutoError, backtest.BTError, collect.CollectError) as e:
        print("ABBRUCH: %s" % e, file=sys.stderr)
        return 1
    return code


if __name__ == "__main__":
    sys.exit(main())
