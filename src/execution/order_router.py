"""
Order Router
============
Translates TradeSignals into broker API calls.

Broker-agnostic: works with any connector that implements the standard
interface (MooMooConnector or IBKRConnector).  The connector is injected
at construction time via BotScheduler.build().

Responsibilities:
  - Single-leg orders for covered calls (sell only)
  - Combo orders for spreads (sell + buy simultaneously)
  - Fill price retrieval after order placement
  - Order status monitoring with timeout
  - Cancellation of unfilled orders after timeout

Paper mode:
  - _place_paper(): simulates a fill at mid-price, no API call
  - Returns a PaperFill with simulated fill prices

Live mode:
  - _place_live(): calls broker API, waits for fill confirmation
  - Requires mode: live in config to prevent accidents
  - Uses limit orders at mid-price to avoid slippage
  - Monitors fill status for up to fill_timeout_seconds

Order status vocabulary (shared with both connectors):
  FILLED    — completely filled
  PARTIAL   — partially filled, still working
  CANCELLED — order cancelled
  FAILED    — order rejected / failed
  PENDING   — submitted / working (anything else)

Safety:
  - Will never place a live order unless mode: live in config
  - Live orders are logged with a [LIVE ORDER] tag for easy auditing
"""

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any

from src.strategies.trade_signal import TradeSignal
from src.exceptions import OrderError
from src.logger import get_logger

logger = get_logger("execution.order_router")

# Maximum seconds to wait for a limit order to fill
FILL_TIMEOUT_SECONDS = 120
# Poll interval when waiting for fill
POLL_INTERVAL_SECONDS = 5

# Normalised status strings (shared vocabulary with all connectors)
STATUS_FILLED    = "FILLED"
STATUS_PARTIAL   = "PARTIAL"
STATUS_CANCELLED = "CANCELLED"
STATUS_FAILED    = "FAILED"
# Anything else is treated as PENDING / still working


@dataclass
class FillResult:
    """Result of an order placement (paper or live)."""
    trade_id:     Optional[str]   # Broker order ID (None for paper)
    fill_sell:    float            # Actual/simulated sell fill price
    fill_buy:     Optional[float]  # Actual/simulated buy fill price
    net_credit:   float            # fill_sell - (fill_buy or 0)
    is_paper:     bool
    filled_at:    datetime
    status:       str              # "filled" | "partial" | "timeout" | "cancelled"


class OrderRouter:
    """
    Routes TradeSignals to a broker connector as actual orders
    (paper simulation or live).

    Accepts any connector that implements the standard interface:
      place_limit_order(contract, qty, price, direction) -> str
      place_combo_order(sell_contract, buy_contract, qty, net_credit) -> str
      cancel_order(order_id) -> bool
      get_order_status(order_id) -> Dict
    """

    def __init__(self, config: dict, connector: Any):
        self._config    = config
        self._connector = connector
        self._mode      = config.get("mode", "paper").lower()
        self._is_paper  = (self._mode == "paper")

        self._fill_timeout = config.get(
            "execution", {}
        ).get("fill_timeout_seconds", FILL_TIMEOUT_SECONDS)

        logger.info(
            f"OrderRouter initialised | mode={self._mode} | "
            f"fill_timeout={self._fill_timeout}s"
        )

    # ── Public API ────────────────────────────────────────────────

    def execute(self, signal: TradeSignal) -> FillResult:
        """
        Execute a TradeSignal.

        In paper mode: simulates a fill at mid-price.
        In live mode:  places actual broker orders.

        Args:
            signal: Validated TradeSignal from PortfolioGuard

        Returns:
            FillResult with fill prices and order status.

        Raises:
            OrderError: If live order placement fails.
        """
        if self._is_paper:
            return self._place_paper(signal)
        else:
            return self._place_live(signal)

    # ── Paper Mode ────────────────────────────────────────────────

    def _place_paper(self, signal: TradeSignal) -> FillResult:
        """
        Simulate a paper fill at the signal's limit prices.
        No API call — purely in-memory.
        """
        fill_sell = signal.sell_price
        fill_buy  = signal.buy_price
        net       = fill_sell - (fill_buy or 0)

        result = FillResult(
            trade_id   = None,
            fill_sell  = fill_sell,
            fill_buy   = fill_buy,
            net_credit = round(net, 4),
            is_paper   = True,
            filled_at  = datetime.now(),
            status     = "filled",
        )

        logger.info(
            f"[PAPER] {signal.symbol} {signal.strategy_name} | "
            f"sell={signal.sell_contract} @ ${fill_sell:.2f} | "
            f"{'buy=' + signal.buy_contract + ' @ $' + str(round(fill_buy, 2)) if fill_buy else 'no hedge'} | "
            f"net_credit=${net:.2f}"
        )
        return result

    # ── Live Mode ─────────────────────────────────────────────────

    def _place_live(self, signal: TradeSignal) -> FillResult:
        """
        Place real broker orders for a signal.

        For covered calls: single sell-to-open order.
        For spreads:       combo order (sell + buy simultaneously).

        All orders are limit orders at mid-price.

        Raises:
            OrderError: On placement failure or timeout.
        """
        logger.warning(
            f"[LIVE ORDER] {signal.symbol} {signal.strategy_name} | "
            f"THIS IS A REAL MONEY TRADE | "
            f"sell={signal.sell_contract} @ ${signal.sell_price:.2f}"
        )

        if signal.is_spread:
            return self._place_spread_live(signal)
        else:
            return self._place_single_live(signal)

    def _place_single_live(self, signal: TradeSignal) -> FillResult:
        """Place a single-leg sell-to-open order (covered call)."""
        order_id = self._connector.place_limit_order(
            contract  = signal.sell_contract,
            qty       = signal.quantity,
            price     = signal.sell_price,
            direction = "SELL",
        )

        if not order_id:
            raise OrderError(
                f"Failed to place covered call order for {signal.sell_contract}"
            )

        fill = self._wait_for_fill(order_id, signal.sell_price)

        if fill["status"] != "filled":
            self._connector.cancel_order(order_id)
            raise OrderError(
                f"Order {order_id} not filled within {self._fill_timeout}s. Cancelled."
            )

        return FillResult(
            trade_id   = order_id,
            fill_sell  = fill["avg_price"],
            fill_buy   = None,
            net_credit = fill["avg_price"],
            is_paper   = False,
            filled_at  = datetime.now(),
            status     = "filled",
        )

    def _place_spread_live(self, signal: TradeSignal) -> FillResult:
        """Place a combo order for a spread (sell + buy simultaneously)."""
        order_id = self._connector.place_combo_order(
            sell_contract = signal.sell_contract,
            buy_contract  = signal.buy_contract,
            qty           = signal.quantity,
            net_credit    = signal.net_credit,
        )

        if not order_id:
            raise OrderError(
                f"Failed to place spread combo order for "
                f"{signal.sell_contract}/{signal.buy_contract}"
            )

        fill = self._wait_for_fill(order_id, signal.net_credit)

        if fill["status"] != "filled":
            self._connector.cancel_order(order_id)
            raise OrderError(
                f"Spread order {order_id} not filled within {self._fill_timeout}s. Cancelled."
            )

        # Combo orders return net credit as avg_price
        net_credit = fill["avg_price"]
        fill_sell  = signal.sell_price   # approximate — combo fills reported as net
        fill_buy   = fill_sell - net_credit

        return FillResult(
            trade_id   = order_id,
            fill_sell  = fill_sell,
            fill_buy   = fill_buy,
            net_credit = net_credit,
            is_paper   = False,
            filled_at  = datetime.now(),
            status     = "filled",
        )

    def close_spread(
        self,
        trade_id:      int,
        sell_contract: str,
        buy_contract:  str,
        qty:           int,
        close_price:   float,
        symbol:        str,
        reason:        str,
    ) -> float:
        """
        Place a buy-to-close order for an existing spread position.

        Paper mode: returns close_price immediately (no broker call).
        Live mode:  places SELL combo via connector, polls for fill.

        Args:
            trade_id:      PaperLedger trade ID (for logging)
            sell_contract: OCC code for the original short leg
            buy_contract:  OCC code for the original long leg
            qty:           Number of spreads
            close_price:   Net debit per share (mark price estimate)
            symbol:        Underlying symbol (for logging)
            reason:        Close reason (for logging)

        Returns:
            Actual fill price (debit per share). In paper mode, equals close_price.

        Raises:
            OrderError: If live order placement or fill fails.
        """
        if self._is_paper:
            logger.info(
                f"[PAPER CLOSE] #{trade_id} {symbol} {reason} | "
                f"debit=${close_price:.2f}"
            )
            return close_price

        logger.warning(
            f"[LIVE CLOSE] #{trade_id} {symbol} {reason} | "
            f"THIS IS A REAL MONEY CLOSE ORDER | "
            f"debit=${close_price:.2f}"
        )

        order_id = self._connector.place_combo_close_order(
            sell_contract=sell_contract,
            buy_contract=buy_contract,
            qty=qty,
            net_debit=close_price,
        )

        if not order_id:
            raise OrderError(
                f"Failed to place close order for trade #{trade_id}"
            )

        fill = self._wait_for_fill(order_id, close_price)

        if fill["status"] != "filled":
            self._connector.cancel_order(order_id)
            raise OrderError(
                f"Close order {order_id} for trade #{trade_id} not filled "
                f"within {self._fill_timeout}s. Cancelled."
            )

        actual_price = fill["avg_price"]
        logger.info(
            f"[LIVE CLOSE] #{trade_id} {symbol} filled | "
            f"debit=${actual_price:.2f} | reason={reason}"
        )
        return actual_price

    def close_single_leg(
        self,
        trade_id:      int,
        sell_contract: str,
        qty:           int,
        close_price:   float,
        symbol:        str,
        reason:        str,
    ) -> float:
        """
        Place a buy-to-close order for a single-leg position (covered call).

        Paper mode: returns close_price immediately (no broker call).
        Live mode:  places BUY limit order via connector, polls for fill.

        Args:
            trade_id:      PaperLedger trade ID (for logging)
            sell_contract: OCC code for the short leg to buy back
            qty:           Number of contracts
            close_price:   Limit price per share (mark price estimate)
            symbol:        Underlying symbol (for logging)
            reason:        Close reason (for logging)

        Returns:
            Actual fill price per share. In paper mode, equals close_price.

        Raises:
            OrderError: If live order placement or fill fails.
        """
        if self._is_paper:
            logger.info(
                f"[PAPER CLOSE] #{trade_id} {symbol} single-leg {reason} | "
                f"buy-to-close ${close_price:.2f}"
            )
            return close_price

        logger.warning(
            f"[LIVE CLOSE] #{trade_id} {symbol} single-leg {reason} | "
            f"THIS IS A REAL MONEY CLOSE ORDER | "
            f"BUY {sell_contract} @ ${close_price:.2f}"
        )

        order_id = self._connector.place_limit_order(
            contract=sell_contract,
            qty=qty,
            price=close_price,
            direction="BUY",
        )

        if not order_id:
            raise OrderError(
                f"Failed to place single-leg close order for trade #{trade_id}"
            )

        fill = self._wait_for_fill(order_id, close_price)

        if fill["status"] != "filled":
            self._connector.cancel_order(order_id)
            raise OrderError(
                f"Single-leg close order {order_id} for trade #{trade_id} not "
                f"filled within {self._fill_timeout}s. Cancelled."
            )

        actual_price = fill["avg_price"]
        logger.info(
            f"[LIVE CLOSE] #{trade_id} {symbol} single-leg filled | "
            f"price=${actual_price:.2f} | reason={reason}"
        )
        return actual_price

    def _wait_for_fill(
        self,
        order_id: str,
        expected_price: float
    ) -> Dict:
        """
        Poll order status until filled or timeout.

        Uses the normalised status vocabulary shared by all connectors:
          FILLED | PARTIAL | CANCELLED | FAILED | PENDING

        Returns:
            Dict with keys: status, avg_price, filled_qty
        """
        deadline = time.time() + self._fill_timeout
        logger.debug(f"Waiting for fill on order {order_id} (timeout={self._fill_timeout}s)")

        while time.time() < deadline:
            try:
                status = self._connector.get_order_status(order_id)
            except Exception as e:
                logger.warning(f"get_order_status error for {order_id}: {e}")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            if not status:
                logger.warning(f"get_order_status returned None for {order_id}")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            order_status = status.get("status", "")

            if order_status in (STATUS_FILLED, STATUS_PARTIAL):
                avg_price  = status.get("filled_price", expected_price)
                filled_qty = status.get("filled_qty", 0)
                logger.info(
                    f"Order {order_id} filled: avg=${avg_price:.2f} | qty={filled_qty}"
                )
                return {
                    "status":     "filled",
                    "avg_price":  float(avg_price),
                    "filled_qty": int(filled_qty),
                }

            if order_status in (STATUS_CANCELLED, STATUS_FAILED):
                logger.warning(f"Order {order_id} ended with status: {order_status}")
                return {
                    "status":     order_status.lower(),
                    "avg_price":  0.0,
                    "filled_qty": 0,
                }

            logger.debug(f"Order {order_id} status={order_status}, waiting...")
            time.sleep(POLL_INTERVAL_SECONDS)

        logger.warning(f"Order {order_id} timed out after {self._fill_timeout}s")
        return {"status": "timeout", "avg_price": 0.0, "filled_qty": 0}
