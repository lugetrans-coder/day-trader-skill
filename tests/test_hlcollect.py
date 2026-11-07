"""hl-backtest/collect.py tests, offline: a fake info endpoint stands in for Hyperliquid."""

import contextlib
import io
import json
import re
import os
import shutil
import sqlite3
import tempfile
import unittest
import urllib.error

from _load import FIX, ROOT, load

col = load("hl-backtest", "collect")
bt = load("hl-backtest", "backtest")
MIN = 60000
T0 = 1794614400000          # 2026-11-14T00:00Z


def candle(t, px=89.0, v="10.0"):
    return {"t": t, "T": t + MIN - 1, "s": "SOL", "i": "1m", "o": "%.3f" % px, "h": "%.3f" % (px + 0.2),
            "l": "%.3f" % (px - 0.2), "c": "%.3f" % (px + 0.05), "v": v, "n": 7}


class FakeInfo(object):
    """candleSnapshot like Hyperliquid's: candles in [startTime, endTime], the forming one included,
    at most 5000, the most recent ones."""

    def __init__(self, minutes, missing=(), oldest_first=False):
        self.oldest_first = oldest_first
        self.horizon = None          # like the real endpoint: nothing older than the last 5000 minutes
        self.book = {T0 + i * MIN: candle(T0 + i * MIN, 89.0 + 0.01 * i) for i in range(minutes) if i not in missing}
        self.calls = []
        self.meta = {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
                                  {"name": "SOL", "szDecimals": 2, "maxLeverage": 20}]}

    def __call__(self, payload):
        self.calls.append(payload)
        if payload["type"] == "meta":
            return self.meta
        req = payload["req"]
        rows = [dict(c) for t, c in sorted(self.book.items()) if req["startTime"] <= t <= req["endTime"]
                and (self.horizon is None or t >= self.horizon)]
        return rows[:col.SERVED] if self.oldest_first else rows[-col.SERVED:]


def quiet(*a, **k):
    pass


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "sol.sqlite")
        self.db = col.connect(self.path)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def fetch(self, info, at_min, **kw):
        kw.setdefault("lookback_minutes", 30)
        return col.fetch(self.db, info, now=T0 + at_min * MIN + 30000, settle_seconds=2, sleep=quiet, log=quiet, **kw)

    def count(self):
        n, d = self.db.execute("SELECT COUNT(*), COUNT(DISTINCT t) FROM candles").fetchone()
        return n, d

    def test_overlapping_fetches_make_no_duplicates(self):
        info = FakeInfo(120)
        s1 = self.fetch(info, 30, lookback_minutes=30)      # 00:00..00:29 closed, 00:30 still forming
        self.assertEqual((s1["inserted"], s1["open_dropped"]), (30, 1))
        s2 = self.fetch(info, 45, overlap_minutes=10)       # starts at 00:19: 11 overlap, 15 new
        self.assertEqual(info.calls[-1]["req"]["startTime"], T0 + 19 * MIN)
        self.assertEqual((s2["inserted"], s2["unchanged"], s2["changed"], s2["open_dropped"]), (15, 11, [], 1))
        s3 = self.fetch(info, 45, overlap_minutes=10)       # the same window again
        self.assertEqual((s3["inserted"], s3["unchanged"]), (0, 11))
        self.assertEqual(self.count(), (45, 45))
        self.assertEqual(self.db.execute("SELECT MIN(t), MAX(t) FROM candles").fetchone(), (T0, T0 + 44 * MIN))

    def test_the_forming_candle_is_stored_once_it_closes_and_revisions_are_counted(self):
        info = FakeInfo(60)
        self.fetch(info, 20)
        self.assertIsNone(self.db.execute("SELECT 1 FROM candles WHERE t=?", (T0 + 20 * MIN,)).fetchone())
        info.book[T0 + 15 * MIN]["v"] = "11.5"               # the exchange revised a closed candle
        s = self.fetch(info, 25, overlap_minutes=10)
        self.assertEqual(s["changed"], [T0 + 15 * MIN])
        self.assertEqual(self.db.execute("SELECT v FROM candles WHERE t=?", (T0 + 15 * MIN,)).fetchone()[0], "11.5")
        self.assertIsNotNone(self.db.execute("SELECT 1 FROM candles WHERE t=?", (T0 + 20 * MIN,)).fetchone())
        self.assertEqual(self.count(), (25, 25))

    def test_gaps_are_reported(self):
        info = FakeInfo(60, missing=(7, 8, 30))
        s = self.fetch(info, 40, lookback_minutes=40)
        self.assertEqual(s["gaps"], [(T0 + 7 * MIN, 2), (T0 + 30 * MIN, 1)])

    def test_history_older_than_what_is_served_is_flagged(self):
        info = FakeInfo(6000)
        self.fetch(info, 30)                                  # 00:00..00:29
        info.horizon = T0 + (5990 - 5000) * MIN               # three and a half days later
        s = self.fetch(info, 5990, overlap_minutes=10)
        self.assertTrue(s["history_lost"])
        self.assertEqual(s["gaps"], [(T0 + 30 * MIN, 990 - 30)])

    def test_paging_whichever_end_the_endpoint_serves(self):
        for oldest in (False, True):
            self.db.execute("DELETE FROM candles")
            info = FakeInfo(7000, oldest_first=oldest)
            self.fetch(info, 10, lookback_minutes=10)
            old = col.SERVED
            col.SERVED = 100                                   # a small page to see the paging
            try:
                s = self.fetch(info, 400, overlap_minutes=5)
            finally:
                col.SERVED = old
            self.assertGreater(s["requests"], 1, oldest)
            self.assertEqual(self.count(), (400, 400), oldest)
            self.assertEqual(s["gaps"], [], oldest)

    def test_failed_fetch_is_logged_and_retried(self):
        sleeps = []

        def down(payload):
            raise urllib.error.HTTPError(col.INFO_URL, 503, "Service Unavailable", {}, None)

        with self.assertRaisesRegex(col.CollectError, "HTTP 503"):
            col.fetch(self.db, down, now=T0, sleep=sleeps.append, log=quiet)
        self.assertEqual(len(sleeps), col.RETRIES - 1)
        row = self.db.execute("SELECT status, error FROM fetches").fetchone()
        self.assertEqual(row[0], "failed")
        self.assertIn("503", row[1])

        def bad_request(payload):
            raise urllib.error.HTTPError(col.INFO_URL, 422, "Unprocessable", {}, None)

        sleeps[:] = []
        with self.assertRaises(col.CollectError):
            col.fetch(self.db, bad_request, now=T0, sleep=sleeps.append, log=quiet)
        self.assertEqual(sleeps, [])                           # not retried

    def test_invalid_candles_are_not_stored(self):
        info = FakeInfo(20)
        info.book[T0 + 5 * MIN]["h"] = "1.0"
        info.book[T0 + 6 * MIN]["s"] = "BTC"
        s = self.fetch(info, 20, lookback_minutes=20)
        self.assertEqual(s["invalid"], 2)
        self.assertEqual(self.count(), (18, 18))

    def test_export_is_read_by_the_backtest(self):
        info = FakeInfo(200, missing=(50,))
        col.fetch_meta(self.db, info, sleep=quiet, log=quiet)
        self.fetch(info, 120, lookback_minutes=120)
        self.fetch(info, 150, overlap_minutes=10)
        doc = col.export(self.db)
        self.assertEqual(doc["closed_candles"], 149)
        self.assertEqual(doc["as_of_utc"], "2026-11-14T02:30:28Z")
        self.assertEqual(doc["exchange_meta"]["szDecimals"], 2)
        self.assertEqual(doc["gaps"], [{"from_utc": "2026-11-14T00:50:00Z", "minutes": 1}])
        out = os.path.join(self.tmp, "SOL-1m.json")
        with open(out, "w") as fh:
            json.dump(doc, fh)
        data = bt.read_data(out, symbol="SOL")
        self.assertEqual((data["count"], data["open_dropped"], data["notes"]), (149, [], []))
        self.assertEqual(data["exchange_meta"]["szDecimals"], 2)
        res = bt.backtest(data, bt.load_config(None, {"price_rule": "hyperliquid"}), runs=("A", "B", "A+B"))
        self.assertEqual([r["name"] for r in res["runs"]], ["A", "B", "A+B"])
        self.assertEqual(res["data"]["gaps"], [{"from": "2026-11-14 00:50Z", "minutes": 1}])

    def test_export_needs_a_successful_fetch(self):
        with self.assertRaisesRegex(col.CollectError, "no SOL 1m candles"):
            col.export(self.db)

    def test_meta(self):
        row = col.fetch_meta(self.db, FakeInfo(1), sleep=quiet, log=quiet)
        self.assertEqual(row["szDecimals"], 2)
        self.assertEqual(col.read_meta(self.db, "SOL")["szDecimals"], 2)
        with self.assertRaisesRegex(col.CollectError, "not in the perps universe"):
            col.fetch_meta(self.db, FakeInfo(1), coin="XYZ", sleep=quiet, log=quiet)


class PriceRule(unittest.TestCase):
    def test_documented_perps_rule(self):
        # 5 significant figures, at most 6 - szDecimals decimals, integers always fine
        for px, sz, dec in ((89.254, 2, 3), (150.0, 2, 2), (9.5, 2, 4), (0.5, 2, 4), (1234.5, 1, 1),
                            (65432.0, 5, 0), (123456.0, 5, 0), (0.001234, 0, 6)):
            self.assertEqual(col.price_decimals(px, sz), dec, (px, sz))
            self.assertEqual(bt.hl_price_decimals(px, sz), dec, (px, sz))

    def test_backtest_rounds_with_the_rule(self):
        c = bt.load_config(None, {"price_rule": "hyperliquid"})
        self.assertEqual(bt.round_price(89.2546, c, "floor"), 89.254)
        self.assertEqual(bt.round_price(89.2541, c, "ceil"), 89.255)
        self.assertEqual(bt.round_price(150.019, c, "floor"), 150.01)
        self.assertEqual(bt.round_price(99.99961, c, "ceil"), 100.0)
        self.assertEqual(bt.round_price(100.0049, c, "floor"), 100.0)

    def test_fixed_tick_mismatch_is_reported(self):
        data = bt.read_data(os.path.join(FIX, "hl-backtest", "SOL-1m-synthetic.json"), symbol="SOL")
        res = bt.backtest(data, bt.load_config(None, {}), runs=("A",))
        self.assertTrue(any("tick_size 0.01 passt nicht" in n for n in res["data"]["notes"]))
        self.assertEqual(res["model_assumptions"]["funding"], "nicht eingerechnet")
        self.assertIn("Modellannahme", res["model_assumptions"]["spread"])
        with self.assertRaises(bt.BTError):
            bt.load_config(None, {"price_rule": "round"})


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "data", "sol.sqlite")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_cli(self, argv, post):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = col.main(argv, post=post)
        return code, out.getvalue(), err.getvalue()

    def test_fetch_export_backtest(self):
        info = FakeInfo(100, missing=(40,))
        code, text, _ = self.run_cli(["meta", "--db", self.db], info)
        self.assertEqual(code, 0)
        self.assertIn("szDecimals 2", text)
        self.assertIn("6 - 2 = 4 Nachkommastellen", text)
        code, text, _ = self.run_cli(["fetch", "--db", self.db, "--now", "2026-11-14T01:30:30Z",
                                      "--lookback-minutes", "90"], info)
        self.assertEqual(code, 0, text)
        self.assertIn("89 neu", text)
        self.assertIn("Datenlücken: 1 (1 fehlende Minuten)", text)
        out = os.path.join(self.tmp, "SOL-1m.json")
        code, text, _ = self.run_cli(["export", "--db", self.db, "--out", out], info)
        self.assertEqual(code, 0)
        self.assertIn("as_of 2026-11-14T01:30:28Z", text)
        o2, e2 = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(o2), contextlib.redirect_stderr(e2):
            code = bt.main(["run", "--data", out, "--price-rule", "hyperliquid", "--account", "1000",
                            "--run", "A", "--run", "B", "--run", "A+B"])
        self.assertEqual((code, e2.getvalue()), (0, ""))
        self.assertIn("Funding nicht eingerechnet", o2.getvalue())
        self.assertIn("szDecimals 2", o2.getvalue())

    def test_failed_fetch_says_fehler(self):
        def down(payload):
            raise urllib.error.URLError("Tunnel connection failed: 403 Forbidden")
        old = col.PAUSE
        col.PAUSE = 0
        try:
            code, _, err = self.run_cli(["fetch", "--db", self.db], down)
        finally:
            col.PAUSE = old
        self.assertEqual(code, 1)
        self.assertIn("FEHLER: info candleSnapshot: URLError: Tunnel connection failed: 403 Forbidden", err)
        code, text, _ = self.run_cli(["status", "--db", self.db], down)
        self.assertEqual(code, 1)     # nothing stored yet

    def test_paths_inside_a_checkout_are_refused(self):
        code, _, err = self.run_cli(["fetch", "--db", os.path.join(ROOT, "sol.sqlite")], FakeInfo(1))
        self.assertEqual(code, 1)
        self.assertIn("inside the git checkout", err)
        self.assertFalse(os.path.exists(os.path.join(ROOT, "sol.sqlite")))
        code, _, err = self.run_cli(["export", "--db", self.db, "--out", os.path.join(ROOT, "x.json")], FakeInfo(1))
        self.assertIn("inside the git checkout", err)
        code, _, err = self.run_cli(["fetch"], FakeInfo(1))
        self.assertIn("--db is required", err)


class Safety(unittest.TestCase):
    def test_public_market_data_only(self):
        with open(os.path.join(ROOT, "hl-backtest", "collect.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertEqual(set(re.findall(r"https://([\w.\-]+)", src)), {"api.hyperliquid.xyz"})
        self.assertIn('INFO_URL = "https://api.hyperliquid.xyz/info"', src)
        for bad in ("/exchange", "private_key", "eth_account", "signature", "api_key", "order("):
            self.assertNotIn(bad, src, bad)
        self.assertNotRegex(src, r"(?<![\w.])(list|dict|tuple)\[")

    def test_the_backtest_stays_offline(self):
        with open(os.path.join(ROOT, "hl-backtest", "backtest.py"), encoding="utf-8") as fh:
            src = fh.read()
        for bad in ("urllib", "socket", "http.client", "import collect", "sqlite3"):
            self.assertNotIn(bad, src, bad)


if __name__ == "__main__":
    unittest.main()
