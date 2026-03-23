"""
tests/scalp/test_gex_calculator.py  —  deploy to: /Users/user/moomoo/tests/scalp/
====================================
Groups A (pure computation), B (live IBKR), C (edge cases)

Run A+C only (no TWS):
    python3 -m pytest tests/scalp/test_gex_calculator.py -v -k "not TestLive"

Run Group B (TWS port 7496, market hours):
    python3 -m pytest tests/scalp/test_gex_calculator.py::TestLiveGEXCompute -v -s
"""

import os, sys, time, re
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from src.scalp.signals.gex_calculator import GEXCalculator, compute_gex


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def calc():
    return GEXCalculator({})

@pytest.fixture
def spy_snap():
    """Call-dominant — positive total GEX (stabilising)."""
    spot = 562.0
    df = pd.DataFrame([
        {"option_type": "C", "strike_price": 560.0, "option_gamma": 0.060, "option_open_interest": 20000},
        {"option_type": "C", "strike_price": 565.0, "option_gamma": 0.055, "option_open_interest": 25000},
        {"option_type": "C", "strike_price": 570.0, "option_gamma": 0.040, "option_open_interest": 30000},
        {"option_type": "C", "strike_price": 575.0, "option_gamma": 0.028, "option_open_interest": 22000},
        {"option_type": "P", "strike_price": 558.0, "option_gamma": 0.055, "option_open_interest": 12000},
        {"option_type": "P", "strike_price": 555.0, "option_gamma": 0.048, "option_open_interest": 18000},
        {"option_type": "P", "strike_price": 550.0, "option_gamma": 0.038, "option_open_interest": 30000},
        {"option_type": "P", "strike_price": 545.0, "option_gamma": 0.025, "option_open_interest": 35000},
    ])
    return df, spot

@pytest.fixture
def put_heavy_snap():
    """Put-dominant — negative total GEX (amplifying)."""
    spot = 562.0
    df = pd.DataFrame([
        {"option_type": "C", "strike_price": 565.0, "option_gamma": 0.020, "option_open_interest":  3000},
        {"option_type": "P", "strike_price": 558.0, "option_gamma": 0.060, "option_open_interest": 40000},
        {"option_type": "P", "strike_price": 555.0, "option_gamma": 0.050, "option_open_interest": 50000},
        {"option_type": "P", "strike_price": 550.0, "option_gamma": 0.040, "option_open_interest": 45000},
    ])
    return df, spot


# ── Group A — Pure computation ────────────────────────────────────────────────

class TestNetGEXComputation:

    def test_call_gex_positive(self, spy_snap):
        snap, spot = spy_snap
        net = GEXCalculator._compute_net_gex(snap, spot)
        for s in [560.0, 565.0, 570.0, 575.0]:
            assert net.loc[s] > 0

    def test_put_gex_negative(self, spy_snap):
        snap, spot = spy_snap
        net = GEXCalculator._compute_net_gex(snap, spot)
        for s in [545.0, 550.0, 555.0, 558.0]:
            assert net.loc[s] < 0

    def test_call_formula_exact(self):
        snap = pd.DataFrame([{"option_type": "C", "strike_price": 100.0, "option_gamma": 0.05, "option_open_interest": 1000}])
        net = GEXCalculator._compute_net_gex(snap, 100.0)
        assert abs(net.loc[100.0] - (0.05 * 1000 * 100.0 * 100)) < 0.01

    def test_put_formula_exact(self):
        snap = pd.DataFrame([{"option_type": "P", "strike_price": 100.0, "option_gamma": 0.05, "option_open_interest": 1000}])
        net = GEXCalculator._compute_net_gex(snap, 100.0)
        assert abs(net.loc[100.0] + (0.05 * 1000 * 100.0 * 100)) < 0.01

    def test_same_strike_aggregated(self):
        snap = pd.DataFrame([
            {"option_type": "C", "strike_price": 100.0, "option_gamma": 0.05, "option_open_interest": 1000},
            {"option_type": "C", "strike_price": 100.0, "option_gamma": 0.03, "option_open_interest":  500},
        ])
        net = GEXCalculator._compute_net_gex(snap, 100.0)
        assert len(net) == 1
        expected = (0.05 * 1000 + 0.03 * 500) * 100.0 * 100
        assert abs(net.loc[100.0] - expected) < 0.01

    def test_drops_zero_gamma(self):
        snap = pd.DataFrame([
            {"option_type": "C", "strike_price": 100.0, "option_gamma": 0.0,  "option_open_interest": 1000},
            {"option_type": "C", "strike_price": 105.0, "option_gamma": 0.05, "option_open_interest":  500},
        ])
        net = GEXCalculator._compute_net_gex(snap, 100.0)
        assert 100.0 not in net.index and 105.0 in net.index

    def test_drops_zero_oi(self):
        snap = pd.DataFrame([
            {"option_type": "C", "strike_price": 100.0, "option_gamma": 0.05, "option_open_interest":   0},
            {"option_type": "C", "strike_price": 105.0, "option_gamma": 0.05, "option_open_interest": 500},
        ])
        net = GEXCalculator._compute_net_gex(snap, 100.0)
        assert 100.0 not in net.index and 105.0 in net.index

    def test_stabilising_positive(self, spy_snap):
        snap, spot = spy_snap
        assert GEXCalculator._compute_net_gex(snap, spot).sum() > 0

    def test_amplifying_negative(self, put_heavy_snap):
        snap, spot = put_heavy_snap
        assert GEXCalculator._compute_net_gex(snap, spot).sum() < 0


class TestGammaWall:

    def test_returns_highest_positive(self, spy_snap):
        snap, spot = spy_snap
        net  = GEXCalculator._compute_net_gex(snap, spot)
        wall = GEXCalculator._find_gamma_wall(net)
        assert wall in [560.0, 565.0, 570.0, 575.0]

    def test_is_valid_index(self, spy_snap):
        snap, spot = spy_snap
        net = GEXCalculator._compute_net_gex(snap, spot)
        assert GEXCalculator._find_gamma_wall(net) in net.index.tolist()

    def test_fallback_when_all_negative(self):
        net = pd.Series({100.0: -500.0, 105.0: -200.0, 110.0: -800.0})
        assert GEXCalculator._find_gamma_wall(net) == 105.0


class TestGEXFlip:

    def test_flip_lte_wall(self, spy_snap):
        snap, spot = spy_snap
        net  = GEXCalculator._compute_net_gex(snap, spot)
        assert GEXCalculator._find_gex_flip(net, spot) <= GEXCalculator._find_gamma_wall(net)

    def test_flip_is_valid_strike(self, spy_snap):
        snap, spot = spy_snap
        net  = GEXCalculator._compute_net_gex(snap, spot)
        assert GEXCalculator._find_gex_flip(net, spot) in net.index.tolist()

    def test_fully_stabilising_returns_lowest(self):
        net = pd.Series({500.0: 1e6, 505.0: 2e6, 510.0: 3e6})
        assert GEXCalculator._find_gex_flip(net, 507.0) == 500.0

    def test_cumsum_logic_manual(self):
        net = pd.Series({570.0: 5_000_000, 565.0: 3_000_000,
                         560.0: -2_000_000, 555.0: -8_000_000, 550.0: -4_000_000})
        assert GEXCalculator._find_gex_flip(net, 563.0) == 555.0

    def test_flip_in_amplifying_env(self, put_heavy_snap):
        snap, spot = put_heavy_snap
        net  = GEXCalculator._compute_net_gex(snap, spot)
        assert GEXCalculator._find_gex_flip(net, spot) in net.index.tolist()


class TestOCCCodeGeneration:

    def test_covers_30_pct_range(self):
        codes   = GEXCalculator._generate_occ_codes("SPY", "260320", 500.0)
        strikes = sorted({int(c[-8:]) / 1000 for c in codes if "260320C" in c})
        assert min(strikes) <= 351 and max(strikes) >= 649

    def test_occ_format(self):
        codes   = GEXCalculator._generate_occ_codes("SPY", "260320", 500.0)
        pattern = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")
        for code in codes[:20]:
            assert pattern.match(code)

    def test_equal_calls_and_puts(self):
        codes = GEXCalculator._generate_occ_codes("SPY", "260320", 500.0)
        assert len([c for c in codes if "C" in c[10]]) == len([c for c in codes if "P" in c[10]])

    def test_increment_1_below_200(self):
        codes   = GEXCalculator._generate_occ_codes("TEST", "260320", 150.0)
        strikes = sorted({int(c[-8:]) / 1000 for c in codes if "260320C" in c})
        diffs   = [round(strikes[i+1] - strikes[i], 4) for i in range(len(strikes)-1)]
        assert all(abs(d - 1.0) < 0.001 for d in diffs)

    def test_increment_5_spy_range(self):
        codes   = GEXCalculator._generate_occ_codes("SPY", "260320", 562.0)
        strikes = sorted({int(c[-8:]) / 1000 for c in codes if "260320C" in c})
        diffs   = [round(strikes[i+1] - strikes[i], 4) for i in range(len(strikes)-1)]
        assert all(abs(d - 5.0) < 0.001 for d in diffs)

    def test_increment_10_above_1000(self):
        codes   = GEXCalculator._generate_occ_codes("NVDA", "260320", 1200.0)
        strikes = sorted({int(c[-8:]) / 1000 for c in codes if "260320C" in c})
        diffs   = [round(strikes[i+1] - strikes[i], 4) for i in range(len(strikes)-1)]
        assert all(abs(d - 10.0) < 0.001 for d in diffs)


class TestProximityCheck:

    def _c(self, wall, flip, spot):
        c = GEXCalculator({"scalp": {"gex": {"proximity_pct": 0.003}}})
        c._cache["SPY"] = {"gamma_wall": wall, "gex_flip": flip, "spot": spot, "error": ""}
        return c

    def test_at_wall(self):
        assert self._c(560.0, 545.0, 562.0).is_near_gex_level("SPY", 560.0)["near_wall"]

    def test_within_tolerance(self):
        assert self._c(560.0, 545.0, 562.0).is_near_gex_level("SPY", 561.5)["near_wall"]

    def test_outside_tolerance(self):
        assert not self._c(560.0, 545.0, 562.0).is_near_gex_level("SPY", 557.5)["near_wall"]

    def test_above_wall_side(self):
        assert self._c(560.0, 545.0, 562.0).is_near_gex_level("SPY", 560.5)["side"] == "above_wall"

    def test_no_cache_safe_defaults(self, calc):
        r = calc.is_near_gex_level("SPY", 562.0)
        assert r["near_wall"] is False and r["wall_dist_pct"] == 999.0


class TestCacheAndRefresh:

    def test_no_cache_stale(self, calc):
        assert calc.should_refresh("SPY")

    def test_get_cached_none_when_empty(self, calc):
        assert calc.get_cached("SPY") is None

    def test_get_cached_returns_stored(self, calc):
        fake = {"gamma_wall": 560.0, "error": ""}
        calc._cache["SPY"] = fake
        assert calc.get_cached("SPY") == fake

    def test_before_first_refresh_not_stale(self, calc):
        from zoneinfo import ZoneInfo
        calc._cache["SPY"] = {"computed_at": time.time(), "error": ""}
        et = datetime.now(ZoneInfo("America/New_York")).replace(hour=8, minute=0)
        assert not calc.should_refresh("SPY", et)


class TestFrontExpirySelect:

    def test_returns_nearest_future(self):
        today = date.today()
        exp = [(today - timedelta(1)).isoformat(),
               (today + timedelta(2)).isoformat(),
               (today + timedelta(9)).isoformat()]
        assert GEXCalculator._select_front_expiry(exp) == exp[1]

    def test_includes_today(self):
        today  = date.today().isoformat()
        future = (date.today() + timedelta(7)).isoformat()
        assert GEXCalculator._select_front_expiry([today, future]) == today

    def test_raises_when_all_past(self):
        past = [(date.today() - timedelta(i)).isoformat() for i in range(1, 4)]
        with pytest.raises(ValueError, match="No valid"):
            GEXCalculator._select_front_expiry(past)


class TestErrorResult:

    def test_structure(self):
        r = GEXCalculator._error_result("SPY", "err")
        assert {"symbol","gamma_wall","gex_flip","is_stabilising",
                "net_gex_series","total_net_gex","spot","expiry",
                "computed_at","error"}.issubset(r.keys())

    def test_error_field(self):
        assert GEXCalculator._error_result("SPY", "my error")["error"] == "my error"

    def test_conservative_default(self):
        assert GEXCalculator._error_result("SPY", "x")["is_stabilising"] is True


# ── Group B — Live IBKR ───────────────────────────────────────────────────────

class TestLiveGEXCompute:

    @pytest.fixture(scope="class")
    def live_client(self):
        """
        Uses IBKRClient from ibkr-connector package.
        client_id=3: options bot=1, test scripts=2, scalp bot=3.

        asyncio fix: pytest on Python 3.13 has no current event loop by
        default. ib_insync's qualifyContracts() calls asyncio.get_event_loop()
        internally — without a running loop, it returns silently with conId=0,
        causing reqSecDefOptParams to return empty expirations.
        Solution: set a new event loop before creating IBKRClient so
        asyncio.get_event_loop() always returns a valid loop.
        """
        try:
            import asyncio
            import yaml

            # Must be set BEFORE IBKRClient / IB() is created
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            with open("config/config.yaml") as f:
                cfg = yaml.safe_load(f)
            ibkr_cfg = cfg.get("ibkr", {})
            from ibkr_connector import IBKRClient
            client = IBKRClient(
                host=      ibkr_cfg.get("host", "127.0.0.1"),
                port=      ibkr_cfg.get("port", 7496),
                client_id= 3,
                account=   ibkr_cfg.get("account", ""),
                mode=      cfg.get("mode", "live"),
            )
            client.connect()
            if not client.is_connected():
                pytest.skip("TWS not connected — skipping Group B live tests")
            yield client
            client.disconnect()
        except Exception as e:
            pytest.skip(f"IBKR unavailable: {e}")

    def test_spy_wall_valid(self, live_client):
        result = GEXCalculator({}).compute("SPY", live_client)
        assert result["error"] == "", result["error"]
        assert result["gamma_wall"] > 0
        assert result["data_source"] == "ibkr_snapshot"
        print(f"\nSPY wall=${result['gamma_wall']:.2f} flip=${result['gex_flip']:.2f} "
              f"stabilising={result['is_stabilising']} spot=${result['spot']:.2f}")

    def test_qqq_sufficient_strikes(self, live_client):
        result = GEXCalculator({}).compute("QQQ", live_client)
        assert result["error"] == "", result["error"]
        n = len(result["net_gex_series"])
        assert n >= 10, f"Only {n} strikes with valid gamma+OI from snapshot"

    def test_cached_after_compute(self, live_client):
        calc = GEXCalculator({})
        calc.compute("SPY", live_client)
        assert calc.get_cached("SPY") is not None


# ── Group C — Edge cases ──────────────────────────────────────────────────────

class TestColumnNormalisation:

    def test_standard_columns(self):
        snap = pd.DataFrame([{"option_type": "C", "strike_price": 100.0, "option_gamma": 0.05, "option_open_interest": 1000}])
        assert not GEXCalculator._compute_net_gex(snap, 100.0).empty

    def test_short_column_names(self):
        snap = pd.DataFrame([{"right": "C", "strike": 100.0, "gamma": 0.05, "oi": 1000}])
        assert not GEXCalculator._compute_net_gex(snap, 100.0).empty

    def test_call_from_occ_code(self):
        snap = pd.DataFrame([{"code": "SPY260320C00560000", "strike_price": 560.0, "option_gamma": 0.05, "option_open_interest": 1000}])
        net = GEXCalculator._compute_net_gex(snap, 560.0)
        assert not net.empty and net.loc[560.0] > 0

    def test_put_from_occ_code(self):
        snap = pd.DataFrame([{"code": "SPY260320P00560000", "strike_price": 560.0, "option_gamma": 0.05, "option_open_interest": 1000}])
        net = GEXCalculator._compute_net_gex(snap, 560.0)
        assert not net.empty and net.loc[560.0] < 0


class TestMissingColumns:

    def test_raises_missing_gamma(self):
        snap = pd.DataFrame([{"option_type": "C", "strike_price": 100.0, "option_open_interest": 1000}])
        with pytest.raises(ValueError, match="missing required columns"):
            GEXCalculator._compute_net_gex(snap, 100.0)

    def test_empty_when_all_filtered(self):
        snap = pd.DataFrame([{"option_type": "C", "strike_price": 100.0, "option_gamma": 0.0, "option_open_interest": 0}])
        assert GEXCalculator._compute_net_gex(snap, 100.0).empty


class TestHasIBConnection:

    def test_ibkr_connector(self):
        mock = MagicMock(); mock.isConnected.return_value = True
        client = MagicMock(); client._ib = mock
        assert GEXCalculator._has_ib_connection(client)

    def test_ibkr_client_package(self):
        mock = MagicMock(); mock.isConnected.return_value = True
        client = MagicMock(spec=[]); client.ib = mock
        assert GEXCalculator._has_ib_connection(client)

    def test_no_ib_attr(self):
        assert not GEXCalculator._has_ib_connection(MagicMock(spec=[]))


class TestSymbolNormalisation:

    def test_strips_us_prefix(self, calc):
        client = MagicMock()
        client.get_spot_price.return_value = 100.0
        client.get_option_expiries.return_value = [(date.today() + timedelta(3)).isoformat()]
        client.get_option_snapshot.return_value = pd.DataFrame()
        assert calc.compute("US.SPY", client)["symbol"] == "SPY"

    def test_uppercase(self, calc):
        client = MagicMock()
        client.get_spot_price.return_value = 100.0
        client.get_option_expiries.return_value = [(date.today() + timedelta(3)).isoformat()]
        client.get_option_snapshot.return_value = pd.DataFrame()
        assert calc.compute("spy", client)["symbol"] == "SPY"


class TestZeroFiltering:

    def test_all_zero_gamma_empty(self):
        snap = pd.DataFrame([{"option_type": "C", "strike_price": s, "option_gamma": 0.0, "option_open_interest": 1000} for s in [100.0, 105.0]])
        assert GEXCalculator._compute_net_gex(snap, 105.0).empty

    def test_all_zero_oi_empty(self):
        snap = pd.DataFrame([{"option_type": "C", "strike_price": s, "option_gamma": 0.05, "option_open_interest": 0} for s in [100.0, 105.0]])
        assert GEXCalculator._compute_net_gex(snap, 105.0).empty
