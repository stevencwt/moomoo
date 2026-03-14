#!/usr/bin/env python3
"""
analytics_report.py — Options Bot Analytics (Phase 1: CLI)
===========================================================
Reads paper_trades.db and prints all analytics sections to stdout.

Usage:
    python3 analytics_report.py                        # auto-discovers DB via config.yaml
    python3 analytics_report.py --db data/paper_trades.db
    python3 analytics_report.py --db data/paper_trades.db --no-color
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple


# ── ANSI colours ─────────────────────────────────────────────────────────────

USE_COLOR = True

def _c(code: str, text: str) -> str:
    if not USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"

def green(s):   return _c("92", s)
def red(s):     return _c("91", s)
def yellow(s):  return _c("93", s)
def cyan(s):    return _c("96", s)
def grey(s):    return _c("90", s)
def bold(s):    return _c("1",  s)
def dim(s):     return _c("2",  s)


# ── Formatting helpers ────────────────────────────────────────────────────────

def fmt_pnl(val: Optional[float]) -> str:
    if val is None:
        return grey("—")
    sign = "+" if val >= 0 else ""
    s = f"{sign}${val:.2f}"
    return green(s) if val > 0 else (red(s) if val < 0 else grey(s))

def fmt_pct(val: Optional[float], suffix="%") -> str:
    if val is None:
        return grey("—")
    return f"{val:.1f}{suffix}"

def fmt_num(val: Optional[float], decimals=1) -> str:
    if val is None:
        return grey("—")
    return f"{val:.{decimals}f}"

def fmt_win_rate(wins: int, total: int) -> str:
    if total == 0:
        return grey("—")
    pct = wins / total * 100
    s = f"{pct:.0f}%"
    return green(s) if pct >= 60 else (yellow(s) if pct >= 40 else red(s))

def fmt_iv_crush(val: Optional[float]) -> str:
    if val is None:
        return grey("—")
    s = f"{val:+.1f}%"
    return green(s) if val > 3 else (grey(s) if val >= -1 else red(s))

def fmt_buffer(val: Optional[float]) -> str:
    if val is None:
        return grey("—")
    s = f"{val:+.1f}%"
    return green(s) if val >= 3 else (yellow(s) if val >= 1 else red(s))

def fmt_theta_real(val: Optional[float]) -> str:
    if val is None:
        return grey("—")
    s = f"{val:.0f}%"
    return green(s) if val >= 100 else (yellow(s) if val >= 60 else red(s))


# ── Table printer ─────────────────────────────────────────────────────────────

def _col_widths(headers: List[str], rows: List[List[str]]) -> List[int]:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            # strip ANSI for width calculation
            import re
            clean = re.sub(r'\033\[[0-9;]*m', '', str(cell))
            if i < len(widths):
                widths[i] = max(widths[i], len(clean))
    return widths

def print_table(headers: List[str], rows: List[List[Any]],
                title: str = "", note: str = "") -> None:
    if title:
        print(f"\n{bold(cyan('  ▸ ' + title))}")
    if not rows:
        print(dim("    — no data yet (need trades with analytics fields populated) —\n"))
        return

    str_rows = [[str(c) for c in r] for r in rows]
    widths = _col_widths(headers, str_rows)

    import re
    def pad(text: str, width: int) -> str:
        clean_len = len(re.sub(r'\033\[[0-9;]*m', '', text))
        return text + " " * (width - clean_len)

    sep = "  " + "─" * (sum(widths) + len(widths) * 3 + 1)
    header_row = "  │ " + " │ ".join(
        bold(h.ljust(widths[i])) for i, h in enumerate(headers)
    ) + " │"

    print(sep)
    print(header_row)
    print(sep)
    for row in str_rows:
        cells = " │ ".join(pad(cell, widths[i]) for i, cell in enumerate(row))
        print(f"  │ {cells} │")
    print(sep)
    if note:
        print(f"  {dim(note)}")
    print()


def section(title: str) -> None:
    width = 72
    print()
    print(bold(f"{'━' * width}"))
    print(bold(f"  {title}"))
    print(bold(f"{'━' * width}"))


# ── Database connection ───────────────────────────────────────────────────────

def get_conn(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        print(red(f"\n✗ Database not found: {db_path}"))
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def q(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []   # column doesn't exist yet — pre-migration DB


def q1(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    try:
        rows = conn.execute(sql, params).fetchall()
        return rows[0] if rows else None
    except sqlite3.OperationalError:
        return None  # column doesn't exist yet — pre-migration DB


# ── Analytics sections ────────────────────────────────────────────────────────

def section_overview(conn):
    section("OVERVIEW")
    row = q1(conn, """
        SELECT
            COUNT(*) AS total_closed,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(pnl) AS total_pnl,
            AVG(pnl) AS avg_pnl,
            AVG(pct_premium_captured) AS avg_captured,
            AVG(days_held) AS avg_days,
            MAX(pnl) AS best,
            MIN(pnl) AS worst
        FROM paper_trades
        WHERE status IN ('closed','expired') AND pnl IS NOT NULL
    """)
    open_ct = q1(conn, "SELECT COUNT(*) AS n FROM paper_trades WHERE status='open'")["n"]

    total = row["total_closed"] or 0
    wins  = row["wins"] or 0
    wr    = wins / total if total else 0

    print(f"\n  Open positions  : {cyan(str(open_ct))}")
    print(f"  Closed trades   : {bold(str(total))}")
    print(f"  Win rate        : {green(f'{wr*100:.0f}%') if wr >= 0.6 else yellow(f'{wr*100:.0f}%')}  ({wins}/{total})")
    print(f"  Total P&L       : {fmt_pnl(row['total_pnl'])}")
    print(f"  Avg P&L / trade : {fmt_pnl(row['avg_pnl'])}")
    print(f"  Avg captured    : {fmt_pct(row['avg_captured'])}")
    print(f"  Avg days held   : {fmt_num(row['avg_days'], 1)}")
    print(f"  Best trade      : {fmt_pnl(row['best'])}")
    print(f"  Worst trade     : {fmt_pnl(row['worst'])}")


def section_exit_type(conn):
    section("1 · EXIT TYPE BREAKDOWN")
    rows = q(conn, """
        SELECT close_reason,
               COUNT(*) AS trades,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
               ROUND(AVG(pnl), 2) AS avg_pnl,
               ROUND(SUM(pnl), 2) AS total_pnl,
               ROUND(AVG(pct_premium_captured), 1) AS avg_captured
        FROM paper_trades
        WHERE status IN ('closed','expired') AND pnl IS NOT NULL
        GROUP BY close_reason
        ORDER BY avg_pnl DESC
    """)
    table = []
    for r in rows:
        table.append([
            bold(r["close_reason"] or "—"),
            str(r["trades"]),
            fmt_win_rate(r["wins"], r["trades"]),
            fmt_pnl(r["avg_pnl"]),
            fmt_pnl(r["total_pnl"]),
            fmt_pct(r["avg_captured"]),
        ])
    print_table(
        ["Exit Reason", "Trades", "Win%", "Avg P&L", "Total P&L", "Avg Captured%"],
        table,
        note="Insight: take_profit exits should dominate avg P&L vs dte_close."
    )


def section_symbol(conn):
    section("2 · SYMBOL PERFORMANCE")
    rows = q(conn, """
        SELECT REPLACE(symbol,'US.','') AS sym,
               COUNT(*) AS trades,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
               ROUND(AVG(pnl), 2) AS avg_pnl,
               ROUND(SUM(pnl), 2) AS total_pnl,
               ROUND(AVG(days_held), 1) AS avg_days,
               ROUND(AVG(buffer_pct), 1) AS avg_buf
        FROM paper_trades
        WHERE status IN ('closed','expired') AND pnl IS NOT NULL
        GROUP BY symbol
        ORDER BY avg_pnl DESC
    """)
    table = []
    for r in rows:
        table.append([
            bold(r["sym"]),
            str(r["trades"]),
            fmt_win_rate(r["wins"], r["trades"]),
            fmt_pnl(r["avg_pnl"]),
            fmt_pnl(r["total_pnl"]),
            fmt_num(r["avg_days"]),
            fmt_pct(r["avg_buf"]) if r["avg_buf"] is not None else grey("—"),
        ])
    print_table(
        ["Symbol", "Trades", "Win%", "Avg P&L", "Total P&L", "Avg Days", "Avg Buffer%"],
        table,
        note="Insight: symbols with avg buffer <3% may need wider strike selection."
    )


def section_iv_crush(conn):
    section("3 · IV CRUSH CONTRIBUTION")
    rows = q(conn, """
        SELECT REPLACE(symbol,'US.','') AS sym,
               ROUND(atm_iv_at_open, 1) AS iv_open,
               ROUND(atm_iv_at_close, 1) AS iv_close,
               ROUND(atm_iv_at_open - atm_iv_at_close, 1) AS iv_crush,
               ROUND((atm_iv_at_open - atm_iv_at_close)
                     / NULLIF(atm_iv_at_open,0) * 100, 1) AS crush_pct,
               ROUND(pct_premium_captured, 1) AS captured,
               ROUND(pnl, 2) AS pnl,
               CASE WHEN pnl > 0 THEN 'WIN' ELSE 'LOSS' END AS result
        FROM paper_trades
        WHERE status IN ('closed','expired')
          AND atm_iv_at_open IS NOT NULL
          AND atm_iv_at_close IS NOT NULL
        ORDER BY iv_crush DESC
    """)
    table = []
    for r in rows:
        result_str = green("WIN") if r["result"] == "WIN" else red("LOSS")
        table.append([
            bold(r["sym"]),
            fmt_pct(r["iv_open"]),
            fmt_pct(r["iv_close"]),
            fmt_iv_crush(r["iv_crush"]),
            fmt_pct(r["crush_pct"]),
            fmt_pct(r["captured"]),
            fmt_pnl(r["pnl"]),
            result_str,
        ])
    print_table(
        ["Symbol", "IV@Open", "IV@Close", "IV Crush", "Crush%", "Captured%", "P&L", "Result"],
        table,
        note="Insight: crush >5% means IV timing is adding alpha beyond pure theta decay."
    )


def section_theta_realisation(conn):
    section("4 · THETA REALISATION RATE")
    rows = q(conn, """
        SELECT REPLACE(symbol,'US.','') AS sym,
               ROUND(theta_at_open * -100, 2) AS theta_daily,
               days_held,
               ROUND(theta_at_open * -100 * days_held, 2) AS theoretical_pnl,
               ROUND(pnl, 2) AS actual_pnl,
               ROUND(pnl / NULLIF(theta_at_open * -100 * days_held, 0) * 100, 1) AS realisation_pct,
               CASE WHEN pnl > 0 THEN 'WIN' ELSE 'LOSS' END AS result
        FROM paper_trades
        WHERE status IN ('closed','expired')
          AND theta_at_open IS NOT NULL
          AND days_held > 0
          AND pnl IS NOT NULL
        ORDER BY realisation_pct DESC
    """)
    table = []
    for r in rows:
        result_str = green("WIN") if r["result"] == "WIN" else red("LOSS")
        table.append([
            bold(r["sym"]),
            f"${r['theta_daily']:.2f}/day",
            str(r["days_held"]),
            fmt_pnl(r["theoretical_pnl"]),
            fmt_pnl(r["actual_pnl"]),
            fmt_theta_real(r["realisation_pct"]),
            result_str,
        ])
    print_table(
        ["Symbol", "Theta/Day", "Days", "Θ-Theoretical", "Actual P&L", "Realisation%", "Result"],
        table,
        note=">100% = IV crush added on top of theta | <100% = IV expanded or exited early."
    )


def section_signal_score(conn):
    section("5 · SIGNAL SCORE vs OUTCOME")
    rows = q(conn, """
        SELECT REPLACE(symbol,'US.','') AS sym,
               ROUND(signal_score, 4) AS score,
               entry_type,
               ROUND(pct_premium_captured, 1) AS captured,
               close_reason,
               ROUND(pnl, 2) AS pnl,
               CASE WHEN pnl > 0 THEN 'WIN' ELSE 'LOSS' END AS result
        FROM paper_trades
        WHERE status IN ('closed','expired')
          AND signal_score IS NOT NULL
          AND pnl IS NOT NULL
        ORDER BY score DESC
    """)
    table = []
    for r in rows:
        result_str = green("WIN") if r["result"] == "WIN" else red("LOSS")
        table.append([
            bold(r["sym"]),
            yellow(f"{r['score']:.4f}"),
            r["entry_type"] or grey("—"),
            fmt_pct(r["captured"]),
            r["close_reason"] or grey("—"),
            fmt_pnl(r["pnl"]),
            result_str,
        ])
    print_table(
        ["Symbol", "Score", "Entry Type", "Captured%", "Exit Reason", "P&L", "Result"],
        table,
        note="Insight: if high-scored trades consistently win → ranking formula validated."
    )


def section_pctb_zones(conn):
    section("6 · %%B ENTRY ZONE vs OUTCOME")
    rows = q(conn, """
        SELECT
          CASE
            WHEN pct_b_at_open >= 0.80 THEN '≥0.80  (overbought)'
            WHEN pct_b_at_open >= 0.60 THEN '0.60–0.80 (upper-mid)'
            WHEN pct_b_at_open >= 0.40 THEN '0.40–0.60 (mid-band)'
            ELSE                             '<0.40  (lower-band)'
          END AS zone,
          COUNT(*) AS trades,
          SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
          ROUND(AVG(pnl), 2) AS avg_pnl,
          ROUND(AVG(pct_premium_captured), 1) AS avg_captured
        FROM paper_trades
        WHERE status IN ('closed','expired')
          AND pnl IS NOT NULL
          AND pct_b_at_open IS NOT NULL
        GROUP BY zone
        ORDER BY avg_pnl DESC
    """)
    table = []
    for r in rows:
        table.append([
            bold(r["zone"]),
            str(r["trades"]),
            fmt_win_rate(r["wins"], r["trades"]),
            fmt_pnl(r["avg_pnl"]),
            fmt_pct(r["avg_captured"]),
        ])
    print_table(
        ["%%B Zone at Entry", "Trades", "Win%", "Avg P&L", "Avg Captured%"],
        table,
        note="Insight: if ≥0.80 zone dominates → consider raising min_pct_b from 0.40 → 0.60."
    )


def section_near_miss(conn):
    section("7 · NEAR-MISS ANALYSIS  (buffer at close)")
    rows = q(conn, """
        SELECT REPLACE(symbol,'US.','') AS sym,
               ROUND(short_strike, 0) AS short_str,
               ROUND(spot_price_at_close, 2) AS spot_close,
               ROUND(buffer_at_close, 2) AS buf_close,
               ROUND(spot_change_pct, 2) AS spot_chg,
               close_reason,
               ROUND(pnl, 2) AS pnl
        FROM paper_trades
        WHERE status IN ('closed','expired')
          AND buffer_at_close IS NOT NULL
        ORDER BY buf_close ASC
    """)
    table = []
    for r in rows:
        table.append([
            bold(r["sym"]),
            f"${r['short_str']:.0f}" if r["short_str"] else grey("—"),
            f"${r['spot_close']:.2f}" if r["spot_close"] else grey("—"),
            fmt_buffer(r["buf_close"]),
            (green if (r["spot_chg"] or 0) <= 0 else red)(f"{r['spot_chg']:+.1f}%") if r["spot_chg"] is not None else grey("—"),
            r["close_reason"] or grey("—"),
            fmt_pnl(r["pnl"]),
        ])
    print_table(
        ["Symbol", "Short Strike", "Spot@Close", "Buffer@Close", "Spot Chg%", "Exit", "P&L"],
        table,
        note="Red buffer = spot above strike at close. Sort: closest calls first."
    )


def section_entry_type(conn):
    section("8 · ENTRY TYPE  (morning scan vs intraday)")
    rows = q(conn, """
        SELECT entry_type,
               COUNT(*) AS trades,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
               ROUND(AVG(pnl), 2) AS avg_pnl,
               ROUND(SUM(pnl), 2) AS total_pnl,
               ROUND(AVG(pct_premium_captured), 1) AS avg_captured
        FROM paper_trades
        WHERE status IN ('closed','expired')
          AND pnl IS NOT NULL
          AND entry_type IS NOT NULL
        GROUP BY entry_type
        ORDER BY avg_pnl DESC
    """)
    table = []
    for r in rows:
        table.append([
            bold(r["entry_type"] or "—"),
            str(r["trades"]),
            fmt_win_rate(r["wins"], r["trades"]),
            fmt_pnl(r["avg_pnl"]),
            fmt_pnl(r["total_pnl"]),
            fmt_pct(r["avg_captured"]),
        ])
    print_table(
        ["Entry Type", "Trades", "Win%", "Avg P&L", "Total P&L", "Avg Captured%"],
        table,
        note="Insight: if intraday lags → tighten intraday-only thresholds."
    )


def section_vix_regime(conn):
    section("9 · VIX REGIME CORRELATION")
    rows = q(conn, """
        SELECT
          CASE
            WHEN vix_at_open < 16 THEN 'VIX <16  (low vol)'
            WHEN vix_at_open < 20 THEN 'VIX 16-20'
            WHEN vix_at_open < 25 THEN 'VIX 20-25'
            WHEN vix_at_open < 30 THEN 'VIX 25-30'
            ELSE                       'VIX 30+  (crisis)'
          END AS vix_zone,
          COUNT(*) AS trades,
          SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
          ROUND(AVG(pnl), 2) AS avg_pnl,
          ROUND(AVG(pct_premium_captured), 1) AS avg_captured,
          ROUND(AVG(atm_iv_at_open - atm_iv_at_close), 2) AS avg_iv_crush
        FROM paper_trades
        WHERE status IN ('closed','expired')
          AND pnl IS NOT NULL
          AND vix_at_open IS NOT NULL
        GROUP BY vix_zone
        ORDER BY MIN(vix_at_open)
    """)
    table = []
    for r in rows:
        table.append([
            bold(r["vix_zone"]),
            str(r["trades"]),
            fmt_win_rate(r["wins"], r["trades"]),
            fmt_pnl(r["avg_pnl"]),
            fmt_pct(r["avg_captured"]),
            fmt_iv_crush(r["avg_iv_crush"]),
        ])
    print_table(
        ["VIX Zone", "Trades", "Win%", "Avg P&L", "Avg Captured%", "Avg IV Crush"],
        table,
        note="Key question: does VIX 20-25 produce more IV crush than VIX <20?"
    )


def section_days_held(conn):
    section("10 · DAYS HELD DISTRIBUTION")
    rows = q(conn, """
        SELECT
          CASE
            WHEN days_held = 0 THEN '0d  (same-day)'
            WHEN days_held <= 3  THEN '1-3d'
            WHEN days_held <= 7  THEN '4-7d'
            WHEN days_held <= 14 THEN '8-14d'
            ELSE                      '15d+'
          END AS bucket,
          MIN(days_held) AS min_days,
          COUNT(*) AS trades,
          SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
          ROUND(AVG(pnl), 2) AS avg_pnl,
          ROUND(AVG(pct_premium_captured), 1) AS avg_captured
        FROM paper_trades
        WHERE status IN ('closed','expired')
          AND pnl IS NOT NULL
          AND days_held IS NOT NULL
        GROUP BY bucket
        ORDER BY min_days
    """)
    table = []
    for r in rows:
        table.append([
            bold(r["bucket"]),
            str(r["trades"]),
            fmt_win_rate(r["wins"], r["trades"]),
            fmt_pnl(r["avg_pnl"]),
            fmt_pct(r["avg_captured"]),
        ])
    print_table(
        ["Days Held", "Trades", "Win%", "Avg P&L", "Avg Captured%"],
        table,
        note="Insight: short hold <3d with losses → stop_loss fires too early?"
    )


def section_data_coverage(conn):
    section("DATA COVERAGE  (analytics fields populated)")
    total_row = q1(conn, "SELECT COUNT(*) AS n FROM paper_trades WHERE status IN ('closed','expired')")
    total = (total_row["n"] if total_row else 0) or 0

    # Detect which columns actually exist — handles pre-migration databases gracefully
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_trades)").fetchall()}

    checks = [
        ("short_strike",   "Short strike"),
        ("atm_iv_at_open", "ATM IV at open"),
        ("theta_at_open",  "Theta at open"),
        ("vega_at_open",   "Vega at open"),
        ("signal_score",   "Signal score"),
        ("entry_type",     "Entry type"),
        ("atm_iv_at_close","ATM IV at close"),
        ("buffer_at_close","Buffer at close"),
        ("spot_change_pct","Spot change %"),
        ("rsi_at_close",   "RSI at close"),
    ]
    print()
    missing_cols = []
    for col, label in checks:
        if col not in existing_cols:
            missing_cols.append(col)
            bar = dim("░" * 20)
            print(f"  {label:<24} [{bar}] {yellow('column not in DB yet')}")
            continue
        row = q1(conn, f"SELECT COUNT(*) AS n FROM paper_trades WHERE status IN ('closed','expired') AND {col} IS NOT NULL")
        n = (row["n"] if row else 0) or 0
        bar_filled = int(n / total * 20) if total else 0
        bar = green("█" * bar_filled) + dim("░" * (20 - bar_filled))
        pct = f"{n/total*100:.0f}%" if total else "—"
        print(f"  {label:<24} [{bar}] {n}/{total}  {dim(pct)}")
    print()
    if total == 0:
        print(yellow("  ⚠  No closed trades yet."))
    elif total < 10:
        print(yellow(f"  ⚠  Only {total}/10 trades closed — analytics will grow more meaningful at 10+."))
    else:
        print(green(f"  ✓  {total} closed trades. Statistical patterns are emerging."))
    if missing_cols:
        print()
        print(yellow(f"  ⚠  {len(missing_cols)} column(s) missing — DB needs migration."))
        fix = "from src.execution.paper_ledger import PaperLedger; PaperLedger('data/paper_trades.db')"
        print(dim(f"     Fix: python3 -c {repr(fix)}"))
        print(dim("     (PaperLedger.__init__ calls _migrate_db() automatically on every start)"))
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def resolve_db(cli_db: Optional[str]) -> str:
    if cli_db:
        return cli_db
    # Try config.yaml
    for cfg_path in ["config.yaml", "moomoo/config.yaml"]:
        if os.path.exists(cfg_path):
            try:
                import yaml
                with open(cfg_path) as f:
                    cfg = yaml.safe_load(f)
                db = cfg.get("paper_ledger", {}).get("db_path")
                if db and os.path.exists(db):
                    return db
            except Exception:
                pass
    # Common fallbacks
    for path in ["data/paper_trades.db", "moomoo/data/paper_trades.db"]:
        if os.path.exists(path):
            return path
    return "data/paper_trades.db"


def main():
    global USE_COLOR

    parser = argparse.ArgumentParser(description="Options bot analytics report")
    parser.add_argument("--db",       help="Path to paper_trades.db")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colour output")
    parser.add_argument("--section",  help="Run only one section (1-10, overview, coverage)")
    args = parser.parse_args()

    if args.no_color or not sys.stdout.isatty():
        USE_COLOR = False

    db_path = resolve_db(args.db)
    conn    = get_conn(db_path)

    print()
    print(bold(cyan("  ╔══════════════════════════════════════════════════════════╗")))
    print(bold(cyan("  ║        OPTIONS BOT — ANALYTICS REPORT                   ║")))
    print(bold(cyan("  ╚══════════════════════════════════════════════════════════╝")))
    print(f"  {dim('DB:')} {dim(db_path)}")

    sections = {
        "overview":  section_overview,
        "1":         section_exit_type,
        "2":         section_symbol,
        "3":         section_iv_crush,
        "4":         section_theta_realisation,
        "5":         section_signal_score,
        "6":         section_pctb_zones,
        "7":         section_near_miss,
        "8":         section_entry_type,
        "9":         section_vix_regime,
        "10":        section_days_held,
        "coverage":  section_data_coverage,
    }

    if args.section:
        fn = sections.get(args.section.lower())
        if fn:
            fn(conn)
        else:
            print(red(f"\n✗ Unknown section '{args.section}'. Choose from: {', '.join(sections)}"))
    else:
        section_overview(conn)
        section_data_coverage(conn)
        section_exit_type(conn)
        section_symbol(conn)
        section_iv_crush(conn)
        section_theta_realisation(conn)
        section_signal_score(conn)
        section_pctb_zones(conn)
        section_near_miss(conn)
        section_entry_type(conn)
        section_vix_regime(conn)
        section_days_held(conn)

    conn.close()
    print()


if __name__ == "__main__":
    main()
