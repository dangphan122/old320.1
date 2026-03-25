"""
live_trader.py — Production Live Execution Engine for Polymarket CLOB.

Implements 3 mechanics:
  1. Pragmatic $0.50 min order floor (gas efficiency)
  2. Dynamic Taker-to-Maker L2 Passive Pegging fallback
  3. Ghost Equity Garbage Collector (5-min unfilled order cancellation)

Requires:
  pip install py_clob_client
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("live_trader")

# Import Polymarket CLOB client
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.constants import POLYGON
    CLOB_AVAILABLE = True
except ImportError:
    CLOB_AVAILABLE = False
    POLYGON = 137  # Fallback to prevent NameError in LiveExecutor signature
    log.warning("py_clob_client not installed — live execution disabled")

# Config
PRAGMATIC_MIN_ORDER     = 0.50   # $0.50 floor — gas fee would destroy smaller edges
MAKER_DEPTH_MULTIPLIER  = 2.5    # required_depth = trade_size * 2.5 for Taker route
MAKER_GC_TIMEOUT_SEC    = 300    # Garbage Collector: cancel unfilled Maker after 5 min
TICK_SIZE               = 0.01   # Polymarket standard tick size


@dataclass
class PendingOrder:
    """Tracks a resting GTC Maker order for the Garbage Collector."""
    order_id:       str
    token_id:       str
    strike:         int
    side:           str
    limit_price:    float
    allocation_usd: float
    placed_at:      float = field(default_factory=time.time)
    event_title:    str = ""

    def age_sec(self) -> float:
        return time.time() - self.placed_at

    def is_expired(self) -> bool:
        return self.age_sec() > MAKER_GC_TIMEOUT_SEC


def get_l2_bid(book, fallback_bid: float) -> float:
    """
    Fetch Level-2 (second-best) bid to use as a passive peg price.

    L2 PASSIVE PEGGING LOGIC:
      By placing our GTC limit at Level-2 (below Level-1 Best Bid),
      we are mathematically GUARANTEED not to cross the spread and become
      an accidental Taker, without needing POST_ONLY order type.

    Example:
      Bids: {0.30: 200, 0.28: 500}  ->  L2 Bid = $0.28  (place here)
      Bids: {0.30: 200}             ->  L2 Bid = $0.29  (best_bid - tick)
    """
    try:
        sorted_bids = sorted(((float(p), s) for p, s in book.bids.items()), reverse=True)
        if len(sorted_bids) >= 2:
            return sorted_bids[1][0]   # Level-2 bid
        elif sorted_bids:
            return max(round(sorted_bids[0][0] - TICK_SIZE, 4), TICK_SIZE)
    except Exception as e:
        log.warning("L2 lookup failed: %s", e)
    return round(fallback_bid - TICK_SIZE, 4)


class LiveExecutor:
    """
    Production execution wrapper for py_clob_client.

    Routing logic:
      ask_depth_usd >= allocation * 2.5  ->  FOK Taker (instant fill)
      ask_depth_usd <  allocation * 2.5  ->  GTC at L2 peg (passive Maker)
    """

    def __init__(self, host: str, key: str, chain_id: int = POLYGON,
                 funder: Optional[str] = None):
        if not CLOB_AVAILABLE:
            raise RuntimeError("py_clob_client not installed. Run: pip install py_clob_client")

        self.client = ClobClient(host, key=key, chain_id=chain_id, funder=funder)
        self.client.set_api_credentials(self.client.derive_api_key())

        self.pending_orders: list = []
        self._lock = asyncio.Lock()
        self._running = False

    # ── MECHANIC 1 + 2: Execute with routing ──────────────────────────────────
    async def execute(self, token_id: str, strike: int, side: str,
                      limit_price: float, allocation_usd: float,
                      book, event_title: str = "") -> Optional[str]:
        """
        Main entry point. Returns order_id or None if skipped.

        Steps:
          1. Check $0.50 floor
          2. Measure ask depth vs. required depth
          3. Route to FOK Taker or GTC L2 Maker
        """

        # ── MECHANIC 1: Pragmatic min order ($0.50) ──
        if allocation_usd < PRAGMATIC_MIN_ORDER:
            log.info("SKIP | $%s %s | $%.2f < $%.2f floor | gas inefficient",
                     f"{strike:,}", side, allocation_usd, PRAGMATIC_MIN_ORDER)
            return None

        async with self._lock:
            if not book:
                return None

            best_ask = book.ba()
            best_bid = book.bb()
            ask_size = book.ba_size() or 0
            if not best_ask or not best_bid:
                return None

            tokens = allocation_usd / limit_price

            # ── MECHANIC 2: Depth check — Taker or Maker? ──
            ask_depth_usd  = ask_size * best_ask
            required_depth = allocation_usd * MAKER_DEPTH_MULTIPLIER

            if ask_depth_usd >= required_depth:
                # Enough liquidity — hit as Taker (FOK)
                return await self._taker_fok(token_id, strike, side, limit_price, tokens, allocation_usd)
            else:
                # Thin book — passive GTC at L2 peg
                l2_peg = get_l2_bid(book, best_bid)
                return await self._maker_gtc(token_id, strike, side, l2_peg, tokens, allocation_usd, event_title)

    async def _taker_fok(self, token_id, strike, side, price, tokens, alloc_usd):
        """FOK: Fill-Or-Kill. Locks in the mathematical edge immediately."""
        try:
            resp = self.client.create_and_post_order(
                OrderArgs(price=price, size=round(tokens, 4), side=side, token_id=token_id),
                OrderType.FOK
            )
            oid = resp.get("orderID") or resp.get("id", "?")
            log.info("TAKER FOK | $%s %s @ %.4f | $%.2f | id=%s", f"{strike:,}", side, price, alloc_usd, oid)
            return oid
        except Exception as e:
            log.error("TAKER FOK failed: %s", e)
            return None

    async def _maker_gtc(self, token_id, strike, side, peg_price, tokens,
                         alloc_usd, event_title):
        """
        GTC at Level-2 bid price.

        WHY THIS IS SAFE:
          peg_price  = L2_bid  < best_bid  < best_ask
          Therefore: our limit order CANNOT cross the spread.
          The exchange will REST the order as a Maker, zero Taker fees.
          This replaces POST_ONLY which py_clob_client does not support.
        """
        try:
            resp = self.client.create_and_post_order(
                OrderArgs(price=peg_price, size=round(tokens, 4), side=side, token_id=token_id),
                OrderType.GTC
            )
            oid = resp.get("orderID") or resp.get("id", "?")
            log.info("MAKER GTC L2 | $%s %s @ %.4f (L2 passive peg) | $%.2f | id=%s",
                     f"{strike:,}", side, peg_price, alloc_usd, oid)

            # Register with Garbage Collector
            self.pending_orders.append(PendingOrder(
                order_id=oid, token_id=token_id,
                strike=strike, side=side,
                limit_price=peg_price, allocation_usd=alloc_usd,
                event_title=event_title,
            ))
            return oid

        except Exception as e:
            log.error("MAKER GTC failed: %s", e)
            return None

    # ── MECHANIC 3: Ghost Equity Garbage Collector ─────────────────────────────
    async def garbage_collector(self):
        """
        Async background loop — scans every 60s for expired Maker orders.

        If a GTC order has NOT been filled after MAKER_GC_TIMEOUT_SEC (5 min),
        the bot calls cancel_order() to unlock the ghost equity so the
        Frank-Wolfe optimizer can redeploy it to better opportunities.

        GHOST EQUITY PROBLEM:
          Capital tied to pending GTC orders is "ghost equity" — still committed
          but generating no returns. Without active cancellation, this capital
          stays locked even if a better trade appears. The GC prevents this.
        """
        self._running = True
        log.info("Ghost Equity GC started | timeout=%ds", MAKER_GC_TIMEOUT_SEC)
        while self._running:
            await asyncio.sleep(60)
            async with self._lock:
                expired = [o for o in self.pending_orders if o.is_expired()]
                for order in expired:
                    try:
                        self.client.cancel(order.order_id)
                        log.info(
                            "GC CANCEL | $%s %s | id=%s | age=%.0fs | $%.2f equity unlocked",
                            f"{order.strike:,}", order.side,
                            order.order_id, order.age_sec(), order.allocation_usd)
                        self.pending_orders.remove(order)
                    except Exception as e:
                        log.warning("GC cancel failed %s: %s", order.order_id, e)
                if expired:
                    log.info("GC cycle complete | %d orders cancelled", len(expired))

    def stop(self):
        self._running = False


# ── Wire-up instructions for bot.py ──────────────────────────────────────────
#
# 1. In TradingBot.__init__:
#      from live_trader import LiveExecutor
#      self.executor = LiveExecutor(
#          host="https://clob.polymarket.com",
#          key=os.environ["POLY_PRIVATE_KEY"],
#          funder=os.environ.get("POLY_FUNDER_ADDRESS"),
#      )
#
# 2. In TradingBot.run() asyncio.gather():
#      asyncio.create_task(self.executor.garbage_collector()),
#
# 3. Replace submit_taker / submit_maker with:
#      await self.executor.execute(
#          token_id       = br.yes_tid if side == "YES" else br.no_tid,
#          strike         = strike,
#          side           = side,
#          limit_price    = target_price,
#          allocation_usd = allocation_usd,
#          book           = ybk,
#          event_title    = br.event_title,
#      )
