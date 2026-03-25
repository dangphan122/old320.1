"""
config.py - Centralised risk constants for the Quant Arb Engine.

3D Risk Matrix dimensions:
  Dim 1: MAX_GLOBAL_EXPOSURE    â€” total portfolio heat ceiling
  Dim 2: MAX_EXPOSURE_PER_DATE  â€” correlation defence per expiry date
  Dim 3: KELLY_FRACTION         â€” fractional Kelly de-leveraging
"""

# â”€â”€ Dimension 1: Global Safety â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
MAX_GLOBAL_EXPOSURE   = 0.30   # 30% of initial capital max in-market at once

# â”€â”€ Dimension 2: Time-Correlation Defence â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
MAX_EXPOSURE_PER_DATE = 0.15   # 15% max across all positions sharing same expiry date

# â”€â”€ Dimension 3: Fractional Kelly Multiplier â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
KELLY_FRACTION        = 0.25   # use 25% of raw Kelly output (conservative half-Kelly)

# â”€â”€ Probability-Tiered Position Caps â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Applied AFTER Kelly fraction, BEFORE portfolio gates
TIER_HIGH_PROB_THRESH    = 0.80   # p_real > 80%  â†’ deep ITM, high confidence
TIER_STANDARD_PROB_THRESH = 0.40  # p_real > 40%  â†’ standard
TIER_HIGH_CAP            = 0.050  # 5.0% of initial capital
TIER_STANDARD_CAP        = 0.030  # 3.0% of initial capital
TIER_LOTTO_CAP           = 0.015  # 1.5% of initial capital (deep OTM)

# â”€â”€ Dust Threshold â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
MIN_EXECUTION_WEIGHT = 0.001   # skip if final weight < 0.5% (not worth it)


# â”€â”€ Unified Defense Architecture â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

# Layer 1: VRP Discount
# Deribit IV includes a Volatility Risk Premium (IV > RV historically).
# Discount IV before passing into N(d2) to avoid overstating tail probabilities.
VRP_DISCOUNT = 0.97            # 3% IV haircut â€” tuned for current market

# Layer 2A: Volatility Circuit Breaker
# Freeze ALL new entries when 15-min realized vol is too high (momentum regime).
# Measured as std-dev of log-returns over last 20 price ticks (~15min at 5s loop).
MOMENTUM_THRESHOLD = 0.008     # 0.8% realized vol per tick window â€” tune up/down

# Layer 2B: Directional Veto
# If ETH has moved more than TREND_VETO_PCT in the last hour, block contrarian entries.
TREND_VETO_PCT = 0.03          # 3% 1-hour price drift triggers the veto

# Layer 3: Spread-Adjusted Edge Floor
# actual_edge must exceed this multiple of the spread to justify entry.
EDGE_SPREAD_MULTIPLIER = 1.15   # edge must beat half the spread cost
# â”€â”€ Other risk params (kept here for single source of truth) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
MIN_TRADE_USD          = 0.5
MAX_PRICE_DEVIATION    = 0.50
MIN_ASK_LIQUIDITY_USD  = 4.0
MODEL_BUFFER           = 0.02
TIME_DISCOUNT_RATE     = 0.01




