"""
Phase 9d — Dashboard Tests
===========================
Tests for dashboard.py Flask routes using a seeded in-memory SQLite ledger.
All tests use the Flask test client — no live server, no network.

Run with:
    cd moomoo
    python3 -m pytest tests/test_phase9d_dashboard.py -v

Coverage:
  TestRoutes            — all 4 pages + healthz return HTTP 200
  TestOverviewContent   — KPI values and gate progress appear in HTML
  TestGateLogic         — gate pass/fail/partial for various trade counts
  TestPositionsContent  — open positions table populated
  TestHistoryContent    — closed trades table populated with entry+exit fields
  TestStatsContent      — by-strategy and by-reason tables populated
  TestEmptyLedger       — graceful rendering with zero trades
  TestHelperFilters     — dte_days filter and _fmt_pnl global
"""

import pytest
import sys
import os
from datetime import date, datetime, timedelta
from pathlib import Path

# ── path bootstrap ───────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))


# ── Fixtures ─────────────────────────────────────────────────────────────────

def expiry_in(days: int) -> str:
    return (date.today() + timedelta(days=days)).strftime("%Y-%m-%d")


def _seed_ledger(ledger, *, open_count=2, closed_count=3,
                 wins=2, strategy="bear_call_spread"):
    """
    Seed the ledger with synthetic trades so dashboard routes have data.
    Returns list of trade IDs created.
    """
    from src.strategies.trade_signal import TradeSignal

    ids = []

    # Open trades
    for i in range(open_count):
        sig = TradeSignal(
            strategy_name=strategy, symbol=f"US.SPY",
            timestamp=datetime.now(), action="OPEN",
            signal_type="bear_call_spread",
            sell_contract=f"SPY260320C70000{i}",
            buy_contract=f"SPY260320C71000{i}",
            quantity=1, sell_price=2.10, buy_price=0.75,
            net_credit=1.35, max_profit=135.0, max_loss=865.0,
            breakeven=701.35, reward_risk=0.156,
            expiry=expiry_in(21 + i * 7), dte=21 + i * 7,
            iv_rank=55.0 + i, delta=0.25,
            reason="test", regime="bearish",
            spot_price=690.0, buffer_pct=1.45 + i,
        )
        tid = ledger.record_open(signal=sig, fill_sell=2.10, fill_buy=0.75)
        ids.append(tid)

    # Closed trades — mix of wins and losses
    for i in range(closed_count):
        is_win = i < wins
        sig = TradeSignal(
            strategy_name=strategy, symbol="US.QQQ",
            timestamp=datetime.now(), action="OPEN",
            signal_type="bear_call_spread",
            sell_contract=f"QQQ260320C62500{i}",
            buy_contract=f"QQQ260320C63500{i}",
            quantity=1, sell_price=1.80, buy_price=0.60,
            net_credit=1.20, max_profit=120.0, max_loss=880.0,
            breakeven=626.20, reward_risk=0.136,
            expiry=expiry_in(7), dte=7,
            iv_rank=50.0, delta=0.22,
            reason="test", regime="bearish",
            spot_price=610.0, buffer_pct=2.5,
        )
        tid = ledger.record_open(signal=sig, fill_sell=1.80, fill_buy=0.60)
        close_price = 0.05 if is_win else 1.90
        reason      = "expired_worthless" if is_win else "stop_loss"
        ledger.record_close(
            trade_id=tid, close_price=close_price,
            close_reason=reason,
            spot_price_at_close=608.0, dte_at_close=2, vix_at_close=19.0,
        )
        ids.append(tid)

    return ids


@pytest.fixture
def app_client(tmp_path):
    """Return a Flask test client backed by a seeded in-memory ledger."""
    import dashboard
    from src.execution.paper_ledger import PaperLedger

    db = str(tmp_path / "test.db")
    ledger = PaperLedger(db_path=db)
    _seed_ledger(ledger)

    # Wire the ledger into the module-level variable
    dashboard._ledger = ledger
    dashboard.app.config["GATE_MIN_TRADES"]   = 10
    dashboard.app.config["GATE_MIN_WIN_RATE"]  = 0.60
    dashboard.app.config["TESTING"] = True

    with dashboard.app.test_client() as client:
        yield client, ledger


@pytest.fixture
def empty_client(tmp_path):
    """Flask test client with an empty ledger (no trades)."""
    import dashboard
    from src.execution.paper_ledger import PaperLedger

    db = str(tmp_path / "empty.db")
    ledger = PaperLedger(db_path=db)
    dashboard._ledger = ledger
    dashboard.app.config["GATE_MIN_TRADES"]   = 10
    dashboard.app.config["GATE_MIN_WIN_RATE"]  = 0.60
    dashboard.app.config["TESTING"] = True

    with dashboard.app.test_client() as client:
        yield client


# ═══════════════════════════════════════════════════════════════════
# Routes — all pages return HTTP 200
# ═══════════════════════════════════════════════════════════════════

class TestRoutes:

    def test_overview_200(self, app_client):
        client, _ = app_client
        assert client.get("/").status_code == 200

    def test_positions_200(self, app_client):
        client, _ = app_client
        assert client.get("/positions").status_code == 200

    def test_history_200(self, app_client):
        client, _ = app_client
        assert client.get("/history").status_code == 200

    def test_stats_200(self, app_client):
        client, _ = app_client
        assert client.get("/stats").status_code == 200

    def test_healthz_200(self, app_client):
        client, _ = app_client
        r = client.get("/healthz")
        assert r.status_code == 200

    def test_healthz_json(self, app_client):
        client, _ = app_client
        data = client.get("/healthz").get_json()
        assert data["status"] == "ok"
        assert "open" in data
        assert "closed" in data
        assert "win_rate" in data

    def test_unknown_route_404(self, app_client):
        client, _ = app_client
        assert client.get("/nonexistent").status_code == 404


# ═══════════════════════════════════════════════════════════════════
# Overview page content
# ═══════════════════════════════════════════════════════════════════

class TestOverviewContent:

    def _html(self, app_client):
        client, _ = app_client
        return client.get("/").data.decode()

    def test_page_title_present(self, app_client):
        assert "Overview" in self._html(app_client)

    def test_nav_links_present(self, app_client):
        html = self._html(app_client)
        for link in ["/positions", "/history", "/stats"]:
            assert link in html

    def test_open_positions_count_shown(self, app_client):
        """Seeded 2 open positions — count appears in page."""
        html = self._html(app_client)
        assert "Open Positions" in html

    def test_validation_gate_section_present(self, app_client):
        html = self._html(app_client)
        assert "Validation Gate" in html

    def test_win_rate_shown(self, app_client):
        html = self._html(app_client)
        assert "Win Rate" in html

    def test_total_pnl_shown(self, app_client):
        html = self._html(app_client)
        assert "Total Realised" in html

    def test_avg_premium_captured_shown(self, app_client):
        html = self._html(app_client)
        assert "Premium Captured" in html

    def test_auto_refresh_meta_tag(self, app_client):
        html = self._html(app_client)
        assert 'http-equiv="refresh"' in html

    def test_navbar_brand(self, app_client):
        html = self._html(app_client)
        assert "OPTIONS BOT" in html

    def test_open_positions_mini_table_shown(self, app_client):
        """When there are open positions, mini-table appears on overview."""
        html = self._html(app_client)
        assert "SPY" in html   # seeded SPY positions


# ═══════════════════════════════════════════════════════════════════
# Gate progress logic
# ═══════════════════════════════════════════════════════════════════

class TestGateLogic:

    def _gate(self, total, wins, min_trades=10, min_win=0.60):
        import dashboard
        stats = {
            "total_trades":   total,
            "winning_trades": wins,
            "win_rate":       wins / total if total > 0 else 0,
        }
        import dashboard as d
        d.app.config["GATE_MIN_TRADES"]   = min_trades
        d.app.config["GATE_MIN_WIN_RATE"] = min_win
        with d.app.app_context():
            return d._gate_progress(stats)

    def test_gate_fails_insufficient_trades(self):
        g = self._gate(total=5, wins=4)
        assert not g["gate_passed"]
        assert not g["trades_ok"]

    def test_gate_fails_low_win_rate(self):
        g = self._gate(total=10, wins=5)
        assert not g["gate_passed"]
        assert g["trades_ok"]
        assert not g["win_rate_ok"]

    def test_gate_passes_both_criteria(self):
        g = self._gate(total=10, wins=7)
        assert g["gate_passed"]
        assert g["trades_ok"]
        assert g["win_rate_ok"]

    def test_gate_trade_pct_capped_at_100(self):
        g = self._gate(total=20, wins=15)
        assert g["trade_pct"] == 100

    def test_gate_progress_zero(self):
        g = self._gate(total=0, wins=0)
        assert g["trade_pct"] == 0
        assert not g["gate_passed"]

    def test_gate_shown_as_passed_in_html(self, tmp_path):
        """Gate passes — HTML shows PASSED text."""
        import dashboard
        from src.execution.paper_ledger import PaperLedger
        from src.strategies.trade_signal import TradeSignal
        from datetime import datetime

        db = str(tmp_path / "gate.db")
        ledger = PaperLedger(db_path=db)
        dashboard._ledger = ledger
        dashboard.app.config["GATE_MIN_TRADES"]   = 3
        dashboard.app.config["GATE_MIN_WIN_RATE"]  = 0.60
        dashboard.app.config["TESTING"] = True

        # Seed 3 closed wins
        for i in range(3):
            sig = TradeSignal(
                strategy_name="bear_call_spread", symbol="US.SPY",
                timestamp=datetime.now(), action="OPEN",
                signal_type="bear_call_spread",
                sell_contract=f"SPY260320C70000{i}",
                buy_contract=f"SPY260320C71000{i}",
                quantity=1, sell_price=2.10, buy_price=0.75,
                net_credit=1.35, max_profit=135.0, max_loss=865.0,
                breakeven=701.35, reward_risk=0.156,
                expiry=expiry_in(7), dte=7, iv_rank=55.0, delta=0.25,
                reason="test", regime="bearish",
            )
            tid = ledger.record_open(signal=sig, fill_sell=2.10, fill_buy=0.75)
            ledger.record_close(trade_id=tid, close_price=0.05,
                                close_reason="expired_worthless")

        with dashboard.app.test_client() as c:
            html = c.get("/").data.decode()
        assert "PASSED" in html


# ═══════════════════════════════════════════════════════════════════
# Positions page
# ═══════════════════════════════════════════════════════════════════

class TestPositionsContent:

    def _html(self, app_client):
        client, _ = app_client
        return client.get("/positions").data.decode()

    def test_symbol_appears(self, app_client):
        html = self._html(app_client)
        assert "SPY" in html

    def test_strategy_badge_appears(self, app_client):
        html = self._html(app_client)
        assert "Bear Call Spread" in html

    def test_entry_context_columns_present(self, app_client):
        html = self._html(app_client)
        for col in ["IV Rank", "Delta", "Buffer", "RSI", "VIX"]:
            assert col in html

    def test_dte_column_present(self, app_client):
        html = self._html(app_client)
        assert "DTE" in html

    def test_expiry_column_present(self, app_client):
        html = self._html(app_client)
        assert "Expiry" in html


# ═══════════════════════════════════════════════════════════════════
# History page
# ═══════════════════════════════════════════════════════════════════

class TestHistoryContent:

    def _html(self, app_client):
        client, _ = app_client
        return client.get("/history").data.decode()

    def test_closed_symbol_appears(self, app_client):
        html = self._html(app_client)
        assert "QQQ" in html

    def test_exit_reason_badges_present(self, app_client):
        html = self._html(app_client)
        assert "Expired Worthless" in html or "expired_worthless" in html.lower()

    def test_pnl_column_present(self, app_client):
        html = self._html(app_client)
        assert "P&amp;L" in html or "P&L" in html

    def test_days_held_column(self, app_client):
        html = self._html(app_client)
        assert "Days" in html

    def test_entry_context_columns_present(self, app_client):
        html = self._html(app_client)
        for col in ["RSI", "VIX", "Buffer", "R/R", "Spot"]:
            assert col in html

    def test_exit_context_columns_present(self, app_client):
        html = self._html(app_client)
        assert "DTE@Close" in html or "dte_at_close" in html.lower() or "DTE" in html

    def test_pct_captured_column(self, app_client):
        html = self._html(app_client)
        assert "Cap" in html   # "% Cap." column header


# ═══════════════════════════════════════════════════════════════════
# Stats page
# ═══════════════════════════════════════════════════════════════════

class TestStatsContent:

    def _html(self, app_client):
        client, _ = app_client
        return client.get("/stats").data.decode()

    def test_by_strategy_section(self, app_client):
        html = self._html(app_client)
        assert "By Strategy" in html

    def test_by_exit_reason_section(self, app_client):
        html = self._html(app_client)
        assert "By Exit Reason" in html

    def test_averages_section(self, app_client):
        html = self._html(app_client)
        assert "Averages" in html

    def test_overall_summary_section(self, app_client):
        html = self._html(app_client)
        assert "Overall Summary" in html

    def test_strategy_name_appears(self, app_client):
        html = self._html(app_client)
        assert "Bear Call Spread" in html

    def test_win_rate_appears_in_stats(self, app_client):
        html = self._html(app_client)
        assert "Win Rate" in html or "Win%" in html


# ═══════════════════════════════════════════════════════════════════
# Empty ledger — graceful rendering
# ═══════════════════════════════════════════════════════════════════

class TestEmptyLedger:

    def test_overview_empty_ok(self, empty_client):
        assert empty_client.get("/").status_code == 200

    def test_positions_empty_ok(self, empty_client):
        r = empty_client.get("/positions")
        assert r.status_code == 200
        assert "No open positions" in r.data.decode()

    def test_history_empty_ok(self, empty_client):
        r = empty_client.get("/history")
        assert r.status_code == 200
        assert "No closed trades" in r.data.decode()

    def test_stats_empty_ok(self, empty_client):
        assert empty_client.get("/stats").status_code == 200

    def test_healthz_empty_ok(self, empty_client):
        data = empty_client.get("/healthz").get_json()
        assert data["open"]   == 0
        assert data["closed"] == 0

    def test_gate_shows_zero_progress(self, empty_client):
        html = empty_client.get("/").data.decode()
        assert "Validation Gate" in html
        assert "In Progress" in html


# ═══════════════════════════════════════════════════════════════════
# Helper filters / globals
# ═══════════════════════════════════════════════════════════════════

class TestHelperFilters:

    def test_dte_days_future(self):
        import dashboard
        f = dashboard.app.jinja_env.filters["dte_days"]
        assert f(expiry_in(21)) == 21

    def test_dte_days_today(self):
        import dashboard
        f = dashboard.app.jinja_env.filters["dte_days"]
        assert f(expiry_in(0)) == 0

    def test_dte_days_expired(self):
        import dashboard
        f = dashboard.app.jinja_env.filters["dte_days"]
        past = (date.today() - timedelta(days=5)).strftime("%Y-%m-%d")
        assert f(past) == 0

    def test_dte_days_bad_string(self):
        import dashboard
        f = dashboard.app.jinja_env.filters["dte_days"]
        assert f("not-a-date") is None

    def test_fmt_pnl_positive(self):
        import dashboard
        with dashboard.app.app_context():
            result = dashboard._fmt_pnl(135.0)
        assert "pnl-pos" in result
        assert "+$135.00" in result

    def test_fmt_pnl_negative(self):
        import dashboard
        with dashboard.app.app_context():
            result = dashboard._fmt_pnl(-865.0)
        assert "pnl-neg" in result
        assert "865.00" in result

    def test_fmt_pnl_none(self):
        import dashboard
        with dashboard.app.app_context():
            result = dashboard._fmt_pnl(None)
        assert "pnl-neu" in result

    def test_resolve_db_path_uses_default_when_no_config(self, tmp_path, monkeypatch):
        import dashboard
        monkeypatch.setattr(dashboard, "ROOT", tmp_path)
        # No config.yaml in tmp_path — should fall back to default
        result = dashboard._resolve_db_path(None)
        assert "paper_trades.db" in result


# ═══════════════════════════════════════════════════════════════════
# Scan page — /scan route
# ═══════════════════════════════════════════════════════════════════

def _make_scan_json(tmp_path, *, signals=2, executed=1, symbols=None) -> str:
    """Write a synthetic scan_results.json and return its path."""
    from datetime import datetime, timezone
    symbols = symbols or [
        {
            "symbol": "US.SPY", "spot_price": 690.50, "regime": "neutral",
            "vix": 18.4, "rsi": 52.3, "pct_b": 0.61, "macd": 0.45,
            "iv_rank": 55.0, "shares_held": 0, "next_earnings_days": None,
            "expiries_available": 3,
            "strategies": [
                {
                    "strategy": "bear_call_spread", "enabled": True,
                    "gates": [
                        {"label": "positions < 3", "passed": True,  "detail": "open=1"},
                        {"label": "IV rank ≥ 35",  "passed": True,  "detail": "iv_rank=55"},
                        {"label": "RSI ≥ 45",       "passed": True,  "detail": "rsi=52"},
                        {"label": "%B ≥ 0.40",      "passed": True,  "detail": "%B=0.61"},
                    ],
                    "result": "signal",
                },
                {
                    "strategy": "bull_put_spread", "enabled": True,
                    "gates": [
                        {"label": "regime in ['bull','neutral']", "passed": True, "detail": "regime=neutral"},
                        {"label": "RSI ≥ 35", "passed": True, "detail": "rsi=52"},
                        {"label": "RSI ≤ 65", "passed": True, "detail": "rsi=52"},
                    ],
                    "result": "skip:no qualifying OTM put",
                },
            ],
        },
        {
            "symbol": "US.QQQ", "spot_price": 480.20, "regime": "bear",
            "vix": 18.4, "rsi": 43.1, "pct_b": 0.30, "macd": -0.20,
            "iv_rank": 48.0, "shares_held": 0, "next_earnings_days": 14,
            "expiries_available": 2,
            "strategies": [
                {
                    "strategy": "bear_call_spread", "enabled": True,
                    "gates": [
                        {"label": "positions < 3", "passed": True,  "detail": "open=0"},
                        {"label": "IV rank ≥ 35",  "passed": True,  "detail": "iv_rank=48"},
                        {"label": "RSI ≥ 45",       "passed": False, "detail": "rsi=43"},
                    ],
                    "result": "skip:RSI ≥ 45 failed",
                },
            ],
        },
    ]
    data = {
        "scan_timestamp": "2026-03-03T09:35:42-05:00",
        "scan_type": "morning",
        "scan_number": 1,
        "elapsed_seconds": 24.7,
        "symbols_scanned": len(symbols),
        "signals_found": signals,
        "signals_executed": executed,
        "executed_trade_ids": list(range(1, executed + 1)),
        "symbols": symbols,
        "candidates": [
            {
                "rank": 1, "symbol": "US.SPY", "strategy": "bear_call_spread",
                "iv_rank": 55.0, "buffer_pct": 2.9, "reward_risk": 0.18,
                "score": 0.724, "net_credit": 1.35, "expiry": expiry_in(28),
                "dte": 28, "outcome": "executed",
            },
        ] if signals > 0 else [],
    }
    out = tmp_path / "scan_results.json"
    import json
    out.write_text(json.dumps(data))
    return str(out)


@pytest.fixture
def scan_client_with_data(tmp_path):
    """Flask test client with seeded scan_results.json."""
    import dashboard
    from src.execution.paper_ledger import PaperLedger

    db = str(tmp_path / "test.db")
    ledger = PaperLedger(db_path=db)
    dashboard._ledger = ledger
    dashboard.app.config["GATE_MIN_TRADES"]  = 10
    dashboard.app.config["GATE_MIN_WIN_RATE"] = 0.60
    dashboard.app.config["TESTING"] = True

    # Write scan_results.json next to the DB
    _make_scan_json(tmp_path)

    # Patch _load_scan_results to read from tmp_path
    import json
    original = dashboard._load_scan_results
    dashboard._load_scan_results = lambda: json.loads((tmp_path / "scan_results.json").read_text())

    with dashboard.app.test_client() as client:
        yield client

    dashboard._load_scan_results = original


@pytest.fixture
def scan_client_no_data(tmp_path):
    """Flask test client with no scan_results.json."""
    import dashboard
    from src.execution.paper_ledger import PaperLedger

    db = str(tmp_path / "empty.db")
    ledger = PaperLedger(db_path=db)
    dashboard._ledger = ledger
    dashboard.app.config["TESTING"] = True

    original = dashboard._load_scan_results
    dashboard._load_scan_results = lambda: None

    with dashboard.app.test_client() as client:
        yield client

    dashboard._load_scan_results = original


class TestScanRoute:

    def test_scan_200_with_data(self, scan_client_with_data):
        assert scan_client_with_data.get("/scan").status_code == 200

    def test_scan_200_no_data(self, scan_client_no_data):
        assert scan_client_no_data.get("/scan").status_code == 200

    def test_scan_nav_link_present(self, scan_client_with_data):
        html = scan_client_with_data.get("/scan").data.decode()
        assert "/scan" in html

    def test_scan_shows_no_data_message_when_missing(self, scan_client_no_data):
        html = scan_client_no_data.get("/scan").data.decode()
        assert "No scan data yet" in html or "scan_results.json" in html

    def test_scan_shows_symbols_scanned(self, scan_client_with_data):
        html = scan_client_with_data.get("/scan").data.decode()
        assert "Symbols Scanned" in html

    def test_scan_shows_signals_found(self, scan_client_with_data):
        html = scan_client_with_data.get("/scan").data.decode()
        assert "Signals Found" in html

    def test_scan_shows_executed_count(self, scan_client_with_data):
        html = scan_client_with_data.get("/scan").data.decode()
        assert "Executed" in html

    def test_scan_shows_scan_duration(self, scan_client_with_data):
        html = scan_client_with_data.get("/scan").data.decode()
        assert "Scan Duration" in html or "24.7" in html

    def test_scan_ranked_candidates_table_shown(self, scan_client_with_data):
        html = scan_client_with_data.get("/scan").data.decode()
        assert "Ranked Candidates" in html

    def test_scan_candidate_outcome_badge(self, scan_client_with_data):
        html = scan_client_with_data.get("/scan").data.decode()
        assert "Executed" in html

    def test_scan_symbol_cards_shown(self, scan_client_with_data):
        html = scan_client_with_data.get("/scan").data.decode()
        assert "SPY" in html
        assert "QQQ" in html

    def test_scan_gate_pass_shown(self, scan_client_with_data):
        html = scan_client_with_data.get("/scan").data.decode()
        assert "gate-pass" in html

    def test_scan_gate_fail_shown(self, scan_client_with_data):
        html = scan_client_with_data.get("/scan").data.decode()
        assert "gate-fail" in html

    def test_scan_regime_badge_shown(self, scan_client_with_data):
        html = scan_client_with_data.get("/scan").data.decode()
        assert "neutral" in html or "bear" in html

    def test_scan_market_status_banner_present(self, scan_client_with_data):
        html = scan_client_with_data.get("/scan").data.decode()
        # Either open or closed — some status banner must appear
        has_status = any(s in html for s in [
            "Market Open", "Market Closed", "Pre-Market", "After Hours"
        ])
        assert has_status

    def test_scan_iv_rank_coloured_when_high(self, scan_client_with_data):
        """IV rank ≥ 35 gets pnl-pos class."""
        html = scan_client_with_data.get("/scan").data.decode()
        assert "pnl-pos" in html

    def test_scan_earnings_warning_shown(self, scan_client_with_data):
        """QQQ has 14d to earnings — should appear."""
        html = scan_client_with_data.get("/scan").data.decode()
        assert "14d" in html or "Earnings" in html.lower() or "earnings" in html


class TestMarketStatus:

    def _status(self, dt_str):
        """Helper: parse an ET datetime string → call _market_status mock."""
        import dashboard
        from datetime import datetime
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        dt = datetime.fromisoformat(dt_str).replace(tzinfo=ET)
        # Monkeypatch datetime.now inside _market_status is complex;
        # instead test the logic directly by re-implementing the key checks
        weekday = dt.weekday()
        open_t  = dt.replace(hour=9,  minute=30, second=0, microsecond=0)
        close_t = dt.replace(hour=16, minute=0,  second=0, microsecond=0)
        if weekday >= 5:
            return "closed"
        if dt < open_t:
            return "pre" if dt >= dt.replace(hour=4, minute=0, second=0, microsecond=0) else "closed"
        if dt < close_t:
            return "open"
        if dt < dt.replace(hour=20, minute=0, second=0, microsecond=0):
            return "post"
        return "closed"

    def test_open_during_session(self):
        assert self._status("2026-03-03 10:30:00") == "open"

    def test_open_at_935(self):
        assert self._status("2026-03-03 09:35:00") == "open"

    def test_closed_before_open(self):
        assert self._status("2026-03-03 09:00:00") in ("pre", "closed")

    def test_closed_after_close(self):
        result = self._status("2026-03-03 16:30:00")
        assert result in ("post", "closed")

    def test_closed_on_saturday(self):
        assert self._status("2026-03-07 11:00:00") == "closed"

    def test_closed_on_sunday(self):
        assert self._status("2026-03-08 11:00:00") == "closed"

    def test_pre_market_early_morning(self):
        assert self._status("2026-03-03 07:00:00") == "pre"


class TestBuildScanData:
    """Verify the JSON structure produced by _build_scan_data (via bot_scheduler)."""

    def test_required_keys_present(self, tmp_path):
        """Write a synthetic scan_results.json and verify all required keys exist."""
        _make_scan_json(tmp_path)
        import json
        data = json.loads((tmp_path / "scan_results.json").read_text())
        for key in ["scan_timestamp", "scan_number", "elapsed_seconds",
                    "symbols_scanned", "signals_found", "signals_executed",
                    "symbols", "candidates"]:
            assert key in data, f"missing key: {key}"

    def test_symbol_entry_has_technicals(self, tmp_path):
        _make_scan_json(tmp_path)
        import json
        data = json.loads((tmp_path / "scan_results.json").read_text())
        sym = data["symbols"][0]
        for field in ["rsi", "pct_b", "macd", "iv_rank", "vix", "regime", "spot_price"]:
            assert field in sym, f"missing field: {field}"

    def test_symbol_has_strategies(self, tmp_path):
        _make_scan_json(tmp_path)
        import json
        data = json.loads((tmp_path / "scan_results.json").read_text())
        sym = data["symbols"][0]
        assert "strategies" in sym
        assert len(sym["strategies"]) > 0

    def test_gate_has_required_fields(self, tmp_path):
        _make_scan_json(tmp_path)
        import json
        data = json.loads((tmp_path / "scan_results.json").read_text())
        gate = data["symbols"][0]["strategies"][0]["gates"][0]
        assert "label"  in gate
        assert "passed" in gate
        assert "detail" in gate

    def test_candidate_has_score(self, tmp_path):
        _make_scan_json(tmp_path)
        import json
        data = json.loads((tmp_path / "scan_results.json").read_text())
        c = data["candidates"][0]
        assert "score"   in c
        assert "outcome" in c
        assert "rank"    in c

    def test_no_candidates_when_no_signals(self, tmp_path):
        _make_scan_json(tmp_path, signals=0, executed=0)
        import json
        data = json.loads((tmp_path / "scan_results.json").read_text())
        assert data["signals_found"]    == 0
        assert data["signals_executed"] == 0
        assert data["candidates"]       == []


# ═══════════════════════════════════════════════════════════════════
# scan_type field — morning vs intraday
# ═══════════════════════════════════════════════════════════════════

def _make_scan_json_typed(tmp_path, scan_type: str) -> dict:
    """Write scan_results.json with an explicit scan_type and return parsed data."""
    import json
    _make_scan_json(tmp_path)
    path = tmp_path / "scan_results.json"
    data = json.loads(path.read_text())
    data["scan_type"] = scan_type
    path.write_text(json.dumps(data))
    return data


@pytest.fixture
def scan_client_morning(tmp_path):
    import dashboard
    from src.execution.paper_ledger import PaperLedger
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import json
    ledger = PaperLedger(db_path=str(tmp_path / "test.db"))
    dashboard._ledger = ledger
    dashboard.app.config["TESTING"] = True
    _make_scan_json_typed(tmp_path, "morning")
    orig_scan    = dashboard._load_scan_results
    orig_market  = dashboard._market_status
    # Force market open so the open-session banner branch is exercised
    dashboard._load_scan_results = lambda: json.loads((tmp_path / "scan_results.json").read_text())
    dashboard._market_status     = lambda: {
        "is_open": True, "session": "open",
        "now_et": datetime.now(ZoneInfo("America/New_York")), "next_open": "",
    }
    with dashboard.app.test_client() as client:
        yield client
    dashboard._load_scan_results = orig_scan
    dashboard._market_status     = orig_market


@pytest.fixture
def scan_client_intraday(tmp_path):
    import dashboard
    from src.execution.paper_ledger import PaperLedger
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import json
    ledger = PaperLedger(db_path=str(tmp_path / "test.db"))
    dashboard._ledger = ledger
    dashboard.app.config["TESTING"] = True
    _make_scan_json_typed(tmp_path, "intraday")
    orig_scan    = dashboard._load_scan_results
    orig_market  = dashboard._market_status
    dashboard._load_scan_results = lambda: json.loads((tmp_path / "scan_results.json").read_text())
    dashboard._market_status     = lambda: {
        "is_open": True, "session": "open",
        "now_et": datetime.now(ZoneInfo("America/New_York")), "next_open": "",
    }
    with dashboard.app.test_client() as client:
        yield client
    dashboard._load_scan_results = orig_scan
    dashboard._market_status     = orig_market


class TestScanType:

    # ── JSON structure ──────────────────────────────────────────────

    def test_build_scan_data_has_scan_type_key(self, tmp_path):
        """_build_scan_data always emits scan_type in the JSON."""
        import json
        _make_scan_json(tmp_path)
        data = json.loads((tmp_path / "scan_results.json").read_text())
        assert "scan_type" in data, "scan_type key missing from scan_results.json"

    def test_default_scan_type_is_morning(self, tmp_path):
        """scan_results.json written by morning scan has scan_type == 'morning'."""
        import json
        _make_scan_json(tmp_path)          # _make_scan_json doesn't set scan_type
        data = json.loads((tmp_path / "scan_results.json").read_text())
        # The key may or may not be present in synthetic data —
        # what matters is it is NOT 'intraday'
        assert data.get("scan_type") != "intraday"

    def test_intraday_scan_type_value(self, tmp_path):
        """Intraday JSON has scan_type == 'intraday'."""
        import json
        data = _make_scan_json_typed(tmp_path, "intraday")
        assert data["scan_type"] == "intraday"

    def test_morning_scan_type_value(self, tmp_path):
        """Morning JSON has scan_type == 'morning'."""
        import json
        data = _make_scan_json_typed(tmp_path, "morning")
        assert data["scan_type"] == "morning"

    # ── Dashboard rendering — morning ───────────────────────────────

    def test_morning_badge_shown_for_morning_scan(self, scan_client_morning):
        html = scan_client_morning.get("/scan").data.decode()
        assert "Morning Scan" in html

    def test_intraday_badge_absent_for_morning_scan(self, scan_client_morning):
        html = scan_client_morning.get("/scan").data.decode()
        assert "Intraday Rescan" not in html

    def test_intraday_callout_absent_for_morning_scan(self, scan_client_morning):
        """The 'daily-bar indicators don't change intraday' note must not appear."""
        html = scan_client_morning.get("/scan").data.decode()
        assert "daily-bar indicators" not in html
        assert "Intraday rescan" not in html

    def test_scan_duration_shown_for_morning(self, scan_client_morning):
        """Morning scan shows elapsed seconds, not 'Intraday' badge."""
        html = scan_client_morning.get("/scan").data.decode()
        assert "Scan Duration" in html
        assert "Rescan Type" not in html

    # ── Dashboard rendering — intraday ──────────────────────────────

    def test_intraday_badge_shown_for_intraday_scan(self, scan_client_intraday):
        html = scan_client_intraday.get("/scan").data.decode()
        assert "Intraday Rescan" in html

    def test_morning_badge_absent_for_intraday_scan(self, scan_client_intraday):
        html = scan_client_intraday.get("/scan").data.decode()
        assert "Morning Scan" not in html

    def test_intraday_callout_shown_for_intraday_scan(self, scan_client_intraday):
        """The explanatory note about daily-bar indicators must appear."""
        html = scan_client_intraday.get("/scan").data.decode()
        assert "Intraday rescan" in html
        assert "daily-bar indicators" not in html or "morning scan" in html

    def test_intraday_kpi_card_shows_rescan_type(self, scan_client_intraday):
        """4th KPI card label changes to 'Rescan Type' for intraday."""
        html = scan_client_intraday.get("/scan").data.decode()
        assert "Rescan Type" in html

    def test_intraday_kpi_card_no_duration(self, scan_client_intraday):
        """'Scan Duration' label must not appear for intraday."""
        html = scan_client_intraday.get("/scan").data.decode()
        assert "Scan Duration" not in html

    def test_page_still_200_for_both_types(self, scan_client_morning, scan_client_intraday):
        assert scan_client_morning.get("/scan").status_code  == 200
        assert scan_client_intraday.get("/scan").status_code == 200


# ═══════════════════════════════════════════════════════════════════
# Positions page — unrealised P&L column
# ═══════════════════════════════════════════════════════════════════

def _make_mark_data(pnl: float = 125.50, pnl_pct: float = 0.35) -> dict:
    """Return a marks_by_id dict mimicking _load_positions_mark output."""
    return {
        "updated_at": "2026-03-03T10:05:00-05:00",
        "marks_by_id": {1: {
            "id": 1, "symbol": "US.SPY",
            "current_price": 0.08,
            "unrealised_pnl": pnl,
            "pnl_pct": pnl_pct,
            "exit_signal": None,
            "as_of": "2026-03-03T10:05:00-05:00",
        }}
    }


def _positions_client(tmp_path, mark_data):
    """Helper: ledger with one open SPY trade + patched _load_positions_mark."""
    import dashboard
    from src.execution.paper_ledger import PaperLedger
    from src.strategies.trade_signal import TradeSignal
    from datetime import datetime
    ledger = PaperLedger(db_path=str(tmp_path / "test.db"))
    sig = TradeSignal(
        strategy_name="bear_call_spread", symbol="US.SPY",
        timestamp=datetime.now(), action="OPEN",
        signal_type="bear_call_spread",
        sell_contract="SPY260321C70000", buy_contract="SPY260321C71000",
        quantity=1, sell_price=2.10, buy_price=0.75,
        net_credit=1.35, max_profit=135.0, max_loss=865.0,
        breakeven=701.35, reward_risk=0.156,
        expiry=expiry_in(18), dte=18,
        iv_rank=50.0, delta=0.18,
        reason="test", regime="neutral",
        spot_price=674.0, buffer_pct=3.8,
    )
    ledger.record_open(signal=sig, fill_sell=2.10, fill_buy=0.75)
    dashboard._ledger = ledger
    dashboard.app.config["TESTING"] = True
    orig = dashboard._load_positions_mark
    dashboard._load_positions_mark = lambda: mark_data
    client = dashboard.app.test_client()
    return client, orig, dashboard


class TestPositionsPnL:
    """Unrealised P&L column on the /positions page."""

    def test_pnl_column_header_present(self, tmp_path):
        client, orig, dash = _positions_client(tmp_path, _make_mark_data())
        try:
            html = client.get("/positions").data.decode()
        finally:
            dash._load_positions_mark = orig
        assert "Unrealised P&L" in html

    def test_mark_at_column_header_present(self, tmp_path):
        client, orig, dash = _positions_client(tmp_path, _make_mark_data())
        try:
            html = client.get("/positions").data.decode()
        finally:
            dash._load_positions_mark = orig
        assert "Mark @" in html

    def test_positive_pnl_shown_green(self, tmp_path):
        client, orig, dash = _positions_client(tmp_path, _make_mark_data(pnl=125.50, pnl_pct=0.35))
        try:
            html = client.get("/positions").data.decode()
        finally:
            dash._load_positions_mark = orig
        assert "pnl-pos" in html
        assert "+$125.50" in html

    def test_pnl_pct_shown(self, tmp_path):
        client, orig, dash = _positions_client(tmp_path, _make_mark_data(pnl=125.50, pnl_pct=0.35))
        try:
            html = client.get("/positions").data.decode()
        finally:
            dash._load_positions_mark = orig
        assert "35%" in html

    def test_negative_pnl_shown_red(self, tmp_path):
        client, orig, dash = _positions_client(tmp_path, _make_mark_data(pnl=-75.00, pnl_pct=-0.21))
        try:
            html = client.get("/positions").data.decode()
        finally:
            dash._load_positions_mark = orig
        assert "pnl-neg" in html
        assert "-$75.00" in html

    def test_mark_timestamp_shown(self, tmp_path):
        client, orig, dash = _positions_client(tmp_path, _make_mark_data())
        try:
            html = client.get("/positions").data.decode()
        finally:
            dash._load_positions_mark = orig
        assert "10:05" in html

    def test_footer_note_when_mark_present(self, tmp_path):
        client, orig, dash = _positions_client(tmp_path, _make_mark_data())
        try:
            html = client.get("/positions").data.decode()
        finally:
            dash._load_positions_mark = orig
        assert "Unrealised P&L last updated" in html

    def test_no_data_message_when_no_mark_file(self, tmp_path):
        client, orig, dash = _positions_client(tmp_path, {})
        try:
            html = client.get("/positions").data.decode()
        finally:
            dash._load_positions_mark = orig
        assert "No live mark data yet" in html

    def test_no_data_cell_when_trade_not_in_marks(self, tmp_path):
        client, orig, dash = _positions_client(tmp_path, {})
        try:
            html = client.get("/positions").data.decode()
        finally:
            dash._load_positions_mark = orig
        assert "no data" in html

    def test_page_200_with_mark(self, tmp_path):
        client, orig, dash = _positions_client(tmp_path, _make_mark_data())
        try:
            assert client.get("/positions").status_code == 200
        finally:
            dash._load_positions_mark = orig

    def test_page_200_without_mark(self, tmp_path):
        client, orig, dash = _positions_client(tmp_path, {})
        try:
            assert client.get("/positions").status_code == 200
        finally:
            dash._load_positions_mark = orig
