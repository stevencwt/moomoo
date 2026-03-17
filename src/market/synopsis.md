# Options Premium Selling Bot — Reference Guide

## Table of Contents

1. [Overview](#1-overview)
2. [Trading Strategies](#2-trading-strategies)
3. [Market Regime Detection](#3-market-regime-detection)
4. [Entry Criteria & Signal Filters](#4-entry-criteria--signal-filters)
5. [Exit Rules & Position Management](#5-exit-rules--position-management)
6. [Broker Architecture](#6-broker-architecture)
7. [Configuration Reference](#7-configuration-reference)
8. [Adding Assets to the Watchlist](#8-adding-assets-to-the-watchlist)
9. [Enabling / Disabling Strategies](#9-enabling--disabling-strategies)
10. [Switching Broker Modes](#10-switching-broker-modes)
11. [Going Live — Validation Gate](#11-going-live--validation-gate)
12. [Daily Operations](#12-daily-operations)
13. [File Structure](#13-file-structure)
14. [Trade Context Logging](#14-trade-context-logging)
15. [Web Dashboard](#15-web-dashboard)
16. [Analytics CLI](#16-analytics-cli)

**Strategies covered:**
- 2.1 Covered Call
- 2.2 Bear Call Spread
- 2.3 Bull Put Spread *(two-sided premium collection)*

**Key subsections:**
- 3.1 Regime Detection v1 (rule-based fallback)
- 3.2 Regime Detection v2 (HMM/Hurst — primary source)
- 3.3 Regime Translation & Strategy Activation
- 3.4 Exit Mandate (regime-shift force-close)
- 4.1 Signal Ranking & Selection *(opportunity scoring — replaces FIFO execution)*
- 14.1 Entry Context Fields *(7 fields written to SQLite at trade open)*
- 14.2 Exit Context Fields *(6 fields written to SQLite at trade close)*
- 14.3 Data Flow *(how context travels from strategy → signal → ledger)*
- 15.1 Pages & Features
- 15.2 Running the Dashboard

---

## 1. Overview

This is a fully automated **options premium selling bot** built in Python. It targets high-probability, defined-risk trades by selling time value (theta) in US equity options markets.

**Core philosophy:** Sell options when implied volatility is elevated (premium is expensive), use technical and statistical filters to avoid unfavourable conditions, and close positions systematically before expiry to avoid gamma risk.

**What the bot does every trading day:**

| Time (ET) | Action |
|---|---|
| 09:35 | **Pass 1:** Scan all watchlist symbols → collect every qualifying signal |
| 09:35 | **Pass 2:** Score and rank candidates → select top N → place orders |
| Every 30 min | Check open positions for stop-loss / take-profit / DTE exits / regime-shift exits |
| 16:05 | Collect end-of-day IV for IV Rank calculation |
| Friday 16:30 | Generate weekly validation report |

**What the bot does NOT do:**
- Trade futures, forex, or crypto
- Use directional strategies (no long calls, no naked puts)
- Override the portfolio guard or risk limits
- Place live orders until `mode: live` is explicitly set in config

---

## 2. Trading Strategies

### 2.1 Covered Call

A covered call sells an out-of-the-money (OTM) call option against 100 shares of the underlying stock already held. The premium collected is immediate income. The maximum risk is the opportunity cost of capped upside — the shares absorb any downside move.

**How it works:**

```
You hold: 100 shares of TSLA at $380
You sell: 1× TSLA call, strike $420, expiry ~35 DTE, for $2.50 credit

Outcome scenarios at expiry:
  TSLA stays below $420  → call expires worthless → keep $250 (max profit)
  TSLA rises above $420  → shares called away at $420 → still profitable, upside capped
  TSLA falls to $350     → loss on shares offset by $250 premium collected
```

**Entry requirements** (all must pass):

| Condition | Default | Reason |
|---|---|---|
| Shares held ≥ 100 | Required | Must own the underlying to be "covered" |
| Open positions < max | 2 | Prevent over-concentration |
| Market regime ≠ high_vol | Required | Expanding volatility = unpredictable premiums |
| IV Rank ≥ 30 | 30 | Only sell when premium is elevated |
| RSI ≤ 70 | 70 | Avoid strong upside breakouts |
| No earnings in option lifetime | Required | Earnings = binary event risk |
| OTM call delta 0.20–0.35 | Required | Balance between premium and safety |
| Call open interest ≥ 100 | 100 | Ensure liquidity |

**Contract selection:** The bot picks the qualifying call with the **highest delta within the 0.20–0.35 range**. Higher delta = more premium collected while remaining OTM.

---

### 2.2 Bear Call Spread

A bear call spread sells an OTM call (short leg) and simultaneously buys a higher-strike call (long leg) for defined-risk protection. The net credit is the difference between the two premiums. Maximum loss is capped at `(spread width − net credit) × 100`.

**How it works:**

```
TSLA trading at $380 in a neutral/bearish regime.
You sell: 1× TSLA call, strike $420, for $3.00
You buy:  1× TSLA call, strike $430, for $1.50
Net credit: $1.50 ($150 per spread)
Max loss:   $8.50 ($850 per spread = $10 spread − $1.50 credit)

Outcome scenarios at expiry:
  TSLA stays below $420  → both expire worthless → keep $150 (max profit)
  TSLA rises to $425     → partial loss
  TSLA rises above $430  → max loss of $850 (defined, no worse)
```

**Unlike the covered call**, no shares are required. This strategy can be used on any stock in the watchlist regardless of whether you own it.

**Entry requirements** (all must pass):

| Condition | Default | Reason |
|---|---|---|
| Open positions < max | 3 | Per-strategy position limit |
| Market regime in bear/neutral | Required | Avoid in pure bull — breakout risk |
| IV Rank ≥ 35 | 35 | Higher threshold than covered call (defined risk needs richer premium) |
| RSI ≥ 45 | 45 | Don't sell calls into a freefall (stock may reverse hard) |
| %B (Bollinger) ≥ 0.40 | 0.40 | Price needs elevation for OTM calls to have sufficient value |
| No earnings in option lifetime | Required | Binary event risk |
| OTM short call delta 0.20–0.35 | Required | Standard OTM selection |
| Net credit ≥ $0.50 | $0.50 | Minimum premium worth collecting |
| Reward/risk ratio ≥ 0.20 | 0.20 | Minimum 1:5 risk/reward (collect $1 to risk $5) |
| Protective long call found | Required | $10 wide spread above short strike |

---

### 2.3 Bull Put Spread

A bull put spread sells an OTM put (short leg) and simultaneously buys a lower-strike put (long leg) for defined-risk protection. It is the **mirror image of the bear call spread** — it sells downside premium instead of upside premium, profiting when the underlying stays *above* the short strike at expiry.

**How it works:**

```
AAPL trading at $269 in a bull/neutral regime.
You sell: 1× AAPL put, strike $250, for $3.00
You buy:  1× AAPL put, strike $240, for $1.50
Net credit: $1.50 ($150 per spread)
Max loss:   $8.50 ($850 per spread = $10 spread − $1.50 credit)

Outcome scenarios at expiry:
  AAPL stays above $250  → both expire worthless → keep $150 (max profit)
  AAPL falls to $246     → partial loss
  AAPL falls below $240  → max loss of $850 (defined, no worse)
  Breakeven: $248.50     → sell_strike ($250) − net_credit ($1.50)
```

**No shares are required.** Like the bear call spread, this strategy works on any watchlist symbol.

**Entry requirements** (all must pass):

| Condition | Default | Reason |
|---|---|---|
| Open positions < max | 3 | Per-strategy position limit |
| Market regime in bull/neutral | Required | Avoid in bear — crash risk to short puts |
| IV Rank ≥ 35 | 35 | Only sell when premium is elevated |
| RSI ≥ 35 | 35 | Don't sell puts in freefall — assignment risk extreme |
| RSI ≤ 65 | 65 | Avoid extreme overbought — reversal could threaten put strikes |
| %B (Bollinger) ≥ 0.20 | 0.20 | Price not near lower band — puts would be too close to the money |
| No earnings in option lifetime | Required | Binary event risk |
| OTM short put abs(delta) 0.20–0.35 | Required | Standard OTM selection |
| Net credit ≥ $0.50 | $0.50 | Minimum premium worth collecting |
| Reward/risk ratio ≥ 0.20 | 0.20 | Minimum 1:5 risk/reward |
| Protective long put found | Required | $10 wide spread below short strike |
| No opposing spread open on same symbol | Required | Iron condor prevention (see below) |

**Why the RSI floor matters differently here:**
For bear call spreads, RSI < 45 means the stock is in freefall and may reverse hard through your short calls — you block it. For bull put spreads, the danger is the opposite: RSI < 35 means the stock is already deeply oversold and the short puts face extreme assignment risk. The floor is set lower (35 vs 45) because being in some downtrend is acceptable as long as it hasn't become a genuine crash.

**Why the RSI ceiling exists:**
If RSI > 65 the stock has run hard. A reversal from overbought levels moves the underlying back *toward* the put strikes quickly. The ceiling provides a margin of safety on the upside.

**Iron condor prevention:**
The portfolio guard blocks a bull put spread on any symbol that already has an open bear call spread, and vice versa. A bear call spread + bull put spread on the same symbol creates an iron condor, which requires different combined Greeks management (delta neutrality, wing adjustments) not yet implemented. Keep both sides on separate symbols to build a diversified theta portfolio without inadvertently creating unchecked condor exposure.

```
Allowed:    US.SPY bear_call_spread  +  US.QQQ bull_put_spread   ✅
Blocked:    US.SPY bear_call_spread  +  US.SPY bull_put_spread   ❌  (same symbol)
```

---

### How the three strategies complement each other

```
Market condition              Covered call   Bear call spread   Bull put spread
────────────────────────────  ────────────   ────────────────   ───────────────
CHOP_NEUTRAL / neutral + IV   ✅ fires        ✅ fires             ✅ fires
BEAR_PERSISTENT / bear + IV   ✅ fires        ✅ fires             ❌ blocked
BULL_PERSISTENT / bull + IV   ✅ fires        ❌ blocked           ✅ fires
EXPANDING vol (exit mandate)  ❌ all blocked  ❌ all blocked       ❌ all blocked
TRANSITION / high_vol / VIX≥threshold  ❌ all blocked  ❌ all blocked  ❌ all blocked
```

In a neutral regime — the most common condition — all three strategies can fire simultaneously. The portfolio guard's `max_open_positions` and `max_trades_per_day` limits govern total exposure. The iron condor prevention ensures that two opposing sides never accumulate on the same underlying, keeping each position independently manageable.

---

## 3. Market Regime Detection

Before any trade is evaluated, the bot classifies the current market environment for each symbol. The bot uses a **two-tier regime detection architecture**: a primary HMM-based statistical engine (v2) and a rule-based fallback (v1). The v2 engine drives all entry and exit decisions; the v1 engine activates only when v2 is unavailable.

---

### 3.1 Regime Detection v1 — Rule-Based Fallback

The original rule-based detector uses three indicators computed from 6 months of daily price data plus the current VIX. It remains in the codebase as a reliable fallback if the v2 package is not installed or returns no output.

| Regime | Condition | Priority |
|---|---|---|
| `high_vol` | VIX ≥ threshold (default 25.0) | 1 — safety gate, overrides everything |
| `bull` | RSI ≥ 55 AND MACD > 0 | 2 |
| `bear` | RSI ≤ 45 AND MACD < 0 | 3 |
| `neutral` | Neither of the above | 4 — default |

Thresholds are configurable in `config.yaml` under `regime:`.

---

### 3.2 Regime Detection v2 — HMM/Hurst Statistical Engine (Primary)

The v2 engine is a pure-computation Python package (`regime-detection`) that uses a hybrid consensus approach combining five independent statistical signals to classify market state in real time.

**Package:** `regime-detection` (installed as editable dependency from `/Users/user/regime-detection`)

**How it works — the consensus pipeline:**

```
Daily OHLCV bars (2 years) fed via .update()
        │
        ├─ GaussianHMM (3-state)        → BULL / BEAR / CHOP          [40% weight]
        ├─ DFA Hurst Exponent           → 0.0 (mean-revert) → 1.0 (trend)  [30% weight]
        ├─ BinSeg Change Point (CPD)    → structural_break: True/False  [20% weight]
        ├─ Volatility Regime            → LOW_STABLE / MODERATE / EXPANDING / CONTRACTING
        └─ Liquidity Heuristic          → CONSOLIDATION / TRAP / PASSED
                │
                ▼
        Consensus Vote → consensus_state + confidence_score
                │
                ▼
        Recommendation → OPTIONS_INCOME / NO_TRADE
        Exit Mandate   → True if regime shift detected
```

**Key output fields from `get_current_regime()`:**

| Field | Values | Bot use |
|---|---|---|
| `consensus_state` | `BULL_PERSISTENT`, `BEAR_PERSISTENT`, `CHOP_NEUTRAL`, `TRANSITION`, `UNKNOWN` | Drives market_regime translation |
| `confidence_score` | 0.0–1.0 | Displayed in scan output; future weighting |
| `volatility_regime` | `LOW_STABLE`, `MODERATE`, `EXPANDING`, `CONTRACTING` | `EXPANDING` → `high_vol` override |
| `recommended_logic` | `OPTIONS_INCOME`, `NO_TRADE` | `NO_TRADE` → `high_vol` override |
| `exit_mandate` | `True` / `False` | **Force-closes all open positions on that symbol immediately** |
| `signals.hmm_label` | `BULL`, `BEAR`, `CHOP` | Visible in scan output |
| `signals.hurst_dfa` | float | Visible in scan output |
| `signals.structural_break` | bool | Immediate exit mandate trigger |

**Temporal matrix for this bot:**
The bot uses `strategy_type="options_income"` and `market_class="us_stocks"`, which selects the following parameters from the module's temporal matrix:

| Parameter | Value |
|---|---|
| Signal timeframe | 1d (daily candles) |
| Lookback buffer | 252 bars (1 trading year) |
| HMM stability bars | 5 (majority vote over last 5 bars) |
| Min training bars | 100 (warmup threshold) |

**Bootstrap behaviour:**
On first scan after bot startup, the bridge fetches 2 years of daily OHLCV per symbol and feeds all bars to the HMM before the first regime reading is taken. This skips the 100-bar warmup period and produces valid signals from tick 1. The bootstrap adds ~45 seconds to the first scan only; all subsequent scans use the cached HMM state and take normal scan time (~5 seconds).

**Consensus state rules:**

| State | Conditions |
|---|---|
| `BULL_PERSISTENT` | HMM=BULL + Hurst ≥ 0.60 + no structural break + vol not EXPANDING |
| `BEAR_PERSISTENT` | HMM=BEAR + Hurst ≥ 0.60 + no structural break + vol not EXPANDING |
| `CHOP_NEUTRAL` | Hurst < 0.60 (Hurst overrides HMM for persistence classification) |
| `TRANSITION` | Structural break, or EXPANDING vol + trending Hurst, or signal conflict |
| `UNKNOWN` | Insufficient data or HMM not yet converged |

**HMM robustness guards (from live testing):**
- *Near-zero variance guard*: stale/flat price data → returns CHOP at 0.5 confidence instead of crashing
- *Direction-aware fallback*: single-regime data where states can't differentiate → checks average return direction → BULL/BEAR/CHOP
- *Majority vote stability*: most-common label over last 5 bars must have >50% share — prevents oscillation during noisy periods

**Scan console output (per symbol):**
```
── US.SPY ──────────────────────────────────────────
Price    $662.29  │  Regime ⚡ high_vol  │  VIX 27.2
RSI      34  │  %B -0.14  │  MACD -4.82  │  IV Rank 69  (⚠ only 12d history)
Regime v2: CHOP_NEUTRAL/OPTIONS_INCOME (conf=0.72 vol=LOW_STABLE)
```

---

### 3.3 Regime Translation — v2 → Bot Regime Strings

The v2 module outputs rich statistical states. These are translated to the bot's four internal regime strings (`bull`, `bear`, `neutral`, `high_vol`) using `translate_to_bot_regime()` in `regime_bridge.py`. The translation is applied in priority order:

| Priority | Condition | → market_regime | Rationale |
|---|---|---|---|
| 1 | VIX ≥ `high_vol_vix_threshold` (25.0) | `high_vol` | VIX safety gate preserved from v1 |
| 2 | `volatility_regime` = `EXPANDING` | `high_vol` | Vol blowing out = dangerous to sell premium |
| 3 | `recommended_logic` = `NO_TRADE` | `high_vol` | Module explicitly says stand aside |
| 4 | `consensus_state` = `BULL_PERSISTENT` | `bull` | Trending up — sell puts, not calls |
| 5 | `consensus_state` = `BEAR_PERSISTENT` | `bear` | Trending down — sell calls, not puts |
| 6 | `consensus_state` = `CHOP_NEUTRAL` | `neutral` | Range-bound — all strategies eligible |
| 7 | `consensus_state` = `TRANSITION` or `UNKNOWN` | `high_vol` | Regime uncertain — stand aside |

**Why the VIX gate is preserved:**
The v2 module evaluates market structure (trend, volatility regime, structural breaks) but does not have a concept of "absolute VIX level is dangerous." The VIX gate in the translation layer ensures that extreme market-wide stress (VIX > 25) blocks all new entries regardless of what the per-symbol HMM sees.

**v1 fallback path:**
If `bridge_instance` is unavailable (package not installed) or returns an empty dict, `market_scanner.py` automatically falls back to `RegimeDetector.detect(technicals, vix)` — the original rule-based detector. No manual intervention needed.

---

### 3.4 Exit Mandate — Regime-Shift Force Close

The most operationally significant feature of v2 is the **exit mandate**. When the regime module detects a real-time structural shift in a symbol's price behaviour, it sets `exit_mandate = True` in the output. The bot force-closes all open positions on that symbol immediately, regardless of P&L, DTE, or any other exit rule.

**Exit mandate triggers (from regime-detection spec Section 5.6):**

| Trigger | Grace Period |
|---|---|
| CPD structural break detected | Immediate (no grace) |
| Hurst ≥ 0.60 when previously CHOP_NEUTRAL | Immediate |
| Volatility → EXPANDING when previously CHOP_NEUTRAL | Immediate |
| Consensus state change (e.g. CHOP→BULL) | 2-bar confirmation |

**Implementation:**
- Every monitor cycle (~30 min), `bot_scheduler._monitor_job()` checks `bridge_instance.get_regime(sym)["exit_mandate"]` for every symbol with an open position
- If `True`: calls `PositionMonitor.close_all_regime_shift(symbol=sym)` immediately, before normal exit rule checks
- The close is recorded as `close_reason = "regime_shift"` in the ledger
- Console prints a `🚨 REGIME SHIFT` line with consensus state, volatility regime, and structural_break flag

**Live example (2026-03-14):**
```
[REGIME SHIFT] exit_mandate=True for US.AMZN | consensus=BEAR_PERSISTENT | vol=LOW_STABLE | break=False
[REGIME SHIFT] Exit mandate fired — force-closing 1 position(s) (symbol=US.AMZN)
[REGIME SHIFT] CLOSED: #9 US.AMZN bear_call_spread | P&L=$+77.50

  ── ⚠️  REGIME SHIFT — 1 position(s) force-closed:
    🚨 REGIME SHIFT  #9  US.AMZN  bear_call_spread  P&L $+77.50
```

AMZN's Hurst had crossed above 0.60, indicating a shift from mean-reverting (CHOP) to persistent bear trend. The position was closed with +$77.50 profit rather than waiting for a potential deterioration in the buffer.

---

## 4. Entry Criteria & Signal Filters

Every morning at 09:35 ET the scanner runs the full data pipeline in two passes:

**Pass 1 — Collect all signals (no execution yet):**

```
For each symbol:
  1. Download 6 months daily OHLCV (Yahoo Finance)
  2. Compute RSI, MACD, Bollinger %B (TechnicalAnalyser)
  3. Fetch current VIX (Yahoo Finance)
  4. Fetch 2 years daily OHLCV for HMM (Yahoo Finance — cached)
  5. Update regime v2 bridge (RegimeBridge.update → HMM/Hurst computation)
  6. Translate regime v2 → market_regime string via translate_to_bot_regime()
     Fallback: RegimeDetector.detect(technicals, vix) if v2 unavailable
  7. Fetch option expiries in 21–45 DTE range (MooMoo / IBKR)
  8. Check IV Rank (stored daily IV history, min 30 days required)
  9. Check upcoming earnings dates (Yahoo Finance)
 10. Read shares held (broker account or config override)
 11. Count open positions for this symbol (paper ledger)
 12. Assemble MarketSnapshot (includes regime_v2 dict) → pass to each strategy
 13. Collect every qualifying signal — do NOT execute yet
     ↳ Each signal carries the full MarketSnapshot for entry context logging
```

**Pass 2 — Rank, select, and execute:**

```
 14. Score all collected signals (SignalRanker)
 15. Sort candidates by composite score descending
 16. Walk ranked list top-to-bottom:
       - Apply portfolio guard checks (risk limits, iron condor prevention, daily cap)
       - Execute approved signals until daily limit reached or pool exhausted
       - Write full entry context to ledger at execution time (RSI, %B, MACD, VIX,
         spot price, buffer %, reward/risk — see Section 14)
 17. Log ranked table with selection rationale
```

This two-pass design ensures the daily trade budget is always allocated to the best available opportunities across the *entire* watchlist, not just the ones that appear earliest in the config file. See [Section 4.1](#41-signal-ranking--selection) for the scoring formula and configuration.

**IV Rank** is the key filter. It measures where current implied volatility sits relative to its 52-week range:

```
IV Rank = (Current IV − 52-week Low IV) / (52-week High IV − 52-week Low IV) × 100
```

A rank of 30 means current IV is in the 30th percentile of the past year — premium is above-average. The bot needs at least 30 days of IV history to compute this; a full 252 trading days gives the most reliable signal.

---

## 4.1 Signal Ranking & Selection

### The problem with FIFO execution

Without ranking, the bot executes signals in watchlist order — the first qualifying symbol fills the first slot, the second fills the second, and so on. With a limited trade budget (e.g. `max_trades_per_day: 2`), this means the best opportunities in the watchlist can be systematically skipped in favour of merely adequate ones that happened to appear earlier in the list.

```
Watchlist order:  SPY → QQQ → NVDA → AAPL → META → AMZN
max_trades_per_day: 2

Without ranking:
  SPY  → signal (IV Rank 41, R/R 0.21) → FILLED  ← trade 1
  QQQ  → signal (IV Rank 44, R/R 0.22) → FILLED  ← trade 2
  NVDA → signal (IV Rank 72, R/R 0.31) → BLOCKED  ← daily limit reached
  AAPL → signal (IV Rank 58, R/R 0.28) → BLOCKED
```

NVDA is the clearly superior trade — elevated IV and better structure — yet it is never reached. The slot is consumed by SPY and QQQ purely because they are listed first.

### The two-pass solution

Ranking decouples scanning from execution. The scan job runs in two distinct phases:

```
Phase 1 — Collect:
  Scan ALL symbols in the watchlist
  Run all strategy gates for each symbol
  Collect every qualifying signal — do not execute any

Phase 2 — Rank → Select → Execute:
  Score each signal using configurable weighted formula
  Sort candidates by score descending
  Apply portfolio guard checks (risk limits, daily caps, iron condor prevention)
  Execute top N signals that pass guard checks
  Log ranked order and selection rationale
```

With ranking, the same example becomes:

```
All signals collected:
  NVDA  → signal (IV Rank 72, buffer 8.2%, R/R 0.31) → score 0.847
  AAPL  → signal (IV Rank 58, buffer 6.5%, R/R 0.28) → score 0.661
  QQQ   → signal (IV Rank 44, buffer 4.8%, R/R 0.22) → score 0.389
  SPY   → signal (IV Rank 41, buffer 5.1%, R/R 0.21) → score 0.412

Ranked:  NVDA (0.847) → AAPL (0.661) → SPY (0.412) → QQQ (0.389)
Selected (top 2):  NVDA ✅   AAPL ✅
Skipped:           SPY  (ranked 3rd)   QQQ  (ranked 4th)
```

The bot now consistently allocates its daily trade budget to the best available opportunities across the entire watchlist, regardless of symbol order in config.

### Scoring formula

Each signal is scored on three dimensions, each normalised to 0–1 within the current candidate pool before weighting:

| Dimension | What it measures | Config key |
|---|---|---|
| IV Rank | How elevated is current premium vs the past year? High IV = sell expensive options | `weight_iv_rank` |
| Buffer distance | How far is the short strike from the current price, as % of spot? Larger buffer = more room to be wrong | `weight_buffer_pct` |
| Reward/risk ratio | How much premium collected per dollar at risk? Higher R/R = more efficient trade | `weight_reward_risk` |

The composite score:

```
score = (iv_rank_norm    × weight_iv_rank)
      + (buffer_norm     × weight_buffer_pct)
      + (reward_risk_norm × weight_reward_risk)

Default weights:  0.40 + 0.35 + 0.25 = 1.00
```

**Buffer formula by strategy:**

```
Bear call spread:  buffer = (short_call_strike − spot_price) / spot_price × 100
Bull put spread:   buffer = (spot_price − short_put_strike)  / spot_price × 100
Covered call:      buffer = (short_call_strike − spot_price) / spot_price × 100
```

A larger buffer means the underlying has further to travel before threatening the short strike. For example, a bear call spread with short strike 10% above spot (buffer = 10%) is structurally safer than one 4% above spot (buffer = 4%), even if both pass the delta gate.

### Normalisation

To make the three dimensions comparable regardless of their natural scales (IV Rank is 0–100, buffer is 0–20%, R/R is 0.15–0.40), each is min-max normalised across the current candidate pool:

```
norm(x) = (x − min_in_pool) / (max_in_pool − min_in_pool)
```

If only one signal qualifies, all norms are 1.0 and the score equals the sum of weights — ranking is a no-op and the single signal is selected if the portfolio guard approves it.

If all candidates have the same value on a dimension (e.g. all have IV Rank 55 because the bootstrap fallback is active), that dimension contributes equally to all scores and effectively becomes a tiebreaker of 0.0 — only the other two dimensions differentiate candidates.

### Configuration

```yaml
signal_ranker:
  enabled:             true   # false = revert to FIFO behaviour (no ranking)
  weight_iv_rank:      0.40   # 40% — how elevated is premium? (foundational filter)
  weight_buffer_pct:   0.35   # 35% — how much buffer to short strike?
  weight_reward_risk:  0.25   # 25% — how efficient is the trade?
```

Setting `enabled: false` reverts the bot to pre-ranking FIFO behaviour without any other changes — useful for A/B comparison during validation.

### Scan output with ranking active

The gate display remains unchanged — all gates are shown for every symbol and strategy. After the gate analysis, a ranked selection table is added:

```
════════════════════════════════════════════════════════════
  SIGNAL RANKING  │  4 candidates → selecting top 2
════════════════════════════════════════════════════════════
  Rank  Symbol   Strategy          IV Rk  Buffer   R/R   Score
  ────  ──────── ────────────────  ─────  ──────  ─────  ─────
   1    US.NVDA  bear_call_spread    72    8.2%   0.31   0.847  ✅ selected
   2    US.AAPL  bear_call_spread    58    6.5%   0.28   0.661  ✅ selected
   3    US.SPY   bear_call_spread    41    5.1%   0.21   0.412  ⏭  skipped (daily limit)
   4    US.QQQ   bear_call_spread    44    4.8%   0.22   0.389  ⏭  skipped (daily limit)
════════════════════════════════════════════════════════════
```

### Edge cases and guardrails

**Single candidate:** Ranking is a no-op. The one signal is sent to the portfolio guard as normal.

**No candidates:** Ranking outputs nothing. The scan ends with "no signals this cycle."

**Portfolio guard overrides ranking:** Even the top-ranked signal is blocked if it violates a guard rule (iron condor prevention, total risk limit, duplicate position). The next-ranked signal is tried instead. This continues until either N signals are approved or the candidate pool is exhausted.

**Ties:** When two signals have identical scores (rare with floating-point arithmetic), the original watchlist order is used as a secondary tiebreaker — preserving deterministic behaviour.

**Ranking disabled:** Setting `signal_ranker.enabled: false` bypasses the ranker entirely. Signals are executed in the order strategies return them (watchlist order × strategy registration order). Useful for debugging or validating that ranking actually improves selection quality.

---

## 5. Exit Rules & Position Management

The bot checks every open position every 30 minutes during market hours. Five exit triggers are evaluated in priority order:

> **Exit context is captured automatically at every close.** Days held, DTE remaining, underlying spot price, IV Rank, VIX, and percentage of premium captured are all written to the trade record at the moment of exit. See [Section 14](#14-trade-context-logging) for the full field list.

### Regime Shift — Priority 0 (Pre-empts all other exit rules)

Force-closes all open positions on a symbol immediately when the regime module fires `exit_mandate = True`. This runs *before* normal exit rule checks on every monitor cycle. The close is recorded as `close_reason = "regime_shift"`.

See [Section 3.4](#34-exit-mandate--regime-shift-force-close) for trigger conditions and a live example.

### Stop Loss — Priority 1
Close immediately when the current option price reaches **3× the original credit collected**, provided the position has been held for at least **5 days**.

```
Example: Sold spread for $3.45. Stop triggers if current price ≥ $10.35.
Loss = (3.45 − 0) × 100 = $345 per contract at stop.
Hard ceiling: loss can never exceed spread width (e.g. $10 wide = max $1,000 − credit).
```

**Why 3× and not 2×?**

The original 2× stop was triggered on day 1 of a trade — not because the underlying moved against the position, but because a small VIX uptick (19.8 → 20.1) caused option prices to reprice upward through vega exposure. The underlying had not moved at all.

This is a known weakness of spread-value-based stops: in the first few days after opening, the position has almost zero theta decay to offset vega exposure. A 1–2 point VIX move can easily double a spread's mark-to-market value without the stock actually threatening the short strike. The 2× stop mistakes vega noise for a real directional move.

Moving to 3× gives more room to breathe through intraday IV spikes while still enforcing a defined loss well before the position reaches max loss. The spread width (e.g. $10 wide) is always the hard ceiling regardless of where the stop is set.

**Why 5-day minimum hold before stop activates?**

Theta decay is negligible in the first week. With 33 DTE at entry, the position is almost entirely driven by vega (IV sensitivity) and delta (directional move). Stopping out in this window means abandoning a structurally valid trade purely on noise. After day 5, meaningful theta has accrued, IV spikes are more likely to mean-revert back through the position, and the stop-loss rule activates normally.

During the 5-day window, the bot logs `STOP LOSS SUPPRESSED | held=Xd < min=5d — holding` and continues monitoring. The position still has a hard ceiling from the spread width — the maximum possible loss is always known and defined.

### DTE Close — Priority 2
Close any position when **21 or fewer days remain to expiry**, regardless of profit or loss. This avoids gamma risk — options accelerate unpredictably in their final weeks.

### Take Profit — Priority 3
Close when the position has **decayed to 50% of the original credit** (i.e., you keep 50% of what you collected and buy back cheaply).

```
Example: Sold for $3.45. Close if current price ≤ $1.73.
Profit = $1.72 × 100 = $172 per contract.
```

### Expired
If an option expires worthless (price = 0 at expiry), close the position and record max profit.

All thresholds are configurable under `position_monitor.exit_rules` in `config.yaml`.

**Exit reason summary for analytics:**

| `close_reason` | Trigger | Interpretation |
|---|---|---|
| `regime_shift` | exit_mandate from HMM engine | Regime structural change detected — closed early |
| `stop_loss` | Price ≥ 3× credit (after day 5) | Loss management |
| `dte_close` | DTE ≤ 21 | Gamma risk avoidance |
| `take_profit` | Price ≤ 50% credit | Systematic profit capture |
| `expired_worthless` | DTE = 0, price ≈ 0 | Best outcome — full premium kept |
| `manual` | Manual override | Human-initiated close |

---

## 6. Broker Architecture

The bot was designed with a **split connector architecture** — market data and order execution are handled by independent, interchangeable connectors. This allows mixing brokers to minimise cost while maintaining full automation.

### Why two brokers?

**MooMoo Singapore** provides a free API through MooMoo OpenD (a local desktop daemon). Option chains, Greeks, live quotes, and expiry data are all accessible at no cost. However, the Singapore API cannot access real trading accounts or place live orders — this is a permanent regional restriction.

**Interactive Brokers (IBKR)** provides full API access to real accounts and supports live order placement. It requires a market data subscription (~$14.50 SGD/month) for live streaming prices via API.

### Three operating modes

The `broker:` section in `config.yaml` independently controls the data source and execution destination:

---

**Mode 1 — Hybrid (current default)**

MooMoo provides free market data. IBKR places real orders in your live account.

```yaml
broker:
  data:      "moomoo"   # free option chains, Greeks, IV from MooMoo OpenD
  execution: "ibkr"     # real orders placed in IBKR account U18705798
```

*Requirements:* MooMoo OpenD running on localhost · IBKR TWS running and logged in

*Cost:* $0/month for market data

*Limitation:* Two applications must be running simultaneously

---

**Mode 2 — Full IBKR**

Both data and execution via IBKR. Eliminates the MooMoo dependency entirely.

```yaml
broker:
  data:      "ibkr"
  execution: "ibkr"
```

*Requirements:* IBKR TWS running · US Equity and Options market data subscription active

*Cost:* ~$14.50 SGD/month (waived if commissions ≥ $30/month)

*Benefit:* Single application to run, NBBO data quality, no MooMoo dependency

---

**Mode 3 — Full MooMoo (paper trading only)**

Both data and execution via MooMoo. Cannot access real accounts in Singapore — paper trading only.

```yaml
broker:
  data:      "moomoo"
  execution: "moomoo"
```

*Use case:* Initial paper trading validation before funding IBKR account

---

### Connector interface

Both `MooMooConnector` and `IBKRConnector` implement an identical public interface defined in `src/connectors/connector_protocol.py`. Every component in the bot — the scanner, strategies, order router, and position monitor — accepts any connector that satisfies this interface. Switching brokers requires only a config change, not a code change.

### MooMoo OpenD setup

MooMoo OpenD is a local desktop application that acts as a bridge between the bot and MooMoo's servers. It must be running and logged in before starting the bot.

```
Default connection: 127.0.0.1:11111
Trade environment:  SIMULATE (paper) or REAL
```

Account IDs used:
- Stock account `565755` — reads TSLA share positions
- Options account `4310610` — all options orders

### IBKR TWS setup

Trader Workstation (TWS) must be running and logged in. API access must be enabled:

```
TWS: Edit → Global Config → API → Settings
  ✓ Enable ActiveX and Socket Clients
  ✓ Socket port: 7496 (live) or 7497 (paper)
  ✓ Trusted IPs: 127.0.0.1
```

Port reference:

| Application | Mode | Port |
|---|---|---|
| TWS | Paper | 7497 |
| TWS | Live | 7496 |
| IB Gateway | Paper | 4002 |
| IB Gateway | Live | 4001 |

---

## 7. Configuration Reference

All configuration lives in `config/config.yaml`. The file is self-documenting with inline comments. This section explains every block in plain English.

### `mode`

```yaml
mode: paper   # "paper" | "live"
```

The master safety switch. In `paper` mode, no real orders are ever placed — all fills are simulated at mid-price. Change to `live` only after the validation report passes all gates.

---

### `broker`

```yaml
broker:
  data:      "moomoo"   # "moomoo" | "ibkr"
  execution: "ibkr"     # "moomoo" | "ibkr"
```

See [Section 10](#10-switching-broker-modes) for all combinations.

---

### `moomoo`

```yaml
moomoo:
  host:              "127.0.0.1"
  port:              11111
  trade_env:         "SIMULATE"   # "SIMULATE" = paper | "REAL" = live
  stock_account_id:  "565755"
  option_account_id: "4310610"
```

Only relevant when `broker.data` or `broker.execution` is `"moomoo"`. Account IDs are discovered by running the Phase 1 connectivity test.

---

### `ibkr`

```yaml
ibkr:
  host:      "127.0.0.1"
  port:      7496        # 7496 = live TWS, 7497 = paper TWS
  client_id: 1
  account:   "U18705798"
```

Only relevant when `broker.data` or `broker.execution` is `"ibkr"`. The `account` field can be left blank for auto-detection.

---

### `universe`

```yaml
universe:
  watchlist:
    - "US.TSLA"
    - "US.AAPL"
    - "US.SPY"

  shares_held:
    "US.TSLA": 100
    "US.AAPL": 200
```

`watchlist` is the list of symbols scanned each morning. See [Section 8](#8-adding-assets-to-the-watchlist) for details on adding symbols.

`shares_held` is an override used during paper trading when the broker API cannot see your real position. Set to 0 or remove the entry when running live (the broker API will be queried directly).

---

### `regime`

```yaml
regime:
  high_vol_vix_threshold:  25.0   # VIX ≥ this → high_vol regardless of v2 consensus
  bull_rsi_threshold:      55.0   # v1 fallback only
  bear_rsi_threshold:      45.0   # v1 fallback only
  macd_threshold:          0.0    # v1 fallback only
```

`high_vol_vix_threshold` is active in both v1 and v2 paths — it is always applied as a first-priority safety gate regardless of what the HMM reports. The RSI and MACD thresholds are only used when the v2 module is unavailable (fallback path).

---

### `options`

```yaml
options:
  target_dte_min:        21    # minimum days to expiry
  target_dte_max:        45    # maximum days to expiry
  earnings_buffer_days:  7     # skip expiries within 7 days of earnings
  min_open_interest:     100   # minimum contract OI for liquidity
  otm_call_delta_min:    0.20  # minimum delta for short call
  otm_call_delta_max:    0.35  # maximum delta for short call
  spread_width_target:   10.0  # target $10 wide spreads
```

The DTE range of 21–45 is the standard "theta sweet spot" — premium decays fastest in this window. The delta range of 0.20–0.35 targets roughly the 20th–35th percentile OTM — high enough premium to be worthwhile, low enough probability of being in-the-money at expiry.

---

### `strategies`

```yaml
strategies:
  covered_call:
    enabled:                  true
    min_iv_rank:              30
    max_rsi:                  70
    max_concurrent_positions: 2

  bear_call_spread:
    enabled:                  true
    min_iv_rank:              35
    min_rsi_for_spread:       45    # don't sell calls in freefall
    min_pct_b:                0.40
    min_credit:               0.50
    min_reward_risk:          0.20
    spread_width_target:      10.0
    max_concurrent_positions: 3
    allowed_regimes:
      - "bear"
      - "neutral"

  bull_put_spread:
    enabled:                  true
    min_iv_rank:              35    # only sell when premium is elevated
    min_rsi_floor:            35    # don't sell puts in freefall (crash risk)
    max_rsi_ceiling:          65    # avoid extreme overbought (reversal risk)
    min_pct_b:                0.20  # price not at extreme lows (puts too close)
    min_credit:               0.50  # minimum premium worth collecting
    min_reward_risk:          0.20  # minimum 1:5 risk/reward
    spread_width_target:      10.0  # target $10 wide spread
    max_concurrent_positions: 3
    allowed_regimes:
      - "bull"
      - "neutral"
```

Set `enabled: false` to completely disable a strategy without removing it. See [Section 9](#9-enabling--disabling-strategies) for common scenarios.

Note that `bear_call_spread` and `bull_put_spread` use **different RSI parameter names** deliberately — `min_rsi_for_spread` (calls) vs `min_rsi_floor` / `max_rsi_ceiling` (puts) — because the put side has both a floor and a ceiling, while the call side only has a floor.

---

### `portfolio_guard`

```yaml
portfolio_guard:
  max_open_positions:  6
  max_risk_pct:        0.05    # 5% of portfolio per trade
  max_total_risk_pct:  0.20    # 20% of portfolio total
  max_trades_per_day:  3
  portfolio_value:     100000
```

The portfolio guard is a hard firewall — signals are blocked regardless of strategy evaluation if any limit is breached. `portfolio_value` should be updated periodically to keep the percentage-based limits meaningful.

**Restart safety:** `max_trades_per_day` is preserved correctly across bot restarts. On startup, `PortfolioGuard.restore_from_ledger()` calls `ledger.get_trades_opened_on(today)` to count all trades already placed today (including those subsequently stopped out or closed) and restores `_trades_today` before any new scan runs.

---

### `signal_ranker`

```yaml
signal_ranker:
  enabled:             true   # false = revert to FIFO (no ranking)
  weight_iv_rank:      0.40   # IV Rank weight  — how elevated is premium?
  weight_buffer_pct:   0.35   # Buffer weight   — distance to short strike as % of spot
  weight_reward_risk:  0.25   # R/R weight      — premium collected per $ at risk
```

Controls whether signals are ranked before execution and how the composite score is computed. See [Section 4.1](#41-signal-ranking--selection) for full details.

**When to adjust weights:**
- `weight_iv_rank` up → bot strongly prefers high-IV environments; borderline-IV signals penalised more
- `weight_buffer_pct` up → bot prefers safety margin over premium; useful in choppy/uncertain regimes
- `weight_reward_risk` up → bot prefers trade efficiency; useful when spreads are narrowing and credit is thin

Setting `enabled: false` reverts to pre-ranking FIFO behaviour with no other changes required.

---

### `position_monitor`

```yaml
position_monitor:
  check_interval_minutes: 30
  max_price_failures:     3
  exit_rules:
    stop_loss_multiplier:  3.0   # close when spread costs 3× the original credit
                                 # widened from 2× — 2× triggers on day-1 IV spikes
                                 # even when underlying has not moved (vega noise)
    min_days_before_stop:  5     # suppress stop loss for first 5 days after opening
                                 # theta is negligible in week 1; vega dominates
    take_profit_pct:       0.50  # close when spread decays to 50% of original credit
    dte_close_threshold:   21    # close any position with ≤ 21 DTE (gamma risk)
    expired_dte_threshold: 0     # record as expired if DTE reaches 0
```

See [Section 5](#5-exit-rules--position-management) for the full rationale behind each exit rule.

---

### `validation`

```yaml
validation:
  min_trades:       10
  min_win_rate:     0.60
  min_sharpe_like:  0.50
```

Criteria the weekly validation report checks before recommending live trading. All three gates must pass simultaneously.

---

## 8. Adding Assets to the Watchlist

### Step 1 — Add the symbol

Edit `config/config.yaml` under `universe.watchlist`. All symbols use the `"US.TICKER"` format:

```yaml
universe:
  watchlist:
    - "US.TSLA"
    - "US.AAPL"    # ← add new symbol here
    - "US.NVDA"    # ← add new symbol here
```

### Step 2 — Declare shares held (paper trading only)

If you own shares in the new symbol and want covered call signals, add it to the `shares_held` override:

```yaml
  shares_held:
    "US.TSLA": 100
    "US.AAPL": 200    # ← 200 shares = 2 covered calls possible
    "US.NVDA": 0      # ← 0 shares = only bear call spreads eligible
```

Once running live against a real broker account, remove this override — the bot will query the account directly.

### Step 3 — No code changes needed

The scanner, strategies, and order router all work off the watchlist dynamically. No code needs to change. The v2 regime bridge will automatically bootstrap 2 years of HMM history for any new symbol on its first scan.

### What symbols are suitable

The strategies work best on liquid US equity options. Good candidates have:
- High average daily volume (>1M shares/day)
- Active options market (open interest >1,000 per strike)
- Implied volatility that moves meaningfully (IV Rank frequently above 30)
- Regular options liquidity in the 21–45 DTE range

Examples: TSLA, AAPL, NVDA, SPY, QQQ, AMZN, MSFT, GOOGL

---

## 9. Enabling / Disabling Strategies

### Trade only covered calls (e.g. you want income only on held shares)

```yaml
strategies:
  covered_call:
    enabled: true
  bear_call_spread:
    enabled: false
  bull_put_spread:
    enabled: false
```

### Trade only bear call spreads (upside premium, no shares needed)

```yaml
strategies:
  covered_call:
    enabled: false
  bear_call_spread:
    enabled: true
  bull_put_spread:
    enabled: false
```

### Trade only bull put spreads (downside premium, no shares needed)

```yaml
strategies:
  covered_call:
    enabled: false
  bear_call_spread:
    enabled: false
  bull_put_spread:
    enabled: true
```

### Trade both spread directions (two-sided premium collection — current setup)

```yaml
strategies:
  covered_call:
    enabled: true    # fires on symbols where you hold shares
  bear_call_spread:
    enabled: true    # fires in bear/neutral regime
  bull_put_spread:
    enabled: true    # fires in bull/neutral regime
```

In a neutral regime both spread strategies can fire. The portfolio guard prevents them from firing on the *same* symbol simultaneously (iron condor prevention), so they naturally spread across different symbols in the watchlist.

### Make bear call spreads more aggressive (lower filters)

```yaml
  bear_call_spread:
    min_iv_rank:       25    # lower threshold — trade at lower IV
    min_credit:        0.30  # accept smaller premium
    min_reward_risk:   0.15  # accept lower reward/risk ratio
    allowed_regimes:
      - "bull"               # add bull regime (more opportunities, more risk)
      - "bear"
      - "neutral"
```

### Make bull put spreads more conservative (tighter filters)

```yaml
  bull_put_spread:
    min_iv_rank:       45    # only trade at higher IV
    min_rsi_floor:     40    # higher floor — stricter freefall protection
    max_rsi_ceiling:   60    # lower ceiling — stricter overbought filter
    min_credit:        0.75  # require more premium
    max_concurrent_positions: 2   # fewer simultaneous positions
```

---

## 10. Switching Broker Modes

Only the `broker:` section in `config.yaml` needs to change. No code changes required.

### Hybrid mode (MooMoo data + IBKR execution) — current

```yaml
broker:
  data:      "moomoo"
  execution: "ibkr"
```

**Prerequisites:**
- MooMoo OpenD running and logged into your MooMoo account
- IBKR TWS running, logged in, with API enabled on port 7496
- IBKR account funded and live

---

### Full IBKR mode (data + execution both via IBKR)

```yaml
broker:
  data:      "ibkr"
  execution: "ibkr"
```

**Prerequisites:**
- IBKR TWS running and logged in
- US Equity and Options Add-On Streaming Bundle subscription active (~$14.50 SGD/month, waived if commissions ≥ $30/month)
- Market Data API Acknowledgement signed in IBKR Account Management

**Benefit over hybrid:** MooMoo OpenD is no longer needed. Single application to manage. IBKR data uses NBBO (National Best Bid/Offer), which is the most accurate consolidated price feed available.

---

### Full MooMoo mode (paper trading only)

```yaml
broker:
  data:      "moomoo"
  execution: "moomoo"
```

**Use case:** Running paper trading validation before the IBKR account is funded. Note: the Singapore MooMoo API cannot access real trading accounts — this mode is paper-only.

---

## 11. Going Live — Validation Gate

The bot enforces a mandatory paper trading period before live orders are possible. Every Friday at 16:30 ET, a validation report is generated and saved to `data/validation_report_{date}.txt`.

### Gate criteria (all must pass)

| Gate | Threshold | Description |
|---|---|---|
| Minimum trades | ≥ 10 closed trades | Enough sample size to evaluate |
| Win rate | ≥ 60% | At least 6 in 10 trades profitable |
| Positive expectancy | avg P&L > 0 | Expected value per trade is positive |
| Max drawdown | No single loss > 2× avg max loss | No catastrophic outlier losses |
| Sharpe-like ratio | ≥ 0.50 | P&L consistency (total / std deviation) |

### Current validation status (as of 2026-03-15)

| Metric | Value | Gate |
|---|---|---|
| Closed trades | 8 | ⚠️  2 more needed |
| Win rate | 88% | ✅ above 60% threshold |
| Realised P&L | +$917.50 | ✅ positive |
| Exit types | 3× take_profit, 2× dte_close, 1× stop_loss, 1× regime_shift | tracking |
| Open positions | 1 (QQQ #10, 43% captured, 33 DTE) | — |

### Go-live procedure

1. Run in paper mode for at least 4 weeks and accumulate 10+ closed trades
2. Confirm the Friday report shows `GO LIVE ✅` across all gates
3. Fund the IBKR account and transfer or buy the relevant shares
4. Update `config.yaml`:

```yaml
mode: live          # ← change from "paper"

broker:
  data:      "moomoo"
  execution: "ibkr"

universe:
  shares_held:       # ← remove or set to 0 (live account queried directly)
    "US.TSLA": 0
```

5. Start the bot and monitor the first few live trades manually

There is no way to bypass this gate programmatically — the bot simply will not place live orders while `mode: paper`.

---

## 12. Daily Operations

### Starting the bot

```bash
# Ensure MooMoo OpenD is running (hybrid mode)

cd /path/to/moomoo
python3 main.py
```

### Starting the dashboard

```bash
# In a separate terminal — reads the same paper_trades.db the bot writes to
cd /path/to/moomoo
python3 dashboard.py

# Custom port or DB path
python3 dashboard.py --port 8080
python3 dashboard.py --db data/paper_trades.db

# → Open http://127.0.0.1:5000 in your browser
```

### Running the analytics report (CLI)

```bash
# Full report — all 10 analytics sections printed to console
python3 analytics_report.py

# Single section only
python3 analytics_report.py --section 1      # exit type breakdown
python3 analytics_report.py --section 3      # IV crush contribution
python3 analytics_report.py --section overview

# Custom DB path
python3 analytics_report.py --db data/paper_trades.db

# No colour (pipe to file)
python3 analytics_report.py --no-color > report.txt
```

See [Section 16](#16-analytics-cli) for all sections and their meanings.

### Logs

All activity is logged to `logs/bot.log`. Each job is tagged for easy filtering:

```
[SCAN JOB]      — morning scan results, signals generated
[MONITOR JOB]   — position check results, exits triggered
[REGIME SHIFT]  — HMM exit mandate fires, force-close logged
[IV JOB]        — end-of-day IV collection
[REPORT JOB]    — weekly validation report
[LIVE ORDER]    — real money orders (always logged with this tag)
[PAPER]         — simulated fills
[RegimeBridge]  — HMM bootstrap and daily update events
```

---

## 13. File Structure

```
moomoo/
├── config/
│   └── config.yaml                  ← all configuration lives here
│
├── src/
│   ├── connectors/
│   │   ├── connector_protocol.py    ← shared interface (BrokerConnector)
│   │   ├── broker_factory.py        ← reads config, builds connectors
│   │   ├── moomoo_connector.py      ← MooMoo OpenD wrapper
│   │   ├── ibkr_connector.py        ← Interactive Brokers TWS wrapper
│   │   └── yfinance_connector.py    ← Yahoo Finance (OHLCV, VIX, earnings)
│   │
│   ├── market/
│   │   ├── market_scanner.py        ← daily data pipeline per symbol
│   │   │                               (v2 regime primary, v1 fallback)
│   │   ├── market_snapshot.py       ← immutable snapshot (includes regime_v2 dict)
│   │   ├── technical_analyser.py    ← RSI, MACD, Bollinger Bands
│   │   ├── options_analyser.py      ← delta filter, spread metrics
│   │   ├── iv_rank_calculator.py    ← stores daily IV, computes IV Rank
│   │   ├── regime_detector.py       ← v1 rule-based: bull/bear/neutral/high_vol
│   │   └── regime_bridge.py         ← v2 HMM bridge: one RegimeManager per symbol
│   │                                   translate_to_bot_regime(), bridge_instance singleton
│   │                                   (pip install -e /Users/user/regime-detection)
│   │
│   ├── strategies/
│   │   ├── base_strategy.py         ← abstract base class
│   │   ├── strategy_registry.py     ← manages list of active strategies
│   │   ├── trade_signal.py          ← signal data class (carries MarketSnapshot)
│   │   └── premium_selling/
│   │       ├── covered_call.py      ← covered call strategy
│   │       ├── bear_call_spread.py  ← bear call spread strategy (upside premium)
│   │       └── bull_put_spread.py   ← bull put spread strategy (downside premium)
│   │
│   ├── execution/
│   │   ├── order_router.py          ← paper simulation or live order placement
│   │   ├── portfolio_guard.py       ← position limits, risk checks
│   │   ├── signal_ranker.py         ← scores and ranks candidates before execution
│   │   ├── trade_manager.py         ← orchestrates guard → router → ledger
│   │   └── paper_ledger.py          ← SQLite trade journal (entry + exit context)
│   │                                   valid close_reasons include: regime_shift
│   │
│   ├── monitoring/
│   │   ├── exit_evaluator.py        ← stop loss, take profit, DTE rules
│   │   ├── position_monitor.py      ← runs exit checks every 30 min
│   │   │                               close_all_regime_shift() for exit mandate
│   │   └── validation_reporter.py   ← weekly go-live gate report
│   │
│   ├── notifier/
│   │   ├── signal_notifier.py       ← writes signals to file for manual review
│   │   └── trade_recorder.py        ← records manually placed trades
│   │
│   └── scheduler/
│       └── bot_scheduler.py         ← event loop, wires all components
│                                       regime exit_mandate check in _monitor_job()
│
├── analytics_report.py              ← standalone analytics CLI (10 sections)
├── dashboard.py                     ← single-file Flask dashboard (port 5000)
│                                       includes /analytics page (10 Bootstrap cards)
├── test_regime_live.py              ← live validation script for regime module
├── tests/                           ← 185+ unit tests (regime) + bot test suite
├── data/                            ← SQLite ledger + IV history + reports
└── logs/                            ← rotating log files
```

---

## Quick Reference Card

| Want to… | Change in config.yaml |
|---|---|
| Add a new stock to scan | `universe.watchlist` — add `"US.TICKER"` |
| Enable covered calls on shares you hold | `universe.shares_held` — add `"US.TICKER": 100` |
| Enable bear call spreads on a stock | Just add it to `watchlist` — no shares needed |
| Enable bull put spreads on a stock | Just add it to `watchlist` — no shares needed |
| Disable bear call spreads entirely | `strategies.bear_call_spread.enabled: false` |
| Disable bull put spreads entirely | `strategies.bull_put_spread.enabled: false` |
| Relax bull put spread RSI floor | `strategies.bull_put_spread.min_rsi_floor` — default is 35 |
| Tighten bull put spread RSI ceiling | `strategies.bull_put_spread.max_rsi_ceiling` — default is 65 |
| Disable signal ranking (revert to FIFO) | `signal_ranker.enabled: false` |
| Prioritise IV environment more strongly | `signal_ranker.weight_iv_rank` — raise above 0.40 |
| Prioritise strike buffer (safety) more | `signal_ranker.weight_buffer_pct` — raise above 0.35 |
| Prioritise trade efficiency (R/R) more | `signal_ranker.weight_reward_risk` — raise above 0.25 |
| Widen the stop loss | `position_monitor.exit_rules.stop_loss_multiplier` — default is 3.0 |
| Change minimum hold before stop | `position_monitor.exit_rules.min_days_before_stop` — default is 5 days |
| Close positions earlier | `position_monitor.exit_rules.dte_close_threshold` — raise from 21 |
| Take profit sooner | `position_monitor.exit_rules.take_profit_pct` — raise from 0.50 |
| Adjust VIX safety gate | `regime.high_vol_vix_threshold` — default 25.0 (applies to both v1 and v2) |
| Switch to full IBKR | `broker.data: "ibkr"` and `broker.execution: "ibkr"` |
| Go live | `mode: live` (only after validation report passes) |
| Pause all new trades | `regime.high_vol_vix_threshold: 0` (always high_vol) |
| Start the web dashboard | `python3 dashboard.py` → http://127.0.0.1:5000 |
| Run dashboard on a custom port | `python3 dashboard.py --port 8080` |
| Point dashboard at a specific DB | `python3 dashboard.py --db path/to/paper_trades.db` |
| Run analytics report (CLI) | `python3 analytics_report.py` |
| View single analytics section | `python3 analytics_report.py --section 3` |
| Validate regime module with live data | `python3 test_regime_live.py --asset US.SPY` |

---

## 14. Trade Context Logging

Every trade written to `paper_trades.db` carries two groups of contextual fields — entry context (captured when the trade opens) and exit context (captured when it closes). These fields enable post-hoc analysis of which market conditions led to winning vs losing trades, without relying on memory or separate logs.

### 14.1 Entry Context Fields

Captured at `record_open()` and written atomically with the trade record. All fields default to `NULL` if the `MarketSnapshot` is unavailable (backward compatible with trades opened before this feature).

| Column | Type | Description |
|---|---|---|
| `spot_price_at_open` | REAL | Underlying price at signal time |
| `buffer_pct` | REAL | % distance from spot to short strike |
| `reward_risk` | REAL | Net credit ÷ max loss — trade efficiency ratio |
| `rsi_at_open` | REAL | RSI(14) value at time of entry |
| `pct_b_at_open` | REAL | Bollinger %B at entry (0.0 = lower band, 1.0 = upper band) |
| `macd_at_open` | REAL | MACD line value at entry |
| `vix_at_open` | REAL | VIX index at entry |
| `short_strike` | REAL | Numeric strike price of the short leg |
| `long_strike` | REAL | Numeric strike price of the long leg (spreads only) |
| `atm_iv_at_open` | REAL | Raw ATM implied volatility % at entry |
| `theta_at_open` | REAL | Short leg theta (daily $ decay) |
| `vega_at_open` | REAL | Short leg vega (per 1% IV move) |
| `signal_score` | REAL | Composite ranking score (0–1) from SignalRanker |
| `entry_type` | TEXT | `'morning_scan'` or `'intraday'` |

**Buffer formula by strategy:**

```
Bear call spread:  buffer_pct = (short_call_strike − spot) / spot × 100
Bull put spread:   buffer_pct = (spot − short_put_strike) / spot × 100
Covered call:      buffer_pct = (short_call_strike − spot) / spot × 100
```

### 14.2 Exit Context Fields

Captured at `record_close()` by `PositionMonitor`. Fields are individually wrapped in try/except — a network failure at exit time does not abort the close; it simply leaves that field as `NULL`.

| Column | Type | Description |
|---|---|---|
| `days_held` | INTEGER | Calendar days from `opened_at` to `closed_at` (auto-computed) |
| `dte_at_close` | INTEGER | Days to expiry remaining when closed |
| `spot_price_at_close` | REAL | Underlying price at close time |
| `iv_rank_at_close` | REAL | IV Rank at close time |
| `vix_at_close` | REAL | VIX at close time |
| `pct_premium_captured` | REAL | `(credit − close_price) / credit × 100` |
| `atm_iv_at_close` | REAL | ATM IV at close (IV crush = open − close) |
| `rsi_at_close` | REAL | RSI at close time |
| `pct_b_at_close` | REAL | Bollinger %B at close |
| `spot_change_pct` | REAL | `(spot_close − spot_open) / spot_open × 100` (auto-derived) |
| `buffer_at_close` | REAL | `(short_strike − spot_close) / spot_close × 100` — negative = ITM at close |

**`pct_premium_captured` interpretation:**

```
Sold spread for $1.35 credit.
Closed (take profit) at $0.68 → pct_premium_captured = 49.6%
Expired worthless at $0.00    → pct_premium_captured = 100.0%
Stopped out at $4.05          → pct_premium_captured = negative (lost money)
Regime shift at $1.57         → pct_premium_captured = 33.0% (early exit)
```

### 14.3 Data Flow

```
── ENTRY CONTEXT ──────────────────────────────────────────────────────────

  MarketScanner assembles MarketSnapshot (includes regime_v2 dict)
    ↓
  Strategy.evaluate(snapshot) — all gates pass
    ↓
  TradeSignal(snapshot=snapshot, spot_price=..., buffer_pct=..., reward_risk=...,
              short_strike=..., atm_iv_at_open=..., theta=..., signal_score=...,
              entry_type=...)
    ↓ signal carried through SignalRanker → PortfolioGuard → TradeManager
  PaperLedger.record_open(...)
    ↓
  SQLite: all entry context columns written to row

── EXIT CONTEXT ────────────────────────────────────────────────────────────

  PositionMonitor._monitor_job() every 30 min
    │
    ├─ [Priority 0] check bridge_instance.get_regime(sym)["exit_mandate"]
    │   → if True: close_all_regime_shift(symbol=sym)
    │              close_reason = "regime_shift"
    │
    └─ [Priority 1-4] run_cycle() → ExitEvaluator
        stop_loss / dte_close / take_profit / expired_worthless
        → TradeManager.close_trade(...)
        → PaperLedger.record_close(...)
        → SQLite: all exit context columns written to row
```

### 14.4 Database Schema (key columns)

```sql
CREATE TABLE paper_trades (
    -- Core trade fields
    id              INTEGER PRIMARY KEY,
    symbol          TEXT,
    strategy_name   TEXT,
    net_credit      REAL,
    max_loss        REAL,
    expiry          TEXT,
    dte_at_open     INTEGER,
    iv_rank         REAL,
    delta           REAL,
    regime          TEXT,
    status          TEXT,      -- 'open' | 'closed' | 'expired'
    pnl             REAL,
    close_reason    TEXT,      -- 'expired_worthless' | 'take_profit' | 'stop_loss'
                               -- | 'dte_close' | 'regime_shift' | 'manual'
    opened_at       TEXT,
    closed_at       TEXT,

    -- Entry context
    spot_price_at_open  REAL, buffer_pct REAL, reward_risk REAL,
    rsi_at_open REAL, pct_b_at_open REAL, macd_at_open REAL, vix_at_open REAL,

    -- Analytics entry context (Phase 9h)
    short_strike REAL, long_strike REAL, atm_iv_at_open REAL,
    theta_at_open REAL, vega_at_open REAL, signal_score REAL, entry_type TEXT,

    -- Exit context
    days_held INTEGER, dte_at_close INTEGER,
    spot_price_at_close REAL, iv_rank_at_close REAL, vix_at_close REAL,
    pct_premium_captured REAL,

    -- Analytics exit context (Phase 9h)
    atm_iv_at_close REAL, rsi_at_close REAL, pct_b_at_close REAL,
    spot_change_pct REAL, buffer_at_close REAL,

    -- Live trading
    commission REAL DEFAULT 0, pnl_net REAL
)
```

### 14.5 Backward Compatibility & Migration

`PaperLedger._migrate_db()` runs on every startup. It adds each new column via `ALTER TABLE ADD COLUMN` inside a try/except — SQLite raises `OperationalError` if the column already exists, which is silently ignored. The migration is idempotent and safe to run on any existing database.

### 14.6 PaperLedger Query Method Reference

| Method | Returns | Description |
|---|---|---|
| `get_open_trades()` | `List[Dict]` | All currently open positions |
| `get_closed_trades()` | `List[Dict]` | Closed/expired trades with full context |
| `get_all_trades()` | `List[Dict]` | All trades regardless of status |
| `get_trade(trade_id)` | `Optional[Dict]` | Single trade by ID |
| `get_trades_opened_on(date_str)` | `List[Dict]` | All trades opened on a given date (any status) |
| `get_statistics()` | `Dict` | Aggregate performance metrics and breakdowns |

---

## 15. Web Dashboard

`dashboard.py` is a single-file Flask application that reads `paper_trades.db` directly and presents a live view of the bot's performance. It runs independently of the bot process.

```bash
pip3 install flask          # one-time install

cd /path/to/moomoo
python3 dashboard.py        # → http://127.0.0.1:5000
```

The page auto-refreshes every 60 seconds.

### 15.1 Pages & Features

#### `/` — Overview
Four KPI cards: Total Realised P&L, Win Rate, Open Positions, Avg Premium Captured. Validation Gate progress bars. Performance and By Strategy panels. Open Positions mini-table.

#### `/positions` — Open Positions
Full table of all open positions with every entry context field: strikes, credit, max loss, reward/risk, DTE (colour-coded), IV Rank, delta, buffer %, spot @ open, RSI, %B, VIX, regime, unrealised P&L.

#### `/history` — Trade History
Full table of all closed trades with side-by-side entry and exit context. Exit reason badges:

| Badge | Reason | Interpretation |
|---|---|---|
| 🟢 Green | Expired Worthless | Best — kept 100% of premium |
| 🔵 Blue | Take Profit | 50% decay target hit |
| 🔵 Cyan | DTE Close | Gamma risk avoidance |
| 🔴 Red | Stop Loss | Loss management |
| 🟡 Amber | Regime Shift | HMM exit mandate fired |
| ⚫ Grey | Manual | Human-initiated |

#### `/stats` — Statistics
By Strategy, By Exit Reason (including `regime_shift`), Averages & Risk, Overall Summary.

#### `/analytics` — Analytics (10 sections)
Bootstrap card layout rendering all 10 analytics sections with data coverage progress bars. Sections with no data yet (pending analytics fields from Phase 9h migrations) show a friendly "no data yet" placeholder rather than errors.

| Section | Content | Data requirement |
|---|---|---|
| 1 · Exit Type Breakdown | Win rate and avg P&L per exit reason | Existing data |
| 2 · Symbol Performance | Win rate and avg P&L per symbol | Existing data |
| 3 · IV Crush Contribution | IV at open vs close, crush %, captured % | `atm_iv_at_close` |
| 4 · Theta Realisation Rate | Actual P&L vs theoretical theta decay | `theta_at_open` |
| 5 · Signal Score vs Outcome | Ranking score vs win/loss | `signal_score` |
| 6 · %B Entry Zone vs Outcome | Win rate by Bollinger Band zone | `pct_b_at_open` |
| 7 · Near-Miss Analysis | Buffer remaining at close | `buffer_at_close` |
| 8 · Entry Type Comparison | Morning scan vs intraday entries | `entry_type` |
| 9 · VIX Regime Correlation | Win rate and IV crush by VIX zone | `vix_at_open` |
| 10 · Days Held Distribution | Win rate by holding period bucket | `days_held` |

#### `/healthz` — Health Check
Returns JSON: `{"status": "ok", "open": N, "closed": N, "win_rate": N}`. Useful for uptime monitoring.

### 15.2 Configuration

The dashboard reads gate thresholds from `config.yaml` automatically:

```yaml
validation_gate:
  min_trades:    10
  min_win_rate:  0.60
```

Database path resolved in order: `--db` CLI argument → `paper_ledger.db_path` in config.yaml → default `data/paper_trades.db`.

---

## 16. Analytics CLI

`analytics_report.py` is a standalone script that reads `paper_trades.db` and prints all analytics sections to the console with ANSI colour output. It is independent of the bot — safe to run at any time without affecting the live bot.

```bash
python3 analytics_report.py                        # full report (auto-discovers DB)
python3 analytics_report.py --db data/paper_trades.db
python3 analytics_report.py --section 1            # exit type breakdown only
python3 analytics_report.py --section overview     # overview + data coverage only
python3 analytics_report.py --no-color > report.txt
```

**Available `--section` values:** `overview`, `1`–`10`, `coverage`

### Section descriptions

**Overview** — Closed trade count, win rate, total P&L, avg P&L, avg captured %, best and worst trade. The at-a-glance health check.

**Coverage** — Progress bars showing which analytics columns are populated vs still NULL in the live DB. Shows a migration hint if Phase 9h columns are missing. Resolves automatically as new trades close.

**1 · Exit Type Breakdown** — Win rate, avg P&L, and avg premium captured grouped by `close_reason`. The first insight visible: take_profit exits typically show 5× higher avg P&L than `dte_close` exits, confirming the 50% threshold is well-calibrated.

**2 · Symbol Performance** — Win rate and total P&L per symbol. Flags symbols with avg buffer < 3% in amber — may indicate strike selection is too tight for that symbol's volatility range.

**3 · IV Crush Contribution** — For each closed trade: IV at open, IV at close, absolute crush, crush as a %, premium captured. Validates whether entering on elevated IV is generating alpha from compression (>5% crush = timing adds value beyond pure theta).

**4 · Theta Realisation Rate** — Compares actual P&L vs `theta_daily × days_held` (theoretical decay). >100% = IV crush added on top. <60% = IV expanded or position was exited early before theta accrued.

**5 · Signal Score vs Outcome** — Maps each trade's composite ranking score to its win/loss outcome. If high-scored trades consistently win, the weighting formula (IV 40% / buffer 35% / R/R 25%) is validated.

**6 · %B Entry Zone vs Outcome** — Buckets entry %B into four zones: ≥0.80 (overbought), 0.60–0.80, 0.40–0.60, <0.40. Shows win rate and avg P&L per zone. If ≥0.80 dominates, consider raising `min_pct_b` threshold.

**7 · Near-Miss Analysis** — All closed trades sorted by `buffer_at_close` ascending (closest calls first). Negative buffer = spot was above the short strike at close. Highlights structural near-misses for position sizing review.

**8 · Entry Type Comparison** — Morning scan vs intraday entries. If intraday consistently underperforms, tighten intraday-specific thresholds.

**9 · VIX Regime Correlation** — Win rate, avg P&L, avg captured %, and avg IV crush bucketed by VIX level at entry (< 16 / 16–20 / 20–25 / 25–30 / 30+). Answers the key question: does entering at VIX 20–25 produce more IV crush than VIX < 20?

**10 · Days Held Distribution** — Win rate and avg P&L bucketed by holding period (same-day / 1–3d / 4–7d / 8–14d / 15d+). Highlights whether very short holds (< 3d) with losses indicate the stop loss is firing too early.

### Robustness on pre-migration databases

The script handles databases that haven't run the Phase 9h migration yet (missing analytics columns). It checks `PRAGMA table_info(paper_trades)` before each query, shows `column not in DB yet` with a migration hint for missing fields, and continues rendering sections that use only pre-existing columns. Sections 1, 2, 6, 9, and 10 work immediately on any database version.
