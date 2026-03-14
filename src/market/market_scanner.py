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
try:
    from src.market.regime_bridge import RegimeBridge
    _BRIDGE = RegimeBridge()
except Exception as _e:
    _BRIDGE = None
    import logging as _logging
    _logging.getLogger('market.market_scanner').warning(
        f'RegimeBridge unavailable: {_e}')

from src.connectors.moomoo_connector import MooMooConnector
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
        moomoo:      MooMooConnector,
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
                    f"RSI={snap.technicals.rsi:.0f} | "
                    f"regime_v2={snap.regime_v2.get('consensus_state','—') if snap.regime_v2 else '—'}/"
                    f"{snap.regime_v2.get('recommended_logic','—') if snap.regime_v2 else '—'} "
                    f"(conf={snap.regime_v2.get('confidence_score',0):.2f})" if snap.regime_v2 else ""
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

        # ── Step 2b: Regime v2 (HMM/Hurst — additive, non-breaking) ──
        # Fetch 2y history — HMM needs 200+ clean bars for stable training.
        # yfinance 60-min cache makes this second fetch nearly free.
        regime_v2 = None
        if _BRIDGE is not None:
            try:
                ohlcv_long = self._yfinance.get_daily_ohlcv(symbol, period="2y")
                regime_v2 = _BRIDGE.update(symbol, ohlcv_long)
            except Exception as _e:
                logger.debug(f'regime_v2 update skipped for {symbol}: {_e}')

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
        shares_held = self._moomoo.get_shares_held(symbol)

        # ── Step 7: Open Positions ────────────────────────────────
        all_positions = self._moomoo.get_option_positions()
        # Count positions for this symbol
        open_positions = 0
        if len(all_positions) > 0 and "code" in all_positions.columns:
            ticker = MooMooConnector.to_yfinance_symbol(symbol)
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
            regime_v2=        regime_v2,
        )

        return snapshot

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
                    f"Store daily IV for {IVRankCalculator.MIN_DAYS_RELIABLE}+ days "
                    f"for reliable signals."
                )

            return atm_iv, iv_rank

        except Exception as e:
            logger.warning(f"{symbol}: Could not fetch IV data: {e}")
            return 0.0, 50.0
