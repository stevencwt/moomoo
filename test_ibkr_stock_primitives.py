#!/usr/bin/env python3
"""
test_ibkr_stock_primitives.py — IBKR Stock Trading Primitives Test
===================================================================
Tests all stock order primitives against the LIVE account U18705798.

Read-only tests:
  1. Connection
  2. Account info
  3. Stock positions
  4. Spot price (AAPL)

Order tests (LIVE account — real but safe):
  5. Place market order BUY 1 share AAPL  ← fills immediately at market
  6. Confirm position shows 1 share AAPL
  7. Place limit order SELL 1 share AAPL at 10x market price (will NOT fill)
  8. Confirm order is PENDING
  9. Cancel limit order
  10. Close position with market order SELL 1 share AAPL
  11. Confirm position back to 0 shares

Stop order tests (read-only validation):
  12. Place stop-loss order (GTC) at 50% below market — extremely safe
  13. Confirm stop order accepted
  14. Cancel stop order

SAFETY:
  Test 5 buys 1 share of AAPL at market (~$220). This costs real money
  (~$220) and is immediately closed in Test 10 at market. Net cost is
  only the round-trip commission (~$1 total). This is intentional —
  the only way to truly validate stock execution is to place a real order.

Usage:
  python3 test_ibkr_stock_primitives.py
  python3 test_ibkr_stock_primitives.py --symbol US.AAPL
  python3 test_ibkr_stock_primitives.py --skip-order   # read-only only
"""

import argparse
import sys
import time
import math
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))


# ── Prerequisites ─────────────────────────────────────────────────────────────
print("\nChecking prerequisites...")

try:
    import yaml
    with open("config/config.yaml") as f:
        config = yaml.safe_load(f)
    print("  ✓ config.yaml loaded")
except Exception as e:
    print(f"  ✗ config.yaml: {e}")
    sys.exit(1)

try:
    from src.logger import get_logger, setup_logger
    setup_logger(config)
    logger = get_logger("test.ibkr_stock")
    print("  ✓ Logger initialised")
except Exception as e:
    print(f"  ✗ Logger: {e}")
    sys.exit(1)

try:
    from src.connectors.ibkr_connector import IBKRConnector
    print("  ✓ IBKRConnector imported")
except Exception as e:
    print(f"  ✗ IBKRConnector: {e}")
    sys.exit(1)

ibkr_cfg = config.get("ibkr", {})
PORT     = ibkr_cfg.get("port", 7496)
ACCOUNT  = ibkr_cfg.get("account", "?")
config["ibkr"]["client_id"] = 2   # avoid conflict with running bot
print(f"  ✓ Target: {ACCOUNT} on port {PORT} (client_id=2)")


# ── Helpers ───────────────────────────────────────────────────────────────────
results = []

def check(label, passed, detail=""):
    icon = "✅" if passed else "❌"
    suffix = f"  [{detail}]" if detail else ""
    print(f"  {icon}  {label}{suffix}")
    results.append((label, passed))
    return passed

def section(title):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol",     default="US.AAPL",
                        help="Stock symbol (default: US.AAPL)")
    parser.add_argument("--skip-order", action="store_true",
                        help="Read-only — skip all order placement")
    args   = parser.parse_args()
    symbol = args.symbol
    ticker = symbol.replace("US.", "")

    print(f"\n{'='*60}")
    print(f"  IBKR STOCK PRIMITIVE TEST  |  Live Account")
    print(f"{'='*60}")
    print(f"  Account  : {ACCOUNT}")
    print(f"  Port     : {PORT}")
    print(f"  Symbol   : {symbol}")
    print(f"  Mode     : {'READ-ONLY' if args.skip_order else 'READ + ORDER TEST'}")
    print(f"{'='*60}")

    if not args.skip_order:
        print(f"\n  ⚠️  ORDER TEST will BUY 1 share of {ticker} at market price (~$220)")
        print(f"     then immediately SELL it back at market.")
        print(f"     Net cost: ~$1 commission only. Real money on {ACCOUNT}.")
        print(f"     Proceed? (yes/no): ", end="")
        if input().strip().lower() != "yes":
            print("  Aborted. Use --skip-order for read-only.")
            sys.exit(0)

    ibkr  = IBKRConnector(config)
    spot  = 0.0
    buy_order_id  = None
    stop_order_id = None

    try:
        # ── 1. Connection ─────────────────────────────────────────
        section("1 · Connection")
        try:
            ibkr.connect()
            check("Connected to IBKR TWS", ibkr.is_connected(),
                  f"port={PORT} account={ACCOUNT}")
        except Exception as e:
            check("Connected", False, str(e))
            print("\n  Cannot proceed — is TWS running?")
            sys.exit(1)

        # ── 2. Account Info ───────────────────────────────────────
        section("2 · Account Info")
        info = ibkr.get_account_info()
        check("get_account_info() returned", bool(info))
        check("NetLiquidation > 0",
              info.get("total_assets", 0) > 0,
              f"${info.get('total_assets', 0):,.2f}")
        print(f"\n    Net Liquidation : ${info.get('total_assets', 0):>12,.2f}")
        print(f"    Cash            : ${info.get('cash', 0):>12,.2f}")

        # ── 3. Stock Positions ────────────────────────────────────
        section("3 · Stock Positions")
        positions = ibkr.get_stock_positions()
        check("get_stock_positions() returned DataFrame",
              positions is not None,
              f"{len(positions)} positions")
        if len(positions) > 0:
            print(f"\n    Current stock positions:")
            for _, row in positions.iterrows():
                print(f"      {row['symbol']:<12} qty={row['qty']:>6}  "
                      f"avg_cost=${row['avg_cost']:.2f}")
        else:
            print(f"\n    No stock positions currently held")

        # ── 4. Spot Price ─────────────────────────────────────────
        section(f"4 · Spot Price ({symbol})")
        import yfinance as _yf
        try:
            spot = ibkr.get_spot_price(symbol)
            if not spot or math.isnan(spot) or spot <= 0:
                raise ValueError("IBKR returned invalid price")
            check("get_spot_price() via IBKR", True, f"${spot:.2f}")
        except Exception:
            check("get_spot_price() via IBKR", False,
                  "no subscription — using yfinance")
            try:
                _hist = _yf.download(ticker, period="1d", interval="1m",
                                     progress=False, auto_adjust=True)
                if len(_hist) == 0:
                    _hist = _yf.download(ticker, period="5d", interval="1d",
                                         progress=False, auto_adjust=True)
                spot = float(_hist["Close"].iloc[-1].iloc[0]
                             if hasattr(_hist["Close"].iloc[-1], "iloc")
                             else _hist["Close"].iloc[-1])
                check("get_spot_price() via yfinance", spot > 0, f"${spot:.2f}")
            except Exception as e2:
                check("get_spot_price() via yfinance", False, str(e2))

        if args.skip_order or spot <= 0:
            if args.skip_order:
                section("ORDER TESTS — SKIPPED (--skip-order)")
            else:
                section("ORDER TESTS — SKIPPED (spot price unavailable)")
            return

        # ── 5. Market BUY ─────────────────────────────────────────
        section(f"5 · Place Market BUY Order (1 share {ticker})")
        print(f"\n    Buying 1 share of {ticker} at market (~${spot:.2f})")
        print(f"    Will be sold back immediately in Test 10")
        try:
            buy_order_id = ibkr.place_stock_market_order(
                symbol=symbol, qty=1, direction="BUY"
            )
            check("place_stock_market_order() returned order_id",
                  bool(buy_order_id), f"id={buy_order_id}")
            time.sleep(3)   # allow fill
            status = ibkr.get_order_status(buy_order_id)
            check("Market BUY filled",
                  status.get("status") == "FILLED",
                  f"status={status.get('status','?')} "
                  f"fill=${status.get('filled_price', 0):.2f}")
        except Exception as e:
            check("place_stock_market_order()", False, str(e))

        # ── 6. Confirm Position ───────────────────────────────────
        section(f"6 · Confirm Stock Position ({ticker})")
        time.sleep(2)
        positions = ibkr.get_stock_positions()
        held = next((r for _, r in positions.iterrows()
                     if ticker in r["symbol"]), None)
        check(f"{ticker} position shows 1 share",
              held is not None and int(held["qty"]) >= 1,
              f"qty={int(held['qty']) if held is not None else 0}")

        # ── 7. Limit SELL at 10x market (won't fill) ──────────────
        section(f"7 · Place Limit SELL at 10x Market (will NOT fill)")
        safe_limit = round(spot * 10, 2)
        print(f"\n    Limit price: ${safe_limit:.2f} (10x market — will never fill)")
        limit_order_id = None
        try:
            limit_order_id = ibkr.place_stock_limit_order(
                symbol=symbol, qty=1, price=safe_limit,
                direction="SELL", tif="DAY"
            )
            check("place_stock_limit_order() returned order_id",
                  bool(limit_order_id), f"id={limit_order_id}")
        except Exception as e:
            check("place_stock_limit_order()", False, str(e))

        # ── 8. Confirm Limit PENDING ──────────────────────────────
        section("8 · Confirm Limit Order PENDING")
        if limit_order_id:
            time.sleep(2)
            try:
                status = ibkr.get_order_status(limit_order_id)
                check("Limit order is PENDING (not filled)",
                      status.get("status") == "PENDING",
                      status.get("status", "?"))
                check("Filled qty = 0",
                      status.get("filled_qty", 0) == 0,
                      f"filled={status.get('filled_qty', 0)}")
            except Exception as e:
                check("get_order_status() for limit", False, str(e))

        # ── 9. Cancel Limit Order ─────────────────────────────────
        section("9 · Cancel Limit Order")
        if limit_order_id:
            try:
                cancelled = ibkr.cancel_order(limit_order_id)
                check("cancel_order() returned True", cancelled,
                      f"id={limit_order_id}")
                time.sleep(2)
            except Exception as e:
                check("cancel_order()", False, str(e))

        # ── 10. Close Position (Market SELL) ──────────────────────
        section(f"10 · Close Position — Market SELL 1 share {ticker}")
        print(f"\n    Selling the 1 share bought in Test 5 back at market")
        try:
            close_id = ibkr.close_stock_position(
                symbol=symbol, qty=1, order_type="MKT"
            )
            check("close_stock_position() returned order_id",
                  bool(close_id), f"id={close_id}")
            time.sleep(3)
            status = ibkr.get_order_status(close_id)
            check("Close order filled",
                  status.get("status") == "FILLED",
                  f"status={status.get('status','?')} "
                  f"fill=${status.get('filled_price', 0):.2f}")
        except Exception as e:
            check("close_stock_position()", False, str(e))

        # ── 11. Confirm Position Back to 0 ────────────────────────
        section(f"11 · Confirm Position Closed ({ticker})")
        time.sleep(2)
        positions = ibkr.get_stock_positions()
        held = next((r for _, r in positions.iterrows()
                     if ticker in r["symbol"]), None)
        check(f"{ticker} position back to 0 shares",
              held is None or int(held["qty"]) == 0,
              f"qty={int(held['qty']) if held is not None else 0}")

        # ── 12. Stop Order Test ───────────────────────────────────
        section(f"12 · Place Stop-Loss Order (GTC, 50% below market)")
        stop_price = round(spot * 0.50, 2)
        print(f"\n    Stop price: ${stop_price:.2f} (50% below market — "
              f"will never trigger in normal conditions)")
        try:
            stop_order_id = ibkr.place_stock_stop_order(
                symbol=symbol, qty=1,
                stop_price=stop_price,
                direction="SELL", tif="GTC"
            )
            check("place_stock_stop_order() returned order_id",
                  bool(stop_order_id), f"id={stop_order_id}")
        except Exception as e:
            check("place_stock_stop_order()", False, str(e))
            print(f"    Note: stop orders may require an open position")

        # ── 13. Confirm Stop Accepted ─────────────────────────────
        section("13 · Confirm Stop Order Accepted")
        if stop_order_id:
            time.sleep(2)
            try:
                status = ibkr.get_order_status(stop_order_id)
                check("Stop order accepted (PENDING)",
                      status.get("status") == "PENDING",
                      status.get("status", "?"))
            except Exception as e:
                check("get_order_status() for stop", False, str(e))

        # ── 14. Cancel Stop Order ─────────────────────────────────
        section("14 · Cancel Stop Order")
        if stop_order_id:
            try:
                cancelled = ibkr.cancel_order(stop_order_id)
                check("cancel_order() for stop returned True",
                      cancelled, f"id={stop_order_id}")
                time.sleep(2)
            except Exception as e:
                check("cancel_order() for stop", False, str(e))
                print(f"  ⚠️  Please manually cancel order {stop_order_id} in TWS")

    except KeyboardInterrupt:
        print("\n\n  Interrupted — check TWS for any open orders")
        if stop_order_id:
            print(f"  ⚠️  Manually cancel stop order {stop_order_id} in TWS")
    except Exception as e:
        print(f"\n  ✗ Unexpected error: {e}")
        import traceback; traceback.print_exc()
    finally:
        ibkr.disconnect()
        print("\n  Disconnected from IBKR")

    # ── Summary ───────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")
    passed = sum(1 for _, p in results if p)
    total  = len(results)
    for label, p in results:
        print(f"  {'✅' if p else '❌'}  {label}")
    print(f"{'─'*60}")
    if total > 0:
        print(f"  {passed}/{total} passed ({passed/total*100:.0f}%)")
        if passed == total:
            print(f"  ✓ All stock trading primitives validated on live account")
        else:
            print(f"  ⚠️  {total-passed} check(s) failed — review above")
    print()


if __name__ == "__main__":
    main()
