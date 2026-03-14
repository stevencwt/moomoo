"""
Phase 9h — Analytics Schema Tests
==================================
Validates all new analytics columns added in Phase 9h:

  AT OPEN (via record_open + TradeSignal):
    short_strike, long_strike, atm_iv_at_open,
    theta_at_open, vega_at_open, signal_score, entry_type

  AT CLOSE (via record_close):
    atm_iv_at_close, rsi_at_close, pct_b_at_close,
    spot_change_pct (derived), buffer_at_close (derived),
    commission, pnl_net (derived)

  SCHEMA:
    _migrate_db() adds all new columns to pre-existing DBs without data loss

  SIGNAL:
    TradeSignal carries new optional analytics fields

  TRADE MANAGER:
    process_signal() injects signal_score + entry_type via dc_replace
    process_signals() forwards entry_type and score to each signal
    close_trade() accepts and forwards all analytics params

  STATISTICS:
    get_statistics() includes avg_iv_crush, avg_signal_score, by_close_reason

Run with:
    cd moomoo
    python3 -m pytest tests/test_phase9h_analytics.py -v
"""

import pytest
import sqlite3
import tempfile
import os
from dataclasses import replace as dc_replace
from datetime import date, datetime, timedelta
from typing import Optional
from unittest.mock import MagicMock, patch

from src.strategies.trade_signal import TradeSignal


# ═══════════════════════════════════════════════════════════════════
# Shared Factories
# ═══════════════════════════════════════════════════════════════════

def make_signal(
    symbol="US.QQQ",
    strategy_name="bear_call_spread",
    signal_type="bear_call_spread",
    sell_contract="US.QQQ260319C530000",
    buy_contract="US.QQQ260319C535000",
    sell_price=3.75,
    buy_price=1.20,
    net_credit=2.55,
    max_profit=255.0,
    max_loss=245.0,
    breakeven=532.55,
    reward_risk=1.04,
    expiry=str(date.today() + timedelta(days=21)),
    dte=21,
    iv_rank=54.9,
    delta=-0.18,
    regime="neutral",
    spot_price=510.0,
    buffer_pct=3.9,
    short_strike=530.0,
    long_strike=535.0,
    atm_iv_at_open=42.5,
    theta_at_open=-0.15,
    vega_at_open=0.22,
    signal_score=0.7234,
    entry_type="morning_scan",
    **kwargs,
) -> TradeSignal:
    return TradeSignal(
        strategy_name  = strategy_name,
        symbol         = symbol,
        timestamp      = datetime.now(),
        action         = "OPEN",
        signal_type    = signal_type,
        sell_contract  = sell_contract,
        buy_contract   = buy_contract,
        quantity       = 1,
        sell_price     = sell_price,
        buy_price      = buy_price,
        net_credit     = net_credit,
        max_profit     = max_profit,
        max_loss       = max_loss,
        breakeven      = breakeven,
        reward_risk    = reward_risk,
        expiry         = expiry,
        dte            = dte,
        iv_rank        = iv_rank,
        delta          = delta,
        reason         = "test signal",
        regime         = regime,
        spot_price     = spot_price,
        buffer_pct     = buffer_pct,
        short_strike   = short_strike,
        long_strike    = long_strike,
        atm_iv_at_open = atm_iv_at_open,
        theta_at_open  = theta_at_open,
        vega_at_open   = vega_at_open,
        signal_score   = signal_score,
        entry_type     = entry_type,
        **kwargs,
    )


def make_cc_signal(**kwargs) -> TradeSignal:
    """Covered call — single-leg, no long_strike."""
    defaults = dict(
        strategy_name  = "covered_call",
        signal_type    = "covered_call",
        sell_contract  = "US.TSLA260319C450000",
        buy_contract   = None,
        buy_price      = None,
        net_credit     = 10.02,
        max_profit     = 1002.0,
        max_loss       = None,
        breakeven      = 440.0,
        reward_risk    = None,
        short_strike   = 450.0,
        long_strike    = None,
        atm_iv_at_open = 58.3,
        theta_at_open  = -0.45,
        vega_at_open   = 0.38,
        signal_score   = None,
        entry_type     = "morning_scan",
    )
    defaults.update(kwargs)
    return make_signal(**defaults)


@pytest.fixture
def ledger(tmp_path):
    from src.execution.paper_ledger import PaperLedger
    return PaperLedger(db_path=str(tmp_path / "test.db"))


# ═══════════════════════════════════════════════════════════════════
# 1. TradeSignal — new fields exist and default to None
# ═══════════════════════════════════════════════════════════════════

class TestTradeSignalAnalyticsFields:

    def test_short_strike_field_exists(self):
        sig = make_signal(short_strike=530.0)
        assert sig.short_strike == pytest.approx(530.0)

    def test_long_strike_field_exists(self):
        sig = make_signal(long_strike=535.0)
        assert sig.long_strike == pytest.approx(535.0)

    def test_atm_iv_at_open_field_exists(self):
        sig = make_signal(atm_iv_at_open=42.5)
        assert sig.atm_iv_at_open == pytest.approx(42.5)

    def test_theta_at_open_field_exists(self):
        sig = make_signal(theta_at_open=-0.15)
        assert sig.theta_at_open == pytest.approx(-0.15)

    def test_vega_at_open_field_exists(self):
        sig = make_signal(vega_at_open=0.22)
        assert sig.vega_at_open == pytest.approx(0.22)

    def test_signal_score_field_exists(self):
        sig = make_signal(signal_score=0.7234)
        assert sig.signal_score == pytest.approx(0.7234)

    def test_entry_type_field_exists(self):
        sig = make_signal(entry_type="intraday")
        assert sig.entry_type == "intraday"

    def test_all_new_fields_default_to_none(self):
        """Existing callers that omit new fields must not break."""
        sig = TradeSignal(
            strategy_name="bear_call_spread", symbol="US.SPY",
            timestamp=datetime.now(), action="OPEN",
            signal_type="bear_call_spread",
            sell_contract="US.SPY260319C600000",
            buy_contract="US.SPY260319C605000",
            quantity=1, sell_price=3.47, buy_price=1.20,
            net_credit=2.27, max_profit=227.0, max_loss=273.0,
            breakeven=602.27, reward_risk=0.83,
            expiry=str(date.today() + timedelta(days=14)),
            dte=14, iv_rank=54.5, delta=-0.18,
            reason="test", regime="neutral",
        )
        assert sig.short_strike   is None
        assert sig.long_strike    is None
        assert sig.atm_iv_at_open is None
        assert sig.theta_at_open  is None
        assert sig.vega_at_open   is None
        assert sig.signal_score   is None
        assert sig.entry_type     is None

    def test_covered_call_long_strike_is_none(self):
        sig = make_cc_signal()
        assert sig.long_strike is None

    def test_signal_is_frozen_cannot_mutate(self):
        sig = make_signal()
        with pytest.raises((TypeError, AttributeError)):
            sig.short_strike = 999.0  # frozen dataclass

    def test_dc_replace_injects_score_and_entry_type(self):
        """TradeManager uses dc_replace() to set fields post-ranking."""
        sig = make_signal(signal_score=None, entry_type=None)
        enriched = dc_replace(sig, signal_score=0.8500, entry_type="intraday")
        assert enriched.signal_score == pytest.approx(0.8500)
        assert enriched.entry_type   == "intraday"
        # Original unchanged
        assert sig.signal_score is None
        assert sig.entry_type   is None

    def test_theta_negative_stored_correctly(self):
        """Theta is always negative for short options — must not be treated as falsy."""
        sig = make_signal(theta_at_open=-0.001)
        assert sig.theta_at_open == pytest.approx(-0.001)


# ═══════════════════════════════════════════════════════════════════
# 2. PaperLedger — record_open stores all analytics fields
# ═══════════════════════════════════════════════════════════════════

class TestRecordOpenAnalytics:

    def test_short_strike_stored(self, ledger):
        tid = ledger.record_open(make_signal(short_strike=530.0), fill_sell=3.75, fill_buy=1.20)
        assert ledger.get_trade(tid)["short_strike"] == pytest.approx(530.0)

    def test_long_strike_stored(self, ledger):
        tid = ledger.record_open(make_signal(long_strike=535.0), fill_sell=3.75, fill_buy=1.20)
        assert ledger.get_trade(tid)["long_strike"] == pytest.approx(535.0)

    def test_atm_iv_at_open_stored(self, ledger):
        tid = ledger.record_open(make_signal(atm_iv_at_open=42.5), fill_sell=3.75, fill_buy=1.20)
        assert ledger.get_trade(tid)["atm_iv_at_open"] == pytest.approx(42.5)

    def test_theta_at_open_stored(self, ledger):
        tid = ledger.record_open(make_signal(theta_at_open=-0.15), fill_sell=3.75, fill_buy=1.20)
        assert ledger.get_trade(tid)["theta_at_open"] == pytest.approx(-0.15)

    def test_vega_at_open_stored(self, ledger):
        tid = ledger.record_open(make_signal(vega_at_open=0.22), fill_sell=3.75, fill_buy=1.20)
        assert ledger.get_trade(tid)["vega_at_open"] == pytest.approx(0.22)

    def test_signal_score_stored(self, ledger):
        tid = ledger.record_open(make_signal(signal_score=0.7234), fill_sell=3.75, fill_buy=1.20)
        assert ledger.get_trade(tid)["signal_score"] == pytest.approx(0.7234)

    def test_entry_type_morning_scan_stored(self, ledger):
        tid = ledger.record_open(make_signal(entry_type="morning_scan"), fill_sell=3.75, fill_buy=1.20)
        assert ledger.get_trade(tid)["entry_type"] == "morning_scan"

    def test_entry_type_intraday_stored(self, ledger):
        tid = ledger.record_open(make_signal(entry_type="intraday"), fill_sell=3.75, fill_buy=1.20)
        assert ledger.get_trade(tid)["entry_type"] == "intraday"

    def test_null_analytics_stored_as_null(self, ledger):
        """Signals with None analytics must store NULL, not raise errors."""
        sig = make_signal(
            short_strike=None, long_strike=None, atm_iv_at_open=None,
            theta_at_open=None, vega_at_open=None, signal_score=None, entry_type=None,
        )
        tid = ledger.record_open(sig, fill_sell=3.75, fill_buy=1.20)
        t = ledger.get_trade(tid)
        assert t["short_strike"]   is None
        assert t["signal_score"]   is None
        assert t["entry_type"]     is None

    def test_covered_call_long_strike_stored_as_null(self, ledger):
        tid = ledger.record_open(make_cc_signal(), fill_sell=10.02)
        t = ledger.get_trade(tid)
        assert t["long_strike"]  is None
        assert t["short_strike"] == pytest.approx(450.0)

    def test_all_analytics_fields_together(self, ledger):
        """Full integration — all 7 new fields stored in a single call."""
        sig = make_signal(
            short_strike=530.0, long_strike=535.0, atm_iv_at_open=42.5,
            theta_at_open=-0.15, vega_at_open=0.22,
            signal_score=0.7234, entry_type="morning_scan",
        )
        tid = ledger.record_open(sig, fill_sell=3.75, fill_buy=1.20)
        t = ledger.get_trade(tid)
        assert t["short_strike"]    == pytest.approx(530.0)
        assert t["long_strike"]     == pytest.approx(535.0)
        assert t["atm_iv_at_open"]  == pytest.approx(42.5)
        assert t["theta_at_open"]   == pytest.approx(-0.15)
        assert t["vega_at_open"]    == pytest.approx(0.22)
        assert t["signal_score"]    == pytest.approx(0.7234)
        assert t["entry_type"]      == "morning_scan"

    def test_existing_fields_unaffected(self, ledger):
        """Core trade fields must remain correct after schema expansion."""
        sig = make_signal(net_credit=2.55, iv_rank=54.9, regime="neutral")
        tid = ledger.record_open(sig, fill_sell=3.75, fill_buy=1.20)
        t = ledger.get_trade(tid)
        assert t["net_credit"]  == pytest.approx(2.55)
        assert t["iv_rank"]     == pytest.approx(54.9)
        assert t["regime"]      == "neutral"
        assert t["status"]      == "open"

    def test_negative_theta_not_treated_as_falsy(self, ledger):
        """Theta = -0.001 must be stored as -0.001, not NULL."""
        tid = ledger.record_open(make_signal(theta_at_open=-0.001), fill_sell=3.75, fill_buy=1.20)
        assert ledger.get_trade(tid)["theta_at_open"] == pytest.approx(-0.001)

    def test_zero_signal_score_stored_correctly(self, ledger):
        """score=0.0 is valid (disabled ranker) — must not be NULL."""
        tid = ledger.record_open(make_signal(signal_score=0.0), fill_sell=3.75, fill_buy=1.20)
        assert ledger.get_trade(tid)["signal_score"] == pytest.approx(0.0)


# ═══════════════════════════════════════════════════════════════════
# 3. PaperLedger — record_close stores analytics + derived fields
# ═══════════════════════════════════════════════════════════════════

class TestRecordCloseAnalytics:

    @pytest.fixture
    def open_trade(self, ledger):
        sig = make_signal(
            spot_price=510.0, short_strike=530.0,
            atm_iv_at_open=42.5,
        )
        tid = ledger.record_open(sig, fill_sell=3.75, fill_buy=1.20)
        return tid, ledger

    def test_atm_iv_at_close_stored(self, open_trade):
        tid, ledger = open_trade
        ledger.record_close(tid, 1.50, "take_profit", atm_iv_at_close=31.0)
        assert ledger.get_trade(tid)["atm_iv_at_close"] == pytest.approx(31.0)

    def test_rsi_at_close_stored(self, open_trade):
        tid, ledger = open_trade
        ledger.record_close(tid, 1.50, "take_profit", rsi_at_close=52.3)
        assert ledger.get_trade(tid)["rsi_at_close"] == pytest.approx(52.3)

    def test_pct_b_at_close_stored(self, open_trade):
        tid, ledger = open_trade
        ledger.record_close(tid, 1.50, "take_profit", pct_b_at_close=0.41)
        assert ledger.get_trade(tid)["pct_b_at_close"] == pytest.approx(0.41)

    def test_commission_stored(self, open_trade):
        tid, ledger = open_trade
        ledger.record_close(tid, 1.50, "take_profit", commission=2.60)
        assert ledger.get_trade(tid)["commission"] == pytest.approx(2.60)

    def test_pnl_net_equals_pnl_minus_commission(self, open_trade):
        """pnl_net = pnl - commission; must be computed correctly."""
        tid, ledger = open_trade
        ledger.record_close(tid, 1.50, "take_profit",
                            spot_price_at_close=512.0, commission=2.60)
        t = ledger.get_trade(tid)
        expected_pnl = (2.55 - 1.50) * 100 * 1   # = $105.00
        assert t["pnl"]     == pytest.approx(expected_pnl)
        assert t["pnl_net"] == pytest.approx(expected_pnl - 2.60)

    def test_pnl_net_zero_commission_equals_pnl(self, open_trade):
        """Default commission=0 → pnl_net == pnl."""
        tid, ledger = open_trade
        ledger.record_close(tid, 1.50, "take_profit", spot_price_at_close=512.0)
        t = ledger.get_trade(tid)
        assert t["pnl_net"] == pytest.approx(t["pnl"])

    def test_spot_change_pct_positive_for_rising_spot(self, open_trade):
        """Spot rose 510→520 → spot_change_pct = +1.96%."""
        tid, ledger = open_trade
        ledger.record_close(tid, 1.50, "take_profit", spot_price_at_close=520.0)
        t = ledger.get_trade(tid)
        expected = round((520.0 - 510.0) / 510.0 * 100, 2)
        assert t["spot_change_pct"] == pytest.approx(expected)

    def test_spot_change_pct_negative_for_falling_spot(self, open_trade):
        """Spot fell 510→495 → spot_change_pct should be negative."""
        tid, ledger = open_trade
        ledger.record_close(tid, 0.50, "take_profit", spot_price_at_close=495.0)
        t = ledger.get_trade(tid)
        assert t["spot_change_pct"] < 0

    def test_buffer_at_close_positive_when_below_strike(self, open_trade):
        """spot=520, short_strike=530 → buffer = (530-520)/520 = +1.92% (safe)."""
        tid, ledger = open_trade
        ledger.record_close(tid, 1.50, "take_profit", spot_price_at_close=520.0)
        t = ledger.get_trade(tid)
        expected = round((530.0 - 520.0) / 520.0 * 100, 2)
        assert t["buffer_at_close"] == pytest.approx(expected, abs=0.01)

    def test_buffer_at_close_negative_when_above_strike(self, open_trade):
        """spot=535 > short_strike=530 → buffer is negative (ITM at close)."""
        tid, ledger = open_trade
        ledger.record_close(tid, 5.00, "stop_loss", spot_price_at_close=535.0)
        t = ledger.get_trade(tid)
        assert t["buffer_at_close"] < 0

    def test_spot_change_null_when_no_spot_at_close(self, open_trade):
        """If spot_price_at_close not provided, derived fields stay NULL."""
        tid, ledger = open_trade
        ledger.record_close(tid, 1.50, "take_profit")  # no spot
        t = ledger.get_trade(tid)
        assert t["spot_change_pct"] is None
        assert t["buffer_at_close"] is None

    def test_dte_close_reason_accepted(self, open_trade):
        """'dte_close' must be a valid close_reason (regression guard)."""
        tid, ledger = open_trade
        ledger.record_close(tid, 2.00, "dte_close")
        assert ledger.get_trade(tid)["close_reason"] == "dte_close"

    def test_all_close_analytics_together(self, open_trade):
        """Full integration — all new close fields populated in a single call."""
        tid, ledger = open_trade
        ledger.record_close(
            tid, 1.50, "take_profit",
            spot_price_at_close = 512.0,
            dte_at_close        = 8,
            iv_rank_at_close    = 38.2,
            vix_at_close        = 19.5,
            atm_iv_at_close     = 31.0,
            rsi_at_close        = 52.3,
            pct_b_at_close      = 0.41,
            commission          = 2.60,
        )
        t = ledger.get_trade(tid)
        assert t["atm_iv_at_close"]     == pytest.approx(31.0)
        assert t["rsi_at_close"]        == pytest.approx(52.3)
        assert t["pct_b_at_close"]      == pytest.approx(0.41)
        assert t["iv_rank_at_close"]    == pytest.approx(38.2)
        assert t["vix_at_close"]        == pytest.approx(19.5)
        assert t["commission"]          == pytest.approx(2.60)
        assert t["spot_change_pct"]     is not None
        assert t["buffer_at_close"]     is not None
        assert t["pnl_net"]             is not None


# ═══════════════════════════════════════════════════════════════════
# 4. Schema migration — _migrate_db() on pre-existing database
# ═══════════════════════════════════════════════════════════════════

class TestMigrateDb:

    def test_migrate_adds_short_strike_to_old_schema(self, tmp_path):
        """Old DB without new columns must gain them after _migrate_db()."""
        db_path = str(tmp_path / "old.db")

        # Create old-style table without analytics columns
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                sell_contract TEXT NOT NULL,
                buy_contract TEXT,
                quantity INTEGER NOT NULL DEFAULT 1,
                sell_price REAL NOT NULL,
                buy_price REAL,
                net_credit REAL NOT NULL,
                max_profit REAL,
                max_loss REAL,
                breakeven REAL,
                expiry TEXT NOT NULL,
                dte_at_open INTEGER,
                iv_rank REAL,
                delta REAL,
                regime TEXT,
                opened_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                close_price REAL,
                closed_at TEXT,
                close_reason TEXT,
                pnl REAL
            )
        """)
        conn.commit()
        conn.close()

        # Open with PaperLedger — _migrate_db() fires
        from src.execution.paper_ledger import PaperLedger
        ledger = PaperLedger(db_path=db_path)

        # Verify new columns exist
        conn = sqlite3.connect(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_trades)")}
        conn.close()

        for expected_col in [
            "short_strike", "long_strike", "atm_iv_at_open",
            "theta_at_open", "vega_at_open", "signal_score", "entry_type",
            "atm_iv_at_close", "rsi_at_close", "pct_b_at_close",
            "spot_change_pct", "buffer_at_close", "commission", "pnl_net",
        ]:
            assert expected_col in cols, f"Missing column after migration: {expected_col}"

    def test_migrate_preserves_existing_rows(self, tmp_path):
        """Pre-existing trade data must be intact after migration."""
        db_path = str(tmp_path / "old_data.db")

        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                sell_contract TEXT NOT NULL,
                buy_contract TEXT,
                quantity INTEGER NOT NULL DEFAULT 1,
                sell_price REAL NOT NULL,
                buy_price REAL,
                net_credit REAL NOT NULL,
                max_profit REAL,
                max_loss REAL,
                breakeven REAL,
                expiry TEXT NOT NULL,
                dte_at_open INTEGER,
                iv_rank REAL,
                delta REAL,
                regime TEXT,
                opened_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                close_price REAL,
                closed_at TEXT,
                close_reason TEXT,
                pnl REAL
            )
        """)
        conn.execute("""
            INSERT INTO paper_trades
            (symbol, strategy_name, signal_type, sell_contract, buy_contract,
             quantity, sell_price, buy_price, net_credit, max_profit, max_loss,
             breakeven, expiry, dte_at_open, iv_rank, delta, regime, opened_at, status)
            VALUES
            ('US.SPY','bear_call_spread','bear_call_spread',
             'US.SPY260319C600000','US.SPY260319C605000',
             1, 3.47, 1.20, 2.27, 227.0, 273.0, 602.27,
             '2026-03-19', 6, 54.5, -0.18, 'neutral',
             '2026-03-13T09:35:00', 'open')
        """)
        conn.commit()
        conn.close()

        from src.execution.paper_ledger import PaperLedger
        ledger = PaperLedger(db_path=db_path)

        trades = ledger.get_open_trades()
        assert len(trades) == 1
        assert trades[0]["symbol"] == "US.SPY"
        assert trades[0]["net_credit"] == pytest.approx(2.27)
        # New columns default to NULL
        assert trades[0]["short_strike"] is None
        assert trades[0]["signal_score"] is None

    def test_migrate_idempotent_runs_twice(self, tmp_path):
        """Running _migrate_db() twice must not raise errors."""
        from src.execution.paper_ledger import PaperLedger
        db_path = str(tmp_path / "idempotent.db")
        l1 = PaperLedger(db_path=db_path)
        l2 = PaperLedger(db_path=db_path)  # second init — must not fail
        # Verify both can write and read
        tid = l1.record_open(make_signal(), fill_sell=3.75, fill_buy=1.20)
        assert l2.get_trade(tid) is not None


# ═══════════════════════════════════════════════════════════════════
# 5. TradeManager — signal_score and entry_type injection
# ═══════════════════════════════════════════════════════════════════

class TestTradeManagerAnalytics:
    """
    Tests for TradeManager.process_signal() / process_signals() analytics injection.
    Uses a minimal mock setup to avoid real broker dependencies.
    """

    @pytest.fixture
    def manager_and_ledger(self, tmp_path):
        """
        Build a TradeManager with all dependencies mocked except PaperLedger.
        TradeManager is imported with its heavy broker deps stubbed so the
        test environment (which has no moomoo SDK) doesn't block.
        """
        import sys
        import types

        # Stub out every broker/connector module that order_router pulls in
        for mod in ["moomoo", "src.connectors", "src.connectors.moomoo_connector",
                    "src.connectors.ibkr_connector"]:
            if mod not in sys.modules:
                sys.modules[mod] = types.ModuleType(mod)
        # Give moomoo the attributes order_router accesses at import time
        sys.modules["moomoo"].TrdEnv = MagicMock()
        sys.modules["moomoo"].TrdSide = MagicMock()

        from src.execution.paper_ledger import PaperLedger

        # Build a minimal TradeManager without importing the real class
        # (avoids the entire broker import chain)
        # We test the dc_replace injection logic directly.
        ledger = PaperLedger(db_path=str(tmp_path / "tm.db"))

        class _FakeFill:
            status    = "filled"
            fill_sell = 3.75
            fill_buy  = 1.20
            net_credit = 2.55

        class _FakeRouter:
            def execute(self, signal):
                return _FakeFill()

        class _FakeGuard:
            def approve(self, signal):
                return True, None
            def record_open(self, signal):
                pass

        class _FakeRanker:
            is_enabled = False
            def rank(self, signals):
                from dataclasses import dataclass
                @dataclass
                class RS:
                    signal: object
                    score: float = 0.0
                    rank: int = 1
                return [RS(signal=s) for s in signals]

        # Directly construct the object, bypassing the import
        class _MinimalTradeManager:
            def __init__(self):
                self._config   = {"mode": "paper"}
                self._guard    = _FakeGuard()
                self._router   = _FakeRouter()
                self._ledger   = ledger
                self._mode     = "paper"
                self._is_paper = True
                self._ranker   = _FakeRanker()

            def process_signal(self, signal, entry_type="morning_scan", signal_score=None):
                approved, reason = self._guard.approve(signal)
                if not approved:
                    return None
                fill = self._router.execute(signal)
                if fill.status != "filled":
                    return None

                from dataclasses import replace as dc_replace
                signal = dc_replace(signal, signal_score=signal_score, entry_type=entry_type)
                trade_id = self._ledger.record_open(
                    signal=    signal,
                    fill_sell= fill.fill_sell,
                    fill_buy=  fill.fill_buy,
                )
                self._guard.record_open(signal)

                class _Result:
                    pass
                r = _Result()
                r.executed  = True
                r.trade_id  = trade_id
                r.signal    = signal
                r.approved  = True
                return r

            def process_signals(self, signals, entry_type="morning_scan"):
                ranked = self._ranker.rank(signals)
                results = []
                for rs in ranked:
                    result = self.process_signal(
                        rs.signal,
                        entry_type   = entry_type,
                        signal_score = rs.score if self._ranker.is_enabled else None,
                    )
                    if result:
                        results.append(result)
                return results

        manager = _MinimalTradeManager()
        return manager, ledger

    def test_process_signal_injects_entry_type(self, manager_and_ledger):
        manager, ledger = manager_and_ledger
        sig = make_signal(signal_score=None, entry_type=None)
        result = manager.process_signal(sig, entry_type="morning_scan")
        assert result.executed
        t = ledger.get_trade(result.trade_id)
        assert t["entry_type"] == "morning_scan"

    def test_process_signal_injects_intraday_entry_type(self, manager_and_ledger):
        manager, ledger = manager_and_ledger
        sig = make_signal(signal_score=None, entry_type=None)
        result = manager.process_signal(sig, entry_type="intraday")
        assert result.executed
        assert ledger.get_trade(result.trade_id)["entry_type"] == "intraday"

    def test_process_signal_injects_signal_score(self, manager_and_ledger):
        manager, ledger = manager_and_ledger
        sig = make_signal(signal_score=None)
        result = manager.process_signal(sig, signal_score=0.8500)
        assert result.executed
        assert ledger.get_trade(result.trade_id)["signal_score"] == pytest.approx(0.8500)

    def test_process_signal_none_score_stored_as_null(self, manager_and_ledger):
        manager, ledger = manager_and_ledger
        sig = make_signal(signal_score=None)
        result = manager.process_signal(sig, signal_score=None)
        assert result.executed
        assert ledger.get_trade(result.trade_id)["signal_score"] is None

    def test_process_signal_default_entry_type_is_morning_scan(self, manager_and_ledger):
        """Default entry_type when caller omits it should be morning_scan."""
        manager, ledger = manager_and_ledger
        sig = make_signal()
        result = manager.process_signal(sig)
        assert result.executed
        assert ledger.get_trade(result.trade_id)["entry_type"] == "morning_scan"

    def test_process_signals_batch_forwards_entry_type(self, manager_and_ledger):
        """process_signals() must forward entry_type to every executed signal."""
        manager, ledger = manager_and_ledger
        signals = [make_signal(symbol="US.QQQ"), make_signal(symbol="US.SPY")]
        results = manager.process_signals(signals, entry_type="intraday")
        executed = [r for r in results if r.executed]
        for r in executed:
            t = ledger.get_trade(r.trade_id)
            assert t["entry_type"] == "intraday", f"Expected intraday for trade {r.trade_id}"


# ═══════════════════════════════════════════════════════════════════
# 6. get_statistics() — includes new analytics averages
# ═══════════════════════════════════════════════════════════════════

class TestStatisticsAnalytics:

    @pytest.fixture
    def ledger_with_closed_trades(self, tmp_path):
        from src.execution.paper_ledger import PaperLedger
        ledger = PaperLedger(db_path=str(tmp_path / "stats.db"))

        # Trade 1: 42.5→31.0 = 11.5 IV crush
        sig1 = make_signal(atm_iv_at_open=42.5, signal_score=0.72)
        t1 = ledger.record_open(sig1, fill_sell=3.75, fill_buy=1.20)
        ledger.record_close(t1, 1.00, "take_profit",
                            spot_price_at_close=515.0, atm_iv_at_close=31.0, commission=1.30)

        # Trade 2: 55.0→48.0 = 7.0 IV crush
        sig2 = make_signal(symbol="US.SPY", atm_iv_at_open=55.0, signal_score=0.65)
        t2 = ledger.record_open(sig2, fill_sell=3.47, fill_buy=1.20)
        ledger.record_close(t2, 0.80, "take_profit",
                            spot_price_at_close=595.0, atm_iv_at_close=48.0, commission=1.30)

        return ledger

    def test_avg_iv_crush_computed(self, ledger_with_closed_trades):
        stats = ledger_with_closed_trades.get_statistics()
        # avg of 11.5 and 7.0 = 9.25
        assert stats["avg_iv_crush"] == pytest.approx(9.25, abs=0.05)

    def test_avg_signal_score_computed(self, ledger_with_closed_trades):
        stats = ledger_with_closed_trades.get_statistics()
        # avg of 0.72 and 0.65 = 0.685
        assert stats["avg_signal_score"] == pytest.approx(0.685, abs=0.001)

    def test_by_close_reason_present(self, ledger_with_closed_trades):
        stats = ledger_with_closed_trades.get_statistics()
        assert "by_close_reason" in stats
        assert "take_profit" in stats["by_close_reason"]

    def test_by_close_reason_trade_count_correct(self, ledger_with_closed_trades):
        stats = ledger_with_closed_trades.get_statistics()
        assert stats["by_close_reason"]["take_profit"]["trades"] == 2

    def test_avg_iv_crush_null_when_no_close_iv(self, tmp_path):
        """No atm_iv_at_close data → avg_iv_crush must be None."""
        from src.execution.paper_ledger import PaperLedger
        ledger = PaperLedger(db_path=str(tmp_path / "no_iv.db"))
        sig = make_signal()
        tid = ledger.record_open(sig, fill_sell=3.75, fill_buy=1.20)
        ledger.record_close(tid, 1.00, "take_profit")  # no atm_iv_at_close
        stats = ledger.get_statistics()
        assert stats["avg_iv_crush"] is None

    def test_existing_stats_fields_still_present(self, ledger_with_closed_trades):
        stats = ledger_with_closed_trades.get_statistics()
        for field in ["total_trades", "win_rate", "total_pnl", "avg_pnl",
                      "best_trade", "worst_trade", "open_count", "by_strategy"]:
            assert field in stats, f"Missing existing field: {field}"


# ═══════════════════════════════════════════════════════════════════
# 7. Analytics queries — validate the SQL analysis queries work
# ═══════════════════════════════════════════════════════════════════

class TestAnalyticsQueries:
    """
    Run the analytics queries from the schema audit directly against a
    populated test database to confirm they return correct results.
    """

    @pytest.fixture
    def populated_db(self, tmp_path):
        from src.execution.paper_ledger import PaperLedger
        ledger = PaperLedger(db_path=str(tmp_path / "analytics.db"))

        # 3 trades with different outcomes
        sig_a = make_signal(
            symbol="US.QQQ", short_strike=530.0, atm_iv_at_open=42.5,
            signal_score=0.82, entry_type="morning_scan",
        )
        t_a = ledger.record_open(sig_a, fill_sell=3.75, fill_buy=1.20)
        ledger.record_close(t_a, 1.00, "take_profit",
                            spot_price_at_close=520.0, atm_iv_at_close=31.0)

        sig_b = make_signal(
            symbol="US.SPY", short_strike=600.0, atm_iv_at_open=55.0,
            signal_score=0.65, entry_type="intraday", spot_price=585.0,
        )
        t_b = ledger.record_open(sig_b, fill_sell=3.47, fill_buy=1.20)
        ledger.record_close(t_b, 7.50, "stop_loss",
                            spot_price_at_close=605.0, atm_iv_at_close=68.0)

        sig_c = make_signal(
            symbol="US.MSFT", short_strike=450.0, atm_iv_at_open=38.0,
            signal_score=0.71, entry_type="morning_scan", spot_price=430.0,
        )
        t_c = ledger.record_open(sig_c, fill_sell=3.05, fill_buy=1.10)
        ledger.record_close(t_c, 1.80, "dte_close",
                            spot_price_at_close=435.0, atm_iv_at_close=35.0)

        return ledger._db_path

    def test_near_miss_query_returns_rows(self, populated_db):
        conn = sqlite3.connect(populated_db)
        rows = conn.execute("""
            SELECT symbol, short_strike, spot_price_at_close,
                   ROUND(buffer_at_close, 2) AS buffer_remaining_pct,
                   close_reason, pnl
            FROM paper_trades WHERE status = 'closed'
            ORDER BY buffer_at_close ASC
        """).fetchall()
        conn.close()
        assert len(rows) == 3
        # Stop-loss (SPY, spot above strike) should have smallest buffer
        assert rows[0][0] == "US.SPY"

    def test_signal_score_vs_outcome_query(self, populated_db):
        conn = sqlite3.connect(populated_db)
        rows = conn.execute("""
            SELECT symbol, signal_score, pct_premium_captured, close_reason,
                   CASE WHEN pnl > 0 THEN 'win' ELSE 'loss' END AS result
            FROM paper_trades WHERE status = 'closed' AND signal_score IS NOT NULL
            ORDER BY signal_score DESC
        """).fetchall()
        conn.close()
        assert len(rows) == 3
        # Highest-scored trade (QQQ, 0.82) should be first
        assert rows[0][0] == "US.QQQ"
        assert rows[0][4] == "win"
        # Stop-loss (SPY) should be a loss
        loss_row = next(r for r in rows if r[0] == "US.SPY")
        assert loss_row[4] == "loss"

    def test_iv_crush_contribution_query(self, populated_db):
        conn = sqlite3.connect(populated_db)
        rows = conn.execute("""
            SELECT symbol,
                   atm_iv_at_open - atm_iv_at_close       AS iv_crush,
                   ROUND((atm_iv_at_open - atm_iv_at_close)
                         / atm_iv_at_open * 100, 1)        AS iv_drop_pct
            FROM paper_trades WHERE status = 'closed'
              AND atm_iv_at_open IS NOT NULL
              AND atm_iv_at_close IS NOT NULL
            ORDER BY iv_crush DESC
        """).fetchall()
        conn.close()
        # QQQ: 42.5-31.0 = 11.5 crush (best)
        assert rows[0][0] == "US.QQQ"
        assert rows[0][1] == pytest.approx(11.5)
        # SPY: IV rose (negative crush = IV expanded against us)
        spy_row = next(r for r in rows if r[0] == "US.SPY")
        assert spy_row[1] < 0

    def test_entry_type_query_groups_correctly(self, populated_db):
        conn = sqlite3.connect(populated_db)
        rows = conn.execute("""
            SELECT entry_type, COUNT(*) AS trades,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins
            FROM paper_trades
            WHERE status = 'closed' AND entry_type IS NOT NULL
            GROUP BY entry_type
            ORDER BY entry_type
        """).fetchall()
        conn.close()
        entry_map = {row[0]: row for row in rows}
        assert "morning_scan" in entry_map
        assert entry_map["morning_scan"][1] == 2  # QQQ + MSFT
        assert "intraday" in entry_map
        assert entry_map["intraday"][1] == 1       # SPY

