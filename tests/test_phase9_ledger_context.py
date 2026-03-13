"""
Phase 9 — PaperLedger Entry & Exit Context Tests
=================================================
Tests for the entry/exit condition fields added to PaperLedger in Phase 1
of the dashboard work.

New entry fields captured at record_open():
  spot_price_at_open, buffer_pct, reward_risk,
  rsi_at_open, pct_b_at_open, macd_at_open, vix_at_open

New exit fields captured at record_close():
  days_held, dte_at_close, spot_price_at_close,
  iv_rank_at_close, vix_at_close, pct_premium_captured

New methods:
  get_closed_trades(), get_all_trades()

New get_statistics() fields:
  avg_days_held, avg_dte_at_close, avg_pct_captured, by_close_reason

Database migration:
  _migrate_db() adds new columns to pre-existing schema without data loss

Run with:
    cd moomoo
    python3 -m pytest tests/test_phase9_ledger_context.py -v
"""

import pytest
import sqlite3
import tempfile
import os
from datetime import date, datetime, timedelta

from src.strategies.trade_signal import TradeSignal


# ── Fixtures & Factories ──────────────────────────────────────────


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
    spot_price=420.00,
    buffer_pct=1.19,
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
        spot_price=     spot_price,
        buffer_pct=     buffer_pct,
    )


def make_spread_signal(**kwargs) -> TradeSignal:
    defaults = dict(
        symbol="US.SPY",
        strategy_name="bear_call_spread",
        signal_type="bear_call_spread",
        sell_contract="US.SPY260320C700000",
        buy_contract="US.SPY260320C710000",
        sell_price=2.10,
        buy_price=0.75,
        net_credit=1.35,
        max_profit=135.0,
        max_loss=865.0,
        breakeven=701.35,
        reward_risk=0.156,
        spot_price=685.45,
        buffer_pct=2.1,
    )
    defaults.update(kwargs)
    return make_signal(**defaults)


def make_bull_put_signal(**kwargs) -> TradeSignal:
    defaults = dict(
        symbol="US.QQQ",
        strategy_name="bull_put_spread",
        signal_type="bull_put_spread",
        sell_contract="US.QQQ260320P00590000",
        buy_contract="US.QQQ260320P00580000",
        sell_price=1.80,
        buy_price=0.65,
        net_credit=1.15,
        max_profit=115.0,
        max_loss=885.0,
        breakeven=588.85,
        reward_risk=0.130,
        spot_price=607.0,
        buffer_pct=2.8,
    )
    defaults.update(kwargs)
    return make_signal(**defaults)


class FakeTechnicals:
    """Mimics MarketSnapshot.technicals"""
    def __init__(self, rsi=47.2, pct_b=0.33, macd=-0.40):
        self.rsi   = rsi
        self.pct_b = pct_b
        self.macd  = macd


class FakeSnapshot:
    """Mimics MarketSnapshot — provides entry context to record_open()"""
    def __init__(self, rsi=47.2, pct_b=0.33, macd=-0.40, vix=21.5):
        self.technicals = FakeTechnicals(rsi=rsi, pct_b=pct_b, macd=macd)
        self.vix        = vix


# ═══════════════════════════════════════════════════════════════════
# Entry Context Tests (record_open with snapshot)
# ═══════════════════════════════════════════════════════════════════

class TestEntryContext:

    @pytest.fixture
    def ledger(self, tmp_path):
        from src.execution.paper_ledger import PaperLedger
        return PaperLedger(db_path=str(tmp_path / "test.db"))

    def test_entry_rsi_stored_from_snapshot(self, ledger):
        sig = make_signal()
        tid = ledger.record_open(sig, fill_sell=4.10, snapshot=FakeSnapshot(rsi=52.3))
        trade = ledger.get_trade(tid)
        assert trade["rsi_at_open"] == pytest.approx(52.3)

    def test_entry_pct_b_stored_from_snapshot(self, ledger):
        sig = make_signal()
        tid = ledger.record_open(sig, fill_sell=4.10, snapshot=FakeSnapshot(pct_b=0.67))
        trade = ledger.get_trade(tid)
        assert trade["pct_b_at_open"] == pytest.approx(0.67)

    def test_entry_macd_stored_from_snapshot(self, ledger):
        sig = make_signal()
        tid = ledger.record_open(sig, fill_sell=4.10, snapshot=FakeSnapshot(macd=-3.14))
        trade = ledger.get_trade(tid)
        assert trade["macd_at_open"] == pytest.approx(-3.14)

    def test_entry_vix_stored_from_snapshot(self, ledger):
        sig = make_signal()
        tid = ledger.record_open(sig, fill_sell=4.10, snapshot=FakeSnapshot(vix=24.7))
        trade = ledger.get_trade(tid)
        assert trade["vix_at_open"] == pytest.approx(24.7)

    def test_entry_spot_price_stored_from_signal(self, ledger):
        sig = make_signal(spot_price=401.79)
        tid = ledger.record_open(sig, fill_sell=4.10)
        trade = ledger.get_trade(tid)
        assert trade["spot_price_at_open"] == pytest.approx(401.79)

    def test_entry_buffer_pct_stored_from_signal(self, ledger):
        sig = make_spread_signal(buffer_pct=2.1)
        tid = ledger.record_open(sig, fill_sell=2.10, fill_buy=0.75)
        trade = ledger.get_trade(tid)
        assert trade["buffer_pct"] == pytest.approx(2.1)

    def test_entry_reward_risk_stored_from_signal(self, ledger):
        sig = make_spread_signal(reward_risk=0.156)
        tid = ledger.record_open(sig, fill_sell=2.10, fill_buy=0.75)
        trade = ledger.get_trade(tid)
        assert trade["reward_risk"] == pytest.approx(0.156)

    def test_all_entry_fields_populated_together(self, ledger):
        snap = FakeSnapshot(rsi=48.0, pct_b=0.41, macd=-0.29, vix=21.8)
        sig  = make_spread_signal(spot_price=685.45, buffer_pct=2.1, reward_risk=0.156)
        tid  = ledger.record_open(sig, fill_sell=2.10, fill_buy=0.75, snapshot=snap)
        t    = ledger.get_trade(tid)
        assert t["rsi_at_open"]        == pytest.approx(48.0)
        assert t["pct_b_at_open"]      == pytest.approx(0.41)
        assert t["macd_at_open"]       == pytest.approx(-0.29)
        assert t["vix_at_open"]        == pytest.approx(21.8)
        assert t["spot_price_at_open"] == pytest.approx(685.45)
        assert t["buffer_pct"]         == pytest.approx(2.1)
        assert t["reward_risk"]        == pytest.approx(0.156)

    def test_existing_fields_unaffected_by_snapshot(self, ledger):
        """Core trade fields must be correct regardless of snapshot."""
        snap = FakeSnapshot()
        sig  = make_spread_signal(net_credit=1.35, iv_rank=59.0, regime="neutral")
        tid  = ledger.record_open(sig, fill_sell=2.10, fill_buy=0.75, snapshot=snap)
        t    = ledger.get_trade(tid)
        assert t["net_credit"]    == pytest.approx(1.35)
        assert t["iv_rank"]       == pytest.approx(59.0)
        assert t["regime"]        == "neutral"
        assert t["status"]        == "open"

    def test_negative_macd_stored_correctly(self, ledger):
        """MACD is often negative — must not be treated as falsy."""
        snap = FakeSnapshot(macd=-7.23)
        tid  = ledger.record_open(make_signal(), fill_sell=4.10, snapshot=snap)
        assert ledger.get_trade(tid)["macd_at_open"] == pytest.approx(-7.23)

    def test_zero_pct_b_stored_correctly(self, ledger):
        """pct_b=0.0 means price is at the lower Bollinger band — must not be NULL."""
        snap = FakeSnapshot(pct_b=0.0)
        tid  = ledger.record_open(make_signal(), fill_sell=4.10, snapshot=snap)
        assert ledger.get_trade(tid)["pct_b_at_open"] == pytest.approx(0.0)


# ═══════════════════════════════════════════════════════════════════
# Backward Compatibility (no snapshot)
# ═══════════════════════════════════════════════════════════════════

class TestBackwardCompatibility:

    @pytest.fixture
    def ledger(self, tmp_path):
        from src.execution.paper_ledger import PaperLedger
        return PaperLedger(db_path=str(tmp_path / "test.db"))

    def test_record_open_without_snapshot_succeeds(self, ledger):
        """Omitting snapshot must not raise — existing call sites unaffected."""
        sig = make_signal()
        tid = ledger.record_open(sig, fill_sell=4.10)
        assert tid >= 1

    def test_context_fields_are_null_without_snapshot(self, ledger):
        tid = ledger.record_open(make_signal(), fill_sell=4.10)
        t   = ledger.get_trade(tid)
        assert t["rsi_at_open"]   is None
        assert t["pct_b_at_open"] is None
        assert t["macd_at_open"]  is None
        assert t["vix_at_open"]   is None

    def test_signal_fields_still_stored_without_snapshot(self, ledger):
        """spot_price, buffer_pct, reward_risk come from signal — always stored."""
        sig = make_spread_signal(spot_price=685.45, buffer_pct=2.1, reward_risk=0.156)
        tid = ledger.record_open(sig, fill_sell=2.10, fill_buy=0.75)
        t   = ledger.get_trade(tid)
        assert t["spot_price_at_open"] == pytest.approx(685.45)
        assert t["buffer_pct"]         == pytest.approx(2.1)
        assert t["reward_risk"]        == pytest.approx(0.156)

    def test_record_close_without_exit_context_succeeds(self, ledger):
        """Omitting exit kwargs must not raise."""
        tid = ledger.record_open(make_signal(), fill_sell=4.10)
        pnl = ledger.record_close(tid, close_price=0.0,
                                   close_reason="expired_worthless")
        assert pnl == pytest.approx(410.0)

    def test_exit_fields_null_without_context(self, ledger):
        tid = ledger.record_open(make_signal(), fill_sell=4.10)
        ledger.record_close(tid, close_price=0.0, close_reason="expired_worthless")
        t = ledger.get_trade(tid)
        assert t["dte_at_close"]         is None
        assert t["spot_price_at_close"]  is None
        assert t["iv_rank_at_close"]     is None
        assert t["vix_at_close"]         is None


# ═══════════════════════════════════════════════════════════════════
# Exit Context Tests (record_close)
# ═══════════════════════════════════════════════════════════════════

class TestExitContext:

    @pytest.fixture
    def ledger(self, tmp_path):
        from src.execution.paper_ledger import PaperLedger
        return PaperLedger(db_path=str(tmp_path / "test.db"))

    @pytest.fixture
    def open_trade_id(self, ledger):
        sig = make_spread_signal(net_credit=1.35)
        return ledger.record_open(sig, fill_sell=2.10, fill_buy=0.75)

    def test_spot_price_at_close_stored(self, ledger, open_trade_id):
        ledger.record_close(open_trade_id, close_price=0.65,
                            close_reason="take_profit",
                            spot_price_at_close=692.10)
        t = ledger.get_trade(open_trade_id)
        assert t["spot_price_at_close"] == pytest.approx(692.10)

    def test_dte_at_close_stored(self, ledger, open_trade_id):
        ledger.record_close(open_trade_id, close_price=0.65,
                            close_reason="take_profit",
                            dte_at_close=18)
        assert ledger.get_trade(open_trade_id)["dte_at_close"] == 18

    def test_iv_rank_at_close_stored(self, ledger, open_trade_id):
        ledger.record_close(open_trade_id, close_price=0.65,
                            close_reason="take_profit",
                            iv_rank_at_close=41.0)
        assert ledger.get_trade(open_trade_id)["iv_rank_at_close"] == pytest.approx(41.0)

    def test_vix_at_close_stored(self, ledger, open_trade_id):
        ledger.record_close(open_trade_id, close_price=0.65,
                            close_reason="take_profit",
                            vix_at_close=18.2)
        assert ledger.get_trade(open_trade_id)["vix_at_close"] == pytest.approx(18.2)

    def test_days_held_is_zero_for_same_day_close(self, ledger, open_trade_id):
        ledger.record_close(open_trade_id, close_price=0.0,
                            close_reason="expired_worthless")
        assert ledger.get_trade(open_trade_id)["days_held"] == 0

    def test_days_held_computed_from_open_to_close(self, ledger, tmp_path):
        from src.execution.paper_ledger import PaperLedger
        from unittest.mock import patch
        ledger2 = PaperLedger(db_path=str(tmp_path / "daystest.db"))
        sig = make_signal()
        # Open 10 days ago
        open_time  = datetime.now() - timedelta(days=10)
        close_time = datetime.now()
        with patch("src.execution.paper_ledger.datetime") as mock_dt:
            mock_dt.now.return_value = open_time
            mock_dt.fromisoformat = datetime.fromisoformat
            tid = ledger2.record_open(sig, fill_sell=4.10)
        pnl = ledger2.record_close(tid, close_price=0.0,
                                    close_reason="expired_worthless",
                                    closed_at=close_time)
        assert ledger2.get_trade(tid)["days_held"] == 10

    def test_pct_premium_captured_50_pct_take_profit(self, ledger, open_trade_id):
        """Close at half the credit → 50% captured."""
        # credit=1.35, close=0.675 → (1.35-0.675)/1.35 * 100 = 50.0%
        ledger.record_close(open_trade_id, close_price=0.675,
                            close_reason="take_profit")
        t = ledger.get_trade(open_trade_id)
        assert t["pct_premium_captured"] == pytest.approx(50.0, abs=0.2)

    def test_pct_premium_captured_full_profit_expired(self, ledger):
        """Expired worthless → 100% captured."""
        sig = make_signal(net_credit=4.10)
        tid = ledger.record_open(sig, fill_sell=4.10)
        ledger.record_close(tid, close_price=0.0, close_reason="expired_worthless")
        assert ledger.get_trade(tid)["pct_premium_captured"] == pytest.approx(100.0)

    def test_pct_premium_captured_negative_on_loss(self, ledger):
        """Stop loss where debit > credit → negative % captured."""
        sig = make_spread_signal(net_credit=1.35)
        tid = ledger.record_open(sig, fill_sell=2.10, fill_buy=0.75)
        # Close at 3× credit = stop loss
        ledger.record_close(tid, close_price=4.05, close_reason="stop_loss")
        t = ledger.get_trade(tid)
        assert t["pct_premium_captured"] < 0

    def test_dte_close_reason_accepted(self, ledger, open_trade_id):
        """'dte_close' is new valid reason alongside original four."""
        pnl = ledger.record_close(open_trade_id, close_price=0.50,
                                   close_reason="dte_close")
        t = ledger.get_trade(open_trade_id)
        assert t["close_reason"] == "dte_close"
        assert t["status"]       == "closed"

    def test_all_exit_fields_populated_together(self, ledger, open_trade_id):
        ledger.record_close(
            open_trade_id, close_price=0.65, close_reason="take_profit",
            spot_price_at_close=692.10, dte_at_close=18,
            iv_rank_at_close=41.0, vix_at_close=18.2,
        )
        t = ledger.get_trade(open_trade_id)
        assert t["spot_price_at_close"] == pytest.approx(692.10)
        assert t["dte_at_close"]        == 18
        assert t["iv_rank_at_close"]    == pytest.approx(41.0)
        assert t["vix_at_close"]        == pytest.approx(18.2)
        assert t["pct_premium_captured"] == pytest.approx(51.9, abs=0.2)


# ═══════════════════════════════════════════════════════════════════
# New Query Methods
# ═══════════════════════════════════════════════════════════════════

class TestNewQueryMethods:

    @pytest.fixture
    def ledger(self, tmp_path):
        from src.execution.paper_ledger import PaperLedger
        return PaperLedger(db_path=str(tmp_path / "test.db"))

    def _open_and_close(self, ledger, signal, close_reason="expired_worthless",
                        close_price=0.0, fill_buy=None, fill_sell=None):
        fs = fill_sell or signal.sell_price
        fb = fill_buy or signal.buy_price
        tid = ledger.record_open(signal, fill_sell=fs, fill_buy=fb)
        ledger.record_close(tid, close_price=close_price, close_reason=close_reason)
        return tid

    def test_get_closed_trades_returns_only_closed(self, ledger):
        s1 = make_signal(symbol="US.TSLA", strategy_name="covered_call")
        s2 = make_spread_signal(symbol="US.SPY")
        self._open_and_close(ledger, s1, fill_sell=4.10)
        # Leave s2 open
        ledger.record_open(s2, fill_sell=2.10, fill_buy=0.75)
        closed = ledger.get_closed_trades()
        assert len(closed) == 1
        assert closed[0]["symbol"] == "US.TSLA"

    def test_get_closed_trades_ordered_most_recent_first(self, ledger):
        for sym in ["US.AAPL", "US.MSFT", "US.GOOGL"]:
            s = make_signal(symbol=sym, strategy_name="covered_call")
            self._open_and_close(ledger, s, fill_sell=4.10)
        closed = ledger.get_closed_trades()
        assert len(closed) == 3
        # Most recent closed first (GOOGL opened+closed last)
        assert closed[0]["symbol"] == "US.GOOGL"

    def test_get_all_trades_returns_open_and_closed(self, ledger):
        s1 = make_signal(symbol="US.TSLA", strategy_name="covered_call")
        s2 = make_spread_signal(symbol="US.SPY")
        self._open_and_close(ledger, s1, fill_sell=4.10)
        ledger.record_open(s2, fill_sell=2.10, fill_buy=0.75)
        all_trades = ledger.get_all_trades()
        assert len(all_trades) == 2
        statuses = {t["status"] for t in all_trades}
        assert "closed" in statuses or "expired" in statuses
        assert "open" in statuses

    def test_get_open_trades_includes_all_new_fields(self, ledger):
        snap = FakeSnapshot(rsi=48.0, pct_b=0.40, macd=-0.29, vix=21.8)
        sig  = make_spread_signal(spot_price=685.45, buffer_pct=2.1, reward_risk=0.156)
        ledger.record_open(sig, fill_sell=2.10, fill_buy=0.75, snapshot=snap)
        open_trades = ledger.get_open_trades()
        assert len(open_trades) == 1
        t = open_trades[0]
        assert t["rsi_at_open"]        == pytest.approx(48.0)
        assert t["spot_price_at_open"] == pytest.approx(685.45)
        assert t["buffer_pct"]         == pytest.approx(2.1)


# ═══════════════════════════════════════════════════════════════════
# New get_statistics() Fields
# ═══════════════════════════════════════════════════════════════════

class TestNewStatisticsFields:

    @pytest.fixture
    def ledger(self, tmp_path):
        from src.execution.paper_ledger import PaperLedger
        return PaperLedger(db_path=str(tmp_path / "test.db"))

    def _trade(self, ledger, symbol, strategy, net_credit, close_price,
               close_reason, dte_at_close=None, fill_buy=None, fill_sell=None,
               buy_contract=None):
        sig = make_signal(
            symbol=symbol, strategy_name=strategy, signal_type=strategy,
            net_credit=net_credit, sell_price=net_credit + (fill_buy or 0),
            buy_price=fill_buy, buy_contract=buy_contract,
        )
        fs = fill_sell or sig.sell_price
        fb = fill_buy
        tid = ledger.record_open(sig, fill_sell=fs, fill_buy=fb)
        ledger.record_close(tid, close_price=close_price,
                            close_reason=close_reason,
                            dte_at_close=dte_at_close)
        return tid

    def test_avg_days_held_present_in_statistics(self, ledger):
        self._trade(ledger, "US.TSLA", "covered_call",
                    net_credit=4.10, close_price=0.0,
                    close_reason="expired_worthless")
        stats = ledger.get_statistics()
        assert "avg_days_held" in stats

    def test_avg_dte_at_close_computed(self, ledger):
        self._trade(ledger, "US.SPY", "bear_call_spread",
                    net_credit=1.35, close_price=0.65,
                    close_reason="take_profit", dte_at_close=18,
                    fill_buy=0.75, buy_contract="US.SPY260320C710000")
        self._trade(ledger, "US.QQQ", "bear_call_spread",
                    net_credit=1.35, close_price=0.65,
                    close_reason="take_profit", dte_at_close=22,
                    fill_buy=0.75, buy_contract="US.QQQ260320C640000")
        stats = ledger.get_statistics()
        assert stats["avg_dte_at_close"] == pytest.approx(20.0)

    def test_avg_pct_captured_only_on_winners(self, ledger):
        """avg_pct_captured averages only winning trades."""
        # Winner: 100% captured (expired worthless)
        self._trade(ledger, "US.TSLA", "covered_call",
                    net_credit=4.10, close_price=0.0,
                    close_reason="expired_worthless")
        # Loser: stop loss — should NOT appear in avg_pct_captured average
        self._trade(ledger, "US.AAPL", "covered_call",
                    net_credit=4.10, close_price=12.30,
                    close_reason="stop_loss")
        stats = ledger.get_statistics()
        # Average should be 100% (only the winner)
        assert stats["avg_pct_captured"] == pytest.approx(100.0)

    def test_by_close_reason_present(self, ledger):
        stats = ledger.get_statistics()
        assert "by_close_reason" in stats

    def test_by_close_reason_counts_correctly(self, ledger):
        self._trade(ledger, "US.TSLA", "covered_call",
                    net_credit=4.10, close_price=0.0,
                    close_reason="expired_worthless")
        self._trade(ledger, "US.AAPL", "covered_call",
                    net_credit=4.10, close_price=0.0,
                    close_reason="expired_worthless")
        self._trade(ledger, "US.SPY", "bear_call_spread",
                    net_credit=1.35, close_price=0.65,
                    close_reason="take_profit",
                    fill_buy=0.75, buy_contract="US.SPY260320C710000")
        stats = ledger.get_statistics()
        assert stats["by_close_reason"]["expired_worthless"]["trades"] == 2
        assert stats["by_close_reason"]["take_profit"]["trades"]       == 1

    def test_by_close_reason_includes_dte_close(self, ledger):
        self._trade(ledger, "US.SPY", "bear_call_spread",
                    net_credit=1.35, close_price=0.50,
                    close_reason="dte_close", dte_at_close=21,
                    fill_buy=0.75, buy_contract="US.SPY260320C710000")
        stats = ledger.get_statistics()
        assert "dte_close" in stats["by_close_reason"]

    def test_statistics_zero_trades_new_fields_safe(self, ledger):
        """No trades — new fields must not raise, return None or 0."""
        stats = ledger.get_statistics()
        assert stats["avg_days_held"]    is None or stats["avg_days_held"]    == 0
        assert stats["avg_dte_at_close"] is None or stats["avg_dte_at_close"] == 0
        assert stats["avg_pct_captured"] is None or stats["avg_pct_captured"] == 0
        assert stats["by_close_reason"]  == {}


# ═══════════════════════════════════════════════════════════════════
# Database Migration Tests
# ═══════════════════════════════════════════════════════════════════

class TestDatabaseMigration:

    OLD_SCHEMA = """
        CREATE TABLE paper_trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol          TEXT    NOT NULL,
            strategy_name   TEXT    NOT NULL,
            signal_type     TEXT    NOT NULL,
            sell_contract   TEXT    NOT NULL,
            buy_contract    TEXT,
            quantity        INTEGER NOT NULL DEFAULT 1,
            sell_price      REAL    NOT NULL,
            buy_price       REAL,
            net_credit      REAL    NOT NULL,
            max_profit      REAL,
            max_loss        REAL,
            breakeven       REAL,
            expiry          TEXT    NOT NULL,
            dte_at_open     INTEGER,
            iv_rank         REAL,
            delta           REAL,
            regime          TEXT,
            opened_at       TEXT    NOT NULL,
            close_price     REAL,
            closed_at       TEXT,
            close_reason    TEXT,
            pnl             REAL,
            status          TEXT    NOT NULL DEFAULT 'open'
        )
    """

    NEW_COLUMNS = [
        "spot_price_at_open", "buffer_pct", "reward_risk",
        "rsi_at_open", "pct_b_at_open", "macd_at_open", "vix_at_open",
        "days_held", "dte_at_close", "spot_price_at_close",
        "iv_rank_at_close", "vix_at_close", "pct_premium_captured",
    ]

    def _create_old_db(self, path):
        conn = sqlite3.connect(path)
        conn.execute(self.OLD_SCHEMA)
        conn.commit()
        conn.close()

    def _get_columns(self, path):
        conn = sqlite3.connect(path)
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(paper_trades)"
        ).fetchall()]
        conn.close()
        return cols

    def test_all_new_columns_added_to_existing_db(self, tmp_path):
        from src.execution.paper_ledger import PaperLedger
        db = str(tmp_path / "old.db")
        self._create_old_db(db)
        PaperLedger(db)  # triggers migration
        cols = self._get_columns(db)
        for col in self.NEW_COLUMNS:
            assert col in cols, f"Missing column after migration: {col}"

    def test_migration_count_is_13_new_columns(self, tmp_path):
        from src.execution.paper_ledger import PaperLedger
        db = str(tmp_path / "old.db")
        self._create_old_db(db)
        cols_before = self._get_columns(db)
        PaperLedger(db)
        cols_after  = self._get_columns(db)
        assert len(cols_after) - len(cols_before) == 13

    def test_migration_is_idempotent(self, tmp_path):
        """Running migration twice must not raise."""
        from src.execution.paper_ledger import PaperLedger
        db = str(tmp_path / "old.db")
        self._create_old_db(db)
        PaperLedger(db)
        PaperLedger(db)   # second startup — must not raise

    def test_existing_rows_preserved_after_migration(self, tmp_path):
        """Data in existing rows must survive the migration."""
        from src.execution.paper_ledger import PaperLedger
        db = str(tmp_path / "old.db")
        self._create_old_db(db)
        # Insert a row in the old schema
        conn = sqlite3.connect(db)
        conn.execute("""
            INSERT INTO paper_trades
                (symbol, strategy_name, signal_type, sell_contract,
                 quantity, sell_price, net_credit, expiry, opened_at, status)
            VALUES ('US.TSLA','covered_call','covered_call',
                    'US.TSLA260320C425000', 1, 4.10, 4.10,
                    '2026-04-02', '2026-03-01T09:35:00', 'open')
        """)
        conn.commit(); conn.close()
        # Migrate
        PaperLedger(db)
        # Row must still be there
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT symbol, net_credit FROM paper_trades WHERE id=1"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "US.TSLA"
        assert row[1] == pytest.approx(4.10)

    def test_new_columns_default_to_null_on_old_rows(self, tmp_path):
        """Old rows must have NULL in new context columns — not garbage."""
        from src.execution.paper_ledger import PaperLedger
        db = str(tmp_path / "old.db")
        self._create_old_db(db)
        conn = sqlite3.connect(db)
        conn.execute("""
            INSERT INTO paper_trades
                (symbol, strategy_name, signal_type, sell_contract,
                 quantity, sell_price, net_credit, expiry, opened_at, status)
            VALUES ('US.TSLA','covered_call','covered_call',
                    'US.TSLA260320C425000', 1, 4.10, 4.10,
                    '2026-04-02', '2026-03-01T09:35:00', 'open')
        """)
        conn.commit(); conn.close()
        PaperLedger(db)
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT rsi_at_open, pct_premium_captured FROM paper_trades WHERE id=1"
        ).fetchone()
        conn.close()
        assert row[0] is None
        assert row[1] is None
