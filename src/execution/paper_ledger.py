"""
Paper Ledger
============
SQLite record of all paper trades during the validation period.

Schema (current):
  Core, entry context (Phase 9), analytics at open (Phase 9h),
  exit fields (Phase 9), analytics at close (Phase 9h),
  live trading fields.

See _init_db() for full column list with types.

Database migration:
  _migrate_db() runs on every __init__ — idempotent, adds missing columns only.
"""

import sqlite3
import os
from datetime import date, datetime
from typing import Optional, Any, List, Dict

from src.strategies.trade_signal import TradeSignal
from src.logger import get_logger

logger = get_logger("execution.paper_ledger")


class PaperLedger:
    """Persistent SQLite record of paper trades."""

    def __init__(self, db_path: str = "data/paper_trades.db"):
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()
        self._migrate_db()
        logger.info(f"PaperLedger initialised | db={db_path}")

    # ── Public API ────────────────────────────────────────────────

    def record_open(
        self,
        signal:    TradeSignal,
        fill_sell: float,
        fill_buy:  Optional[float] = None,
        snapshot:  Optional[Any]   = None,
    ) -> int:
        """
        Record a newly opened paper position.

        Args:
            signal    : TradeSignal (may carry analytics fields set by strategy/TradeManager)
            fill_sell : Actual fill price of the short leg
            fill_buy  : Actual fill price of the long leg (None for covered calls)
            snapshot  : MarketSnapshot — provides RSI, %B, MACD, VIX at entry

        Returns:
            Trade ID (row id in paper_trades table)
        """
        net_credit = fill_sell - (fill_buy or 0)

        # ── Entry context from MarketSnapshot ────────────────────
        rsi_at_open = pct_b_at_open = macd_at_open = vix_at_open = None
        if snapshot is not None:
            try:
                rsi_at_open   = float(snapshot.technicals.rsi)
                pct_b_at_open = float(snapshot.technicals.pct_b)
                macd_at_open  = float(snapshot.technicals.macd)
                vix_at_open   = float(snapshot.vix)
            except Exception as e:
                logger.warning(f"record_open: could not extract snapshot context: {e}")

        # ── Analytics fields from signal ─────────────────────────
        short_strike   = getattr(signal, "short_strike",   None)
        long_strike    = getattr(signal, "long_strike",    None)
        atm_iv_at_open = getattr(signal, "atm_iv_at_open", None)
        theta_at_open  = getattr(signal, "theta_at_open",  None)
        vega_at_open   = getattr(signal, "vega_at_open",   None)
        signal_score   = getattr(signal, "signal_score",   None)
        entry_type     = getattr(signal, "entry_type",     None)

        with self._get_conn() as conn:
            cursor = conn.execute("""
                INSERT INTO paper_trades (
                    symbol, strategy_name, signal_type,
                    sell_contract, buy_contract, quantity,
                    sell_price, buy_price, net_credit,
                    max_profit, max_loss, breakeven,
                    expiry, dte_at_open, iv_rank, delta, regime,
                    opened_at, status,
                    spot_price_at_open, buffer_pct, reward_risk,
                    rsi_at_open, pct_b_at_open, macd_at_open, vix_at_open,
                    short_strike, long_strike, atm_iv_at_open,
                    theta_at_open, vega_at_open,
                    signal_score, entry_type
                ) VALUES (
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, 'open',
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?,
                    ?, ?
                )
            """, (
                signal.symbol, signal.strategy_name, signal.signal_type,
                signal.sell_contract, signal.buy_contract, signal.quantity,
                round(fill_sell, 4), round(fill_buy, 4) if fill_buy else None,
                round(net_credit, 4),
                round(net_credit * 100, 2),
                signal.max_loss,
                signal.breakeven,
                signal.expiry, signal.dte,
                signal.iv_rank, signal.delta, signal.regime,
                datetime.now().isoformat(),
                signal.spot_price, signal.buffer_pct, signal.reward_risk,
                rsi_at_open, pct_b_at_open, macd_at_open, vix_at_open,
                short_strike, long_strike, atm_iv_at_open,
                theta_at_open, vega_at_open,
                signal_score, entry_type,
            ))
            trade_id = cursor.lastrowid

        logger.info(
            f"[PAPER] Opened #{trade_id}: {signal.symbol} {signal.strategy_name} | "
            f"credit=${net_credit:.2f} | strike={short_strike}/{long_strike} | "
            f"IV={signal.iv_rank:.0f} | RSI={rsi_at_open} | "
            f"score={signal_score} | entry={entry_type} | expiry={signal.expiry}"
        )
        return trade_id

    def record_close(
        self,
        trade_id:             int,
        close_price:          float,
        close_reason:         str,
        closed_at:            Optional[datetime] = None,
        # Phase 9 exit context
        spot_price_at_close:  Optional[float] = None,
        dte_at_close:         Optional[int]   = None,
        iv_rank_at_close:     Optional[float] = None,
        vix_at_close:         Optional[float] = None,
        # Phase 9h analytics at close
        atm_iv_at_close:      Optional[float] = None,
        rsi_at_close:         Optional[float] = None,
        pct_b_at_close:       Optional[float] = None,
        commission:           float = 0.0,
    ) -> float:
        """
        Record the close of a paper position and compute P&L.

        Derived automatically:
          - days_held         : calendar days from open to close
          - pct_premium_captured
          - spot_change_pct   : % move of underlying from open to close
          - buffer_at_close   : % distance from spot_at_close to short_strike
          - pnl_net           : pnl - commission

        Returns:
            Realised gross P&L in dollars.
        """
        valid_reasons = {
            "expired_worthless", "stop_loss", "take_profit", "dte_close", "manual",
            "regime_shift",   # HMM exit mandate — regime module force-close
            "ledger_reset",   # manual ledger reset
        }
        if close_reason not in valid_reasons:
            raise ValueError(
                f"Invalid close_reason '{close_reason}'. Must be one of {valid_reasons}"
            )

        close_ts = (closed_at or datetime.now()).isoformat()

        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT net_credit, quantity, opened_at, short_strike, spot_price_at_open "
                "FROM paper_trades WHERE id = ?",
                (trade_id,)
            ).fetchone()

            if not row:
                raise ValueError(f"No paper trade found with id={trade_id}")

            net_credit, quantity, opened_at_str, short_strike, spot_open = row

            # ── Derived metrics ───────────────────────────────────
            pnl     = (net_credit - close_price) * 100 * quantity
            pnl_net = round(pnl - commission, 2)

            try:
                opened_dt = datetime.fromisoformat(opened_at_str)
                closed_dt = closed_at or datetime.now()
                days_held = max(0, (closed_dt.date() - opened_dt.date()).days)
            except Exception:
                days_held = None

            try:
                pct_premium_captured = round(
                    (net_credit - close_price) / net_credit * 100, 1
                ) if net_credit > 0 else None
            except Exception:
                pct_premium_captured = None

            spot_change_pct = None
            buffer_at_close = None
            try:
                if spot_open and spot_price_at_close:
                    spot_change_pct = round(
                        (spot_price_at_close - spot_open) / spot_open * 100, 2
                    )
                if short_strike and spot_price_at_close:
                    buffer_at_close = round(
                        (short_strike - spot_price_at_close) / spot_price_at_close * 100, 2
                    )
            except Exception:
                pass

            status = "expired" if close_reason == "expired_worthless" else "closed"

            conn.execute("""
                UPDATE paper_trades
                SET close_price          = ?,
                    closed_at            = ?,
                    close_reason         = ?,
                    pnl                  = ?,
                    pnl_net              = ?,
                    status               = ?,
                    days_held            = ?,
                    dte_at_close         = ?,
                    spot_price_at_close  = ?,
                    iv_rank_at_close     = ?,
                    vix_at_close         = ?,
                    pct_premium_captured = ?,
                    atm_iv_at_close      = ?,
                    rsi_at_close         = ?,
                    pct_b_at_close       = ?,
                    spot_change_pct      = ?,
                    buffer_at_close      = ?,
                    commission           = ?
                WHERE id = ?
            """, (
                round(close_price, 4),
                close_ts,
                close_reason,
                round(pnl, 2),
                pnl_net,
                status,
                days_held,
                dte_at_close,
                spot_price_at_close,
                iv_rank_at_close,
                vix_at_close,
                pct_premium_captured,
                atm_iv_at_close,
                rsi_at_close,
                pct_b_at_close,
                spot_change_pct,
                buffer_at_close,
                round(commission, 2),
                trade_id,
            ))

        logger.info(
            f"[PAPER] Closed #{trade_id}: reason={close_reason} | "
            f"P&L=${pnl:+.2f} (net=${pnl_net:+.2f}) | "
            f"days={days_held} | dte_rem={dte_at_close} | "
            f"captured={pct_premium_captured}% | "
            f"buf_close={buffer_at_close}% | spot_chg={spot_change_pct}%"
        )
        return pnl

    def get_open_trades(self) -> List[Dict]:
        """Return all currently open paper trades with full context."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "SELECT * FROM paper_trades WHERE status = 'open' ORDER BY opened_at DESC"
            )
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def get_closed_trades(self) -> List[Dict]:
        """Return all closed/expired trades ordered newest first."""
        with self._get_conn() as conn:
            cursor = conn.execute("""
                SELECT * FROM paper_trades
                WHERE status IN ('closed', 'expired')
                ORDER BY closed_at DESC
            """)
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def get_all_trades(self) -> List[Dict]:
        """Return every trade regardless of status."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "SELECT * FROM paper_trades ORDER BY opened_at DESC"
            )
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def get_trades_opened_on(self, date_str: str) -> List[Dict]:
        """Return all trades (any status) opened on the given date (YYYY-MM-DD)."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "SELECT * FROM paper_trades WHERE opened_at LIKE ? ORDER BY opened_at",
                (f"{date_str}%",),
            )
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def get_trade(self, trade_id: int) -> Optional[Dict]:
        """Return a single trade by ID."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "SELECT * FROM paper_trades WHERE id = ?", (trade_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))

    def get_statistics(self) -> Dict:
        """
        Compute validation statistics across all closed trades.
        Includes averages of key analytics fields for strategy review.
        """
        with self._get_conn() as conn:
            overall = conn.execute("""
                SELECT
                    COUNT(*)                                               AS total_trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)             AS winning_trades,
                    SUM(pnl)                                               AS total_pnl,
                    AVG(pnl)                                               AS avg_pnl,
                    AVG(net_credit)                                        AS avg_credit,
                    AVG(max_loss)                                          AS avg_max_loss,
                    MAX(pnl)                                               AS best_trade,
                    MIN(pnl)                                               AS worst_trade,
                    AVG(days_held)                                         AS avg_days_held,
                    AVG(dte_at_close)                                      AS avg_dte_at_close,
                    AVG(pct_premium_captured)                              AS avg_pct_captured,
                    AVG(CASE WHEN atm_iv_at_open IS NOT NULL
                             AND atm_iv_at_close IS NOT NULL
                        THEN atm_iv_at_open - atm_iv_at_close END)        AS avg_iv_crush,
                    AVG(signal_score)                                      AS avg_signal_score
                FROM paper_trades
                WHERE status IN ('closed', 'expired') AND pnl IS NOT NULL
            """).fetchone()

            open_count = conn.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE status = 'open'"
            ).fetchone()[0]

            by_strategy_rows = conn.execute("""
                SELECT strategy_name,
                       COUNT(*), SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
                       SUM(pnl), AVG(pnl), AVG(pct_premium_captured)
                FROM paper_trades
                WHERE status IN ('closed', 'expired') AND pnl IS NOT NULL
                GROUP BY strategy_name
            """).fetchall()

            by_reason_rows = conn.execute("""
                SELECT close_reason,
                       COUNT(*), AVG(pnl), AVG(pct_premium_captured)
                FROM paper_trades
                WHERE status IN ('closed', 'expired') AND pnl IS NOT NULL
                GROUP BY close_reason
            """).fetchall()

        total = overall[0] or 0
        wins  = overall[1] or 0

        by_strategy = {}
        for row in by_strategy_rows:
            by_strategy[row[0]] = {
                "trades":           row[1],
                "wins":             row[2],
                "win_rate":         row[2] / row[1] if row[1] > 0 else 0,
                "total_pnl":        round(row[3] or 0, 2),
                "avg_pnl":          round(row[4] or 0, 2),
                "avg_pct_captured": round(row[5] or 0, 1) if row[5] is not None else None,
            }

        by_close_reason = {}
        for row in by_reason_rows:
            by_close_reason[row[0]] = {
                "trades":           row[1],
                "avg_pnl":          round(row[2] or 0, 2),
                "avg_pct_captured": round(row[3] or 0, 1) if row[3] is not None else None,
            }

        return {
            "total_trades":     total,
            "winning_trades":   wins,
            "win_rate":         (wins / total) if total > 0 else 0,
            "total_pnl":        round(overall[2] or 0, 2),
            "avg_pnl":          round(overall[3] or 0, 2),
            "avg_credit":       round(overall[4] or 0, 4),
            "avg_max_loss":     round(overall[5] or 0, 2),
            "best_trade":       round(overall[6] or 0, 2),
            "worst_trade":      round(overall[7] or 0, 2),
            "avg_days_held":    round(overall[8], 1)  if overall[8]  is not None else None,
            "avg_dte_at_close": round(overall[9], 1)  if overall[9]  is not None else None,
            "avg_pct_captured": round(overall[10], 1) if overall[10] is not None else None,
            "avg_iv_crush":     round(overall[11], 2) if overall[11] is not None else None,
            "avg_signal_score": round(overall[12], 4) if overall[12] is not None else None,
            "open_count":       open_count,
            "by_strategy":      by_strategy,
            "by_close_reason":  by_close_reason,
        }

    # ── Private Helpers ───────────────────────────────────────────

    def _init_db(self) -> None:
        """Create paper_trades table with the full current schema if it doesn\'t exist."""
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    -- Core
                    symbol               TEXT    NOT NULL,
                    strategy_name        TEXT    NOT NULL,
                    signal_type          TEXT    NOT NULL,
                    sell_contract        TEXT    NOT NULL,
                    buy_contract         TEXT,
                    quantity             INTEGER NOT NULL DEFAULT 1,
                    sell_price           REAL    NOT NULL,
                    buy_price            REAL,
                    net_credit           REAL    NOT NULL,
                    max_profit           REAL,
                    max_loss             REAL,
                    breakeven            REAL,
                    expiry               TEXT    NOT NULL,
                    dte_at_open          INTEGER,
                    iv_rank              REAL,
                    delta                REAL,
                    regime               TEXT,
                    opened_at            TEXT    NOT NULL,
                    status               TEXT    NOT NULL DEFAULT \'open\',
                    -- Phase 9 entry context
                    spot_price_at_open   REAL,
                    buffer_pct           REAL,
                    reward_risk          REAL,
                    rsi_at_open          REAL,
                    pct_b_at_open        REAL,
                    macd_at_open         REAL,
                    vix_at_open          REAL,
                    -- Phase 9h analytics at open
                    short_strike         REAL,
                    long_strike          REAL,
                    atm_iv_at_open       REAL,
                    theta_at_open        REAL,
                    vega_at_open         REAL,
                    signal_score         REAL,
                    entry_type           TEXT,
                    -- Exit fields
                    close_price          REAL,
                    closed_at            TEXT,
                    close_reason         TEXT,
                    pnl                  REAL,
                    days_held            INTEGER,
                    dte_at_close         INTEGER,
                    spot_price_at_close  REAL,
                    iv_rank_at_close     REAL,
                    vix_at_close         REAL,
                    pct_premium_captured REAL,
                    -- Phase 9h analytics at close
                    atm_iv_at_close      REAL,
                    rsi_at_close         REAL,
                    pct_b_at_close       REAL,
                    spot_change_pct      REAL,
                    buffer_at_close      REAL,
                    -- Live trading
                    commission           REAL    NOT NULL DEFAULT 0.0,
                    pnl_net              REAL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_pt_symbol
                ON paper_trades (symbol, strategy_name, status)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_pt_status
                ON paper_trades (status, opened_at)
            """)

    def _migrate_db(self) -> None:
        """
        Add any missing columns to pre-existing databases.
        Idempotent — safe to run on every start.
        """
        columns = [
            ("spot_price_at_open",  "REAL"),
            ("buffer_pct",          "REAL"),
            ("reward_risk",         "REAL"),
            ("rsi_at_open",         "REAL"),
            ("pct_b_at_open",       "REAL"),
            ("macd_at_open",        "REAL"),
            ("vix_at_open",         "REAL"),
            ("short_strike",        "REAL"),
            ("long_strike",         "REAL"),
            ("atm_iv_at_open",      "REAL"),
            ("theta_at_open",       "REAL"),
            ("vega_at_open",        "REAL"),
            ("signal_score",        "REAL"),
            ("entry_type",          "TEXT"),
            ("days_held",           "INTEGER"),
            ("dte_at_close",        "INTEGER"),
            ("spot_price_at_close", "REAL"),
            ("iv_rank_at_close",    "REAL"),
            ("vix_at_close",        "REAL"),
            ("pct_premium_captured","REAL"),
            ("atm_iv_at_close",     "REAL"),
            ("rsi_at_close",        "REAL"),
            ("pct_b_at_close",      "REAL"),
            ("spot_change_pct",     "REAL"),
            ("buffer_at_close",     "REAL"),
            ("commission",          "REAL DEFAULT 0.0"),
            ("pnl_net",             "REAL"),
        ]
        with self._get_conn() as conn:
            for col_name, col_type in columns:
                try:
                    conn.execute(
                        f"ALTER TABLE paper_trades ADD COLUMN {col_name} {col_type}"
                    )
                    logger.info(f"Migration: added column {col_name}")
                except Exception:
                    pass

    def _get_conn(self):
        """
        Context manager that opens, yields, and CLOSES a SQLite connection.
        Guarantees the file descriptor is released on every call, preventing
        OS fd exhaustion after long bot runtimes.

        Usage (unchanged from caller side):
            with self._get_conn() as conn:
                conn.execute(...)
        """
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            conn = sqlite3.connect(
                self._db_path,
                timeout=30,
                check_same_thread=False,
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()   # always release the file descriptor

        return _cm()
