"""hl-backtest/daily_report.py and the Windows task scripts. Offline: a fake info endpoint."""

import csv
import gzip
import json
import os
import re
import shutil
import tempfile
import unittest

import test_hlauto as ha
from _load import ROOT, load

dr = load("hl-backtest", "daily_report")
col = dr.collect
MIN = 60000
WIN = os.path.join(ROOT, "hl-backtest", "windows")
BREAKOUT = ha.RANGE + [(89.30, 89.55, 89.25, 89.50), (89.50, 89.95, 89.45, 89.92)] + \
    [(89.92, 89.95, 89.90, 89.92)] * 13 + ha.RANGE + [(88.90, 88.95, 88.65, 88.70), (88.70, 88.75, 88.20, 88.25)] + \
    [(88.25, 88.30, 88.20, 88.25)] * 14                      # 61 minutes: one LONG and one SHORT trade at 1,000 $


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def no_git():
    return "abc1234", False


class Daily(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "home", "sol.sqlite")
        self.archive = os.path.join(self.tmp, "home", "reports")
        os.makedirs(os.path.dirname(self.db))
        self.lines = []
        self.old = (col.now_ms, col.PAUSE)
        col.PAUSE = 0

    def tearDown(self):
        col.now_ms, col.PAUSE = self.old
        shutil.rmtree(self.tmp)

    def go(self, rows, at_min, post=None, **kw):
        now = ha.T0 + at_min * MIN + 30000
        col.now_ms = lambda: now
        kw.setdefault("git", no_git)
        return dr.daily(self.db, self.archive, dr.parse_accounts(dr.DEFAULT_ACCOUNTS), post=post or ha.Fake(rows),
                        now=now, out=self.lines.append, **kw)

    def index(self):
        with open(os.path.join(self.archive, "index.csv"), encoding="utf-8-sig") as fh:
            return list(csv.DictReader(fh, delimiter=";"))

    def test_archived_folder_with_date_and_commit(self):
        folder, code = self.go(BREAKOUT, 61, min_fetches=1)
        self.assertEqual(os.path.basename(folder), "2026-11-14_0101Z_abc1234")
        self.assertEqual(code, 0)
        for f in ("bericht.txt", "run-info.json", "lauf.log", "SOL-1m.json.gz",
                  os.path.join("account-250", "report", "report.txt"),
                  os.path.join("account-1000", "report", "trades.csv")):
            self.assertTrue(os.path.exists(os.path.join(folder, f)), f)
        self.assertFalse(os.path.exists(os.path.join(folder, "SOL-1m.json")))
        with gzip.open(os.path.join(folder, "SOL-1m.json.gz")) as fh:
            self.assertEqual(json.load(fh)["market"], "SOL-PERP")
        info = json.loads(read(os.path.join(folder, "run-info.json")))
        self.assertEqual((info["commit"], info["dirty"], info["market"]), ("abc1234", False, "hyperliquid:SOL-PERP"))
        self.assertFalse(os.path.exists(os.path.join(self.archive, "ACHTUNG-DATENZUSTAND.txt")))

    def test_long_and_short_apart_for_both_accounts(self):
        folder, _ = self.go(BREAKOUT, 61, min_fetches=1)
        text = read(os.path.join(folder, "bericht.txt"))
        self.assertIn("=== Konto 250 $ (Vergleich: aktuelles Regelwerk) ===", text)
        self.assertIn("=== Konto 1.000 $ (separates Simulationsszenario) ===", text)
        self.assertEqual(len(re.findall(r"(?m)^LONG   Trades", text)), 2)
        self.assertEqual(len(re.findall(r"(?m)^SHORT  Trades", text)), 2)
        rows = {r["role"]: r for r in self.index()}
        self.assertEqual((rows["regelwerk"]["long_trades"], rows["regelwerk"]["short_trades"]), ("0", "0"))
        self.assertIn("skipped_min_notional=2", rows["regelwerk"]["skipped"])
        self.assertEqual((rows["szenario"]["long_trades"], rows["szenario"]["short_trades"]), ("1", "1"))
        self.assertGreater(float(rows["szenario"]["long_gross"]), float(rows["szenario"]["long_net"]))
        info = json.loads(read(os.path.join(folder, "run-info.json")))
        acc = {a["role"]: a for a in info["accounts"]}
        self.assertEqual(acc["szenario"]["long"]["trades"], 1)
        self.assertEqual(acc["szenario"]["short"]["trades"], 1)

    def test_earlier_reports_are_kept(self):
        f1, _ = self.go(BREAKOUT, 61, min_fetches=1)
        before = read(os.path.join(f1, "bericht.txt"))
        f2, _ = self.go(BREAKOUT, 61, min_fetches=1)
        self.assertEqual(os.path.basename(f2), "2026-11-14_0101Z_abc1234-2")
        self.assertEqual(read(os.path.join(f1, "bericht.txt")), before)
        self.assertEqual(len(self.index()), 4)
        with open(os.path.join(self.archive, "index.csv"), encoding="utf-8-sig") as fh:
            self.assertEqual(sum(1 for line in fh if line.startswith("run_utc")), 1)
        latest = read(os.path.join(self.archive, "letzter-bericht.txt"))
        self.assertTrue(latest.startswith("Ordner: 2026-11-14_0101Z_abc1234-2"))

    def test_dirty_checkout_and_no_git(self):
        folder, _ = self.go(BREAKOUT, 61, min_fetches=1, git=lambda: ("abc1234", True))
        self.assertTrue(folder.endswith("_abc1234-dirty"))
        self.assertIn("(mit lokalen Änderungen)", read(os.path.join(folder, "bericht.txt")))

        class R(object):
            returncode, stdout = 128, ""
        self.assertEqual(dr.git_info(run=lambda *a, **k: R()), ("nogit", None))

        def missing(*a, **k):
            raise FileNotFoundError("git")
        self.assertEqual(dr.git_info(run=missing), ("nogit", None))

    def test_too_few_hourly_fetches_is_a_warning(self):
        folder, code = self.go(BREAKOUT, 61)                    # one fetch, 20 expected
        self.assertEqual(code, 2)
        text = read(os.path.join(folder, "bericht.txt"))
        self.assertIn("!! ABRUFE: nur 1 erfolgreiche Abrufe in den letzten 24 h", text)
        alarm = os.path.join(self.archive, "ACHTUNG-DATENZUSTAND.txt")
        self.assertTrue(os.path.exists(alarm))
        _, code = self.go(BREAKOUT, 61, min_fetches=1)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(alarm))                 # cleared by the next clean run

    def test_gaps_are_visible(self):
        fake = ha.Fake(BREAKOUT)
        fake.candles = [c for c in fake.candles if c["t"] != ha.T0 + 40 * MIN]
        folder, code = self.go(BREAKOUT, 61, post=fake, min_fetches=1)
        self.assertEqual(code, 2)
        text = read(os.path.join(folder, "bericht.txt"))
        self.assertIn("!! DATENLÜCKEN: 1, zusammen 1 min: 2026-11-14 00:40Z (1 min)", text)
        self.assertEqual(self.index()[0]["gap_minutes"], "1")

    def test_stale_data_and_failed_fetch_are_visible(self):
        self.go(BREAKOUT, 61, min_fetches=1)

        def down(payload):
            if payload["type"] == "meta":
                return {"universe": [{"name": "SOL", "szDecimals": 2}]}
            raise col.urllib.error.URLError("Tunnel connection failed: 403 Forbidden")

        folder, code = self.go(BREAKOUT, 61 + 180, post=down, min_fetches=1)   # three hours later, fetch fails
        self.assertEqual(code, 2)
        text = read(os.path.join(folder, "bericht.txt"))
        self.assertIn("!! VERALTET: heute fehlgeschlagen, Bericht auf gespeicherten Daten", text)
        self.assertIn("!! VERALTET: die neueste Kerze 2026-11-14 01:00Z ist 180 min alt (Grenze 90 min)", text)
        self.assertIn("!! ABRUFE: 1 fehlgeschlagen in den letzten 24 h", text)
        self.assertIn("veraltet: jetzt", text)                   # the status is FLAT, not a stale signal
        rows = self.index()
        self.assertEqual([r["stale"] for r in rows[-2:]], ["True", "True"])
        self.assertEqual([r["status"] for r in rows[-2:]], ["FLAT", "FLAT"])

    def test_abort_leaves_a_note(self):
        def broken(payload):
            raise col.urllib.error.URLError("offline")
        with self.assertRaises(dr.auto_sol.AutoError):
            self.go(BREAKOUT, 61, post=broken)                  # nothing stored, no meta: the export fails
        folders = [d for d in os.listdir(self.archive) if d.startswith("2026-11-14")]
        self.assertEqual(len(folders), 1)
        note = read(os.path.join(self.archive, folders[0], "ABGEBROCHEN.txt"))
        self.assertIn("ABBRUCH", note)

    def test_accounts(self):
        self.assertEqual(dr.parse_accounts(dr.DEFAULT_ACCOUNTS), [(250.0, "regelwerk"), (1000.0, "szenario")])
        for bad in (["x=regelwerk"], ["-5=szenario"], ["250=live"], ["250=regelwerk", "250=szenario"]):
            with self.assertRaises(dr.ReportError, msg=str(bad)):
                dr.parse_accounts(bad)

    def test_cli_paths(self):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(dr.main(["--db", self.db]), 1)
        self.assertIn("--archive are required", err.getvalue())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(dr.main(["--db", self.db, "--archive", os.path.join(ROOT, "reports")]), 1)
        self.assertIn("inside the git checkout", err.getvalue())


class WindowsScripts(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(WIN, name), "rb") as fh:
            raw = fh.read()
        return raw

    def test_ascii_only_for_windows_powershell_5(self):
        for f in sorted(os.listdir(WIN)):
            self.assertTrue(f.endswith(".ps1"), f)
            raw = self.read(f)
            self.assertTrue(all(b < 128 for b in raw), "%s has non-ASCII bytes" % f)

    def test_the_tasks_call_the_right_tools(self):
        common = self.read("hl-common.ps1").decode()
        self.assertIn("Join-Path $env:LOCALAPPDATA 'day-trader-skill'", common)
        self.assertIn("Join-Path $HlHome 'sol.sqlite'", common)
        hourly = self.read("collect_hourly.ps1").decode()
        self.assertIn("@($collect, 'fetch', '--db', $HlDb, '--coin', 'SOL')", hourly)
        daily = self.read("daily_report.ps1").decode()
        self.assertIn("'--db', $HlDb, '--archive', $HlReports", daily)
        self.assertIn("'--account', '250=regelwerk', '--account', '1000=szenario'", daily)
        reg = self.read("register_tasks.ps1").decode()
        for word in ("collect_hourly.ps1", "daily_report.ps1", "-RepetitionInterval (New-TimeSpan -Hours 1)",
                     "-Daily -At $daily", "-LogonType Interactive", "-MultipleInstances IgnoreNew"):
            self.assertIn(word, reg, word)

    def test_no_order_or_download_code(self):
        for f in os.listdir(WIN):
            src = self.read(f).decode()
            for bad in ("Invoke-WebRequest", "Invoke-RestMethod", "/exchange", "api_key", "private"):
                self.assertNotIn(bad, src, "%s: %s" % (f, bad))
        with open(os.path.join(ROOT, "hl-backtest", "daily_report.py"), encoding="utf-8") as fh:
            src = fh.read()
        for bad in ("urllib", "/exchange", "private_key", "signature", "api_key"):
            self.assertNotIn(bad, src, bad)


if __name__ == "__main__":
    unittest.main()
