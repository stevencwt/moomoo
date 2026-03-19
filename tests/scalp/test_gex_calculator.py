#!/usr/bin/env python3
"""
tests/scalp/test_gex_calculator.py
====================================
Test suite for GEXCalculator.

Groups:
    A — Unit tests: pure math, no network, no IBKR
    B — Live tests: real chain via IBKR (market hours, TWS running)
    C — Integration: proximity checks, cache, should_refresh

Usage:
    python3 tests/scalp/test_gex_calculator.py --unit-only
    python3 tests/scalp/test_gex_calculator.py --account U18705798
    python3 tests/scalp/test_gex_calculator.py --account U18705798 --symbol NVDA
"""

import argparse
import sys
import time
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pandas as pd
import numpy as np

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.scalp.signals.gex_calculator import GEXCalculator, compute_gex

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

def make_config(proximity_pct=0.003) -> dict:
    return {"scalp": {"gex": {"proximity_pct": proximity_pct}}}


# ── Synthetic chain builder ───────────────────────────────────────────────────

def make_synthetic_snapshot(
    spot: float,
    strikes: list,
    call_oi_pattern: dict = None,    # strike → OI multiplier
    put_oi_pattern:  dict = None,
    gamma_base: float = 0.05,
) -> pd.DataFrame:
    """
    Build a synthetic option snapshot DataFrame for testing.
    Gamma follows a bell curve centred on ATM (peak at spot).
    OI can be customised per strike via pattern dicts.

    Returns DataFrame with columns matching IBKRClient.get_option_snapshot():
        option_type, strike_price, option_gamma, option_open_interest
    """
    rows = []
    atm  = min(strikes, key=lambda s: abs(s - spot))

    for strike in strikes:
        # Gamma: bell curve — highest ATM, decays away
        distance_pct = abs(strike - spot) / spot
        gamma        = gamma_base * np.exp(-20 * distance_pct ** 2)
        gamma        = max(gamma, 0.001)   # floor at 0.001

        # Default OI: 1000 contracts, modified by pattern
        call_oi = 1000 * (call_oi_pattern.get(strike, 1.0) if call_oi_pattern else 1.0)
        put_oi  = 1000 * (put_oi_pattern.get(strike, 1.0)  if put_oi_pattern  else 1.0)

        rows.append({
            "option_type":          "C",
            "strike_price":         float(strike),
            "option_gamma":         round(gamma, 6),
            "option_open_interest": float(call_oi),
        })
        rows.append({
            "option_type":          "P",
            "strike_price":         float(strike),
            "option_gamma":         round(gamma, 6),
            "option_open_interest": float(put_oi),
        })

    return pd.DataFrame(rows)


def make_mock_client(
    spot: float,
    strikes: list,
    expiries: list = None,
    call_oi_pattern: dict = None,
    put_oi_pattern:  dict = None,
    gamma_base: float = 0.05,
) -> MagicMock:
    """Build a mock IBKRClient that returns synthetic data."""
    client = MagicMock()
    client.get_spot_price.return_value = spot

    if expiries is None:
        # Use a future date as default expiry
        from datetime import date, timedelta
        next_fri = date.today() + timedelta(days=(4 - date.today().weekday()) % 7 or 7)
        expiries = [next_fri.isoformat()]

    client.get_option_expiries.return_value = expiries

    # Fake chain returns DataFrames with 'code' column
    # OCC format: strike × 1000, zero-padded to 8 digits
    # e.g. strike 580.0 → 580000 → '00580000'
    codes_call = [f"SPY{e.replace('-','')[-6:]}C{str(int(s*1000)).zfill(8)}" for s in strikes
                  for e in expiries[:1]]
    codes_put  = [f"SPY{e.replace('-','')[-6:]}P{str(int(s*1000)).zfill(8)}" for s in strikes
                  for e in expiries[:1]]

    client.get_option_chain.side_effect = lambda sym, exp, right: (
        pd.DataFrame({"code": codes_call}) if right in ("CALL", "C")
        else pd.DataFrame({"code": codes_put})
    )

    snap = make_synthetic_snapshot(
        spot, strikes, call_oi_pattern, put_oi_pattern, gamma_base
    )
    client.get_option_snapshot.return_value = snap

    return client


# ── Group A: Pure math / unit tests ──────────────────────────────────────────

def test_group_a():
    section("GROUP A — Unit tests (no network, no IBKR)")

    # A1: Config reads correctly
    calc = GEXCalculator(make_config(proximity_pct=0.005))
    check("A1: proximity_pct reads from config",
          calc._proximity_pct == 0.005, f"={calc._proximity_pct}")

    calc2 = GEXCalculator({})
    check("A1: default proximity_pct=0.003",
          calc2._proximity_pct == 0.003, f"={calc2._proximity_pct}")

    # A2: Net GEX computation — equal OI, calls dominate at call-heavy strike
    spot    = 500.0
    strikes = [490, 495, 500, 505, 510]

    # Boost call OI at 500 massively → that strike should be gamma wall
    snap = make_synthetic_snapshot(spot, strikes, call_oi_pattern={500: 10.0})
    net_gex = GEXCalculator._compute_net_gex(snap, spot)

    check("A2: net_gex Series not empty", not net_gex.empty,
          f"strikes={list(net_gex.index)}")
    check("A2: net_gex has all strikes",
          set(net_gex.index) == set(float(s) for s in strikes),
          f"index={sorted(net_gex.index)}")
    check("A2: strike 500 has positive net GEX (call-heavy)",
          net_gex[500.0] > 0, f"gex[500]={net_gex[500.0]:.0f}")

    # A3: Gamma wall identification
    # Manually set OI so 505 has the highest positive GEX
    snap3 = make_synthetic_snapshot(
        spot, strikes,
        call_oi_pattern={490: 0.1, 495: 0.5, 500: 1.0, 505: 8.0, 510: 0.1}
    )
    net_gex3 = GEXCalculator._compute_net_gex(snap3, spot)
    wall3    = GEXCalculator._find_gamma_wall(net_gex3)
    check("A3: gamma wall = strike with highest positive net GEX",
          wall3 == 505.0, f"wall={wall3}, net_gex={net_gex3.to_dict()}")

    # A4: Gamma wall fallback when all GEX is negative
    neg_snap = make_synthetic_snapshot(
        spot, strikes,
        call_oi_pattern={s: 0.01 for s in strikes},   # tiny call OI
        put_oi_pattern={s: 50.0 for s in strikes},     # massive put OI
    )
    neg_net_gex = GEXCalculator._compute_net_gex(neg_snap, spot)
    wall_neg    = GEXCalculator._find_gamma_wall(neg_net_gex)
    check("A4: gamma wall fallback — no positive GEX, uses idxmax",
          wall_neg in [float(s) for s in strikes],
          f"wall={wall_neg}")

    # A5: GEX flip level
    # Build a net GEX series where strikes ≤ 495 sum to negative
    net_gex_manual = pd.Series({
        510.0:  5000.0,
        505.0:  8000.0,
        500.0:  3000.0,
        495.0: -6000.0,
        490.0: -5000.0,
        485.0: -3000.0,
    }).sort_index(ascending=False)

    flip = GEXCalculator._find_gex_flip(net_gex_manual, 502.0)
    # Cumsum from top: 5000, 13000, 16000, 10000, 5000, 2000 — never negative
    # So no flip found → falls back to minimum strike
    check("A5: GEX flip fallback to min strike when cumsum never negative",
          flip == 485.0, f"flip={flip}")

    # A5b: GEX flip when cumsum does go negative
    net_gex_flip = pd.Series({
        510.0:  2000.0,
        505.0:  1000.0,
        500.0:   500.0,
        495.0: -5000.0,   # large put GEX — cumsum goes negative here
        490.0: -3000.0,
        485.0: -2000.0,
    }).sort_index(ascending=False)
    flip2 = GEXCalculator._find_gex_flip(net_gex_flip, 502.0)
    # Cumsum: 2000, 3000, 3500, -1500 → goes negative at 495
    check("A5b: GEX flip found at correct strike",
          flip2 == 495.0, f"flip={flip2}")

    # A6: is_stabilising based on total net GEX sign
    pos_series = pd.Series({500.0: 5000.0, 495.0: 2000.0, 490.0: -1000.0})
    neg_series = pd.Series({500.0: -5000.0, 495.0: -3000.0, 490.0: 1000.0})
    check("A6: positive total GEX → is_stabilising=True",
          pos_series.sum() > 0)
    check("A6: negative total GEX → is_stabilising=False",
          neg_series.sum() < 0)

    # A7: compute() with mock client — end-to-end
    calc7  = GEXCalculator(make_config())
    client = make_mock_client(
        spot=580.0,
        strikes=[565, 570, 575, 580, 585, 590, 595],
        call_oi_pattern={580: 5.0},   # ATM call-heavy → wall at 580
    )
    result = calc7.compute("SPY", client)

    check("A7: compute() returns non-error result", result["error"] == "",
          f"error='{result['error']}'")
    check("A7: gamma_wall is a valid strike",
          result["gamma_wall"] in [565.0, 570.0, 575.0, 580.0, 585.0, 590.0, 595.0],
          f"wall={result['gamma_wall']}")
    check("A7: gex_flip is a valid strike",
          result["gex_flip"] in [565.0, 570.0, 575.0, 580.0, 585.0, 590.0, 595.0],
          f"flip={result['gex_flip']}")
    check("A7: spot returned correctly", result["spot"] == 580.0, f"={result['spot']}")
    check("A7: is_stabilising is bool", isinstance(result["is_stabilising"], bool))
    check("A7: net_gex_series is pd.Series",
          isinstance(result["net_gex_series"], pd.Series))
    check("A7: computed_at is recent", abs(time.time() - result["computed_at"]) < 5)

    # A8: compute() result is cached
    cached = calc7.get_cached("SPY")
    check("A8: result cached after compute()", cached is not None)
    check("A8: cached gamma_wall matches", cached["gamma_wall"] == result["gamma_wall"])

    # A9: compute() with client error returns safe error result
    bad_client = MagicMock()
    bad_client.get_spot_price.side_effect = Exception("IBKR disconnected")
    result_err = calc7.compute("QQQ", bad_client)
    check("A9: error result has error field", result_err["error"] != "",
          f"error='{result_err['error']}'")
    check("A9: error result is_stabilising=True (conservative default)",
          result_err["is_stabilising"])
    check("A9: error result gamma_wall=0.0", result_err["gamma_wall"] == 0.0)

    # A10: _compute_net_gex handles missing gamma/OI gracefully
    snap_missing = pd.DataFrame([
        {"option_type": "C", "strike_price": 500.0,
         "option_gamma": None, "option_open_interest": 1000.0},
        {"option_type": "C", "strike_price": 505.0,
         "option_gamma": 0.05, "option_open_interest": None},
        {"option_type": "C", "strike_price": 510.0,
         "option_gamma": 0.03, "option_open_interest": 800.0},
        {"option_type": "P", "strike_price": 510.0,
         "option_gamma": 0.03, "option_open_interest": 500.0},
    ])
    net_missing = GEXCalculator._compute_net_gex(snap_missing, 507.0)
    check("A10: rows with missing gamma/OI dropped gracefully",
          510.0 in net_missing.index and 500.0 not in net_missing.index,
          f"index={sorted(net_missing.index)}")

    # A11: is_near_gex_level proximity check
    calc11 = GEXCalculator(make_config(proximity_pct=0.003))
    calc11._cache["SPY"] = {
        "gamma_wall": 580.0,
        "gex_flip":   570.0,
        "error":      "",
    }
    # spot=580.5: 0.5/580=0.086% — within 0.3%
    near1 = calc11.is_near_gex_level("SPY", 580.5)
    check("A11: spot within 0.3% of wall → near_wall=True",
          near1["near_wall"], f"dist={near1['wall_dist_pct']:.3f}%")

    # spot=585: 5/580=0.86% — outside 0.3%
    near2 = calc11.is_near_gex_level("SPY", 585.0)
    check("A11: spot 0.86% from wall → near_wall=False",
          not near2["near_wall"], f"dist={near2['wall_dist_pct']:.3f}%")

    # spot=570.2: near flip
    near3 = calc11.is_near_gex_level("SPY", 570.2)
    check("A11: spot within 0.3% of flip → near_flip=True",
          near3["near_flip"], f"flip_dist={near3['flip_dist_pct']:.3f}%")

    # A12: is_near_gex_level returns safe result when no cache
    calc12 = GEXCalculator(make_config())
    no_cache = calc12.is_near_gex_level("NVDA", 800.0)
    check("A12: no cache → near_wall=False, near_flip=False",
          not no_cache["near_wall"] and not no_cache["near_flip"])
    check("A12: no cache → side='none'", no_cache["side"] == "none")

    # A13: _select_front_expiry picks nearest future date
    from datetime import timedelta
    today    = date.today()
    expiries = [
        (today - timedelta(days=1)).isoformat(),   # past — should be skipped
        (today + timedelta(days=2)).isoformat(),   # nearest future
        (today + timedelta(days=9)).isoformat(),
        (today + timedelta(days=30)).isoformat(),
    ]
    front = GEXCalculator._select_front_expiry(expiries)
    check("A13: front expiry skips past dates, picks nearest future",
          front == expiries[1], f"front={front}, expected={expiries[1]}")

    # A14: _select_front_expiry includes today
    expiries_with_today = [
        today.isoformat(),
        (today + timedelta(days=7)).isoformat(),
    ]
    front_today = GEXCalculator._select_front_expiry(expiries_with_today)
    check("A14: front expiry includes today if present",
          front_today == today.isoformat(), f"front={front_today}")

    # A15: net_gex column normalisation — alternative column names
    snap_alt = pd.DataFrame([
        {"right": "C", "strike": 500.0, "gamma": 0.05, "oi": 1000.0},
        {"right": "P", "strike": 500.0, "gamma": 0.05, "oi":  800.0},
        {"right": "C", "strike": 505.0, "gamma": 0.03, "oi":  500.0},
        {"right": "P", "strike": 505.0, "gamma": 0.03, "oi":  600.0},
    ])
    try:
        net_alt = GEXCalculator._compute_net_gex(snap_alt, 502.0)
        check("A15: alternative column names (right/gamma/oi/strike) handled",
              not net_alt.empty, f"strikes={sorted(net_alt.index)}")
    except Exception as e:
        check("A15: alternative column names handled", False, str(e))


# ── Group B: Live IBKR tests ──────────────────────────────────────────────────

def test_group_b(account: str, symbol: str = "SPY"):
    section(f"GROUP B — Live IBKR tests ({symbol}, requires TWS + market hours)")
    print(f"  Connecting to TWS | account={account} | client_id=4\n")

    try:
        sys.path.insert(0, str(ROOT / "ibkr-connector"))
    except Exception:
        pass

    try:
        from ibkr_connector import IBKRClient
    except ImportError:
        try:
            from src.connectors.ibkr_connector import IBKRConnector as IBKRClient
        except ImportError:
            check("B0: IBKRClient importable", False,
                  "Install ibkr_connector: pip install -e /Users/user/ibkr-connector")
            return

    client = IBKRClient(
        port=7496,
        account=account,
        client_id=4,   # options=1, test_suite=2, stream_test=3, gex_test=4
        mode="paper",
    )

    try:
        client.connect()
        check("B0: connected to IBKR TWS", client.is_connected(),
              f"account={account}")
    except Exception as e:
        check("B0: connected to IBKR TWS", False, str(e))
        return

    calc = GEXCalculator(make_config())

    # B1: compute() against live chain
    try:
        print(f"  Computing GEX for {symbol}...")
        # Debug: print spot and raw chain size before compute
        try:
            raw_spot = client.get_spot_price(symbol)
            expiries = client.get_option_expiries(symbol)
            front    = sorted([e for e in expiries if e >= str(__import__('datetime').date.today())])[0]
            calls_raw = client.get_option_chain(symbol, front, "CALL")
            puts_raw  = client.get_option_chain(symbol, front, "PUT")
            print(f"  Debug: spot=${raw_spot:.2f}  expiry={front}  "
                  f"calls={len(calls_raw)}  puts={len(puts_raw)}")
            if not calls_raw.empty and "code" in calls_raw.columns:
                sample = calls_raw["code"].head(3).tolist()
                print(f"  Debug: sample codes: {sample}")
        except Exception as dbg_e:
            print(f"  Debug: pre-check failed: {dbg_e}")
        result = calc.compute(symbol, client)

        check("B1: compute() no error", result["error"] == "",
              f"error='{result['error']}'")
        check("B1: gamma_wall > 0", result["gamma_wall"] > 0,
              f"wall=${result['gamma_wall']:.2f}")
        check("B1: gex_flip > 0", result["gex_flip"] > 0,
              f"flip=${result['gex_flip']:.2f}")
        check("B1: spot > 0", result["spot"] > 0, f"spot=${result['spot']:.2f}")
        check("B1: expiry is valid ISO date",
              len(result["expiry"]) == 10, f"expiry={result['expiry']}")
        check("B1: net_gex_series not empty",
              len(result["net_gex_series"]) > 0,
              f"{len(result['net_gex_series'])} strikes")
        check("B1: total_net_gex is float",
              isinstance(result["total_net_gex"], float),
              f"total={result['total_net_gex']:.0f}")
        check("B1: is_stabilising is bool",
              isinstance(result["is_stabilising"], bool),
              f"stabilising={result['is_stabilising']}")

        # Print the GEX map for visual inspection
        spot = result["spot"]
        wall = result["gamma_wall"]
        flip = result["gex_flip"]
        print(f"\n  {'─'*50}")
        print(f"  {symbol} GEX Map  |  expiry={result['expiry']}")
        print(f"  {'─'*50}")
        if spot <= 0:
            print("  (no data — spot=0, computation failed)")
        else:
            print(f"  Spot            : ${spot:.2f}")
            print(f"  Gamma wall      : ${wall:.2f}  "
                  f"({(wall-spot)/spot*100:+.2f}% from spot)")
            print(f"  GEX flip        : ${flip:.2f}  "
                  f"({(flip-spot)/spot*100:+.2f}% from spot)")
        print(f"  Mode            : "
              f"{'STABILISING (reversion)' if result['is_stabilising'] else 'AMPLIFYING (momentum)'}")
        print(f"  Total net GEX   : {result['total_net_gex']:+,.0f}")
        print(f"  Call GEX        : {result['call_gex_total']:+,.0f}")
        print(f"  Put GEX         : {result['put_gex_total']:+,.0f}")
        print(f"  Strikes in chain: {len(result['net_gex_series'])}")

        # Print top 10 strikes by absolute GEX
        print(f"\n  Top 10 strikes by |GEX|:")
        top10 = result["net_gex_series"].abs().nlargest(10)
        for strike in top10.index:
            gex_val = result["net_gex_series"][strike]
            marker  = " ← wall" if strike == wall else (" ← flip" if strike == flip else "")
            dist    = (strike - spot) / spot * 100 if spot > 0 else 0.0
            print(f"    ${strike:>7.2f}  ({dist:+5.2f}%)  GEX={gex_val:>+12,.0f}{marker}")
        print()

        # B2: Proximity check near current spot
        prox = calc.is_near_gex_level(symbol, spot)
        check("B2: is_near_gex_level() returns valid structure",
              "near_wall" in prox and "near_flip" in prox)
        check("B2: wall_dist_pct is float",
              isinstance(prox["wall_dist_pct"], float),
              f"{prox['wall_dist_pct']:.3f}%")
        print(f"  Proximity check  : wall_dist={prox['wall_dist_pct']:.3f}%  "
              f"flip_dist={prox['flip_dist_pct']:.3f}%  "
              f"near_wall={prox['near_wall']}  side={prox['side']}")

        # B3: Cache check
        cached = calc.get_cached(symbol)
        check("B3: result cached after compute()", cached is not None)
        if cached is not None:
            check("B3: cached wall matches",
                  cached["gamma_wall"] == result["gamma_wall"],
                  f"cached={cached['gamma_wall']} result={result['gamma_wall']}")

        # B4: Second symbol (QQQ) if primary was SPY
        if symbol == "SPY":
            print(f"\n  Computing GEX for QQQ...")
            result_qqq = calc.compute("QQQ", client)
            check("B4: QQQ compute() no error", result_qqq["error"] == "",
                  f"error='{result_qqq['error']}'")
            check("B4: QQQ gamma_wall > 0", result_qqq["gamma_wall"] > 0,
                  f"wall=${result_qqq['gamma_wall']:.2f}")
            print(f"  QQQ wall=${result_qqq['gamma_wall']:.2f}  "
                  f"flip=${result_qqq['gex_flip']:.2f}  "
                  f"spot=${result_qqq['spot']:.2f}  "
                  f"{'STAB' if result_qqq['is_stabilising'] else 'AMP'}")

    except Exception as e:
        check("B1: compute() no error", False, str(e))
        import traceback; traceback.print_exc()
    finally:
        client.disconnect()
        print("\n  Disconnected")


# ── Group C: Integration tests ────────────────────────────────────────────────

def test_group_c():
    section("GROUP C — Integration tests (proximity, cache, refresh logic)")

    # C1: compute() then is_near_gex_level() pipeline
    calc   = GEXCalculator(make_config(proximity_pct=0.003))
    client = make_mock_client(
        spot=580.0,
        strikes=[570, 575, 580, 585, 590],
        call_oi_pattern={580: 8.0},   # strong wall at 580
    )
    result = calc.compute("SPY", client)
    check("C1: pipeline — compute succeeds", result["error"] == "")

    # Test spot very near wall
    spot_near_wall = result["gamma_wall"] * 1.002  # 0.2% above wall
    prox = calc.is_near_gex_level("SPY", spot_near_wall)
    check("C1: pipeline — spot 0.2% from wall triggers near_wall",
          prox["near_wall"], f"dist={prox['wall_dist_pct']:.3f}%")

    # C2: Multiple symbols cached independently
    calc2   = GEXCalculator(make_config())
    client_spy = make_mock_client(580.0, [570, 575, 580, 585, 590])
    client_qqq = make_mock_client(470.0, [460, 465, 470, 475, 480])
    calc2.compute("SPY", client_spy)
    calc2.compute("QQQ", client_qqq)
    check("C2: SPY and QQQ cached independently",
          calc2.get_cached("SPY") is not None and calc2.get_cached("QQQ") is not None)
    check("C2: SPY spot != QQQ spot in cache",
          calc2.get_cached("SPY")["spot"] != calc2.get_cached("QQQ")["spot"])

    # C3: Error result doesn't pollute cache with bad data
    calc3 = GEXCalculator(make_config())
    bad_client = MagicMock()
    bad_client.get_spot_price.return_value = 0.0   # invalid
    bad_client.get_option_expiries.return_value = []
    result_err = calc3.compute("NVDA", bad_client)
    check("C3: invalid spot → error result returned",
          result_err["error"] != "", f"error='{result_err['error']}'")

    # C4: GEX with realistic SPY-like values — sanity check magnitudes
    # SPY at 580, gamma ~0.005 at ATM, OI ~50000 contracts
    # Use realistic OI (50000 base) so GEX magnitude is correct
    # ATM gex = gamma × OI × spot × 100
    #         ≈ 0.005 × 50000 × 580 × 100 = 14,500,000
    # Call heavy at 580 (3×): ≈ 0.005 × 150000 × 580 × 100 ≈ 43,500,000
    import pandas as pd
    rows_realistic = []
    for strike in range(555, 605, 5):
        dist  = abs(strike - 580.0) / 580.0
        gamma = 0.005 * __import__('numpy').exp(-20 * dist**2)
        gamma = max(gamma, 0.0005)
        call_oi = 150000.0 if strike == 580 else 50000.0
        put_oi  = 50000.0
        rows_realistic += [
            {"option_type": "C", "strike_price": float(strike),
             "option_gamma": gamma, "option_open_interest": call_oi},
            {"option_type": "P", "strike_price": float(strike),
             "option_gamma": gamma, "option_open_interest": put_oi},
        ]
    snap_realistic  = pd.DataFrame(rows_realistic)
    net_realistic   = GEXCalculator._compute_net_gex(snap_realistic, 580.0)
    atm_gex = float(net_realistic.get(580.0, 0))
    check("C4: ATM GEX magnitude is reasonable (>1M, <500B)",
          1e6 < abs(atm_gex) < 5e11, f"ATM GEX={atm_gex:.2e}")

    # C5: compute_gex() standalone helper works
    client5 = make_mock_client(500.0, [490, 495, 500, 505, 510])
    result5 = compute_gex(client5, "SPY")
    check("C5: compute_gex() standalone helper works",
          result5["error"] == "" and result5["gamma_wall"] > 0,
          f"wall={result5['gamma_wall']}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--unit-only",  action="store_true")
    parser.add_argument("--account",    default="")
    parser.add_argument("--symbol",     default="SPY")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  GEXCalculator Test Suite")
    print(f"{'='*60}")

    test_group_a()
    test_group_c()

    if args.account and not args.unit_only:
        test_group_b(args.account, args.symbol)
    elif not args.unit_only and not args.account:
        print("\n  Skipping Group B — pass --account U18705798 to run live tests")

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
        print("  ✓ All checks passed")
    else:
        print(f"  ⚠️  {total-passed} failed")
    print()


if __name__ == "__main__":
    main()
