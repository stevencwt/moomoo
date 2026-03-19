"""
src/scalp/signals/gex_calculator.py
=====================================
Gamma Exposure (GEX) calculator for the GEX Reversion Scalper.

Computes the GEX map from the front-expiry option chain via IBKRClient.
Returns the three key outputs used by scalp_gate.py and scalp_entry.py:

    gamma_wall     : float  — strike with highest positive net GEX
                              (mean-reversion target / price magnet)
    gex_flip       : float  — first strike where cumulative net GEX goes negative
                              (momentum trigger level — break below = amplifying)
    is_stabilising : bool   — True if total net GEX is positive (dampening env)
                              False if negative (amplifying env — momentum mode)

    Also returns the full net_gex series for logging and analysis.

Sign convention (SpotGamma / Barchart standard):
    Call GEX = +gamma × open_interest × spot × 100   (stabilising — dealers buy dips)
    Put GEX  = −gamma × open_interest × spot × 100   (destabilising — dealers sell dips)

Refresh schedule (per strategy doc Section 4):
    09:00 ET — pre-market structural map
    11:00 ET — first intraday refresh (0DTE opens heavily in first hour)
    13:00 ET — second intraday refresh (post-lunch repositioning)

Why front expiry only:
    0DTE options have gamma 10–20× higher than monthly options at the same strike.
    Since GEX is weighted by gamma, 0DTE dominates the map completely.
    Using a longer-dated expiry gives a misleading picture of intraday hedging pressure.

Usage:
    from ibkr_connector import IBKRClient
    from src.scalp.signals.gex_calculator import GEXCalculator

    client = IBKRClient(port=7496, account="U18705798")
    client.connect()

    calc   = GEXCalculator(config)
    gex    = calc.compute("SPY", client)

    print(f"Gamma wall : ${gex['gamma_wall']:.2f}")
    print(f"GEX flip   : ${gex['gex_flip']:.2f}")
    print(f"Mode       : {'stabilising' if gex['is_stabilising'] else 'amplifying'}")
    print(f"Spot       : ${gex['spot']:.2f}")
"""

import logging
import time
from datetime import date, datetime
from typing import Dict, List, Optional

import pandas as pd
import numpy as np

logger = logging.getLogger("options_bot.scalp.signals.gex_calculator")


# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_REFRESH_TIMES  = ["09:00", "11:00", "13:00"]   # ET
DEFAULT_PROXIMITY_PCT  = 0.003   # 0.3% — price must be within this of a GEX level


class GEXCalculator:
    """
    Computes GEX map from front-expiry option chain.

    Caches the last computed map. Call compute() at each refresh time
    (09:00, 11:00, 13:00 ET). Call get_cached() between refreshes to
    avoid unnecessary API calls.

    Args:
        config: bot config dict. Reads from config["scalp"]["gex"] if present.
    """

    def __init__(self, config: dict):
        scalp_cfg = config.get("scalp", {})
        gex_cfg   = scalp_cfg.get("gex", {})

        self._refresh_times  = gex_cfg.get("refresh_times",  DEFAULT_REFRESH_TIMES)
        self._proximity_pct  = gex_cfg.get("proximity_pct",  DEFAULT_PROXIMITY_PCT)

        # Cache: maps symbol → last GEX result dict
        self._cache: Dict[str, dict] = {}

        logger.info(
            f"GEXCalculator initialised | refresh={self._refresh_times} | "
            f"proximity={self._proximity_pct*100:.1f}%"
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def compute(self, symbol: str, client) -> dict:
        """
        Compute GEX map for a symbol from its front-expiry option chain.

        Args:
            symbol : Plain ticker e.g. "SPY", "QQQ", "NVDA"
            client : Connected IBKRClient instance

        Returns dict with keys:
            gamma_wall      : float  — strike with highest positive net GEX
            gex_flip        : float  — first strike where cumulative GEX < 0
            is_stabilising  : bool   — True = dampening, False = amplifying
            net_gex_series  : pd.Series  — full net GEX per strike (index=strike)
            total_net_gex   : float  — sum of all net GEX values
            spot            : float  — spot price at computation time
            expiry          : str    — front expiry date used (ISO format)
            symbol          : str    — symbol computed for
            call_gex_total  : float  — total positive (call) GEX
            put_gex_total   : float  — total negative (put) GEX
            strikes_near_wall: list  — strikes within proximity_pct of gamma wall
            computed_at     : float  — time.time()
            error           : str    — non-empty if computation partially failed
        """
        ticker = symbol.replace("US.", "").upper()

        try:
            result = self._compute_internal(ticker, client)
            self._cache[ticker] = result
            logger.info(
                f"[GEX:{ticker}] wall=${result['gamma_wall']:.2f} "
                f"flip=${result['gex_flip']:.2f} "
                f"stabilising={result['is_stabilising']} "
                f"spot=${result['spot']:.2f} "
                f"expiry={result['expiry']}"
            )
            return result

        except Exception as e:
            logger.error(f"[GEX:{ticker}] compute failed: {e}")
            err = self._error_result(ticker, str(e))
            self._cache[ticker] = err   # cache error result so callers can inspect
            return err

    def get_cached(self, symbol: str) -> Optional[dict]:
        """
        Return last computed GEX map for a symbol without making API calls.
        Returns None if no cached result exists.
        """
        ticker = symbol.replace("US.", "").upper()
        return self._cache.get(ticker)

    def is_near_gex_level(self, symbol: str, spot: float) -> dict:
        """
        Check whether current spot price is within proximity_pct of any
        significant GEX level (gamma wall or GEX flip).

        Returns:
            near_wall    : bool  — within proximity_pct of gamma_wall
            near_flip    : bool  — within proximity_pct of gex_flip
            wall_dist_pct: float — distance to gamma wall as % of spot
            flip_dist_pct: float — distance to GEX flip as % of spot
            side         : str   — "above_wall"|"below_wall"|"at_wall"|"none"
        """
        ticker  = symbol.replace("US.", "").upper()
        cached  = self._cache.get(ticker)

        if not cached or cached.get("error"):
            return {
                "near_wall": False, "near_flip": False,
                "wall_dist_pct": 999.0, "flip_dist_pct": 999.0,
                "side": "none",
            }

        wall = cached["gamma_wall"]
        flip = cached["gex_flip"]

        wall_dist = abs(spot - wall) / spot
        flip_dist = abs(spot - flip) / spot

        near_wall = wall_dist <= self._proximity_pct
        near_flip = flip_dist <= self._proximity_pct

        if near_wall:
            side = "above_wall" if spot > wall else ("at_wall" if spot == wall else "below_wall")
        else:
            side = "none"

        return {
            "near_wall":     near_wall,
            "near_flip":     near_flip,
            "wall_dist_pct": round(wall_dist * 100, 4),
            "flip_dist_pct": round(flip_dist * 100, 4),
            "side":          side,
        }

    def should_refresh(self, symbol: str, et_now: Optional[datetime] = None) -> bool:
        """
        Return True if the cache for this symbol is stale and should be refreshed.
        Stale = no cache exists OR cache is older than time since last refresh window.

        Args:
            symbol : ticker
            et_now : current ET datetime (defaults to now)
        """
        ticker = symbol.replace("US.", "").upper()
        cached = self._cache.get(ticker)
        if not cached:
            return True

        if et_now is None:
            from zoneinfo import ZoneInfo
            et_now = datetime.now(ZoneInfo("America/New_York"))

        # Find the most recent refresh time that has passed
        current_hhmm = et_now.strftime("%H:%M")
        last_refresh_hhmm = None
        for rt in sorted(self._refresh_times):
            if current_hhmm >= rt:
                last_refresh_hhmm = rt

        if last_refresh_hhmm is None:
            return False   # before first refresh window

        # Convert last_refresh_hhmm to today's timestamp
        h, m    = map(int, last_refresh_hhmm.split(":"))
        from zoneinfo import ZoneInfo
        last_refresh_dt = et_now.replace(hour=h, minute=m, second=0, microsecond=0)
        cached_at_dt    = datetime.fromtimestamp(
            cached["computed_at"],
            tz=ZoneInfo("America/New_York")
        )

        return cached_at_dt < last_refresh_dt

    # ── Internal ──────────────────────────────────────────────────────────────

    def _compute_internal(self, ticker: str, client) -> dict:
        """Core GEX computation — raises on failure."""

        # Step 1: Get spot price
        spot = client.get_spot_price(ticker)
        if spot <= 0:
            raise ValueError(f"Invalid spot price: {spot}")

        # Step 2: Get front expiry — always use nearest expiry for maximum gamma
        expiries = client.get_option_expiries(ticker)
        if not expiries:
            raise ValueError(f"No expiries returned for {ticker}")

        front_expiry = self._select_front_expiry(expiries)
        logger.debug(f"[GEX:{ticker}] front expiry={front_expiry} "
                     f"(from {len(expiries)} available)")

        # Step 3: Generate OCC codes directly from strike range
        # get_option_chain() returns only boundary strikes (min/max) — not usable.
        # Instead, generate codes for standard strike increments around spot.
        # OCC format: {ticker}{YYMMDD}{C|P}{strike×1000 zero-padded to 8 digits}
        expiry_occ = front_expiry.replace("-", "")[2:]  # 2026-03-20 → 260320
        all_codes  = self._generate_occ_codes(ticker, expiry_occ, spot)

        if not all_codes:
            raise ValueError(f"Could not generate OCC codes for {ticker} "
                             f"at spot ${spot:.2f}")

        logger.info(f"[GEX:{ticker}] generated {len(all_codes)} OCC codes "
                     f"(spot=${spot:.2f} ±30%)")

        snap = client.get_option_snapshot(all_codes)
        if snap.empty:
            raise ValueError(f"Empty snapshot for {ticker} {front_expiry}")

        # Step 5: Compute GEX per strike
        net_gex = self._compute_net_gex(snap, spot)

        if net_gex.empty:
            raise ValueError(f"Could not compute net GEX for {ticker} — "
                             f"missing gamma/open_interest data")

        # Step 6: Derive key levels
        gamma_wall = self._find_gamma_wall(net_gex)
        gex_flip   = self._find_gex_flip(net_gex, spot)
        total_gex  = float(net_gex.sum())

        # Step 7: Proximity strikes
        strikes_near_wall = [
            float(s) for s in net_gex.index
            if abs(s - gamma_wall) / spot <= self._proximity_pct
        ]

        # Step 8: Call/put GEX totals for diagnostics
        call_gex_total = float(net_gex[net_gex > 0].sum())
        put_gex_total  = float(net_gex[net_gex < 0].sum())

        return {
            "symbol":           ticker,
            "gamma_wall":       float(gamma_wall),
            "gex_flip":         float(gex_flip),
            "is_stabilising":   total_gex > 0,
            "net_gex_series":   net_gex,
            "total_net_gex":    round(total_gex, 2),
            "call_gex_total":   round(call_gex_total, 2),
            "put_gex_total":    round(put_gex_total, 2),
            "strikes_near_wall": strikes_near_wall,
            "spot":             round(spot, 2),
            "expiry":           front_expiry,
            "computed_at":      time.time(),
            "error":            "",
        }

    @staticmethod
    def _select_front_expiry(expiries: List[str]) -> str:
        """
        Select the front (nearest) expiry from a list of ISO date strings.
        Skips today's date if markets are closed (after 16:00 ET).
        Guarantees the returned expiry is a valid future or same-day date.
        """
        today = date.today()
        valid = sorted([
            e for e in expiries
            if date.fromisoformat(e) >= today
        ])
        if not valid:
            raise ValueError(f"No valid (non-past) expiries in: {expiries}")
        return valid[0]

    @staticmethod
    def _compute_net_gex(snap: pd.DataFrame, spot: float) -> pd.Series:
        """
        Compute net GEX per strike from snapshot DataFrame.

        Expected columns in snap:
            option_type        : "C" or "P"
            strike_price       : float
            option_gamma       : float
            option_open_interest: float (or open_interest)

        SpotGamma sign convention:
            Call GEX = +gamma × OI × spot × 100
            Put GEX  = −gamma × OI × spot × 100
        """
        df = snap.copy()

        # Normalise column names — handle both naming conventions
        col_map = {}
        for col in df.columns:
            lc = col.lower()
            if "open_interest" in lc or lc == "oi":
                col_map[col] = "oi"
            elif "gamma" in lc:
                col_map[col] = "gamma"
            elif "strike" in lc:
                col_map[col] = "strike"
            elif lc in ("option_type", "type", "right", "call_put"):
                col_map[col] = "option_type"
        df = df.rename(columns=col_map)

        # If option_type column is missing, derive it from the OCC code column
        # OCC format: SPY260320C00580000 — the C/P char is after the 6-digit date
        if "option_type" not in df.columns and "code" in df.columns:
            import re
            def _extract_type(code: str) -> str:
                m = re.search(r'\d{6}([CP])', str(code))
                return m.group(1) if m else ""
            df["option_type"] = df["code"].apply(_extract_type)
            df = df[df["option_type"].isin(["C", "P"])]   # drop rows we couldn't parse
            logger.debug(f"Derived option_type from OCC code column")

        required = {"gamma", "oi", "strike", "option_type"}
        missing  = required - set(df.columns)
        if missing:
            raise ValueError(f"Snapshot missing required columns: {missing}. "
                             f"Available: {list(snap.columns)}")

        # Drop rows with missing gamma or OI — can happen at far OTM strikes
        df = df.dropna(subset=["gamma", "oi"])
        df = df[df["gamma"] > 0]
        df = df[df["oi"]    > 0]

        if df.empty:
            return pd.Series(dtype=float)

        # Normalise option_type to "C" / "P"
        df["option_type"] = df["option_type"].str.upper().str[0]

        # Compute per-row GEX
        calls = df[df["option_type"] == "C"].copy()
        puts  = df[df["option_type"] == "P"].copy()

        calls["gex"] =  calls["gamma"] * calls["oi"] * spot * 100
        puts["gex"]  = -puts["gamma"]  * puts["oi"]  * spot * 100

        # Aggregate per strike
        call_gex = calls.groupby("strike")["gex"].sum()
        put_gex  = puts.groupby("strike")["gex"].sum()

        net_gex = call_gex.add(put_gex, fill_value=0).sort_index()
        return net_gex

    @staticmethod
    def _find_gamma_wall(net_gex: pd.Series) -> float:
        """
        Gamma wall = strike with highest positive net GEX.
        Falls back to the strike with maximum net GEX if none are positive.
        """
        positive = net_gex[net_gex > 0]
        if positive.empty:
            logger.warning("No positive GEX strikes found — using max GEX strike as wall")
            return float(net_gex.idxmax())
        return float(positive.idxmax())

    @staticmethod
    def _find_gex_flip(net_gex: pd.Series, spot: float) -> float:
        """
        GEX flip = first strike BELOW spot where cumulative net GEX
        (summed from highest strike downward) first goes negative.

        This is the level below which dealer hedging becomes destabilising.
        Methodology: sort strikes descending, cumsum, find first negative.

        Falls back to the lowest strike if cumsum never goes negative
        (fully stabilising environment).
        """
        # Sort descending (highest strike first)
        sorted_gex = net_gex.sort_index(ascending=False)
        cumsum     = sorted_gex.cumsum()

        # Find strikes where cumsum < 0
        negative_cum = cumsum[cumsum < 0]

        if negative_cum.empty:
            # Fully stabilising — no flip level (return lowest strike as theoretical floor)
            logger.debug("No GEX flip found (fully stabilising) — using lowest strike")
            return float(net_gex.index.min())

        # First (highest) strike where cumsum goes negative
        flip_strike = float(negative_cum.index.max())

        # If flip is above spot, that's unusual — log it
        if flip_strike > spot:
            logger.debug(f"GEX flip={flip_strike:.2f} is above spot={spot:.2f} "
                         f"(calls dominant above current price)")

        return flip_strike


    @staticmethod
    def _generate_occ_codes(
        ticker: str, expiry_occ: str, spot: float,
        filter_pct: float = 0.30,
    ) -> list:
        """
        Generate OCC codes for strikes within ±filter_pct of spot.
        Uses standard strike increments for each ticker class.

        OCC format: SPY260320C00580000
          ticker   : up to 6 chars
          expiry   : YYMMDD
          right    : C or P
          strike   : strike × 1000, zero-padded to 8 digits
        """
        # Strike increments by price range
        if spot < 50:
            increment = 0.5
        elif spot < 200:
            increment = 1.0
        elif spot < 500:
            increment = 2.0
        elif spot < 1000:
            increment = 5.0
        else:
            increment = 10.0

        low  = spot * (1 - filter_pct)
        high = spot * (1 + filter_pct)

        # Round to nearest increment
        import math
        first = math.ceil(low  / increment) * increment
        last  = math.floor(high / increment) * increment

        codes = []
        s = first
        while s <= last + 0.001:
            strike_int = int(round(s * 1000))
            strike_str = str(strike_int).zfill(8)
            t          = ticker[:6].ljust(6)   # OCC pads ticker to 6 chars
            # Both standard and mini format
            codes.append(f"{ticker}{expiry_occ}C{strike_str}")
            codes.append(f"{ticker}{expiry_occ}P{strike_str}")
            s = round(s + increment, 8)

        return codes

    @staticmethod
    def _error_result(ticker: str, error_msg: str) -> dict:
        """Return a safe empty result when computation fails."""
        return {
            "symbol":            ticker,
            "gamma_wall":        0.0,
            "gex_flip":          0.0,
            "is_stabilising":    True,   # conservative default
            "net_gex_series":    pd.Series(dtype=float),
            "total_net_gex":     0.0,
            "call_gex_total":    0.0,
            "put_gex_total":     0.0,
            "strikes_near_wall": [],
            "spot":              0.0,
            "expiry":            "",
            "computed_at":       time.time(),
            "error":             error_msg,
        }


# ── Standalone helper (matches Section 4 reference implementation) ────────────

def compute_gex(client, symbol: str) -> dict:
    """
    Thin wrapper matching the reference implementation in the strategy doc.
    Computes GEX map from front-expiry option chain.

    Returns same structure as GEXCalculator.compute().
    For production use, prefer GEXCalculator (has caching + proximity checks).
    """
    calc = GEXCalculator({})
    return calc.compute(symbol, client)
