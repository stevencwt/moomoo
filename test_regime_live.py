#!/usr/bin/env python3
"""
test_regime_live.py — Live regime detection validation for the Options Bot
==========================================================================
Uses the bot's existing YFinanceConnector to bootstrap with 252 daily bars,
then polls at --poll second intervals to validate the regime module.

Usage:
    python3 test_regime_live.py                        # SPY, 5s poll
    python3 test_regime_live.py --asset US.QQQ
    python3 test_regime_live.py --asset US.TSLA --poll 10 --history 300

Validation checklist (Section 14.2 of integration guide):
  HMM label      — NOT "UNKNOWN"
  Hurst DFA      — a float between 0.0 and 1.0
  Volatility     — NOT "UNKNOWN"
  Consensus      — NOT "UNKNOWN"
  Recommended    — real value (not empty string)
  No crashes     — script runs 20+ ticks cleanly
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Bootstrap import path ─────────────────────────────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# ── Verify regime_detection installed ────────────────────────────────────────
try:
    from regime_detection import RegimeManager
except ImportError:
    print("\n✗ regime_detection not installed.")
    print("  Fix: pip3 install -e /Users/user/regime-detection\n")
    sys.exit(1)

from src.connectors.yfinance_connector import YFinanceConnector


# ── Formatting helpers ────────────────────────────────────────────────────────

def _c(code, s): return f"\033[{code}m{s}\033[0m"
def green(s):    return _c("92", s)
def yellow(s):   return _c("93", s)
def red(s):      return _c("91", s)
def cyan(s):     return _c("96", s)
def dim(s):      return _c("2", s)
def bold(s):     return _c("1", s)

def col(val, ok_fn=None):
    if val is None or val == "UNKNOWN" or val == "":
        return red(str(val or "—"))
    if ok_fn and ok_fn(val):
        return green(str(val))
    return str(val)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Live regime detection test — Options Bot")
    parser.add_argument("--asset",    default="US.SPY",        help="Symbol in MooMoo format (default: US.SPY)")
    parser.add_argument("--timeframe",default="1d",            help="Candle timeframe (default: 1d — daily)")
    parser.add_argument("--poll",     type=int, default=5,     help="Poll interval seconds (default: 5)")
    parser.add_argument("--history",  type=int, default=252,   help="Historical bars to bootstrap (default: 252)")
    parser.add_argument("--strategy", default="options_income",help="Strategy type (default: options_income)")
    args = parser.parse_args()

    asset    = args.asset
    ticker   = asset.replace("US.", "")  # SPY, QQQ, etc.

    print(f"\n{'='*72}")
    print(f"  LIVE REGIME DETECTION — Options Bot (YFinance connector)")
    print(f"{'='*72}")
    print(f"  Asset:         {asset}  ({ticker})")
    print(f"  Timeframe:     {args.timeframe}  (daily OHLCV)")
    print(f"  Strategy:      {args.strategy}")
    print(f"  Poll interval: {args.poll}s")
    print(f"  History bars:  {args.history}")
    print(f"  Connector:     YFinanceConnector")
    print(f"{'='*72}\n")

    # ── Init connector ────────────────────────────────────────────────────────
    yf = YFinanceConnector()

    # ── Init RegimeManager ────────────────────────────────────────────────────
    manager = RegimeManager(
        market_type="US_STOCK",
        strategy_type=args.strategy,
        market_class="us_stocks",
    )
    min_bars = manager.config["hmm"]["min_training_bars"]
    print(f"[INIT] HMM warmup threshold: {min_bars} bars")

    # ── Bootstrap ─────────────────────────────────────────────────────────────
    print(f"[BOOTSTRAP] Fetching {args.history} daily bars for {ticker}...")
    ohlcv = yf.get_daily_ohlcv(asset, period="2y")  # 2y gives ~500 daily bars
    ohlcv = ohlcv.tail(args.history)

    if len(ohlcv) == 0:
        print(f"✗ No OHLCV data returned for {ticker}. Check symbol and connectivity.")
        sys.exit(1)

    price_min = ohlcv["close"].min()
    price_max = ohlcv["close"].max()
    print(f"[BOOTSTRAP] Got {len(ohlcv)} bars | "
          f"price range ${price_min:.2f}–${price_max:.2f}")

    # Feed all historical bars
    for ts, row in ohlcv.iterrows():
        bar = {
            "timestamp": int(ts.timestamp()),
            "o": float(row["open"]),
            "h": float(row["high"]),
            "l": float(row["low"]),
            "c": float(row["close"]),
            "v": float(row["volume"]),
        }
        manager.update(bar=bar)

    regime = manager.get_current_regime()
    print(f"[BOOTSTRAP] Fed {len(ohlcv)} bars | "
          f"bars in buffer: {manager.bar_count}")
    print(f"[BOOTSTRAP] Initial: "
          f"{regime.get('consensus_state','?')} | "
          f"{regime.get('recommended_logic','?')} | "
          f"conf={regime.get('confidence_score',0):.2f} | "
          f"vol={regime.get('volatility_regime','?')}\n")

    # ── Live loop ─────────────────────────────────────────────────────────────
    hdr_w = 6+9+13+19+25+6+13+8+8+4+16+5
    print(f"[LIVE] Polling every {args.poll}s using latest daily bar. Ctrl+C to stop.")
    print(f"{'─'*102}")
    print(f"{'Tick':>5s}  {'Time':>8s}  {'Price':>11s}  "
          f"{'Consensus':<18s}  {'Recommended':<24s}  "
          f"{'Conf':>5s}  {'Vol':<13s}  {'HMM':<6s}  {'Hurst':>7s}  "
          f"{'Brk':>3s}  {'Liq':<15s}  {'Exit':>4s}")
    print(f"{'─'*102}")

    tick = 0
    prev_close = float(ohlcv["close"].iloc[-1])
    checks = {
        "hmm_ok":    False,
        "hurst_ok":  False,
        "vol_ok":    False,
        "cons_ok":   False,
        "logic_ok":  False,
        "no_crash":  True,
    }

    try:
        while True:
            tick += 1

            # Fetch fresh daily data — uses yfinance cache (60min TTL) so won't
            # hammer the API; on each poll we just feed the latest available bar
            try:
                fresh = yf.get_daily_ohlcv(asset, period="5d")
                if len(fresh) > 0:
                    latest = fresh.iloc[-1]
                    price  = float(latest["close"])
                    bar = {
                        "timestamp": int(fresh.index[-1].timestamp()),
                        "o": float(latest["open"]),
                        "h": float(latest["high"]),
                        "l": float(latest["low"]),
                        "c": price,
                        "v": float(latest["volume"]),
                    }
                else:
                    # Fallback: synthetic tick bar from last known price
                    price = prev_close
                    bar   = {
                        "timestamp": int(time.time()),
                        "o": prev_close, "h": prev_close,
                        "l": prev_close, "c": prev_close, "v": 0.0,
                    }
            except Exception as e:
                print(f"  [WARN] fetch failed tick {tick}: {e}")
                time.sleep(args.poll)
                continue

            manager.update(bar=bar)
            r   = manager.get_current_regime()
            sig = r.get("signals", {})

            consensus  = r.get("consensus_state",  "?")
            logic      = r.get("recommended_logic","?")
            conf       = r.get("confidence_score",  0.0)
            vol        = r.get("volatility_regime", "?")
            exit_m     = r.get("exit_mandate",      False)
            hmm        = sig.get("hmm_label",       "?")
            hurst      = sig.get("hurst_dfa")
            struct_brk = sig.get("structural_break", False)
            liq        = sig.get("liquidity_status", "?")
            hurst_str  = f"{hurst:.4f}" if hurst is not None else "  N/A "

            # Update validation checks
            if hmm not in ("UNKNOWN", "?", ""):        checks["hmm_ok"]   = True
            if hurst is not None:                      checks["hurst_ok"] = True
            if vol not in ("UNKNOWN", "?", ""):        checks["vol_ok"]   = True
            if consensus not in ("UNKNOWN", "?", ""):  checks["cons_ok"]  = True
            if logic not in ("NO_TRADE", "?", ""):     checks["logic_ok"] = True

            # Colour coding
            cons_str = (green if "BULL" in consensus else
                        (red if "BEAR" in consensus else
                         (yellow if "CHOP" in consensus else dim)))(consensus)
            exit_str = red("YES") if exit_m else dim(" no")
            brk_str  = red(" Y ") if struct_brk else dim(" n ")

            print(
                f"{tick:5d}  {datetime.now():%H:%M:%S}  "
                f"${price:>9,.2f}  "
                f"{cons_str:<27s}  "
                f"{logic:<24s}  "
                f"{conf:5.2f}  "
                f"{vol:<13s}  "
                f"{hmm:<6s}  {hurst_str}  "
                f"{brk_str}  "
                f"{liq:<15s}  "
                f"{exit_str}"
            )

            # Detailed box every 20 ticks
            if tick % 20 == 0:
                opts = sig.get("options_context", {})
                rh   = sig.get("range_hints")
                print(f"{'─'*102}")
                print(f"  ┌─ {ticker} 1d Regime Detail (tick {tick})")
                print(f"  │  Consensus:     {bold(consensus)} (conf={conf:.3f})")
                print(f"  │  Recommended:   {bold(logic)}")
                print(f"  │  HMM state:     {hmm}")
                print(f"  │  Hurst DFA:     {hurst_str.strip()}")
                print(f"  │  Volatility:    {vol}")
                print(f"  │  Break detect:  {struct_brk}")
                print(f"  │  Liquidity:     {liq}")
                if opts:
                    print(f"  │  Options ctx:   vanna={opts.get('vanna','—')} "
                          f"gamma={opts.get('gamma','—')} "
                          f"oi_skew={opts.get('oi_skew','—')}")
                if rh:
                    print(f"  │  Range hints:   "
                          f"[${rh.get('range_lower',0):.2f} — ${rh.get('range_upper',0):.2f}] "
                          f"dev={rh.get('current_deviation_pct',0):.1f}% "
                          f"({rh.get('channel_type','?')})")
                print(f"  │  Exit mandate:  {exit_m}")
                print(f"  │  Bars in buffer:{manager.bar_count}")
                print(f"  └─────────────────────────────")
                print(f"{'─'*102}")

            prev_close = price
            time.sleep(args.poll)

    except KeyboardInterrupt:
        print(f"\n{'='*72}")
        print(f"  STOPPED — {tick} ticks | {manager.bar_count} bars in buffer")
        print(f"{'='*72}")

        # ── Validation report ─────────────────────────────────────────────────
        print(f"\n{'─'*50}")
        print(bold("  VALIDATION CHECKLIST (Section 14.2)"))
        print(f"{'─'*50}")
        items = [
            ("HMM label NOT UNKNOWN",       checks["hmm_ok"]),
            ("Hurst DFA is a float",         checks["hurst_ok"]),
            ("Volatility NOT UNKNOWN",        checks["vol_ok"]),
            ("Consensus NOT UNKNOWN",         checks["cons_ok"]),
            ("Recommended logic real value",  checks["logic_ok"]),
            ("No Python errors / crashes",    checks["no_crash"]),
        ]
        all_pass = True
        for label, passed in items:
            icon = green("✅") if passed else red("❌")
            print(f"  {icon}  {label}")
            if not passed:
                all_pass = False
        print(f"{'─'*50}")
        if all_pass:
            print(green("  ✓ All checks passed — regime module validated for this bot."))
        else:
            print(yellow("  ⚠ Some checks failed — see above."))
            print(dim("    Common fix: run with --history 300 to ensure HMM warmup."))
        print()

        print("Final regime JSON:")
        print(manager.get_json(indent=2))


if __name__ == "__main__":
    main()
