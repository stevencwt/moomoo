#!/usr/bin/env python3
"""
dashboard.py — Paper-Trading Dashboard
=======================================
Single-file Flask app.  Run from the project root:

    python3 dashboard.py                  # port 5000
    python3 dashboard.py --port 8080      # custom port
    python3 dashboard.py --db path/to/paper_trades.db

The app reads data/paper_trades.db (or config.yaml → paper_ledger.db_path)
directly via PaperLedger — no network calls, no live connector needed.

Pages
-----
  /              Overview: P&L summary + validation gate progress
  /positions     Open positions with DTE countdown and unrealised mark
  /history       Closed trade history with entry + exit context
  /stats         Statistics: by strategy, by exit reason, averages

Auto-refreshes every 60 s (configurable via REFRESH_SECS).
"""

import argparse
import os
import sys
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import yaml
from flask import Flask, render_template_string, request

# ── Bootstrap the import path so PaperLedger resolves ────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from src.execution.paper_ledger import PaperLedger  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
REFRESH_SECS = 60          # auto-refresh interval (seconds)
DEFAULT_DB   = "data/paper_trades.db"
DEFAULT_CFG  = "config.yaml"

app = Flask(__name__)
_ledger: Optional[PaperLedger] = None   # wired in main()

# ── Bootstrap 5 base template ────────────────────────────────────────────────
BASE = """
<!doctype html>
<html lang="en" data-bs-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="{{ refresh }}">
  <title>{{ title }} — Options Bot Dashboard</title>
  <link rel="stylesheet"
    href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
  <style>
    body { background:#0d1117; }
    .navbar-brand { font-family:monospace; letter-spacing:2px; }
    .card { background:#161b22; border:1px solid #30363d; }
    .card-header { background:#21262d; border-bottom:1px solid #30363d; }
    .table { font-size:.87rem; }
    .table-dark { --bs-table-bg:#161b22; }
    .badge-strategy { font-size:.75rem; text-transform:uppercase; letter-spacing:1px; }
    .stat-number   { font-size:1.9rem; font-weight:700; font-family:monospace; }
    .stat-label    { font-size:.78rem; text-transform:uppercase;
                     letter-spacing:.08em; color:#8b949e; }
    .gate-pass { color:#3fb950; }
    .gate-fail { color:#f85149; }
    .gate-warn { color:#d29922; }
    .dte-urgent  { color:#f85149; font-weight:700; }
    .dte-soon    { color:#d29922; }
    .dte-ok      { color:#3fb950; }
    .pnl-pos { color:#3fb950; }
    .pnl-neg { color:#f85149; }
    .pnl-neu { color:#8b949e; }
    .refresh-note { font-size:.75rem; color:#484f58; }
  </style>
</head>
<body>
<nav class="navbar navbar-dark" style="background:#161b22;border-bottom:1px solid #30363d">
  <div class="container-fluid">
    <span class="navbar-brand">📊 OPTIONS BOT</span>
    <div class="d-flex gap-3">
      <a class="nav-link {% if active=='overview'  %}text-white{% else %}text-secondary{% endif %}"
         href="/">Overview</a>
      <a class="nav-link {% if active=='positions' %}text-white{% else %}text-secondary{% endif %}"
         href="/positions">Positions</a>
      <a class="nav-link {% if active=='history'   %}text-white{% else %}text-secondary{% endif %}"
         href="/history">History</a>
      <a class="nav-link {% if active=='stats'     %}text-white{% else %}text-secondary{% endif %}"
         href="/stats">Stats</a>
      <a class="nav-link {% if active=='scan'      %}text-white{% else %}text-secondary{% endif %}"
         href="/scan">Scan</a>
    </div>
    <span class="refresh-note">auto-refresh {{ refresh }}s</span>
  </div>
</nav>
<div class="container-fluid py-3 px-4">
  {% block content %}{% endblock %}
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

# ── Helper functions ──────────────────────────────────────────────────────────

def _fmt_pnl(val: Optional[float], cls_only=False) -> str:
    if val is None:
        css = "pnl-neu"; txt = "—"
    elif val > 0:
        css = "pnl-pos"; txt = f"+${val:,.2f}"
    elif val < 0:
        css = "pnl-neg"; txt = f"-${abs(val):,.2f}"
    else:
        css = "pnl-neu"; txt = "$0.00"
    if cls_only:
        return css
    return f'<span class="{css}">{txt}</span>'


def _fmt_pct(val: Optional[float]) -> str:
    if val is None:
        return "—"
    return f"{val:.1f}%"


def _fmt_date(s: Optional[str]) -> str:
    if not s:
        return "—"
    try:
        return s[:10]       # keep YYYY-MM-DD
    except Exception:
        return str(s)


def _dte_css(dte: Optional[int]) -> str:
    if dte is None:
        return "pnl-neu"
    if dte <= 5:
        return "dte-urgent"
    if dte <= 10:
        return "dte-soon"
    return "dte-ok"


def _strategy_badge(name: str) -> str:
    colours = {
        "bear_call_spread": "danger",
        "bull_put_spread":  "success",
        "covered_call":     "warning",
    }
    colour = colours.get(name, "secondary")
    label  = name.replace("_", " ").title()
    return f'<span class="badge bg-{colour} badge-strategy">{label}</span>'


def _reason_badge(reason: Optional[str]) -> str:
    if not reason:
        return '<span class="badge bg-secondary">—</span>'
    colours = {
        "expired_worthless": "success",
        "take_profit":       "primary",
        "dte_close":         "info",
        "stop_loss":         "danger",
        "manual":            "secondary",
    }
    colour = colours.get(reason, "secondary")
    label  = reason.replace("_", " ").title()
    return f'<span class="badge bg-{colour}">{label}</span>'


def _ledger_data():
    """Return (stats, open_trades, closed_trades) from the live ledger."""
    stats  = _ledger.get_statistics()
    open_t = _ledger.get_open_trades()
    closed = _ledger.get_closed_trades(limit=200)
    return stats, open_t, closed


def _gate_progress(stats: Dict) -> Dict:
    """Derive validation gate status."""
    cfg_min_trades   = app.config.get("GATE_MIN_TRADES",   10)
    cfg_min_win_rate = app.config.get("GATE_MIN_WIN_RATE",  0.60)

    total    = stats["total_trades"]
    win_rate = stats["win_rate"]
    passed   = total >= cfg_min_trades and win_rate >= cfg_min_win_rate

    return {
        "total":          total,
        "min_trades":     cfg_min_trades,
        "win_rate":       win_rate,
        "min_win_rate":   cfg_min_win_rate,
        "trades_ok":      total >= cfg_min_trades,
        "win_rate_ok":    win_rate >= cfg_min_win_rate and total >= cfg_min_trades,
        "gate_passed":    passed,
        "trade_pct":      min(100, round(total / cfg_min_trades * 100)),
        "win_rate_pct":   min(100, round(win_rate / cfg_min_win_rate * 100))
                          if total >= cfg_min_trades else 0,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

OVERVIEW_TMPL = BASE.replace("{% block content %}{% endblock %}", """
{% block content %}
<div class="row g-3 mb-3">

  {# ── KPI cards ── #}
  <div class="col-6 col-md-3">
    <div class="card h-100 text-center py-3">
      <div class="stat-number {{ 'pnl-pos' if stats.total_pnl >= 0 else 'pnl-neg' }}">
        {{ '+' if stats.total_pnl >= 0 else '' }}${{ "%.2f"|format(stats.total_pnl) }}
      </div>
      <div class="stat-label mt-1">Total Realised P&amp;L</div>
    </div>
  </div>
  <div class="col-6 col-md-3">
    <div class="card h-100 text-center py-3">
      <div class="stat-number {{ 'pnl-pos' if stats.win_rate >= 0.6 else 'gate-warn' }}">
        {{ "%.0f"|format(stats.win_rate * 100) }}%
      </div>
      <div class="stat-label mt-1">Win Rate
        <small class="d-block">({{ stats.winning_trades }}/{{ stats.total_trades }} trades)</small>
      </div>
    </div>
  </div>
  <div class="col-6 col-md-3">
    <div class="card h-100 text-center py-3">
      <div class="stat-number text-info">{{ stats.open_count }}</div>
      <div class="stat-label mt-1">Open Positions</div>
    </div>
  </div>
  <div class="col-6 col-md-3">
    <div class="card h-100 text-center py-3">
      <div class="stat-number {{ 'pnl-pos' if stats.avg_pct_captured else 'pnl-neu' }}">
        {% if stats.avg_pct_captured %}{{ "%.1f"|format(stats.avg_pct_captured) }}%{% else %}—{% endif %}
      </div>
      <div class="stat-label mt-1">Avg Premium Captured</div>
    </div>
  </div>

</div>

<div class="row g-3">

  {# ── Validation gate ── #}
  <div class="col-md-5">
    <div class="card h-100">
      <div class="card-header fw-semibold">
        {% if gate.gate_passed %}
          <span class="gate-pass">✅ Validation Gate — PASSED</span>
        {% else %}
          <span class="gate-warn">🔒 Validation Gate — In Progress</span>
        {% endif %}
      </div>
      <div class="card-body">

        <div class="mb-3">
          <div class="d-flex justify-content-between mb-1">
            <small>Closed Trades</small>
            <small class="{{ 'gate-pass' if gate.trades_ok else 'gate-warn' }}">
              {{ gate.total }} / {{ gate.min_trades }}
            </small>
          </div>
          <div class="progress" style="height:10px">
            <div class="progress-bar {{ 'bg-success' if gate.trades_ok else 'bg-warning' }}"
                 style="width:{{ gate.trade_pct }}%"></div>
          </div>
        </div>

        <div class="mb-3">
          <div class="d-flex justify-content-between mb-1">
            <small>Win Rate</small>
            <small class="{{ 'gate-pass' if gate.win_rate_ok else ('gate-fail' if gate.total >= gate.min_trades else 'pnl-neu') }}">
              {{ "%.0f"|format(gate.win_rate * 100) }}% / {{ "%.0f"|format(gate.min_win_rate * 100) }}% target
            </small>
          </div>
          <div class="progress" style="height:10px">
            <div class="progress-bar {{ 'bg-success' if gate.win_rate_ok else ('bg-danger' if gate.total >= gate.min_trades else 'bg-secondary') }}"
                 style="width:{{ gate.win_rate_pct }}%"></div>
          </div>
        </div>

        {% if gate.gate_passed %}
          <div class="alert alert-success py-2 mb-0 small">
            🚀 Gate passed — ready to switch <code>mode: live</code> in config.yaml
          </div>
        {% else %}
          <div class="alert alert-secondary py-2 mb-0 small">
            Complete {{ gate.min_trades - gate.total }} more trade(s) and
            maintain ≥{{ "%.0f"|format(gate.min_win_rate * 100) }}% win rate
            to unlock live mode.
          </div>
        {% endif %}
      </div>
    </div>
  </div>

  {# ── Quick stats ── #}
  <div class="col-md-4">
    <div class="card h-100">
      <div class="card-header fw-semibold">Performance</div>
      <div class="card-body p-0">
        <table class="table table-sm table-dark mb-0">
          <tbody>
            <tr><td class="text-secondary">Avg P&amp;L / trade</td>
                <td>{{ _fmt_pnl(stats.avg_pnl) | safe }}</td></tr>
            <tr><td class="text-secondary">Best trade</td>
                <td>{{ _fmt_pnl(stats.best_trade) | safe }}</td></tr>
            <tr><td class="text-secondary">Worst trade</td>
                <td>{{ _fmt_pnl(stats.worst_trade) | safe }}</td></tr>
            <tr><td class="text-secondary">Avg credit collected</td>
                <td class="text-warning">${{ "%.2f"|format(stats.avg_credit * 100) }}</td></tr>
            <tr><td class="text-secondary">Avg days held</td>
                <td>{{ stats.avg_days_held if stats.avg_days_held else '—' }}</td></tr>
            <tr><td class="text-secondary">Avg DTE at close</td>
                <td>{{ stats.avg_dte_at_close if stats.avg_dte_at_close else '—' }}</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  {# ── By strategy ── #}
  <div class="col-md-3">
    <div class="card h-100">
      <div class="card-header fw-semibold">By Strategy</div>
      <div class="card-body p-0">
        {% if stats.by_strategy %}
          <table class="table table-sm table-dark mb-0">
            <thead><tr>
              <th>Strategy</th><th>T</th><th>W%</th><th>P&amp;L</th>
            </tr></thead>
            <tbody>
            {% for name, s in stats.by_strategy.items() %}
            <tr>
              <td class="small">{{ name.replace('_',' ').title() }}</td>
              <td>{{ s.trades }}</td>
              <td class="{{ 'gate-pass' if s.win_rate >= 0.6 else 'gate-warn' }}">
                {{ "%.0f"|format(s.win_rate * 100) }}%</td>
              <td>{{ _fmt_pnl(s.total_pnl) | safe }}</td>
            </tr>
            {% endfor %}
            </tbody>
          </table>
        {% else %}
          <div class="p-3 text-secondary small">No closed trades yet.</div>
        {% endif %}
      </div>
    </div>
  </div>

</div>

{# ── Open positions mini-table ── #}
{% if open_trades %}
<div class="card mt-3">
  <div class="card-header fw-semibold">Open Positions <span class="badge bg-info">{{ open_trades|length }}</span></div>
  <div class="card-body p-0">
    <table class="table table-sm table-dark table-hover mb-0">
      <thead><tr>
        <th>#</th><th>Symbol</th><th>Strategy</th><th>Credit</th>
        <th>Expiry</th><th>DTE</th><th>IV Rank</th><th>Buffer</th><th>Opened</th>
      </tr></thead>
      <tbody>
      {% for t in open_trades %}
      {% set dte = (t.expiry | dte_days) %}
      <tr>
        <td class="text-secondary">{{ t.id }}</td>
        <td><strong>{{ t.symbol.replace('US.','') }}</strong></td>
        <td>{{ _strategy_badge(t.strategy_name) | safe }}</td>
        <td class="text-warning">${{ "%.2f"|format(t.net_credit * 100) }}</td>
        <td>{{ t.expiry }}</td>
        <td class="{{ _dte_css(dte) }}">{{ dte if dte is not none else '—' }}</td>
        <td>{{ "%.0f"|format(t.iv_rank) if t.iv_rank else '—' }}</td>
        <td>{{ "%.1f%%"|format(t.buffer_pct) if t.buffer_pct else '—' }}</td>
        <td class="text-secondary">{{ t.opened_at[:10] if t.opened_at else '—' }}</td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div>
{% endif %}
{% endblock %}
""")


POSITIONS_TMPL = BASE.replace("{% block content %}{% endblock %}", """
{% block content %}
<h5 class="mb-3 text-secondary">Open Positions
  <span class="badge bg-info ms-1">{{ open_trades|length }}</span>
</h5>

{% if open_trades %}
{% set missing_ctx = open_trades | selectattr('rsi_at_open', 'none') | list %}
{% if missing_ctx %}
<div class="alert alert-secondary py-2 mb-3 small">
  ℹ️ <strong>{{ missing_ctx|length }} position(s)</strong> were opened before entry context logging was deployed —
  RSI, %B, MACD, VIX, Spot@Open, Buffer and R/R will show <strong>—</strong> for those rows.
  New trades will capture all fields automatically.
</div>
{% endif %}
<div class="card">
  <div class="card-body p-0">
    <div class="table-responsive">
    <table class="table table-sm table-dark table-hover mb-0">
      <thead class="table-secondary">
        <tr>
          <th>#</th><th>Symbol</th><th>Strategy</th>
          <th>Sell Strike</th><th>Buy Strike</th>
          <th>Credit</th><th>Max Loss</th><th>R/R</th>
          <th>Expiry</th><th>DTE</th>
          <th>IV Rank</th><th>Delta</th><th>Buffer</th>
          <th>Spot@Open</th><th>RSI</th><th>%B</th><th>VIX</th>
          <th>Regime</th><th>Opened</th>
          <th>Unrealised P&L</th><th>Mark @</th>
        </tr>
      </thead>
      <tbody>
      {% for t in open_trades %}
      {% set dte = (t.expiry | dte_days) %}
      <tr>
        <td class="text-secondary">{{ t.id }}</td>
        <td><strong>{{ t.symbol.replace('US.','') }}</strong></td>
        <td>{{ _strategy_badge(t.strategy_name) | safe }}</td>

        {# parse strikes from contract codes using regex — handles all MooMoo/OSI formats #}
        <td class="text-danger">{{ _parse_strike(t.sell_contract) }}</td>
        <td class="text-secondary">{{ _parse_strike(t.buy_contract) if t.buy_contract else '—' }}</td>

        <td class="text-warning">${{ "%.2f"|format(t.net_credit * 100) }}</td>
        <td class="text-danger">${{ "%.0f"|format(t.max_loss) if t.max_loss else '—' }}</td>
        <td>{{ "%.2f"|format(t.reward_risk) if t.reward_risk else '—' }}</td>

        <td>{{ t.expiry }}</td>
        <td class="{{ _dte_css(dte) }} fw-bold">{{ dte if dte is not none else '—' }}</td>

        <td>{{ "%.0f"|format(t.iv_rank) if t.iv_rank else '—' }}</td>
        <td>{{ "%.2f"|format(t.delta) if t.delta else '—' }}</td>
        <td>{{ "%.1f%%"|format(t.buffer_pct) if t.buffer_pct else '—' }}</td>
        <td>{{ "$%.0f"|format(t.spot_price_at_open) if t.spot_price_at_open else '—' }}</td>
        <td>{{ "%.1f"|format(t.rsi_at_open) if t.rsi_at_open else '—' }}</td>
        <td>{{ "%.2f"|format(t.pct_b_at_open) if t.pct_b_at_open else '—' }}</td>
        <td>{{ "%.1f"|format(t.vix_at_open) if t.vix_at_open else '—' }}</td>
        <td class="text-secondary">{{ t.regime or '—' }}</td>
        <td class="text-secondary">{{ t.opened_at[:16] if t.opened_at else '—' }}</td>
        {% set mk = marks_by_id.get(t.id) %}
        {% if mk and mk.unrealised_pnl is not none %}
          {% set pnl = mk.unrealised_pnl %}
          <td class="fw-bold {{ 'pnl-pos' if pnl > 0 else ('pnl-neg' if pnl < 0 else 'pnl-neu') }}">
            {{ '+$' if pnl > 0 else '-$' }}{{ '%.2f'|format(pnl|abs) }}
            {% if mk.pnl_pct is not none %}<small class="text-secondary"> ({{ '%.0f'|format(mk.pnl_pct * 100) }}%)</small>{% endif %}
          </td>
          <td class="text-secondary" style="font-size:.78rem">{{ mk.as_of[11:16] if mk.as_of else '—' }} ET</td>
        {% else %}
          <td class="pnl-neu">—</td>
          <td class="text-secondary" style="font-size:.78rem">{% if marks_updated %}new trade{% else %}no data{% endif %}</td>
        {% endif %}
      </tr>
      {% endfor %}
      </tbody>
    </table>
    </div>
  </div>
</div>
{% if marks_updated %}
<div class="text-secondary mt-2" style="font-size:.78rem">
  ℹ️  Unrealised P&L last updated {{ marks_updated[11:16] }} ET · refreshes every ~5 min during market hours
</div>
{% else %}
<div class="alert alert-secondary py-2 mt-2 small">
  ℹ️  No live mark data yet — unrealised P&L is written by the bot’s position monitor every ~5 min during market hours.
</div>
{% endif %}
{% else %}
  <div class="alert alert-secondary">No open positions.</div>
{% endif %}
{% endblock %}
""")


HISTORY_TMPL = BASE.replace("{% block content %}{% endblock %}", """
{% block content %}
<h5 class="mb-3 text-secondary">Trade History
  <span class="badge bg-secondary ms-1">{{ closed_trades|length }}</span>
</h5>

{% if closed_trades %}
<div class="card">
  <div class="card-body p-0">
    <div class="table-responsive">
    <table class="table table-sm table-dark table-hover mb-0">
      <thead class="table-secondary">
        <tr>
          <th>#</th><th>Symbol</th><th>Strategy</th>
          <th>Credit</th><th>Close$</th><th>P&amp;L</th><th>% Cap.</th>
          <th>Exit</th><th>Days</th><th>DTE@Close</th>
          <th>IV@Open</th><th>IV@Close</th>
          <th>VIX@Open</th><th>VIX@Close</th>
          <th>Spot@Open</th><th>Spot@Close</th>
          <th>RSI</th><th>%B</th><th>MACD</th>
          <th>Buffer</th><th>R/R</th>
          <th>Opened</th><th>Closed</th>
        </tr>
      </thead>
      <tbody>
      {% for t in closed_trades %}
      <tr>
        <td class="text-secondary">{{ t.id }}</td>
        <td><strong>{{ t.symbol.replace('US.','') }}</strong></td>
        <td>{{ _strategy_badge(t.strategy_name) | safe }}</td>

        <td class="text-warning">${{ "%.2f"|format(t.net_credit * 100) }}</td>
        <td>{{ "$%.2f"|format(t.close_price * 100) if t.close_price else '—' }}</td>
        <td>{{ _fmt_pnl(t.pnl) | safe }}</td>
        <td>{{ "%.0f%%"|format(t.pct_premium_captured) if t.pct_premium_captured is not none else '—' }}</td>

        <td>{{ _reason_badge(t.close_reason) | safe }}</td>
        <td>{{ t.days_held if t.days_held is not none else '—' }}</td>
        <td>{{ t.dte_at_close if t.dte_at_close is not none else '—' }}</td>

        <td>{{ "%.0f"|format(t.iv_rank) if t.iv_rank else '—' }}</td>
        <td>{{ "%.0f"|format(t.iv_rank_at_close) if t.iv_rank_at_close else '—' }}</td>
        <td>{{ "%.1f"|format(t.vix_at_open) if t.vix_at_open else '—' }}</td>
        <td>{{ "%.1f"|format(t.vix_at_close) if t.vix_at_close else '—' }}</td>
        <td>{{ "$%.0f"|format(t.spot_price_at_open) if t.spot_price_at_open else '—' }}</td>
        <td>{{ "$%.0f"|format(t.spot_price_at_close) if t.spot_price_at_close else '—' }}</td>

        <td>{{ "%.1f"|format(t.rsi_at_open) if t.rsi_at_open else '—' }}</td>
        <td>{{ "%.2f"|format(t.pct_b_at_open) if t.pct_b_at_open else '—' }}</td>
        <td>{{ "%.2f"|format(t.macd_at_open) if t.macd_at_open else '—' }}</td>
        <td>{{ "%.1f%%"|format(t.buffer_pct) if t.buffer_pct else '—' }}</td>
        <td>{{ "%.2f"|format(t.reward_risk) if t.reward_risk else '—' }}</td>

        <td class="text-secondary">{{ t.opened_at[:10] if t.opened_at else '—' }}</td>
        <td class="text-secondary">{{ t.closed_at[:10] if t.closed_at else '—' }}</td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
    </div>
  </div>
</div>
{% else %}
  <div class="alert alert-secondary">No closed trades yet.</div>
{% endif %}
{% endblock %}
""")


STATS_TMPL = BASE.replace("{% block content %}{% endblock %}", """
{% block content %}
<div class="row g-3">

  {# ── By strategy ── #}
  <div class="col-md-6">
    <div class="card">
      <div class="card-header fw-semibold">By Strategy</div>
      <div class="card-body p-0">
        {% if stats.by_strategy %}
        <table class="table table-sm table-dark mb-0">
          <thead><tr>
            <th>Strategy</th><th>Trades</th><th>Wins</th>
            <th>Win%</th><th>Total P&amp;L</th><th>Avg P&amp;L</th><th>Avg Days</th>
          </tr></thead>
          <tbody>
          {% for name, s in stats.by_strategy.items() %}
          <tr>
            <td>{{ _strategy_badge(name) | safe }}</td>
            <td>{{ s.trades }}</td>
            <td class="gate-pass">{{ s.wins }}</td>
            <td class="{{ 'gate-pass' if s.win_rate >= 0.6 else 'gate-warn' }}">
              {{ "%.0f"|format(s.win_rate * 100) }}%</td>
            <td>{{ _fmt_pnl(s.total_pnl) | safe }}</td>
            <td>{{ _fmt_pnl(s.avg_pnl) | safe }}</td>
            <td>{{ s.avg_days_held if s.avg_days_held else '—' }}</td>
          </tr>
          {% endfor %}
          </tbody>
        </table>
        {% else %}
          <div class="p-3 text-secondary small">No data yet.</div>
        {% endif %}
      </div>
    </div>
  </div>

  {# ── By exit reason ── #}
  <div class="col-md-6">
    <div class="card">
      <div class="card-header fw-semibold">By Exit Reason</div>
      <div class="card-body p-0">
        {% if stats.by_close_reason %}
        <table class="table table-sm table-dark mb-0">
          <thead><tr>
            <th>Reason</th><th>Trades</th><th>Total P&amp;L</th><th>Avg P&amp;L</th>
          </tr></thead>
          <tbody>
          {% for reason, r in stats.by_close_reason.items() %}
          <tr>
            <td>{{ _reason_badge(reason) | safe }}</td>
            <td>{{ r.trades }}</td>
            <td>{{ _fmt_pnl(r.total_pnl) | safe }}</td>
            <td>{{ _fmt_pnl(r.avg_pnl) | safe }}</td>
          </tr>
          {% endfor %}
          </tbody>
        </table>
        {% else %}
          <div class="p-3 text-secondary small">No data yet.</div>
        {% endif %}
      </div>
    </div>
  </div>

  {# ── Averages ── #}
  <div class="col-md-6">
    <div class="card">
      <div class="card-header fw-semibold">Averages &amp; Risk</div>
      <div class="card-body p-0">
        <table class="table table-sm table-dark mb-0">
          <tbody>
            <tr><td class="text-secondary">Avg credit (×100)</td>
                <td class="text-warning">${{ "%.2f"|format(stats.avg_credit * 100) }}</td></tr>
            <tr><td class="text-secondary">Avg max loss</td>
                <td class="text-danger">${{ "%.2f"|format(stats.avg_max_loss) if stats.avg_max_loss else '—' }}</td></tr>
            <tr><td class="text-secondary">Avg days held</td>
                <td>{{ stats.avg_days_held if stats.avg_days_held else '—' }}</td></tr>
            <tr><td class="text-secondary">Avg DTE at close</td>
                <td>{{ stats.avg_dte_at_close if stats.avg_dte_at_close else '—' }}</td></tr>
            <tr><td class="text-secondary">Avg % premium captured (winners)</td>
                <td class="gate-pass">{{ "%.1f%%"|format(stats.avg_pct_captured) if stats.avg_pct_captured else '—' }}</td></tr>
            <tr><td class="text-secondary">Best trade</td>
                <td>{{ _fmt_pnl(stats.best_trade) | safe }}</td></tr>
            <tr><td class="text-secondary">Worst trade</td>
                <td>{{ _fmt_pnl(stats.worst_trade) | safe }}</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  {# ── Overall summary ── #}
  <div class="col-md-6">
    <div class="card">
      <div class="card-header fw-semibold">Overall Summary</div>
      <div class="card-body p-0">
        <table class="table table-sm table-dark mb-0">
          <tbody>
            <tr><td class="text-secondary">Total closed trades</td>
                <td>{{ stats.total_trades }}</td></tr>
            <tr><td class="text-secondary">Winning trades</td>
                <td class="gate-pass">{{ stats.winning_trades }}</td></tr>
            <tr><td class="text-secondary">Win rate</td>
                <td class="{{ 'gate-pass' if stats.win_rate >= 0.6 else 'gate-warn' }}">
                  {{ "%.1f%%"|format(stats.win_rate * 100) }}</td></tr>
            <tr><td class="text-secondary">Total P&amp;L</td>
                <td>{{ _fmt_pnl(stats.total_pnl) | safe }}</td></tr>
            <tr><td class="text-secondary">Avg P&amp;L / trade</td>
                <td>{{ _fmt_pnl(stats.avg_pnl) | safe }}</td></tr>
            <tr><td class="text-secondary">Open positions</td>
                <td class="text-info">{{ stats.open_count }}</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

</div>
{% endblock %}
""")


# ── Jinja2 helpers registered as globals ─────────────────────────────────────

@app.template_global()
def _fmt_pnl(val):
    if val is None:
        return '<span class="pnl-neu">—</span>'
    if val > 0:
        return f'<span class="pnl-pos">+${val:,.2f}</span>'
    if val < 0:
        return f'<span class="pnl-neg">-${abs(val):,.2f}</span>'
    return '<span class="pnl-neu">$0.00</span>'

@app.template_global()
def _strategy_badge(name):
    colours = {
        "bear_call_spread": "danger",
        "bull_put_spread":  "success",
        "covered_call":     "warning",
    }
    c = colours.get(name, "secondary")
    l = name.replace("_", " ").title()
    return f'<span class="badge bg-{c} badge-strategy">{l}</span>'

@app.template_global()
def _reason_badge(reason):
    if not reason:
        return '<span class="badge bg-secondary">—</span>'
    colours = {
        "expired_worthless": "success",
        "take_profit":       "primary",
        "dte_close":         "info",
        "stop_loss":         "danger",
        "manual":            "secondary",
    }
    c = colours.get(reason, "secondary")
    l = reason.replace("_", " ").title()
    return f'<span class="badge bg-{c}">{l}</span>'

@app.template_global()
def _dte_css(dte):
    if dte is None: return "pnl-neu"
    if dte <= 5:    return "dte-urgent"
    if dte <= 10:   return "dte-soon"
    return "dte-ok"

@app.template_global()
def _parse_strike(code: Optional[str]) -> str:
    """Extract a human-readable strike price from a MooMoo/OSI option contract code.

    Handles all known formats:
      SPY260402C700000        → "700"    (short, strike already in dollars)
      SPY260402C00700000      → "700"    (OSI 8-digit, strike × 1000)
      US.SPY260402C00700000   → "700"    (prefixed OSI)

    Returns "—" when the code is absent or unparseable.
    """
    import re
    if not code:
        return "—"
    m = re.search(r'[CP](\d+)$', code)
    if not m:
        return "—"
    raw = int(m.group(1))
    # OSI encoding: 8-digit field = strike × 1000 (raw > 10000 signals this)
    strike = raw / 1000 if raw > 10000 else float(raw)
    # Format: drop .0 for whole numbers, keep decimals for fractional strikes
    return f"${strike:,.0f}" if strike == int(strike) else f"${strike:,.2f}"

@app.template_filter()
def dte_days(expiry_str: str) -> Optional[int]:
    """Convert expiry string to DTE (days from today)."""
    try:
        exp = date.fromisoformat(expiry_str)
        return max(0, (exp - date.today()).days)
    except Exception:
        return None


# ── Market-hours helpers ──────────────────────────────────────────────────────

ET = ZoneInfo("America/New_York")

def _market_status() -> dict:
    """
    Return a dict describing the current US equity market status.

    Keys:
      is_open   bool   True during regular session (09:30–16:00 ET, Mon–Fri)
      session   str    "pre" | "open" | "post" | "closed"
      now_et    datetime  current ET time
      next_open str    human-readable next open (only when closed)
    """
    now = datetime.now(ET)
    weekday = now.weekday()   # 0=Mon … 6=Sun

    open_time  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_time = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    pre_open   = now.replace(hour=4,  minute=0,  second=0, microsecond=0)
    post_close = now.replace(hour=20, minute=0,  second=0, microsecond=0)

    if weekday >= 5:  # weekend
        days_to_monday = (7 - weekday) % 7 or 7
        next_open = (now + timedelta(days=days_to_monday)).strftime("%A %b %-d at 9:30 AM ET")
        return {"is_open": False, "session": "closed", "now_et": now, "next_open": next_open}

    if now < pre_open:
        next_open = now.strftime("Today at 9:30 AM ET")
        return {"is_open": False, "session": "closed", "now_et": now, "next_open": next_open}

    if pre_open <= now < open_time:
        return {"is_open": False, "session": "pre", "now_et": now, "next_open": now.strftime("Today at 9:30 AM ET")}

    if open_time <= now < close_time:
        return {"is_open": True, "session": "open", "now_et": now, "next_open": ""}

    if close_time <= now < post_close:
        # Check next business day
        next_day = now + timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)
        next_open = next_day.strftime("%A %b %-d at 9:30 AM ET")
        return {"is_open": False, "session": "post", "now_et": now, "next_open": next_open}

    # After post-market
    next_day = now + timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += timedelta(days=1)
    next_open = next_day.strftime("%A %b %-d at 9:30 AM ET")
    return {"is_open": False, "session": "closed", "now_et": now, "next_open": next_open}


def _load_scan_results() -> Optional[dict]:
    """Read data/scan_results.json (or path from config). Returns None if missing."""
    cfg_file = ROOT / DEFAULT_CFG
    db_path  = DEFAULT_DB
    if cfg_file.exists():
        try:
            with open(cfg_file) as f:
                cfg = yaml.safe_load(f)
            db_path = cfg.get("paper_ledger", {}).get("db_path", DEFAULT_DB)
        except Exception:
            pass
    scan_file = Path(db_path).parent / "scan_results.json"
    if not scan_file.exists():
        return None
    try:
        with open(scan_file) as f:
            return json.load(f)
    except Exception:
        return None


# ── Views ─────────────────────────────────────────────────────────────────────

@app.route("/")
def overview():
    stats, open_t, closed = _ledger_data()
    gate = _gate_progress(stats)
    return render_template_string(
        OVERVIEW_TMPL,
        active="overview", refresh=REFRESH_SECS,
        title="Overview", stats=stats,
        open_trades=open_t, gate=gate,
    )

@app.route("/positions")
def positions():
    stats, open_t, _ = _ledger_data()
    mark_data   = _load_positions_mark()
    marks_by_id = mark_data.get("marks_by_id", {})
    marks_updated = mark_data.get("updated_at", None)
    return render_template_string(
        POSITIONS_TMPL,
        active="positions", refresh=REFRESH_SECS,
        title="Positions", stats=stats,
        open_trades=open_t,
        marks_by_id=marks_by_id,
        marks_updated=marks_updated,
    )

@app.route("/history")
def history():
    stats, _, closed = _ledger_data()
    return render_template_string(
        HISTORY_TMPL,
        active="history", refresh=REFRESH_SECS,
        title="History", stats=stats,
        closed_trades=closed,
    )

@app.route("/stats")
def stats_page():
    stats, _, _ = _ledger_data()
    return render_template_string(
        STATS_TMPL,
        active="stats", refresh=REFRESH_SECS,
        title="Stats", stats=stats,
    )

@app.route("/healthz")
def healthz():
    """Simple health-check — returns 200 with trade count."""
    stats = _ledger.get_statistics()
    return {
        "status": "ok",
        "open":   stats["open_count"],
        "closed": stats["total_trades"],
        "win_rate": round(stats["win_rate"] * 100, 1),
    }


SCAN_TMPL = BASE.replace("{% block content %}{% endblock %}", """
{% block content %}

{# ── Market status banner ── #}
{% if market.session == 'open' %}
  <div class="alert alert-success py-2 mb-3 d-flex align-items-center gap-2">
    <span style="font-size:1.1rem">🟢</span>
    <span><strong>Market Open</strong> — {{ market.now_et.strftime('%H:%M ET') }}
      {% if scan %}
        &nbsp;·&nbsp;
        {% if scan.get('scan_type') == 'intraday' %}
          <span class="badge bg-info">Intraday Rescan</span>
        {% else %}
          <span class="badge bg-secondary">Morning Scan #{{ scan.scan_number }}</span>
        {% endif %}
        last updated {{ scan.scan_timestamp[11:16] }} ET
        &nbsp;·&nbsp; refreshes every 30 min during market hours
      {% else %}
        &nbsp;·&nbsp; waiting for 09:35 morning scan…
      {% endif %}
    </span>
  </div>
{% elif market.session == 'pre' %}
  <div class="alert alert-warning py-2 mb-3">
    🟡 <strong>Pre-Market</strong> — morning scan runs at 09:35 ET, then every 30 min
    {% if scan %}&nbsp;·&nbsp; Showing yesterday's last scan ({{ scan.scan_timestamp[:10] }}){% endif %}
  </div>
{% elif market.session == 'post' %}
  <div class="alert alert-secondary py-2 mb-3">
    🔵 <strong>After Hours</strong> — {{ market.now_et.strftime('%H:%M ET') }}
    &nbsp;·&nbsp; Next open: {{ market.next_open }}
    {% if scan %}&nbsp;·&nbsp; Showing today's final scan ({{ scan.scan_timestamp[11:16] }} ET){% endif %}
  </div>
{% else %}
  <div class="alert alert-secondary py-2 mb-3">
    ⚫ <strong>Market Closed</strong> &nbsp;·&nbsp; Next open: {{ market.next_open }}
    {% if scan %}&nbsp;·&nbsp; Last scan: {{ scan.scan_timestamp[:10] }} at {{ scan.scan_timestamp[11:16] }} ET{% endif %}
  </div>
{% endif %}

{% if not scan %}
  {# ── No data yet ── #}
  <div class="card">
    <div class="card-body text-center py-5">
      <div style="font-size:2.5rem">📭</div>
      <h5 class="mt-3 text-secondary">No scan data yet</h5>
      <p class="text-secondary mb-0">
        <code>scan_results.json</code> is written by the bot after each morning scan.<br>
        Start the bot and wait for the 09:35 ET scan to complete.
      </p>
    </div>
  </div>

{% else %}

  {# ── Scan summary strip ── #}
  <div class="row g-2 mb-3">
    <div class="col-6 col-md-3">
      <div class="card text-center py-2">
        <div class="stat-number text-info">{{ scan.symbols_scanned }}</div>
        <div class="stat-label">Symbols Scanned</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card text-center py-2">
        <div class="stat-number {{ 'pnl-pos' if scan.signals_found > 0 else 'pnl-neu' }}">{{ scan.signals_found }}</div>
        <div class="stat-label">Signals Found</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card text-center py-2">
        <div class="stat-number {{ 'pnl-pos' if scan.signals_executed > 0 else 'pnl-neu' }}">{{ scan.signals_executed }}</div>
        <div class="stat-label">Executed</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card text-center py-2">
        {% if scan.get('scan_type') == 'intraday' %}
          <div class="stat-number"><span class="badge bg-info" style="font-size:.9rem">Intraday</span></div>
          <div class="stat-label">Rescan Type</div>
        {% else %}
          <div class="stat-number text-secondary">{{ scan.elapsed_seconds }}s</div>
          <div class="stat-label">Scan Duration</div>
        {% endif %}
      </div>
    </div>
  </div>

  {% if scan.get('scan_type') == 'intraday' %}
  <div class="alert alert-info py-2 mb-3 small">
    ℹ️  <strong>Intraday rescan</strong> — live option pricing and spot price refreshed every 30 min.
    RSI, MACD, %B and regime are carried from the morning scan
    (daily-bar indicators don't change intraday).
    Symbol cards below reflect morning indicator values; gate outcomes and candidates use current pricing.
  </div>
  {% endif %}

  {# ── Ranked candidates table (only if signals were found) ── #}
  {% if scan.candidates %}
  <div class="card mb-3">
    <div class="card-header fw-semibold">
      📊 Ranked Candidates
      <span class="badge bg-secondary ms-1">{{ scan.candidates|length }}</span>
    </div>
    <div class="card-body p-0">
      <table class="table table-sm table-dark table-hover mb-0">
        <thead class="table-secondary">
          <tr>
            <th>Rank</th><th>Symbol</th><th>Strategy</th>
            <th>IV Rank</th><th>Buffer</th><th>R/R</th><th>Score</th>
            <th>Credit</th><th>Expiry</th><th>DTE</th><th>Outcome</th>
          </tr>
        </thead>
        <tbody>
        {% for c in scan.candidates %}
        <tr>
          <td class="text-secondary fw-bold">#{{ c.rank }}</td>
          <td><strong>{{ c.symbol.replace('US.','') }}</strong></td>
          <td>{{ _strategy_badge(c.strategy) | safe }}</td>
          <td>{{ "%.0f"|format(c.iv_rank) }}</td>
          <td>{{ "%.1f%%"|format(c.buffer_pct) if c.buffer_pct else '—' }}</td>
          <td>{{ "%.2f"|format(c.reward_risk) if c.reward_risk else '—' }}</td>
          <td class="fw-bold">{{ "%.3f"|format(c.score) if c.score else '—' }}</td>
          <td class="text-warning">${{ "%.2f"|format(c.net_credit * 100) }}</td>
          <td>{{ c.expiry }}</td>
          <td>{{ c.dte }}</td>
          <td>
            {% if c.outcome == 'executed' %}
              <span class="badge bg-success">✅ Executed</span>
            {% elif c.outcome == 'approved_not_filled' %}
              <span class="badge bg-primary">⏳ Approved</span>
            {% elif c.outcome.startswith('blocked:') %}
              <span class="badge bg-danger" title="{{ c.outcome[8:] }}">🚫 Blocked</span>
            {% else %}
              <span class="badge bg-secondary">⏭ Skipped</span>
            {% endif %}
          </td>
        </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
  {% elif scan.signals_found == 0 %}
  <div class="alert alert-secondary mb-3">
    No signals generated this cycle — all symbols filtered by gates or option chain checks.
  </div>
  {% endif %}

  {# ── Per-symbol market data + gate results ── #}
  <h6 class="text-secondary mb-2 mt-1">Symbol Detail</h6>
  <div class="row g-3">
  {% for sym in scan.symbols %}
    <div class="col-md-6 col-xl-4">
      <div class="card h-100">

        {# symbol header #}
        <div class="card-header d-flex justify-content-between align-items-center">
          <span class="fw-bold">{{ sym.symbol.replace('US.','') }}</span>
          <span class="badge {{ 'bg-warning text-dark' if sym.regime == 'bull'
                               else 'bg-danger' if sym.regime == 'bear'
                               else 'bg-secondary' if sym.regime == 'high_vol'
                               else 'bg-primary' }}">
            {{ sym.regime }}
          </span>
        </div>

        <div class="card-body pb-1">
          {# market data row #}
          <div class="row g-1 mb-2" style="font-size:.8rem">
            <div class="col-4 text-center">
              <div class="fw-bold">${{ "%.2f"|format(sym.spot_price) }}</div>
              <div class="stat-label">Price</div>
            </div>
            <div class="col-4 text-center">
              <div class="fw-bold {{ 'pnl-pos' if sym.iv_rank >= 35 else 'gate-warn' }}">{{ "%.0f"|format(sym.iv_rank) }}</div>
              <div class="stat-label">IV Rank</div>
            </div>
            <div class="col-4 text-center">
              <div class="fw-bold {{ 'gate-warn' if sym.vix >= 25 else '' }}">{{ "%.1f"|format(sym.vix) }}</div>
              <div class="stat-label">VIX</div>
            </div>
          </div>
          <div class="row g-1 mb-2" style="font-size:.8rem">
            <div class="col-4 text-center">
              <div class="fw-bold">{{ "%.1f"|format(sym.rsi) }}</div>
              <div class="stat-label">RSI</div>
            </div>
            <div class="col-4 text-center">
              <div class="fw-bold">{{ "%.2f"|format(sym.pct_b) }}</div>
              <div class="stat-label">%B</div>
            </div>
            <div class="col-4 text-center">
              <div class="fw-bold {{ 'pnl-pos' if sym.macd > 0 else 'pnl-neg' }}">{{ "%.2f"|format(sym.macd) }}</div>
              <div class="stat-label">MACD</div>
            </div>
          </div>
          {% if sym.next_earnings_days %}
          <div class="mb-2" style="font-size:.78rem">
            <span class="text-secondary">Earnings in </span>
            <span class="{{ 'gate-warn' if sym.next_earnings_days <= 21 else '' }}">
              {{ sym.next_earnings_days }}d
            </span>
          </div>
          {% endif %}

          {# gate pills per strategy #}
          {% for strat in sym.strategies %}
          <div class="mb-2">
            <div class="d-flex align-items-center gap-1 mb-1">
              {{ _strategy_badge(strat.strategy) | safe }}
              {% if strat.result == 'signal' %}
                <span class="badge bg-success ms-auto">✅ Signal</span>
              {% elif strat.result == 'disabled' %}
                <span class="badge bg-secondary ms-auto">Off</span>
              {% else %}
                <span class="badge bg-danger ms-auto" title="{{ strat.result[5:] if strat.result.startswith('skip:') else strat.result }}">
                  ❌ Skip
                </span>
              {% endif %}
            </div>
            {% if strat.gates %}
            <div class="ps-1" style="font-size:.74rem">
              {% for gate in strat.gates %}
              <div class="{{ 'gate-pass' if gate.passed else 'gate-fail' }}">
                {{ '✓' if gate.passed else '✗' }} {{ gate.label }}
                <span class="text-secondary">({{ gate.detail }})</span>
              </div>
              {% endfor %}
              {% if strat.result.startswith('skip:') and strat.gates | selectattr('passed') | list | length == strat.gates | length %}
              <div class="gate-fail">✗ option chain: {{ strat.result[5:] }}</div>
              {% endif %}
            </div>
            {% endif %}
          </div>
          {% endfor %}
        </div>

      </div>
    </div>
  {% endfor %}
  </div>

{% endif %} {# end if scan #}
{% endblock %}
""")


def _load_positions_mark() -> dict:
    """Read positions_mark.json written by _monitor_job.
    Returns {updated_at: str, marks_by_id: {trade_id: mark_dict}} or empty dict.
    """
    cfg_file = ROOT / DEFAULT_CFG
    db_path  = DEFAULT_DB
    if cfg_file.exists():
        try:
            with open(cfg_file) as f:
                cfg = yaml.safe_load(f)
            db_path = cfg.get("paper_ledger", {}).get("db_path", DEFAULT_DB)
        except Exception:
            pass
    mark_file = Path(db_path).parent / "positions_mark.json"
    if not mark_file.exists():
        return {}
    try:
        with open(mark_file) as f:
            data = json.load(f)
        by_id = {int(m["id"]): m for m in data.get("marks", []) if m.get("id") is not None}
        return {"updated_at": data.get("updated_at"), "marks_by_id": by_id}
    except Exception:
        return {}


@app.route("/scan")
def scan_page():
    market = _market_status()
    scan   = _load_scan_results()
    return render_template_string(
        SCAN_TMPL,
        active="scan", refresh=REFRESH_SECS,
        title="Scan", market=market, scan=scan,
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def _resolve_db_path(cli_db: Optional[str]) -> str:
    """Resolve DB path: CLI arg > config.yaml > default."""
    if cli_db:
        return cli_db

    cfg_file = ROOT / DEFAULT_CFG
    if cfg_file.exists():
        try:
            with open(cfg_file) as f:
                cfg = yaml.safe_load(f)
            path = cfg.get("paper_ledger", {}).get("db_path")
            if path:
                return str(ROOT / path)
        except Exception:
            pass

    return str(ROOT / DEFAULT_DB)


def _resolve_gate_config() -> tuple:
    """Read validation gate thresholds from config.yaml."""
    cfg_file = ROOT / DEFAULT_CFG
    if cfg_file.exists():
        try:
            with open(cfg_file) as f:
                cfg = yaml.safe_load(f)
            vg = cfg.get("validation_gate", {})
            return (
                vg.get("min_trades",   10),
                vg.get("min_win_rate", 0.60),
            )
        except Exception:
            pass
    return 10, 0.60


def main():
    global _ledger

    parser = argparse.ArgumentParser(description="Options Bot Dashboard")
    parser.add_argument("--db",   default=None, help="Path to paper_trades.db")
    parser.add_argument("--port", default=5000,  type=int)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    db_path = _resolve_db_path(args.db)
    print(f"📊 Dashboard connecting to: {db_path}")

    if not Path(db_path).exists():
        print(f"⚠️  DB not found at {db_path} — starting with empty ledger")

    _ledger = PaperLedger(db_path=db_path)

    min_trades, min_win_rate = _resolve_gate_config()
    app.config["GATE_MIN_TRADES"]   = min_trades
    app.config["GATE_MIN_WIN_RATE"] = min_win_rate

    print(f"🌐 Serving on http://{args.host}:{args.port}")
    print(f"🔒 Gate: {min_trades} trades, {min_win_rate*100:.0f}% win rate")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
