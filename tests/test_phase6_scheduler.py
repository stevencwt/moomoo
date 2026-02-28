"""
Phase 6 — Scheduler & Wiring Tests
=====================================
Tests for: BotScheduler, main.py config loading, job isolation,
           component wiring, graceful shutdown behaviour.

Run with:
    cd moomoo
    python3 -m pytest tests/test_phase6_scheduler.py -v

All tests mock external dependencies — no live API calls, no real scheduling.
"""

import pytest
import os
import yaml
import tempfile
from datetime import datetime
from unittest.mock import MagicMock, patch, call

TEST_CONFIG = {
    "mode": "paper",
    "moomoo": {"host": "127.0.0.1", "port": 11111, "trade_env": "SIMULATE"},
    "logging": {"level": "DEBUG", "file": "logs/test.log",
                "max_bytes": 1048576, "backup_count": 1},
    "universe": {"watchlist": ["US.TSLA"]},
    "regime": {
        "high_vol_vix_threshold": 25.0,
        "bull_rsi_threshold":     55.0,
        "bear_rsi_threshold":     45.0,
        "macd_threshold":         0.0,
    },
    "options": {
        "target_dte_min":         21,
        "target_dte_max":         45,
        "earnings_buffer_days":   7,
        "min_open_interest":      100,
        "otm_call_delta_min":     0.20,
        "otm_call_delta_max":     0.35,
        "spread_width_target":    10.0,
    },
    "strategies": {
        "covered_call": {
            "enabled": True, "min_iv_rank": 30,
            "max_rsi": 70, "max_concurrent_positions": 2,
        },
        "bear_call_spread": {
            "enabled": True, "min_iv_rank": 35,
            "min_rsi_for_spread": 45, "min_pct_b": 0.40,
            "min_credit": 0.50, "min_reward_risk": 0.20,
            "spread_width_target": 10.0, "max_concurrent_positions": 3,
            "allowed_regimes": ["bear", "neutral"],
        }
    },
    "portfolio_guard": {
        "max_open_positions":   6,
        "max_risk_pct":         0.05,
        "max_total_risk_pct":   0.20,
        "max_trades_per_day":   3,
        "portfolio_value":      100_000,
    },
    "execution":  {"fill_timeout_seconds": 60},
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
    "scheduler": {
        "market_scan_time":   "09:35",
        "iv_collection_time": "16:05",
        "weekly_report_day":  "friday",
        "weekly_report_time": "16:30",
    },
    "validation": {
        "min_trades": 10, "min_win_rate": 0.60, "min_sharpe_like": 0.50,
    },
}


# ── Fixtures ──────────────────────────────────────────────────────

@pytest.fixture
def scheduler(tmp_path):
    """
    Build a BotScheduler using the DI constructor with all components mocked.
    No real API calls. Real execution/monitoring components with SQLite in tmp_path.
    """
    from src.scheduler.bot_scheduler import BotScheduler
    from src.execution.paper_ledger import PaperLedger
    from src.execution.portfolio_guard import PortfolioGuard
    from src.execution.order_router import OrderRouter
    from src.execution.trade_manager import TradeManager
    from src.monitoring.exit_evaluator import ExitEvaluator
    from src.monitoring.position_monitor import PositionMonitor
    from src.monitoring.validation_reporter import ValidationReporter
    from src.strategies.strategy_registry import StrategyRegistry
    from src.strategies.premium_selling.covered_call import CoveredCallStrategy
    from src.strategies.premium_selling.bear_call_spread import BearCallSpreadStrategy
    from src.notifier.signal_notifier import SignalNotifier

    cfg = {
        **TEST_CONFIG,
        "paper_ledger": {"db_path": str(tmp_path / "test.db")},
    }

    mock_moomoo   = MagicMock()
    mock_yfinance = MagicMock()
    mock_options  = MagicMock()
    mock_iv_calc  = MagicMock()

    # Real components that don't touch external APIs
    ledger    = PaperLedger(db_path=str(tmp_path / "test.db"))
    guard     = PortfolioGuard(cfg)
    router    = OrderRouter(cfg, mock_moomoo)
    manager   = TradeManager(cfg, guard, router, ledger)
    evaluator = ExitEvaluator(cfg)
    monitor   = PositionMonitor(cfg, ledger, manager, mock_moomoo, evaluator)
    reporter  = ValidationReporter(cfg, ledger)

    # Registry with real strategy objects (mocked connectors injected)
    registry = StrategyRegistry()
    registry.register(CoveredCallStrategy(cfg, mock_moomoo, mock_options))
    registry.register(BearCallSpreadStrategy(cfg, mock_moomoo, mock_options))

    mock_scanner = MagicMock()
    mock_scanner.scan_universe.return_value = []

    return BotScheduler(
        config=cfg,
        moomoo=mock_moomoo,
        yfinance=mock_yfinance,
        scanner=mock_scanner,
        registry=registry,
        guard=guard,
        router=router,
        ledger=ledger,
        manager=manager,
        evaluator=evaluator,
        monitor=monitor,
        reporter=reporter,
        options_analyser=mock_options,
        iv_calculator=mock_iv_calc,
        notifier=SignalNotifier(),
    )


# ═══════════════════════════════════════════════════════════════════
# Config Loading Tests
# ═══════════════════════════════════════════════════════════════════

class TestConfigLoading:

    def test_load_valid_config(self, tmp_path):
        from main import load_config
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump(TEST_CONFIG))
        config = load_config(str(cfg_file))
        assert config["mode"] == "paper"

    def test_load_config_missing_file_exits(self, tmp_path):
        from main import load_config
        with pytest.raises(SystemExit):
            load_config(str(tmp_path / "nonexistent.yaml"))

    def test_validate_config_paper_mode_ok(self, tmp_path):
        from main import validate_config
        cfg = {**TEST_CONFIG, "mode": "paper",
               "universe": {"watchlist": ["US.TSLA"]}}
        validate_config(cfg)   # Should not raise

    def test_validate_config_invalid_mode_exits(self):
        from main import validate_config
        with pytest.raises(SystemExit):
            validate_config({"mode": "invalid",
                             "universe": {"watchlist": ["US.TSLA"]}})

    def test_validate_config_empty_watchlist_exits(self):
        from main import validate_config
        with pytest.raises(SystemExit):
            validate_config({"mode": "paper", "universe": {"watchlist": []}})

    def test_validate_config_live_mode_prompts(self):
        from main import validate_config
        cfg = {"mode": "live", "universe": {"watchlist": ["US.TSLA"]}}
        # Simulate user typing "no" — should exit
        with patch("builtins.input", return_value="no"):
            with pytest.raises(SystemExit):
                validate_config(cfg)

    def test_validate_config_live_mode_yes_proceeds(self):
        from main import validate_config
        cfg = {"mode": "live", "universe": {"watchlist": ["US.TSLA"]}}
        with patch("builtins.input", return_value="yes"):
            validate_config(cfg)  # Should not raise


# ═══════════════════════════════════════════════════════════════════
# BotScheduler Wiring Tests
# ═══════════════════════════════════════════════════════════════════

class TestBotSchedulerWiring:

    def test_scheduler_initialises_all_components(self, scheduler):
        assert scheduler._registry    is not None
        assert scheduler._guard       is not None
        assert scheduler._router      is not None
        assert scheduler._ledger      is not None
        assert scheduler._manager     is not None
        assert scheduler._monitor     is not None
        assert scheduler._reporter    is not None

    def test_registry_has_two_strategies(self, scheduler):
        assert len(scheduler._registry.strategy_names) == 2
        assert "covered_call"     in scheduler._registry.strategy_names
        assert "bear_call_spread" in scheduler._registry.strategy_names

    def test_both_strategies_enabled(self, scheduler):
        assert scheduler._registry.enabled_count == 2

    def test_mode_is_paper(self, scheduler):
        assert scheduler._mode      == "paper"
        assert scheduler._router._is_paper is True

    def test_portfolio_guard_configured(self, scheduler):
        assert scheduler._guard._max_open_positions == 6
        assert scheduler._guard._max_trades_per_day == 3

    def test_exit_evaluator_configured(self, scheduler):
        assert scheduler._evaluator._stop_loss_multiplier == 2.0
        assert scheduler._evaluator._take_profit_pct      == 0.50
        assert scheduler._evaluator._dte_close_threshold  == 21


# ═══════════════════════════════════════════════════════════════════
# Job Execution Tests
# ═══════════════════════════════════════════════════════════════════

class TestScheduledJobs:

    def test_scan_job_calls_scanner_and_registry(self, scheduler):
        from src.market.market_snapshot import MarketSnapshot, Technicals, OptionsContext
        from datetime import date, timedelta

        # Build a minimal valid snapshot
        snap = MagicMock(spec=MarketSnapshot)
        snap.symbol = "US.TSLA"
        scheduler._scanner.scan_universe.return_value = [snap]
        scheduler._registry = MagicMock()
        scheduler._registry.evaluate_universe.return_value = []

        scheduler._scan_job()

        scheduler._scanner.scan_universe.assert_called_once()
        scheduler._registry.evaluate_universe.assert_called_once_with([snap])

    def test_scan_job_processes_signals_when_generated(self, scheduler):
        from src.strategies.trade_signal import TradeSignal

        snap = MagicMock()
        scheduler._scanner.scan_universe.return_value = [snap]

        mock_signal = MagicMock(spec=TradeSignal)
        mock_signal.net_credit = 4.10
        mock_signal.dte = 30

        scheduler._registry = MagicMock()
        scheduler._registry.evaluate_universe.return_value = [mock_signal]
        scheduler._manager = MagicMock()
        scheduler._manager.process_signals.return_value = [
            MagicMock(executed=True, approved=True, signal=mock_signal)
        ]
        scheduler._notifier = MagicMock()  # prevent real formatting of MagicMock signal

        scheduler._scan_job()

        scheduler._manager.process_signals.assert_called_once_with([mock_signal])

    def test_scan_job_handles_empty_universe(self, scheduler):
        scheduler._scanner.scan_universe.return_value = []
        # Should not raise
        scheduler._scan_job()

    def test_monitor_job_calls_run_cycle(self, scheduler):
        scheduler._monitor = MagicMock()
        scheduler._monitor.run_cycle.return_value = []
        scheduler._monitor_job()
        scheduler._monitor.run_cycle.assert_called_once()

    def test_monitor_job_with_force_flag(self, scheduler):
        scheduler._monitor = MagicMock()
        scheduler._monitor.run_cycle.return_value = []
        scheduler._monitor_job(force=True)
        scheduler._monitor.run_cycle.assert_called_once_with(force=True)

    def test_iv_job_collects_for_watchlist(self, scheduler):
        scheduler._moomoo.get_option_expiries.return_value  = ["2026-03-20"]
        scheduler._moomoo.get_option_chain.return_value     = MagicMock()
        scheduler._moomoo.get_option_chain.return_value.__len__ = lambda s: 5
        scheduler._moomoo.get_option_snapshot.return_value  = MagicMock()
        scheduler._yfinance.get_current_price.return_value  = 410.0
        scheduler._options_analyser = MagicMock()
        scheduler._options_analyser.get_atm_iv.return_value = 35.0
        scheduler._iv_calculator = MagicMock()

        scheduler._iv_job()

        # Should attempt to collect IV for US.TSLA
        scheduler._moomoo.get_option_expiries.assert_called_with("US.TSLA")

    def test_report_job_calls_reporter(self, scheduler):
        scheduler._reporter = MagicMock()
        scheduler._reporter.generate.return_value = {
            "go_live": False,
            "gates": {}
        }
        scheduler._report_job()
        scheduler._reporter.generate.assert_called_once_with(save_to_file=True)

    def test_report_job_logs_when_go_live(self, scheduler):
        scheduler._reporter = MagicMock()
        scheduler._reporter.generate.return_value = {
            "go_live": True,
            "gates": {"g": {"passed": True}}
        }
        # Should not raise — just logs a warning
        scheduler._report_job()

    def test_report_job_logs_failing_gates(self, scheduler):
        scheduler._reporter = MagicMock()
        scheduler._reporter.generate.return_value = {
            "go_live": False,
            "gates": {
                "min_trades": {"passed": False, "name": "Minimum trade count"},
                "win_rate":   {"passed": True,  "name": "Win rate"},
            }
        }
        scheduler._report_job()
        scheduler._reporter.generate.assert_called_once()


# ═══════════════════════════════════════════════════════════════════
# Job Isolation Tests
# ═══════════════════════════════════════════════════════════════════

class TestJobIsolation:

    def test_scan_job_exception_does_not_crash_scheduler(self, scheduler):
        """_safe_run must catch all exceptions from any job."""
        def boom():
            raise RuntimeError("scan failed catastrophically")

        # Should not raise
        scheduler._safe_run(boom, "test_job")

    def test_monitor_job_exception_does_not_crash_scheduler(self, scheduler):
        scheduler._monitor = MagicMock()
        scheduler._monitor.run_cycle.side_effect = Exception("monitor exploded")

        scheduler._safe_run(scheduler._monitor_job, "monitor")
        # Still alive — no exception propagated

    def test_iv_job_exception_per_symbol_continues(self, scheduler):
        """IV job should continue with other symbols even if one fails."""
        scheduler._moomoo.get_option_expiries.side_effect = Exception("API down")
        # Should not raise — error is caught per-symbol
        scheduler._iv_job()

    def test_multiple_job_failures_dont_accumulate(self, scheduler):
        """Running multiple failing jobs in sequence should all be safe."""
        bad_job = MagicMock(side_effect=Exception("always fails"))
        for _ in range(3):
            scheduler._safe_run(bad_job, "bad_job")
        # All 3 calls completed, 3 failures caught
        assert bad_job.call_count == 3


# ═══════════════════════════════════════════════════════════════════
# Startup Behaviour Tests
# ═══════════════════════════════════════════════════════════════════

class TestStartupBehaviour:

    def test_startup_runs_initial_monitor_cycle(self, scheduler):
        """Bot should check existing positions immediately on startup."""
        scheduler._monitor = MagicMock()
        scheduler._monitor.run_cycle.return_value = []
        scheduler._reporter = MagicMock()
        scheduler._reporter.print_current_status = MagicMock()
        scheduler._moomoo.connect = MagicMock()

        # Patch schedule.run_pending and the running loop to exit immediately
        with patch("src.scheduler.bot_scheduler.schedule") as mock_schedule, \
             patch("src.scheduler.bot_scheduler.time.sleep",
                   side_effect=KeyboardInterrupt):
            mock_schedule.every.return_value = MagicMock()
            try:
                scheduler.start()
            except (KeyboardInterrupt, SystemExit):
                pass

        # Initial monitor cycle must have been called
        scheduler._monitor.run_cycle.assert_called()

    def test_stop_clears_schedule_and_disconnects(self, scheduler):
        scheduler._running = True
        scheduler._moomoo.disconnect = MagicMock()

        with patch("src.scheduler.bot_scheduler.schedule") as mock_sched:
            scheduler.stop()

        assert scheduler._running is False
        mock_sched.clear.assert_called_once()
        scheduler._moomoo.disconnect.assert_called_once()

    def test_stop_handles_disconnect_exception(self, scheduler):
        """Disconnect failure should not prevent clean shutdown."""
        scheduler._running = True
        scheduler._moomoo.disconnect.side_effect = Exception("already disconnected")
        with patch("src.scheduler.bot_scheduler.schedule"):
            scheduler.stop()   # Should not raise
        assert scheduler._running is False


# ═══════════════════════════════════════════════════════════════════
# Config YAML Round-trip Test
# ═══════════════════════════════════════════════════════════════════

class TestConfigYaml:

    def test_default_config_is_valid_yaml(self):
        config_path = os.path.join(
            os.path.dirname(__file__), "..", "config", "config.yaml"
        )
        if os.path.exists(config_path):
            with open(config_path) as f:
                cfg = yaml.safe_load(f)
            assert cfg["mode"] == "paper"
            assert "watchlist" in cfg["universe"]
            assert "covered_call" in cfg["strategies"]
            assert "bear_call_spread" in cfg["strategies"]
        else:
            pytest.skip("config/config.yaml not found — copy from outputs")

    def test_config_has_all_required_sections(self):
        config_path = os.path.join(
            os.path.dirname(__file__), "..", "config", "config.yaml"
        )
        if not os.path.exists(config_path):
            pytest.skip("config/config.yaml not found")

        with open(config_path) as f:
            cfg = yaml.safe_load(f)

        required = [
            "mode", "moomoo", "logging", "universe", "regime",
            "options", "strategies", "portfolio_guard",
            "execution", "position_monitor", "scheduler", "validation",
        ]
        for section in required:
            assert section in cfg, f"Missing config section: {section}"

    def test_config_paper_mode_by_default(self):
        config_path = os.path.join(
            os.path.dirname(__file__), "..", "config", "config.yaml"
        )
        if not os.path.exists(config_path):
            pytest.skip("config/config.yaml not found")

        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        # SAFETY: default must always be paper
        assert cfg["mode"] == "paper", (
            "config.yaml must default to mode: paper for safety"
        )


# ═══════════════════════════════════════════════════════════════════
# End-to-end wiring smoke test
# ═══════════════════════════════════════════════════════════════════

class TestEndToEndWiring:

    def test_full_pipeline_paper_trade_via_scheduler(self, scheduler, tmp_path):
        """
        Smoke test: verify the full signal→guard→execute→ledger→monitor
        pipeline works when wired through the scheduler.
        """
        from src.strategies.trade_signal import TradeSignal
        from datetime import date, timedelta

        # Build a real signal
        signal = TradeSignal(
            strategy_name="covered_call", symbol="US.TSLA",
            timestamp=datetime.now(), action="OPEN",
            signal_type="covered_call",
            sell_contract="US.TSLA260320C425000", buy_contract=None,
            quantity=1, sell_price=4.10, buy_price=None,
            net_credit=4.10, max_profit=410.0, max_loss=None,
            breakeven=405.90, reward_risk=None,
            expiry=(date.today() + timedelta(days=30)).strftime("%Y-%m-%d"),
            dte=30, iv_rank=55.0, delta=0.28,
            reason="Smoke test", regime="neutral",
        )

        # Feed the signal through the manager (bypasses scanner/registry)
        result = scheduler._manager.process_signal(signal)

        assert result.executed is True
        assert result.trade_id is not None
        assert scheduler._guard.open_position_count == 1

        # Verify it's in the ledger
        trade = scheduler._ledger.get_trade(result.trade_id)
        assert trade["symbol"] == "US.TSLA"
        assert trade["status"] == "open"

    def test_paper_mode_never_calls_live_order_methods(self, scheduler):
        """In paper mode, MooMoo order placement methods must never be called."""
        from src.strategies.trade_signal import TradeSignal
        from datetime import date, timedelta

        signal = TradeSignal(
            strategy_name="covered_call", symbol="US.TSLA",
            timestamp=datetime.now(), action="OPEN",
            signal_type="covered_call",
            sell_contract="US.TSLA260320C425000", buy_contract=None,
            quantity=1, sell_price=4.10, buy_price=None,
            net_credit=4.10, max_profit=410.0, max_loss=None,
            breakeven=405.90, reward_risk=None,
            expiry=(date.today() + timedelta(days=30)).strftime("%Y-%m-%d"),
            dte=30, iv_rank=55.0, delta=0.28,
            reason="Smoke test", regime="neutral",
        )

        scheduler._manager.process_signal(signal)

        # In paper mode, these must NEVER be called
        scheduler._moomoo.place_limit_order.assert_not_called()
        scheduler._moomoo.place_combo_order.assert_not_called()
