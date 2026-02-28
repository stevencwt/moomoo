import moomoo as mm
import time
from datetime import date

# ── Configuration ──────────────────────────────────────────────────────────────
OPEND_HOST = '127.0.0.1'
OPEND_PORT  = 11111
SYMBOL      = 'US.TSLA'

# ── Streaming Handlers ─────────────────────────────────────────────────────────

class OptionQuoteHandler(mm.StockQuoteHandlerBase):
    """
    Fires on every price update.
    QUOTE stream columns include Greeks directly (no option_ prefix):
    delta, gamma, vega, theta, rho, implied_volatility, open_interest
    """
    def on_recv_rsp(self, rsp_str):
        ret, data = super().on_recv_rsp(rsp_str)
        if ret == mm.RET_OK:
            for _, row in data.iterrows():
                code   = row.get('code', '')
                last   = row.get('last_price', 'N/A')
                volume = row.get('volume', 'N/A')
                iv     = row.get('implied_volatility', 'N/A')
                delta  = row.get('delta',  'N/A')
                gamma  = row.get('gamma',  'N/A')
                theta  = row.get('theta',  'N/A')
                vega   = row.get('vega',   'N/A')
                oi     = row.get('open_interest', 'N/A')

                # Format floats nicely if available
                def fmt(v, w=7, d=4):
                    try:    return f"{float(v):>{w}.{d}f}"
                    except: return f"{'N/A':>{w}}"

                print(f"[QUOTE]  {str(code):<30} "
                      f"last={fmt(last,7,3)}  "
                      f"vol={str(volume):>6}  "
                      f"OI={str(oi):>5}  "
                      f"IV={fmt(iv,6,2)}%  "
                      f"δ={fmt(delta,7,4)}  "
                      f"γ={fmt(gamma,7,5)}  "
                      f"θ={fmt(theta,8,4)}  "
                      f"ν={fmt(vega,7,4)}")
        return ret, data


class OptionTickerHandler(mm.TickerHandlerBase):
    """Fires on every individual trade tick."""
    def on_recv_rsp(self, rsp_str):
        ret, data = super().on_recv_rsp(rsp_str)
        if ret == mm.RET_OK:
            for _, row in data.iterrows():
                direction_raw = str(row.get('ticker_direction', ''))
                direction     = "🟢 BUY " if 'BUY' in direction_raw else "🔴 SELL"
                code          = row.get('code', '')
                price         = row.get('price', 'N/A')
                volume        = row.get('volume', 'N/A')
                print(f"[TICK]   {str(code):<30} "
                      f"{direction}  "
                      f"price={str(price):>7}  "
                      f"qty={str(volume):>5}")
        return ret, data


class OptionCurKlineHandler(mm.CurKlineHandlerBase):
    """Fires on every 1-minute candle update."""
    def on_recv_rsp(self, rsp_str):
        ret, data = super().on_recv_rsp(rsp_str)
        if ret == mm.RET_OK:
            for _, row in data.iterrows():
                code = row.get('code', '')
                o    = row.get('open',   'N/A')
                h    = row.get('high',   'N/A')
                l    = row.get('low',    'N/A')
                c    = row.get('close',  'N/A')
                v    = row.get('volume', 'N/A')
                def fmt(v, d=3):
                    try:    return f"{float(v):>7.{d}f}"
                    except: return f"{'N/A':>7}"
                print(f"[1M BAR] {str(code):<30} "
                      f"O={fmt(o)}  H={fmt(h)}  "
                      f"L={fmt(l)}  C={fmt(c)}  vol={str(v):>6}")
        return ret, data


# ── Main ───────────────────────────────────────────────────────────────────────
print("=" * 80)
print(f"  MooMoo Options Streaming — {SYMBOL}  (Greeks in real-time)")
print("=" * 80)

quote_ctx = mm.OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)

# ── Step 1: Get nearest expiry and ATM contracts ───────────────────────────────
ret, expiry_data = quote_ctx.get_option_expiration_date(code=SYMBOL)
if ret != mm.RET_OK:
    print(f"❌ Failed to get expiry dates: {expiry_data}")
    quote_ctx.close()
    exit()

today_str      = date.today().strftime('%Y-%m-%d')
upcoming       = expiry_data[expiry_data['strike_time'] > today_str]
nearest_expiry = upcoming['strike_time'].iloc[0]
print(f"\n📌 Expiry: {nearest_expiry}")

ret, chain = quote_ctx.get_option_chain(
    code=SYMBOL,
    start=nearest_expiry,
    end=nearest_expiry,
    option_type=mm.OptionType.ALL,
    option_cond_type=mm.OptionCondType.ALL
)
if ret != mm.RET_OK:
    print(f"❌ Option chain error: {chain}")
    quote_ctx.close()
    exit()

calls = chain[chain['option_type'] == 'CALL'].reset_index(drop=True)
puts  = chain[chain['option_type'] == 'PUT'].reset_index(drop=True)

# Pick 2 ATM calls + 2 ATM puts
mid       = len(calls) // 2
atm_calls = calls.iloc[mid-1 : mid+1]['code'].tolist()
atm_puts  = puts.iloc[mid-1  : mid+1]['code'].tolist()
contracts = atm_calls + atm_puts

print(f"\n📋 Subscribed contracts ({len(contracts)}):")
for c in contracts:
    print(f"   {c}")

# ── Step 2: Attach handlers ────────────────────────────────────────────────────
quote_ctx.set_handler(OptionQuoteHandler())
quote_ctx.set_handler(OptionTickerHandler())
quote_ctx.set_handler(OptionCurKlineHandler())

# ── Step 3: Subscribe ─────────────────────────────────────────────────────────
sub_types = [mm.SubType.QUOTE, mm.SubType.TICKER, mm.SubType.K_1M]
ret, err  = quote_ctx.subscribe(contracts, sub_types, subscribe_push=True)
if ret != mm.RET_OK:
    print(f"❌ Subscription failed: {err}")
    quote_ctx.close()
    exit()

print(f"\n✅ Streaming: QUOTE (with live Greeks) | TICKER | 1M BAR")
print(f"   Format: last | vol | OI | IV% | delta | gamma | theta | vega")
print("\n⏳ Live feed running... (Ctrl+C to stop)\n")
print("-" * 80)

# ── Step 4: Keep alive ────────────────────────────────────────────────────────
try:
    while True:
        time.sleep(1)

except KeyboardInterrupt:
    print("\n\n🛑 Stopped by user.")

finally:
    quote_ctx.unsubscribe(contracts, sub_types)
    quote_ctx.close()
    print("✅ Unsubscribed and disconnected cleanly.")
    print("=" * 80)