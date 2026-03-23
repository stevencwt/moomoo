#!/usr/bin/env python3
"""
diagnose_gex.py — GEX data pipeline diagnostic
Run from /Users/user/moomoo:  python3 diagnose_gex.py
"""
import asyncio, sys, time
from pathlib import Path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
print(f"✓ asyncio event loop set: {loop}")

import yaml
with open("config/config.yaml") as f:
    cfg = yaml.safe_load(f)
ibkr_cfg = cfg.get("ibkr", {})
print(f"✓ Config loaded | account={ibkr_cfg.get('account')} port={ibkr_cfg.get('port')}")

from ibkr_connector import IBKRClient
client = IBKRClient(
    host=ibkr_cfg.get("host","127.0.0.1"), port=ibkr_cfg.get("port",7496),
    client_id=3, account=ibkr_cfg.get("account",""), mode=cfg.get("mode","live"),
)
client.connect()
print(f"✓ Connected: {client.is_connected()}")

TICKER = "SPY"

# ── Step 1: qualify underlying ────────────────────────────────────────────────
print(f"\n── Step 1: qualifyContracts(Stock('{TICKER}')) ──")
from ib_insync import Stock
stock     = Stock(TICKER, "SMART", "USD")
qualified = client._ib.qualifyContracts(stock)
print(f"  conId after qualify: {qualified[0].conId if qualified else 'FAILED'}")

# ── Step 2: reqSecDefOptParams ────────────────────────────────────────────────
print(f"\n── Step 2: reqSecDefOptParams ──")
chains = client._ib.reqSecDefOptParams(
    underlyingSymbol=TICKER, futFopExchange="",
    underlyingSecType="STK", underlyingConId=qualified[0].conId,
)
print(f"  chains returned: {len(chains)}")
best = max(chains, key=lambda c: len(c.expirations))
print(f"  best exchange={best.exchange}  strikes={len(best.strikes)}  expiries={len(best.expirations)}")

# ── Step 3: get_option_expiries ───────────────────────────────────────────────
print(f"\n── Step 3: client.get_option_expiries('{TICKER}') ──")
expiries = client.get_option_expiries(TICKER)
print(f"  returned {len(expiries)} expiries — {'✓' if expiries else '✗ EMPTY'}")
if expiries:
    print(f"  first 5: {expiries[:5]}")
front_expiry = expiries[0] if expiries else None
if not front_expiry:
    print("Cannot continue without expiries"); client.disconnect(); sys.exit(1)

# ── Spot price ────────────────────────────────────────────────────────────────
spot = client.get_spot_price(TICKER)
print(f"\nSpot price: ${spot:.2f}")

# ── Step 4: reqContractDetails wildcard → expiry-specific valid strikes ───────
print(f"\n── Step 4: reqContractDetails(strike=0 wildcard) for {front_expiry} ──")
from ib_insync import Option as IBOption
expiry_ibkr  = front_expiry.replace("-", "")
call_details = client._ib.reqContractDetails(IBOption(
    symbol=TICKER, lastTradeDateOrContractMonth=expiry_ibkr,
    strike=0, right="C", exchange="SMART", currency="USD",
    multiplier="100", tradingClass=TICKER))
put_details  = client._ib.reqContractDetails(IBOption(
    symbol=TICKER, lastTradeDateOrContractMonth=expiry_ibkr,
    strike=0, right="P", exchange="SMART", currency="USD",
    multiplier="100", tradingClass=TICKER))
print(f"  calls listed: {len(call_details)}")
print(f"  puts  listed: {len(put_details)}")
if call_details:
    strikes = sorted(d.contract.strike for d in call_details)
    print(f"  strike range: ${min(strikes):.0f} – ${max(strikes):.0f}")

# ── Step 5: get_option_chain (uses reqContractDetails now) ────────────────────
print(f"\n── Step 5: client.get_option_chain('{TICKER}', '{front_expiry}', 'ALL') ──")
try:
    chain_df = client.get_option_chain(TICKER, front_expiry, "ALL")
    print(f"  rows: {len(chain_df)}")
    print(f"  columns: {list(chain_df.columns)}")
    atm = chain_df[chain_df["strike_price"].between(spot*0.98, spot*1.02)]
    atm_codes = atm["code"].head(6).tolist()
    print(f"  ATM codes: {atm_codes}")
except Exception as e:
    print(f"  FAILED: {e}"); client.disconnect(); sys.exit(1)

# ── Step 6: get_option_snapshot (streaming Greeks + reqContractDetails OI) ────
print(f"\n── Step 6: client.get_option_snapshot({len(atm_codes)} ATM codes) ──")
snap = client.get_option_snapshot(atm_codes)
print(f"  rows: {len(snap)}")
if not snap.empty:
    row = snap.iloc[0]
    for col in ["code","strike_price","option_gamma","option_open_interest",
                "option_delta","option_iv","bid_price","ask_price"]:
        print(f"    {col}: {row.get(col,'N/A')}")

# ── Step 7: full GEX compute ──────────────────────────────────────────────────
print(f"\n── Step 7: GEXCalculator({{}}).compute('{TICKER}', client) ──")
from src.scalp.signals.gex_calculator import GEXCalculator
result = GEXCalculator({}).compute(TICKER, client)
print(f"  error:          '{result['error']}'")
print(f"  gamma_wall:     ${result['gamma_wall']:.2f}")
print(f"  gex_flip:       ${result['gex_flip']:.2f}")
print(f"  is_stabilising: {result['is_stabilising']}")
print(f"  total_net_gex:  {result['total_net_gex']:,.0f}")
print(f"  strikes:        {len(result['net_gex_series'])}")
print(f"  data_source:    {result['data_source']}")

client.disconnect()
print("\n✓ Done")
