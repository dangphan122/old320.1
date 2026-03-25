"""
quant_engine.py - Pure stateless math module.
4-Phase Pipeline from The Master Quant Architecture.

Phase 1: Black-Scholes N(d2) probability extraction
Phase 2: Frank-Wolfe Kelly portfolio optimisation (Cash Asset)
Phase 3: Hybrid execution (Taker / Maker / Skip)
Phase 4: Smart TP lifecycle management
"""

import math
import numpy as np
from scipy.stats import norm


# =====================================================================
# PHASE 1 - Probability Extraction
# =====================================================================

def calculate_nd2(spot, strike, t_years, iv, r=0.0):
    """Extract risk-neutral probability P(S > K) = N(d2)."""
    if not _ok(spot) or not _ok(strike) or not _ok(iv) or not _ok(t_years):
        return float("nan")
    if t_years <= 0 or iv <= 0:
        return 1.0 if spot >= strike else 0.0
    if strike <= 0 or spot <= 0:
        return float("nan")
    sqrt_t = np.sqrt(t_years)
    d1 = (np.log(spot / strike) + (r + (iv ** 2) / 2) * t_years) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    return float(norm.cdf(d2))


def _ok(v):
    """Check value is not None/NaN."""
    if v is None:
        return False
    try:
        return not math.isnan(float(v))
    except (ValueError, TypeError):
        return False


def get_position_cap(p_real: float) -> float:
    """
    Probability-tiered position cap (Dimension 3 of 3D Risk Matrix).

    Returns the maximum fraction of initial_capital allowed for this
    position, based on the model's win probability:
      p_real > 0.80  → deep ITM, high conviction  → 5.0% cap
      p_real > 0.40  → standard / mid-range       → 3.0% cap
      else           → lotto / deep OTM            → 1.5% cap
    """
    if p_real > 0.80:
        return 0.050   # High Confidence: Max 5%
    elif p_real > 0.40:
        return 0.030   # Standard: Max 3%
    else:
        return 0.015   # Lotto/High-Variance: Max 1.5%


# =====================================================================
# PHASE 2 - Frank-Wolfe Kelly Optimisation
# =====================================================================

def build_state_probabilities(strike_probs):
    """Build discrete state probability vector. N strikes -> N+1 states."""
    strikes = sorted(strike_probs.keys())
    if not strikes:
        return np.array([1.0]), ["empty"]
    n = len(strikes)
    probs, labels = [], []
    probs.append(1.0 - strike_probs[strikes[0]])
    labels.append(f"S<{strikes[0]}")
    for i in range(1, n):
        p = strike_probs[strikes[i - 1]] - strike_probs[strikes[i]]
        probs.append(max(p, 1e-9))
        labels.append(f"{strikes[i-1]}<S<{strikes[i]}")
    probs.append(strike_probs[strikes[-1]])
    labels.append(f"S>{strikes[-1]}")
    p_states = np.array(probs, dtype=np.float64)
    p_states = np.clip(p_states, 1e-9, None)
    p_states /= p_states.sum()
    return p_states, labels


def build_returns_matrix(strikes, entry_prices, sides=None):
    """R[state, asset]: Win = (1/price)-1, Lose = -1. YES wins when S>K."""
    if sides is None: sides = {}
    ss = sorted(strikes)
    n_st, n_as = len(ss) + 1, len(ss)
    R = np.full((n_st, n_as), -1.0, dtype=np.float64)
    for j, k in enumerate(ss):
        p = entry_prices.get(k, 0.5)
        if p <= 0: p = 0.01
        wr = (1.0 / p) - 1.0
        if sides.get(k, "YES") == "YES":
            for s in range(j + 1, n_st):
                R[s, j] = wr
        else:
            for s in range(0, j + 1):
                R[s, j] = wr
    return R


def frank_wolfe_optimizer(p_states, R_matrix, max_iter=5000, tol=1e-6):
    """Frank-Wolfe Kelly with Cash Asset. Returns half-Kelly weights."""
    ns, na = R_matrix.shape
    R_ext = np.hstack([R_matrix, np.zeros((ns, 1))])
    w = np.zeros(na + 1)
    w[-1] = 1.0
    for k in range(max_iter):
        wealth = np.clip(1.0 + R_ext @ w, 1e-12, None)
        grad = (p_states / wealth) @ R_ext
        best = int(np.argmax(grad))
        if np.max(grad) - grad @ w < tol:
            break
        s = np.zeros(na + 1)
        s[best] = 1.0
        gamma = 2.0 / (k + 2.0)
        w = w + gamma * (s - w)
    return w[:-1] * 0.5


# =====================================================================
# PHASE 3 - Hybrid Execution Logic
# =====================================================================

MAX_ENTRY_PRICE = 0.45   # raised from 0.40; near-saturated books still blocked

def evaluate_execution(p_real, best_bid, best_ask, model_buffer=0.02, ask_size=0.0, bid_size=0.0, target_side="YES"):
    """Evaluates execution ONLY for the pre-selected target_side."""
    if not _ok(p_real) or not _ok(best_bid) or not _ok(best_ask):
        return ("SKIP_TRADE", 0.0, 0.0, target_side)
    if best_ask <= 0 or best_bid <= 0 or best_ask > 1.0 or best_bid > 1.0:
        return ("SKIP_TRADE", 0.0, 0.0, target_side)
    
    spread = max(0.0, best_ask - best_bid)
    dyn_thresh = (spread * 1.15) + model_buffer  # Applied 1.15 multiplier

    if target_side == "YES":
        ask, bid, size = best_ask, best_bid, ask_size
        edge = p_real - ask
        maker_p = bid + 0.001
        maker_e = p_real - maker_p
    else:
        ask, bid, size = 1.0 - best_bid, 1.0 - best_ask, bid_size
        edge = (1.0 - p_real) - ask
        maker_p = bid + 0.001
        maker_e = (1.0 - p_real) - maker_p

    if edge >= dyn_thresh:
        return ("TAKER_FAK", ask, size, target_side)
    elif maker_e >= model_buffer:
        return ("MAKER_POST_ONLY", maker_p, 0.0, target_side)

    return ("SKIP_TRADE", 0.0, 0.0, target_side)


# =====================================================================
# PHASE 4 - Smart Take-Profit
# =====================================================================

CAPITAL_RECYCLING_THRESHOLD = 0.99

def evaluate_exit(p_real, best_bid, bid_size, tokens_held, days_remaining,
                  entry_price, time_discount_rate=0.01):
    """
    Returns (action, optimal_tp, reason).

    Exit pipeline (strictest priority first):
      1. Capital Recycling    — bid >= 0.99  -> sell instantly
      2. Dynamic Greed Decay  — exponential trailing TP scaled to entry price
      3. Standard Time-Decay  — p_real minus time-remaining discount
      4. Hold
    """
    import math

    optimal_tp = p_real - (days_remaining * time_discount_rate)

    # Guard: need a valid bid to do anything useful
    if not _ok(best_bid) or best_bid <= 0:
        return ("HOLD", optimal_tp, "no_bid")

    # ── 1. CAPITAL RECYCLING (always fires first) ─────────────────────────
    if best_bid >= CAPITAL_RECYCLING_THRESHOLD:
        return ("MARKET_SELL", best_bid, "CAPITAL_RECYCLING")

    # ── 2. DYNAMIC GREED DECAY TP ─────────────────────────────────────────
    #
    # dynamic_target = p_real * exp(-k * max(0, ROI))
    #
    # Dynamic k scales with entry_price:
    #   $0.033 entry  ->  k = 0.15  (slow decay, patient for 10x+)
    #   $0.10  entry  ->  k = 0.25  (medium,     locks 2-3x)
    #   $0.80  entry  ->  k = 1.30  (aggressive, secures 50% gain)
    #
    # Trigger requires: bid >= dynamic_target AND roi > 40%
    # (prevents noise selling on deep-OTM options)
    if _ok(entry_price) and entry_price > 0:
        current_roi    = (best_bid - entry_price) / entry_price
        dynamic_k      = 0.10 + (entry_price * 1.5)
        dynamic_target = p_real * math.exp(-dynamic_k * max(0.0, current_roi))

        if current_roi > 0.40 and best_bid >= dynamic_target:
            return (
                "MARKET_SELL", best_bid,
                f"DYNAMIC_GREED_TP (+{current_roi*100:.0f}% k={dynamic_k:.3f} tgt={dynamic_target:.3f})"
            )

    # ── 3. STANDARD TIME-DECAY TP ─────────────────────────────────────────
    if best_bid < entry_price:
        return ("HOLD", optimal_tp, "below_entry")
    if best_bid >= optimal_tp and bid_size >= tokens_held:
        return ("MARKET_SELL", optimal_tp, "tp_hit_liquid")
    elif best_bid >= optimal_tp:
        return ("HOLD", optimal_tp, "tp_hit_thin_book")

    # ── 4. HOLD ───────────────────────────────────────────────────────────
    return ("HOLD", optimal_tp, "below_tp")
