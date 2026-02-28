"""
Strategy Registry
=================
Holds all registered strategies and evaluates them against a MarketSnapshot.

Design principles:
  - Strategy-agnostic: the registry never knows what a strategy does internally
  - Fail-safe: one strategy crashing never prevents others from running
  - Order-aware: strategies evaluated in registration order (priority)
  - Deduplication: only one signal per symbol per strategy per cycle

Usage:
    registry = StrategyRegistry(config, moomoo, options)
    registry.register(CoveredCallStrategy(config, moomoo, options))
    registry.register(BearCallSpreadStrategy(config, moomoo, options))

    signals = registry.evaluate(snapshot)
    # Returns list of TradeSignals (0 to N per snapshot)
"""

from typing import List, Optional

from src.market.market_snapshot import MarketSnapshot
from src.strategies.base_strategy import BaseStrategy
from src.strategies.trade_signal import TradeSignal
from src.logger import get_logger

logger = get_logger("strategies.registry")


class StrategyRegistry:
    """
    Registry of all active strategies.
    Evaluates each against a MarketSnapshot and returns qualifying signals.
    """

    def __init__(self):
        self._strategies: List[BaseStrategy] = []
        logger.info("StrategyRegistry initialised")

    def register(self, strategy: BaseStrategy) -> None:
        """
        Add a strategy to the registry.

        Args:
            strategy: Any class that extends BaseStrategy
        """
        self._strategies.append(strategy)
        logger.info(
            f"Registered strategy '{strategy.name}' | "
            f"enabled={strategy.is_enabled} | "
            f"total={len(self._strategies)}"
        )

    def evaluate(self, snapshot: MarketSnapshot) -> List[TradeSignal]:
        """
        Evaluate all enabled strategies against a single snapshot.

        Strategies that raise exceptions are logged and skipped.
        Disabled strategies are silently skipped.

        Args:
            snapshot: Frozen MarketSnapshot from MarketScanner

        Returns:
            List of TradeSignals (may be empty).
        """
        signals = []

        for strategy in self._strategies:
            if not strategy.is_enabled:
                logger.debug(f"Skipping disabled strategy '{strategy.name}'")
                continue

            try:
                signal = strategy.evaluate(snapshot)
                if signal is not None:
                    signals.append(signal)
                    logger.debug(
                        f"Signal from '{strategy.name}': {signal.symbol} "
                        f"{signal.signal_type} credit=${signal.net_credit:.2f}"
                    )
            except Exception as e:
                logger.error(
                    f"Strategy '{strategy.name}' raised an exception "
                    f"for {snapshot.symbol}: {e}",
                    exc_info=True
                )

        if signals:
            logger.info(
                f"{snapshot.symbol}: {len(signals)} signal(s) generated "
                f"[{', '.join(s.strategy_name for s in signals)}]"
            )
        else:
            logger.debug(f"{snapshot.symbol}: no signals this cycle")

        return signals

    def evaluate_universe(
        self, snapshots: List[MarketSnapshot]
    ) -> List[TradeSignal]:
        """
        Evaluate all strategies against all snapshots in the universe.

        Args:
            snapshots: List of MarketSnapshots from MarketScanner.scan_universe()

        Returns:
            Flat list of all TradeSignals across all symbols.
        """
        all_signals = []

        for snapshot in snapshots:
            signals = self.evaluate(snapshot)
            all_signals.extend(signals)

        logger.info(
            f"Universe evaluation complete: "
            f"{len(snapshots)} symbols → {len(all_signals)} total signals"
        )
        return all_signals

    @property
    def strategy_names(self) -> List[str]:
        """Return names of all registered strategies."""
        return [s.name for s in self._strategies]

    @property
    def enabled_count(self) -> int:
        """Return count of enabled strategies."""
        return sum(1 for s in self._strategies if s.is_enabled)
