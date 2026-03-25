"""
paper_trader.py - Simulated fills, position tracking, CSV logging.
- No SQLite - pure in-memory
- Unified CSV: OPEN row on entry, CLOSE row on exit (same file)
- Mark-to-market using current best_bid
"""

import asyncio
import csv
import logging
import math
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger("paper_trader")

INITIAL_CAPITAL = 200.0
MAKER_FILL_PROBABILITY = 0.60
MAKER_FILL_DELAY_MIN = 5
MAKER_FILL_DELAY_MAX = 30
MIN_BOOK_PRICE = 0.01        # reject any fill below 1 cent (bad book)
MAX_BOOK_AGE_SEC = 30        # reject books older than 30 seconds
MAKER_MAX_SLIP = 0.20        # cancel maker if market moved >20% from queue price

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_history.csv")
CSV_HEADERS = [
    "timestamp", "event_type", "position_id",
    "event_name", "strike", "side", "order_type",
    "tokens", "entry_price", "exit_price",
    "invested_usd", "realized_pnl_usd", "roi_pct",
    "entry_edge_pct", "entry_reason", "close_reason",
    "runtime_min"
]


@dataclass
class Position:
    id: str
    strike: int
    side: str            # YES or NO
    order_type: str      # TAKER_FAK or MAKER_POST_ONLY
    entry_price: float
    tokens: float
    cost_usd: float
    end_ts: float        # bracket expiry
    opened_at: float
    event_title: str = ""
    entry_reason: str = ""   # e.g. "edge=5.2% w=0.35"
    edge_at_entry: float = 0.0
    status: str = "OPEN"
    _pnl: float = field(default=0.0, repr=False)


class PaperTrader:
    """Manages virtual capital and simulated trades."""

    def __init__(self, initial_capital=INITIAL_CAPITAL):
        self.capital = initial_capital
        self.initial_capital = initial_capital
        self.positions = []
        self.pending_makers = []
        self.trade_lock = asyncio.Lock()
        self.log_lines = []
        self._ensure_csv()
        log.info("PaperTrader | capital=$%.2f", self.capital)

    def _ensure_csv(self):
        if not os.path.exists(CSV_PATH):
            with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(CSV_HEADERS)

    def _add_log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self.log_lines.append(line)
        if len(self.log_lines) > 300:
            self.log_lines = self.log_lines[-300:]
        log.info(msg)

    # --- Queries ---
    def get_cash(self): return self.capital

    def open_positions(self):
        return [p for p in self.positions if p.status == "OPEN"]

    def open_strikes(self):
        return set(p.strike for p in self.open_positions())

    def _get_bid(self, current_bids, pos):
        # Dashboard/Bot pass (strike, end_ts, side)
        if (pos.strike, pos.end_ts, pos.side) in current_bids:
            return current_bids[(pos.strike, pos.end_ts, pos.side)]
        elif (pos.strike, pos.end_ts) in current_bids:
            return current_bids[(pos.strike, pos.end_ts)]
        return current_bids.get(pos.strike, pos.entry_price)

    def open_positions_value(self, current_bids):
        return sum(pos.tokens * self._get_bid(current_bids, pos)
                   for pos in self.open_positions())

    def unrealized_pnl(self, current_bids):
        return sum((self._get_bid(current_bids, pos) - pos.entry_price) * pos.tokens
                   for pos in self.open_positions())

    def realized_pnl(self):
        return sum(p._pnl for p in self.positions if p.status == "CLOSED")

    def pending_makers_value(self):
        return sum(order["allocation_usd"] for order in self.pending_makers)

    def total_equity(self, current_bids):
        return self.capital + self.pending_makers_value() + self.open_positions_value(current_bids)

    def win_rate(self):
        closed = [p for p in self.positions if p.status == "CLOSED"]
        if not closed: return 0.0
        return sum(1 for p in closed if p._pnl > 0) / len(closed) * 100

    # --- Sanity Check for Order Book ---
    @staticmethod
    def book_is_valid(book):
        """Reject books that are stale or have suspiciously low prices."""
        if book is None:
            return False
        bid = book.bb()
        ask = book.ba()
        if bid is None or ask is None:
            return False
        if bid < MIN_BOOK_PRICE or ask < MIN_BOOK_PRICE:
            return False
        now = time.time()
        if book.ts > 0 and (now - book.ts) > MAX_BOOK_AGE_SEC:
            return False
        return True

    # --- Order Submission ---
    async def submit_taker(self, strike, price, allocation_usd, end_ts,
                           event_title="", entry_reason="", edge=0.0, book=None,
                           ask_size=0.0, side="YES"):
        if not self.book_is_valid(book):
            self._add_log(f"SKIP TAKER | ${strike:,} | book invalid/stale/thin")
            return None
        async with self.trade_lock:
            desired_alloc = min(allocation_usd, self.capital)
            if desired_alloc < 0.01: return None
            desired_tokens = desired_alloc / price
            
            filled_tokens = min(desired_tokens, ask_size) if ask_size > 0 else desired_tokens
            if filled_tokens < 0.01:
                self._add_log(f"SKIP TAKER | ${strike:,} | ask_size too small for fill")
                return None

            filled_alloc = filled_tokens * price
            self.capital -= filled_alloc
            pos = Position(
                id=str(uuid.uuid4())[:8],
                strike=strike, side=side, order_type="TAKER_FAK",
                entry_price=price, tokens=filled_tokens, cost_usd=filled_alloc,
                end_ts=end_ts, opened_at=time.time(),
                event_title=event_title, entry_reason=entry_reason,
                edge_at_entry=edge)
            self.positions.append(pos)
            self._add_log(
                f"BUY TAKER | {event_title or f'${strike:,}'} | "
                f"{filled_tokens:.1f}tok @ {price:.4f} (max_avail={ask_size:.1f}) | ${filled_alloc:.2f} | {entry_reason}")
            self._csv_open(pos)
            return pos

    async def submit_maker(self, strike, price, allocation_usd, end_ts,
                           event_title="", entry_reason="", edge=0.0, book=None,
                           yes_tid=None, side="YES"):
        if not self.book_is_valid(book):
            self._add_log(f"SKIP MAKER | ${strike:,} | book invalid/stale/thin")
            return None
        async with self.trade_lock:
            alloc = min(allocation_usd, self.capital)
            if alloc < 0.01:
                return None
            self.capital -= alloc
            self.pending_makers.append({
                "strike": strike, "price": price, "allocation_usd": alloc,
                "end_ts": end_ts, "event_title": event_title,
                "entry_reason": entry_reason, "edge": edge,
                "yes_tid": yes_tid, "side": side,
                "queued_at": time.time(),
                "fill_after": time.time() + random.uniform(MAKER_FILL_DELAY_MIN, MAKER_FILL_DELAY_MAX),
            })
            self._add_log(
                f"QUEUE MAKER | {event_title or f'${strike:,}'} @ {price:.4f} | "
                f"${alloc:.2f} | {entry_reason}")
            return True

    async def process_pending_makers(self, get_book=None):
        """
        get_book: optional callable(token_id) -> Book | None
        At fill time, re-validate that the market price hasn't moved 
        far from the queued price (prevents stale-book fills).
        """
        async with self.trade_lock:
            now = time.time()
            remaining = []
            for order in self.pending_makers:
                if now >= order["fill_after"]:
                    queued_price = order["price"]
                    # Re-check book at fill time if we have a feed reference
                    if get_book is not None:
                        yes_tid = order.get("yes_tid")
                        bk = get_book(yes_tid) if yes_tid else None
                        if bk is not None:
                            target_side = order.get("side", "YES")
                            if target_side == "YES": current_bid = bk.bb()
                            else: current_bid = 1.0 - bk.ba() if bk.ba() else None
                            
                            if current_bid and current_bid > 0:
                                # If market bid is >20% higher than our queue price,
                                # we would never have been filled — cancel
                                slip = (current_bid - queued_price) / queued_price
                                if slip > MAKER_MAX_SLIP:
                                    self.capital += order["allocation_usd"]  # REFUND
                                    self._add_log(
                                        f"MAKER CANCEL | ${order['strike']:,} | "
                                        f"market moved: queued={queued_price:.4f} "
                                        f"current_bid={current_bid:.4f} "
                                        f"slip={slip*100:.1f}% > {MAKER_MAX_SLIP*100:.0f}%")
                                    continue  # drop order, don't re-queue
                            # Also re-validate book freshness/price floor
                            if not self.book_is_valid(bk):
                                self.capital += order["allocation_usd"]  # REFUND
                                self._add_log(
                                    f"MAKER CANCEL | ${order['strike']:,} | "
                                    f"book invalid at fill time")
                                continue

                    if random.random() < MAKER_FILL_PROBABILITY:
                        self._fill_maker(order)
                    else:
                        self.capital += order["allocation_usd"]  # REFUND
                        self._add_log(f"MAKER MISS | ${order['strike']:,} (probability)")
                else:
                    remaining.append(order)
            self.pending_makers = remaining

    def _fill_maker(self, order):
        price = order["price"]
        alloc = order["allocation_usd"]
        tokens = alloc / price
        # self.capital -= alloc  <-- Removed: Already deducted in submit_maker
        pos = Position(
            id=str(uuid.uuid4())[:8],
            strike=order["strike"], side=order.get("side", "YES"), order_type="MAKER_POST_ONLY",
            entry_price=price, tokens=tokens, cost_usd=alloc,
            end_ts=order["end_ts"], opened_at=time.time(),
            event_title=order.get("event_title", ""),
            entry_reason=order.get("entry_reason", ""),
            edge_at_entry=order.get("edge", 0.0))
        self.positions.append(pos)
        self._add_log(
            f"BUY MAKER | {pos.event_title or f'${pos.strike:,}'} | "
            f"{tokens:.1f}tok @ {price:.4f} | ${alloc:.2f}")
        self._csv_open(pos)

    # --- Position Exit ---
    async def exit_position(self, pos, exit_price, reason="Smart TP"):
        async with self.trade_lock:
            if pos.status != "OPEN":
                return 0.0
            pos.status = "CLOSED"
            proceeds = pos.tokens * exit_price
            pnl = proceeds - pos.cost_usd
            roi = (pnl / pos.cost_usd * 100) if pos.cost_usd > 0 else 0
            pos._pnl = pnl
            self.capital += proceeds
            runtime = (time.time() - pos.opened_at) / 60
            self._add_log(
                f"CLOSE | {pos.event_title or f'${pos.strike:,}'} | "
                f"{pos.tokens:.1f}tok @ {exit_price:.4f} | "
                f"PnL=${pnl:+.2f} ({roi:+.1f}%) | {reason} | {runtime:.1f}m")
            self._csv_close(pos, exit_price, pnl, roi, reason)
            return pnl

    async def resolve_position(self, pos, eth_price):
        if pos.status != "OPEN":
            return 0.0
        won = eth_price > pos.strike if pos.side == "YES" else eth_price <= pos.strike
        exit_price = 1.0 if won else 0.0
        result = f"EXPIRY_{'WIN' if won else 'LOSS'}"
        pos.status = "CLOSED"
        proceeds = pos.tokens * exit_price
        pnl = proceeds - pos.cost_usd
        roi = (pnl / pos.cost_usd * 100) if pos.cost_usd > 0 else 0
        pos._pnl = pnl
        self.capital += proceeds
        runtime = (time.time() - pos.opened_at) / 60
        self._add_log(
            f"RESOLVED {'WON' if won else 'LOST'} | "
            f"{pos.event_title or f'${pos.strike:,}'} | "
            f"PnL=${pnl:+.2f} ({roi:+.1f}%) | ETH=${eth_price:,.0f}")
        self._csv_close(pos, exit_price, pnl, roi, result)
        return pnl

    def _csv_open(self, pos):
        try:
            with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([
                    datetime.now(timezone.utc).isoformat(),
                    "OPEN", pos.id, pos.event_title,
                    pos.strike, pos.side, pos.order_type,
                    f"{pos.tokens:.2f}", f"{pos.entry_price:.4f}", "",
                    f"{pos.cost_usd:.2f}", "", "",
                    f"{pos.edge_at_entry*100:.2f}",
                    pos.entry_reason, "", ""
                ])
        except Exception as e:
            log.error("CSV open: %s", e)

    def _csv_close(self, pos, exit_price, pnl, roi, reason):
        try:
            runtime_min = round((time.time() - pos.opened_at) / 60, 1)
            with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([
                    datetime.now(timezone.utc).isoformat(),
                    "CLOSE", pos.id, pos.event_title,
                    pos.strike, pos.side, pos.order_type,
                    f"{pos.tokens:.2f}", f"{pos.entry_price:.4f}",
                    f"{exit_price:.4f}",
                    f"{pos.cost_usd:.2f}", f"{pnl:.4f}", f"{roi:.2f}",
                    f"{pos.edge_at_entry*100:.2f}",
                    pos.entry_reason, reason, runtime_min
                ])
        except Exception as e:
            log.error("CSV close: %s", e)
