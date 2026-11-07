"""The automated SOL-perps run (hl-backtest/auto_sol.py), the market check and the LONG / SHORT / FLAT
status. Offline: a fake info endpoint stands in for Hyperliquid."""

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from _load import FIX, ROOT, load

auto = load("hl-backtest", "auto_sol")
bt = auto.backtest
col = auto.collect
MIN = 60000
T0 = 1794614400000          # 2026-11-14T00:00Z, a cycle anchor
SYNTH = os.path.join(FIX, "hl-backtest", "SOL-1m-synthetic.json")

# range 88.80 .. 89.40 (mid 89.10), wide enough for the costs, narrow enough for the cap
RANGE = [(89.10, 89.20, 89.00, 89.10)] * 15
RANGE = RANGE[:4] + [(89.10, 89.40, 89.00, 89.10)] + RANGE[5:9] + [(89.10, 89.20, 88.80, 89.10)] + RANGE[10:]


def hl(t, row, sym="SOL"):
    o, h, l, c = row
    return {"t": t, "T": t + MIN - 1, "s": sym, "i": "1m", "o": "%.3f" % o, "h": "%.3f" % h, "l": "%.3f" % l,
            "c": "%.3f" % c, "v": "100.0", "n": 5}


def rows_to_candles(rows, sym="SOL"):
    return [hl(T0 + i * MIN, r, sym) for i, r in enumerate(rows) if r is not None]


def doc(rows, as_of=None, **meta):
    d = {"venue": "hyperliquid", "market_type": "perp", "market": "SOL-PERP", "symbol": "SOL", "interval": "1m",
         "as_of": as_of if as_of is not None else T0 + len(rows) * MIN + 30000, "candles": rows_to_candles(rows),
         "exchange_meta": {"szDecimals": 2}}
    d.update(meta)
    return d


def status(rows, account=1000.0, **kw):
    data = bt.parse_candles(doc(rows, **kw))
    cfg = bt.load_config(None, {"price_rule": "hyperliquid", "account_value_usd": account})
    bt.check_market(data, auto.MARKET, cfg)
    res = bt.backtest(data, cfg, runs=("A+B",))
    return res["runs"][0]["signal"], res


class Fake(object):
    """candleSnapshot and meta like Hyperliquid's; the candle at index `forming` is still open."""

    def __init__(self, rows, sym="SOL"):
        self.candles = rows_to_candles(rows, sym)
        self.calls = []

    def __call__(self, payload):
        self.calls.append(payload)
        if payload["type"] == "meta":
            return {"universe": [{"name": "SOL", "szDecimals": 2, "maxLeverage": 20}]}
        r = payload["req"]
        return [dict(c) for c in self.candles if r["startTime"] <= c["t"] <= r["endTime"]][-col.SERVED:]


class Status(unittest.TestCase):
    def test_long_on_a_breakout_close(self):
        sg, _ = status(RANGE + [(89.30, 89.55, 89.25, 89.50)])
        self.assertEqual((sg["status"], sg["setup"], sg["side"]), ("LONG", "A", "long"))
        self.assertEqual((sg["signal_candle"], sg["entry_candle"]), ("2026-11-14 00:15Z", "2026-11-14 00:16Z"))
        self.assertEqual((sg["entry_estimate"], sg["stop"], sg["target"]), (89.5, 89.1, 89.9))
        self.assertEqual(sg["qty"], 0.22)                        # 2 % of 1,000 $ = 20 $ / 89.518 -> 0.22
        self.assertLessEqual(sg["position_value"], 20.0)
        self.assertGreater(sg["est_loss_at_stop"], 0)

    def test_short_on_a_breakdown_close(self):
        sg, _ = status(RANGE + [(88.90, 88.95, 88.65, 88.70)])
        self.assertEqual((sg["status"], sg["setup"], sg["stop"], sg["target"]), ("SHORT", "A", 89.1, 88.3))

    def test_flat_without_a_setup(self):
        sg, _ = status(RANGE + [(89.10, 89.20, 89.00, 89.15)])
        self.assertEqual(sg["status"], "FLAT")
        self.assertIn("kein Setup schließt", sg["reasons"][0])
        sg, _ = status(RANGE[:10])                               # inside the range phase
        self.assertEqual(sg["status"], "FLAT")

    def test_flat_when_the_size_is_under_the_minimum(self):
        sg, _ = status(RANGE + [(89.30, 89.55, 89.25, 89.50)], account=250)
        self.assertEqual(sg["status"], "FLAT")
        self.assertIn("skipped_min_notional", sg["reasons"][0])

    def test_flat_while_a_simulated_position_is_open(self):
        rows = RANGE + [(89.30, 89.45, 89.25, 89.42), (89.42, 89.44, 89.35, 89.41), (89.41, 89.43, 89.30, 89.36)]
        sg, res = status(rows)
        self.assertEqual(sg["status"], "FLAT")                    # B short closes at 00:17, A long still open
        self.assertIn("B SHORT: skipped_position_open", sg["reasons"][0])
        self.assertIn("A LONG seit 2026-11-14 00:16Z", sg["open_position"])
        self.assertEqual(res["runs"][0]["trades"][0]["exit_kind"], "unknown_data_end")

    def test_flat_on_stale_data_or_without_as_of(self):
        rows = RANGE + [(89.30, 89.55, 89.25, 89.50)]
        sg, _ = status(rows, as_of=T0 + 17 * MIN + 30000)        # the 00:16 candle should be there
        self.assertEqual(sg["status"], "FLAT")
        self.assertIn("veraltet", sg["reasons"][0])
        d = doc(rows)
        del d["as_of"]
        data = bt.parse_candles(d)
        cfg = bt.load_config(None, {"price_rule": "hyperliquid", "account_value_usd": 1000})
        sg = bt.backtest(data, cfg, runs=("A+B",))["runs"][0]["signal"]
        self.assertEqual(sg["status"], "FLAT")
        self.assertIn("kein Datenstand", sg["reasons"][0])


class Market(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def cli(self, d, market=auto.MARKET):
        path = os.path.join(self.tmp, "in.json")
        with open(path, "w") as fh:
            json.dump(d, fh)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bt.main(["run", "--data", path, "--market", market, "--price-rule", "hyperliquid", "--run", "A+B"])
        return code, out.getvalue(), err.getvalue()

    def test_sol_perp_passes(self):
        code, text, err = self.cli(doc(RANGE + [(89.30, 89.55, 89.25, 89.50)]))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Markt: hyperliquid:SOL-PERP (Handelsplatz, Markttyp und Symbol jeder Kerze geprüft)", text)
        self.assertIn("Status (Signal-Kerze 2026-11-14 00:15Z, simuliert, keine Order): LONG", text)

    def test_other_symbol_stops_with_a_clear_message(self):
        d = doc(RANGE, symbol="BTC", market="BTC-PERP")
        d["candles"] = rows_to_candles(RANGE, "BTC")
        code, text, err = self.cli(d)
        self.assertEqual((code, text), (1, ""))
        self.assertIn("Eingabedaten gehören nicht zum Markt hyperliquid:SOL-PERP", err)
        self.assertIn("Datei-Symbol BTC statt SOL", err)
        self.assertIn("Kerzen mit Symbol BTC", err)
        self.assertIn("Abbruch, kein Backtest", err)

    def test_spot_or_unknown_venue_stops(self):
        for kw, words in (({"market_type": "spot"}, "Markttyp spot statt perp"),
                          ({"venue": "binance"}, "Handelsplatz binance statt hyperliquid"),
                          ({"market": "SOL-SPOT"}, "Markt SOL-SPOT statt SOL-PERP")):
            code, _, err = self.cli(doc(RANGE, **kw))
            self.assertEqual(code, 1, kw)
            self.assertIn(words, err, kw)

    def test_a_file_without_market_fields_stops(self):
        with open(SYNTH, encoding="utf-8") as fh:
            d = json.load(fh)
        code, _, err = self.cli(d)
        self.assertEqual(code, 1)
        self.assertIn("Handelsplatz nicht angegeben", err)

    def test_one_foreign_candle_stops(self):
        d = doc(RANGE)
        d["candles"][3]["s"] = "ETH"
        code, _, err = self.cli(d)
        self.assertEqual(code, 1)
        self.assertIn("ETH", err)
        d = doc(RANGE)
        del d["candles"][3]["s"]
        code, _, err = self.cli(d)
        self.assertIn("1 Kerzen ohne Symbol", err)

    def test_market_spec(self):
        self.assertEqual(bt.parse_market("hyperliquid:SOL-PERP"), ("hyperliquid", "SOL", "perp"))
        for bad in ("SOL", "hyperliquid:SOL", "binance:SOL-PERP", "hyperliquid:SOL-SPOT"):
            with self.assertRaises(bt.BTError, msg=bad):
                bt.parse_market(bad)

    def test_collector_takes_perps_coins_only(self):
        self.assertEqual(col.perp_coin("sol"), "SOL")
        for bad in ("@107", "PURR/USDC", "xyz:SOL", "", "SOL-PERP"):
            with self.assertRaises(col.CollectError, msg=bad):
                col.perp_coin(bad)


class Auto(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "data", "sol.sqlite")
        self.work = os.path.join(self.tmp, "run")
        self.lines = []

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def go(self, rows, at_min, account=1000.0, config=None):
        fake = Fake(rows)
        old = col.now_ms
        col.now_ms = lambda: T0 + at_min * MIN + 30000
        try:
            res = auto.run(self.db, self.work, account, config, post=fake, out=self.lines.append)
        finally:
            col.now_ms = old
        return res, fake

    def test_the_whole_run_and_long_short_apart(self):
        rows = RANGE + [(89.30, 89.55, 89.25, 89.50), (89.50, 89.60, 89.45, 89.55)]   # 00:16 still forming
        res, fake = self.go(rows, 16)
        text = "\n".join(self.lines)
        for step in ("meta", "fetch", "export"):
            self.assertRegex(text, r"\$ python3 hl-backtest/collect.py %s --db \S+ --coin SOL" % step)
        self.assertIn("--market hyperliquid:SOL-PERP --price-rule hyperliquid --account 1000.0 --run A+B", text)
        self.assertTrue(all(c.get("req", {}).get("coin", "SOL") == "SOL" for c in fake.calls))
        self.assertIn("LONG   Trades   0", text)
        self.assertIn("SHORT  Trades   0", text)
        self.assertIn("Status (Signal-Kerze 2026-11-14 00:15Z, simuliert, keine Order): LONG (Setup A)", text)
        self.assertIn("Funding ist nicht eingerechnet", text)
        self.assertEqual(res["data"]["market_checked"], "hyperliquid:SOL-PERP")
        self.assertTrue(os.path.exists(os.path.join(self.work, "report", "report.txt")))

    def test_trades_are_counted_per_side(self):
        win = [(89.30, 89.55, 89.25, 89.50), (89.50, 89.95, 89.45, 89.92)] + [(89.92, 89.95, 89.90, 89.92)] * 13
        rows = RANGE + win + RANGE + [(88.90, 88.95, 88.65, 88.70), (88.70, 88.75, 88.20, 88.25)] + \
            [(88.25, 88.30, 88.20, 88.25)] * 13 + [(88.25, 88.30, 88.20, 88.25)]
        res, _ = self.go(rows, len(rows) - 1)
        rows_ = {(x["setup"], x["side"]): x for x in res["runs"][0]["summary"]}
        self.assertEqual((rows_[("*", "long")]["trades"], rows_[("*", "short")]["trades"]), (1, 1))
        self.assertGreater(rows_[("*", "long")]["gross"], rows_[("*", "long")]["net"])
        self.assertGreater(rows_[("*", "short")]["gross"], 0)
        self.assertEqual(res["runs"][0]["signal"]["status"], "FLAT")

    def test_a_failed_fetch_stops_before_the_backtest(self):
        def down(payload):
            if payload["type"] == "meta":
                return {"universe": [{"name": "SOL", "szDecimals": 2}]}
            raise col.urllib.error.URLError("Tunnel connection failed: 403 Forbidden")
        old = col.PAUSE
        col.PAUSE = 0
        try:
            with self.assertRaisesRegex(auto.AutoError, r"Schritt 2 \(fetch\).*403 Forbidden"):
                auto.run(self.db, self.work, 1000.0, post=down, out=self.lines.append)
        finally:
            col.PAUSE = old
        self.assertFalse(os.path.exists(os.path.join(self.work, "report")))

    def test_limits_are_checked_first(self):
        for bad in ({"position_pct": 3}, {"max_positions": 2}, {"symbol": "BTC"}):
            path = os.path.join(self.tmp, "cfg.json")
            with open(path, "w") as fh:
                json.dump(bad, fh)
            with self.assertRaisesRegex(auto.AutoError, "Limits des SOL-Laufs", msg=str(bad)):
                auto.run(self.db, self.work, 1000.0, path, post=Fake(RANGE), out=self.lines.append)
        with self.assertRaisesRegex(bt.BTError, "leverage must be 1"):
            bt.load_config(None, {"leverage": 2})

    def test_cli_needs_the_account_and_folders_outside_the_checkout(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(auto.main(["--db", self.db, "--work", self.work], post=Fake(RANGE)), 1)
        self.assertIn("--account is required", err.getvalue())
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(auto.main(["--db", self.db, "--work", os.path.join(ROOT, "run"), "--account", "1000"],
                                       post=Fake(RANGE)), 1)
        self.assertIn("inside the git checkout", err.getvalue())

    def test_export_needs_meta(self):
        db = col.connect(os.path.join(self.tmp, "m.sqlite"))
        col.fetch(db, Fake(RANGE), now=T0 + 15 * MIN + 30000, sleep=lambda s: None, log=lambda *a: None)
        with self.assertRaisesRegex(col.CollectError, "no meta for SOL"):
            col.export(db)
        db.close()

    def test_no_order_code(self):
        with open(os.path.join(ROOT, "hl-backtest", "auto_sol.py"), encoding="utf-8") as fh:
            src = fh.read()
        for bad in ("/exchange", "private_key", "signature", "api_key", "subprocess", "urllib"):
            self.assertNotIn(bad, src, bad)
        self.assertIn('COIN = "SOL"', src)
        self.assertIn('MARKET = "hyperliquid:SOL-PERP"', src)


if __name__ == "__main__":
    unittest.main()
