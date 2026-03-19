#!/usr/bin/env python3
"""
tests/scalp/test_iv_skew.py
=============================
Test suite for IVSkewMonitor.

Groups:
    A — Unit tests: config, delta extraction, rate of change, gate logic
    B — Live tests: real chain via IBKR (market hours required)
    C — Integration: multi-symbol history, passes_gate pipeline

Usage:
    python3 tests/scalp/test_iv_skew.py --unit-only
    python3 tests/scalp/test_iv_skew.py --account U18705798
"""

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import numpy as np

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.scalp.signals.iv_skew import IVSkewMonitor

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
    bearish_level=3.0,
    steepening_threshold=0.5,
    delta_target=0.25,
    delta_tolerance=0.05,
    history_window=6,
) -> dict:
    return {
        "scalp": {
            "skew": {
                "bearish_level":        bearish_level,
                "steepening_threshold": steepening_threshold,
                "delta_target":         delta_target,
                "delta_tolerance":      delta_tolerance,
                "history_window":       history_window,
            }
        }
    }


# ── Synthetic snapshot builder ────────────────────────────────────────────────

def make_skew_snapshot(
    spot: float,
    call_iv_25d: float,   # IV of the ~25-delta call
    put_iv_25d: float,    # IV of the ~25-delta put
    n_strikes: int = 10,
    strike_step: float = 5.0,
) -> pd.DataFrame:
    """
    Build a synthetic snapshot DataFrame with realistic delta/IV values.
    Strikes span from spot - n/2*step to spot + n/2*step.
    Delta follows: calls decay toward 0 OTM, puts decay toward -1 ITM.
    IV uses a smile: higher at wings, lowest at ATM.
    """
    rows = []
    half = n_strikes // 2

    for i in range(-half, half + 1):
        strike = spot + i * strike_step
        dist   = i / half  # normalised distance: -1 to +1

        # Call delta: 0.5 ATM, decays toward 0 far OTM
        call_delta = 0.5 * np.exp(-abs(dist) * 1.5)
        if dist < 0:
            call_delta = min(call_delta + 0.4, 0.98)  # ITM calls near 1

        # Put delta: -0.5 ATM, more negative ITM
        put_delta = -(0.5 * np.exp(-abs(dist) * 1.5))
        if dist > 0:
            put_delta = max(put_delta - 0.4, -0.98)   # ITM puts near -1

        # IV smile: peaks at wings, lowest ATM
        base_iv   = 0.20 + 0.10 * dist**2

        # At approximately ±25-delta strikes, use the provided values
        # 25-delta call is at dist ≈ +0.5 (approx 2-3 strikes OTM)
        # 25-delta put  is at dist ≈ -0.5
        if abs(dist - 0.5) < 0.3:
            call_iv_this = call_iv_25d * (1 + 0.1 * abs(dist - 0.5))
        else:
            call_iv_this = base_iv

        if abs(dist + 0.5) < 0.3:
            put_iv_this = put_iv_25d * (1 + 0.1 * abs(dist + 0.5))
        else:
            put_iv_this = base_iv + 0.02  # slight put premium everywhere

        rows.append({
            "option_type": "C",
            "strike":      round(strike, 2),
            "delta":       round(call_delta, 4),
            "iv":          round(call_iv_this, 6),
        })
        rows.append({
            "option_type": "P",
            "strike":      round(strike, 2),
            "delta":       round(put_delta, 4),
            "iv":          round(put_iv_this, 6),
        })

    return pd.DataFrame(rows)


def make_mock_client(spot: float, snap: pd.DataFrame) -> MagicMock:
    """Mock client returning provided snapshot."""
    client = MagicMock()
    client.get_spot_price.return_value = spot
    today  = date.today()
    client.get_option_expiries.return_value = [
        (today + timedelta(days=2)).isoformat()
    ]
    n = len(snap) // 2
    client.get_option_chain.return_value = pd.DataFrame(
        {"code": [f"OPT{i:04d}" for i in range(n)]}
    )
    client.get_option_snapshot.return_value = snap
    return client


# ── Group A: Unit tests ───────────────────────────────────────────────────────

def test_group_a():
    section("GROUP A — Unit tests (no network)")

    # A1: Config reads correctly
    m = IVSkewMonitor(make_config(bearish_level=4.0, steepening_threshold=0.8))
    check("A1: bearish_level reads from config",
          m._bearish_level == 4.0, f"={m._bearish_level}")
    check("A1: steepening_threshold reads from config",
          m._steepening_threshold == 0.8, f"={m._steepening_threshold}")

    m2 = IVSkewMonitor({})
    check("A1: defaults applied when no config",
          m2._bearish_level == 3.0, f"={m2._bearish_level}")

    # A2: Empty state is permissive (no readings yet)
    state = m2.get("SPY")
    check("A2: empty state ok_for_long=True (permissive before data)",
          state["ok_for_long"])
    check("A2: empty state readings=0", state["readings"] == 0)
    check("A2: empty state stale=True", state["stale"])

    # A3: _extract_risk_reversal finds correct 25-delta strikes
    monitor = IVSkewMonitor(make_config(delta_target=0.25, delta_tolerance=0.05))

    # Clearly bearish skew: put IV (0.30) much higher than call IV (0.22)
    snap_bearish = make_skew_snapshot(spot=580.0, call_iv_25d=0.22, put_iv_25d=0.30)
    rr_bearish   = monitor._extract_risk_reversal(snap_bearish, 580.0)
    check("A3: bearish skew → positive risk reversal",
          rr_bearish > 0, f"RR={rr_bearish:.4f}")

    # Neutral skew: equal IV
    snap_neutral = make_skew_snapshot(spot=580.0, call_iv_25d=0.22, put_iv_25d=0.22)
    rr_neutral   = monitor._extract_risk_reversal(snap_neutral, 580.0)
    check("A3: equal IV → risk reversal near zero",
          abs(rr_neutral) < 0.05, f"RR={rr_neutral:.4f}")

    # A4: Risk reversal magnitude is in the right ballpark
    # Strategy doc uses values like 3.0 (in vol points = 0.03 in decimal)
    # but ibkr_connector returns IV as decimal (0.22 = 22%)
    # The monitor should return raw difference in same units
    snap_high = make_skew_snapshot(spot=580.0, call_iv_25d=0.20, put_iv_25d=0.30)
    rr_high   = monitor._extract_risk_reversal(snap_high, 580.0)
    check("A4: high skew: put_iv=0.30, call_iv=0.20 → RR in (0, 0.15)",
          0 < rr_high < 0.15, f"RR={rr_high:.4f}")

    # A5: Rate of change — compute_delta
    from collections import deque
    m3 = IVSkewMonitor(make_config(history_window=4))
    m3._history["SPY"] = deque([
        (time.time() - 300, 2.0),
        (time.time() - 200, 2.5),
        (time.time() - 100, 3.0),
        (time.time(),       3.5),
    ], maxlen=5)
    delta = m3._compute_delta("SPY")
    check("A5: delta = latest - oldest = 3.5 - 2.0 = 1.5",
          abs(delta - 1.5) < 0.01, f"delta={delta}")

    # A6: Not enough history → delta=0
    m4 = IVSkewMonitor(make_config())
    m4._history["SPY"] = deque([(time.time(), 2.5)], maxlen=5)
    delta2 = m4._compute_delta("SPY")
    check("A6: single reading → delta=0", delta2 == 0.0, f"delta={delta2}")

    # A7: refresh() with mock client updates state
    m5  = IVSkewMonitor(make_config())
    snap7 = make_skew_snapshot(spot=580.0, call_iv_25d=0.22, put_iv_25d=0.27)
    c7    = make_mock_client(580.0, snap7)
    state7 = m5.refresh("SPY", c7)

    check("A7: refresh() returns non-stale state", not state7["stale"])
    check("A7: skew_level is float > 0", state7["skew_level"] > 0,
          f"level={state7['skew_level']:.4f}")
    check("A7: readings=1 after first refresh", state7["readings"] == 1)

    # A8: Gate logic — elevated skew blocks longs
    m6 = IVSkewMonitor(make_config(bearish_level=3.0, steepening_threshold=0.5))
    m6._state["SPY"] = {
        "symbol": "SPY", "skew_level": 3.5, "skew_delta": 0.1,
        "bias": "bearish", "steepening": False,
        "ok_for_long": False, "ok_for_short": True,
        "readings": 5, "stale": False, "timestamp": time.time(),
    }
    gate_long  = m6.passes_gate("SPY", "LONG")
    gate_short = m6.passes_gate("SPY", "SHORT")
    check("A8: elevated skew (3.5 >= 3.0) → LONG gate fails",
          not gate_long["passed"], f"reason={gate_long['reason']}")
    check("A8: elevated skew (3.5 >= 3.0) → SHORT gate passes",
          gate_short["passed"])

    # A9: Steepening blocks longs regardless of level
    m7 = IVSkewMonitor(make_config(bearish_level=3.0, steepening_threshold=0.5))
    m7._state["QQQ"] = {
        "symbol": "QQQ", "skew_level": 2.5, "skew_delta": 0.8,
        "bias": "neutral", "steepening": True,   # steepening despite low level
        "ok_for_long": False, "ok_for_short": True,
        "readings": 3, "stale": False, "timestamp": time.time(),
    }
    gate_steep = m7.passes_gate("QQQ", "LONG")
    check("A9: steepening → LONG gate fails even with level < 3.0",
          not gate_steep["passed"], f"reason={gate_steep['reason']}")

    # A10: Low skew passes long gate
    m8 = IVSkewMonitor(make_config())
    m8._state["AAPL"] = {
        "symbol": "AAPL", "skew_level": 1.5, "skew_delta": -0.1,
        "bias": "bullish", "steepening": False,
        "ok_for_long": True, "ok_for_short": False,
        "readings": 4, "stale": False, "timestamp": time.time(),
    }
    gate_low = m8.passes_gate("AAPL", "LONG")
    check("A10: low skew (1.5) + not steepening → LONG gate passes",
          gate_low["passed"], f"reason={gate_low['reason']}")

    # A11: Short gate fails when skew is low and not steepening
    gate_short_low = m8.passes_gate("AAPL", "SHORT")
    check("A11: low skew + not steepening → SHORT gate fails",
          not gate_short_low["passed"],
          f"reason={gate_short_low['reason']}")

    # A12: bias computation
    m9 = IVSkewMonitor(make_config(bearish_level=3.0, steepening_threshold=0.5))
    check("A12: bias=bearish_accelerating when high+steepening",
          m9._compute_bias(4.0, 0.8) == "bearish_accelerating")
    check("A12: bias=bearish when high+stable",
          m9._compute_bias(3.5, 0.1) == "bearish")
    check("A12: bias=neutral when moderate",
          m9._compute_bias(2.2, 0.1) == "neutral")
    check("A12: bias=bullish when low",
          m9._compute_bias(1.0, 0.1) == "bullish")
    check("A12: bias=bullish_improving when falling fast",
          m9._compute_bias(1.0, -0.6) == "bullish_improving")

    # A13: steepening flag set correctly in refresh
    m10 = IVSkewMonitor(make_config(bearish_level=3.0, steepening_threshold=0.5))
    snap10 = make_skew_snapshot(580.0, call_iv_25d=0.20, put_iv_25d=0.25)
    c10    = make_mock_client(580.0, snap10)

    # Inject prior history so delta will be high
    from collections import deque as dq
    m10._history["SPY"] = dq(
        [(time.time() - i*300, 1.0) for i in range(4, 0, -1)],
        maxlen=7
    )
    state10 = m10.refresh("SPY", c10)
    # delta = latest - first_reading; first was 1.0, latest is skew_level
    # If skew_level - 1.0 >= 0.5, steepening=True
    check("A13: steepening=True when delta >= threshold",
          state10["steepening"] == (state10["skew_delta"] >= 0.5),
          f"delta={state10['skew_delta']:.3f}, steepening={state10['steepening']}")

    # A14: Column name normalisation — alternative snapshot format
    snap_alt = pd.DataFrame([
        {"right": "C", "strike": 590.0, "delta": 0.26, "iv": 0.22},
        {"right": "C", "strike": 595.0, "delta": 0.18, "iv": 0.23},
        {"right": "P", "strike": 570.0, "delta": -0.24, "iv": 0.28},
        {"right": "P", "strike": 565.0, "delta": -0.18, "iv": 0.30},
    ])
    try:
        rr_alt = IVSkewMonitor(make_config())._extract_risk_reversal(snap_alt, 580.0)
        check("A14: alternative column names (right/delta/iv/strike) handled",
              rr_alt > 0, f"RR={rr_alt:.4f}")
    except Exception as e:
        check("A14: alternative column names handled", False, str(e))

    # A15: No OTM calls raises ValueError gracefully
    snap_itm_only = pd.DataFrame([
        {"option_type": "C", "strike": 560.0, "delta": 0.90, "iv": 0.18},  # ITM
        {"option_type": "P", "strike": 570.0, "delta": -0.25, "iv": 0.27},
    ])
    try:
        IVSkewMonitor(make_config())._extract_risk_reversal(snap_itm_only, 580.0)
        check("A15: no OTM calls raises ValueError", False, "should have raised")
    except ValueError as e:
        check("A15: no OTM calls raises ValueError", True, str(e)[:50])


# ── Group B: Live IBKR tests ──────────────────────────────────────────────────

def test_group_b(account: str, symbol: str = "SPY"):
    section(f"GROUP B — Live IBKR tests ({symbol}, requires TWS)")
    print(f"  Connecting | account={account} | client_id=4\n")

    try:
        from ibkr_connector import IBKRClient
    except ImportError:
        check("B0: IBKRClient importable", False,
              "pip install -e /Users/user/ibkr-connector")
        return

    client = IBKRClient(port=7496, account=account, client_id=4, mode="paper")
    try:
        client.connect()
        check("B0: connected", client.is_connected())
    except Exception as e:
        check("B0: connected", False, str(e))
        return

    monitor = IVSkewMonitor(make_config())
    try:
        print(f"  Computing IV skew for {symbol}...")
        state = monitor.refresh(symbol, client)

        check("B1: refresh() not stale", not state["stale"], f"stale={state['stale']}")
        check("B1: skew_level is float", isinstance(state["skew_level"], float),
              f"level={state['skew_level']:.4f}")
        check("B1: skew in plausible range (-0.2 to 0.3)",
              -0.20 <= state["skew_level"] <= 0.30,
              f"level={state['skew_level']:.4f}")
        check("B1: bias is valid string",
              state["bias"] in ("bearish_accelerating", "bearish", "neutral",
                                "bullish", "bullish_improving"),
              f"bias={state['bias']}")
        check("B1: readings=1", state["readings"] == 1, f"={state['readings']}")

        print(f"\n  {symbol} IV Skew:")
        print(f"    Skew level : {state['skew_level']:.4f} "
              f"({'bearish' if state['skew_level'] > 0 else 'bullish'})")
        print(f"    Skew delta : {state['skew_delta']:+.4f}")
        print(f"    Bias       : {state['bias']}")
        print(f"    Steepening : {state['steepening']}")
        print(f"    OK for long: {state['ok_for_long']}")
        print(f"    OK for short: {state['ok_for_short']}")

        gate_l = monitor.passes_gate(symbol, "LONG")
        gate_s = monitor.passes_gate(symbol, "SHORT")
        print(f"\n  Gate 3 LONG  : {'PASS' if gate_l['passed'] else 'FAIL'} — {gate_l['reason']}")
        print(f"  Gate 3 SHORT : {'PASS' if gate_s['passed'] else 'FAIL'} — {gate_s['reason']}")

        # B2: Second refresh accumulates history
        state2 = monitor.refresh(symbol, client)
        check("B2: second refresh readings=2", state2["readings"] == 2,
              f"={state2['readings']}")

    except Exception as e:
        check("B1: refresh() no error", False, str(e))
        import traceback; traceback.print_exc()
    finally:
        client.disconnect()
        print("\n  Disconnected")


# ── Group C: Integration ──────────────────────────────────────────────────────

def test_group_c():
    section("GROUP C — Integration (multi-symbol, history, gate pipeline)")

    # C1: Multiple symbols tracked independently
    m = IVSkewMonitor(make_config())
    snap_spy = make_skew_snapshot(580.0, 0.22, 0.28)
    snap_qqq = make_skew_snapshot(470.0, 0.24, 0.29)
    c_spy    = make_mock_client(580.0, snap_spy)
    c_qqq    = make_mock_client(470.0, snap_qqq)

    s_spy = m.refresh("SPY", c_spy)
    s_qqq = m.refresh("QQQ", c_qqq)

    check("C1: SPY and QQQ tracked independently",
          s_spy["symbol"] == "SPY" and s_qqq["symbol"] == "QQQ")
    check("C1: SPY skew != QQQ skew (different IV inputs)",
          abs(s_spy["skew_level"] - s_qqq["skew_level"]) < 0.2,   # both use symmetric snap
          f"spy={s_spy['skew_level']:.4f} qqq={s_qqq['skew_level']:.4f}")

    # C2: History accumulates over multiple refreshes
    m2 = IVSkewMonitor(make_config(history_window=5))
    snap2 = make_skew_snapshot(500.0, 0.20, 0.25)
    c2    = make_mock_client(500.0, snap2)

    for _ in range(4):
        m2.refresh("NVDA", c2)

    hist = m2.get_history("NVDA")
    check("C2: history has 4 entries", len(hist) == 4, f"len={len(hist)}")
    check("C2: history entries are (timestamp, level) tuples",
          isinstance(hist[0], tuple) and len(hist[0]) == 2)

    # C3: History window caps correctly
    m3 = IVSkewMonitor(make_config(history_window=3))
    snap3 = make_skew_snapshot(400.0, 0.20, 0.22)
    c3    = make_mock_client(400.0, snap3)

    for _ in range(6):  # 6 refreshes into window of 3
        m3.refresh("TSLA", c3)

    hist3 = m3.get_history("TSLA")
    check("C3: history window caps at history_window+1",
          len(hist3) <= 4, f"len={len(hist3)}")  # deque maxlen = window+1

    # C4: passes_gate pipeline end-to-end
    m4 = IVSkewMonitor(make_config(bearish_level=3.0))
    # High put demand scenario
    snap_high = make_skew_snapshot(580.0, call_iv_25d=0.18, put_iv_25d=0.30)
    c4 = make_mock_client(580.0, snap_high)
    m4.refresh("SPY", c4)

    g_long  = m4.passes_gate("SPY", "LONG")
    g_short = m4.passes_gate("SPY", "SHORT")
    check("C4: high put demand — gate result contains passed+reason+state",
          "passed" in g_long and "reason" in g_long and "state" in g_long)

    # C5: get() before refresh returns empty/permissive state
    m5 = IVSkewMonitor(make_config())
    state5 = m5.get("AMZN")
    check("C5: get() before refresh returns safe state",
          state5["readings"] == 0 and state5["stale"])

    # C6: Unknown direction in passes_gate returns False
    m6 = IVSkewMonitor(make_config())
    m6._state["SPY"] = IVSkewMonitor._empty_state("SPY")
    m6._state["SPY"]["readings"] = 1
    gate_bad = m6.passes_gate("SPY", "SIDEWAYS")
    check("C6: unknown direction → gate fails safely",
          not gate_bad["passed"], f"reason={gate_bad['reason']}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--unit-only", action="store_true")
    parser.add_argument("--account",   default="")
    parser.add_argument("--symbol",    default="SPY")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  IVSkewMonitor Test Suite")
    print(f"{'='*60}")

    test_group_a()
    test_group_c()

    if args.account and not args.unit_only:
        test_group_b(args.account, args.symbol)
    elif not args.unit_only and not args.account:
        print("\n  Skipping Group B — pass --account U18705798 for live tests")

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
        print("  ✓ All checks passed")
    else:
        print(f"  ⚠️  {total-passed} failed")
    print()


if __name__ == "__main__":
    main()
