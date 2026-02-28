"""
Covered Call Strategy
=====================
Sells an OTM call against 100 shares of underlying already held.

Entry criteria (ALL must pass):
  1. shares_held >= 100 in the stock account
  2. open_positions < max_concurrent_positions
  3. market_regime != "high_vol"
  4. iv_rank >= min_iv_rank  (selling premium when IV is elevated)
  5. RSI <= max_rsi           (avoid selling calls at extreme overbought — breakout risk)
  6. No earnings conflict within the option's lifetime
  7. A qualifying OTM call exists (delta 0.20-0.35, OI >= 100)

Signal output:
  - sell_contract: OTM call with highest delta in range
  - buy_contract : None (shares provide coverage)
  - max_loss     : None (shares absorb downside, but upside is capped)
  - breakeven    : spot_price - net_credit (lower bound)

Risk parameters (all config-driven):
  min_iv_rank               : 30  (don't sell cheap premium)
  max_rsi                   : 70  (avoid if already extremely overbought)
  target_dte_min/max        : 21-45 days
  otm_call_delta_min/max    : 0.20-0.35
  max_concurrent_positions  : 2   (max covered calls at once)
  quantity                  : 1   (1 contract = 100 shares covered)
"""

from datetime import date, datetime
from typing import Optional

from src.market.market_snapshot import MarketSnapshot
from src.strategies.base_strategy import BaseStrategy
from src.strategies.trade_signal import TradeSignal
from src.logger import get_logger


class CoveredCallStrategy(BaseStrategy):

    @property
    def name(self) -> str:
        return "covered_call"

    def evaluate(self, snapshot: MarketSnapshot) -> Optional[TradeSignal]:
        """
        Evaluate covered call entry criteria.

        Returns TradeSignal if all criteria pass, None otherwise.
        Each rejection is logged with the specific reason.
        """
        symbol = snapshot.symbol
        self._logger.debug(f"Evaluating covered call for {symbol}")

        # ── Gate 1: Shares held ───────────────────────────────────
        if snapshot.shares_held < 100:
            self._skip(f"shares_held={snapshot.shares_held} < 100")
            return None

        # ── Gate 2: Position limit ────────────────────────────────
        max_pos = self._get_cfg("max_concurrent_positions", 2)
        if snapshot.open_positions >= max_pos:
            self._skip(f"open_positions={snapshot.open_positions} >= max_concurrent={max_pos}")
            return None

        # ── Gate 3: Regime ────────────────────────────────────────
        if snapshot.market_regime == "high_vol":
            self._skip("regime=high_vol — no new trades during high volatility")
            return None

        # ── Gate 4: IV Rank ───────────────────────────────────────
        min_iv_rank = self._get_cfg("min_iv_rank", 30)
        if snapshot.options_context.iv_rank < min_iv_rank:
            self._skip(f"iv_rank={snapshot.options_context.iv_rank:.0f} < min_iv_rank={min_iv_rank}")
            return None

        # ── Gate 5: RSI cap (avoid extreme overbought breakouts) ──
        max_rsi = self._get_cfg("max_rsi", 70)
        if snapshot.technicals.rsi > max_rsi:
            self._skip(f"RSI={snapshot.technicals.rsi:.0f} > max_rsi={max_rsi}")
            return None

        # ── Gate 6: Target expiry ─────────────────────────────────
        expiry = self._options.get_target_expiry(
            snapshot.options_context.available_expiries
        )
        if expiry is None:
            self._skip(f"no expiry in DTE range {self._options._dte_min}-{self._options._dte_max}d")
            return None

        # ── Gate 7: Earnings conflict ─────────────────────────────
        if self._options.check_earnings_conflict(expiry, snapshot.next_earnings):
            self._skip(f"earnings conflict: expiry={expiry} overlaps earnings={snapshot.next_earnings}")
            return None

        # ── Gate 8: Find qualifying OTM call ─────────────────────
        try:
            chain = self._moomoo.get_option_chain(symbol, expiry, "CALL")
            if len(chain) == 0:
                self._skip(f"empty option chain for expiry={expiry}")
                return None

            call_codes = chain["code"].tolist()
            snap_df    = self._moomoo.get_option_snapshot(call_codes)

            otm_calls  = self._options.filter_otm_calls(chain, snap_df, snapshot.spot_price)
            best_call  = self._options.select_best_call(otm_calls)

            if best_call is None:
                self._skip(f"no OTM call with delta {self._options._delta_min}-{self._options._delta_max} and OI≥{self._options._min_oi}")
                return None

        except Exception as e:
            self._skip(f"error fetching option chain: {e}")
            return None

        # ── All gates passed — build TradeSignal ─────────────────
        sell_price = float(best_call.get("mid_price", best_call.get("ask_price", 0)))
        if sell_price <= 0:
            self._skip(f"sell_price={sell_price:.2f} ≤ 0 — no valid mid/ask price")
            return None

        delta    = float(best_call.get("option_delta", 0))
        strike   = float(best_call["strike_price"])
        today    = date.today()
        expiry_d = date.fromisoformat(expiry)
        dte      = (expiry_d - today).days

        reason = (
            f"Covered call opportunity | "
            f"regime={snapshot.market_regime} | "
            f"IV_rank={snapshot.options_context.iv_rank:.0f} | "
            f"RSI={snapshot.technicals.rsi:.0f} | "
            f"%B={snapshot.technicals.pct_b:.2f} | "
            f"strike={strike} | delta={delta:.2f} | "
            f"DTE={dte} | credit=${sell_price:.2f}"
        )

        signal = TradeSignal(
            strategy_name = self.name,
            symbol        = symbol,
            timestamp     = datetime.now(),
            action        = "OPEN",
            signal_type   = "covered_call",
            sell_contract = str(best_call["code"]),
            buy_contract  = None,
            quantity      = 1,
            sell_price    = round(sell_price, 2),
            buy_price     = None,
            net_credit    = round(sell_price, 2),
            max_profit    = round(sell_price * 100, 2),
            max_loss      = None,   # shares absorb downside; upside is capped at strike
            breakeven     = round(snapshot.spot_price - sell_price, 2),
            reward_risk   = None,
            expiry        = expiry,
            dte           = dte,
            iv_rank       = snapshot.options_context.iv_rank,
            delta         = delta,
            reason        = reason,
            regime        = snapshot.market_regime,
        )

        self._logger.info(
            f"✅ COVERED CALL SIGNAL: {symbol} | "
            f"sell={signal.sell_contract} | "
            f"credit=${signal.net_credit:.2f} | DTE={dte}"
        )
        return signal
