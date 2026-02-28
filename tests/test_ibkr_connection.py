"""
IBKR TWS Connectivity Validation
==================================
Confirms that TWS is running, API is enabled, and the bot can read
your real account positions.

Usage:
    python3 tests/test_ibkr_connection.py

What it checks:
  1. Can connect to TWS on 127.0.0.1:7497
  2. Managed accounts (paper) or real accounts visible
  3. Account summary (net liquidation, cash)
  4. Stock positions — confirms TSLA shares visible
  5. Option positions (if any open)
  6. Spot price fetch for TSLA
  7. Option chain available for TSLA
"""

import sys

try:
    from ib_insync import IB, Stock, util
except ImportError:
    print("❌  ib_insync not installed. Run: pip3 install ib_insync")
    sys.exit(1)

util.logToConsole(level="ERROR")   # suppress ib_insync noise

HOST      = "127.0.0.1"
PORT      = 7496    # 7497 = TWS paper | 7496 = TWS live | 4002 = Gateway paper
CLIENT_ID = 10      # Use 10 for this test to avoid conflict with the bot (client_id=1)
SYMBOL    = "TSLA"

print("\n" + "═" * 60)
print("  IBKR TWS Connectivity Validation")
print("═" * 60)

ib = IB()

# ── 1. Connect ────────────────────────────────────────────────────
print(f"\n[1] Connecting to TWS at {HOST}:{PORT} (client_id={CLIENT_ID})...")
try:
    ib.connect(HOST, PORT, clientId=CLIENT_ID, timeout=10)
    print("    ✅ Connected")
except Exception as e:
    print(f"    ❌ Connection failed: {e}")
    print()
    print("  Troubleshooting:")
    print("  - Is TWS running and logged in?")
    print("  - Is API enabled? Edit → Global Config → API → Settings")
    print("    ✓ Enable ActiveX and Socket Clients")
    print(f"    Socket port must be {PORT}")
    print("  - Try port 7496 if you are using a live account")
    print("  - Try port 4002 if you are using IB Gateway instead of TWS")
    sys.exit(1)

# ── 2. Managed accounts ───────────────────────────────────────────
print("\n[2] Accounts:")
accounts = ib.managedAccounts()
for acc in accounts:
    env = "PAPER" if acc.startswith("DU") else "LIVE"
    print(f"    {acc}  [{env}]")
account = accounts[0]

# ── 3. Account summary ────────────────────────────────────────────
print("\n[3] Account summary:")
summary = ib.accountSummary(account)
tags    = {s.tag: s.value for s in summary}
net_liq = tags.get("NetLiquidation", "N/A")
cash    = tags.get("TotalCashValue", "N/A")
print(f"    Net liquidation : ${float(net_liq):,.2f}" if net_liq != "N/A" else "    Net liquidation : N/A")
print(f"    Cash            : ${float(cash):,.2f}"    if cash    != "N/A" else "    Cash            : N/A")

# ── 4. Stock positions ────────────────────────────────────────────
print(f"\n[4] Stock positions (account: {account}):")
positions = ib.positions(account)
stock_pos = [p for p in positions if p.contract.secType == "STK"]

if not stock_pos:
    print("    (no stock positions)")
else:
    print(f"    {'Symbol':<12} {'Qty':>8} {'Avg Cost':>12}")
    print(f"    {'─'*12} {'─'*8} {'─'*12}")
    for p in stock_pos:
        print(f"    {p.contract.symbol:<12} {int(p.position):>8} {p.avgCost:>12.2f}")

tsla_pos = next((p for p in stock_pos if p.contract.symbol == SYMBOL), None)
if tsla_pos:
    print(f"\n    ✅ {SYMBOL} found: {int(tsla_pos.position)} shares")
else:
    print(f"\n    ℹ️  {SYMBOL} not found in this account")

# ── 5. Option positions ───────────────────────────────────────────
print(f"\n[5] Option positions:")
opt_pos = [p for p in positions if p.contract.secType == "OPT"]
if not opt_pos:
    print("    (no open option positions)")
else:
    for p in opt_pos:
        c = p.contract
        print(f"    {c.symbol} {c.lastTradeDateOrContractMonth} "
              f"{c.right}{c.strike}  qty={int(p.position)}  cost={p.avgCost:.2f}")

# ── 6. Spot price ─────────────────────────────────────────────────
print(f"\n[6] Spot price for {SYMBOL}:")
try:
    contract = Stock(SYMBOL, "SMART", "USD")
    ib.qualifyContracts(contract)
    ticker = ib.reqMktData(contract, snapshot=True)
    ib.sleep(2)
    price = ticker.last if (ticker.last and ticker.last > 0) else (
        (ticker.bid + ticker.ask) / 2 if (ticker.bid and ticker.ask) else None
    )
    ib.cancelMktData(contract)
    if price:
        print(f"    ✅ {SYMBOL} = ${price:.2f}")
    else:
        print(f"    ⚠️  Price not available (market may be closed — this is normal)")
except Exception as e:
    print(f"    ❌ Failed: {e}")

# ── 7. Option chain ───────────────────────────────────────────────
print(f"\n[7] Option chain for {SYMBOL}:")
try:
    stock = Stock(SYMBOL, "SMART", "USD")
    ib.qualifyContracts(stock)
    chains = ib.reqSecDefOptParams(
        underlyingSymbol=SYMBOL,
        futFopExchange="",
        underlyingSecType="STK",
        underlyingConId=stock.conId,
    )
    smart = next((c for c in chains if c.exchange == "SMART"), None)
    if smart:
        upcoming = sorted([e for e in smart.expirations
                           if e >= __import__("datetime").date.today().strftime("%Y%m%d")])[:5]
        print(f"    ✅ {len(smart.strikes)} strikes | {len(smart.expirations)} expiries available")
        print(f"    Next 5 expiries: {[f'{e[:4]}-{e[4:6]}-{e[6:]}' for e in upcoming]}")
    else:
        print("    ⚠️  No SMART exchange chain found")
except Exception as e:
    print(f"    ❌ Failed: {e}")

# ── Summary ───────────────────────────────────────────────────────
ib.disconnect()
print("\n" + "═" * 60)
print("  Validation complete")
print("═" * 60)
print()
print("  If all steps show ✅ — update config.yaml:")
print("    broker:  \"ibkr\"")
print("    ibkr:")
print(f"      port:    {PORT}")
print(f"      account: \"{account}\"")
print()