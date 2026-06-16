"""
Diagnostic script for notice 34279032 bidder display.
Run with: railway run python3 diag_34279032.py
"""
import db
from bidder_intelligence import _ach_relevance_gate, _gate_title_keywords

NOTICE_ID = "34279032"

print("=" * 70)
print(f"DIAGNOSTIC: notice {NOTICE_ID}")
print("=" * 70)

# ── 1. Raw bidder_pool rows ───────────────────────────────────────────────────
print("\n[1] ALL bidder_pool rows for this notice:\n")
rows = db.fetchall(
    """
    SELECT match_type, firm_name, relevance_score, sector,
           LEFT(reasoning, 120) AS reasoning_snippet
      FROM bidder_pool
     WHERE notice_id = %s
     ORDER BY CASE match_type WHEN 'ach_analysis' THEN 0 ELSE 1 END,
              relevance_score DESC NULLS LAST
    """,
    (NOTICE_ID,),
)
if not rows:
    print("  (no rows in bidder_pool)")
for r in rows:
    print(f"  match_type={r['match_type']!r:20} firm={r['firm_name']!r}")
    print(f"    relevance_score={r['relevance_score']}  sector={r['sector']!r}")
    print(f"    reasoning: {r['reasoning_snippet']}")
    print()

# ── 2. Gate trace ─────────────────────────────────────────────────────────────
print("\n[2] Gate trace:\n")
title_row = db.fetchone(
    "SELECT title FROM raw_notices WHERE notice_id = %s", (NOTICE_ID,)
)
notice_title = (title_row or {}).get("title") or ""
print(f"  Notice title: {notice_title!r}")

kws = _gate_title_keywords(notice_title)
print(f"  Gate keywords: {kws}")

ach_rows = [r for r in rows if r.get("match_type") == "ach_analysis"]
print(f"  ACH rows count: {len(ach_rows)}")

for b in ach_rows:
    name_text = (b.get("firm_name") or "").lower()
    evidence = b.get("evidence") or []
    discriminator = b.get("discriminator") or ""
    reasoning_raw = b.get("reasoning") or ""
    parts = [p.strip() for p in reasoning_raw.split("|") if p.strip()
             and not p.strip().startswith("CAPMATCH:")]
    all_text = " ".join([name_text, " ".join(str(e) for e in evidence),
                         discriminator, " ".join(parts)]).lower()
    hits = [kw for kw in kws if kw in all_text]
    print(f"  {b['firm_name']!r}: keyword hits={hits} -> {'PASS' if hits else 'FAIL'}")

gate_result = _ach_relevance_gate(ach_rows, notice_title) if ach_rows else None
print(f"\n  _ach_relevance_gate result: {gate_result}  (True=pass/show, False=block)")

# ── 3. Confirm which code path portal.py watchlist uses ──────────────────────
print("\n[3] Portal.py watchlist code path:")
print("  portal.py:3944-3948 — ACH rows used directly with NO gate check.")
print("  _fetch_top_bidders() in output.py is NOT called by the watchlist.")
print("  The fix to output.py did not affect what the user sees in the browser.")
print()
print("  Fix needed: apply gate at portal.py:3944-3948 before assigning ach_rows.")
print("=" * 70)
