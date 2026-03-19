# Options-Informed GEX Reversion Scalper
## Strategy Research & Implementation Reference — `/Users/user/moomoo`

*Consolidated from three rounds of analysis reviewed by Claude and Xai (March 2026). Covers conceptual foundations, signal architecture, strategy logic, and full implementation specification.*

---

## Table of Contents

1. [Purpose and Scope](#1-purpose-and-scope)
2. [Conceptual Foundation — Why Options Signals Lead Price](#2-conceptual-foundation)
3. [Signal Stack — Ranked by Quality and Buildability](#3-signal-stack)
4. [Signal 1: Gamma Exposure (GEX) — Deep Dive](#4-signal-1-gex-deep-dive)
5. [Signal 2: IV Skew + Intraday Rate of Change](#5-signal-2-iv-skew)
6. [Signal 3: VIX Slope + VVIX Spike Detection](#6-signal-3-vix-vvix)
7. [Signal 4: Per-Symbol IV Rank (Repurposed)](#7-signal-4-iv-rank)
8. [Signal 5: CBOE Aggregate Put/Call Ratio (Later)](#8-signal-5-pcr)
9. [Regime Integration — Existing Infrastructure as Gate](#9-regime-integration)
10. [Strategy Architecture — Full Specification](#10-strategy-architecture)
11. [Instrument Selection — Stock vs Option](#11-instrument-selection)
12. [Entry Triggers — Detailed Rules](#12-entry-triggers)
13. [Position Management — Stops, Targets, Exits](#13-position-management)
14. [Trading the GEX Environment — Mode Switching](#14-gex-mode-switching)
15. [Level 2 Order Book — Decision](#15-level-2-decision)
16. [File Structure and Build Order](#16-file-structure-and-build-order)
17. [Module Specifications — Implementation Reference](#17-module-specifications)
18. [Validation Gate — Paper Trading Requirements](#18-validation-gate)
19. [Key Numbers and Thresholds Reference](#19-key-numbers-reference)

---

## 1. Purpose and Scope

This document specifies the **Options-Informed GEX Reversion Scalper** — a day trading strategy for US large-cap stocks that uses derivatives market signals as leading indicators, filtered through the existing regime detection infrastructure.

**Deployment target:** This strategy is deployed inside the existing options income bot at `/Users/user/moomoo`, not in a new codebase. It reuses the existing `IBKRClient` connection, HMM + LLM regime bridge, IV Rank pipeline, `paper_ledger.py` pattern, `portfolio_guard.py` pattern, and `config.yaml` structure. The scalping bot runs as a parallel scheduler alongside the options income bot — both share the same broker connection (different `client_id`) but write to separate SQLite ledgers.

**What this strategy is:**
A 5-minute timeframe scalping strategy that enters at structurally significant options market levels, in the direction confirmed by regime, skew, and volatility signals. It profits from the mechanical reality that large market participants (options dealers) are *forced* to trade the underlying stock in predictable ways based on their options positions. Knowing where and how they must trade gives you an informational edge.

**What this strategy is not:**
It is not a pure technical analysis strategy (RSI/MACD alone), not a news-driven strategy, and not a high-frequency strategy. It does not replace or interfere with the options income bot's bear call spread / bull put spread logic — the two strategies run as independent schedulers within the same codebase and write to separate SQLite ledgers (`paper_trades.db` for the options bot, `scalp_trades.db` for this strategy).

**Relationship to existing infrastructure:**
Deployed inside `/Users/user/moomoo`. The strategy directly reuses:
- `IBKRClient` — already connected, validated on account U18705798
- HMM + LLM regime bridge — already running on the full watchlist
- IV Rank pipeline — already computed daily for all 8 symbols
- `paper_ledger.py` pattern — the scalp ledger follows the same SQLite conventions
- `portfolio_guard.py` pattern — the scalp guard mirrors the same daily-cap logic
- `config.yaml` — scalping parameters are added as a new `scalp:` block

New code required: `gex_calculator.py`, `iv_skew.py`, `vix_monitor.py`, `scalp_gate.py`, `scalp_entry.py`, `scalp_instrument.py`, `scalp_position.py`, `scalp_ledger.py`, and a new `scalp_scheduler` section in `main.py` or `bot_scheduler.py`.

---

## 2. Conceptual Foundation — Why Options Signals Lead Price

### The dealer hedging mechanic

To understand why this strategy works, you need to understand what options market makers (dealers) do for a living and why it forces them to trade stocks in predictable ways.

When you buy a call option, a dealer sells it to you. That dealer is now *short* the call — they collect premium, but they're exposed to the risk that the stock rises above the strike. To protect themselves, they *buy shares* of the underlying stock. How many shares? Exactly `delta × 100` per contract — where delta is the option's sensitivity to a $1 move in the stock. A 0.40-delta call means they buy 40 shares per contract as a hedge.

But here's what makes this interesting: **delta changes as the stock moves.** If the stock rises, the delta of the call increases (it's moving closer to the money), so the dealer must buy *more* shares. If the stock falls, delta decreases, and they sell shares. This continuous adjustment is called **delta hedging**, and it creates mechanical buying and selling flows that are entirely predictable from the option chain.

**Gamma** is the rate of change of delta. A high-gamma option requires large, rapid hedge adjustments for every dollar of stock movement. The total gamma exposure of all outstanding options — weighted by open interest — is the **GEX (Gamma Exposure)** map.

### Positive vs negative GEX environments

When aggregate dealer gamma is positive (more call open interest than put open interest at nearby strikes), dealers are:
- **Buying** when the stock falls (delta decreased → they bought too many shares → they sell... wait, this is the stabilising case)

Let's be precise. Dealers who sold calls are *short gamma* — but from the market's perspective, the net dealer position is what matters:

**Positive net GEX** means dealers are net short gamma (they sold more calls than puts to the market). To stay hedged, they must:
- Buy shares when the stock *falls* (their delta dropped, they now own fewer shares than needed, so they buy back)
- Sell shares when the stock *rises* (their delta rose, they now own too many shares, so they trim)

This creates a **stabilising force** — dealers are systematically buying dips and selling rips. Price tends to oscillate in a range, reverting toward the gamma wall (the strike with the highest net GEX). This is the environment where mean-reversion scalping works.

**Negative net GEX** means dealers are net long gamma (they bought more options than they sold, or put open interest dominates). Now the hedging reverses:
- They must sell shares when the stock falls (amplifying the move)
- They must buy shares when the stock rises (amplifying the move)

This creates an **amplifying force** — price moves faster and extends further than normal. This is a momentum environment, not a mean-reversion environment. The strategy switches mode accordingly.

### Why this matters for intraday trading

0DTE (zero days to expiration) options now represent **50–62% of total SPX/SPY daily volume** (CBOE data 2025–2026, record 62.4% in August 2025). These options have gamma that is 10–20× higher than monthly options at the same strike. This means intraday dealer hedging flows are dominated by 0DTE positions, making the GEX map computed from the front expiry the highest-signal version of the metric.

**Historical evidence:** Positive-GEX environments have shown approximately 78–93% intraday range containment in SPX/SPY (SpotGamma analysis). This means price stays between the gamma walls roughly 8–9 times out of 10. The walls are not brick walls — they are gravity — but the failure rate is low enough to build a strategy around.

### Why IV skew leads price

When portfolio managers and institutions want to protect a long equity position, they buy put options. High demand for puts drives up their implied volatility relative to calls. The **IV skew** (the difference in implied volatility between equidistant OTM puts and calls) therefore measures *how much the smart money is paying to protect against downside*. This is forward-looking — it reflects what informed participants believe, not where price has been. A steepening skew (put demand increasing relative to calls) often precedes downside moves or reduces the reliability of upside breakouts.

### Why VVIX gives early warning

VVIX is the "VIX of VIX" — it measures how volatile implied volatility itself is expected to be. When institutions sense an imminent regime shift or risk-off event, they buy options on VIX before VIX itself moves. VVIX therefore often spikes 15–30 minutes before VIX reacts, giving you an early warning signal to stop entering new positions before the storm arrives.

---

## 3. Signal Stack — Ranked by Quality and Buildability

| Rank | Signal | Quality | Complexity | Phase | Note |
|---|---|---|---|---|---|
| 1 | Gamma Exposure (GEX) | High — mechanical, not sentiment | Medium-High | Build first | Structural backbone of the strategy |
| 2 | IV Skew + Δ% | Medium-High — forward-looking | Low-Medium | Build second | Level + rate of change both required |
| 3 | VIX slope + VVIX spike | Medium standalone, High combined | Low | Use now | `get_current_vix()` already available |
| 4 | Per-symbol IV Rank | Medium — caution/confirmation filter | Zero | Repurpose | Already computed in options bot |
| 5 | CBOE aggregate P/C ratio | Medium — contrarian extremes only | Medium | Build later | Use CBOE total, not per-symbol |
| 6 | Unusual options activity | Low-Medium | High | Defer | Needs volume baseline (weeks of data) |
| 7 | Dark pool flow | High in theory | Inaccessible | Skip | Not in IBKR retail API |

**The fundamental principle behind this ranking:** Signals 1–4 are all computable from data you already have access to (IBKR option chain, Greeks snapshots, yfinance VIX). Signals 5–7 either require data you don't have, infrastructure you'd need to build first, or are simply inaccessible at the retail API level.

---

## 4. Signal 1: GEX Deep Dive

### What it computes

GEX is computed per strike by multiplying each option contract's gamma by its open interest and the spot price. This gives a dollar-weighted measure of how much dealer hedging activity is anchored at each strike level.

```
Per-contract GEX:
  Call GEX = +gamma × open_interest × spot × 100   (positive — stabilising)
  Put GEX  = −gamma × open_interest × spot × 100   (negative — note the sign)

Net GEX per strike = sum of all call GEX + sum of all put GEX at that strike
```

The sign convention (calls positive, puts negative) follows the SpotGamma/Barchart standard. The intuition: calls create buying-on-dips dealer hedging; puts create selling-on-falls dealer hedging. When calls dominate, net GEX is positive and the environment is stabilising.

### Key outputs

**Gamma wall** — the strike with the highest positive net GEX. This is where dealer hedging pressure is strongest. Price is magnetically attracted to this level and tends to oscillate around it. This is your primary mean-reversion target.

**GEX flip level** — the first strike where the cumulative sum of net GEX (from highest strikes downward) crosses zero. Below this level, the net gamma environment flips negative and dealer hedging becomes destabilising. A break below the GEX flip is a momentum signal, not a mean-reversion opportunity.

**Total net GEX sign** — the sum of all net GEX values. Positive = stabilising environment overall. Negative = amplifying environment overall. This determines which trade mode the strategy operates in (see Section 14).

### The 0DTE priority

Always compute GEX from the front expiry — the nearest expiration date available. On SPY, 0DTE options are available Monday, Wednesday, and Friday. On QQQ, daily micro options are available every trading day. For NVDA, AAPL, MSFT, AMZN, GOOGL, TSLA — use the nearest weekly expiry.

The reason is pure gamma mechanics: a 0DTE ATM option has gamma 10–20× higher than the equivalent monthly option. Since GEX is weighted by gamma, 0DTE options dominate the map completely. Using a longer-dated expiry gives you a misleading picture of where intraday hedging pressure is actually concentrated.

### GEX refresh schedule

Compute GEX three times per day:
- **09:00 ET** — pre-market, using prior night's OI data. Establishes the structural map.
- **11:00 ET** — first intraday refresh. 0DTE positions open heavily in the first hour.
- **13:00 ET** — second intraday refresh. Post-lunch repositioning shifts OI.

Do not refresh more than once every 2 hours. More frequent refreshes pick up transient OI reporting noise and create flip-flopping signals.

### GEX as gravity, not destiny

GEX walls are not guaranteed reversal points. They are zones where the probability of a reversal is higher than average, because large participants are mechanically trading against the move. Failures happen in approximately 7–22% of cases (inverse of the 78–93% range containment figure). This is why the entry trigger (Section 12) requires confirmation that the wall is *actually holding* — a rejection wick with volume confirmation — before entering. Never enter simply because price is near a GEX level.

### Reference implementation

```python
from datetime import date
import pandas as pd

def compute_gex(client, symbol: str) -> dict:
    """
    Compute intraday GEX map from front-expiry option chain.
    Returns gamma_wall, gex_flip, is_stabilising, full net_gex series.
    """
    spot = client.get_spot_price(symbol)
    expiries = client.get_option_expiries(symbol)

    # Always use front expiry — 0DTE dominates intraday gamma (50–62% of SPX/SPY volume)
    front_expiry = min(
        expiries,
        key=lambda d: (date.fromisoformat(d) - date.today()).days
    )

    chain = client.get_option_chain(symbol, front_expiry, "ALL")
    snap  = client.get_option_snapshot(chain["code"].tolist())

    calls = snap[snap["option_type"] == "C"].copy()
    puts  = snap[snap["option_type"] == "P"].copy()

    # SpotGamma sign convention: calls positive, puts negative at source
    calls["gex"] =  calls["option_gamma"] * calls["option_open_interest"] * spot * 100
    puts["gex"]  = -puts["option_gamma"]  * puts["option_open_interest"]  * spot * 100

    # Aggregate per strike
    net_gex = (
        calls.set_index("strike_price")["gex"]
             .add(puts.set_index("strike_price")["gex"], fill_value=0)
    )

    gamma_wall     = net_gex.idxmax()
    # GEX flip: first strike where cumulative sum goes negative (acceleration zone)
    gex_flip       = net_gex.cumsum().lt(0).idxmax()
    is_stabilising = net_gex.sum() > 0     # True = dampening; False = amplifying

    return {
        "gamma_wall":      gamma_wall,
        "gex_flip":        gex_flip,
        "is_stabilising":  is_stabilising,
        "net_gex_series":  net_gex,         # full series — log this for later analysis
        "spot":            spot,
        "expiry":          front_expiry,
        "computed_at":     pd.Timestamp.now(),
    }
```

---

## 5. Signal 2: IV Skew + Intraday Rate of Change

### Conceptual understanding

IV skew measures the implied volatility difference between equidistant OTM puts and OTM calls, typically at the 25-delta point. A positive skew means put IV is higher than call IV — the market is paying a premium to protect against downside. The magnitude tells you *how much* downside protection is being demanded. The *change* in skew tells you whether that demand is increasing or decreasing.

**Why track the change, not just the level?** A skew of 4.0 (puts at 4 vol points above calls) is bearish if it was 2.0 an hour ago (rapidly steepening = increasing fear). The same reading of 4.0 is bullish if it was 6.0 an hour ago (rapidly flattening = fear subsiding). Absolute level without direction is ambiguous.

### Trading implications

| Skew condition | Interpretation | Bias |
|---|---|---|
| Skew low (≤2) and flat/falling | Complacency — market not protecting against downside | Long-friendly |
| Skew moderate (2–4) and stable | Normal hedging demand | Neutral |
| Skew high (≥4) or steeply rising | Elevated downside fear — smart money hedging | Short-friendly; avoid longs |
| Skew spiking rapidly (Δ% > +0.5 in 30 min) | Sudden demand surge — potential precursor to sell-off | Hard pause on longs |

### Reference implementation

```python
import time
from collections import deque
from datetime import date

class IVSkewMonitor:
    """
    Tracks 25-delta risk reversal per symbol.
    Records level history for intraday rate-of-change calculation.
    """

    def __init__(self, lookback_minutes: int = 30):
        self.history      = {}   # symbol → deque of (timestamp, skew_value)
        self.lookback_sec = lookback_minutes * 60

    def update(self, client, symbol: str) -> dict:
        expiries = client.get_option_expiries(symbol)
        front    = min(expiries, key=lambda d: (date.fromisoformat(d) - date.today()).days)

        chain = client.get_option_chain(symbol, front, "ALL")
        snap  = client.get_option_snapshot(chain["code"].tolist())

        puts  = snap[snap["option_type"] == "P"].copy()
        calls = snap[snap["option_type"] == "C"].copy()

        # Find nearest 0.25-delta contracts
        put_25d  = puts.iloc[(puts["option_delta"].abs() - 0.25).abs().argsort()[:1]]
        call_25d = calls.iloc[(calls["option_delta"] - 0.25).abs().argsort()[:1]]

        skew = float(put_25d["option_iv"].iloc[0]) - float(call_25d["option_iv"].iloc[0])

        # Maintain rolling history
        if symbol not in self.history:
            self.history[symbol] = deque()
        now = time.time()
        self.history[symbol].append((now, skew))

        # Prune entries older than lookback window
        while self.history[symbol] and (now - self.history[symbol][0][0]) > self.lookback_sec:
            self.history[symbol].popleft()

        # Rate of change over lookback window
        if len(self.history[symbol]) >= 2:
            oldest_skew = self.history[symbol][0][1]
            skew_delta  = skew - oldest_skew   # positive = steepening (bearish pressure increasing)
        else:
            skew_delta = 0.0

        return {
            "skew":         skew,
            "skew_delta":   skew_delta,
            # bearish_lean thresholds are configurable — these are starting defaults
            "bearish_lean": skew > 3.0 or skew_delta > 0.5,
        }
```

---

## 6. Signal 3: VIX Slope + VVIX Spike Detection

### Conceptual understanding

**VIX** (the CBOE Volatility Index) measures the market's expectation of 30-day implied volatility for the S&P 500. A rising VIX means the market is pricing in increasing uncertainty — this is bearish for mean-reversion long setups. A falling VIX confirms a rally and supports long-side entries.

**VVIX** (Volatility of VIX) measures how volatile VIX itself is expected to be — effectively the "fear of fear." When institutions anticipate a volatility regime change, they buy VIX options before VIX itself reacts. This makes VVIX a leading indicator that often spikes 15–30 minutes before VIX itself shows the stress. A VVIX reading 5%+ above its recent average is a warning to stop entering new long positions.

### The VIX hard block

VIX ≥ 25 is already the master safety switch in the options income bot. This same threshold applies here. Above 25, options mechanics break down — GEX walls fail more frequently, skew becomes unreliable, and dealer hedging becomes erratic. No new entries when VIX ≥ 25.

### Reference implementation

```python
import numpy as np
import yfinance as yf
from collections import deque

class VIXMonitor:
    """
    Polls VIX and VVIX every N minutes.
    Computes rolling slope (intraday direction) and VVIX spike flag.
    """

    def __init__(self, slope_window: int = 3):
        self.vix_history  = deque(maxlen=12)   # ~60 min at 5-min polling
        self.vvix_history = deque(maxlen=12)
        self.slope_window = slope_window       # number of readings for slope calculation

    def poll(self) -> dict:
        vix  = float(yf.Ticker("^VIX").fast_info["last_price"])
        vvix = float(yf.Ticker("^VVIX").fast_info["last_price"])

        self.vix_history.append(vix)
        self.vvix_history.append(vvix)

        vix_slope  = self._slope(list(self.vix_history)[-self.slope_window:])
        vvix_avg   = float(np.mean(list(self.vvix_history))) if len(self.vvix_history) > 1 else vvix
        vvix_spike = vvix > vvix_avg * 1.05   # 5% above recent average = spike warning

        return {
            "vix":              vix,
            "vix_slope":        vix_slope,     # positive = rising (bearish for longs)
            "vvix":             vvix,
            "vvix_spike":       vvix_spike,    # True = early vol-regime warning, pause new longs
            "hard_block":       vix >= 25,     # absolute no-trade threshold
            "ok_for_long":      vix < 25 and vix_slope <= 0.0 and not vvix_spike,
            "ok_for_short":     vix < 25 and (vix_slope >= 0.0 or vvix_spike),
        }

    @staticmethod
    def _slope(values: list) -> float:
        if len(values) < 2:
            return 0.0
        x = np.arange(len(values))
        return float(np.polyfit(x, values, 1)[0])
```

---

## 7. Signal 4: Per-Symbol IV Rank (Repurposed)

### Conceptual understanding

IV Rank (IVR) measures where the current implied volatility of a symbol sits relative to its 52-week range. It is already computed for every watchlist symbol in the options income bot and requires zero new code.

For the scalping bot, IVR plays a different role than in the options bot:

**High IVR (≥70)** on a scalping candidate means the options market is pricing in a large move for that symbol. This is a caution flag for mean-reversion longs — the market "knows" something, and fading into a potentially news-driven move is dangerous. Do not attempt reversion longs on high-IVR names.

**Low IVR (≤20)** means options are cheap and the market expects calm continuation. This supports momentum continuation trades but reduces conviction for counter-trend shorts.

**Mid IVR (20–70)** is the neutral zone where IVR is not a blocking condition.

### Gate condition 5

IVR is Gate Condition 5 in the confluence gate (see Section 10):
- If IVR ≥ 70 and trade direction is LONG (reversion): **block**
- If IVR ≤ 20 and trade direction is SHORT: **reduce conviction** (can still trade, but size at 50%)
- Otherwise: **pass**

---

## 8. Signal 5: CBOE Aggregate Put/Call Ratio (Later)

### Conceptual understanding

The put/call ratio (PCR) measures the number of puts traded relative to calls. When PCR is very high (≥1.5), the market is overwhelmingly buying puts — extreme fear that is often a contrarian buy signal. When PCR is very low (≤0.7), the market is buying calls overwhelmingly — complacency that is often a contrarian sell signal.

**Important: use CBOE aggregate PCR, not per-symbol.** Per-symbol PCR is heavily distorted by the options structure of each stock — TSLA and NVDA are structurally put-heavy names, so their PCR is always elevated and tells you little. The CBOE total equity or SPX PCR is a cleaner broad-market sentiment gauge.

**Build priority:** This is a Phase 2 enhancement. The core strategy functions without it, and PCR is a slowly-moving signal (you'd check it once at open and maybe once mid-day) rather than an intraday trigger. Add it after GEX, skew, and VIX are working and validated.

---

## 9. Regime Integration — Existing Infrastructure as Gate

### How the existing regime system plugs in

The HMM quantitative regime detector and LLM regime bridge are already built and running in the options income bot. For the scalping bot, they serve as the master gate — if regime signals conflict or are unfavourable, no trades are taken regardless of what GEX, skew, or VIX show.

**HTF regime (daily bars, updated every 2h):**
This provides macro context. States: `bull`, `bear`, `neutral`, `high_vol`.
- `high_vol` → **absolute block on all new entries**, no exceptions
- `bull` → long-side entries favoured, short entries require strong skew/VIX confirmation
- `bear` → short-side entries favoured, long entries require strong confluence
- `neutral` → both directions allowed, other signals determine bias

**LTF regime (1h bars, updated every 30min):**
This provides intraday direction context. States: `UPTREND`, `DOWNTREND`, `RANGING`, `TRANSITION` (LLM output), plus `bull`/`bear`/`neutral`/`high_vol` (HMM output).
- `TRANSITION` state → **block new entries**. Regime is changing; GEX walls are less reliable during transitions.
- LTF must agree with or be neutral relative to the trade direction

**Regime confidence:**
The LLM bridge returns a confidence score of 1–5. While a hard threshold of ≥3 is a reasonable starting point, defer enforcing it as a code block until you've calibrated the confidence distribution across real intraday data. Log it always; enforce it after you see the distribution.

### Direction mapping

| HTF state | LTF state | Allowed directions |
|---|---|---|
| high_vol | any | NO TRADE |
| bull | UPTREND | LONG only |
| bull | RANGING | LONG (preferred), SHORT (reduced size) |
| bull | TRANSITION | NO TRADE |
| neutral | any (non-TRANSITION) | LONG and SHORT |
| bear | DOWNTREND | SHORT only |
| bear | RANGING | SHORT (preferred), LONG (reduced size) |
| bear | TRANSITION | NO TRADE |

---

## 10. Strategy Architecture — Full Specification

### Core thesis

GEX walls and flip levels are structural gravity fields created by dealer hedging mechanics. In a positive-GEX environment (the majority of sessions), price oscillates around the gamma wall and dealer flows systematically absorb directional moves. The strategy fades moves toward the gamma wall with confirmation from regime, skew, and VIX. In the rarer negative-GEX environment, dealer flows amplify directional moves and the strategy switches to momentum mode — only if regime and skew strongly confirm a direction.

### Instruments

**Primary (liquidity, safety):** SPY, QQQ
- Deepest options market → highest GEX signal quality
- Tightest spreads → best fill quality
- 0DTE available most/every trading day

**Secondary (edge from volatility):** NVDA, TSLA
- Higher IV → richer GEX map (more gamma per strike)
- More intraday range → larger profit potential
- Wider spreads and more gap risk → smaller position sizes

AAPL, MSFT, AMZN, GOOGL are on the watchlist but produce lower-quality GEX signals due to lower single-stock IV. Trade them only when confluence is strong across all five gates.

### Timeframes

**5m bars — primary.** All entries and exits are based on 5-minute bar closes. Each bar represents substantial volume (hundreds of millions of dollars on SPY), filtering out bid-ask noise while still being responsive enough for intraday moves.

**15m bars — context.** Used to confirm trend direction within the GEX range. If the 15m chart shows a clear downtrend, only take short entries on the 5m even if the GEX map is positive.

**1m bars — defer.** Available but adds noise during the paper trading phase. Add 1m precision timing only after the 5m strategy is validated.

### Session rules

| Time | Rule |
|---|---|
| Before 09:00 ET | Compute pre-market GEX map; no trading |
| 09:00–09:30 ET | VIX + VVIX poll begins; regime confirmed |
| 09:30–09:45 ET | Opening 15 minutes: observe only, do not enter |
| 09:45–14:30 ET | Active trading window |
| 14:30 ET | **Hard cutoff — no new entries after this time** |
| 14:30–16:00 ET | Manage open positions only; close by 15:45 ET |
| 11:00 ET | First GEX map refresh |
| 13:00 ET | Second GEX map refresh |

The 14:30 hard cutoff exists because 0DTE options in the final 90 minutes have gamma so high that a $1 move in SPY requires dealers to adjust hedges by 5–10× more shares than in the morning. GEX walls that were reliable all morning can flip from stabilising to amplifying in minutes as 0DTE strikes cross ATM. Do not fight this — simply stop entering new positions.

The 09:30–09:45 observation window exists because the first 15 minutes after open are dominated by overnight order flow clearing, retail gap fills, and market maker positioning. GEX levels from pre-market may shift substantially before settling. Waiting for the initial volatility to resolve produces significantly cleaner entries.

### The five-gate confluence system

All five conditions must pass (minimum 4 of 5) before an entry is considered. Gates are evaluated in priority order — if Gates 1 or 2 fail, stop checking.

**Gate 1 — Regime alignment (hard gate; failure blocks trade):**
- HTF regime is not `high_vol`
- LTF regime is not `TRANSITION`
- Regime direction aligns with or is neutral to the proposed trade direction

**Gate 2 — VIX and VVIX (hard gate; VVIX spike blocks long entries):**
- VIX < 25 (absolute block above this level)
- No VVIX spike (VVIX ≤ 1.05× its recent average)
- VIX slope direction supports trade (falling for longs, flat/rising for shorts)

**Gate 3 — IV skew alignment:**
- For long entries: skew ≤ 3.0 (no elevated put demand) and skew_delta ≤ 0 (not steepening)
- For short entries: skew ≥ 3.0 OR skew_delta ≥ +0.5 (put demand present or increasing)

**Gate 4 — GEX level proximity:**
- Price is within 0.3% of a gamma wall (mean-reversion setup) OR
- Price is within 0.3% of the GEX flip level (momentum setup, negative-GEX mode only)

**Gate 5 — IV Rank filter:**
- For long reversion entries: IVR < 70 (avoid fading into potentially news-driven moves)
- For short entries: IVR > 20 (some elevated IV confirms downside expectation)
- Mid-range IVR (20–70): always passes

**The 4-of-5 rule:** The system passes with 4 gates satisfied. This prevents the system from being over-filtered into never trading while still requiring strong confluence. Gate 1 and Gate 2 are hard gates — failing either blocks the trade regardless of the 4-of-5 rule.

---

## 11. Instrument Selection — Stock vs Option

Once a trade setup qualifies (gates pass, trigger fires), the bot must decide *what* to buy. This section defines the decision logic for choosing between the underlying stock and an options contract, for both long and short directions.

### Why this decision matters

Buying stock gives you linear, theta-free exposure to the move. Buying an option gives you leveraged, defined-risk exposure — but you are paying for time value that erodes during your 60-90 minute hold. The wrong instrument choice can turn a correct directional call into a losing trade: a stock buy on a small reversion move is fine; a 0DTE call buy on the same move may expire worthless after theta decay eats the premium during the hold.

Conversely, buying stock when a large momentum move is developing wastes leverage. A 0.5% move in SPY is a moderate stock gain but can be 3-4× on an ATM call.

**The unifying principle:** the expected profit from the move must justify the option premium cost (intrinsic + time value decay during the hold). If it does, buy the option. If it doesn't, buy the stock.

### The IVR inversion — a critical conceptual shift

IVR means opposite things depending on which side of the options trade you are on:

| Context | High IVR | Low IVR |
|---|---|---|
| Options income bot (selling options) | Good — sell expensive premium | Bad — premium too thin |
| Scalping bot (buying options) | Bad — overpaying for vega | Good — options are cheap |

The scalping bot is a *buyer* of options. High IVR is a cost headwind, not a tailwind. This is the most important conceptual distinction between the two strategies sharing the same codebase.

### Short-side default: puts over stock shorts

Shorting stock requires borrowing shares, paying stock borrow fees, and carries unlimited upside risk. More importantly, it introduces a forced-close pressure that conflicts with disciplined position management — prime brokers can recall borrowed shares, and margin requirements can force closure at the worst moment.

**The bot defaults to buying puts for all bearish trades.** Stock shorting is a config flag (`scalp.allow_short_stock: false` by default) and should remain disabled unless there is a specific operational reason to enable it (e.g., a symbol where puts are illiquid or spreads are prohibitively wide). Even when enabled, the same instrument selection scoring applies — the bot only recommends stock shorting when the score clearly favours it, and the borrow rate check must pass first (see Factor 6).

---

### Instrument comparison reference

| Factor | Buy stock (long) | Buy call | Short stock | Buy put |
|---|---|---|---|---|
| Capital required | Full notional | Premium only | ~150% Reg T margin | Premium only |
| Max loss | Unlimited (stops used) | Premium paid | Unlimited (squeeze risk) | Premium paid |
| Leverage | 1:1 | ~4–7× on delta-equivalent move | ~2:1 margin | ~4–7× |
| Delta | +1.0 per share | +0.40–0.70 | −1.0 per share | −0.40–0.70 |
| Gamma | 0 (linear) | Positive — accelerates gains | 0 | Positive |
| Theta | 0 | Negative — erodes if price stalls | 0 | Negative |
| Vega | 0 | Positive — benefits from IV rise | 0 | Positive |
| Borrow cost | None | None | Borrow fee + margin interest | None |
| Best for | Steady/gradual move, high IV, 30–90 min hold | Fast momentum, low IV, expected IV pop, 5–45 min | Slow grind lower, cheap borrow, no IV edge | Sharp fast drop, capped risk priority, IV pop expected |
| Worst for | Large expected move (wastes leverage) | Slow move, high IV, wrong timing | Unlimited risk, borrow recall, short squeeze | Slow move, IV crush, wrong timing |

**Expected hold time by signal type** (used in theta burden assessment):
- Trigger A (rejection wick, reversion): 20–60 minutes
- Trigger B (EMA/VWAP, reversion): 30–75 minutes
- Trigger C (flip break, momentum): 5–30 minutes

Momentum signals (Trigger C) produce the fastest moves — options leverage is most valuable here because the move happens before theta erodes premium meaningfully. Reversion signals (Triggers A/B) are slower and theta accumulates during the hold — stock is preferred unless other factors strongly favour options.

---

### The six instrument selection factors

**Factor 1 — IVR level (most important)**

| IVR range | Implication for buying options | Score adjustment |
|---|---|---|
| < 25 (low) | Options cheap — excellent time to buy | Strong prefer option (+2) |
| 25–45 (moderate-low) | Reasonable option cost | Slight prefer option (+1) |
| 45–65 (moderate-high) | Options starting to get expensive | Neutral (0) |
| 65–80 (high) | Options expensive — premium a significant drag | Slight prefer stock (−1) |
| > 80 (very high) | Options very expensive — theta will likely exceed profit | Strong prefer stock (−2) |

**Factor 2 — Trade mode (GEX environment)**

| Trade mode | Implication | Score adjustment |
|---|---|---|
| Momentum (negative GEX, flip break) | Large, fast move expected — leverage is valuable | Prefer option (+2) |
| Reversion (positive GEX, wall fade) | Small, slow move expected — theta drag matters more | Prefer stock (+1) |

**Factor 3 — Expected move distance (T1 target)**

This is computed directly from the GEX map: the distance from entry to the nearest T1 target (next GEX level or VWAP) as a percentage of spot.

| T1 distance | Implication | Score adjustment |
|---|---|---|
| > 0.6% | Large move — options leverage amplifies meaningfully | Prefer option (+2) |
| 0.4–0.6% | Moderate move — options viable if IVR is not high | Slight prefer option (+1) |
| 0.25–0.4% | Small move — theta drag may equal or exceed profit | Neutral (0) |
| < 0.25% | Very small move — theta will likely exceed profit | Prefer stock (+2) |

**Factor 4 — Time of day (theta acceleration)**

0DTE options lose value slowly in the morning and accelerate dramatically after 13:00 ET as gamma and theta interact. Buying 0DTE options in the afternoon means fighting a steep decay curve even if the move happens.

| Time (ET) | 0DTE theta impact | Score adjustment |
|---|---|---|
| 09:45–11:30 | Low theta drag — most of the day remains | No adjustment (0) |
| 11:30–13:00 | Moderate theta drag | Slight prefer stock (−1) |
| 13:00–14:30 | Accelerating theta drag | Prefer stock (−2) |

For non-0DTE options (nearest weekly with 2–7 DTE), theta decay over 60-90 minutes is much smaller — apply no time-of-day adjustment.

**Factor 5 — Bid-ask spread quality**

Wide spreads on options mean you lose money before the trade even starts. For SPY/QQQ options this is rarely an issue. For single stocks (NVDA, TSLA especially) at volatile moments, spreads can widen substantially.

| Spread as % of mid price | Implication | Score adjustment |
|---|---|---|
| < 2% | Tight — options viable | No adjustment (0) |
| 2–5% | Moderate — acceptable | Slight prefer stock (−1) |
| > 5% | Wide — spread cost too large | Prefer stock (−2) |

**Factor 6 — ATM straddle implied move vs T1 target**

This is the most precise cost-efficiency check. The ATM straddle price (call premium + put premium at the nearest ATM strike) divided by spot gives the market's expected move magnitude for that expiry. Comparing this to your T1 target distance tells you whether the option is cheap or expensive *relative to your specific trade*.

```python
straddle_implied_move = (atm_call_mid + atm_put_mid) / spot   # e.g. 0.008 = 0.8%
efficiency_ratio = t1_distance / straddle_implied_move
# Ratio > 1.0 → your target exceeds what the market expects → option cheap for your trade
# Ratio < 0.5 → option priced for a move 2× larger than your target → overpaying
```

| Efficiency ratio | Interpretation | Score adjustment |
|---|---|---|
| > 1.2 | Option cheap relative to expected profit — strong value | Prefer option (+2) |
| 0.8–1.2 | Option fairly priced for the expected move | Slight prefer option (+1) |
| 0.5–0.8 | Option priced for a larger move than you're targeting | Neutral (0) |
| < 0.5 | Option significantly overpriced for your target — clear headwind | Prefer stock (−2) |

**Vega tailwind condition (modifies Factor 1):** If `skew_delta > 0` (put demand rising in the last 30 minutes) AND `ivr < 50`, the option is cheap *and* IV is rising — you benefit from both the move and the IV expansion. In this case, add +1 to the IVR score from Factor 1 (capped at +2 total from Factor 1).

---

### The instrument selection scoring model

```python
def select_instrument(
    ivr:                    float,   # IV Rank 0–100
    trade_mode:             str,     # 'reversion' | 'momentum'
    t1_distance:            float,   # T1 target distance as decimal (e.g. 0.004 = 0.4%)
    entry_time:             str,     # "HH:MM" ET
    option_spread_pct:      float,   # bid-ask spread as % of option mid price
    straddle_implied_move:  float,   # (atm_call_mid + atm_put_mid) / spot
    skew_delta:             float,   # from IVSkewMonitor — rate of change over 30 min
    direction:              str,     # "LONG" or "SHORT"
    borrow_rate_pct:        float,   # annualised borrow rate (0.0 if short_stock disabled)
    config:                 dict,
) -> dict:
    """
    Returns instrument recommendation and supporting rationale.
    Six factors, each scored ±2. Score ≥ 2 → option. Score ≤ −1 → stock. Neutral → stock.
    """
    score = 0

    # ── Factor 1: IVR level ──────────────────────────────────────────────────
    if ivr < 25:
        ivr_score = 2
    elif ivr < 45:
        ivr_score = 1
    elif ivr < 65:
        ivr_score = 0
    elif ivr < 80:
        ivr_score = -1
    else:
        ivr_score = -2

    # Vega tailwind bonus: cheap IV AND actively rising put demand
    if skew_delta > 0 and ivr < 50:
        ivr_score = min(ivr_score + 1, 2)   # cap at +2

    score += ivr_score

    # ── Factor 2: Trade mode (GEX environment) ───────────────────────────────
    if trade_mode == "momentum":
        score += 2
    else:
        score -= 1   # reversion: gradual move, theta drag matters

    # ── Factor 3: Expected move distance (T1 target) ─────────────────────────
    if t1_distance > 0.006:
        score += 2
    elif t1_distance > 0.004:
        score += 1
    elif t1_distance > 0.0025:
        score += 0
    else:
        score -= 2   # move too small — theta will exceed profit

    # ── Factor 4: Time of day (0DTE theta acceleration) ──────────────────────
    hour         = int(entry_time.split(":")[0])
    minute       = int(entry_time.split(":")[1])
    time_decimal = hour + minute / 60.0
    if time_decimal >= 13.0:
        score -= 2
    elif time_decimal >= 11.5:
        score -= 1

    # ── Factor 5: Bid-ask spread quality ─────────────────────────────────────
    if option_spread_pct > 0.05:
        score -= 2
    elif option_spread_pct > 0.02:
        score -= 1

    # ── Factor 6: ATM straddle implied move vs T1 target ─────────────────────
    if straddle_implied_move > 0:
        efficiency = t1_distance / straddle_implied_move
        if efficiency > 1.2:
            score += 2   # option cheap for our expected move
        elif efficiency > 0.8:
            score += 1
        elif efficiency > 0.5:
            score += 0
        else:
            score -= 2   # option priced for 2× our target — overpaying

    # ── Short-side: borrow check (only when allow_short_stock is true) ────────
    # NOTE on borrow rate data: IBKR exposes indicative borrow rates via the
    # SLB (Securities Loan Borrow) tool in TWS/Client Portal, but there is no
    # direct ib_insync/ibkr-connector API endpoint yet. For now, pass 0.0
    # (short stock is disabled by default). If allow_short_stock is ever enabled,
    # poll borrow rate manually from TWS or extend ibkr-connector with SLB support.
    allow_short = config.get("scalp", {}).get("allow_short_stock", False)
    if direction == "SHORT":
        if not allow_short:
            return {
                "instrument": "PUT",
                "score":      score,
                "overridden": True,
                "reason":     "stock_short_disabled — buying put by default",
            }
        # When enabled: expensive borrow pushes toward puts
        if borrow_rate_pct > 10.0:
            score += 3   # very expensive borrow — strong prefer put
        elif borrow_rate_pct > 5.0:
            score += 2   # meaningful borrow cost — prefer put
        elif borrow_rate_pct > 2.0:
            score += 1   # moderate borrow — slight prefer put

    # ── Decision ─────────────────────────────────────────────────────────────
    threshold = config.get("scalp", {}).get(
        "instrument_selection", {}
    ).get("option_score_threshold", 2)

    if score >= threshold:
        instrument = "CALL" if direction == "LONG" else "PUT"
    else:
        instrument = "STOCK"

    return {
        "instrument": instrument,   # "STOCK" | "CALL" | "PUT"
        "score":      score,
        "overridden": False,
        "reason":     _build_reason(
            ivr, trade_mode, t1_distance, entry_time,
            option_spread_pct, straddle_implied_move, skew_delta,
        ),
    }
```

**Threshold interpretation:**
- Score ≥ 2 → Buy option. Multiple factors favour options; leverage is worth the premium cost. With six factors at ±2 each, the maximum possible score is +12 (all factors strongly prefer option) and minimum is −12 (all strongly prefer stock). A threshold of 2 means at least a net two-point lean toward options after all factors are balanced.
- Score 0–1 → Buy stock. Neutral or marginal lean toward options — stock is simpler and avoids theta risk.
- Score ≤ −1 → Buy stock. One or more factors clearly penalise options.

The default-to-stock in the neutral zone is intentional. During paper trading validation, analysing why a trade won or lost is easier without option Greeks complicating the picture. Migrate more setups to options after the strategy is validated across 30+ stock trades.

---

### Option contract selection (when instrument = CALL or PUT)

Once the bot decides to use an option, it must select the specific contract. The goal is maximum sensitivity to the expected move with minimum wasted premium.

**Delta target: 0.50–0.65 (ATM to slightly in-the-money)**

This range gives the best gamma sensitivity per dollar of premium. Deep OTM options (delta < 0.30) need a large move to overcome the premium cost. Deep ITM options (delta > 0.80) behave like stock with extra cost. ATM-to-slightly-ITM is the sweet spot for scalping.

**Expiry selection:**

| Condition | Preferred DTE | Rationale |
|---|---|---|
| SPY/QQQ, time before 11:30 ET, 0DTE available | 0 DTE | Gamma is maximum — small moves produce large option gains. Only viable in morning. |
| SPY/QQQ, time after 11:30 ET, or 0DTE unavailable | 1–5 DTE (nearest weekly) | Avoids afternoon theta cliff while maintaining high gamma |
| Single stocks (NVDA, TSLA, AAPL, etc.) | 3–7 DTE (nearest weekly) | 0DTE less liquid on single stocks; weekly provides cleaner fills |
| Momentum trade mode, any symbol | 1–7 DTE | Slightly longer DTE gives the momentum move time to develop without theta eating the gain |

**Maximum acceptable bid-ask spread:** 5% of mid price. Above this, the round-trip cost is too large for a scalping strategy.

**Fetching the ATM straddle implied move (for Factor 6):**

```python
def get_straddle_implied_move(client, symbol: str, expiry: str) -> float:
    """
    Returns the straddle implied move as a fraction of spot.
    (atm_call_mid + atm_put_mid) / spot
    Used as Factor 6 in select_instrument().
    """
    spot  = client.get_spot_price(symbol)
    # Find nearest ATM strike
    chain = client.get_option_chain(symbol, expiry, "ALL")
    all_strikes = sorted(chain["strike_price"].unique())
    atm_strike  = min(all_strikes, key=lambda s: abs(s - spot))

    # Get both ATM call and put
    atm_codes = chain[chain["strike_price"] == atm_strike]["code"].tolist()
    snap = client.get_option_snapshot(atm_codes)

    call_mid = snap[snap["option_type"] == "C"]["mid_price"].iloc[0] if not snap.empty else 0
    put_mid  = snap[snap["option_type"] == "P"]["mid_price"].iloc[0] if not snap.empty else 0

    if spot > 0:
        return (call_mid + put_mid) / spot
    return 0.0
```

**Contract selection:**

```python
def select_option_contract(client, symbol, direction, config) -> str:
    """
    Returns OCC contract code for the best matching option.
    direction: 'LONG' (buy call) or 'SHORT' (buy put)
    """
    right = "CALL" if direction == "LONG" else "PUT"
    expiries = client.get_option_expiries(symbol)
    spot = client.get_spot_price(symbol)

    # Select expiry based on time of day and symbol type
    from datetime import date
    import datetime
    now_et = datetime.datetime.now()  # assume ET timezone conversion handled
    is_primary = symbol in ["SPY", "QQQ"]
    use_0dte   = is_primary and now_et.hour < 11.5  # morning only

    if use_0dte:
        target_expiry = min(expiries, key=lambda d: (date.fromisoformat(d) - date.today()).days)
    else:
        # Find nearest expiry with 1–7 DTE
        candidates = [e for e in expiries
                      if 1 <= (date.fromisoformat(e) - date.today()).days <= 7]
        target_expiry = candidates[0] if candidates else expiries[0]

    chain = client.get_option_chain(symbol, target_expiry, right)
    snap  = client.get_option_snapshot(chain["code"].tolist())

    # Filter to delta target range (0.50–0.65)
    delta_col = "option_delta"
    if right == "PUT":
        snap = snap.copy()
        snap[delta_col] = snap[delta_col].abs()  # puts have negative delta

    candidates = snap[snap[delta_col].between(0.50, 0.65)]
    if candidates.empty:
        # Fallback: nearest to 0.55
        candidates = snap.iloc[(snap[delta_col] - 0.55).abs().argsort()[:3]]

    # Filter out wide spreads (> 5% of mid)
    candidates = candidates.copy()
    candidates["mid"]        = (candidates["bid_price"] + candidates["ask_price"]) / 2
    candidates["spread_pct"] = (candidates["ask_price"] - candidates["bid_price"]) / candidates["mid"]
    candidates = candidates[candidates["spread_pct"] <= 0.05]

    if candidates.empty:
        return None  # no suitable contract — fall back to stock

    # Select highest delta within range (most responsive to move)
    best = candidates.loc[candidates[delta_col].idxmax()]
    return best["code"]
```

---

### Summary decision table

The straddle efficiency column shows `t1_distance / straddle_implied_move` — values above 1.0 mean the option is cheap for your target.

| Setup | IVR | Mode | T1 dist | Time | Straddle eff | Recommendation |
|---|---|---|---|---|---|---|
| SPY reversion at gamma wall | 35 | Reversion | 0.2% | 10:15 | 0.4 | Stock (move too small; option overpriced for target) |
| SPY reversion at gamma wall | 20 | Reversion | 0.4% | 10:15 | 0.9 | Call (cheap IV, fair straddle efficiency) |
| SPY momentum flip break | 45 | Momentum | 0.7% | 09:55 | 1.4 | Call (large fast move, option cheap for target) |
| NVDA reversion | 75 | Reversion | 0.35% | 11:00 | 0.5 | Stock (expensive IV + borderline efficiency) |
| NVDA momentum | 40 | Momentum | 0.8% | 10:30 | 1.1 | Call (large move, fair pricing, morning timing) |
| Any symbol, bearish | any | any | any | any | any | Put (stock short disabled by default) |
| SPY reversion | 30 | Reversion | 0.3% | 13:45 | 0.7 | Stock (afternoon theta wipes factor advantage) |
| QQQ momentum | 25 | Momentum | 0.6% | 13:00 | 1.3 | Call (cheap IV + high straddle eff offsets time penalty) |

---

## 12. Entry Triggers — Detailed Rules

Gates tell you *where* and *whether* to trade. Triggers tell you *when* to enter on the 5-minute bar close. Never enter on a bar open or mid-bar — always wait for the 5m close.

### Trigger A — Rejection at gamma wall (primary, lower risk)

This is the cleanest mean-reversion entry. Use in positive-GEX environments.

**Long rejection trigger (price touched wall from above, rejected higher):**
1. 5m bar wicks below the gamma wall (low < gamma_wall) but closes *above* it
2. Volume on the wick bar is declining relative to the prior 3-bar average
3. RSI (14, 5m) is ≤ 35 at bar close
4. 5m close is above the EMA(8) or reclaims EMA(8) at the close

**Short rejection trigger (price touched wall from below, rejected lower):**
1. 5m bar wicks above the gamma wall (high > gamma_wall) but closes *below* it
2. Volume on the wick bar is declining relative to the prior 3-bar average
3. RSI (14, 5m) is ≥ 65 at bar close
4. 5m close is below the EMA(8) or breaks below EMA(8) at the close

The declining volume on the wick is critical — it confirms the probe into the GEX wall was absorbed (dealers hedged it) rather than broken through. A wick on expanding volume is a warning sign that the wall is breaking; do not fade it.

### Trigger B — EMA/VWAP deviation (secondary, more common)

Use when price is near a GEX level but hasn't produced a clean rejection wick. Lower conviction than Trigger A.

**Long EMA/VWAP trigger:**
1. Price has deviated ≥ 0.4% below VWAP OR has pulled back to and closed above EMA(8) near the gamma wall
2. RSI (14, 5m) ≤ 35
3. All five gates pass

**Short EMA/VWAP trigger:**
1. Price has deviated ≥ 0.4% above VWAP OR has extended above EMA(8) near the gamma wall (resistance)
2. RSI (14, 5m) ≥ 65
3. All five gates pass

### Trigger C — GEX flip break (momentum, negative-GEX only)

Use only when `is_stabilising = False` in the GEX map AND regime and skew strongly confirm the direction.

**Bearish flip break:**
1. 5m bar closes below the GEX flip level
2. MACD histogram (5m) is expanding in the negative direction (increasing bearish momentum)
3. Volume on the break bar is above the 20-bar average
4. Skew is elevated (≥ 3.0) or steepening (skew_delta ≥ +0.5)

**Bullish flip break (rarer — negative GEX with bullish regime):**
1. 5m bar closes above the GEX flip level
2. MACD histogram expanding positive
3. Volume above 20-bar average
4. Skew flat or falling

---

## 13. Position Management — Stops, Targets, Exits

### Stop placement

Use ATR (Average True Range, 14-period, on 5m bars). Compute ATR at the time of entry.

- **Long stop:** entry bar's low minus (0.8–1.2 × ATR)
- **Short stop:** entry bar's high plus (0.8–1.2 × ATR)

Use the tighter multiplier (0.8×) for Trigger A (rejection wick — the wall just held, so a move back through it is a clear failure). Use the wider multiplier (1.2×) for Trigger B (more room for noise before the thesis fails).

Never set stops at round numbers (e.g., "stop at $580.00"). Round numbers are where every retail trader has their stop — you will get swept by liquidity-hunting before the real move begins.

### Target structure

Split every position into two halves at entry.

**Target 1 — 50% of position:**
Close half at the next GEX level in the direction of the trade, or at VWAP (whichever is closer). After T1 is hit, move stop on the remaining half to breakeven (entry price). This guarantees the trade cannot become a loser after T1 is hit.

**Target 2 — remaining 50%:**
Close at the GEX flip zone (for reversion trades, this is the maximum structural target), or at 2.5–3:1 risk-reward ratio, or at a 15m trend exhaustion signal (RSI divergence, MACD cross). Trail the stop on T2 using a 1× ATR trail once price has moved 1.5× ATR from entry.

### Option-specific position management

When the instrument is a CALL or PUT, three additional rules apply on top of the standard stock rules.

**Stop on the option, not the underlying.** Set the option stop as a percentage of premium paid, not derived from the stock's ATR. A 40–50% loss of option premium is the equivalent of the ATR stop on the stock. The bot computes the entry option mid price and places a stop at `entry_option_price × 0.50`. This is simpler and avoids the problem of converting a stock-level stop into an option price under changing delta/gamma.

**The 0DTE theta escape rule.** For 0DTE options specifically: if the position has not reached T1 within 45 minutes of entry, close the entire position regardless of P&L. After 45 minutes a 0DTE option has lost a significant portion of its remaining time value, and the expected-value calculation that justified the trade no longer holds. Weekly options (1–7 DTE) follow the standard 60–90 minute maximum hold instead.

**Position sizing for options.** Risk per trade is expressed in premium dollars, not notional. For a `max_risk_per_trade_pct` of 0.75%, compute the maximum dollar loss allowed, then size the number of contracts so that if the option goes to zero (total loss), the loss equals that dollar amount. This means option positions are smaller in contract count than they appear — always size to the full-loss scenario, not to a delta-adjusted stock equivalent.

```python
# Option position sizing
max_loss_dollars  = account_value * config["scalp"]["risk"]["max_risk_per_trade_pct"]
option_premium    = entry_option_price * 100   # per contract (100 shares/contract)
contracts         = int(max_loss_dollars / option_premium)
contracts         = max(1, contracts)           # always at least 1 contract
```

### Hard exits

- **Maximum hold — stock:** 60–90 minutes from entry.
- **Maximum hold — 0DTE option:** 45 minutes, or until T1 hit, whichever comes first.
- **Maximum hold — weekly option:** 60–90 minutes (same as stock).
- **Hard session exit:** All positions closed by 15:45 ET regardless of instrument or P&L.
- **14:30 ET rule:** No new entries after 14:30 ET. Manage existing positions; do not add.
- **VIX spike exit:** VIX spikes ≥5% intraday → close all positions immediately at market.

### Risk per trade

- Paper trading phase: 0.75% of account per trade
- After validation gate passed: scale to 0.5–1.0% based on gate score strength
- Daily maximum loss: 2.5% of account — if hit, no new entries for the remainder of the session
- For options: size to full-loss scenario (see option sizing formula above)

### Daily trade cap

Maximum 4–6 trades per day. This is not an arbitrary rule — it reflects the number of genuinely high-quality setups available in a normal session. More trades typically means lower-quality entries are being accepted. The daily cap forces selectivity.

---

## 14. GEX Mode Switching

The GEX environment (`is_stabilising`) determines which trade mode the strategy operates in. This should be evaluated once at the start of each trading session and updated at each GEX refresh.

```python
def get_trade_mode(gex_map: dict, skew: dict, vix: dict) -> str:
    """
    Returns: 'reversion' | 'momentum' | 'no_trade'
    Determines which trigger set to use for the session.
    """
    # VVIX spike always pauses entries regardless of GEX environment
    if vix["vvix_spike"]:
        return "no_trade"

    # VIX hard block
    if vix["hard_block"]:
        return "no_trade"

    if gex_map["is_stabilising"]:
        # Positive net GEX: dampening environment
        # Dealer hedging absorbs moves → fade at walls
        return "reversion"

    else:
        # Negative net GEX: amplifying environment
        # Dealer hedging accelerates moves → ride breakouts
        # Only trade if strong directional confluence (avoid random momentum bets)
        if skew["skew_delta"] > 0.5 and not vix["ok_for_long"]:
            return "momentum"     # bearish momentum setup
        # Without strong confluence, negative GEX is a signal to reduce activity
        return "no_trade"

# Trade mode implications:
#
# 'reversion' — use Trigger A (rejection wick) or Trigger B (EMA/VWAP)
#               fade moves toward gamma wall
#               T1 = gamma wall / VWAP, T2 = GEX flip
#
# 'momentum'  — use Trigger C (flip break) only
#               enter on flip level break with MACD + volume
#               T1 = 1.5:1 R:R, T2 = 3:1 R:R or MACD exhaustion
#
# 'no_trade'  — skip session or wait for conditions to improve
```

**Practical note:** The vast majority of trading sessions will be in reversion mode. Momentum mode (negative net GEX) occurs most frequently during high-volatility sell-off days, which are also the days where you're most likely to have other flags (VVIX spike, VIX ≥ 25) blocking entries anyway. In practice, momentum mode will produce a small number of high-conviction trades per month.

---

## 15. Level 2 Order Book — Decision

**Decision: defer the $34/month subscription.**

The alpha in this strategy comes from GEX levels (option chain), skew (option snapshots), and regime (daily/hourly bars). None of these require knowing where the bids and offers are stacked at the moment of entry. You are entering at structurally defined levels where you already know large participants are mechanically forced to transact — you don't need to read the microstructure to find those levels.

L2 would improve *execution quality* — helping you set limit orders that fill reliably rather than chasing. But execution quality is only a bottleneck after the strategy is validated and you're trading real size. During paper trading validation, market orders or generous limit orders are fine.

**When to subscribe:** After 30+ paper trades with ≥55% win rate and ≥1.5:1 actual R:R. If fill quality is materially impacting P&L (e.g., getting slipped more than 0.1% on SPY entries consistently), that is the trigger to add L2.

---

## 16. File Structure and Build Order

The scalping strategy slots into the existing `/Users/user/moomoo` codebase as a self-contained subdirectory. It does not touch any existing files except for two additive changes: a new `scalp:` block appended to `config.yaml`, and a new scheduler entry in `main.py` / `bot_scheduler.py`.

```
/Users/user/moomoo/
├── src/
│   ├── scalp/                           ← NEW — all new files live here
│   │   ├── signals/
│   │   │   ├── gex_calculator.py        # GEX map: chain → wall, flip, is_stabilising
│   │   │   ├── iv_skew.py               # 25Δ risk reversal level + intraday Δ%
│   │   │   └── vix_monitor.py           # VIX slope + VVIX spike flag
│   │   ├── strategy/
│   │   │   ├── scalp_gate.py            # 4-of-5 confluence gate + trade mode selector
│   │   │   ├── scalp_entry.py           # Trigger A/B/C logic on 5m bars
│   │   │   ├── scalp_instrument.py      # Stock vs option decision + contract selection
│   │   │   └── scalp_position.py        # ATR stop, T1/T2 targets, trail, hard exits
│   │   ├── scalp_ledger.py              # SQLite ledger (scalp_trades.db)
│   │   ├── scalp_report.py              # Analytics report — mirrors analytics_report.py
│   │   └── scalp_scheduler.py           # Main scalp loop — called from bot_scheduler.py
│   │
│   ├── market/                          ← EXISTING — regime bridge already here
│   │   ├── llm_regime_bridge.py         # reused as-is
│   │   └── regime_combined.py           # reused as-is
│   │
│   ├── strategies/                      ← EXISTING — options bot strategies unchanged
│   │   ├── bear_call_spread.py
│   │   └── bull_put_spread.py
│   │
│   └── connectors/                      ← EXISTING — IBKRClient reused as-is
│
├── data/
│   ├── paper_trades.db                  ← EXISTING — options bot ledger (untouched)
│   └── scalp_trades.db                  ← NEW — separate ledger for scalp trades
│
├── config/
│   └── config.yaml                      ← EXISTING — add new scalp: block (see Section 16)
│
├── main.py                              ← EXISTING — add scalp_scheduler call (additive only)
└── bot_scheduler.py                     ← EXISTING — add scalp_scheduler.run() call
```

**Critical isolation rule:** The scalp strategy must never write to `paper_trades.db` and must never call `portfolio_guard.py`. The options bot's position counts, daily trade caps, and iron condor prevention are entirely independent of the scalping trades. Keep the two ledgers and two guards completely separate.

**IBKR client_id:** The options bot uses `client_id: 1`. The scalping scheduler must use `client_id: 3` (client_id 2 is reserved for test scripts per the `ibkr-connector` convention). This allows both bots to maintain simultaneous IBKR connections without conflict.

### Build phases

**Phase 1 — Signal infrastructure (build these first, in order):**

1. `vix_monitor.py` — One day. VIX slope + VVIX spike. Operational immediately; start collecting live data before anything else is built.
2. `gex_calculator.py` — Two to three days. The load-bearing piece. Build, run against the live chain on SPY and QQQ, verify wall/flip outputs visually against intraday price action before wiring into strategy logic.
3. `iv_skew.py` — One day. The delta-finding logic is the only tricky part; the rest is straightforward.

**Phase 2 — Strategy logic:**

4. `scalp_gate.py` — One to two days. Wire all five gates together + `get_trade_mode()`. Unit-test each gate independently before integration testing.
5. `scalp_entry.py` — Two days. Build Trigger A (rejection wick) and Trigger B (EMA/VWAP) first. Add Trigger C (flip break, momentum mode) only after A and B are validated through paper trading.
6. `scalp_instrument.py` — One day. Stock vs option scoring model + contract selection. During initial paper trading, run in logging-only mode (log recommendation but always trade stock) to validate the scores before committing to options trades.

**Phase 3 — Execution and ledger:**

7. `scalp_position.py` — One to two days. ATR computation, T1/T2 split logic, trail stop, all hard exit rules including 14:30 ET cutoff and VIX spike exit. Include `should_exit()` which checks all conditions in priority order.
8. `scalp_ledger.py` + `scalp_trades.db` — One day. Mirror `paper_ledger.py` structure and conventions exactly. Include `get_trades_opened_on()` for restart safety.
9. `scalp_scheduler.py` — Two days. Wire all modules into the main event loop (see interface contract in Section 17). Register in `bot_scheduler.py` as a parallel job guarded by `scalp.enabled: false` config flag.
10. `scalp_report.py` — One day. Analytics report; enable after first 10 closed trades to start seeing patterns.

**Phase 4 — Paper trading validation:**

11. Run paper trading for minimum 30 closed trades before any real capital. Both the options bot and scalp bot run simultaneously during this phase. After 30 trades, set `instrument_log_only: false` to enable live instrument selection.

**Phase 5 — If fills are slipping:**

12. Subscribe L2 ($34/month) and add limit-order placement logic to `scalp_position.py`.

---

## 17. Module Specifications — Implementation Reference

### config.yaml — new scalp: block to append

The existing `config.yaml` in `/Users/user/moomoo/config/` is unchanged except for appending this new block. The `ibkr:`, `mode:`, and `watchlist:` blocks at the top of the file are already present and shared between both strategies.

```yaml
# ─────────────────────────────────────────────
# SCALPING STRATEGY — Options-Informed GEX Reversion
# Add this block to the existing config.yaml
# ─────────────────────────────────────────────

scalp:
  enabled: false    # set true to activate the scalp scheduler

  ibkr_client_id: 3   # options bot = 1, test scripts = 2, scalp bot = 3

  watchlist:
    primary:   ["SPY", "QQQ"]
    secondary: ["NVDA", "TSLA", "AAPL", "MSFT", "AMZN", "GOOGL"]

  gex:
    refresh_times:  ["09:00", "11:00", "13:00"]   # ET
    proximity_pct:  0.003                          # 0.3% of spot price

  vix:
    hard_block:       25.0
    poll_interval_s:  300     # 5 minutes
    slope_window:     3       # number of readings for slope calculation
    vvix_spike_pct:   0.05    # 5% above recent average triggers spike flag

  skew:
    lookback_minutes:   30
    bearish_level:      3.0   # 25Δ put IV − call IV ≥ this = bearish lean
    bearish_delta_pct:  0.5   # steepening ≥ this over lookback = bearish lean

  session:
    no_entry_before:  "09:45"   # ET — skip opening volatility
    hard_cutoff:      "14:30"   # ET — no new entries after this
    hard_close:       "15:45"   # ET — all positions closed

  risk:
    max_risk_per_trade_pct:  0.0075   # 0.75% of account (paper phase)
    max_daily_loss_pct:      0.025    # 2.5% — hard stop for the session
    max_trades_per_day:      6
    atr_stop_multiplier_a:   0.8      # Trigger A (rejection wick)
    atr_stop_multiplier_b:   1.2      # Trigger B (EMA/VWAP)
    max_hold_minutes:        90       # stock and weekly options
    max_hold_minutes_0dte:   45       # 0DTE options — theta escape rule
    option_stop_pct:         0.50     # close option if premium drops to 50% of entry

  ivr:
    long_block_above:    70    # block long reversion if IVR ≥ this
    short_reduce_below:  20    # reduce short size to 50% if IVR ≤ this

  ledger:
    db_path: "data/scalp_trades.db"   # separate from paper_trades.db

  allow_short_stock: false   # keep false — buy puts instead for all bearish trades

  instrument_selection:
    # Score threshold to prefer option over stock (see Section 11)
    option_score_threshold:     2      # score >= this → buy option
    # 0DTE cutoff time — after this, use weekly options instead
    dte0_cutoff_hour_et:        11     # 11:30 ET
    dte0_cutoff_minute_et:      30
    # ATM-to-slightly-ITM delta target for bought options
    option_delta_min:           0.50
    option_delta_max:           0.65
    # Maximum acceptable bid-ask spread as % of mid
    option_max_spread_pct:      0.05
    # DTE range for weekly option fallback
    option_weekly_dte_min:      1
    option_weekly_dte_max:      7
    # Borrow rate thresholds (only relevant when allow_short_stock: true)
    borrow_rate_strong_put_pct: 10.0   # > this → strong prefer put (+3)
    borrow_rate_prefer_put_pct: 5.0    # > this → prefer put (+2)
    borrow_rate_slight_put_pct: 2.0    # > this → slight prefer put (+1)

  validation_gate:
    min_trades:    30
    min_win_rate:  0.55
    min_avg_rr:    1.5

  instrument_log_only: true   # Phase 1: log recommendation, always trade stock
                               # Set false after 30 paper trades to enable live instrument selection
```

---

### scalp_gate.py interface contract

`scalp_gate.py` owns both the five-gate confluence check and the trade mode selector. `get_trade_mode()` from Section 14 lives here — it is called internally by `check_gates()` and also exposed for the scheduler to call after a GEX refresh.

```python
def check_gates(
    regime_htf:  dict,   # from HMM/LLM regime bridge, daily bars
    regime_ltf:  dict,   # from HMM/LLM regime bridge, 1h bars
    vix:         dict,   # from VIXMonitor.poll()
    skew:        dict,   # from IVSkewMonitor.update()
    gex_map:     dict,   # from compute_gex()
    spot:        float,
    ivr:         float,  # IV Rank 0–100 for the symbol
    direction:   str,    # "LONG" or "SHORT"
    config:      dict,
) -> dict:
    """
    Returns:
      passed:     bool  — True if ≥4 gates pass AND hard gates (1,2) pass
      gates:      dict  — individual gate keys: gate1..gate5, each {passed, reason}
      trade_mode: str   — 'reversion' | 'momentum' | 'no_trade'
      score:      int   — 0–5, number of gates passed
    """

def get_trade_mode(gex_map: dict, skew: dict, vix: dict) -> str:
    """
    Returns 'reversion' | 'momentum' | 'no_trade'.
    Called by check_gates() and also directly by scalp_scheduler after GEX refresh
    to set the session-level trade mode before any individual symbol evaluation.
    See Section 14 for full decision logic.
    """
```

### scalp_entry.py interface contract

```python
def check_trigger(
    bars:       pd.DataFrame,  # 5m OHLCV, at least 20 bars, most recent last
    gex_map:    dict,
    direction:  str,           # "LONG" or "SHORT"
    trade_mode: str,           # "reversion" or "momentum"
    config:     dict,
) -> dict:
    """
    Returns:
      triggered:    bool
      trigger_type: str   — "A_rejection_wick" | "B_ema_vwap" | "C_flip_break"
      entry_price:  float — suggested entry (last close)
      stop_price:   float — ATR-based stop
      reason:       str   — human-readable explanation for log
    """
```

### scalp_instrument.py interface contract

```python
def get_straddle_implied_move(client, symbol: str, expiry: str) -> float:
    """
    Returns (atm_call_mid + atm_put_mid) / spot for the given expiry.
    Used as Factor 6 input to select_instrument().
    """

def select_instrument(
    ivr:                   float,   # IV Rank 0–100 for the symbol
    trade_mode:            str,     # 'reversion' | 'momentum'
    t1_distance:           float,   # T1 target as decimal fraction of spot (e.g. 0.004)
    entry_time_et:         str,     # "HH:MM" 24h in ET
    option_spread_pct:     float,   # bid-ask spread as % of option mid (0.0 if unknown)
    straddle_implied_move: float,   # from get_straddle_implied_move()
    skew_delta:            float,   # from IVSkewMonitor — 30-min rate of change
    direction:             str,     # "LONG" | "SHORT"
    borrow_rate_pct:       float,   # annualised borrow rate (0.0 when short_stock disabled)
    config:                dict,
) -> dict:
    """
    Returns:
      instrument:   str    — "STOCK" | "CALL" | "PUT"
      score:        int    — raw composite score across six factors
      overridden:   bool   — True if short_stock disabled and PUT forced
      reason:       str    — human-readable rationale for log and dashboard
    """

def select_option_contract(
    client,
    symbol:    str,
    direction: str,     # "LONG" (→ CALL) | "SHORT" (→ PUT)
    config:    dict,
) -> str | None:
    """
    Returns OCC contract code of best matching option, or None if no
    suitable contract found (caller should fall back to STOCK).
    Selects: front expiry or nearest weekly based on time-of-day config.
    Delta target: config.scalp.instrument_selection.option_delta_min/max
    Max spread:   config.scalp.instrument_selection.option_max_spread_pct
    """

def _build_reason(
    ivr: float, trade_mode: str, t1_distance: float,
    entry_time: str, option_spread_pct: float,
    straddle_implied_move: float, skew_delta: float,
) -> str:
    """
    Internal helper — builds a human-readable one-line rationale string
    summarising which factors drove the instrument recommendation.
    Example output:
      "ivr=28(+1) mode=momentum(+2) t1=0.6%(+2) time=10:15(0) spread=1.2%(0) eff=1.3(+2) → score=7 → CALL"
    Logged to scalp_trades.db instrument_reason column and to the console.
    """
```

---

### scalp_position.py interface contract

```python
def compute_stop(
    entry_price:  float,
    direction:    str,       # "LONG" | "SHORT"
    trigger_type: str,       # "A_rejection_wick" | "B_ema_vwap" | "C_flip_break"
    bars_5m:      pd.DataFrame,
    config:       dict,
) -> float:
    """
    Returns stop price for a stock position.
    Uses ATR(14) on 5m bars × atr_stop_multiplier_a (Trigger A) or _b (Trigger B/C).
    Stop is placed beyond the entry bar's extreme (low for long, high for short).
    Never placed at a round number — rounded away from entry by 1 cent.
    """

def compute_option_stop(entry_option_price: float, config: dict) -> float:
    """
    Returns the option premium level at which to close the option position.
    = entry_option_price × (1 - config.scalp.risk.option_stop_pct)
    e.g. entry $2.40 × (1 - 0.50) = $1.20 stop on option premium.
    """

def compute_targets(
    entry_price: float,
    direction:   str,
    gex_map:     dict,
    spot:        float,
    risk_r:      float,   # risk in dollars (entry - stop) × size
    config:      dict,
) -> dict:
    """
    Returns:
      t1_price: float  — first target (next GEX level or VWAP, whichever closer)
      t2_price: float  — second target (GEX flip zone or 2.5–3:1 R:R)
      t1_pct:   float  — T1 distance as fraction of spot (used by instrument selector)
    """

def compute_option_size(
    account_value: float,
    entry_option_price: float,  # premium per share (multiply by 100 for per-contract cost)
    config:        dict,
) -> int:
    """
    Returns number of contracts to buy, sized so that total premium paid
    equals max_risk_per_trade_pct × account_value.
    Minimum 1 contract. Formula: floor(max_loss / (premium × 100)), min 1.
    """

def should_exit(
    position:    dict,   # open trade record from scalp_ledger
    current_bar: dict,   # latest 5m OHLCV bar
    vix:         dict,   # from VIXMonitor.poll()
    gex_map:     dict,
    config:      dict,
) -> dict:
    """
    Evaluates all exit conditions for an open position.
    Returns:
      exit:        bool  — True if position should be closed now
      exit_reason: str   — 't1' | 't2' | 'stop' | 'time' | 'time_0dte' | 'vix_spike' | 'session_end'
      exit_price:  float — suggested exit price (last close or stop price)
    Checks in priority order:
      1. VIX spike ≥5% intraday → immediate close at market
      2. Hard session close (15:45 ET)
      3. 0DTE theta escape: instrument is CALL/PUT + 0DTE + held ≥ 45 min + T1 not hit
      4. Max hold time exceeded (90 min stock/weekly, 45 min 0DTE option)
      5. Stop hit (stock: price crosses stop; option: premium ≤ option_stop_price)
      6. T1 hit → close half, move stop to breakeven
      7. T2 hit → close remainder
    """
```

---

### scalp_ledger.py interface contract

```python
class ScalpLedger:
    """
    SQLite ledger for scalp trades. Mirrors paper_ledger.py conventions.
    Writes to data/scalp_trades.db — never touches paper_trades.db.
    Uses WAL mode so reads never block writes (dashboard can read while bot writes).
    """

    def open_trade(self, trade: dict) -> int:
        """
        Inserts a new open trade record. Returns the assigned trade ID.
        trade dict must contain all entry context fields from the schema.
        """

    def close_trade(self, trade_id: int, exit_context: dict) -> None:
        """
        Updates an open trade to closed status with exit context fields.
        exit_context must contain: exit_price, exit_time, exit_reason,
        pnl_dollars, pnl_r, hold_minutes, option_exit_price (if applicable).
        """

    def get_open_trades(self) -> list[dict]:
        """Returns all currently open scalp positions."""

    def get_closed_trades(self, limit: int = 100) -> list[dict]:
        """Returns closed trades, newest first."""

    def get_trades_opened_on(self, date_str: str) -> list[dict]:
        """
        Returns all trades opened on date_str (LIKE 'date_str%').
        Used by the daily cap guard at scheduler startup — same pattern
        as paper_ledger.get_trades_opened_on() for restart safety.
        """

    def get_statistics(self) -> dict:
        """
        Returns aggregate metrics used by scalp_report.py:
          total_trades, win_rate, avg_rr, avg_hold_minutes,
          by_instrument (STOCK/CALL/PUT breakdown),
          by_trigger (A/B/C breakdown),
          by_trade_mode (reversion/momentum breakdown),
          by_time_bucket (09:45–11:30 / 11:30–13:00 / 13:00–14:30),
          by_gex_environment (stabilising/amplifying breakdown).
        """
```

---

### scalp_scheduler.py — main loop specification

This is the orchestration module. It owns the event loop, coordinates all signal refreshes, and calls the strategy modules in the correct order. It is registered as a parallel job in `bot_scheduler.py` alongside the existing options bot scheduler — the two run independently and never share state.

**Client ID isolation:** The options bot uses `client_id=1`. The scalp bot must initialise its own `IBKRClient` with `client_id=3`. Both can be connected simultaneously to the same TWS instance without conflict.

```python
from ibkr_connector import IBKRClient
from src.scalp.signals.gex_calculator import compute_gex
from src.scalp.signals.iv_skew import IVSkewMonitor
from src.scalp.signals.vix_monitor import VIXMonitor
from src.scalp.strategy.scalp_gate import check_gates, get_trade_mode
from src.scalp.strategy.scalp_entry import check_trigger
from src.scalp.strategy.scalp_instrument import (
    select_instrument, select_option_contract, get_straddle_implied_move
)
from src.scalp.strategy.scalp_position import (
    compute_stop, compute_option_stop, compute_targets,
    compute_option_size, should_exit
)
from src.scalp.scalp_ledger import ScalpLedger
from src.market.regime_combined import get_regime   # existing regime module

class ScalpScheduler:
    """
    Main intraday loop for the GEX Reversion Scalper.
    Runs alongside the options bot — separate IBKRClient, separate ledger.
    """

    def __init__(self, config: dict):
        self.config  = config
        self.client  = IBKRClient(
            port=      config["ibkr"]["port"],
            account=   config["ibkr"]["account"],
            client_id= config["scalp"]["ibkr_client_id"],   # 3 — never 1 or 2
            streaming= True,
        )
        self.ledger     = ScalpLedger(config)
        self.vix_mon    = VIXMonitor(config)
        self.skew_mon   = IVSkewMonitor(config)
        self.gex_cache  = {}   # symbol → {gex_map, computed_at}
        self.regime_htf = {}   # symbol → regime dict (refreshed every 2h by options bot)
        self.regime_ltf = {}   # symbol → regime dict (refreshed every 30min)
        self.daily_trades = 0  # restored from ledger at startup (restart safety)

    def run(self):
        """
        Top-level entry point called by bot_scheduler.py.
        Runs the full intraday session from pre-market to session close.
        """
        self.client.connect()

        # Restore daily trade count (restart safety — same pattern as portfolio_guard)
        from datetime import date
        today_trades = self.ledger.get_trades_opened_on(str(date.today()))
        self.daily_trades = len(today_trades)

        try:
            self._pre_market_setup()    # 09:00 ET — GEX maps, regime HTF
            self._session_loop()        # 09:30–14:30 ET — 5m scanning + position monitoring
            self._close_all_positions() # 15:45 ET — hard close any remaining positions
        finally:
            self.client.disconnect()

    # ── Pre-market (09:00 ET) ─────────────────────────────────────────────────

    def _pre_market_setup(self):
        """
        Compute pre-market GEX maps for all watchlist symbols.
        Pull HTF regime for each symbol (reuse options bot's regime cache if available).
        Start VIX polling.
        """
        cfg = self.config["scalp"]
        watchlist = cfg["watchlist"]["primary"] + cfg["watchlist"]["secondary"]
        for symbol in watchlist:
            self.gex_cache[symbol] = compute_gex(self.client, symbol)
        self.vix_mon.poll()   # first VIX reading

    # ── Session loop (09:30–14:30 ET) ────────────────────────────────────────

    def _session_loop(self):
        """
        5-minute scan loop. On every 5m bar close:
          1. Poll VIX + VVIX
          2. Check open positions for exits
          3. If before 14:30 ET and daily cap not hit: scan for new entries
          4. Refresh GEX at scheduled times (11:00, 13:00 ET)
          5. Refresh LTF regime every 30 min
        """
        import time, datetime

        while True:
            now_et = self._now_et()

            # Hard session end
            if now_et.hour >= 15 and now_et.minute >= 45:
                break

            # ── Signal refresh ─────────────────────────────────
            vix = self.vix_mon.poll()

            # GEX refresh at scheduled times
            if self._should_refresh_gex(now_et):
                for symbol in self._all_symbols():
                    self.gex_cache[symbol] = compute_gex(self.client, symbol)

            # LTF regime refresh every 30 min
            if now_et.minute in (0, 30):
                for symbol in self._all_symbols():
                    self.regime_ltf[symbol] = get_regime(self.client, symbol, "ltf")

            # ── Monitor open positions ─────────────────────────
            for trade in self.ledger.get_open_trades():
                bars = self.client.get_intraday_ohlcv(trade["symbol"], "5m", "1d")
                exit_signal = should_exit(
                    trade, bars.iloc[-1].to_dict(),
                    vix, self.gex_cache.get(trade["symbol"], {}), self.config
                )
                if exit_signal["exit"]:
                    self._close_position(trade, exit_signal)

            # ── Scan for new entries (only before 14:30, cap not hit) ─────
            if (now_et.hour < 14 or (now_et.hour == 14 and now_et.minute < 30)):
                if self.daily_trades < self.config["scalp"]["risk"]["max_trades_per_day"]:
                    if now_et.hour >= 9 and now_et.minute >= 45:  # skip first 15 min
                        self._scan_entries(vix, now_et)

            # Wait for next 5m bar close
            time.sleep(300)

    # ── Entry scanning ────────────────────────────────────────────────────────

    def _scan_entries(self, vix: dict, now_et):
        """
        Evaluates every watchlist symbol for an entry setup.
        Checks both LONG and SHORT directions where regime allows.
        """
        for symbol in self._all_symbols():
            gex_map    = self.gex_cache.get(symbol, {})
            skew       = self.skew_mon.update(self.client, symbol)
            regime_htf = self.regime_htf.get(symbol, {})
            regime_ltf = self.regime_ltf.get(symbol, {})
            ivr        = self._get_ivr(symbol)   # from options bot pipeline
            spot       = gex_map.get("spot", self.client.get_spot_price(symbol))
            bars_5m    = self.client.get_intraday_ohlcv(symbol, "5m", "1d")

            for direction in ("LONG", "SHORT"):
                gates = check_gates(
                    regime_htf, regime_ltf, vix, skew, gex_map,
                    spot, ivr, direction, self.config
                )
                if not gates["passed"]:
                    continue

                trigger = check_trigger(
                    bars_5m, gex_map, direction, gates["trade_mode"], self.config
                )
                if not trigger["triggered"]:
                    continue

                # Compute targets first — needed for instrument selection
                targets = compute_targets(
                    trigger["entry_price"], direction, gex_map, spot,
                    risk_r=0.0,   # placeholder; recomputed after stop is known
                    config=self.config
                )

                # Instrument selection
                expiry = gex_map.get("expiry")
                straddle_move = (
                    get_straddle_implied_move(self.client, symbol, expiry)
                    if expiry else 0.0
                )

                # Approximate spread from ATM option snapshot
                option_spread_pct = self._estimate_option_spread(symbol, expiry, direction)

                instrument_result = select_instrument(
                    ivr=ivr,
                    trade_mode=gates["trade_mode"],
                    t1_distance=targets["t1_pct"],
                    entry_time_et=now_et.strftime("%H:%M"),
                    option_spread_pct=option_spread_pct,
                    straddle_implied_move=straddle_move,
                    skew_delta=skew["skew_delta"],
                    direction=direction,
                    borrow_rate_pct=0.0,   # short stock disabled; extend later
                    config=self.config,
                )

                # Logging-only mode: always trade stock during first 30 paper trades
                log_only = self.config["scalp"].get("instrument_log_only", True)
                if log_only:
                    instrument = "STOCK"
                else:
                    instrument = instrument_result["instrument"]

                # Get option contract if needed
                option_contract = None
                if instrument in ("CALL", "PUT"):
                    option_contract = select_option_contract(
                        self.client, symbol, direction, self.config
                    )
                    if option_contract is None:
                        instrument = "STOCK"   # fallback

                self._open_position(
                    symbol, direction, trigger, targets,
                    instrument, option_contract, instrument_result,
                    gex_map, skew, vix, ivr, gates, straddle_move,
                )
                self.daily_trades += 1
                break   # one direction per symbol per bar

    # ── Position open / close ─────────────────────────────────────────────────

    def _open_position(self, symbol, direction, trigger, targets,
                       instrument, option_contract, instrument_result,
                       gex_map, skew, vix, ivr, gates, straddle_move):
        """Places the order and writes to the ledger."""
        # ... order placement via IBKRClient, then ledger.open_trade(...)

    def _close_position(self, trade: dict, exit_signal: dict):
        """Closes the position and writes exit context to the ledger."""
        # ... order placement via IBKRClient, then ledger.close_trade(...)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _all_symbols(self):
        cfg = self.config["scalp"]["watchlist"]
        return cfg["primary"] + cfg["secondary"]

    def _should_refresh_gex(self, now_et) -> bool:
        refresh_times = self.config["scalp"]["gex"]["refresh_times"]   # ["09:00","11:00","13:00"]
        hhmm = now_et.strftime("%H:%M")
        return hhmm in refresh_times

    def _get_ivr(self, symbol: str) -> float:
        """
        Reads IV Rank from the options bot's existing IV pipeline.
        The options bot already computes IVR daily for all watchlist symbols
        and stores it. Read from the shared config or data layer — do NOT
        recompute from scratch in the scalp bot.
        """
        # Implementation: read from options bot's iv_history store or shared cache
        pass

    def _estimate_option_spread(self, symbol: str, expiry: str, direction: str) -> float:
        """
        Fetches a quick ATM option snapshot to estimate current spread quality.
        Returns spread as fraction of mid price, or 0.0 if snapshot fails.
        """
        pass

    @staticmethod
    def _now_et():
        import datetime, pytz
        return datetime.datetime.now(pytz.timezone("America/New_York"))
```

**Integration with `bot_scheduler.py` (additive — two lines):**

```python
# In bot_scheduler.py — existing file, two lines added:
from src.scalp.scalp_scheduler import ScalpScheduler

# In the scheduler's daily job setup:
if config.get("scalp", {}).get("enabled", False):
    scalp = ScalpScheduler(config)
    scalp.run()   # runs its own blocking session loop in a thread
```

---

### scalp_report.py — analytics specification

Build after 30+ paper trades. Mirrors `analytics_report.py` from the options bot. Outputs to console (ANSI colour) and is integrated into `dashboard.py` via a `/scalp-analytics` route.

**Required sections:**

| Section | Content |
|---|---|
| Overview | Total trades, win rate, avg R:R, avg hold, total P&L |
| By instrument | STOCK / CALL / PUT breakdown — win rate, avg R:R, avg hold per instrument |
| By trigger type | Trigger A / B / C breakdown |
| By trade mode | Reversion vs momentum — win rate and avg R:R |
| By time bucket | 09:45–11:30 / 11:30–13:00 / 13:00–14:30 — confirms time-of-day adjustments working |
| By GEX environment | Stabilising vs amplifying — validates core GEX thesis |
| Instrument score vs outcome | Groups trades by instrument score bands; shows win rate per band |
| Straddle efficiency vs outcome | Groups trades by efficiency ratio; validates Factor 6 |
| Days held distribution | Histogram of hold times — confirms exits working as designed |

```python
# scalp_report.py — entry point
def generate_report(db_path: str, config: dict) -> None:
    """
    Reads scalp_trades.db, generates all analytics sections.
    Called via: python scalp_report.py
    Or from dashboard /scalp-analytics route.
    """
```

```sql
CREATE TABLE scalp_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL,          -- LONG | SHORT
    trigger_type    TEXT NOT NULL,          -- A_rejection_wick | B_ema_vwap | C_flip_break
    trade_mode      TEXT NOT NULL,          -- reversion | momentum
    status          TEXT NOT NULL,          -- open | closed

    -- Entry context
    entry_price         REAL,
    entry_time          TEXT,
    stop_price          REAL,
    t1_price            REAL,
    t2_price            REAL,
    position_size       INTEGER,
    risk_dollars        REAL,

    -- Instrument selection
    instrument              TEXT,    -- STOCK | CALL | PUT
    instrument_score        INTEGER, -- raw composite score from select_instrument()
    instrument_override     TEXT,    -- NULL | 'short_stock_disabled' | 'no_contract_found' | 'log_only'
    instrument_reason       TEXT,    -- human-readable factor summary from _build_reason()
    straddle_implied_move   REAL,    -- (atm_call + atm_put) / spot at entry
    straddle_efficiency     REAL,    -- t1_distance / straddle_implied_move (Factor 6 input)
    option_contract         TEXT,    -- OCC code if CALL or PUT, else NULL
    option_delta            REAL,    -- delta of bought option at entry
    option_iv               REAL,    -- IV of bought option at entry
    option_dte              INTEGER, -- DTE of bought option at entry

    -- Options signals at entry
    gamma_wall      REAL,
    gex_flip        REAL,
    gex_sign        TEXT,                   -- stabilising | amplifying
    skew_level      REAL,
    skew_delta      REAL,
    vix_at_entry    REAL,
    vvix_at_entry   REAL,
    ivr_at_entry    REAL,
    regime_htf      TEXT,
    regime_ltf      TEXT,
    gates_passed    INTEGER,

    -- Exit context
    exit_price          REAL,
    exit_time           TEXT,
    exit_reason         TEXT,    -- t1 | t2 | stop | time | time_0dte | manual | vix_spike
    pnl_dollars         REAL,
    pnl_r               REAL,    -- P&L in units of initial risk (stock) or premium (options)
    hold_minutes        INTEGER,
    option_entry_price  REAL,    -- option premium at entry if CALL/PUT, else NULL
    option_exit_price   REAL,    -- option premium at exit if CALL/PUT, else NULL
);
```

---

## 18. Validation Gate — Paper Trading Requirements

Before committing real capital, the following thresholds must all be met:

| Metric | Threshold | Why |
|---|---|---|
| Minimum closed trades | 30 | Statistical minimum for meaningful win rate estimate |
| Win rate | ≥ 55% | Viable at the target R:R ratios |
| Average actual R:R | ≥ 1.5:1 | Profitable even at 55% win rate; confirms targets are achievable |
| Maximum drawdown in paper period | ≤ 8% of paper account | Confirms risk controls are working |
| Sample includes both reversion and momentum | ≥ 3 momentum trades | Validates Trigger C works as designed |
| Sample covers multiple market regimes | At least 1 bull, 1 bear, 1 neutral week | Strategy not regime-specific |

### Phase 2 validation — instrument scoring calibration (after 50+ trades)

Run the instrument selector in **logging-only mode** for the first 30 paper trades (always trades stock, logs what the selector *would have* recommended). After 30 trades, enable the selector fully and continue to 50+ trades. Then split the performance analysis:

| Analysis | Split dimension | What to look for |
|---|---|---|
| Win rate by instrument | STOCK vs CALL vs PUT | If CALL win rate < STOCK win rate, raise `option_score_threshold` |
| Avg R:R by instrument | STOCK vs CALL vs PUT | Options should show higher R:R than stock when used correctly |
| Score vs outcome | Instrument score buckets | Verify higher scores actually produce better option outcomes |
| Straddle efficiency vs outcome | Efficiency ratio buckets | Verify low-efficiency trades underperform — tighten Factor 6 threshold |
| Time-of-day breakdown | 09:45–11:30 / 11:30–13:00 / 13:00–14:30 | Confirm afternoon time penalty is correctly sized |

Build these breakdowns into `scalp_report.py` alongside the existing trigger-type and GEX-environment splits. The goal is empirical calibration of the scoring model — thresholds in config should reflect actual observed outcomes, not just theoretical reasoning.

Validate using the weekly report pattern from the options bot. Build `scalp_report.py` analogous to `analytics_report.py` producing: trade-by-trade breakdown, win rate by trigger type, win rate by time-of-day bucket, P&L by GEX environment (stabilising vs amplifying), and instrument breakdown as above.

---

## 19. Key Numbers and Thresholds Reference

Quick reference for all configurable parameters. All are in `config.yaml`.

| Parameter | Default | Notes |
|---|---|---|
| GEX proximity threshold | 0.3% | Price must be within this % of a GEX level to qualify |
| GEX refresh times | 09:00, 11:00, 13:00 ET | Pre-market + two intraday refreshes |
| VIX hard block | 25.0 | Absolute no-trade threshold |
| VIX poll interval | 5 minutes | Balance of responsiveness and API cost |
| VVIX spike threshold | +5% above 12-reading average | Early vol-regime warning |
| Skew bearish level | 3.0 (25Δ put IV − call IV) | ≥3 = put demand present |
| Skew steepening threshold | Δ+ 0.5 over 30 min | Rapid steepening = bearish momentum |
| IVR long block | ≥70 | Avoid fading into potential news moves |
| IVR short reduce | ≤20 | Scale short size to 50% |
| No entry before | 09:45 ET | Skip opening volatility |
| Hard entry cutoff | 14:30 ET | 0DTE gamma becomes erratic |
| Hard position close | 15:45 ET | No overnight exposure |
| Max hold per trade | 90 minutes | GEX patterns resolve within this window |
| ATR stop multiplier (Trigger A) | 0.8× 5m ATR | Tight — rejection should hold immediately |
| ATR stop multiplier (Trigger B) | 1.2× 5m ATR | Wider — EMA/VWAP entries need more room |
| Risk per trade | 0.75% of account | Paper phase; scale to 0.5–1.0% live |
| Daily loss cap | 2.5% of account | Hard stop for the session |
| Daily trade cap | 4–6 trades | Enforce selectivity |
| 0DTE dominance (SPY/QQQ) | ~59% average (2025), peaks 62%+ | Full-year 2025 SPX/SPY data (CBOE); justification for front-expiry GEX focus |
| GEX wall success rate | ~78–93% | Historical containment in positive GEX (SpotGamma) |
| **Instrument selection** | | |
| IVR threshold — prefer options | < 45 | Low IV = cheap options = buy |
| IVR threshold — prefer stock | > 65 | High IV = expensive options = buy stock instead |
| T1 distance — options viable | ≥ 0.4% | Move large enough to overcome theta |
| T1 distance — stock preferred | < 0.25% | Move too small for options leverage to matter |
| Option score threshold | ≥ 2 | Minimum score to recommend option over stock |
| Straddle efficiency — prefer option | > 0.8 (t1/straddle) | Target exceeds or meets implied move |
| Straddle efficiency — prefer stock | < 0.5 | Option priced for 2× your target move |
| Vega tailwind condition | skew_delta > 0 AND ivr < 50 | Cheap + rising IV → +1 bonus to Factor 1 |
| Borrow rate — strong prefer put | > 10% annualised | Short stock borrow very expensive |
| Borrow rate — prefer put | > 5% annualised | Meaningful borrow cost |
| 0DTE cutoff (morning only) | 11:30 ET | After this, use weekly options to avoid theta cliff |
| Option delta target | 0.50–0.65 | ATM to slightly ITM — max gamma per dollar |
| Option max DTE (weekly) | 1–7 days | Nearest weekly expiry for scalp holds |
| Max option spread | ≤ 5% of mid | Above this, round-trip cost too large for scalping |
| Short stock | Disabled by default | `allow_short_stock: false` — buy puts instead |
| **Option position management** | | |
| Option stop loss | 50% of premium paid | `option_stop_pct: 0.50` in config |
| Max hold — 0DTE option | 45 minutes | Theta escape rule; close if T1 not hit |
| Max hold — weekly option | 60–90 minutes | Same as stock |
| Option position sizing | Size to full-loss scenario | `contracts = max_loss_dollars / (premium × 100)` |
| Validation: min trades | 30 | Before real capital |
| Validation: min win rate | 55% | Paper trading threshold |
| Validation: min avg R:R | 1.5:1 | Actual achieved, not theoretical |

---

*End of document. Start implementation with `vix_monitor.py` for immediate data collection, then `gex_calculator.py` as the load-bearing core. All other modules plug into GEX map output.*
