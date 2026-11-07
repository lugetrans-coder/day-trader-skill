"""hl-testnet/trader.py with a simulated Hyperliquid API: LONG, SHORT, partial fill, timeouts, restart,
the limits, dry-run and the testnet-only guards. No network, no SDK needed."""

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

import test_hlauto as ha
from _load import ROOT, load

tn = load("hl-testnet", "trader")
MIN = 60000
T0 = ha.T0
LONG_BAR = (89.30, 89.55, 89.25, 89.50)
SHORT_BAR = (88.90, 88.95, 88.65, 88.70)


class FakeHL(object):
    """Simulated Hyperliquid perps account for SOL: reads, IOC and trigger orders, cancels, order status.
    fail[name] = "before" (the request never arrives) or "after" (it arrives, the answer is lost)."""

    def __init__(self, bar, mid, av=1000.0):
        self.rows = ha.RANGE + [bar, (mid, mid, mid, mid)]          # the last candle is still forming
        self.mid_ = mid
        self.av = av
        self.pos = None
        self.lev = {"type": "cross", "value": 20}
        self.orders = {}
        self.by_cloid = {}
        self.next_oid = 100
        self.calls = []
        self.fail = {}
        self.fill_fraction = 1.0
        self.fill_px = None
        self.exit_fraction = 1.0
        self.read_fail = False

    # reads
    def sz_decimals(self):
        return 2

    def candles(self, start, end):
        return [c for c in ha.rows_to_candles(self.rows) if start <= c["t"] <= end]

    def mid(self):
        return self.mid_

    def user_state(self):
        if self.read_fail:
            raise TimeoutError("read timed out")
        ap = []
        if self.pos:
            ap = [{"type": "oneWay", "position": {"coin": "SOL", "szi": str(self.pos["szi"]),
                                                  "entryPx": str(self.pos["px"]), "leverage": dict(self.lev)}}]
        return {"assetPositions": ap, "marginSummary": {"accountValue": str(self.av)}, "withdrawable": str(self.av)}

    def open_orders(self):
        return [{"coin": "SOL", "oid": oid, "cloid": o["cloid"], "sz": str(o["sz"]), "isTrigger": True,
                 "reduceOnly": True, "triggerPx": str(o["px"]), "orderType": o["tpsl"]} for oid, o in self.orders.items()]

    def order_status(self, cloid):
        if cloid not in self.by_cloid:
            return {"status": "unknownOid"}
        st, oid = self.by_cloid[cloid]
        o = self.orders.get(oid, {})
        return {"status": "order", "order": {"status": st, "order": {"oid": oid, "cloid": cloid,
                                                                     "sz": str(o.get("sz", 0)),
                                                                     "triggerPx": str(o.get("px", 0))}}}

    # writes
    def _call(self, name, effect, **kw):
        self.calls.append((name, kw))
        mode = self.fail.pop(name, None)
        if mode == "before":
            raise TimeoutError("%s timed out" % name)
        res = effect()
        if mode == "after":
            raise TimeoutError("%s timed out after it reached the exchange" % name)
        return res

    def set_leverage(self, leverage, is_cross):
        def eff():
            self.lev = {"type": "cross" if is_cross else "isolated", "value": leverage}
            return {"status": "ok", "response": {"type": "default"}}
        return self._call("set_leverage", eff, leverage=leverage, is_cross=is_cross)

    def _oid(self):
        self.next_oid += 1
        return self.next_oid

    def ioc(self, is_buy, sz, limit_px, cloid, reduce_only):
        def eff():
            oid = self._oid()
            px = self.fill_px or self.mid_
            if reduce_only:
                have = abs(self.pos["szi"]) if self.pos else 0
                if not self.pos or (self.pos["szi"] > 0) == is_buy:
                    self.by_cloid[cloid] = ("rejected", oid)
                    return {"status": "ok", "response": {"data": {"statuses": [{"error": "Reduce only order"}]}}}
                fill = round(min(sz, have) * self.exit_fraction, 2)
                self._reduce(fill)
            else:
                fill = round(sz * self.fill_fraction, 2)
                if fill > 0:
                    self.pos = {"szi": fill if is_buy else -fill, "px": px}
            self.by_cloid[cloid] = ("filled" if fill else "canceled", oid)
            st = {"filled": {"totalSz": str(fill), "avgPx": str(px), "oid": oid}} if fill else \
                {"error": "IOC could not match"}
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [st]}}}
        return self._call("ioc", eff, is_buy=is_buy, sz=sz, limit_px=limit_px, cloid=cloid, reduce_only=reduce_only)

    def trigger(self, is_buy, sz, trigger_px, limit_px, tpsl, cloid):
        def eff():
            oid = self._oid()
            self.orders[oid] = {"cloid": cloid, "sz": sz, "px": trigger_px, "tpsl": tpsl, "is_buy": is_buy}
            self.by_cloid[cloid] = ("open", oid)
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": oid}}]}}}
        return self._call("trigger", eff, is_buy=is_buy, sz=sz, trigger_px=trigger_px, limit_px=limit_px, tpsl=tpsl,
                          cloid=cloid)

    def cancel(self, oid):
        def eff():
            o = self.orders.pop(oid, None)
            if o:
                self.by_cloid[o["cloid"]] = ("canceled", oid)
            return {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}}
        return self._call("cancel", eff, oid=oid)

    # the market
    def _reduce(self, fill):
        left = round(abs(self.pos["szi"]) - fill, 8)
        self.pos = None if left <= 1e-9 else dict(self.pos, szi=left if self.pos["szi"] > 0 else -left)

    def move(self, px):
        """The price moves to px: stops and targets trigger and fill."""
        self.mid_ = px
        for oid, o in list(self.orders.items()):
            long_pos = not o["is_buy"]
            hit = (px <= o["px"] if long_pos else px >= o["px"]) if o["tpsl"] == "sl" else \
                (px >= o["px"] if long_pos else px <= o["px"])
            if hit and self.pos:
                del self.orders[oid]
                self.by_cloid[o["cloid"]] = ("filled", oid)
                self._reduce(min(o["sz"], abs(self.pos["szi"])))

    def count(self, name, **match):
        return sum(1 for n, kw in self.calls if n == name and all(kw.get(k) == v for k, v in match.items()))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = os.path.join(self.tmp, "testnet")
        self.clock = [T0 + 16 * MIN + 2000]                # 2 s after the signal candle closed
        self.lines = []

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def trader(self, hl, live=True):
        t = tn.Trader(hl, hl if live else None, self.dir, live=live, now=lambda: self.clock[0], out=self.lines.append)
        if not live:
            t.writer = tn.DryRun(t.log)
        return t

    def at(self, minutes, seconds=2):
        self.clock[0] = T0 + minutes * MIN + seconds * 1000

    def state(self):
        with open(os.path.join(self.dir, "state.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def journal(self):
        with open(os.path.join(self.dir, "journal.jsonl"), encoding="utf-8") as fh:
            return [json.loads(line) for line in fh]


class Long(Base):
    def test_entry_protection_and_five_minute_exit(self):
        hl = FakeHL(LONG_BAR, 89.52)
        hl.fill_px = 89.55
        t = self.trader(hl)
        self.assertEqual(t.tick(), "open")
        self.assertEqual(hl.lev, {"type": "isolated", "value": 1})          # 1x, set before the entry
        name, kw = [c for c in hl.calls if c[0] == "ioc"][0]
        self.assertEqual((kw["is_buy"], kw["sz"], kw["reduce_only"], kw["limit_px"]), (True, 0.22, False, 89.968))
        self.assertLessEqual(kw["sz"] * kw["limit_px"], 1000 * 0.02)        # at most 2 % even at the limit
        self.assertTrue(kw["cloid"].startswith(tn.CLOID_PREFIX))
        trig = {kw["tpsl"]: kw for n, kw in hl.calls if n == "trigger"}
        self.assertEqual((trig["sl"]["is_buy"], trig["sl"]["sz"], trig["sl"]["trigger_px"]), (False, 0.22, 89.1))
        self.assertEqual((trig["tp"]["is_buy"], trig["tp"]["sz"], trig["tp"]["trigger_px"]), (False, 0.22, 90.0))
        st = self.state()
        self.assertEqual((st["phase"], st["trade"]["filled"], st["trade"]["deadline"]), ("open", 0.22, T0 + 21 * MIN))
        for m in (17, 18, 19, 20):                                           # nothing new while it runs
            self.at(m)
            self.assertEqual(t.tick(), "open")
        self.assertEqual((hl.count("ioc"), hl.count("trigger")), (1, 2))
        self.at(21)                                                          # the close of the fifth candle
        self.assertEqual(t.tick(), "idle")
        exits = [kw for n, kw in hl.calls if n == "ioc" and kw["reduce_only"]]
        self.assertEqual(len(exits), 1)
        self.assertEqual((exits[0]["is_buy"], exits[0]["sz"]), (False, 0.22))
        self.assertIsNone(hl.pos)
        self.assertEqual(hl.orders, {})                                      # stop and target cancelled
        self.assertEqual(self.state()["phase"], "idle")

    def test_stop_hit_cancels_the_target(self):
        hl = FakeHL(LONG_BAR, 89.52)
        t = self.trader(hl)
        t.tick()
        hl.move(89.05)
        self.at(18)
        self.assertEqual(t.tick(), "idle")
        self.assertIsNone(hl.pos)
        self.assertEqual(hl.orders, {})
        self.assertIn("Position geschlossen (Stop, Ziel oder von Hand)", "\n".join(self.lines))


class Short(Base):
    def test_short_mirror(self):
        hl = FakeHL(SHORT_BAR, 88.68)
        hl.fill_px = 88.66
        t = self.trader(hl)
        self.assertEqual(t.tick(), "open")
        kw = [kw for n, kw in hl.calls if n == "ioc"][0]
        self.assertEqual((kw["is_buy"], kw["reduce_only"], kw["limit_px"]), (False, False, 88.236))
        self.assertEqual(hl.pos["szi"], -0.22)
        trig = {kw["tpsl"]: kw for n, kw in hl.calls if n == "trigger"}
        self.assertEqual((trig["sl"]["is_buy"], trig["sl"]["trigger_px"]), (True, 89.1))
        self.assertEqual((trig["tp"]["is_buy"], trig["tp"]["trigger_px"]), (True, 88.22))
        hl.move(88.20)                                                       # the target fills
        self.at(18)
        self.assertEqual(t.tick(), "idle")
        self.assertEqual((hl.pos, hl.orders), (None, {}))


class Partial(Base):
    def test_protection_covers_only_the_filled_size(self):
        hl = FakeHL(LONG_BAR, 89.52)
        hl.fill_fraction = 0.5
        t = self.trader(hl)
        t.tick()
        self.assertEqual(hl.pos["szi"], 0.11)
        self.assertEqual({o["sz"] for o in hl.orders.values()}, {0.11})
        self.assertIn("TEILFÜLLUNG", "\n".join(self.lines))

    def test_protection_follows_a_smaller_position(self):
        hl = FakeHL(LONG_BAR, 89.52)
        t = self.trader(hl)
        t.tick()
        hl.pos["szi"] = 0.15                                                 # e.g. part closed by hand
        self.at(17)
        t.tick()
        self.assertEqual(sorted((o["tpsl"], o["sz"]) for o in hl.orders.values()), [("sl", 0.15), ("tp", 0.15)])

    def test_partial_exit_is_retried(self):
        hl = FakeHL(LONG_BAR, 89.52)
        t = self.trader(hl)
        t.tick()
        hl.exit_fraction = 0.5
        self.at(21)
        self.assertEqual(t.tick(), "exit_pending")
        self.assertAlmostEqual(hl.pos["szi"], 0.11)
        hl.exit_fraction = 1.0
        self.at(22)
        self.assertEqual(t.tick(), "idle")
        self.assertIsNone(hl.pos)
        self.assertEqual(hl.count("ioc", reduce_only=True), 2)


class Timeouts(Base):
    def test_entry_timeout_after_it_arrived(self):
        hl = FakeHL(LONG_BAR, 89.52)
        hl.fail["ioc"] = "after"
        t = self.trader(hl)
        self.assertEqual(t.tick(), "uncertain")
        self.assertEqual(self.state()["phase"], "entry_pending")
        self.assertEqual(hl.count("trigger"), 0)
        self.at(17)
        self.assertEqual(t.tick(), "open")                                   # found by its cloid
        self.assertEqual(hl.count("ioc"), 1)                                 # no second entry
        self.assertEqual(sorted(o["tpsl"] for o in hl.orders.values()), ["sl", "tp"])

    def test_entry_timeout_before_it_arrived(self):
        hl = FakeHL(LONG_BAR, 89.52)
        hl.fail["ioc"] = "before"
        t = self.trader(hl)
        self.assertEqual(t.tick(), "uncertain")
        self.at(16, 10)                                                      # 8 s later: still waiting
        self.assertEqual(t.tick(), "entry_pending")
        self.at(17)
        self.assertEqual(t.tick(), "flat")                                   # unknown and flat: not executed
        self.assertEqual((self.state()["phase"], self.state()["trade"]), ("idle", None))
        self.assertIn("nicht ausgeführt", "\n".join(self.lines))
        self.at(17, 5)
        t.tick()
        self.assertEqual(hl.count("ioc"), 1)                                 # the signal is not repeated
        self.assertIsNone(hl.pos)

    def test_stop_order_timeout_makes_no_duplicate(self):
        hl = FakeHL(LONG_BAR, 89.52)
        hl.fail["trigger"] = "after"
        t = self.trader(hl)
        self.assertEqual(t.tick(), "uncertain")
        self.at(17)
        t.tick()
        self.at(18)
        t.tick()
        kinds = sorted(o["tpsl"] for o in hl.orders.values())
        self.assertEqual(kinds, ["sl", "tp"])                                # exactly one each

    def test_exit_timeout(self):
        hl = FakeHL(LONG_BAR, 89.52)
        t = self.trader(hl)
        t.tick()
        hl.fail["ioc"] = "after"                                             # the close arrives, the answer is lost
        self.at(21)
        self.assertEqual(t.tick(), "uncertain")
        self.at(22)
        self.assertEqual(t.tick(), "idle")
        self.assertEqual((hl.pos, hl.orders), (None, {}))
        self.assertEqual(hl.count("ioc", reduce_only=True), 1)

    def test_no_order_when_the_exchange_cannot_be_read(self):
        hl = FakeHL(LONG_BAR, 89.52)
        hl.read_fail = True
        t = self.trader(hl)
        self.assertEqual(t.tick(), "read_error")
        self.assertEqual(hl.calls, [])


class Restart(Base):
    def test_restart_while_open(self):
        hl = FakeHL(LONG_BAR, 89.52)
        self.trader(hl).tick()
        self.at(18)
        t2 = self.trader(hl)                                                 # a new process, same state folder
        self.assertEqual(t2.tick(), "open")
        self.assertEqual((hl.count("ioc"), hl.count("trigger")), (1, 2))
        self.at(21)
        self.assertEqual(self.trader(hl).tick(), "idle")
        self.assertIsNone(hl.pos)

    def test_restart_after_a_lost_entry_answer(self):
        hl = FakeHL(LONG_BAR, 89.52)
        hl.fail["ioc"] = "after"
        self.trader(hl).tick()
        self.at(17)
        self.assertEqual(self.trader(hl).tick(), "open")
        self.assertEqual(hl.count("ioc"), 1)

    def test_lost_state_blocks_instead_of_trading(self):
        hl = FakeHL(LONG_BAR, 89.52)
        self.trader(hl).tick()
        os.remove(os.path.join(self.dir, "state.json"))
        self.at(17)
        t = self.trader(hl)
        self.assertEqual(t.tick(), "idle")
        self.assertIn("ohne Bot-Zustand", "\n".join(self.lines))
        self.assertEqual(hl.count("ioc"), 1)


class Limits(Base):
    def test_min_notional_skips(self):
        hl = FakeHL(LONG_BAR, 89.52, av=250.0)
        t = self.trader(hl)
        self.assertEqual(t.tick(), "skipped")
        self.assertEqual(hl.calls, [])
        self.assertIn("skipped_min_notional", "\n".join(self.lines))

    def test_leverage_not_1x_after_the_fill_closes_and_halts(self):
        hl = FakeHL(LONG_BAR, 89.52)
        orig = hl.set_leverage
        hl.set_leverage = lambda lev, cross: orig(3, cross)                  # the exchange keeps 3x
        t = self.trader(hl)
        t.tick()
        self.assertIsNone(hl.pos)
        self.assertIn("Hebel der Position ist 3", self.state()["halt"])
        self.at(30, 2)
        hl.rows = ha.RANGE * 2 + [LONG_BAR, (89.5, 89.5, 89.5, 89.5)]
        self.clock[0] = T0 + 31 * MIN + 2000
        t.tick()
        self.assertEqual(hl.count("ioc", reduce_only=False), 1)              # halted: no new entry

    def test_foreign_position_or_order_blocks(self):
        hl = FakeHL(LONG_BAR, 89.52)
        hl.pos = {"szi": 1.0, "px": 80.0}
        self.trader(hl).tick()
        self.assertEqual(hl.calls, [])
        hl.pos = None
        hl.orders[5] = {"cloid": None, "sz": 1.0, "px": 70.0, "tpsl": "sl", "is_buy": False}
        self.trader(hl).tick()
        self.assertEqual(hl.calls, [])
        self.assertIn(5, hl.orders)                                          # never touched

    def test_stop_file_and_flat(self):
        hl = FakeHL(LONG_BAR, 89.52)
        os.makedirs(self.dir)
        open(os.path.join(self.dir, "STOP"), "w").close()
        self.assertEqual(self.trader(hl).tick(), "stopped")
        os.remove(os.path.join(self.dir, "STOP"))
        hl2 = FakeHL((89.10, 89.20, 89.00, 89.15), 89.15)                    # no setup
        self.assertEqual(self.trader(hl2).tick(), "flat")
        self.assertEqual(hl2.calls, [])
        self.at(16, 40)                                                      # too late after the close
        self.assertEqual(self.trader(FakeHL(LONG_BAR, 89.52)).tick(), "flat")


class DryRun(Base):
    def test_dry_run_only_logs(self):
        hl = FakeHL(LONG_BAR, 89.52)
        t = self.trader(hl, live=False)
        self.assertEqual(t.tick(), "dry-run")
        self.assertEqual(hl.calls, [])
        self.assertIsNone(hl.pos)
        planned = [e for e in self.journal() if e["event"] == "dry_run" and "order" in e]
        self.assertEqual([p["order"] for p in planned], ["set_leverage", "ioc", "trigger", "trigger"])
        self.assertEqual({p.get("reduce_only") for p in planned if p["order"] == "trigger"}, {True})
        self.at(16, 20)
        t.tick()                                                             # the same signal is logged once
        self.assertEqual(len([e for e in self.journal() if e.get("order") == "ioc"]), 1)


class Guards(unittest.TestCase):
    def test_testnet_url_only_and_no_mainnet_switch(self):
        with open(os.path.join(ROOT, "hl-testnet", "trader.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertEqual(set(__import__("re").findall(r"https://([\w.\-]+)", src)), {"api.hyperliquid-testnet.xyz"})
        self.assertNotIn("MAINNET", src)
        self.assertNotIn("--mainnet", src)
        for bad in ("withdraw_from_bridge", ".usd_transfer(", "approve_agent", "spot_transfer", "send_asset"):
            self.assertNotIn(bad, src)

    def test_cli_dry_run_needs_no_key_and_live_needs_the_switch(self):
        tmp = tempfile.mkdtemp()
        made = []

        class V(FakeHL):
            def __init__(self, address, wallet=None):
                FakeHL.__init__(self, (89.10, 89.20, 89.00, 89.15), 89.15)
                made.append(wallet)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = tn.main(["run", "--address", "0x" + "1" * 40, "--state-dir", tmp, "--once"], venue_factory=V)
            self.assertEqual(code, 0)
            self.assertEqual(made, [None])                                   # no wallet in the dry-run
            self.assertIn("DRY-RUN", out.getvalue())
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = tn.main(["run", "--address", "0x" + "1" * 40, "--state-dir", tmp, "--once",
                                "--key-file", os.path.join(tmp, "k")], venue_factory=V)
            self.assertEqual(code, 1)
            self.assertIn("only with --send-testnet-orders", err.getvalue())
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = tn.main(["run", "--address", "nope", "--state-dir", tmp], venue_factory=V)
            self.assertIn("--address must be", err.getvalue())
        finally:
            shutil.rmtree(tmp)

    def test_key_handling(self):
        tmp = tempfile.mkdtemp()
        try:
            with self.assertRaisesRegex(tn.TraderError, "inside the git checkout"):
                tn.read_key(os.path.join(ROOT, "agent.key"))
            path = os.path.join(tmp, "agent.key")
            with open(path, "w") as fh:
                fh.write("not a key\n")
            with self.assertRaisesRegex(tn.TraderError, "not a 32-byte hex"):
                tn.read_key(path)
            with open(path, "w") as fh:
                fh.write("ab" * 32 + "\n")
            self.assertEqual(tn.read_key(path), "0x" + "ab" * 32)
            old = os.environ.pop("HL_TESTNET_AGENT_KEY", None)
            os.environ["HL_TESTNET_AGENT_KEY"] = "0x" + "cd" * 32
            try:
                self.assertEqual(tn.read_key(None), "0x" + "cd" * 32)
            finally:
                del os.environ["HL_TESTNET_AGENT_KEY"]
                if old:
                    os.environ["HL_TESTNET_AGENT_KEY"] = old
        finally:
            shutil.rmtree(tmp)

    def test_windows_scripts(self):
        win = os.path.join(ROOT, "hl-testnet", "windows")
        src = {}
        for f in os.listdir(win):
            with open(os.path.join(win, f), "rb") as fh:
                raw = fh.read()
            self.assertTrue(all(c < 128 for c in raw), f)
            src[f] = raw.decode()
        self.assertNotIn("--send-testnet-orders", src["run_dryrun.ps1"])
        self.assertNotIn("KeyFile", src["run_dryrun.ps1"])
        self.assertIn("if (-not $SendTestnetOrders) {", src["run_testnet.ps1"])
        self.assertIn("'--send-testnet-orders'", src["run_testnet.ps1"])
        self.assertIn("Join-Path $TnHome 'venv-testnet'", src["hl-testnet-common.ps1"])

    def test_state_folder_outside_the_checkout(self):
        with self.assertRaisesRegex(tn.TraderError, "inside the git checkout"):
            tn.Trader(None, None, os.path.join(ROOT, "state"), live=False)

    def test_one_trader_per_state_folder(self):
        tmp = tempfile.mkdtemp()
        try:
            lock = tn.Lock(tmp)
            lock.acquire()
            with self.assertRaisesRegex(tn.TraderError, "another trader is running"):
                tn.Lock(tmp).acquire()
            lock.release()
            tn.Lock(tmp).acquire()
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
