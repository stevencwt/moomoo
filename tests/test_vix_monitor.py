#!/usr/bin/env python3
"""
tests/scalp/test_vix_monitor.py
================================
Test suite for VIXMonitor.

Tests:
    Group A — Unit tests (no network, no IBKR required)
    Group B — Live data tests (requires internet, runs during any hours)
    Group C — Behaviour tests (logic validation with synthetic data)

Usage:
    python3 tests/scalp/test_vix_monitor.py              # all groups
    python3 tests/scalp/test_vix_monitor.py --unit-only  # Group A only
    python3 tests/scalp/test_vix_monitor.py --live       # Groups A + B
"""

import argparse
import sys
import time
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock
from collections import deque

# Add project root to path
ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.scalp.signals.vix_monitor import VIXMonitor


# ── Helpers ───────────────────────────────────────────────────────────────────

results = []

def check(label: str, passed: bool, detail: str = "") -> bool:
    icon   = "✅" if passed else "❌"
    suffix = f"  [{detail}]" if detail else ""
    print(f"  {icon}  {label}{suffix}")
    results.append((label, passed))
    return passed

def section(title: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")

def make_config(
    hard_block=25.0,
    poll_interval_s=1,    # 1 second for tests
    slope_window=3,
    vvix_spike_pct=0.05,
    vvix_avg_window=5,
) -> dict:
    return {
        "scalp": {
            "vix": {
                "hard_block":       hard_block,
                "poll_interval_s":  poll_interval_s,
                "slope_window":     slope_window,
                "vvix_spike_pct":   vvix_spike_pct,
                "vvix_avg_window":  vvix_avg_window,
            }
        }
    }


# ── Group A: Unit tests ───────────────────────────────────────────────────────

def test_group_a():
    section("GROUP A — Unit tests (no network)")

    # A1: Constructor reads config correctly
    config  = make_config(hard_block=20.0, poll_interval_s=60, slope_window=5)
    monitor = VIXMonitor(config)
    check("A1: hard_block reads from config",
          monitor._hard_block_level == 20.0, f"={monitor._hard_block_level}")
    check("A1: poll_interval reads from config",
          monitor._poll_interval == 60, f"={monitor._poll_interval}")
    check("A1: slope_window reads from config",
          monitor._slope_window == 5, f"={monitor._slope_window}")

    # A2: Default config fallback
    monitor2 = VIXMonitor({})
    check("A2: defaults applied when no scalp config",
          monitor2._hard_block_level == 25.0, f"={monitor2._hard_block_level}")

    # A3: Initial state is conservative
    state = monitor.poll()
    check("A3: initial hard_block=True (conservative default)", state["hard_block"])
    check("A3: initial ok_for_long=False (conservative default)", not state["ok_for_long"])
    check("A3: initial stale=True", state["stale"])
    check("A3: initial readings=0", state["readings"] == 0, f"={state['readings']}")

    # A4: Slope computation
    check("A4: slope rising — end > start by >2%",
          VIXMonitor._compute_slope([10.0, 10.5, 11.0, 12.0], 3) == "rising",
          VIXMonitor._compute_slope([10.0, 10.5, 11.0, 12.0], 3))
    check("A4: slope falling — end < start by >2%",
          VIXMonitor._compute_slope([20.0, 19.0, 18.0, 17.0], 3) == "falling",
          VIXMonitor._compute_slope([20.0, 19.0, 18.0, 17.0], 3))
    check("A4: slope flat — change within 2%",
          VIXMonitor._compute_slope([15.0, 15.1, 15.2, 15.1], 3) == "flat",
          VIXMonitor._compute_slope([15.0, 15.1, 15.2, 15.1], 3))
    check("A4: slope flat with only 1 reading",
          VIXMonitor._compute_slope([15.0], 3) == "flat")
    check("A4: slope flat with 0 start value (edge case)",
          VIXMonitor._compute_slope([0.0, 5.0], 2) == "flat")

    # A5: Gate logic with synthetic _do_poll via mock
    monitor3 = VIXMonitor(make_config(hard_block=25.0, vvix_spike_pct=0.05))

    # Inject synthetic history
    monitor3._vix_history  = deque([22.0, 21.5, 21.0], maxlen=5)
    monitor3._vvix_history = deque([85.0, 86.0, 87.0, 86.5, 87.0], maxlen=5)

    with patch.object(monitor3, "_fetch_vix_vvix", return_value=(21.0, 87.0)):
        state = monitor3._do_poll()

    check("A5: VIX=21 < 25 → hard_block=False", not state["hard_block"],
          f"hard_block={state['hard_block']}")
    check("A5: falling VIX slope detected", state["vix_slope"] == "falling",
          f"slope={state['vix_slope']}")
    check("A5: no VVIX spike (stable readings)", not state["vvix_spike"],
          f"spike={state['vvix_spike']}, vvix={state['vvix']}, avg={state['vvix_avg']}")
    check("A5: ok_for_long=True (VIX<25, falling, no spike)", state["ok_for_long"])
    check("A5: ok_for_short=True (VIX<25, no spike)", state["ok_for_short"])

    # A6: Hard block triggers above threshold
    with patch.object(monitor3, "_fetch_vix_vvix", return_value=(26.5, 87.0)):
        monitor3._vix_history = deque([24.0, 25.0, 26.5], maxlen=5)
        state_block = monitor3._do_poll()
    check("A6: VIX=26.5 → hard_block=True", state_block["hard_block"],
          f"vix={state_block['vix']}")
    check("A6: ok_for_long=False when hard_block", not state_block["ok_for_long"])
    check("A6: ok_for_short=False when hard_block", not state_block["ok_for_short"])

    # A7: VVIX spike detection
    monitor4 = VIXMonitor(make_config(vvix_spike_pct=0.05, vvix_avg_window=5))
    monitor4._vix_history  = deque([20.0, 20.0, 20.0], maxlen=5)
    monitor4._vvix_history = deque([80.0, 80.0, 80.0, 80.0, 80.0], maxlen=5)
    # 80 × 1.05 = 84 → value of 85 should spike
    with patch.object(monitor4, "_fetch_vix_vvix", return_value=(20.0, 85.0)):
        state_spike = monitor4._do_poll()
    check("A7: VVIX=85 vs avg=80 → spike=True (>5%)", state_spike["vvix_spike"],
          f"vvix={state_spike['vvix']} avg={state_spike['vvix_avg']}")
    check("A7: ok_for_long=False when VVIX spike", not state_spike["ok_for_long"])

    # A8: No VVIX spike with marginal value
    monitor5 = VIXMonitor(make_config(vvix_spike_pct=0.05))
    monitor5._vix_history  = deque([20.0, 20.0], maxlen=5)
    monitor5._vvix_history = deque([80.0, 80.0, 80.0, 80.0, 80.0], maxlen=5)
    # 80 × 1.05 = 84 → value of 83 should NOT spike
    with patch.object(monitor5, "_fetch_vix_vvix", return_value=(20.0, 83.0)):
        state_no_spike = monitor5._do_poll()
    check("A8: VVIX=83 vs avg=80 → spike=False (<5%)", not state_no_spike["vvix_spike"],
          f"vvix={state_no_spike['vvix']} avg={state_no_spike['vvix_avg']}")

    # A9: Rising VIX blocks longs but not shorts
    monitor6 = VIXMonitor(make_config(hard_block=25.0))
    monitor6._vix_history  = deque([18.0, 20.0, 22.0], maxlen=5)
    monitor6._vvix_history = deque([80.0, 80.0, 80.0], maxlen=5)
    with patch.object(monitor6, "_fetch_vix_vvix", return_value=(22.0, 80.0)):
        state_rising = monitor6._do_poll()
    check("A9: rising VIX → ok_for_long=False", not state_rising["ok_for_long"],
          f"slope={state_rising['vix_slope']}")
    check("A9: rising VIX → ok_for_short=True (short-friendly)", state_rising["ok_for_short"])

    # A10: Stale state on fetch failure
    monitor7 = VIXMonitor(make_config())
    monitor7._state = {
        "vix": 18.0, "vvix": 75.0, "vvix_avg": 74.0,
        "hard_block": False, "vvix_spike": False,
        "vix_slope": "flat", "ok_for_long": True, "ok_for_short": True,
        "readings": 5, "stale": False, "timestamp": time.time() - 300,
    }
    with patch.object(monitor7, "_fetch_vix_vvix", side_effect=Exception("network error")):
        stale_state = monitor7._do_poll()
    check("A10: fetch error → stale=True", stale_state["stale"],
          f"stale={stale_state['stale']}")
    check("A10: stale state preserves last known VIX", stale_state["vix"] == 18.0,
          f"vix={stale_state['vix']}")


# ── Group B: Live data tests ──────────────────────────────────────────────────

def test_group_b():
    section("GROUP B — Live data tests (requires internet)")
    print("  Testing actual VIX + VVIX data from yfinance...\n")

    config  = make_config(poll_interval_s=999)   # disable auto-polling
    monitor = VIXMonitor(config)

    # B1: force_poll returns real data
    try:
        state = monitor.force_poll()
        check("B1: force_poll() succeeds", not state["stale"],
              f"stale={state['stale']}")
        check("B1: VIX > 0", state["vix"] > 0,
              f"VIX={state['vix']:.2f}")
        check("B1: VIX in plausible range (5–80)", 5.0 <= state["vix"] <= 80.0,
              f"VIX={state['vix']:.2f}")
        check("B1: VVIX > 0", state["vvix"] > 0,
              f"VVIX={state['vvix']:.2f}")
        check("B1: VVIX in plausible range (50–200)", 50.0 <= state["vvix"] <= 200.0,
              f"VVIX={state['vvix']:.2f}")
        check("B1: vix_slope is valid string",
              state["vix_slope"] in ("rising", "falling", "flat"),
              f"slope='{state['vix_slope']}'")
        check("B1: readings incremented to 1", state["readings"] == 1,
              f"readings={state['readings']}")
        check("B1: timestamp recent (within 60s)",
              abs(time.time() - state["timestamp"]) < 60)

        print(f"\n  Live readings:")
        print(f"    VIX    : {state['vix']:.2f}")
        print(f"    VVIX   : {state['vvix']:.2f}")
        print(f"    Slope  : {state['vix_slope']}")
        print(f"    Spike  : {state['vvix_spike']}")
        print(f"    Block  : {state['hard_block']}")
        print(f"    Long ✓ : {state['ok_for_long']}")
        print(f"    Short ✓: {state['ok_for_short']}")

    except Exception as e:
        check("B1: force_poll() succeeds", False, str(e))
        return

    # B2: Second poll accumulates readings
    state2 = monitor.force_poll()
    check("B2: second poll increments readings",
          state2["readings"] == 2, f"readings={state2['readings']}")
    check("B2: second poll not stale", not state2["stale"])

    # B3: Background thread start/stop
    config3  = make_config(poll_interval_s=2)   # fast for test
    monitor3 = VIXMonitor(config3)
    monitor3.start()
    check("B3: is_running() True after start()", monitor3.is_running())
    time.sleep(3)   # wait for at least one poll
    state3 = monitor3.poll()
    check("B3: background poll populated state",
          state3["readings"] >= 1, f"readings={state3['readings']}")
    check("B3: background state not stale", not state3["stale"])
    monitor3.stop()
    time.sleep(0.5)
    check("B3: is_running() False after stop()", not monitor3.is_running())


# ── Group C: Behaviour validation ────────────────────────────────────────────

def test_group_c():
    section("GROUP C — Behaviour validation")

    # C1: Thread safety — concurrent polls
    monitor = VIXMonitor(make_config())
    call_count = {"n": 0}
    errors     = []

    def fetch_mock():
        call_count["n"] += 1
        return (20.0 + call_count["n"] * 0.1, 80.0)

    monitor._fetch_vix_vvix = fetch_mock

    def poll_repeatedly():
        for _ in range(20):
            try:
                state = monitor.poll()
                assert isinstance(state["vix"], float)
            except Exception as e:
                errors.append(str(e))

    threads = [threading.Thread(target=poll_repeatedly) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("C1: concurrent polls — no threading errors", len(errors) == 0,
          f"{len(errors)} errors")

    # C2: Enough history — VVIX average correct
    monitor2 = VIXMonitor(make_config(vvix_avg_window=4))
    monitor2._vvix_history = deque([100.0, 100.0, 100.0, 100.0], maxlen=4)
    monitor2._vix_history  = deque([20.0, 20.0], maxlen=5)
    with patch.object(monitor2, "_fetch_vix_vvix", return_value=(20.0, 100.0)):
        state = monitor2._do_poll()
    check("C2: VVIX average computed correctly",
          abs(state["vvix_avg"] - 100.0) < 0.1, f"avg={state['vvix_avg']:.2f}")

    # C3: Less than 3 VVIX readings → no spike declared
    monitor3 = VIXMonitor(make_config(vvix_spike_pct=0.05))
    monitor3._vvix_history = deque([100.0, 100.0], maxlen=5)   # only 2 readings
    monitor3._vix_history  = deque([20.0, 20.0], maxlen=5)
    # Would spike if average was computed (110 > 100 × 1.05)
    with patch.object(monitor3, "_fetch_vix_vvix", return_value=(20.0, 110.0)):
        state3 = monitor3._do_poll()
    check("C3: < 3 VVIX readings → no spike declared (insufficient history)",
          not state3["vvix_spike"], f"spike={state3['vvix_spike']}")

    # C4: poll() before any data returns safe defaults
    monitor4 = VIXMonitor(make_config())
    state4 = monitor4.poll()
    check("C4: poll() before first data → hard_block=True (safe default)",
          state4["hard_block"])
    check("C4: poll() before first data → ok_for_long=False", not state4["ok_for_long"])

    # C5: Config with scalp.vix block partially specified
    partial_config = {"scalp": {"vix": {"hard_block": 30.0}}}
    monitor5 = VIXMonitor(partial_config)
    check("C5: partial config — specified key overrides default",
          monitor5._hard_block_level == 30.0, f"={monitor5._hard_block_level}")
    check("C5: partial config — unspecified keys use defaults",
          monitor5._poll_interval == 300, f"={monitor5._poll_interval}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="VIXMonitor test suite")
    parser.add_argument("--unit-only", action="store_true",
                        help="Run Group A only (no network required)")
    parser.add_argument("--live",      action="store_true",
                        help="Run Groups A + B (network required)")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  VIXMonitor Test Suite")
    print(f"{'='*60}")

    test_group_a()

    if not args.unit_only:
        test_group_b()
        test_group_c()
    elif args.live:
        test_group_b()
        test_group_c()
    else:
        test_group_c()

    # Summary
    passed = sum(1 for _, p in results if p)
    total  = len(results)
    print(f"\n{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")
    for label, p in results:
        print(f"  {'✅' if p else '❌'}  {label}")
    print(f"{'─'*60}")
    print(f"  {passed}/{total} passed ({passed/total*100:.0f}%)")
    if passed == total:
        print(f"  ✓ All checks passed")
    else:
        print(f"  ⚠️  {total-passed} failed")
    print()


if __name__ == "__main__":
    main()
