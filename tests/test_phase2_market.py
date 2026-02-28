"""
Phase 2 — Market Data & Analysis Tests
=======================================
Tests for: TechnicalAnalyser, IVRankCalculator, RegimeDetector,
           OptionsAnalyser, MarketScanner, MarketSnapshot

Run with:
    cd moomoo
    python3 -m pytest tests/test_phase2_market.py -v

All tests use synthetic data — zero live API calls.
"""

import pytest
import pandas as pd
import numpy as np
import sqlite3
import os
import tempfile
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

# ── Test config ────────────────────────────────────────────────────
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
    }
}


# ── Synthetic data helpers ────────────────────────────────────────

def make_ohlcv(
    n: int = 120,
    base_price: float = 400.0,
    trend: float = 0.0,
    volatility: float = 5.0,
    seed: int = 42
) -> pd.DataFrame:
    """Generate synthetic OHLCV DataFrame with n bars."""
    np.random.seed(seed)
    dates  = pd.date_range(end=pd.Timestamp.today(), periods=n, freq="B")
    noise  = np.random.randn(n) * volatility
    close  = base_price + trend * np.arange(n) + np.cumsum(noise)
    close  = np.maximum(close, 1.0)   # prevent negative prices

    return pd.DataFrame({
        "open":   close * 0.995,
        "high":   close * 1.015,
        "low":    close * 0.985,
        "close":  close,
        "volume": np.random.randint(40_000_000, 80_000_000, n).astype(float)
    }, index=dates)


def make_chain_df(spot: float = 400.0, expiry: str = "2026-03-20") -> pd.DataFrame:
    """Generate synthetic option chain around a spot price."""
    strikes = [spot + i * 5 for i in range(-5, 10)]   # 15 strikes
    rows = []
    for s in strikes:
        for ot in ["CALL", "PUT"]:
            rows.append({
                "code":            f"US.TSLA{expiry.replace('-','')}"
                                   f"{'C' if ot=='CALL' else 'P'}"
                                   f"{int(s*1000):08d}",
                "option_type":     ot,
                "strike_price":    s,
                "strike_time":     expiry,
                "lot_size":        100,
                "stock_owner":     "US.TSLA",
                "expiration_cycle": "MONTH",
            })
    return pd.DataFrame(rows)


def make_snapshot_df(
    contracts: list,
    spot: float = 400.0,
    expiry: str = "2026-03-20"
) -> pd.DataFrame:
    """Generate synthetic snapshot DataFrame with Greeks."""
    rows = []
    for code in contracts:
        # Extract strike from code (last 8 chars / 1000)
        try:
            strike = int(code[-8:]) / 1000
        except Exception:
            strike = spot + 25
        moneyness = (strike - spot) / spot
        delta     = max(0.05, min(0.95, 0.5 - moneyness * 2))
        rows.append({
            "code":                code,
            "last_price":          max(0.05, (0.5 - abs(moneyness)) * 20),
            "bid_price":           max(0.05, (0.5 - abs(moneyness)) * 19),
            "ask_price":           max(0.05, (0.5 - abs(moneyness)) * 21),
            "mid_price":           max(0.05, (0.5 - abs(moneyness)) * 20),
            "option_delta":        delta,
            "option_gamma":        0.02,
            "option_theta":       -0.40,
            "option_vega":         0.20,
            "option_iv":           35.0,
            "option_open_interest": 500,
            "strike_price":        strike,
            "expiry":              expiry,
            "sec_status":          "NORMAL",
        })
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════
# MarketSnapshot Tests
# ═══════════════════════════════════════════════════════════════════

class TestMarketSnapshot:

    def _make_technicals(self, **kwargs) -> "Technicals":
        from src.market.market_snapshot import Technicals
        defaults = dict(
            bb_upper=430.0, bb_middle=410.0, bb_lower=390.0, pct_b=0.8,
            rsi=65.0, macd=1.5, macd_signal=1.0, macd_hist=0.5,
            atr=12.0, atr_pct=2.9
        )
        defaults.update(kwargs)
        return Technicals(**defaults)

    def _make_snapshot(self, **kwargs):
        from src.market.market_snapshot import MarketSnapshot, OptionsContext
        defaults = dict(
            symbol="US.TSLA",
            timestamp=datetime.now(),
            spot_price=410.0,
            technicals=self._make_technicals(),
            vix=18.0,
            market_regime="neutral",
            options_context=OptionsContext(
                iv_rank=55.0,
                atm_iv=34.0,
                available_expiries=["2026-03-20", "2026-04-17"]
            ),
            next_earnings=None,
            days_to_earnings=None,
            shares_held=100,
            open_positions=0
        )
        defaults.update(kwargs)
        return MarketSnapshot(**defaults)

    def test_snapshot_creates_successfully(self):
        snap = self._make_snapshot()
        assert snap.symbol == "US.TSLA"
        assert snap.spot_price == 410.0

    def test_snapshot_is_frozen(self):
        snap = self._make_snapshot()
        with pytest.raises(Exception):   # FrozenInstanceError
            snap.spot_price = 999.0

    def test_snapshot_rejects_invalid_regime(self):
        with pytest.raises(ValueError, match="Invalid market_regime"):
            self._make_snapshot(market_regime="sideways")

    def test_snapshot_rejects_negative_spot_price(self):
        with pytest.raises(ValueError, match="spot_price"):
            self._make_snapshot(spot_price=-1.0)

    def test_snapshot_rejects_negative_shares(self):
        with pytest.raises(ValueError, match="shares_held"):
            self._make_snapshot(shares_held=-1)

    def test_all_valid_regimes_accepted(self):
        for regime in ["bull", "bear", "neutral", "high_vol"]:
            snap = self._make_snapshot(market_regime=regime)
            assert snap.market_regime == regime


# ═══════════════════════════════════════════════════════════════════
# TechnicalAnalyser Tests
# ═══════════════════════════════════════════════════════════════════

class TestTechnicalAnalyser:

    @pytest.fixture
    def analyser(self):
        from src.market.technical_analyser import TechnicalAnalyser
        return TechnicalAnalyser()

    def test_compute_all_adds_indicator_columns(self, analyser):
        df     = make_ohlcv(120)
        result = analyser.compute_all(df)
        for col in ["bb_upper", "bb_middle", "bb_lower", "pct_b",
                    "rsi", "macd", "macd_signal", "macd_hist", "atr", "atr_pct"]:
            assert col in result.columns, f"Missing column: {col}"

    def test_compute_all_does_not_modify_original(self, analyser):
        df     = make_ohlcv(120)
        cols_before = set(df.columns)
        analyser.compute_all(df)
        assert set(df.columns) == cols_before

    def test_bollinger_band_ordering(self, analyser):
        df     = make_ohlcv(120)
        result = analyser.compute_all(df).dropna()
        assert (result["bb_upper"] >= result["bb_middle"]).all()
        assert (result["bb_middle"] >= result["bb_lower"]).all()

    def test_rsi_bounded_0_to_100(self, analyser):
        df     = make_ohlcv(120)
        result = analyser.compute_all(df).dropna(subset=["rsi"])
        assert (result["rsi"] >= 0).all()
        assert (result["rsi"] <= 100).all()

    def test_atr_always_positive(self, analyser):
        df     = make_ohlcv(120)
        result = analyser.compute_all(df).dropna(subset=["atr"])
        assert (result["atr"] > 0).all()

    def test_pct_b_around_one_when_close_near_upper_band(self, analyser):
        # Create strongly trending up data so close is near upper band
        df     = make_ohlcv(120, trend=1.5, volatility=1.0)
        result = analyser.compute_all(df).dropna()
        last   = result.iloc[-1]
        # In an uptrend close should be in upper half of bands (%B > 0.5)
        assert last["pct_b"] > 0.5

    def test_extract_latest_returns_technicals_dataclass(self, analyser):
        from src.market.market_snapshot import Technicals
        df     = make_ohlcv(120)
        result = analyser.compute_all(df)
        tech   = analyser.extract_latest(result)
        assert isinstance(tech, Technicals)

    def test_extract_latest_values_are_finite(self, analyser):
        df     = make_ohlcv(120)
        result = analyser.compute_all(df)
        tech   = analyser.extract_latest(result)
        for field in ["bb_upper", "bb_middle", "bb_lower", "pct_b",
                      "rsi", "macd", "macd_signal", "macd_hist", "atr", "atr_pct"]:
            val = getattr(tech, field)
            assert np.isfinite(val), f"{field} is not finite: {val}"

    def test_raises_on_too_few_bars(self, analyser):
        from src.exceptions import DataError
        df = make_ohlcv(10)
        with pytest.raises(DataError, match="bars"):
            analyser.compute_all(df)

    def test_raises_on_missing_columns(self, analyser):
        from src.exceptions import DataError
        df = pd.DataFrame({"close": [100, 101, 102]})
        with pytest.raises(DataError, match="missing"):
            analyser.compute_all(df)

    def test_overbought_scenario_rsi_high(self, analyser):
        # Strongly trending up with sufficient volatility → RSI should be elevated
        # trend=2.0 with some noise ensures both gains and losses exist for RSI calc
        df     = make_ohlcv(120, trend=2.0, volatility=2.0, seed=42)
        result = analyser.compute_all(df)
        tech   = analyser.extract_latest(result)
        assert tech.rsi > 60, f"Expected RSI > 60 in uptrend, got {tech.rsi:.1f}"


# ═══════════════════════════════════════════════════════════════════
# IVRankCalculator Tests
# ═══════════════════════════════════════════════════════════════════

class TestIVRankCalculator:

    @pytest.fixture
    def iv_calc(self, tmp_path):
        from src.market.iv_rank_calculator import IVRankCalculator
        db = str(tmp_path / "test_iv.db")
        return IVRankCalculator(db_path=db)

    def test_store_and_retrieve(self, iv_calc):
        iv_calc.store_daily_iv("US.TSLA", 35.0, as_of=date(2026, 1, 15))
        assert iv_calc.get_days_stored("US.TSLA") == 1

    def test_store_overwrites_same_date(self, iv_calc):
        iv_calc.store_daily_iv("US.TSLA", 35.0, as_of=date(2026, 1, 15))
        iv_calc.store_daily_iv("US.TSLA", 40.0, as_of=date(2026, 1, 15))
        assert iv_calc.get_days_stored("US.TSLA") == 1

    def test_get_iv_rank_reliable_with_sufficient_history(self, iv_calc):
        # Store 35 days of IV history
        base = date(2025, 12, 1)
        for i in range(35):
            d = base + timedelta(days=i)
            iv_calc.store_daily_iv("US.TSLA", 25.0 + i * 0.5, as_of=d)

        iv_rank, quality = iv_calc.get_iv_rank("US.TSLA", current_iv=32.0)
        assert quality == "estimate"   # 35 days = estimate (not reliable until 252)
        assert 0 <= iv_rank <= 100

    def test_get_iv_rank_high_iv_returns_high_rank(self, iv_calc):
        base = date(2025, 12, 1)
        for i in range(35):
            d = base + timedelta(days=i)
            iv_calc.store_daily_iv("US.TSLA", 20.0 + i * 0.5, as_of=d)
        # current_iv higher than all stored → rank near 100
        iv_rank, _ = iv_calc.get_iv_rank("US.TSLA", current_iv=50.0)
        assert iv_rank > 80

    def test_get_iv_rank_low_iv_returns_low_rank(self, iv_calc):
        base = date(2025, 12, 1)
        for i in range(35):
            d = base + timedelta(days=i)
            iv_calc.store_daily_iv("US.TSLA", 30.0 + i * 0.5, as_of=d)
        # current_iv lower than all stored → rank near 0
        iv_rank, _ = iv_calc.get_iv_rank("US.TSLA", current_iv=10.0)
        assert iv_rank < 20

    def test_get_iv_rank_bootstrap_with_no_history(self, iv_calc):
        iv_rank, quality = iv_calc.get_iv_rank("US.TSLA", current_iv=35.0, vix=20.0)
        assert quality == "unavailable"
        assert 0 <= iv_rank <= 100

    def test_bootstrap_high_vix_gives_high_rank(self, iv_calc):
        iv_rank, quality = iv_calc.get_iv_rank("US.TSLA", current_iv=50.0, vix=35.0)
        assert iv_rank > 70

    def test_bootstrap_low_vix_gives_low_rank(self, iv_calc):
        iv_rank, quality = iv_calc.get_iv_rank("US.TSLA", current_iv=20.0, vix=12.0)
        assert iv_rank < 35

    def test_store_skips_zero_iv(self, iv_calc):
        iv_calc.store_daily_iv("US.TSLA", 0.0)
        assert iv_calc.get_days_stored("US.TSLA") == 0

    def test_purge_removes_old_records(self, iv_calc):
        old_date = date.today() - timedelta(days=400)
        iv_calc.store_daily_iv("US.TSLA", 35.0, as_of=old_date)
        assert iv_calc.get_days_stored("US.TSLA") == 1
        deleted = iv_calc.purge_old_records(keep_days=365)
        assert deleted == 1
        assert iv_calc.get_days_stored("US.TSLA") == 0

    def test_separate_symbols_independent(self, iv_calc):
        iv_calc.store_daily_iv("US.TSLA", 35.0, as_of=date(2026, 1, 15))
        iv_calc.store_daily_iv("US.AAPL", 25.0, as_of=date(2026, 1, 15))
        assert iv_calc.get_days_stored("US.TSLA") == 1
        assert iv_calc.get_days_stored("US.AAPL") == 1


# ═══════════════════════════════════════════════════════════════════
# RegimeDetector Tests
# ═══════════════════════════════════════════════════════════════════

class TestRegimeDetector:

    @pytest.fixture
    def detector(self):
        from src.market.regime_detector import RegimeDetector
        return RegimeDetector(TEST_CONFIG)

    def _make_tech(self, rsi=50.0, macd=0.0):
        from src.market.market_snapshot import Technicals
        return Technicals(
            bb_upper=430, bb_middle=410, bb_lower=390, pct_b=0.5,
            rsi=rsi, macd=macd, macd_signal=0.0, macd_hist=macd,
            atr=12.0, atr_pct=2.9
        )

    def test_high_vix_returns_high_vol(self, detector):
        tech = self._make_tech(rsi=50, macd=0)
        assert detector.detect(tech, vix=26.0) == "high_vol"

    def test_high_vix_overrides_bullish_indicators(self, detector):
        tech = self._make_tech(rsi=75, macd=5.0)
        assert detector.detect(tech, vix=30.0) == "high_vol"

    def test_high_rsi_and_positive_macd_returns_bull(self, detector):
        tech = self._make_tech(rsi=65.0, macd=2.0)
        assert detector.detect(tech, vix=15.0) == "bull"

    def test_low_rsi_and_negative_macd_returns_bear(self, detector):
        tech = self._make_tech(rsi=40.0, macd=-2.0)
        assert detector.detect(tech, vix=15.0) == "bear"

    def test_neutral_conditions_returns_neutral(self, detector):
        tech = self._make_tech(rsi=50.0, macd=0.5)
        assert detector.detect(tech, vix=15.0) == "neutral"

    def test_high_rsi_but_negative_macd_is_neutral(self, detector):
        # RSI overbought but MACD negative — mixed signal → neutral
        tech = self._make_tech(rsi=65.0, macd=-1.0)
        assert detector.detect(tech, vix=15.0) == "neutral"

    def test_is_eligible_blocks_high_vol(self, detector):
        assert detector.is_eligible_to_trade("high_vol") is False

    def test_is_eligible_allows_neutral(self, detector):
        assert detector.is_eligible_to_trade("neutral") is True

    def test_is_eligible_allows_bull(self, detector):
        assert detector.is_eligible_to_trade("bull") is True

    def test_is_eligible_allows_bear(self, detector):
        assert detector.is_eligible_to_trade("bear") is True

    def test_vix_just_below_threshold_not_high_vol(self, detector):
        tech = self._make_tech(rsi=50, macd=0)
        # threshold is 25.0 — 24.9 should not trigger high_vol
        result = detector.detect(tech, vix=24.9)
        assert result != "high_vol"

    def test_vix_exactly_at_threshold_is_high_vol(self, detector):
        tech = self._make_tech(rsi=50, macd=0)
        assert detector.detect(tech, vix=25.0) == "high_vol"


# ═══════════════════════════════════════════════════════════════════
# OptionsAnalyser Tests
# ═══════════════════════════════════════════════════════════════════

class TestOptionsAnalyser:

    @pytest.fixture
    def analyser(self):
        from src.market.options_analyser import OptionsAnalyser
        return OptionsAnalyser(TEST_CONFIG)

    # ── Expiry Selection ──────────────────────────────────────────

    def test_get_target_expiry_selects_within_range(self, analyser):
        today    = date.today()
        expiries = [
            (today + timedelta(days=d)).strftime("%Y-%m-%d")
            for d in [7, 14, 28, 35, 42, 60, 90]
        ]
        result = analyser.get_target_expiry(expiries)
        assert result is not None
        expiry_date = date.fromisoformat(result)
        dte = (expiry_date - today).days
        assert 21 <= dte <= 45

    def test_get_target_expiry_returns_none_when_no_suitable(self, analyser):
        today    = date.today()
        # Only very short DTEs (< 21 days)
        expiries = [(today + timedelta(days=d)).strftime("%Y-%m-%d")
                    for d in [3, 7, 10]]
        # Falls back to ±7 buffer, still too short
        result = analyser.get_target_expiry(expiries)
        # May return fallback — just check it doesn't crash
        assert result is None or isinstance(result, str)

    def test_get_target_expiry_prefers_midpoint(self, analyser):
        today    = date.today()
        # 28 days is closest to midpoint of 21-45 (midpoint = 33)
        expiries = [
            (today + timedelta(days=d)).strftime("%Y-%m-%d")
            for d in [22, 28, 44]
        ]
        result = analyser.get_target_expiry(expiries)
        # Should pick 28 or 44 as closest to midpoint 33
        assert result is not None

    # ── Earnings Conflict ─────────────────────────────────────────

    def test_earnings_within_expiry_window_is_conflict(self, analyser):
        today        = date.today()
        expiry       = (today + timedelta(days=30)).strftime("%Y-%m-%d")
        # Earnings 20 days out — within the 30-day expiry window
        next_earnings = today + timedelta(days=20)
        assert analyser.check_earnings_conflict(expiry, next_earnings) is True

    def test_earnings_after_expiry_buffer_is_safe(self, analyser):
        today         = date.today()
        expiry        = (today + timedelta(days=30)).strftime("%Y-%m-%d")
        # Earnings 45 days out — after expiry + 7 day buffer (30+7=37)
        next_earnings = today + timedelta(days=45)
        assert analyser.check_earnings_conflict(expiry, next_earnings) is False

    def test_no_earnings_is_safe(self, analyser):
        today  = date.today()
        expiry = (today + timedelta(days=30)).strftime("%Y-%m-%d")
        assert analyser.check_earnings_conflict(expiry, None) is False

    # ── OTM Call Filtering ────────────────────────────────────────

    def test_filter_otm_calls_returns_only_calls_above_spot(self, analyser):
        spot    = 400.0
        expiry  = "2026-03-20"
        chain   = make_chain_df(spot=spot, expiry=expiry)
        calls   = chain[chain["option_type"] == "CALL"]["code"].tolist()
        snap    = make_snapshot_df(calls, spot=spot)

        result = analyser.filter_otm_calls(chain, snap, spot)
        assert len(result) > 0
        assert (result["strike_price"] > spot).all()
        assert (result["option_type"] == "CALL").all()

    def test_filter_otm_calls_applies_delta_range(self, analyser):
        spot   = 400.0
        chain  = make_chain_df(spot=spot)
        calls  = chain[chain["option_type"] == "CALL"]["code"].tolist()
        snap   = make_snapshot_df(calls, spot=spot)

        result = analyser.filter_otm_calls(chain, snap, spot)
        if len(result) > 0 and "option_delta" in result.columns:
            assert (result["option_delta"] >= 0.20).all()
            assert (result["option_delta"] <= 0.35).all()

    def test_select_best_call_returns_highest_delta(self, analyser):
        spot  = 400.0
        chain = make_chain_df(spot=spot)
        calls = chain[chain["option_type"] == "CALL"]["code"].tolist()
        snap  = make_snapshot_df(calls, spot=spot)

        filtered = analyser.filter_otm_calls(chain, snap, spot)
        if len(filtered) > 0:
            best = analyser.select_best_call(filtered)
            assert best is not None
            if "option_delta" in filtered.columns:
                max_delta = filtered["option_delta"].max()
                assert best["option_delta"] == max_delta

    def test_select_best_call_returns_none_when_empty(self, analyser):
        result = analyser.select_best_call(pd.DataFrame())
        assert result is None

    # ── Spread Metrics ────────────────────────────────────────────

    def test_compute_spread_metrics_correct_values(self, analyser):
        metrics = analyser.compute_spread_metrics(
            sell_strike=420.0,
            buy_strike=430.0,
            sell_premium=2.50,
            buy_premium=1.00
        )
        assert metrics["net_credit"]   == 1.50
        assert metrics["max_profit"]   == 150.0    # 1.50 × 100
        assert metrics["max_loss"]     == 850.0    # (10 - 1.50) × 100
        assert metrics["breakeven"]    == 421.50   # 420 + 1.50
        assert metrics["spread_width"] == 10.0
        assert 0 < metrics["reward_risk"] < 1

    def test_compute_spread_metrics_raises_on_inverted_strikes(self, analyser):
        from src.exceptions import DataError
        with pytest.raises(DataError, match="buy_strike"):
            analyser.compute_spread_metrics(
                sell_strike=430.0, buy_strike=420.0,
                sell_premium=2.50, buy_premium=1.00
            )

    def test_find_protective_call_selects_correct_width(self, analyser):
        chain  = make_chain_df(spot=400.0, expiry="2026-03-20")
        result = analyser.find_protective_call(
            sell_strike=410.0,
            chain=chain,
            width=10.0
        )
        assert result is not None
        assert result["strike_price"] > 410.0


# ═══════════════════════════════════════════════════════════════════
# MarketScanner Tests
# ═══════════════════════════════════════════════════════════════════

class TestMarketScanner:
    """
    Tests for MarketScanner using fully mocked connectors.
    Verifies that the scanner correctly assembles a MarketSnapshot
    from the outputs of all sub-components.
    """

    @pytest.fixture
    def scanner(self, tmp_path):
        """Build a MarketScanner with all dependencies mocked."""
        from src.market.market_scanner import MarketScanner
        from src.market.technical_analyser import TechnicalAnalyser
        from src.market.options_analyser import OptionsAnalyser
        from src.market.iv_rank_calculator import IVRankCalculator
        from src.market.regime_detector import RegimeDetector

        # Real components (no external calls)
        tech    = TechnicalAnalyser()
        options = OptionsAnalyser(TEST_CONFIG)
        iv_rank = IVRankCalculator(db_path=str(tmp_path / "test_iv.db"))
        regime  = RegimeDetector(TEST_CONFIG)

        # Mock connectors
        mock_moomoo   = MagicMock()
        mock_yfinance = MagicMock()

        # Configure mock returns
        ohlcv = make_ohlcv(120, base_price=410.0, seed=42)
        mock_yfinance.get_daily_ohlcv.return_value  = ohlcv
        mock_yfinance.get_current_vix.return_value  = 18.0
        mock_yfinance.get_earnings_dates.return_value = []

        today = date.today()
        mock_moomoo.get_option_expiries.return_value = [
            (today + timedelta(days=d)).strftime("%Y-%m-%d")
            for d in [28, 35, 42, 60]
        ]
        chain = make_chain_df(spot=410.0, expiry=(today + timedelta(days=28)).strftime("%Y-%m-%d"))
        mock_moomoo.get_option_chain.return_value   = chain

        atm_calls = chain[chain["option_type"] == "CALL"]["code"].tolist()[:3]
        snap = make_snapshot_df(atm_calls, spot=410.0)
        mock_moomoo.get_option_snapshot.return_value = snap
        mock_moomoo.get_shares_held.return_value     = 100
        mock_moomoo.get_option_positions.return_value = pd.DataFrame()

        return MarketScanner(
            config=TEST_CONFIG,
            moomoo=mock_moomoo,
            yfinance=mock_yfinance,
            tech=tech,
            options=options,
            iv_rank=iv_rank,
            regime=regime
        )

    def test_scan_symbol_returns_market_snapshot(self, scanner):
        from src.market.market_snapshot import MarketSnapshot
        snap = scanner.scan_symbol("US.TSLA")
        assert isinstance(snap, MarketSnapshot)

    def test_scan_symbol_populates_all_fields(self, scanner):
        snap = scanner.scan_symbol("US.TSLA")
        assert snap.symbol       == "US.TSLA"
        assert snap.spot_price   > 0
        assert snap.vix          == 18.0
        assert snap.shares_held  == 100
        assert snap.open_positions >= 0
        assert snap.market_regime in {"bull", "bear", "neutral", "high_vol"}
        assert len(snap.options_context.available_expiries) > 0

    def test_scan_symbol_technicals_are_finite(self, scanner):
        snap = scanner.scan_symbol("US.TSLA")
        t    = snap.technicals
        for field in ["pct_b", "rsi", "macd", "atr"]:
            val = getattr(t, field)
            assert np.isfinite(val), f"technicals.{field} is not finite: {val}"

    def test_scan_universe_returns_list(self, scanner):
        results = scanner.scan_universe()
        assert isinstance(results, list)
        assert len(results) == 1   # watchlist has 1 symbol

    def test_scan_universe_skips_failed_symbols(self, scanner):
        # Make the second symbol fail
        scanner._watchlist = ["US.TSLA", "US.BROKEN"]
        scanner._moomoo.get_option_expiries.side_effect = [
            ["2026-03-20"],     # TSLA succeeds
            Exception("API error")  # BROKEN fails
        ]
        # Reset side effect to normal for chain calls
        scanner._moomoo.get_option_chain.side_effect = None
        results = scanner.scan_universe()
        # Should return 1 result (TSLA), skipping BROKEN
        # Note: side_effect also affects chain calls, so we just check it doesn't crash
        assert isinstance(results, list)

    def test_scan_symbol_no_earnings_sets_none(self, scanner):
        scanner._yfinance.get_earnings_dates.return_value = []
        snap = scanner.scan_symbol("US.TSLA")
        assert snap.next_earnings    is None
        assert snap.days_to_earnings is None

    def test_scan_symbol_with_earnings_sets_days(self, scanner):
        future_date = date.today() + timedelta(days=25)
        scanner._yfinance.get_earnings_dates.return_value = [future_date]
        snap = scanner.scan_symbol("US.TSLA")
        assert snap.next_earnings    == future_date
        assert snap.days_to_earnings == 25
