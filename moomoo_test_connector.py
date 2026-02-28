"""
MooMoo Connector — Live Test Script v3
============================================================
Fix from v2: Use correct OPTION account (4310610) for options orders.
             STOCK account (565755) cannot place options trades.

Run on your Mac:
    python3 test_connector_v3.py
"""

import moomoo as mm
import pandas as pd
from datetime import datetime
import time

# ── CONFIG ────────────────────────────────────────────────────────
HOST             = "127.0.0.1"
PORT             = 11111
TRADE_ENV        = mm.TrdEnv.SIMULATE
STOCK_ACCOUNT_ID = 565755    # For stock positions / share queries
OPTION_ACCOUNT_ID = 4310610  # For ALL options orders ← key fix

SYMBOL = "US.TSLA"

print("=" * 60)
print("MooMoo Connector Test v3")
print(f"Mode  : PAPER/SIMULATE")
print(f"Stock account  : {STOCK_ACCOUNT_ID}")
print(f"Options account: {OPTION_ACCOUNT_ID}  ← used for orders")
print(f"Time  : {datetime.now()}")
print("=" * 60)

# ── TEST 1: Quote API + Expiry Dates ─────────────────────────────
print("\n[1/8] Quote API + expiry dates...")
test_contract = None
TEST_EXPIRY   = None

try:
    quote_ctx = mm.OpenQuoteContext(host=HOST, port=PORT)
    ret, data = quote_ctx.get_option_expiration_date(code=SYMBOL)
    if ret == 0:
        expiries    = data["strike_time"].tolist()
        # Skip expired ones — find first expiry from today onward
        from datetime import date
        today       = date.today().strftime("%Y-%m-%d")
        future      = [e for e in expiries if e >= today]
        TEST_EXPIRY = future[0] if future else expiries[0]
        print(f"  ✅ Connected | {len(expiries)} expiries found")
        print(f"  Next 3 upcoming: {future[:3]}")
        print(f"  Using: {TEST_EXPIRY}")
    else:
        print(f"  ❌ Failed: {data}")
        exit(1)
except Exception as e:
    print(f"  ❌ Exception: {e}")
    exit(1)

# ── TEST 2: Option Chain ───────────────────────────────────────────
print(f"\n[2/8] Option chain for {SYMBOL} expiry={TEST_EXPIRY}...")
try:
    ret, chain = quote_ctx.get_option_chain(
        code=SYMBOL,
        start=TEST_EXPIRY,
        end=TEST_EXPIRY,
        option_type=mm.OptionType.ALL
    )
    if ret == 0:
        calls = chain[chain["option_type"] == "CALL"]
        puts  = chain[chain["option_type"] == "PUT"]
        print(f"  ✅ {len(calls)} calls, {len(puts)} puts")

        # Pick OTM call for snapshot + order tests
        # Get current spot price from a snapshot first
        # For now estimate: pick strike a bit above round number
        otm = calls[calls["strike_price"] > 400].head(5)
        if len(otm) == 0:
            otm = calls.head(5)
        test_contract = otm.iloc[0]["code"]
        test_strike   = otm.iloc[0]["strike_price"]
        print(f"  Test contract: {test_contract} @ strike ${test_strike}")
        print(f"  Available OTM calls (top 5):")
        for _, row in otm.iterrows():
            print(f"     {row['code']}  strike={row['strike_price']}")
    else:
        print(f"  ❌ Failed: {chain}")
        exit(1)
except Exception as e:
    print(f"  ❌ Exception: {e}")
    exit(1)

# ── TEST 3: Snapshot + Greeks ──────────────────────────────────────
print(f"\n[3/8] Snapshot + Greeks for {test_contract}...")
try:
    ret, snap = quote_ctx.get_market_snapshot([test_contract])
    if ret == 0:
        row = snap.iloc[0]
        print(f"  ✅ Snapshot received")
        print(f"  sec_status     : {row.get('sec_status','?')}")
        print(f"  last_price     : {row.get('last_price','?')}")
        print(f"  bid_price      : {row.get('bid_price','?')}")
        print(f"  ask_price      : {row.get('ask_price','?')}")
        print(f"  option_delta   : {row.get('option_delta','?')}")
        print(f"  option_gamma   : {row.get('option_gamma','?')}")
        print(f"  option_theta   : {row.get('option_theta','?')}")
        print(f"  option_vega    : {row.get('option_vega','?')}")
        print(f"  option_implied_volatility: {row.get('option_implied_volatility','?')}")
        print(f"  option_open_interest     : {row.get('option_open_interest','?')}")

        # Note if Greeks are zero (expected outside market hours)
        if row.get('option_delta', 0) == 0:
            print(f"  ℹ️  Greeks are 0 — expected outside market hours (9:30–16:00 ET)")
    else:
        print(f"  ❌ Failed: {snap}")
except Exception as e:
    print(f"  ❌ Exception: {e}")

quote_ctx.close()
print("  Quote context closed.")

# ── TEST 4: Trade API ──────────────────────────────────────────────
print(f"\n[4/8] Trade API connection...")
try:
    trd_ctx = mm.OpenSecTradeContext(
        filter_trdmarket=mm.TrdMarket.US,
        host=HOST,
        port=PORT,
        security_firm=mm.SecurityFirm.FUTUINC
    )
    ret, acc_list = trd_ctx.get_acc_list()
    if ret == 0:
        print(f"  ✅ Connected | {len(acc_list)} accounts")
        for _, acc in acc_list.iterrows():
            marker = " ← options" if acc['acc_id'] == OPTION_ACCOUNT_ID else " ← stocks"
            print(f"     acc_id={acc['acc_id']}  type={acc['sim_acc_type']}{marker}")
    else:
        print(f"  ❌ Failed: {acc_list}")
        exit(1)
except Exception as e:
    print(f"  ❌ Exception: {e}")
    exit(1)

# ── TEST 5: Account Funds (Options Account) ────────────────────────
print(f"\n[5/8] Funds in options account ({OPTION_ACCOUNT_ID})...")
try:
    ret, funds = trd_ctx.accinfo_query(
        trd_env=TRADE_ENV,
        acc_id=OPTION_ACCOUNT_ID
    )
    if ret == 0:
        row = funds.iloc[0]
        print(f"  ✅ Funds received")
        print(f"  total_assets : {row.get('total_assets','?')}")
        print(f"  cash         : {row.get('cash','?')}")
        print(f"  market_val   : {row.get('market_val','?')}")
    else:
        print(f"  ❌ Failed: {funds}")
except Exception as e:
    print(f"  ❌ Exception: {e}")

# ── TEST 6: Open Positions (Options Account) ───────────────────────
print(f"\n[6/8] Open positions in options account...")
try:
    ret, positions = trd_ctx.position_list_query(
        trd_env=TRADE_ENV,
        acc_id=OPTION_ACCOUNT_ID
    )
    if ret == 0:
        print(f"  ✅ {len(positions)} open position(s)")
        if len(positions) > 0:
            for _, pos in positions.iterrows():
                print(f"     → code={pos.get('code','?')} "
                      f"qty={pos.get('qty','?')} "
                      f"cost={pos.get('cost_price','?')}")
    else:
        print(f"  ❌ Failed: {positions}")
except Exception as e:
    print(f"  ❌ Exception: {e}")

# ── TEST 7: Place Paper Order (Options Account) ────────────────────
order_id = None
print(f"\n[7/8] Place test order on OPTIONS account ({OPTION_ACCOUNT_ID})...")
if test_contract is None:
    print("  ⚠️  Skipping — no contract found")
else:
    print(f"  Contract : {test_contract}")
    print(f"  Price    : $99.00 (unrealistically high — will NOT fill)")
    print(f"  Account  : {OPTION_ACCOUNT_ID} (OPTION account)")
    try:
        ret, order_data = trd_ctx.place_order(
            price=99.00,
            qty=1,
            code=test_contract,
            trd_side=mm.TrdSide.SELL,
            order_type=mm.OrderType.NORMAL,
            trd_env=TRADE_ENV,
            acc_id=OPTION_ACCOUNT_ID      # ← key fix: use OPTION account
        )
        if ret == 0:
            order_id = str(order_data["order_id"].iloc[0])
            status   = order_data["order_status"].iloc[0]
            print(f"  ✅ Order placed!")
            print(f"  Order ID : {order_id}")
            print(f"  Status   : {status}")
        else:
            print(f"  ❌ Failed: {order_data}")
    except Exception as e:
        print(f"  ❌ Exception: {e}")

# ── TEST 8: Verify + Cancel ────────────────────────────────────────
print(f"\n[8/8] Verify order then cancel...")
if order_id is None:
    print("  ⚠️  Skipping — no order placed")
else:
    try:
        time.sleep(1)
        ret, open_orders = trd_ctx.order_list_query(
            trd_env=TRADE_ENV,
            acc_id=OPTION_ACCOUNT_ID
        )
        if ret == 0:
            print(f"  ✅ Open orders: {len(open_orders)}")
            for _, o in open_orders.iterrows():
                print(f"     → order_id={o.get('order_id','?')} "
                      f"code={o.get('code','?')} "
                      f"status={o.get('order_status','?')}")
        else:
            print(f"  ❌ order_list_query failed: {open_orders}")

        # Cancel
        print(f"\n  Cancelling {order_id}...")
        ret, cancel = trd_ctx.modify_order(
            modify_order_op=mm.ModifyOrderOp.CANCEL,
            order_id=order_id,
            qty=1,
            price=99.00,
            trd_env=TRADE_ENV,
            acc_id=OPTION_ACCOUNT_ID
        )
        if ret == 0:
            print(f"  ✅ Cancelled successfully")
        else:
            print(f"  ❌ Cancel failed: {cancel}")
            print(f"  ⚠️  Manually cancel order {order_id} in MooMoo app NOW")

    except Exception as e:
        print(f"  ❌ Exception: {e}")
        print(f"  ⚠️  Manually cancel any open test orders in MooMoo app")

trd_ctx.close()

# ── SUMMARY ───────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("TEST COMPLETE")
print("=" * 60)
print(f"""
Key findings from this test run:
  Stock account  : {STOCK_ACCOUNT_ID}  → use for querying share positions
  Options account: {OPTION_ACCOUNT_ID} → use for ALL options orders

All ✅?  → Paper connector fully working. Proceed to Phase 2.
Any ❌?  → Paste output to Claude for diagnosis.

To test LIVE execution (single trade, ~$5-10 risk):
  1. Change TRADE_ENV = mm.TrdEnv.REAL
  2. Pick a deep OTM call expiring within 7 days worth ~$0.05-0.10
  3. Change price=99.00 to the real bid price
  4. Run once, confirm fill, close manually in MooMoo app
""")