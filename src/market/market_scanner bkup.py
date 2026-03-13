"""
Market Scanner
==============
Orchestrates the full data pipeline to produce a MarketSnapshot for each symbol.

Pipeline per symbol:
  1. Fetch daily OHLCV (yfinance)
  2. Compute technical indicators (TechnicalAnalyser)
  3. Fetch current VIX (yfinance)
  4. Detect market regime (RegimeDetector)
  5. Fetch upcoming option expiries (MooMoo)
  6. Compute IV Rank (IVRankCalculator)
  7. Check earnings dates (yfinance)
  8. Query shares held (MooMoo stock account)
  9. Query open option positions (MooMoo options account)
  10. Assemble MarketSnapshot

The scanner is strategy-agnostic. It does not know which strategies exist.
It simply produces a complete, frozen MarketSnapshot for each symbol.
Strategies then evaluate the snapshot independently.
"""

from datetime import datetime, date
from typing import List, Optional, Dict

from src.market.market_snapshot import MarketSnapshot, Technicals, OptionsContext
from src.market.technical_analyser import TechnicalAnalyser
from src.market.options_analyser import OptionsAnalyser
from src.market.iv_rank_calculator import IVRankCalculator
from src.market.regime_detector import RegimeDetector
from src.connectors.connector_protocol import BrokerConnector
from src.connectors.yfinance_connector import YFinanceConnector
from src.exceptions import DataError
from src.logger import get_logger

logger = get_logger("market.market_scanner")


class MarketScanner:
    """
    Scans the configured watchlist and produces MarketSnapshot objects.
    """

    def __init__(
        self,
        config:      Dict,
        moomoo:      BrokerConnector,
        yfinance:    YFinanceConnector,
        tech:        TechnicalAnalyser,
        options:     OptionsAnalyser,
        iv_rank:     IVRankCalculator,
        regime:      RegimeDetector,
    ):
        self._config   = config
        self._moomoo   = moomoo
        self._yfinance = yfinance
        self._tech     = tech
        self._options  = options
        self._iv_rank  = iv_rank
        self._regime   = regime

        self._watchlist: List[str] = config.get("universe", {}).get(
            "watchlist", ["US.TSLA"]
        )
        logger.info(f"MarketScanner initialised | watchlist={self._watchlist}")

    def scan_universe(self) -> List[MarketSnapshot]:
        """
        Scan all symbols in the watchlist.

        Symbols that fail to scan are logged and skipped — one bad symbol
        should never prevent the rest from being scanned.

        Returns:
            List of MarketSnapshot objects (one per successfully scanned symbol).
        """
        snapshots = []
        logger.info(f"Scanning {len(self._watchlist)} symbols...")

        for symbol in self._watchlist:
            try:
                snap = self.scan_symbol(symbol)
                snapshots.append(snap)
                logger.info(
                    f"✅ {symbol} | price={snap.spot_price:.2f} | "
                    f"regime={snap.market_regime} | "
                    f"IV_rank={snap.options_context.iv_rank:.0f} | "
                    f"%B={snap.technicals.pct_b:.2f} | "
                    f"RSI={snap.technicals.rsi:.0f}"
                )
            except Exception as e:
                logger.error(f"❌ Failed to scan {symbol}: {e}", exc_info=True)

        logger.info(f"Scan complete: {len(snapshots)}/{len(self._watchlist)} symbols")
        return snapshots

    def scan_symbol(self, symbol: str) -> MarketSnapshot:
        """
        Run full data pipeline for a single symbol and return MarketSnapshot.

        Args:
            symbol: MooMoo format e.g. "US.TSLA"

        Returns:
            Fully populated MarketSnapshot.

        Raises:
            DataError: If critical data is unavailable (OHLCV, spot price).
        """
        logger.debug(f"Scanning {symbol}...")
        now = datetime.now()

        # ── Step 1: OHLCV + Technicals ────────────────────────────
        ohlcv = self._yfinance.get_daily_ohlcv(symbol, period="6mo")
        if len(ohlcv) == 0:
            raise DataError(f"No OHLCV data for {symbol}")

        spot_price = float(ohlcv["close"].iloc[-1])
        df_with_indicators = self._tech.compute_all(ohlcv)
        technicals = self._tech.extract_latest(df_with_indicators)

        # ── Step 2: VIX + Regime ──────────────────────────────────
        vix = self._yfinance.get_current_vix()
        market_regime = self._regime.detect(technicals, vix)

        # ── Step 3: Option Expiries ───────────────────────────────
        available_expiries = self._moomoo.get_option_expiries(symbol)

        # ── Step 4: ATM IV + IV Rank ─────────────────────────────
        atm_iv, iv_rank = self._get_iv_data(symbol, available_expiries, spot_price)

        # ── Step 5: Earnings ──────────────────────────────────────
        next_earnings    = None
        days_to_earnings = None

        earnings_dates = self._yfinance.get_earnings_dates(symbol)
        if earnings_dates:
            next_earnings    = earnings_dates[0]
            days_to_earnings = (next_earnings - date.today()).days

        # ── Step 6: Shares Held ───────────────────────────────────
        # In paper mode, config can override shares_held per symbol so that
        # covered call strategy can fire against real shares you hold.
        # Set universe.shares_held.US.TSLA: 100 in config.yaml
        shares_config = (
            self._config.get("universe", {})
                        .get("shares_held", {})
        )
        if symbol in shares_config:
            shares_held = int(shares_config[symbol])
            logger.debug(
                f"{symbol}: shares_held overridden from config: {shares_held}"
            )
        else:
            shares_held = self._moomoo.get_shares_held(symbol)

        # ── Step 7: Open Positions ────────────────────────────────
        all_positions = self._moomoo.get_option_positions()
        # Count positions for this symbol
        open_positions = 0
        if len(all_positions) > 0 and "code" in all_positions.columns:
            ticker = symbol.replace("US.", "").replace("HK.", "")
            open_positions = len(
                all_positions[all_positions["code"].str.contains(ticker, na=False)]
            )

        # ── Assemble Snapshot ─────────────────────────────────────
        snapshot = MarketSnapshot(
            symbol=          symbol,
            timestamp=       now,
            spot_price=      spot_price,
            technicals=      technicals,
            vix=             vix,
            market_regime=   market_regime,
            options_context= OptionsContext(
                iv_rank=           iv_rank,
                atm_iv=            atm_iv,
                available_expiries= available_expiries,
            ),
            next_earnings=    next_earnings,
            days_to_earnings= days_to_earnings,
            shares_held=      shares_held,
            open_positions=   open_positions,
        )

        return snapshot


    def scan_symbol_intraday(
        self,
        symbol: str,
        morning_snapshot: "MarketSnapshot",
    ) -> "MarketSnapshot":
        """
        Lightweight intraday rescan — reuses morning's daily-bar technicals
        and regime, but fetches fresh spot price, VIX, IV rank, and
        open-position count.

        This is called every 30 min by the monitor job so that option-chain
        gates (IV rank, credit, delta, R/R) are evaluated against current
        market pricing rather than just the 09:35 snapshot.

        Gates that are FIXED (computed from daily bars — won't change today):
          - RSI, MACD, %B, Bollinger Bands → reused from morning_snapshot
          - Market regime                  → reused from morning_snapshot
          - Earnings buffer                → reused from morning_snapshot

        Gates that are REFRESHED every 30 min:
          - Spot price                     → live via yfinance fast_info
          - VIX                            → live via yfinance
          - ATM IV + IV Rank               → live MooMoo snapshot
          - open_positions count           → live ledger query
          - shares_held                    → live or config override

        Args:
            symbol           : MooMoo format e.g. "US.TSLA"
            morning_snapshot : Snapshot produced by scan_symbol() at 09:35

        Returns:
            New MarketSnapshot with refreshed pricing data.

        Raises:
            DataError: If critical live data (spot price) is unavailable.
        """
        logger.debug(f"Intraday rescan: {symbol}")
        now = datetime.now()

        # ── Fresh spot price ──────────────────────────────────────
        spot_price = self._yfinance.get_current_price(symbol)

        # ── Fresh VIX ─────────────────────────────────────────────
        try:
            vix = self._yfinance.get_current_vix()
        except Exception:
            vix = morning_snapshot.vix

        # ── Fresh IV + IV Rank ────────────────────────────────────
        try:
            atm_iv, iv_rank = self._get_iv_data(
                symbol,
                morning_snapshot.options_context.available_expiries,
                spot_price,
            )
        except Exception:
            atm_iv  = morning_snapshot.options_context.atm_iv
            iv_rank = morning_snapshot.options_context.iv_rank

        # ── Fresh open-position count ─────────────────────────────
        try:
            all_positions = self._moomoo.get_option_positions()
            if len(all_positions) > 0 and "code" in all_positions.columns:
                ticker = symbol.replace("US.", "").replace("HK.", "")
                open_positions = len(
                    all_positions[
                        all_positions["code"].str.contains(ticker, na=False)
                    ]
                )
            else:
                open_positions = 0
        except Exception:
            open_positions = morning_snapshot.open_positions

        # ── Shares held (config override or live) ────────────────
        shares_config = (
            self._config.get("universe", {}).get("shares_held", {})
        )
        if symbol in shares_config:
            shares_held = int(shares_config[symbol])
        else:
            try:
                shares_held = self._moomoo.get_shares_held(symbol)
            except Exception:
                shares_held = morning_snapshot.shares_held

        # ── Re-evaluate regime with fresh VIX + same daily techs ─
        market_regime = self._regime.detect(morning_snapshot.technicals, vix)

        return MarketSnapshot(
            symbol=           symbol,
            timestamp=        now,
            spot_price=       spot_price,
            technicals=       morning_snapshot.technicals,
            vix=              vix,
            market_regime=    market_regime,
            options_context=  OptionsContext(
                iv_rank=            iv_rank,
                atm_iv=             atm_iv,
                available_expiries= morning_snapshot.options_context.available_expiries,
            ),
            next_earnings=    morning_snapshot.next_earnings,
            days_to_earnings= morning_snapshot.days_to_earnings,
            shares_held=      shares_held,
            open_positions=   open_positions,
        )

    # ── Private Helpers ───────────────────────────────────────────

    def _get_iv_data(
        self,
        symbol: str,
        available_expiries: List[str],
        spot_price: float
    ) -> tuple:
        """
        Fetch ATM IV for the nearest expiry and compute IV Rank.

        Returns:
            Tuple of (atm_iv, iv_rank) — both floats.
            Returns (0.0, 50.0) if IV data cannot be fetched.
        """
        if not available_expiries:
            logger.warning(f"{symbol}: No expiries available for IV fetch")
            return 0.0, 50.0

        try:
            # Use nearest expiry for ATM IV
            nearest_expiry = available_expiries[0]
            chain = self._moomoo.get_option_chain(symbol, nearest_expiry, "CALL")

            # Find ATM strike (nearest to spot)
            chain["strike_dist"] = (chain["strike_price"] - spot_price).abs()
            atm_row = chain.nsmallest(1, "strike_dist").iloc[0]
            atm_contract = atm_row["code"]

            # Get snapshot for ATM contract
            snap = self._moomoo.get_option_snapshot([atm_contract])
            if len(snap) == 0:
                return 0.0, 50.0

            atm_iv = float(snap.iloc[0].get("option_iv", 0))

            if atm_iv <= 0:
                logger.warning(
                    f"{symbol}: ATM IV is 0 — market likely closed. "
                    f"Greeks unavailable outside market hours."
                )
                atm_iv = 0.0

            # Compute IV rank using stored history
            vix = self._yfinance.get_current_vix()
            iv_rank, quality = self._iv_rank.get_iv_rank(symbol, atm_iv, vix)

            if quality == "unavailable":
                logger.warning(
                    f"{symbol}: IV Rank quality is '{quality}'. "
                    f"Store daily IV for 30+ days "
                    f"for reliable signals."
                )

            return atm_iv, iv_rank

        except Exception as e:
            logger.warning(f"{symbol}: Could not fetch IV data: {e}")
            return 0.0, 50.0
