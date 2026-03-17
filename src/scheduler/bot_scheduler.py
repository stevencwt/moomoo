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
import re
import sys
import time
import json
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

import schedule

from src.connectors.broker_factory import build_connectors
from src.connectors.yfinance_connector import YFinanceConnector
from src.market.technical_analyser import TechnicalAnalyser
from src.market.iv_rank_calculator import IVRankCalculator
from src.market.regime_detector import RegimeDetector
from src.market.options_analyser import OptionsAnalyser
from src.market.market_scanner import MarketScanner
from src.strategies.strategy_registry import StrategyRegistry
from src.strategies.premium_selling.covered_call import CoveredCallStrategy
from src.strategies.premium_selling.bear_call_spread import BearCallSpreadStrategy
from src.strategies.premium_selling.bull_put_spread import BullPutSpreadStrategy
from src.execution.portfolio_guard import PortfolioGuard
from src.execution.order_router import OrderRouter
from src.execution.paper_ledger import PaperLedger
from src.execution.trade_manager import TradeManager
from src.monitoring.exit_evaluator import ExitEvaluator
from src.monitoring.position_monitor import PositionMonitor
from src.monitoring.validation_reporter import ValidationReporter
from src.notifier.signal_notifier import SignalNotifier
from src.logger import get_logger

# ── LLM Regime (optional — graceful degradation if not installed) ────────────
try:
    from src.market.llm_regime_bridge import LLMRegimeBridgePool
    from src.market.regime_combined import CombinedRegime
    _LLM_REGIME_AVAILABLE = True
except ImportError:
    _LLM_REGIME_AVAILABLE = False

logger = get_logger("scheduler.bot_scheduler")

ET = ZoneInfo("America/New_York")

W = 64   # console width


# ── Console helpers ───────────────────────────────────────────────

def _now_et() -> str:
    return datetime.now(ET).strftime("%H:%M ET")

def _ts() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")

def _bar(char="═") -> str:
    return char * W

def _header(title: str, char="═") -> None:
    print(_bar(char))
    pad = (W - len(title) - 2) // 2
    print(f"{'':>{pad}} {title} ")
    print(_bar(char))

def _section(title: str) -> None:
    print(f"\n  ── {title} {'─' * (W - len(title) - 6)}")

def _row(label: str, value: str, width: int = 22) -> None:
    print(f"  {label:<{width}} {value}")

def _blank() -> None:
    print()

def _regime_icon(regime: str) -> str:
    return {"bull": "📈", "bear": "📉", "neutral": "➡️ ",
            "high_vol": "⚡"}.get(regime, "?")

def _gate(passed: bool, label: str, detail: str = "") -> str:
    icon = "✅" if passed else "❌"
    suffix = f"  ({detail})" if detail else ""
    return f"    {icon}  {label}{suffix}"

def _exit_icon(reason: str) -> str:
    return {"stop_loss": "🛑 STOP LOSS", "take_profit": "💰 TAKE PROFIT",
            "dte_close": "⏰ DTE CLOSE", "expired_worthless": "✔️  EXPIRED",
            "expired": "⚠️  EXPIRED ITM"}.get(reason, f"⚠️  {reason.upper()}")


def _et_to_local(time_str: str) -> str:
    """
    Convert a "HH:MM" time string expressed in US Eastern Time
    to the equivalent local system time string for the schedule library.

    The schedule library has no timezone awareness — it fires jobs based
    on the system clock. If the bot runs in Singapore (SGT = UTC+8),
    "09:35 ET" must be registered as "22:35" (SGT, previous calendar day
    handled automatically by schedule's daily scheduling).

    Args:
        time_str: "HH:MM" in US Eastern Time

    Returns:
        "HH:MM" in local system time
    """
    from zoneinfo import ZoneInfo
    h, m    = map(int, time_str.split(":"))
    et      = ZoneInfo("America/New_York")
    now_et  = datetime.now(et)
    dt_et   = now_et.replace(hour=h, minute=m, second=0, microsecond=0)
    dt_local = dt_et.astimezone()   # convert to system local timezone
    local_str = dt_local.strftime("%H:%M")
    if local_str != time_str:
        logger_ref = __import__('logging').getLogger('options_bot.scheduler.bot_scheduler')
        logger_ref.info(
            f"Schedule time conversion: {time_str} ET → {local_str} local "
            f"(system timezone: {dt_local.tzname()})"
        )
    return local_str


class BotScheduler:
    """
    Wires all bot components together and drives the daily schedule.
    """

    def __init__(
        self,
        config,
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
        exec_connector=None,
    ):
        self._config          = config
        self._mode            = config.get("mode", "paper")
        self._running         = False
        self._moomoo          = moomoo
        self._exec_connector  = exec_connector
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

        self._start_time      = None
        self._last_heartbeat  = 0
        self._scan_count      = 0
        self._monitor_count   = 0
        self._morning_snapshots: dict = {}   # symbol → MarketSnapshot from 09:35 scan
        self._last_unrealised_pnl: float = 0.0  # updated by monitor job, used by heartbeat
        self._llm_pool  = None   # LLMRegimeBridgePool — set by build()
        self._combined  = None   # CombinedRegime — set by build()

        logger.info(
            f"BotScheduler initialised | mode={self._mode} | "
            f"watchlist={config.get('universe', {}).get('watchlist', [])}"
        )

    @classmethod
    def build(cls, config: dict) -> "BotScheduler":
        """
        Factory method: build a fully-wired BotScheduler from config.
        """
        logger.info(
            f"Building BotScheduler | mode={config.get('mode')} | "
            f"watchlist={config.get('universe', {}).get('watchlist', [])}"
        )

        data_connector, exec_connector = build_connectors(config)

        broker_cfg  = config.get("broker", {})
        data_name   = broker_cfg.get("data", "moomoo") if isinstance(broker_cfg, dict) else broker_cfg
        exec_name   = broker_cfg.get("execution", "moomoo") if isinstance(broker_cfg, dict) else broker_cfg
        logger.info(
            f"Connectors | data={data_name} | execution={exec_name} | "
            f"shared={'yes' if data_connector is exec_connector else 'no'}"
        )

        yfinance        = YFinanceConnector()
        tech_analyser   = TechnicalAnalyser()
        iv_calculator   = IVRankCalculator()
        regime_detector = RegimeDetector(config)
        options         = OptionsAnalyser(config)

        scanner = MarketScanner(
            config, data_connector, yfinance,
            tech_analyser, options, iv_calculator, regime_detector,
        )

        registry = StrategyRegistry()
        registry.register(CoveredCallStrategy(config, data_connector, options))
        registry.register(BearCallSpreadStrategy(config, data_connector, options))
        registry.register(BullPutSpreadStrategy(config, data_connector, options))

        guard  = PortfolioGuard(config)
        router = OrderRouter(config, exec_connector)
        ledger = PaperLedger(config.get("paper_ledger", {}).get(
            "db_path", "data/paper_trades.db"
        ))
        # Restore open positions from ledger so guard has accurate state
        # after a restart — prevents duplicate trades on startup scan
        guard.restore_from_ledger(ledger)
        manager   = TradeManager(config, guard, router, ledger)
        evaluator = ExitEvaluator(config)
        monitor   = PositionMonitor(config, ledger, manager, data_connector, evaluator)
        reporter  = ValidationReporter(config, ledger)
        notifier  = SignalNotifier()

        logger.info("All components built successfully")

        # ── LLM Regime pool (optional) ────────────────────────────────────
        llm_pool       = None
        combined_regime = None
        if _LLM_REGIME_AVAILABLE:
            try:
                from src.market.regime_bridge import bridge_instance as _qbridge
                _watchlist = config.get("universe", {}).get("watchlist", [])
                _llm_cfg   = config.get("llm_regime", {})
                llm_pool   = LLMRegimeBridgePool(
                    symbols           = _watchlist,
                    yfinance          = yfinance,
                    provider          = _llm_cfg.get("provider", "gemini"),
                    htf_interval_secs = _llm_cfg.get("htf_interval_secs", 7200),   # 2h
                    ltf_interval_secs = _llm_cfg.get("ltf_interval_secs", 1800),   # 30min
                    min_confidence    = _llm_cfg.get("min_confidence", 3),
                )
                combined_regime = CombinedRegime(
                    quant_bridge = _qbridge,
                    llm_pool     = llm_pool,
                )
                logger.info(
                    f"[LLM Regime] pool initialized | "
                    f"{len(_watchlist)} symbols | "
                    f"provider={_llm_cfg.get('provider','gemini')} | "
                    f"HTF={_llm_cfg.get('htf_interval_secs',7200)//3600}h "
                    f"LTF={_llm_cfg.get('ltf_interval_secs',1800)//60}min"
                )
            except Exception as _le:
                logger.warning(f"[LLM Regime] pool init failed: {_le}")

        _scheduler = cls(
            config=config, moomoo=data_connector, yfinance=yfinance,
            scanner=scanner, registry=registry, guard=guard,
            router=router, ledger=ledger, manager=manager,
            evaluator=evaluator, monitor=monitor, reporter=reporter,
            options_analyser=options, iv_calculator=iv_calculator,
            notifier=notifier, exec_connector=exec_connector,
        )
        _scheduler._llm_pool = llm_pool
        _scheduler._combined = combined_regime
        return _scheduler

    # ── Public API ────────────────────────────────────────────────

    def start(self) -> None:
        signal.signal(signal.SIGINT,  self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        # Connect brokers
        self._moomoo.connect()
        if self._exec_connector is not None and self._exec_connector is not self._moomoo:
            try:
                self._exec_connector.connect()
            except Exception as _ce:
                mode = self._config.get('mode', 'paper').lower()
                if mode == 'paper':
                    logger.warning(
                        f"IBKR connection failed (paper mode — continuing without it): {_ce}"
                    )
                    self._exec_connector = None
                else:
                    raise

        self._start_time = datetime.now(ET)
        self._print_startup_banner()

        # Startup monitor — show open positions from prior session
        self._safe_run(self._monitor_job, "startup_monitor", force=True)

        # Register scheduled jobs
        sched_cfg   = self._config.get("scheduler", {})
        scan_time_et   = sched_cfg.get("market_scan_time",   "09:35")
        iv_time_et     = sched_cfg.get("iv_collection_time", "16:05")
        report_day     = sched_cfg.get("weekly_report_day",  "friday")
        report_time_et = sched_cfg.get("weekly_report_time", "16:30")
        interval       = self._config.get(
            "position_monitor", {}
        ).get("check_interval_minutes", 30)

        # Convert ET config times to local system time for the schedule library
        # (schedule has no timezone awareness — it fires based on system clock)
        scan_time   = _et_to_local(scan_time_et)
        iv_time     = _et_to_local(iv_time_et)
        report_time = _et_to_local(report_time_et)

        schedule.every().day.at(scan_time).do(self._safe_run, self._scan_job, "scan")
        schedule.every(interval).minutes.do(self._safe_run, self._monitor_job, "monitor")
        schedule.every().day.at(iv_time).do(self._safe_run, self._iv_job, "iv_collection")
        getattr(schedule.every(), report_day).at(report_time).do(
            self._safe_run, self._report_job, "weekly_report"
        )

        # Display uses original ET times so output is human-readable
        self._print_schedule(scan_time_et, interval, iv_time_et, report_day, report_time_et)

        # ── Immediate startup scan ────────────────────────────────
        # Run a full scan right away so there's no blind period between
        # bot start and the next scheduled 09:35 ET scan.
        # Only runs during market hours (09:35–16:00 ET) to avoid
        # scanning outside trading hours when data is stale/unavailable.
        now_et = datetime.now(ET)
        hour   = now_et.hour + now_et.minute / 60
        if 9.6 <= hour < 16.0:
            self._safe_run(self._scan_job, "scan")
            # Populate positions_mark.json immediately so dashboard shows P&L on first load.
            # skip_rescan=True avoids a second option chain fetch right after the scan
            # which would breach MooMoo's 10 requests/30s rate limit.
            self._safe_run(self._monitor_job, "monitor", skip_rescan=True)
        else:
            print(f"  (startup scan/monitor skipped — outside market hours)\n")

        self._running = True
        while self._running:
            schedule.run_pending()
            self._heartbeat()
            time.sleep(30)

    def stop(self) -> None:
        logger.info("Scheduler stopping...")
        self._running = False
        schedule.clear()
        self._print_shutdown_banner()
        try:
            self._moomoo.disconnect()
        except Exception:
            pass
        if self._exec_connector is not None and self._exec_connector is not self._moomoo:
            try:
                self._exec_connector.disconnect()
            except Exception:
                pass
        logger.info("Scheduler stopped")

    # ── Startup / Shutdown display ────────────────────────────────

    def _print_startup_banner(self) -> None:
        broker_cfg = self._config.get("broker", {})
        data_b     = broker_cfg.get("data", "moomoo").upper()
        exec_b     = broker_cfg.get("execution", "ibkr").upper()
        watchlist  = self._config.get("universe", {}).get("watchlist", [])
        mode       = self._mode.upper()
        mode_icon  = "🔴 LIVE" if mode == "LIVE" else "📄 PAPER"
        ibkr_acc   = self._config.get("ibkr", {}).get("account", "—")

        _blank()
        print(_bar("═"))
        print(f"  OPTIONS BOT  │  {mode_icon}  │  {data_b} data + {exec_b} execution")
        print(_bar("═"))
        _row("Watchlist:",    ", ".join(watchlist))
        _row("IBKR account:", ibkr_acc)
        _row("Mode:",         mode)
        _row("Started:",      self._start_time.strftime("%Y-%m-%d %H:%M:%S ET"))
        print(_bar("─"))
        _blank()

    def _print_schedule(self, scan_time, interval, iv_time, report_day, report_time) -> None:
        _section("SCHEDULE  (all times US Eastern)")
        _row("Market scan:",    f"daily at {scan_time} ET")
        _row("Position check:", f"every {interval} min during market hours")
        _row("IV collection:",  f"daily at {iv_time} ET")
        _row("Weekly report:",  f"{report_day.capitalize()} at {report_time} ET")
        _blank()
        print(_bar("─"))
        _blank()

    def _print_shutdown_banner(self) -> None:
        try:
            stats       = self._ledger.get_statistics()
            open_count  = int(stats.get("open_count", 0) or 0)
            total       = int(stats.get("total_trades", 0) or 0)
            total_pnl   = float(stats.get("total_pnl", 0) or 0)
            win_rate    = float(stats.get("win_rate", 0) or 0)
        except Exception:
            open_count = total = 0
            total_pnl = win_rate = 0.0
        _blank()
        print(_bar("═"))
        print(f"  BOT STOPPED  │  {_ts()}")
        print(_bar("─"))
        _row("Scan cycles run:",    str(self._scan_count))
        _row("Monitor cycles run:", str(self._monitor_count))
        _row("Open positions:",     str(open_count))
        _row("Closed trades:",      str(total))
        if total > 0:
            _row("Total P&L:",      f"${total_pnl:+,.2f}")
            _row("Win rate:",       f"{win_rate:.0%}")
        print(_bar("═"))
        _blank()

    # ── Heartbeat ─────────────────────────────────────────────────

    def _heartbeat(self) -> None:
        """Print a brief alive line every 5 minutes between jobs."""
        now = time.time()
        if now - self._last_heartbeat < 300:   # 5 minutes
            return
        self._last_heartbeat = now

        stats     = self._ledger.get_statistics()
        open_cnt  = stats.get("open_count", 0)
        total_pnl = stats.get("total_pnl", 0) or 0

        # Next scheduled job — convert local next_run back to ET for display
        from zoneinfo import ZoneInfo as _ZI
        next_jobs  = sorted(schedule.jobs, key=lambda j: j.next_run)
        if next_jobs:
            import datetime as _dt
            nxt_local = next_jobs[0].next_run          # naive local datetime from schedule
            nxt_aware = nxt_local.replace(tzinfo=_dt.datetime.now().astimezone().tzinfo)
            next_job  = nxt_aware.astimezone(_ZI("America/New_York")).strftime("%H:%M ET")
        else:
            next_job  = "—"

        # Build P&L string: realised (closed trades) + unrealised (open positions)
        has_closed = stats.get("total_trades", 0) > 0
        realised_str   = f"realised ${total_pnl:+,.0f}" if has_closed else "no closed trades"
        unrealised_str = f"  open ${self._last_unrealised_pnl:+,.0f}" if self._last_unrealised_pnl != 0.0 else ""
        pnl_str = f"P&L {realised_str}{unrealised_str}"

        print(
            f"  [{_now_et()}] ♥  bot alive  │  "
            f"{open_cnt} open position{'s' if open_cnt != 1 else ''}  │  "
            f"{pnl_str}  │  next job: {next_job}"
        )

    # ── SCAN JOB ─────────────────────────────────────────────────

    def _scan_job(self) -> None:
        self._scan_count += 1
        _blank()
        print(_bar("═"))
        print(f"  SCAN JOB  #{self._scan_count}  │  {_ts()}")
        print(_bar("═"))

        logger.info(f"[SCAN JOB] Starting | {_now_et()}")
        start = time.time()

        snapshots = self._scanner.scan_universe()
        # Cache for intraday rescans — keyed by symbol
        self._morning_snapshots = {s.symbol: s for s in snapshots}
        if not snapshots:
            print(f"  ❌  No snapshots returned — check API connectivity")
            logger.warning("[SCAN JOB] No snapshots returned")
            print(_bar("─"))
            return

        # ── Print each symbol snapshot ────────────────────────────
        for snap in snapshots:
            try:
                regime_str = f"{_regime_icon(snap.market_regime)} {snap.market_regime}"
                iv_days    = self._iv_calculator.get_days_stored(snap.symbol) if hasattr(self._iv_calculator, "get_days_stored") else None
                iv_note    = f"  (⚠ only {iv_days}d history)" if iv_days and iv_days < 30 else ""
                shares_str = f"{snap.shares_held} shares" if snap.shares_held else "no shares"
                earnings_str = f"{snap.days_to_earnings}d to earnings" if snap.days_to_earnings else "no earnings soon"

                # ── regime_v2 summary (HMM-based, additive) ──
                r2 = snap.regime_v2 or {}
                if r2:
                    r2_cons  = r2.get('consensus_state',  '—')
                    r2_logic = r2.get('recommended_logic','—')
                    r2_conf  = r2.get('confidence_score',  0.0)
                    r2_vol   = r2.get('volatility_regime', '—')
                    r2_str   = (
                        f"{r2_cons}/{r2_logic} "
                        f"(conf={r2_conf:.2f} vol={r2_vol})"
                    )
                else:
                    r2_str = "—"

                _section(snap.symbol)
                print(
                    f"  Price    ${snap.spot_price:.2f}  │  "
                    f"Regime {regime_str}  │  VIX {snap.vix:.1f}"
                )
                print(
                    f"  RSI      {snap.technicals.rsi:.0f}  │  "
                    f"%B {snap.technicals.pct_b:.2f}  │  "
                    f"MACD {snap.technicals.macd:.2f}  │  "
                    f"IV Rank {snap.options_context.iv_rank:.0f}{iv_note}"
                )
                print(f"  Regime v2: {r2_str}")
                print(
                    f"  {shares_str}  │  "
                    f"{len(snap.options_context.available_expiries)} expiries available  │  "
                    f"{earnings_str}"
                )

                # ── Gate analysis per strategy ─────────────────────
                self._print_gate_analysis(snap)
            except Exception as _disp_err:
                _section(getattr(snap, "symbol", str(snap)))
                print(f"  (display error: {_disp_err})")

        # ── Evaluate strategies and execute signals ───────────────
        signals = self._registry.evaluate_universe(snapshots)

        if not signals:
            _blank()
            print(f"  ── Result: no signals generated this cycle")
        else:
            _blank()
            print(f"  ── {len(signals)} signal(s) generated — ranking candidates")

            # ── Rank for display (pure call — no side effects) ────
            ranked = self._manager._ranker.rank(signals)

            # ── Execute (ranker runs internally in process_signals) ──
            results = self._manager.process_signals(signals)

            # ── Ranked table then per-signal detail ─────────────
            self._print_ranking_table(ranked, results)
            for res in results:
                self._print_trade_result(res)

            # Notify
            # Only notify for signals approved but NOT auto-executed (need manual action)
            # Paper fills (r.executed=True) are auto-recorded — no bell needed
            notifiable = [r.signal for r in results if r.approved and not r.executed]
            if notifiable:
                self._notifier.notify(notifiable)

        elapsed = time.time() - start
        _blank()
        print(_bar("─"))
        print(f"  Scan complete in {elapsed:.1f}s  │  {_now_et()}")
        print(_bar("─"))
        _blank()
        logger.info(f"[SCAN JOB] Complete in {elapsed:.1f}s | {len(snapshots)} symbols | {len(signals)} signals")

        # ── Persist scan results for dashboard ────────────────────
        try:
            _ranked   = ranked   if signals else []
            _results  = results  if signals else []
            scan_data = self._build_scan_data(snapshots, signals, _ranked, _results, elapsed)
            self._write_scan_results(scan_data)
        except Exception as _e:
            logger.warning(f"[SCAN JOB] Could not write scan_results.json: {_e}")

    def _print_gate_analysis(self, snap) -> None:
        """
        Print ALL pass/fail gates for each strategy — never short-circuits.
        After evaluate_universe() runs, strategy.last_skip_reason provides
        the exact final verdict including option-chain gate results.

        open_positions is read from the ledger (not the broker snapshot) so
        paper trades are counted correctly.
        """
        # Count open positions per strategy from ledger — accurate in paper mode
        open_trades = self._ledger.get_open_trades()
        symbol_ticker = snap.symbol.replace("US.", "")
        open_bcs = sum(1 for t in open_trades
                       if t["strategy_name"] == "bear_call_spread")
        open_bps = sum(1 for t in open_trades
                       if t["strategy_name"] == "bull_put_spread")
        open_cc  = sum(1 for t in open_trades
                       if t["strategy_name"] == "covered_call")

        # ── Covered call ──────────────────────────────────────────
        cc_cfg  = self._config.get("strategies", {}).get("covered_call", {})
        cc_on   = cc_cfg.get("enabled", True)
        _blank()
        print(f"  covered_call{'  [DISABLED]' if not cc_on else ''}:")
        if cc_on:
            min_iv  = cc_cfg.get("min_iv_rank", 30)
            max_rsi = cc_cfg.get("max_rsi", 70)
            max_pos = cc_cfg.get("max_concurrent_positions", 2)
            g = [
                (snap.shares_held >= 100,
                    f"shares ≥ 100",                          f"held={snap.shares_held}"),
                (open_cc < max_pos,
                    f"positions < {max_pos}",                 f"open={open_cc}"),
                (snap.market_regime != "high_vol",
                    f"regime ≠ high_vol",                     f"regime={snap.market_regime}"),
                (snap.options_context.iv_rank >= min_iv,
                    f"IV rank ≥ {min_iv}",                    f"iv_rank={snap.options_context.iv_rank:.0f}"),
                (snap.technicals.rsi <= max_rsi,
                    f"RSI ≤ {max_rsi}",                       f"rsi={snap.technicals.rsi:.0f}"),
            ]
            all_pass = True
            for passed, label, detail in g:
                print(_gate(passed, label, detail))
                if not passed:
                    all_pass = False
            if all_pass:
                # All pre-scan gates pass — verdict comes from strategy after chain eval
                strat = next((s for s in self._registry._strategies
                               if s.name == "covered_call"), None)
                reason = strat.last_skip_reason if strat else ""
                if reason:
                    print(f"    ↳  chain evaluated  →  ❌  {reason}")
                else:
                    print(f"    ↳  all gates passed — signal generated ✅")

        # ── Bear call spread ──────────────────────────────────────
        bcs_cfg = self._config.get("strategies", {}).get("bear_call_spread", {})
        bcs_on  = bcs_cfg.get("enabled", True)
        _blank()
        print(f"  bear_call_spread{'  [DISABLED]' if not bcs_on else ''}:")
        if bcs_on:
            min_iv    = bcs_cfg.get("min_iv_rank", 35)
            min_rsi   = bcs_cfg.get("min_rsi_for_spread", 45)
            min_pct_b = bcs_cfg.get("min_pct_b", 0.40)
            min_cred  = bcs_cfg.get("min_credit", 0.50)
            min_rr    = bcs_cfg.get("min_reward_risk", 0.20)
            max_pos   = bcs_cfg.get("max_concurrent_positions", 3)
            allowed   = bcs_cfg.get("allowed_regimes", ["bear", "neutral"])
            g = [
                (open_bcs < max_pos,
                    f"positions < {max_pos}",                 f"open={open_bcs}"),
                (snap.market_regime in allowed,
                    f"regime in {allowed}",                   f"regime={snap.market_regime}"),
                (snap.options_context.iv_rank >= min_iv,
                    f"IV rank ≥ {min_iv}",                    f"iv_rank={snap.options_context.iv_rank:.0f}"),
                (snap.technicals.rsi >= min_rsi,
                    f"RSI ≥ {min_rsi}  (not in freefall)",    f"rsi={snap.technicals.rsi:.0f}"),
                (snap.technicals.pct_b >= min_pct_b,
                    f"%B ≥ {min_pct_b}",                      f"%B={snap.technicals.pct_b:.2f}"),
            ]
            all_pass = True
            for passed, label, detail in g:
                print(_gate(passed, label, detail))
                if not passed:
                    all_pass = False
            if all_pass:
                strat = next((s for s in self._registry._strategies
                               if s.name == "bear_call_spread"), None)
                reason = strat.last_skip_reason if strat else ""
                if reason:
                    print(f"    ↳  chain evaluated  (need credit≥${min_cred}, R/R≥{min_rr})")
                    print(f"    ↳  ❌  {reason}")
                else:
                    print(f"    ↳  chain evaluated  →  all gates passed — signal generated ✅")

        # ── Bull put spread ───────────────────────────────────────
        bps_cfg = self._config.get("strategies", {}).get("bull_put_spread", {})
        bps_on  = bps_cfg.get("enabled", True)
        _blank()
        print(f"  bull_put_spread{'  [DISABLED]' if not bps_on else ''}:")
        if bps_on:
            min_iv    = bps_cfg.get("min_iv_rank", 35)
            min_rsi   = bps_cfg.get("min_rsi_floor", 35)
            max_rsi   = bps_cfg.get("max_rsi_ceiling", 65)
            min_pct_b = bps_cfg.get("min_pct_b", 0.20)
            min_cred  = bps_cfg.get("min_credit", 0.50)
            min_rr    = bps_cfg.get("min_reward_risk", 0.20)
            max_pos   = bps_cfg.get("max_concurrent_positions", 3)
            allowed   = bps_cfg.get("allowed_regimes", ["bull", "neutral"])
            g = [
                (open_bps < max_pos,
                    f"positions < {max_pos}",                 f"open={open_bps}"),
                (snap.market_regime in allowed,
                    f"regime in {allowed}",                   f"regime={snap.market_regime}"),
                (snap.options_context.iv_rank >= min_iv,
                    f"IV rank ≥ {min_iv}",                    f"iv_rank={snap.options_context.iv_rank:.0f}"),
                (snap.technicals.rsi >= min_rsi,
                    f"RSI ≥ {min_rsi}  (not in freefall)",    f"rsi={snap.technicals.rsi:.0f}"),
                (snap.technicals.rsi <= max_rsi,
                    f"RSI ≤ {max_rsi}  (not overbought)",     f"rsi={snap.technicals.rsi:.0f}"),
                (snap.technicals.pct_b >= min_pct_b,
                    f"%B ≥ {min_pct_b}",                      f"%B={snap.technicals.pct_b:.2f}"),
            ]
            all_pass = True
            for passed, label, detail in g:
                print(_gate(passed, label, detail))
                if not passed:
                    all_pass = False
            if all_pass:
                strat = next((s for s in self._registry._strategies
                               if s.name == "bull_put_spread"), None)
                reason = strat.last_skip_reason if strat else ""
                if reason:
                    print(f"    ↳  chain evaluated  (need credit≥${min_cred}, R/R≥{min_rr})")
                    print(f"    ↳  ❌  {reason}")
                else:
                    print(f"    ↳  chain evaluated  →  all gates passed — signal generated ✅")

    def _print_ranking_table(self, ranked, results) -> None:
        """
        Print a compact ranked table showing all candidates with scores and outcomes.

        Called after process_signals() so we can annotate each row with its
        execution outcome (executed / blocked / skipped).

        When the ranker is disabled, prints a note that FIFO order was used.
        """
        if not ranked:
            return

        ranker_enabled = self._manager._ranker.is_enabled

        _blank()
        if ranker_enabled:
            print(f"  ── RANKING TABLE  ({len(ranked)} candidate(s))")
        else:
            print(f"  ── CANDIDATES  ({len(ranked)})  [ranker disabled — FIFO order]")
        print(f"  {'#':<4}  {'Symbol':<10}  {'Strategy':<20}  {'IVR':>4}  {'Buf%':>5}  {'R/R':>5}  {'Score':>6}  Outcome")
        print(f"  {'─'*78}")

        # Build a lookup from signal identity to TradeResult
        # TradeManager returns results in ranked order, so zip is reliable
        result_map: dict = {}
        for res in results:
            key = (res.signal.symbol, res.signal.strategy_name)
            result_map[key] = res

        for rs in ranked:
            sig  = rs.signal
            key  = (sig.symbol, sig.strategy_name)
            res  = result_map.get(key)

            # Outcome tag
            if res is None:
                outcome = "?"
            elif res.executed:
                outcome = "✅ EXECUTED"
            elif not res.approved:
                short_reason = (res.blocked_reason or "blocked")[:28]
                outcome = f"🚫 {short_reason}"
            else:
                short_reason = (res.blocked_reason or "skipped")[:28]
                outcome = f"⚠️  {short_reason}"

            ivr_str  = f"{sig.iv_rank:.0f}"   if sig.iv_rank   is not None else "—"
            buf_str  = f"{sig.buffer_pct:.1f}" if getattr(sig, "buffer_pct",  None) is not None else "—"
            rr_str   = f"{sig.reward_risk:.2f}" if sig.reward_risk is not None else "—"
            score_str = f"{rs.score:.4f}" if ranker_enabled else "  —   "
            rank_str  = f"#{rs.rank}" if ranker_enabled else f"  {ranked.index(rs)+1}"

            sym   = sig.symbol.replace("US.", "")
            strat = sig.strategy_name[:20]

            print(
                f"  {rank_str:<4}  {sym:<10}  {strat:<20}  {ivr_str:>4}  "
                f"{buf_str:>5}  {rr_str:>5}  {score_str:>6}  {outcome}"
            )

        print(f"  {'─'*78}")
        _blank()

    def _print_trade_result(self, result) -> None:
        """Print one trade result (signal → execution outcome)."""
        try:
            sig      = result.signal
            approved = bool(result.approved)
            executed = bool(result.executed)
        except Exception:
            return
        _blank()
        if not approved:
            print(
                f"  🚫  {sig.strategy_name}  {sig.symbol}  "
                f"BLOCKED by portfolio guard\n"
                f"       Reason: {result.blocked_reason}"
            )
        elif executed:
            try:
                mode_tag   = "PAPER FILL" if self._mode == "paper" else "LIVE ORDER"
                buy_c      = getattr(sig, "buy_contract", None)
                max_loss   = getattr(sig, "max_loss", None)
                print(f"  {'📄' if self._mode == 'paper' else '🔴'}  {mode_tag}  ──────────────────")
                _row("  Strategy:", sig.strategy_name)
                _row("  Symbol:",   sig.symbol)
                _row("  Sell:",     sig.sell_contract)
                if buy_c:
                    _row("  Buy:",  buy_c)
                _row("  Credit:",   f"${sig.net_credit:.2f}  (${sig.net_credit * 100:.0f} per contract)")
                if max_loss:
                    _row("  Max loss:",    f"${max_loss:.0f}")
                    _row("  Breakeven:",   f"${sig.breakeven:.2f}")
                    _row("  Reward/risk:", f"{sig.reward_risk:.2f}")
                _row("  Expiry:",    f"{sig.expiry}  (DTE={sig.dte})")
                _row("  Delta:",     f"{sig.delta:.2f}")
                _row("  IV rank:",   f"{sig.iv_rank:.0f}")
                _row("  Regime:",    sig.regime)
            except Exception as _e:
                print(f"  ✅  Trade executed (display error: {_e})")
        else:
            try:
                print(
                    f"  ⚠️   {sig.strategy_name}  {sig.symbol}  "
                    f"approved but not executed\n"
                    f"       Reason: {result.blocked_reason}"
                )
            except Exception:
                print("  ⚠️  Trade approved but not executed")

    # ── MONITOR JOB ──────────────────────────────────────────────

    def _monitor_job(self, force: bool = False, skip_rescan: bool = False) -> None:
        self._monitor_count += 1
        _blank()
        print(_bar("─"))
        print(f"  MONITOR  #{self._monitor_count}  │  {_ts()}")
        print(_bar("─"))

        # Outside market hours: show positions without fetching live prices
        # (prices are stale after close — fetching adds noise without value)
        now_et   = datetime.now(ET)
        hour_et  = now_et.hour + now_et.minute / 60
        market_open = 9.5 <= hour_et < 16.0

        # Get position summaries (with current price + unrealised P&L)
        summary = self._monitor.get_position_summary() if market_open else self._ledger.get_open_trades()
        mode    = self._mode.upper()

        def _parse_strike(contract_code: str) -> str:
            """Extract strike price from contract code e.g. US.SPY260402C700000 → 700"""
            if not contract_code:
                return "?"
            m = re.search(r'[CP](\d+)$', contract_code)
            if not m:
                return "?"
            val = int(m.group(1)) / 1000
            return f"{val:.0f}" if val == int(val) else f"{val:.1f}"

        if not summary:
            print(f"  No open positions  │  mode={mode}")
        else:
            hdr = f"  {'#':<4} {'Symbol':<10} {'Strategy':<22} {'Strikes':<14} {'Credit':>7} {'Current':>8} {'P&L':>8} {'DTE':>4}  {'Status'}"
            print(hdr)
            print(f"  {'─'*4} {'─'*10} {'─'*22} {'─'*14} {'─'*7} {'─'*8} {'─'*8} {'─'*4}  {'─'*18}")
            for pos in summary:
                credit  = pos.get("net_credit", 0)
                current = pos.get("current_price")
                pnl     = pos.get("unrealised_pnl")
                expiry  = pos.get("expiry", "")
                dte     = max(0, (date.fromisoformat(expiry) - date.today()).days) if expiry else "?"
                exit_sig = pos.get("exit_signal")

                # Parse strikes from contract codes
                sell_strike = _parse_strike(pos.get("sell_contract", ""))
                buy_strike  = _parse_strike(pos.get("buy_contract", ""))
                if pos.get("buy_contract"):
                    strikes = f"{sell_strike}/{buy_strike}"   # spread: short/long
                else:
                    strikes = f"@{sell_strike}"               # single leg (covered call)

                current_str = f"${current:.2f}" if current is not None else "  N/A"
                pnl_str     = f"${pnl:+.0f}"   if pnl     is not None else "   N/A"

                if exit_sig:
                    status = _exit_icon(exit_sig)
                elif current is None:
                    status = "🌙 market closed" if not market_open else "⚠️  price unavailable"
                else:
                    pnl_pct = pos.get("pnl_pct", 0) or 0
                    status  = f"holding  {pnl_pct:.0%} captured"

                print(
                    f"  {pos['id']:<4} {pos['symbol']:<10} "
                    f"{pos['strategy_name']:<22} "
                    f"{strikes:<14} "
                    f"${credit:>6.2f} {current_str:>8} {pnl_str:>8} {str(dte):>4}  {status}"
                )

        # Cache current unrealised P&L for heartbeat display
        if summary and market_open:
            live_pnls = [pos.get("unrealised_pnl") for pos in summary
                         if pos.get("unrealised_pnl") is not None]
            if live_pnls:
                self._last_unrealised_pnl = sum(live_pnls)

        # Persist mark prices for dashboard /positions page
        if market_open and summary:
            try:
                self._write_positions_mark(summary)
            except Exception as _me:
                logger.warning(f"[MONITOR] Could not write positions_mark.json: {_me}")

        # ── LLM Regime update (market hours only — conserves API tokens) ──
        # Runs every 30-min monitor cycle but LLMRegimeBridgePool.maybe_update()
        # only fires the LLM when the update_interval has elapsed (default: daily).
        # Returns in <1ms — background thread spawned when due.
        if market_open and self._llm_pool is not None:
            _all_syms = set(
                self._config.get("universe", {}).get("watchlist", [])
            )
            for _sym in _all_syms:
                try:
                    _ohlcv = self._yfinance.get_daily_ohlcv(_sym, period="2y")
                    self._llm_pool.maybe_update(_sym, _ohlcv)
                except Exception as _le:
                    logger.debug(f"[LLM Regime] {_sym} update skipped: {_le}")
            # Log combined state for all open positions (informational)
            if self._combined is not None:
                _open_syms = {t['symbol'] for t in self._ledger.get_open_trades()}
                for _sym in _open_syms:
                    _state = self._combined.get_state(_sym)
                    _dir   = _state.get('direction', '—')
                    _src   = _state.get('direction_source', '—')
                    _htf   = _state.get('llm_htf_regime') or '—'
                    _ltf   = _state.get('llm_ltf_regime') or '—'
                    _stale = _state.get('llm_stale', True)
                    if not _stale:
                        logger.info(
                            f"[LLM Regime] {_sym}: direction={_dir} "
                            f"(src={_src}) HTF={_htf} LTF={_ltf}"
                        )

        # ── Regime exit mandate check (fires before normal exit rules) ──
        # If any open position's symbol has regime_v2.exit_mandate=True,
        # force-close it immediately per spec Section 5.6.
        mandate_actions = []
        if self._morning_snapshots:
            from src.market.regime_bridge import bridge_instance as _regime_bridge
            if _regime_bridge is not None:
                open_syms = {t['symbol'] for t in self._ledger.get_open_trades()}
                for sym in open_syms:
                    r2 = _regime_bridge.get_regime(sym)
                    if r2 and r2.get('exit_mandate'):
                        logger.warning(
                            f'[REGIME SHIFT] exit_mandate=True for {sym} | '
                            f'consensus={r2.get("consensus_state","?")} | '
                            f'vol={r2.get("volatility_regime","?")} | '
                            f'break={r2.get("signals",{}).get("structural_break","?")}'
                        )
                        closed = self._monitor.close_all_regime_shift(symbol=sym)
                        mandate_actions.extend(closed)
        if mandate_actions:
            _blank()
            print(f'  ── ⚠️  REGIME SHIFT — {len(mandate_actions)} position(s) force-closed:')
            for a in mandate_actions:
                print(
                    f"    🚨 REGIME SHIFT  #{a.get('trade_id','?')}  "
                    f"{a['symbol']}  {a['strategy']}  "
                    f"P&L ${a['pnl']:+.2f}"
                )

        # Run actual exit checks
        actions = self._monitor.run_cycle(force=force)
        if actions:
            _blank()
            print(f"  ── {len(actions)} exit(s) triggered:")
            for a in actions:
                icon = _exit_icon(a.get("reason", ""))
                print(
                    f"    {icon}  #{a.get('trade_id', '?')}  "
                    f"{a['symbol']}  {a['strategy']}  "
                    f"P&L ${a['pnl']:+.2f}"
                )
        else:
            if summary:
                _blank()
                print(f"  ── No exits triggered — all positions held")

        # ── Intraday rescan for new entries ─────────────────────
        if not skip_rescan:
            self._intraday_rescan()

        print(_bar("─"))
        _blank()
        logger.info(
            f"[MONITOR JOB] {len(summary)} open | "
            f"{len(actions) if actions else 0} exits"
        )


    # ── SCAN RESULTS PERSISTENCE (for dashboard /scan page) ──────

    def _build_gate_data(self, snap) -> list:
        """
        Return structured gate data for a snapshot — mirrors _print_gate_analysis
        but produces a list of dicts instead of printing.

        Returns:
            [
              {
                "strategy": "bear_call_spread",
                "enabled":  True,
                "gates": [{"label": "IV rank ≥ 35", "passed": True, "detail": "iv_rank=55"}],
                "result": "signal" | "skip:<reason>" | "disabled"
              },
              ...
            ]
        """
        open_trades = self._ledger.get_open_trades()
        open_bcs = sum(1 for t in open_trades if t["strategy_name"] == "bear_call_spread")
        open_bps = sum(1 for t in open_trades if t["strategy_name"] == "bull_put_spread")
        open_cc  = sum(1 for t in open_trades if t["strategy_name"] == "covered_call")

        strategies_data = []

        # ── Covered call ──────────────────────────────────────────
        cc_cfg = self._config.get("strategies", {}).get("covered_call", {})
        cc_on  = cc_cfg.get("enabled", True)
        if cc_on:
            min_iv  = cc_cfg.get("min_iv_rank", 30)
            max_rsi = cc_cfg.get("max_rsi", 70)
            max_pos = cc_cfg.get("max_concurrent_positions", 2)
            gates = [
                {"label": f"shares ≥ 100",        "passed": snap.shares_held >= 100,                       "detail": f"held={snap.shares_held}"},
                {"label": f"positions < {max_pos}","passed": open_cc < max_pos,                             "detail": f"open={open_cc}"},
                {"label": "regime ≠ high_vol",     "passed": snap.market_regime != "high_vol",              "detail": f"regime={snap.market_regime}"},
                {"label": f"IV rank ≥ {min_iv}",   "passed": snap.options_context.iv_rank >= min_iv,        "detail": f"iv_rank={snap.options_context.iv_rank:.0f}"},
                {"label": f"RSI ≤ {max_rsi}",      "passed": snap.technicals.rsi <= max_rsi,               "detail": f"rsi={snap.technicals.rsi:.0f}"},
            ]
            all_pass = all(g["passed"] for g in gates)
            if all_pass:
                strat  = next((s for s in self._registry._strategies if s.name == "covered_call"), None)
                reason = strat.last_skip_reason if strat else ""
                result = f"skip:{reason}" if reason else "signal"
            else:
                result = f"skip:{next(g['label'] for g in gates if not g['passed'])} failed"
        else:
            gates  = []
            result = "disabled"
        strategies_data.append({"strategy": "covered_call",     "enabled": cc_on, "gates": gates, "result": result})

        # ── Bear call spread ──────────────────────────────────────
        bcs_cfg = self._config.get("strategies", {}).get("bear_call_spread", {})
        bcs_on  = bcs_cfg.get("enabled", True)
        if bcs_on:
            min_iv    = bcs_cfg.get("min_iv_rank", 35)
            min_rsi   = bcs_cfg.get("min_rsi_for_spread", 45)
            min_pct_b = bcs_cfg.get("min_pct_b", 0.40)
            max_pos   = bcs_cfg.get("max_concurrent_positions", 3)
            allowed   = bcs_cfg.get("allowed_regimes", ["bear", "neutral"])
            gates = [
                {"label": f"positions < {max_pos}",      "passed": open_bcs < max_pos,                        "detail": f"open={open_bcs}"},
                {"label": f"regime in {allowed}",         "passed": snap.market_regime in allowed,             "detail": f"regime={snap.market_regime}"},
                {"label": f"IV rank ≥ {min_iv}",          "passed": snap.options_context.iv_rank >= min_iv,    "detail": f"iv_rank={snap.options_context.iv_rank:.0f}"},
                {"label": f"RSI ≥ {min_rsi}",             "passed": snap.technicals.rsi >= min_rsi,            "detail": f"rsi={snap.technicals.rsi:.0f}"},
                {"label": f"%B ≥ {min_pct_b}",            "passed": snap.technicals.pct_b >= min_pct_b,        "detail": f"%B={snap.technicals.pct_b:.2f}"},
            ]
            all_pass = all(g["passed"] for g in gates)
            if all_pass:
                strat  = next((s for s in self._registry._strategies if s.name == "bear_call_spread"), None)
                reason = strat.last_skip_reason if strat else ""
                result = f"skip:{reason}" if reason else "signal"
            else:
                result = f"skip:{next(g['label'] for g in gates if not g['passed'])} failed"
        else:
            gates  = []
            result = "disabled"
        strategies_data.append({"strategy": "bear_call_spread", "enabled": bcs_on, "gates": gates, "result": result})

        # ── Bull put spread ───────────────────────────────────────
        bps_cfg = self._config.get("strategies", {}).get("bull_put_spread", {})
        bps_on  = bps_cfg.get("enabled", True)
        if bps_on:
            min_iv    = bps_cfg.get("min_iv_rank", 35)
            min_rsi   = bps_cfg.get("min_rsi_floor", 35)
            max_rsi   = bps_cfg.get("max_rsi_ceiling", 65)
            min_pct_b = bps_cfg.get("min_pct_b", 0.20)
            max_pos   = bps_cfg.get("max_concurrent_positions", 3)
            allowed   = bps_cfg.get("allowed_regimes", ["bull", "neutral"])
            gates = [
                {"label": f"positions < {max_pos}",      "passed": open_bps < max_pos,                        "detail": f"open={open_bps}"},
                {"label": f"regime in {allowed}",         "passed": snap.market_regime in allowed,             "detail": f"regime={snap.market_regime}"},
                {"label": f"IV rank ≥ {min_iv}",          "passed": snap.options_context.iv_rank >= min_iv,    "detail": f"iv_rank={snap.options_context.iv_rank:.0f}"},
                {"label": f"RSI ≥ {min_rsi}",             "passed": snap.technicals.rsi >= min_rsi,            "detail": f"rsi={snap.technicals.rsi:.0f}"},
                {"label": f"RSI ≤ {max_rsi}",             "passed": snap.technicals.rsi <= max_rsi,            "detail": f"rsi={snap.technicals.rsi:.0f}"},
                {"label": f"%B ≥ {min_pct_b}",            "passed": snap.technicals.pct_b >= min_pct_b,        "detail": f"%B={snap.technicals.pct_b:.2f}"},
            ]
            all_pass = all(g["passed"] for g in gates)
            if all_pass:
                strat  = next((s for s in self._registry._strategies if s.name == "bull_put_spread"), None)
                reason = strat.last_skip_reason if strat else ""
                result = f"skip:{reason}" if reason else "signal"
            else:
                result = f"skip:{next(g['label'] for g in gates if not g['passed'])} failed"
        else:
            gates  = []
            result = "disabled"
        strategies_data.append({"strategy": "bull_put_spread",  "enabled": bps_on, "gates": gates, "result": result})

        return strategies_data

    def _build_scan_data(self, snapshots, signals, ranked, results, elapsed: float) -> dict:
        """
        Assemble a JSON-serialisable dict from the completed scan cycle.
        Written to data/scan_results.json after every _scan_job() run.
        """
        ET = ZoneInfo("America/New_York")
        now_et = datetime.now(ET)

        # Per-symbol market data
        symbols_out = []
        for snap in snapshots:
            strategies = []
            try:
                strategies = self._build_gate_data(snap)
            except Exception:
                pass

            symbols_out.append({
                "symbol":       snap.symbol,
                "spot_price":   round(snap.spot_price, 2),
                "regime":       snap.market_regime,
                "vix":          round(snap.vix, 2),
                "rsi":          round(snap.technicals.rsi, 1),
                "pct_b":        round(snap.technicals.pct_b, 3),
                "macd":         round(snap.technicals.macd, 3),
                "iv_rank":      round(snap.options_context.iv_rank, 1),
                "shares_held":  snap.shares_held,
                "next_earnings_days": snap.days_to_earnings,
                "expiries_available": len(snap.options_context.available_expiries or []),
                "strategies":   strategies,
            })

        # Ranked candidates
        # Build outcome lookup from TradeResult list: signal → outcome string
        outcome_map: dict = {}
        executed_ids: list = []
        for r in results:
            key = id(r.signal)
            if r.executed:
                outcome_map[key] = "executed"
                if r.trade_id:
                    executed_ids.append(r.trade_id)
            elif r.approved:
                outcome_map[key] = "approved_not_filled"
            else:
                outcome_map[key] = f"blocked:{r.reject_reason or 'guard'}"

        candidates_out = []
        for rs in ranked:
            sig = rs.signal
            candidates_out.append({
                "rank":         rs.rank,
                "symbol":       sig.symbol,
                "strategy":     sig.strategy_name,
                "iv_rank":      round(sig.iv_rank, 1),
                "buffer_pct":   round(sig.buffer_pct, 2) if sig.buffer_pct else None,
                "reward_risk":  round(sig.reward_risk, 3) if sig.reward_risk else None,
                "score":        round(rs.score, 4) if hasattr(rs, "score") else None,
                "net_credit":   round(sig.net_credit, 4),
                "expiry":       sig.expiry,
                "dte":          sig.dte,
                "outcome":      outcome_map.get(id(sig), "skipped"),
            })

        return {
            "scan_timestamp":    now_et.isoformat(),
            "scan_type":         "morning",
            "scan_number":       self._scan_count,
            "elapsed_seconds":   round(elapsed, 1),
            "symbols_scanned":   len(snapshots),
            "signals_found":     len(signals),
            "signals_executed":  len(executed_ids),
            "executed_trade_ids": executed_ids,
            "symbols":           symbols_out,
            "candidates":        candidates_out,
        }

    def _write_scan_results(self, data: dict) -> None:
        """Write scan_results.json to the data/ directory (same folder as the ledger DB)."""
        db_path = self._config.get("paper_ledger", {}).get("db_path", "data/paper_trades.db")
        out_path = Path(db_path).parent / "scan_results.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        logger.debug(f"[SCAN JOB] scan_results.json written → {out_path}")

    def _write_positions_mark(self, summary: list) -> None:
        """Persist live mark prices and P&L to positions_mark.json for the dashboard.

        Called by _monitor_job every ~5 min during market hours.
        Dashboard /positions reads this to populate the Current and P&L columns.
        Each mark entry: id, symbol, current_price, unrealised_pnl, pnl_pct, exit_signal, as_of.
        """
        ET = ZoneInfo("America/New_York")
        now_str = datetime.now(ET).isoformat()
        marks = [
            {
                "id":             pos.get("id"),
                "symbol":         pos.get("symbol"),
                "current_price":  pos.get("current_price"),
                "unrealised_pnl": pos.get("unrealised_pnl"),
                "pnl_pct":        pos.get("pnl_pct"),
                "exit_signal":    pos.get("exit_signal"),
                "as_of":          now_str,
            }
            for pos in summary
        ]
        db_path  = self._config.get("paper_ledger", {}).get("db_path", "data/paper_trades.db")
        out_path = Path(db_path).parent / "positions_mark.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as fh:
            json.dump({"updated_at": now_str, "marks": marks}, fh, indent=2, default=str)
        logger.debug(f"[MONITOR] positions_mark.json written -> {out_path}")

    # ── INTRADAY RESCAN ──────────────────────────────────────────

    def _intraday_rescan(self) -> None:
        """
        Re-evaluate entry criteria every 30 min using live option pricing.

        Daily-bar indicators (RSI, MACD, %B, regime) are reused from the
        morning scan — they won't change until tomorrow's bar closes.

        What IS refreshed:
          - Spot price (yfinance fast_info)
          - VIX (yfinance)
          - ATM IV and IV Rank (live MooMoo snapshot)
          - Open position count
          - Option chain pricing, delta, credit, R/R

        Only runs during market hours (09:35–15:59 ET) and only when
        morning snapshots are available from today's scan job.
        """
        # Skip outside market hours
        now_et = datetime.now(ET)
        hour   = now_et.hour + now_et.minute / 60
        if not (9.6 <= hour < 16.0):   # 09:35 – 16:00 ET
            return

        # Skip if morning scan hasn't run yet today
        if not self._morning_snapshots:
            return

        watchlist = self._config.get("universe", {}).get("watchlist", [])
        signals   = []

        for symbol in watchlist:
            morning = self._morning_snapshots.get(symbol)
            if morning is None:
                continue
            try:
                snap = self._scanner.scan_symbol_intraday(symbol, morning)
                new_sigs = self._registry.evaluate(snap)
                if new_sigs:
                    signals.extend(new_sigs)
            except Exception as e:
                logger.warning(f"[INTRADAY] {symbol}: rescan failed — {e}")
                continue

        if not signals:
            return   # silent — no output when nothing fires

        # ── Display and execute any new signals ───────────────────
        _blank()
        print(f"  ── INTRADAY RESCAN  │  {_now_et()}  │  {len(signals)} signal(s)")
        results = self._manager.process_signals(signals)
        for res in results:
            self._print_trade_result(res)
        # Only notify for signals approved but NOT auto-executed (need manual action)
        # Paper fills (r.executed=True) are auto-recorded — no bell needed
        notifiable = [r.signal for r in results if r.approved and not r.executed]
        if notifiable:
            self._notifier.notify(notifiable)
        logger.info(
            f"[INTRADAY] {len(signals)} signal(s) | "
            f"{sum(1 for r in results if r.executed)} executed"
        )

        # -- Persist intraday results for dashboard /scan page --
        try:
            ranked         = self._manager._ranker.rank(signals)
            intraday_snaps = list(self._morning_snapshots.values())
            scan_data      = self._build_scan_data(
                intraday_snaps, signals, ranked, results, 0.0
            )
            scan_data["scan_type"] = "intraday"
            self._write_scan_results(scan_data)
        except Exception as _ie:
            logger.warning(f"[INTRADAY] Could not write scan_results.json: {_ie}")

    # ── IV JOB ───────────────────────────────────────────────────

    def _iv_job(self) -> None:
        _blank()
        print(_bar("─"))
        print(f"  IV COLLECTION  │  {_ts()}")
        print(_bar("─"))
        logger.info("[IV JOB] Collecting end-of-day IV...")

        watchlist = self._config.get("universe", {}).get("watchlist", [])
        for symbol in watchlist:
            try:
                expiries = self._moomoo.get_option_expiries(symbol)
                if not expiries:
                    print(f"  ⚠️  {symbol}: no expiries returned")
                    continue
                # Skip any already-expired expiries (MooMoo may return
                # yesterday's weekly at position [0] after close/weekend)
                today_str = date.today().isoformat()
                future_expiries = [e for e in expiries if e > today_str]
                if not future_expiries:
                    print(f"  ⚠️  {symbol}: no future expiries available")
                    continue
                chain = self._moomoo.get_option_chain(symbol, future_expiries[0], "CALL")
                if chain is None or len(chain) == 0:
                    print(f"  ⚠️  {symbol}: empty chain")
                    continue
                spot  = self._yfinance.get_current_price(symbol)
                # Fetch snapshot for ATM strike (±5 strikes around spot)
                atm_calls = chain[
                    (chain["option_type"] == "CALL") &
                    (chain["strike_price"] >= spot * 0.97) &
                    (chain["strike_price"] <= spot * 1.03)
                ]["code"].tolist()[:10]
                if not atm_calls:
                    # Fallback: just grab closest strikes
                    atm_calls = chain.sort_values(
                        "strike_price", key=lambda s: abs(s - spot)
                    )["code"].tolist()[:10]
                snap  = self._moomoo.get_option_snapshot(atm_calls)
                if snap is None or len(snap) == 0:
                    print(f"  ⚠️  {symbol}: empty snapshot")
                    continue
                # Compute ATM IV — average option_iv across ATM options with valid data
                if "option_iv" not in snap.columns:
                    print(f"  ⚠️  {symbol}: option_iv column missing from snapshot")
                    continue
                iv_vals = snap["option_iv"].dropna()
                iv_vals = iv_vals[iv_vals > 0]
                if len(iv_vals) == 0:
                    print(f"  ⚠️  {symbol}: IV=0 — Greeks not settled (market may be closed)")
                    continue
                atm_iv = float(iv_vals.mean())   # MooMoo option_iv already returns percentage (e.g. 35.0 = 35%)
                if atm_iv and atm_iv > 0:
                    self._iv_calculator.store_daily_iv(symbol, atm_iv)
                    days = self._iv_calculator.get_days_stored(symbol) if hasattr(self._iv_calculator, "get_days_stored") else "?"
                    print(f"  ✅  {symbol}  ATM IV={atm_iv:.1f}%  │  {days} days stored")
                    logger.info(f"[IV JOB] {symbol}: ATM IV={atm_iv:.1f}%")
                else:
                    print(f"  ⚠️  {symbol}: IV=0 — market likely just closed, Greeks not yet settled")
            except Exception as e:
                print(f"  ❌  {symbol}: {e}")
                logger.error(f"[IV JOB] Failed for {symbol}: {e}")

        print(_bar("─"))
        _blank()

    # ── REPORT JOB ───────────────────────────────────────────────

    def _report_job(self) -> None:
        _blank()
        print(_bar("═"))
        print(f"  WEEKLY VALIDATION REPORT  │  {_ts()}")
        print(_bar("═"))
        logger.info("[REPORT JOB] Generating weekly validation report...")

        report = self._reporter.generate(save_to_file=True)
        gates  = report.get("gates", {})

        for gate_name, gate in gates.items():
            passed  = gate.get("passed", False)
            detail  = gate.get("detail", "")
            print(_gate(passed, gate_name.replace("_", " ").title(), detail))

        _blank()
        if report["go_live"]:
            print(f"  🟢  ALL GATES PASSED — ready to go live")
            print(f"      Change config.yaml: mode: live  (after review)")
            logger.warning("⚠️  VALIDATION GATES ALL PASSED — review report before going live.")
        else:
            failed = [g for g, v in gates.items() if not v.get("passed")]
            print(f"  🔴  NOT READY — failing: {', '.join(failed)}")
            logger.info(f"[REPORT JOB] Not ready. Failing: {failed}")

        print(_bar("═"))
        _blank()

    # ── Safety wrapper ────────────────────────────────────────────

    def _safe_run(self, job_fn, job_name: str, **kwargs) -> None:
        try:
            job_fn(**kwargs)
        except Exception as e:
            print(f"\n  ❌  Job '{job_name}' failed: {e}")
            logger.error(
                f"Job '{job_name}' raised an unhandled exception: {e}",
                exc_info=True
            )

    # ── Shutdown ──────────────────────────────────────────────────

    def _handle_shutdown(self, signum, frame) -> None:
        logger.info(f"Shutdown signal received ({signum}) — stopping...")
        self.stop()
        sys.exit(0)
