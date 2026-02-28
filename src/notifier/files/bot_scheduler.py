"""
Bot Scheduler
=============
The event loop that drives the entire bot.

Daily schedule (all times US Eastern):
  09:35  market_scan_job     : Scan universe → generate signals → execute trades
  Every 30 min (market hours): monitor_job : Check all open positions for exits
  16:05  iv_collection_job   : Collect daily ATM IV for IV Rank calculation
  Friday 16:30: weekly_report_job : Generate validation report

Architecture:
  - Uses Python's `schedule` library (pure Python, no external deps)
  - All jobs are synchronous and single-threaded
  - Job failures are caught, logged, and do NOT crash the scheduler
  - Graceful shutdown on SIGINT / SIGTERM

Startup sequence:
  1. Load config
  2. Connect to MooMoo OpenD
  3. Wire all components (scanner, registry, strategies, guard, router, ledger,
     manager, monitor, reporter)
  4. Run an immediate monitor cycle to pick up any positions from prior session
  5. Start scheduler loop

Paper mode guarantees:
  - OrderRouter.is_paper = True prevents any live orders
  - TradeManager._confirm_live_trade() always returns False during paper phase
  - Both checks are independent — two separate safety layers
"""

import signal
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import schedule

from src.connectors.moomoo_connector import MooMooConnector
from src.connectors.yfinance_connector import YFinanceConnector
from src.market.technical_analyser import TechnicalAnalyser
from src.market.iv_rank_calculator import IVRankCalculator
from src.market.regime_detector import RegimeDetector
from src.market.options_analyser import OptionsAnalyser
from src.market.market_scanner import MarketScanner
from src.strategies.strategy_registry import StrategyRegistry
from src.strategies.premium_selling.covered_call import CoveredCallStrategy
from src.strategies.premium_selling.bear_call_spread import BearCallSpreadStrategy
from src.execution.portfolio_guard import PortfolioGuard
from src.execution.order_router import OrderRouter
from src.execution.paper_ledger import PaperLedger
from src.execution.trade_manager import TradeManager
from src.monitoring.exit_evaluator import ExitEvaluator
from src.monitoring.position_monitor import PositionMonitor
from src.monitoring.validation_reporter import ValidationReporter
from src.notifier.signal_notifier import SignalNotifier
from src.logger import get_logger

logger = get_logger("scheduler.bot_scheduler")

ET = ZoneInfo("America/New_York")


class BotScheduler:
    """
    Wires all bot components together and drives the daily schedule.
    """

    def __init__(
        self,
        config:     dict,
        moomoo,
        yfinance,
        scanner,
        registry,
        guard,
        router,
        ledger,
        manager,
        evaluator,
        monitor,
        reporter,
        options_analyser,
        iv_calculator,
        notifier,
    ):
        """
        Low-level constructor — accepts pre-built components.
        Use BotScheduler.build(config) to wire everything from scratch.
        """
        self._config          = config
        self._mode            = config.get("mode", "paper")
        self._running         = False
        self._moomoo          = moomoo
        self._yfinance        = yfinance
        self._scanner         = scanner
        self._registry        = registry
        self._guard           = guard
        self._router          = router
        self._ledger          = ledger
        self._manager         = manager
        self._evaluator       = evaluator
        self._monitor         = monitor
        self._reporter        = reporter
        self._options_analyser = options_analyser
        self._iv_calculator   = iv_calculator
        self._notifier        = notifier

        logger.info(
            f"BotScheduler initialised | mode={self._mode} | "
            f"watchlist={config.get('universe', {}).get('watchlist', [])}"
        )

    @classmethod
    def build(cls, config: dict) -> "BotScheduler":
        """
        Factory method: build a fully-wired BotScheduler from config.
        This is the production entry point.
        """
        logger.info(
            f"Building BotScheduler | mode={config.get('mode')} | "
            f"watchlist={config.get('universe', {}).get('watchlist', [])}"
        )

        moomoo   = MooMooConnector(config)
        yfinance = YFinanceConnector()

        tech_analyser   = TechnicalAnalyser()
        iv_calculator   = IVRankCalculator()
        regime_detector = RegimeDetector(config)
        options         = OptionsAnalyser(config)
        scanner         = MarketScanner(
            config, moomoo, yfinance,
            tech_analyser, options, iv_calculator, regime_detector,
        )

        registry = StrategyRegistry()
        registry.register(CoveredCallStrategy(config, moomoo, options))
        registry.register(BearCallSpreadStrategy(config, moomoo, options))

        guard   = PortfolioGuard(config)
        router  = OrderRouter(config, moomoo)
        ledger  = PaperLedger(config.get("paper_ledger", {}).get(
            "db_path", "data/paper_trades.db"
        ))
        manager = TradeManager(config, guard, router, ledger)

        evaluator = ExitEvaluator(config)
        monitor   = PositionMonitor(config, ledger, manager, moomoo, evaluator)
        reporter  = ValidationReporter(config, ledger)
        notifier  = SignalNotifier()

        logger.info("All components built successfully")

        return cls(
            config=config, moomoo=moomoo, yfinance=yfinance,
            scanner=scanner, registry=registry, guard=guard,
            router=router, ledger=ledger, manager=manager,
            evaluator=evaluator, monitor=monitor, reporter=reporter,
            options_analyser=options, iv_calculator=iv_calculator,
            notifier=notifier,
        )

    # ── Public API ────────────────────────────────────────────────

    def start(self) -> None:
        """
        Connect to MooMoo, set up schedule, and run the event loop.
        Blocks until shutdown signal received.
        """
        # Register shutdown handlers
        signal.signal(signal.SIGINT,  self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        # Connect to MooMoo OpenD
        logger.info("Connecting to MooMoo OpenD...")
        self._moomoo.connect()
        logger.info("Connected to MooMoo OpenD ✅")

        # Run startup monitor cycle (pick up positions from prior session)
        logger.info("Running startup monitor cycle...")
        self._safe_run(self._monitor_job, "startup_monitor", force=True)

        # Register scheduled jobs
        sched_cfg = self._config.get("scheduler", {})
        scan_time   = sched_cfg.get("market_scan_time",   "09:35")
        iv_time     = sched_cfg.get("iv_collection_time", "16:05")
        report_day  = sched_cfg.get("weekly_report_day",  "friday")
        report_time = sched_cfg.get("weekly_report_time", "16:30")
        interval    = self._config.get(
            "position_monitor", {}
        ).get("check_interval_minutes", 30)

        schedule.every().day.at(scan_time).do(self._safe_run, self._scan_job, "scan")
        schedule.every(interval).minutes.do(self._safe_run, self._monitor_job, "monitor")
        schedule.every().day.at(iv_time).do(self._safe_run, self._iv_job, "iv_collection")
        getattr(schedule.every(), report_day).at(report_time).do(
            self._safe_run, self._report_job, "weekly_report"
        )

        logger.info(
            f"Scheduler running | "
            f"scan={scan_time} ET | "
            f"monitor=every {interval}min | "
            f"iv_collection={iv_time} ET | "
            f"weekly_report={report_day} {report_time} ET"
        )

        self._reporter.print_current_status()
        self._running = True

        while self._running:
            schedule.run_pending()
            time.sleep(30)

    def stop(self) -> None:
        """Graceful shutdown — completes current job then exits."""
        logger.info("Scheduler stopping...")
        self._running = False
        schedule.clear()
        try:
            self._moomoo.disconnect()
        except Exception:
            pass
        logger.info("Scheduler stopped")

    # ── Scheduled Jobs ────────────────────────────────────────────

    def _scan_job(self) -> None:
        """
        09:35 ET: Scan universe → evaluate strategies → execute signals.
        Core entry/signal pipeline.
        """
        logger.info(f"[SCAN JOB] Starting | {datetime.now(ET).strftime('%H:%M ET')}")
        start = time.time()

        snapshots = self._scanner.scan_universe()
        if not snapshots:
            logger.warning("[SCAN JOB] No snapshots returned — check API connectivity")
            return

        signals = self._registry.evaluate_universe(snapshots)
        logger.info(f"[SCAN JOB] {len(snapshots)} snapshots → {len(signals)} signal(s)")

        if signals:
            results  = self._manager.process_signals(signals)
            executed = sum(1 for r in results if r.executed)
            blocked  = sum(1 for r in results if not r.approved)
            logger.info(
                f"[SCAN JOB] Signals processed: "
                f"{executed} executed | {blocked} blocked by portfolio guard"
            )
            # Notify for manual execution (all approved signals, executed or not)
            notifiable = [
                r.signal for r in results
                if r.approved or not r.executed
            ]
            if notifiable:
                self._notifier.notify(notifiable)

        elapsed = time.time() - start
        logger.info(f"[SCAN JOB] Complete in {elapsed:.1f}s")

    def _monitor_job(self, force: bool = False) -> None:
        """
        Every 30 min during market hours: check all open positions for exits.
        """
        actions = self._monitor.run_cycle(force=force)
        if actions:
            exits = [(a["symbol"], a["reason"], f"${a['pnl']:+.0f}") for a in actions]
            logger.info(f"[MONITOR JOB] {len(actions)} exit(s): {exits}")

    def _iv_job(self) -> None:
        """
        16:05 ET: Collect end-of-day ATM IV for each watchlist symbol.
        Feeds the IV Rank calculator (requires 30 days minimum, 252 for full reliability).
        """
        logger.info("[IV JOB] Collecting end-of-day IV...")
        watchlist = self._config.get("universe", {}).get("watchlist", [])

        for symbol in watchlist:
            try:
                expiries = self._moomoo.get_option_expiries(symbol)
                if not expiries:
                    continue
                # Use nearest expiry for ATM IV
                chain  = self._moomoo.get_option_chain(symbol, expiries[0], "CALL")
                if chain is None or len(chain) == 0:
                    continue
                spot   = self._yfinance.get_current_price(symbol)
                snap   = self._moomoo.get_option_snapshot(chain["code"].tolist()[:10])
                if snap is None or len(snap) == 0:
                    continue
                atm_iv = self._options_analyser.get_atm_iv(chain, snap, spot)
                if atm_iv and atm_iv > 0:
                    self._iv_calculator.store_daily_iv(symbol, atm_iv)
                    logger.info(f"[IV JOB] {symbol}: ATM IV={atm_iv:.1f}%")
            except Exception as e:
                logger.error(f"[IV JOB] Failed for {symbol}: {e}")

    def _report_job(self) -> None:
        """
        Friday 16:30 ET: Generate weekly validation report.
        This is the GATE that determines readiness for live trading.
        """
        logger.info("[REPORT JOB] Generating weekly validation report...")
        report = self._reporter.generate(save_to_file=True)
        if report["go_live"]:
            logger.warning(
                "⚠️  VALIDATION GATES ALL PASSED — "
                "Review report before switching to live mode. "
                "Change config.yaml mode: live to proceed."
            )
        else:
            failed = [g["name"] for g in report["gates"].values()
                      if not g["passed"]]
            logger.info(
                f"[REPORT JOB] Not yet ready for live. "
                f"Failing gates: {failed}"
            )

    # ── Safety Wrapper ────────────────────────────────────────────

    def _safe_run(self, job_fn, job_name: str, **kwargs) -> None:
        """
        Execute a job with full exception isolation.
        One failing job never crashes the scheduler.
        """
        try:
            job_fn(**kwargs)
        except Exception as e:
            logger.error(
                f"Job '{job_name}' raised an unhandled exception: {e}",
                exc_info=True
            )

    # ── Shutdown Handler ──────────────────────────────────────────

    def _handle_shutdown(self, signum, frame) -> None:
        """Handle SIGINT / SIGTERM gracefully."""
        logger.info(f"Shutdown signal received ({signum}) — stopping...")
        self.stop()
        sys.exit(0)
