"""
Regime Detector
===============
Classifies the current market regime into one of four states:

  "bull"     : Trending up   → favour bull put spreads, avoid bear call spreads
  "bear"     : Trending down → favour bear call spreads, avoid bull put spreads
  "neutral"  : Range-bound   → ideal for all premium selling strategies
  "high_vol" : VIX spike     → avoid all new positions (undefined risk environment)

Decision logic (priority order):
  1. VIX >= high_vol_threshold          → high_vol  (safety first)
  2. RSI >= overbought AND MACD > 0     → bull
  3. RSI <= oversold  AND MACD < 0      → bear
  4. Otherwise                          → neutral

All thresholds are config-driven — no hardcoded values.
"""

from src.market.market_snapshot import Technicals
from src.logger import get_logger

logger = get_logger("market.regime_detector")


class RegimeDetector:
    """
    Classifies market regime from technical indicators and VIX.
    Stateless — call detect() on each scan cycle.
    """

    def __init__(self, config: dict):
        regime_cfg = config.get("regime", {})

        self._high_vol_vix    = regime_cfg.get("high_vol_vix_threshold", 25.0)
        self._bull_rsi        = regime_cfg.get("bull_rsi_threshold", 55.0)
        self._bear_rsi        = regime_cfg.get("bear_rsi_threshold", 45.0)
        self._macd_threshold  = regime_cfg.get("macd_threshold", 0.0)

        logger.info(
            f"RegimeDetector | high_vol_vix={self._high_vol_vix} | "
            f"bull_rsi>={self._bull_rsi} | bear_rsi<={self._bear_rsi}"
        )

    def detect(self, technicals: Technicals, vix: float) -> str:
        """
        Classify market regime.

        Args:
            technicals: Latest technical indicator values
            vix       : Current VIX level

        Returns:
            One of: "bull" | "bear" | "neutral" | "high_vol"
        """
        # Rule 1: High volatility overrides everything
        if vix >= self._high_vol_vix:
            logger.debug(f"Regime: high_vol (VIX={vix:.1f} >= {self._high_vol_vix})")
            return "high_vol"

        # Rule 2: Bullish — RSI overbought + MACD positive
        if (technicals.rsi >= self._bull_rsi
                and technicals.macd > self._macd_threshold):
            logger.debug(
                f"Regime: bull "
                f"(RSI={technicals.rsi:.1f} >= {self._bull_rsi}, "
                f"MACD={technicals.macd:.3f} > {self._macd_threshold})"
            )
            return "bull"

        # Rule 3: Bearish — RSI oversold + MACD negative
        if (technicals.rsi <= self._bear_rsi
                and technicals.macd < self._macd_threshold):
            logger.debug(
                f"Regime: bear "
                f"(RSI={technicals.rsi:.1f} <= {self._bear_rsi}, "
                f"MACD={technicals.macd:.3f} < {self._macd_threshold})"
            )
            return "bear"

        # Default: range-bound / neutral
        logger.debug(
            f"Regime: neutral "
            f"(RSI={technicals.rsi:.1f}, MACD={technicals.macd:.3f}, VIX={vix:.1f})"
        )
        return "neutral"

    def is_eligible_to_trade(self, regime: str) -> bool:
        """
        Return False if regime makes all new positions inadvisable.
        Currently blocks high_vol only — strategies handle bull/bear/neutral
        selection themselves.

        Args:
            regime: Output from detect()

        Returns:
            True if safe to evaluate strategies, False if all positions blocked.
        """
        eligible = regime != "high_vol"
        if not eligible:
            logger.info("Trading blocked: high_vol regime")
        return eligible
