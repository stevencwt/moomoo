"""
Options Bot — Main Entry Point
================================
Usage:
    python3 main.py                       # Run with default config
    python3 main.py --config path/to/config.yaml
    python3 main.py --scan-now            # Run one scan cycle and exit
    python3 main.py --monitor-now         # Run one monitor cycle and exit
    python3 main.py --report              # Generate validation report and exit
    python3 main.py --status              # Print current portfolio status and exit

⚠️  PAPER MODE ONLY until ValidationReporter says GO LIVE.
   Do not change mode: live in config.yaml until all validation gates pass.
"""

import argparse
import os
import sys
import yaml

# Ensure src/ is on the Python path when running from project root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.logger import get_logger, setup_logger
from src.scheduler.bot_scheduler import BotScheduler

logger = get_logger("main")


def load_config(config_path: str) -> dict:
    """Load YAML config file."""
    if not os.path.exists(config_path):
        print(f"ERROR: Config file not found: {config_path}")
        sys.exit(1)
    with open(config_path) as f:
        config = yaml.safe_load(f)
    return config


def validate_config(config: dict) -> None:
    """Basic sanity checks on config before starting."""
    mode = config.get("mode", "paper")
    if mode not in ("paper", "live"):
        print(f"ERROR: config.mode must be 'paper' or 'live', got '{mode}'")
        sys.exit(1)

    if mode == "live":
        print(
            "\n" + "⚠️ " * 30 + "\n"
            "  WARNING: mode=live detected.\n"
            "  This bot will place REAL MONEY trades.\n"
            "  Are you sure validation has passed? (yes/no): ",
            end=""
        )
        answer = input().strip().lower()
        if answer != "yes":
            print("Aborting. Set mode: paper in config.yaml to run safely.")
            sys.exit(0)

    watchlist = config.get("universe", {}).get("watchlist", [])
    if not watchlist:
        print("ERROR: universe.watchlist is empty in config.yaml")
        sys.exit(1)


def run_full_bot(config: dict) -> None:
    """Start the full scheduler loop."""
    scheduler = BotScheduler.build(config)
    logger.info(
        f"Starting Options Bot | "
        f"mode={config.get('mode')} | "
        f"watchlist={config.get('universe', {}).get('watchlist', [])}"
    )
    scheduler.start()


def run_scan_now(config: dict) -> None:
    """
    Run a single scan cycle using the full BotScheduler display pipeline.
    Identical output to the scheduled 09:35 scan — all gate analysis visible.
    """
    from src.scheduler.bot_scheduler import BotScheduler

    bot = BotScheduler.build(config)
    # Call the internal scan job directly — same code path as the daily 09:35 scan
    bot._scan_job(force=True)


def run_monitor_now(config: dict) -> None:
    """Run a single monitor cycle."""
    from src.connectors.moomoo_connector import MooMooConnector
    from src.execution.paper_ledger import PaperLedger
    from src.execution.portfolio_guard import PortfolioGuard
    from src.execution.order_router import OrderRouter
    from src.execution.trade_manager import TradeManager
    from src.monitoring.exit_evaluator import ExitEvaluator
    from src.monitoring.position_monitor import PositionMonitor

    print("\n👁️  Running monitor cycle...\n")

    moomoo  = MooMooConnector(config)
    moomoo.connect()

    try:
        ledger   = PaperLedger()
        guard    = PortfolioGuard(config)
        router   = OrderRouter(config, moomoo)
        manager  = TradeManager(config, guard, router, ledger)
        monitor  = PositionMonitor(
            config, ledger, manager, moomoo, ExitEvaluator(config)
        )

        summary = monitor.get_position_summary()
        print(f"Open positions: {len(summary)}")
        for pos in summary:
            pnl = f"${pos['unrealised_pnl']:+.0f}" if pos["unrealised_pnl"] else "N/A"
            print(
                f"  #{pos['id']} {pos['symbol']:<12} "
                f"{pos['strategy_name']:<20} | "
                f"credit=${pos['net_credit']:.2f} | "
                f"unrealised={pnl} | "
                f"expiry={pos['expiry']}"
            )

        actions = monitor.run_cycle(force=True)
        if actions:
            print(f"\n{len(actions)} exit(s) triggered:")
            for a in actions:
                print(f"  {a['symbol']} {a['reason']}: P&L=${a['pnl']:+.2f}")
        else:
            print("\nAll positions held — no exits triggered")

    finally:
        moomoo.disconnect()


def run_report(config: dict) -> None:
    """Generate and print the validation report."""
    from src.execution.paper_ledger import PaperLedger
    from src.monitoring.validation_reporter import ValidationReporter

    ledger   = PaperLedger()
    reporter = ValidationReporter(config, ledger)
    reporter.generate(save_to_file=True)


def run_status(config: dict) -> None:
    """Print current portfolio status."""
    from src.execution.paper_ledger import PaperLedger
    from src.monitoring.validation_reporter import ValidationReporter

    ledger   = PaperLedger()
    reporter = ValidationReporter(config, ledger)
    reporter.print_current_status()


def run_pending(config: dict) -> None:
    """Show all pending signals awaiting manual execution."""
    from src.notifier.signal_notifier import SignalNotifier
    notifier = SignalNotifier()
    notifier.show_pending(notifier.get_pending()) if hasattr(notifier, 'show_pending') else None
    from src.notifier.trade_recorder import show_pending
    show_pending(notifier)


def run_record_trade(config: dict) -> None:
    """Interactively record a manually executed trade."""
    from src.execution.paper_ledger import PaperLedger
    from src.execution.portfolio_guard import PortfolioGuard
    from src.notifier.signal_notifier import SignalNotifier
    from src.notifier.trade_recorder import record_trade

    ledger   = PaperLedger()
    guard    = PortfolioGuard(config)
    notifier = SignalNotifier()
    record_trade(config, ledger, guard, notifier)


def run_close_trade(config: dict) -> None:
    """Interactively record a manually closed position."""
    from src.execution.paper_ledger import PaperLedger
    from src.execution.portfolio_guard import PortfolioGuard
    from src.notifier.trade_recorder import close_trade

    ledger = PaperLedger()
    guard  = PortfolioGuard(config)
    close_trade(config, ledger, guard)



def run_close_trade_live(config: dict) -> None:
    """
    Close an open position through the full execution pipeline:
    TradeManager.close_trade() → OrderRouter.close_spread() → IBKRConnector

    Unlike --close-trade (which only records in the ledger), this places a
    real buy-to-close order in IBKR when mode=live.
    """
    from src.connectors.broker_factory import build_connectors
    from src.execution.paper_ledger import PaperLedger
    from src.execution.portfolio_guard import PortfolioGuard
    from src.execution.order_router import OrderRouter
    from src.execution.trade_manager import TradeManager

    print("\n" + "=" * 60)
    print("  CLOSE TRADE — LIVE EXECUTION PIPELINE")
    print("=" * 60)
    print(f"  Mode: {config.get('mode', 'paper')}")

    if config.get("mode") != "live":
        print("\n  WARNING: mode is not 'live' — broker order will be simulated (paper).")

    # Wire up using the same factory as BotScheduler.build()
    data_connector, exec_connector = build_connectors(config)

    ledger  = PaperLedger(config.get("paper_ledger", {}).get(
        "db_path", "data/paper_trades.db"
    ))
    guard   = PortfolioGuard(config)
    guard.restore_from_ledger(ledger)
    router  = OrderRouter(config, exec_connector)
    manager = TradeManager(config, guard, router, ledger)

    # List open positions
    open_trades = ledger.get_open_trades()
    if not open_trades:
        print("\n  No open positions to close.\n")
        return

    print(f"\n  Open positions ({len(open_trades)}):")
    for i, t in enumerate(open_trades, 1):
        strikes = ""
        if t.get("short_strike") and t.get("long_strike"):
            strikes = f"  {t['short_strike']:.0f}/{t['long_strike']:.0f}"
        print(
            f"    [{i}] #{t['id']}  {t['symbol']:<10} {t['strategy_name']:<20}"
            f"{strikes}  credit=${t['net_credit']:.2f}  expiry={t['expiry']}"
        )

    # Select trade
    print()
    try:
        choice = int(input("  Select position number to close: ").strip())
    except (ValueError, EOFError):
        print("  Aborted.")
        return

    if choice < 1 or choice > len(open_trades):
        print(f"  Invalid choice. Must be 1-{len(open_trades)}.")
        return

    trade = open_trades[choice - 1]
    trade_id = trade["id"]

    # Get close price
    print(f"\n  Selected: #{trade_id} {trade['symbol']} {trade['strategy_name']}")
    print(f"  Sell contract: {trade['sell_contract']}")
    print(f"  Buy contract:  {trade.get('buy_contract', 'N/A')}")
    print(f"  Net credit:    ${trade['net_credit']:.2f}")

    # Try fetching live price via data connector
    live_price = None
    try:
        contracts = [trade["sell_contract"]]
        if trade.get("buy_contract"):
            contracts.append(trade["buy_contract"])
        snap = data_connector.get_option_snapshot(contracts)
        if snap is not None and len(snap) > 0:
            sell_row = snap[snap["code"] == trade["sell_contract"]]
            if len(sell_row) > 0:
                sell_mid = float(sell_row.iloc[0].get("mid_price",
                    sell_row.iloc[0].get("ask_price", 0)))
                if trade.get("buy_contract"):
                    buy_row = snap[snap["code"] == trade["buy_contract"]]
                    if len(buy_row) > 0:
                        buy_mid = float(buy_row.iloc[0].get("mid_price",
                            buy_row.iloc[0].get("bid_price", 0)))
                        live_price = max(0.0, sell_mid - buy_mid)
                else:
                    live_price = sell_mid
    except Exception as e:
        logger.warning(f"Could not fetch live price: {e}")

    if live_price is not None:
        print(f"  Live mark:     ${live_price:.2f}")
        use_live = input(f"  Use live price ${live_price:.2f}? (yes/no): ").strip().lower()
        if use_live == "yes":
            close_price = live_price
        else:
            close_price = float(input("  Enter close price (debit per share): ").strip())
    else:
        print("  Live price unavailable.")
        close_price = float(input("  Enter close price (debit per share): ").strip())

    # Select close reason
    print(f"\n  Close price: ${close_price:.2f}")
    print("  Close reasons: manual, stop_loss, take_profit, dte_close, regime_shift")
    reason = input("  Close reason [manual]: ").strip() or "manual"

    # Final confirmation
    expected_pnl = (trade["net_credit"] - close_price) * 100 * trade.get("quantity", 1)
    print(f"\n  {'!' * 50}")
    print(f"  CONFIRM: Close #{trade_id} {trade['symbol']}")
    print(f"    Debit:         ${close_price:.2f}")
    print(f"    Reason:        {reason}")
    print(f"    Expected P&L:  ${expected_pnl:+.2f}")
    if config.get("mode") == "live":
        print(f"    IBKR ORDER:    YES — real buy-to-close will be placed")
    else:
        print(f"    IBKR ORDER:    NO (paper mode)")
    print(f"  {'!' * 50}")

    answer = input("\n  Type 'yes' to execute: ").strip().lower()
    if answer != "yes":
        print("  Aborted.")
        return

    # Execute through full pipeline
    print(f"\n  Executing close via TradeManager → OrderRouter → IBKR...")
    pnl = manager.close_trade(
        trade_id=trade_id,
        close_price=close_price,
        close_reason=reason,
        symbol=trade["symbol"],
        strategy_name=trade["strategy_name"],
    )

    print(f"\n  {'=' * 50}")
    action = "PROFIT" if pnl > 0 else "LOSS"
    print(f"  Trade #{trade_id} CLOSED | {action} | P&L=${pnl:+.2f}")
    print(f"  {'=' * 50}")
    print()
    print("  Verification steps:")
    print("    1. IBKR TWS → Activity → Trades tab: check for closing order")
    print("    2. IBKR TWS → Portfolio tab: NVDA position should be gone")
    print("    3. python3 main.py --status: confirm 0 open positions")
    print()


def run_reset_ledger(config: dict) -> None:
    """
    Clear all open positions from the paper ledger.
    Use this when transitioning brokers or starting a fresh paper trading run.
    Closed/expired trades are preserved for historical record.
    """
    import sqlite3
    from datetime import datetime
    from src.execution.paper_ledger import PaperLedger

    ledger   = PaperLedger()
    db_path  = ledger._db_path

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        open_trades = conn.execute(
            "SELECT id, symbol, strategy_name, sell_contract, net_credit, expiry "
            "FROM paper_trades WHERE status = 'open' ORDER BY id"
        ).fetchall()

    if not open_trades:
        print("\n✅  No open positions in the paper ledger — nothing to clear.")
        return

    # ── Show what will be cleared ─────────────────────────────────
    print("\n" + "═" * 60)
    print("  Reset Paper Ledger — Open Positions")
    print("═" * 60)
    print(f"  {'#':<5} {'Symbol':<12} {'Strategy':<22} {'Contract':<28} {'Credit':>7}  {'Expiry'}")
    print(f"  {'─'*5} {'─'*12} {'─'*22} {'─'*28} {'─'*7}  {'─'*10}")
    for t in open_trades:
        print(
            f"  {t['id']:<5} {t['symbol']:<12} {t['strategy_name']:<22} "
            f"{t['sell_contract']:<28} ${t['net_credit']:>6.2f}  {t['expiry']}"
        )
    print(f"\n  {len(open_trades)} open position(s) will be marked as CANCELLED.")
    print("  Closed and expired trades will NOT be affected.")
    print()

    # ── Confirm ───────────────────────────────────────────────────
    answer = input("  Type 'yes' to confirm reset, anything else to abort: ").strip().lower()
    if answer != "yes":
        print("\n  Aborted — ledger unchanged.")
        return

    # ── Apply reset ───────────────────────────────────────────────
    now = datetime.now().isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            UPDATE paper_trades
            SET status       = 'cancelled',
                close_reason = 'ledger_reset',
                closed_at    = ?,
                close_price  = 0.0,
                pnl          = 0.0
            WHERE status = 'open'
        """, (now,))
        affected = conn.execute(
            "SELECT changes()"
        ).fetchone()[0]

    print(f"\n  ✅  {affected} position(s) cancelled and removed from active tracking.")
    print(f"  Records preserved in {db_path} under status='cancelled'.")
    print("\n  You can now start a fresh paper trading run.")


# ── Entry point ───────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Options Income Bot — automated premium selling"
    )
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to config YAML (default: config/config.yaml)"
    )
    parser.add_argument(
        "--scan-now",
        action="store_true",
        help="Run one scan cycle and exit"
    )
    parser.add_argument(
        "--monitor-now",
        action="store_true",
        help="Run one monitor cycle and exit"
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Generate validation report and exit"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print current portfolio status and exit"
    )
    parser.add_argument(
        "--pending",
        action="store_true",
        help="Show signals awaiting manual execution"
    )
    parser.add_argument(
        "--record-trade",
        action="store_true",
        dest="record_trade",
        help="Record a manually placed trade into the paper ledger"
    )
    parser.add_argument(
        "--close-trade",
        action="store_true",
        dest="close_trade",
        help="Record a manually closed position into the paper ledger"
    )
    parser.add_argument(
        "--close-trade-live",
        action="store_true",
        dest="close_trade_live",
        help="Close a position through the full execution pipeline (places real IBKR order in live mode)"
    )
    parser.add_argument(
        "--reset-ledger",
        action="store_true",
        dest="reset_ledger",
        help="Clear all open positions from the paper ledger (use when changing brokers)"
    )

    args   = parser.parse_args()
    config = load_config(args.config)
    validate_config(config)
    setup_logger(config)   # attach file handler → creates logs/bot.log

    if args.scan_now:
        run_scan_now(config)
    elif args.monitor_now:
        run_monitor_now(config)
    elif args.report:
        run_report(config)
    elif args.status:
        run_status(config)
    elif args.pending:
        run_pending(config)
    elif args.record_trade:
        run_record_trade(config)
    elif args.close_trade:
        run_close_trade(config)
    elif args.close_trade_live:
        run_close_trade_live(config)
    elif args.reset_ledger:
        run_reset_ledger(config)
    else:
        run_full_bot(config)


if __name__ == "__main__":
    main()
