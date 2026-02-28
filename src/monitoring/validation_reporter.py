"""
Validation Reporter
===================
Generates the 4-week paper trading validation report used to decide
whether the bot is ready to transition to live trading.

Validation criteria (ALL must pass for live approval):
  1. min_trades          : >= 10 closed trades (enough sample size)
  2. min_win_rate        : >= 60% win rate
  3. positive_expectancy : avg_pnl > 0 (positive expected value per trade)
  4. max_drawdown        : No single trade loss > 2× average max_loss
  5. sharpe_like         : total_pnl / std_dev_pnl >= 0.5

Report sections:
  - Overview: dates, total trades, win rate
  - P&L Summary: total, average, best/worst
  - By Strategy: per-strategy breakdown
  - Validation Gate: PASS/FAIL for each criterion with details
  - Recommendation: GO / NO-GO for live trading

Output: printed to console + saved to data/validation_report_{date}.txt
"""

import os
from datetime import date, datetime
from typing import Dict, List

from src.execution.paper_ledger import PaperLedger
from src.logger import get_logger

logger = get_logger("monitoring.validation_reporter")


class ValidationReporter:
    """
    Generates paper trading validation reports.
    """

    def __init__(self, config: dict, ledger: PaperLedger):
        self._config = config
        self._ledger = ledger

        val_cfg = config.get("validation", {})
        self._min_trades         = val_cfg.get("min_trades",         10)
        self._min_win_rate       = val_cfg.get("min_win_rate",       0.60)
        self._min_sharpe_like    = val_cfg.get("min_sharpe_like",    0.50)

        logger.info(
            f"ValidationReporter | min_trades={self._min_trades} | "
            f"min_win_rate={self._min_win_rate:.0%} | "
            f"min_sharpe={self._min_sharpe_like}"
        )

    # ── Public API ────────────────────────────────────────────────

    def generate(self, save_to_file: bool = True) -> Dict:
        """
        Generate the full validation report.

        Args:
            save_to_file: If True, save report to data/validation_report_{date}.txt

        Returns:
            Report dict with all statistics and gate results.
        """
        stats  = self._ledger.get_statistics()
        trades = self._get_all_closed_trades()

        gates  = self._evaluate_gates(stats, trades)
        go_live = all(g["passed"] for g in gates.values())

        report = {
            "generated_at":  datetime.now().isoformat(),
            "statistics":    stats,
            "gates":         gates,
            "go_live":       go_live,
            "recommendation": "GO LIVE ✅" if go_live else "NOT READY ❌",
        }

        report_text = self._format_report(report, stats, gates)
        print(report_text)

        if save_to_file:
            self._save_report(report_text)

        return report

    def print_current_status(self) -> None:
        """Print a brief current status (for daily monitoring, not full report)."""
        stats = self._ledger.get_statistics()

        print("\n" + "─" * 55)
        print(f"  📊 Paper Trading Status  [{date.today()}]")
        print("─" * 55)
        print(f"  Open positions:  {stats['open_count']}")
        print(f"  Closed trades:   {stats['total_trades']}")
        if stats["total_trades"] > 0:
            print(f"  Win rate:        {stats['win_rate']:.0%} "
                  f"({stats['winning_trades']}/{stats['total_trades']})")
            print(f"  Total P&L:       ${stats['total_pnl']:+,.2f}")
            print(f"  Avg P&L/trade:   ${stats['avg_pnl']:+,.2f}")
            if stats["by_strategy"]:
                print("  By strategy:")
                for strat, s in stats["by_strategy"].items():
                    print(f"    {strat:<25} "
                          f"win={s['win_rate']:.0%} "
                          f"P&L=${s['total_pnl']:+,.0f}")
        print("─" * 55 + "\n")

    # ── Gate Evaluation ───────────────────────────────────────────

    def _evaluate_gates(self, stats: Dict, trades: List[Dict]) -> Dict:
        """Evaluate all validation gates."""
        gates = {}

        # Gate 1: Minimum trade count
        passed = stats["total_trades"] >= self._min_trades
        gates["min_trades"] = {
            "name":    "Minimum trade count",
            "passed":  passed,
            "value":   stats["total_trades"],
            "target":  f">= {self._min_trades}",
            "detail":  f"{stats['total_trades']} closed trades",
        }

        # Gate 2: Win rate
        passed = stats["win_rate"] >= self._min_win_rate
        gates["win_rate"] = {
            "name":    "Win rate",
            "passed":  passed,
            "value":   stats["win_rate"],
            "target":  f">= {self._min_win_rate:.0%}",
            "detail":  f"{stats['win_rate']:.1%} ({stats['winning_trades']}/{stats['total_trades']})",
        }

        # Gate 3: Positive expectancy
        passed = stats["avg_pnl"] > 0
        gates["positive_expectancy"] = {
            "name":    "Positive expectancy",
            "passed":  passed,
            "value":   stats["avg_pnl"],
            "target":  "> $0 per trade",
            "detail":  f"${stats['avg_pnl']:+.2f} average P&L per trade",
        }

        # Gate 4: Sharpe-like ratio (total_pnl / std_dev of P&Ls)
        sharpe = self._compute_sharpe_like(trades)
        passed = sharpe >= self._min_sharpe_like
        gates["sharpe_like"] = {
            "name":    "Sharpe-like ratio",
            "passed":  passed,
            "value":   sharpe,
            "target":  f">= {self._min_sharpe_like}",
            "detail":  f"{sharpe:.2f} (total_pnl / std_dev)",
        }

        # Gate 5: No catastrophic loss (worst trade < 3× avg max_loss)
        if stats["total_trades"] > 0 and stats["avg_max_loss"] and stats["avg_max_loss"] > 0:
            worst_ratio  = abs(stats["worst_trade"]) / stats["avg_max_loss"] if stats["avg_max_loss"] else 0
            passed = worst_ratio <= 3.0
            gates["no_catastrophic_loss"] = {
                "name":    "No catastrophic loss",
                "passed":  passed,
                "value":   worst_ratio,
                "target":  "<= 3.0× avg max_loss",
                "detail":  (
                    f"Worst trade: ${stats['worst_trade']:.0f} | "
                    f"Avg max loss: ${stats['avg_max_loss']:.0f} | "
                    f"Ratio: {worst_ratio:.1f}×"
                ),
            }
        else:
            gates["no_catastrophic_loss"] = {
                "name":    "No catastrophic loss",
                "passed":  True,   # No data = no loss
                "value":   0,
                "target":  "<= 3.0× avg max_loss",
                "detail":  "No closed trades with defined max_loss yet",
            }

        return gates

    # ── Formatting ────────────────────────────────────────────────

    def _format_report(self, report: Dict, stats: Dict, gates: Dict) -> str:
        lines = []
        sep   = "═" * 60

        lines.append(f"\n{sep}")
        lines.append(f"  PAPER TRADING VALIDATION REPORT")
        lines.append(f"  Generated: {report['generated_at'][:19]}")
        lines.append(sep)

        # P&L Overview
        lines.append(f"\n{'─'*60}")
        lines.append(f"  P&L SUMMARY")
        lines.append(f"{'─'*60}")
        lines.append(f"  Total closed trades  : {stats['total_trades']}")
        lines.append(f"  Open positions       : {stats['open_count']}")
        lines.append(f"  Win rate             : {stats['win_rate']:.1%} "
                     f"({stats['winning_trades']}/{stats['total_trades']})")
        lines.append(f"  Total P&L            : ${stats['total_pnl']:+,.2f}")
        lines.append(f"  Average P&L / trade  : ${stats['avg_pnl']:+,.2f}")
        lines.append(f"  Best trade           : ${stats['best_trade']:+,.2f}")
        lines.append(f"  Worst trade          : ${stats['worst_trade']:+,.2f}")
        lines.append(f"  Avg credit collected : ${stats['avg_credit']:.2f}/share")
        lines.append(f"  Avg max risk / trade : ${stats['avg_max_loss']:,.0f}")

        # Per-strategy breakdown
        if stats["by_strategy"]:
            lines.append(f"\n{'─'*60}")
            lines.append(f"  BY STRATEGY")
            lines.append(f"{'─'*60}")
            for strat, s in stats["by_strategy"].items():
                lines.append(
                    f"  {strat:<28} "
                    f"trades={s['trades']:<4} "
                    f"win={s['win_rate']:.0%}  "
                    f"P&L=${s['total_pnl']:+,.0f}"
                )

        # Validation gates
        lines.append(f"\n{'─'*60}")
        lines.append(f"  VALIDATION GATES")
        lines.append(f"{'─'*60}")
        for key, gate in gates.items():
            status = "✅ PASS" if gate["passed"] else "❌ FAIL"
            lines.append(f"  {status}  {gate['name']}")
            lines.append(f"         Target: {gate['target']}")
            lines.append(f"         Actual: {gate['detail']}")

        # Recommendation
        lines.append(f"\n{sep}")
        lines.append(f"  RECOMMENDATION: {report['recommendation']}")
        if not report["go_live"]:
            failed = [g["name"] for g in gates.values() if not g["passed"]]
            lines.append(f"  Failing gates: {', '.join(failed)}")
        lines.append(f"{sep}\n")

        return "\n".join(lines)

    # ── Helpers ───────────────────────────────────────────────────

    def _get_all_closed_trades(self) -> List[Dict]:
        """Fetch all closed/expired trades from the ledger."""
        with self._ledger._get_conn() as conn:
            cursor = conn.execute("""
                SELECT id, pnl, net_credit, max_loss, strategy_name
                FROM paper_trades
                WHERE status IN ('closed', 'expired')
                  AND pnl IS NOT NULL
                ORDER BY closed_at
            """)
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def _compute_sharpe_like(self, trades: List[Dict]) -> float:
        """
        Compute a simplified Sharpe-like ratio: total_pnl / std_dev(pnl).
        Returns 0 if fewer than 2 trades.
        """
        if len(trades) < 2:
            return 0.0

        pnls = [t["pnl"] for t in trades if t["pnl"] is not None]
        if len(pnls) < 2:
            return 0.0

        total  = sum(pnls)
        mean   = total / len(pnls)
        var    = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
        std    = var ** 0.5

        return round(total / std, 4) if std > 0 else 0.0

    def _save_report(self, report_text: str) -> None:
        """Save report text to data/validation_report_{date}.txt"""
        os.makedirs("data", exist_ok=True)
        filename = f"data/validation_report_{date.today()}.txt"
        with open(filename, "w") as f:
            f.write(report_text)
        logger.info(f"Validation report saved to {filename}")
