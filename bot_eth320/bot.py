"""
bot.py - The Orchestrator.
Connects feeds, quant engine, paper trader, and dashboard.

Risk Rules (from Master Quant Architecture):
  Rule 1: 30% max exposure
  Rule 2: Max 3 concurrent positions
  Rule 3: $0.50 minimum trade size (optimized for $200 capital)
  Rule 4: asyncio.Lock for sequential trade execution
"""

import atexit
import asyncio
import logging
import os
import sys
import time
import threading
from collections import defaultdict
from datetime import datetime, timezone

from feeds import DeribitOracle, PolymarketFeed
from quant_engine import (
    calculate_nd2, build_state_probabilities, build_returns_matrix,
    frank_wolfe_optimizer, evaluate_execution, evaluate_exit, _ok,
    get_position_cap,
)
from config import (
    MAX_GLOBAL_EXPOSURE, MAX_EXPOSURE_PER_DATE, KELLY_FRACTION,
    MIN_EXECUTION_WEIGHT, MIN_TRADE_USD as CFG_MIN_TRADE,
    MAX_PRICE_DEVIATION as CFG_PRICE_DEV,
    MIN_ASK_LIQUIDITY_USD as CFG_MIN_LIQ,
    # Unified Defense Architecture
    VRP_DISCOUNT, MOMENTUM_THRESHOLD, TREND_VETO_PCT, EDGE_SPREAD_MULTIPLIER,
)
from paper_trader import PaperTrader
from dashboard import create_app

# Live Execution Engine (graceful degradation â€” paper-trades if not configured)
try:
    from live_trader import LiveExecutor, PRAGMATIC_MIN_ORDER as _LIVE_MIN
    _LIVE_KEY = __import__("os").environ.get("POLY_PRIVATE_KEY", "")
    _LIVE_FUNDER = __import__("os").environ.get("POLY_FUNDER_ADDRESS", None)
    LIVE_MODE = bool(_LIVE_KEY)
except ImportError:
    LIVE_MODE = False
    _LIVE_MIN = 5.0

# ===================== CONFIGURATION =================================
MODEL_BUFFER = 0.02          # 2% IV/drift tolerance
TIME_DISCOUNT_RATE = 0.01   # 1% EV/day for Smart TP
STRATEGY_LOOP_SEC = 5
ORACLE_POLL_SEC = 5
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 5557

# Risk Management Rules â€” values sourced from config.py (3D Risk Matrix)
# MAX_GLOBAL_EXPOSURE    = 0.30  (from config)
# MAX_EXPOSURE_PER_DATE  = 0.15  (from config)
# KELLY_FRACTION         = 0.25  (from config)
# Probability-tiered caps: 5% / 3% / 1.5% via get_position_cap()
MIN_TRADE_USD         = CFG_MIN_TRADE     # $0.50 minimum (optimized for $200 cap)
MAX_PRICE_DEVIATION   = CFG_PRICE_DEV     # 35c stale-book guard
MIN_ASK_LIQUIDITY_USD = CFG_MIN_LIQ       # $4 minimum ask depth (scaled 1:5 from $20 @ $1000 cap)

# ===================== LOGGING =======================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")

# ===================== THE ORCHESTRATOR ==============================

class TradingBot:
    def __init__(self):
        self.feed = PolymarketFeed()
        self.oracle = DeribitOracle()
        self.trader = PaperTrader()
        self.trade_lock = self.trader.trade_lock  # Rule 4
        self.status = "INIT"
        self.start_time = time.time()

        # Live execution engine (None = paper-trade mode)
        if LIVE_MODE:
            self.executor = LiveExecutor(
                host="https://clob.polymarket.com",
                key=_LIVE_KEY,
                funder=_LIVE_FUNDER,
            )
            log.info("LIVE MODE ACTIVE â€” real orders will be placed on Polymarket")
        else:
            self.executor = None
            log.info("PAPER MODE â€” set POLY_PRIVATE_KEY env var to enable live trading")

    async def run(self):
        log.info("=" * 60)
        log.info("  QUANT ARB ENGINE â€” Paper Trading v2")
        log.info("  Capital: $%.2f | Buffer: %.1f%% | Discount: %.1f%%/day",
                 self.trader.initial_capital, MODEL_BUFFER*100, TIME_DISCOUNT_RATE*100)
        log.info("  Risk: %.0f%% global | %.0f%% per-date | Kelly=%.0f%% | $%.0f min",
                 MAX_GLOBAL_EXPOSURE*100, MAX_EXPOSURE_PER_DATE*100,
                 KELLY_FRACTION*100, MIN_TRADE_USD)
        log.info("=" * 60)

        log.info("Starting feeds and engines...")

        # Start dashboard in daemon thread
        app = create_app(self)
        dash_thread = threading.Thread(
            target=lambda: app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT,
                                   debug=False, use_reloader=False),
            daemon=True)
        dash_thread.start()
        log.info("Dashboard: http://%s:%d", DASHBOARD_HOST, DASHBOARD_PORT)

        self.status = "RUNNING"

        # Run all async loops
        tasks = [
            self.feed.run(),
            self.oracle.run(poll_sec=ORACLE_POLL_SEC),
            self._strategy_loop(),
            self._maker_loop(),
        ]
        if self.executor:
            tasks.append(self.executor.garbage_collector())  # Ghost Equity GC
        await asyncio.gather(*tasks)

    # === STRATEGY LOOP ===============================================

    async def _strategy_loop(self):
        await asyncio.sleep(12)  # let feeds warm up
        log.info("Strategy loop starting (every %ds)", STRATEGY_LOOP_SEC)

        while True:
            try:
                # Dynamically update oracle targets based on latest discovered brackets
                targets = defaultdict(set)
                for br in self.feed.brackets:
                    targets[br.end_ts].add(br.strike)
                self.oracle.update_targets(dict(targets))

                await self._resolve_expired()
                await self._evaluate()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Strategy tick failed")
            await asyncio.sleep(STRATEGY_LOOP_SEC)

    async def _resolve_expired(self):
        """Resolve positions whose bracket has expired."""
        now = time.time()
        eth = self.feed.get_eth_price()
        if not eth:
            return
        for pos in list(self.trader.open_positions()):
            if pos.end_ts > 0 and pos.end_ts < now:
                await self.trader.resolve_position(pos, eth)

    async def _evaluate(self):
        """Main evaluation: Risk checks -> Per-Bracket IV -> Quant Engine -> Execute.

        STRICT PER-BRACKET T_YEARS AND IV:
          t_years = (br.end_ts - now) / SECS            (this bracket's exact time)
          iv      = oracle.get_iv_for_date(target_dt, strike) (this bracket's Deribit IV)
          p_real  = calculate_nd2(spot, strike, t_years, iv)  (this bracket's probability)
        """
        spot = self.oracle.spot_price
        if not spot or spot <= 0:
            self.trader._add_log("DIAG | _evaluate returned: spot=0 or None")
            return

        brackets = self.feed.get_brackets()
        if not brackets:
            self.trader._add_log("DIAG | _evaluate returned: no brackets")
            return

        now_ts = datetime.now(timezone.utc).timestamp()

        # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
        # LAYER 2 â€” MARKET REGIME SHIELD
        # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
        eth_hist = list(self.feed.eth_history)  # deque of {"p": price, "t": ts}

        # Layer 2A â€” Volatility Circuit Breaker (15-min chaos)
        if len(eth_hist) >= 5:
            import math
            prices_rv = [h["p"] for h in eth_hist[-20:]]
            if len(prices_rv) >= 2:
                log_rets = [math.log(prices_rv[i] / prices_rv[i-1])
                            for i in range(1, len(prices_rv))
                            if prices_rv[i-1] > 0]
                if log_rets:
                    mean_r = sum(log_rets) / len(log_rets)
                    rv = (sum((r - mean_r)**2 for r in log_rets) / len(log_rets)) ** 0.5
                    if rv > MOMENTUM_THRESHOLD:
                        self.trader._add_log(f"REGIME SHIELD | Vol Circuit Breaker rv={rv:.4f} > {MOMENTUM_THRESHOLD} | ALL ENTRIES FROZEN")
                        log.info("REGIME SHIELD | Vol Circuit Breaker ACTIVE rv=%.4f > %.4f | Freezing entries", rv, MOMENTUM_THRESHOLD)
                        return

        # Layer 2B â€” Directional Veto (1-hour trend)
        veto_yes = False  # forbid YES entries
        veto_no  = False  # forbid NO entries
        hour_ago_ts = now_ts - 3600
        hist_1h = [h for h in eth_hist if h.get("t", 0) >= hour_ago_ts]
        if len(hist_1h) >= 2:
            p_now  = hist_1h[-1]["p"]
            p_1h   = hist_1h[0]["p"]
            drift  = (p_now - p_1h) / p_1h if p_1h > 0 else 0
            if drift > TREND_VETO_PCT:
                veto_no = True   # Strong uptrend â€” forbid betting ETH stays below
                log.info("REGIME SHIELD | Uptrend Veto drift=+%.2f%% | NO entries blocked", drift*100)
            elif drift < -TREND_VETO_PCT:
                veto_yes = True  # Strong downtrend â€” forbid betting ETH ends above
                log.info("REGIME SHIELD | Downtrend Veto drift=%.2f%% | YES entries blocked", drift*100)
        # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

        # current bids for mark-to-market
        current_bids = {}
        for br in brackets:
            bk = self.feed.get_book(br.yes_tid)
            if bk and bk.bb() and bk.ba():
                current_bids[(br.strike, br.end_ts, "YES")] = bk.bb()
                current_bids[(br.strike, br.end_ts, "NO")] = 1.0 - bk.ba()

        # RULE 1: Global Exposure Check (Dimension 1)
        open_value    = self.trader.open_positions_value(current_bids)
        pending_value = self.trader.pending_makers_value()
        if (open_value + pending_value) / self.trader.initial_capital >= MAX_GLOBAL_EXPOSURE:
            self.trader._add_log("DIAG | _evaluate returned: global exposure gate hit")
            return

        equity = self.trader.total_equity(current_bids)

        # PASS 1: compute t_years, iv, p_real INDIVIDUALLY per bracket
        bd = {}  # (strike, end_ts) -> {p_real, iv, t_years, exp_code, br}
        for br in brackets:
            if br.end_ts <= now_ts:
                continue
            t_years = (br.end_ts - now_ts) / 31_536_000.0
            if t_years <= 0:
                continue
            target_dt = datetime.fromtimestamp(br.end_ts, tz=timezone.utc)
            iv, exp_code = self.oracle.get_iv_for_date(target_dt, br.strike)
            if iv is None or iv <= 0:
                continue
            # LAYER 1: VRP Discount â€” haircut IV before N(d2) to strip the risk premium
            adjusted_iv = iv * VRP_DISCOUNT
            p_real = calculate_nd2(spot, br.strike, t_years, adjusted_iv)
            if not _ok(p_real):
                continue
            bd[(br.strike, br.end_ts)] = dict(
                p_real=p_real, iv=iv, adjusted_iv=adjusted_iv,
                t_years=t_years, exp_code=exp_code, br=br)

        if not bd:
            self.trader._add_log(f"DIAG | _evaluate returned: bd empty (all {len(brackets)} brackets failed IV/p_real check)")
            return

        # PASS 2: group by end_ts for Frank-Wolfe
        groups = defaultdict(list)
        for (strike, end_ts), info in bd.items():
            groups[end_ts].append((strike, info))

        for end_ts, items in groups.items():
            strike_probs = {}
            entry_prices = {}
            sides        = {}
            info_map     = {}

            for strike, info in items:
                br  = info["br"]
                ybk = self.feed.get_book(br.yes_tid)
                if not ybk: continue

                best_bid = ybk.bb()
                best_ask = ybk.ba()
                if not best_bid or not best_ask or best_bid <= 0 or best_ask <= 0: continue

                p_real = info["p_real"]

                # TRUE INDEPENDENT SPREAD-ADJUSTED EDGES
                # YES: cost = ask_yes  (buy YES at the offer)
                yes_ask  = best_ask
                yes_edge = p_real - yes_ask

                # NO: cost = 1 - bid_yes  (i.e. what you pay including the spread)
                # Using 1 - bid_yes is correct; using 1 - ask_yes would ignore spread cost
                no_ask   = 1.0 - best_bid
                no_p_real = 1.0 - p_real
                no_edge  = no_p_real - no_ask

                # Price ceiling check + LAYER 2B Directional Veto
                spread     = max(0.0, best_ask - best_bid)
                edge_floor = EDGE_SPREAD_MULTIPLIER * spread  # Layer 3

                valid_yes = yes_ask <= 0.45 and not veto_yes
                valid_no  = no_ask  <= 0.45 and not veto_no

                # LAYER 3: Spread-Adjusted Edge Floor â€” edge must exceed multiplier*spread
                valid_yes = valid_yes and yes_edge > edge_floor
                valid_no  = valid_no  and no_edge  > edge_floor

                # Per-bracket diagnostic (temporary)
                if not valid_yes and not valid_no:
                    reasons = []
                    if yes_ask > 0.95: reasons.append(f"yes_ask={yes_ask:.3f}>0.95")
                    if no_ask  > 0.95: reasons.append(f"no_ask={no_ask:.3f}>0.95")
                    if veto_yes: reasons.append("veto_YES")
                    if veto_no:  reasons.append("veto_NO")
                    if yes_edge <= edge_floor: reasons.append(f"ye={yes_edge:.4f}<=floor={edge_floor:.4f}")
                    if no_edge  <= edge_floor: reasons.append(f"ne={no_edge:.4f}<=floor={edge_floor:.4f}")
                    self.trader._add_log(f"SKIP K=${strike:,} | " + " | ".join(reasons))

                # Choose the side with the better independent spread-adjusted edge
                if valid_yes and valid_no:
                    if no_edge > yes_edge:
                        target_side, target_price, actual_edge = "NO",  no_ask,  no_edge
                    else:
                        target_side, target_price, actual_edge = "YES", yes_ask, yes_edge
                elif valid_yes:
                    target_side, target_price, actual_edge = "YES", yes_ask, yes_edge
                elif valid_no:
                    target_side, target_price, actual_edge = "NO",  no_ask,  no_edge
                else:
                    continue

                strike_probs[strike] = p_real
                entry_prices[strike] = target_price
                sides[strike]        = target_side
                info["actual_edge"]  = actual_edge   # carry true edge into logging
                info_map[strike]     = info

            if not strike_probs:
                self.trader._add_log(f"DIAG | end_ts group: all {len(items)} brackets rejected by edge/ceiling filters")
                continue

            try:
                p_states, _  = build_state_probabilities(strike_probs)
                strikes_list = sorted(strike_probs.keys())
                R            = build_returns_matrix(strikes_list, entry_prices, sides)
                weights      = frank_wolfe_optimizer(p_states, R)
            except Exception as e:
                log.warning("Optimizer: %s", e)
                continue

            for i, strike in enumerate(strikes_list):
                weight = weights[i] if i < len(weights) else 0
                if weight < 0.001:
                    continue

                # Skip if already holding OR pending for this strike+expiry
                # FIX: Check ONLY currently open positions to allow capital recycling/re-entry
                already_open = any(
                    p.strike == strike and p.end_ts == end_ts
                    for p in self.trader.open_positions()
                )
                already_pending = any(
                    o["strike"] == strike and o["end_ts"] == end_ts
                    for o in self.trader.pending_makers
                )
                if already_open or already_pending:
                    continue

                info   = info_map.get(strike)
                if not info: continue
                p_real = info["p_real"]
                iv     = info["iv"]
                br     = info["br"]

                ybk = self.feed.get_book(br.yes_tid)
                if not ybk: continue
                best_bid = ybk.bb()
                best_ask = ybk.ba()
                ask_size = ybk.ba_size()
                bid_size = ybk.bb_size()

                action, target_price, avail_size, side = evaluate_execution(
                    p_real, best_bid, best_ask, MODEL_BUFFER, ask_size, bid_size, sides[strike])
                if action == "SKIP_TRADE":
                    continue

                p_real_eval = p_real if side == "YES" else 1.0 - p_real
                if abs(target_price - p_real_eval) > MAX_PRICE_DEVIATION:
                    self.trader._add_log(
                        f"SKIP | ${strike:,} | STALE BOOK "
                        f"price={target_price:.4f} p_real={p_real_eval:.4f} "
                        f"diff={abs(target_price-p_real_eval):.3f}")
                    continue

                if action == "TAKER_FAK":
                    ask_liq = (ask_size or 0) * (best_ask or 0)
                    if ask_liq < MIN_ASK_LIQUIDITY_USD:
                        self.trader._add_log(
                            f"SKIP | ${strike:,} | THIN BOOK depth=${ask_liq:.1f}")
                        continue

                # â”€â”€ 3D RISK MATRIX SIZING â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                initial_cap = self.trader.initial_capital

                # Step A: Fractional Kelly de-leveraging
                adjusted_kelly = weight * KELLY_FRACTION

                # Step B: Probability-tiered cap
                tier_cap      = get_position_cap(p_real)
                target_weight = min(adjusted_kelly, tier_cap)

                # Step C: Portfolio gates
                # Gate 1 â€” global exposure remaining
                current_global = (open_value + pending_value) / initial_cap
                allowed_global = max(0.0, MAX_GLOBAL_EXPOSURE - current_global)

                # Gate 2 â€” per-date correlation defence
                target_date = datetime.fromtimestamp(end_ts, tz=timezone.utc).date()
                date_value  = sum(
                    pos.cost_usd for pos in self.trader.open_positions()
                    if datetime.fromtimestamp(pos.end_ts, tz=timezone.utc).date() == target_date
                ) + sum(
                    o["allocation_usd"] for o in self.trader.pending_makers
                    if datetime.fromtimestamp(o["end_ts"], tz=timezone.utc).date() == target_date
                )
                current_date = date_value / initial_cap
                allowed_date = max(0.0, MAX_EXPOSURE_PER_DATE - current_date)

                # Final clamped weight
                final_weight = min(target_weight, allowed_global, allowed_date)

                if final_weight <= MIN_EXECUTION_WEIGHT:
                    continue   # dust â€” not worth the API call

                allocation_usd = equity * final_weight

                # Hard liquidity cap for takers
                if action == "TAKER_FAK" and best_ask:
                    allocation_usd = min(allocation_usd, ask_size * best_ask)

                # â”€â”€ Pragmatic floor: $0.50 min (gas efficiency on Polygon) â”€â”€
                min_order = _LIVE_MIN if LIVE_MODE else MIN_TRADE_USD
                if allocation_usd < min_order:
                    continue
                # â”€â”€ END 3D RISK MATRIX â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

                # Use the TRUE spread-adjusted edge for the chosen side, not a YES-only proxy
                side  = sides[strike]
                edge  = info.get("actual_edge", (p_real - best_ask) if best_ask else 0)
                adj_iv = info.get("adjusted_iv", iv)
                self.trader._add_log(
                    f"{'ðŸŸ¢ LIVE' if LIVE_MODE else 'ðŸ“ PAPER'} SIGNAL | ${strike:,} | {side} edge={edge:.4f} | "
                    f"kelly={weight:.3f}â†’{final_weight:.3f} | "
                    f"${allocation_usd:.2f} | {action} | "
                    f"T={info['t_years']*365:.1f}d iv={iv*100:.1f}%â†’{adj_iv*100:.1f}%(adj) "
                    f"[{info['exp_code']}] tier={tier_cap*100:.1f}%")

                entry_reason = (f"{side} edge={edge*100:.1f}% kelly={weight:.3f}â†’{final_weight:.3f} "
                                f"iv_raw={iv*100:.1f}% iv_adj={adj_iv*100:.1f}% T={info['t_years']*365:.1f}d "
                                f"deribit={info['exp_code']} tier={tier_cap*100:.1f}%")

                if LIVE_MODE and self.executor:
                    # â”€â”€ LIVE EXECUTION: route through LiveExecutor â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                    # LiveExecutor handles: FOK vs GTC L2 routing, GC registration
                    token_id = br.yes_tid if side == "YES" else br.no_tid
                    await self.executor.execute(
                        token_id=token_id, strike=strike, side=side,
                        limit_price=target_price, allocation_usd=allocation_usd,
                        book=ybk, event_title=br.event_title)
                else:
                    # â”€â”€ PAPER TRADING: simulate fills â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                    if action == "TAKER_FAK":
                        await self.trader.submit_taker(
                            strike, target_price, allocation_usd, end_ts,
                            event_title=br.event_title, entry_reason=entry_reason,
                            edge=edge, book=ybk, ask_size=avail_size, side=side)
                    elif action == "MAKER_POST_ONLY":
                        await self.trader.submit_maker(
                            strike, target_price, allocation_usd, end_ts,
                            event_title=br.event_title, entry_reason=entry_reason,
                            edge=edge, book=ybk, yes_tid=br.yes_tid, side=side)

        # --- Phase 4: Lifecycle (Smart TP) ---
        now = datetime.now(timezone.utc)
        for pos in self.trader.open_positions():
            # Match bracket by strike + end_ts
            br = next((b for b in brackets
                       if b.strike == pos.strike
                       and abs(b.end_ts - pos.end_ts) < 60), None)
            if not br:
                continue

            # Per-bracket p_real for Smart TP
            target_dt = datetime.fromtimestamp(br.end_ts, tz=timezone.utc)
            now_ts_tp = datetime.now(timezone.utc).timestamp()
            t_years_tp = max(0, (br.end_ts - now_ts_tp) / 31_536_000.0)
            iv_tp, _ = self.oracle.get_iv_for_date(target_dt, pos.strike)
            if iv_tp is None or iv_tp <= 0:
                continue
            spot_tp = self.oracle.spot_price
            if not spot_tp or spot_tp <= 0:
                continue
            p_real = calculate_nd2(spot_tp, pos.strike, t_years_tp, iv_tp)
            if not _ok(p_real):
                continue
            end_dt = datetime.fromtimestamp(br.end_ts, tz=timezone.utc)
            days_remaining = max(0, (end_dt - now).total_seconds() / 86400)

            ybk = self.feed.get_book(br.yes_tid)
            if not ybk:
                continue
            
            if pos.side == "YES":
                best_bid = ybk.bb()
                bid_size = ybk.bb_size()
                p_real_eval = p_real
            else:
                best_bid = 1.0 - ybk.ba() if ybk.ba() else None
                bid_size = ybk.ba_size()
                p_real_eval = 1.0 - p_real

            exit_action, optimal_tp, reason = evaluate_exit(
                p_real_eval, best_bid, bid_size,
                pos.tokens, days_remaining,
                pos.entry_price, TIME_DISCOUNT_RATE)

            if exit_action == "MARKET_SELL" and best_bid:
                await self.trader.exit_position(pos, best_bid, f"TP:{reason}")

    # === MAKER LOOP ==================================================

    async def _maker_loop(self):
        while True:
            try:
                await self.trader.process_pending_makers(
                    get_book=self.feed.get_book)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Maker loop error")
            await asyncio.sleep(1)

    # === SHUTDOWN =====================================================

    def stop(self):
        if self.status == "STOPPED":
            return
        self.feed.stop()
        self.oracle.stop()
        self.status = "STOPPED"
        log.info("Bot stopped.")


# ===================== ANTI-ZOMBIE ENTRY POINT =======================

_bot = None

def _on_exit():
    """Guaranteed cleanup: stop bot + force kill Flask thread."""
    global _bot
    if _bot and _bot.status != "STOPPED":
        _bot.stop()
    print("\nBot stopped cleanly.")
    os._exit(0)

atexit.register(_on_exit)


async def main():
    global _bot
    _bot = TradingBot()
    try:
        await _bot.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if _bot.status != "STOPPED":
            _bot.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _on_exit()





