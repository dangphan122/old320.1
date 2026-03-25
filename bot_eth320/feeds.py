"""
feeds.py - Real data feeds for the paper trading bot.
- DeribitOracle: Fetches IV from Deribit, computes N(d2)
- PolymarketFeed: Discovers brackets, WebSocket order books
- Binance: Live ETH price
"""

import asyncio
import json
import logging
import re
import socket
import ssl
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import aiohttp
import aiohttp.abc
import numpy as np
import requests
from scipy.stats import norm

try:
    import websockets
except ImportError:
    raise SystemExit("pip install websockets")

log = logging.getLogger("feeds")

# ===================== CONSTANTS =====================================
GAMMA_URL = "https://gamma-api.polymarket.com/events"
CLOB_URL = "https://clob.polymarket.com"
POLY_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BINANCE_WS = "wss://stream.binance.com:9443/ws/ethusdt@bookTicker"
DERIBIT_API = ("https://www.deribit.com/api/v2/public/"
               "get_book_summary_by_currency?currency=ETH&kind=option")
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
SECONDS_PER_YEAR = 31_536_000.0
_INST_RE = re.compile(r"^ETH-(\d{1,2}[A-Z]{3}\d{2})-(\d+)-(C|P)$")
_MONTH = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
           "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}


# ===================== DATA MODELS ===================================

@dataclass
class Bracket:
    question: str
    strike: int
    condition_id: str
    end_ts: float
    yes_tid: str
    no_tid: str
    event_title: str = ""


@dataclass
class Book:
    bids: dict = field(default_factory=dict)
    asks: dict = field(default_factory=dict)
    ts: float = 0.0

    def bb(self):
        return max((float(p) for p in self.bids), default=None)

    def ba(self):
        return min((float(p) for p in self.asks), default=None)

    def bb_size(self):
        b = self.bb()
        if b is None: return 0.0
        for p, s in self.bids.items():
            if abs(float(p) - b) < 0.0001: return s
        return 0.0

    def ba_size(self):
        a = self.ba()
        if a is None: return 0.0
        for p, s in self.asks.items():
            if abs(float(p) - a) < 0.0001: return s
        return 0.0

    def mid(self):
        b, a = self.bb(), self.ba()
        if b and a: return (b + a) / 2
        return b or a

    def spread(self):
        b, a = self.bb(), self.ba()
        return (a - b) if (b and a) else None


# ===================== WINDOWS DNS FIX ===============================

class _ManualResolver(aiohttp.abc.AbstractResolver):
    """Windows DNS workaround for aiohttp."""
    def __init__(self):
        self._cache = {}
    async def resolve(self, host, port=0, family=socket.AF_INET):
        if host not in self._cache:
            self._cache[host] = socket.gethostbyname(host)
            log.info("DNS: %s -> %s", host, self._cache[host])
        return [{"hostname": host, "host": self._cache[host], "port": port,
                 "family": family, "proto": 0, "flags": socket.AI_NUMERICHOST}]
    async def close(self):
        pass


# ===================== DERIBIT ORACLE ================================

def _parse_deribit_expiry(s):
    day = int(s[:-5])
    return datetime(2000+int(s[-2:]), _MONTH[s[-5:-2]], day,
                    hour=8, tzinfo=timezone.utc)


class MarketDataCache:
    def __init__(self):
        self.spot_price = 0.0
        self.spot_ts = 0.0
        
        self.future_expiries = []
        self.raw_iv_cache = {}
        self.iv_ts = 0.0
        
    def is_stale(self, max_age_spot=5.0, max_age_iv=15.0):
        now = time.time()
        # Spot needs to be extremely fresh
        if now - self.spot_ts > max_age_spot: return True
        # IV updates over WS loop every 2-3s
        if now - self.iv_ts > max_age_iv: return True
        return False

class DeribitOracle:
    """Fetches Deribit IV and computes N(d2) probabilities using WebSocket Architecture."""

    def __init__(self):
        self.targets = {}
        self.latest = {}
        self.cache = MarketDataCache()
        self._running = False

    @property
    def spot_price(self): return self.cache.spot_price

    @property
    def future_expiries(self): return self.cache.future_expiries

    @property
    def raw_iv_cache(self): return self.cache.raw_iv_cache

    def update_targets(self, targets):
        self.targets = {ts: set(strikes) for ts, strikes in targets.items()}

    @staticmethod
    def _norm_exp(s):
        dt = _parse_deribit_expiry(s)
        return f"{dt.day}{dt.strftime('%b%y').upper()}"

    @staticmethod
    def _nd2(spot, strike, iv, t_years):
        if t_years <= 0 or iv <= 0:
            return 1.0 if spot >= strike else 0.0
        sqrt_t = np.sqrt(t_years)
        d1 = (np.log(spot/strike) + (iv**2/2)*t_years) / (iv*sqrt_t)
        return float(norm.cdf(d1 - iv*sqrt_t))

    def get_iv_for_date(self, target_dt, strike):
        """
        Strict per-bracket IV lookup with Volatility Smile Interpolation.
        Returns (interpolated_iv, expiry_code) or (None, None).
        """
        # READ FROM MICROSECOND CACHE
        futures = self.cache.future_expiries
        iv_cache = self.cache.raw_iv_cache
        if not futures or not iv_cache:
            return None, None
            
        target_date = target_dt.date() if hasattr(target_dt, "date") else target_dt
        chosen_code = None
        for dt, code in futures:
            if dt.date() >= target_date:
                chosen_code = code
                break
        if chosen_code is None:
            chosen_code = futures[-1][1]
            
        strike_map = iv_cache.get(chosen_code, {})
        if not strike_map: return None, chosen_code
        
        if strike in strike_map: return strike_map[strike], chosen_code
            
        strikes = sorted(strike_map.keys())
        if strike <= strikes[0]: return strike_map[strikes[0]], chosen_code
        if strike >= strikes[-1]: return strike_map[strikes[-1]], chosen_code
            
        for i in range(len(strikes) - 1):
            k1 = strikes[i]
            k2 = strikes[i+1]
            if k1 < strike < k2:
                iv1 = strike_map[k1]
                iv2 = strike_map[k2]
                weight = (strike - k1) / float(k2 - k1)
                return iv1 + weight * (iv2 - iv1), chosen_code
                
        return None, chosen_code

    async def _binance_spot_ws(self):
        """True Streaming @bookTicker for instant Spot Price"""
        uri = "wss://stream.binance.com:9443/ws/ethusdt@bookTicker"
        while self._running:
            try:
                async with websockets.connect(uri) as ws:
                    log.info("Binance @bookTicker WS Connected (Oracle)")
                    async for msg in ws:
                        data = json.loads(msg)
                        if "b" in data and "a" in data:
                            self.cache.spot_price = (float(data['b']) + float(data['a'])) / 2.0
                            self.cache.spot_ts = time.time()
            except Exception as e:
                log.error("Binance WS dropped: %s", e)
                await asyncio.sleep(2)

    async def _deribit_chain_ws(self):
        """Websocket connection polling Option Chain every 5.0s"""
        uri = "wss://www.deribit.com/ws/api/v2"
        while self._running:
            try:
                async with websockets.connect(uri) as ws:
                    log.info("Deribit Options Chain WS Connected")
                    while self._running:
                        req = {
                            "jsonrpc": "2.0", "id": 1,
                            "method": "public/get_book_summary_by_currency",
                            "params": {"currency": "ETH", "kind": "option"}
                        }
                        await ws.send(json.dumps(req))
                        resp = json.loads(await ws.recv())
                        results = resp.get("result", [])
                        if not results:
                            await asyncio.sleep(5.0)
                            continue
                            
                        avail = set()
                        for item in results:
                            m = _INST_RE.match(item.get("instrument_name", ""))
                            if m: avail.add(m.group(1))

                        now = datetime.now(timezone.utc)
                        future = []
                        for s in avail:
                            try:
                                dt = _parse_deribit_expiry(s)
                                if dt > now: future.append((dt, s))
                            except: pass
                        future.sort()

                        raw_cache = {}
                        for item in results:
                            m = _INST_RE.match(item.get("instrument_name", ""))
                            if not m or m.group(3) != "C": continue
                            exp_code = m.group(1)
                            try: strike = int(m.group(2))
                            except: continue

                            raw_iv = item.get("mark_iv")
                            if raw_iv is None or raw_iv <= 0: continue

                            oi = item.get("open_interest", 0)
                            ask_iv_r = item.get("ask_iv", 0)
                            bid_iv_r = item.get("bid_iv", 0)
                            if oi == 0: continue
                            if ask_iv_r and bid_iv_r and (ask_iv_r - bid_iv_r) / 100.0 > 0.15: continue

                            if exp_code not in raw_cache: raw_cache[exp_code] = {}
                            raw_cache[exp_code][strike] = raw_iv / 100.0

                        self.cache.future_expiries = future
                        self.cache.raw_iv_cache = raw_cache
                        self.cache.iv_ts = time.time()
                        
                        # Dashboard compat
                        out = {}
                        for exp_ts in self.targets.keys():
                            poly_exp = datetime.fromtimestamp(exp_ts, tz=timezone.utc)
                            t_years = (poly_exp - now).total_seconds() / SECONDS_PER_YEAR
                            active = None
                            if future:
                                poly_date = poly_exp.date()
                                for dt, s in future:
                                    if dt.date() >= poly_date:
                                        active = self._norm_exp(s); break
                                if not active: active = self._norm_exp(future[0][1])
                            out[exp_ts] = {"spot_price": round(self.cache.spot_price, 2), "t_years": round(t_years, 8),
                                           "deribit_expiry": active, "probabilities": {}}
                        self.latest = out
                        
                        await asyncio.sleep(5.0)
            except Exception as e:
                log.error("Deribit WS dropped: %s", e)
                await asyncio.sleep(2)

    async def run(self, poll_sec=5):
        self._running = True
        log.info("Starting WS Engines (Oracle & Binance)")
        await asyncio.gather(
            self._binance_spot_ws(),
            self._deribit_chain_ws()
        )

    def stop(self):
        self._running = False


# ===================== POLYMARKET FEED ===============================

def parse_strike(q):
    m = re.search(r'\$?([\d,]+)', q)
    return int(m.group(1).replace(",", "")) if m else 0


def _parse_event_markets(ev, seen, now):
    brackets = []
    tl = ev.get("title", "").lower()
    # Data Diet: Restore v314 Standard Filters
    if "ethereum price on" in tl: return brackets
    if "ethereum" not in tl: return brackets
    
    for m in ev.get("markets", []):
        cid = m.get("conditionId", "")
        if cid in seen: continue
        seen.add(cid)
        end_str = m.get("endDate", "")
        if not end_str: continue
        try:
            end_ts = datetime.fromisoformat(
                end_str.replace("Z", "+00:00")).timestamp()
        except: continue
        if end_ts <= now: continue
        q = m.get("question", "")
        if not re.search(r"ethereum.*?above", q, re.IGNORECASE): continue
        strike = parse_strike(q)
        if strike == 0: continue
        tids = m.get("clobTokenIds", "[]")
        if isinstance(tids, str): tids = json.loads(tids)
        outs = m.get("outcomes", "[]")
        if isinstance(outs, str): outs = json.loads(outs)
        if len(tids) < 2: continue
        yt, nt = str(tids[0]), str(tids[1])
        for i, label in enumerate(outs):
            if str(label).lower() == "yes" and i < len(tids): yt = str(tids[i])
            elif str(label).lower() == "no" and i < len(tids): nt = str(tids[i])
        brackets.append(Bracket(question=q, strike=strike, condition_id=cid,
                                end_ts=end_ts, yes_tid=yt, no_tid=nt,
                                event_title=ev.get("title", "")))
    return brackets


async def discover_bracket_events(session):
    now = time.time()
    brackets, seen = [], set()
    today = datetime.now(timezone.utc)
    for delta in range(0, 14):
        d = today + timedelta(days=delta)
        slug = f"ethereum-above-on-{d.strftime('%B').lower()}-{d.day}"
        try:
            async with session.get(GAMMA_URL, params={"slug": slug, "limit": "1"}, headers=UA, timeout=aiohttp.ClientTimeout(total=10)) as r:
                r.raise_for_status()
                data = await r.json()
                for ev in data:
                    brackets.extend(_parse_event_markets(ev, seen, now))
        except: continue
    for params in [
        {"tag": "ethereum", "active": "true", "closed": "false", "limit": "50"},
        {"tag": "crypto", "active": "true", "closed": "false", "limit": "100"},
    ]:
        try:
            async with session.get(GAMMA_URL, params=params, headers=UA, timeout=aiohttp.ClientTimeout(total=15)) as r:
                r.raise_for_status()
                data = await r.json()
                for ev in data:
                    brackets.extend(_parse_event_markets(ev, seen, now))
        except: continue
    brackets.sort(key=lambda b: (b.end_ts, b.strike))
    return brackets


class PolymarketFeed:
    """Live order books + ETH price."""

    def __init__(self):
        self.brackets = []
        self.books = {}
        self.eth_price = None
        self.eth_history = deque(maxlen=800)
        self._running = False
        self.poly_ok = False
        self.bnc_ok = False

    def get_book(self, tid): return self.books.get(tid)
    def get_eth_price(self): return self.eth_price
    def get_brackets(self):
        now = time.time()
        return [b for b in self.brackets if b.end_ts > now]

    async def refresh(self, session):
        self.brackets = await discover_bracket_events(session)
        log.info("Discovered %d brackets", len(self.brackets))
        return len(self.brackets) > 0

    async def run(self):
        self._running = True
        ssl_ctx = ssl.create_default_context()
        conn = aiohttp.TCPConnector(resolver=_ManualResolver(), ssl=ssl_ctx, limit=20, force_close=True)
        async with aiohttp.ClientSession(connector=conn) as session:
            await self.refresh(session)
            await asyncio.gather(
                self._ws_poly(), self._ws_bnc(),
                self._refresh_loop(session))

    async def _ws_poly(self):
        while self._running:
            if not self.brackets:
                await asyncio.sleep(5); continue
            tids = []
            for b in self.brackets: tids.extend([b.yes_tid, b.no_tid])
            try:
                hdrs = {"User-Agent": "Mozilla/5.0", "Origin": "https://polymarket.com"}
                async with websockets.connect(POLY_WS, additional_headers=hdrs, ping_interval=15, ping_timeout=10, max_size=2**26) as ws:
                    log.info("Polymarket CLOB WS Subscribing to %d tokens", len(tids))
                    await ws.send(json.dumps({"assets_ids": tids, "type": "market"}))
                    self.poly_ok = True
                    async for msg in ws:
                        if not self._running: break
                        try:
                            raw = json.loads(msg)
                            items = raw if isinstance(raw, list) else [raw]
                            for item in items:
                                if isinstance(item, dict):
                                    self._handle_poly(item)
                        except json.JSONDecodeError: pass
            except Exception as e:
                log.warning("Poly WS Disconnected: %s", e)
            self.poly_ok = False
            if self._running: await asyncio.sleep(5)

    def _handle_poly(self, d):
        et = d.get("event_type", "")
        
        # 1. Full Book Snapshot
        if et == "book":
            aid = d.get("asset_id", "")
            if not aid: return
            bk = Book()
            for b in d.get("bids", []):
                if isinstance(b, dict):
                    p, s = str(b.get("price","0")), float(b.get("size",0))
                    if float(p) > 0 and s > 0: bk.bids[p] = s
            for a in d.get("asks", []):
                if isinstance(a, dict):
                    p, s = str(a.get("price","0")), float(a.get("size",0))
                    if float(p) > 0 and s > 0: bk.asks[p] = s
            bk.ts = time.time()
            self.books[aid] = bk
            return
            
        # 2. Delta Price Changes
        if et == "price_change":
            for c in (d.get("changes") or d.get("price_changes") or []):
                if not isinstance(c, dict): continue
                aid = c.get("asset_id") or d.get("asset_id", "")
                if not aid: continue
                if aid not in self.books: continue
                bk = self.books[aid]
                side = c.get("side", "")
                price = str(c.get("price", "0"))
                size = float(c.get("size", 0))
                if size == 0:
                    if side == "buy": bk.bids.pop(price, None)
                    elif side == "sell": bk.asks.pop(price, None)
                else:
                    if side == "buy": bk.bids[price] = size
                    elif side == "sell": bk.asks[price] = size
                bk.ts = time.time()

    async def _ws_bnc(self):
        while self._running:
            try:
                async with websockets.connect(BINANCE_WS,
                        ping_interval=20, ping_timeout=10) as ws:
                    log.info("Binance @bookTicker WS Connected (Feed)")
                    self.bnc_ok = True
                    async for msg in ws:
                        if not self._running: break
                        try:
                            d = json.loads(msg)
                            if "b" in d and "a" in d:
                                self.eth_price = (float(d["b"]) + float(d["a"])) / 2.0
                                now_t = time.time()
                                if not self.eth_history or now_t - self.eth_history[-1]["t"] >= 5.0:
                                    self.eth_history.append({"p": self.eth_price, "t": now_t})
                        except: pass
            except Exception as e:
                log.warning("Bnc WS: %s", e)
            self.bnc_ok = False
            if self._running: await asyncio.sleep(3)

    async def _poll_books(self, session):
        await asyncio.sleep(8)
        while self._running:
            for br in self.brackets:
                if not self._running: break
                for tid in [br.yes_tid, br.no_tid]:
                    try:
                        async with session.get(f"{CLOB_URL}/book", params={"token_id": tid}, headers=UA, timeout=aiohttp.ClientTimeout(total=5)) as r:
                            r.raise_for_status()
                            data = await r.json()
                            bk = Book()
                            for b in data.get("bids", []):
                                p = str(b.get("price","0"))
                                s = float(b.get("size",0))
                                if float(p)>0 and s>0: bk.bids[p] = s
                            for a in data.get("asks", []):
                                p = str(a.get("price","0"))
                                s = float(a.get("size",0))
                                if float(p)>0 and s>0: bk.asks[p] = s
                            bk.ts = time.time()
                            self.books[tid] = bk
                    except: pass
                await asyncio.sleep(0.2)
            await asyncio.sleep(15)

    async def _refresh_loop(self, session):
        while self._running:
            await asyncio.sleep(120)
            if self._running: await self.refresh(session)

    def stop(self):
        self._running = False
