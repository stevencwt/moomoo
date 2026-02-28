"""
Options Analyser
================
Handles options-specific calculations that strategies need:

  - Target expiry selection (DTE targeting)
  - Earnings conflict detection (block trades within N days of earnings)
  - OTM call filtering by delta range
  - Best call selection for covered calls
  - Spread leg selection for bear call spreads
  - Spread metrics calculation (max profit, max loss, breakeven)

All thresholds are config-driven.
"""

import pandas as pd
from datetime import date, datetime
from typing import List, Optional, Dict, Tuple

from src.logger import get_logger
from src.exceptions import DataError

logger = get_logger("market.options_analyser")


class OptionsAnalyser:
    """
    Options-specific analysis utilities.
    Stateless — all methods take explicit inputs.
    """

    def __init__(self, config: dict):
        opts_cfg = config.get("options", {})

        self._target_dte_min      = opts_cfg.get("target_dte_min", 21)
        self._target_dte_max      = opts_cfg.get("target_dte_max", 45)
        self._earnings_buffer_days = opts_cfg.get("earnings_buffer_days", 7)
        self._min_open_interest   = opts_cfg.get("min_open_interest", 100)
        self._delta_min           = opts_cfg.get("otm_call_delta_min", 0.20)
        self._delta_max           = opts_cfg.get("otm_call_delta_max", 0.35)
        self._spread_width_target = opts_cfg.get("spread_width_target", 10.0)

        logger.info(
            f"OptionsAnalyser | DTE={self._target_dte_min}-{self._target_dte_max} | "
            f"delta={self._delta_min}-{self._delta_max} | "
            f"earnings_buffer={self._earnings_buffer_days}d"
        )

    # ── Expiry Selection ──────────────────────────────────────────

    def get_target_expiry(
        self,
        available_expiries: List[str],
        as_of: Optional[date] = None
    ) -> Optional[str]:
        """
        Select the best expiry date targeting the configured DTE range.

        Prefers the expiry with DTE closest to the midpoint of the target range.
        Falls back to nearest expiry within ±7 days of the range if nothing
        falls cleanly inside it.

        Args:
            available_expiries: List of "YYYY-MM-DD" strings, sorted ascending
            as_of             : Reference date (defaults to today)

        Returns:
            Best expiry date string, or None if no suitable expiry found.
        """
        today     = as_of or date.today()
        mid_dte   = (self._target_dte_min + self._target_dte_max) / 2
        best      = None
        best_dist = float("inf")

        for expiry_str in available_expiries:
            expiry = date.fromisoformat(expiry_str)
            dte    = (expiry - today).days

            if self._target_dte_min <= dte <= self._target_dte_max:
                dist = abs(dte - mid_dte)
                if dist < best_dist:
                    best      = expiry_str
                    best_dist = dist

        if best:
            logger.debug(f"Target expiry: {best} (DTE range {self._target_dte_min}-{self._target_dte_max})")
            return best

        # Fallback: nearest expiry within ±7 days of range
        buffer = 7
        for expiry_str in available_expiries:
            expiry = date.fromisoformat(expiry_str)
            dte    = (expiry - today).days
            if (self._target_dte_min - buffer) <= dte <= (self._target_dte_max + buffer):
                logger.debug(f"Target expiry (fallback ±{buffer}d): {expiry_str} DTE={dte}")
                return expiry_str

        logger.warning(
            f"No suitable expiry found in range {self._target_dte_min}-{self._target_dte_max} "
            f"from {available_expiries[:5]}"
        )
        return None

    # ── Earnings Conflict ─────────────────────────────────────────

    def check_earnings_conflict(
        self,
        expiry: str,
        next_earnings: Optional[date],
        as_of: Optional[date] = None
    ) -> bool:
        """
        Check if an earnings date falls within the option's lifetime.

        Blocks the trade if earnings falls between today and expiry + buffer.
        This prevents holding a short premium position through a binary event.

        Args:
            expiry       : Option expiry date "YYYY-MM-DD"
            next_earnings: Next known earnings date, or None
            as_of        : Reference date (defaults to today)

        Returns:
            True if earnings conflict detected (BLOCK trade),
            False if safe to proceed.
        """
        if next_earnings is None:
            return False

        today         = as_of or date.today()
        expiry_date   = date.fromisoformat(expiry)
        buffer_date   = expiry_date + pd.Timedelta(days=self._earnings_buffer_days).to_pytimedelta()
        buffer_date   = expiry_date.__class__(
            buffer_date.year if hasattr(buffer_date, 'year') else expiry_date.year,
            buffer_date.month if hasattr(buffer_date, 'month') else expiry_date.month,
            buffer_date.day if hasattr(buffer_date, 'day') else expiry_date.day
        )

        # Simpler calculation
        from datetime import timedelta
        buffer_date = expiry_date + timedelta(days=self._earnings_buffer_days)

        conflict = today <= next_earnings <= buffer_date

        if conflict:
            logger.info(
                f"Earnings conflict: earnings={next_earnings} falls within "
                f"{today} to {buffer_date} (expiry={expiry} +{self._earnings_buffer_days}d buffer)"
            )
        return conflict

    # ── OTM Call Filtering ────────────────────────────────────────

    def filter_otm_calls(
        self,
        chain: pd.DataFrame,
        snapshot: pd.DataFrame,
        spot_price: float
    ) -> pd.DataFrame:
        """
        Filter option chain to OTM calls within the configured delta range.

        Requires snapshot data to have delta values.
        Falls back to strike-based OTM filtering if delta not available.

        Args:
            chain     : Option chain DataFrame (from MooMooConnector.get_option_chain)
            snapshot  : Snapshot DataFrame with Greeks (from get_option_snapshot)
            spot_price: Current underlying price

        Returns:
            Filtered DataFrame of qualifying OTM calls, sorted by strike ascending.
        """
        # Start with calls only, above spot price (OTM)
        calls = chain[
            (chain["option_type"] == "CALL") &
            (chain["strike_price"] > spot_price)
        ].copy()

        if len(calls) == 0:
            logger.debug("No OTM calls found in chain")
            return pd.DataFrame()

        # Merge with snapshot to get Greeks
        if len(snapshot) > 0 and "option_delta" in snapshot.columns:
            calls = calls.merge(
                snapshot[["code", "option_delta", "option_open_interest",
                           "bid_price", "ask_price", "mid_price"]].rename(
                    columns={"option_open_interest": "open_interest"}
                ),
                on="code",
                how="left"
            )

            # Filter by delta range
            delta_filtered = calls[
                (calls["option_delta"] >= self._delta_min) &
                (calls["option_delta"] <= self._delta_max)
            ]

            # Filter by minimum open interest
            if "open_interest" in delta_filtered.columns:
                delta_filtered = delta_filtered[
                    delta_filtered["open_interest"] >= self._min_open_interest
                ]

            if len(delta_filtered) > 0:
                result = delta_filtered.sort_values("strike_price")
                logger.debug(
                    f"OTM calls after delta filter ({self._delta_min}-{self._delta_max}): "
                    f"{len(result)}"
                )
                return result

        # Fallback: no Greeks available — return all OTM calls sorted by strike
        logger.debug(
            "Delta data not available — returning all OTM calls (no delta filter)"
        )
        return calls.sort_values("strike_price")

    def select_best_call(self, otm_calls: pd.DataFrame) -> Optional[pd.Series]:
        """
        Select the best OTM call from filtered candidates.

        Selection logic:
          1. Prefer highest delta within range (more premium)
          2. Must have bid > 0 (must be tradeable)
          3. Must meet minimum open interest

        Args:
            otm_calls: Filtered DataFrame from filter_otm_calls()

        Returns:
            Best call row as pd.Series, or None if no qualifying call found.
        """
        if len(otm_calls) == 0:
            return None

        candidates = otm_calls.copy()

        # Must have a bid
        if "bid_price" in candidates.columns:
            candidates = candidates[candidates["bid_price"] > 0]

        if len(candidates) == 0:
            logger.debug("No OTM calls with positive bid found")
            return None

        # Sort by delta descending (highest delta = most premium = best covered call)
        if "option_delta" in candidates.columns:
            candidates = candidates.sort_values("option_delta", ascending=False)

        best = candidates.iloc[0]
        logger.debug(
            f"Best call selected: {best['code']} | "
            f"strike={best['strike_price']} | "
            f"delta={best.get('option_delta', 'N/A')} | "
            f"bid={best.get('bid_price', 'N/A')}"
        )
        return best

    # ── Spread Construction ───────────────────────────────────────

    def find_protective_call(
        self,
        sell_strike: float,
        chain: pd.DataFrame,
        width: Optional[float] = None
    ) -> Optional[pd.Series]:
        """
        Find the protective (long) call leg for a bear call spread.

        Targets a strike approximately `width` above the sell strike.
        If exact width not available, picks nearest available strike above.

        Args:
            sell_strike: Strike price of the short leg
            chain      : Full option chain DataFrame
            width      : Target spread width in dollars (uses config default if None)

        Returns:
            Best protective call row, or None if not found.
        """
        target_width  = width or self._spread_width_target
        target_strike = sell_strike + target_width

        calls = chain[
            (chain["option_type"] == "CALL") &
            (chain["strike_price"] > sell_strike)
        ].copy()

        if len(calls) == 0:
            return None

        # Find closest strike to target
        calls["strike_dist"] = (calls["strike_price"] - target_strike).abs()
        calls = calls.sort_values("strike_dist")

        best = calls.iloc[0]
        logger.debug(
            f"Protective call: {best['code']} | "
            f"strike={best['strike_price']} | "
            f"actual_width={best['strike_price'] - sell_strike:.1f}"
        )
        return best

    def compute_spread_metrics(
        self,
        sell_strike: float,
        buy_strike: float,
        sell_premium: float,
        buy_premium: float
    ) -> Dict:
        """
        Compute bear call spread risk/reward metrics.

        Args:
            sell_strike  : Short call strike
            buy_strike   : Long call strike (must be > sell_strike)
            sell_premium : Credit received on short leg (per share)
            buy_premium  : Debit paid on long leg (per share)

        Returns:
            Dict with:
              net_credit  : Premium received per share (sell - buy)
              max_profit  : Net credit × 100 (per contract)
              max_loss    : (spread_width - net_credit) × 100 (per contract)
              breakeven   : sell_strike + net_credit
              reward_risk : max_profit / max_loss ratio
        """
        if buy_strike <= sell_strike:
            raise DataError(
                f"buy_strike ({buy_strike}) must be > sell_strike ({sell_strike})"
            )

        spread_width = buy_strike - sell_strike
        net_credit   = sell_premium - buy_premium
        max_profit   = net_credit * 100
        max_loss     = (spread_width - net_credit) * 100
        breakeven    = sell_strike + net_credit
        reward_risk  = max_profit / max_loss if max_loss > 0 else 0

        metrics = {
            "net_credit":  round(net_credit, 4),
            "max_profit":  round(max_profit, 2),
            "max_loss":    round(max_loss, 2),
            "breakeven":   round(breakeven, 2),
            "reward_risk": round(reward_risk, 4),
            "spread_width": round(spread_width, 2),
        }

        logger.debug(
            f"Spread metrics: credit=${net_credit:.2f} | "
            f"max_profit=${max_profit:.0f} | max_loss=${max_loss:.0f} | "
            f"R/R={reward_risk:.2f}"
        )
        return metrics
