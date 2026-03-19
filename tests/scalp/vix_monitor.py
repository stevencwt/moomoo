"""
src/scalp/signals/vix_monitor.py
=================================
VIX slope and VVIX spike monitor for the GEX Reversion Scalper.

Polls VIX and VVIX every N seconds (default 300s = 5 minutes).
Returns a structured dict used by scalp_gate.py to block/allow entries.

Outputs:
    vix          : float  — current VIX level
    vvix         : float  — current VVIX level
    hard_block   : bool   — True if VIX >= hard_block threshold (default 25.0)
    vvix_spike   : bool   — True if VVIX >= 1.05× its recent rolling average
    vix_slope    : str    — "rising" | "falling" | "flat"
    ok_for_long  : bool   — True if VIX < hard_block AND slope is falling or flat
                             AND no VVIX spike
    ok_for_short : bool   — True if VIX < hard_block (short-friendly even on rising VIX)
                             AND no VVIX spike
    readings     : int    — number of readings collected so far
    timestamp    : float  — time.time() of last poll

Design:
    - Uses yfinance for VIX and VVIX — same source as the existing options bot
    - Thread-safe: can be polled from any thread after start()
    - Runs as a background daemon thread; poll() returns latest cached state
    - Graceful degradation: if yfinance fails, returns last known state with
      stale=True flag rather than crashing

Usage:
    from src.scalp.signals.vix_monitor import VIXMonitor

    monitor = VIXMonitor(config)
    monitor.start()                    # begins background polling

    state = monitor.poll()             # non-blocking, returns latest state
    if state["hard_block"]:
        return  # skip all entries
    if state["vvix_spike"]:
        return  # pause long entries
    if state["ok_for_long"] and direction == "LONG":
        proceed_with_entry()

    monitor.stop()                     # clean shutdown
"""

import logging
import threading
import time
from collections import deque
from typing import Dict, Optional

logger = logging.getLogger("options_bot.scalp.signals.vix_monitor")


# ── Defaults (overridden by config) ──────────────────────────────────────────
DEFAULT_HARD_BLOCK        = 25.0    # VIX above this → no trades at all
DEFAULT_POLL_INTERVAL_S   = 300     # 5 minutes between polls
DEFAULT_SLOPE_WINDOW      = 3       # readings used for slope calculation
DEFAULT_VVIX_SPIKE_PCT    = 0.05    # 5% above average = spike
DEFAULT_VVIX_AVG_WINDOW   = 12      # readings for VVIX rolling average (~1 hour at 5min)


class VIXMonitor:
    """
    Polls VIX and VVIX on a background thread.
    Thread-safe — poll() can be called from any thread.

    Args:
        config: bot config dict. Reads from config["scalp"]["vix"] if present.
                Falls back to defaults for any missing key.
    """

    def __init__(self, config: dict):
        scalp_cfg  = config.get("scalp", {})
        vix_cfg    = scalp_cfg.get("vix", {})

        self._hard_block_level  = vix_cfg.get("hard_block",      DEFAULT_HARD_BLOCK)
        self._poll_interval     = vix_cfg.get("poll_interval_s", DEFAULT_POLL_INTERVAL_S)
        self._slope_window      = vix_cfg.get("slope_window",    DEFAULT_SLOPE_WINDOW)
        self._vvix_spike_pct    = vix_cfg.get("vvix_spike_pct",  DEFAULT_VVIX_SPIKE_PCT)
        self._vvix_avg_window   = vix_cfg.get("vvix_avg_window", DEFAULT_VVIX_AVG_WINDOW)

        # Rolling history for slope + VVIX average
        self._vix_history : deque = deque(maxlen=max(self._slope_window + 1, 5))
        self._vvix_history: deque = deque(maxlen=max(self._vvix_avg_window, 12))

        # Latest computed state — updated on each poll, read by poll()
        self._state: Dict = self._empty_state()
        self._lock  = threading.Lock()

        self._stop_flag  = threading.Event()
        self._thread: Optional[threading.Thread] = None

        logger.info(
            f"VIXMonitor initialised | hard_block={self._hard_block_level} | "
            f"poll={self._poll_interval}s | vvix_spike={self._vvix_spike_pct*100:.0f}% | "
            f"slope_window={self._slope_window}"
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """
        Start background polling thread.
        First poll runs immediately; subsequent polls every poll_interval_s.
        """
        if self._thread and self._thread.is_alive():
            logger.debug("VIXMonitor already running")
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="vix-monitor",
            daemon=True,
        )
        self._thread.start()
        logger.info("VIXMonitor started")

    def stop(self) -> None:
        """Stop background polling cleanly."""
        self._stop_flag.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info("VIXMonitor stopped")

    def poll(self) -> Dict:
        """
        Return latest VIX/VVIX state. Non-blocking, thread-safe.

        Returns dict with keys:
            vix          : float
            vvix         : float
            hard_block   : bool   — True if VIX >= hard_block threshold
            vvix_spike   : bool   — True if VVIX >= (1 + spike_pct) × rolling avg
            vix_slope    : str    — "rising" | "falling" | "flat"
            ok_for_long  : bool   — VIX < hard_block AND slope not rising AND no spike
            ok_for_short : bool   — VIX < hard_block AND no spike (rising VIX is ok for shorts)
            readings     : int    — number of successful polls so far
            stale        : bool   — True if last poll failed (showing cached values)
            timestamp    : float  — time.time() of last successful poll
        """
        with self._lock:
            return dict(self._state)

    def force_poll(self) -> Dict:
        """
        Force an immediate poll (blocking). Returns updated state.
        Useful for testing and for the first call at scheduler startup.
        """
        state = self._do_poll()
        with self._lock:
            self._state = state
        return dict(state)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        """Background thread: poll immediately, then every poll_interval_s."""
        logger.debug("VIXMonitor poll loop started")
        while not self._stop_flag.is_set():
            state = self._do_poll()
            with self._lock:
                self._state = state
            logger.debug(
                f"VIX={state['vix']:.2f} slope={state['vix_slope']} "
                f"VVIX={state['vvix']:.2f} spike={state['vvix_spike']} "
                f"hard_block={state['hard_block']}"
            )
            # Sleep in short chunks so stop() responds quickly
            for _ in range(self._poll_interval * 2):
                if self._stop_flag.is_set():
                    break
                time.sleep(0.5)
        logger.debug("VIXMonitor poll loop stopped")

    def _do_poll(self) -> Dict:
        """Fetch VIX + VVIX from yfinance, update history, compute state."""
        try:
            vix_val, vvix_val = self._fetch_vix_vvix()
        except Exception as e:
            logger.warning(f"VIXMonitor fetch error: {e}")
            # Return stale state with stale=True flag
            with self._lock:
                stale = dict(self._state)
                stale["stale"] = True
            return stale

        # Compute VVIX average from history BEFORE appending new value
        # This prevents the spike value from diluting its own average
        vvix_avg = (
            sum(self._vvix_history) / len(self._vvix_history)
            if len(self._vvix_history) >= 3
            else None  # not enough history yet — no spike possible
        )
        vvix_spike = (
            vvix_val >= vvix_avg * (1 + self._vvix_spike_pct)
            if vvix_avg is not None
            else False
        )

        # Now append new values to history
        self._vix_history.append(vix_val)
        self._vvix_history.append(vvix_val)

        # Compute slope from last slope_window readings
        vix_slope = self._compute_slope(list(self._vix_history), self._slope_window)

        hard_block   = vix_val >= self._hard_block_level
        ok_for_long  = (
            not hard_block
            and not vvix_spike
            and vix_slope in ("falling", "flat")
        )
        ok_for_short = not hard_block and not vvix_spike

        with self._lock:
            readings = self._state.get("readings", 0) + 1

        return {
            "vix":          round(vix_val, 2),
            "vvix":         round(vvix_val, 2),
            "vvix_avg":     round(vvix_avg if vvix_avg is not None else vvix_val, 2),
            "hard_block":   hard_block,
            "vvix_spike":   vvix_spike,
            "vix_slope":    vix_slope,
            "ok_for_long":  ok_for_long,
            "ok_for_short": ok_for_short,
            "readings":     readings,
            "stale":        False,
            "timestamp":    time.time(),
        }

    def _fetch_vix_vvix(self) -> tuple:
        """
        Fetch latest VIX and VVIX from yfinance.
        Uses 1-day 5m bars and takes the last closing price.
        """
        import yfinance as yf

        # Fetch both in one call using a Tickers object
        tickers = yf.Tickers("^VIX ^VVIX")
        vix_data  = tickers.tickers["^VIX"].history( period="1d", interval="5m")
        vvix_data = tickers.tickers["^VVIX"].history(period="1d", interval="5m")

        if vix_data.empty:
            raise ValueError("VIX data returned empty from yfinance")
        if vvix_data.empty:
            # VVIX sometimes has gaps — fall back to daily if 5m is empty
            vvix_data = tickers.tickers["^VVIX"].history(period="5d", interval="1d")
        if vvix_data.empty:
            raise ValueError("VVIX data returned empty from yfinance")

        vix_val  = float(vix_data["Close"].iloc[-1])
        vvix_val = float(vvix_data["Close"].iloc[-1])

        return vix_val, vvix_val

    @staticmethod
    def _compute_slope(history: list, window: int) -> str:
        """
        Compute slope direction from the last `window` readings.

        Returns:
            "rising"  — latest reading is higher than window-ago reading
            "falling" — latest reading is lower than window-ago reading
            "flat"    — difference is within 2% of starting value

        If fewer than 2 readings available, returns "flat" (not enough data).
        """
        if len(history) < 2:
            return "flat"

        # Use at most `window` readings, but at least 2
        n = min(window, len(history))
        recent = history[-n:]
        start  = recent[0]
        end    = recent[-1]

        if start == 0:
            return "flat"

        change_pct = (end - start) / start

        if change_pct > 0.02:    # > 2% increase = rising
            return "rising"
        elif change_pct < -0.02: # > 2% decrease = falling
            return "falling"
        else:
            return "flat"

    @staticmethod
    def _empty_state() -> Dict:
        """Initial state before any polls complete."""
        return {
            "vix":          0.0,
            "vvix":         0.0,
            "vvix_avg":     0.0,
            "hard_block":   True,    # conservative default until first poll
            "vvix_spike":   False,
            "vix_slope":    "flat",
            "ok_for_long":  False,   # conservative default
            "ok_for_short": False,
            "readings":     0,
            "stale":        True,    # True until first successful poll
            "timestamp":    0.0,
        }
