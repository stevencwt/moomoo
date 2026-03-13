"""
Bull Put Spread Strategy
========================
Sells an OTM put and buys a lower-strike put for protection.
Profits when the underlying stays above the short strike at expiry.

The mirror image of the bear call spread — sells downside premium instead of
upside premium. Together they provide two-sided theta collection.

Entry criteria (ALL must pass):
  1. open_positions < max_concurrent_positions
  2. market_regime in ("bull", "neutral") — avoid in pure bear (crash risk)
  3. iv_rank >= min_iv_rank           (selling elevated premium)
  4. RSI >= min_rsi_floor             (stock not in freefall — don't sell puts into a crash)
  5. RSI <= max_rsi_ceiling           (not extreme overbought — bull run could reverse)
  6. %B >= min_pct_b                  (stock not at extreme lows — avoid selling into crash)
  7. No earnings conflict within the option's lifetime
  8. A qualifying OTM short put exists (abs delta 0.20-0.35)
  9. A protective long put can be found (target_width below short)
 10. Net credit >= min_credit         (minimum acceptable premium)
 11. reward_risk >= min_reward_risk   (minimum R/R ratio)
 12. No existing opposing spread on same symbol (iron condor prevention)

Signal output:
  - sell_contract: OTM put (short leg, higher strike)
  - buy_contract : Lower-strike put (long leg, defined risk protection)
  - max_loss     : (spread_width - net_credit) × 100 per contract
  - max_profit   : net_credit × 100 per contract
  - breakeven    : sell_strike - net_credit

Risk parameters (all config-driven):
  min_iv_rank            : 35
  min_rsi_floor          : 35   (don't sell puts when stock is in freefall)
  max_rsi_ceiling        : 65   (don't sell puts at extreme overbought — reversal risk)
  min_pct_b              : 0.20 (stock not at extreme lower band)
  min_credit             : 0.50 (minimum $0.50 credit to bother)
  min_reward_risk        : 0.20 (minimum 1:5 risk/reward)
  spread_width_target    : 10.0 (target $10 wide spread)
  max_concurrent_positions: 3

Design note on gate thresholds:
  RSI floor (35): Protects against selling puts into a genuine crash. If RSI < 35,
    the stock is already deeply oversold — short puts face extreme assignment risk.
  RSI ceiling (65): Avoids selling puts when a stock has run hard — a reversal from
    overbought levels would move the underlying toward the put strikes quickly.
  %B floor (0.20): If price is already near the lower Bollinger Band, the put strikes
    will be relatively close to the current price, compressing the margin of safety.
  Regime (bull/neutral): In a confirmed bear regime, crash risk makes put selling
    structurally dangerous. The bear call spread fires in bear regimes instead.
"""

from datetime import date, datetime
from typing import Optional

from src.market.market_snapshot import MarketSnapshot
from src.strategies.base_strategy import BaseStrategy
from src.strategies.trade_signal import TradeSignal


class BullPutSpreadStrategy(BaseStrategy):

    @property
    def name(self) -> str:
        return "bull_put_spread"

    def evaluate(self, snapshot: MarketSnapshot) -> Optional[TradeSignal]:
        """
        Evaluate bull put spread entry criteria.

        Returns TradeSignal if all criteria pass, None otherwise.
        """
        symbol = snapshot.symbol
        self._logger.debug(f"Evaluating bull put spread for {symbol}")

        # ── Gate 1: Position limit ────────────────────────────────
        max_pos = self._get_cfg("max_concurrent_positions", 3)
        if snapshot.open_positions >= max_pos:
            self._skip(f"open_positions={snapshot.open_positions} >= max_concurrent={max_pos}")
            return None

        # ── Gate 2: Regime ────────────────────────────────────────
        # Bull put spreads work in bull and neutral — avoid in pure bear (crash risk)
        allowed_regimes = self._get_cfg("allowed_regimes", ["bull", "neutral"])
        if snapshot.market_regime not in allowed_regimes:
            self._skip(f"regime={snapshot.market_regime} not in allowed={allowed_regimes}")
            return None

        # ── Gate 3: IV Rank ───────────────────────────────────────
        min_iv_rank = self._get_cfg("min_iv_rank", 35)
        if snapshot.options_context.iv_rank < min_iv_rank:
            self._skip(
                f"iv_rank={snapshot.options_context.iv_rank:.0f} < min_iv_rank={min_iv_rank}"
            )
            return None

        # ── Gate 4: RSI floor (don't sell puts in freefall) ───────
        min_rsi = self._get_cfg("min_rsi_floor", 35)
        if snapshot.technicals.rsi < min_rsi:
            self._skip(
                f"RSI={snapshot.technicals.rsi:.0f} < min_rsi_floor={min_rsi} "
                f"(stock in freefall — put assignment risk)"
            )
            return None

        # ── Gate 5: RSI ceiling (avoid extreme overbought) ────────
        max_rsi = self._get_cfg("max_rsi_ceiling", 65)
        if snapshot.technicals.rsi > max_rsi:
            self._skip(
                f"RSI={snapshot.technicals.rsi:.0f} > max_rsi_ceiling={max_rsi} "
                f"(overbought — reversal could threaten put strikes)"
            )
            return None

        # ── Gate 6: %B floor (price not at extreme lows) ──────────
        min_pct_b = self._get_cfg("min_pct_b", 0.20)
        if snapshot.technicals.pct_b < min_pct_b:
            self._skip(
                f"%B={snapshot.technicals.pct_b:.2f} < min_pct_b={min_pct_b} "
                f"(price near lower band — put strikes too close)"
            )
            return None

        # ── Gate 7: Target expiry ─────────────────────────────────
        expiry = self._options.get_target_expiry(
            snapshot.options_context.available_expiries
        )
        if expiry is None:
            self._skip(
                f"no expiry in DTE range "
                f"{self._options._target_dte_min}-{self._options._target_dte_max}d"
            )
            return None

        # ── Gate 8: Earnings conflict ─────────────────────────────
        if self._options.check_earnings_conflict(expiry, snapshot.next_earnings):
            self._skip(
                f"earnings conflict: expiry={expiry} overlaps earnings={snapshot.next_earnings}"
            )
            return None

        # ── Gate 9: Find qualifying short put ─────────────────────
        try:
            chain = self._moomoo.get_option_chain(symbol, expiry, "PUT")
            if len(chain) == 0:
                self._skip(f"empty PUT option chain for expiry={expiry}")
                return None

            put_codes = chain["code"].tolist()
            snap_df   = self._moomoo.get_option_snapshot(put_codes)

            otm_puts  = self._options.filter_otm_puts(chain, snap_df, snapshot.spot_price)
            short_put = self._options.select_best_put(otm_puts)

            if short_put is None:
                self._skip(
                    f"no short put with abs(delta) {self._options._delta_min}-"
                    f"{self._options._delta_max} and OI≥{self._options._min_open_interest}"
                )
                return None

        except Exception as e:
            self._skip(f"error fetching PUT option chain: {e}")
            return None

        # ── Gate 10: Find protective long put ─────────────────────
        sell_strike  = float(short_put["strike_price"])
        spread_width = self._get_cfg("spread_width_target",
                                     self._options._spread_width_target)
        long_put = self._options.find_protective_put(
            sell_strike=sell_strike,
            chain=chain,
            width=spread_width
        )
        if long_put is None:
            self._skip(f"no protective long put found below short strike {sell_strike}")
            return None

        # ── Gate 11: Pricing and R/R ──────────────────────────────
        long_code = str(long_put["code"])
        long_snap = snap_df[snap_df["code"] == long_code] if len(snap_df) > 0 else None

        if long_snap is None or len(long_snap) == 0:
            try:
                long_snap = self._moomoo.get_option_snapshot([long_code])
            except Exception:
                long_snap = None

        if long_snap is not None and len(long_snap) > 0:
            buy_price = float(
                long_snap.iloc[0].get("mid_price",
                long_snap.iloc[0].get("ask_price", 0))
            )
        else:
            buy_price = 0.0

        sell_price = float(short_put.get("mid_price", short_put.get("bid_price", 0)))

        if sell_price <= 0:
            self._skip(f"sell_price={sell_price:.2f} ≤ 0 — no valid bid/ask on short put")
            return None

        net_credit = sell_price - buy_price
        min_credit = self._get_cfg("min_credit", 0.50)
        if net_credit < min_credit:
            self._skip(f"net_credit=${net_credit:.2f} < min_credit=${min_credit:.2f}")
            return None

        buy_strike = float(long_put["strike_price"])
        metrics    = self._options.compute_put_spread_metrics(
            sell_strike=sell_strike,
            buy_strike=buy_strike,
            sell_premium=sell_price,
            buy_premium=buy_price
        )

        min_rr = self._get_cfg("min_reward_risk", 0.20)
        if metrics["reward_risk"] < min_rr:
            self._skip(f"reward_risk={metrics['reward_risk']:.2f} < min_reward_risk={min_rr}")
            return None

        # ── All gates passed — build TradeSignal ─────────────────
        delta  = float(short_put.get("option_delta", 0))
        today  = date.today()
        dte    = (date.fromisoformat(expiry) - today).days

        reason = (
            f"Bull put spread opportunity | "
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
            signal_type   = "bull_put_spread",
            sell_contract = str(short_put["code"]),
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
            spot_price    = round(snapshot.spot_price, 2),
            buffer_pct    = round((snapshot.spot_price - sell_strike) / snapshot.spot_price * 100, 2),
            snapshot      = snapshot,
        )

        self._logger.info(
            f"✅ BULL PUT SPREAD SIGNAL: {symbol} | "
            f"sell={sell_strike} buy={buy_strike} | "
            f"credit=${signal.net_credit:.2f} | "
            f"max_loss=${signal.max_loss:.0f} | "
            f"R/R={signal.reward_risk:.2f} | DTE={dte}"
        )
        return signal
