"""
regime_combined.py — Combined Quant + LLM Regime Layer (Options Bot)
=====================================================================
Drop into: moomoo/src/market/regime_combined.py

Merges the quantitative regime bridge (regime_bridge.py) and the LLM
vision bridge (llm_regime_bridge.py) into a single interface.

Design (mirrors regime_combined from zpair):
  - Direction: LLM preferred → quant drift fallback → no opinion
  - Volatility: always from quant (real-time)
  - Exit mandate: always from quant (real-time, immediate)
  - Range hints: from quant
  - Entry strategy reads get_state() dict

Fallback chain:
  1. LLM direction (if available + not stale + confidence ≥ min)
  2. Quant drift detection (signals.drift_direction)
  3. "BOTH" (no opinion — don't gate strategies)

Usage (from bot_scheduler.py):
    from src.market.regime_bridge import bridge_instance
    from src.market.llm_regime_bridge import LLMRegimeBridgePool
    from src.market.regime_combined import CombinedRegime

    pool = LLMRegimeBridgePool(symbols=watchlist, provider="gemini")
    combined = CombinedRegime(quant_bridge=bridge_instance, llm_pool=pool)

    # In monitor loop:
    pool.maybe_update(symbol, ohlcv_df)

    # Read state per symbol:
    state = combined.get_state(symbol)
    direction    = state["direction"]     # LONG_ONLY / SHORT_ONLY / BOTH / WAIT / NO_TRADE
    exit_mandate = state["exit_mandate"]  # from quant (real-time)
"""

import logging
from typing import Optional

try:
    from src.logger import get_logger
    logger = get_logger("market.regime_combined")
except Exception:
    logger = logging.getLogger("market.regime_combined")


class CombinedRegime:
    """
    Reads from both quant RegimeBridge and LLM LLMRegimeBridgePool.

    The quant bridge is the shared singleton (bridge_instance) from
    regime_bridge.py. The LLM pool holds one LLMRegimeBridge per symbol.

    All signals that require real-time data (volatility, exit mandate,
    structural breaks) always come from quant. Direction comes from LLM
    when available, with drift detection as fallback.
    """

    def __init__(self, quant_bridge, llm_pool=None):
        """
        Args:
            quant_bridge : RegimeBridge singleton (from regime_bridge.bridge_instance)
            llm_pool     : LLMRegimeBridgePool instance, or None if LLM not available
        """
        self.quant = quant_bridge
        self.llm   = llm_pool

    def get_state(self, symbol: str) -> dict:
        """
        Get combined regime state for a symbol.

        Args:
            symbol: MooMoo format e.g. "US.SPY"

        Returns:
            dict with keys:
              direction          — LONG_ONLY / SHORT_ONLY / BOTH / WAIT / NO_TRADE
              direction_source   — "llm" / "drift" / "none"
              consensus_state    — from quant (CHOP_NEUTRAL / BULL_PERSISTENT / etc.)
              volatility_regime  — from quant (LOW_STABLE / MODERATE / EXPANDING / CONTRACTING)
              exit_mandate       — from quant (bool, real-time)
              confidence_score   — from quant (0.0–1.0)
              recommended_logic  — from quant (OPTIONS_INCOME / NO_TRADE)
              range_hints        — from quant (dict or None)
              llm_htf_regime     — for logging (str or None)
              llm_ltf_regime     — for logging (str or None)
              llm_confidence     — for logging (int or None)
              llm_stale          — whether LLM result is stale (bool)
        """
        # ── Quant signals (always real-time) ──────────────────────────────────
        q = {}
        if self.quant is not None:
            q = self.quant.get_regime(symbol) or {}

        consensus   = q.get("consensus_state",   "UNKNOWN")
        vol_regime  = q.get("volatility_regime",  "UNKNOWN")
        exit_m      = q.get("exit_mandate",        False)
        conf        = q.get("confidence_score",    0.0)
        rec_logic   = q.get("recommended_logic",   "NO_TRADE")
        signals     = q.get("signals",             {})
        range_hints = signals.get("range_hints")
        drift_raw   = signals.get("drift_direction", "NONE")   # UP / DOWN / NONE

        # ── LLM direction (periodic, cached) ─────────────────────────────────
        llm_dir     = None
        llm_stale   = True
        llm_htf     = None
        llm_ltf     = None
        llm_conf    = None

        if self.llm is not None:
            llm_stale = self.llm.is_stale(symbol)
            if not llm_stale:
                raw_dir = self.llm.direction(symbol)
                # Only use LLM direction if it's an actual opinion
                if raw_dir not in ("BOTH",):
                    llm_dir = raw_dir
            llm_htf  = self.llm.htf(symbol)
            llm_ltf  = self.llm.ltf(symbol)
            llm_conf = llm_ltf.confidence if llm_ltf else None

            if llm_htf:
                llm_htf = llm_htf.regime
            if llm_ltf:
                llm_ltf = llm_ltf.regime

        # ── Direction: LLM → drift → no opinion ──────────────────────────────
        if llm_dir and not llm_stale:
            direction        = llm_dir
            direction_source = "llm"
        elif drift_raw == "UP":
            direction        = "LONG_ONLY"
            direction_source = "drift"
        elif drift_raw == "DOWN":
            direction        = "SHORT_ONLY"
            direction_source = "drift"
        else:
            direction        = "BOTH"
            direction_source = "none"

        return {
            # Primary direction signal
            "direction":          direction,
            "direction_source":   direction_source,

            # Quant signals (always real-time)
            "consensus_state":    consensus,
            "volatility_regime":  vol_regime,
            "exit_mandate":       exit_m,
            "confidence_score":   conf,
            "recommended_logic":  rec_logic,
            "range_hints":        range_hints,

            # LLM metadata (for logging / dashboard)
            "llm_htf_regime":     llm_htf,
            "llm_ltf_regime":     llm_ltf,
            "llm_confidence":     llm_conf,
            "llm_stale":          llm_stale,
        }

    def get_direction(self, symbol: str) -> str:
        """Convenience: just the direction string."""
        return self.get_state(symbol)["direction"]

    def is_exit_mandated(self, symbol: str) -> bool:
        """Convenience: check exit mandate for a symbol."""
        if self.quant is None:
            return False
        r = self.quant.get_regime(symbol)
        return r.get("exit_mandate", False) if r else False
