#!/usr/bin/env python3
"""Separate SOL-perps trading process for the Hyperliquid TESTNET, with the official Python SDK.

TESTNET ONLY. The base URL is fixed to https://api.hyperliquid-testnet.xyz; there is no switch for
mainnet. Market data and orders both come from the testnet. Default is DRY-RUN: planned orders
are only logged. Orders are sent only with --send-testnet-orders and an API-wallet key.

    python trader.py status --address 0xMAIN                         # account, position, orders, state
    python trader.py run --address 0xMAIN                            # dry-run, every minute
    python trader.py run --address 0xMAIN --send-testnet-orders --key-file %LOCALAPPDATA%\\...\\agent.key
    python trader.py reset-halt                                      # after you checked a halt by hand

Rules (the backtest's, on closed 1m candles of the testnet): setup A range breakout, setup B failed
breakout back into the range, LONG and SHORT, the same cycles, stops and targets (hl-backtest/backtest.py).
A signal counts only on the newest closed candle and only within max_entry_delay_s (30 s) after it
closed; otherwise the bot stays FLAT.

Every minute (2 s after the candle closes), in this order:
1. Read the exchange: account value, free margin, the SOL position, the open SOL orders. If that fails,
   nothing is sent in this pass.
2. Reconcile with the saved state BEFORE any new order, so a timeout or a restart never causes a
   second order: an entry whose answer was lost is looked up by its cloid; an own order is known by
   its oid or its cloid prefix; the position on the exchange is the truth for the size.
3. Manage an open trade: a reduce-only stop and target for exactly the filled size (resized when the
   size changes, e.g. a partial fill); at the deadline (the close of the fifth candle after the entry
   candle opened) a reduce-only IOC close, then the protective orders are cancelled.
4. Only when flat, with no SOL order open and no halt: check the signal, the account and the limits
   again, set 1x leverage (isolated), and send one IOC entry.

Limits, not configurable upwards: 1x leverage, position value at most 2 % of the account value at the
entry's limit price, at most one open SOL position, no entry under the 10 $ minimum order value.
A position or SOL order the bot did not make blocks new entries and is never touched. A file STOP in
the state folder blocks new entries (an open trade is still managed).
"""

import argparse
import datetime
import getpass
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TESTNET_URL = "https://api.hyperliquid-testnet.xyz"
COIN = "SOL"
MIN = 60000
POSITION_PCT = 2.0          # of the account value, hard
LEVERAGE = 1                # hard
MAX_POSITIONS = 1           # hard: one SOL position
MIN_NOTIONAL = 10.0
CLOID_PREFIX = "0xd7a1"     # every order this bot sends carries it
SETTINGS = {"entry_slippage": 0.005, "protect_slippage": 0.05, "max_entry_delay_s": 30, "settle_s": 2,
            "unknown_grace_s": 20, "exit_retries": 5, "history_minutes": 45}
EPS = 1e-9


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


backtest = _load(os.path.join(REPO, "hl-backtest", "backtest.py"), "hl_backtest_for_testnet")


class TraderError(Exception):
    """One plain line: why the trader cannot run."""


class Uncertain(Exception):
    """A write whose outcome is unknown (timeout, connection error). The next pass checks the exchange."""


# ---------------------------------------------------------------- small helpers

def stamp(ms):
    return backtest.stamp(ms)


def iso(ms):
    d = datetime.datetime(1970, 1, 1) + datetime.timedelta(milliseconds=ms)
    return d.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def now_ms():
    return int(time.time() * 1000)


def inside_checkout(path):
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
        raise TraderError("%s %s is inside the git checkout %s: keep it outside" % (what, path, repo))
    return os.path.abspath(os.path.expanduser(path))


def default_state_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "day-trader-skill", "testnet")


def make_cloid(*parts):
    h = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return CLOID_PREFIX + h[:32 - (len(CLOID_PREFIX) - 2)]


def size_round(x, sz_decimals):
    return round(backtest.floor_qty(x, sz_decimals), sz_decimals)


def check_address(a):
    if not re.match(r"^0x[0-9a-fA-F]{40}$", str(a or "")):
        raise TraderError("--address must be the 0x... address of the main testnet account (42 characters)")
    return a.lower()


# ---------------------------------------------------------------- the exchange: SDK adapter (testnet only)

class SdkVenue(object):
    """Reads and (with a wallet) writes through the official SDK, always against the testnet."""

    def __init__(self, address, wallet=None, timeout=10.0):
        try:
            from hyperliquid.info import Info
            from hyperliquid.utils import constants
            from hyperliquid.utils.types import Cloid
        except ImportError:
            raise TraderError("the official SDK is missing: py -3 -m pip install -r hl-testnet\\requirements.txt")
        if constants.TESTNET_API_URL != TESTNET_URL:
            raise TraderError("the SDK's testnet URL is %s, expected %s" % (constants.TESTNET_API_URL, TESTNET_URL))
        self.Cloid = Cloid
        self.address = address
        self.info = Info(TESTNET_URL, skip_ws=True, timeout=timeout)
        self.ex = None
        if wallet is not None:
            from hyperliquid.exchange import Exchange
            self.ex = Exchange(wallet, TESTNET_URL, account_address=address, timeout=timeout)

    def _guard(self):
        if self.ex is None:
            raise TraderError("no wallet: this venue is read-only")
        if self.ex.base_url != TESTNET_URL or self.info.base_url != TESTNET_URL:
            raise TraderError("refusing to send: base URL is not the testnet")

    # reads
    def sz_decimals(self):
        for u in self.info.meta()["universe"]:
            if u["name"] == COIN:
                return int(u["szDecimals"])
        raise TraderError("SOL is not in the testnet perps universe")

    def candles(self, start, end):
        return self.info.candles_snapshot(COIN, "1m", start, end)

    def mid(self):
        return float(self.info.all_mids()[COIN])

    def user_state(self):
        return self.info.user_state(self.address)

    def open_orders(self):
        return [o for o in self.info.frontend_open_orders(self.address) if o.get("coin") == COIN]

    def order_status(self, cloid):
        return self.info.query_order_by_cloid(self.address, self.Cloid.from_str(cloid))

    # writes (testnet only)
    def set_leverage(self, leverage, is_cross):
        self._guard()
        return self.ex.update_leverage(leverage, COIN, is_cross=is_cross)

    def ioc(self, is_buy, sz, limit_px, cloid, reduce_only):
        self._guard()
        return self.ex.order(COIN, is_buy, sz, limit_px, {"limit": {"tif": "Ioc"}}, reduce_only=reduce_only,
                             cloid=self.Cloid.from_str(cloid))

    def trigger(self, is_buy, sz, trigger_px, limit_px, tpsl, cloid):
        self._guard()
        return self.ex.order(COIN, is_buy, sz, limit_px, {"trigger": {"triggerPx": trigger_px, "isMarket": True,
                                                                      "tpsl": tpsl}},
                             reduce_only=True, cloid=self.Cloid.from_str(cloid))

    def cancel(self, oid):
        self._guard()
        return self.ex.cancel(COIN, oid)


class DryRun(object):
    """Write side of the dry-run: every write is only logged. Reads go to the real (testnet) venue."""

    def __init__(self, log):
        self.log = log
        self.sent = []

    def _w(self, what, **kw):
        self.sent.append((what, kw))
        self.log("dry_run", "DRY-RUN, nicht gesendet: %s %s" % (what, json.dumps(kw, sort_keys=True)), order=what, **kw)
        return {"status": "dry-run"}

    def set_leverage(self, leverage, is_cross):
        return self._w("set_leverage", leverage=leverage, is_cross=is_cross)

    def ioc(self, is_buy, sz, limit_px, cloid, reduce_only):
        return self._w("ioc", is_buy=is_buy, sz=sz, limit_px=limit_px, cloid=cloid, reduce_only=reduce_only)

    def trigger(self, is_buy, sz, trigger_px, limit_px, tpsl, cloid):
        return self._w("trigger", is_buy=is_buy, sz=sz, trigger_px=trigger_px, limit_px=limit_px, tpsl=tpsl,
                       cloid=cloid, reduce_only=True)

    def cancel(self, oid):
        return self._w("cancel", oid=oid)


# ---------------------------------------------------------------- reading answers

def statuses(resp):
    """Order answer -> list of status dicts. Raises TraderError for a rejected request."""
    if not isinstance(resp, dict) or resp.get("status") != "ok":
        raise TraderError("order request rejected: %s" % (resp,))
    data = (resp.get("response") or {}).get("data") or {}
    return data.get("statuses") or []


def position_of(user_state):
    for p in user_state.get("assetPositions") or []:
        pos = p.get("position") or {}
        if pos.get("coin") == COIN:
            szi = float(pos.get("szi") or 0)
            if abs(szi) > EPS:
                return {"szi": szi, "entry_px": float(pos.get("entryPx") or 0), "leverage": pos.get("leverage") or {}}
    return None


def account_of(user_state):
    ms = user_state.get("marginSummary") or {}
    return float(ms.get("accountValue") or 0), float(user_state.get("withdrawable") or 0)


def order_state(resp):
    """orderStatus answer -> (status or None for unknown, order dict)."""
    if not isinstance(resp, dict) or resp.get("status") != "order":
        return None, {}
    o = resp.get("order") or {}
    return o.get("status"), o.get("order") or {}


# ---------------------------------------------------------------- the trader

class Trader(object):
    def __init__(self, venue, writer, state_dir, live, now=now_ms, out=print, settings=None):
        self.venue = venue
        self.writer = writer
        self.live = live
        self.now = now
        self.out = out
        self.s = dict(SETTINGS)
        self.s.update(settings or {})
        self.dir = outside_checkout(os.path.join(state_dir, "state.json"), "the state folder")
        self.dir = os.path.dirname(self.dir)
        os.makedirs(self.dir, exist_ok=True)
        self.state_path = os.path.join(self.dir, "state.json")
        self.journal = os.path.join(self.dir, "journal.jsonl")
        self.cfg = None

    # --- persistence and log
    def log(self, event, text, **kw):
        rec = {"utc": iso(self.now()), "event": event, "mode": "testnet" if self.live else "dry-run", "text": text}
        rec.update(kw)
        with open(self.journal, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")
        self.out("%s  %s" % (rec["utc"], text))

    def load(self):
        if not os.path.exists(self.state_path):
            return {"phase": "idle", "trade": None, "halt": None, "last_signal": None, "uncertain": False}
        with open(self.state_path, encoding="utf-8") as fh:
            return json.load(fh)

    def save(self, st):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.state_path)

    def send(self, st, what, fn, *args):
        """Run a write. Any error = outcome unknown: save, and let the next pass check the exchange."""
        try:
            return fn(*args)
        except TraderError:
            raise
        except Exception as e:
            st["uncertain"] = True
            self.save(st)
            raise Uncertain("%s: %s: %s" % (what, type(e).__name__, e))

    # --- one pass
    def tick(self):
        st = self.load()
        now = self.now()
        if self.cfg is None:
            try:
                sz = self.venue.sz_decimals()
            except Exception as e:
                self.log("read_error", "Meta nicht lesbar (%s): keine Order in diesem Durchlauf" % e)
                return "read_error"
            self.cfg = backtest.load_config(None, {"price_rule": "hyperliquid", "sz_decimals": sz,
                                                   "position_pct": POSITION_PCT, "max_positions": MAX_POSITIONS})
        try:
            snap = self.read()
        except Exception as e:
            self.log("read_error", "Börsenstatus nicht lesbar (%s: %s): keine Order in diesem Durchlauf"
                     % (type(e).__name__, e))
            return "read_error"
        try:
            free = self.reconcile(st, snap, now)
            st["uncertain"] = False
            self.save(st)
            if free:
                return self.try_entry(st, now)
            return st["phase"]
        except Uncertain as e:
            self.log("uncertain", "Ausgang unbekannt (%s): vor der nächsten Order wird der Börsenstatus geprüft" % e)
            return "uncertain"
        except Exception as e:
            st["uncertain"] = True
            self.save(st)
            self.log("error", "Fehler im Durchlauf (%s: %s): vor der nächsten Order wird der Börsenstatus geprüft"
                     % (type(e).__name__, e))
            return "error"

    def read(self):
        us = self.venue.user_state()
        orders = self.venue.open_orders()
        return {"user_state": us, "position": position_of(us), "account": account_of(us), "orders": orders}

    def own(self, st, o):
        known = {int(k) for k in ((st.get("trade") or {}).get("orders") or {})}
        return int(o.get("oid", -1)) in known or str(o.get("cloid") or "").lower().startswith(CLOID_PREFIX)

    # --- reconcile: the exchange is the truth
    def reconcile(self, st, snap, now):
        pos, orders = snap["position"], snap["orders"]
        tr = st.get("trade")
        if st["phase"] == "entry_pending":
            self.resolve_entry(st, snap, now)
            tr = st.get("trade")
            if st["phase"] == "entry_pending":
                return False
        if tr and tr.get("pending"):
            self.resolve_pending(st)
        if st["phase"] in ("open", "exit_pending"):
            if pos is None:
                self.log("closed", "Position geschlossen (%s)" % ("5-Min-Ausstieg" if st["phase"] == "exit_pending"
                                                                  else "Stop, Ziel oder von Hand"),
                         trade=tr.get("id"))
                self.cancel_own(st, orders)
                st.update(phase="idle", trade=None)
                return False
            if (pos["szi"] > 0) != (tr["side"] == "long"):
                st["halt"] = "Position %s passt nicht zum Trade %s %s" % (pos["szi"], tr["id"], tr["side"])
                self.log("halt", "HALT: " + st["halt"])
                return False
            size = abs(pos["szi"])
            if abs(size - tr.get("filled", 0)) > EPS:
                self.log("size", "Positionsgröße laut Börse %s (bisher %s)" % (size, tr.get("filled")), trade=tr["id"])
                tr["filled"] = size
            if now >= tr["deadline"]:
                self.exit(st, size, now)
                return False
            self.protect(st, orders, size)
            return False
        # idle
        if pos is not None:
            if not st.get("halt"):
                self.log("foreign", "Offene SOL-Position %s ohne Bot-Zustand: keine neuen Orders, sie wird nicht "
                                    "angefasst" % pos["szi"])
            return False
        mine = [o for o in orders if self.own(st, o)]
        if mine:
            self.cancel_own(st, orders)
            return False
        if orders:
            self.log("foreign", "%d offene SOL-Order(s) ohne Bot-Bezug: kein Einstieg" % len(orders))
            return False
        return not st.get("halt")

    def resolve_entry(self, st, snap, now):
        tr = st["trade"]
        status, order = order_state(self.venue.order_status(tr["entry_cloid"]))
        pos = snap["position"]
        if status is None and pos is None:
            if now - tr["sent_at"] < self.s["unknown_grace_s"] * 1000:
                self.log("entry_wait", "Entry %s noch nicht bekannt: warte, keine weitere Order" % tr["entry_cloid"])
                return
            self.log("entry_none", "Entry %s ist an der Börse unbekannt und es gibt keine Position: nicht ausgeführt; "
                                   "das Signal wird nicht wiederholt" % tr["entry_cloid"])
            st.update(phase="idle", trade=None)
            return
        if status == "open":
            self.log("entry_open", "Entry ruht unerwartet im Buch: wird storniert")
            self.send(st, "cancel entry", self.writer.cancel, int(order.get("oid")))
            return
        if pos is None:
            self.log("entry_none", "Entry %s: Status %s, keine Position: nicht gefüllt" % (tr["entry_cloid"], status))
            st.update(phase="idle", trade=None)
            return
        self.log("entry_found", "Entry %s an der Börse gefunden (Status %s): Position %s" % (tr["entry_cloid"], status,
                                                                                            pos["szi"]))
        self.after_fill(st, pos, now)

    def resolve_pending(self, st):
        tr = st["trade"]
        for cloid, kind in list(tr["pending"].items()):
            status, order = order_state(self.venue.order_status(cloid))
            if status == "open" and order.get("oid") is not None:
                tr["orders"][str(int(order["oid"]))] = {"kind": kind, "cloid": cloid, "sz": float(order.get("sz") or 0),
                                                        "px": float(order.get("triggerPx") or 0)}
            del tr["pending"][cloid]

    def cancel_own(self, st, orders):
        for o in orders:
            if self.own(st, o):
                self.log("cancel", "storniere eigene Order %s" % o.get("oid"))
                self.send(st, "cancel", self.writer.cancel, int(o["oid"]))
                ((st.get("trade") or {}).get("orders") or {}).pop(str(int(o["oid"])), None)

    # --- the open trade
    def protect(self, st, orders, size):
        """Exactly one reduce-only stop and one reduce-only target, each for the position's size."""
        tr = st["trade"]
        live_oids = {int(o["oid"]): o for o in orders}
        for oid in list(tr["orders"]):
            if int(oid) not in live_oids:
                del tr["orders"][oid]                    # filled, triggered or cancelled
        have = {}
        gone = set()
        for oid, meta in sorted(tr["orders"].items()):
            o = live_oids[int(oid)]
            ok = abs(float(o.get("sz") or 0) - size) < EPS and meta["kind"] not in have
            if ok:
                have[meta["kind"]] = oid
            else:
                self.log("resize", "%s-Order %s passt nicht zur Position %s: storniert" % (meta["kind"], oid, size))
                self.send(st, "cancel", self.writer.cancel, int(oid))
                gone.add(int(oid))
                del tr["orders"][oid]
        for o in orders:                                 # own orders the state lost track of
            if self.own(st, o) and str(int(o["oid"])) not in tr["orders"] and int(o["oid"]) not in gone:
                self.log("cancel", "storniere eigene Order %s ohne Zuordnung" % o["oid"])
                self.send(st, "cancel", self.writer.cancel, int(o["oid"]))
        long_ = tr["side"] == "long"
        cfg, ps = self.cfg, self.s["protect_slippage"]
        for kind, tpsl, px in (("stop", "sl", tr["stop"]), ("target", "tp", tr["target"])):
            if kind in have:
                continue
            limit = backtest.round_price(px * (1 - ps) if long_ else px * (1 + ps), cfg, "floor" if long_ else "ceil")
            tr["n"] = tr.get("n", 0) + 1
            cloid = make_cloid(tr["id"], kind, tr["n"])
            tr["pending"][cloid] = kind
            self.save(st)
            resp = self.send(st, "%s order" % kind, self.writer.trigger, not long_, size, px, limit, tpsl, cloid)
            if not self.live:
                del tr["pending"][cloid]
                continue
            s0 = (statuses(resp) or [{}])[0]
            oid = (s0.get("resting") or s0.get("filled") or {}).get("oid")
            if "error" in s0 or oid is None:
                self.log("protect_error", "%s-Order abgelehnt: %s" % (kind, s0))
                del tr["pending"][cloid]
                continue
            tr["orders"][str(int(oid))] = {"kind": kind, "cloid": cloid, "sz": size, "px": px}
            del tr["pending"][cloid]
            self.log("protect", "%s reduce-only %s %s SOL bei %s (oid %s)" % (kind, "SELL" if long_ else "BUY", size, px,
                                                                            oid), trade=tr["id"])
        self.save(st)

    def exit(self, st, size, now):
        """The five-minute exit: reduce-only IOC for the whole position, then the protective orders go."""
        tr = st["trade"]
        tr["exit_tries"] = tr.get("exit_tries", 0) + 1
        if tr["exit_tries"] > self.s["exit_retries"]:
            st["halt"] = "5-Min-Ausstieg nach %d Versuchen nicht erreicht, Position %s" % (tr["exit_tries"] - 1, size)
            self.log("halt", "HALT: " + st["halt"] + " (Stop und Ziel bleiben aktiv)")
            return
        long_ = tr["side"] == "long"
        mid = self.venue.mid()
        ps = self.s["protect_slippage"]
        limit = backtest.round_price(mid * (1 - ps) if long_ else mid * (1 + ps), self.cfg, "floor" if long_ else "ceil")
        cloid = make_cloid(tr["id"], "exit", tr["exit_tries"])
        st["phase"] = "exit_pending"
        self.save(st)
        self.log("exit", "5-Min-Ausstieg %s: reduce-only IOC %s %s SOL, Limit %s" % (
            stamp(tr["deadline"]), "SELL" if long_ else "BUY", size, limit), trade=tr["id"])
        resp = self.send(st, "exit order", self.writer.ioc, not long_, size, limit, cloid, True)
        if self.live:
            self.log("exit_answer", "Antwort: %s" % (statuses(resp),), trade=tr["id"])
        snap = self.read()
        if snap["position"] is None:
            self.cancel_own(st, snap["orders"])
            self.log("closed", "Position geschlossen (5-Min-Ausstieg)", trade=tr["id"])
            st.update(phase="idle", trade=None)
        self.save(st)

    # --- a new entry
    def signal(self, now):
        """(signal dict, levels at the mid) for the newest closed candle, or (None, reason)."""
        settle = self.s["settle_s"] * 1000
        raw = self.venue.candles(now - self.s["history_minutes"] * MIN, now)
        data = backtest.parse_candles({"symbol": COIN, "interval": "1m", "as_of": now - settle, "candles": raw},
                                      symbol=COIN)
        last_close = data["last"] + MIN
        if now - last_close > self.s["max_entry_delay_s"] * 1000:
            return None, "FLAT: die letzte abgeschlossene Kerze %s ist zu alt für einen Einstieg" % stamp(data["last"])
        _, sigs = backtest.scan(data, self.cfg)
        cands = [s for s in sigs if s["signal_t"] == data["last"]]
        if not cands:
            return None, "FLAT: kein Setup auf der Kerze %s" % stamp(data["last"])
        if len({s["side"] for s in cands}) > 1:
            return None, "FLAT: widersprüchliche Signale auf der Kerze %s" % stamp(data["last"])
        mid = self.venue.mid()
        for sig in sorted(cands, key=lambda s: s["setup"]):
            lv, why = backtest.levels(sig, mid, self.cfg)
            if why:
                return None, "FLAT: %s %s %s (%s)" % (sig["setup"], sig["side"].upper(), why[0], why[1])
            return dict(sig, mid=mid, levels=lv), None
        return None, "FLAT"

    def try_entry(self, st, now):
        if os.path.exists(os.path.join(self.dir, "STOP")):
            self.log("stop_file", "STOP-Datei vorhanden: kein neuer Einstieg")
            return "stopped"
        sig, why = self.signal(now)
        if sig is None:
            self.out("%s  %s" % (iso(now), why))
            return "flat"
        sid = "%s|%s|%s" % (sig["cycle"], sig["setup"], sig["side"])
        if st.get("last_signal") == sid:
            return "flat"
        st["last_signal"] = sid
        self.save(st)
        long_ = sig["side"] == "long"
        # right before the entry: the account, the position and the orders as they are now
        snap = self.read()
        if snap["position"] is not None or snap["orders"]:
            self.log("skip", "Einstieg übersprungen: Position oder offene SOL-Order vorhanden", signal=sid)
            return "skipped"
        av, free = snap["account"]
        mid = sig["mid"]
        limit = backtest.round_price(mid * (1 + self.s["entry_slippage"]) if long_ else
                                     mid * (1 - self.s["entry_slippage"]), self.cfg, "ceil" if long_ else "floor")
        budget = av * POSITION_PCT / 100.0
        qty = size_round(budget / limit, self.cfg["sz_decimals"])
        if qty <= 0 or qty * mid < MIN_NOTIONAL - EPS or qty * limit < MIN_NOTIONAL - EPS:
            self.log("skip", "skipped_min_notional: %.2f %% von %.2f $ = %.2f $ -> %s SOL = %.2f $ unter %.2f $"
                     % (POSITION_PCT, av, budget, qty, qty * mid, MIN_NOTIONAL), signal=sid)
            return "skipped"
        if free < qty * limit * 1.01:
            self.log("skip", "skipped_margin: frei %.2f $, nötig %.2f $ bei 1x" % (free, qty * limit * 1.01), signal=sid)
            return "skipped"
        stop, target = sig["levels"][0], sig["levels"][1]
        self.log("signal", "%s %s Setup %s auf Kerze %s: Mid %s, Menge %s SOL (%.2f $ = %.2f %% von %.2f $), "
                           "IOC-Limit %s, Stop %s, Ziel %s, Ausstieg spätestens %s"
                 % ("LONG" if long_ else "SHORT", sid, sig["setup"], stamp(sig["signal_t"]), mid, qty, qty * limit,
                    100 * qty * limit / av, av, limit, stop, target, stamp(sig["entry_t"] + 5 * MIN)), signal=sid)
        resp = self.send(st, "leverage", self.writer.set_leverage, LEVERAGE, False)
        if self.live and (not isinstance(resp, dict) or resp.get("status") != "ok"):
            self.log("skip", "Hebel 1x nicht gesetzt (%s): kein Einstieg" % (resp,), signal=sid)
            return "skipped"
        cloid = make_cloid(sid, "entry")
        tr = {"id": sid, "setup": sig["setup"], "side": sig["side"], "signal": {k: sig[k] for k in
                                                                                ("cycle", "setup", "side", "signal_t",
                                                                                 "entry_t", "RH", "RL", "RM")},
              "entry_cloid": cloid, "planned": qty, "limit": limit, "sent_at": now, "filled": 0.0,
              "deadline": sig["entry_t"] + 5 * MIN, "stop": stop, "target": target, "orders": {}, "pending": {}}
        if sig["setup"] == "B":
            tr["signal"]["stop"] = sig["stop"]
        if self.live:
            st.update(phase="entry_pending", trade=tr)
            self.save(st)
        resp = self.send(st, "entry", self.writer.ioc, long_, qty, limit, cloid, False)
        if not self.live:
            st["trade"] = tr
            self.protect(st, [], qty)          # logs the planned stop and target
            st["trade"] = None
            self.log("dry_run", "DRY-RUN: geplanter 5-Min-Ausstieg %s (reduce-only IOC)" % stamp(tr["deadline"]))
            self.save(st)
            return "dry-run"
        s0 = (statuses(resp) or [{}])[0]
        if "error" in s0:
            self.log("entry_error", "Entry abgelehnt: %s" % s0["error"], signal=sid)
            st.update(phase="idle", trade=None)
            self.save(st)
            return "rejected"
        snap = self.read()
        if snap["position"] is None:
            self.log("entry_none", "Entry nicht gefüllt (%s)" % s0, signal=sid)
            st.update(phase="idle", trade=None)
            self.save(st)
            return "unfilled"
        self.after_fill(st, snap["position"], now)
        if st["trade"]["deadline"] <= now:
            self.exit(st, abs(snap["position"]["szi"]), now)
            return st["phase"]
        self.protect(st, snap["orders"], abs(snap["position"]["szi"]))
        return "open"

    def after_fill(self, st, pos, now):
        """The real fill: size and price from the position; check 1x; levels from the fill price."""
        tr = st["trade"]
        size = abs(pos["szi"])
        tr["filled"] = size
        tr["avg_px"] = pos["entry_px"]
        lev = pos.get("leverage") or {}
        st["phase"] = "open"
        partial = size < tr["planned"] - EPS
        self.log("filled", "Gefüllt: %s von %s SOL zu %s%s" % (size, tr["planned"], pos["entry_px"],
                                                               " (TEILFÜLLUNG)" if partial else ""), trade=tr["id"])
        if int(float(lev.get("value") or 0)) != LEVERAGE:
            st["halt"] = "Hebel der Position ist %s, nicht 1x" % lev.get("value")
            self.log("halt", "HALT: %s: sofortiger Ausstieg" % st["halt"])
            tr["deadline"] = now
            return
        sig = dict(tr["signal"])
        lv, why = backtest.levels(sig, pos["entry_px"], self.cfg)
        if why:
            self.log("bad_fill", "Füllpreis %s macht das Setup ungültig (%s): sofortiger Ausstieg" % (pos["entry_px"],
                                                                                                    why[1]))
            tr["deadline"] = now
            return
        tr["stop"], tr["target"] = lv[0], lv[1]
        self.save(st)


# ---------------------------------------------------------------- key and CLI

def read_key(key_file):
    """The API wallet's private key: from a file outside the checkout, HL_TESTNET_AGENT_KEY, or a prompt."""
    if key_file:
        path = outside_checkout(key_file, "the key file")
        with open(path, encoding="utf-8") as fh:
            key = fh.read().strip()
    elif os.environ.get("HL_TESTNET_AGENT_KEY"):
        key = os.environ["HL_TESTNET_AGENT_KEY"].strip()
    else:
        key = getpass.getpass("Privater Schlüssel des Testnet-API-Wallets (Eingabe unsichtbar): ").strip()
    if not key.startswith("0x"):
        key = "0x" + key
    if not re.match(r"^0x[0-9a-fA-F]{64}$", key):
        raise TraderError("the key is not a 32-byte hex private key")
    return key


def wallet_from_key(key, address):
    try:
        from eth_account import Account
    except ImportError:
        raise TraderError("eth-account is missing: py -3 -m pip install -r hl-testnet\\requirements.txt")
    w = Account.from_key(key)
    if w.address.lower() == address.lower():
        raise TraderError("this key belongs to the main account itself: use a separate API wallet (agent) key")
    return w


def sleep_to_next_minute(settle_s):
    t = time.time()
    time.sleep(60 - (t % 60) + settle_s)


class Lock(object):
    """One trader per state folder: a lock file with a heartbeat."""

    def __init__(self, folder, stale_s=180):
        self.path = os.path.join(folder, "trader.lock")
        self.stale_s = stale_s

    def acquire(self):
        if os.path.exists(self.path) and time.time() - os.path.getmtime(self.path) < self.stale_s:
            raise TraderError("another trader is running for this state folder (%s)" % self.path)
        self.beat()

    def beat(self):
        with open(self.path, "w") as fh:
            fh.write("%d %d\n" % (os.getpid(), time.time()))

    def release(self):
        if os.path.exists(self.path):
            os.remove(self.path)


def main(argv=None, venue_factory=SdkVenue):
    ap = argparse.ArgumentParser(prog="trader.py", description="SOL perps on the Hyperliquid TESTNET (dry-run first).")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("run", "status", "reset-halt"):
        p = sub.add_parser(name)
        p.add_argument("--state-dir", default=os.environ.get("HL_TESTNET_STATE") or default_state_dir())
        if name != "reset-halt":
            p.add_argument("--address", default=os.environ.get("HL_TESTNET_ADDRESS"),
                           help="0x address of the main testnet account (public)")
        if name == "run":
            p.add_argument("--send-testnet-orders", action="store_true",
                           help="really send orders, to the testnet only (default: dry-run)")
            p.add_argument("--key-file", help="the API wallet's private key, in a file outside the checkout")
            p.add_argument("--once", action="store_true", help="one pass, then stop")
            p.add_argument("--max-minutes", type=int, help="stop after this many passes")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        state_dir = os.path.dirname(outside_checkout(os.path.join(a.state_dir, "state.json"), "the state folder"))
        os.makedirs(state_dir, exist_ok=True)
        if a.cmd == "reset-halt":
            path = os.path.join(state_dir, "state.json")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    st = json.load(fh)
                print("Halt war: %s" % st.get("halt"))
                st["halt"] = None
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(st, fh, indent=1, sort_keys=True)
            return 0
        address = check_address(a.address)
        if a.cmd == "status":
            v = venue_factory(address)
            us = v.user_state()
            av, free = account_of(us)
            print("Testnet %s, Konto %s: Kontowert %.2f $, frei %.2f $" % (TESTNET_URL, address, av, free))
            print("SOL-Position: %s" % (position_of(us),))
            print("Offene SOL-Orders: %s" % (json.dumps(v.open_orders()),))
            path = os.path.join(state_dir, "state.json")
            text = "keiner"
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    text = fh.read().strip()
            print("Zustand: %s" % text)
            return 0
        if a.send_testnet_orders:
            wallet = wallet_from_key(read_key(a.key_file), address)
            venue = venue_factory(address, wallet)
            writer = venue
            print("TESTNET-ORDERS AKTIV: %s, Konto %s, API-Wallet %s" % (TESTNET_URL, address, wallet.address))
        else:
            if a.key_file:
                raise TraderError("--key-file only with --send-testnet-orders; the dry-run needs no key")
            venue = venue_factory(address)
            writer = None
            print("DRY-RUN: geplante Orders werden nur protokolliert, nichts wird gesendet (%s)" % TESTNET_URL)
        lock = Lock(state_dir)
        lock.acquire()
        trader = Trader(venue, writer, state_dir, live=bool(a.send_testnet_orders))
        if writer is None:
            trader.writer = DryRun(trader.log)
        try:
            n = 0
            while True:
                trader.tick()
                lock.beat()
                n += 1
                if a.once or (a.max_minutes and n >= a.max_minutes):
                    break
                sleep_to_next_minute(trader.s["settle_s"])
        except KeyboardInterrupt:
            print("beendet (Strg+C). Offene Testnet-Position und Stop/Ziel bleiben an der Börse bestehen.")
        finally:
            lock.release()
    except TraderError as e:
        print("FEHLER: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
