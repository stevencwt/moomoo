"""
Broker Connector Protocol
=========================
Defines the interface that all broker connectors must implement.

Using typing.Protocol (structural subtyping) means MooMooConnector and
IBKRConnector don't need to explicitly inherit from this class — they just
need to implement the same methods with compatible signatures.

Usage in type hints:
    from src.connectors.connector_protocol import BrokerConnector

    class MarketScanner:
        def __init__(self, connector: BrokerConnector, ...):
            ...

This replaces all direct imports of MooMooConnector used only for type
annotations, making every component broker-agnostic.
"""

from __future__ import annotations

from typing import Dict, List, Protocol, runtime_checkable

import pandas as pd


@runtime_checkable
class BrokerConnector(Protocol):
    """
    Structural interface for broker connectors.

    Both MooMooConnector and IBKRConnector implement this interface.
    Any new broker connector must implement all methods below to be
    compatible with the bot's components.
    """

    # ── Connection ────────────────────────────────────────────────

    def connect(self) -> None:
        """Open connection to the broker API."""
        ...

    def disconnect(self) -> None:
        """Close connection and release resources."""
        ...

    def is_connected(self) -> bool:
        """Return True if connection is active."""
        ...

    def reconnect(self) -> None:
        """Disconnect and reconnect, with retry logic."""
        ...

    # ── Market Data ───────────────────────────────────────────────

    def get_option_expiries(self, symbol: str) -> List[str]:
        """
        Return available option expiry dates for symbol.

        Args:
            symbol: Bot format e.g. "US.TSLA"

        Returns:
            List of ISO dates e.g. ["2026-03-21", "2026-04-18"]
            Sorted ascending, future dates only.
        """
        ...

    def get_option_chain(
        self,
        symbol:      str,
        expiry:      str,
        option_type: str = "ALL",
    ) -> pd.DataFrame:
        """
        Return the full option chain for a symbol/expiry.

        Args:
            symbol:      Bot format e.g. "US.TSLA"
            expiry:      ISO date e.g. "2026-03-21"
            option_type: "CALL" | "PUT" | "ALL"

        Returns:
            DataFrame with columns:
              code, strike_price, option_type, expiry,
              bid_price, ask_price, last_price
        """
        ...

    def get_option_snapshot(self, contracts: List[str]) -> pd.DataFrame:
        """
        Return real-time snapshot including Greeks for a list of contracts.

        Args:
            contracts: List of contract codes (bot/OCC format)

        Returns:
            DataFrame with columns:
              code, last_price, bid_price, ask_price, mid_price,
              option_delta, option_gamma, option_theta, option_vega,
              option_iv, option_open_interest, strike_price, expiry
        """
        ...

    def get_spot_price(self, symbol: str) -> float:
        """
        Return current spot price for a symbol.

        Args:
            symbol: Bot format e.g. "US.TSLA"

        Returns:
            Current price as float.
        """
        ...

    # ── Account ───────────────────────────────────────────────────

    def get_account_info(self) -> Dict:
        """
        Return account summary.

        Returns:
            Dict with keys: net_liquidation, cash, currency
        """
        ...

    def get_spot_price(self, symbol: str) -> float:
        """
        Return current spot price for a symbol.

        Args:
            symbol: Bot format e.g. "US.TSLA"

        Returns:
            Current price as float. Raises DataError if unavailable.
        """
        ...

    def get_shares_held(self, symbol: str) -> int:
        """
        Return number of shares held for a symbol.

        Args:
            symbol: Bot format e.g. "US.TSLA"

        Returns:
            Integer share count (0 if not held).
        """
        ...

    def get_option_positions(self) -> pd.DataFrame:
        """
        Return all open option positions.

        Returns:
            DataFrame with columns: code, qty, avg_cost, cur_price, pnl
            Empty DataFrame if no positions.
        """
        ...

    def get_open_orders(self) -> pd.DataFrame:
        """
        Return all open/pending orders.

        Returns:
            DataFrame with columns: order_id, code, qty, price, status
            Empty DataFrame if none.
        """
        ...

    # ── Order Execution ───────────────────────────────────────────

    def place_limit_order(
        self,
        contract:  str,
        qty:       int,
        price:     float,
        direction: str,
    ) -> str:
        """
        Place a single-leg limit order.

        Args:
            contract:  Contract code (bot/OCC format)
            qty:       Number of contracts
            price:     Limit price per contract
            direction: "BUY" or "SELL"

        Returns:
            Order ID string.
        """
        ...

    def place_combo_order(
        self,
        sell_contract: str,
        buy_contract:  str,
        qty:           int,
        net_credit:    float,
    ) -> str:
        """
        Place a two-leg spread order atomically.

        Args:
            sell_contract: Contract code to sell (short leg)
            buy_contract:  Contract code to buy (long leg / hedge)
            qty:           Number of spreads
            net_credit:    Target net credit for the spread

        Returns:
            Order ID string.
        """
        ...

    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an open order.

        Args:
            order_id: Broker order ID from place_*_order

        Returns:
            True if cancelled, False if order not found.
        """
        ...

    def get_order_status(self, order_id: str) -> Dict:
        """
        Return current status of an order.

        Args:
            order_id: Broker order ID

        Returns:
            Dict with keys:
              order_id     : str
              status       : "FILLED" | "PARTIAL" | "CANCELLED" | "FAILED" | "PENDING"
              filled_qty   : int
              filled_price : float

        Raises:
            DataError: If order not found.
        """
        ...

    # ── Symbol Utilities ──────────────────────────────────────────

    @staticmethod
    def to_yfinance_symbol(symbol: str) -> str:
        """
        Convert bot symbol to plain ticker for yfinance.
        "US.TSLA" → "TSLA"
        """
        ...
