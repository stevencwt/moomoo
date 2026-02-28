import moomoo as mm
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import date, timedelta

# ── Configuration ──────────────────────────────────────────────────────────────
OPEND_HOST    = '127.0.0.1'
OPEND_PORT    = 11111
SYMBOL_MM     = 'US.TSLA'   # MooMoo format
SYMBOL_YF     = 'TSLA'      # yfinance format
TARGET_DELTA  = 0.30        # target delta for strike selection
MIN_IV        = 25.0        # minimum IV% to bother selling
MIN_PREMIUM   = 1.00        # minimum option premium to collect ($)
DTE_MIN       = 7           # minimum days to expiry
DTE_MAX       = 45          # maximum days to expiry
BB_WINDOW     = 20          # Bollinger Band period
BB_STD        = 2.0         # Bollinger Band std devs
RSI_WINDOW    = 14          # RSI period
RSI_THRESHOLD = 60          # RSI overbought level for entry

print("=" * 65)
print("  Covered Call Signal Engine — TSLA")
print("=" * 65)

# ══════════════════════════════════════════════════════════════════
# PART 1: UNDERLYING ANALYSIS via yfinance
# ══════════════════════════════════════════════════════════════════
print("\n📈 PART 1: Fetching TSLA price history (yfinance)...")

df = yf.download(SYMBOL_YF, period='6mo', interval='1d', progress=False)
df.columns = df.columns.get_level_values(0)  # flatten multi-index
df = df[['Open','High','Low','Close','Volume']].copy()
df.columns = ['open','high','low','close','volume']
df = df.dropna()

print(f"   Loaded {len(df)} daily candles  "
      f"({df.index[0].date()} → {df.index[-1].date()})")

# ── Bollinger Bands ────────────────────────────────────────────────
df['sma20']       = df['close'].rolling(BB_WINDOW).mean()
df['std20']       = df['close'].rolling(BB_WINDOW).std()
df['upper_band']  = df['sma20'] + BB_STD * df['std20']
df['lower_band']  = df['sma20'] - BB_STD * df['std20']
df['bandwidth']   = (df['upper_band'] - df['lower_band']) / df['sma20'] * 100
df['pct_b']       = (df['close'] - df['lower_band']) / (df['upper_band'] - df['lower_band'])

# ── RSI ────────────────────────────────────────────────────────────
delta_close   = df['close'].diff()
gain          = delta_close.clip(lower=0).rolling(RSI_WINDOW).mean()
loss          = (-delta_close.clip(upper=0)).rolling(RSI_WINDOW).mean()
rs            = gain / loss
df['rsi']     = 100 - (100 / (1 + rs))

# ── MACD ───────────────────────────────────────────────────────────
ema12         = df['close'].ewm(span=12, adjust=False).mean()
ema26         = df['close'].ewm(span=26, adjust=False).mean()
df['macd']    = ema12 - ema26
df['signal']  = df['macd'].ewm(span=9, adjust=False).mean()
df['hist']    = df['macd'] - df['signal']

# ── ATR ────────────────────────────────────────────────────────────
tr            = pd.concat([
    df['high'] - df['low'],
    (df['high'] - df['close'].shift()).abs(),
    (df['low']  - df['close'].shift()).abs()
], axis=1).max(axis=1)
df['atr14']   = tr.rolling(14).mean()

# ── Latest values ──────────────────────────────────────────────────
last          = df.iloc[-1]
spot          = float(last['close'])
sma20         = float(last['sma20'])
upper_band    = float(last['upper_band'])
lower_band    = float(last['lower_band'])
pct_b         = float(last['pct_b'])
rsi           = float(last['rsi'])
macd_hist     = float(last['hist'])
atr           = float(last['atr14'])
bandwidth     = float(last['bandwidth'])

print(f"\n── Technical Indicators (latest daily close) ─────────────")
print(f"   Spot Price   : ${spot:.2f}")
print(f"   SMA20        : ${sma20:.2f}")
print(f"   Upper Band   : ${upper_band:.2f}")
print(f"   Lower Band   : ${lower_band:.2f}")
print(f"   %B           : {pct_b:.2f}  (>0.8 = near upper band)")
print(f"   BB Width     : {bandwidth:.1f}%")
print(f"   RSI(14)      : {rsi:.1f}")
print(f"   MACD Hist    : {macd_hist:.3f}  ({'bullish' if macd_hist > 0 else 'bearish'})")
print(f"   ATR(14)      : ${atr:.2f}")

# ── Technical Signal ───────────────────────────────────────────────
bb_signal    = pct_b >= 0.75        # price in upper 25% of BB
rsi_signal   = rsi >= RSI_THRESHOLD
macd_signal  = macd_hist > 0

print(f"\n── Signal Check ──────────────────────────────────────────")
print(f"   BB  (%B ≥ 0.75)    : {'✅ YES' if bb_signal   else '❌ NO '} ({pct_b:.2f})")
print(f"   RSI (≥ {RSI_THRESHOLD})         : {'✅ YES' if rsi_signal  else '❌ NO '} ({rsi:.1f})")
print(f"   MACD (bullish)     : {'✅ YES' if macd_signal else '❌ NO '} ({macd_hist:.3f})")

tech_signal = bb_signal and rsi_signal
print(f"\n   📊 Technical Entry Signal : {'✅ FAVOURABLE' if tech_signal else '⚠️  NOT IDEAL (proceed to scan options anyway)'}")

# ══════════════════════════════════════════════════════════════════
# PART 2: OPTIONS SCAN via MooMoo
# ══════════════════════════════════════════════════════════════════
print("\n\n📋 PART 2: Scanning options chain (MooMoo)...")

quote_ctx = mm.OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)

# Get expiry dates within DTE range
ret, expiry_data = quote_ctx.get_option_expiration_date(code=SYMBOL_MM)
if ret != mm.RET_OK:
    print(f"❌ Failed: {expiry_data}")
    quote_ctx.close()
    exit()

today     = date.today()
today_str = today.strftime('%Y-%m-%d')
upcoming  = expiry_data[expiry_data['strike_time'] > today_str].copy()
upcoming['dte'] = upcoming['strike_time'].apply(
    lambda x: (date.fromisoformat(x) - today).days
)
in_range = upcoming[(upcoming['dte'] >= DTE_MIN) & (upcoming['dte'] <= DTE_MAX)]

if in_range.empty:
    print(f"❌ No expiries found between {DTE_MIN}-{DTE_MAX} DTE")
    quote_ctx.close()
    exit()

print(f"\n   Expiries in {DTE_MIN}-{DTE_MAX} DTE range:")
print(in_range[['strike_time','dte','expiration_cycle']].to_string(index=False))

# Use nearest expiry in range
target_expiry = in_range.iloc[0]['strike_time']
target_dte    = int(in_range.iloc[0]['dte'])
print(f"\n   Selected: {target_expiry}  ({target_dte} DTE)")

# Get option chain
ret, chain = quote_ctx.get_option_chain(
    code=SYMBOL_MM,
    start=target_expiry,
    end=target_expiry,
    option_type=mm.OptionType.CALL,
    option_cond_type=mm.OptionCondType.ALL
)
if ret != mm.RET_OK:
    print(f"❌ Option chain error: {chain}")
    quote_ctx.close()
    exit()

# Filter OTM calls only (strike above spot)
calls = chain[chain['strike_price'] > spot].reset_index(drop=True)
print(f"   OTM calls available: {len(calls)}")

# Get snapshots for OTM calls
ret, snap = quote_ctx.get_market_snapshot(calls['code'].tolist())
if ret != mm.RET_OK:
    print(f"❌ Snapshot error: {snap}")
    quote_ctx.close()
    exit()

# Merge strike info
snap = snap.merge(
    calls[['code','strike_price']], on='code', how='left'
)

# Filter by delta and premium
greek_cols = ['option_delta','option_implied_volatility',
              'option_theta','option_vega']
for c in greek_cols:
    if c not in snap.columns:
        snap[c] = None

snap = snap[snap['option_delta'].notna()]
snap['option_delta'] = snap['option_delta'].astype(float)

candidates = snap[
    (snap['option_delta']  >= TARGET_DELTA - 0.10) &
    (snap['option_delta']  <= TARGET_DELTA + 0.10) &
    (snap['last_price']    >= MIN_PREMIUM) &
    (snap['option_implied_volatility'].astype(float) >= MIN_IV)
].copy()

print(f"\n── OTM Call Candidates (delta {TARGET_DELTA-0.10:.2f}–{TARGET_DELTA+0.10:.2f}, "
      f"premium ≥ ${MIN_PREMIUM}, IV ≥ {MIN_IV}%) ────────")

if candidates.empty:
    print("   ⚠️  No candidates match criteria. Widening delta range...")
    candidates = snap[snap['option_delta'] > 0].nsmallest(5, 'option_delta')

display_cols = ['code', 'strike_price', 'last_price', 'bid_price', 'ask_price',
                'option_implied_volatility', 'option_delta',
                'option_theta', 'option_vega', 'option_open_interest']
available    = [c for c in display_cols if c in candidates.columns]
print(candidates[available].to_string(index=False))

# ── Best candidate ─────────────────────────────────────────────────
if not candidates.empty:
    # Pick contract closest to target delta
    candidates['delta_diff'] = (candidates['option_delta'] - TARGET_DELTA).abs()
    best = candidates.loc[candidates['delta_diff'].idxmin()]

    print(f"\n── Best Covered Call Candidate ───────────────────────────")
    print(f"   Contract    : {best['code']}")
    print(f"   Strike      : ${float(best['strike_price']):.2f}  "
          f"(+{float(best['strike_price'])-spot:.2f} above spot)")
    print(f"   Premium     : ${float(best['last_price']):.2f}  "
          f"(bid ${float(best['bid_price']):.2f} / ask ${float(best['ask_price']):.2f})")
    print(f"   Delta       : {float(best['option_delta']):.4f}")
    print(f"   IV          : {float(best['option_implied_volatility']):.2f}%")
    print(f"   Theta       : {float(best['option_theta']):.4f}  (${abs(float(best['option_theta']))*100:.2f}/day per contract)")
    print(f"   DTE         : {target_dte} days")
    print(f"   Max Profit  : ${float(best['last_price'])*100:.2f} per contract (if expires worthless)")
    print(f"   Breakeven   : ${spot - float(best['last_price']):.2f}")

# ══════════════════════════════════════════════════════════════════
# PART 3: COMBINED SIGNAL SUMMARY
# ══════════════════════════════════════════════════════════════════
print(f"\n\n── Combined Signal Summary ───────────────────────────────")
print(f"   Underlying   : {SYMBOL_YF}  @ ${spot:.2f}")
print(f"   BB %B        : {pct_b:.2f}   {'✅' if bb_signal   else '❌'}")
print(f"   RSI          : {rsi:.1f}    {'✅' if rsi_signal  else '❌'}")
print(f"   MACD         : {'Bullish' if macd_signal else 'Bearish'}  {'✅' if macd_signal else '⚠️'}")

if not candidates.empty:
    iv_ok  = float(best['option_implied_volatility']) >= MIN_IV
    prem_ok = float(best['last_price']) >= MIN_PREMIUM
    print(f"   IV ≥ {MIN_IV}%    : {float(best['option_implied_volatility']):.1f}%  {'✅' if iv_ok else '❌'}")
    print(f"   Premium OK   : ${float(best['last_price']):.2f}  {'✅' if prem_ok else '❌'}")
    options_ok = iv_ok and prem_ok
else:
    options_ok = False

all_green = tech_signal and options_ok
print(f"\n   🚦 OVERALL SIGNAL : {'🟢 EXECUTE COVERED CALL' if all_green else '🔴 WAIT — CONDITIONS NOT MET'}")
print("=" * 65)

quote_ctx.close()