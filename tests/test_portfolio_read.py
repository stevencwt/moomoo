"""
Portfolio Read Test
====================
Connects to MooMoo OpenD and reads your real account positions.

Usage:
    python3 tests/test_portfolio_read.py

What it does:
  1. Unlocks real trading using your MooMoo 6-digit trading PIN
  2. Lists all accounts (sim + real)
  3. Shows all positions per account
"""

import moomoo as mm
import getpass

HOST = "127.0.0.1"
PORT = 11111

print("\n" + "═" * 60)
print("  MooMoo Portfolio Read Test")
print("═" * 60)

# ── Connect ───────────────────────────────────────────────────────
print("\n[1] Connecting to OpenD...")
trade_ctx = mm.OpenSecTradeContext(host=HOST, port=PORT, is_encrypt=False)

# ── Unlock real trading ───────────────────────────────────────────
print("\n[2] Unlocking real trading account...")
print("    (Enter your MooMoo 6-digit trading PIN)")
pin = getpass.getpass("    Trading PIN: ")

ret, data = trade_ctx.unlock_trade(pin)
if ret != 0:
    print(f"    ❌ Unlock failed: {data}")
    print("    → Check your trading PIN and try again")
else:
    print("    ✅ Unlocked successfully")

# ── List all accounts ─────────────────────────────────────────────
print("\n[3] Account list:")
ret, acc_data = trade_ctx.get_acc_list()
if ret != 0:
    print(f"    ❌ Failed: {acc_data}")
else:
    cols = [c for c in ["acc_id", "trd_env", "acc_type", "security_firm",
                        "acc_status"] if c in acc_data.columns]
    print(acc_data[cols].to_string(index=False))
    print()

    real_accounts = acc_data[acc_data["trd_env"] == mm.TrdEnv.REAL]
    sim_accounts  = acc_data[acc_data["trd_env"] == mm.TrdEnv.SIMULATE]
    print(f"    Simulator accounts : {len(sim_accounts)}")
    print(f"    Real accounts      : {len(real_accounts)}")

# ── Query positions from EVERY account ───────────────────────────
print("\n[4] Positions by account:")
print("─" * 60)

if ret == 0:
    for _, row in acc_data.iterrows():
        acc_id    = int(row["acc_id"])
        trd_env   = row["trd_env"]
        acc_type  = row.get("acc_type", "unknown")
        env_label = "REAL" if trd_env == mm.TrdEnv.REAL else "SIMULATE"

        print(f"\n  Account {acc_id} [{env_label}] type={acc_type}")

        ret2, pos = trade_ctx.position_list_query(
            trd_env=trd_env,
            acc_id=acc_id
        )
        if ret2 != 0:
            print(f"    ❌ Could not read: {pos}")
            continue

        if len(pos) == 0:
            print("    (no positions)")
            continue

        cols = [c for c in ["code", "stock_name", "qty", "cost_price",
                             "market_val", "pl_ratio"] if c in pos.columns]
        print(pos[cols].to_string(index=False))

# ── Cleanup ───────────────────────────────────────────────────────
trade_ctx.close()
print("\n" + "═" * 60)
print("  Done")
print("═" * 60 + "\n")