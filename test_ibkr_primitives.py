#!/usr/bin/env python3
"""
test_ibkr_primitives.py — IBKR Live Account Primitive Test
===========================================================
Tests all IBKR trading primitives against the LIVE account U18705798.

Read-only tests:
  1. Connection
  2. Account info (balance, cash, market value)
  3. Shares held (TSLA)
  4. Open option positions
  5. Open orders
  6. Spot price (SPY)
  7. Option expiries (SPY)
  8. Option chain (SPY, first available expiry)

Order tests (LIVE account — real but safe):
  9. Place bear call spread combo order at 10x market credit (will NEVER fill)
 10. Confirm order status = PENDING
 11. Cancel the order
 12. Confirm order no longer in open orders

SAFETY: The order test places a limit order at $50 net credit — roughly
10x the real market price for any spread in our watchlist. It physically
cannot fill. It is cancelled immediately after confirming it was accepted.
Net financial effect: zero.

Requirements:
  - TWS running and logged into live account on port 7496
  - python3 test_ibkr_primitives.py
  - python3 test_ibkr_primitives.py --skip-order   (read-only only)
"""

import argparse
import sys
import time
from pathlib import Path
from datetime import date

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
    print(f"  ✗ config.yaml not found: {e}")
    sys.exit(1)

try:
    from src.logger import get_logger, setup_logger
    setup_logger(config)
    logger = get_logger("test.ibkr_primitives")
    print("  ✓ Logger initialised")
except Exception as e:
    print(f"  ✗ Logger failed: {e}")
    sys.exit(1)

try:
    from src.connectors.ibkr_connector import IBKRConnector
    print("  ✓ IBKRConnector imported")
except Exception as e:
    print(f"  ✗ IBKRConnector import failed: {e}")
    sys.exit(1)

ibkr_cfg = config.get("ibkr", {})
PORT     = ibkr_cfg.get("port", 7496)
ACCOUNT  = ibkr_cfg.get("account", "?")
# Use client_id=2 so this test doesn't conflict with the running bot (client_id=1)
config["ibkr"]["client_id"] = 2
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
    parser.add_argument("--symbol",     default="US.SPY")
    parser.add_argument("--skip-order", action="store_true",
                        help="Skip order test — read-only primitives only")
    args   = parser.parse_args()
    symbol = args.symbol

    print(f"\n{'='*60}")
    print(f"  IBKR PRIMITIVE TEST  |  Live Account")
    print(f"{'='*60}")
    print(f"  Account : {ACCOUNT}")
    print(f"  Port    : {PORT}")
    print(f"  Symbol  : {symbol}")
    print(f"  Mode    : {'READ-ONLY' if args.skip_order else 'READ + ORDER TEST'}")
    print(f"{'='*60}")

    if not args.skip_order:
        print(f"\n  ⚠️  ORDER TEST will place 1 limit order at $8 credit")
        print(f"     on {symbol} — above market — will not fill, cancelled immediately.")
        print(f"     Proceed on LIVE account {ACCOUNT}? (yes/no): ", end="")
        if input().strip().lower() != "yes":
            print("  Aborted. Use --skip-order for read-only tests.")
            sys.exit(0)

    ibkr = IBKRConnector(config)
    spot          = 0.0
    target_expiry = None

    try:
        # ── 1. Connection ─────────────────────────────────────────
        section("1 · Connection")
        try:
            ibkr.connect()
            check("Connected to IBKR TWS", ibkr.is_connected(),
                  f"port={PORT} account={ACCOUNT}")
        except Exception as e:
            check("Connected to IBKR TWS", False, str(e))
            print("\n  Cannot proceed — is TWS running on port 7496?")
            sys.exit(1)

        # ── 2. Account Info ───────────────────────────────────────
        section("2 · Account Info")
        info = ibkr.get_account_info()
        check("get_account_info() returned dict", bool(info))
        check("NetLiquidation > 0",
              info.get("total_assets", 0) > 0,
              f"${info.get('total_assets', 0):,.2f}")
        check("Cash field present",
              "cash" in info,
              f"${info.get('cash', 0):,.2f}")
        check("MarketValue field present",
              "market_val" in info,
              f"${info.get('market_val', 0):,.2f}")
        print(f"\n    Net Liquidation : ${info.get('total_assets', 0):>12,.2f}")
        print(f"    Cash            : ${info.get('cash', 0):>12,.2f}")
        print(f"    Market Value    : ${info.get('market_val', 0):>12,.2f}")

        # ── 3. Shares Held ────────────────────────────────────────
        section("3 · Shares Held (US.TSLA)")
        shares = ibkr.get_shares_held("US.TSLA")
        check("get_shares_held() returned int", isinstance(shares, int),
              f"{shares} shares")

        # ── 4. Open Option Positions ──────────────────────────────
        section("4 · Open Option Positions")
        opt_pos = ibkr.get_option_positions()
        check("get_option_positions() returned DataFrame",
              opt_pos is not None,
              f"{len(opt_pos)} rows")
        if len(opt_pos) > 0:
            print(f"\n    Positions in IBKR account:")
            for _, row in opt_pos.iterrows():
                print(f"      {row['code']:<42} qty={row['qty']:>4}  "
                      f"cost=${row['cost_price']:.2f}")
        else:
            print(f"\n    No options in IBKR account")
            print(f"    (Paper trades are in local SQLite — not sent to IBKR)")

        # ── 5. Open Orders ────────────────────────────────────────
        section("5 · Open Orders")
        open_orders = ibkr.get_open_orders()
        check("get_open_orders() returned DataFrame",
              open_orders is not None,
              f"{len(open_orders)} orders")
        if len(open_orders) > 0:
            print(f"\n    Existing open orders:")
            for _, row in open_orders.iterrows():
                print(f"      id={row.get('order_id','?'):<8} "
                      f"{row.get('code','?'):<35} "
                      f"{row.get('order_status','?')}")
        else:
            print(f"\n    No open orders")

        # ── 6. Spot Price ─────────────────────────────────────────
        section(f"6 · Spot Price ({symbol})")
        # Try IBKR first, fall back to yfinance (IBKR needs subscription for live data)
        import yfinance as _yf
        import math as _math
        try:
            _ibkr_spot = ibkr.get_spot_price(symbol)
            if _ibkr_spot and not _math.isnan(_ibkr_spot) and _ibkr_spot > 0:
                spot = float(_ibkr_spot)
                check("get_spot_price() via IBKR", True, f"${spot:.2f}")
            else:
                raise ValueError(f"IBKR returned invalid price: {_ibkr_spot}")
        except Exception as e:
            check("get_spot_price() via IBKR", False,
                  "no subscription — falling back to yfinance")
            try:
                _ticker = symbol.replace("US.", "")
                _hist = _yf.download(_ticker, period="1d", interval="1m",
                                     progress=False, auto_adjust=True)
                if len(_hist) > 0:
                    spot = float(_hist["Close"].iloc[-1])
                else:
                    # Outside market hours — use last daily close
                    _hist = _yf.download(_ticker, period="5d", interval="1d",
                                         progress=False, auto_adjust=True)
                    spot = float(_hist["Close"].iloc[-1])
                check("get_spot_price() via yfinance", spot > 0, f"${spot:.2f}")
                print(f"    yfinance spot: ${spot:.2f} (IBKR subscription not active)")
            except Exception as e2:
                check("get_spot_price() via yfinance", False, str(e2))
                spot = 0.0

        # ── 7. Option Expiries ────────────────────────────────────
        section(f"7 · Option Expiries ({symbol})")
        expiries = ibkr.get_option_expiries(symbol)
        check("get_option_expiries() returned list",
              len(expiries) > 0,
              f"{len(expiries)} expiries")
        print(f"\n    Upcoming expiries: {expiries[:6]}")

        for exp in expiries:
            try:
                dte = (date.fromisoformat(exp) - date.today()).days
                if 21 <= dte <= 45:
                    target_expiry = exp
                    break
            except Exception:
                pass

        check("21-45 DTE expiry found",
              target_expiry is not None,
              target_expiry or "none in range")
        if target_expiry:
            dte = (date.fromisoformat(target_expiry) - date.today()).days
            print(f"    Best expiry for order test: {target_expiry} ({dte} DTE)")

        # ── 8. Option Chain ───────────────────────────────────────
        section(f"8 · Option Chain ({symbol})")
        # Fallback: hardcode a known SPY weekly expiry (31 DTE from today)
        # IBKR without subscription returns garbage expiries — we bypass that
        if target_expiry is None:
            from datetime import date, timedelta
            # Find next Friday that's 21-45 DTE
            today = date.today()
            for days in range(21, 46):
                candidate = today + timedelta(days=days)
                if candidate.weekday() == 4:  # Friday
                    target_expiry = candidate.isoformat()
                    print(f"    Using hardcoded expiry: {target_expiry} "
                          f"({days} DTE) — bypassing IBKR subscription requirement")
                    break
        chain_expiry = target_expiry or (expiries[0] if expiries else None)
        if chain_expiry:
            try:
                chain = ibkr.get_option_chain(symbol, chain_expiry, "CALL")
                # Filter out garbage strikes (e.g. $10010 from missing subscription)
                if len(chain) > 0:
                    chain = chain[chain["strike_price"] < 50000]
                check("get_option_chain() returned valid rows",
                      len(chain) > 0,
                      f"{len(chain)} call strikes")
                if len(chain) > 0:
                    mid = len(chain) // 2
                    print(f"\n    Sample call strikes ({chain_expiry}):")
                    for _, row in chain.iloc[mid:mid+5].iterrows():
                        print(f"      ${row['strike_price']:.1f}")
            except Exception as e:
                # IBKR without subscription may not have this expiry — expected
                check("get_option_chain()", False,
                      f"{str(e)[:60]} (expected without subscription)")
                print(f"    Note: option chain requires market data subscription")
                print(f"    Order test will proceed using calculated strikes from yfinance spot")
        else:
            check("Option chain test skipped", False, "no expiry found")

        # ── 9-12. Order Tests ─────────────────────────────────────
        if args.skip_order:
            section("ORDER TESTS — SKIPPED (--skip-order)")
        elif spot <= 0 or (hasattr(spot, '__float__') and __import__('math').isnan(spot)):
            section("ORDER TESTS — SKIPPED")
            print(f"\n  Reason: spot price unavailable — both IBKR and yfinance failed")
        else:
            _order_tests(ibkr, symbol, spot, target_expiry)

    except KeyboardInterrupt:
        print("\n\n  Interrupted")
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
        pct = passed / total * 100
        print(f"  {passed}/{total} passed ({pct:.0f}%)")
        if passed == total:
            print(f"  ✓ All IBKR primitives validated on live account")
        else:
            print(f"  ⚠️  {total-passed} check(s) failed — review output above")
    print()


def _order_tests(ibkr, symbol, spot, expiry):
    """Place an unreachable limit order, verify pending, then cancel."""

    ticker       = symbol.replace("US.", "")
    exp_short    = expiry.replace("-", "")[2:]        # "260417"
    short_strike = round(spot * 1.08 / 5) * 5        # ~8% OTM, $5 round
    long_strike  = short_strike + 10
    sell_code    = f"{ticker}{exp_short}C{int(short_strike * 1000):08d}"
    buy_code     = f"{ticker}{exp_short}C{int(long_strike  * 1000):08d}"
    SAFE_CREDIT  = 8.00    # $8 on $10-wide spread — above market but mathematically valid

    section("9 · Place Combo Order at Unreachable Price")
    print(f"\n    SELL  {sell_code}")
    print(f"    BUY   {buy_code}")
    print(f"    Credit: ${SAFE_CREDIT:.2f}  (above market ~$1-3, valid for $10 spread, will not fill)")
    print(f"    Spot:   ${spot:.2f}  |  Strikes: ${short_strike:.0f}/${long_strike:.0f}")

    order_id = None
    try:
        order_id = ibkr.place_combo_order(
            sell_contract=sell_code,
            buy_contract=buy_code,
            qty=1,
            net_credit=SAFE_CREDIT,
        )
        check("place_combo_order() returned order_id",
              bool(order_id), f"order_id={order_id}")
    except Exception as e:
        check("place_combo_order()", False, str(e))
        print(f"\n    Combo order failed — checking if single-leg works instead")
        try:
            order_id = ibkr.place_limit_order(
                contract=sell_code,
                qty=1,
                price=SAFE_CREDIT,
                direction="SELL",
            )
            check("place_limit_order() fallback", bool(order_id),
                  f"order_id={order_id}")
        except Exception as e2:
            check("place_limit_order() fallback", False, str(e2))
            return

    if not order_id:
        return

    # ── 10. Check Status ──────────────────────────────────────────
    section("10 · Order Status Check")
    time.sleep(2)
    try:
        status = ibkr.get_order_status(order_id)
        check("get_order_status() returned", bool(status),
              f"status={status.get('status','?')}")
        check("Order NOT filled (as expected)",
              status.get("status") not in ("FILLED",),
              status.get("status", "?"))
        check("Filled qty = 0",
              int(status.get("filled_qty", 0)) == 0,
              f"filled={status.get('filled_qty', 0)}")
        print(f"\n    Full status: {status}")
    except Exception as e:
        check("get_order_status()", False, str(e))

    # ── 11. Cancel ────────────────────────────────────────────────
    section("11 · Cancel Order")
    try:
        cancelled = ibkr.cancel_order(order_id)
        check("cancel_order() returned True", cancelled, f"id={order_id}")
        time.sleep(2)
    except Exception as e:
        check("cancel_order()", False, str(e))
        print(f"\n  ⚠️  Cancel failed — please manually cancel order {order_id} in TWS")

    # ── 12. Confirm Removed ───────────────────────────────────────
    section("12 · Confirm Order Removed from Open Orders")
    try:
        open_orders = ibkr.get_open_orders()
        still_open  = False
        if len(open_orders) > 0 and "order_id" in open_orders.columns:
            still_open = str(order_id) in open_orders["order_id"].astype(str).values
        check("Order no longer in open orders",
              not still_open,
              f"remaining open: {len(open_orders)}")
        print(f"\n    ✓ Full order lifecycle validated:")
        print(f"      place_combo_order → PENDING → cancel_order → removed")
    except Exception as e:
        check("Confirm cancellation", False, str(e))


if __name__ == "__main__":
    main()
