"""
Phase 1 — Connector Unit Tests
================================
All tests use mocked responses — zero live API calls.
Fixtures loaded from tests/fixtures/tsla_options.json

Run with:
    cd options_bot
    pytest tests/test_phase1_connectors.py -v
"""

import json
import pytest
import pandas as pd
import numpy as np
from datetime import date, timedelta
from unittest.mock import patch, MagicMock, PropertyMock
from pathlib import Path

# ── Load fixtures ─────────────────────────────────────────────────
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "tsla_options.json"
with open(FIXTURE_PATH) as f:
    FIXTURES = json.load(f)


# ── Helpers ───────────────────────────────────────────────────────

def make_config(mode: str = "paper", trade_env: str = "SIMULATE") -> dict:
    return {
        "mode": mode,
        "moomoo": {
            "host": "127.0.0.1",
            "port": 11111,
            "trade_env": trade_env,
        },
        "logging": {
            "level": "DEBUG",
            "file": "logs/test.log",
            "max_bytes": 1048576,
            "backup_count": 1,
        }
    }


def make_chain_df() -> pd.DataFrame:
    f = FIXTURES["option_chain"]
    return pd.DataFrame(f["data"], columns=f["columns"])


def make_snapshot_df(contract: str) -> pd.DataFrame:
    snap_data = FIXTURES["snapshots"][contract]
    return pd.DataFrame([snap_data])


def make_expiry_df() -> pd.DataFrame:
    return pd.DataFrame({
        "strike_time": FIXTURES["expiries"],
        "option_expiry_date_distance": [1, 8, 22, 50],
        "expiration_cycle": ["WEEK", "WEEK", "MONTH", "MONTH"]
    })


def make_account_df() -> pd.DataFrame:
    return pd.DataFrame([{
        "acc_id": 4310610,
        "trd_env": "SIMULATE",
        "acc_type": "MARGIN",
        "sim_acc_type": "OPTION",
        "acc_status": "ACTIVE",
        "acc_role": "N/A",
        "trdmarket_auth": ["US"]
    }])


def make_funds_df() -> pd.DataFrame:
    return pd.DataFrame([{
        "total_assets": 1_000_000.0,
        "cash":         1_000_000.0,
        "market_val":   0.0
    }])


def make_positions_df(rows: list = None) -> pd.DataFrame:
    if rows is None:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def make_order_df(order_id: str = "TEST001") -> pd.DataFrame:
    return pd.DataFrame([{
        "order_id":     order_id,
        "order_status": "SUBMITTING",
        "code":         "US.TSLA260320C425000",
        "qty":          1,
        "price":        99.0,
        "dealt_qty":    0,
        "dealt_avg_price": 0.0
    }])


# ═══════════════════════════════════════════════════════════════════
# MooMooConnector Tests
# ═══════════════════════════════════════════════════════════════════

class TestMooMooConnectorInit:

    def test_initialises_with_paper_config(self):
        with patch("src.connectors.moomoo_connector.mm"):
            from src.connectors.moomoo_connector import MooMooConnector
            conn = MooMooConnector(make_config("paper", "SIMULATE"))
            assert conn._mode == "paper"
            assert conn._quote_ctx is None
            assert conn._trade_ctx is None

    def test_is_connected_false_before_connect(self):
        with patch("src.connectors.moomoo_connector.mm"):
            from src.connectors.moomoo_connector import MooMooConnector
            conn = MooMooConnector(make_config())
            assert conn.is_connected() is False


class TestMooMooConnectorConnection:

    def test_connect_opens_both_contexts(self):
        with patch("src.connectors.moomoo_connector.mm") as mock_mm:
            mock_mm.OpenQuoteContext.return_value  = MagicMock()
            mock_mm.OpenSecTradeContext.return_value = MagicMock()
            mock_mm.TrdEnv.SIMULATE = "SIMULATE"
            mock_mm.TrdMarket.US    = "US"
            mock_mm.SecurityFirm.FUTUINC = "FUTUINC"

            from importlib import reload
            import src.connectors.moomoo_connector as mod
            reload(mod)
            conn = mod.MooMooConnector(make_config())
            conn._quote_ctx = mock_mm.OpenQuoteContext.return_value
            conn._trade_ctx = mock_mm.OpenSecTradeContext.return_value

            assert conn.is_connected() is True

    def test_disconnect_clears_contexts(self):
        with patch("src.connectors.moomoo_connector.mm"):
            from src.connectors.moomoo_connector import MooMooConnector
            conn = MooMooConnector(make_config())
            conn._quote_ctx = MagicMock()
            conn._trade_ctx = MagicMock()
            conn.disconnect()
            assert conn._quote_ctx is None
            assert conn._trade_ctx is None


class TestMooMooConnectorOptionData:

    @pytest.fixture
    def conn(self):
        with patch("src.connectors.moomoo_connector.mm") as mock_mm:
            mock_mm.TrdEnv.SIMULATE = "SIMULATE"
            mock_mm.TrdEnv.REAL     = "REAL"
            mock_mm.TrdMarket.US    = "US"
            mock_mm.SecurityFirm.FUTUINC = "FUTUINC"
            mock_mm.OptionType.ALL  = "ALL"
            mock_mm.OptionType.CALL = "CALL"
            mock_mm.OptionType.PUT  = "PUT"

            from importlib import reload
            import src.connectors.moomoo_connector as mod
            reload(mod)
            c = mod.MooMooConnector(make_config())
            c._quote_ctx = MagicMock()
            c._trade_ctx = MagicMock()
            yield c

    def test_get_option_expiries_returns_future_only(self, conn):
        conn._quote_ctx.get_option_expiration_date.return_value = (
            0, make_expiry_df()
        )
        result = conn.get_option_expiries("US.TSLA")
        assert isinstance(result, list)
        assert len(result) > 0
        today = date.today().strftime("%Y-%m-%d")
        for expiry in result:
            assert expiry >= today, f"Past expiry returned: {expiry}"

    def test_get_option_expiries_raises_on_api_failure(self, conn):
        from src.exceptions import DataError
        conn._quote_ctx.get_option_expiration_date.return_value = (
            -1, "API error"
        )
        with pytest.raises(DataError):
            conn.get_option_expiries("US.TSLA")

    def test_get_option_chain_returns_dataframe(self, conn):
        conn._quote_ctx.get_option_chain.return_value = (0, make_chain_df())
        result = conn.get_option_chain("US.TSLA", "2026-03-20")
        assert isinstance(result, pd.DataFrame)
        assert len(result) > 0
        assert "option_type" in result.columns
        assert "strike_price" in result.columns
        assert "code" in result.columns

    def test_get_option_chain_separates_calls_and_puts(self, conn):
        conn._quote_ctx.get_option_chain.return_value = (0, make_chain_df())
        result = conn.get_option_chain("US.TSLA", "2026-03-20")
        calls = result[result["option_type"] == "CALL"]
        puts  = result[result["option_type"] == "PUT"]
        assert len(calls) > 0
        assert len(puts) > 0

    def test_get_option_chain_raises_on_empty(self, conn):
        from src.exceptions import DataError
        conn._quote_ctx.get_option_chain.return_value = (0, pd.DataFrame())
        with pytest.raises(DataError, match="Empty option chain"):
            conn.get_option_chain("US.TSLA", "2026-03-20")

    def test_get_option_snapshot_returns_greeks(self, conn):
        snap_df = make_snapshot_df("US.TSLA260320C425000")
        conn._quote_ctx.get_market_snapshot.return_value = (0, snap_df)
        result = conn.get_option_snapshot(["US.TSLA260320C425000"])
        assert isinstance(result, pd.DataFrame)
        assert "option_delta" in result.columns
        assert "option_gamma" in result.columns
        assert "option_theta" in result.columns
        assert "option_vega" in result.columns
        assert "option_iv"   in result.columns
        assert "mid_price"   in result.columns

    def test_get_option_snapshot_computes_mid_price(self, conn):
        snap_df = make_snapshot_df("US.TSLA260320C425000")
        conn._quote_ctx.get_market_snapshot.return_value = (0, snap_df)
        result = conn.get_option_snapshot(["US.TSLA260320C425000"])
        row = result.iloc[0]
        expected_mid = (row["bid_price"] + row["ask_price"]) / 2
        assert abs(row["mid_price"] - expected_mid) < 0.001

    def test_get_option_snapshot_empty_input_returns_empty(self, conn):
        result = conn.get_option_snapshot([])
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0


class TestMooMooConnectorAccount:

    @pytest.fixture
    def conn(self):
        with patch("src.connectors.moomoo_connector.mm") as mock_mm:
            mock_mm.TrdEnv.SIMULATE = "SIMULATE"
            mock_mm.TrdEnv.REAL     = "REAL"
            mock_mm.TrdMarket.US    = "US"
            mock_mm.SecurityFirm.FUTUINC = "FUTUINC"

            from importlib import reload
            import src.connectors.moomoo_connector as mod
            reload(mod)
            c = mod.MooMooConnector(make_config())
            c._quote_ctx = MagicMock()
            c._trade_ctx = MagicMock()
            yield c

    def test_get_account_info_returns_dict(self, conn):
        conn._trade_ctx.accinfo_query.return_value = (0, make_funds_df())
        result = conn.get_account_info()
        assert isinstance(result, dict)
        assert "total_assets" in result
        assert "cash" in result
        assert "market_val" in result
        assert result["total_assets"] == 1_000_000.0

    def test_get_shares_held_returns_zero_when_no_positions(self, conn):
        conn._trade_ctx.position_list_query.return_value = (0, pd.DataFrame())
        result = conn.get_shares_held("US.TSLA")
        assert result == 0

    def test_get_shares_held_returns_correct_qty(self, conn):
        positions = make_positions_df([{
            "code": "US.TSLA",
            "qty":  100,
            "cost_price": 350.0
        }])
        conn._trade_ctx.position_list_query.return_value = (0, positions)
        result = conn.get_shares_held("US.TSLA")
        assert result == 100

    def test_get_shares_held_returns_zero_for_different_symbol(self, conn):
        positions = make_positions_df([{
            "code": "US.AAPL",
            "qty":  100,
            "cost_price": 180.0
        }])
        conn._trade_ctx.position_list_query.return_value = (0, positions)
        result = conn.get_shares_held("US.TSLA")
        assert result == 0

    def test_get_option_positions_returns_empty_when_none(self, conn):
        conn._trade_ctx.position_list_query.return_value = (0, pd.DataFrame())
        result = conn.get_option_positions()
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0


class TestMooMooConnectorOrders:

    @pytest.fixture
    def conn(self):
        with patch("src.connectors.moomoo_connector.mm") as mock_mm:
            mock_mm.TrdEnv.SIMULATE = "SIMULATE"
            mock_mm.TrdEnv.REAL     = "REAL"
            mock_mm.TrdMarket.US    = "US"
            mock_mm.SecurityFirm.FUTUINC = "FUTUINC"
            mock_mm.TrdSide.SELL    = "SELL"
            mock_mm.TrdSide.BUY     = "BUY"
            mock_mm.OrderType.NORMAL = "NORMAL"
            mock_mm.ModifyOrderOp.CANCEL = "CANCEL"

            from importlib import reload
            import src.connectors.moomoo_connector as mod
            reload(mod)
            c = mod.MooMooConnector(make_config())
            c._quote_ctx = MagicMock()
            c._trade_ctx = MagicMock()
            yield c

    def test_place_limit_order_returns_order_id(self, conn):
        conn._trade_ctx.place_order.return_value = (0, make_order_df("ORD001"))
        result = conn.place_limit_order(
            contract="US.TSLA260320C425000",
            qty=1,
            price=4.10,
            direction="SELL"
        )
        assert result == "ORD001"

    def test_place_limit_order_raises_on_failure(self, conn):
        from src.exceptions import OrderError
        conn._trade_ctx.place_order.return_value = (-1, "Insufficient funds")
        with pytest.raises(OrderError):
            conn.place_limit_order(
                contract="US.TSLA260320C425000",
                qty=1,
                price=4.10,
                direction="SELL"
            )

    def test_cancel_order_returns_true_on_success(self, conn):
        conn._trade_ctx.modify_order.return_value = (0, pd.DataFrame())
        result = conn.cancel_order("ORD001")
        assert result is True

    def test_cancel_order_returns_false_on_failure(self, conn):
        conn._trade_ctx.modify_order.return_value = (-1, "Order not found")
        result = conn.cancel_order("ORD001")
        assert result is False

    def test_get_order_status_returns_dict(self, conn):
        orders_df = make_order_df("ORD001")
        orders_df["order_status"] = "SUBMITTED"
        conn._trade_ctx.order_list_query.return_value = (0, orders_df)
        result = conn.get_order_status("ORD001")
        assert result["order_id"] == "ORD001"
        assert result["status"]   == "PENDING"   # "SUBMITTED" normalises to PENDING
        assert "filled_qty"   in result
        assert "filled_price" in result

    def test_get_order_status_raises_when_not_found(self, conn):
        from src.exceptions import DataError
        conn._trade_ctx.order_list_query.return_value = (0, pd.DataFrame(
            columns=["order_id", "order_status", "dealt_qty", "dealt_avg_price"]
        ))
        with pytest.raises(DataError, match="not found"):
            conn.get_order_status("NONEXISTENT")


class TestMooMooConnectorUtility:

    def test_to_yfinance_symbol_strips_us_prefix(self):
        with patch("src.connectors.moomoo_connector.mm"):
            from src.connectors.moomoo_connector import MooMooConnector
            assert MooMooConnector.to_yfinance_symbol("US.TSLA") == "TSLA"
            assert MooMooConnector.to_yfinance_symbol("US.AAPL") == "AAPL"
            assert MooMooConnector.to_yfinance_symbol("HK.0700") == "0700"

    def test_to_moomoo_symbol_adds_us_prefix(self):
        with patch("src.connectors.moomoo_connector.mm"):
            from src.connectors.moomoo_connector import MooMooConnector
            assert MooMooConnector.to_moomoo_symbol("TSLA")    == "US.TSLA"
            assert MooMooConnector.to_moomoo_symbol("US.TSLA") == "US.TSLA"  # idempotent


# ═══════════════════════════════════════════════════════════════════
# YFinanceConnector Tests
# ═══════════════════════════════════════════════════════════════════

def make_ohlcv_df(days: int = 120) -> pd.DataFrame:
    """Generate synthetic OHLCV data."""
    import numpy as np
    dates = pd.date_range(end=pd.Timestamp.today(), periods=days, freq="B")
    close = 400 + np.cumsum(np.random.randn(days) * 5)
    return pd.DataFrame({
        "open":   close * 0.99,
        "high":   close * 1.02,
        "low":    close * 0.98,
        "close":  close,
        "volume": np.random.randint(50_000_000, 100_000_000, days).astype(float)
    }, index=dates)


class TestYFinanceConnector:

    @pytest.fixture
    def yf_conn(self):
        from src.connectors.yfinance_connector import YFinanceConnector
        return YFinanceConnector()

    def test_get_daily_ohlcv_returns_dataframe(self, yf_conn):
        with patch("src.connectors.yfinance_connector.yf.download") as mock_dl:
            mock_dl.return_value = make_ohlcv_df(120)
            result = yf_conn.get_daily_ohlcv("US.TSLA")
            assert isinstance(result, pd.DataFrame)
            assert len(result) > 0
            for col in ["open", "high", "low", "close", "volume"]:
                assert col in result.columns, f"Missing column: {col}"

    def test_get_daily_ohlcv_strips_us_prefix(self, yf_conn):
        with patch("src.connectors.yfinance_connector.yf.download") as mock_dl:
            mock_dl.return_value = make_ohlcv_df()
            yf_conn.get_daily_ohlcv("US.TSLA")
            call_args = mock_dl.call_args
            assert "TSLA" in str(call_args)
            assert "US." not in str(call_args)

    def test_get_daily_ohlcv_uses_cache_on_second_call(self, yf_conn):
        with patch("src.connectors.yfinance_connector.yf.download") as mock_dl:
            mock_dl.return_value = make_ohlcv_df()
            yf_conn.get_daily_ohlcv("US.TSLA")
            yf_conn.get_daily_ohlcv("US.TSLA")
            assert mock_dl.call_count == 1  # second call uses cache

    def test_get_daily_ohlcv_raises_on_empty_data(self, yf_conn):
        from src.exceptions import DataError
        with patch("src.connectors.yfinance_connector.yf.download") as mock_dl:
            mock_dl.return_value = pd.DataFrame()
            with pytest.raises(DataError):
                yf_conn.get_daily_ohlcv("US.TSLA")

    def test_get_current_vix_returns_float(self, yf_conn):
        vix_df = pd.DataFrame(
            {"close": [18.5, 19.2, 17.8]},
            index=pd.date_range("2026-02-20", periods=3)
        )
        with patch("src.connectors.yfinance_connector.yf.download") as mock_dl:
            mock_dl.return_value = vix_df
            result = yf_conn.get_current_vix()
            assert isinstance(result, float)
            assert 5 <= result <= 150

    def test_get_current_vix_returns_latest_close(self, yf_conn):
        vix_df = pd.DataFrame(
            {"close": [18.5, 19.2, 21.3]},
            index=pd.date_range("2026-02-20", periods=3)
        )
        with patch("src.connectors.yfinance_connector.yf.download") as mock_dl:
            mock_dl.return_value = vix_df
            result = yf_conn.get_current_vix()
            assert result == 21.3

    def test_clear_cache_empties_all_entries(self, yf_conn):
        with patch("src.connectors.yfinance_connector.yf.download") as mock_dl:
            mock_dl.return_value = make_ohlcv_df()
            yf_conn.get_daily_ohlcv("US.TSLA")
            assert len(yf_conn._cache) > 0
            yf_conn.clear_cache()
            assert len(yf_conn._cache) == 0

    def test_get_earnings_dates_returns_list(self, yf_conn):
        mock_ticker = MagicMock()
        mock_ticker.calendar = {"Earnings Date": [
            pd.Timestamp("2026-04-15"),
            pd.Timestamp("2026-07-15")
        ]}
        with patch("src.connectors.yfinance_connector.yf.Ticker") as mock_yf:
            mock_yf.return_value = mock_ticker
            result = yf_conn.get_earnings_dates("US.TSLA")
            assert isinstance(result, list)
            assert len(result) >= 1

    def test_get_earnings_dates_returns_empty_on_error(self, yf_conn):
        with patch("src.connectors.yfinance_connector.yf.Ticker") as mock_yf:
            mock_yf.side_effect = Exception("Network error")
            result = yf_conn.get_earnings_dates("US.TSLA")
            assert result == []


# ═══════════════════════════════════════════════════════════════════
# Account ID Routing Tests
# ═══════════════════════════════════════════════════════════════════

class TestAccountRouting:
    """
    Verify that the connector routes to the correct account for each operation.
    Stock account (565755)  → share position queries
    Options account (4310610) → all options orders
    """

    @pytest.fixture
    def conn(self):
        with patch("src.connectors.moomoo_connector.mm") as mock_mm:
            mock_mm.TrdEnv.SIMULATE = "SIMULATE"
            mock_mm.TrdEnv.REAL     = "REAL"
            mock_mm.TrdMarket.US    = "US"
            mock_mm.SecurityFirm.FUTUINC = "FUTUINC"
            mock_mm.TrdSide.SELL    = "SELL"
            mock_mm.TrdSide.BUY     = "BUY"
            mock_mm.OrderType.NORMAL = "NORMAL"

            from importlib import reload
            import src.connectors.moomoo_connector as mod
            reload(mod)
            c = mod.MooMooConnector(make_config())
            c._quote_ctx = MagicMock()
            c._trade_ctx = MagicMock()
            c._trade_ctx.position_list_query.return_value = (0, pd.DataFrame())
            c._trade_ctx.accinfo_query.return_value       = (0, make_funds_df())
            c._trade_ctx.place_order.return_value         = (0, make_order_df("ORD001"))
            yield c

    def test_get_shares_held_uses_stock_account(self, conn):
        conn.get_shares_held("US.TSLA")
        call_kwargs = conn._trade_ctx.position_list_query.call_args.kwargs
        assert call_kwargs.get("acc_id") == 565755

    def test_get_account_info_uses_options_account(self, conn):
        conn.get_account_info()
        call_kwargs = conn._trade_ctx.accinfo_query.call_args.kwargs
        assert call_kwargs.get("acc_id") == 4310610

    def test_place_order_uses_options_account(self, conn):
        conn.place_limit_order("US.TSLA260320C425000", 1, 4.10, "SELL")
        call_kwargs = conn._trade_ctx.place_order.call_args.kwargs
        assert call_kwargs.get("acc_id") == 4310610

    def test_option_positions_uses_options_account(self, conn):
        conn.get_option_positions()
        call_kwargs = conn._trade_ctx.position_list_query.call_args.kwargs
        assert call_kwargs.get("acc_id") == 4310610
