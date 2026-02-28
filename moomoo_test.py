import moomoo as mm
from datetime import date

# ── Configuration ──────────────────────────────────────────────────────────────
OPEND_HOST = '127.0.0.1'
OPEND_PORT  = 11111
SYMBOL      = 'US.TSLA'

print("=" * 60)
print(f"  MooMoo Options Data — {SYMBOL}")
print("=" * 60)

quote_ctx = mm.OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)

# ── 1. Get Expiry Dates ────────────────────────────────────────────────────────
ret, expiry_data = quote_ctx.get_option_expiration_date(code=SYMBOL)
if ret != mm.RET_OK:
    print(f"❌ Failed to get expiry dates: {expiry_data}")
    quote_ctx.close()
    exit()

today_str      = date.today().strftime('%Y-%m-%d')
upcoming       = expiry_data[expiry_data['strike_time'] > today_str]
nearest_expiry = upcoming['strike_time'].iloc[0]

print("\n📅 Available expiries (next 6):")
print(upcoming[['strike_time', 'expiration_cycle']].head(6).to_string(index=False))
print(f"\n📌 Selected expiry: {nearest_expiry}")

# ── 2. Get Option Chain ────────────────────────────────────────────────────────
ret, chain = quote_ctx.get_option_chain(
    code=SYMBOL,
    start=nearest_expiry,
    end=nearest_expiry,
    option_type=mm.OptionType.ALL,
    option_cond_type=mm.OptionCondType.ALL
)
if ret != mm.RET_OK:
    print(f"❌ Failed to get option chain: {chain}")
    quote_ctx.close()
    exit()

calls = chain[chain['option_type'] == 'CALL'].reset_index(drop=True)
puts  = chain[chain['option_type'] == 'PUT'].reset_index(drop=True)
print(f"\n✅ Option chain: {len(chain)} contracts  (CALLs: {len(calls)} | PUTs: {len(puts)})")

# ── 3. Estimate Spot Price via Put-Call Parity ────────────────────────────────
mid_strike      = calls['strike_price'].iloc[len(calls) // 2]
atm_range_calls = calls[(calls['strike_price'] >= mid_strike * 0.92) &
                         (calls['strike_price'] <= mid_strike * 1.08)]
atm_range_puts  = puts[(puts['strike_price']  >= mid_strike * 0.92) &
                        (puts['strike_price']  <= mid_strike * 1.08)]

probe_contracts = atm_range_calls['code'].tolist() + atm_range_puts['code'].tolist()
ret, probe_snap = quote_ctx.get_market_snapshot(probe_contracts)
if ret != mm.RET_OK:
    print(f"❌ Probe snapshot failed: {probe_snap}")
    quote_ctx.close()
    exit()

probe_calls = probe_snap[probe_snap['code'].str.contains('C')].copy()
probe_puts  = probe_snap[probe_snap['code'].str.contains('P')].copy()

spot_estimate = mid_strike
atm_strike    = mid_strike

if not probe_calls.empty and not probe_puts.empty:
    probe_calls['mid']    = (probe_calls['bid_price'] + probe_calls['ask_price']) / 2
    probe_puts['mid']     = (probe_puts['bid_price']  + probe_puts['ask_price'])  / 2
    probe_calls['strike'] = atm_range_calls.set_index('code').loc[probe_calls['code'], 'strike_price'].values
    probe_puts['strike']  = atm_range_puts.set_index('code').loc[probe_puts['code'],  'strike_price'].values

    merged = probe_calls[['strike', 'mid']].rename(columns={'mid': 'call_mid'}).merge(
             probe_puts[['strike',  'mid']].rename(columns={'mid': 'put_mid'}), on='strike')
    merged['implied_spot'] = merged['strike'] + merged['call_mid'] - merged['put_mid']

    spot_estimate = merged['implied_spot'].mean()
    atm_strike    = merged.iloc[(merged['strike'] - spot_estimate).abs().argsort().iloc[:1]]['strike'].values[0]
    print(f"\n💰 Implied spot price (put-call parity): ${spot_estimate:.2f}")
    print(f"🎯 ATM strike: ${atm_strike:.2f}")

# ── 4. Select Strikes Around ATM ─────────────────────────────────────────────
atm_calls = calls[(calls['strike_price'] >= atm_strike * 0.94) &
                  (calls['strike_price'] <= atm_strike * 1.06)].head(8)
atm_puts  = puts[(puts['strike_price']  >= atm_strike * 0.94) &
                 (puts['strike_price']  <= atm_strike * 1.06)].head(8)

final_contracts = atm_calls['code'].tolist() + atm_puts['code'].tolist()
print(f"\n📊 Fetching final snapshots for {len(final_contracts)} ATM contracts...")

ret, snap = quote_ctx.get_market_snapshot(final_contracts)
if ret != mm.RET_OK:
    print(f"❌ Snapshot error: {snap}")
    quote_ctx.close()
    exit()

# ── 5. Display Results (using correct option_ prefixed column names) ───────────
display_cols = [
    'code',
    'last_price', 'bid_price', 'ask_price',
    'volume',
    'option_open_interest',
    'option_implied_volatility',
    'option_delta',
    'option_gamma',
    'option_theta',
    'option_vega',
    'option_rho'
]
# Only keep columns that exist
display_cols = [c for c in display_cols if c in snap.columns]

# Rename for cleaner display
rename_map = {
    'option_open_interest'     : 'OI',
    'option_implied_volatility': 'IV%',
    'option_delta'             : 'delta',
    'option_gamma'             : 'gamma',
    'option_theta'             : 'theta',
    'option_vega'              : 'vega',
    'option_rho'               : 'rho',
}

print(f"\n── TSLA CALL Options (Expiry: {nearest_expiry}) {'─' * 18}")
call_snap = snap[snap['code'].str.contains('C')][display_cols].rename(columns=rename_map)
print(call_snap.to_string(index=False))

print(f"\n── TSLA PUT Options  (Expiry: {nearest_expiry}) {'─' * 18}")
put_snap = snap[snap['code'].str.contains('P')][display_cols].rename(columns=rename_map)
print(put_snap.to_string(index=False))

# ── 6. Summary ────────────────────────────────────────────────────────────────
print("\n── Summary " + "─" * 49)
print(f"   Symbol          : {SYMBOL}")
print(f"   Expiry          : {nearest_expiry}")
print(f"   Implied Spot    : ${spot_estimate:.2f}")
print(f"   ATM Strike      : ${atm_strike:.2f}")
print(f"   Contracts shown : {len(snap)}")
if 'option_implied_volatility' in snap.columns:
    print(f"   Avg IV          : {snap['option_implied_volatility'].mean():.2f}%")
if 'option_open_interest' in snap.columns:
    print(f"   Total OI        : {snap['option_open_interest'].sum():,}")

quote_ctx.close()
print("=" * 60)