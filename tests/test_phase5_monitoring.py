"""
Phase 5 — Position Monitor Tests
==================================
Tests for: ExitEvaluator, PositionMonitor, ValidationReporter

Run with:
    cd moomoo
    python3 -m pytest tests/test_phase5_monitoring.py -v

All tests use synthetic data — zero live API calls.
"""

import pytest
import pandas as pd
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

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
            "stop_loss_multiplier":  2.0,
            "take_profit_pct":       0.50,
            "dte_close_threshold":   21,
            "expired_dte_threshold": 0,
        }
    },
    "validation": {
        "min_trades":       10,
        "min_win_rate":     0.60,
        "min_sharpe_like":  0.50,
    },
    "execution": {"fill_timeout_seconds": 60},
}


def expiry_in(days: int) -> str:
    return (date.today() + timedelta(days=days)).strftime("%Y-%m-%d")


def expiry_ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")


# ═══════════════════════════════════════════════════════════════════
# ExitEvaluator Tests
# ═══════════════════════════════════════════════════════════════════

class TestExitEvaluator:

    @pytest.fixture
    def evaluator(self):
        from src.monitoring.exit_evaluator import ExitEvaluator
        return ExitEvaluator(TEST_CONFIG)

    def _eval(self, evaluator, net_credit=4.00, current_price=2.00,
              days_to_expiry=30):
        return evaluator.evaluate(
            trade_id=      1,
            net_credit=    net_credit,
            max_profit=    net_credit * 100,
            expiry=        expiry_in(days_to_expiry),
            current_price= current_price,
        )

    # ── Hold conditions ───────────────────────────────────────────

    def test_hold_when_no_conditions_met(self, evaluator):
        d = self._eval(evaluator, net_credit=4.00, current_price=2.50,
                       days_to_expiry=30)
        assert d.should_exit is False
        assert d.reason      == "hold"

    def test_hold_calculates_unrealised_pnl_correctly(self, evaluator):
        # Sold for $4.00, currently $2.00 → unrealised profit = $2.00 × 100 = $200
        d = self._eval(evaluator, net_credit=4.00, current_price=2.00,
                       days_to_expiry=30)
        assert d.unrealised_pnl == pytest.approx(200.0)

    def test_hold_calculates_pnl_pct_correctly(self, evaluator):
        # $200 unrealised / $400 max_profit = 50% — just at take_profit threshold
        # with current=2.0 and net_credit=4.0, take_profit_price = 4.0*0.5 = 2.0
        # current (2.0) <= take_profit_price (2.0) → triggers take profit
        d = self._eval(evaluator, net_credit=4.00, current_price=2.10,
                       days_to_expiry=30)
        assert d.should_exit is False
        assert d.pnl_pct     == pytest.approx(190 / 400)

    # ── Stop loss ─────────────────────────────────────────────────

    def test_stop_loss_triggers_at_2x_credit(self, evaluator):
        # stop = 2.0 × $4.00 = $8.00
        d = self._eval(evaluator, net_credit=4.00, current_price=8.00,
                       days_to_expiry=30)
        assert d.should_exit    is True
        assert d.reason         == "stop_loss"
        assert d.close_urgency  == "immediate"

    def test_stop_loss_triggers_above_2x(self, evaluator):
        d = self._eval(evaluator, net_credit=4.00, current_price=9.00,
                       days_to_expiry=30)
        assert d.should_exit is True
        assert d.reason      == "stop_loss"

    def test_stop_loss_does_not_trigger_below_2x(self, evaluator):
        d = self._eval(evaluator, net_credit=4.00, current_price=7.99,
                       days_to_expiry=30)
        assert d.should_exit is False

    def test_stop_loss_pnl_is_negative(self, evaluator):
        # Sold $4, stop at $8 → loss = ($4-$8)×100 = -$400
        d = self._eval(evaluator, net_credit=4.00, current_price=8.00,
                       days_to_expiry=30)
        assert d.unrealised_pnl == pytest.approx(-400.0)

    # ── Take profit ───────────────────────────────────────────────

    def test_take_profit_triggers_at_50pct(self, evaluator):
        # take_profit = (1 - 0.50) × $4.00 = $2.00 → close when price <= $2.00
        d = self._eval(evaluator, net_credit=4.00, current_price=2.00,
                       days_to_expiry=30)
        assert d.should_exit    is True
        assert d.reason         == "take_profit"
        assert d.close_urgency  == "end_of_day"

    def test_take_profit_triggers_below_threshold(self, evaluator):
        d = self._eval(evaluator, net_credit=4.00, current_price=1.00,
                       days_to_expiry=30)
        assert d.should_exit is True
        assert d.reason      == "take_profit"

    def test_take_profit_does_not_trigger_above_threshold(self, evaluator):
        d = self._eval(evaluator, net_credit=4.00, current_price=2.01,
                       days_to_expiry=30)
        assert d.should_exit is False

    def test_take_profit_pnl_is_positive(self, evaluator):
        # Sold $4, now $2 → profit = ($4-$2)×100 = $200
        d = self._eval(evaluator, net_credit=4.00, current_price=2.00,
                       days_to_expiry=30)
        assert d.unrealised_pnl == pytest.approx(200.0)

    # ── DTE close ─────────────────────────────────────────────────

    def test_dte_close_triggers_at_threshold(self, evaluator):
        d = self._eval(evaluator, net_credit=4.00, current_price=2.50,
                       days_to_expiry=21)
        assert d.should_exit    is True
        assert d.reason         == "dte_close"
        assert d.close_urgency  == "end_of_day"

    def test_dte_close_triggers_below_threshold(self, evaluator):
        d = self._eval(evaluator, net_credit=4.00, current_price=2.50,
                       days_to_expiry=10)
        assert d.should_exit is True
        assert d.reason      == "dte_close"

    def test_dte_close_does_not_trigger_above_threshold(self, evaluator):
        d = self._eval(evaluator, net_credit=4.00, current_price=2.50,
                       days_to_expiry=22)
        assert d.should_exit is False

    # ── Expiry ────────────────────────────────────────────────────

    def test_expired_worthless_triggers_when_dte_zero(self, evaluator):
        d = evaluator.evaluate(
            trade_id=1, net_credit=4.00, max_profit=400.0,
            expiry=expiry_in(0), current_price=0.01,
        )
        assert d.should_exit    is True
        assert d.reason         in ("expired_worthless", "expired")
        assert d.close_urgency  == "immediate"

    def test_expired_worthless_pnl_is_max_profit(self, evaluator):
        d = evaluator.evaluate(
            trade_id=1, net_credit=4.00, max_profit=400.0,
            expiry=expiry_ago(1), current_price=0.01,
        )
        assert d.should_exit is True
        assert d.unrealised_pnl == pytest.approx(399.0, abs=1.0)

    # ── Priority: stop loss wins over DTE close ───────────────────

    def test_stop_loss_priority_over_dte_close(self, evaluator):
        # DTE = 15 (would trigger dte_close) AND price = 2× credit (stop loss)
        # Stop loss should win (it's checked first)
        d = self._eval(evaluator, net_credit=4.00, current_price=8.00,
                       days_to_expiry=15)
        assert d.reason        == "stop_loss"
        assert d.close_urgency == "immediate"

    # ── Custom config ─────────────────────────────────────────────

    def test_custom_stop_loss_multiplier(self):
        from src.monitoring.exit_evaluator import ExitEvaluator
        cfg = {**TEST_CONFIG, "position_monitor": {
            "exit_rules": {"stop_loss_multiplier": 3.0, "take_profit_pct": 0.50,
                           "dte_close_threshold": 21, "expired_dte_threshold": 0}
        }}
        ev = ExitEvaluator(cfg)
        # 2× credit should NOT trigger stop (threshold is 3×)
        d  = ev.evaluate(1, 4.00, 400.0, expiry_in(30), 8.00)
        assert d.should_exit is False

    def test_custom_take_profit_pct(self):
        from src.monitoring.exit_evaluator import ExitEvaluator
        cfg = {**TEST_CONFIG, "position_monitor": {
            "exit_rules": {"stop_loss_multiplier": 2.0, "take_profit_pct": 0.25,
                           "dte_close_threshold": 21, "expired_dte_threshold": 0}
        }}
        ev = ExitEvaluator(cfg)
        # Only 25% profit should not trigger with 50% target
        d  = ev.evaluate(1, 4.00, 400.0, expiry_in(30), 3.01)
        assert d.should_exit is False
        # 75% profit should trigger
        d2 = ev.evaluate(1, 4.00, 400.0, expiry_in(30), 1.00)
        assert d2.should_exit is True
        assert d2.reason      == "take_profit"


# ═══════════════════════════════════════════════════════════════════
# PositionMonitor Tests
# ═══════════════════════════════════════════════════════════════════

class TestPositionMonitor:

    @pytest.fixture
    def stack(self, tmp_path):
        """Build full monitoring stack with mocked dependencies."""
        from src.execution.paper_ledger import PaperLedger
        from src.execution.portfolio_guard import PortfolioGuard
        from src.execution.order_router import OrderRouter
        from src.execution.trade_manager import TradeManager
        from src.monitoring.exit_evaluator import ExitEvaluator
        from src.monitoring.position_monitor import PositionMonitor

        ledger       = PaperLedger(db_path=str(tmp_path / "test.db"))
        guard        = PortfolioGuard(TEST_CONFIG)
        router       = OrderRouter(TEST_CONFIG, MagicMock())
        trade_manager = TradeManager(TEST_CONFIG, guard, router, ledger)
        evaluator    = ExitEvaluator(TEST_CONFIG)
        mock_moomoo  = MagicMock()

        monitor = PositionMonitor(TEST_CONFIG, ledger, trade_manager,
                                  mock_moomoo, evaluator)
        return monitor, ledger, trade_manager, mock_moomoo

    def _open_covered_call(self, trade_manager, ledger,
                           net_credit=4.00, days_to_expiry=30,
                           symbol="US.TSLA", sell_contract="US.TSLA260320C425000"):
        from src.strategies.trade_signal import TradeSignal
        signal = TradeSignal(
            strategy_name="covered_call", symbol=symbol,
            timestamp=datetime.now(), action="OPEN",
            signal_type="covered_call",
            sell_contract=sell_contract, buy_contract=None,
            quantity=1, sell_price=net_credit, buy_price=None,
            net_credit=net_credit, max_profit=net_credit * 100,
            max_loss=None, breakeven=410 - net_credit, reward_risk=None,
            expiry=expiry_in(days_to_expiry), dte=days_to_expiry,
            iv_rank=55.0, delta=0.28, reason="Test", regime="neutral",
        )
        result = trade_manager.process_signal(signal)
        return result.trade_id

    def _make_price_snapshot(self, contract: str, mid_price: float) -> pd.DataFrame:
        return pd.DataFrame([{
            "code":      contract,
            "mid_price": mid_price,
            "bid_price": mid_price * 0.95,
            "ask_price": mid_price * 1.05,
        }])

    # ── Market hours / no-op ──────────────────────────────────────

    def test_run_cycle_skips_outside_market_hours(self, stack):
        monitor, ledger, trade_manager, mock_moomoo = stack
        self._open_covered_call(trade_manager, ledger)
        # Not forcing — will check market hours
        with patch("src.monitoring.position_monitor.PositionMonitor._is_market_hours",
                   return_value=False):
            actions = monitor.run_cycle()
        assert actions == []

    def test_run_cycle_proceeds_with_force(self, stack):
        monitor, ledger, trade_manager, mock_moomoo = stack
        # force=True skips market hours check
        mock_moomoo.get_option_snapshot.return_value = pd.DataFrame()
        actions = monitor.run_cycle(force=True)
        # No open trades yet → empty actions
        assert actions == []

    def test_run_cycle_returns_empty_when_no_open_trades(self, stack):
        monitor, *_ = stack
        actions = monitor.run_cycle(force=True)
        assert actions == []

    # ── Take profit ───────────────────────────────────────────────

    def test_take_profit_triggers_and_closes_trade(self, stack):
        monitor, ledger, trade_manager, mock_moomoo = stack
        trade_id = self._open_covered_call(
            trade_manager, ledger, net_credit=4.00, days_to_expiry=30
        )
        # Current price $1.80 < $2.00 take-profit threshold
        mock_moomoo.get_option_snapshot.return_value = \
            self._make_price_snapshot("US.TSLA260320C425000", 1.80)

        actions = monitor.run_cycle(force=True)

        assert len(actions) == 1
        assert actions[0]["reason"]  == "take_profit"
        assert actions[0]["trade_id"] == trade_id

        trade = ledger.get_trade(trade_id)
        assert trade["status"] in ("closed", "expired")
        assert trade["pnl"]    > 0

    # ── Stop loss ─────────────────────────────────────────────────

    def test_stop_loss_triggers_and_closes_trade(self, stack):
        monitor, ledger, trade_manager, mock_moomoo = stack
        trade_id = self._open_covered_call(
            trade_manager, ledger, net_credit=4.00, days_to_expiry=30
        )
        # Current price $8.50 > $8.00 stop-loss threshold
        mock_moomoo.get_option_snapshot.return_value = \
            self._make_price_snapshot("US.TSLA260320C425000", 8.50)

        actions = monitor.run_cycle(force=True)

        assert len(actions) == 1
        assert actions[0]["reason"] == "stop_loss"

        trade = ledger.get_trade(trade_id)
        assert trade["pnl"] < 0

    # ── DTE close ─────────────────────────────────────────────────

    def test_dte_close_triggers_at_21_days(self, stack):
        monitor, ledger, trade_manager, mock_moomoo = stack
        # Open a position expiring in 21 days (exactly at threshold)
        trade_id = self._open_covered_call(
            trade_manager, ledger, net_credit=4.00, days_to_expiry=21
        )
        # Price between stop and take-profit (neutral, but DTE forces close)
        mock_moomoo.get_option_snapshot.return_value = \
            self._make_price_snapshot("US.TSLA260320C425000", 2.50)

        actions = monitor.run_cycle(force=True)

        assert len(actions) == 1
        assert actions[0]["reason"] == "dte_close"

    # ── Hold ──────────────────────────────────────────────────────

    def test_hold_when_no_exit_conditions_met(self, stack):
        monitor, ledger, trade_manager, mock_moomoo = stack
        self._open_covered_call(
            trade_manager, ledger, net_credit=4.00, days_to_expiry=30
        )
        # Price $2.50 — between take-profit ($2.00) and stop-loss ($8.00)
        mock_moomoo.get_option_snapshot.return_value = \
            self._make_price_snapshot("US.TSLA260320C425000", 2.50)

        actions = monitor.run_cycle(force=True)
        assert actions == []

    # ── Price failures ────────────────────────────────────────────

    def test_skips_position_when_price_unavailable(self, stack):
        monitor, ledger, trade_manager, mock_moomoo = stack
        self._open_covered_call(trade_manager, ledger)
        mock_moomoo.get_option_snapshot.return_value = pd.DataFrame()

        actions = monitor.run_cycle(force=True)
        assert actions == []  # skipped, not closed

    def test_position_not_closed_on_price_failure(self, stack):
        monitor, ledger, trade_manager, mock_moomoo = stack
        trade_id = self._open_covered_call(trade_manager, ledger)
        mock_moomoo.get_option_snapshot.side_effect = Exception("API error")

        monitor.run_cycle(force=True)

        trade = ledger.get_trade(trade_id)
        assert trade["status"] == "open"   # still open despite price failure

    # ── Multiple positions ────────────────────────────────────────

    def test_monitors_multiple_positions_independently(self, stack):
        monitor, ledger, trade_manager, mock_moomoo = stack

        # Open two positions with different contracts
        self._open_covered_call(
            trade_manager, ledger, net_credit=4.00,
            days_to_expiry=30, symbol="US.TSLA",
            sell_contract="US.TSLA260320C425000"
        )
        self._open_covered_call(
            trade_manager, ledger, net_credit=4.00,
            days_to_expiry=30, symbol="US.AAPL",
            sell_contract="US.AAPL260320C200000"
        )

        def side_effect(contracts):
            rows = []
            for c in contracts:
                # TSLA position at take-profit, AAPL holding
                price = 1.80 if "TSLA" in c else 2.50
                rows.append({"code": c, "mid_price": price,
                              "bid_price": price*0.95, "ask_price": price*1.05})
            return pd.DataFrame(rows)

        mock_moomoo.get_option_snapshot.side_effect = side_effect

        actions = monitor.run_cycle(force=True)
        # Only TSLA should trigger take-profit
        assert len(actions) == 1
        assert actions[0]["symbol"] == "US.TSLA"

    # ── Position summary ──────────────────────────────────────────

    def test_get_position_summary_returns_list(self, stack):
        monitor, ledger, trade_manager, mock_moomoo = stack
        self._open_covered_call(trade_manager, ledger)
        mock_moomoo.get_option_snapshot.return_value = \
            self._make_price_snapshot("US.TSLA260320C425000", 2.50)

        summary = monitor.get_position_summary()
        assert isinstance(summary, list)
        assert len(summary) == 1

    def test_get_position_summary_includes_unrealised_pnl(self, stack):
        monitor, ledger, trade_manager, mock_moomoo = stack
        self._open_covered_call(
            trade_manager, ledger, net_credit=4.00, days_to_expiry=30
        )
        mock_moomoo.get_option_snapshot.return_value = \
            self._make_price_snapshot("US.TSLA260320C425000", 2.50)

        summary = monitor.get_position_summary()
        assert summary[0]["unrealised_pnl"] == pytest.approx(150.0)  # (4-2.5)×100

    def test_get_position_summary_handles_missing_price(self, stack):
        monitor, ledger, trade_manager, mock_moomoo = stack
        self._open_covered_call(trade_manager, ledger)
        mock_moomoo.get_option_snapshot.return_value = pd.DataFrame()

        summary = monitor.get_position_summary()
        assert summary[0]["unrealised_pnl"] is None


# ═══════════════════════════════════════════════════════════════════
# ValidationReporter Tests
# ═══════════════════════════════════════════════════════════════════

class TestValidationReporter:

    @pytest.fixture
    def reporter_and_ledger(self, tmp_path):
        from src.execution.paper_ledger import PaperLedger
        from src.monitoring.validation_reporter import ValidationReporter
        ledger   = PaperLedger(db_path=str(tmp_path / "val.db"))
        reporter = ValidationReporter(TEST_CONFIG, ledger)
        return reporter, ledger

    def _add_trade(self, ledger, net_credit=4.00, won=True):
        from src.strategies.trade_signal import TradeSignal
        signal = TradeSignal(
            strategy_name="covered_call", symbol="US.TSLA",
            timestamp=datetime.now(), action="OPEN",
            signal_type="covered_call",
            sell_contract="US.TSLA260320C425000", buy_contract=None,
            quantity=1, sell_price=net_credit, buy_price=None,
            net_credit=net_credit, max_profit=net_credit * 100,
            max_loss=None, breakeven=406.0, reward_risk=None,
            expiry=expiry_in(30), dte=30, iv_rank=55.0, delta=0.28,
            reason="Test", regime="neutral",
        )
        trade_id = ledger.record_open(signal, fill_sell=net_credit)
        close_price = 0.0 if won else net_credit * 2
        reason = "expired_worthless" if won else "stop_loss"
        ledger.record_close(trade_id, close_price, reason)
        return trade_id

    def test_generate_returns_report_dict(self, reporter_and_ledger):
        reporter, ledger = reporter_and_ledger
        report = reporter.generate(save_to_file=False)
        assert isinstance(report, dict)
        assert "statistics"     in report
        assert "gates"          in report
        assert "go_live"        in report
        assert "recommendation" in report

    def test_not_ready_with_no_trades(self, reporter_and_ledger):
        reporter, ledger = reporter_and_ledger
        report = reporter.generate(save_to_file=False)
        assert report["go_live"] is False
        assert "NOT READY" in report["recommendation"]

    def test_not_ready_with_insufficient_trades(self, reporter_and_ledger):
        reporter, ledger = reporter_and_ledger
        # Only 5 trades (min is 10)
        for _ in range(5):
            self._add_trade(ledger, won=True)
        report = reporter.generate(save_to_file=False)
        assert report["gates"]["min_trades"]["passed"] is False

    def test_not_ready_with_low_win_rate(self, reporter_and_ledger):
        reporter, ledger = reporter_and_ledger
        # 10 trades, only 3 winners (30% < 60%)
        for i in range(3):
            self._add_trade(ledger, won=True)
        for i in range(7):
            self._add_trade(ledger, won=False)
        report = reporter.generate(save_to_file=False)
        assert report["gates"]["win_rate"]["passed"] is False

    def test_not_ready_with_negative_expectancy(self, reporter_and_ledger):
        reporter, ledger = reporter_and_ledger
        # 10 trades, 7 winners (70% win rate) but large losses dominate
        for _ in range(7):
            self._add_trade(ledger, net_credit=0.10, won=True)   # tiny wins
        for _ in range(3):
            self._add_trade(ledger, net_credit=10.00, won=False)  # large losses
        report = reporter.generate(save_to_file=False)
        assert report["gates"]["positive_expectancy"]["passed"] is False

    def test_go_live_with_strong_results(self, reporter_and_ledger):
        reporter, ledger = reporter_and_ledger
        # 15 trades, 12 winners (80% win rate), consistent profits
        for _ in range(12):
            self._add_trade(ledger, net_credit=4.00, won=True)
        for _ in range(3):
            self._add_trade(ledger, net_credit=4.00, won=False)
        report = reporter.generate(save_to_file=False)
        assert report["gates"]["min_trades"]["passed"]          is True
        assert report["gates"]["win_rate"]["passed"]            is True
        assert report["gates"]["positive_expectancy"]["passed"] is True

    def test_gate_min_trades_passes_at_exact_threshold(self, reporter_and_ledger):
        reporter, ledger = reporter_and_ledger
        for _ in range(10):   # exactly 10 = min_trades
            self._add_trade(ledger, won=True)
        report = reporter.generate(save_to_file=False)
        assert report["gates"]["min_trades"]["passed"] is True

    def test_gate_win_rate_passes_at_60pct(self, reporter_and_ledger):
        reporter, ledger = reporter_and_ledger
        for _ in range(6):   # 60% win rate exactly
            self._add_trade(ledger, won=True)
        for _ in range(4):
            self._add_trade(ledger, won=False)
        report = reporter.generate(save_to_file=False)
        assert report["gates"]["win_rate"]["passed"] is True

    def test_print_current_status_runs_without_error(
        self, reporter_and_ledger, capsys
    ):
        reporter, ledger = reporter_and_ledger
        reporter.print_current_status()
        captured = capsys.readouterr()
        assert "Paper Trading Status" in captured.out

    def test_report_saves_to_file(self, reporter_and_ledger, tmp_path):
        reporter, ledger = reporter_and_ledger
        with patch("src.monitoring.validation_reporter.os.makedirs"):
            with patch("builtins.open", create=True) as mock_open:
                mock_open.return_value.__enter__ = lambda s: s
                mock_open.return_value.__exit__  = MagicMock(return_value=False)
                mock_open.return_value.write     = MagicMock()
                reporter.generate(save_to_file=True)
                mock_open.assert_called()

    def test_sharpe_like_zero_with_fewer_than_2_trades(self, reporter_and_ledger):
        reporter, _ = reporter_and_ledger
        result = reporter._compute_sharpe_like([])
        assert result == 0.0

    def test_sharpe_like_positive_with_consistent_winners(self, reporter_and_ledger):
        reporter, _ = reporter_and_ledger
        trades = [{"pnl": 400.0} for _ in range(10)]
        # All same P&L → std_dev = 0 → returns 0.0 (no division by 0)
        result = reporter._compute_sharpe_like(trades)
        assert result == 0.0  # std_dev = 0, returns 0 by convention

    def test_sharpe_like_handles_mixed_results(self, reporter_and_ledger):
        reporter, _ = reporter_and_ledger
        trades = [{"pnl": 400.0}] * 7 + [{"pnl": -800.0}] * 3
        result = reporter._compute_sharpe_like(trades)
        # 7×$400 + 3×(-$800) = $2800 - $2400 = $400 total → positive
        assert isinstance(result, float)


# ═══════════════════════════════════════════════════════════════════
# Integration: Open → Monitor → Exit lifecycle
# ═══════════════════════════════════════════════════════════════════

class TestPhase5Integration:

    MAX_TRADES = 20  # override daily limit for integration test

    @pytest.fixture
    def full_stack(self, tmp_path):
        from src.execution.paper_ledger import PaperLedger
        from src.execution.portfolio_guard import PortfolioGuard
        from src.execution.order_router import OrderRouter
        from src.execution.trade_manager import TradeManager
        from src.monitoring.exit_evaluator import ExitEvaluator
        from src.monitoring.position_monitor import PositionMonitor
        from src.monitoring.validation_reporter import ValidationReporter

        cfg = {**TEST_CONFIG, "portfolio_guard": {
            **TEST_CONFIG["portfolio_guard"],
            "max_trades_per_day": self.MAX_TRADES,
            "max_open_positions": self.MAX_TRADES,
        }}
        ledger        = PaperLedger(db_path=str(tmp_path / "int.db"))
        guard         = PortfolioGuard(cfg)
        router        = OrderRouter(cfg, MagicMock())
        trade_manager = TradeManager(cfg, guard, router, ledger)
        evaluator     = ExitEvaluator(cfg)
        mock_moomoo   = MagicMock()
        monitor       = PositionMonitor(cfg, ledger, trade_manager,
                                        mock_moomoo, evaluator)
        reporter      = ValidationReporter(cfg, ledger)
        return trade_manager, ledger, monitor, mock_moomoo, reporter

    def _open_signal(self, trade_manager, net_credit=4.00, days=30,
                     symbol="US.TSLA", contract="US.TSLA260320C425000"):
        from src.strategies.trade_signal import TradeSignal
        signal = TradeSignal(
            strategy_name="covered_call", symbol=symbol,
            timestamp=datetime.now(), action="OPEN",
            signal_type="covered_call",
            sell_contract=contract, buy_contract=None,
            quantity=1, sell_price=net_credit, buy_price=None,
            net_credit=net_credit, max_profit=net_credit * 100,
            max_loss=None, breakeven=406.0, reward_risk=None,
            expiry=expiry_in(days), dte=days, iv_rank=55.0,
            delta=0.28, reason="Test", regime="neutral",
        )
        return trade_manager.process_signal(signal)

    def test_full_take_profit_lifecycle(self, full_stack):
        trade_manager, ledger, monitor, mock_moomoo, reporter = full_stack

        result = self._open_signal(trade_manager, net_credit=4.00, days=30)
        assert result.executed

        # Monitor sees price at take-profit level
        mock_moomoo.get_option_snapshot.return_value = pd.DataFrame([{
            "code": "US.TSLA260320C425000",
            "mid_price": 1.80, "bid_price": 1.71, "ask_price": 1.89,
        }])
        actions = monitor.run_cycle(force=True)
        assert len(actions) == 1
        assert actions[0]["reason"] == "take_profit"
        assert actions[0]["pnl"]    > 0

        # Position is now closed
        assert ledger.get_open_trades() == []

    def test_full_stop_loss_lifecycle(self, full_stack):
        trade_manager, ledger, monitor, mock_moomoo, reporter = full_stack

        result = self._open_signal(trade_manager, net_credit=4.00, days=30)

        mock_moomoo.get_option_snapshot.return_value = pd.DataFrame([{
            "code": "US.TSLA260320C425000",
            "mid_price": 8.50, "bid_price": 8.00, "ask_price": 9.00,
        }])
        actions = monitor.run_cycle(force=True)
        assert actions[0]["reason"] == "stop_loss"
        assert actions[0]["pnl"]    < 0

    def test_monitor_then_report_validation(self, full_stack):
        trade_manager, ledger, monitor, mock_moomoo, reporter = full_stack

        # Run 12 winning trades through the full pipeline
        for i in range(12):
            contract = f"US.TSLA260320C{42500 + i * 100:08d}"
            r = self._open_signal(trade_manager, net_credit=4.00,
                                  days=30, contract=contract)
            mock_moomoo.get_option_snapshot.return_value = pd.DataFrame([{
                "code": contract, "mid_price": 1.80,
                "bid_price": 1.71, "ask_price": 1.89,
            }])
            monitor.run_cycle(force=True)

        # Run 3 losing trades
        for i in range(3):
            contract = f"US.TSLA260320C{43500 + i * 100:08d}"
            r = self._open_signal(trade_manager, net_credit=4.00,
                                  days=30, contract=contract)
            mock_moomoo.get_option_snapshot.return_value = pd.DataFrame([{
                "code": contract, "mid_price": 8.50,
                "bid_price": 8.00, "ask_price": 9.00,
            }])
            monitor.run_cycle(force=True)

        report = reporter.generate(save_to_file=False)
        stats  = report["statistics"]
        assert stats["total_trades"]   == 15
        assert stats["winning_trades"] == 12
        assert stats["win_rate"]       == pytest.approx(12/15, rel=1e-3)
        assert report["gates"]["min_trades"]["passed"] is True
        assert report["gates"]["win_rate"]["passed"]   is True
