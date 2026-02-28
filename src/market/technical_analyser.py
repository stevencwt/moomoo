"""
Technical Analyser
==================
Computes Bollinger Bands, RSI, MACD, and ATR from daily OHLCV data.

All calculations use standard parameters matching TradingView defaults:
  Bollinger Bands : 20-period SMA, 2 std deviations
  RSI             : 14-period Wilder smoothing
  MACD            : 12/26/9
  ATR             : 14-period Wilder smoothing

Input : pandas DataFrame with columns: open, high, low, close, volume
Output: Technicals dataclass (latest bar values only)
"""

import pandas as pd
import numpy as np
from typing import Dict

from src.market.market_snapshot import Technicals
from src.exceptions import DataError
from src.logger import get_logger

logger = get_logger("market.technical_analyser")

# Minimum bars required for reliable indicator calculation
MIN_BARS_BB   = 20
MIN_BARS_RSI  = 28   # 14 + 14 warmup
MIN_BARS_MACD = 35   # 26 + 9 warmup
MIN_BARS      = max(MIN_BARS_BB, MIN_BARS_RSI, MIN_BARS_MACD)


class TechnicalAnalyser:
    """
    Stateless technical indicator calculator.
    All methods are pure functions of the input DataFrame.
    """

    def compute_all(self, ohlcv: pd.DataFrame) -> pd.DataFrame:
        """
        Compute all indicators and append as new columns to the DataFrame.

        Args:
            ohlcv: DataFrame with lowercase columns: open, high, low, close, volume
                   Index should be DatetimeIndex, sorted ascending.

        Returns:
            Copy of input DataFrame with additional columns:
            bb_upper, bb_middle, bb_lower, pct_b,
            rsi, macd, macd_signal, macd_hist,
            atr, atr_pct

        Raises:
            DataError: If fewer than MIN_BARS rows are provided.
        """
        self._validate(ohlcv)
        df = ohlcv.copy()

        df = self._add_bollinger_bands(df)
        df = self._add_rsi(df)
        df = self._add_macd(df)
        df = self._add_atr(df)

        logger.debug(f"Indicators computed on {len(df)} bars")
        return df

    def extract_latest(self, df: pd.DataFrame) -> Technicals:
        """
        Extract the most recent bar's indicator values as a Technicals dataclass.

        Args:
            df: DataFrame returned by compute_all()

        Returns:
            Technicals instance with latest values.

        Raises:
            DataError: If required indicator columns are missing.
        """
        required = [
            "bb_upper", "bb_middle", "bb_lower", "pct_b",
            "rsi", "macd", "macd_signal", "macd_hist",
            "atr", "atr_pct"
        ]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise DataError(
                f"Missing indicator columns: {missing}. "
                f"Run compute_all() first."
            )

        # Get last non-NaN row
        valid = df.dropna(subset=required)
        if len(valid) == 0:
            raise DataError("No valid rows after dropping NaN indicator values")

        row = valid.iloc[-1]

        t = Technicals(
            bb_upper=    float(row["bb_upper"]),
            bb_middle=   float(row["bb_middle"]),
            bb_lower=    float(row["bb_lower"]),
            pct_b=       float(row["pct_b"]),
            rsi=         float(row["rsi"]),
            macd=        float(row["macd"]),
            macd_signal= float(row["macd_signal"]),
            macd_hist=   float(row["macd_hist"]),
            atr=         float(row["atr"]),
            atr_pct=     float(row["atr_pct"]),
        )

        logger.debug(
            f"Latest technicals | close={float(row['close']):.2f} | "
            f"%B={t.pct_b:.2f} | RSI={t.rsi:.1f} | "
            f"MACD={t.macd:.3f} | ATR={t.atr:.2f}"
        )
        return t

    # ── Indicator Calculations ────────────────────────────────────

    def _add_bollinger_bands(
        self,
        df: pd.DataFrame,
        period: int = 20,
        std_dev: float = 2.0
    ) -> pd.DataFrame:
        """
        Add Bollinger Bands columns.

        Columns added: bb_upper, bb_middle, bb_lower, pct_b
        """
        close = df["close"]
        sma   = close.rolling(window=period).mean()
        std   = close.rolling(window=period).std(ddof=0)

        df["bb_middle"] = sma
        df["bb_upper"]  = sma + std_dev * std
        df["bb_lower"]  = sma - std_dev * std

        band_width = df["bb_upper"] - df["bb_lower"]
        # Avoid division by zero when bands are flat (extremely rare)
        df["pct_b"] = (close - df["bb_lower"]) / band_width.replace(0, np.nan)

        return df

    def _add_rsi(self, df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        """
        Add RSI using Wilder smoothing (matches TradingView).

        Wilder smoothing = EMA with alpha = 1/period
        Columns added: rsi
        """
        delta = df["close"].diff()
        gain  = delta.clip(lower=0)
        loss  = (-delta).clip(lower=0)

        # Wilder smoothing = EMA with alpha = 1/period
        alpha     = 1.0 / period
        avg_gain  = gain.ewm(alpha=alpha, adjust=False).mean()
        avg_loss  = loss.ewm(alpha=alpha, adjust=False).mean()

        # Handle zero avg_loss (all up-days) → RSI = 100
        zero_loss    = avg_loss == 0
        safe_loss    = avg_loss.where(~zero_loss, other=np.nan)
        rs           = avg_gain / safe_loss
        rsi_vals     = 100 - (100 / (1 + rs))
        rsi_vals[zero_loss] = 100.0
        df["rsi"]   = rsi_vals

        return df

    def _add_macd(
        self,
        df: pd.DataFrame,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9
    ) -> pd.DataFrame:
        """
        Add MACD line, signal line, and histogram.

        Columns added: macd, macd_signal, macd_hist
        """
        close  = df["close"]
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()

        df["macd"]        = ema_fast - ema_slow
        df["macd_signal"] = df["macd"].ewm(span=signal, adjust=False).mean()
        df["macd_hist"]   = df["macd"] - df["macd_signal"]

        return df

    def _add_atr(self, df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        """
        Add ATR using Wilder smoothing.

        True Range = max(high-low, |high-prev_close|, |low-prev_close|)
        Columns added: atr, atr_pct
        """
        high  = df["high"]
        low   = df["low"]
        close = df["close"]

        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs()
        ], axis=1).max(axis=1)

        alpha    = 1.0 / period
        df["atr"] = tr.ewm(alpha=alpha, adjust=False).mean()
        df["atr_pct"] = df["atr"] / close * 100

        return df

    # ── Validation ────────────────────────────────────────────────

    def _validate(self, ohlcv: pd.DataFrame) -> None:
        """Raise DataError if input is insufficient for reliable indicators."""
        required_cols = {"open", "high", "low", "close", "volume"}
        missing = required_cols - set(ohlcv.columns)
        if missing:
            raise DataError(f"OHLCV missing columns: {missing}")

        if len(ohlcv) < MIN_BARS:
            raise DataError(
                f"Need at least {MIN_BARS} bars for reliable indicators, "
                f"got {len(ohlcv)}"
            )
