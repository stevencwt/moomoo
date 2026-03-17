"""
llm_regime_bridge.py — LLM Vision Regime Bridge for US Stocks (Options Bot)
============================================================================
Stock-compatible version of the LLM regime bridge.

Drop into: moomoo/src/market/llm_regime_bridge.py

Key differences from the crypto (zpair) version:
  - Accepts yfinance daily OHLCV DataFrame — no HyperliquidAdapter dependency
  - Timeframe: 1d (daily bars) instead of 5m / 1h
  - HTF: 100 daily bars (~5 months macro context, full chart)
  - LTF: 120 hourly bars (~3 weeks), window=60 (~1.5 weeks intraday focus)
  - Multi-symbol: instantiate one bridge per symbol (or use LLMRegimeBridgePool)
  - Update interval: configurable, default once per day (aligns with 09:35 morning scan)
  - telemetry_service: optional — not required for options bot
  - Threading: same non-blocking daemon thread pattern as crypto version

Public interface (compatible with regime_combined.py):
  bridge.maybe_update(ohlcv_df)  → None  (returns < 1ms, spawns thread when due)
  bridge.direction               → "LONG_ONLY" | "SHORT_ONLY" | "BOTH" | "WAIT" | "NO_TRADE"
  bridge.htf                     → RegimeResult | None
  bridge.ltf                     → RegimeResult | None
  bridge.is_stale                → bool
  bridge.enabled                 → bool

Direction → options strategy mapping:
  LONG_ONLY  → "bull"    — bull put spreads favoured
  SHORT_ONLY → "bear"    — bear call spreads favoured
  BOTH       → "neutral" — both spread directions eligible
  WAIT       → "high_vol"— stand aside (regime unclear)
  NO_TRADE   → "high_vol"— stand aside (TRANSITION / low confidence)

Installation:
  pip3 install -e "/Users/user/llm-regime[google]"   # Gemini (recommended)
  pip3 install -e "/Users/user/llm-regime[anthropic]" # Claude (best accuracy)
  pip3 install -e "/Users/user/llm-regime[ollama]"    # Local / free

Environment variables:
  export GOOGLE_API_KEY="..."        # for Gemini
  export ANTHROPIC_API_KEY="sk-ant-..." # for Claude
  export OPENAI_API_KEY="sk-..."     # for OpenAI

Cost estimate (Gemini 2.5 Flash):
  ~$0.0016/symbol/day (2 calls: HTF + LTF)
  ~$0.013/day for 8-symbol watchlist
  ~$0.40/month
"""

import threading
import time
import logging
from typing import Optional

import numpy as np
import pandas as pd

# ── Optional import — graceful degradation if llm_regime not installed ────────
try:
    from llm_regime import RegimeAnalyzer
    LLM_REGIME_AVAILABLE = True
except ImportError:
    LLM_REGIME_AVAILABLE = False

try:
    from src.logger import get_logger
    logger = get_logger("market.llm_regime_bridge")
except Exception:
    logger = logging.getLogger("market.llm_regime_bridge")

# ── Constants ─────────────────────────────────────────────────────────────────

# Timeframe parameters
# HTF: Daily   — 100 bars (~5 months macro context, full chart)
# LTF: Hourly  — 120 bars (~3 weeks of trading hours), window=60 (~1.5 weeks focus)
HTF_BARS          = 100    # daily bars  — ~5 months macro view
HTF_PERIOD        = "2y"   # yfinance period for daily OHLCV
LTF_BARS          = 120    # hourly bars — 60d × ~6.5h/day ≈ 390 bars available
LTF_ANALYSIS_WIN  = 60     # focus on last 60 bars (~1.5 weeks intraday)
LTF_PERIOD        = "60d"  # yfinance period for hourly OHLCV (max for 1h interval)
LTF_INTERVAL      = "1h"   # yfinance interval for LTF

# Default update intervals — split HTF (slow) from LTF (fast)
# HTF: macro trend changes slowly — every 2 hours is sufficient
# LTF: intraday regime can shift — every 30 min aligns with monitor cycle
DEFAULT_HTF_INTERVAL_SECS = 7200    # 2 hours
DEFAULT_LTF_INTERVAL_SECS = 1800    # 30 minutes

# Staleness threshold: 1.5× the respective interval
STALE_MULTIPLIER = 1.5

# Minimum LLM confidence to trust direction (1–5 scale)
DEFAULT_MIN_CONFIDENCE = 3


# ── Main Bridge Class ─────────────────────────────────────────────────────────

class LLMRegimeBridge:
    """
    Non-blocking LLM regime bridge for a single US stock symbol.

    Spawns daemon threads for LLM calls. The main trading loop calls
    maybe_update() on every monitor cycle — it returns in <1ms.
    LLM results are cached and read through a threading.Lock.

    Usage:
        bridge = LLMRegimeBridge(symbol="US.SPY", provider="gemini")

        # In monitor loop (every 30 min):
        bridge.maybe_update(ohlcv_df)   # returns immediately

        # Read result (lock-protected, always fast):
        direction = bridge.direction    # LONG_ONLY / SHORT_ONLY / BOTH / WAIT / NO_TRADE
        htf       = bridge.htf          # RegimeResult or None
        ltf       = bridge.ltf          # RegimeResult or None
    """

    def __init__(
        self,
        symbol: str,
        yfinance=None,
        provider: str = "gemini",
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        htf_interval_secs: int = DEFAULT_HTF_INTERVAL_SECS,
        ltf_interval_secs: int = DEFAULT_LTF_INTERVAL_SECS,
        min_confidence: int = DEFAULT_MIN_CONFIDENCE,
        cache_ttl: int = DEFAULT_LTF_INTERVAL_SECS,
        cache_path: Optional[str] = None,
    ):
        """
        Args:
            symbol              : MooMoo symbol e.g. "US.SPY"
            provider            : "gemini" | "anthropic" | "openai" | "ollama"
            model               : None = provider default
            api_key             : None = read from environment variable
            htf_interval_secs: How often to fire HTF LLM call (default: 7200 = 2h)
            ltf_interval_secs: How often to fire LTF LLM call (default: 1800 = 30min)
            min_confidence   : Minimum LLM confidence (1-5) to use direction signal
            cache_ttl        : RegimeAnalyzer internal cache TTL in seconds
            cache_path          : Optional path for persistent cache file
        """
        self.symbol    = symbol
        self.ticker    = symbol.replace("US.", "")    # "SPY", "QQQ", etc.
        self.enabled   = LLM_REGIME_AVAILABLE
        self._yfinance = yfinance   # YFinanceConnector — for LTF hourly fetch

        self._provider           = provider
        self._htf_interval       = htf_interval_secs
        self._ltf_interval       = ltf_interval_secs
        self._min_confidence     = min_confidence

        # Thread state — separate guards and timestamps for HTF vs LTF
        self._lock               = threading.Lock()
        self._htf_running        = False   # prevents duplicate HTF threads
        self._ltf_running        = False   # prevents duplicate LTF threads
        self._htf_last_update    = 0.0    # epoch seconds of last HTF call
        self._ltf_last_update    = 0.0    # epoch seconds of last LTF call

        # Results (written by background thread, read by main thread)
        self._htf_result         = None
        self._ltf_result         = None

        # Latest OHLCV snapshots — daily for HTF, hourly for LTF
        self._latest_ohlcv: Optional[pd.DataFrame] = None          # daily
        self._latest_ohlcv_hourly: Optional[pd.DataFrame] = None   # hourly

        if not self.enabled:
            logger.warning(
                f"[LLMRegimeBridge:{self.ticker}] llm_regime not installed. "
                f"Run: pip3 install -e \"/Users/user/llm-regime[google]\""
            )
            return

        try:
            self._analyzer = RegimeAnalyzer(
                provider=provider,
                model=model,
                api_key=api_key,
                cache_ttl=cache_ttl,
                cache_path=cache_path,
                temperature=0.1,
                sma_periods=(20, 50),
                chart_dpi=100,
            )
            logger.info(
                f"[LLMRegimeBridge:{self.ticker}] initialized | "
                f"provider={provider} | "
                f"HTF={HTF_BARS}d every {htf_interval_secs//3600}h | "
                f"LTF={LTF_BARS}×1h w{LTF_ANALYSIS_WIN} every {ltf_interval_secs//60}min"
            )
        except Exception as e:
            logger.error(f"[LLMRegimeBridge:{self.ticker}] init failed: {e}")
            self.enabled = False

    # ── Public API ────────────────────────────────────────────────────────────

    def maybe_update(self, ohlcv_df: pd.DataFrame) -> None:
        """
        Call on every monitor cycle. Returns immediately (< 1ms).

        Stores the latest OHLCV snapshot. When the update interval has elapsed
        and no thread is already running, spawns a background daemon thread to
        call the LLM. Thread guard prevents duplicate concurrent calls.

        Args:
            ohlcv_df: Daily OHLCV DataFrame from YFinanceConnector.get_daily_ohlcv()
                      Columns: open, high, low, close, volume (lowercase)
                      Index: DatetimeIndex
        """
        if not self.enabled or ohlcv_df is None or len(ohlcv_df) == 0:
            return

        # Store daily OHLCV for HTF thread
        with self._lock:
            self._latest_ohlcv = ohlcv_df

        # Fetch hourly OHLCV for LTF thread when update is due
        now_check = time.time()
        if (now_check - self._ltf_last_update >= self._ltf_interval
                and not self._ltf_running
                and self._yfinance is not None):
            try:
                hourly = self._yfinance.get_intraday_ohlcv(
                    self.symbol, interval=LTF_INTERVAL, period=LTF_PERIOD
                )
                with self._lock:
                    self._latest_ohlcv_hourly = hourly
            except Exception as _he:
                logger.warning(
                    f"[LLMRegimeBridge:{self.ticker}] hourly fetch failed: {_he}"
                )

        now = time.time()

        # HTF — fires every 2h (macro trend, slow-moving)
        if (now - self._htf_last_update >= self._htf_interval
                and not self._htf_running):
            self._htf_running = True
            threading.Thread(
                target=self._run_htf,
                daemon=True,
                name=f"llm_htf_{self.ticker}"
            ).start()

        # LTF — fires every 30 min (intraday regime, needs frequent refresh)
        if (now - self._ltf_last_update >= self._ltf_interval
                and not self._ltf_running):
            self._ltf_running = True
            threading.Thread(
                target=self._run_ltf,
                daemon=True,
                name=f"llm_ltf_{self.ticker}"
            ).start()

    @property
    def direction(self) -> str:
        """
        Combined direction from HTF × LTF decision matrix.
        Thread-safe read. Returns in < 1ms.

        Returns: LONG_ONLY | SHORT_ONLY | BOTH | WAIT | NO_TRADE
        """
        if not self.enabled:
            return "BOTH"   # no opinion — don't gate strategies

        with self._lock:
            htf = self._htf_result
            ltf = self._ltf_result

        return self._compute_direction(htf, ltf)

    @property
    def htf(self):
        """HTF RegimeResult (macro ~5 months view). Thread-safe."""
        with self._lock:
            return self._htf_result

    @property
    def ltf(self):
        """LTF RegimeResult (hourly — ~3 weeks context, 1.5-week focus). Thread-safe."""
        with self._lock:
            return self._ltf_result

    @property
    def is_stale(self) -> bool:
        """
        True if the LLM hasn't been updated in > 1.5× the update interval.
        Used by CombinedRegime to fall back to drift detection.
        """
        # Stale = LTF hasn't been updated recently (LTF drives intraday decisions)
        if self._ltf_last_update == 0:
            return True
        return time.time() - self._ltf_last_update > self._ltf_interval * STALE_MULTIPLIER

    def get_summary(self) -> dict:
        """Return a loggable summary of current LLM state."""
        with self._lock:
            htf = self._htf_result
            ltf = self._ltf_result
        return {
            "symbol":        self.symbol,
            "direction":     self.direction,
            "htf_regime":    htf.regime if htf else None,
            "htf_conf":      htf.confidence if htf else None,
            "ltf_regime":    ltf.regime if ltf else None,
            "ltf_conf":      ltf.confidence if ltf else None,
            "stale":         self.is_stale,
            "htf_last_update": self._htf_last_update,
            "ltf_last_update": self._ltf_last_update,
        }

    # ── Background Thread ─────────────────────────────────────────────────────

    def _run_htf(self) -> None:
        """Background thread: HTF LLM call (every 2h). Never call directly."""
        try:
            ohlcv = self._get_clean_ohlcv(min_bars=HTF_BARS)
            if ohlcv is None:
                return
            opens   = ohlcv["open"].values.astype(float)
            highs   = ohlcv["high"].values.astype(float)
            lows    = ohlcv["low"].values.astype(float)
            closes  = ohlcv["close"].values.astype(float)
            volumes = ohlcv["volume"].values.astype(float)

            t0 = time.time()
            result = self._analyzer.analyze(
                asset=self.ticker,
                timeframe="1d",
                opens=opens[-HTF_BARS:],
                highs=highs[-HTF_BARS:],
                lows=lows[-HTF_BARS:],
                closes=closes[-HTF_BARS:],
                volumes=volumes[-HTF_BARS:],
                analysis_window=None,   # full chart — macro view
                extra_context=(
                    f"This is a US stock ({self.ticker}) daily chart for macro trend analysis. "
                    f"Focus on the overall trend direction over the past 5 months."
                ),
                force_refresh=True,     # HTF always fetches fresh (2h cache bypass)
            )
            ms = int((time.time() - t0) * 1000)
            with self._lock:
                self._htf_result     = result
                self._htf_last_update = time.time()
            logger.info(
                f"[LLMRegimeBridge:{self.ticker}] HTF: "
                f"{result.regime} conf={result.confidence} "
                f"dir={result.scalp_direction} ({ms}ms)"
            )
        except Exception as e:
            logger.error(f"[LLMRegimeBridge:{self.ticker}] HTF thread failed: {e}")
        finally:
            self._htf_running = False

    def _run_ltf(self) -> None:
        """Background thread: LTF LLM call (every 30min). Uses hourly OHLCV."""
        try:
            ohlcv = self._get_clean_ohlcv(min_bars=LTF_BARS, hourly=True)
            if ohlcv is None:
                return
            opens   = ohlcv["open"].values.astype(float)
            highs   = ohlcv["high"].values.astype(float)
            lows    = ohlcv["low"].values.astype(float)
            closes  = ohlcv["close"].values.astype(float)
            volumes = ohlcv["volume"].values.astype(float)

            t0 = time.time()
            result = self._analyzer.analyze(
                asset=self.ticker,
                timeframe="1h",
                opens=opens[-LTF_BARS:],
                highs=highs[-LTF_BARS:],
                lows=lows[-LTF_BARS:],
                closes=closes[-LTF_BARS:],
                volumes=volumes[-LTF_BARS:],
                analysis_window=LTF_ANALYSIS_WIN,   # focus on last ~1.5 weeks
                extra_context=(
                    f"This is a US stock ({self.ticker}) 1-hour chart. "
                    f"The grayed bars are context; the colored right-edge bars "
                    f"show the current intraday structure (~1.5 weeks focus). "
                    f"Focus on the colored section for current regime."
                ),
                force_refresh=True,     # always fresh — direction can change intraday
            )
            ms = int((time.time() - t0) * 1000)
            with self._lock:
                self._ltf_result     = result
                self._ltf_last_update = time.time()
            # Log combined direction after each LTF update
            with self._lock:
                htf = self._htf_result
                ltf = self._ltf_result
            combined = self._compute_direction(htf, ltf)
            logger.info(
                f"[LLMRegimeBridge:{self.ticker}] LTF: "
                f"{result.regime} conf={result.confidence} "
                f"dir={result.scalp_direction} ({ms}ms) → combined: {combined}"
            )
        except Exception as e:
            logger.error(f"[LLMRegimeBridge:{self.ticker}] LTF thread failed: {e}")
        finally:
            self._ltf_running = False

    def _get_clean_ohlcv(self, min_bars: int, hourly: bool = False):
        """Thread-safe read of OHLCV (daily or hourly), cleaned."""
        with self._lock:
            ohlcv = self._latest_ohlcv_hourly if hourly else self._latest_ohlcv
        label = "hourly" if hourly else "daily"
        if ohlcv is None or len(ohlcv) < min_bars:
            logger.warning(
                f"[LLMRegimeBridge:{self.ticker}] insufficient {label} bars "
                f"({len(ohlcv) if ohlcv is not None else 0} < {min_bars})"
            )
            return None
        cleaned = self._clean(ohlcv)
        if len(cleaned) < min_bars:
            logger.warning(
                f"[LLMRegimeBridge:{self.ticker}] insufficient clean {label} bars"
            )
            return None
        return cleaned

    # ── Direction Decision Matrix ─────────────────────────────────────────────

    def _compute_direction(self, htf, ltf) -> str:
        """
        Apply the HTF × LTF decision matrix.

        Mirrors the crypto version's logic (REGIME_MODULE_SYNOPSIS_V3 §8 Step 2).
        For options income, direction maps to which spread strategy to favour.

        Returns: LONG_ONLY | SHORT_ONLY | BOTH | WAIT | NO_TRADE
        """
        # No data yet — no opinion
        if htf is None and ltf is None:
            return "BOTH"

        # Below confidence threshold — no opinion
        htf_ok = htf is not None and htf.confidence >= self._min_confidence
        ltf_ok = ltf is not None and ltf.confidence >= self._min_confidence

        if not htf_ok and not ltf_ok:
            return "BOTH"

        # Only one available — use whichever exists
        if not htf_ok:
            return ltf.scalp_direction if ltf_ok else "BOTH"
        if not ltf_ok:
            return htf.scalp_direction

        # Both available — apply decision matrix (Section 9 of v3 spec)
        if htf.regime == "TRANSITION":
            return "NO_TRADE"

        if htf.is_bullish:
            if ltf.is_bullish:
                return "LONG_ONLY"     # strong long — bull put spreads
            elif ltf.is_ranging:
                return "LONG_ONLY"     # buy dips in macro uptrend
            else:
                return "WAIT"          # pullback — don't fight HTF trend

        elif htf.is_bearish:
            if ltf.is_bearish:
                return "SHORT_ONLY"    # strong short — bear call spreads
            elif ltf.is_ranging:
                return "SHORT_ONLY"    # sell rips in macro downtrend
            else:
                return "WAIT"          # counter-trend bounce — skip

        elif htf.is_ranging:
            if ltf.is_bullish:
                return "LONG_ONLY"     # micro uptrend in ranging macro
            elif ltf.is_bearish:
                return "SHORT_ONLY"    # micro downtrend in ranging macro
            else:
                return "BOTH"          # both sides eligible (neutral regime)

        else:
            # HTF = TRANSITION or UNKNOWN
            return ltf.scalp_direction if ltf_ok else "BOTH"

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _clean(ohlcv: pd.DataFrame) -> pd.DataFrame:
        """Drop NaN/inf rows before sending to LLM chart generator."""
        cols = [c for c in ("open", "high", "low", "close", "volume")
                if c in ohlcv.columns]
        cleaned = ohlcv.dropna(subset=cols)
        mask = ~cleaned[cols].apply(lambda s: np.isinf(s)).any(axis=1)
        return cleaned[mask]


# ── Multi-Symbol Pool ─────────────────────────────────────────────────────────

class LLMRegimeBridgePool:
    """
    Manages one LLMRegimeBridge instance per symbol in the watchlist.

    Usage (from market_scanner or bot_scheduler):

        pool = LLMRegimeBridgePool(
            symbols=["US.SPY", "US.QQQ", "US.NVDA", ...],
            provider="gemini",
        )

        # In monitor loop, after fetching OHLCV:
        pool.maybe_update("US.SPY", ohlcv_spy)
        pool.maybe_update("US.QQQ", ohlcv_qqq)
        ...

        # Read direction per symbol:
        direction = pool.direction("US.SPY")   # LONG_ONLY / SHORT_ONLY / BOTH / ...
        htf       = pool.htf("US.SPY")         # RegimeResult or None
        stale     = pool.is_stale("US.SPY")    # bool
    """

    def __init__(
        self,
        symbols: list,
        yfinance=None,
        provider: str = "gemini",
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        htf_interval_secs: int = DEFAULT_HTF_INTERVAL_SECS,
        ltf_interval_secs: int = DEFAULT_LTF_INTERVAL_SECS,
        min_confidence: int = DEFAULT_MIN_CONFIDENCE,
        cache_path: Optional[str] = None,
    ):
        self._bridges: dict[str, LLMRegimeBridge] = {}
        self.enabled = LLM_REGIME_AVAILABLE

        for symbol in symbols:
            # Per-symbol persistent cache file to survive restarts
            sym_cache = None
            if cache_path:
                sym_cache = f"{cache_path}_{symbol.replace('.', '_')}.json"

            self._bridges[symbol] = LLMRegimeBridge(
                symbol=symbol,
                yfinance=yfinance,
                provider=provider,
                model=model,
                api_key=api_key,
                htf_interval_secs=htf_interval_secs,
                ltf_interval_secs=ltf_interval_secs,
                min_confidence=min_confidence,
                cache_path=sym_cache,
            )

        if self.enabled:
            logger.info(
                f"[LLMRegimeBridgePool] {len(symbols)} symbols | "
                f"provider={provider} | "
                f"HTF={htf_interval_secs//3600}h LTF={ltf_interval_secs//60}min"
            )

    def maybe_update(self, symbol: str, ohlcv_df: pd.DataFrame) -> None:
        """Update the bridge for a specific symbol. Returns immediately."""
        if symbol in self._bridges:
            self._bridges[symbol].maybe_update(ohlcv_df)

    def direction(self, symbol: str) -> str:
        """Get combined direction for a symbol."""
        if symbol in self._bridges:
            return self._bridges[symbol].direction
        return "BOTH"

    def htf(self, symbol: str):
        """Get HTF RegimeResult for a symbol."""
        if symbol in self._bridges:
            return self._bridges[symbol].htf
        return None

    def ltf(self, symbol: str):
        """Get LTF RegimeResult for a symbol."""
        if symbol in self._bridges:
            return self._bridges[symbol].ltf
        return None

    def is_stale(self, symbol: str) -> bool:
        """True if the LLM result for this symbol is stale."""
        if symbol in self._bridges:
            return self._bridges[symbol].is_stale
        return True

    def get_summary(self) -> dict:
        """Return loggable summary for all symbols."""
        return {sym: bridge.get_summary()
                for sym, bridge in self._bridges.items()}


# ── Direction → Bot Regime Mapping ───────────────────────────────────────────

def llm_direction_to_regime_hint(direction: str) -> Optional[str]:
    """
    Map LLM direction to the bot's regime strings.

    This is a HINT only — it supplements translate_to_bot_regime() in
    regime_bridge.py but does not override the VIX safety gate or
    EXPANDING volatility check.

    LONG_ONLY  → "bull"    → bull put spreads favoured
    SHORT_ONLY → "bear"    → bear call spreads favoured
    BOTH       → "neutral" → both spread directions eligible
    WAIT       → None      → defer to quant regime (no opinion)
    NO_TRADE   → "high_vol"→ stand aside
    """
    mapping = {
        "LONG_ONLY":  "bull",
        "SHORT_ONLY": "bear",
        "BOTH":       "neutral",
        "WAIT":       None,        # defer to quant
        "NO_TRADE":   "high_vol",
    }
    return mapping.get(direction, None)
