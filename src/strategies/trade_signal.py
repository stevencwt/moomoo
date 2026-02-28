"""
TradeSignal
===========
Frozen dataclass representing a fully specified trade recommendation
produced by a strategy after evaluating a MarketSnapshot.

A TradeSignal is NOT an order — it is a recommendation.
The Trade Manager (Phase 4) decides whether to act on it based on
portfolio-level constraints (position limits, total risk budget, etc.)

Design principles:
  - Frozen (read-only) — no mutation after creation
  - Single-leg for covered calls (sell only — shares provide the cover)
  - Two-leg for spreads (sell_contract + buy_contract)
  - All risk metrics pre-computed — Trade Manager never recalculates
  - reason field explains WHY the signal was generated (for logging/review)
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class TradeSignal:
    """
    A fully specified trade recommendation from a strategy.

    Fields:
      strategy_name  : Strategy that generated this signal e.g. "covered_call"
      symbol         : Underlying symbol in MooMoo format e.g. "US.TSLA"
      timestamp      : When this signal was generated
      action         : "OPEN" (new position) | "CLOSE" (exit existing)
      signal_type    : "covered_call" | "bear_call_spread" | "bull_put_spread" | "iron_condor"

    Legs:
      sell_contract  : MooMoo contract code for the short leg (always present)
      buy_contract   : MooMoo contract code for the protective leg (spreads only, None for covered calls)
      quantity       : Number of contracts

    Pricing (per share, multiply by 100 for per-contract):
      sell_price     : Limit price for the sell leg (mid-price at signal time)
      buy_price      : Limit price for the buy leg (None for covered calls)
      net_credit     : Net credit received per share

    Risk metrics (per contract = × 100):
      max_profit     : Maximum profit if expires worthless ($)
      max_loss       : Maximum loss at expiry ($) — undefined risk for uncovered calls = None
      breakeven      : Underlying price at breakeven at expiry
      reward_risk    : max_profit / max_loss ratio (None if max_loss undefined)

    Context:
      expiry         : Option expiry date "YYYY-MM-DD"
      dte            : Days to expiry at signal time
      iv_rank        : IV Rank at signal time (0-100)
      delta          : Delta of the short leg at signal time
      reason         : Human-readable explanation of why this signal was generated
      regime         : Market regime at signal time
    """
    # Identity
    strategy_name:  str
    symbol:         str
    timestamp:      datetime
    action:         str           # "OPEN" | "CLOSE"
    signal_type:    str           # "covered_call" | "bear_call_spread"

    # Legs
    sell_contract:  str
    buy_contract:   Optional[str]
    quantity:       int

    # Pricing
    sell_price:     float
    buy_price:      Optional[float]
    net_credit:     float

    # Risk metrics
    max_profit:     float
    max_loss:       Optional[float]   # None = undefined risk (naked positions)
    breakeven:      float
    reward_risk:    Optional[float]   # None if max_loss undefined

    # Context
    expiry:         str
    dte:            int
    iv_rank:        float
    delta:          float
    reason:         str
    regime:         str

    def __post_init__(self):
        valid_actions = {"OPEN", "CLOSE"}
        if self.action not in valid_actions:
            raise ValueError(f"Invalid action '{self.action}'. Must be one of {valid_actions}")

        valid_types = {"covered_call", "bear_call_spread", "bull_put_spread", "iron_condor"}
        if self.signal_type not in valid_types:
            raise ValueError(f"Invalid signal_type '{self.signal_type}'")

        if self.quantity < 1:
            raise ValueError(f"quantity must be >= 1, got {self.quantity}")

        if self.net_credit <= 0:
            raise ValueError(
                f"net_credit must be positive for premium selling, got {self.net_credit}"
            )

        if self.dte < 0:
            raise ValueError(f"dte cannot be negative, got {self.dte}")

    @property
    def is_spread(self) -> bool:
        """Return True if this is a multi-leg spread."""
        return self.buy_contract is not None

    @property
    def total_credit(self) -> float:
        """Total credit received across all contracts (net_credit × quantity × 100)."""
        return round(self.net_credit * self.quantity * 100, 2)

    @property
    def total_max_loss(self) -> Optional[float]:
        """Total maximum loss across all contracts."""
        if self.max_loss is None:
            return None
        return round(self.max_loss * self.quantity, 2)

    def summary(self) -> str:
        """Return a one-line human-readable summary for logging."""
        legs = f"{self.sell_contract}"
        if self.buy_contract:
            legs += f" / {self.buy_contract}"
        return (
            f"[{self.strategy_name.upper()}] {self.symbol} | "
            f"{legs} | qty={self.quantity} | "
            f"credit=${self.net_credit:.2f} | "
            f"max_loss=${self.max_loss:.0f}" if self.max_loss else
            f"[{self.strategy_name.upper()}] {self.symbol} | "
            f"{legs} | qty={self.quantity} | "
            f"credit=${self.net_credit:.2f} | "
            f"DTE={self.dte} | IV_rank={self.iv_rank:.0f}"
        )
