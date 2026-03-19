"""
src/scalp/signals/iv_skew.py
==============================
IV Skew monitor for the GEX Reversion Scalper.

Tracks the 25-delta risk reversal (put IV minus call IV at equidistant OTM
strikes) and its intraday rate of change. Used by scalp_gate.py as Gate 3.

Key outputs:
    skew_level  : float — current 25Δ risk reversal value
                          positive = puts more expensive than calls (bearish lean)
                          negative = calls more expensive (bullish lean, rare)
    skew_delta  : float — change in skew over last N readings
                          positive = put demand increasing (bearish momentum)
                          negative = put demand decreasing (fear subsiding)
    bias        : str   — "bearish" | "neutral" | "bullish"
    steepening  : bool  — True if skew_delta >= steepening_threshold (rapid put demand)
    readings    : int   — number of readings collected so far

Gate 3 conditions (from strategy doc Section 10):
    For LONG entries:
        skew_level <= 3.0 AND NOT steepening
    For SHORT entries:
        skew_level >= 3.0 OR steepening

How 25-delta strikes are found:
    The 25-delta put strike is the OTM put whose absolute delta is closest to 0.25.
    The 25-delta call strike is the OTM call whose absolute delta is closest to 0.25.
    Risk reversal = IV(25Δ put) − IV(25Δ call)

Usage:
    from ibkr_connector import IBKRClient
    from src.scalp.signals.iv_skew import IVSkewMonitor

    client  = IBKRClient(port=7496, account="U18705798")
    client.connect()

    monitor = IVSkewMonitor(config)
    monitor.refresh("SPY", client)   # call at each GEX refresh time

    state = monitor.get("SPY")
    if state["skew_level"] >= 3.0 or state["steepening"]:
        direction_bias = "SHORT"
    else:
        direction_bias = "LONG_OK"
"""

import logging
import time
from collections import deque
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger("options_bot.scalp.signals.iv_skew")


# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_BEARISH_LEVEL       = 3.0    # skew >= this = bearish put demand
DEFAULT_STEEPENING_THRESHOLD= 0.5   # skew_delta >= this in window = rapid steepening
DEFAULT_DELTA_TARGET        = 0.25  # target delta for risk reversal legs
DEFAULT_DELTA_TOLERANCE     = 0.05  # accept delta in range [0.20, 0.30]
DEFAULT_HISTORY_WINDOW      = 6     # readings for rate-of-change (6 × 30min = 3h)


class IVSkewMonitor:
    """
    Tracks 25-delta risk reversal per symbol.
    Records level history for intraday rate-of-change calculation.

    Call refresh(symbol, client) at each GEX refresh time (09:00, 11:00, 13:00 ET).
    Call get(symbol) between refreshes to read latest state.

    Args:
        config: bot config dict. Reads from config["scalp"]["skew"] if present.
    """

    def __init__(self, config: dict):
        scalp_cfg = config.get("scalp", {})
        skew_cfg  = scalp_cfg.get("skew", {})

        self._bearish_level        = skew_cfg.get("bearish_level",        DEFAULT_BEARISH_LEVEL)
        self._steepening_threshold = skew_cfg.get("steepening_threshold", DEFAULT_STEEPENING_THRESHOLD)
        self._delta_target         = skew_cfg.get("delta_target",         DEFAULT_DELTA_TARGET)
        self._delta_tolerance      = skew_cfg.get("delta_tolerance",      DEFAULT_DELTA_TOLERANCE)
        self._history_window       = skew_cfg.get("history_window",       DEFAULT_HISTORY_WINDOW)

        # Per-symbol history: deque of (timestamp, skew_level) tuples
        self._history: Dict[str, deque] = {}
        # Per-symbol latest state
        self._state:   Dict[str, dict]  = {}

        logger.info(
            f"IVSkewMonitor initialised | bearish={self._bearish_level} | "
            f"steepening_thresh={self._steepening_threshold} | "
            f"delta_target={self._delta_target} | "
            f"window={self._history_window}"
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def refresh(self, symbol: str, client) -> dict:
        """
        Fetch latest option chain snapshot, compute 25Δ risk reversal,
        update history, and return current state.

        Args:
            symbol : Plain ticker e.g. "SPY"
            client : Connected IBKRClient instance

        Returns same dict as get().
        """
        ticker = symbol.replace("US.", "").upper()

        try:
            skew_level = self._compute_skew(ticker, client)
        except Exception as e:
            logger.warning(f"[IVSkew:{ticker}] compute failed: {e}")
            # Return stale state if available
            existing = self._state.get(ticker, {})
            if existing:
                stale = dict(existing)
                stale["stale"] = True
                return stale
            return self._empty_state(ticker)

        # Update history
        if ticker not in self._history:
            self._history[ticker] = deque(maxlen=self._history_window + 1)
        self._history[ticker].append((time.time(), skew_level))

        # Compute rate of change
        skew_delta = self._compute_delta(ticker)

        # Derive gates
        steepening = skew_delta >= self._steepening_threshold
        bias       = self._compute_bias(skew_level, skew_delta)

        state = {
            "symbol":      ticker,
            "skew_level":  round(skew_level, 4),
            "skew_delta":  round(skew_delta, 4),
            "bias":        bias,
            "steepening":  steepening,
            "ok_for_long": skew_level <= self._bearish_level and not steepening,
            "ok_for_short":skew_level >= self._bearish_level or steepening,
            "readings":    len(self._history[ticker]),
            "stale":       False,
            "timestamp":   time.time(),
        }
        self._state[ticker] = state

        logger.info(
            f"[IVSkew:{ticker}] level={skew_level:.3f} delta={skew_delta:+.3f} "
            f"bias={bias} steepening={steepening}"
        )
        return dict(state)

    def get(self, symbol: str) -> dict:
        """
        Return latest state for a symbol without making API calls.
        Returns empty/conservative state if never refreshed.
        """
        ticker = symbol.replace("US.", "").upper()
        return dict(self._state.get(ticker, self._empty_state(ticker)))

    def get_history(self, symbol: str) -> List[tuple]:
        """
        Return list of (timestamp, skew_level) tuples for a symbol.
        Useful for plotting and debugging.
        """
        ticker = symbol.replace("US.", "").upper()
        return list(self._history.get(ticker, []))

    def passes_gate(self, symbol: str, direction: str) -> dict:
        """
        Evaluate Gate 3 (IV skew alignment) for a trade direction.

        Args:
            symbol   : ticker
            direction: "LONG" | "SHORT"

        Returns:
            passed : bool
            reason : str — human-readable explanation
            state  : dict — full skew state
        """
        state = self.get(symbol)

        if state["stale"] and state["readings"] == 0:
            return {
                "passed": False,
                "reason": "No skew data available yet",
                "state":  state,
            }

        if direction == "LONG":
            passed = state["ok_for_long"]
            if not passed:
                if state["steepening"]:
                    reason = (f"Skew steepening (delta={state['skew_delta']:+.3f} >= "
                              f"{self._steepening_threshold}) — avoid longs")
                else:
                    reason = (f"Elevated put demand (skew={state['skew_level']:.2f} >= "
                              f"{self._bearish_level}) — avoid longs")
            else:
                reason = (f"Skew favourable for longs "
                          f"(level={state['skew_level']:.2f}, delta={state['skew_delta']:+.3f})")

        elif direction == "SHORT":
            passed = state["ok_for_short"]
            if not passed:
                reason = (f"Skew not supporting shorts "
                          f"(level={state['skew_level']:.2f} < {self._bearish_level} "
                          f"and not steepening)")
            else:
                reason = (f"Skew supports short "
                          f"(level={state['skew_level']:.2f}, delta={state['skew_delta']:+.3f})")
        else:
            return {"passed": False, "reason": f"Unknown direction: {direction}", "state": state}

        return {"passed": passed, "reason": reason, "state": state}

    # ── Internal ──────────────────────────────────────────────────────────────

    def _compute_skew(self, ticker: str, client) -> float:
        """
        Compute 25-delta risk reversal from live option chain.
        Returns put_iv_25d - call_iv_25d.
        """
        spot     = client.get_spot_price(ticker)
        expiries = client.get_option_expiries(ticker)

        if not expiries:
            raise ValueError(f"No expiries for {ticker}")

        # Use front expiry — same as GEX calculator
        from datetime import date as date_cls
        today       = date_cls.today()
        valid       = sorted([e for e in expiries if date_cls.fromisoformat(e) >= today])
        front_expiry= valid[0] if valid else expiries[0]

        # Get both legs of the chain
        calls_df = client.get_option_chain(ticker, front_expiry, "CALL")
        puts_df  = client.get_option_chain(ticker, front_expiry, "PUT")

        if calls_df.empty or puts_df.empty:
            raise ValueError(f"Empty chain for {ticker} {front_expiry}")

        # Get snapshots for both legs
        all_codes = calls_df["code"].tolist() + puts_df["code"].tolist()
        snap      = client.get_option_snapshot(all_codes)

        if snap.empty:
            raise ValueError(f"Empty snapshot for {ticker}")

        return self._extract_risk_reversal(snap, spot)

    def _extract_risk_reversal(self, snap: pd.DataFrame, spot: float) -> float:
        """
        Find the 25-delta put and call, return put_iv - call_iv.

        Strategy:
            OTM calls: delta > 0, strike > spot, find closest delta to +0.25
            OTM puts:  delta < 0 (absolute), strike < spot, find closest |delta| to 0.25

        Falls back to nearest-to-0.25 if no strike is within tolerance.
        """
        df = snap.copy()

        # Normalise column names
        col_map = {}
        for col in df.columns:
            lc = col.lower()
            if "delta" in lc:
                col_map[col] = "delta"
            elif "iv" in lc or "implied" in lc:
                col_map[col] = "iv"
            elif "strike" in lc:
                col_map[col] = "strike"
            elif lc in ("option_type", "type", "right", "call_put"):
                col_map[col] = "option_type"
        df = df.rename(columns=col_map)

        required = {"delta", "iv", "strike", "option_type"}
        missing  = required - set(df.columns)
        if missing:
            raise ValueError(f"Snapshot missing columns for skew: {missing}. "
                             f"Available: {list(snap.columns)}")

        df = df.dropna(subset=["delta", "iv"])
        df["option_type"] = df["option_type"].str.upper().str[0]
        df["delta"] = pd.to_numeric(df["delta"], errors="coerce")
        df["iv"]    = pd.to_numeric(df["iv"],    errors="coerce")
        df = df.dropna(subset=["delta", "iv"])
        df = df[df["iv"] > 0]

        # OTM calls: positive delta, strike >= spot
        otm_calls = df[(df["option_type"] == "C") & (df["strike"] >= spot)].copy()
        # OTM puts:  negative delta (or we use absolute), strike <= spot
        otm_puts  = df[(df["option_type"] == "P") & (df["strike"] <= spot)].copy()

        if otm_calls.empty:
            raise ValueError(f"No OTM calls found (spot={spot:.2f})")
        if otm_puts.empty:
            raise ValueError(f"No OTM puts found (spot={spot:.2f})")

        # Find strike with delta closest to target (0.25) for calls
        otm_calls["delta_dist"] = (otm_calls["delta"].abs() - self._delta_target).abs()
        call_25d  = otm_calls.loc[otm_calls["delta_dist"].idxmin()]
        call_iv   = float(call_25d["iv"])
        call_delta= float(call_25d["delta"])
        call_strike=float(call_25d["strike"])

        # Find strike with |delta| closest to target for puts
        otm_puts["delta_dist"] = (otm_puts["delta"].abs() - self._delta_target).abs()
        put_25d   = otm_puts.loc[otm_puts["delta_dist"].idxmin()]
        put_iv    = float(put_25d["iv"])
        put_delta = float(put_25d["delta"])
        put_strike= float(put_25d["strike"])

        risk_reversal = put_iv - call_iv

        logger.debug(
            f"25Δ call: strike={call_strike:.2f} delta={call_delta:.3f} IV={call_iv:.4f} | "
            f"25Δ put: strike={put_strike:.2f} delta={put_delta:.3f} IV={put_iv:.4f} | "
            f"RR={risk_reversal:.4f}"
        )
        return risk_reversal

    def _compute_delta(self, ticker: str) -> float:
        """
        Compute rate of change of skew: latest reading minus oldest in window.
        Returns 0.0 if fewer than 2 readings.
        """
        hist = self._history.get(ticker, deque())
        if len(hist) < 2:
            return 0.0
        oldest_level = hist[0][1]
        latest_level = hist[-1][1]
        return round(latest_level - oldest_level, 4)

    def _compute_bias(self, skew_level: float, skew_delta: float) -> str:
        """
        Derive directional bias from skew level and direction.
        Matches the table in strategy doc Section 5.
        """
        if skew_level >= self._bearish_level and skew_delta >= self._steepening_threshold:
            return "bearish_accelerating"
        elif skew_level >= self._bearish_level:
            return "bearish"
        elif skew_level >= self._bearish_level * 0.67:  # moderate: 2.0–3.0
            return "neutral"
        elif skew_delta <= -self._steepening_threshold:
            return "bullish_improving"   # skew falling fast = fear subsiding
        else:
            return "bullish"

    @staticmethod
    def _empty_state(ticker: str) -> dict:
        """Conservative empty state before any readings."""
        return {
            "symbol":       ticker,
            "skew_level":   0.0,
            "skew_delta":   0.0,
            "bias":         "neutral",
            "steepening":   False,
            "ok_for_long":  True,   # permissive default — gate fails open before data
            "ok_for_short": True,
            "readings":     0,
            "stale":        True,
            "timestamp":    0.0,
        }
