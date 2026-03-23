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
        IB, Stock, Option, LimitOrder, MarketOrder,
        StopOrder, ComboLeg, Contract, util
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
        import yfinance as yf
        ticker   = self.to_ibkr_symbol(symbol)
        today    = date.today().isoformat()
        expiries = sorted([e for e in (yf.Ticker(ticker).options or []) if e >= today])
        if not expiries:
            raise DataError(f"No option expiries found for {ticker}")
        logger.debug(f"{symbol}: {len(expiries)} upcoming expiries via yfinance")
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
        # reqSecDefOptParams returns only 2 boundary strikes on SMART exchange.
        # Use yfinance for the real listed strike ladder.
        import yfinance as yf
        ticker   = self.to_ibkr_symbol(symbol)
        ibkr_exp = self._iso_to_ibkr(expiry)
        logger.debug(f"Fetching chain: {ticker} {expiry} {option_type} via yfinance")

        yf_chain = yf.Ticker(ticker).option_chain(expiry)

        rights = []
        if option_type.upper() in ("CALL", "ALL"):
            rights.append(("C", yf_chain.calls))
        if option_type.upper() in ("PUT", "ALL"):
            rights.append(("P", yf_chain.puts))

        rows = []
        for right, df_leg in rights:
            for _, row in df_leg.iterrows():
                strike = float(row["strike"])
                rows.append({
                    "code":         self._build_code(ticker, ibkr_exp, right, strike),
                    "option_type":  "CALL" if right == "C" else "PUT",
                    "strike_price": strike,
                    "strike_time":  expiry,
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

        # IBKR NP: snapshot=True + genericTickList="106" → Error 321.
        # Use yfinance for Greeks+OI. IBKR for live bid/ask only.
        import yfinance as yf
        import re as _re

        logger.debug(f"Fetching snapshot for {len(contracts)} contracts (yfinance Greeks)")

        yf_cache = {}
        def _yf_chain(tkr, exp_iso):
            key = (tkr, exp_iso)
            if key not in yf_cache:
                try:
                    d = yf.Ticker(tkr).option_chain(exp_iso)
                    yf_cache[key] = {
                        "C": {round(float(r["strike"]),3): r for _,r in d.calls.iterrows()},
                        "P": {round(float(r["strike"]),3): r for _,r in d.puts.iterrows()},
                    }
                except Exception as e:
                    logger.warning(f"yfinance chain {tkr} {exp_iso}: {e}")
                    yf_cache[key] = {"C": {}, "P": {}}
            return yf_cache[key]

        rows = []
        spot_prices = {}   # cache per ticker to avoid repeated calls
        for code in contracts:
            try:
                strike, expiry_iso = self._parse_code(code)
                m = _re.match(r"^([A-Z]+)\d{6}([CP])", code.replace("US.", ""))
                if not m:
                    rows.append(self._empty_snapshot_row(code)); continue
                tkr   = m.group(1)
                right = m.group(2)

                bid, ask, last = 0.0, 0.0, 0.0
                try:
                    ib_c = self._code_to_contract(code)
                    self._ib.qualifyContracts(ib_c)
                    td = self._ib.reqMktData(ib_c, genericTickList="",
                                             snapshot=True, regulatorySnapshot=False)
                    self._ib.sleep(1.5)
                    bid  = float(td.bid)  if (td.bid  and td.bid  > 0) else 0.0
                    ask  = float(td.ask)  if (td.ask  and td.ask  > 0) else 0.0
                    last = float(td.last) if (td.last and td.last > 0) else 0.0
                    self._ib.cancelMktData(ib_c)
                except Exception as ie:
                    logger.debug(f"IBKR bid/ask {code}: {ie}")

                mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else last

                chain  = _yf_chain(tkr, expiry_iso)
                yf_row = chain.get(right, {}).get(round(strike, 3))
                delta = gamma = theta = vega = iv = oi = 0.0
                if yf_row is not None:
                    delta = float(yf_row.get("delta", 0) or 0)
                    gamma = float(yf_row.get("gamma", 0) or 0)
                    theta = float(yf_row.get("theta", 0) or 0)
                    vega  = float(yf_row.get("vega",  0) or 0)
                    iv    = float(yf_row.get("impliedVolatility", 0) or 0)
                    oi    = float(yf_row.get("openInterest", 0) or 0)
                    if bid == 0 and ask == 0:
                        bid  = float(yf_row.get("bid", 0) or 0)
                        ask  = float(yf_row.get("ask", 0) or 0)
                        last = float(yf_row.get("lastPrice", 0) or 0)
                        mid  = (bid + ask) / 2 if (bid > 0 and ask > 0) else last

                # yfinance doesn't return delta/gamma — compute via Black-Scholes
                # when IV is available. Accurate for European-style options.
                if iv > 0 and (delta == 0.0 or gamma == 0.0):
                    try:
                        spot_bs = spot_prices.get(tkr)
                        if spot_bs is None:
                            spot_bs = self.get_spot_price(f"US.{tkr}")
                            spot_prices[tkr] = spot_bs
                        bs = self._bs_greeks(
                            spot=spot_bs, strike=strike,
                            expiry_iso=expiry_iso, iv=iv, right=right,
                        )
                        delta = bs["delta"]
                        gamma = bs["gamma"]
                        if theta == 0.0: theta = bs["theta"]
                        if vega  == 0.0: vega  = bs["vega"]
                    except Exception as bs_e:
                        logger.debug(f"BS Greeks failed for {code}: {bs_e}")

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
                    "option_open_interest": oi,
                    "strike_price":         strike,
                    "expiry":               expiry_iso,
                    "sec_status":           "NORMAL",
                })
            except Exception as e:
                logger.warning(f"Snapshot failed {code}: {e}")
                rows.append(self._empty_snapshot_row(code))

        snap = pd.DataFrame(rows)
        if len(snap) > 0 and (snap["option_delta"] == 0).all():
            logger.warning("Greeks all 0.0 — market closed or yfinance chain unavailable.")
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

        bid   = float(mkt.bid)   if (mkt.bid   and mkt.bid   > 0) else 0.0
        ask   = float(mkt.ask)   if (mkt.ask   and mkt.ask   > 0) else 0.0
        last  = float(mkt.last)  if (mkt.last  and mkt.last  > 0) else 0.0
        close = float(mkt.close) if (mkt.close and mkt.close > 0) else 0.0
        self._ib.cancelMktData(contract)

        if bid > 0 and ask > 0:
            price = (bid + ask) / 2
        elif last > 0:
            price = last
        elif close > 0:
            price = close
        else:
            try:
                import yfinance as yf
                hist  = yf.Ticker(ticker).history(period="1d")
                price = float(hist["Close"].iloc[-1]) if not hist.empty else 0.0
                if price > 0:
                    logger.info(f"{symbol}: IBKR spot zero — yfinance fallback ${price:.2f}")
            except Exception:
                price = 0.0

        if price <= 0:
            raise DataError(f"Could not get spot price for {symbol}")
        logger.debug(f"{symbol}: spot=${price:.2f} (bid={bid:.2f} ask={ask:.2f})")
        return float(price)

    # ── Order Execution ───────────────────────────────────────────

    # ── Stock Order Execution ────────────────────────────────────────────────

    def place_stock_market_order(
        self,
        symbol:    str,
        qty:       int,
        direction: str,   # "BUY" | "SELL"
    ) -> str:
        """
        Place a market order for a stock.

        Fills immediately at the best available price.
        Use with caution on illiquid stocks — slippage risk.

        Args:
            symbol   : Bot format e.g. "US.TSLA"
            qty      : Number of shares
            direction: "BUY" or "SELL"

        Returns:
            Order ID string.
        """
        self._ensure_connected()
        self._guard_live_mode("place_stock_market_order")

        action   = "BUY" if direction.upper() == "BUY" else "SELL"
        ticker   = self.to_ibkr_symbol(symbol)
        contract = Stock(ticker, "SMART", "USD")
        self._ib.qualifyContracts(contract)

        order = MarketOrder(
            action=action,
            totalQuantity=qty,
            account=self._account,
        )

        logger.info(
            f"[STOCK] Market order: {action} {qty}x {ticker} @ MKT "
            f"| account={self._account}"
        )

        trade    = self._ib.placeOrder(contract, order)
        self._ib.sleep(1.0)

        order_id = str(trade.order.orderId)
        status   = trade.orderStatus.status
        logger.info(
            f"[STOCK] Market order placed | id={order_id} | status={status}"
        )
        return order_id

    def place_stock_limit_order(
        self,
        symbol:    str,
        qty:       int,
        price:     float,
        direction: str,   # "BUY" | "SELL"
        tif:       str = "DAY",   # "DAY" | "GTC" | "IOC"
    ) -> str:
        """
        Place a limit order for a stock.

        Only fills at the specified price or better.

        Args:
            symbol   : Bot format e.g. "US.TSLA"
            qty      : Number of shares
            price    : Limit price per share
            direction: "BUY" or "SELL"
            tif      : Time in force — "DAY" (default), "GTC" (good till cancelled),
                       "IOC" (immediate or cancel)

        Returns:
            Order ID string.
        """
        self._ensure_connected()
        self._guard_live_mode("place_stock_limit_order")

        action   = "BUY" if direction.upper() == "BUY" else "SELL"
        ticker   = self.to_ibkr_symbol(symbol)
        contract = Stock(ticker, "SMART", "USD")
        self._ib.qualifyContracts(contract)

        order = LimitOrder(
            action=action,
            totalQuantity=qty,
            lmtPrice=round(price, 2),
            tif=tif.upper(),
            account=self._account,
        )

        logger.info(
            f"[STOCK] Limit order: {action} {qty}x {ticker} @ ${price:.2f} "
            f"tif={tif} | account={self._account}"
        )

        trade    = self._ib.placeOrder(contract, order)
        self._ib.sleep(1.0)

        order_id = str(trade.order.orderId)
        status   = trade.orderStatus.status
        logger.info(
            f"[STOCK] Limit order placed | id={order_id} | status={status}"
        )
        return order_id

    def place_stock_stop_order(
        self,
        symbol:    str,
        qty:       int,
        stop_price: float,
        direction: str,   # "BUY" | "SELL"
        tif:       str = "GTC",
    ) -> str:
        """
        Place a stop order for a stock.

        Triggers a market order when price reaches stop_price.
        Typically used as a stop-loss on an existing position.

        Args:
            symbol    : Bot format e.g. "US.TSLA"
            qty       : Number of shares
            stop_price: Price at which order triggers
            direction : "BUY" (stop above market) or "SELL" (stop-loss below market)
            tif       : "GTC" (default) or "DAY"

        Returns:
            Order ID string.
        """
        self._ensure_connected()
        self._guard_live_mode("place_stock_stop_order")

        action   = "BUY" if direction.upper() == "BUY" else "SELL"
        ticker   = self.to_ibkr_symbol(symbol)
        contract = Stock(ticker, "SMART", "USD")
        self._ib.qualifyContracts(contract)

        order = StopOrder(
            action=action,
            totalQuantity=qty,
            stopPrice=round(stop_price, 2),
            tif=tif.upper(),
            account=self._account,
        )

        logger.info(
            f"[STOCK] Stop order: {action} {qty}x {ticker} @ stop ${stop_price:.2f} "
            f"tif={tif} | account={self._account}"
        )

        trade    = self._ib.placeOrder(contract, order)
        self._ib.sleep(1.0)

        order_id = str(trade.order.orderId)
        status   = trade.orderStatus.status
        logger.info(
            f"[STOCK] Stop order placed | id={order_id} | status={status}"
        )
        return order_id

    def place_stock_stop_limit_order(
        self,
        symbol:     str,
        qty:        int,
        stop_price: float,
        limit_price: float,
        direction:  str,   # "BUY" | "SELL"
        tif:        str = "GTC",
    ) -> str:
        """
        Place a stop-limit order for a stock.

        Triggers a limit order (not market) when stop_price is reached.
        Safer than a plain stop order — avoids bad fills in fast markets.

        Args:
            symbol      : Bot format e.g. "US.TSLA"
            qty         : Number of shares
            stop_price  : Price that triggers the order
            limit_price : Worst acceptable fill price after trigger
            direction   : "BUY" or "SELL"
            tif         : "GTC" (default) or "DAY"

        Returns:
            Order ID string.
        """
        self._ensure_connected()
        self._guard_live_mode("place_stock_stop_limit_order")

        from ib_insync import StopLimitOrder

        action   = "BUY" if direction.upper() == "BUY" else "SELL"
        ticker   = self.to_ibkr_symbol(symbol)
        contract = Stock(ticker, "SMART", "USD")
        self._ib.qualifyContracts(contract)

        order = StopLimitOrder(
            action=action,
            totalQuantity=qty,
            stopPrice=round(stop_price, 2),
            lmtPrice=round(limit_price, 2),
            tif=tif.upper(),
            account=self._account,
        )

        logger.info(
            f"[STOCK] Stop-limit order: {action} {qty}x {ticker} "
            f"stop=${stop_price:.2f} limit=${limit_price:.2f} "
            f"tif={tif} | account={self._account}"
        )

        trade    = self._ib.placeOrder(contract, order)
        self._ib.sleep(1.0)

        order_id = str(trade.order.orderId)
        status   = trade.orderStatus.status
        logger.info(
            f"[STOCK] Stop-limit order placed | id={order_id} | status={status}"
        )
        return order_id

    def get_stock_positions(self) -> pd.DataFrame:
        """
        Return all open stock positions.

        Returns:
            DataFrame with columns:
              symbol, qty, avg_cost, market_price, unrealised_pnl
            Empty DataFrame if no stock positions.
        """
        self._ensure_connected()
        positions = self._ib.positions(self._account)
        rows = []

        for pos in positions:
            if pos.contract.secType == "STK":
                rows.append({
                    "symbol":         self.to_bot_symbol(pos.contract.symbol),
                    "qty":            int(pos.position),
                    "avg_cost":       round(pos.avgCost, 4),
                    "market_price":   0.0,   # populated during market hours
                    "unrealised_pnl": 0.0,
                })

        return pd.DataFrame(rows) if rows else pd.DataFrame(
            columns=["symbol", "qty", "avg_cost", "market_price", "unrealised_pnl"]
        )

    def close_stock_position(
        self,
        symbol:    str,
        qty:       int,
        order_type: str = "MKT",   # "MKT" | "LMT"
        limit_price: float = 0.0,
    ) -> str:
        """
        Close (sell) an existing stock position.

        Args:
            symbol      : Bot format e.g. "US.TSLA"
            qty         : Shares to sell (must be <= position size)
            order_type  : "MKT" (market, immediate) or "LMT" (limit)
            limit_price : Required if order_type="LMT"

        Returns:
            Order ID string.
        """
        self._ensure_connected()
        self._guard_live_mode("close_stock_position")

        if order_type.upper() == "LMT":
            if limit_price <= 0:
                raise ValueError(
                    "limit_price must be > 0 for LMT close_stock_position"
                )
            return self.place_stock_limit_order(
                symbol=symbol, qty=qty, price=limit_price,
                direction="SELL", tif="DAY"
            )
        else:
            return self.place_stock_market_order(
                symbol=symbol, qty=qty, direction="SELL"
            )

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
            f"Placing combo (credit spread): "
            f"SELL {sell_contract} | BUY {buy_contract} | "
            f"qty={qty} | credit=${net_credit:.2f} | "
            f"order=BUY @ $-{net_credit:.2f} (IBKR credit convention)"
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

        leg1              = ComboLeg()
        leg1.conId        = sell_c.conId
        leg1.ratio        = 1
        leg1.action       = "SELL"
        leg1.exchange     = "SMART"
        leg1.openClose    = 1   # 1=OPEN — required by some IBKR accounts

        leg2              = ComboLeg()
        leg2.conId        = buy_c.conId
        leg2.ratio        = 1
        leg2.action       = "BUY"
        leg2.exchange     = "SMART"
        leg2.openClose    = 1   # 1=OPEN — required by some IBKR accounts

        combo.comboLegs = [leg1, leg2]

        # IBKR credit combo convention (confirmed by IBKR Trades desk):
        # For a credit spread, place as BUY with NEGATIVE limit price.
        # BUY at -net_credit means we RECEIVE the credit from the counterparty.
        # SELL at +price would imply paying a debit, which IBKR flags as riskless
        # when the combo bid/ask is negative (credit spread market structure).
        order = LimitOrder(
            action="BUY",
            totalQuantity=qty,
            lmtPrice=round(-abs(net_credit), 2),   # negative = collect credit
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

        # Check open/working orders first
        for trade in self._ib.openTrades():
            if str(trade.order.orderId) == str(order_id):
                return {
                    "order_id":     str(order_id),
                    "status":       self._normalise_status(trade.orderStatus.status),
                    "filled_qty":   int(trade.orderStatus.filled),
                    "filled_price": float(trade.orderStatus.avgFillPrice),
                }

        # Check fills — covers orders that completed and left openTrades()
        for fill in self._ib.fills():
            if str(fill.execution.orderId) == str(order_id):
                return {
                    "order_id":     str(order_id),
                    "status":       "FILLED",
                    "filled_qty":   int(fill.execution.shares),
                    "filled_price": float(fill.execution.price),
                }

        # Also check all trades (includes completed) via trades()
        for trade in self._ib.trades():
            if str(trade.order.orderId) == str(order_id):
                return {
                    "order_id":     str(order_id),
                    "status":       self._normalise_status(trade.orderStatus.status),
                    "filled_qty":   int(trade.orderStatus.filled),
                    "filled_price": float(trade.orderStatus.avgFillPrice),
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
    def _bs_greeks(
        spot: float, strike: float, expiry_iso: str,
        iv: float, right: str, r: float = 0.045,
    ) -> dict:
        """
        Compute Black-Scholes Greeks for a European option.

        Args:
            spot      : Current underlying price
            strike    : Option strike price
            expiry_iso: Expiry date ISO format e.g. '2026-04-17'
            iv        : Implied volatility as decimal (e.g. 0.25 = 25%)
            right     : 'C' for call, 'P' for put
            r         : Risk-free rate (default 4.5%)

        Returns:
            Dict with keys: delta, gamma, theta, vega
        """
        import math
        from datetime import date as _date

        exp_date = _date.fromisoformat(expiry_iso)
        T = (exp_date - _date.today()).days / 365.0
        if T <= 0:
            T = 0.0001   # avoid division by zero on expiry day

        S, K, sigma = float(spot), float(strike), float(iv)
        if S <= 0 or K <= 0 or sigma <= 0:
            return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)

        def _norm_cdf(x: float) -> float:
            return 0.5 * (1 + math.erf(x / math.sqrt(2)))

        def _norm_pdf(x: float) -> float:
            return math.exp(-0.5 * x**2) / math.sqrt(2 * math.pi)

        nd1 = _norm_cdf(d1)
        nd2 = _norm_cdf(d2)
        pdf_d1 = _norm_pdf(d1)

        if right.upper() == "C":
            delta = nd1
            theta = (-(S * pdf_d1 * sigma) / (2 * math.sqrt(T))
                     - r * K * math.exp(-r * T) * nd2) / 365
        else:
            delta = nd1 - 1.0
            theta = (-(S * pdf_d1 * sigma) / (2 * math.sqrt(T))
                     + r * K * math.exp(-r * T) * _norm_cdf(-d2)) / 365

        gamma = pdf_d1 / (S * sigma * math.sqrt(T))
        vega  = S * pdf_d1 * math.sqrt(T) / 100   # per 1% IV move

        return {
            "delta": round(delta, 6),
            "gamma": round(gamma, 6),
            "theta": round(theta, 6),
            "vega":  round(vega,  6),
        }

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
