"""
IBKR Connector
==============
Thin wrapper around ib_insync that mirrors the MooMooConnector interface
exactly. Every public method has the same signature and return shape so
the rest of the bot (scanner, strategies, order router) works unchanged.

Prerequisites:
  - TWS or IB Gateway running locally
  - Paper trading account enabled in TWS/Gateway
  - API connections enabled in TWS: Edit → Global Config → API → Settings
    ✓ Enable ActiveX and Socket Clients
    ✓ Socket port: 7497 (TWS paper) or 4002 (IB Gateway paper)
    ✓ Trusted IP: 127.0.0.1

Install:
  pip3 install ib_insync

Port reference:
  TWS paper trading    : 7497
  TWS live trading     : 7496
  IB Gateway paper     : 4002
  IB Gateway live      : 4001

Symbol format:
  Internal (bot)  : "US.TSLA"   (same as moomoo format)
  IBKR API        : "TSLA"      (converted internally)
  Contract code   : "TSLA260320C00425000"  (OCC format)

Never import ib_insync directly outside this module.
All other code calls this connector only.
"""

import re
import time
import pandas as pd
from datetime import date
from typing import List, Dict, Optional, Callable

from src.logger import get_logger
from src.exceptions import ConnectionError, ReconnectError, DataError, OrderError

logger = get_logger("connectors.ibkr")

try:
    from ib_insync import (
        IB, Stock, Option, LimitOrder, ComboLeg, Contract, util
    )
    IB_INSYNC_AVAILABLE = True
except ImportError:
    IB_INSYNC_AVAILABLE = False
    logger.warning("ib_insync not installed. Run: pip3 install ib_insync")


class IBKRConnector:
    """
    Wrapper around Interactive Brokers TWS/Gateway API via ib_insync.

    Mirrors MooMooConnector interface exactly — same method names,
    same parameter formats, same return column names.

    Key differences handled internally:
    - Symbol format: "US.TSLA" <-> "TSLA" conversion
    - Contract codes: OCC format "TSLA260320C00425000"
    - Greeks: fetched via reqMktData with modelGreeks tick type
    - Expiries: IBKR uses "20260320", bot uses "2026-03-20"
    """

    MAX_RETRIES   = 3
    RETRY_DELAY   = 2       # seconds, doubled on each retry
    MKT_DATA_WAIT = 3.0     # seconds to wait for market data / Greeks

    def __init__(self, config: Dict):
        if not IB_INSYNC_AVAILABLE:
            raise ImportError("ib_insync not installed. Run: pip3 install ib_insync")

        ibkr_cfg        = config.get("ibkr", {})
        self._host      = ibkr_cfg.get("host", "127.0.0.1")
        self._port      = int(ibkr_cfg.get("port", 7497))   # 7497=TWS paper
        self._client_id = int(ibkr_cfg.get("client_id", 1))
        self._account   = ibkr_cfg.get("account", "")       # e.g. "DU123456" paper
        self._mode      = config.get("mode", "paper")

        self._ib: Optional[IB] = None

        util.logToConsole(level="WARNING")  # suppress ib_insync verbose output

        logger.info(
            f"IBKRConnector initialised | "
            f"host={self._host}:{self._port} | "
            f"client_id={self._client_id} | "
            f"mode={self._mode}"
        )

    # ── Connection Management ─────────────────────────────────────

    def connect(self) -> None:
        """Connect to TWS or IB Gateway."""
        logger.info(f"Connecting to IBKR at {self._host}:{self._port}...")
        try:
            self._ib = IB()
            self._ib.connect(
                host=self._host,
                port=self._port,
                clientId=self._client_id,
                readonly=False,
                timeout=10,
            )
            if not self._account:
                accounts      = self._ib.managedAccounts()
                self._account = accounts[0] if accounts else ""
                logger.info(f"Using account: {self._account}")

            logger.info(f"✅ Connected to IBKR | account={self._account}")
        except Exception as e:
            self._ib = None
            raise ConnectionError(f"Failed to connect to IBKR TWS/Gateway: {e}")

    def disconnect(self) -> None:
        """Disconnect cleanly."""
        if self._ib and self._ib.isConnected():
            try:
                self._ib.disconnect()
                logger.info("Disconnected from IBKR")
            except Exception as e:
                logger.warning(f"Error during disconnect: {e}")
        self._ib = None

    def is_connected(self) -> bool:
        """Return True if connected to TWS/Gateway."""
        return self._ib is not None and self._ib.isConnected()

    def reconnect(self) -> None:
        """
        Attempt reconnection with exponential backoff.
        Raises ReconnectError if all attempts fail.
        """
        self.disconnect()
        delay = self.RETRY_DELAY
        for attempt in range(1, self.MAX_RETRIES + 1):
            logger.warning(f"Reconnect attempt {attempt}/{self.MAX_RETRIES}...")
            try:
                self.connect()
                return
            except Exception as e:
                logger.error(f"Reconnect attempt {attempt} failed: {e}")
                if attempt < self.MAX_RETRIES:
                    time.sleep(delay)
                    delay *= 2
        raise ReconnectError(f"Failed to reconnect after {self.MAX_RETRIES} attempts")

    # ── Option Chain Data ─────────────────────────────────────────

    def get_option_expiries(self, symbol: str) -> List[str]:
        """
        Return upcoming expiry date strings for a symbol.

        Args:
            symbol: Bot format e.g. "US.TSLA"

        Returns:
            List of "YYYY-MM-DD" strings, sorted ascending.
        """
        self._ensure_connected()
        ticker = self.to_ibkr_symbol(symbol)
        logger.debug(f"Fetching expiries for {ticker}")

        stock = Stock(ticker, "SMART", "USD")
        self._ib.qualifyContracts(stock)

        chains = self._ib.reqSecDefOptParams(
            underlyingSymbol=ticker,
            futFopExchange="",
            underlyingSecType="STK",
            underlyingConId=stock.conId,
        )
        if not chains:
            raise DataError(f"No option chain data for {ticker}")

        chain     = next((c for c in chains if c.exchange == "SMART"), chains[0])
        today_str = date.today().strftime("%Y%m%d")
        expiries  = sorted([
            self._ibkr_to_iso(e)
            for e in chain.expirations
            if e >= today_str
        ])
        logger.debug(f"{symbol}: {len(expiries)} upcoming expiries")
        return expiries

    def get_option_chain(
        self,
        symbol:      str,
        expiry:      str,
        option_type: str = "ALL",
    ) -> pd.DataFrame:
        """
        Return option chain for a symbol and expiry.

        Returns:
            DataFrame with columns: code, option_type, strike_price, strike_time
            (column names match MooMoo format for compatibility)
        """
        self._ensure_connected()
        ticker   = self.to_ibkr_symbol(symbol)
        ibkr_exp = self._iso_to_ibkr(expiry)
        logger.debug(f"Fetching chain: {ticker} {expiry} {option_type}")

        stock = Stock(ticker, "SMART", "USD")
        self._ib.qualifyContracts(stock)

        chains = self._ib.reqSecDefOptParams(
            underlyingSymbol=ticker,
            futFopExchange="",
            underlyingSecType="STK",
            underlyingConId=stock.conId,
        )
        if not chains:
            raise DataError(f"No option chain params for {ticker}")

        chain = next((c for c in chains if c.exchange == "SMART"), chains[0])

        if ibkr_exp not in chain.expirations:
            raise DataError(f"Expiry {expiry} not available for {ticker}")

        rights = []
        if option_type.upper() in ("CALL", "ALL"):
            rights.append("C")
        if option_type.upper() in ("PUT", "ALL"):
            rights.append("P")

        rows = []
        for strike in sorted(chain.strikes):
            for right in rights:
                rows.append({
                    "code":         self._build_code(ticker, ibkr_exp, right, strike),
                    "option_type":  "CALL" if right == "C" else "PUT",
                    "strike_price": float(strike),
                    "strike_time":  expiry,     # ISO, matching moomoo column name
                })

        df = pd.DataFrame(rows)
        logger.debug(
            f"{symbol} {expiry}: "
            f"{len(df[df['option_type']=='CALL'])} calls, "
            f"{len(df[df['option_type']=='PUT'])} puts"
        )
        return df

    def get_option_snapshot(self, contracts: List[str]) -> pd.DataFrame:
        """
        Return snapshot with Greeks for a list of option contracts.

        Returns columns matching MooMoo format:
        code, last_price, bid_price, ask_price, mid_price,
        option_delta, option_gamma, option_theta, option_vega,
        option_iv, option_open_interest, strike_price, expiry
        """
        self._ensure_connected()
        if not contracts:
            return pd.DataFrame()

        logger.debug(f"Fetching snapshot for {len(contracts)} contracts")
        rows = []

        for code in contracts:
            try:
                ib_contract = self._code_to_contract(code)
                self._ib.qualifyContracts(ib_contract)

                # genericTickList "106" enables impliedVol + modelGreeks
                ticker_data = self._ib.reqMktData(
                    ib_contract,
                    genericTickList="106",
                    snapshot=True,
                    regulatorySnapshot=False,
                )
                self._ib.sleep(self.MKT_DATA_WAIT)

                bid  = ticker_data.bid  if (ticker_data.bid  and ticker_data.bid  > 0) else 0.0
                ask  = ticker_data.ask  if (ticker_data.ask  and ticker_data.ask  > 0) else 0.0
                last = ticker_data.last if (ticker_data.last and ticker_data.last > 0) else 0.0
                mid  = (bid + ask) / 2  if (bid > 0 and ask > 0) else 0.0

                g     = ticker_data.modelGreeks
                delta = float(g.delta)      if g and g.delta      is not None else 0.0
                gamma = float(g.gamma)      if g and g.gamma      is not None else 0.0
                theta = float(g.theta)      if g and g.theta      is not None else 0.0
                vega  = float(g.vega)       if g and g.vega       is not None else 0.0
                iv    = float(g.impliedVol) if g and g.impliedVol is not None else 0.0

                strike, expiry_iso = self._parse_code(code)

                rows.append({
                    "code":                 code,
                    "last_price":           last,
                    "bid_price":            bid,
                    "ask_price":            ask,
                    "mid_price":            mid,
                    "option_delta":         delta,
                    "option_gamma":         gamma,
                    "option_theta":         theta,
                    "option_vega":          vega,
                    "option_iv":            iv,
                    "option_open_interest": float(ticker_data.callOpenInterest or 0),
                    "strike_price":         strike,
                    "expiry":               expiry_iso,
                    "sec_status":           "NORMAL",
                })
                self._ib.cancelMktData(ib_contract)

            except Exception as e:
                logger.warning(f"Snapshot failed for {code}: {e}")
                rows.append(self._empty_snapshot_row(code))

        snap = pd.DataFrame(rows)

        if len(snap) > 0 and (snap["option_delta"] == 0).all():
            logger.warning(
                "Greeks are all 0.0 — market may be closed. "
                "Greeks populate during market hours (9:30–16:00 ET)."
            )
        return snap

    # ── Streaming ─────────────────────────────────────────────────

    def subscribe_quotes(self, contracts: List[str], handler: Callable) -> None:
        """Subscribe to live quotes + Greeks. Handler receives Ticker objects."""
        self._ensure_connected()
        for code in contracts:
            c = self._code_to_contract(code)
            self._ib.qualifyContracts(c)
            self._ib.reqMktData(c, genericTickList="106", snapshot=False)
        self._ib.pendingTickersEvent += handler
        logger.info(f"Subscribed to live quotes for {len(contracts)} contracts")

    def unsubscribe_quotes(self, contracts: List[str]) -> None:
        """Unsubscribe from live quotes."""
        self._ensure_connected()
        for code in contracts:
            try:
                self._ib.cancelMktData(self._code_to_contract(code))
            except Exception as e:
                logger.warning(f"Unsubscribe failed for {code}: {e}")

    # ── Account & Positions ───────────────────────────────────────

    def get_account_info(self) -> Dict:
        """
        Return key account metrics.

        Returns:
            Dict with keys: total_assets, cash, market_val
        """
        self._ensure_connected()
        summary = self._ib.accountSummary(self._account)

        def _val(tag: str) -> float:
            row = next((s for s in summary if s.tag == tag), None)
            return float(row.value) if row else 0.0

        return {
            "total_assets": _val("NetLiquidation"),
            "cash":         _val("TotalCashValue"),
            "market_val":   _val("GrossPositionValue"),
        }

    def get_shares_held(self, symbol: str) -> int:
        """
        Return shares held for a symbol — reads directly from IBKR account.
        No config override needed (unlike moomoo SG limitation).

        Args:
            symbol: Bot format e.g. "US.TSLA"

        Returns:
            Number of shares held (0 if none).
        """
        self._ensure_connected()
        ticker    = self.to_ibkr_symbol(symbol)
        positions = self._ib.positions(self._account)

        for pos in positions:
            if pos.contract.symbol == ticker and pos.contract.secType == "STK":
                qty = int(pos.position)
                logger.debug(f"{symbol}: {qty} shares held")
                return qty
        return 0

    def get_option_positions(self) -> pd.DataFrame:
        """
        Return all open option positions.

        Returns:
            DataFrame with columns: code, qty, cost_price, market_val
        """
        self._ensure_connected()
        positions = self._ib.positions(self._account)
        rows = []

        for pos in positions:
            if pos.contract.secType == "OPT":
                c    = pos.contract
                code = self._build_code(
                    c.symbol,
                    c.lastTradeDateOrContractMonth,
                    c.right,
                    c.strike,
                )
                rows.append({
                    "code":       code,
                    "qty":        pos.position,
                    "cost_price": pos.avgCost / 100,   # IBKR: per share × 100
                    "market_val": 0.0,
                })

        return pd.DataFrame(rows) if rows else pd.DataFrame(
            columns=["code", "qty", "cost_price", "market_val"]
        )

    def get_open_orders(self) -> pd.DataFrame:
        """Return all open/pending orders."""
        self._ensure_connected()
        trades = self._ib.openTrades()
        rows   = []

        for t in trades:
            rows.append({
                "order_id":        str(t.order.orderId),
                "code":            t.contract.localSymbol or "",
                "order_status":    t.orderStatus.status,
                "qty":             t.order.totalQuantity,
                "price":           t.order.lmtPrice,
                "dealt_qty":       t.orderStatus.filled,
                "dealt_avg_price": t.orderStatus.avgFillPrice,
            })
        return pd.DataFrame(rows) if rows else pd.DataFrame()

    def get_spot_price(self, symbol: str) -> float:
        """
        Return current spot price for a stock.

        Args:
            symbol: Bot format e.g. "US.TSLA"
        """
        self._ensure_connected()
        ticker   = self.to_ibkr_symbol(symbol)
        contract = Stock(ticker, "SMART", "USD")
        self._ib.qualifyContracts(contract)

        mkt = self._ib.reqMktData(contract, snapshot=True)
        self._ib.sleep(2.0)

        price = mkt.last if (mkt.last and mkt.last > 0) else (
            (mkt.bid + mkt.ask) / 2 if (mkt.bid and mkt.ask) else 0.0
        )
        self._ib.cancelMktData(contract)

        if price <= 0:
            raise DataError(f"Could not get spot price for {symbol}")
        return float(price)

    # ── Order Execution ───────────────────────────────────────────

    def place_limit_order(
        self,
        contract:  str,
        qty:       int,
        price:     float,
        direction: str,     # "BUY" | "SELL"
    ) -> str:
        """
        Place a single-leg limit order.

        Args:
            contract : OCC contract code e.g. "TSLA260320C00425000"
            qty      : Number of contracts
            price    : Limit price per share
            direction: "BUY" or "SELL"

        Returns:
            Order ID string.
        """
        self._ensure_connected()
        self._guard_live_mode("place_limit_order")

        action      = "SELL" if direction.upper() == "SELL" else "BUY"
        ib_contract = self._code_to_contract(contract)
        self._ib.qualifyContracts(ib_contract)

        order = LimitOrder(
            action=action,
            totalQuantity=qty,
            lmtPrice=round(price, 2),
            tif="DAY",
            account=self._account,
        )

        logger.info(
            f"Placing order: {action} {qty}x {contract} @ ${price:.2f} "
            f"| account={self._account}"
        )

        trade    = self._ib.placeOrder(ib_contract, order)
        self._ib.sleep(1.0)

        order_id = str(trade.order.orderId)
        status   = trade.orderStatus.status
        logger.info(f"Order placed | id={order_id} | status={status}")
        return order_id

    def place_combo_order(
        self,
        sell_contract: str,
        buy_contract:  str,
        qty:           int,
        net_credit:    float,
    ) -> str:
        """
        Place a two-leg spread as a native IBKR BAG (combo) order.
        Both legs fill atomically — more reliable than separate orders.

        Args:
            sell_contract: OCC code for the short leg
            buy_contract : OCC code for the long (protective) leg
            qty          : Number of spreads
            net_credit   : Target net credit per share

        Returns:
            Order ID string.
        """
        self._ensure_connected()
        self._guard_live_mode("place_combo_order")

        logger.info(
            f"Placing combo: SELL {sell_contract} | BUY {buy_contract} | "
            f"qty={qty} | credit=${net_credit:.2f}"
        )

        sell_c = self._code_to_contract(sell_contract)
        buy_c  = self._code_to_contract(buy_contract)
        self._ib.qualifyContracts(sell_c, buy_c)

        # Build BAG contract
        combo          = Contract()
        combo.symbol   = sell_c.symbol
        combo.secType  = "BAG"
        combo.currency = "USD"
        combo.exchange = "SMART"

        leg1           = ComboLeg()
        leg1.conId     = sell_c.conId
        leg1.ratio     = 1
        leg1.action    = "SELL"
        leg1.exchange  = "SMART"

        leg2           = ComboLeg()
        leg2.conId     = buy_c.conId
        leg2.ratio     = 1
        leg2.action    = "BUY"
        leg2.exchange  = "SMART"

        combo.comboLegs = [leg1, leg2]

        order = LimitOrder(
            action="SELL",
            totalQuantity=qty,
            lmtPrice=round(net_credit, 2),
            tif="DAY",
            account=self._account,
        )

        trade    = self._ib.placeOrder(combo, order)
        self._ib.sleep(1.0)

        order_id = str(trade.order.orderId)
        logger.info(f"Combo order placed | id={order_id} | status={trade.orderStatus.status}")
        return order_id

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True if cancelled."""
        self._ensure_connected()
        target = next(
            (t for t in self._ib.openTrades()
             if str(t.order.orderId) == str(order_id)),
            None
        )
        if not target:
            logger.warning(f"Order {order_id} not found")
            return False

        self._ib.cancelOrder(target.order)
        self._ib.sleep(0.5)
        logger.info(f"Order {order_id} cancelled")
        return True

    def get_order_status(self, order_id: str) -> Dict:
        """
        Return current status of an order.

        Returns:
            Dict with keys: order_id, status, filled_qty, filled_price
        """
        self._ensure_connected()

        for trade in self._ib.openTrades():
            if str(trade.order.orderId) == str(order_id):
                return {
                    "order_id":     str(order_id),
                    "status":       self._normalise_status(trade.orderStatus.status),
                    "filled_qty":   int(trade.orderStatus.filled),
                    "filled_price": float(trade.orderStatus.avgFillPrice),
                }

        for fill in self._ib.fills():
            if str(fill.execution.orderId) == str(order_id):
                return {
                    "order_id":     str(order_id),
                    "status":       "FILLED",
                    "filled_qty":   int(fill.execution.shares),
                    "filled_price": float(fill.execution.price),
                }

        raise DataError(f"Order {order_id} not found")

    # ── Status normalisation ──────────────────────────────────────

    @staticmethod
    def _normalise_status(raw: str) -> str:
        """
        Map IBKR order status strings to the shared bot vocabulary.

        Shared vocabulary (used by OrderRouter._wait_for_fill):
          FILLED    — completely filled
          PARTIAL   — partially filled, still working
          CANCELLED — cancelled
          FAILED    — rejected / inactive
          PENDING   — submitted / working (anything else)
        """
        mapping = {
            "Filled":           "FILLED",
            "PartiallyFilled":  "PARTIAL",
            "Cancelled":        "CANCELLED",
            "ApiCancelled":     "CANCELLED",
            "Inactive":         "FAILED",
        }
        return mapping.get(raw, "PENDING")

    # ── Symbol Utilities ──────────────────────────────────────────

    @staticmethod
    def to_ibkr_symbol(bot_symbol: str) -> str:
        """Convert "US.TSLA" → "TSLA"."""
        return bot_symbol.replace("US.", "").replace("HK.", "")

    @staticmethod
    def to_bot_symbol(ibkr_symbol: str) -> str:
        """Convert "TSLA" → "US.TSLA"."""
        if "." in ibkr_symbol:
            return ibkr_symbol
        return f"US.{ibkr_symbol}"

    @staticmethod
    def to_yfinance_symbol(bot_symbol: str) -> str:
        """Alias — compatible with code that calls to_yfinance_symbol."""
        return IBKRConnector.to_ibkr_symbol(bot_symbol)

    @staticmethod
    def to_moomoo_symbol(ticker: str) -> str:
        """Compatibility alias."""
        return IBKRConnector.to_bot_symbol(ticker)

    # ── Private Helpers ───────────────────────────────────────────

    def _ensure_connected(self) -> None:
        if not self.is_connected():
            logger.warning("Not connected — attempting reconnect...")
            self.reconnect()

    def _guard_live_mode(self, method_name: str) -> None:
        if self._mode == "paper":
            logger.info(f"[PAPER] {method_name} — paper trading mode")
        else:
            logger.warning(f"[LIVE] {method_name} — REAL money order being placed")

    @staticmethod
    def _ibkr_to_iso(d: str) -> str:
        """Convert "20260320" → "2026-03-20"."""
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"

    @staticmethod
    def _iso_to_ibkr(d: str) -> str:
        """Convert "2026-03-20" → "20260320"."""
        return d.replace("-", "")

    @staticmethod
    def _build_code(symbol: str, ibkr_exp: str, right: str, strike: float) -> str:
        """
        Build OCC-style contract code.
        symbol=TSLA, exp=20260320, right=C, strike=425.0 → "TSLA260320C00425000"
        """
        exp_short  = ibkr_exp[2:] if len(ibkr_exp) == 8 else ibkr_exp   # "260320"
        strike_int = int(round(strike * 1000))
        return f"{symbol}{exp_short}{right}{strike_int:08d}"

    @staticmethod
    def _parse_code(code: str):
        """
        Parse OCC contract code → (strike: float, expiry_iso: str).
        "TSLA260320C00425000" → (425.0, "2026-03-20")
        """
        m = re.search(r'(\d{6})([CP])(\d{8})$', code)
        if not m:
            return 0.0, ""
        exp_short  = m.group(1)
        strike_raw = int(m.group(3))
        expiry_iso = f"20{exp_short[:2]}-{exp_short[2:4]}-{exp_short[4:]}"
        return strike_raw / 1000.0, expiry_iso

    @staticmethod
    def _code_to_contract(code: str) -> "Option":
        """
        Parse a contract code into an ib_insync Option contract.

        Accepts both formats:
          MooMoo format : "US.TSLA260320C425000"   (6-digit strike in thousandths)
          OCC format    : "TSLA260320C00425000"     (8-digit strike in thousandths)

        Both represent the same contract — the parser handles either.
        """
        # Strip MooMoo market prefix if present ("US." or "HK.")
        clean = re.sub(r'^(US\.|HK\.)', '', code)

        # Try OCC 8-digit format first: TSLA260320C00425000
        m = re.match(r'^([A-Z]+)(\d{6})([CP])(\d{8})$', clean)
        if m:
            symbol    = m.group(1)
            exp_short = m.group(2)
            right     = m.group(3)
            strike    = int(m.group(4)) / 1000.0
            ibkr_exp  = f"20{exp_short}"
            return Option(
                symbol=symbol,
                lastTradeDateOrContractMonth=ibkr_exp,
                strike=strike,
                right=right,
                exchange="SMART",
                currency="USD",
            )

        # Try MooMoo 6-digit format: TSLA260320C425000
        m = re.match(r'^([A-Z]+)(\d{6})([CP])(\d{4,6})$', clean)
        if m:
            symbol    = m.group(1)
            exp_short = m.group(2)
            right     = m.group(3)
            strike    = int(m.group(4)) / 1000.0
            ibkr_exp  = f"20{exp_short}"
            return Option(
                symbol=symbol,
                lastTradeDateOrContractMonth=ibkr_exp,
                strike=strike,
                right=right,
                exchange="SMART",
                currency="USD",
            )

        raise DataError(
            f"Cannot parse contract code: {code!r} — "
            f"expected MooMoo format 'US.TSLA260320C425000' "
            f"or OCC format 'TSLA260320C00425000'"
        )

    @staticmethod
    def _empty_snapshot_row(code: str) -> Dict:
        strike, expiry = IBKRConnector._parse_code(code)
        return {
            "code":                 code,
            "last_price":           0.0,
            "bid_price":            0.0,
            "ask_price":            0.0,
            "mid_price":            0.0,
            "option_delta":         0.0,
            "option_gamma":         0.0,
            "option_theta":         0.0,
            "option_vega":          0.0,
            "option_iv":            0.0,
            "option_open_interest": 0.0,
            "strike_price":         strike,
            "expiry":               expiry,
            "sec_status":           "UNKNOWN",
        }
