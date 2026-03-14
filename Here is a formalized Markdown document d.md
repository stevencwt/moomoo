Here is a formalized Markdown document designed for high-density information transfer between AI systems. It structures the "Regime Voting System" logic we discussed into a technical specification.

---

## Technical Specification: Multi-Modal Market Regime Analysis Framework

### 1. Executive Summary

This document defines a modular framework for market regime classification. The core thesis is that execution logic (Scalping, Swing, Pairs, or Options) must be secondary to a consensus-based assessment of the **Latent Market State**. The framework utilizes a "Voting System" to reduce information paralysis while maximizing statistical confidence.

### 2. Architecture: The Regime Voting System

The system is composed of four "Specialist" layers. A **Regime Manager** class aggregates these signals into a final `RegimeState` object.

| Layer | Component | Technical Method | Primary Objective |
| --- | --- | --- | --- |
| **Core** | HMM | Gaussian Hidden Markov Model | Categorize latent state (Bull, Bear, Chop). |
| **Momentum** | Hurst | Rescaled Range (R/S) Analysis | Determine "Memory" (Mean-reverting vs. Trending). |
| **Structural** | CPD | Ruptures (PELT / BinSeg) | Identify "Regime Breaks" to act as a kill-switch. |
| **Predictive** | Greeks | Option Chain Skew / Vanna | Capture forward-looking institutional sentiment. |

---

### 3. Category-Specific Implementation

#### A. Pure Crypto (Directional)

* **HMM (3-State):** Trains on $log(returns)$ and $log(volatility)$.
* **Hurst Filter:** If $H < 0.45$, inhibit Swing logic; enable Mean-Reversion Scalping.
* **CPD Kill-Switch:** If a Change Point is detected within $n$ periods, reduce position size by 50% regardless of HMM state.

#### B. Pairs Trading (Spread Analysis)

* **Kalman Filter:** Dynamically tracks the Hedge Ratio ($\beta$).
* **Stationarity Check:** Monitor the Residuals of the spread. If residuals exhibit a "Random Walk" (Hurst $\to$ 0.5), the regime has shifted from "Co-integrated" to "Decoupled."

#### C. Options Strategies

* **Income (Selling):** Utilize **MRS-GARCH** to detect Volatility Persistence. Only execute spreads if the model confirms the "Low Volatility Persistence" state.
* **Speculative (Buying):** Use **Vanna/Gamma Skew** analysis.
* *Positive Vanna Regime:* Rising Spot + Rising IV creates a feedback loop for Long Calls.
* *Gamma Walls:* Use strike concentration to define regime boundaries where price behavior transitions from "Mean Reverting" (Inside Wall) to "Trend" (Breakout).



---

### 4. Data Inputs & Feature Engineering

1. **Price Series:** OHLCV (Log-transformed).
2. **Volatility:** 5-period Standard Deviation of returns.
3. **Options Chain:** * `Delta_Skew = (Put_Delta_OTM - Call_Delta_OTM)`
* `Vanna = d(Delta) / d(IV)`
* `Gamma_Concentration = Total_OI_at_Strike`



---

### 5. Output Schema (JSON)

```json
{
  "consensus_state": "BULL_TRENDING",
  "confidence_score": 0.82,
  "regime_persistence_prob": 0.94,
  "structural_break_alert": false,
  "recommended_logic": "TREND_FOLLOWING",
  "volatility_regime": "LOW_STABLE"
}

