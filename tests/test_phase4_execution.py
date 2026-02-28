"""
Phase 4 — Trade Execution Tests
=================================
Tests for: PortfolioGuard, PaperLedger, OrderRouter, TradeManager

Run with:
    cd moomoo
    python3 -m pytest tests/test_phase4_execution.py -v

All tests use synthetic data — zero live API calls.
"""

import pytest
import tempfile
import os
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

from src.strategies.trade_signal import TradeSignal


# ── Signal factory ────────────────────────────────────────────────

def make_signal(
    symbol="US.TSLA",
    strategy_name="covered_call",
    signal_type="covered_call",
    sell_contract="US.TSLA260320C425000",
    buy_contract=None,
    sell_price=4.10,
    buy_price=None,
    net_credit=4.10,
    max_profit=410.0,
    max_loss=None,
    breakeven=405.90,
    reward_risk=None,
    quantity=1,
    dte=30,
    iv_rank=55.0,
    delta=0.28,
    regime="neutral",
) -> TradeSignal:
    return TradeSignal(
        strategy_name=  strategy_name,
        symbol=         symbol,
        timestamp=      datetime.now(),
        action=         "OPEN",
        signal_type=    signal_type,
        sell_contract=  sell_contract,
        buy_contract=   buy_contract,
        quantity=       quantity,
        sell_price=     sell_price,
        buy_price=      buy_price,
        net_credit=     net_credit,
        max_profit=     max_profit,
        max_loss=       max_loss,
        breakeven=      breakeven,
        reward_risk=    reward_risk,
        expiry=         (date.today() + timedelta(days=dte)).strftime("%Y-%m-%d"),
        dte=            dte,
        iv_rank=        iv_rank,
        delta=          delta,
        reason=         "Test signal",
        regime=         regime,
    )


def make_spread_signal(**kwargs) -> TradeSignal:
    defaults = dict(
        strategy_name="bear_call_spread",
        signal_type="bear_call_spread",
        sell_contract="US.TSLA260320C425000",
        buy_contract="US.TSLA260320C435000",
        sell_price=4.10,
        buy_price=1.50,
        net_credit=2.60,
        max_profit=260.0,
        max_loss=740.0,
        breakeven=427.60,
        reward_risk=0.35,
    )
    defaults.update(kwargs)
    return make_signal(**defaults)


TEST_CONFIG = {
    "mode": "paper",
    "logging": {"level": "DEBUG", "file": "logs/test.log",
                "max_bytes": 1048576, "backup_count": 1},
    "portfolio_guard": {
        "max_open_positions": 6,
        "max_risk_pct":       0.05,
        "max_total_risk_pct": 0.20,
        "max_trades_per_day": 3,
        "portfolio_value":    100_000,
    },
    "execution": {
        "fill_timeout_seconds": 60,
    }
}


# ═══════════════════════════════════════════════════════════════════
# PortfolioGuard Tests
# ═══════════════════════════════════════════════════════════════════

class TestPortfolioGuard:

    @pytest.fixture
    def guard(self):
        from src.execution.portfolio_guard import PortfolioGuard
        return PortfolioGuard(TEST_CONFIG)

    def test_approves_valid_signal(self, guard):
        signal = make_signal()
        approved, reason = guard.approve(signal)
        assert approved is True
        assert reason == "approved"

    def test_blocks_when_max_positions_reached(self, guard):
        # Fill up all 6 positions
        for i in range(6):
            s = make_signal(symbol=f"US.SYM{i}", strategy_name="covered_call")
            guard.record_open(s)
        signal = make_signal(symbol="US.NEW")
        approved, reason = guard.approve(signal)
        assert approved is False
        assert "Max open positions" in reason

    def test_blocks_when_daily_limit_reached(self, guard):
        for _ in range(3):
            guard.record_open(make_signal(symbol="US.TSLA",
                                          strategy_name=f"s{_}"))
        approved, reason = guard.approve(make_signal())
        assert approved is False
        assert "Daily trade limit" in reason

    def test_blocks_when_single_trade_risk_too_high(self, guard):
        # max_loss = $6000 on $100k portfolio = 6% > 5% limit
        signal = make_spread_signal(max_loss=6000.0)
        approved, reason = guard.approve(signal)
        assert approved is False
        assert "Per-trade risk" in reason

    def test_blocks_when_total_risk_too_high(self):
        from src.execution.portfolio_guard import PortfolioGuard
        # Use higher daily limit so the risk check is reached before daily limit
        cfg = {**TEST_CONFIG, "portfolio_guard": {
            **TEST_CONFIG["portfolio_guard"],
            "max_trades_per_day": 10,
        }}
        guard = PortfolioGuard(cfg)
        # 3 positions × $6k = $18k = 18% of $100k portfolio
        for i in range(3):
            s = make_spread_signal(symbol=f"US.SYM{i}",
                                   strategy_name=f"strat{i}",
                                   max_loss=6000.0)
            guard.record_open(s)
        # Adding $4k more would be 22% total > 20% limit
        signal = make_spread_signal(symbol="US.NEW", max_loss=4000.0)
        approved, reason = guard.approve(signal)
        assert approved is False
        assert "Total portfolio risk" in reason

    def test_blocks_duplicate_same_symbol_and_strategy(self, guard):
        signal = make_signal(symbol="US.TSLA", strategy_name="covered_call")
        guard.record_open(signal)
        # Same symbol + same strategy = duplicate
        approved, reason = guard.approve(signal)
        assert approved is False
        assert "Duplicate" in reason

    def test_allows_same_symbol_different_strategy(self, guard):
        guard.record_open(make_signal(symbol="US.TSLA",
                                      strategy_name="covered_call"))
        signal = make_spread_signal(symbol="US.TSLA",
                                    strategy_name="bear_call_spread")
        approved, reason = guard.approve(signal)
        assert approved is True

    def test_record_open_increments_count(self, guard):
        assert guard.open_position_count == 0
        guard.record_open(make_signal())
        assert guard.open_position_count == 1

    def test_record_open_increments_daily_counter(self, guard):
        assert guard.trades_today == 0
        guard.record_open(make_signal())
        assert guard.trades_today == 1

    def test_record_close_decrements_count(self, guard):
        signal = make_signal()
        guard.record_open(signal)
        assert guard.open_position_count == 1
        guard.record_close(signal.symbol, signal.strategy_name)
        assert guard.open_position_count == 0

    def test_available_capacity_calculated_correctly(self, guard):
        # max_positions=6, max_trades_per_day=3 → min(6,3) = 3 initially
        assert guard.available_capacity == 3
        guard.record_open(make_signal())
        assert guard.available_capacity == 2

    def test_total_committed_risk_sums_max_loss(self, guard):
        guard.record_open(make_spread_signal(symbol="US.TSLA",
                                             strategy_name="s1",
                                             max_loss=500.0))
        guard.record_open(make_spread_signal(symbol="US.AAPL",
                                             strategy_name="s2",
                                             max_loss=300.0))
        assert guard.total_committed_risk == 800.0

    def test_covered_call_with_none_max_loss_approved(self, guard):
        # Covered calls have no defined max_loss — should still pass risk checks
        signal = make_signal(max_loss=None)
        approved, reason = guard.approve(signal)
        assert approved is True

    def test_update_portfolio_value(self, guard):
        guard.update_portfolio_value(200_000)
        # Now a $6k max_loss = 3% of $200k — should be approved
        signal = make_spread_signal(max_loss=6000.0)
        approved, _ = guard.approve(signal)
        assert approved is True

    def test_record_close_unknown_position_logs_warning(self, guard):
        # Should not raise — just log a warning
        guard.record_close("US.UNKNOWN", "some_strategy")


# ═══════════════════════════════════════════════════════════════════
# PaperLedger Tests
# ═══════════════════════════════════════════════════════════════════

class TestPaperLedger:

    @pytest.fixture
    def ledger(self, tmp_path):
        from src.execution.paper_ledger import PaperLedger
        return PaperLedger(db_path=str(tmp_path / "test_paper.db"))

    def test_record_open_returns_trade_id(self, ledger):
        signal = make_signal()
        trade_id = ledger.record_open(signal, fill_sell=4.10)
        assert isinstance(trade_id, int)
        assert trade_id >= 1

    def test_record_open_covered_call(self, ledger):
        signal   = make_signal()
        trade_id = ledger.record_open(signal, fill_sell=4.10, fill_buy=None)
        trade    = ledger.get_trade(trade_id)
        assert trade["symbol"]        == "US.TSLA"
        assert trade["strategy_name"] == "covered_call"
        assert trade["net_credit"]    == pytest.approx(4.10)
        assert trade["buy_price"]     is None
        assert trade["status"]        == "open"

    def test_record_open_spread(self, ledger):
        signal   = make_spread_signal()
        trade_id = ledger.record_open(signal, fill_sell=4.10, fill_buy=1.50)
        trade    = ledger.get_trade(trade_id)
        assert trade["net_credit"]  == pytest.approx(2.60)
        assert trade["buy_price"]   == pytest.approx(1.50)
        assert trade["buy_contract"] == "US.TSLA260320C435000"

    def test_record_close_expired_worthless(self, ledger):
        signal   = make_signal(net_credit=4.10, max_profit=410.0)
        trade_id = ledger.record_open(signal, fill_sell=4.10)
        pnl      = ledger.record_close(trade_id, close_price=0.0,
                                        close_reason="expired_worthless")
        # credit $4.10, close $0.00 → P&L = $4.10 × 100 = $410
        assert pnl == pytest.approx(410.0)

    def test_record_close_stop_loss(self, ledger):
        signal   = make_spread_signal(net_credit=2.60)
        trade_id = ledger.record_open(signal, fill_sell=4.10, fill_buy=1.50)
        # Close at 2× credit (2.0 × 2.60 = stop loss)
        pnl = ledger.record_close(trade_id, close_price=5.20,
                                   close_reason="stop_loss")
        # Opened at $2.60 credit, closed at $5.20 debit → loss = ($2.60-$5.20)×100
        assert pnl == pytest.approx(-260.0)

    def test_record_close_updates_status(self, ledger):
        signal   = make_signal()
        trade_id = ledger.record_open(signal, fill_sell=4.10)
        ledger.record_close(trade_id, close_price=0.0,
                            close_reason="expired_worthless")
        trade = ledger.get_trade(trade_id)
        assert trade["status"] == "expired"
        assert trade["pnl"] is not None

    def test_record_close_stop_loss_status_is_closed(self, ledger):
        signal   = make_signal()
        trade_id = ledger.record_open(signal, fill_sell=4.10)
        ledger.record_close(trade_id, close_price=2.00, close_reason="stop_loss")
        trade = ledger.get_trade(trade_id)
        assert trade["status"] == "closed"

    def test_record_close_invalid_reason_raises(self, ledger):
        signal   = make_signal()
        trade_id = ledger.record_open(signal, fill_sell=4.10)
        with pytest.raises(ValueError, match="close_reason"):
            ledger.record_close(trade_id, close_price=0.0,
                                close_reason="bad_reason")

    def test_get_open_trades_returns_only_open(self, ledger):
        s1 = make_signal(symbol="US.TSLA", strategy_name="covered_call")
        s2 = make_spread_signal(symbol="US.AAPL", strategy_name="bear_call_spread")
        id1 = ledger.record_open(s1, fill_sell=4.10)
        id2 = ledger.record_open(s2, fill_sell=4.10, fill_buy=1.50)
        # Close one
        ledger.record_close(id1, close_price=0.0, close_reason="expired_worthless")

        open_trades = ledger.get_open_trades()
        assert len(open_trades) == 1
        assert open_trades[0]["symbol"] == "US.AAPL"

    def test_statistics_with_no_trades(self, ledger):
        stats = ledger.get_statistics()
        assert stats["total_trades"]  == 0
        assert stats["win_rate"]      == 0
        assert stats["total_pnl"]     == 0

    def test_statistics_win_rate_calculation(self, ledger):
        # 3 winners, 1 loser
        for i in range(3):
            s  = make_signal(symbol=f"US.SYM{i}", strategy_name=f"s{i}",
                             net_credit=4.10)
            id = ledger.record_open(s, fill_sell=4.10)
            ledger.record_close(id, 0.0, "expired_worthless")  # winner

        s  = make_signal(symbol="US.LOSER", strategy_name="loser",
                         net_credit=4.10)
        id = ledger.record_open(s, fill_sell=4.10)
        ledger.record_close(id, 10.0, "stop_loss")  # loser

        stats = ledger.get_statistics()
        assert stats["total_trades"]  == 4
        assert stats["winning_trades"] == 3
        assert stats["win_rate"]      == pytest.approx(0.75)

    def test_statistics_by_strategy_breakdown(self, ledger):
        s1 = make_signal(strategy_name="covered_call", net_credit=4.10)
        s2 = make_spread_signal(symbol="US.AAPL",
                                strategy_name="bear_call_spread",
                                net_credit=2.60)
        id1 = ledger.record_open(s1, fill_sell=4.10)
        id2 = ledger.record_open(s2, fill_sell=4.10, fill_buy=1.50)
        ledger.record_close(id1, 0.0, "expired_worthless")
        ledger.record_close(id2, 0.0, "expired_worthless")

        stats = ledger.get_statistics()
        assert "covered_call"     in stats["by_strategy"]
        assert "bear_call_spread" in stats["by_strategy"]

    def test_get_trade_returns_none_for_unknown_id(self, ledger):
        assert ledger.get_trade(9999) is None

    def test_multiple_trades_same_symbol(self, ledger):
        for i in range(3):
            s = make_signal(net_credit=4.10)
            ledger.record_open(s, fill_sell=4.10)
        assert len(ledger.get_open_trades()) == 3


# ═══════════════════════════════════════════════════════════════════
# OrderRouter Tests
# ═══════════════════════════════════════════════════════════════════

class TestOrderRouter:

    @pytest.fixture
    def router_paper(self):
        from src.execution.order_router import OrderRouter
        mock_moomoo = MagicMock()
        return OrderRouter(TEST_CONFIG, mock_moomoo)

    def test_paper_covered_call_returns_fill_result(self, router_paper):
        from src.execution.order_router import FillResult
        signal = make_signal()
        result = router_paper.execute(signal)
        assert isinstance(result, FillResult)
        assert result.is_paper    is True
        assert result.status      == "filled"
        assert result.fill_sell   == signal.sell_price
        assert result.fill_buy    is None
        assert result.net_credit  == signal.net_credit

    def test_paper_spread_returns_both_fill_prices(self, router_paper):
        signal = make_spread_signal()
        result = router_paper.execute(signal)
        assert result.fill_sell  == signal.sell_price
        assert result.fill_buy   == signal.buy_price
        assert result.net_credit == pytest.approx(signal.net_credit)

    def test_paper_fill_never_calls_moomoo(self, router_paper):
        signal = make_signal()
        router_paper.execute(signal)
        router_paper._connector.place_limit_order.assert_not_called()
        router_paper._connector.place_combo_order.assert_not_called()

    def test_paper_mode_is_detected_from_config(self, router_paper):
        assert router_paper._is_paper is True

    def test_live_mode_detected_from_config(self):
        from src.execution.order_router import OrderRouter
        live_config = {**TEST_CONFIG, "mode": "live"}
        router = OrderRouter(live_config, MagicMock())
        assert router._is_paper is False

    def test_paper_fill_result_has_timestamp(self, router_paper):
        signal = make_signal()
        result = router_paper.execute(signal)
        assert isinstance(result.filled_at, datetime)

    def test_paper_net_credit_matches_signal(self, router_paper):
        signal = make_signal(sell_price=5.50, net_credit=5.50)
        result = router_paper.execute(signal)
        assert result.net_credit == pytest.approx(5.50)


# ═══════════════════════════════════════════════════════════════════
# TradeManager Tests
# ═══════════════════════════════════════════════════════════════════

class TestTradeManager:

    @pytest.fixture
    def manager(self, tmp_path):
        from src.execution.trade_manager import TradeManager
        from src.execution.portfolio_guard import PortfolioGuard
        from src.execution.order_router import OrderRouter
        from src.execution.paper_ledger import PaperLedger

        guard  = PortfolioGuard(TEST_CONFIG)
        router = OrderRouter(TEST_CONFIG, MagicMock())
        ledger = PaperLedger(db_path=str(tmp_path / "test_trades.db"))
        return TradeManager(TEST_CONFIG, guard, router, ledger)

    def test_process_signal_returns_trade_result(self, manager):
        from src.execution.trade_manager import TradeResult
        signal = make_signal()
        result = manager.process_signal(signal)
        assert isinstance(result, TradeResult)

    def test_process_signal_executes_in_paper_mode(self, manager):
        signal = make_signal()
        result = manager.process_signal(signal)
        assert result.approved  is True
        assert result.executed  is True
        assert result.trade_id  is not None
        assert result.fill      is not None
        assert result.blocked_reason is None

    def test_process_signal_blocked_by_guard(self, manager):
        # Fill up positions first
        for i in range(6):
            s = make_signal(symbol=f"US.SYM{i}", strategy_name=f"s{i}")
            manager.process_signal(s)
        # 7th should be blocked
        result = manager.process_signal(make_signal(symbol="US.NEW"))
        assert result.approved  is False
        assert result.executed  is False
        assert result.blocked_reason is not None

    def test_process_signal_updates_portfolio_state(self, manager):
        assert manager._guard.open_position_count == 0
        signal = make_signal()
        manager.process_signal(signal)
        assert manager._guard.open_position_count == 1

    def test_process_signal_records_in_ledger(self, manager):
        signal = make_signal()
        result = manager.process_signal(signal)
        trade  = manager._ledger.get_trade(result.trade_id)
        assert trade is not None
        assert trade["symbol"] == "US.TSLA"

    def test_process_signals_batch(self, manager):
        signals = [
            make_signal(symbol="US.TSLA", strategy_name="covered_call"),
            make_spread_signal(symbol="US.AAPL"),
        ]
        results = manager.process_signals(signals)
        assert len(results) == 2
        assert all(r.executed for r in results)

    def test_close_trade_updates_ledger_and_guard(self, manager):
        signal   = make_signal()
        result   = manager.process_signal(signal)
        trade_id = result.trade_id

        assert manager._guard.open_position_count == 1
        pnl = manager.close_trade(
            trade_id=      trade_id,
            close_price=   0.0,
            close_reason=  "expired_worthless",
            symbol=        signal.symbol,
            strategy_name= signal.strategy_name,
        )
        assert pnl == pytest.approx(410.0)   # $4.10 × 100
        assert manager._guard.open_position_count == 0

    def test_close_trade_stop_loss_negative_pnl(self, manager):
        signal   = make_signal(sell_price=4.10, net_credit=4.10)
        result   = manager.process_signal(signal)
        # Close at 2× credit = stop loss hit
        pnl = manager.close_trade(
            trade_id=      result.trade_id,
            close_price=   8.20,
            close_reason=  "stop_loss",
            symbol=        signal.symbol,
            strategy_name= signal.strategy_name,
        )
        assert pnl < 0

    def test_get_portfolio_summary_structure(self, manager):
        summary = manager.get_portfolio_summary()
        assert "mode"               in summary
        assert "open_positions"     in summary
        assert "trades_today"       in summary
        assert "available_capacity" in summary
        assert "committed_risk"     in summary
        assert "paper_stats"        in summary

    def test_live_mode_declines_confirmation(self):
        """Live mode confirmation always returns False during development."""
        from src.execution.trade_manager import TradeManager
        from src.execution.portfolio_guard import PortfolioGuard
        from src.execution.order_router import OrderRouter
        from src.execution.paper_ledger import PaperLedger
        import tempfile

        live_cfg = {**TEST_CONFIG, "mode": "live"}
        with tempfile.TemporaryDirectory() as tmp:
            guard  = PortfolioGuard(live_cfg)
            router = OrderRouter(live_cfg, MagicMock())
            ledger = PaperLedger(db_path=os.path.join(tmp, "live_test.db"))
            manager = TradeManager(live_cfg, guard, router, ledger)

            signal = make_signal()
            result = manager.process_signal(signal)

            # Approved by guard but declined at confirmation step
            assert result.approved  is True
            assert result.executed  is False
            assert result.blocked_reason == "live_confirmation_declined"


# ═══════════════════════════════════════════════════════════════════
# Integration: Full signal → guard → execute → ledger round-trip
# ═══════════════════════════════════════════════════════════════════

class TestPhase4Integration:

    @pytest.fixture
    def full_stack(self, tmp_path):
        from src.execution.trade_manager import TradeManager
        from src.execution.portfolio_guard import PortfolioGuard
        from src.execution.order_router import OrderRouter
        from src.execution.paper_ledger import PaperLedger

        guard   = PortfolioGuard(TEST_CONFIG)
        router  = OrderRouter(TEST_CONFIG, MagicMock())
        ledger  = PaperLedger(db_path=str(tmp_path / "integration.db"))
        manager = TradeManager(TEST_CONFIG, guard, router, ledger)
        return manager, guard, ledger

    def test_full_lifecycle_covered_call(self, full_stack):
        manager, guard, ledger = full_stack

        # Open
        signal = make_signal()
        result = manager.process_signal(signal)
        assert result.executed
        assert guard.open_position_count == 1

        trade = ledger.get_trade(result.trade_id)
        assert trade["status"] == "open"

        # Close (expired worthless = max profit)
        pnl = manager.close_trade(
            trade_id=      result.trade_id,
            close_price=   0.0,
            close_reason=  "expired_worthless",
            symbol=        signal.symbol,
            strategy_name= signal.strategy_name,
        )
        assert pnl   == pytest.approx(410.0)
        assert guard.open_position_count == 0

        trade = ledger.get_trade(result.trade_id)
        assert trade["status"] == "expired"
        assert trade["pnl"]    == pytest.approx(410.0)

    def test_full_lifecycle_spread(self, full_stack):
        manager, guard, ledger = full_stack

        signal = make_spread_signal()
        result = manager.process_signal(signal)
        assert result.executed
        assert result.fill.net_credit == pytest.approx(2.60)

        pnl = manager.close_trade(
            trade_id=      result.trade_id,
            close_price=   0.0,
            close_reason=  "expired_worthless",
            symbol=        signal.symbol,
            strategy_name= signal.strategy_name,
        )
        assert pnl == pytest.approx(260.0)   # 2.60 × 100

    def test_daily_limit_resets_next_day(self, full_stack):
        manager, guard, ledger = full_stack

        # Max out today
        for i in range(3):
            manager.process_signal(
                make_signal(symbol=f"US.SYM{i}", strategy_name=f"s{i}")
            )
        assert guard.trades_today == 3

        # Simulate next day by resetting the date tracker
        from datetime import timedelta
        guard._last_reset_date = date.today() - timedelta(days=1)
        guard._reset_daily_counter_if_needed()
        assert guard.trades_today == 0

        # Should now accept new trades
        result = manager.process_signal(make_signal(symbol="US.NEW"))
        assert result.executed is True

    def test_statistics_after_mixed_results(self, full_stack):
        manager, guard, ledger = full_stack

        # 2 winners, 1 loser
        for i in range(2):
            s = make_signal(symbol=f"US.W{i}", strategy_name=f"w{i}",
                            net_credit=4.10)
            r = manager.process_signal(s)
            manager.close_trade(r.trade_id, 0.0, "expired_worthless",
                                s.symbol, s.strategy_name)

        s = make_signal(symbol="US.LOSER", strategy_name="loser",
                        net_credit=4.10)
        r = manager.process_signal(s)
        manager.close_trade(r.trade_id, 8.20, "stop_loss",
                            s.symbol, s.strategy_name)

        stats = ledger.get_statistics()
        assert stats["total_trades"]   == 3
        assert stats["winning_trades"] == 2
        assert stats["win_rate"]       == pytest.approx(2/3)
        assert stats["total_pnl"]      == pytest.approx(410 + 410 - 410)
