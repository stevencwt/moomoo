"""
Paper Ledger
============
SQLite record of all paper trades during the 4-week validation period.

Purpose:
  - Track all paper trades from signal → fill → close
  - Compute running P&L statistics for validation
  - Provide the data needed to decide "is this strategy ready for live?"
  - Permanent record even if the bot restarts

Schema — paper_trades table:

  Core identity:
    id             : auto-increment PK
    symbol         : "US.TSLA"
    strategy_name  : "covered_call" | "bear_call_spread" | "bull_put_spread"
    signal_type    : same as strategy_name
    status         : "open" | "closed" | "expired"

  Legs & pricing:
    sell_contract  : MooMoo contract code for the short leg
    buy_contract   : protective leg contract (spreads only, None for covered calls)
    quantity       : number of contracts
    sell_price     : fill price of short leg
    buy_price      : fill price of long leg (None for covered calls)
    net_credit     : sell_price - buy_price (per share)
    max_profit     : net_credit × 100 (per contract)
    max_loss       : (spread_width - net_credit) × 100, or None
    breakeven      : breakeven price at expiry

  Expiry:
    expiry         : "YYYY-MM-DD"
    dte_at_open    : days to expiry when opened

  ── Entry conditions (market state when signal was generated) ──
    iv_rank        : IV Rank at entry (0-100)
    delta          : short leg delta at entry
    regime         : market regime at entry ("bull"|"bear"|"neutral"|"high_vol")
    spot_price_at_open  : underlying price at entry
    buffer_pct     : % distance from spot to short strike at entry
    reward_risk    : max_profit / max_loss ratio at entry
    rsi_at_open    : RSI(14) at entry
    pct_b_at_open  : Bollinger %B at entry (0=lower band, 1=upper band)
    macd_at_open   : MACD value at entry
    vix_at_open    : VIX at entry
    opened_at      : datetime opened

  ── Exit conditions (market state when position was closed) ────
    close_price        : net debit paid to close (0 if expired worthless)
    close_reason       : "expired_worthless"|"stop_loss"|"take_profit"|"dte_close"|"manual"
    closed_at          : datetime closed
    pnl                : realised P&L in dollars
    days_held          : calendar days position was held
    dte_at_close       : days to expiry remaining when closed
    spot_price_at_close: underlying price at close
    iv_rank_at_close   : IV Rank at close
    vix_at_close       : VIX at close
    pct_premium_captured: % of original credit kept = (credit-close_price)/credit × 100
"""

import sqlite3
import os
from datetime import date, datetime
from typing import Optional, List, Dict, Any

from src.strategies.trade_signal import TradeSignal
from src.logger import get_logger

logger = get_logger("execution.paper_ledger")


class PaperLedger:
    """
    Persistent SQLite record of paper trades.

    Entry context (RSI, %B, MACD, VIX, spot price) is captured by passing
    the MarketSnapshot to record_open(). Exit context (DTE remaining,
    spot price, IV rank, VIX) is captured by passing keyword args to
    record_close().

    Both are optional — existing code that omits them continues to work,
    the new columns simply receive NULL for those records.
    """

    def __init__(self, db_path: str = "data/paper_trades.db"):
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()
        self._migrate_db()
        logger.info(f"PaperLedger initialised | db={db_path}")

    # ── Public API ────────────────────────────────────────────────

    def record_open(
        self,
        signal:   TradeSignal,
        fill_sell: float,
        fill_buy:  Optional[float] = None,
        snapshot:  Optional[Any]   = None,   # MarketSnapshot — provides entry context
    ) -> int:
        """
        Record a newly opened paper position.

        Args:
            signal    : The TradeSignal that was executed
            fill_sell : Actual fill price of the short leg
            fill_buy  : Actual fill price of the long leg (None for covered calls)
            snapshot  : MarketSnapshot at signal time — captures RSI, %B, MACD,
                        VIX, spot price for post-trade analysis. Optional — omitting
                        it leaves those columns NULL for this record.

        Returns:
            Trade ID (row id in paper_trades table)
        """
        net_credit = fill_sell - (fill_buy or 0)

        # ── Extract entry context from snapshot (if provided) ────
        rsi_at_open       = None
        pct_b_at_open     = None
        macd_at_open      = None
        vix_at_open       = None

        if snapshot is not None:
            try:
                rsi_at_open   = float(snapshot.technicals.rsi)
                pct_b_at_open = float(snapshot.technicals.pct_b)
                macd_at_open  = float(snapshot.technicals.macd)
                vix_at_open   = float(snapshot.vix)
            except Exception as e:
                logger.warning(f"record_open: could not extract snapshot context: {e}")

        # spot_price, buffer_pct, reward_risk come from the TradeSignal directly
        spot_at_open  = signal.spot_price
        buffer_pct    = getattr(signal, "buffer_pct",  None)
        reward_risk   = getattr(signal, "reward_risk", None)

        with self._get_conn() as conn:
            cursor = conn.execute("""
                INSERT INTO paper_trades (
                    symbol, strategy_name, signal_type,
                    sell_contract, buy_contract, quantity,
                    sell_price, buy_price, net_credit,
                    max_profit, max_loss, breakeven,
                    expiry, dte_at_open,
                    iv_rank, delta, regime,
                    spot_price_at_open, buffer_pct, reward_risk,
                    rsi_at_open, pct_b_at_open, macd_at_open, vix_at_open,
                    opened_at, status
                ) VALUES (
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, 'open'
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
                spot_at_open, buffer_pct, reward_risk,
                rsi_at_open, pct_b_at_open, macd_at_open, vix_at_open,
                datetime.now().isoformat(),
            ))
            trade_id = cursor.lastrowid

        logger.info(
            f"[PAPER] Opened trade #{trade_id}: {signal.symbol} "
            f"{signal.strategy_name} | credit=${net_credit:.2f} | "
            f"expiry={signal.expiry} | "
            f"IV={signal.iv_rank:.0f} | RSI={rsi_at_open} | "
            f"buffer={buffer_pct}% | R/R={reward_risk}"
        )
        return trade_id

    def record_close(
        self,
        trade_id:            int,
        close_price:         float,
        close_reason:        str,
        closed_at:           Optional[datetime] = None,
        # ── Exit context ──────────────────────────────────────────
        spot_price_at_close: Optional[float]    = None,
        dte_at_close:        Optional[int]       = None,
        iv_rank_at_close:    Optional[float]     = None,
        vix_at_close:        Optional[float]     = None,
    ) -> float:
        """
        Record the close of a paper position and compute P&L.

        Args:
            trade_id            : ID from record_open()
            close_price         : Net debit paid to close (0 if expired worthless)
            close_reason        : "expired_worthless" | "stop_loss" | "take_profit"
                                  | "dte_close" | "manual"
            closed_at           : Close datetime (defaults to now)
            spot_price_at_close : Underlying price when closed
            dte_at_close        : Days to expiry remaining when closed
            iv_rank_at_close    : IV Rank at close time
            vix_at_close        : VIX at close time

        Returns:
            Realised P&L in dollars.
        """
        valid_reasons = {
            "expired_worthless", "stop_loss", "take_profit", "dte_close", "manual"
        }
        if close_reason not in valid_reasons:
            raise ValueError(
                f"Invalid close_reason '{close_reason}'. "
                f"Must be one of {valid_reasons}"
            )

        close_ts = (closed_at or datetime.now()).isoformat()

        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT net_credit, quantity, opened_at FROM paper_trades WHERE id = ?",
                (trade_id,)
            ).fetchone()

            if not row:
                raise ValueError(f"No paper trade found with id={trade_id}")

            net_credit, quantity, opened_at_str = row

            # ── Derived exit metrics ──────────────────────────────
            pnl = (net_credit - close_price) * 100 * quantity

            # Days held
            try:
                opened_dt = datetime.fromisoformat(opened_at_str)
                closed_dt = closed_at or datetime.now()
                days_held = max(0, (closed_dt.date() - opened_dt.date()).days)
            except Exception:
                days_held = None

            # % of premium captured: positive = profit, negative = loss
            try:
                pct_premium_captured = round(
                    (net_credit - close_price) / net_credit * 100, 1
                ) if net_credit > 0 else None
            except Exception:
                pct_premium_captured = None

            status = "expired" if close_reason == "expired_worthless" else "closed"

            conn.execute("""
                UPDATE paper_trades
                SET close_price          = ?,
                    closed_at            = ?,
                    close_reason         = ?,
                    pnl                  = ?,
                    status               = ?,
                    days_held            = ?,
                    dte_at_close         = ?,
                    spot_price_at_close  = ?,
                    iv_rank_at_close     = ?,
                    vix_at_close         = ?,
                    pct_premium_captured = ?
                WHERE id = ?
            """, (
                round(close_price, 4),
                close_ts,
                close_reason,
                round(pnl, 2),
                status,
                days_held,
                dte_at_close,
                spot_price_at_close,
                iv_rank_at_close,
                vix_at_close,
                pct_premium_captured,
                trade_id,
            ))

        logger.info(
            f"[PAPER] Closed trade #{trade_id}: "
            f"reason={close_reason} | close_price=${close_price:.2f} | "
            f"P&L=${pnl:+.2f} | days_held={days_held} | "
            f"dte_remaining={dte_at_close} | "
            f"pct_captured={pct_premium_captured}%"
        )
        return pnl

    def get_open_trades(self) -> List[Dict]:
        """Return all currently open paper trades with full entry context."""
        with self._get_conn() as conn:
            cursor = conn.execute("""
                SELECT *
                FROM paper_trades
                WHERE status = 'open'
                ORDER BY opened_at DESC
            """)
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def get_closed_trades(self, limit: int = 100) -> List[Dict]:
        """Return closed/expired trades most-recent-first, with full entry+exit context."""
        with self._get_conn() as conn:
            cursor = conn.execute("""
                SELECT *
                FROM paper_trades
                WHERE status IN ('closed', 'expired')
                ORDER BY closed_at DESC
                LIMIT ?
            """, (limit,))
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def get_all_trades(self, limit: int = 200) -> List[Dict]:
        """Return all trades (open + closed) most-recent-first."""
        with self._get_conn() as conn:
            cursor = conn.execute("""
                SELECT *
                FROM paper_trades
                ORDER BY opened_at DESC
                LIMIT ?
            """, (limit,))
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def get_statistics(self) -> Dict:
        """
        Compute validation statistics for the paper trading period.

        Returns dict with:
          total_trades        : Total closed trades
          winning_trades      : Trades with P&L > 0
          win_rate            : Winning / total (0-1)
          total_pnl           : Sum of all P&L
          avg_pnl             : Average P&L per trade
          avg_credit          : Average credit collected
          avg_max_loss        : Average max risk per trade
          best_trade          : Highest P&L trade
          worst_trade         : Lowest P&L trade
          avg_days_held       : Average holding period
          avg_dte_at_close    : Average DTE remaining when closed
          avg_pct_captured    : Average % of premium captured on winners
          open_count          : Currently open positions
          by_strategy         : Per-strategy breakdown
          by_close_reason     : Breakdown by exit reason
        """
        with self._get_conn() as conn:
            overall = conn.execute("""
                SELECT
                    COUNT(*)                                        AS total_trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)       AS winning_trades,
                    SUM(pnl)                                        AS total_pnl,
                    AVG(pnl)                                        AS avg_pnl,
                    AVG(net_credit)                                 AS avg_credit,
                    AVG(max_loss)                                   AS avg_max_loss,
                    MAX(pnl)                                        AS best_trade,
                    MIN(pnl)                                        AS worst_trade,
                    AVG(days_held)                                  AS avg_days_held,
                    AVG(dte_at_close)                               AS avg_dte_at_close,
                    AVG(CASE WHEN pnl > 0 THEN pct_premium_captured END) AS avg_pct_captured
                FROM paper_trades
                WHERE status IN ('closed', 'expired')
                  AND pnl IS NOT NULL
            """).fetchone()

            open_count = conn.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE status = 'open'"
            ).fetchone()[0]

            by_strategy_rows = conn.execute("""
                SELECT
                    strategy_name,
                    COUNT(*)          AS trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(pnl)          AS total_pnl,
                    AVG(pnl)          AS avg_pnl,
                    AVG(days_held)    AS avg_days_held
                FROM paper_trades
                WHERE status IN ('closed', 'expired')
                  AND pnl IS NOT NULL
                GROUP BY strategy_name
            """).fetchall()

            by_reason_rows = conn.execute("""
                SELECT
                    close_reason,
                    COUNT(*)     AS trades,
                    SUM(pnl)     AS total_pnl,
                    AVG(pnl)     AS avg_pnl
                FROM paper_trades
                WHERE status IN ('closed', 'expired')
                  AND pnl IS NOT NULL
                GROUP BY close_reason
            """).fetchall()

        total = overall[0] or 0
        wins  = overall[1] or 0

        by_strategy = {}
        for row in by_strategy_rows:
            by_strategy[row[0]] = {
                "trades":        row[1],
                "wins":          row[2],
                "win_rate":      row[2] / row[1] if row[1] > 0 else 0,
                "total_pnl":     round(row[3] or 0, 2),
                "avg_pnl":       round(row[4] or 0, 2),
                "avg_days_held": round(row[5] or 0, 1) if row[5] else None,
            }

        by_close_reason = {}
        for row in by_reason_rows:
            by_close_reason[row[0]] = {
                "trades":    row[1],
                "total_pnl": round(row[2] or 0, 2),
                "avg_pnl":   round(row[3] or 0, 2),
            }

        return {
            "total_trades":       total,
            "winning_trades":     wins,
            "win_rate":           (wins / total) if total > 0 else 0,
            "total_pnl":          round(overall[2] or 0, 2),
            "avg_pnl":            round(overall[3] or 0, 2),
            "avg_credit":         round(overall[4] or 0, 4),
            "avg_max_loss":       round(overall[5] or 0, 2),
            "best_trade":         round(overall[6] or 0, 2),
            "worst_trade":        round(overall[7] or 0, 2),
            "avg_days_held":      round(overall[8] or 0, 1) if overall[8] else None,
            "avg_dte_at_close":   round(overall[9] or 0, 1) if overall[9] else None,
            "avg_pct_captured":   round(overall[10] or 0, 1) if overall[10] else None,
            "open_count":         open_count,
            "by_strategy":        by_strategy,
            "by_close_reason":    by_close_reason,
        }

    def get_trades_opened_on(self, date_str: str) -> List[Dict]:
        """Return all trades (any status) opened on the given date.

        Args:
            date_str: ISO date string, e.g. "2026-03-03"

        Used by PortfolioGuard.restore_from_ledger() to count how many
        trades were already placed today (including those subsequently
        stopped out or closed), so the daily trade limit is respected
        across bot restarts.
        """
        with self._get_conn() as conn:
            cursor = conn.execute(
                """
                SELECT *
                FROM paper_trades
                WHERE opened_at LIKE ?
                ORDER BY opened_at ASC
                """,
                (f"{date_str}%",),
            )
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def get_trade(self, trade_id: int) -> Optional[Dict]:
        """Return a single trade by ID with all fields."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "SELECT * FROM paper_trades WHERE id = ?", (trade_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))

    # ── Private Helpers ───────────────────────────────────────────

    def _init_db(self) -> None:
        """Create table and indexes if they don't exist."""
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol          TEXT    NOT NULL,
                    strategy_name   TEXT    NOT NULL,
                    signal_type     TEXT    NOT NULL,
                    sell_contract   TEXT    NOT NULL,
                    buy_contract    TEXT,
                    quantity        INTEGER NOT NULL DEFAULT 1,
                    sell_price      REAL    NOT NULL,
                    buy_price       REAL,
                    net_credit      REAL    NOT NULL,
                    max_profit      REAL,
                    max_loss        REAL,
                    breakeven       REAL,
                    expiry          TEXT    NOT NULL,
                    dte_at_open     INTEGER,

                    -- Entry conditions
                    iv_rank             REAL,
                    delta               REAL,
                    regime              TEXT,
                    spot_price_at_open  REAL,
                    buffer_pct          REAL,
                    reward_risk         REAL,
                    rsi_at_open         REAL,
                    pct_b_at_open       REAL,
                    macd_at_open        REAL,
                    vix_at_open         REAL,
                    opened_at           TEXT NOT NULL,

                    -- Exit conditions
                    close_price          REAL,
                    close_reason         TEXT,
                    closed_at            TEXT,
                    pnl                  REAL,
                    status               TEXT NOT NULL DEFAULT 'open',
                    days_held            INTEGER,
                    dte_at_close         INTEGER,
                    spot_price_at_close  REAL,
                    iv_rank_at_close     REAL,
                    vix_at_close         REAL,
                    pct_premium_captured REAL
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
        Add new columns to existing databases that were created before
        the entry/exit context fields were introduced.

        Uses try/except on each ALTER TABLE — SQLite raises OperationalError
        if the column already exists, which we silently ignore.
        """
        new_columns = [
            # Entry context
            ("spot_price_at_open",  "REAL"),
            ("buffer_pct",          "REAL"),
            ("reward_risk",         "REAL"),
            ("rsi_at_open",         "REAL"),
            ("pct_b_at_open",       "REAL"),
            ("macd_at_open",        "REAL"),
            ("vix_at_open",         "REAL"),
            # Exit context
            ("days_held",            "INTEGER"),
            ("dte_at_close",         "INTEGER"),
            ("spot_price_at_close",  "REAL"),
            ("iv_rank_at_close",     "REAL"),
            ("vix_at_close",         "REAL"),
            ("pct_premium_captured", "REAL"),
        ]
        with self._get_conn() as conn:
            for col_name, col_type in new_columns:
                try:
                    conn.execute(
                        f"ALTER TABLE paper_trades ADD COLUMN {col_name} {col_type}"
                    )
                    logger.info(f"Migration: added column {col_name} to paper_trades")
                except Exception:
                    pass   # Column already exists — expected on subsequent starts

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
