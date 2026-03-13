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

**Strategies covered:**
- 2.1 Covered Call
- 2.2 Bear Call Spread
- 2.3 Bull Put Spread *(two-sided premium collection)*

**Key subsections:**
- 4.1 Signal Ranking & Selection *(opportunity scoring — replaces FIFO execution)*

---

## 1. Overview

This is a fully automated **options premium selling bot** built in Python. It targets high-probability, defined-risk trades by selling time value (theta) in US equity options markets.

**Core philosophy:** Sell options when implied volatility is elevated (premium is expensive), use technical filters to avoid unfavourable conditions, and close positions systematically before expiry to avoid gamma risk.

**What the bot does every trading day:**

| Time (ET) | Action |
|---|---|
| 09:35 | **Pass 1:** Scan all watchlist symbols → collect every qualifying signal |
| 09:35 | **Pass 2:** Score and rank candidates → select top N → place orders |
| Every 30 min | Check open positions for stop-loss / take-profit / DTE exits |
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
| Market regime ≠ high_vol | Required | VIX spike = unpredictable premiums |
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
Market condition         Covered call   Bear call spread   Bull put spread
────────────────────     ────────────   ────────────────   ───────────────
Bull + high IV           ✅ fires        ❌ blocked          ✅ fires
Neutral + high IV        ✅ fires        ✅ fires             ✅ fires
Bear + high IV           ✅ fires        ✅ fires             ❌ blocked
High volatility (VIX≥25) ❌ all blocked  ❌ all blocked       ❌ all blocked
```

In a neutral regime — the most common condition — all three strategies can fire simultaneously. The portfolio guard's `max_open_positions` and `max_trades_per_day` limits govern total exposure. The iron condor prevention ensures that two opposing sides never accumulate on the same underlying, keeping each position independently manageable.

---

## 3. Market Regime Detection

Before any trade is evaluated, the bot classifies the current market environment. Regime detection uses three indicators computed from 6 months of daily price data plus the current VIX.

| Regime | Condition | Effect on strategies |
|---|---|---|
| `high_vol` | VIX ≥ 25 | **All new positions blocked.** Undefined risk in volatile markets. |
| `bull` | RSI ≥ 55 AND MACD > 0 | Covered calls and bull put spreads allowed. Bear call spreads blocked (breakout risk). |
| `neutral` | Neither bull nor bear | **All strategies allowed.** Ideal condition for two-sided premium collection. |
| `bear` | RSI ≤ 45 AND MACD < 0 | Bear call spreads and covered calls allowed. Bull put spreads blocked (crash risk to short puts). |

**VIX threshold is the master safety switch.** When VIX spikes above 25, no new positions are opened regardless of all other conditions. Existing positions continue to be monitored and exited normally.

Thresholds are configurable in `config.yaml` under the `regime:` section.

---

## 4. Entry Criteria & Signal Filters

Every morning at 09:35 ET the scanner runs the full data pipeline in two passes:

**Pass 1 — Collect all signals (no execution yet):**

```
For each symbol:
  1. Download 6 months daily OHLCV (Yahoo Finance)
  2. Compute RSI, MACD, Bollinger %B (TechnicalAnalyser)
  3. Fetch current VIX (Yahoo Finance)
  4. Classify market regime (RegimeDetector)
  5. Fetch option expiries in 21-45 DTE range (MooMoo / IBKR)
  6. Check IV Rank (stored daily IV history, min 30 days required)
  7. Check upcoming earnings dates (Yahoo Finance)
  8. Read shares held (broker account or config override)
  9. Count open positions for this symbol (paper ledger)
 10. Assemble MarketSnapshot → pass to each strategy for evaluation
 11. Collect every qualifying signal — do NOT execute yet
```

**Pass 2 — Rank, select, and execute:**

```
 12. Score all collected signals (SignalRanker)
 13. Sort candidates by composite score descending
 14. Walk ranked list top-to-bottom:
       - Apply portfolio guard checks (risk limits, iron condor prevention, daily cap)
       - Execute approved signals until daily limit reached or pool exhausted
 15. Log ranked table with selection rationale
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
```

Weights do not need to sum to 1.0 — each component is independently normalised, so a weight of 0.40 means "IV Rank is weighted at 40% of the maximum possible score contribution from that dimension." The defaults are designed so that IV Rank dominates (it is the foundational thesis of the strategy), buffer distance provides the structural safety component, and R/R breaks ties between similarly-ranked candidates.

### Buffer distance calculation

Buffer is computed differently depending on the strategy:

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

**Tuning guidance:**

- Increase `weight_iv_rank` if you want the bot to be more selective about IV environment — it will strongly prefer signals with genuinely elevated premium and penalise borderline-IV signals more aggressively.
- Increase `weight_buffer_pct` if you want the bot to prioritise structural safety — it will favour wider strike buffers even at the cost of slightly lower premium.
- Increase `weight_reward_risk` if you want the bot to focus on trade efficiency — it will favour spreads that collect more premium per dollar risked.

The three weights do not need to sum to 1.0. Doubling all three weights produces identical rankings — only their *relative* magnitudes matter.

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

The bot checks every open position every 30 minutes during market hours. Four exit triggers are evaluated in priority order:

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

```
Config:
  stop_loss_multiplier: 3.0
  min_days_before_stop: 5
```

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
  high_vol_vix_threshold:  25.0
  bull_rsi_threshold:      55.0
  bear_rsi_threshold:      45.0
  macd_threshold:          0.0
```

Controls how market conditions are classified. Raise `high_vol_vix_threshold` to trade through higher volatility environments. Widen the gap between `bull_rsi_threshold` and `bear_rsi_threshold` to make the regime more conservative (more time classified as "neutral").

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

The scanner, strategies, and order router all work off the watchlist dynamically. No code needs to change.

### What symbols are suitable

The strategies work best on liquid US equity options. Good candidates have:
- High average daily volume (>1M shares/day)
- Active options market (open interest >1,000 per strike)
- Implied volatility that moves meaningfully (IV Rank frequently above 30)
- Regular options liquidity in the 21–45 DTE range

Examples: TSLA, AAPL, NVDA, SPY, QQQ, AMZN, MSFT

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

### Make covered calls more conservative (tighter filters)

```yaml
  covered_call:
    min_iv_rank:              40    # only trade when IV is high
    max_rsi:                  60    # more conservative RSI cap
    max_concurrent_positions: 1     # only 1 covered call at a time
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

**To subscribe to IBKR market data:**
1. Log in to interactivebrokers.com → Settings → Market Data Subscriptions
2. Subscribe to: **US Securities Snapshot and Futures Value Bundle (NP,L1)** — $10/month base
3. Then subscribe to: **US Equity and Options Add-On Streaming Bundle (NP)** — $4.50/month
4. Both fees are waived when monthly commissions exceed their respective thresholds

---

### Full MooMoo mode (paper trading only)

```yaml
broker:
  data:      "moomoo"
  execution: "moomoo"
```

**Use case:** Running paper trading validation before the IBKR account is funded. Note: the Singapore MooMoo API cannot access real trading accounts — this mode is paper-only.

---

### Additional IBKR config when switching to full IBKR

When using IBKR for data, the `shares_held` override should be removed from `config.yaml` — the bot will read your actual position directly from the IBKR account:

```yaml
universe:
  watchlist:
    - "US.TSLA"
  # shares_held section can be removed — IBKR account queried directly
```

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
# Ensure MooMoo OpenD is running (hybrid mode) or IBKR TWS is running (full IBKR)

cd /path/to/moomoo
python3 main.py
```

### Checking current status

```bash
python3 main.py --status
```

### Viewing pending signals (for manual execution workflow)

```bash
python3 main.py --pending
```

### Recording a trade placed manually

```bash
python3 main.py --record-trade
```

### Closing a trade manually

```bash
python3 main.py --close-trade
```

### Logs

All activity is logged to `logs/bot.log`. Each job is tagged for easy filtering:

```
[SCAN JOB]    — morning scan results, signals generated
[MONITOR JOB] — position check results, exits triggered
[IV JOB]      — end-of-day IV collection
[REPORT JOB]  — weekly validation report
[LIVE ORDER]  — real money orders (always logged with this tag)
[PAPER]       — simulated fills
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
│   │   ├── market_snapshot.py       ← immutable snapshot data class
│   │   ├── technical_analyser.py    ← RSI, MACD, Bollinger Bands
│   │   ├── options_analyser.py      ← delta filter, spread metrics
│   │   ├── iv_rank_calculator.py    ← stores daily IV, computes IV Rank
│   │   └── regime_detector.py       ← bull / bear / neutral / high_vol
│   │
│   ├── strategies/
│   │   ├── base_strategy.py         ← abstract base class
│   │   ├── strategy_registry.py     ← manages list of active strategies
│   │   ├── trade_signal.py          ← signal data class
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
│   │   └── paper_ledger.py          ← SQLite trade journal
│   │
│   ├── monitoring/
│   │   ├── exit_evaluator.py        ← stop loss, take profit, DTE rules
│   │   ├── position_monitor.py      ← runs exit checks every 30 min
│   │   └── validation_reporter.py   ← weekly go-live gate report
│   │
│   ├── notifier/
│   │   ├── signal_notifier.py       ← writes signals to file for manual review
│   │   └── trade_recorder.py        ← records manually placed trades
│   │
│   └── scheduler/
│       └── bot_scheduler.py         ← event loop, wires all components
│
├── tests/                           ← 407 unit tests across 8 phases + signal_ranker suite (pending)
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
| Widen the stop loss | `position_monitor.exit_rules.stop_loss_multiplier` — default is 3.0 (3× credit) |
| Change minimum hold before stop | `position_monitor.exit_rules.min_days_before_stop` — default is 5 days |
| Close positions earlier | `position_monitor.exit_rules.dte_close_threshold` — raise from 21 |
| Take profit sooner | `position_monitor.exit_rules.take_profit_pct` — raise from 0.50 |
| Switch to full IBKR | `broker.data: "ibkr"` and `broker.execution: "ibkr"` |
| Go live | `mode: live` (only after validation report passes) |
| Pause all new trades | `regime.high_vol_vix_threshold: 0` (always high_vol) |