"""
Exit Evaluator
==============
Defines and evaluates exit rules for open positions.

Three exit triggers (in priority order):
  1. STOP_LOSS    : Current price >= stop_loss_multiplier × net_credit
                   Default: close when position costs 2× what we collected
                   e.g. sold for $2.00 → close if current price >= $4.00
                   (loss = $2.00 × 100 = $200 per contract)

  2. TAKE_PROFIT  : Current price <= take_profit_pct × net_credit
                   Default: close when we've kept 50% of the premium
                   e.g. sold for $2.00 → close if current price <= $1.00
                   (profit = $1.00 × 100 = $100 per contract)

  3. DTE_CLOSE    : DTE <= dte_close_threshold
                   Default: close at 21 DTE to avoid gamma risk
                   regardless of profit/loss level

  4. EXPIRED      : Option has expired (DTE = 0), close at market
                   If expired OTM → max profit realised
                   If expired ITM → assignment risk for covered calls

Exit priority: STOP_LOSS > DTE_CLOSE > TAKE_PROFIT
(Stop losses are always executed first regardless of other conditions)

All thresholds are config-driven under position_monitor.exit_rules.
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional

from src.logger import get_logger

logger = get_logger("monitoring.exit_evaluator")


@dataclass
class ExitDecision:
    """
    Result of evaluating exit rules for a single position.
    """
    should_exit:  bool
    reason:       str           # "stop_loss" | "take_profit" | "dte_close" | "expired" | "hold"
    close_urgency: str          # "immediate" | "end_of_day" | "hold"
    current_price: float        # Current mid-price of the position (net debit to close)
    net_credit:    float        # Original net credit collected
    unrealised_pnl: float       # (net_credit - current_price) × 100 per contract
    pnl_pct:       float        # unrealised_pnl / max_profit (0-1)


class ExitEvaluator:
    """
    Evaluates exit conditions for open positions.
    Stateless — all state comes from the trade record and current market data.
    """

    def __init__(self, config: dict):
        exit_cfg = config.get("position_monitor", {}).get("exit_rules", {})

        self._stop_loss_multiplier   = exit_cfg.get("stop_loss_multiplier",   3.0)
        self._min_days_before_stop   = exit_cfg.get("min_days_before_stop",    5)
        self._take_profit_pct        = exit_cfg.get("take_profit_pct",        0.50)
        self._dte_close_threshold    = exit_cfg.get("dte_close_threshold",    21)
        self._expired_dte_threshold  = exit_cfg.get("expired_dte_threshold",  0)

        logger.info(
            f"ExitEvaluator | stop_loss={self._stop_loss_multiplier}×credit | "
            f"min_hold={self._min_days_before_stop}d | "
            f"take_profit={self._take_profit_pct:.0%} | "
            f"dte_close={self._dte_close_threshold}d"
        )

    def evaluate(
        self,
        trade_id:      int,
        net_credit:    float,
        max_profit:    float,
        expiry:        str,
        current_price: float,
        opened_at:     Optional[str] = None,   # ISO datetime string from ledger
    ) -> ExitDecision:
        """
        Evaluate all exit rules for an open position.

        Args:
            trade_id     : PaperLedger trade ID (for logging)
            net_credit   : Original net credit collected per share
            max_profit   : Max profit per contract (net_credit × 100)
            expiry       : Option expiry "YYYY-MM-DD"
            current_price: Current mid-price to close the position (net debit)

        Returns:
            ExitDecision with should_exit flag and reason.
        """
        today         = date.today()
        expiry_date   = date.fromisoformat(expiry)
        dte           = max(0, (expiry_date - today).days)

        # Days held — used to enforce minimum holding period before stop loss
        days_held = 0
        if opened_at:
            try:
                from datetime import datetime
                open_date = datetime.fromisoformat(str(opened_at)).date()
                days_held = max(0, (today - open_date).days)
            except Exception:
                days_held = 0  # Unknown — don't block stop loss

        unrealised_pnl = (net_credit - current_price) * 100
        pnl_pct        = unrealised_pnl / max_profit if max_profit > 0 else 0

        # ── Rule 1: Expired ────────────────────────────────────────
        if dte <= self._expired_dte_threshold:
            logger.info(
                f"Trade #{trade_id}: EXPIRED | DTE={dte} | "
                f"current=${current_price:.2f} | P&L=${unrealised_pnl:+.0f}"
            )
            return ExitDecision(
                should_exit=    True,
                reason=         "expired_worthless" if current_price < 0.05 else "expired",
                close_urgency=  "immediate",
                current_price=  current_price,
                net_credit=     net_credit,
                unrealised_pnl= unrealised_pnl,
                pnl_pct=        pnl_pct,
            )

        # ── Rule 2: Stop Loss ─────────────────────────────────────
        stop_loss_price = net_credit * self._stop_loss_multiplier
        stop_eligible   = days_held >= self._min_days_before_stop
        if current_price >= stop_loss_price:
            if stop_eligible:
                logger.info(
                    f"Trade #{trade_id}: STOP LOSS | "
                    f"current=${current_price:.2f} >= stop=${stop_loss_price:.2f} | "
                    f"P&L=${unrealised_pnl:+.0f} | DTE={dte} | held={days_held}d"
                )
                return ExitDecision(
                    should_exit=    True,
                    reason=         "stop_loss",
                    close_urgency=  "immediate",
                    current_price=  current_price,
                    net_credit=     net_credit,
                    unrealised_pnl= unrealised_pnl,
                    pnl_pct=        pnl_pct,
                )
            else:
                logger.info(
                    f"Trade #{trade_id}: STOP LOSS SUPPRESSED | "
                    f"current=${current_price:.2f} >= stop=${stop_loss_price:.2f} | "
                    f"held={days_held}d < min={self._min_days_before_stop}d — holding"
                )

        # ── Rule 3: DTE close ─────────────────────────────────────
        if dte <= self._dte_close_threshold:
            logger.info(
                f"Trade #{trade_id}: DTE CLOSE | DTE={dte} <= {self._dte_close_threshold} | "
                f"current=${current_price:.2f} | P&L=${unrealised_pnl:+.0f}"
            )
            return ExitDecision(
                should_exit=    True,
                reason=         "dte_close",
                close_urgency=  "end_of_day",
                current_price=  current_price,
                net_credit=     net_credit,
                unrealised_pnl= unrealised_pnl,
                pnl_pct=        pnl_pct,
            )

        # ── Rule 4: Take Profit ───────────────────────────────────
        take_profit_price = net_credit * (1 - self._take_profit_pct)
        if current_price <= take_profit_price:
            logger.info(
                f"Trade #{trade_id}: TAKE PROFIT | "
                f"current=${current_price:.2f} <= target=${take_profit_price:.2f} | "
                f"P&L=${unrealised_pnl:+.0f} ({pnl_pct:.0%}) | DTE={dte}"
            )
            return ExitDecision(
                should_exit=    True,
                reason=         "take_profit",
                close_urgency=  "end_of_day",
                current_price=  current_price,
                net_credit=     net_credit,
                unrealised_pnl= unrealised_pnl,
                pnl_pct=        pnl_pct,
            )

        # ── Hold ──────────────────────────────────────────────────
        logger.debug(
            f"Trade #{trade_id}: HOLD | DTE={dte} | "
            f"current=${current_price:.2f} | P&L=${unrealised_pnl:+.0f} ({pnl_pct:.0%}) | "
            f"stop=${stop_loss_price:.2f} | take_profit=${take_profit_price:.2f}"
        )
        return ExitDecision(
            should_exit=    False,
            reason=         "hold",
            close_urgency=  "hold",
            current_price=  current_price,
            net_credit=     net_credit,
            unrealised_pnl= unrealised_pnl,
            pnl_pct=        pnl_pct,
        )
