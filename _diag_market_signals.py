"""
Diagnostic: market_signals table state and timezone analysis.
Usage: railway run python3 _diag_market_signals.py
"""
import sys
sys.path.insert(0, ".")
import db

DIVIDER = "\n" + "=" * 72 + "\n"

print(DIVIDER)
print("Market Signals Diagnostic")

# ── 1. Server timezone and current timestamps ─────────────────────────────────
ts = db.fetchone("""
    SELECT NOW()                                    AS now_utc,
           NOW() AT TIME ZONE 'Pacific/Auckland'    AS now_nzt,
           CURRENT_DATE                             AS current_date_utc,
           (NOW() AT TIME ZONE 'Pacific/Auckland')::date AS current_date_nzt
""")
print("\n--- Server Timestamps ---")
print(f"  NOW() (UTC):         {ts['now_utc']}")
print(f"  NOW() at NZT:        {ts['now_nzt']}")
print(f"  CURRENT_DATE (UTC):  {ts['current_date_utc']}")
print(f"  CURRENT_DATE (NZT):  {ts['current_date_nzt']}")

# ── 2. All rows in market_signals ─────────────────────────────────────────────
all_rows = db.fetchall("""
    SELECT id, user_id, priority,
           generated_at,
           generated_at::date                        AS generated_date_utc,
           (generated_at AT TIME ZONE 'Pacific/Auckland')::date AS generated_date_nzt,
           LEFT(signal, 80)                          AS signal_preview
      FROM market_signals
     ORDER BY generated_at DESC
     LIMIT 20
""")
print(f"\n--- All market_signals rows (newest first, max 20) ---")
print(f"  Total rows returned: {len(all_rows)}")
if all_rows:
    print(f"\n  {'id':<5} {'user_id':<12} {'pri':<6} {'generated_at (UTC)':<22} {'UTC date':<10} {'NZT date':<10} signal")
    print(f"  {'-'*5} {'-'*12} {'-'*6} {'-'*22} {'-'*10} {'-'*10} {'-'*40}")
    for r in all_rows:
        print(f"  {str(r['id']):<5} {str(r['user_id']):<12} {str(r['priority']):<6} "
              f"{str(r['generated_at'])[:22]:<22} {str(r['generated_date_utc']):<10} "
              f"{str(r['generated_date_nzt']):<10} {r['signal_preview']}")
else:
    print("  No rows found in market_signals table.")

# ── 3. What the dashboard query actually finds ─────────────────────────────────
print("\n--- Dashboard query result (generated_at::date = CURRENT_DATE) ---")
for user_id in ["robert", "admin"]:
    found = db.fetchall("""
        SELECT id, priority, generated_at::date AS date_utc
          FROM market_signals
         WHERE user_id = %s
           AND generated_at::date = CURRENT_DATE
         ORDER BY id ASC
         LIMIT 3
    """, (user_id,))
    print(f"  user_id={user_id!r}: {len(found)} row(s) match")

# ── 4. What the NZT-aware query would find ────────────────────────────────────
print("\n--- NZT-aware query result ((generated_at AT TIME ZONE 'Pacific/Auckland')::date = ...) ---")
for user_id in ["robert", "admin"]:
    found = db.fetchall("""
        SELECT id, priority,
               (generated_at AT TIME ZONE 'Pacific/Auckland')::date AS date_nzt
          FROM market_signals
         WHERE user_id = %s
           AND (generated_at AT TIME ZONE 'Pacific/Auckland')::date
               = (NOW() AT TIME ZONE 'Pacific/Auckland')::date
         ORDER BY id ASC
         LIMIT 3
    """, (user_id,))
    print(f"  user_id={user_id!r}: {len(found)} row(s) match")

print(DIVIDER)
print("Done.")
