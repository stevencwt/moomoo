"""
Trade Manager
=============
Orchestrates the full lifecycle of a trade signal:

  Signal → PortfolioGuard → [Confirmation] → OrderRouter → PaperLedger → PortfolioGuard.record_open()

This is the single entry point for all order execution.
Nothing else in the system places orders — all execution flows through here.

Paper mode flow:
  1. Signal arrives from StrategyRegistry
  2. PortfolioGuard.approve() checks portfolio constraints
  3. OrderRouter.execute() simulates fill at mid-price
  4. PaperLedger.record_open() writes to SQLite
  5. PortfolioGuard.record_open() updates in-memory state
  6. Returns TradeResult

Live mode flow (additional step 3):
  1-2. Same as paper
  3.  [CONFIRMATION] Logs full trade details and requires explicit confirmation
  4.  OrderRouter.execute() places real MooMoo orders
  5-6. Same as paper

Live mode requires:
  config.mode = "live"
  The explicit confirmation step cannot be bypassed programmatically.
  In autonomous operation (Phase 6), confirmation is replaced by a final
  rule-based check rather than human input.
"""

from datetime import datetime
from dataclasses import dataclass, replace as dc_replace
from typing import List, Optional

from src.strategies.trade_signal import TradeSignal
from src.execution.portfolio_guard import PortfolioGuard
from src.execution.order_router import OrderRouter, FillResult
from src.execution.paper_ledger import PaperLedger
from src.execution.signal_ranker import SignalRanker
from src.exceptions import OrderError
from src.logger import get_logger

logger = get_logger("execution.trade_manager")


@dataclass
class TradeResult:
    """Outcome of a trade execution attempt."""
    signal:     TradeSignal
    approved:   bool
    executed:   bool
    trade_id:   Optional[int]    # PaperLedger ID (paper) or MooMoo order ID (live)
    fill:       Optional[FillResult]
    blocked_reason: Optional[str]   # Set when approved=False or executed=False
    timestamp:  datetime


class TradeManager:
    """
    Single entry point for all trade execution.
    """

    def __init__(
        self,
        config:  dict,
        guard:   PortfolioGuard,
        router:  OrderRouter,
        ledger:  PaperLedger,
    ):
        self._config   = config
        self._guard    = guard
        self._router   = router
        self._ledger   = ledger
        self._mode     = config.get("mode", "paper").lower()
        self._is_paper = (self._mode == "paper")
        self._ranker   = SignalRanker(config)

        logger.info(
            f"TradeManager initialised | mode={self._mode} | "
            f"ranker_enabled={self._ranker.is_enabled}"
        )

    # ── Public API ────────────────────────────────────────────────

    def process_signal(self, signal: TradeSignal, entry_type: str = "morning_scan", signal_score: Optional[float] = None) -> TradeResult:
        """
        Process a single TradeSignal through the full execution pipeline.

        Args:
            signal: TradeSignal from StrategyRegistry

        Returns:
            TradeResult indicating what happened.
        """
        logger.info(
            f"Processing signal: {signal.symbol} {signal.strategy_name} | "
            f"regime={signal.regime} | credit=${signal.net_credit:.2f} | "
            f"mode={self._mode}"
        )

        # ── Step 1: Portfolio Guard ───────────────────────────────
        approved, reason = self._guard.approve(signal)
        if not approved:
            return TradeResult(
                signal=         signal,
                approved=       False,
                executed=       False,
                trade_id=       None,
                fill=           None,
                blocked_reason= reason,
                timestamp=      datetime.now(),
            )

        # ── Step 2: Live mode confirmation ────────────────────────
        if not self._is_paper:
            confirmed = self._confirm_live_trade(signal)
            if not confirmed:
                return TradeResult(
                    signal=         signal,
                    approved=       True,
                    executed=       False,
                    trade_id=       None,
                    fill=           None,
                    blocked_reason= "live_confirmation_declined",
                    timestamp=      datetime.now(),
                )

        # ── Step 3: Execute order ─────────────────────────────────
        try:
            fill = self._router.execute(signal)
        except OrderError as e:
            logger.error(f"Order execution failed for {signal.symbol}: {e}")
            return TradeResult(
                signal=         signal,
                approved=       True,
                executed=       False,
                trade_id=       None,
                fill=           None,
                blocked_reason= f"order_error: {e}",
                timestamp=      datetime.now(),
            )

        if fill.status != "filled":
            return TradeResult(
                signal=         signal,
                approved=       True,
                executed=       False,
                trade_id=       None,
                fill=           fill,
                blocked_reason= f"fill_status: {fill.status}",
                timestamp=      datetime.now(),
            )

        # ── Step 4: Record in ledger ──────────────────────────────
        # Inject analytics fields that are only known at execution time:
        # signal_score comes from SignalRanker; entry_type from the calling job.
        # TradeSignal is frozen so we use dataclasses.replace() (zero side effects).
        signal = dc_replace(
            signal,
            signal_score = signal_score,
            entry_type   = entry_type,
        )
        trade_id = self._ledger.record_open(
            signal=    signal,
            fill_sell= fill.fill_sell,
            fill_buy=  fill.fill_buy,
        )

        # ── Step 5: Update portfolio guard state ──────────────────
        self._guard.record_open(signal)

        logger.info(
            f"✅ Trade executed: #{trade_id} | "
            f"{signal.symbol} {signal.strategy_name} | "
            f"credit=${fill.net_credit:.2f} | "
            f"mode={self._mode}"
        )

        return TradeResult(
            signal=         signal,
            approved=       True,
            executed=       True,
            trade_id=       trade_id,
            fill=           fill,
            blocked_reason= None,
            timestamp=      datetime.now(),
        )

    def process_signals(self, signals: List[TradeSignal], entry_type: str = "morning_scan") -> List[TradeResult]:
        """
        Process a batch of signals using the two-pass rank-then-execute approach.

        Pass 1 — Rank:
          SignalRanker scores all candidates and returns them sorted best-first.
          When signal_ranker.enabled=false the original order is preserved.

        Pass 2 — Execute:
          Walk the ranked list top-to-bottom.
          PortfolioGuard.approve() is called for each signal in rank order.
          The first N signals that pass all guard checks are executed, where N
          is determined by the guard's daily limit and position capacity.
          Signals that are blocked, or that would exceed the daily limit after
          it has already been reached, still get a TradeResult so the scheduler
          can display the full ranked outcome.

        Args:
            signals: All qualifying signals collected from scan_universe()

        Returns:
            List of TradeResults in rank order (best-ranked first).
            Every input signal gets exactly one result.
        """
        if not signals:
            return []

        # ── Pass 1: Rank all candidates ───────────────────────────
        ranked = self._ranker.rank(signals)

        if self._ranker.is_enabled:
            logger.info(
                f"SignalRanker: {len(ranked)} candidate(s) ranked | "
                f"top candidate: {ranked[0].signal.symbol} "
                f"{ranked[0].signal.strategy_name} score={ranked[0].score:.4f}"
            )
        else:
            logger.info(
                f"SignalRanker disabled — processing {len(signals)} signal(s) in FIFO order"
            )

        # ── Pass 2: Walk ranked list through the guard ────────────
        results = []
        for ranked_signal in ranked:
            result = self.process_signal(
                ranked_signal.signal,
                entry_type   = entry_type,
                signal_score = ranked_signal.score if self._ranker.is_enabled else None,
            )
            results.append(result)

        executed = sum(1 for r in results if r.executed)
        blocked  = sum(1 for r in results if not r.approved)
        skipped  = sum(1 for r in results if r.approved and not r.executed)

        logger.info(
            f"Batch complete: {len(signals)} signals | "
            f"{executed} executed | {blocked} blocked | {skipped} skipped"
        )
        return results

    def close_trade(
        self,
        trade_id:            int,
        close_price:         float,
        close_reason:        str,
        symbol:              str,
        strategy_name:       str,
        # ── Exit analytics ────────────────────────────────────────
        spot_price_at_close: Optional[float] = None,
        dte_at_close:        Optional[int]   = None,
        iv_rank_at_close:    Optional[float] = None,
        vix_at_close:        Optional[float] = None,
        atm_iv_at_close:     Optional[float] = None,
        rsi_at_close:        Optional[float] = None,
        pct_b_at_close:      Optional[float] = None,
        commission:          float = 0.0,
    ) -> float:
        """
        Close an existing paper trade.

        Args:
            trade_id:             PaperLedger trade ID
            close_price:          Net debit to close (0 if expired worthless)
            close_reason:         "expired_worthless"|"stop_loss"|"take_profit"|"dte_close"|"manual"
            symbol:               MooMoo symbol for portfolio guard update
            strategy_name:        Strategy that opened the position
            spot_price_at_close:  Underlying spot price at close
            dte_at_close:         Days to expiry remaining at close
            iv_rank_at_close:     IV Rank at close (0–100)
            vix_at_close:         VIX at close
            atm_iv_at_close:      Raw ATM IV % at close (for IV crush measurement)
            rsi_at_close:         RSI(14) at close
            pct_b_at_close:       Bollinger %B at close
            commission:           Brokerage commission (default 0.0 for paper trading)

        Returns:
            Realised gross P&L in dollars.
        """
        pnl = self._ledger.record_close(
            trade_id,
            close_price,
            close_reason,
            spot_price_at_close = spot_price_at_close,
            dte_at_close        = dte_at_close,
            iv_rank_at_close    = iv_rank_at_close,
            vix_at_close        = vix_at_close,
            atm_iv_at_close     = atm_iv_at_close,
            rsi_at_close        = rsi_at_close,
            pct_b_at_close      = pct_b_at_close,
            commission          = commission,
        )
        self._guard.record_close(symbol, strategy_name)

        action = "✅ profit" if pnl > 0 else "❌ loss"
        logger.info(
            f"Trade #{trade_id} closed | {action} | P&L=${pnl:+.2f} | "
            f"reason={close_reason}"
        )
        return pnl

    def get_portfolio_summary(self) -> dict:
        """Return current portfolio state for monitoring."""
        stats = self._ledger.get_statistics()
        return {
            "mode":               self._mode,
            "open_positions":     self._guard.open_position_count,
            "trades_today":       self._guard.trades_today,
            "available_capacity": self._guard.available_capacity,
            "committed_risk":     self._guard.total_committed_risk,
            "paper_stats":        stats,
        }

    # ── Private Helpers ───────────────────────────────────────────

    def _confirm_live_trade(self, signal: TradeSignal) -> bool:
        """
        Log full trade details for live confirmation.
        In autonomous mode (Phase 6), this becomes a rule-based check.
        During Phase 4/5 testing, this ALWAYS returns False to prevent
        accidental live orders during development.

        Returns:
            True = proceed with live order
            False = abort (safe default during development)
        """
        logger.warning(
            f"[LIVE CONFIRMATION REQUIRED]\n"
            f"  Symbol:      {signal.symbol}\n"
            f"  Strategy:    {signal.strategy_name}\n"
            f"  Sell:        {signal.sell_contract} @ ${signal.sell_price:.2f}\n"
            f"  Buy:         {signal.buy_contract or 'N/A'} @ "
            f"${signal.buy_price:.2f}" if signal.buy_price else
            f"  Buy:         N/A\n"
            f"  Net Credit:  ${signal.net_credit:.2f}\n"
            f"  Max Loss:    ${signal.max_loss:.0f}" if signal.max_loss else
            f"  Max Loss:    N/A (covered)\n"
            f"  Expiry:      {signal.expiry} (DTE={signal.dte})\n"
            f"  Regime:      {signal.regime}\n"
            f"  IV Rank:     {signal.iv_rank:.0f}\n"
            f"  Delta:       {signal.delta:.2f}\n"
            f"  Reason:      {signal.reason}\n"
        )

        # SAFETY: Always returns False during phases 3-5
        # Phase 6 will replace this with autonomous confirmation logic
        logger.warning(
            "Live confirmation: DECLINED (development mode — "
            "Phase 6 implements autonomous confirmation)"
        )
        return False
