#!/usr/bin/env python3
"""
test_llm_regime_live.py — Live test for LLM regime bridge (US stocks)
======================================================================
Validates that llm_regime_bridge.py works correctly with real yfinance
daily OHLCV data for a US stock symbol.

Usage:
    python3 test_llm_regime_live.py
    python3 test_llm_regime_live.py --symbol US.QQQ
    python3 test_llm_regime_live.py --symbol US.TSLA --provider anthropic
"""

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# ── Verify prerequisites ──────────────────────────────────────────────────────
print("\nChecking prerequisites...")

try:
    from llm_regime import RegimeAnalyzer
    print("  ✓ llm_regime installed")
except ImportError:
    print("  ✗ llm_regime not installed")
    print('    Fix: pip3 install -e "/Users/user/llm-regime[google]"')
    sys.exit(1)

try:
    from src.market.llm_regime_bridge import LLMRegimeBridge, llm_direction_to_regime_hint
    print("  ✓ llm_regime_bridge imported")
except ImportError as e:
    print(f"  ✗ llm_regime_bridge import failed: {e}")
    sys.exit(1)

try:
    from src.connectors.yfinance_connector import YFinanceConnector
    print("  ✓ YFinanceConnector imported")
except ImportError as e:
    print(f"  ✗ YFinanceConnector import failed: {e}")
    sys.exit(1)

import os
if not os.environ.get("GOOGLE_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY"):
    print("  ✗ No API key found in environment")
    print("    Fix: export GOOGLE_API_KEY='your_key_here'")
    sys.exit(1)
print("  ✓ API key found")


def main():
    parser = argparse.ArgumentParser(description="LLM regime bridge live test")
    parser.add_argument("--symbol",   default="US.SPY",  help="Symbol (default: US.SPY)")
    parser.add_argument("--provider", default="gemini",  help="LLM provider (default: gemini)")
    args = parser.parse_args()

    symbol = args.symbol
    ticker = symbol.replace("US.", "")

    print(f"\n{'='*60}")
    print(f"  LLM REGIME BRIDGE — Live Test")
    print(f"{'='*60}")
    print(f"  Symbol:   {symbol} ({ticker})")
    print(f"  Provider: {args.provider}")
    print(f"  HTF:      100 daily bars (~5 months) — macro trend")
    print(f"  LTF:      120 hourly bars, window=60 (~1.5 weeks intraday)")
    print(f"{'='*60}\n")

    # ── Fetch OHLCV ──────────────────────────────────────────────────────────
    print(f"[1/3] Fetching 2y daily OHLCV for {ticker} via yfinance...")
    yf = YFinanceConnector()
    ohlcv = yf.get_daily_ohlcv(symbol, period="2y")

    if len(ohlcv) == 0:
        print(f"  ✗ No data returned for {symbol}")
        sys.exit(1)

    print(f"  ✓ Got {len(ohlcv)} bars | "
          f"${ohlcv['close'].min():.2f}–${ohlcv['close'].max():.2f} | "
          f"latest close: ${ohlcv['close'].iloc[-1]:.2f}")

    # ── Init bridge with a short interval so it fires immediately ────────────
    print(f"\n[2/3] Initialising LLMRegimeBridge...")
    bridge = LLMRegimeBridge(
        symbol=symbol,
        yfinance=yf,
        provider=args.provider,
        htf_interval_secs=1,       # fire immediately; small value avoids stale=True
        ltf_interval_secs=1,       # fire immediately
        min_confidence=2,          # lower threshold for test
    )

    if not bridge.enabled:
        print(f"  ✗ Bridge not enabled — check installation and API key")
        sys.exit(1)
    print(f"  ✓ Bridge initialized")

    # ── Trigger analysis ─────────────────────────────────────────────────────
    print(f"\n[3/3] Triggering LLM analysis (this takes 5–15s per call)...")
    print(f"  Calling maybe_update() — spawns background thread...")
    t_start = time.time()
    bridge.maybe_update(ohlcv)
    print(f"  ✓ maybe_update() returned in {(time.time()-t_start)*1000:.0f}ms (non-blocking)")

    # Wait for background thread to complete (both HTF + LTF)
    print(f"  Waiting for LLM calls to complete", end="", flush=True)
    max_wait = 60   # seconds
    waited   = 0
    while waited < max_wait:
        time.sleep(2)
        waited += 2
        print(".", end="", flush=True)
        # Both results available = done
        if bridge.htf is not None and bridge.ltf is not None:
            break

    print(f"\n  Waited {waited}s total\n")

    # ── Print results ─────────────────────────────────────────────────────────
    htf = bridge.htf
    ltf = bridge.ltf
    direction = bridge.direction
    stale = bridge.is_stale

    print(f"{'─'*60}")
    print(f"  RESULTS")
    print(f"{'─'*60}")

    if htf:
        print(f"  HTF (macro ~5 months):")
        print(f"    Regime:         {htf.regime}")
        print(f"    Confidence:     {htf.confidence}/5")
        print(f"    Bias:           {htf.bias}")
        print(f"    Direction:      {htf.scalp_direction}")
        print(f"    Trend strength: {htf.trend_strength}")
        if htf.key_levels:
            print(f"    Key levels:     {len(htf.key_levels)} detected")
        print(f"    Reasoning:      {htf.reasoning[:120]}...")
        print(f"    Cost:           ${htf.cost_usd:.4f} | {htf.latency_ms}ms")
    else:
        print(f"  HTF: ✗ No result received")

    print()

    if ltf:
        print(f"  LTF (hourly — ~1.5 week intraday focus):")
        print(f"    Regime:         {ltf.regime}")
        print(f"    Confidence:     {ltf.confidence}/5")
        print(f"    Bias:           {ltf.bias}")
        print(f"    Direction:      {ltf.scalp_direction}")
        print(f"    Trend strength: {ltf.trend_strength}")
        print(f"    Cost:           ${ltf.cost_usd:.4f} | {ltf.latency_ms}ms")
    else:
        print(f"  LTF: ✗ No result received")

    print()
    print(f"  Combined direction:  {direction}")
    regime_hint = llm_direction_to_regime_hint(direction)
    print(f"  → Bot regime hint:   {regime_hint or 'defer to quant'}")
    print(f"  Stale:               {stale}")

    # ── Validation checklist ──────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"  VALIDATION CHECKLIST")
    print(f"{'─'*60}")

    checks = [
        ("HTF result received",             htf is not None),
        ("LTF result received",             ltf is not None),
        ("HTF regime not UNKNOWN",          htf is not None and htf.regime != "UNKNOWN"),
        ("LTF regime not UNKNOWN",          ltf is not None and ltf.regime != "UNKNOWN"),
        ("HTF confidence ≥ 1",              htf is not None and htf.confidence >= 1),
        ("LTF confidence ≥ 1",              ltf is not None and ltf.confidence >= 1),
        ("Direction is valid value",        direction in ("LONG_ONLY","SHORT_ONLY","BOTH","WAIT","NO_TRADE")),
        ("Not stale after fresh call",      not stale),
        ("No crashes",                      True),
    ]

    all_pass = True
    for label, passed in checks:
        icon = "✅" if passed else "❌"
        print(f"  {icon}  {label}")
        if not passed:
            all_pass = False

    print(f"{'─'*60}")
    if all_pass:
        print(f"  ✓ All checks passed — LLM bridge validated for US stocks.")
    else:
        print(f"  ⚠ Some checks failed — see above.")

    # Daily cost estimate
    if htf and ltf:
        cost_per_day = (htf.cost_usd + ltf.cost_usd) * 8   # 8 symbols
        print(f"\n  Est. daily cost (8 symbols): ${cost_per_day:.4f}")
        print(f"  Est. monthly cost:           ${cost_per_day*21:.3f} (21 trading days)")

    print()


if __name__ == "__main__":
    main()
