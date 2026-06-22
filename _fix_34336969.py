"""
One-shot fix for notice 34336969 — removes the bad service-description row
from bidder_pool and re-runs ACH to generate correct likely bidders.

Usage:
    railway run python3 _fix_34336969.py
"""
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s — %(message)s")

import db

NOTICE_ID = "34336969"

# ── 1. Remove bad rows ────────────────────────────────────────────────────────
deleted = db.fetchone(
    """
    WITH del AS (
        DELETE FROM bidder_pool
         WHERE notice_id = %s
           AND match_type = 'ach_analysis'
           AND (LOWER(firm_name) LIKE '%%core service%%'
                OR LOWER(firm_name) LIKE '%%three waters operations%%'
                OR LOWER(firm_name) LIKE '%%operations and maintenance%%')
         RETURNING firm_name
    )
    SELECT STRING_AGG(firm_name, ', ') AS removed FROM del
    """,
    (NOTICE_ID,),
)
removed = (deleted or {}).get("removed") or "(none matched)"
print(f"Deleted bad rows: {removed}")

# ── 2. Delete ALL ach_analysis rows so re-run starts clean ───────────────────
db.execute(
    "DELETE FROM bidder_pool WHERE notice_id = %s AND match_type = 'ach_analysis'",
    (NOTICE_ID,),
)
print("Cleared all ach_analysis rows for notice — ready for fresh run.")

# ── 3. Fetch notice details ───────────────────────────────────────────────────
notice_row = db.fetchone(
    """
    SELECT r.notice_id, r.title, r.agency, r.description,
           r.overview_text, p.sector_tag
      FROM raw_notices r
      JOIN enriched_notices e ON e.notice_id = r.notice_id
      JOIN parsed_notices   p ON p.notice_id = r.notice_id
     WHERE r.notice_id = %s
    """,
    (NOTICE_ID,),
)
if not notice_row:
    print(f"ERROR: notice {NOTICE_ID} not found in DB — cannot re-run ACH.")
    sys.exit(1)

notice = {
    "notice_id":     NOTICE_ID,
    "title":         notice_row.get("title") or "",
    "agency":        notice_row.get("agency") or "",
    "description":   notice_row.get("description") or "",
    "overview_text": notice_row.get("overview_text") or "",
    "sector_tag":    notice_row.get("sector_tag") or "infrastructure",
}
print(f"Notice: {notice['title']!r} — {notice['agency']} — sector={notice['sector_tag']}")

# ── 4. Re-run ACH ─────────────────────────────────────────────────────────────
from bidder_intelligence import generate_bidder_intelligence, store_ach_results

print("Running ACH analysis (web search + MBIE validation + Claude assessment)…")
bidders = generate_bidder_intelligence(notice, show_reasoning=True)

if not bidders:
    print("ACH returned no bidders — web search found no candidates for this notice.")
    sys.exit(0)

print(f"ACH produced {len(bidders)} bidder(s):")
for b in bidders:
    print(f"  {b['name']!r:40s} probability={b.get('probability')} cap={b.get('capability_match')}")

store_ach_results(NOTICE_ID, bidders)
print("Done — new ACH bidders stored in bidder_pool.")
