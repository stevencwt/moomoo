"""
IV Rank Calculator
==================
Computes IV Rank using a rolling 252-day window of stored daily ATM IV values.

IV Rank = (current_iv - 52w_low) / (52w_high - 52w_low) * 100

Storage: SQLite database (data/iv_history.db)
  - One row per symbol per date
  - Rolling window: 252 trading days (~1 year)
  - Bootstrap: if fewer than 30 days stored, estimates from VIX correlation

Why store locally:
  MooMoo does not provide historical IV data.
  We collect one reading per day at market close and accumulate over time.
  After ~30 days the rank becomes usable; after 252 days it is reliable.
"""

import sqlite3
import os
from datetime import date, datetime, timedelta
from typing import Optional, Tuple

from src.logger import get_logger
from src.exceptions import DataError

logger = get_logger("market.iv_rank_calculator")

# Minimum days needed before IV Rank is considered reliable
MIN_DAYS_RELIABLE = 30
# Rolling window in trading days
IV_WINDOW_DAYS    = 252


class IVRankCalculator:
    """
    Stores daily ATM IV and computes rolling IV Rank.
    """

    # Expose module-level constant as class attribute so callers can use
    # either IVRankCalculator.MIN_DAYS_RELIABLE or the module constant.
    MIN_DAYS_RELIABLE = MIN_DAYS_RELIABLE

    def __init__(self, db_path: str = "data/iv_history.db"):
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()
        logger.info(f"IVRankCalculator initialised | db={db_path}")

    # ── Public API ────────────────────────────────────────────────

    def store_daily_iv(self, symbol: str, atm_iv: float, as_of: Optional[date] = None) -> None:
        """
        Store today's ATM IV for a symbol.
        Overwrites if a record already exists for this date.

        Args:
            symbol: MooMoo format e.g. "US.TSLA"
            atm_iv: ATM implied volatility as percentage e.g. 34.5
            as_of : Date to store for (defaults to today)
        """
        if atm_iv <= 0:
            logger.warning(f"Skipping IV storage for {symbol}: atm_iv={atm_iv} is invalid")
            return

        record_date = as_of or date.today()

        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO iv_history (symbol, record_date, atm_iv)
                VALUES (?, ?, ?)
                ON CONFLICT(symbol, record_date) DO UPDATE SET atm_iv = excluded.atm_iv
            """, (symbol, record_date.isoformat(), atm_iv))

        logger.debug(f"Stored IV: {symbol} {record_date} = {atm_iv:.2f}%")

    def get_iv_rank(
        self,
        symbol: str,
        current_iv: float,
        vix: Optional[float] = None
    ) -> Tuple[float, str]:
        """
        Compute IV Rank for a symbol.

        Args:
            symbol    : MooMoo format e.g. "US.TSLA"
            current_iv: Current ATM IV as percentage
            vix       : Current VIX (used for bootstrap estimate if < MIN_DAYS stored)

        Returns:
            Tuple of (iv_rank, quality):
              iv_rank : 0-100 float
              quality : "reliable" | "estimate" | "unavailable"

        Quality meanings:
          "reliable"    : >= 252 days of history
          "estimate"    : 30-251 days of history (rank is directionally useful)
          "unavailable" : < 30 days (bootstrap used or default returned)
        """
        history = self._get_history(symbol, days=IV_WINDOW_DAYS)
        n_days  = len(history)

        if n_days >= MIN_DAYS_RELIABLE:
            iv_low  = min(history)
            iv_high = max(history)

            if iv_high == iv_low:
                iv_rank = 50.0   # All values identical — return neutral
            else:
                iv_rank = (current_iv - iv_low) / (iv_high - iv_low) * 100
                iv_rank = max(0.0, min(100.0, iv_rank))

            quality = "reliable" if n_days >= IV_WINDOW_DAYS else "estimate"
            logger.debug(
                f"{symbol} IV Rank: {iv_rank:.1f} | "
                f"current={current_iv:.1f}% | low={iv_low:.1f}% | "
                f"high={iv_high:.1f}% | days={n_days} | quality={quality}"
            )
            return iv_rank, quality

        # Bootstrap: fewer than MIN_DAYS_RELIABLE stored
        iv_rank = self._bootstrap_iv_rank(current_iv, vix)
        logger.warning(
            f"{symbol}: Only {n_days} days of IV history stored. "
            f"Using bootstrap estimate: {iv_rank:.1f} "
            f"(needs {MIN_DAYS_RELIABLE}+ days for reliability)"
        )
        return iv_rank, "unavailable"

    def get_days_stored(self, symbol: str) -> int:
        """Return number of IV history records stored for a symbol."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM iv_history WHERE symbol = ?",
                (symbol,)
            )
            return cursor.fetchone()[0]

    def purge_old_records(self, keep_days: int = IV_WINDOW_DAYS + 30) -> int:
        """
        Delete IV records older than keep_days from today.
        Run periodically to prevent database growth.

        Returns:
            Number of records deleted.
        """
        cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM iv_history WHERE record_date < ?",
                (cutoff,)
            )
            deleted = cursor.rowcount

        if deleted > 0:
            logger.info(f"Purged {deleted} old IV records (older than {cutoff})")
        return deleted

    # ── Private Helpers ───────────────────────────────────────────

    def _init_db(self) -> None:
        """Create IV history table if it doesn't exist."""
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS iv_history (
                    symbol      TEXT    NOT NULL,
                    record_date TEXT    NOT NULL,
                    atm_iv      REAL    NOT NULL,
                    created_at  TEXT    DEFAULT (datetime('now')),
                    PRIMARY KEY (symbol, record_date)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_iv_symbol_date
                ON iv_history (symbol, record_date DESC)
            """)

    def _get_conn(self) -> sqlite3.Connection:
        """Return a SQLite connection with WAL mode for concurrent access."""
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _get_history(self, symbol: str, days: int) -> list:
        """Return up to `days` most recent ATM IV values for a symbol."""
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute("""
                SELECT atm_iv FROM iv_history
                WHERE symbol = ? AND record_date >= ?
                ORDER BY record_date ASC
            """, (symbol, cutoff))
            return [row[0] for row in cursor.fetchall()]

    @staticmethod
    def _bootstrap_iv_rank(current_iv: float, vix: Optional[float]) -> float:
        """
        Estimate IV Rank when insufficient history exists.

        Method: Use VIX as a proxy for market-wide volatility context.
        High VIX (> 25) suggests elevated IV environment → rank > 60
        Low VIX  (< 15) suggests low IV environment     → rank < 40
        Mid VIX maps linearly.

        This is a rough directional estimate, not a precise calculation.
        """
        if vix is None:
            # No VIX available — return neutral
            return 50.0

        # Map VIX to approximate IV rank
        # VIX ~12 → rank ~20 (historically low)
        # VIX ~20 → rank ~50 (median)
        # VIX ~30 → rank ~75 (elevated)
        # VIX ~45 → rank ~90 (crisis)
        if vix <= 12:
            return 20.0
        elif vix <= 15:
            return 30.0 + (vix - 12) / 3 * 10
        elif vix <= 20:
            return 40.0 + (vix - 15) / 5 * 15
        elif vix <= 25:
            return 55.0 + (vix - 20) / 5 * 10
        elif vix <= 30:
            return 65.0 + (vix - 25) / 5 * 10
        elif vix <= 40:
            return 75.0 + (vix - 30) / 10 * 10
        else:
            return 90.0
