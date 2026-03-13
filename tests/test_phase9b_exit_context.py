"""
Phase 9b — Exit Context Threading Tests
========================================
Tests that exit context (DTE, spot price, VIX) is gathered by PositionMonitor
and threaded through TradeManager → PaperLedger.record_close().

Covers:
  TradeManager.close_trade()   — accepts and forwards exit context kwargs
  PositionMonitor._check_position() — gathers context before calling close_trade
  PositionMonitor._compute_dte()    — DTE computed correctly from expiry date
  PositionMonitor.__init__()        — yfinance optional, backward compatible
  _map_reason()                     — dte_close maps to "dte_close" (not "take_profit")

Run with:
    cd moomoo
    python3 -m pytest tests/test_phase9b_exit_context.py -v

All tests use synthetic data — zero live API calls.
"""

import pytest
import pandas as pd
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch, call

TEST_CONFIG = {
    "mode": "paper",
    "logging": {"level": "DEBUG", "file": "logs/test.log",
                "max_bytes": 1048576, "backup_count": 1},
    "portfolio_guard": {
        "max_open_positions":   6,
        "max_risk_pct":         0.05,
        "max_total_risk_pct":   0.20,
        "max_trades_per_day":   10,
        "portfolio_value":      100_000,
    },
    "position_monitor": {
        "check_interval_minutes": 30,
        "max_price_failures":     3,
        "exit_rules": {
            "stop_loss_multiplier":  3.0,
            "min_days_before_stop":  5,
            "take_profit_pct":       0.50,
            "dte_close_threshold":   21,
            "expired_dte_threshold": 0,
        },
    },
    "execution": {"fill_timeout_seconds": 60},
    "signal_ranker": {"enabled": False},
}


def expiry_in(days: int) -> str:
    return (date.today() + timedelta(days=days)).strftime("%Y-%m-%d")


def expiry_ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")


def make_open_trade(
    trade_id=1,
    symbol="US.SPY",
    strategy_name="bear_call_spread",
    sell_contract="SPY260320C700000",
    buy_contract="SPY260320C710000",
    net_credit=1.35,
    max_loss=865.0,
    expiry_days=30,
    opened_at=None,
):
    """Minimal open trade dict as returned by PaperLedger.get_open_trades()."""
    return {
        "id":             trade_id,
        "symbol":         symbol,
        "strategy_name":  strategy_name,
        "sell_contract":  sell_contract,
        "buy_contract":   buy_contract,
        "net_credit":     net_credit,
        "max_loss":       max_loss,
        "expiry":         expiry_in(expiry_days),
        "opened_at":      (opened_at or datetime.now()).isoformat(),
        "status":         "open",
    }


def make_snapshot_df(sell_contract, sell_mid=0.65,
                     buy_contract=None, buy_mid=0.10):
    """Fake MooMoo option snapshot DataFrame."""
    rows = [{"code": sell_contract, "mid_price": sell_mid}]
    if buy_contract:
        rows.append({"code": buy_contract, "mid_price": buy_mid})
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════
# TradeManager.close_trade() — exit context passthrough
# ═══════════════════════════════════════════════════════════════════

class TestTradeManagerCloseTradeExitContext:

    @pytest.fixture
    def trade_manager(self, tmp_path):
        from src.execution.trade_manager import TradeManager
        from src.execution.paper_ledger import PaperLedger
        from src.execution.portfolio_guard import PortfolioGuard
        from src.execution.order_router import OrderRouter

        ledger = PaperLedger(db_path=str(tmp_path / "test.db"))
        guard  = PortfolioGuard(TEST_CONFIG)
        router = OrderRouter(TEST_CONFIG, MagicMock())
        return TradeManager(TEST_CONFIG, guard, router, ledger), ledger

    def _open_trade(self, tm, ledger):
        """Open a synthetic trade directly in the ledger."""
        from src.strategies.trade_signal import TradeSignal
        sig = TradeSignal(
            strategy_name="bear_call_spread", symbol="US.SPY",
            timestamp=datetime.now(), action="OPEN",
            signal_type="bear_call_spread",
            sell_contract="SPY260320C700000",
            buy_contract="SPY260320C710000",
            quantity=1, sell_price=2.10, buy_price=0.75,
            net_credit=1.35, max_profit=135.0, max_loss=865.0,
            breakeven=701.35, reward_risk=0.156,
            expiry=expiry_in(30), dte=30,
            iv_rank=59.0, delta=0.25,
            reason="test", regime="neutral",
        )
        return ledger.record_open(sig, fill_sell=2.10, fill_buy=0.75)

    def test_close_trade_accepts_exit_context_kwargs(self, trade_manager):
        tm, ledger = trade_manager
        tid = self._open_trade(tm, ledger)
        # Must not raise when exit context is passed
        pnl = tm.close_trade(
            trade_id=            tid,
            close_price=         0.65,
            close_reason=        "take_profit",
            symbol=              "US.SPY",
            strategy_name=       "bear_call_spread",
            spot_price_at_close= 692.10,
            dte_at_close=        18,
            iv_rank_at_close=    41.0,
            vix_at_close=        18.2,
        )
        assert pnl == pytest.approx(70.0)   # (1.35 - 0.65) * 100

    def test_close_trade_stores_exit_context_in_ledger(self, trade_manager):
        tm, ledger = trade_manager
        tid = self._open_trade(tm, ledger)
        tm.close_trade(
            tid, 0.65, "take_profit", "US.SPY", "bear_call_spread",
            spot_price_at_close=692.10,
            dte_at_close=18,
            iv_rank_at_close=41.0,
            vix_at_close=18.2,
        )
        t = ledger.get_trade(tid)
        assert t["spot_price_at_close"] == pytest.approx(692.10)
        assert t["dte_at_close"]        == 18
        assert t["iv_rank_at_close"]    == pytest.approx(41.0)
        assert t["vix_at_close"]        == pytest.approx(18.2)

    def test_close_trade_without_exit_context_still_works(self, trade_manager):
        """Omitting exit context kwargs must not raise — backward compatible."""
        tm, ledger = trade_manager
        tid = self._open_trade(tm, ledger)
        pnl = tm.close_trade(
            tid, 0.0, "expired_worthless", "US.SPY", "bear_call_spread"
        )
        assert pnl == pytest.approx(135.0)
        t = ledger.get_trade(tid)
        assert t["spot_price_at_close"] is None
        assert t["dte_at_close"]        is None

    def test_close_trade_dte_close_reason_accepted(self, trade_manager):
        """'dte_close' is now a valid reason all the way to the ledger."""
        tm, ledger = trade_manager
        tid = self._open_trade(tm, ledger)
        pnl = tm.close_trade(
            tid, 0.50, "dte_close", "US.SPY", "bear_call_spread",
            dte_at_close=21,
        )
        t = ledger.get_trade(tid)
        assert t["close_reason"] == "dte_close"
        assert t["status"]       == "closed"


# ═══════════════════════════════════════════════════════════════════
# PositionMonitor._compute_dte()
# ═══════════════════════════════════════════════════════════════════

class TestComputeDte:

    @pytest.fixture
    def monitor(self):
        from src.monitoring.position_monitor import PositionMonitor
        return PositionMonitor(
            config=        TEST_CONFIG,
            ledger=        MagicMock(),
            trade_manager= MagicMock(),
            moomoo=        MagicMock(),
            evaluator=     MagicMock(),
        )

    def test_dte_for_expiry_in_21_days(self, monitor):
        expiry = expiry_in(21)
        assert monitor._compute_dte(expiry) == 21

    def test_dte_for_expiry_today_is_zero(self, monitor):
        assert monitor._compute_dte(date.today().isoformat()) == 0

    def test_dte_for_already_expired_is_zero(self, monitor):
        past = expiry_ago(5)
        assert monitor._compute_dte(past) == 0   # max(0, negative) = 0

    def test_dte_for_bad_string_returns_none(self, monitor):
        assert monitor._compute_dte("not-a-date") is None


# ═══════════════════════════════════════════════════════════════════
# PositionMonitor — yfinance optional / backward compat
# ═══════════════════════════════════════════════════════════════════

class TestPositionMonitorYfinanceOptional:

    def test_monitor_initialises_without_yfinance(self):
        """PositionMonitor must construct without yfinance — backward compat."""
        from src.monitoring.position_monitor import PositionMonitor
        monitor = PositionMonitor(
            config=        TEST_CONFIG,
            ledger=        MagicMock(),
            trade_manager= MagicMock(),
            moomoo=        MagicMock(),
            evaluator=     MagicMock(),
        )
        assert monitor._yfinance is None

    def test_monitor_initialises_with_yfinance(self):
        from src.monitoring.position_monitor import PositionMonitor
        fake_yfinance = MagicMock()
        monitor = PositionMonitor(
            config=        TEST_CONFIG,
            ledger=        MagicMock(),
            trade_manager= MagicMock(),
            moomoo=        MagicMock(),
            evaluator=     MagicMock(),
            yfinance=      fake_yfinance,
        )
        assert monitor._yfinance is fake_yfinance


# ═══════════════════════════════════════════════════════════════════
# PositionMonitor._map_reason()
# ═══════════════════════════════════════════════════════════════════

class TestMapReason:

    @pytest.fixture
    def monitor(self):
        from src.monitoring.position_monitor import PositionMonitor
        return PositionMonitor(
            config=        TEST_CONFIG,
            ledger=        MagicMock(),
            trade_manager= MagicMock(),
            moomoo=        MagicMock(),
            evaluator=     MagicMock(),
        )

    def test_expired_worthless_maps_to_itself(self, monitor):
        assert monitor._map_reason("expired_worthless") == "expired_worthless"

    def test_stop_loss_maps_to_itself(self, monitor):
        assert monitor._map_reason("stop_loss") == "stop_loss"

    def test_take_profit_maps_to_itself(self, monitor):
        assert monitor._map_reason("take_profit") == "take_profit"

    def test_dte_close_maps_to_dte_close(self, monitor):
        """Phase 1 added 'dte_close' as valid ledger reason — must not map to 'take_profit'."""
        assert monitor._map_reason("dte_close") == "dte_close"

    def test_unknown_reason_maps_to_manual(self, monitor):
        assert monitor._map_reason("some_unknown_reason") == "manual"


# ═══════════════════════════════════════════════════════════════════
# PositionMonitor._check_position() — exit context gathered + passed
# ═══════════════════════════════════════════════════════════════════

class TestCheckPositionExitContext:
    """
    Tests that _check_position gathers exit context correctly and passes
    it to trade_manager.close_trade().
    """

    def _make_monitor(self, yfinance=None, exit_decision=None):
        from src.monitoring.position_monitor import PositionMonitor
        from src.monitoring.exit_evaluator import ExitDecision

        mock_ledger = MagicMock()
        mock_tm     = MagicMock()
        mock_tm.close_trade.return_value = 70.0

        mock_moomoo = MagicMock()
        # Return a price that triggers take_profit
        mock_moomoo.get_option_snapshot.return_value = make_snapshot_df(
            "SPY260320C700000", sell_mid=0.65,
            buy_contract="SPY260320C710000", buy_mid=0.10,
        )

        # Exit decision triggers take_profit
        if exit_decision is None:
            exit_decision = ExitDecision(
                should_exit=   True,
                reason=        "take_profit",
                close_urgency= "normal",
                current_price= 0.55,
                net_credit=    1.35,
                unrealised_pnl= 80.0,
                pnl_pct=       59.3,
            )

        mock_evaluator = MagicMock()
        mock_evaluator.evaluate.return_value = exit_decision

        monitor = PositionMonitor(
            config=        TEST_CONFIG,
            ledger=        mock_ledger,
            trade_manager= mock_tm,
            moomoo=        mock_moomoo,
            evaluator=     mock_evaluator,
            yfinance=      yfinance,
        )
        return monitor, mock_tm

    def test_dte_at_close_always_passed_to_close_trade(self):
        """DTE is computed from expiry — always available without yfinance."""
        monitor, mock_tm = self._make_monitor(yfinance=None)
        trade = make_open_trade(expiry_days=18)
        monitor._check_position(trade)

        call_kwargs = mock_tm.close_trade.call_args.kwargs
        assert call_kwargs["dte_at_close"] == 18

    def test_spot_price_fetched_when_yfinance_available(self):
        fake_yf = MagicMock()
        fake_yf.get_current_price.return_value = 692.10
        fake_yf.get_current_vix.return_value   = 18.2

        monitor, mock_tm = self._make_monitor(yfinance=fake_yf)
        monitor._check_position(make_open_trade())

        call_kwargs = mock_tm.close_trade.call_args.kwargs
        assert call_kwargs["spot_price_at_close"] == pytest.approx(692.10)

    def test_vix_fetched_when_yfinance_available(self):
        fake_yf = MagicMock()
        fake_yf.get_current_price.return_value = 692.10
        fake_yf.get_current_vix.return_value   = 18.2

        monitor, mock_tm = self._make_monitor(yfinance=fake_yf)
        monitor._check_position(make_open_trade())

        call_kwargs = mock_tm.close_trade.call_args.kwargs
        assert call_kwargs["vix_at_close"] == pytest.approx(18.2)

    def test_spot_price_none_without_yfinance(self):
        """When yfinance not provided, spot_price_at_close must be None."""
        monitor, mock_tm = self._make_monitor(yfinance=None)
        monitor._check_position(make_open_trade())

        call_kwargs = mock_tm.close_trade.call_args.kwargs
        assert call_kwargs["spot_price_at_close"] is None

    def test_vix_none_without_yfinance(self):
        monitor, mock_tm = self._make_monitor(yfinance=None)
        monitor._check_position(make_open_trade())

        call_kwargs = mock_tm.close_trade.call_args.kwargs
        assert call_kwargs["vix_at_close"] is None

    def test_spot_price_none_when_yfinance_raises(self):
        """yfinance failure must not abort the exit — spot stays None."""
        fake_yf = MagicMock()
        fake_yf.get_current_price.side_effect = Exception("network error")
        fake_yf.get_current_vix.return_value  = 21.5

        monitor, mock_tm = self._make_monitor(yfinance=fake_yf)
        monitor._check_position(make_open_trade())

        call_kwargs = mock_tm.close_trade.call_args.kwargs
        assert call_kwargs["spot_price_at_close"] is None   # failed gracefully
        assert call_kwargs["vix_at_close"] == pytest.approx(21.5)  # VIX still captured

    def test_vix_none_when_yfinance_vix_raises(self):
        """VIX fetch failure must not abort — VIX stays None."""
        fake_yf = MagicMock()
        fake_yf.get_current_price.return_value = 692.10
        fake_yf.get_current_vix.side_effect    = Exception("vix error")

        monitor, mock_tm = self._make_monitor(yfinance=fake_yf)
        monitor._check_position(make_open_trade())

        call_kwargs = mock_tm.close_trade.call_args.kwargs
        assert call_kwargs["spot_price_at_close"] == pytest.approx(692.10)
        assert call_kwargs["vix_at_close"] is None

    def test_close_reason_passed_correctly_for_take_profit(self):
        monitor, mock_tm = self._make_monitor(yfinance=None)
        monitor._check_position(make_open_trade())

        call_kwargs = mock_tm.close_trade.call_args.kwargs
        assert call_kwargs["close_reason"] == "take_profit"

    def test_close_reason_dte_close_passed_correctly(self):
        from src.monitoring.exit_evaluator import ExitDecision
        dte_decision = ExitDecision(
            should_exit=True, reason="dte_close",
            close_urgency="normal",
            current_price=0.85, net_credit=1.35,
            unrealised_pnl=50.0, pnl_pct=37.0,
        )
        monitor, mock_tm = self._make_monitor(
            yfinance=None, exit_decision=dte_decision
        )
        monitor._check_position(make_open_trade(expiry_days=21))

        call_kwargs = mock_tm.close_trade.call_args.kwargs
        assert call_kwargs["close_reason"] == "dte_close"
        assert call_kwargs["dte_at_close"] == 21

    def test_no_exit_triggered_when_decision_is_hold(self):
        """When evaluator says hold, close_trade must never be called."""
        from src.monitoring.position_monitor import PositionMonitor
        from src.monitoring.exit_evaluator import ExitDecision

        hold_decision = ExitDecision(
            should_exit=False, reason="hold",
            close_urgency=None,
            current_price=1.10, net_credit=1.35,
            unrealised_pnl=25.0, pnl_pct=18.5,
        )
        mock_evaluator = MagicMock()
        mock_evaluator.evaluate.return_value = hold_decision

        mock_moomoo = MagicMock()
        mock_moomoo.get_option_snapshot.return_value = make_snapshot_df(
            "SPY260320C700000", sell_mid=1.10,
            buy_contract="SPY260320C710000", buy_mid=0.20,
        )
        mock_tm = MagicMock()

        monitor = PositionMonitor(
            config=        TEST_CONFIG,
            ledger=        MagicMock(),
            trade_manager= mock_tm,
            moomoo=        mock_moomoo,
            evaluator=     mock_evaluator,
        )
        result = monitor._check_position(make_open_trade())
        assert result is None
        mock_tm.close_trade.assert_not_called()
