"""
Position Monitor
================
Background component that monitors all open paper positions and triggers
exits when conditions are met.

Responsibilities:
  1. Load all open trades from PaperLedger on each cycle
  2. Fetch current mid-price for each open position from MooMoo
  3. Run ExitEvaluator against each position
  4. If exit triggered: call TradeManager.close_trade()
  5. Log position status summary after each cycle

Monitoring cycle:
  - During market hours (9:30 AM - 4:00 PM ET): every check_interval_minutes
  - Outside market hours: skip (options data unavailable)
  - Overnight: options expire — check expiry at 9:30 AM on expiry date

Price retrieval:
  - For covered calls: get_option_snapshot([sell_contract])
  - For spreads: get_option_snapshot([sell_contract, buy_contract])
    Net debit to close = buy_to_close - sell_to_close (reversed legs)

Fallback:
  - If price unavailable → skip position (do not close based on stale data)
  - Log the skip as a warning
  - After 3 consecutive price failures → alert via logger

Configuration (under position_monitor):
  check_interval_minutes : 30    (how often to run during market hours)
  exit_rules:
    stop_loss_multiplier : 2.0
    take_profit_pct      : 0.50
    dte_close_threshold  : 21
"""

from datetime import datetime, time as dtime
from typing import List, Dict, Optional
from zoneinfo import ZoneInfo

from src.execution.paper_ledger import PaperLedger
from src.execution.trade_manager import TradeManager
from src.connectors.connector_protocol import BrokerConnector
from src.monitoring.exit_evaluator import ExitEvaluator, ExitDecision
from src.logger import get_logger

logger = get_logger("monitoring.position_monitor")

# US Eastern Time zone
ET = ZoneInfo("America/New_York")
MARKET_OPEN  = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)


class PositionMonitor:
    """
    Monitors open positions and triggers exits when conditions are met.
    Designed to be called periodically by the scheduler (Phase 6).
    """

    def __init__(
        self,
        config:        dict,
        ledger:        PaperLedger,
        trade_manager: TradeManager,
        moomoo:        BrokerConnector,
        evaluator:     ExitEvaluator,
    ):
        self._config        = config
        self._ledger        = ledger
        self._trade_manager = trade_manager
        self._moomoo        = moomoo
        self._evaluator     = evaluator

        # Track consecutive price fetch failures per trade
        self._price_failures: Dict[int, int] = {}
        self._max_failures    = config.get(
            "position_monitor", {}
        ).get("max_price_failures", 3)

        logger.info("PositionMonitor initialised")

    # ── Public API ────────────────────────────────────────────────

    def run_cycle(self, force: bool = False) -> List[Dict]:
        """
        Run one monitoring cycle: check all open positions for exits.

        Args:
            force: If True, skip market hours check (useful for testing
                   and end-of-day forced expiry checks)

        Returns:
            List of action dicts for positions that were closed:
            [{trade_id, symbol, strategy, reason, pnl, close_price}]
        """
        if not force and not self._is_market_hours():
            logger.debug("Outside market hours — skipping monitor cycle")
            return []

        open_trades = self._ledger.get_open_trades()

        if not open_trades:
            logger.debug("No open positions to monitor")
            return []

        logger.info(f"Monitor cycle: checking {len(open_trades)} open position(s)")
        actions = []

        for trade in open_trades:
            action = self._check_position(trade)
            if action:
                actions.append(action)

        if actions:
            logger.info(
                f"Monitor cycle complete: {len(actions)} exit(s) triggered | "
                f"still open: {len(open_trades) - len(actions)}"
            )
        else:
            logger.info(
                f"Monitor cycle complete: all {len(open_trades)} position(s) held"
            )

        return actions

    def get_position_summary(self) -> List[Dict]:
        """
        Return current status of all open positions with unrealised P&L.
        Used for dashboard/reporting without triggering exits.

        Returns:
            List of position dicts with current_price and unrealised_pnl fields.
        """
        open_trades = self._ledger.get_open_trades()
        summary     = []

        for trade in open_trades:
            current_price = self._fetch_current_price(trade)
            if current_price is None:
                summary.append({**trade, "current_price": None,
                                 "unrealised_pnl": None})
                continue

            decision = self._evaluator.evaluate(
                trade_id=      trade["id"],
                net_credit=    trade["net_credit"],
                max_profit=    trade["net_credit"] * 100,
                expiry=        trade["expiry"],
                current_price= current_price,
            )
            summary.append({
                **trade,
                "current_price":  current_price,
                "unrealised_pnl": decision.unrealised_pnl,
                "pnl_pct":        decision.pnl_pct,
                "exit_signal":    decision.reason if decision.should_exit else None,
            })

        return summary


    # ── Public API (regime exit) ─────────────────────

    def close_all_regime_shift(self, symbol: Optional[str] = None) -> List[Dict]:
        """
        Force-close all open positions due to an exit mandate from the regime module.

        Called when RegimeManager.get_current_regime()["exit_mandate"] is True.
        Per spec Section 5.6: immediate close, no grace period, no price threshold checks.

        Args:
            symbol: If provided, only close positions for this symbol.
                    If None, close ALL open positions across all symbols.

        Returns:
            List of action dicts (same format as run_cycle).
        """
        open_trades = self._ledger.get_open_trades()
        if symbol:
            open_trades = [t for t in open_trades if t["symbol"] == symbol]

        if not open_trades:
            return []

        scope = f"symbol={symbol}" if symbol else "ALL symbols"
        logger.warning(
            f"[REGIME SHIFT] Exit mandate fired \u2014 force-closing "
            f"{len(open_trades)} position(s) ({scope})"
        )

        actions = []
        for trade in open_trades:
            current_price = self._fetch_current_price(trade)
            if current_price is None:
                # Do not skip on regime shift \u2014 use fallback price
                current_price = float(trade.get("net_credit", 0))
                logger.warning(
                    f"[REGIME SHIFT] Trade #{trade['id']} price unavailable \u2014 "
                    f"using fallback ${current_price:.2f}"
                )

            spot_at_close = None
            dte_remaining = None
            try:
                from datetime import date as _date
                dte_remaining = max(
                    0, (_date.fromisoformat(trade["expiry"]) - _date.today()).days
                )
            except Exception:
                pass
            try:
                # get_spot_price() works on both MooMoo and IBKR connectors
                _spot = self._moomoo.get_spot_price(trade["symbol"])
                spot_at_close = float(_spot) if _spot and _spot > 0 else None
            except Exception:
                pass

            pnl = self._trade_manager.close_trade(
                trade_id=            trade["id"],
                close_price=         current_price,
                close_reason=        "regime_shift",
                symbol=              trade["symbol"],
                strategy_name=       trade["strategy_name"],
                spot_price_at_close= spot_at_close,
                dte_at_close=        dte_remaining,
                iv_rank_at_close=    None,
                vix_at_close=        None,
                atm_iv_at_close=     None,
                rsi_at_close=        None,
                pct_b_at_close=      None,
            )

            action = {
                "trade_id":    trade["id"],
                "symbol":      trade["symbol"],
                "strategy":    trade["strategy_name"],
                "reason":      "regime_shift",
                "close_price": current_price,
                "pnl":         pnl,
                "urgency":     "immediate",
            }
            logger.warning(
                f"[REGIME SHIFT] CLOSED: #{trade['id']} {trade['symbol']} "
                f"{trade['strategy_name']} | P&L=${pnl:+.2f}"
            )
            actions.append(action)

        return actions

    # ── Private Methods ───────────────────────────────────────────

    def _check_position(self, trade: Dict) -> Optional[Dict]:
        """
        Check a single position for exit conditions.

        Returns action dict if exit triggered, None if holding.
        """
        trade_id = trade["id"]

        # Fetch current price
        current_price = self._fetch_current_price(trade)
        if current_price is None:
            self._price_failures[trade_id] = (
                self._price_failures.get(trade_id, 0) + 1
            )
            if self._price_failures[trade_id] >= self._max_failures:
                logger.error(
                    f"Trade #{trade_id} [{trade['symbol']}]: "
                    f"{self._price_failures[trade_id]} consecutive price failures — "
                    f"manual review required"
                )
            else:
                logger.warning(
                    f"Trade #{trade_id} [{trade['symbol']}]: "
                    f"price unavailable — skipping (failure "
                    f"{self._price_failures.get(trade_id, 0)}/{self._max_failures})"
                )
            return None

        # Reset failure counter on successful price fetch
        self._price_failures.pop(trade_id, None)

        # Evaluate exit rules
        decision = self._evaluator.evaluate(
            trade_id=      trade_id,
            net_credit=    trade["net_credit"],
            max_profit=    trade["net_credit"] * 100,
            expiry=        trade["expiry"],
            current_price= current_price,
        )

        if not decision.should_exit:
            return None

        # Determine close_reason for ledger
        close_reason = self._map_reason(decision.reason)

        # Execute close via TradeManager
        # Capture exit analytics: spot price, IV rank, VIX at close time
        close_price       = current_price if decision.reason != "expired_worthless" else 0.0
        spot_at_close     = None
        dte_remaining     = None
        iv_rank_at_close  = None
        vix_at_close      = None
        atm_iv_at_close   = None
        rsi_at_close      = None
        pct_b_at_close    = None

        try:
            from datetime import date as _date
            dte_remaining = max(0, (_date.fromisoformat(trade["expiry"]) - _date.today()).days)
        except Exception:
            pass

        try:
            # get_spot_price() works on both MooMoo and IBKR connectors
            _spot = self._moomoo.get_spot_price(trade["symbol"])
            spot_at_close = float(_spot) if _spot and _spot > 0 else None
        except Exception:
            pass

        try:
            iv_snap = getattr(self, "_iv_calculator", None)
            if iv_snap is not None and hasattr(iv_snap, "get_iv_rank"):
                _iv_val = trade.get("atm_iv_at_open")
                if _iv_val:
                    iv_rank_at_close, _ = iv_snap.get_iv_rank(trade["symbol"], _iv_val)
        except Exception:
            pass

        pnl = self._trade_manager.close_trade(
            trade_id=            trade_id,
            close_price=         close_price,
            close_reason=        close_reason,
            symbol=              trade["symbol"],
            strategy_name=       trade["strategy_name"],
            spot_price_at_close= spot_at_close,
            dte_at_close=        dte_remaining,
            iv_rank_at_close=    iv_rank_at_close,
            vix_at_close=        vix_at_close,
            atm_iv_at_close=     atm_iv_at_close,
            rsi_at_close=        rsi_at_close,
            pct_b_at_close=      pct_b_at_close,
        )

        action = {
            "trade_id":    trade_id,
            "symbol":      trade["symbol"],
            "strategy":    trade["strategy_name"],
            "reason":      decision.reason,
            "close_price": close_price,
            "pnl":         pnl,
            "urgency":     decision.close_urgency,
        }
        logger.info(
            f"EXIT: #{trade_id} {trade['symbol']} {trade['strategy_name']} | "
            f"reason={decision.reason} | P&L=${pnl:+.2f} | "
            f"urgency={decision.close_urgency}"
        )
        return action

    def _fetch_current_price(self, trade: Dict) -> Optional[float]:
        """
        Fetch the current mid-price to close a position.

        For covered calls: price of the short call (cost to buy back)
        For spreads: net debit = buy_back_short - sell_back_long
                     (reversed: buy the short back, sell the long)

        Returns None if data unavailable.
        """
        sell_contract = trade["sell_contract"]
        buy_contract  = trade.get("buy_contract")

        try:
            contracts = [sell_contract]
            if buy_contract:
                contracts.append(buy_contract)

            snap = self._moomoo.get_option_snapshot(contracts)
            if snap is None or len(snap) == 0:
                return None

            # Get sell-side price (cost to buy back the short)
            sell_row = snap[snap["code"] == sell_contract]
            if len(sell_row) == 0:
                return None
            sell_close_price = float(
                sell_row.iloc[0].get("mid_price",
                sell_row.iloc[0].get("ask_price", 0))
            )

            if not buy_contract:
                # Single leg — current price = cost to buy back
                return sell_close_price

            # Spread — net debit = buy_back_short - sell_back_long
            buy_row = snap[snap["code"] == buy_contract]
            if len(buy_row) == 0:
                return None
            buy_close_price = float(
                buy_row.iloc[0].get("mid_price",
                buy_row.iloc[0].get("bid_price", 0))
            )

            # Net debit to close spread = buy back short - sell back long
            net_debit = sell_close_price - buy_close_price
            return max(0.0, net_debit)

        except Exception as e:
            logger.warning(
                f"Failed to fetch price for trade #{trade['id']}: {e}"
            )
            return None

    @staticmethod
    def _map_reason(exit_reason: str) -> str:
        """Map exit evaluator reason to PaperLedger close_reason."""
        mapping = {
            "expired_worthless": "expired_worthless",
            "expired":           "manual",          # ITM expiry — manual handling
            "stop_loss":         "stop_loss",
            "take_profit":       "take_profit",
            "dte_close":         "take_profit",     # Treat as take_profit for ledger
            "regime_shift":      "regime_shift",    # HMM exit mandate
        }
        return mapping.get(exit_reason, "manual")

    @staticmethod
    def _is_market_hours() -> bool:
        """Return True if current time is within US market hours (ET)."""
        now_et = datetime.now(ET).time()
        return MARKET_OPEN <= now_et <= MARKET_CLOSE
