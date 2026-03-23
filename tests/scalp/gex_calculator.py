"""
src/scalp/signals/gex_calculator.py
=====================================
Gamma Exposure (GEX) calculator for the GEX Reversion Scalper.

Computes the GEX map from the front-expiry option chain via IBKRClient.

Returns the three key outputs used by scalp_gate.py and scalp_entry.py:
  gamma_wall    : float — strike with highest positive net GEX
                          (mean-reversion target / price magnet)
  gex_flip      : float — first strike where cumulative net GEX goes negative
                          (momentum trigger level — break below = amplifying)
  is_stabilising: bool  — True if total net GEX is positive (dampening env)
                          False if negative (amplifying env — momentum mode)

Also returns the full net_gex series for logging and analysis.

Sign convention (SpotGamma / Barchart standard):
  Call GEX = +gamma × open_interest × spot × 100  (stabilising — dealers buy dips)
  Put  GEX = −gamma × open_interest × spot × 100  (destabilising — dealers sell dips)

Refresh schedule (per strategy doc Section 4):
  09:00 ET — pre-market structural map
  11:00 ET — first intraday refresh (0DTE opens heavily in first hour)
  13:00 ET — second intraday refresh (post-lunch repositioning)

Data source strategy:
  PRIMARY  — IBKR streaming (reqMktData, modelGreeks) via _stream_option_greeks()
             Requires live TWS connection. Fetches real OI + model Greeks for
             generated OCC codes in batches of 50.
  FALLBACK — ibkr-connector get_option_snapshot() for a small set of near-ATM
             strikes. Used only when streaming is unavailable or returns < 10 rows.
             Note: yfinance-based snapshot was not designed for bulk GEX computation
             and will return sparse/empty data for most generated codes.

Group B live tests validate the PRIMARY path with a real TWS connection.
Group A/C tests use mock data and validate all pure-computation logic.
"""

import logging
import re
import time
from datetime import date, datetime
from typing import Dict, List, Optional

import pandas as pd
import numpy as np

logger = logging.getLogger("options_bot.scalp.signals.gex_calculator")

# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_REFRESH_TIMES  = ["09:00", "11:00", "13:00"]  # ET
DEFAULT_PROXIMITY_PCT  = 0.003   # 0.3% — price must be within this of a GEX level
DEFAULT_STREAM_WAIT    = 4.0     # seconds to wait for IBKR streaming to populate Greeks
DEFAULT_BATCH_SIZE     = 50      # IBKR reqMktData concurrent limit (safe limit)
MIN_VALID_ROWS         = 10      # minimum strike rows needed for meaningful GEX map


class GEXCalculator:
    """
    Computes GEX map from front-expiry option chain.

    Caches the last computed map. Call compute() at each refresh time
    (09:00, 11:00, 13:00 ET). Call get_cached() between refreshes to
    avoid unnecessary API calls.

    Data source priority:
      1. IBKR streaming via reqMktData (modelGreeks) — primary, requires live TWS
      2. ibkr-connector get_option_snapshot() — fallback for near-ATM strikes only

    Args:
        config: bot config dict. Reads from config["scalp"]["gex"] if present.
    """

    def __init__(self, config: dict):
        scalp_cfg = config.get("scalp", {})
        gex_cfg   = scalp_cfg.get("gex", {})

        self._refresh_times  = gex_cfg.get("refresh_times",  DEFAULT_REFRESH_TIMES)
        self._proximity_pct  = gex_cfg.get("proximity_pct",  DEFAULT_PROXIMITY_PCT)
        self._stream_wait    = gex_cfg.get("stream_wait_secs", DEFAULT_STREAM_WAIT)
        self._batch_size     = gex_cfg.get("batch_size",      DEFAULT_BATCH_SIZE)

        # Cache: maps symbol → last GEX result dict
        self._cache: Dict[str, dict] = {}

        logger.info(
            f"GEXCalculator initialised | refresh={self._refresh_times} | "
            f"proximity={self._proximity_pct*100:.1f}% | "
            f"stream_wait={self._stream_wait}s"
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def compute(self, symbol: str, client) -> dict:
        """
        Compute GEX map for a symbol from its front-expiry option chain.

        Args:
            symbol : Plain ticker e.g. "SPY", "QQQ", "NVDA"
            client : Connected IBKRClient instance (or IBKRConnector)

        Returns dict with keys:
            gamma_wall      : float — strike with highest positive net GEX
            gex_flip        : float — first strike where cumulative GEX < 0
            is_stabilising  : bool  — True = dampening, False = amplifying
            net_gex_series  : pd.Series — full net GEX per strike (index=strike)
            total_net_gex   : float — sum of all net GEX values
            spot            : float — spot price at computation time
            expiry          : str   — front expiry date used (ISO format)
            symbol          : str   — symbol computed for
            call_gex_total  : float — total positive (call) GEX
            put_gex_total   : float — total negative (put) GEX
            strikes_near_wall: list — strikes within proximity_pct of gamma wall
            data_source     : str  — 'streaming' | 'snapshot_fallback'
            computed_at     : float — time.time()
            error           : str  — non-empty if computation partially failed
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
                f"expiry={result['expiry']} "
                f"source={result['data_source']}"
            )
            return result

        except Exception as e:
            logger.error(f"[GEX:{ticker}] compute failed: {e}")
            err = self._error_result(ticker, str(e))
            self._cache[ticker] = err
            return err

    def get_cached(self, symbol: str) -> Optional[dict]:
        """Return last computed GEX map without making API calls. None if no cache."""
        ticker = symbol.replace("US.", "").upper()
        return self._cache.get(ticker)

    def is_near_gex_level(self, symbol: str, spot: float) -> dict:
        """
        Check whether current spot price is within proximity_pct of any
        significant GEX level (gamma wall or GEX flip).

        Returns:
            near_wall     : bool  — within proximity_pct of gamma_wall
            near_flip     : bool  — within proximity_pct of gex_flip
            wall_dist_pct : float — distance to gamma wall as % of spot
            flip_dist_pct : float — distance to GEX flip as % of spot
            side          : str   — "above_wall"|"below_wall"|"at_wall"|"none"
        """
        ticker = symbol.replace("US.", "").upper()
        cached = self._cache.get(ticker)

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
        near_wall  = wall_dist <= self._proximity_pct
        near_flip  = flip_dist <= self._proximity_pct

        if near_wall:
            side = "above_wall" if spot > wall else ("at_wall" if spot == wall else "below_wall")
        else:
            side = "none"

        return {
            "near_wall":      near_wall,
            "near_flip":      near_flip,
            "wall_dist_pct":  round(wall_dist * 100, 4),
            "flip_dist_pct":  round(flip_dist * 100, 4),
            "side":           side,
        }

    def should_refresh(self, symbol: str, et_now: Optional[datetime] = None) -> bool:
        """
        Return True if the cache for this symbol is stale and should be refreshed.
        Stale = no cache exists OR cache is older than the last scheduled refresh window.
        """
        ticker = symbol.replace("US.", "").upper()
        cached = self._cache.get(ticker)

        if not cached:
            return True

        if et_now is None:
            from zoneinfo import ZoneInfo
            et_now = datetime.now(ZoneInfo("America/New_York"))

        current_hhmm     = et_now.strftime("%H:%M")
        last_refresh_hhmm = None
        for rt in sorted(self._refresh_times):
            if current_hhmm >= rt:
                last_refresh_hhmm = rt

        if last_refresh_hhmm is None:
            return False  # before first refresh window today

        h, m = map(int, last_refresh_hhmm.split(":"))
        from zoneinfo import ZoneInfo
        last_refresh_dt = et_now.replace(hour=h, minute=m, second=0, microsecond=0)
        cached_at_dt    = datetime.fromtimestamp(
            cached["computed_at"], tz=ZoneInfo("America/New_York")
        )
        return cached_at_dt < last_refresh_dt

    # ── Internal ──────────────────────────────────────────────────────────────

    def _compute_internal(self, ticker: str, client) -> dict:
        """
        Core GEX computation — raises on failure.

        Data source priority:
          1. IBKR streaming (reqMktData modelGreeks) — primary
          2. ibkr-connector snapshot (yfinance-based) — fallback for near-ATM only
        """
        # Step 1: Spot price
        spot = client.get_spot_price(ticker)
        if spot <= 0 or (hasattr(spot, '__float__') and __import__('math').isnan(spot)):
            raise ValueError(f"Invalid spot price: {spot}")

        # Step 2: Front expiry
        expiries = client.get_option_expiries(ticker)
        if not expiries:
            raise ValueError(f"No expiries returned for {ticker}")
        front_expiry = self._select_front_expiry(expiries)
        logger.debug(f"[GEX:{ticker}] front expiry={front_expiry} "
                     f"(from {len(expiries)} available)")

        # Step 3: Get VALID strikes from the actual option chain (yfinance).
        # Replaces _generate_occ_codes() — generated codes often fail with
        # IBKR Error 200 ("No security definition") for strikes that don't
        # exist as listed contracts. get_option_chain() returns only real
        # strikes that IBKR/yfinance can actually fulfil.
        try:
            chain_calls = client.get_option_chain(ticker, front_expiry, "CALL")
            chain_puts  = client.get_option_chain(ticker, front_expiry, "PUT")
            chain_df    = pd.concat([chain_calls, chain_puts], ignore_index=True)
        except Exception as e:
            raise ValueError(f"get_option_chain failed for {ticker} {front_expiry}: {e}")

        if chain_df.empty or "code" not in chain_df.columns:
            raise ValueError(
                f"Empty or malformed chain for {ticker} {front_expiry}. "
                f"Columns returned: {list(chain_df.columns) if not chain_df.empty else '(empty)'}"
            )

        all_codes = chain_df["code"].tolist()
        logger.info(
            f"[GEX:{ticker}] chain has {len(all_codes)} valid contracts "
            f"(spot=${spot:.2f} expiry={front_expiry})"
        )

        # Step 4: Fetch Greeks — PRIMARY path: IBKR streaming
        snap        = pd.DataFrame()
        data_source = "none"

        if self._has_ib_connection(client):
            try:
                snap = self._stream_option_greeks(
                    client, all_codes, ticker, wait_secs=self._stream_wait
                )
                if not snap.empty and len(snap) >= MIN_VALID_ROWS:
                    data_source = "streaming"
                    logger.info(f"[GEX:{ticker}] streaming: {len(snap)} rows with Greeks")
                else:
                    logger.warning(
                        f"[GEX:{ticker}] streaming returned only {len(snap)} rows — "
                        f"falling back to snapshot"
                    )
                    snap = pd.DataFrame()
            except Exception as e:
                logger.warning(f"[GEX:{ticker}] streaming failed ({e}) — trying snapshot")
                snap = pd.DataFrame()

        # Step 4b: FALLBACK path — ibkr-connector snapshot (yfinance-based)
        # Uses the same valid chain codes, not generated OCC codes.
        if snap.empty:
            try:
                snap = client.get_option_snapshot(all_codes)
                if not snap.empty:
                    data_source = "snapshot_fallback"
                    logger.info(
                        f"[GEX:{ticker}] snapshot fallback: {len(snap)} rows "
                        f"(OI/gamma may be zero — streaming preferred)"
                    )
            except Exception as e:
                logger.warning(f"[GEX:{ticker}] snapshot also failed: {e}")

        if snap.empty:
            raise ValueError(
                f"No option data returned for {ticker} {front_expiry}. "
                f"Ensure TWS is connected with streaming=True for GEX computation."
            )

        # Step 5: Compute GEX per strike
        net_gex = self._compute_net_gex(snap, spot)
        if net_gex.empty:
            raise ValueError(
                f"Could not compute net GEX for {ticker} — "
                f"snapshot returned {len(snap)} rows but none had valid gamma+OI. "
                f"Using snapshot_fallback with yfinance data is not recommended "
                f"for GEX — connect TWS with streaming=True."
            )

        # Step 6: Derive key levels
        gamma_wall  = self._find_gamma_wall(net_gex)
        gex_flip    = self._find_gex_flip(net_gex, spot)
        total_gex   = float(net_gex.sum())

        # Step 7: Proximity strikes
        strikes_near_wall = [
            float(s) for s in net_gex.index
            if abs(s - gamma_wall) / spot <= self._proximity_pct
        ]

        # Step 8: Call/put totals for diagnostics
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
            "data_source":      data_source,
            "computed_at":      time.time(),
            "error":            "",
        }

    @staticmethod
    def _has_ib_connection(client) -> bool:
        """
        Return True if client has a live ib_insync connection we can use
        for streaming. Works with both IBKRConnector (options bot) and
        IBKRClient (ibkr-connector package).
        """
        # IBKRConnector (src/connectors/ibkr_connector.py)
        if hasattr(client, '_ib') and client._ib is not None:
            try:
                return client._ib.isConnected()
            except Exception:
                return False
        # IBKRClient (ibkr-connector package)
        if hasattr(client, 'ib') and client.ib is not None:
            try:
                return client.ib.isConnected()
            except Exception:
                return False
        return False

    @staticmethod
    def _get_ib(client):
        """Extract the underlying ib_insync IB instance from either connector type."""
        if hasattr(client, '_ib'):
            return client._ib
        if hasattr(client, 'ib'):
            return client.ib
        raise AttributeError("client has no _ib or ib attribute — cannot stream Greeks")

    def _stream_option_greeks(
        self,
        client,
        occ_codes: list,
        ticker: str,
        wait_secs: float = 4.0,
    ) -> pd.DataFrame:
        """
        Fetch option Greeks via IBKR streaming (reqMktData + modelGreeks).

        This is the PRIMARY data source for GEX computation. It subscribes
        to market data for each OCC code, waits for Greeks to populate, then
        cancels all subscriptions.

        Args:
            client    : Connector with live ib_insync IB instance
            occ_codes : List of OCC-format option codes
            ticker    : Symbol name for logging
            wait_secs : Seconds to wait after subscribing before reading Greeks.
                        4.0s is sufficient for IBKR to populate modelGreeks.
                        Reduce to 2.0s in tests with mock data.

        Returns:
            pd.DataFrame with columns:
                option_type, strike_price, option_gamma,
                option_open_interest, option_delta, option_iv
            Empty DataFrame if no valid rows collected.

        Note: genericTickList="106" (option implied volatility/greeks) causes
        Error 321 with snapshot=True on NP subscription. Using streaming
        (snapshot=False) with a timed wait resolves this — Greeks populate
        within 2-4 seconds without Error 321.
        """
        from ib_insync import Option as IBOption

        ib   = self._get_ib(client)
        rows = []

        occ_pattern = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")

        for batch_start in range(0, len(occ_codes), self._batch_size):
            batch    = occ_codes[batch_start : batch_start + self._batch_size]
            tick_map = {}

            # Subscribe to market data for this batch
            for code in batch:
                m = occ_pattern.match(str(code).replace(" ", ""))
                if not m:
                    continue

                sym, exp, right, strike_str = m.groups()
                strike   = int(strike_str) / 1000.0
                exp_full = f"20{exp}"   # 260320 → 20260320

                contract = IBOption(
                    symbol=sym,
                    lastTradeDateOrContractMonth=exp_full,
                    strike=strike,
                    right=right,
                    exchange="SMART",
                    currency="USD",
                )

                # Qualify contract first — IBKR fills in conId and rejects
                # contracts that don't exist (avoids Error 200 spam from
                # strikes that aren't listed). This is a synchronous call.
                try:
                    qualified = ib.qualifyContracts(contract)
                    if not qualified:
                        logger.debug(f"[GEX:{ticker}] skip {code}: not qualified")
                        continue
                except Exception:
                    logger.debug(f"[GEX:{ticker}] skip {code}: qualify failed")
                    continue

                ib_ticker = ib.reqMktData(
                    contract,
                    genericTickList="106",   # model greeks + IV
                    snapshot=False,
                    regulatorySnapshot=False,
                )
                tick_map[code] = (ib_ticker, strike, right)

            # Wait for Greeks to populate
            time.sleep(wait_secs)

            # Collect populated Greeks and cancel subscriptions
            for code, (ib_ticker, strike, right) in tick_map.items():
                try:
                    g     = ib_ticker.modelGreeks
                    gamma = float(g.gamma)    if g and g.gamma    is not None else 0.0
                    delta = float(g.delta)    if g and g.delta    is not None else 0.0
                    iv    = float(g.impliedVol) if g and g.impliedVol is not None else 0.0

                    # Open interest — prefer optionOpenInterest, fall back to volume
                    oi = float(getattr(ib_ticker, "optionOpenInterest", 0) or 0)
                    if oi <= 0:
                        oi = float(getattr(ib_ticker, "volume", 0) or 0)

                    if gamma > 0:   # only useful rows
                        rows.append({
                            "option_type":          right,
                            "strike_price":         strike,
                            "option_gamma":         gamma,
                            "option_open_interest": oi,
                            "option_delta":         delta,
                            "option_iv":            iv,
                        })

                except Exception as e:
                    logger.debug(f"[GEX:{ticker}] skip {code}: {e}")

                finally:
                    try:
                        ib.cancelMktData(ib_ticker.contract)
                    except Exception:
                        pass

            logger.info(
                f"[GEX:{ticker}] batch {batch_start // self._batch_size + 1}: "
                f"{len(tick_map)} requested → {len(rows)} valid rows so far"
            )

        return pd.DataFrame(rows) if rows else pd.DataFrame()

    @staticmethod
    def _select_front_expiry(expiries: List[str]) -> str:
        """
        Select the front (nearest) expiry from a list of ISO date strings.
        Guarantees the returned expiry is today or in the future.
        """
        today = date.today()
        valid = sorted([e for e in expiries if date.fromisoformat(e) >= today])
        if not valid:
            raise ValueError(f"No valid (non-past) expiries in: {expiries}")
        return valid[0]

    @staticmethod
    def _compute_net_gex(snap: pd.DataFrame, spot: float) -> pd.Series:
        """
        Compute net GEX per strike from a snapshot DataFrame.

        Accepts columns from both IBKRConnector snapshot and _stream_option_greeks:
          option_type / right / type / call_put
          strike_price / strike
          option_gamma / gamma
          option_open_interest / open_interest / oi

        SpotGamma sign convention:
          Call GEX = +gamma × OI × spot × 100
          Put  GEX = −gamma × OI × spot × 100
        """
        df = snap.copy()

        # Normalise column names
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

        # Derive option_type from OCC code if missing
        if "option_type" not in df.columns and "code" in df.columns:
            def _extract_type(code: str) -> str:
                m = re.search(r'\d{6}([CP])', str(code))
                return m.group(1) if m else ""
            df["option_type"] = df["code"].apply(_extract_type)
            df = df[df["option_type"].isin(["C", "P"])]
            logger.debug("Derived option_type from OCC code column")

        required = {"gamma", "oi", "strike", "option_type"}
        missing  = required - set(df.columns)
        if missing:
            raise ValueError(
                f"Snapshot missing required columns: {missing}. "
                f"Available: {list(snap.columns)}"
            )

        # Drop rows with zero/missing gamma or OI
        df = df.dropna(subset=["gamma", "oi"])
        df = df[df["gamma"] > 0]
        df = df[df["oi"] > 0]

        if df.empty:
            return pd.Series(dtype=float)

        df["option_type"] = df["option_type"].str.upper().str[0]

        calls = df[df["option_type"] == "C"].copy()
        puts  = df[df["option_type"] == "P"].copy()

        calls["gex"] =  calls["gamma"] * calls["oi"] * spot * 100
        puts["gex"]  = -puts["gamma"]  * puts["oi"]  * spot * 100

        call_gex = calls.groupby("strike")["gex"].sum()
        put_gex  = puts.groupby("strike")["gex"].sum()
        net_gex  = call_gex.add(put_gex, fill_value=0).sort_index()

        return net_gex

    @staticmethod
    def _find_gamma_wall(net_gex: pd.Series) -> float:
        """Gamma wall = strike with highest positive net GEX."""
        positive = net_gex[net_gex > 0]
        if positive.empty:
            logger.warning("No positive GEX strikes — using max GEX strike as wall")
            return float(net_gex.idxmax())
        return float(positive.idxmax())

    @staticmethod
    def _find_gex_flip(net_gex: pd.Series, spot: float) -> float:
        """
        GEX flip = first strike where cumulative net GEX (top-down) goes negative.
        Falls back to the lowest strike in a fully-stabilising environment.
        """
        sorted_gex   = net_gex.sort_index(ascending=False)
        cumsum       = sorted_gex.cumsum()
        negative_cum = cumsum[cumsum < 0]

        if negative_cum.empty:
            logger.debug("No GEX flip found (fully stabilising) — using lowest strike")
            return float(net_gex.index.min())

        flip_strike = float(negative_cum.index.max())
        if flip_strike > spot:
            logger.debug(
                f"GEX flip={flip_strike:.2f} is above spot={spot:.2f} "
                f"(calls dominant above current price)"
            )
        return flip_strike

    @staticmethod
    def _generate_occ_codes(
        ticker: str, expiry_occ: str, spot: float, filter_pct: float = 0.30,
    ) -> list:
        """
        Generate OCC codes for strikes within ±filter_pct of spot.
        OCC format: SPY260320C00580000
        """
        if spot < 50:       increment = 0.5
        elif spot < 200:    increment = 1.0
        elif spot < 500:    increment = 2.0
        elif spot < 1000:   increment = 5.0
        else:               increment = 10.0

        import math
        low   = spot * (1 - filter_pct)
        high  = spot * (1 + filter_pct)
        first = math.ceil(low  / increment) * increment
        last  = math.floor(high / increment) * increment

        codes = []
        s = first
        while s <= last + 0.001:
            strike_int = int(round(s * 1000))
            strike_str = str(strike_int).zfill(8)
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
            "data_source":       "error",
            "computed_at":       time.time(),
            "error":             error_msg,
        }


# ── Standalone helper ─────────────────────────────────────────────────────────

def compute_gex(client, symbol: str) -> dict:
    """
    Thin wrapper matching the reference implementation in the strategy doc.
    For production use, prefer GEXCalculator (has caching + proximity checks).
    """
    calc = GEXCalculator({})
    return calc.compute(symbol, client)
