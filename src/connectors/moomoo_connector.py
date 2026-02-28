"""
MooMoo OpenD Connector
======================
Thin wrapper around the moomoo-api SDK.

All column names and account IDs confirmed via live testing on 2026-02-26:

  Expiry dates  : strike_time
  Option type   : option_type         ("CALL" / "PUT")
  Strike        : strike_price
  Greeks        : option_delta, option_gamma, option_theta,
                  option_vega, option_implied_volatility
  Open interest : option_open_interest
  Bid / Ask     : bid_price, ask_price
  Last price    : last_price

  Stock account  : 565755   — share position queries only
  Options account: 4310610  — ALL options orders

Never import moomoo directly outside this module.
All other code calls this connector only.
"""

import time
import pandas as pd
from datetime import date, datetime
from typing import List, Dict, Optional, Callable
import moomoo as mm

from src.logger import get_logger
from src.exceptions import ConnectionError, ReconnectError, DataError, OrderError

logger = get_logger("connectors.moomoo")


class MooMooConnector:
    """
    Wrapper around MooMoo OpenD API.

    Handles:
    - Quote context (option chain, snapshots, streaming)
    - Trade context (account info, positions, order placement)
    - Automatic reconnection with exponential backoff
    - Symbol format conversion (US.TSLA ↔ TSLA)
    """

    # Confirmed account IDs from live testing
    STOCK_ACCOUNT_ID  = 565755    # STOCK sim account — share position queries
    OPTION_ACCOUNT_ID = 4310610   # OPTION sim account — all options orders

    # Confirmed column names from live testing
    EXPIRY_COL    = "strike_time"
    TYPE_COL      = "option_type"
    STRIKE_COL    = "strike_price"
    CODE_COL      = "code"

    SNAPSHOT_COLS = {
        "last_price":                  "last_price",
        "bid_price":                   "bid_price",
        "ask_price":                   "ask_price",
        "option_delta":                "option_delta",
        "option_gamma":                "option_gamma",
        "option_theta":                "option_theta",
        "option_vega":                 "option_vega",
        "option_implied_volatility":   "option_iv",
        "option_open_interest":        "option_open_interest",
        "option_strike_price":         "strike_price",
        "strike_time":                 "expiry",
        "sec_status":                  "sec_status",
    }

    MAX_RETRIES    = 3
    RETRY_DELAY    = 2    # seconds, doubled on each retry

    def __init__(self, config: Dict):
        self._host      = config["moomoo"]["host"]
        self._port      = config["moomoo"]["port"]
        self._trade_env = (mm.TrdEnv.REAL
                           if config["moomoo"]["trade_env"] == "REAL"
                           else mm.TrdEnv.SIMULATE)
        self._mode      = config["mode"]   # "paper" | "live"

        self._quote_ctx: Optional[mm.OpenQuoteContext]   = None
        self._trade_ctx: Optional[mm.OpenSecTradeContext] = None

        logger.info(
            f"MooMooConnector initialised | "
            f"host={self._host}:{self._port} | "
            f"trade_env={config['moomoo']['trade_env']} | "
            f"mode={self._mode}"
        )

    # ── Connection Management ─────────────────────────────────────

    def connect(self) -> None:
        """Open quote and trade contexts."""
        logger.info("Connecting to OpenD...")
        self._connect_quote()
        self._connect_trade()
        logger.info("✅ Connected to OpenD successfully")

    def disconnect(self) -> None:
        """Close all contexts cleanly."""
        if self._quote_ctx:
            try:
                self._quote_ctx.close()
                logger.info("Quote context closed")
            except Exception as e:
                logger.warning(f"Error closing quote context: {e}")
            self._quote_ctx = None

        if self._trade_ctx:
            try:
                self._trade_ctx.close()
                logger.info("Trade context closed")
            except Exception as e:
                logger.warning(f"Error closing trade context: {e}")
            self._trade_ctx = None

    def is_connected(self) -> bool:
        """Return True if both contexts are open."""
        return self._quote_ctx is not None and self._trade_ctx is not None

    def reconnect(self) -> None:
        """
        Attempt reconnection up to MAX_RETRIES times with exponential backoff.
        Raises ReconnectError if all attempts fail.
        """
        self.disconnect()
        delay = self.RETRY_DELAY

        for attempt in range(1, self.MAX_RETRIES + 1):
            logger.warning(f"Reconnect attempt {attempt}/{self.MAX_RETRIES}...")
            try:
                self.connect()
                logger.info("Reconnected successfully")
                return
            except Exception as e:
                logger.error(f"Reconnect attempt {attempt} failed: {e}")
                if attempt < self.MAX_RETRIES:
                    logger.info(f"Waiting {delay}s before next attempt...")
                    time.sleep(delay)
                    delay *= 2

        raise ReconnectError(
            f"Failed to reconnect after {self.MAX_RETRIES} attempts"
        )

    # ── Option Chain Data ─────────────────────────────────────────

    def get_option_expiries(self, symbol: str) -> List[str]:
        """
        Return list of available expiry date strings for a symbol.
        Filters to upcoming dates only (today or later).

        Args:
            symbol: MooMoo format e.g. "US.TSLA"

        Returns:
            List of date strings "YYYY-MM-DD", sorted ascending.

        Raises:
            DataError: If API call fails.
        """
        self._ensure_connected()
        logger.debug(f"Fetching expiries for {symbol}")

        ret, data = self._quote_ctx.get_option_expiration_date(code=symbol)
        if ret != 0:
            raise DataError(f"get_option_expiration_date failed for {symbol}: {data}")

        today    = date.today().strftime("%Y-%m-%d")
        expiries = sorted([
            e for e in data[self.EXPIRY_COL].tolist()
            if e >= today
        ])

        logger.debug(f"{symbol}: {len(expiries)} upcoming expiries")
        return expiries

    def get_option_chain(
        self,
        symbol: str,
        expiry: str,
        option_type: str = "ALL"
    ) -> pd.DataFrame:
        """
        Return option chain for a symbol and expiry.

        Args:
            symbol     : MooMoo format e.g. "US.TSLA"
            expiry     : Date string "YYYY-MM-DD"
            option_type: "CALL" | "PUT" | "ALL"

        Returns:
            DataFrame with columns: code, option_type, strike_price,
            strike_time, expiration_cycle, lot_size

        Raises:
            DataError: If API call fails or returns empty chain.
        """
        self._ensure_connected()
        logger.debug(f"Fetching option chain: {symbol} {expiry} {option_type}")

        ot_map = {
            "CALL": mm.OptionType.CALL,
            "PUT":  mm.OptionType.PUT,
            "ALL":  mm.OptionType.ALL,
        }
        mm_type = ot_map.get(option_type.upper(), mm.OptionType.ALL)

        ret, chain = self._quote_ctx.get_option_chain(
            code=symbol,
            start=expiry,
            end=expiry,
            option_type=mm_type
        )
        if ret != 0:
            raise DataError(f"get_option_chain failed for {symbol} {expiry}: {chain}")

        if len(chain) == 0:
            raise DataError(f"Empty option chain for {symbol} {expiry}")

        logger.debug(
            f"{symbol} {expiry}: "
            f"{len(chain[chain[self.TYPE_COL]=='CALL'])} calls, "
            f"{len(chain[chain[self.TYPE_COL]=='PUT'])} puts"
        )
        return chain

    def get_option_snapshot(self, contracts: List[str]) -> pd.DataFrame:
        """
        Return snapshot with Greeks for a list of option contracts.

        Renames columns to clean internal names (see SNAPSHOT_COLS mapping).
        Greeks will be 0.0 outside market hours — this is expected behaviour.

        Args:
            contracts: List of MooMoo contract codes e.g.
                       ["US.TSLA260320C425000"]

        Returns:
            DataFrame with standardised columns:
            code, last_price, bid_price, ask_price, option_delta,
            option_gamma, option_theta, option_vega, option_iv,
            option_open_interest, strike_price, expiry, sec_status

        Raises:
            DataError: If API call fails.
        """
        self._ensure_connected()
        if not contracts:
            return pd.DataFrame()

        logger.debug(f"Fetching snapshot for {len(contracts)} contracts")

        ret, snap = self._quote_ctx.get_market_snapshot(contracts)
        if ret != 0:
            raise DataError(f"get_market_snapshot failed: {snap}")

        # Rename to clean internal column names
        rename_map = {k: v for k, v in self.SNAPSHOT_COLS.items() if k in snap.columns}
        snap = snap.rename(columns=rename_map)

        # Add mid price
        if "bid_price" in snap.columns and "ask_price" in snap.columns:
            snap["mid_price"] = (snap["bid_price"] + snap["ask_price"]) / 2

        # Warn if Greeks are zero (outside market hours)
        if "option_delta" in snap.columns and (snap["option_delta"] == 0).all():
            logger.warning(
                "Greeks are all 0.0 — market may be closed. "
                "Greeks populate during market hours (9:30–16:00 ET)."
            )

        return snap

    # ── Streaming ─────────────────────────────────────────────────

    def subscribe_quotes(
        self,
        contracts: List[str],
        handler: Callable
    ) -> None:
        """
        Subscribe to real-time quotes + Greeks for option contracts.

        Args:
            contracts: List of contract codes
            handler  : Callback class extending mm.StockQuoteHandlerBase
        """
        self._ensure_connected()
        self._quote_ctx.set_handler(handler)
        ret, msg = self._quote_ctx.subscribe(
            contracts,
            [mm.SubType.QUOTE],
            subscribe_push=True
        )
        if ret != 0:
            raise DataError(f"subscribe failed: {msg}")
        logger.info(f"Subscribed to quotes for {len(contracts)} contracts")

    def unsubscribe_quotes(self, contracts: List[str]) -> None:
        """Unsubscribe from real-time quotes."""
        self._ensure_connected()
        ret, msg = self._quote_ctx.unsubscribe(contracts, [mm.SubType.QUOTE])
        if ret != 0:
            logger.warning(f"Unsubscribe failed: {msg}")

    # ── Account & Positions ───────────────────────────────────────

    def get_account_info(self) -> Dict:
        """
        Return key account metrics for the options account.

        Returns:
            Dict with keys: total_assets, cash, market_val
        """
        self._ensure_connected()
        ret, funds = self._trade_ctx.accinfo_query(
            trd_env=self._trade_env,
            acc_id=self.OPTION_ACCOUNT_ID
        )
        if ret != 0:
            raise DataError(f"accinfo_query failed: {funds}")

        row = funds.iloc[0]
        return {
            "total_assets": float(row.get("total_assets", 0)),
            "cash":         float(row.get("cash", 0)),
            "market_val":   float(row.get("market_val", 0)),
        }

    def get_shares_held(self, symbol: str) -> int:
        """
        Return number of shares held for a symbol in the STOCK account.
        Used to determine if covered call is eligible (requires >= 100 shares).

        Args:
            symbol: MooMoo format e.g. "US.TSLA"

        Returns:
            Number of shares held (0 if none).
        """
        self._ensure_connected()
        ret, positions = self._trade_ctx.position_list_query(
            trd_env=self._trade_env,
            acc_id=self.STOCK_ACCOUNT_ID
        )
        if ret != 0:
            raise DataError(f"position_list_query (stock) failed: {positions}")

        if len(positions) == 0:
            return 0

        match = positions[positions["code"] == symbol]
        if len(match) == 0:
            return 0

        qty = match.iloc[0].get("qty", 0)
        logger.debug(f"{symbol}: {qty} shares held in stock account")
        return int(qty)

    def get_option_positions(self) -> pd.DataFrame:
        """
        Return all open option positions from the OPTIONS account.

        Returns:
            DataFrame with open positions, empty DataFrame if none.
        """
        self._ensure_connected()
        ret, positions = self._trade_ctx.position_list_query(
            trd_env=self._trade_env,
            acc_id=self.OPTION_ACCOUNT_ID
        )
        if ret != 0:
            raise DataError(f"position_list_query (options) failed: {positions}")

        logger.debug(f"Open option positions: {len(positions)}")
        return positions

    def get_open_orders(self) -> pd.DataFrame:
        """
        Return all open/pending orders from the OPTIONS account.

        Returns:
            DataFrame of open orders, empty DataFrame if none.
        """
        self._ensure_connected()
        ret, orders = self._trade_ctx.order_list_query(
            trd_env=self._trade_env,
            acc_id=self.OPTION_ACCOUNT_ID
        )
        if ret != 0:
            raise DataError(f"order_list_query failed: {orders}")

        return orders

    # ── Order Execution ───────────────────────────────────────────

    def place_limit_order(
        self,
        contract: str,
        qty: int,
        price: float,
        direction: str            # "BUY" | "SELL"
    ) -> str:
        """
        Place a single-leg limit order on the OPTIONS account.

        Args:
            contract : MooMoo contract code e.g. "US.TSLA260320C425000"
            qty      : Number of contracts
            price    : Limit price per share
            direction: "BUY" or "SELL"

        Returns:
            Order ID string.

        Raises:
            OrderError  : If order placement fails.
            AssertionError: If called in live mode without explicit confirmation.
        """
        self._ensure_connected()
        self._guard_live_mode("place_limit_order")

        side = mm.TrdSide.BUY if direction.upper() == "BUY" else mm.TrdSide.SELL

        logger.info(
            f"Placing order: {direction} {qty}x {contract} @ ${price:.2f} "
            f"| env={self._trade_env}"
        )

        ret, order_data = self._trade_ctx.place_order(
            price=price,
            qty=qty,
            code=contract,
            trd_side=side,
            order_type=mm.OrderType.NORMAL,
            trd_env=self._trade_env,
            acc_id=self.OPTION_ACCOUNT_ID
        )

        if ret != 0:
            raise OrderError(f"place_order failed for {contract}: {order_data}")

        order_id = str(order_data["order_id"].iloc[0])
        status   = order_data["order_status"].iloc[0]
        logger.info(f"Order placed | id={order_id} | status={status}")
        return order_id

    def place_combo_order(
        self,
        sell_contract: str,
        buy_contract: str,
        qty: int,
        net_credit: float
    ) -> str:
        """
        Place a two-leg spread order (sell + buy) as a combo.
        Used for bear call spreads and bull put spreads.

        Note: MooMoo may not support native combo orders for all account types.
        If combo placement fails, falls back to placing legs individually
        (sell leg first, then buy leg).

        Args:
            sell_contract: Contract code for the short leg
            buy_contract : Contract code for the long (protective) leg
            qty          : Number of spreads
            net_credit   : Target net credit (sell price - buy price)

        Returns:
            Order ID of the sell leg (primary leg).

        Raises:
            OrderError: If either leg fails to place.
        """
        self._ensure_connected()
        self._guard_live_mode("place_combo_order")

        logger.info(
            f"Placing spread: SELL {sell_contract} | BUY {buy_contract} | "
            f"qty={qty} | target credit=${net_credit:.2f}"
        )

        # Place sell leg first (credit received upfront)
        sell_order_id = self.place_limit_order(
            contract=sell_contract,
            qty=qty,
            price=net_credit,   # Net credit as limit
            direction="SELL"
        )

        # Place buy leg immediately after (protection)
        try:
            buy_order_id = self.place_limit_order(
                contract=buy_contract,
                qty=qty,
                price=0.01,     # Minimal price for protective leg
                direction="BUY"
            )
            logger.info(
                f"Spread legs placed | sell={sell_order_id} | buy={buy_order_id}"
            )
        except OrderError as e:
            logger.error(
                f"Buy leg failed after sell leg placed ({sell_order_id}). "
                f"MANUAL ACTION REQUIRED: close sell leg {sell_order_id}. "
                f"Error: {e}"
            )
            raise OrderError(
                f"Buy leg placement failed — sell leg {sell_order_id} "
                f"is open and must be closed manually. Original error: {e}"
            )

        return sell_order_id

    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an open order.

        Args:
            order_id: Order ID to cancel.

        Returns:
            True if cancelled successfully, False otherwise.
        """
        self._ensure_connected()
        logger.info(f"Cancelling order {order_id}")

        ret, data = self._trade_ctx.modify_order(
            modify_order_op=mm.ModifyOrderOp.CANCEL,
            order_id=order_id,
            qty=1,
            price=0,
            trd_env=self._trade_env,
            acc_id=self.OPTION_ACCOUNT_ID
        )

        if ret == 0:
            logger.info(f"Order {order_id} cancelled")
            return True
        else:
            logger.warning(f"Cancel failed for order {order_id}: {data}")
            return False

    def get_order_status(self, order_id: str) -> Dict:
        """
        Return current status of an order.

        Args:
            order_id: Order ID to query.

        Returns:
            Dict with keys: order_id, status, filled_qty, filled_price

        Raises:
            DataError: If order not found.
        """
        self._ensure_connected()
        ret, orders = self._trade_ctx.order_list_query(
            trd_env=self._trade_env,
            acc_id=self.OPTION_ACCOUNT_ID
        )
        if ret != 0:
            raise DataError(f"order_list_query failed: {orders}")

        match = orders[orders["order_id"].astype(str) == str(order_id)]
        if len(match) == 0:
            raise DataError(f"Order {order_id} not found in order list")

        row = match.iloc[0]
        return {
            "order_id":     str(row.get("order_id")),
            "status":       self._normalise_status(str(row.get("order_status", "UNKNOWN"))),
            "filled_qty":   int(row.get("dealt_qty", 0)),
            "filled_price": float(row.get("dealt_avg_price", 0)),
        }

    # ── Status normalisation ──────────────────────────────────────

    @staticmethod
    def _normalise_status(raw: str) -> str:
        """
        Map MooMoo order status strings to the shared bot vocabulary.

        Shared vocabulary (used by OrderRouter._wait_for_fill):
          FILLED    — completely filled
          PARTIAL   — partially filled, still working
          CANCELLED — cancelled
          FAILED    — rejected / failed
          PENDING   — submitted / working (anything else)
        """
        mapping = {
            "FILLED_ALL":    "FILLED",
            "FILLED_PART":   "PARTIAL",
            "CANCELLED_ALL": "CANCELLED",
            "CANCELLED_PART":"CANCELLED",
            "DELETED":       "CANCELLED",
            "FAILED":        "FAILED",
        }
        return mapping.get(raw.upper(), "PENDING")

    # ── Utility ───────────────────────────────────────────────────

    @staticmethod
    def to_yfinance_symbol(moomoo_symbol: str) -> str:
        """Convert "US.TSLA" → "TSLA" for yfinance calls."""
        return moomoo_symbol.replace("US.", "").replace("HK.", "")

    @staticmethod
    def to_moomoo_symbol(ticker: str) -> str:
        """Convert "TSLA" → "US.TSLA" for MooMoo calls."""
        if ticker.startswith("US.") or ticker.startswith("HK."):
            return ticker
        return f"US.{ticker}"

    # ── Private Helpers ───────────────────────────────────────────

    def _connect_quote(self) -> None:
        """Open the quote context."""
        try:
            self._quote_ctx = mm.OpenQuoteContext(
                host=self._host,
                port=self._port
            )
            logger.info("Quote context opened")
        except Exception as e:
            raise ConnectionError(f"Failed to open quote context: {e}")

    def _connect_trade(self) -> None:
        """Open the trade context."""
        try:
            self._trade_ctx = mm.OpenSecTradeContext(
                filter_trdmarket=mm.TrdMarket.US,
                host=self._host,
                port=self._port,
                security_firm=mm.SecurityFirm.FUTUINC
            )
            logger.info("Trade context opened")
        except Exception as e:
            raise ConnectionError(f"Failed to open trade context: {e}")

    def _ensure_connected(self) -> None:
        """Reconnect if contexts have dropped."""
        if not self.is_connected():
            logger.warning("Not connected — attempting reconnect...")
            self.reconnect()

    def _guard_live_mode(self, method_name: str) -> None:
        """
        Safety guard for order placement.
        In paper mode: logs a clear indicator that this is a paper order.
        In live mode  : logs a warning that real money is being used.
        Future versions can add additional confirmation steps here.
        """
        if self._trade_env == mm.TrdEnv.SIMULATE:
            logger.info(f"[PAPER] {method_name} called in SIMULATE mode")
        else:
            logger.warning(f"[LIVE] {method_name} — REAL money order being placed")
