"""
regime_bridge.py — Regime Detection bridge for the Options Bot
==============================================================
Drop into: moomoo/src/market/regime_bridge.py

Wraps RegimeManager (one per symbol) with:
  - Bootstrap from yfinance daily OHLCV on first call
  - Daily update after market close
  - Graceful degradation if package not installed

Usage (from market_scanner.py):
    from src.market.regime_bridge import RegimeBridge
    bridge = RegimeBridge()
    regime_v2 = bridge.update(symbol, ohlcv_df)   # ohlcv_df = yfinance daily OHLCV
"""

import time
from datetime import date
from typing import Dict, Optional

import pandas as pd

try:
    from regime_detection import RegimeManager
    REGIME_AVAILABLE = True
except ImportError:
    REGIME_AVAILABLE = False

import logging
from src.logger import get_logger

logger = get_logger("market.regime_bridge")

# Suppress hmmlearn/regime_detection internal convergence noise.
# These warnings fire during HMM bootstrap on every incremental refit
# and are not actionable — the regime output remains valid despite them.
logging.getLogger("hmmlearn.base").setLevel(logging.ERROR)
logging.getLogger("regime_detection.signals").setLevel(logging.ERROR)

# ── Constants ─────────────────────────────────────────────────────────────────

# options_income: daily bars, 252-bar lookback, 5-bar HMM stability
MARKET_TYPE    = "US_STOCK"
STRATEGY_TYPE  = "options_income"
MARKET_CLASS   = "us_stocks"

# Column name map: yfinance → regime module (open/high/low/close/volume)
_COL_MAP = {"open": "o", "high": "h", "low": "l", "close": "c", "volume": "v"}


class RegimeBridge:
    """
    Maintains one RegimeManager per symbol, bootstrapped from daily OHLCV.

    Thread safety: not thread-safe — designed for single-threaded scheduler.
    """

    def __init__(self):
        if not REGIME_AVAILABLE:
            logger.warning(
                "[RegimeBridge] regime_detection not installed — "
                "run: pip3 install -e /Users/user/regime-detection"
            )
        self._managers:       Dict[str, RegimeManager] = {}
        self._bootstrapped:   Dict[str, bool]          = {}
        self._last_bar_date:  Dict[str, date]          = {}
        self.enabled = REGIME_AVAILABLE
        if self.enabled:
            logger.info(
                f"[RegimeBridge] enabled — "
                f"strategy={STRATEGY_TYPE} market={MARKET_TYPE}"
            )

    # ── Public API ────────────────────────────────────────────────────────────

    def update(self, symbol: str, ohlcv: pd.DataFrame) -> dict:
        """
        Feed today's OHLCV to the regime manager for this symbol.

        Args:
            symbol : MooMoo format e.g. "US.SPY"
            ohlcv  : Daily OHLCV DataFrame from YFinanceConnector.get_daily_ohlcv()
                     Columns: open, high, low, close, volume (lowercase)
                     Index: DatetimeIndex

        Returns:
            Regime dict from get_current_regime(), or {} if unavailable.
        """
        if not self.enabled or ohlcv is None or len(ohlcv) == 0:
            return {}

        try:
            ohlcv = self._clean(ohlcv)
            if len(ohlcv) == 0:
                logger.warning(f"[RegimeBridge] {symbol}: no clean bars after NaN/inf drop")
                return {}
            manager = self._get_or_create(symbol)

            # Bootstrap: feed all historical bars on first call
            if not self._bootstrapped.get(symbol):
                self._bootstrap(symbol, manager, ohlcv)
                return manager.get_current_regime()

            # Subsequent calls: only feed bars we haven't seen yet
            last = self._last_bar_date.get(symbol)
            new_rows = ohlcv[ohlcv.index.date > last] if last else ohlcv.tail(1)
            for ts, row in new_rows.iterrows():
                bar = self._row_to_bar(ts, row)
                manager.update(bar=bar)
            if len(new_rows):
                self._last_bar_date[symbol] = ohlcv.index[-1].date()

            return manager.get_current_regime()

        except Exception as e:
            logger.warning(f"[RegimeBridge] {symbol} update failed: {e}")
            return {}

    def get_regime(self, symbol: str) -> dict:
        """Return the last computed regime dict for a symbol without updating."""
        if not self.enabled or symbol not in self._managers:
            return {}
        try:
            return self._managers[symbol].get_current_regime()
        except Exception:
            return {}

    def bar_count(self, symbol: str) -> int:
        """Return number of bars buffered for a symbol."""
        if symbol not in self._managers:
            return 0
        return self._managers[symbol].bar_count

    # ── Private ───────────────────────────────────────────────────────────────

    def _get_or_create(self, symbol: str) -> "RegimeManager":
        if symbol not in self._managers:
            self._managers[symbol] = RegimeManager(
                market_type=MARKET_TYPE,
                strategy_type=STRATEGY_TYPE,
                market_class=MARKET_CLASS,
            )
            self._bootstrapped[symbol] = False
            logger.debug(f"[RegimeBridge] created manager for {symbol}")
        return self._managers[symbol]

    def _bootstrap(self, symbol: str, manager: "RegimeManager",
                   ohlcv: pd.DataFrame) -> None:
        """Feed entire historical OHLCV history to warm up the HMM."""
        ohlcv = self._clean(ohlcv)
        fed = 0
        for ts, row in ohlcv.iterrows():
            bar = self._row_to_bar(ts, row)
            manager.update(bar=bar)
            fed += 1

        self._bootstrapped[symbol]  = True
        self._last_bar_date[symbol] = ohlcv.index[-1].date()

        r = manager.get_current_regime()
        logger.info(
            f"[RegimeBridge] {symbol} bootstrap: {fed} bars | "
            f"consensus={r.get('consensus_state','?')} | "
            f"recommended={r.get('recommended_logic','?')} | "
            f"vol={r.get('volatility_regime','?')} | "
            f"conf={r.get('confidence_score', 0):.2f}"
        )

    @staticmethod
    def _clean(ohlcv: pd.DataFrame) -> pd.DataFrame:
        """Drop rows with NaN or inf in OHLCV columns before feeding to HMM."""
        cols = [c for c in ("open", "high", "low", "close", "volume") if c in ohlcv.columns]
        cleaned = ohlcv.dropna(subset=cols)
        # Also drop rows where any OHLCV value is inf
        import numpy as np
        mask = ~cleaned[cols].apply(lambda s: np.isinf(s)).any(axis=1)
        cleaned = cleaned[mask]
        return cleaned

    @staticmethod
    def _row_to_bar(ts, row) -> dict:
        """Convert a yfinance OHLCV row to the regime module bar format."""
        return {
            "timestamp": int(ts.timestamp()) if hasattr(ts, "timestamp") else int(time.time()),
            "o": float(row.get("open",  row.get("o", 0))),
            "h": float(row.get("high",  row.get("h", 0))),
            "l": float(row.get("low",   row.get("l", 0))),
            "c": float(row.get("close", row.get("c", 0))),
            "v": float(row.get("volume",row.get("v", 0))),
        }


# ── Regime translation helpers ────────────────────────────────────────────────

def translate_to_bot_regime(regime_v2: dict, vix: float,
                             high_vol_vix: float = 25.0) -> Optional[str]:
    """
    Translate the v2 regime dict into the bot's existing regime strings.
    Returns None if regime_v2 is empty (module not available).

    Mapping:
        BULL_PERSISTENT  → "bull"
        BEAR_PERSISTENT  → "bear"
        CHOP_NEUTRAL     → "neutral"
        TRANSITION       → "high_vol"  (conservative: stand aside during transitions)
        EXPANDING vol    → "high_vol"  (overrides consensus — IV expanding = danger)
        NO_TRADE logic   → "high_vol"  (module says don't trade)
        VIX >= threshold → "high_vol"  (preserve original VIX safety gate)
    """
    if not regime_v2:
        return None

    # VIX gate preserved — highest priority
    if vix >= high_vol_vix:
        return "high_vol"

    # Expanding volatility → stand aside
    if regime_v2.get("volatility_regime") == "EXPANDING":
        return "high_vol"

    # Module says don't trade (NO_TRADE or structural break)
    if regime_v2.get("recommended_logic") == "NO_TRADE":
        return "high_vol"

    consensus = regime_v2.get("consensus_state", "UNKNOWN")
    mapping = {
        "BULL_PERSISTENT": "bull",
        "BEAR_PERSISTENT": "bear",
        "CHOP_NEUTRAL":    "neutral",
        "TRANSITION":      "high_vol",
        "UNKNOWN":         "high_vol",
    }
    return mapping.get(consensus, "high_vol")
