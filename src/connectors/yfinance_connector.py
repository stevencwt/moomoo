"""
Yahoo Finance Connector
=======================
Thin wrapper around yfinance for price history and earnings dates.

Note on symbol format:
  MooMoo uses "US.TSLA", yfinance uses "TSLA".
  This connector accepts both formats — strips "US." prefix internally.

Caching:
  Daily OHLCV cached for 60 minutes per symbol.
  VIX cached for 30 minutes.
  Intraday data is never cached.
"""

import yfinance as yf
import pandas as pd
from datetime import date, datetime, timedelta
from typing import List, Dict, Optional
import time

from src.logger import get_logger
from src.exceptions import DataError

logger = get_logger("connectors.yfinance")


class YFinanceConnector:
    """
    Wrapper around yfinance for historical price data,
    VIX, and earnings dates.
    """

    CACHE_TTL_DAILY   = 3600   # 60 minutes
    CACHE_TTL_VIX     = 1800   # 30 minutes

    def __init__(self):
        self._cache: Dict[str, Dict] = {}
        logger.info("YFinanceConnector initialised")

    # ── OHLCV Data ────────────────────────────────────────────────

    def get_daily_ohlcv(
        self,
        symbol: str,
        period: str = "6mo"
    ) -> pd.DataFrame:
        """
        Return daily OHLCV data for a symbol.

        Args:
            symbol: MooMoo format ("US.TSLA") or plain ("TSLA")
            period: yfinance period string e.g. "6mo", "1y", "2y"

        Returns:
            DataFrame with lowercase columns: open, high, low, close, volume
            Index: DatetimeIndex (timezone-naive)

        Raises:
            DataError: If no data returned.
        """
        ticker = self._to_yf_symbol(symbol)
        cache_key = f"daily_{ticker}_{period}"

        # Return cached if fresh
        cached = self._get_cache(cache_key, self.CACHE_TTL_DAILY)
        if cached is not None:
            logger.debug(f"Cache hit: {cache_key}")
            return cached

        logger.debug(f"Fetching daily OHLCV: {ticker} period={period}")

        try:
            data = yf.download(
                ticker,
                period=period,
                interval="1d",
                progress=False,
                auto_adjust=True
            )
        except Exception as e:
            raise DataError(f"yfinance download failed for {ticker}: {e}")

        if data is None or len(data) == 0:
            raise DataError(f"No OHLCV data returned for {ticker}")

        # Flatten MultiIndex columns (yfinance >= 0.2.x returns tuples)
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)

        # Normalise column names to lowercase
        data.columns = [c.lower() for c in data.columns]

        # Remove timezone info for consistency
        if hasattr(data.index, "tz") and data.index.tz is not None:
            data.index = data.index.tz_localize(None)

        required = {"open", "high", "low", "close", "volume"}
        missing  = required - set(data.columns)
        if missing:
            raise DataError(
                f"Missing columns in OHLCV data for {ticker}: {missing}. "
                f"Got: {data.columns.tolist()}"
            )

        logger.debug(f"{ticker}: {len(data)} daily bars fetched")
        self._set_cache(cache_key, data)
        return data

    def get_intraday_ohlcv(
        self,
        symbol: str,
        interval: str = "1h",
        period: str = "60d"
    ) -> pd.DataFrame:
        """
        Return intraday OHLCV data. Not cached.

        Args:
            symbol  : MooMoo or plain format
            interval: "1h" | "30m" | "15m" | "5m"
            period  : Max "60d" for hourly, "7d" for 1m

        Returns:
            DataFrame with lowercase columns: open, high, low, close, volume
        """
        ticker = self._to_yf_symbol(symbol)
        logger.debug(f"Fetching intraday OHLCV: {ticker} {interval} {period}")

        try:
            data = yf.download(
                ticker,
                period=period,
                interval=interval,
                progress=False,
                auto_adjust=True
            )
        except Exception as e:
            raise DataError(f"yfinance intraday download failed for {ticker}: {e}")

        if data is None or len(data) == 0:
            raise DataError(f"No intraday data returned for {ticker}")

        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)
        data.columns = [c.lower() for c in data.columns]

        if hasattr(data.index, "tz") and data.index.tz is not None:
            data.index = data.index.tz_localize(None)

        return data

    # ── VIX ───────────────────────────────────────────────────────

    def get_current_vix(self) -> float:
        """
        Return the most recent VIX closing value.

        Returns:
            VIX as a float (e.g. 18.5)

        Raises:
            DataError: If VIX data unavailable.
        """
        cache_key = "vix_current"
        cached = self._get_cache(cache_key, self.CACHE_TTL_VIX)
        if cached is not None:
            return cached

        logger.debug("Fetching current VIX")

        try:
            data = yf.download(
                "^VIX",
                period="5d",
                interval="1d",
                progress=False,
                auto_adjust=True
            )
        except Exception as e:
            raise DataError(f"VIX download failed: {e}")

        if data is None or len(data) == 0:
            raise DataError("No VIX data returned")

        # Handle MultiIndex
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)

        data.columns = [c.lower() for c in data.columns]
        vix = float(data["close"].iloc[-1])

        if not (5 <= vix <= 150):
            logger.warning(f"VIX value {vix} looks unusual — verify data source")

        logger.debug(f"Current VIX: {vix:.2f}")
        self._set_cache(cache_key, vix)
        return vix


    def get_current_price(self, symbol: str) -> float:
        """
        Return the current intraday price for a symbol using fast_info.
        Much faster than downloading OHLCV — no bar data needed.

        Falls back to last daily close if fast_info is unavailable.

        Args:
            symbol: MooMoo format ("US.TSLA") or plain ("TSLA")

        Returns:
            Current price as float.

        Raises:
            DataError: If price cannot be determined.
        """
        ticker_str = self._to_yf_symbol(symbol)
        cache_key  = f"spot_{ticker_str}"

        # Short TTL — 2 minutes for intraday use
        cached = self._get_cache(cache_key, 120)
        if cached is not None:
            return cached

        try:
            tk    = yf.Ticker(ticker_str)
            price = tk.fast_info.get("lastPrice") or tk.fast_info.get("last_price")
            if price and price > 0:
                self._set_cache(cache_key, float(price))
                logger.debug(f"Spot price {ticker_str}: ${price:.2f} (fast_info)")
                return float(price)
        except Exception:
            pass

        # Fallback: last close from daily OHLCV
        try:
            ohlcv = self.get_daily_ohlcv(symbol)
            price = float(ohlcv["close"].iloc[-1])
            logger.debug(f"Spot price {ticker_str}: ${price:.2f} (daily close fallback)")
            self._set_cache(cache_key, price)
            return price
        except Exception as e:
            raise DataError(f"Cannot fetch spot price for {ticker_str}: {e}")

    def get_vix_history(self, period: str = "6mo") -> pd.DataFrame:
        """
        Return historical VIX data for regime analysis.

        Args:
            period: yfinance period string

        Returns:
            DataFrame with lowercase columns including 'close'
        """
        cache_key = f"vix_history_{period}"
        cached = self._get_cache(cache_key, self.CACHE_TTL_DAILY)
        if cached is not None:
            return cached

        try:
            data = yf.download(
                "^VIX",
                period=period,
                interval="1d",
                progress=False,
                auto_adjust=True
            )
        except Exception as e:
            raise DataError(f"VIX history download failed: {e}")

        if data is None or len(data) == 0:
            raise DataError("No VIX history returned")

        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)

        data.columns = [c.lower() for c in data.columns]

        if hasattr(data.index, "tz") and data.index.tz is not None:
            data.index = data.index.tz_localize(None)

        self._set_cache(cache_key, data)
        return data

    # ── Earnings Dates ────────────────────────────────────────────

    def get_earnings_dates(self, symbol: str) -> List[date]:
        """
        Return upcoming earnings dates for a symbol.

        Args:
            symbol: MooMoo or plain format

        Returns:
            List of upcoming earnings dates (today or later), sorted ascending.
            Empty list if no earnings data available.
        """
        ticker   = self._to_yf_symbol(symbol)
        today    = date.today()
        cache_key = f"earnings_{ticker}"

        cached = self._get_cache(cache_key, self.CACHE_TTL_DAILY)
        if cached is not None:
            return cached

        # ETFs have no earnings — skip the fetch entirely to avoid noisy 404 errors
        _KNOWN_ETFS = {"SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "TLT",
                       "XLK", "XLF", "XLE", "XLV", "ARKK", "VIX"}
        if ticker in _KNOWN_ETFS:
            logger.debug(f"{ticker}: ETF — no earnings dates applicable")
            self._set_cache(cache_key, [])
            return []

        logger.debug(f"Fetching earnings dates for {ticker}")

        try:
            t = yf.Ticker(ticker)
            cal = t.calendar
        except Exception as e:
            logger.warning(f"Could not fetch earnings for {ticker}: {e}")
            return []

        earnings_dates = []

        if cal is not None:
            # calendar is a dict with 'Earnings Date' as a list or Timestamp
            earnings_val = cal.get("Earnings Date")
            if earnings_val is not None:
                if not isinstance(earnings_val, list):
                    earnings_val = [earnings_val]
                for val in earnings_val:
                    try:
                        if hasattr(val, "date"):
                            d = val.date()
                        else:
                            d = pd.Timestamp(val).date()
                        if d >= today:
                            earnings_dates.append(d)
                    except Exception:
                        pass

        # Fallback: try earnings_dates property
        if not earnings_dates:
            try:
                ed = t.earnings_dates
                if ed is not None and len(ed) > 0:
                    for idx in ed.index:
                        try:
                            d = pd.Timestamp(idx).date()
                            if d >= today:
                                earnings_dates.append(d)
                        except Exception:
                            pass
            except Exception:
                pass

        earnings_dates = sorted(set(earnings_dates))
        logger.debug(f"{ticker}: next earnings = {earnings_dates[:2] if earnings_dates else 'unknown'}")

        self._set_cache(cache_key, earnings_dates)
        return earnings_dates

    # ── Cache Management ──────────────────────────────────────────

    def clear_cache(self) -> None:
        """Clear all cached data."""
        self._cache.clear()
        logger.debug("Cache cleared")

    # ── Private Helpers ───────────────────────────────────────────

    @staticmethod
    def _to_yf_symbol(symbol: str) -> str:
        """Convert MooMoo format to yfinance format."""
        return symbol.replace("US.", "").replace("HK.", "").upper()

    def _get_cache(self, key: str, ttl: int) -> Optional[object]:
        """Return cached value if within TTL, else None."""
        if key not in self._cache:
            return None
        entry = self._cache[key]
        if time.time() - entry["timestamp"] > ttl:
            return None
        return entry["data"]

    def _set_cache(self, key: str, data: object) -> None:
        """Store value in cache with current timestamp."""
        self._cache[key] = {
            "data":      data,
            "timestamp": time.time()
        }
