"""hl-backtest tests: the offline SOL-perps range backtest. Fixture in tests/fixtures/hl-backtest/:
SOL-1m-synthetic.json (hand-made candles, built by make_sol_synthetic.py, one case per cycle)."""

import contextlib
import copy
import io
import json
import os
import re
import shutil
import tempfile
import unittest

from _load import FIX, ROOT, load

bt = load("hl-backtest", "backtest")
FIXDIR = os.path.join(FIX, "hl-backtest")
DATA = os.path.join(FIXDIR, "SOL-1m-synthetic.json")
CONFIG = os.path.join(ROOT, "hl-backtest", "config.example.json")
START = 1793577600000      # 2026-11-02T00:00Z
MIN = 60000


def cfg(**kw):
    return bt.load_config(None, kw)


def fixture(**kw):
    return bt.read_data(DATA, symbol="SOL", **kw)


def result(runs=("A", "B", "A+B"), **kw):
    return bt.backtest(fixture(), cfg(**kw), runs=runs)


def by_run(res, name):
    return next(r for r in res["runs"] if r["name"] == name)


def at(ms):
    return bt.stamp(ms)


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = bt.main(argv)
    return code, out.getvalue(), err.getvalue()


def candles(rows, t0=START):
    """[(o, h, l, c) or None, ...] one a minute from t0 -> parsed data."""
    raw = []
    for i, r in enumerate(rows):
        if r is not None:
            o, h, l, c = r
            raw.append({"t": t0 + i * MIN, "o": o, "h": h, "l": l, "c": c, "s": "SOL", "i": "1m"})
    return bt.parse_candles({"symbol": "SOL", "interval": "1m", "as_of": t0 + len(rows) * MIN, "candles": raw})


RANGE = [(100.0, 100.2, 99.8, 100.0)] * 3 + [(100.0, 100.5, 99.8, 100.0)] + [(100.0, 100.2, 99.8, 100.0)] * 5 + \
        [(100.0, 100.2, 99.5, 100.0)] + [(100.0, 100.2, 99.8, 100.0)] * 5


def one_cycle(window, **kw):
    data = candles(RANGE + window + [(100.0, 100.05, 99.95, 100.0)] * (15 - len(window)))
    return bt.backtest(data, cfg(**kw), runs=("A", "B"))


class Fixture(unittest.TestCase):
    def test_the_fixture_is_what_the_generator_builds(self):
        gen = load(os.path.join("tests", "fixtures", "hl-backtest"), "make_sol_synthetic")
        with open(DATA, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), gen.text())

    def test_only_closed_candles_and_gaps_in_time_order(self):
        d = fixture()
        self.assertEqual(d["open_dropped"], [START + (300 + 18) * MIN])          # 05:18 still open at 05:18:30
        self.assertEqual(d["count"], 316)
        self.assertEqual([(at(g["from"]), g["minutes"]) for g in d["gaps"]],
                         [("2026-11-02 02:47Z", 1), ("2026-11-02 03:05Z", 1)])

    def test_cycles(self):
        st = [(c["start"][11:16], c["status"]) for c in result()["cycles"]]
        self.assertEqual(st, [("00:00", "ok"), ("00:30", "ok"), ("01:00", "ok"), ("01:30", "ok"), ("02:00", "ok"),
                              ("02:30", "window_gap"), ("03:00", "skipped_range_gap"),
                              ("03:30", "skipped_width_min"), ("04:00", "ok"), ("04:30", "ok"),
                              ("05:00", "data_end")])

    def test_setup_a_trades(self):
        tr = by_run(result(), "A")["trades"]
        got = [(t["side"], t["entry_time"][11:16], t["exit_kind"], t["exit_candle"][11:16], t["ambiguous"])
               for t in tr]
        self.assertEqual(got, [
            ("long", "00:16", "target", "00:17", False),
            ("short", "00:46", "gap_stop", "00:47", False),
            ("long", "01:16", "stop", "01:16", True),
            ("long", "01:46", "stop", "01:48", False),
            ("long", "02:16", "timeout", "02:20", False),
            ("long", "02:46", "unknown_data_gap", "02:47", False),
            ("short", "04:16", "target", "04:17", False),
            ("short", "04:46", "stop", "04:48", False),
            ("long", "05:16", "unknown_data_end", "05:18", False),
        ])

    def test_setup_b_trades_and_skips(self):
        r = by_run(result(), "B")
        self.assertEqual([(t["side"], t["entry_time"][11:16], t["stop"], t["target"], t["exit_kind"])
                          for t in r["trades"]],
                         [("short", "01:47", 100.59, 100.0, "target"), ("long", "04:47", 99.41, 100.0, "target")])
        self.assertEqual([(s["side"], s["reason"]) for s in r["skipped"]], [("long", "skipped_rr")])

    def test_one_open_position_in_the_combined_run(self):
        r = by_run(result(), "A+B")
        self.assertEqual(len(r["trades"]), 9)
        self.assertEqual([(s["setup"], s["side"], s["reason"]) for s in r["skipped"]],
                         [("B", "long", "skipped_rr"), ("B", "short", "skipped_position_open"),
                          ("B", "long", "skipped_position_open")])


class Numbers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = result()
        cls.a = by_run(cls.res, "A")["trades"]

    def test_entry_at_the_next_open_and_the_size(self):
        t = self.a[0]                       # signal candle 00:15 closes at 00:16; entry = 00:16 open
        self.assertEqual((t["signal"], t["entry_time"], t["entry_ref"]), ("2026-11-02 00:15Z", "2026-11-02 00:16Z", 100.6))
        self.assertAlmostEqual(t["entry_fill"], 100.60 * 1.0002, places=6)      # half spread 1 bp + slippage 1 bp
        self.assertEqual(t["qty"], 1.98)                                        # floor(200 / 100.62012, 2 decimals)
        self.assertAlmostEqual(t["position_value"], 1.98 * 100.62012, places=6)
        self.assertLessEqual(t["position_value"], 200.0)
        self.assertEqual((t["stop"], t["target"]), (100.0, 101.2))

    def test_costs_once_each_and_net_is_gross_minus_costs(self):
        t = self.a[0]
        self.assertAlmostEqual(t["gross"], 1.98 * 0.60, places=9)
        self.assertAlmostEqual(t["spread"], 1.98 * 1e-4 * (100.60 + 101.20), places=9)
        self.assertAlmostEqual(t["slippage"], 1.98 * 1e-4 * (100.60 + 101.20), places=9)
        self.assertAlmostEqual(t["fees"], 1.98 * 4.5e-4 * (100.60 * 1.0002 + 101.20 * 0.9998), places=9)
        for r in self.res["runs"]:
            for t in r["trades"]:
                if t["outcome_known"]:
                    self.assertAlmostEqual(t["net"], t["gross"] - t["fees"] - t["spread"] - t["slippage"], places=9)
                    d = 1 if t["side"] == "long" else -1
                    self.assertAlmostEqual(t["net"], t["qty"] * d * (t["exit_fill"] - t["entry_fill"]) - t["fees"],
                                           places=5)

    def test_zero_costs_net_equals_gross(self):
        res = result(taker_fee_bps=0, half_spread_bps=0, slippage_entry_bps=0, slippage_exit_bps=0,
                     slippage_stop_bps=0, width_min_cost_mult=0)
        for t in by_run(res, "A")["trades"]:
            if t["outcome_known"]:
                self.assertAlmostEqual(t["net"], t["gross"], places=12)

    def test_gap_stop_short_fills_at_the_open(self):
        t = self.a[1]
        self.assertEqual((t["exit_kind"], t["exit_ref"], t["stop"]), ("gap_stop", 100.2, 100.0))
        self.assertAlmostEqual(t["gross"], -2.01 * 0.80, places=9)
        self.assertAlmostEqual(t["slippage"], 2.01 * (1e-4 * 99.40 + 3e-4 * 100.20), places=9)   # stop slippage 3 bp
        self.assertIn("Mark-Preis", t["stop_trigger_note"])

    def test_ambiguous_counts_the_stop_and_shows_the_other_case(self):
        t = self.a[2]
        self.assertTrue(t["ambiguous"])
        self.assertEqual((t["exit_kind"], t["exit_ref"]), ("stop", 100.0))
        self.assertGreater(t["alt_net_target_first"], 0)
        s = by_run(self.res, "A")["summary"][-1]
        self.assertEqual(s["ambiguous"], 1)
        self.assertAlmostEqual(s["net_if_ambiguous_target_first"], s["net"] + t["alt_net_target_first"] - t["net"],
                               places=6)

    def test_five_minute_exit(self):
        t = self.a[4]
        self.assertEqual((t["exit_kind"], t["exit_candle"], t["candles_held"], t["exit_ref"]),
                         ("timeout", "2026-11-02 02:20Z", 5, 100.7))   # entry 02:16 open, exit 02:20 close = 02:21

    def test_unknown_outcomes_stay_out_of_the_totals(self):
        unk = [t for t in self.a if not t["outcome_known"]]
        self.assertEqual([t["exit_kind"] for t in unk], ["unknown_data_gap", "unknown_data_end"])
        for t in unk:
            self.assertIsNone(t["net"])
            self.assertIsNotNone(t["est_loss_at_stop"])
        s = by_run(self.res, "A")["summary"][-1]
        self.assertEqual((s["trades"], s["known"], s["unknown"]), (9, 7, 2))
        self.assertAlmostEqual(s["net"], sum(t["net"] for t in self.a if t["outcome_known"]), places=6)
        self.assertEqual(unk[0]["blocked_until"], "2026-11-02 02:51Z")

    def test_loss_at_the_stop_is_its_own_number(self):
        t = self.a[0]
        exit_fill = 100.0 * (1 - 4e-4)                  # half spread 1 bp + stop slippage 3 bp
        loss = -(1.98 * (exit_fill - 100.62012) - 1.98 * 4.5e-4 * (100.62012 + exit_fill))
        self.assertAlmostEqual(t["est_loss_at_stop"], loss, places=6)
        self.assertAlmostEqual(t["est_loss_at_stop_pct_of_account"], 100 * loss / 10000, places=6)
        self.assertLess(t["est_loss_at_stop"], t["position_value"] / 50)

    def test_long_and_short_reported_apart(self):
        rows = {(r["setup"], r["side"]): r for r in by_run(self.res, "A+B")["summary"]}
        self.assertEqual(set(rows), {("A", "long"), ("A", "short"), ("B", "long"), ("B", "short"), ("*", "*")})
        self.assertEqual((rows[("A", "long")]["trades"], rows[("A", "short")]["trades"]), (6, 3))
        for k in ("gross", "fees", "spread", "slippage", "net"):
            self.assertAlmostEqual(rows[("*", "*")][k], rows[("A", "long")][k] + rows[("A", "short")][k], places=5)


class Limits(unittest.TestCase):
    def test_250_account_is_skipped_min_notional(self):
        res = result(runs=("A", "B"), account_value_usd=250)
        for r in res["runs"]:
            self.assertEqual(r["trades"], [])
        reasons = [s["reason"] for s in by_run(res, "A")["skipped"]]
        self.assertEqual(reasons, ["skipped_min_notional"] * 9)
        first = by_run(res, "A")["skipped"][0]
        self.assertIn("Menge 0.04", first["detail"])         # 5 $ / 100.62 = 0.0497 -> 0.04 -> 4.02 $ < 10 $

    def test_leverage_is_1x_only(self):
        with self.assertRaises(bt.BTError):
            cfg(leverage=2)

    def test_exposure_cap(self):
        res = result(runs=("A",), position_pct=2, max_positions=4, max_exposure_pct=1)
        self.assertEqual({s["reason"] for s in by_run(res, "A")["skipped"]}, {"skipped_exposure"})

    def test_bad_config(self):
        for kw in ({"cycle_minutes": 7}, {"range_minutes": 20}, {"cycle_anchor_utc": "25:00"},
                   {"position_pct": 0}, {"hold_candles": 0}):
            with self.assertRaises(bt.BTError, msg=str(kw)):
                cfg(**kw)


class Mirrors(unittest.TestCase):
    def test_long_gap_through_the_stop_mirrors_the_short(self):
        r = one_cycle([(100.40, 100.65, 100.35, 100.60), (100.60, 100.70, 100.30, 100.40),
                       (99.80, 99.90, 99.70, 99.85)])
        t = by_run(r, "A")["trades"][0]
        self.assertEqual((t["side"], t["exit_kind"], t["exit_ref"]), ("long", "gap_stop", 99.8))

    def test_short_ambiguous_mirrors_the_long(self):
        r = one_cycle([(99.90, 99.95, 99.35, 99.40), (99.40, 100.10, 98.70, 99.20)])
        t = by_run(r, "A")["trades"][0]
        self.assertEqual((t["side"], t["exit_kind"], t["ambiguous"], t["exit_ref"]), ("short", "stop", True, 100.0))

    def test_gap_through_the_target_fills_at_the_open(self):
        r = one_cycle([(100.40, 100.65, 100.35, 100.60), (100.60, 100.80, 100.50, 100.70),
                       (101.40, 101.50, 101.30, 101.45)])
        t = by_run(r, "A")["trades"][0]
        self.assertEqual((t["exit_kind"], t["exit_ref"]), ("gap_target", 101.4))

    def test_a_wick_or_a_close_at_the_high_is_no_breakout(self):
        r = one_cycle([(100.40, 100.90, 100.35, 100.50), (100.50, 100.95, 100.10, 100.20)])
        self.assertEqual(r["signals"], 0)

    def test_setup_expires_with_the_window(self):
        # the first close above RH comes from the candle opening 00:30, the next cycle's range: no signal
        data = candles(RANGE + [(100.0, 100.05, 99.95, 100.0)] * 15 + [(100.40, 100.65, 100.35, 100.60)])
        res = bt.backtest(data, cfg(), runs=("A",))
        self.assertEqual(res["signals"], 0)

    def test_failed_breakout_attempt_expires_after_three_candles(self):
        up = (100.60, 100.70, 100.55, 100.60)
        r = one_cycle([(100.40, 100.65, 100.35, 100.60), up, up, up, (100.60, 100.62, 100.40, 100.45)])
        self.assertEqual(by_run(r, "B")["trades"], [])     # back inside on b+4: too late
        r = one_cycle([(100.40, 100.65, 100.35, 100.60), up, up, (100.60, 100.62, 100.40, 100.45),
                       (100.45, 100.50, 99.95, 100.00)])
        self.assertEqual([(t["side"], t["exit_kind"]) for t in by_run(r, "B")["trades"]], [("short", "target")])

    def test_no_lookahead(self):
        d = fixture()
        cut = START + (60 + 16) * MIN               # the ambiguous candle 01:16 and everything after it
        changed = copy.deepcopy(d)
        for t in list(changed["by_t"]):
            if t >= cut:
                b = changed["by_t"][t]
                changed["by_t"][t] = dict(b, h=b["h"] + 5, l=b["l"] - 5, c=b["c"] + 3)
        before = [s for s in bt.scan(d, cfg())[1] if s["signal_t"] < cut]
        after = [s for s in bt.scan(changed, cfg())[1] if s["signal_t"] < cut]
        self.assertEqual(before, after)


class Data(unittest.TestCase):
    def test_bad_candles_fail_plainly(self):
        good = {"t": START, "o": "1", "h": "2", "l": "0.5", "c": "1.5"}
        for bad, msg in ((dict(good, t=START + 1), "grid"), (dict(good, h="1.2"), "high/low"),
                         (dict(good, i="5m"), "1m"), (dict(good, o="x"), "not a number")):
            with self.assertRaisesRegex(bt.BTError, msg):
                bt.parse_candles([good, dict(bad, t=bad["t"] + MIN)])
        with self.assertRaisesRegex(bt.BTError, "two different"):
            bt.parse_candles([good, dict(good, c="1.4")])
        with self.assertRaisesRegex(bt.BTError, "holds"):
            bt.parse_candles([dict(good, s="BTC")], symbol="SOL")

    def test_without_as_of_it_says_so(self):
        d = bt.parse_candles([{"t": START, "o": 1, "h": 1, "l": 1, "c": 1}])
        self.assertIn("as_of", d["notes"][0])


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_report_files_and_reproducible(self):
        argv = ["run", "--data", DATA, "--config", CONFIG, "--run", "A", "--run", "B", "--run", "A+B"]
        code, text, err = run_cli(argv + ["--out", self.tmp])
        self.assertEqual((code, err), (0, ""))
        for f in ("report.txt", "report.json", "trades.csv", "skipped.csv"):
            self.assertTrue(os.path.exists(os.path.join(self.tmp, f)), f)
        for word in ("alle Ausführungen simuliert", "MEHRDEUTIG", "Mark-Preis", "UNBEKANNT (Datenlücke)",
                     "UNBEKANNT (Datenende)", "5-Min-Exit", "Gap-Stop", "skipped_position_open", "skipped_rr",
                     "geschätzter Verlust am Stop inkl. Kosten", "brutto", "netto"):
            self.assertIn(word, text, word)
        code2, text2, _ = run_cli(argv)
        self.assertEqual(text2, text.replace("Geschrieben: %s (report.txt, report.json, trades.csv, skipped.csv)\n"
                                             % self.tmp, ""))
        code, j1, _ = run_cli(argv + ["--json"])
        code, j2, _ = run_cli(argv + ["--json"])
        self.assertEqual(j1, j2)
        self.assertTrue(json.loads(j1)["simulated"])

    def test_account_flag_and_errors(self):
        code, text, _ = run_cli(["run", "--data", DATA, "--account", "250", "--run", "A"])
        self.assertEqual(code, 0)
        self.assertIn("skipped_min_notional 9", text)
        code, _, err = run_cli(["run", "--data", os.path.join(self.tmp, "missing.json")])
        self.assertEqual(code, 1)
        self.assertIn("cannot read", err)
        code, _, err = run_cli(["run", "--data", DATA, "--run", "C"])
        self.assertEqual(code, 1)
        self.assertIn("--run is A, B or A+B", err)

    def test_example_config_holds_every_default(self):
        with open(CONFIG, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual({k: v for k, v in doc.items() if not k.startswith("_")}, bt.DEFAULTS)


class Safety(unittest.TestCase):
    def test_offline_only(self):
        for path in (os.path.join(ROOT, "hl-backtest", "backtest.py"),
                     os.path.join(FIXDIR, "make_sol_synthetic.py")):
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            for bad in ("urllib", "http.client", "socket", "requests", "subprocess", "api_key", "secret"):
                self.assertNotIn(bad, src, "%s: %s" % (path, bad))
            self.assertNotRegex(src, r"(?<![\w.])(list|dict|tuple)\[", path)   # Python 3.8


if __name__ == "__main__":
    unittest.main()
