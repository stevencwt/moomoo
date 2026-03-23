"""
src/scalp/signals/gex_calculator.py
=====================================
Gamma Exposure (GEX) calculator for the GEX Reversion Scalper.

Computes the GEX map from the front-expiry option chain.

Data source: ibkr-connector IBKRClient public API only.
  - get_option_chain()    → valid OCC codes per expiry
  - get_option_snapshot() → option_gamma, option_open_interest per strike

No direct ib_insync access. No reqMktData. No reqSecDefOptParams.
All data comes through the validated ibkr-connector public API.

Returns three key outputs:
  gamma_wall     : strike with highest positive net GEX (price magnet)
  gex_flip       : first strike where cumulative net GEX goes negative
  is_stabilising : True if total net GEX > 0 (dampening environment)

Sign convention (SpotGamma standard):
  Call GEX = +gamma × open_interest × spot × 100
  Put  GEX = −gamma × open_interest × spot × 100

Refresh schedule: 09:00 ET, 11:00 ET, 13:00 ET
"""

import logging
import re
import time
from datetime import date, datetime
from typing import Dict, List, Optional

import pandas as pd
import numpy as np

logger = logging.getLogger("options_bot.scalp.signals.gex_calculator")

DEFAULT_REFRESH_TIMES = ["09:00", "11:00", "13:00"]
DEFAULT_PROXIMITY_PCT = 0.003
MIN_VALID_ROWS        = 10


class GEXCalculator:
    """
    Computes GEX map using ibkr-connector public API.

    Pipeline:
      1. get_option_expiries() → front expiry
      2. get_option_chain(expiry, "ALL") → valid OCC codes + strike_price
      3. get_option_snapshot(codes) → option_gamma, option_open_interest
      4. _compute_net_gex() → net GEX per strike
      5. Derive gamma_wall, gex_flip, is_stabilising
    """

    def __init__(self, config: dict):
        gex_cfg = config.get("scalp", {}).get("gex", {})
        self._refresh_times = gex_cfg.get("refresh_times", DEFAULT_REFRESH_TIMES)
        self._proximity_pct = gex_cfg.get("proximity_pct", DEFAULT_PROXIMITY_PCT)
        self._cache: Dict[str, dict] = {}
        logger.info(
            f"GEXCalculator initialised | refresh={self._refresh_times} | "
            f"proximity={self._proximity_pct*100:.1f}%"
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def compute(self, symbol: str, client) -> dict:
        """
        Compute GEX map for symbol using ibkr-connector public API.

        Args:
            symbol : Plain ticker or "US.SPY" — "US." prefix is stripped
            client : IBKRClient (ibkr-connector) or IBKRConnector (local)
                     Must implement: get_spot_price, get_option_expiries,
                     get_option_chain, get_option_snapshot

        Returns dict with keys:
            gamma_wall, gex_flip, is_stabilising, net_gex_series,
            total_net_gex, call_gex_total, put_gex_total,
            strikes_near_wall, spot, expiry, symbol,
            data_source, computed_at, error
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
                f"rows={len(result['net_gex_series'])} "
                f"expiry={result['expiry']}"
            )
            return result
        except Exception as e:
            logger.error(f"[GEX:{ticker}] compute failed: {e}")
            err = self._error_result(ticker, str(e))
            self._cache[ticker] = err
            return err

    def get_cached(self, symbol: str) -> Optional[dict]:
        """Return last computed GEX map without API calls. None if no cache."""
        return self._cache.get(symbol.replace("US.", "").upper())

    def is_near_gex_level(self, symbol: str, spot: float) -> dict:
        """Check whether spot is within proximity_pct of gamma_wall or gex_flip."""
        ticker = symbol.replace("US.", "").upper()
        cached = self._cache.get(ticker)
        if not cached or cached.get("error"):
            return {"near_wall": False, "near_flip": False,
                    "wall_dist_pct": 999.0, "flip_dist_pct": 999.0, "side": "none"}

        wall      = cached["gamma_wall"]
        flip      = cached["gex_flip"]
        wall_dist = abs(spot - wall) / spot
        flip_dist = abs(spot - flip) / spot
        near_wall = wall_dist <= self._proximity_pct
        near_flip = flip_dist <= self._proximity_pct
        side      = ("above_wall" if spot > wall else
                     ("at_wall"   if spot == wall else "below_wall")) if near_wall else "none"
        return {
            "near_wall":     near_wall,
            "near_flip":     near_flip,
            "wall_dist_pct": round(wall_dist * 100, 4),
            "flip_dist_pct": round(flip_dist * 100, 4),
            "side":          side,
        }

    def should_refresh(self, symbol: str, et_now: Optional[datetime] = None) -> bool:
        """True if cache is absent or older than the last scheduled refresh window."""
        ticker = symbol.replace("US.", "").upper()
        cached = self._cache.get(ticker)
        if not cached:
            return True
        if et_now is None:
            from zoneinfo import ZoneInfo
            et_now = datetime.now(ZoneInfo("America/New_York"))
        current_hhmm      = et_now.strftime("%H:%M")
        last_refresh_hhmm = None
        for rt in sorted(self._refresh_times):
            if current_hhmm >= rt:
                last_refresh_hhmm = rt
        if last_refresh_hhmm is None:
            return False
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
        Core GEX pipeline using ibkr-connector public API only.

        Step 1: get_spot_price()
        Step 2: get_option_expiries() → front expiry
        Step 3: get_option_chain(expiry, "ALL") → valid OCC codes
                Falls back to separate CALL + PUT calls if ALL not supported.
        Step 4: get_option_snapshot(codes) → option_gamma, option_open_interest
        Step 5: _compute_net_gex() → net GEX per strike
        Step 6: Derive gamma_wall, gex_flip, is_stabilising
        """
        # Step 1: Spot price
        spot = client.get_spot_price(ticker)
        if not spot or spot <= 0:
            raise ValueError(f"Invalid spot price: {spot}")
        import math
        if math.isnan(spot):
            raise ValueError(f"Spot price is NaN for {ticker}")

        # Step 2: Front expiry via ibkr-connector public API
        expiries = client.get_option_expiries(ticker)
        if not expiries:
            raise ValueError(f"No expiries returned for {ticker}")
        front_expiry = self._select_front_expiry(expiries)
        logger.debug(f"[GEX:{ticker}] front expiry={front_expiry} spot=${spot:.2f}")

        # Step 3: Full option chain from ibkr-connector public API
        chain_df = client.get_option_chain(ticker, front_expiry, "ALL")
        if chain_df.empty or "code" not in chain_df.columns:
            raise ValueError(f"Empty chain for {ticker} {front_expiry}")

        # Filter strikes by DTE — near-term expiries only have ATM contracts.
        # reqSecDefOptParams returns strikes across ALL expiries combined;
        # submitting far-OTM strikes for a 2-day expiry causes Error 200.
        # ≤7 DTE (weekly/0DTE): ±15% — only listed ATM strikes exist
        # >7 DTE (monthly+):    ±30% — broader range has real contracts
        dte = (date.fromisoformat(front_expiry) - date.today()).days
        pct = 0.15 if dte <= 7 else 0.30
        chain_df = chain_df[
            chain_df["strike_price"].between(spot * (1 - pct), spot * (1 + pct))
        ]
        logger.debug(f"[GEX:{ticker}] DTE={dte} → ±{pct*100:.0f}% filter")
        codes = chain_df["code"].dropna().tolist()
        if not codes:
            raise ValueError(
                f"No strikes within ±30% of spot ${spot:.2f} "
                f"for {ticker} {front_expiry}"
            )
        logger.info(f"[GEX:{ticker}] {len(codes)} contracts for {front_expiry}")

        # Step 4: Greeks + OI via ibkr-connector public API
        snap = client.get_option_snapshot(codes)
        if snap is None or snap.empty:
            raise ValueError(
                f"get_option_snapshot() returned empty for {ticker} {front_expiry}."
            )
        logger.info(f"[GEX:{ticker}] snapshot: {len(snap)} rows")

        # Step 5: Compute net GEX per strike
        net_gex = self._compute_net_gex(snap, spot)
        if net_gex.empty:
            raise ValueError(
                f"net_gex empty after computation for {ticker}. "
                f"Snapshot had {len(snap)} rows — check option_gamma and "
                f"option_open_interest are non-zero in snapshot response."
            )

        # Step 6: Key levels
        gamma_wall     = self._find_gamma_wall(net_gex)
        gex_flip       = self._find_gex_flip(net_gex, spot)
        total_gex      = float(net_gex.sum())
        call_gex_total = float(net_gex[net_gex > 0].sum())
        put_gex_total  = float(net_gex[net_gex < 0].sum())

        strikes_near_wall = [
            float(s) for s in net_gex.index
            if abs(s - gamma_wall) / spot <= self._proximity_pct
        ]

        return {
            "symbol":            ticker,
            "gamma_wall":        float(gamma_wall),
            "gex_flip":          float(gex_flip),
            "is_stabilising":    total_gex > 0,
            "net_gex_series":    net_gex,
            "total_net_gex":     round(total_gex, 2),
            "call_gex_total":    round(call_gex_total, 2),
            "put_gex_total":     round(put_gex_total, 2),
            "strikes_near_wall": strikes_near_wall,
            "spot":              round(spot, 2),
            "expiry":            front_expiry,
            "data_source":       "ibkr_snapshot",
            "computed_at":       time.time(),
            "error":             "",
        }

    def _stream_greeks(
        self,
        ib,
        ticker:    str,
        expiry:    str,
        occ_codes: list,
        wait_secs: float = 4.0,
    ) -> pd.DataFrame:
        """
        Fetch modelGreeks for a list of OCC codes via IBKR streaming.

        Uses reqMktData(snapshot=False, genericTickList="106") — the only
        mode that returns model Greeks (delta, gamma, IV) with an IBKR
        data subscription. snapshot=True with genericTickList causes Error 321.

        Subscribes to all codes in batches of batch_size, waits wait_secs
        for IBKR to populate modelGreeks, reads, then cancels all subscriptions.

        Args:
            ib        : ib_insync IB instance (client._ib)
            ticker    : Symbol for logging
            expiry    : ISO expiry date e.g. "2026-03-23"
            occ_codes : OCC codes from _get_occ_codes_via_ib (valid contracts)
            wait_secs : Seconds to wait for Greeks to populate (default 4.0)

        Returns:
            DataFrame with columns: option_type, strike_price, option_gamma,
            option_open_interest, option_delta, option_iv
            Only rows where gamma > 0 are included.
        """
        from ibkr_connector.utils import parse_occ_code
        from ib_insync import Option as IBOption

        expiry_ibkr = expiry.replace("-", "")   # 2026-03-23 → 20260323
        rows        = []

        for batch_start in range(0, len(occ_codes), self._batch_size):
            batch    = occ_codes[batch_start : batch_start + self._batch_size]
            tick_map = {}   # occ_code → (ib_ticker, strike, right)

            for code in batch:
                try:
                    sym, exp, right, strike = parse_occ_code(code)
                    contract = IBOption(
                        symbol=sym,
                        lastTradeDateOrContractMonth=expiry_ibkr,
                        strike=strike,
                        right=right,
                        exchange="SMART",
                        currency="USD",
                    )
                    ib_ticker = ib.reqMktData(
                        contract,
                        genericTickList="106",  # model Greeks + IV
                        snapshot=False,         # streaming — snapshot=True causes Error 321
                        regulatorySnapshot=False,
                    )
                    tick_map[code] = (ib_ticker, contract, strike, right)
                except Exception as e:
                    logger.debug(f"[GEX:{ticker}] subscribe failed {code}: {e}")

            # Wait for IBKR to populate modelGreeks
            time.sleep(wait_secs)

            # Read Greeks and cancel subscriptions
            for code, (ib_ticker, contract, strike, right) in tick_map.items():
                try:
                    g     = ib_ticker.modelGreeks
                    gamma = float(g.gamma)      if g and g.gamma      is not None else 0.0
                    delta = float(g.delta)      if g and g.delta      is not None else 0.0
                    iv    = float(g.impliedVol) if g and g.impliedVol is not None else 0.0

                    # Open interest — prefer optionOpenInterest tick
                    oi = float(getattr(ib_ticker, "optionOpenInterest", 0) or 0)
                    if oi <= 0:
                        oi = float(getattr(ib_ticker, "volume", 0) or 0)

                    if gamma > 0:
                        rows.append({
                            "option_type":          right,
                            "strike_price":         float(strike),
                            "option_gamma":         gamma,
                            "option_open_interest": oi,
                            "option_delta":         delta,
                            "option_iv":            iv,
                        })
                except Exception as e:
                    logger.debug(f"[GEX:{ticker}] read failed {code}: {e}")
                finally:
                    try:
                        ib.cancelMktData(contract)
                    except Exception:
                        pass

            logger.debug(
                f"[GEX:{ticker}] batch {batch_start // self._batch_size + 1}: "
                f"{len(tick_map)} subscribed → {len(rows)} valid rows so far"
            )

        return pd.DataFrame(rows) if rows else pd.DataFrame()

    @staticmethod
    def _get_occ_codes_via_ib(
        client, ticker: str, expiry: str, spot: float,
        filter_pct: float = 0.30,
    ) -> list:
        """
        Get valid OCC codes by calling reqSecDefOptParams directly on
        client._ib with a properly qualified conId.

        client.get_option_chain() has the same bug as get_option_expiries():
        the internal qualifyContracts call creates a new Stock() object in a
        way that doesn't populate conId before reqSecDefOptParams is called.
        Here we qualify once explicitly, read conId, then call
        reqSecDefOptParams directly — proven to work in live diagnostics.

        Returns OCC codes for all valid strikes within ±filter_pct of spot.
        """
        from ib_insync import Stock as IBStock
        from ibkr_connector.utils import build_occ_code

        # Qualify to get conId — same approach proven in diagnostic
        stock     = IBStock(ticker, "SMART", "USD")
        qualified = client._ib.qualifyContracts(stock)
        if not qualified or not qualified[0].conId:
            raise ValueError(f"qualifyContracts failed for {ticker}")

        con_id = qualified[0].conId
        chains = client._ib.reqSecDefOptParams(
            underlyingSymbol= ticker,
            futFopExchange=   "",
            underlyingSecType="STK",
            underlyingConId=  con_id,
        )
        if not chains:
            raise ValueError(f"reqSecDefOptParams returned nothing for {ticker}")

        # Find the exchange with the most strikes (usually SMART or NASDAQOM)
        expiry_ibkr = expiry.replace("-", "")   # 2026-03-23 → 20260323
        valid_chains = [c for c in chains if expiry_ibkr in c.expirations]
        if not valid_chains:
            raise ValueError(
                f"Expiry {expiry} not in any chain for {ticker}. "
                f"Available chains: {len(chains)}"
            )
        # Pick exchange with most strikes (broadest coverage)
        best_chain  = max(valid_chains, key=lambda c: len(c.strikes))
        all_strikes = sorted(best_chain.strikes)

        # Filter to ±filter_pct of spot
        lo = spot * (1 - filter_pct)
        hi = spot * (1 + filter_pct)
        strikes = [s for s in all_strikes if lo <= s <= hi]

        if not strikes:
            raise ValueError(
                f"No strikes within ±{filter_pct*100:.0f}% of "
                f"spot ${spot:.2f} for {ticker} {expiry}"
            )

        logger.debug(
            f"[GEX:{ticker}] {len(strikes)} strikes from {best_chain.exchange} "
            f"(conId={con_id}, ±{filter_pct*100:.0f}% of ${spot:.2f})"
        )

        codes = []
        for strike in strikes:
            for right in ("C", "P"):
                codes.append(build_occ_code(ticker, expiry, right, strike))
        return codes

    @staticmethod
    def _fetch_chain(client, ticker: str, expiry: str) -> pd.DataFrame:
        """
        Fetch the full option chain (calls + puts) for a given expiry.
        Tries "ALL" first; falls back to separate CALL + PUT calls.
        """
        # Try ALL first (ibkr-connector IBKRClient supports this)
        try:
            chain = client.get_option_chain(ticker, expiry, "ALL")
            if chain is not None and not chain.empty:
                return chain
        except Exception:
            pass

        # Fallback: separate CALL and PUT
        frames = []
        for side in ("CALL", "PUT"):
            try:
                df = client.get_option_chain(ticker, expiry, side)
                if df is not None and not df.empty:
                    frames.append(df)
            except Exception as e:
                logger.warning(f"[GEX:{ticker}] get_option_chain({side}) failed: {e}")

        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    @staticmethod
    def _get_front_expiry_yf(ticker: str) -> str:
        """
        Return the nearest future expiry date using yfinance metadata.

        yf.Ticker(ticker).options returns a tuple of ISO date strings already
        sorted ascending — no market data subscription required.

        Used instead of client.get_option_expiries() which has a known bug
        where the internal qualifyContracts call does not wait for conId to
        populate before reqSecDefOptParams runs, resulting in empty expirations
        even when IBKR has data available.
        """
        try:
            import yfinance as yf
            opts = yf.Ticker(ticker).options   # tuple of "YYYY-MM-DD" strings
        except Exception as e:
            raise ValueError(f"yfinance expiry lookup failed for {ticker}: {e}")
        if not opts:
            raise ValueError(f"No option expiries from yfinance for {ticker}")
        today = date.today()
        valid = sorted(e for e in opts if date.fromisoformat(e) >= today)
        if not valid:
            raise ValueError(f"No future expiries in yfinance data for {ticker}")
        return valid[0]

    @staticmethod
    def _select_front_expiry(expiries: List[str]) -> str:
        today = date.today()
        valid = sorted(e for e in expiries if date.fromisoformat(e) >= today)
        if not valid:
            raise ValueError(f"No valid (non-past) expiries in: {expiries}")
        return valid[0]

    @staticmethod
    def _compute_net_gex(snap: pd.DataFrame, spot: float) -> pd.Series:
        """
        Compute net GEX per strike from get_option_snapshot() output.

        ibkr-connector snapshot columns:
          code, option_delta, option_gamma, option_theta, option_vega,
          option_iv, option_open_interest, strike_price, expiry

        SpotGamma sign convention:
          Call GEX = +gamma × OI × spot × 100
          Put  GEX = −gamma × OI × spot × 100
        """
        df = snap.copy()

        # Normalise column names — handle ibkr-connector and alternate schemas
        col_map = {}
        for col in df.columns:
            lc = col.lower()
            if lc in ("option_open_interest", "open_interest", "oi"):
                col_map[col] = "oi"
            elif lc in ("option_gamma", "gamma"):
                col_map[col] = "gamma"
            elif lc in ("strike_price", "strike"):
                col_map[col] = "strike"
            elif lc in ("option_type", "type", "right", "call_put"):
                col_map[col] = "option_type"
        df = df.rename(columns=col_map)

        # Derive option_type from OCC code if not present
        if "option_type" not in df.columns and "code" in df.columns:
            def _extract_type(code: str) -> str:
                m = re.search(r'\d{6}([CP])', str(code))
                return m.group(1) if m else ""
            df["option_type"] = df["code"].apply(_extract_type)
            df = df[df["option_type"].isin(["C", "P"])]

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
        df = df[df["oi"]    > 0]

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
        positive = net_gex[net_gex > 0]
        if positive.empty:
            logger.warning("No positive GEX strikes — using max GEX strike as wall")
            return float(net_gex.idxmax())
        return float(positive.idxmax())

    @staticmethod
    def _find_gex_flip(net_gex: pd.Series, spot: float) -> float:
        sorted_gex   = net_gex.sort_index(ascending=False)
        cumsum       = sorted_gex.cumsum()
        negative_cum = cumsum[cumsum < 0]
        if negative_cum.empty:
            logger.debug("No GEX flip (fully stabilising) — using lowest strike")
            return float(net_gex.index.min())
        flip_strike = float(negative_cum.index.max())
        if flip_strike > spot:
            logger.debug(f"GEX flip={flip_strike:.2f} above spot={spot:.2f}")
        return flip_strike

    @staticmethod
    def _generate_occ_codes(ticker: str, expiry_occ: str, spot: float,
                            filter_pct: float = 0.30) -> list:
        """Kept for unit tests. Not used in live computation."""
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
        s     = first
        while s <= last + 0.001:
            strike_int = int(round(s * 1000))
            codes.append(f"{ticker}{expiry_occ}C{str(strike_int).zfill(8)}")
            codes.append(f"{ticker}{expiry_occ}P{str(strike_int).zfill(8)}")
            s = round(s + increment, 8)
        return codes

    @staticmethod
    def _has_ib_connection(client) -> bool:
        """Kept for unit tests. Not used in live computation."""
        if hasattr(client, '_ib') and client._ib is not None:
            try:
                return client._ib.isConnected()
            except Exception:
                return False
        if hasattr(client, 'ib') and client.ib is not None:
            try:
                return client.ib.isConnected()
            except Exception:
                return False
        return False

    @staticmethod
    def _error_result(ticker: str, error_msg: str) -> dict:
        return {
            "symbol":            ticker,
            "gamma_wall":        0.0,
            "gex_flip":          0.0,
            "is_stabilising":    True,
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
    """Thin wrapper for one-off use. Prefer GEXCalculator for caching."""
    return GEXCalculator({}).compute(symbol, client)
