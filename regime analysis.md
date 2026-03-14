The **v3.1 Multi-Modal Market Regime Analysis Framework** is now updated to explicitly distinguish **Range Trading** from **Scalping** (both mean-reversion styles in choppy regimes) so downstream strategy modules can activate the appropriate playbook:

- **Scalping** → ultra-short duration, high frequency, small targets (seconds to ~10–15 min holds, many trades).
- **Range Trading** → longer holds within the same regime (minutes to several hours, fewer but larger targets per oscillation).

This separation enables:
- Better entry/exit precision (range trading waits for clearer extremes).
- Different risk sizing / frequency rules.
- Regime-change exits: any shift out of CHOP_NEUTRAL (e.g., Hurst > 0.60 or structural break) mandates exit of open range/scalp positions.

The module remains the **foundational reusable layer** — a single `RegimeManager` class that other bot components (entry logic, position manager, exit rules) can query periodically for the current regime and recommended_logic. Regime changes trigger mandatory exits across all styles.

### Key Changes in v3.1
- New **RANGE_TRADING** recommended_logic with explicit activation conditions.
- Tighter regime criteria for clean ranges (Hurst 0.48–0.58 sweet spot, LOW_STABLE vol).
- Added range-boundary hints in JSON (lower/upper levels, deviation) for entry/exit guidance.
- Explicit exit rule: regime shift → force exit all positions (configurable delay/grace period).
- Minor temporal matrix adjustments for range trading.

---

# Technical Specification: Multi-Modal Market Regime Analysis Framework (v3.1 – Master Final with Range vs Scalp Distinction)

## 1. Executive Summary

This framework serves as the **robust, reusable foundational regime detection layer** for a full trading bot. It classifies the current market state and recommends execution logic, enabling:
- Entry filtering (only allow trades in matching regimes).
- Mandatory exits on regime change (e.g., exit all range/scalp positions when Hurst rises or CPD triggers).
- Downstream modules query the RegimeManager for real-time consensus.

Supported categories (now with clear Range vs Scalp split):
- Swing (Crypto/US Stocks)
- **Scalping** (Crypto/US Stocks – ultra-short mean-reversion)
- **Range Trading** (Crypto/US Stocks – longer-hold mean-reversion in clean ranges)
- Options Income/Selling (US Stocks)
- Options Speculative/Buying (US Stocks)
- Pairs Trading (Crypto Perps on Hyperliquid)

## 2. The Hybrid Consensus Engine

(Unchanged core layers; added emphasis on range detection via Hurst + vol stability + boundary persistence.)

## 3. Temporal & Lookback Matrix (Updated)

| Strategy Type              | Market       | Regime Signal TF | Execution TF     | Lookback Window          | Stability Filter (HMM) |
|----------------------------|--------------|------------------|------------------|--------------------------|------------------------|
| Scalping                   | Crypto       | 5m               | 1m               | 750–1,000 bars           | 2 bars                |
| Scalping                   | US Stocks    | 5m / 15m         | 1m / 5m          | 500–800 bars             | 2–3 bars              |
| **Range Trading**          | Crypto       | 15m / 1h         | 5m / 15m         | 1,000–2,000 bars         | 3–4 bars              |
| **Range Trading**          | US Stocks    | 1h / 4h          | 15m / 30m        | 800–1,500 bars           | 4 bars                |
| Swing                      | Crypto       | 1h               | 15m              | 1,000–1,500 bars         | 3 bars                |
| Swing                      | US Stocks    | 1h / 4h          | 15m / 30m        | 800–1,200 bars           | 3–4 bars              |
| Options Income             | US Stocks    | Daily            | Daily / 30m      | 252–500 bars             | 5 bars                |
| Options Speculative        | US Stocks    | Daily / 4h       | 30m / 1h         | 252–400 bars             | 4–5 bars              |
| Pairs Trading              | Crypto       | 15m / 1h         | 5m / 15m         | 1,000–2,000 bars (spread)| 3–4 bars              |

## 4. Regime Definitions & Thresholds (Enhanced for Range)

- **CHOP_NEUTRAL** now splits into sub-flavors:
  - Noisy chop: Hurst < 0.48 or high vol noise → favors Scalping
  - Clean range: Hurst 0.48–0.58 + LOW_STABLE vol + price persisting inside bounds → favors Range Trading
- **Volatility Regime**: LOW_STABLE strongly preferred for Range Trading (reduces false extremes).

## 5. Strategy-Specific Regime Activation Logic (Updated)

1. **Scalping** (Ultra-short mean-reversion)
   - **Recommended Logic**: SCALP_MEAN_REVERSION
   - **Activation**: HMM == CHOP + Hurst < 0.48 + Liquidity CONSOLIDATION + No CPD break
   - **Style**: Many small trades; enter on micro-reversions (e.g., quick bounce off EMA, order-flow signal); target 0.1–0.5% / few ticks.
   - **Exit**: Regime change OR small trailing stop OR time limit (~10–15 min max hold).

2. **Range Trading** (Buy bottom / Sell top in defined range)
   - **Recommended Logic**: RANGE_TRADING
   - **Activation**:
     - HMM == CHOP
     - Hurst ∈ [0.48, 0.58] (random walk with moderate mean-reversion)
     - Volatility LOW_STABLE or CONTRACTING
     - Liquidity CONSOLIDATION or PASSED
     - No recent CPD break
     - Range persistence: price inside established channel (e.g., 20–50 period Donchian/Keltner) for ≥8–12 bars
   - **Style**: Fewer trades; wait for extremes (lower/upper channel, RSI <30/>70, deviation >1.5–2 SD from range mean/VWAP); target 50–80% of range width.
   - **Exit**: Regime change (mandatory) OR price reaches opposite extreme OR range breakout (Hurst shift or CPD).

3–6. Other strategies (Swing, Options Income, Options Speculative, Pairs Trading) — unchanged from v3.0.

**Global Mandatory Exit Rule** (core to robustness):
- Any regime shift away from current regime (e.g., CHOP → BULL_PERSISTENT, Hurst > 0.60, CPD break = true, volatility regime → EXPANDING) → force exit all open positions (range, scalp, swing, etc.).
- Configurable grace period (e.g., 1–3 bars confirmation) to avoid whipsaw exits.
- Downstream bot must poll RegimeManager frequently (e.g., every 1–5 min) for regime updates.

## 6. Standardized Output Schema (JSON – Enhanced)

```json
{
  "consensus_state": "CHOP_NEUTRAL",
  "market_type": "CRYPTO_PERP",
  "confidence_score": 0.85,
  "volatility_regime": "LOW_STABLE",
  "signals": {
    "hmm_label": "CHOP",
    "hurst_dfa": 0.52,
    "structural_break": false,
    "liquidity_status": "CONSOLIDATION",
    "crypto_context": { ... },
    "options_context": null,
    "range_hints": {
      "is_clean_range": true,
      "range_lower": 145.20,
      "range_upper": 152.80,
      "current_deviation_pct": -1.4,
      "channel_type": "Donchian_30"
    }
  },
  "recommended_logic": "RANGE_TRADING",   // now distinct from SCALP_MEAN_REVERSION
  "exit_mandate": false,                   // true if regime change detected → force exit
  "timestamp": "2026-03-13T14:06:00Z"
}
```

## 7. Implementation Notes for RegimeManager (Reusable Module)

- Public API remains simple:
  ```python
  manager = RegimeManager(config_path="config.yaml")
  manager.update(new_bar_data, funding=None, options_greeks=None)
  regime = manager.get_current_regime()          # dict
  json_output = manager.get_json()               # standardized schema
  if regime["exit_mandate"]:
      # downstream: close all positions
  ```
- Range hints computed optionally (configurable) using rolling Donchian/Keltner or VWAP.
- Config.yaml now includes:
  - range_min_hurst: 0.48
  - range_max_hurst: 0.58
  - range_min_bars_persistence: 10
  - exit_grace_bars: 2
  - scalping_max_hold_min: 15
  - etc.

This v3.1 version makes the regime detection layer truly foundational: robust identification of chop → clear distinction between fast scalping vs patient range trading → automatic regime-shift exits for risk control. Downstream modules (entry generators, position managers) can now branch cleanly on `recommended_logic` while always respecting `exit_mandate`.

