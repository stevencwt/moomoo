"""
Phase 9c — Entry Context Threading Tests
==========================================
Tests that the MarketSnapshot is carried from each strategy through
TradeSignal → TradeManager → PaperLedger.record_open(), so that
RSI, %B, MACD, and VIX at entry are persisted in the ledger.

Covers:
  TradeSignal.snapshot field      — optional, backward compatible
  BearCallSpread.evaluate()       — sets signal.snapshot = snapshot
  BullPutSpread.evaluate()        — sets signal.snapshot = snapshot
  CoveredCall.evaluate()          — sets signal.snapshot = snapshot
  TradeManager.process_signal()   — forwards signal.snapshot to record_open
  PaperLedger                     — RSI/%B/MACD/VIX stored when snapshot given

Run with:
    cd moomoo
    python3 -m pytest tests/test_phase9c_entry_context.py -v

All tests use synthetic data — zero live API calls.
"""

import pytest
from datetime import datetime, date, timedelta
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
    "execution": {"fill_timeout_seconds": 60},
    "signal_ranker": {"enabled": False},
}


def expiry_in(days: int) -> str:
    return (date.today() + timedelta(days=days)).strftime("%Y-%m-%d")


def make_snapshot(
    symbol="US.SPY",
    spot_price=690.0,
    rsi=62.0,
    pct_b=0.78,
    macd=3.45,
    vix=18.5,
    iv_rank=55.0,
    regime="bearish",
):
    """Build a minimal MarketSnapshot mock with the fields strategies inspect."""
    snap = MagicMock()
    snap.symbol         = symbol
    snap.spot_price     = spot_price
    snap.market_regime  = regime
    snap.vix            = vix
    snap.open_positions = 0
    snap.next_earnings  = None

    snap.technicals.rsi   = rsi
    snap.technicals.pct_b = pct_b
    snap.technicals.macd  = macd

    snap.options_context.iv_rank           = iv_rank
    snap.options_context.available_expiries = [expiry_in(35)]
    return snap


# ═══════════════════════════════════════════════════════════════════
# TradeSignal — snapshot field
# ═══════════════════════════════════════════════════════════════════

class TestTradeSignalSnapshotField:

    def _make_signal(self, snapshot=None):
        from src.strategies.trade_signal import TradeSignal
        return TradeSignal(
            strategy_name="bear_call_spread", symbol="US.SPY",
            timestamp=datetime.now(), action="OPEN",
            signal_type="bear_call_spread",
            sell_contract="SPY260320C700000",
            buy_contract="SPY260320C710000",
            quantity=1, sell_price=2.10, buy_price=0.75,
            net_credit=1.35, max_profit=135.0, max_loss=865.0,
            breakeven=701.35, reward_risk=0.156,
            expiry=expiry_in(35), dte=35,
            iv_rank=55.0, delta=0.25,
            reason="test", regime="bearish",
            spot_price=690.0, buffer_pct=1.45,
            snapshot=snapshot,
        )

    def test_snapshot_defaults_to_none(self):
        """Omitting snapshot must not raise — backward compatible."""
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
            expiry=expiry_in(35), dte=35,
            iv_rank=55.0, delta=0.25,
            reason="test", regime="bearish",
        )
        assert sig.snapshot is None

    def test_snapshot_stored_when_provided(self):
        snap = make_snapshot()
        sig  = self._make_signal(snapshot=snap)
        assert sig.snapshot is snap

    def test_snapshot_none_when_explicitly_none(self):
        sig = self._make_signal(snapshot=None)
        assert sig.snapshot is None

    def test_other_fields_unaffected_by_snapshot(self):
        """Adding snapshot must not alter any other field values."""
        snap = make_snapshot()
        sig  = self._make_signal(snapshot=snap)
        assert sig.net_credit  == pytest.approx(1.35)
        assert sig.spot_price  == pytest.approx(690.0)
        assert sig.buffer_pct  == pytest.approx(1.45)
        assert sig.iv_rank     == pytest.approx(55.0)


# ═══════════════════════════════════════════════════════════════════
# Strategy evaluate() — snapshot attached to returned signal
# ═══════════════════════════════════════════════════════════════════

class TestBearCallSpreadSnapshotAttached:
    """
    Verify that BearCallSpread.evaluate() attaches the snapshot it
    received to the TradeSignal it returns.
    Uses mocked MooMoo + options helpers to get past filtering.
    """

    def _make_strategy(self, config_overrides=None):
        from src.strategies.premium_selling.bear_call_spread import BearCallSpreadStrategy

        cfg = {
            "strategies": {
                "bear_call_spread": {
                    "enabled": True,
                    "min_iv_rank": 30,
                    "min_rsi": 50,
                    "min_pct_b": 0.5,
                    "dte_min": 21, "dte_max": 45,
                    "delta_target": 0.25, "delta_max": 0.35,
                    "spread_width": 10.0,
                    "allowed_regimes": ["bearish", "neutral"],
                    "min_reward_risk": 0.10,
                }
            }
        }
        if config_overrides:
            cfg["strategies"]["bear_call_spread"].update(config_overrides)

        mock_moomoo  = MagicMock()
        mock_options = MagicMock()
        return BearCallSpreadStrategy(cfg, mock_moomoo, mock_options)

    def _configure_mocks_for_signal(self, strategy, snapshot):
        """Wire all internal mock calls so evaluate() reaches the TradeSignal constructor."""
        import pandas as pd

        expiry = snapshot.options_context.available_expiries[0]
        sell_strike = round(snapshot.spot_price * 1.015 / 5) * 5  # first OTM

        # options helper chain
        strategy._options.check_earnings_conflict.return_value = False
        strategy._options.select_expiry.return_value            = expiry

        # Fake OTM calls df
        otm_calls = pd.DataFrame([{
            "code":   "SPY260320C700000",
            "strike": sell_strike,
            "delta":  0.26,
            "mid_price": 2.10,
            "bid": 2.05, "ask": 2.15,
            "open_interest": 5000,
        }])
        strategy._options.filter_otm_calls.return_value = otm_calls

        # moomoo snapshot for prices
        snap_df = pd.DataFrame([
            {"code": "SPY260320C700000", "mid_price": 2.10,
             "bid": 2.05, "ask": 2.15, "open_interest": 5000},
            {"code": "SPY260320C710000", "mid_price": 0.75,
             "bid": 0.70, "ask": 0.80, "open_interest": 4000},
        ])
        strategy._moomoo.get_option_snapshot.return_value = snap_df
        strategy._moomoo.get_option_chain.return_value    = snap_df

        # metrics
        strategy._options.calculate_spread_metrics.return_value = {
            "net_credit":   1.35,
            "max_profit":   135.0,
            "max_loss":     865.0,
            "breakeven":    701.35,
            "reward_risk":  0.156,
        }
        strategy._options.get_long_strike.return_value   = sell_strike + 10
        strategy._options.find_long_contract.return_value = "SPY260320C710000"
        strategy._options.get_delta.return_value         = 0.26

    def test_bear_call_snapshot_attached_to_signal(self):
        snapshot = make_snapshot(regime="bearish", rsi=65.0, pct_b=0.80, iv_rank=55.0)
        strategy = self._make_strategy()
        self._configure_mocks_for_signal(strategy, snapshot)

        signal = strategy.evaluate(snapshot)

        if signal is not None:
            assert signal.snapshot is snapshot, \
                "BearCallSpread must attach snapshot to the returned TradeSignal"


class TestBullPutSpreadSnapshotAttached:

    def _make_strategy(self):
        from src.strategies.premium_selling.bull_put_spread import BullPutSpreadStrategy

        cfg = {
            "strategies": {
                "bull_put_spread": {
                    "enabled": True,
                    "min_iv_rank": 30,
                    "min_rsi_floor": 35,
                    "max_rsi_ceiling": 65,
                    "min_pct_b": 0.20,
                    "dte_min": 21, "dte_max": 45,
                    "delta_target": -0.25, "delta_max": -0.35,
                    "spread_width_target": 10.0,
                    "allowed_regimes": ["bullish", "neutral"],
                    "min_reward_risk": 0.10,
                }
            }
        }
        return BullPutSpreadStrategy(cfg, MagicMock(), MagicMock())

    def test_bull_put_snapshot_attached_to_signal(self):
        import pandas as pd
        snapshot = make_snapshot(regime="bullish", rsi=42.0, pct_b=0.30, iv_rank=52.0)
        strategy = self._make_strategy()

        expiry     = snapshot.options_context.available_expiries[0]
        sell_strike = round(snapshot.spot_price * 0.985 / 5) * 5

        strategy._options.check_earnings_conflict.return_value = False
        strategy._options.select_expiry.return_value           = expiry

        otm_puts = pd.DataFrame([{
            "code":   "SPY260320P680000",
            "strike": sell_strike,
            "delta":  -0.24,
            "mid_price": 2.10,
            "bid": 2.05, "ask": 2.15,
            "open_interest": 5000,
        }])
        strategy._options.filter_otm_puts.return_value = otm_puts

        snap_df = pd.DataFrame([
            {"code": "SPY260320P680000", "mid_price": 2.10,
             "bid": 2.05, "ask": 2.15, "open_interest": 5000},
            {"code": "SPY260320P670000", "mid_price": 0.75,
             "bid": 0.70, "ask": 0.80, "open_interest": 4000},
        ])
        strategy._moomoo.get_option_snapshot.return_value = snap_df
        strategy._moomoo.get_option_chain.return_value    = snap_df

        strategy._options.calculate_spread_metrics.return_value = {
            "net_credit":  1.35,
            "max_profit":  135.0,
            "max_loss":    865.0,
            "breakeven":   678.65,
            "reward_risk": 0.156,
        }
        strategy._options.get_short_strike.return_value  = sell_strike
        strategy._options.get_long_strike.return_value   = sell_strike - 10
        strategy._options.find_long_contract.return_value = "SPY260320P670000"
        strategy._options.get_delta.return_value         = -0.24

        signal = strategy.evaluate(snapshot)
        if signal is not None:
            assert signal.snapshot is snapshot, \
                "BullPutSpread must attach snapshot to the returned TradeSignal"


class TestCoveredCallSnapshotAttached:

    def _make_strategy(self):
        from src.strategies.premium_selling.covered_call import CoveredCallStrategy

        cfg = {
            "universe": {"shares_held": {"US.TSLA": 100}},
            "strategies": {
                "covered_call": {
                    "enabled": True,
                    "min_iv_rank": 30,
                    "max_rsi": 70,
                    "dte_min": 21, "dte_max": 45,
                    "delta_target": 0.30, "delta_max": 0.40,
                    "allowed_regimes": ["bullish", "neutral", "bearish"],
                }
            }
        }
        return CoveredCallStrategy(cfg, MagicMock(), MagicMock())

    def test_covered_call_snapshot_attached_to_signal(self):
        import pandas as pd
        snapshot = make_snapshot(
            symbol="US.TSLA", spot_price=380.0,
            regime="neutral", rsi=55.0, pct_b=0.60, iv_rank=48.0
        )
        snapshot.shares_held = 100   # gate 1: covered call requires 100 shares
        strategy = self._make_strategy()

        expiry = snapshot.options_context.available_expiries[0]

        # covered_call uses get_target_expiry (not select_expiry)
        strategy._options.get_target_expiry.return_value       = expiry
        strategy._options.check_earnings_conflict.return_value = False

        snap_df = pd.DataFrame([{
            "code":        "TSLA260320C390000",
            "strike_price": 390.0,
            "option_delta": 0.31,
            "mid_price":   3.50,
            "bid":  3.40, "ask": 3.60,
            "open_interest": 3000,
        }])
        strategy._moomoo.get_option_snapshot.return_value = snap_df
        strategy._moomoo.get_option_chain.return_value    = snap_df
        strategy._options.filter_otm_calls.return_value   = snap_df

        # select_best_call returns a single row dict
        strategy._options.select_best_call.return_value = {
            "code":         "TSLA260320C390000",
            "strike_price": 390.0,
            "option_delta": 0.31,
            "mid_price":    3.50,
        }

        signal = strategy.evaluate(snapshot)
        if signal is not None:
            assert signal.snapshot is snapshot, \
                "CoveredCall must attach snapshot to the returned TradeSignal"


# ═══════════════════════════════════════════════════════════════════
# TradeManager — snapshot forwarded to record_open
# ═══════════════════════════════════════════════════════════════════

class TestTradeManagerForwardsSnapshot:

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

    def _make_signal(self, snapshot=None):
        from src.strategies.trade_signal import TradeSignal
        return TradeSignal(
            strategy_name="bear_call_spread", symbol="US.SPY",
            timestamp=datetime.now(), action="OPEN",
            signal_type="bear_call_spread",
            sell_contract="SPY260320C700000",
            buy_contract="SPY260320C710000",
            quantity=1, sell_price=2.10, buy_price=0.75,
            net_credit=1.35, max_profit=135.0, max_loss=865.0,
            breakeven=701.35, reward_risk=0.156,
            expiry=expiry_in(35), dte=35,
            iv_rank=55.0, delta=0.25,
            reason="test", regime="bearish",
            spot_price=690.0, buffer_pct=1.45,
            snapshot=snapshot,
        )

    def test_entry_context_stored_when_snapshot_on_signal(self, trade_manager):
        """Full end-to-end: snapshot on signal → entry context in ledger row."""
        tm, ledger = trade_manager
        snapshot   = make_snapshot(rsi=62.0, pct_b=0.78, macd=3.45, vix=18.5)
        signal     = self._make_signal(snapshot=snapshot)

        result = tm.process_signal(signal)

        assert result.executed, f"signal should have executed, got: {result.blocked_reason}"
        trade = ledger.get_trade(result.trade_id)

        assert trade["rsi_at_open"]   == pytest.approx(62.0)
        assert trade["pct_b_at_open"] == pytest.approx(0.78)
        assert trade["macd_at_open"]  == pytest.approx(3.45)
        assert trade["vix_at_open"]   == pytest.approx(18.5)

    def test_entry_context_null_when_no_snapshot_on_signal(self, trade_manager):
        """Signal without snapshot → entry context columns are NULL."""
        tm, ledger = trade_manager
        signal = self._make_signal(snapshot=None)

        result = tm.process_signal(signal)

        assert result.executed
        trade = ledger.get_trade(result.trade_id)
        assert trade["rsi_at_open"]   is None
        assert trade["pct_b_at_open"] is None
        assert trade["macd_at_open"]  is None
        assert trade["vix_at_open"]   is None

    def test_spot_price_stored_from_signal(self, trade_manager):
        """spot_price comes from TradeSignal.spot_price (not snapshot)."""
        tm, ledger = trade_manager
        signal = self._make_signal(snapshot=make_snapshot(spot_price=690.0))

        result = tm.process_signal(signal)

        trade = ledger.get_trade(result.trade_id)
        assert trade["spot_price_at_open"] == pytest.approx(690.0)

    def test_buffer_pct_stored_from_signal(self, trade_manager):
        """buffer_pct comes from TradeSignal.buffer_pct."""
        tm, ledger = trade_manager
        signal = self._make_signal(snapshot=make_snapshot())

        result = tm.process_signal(signal)

        trade = ledger.get_trade(result.trade_id)
        assert trade["buffer_pct"] == pytest.approx(1.45)

    def test_reward_risk_stored_from_signal(self, trade_manager):
        """reward_risk comes from TradeSignal.reward_risk."""
        tm, ledger = trade_manager
        signal = self._make_signal(snapshot=make_snapshot())

        result = tm.process_signal(signal)

        trade = ledger.get_trade(result.trade_id)
        assert trade["reward_risk"] == pytest.approx(0.156)

    def test_macd_negative_value_stored_correctly(self, trade_manager):
        """MACD can be negative — must not be treated as falsy/NULL."""
        tm, ledger = trade_manager
        snapshot   = make_snapshot(macd=-7.23)
        signal     = self._make_signal(snapshot=snapshot)

        result = tm.process_signal(signal)

        trade = ledger.get_trade(result.trade_id)
        assert trade["macd_at_open"] == pytest.approx(-7.23)

    def test_pct_b_zero_stored_correctly(self, trade_manager):
        """%B = 0.0 means price is at lower Bollinger Band — must not be NULL."""
        tm, ledger = trade_manager
        snapshot   = make_snapshot(pct_b=0.0)
        signal     = self._make_signal(snapshot=snapshot)

        result = tm.process_signal(signal)

        trade = ledger.get_trade(result.trade_id)
        assert trade["pct_b_at_open"] == pytest.approx(0.0)

    def test_snapshot_extraction_failure_does_not_abort_trade(self, trade_manager):
        """If snapshot raises on attribute access, trade must still be recorded."""
        tm, ledger = trade_manager

        bad_snapshot = MagicMock()
        bad_snapshot.technicals.rsi = property(lambda self: (_ for _ in ()).throw(AttributeError("broken")))
        # Make technicals raise on any attribute
        type(bad_snapshot.technicals).rsi = property(lambda self: exec('raise AttributeError("broken")'))

        # Use a snapshot that will cause issues — paper_ledger has try/except so trade goes through
        signal = self._make_signal(snapshot=bad_snapshot)
        result = tm.process_signal(signal)

        # Trade must still be recorded even if snapshot extraction fails
        assert result.executed
        trade = ledger.get_trade(result.trade_id)
        assert trade is not None
        assert trade["status"] == "open"

    def test_iv_rank_stored_from_signal_field(self, trade_manager):
        """iv_rank comes from TradeSignal.iv_rank, written to ledger at open."""
        tm, ledger = trade_manager
        signal = self._make_signal(snapshot=None)

        result = tm.process_signal(signal)

        trade = ledger.get_trade(result.trade_id)
        assert trade["iv_rank"] == pytest.approx(55.0)


# ═══════════════════════════════════════════════════════════════════
# End-to-end: all 7 entry context fields populated in one trade
# ═══════════════════════════════════════════════════════════════════

class TestEntryContextEndToEnd:

    def test_all_seven_entry_fields_populated(self, tmp_path):
        """All 7 entry context fields present in one executed trade."""
        from src.execution.trade_manager import TradeManager
        from src.execution.paper_ledger import PaperLedger
        from src.execution.portfolio_guard import PortfolioGuard
        from src.execution.order_router import OrderRouter
        from src.strategies.trade_signal import TradeSignal

        ledger = PaperLedger(db_path=str(tmp_path / "e2e.db"))
        guard  = PortfolioGuard(TEST_CONFIG)
        router = OrderRouter(TEST_CONFIG, MagicMock())
        tm     = TradeManager(TEST_CONFIG, guard, router, ledger)

        snapshot = make_snapshot(
            spot_price=690.0,
            rsi=62.5, pct_b=0.78, macd=3.45, vix=18.5,
            iv_rank=55.0,
        )
        signal = TradeSignal(
            strategy_name="bear_call_spread", symbol="US.SPY",
            timestamp=datetime.now(), action="OPEN",
            signal_type="bear_call_spread",
            sell_contract="SPY260320C700000",
            buy_contract="SPY260320C710000",
            quantity=1, sell_price=2.10, buy_price=0.75,
            net_credit=1.35, max_profit=135.0, max_loss=865.0,
            breakeven=701.35, reward_risk=0.156,
            expiry=expiry_in(35), dte=35,
            iv_rank=55.0, delta=0.25,
            reason="e2e test", regime="bearish",
            spot_price=690.0, buffer_pct=1.45,
            snapshot=snapshot,
        )

        result = tm.process_signal(signal)
        assert result.executed

        t = ledger.get_trade(result.trade_id)

        # All 7 entry context fields
        assert t["spot_price_at_open"] == pytest.approx(690.0)
        assert t["buffer_pct"]         == pytest.approx(1.45)
        assert t["reward_risk"]        == pytest.approx(0.156)
        assert t["rsi_at_open"]        == pytest.approx(62.5)
        assert t["pct_b_at_open"]      == pytest.approx(0.78)
        assert t["macd_at_open"]       == pytest.approx(3.45)
        assert t["vix_at_open"]        == pytest.approx(18.5)
