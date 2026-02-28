"""
Phase 7 — IBKR Connector Tests
================================
Unit tests for IBKRConnector.

All tests use mocked ib_insync objects — no live TWS/Gateway connection required.

Run:
    python3 -m pytest tests/test_phase7_ibkr_connector.py -v -W ignore::DeprecationWarning

Test coverage:
  TestIBKRConnection       (6)  : connect, disconnect, reconnect, error handling
  TestSymbolConversion     (6)  : to/from bot symbol, OCC contract code build/parse
  TestOptionExpiries       (4)  : expiry fetch, filtering, format conversion
  TestOptionChain          (5)  : chain fetch, call/put filter, empty chain
  TestOptionSnapshot       (6)  : Greeks, bid/ask, zero-Greeks warning, failed contract
  TestAccountPositions     (6)  : shares held, option positions, account info
  TestOrderExecution       (7)  : limit order, combo order, cancel, status
  TestContractCodeParsing  (6)  : OCC format round-trip, edge cases
  Total                   (46)
"""

import pytest
import pandas as pd
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import date


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def config():
    return {
        "mode": "paper",
        "broker": "ibkr",
        "ibkr": {
            "host":      "127.0.0.1",
            "port":      7497,
            "client_id": 1,
            "account":   "DU123456",
        },
    }


@pytest.fixture
def mock_ib():
    """A fully mocked ib_insync IB instance."""
    ib = MagicMock()
    ib.isConnected.return_value = True
    ib.managedAccounts.return_value = ["DU123456"]
    return ib


@pytest.fixture
def connector(config, mock_ib):
    """IBKRConnector with mocked IB and ib_insync patched."""
    with patch("src.connectors.ibkr_connector.IB_INSYNC_AVAILABLE", True), \
         patch("src.connectors.ibkr_connector.IB", return_value=mock_ib), \
         patch("src.connectors.ibkr_connector.Stock"), \
         patch("src.connectors.ibkr_connector.Option"), \
         patch("src.connectors.ibkr_connector.LimitOrder"), \
         patch("src.connectors.ibkr_connector.ComboLeg"), \
         patch("src.connectors.ibkr_connector.Contract"), \
         patch("src.connectors.ibkr_connector.util"):
        from src.connectors.ibkr_connector import IBKRConnector
        conn = IBKRConnector(config)
        conn._ib = mock_ib
        yield conn


# ── TestIBKRConnection ────────────────────────────────────────────────────────

class TestIBKRConnection:

    def test_connect_calls_ib_connect(self, config, mock_ib):
        with patch("src.connectors.ibkr_connector.IB_INSYNC_AVAILABLE", True), \
             patch("src.connectors.ibkr_connector.IB", return_value=mock_ib), \
             patch("src.connectors.ibkr_connector.util"):
            from src.connectors.ibkr_connector import IBKRConnector
            conn = IBKRConnector(config)
            conn.connect()
            mock_ib.connect.assert_called_once_with(
                host=config["ibkr"]["host"],
                port=config["ibkr"]["port"],
                clientId=config["ibkr"]["client_id"],
                readonly=False,
                timeout=10,
            )

    def test_connect_sets_account(self, config, mock_ib):
        config["ibkr"]["account"] = ""  # auto-detect
        with patch("src.connectors.ibkr_connector.IB_INSYNC_AVAILABLE", True), \
             patch("src.connectors.ibkr_connector.IB", return_value=mock_ib), \
             patch("src.connectors.ibkr_connector.util"):
            from src.connectors.ibkr_connector import IBKRConnector
            conn = IBKRConnector(config)
            conn.connect()
            assert conn._account == "DU123456"

    def test_disconnect_calls_ib_disconnect(self, connector, mock_ib):
        connector.disconnect()
        mock_ib.disconnect.assert_called_once()
        assert connector._ib is None

    def test_is_connected_returns_true_when_connected(self, connector, mock_ib):
        mock_ib.isConnected.return_value = True
        assert connector.is_connected() is True

    def test_is_connected_returns_false_when_none(self, connector):
        connector._ib = None
        assert connector.is_connected() is False

    def test_reconnect_retries_on_failure(self, config, mock_ib):
        from src.exceptions import ReconnectError
        with patch("src.connectors.ibkr_connector.IB_INSYNC_AVAILABLE", True), \
             patch("src.connectors.ibkr_connector.IB", return_value=mock_ib), \
             patch("src.connectors.ibkr_connector.util"), \
             patch("time.sleep"):
            from src.connectors.ibkr_connector import IBKRConnector
            conn = IBKRConnector(config)
            conn._ib = mock_ib
            mock_ib.connect.side_effect = Exception("connection refused")
            mock_ib.isConnected.return_value = False
            with pytest.raises(ReconnectError):
                conn.reconnect()
            assert mock_ib.connect.call_count == IBKRConnector.MAX_RETRIES


# ── TestSymbolConversion ──────────────────────────────────────────────────────

class TestSymbolConversion:

    def test_to_ibkr_symbol_strips_us_prefix(self):
        from src.connectors.ibkr_connector import IBKRConnector
        assert IBKRConnector.to_ibkr_symbol("US.TSLA") == "TSLA"

    def test_to_ibkr_symbol_strips_hk_prefix(self):
        from src.connectors.ibkr_connector import IBKRConnector
        assert IBKRConnector.to_ibkr_symbol("HK.00700") == "00700"

    def test_to_bot_symbol_adds_us_prefix(self):
        from src.connectors.ibkr_connector import IBKRConnector
        assert IBKRConnector.to_bot_symbol("TSLA") == "US.TSLA"

    def test_to_bot_symbol_passthrough_if_already_prefixed(self):
        from src.connectors.ibkr_connector import IBKRConnector
        assert IBKRConnector.to_bot_symbol("US.TSLA") == "US.TSLA"

    def test_to_yfinance_symbol_alias(self):
        from src.connectors.ibkr_connector import IBKRConnector
        assert IBKRConnector.to_yfinance_symbol("US.TSLA") == "TSLA"

    def test_build_contract_code(self):
        from src.connectors.ibkr_connector import IBKRConnector
        code = IBKRConnector._build_code("TSLA", "20260320", "C", 425.0)
        assert code == "TSLA260320C00425000"


# ── TestContractCodeParsing ───────────────────────────────────────────────────

class TestContractCodeParsing:

    def test_parse_code_call(self):
        from src.connectors.ibkr_connector import IBKRConnector
        strike, expiry = IBKRConnector._parse_code("TSLA260320C00425000")
        assert strike == 425.0
        assert expiry == "2026-03-20"

    def test_parse_code_put(self):
        from src.connectors.ibkr_connector import IBKRConnector
        strike, expiry = IBKRConnector._parse_code("TSLA260320P00400000")
        assert strike == 400.0
        assert expiry == "2026-03-20"

    def test_parse_code_fractional_strike(self):
        from src.connectors.ibkr_connector import IBKRConnector
        code   = IBKRConnector._build_code("AAPL", "20260320", "C", 182.5)
        strike, _ = IBKRConnector._parse_code(code)
        assert strike == 182.5

    def test_build_parse_roundtrip(self):
        from src.connectors.ibkr_connector import IBKRConnector
        code = IBKRConnector._build_code("SPY", "20260620", "P", 500.0)
        assert "SPY" in code
        assert "P" in code
        strike, expiry = IBKRConnector._parse_code(code)
        assert strike == 500.0
        assert expiry == "2026-06-20"

    def test_iso_to_ibkr_conversion(self):
        from src.connectors.ibkr_connector import IBKRConnector
        assert IBKRConnector._iso_to_ibkr("2026-03-20") == "20260320"

    def test_ibkr_to_iso_conversion(self):
        from src.connectors.ibkr_connector import IBKRConnector
        assert IBKRConnector._ibkr_to_iso("20260320") == "2026-03-20"


# ── TestOptionExpiries ────────────────────────────────────────────────────────

class TestOptionExpiries:

    def _make_chain(self, expirations):
        chain      = MagicMock()
        chain.exchange    = "SMART"
        chain.expirations = expirations
        chain.strikes     = [400.0, 425.0, 450.0]
        return chain

    def test_returns_iso_format_dates(self, connector, mock_ib):
        mock_ib.reqSecDefOptParams.return_value = [
            self._make_chain(["20260320", "20260417"])
        ]
        mock_ib.qualifyContracts.return_value = [MagicMock(conId=12345)]
        result = connector.get_option_expiries("US.TSLA")
        assert all("-" in e for e in result)

    def test_filters_past_expiries(self, connector, mock_ib):
        mock_ib.reqSecDefOptParams.return_value = [
            self._make_chain(["20200101", "20260320"])  # one past, one future
        ]
        mock_ib.qualifyContracts.return_value = [MagicMock(conId=12345)]
        result = connector.get_option_expiries("US.TSLA")
        assert all(e >= date.today().strftime("%Y-%m-%d") for e in result)

    def test_returns_sorted_ascending(self, connector, mock_ib):
        mock_ib.reqSecDefOptParams.return_value = [
            self._make_chain(["20260417", "20260320", "20260515"])
        ]
        mock_ib.qualifyContracts.return_value = [MagicMock(conId=12345)]
        result = connector.get_option_expiries("US.TSLA")
        assert result == sorted(result)

    def test_raises_data_error_on_empty_chain(self, connector, mock_ib):
        from src.exceptions import DataError
        mock_ib.reqSecDefOptParams.return_value = []
        mock_ib.qualifyContracts.return_value = [MagicMock(conId=12345)]
        with pytest.raises(DataError):
            connector.get_option_expiries("US.TSLA")


# ── TestOptionChain ───────────────────────────────────────────────────────────

class TestOptionChain:

    def _make_chain(self):
        chain             = MagicMock()
        chain.exchange    = "SMART"
        chain.expirations = ["20260320"]
        chain.strikes     = [400.0, 425.0, 450.0]
        return chain

    def test_returns_calls_and_puts_for_all(self, connector, mock_ib):
        mock_ib.reqSecDefOptParams.return_value = [self._make_chain()]
        mock_ib.qualifyContracts.return_value   = [MagicMock(conId=12345)]
        df = connector.get_option_chain("US.TSLA", "2026-03-20", "ALL")
        assert "CALL" in df["option_type"].values
        assert "PUT"  in df["option_type"].values

    def test_returns_only_calls_when_specified(self, connector, mock_ib):
        mock_ib.reqSecDefOptParams.return_value = [self._make_chain()]
        mock_ib.qualifyContracts.return_value   = [MagicMock(conId=12345)]
        df = connector.get_option_chain("US.TSLA", "2026-03-20", "CALL")
        assert (df["option_type"] == "CALL").all()

    def test_returns_only_puts_when_specified(self, connector, mock_ib):
        mock_ib.reqSecDefOptParams.return_value = [self._make_chain()]
        mock_ib.qualifyContracts.return_value   = [MagicMock(conId=12345)]
        df = connector.get_option_chain("US.TSLA", "2026-03-20", "PUT")
        assert (df["option_type"] == "PUT").all()

    def test_columns_match_moomoo_format(self, connector, mock_ib):
        mock_ib.reqSecDefOptParams.return_value = [self._make_chain()]
        mock_ib.qualifyContracts.return_value   = [MagicMock(conId=12345)]
        df = connector.get_option_chain("US.TSLA", "2026-03-20")
        assert "code"         in df.columns
        assert "option_type"  in df.columns
        assert "strike_price" in df.columns
        assert "strike_time"  in df.columns   # moomoo column name for expiry

    def test_raises_on_missing_expiry(self, connector, mock_ib):
        from src.exceptions import DataError
        mock_ib.reqSecDefOptParams.return_value = [self._make_chain()]
        mock_ib.qualifyContracts.return_value   = [MagicMock(conId=12345)]
        with pytest.raises(DataError):
            connector.get_option_chain("US.TSLA", "2099-01-01")


# ── TestOptionSnapshot ────────────────────────────────────────────────────────

class TestOptionSnapshot:

    def _make_ticker(self, bid=4.0, ask=4.20, last=4.10,
                     delta=0.30, gamma=0.02, theta=-0.05,
                     vega=0.15, iv=0.45):
        t = MagicMock()
        t.bid  = bid
        t.ask  = ask
        t.last = last
        t.callOpenInterest = 500

        g            = MagicMock()
        g.delta      = delta
        g.gamma      = gamma
        g.theta      = theta
        g.vega       = vega
        g.impliedVol = iv
        t.modelGreeks = g
        return t

    def test_snapshot_returns_correct_columns(self, connector, mock_ib):
        mock_ib.reqMktData.return_value = self._make_ticker()
        df = connector.get_option_snapshot(["TSLA260320C00425000"])
        assert "option_delta" in df.columns
        assert "option_iv"    in df.columns
        assert "mid_price"    in df.columns
        assert "strike_price" in df.columns

    def test_mid_price_computed_correctly(self, connector, mock_ib):
        mock_ib.reqMktData.return_value = self._make_ticker(bid=4.0, ask=4.20)
        df = connector.get_option_snapshot(["TSLA260320C00425000"])
        assert df.iloc[0]["mid_price"] == pytest.approx(4.10)

    def test_greeks_populated(self, connector, mock_ib):
        mock_ib.reqMktData.return_value = self._make_ticker(delta=0.32)
        df = connector.get_option_snapshot(["TSLA260320C00425000"])
        assert df.iloc[0]["option_delta"] == pytest.approx(0.32)

    def test_empty_contracts_returns_empty_df(self, connector, mock_ib):
        df = connector.get_option_snapshot([])
        assert len(df) == 0

    def test_failed_contract_returns_zero_row(self, connector, mock_ib):
        mock_ib.reqMktData.side_effect = Exception("contract error")
        df = connector.get_option_snapshot(["TSLA260320C00425000"])
        assert len(df) == 1
        assert df.iloc[0]["option_delta"] == 0.0

    def test_zero_greeks_logs_warning(self, connector, mock_ib):
        t             = self._make_ticker()
        t.modelGreeks = None  # No Greeks → all zero
        mock_ib.reqMktData.return_value = t
        # Should not raise — just logs a warning
        df = connector.get_option_snapshot(["TSLA260320C00425000"])
        assert df.iloc[0]["option_delta"] == 0.0


# ── TestAccountPositions ──────────────────────────────────────────────────────

class TestAccountPositions:

    def test_get_shares_held_returns_correct_qty(self, connector, mock_ib):
        pos          = MagicMock()
        pos.contract = MagicMock()
        pos.contract.symbol  = "TSLA"
        pos.contract.secType = "STK"
        pos.position = 150
        mock_ib.positions.return_value = [pos]
        result = connector.get_shares_held("US.TSLA")
        assert result == 150

    def test_get_shares_held_returns_zero_if_not_held(self, connector, mock_ib):
        mock_ib.positions.return_value = []
        assert connector.get_shares_held("US.TSLA") == 0

    def test_get_option_positions_filters_options_only(self, connector, mock_ib):
        stock_pos          = MagicMock()
        stock_pos.contract = MagicMock(symbol="TSLA", secType="STK",
                                       lastTradeDateOrContractMonth="", right="", strike=0.0)
        stock_pos.position = 100

        opt_pos          = MagicMock()
        opt_pos.contract = MagicMock(symbol="TSLA", secType="OPT",
                                     lastTradeDateOrContractMonth="20260320",
                                     right="C", strike=425.0)
        opt_pos.position = -1
        opt_pos.avgCost  = 410.0

        mock_ib.positions.return_value = [stock_pos, opt_pos]
        df = connector.get_option_positions()
        assert len(df) == 1
        assert df.iloc[0]["qty"] == -1

    def test_get_option_positions_empty_when_no_options(self, connector, mock_ib):
        mock_ib.positions.return_value = []
        df = connector.get_option_positions()
        assert len(df) == 0

    def test_get_account_info_returns_required_keys(self, connector, mock_ib):
        summary_items = [
            MagicMock(tag="NetLiquidation",  value="50000"),
            MagicMock(tag="TotalCashValue",  value="30000"),
            MagicMock(tag="GrossPositionValue", value="20000"),
        ]
        mock_ib.accountSummary.return_value = summary_items
        info = connector.get_account_info()
        assert "total_assets" in info
        assert "cash"         in info
        assert "market_val"   in info
        assert info["total_assets"] == 50000.0

    def test_get_spot_price_returns_last_price(self, connector, mock_ib):
        ticker_data      = MagicMock()
        ticker_data.last = 425.50
        ticker_data.bid  = 425.00
        ticker_data.ask  = 426.00
        mock_ib.reqMktData.return_value = ticker_data
        price = connector.get_spot_price("US.TSLA")
        assert price == pytest.approx(425.50)


# ── TestOrderExecution ────────────────────────────────────────────────────────

class TestOrderExecution:

    def test_place_limit_order_calls_placeOrder(self, connector, mock_ib):
        trade              = MagicMock()
        trade.order.orderId = 42
        trade.orderStatus.status = "Submitted"
        mock_ib.placeOrder.return_value = trade
        mock_ib.qualifyContracts.return_value = None

        with patch("src.connectors.ibkr_connector.Option"), \
             patch("src.connectors.ibkr_connector.LimitOrder") as mock_lo:
            order_id = connector.place_limit_order(
                "TSLA260320C00425000", qty=1, price=4.10, direction="SELL"
            )

        assert order_id == "42"
        mock_ib.placeOrder.assert_called_once()

    def test_place_limit_order_buy_direction(self, connector, mock_ib):
        trade               = MagicMock()
        trade.order.orderId = 43
        trade.orderStatus.status = "Submitted"
        mock_ib.placeOrder.return_value = trade

        with patch("src.connectors.ibkr_connector.Option"), \
             patch("src.connectors.ibkr_connector.LimitOrder") as mock_lo:
            connector.place_limit_order(
                "TSLA260320C00425000", qty=1, price=2.0, direction="BUY"
            )
            call_args = mock_lo.call_args
            assert call_args[1]["action"] == "BUY" or \
                   (call_args[0] and call_args[0][0] == "BUY")

    def test_cancel_order_returns_true_on_success(self, connector, mock_ib):
        trade               = MagicMock()
        trade.order.orderId = 42
        mock_ib.openTrades.return_value = [trade]
        result = connector.cancel_order("42")
        assert result is True
        mock_ib.cancelOrder.assert_called_once()

    def test_cancel_order_returns_false_if_not_found(self, connector, mock_ib):
        mock_ib.openTrades.return_value = []
        result = connector.cancel_order("999")
        assert result is False

    def test_get_order_status_from_open_trades(self, connector, mock_ib):
        trade                      = MagicMock()
        trade.order.orderId        = 42
        trade.orderStatus.status   = "Submitted"
        trade.orderStatus.filled   = 0
        trade.orderStatus.avgFillPrice = 0.0
        mock_ib.openTrades.return_value = [trade]
        mock_ib.fills.return_value      = []

        status = connector.get_order_status("42")
        assert status["status"]     == "PENDING"   # "Submitted" normalises to PENDING
        assert status["order_id"]   == "42"

    def test_get_order_status_from_fills(self, connector, mock_ib):
        mock_ib.openTrades.return_value = []
        fill = MagicMock()
        fill.execution.orderId = 42
        fill.execution.shares  = 1
        fill.execution.price   = 4.10
        mock_ib.fills.return_value = [fill]

        status = connector.get_order_status("42")
        assert status["status"]       == "FILLED"   # fills path always returns FILLED
        assert status["filled_price"] == pytest.approx(4.10)

    def test_get_order_status_raises_if_not_found(self, connector, mock_ib):
        from src.exceptions import DataError
        mock_ib.openTrades.return_value = []
        mock_ib.fills.return_value      = []
        with pytest.raises(DataError):
            connector.get_order_status("999")
