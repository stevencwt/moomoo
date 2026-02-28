"""
Trade Recorder
==============
Interactive CLI for manually recording trades and closes into the paper ledger.

Used when moomoo API real account access is unavailable (e.g. moomoo Singapore).
You place the trade manually in the moomoo app, then run this to log it.

Commands:
  python3 main.py --pending        List signals awaiting manual execution
  python3 main.py --record-trade   Record a fill from manual execution
  python3 main.py --close-trade    Record a manual close of an open position
"""

from datetime import datetime
from typing import Optional

from src.execution.paper_ledger import PaperLedger
from src.execution.portfolio_guard import PortfolioGuard
from src.notifier.signal_notifier import SignalNotifier
from src.logger import get_logger

logger = get_logger("notifier.trade_recorder")


def show_pending(notifier: SignalNotifier) -> None:
    """Print all signals awaiting manual execution."""
    pending = notifier.get_pending()

    print("\n" + "═" * 60)
    print("  📋 PENDING SIGNALS  (awaiting manual execution)")
    print("═" * 60)

    if not pending:
        print("\n  No pending signals.\n")
        return

    for i, s in enumerate(pending, 1):
        print(f"\n  [{i}] {s['strategy_name'].replace('_',' ').title()} — {s['symbol']}")
        print(f"      Generated : {s['generated_at'][:16]}")
        print(f"      Sell      : {s['sell_contract']}")
        if s.get("buy_contract"):
            print(f"      Buy       : {s['buy_contract']}")
        print(f"      Credit    : ${s['net_credit']:.2f}/share  "
              f"= ${s['net_credit']*100:.0f}/contract")
        print(f"      Expiry    : {s['expiry']}  ({s['dte']} DTE)")
        print(f"      Status    : {s['status']}")

    print(f"\n  Total: {len(pending)} pending signal(s)")
    print("─" * 60)
    print("  To record a fill: python3 main.py --record-trade")
    print("═" * 60 + "\n")


def record_trade(
    config:  dict,
    ledger:  PaperLedger,
    guard:   PortfolioGuard,
    notifier: SignalNotifier,
) -> None:
    """
    Interactive prompt to record a manually executed trade into the paper ledger.
    """
    from src.strategies.trade_signal import TradeSignal

    pending = notifier.get_pending()

    print("\n" + "═" * 60)
    print("  📝 RECORD MANUAL TRADE")
    print("═" * 60)

    # ── Select signal ─────────────────────────────────────────────
    if not pending:
        print("\n  No pending signals to record.")
        print("  Run a scan first: python3 main.py --scan-now\n")
        return

    print("\n  Pending signals:")
    for i, s in enumerate(pending, 1):
        print(f"    [{i}] {s['strategy_name'].replace('_',' ').title()} "
              f"{s['symbol']}  {s['sell_contract']}  "
              f"credit=${s['net_credit']:.2f}  expiry={s['expiry']}")

    print(f"    [0] Enter manually (signal not in list)")

    choice = _prompt_int("\n  Select signal number: ", 0, len(pending))

    if choice == 0:
        signal = _build_signal_manually()
        signal_id = None
    else:
        selected  = pending[choice - 1]
        signal_id = selected["id"]
        signal    = _signal_from_dict(selected)
        print(f"\n  Selected: {signal.strategy_name} {signal.symbol}")
        print(f"  Contract: {signal.sell_contract}")
        if signal.buy_contract:
            print(f"  Spread  : {signal.buy_contract}")
        print(f"  Bot's mid-price credit: ${signal.net_credit:.2f}/share")

    # ── Get actual fill prices ────────────────────────────────────
    print("\n  Enter your actual fill prices from the moomoo app:")
    fill_sell = _prompt_float(
        f"  Fill price for SELL leg [{signal.sell_price:.2f}]: ",
        default=signal.sell_price
    )

    fill_buy = None
    if signal.buy_contract:
        fill_buy = _prompt_float(
            f"  Fill price for BUY  leg [{signal.buy_price:.2f}]: ",
            default=signal.buy_price
        )

    net_credit = fill_sell - (fill_buy or 0)
    print(f"\n  Net credit: ${net_credit:.2f}/share = ${net_credit*100:.0f}/contract")

    # ── Confirm ───────────────────────────────────────────────────
    print(f"\n  Recording:")
    print(f"    Strategy  : {signal.strategy_name}")
    print(f"    Symbol    : {signal.symbol}")
    print(f"    Sell      : {signal.sell_contract} @ ${fill_sell:.2f}")
    if fill_buy:
        print(f"    Buy       : {signal.buy_contract} @ ${fill_buy:.2f}")
    print(f"    Credit    : ${net_credit:.2f}/share = ${net_credit*100:.0f}/contract")
    print(f"    Expiry    : {signal.expiry}")

    confirm = input("\n  Confirm? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("  Cancelled.\n")
        return

    # ── Record in ledger ──────────────────────────────────────────
    trade_id = ledger.record_open(signal, fill_sell=fill_sell, fill_buy=fill_buy)
    guard.record_open(signal)

    # Remove from pending
    if signal_id:
        notifier.remove_pending(signal_id)

    print(f"\n  ✅ Trade recorded! Trade ID: #{trade_id}")
    print(f"     The bot will now monitor this position for exits.")
    print(f"     Stop loss  : close if position costs > ${net_credit*2:.2f}")
    print(f"     Take profit: close if position worth < ${net_credit*0.5:.2f}")
    print(f"     DTE close  : close at 21 DTE ({signal.expiry})\n")

    logger.info(
        f"Manual trade recorded: #{trade_id} {signal.strategy_name} "
        f"{signal.symbol} credit=${net_credit:.2f}"
    )


def close_trade(
    config:  dict,
    ledger:  PaperLedger,
    guard:   PortfolioGuard,
) -> None:
    """
    Interactive prompt to record a manually closed position.
    """
    print("\n" + "═" * 60)
    print("  🔒 RECORD MANUAL CLOSE")
    print("═" * 60)

    open_trades = ledger.get_open_trades()

    if not open_trades:
        print("\n  No open positions to close.\n")
        return

    print("\n  Open positions:")
    for i, t in enumerate(open_trades, 1):
        print(f"    [{i}] #{t['id']} {t['strategy_name'].replace('_',' ').title()} "
              f"{t['symbol']}  credit=${t['net_credit']:.2f}  expiry={t['expiry']}")

    choice = _prompt_int("\n  Select position to close: ", 1, len(open_trades))
    trade  = open_trades[choice - 1]

    print(f"\n  Closing: #{trade['id']} {trade['symbol']} {trade['sell_contract']}")
    print(f"  Opened at credit: ${trade['net_credit']:.2f}/share")

    # ── Get close price ───────────────────────────────────────────
    print("\n  Close reasons:")
    print("    [1] expired_worthless  (option expired OTM — keep full credit)")
    print("    [2] take_profit        (closed early for profit)")
    print("    [3] stop_loss          (closed to limit loss)")
    print("    [4] manual             (other reason)")

    reason_map = {
        1: "expired_worthless",
        2: "take_profit",
        3: "stop_loss",
        4: "manual",
    }
    reason_choice = _prompt_int("  Select reason: ", 1, 4)
    close_reason  = reason_map[reason_choice]

    if close_reason == "expired_worthless":
        close_price = 0.0
        print("  Close price set to $0.00 (expired worthless = full credit kept)")
    else:
        close_price = _prompt_float("  Net debit to close (cost to buy back): ")

    pnl = (trade["net_credit"] - close_price) * 100 * trade["quantity"]

    print(f"\n  P&L: ${pnl:+.2f}")
    confirm = input("  Confirm close? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("  Cancelled.\n")
        return

    # ── Record close ──────────────────────────────────────────────
    actual_pnl = ledger.record_close(trade["id"], close_price, close_reason)
    guard.record_close(trade["symbol"], trade["strategy_name"])

    print(f"\n  ✅ Position #{trade['id']} closed. P&L: ${actual_pnl:+.2f}\n")
    logger.info(
        f"Manual close recorded: #{trade['id']} {trade['symbol']} "
        f"reason={close_reason} pnl=${actual_pnl:+.2f}"
    )


# ── Helpers ───────────────────────────────────────────────────────

def _prompt_int(prompt: str, min_val: int, max_val: int) -> int:
    while True:
        try:
            val = int(input(prompt).strip())
            if min_val <= val <= max_val:
                return val
            print(f"  Please enter a number between {min_val} and {max_val}")
        except ValueError:
            print("  Please enter a valid number")


def _prompt_float(prompt: str, default: Optional[float] = None) -> float:
    while True:
        try:
            raw = input(prompt).strip()
            if raw == "" and default is not None:
                return default
            val = float(raw)
            if val >= 0:
                return val
            print("  Please enter a non-negative number")
        except ValueError:
            print("  Please enter a valid number (e.g. 4.10)")


def _signal_from_dict(d: dict):
    """Reconstruct a TradeSignal from a pending signal dict."""
    from src.strategies.trade_signal import TradeSignal
    return TradeSignal(
        strategy_name= d["strategy_name"],
        symbol=        d["symbol"],
        timestamp=     datetime.fromisoformat(d["generated_at"]),
        action=        "OPEN",
        signal_type=   d["signal_type"],
        sell_contract= d["sell_contract"],
        buy_contract=  d.get("buy_contract"),
        quantity=      d["quantity"],
        sell_price=    d["sell_price"],
        buy_price=     d.get("buy_price"),
        net_credit=    d["net_credit"],
        max_profit=    d["max_profit"],
        max_loss=      d.get("max_loss"),
        breakeven=     d["breakeven"],
        reward_risk=   d.get("reward_risk"),
        expiry=        d["expiry"],
        dte=           d["dte"],
        iv_rank=       d["iv_rank"],
        delta=         d["delta"],
        regime=        d["regime"],
        reason=        d["reason"],
    )


def _build_signal_manually():
    """Prompt user to enter all signal details manually."""
    from src.strategies.trade_signal import TradeSignal

    print("\n  Enter signal details manually:")
    strategy   = input("  Strategy (covered_call / bear_call_spread): ").strip()
    symbol     = input("  Symbol (e.g. US.TSLA): ").strip()
    sell_c     = input("  Sell contract code: ").strip()
    buy_c      = input("  Buy contract code (leave blank for covered call): ").strip() or None
    sell_price = _prompt_float("  Sell price: ")
    buy_price  = _prompt_float("  Buy price (0 if none): ") if buy_c else None
    expiry     = input("  Expiry (YYYY-MM-DD): ").strip()
    dte        = _prompt_int("  DTE: ", 0, 365)
    iv_rank    = _prompt_float("  IV Rank (0-100): ")
    delta      = _prompt_float("  Delta (e.g. 0.30): ")
    regime     = input("  Regime (bull/bear/neutral/high_vol): ").strip()

    net_credit = sell_price - (buy_price or 0)

    return TradeSignal(
        strategy_name= strategy,
        symbol=        symbol,
        timestamp=     datetime.now(),
        action=        "OPEN",
        signal_type=   strategy,
        sell_contract= sell_c,
        buy_contract=  buy_c,
        quantity=      1,
        sell_price=    sell_price,
        buy_price=     buy_price,
        net_credit=    net_credit,
        max_profit=    net_credit * 100,
        max_loss=      None,
        breakeven=     0.0,
        reward_risk=   None,
        expiry=        expiry,
        dte=           dte,
        iv_rank=       iv_rank,
        delta=         delta,
        regime=        regime,
        reason=        "Manually entered",
    )
