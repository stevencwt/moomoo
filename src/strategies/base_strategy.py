"""
Base Strategy
=============
Abstract base class that all strategies must implement.

Contract:
  - evaluate(snapshot) returns a TradeSignal if entry criteria are met, else None
  - Each strategy is self-contained — it uses OptionsAnalyser internally
  - Strategies are read-only consumers of MarketSnapshot — they never modify it
  - Strategies never place orders — they only produce signals

Adding a new strategy:
  1. Subclass BaseStrategy
  2. Implement evaluate() and name
  3. Register in StrategyRegistry config
"""

from abc import ABC, abstractmethod
from typing import Optional

from src.market.market_snapshot import MarketSnapshot
from src.market.options_analyser import OptionsAnalyser
from src.connectors.connector_protocol import BrokerConnector
from src.strategies.trade_signal import TradeSignal
from src.logger import get_logger


class BaseStrategy(ABC):
    """Abstract base for all trading strategies."""

    def __init__(
        self,
        config:   dict,
        moomoo:   BrokerConnector,
        options:  OptionsAnalyser,
    ):
        self._config  = config
        self._moomoo  = moomoo
        self._options = options
        self._logger  = get_logger(f"strategies.{self.name}")

        strategy_cfg = config.get("strategies", {}).get(self.name, {})
        self._enabled = strategy_cfg.get("enabled", True)

        self._last_skip_reason: str = ""   # populated by evaluate() on every skip
        self._logger.info(
            f"Strategy '{self.name}' initialised | enabled={self._enabled}"
        )

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique snake_case identifier e.g. 'covered_call'."""
        ...

    @abstractmethod
    def evaluate(self, snapshot: MarketSnapshot) -> Optional[TradeSignal]:
        """
        Evaluate entry criteria against the snapshot.

        Args:
            snapshot: Frozen MarketSnapshot produced by MarketScanner

        Returns:
            TradeSignal if all entry criteria are met, None otherwise.
        """
        ...

    @property
    def is_enabled(self) -> bool:
        """Return False to skip evaluation without removing from registry."""
        return self._enabled

    @property
    def last_skip_reason(self) -> str:
        """Human-readable reason why the last evaluate() call returned None."""
        return self._last_skip_reason

    def _skip(self, reason: str) -> None:
        """Record skip reason and return (call before returning None in evaluate)."""
        self._last_skip_reason = reason
        self._logger.debug(f"{self.name}: SKIP — {reason}")

    def _get_cfg(self, key: str, default):
        """Convenience: read from strategy-specific config block."""
        return (
            self._config
            .get("strategies", {})
            .get(self.name, {})
            .get(key, default)
        )
