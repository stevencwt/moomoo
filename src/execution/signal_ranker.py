"""
SignalRanker
============
Scores and ranks trade signal candidates before execution, ensuring the
daily trade budget is allocated to the best available opportunities across
the entire watchlist rather than the first qualifying ones encountered.

Design
------
The ranker is a **pure transformation** — it takes a list of TradeSignals,
assigns each a composite score, and returns them sorted best-first.  It has
no side effects and does not call the broker, ledger, or portfolio guard.

Scoring formula
---------------
Each signal is scored on three dimensions, each min-max normalised to [0, 1]
within the current candidate pool before weighting:

    score = (iv_rank_norm      × weight_iv_rank)
          + (buffer_pct_norm   × weight_buffer_pct)
          + (reward_risk_norm  × weight_reward_risk)

Weights are read from config and do NOT need to sum to 1.0.  Only their
relative magnitudes matter (multiplying all by 2 produces identical rankings).

Normalisation
-------------
    norm(x) = (x − min_in_pool) / (max_in_pool − min_in_pool)

Edge cases:
  - Single candidate: all norms = 1.0, score = sum of weights (ranking is a no-op)
  - Constant dimension: all norms = 0.0 for that dimension (no differentiation)
  - missing buffer_pct on a signal (None): treated as 0.0 before normalisation

Buffer direction
----------------
Computed by each strategy and stored in TradeSignal.buffer_pct:
  Bear call spread : (short_call_strike − spot) / spot × 100
  Bull put spread  : (spot − short_put_strike)  / spot × 100
  Covered call     : (short_call_strike − spot) / spot × 100

Configuration (under signal_ranker in config.yaml)
--------------------------------------------------
    signal_ranker:
      enabled:           true   # false = FIFO behaviour (ranker is a no-op)
      weight_iv_rank:    0.40
      weight_buffer_pct: 0.35
      weight_reward_risk: 0.25

Disabled mode
-------------
When enabled=false, rank() returns signals in their original order with
rank=1 for all and score=0.0.  The rest of the pipeline sees no change.
"""

from dataclasses import dataclass
from typing import List, Optional

from src.strategies.trade_signal import TradeSignal
from src.logger import get_logger

logger = get_logger("execution.signal_ranker")


# ── RankedSignal ──────────────────────────────────────────────────

@dataclass
class RankedSignal:
    """
    A TradeSignal decorated with its composite score and rank position.

    Fields
    ------
    signal          : The original TradeSignal (unchanged)
    rank            : 1-based position in the sorted candidate pool (1 = best)
    score           : Composite weighted score (higher = better)
    score_breakdown : Per-dimension contributions for logging/display
                      {"iv_rank": float, "buffer_pct": float, "reward_risk": float}
    iv_rank_norm    : Normalised IV Rank value (0–1) used in scoring
    buffer_pct_norm : Normalised buffer % value (0–1) used in scoring
    reward_risk_norm: Normalised reward/risk value (0–1) used in scoring
    """
    signal:           TradeSignal
    rank:             int
    score:            float
    score_breakdown:  dict        # {"iv_rank": float, "buffer_pct": float, "reward_risk": float}
    iv_rank_norm:     float
    buffer_pct_norm:  float
    reward_risk_norm: float


# ── SignalRanker ──────────────────────────────────────────────────

class SignalRanker:
    """
    Scores and ranks a pool of TradeSignal candidates.

    Usage
    -----
        ranker = SignalRanker(config)
        ranked = ranker.rank(signals)       # List[RankedSignal], best first
        top_n  = ranker.top_n(signals, 2)   # Best 2 signals as TradeSignals
    """

    # Default weights — used when config block is missing
    _DEFAULT_WEIGHT_IV_RANK     = 0.40
    _DEFAULT_WEIGHT_BUFFER_PCT  = 0.35
    _DEFAULT_WEIGHT_REWARD_RISK = 0.25

    def __init__(self, config: dict):
        cfg = config.get("signal_ranker", {})

        self._enabled            = cfg.get("enabled", True)
        self._weight_iv_rank     = cfg.get("weight_iv_rank",     self._DEFAULT_WEIGHT_IV_RANK)
        self._weight_buffer_pct  = cfg.get("weight_buffer_pct",  self._DEFAULT_WEIGHT_BUFFER_PCT)
        self._weight_reward_risk = cfg.get("weight_reward_risk", self._DEFAULT_WEIGHT_REWARD_RISK)

        logger.info(
            f"SignalRanker initialised | "
            f"enabled={self._enabled} | "
            f"weights: iv_rank={self._weight_iv_rank} "
            f"buffer={self._weight_buffer_pct} "
            f"rr={self._weight_reward_risk}"
        )

    # ── Public API ────────────────────────────────────────────────

    @property
    def is_enabled(self) -> bool:
        """True if ranking is active; False = FIFO passthrough."""
        return self._enabled

    def rank(self, signals: List[TradeSignal]) -> List[RankedSignal]:
        """
        Score and rank a pool of candidate signals.

        When disabled, returns signals in original order with rank=1 and
        score=0.0 so callers need no special handling for the disabled case.

        Args:
            signals: Any list of TradeSignals (may be empty)

        Returns:
            List of RankedSignal sorted descending by score (best first).
            Empty input → empty output.
        """
        if not signals:
            return []

        if not self._enabled:
            # Passthrough — preserve original order, mark all rank=1
            logger.debug(f"SignalRanker disabled — returning {len(signals)} signals in FIFO order")
            return [
                RankedSignal(
                    signal=s,
                    rank=1,
                    score=0.0,
                    score_breakdown={"iv_rank": 0.0, "buffer_pct": 0.0, "reward_risk": 0.0},
                    iv_rank_norm=0.0,
                    buffer_pct_norm=0.0,
                    reward_risk_norm=0.0,
                )
                for s in signals
            ]

        return self._rank_enabled(signals)

    def top_n(self, signals: List[TradeSignal], n: int) -> List[TradeSignal]:
        """
        Return the top-N TradeSignals from the pool (ranked best-first).

        Convenience wrapper around rank() — strips RankedSignal wrappers.
        Returns at most min(n, len(signals)) results.

        Args:
            signals: Candidate pool
            n:       Maximum number of signals to return

        Returns:
            List of up to N TradeSignals, best-ranked first.
        """
        ranked = self.rank(signals)
        return [r.signal for r in ranked[:n]]

    # ── Internal helpers ──────────────────────────────────────────

    def _rank_enabled(self, signals: List[TradeSignal]) -> List[RankedSignal]:
        """Core ranking logic — only called when enabled=True."""

        # Step 1: Extract raw dimension values for each signal
        iv_ranks     = [s.iv_rank                             for s in signals]
        buffer_pcts  = [self._safe_buffer(s)                  for s in signals]
        reward_risks = [self._safe_reward_risk(s)             for s in signals]

        # Step 2: Normalise each dimension to [0, 1] across the candidate pool
        iv_norms   = self._normalise(iv_ranks)
        buf_norms  = self._normalise(buffer_pcts)
        rr_norms   = self._normalise(reward_risks)

        # Step 3: Compute composite score for each signal
        ranked = []
        for i, signal in enumerate(signals):
            iv_contrib  = iv_norms[i]  * self._weight_iv_rank
            buf_contrib = buf_norms[i] * self._weight_buffer_pct
            rr_contrib  = rr_norms[i]  * self._weight_reward_risk

            score = round(iv_contrib + buf_contrib + rr_contrib, 4)

            ranked.append(RankedSignal(
                signal=signal,
                rank=0,           # assigned after sort
                score=score,
                score_breakdown={
                    "iv_rank":     round(iv_contrib,  4),
                    "buffer_pct":  round(buf_contrib, 4),
                    "reward_risk": round(rr_contrib,  4),
                },
                iv_rank_norm=round(iv_norms[i],  4),
                buffer_pct_norm=round(buf_norms[i], 4),
                reward_risk_norm=round(rr_norms[i], 4),
            ))

        # Step 4: Sort descending by score; use original index as tiebreaker
        #         (preserves watchlist order for equal-score candidates)
        ranked.sort(key=lambda r: (-r.score, signals.index(r.signal)))

        # Step 5: Assign 1-based rank positions
        for position, ranked_signal in enumerate(ranked, start=1):
            # RankedSignal is a dataclass (not frozen) — can mutate rank field
            ranked_signal.rank = position

        # Log summary
        self._log_ranking(ranked)

        return ranked

    @staticmethod
    def _normalise(values: List[float]) -> List[float]:
        """
        Min-max normalise a list of floats to [0, 1].

        If all values are identical (zero range), returns all 0.0 — that
        dimension cannot differentiate candidates and contributes equally
        (zero contribution per the multiplication with its norm value).
        """
        if not values:
            return []
        min_v = min(values)
        max_v = max(values)
        rng   = max_v - min_v

        if rng == 0.0:
            # All candidates have the same value — dimension is non-discriminating
            return [0.0] * len(values)

        return [(v - min_v) / rng for v in values]

    @staticmethod
    def _safe_buffer(signal: TradeSignal) -> float:
        """
        Return buffer_pct from the signal, defaulting to 0.0 if not set.

        buffer_pct is an optional field added in Phase 2.  Fallback to 0.0
        means older signals (no buffer_pct) are ranked at the minimum on
        this dimension — they won't crash the ranker but will rank lower.
        """
        val = getattr(signal, "buffer_pct", None)
        return float(val) if val is not None else 0.0

    @staticmethod
    def _safe_reward_risk(signal: TradeSignal) -> float:
        """
        Return reward_risk from the signal, defaulting to 0.0 if not set.

        Covered calls may have reward_risk=None (undefined risk positions).
        Default to 0.0 so they sort last on this dimension.
        """
        val = signal.reward_risk
        return float(val) if val is not None else 0.0

    def _log_ranking(self, ranked: List[RankedSignal]) -> None:
        """Log the full ranked table at INFO level."""
        if not ranked:
            return

        logger.info(
            f"SignalRanker: {len(ranked)} candidate(s) ranked | "
            f"weights: iv={self._weight_iv_rank} "
            f"buf={self._weight_buffer_pct} "
            f"rr={self._weight_reward_risk}"
        )
        for r in ranked:
            logger.info(
                f"  #{r.rank:>2}  {r.signal.symbol:<10} "
                f"{r.signal.strategy_name:<22} "
                f"iv={r.signal.iv_rank:>5.1f} ({r.iv_rank_norm:.2f})  "
                f"buf={self._safe_buffer(r.signal):>5.1f}% ({r.buffer_pct_norm:.2f})  "
                f"rr={self._safe_reward_risk(r.signal):.3f} ({r.reward_risk_norm:.2f})  "
                f"→ score={r.score:.4f}"
            )
