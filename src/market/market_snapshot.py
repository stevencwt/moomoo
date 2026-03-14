"""
MarketSnapshot
==============
Frozen dataclass representing a complete point-in-time view of a symbol.

Produced by: MarketScanner.scan_symbol()
Consumed by: StrategyRegistry (all strategies read from this, never modify it)

Design principles:
  - Frozen (read-only) — strategies can never accidentally mutate shared state
  - All fields typed — no silent None surprises downstream
  - Optional fields use None with clear semantics documented below
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Technicals:
    """
    Latest technical indicator values computed from daily OHLCV.
    All values are from the most recent completed bar.
    """
    # Bollinger Bands
    bb_upper:   float   # Upper band
    bb_middle:  float   # Middle band (20-day SMA)
    bb_lower:   float   # Lower band
    pct_b:      float   # %B = (close - lower) / (upper - lower). Range 0-1, can exceed

    # Momentum
    rsi:        float   # RSI(14). Overbought >= 70, oversold <= 30
    macd:       float   # MACD line (12/26 EMA diff)
    macd_signal: float  # Signal line (9-day EMA of MACD)
    macd_hist:  float   # Histogram = MACD - signal

    # Volatility
    atr:        float   # ATR(14) in dollar terms
    atr_pct:    float   # ATR as % of close price


@dataclass(frozen=True)
class OptionsContext:
    """
    Options-specific data for the symbol at scan time.
    """
    iv_rank:          float         # IV Rank 0-100 (current IV vs 52-week range)
    atm_iv:           float         # ATM implied volatility %
    available_expiries: List[str]   # Upcoming expiry dates "YYYY-MM-DD"


@dataclass(frozen=True)
class MarketSnapshot:
    """
    Complete point-in-time view of a symbol for strategy evaluation.

    Fields:
      symbol          : MooMoo format e.g. "US.TSLA"
      timestamp       : When this snapshot was produced
      spot_price      : Last traded price of the underlying
      technicals      : Bollinger Bands, RSI, MACD, ATR
      vix             : Current VIX level
      market_regime   : "bull" | "bear" | "neutral" | "high_vol"
      options_context : IV rank, ATM IV, available expiries
      next_earnings   : Next earnings date, None if unknown
      days_to_earnings: Calendar days to next earnings, None if no earnings known
      shares_held     : Number of underlying shares held in stock account
      open_positions  : Number of currently open option positions
    """
    symbol:           str
    timestamp:        datetime
    spot_price:       float
    technicals:       Technicals
    vix:              float
    market_regime:    str           # "bull" | "bear" | "neutral" | "high_vol"
    options_context:  OptionsContext
    next_earnings:    Optional[date]
    days_to_earnings: Optional[int]
    shares_held:      int
    open_positions:   int
    regime_v2:        Optional[Dict] = None  # Full output from regime-detection module (None if not installed)

    def __post_init__(self):
        valid_regimes = {"bull", "bear", "neutral", "high_vol"}
        if self.market_regime not in valid_regimes:
            raise ValueError(
                f"Invalid market_regime '{self.market_regime}'. "
                f"Must be one of {valid_regimes}"
            )
        if self.spot_price <= 0:
            raise ValueError(f"spot_price must be positive, got {self.spot_price}")
        if self.shares_held < 0:
            raise ValueError(f"shares_held cannot be negative, got {self.shares_held}")
        if self.open_positions < 0:
            raise ValueError(f"open_positions cannot be negative")
