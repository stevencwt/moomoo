"""
Signal Notifier
===============
Formats trade signals for manual execution and persists them to a
signals log so you can review and act on them in the moomoo app.

Since moomoo Singapore doesn't support API trading on real accounts,
the workflow is:

  Bot detects signal
       ↓
  SignalNotifier writes to signals/pending_signals.json + logs/signals.log
       ↓
  You review the signal (--pending command)
       ↓
  You manually place the order in the moomoo app
       ↓
  You run: python3 main.py --record-trade
       ↓
  Bot records your fill in the paper ledger and begins monitoring

Signal files:
  signals/pending_signals.json  : signals awaiting manual execution
  logs/signals.log              : full history of all signals
"""

import json
import os
from datetime import datetime
from typing import List, Optional

from src.strategies.trade_signal import TradeSignal
from src.logger import get_logger

logger = get_logger("notifier.signal_notifier")

PENDING_FILE = "signals/pending_signals.json"
SIGNAL_LOG   = "logs/signals.log"


class SignalNotifier:
    """
    Formats signals and persists them for manual review and execution.
    """

    def __init__(self):
        os.makedirs("signals", exist_ok=True)
        os.makedirs("logs",    exist_ok=True)

    # ── Public API ────────────────────────────────────────────────

    def notify(self, signals: List[TradeSignal]) -> None:
        """
        Process a list of signals: format, log, and save to pending file.
        Called by the scheduler after each scan cycle.
        """
        if not signals:
            return

        for signal in signals:
            formatted = self.format_signal(signal)
            self._log_signal(signal, formatted)
            self._add_to_pending(signal)

        # Print to console so it's visible when the bot is running
        print("\n" + "🔔 " * 20)
        print(f"  {len(signals)} NEW SIGNAL(S) — manual execution required")
        print("🔔 " * 20)
        for signal in signals:
            print(self.format_signal(signal))
        print(f"\n  Run:  python3 main.py --pending    (review all pending signals)")
        print(f"  Run:  python3 main.py --record-trade  (after placing in moomoo app)")
        print()

    def format_signal(self, signal: TradeSignal) -> str:
        """Format a signal as a human-readable trade ticket."""
        lines = []
        sep = "─" * 55

        lines.append(f"\n{sep}")
        lines.append(f"  📋 TRADE SIGNAL  [{signal.timestamp.strftime('%Y-%m-%d %H:%M')}]")
        lines.append(sep)
        lines.append(f"  Strategy  : {signal.strategy_name.replace('_', ' ').title()}")
        lines.append(f"  Symbol    : {signal.symbol}")
        lines.append(f"  Action    : SELL TO OPEN")
        lines.append("")

        if signal.signal_type == "covered_call":
            lines.append(f"  ── Leg ──────────────────────────────────────")
            lines.append(f"  SELL 1 {signal.sell_contract}")
            lines.append(f"  Limit price : ${signal.sell_price:.2f}  (mid-price at signal time)")
            lines.append(f"  Net credit  : ${signal.net_credit:.2f} / share  "
                         f"= ${signal.net_credit * 100:.0f} / contract")
        else:
            lines.append(f"  ── Spread Legs ──────────────────────────────")
            lines.append(f"  SELL 1 {signal.sell_contract}  @ ${signal.sell_price:.2f}")
            lines.append(f"  BUY  1 {signal.buy_contract}  @ ${signal.buy_price:.2f}")
            lines.append(f"  Net credit  : ${signal.net_credit:.2f} / share  "
                         f"= ${signal.net_credit * 100:.0f} / contract")

        lines.append("")
        lines.append(f"  ── Risk ─────────────────────────────────────")
        lines.append(f"  Max profit  : ${signal.max_profit:.0f}  (if expires worthless)")
        if signal.max_loss:
            lines.append(f"  Max loss    : ${signal.max_loss:.0f}  (if spread goes full ITM)")
            lines.append(f"  Reward/risk : {signal.reward_risk:.2f}×")
        else:
            lines.append(f"  Max loss    : Covered by 100 shares of {signal.symbol}")
        lines.append(f"  Breakeven   : ${signal.breakeven:.2f}  (underlying at expiry)")
        lines.append(f"  Stop loss   : Close if position costs "
                     f">${signal.net_credit * 2:.2f}  (2× credit)")
        lines.append(f"  Take profit : Close if position worth "
                     f"<${signal.net_credit * 0.5:.2f}  (50% profit)")

        lines.append("")
        lines.append(f"  ── Context ──────────────────────────────────")
        lines.append(f"  Expiry      : {signal.expiry}  ({signal.dte} DTE)")
        lines.append(f"  Delta       : {signal.delta:.2f}")
        lines.append(f"  IV Rank     : {signal.iv_rank:.0f}")
        lines.append(f"  Regime      : {signal.regime}")
        lines.append(f"  Reason      : {signal.reason}")
        lines.append(sep)

        return "\n".join(lines)

    def get_pending(self) -> List[dict]:
        """Return all pending signals awaiting manual execution."""
        if not os.path.exists(PENDING_FILE):
            return []
        with open(PENDING_FILE) as f:
            return json.load(f)

    def remove_pending(self, signal_id: str) -> None:
        """Remove a signal from the pending list after it's been recorded."""
        pending = self.get_pending()
        pending = [s for s in pending if s["id"] != signal_id]
        self._save_pending(pending)

    def clear_pending(self) -> None:
        """Clear all pending signals."""
        self._save_pending([])

    # ── Private Helpers ───────────────────────────────────────────

    def _add_to_pending(self, signal: TradeSignal) -> None:
        """Append signal to pending file."""
        pending = self.get_pending()

        # Generate a unique ID for this signal
        signal_id = (
            f"{signal.strategy_name}_{signal.symbol}_"
            f"{signal.sell_contract}_{signal.timestamp.strftime('%Y%m%d%H%M%S')}"
        )

        # Don't add duplicates (same contract already pending)
        existing_contracts = {s["sell_contract"] for s in pending}
        if signal.sell_contract in existing_contracts:
            logger.debug(f"Signal for {signal.sell_contract} already pending — skipping")
            return

        entry = {
            "id":              signal_id,
            "strategy_name":   signal.strategy_name,
            "symbol":          signal.symbol,
            "signal_type":     signal.signal_type,
            "sell_contract":   signal.sell_contract,
            "buy_contract":    signal.buy_contract,
            "quantity":        signal.quantity,
            "sell_price":      signal.sell_price,
            "buy_price":       signal.buy_price,
            "net_credit":      signal.net_credit,
            "max_profit":      signal.max_profit,
            "max_loss":        signal.max_loss,
            "breakeven":       signal.breakeven,
            "reward_risk":     signal.reward_risk,
            "expiry":          signal.expiry,
            "dte":             signal.dte,
            "iv_rank":         signal.iv_rank,
            "delta":           signal.delta,
            "regime":          signal.regime,
            "reason":          signal.reason,
            "generated_at":    signal.timestamp.isoformat(),
            "status":          "pending",
        }
        pending.append(entry)
        self._save_pending(pending)
        logger.info(f"Signal saved to pending: {signal_id}")

    def _save_pending(self, pending: List[dict]) -> None:
        with open(PENDING_FILE, "w") as f:
            json.dump(pending, f, indent=2)

    def _log_signal(self, signal: TradeSignal, formatted: str) -> None:
        """Append signal to the signals log file."""
        with open(SIGNAL_LOG, "a") as f:
            f.write(formatted + "\n")
        logger.info(
            f"Signal logged: {signal.strategy_name} {signal.symbol} "
            f"credit=${signal.net_credit:.2f} DTE={signal.dte}"
        )
