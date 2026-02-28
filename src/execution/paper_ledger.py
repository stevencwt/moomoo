"""
Paper Ledger
============
SQLite record of all paper trades during the 4-week validation period.

Purpose:
  - Track all paper trades from signal → fill → close
  - Compute running P&L statistics for validation
  - Provide the data needed to decide "is this strategy ready for live?"
  - Permanent record even if the bot restarts

Schema:
  paper_trades table:
    id             : auto-increment PK
    symbol         : "US.TSLA"
    strategy_name  : "covered_call" | "bear_call_spread"
    signal_type    : same as strategy_name
    sell_contract  : MooMoo contract code
    buy_contract   : None for covered calls
    quantity       : number of contracts
    sell_price     : actual fill price of short leg
    buy_price      : actual fill price of long leg (None for covered calls)
    net_credit     : sell_price - buy_price
    max_profit     : net_credit × 100
    max_loss       : (spread_width - net_credit) × 100 or None
    breakeven      : breakeven price at expiry
    expiry         : "YYYY-MM-DD"
    dte_at_open    : days to expiry when opened
    iv_rank        : IV rank at signal time
    delta          : short leg delta at signal time
    regime         : market regime at signal time
    opened_at      : datetime opened
    close_price    : net debit paid to close (None if expired)
    closed_at      : datetime closed (None if still open)
    close_reason   : "expired_worthless" | "stop_loss" | "take_profit" | "manual"
    pnl            : realised P&L in dollars (None until closed)
    status         : "open" | "closed" | "expired"

  validation_summary view:
    Computed stats for the validation report
"""

import sqlite3
import os
from datetime import date, datetime
from typing import Optional, List, Dict

from src.strategies.trade_signal import TradeSignal
from src.logger import get_logger

logger = get_logger("execution.paper_ledger")


class PaperLedger:
    """
    Persistent SQLite record of paper trades.
    """

    def __init__(self, db_path: str = "data/paper_trades.db"):
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()
        logger.info(f"PaperLedger initialised | db={db_path}")

    # ── Public API ────────────────────────────────────────────────

    def record_open(self, signal: TradeSignal, fill_sell: float,
                    fill_buy: Optional[float] = None) -> int:
        """
        Record a newly opened paper position.

        Args:
            signal    : The TradeSignal that was executed
            fill_sell : Actual fill price of the short leg
            fill_buy  : Actual fill price of the long leg (None for covered calls)

        Returns:
            Trade ID (row id in paper_trades table)
        """
        net_credit = fill_sell - (fill_buy or 0)

        with self._get_conn() as conn:
            cursor = conn.execute("""
                INSERT INTO paper_trades (
                    symbol, strategy_name, signal_type,
                    sell_contract, buy_contract, quantity,
                    sell_price, buy_price, net_credit,
                    max_profit, max_loss, breakeven,
                    expiry, dte_at_open, iv_rank, delta, regime,
                    opened_at, status
                ) VALUES (
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?, ?,
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
                datetime.now().isoformat(),
            ))
            trade_id = cursor.lastrowid

        logger.info(
            f"[PAPER] Opened trade #{trade_id}: {signal.symbol} "
            f"{signal.strategy_name} | credit=${net_credit:.2f} | "
            f"expiry={signal.expiry}"
        )
        return trade_id

    def record_close(
        self,
        trade_id:     int,
        close_price:  float,
        close_reason: str,
        closed_at:    Optional[datetime] = None
    ) -> float:
        """
        Record the close of a paper position and compute P&L.

        Args:
            trade_id    : ID from record_open()
            close_price : Net debit paid to close (0 if expired worthless)
            close_reason: "expired_worthless" | "stop_loss" | "take_profit" | "manual"
            closed_at   : Close datetime (defaults to now)

        Returns:
            Realised P&L in dollars.
        """
        valid_reasons = {"expired_worthless", "stop_loss", "take_profit", "manual"}
        if close_reason not in valid_reasons:
            raise ValueError(
                f"Invalid close_reason '{close_reason}'. "
                f"Must be one of {valid_reasons}"
            )

        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT net_credit, quantity FROM paper_trades WHERE id = ?",
                (trade_id,)
            ).fetchone()

            if not row:
                raise ValueError(f"No paper trade found with id={trade_id}")

            net_credit, quantity = row
            # P&L = (credit received - debit to close) × 100 × contracts
            pnl = (net_credit - close_price) * 100 * quantity

            status = "expired" if close_reason == "expired_worthless" else "closed"

            conn.execute("""
                UPDATE paper_trades
                SET close_price = ?,
                    closed_at   = ?,
                    close_reason = ?,
                    pnl         = ?,
                    status      = ?
                WHERE id = ?
            """, (
                round(close_price, 4),
                (closed_at or datetime.now()).isoformat(),
                close_reason,
                round(pnl, 2),
                status,
                trade_id
            ))

        logger.info(
            f"[PAPER] Closed trade #{trade_id}: "
            f"reason={close_reason} | close_price=${close_price:.2f} | "
            f"P&L=${pnl:+.2f}"
        )
        return pnl

    def get_open_trades(self) -> List[Dict]:
        """Return all currently open paper trades."""
        with self._get_conn() as conn:
            cursor = conn.execute("""
                SELECT id, symbol, strategy_name, sell_contract, buy_contract,
                       net_credit, max_loss, expiry, opened_at, iv_rank, delta
                FROM paper_trades
                WHERE status = 'open'
                ORDER BY opened_at DESC
            """)
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def get_trades_opened_on(self, date_str: str) -> List[Dict]:
        """
        Return all trades opened on a given date (any status: open, closed, expired).
        Used by PortfolioGuard to count daily trades including stopped-out positions.

        Args:
            date_str: ISO date string e.g. "2026-02-28"
        """
        with self._get_conn() as conn:
            cursor = conn.execute("""
                SELECT id, symbol, strategy_name, sell_contract, opened_at, status
                FROM paper_trades
                WHERE substr(opened_at, 1, 10) = ?
                ORDER BY opened_at ASC
            """, (date_str,))
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def get_statistics(self) -> Dict:
        """
        Compute validation statistics for the paper trading period.

        Returns dict with:
          total_trades    : Total closed trades
          winning_trades  : Trades with P&L > 0
          win_rate        : Winning / total (0-1)
          total_pnl       : Sum of all P&L
          avg_pnl         : Average P&L per trade
          avg_credit      : Average credit collected
          avg_max_loss    : Average max risk per trade
          best_trade      : Highest P&L trade
          worst_trade     : Lowest P&L trade
          open_count      : Currently open positions
          by_strategy     : Per-strategy breakdown
        """
        with self._get_conn() as conn:
            # Overall stats
            overall = conn.execute("""
                SELECT
                    COUNT(*)                           AS total_trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS winning_trades,
                    SUM(pnl)                           AS total_pnl,
                    AVG(pnl)                           AS avg_pnl,
                    AVG(net_credit)                    AS avg_credit,
                    AVG(max_loss)                      AS avg_max_loss,
                    MAX(pnl)                           AS best_trade,
                    MIN(pnl)                           AS worst_trade
                FROM paper_trades
                WHERE status IN ('closed', 'expired')
                  AND pnl IS NOT NULL
            """).fetchone()

            open_count = conn.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE status = 'open'"
            ).fetchone()[0]

            # Per-strategy breakdown
            by_strategy_rows = conn.execute("""
                SELECT
                    strategy_name,
                    COUNT(*)          AS trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(pnl)          AS total_pnl,
                    AVG(pnl)          AS avg_pnl
                FROM paper_trades
                WHERE status IN ('closed', 'expired')
                  AND pnl IS NOT NULL
                GROUP BY strategy_name
            """).fetchall()

        total = overall[0] or 0
        wins  = overall[1] or 0

        by_strategy = {}
        for row in by_strategy_rows:
            by_strategy[row[0]] = {
                "trades":    row[1],
                "wins":      row[2],
                "win_rate":  row[2] / row[1] if row[1] > 0 else 0,
                "total_pnl": round(row[3] or 0, 2),
                "avg_pnl":   round(row[4] or 0, 2),
            }

        return {
            "total_trades":  total,
            "winning_trades": wins,
            "win_rate":      (wins / total) if total > 0 else 0,
            "total_pnl":     round(overall[2] or 0, 2),
            "avg_pnl":       round(overall[3] or 0, 2),
            "avg_credit":    round(overall[4] or 0, 4),
            "avg_max_loss":  round(overall[5] or 0, 2),
            "best_trade":    round(overall[6] or 0, 2),
            "worst_trade":   round(overall[7] or 0, 2),
            "open_count":    open_count,
            "by_strategy":   by_strategy,
        }

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

    # ── Private Helpers ───────────────────────────────────────────

    def _init_db(self) -> None:
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
                    iv_rank         REAL,
                    delta           REAL,
                    regime          TEXT,
                    opened_at       TEXT    NOT NULL,
                    close_price     REAL,
                    closed_at       TEXT,
                    close_reason    TEXT,
                    pnl             REAL,
                    status          TEXT    NOT NULL DEFAULT 'open'
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

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
