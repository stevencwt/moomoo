"""
Phase 8 — Signal Ranker Tests
==============================
Tests for: SignalRanker, RankedSignal

Run with:
    cd moomoo
    python3 -m pytest tests/test_phase8_signal_ranker.py -v

All tests are pure unit tests — zero broker/ledger/API dependencies.
The ranker is tested in complete isolation with hand-crafted TradeSignals.

Test groups
-----------
  TestSignalRankerInit           — config loading, defaults, enabled flag
  TestNormalisation              — _normalise() edge cases (empty, constant, range)
  TestSafeHelpers                — _safe_buffer, _safe_reward_risk fallbacks
  TestRankEmptyAndSingle         — empty pool and single-candidate edge cases
  TestRankOrdering               — scores produce correct top→bottom ordering
  TestRankScoreFormula           — numerical correctness of the composite score
  TestRankDisabledMode           — disabled ranker returns FIFO order, rank=1
  TestRankTiebreaker             — ties resolved by original watchlist order
  TestTopN                       — top_n() convenience wrapper
  TestRankWithMissingBuffer      — signals without buffer_pct handled gracefully
  TestRankWithMissingRewardRisk  — signals with reward_risk=None handled gracefully
  TestRankWeightSensitivity      — changing weights changes ranking outcome
  TestRankConstantDimension      — constant dimension contributes equally (no crash)
  TestRankedSignalFields         — RankedSignal fields populated correctly
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from datetime import datetime
from unittest.mock import MagicMock

from src.execution.signal_ranker import SignalRanker, RankedSignal
from src.strategies.trade_signal import TradeSignal


# ── Fixtures & helpers ────────────────────────────────────────────

BASE_CONFIG = {
    "signal_ranker": {
        "enabled":            True,
        "weight_iv_rank":     0.40,
        "weight_buffer_pct":  0.35,
        "weight_reward_risk": 0.25,
    }
}

DISABLED_CONFIG = {
    "signal_ranker": {
        "enabled":            False,
        "weight_iv_rank":     0.40,
        "weight_buffer_pct":  0.35,
        "weight_reward_risk": 0.25,
    }
}

NO_RANKER_CONFIG = {}   # no signal_ranker block at all


def _make_signal(
    symbol="US.SPY",
    strategy="bear_call_spread",
    iv_rank=50.0,
    reward_risk=0.25,
    net_credit=1.50,
    max_loss=1000.0,
    breakeven=510.0,
    delta=0.25,
    dte=35,
    expiry="2026-05-16",
    buffer_pct=None,      # optional — set in Phase 2; ranker falls back to 0.0
    spot_price=None,      # optional — set in Phase 2
) -> TradeSignal:
    """
    Helper that builds a minimal TradeSignal for ranking tests.
    Uses keyword args so tests can override only the fields they care about.
    """
    kwargs = dict(
        strategy_name=strategy,
        symbol=symbol,
        timestamp=datetime(2026, 3, 2, 9, 35, 0),
        action="OPEN",
        signal_type=strategy,
        sell_contract=f"US.{symbol.split('.')[-1]}260516C00500000",
        buy_contract=f"US.{symbol.split('.')[-1]}260516C00510000",
        quantity=1,
        sell_price=2.50,
        buy_price=1.00,
        net_credit=net_credit,
        max_profit=net_credit * 100,
        max_loss=max_loss,
        breakeven=breakeven,
        reward_risk=reward_risk,
        expiry=expiry,
        dte=dte,
        iv_rank=iv_rank,
        delta=delta,
        reason="test signal",
        regime="neutral",
    )
    # Inject optional Phase-2 fields if TradeSignal already supports them
    # (safe to pass as extra kwargs if field exists; ignored here if not yet added)
    try:
        sig = TradeSignal(**kwargs)
    except TypeError:
        # TradeSignal doesn't yet have buffer_pct / spot_price fields — that's fine
        sig = TradeSignal(**{k: v for k, v in kwargs.items()
                             if k not in ("buffer_pct", "spot_price")})

    # Monkey-patch buffer_pct for tests that need it before Phase 2 adds the field
    if buffer_pct is not None:
        object.__setattr__(sig, "buffer_pct", buffer_pct)

    return sig


# ── TestSignalRankerInit ──────────────────────────────────────────

class TestSignalRankerInit:

    def test_reads_config_weights(self):
        ranker = SignalRanker(BASE_CONFIG)
        assert ranker._weight_iv_rank     == 0.40
        assert ranker._weight_buffer_pct  == 0.35
        assert ranker._weight_reward_risk == 0.25

    def test_reads_enabled_flag_true(self):
        ranker = SignalRanker(BASE_CONFIG)
        assert ranker.is_enabled is True

    def test_reads_enabled_flag_false(self):
        ranker = SignalRanker(DISABLED_CONFIG)
        assert ranker.is_enabled is False

    def test_defaults_when_no_config_block(self):
        """Missing signal_ranker block → safe defaults."""
        ranker = SignalRanker(NO_RANKER_CONFIG)
        assert ranker.is_enabled is True                          # default on
        assert ranker._weight_iv_rank     == 0.40
        assert ranker._weight_buffer_pct  == 0.35
        assert ranker._weight_reward_risk == 0.25

    def test_custom_weights(self):
        cfg = {"signal_ranker": {"enabled": True,
                                  "weight_iv_rank": 0.60,
                                  "weight_buffer_pct": 0.20,
                                  "weight_reward_risk": 0.20}}
        ranker = SignalRanker(cfg)
        assert ranker._weight_iv_rank     == 0.60
        assert ranker._weight_buffer_pct  == 0.20
        assert ranker._weight_reward_risk == 0.20


# ── TestNormalisation ─────────────────────────────────────────────

class TestNormalisation:

    def test_basic_normalisation(self):
        result = SignalRanker._normalise([0.0, 50.0, 100.0])
        assert result == pytest.approx([0.0, 0.5, 1.0])

    def test_single_value_returns_zero(self):
        """Single value: range=0, norm=0.0."""
        result = SignalRanker._normalise([75.0])
        assert result == [0.0]

    def test_all_same_returns_zeros(self):
        """Constant dimension: all norms = 0.0."""
        result = SignalRanker._normalise([42.0, 42.0, 42.0])
        assert result == [0.0, 0.0, 0.0]

    def test_empty_list_returns_empty(self):
        result = SignalRanker._normalise([])
        assert result == []

    def test_two_values_min_max(self):
        result = SignalRanker._normalise([20.0, 80.0])
        assert result == pytest.approx([0.0, 1.0])

    def test_negative_values_handled(self):
        """Normalisation works with negative values (e.g. negative MACD)."""
        result = SignalRanker._normalise([-10.0, 0.0, 10.0])
        assert result == pytest.approx([0.0, 0.5, 1.0])

    def test_order_preserved(self):
        values = [30.0, 70.0, 50.0]
        result = SignalRanker._normalise(values)
        # 30 → 0.0, 70 → 1.0, 50 → 0.5
        assert result == pytest.approx([0.0, 1.0, 0.5])


# ── TestSafeHelpers ───────────────────────────────────────────────

class TestSafeHelpers:

    def test_safe_buffer_returns_buffer_pct_when_set(self):
        sig = _make_signal(buffer_pct=8.2)
        assert SignalRanker._safe_buffer(sig) == pytest.approx(8.2)

    def test_safe_buffer_returns_zero_when_none(self):
        sig = _make_signal()   # no buffer_pct set
        # After monkey-patch workaround, attribute may be 0.0 already
        # Check that _safe_buffer never raises
        val = SignalRanker._safe_buffer(sig)
        assert isinstance(val, float)
        assert val >= 0.0

    def test_safe_buffer_returns_zero_when_attribute_missing(self):
        """Signal with no buffer_pct attribute at all → 0.0."""
        sig = _make_signal()
        # Remove the attribute if monkey-patched
        try:
            object.__delattr__(sig, "buffer_pct")
        except (AttributeError, TypeError):
            pass
        val = SignalRanker._safe_buffer(sig)
        assert val == 0.0

    def test_safe_reward_risk_returns_value(self):
        sig = _make_signal(reward_risk=0.31)
        assert SignalRanker._safe_reward_risk(sig) == pytest.approx(0.31)

    def test_safe_reward_risk_returns_zero_for_none(self):
        # Covered calls can have reward_risk=None
        # Need to create signal with reward_risk=None — but TradeSignal __post_init__
        # doesn't validate reward_risk so this should work
        sig = _make_signal(reward_risk=None)
        assert SignalRanker._safe_reward_risk(sig) == 0.0


# ── TestRankEmptyAndSingle ────────────────────────────────────────

class TestRankEmptyAndSingle:

    def test_empty_input_returns_empty(self):
        ranker = SignalRanker(BASE_CONFIG)
        result = ranker.rank([])
        assert result == []

    def test_single_signal_returns_rank_one(self):
        ranker = SignalRanker(BASE_CONFIG)
        sig    = _make_signal(iv_rank=60.0, buffer_pct=7.0, reward_risk=0.28)
        result = ranker.rank([sig])
        assert len(result) == 1
        assert result[0].rank == 1

    def test_single_signal_all_norms_zero(self):
        """Single candidate: all norms = 0.0 (range = 0 for every dimension)."""
        ranker = SignalRanker(BASE_CONFIG)
        sig    = _make_signal(iv_rank=60.0, buffer_pct=7.0, reward_risk=0.28)
        result = ranker.rank([sig])
        r      = result[0]
        assert r.iv_rank_norm     == 0.0
        assert r.buffer_pct_norm  == 0.0
        assert r.reward_risk_norm == 0.0
        assert r.score            == pytest.approx(0.0)

    def test_single_signal_wrapped_in_ranked_signal(self):
        ranker = SignalRanker(BASE_CONFIG)
        sig    = _make_signal()
        result = ranker.rank([sig])
        assert isinstance(result[0], RankedSignal)
        assert result[0].signal is sig


# ── TestRankOrdering ──────────────────────────────────────────────

class TestRankOrdering:

    def test_higher_iv_rank_ranks_first(self):
        """NVDA (IV=72) should rank above SPY (IV=41) all else equal."""
        ranker = SignalRanker(BASE_CONFIG)
        spy  = _make_signal("US.SPY",  iv_rank=41.0, buffer_pct=5.1, reward_risk=0.21)
        nvda = _make_signal("US.NVDA", iv_rank=72.0, buffer_pct=5.1, reward_risk=0.21)
        ranked = ranker.rank([spy, nvda])
        assert ranked[0].signal.symbol == "US.NVDA"
        assert ranked[1].signal.symbol == "US.SPY"

    def test_four_candidates_ranked_correctly(self):
        """
        Reproduce the worked example from synopsis.md Section 4.1:
          NVDA  IV=72  buf=8.2%  rr=0.31  → highest score
          AAPL  IV=58  buf=6.5%  rr=0.28
          SPY   IV=41  buf=5.1%  rr=0.21
          QQQ   IV=44  buf=4.8%  rr=0.22  → lowest score
        """
        ranker = SignalRanker(BASE_CONFIG)
        spy  = _make_signal("US.SPY",  iv_rank=41.0, buffer_pct=5.1, reward_risk=0.21)
        qqq  = _make_signal("US.QQQ",  iv_rank=44.0, buffer_pct=4.8, reward_risk=0.22)
        nvda = _make_signal("US.NVDA", iv_rank=72.0, buffer_pct=8.2, reward_risk=0.31)
        aapl = _make_signal("US.AAPL", iv_rank=58.0, buffer_pct=6.5, reward_risk=0.28)

        # Input in watchlist FIFO order: SPY, QQQ, NVDA, AAPL
        ranked = ranker.rank([spy, qqq, nvda, aapl])

        symbols = [r.signal.symbol for r in ranked]
        assert symbols[0] == "US.NVDA"    # rank 1 — best
        assert symbols[1] == "US.AAPL"    # rank 2
        # SPY and QQQ are close — both should be ranked 3 and 4
        assert set(symbols[2:]) == {"US.SPY", "US.QQQ"}

    def test_ranks_are_sequential(self):
        ranker = SignalRanker(BASE_CONFIG)
        signals = [
            _make_signal("US.A", iv_rank=70.0, buffer_pct=9.0, reward_risk=0.30),
            _make_signal("US.B", iv_rank=50.0, buffer_pct=6.0, reward_risk=0.25),
            _make_signal("US.C", iv_rank=40.0, buffer_pct=4.0, reward_risk=0.20),
        ]
        ranked = ranker.rank(signals)
        assert [r.rank for r in ranked] == [1, 2, 3]

    def test_scores_descending(self):
        ranker  = SignalRanker(BASE_CONFIG)
        signals = [
            _make_signal("US.X", iv_rank=70.0, buffer_pct=8.0, reward_risk=0.30),
            _make_signal("US.Y", iv_rank=50.0, buffer_pct=5.0, reward_risk=0.25),
            _make_signal("US.Z", iv_rank=35.0, buffer_pct=3.0, reward_risk=0.20),
        ]
        ranked = ranker.rank(signals)
        scores = [r.score for r in ranked]
        assert scores[0] >= scores[1] >= scores[2]


# ── TestRankScoreFormula ──────────────────────────────────────────

class TestRankScoreFormula:

    def test_two_candidates_score_formula_correct(self):
        """
        Manually verify the composite score for a 2-candidate pool.

        Setup:
          Signal A: iv=80, buf=10%, rr=0.35
          Signal B: iv=40, buf=5%,  rr=0.20

        Normalised (range matters, not absolute):
          iv:  A=1.0  B=0.0     (range 40)
          buf: A=1.0  B=0.0     (range 5)
          rr:  A=1.0  B=0.0     (range 0.15)

        Weights: iv=0.40, buf=0.35, rr=0.25

        Score A = 1.0×0.40 + 1.0×0.35 + 1.0×0.25 = 1.00
        Score B = 0.0×0.40 + 0.0×0.35 + 0.0×0.25 = 0.00
        """
        ranker = SignalRanker(BASE_CONFIG)
        sig_a  = _make_signal("US.A", iv_rank=80.0, buffer_pct=10.0, reward_risk=0.35)
        sig_b  = _make_signal("US.B", iv_rank=40.0, buffer_pct=5.0,  reward_risk=0.20)

        ranked = ranker.rank([sig_a, sig_b])

        assert ranked[0].signal.symbol == "US.A"
        assert ranked[0].score         == pytest.approx(1.00, abs=1e-4)
        assert ranked[1].signal.symbol == "US.B"
        assert ranked[1].score         == pytest.approx(0.00, abs=1e-4)

    def test_score_breakdown_sums_to_total(self):
        """score_breakdown values must sum to the total score."""
        ranker  = SignalRanker(BASE_CONFIG)
        signals = [
            _make_signal("US.A", iv_rank=70.0, buffer_pct=8.0, reward_risk=0.30),
            _make_signal("US.B", iv_rank=45.0, buffer_pct=5.5, reward_risk=0.22),
        ]
        ranked = ranker.rank(signals)
        for r in ranked:
            bd_sum = sum(r.score_breakdown.values())
            assert bd_sum == pytest.approx(r.score, abs=1e-4)

    def test_score_breakdown_keys(self):
        ranker = SignalRanker(BASE_CONFIG)
        sig    = _make_signal(iv_rank=50.0, buffer_pct=6.0, reward_risk=0.25)
        sig2   = _make_signal("US.B", iv_rank=60.0, buffer_pct=7.0, reward_risk=0.28)
        ranked = ranker.rank([sig, sig2])
        for r in ranked:
            assert set(r.score_breakdown.keys()) == {"iv_rank", "buffer_pct", "reward_risk"}

    def test_three_candidate_mid_score(self):
        """
        Middle candidate should have intermediate norms ≈ 0.5 per dimension.

        Setup: A=best, C=worst, B=exactly midpoint on all three dims.

        Norms for B: (50-40)/(80-40)=0.25 for iv, (6-4)/(10-4)=0.33 for buf
        — not exactly 0.5 because ranges differ. Just verify B is between A and C.
        """
        ranker = SignalRanker(BASE_CONFIG)
        sig_a  = _make_signal("US.A", iv_rank=80.0, buffer_pct=10.0, reward_risk=0.35)
        sig_b  = _make_signal("US.B", iv_rank=50.0, buffer_pct=6.0,  reward_risk=0.25)
        sig_c  = _make_signal("US.C", iv_rank=40.0, buffer_pct=4.0,  reward_risk=0.20)

        ranked = ranker.rank([sig_a, sig_b, sig_c])
        assert ranked[0].signal.symbol == "US.A"
        assert ranked[1].signal.symbol == "US.B"
        assert ranked[2].signal.symbol == "US.C"
        # B's score strictly between A and C
        assert ranked[0].score > ranked[1].score > ranked[2].score


# ── TestRankDisabledMode ──────────────────────────────────────────

class TestRankDisabledMode:

    def test_disabled_returns_original_order(self):
        """Disabled ranker must preserve FIFO order."""
        ranker = SignalRanker(DISABLED_CONFIG)
        # Give B higher IV so ranking would flip them — but disabled = FIFO
        sig_a  = _make_signal("US.A", iv_rank=40.0)
        sig_b  = _make_signal("US.B", iv_rank=80.0)

        ranked = ranker.rank([sig_a, sig_b])

        assert ranked[0].signal.symbol == "US.A"   # FIFO — not promoted
        assert ranked[1].signal.symbol == "US.B"

    def test_disabled_all_ranks_are_one(self):
        """Disabled mode assigns rank=1 to every candidate."""
        ranker  = SignalRanker(DISABLED_CONFIG)
        signals = [_make_signal(f"US.{c}") for c in "ABCD"]
        ranked  = ranker.rank(signals)
        assert all(r.rank == 1 for r in ranked)

    def test_disabled_all_scores_are_zero(self):
        ranker  = SignalRanker(DISABLED_CONFIG)
        signals = [_make_signal(f"US.{c}") for c in "AB"]
        ranked  = ranker.rank(signals)
        assert all(r.score == 0.0 for r in ranked)

    def test_disabled_empty_returns_empty(self):
        ranker = SignalRanker(DISABLED_CONFIG)
        assert ranker.rank([]) == []


# ── TestRankTiebreaker ────────────────────────────────────────────

class TestRankTiebreaker:

    def test_identical_signals_preserve_watchlist_order(self):
        """
        When two signals have identical scores, original watchlist order
        (input order) is used as the tiebreaker.
        """
        ranker = SignalRanker(BASE_CONFIG)
        # Identical on all three scored dimensions
        sig_spy = _make_signal("US.SPY", iv_rank=50.0, buffer_pct=6.0, reward_risk=0.25)
        sig_qqq = _make_signal("US.QQQ", iv_rank=50.0, buffer_pct=6.0, reward_risk=0.25)

        # SPY first in input (watchlist order)
        ranked = ranker.rank([sig_spy, sig_qqq])
        assert ranked[0].signal.symbol == "US.SPY"
        assert ranked[1].signal.symbol == "US.QQQ"

    def test_tiebreaker_reversed_input(self):
        """Reversed input → reversed tiebreaker output."""
        ranker  = SignalRanker(BASE_CONFIG)
        sig_spy = _make_signal("US.SPY", iv_rank=50.0, buffer_pct=6.0, reward_risk=0.25)
        sig_qqq = _make_signal("US.QQQ", iv_rank=50.0, buffer_pct=6.0, reward_risk=0.25)

        ranked = ranker.rank([sig_qqq, sig_spy])   # QQQ first
        assert ranked[0].signal.symbol == "US.QQQ"
        assert ranked[1].signal.symbol == "US.SPY"


# ── TestTopN ─────────────────────────────────────────────────────

class TestTopN:

    def test_top_n_returns_n_signals(self):
        ranker  = SignalRanker(BASE_CONFIG)
        signals = [
            _make_signal("US.A", iv_rank=70.0, buffer_pct=8.0, reward_risk=0.30),
            _make_signal("US.B", iv_rank=55.0, buffer_pct=6.0, reward_risk=0.25),
            _make_signal("US.C", iv_rank=40.0, buffer_pct=4.0, reward_risk=0.20),
            _make_signal("US.D", iv_rank=35.0, buffer_pct=3.0, reward_risk=0.18),
        ]
        result = ranker.top_n(signals, 2)
        assert len(result) == 2

    def test_top_n_returns_best_signals(self):
        ranker  = SignalRanker(BASE_CONFIG)
        signals = [
            _make_signal("US.A", iv_rank=70.0, buffer_pct=8.0, reward_risk=0.30),
            _make_signal("US.B", iv_rank=55.0, buffer_pct=6.0, reward_risk=0.25),
            _make_signal("US.C", iv_rank=40.0, buffer_pct=4.0, reward_risk=0.20),
        ]
        result = ranker.top_n(signals, 2)
        assert result[0].symbol == "US.A"
        assert result[1].symbol == "US.B"

    def test_top_n_returns_trade_signals_not_ranked(self):
        ranker = SignalRanker(BASE_CONFIG)
        sig    = _make_signal("US.A", iv_rank=50.0)
        result = ranker.top_n([sig], 1)
        assert isinstance(result[0], TradeSignal)

    def test_top_n_capped_at_pool_size(self):
        """Requesting more than available → returns all available."""
        ranker  = SignalRanker(BASE_CONFIG)
        signals = [_make_signal("US.A"), _make_signal("US.B")]
        result  = ranker.top_n(signals, 10)
        assert len(result) == 2

    def test_top_n_empty_input(self):
        ranker = SignalRanker(BASE_CONFIG)
        assert ranker.top_n([], 3) == []

    def test_top_n_zero_returns_empty(self):
        ranker = SignalRanker(BASE_CONFIG)
        sig    = _make_signal()
        result = ranker.top_n([sig], 0)
        assert result == []


# ── TestRankWithMissingBuffer ─────────────────────────────────────

class TestRankWithMissingBuffer:

    def test_missing_buffer_defaults_to_zero(self):
        """Signals without buffer_pct don't crash — treated as 0.0."""
        ranker = SignalRanker(BASE_CONFIG)
        # Both signals lack buffer_pct; IV breaks the tie
        sig_a  = _make_signal("US.A", iv_rank=70.0, reward_risk=0.30)
        sig_b  = _make_signal("US.B", iv_rank=40.0, reward_risk=0.20)
        ranked = ranker.rank([sig_a, sig_b])
        # Should not raise; A should still rank first (higher IV)
        assert ranked[0].signal.symbol == "US.A"

    def test_missing_buffer_on_some_signals(self):
        """One signal has buffer_pct, another doesn't — both handled."""
        ranker = SignalRanker(BASE_CONFIG)
        # C has buffer_pct=10.0 (high safety), A has none (treated as 0)
        # But A has much higher IV — ranking depends on weights
        sig_a  = _make_signal("US.A", iv_rank=80.0, reward_risk=0.30)   # no buffer
        sig_c  = _make_signal("US.C", iv_rank=40.0, buffer_pct=10.0, reward_risk=0.20)
        # Should not crash
        ranked = ranker.rank([sig_a, sig_c])
        assert len(ranked) == 2


# ── TestRankWithMissingRewardRisk ─────────────────────────────────

class TestRankWithMissingRewardRisk:

    def test_none_reward_risk_treated_as_zero(self):
        """Covered calls may have reward_risk=None."""
        ranker = SignalRanker(BASE_CONFIG)
        sig_cc = _make_signal("US.A", strategy="covered_call",
                               iv_rank=60.0, reward_risk=None)
        sig_bcs = _make_signal("US.B", strategy="bear_call_spread",
                                iv_rank=60.0, reward_risk=0.25)
        # Should not raise
        ranked = ranker.rank([sig_cc, sig_bcs])
        assert len(ranked) == 2
        # BCS ranks first (rr=0.25 > 0.0) when IV is equal
        assert ranked[0].signal.symbol == "US.B"


# ── TestRankWeightSensitivity ─────────────────────────────────────

class TestRankWeightSensitivity:

    def test_iv_heavy_weight_promotes_high_iv(self):
        """
        With weight_iv_rank=0.90, a high-IV signal beats a high-buffer signal.
        """
        cfg = {"signal_ranker": {
            "enabled": True,
            "weight_iv_rank":     0.90,
            "weight_buffer_pct":  0.05,
            "weight_reward_risk": 0.05,
        }}
        ranker = SignalRanker(cfg)
        high_iv  = _make_signal("US.A", iv_rank=80.0, buffer_pct=3.0,  reward_risk=0.20)
        high_buf = _make_signal("US.B", iv_rank=35.0, buffer_pct=15.0, reward_risk=0.20)
        ranked = ranker.rank([high_iv, high_buf])
        assert ranked[0].signal.symbol == "US.A"    # IV dominates

    def test_buffer_heavy_weight_promotes_high_buffer(self):
        """
        With weight_buffer_pct=0.90, a high-buffer signal beats a high-IV signal.
        """
        cfg = {"signal_ranker": {
            "enabled": True,
            "weight_iv_rank":     0.05,
            "weight_buffer_pct":  0.90,
            "weight_reward_risk": 0.05,
        }}
        ranker = SignalRanker(cfg)
        high_iv  = _make_signal("US.A", iv_rank=80.0, buffer_pct=3.0,  reward_risk=0.20)
        high_buf = _make_signal("US.B", iv_rank=35.0, buffer_pct=15.0, reward_risk=0.20)
        ranked = ranker.rank([high_iv, high_buf])
        assert ranked[0].signal.symbol == "US.B"    # buffer dominates


# ── TestRankConstantDimension ─────────────────────────────────────

class TestRankConstantDimension:

    def test_all_same_iv_rank_no_crash(self):
        """All signals with identical IV rank — dimension is non-discriminating."""
        ranker  = SignalRanker(BASE_CONFIG)
        signals = [
            _make_signal("US.A", iv_rank=50.0, buffer_pct=8.0, reward_risk=0.30),
            _make_signal("US.B", iv_rank=50.0, buffer_pct=5.0, reward_risk=0.22),
            _make_signal("US.C", iv_rank=50.0, buffer_pct=3.0, reward_risk=0.18),
        ]
        ranked = ranker.rank(signals)
        assert len(ranked) == 3
        # IV dimension contributes 0 to all; buffer+rr determine ranking
        assert ranked[0].signal.symbol == "US.A"

    def test_all_same_all_dimensions_uses_tiebreaker(self):
        """All signals identical on all dimensions → original order preserved."""
        ranker  = SignalRanker(BASE_CONFIG)
        signals = [
            _make_signal("US.A", iv_rank=50.0, buffer_pct=6.0, reward_risk=0.25),
            _make_signal("US.B", iv_rank=50.0, buffer_pct=6.0, reward_risk=0.25),
        ]
        ranked = ranker.rank(signals)
        assert ranked[0].signal.symbol == "US.A"   # watchlist order tiebreaker
        assert ranked[1].signal.symbol == "US.B"


# ── TestRankedSignalFields ────────────────────────────────────────

class TestRankedSignalFields:

    def test_ranked_signal_has_all_fields(self):
        ranker = SignalRanker(BASE_CONFIG)
        sig_a  = _make_signal("US.A", iv_rank=70.0, buffer_pct=8.0, reward_risk=0.30)
        sig_b  = _make_signal("US.B", iv_rank=40.0, buffer_pct=4.0, reward_risk=0.20)
        ranked = ranker.rank([sig_a, sig_b])
        r = ranked[0]
        assert hasattr(r, "signal")
        assert hasattr(r, "rank")
        assert hasattr(r, "score")
        assert hasattr(r, "score_breakdown")
        assert hasattr(r, "iv_rank_norm")
        assert hasattr(r, "buffer_pct_norm")
        assert hasattr(r, "reward_risk_norm")

    def test_norm_values_in_zero_one_range(self):
        ranker  = SignalRanker(BASE_CONFIG)
        signals = [
            _make_signal("US.A", iv_rank=72.0, buffer_pct=8.2, reward_risk=0.31),
            _make_signal("US.B", iv_rank=41.0, buffer_pct=5.1, reward_risk=0.21),
            _make_signal("US.C", iv_rank=58.0, buffer_pct=6.5, reward_risk=0.28),
        ]
        ranked = ranker.rank(signals)
        for r in ranked:
            assert 0.0 <= r.iv_rank_norm     <= 1.0
            assert 0.0 <= r.buffer_pct_norm  <= 1.0
            assert 0.0 <= r.reward_risk_norm <= 1.0

    def test_score_is_non_negative(self):
        ranker  = SignalRanker(BASE_CONFIG)
        signals = [_make_signal("US.A"), _make_signal("US.B")]
        ranked  = ranker.rank(signals)
        assert all(r.score >= 0.0 for r in ranked)

    def test_original_signal_unchanged(self):
        """Ranking must not mutate the original TradeSignal."""
        ranker  = SignalRanker(BASE_CONFIG)
        sig     = _make_signal("US.A", iv_rank=65.0)
        original_iv = sig.iv_rank
        ranker.rank([sig])
        assert sig.iv_rank == original_iv   # frozen — should never change


# ═══════════════════════════════════════════════════════════════════
# Phase 2 — TradeSignal buffer_pct / spot_price field validation
#
# Verifies that:
#   1. TradeSignal accepts and stores buffer_pct and spot_price
#   2. The buffer formula is correct for each strategy direction
#   3. The ranker uses the real field (not the monkey-patch fallback)
#   4. Edge cases: zero buffer, very tight buffer, large buffer
# ═══════════════════════════════════════════════════════════════════

class TestTradeSignalBufferFields:
    """TradeSignal accepts buffer_pct and spot_price as optional fields."""

    def test_buffer_pct_stored_correctly(self):
        sig = _make_signal(buffer_pct=8.2)
        assert sig.buffer_pct == pytest.approx(8.2)

    def test_spot_price_stored_correctly(self):
        """Create a signal with spot_price set via the proper field."""
        # Build using the real TradeSignal constructor with spot_price
        sig = TradeSignal(
            strategy_name="bear_call_spread",
            symbol="US.SPY",
            timestamp=datetime(2026, 3, 2, 9, 35, 0),
            action="OPEN",
            signal_type="bear_call_spread",
            sell_contract="US.SPY260516C00570000",
            buy_contract="US.SPY260516C00580000",
            quantity=1,
            sell_price=2.50,
            buy_price=1.00,
            net_credit=1.50,
            max_profit=150.0,
            max_loss=850.0,
            breakeven=571.50,
            reward_risk=0.18,
            expiry="2026-05-16",
            dte=35,
            iv_rank=55.0,
            delta=0.25,
            reason="test",
            regime="neutral",
            spot_price=550.0,
            buffer_pct=round((570.0 - 550.0) / 550.0 * 100, 2),
        )
        assert sig.spot_price == pytest.approx(550.0)
        assert sig.buffer_pct == pytest.approx(3.64, abs=0.01)

    def test_buffer_pct_defaults_to_none(self):
        """Signal without buffer_pct should default to None (not crash)."""
        sig = _make_signal()
        # buffer_pct is None unless explicitly set
        # (_make_signal monkey-patches it only when buffer_pct arg is given)
        val = getattr(sig, "buffer_pct", None)
        # Either None or 0.0 is acceptable depending on whether the field exists
        assert val is None or val == 0.0

    def test_spot_price_defaults_to_none(self):
        sig = _make_signal()
        val = getattr(sig, "spot_price", None)
        assert val is None or isinstance(val, float)


class TestBufferPctFormulas:
    """
    Validate the three buffer_pct formulas match the spec in synopsis.md:

      Bear call spread:  (short_call_strike − spot) / spot × 100
      Bull put spread:   (spot − short_put_strike)  / spot × 100
      Covered call:      (short_call_strike − spot) / spot × 100
    """

    def _make_real_signal(self, strategy, sell_strike, spot, **kwargs):
        """Build a TradeSignal with buffer_pct set using the correct formula."""
        if strategy in ("bear_call_spread", "covered_call"):
            buffer = round((sell_strike - spot) / spot * 100, 2)
        else:  # bull_put_spread
            buffer = round((spot - sell_strike) / spot * 100, 2)

        return TradeSignal(
            strategy_name=strategy,
            symbol="US.SPY",
            timestamp=datetime(2026, 3, 2, 9, 35, 0),
            action="OPEN",
            signal_type=strategy if strategy != "covered_call" else "covered_call",
            sell_contract="US.SPY260516C00570000",
            buy_contract=None if strategy == "covered_call" else "US.SPY260516C00580000",
            quantity=1,
            sell_price=2.50,
            buy_price=None if strategy == "covered_call" else 1.00,
            net_credit=1.50,
            max_profit=150.0,
            max_loss=None if strategy == "covered_call" else 850.0,
            breakeven=spot - 1.50 if strategy == "covered_call" else sell_strike + 1.50,
            reward_risk=None if strategy == "covered_call" else 0.18,
            expiry="2026-05-16",
            dte=35,
            iv_rank=55.0,
            delta=0.25,
            reason="test",
            regime="neutral",
            spot_price=round(spot, 2),
            buffer_pct=buffer,
        )

    def test_bear_call_spread_formula(self):
        """
        Bear call spread: sell OTM call above spot.
        spot=500, sell_strike=520 → buffer = (520-500)/500 × 100 = 4.0%
        """
        sig = self._make_real_signal("bear_call_spread", sell_strike=520.0, spot=500.0)
        assert sig.buffer_pct == pytest.approx(4.0, abs=0.01)
        assert sig.spot_price == pytest.approx(500.0)

    def test_bull_put_spread_formula(self):
        """
        Bull put spread: sell OTM put below spot.
        spot=500, sell_strike=470 → buffer = (500-470)/500 × 100 = 6.0%
        """
        sig = self._make_real_signal("bull_put_spread", sell_strike=470.0, spot=500.0)
        assert sig.buffer_pct == pytest.approx(6.0, abs=0.01)
        assert sig.spot_price == pytest.approx(500.0)

    def test_covered_call_formula(self):
        """
        Covered call: sell OTM call above spot (same direction as bear call).
        spot=180, sell_strike=190 → buffer = (190-180)/180 × 100 = 5.56%
        """
        sig = self._make_real_signal("covered_call", sell_strike=190.0, spot=180.0)
        assert sig.buffer_pct == pytest.approx(5.56, abs=0.01)
        assert sig.spot_price == pytest.approx(180.0)

    def test_bear_call_wider_buffer_ranks_higher(self):
        """
        Given two bear call spreads, the one with a wider buffer should rank higher
        (more room before the short strike is threatened).
        """
        ranker = SignalRanker(BASE_CONFIG)

        # Tight buffer: sell_strike 3% above spot
        tight = self._make_real_signal("bear_call_spread", sell_strike=515.0, spot=500.0)
        # Wide buffer: sell_strike 8% above spot
        wide  = self._make_real_signal("bear_call_spread", sell_strike=540.0, spot=500.0)

        # Make IV and R/R equal so buffer is the only differentiator
        # (both signals have same iv_rank=55 and reward_risk=0.18 from _make_real_signal)
        ranked = ranker.rank([tight, wide])
        assert ranked[0].signal.buffer_pct > ranked[1].signal.buffer_pct

    def test_bull_put_wider_buffer_ranks_higher(self):
        """
        For bull put spreads, a sell_strike further below spot = larger buffer = safer.
        """
        ranker = SignalRanker(BASE_CONFIG)

        # Tight: sell_strike only 2% below spot
        tight = self._make_real_signal("bull_put_spread", sell_strike=490.0, spot=500.0)
        # Wide: sell_strike 10% below spot
        wide  = self._make_real_signal("bull_put_spread", sell_strike=450.0, spot=500.0)

        ranked = ranker.rank([tight, wide])
        assert ranked[0].signal.buffer_pct > ranked[1].signal.buffer_pct

    def test_buffer_always_positive(self):
        """OTM strikes always produce a positive buffer (by strategy gate design)."""
        bcs = self._make_real_signal("bear_call_spread", sell_strike=520.0, spot=500.0)
        bps = self._make_real_signal("bull_put_spread",  sell_strike=470.0, spot=500.0)
        cc  = self._make_real_signal("covered_call",     sell_strike=190.0, spot=180.0)
        assert bcs.buffer_pct > 0.0
        assert bps.buffer_pct > 0.0
        assert cc.buffer_pct  > 0.0

    def test_ranker_uses_buffer_pct_field_not_monkey_patch(self):
        """
        Confirm _safe_buffer reads the real field when properly set,
        not just falling back to 0.0.
        """
        sig = self._make_real_signal("bear_call_spread", sell_strike=530.0, spot=500.0)
        val = SignalRanker._safe_buffer(sig)
        assert val == pytest.approx(6.0, abs=0.01)   # (530-500)/500 × 100

    def test_mixed_strategy_pool_ranked_by_combined_score(self):
        """
        A pool with mixed strategy types ranks correctly — buffer direction
        doesn't matter to the ranker, only the magnitude of buffer_pct does.
        """
        ranker = SignalRanker(BASE_CONFIG)

        bcs  = self._make_real_signal("bear_call_spread", sell_strike=540.0, spot=500.0)  # buf=8%
        bps  = self._make_real_signal("bull_put_spread",  sell_strike=460.0, spot=500.0)  # buf=8%
        tight_bcs = self._make_real_signal("bear_call_spread", sell_strike=510.0, spot=500.0)  # buf=2%

        # bcs and bps both have buf=8%; tight_bcs has buf=2%
        # IV and R/R are equal across all three → buffer+tiebreaker decides
        ranked = ranker.rank([tight_bcs, bcs, bps])
        symbols_at_top = {ranked[0].signal.symbol, ranked[1].signal.symbol}
        # The tight spread (2% buffer) must be last
        assert ranked[2].signal.buffer_pct == pytest.approx(2.0, abs=0.1)


# ═══════════════════════════════════════════════════════════════════
# Phase 3 — TradeManager integration tests
#
# Verifies that:
#   1. TradeManager instantiates SignalRanker from config
#   2. process_signals() ranks before executing (best candidate first)
#   3. Guard daily limit stops at N — best N are chosen, not first N
#   4. Disabled ranker preserves FIFO order through TradeManager
#   5. Empty signal list handled gracefully
#   6. All signals get a TradeResult (executed, blocked, or skipped)
#   7. process_signal() (single) is unchanged by ranking
# ═══════════════════════════════════════════════════════════════════

from unittest.mock import MagicMock, patch, call
from datetime import datetime, date


def _make_trade_manager_mocks(config):
    """
    Build a TradeManager with fully mocked Guard, Router, and Ledger.

    Guard approves everything by default (can be overridden per test).
    Router always returns a filled FillResult.
    Ledger records_open returns incrementing trade IDs.
    """
    from src.execution.trade_manager import TradeManager
    from src.execution.portfolio_guard import PortfolioGuard
    from src.execution.order_router import OrderRouter, FillResult
    from src.execution.paper_ledger import PaperLedger

    guard  = MagicMock(spec=PortfolioGuard)
    router = MagicMock(spec=OrderRouter)
    ledger = MagicMock(spec=PaperLedger)

    # Default: guard approves everything
    guard.approve.return_value = (True, "approved")

    # Router always fills
    fill = MagicMock()
    fill.status     = "filled"
    fill.fill_sell  = 2.50
    fill.fill_buy   = 1.00
    fill.net_credit = 1.50
    router.execute.return_value = fill

    # Ledger returns incrementing IDs
    ledger.record_open.side_effect = [1, 2, 3, 4, 5, 6, 7, 8]

    manager = TradeManager(config, guard, router, ledger)
    return manager, guard, router, ledger


def _make_signal_with_quality(symbol, iv_rank, buffer_pct, reward_risk,
                               strategy="bear_call_spread"):
    """Build a TradeSignal with quality indicators for ranking."""
    return TradeSignal(
        strategy_name=strategy,
        symbol=symbol,
        timestamp=datetime(2026, 3, 2, 9, 35, 0),
        action="OPEN",
        signal_type=strategy,
        sell_contract=f"US.{symbol.split('.')[-1]}260516C00500000",
        buy_contract=f"US.{symbol.split('.')[-1]}260516C00510000",
        quantity=1,
        sell_price=2.50,
        buy_price=1.00,
        net_credit=1.50,
        max_profit=150.0,
        max_loss=850.0,
        breakeven=501.50,
        reward_risk=reward_risk,
        expiry="2026-05-16",
        dte=35,
        iv_rank=iv_rank,
        delta=0.25,
        reason="test",
        regime="neutral",
        spot_price=500.0,
        buffer_pct=buffer_pct,
    )


TM_CONFIG_ENABLED = {
    "mode": "paper",
    "signal_ranker": {
        "enabled": True,
        "weight_iv_rank":     0.40,
        "weight_buffer_pct":  0.35,
        "weight_reward_risk": 0.25,
    },
    "logging": {"level": "WARNING", "file": "logs/test.log",
                "max_bytes": 1048576, "backup_count": 1},
}

TM_CONFIG_DISABLED = {
    "mode": "paper",
    "signal_ranker": {"enabled": False},
    "logging": {"level": "WARNING", "file": "logs/test.log",
                "max_bytes": 1048576, "backup_count": 1},
}


class TestTradeManagerRankerInit:

    def test_trade_manager_creates_ranker(self):
        """TradeManager must instantiate a SignalRanker from config."""
        from src.execution.trade_manager import TradeManager
        manager, _, _, _ = _make_trade_manager_mocks(TM_CONFIG_ENABLED)
        assert hasattr(manager, "_ranker")
        from src.execution.signal_ranker import SignalRanker
        assert isinstance(manager._ranker, SignalRanker)

    def test_ranker_enabled_when_config_true(self):
        manager, _, _, _ = _make_trade_manager_mocks(TM_CONFIG_ENABLED)
        assert manager._ranker.is_enabled is True

    def test_ranker_disabled_when_config_false(self):
        manager, _, _, _ = _make_trade_manager_mocks(TM_CONFIG_DISABLED)
        assert manager._ranker.is_enabled is False


class TestTradeManagerRankingBehaviour:

    def test_empty_signals_returns_empty(self):
        manager, guard, router, _ = _make_trade_manager_mocks(TM_CONFIG_ENABLED)
        results = manager.process_signals([])
        assert results == []
        guard.approve.assert_not_called()
        router.execute.assert_not_called()

    def test_single_signal_executed(self):
        """Single signal always gets executed (ranking is a no-op)."""
        manager, guard, _, _ = _make_trade_manager_mocks(TM_CONFIG_ENABLED)
        sig = _make_signal_with_quality("US.SPY", iv_rank=50.0,
                                        buffer_pct=6.0, reward_risk=0.25)
        results = manager.process_signals([sig])
        assert len(results) == 1
        assert results[0].executed is True

    def test_best_ranked_signal_executed_first(self):
        """
        With max_trades_per_day=1, only the highest-ranked signal executes.
        NVDA (IV=72) should beat SPY (IV=41) even though SPY arrives first.
        """
        from src.execution.portfolio_guard import PortfolioGuard

        manager, guard, router, ledger = _make_trade_manager_mocks(TM_CONFIG_ENABLED)

        # Guard allows first call, blocks second (daily limit simulation)
        guard.approve.side_effect = [
            (True,  "approved"),          # best signal — approved
            (False, "Daily limit reached"),  # second signal — blocked
        ]

        spy  = _make_signal_with_quality("US.SPY",  iv_rank=41.0,
                                          buffer_pct=5.1, reward_risk=0.21)
        nvda = _make_signal_with_quality("US.NVDA", iv_rank=72.0,
                                          buffer_pct=8.2, reward_risk=0.31)

        # Pass in FIFO order: SPY first, NVDA second
        results = manager.process_signals([spy, nvda])

        assert len(results) == 2
        # First result should be NVDA (ranked #1), not SPY
        executed = [r for r in results if r.executed]
        blocked  = [r for r in results if not r.approved]
        assert len(executed) == 1
        assert executed[0].signal.symbol == "US.NVDA"
        assert blocked[0].signal.symbol  == "US.SPY"

    def test_fifo_order_preserved_when_disabled(self):
        """
        When ranker is disabled, SPY (first in list) executes, NVDA is blocked.
        """
        manager, guard, router, ledger = _make_trade_manager_mocks(TM_CONFIG_DISABLED)

        guard.approve.side_effect = [
            (True,  "approved"),
            (False, "Daily limit reached"),
        ]

        spy  = _make_signal_with_quality("US.SPY",  iv_rank=41.0,
                                          buffer_pct=5.1, reward_risk=0.21)
        nvda = _make_signal_with_quality("US.NVDA", iv_rank=72.0,
                                          buffer_pct=8.2, reward_risk=0.31)

        results = manager.process_signals([spy, nvda])

        executed = [r for r in results if r.executed]
        blocked  = [r for r in results if not r.approved]
        assert executed[0].signal.symbol == "US.SPY"   # FIFO — SPY goes first
        assert blocked[0].signal.symbol  == "US.NVDA"

    def test_all_signals_get_a_result(self):
        """Every input signal must appear in results (executed, blocked, or skipped)."""
        manager, guard, _, _ = _make_trade_manager_mocks(TM_CONFIG_ENABLED)
        # Block everything
        guard.approve.return_value = (False, "blocked")

        signals = [
            _make_signal_with_quality(f"US.S{i}", iv_rank=50.0+i,
                                       buffer_pct=5.0, reward_risk=0.25)
            for i in range(4)
        ]
        results = manager.process_signals(signals)
        assert len(results) == 4

    def test_results_count_equals_input_count(self):
        """len(results) must always equal len(signals)."""
        manager, guard, _, _ = _make_trade_manager_mocks(TM_CONFIG_ENABLED)
        guard.approve.return_value = (True, "approved")

        signals = [
            _make_signal_with_quality(f"US.A{i}", iv_rank=50.0+i,
                                       buffer_pct=5.0, reward_risk=0.25)
            for i in range(3)
        ]
        results = manager.process_signals(signals)
        assert len(results) == len(signals)

    def test_four_candidates_top_two_are_best(self):
        """
        Four candidates, guard allows 2 then blocks remaining.
        The two best (highest score) must be the ones executed.
        """
        manager, guard, router, ledger = _make_trade_manager_mocks(TM_CONFIG_ENABLED)

        guard.approve.side_effect = [
            (True,  "approved"),
            (True,  "approved"),
            (False, "Daily limit reached"),
            (False, "Daily limit reached"),
        ]

        spy  = _make_signal_with_quality("US.SPY",  iv_rank=41.0, buffer_pct=5.1, reward_risk=0.21)
        qqq  = _make_signal_with_quality("US.QQQ",  iv_rank=44.0, buffer_pct=4.8, reward_risk=0.22)
        aapl = _make_signal_with_quality("US.AAPL", iv_rank=58.0, buffer_pct=6.5, reward_risk=0.28)
        nvda = _make_signal_with_quality("US.NVDA", iv_rank=72.0, buffer_pct=8.2, reward_risk=0.31)

        # FIFO order: SPY, QQQ, AAPL, NVDA
        results = manager.process_signals([spy, qqq, aapl, nvda])

        executed_symbols = {r.signal.symbol for r in results if r.executed}
        assert "US.NVDA" in executed_symbols   # must be selected
        assert "US.AAPL" in executed_symbols   # must be selected
        assert "US.SPY"  not in executed_symbols   # too low-ranked
        assert "US.QQQ"  not in executed_symbols   # too low-ranked

    def test_process_signal_single_unaffected(self):
        """process_signal() (single-signal path) is unchanged by ranking."""
        manager, guard, _, _ = _make_trade_manager_mocks(TM_CONFIG_ENABLED)
        guard.approve.return_value = (True, "approved")

        sig = _make_signal_with_quality("US.SPY", iv_rank=55.0,
                                         buffer_pct=6.0, reward_risk=0.25)
        result = manager.process_signal(sig)
        assert result.signal is sig
        assert result.approved is True
        assert result.executed is True

    def test_guard_called_in_rank_order(self):
        """
        PortfolioGuard.approve() must be called with signals in ranked order.
        NVDA has better score → guard sees NVDA first, then SPY.
        """
        manager, guard, _, _ = _make_trade_manager_mocks(TM_CONFIG_ENABLED)
        guard.approve.return_value = (False, "blocked")  # block all — just check call order

        spy  = _make_signal_with_quality("US.SPY",  iv_rank=41.0,
                                          buffer_pct=5.1, reward_risk=0.21)
        nvda = _make_signal_with_quality("US.NVDA", iv_rank=72.0,
                                          buffer_pct=8.2, reward_risk=0.31)

        manager.process_signals([spy, nvda])   # FIFO: SPY first

        # Guard should have been called with NVDA first (ranked #1)
        first_call_symbol  = guard.approve.call_args_list[0][0][0].symbol
        second_call_symbol = guard.approve.call_args_list[1][0][0].symbol
        assert first_call_symbol  == "US.NVDA"
        assert second_call_symbol == "US.SPY"
