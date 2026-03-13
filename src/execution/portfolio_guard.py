"""
Portfolio Guard
===============
Final safety gate before any order reaches the broker.
Enforces portfolio-level constraints that individual strategies cannot see.

Checks performed (in order):
  1. Max concurrent positions across ALL symbols and strategies
  2. Max total risk capital committed (sum of all open max_loss values)
  3. Max risk per single trade as % of portfolio value
  4. Duplicate signal detection (same symbol + strategy already has open position)
  5. Daily trade limit (max new positions per calendar day)

Design principles:
  - Strategies enforce their OWN entry criteria (IV rank, RSI, regime, etc.)
  - PortfolioGuard enforces PORTFOLIO-LEVEL constraints orthogonally
  - A signal passing all strategy gates can still be blocked here
  - All decisions are logged with explicit reasons

Configuration (under portfolio_guard in config):
  max_open_positions   : 6     total positions across all symbols
  max_risk_pct         : 0.05  max 5% of portfolio value at risk on any single trade
  max_total_risk_pct   : 0.20  max 20% of portfolio value at risk total
  max_trades_per_day   : 3     max new positions opened per calendar day
  portfolio_value      : 100000  current portfolio value in USD (updated periodically)
"""

from datetime import date
from typing import List, Optional

from src.strategies.trade_signal import TradeSignal
from src.logger import get_logger

logger = get_logger("execution.portfolio_guard")


class PortfolioGuard:
    """
    Enforces portfolio-level risk constraints.
    Stateful: tracks open positions and daily trade count.
    """

    def __init__(self, config: dict):
        cfg = config.get("portfolio_guard", {})

        self._max_open_positions  = cfg.get("max_open_positions",  6)
        self._max_risk_pct        = cfg.get("max_risk_pct",        0.05)
        self._max_total_risk_pct  = cfg.get("max_total_risk_pct",  0.20)
        self._max_trades_per_day  = cfg.get("max_trades_per_day",  3)
        self._portfolio_value     = cfg.get("portfolio_value",     100_000)

        # Runtime state — updated by TradeManager as orders are placed/closed
        self._open_positions:    List[dict] = []   # [{symbol, strategy, max_loss, opened_date}]
        self._trades_today:      int        = 0
        self._last_reset_date:  Optional[date] = date.today()

        logger.info(
            f"PortfolioGuard initialised | "
            f"max_positions={self._max_open_positions} | "
            f"max_risk_pct={self._max_risk_pct:.0%} | "
            f"max_total_risk_pct={self._max_total_risk_pct:.0%} | "
            f"max_trades/day={self._max_trades_per_day} | "
            f"portfolio=${self._portfolio_value:,.0f}"
        )

    # ── Startup Restore ──────────────────────────────────────────

    def restore_from_ledger(self, ledger) -> None:
        """
        Reload open positions from the ledger on bot startup.

        Called once during BotScheduler.build() so the guard has accurate
        state after a restart — prevents duplicate trades being placed for
        positions that are already open from a previous session.

        Args:
            ledger: PaperLedger instance (or any object with get_open_trades())
        """
        today     = date.today()
        today_str = today.isoformat()

        # Load currently open positions into the guard
        open_trades = ledger.get_open_trades()
        restored = 0
        for trade in open_trades:
            self._open_positions.append({
                "symbol":      trade["symbol"],
                "strategy":    trade["strategy_name"],
                "max_loss":    float(trade.get("max_loss") or 0),
                "opened_date": today,
            })
            restored += 1

        # Count ALL trades opened today toward the daily limit — including
        # any that were subsequently stopped out or closed.  This prevents
        # re-entry on the same symbol after a stop loss on the same day.
        all_today = ledger.get_trades_opened_on(today_str)
        self._trades_today = len(all_today)

        if restored or self._trades_today:
            logger.info(
                f"PortfolioGuard restored {restored} open position(s) from ledger | "
                f"trades today={self._trades_today} (incl. closed/stopped)"
            )
        else:
            logger.info("PortfolioGuard restore: no trades today, no open positions")

    # ── Public API ────────────────────────────────────────────────

    def approve(self, signal: TradeSignal) -> tuple[bool, str]:
        """
        Evaluate all portfolio-level constraints for a signal.

        Args:
            signal: TradeSignal from a strategy

        Returns:
            Tuple of (approved: bool, reason: str)
            If approved=False, reason explains which constraint was violated.
        """
        self._reset_daily_counter_if_needed()

        # Check 1: Total open positions
        if len(self._open_positions) >= self._max_open_positions:
            reason = (
                f"Max open positions reached: "
                f"{len(self._open_positions)}/{self._max_open_positions}"
            )
            logger.info(f"BLOCKED [{signal.symbol}]: {reason}")
            return False, reason

        # Check 2: Daily trade limit
        if self._trades_today >= self._max_trades_per_day:
            reason = (
                f"Daily trade limit reached: "
                f"{self._trades_today}/{self._max_trades_per_day}"
            )
            logger.info(f"BLOCKED [{signal.symbol}]: {reason}")
            return False, reason

        # Check 3: Per-trade risk limit
        if signal.max_loss is not None:
            risk_pct = signal.max_loss / self._portfolio_value
            if risk_pct > self._max_risk_pct:
                reason = (
                    f"Per-trade risk too high: "
                    f"${signal.max_loss:.0f} = {risk_pct:.1%} > "
                    f"max {self._max_risk_pct:.0%} of portfolio "
                    f"(${self._portfolio_value:,.0f})"
                )
                logger.info(f"BLOCKED [{signal.symbol}]: {reason}")
                return False, reason

        # Check 4: Total portfolio risk
        current_risk  = self._total_committed_risk()
        new_risk       = signal.max_loss or 0
        total_risk_pct = (current_risk + new_risk) / self._portfolio_value
        if total_risk_pct > self._max_total_risk_pct:
            reason = (
                f"Total portfolio risk would exceed limit: "
                f"${current_risk + new_risk:.0f} = {total_risk_pct:.1%} > "
                f"max {self._max_total_risk_pct:.0%}"
            )
            logger.info(f"BLOCKED [{signal.symbol}]: {reason}")
            return False, reason

        # Check 5: Duplicate (same symbol + same strategy already open)
        duplicate = self._find_duplicate(signal)
        if duplicate:
            reason = (
                f"Duplicate position: {signal.symbol} {signal.strategy_name} "
                f"already open (opened {duplicate['opened_date']})"
            )
            logger.info(f"BLOCKED [{signal.symbol}]: {reason}")
            return False, reason

        # Check 6: Iron condor prevention — don't hold opposing spreads on the same symbol.
        # A bear call spread + bull put spread on the same symbol forms an iron condor,
        # which requires combined Greeks management not yet implemented. Keep them separate.
        opposing = self._find_opposing_spread(signal)
        if opposing:
            reason = (
                f"Iron condor prevention: {signal.symbol} already has an open "
                f"{opposing['strategy']} position. Managing both sides on the same symbol "
                f"requires iron condor logic not yet implemented. Use different symbols."
            )
            logger.info(f"BLOCKED [{signal.symbol}]: {reason}")
            return False, reason

        logger.info(
            f"APPROVED [{signal.symbol}]: {signal.strategy_name} | "
            f"open_positions={len(self._open_positions)} | "
            f"trades_today={self._trades_today} | "
            f"risk={signal.max_loss or 0:.0f}"
        )
        return True, "approved"

    def record_open(self, signal: TradeSignal) -> None:
        """
        Register a new open position.
        Call this AFTER a successful order placement.

        Args:
            signal: The TradeSignal that was executed
        """
        self._open_positions.append({
            "symbol":      signal.symbol,
            "strategy":    signal.strategy_name,
            "max_loss":    signal.max_loss or 0,
            "signal_type": signal.signal_type,
            "opened_date": date.today(),
            "sell_contract": signal.sell_contract,
        })
        self._trades_today += 1

        logger.info(
            f"Position recorded: {signal.symbol} {signal.strategy_name} | "
            f"open={len(self._open_positions)} | today={self._trades_today}"
        )

    def record_close(self, symbol: str, strategy_name: str) -> None:
        """
        Remove a closed position from tracking.
        Call this AFTER a successful close order.

        Args:
            symbol       : MooMoo format e.g. "US.TSLA"
            strategy_name: Strategy that opened the position
        """
        before = len(self._open_positions)
        self._open_positions = [
            p for p in self._open_positions
            if not (p["symbol"] == symbol and p["strategy"] == strategy_name)
        ]
        removed = before - len(self._open_positions)
        if removed:
            logger.info(
                f"Position closed: {symbol} {strategy_name} | "
                f"remaining={len(self._open_positions)}"
            )
        else:
            logger.warning(
                f"record_close called for {symbol}/{strategy_name} "
                f"but no matching open position found"
            )

    def update_portfolio_value(self, value: float) -> None:
        """Update the portfolio value used for risk % calculations."""
        old = self._portfolio_value
        self._portfolio_value = value
        logger.info(f"Portfolio value updated: ${old:,.0f} → ${value:,.0f}")

    @property
    def open_position_count(self) -> int:
        return len(self._open_positions)

    @property
    def trades_today(self) -> int:
        return self._trades_today

    @property
    def total_committed_risk(self) -> float:
        return self._total_committed_risk()

    @property
    def available_capacity(self) -> int:
        """Positions that can still be opened today."""
        return min(
            self._max_open_positions  - len(self._open_positions),
            self._max_trades_per_day  - self._trades_today
        )

    # ── Private Helpers ───────────────────────────────────────────

    def _total_committed_risk(self) -> float:
        return sum(p["max_loss"] for p in self._open_positions)

    def _find_duplicate(self, signal: TradeSignal) -> Optional[dict]:
        for p in self._open_positions:
            if p["symbol"] == signal.symbol and p["strategy"] == signal.strategy_name:
                return p
        return None

    def _find_opposing_spread(self, signal: TradeSignal) -> Optional[dict]:
        """
        Check whether the same symbol already has the opposing spread direction open.
        Prevents accidental iron condor formation without combined management.

        bear_call_spread opposes bull_put_spread and vice versa.
        """
        opposing_map = {
            "bear_call_spread": "bull_put_spread",
            "bull_put_spread":  "bear_call_spread",
        }
        opposing_name = opposing_map.get(signal.strategy_name)
        if opposing_name is None:
            return None   # covered_call has no opposing spread
        for p in self._open_positions:
            if p["symbol"] == signal.symbol and p["strategy"] == opposing_name:
                return p
        return None

    def _reset_daily_counter_if_needed(self) -> None:
        today = date.today()
        if self._last_reset_date != today:
            if self._trades_today > 0:
                logger.info(
                    f"New trading day — resetting daily counter "
                    f"(was {self._trades_today})"
                )
            self._trades_today      = 0
            self._last_reset_date  = today
