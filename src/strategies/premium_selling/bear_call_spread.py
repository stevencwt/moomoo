"""
Bear Call Spread Strategy
=========================
Sells an OTM call and buys a higher-strike call for protection.
Profits when the underlying stays below the short strike at expiry.

Entry criteria (ALL must pass):
  1. open_positions < max_concurrent_positions
  2. market_regime in ("bear", "neutral") — not ideal in pure bull
  3. iv_rank >= min_iv_rank       (selling elevated premium)
  4. RSI >= min_rsi_for_spread    (stock at/above neutral — not in freefall)
  5. %B >= min_pct_b              (price near or above midband — OTM call has value)
  6. No earnings conflict within the option's lifetime
  7. A qualifying OTM short call exists (delta 0.20-0.35)
  8. A protective long call can be found (target_width above short)
  9. Net credit >= min_credit     (minimum acceptable premium)
 10. reward_risk >= min_reward_risk (minimum R/R ratio)

Signal output:
  - sell_contract: OTM call (short leg)
  - buy_contract : Higher-strike call (long leg, defined risk)
  - max_loss     : (spread_width - net_credit) × 100 per contract
  - max_profit   : net_credit × 100 per contract
  - breakeven    : sell_strike + net_credit

Risk parameters (all config-driven):
  min_iv_rank            : 35
  min_rsi_for_spread     : 45   (don't sell calls when stock is in freefall)
  min_pct_b              : 0.40 (price at least at midband)
  min_credit             : 0.50 (minimum $0.50 credit to bother)
  min_reward_risk        : 0.20 (minimum 1:5 risk/reward)
  spread_width_target    : 10.0 (target $10 wide spread)
  max_concurrent_positions: 3
"""

from datetime import date, datetime
from typing import Optional

from src.market.market_snapshot import MarketSnapshot
from src.strategies.base_strategy import BaseStrategy
from src.strategies.trade_signal import TradeSignal


class BearCallSpreadStrategy(BaseStrategy):

    @property
    def name(self) -> str:
        return "bear_call_spread"

    def evaluate(self, snapshot: MarketSnapshot) -> Optional[TradeSignal]:
        """
        Evaluate bear call spread entry criteria.

        Returns TradeSignal if all criteria pass, None otherwise.
        """
        symbol = snapshot.symbol
        self._logger.debug(f"Evaluating bear call spread for {symbol}")

        # ── Gate 1: Position limit ────────────────────────────────
        max_pos = self._get_cfg("max_concurrent_positions", 3)
        if snapshot.open_positions >= max_pos:
            self._skip(f"open_positions={snapshot.open_positions} >= max_concurrent={max_pos}")
            return None

        # ── Gate 2: Regime ────────────────────────────────────────
        # Bear call spreads work in bear and neutral — avoid in pure bull (breakout risk)
        allowed_regimes = self._get_cfg("allowed_regimes", ["bear", "neutral"])
        if snapshot.market_regime not in allowed_regimes:
            self._skip(f"regime={snapshot.market_regime} not in allowed={allowed_regimes}")
            return None

        # ── Gate 3: IV Rank ───────────────────────────────────────
        min_iv_rank = self._get_cfg("min_iv_rank", 35)
        if snapshot.options_context.iv_rank < min_iv_rank:
            self._skip(f"iv_rank={snapshot.options_context.iv_rank:.0f} < min_iv_rank={min_iv_rank}")
            return None

        # ── Gate 4: RSI floor (don't sell calls in freefall) ──────
        min_rsi = self._get_cfg("min_rsi_for_spread", 45)
        if snapshot.technicals.rsi < min_rsi:
            self._skip(f"RSI={snapshot.technicals.rsi:.0f} < min_rsi={min_rsi} (stock in freefall)")
            return None

        # ── Gate 5: %B floor (price needs some elevation for OTM calls to have value) ─
        min_pct_b = self._get_cfg("min_pct_b", 0.40)
        if snapshot.technicals.pct_b < min_pct_b:
            self._skip(f"%B={snapshot.technicals.pct_b:.2f} < min_pct_b={min_pct_b}")
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

        # ── Gate 8: Find qualifying short call ───────────────────
        try:
            chain = self._moomoo.get_option_chain(symbol, expiry, "CALL")
            if len(chain) == 0:
                self._skip(f"empty option chain for expiry={expiry}")
                return None

            call_codes = chain["code"].tolist()
            snap_df    = self._moomoo.get_option_snapshot(call_codes)

            otm_calls  = self._options.filter_otm_calls(chain, snap_df, snapshot.spot_price)
            short_call = self._options.select_best_call(otm_calls)

            if short_call is None:
                self._skip(f"no short call with delta {self._options._delta_min}-{self._options._delta_max} and OI≥{self._options._min_oi}")
                return None

        except Exception as e:
            self._skip(f"error fetching option chain: {e}")
            return None

        # ── Gate 9: Find protective long call ────────────────────
        sell_strike   = float(short_call["strike_price"])
        spread_width  = self._get_cfg("spread_width_target",
                                      self._options._spread_width_target)
        long_call = self._options.find_protective_call(
            sell_strike=sell_strike,
            chain=chain,
            width=spread_width
        )
        if long_call is None:
            self._skip(f"no protective long call found above short strike {sell_strike}")
            return None

        # ── Gate 10: Pricing and R/R ──────────────────────────────
        # Get snapshot data for long call if not already in snap_df
        long_code = str(long_call["code"])
        long_snap = snap_df[snap_df["code"] == long_code] if len(snap_df) > 0 else None

        if long_snap is None or len(long_snap) == 0:
            try:
                long_snap = self._moomoo.get_option_snapshot([long_code])
            except Exception:
                long_snap = None

        # Determine buy price
        if long_snap is not None and len(long_snap) > 0:
            buy_price = float(
                long_snap.iloc[0].get("mid_price",
                long_snap.iloc[0].get("ask_price", 0))
            )
        else:
            buy_price = 0.0

        sell_price = float(short_call.get("mid_price", short_call.get("bid_price", 0)))

        if sell_price <= 0:
            self._skip(f"sell_price={sell_price:.2f} ≤ 0 — no valid bid/ask")
            return None

        # Minimum credit check
        net_credit = sell_price - buy_price
        min_credit = self._get_cfg("min_credit", 0.50)
        if net_credit < min_credit:
            self._skip(f"net_credit=${net_credit:.2f} < min_credit=${min_credit:.2f}")
            return None

        # Compute metrics
        buy_strike = float(long_call["strike_price"])
        metrics    = self._options.compute_spread_metrics(
            sell_strike=sell_strike,
            buy_strike=buy_strike,
            sell_premium=sell_price,
            buy_premium=buy_price
        )

        # Minimum R/R check
        min_rr = self._get_cfg("min_reward_risk", 0.20)
        if metrics["reward_risk"] < min_rr:
            self._skip(f"reward_risk={metrics['reward_risk']:.2f} < min_reward_risk={min_rr}")
            return None

        # ── All gates passed — build TradeSignal ─────────────────
        delta  = float(short_call.get("option_delta", 0))
        today  = date.today()
        dte    = (date.fromisoformat(expiry) - today).days

        reason = (
            f"Bear call spread opportunity | "
            f"regime={snapshot.market_regime} | "
            f"IV_rank={snapshot.options_context.iv_rank:.0f} | "
            f"RSI={snapshot.technicals.rsi:.0f} | "
            f"%B={snapshot.technicals.pct_b:.2f} | "
            f"sell={sell_strike}/buy={buy_strike} | "
            f"delta={delta:.2f} | net_credit=${net_credit:.2f} | "
            f"max_loss=${metrics['max_loss']:.0f} | "
            f"R/R={metrics['reward_risk']:.2f} | DTE={dte}"
        )

        signal = TradeSignal(
            strategy_name = self.name,
            symbol        = symbol,
            timestamp     = datetime.now(),
            action        = "OPEN",
            signal_type   = "bear_call_spread",
            sell_contract = str(short_call["code"]),
            buy_contract  = long_code,
            quantity      = 1,
            sell_price    = round(sell_price, 2),
            buy_price     = round(buy_price, 2),
            net_credit    = round(metrics["net_credit"], 2),
            max_profit    = metrics["max_profit"],
            max_loss      = metrics["max_loss"],
            breakeven     = metrics["breakeven"],
            reward_risk   = metrics["reward_risk"],
            expiry        = expiry,
            dte           = dte,
            iv_rank       = snapshot.options_context.iv_rank,
            delta         = delta,
            reason        = reason,
            regime        = snapshot.market_regime,
        )

        self._logger.info(
            f"✅ BEAR CALL SPREAD SIGNAL: {symbol} | "
            f"sell={sell_strike} buy={buy_strike} | "
            f"credit=${signal.net_credit:.2f} | "
            f"max_loss=${signal.max_loss:.0f} | "
            f"R/R={signal.reward_risk:.2f} | DTE={dte}"
        )
        return signal
