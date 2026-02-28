#!/usr/bin/env python3
"""
Hybrid Connectivity Pre-Flight Check
=====================================
Run this before main.py to confirm that all services the bot depends on
are reachable and returning valid data.

Checks performed:
  ── MooMoo OpenD ────────────────────────────────────────────────
  [M1] OpenD connection on 127.0.0.1:11111
  [M2] Quote context — option expiries for watchlist symbols
  [M3] Option chain  — CALL chain for nearest expiry
  [M4] Option Greeks — snapshot with delta/IV for top 5 strikes
  [M5] Account info  — paper options account balance

  ── Yahoo Finance ────────────────────────────────────────────────
  [Y1] Daily OHLCV   — 6-month price history
  [Y2] Current VIX   — market regime input
  [Y3] Earnings dates — earnings conflict filter

  ── Interactive Brokers TWS ──────────────────────────────────────
  [I1] TWS connection on 127.0.0.1:7496
  [I2] Account identity — confirms live account visible
  [I3] Account balance   — net liquidation and cash
  [I4] Stock positions   — confirms watchlist shares visible
  [I5] Option positions  — any open option positions
  [I6] Option chain      — contract params (no subscription needed)
  [I7] Live price        — spot price (requires market data subscription)

Usage:
    python3 tests/preflight_check.py

    # To test a specific symbol:
    python3 tests/preflight_check.py --symbol AAPL

    # To skip sections:
    python3 tests/preflight_check.py --skip-moomoo
    python3 tests/preflight_check.py --skip-ibkr
    python3 tests/preflight_check.py --skip-yahoo

Exit codes:
    0 — all checks passed (safe to run main.py)
    1 — one or more checks failed (do not run main.py)
"""

import sys
import os
import argparse
import math
from datetime import date, datetime

# ── Path setup ────────────────────────────────────────────────────────────────
# Allow running from project root or tests/ directory
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ── Config ────────────────────────────────────────────────────────────────────
# Read from config.yaml so this script always matches what the bot uses
import yaml  # pip3 install pyyaml

CONFIG_PATH = os.path.join(ROOT, "config", "config.yaml")

def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

# ── Colours ───────────────────────────────────────────────────────────────────
# Works on macOS and Linux terminals; falls back gracefully on Windows
def green(s):  return f"\033[32m{s}\033[0m"
def red(s):    return f"\033[31m{s}\033[0m"
def yellow(s): return f"\033[33m{s}\033[0m"
def bold(s):   return f"\033[1m{s}\033[0m"
def dim(s):    return f"\033[2m{s}\033[0m"

PASS = green("✅ PASS")
FAIL = red("❌ FAIL")
WARN = yellow("⚠️  WARN")
SKIP = dim("── SKIP")

# ── Result tracking ───────────────────────────────────────────────────────────
results = []   # list of (check_id, label, passed, message)

def record(check_id, label, passed, message="", warn=False):
    """Record a check result and print it immediately."""
    if passed is None:      # skipped
        tag = SKIP
    elif warn:
        tag = WARN
        passed = True       # warnings don't fail the run
    elif passed:
        tag = PASS
    else:
        tag = FAIL
    print(f"  {tag}  [{check_id}] {label}")
    if message:
        indent = "            " if passed else "            "
        for line in message.split("\n"):
            print(f"         {dim(line)}")
    results.append((check_id, label, passed, warn))

def section(title):
    print(f"\n{bold(title)}")
    print("  " + "─" * 58)

def summary():
    failures = [(cid, lbl) for cid, lbl, ok, warn in results
                if ok is False and not warn]
    warnings = [(cid, lbl) for cid, lbl, ok, warn in results
                if ok is True and warn]
    skipped  = [(cid, lbl) for cid, lbl, ok, warn in results
                if ok is None]
    passed   = [(cid, lbl) for cid, lbl, ok, warn in results
                if ok is True and not warn]

    total = len([r for r in results if r[2] is not None])

    print("\n" + "═" * 62)
    print(bold("  Pre-Flight Summary"))
    print("═" * 62)
    print(f"  Passed  : {green(str(len(passed)))}")
    if warnings:
        print(f"  Warnings: {yellow(str(len(warnings)))}")
    if failures:
        print(f"  Failed  : {red(str(len(failures)))}")
    if skipped:
        print(f"  Skipped : {len(skipped)}")
    print()

    if failures:
        print(red("  ❌  PRE-FLIGHT FAILED — do not run main.py"))
        print()
        print("  Failing checks:")
        for cid, lbl in failures:
            print(f"    [{cid}] {lbl}")
        print()
        return False
    else:
        print(green("  ✅  ALL CHECKS PASSED — safe to run main.py"))
        if warnings:
            print()
            print(yellow("  Warnings (non-blocking):"))
            for cid, lbl in warnings:
                print(f"    [{cid}] {lbl}")
        print()
        return True


# ══════════════════════════════════════════════════════════════════════════════
#  MooMoo checks
# ══════════════════════════════════════════════════════════════════════════════

def check_moomoo(config, watchlist):
    section("MooMoo OpenD  (market data + Greeks)")

    # ── Import SDK ────────────────────────────────────────────────
    try:
        import moomoo as mm
    except ImportError:
        record("M1", "OpenD connection", False,
               "moomoo-api not installed. Run: pip3 install moomoo-api")
        for cid in ["M2", "M3", "M4", "M5"]:
            record(cid, "skipped — moomoo-api missing", None)
        return

    host = config["moomoo"]["host"]
    port = config["moomoo"]["port"]
    trade_env = (mm.TrdEnv.SIMULATE
                 if config["moomoo"]["trade_env"] == "SIMULATE"
                 else mm.TrdEnv.REAL)
    stock_acc  = int(config["moomoo"]["stock_account_id"])
    option_acc = int(config["moomoo"]["option_account_id"])

    # ── M1: Connection ────────────────────────────────────────────
    quote_ctx = None
    trade_ctx = None
    try:
        quote_ctx = mm.OpenQuoteContext(host=host, port=port)
        ret, data = quote_ctx.get_global_state()
        if ret != 0:
            raise RuntimeError(f"get_global_state failed: {data}")
        record("M1", f"OpenD connection  ({host}:{port})", True,
               f"OpenD running | market={data.get('market_state', '?')}")
    except Exception as e:
        record("M1", f"OpenD connection  ({host}:{port})", False,
               f"{e}\nIs MooMoo OpenD running and logged in?")
        for cid in ["M2", "M3", "M4", "M5"]:
            record(cid, "skipped — OpenD unavailable", None)
        return

    # ── M2: Option expiries ───────────────────────────────────────
    symbol     = watchlist[0]    # test on first symbol in watchlist
    expiries   = []
    try:
        ret, data = quote_ctx.get_option_expiration_date(code=symbol)
        if ret != 0:
            raise RuntimeError(f"get_option_expiration_date failed: {data}")
        today    = date.today().strftime("%Y-%m-%d")
        # MooMoo SDK returns expiry dates under "strike_time"; fall back by
        # scanning for any date-like column if the expected one is missing.
        if "strike_time" in data.columns:
            expiry_col = "strike_time"
        elif "time" in data.columns:
            expiry_col = "time"
        else:
            # Pick first string column that looks like a date
            date_cols = [c for c in data.columns
                         if data[c].dtype == object and
                         len(data) > 0 and
                         str(data[c].iloc[0])[:4].isdigit()]
            if not date_cols:
                raise RuntimeError(
                    f"Cannot find expiry date column. "
                    f"Available columns: {list(data.columns)}"
                )
            expiry_col = date_cols[0]
        expiries = sorted([e for e in data[expiry_col].tolist() if e >= today])
        if not expiries:
            raise RuntimeError("no upcoming expiries returned")
        record("M2", f"Option expiries  ({symbol})", True,
               f"{len(expiries)} upcoming | nearest: {expiries[0]}")
    except Exception as e:
        record("M2", f"Option expiries  ({symbol})", False, str(e))

    # ── M3: Option chain ──────────────────────────────────────────
    chain      = None
    expiry     = expiries[0] if expiries else None
    call_codes = []
    try:
        if not expiry:
            raise RuntimeError("no expiry available from M2")
        ret, chain = quote_ctx.get_option_chain(
            code=symbol, start=expiry, end=expiry,
            option_type=mm.OptionType.CALL
        )
        if ret != 0:
            raise RuntimeError(f"get_option_chain failed: {chain}")
        if len(chain) == 0:
            raise RuntimeError("empty chain returned")
        call_codes = chain["code"].tolist()
        record("M3", f"Option chain     ({symbol} {expiry} CALL)", True,
               f"{len(call_codes)} strikes returned")
    except Exception as e:
        record("M3", f"Option chain     ({symbol} {expiry or 'N/A'} CALL)", False, str(e))

    # ── M4: Greeks / snapshot ─────────────────────────────────────
    try:
        if not call_codes:
            raise RuntimeError("no contracts available from M3")
        sample   = call_codes[:5]
        ret, snap = quote_ctx.get_market_snapshot(sample)
        if ret != 0:
            raise RuntimeError(f"get_market_snapshot failed: {snap}")
        if len(snap) == 0:
            raise RuntimeError("empty snapshot returned")
        # Check for valid Greeks (may be 0 outside market hours)
        has_delta = "option_delta" in snap.columns
        deltas    = snap["option_delta"].tolist() if has_delta else []
        non_zero  = [d for d in deltas if d != 0]
        if non_zero:
            record("M4", f"Option Greeks    (δ sample: {[round(d,2) for d in deltas[:3]]})", True,
                   f"Greeks live | {len(snap)} contracts snapshotted")
        else:
            record("M4", "Option Greeks    (deltas all zero)", True, warn=True,
                   message="Greeks are zero — normal outside market hours (09:30–16:00 ET)")
    except Exception as e:
        record("M4", "Option Greeks", False, str(e))

    # ── M5: Account info ──────────────────────────────────────────
    try:
        trade_ctx = mm.OpenSecTradeContext(
            filter_trdmarket=mm.TrdMarket.US,
            host=host, port=port,
            security_firm=mm.SecurityFirm.FUTUSECURITIES
        )
        ret, funds = trade_ctx.accinfo_query(
            trd_env=trade_env, acc_id=option_acc
        )
        if ret != 0:
            raise RuntimeError(f"accinfo_query failed: {funds}")
        row   = funds.iloc[0]
        cash  = float(row.get("cash", 0))
        total = float(row.get("total_assets", 0))
        record("M5", "Account info     (paper options account)", True,
               f"total_assets=${total:,.2f} | cash=${cash:,.2f}")
    except Exception as e:
        record("M5", "Account info     (paper options account)", False, str(e))
    finally:
        if quote_ctx:
            try: quote_ctx.close()
            except: pass
        if trade_ctx:
            try: trade_ctx.close()
            except: pass


# ══════════════════════════════════════════════════════════════════════════════
#  Yahoo Finance checks
# ══════════════════════════════════════════════════════════════════════════════

def check_yahoo(config, watchlist):
    section("Yahoo Finance  (historical data · VIX · earnings)")

    try:
        import yfinance as yf
    except ImportError:
        record("Y1", "Daily OHLCV", False,
               "yfinance not installed. Run: pip3 install yfinance")
        for cid in ["Y2", "Y3"]:
            record(cid, "skipped — yfinance missing", None)
        return

    symbol  = watchlist[0]
    ticker  = symbol.replace("US.", "").replace("HK.", "")

    # ── Y1: Daily OHLCV ───────────────────────────────────────────
    try:
        data = yf.download(ticker, period="5d", interval="1d",
                           progress=False, auto_adjust=True)
        if data is None or len(data) == 0:
            raise RuntimeError("no data returned")
        if isinstance(data.columns, __import__("pandas").MultiIndex):
            data.columns = data.columns.get_level_values(0)
        data.columns = [c.lower() for c in data.columns]
        last_close = float(data["close"].iloc[-1])
        last_date  = str(data.index[-1].date())
        record("Y1", f"Daily OHLCV      ({ticker})", True,
               f"last close=${last_close:.2f} on {last_date}")
    except Exception as e:
        record("Y1", f"Daily OHLCV      ({ticker})", False, str(e))

    # ── Y2: VIX ───────────────────────────────────────────────────
    try:
        vdata = yf.download("^VIX", period="5d", interval="1d",
                            progress=False, auto_adjust=True)
        if vdata is None or len(vdata) == 0:
            raise RuntimeError("no VIX data returned")
        if isinstance(vdata.columns, __import__("pandas").MultiIndex):
            vdata.columns = vdata.columns.get_level_values(0)
        vdata.columns = [c.lower() for c in vdata.columns]
        vix     = float(vdata["close"].iloc[-1])
        vix_cfg = config.get("regime", {}).get("high_vol_vix_threshold", 25.0)
        warn    = vix >= vix_cfg
        record("Y2", f"Current VIX      ({vix:.1f})", True, warn=warn,
               message=(f"VIX={vix:.1f} is above high_vol threshold ({vix_cfg}) — "
                        f"bot will block new positions" if warn else
                        f"VIX={vix:.1f} | high_vol threshold={vix_cfg}"))
    except Exception as e:
        record("Y2", "Current VIX", False, str(e))

    # ── Y3: Earnings dates ────────────────────────────────────────
    try:
        t       = yf.Ticker(ticker)
        cal     = t.calendar
        today   = date.today()
        upcoming = []
        if cal is not None:
            ev = cal.get("Earnings Date")
            if ev:
                if not isinstance(ev, list): ev = [ev]
                for v in ev:
                    try:
                        d = __import__("pandas").Timestamp(v).date()
                        if d >= today:
                            upcoming.append(d)
                    except: pass
        if upcoming:
            buffer = config.get("options", {}).get("earnings_buffer_days", 7)
            days_away = (upcoming[0] - today).days
            warn = days_away <= buffer
            record("Y3", f"Earnings dates   ({ticker})", True, warn=warn,
                   message=(f"Next earnings in {days_away} days ({upcoming[0]}) — "
                            f"within {buffer}-day buffer! Bot may skip trades." if warn
                            else f"Next earnings: {upcoming[0]} ({days_away} days away)"))
        else:
            record("Y3", f"Earnings dates   ({ticker})", True,
                   "No upcoming earnings found (or earnings date not published yet)")
    except Exception as e:
        record("Y3", f"Earnings dates   ({ticker})", False, str(e))


# ══════════════════════════════════════════════════════════════════════════════
#  IBKR checks
# ══════════════════════════════════════════════════════════════════════════════

def check_ibkr(config, watchlist):
    section("Interactive Brokers TWS  (order execution)")

    try:
        from ib_insync import IB, Stock, util
    except ImportError:
        record("I1", "TWS connection", False,
               "ib_insync not installed. Run: pip3 install ib_insync")
        for cid in ["I2", "I3", "I4", "I5", "I6", "I7"]:
            record(cid, "skipped — ib_insync missing", None)
        return

    util.logToConsole(level="ERROR")   # suppress ib_insync noise

    host      = config["ibkr"]["host"]
    port      = config["ibkr"]["port"]
    client_id = 99    # use high ID to avoid conflict with running bot (client_id=1)
    account   = config["ibkr"].get("account", "")
    symbol    = watchlist[0].replace("US.", "").replace("HK.", "")

    ib = IB()

    # ── I1: Connection ────────────────────────────────────────────
    try:
        ib.connect(host, port, clientId=client_id, timeout=10, readonly=True)
        env = "LIVE" if not (account or "").startswith("D") else "PAPER"
        record("I1", f"TWS connection   ({host}:{port})", True,
               f"Connected in read-only mode | env={env}")
    except Exception as e:
        record("I1", f"TWS connection   ({host}:{port})", False,
               f"{e}\n"
               f"Checklist:\n"
               f"  • Is TWS open and logged in?\n"
               f"  • Edit → Global Config → API → Settings\n"
               f"    ✓ Enable ActiveX and Socket Clients\n"
               f"    ✓ Socket port: {port}\n"
               f"    ✓ Trusted IPs: 127.0.0.1")
        for cid in ["I2", "I3", "I4", "I5", "I6", "I7"]:
            record(cid, "skipped — TWS unavailable", None)
        return

    # ── I2: Account identity ──────────────────────────────────────
    try:
        accounts = ib.managedAccounts()
        if not accounts:
            raise RuntimeError("no managed accounts returned")
        active = accounts[0]
        if account and active != account:
            raise RuntimeError(
                f"Connected to account {active} but config specifies {account}. "
                f"Update ibkr.account in config.yaml or log into the correct account."
            )
        env  = "PAPER" if active.startswith("D") else "LIVE"
        record("I2", f"Account identity ({active}  [{env}])", True)
    except Exception as e:
        record("I2", "Account identity", False, str(e))
        active = account or accounts[0] if ib.isConnected() else ""

    # ── I3: Account balance ───────────────────────────────────────
    try:
        summary  = ib.accountSummary(active)
        # Currency is a per-item attribute, not a standalone tag.
        # Find the currency reported alongside NetLiquidation directly.
        nl_item  = next((s for s in summary if s.tag == "NetLiquidation"), None)
        cv_item  = next((s for s in summary if s.tag == "TotalCashValue"), None)
        net_liq  = float(nl_item.value) if nl_item else 0.0
        cash     = float(cv_item.value) if cv_item else 0.0
        currency = nl_item.currency if nl_item and nl_item.currency else "USD"
        if net_liq == 0:
            record("I3", "Account balance", True, warn=True,
                   message=f"Net liquidation = $0 — account may not be funded yet")
        else:
            record("I3", f"Account balance  (net_liq={currency} {net_liq:,.2f})", True,
                   f"cash={currency} {cash:,.2f}")
    except Exception as e:
        record("I3", "Account balance", False, str(e))

    # ── I4: Stock positions ───────────────────────────────────────
    try:
        positions = ib.positions(active)
        stocks    = [p for p in positions if p.contract.secType == "STK"]
        # Check for each watchlist symbol
        found_any = False
        lines     = []
        for sym_full in watchlist:
            sym = sym_full.replace("US.", "").replace("HK.", "")
            pos = next((p for p in stocks if p.contract.symbol == sym), None)
            if pos:
                lines.append(f"{sym}: {int(pos.position)} shares @ ${pos.avgCost:.2f}")
                found_any = True
            else:
                cfg_held = config.get("universe", {}).get("shares_held", {}).get(sym_full, 0)
                if cfg_held > 0:
                    lines.append(f"{sym}: not in IBKR account "
                                 f"(config override = {cfg_held} shares — covered calls will use override)")
                else:
                    lines.append(f"{sym}: no shares held (bear call spreads only)")
        record("I4", "Stock positions  (watchlist symbols)", True,
               "\n".join(lines))
    except Exception as e:
        record("I4", "Stock positions", False, str(e))

    # ── I5: Option positions ──────────────────────────────────────
    try:
        positions = ib.positions(active)
        opts      = [p for p in positions if p.contract.secType == "OPT"]
        if not opts:
            record("I5", "Option positions (none open)", True)
        else:
            lines = []
            for p in opts:
                c = p.contract
                lines.append(
                    f"{c.symbol} {c.lastTradeDateOrContractMonth} "
                    f"{c.right}{c.strike}  qty={int(p.position)}  cost={p.avgCost:.2f}"
                )
            record("I5", f"Option positions ({len(opts)} open)", True,
                   "\n".join(lines))
    except Exception as e:
        record("I5", "Option positions", False, str(e))

    # ── I6: Option chain (no subscription needed) ─────────────────
    try:
        stock = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(stock)
        chains = ib.reqSecDefOptParams(
            underlyingSymbol=symbol,
            futFopExchange="",
            underlyingSecType="STK",
            underlyingConId=stock.conId,
        )
        smart = next((c for c in chains if c.exchange == "SMART"), None)
        if not smart:
            raise RuntimeError("no SMART exchange chain found")
        today_str = date.today().strftime("%Y%m%d")
        upcoming  = sorted([e for e in smart.expirations if e >= today_str])[:5]
        iso_dates = [f"{e[:4]}-{e[4:6]}-{e[6:]}" for e in upcoming]
        record("I6", f"Option chain     ({symbol})", True,
               f"{len(smart.strikes)} strikes | {len(smart.expirations)} expiries | "
               f"next 3: {iso_dates[:3]}")
    except Exception as e:
        record("I6", f"Option chain     ({symbol})", False, str(e))

    # ── I7: Live price (requires market data subscription) ────────
    try:
        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)
        ticker = ib.reqMktData(contract, snapshot=True)
        ib.sleep(2)
        price  = None
        if ticker.last and ticker.last > 0 and not math.isnan(ticker.last):
            price = ticker.last
        elif (ticker.bid and ticker.ask and
              ticker.bid > 0 and ticker.ask > 0 and
              not math.isnan(ticker.bid) and not math.isnan(ticker.ask)):
            price = (ticker.bid + ticker.ask) / 2
        ib.cancelMktData(contract)

        if price:
            record("I7", f"Live price       ({symbol} = ${price:.2f})", True,
                   "Market data subscription active ✅")
        else:
            record("I7", f"Live price       ({symbol})", True, warn=True,
                   message="Price returned as NaN — market data subscription not active.\n"
                           "The bot will use Yahoo Finance for spot price (hybrid mode).\n"
                           "To fix: subscribe to 'US Equity and Options Add-On Streaming Bundle'\n"
                           "in IBKR Account Management → Settings → Market Data Subscriptions.")
    except Exception as e:
        record("I7", f"Live price       ({symbol})", False, str(e))
    finally:
        try:
            ib.disconnect()
        except:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Pre-flight connectivity check for the options trading bot"
    )
    parser.add_argument("--symbol",       default=None,
                        help="Override watchlist symbol for tests (e.g. AAPL)")
    parser.add_argument("--skip-moomoo",  action="store_true",
                        help="Skip all MooMoo checks")
    parser.add_argument("--skip-ibkr",    action="store_true",
                        help="Skip all IBKR checks")
    parser.add_argument("--skip-yahoo",   action="store_true",
                        help="Skip all Yahoo Finance checks")
    args = parser.parse_args()

    # ── Load config ───────────────────────────────────────────────
    try:
        config = load_config()
    except FileNotFoundError:
        print(red(f"\n❌  Config file not found: {CONFIG_PATH}"))
        print("    Run from the project root directory.")
        sys.exit(1)
    except Exception as e:
        print(red(f"\n❌  Failed to load config: {e}"))
        sys.exit(1)

    watchlist = config.get("universe", {}).get("watchlist", ["US.TSLA"])
    if args.symbol:
        symbol_arg = args.symbol.upper()
        # Accept plain "AAPL" or prefixed "US.AAPL"
        watchlist = [symbol_arg if symbol_arg.startswith("US.") else f"US.{symbol_arg}"]

    broker_cfg  = config.get("broker", {})
    data_broker = broker_cfg.get("data", "moomoo")
    exec_broker = broker_cfg.get("execution", "ibkr")

    # ── Header ────────────────────────────────────────────────────
    print()
    print("═" * 62)
    print(bold("  Options Bot — Pre-Flight Connectivity Check"))
    print("═" * 62)
    print(f"  Config      : {CONFIG_PATH}")
    print(f"  Mode        : {bold(config.get('mode', 'paper').upper())}")
    print(f"  Data broker : {bold(data_broker)}")
    print(f"  Exec broker : {bold(exec_broker)}")
    print(f"  Watchlist   : {', '.join(watchlist)}")
    print(f"  Time        : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # ── Run checks ────────────────────────────────────────────────
    # MooMoo: run if it's used for data OR execution
    if not args.skip_moomoo and (data_broker == "moomoo" or exec_broker == "moomoo"):
        check_moomoo(config, watchlist)
    elif args.skip_moomoo:
        section("MooMoo OpenD  (skipped)")
    else:
        section("MooMoo OpenD  (not configured — skipping)")

    # Yahoo Finance: always needed (OHLCV, VIX, earnings dates)
    if not args.skip_yahoo:
        check_yahoo(config, watchlist)
    else:
        section("Yahoo Finance  (skipped)")

    # IBKR: run if it's used for data OR execution
    if not args.skip_ibkr and (data_broker == "ibkr" or exec_broker == "ibkr"):
        check_ibkr(config, watchlist)
    elif args.skip_ibkr:
        section("Interactive Brokers TWS  (skipped)")
    else:
        section("Interactive Brokers TWS  (not configured — skipping)")

    # ── Summary ───────────────────────────────────────────────────
    all_passed = summary()
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
