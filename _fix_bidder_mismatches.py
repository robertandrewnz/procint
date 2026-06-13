"""
Purge and re-run bidder matching for all notices with sector mismatches.

Finds every active watchlist notice whose mbie_evidence bidder records have a
sector that is excluded from the notice's sector (same logic as QA audit check 1).
Deletes the bad mbie_evidence and csv_inferred rows, then re-runs
score_bidders_for_notice() with the corrected exclusion logic so the records
are replaced with clean data.

ACH (ach_analysis) rows are NOT touched.

Run:
    railway run python3 _fix_bidder_mismatches.py          # dry run (report only)
    railway run python3 _fix_bidder_mismatches.py --fix    # apply deletions + re-run
"""

import sys
import logging
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

import db
import config
from bidders import SECTOR_EXCLUSION_MATRIX, score_bidders_for_notice, _store_bidders, load_bidders

FIX_MODE = "--fix" in sys.argv

_PHYSICAL_WORKS = {"construction", "roading", "civil", "infrastructure", "fm"}
_PHYSICAL_TITLE_SIGNALS = {
    "building", "construct", "infrastructure", "roading", "maintenance",
    "civil", "facility", "upgrade", "installation", "earthworks", "structural",
    "bridge", "pavement", "drainage", "demolition", "fitout",
}
_SERVICES_SIGNALS = {
    "advisory", "consulting", "professional services", "management services",
    "strategy", "research", "analysis", "training", "audit",
    "software", "ict", "it services", "digital", "technology",
    "platform", "system development", "application", "data", "cyber",
    "recruitment", "legal services", "financial services",
}


def _is_mismatch(firm_sector: str, notice_sector: str, notice_text: str) -> bool:
    fs = (firm_sector or "").lower().strip()
    ns = (notice_sector or "other").lower().strip()
    text = notice_text.lower()

    if not fs:
        return False

    # Rule 1: exclusion matrix
    excluded = {e.lower() for e in SECTOR_EXCLUSION_MATRIX.get(ns, set())}
    if fs in excluded:
        return True

    # Rule 2: physical works firm + unclassified notice with services signals
    notice_is_physical = any(sig in text for sig in _PHYSICAL_TITLE_SIGNALS)
    notice_is_services = not notice_is_physical and any(sig in text for sig in _SERVICES_SIGNALS)
    if fs in _PHYSICAL_WORKS and notice_is_services:
        return True

    # Rule 3: physical works firm in other/unknown sector, no construction signals
    if ns in ("other", "unknown", "") and fs in _PHYSICAL_WORKS and not notice_is_physical:
        return True

    return False


print("\n" + "=" * 70)
print("STEP 1 — Query active watchlist MBIE bidder records")
print("=" * 70)

rows = db.fetchall(
    """
    SELECT
        bp.notice_id,
        r.title          AS notice_title,
        r.agency,
        p.sector_tag     AS notice_sector,
        r.title || ' ' || COALESCE(r.description, '') AS combined_text,
        bp.firm_name,
        bp.match_type,
        wh.primary_sector AS firm_sector
    FROM bidder_pool bp
    JOIN parsed_notices p  ON p.notice_id = bp.notice_id
    JOIN raw_notices r     ON r.notice_id = bp.notice_id
    LEFT JOIN supplier_win_history wh ON wh.supplier_name = bp.firm_name
    WHERE bp.match_type IN ('mbie_evidence', 'csv_inferred')
      AND (r.close_date IS NULL OR r.close_date >= CURRENT_DATE)
      AND EXISTS (
          SELECT 1 FROM scored_notices s
           WHERE s.notice_id = bp.notice_id
             AND (s.composite_score >= %s OR r.category_raw ILIKE '%%advance%%')
      )
    ORDER BY bp.notice_id, bp.firm_name
    """,
    (config.PRIORITY_THRESHOLD,),
)

print(f"Total active mbie_evidence/csv_inferred records: {len(rows)}")

flagged_notices: dict[str, dict] = {}

for row in rows:
    nid = row["notice_id"]
    if _is_mismatch(
        row.get("firm_sector"),
        row.get("notice_sector"),
        row.get("combined_text") or "",
    ):
        if nid not in flagged_notices:
            flagged_notices[nid] = {
                "notice_id":    nid,
                "title":        row.get("notice_title") or "",
                "agency":       row.get("agency") or "",
                "notice_sector": row.get("notice_sector") or "other",
                "bad_firms":    [],
            }
        flagged_notices[nid]["bad_firms"].append(
            f"{row['firm_name']} (firm sector: {row.get('firm_sector') or 'unknown'})"
        )

print(f"\nFlagged: {len(flagged_notices)} notices with sector-mismatched bidders\n")

if not flagged_notices:
    print("Nothing to fix.")
    sys.exit(0)

print("=" * 70)
print("STEP 2 — Mismatch report")
print("=" * 70)

for i, (nid, info) in enumerate(list(flagged_notices.items())[:20], 1):
    print(f"  {i}. {nid}: {info['title'][:65]}")
    print(f"     Sector: {info['notice_sector']}")
    print(f"     Bad:    {'; '.join(info['bad_firms'][:3])}")
    print()

if len(flagged_notices) > 20:
    print(f"  ... and {len(flagged_notices) - 20} more notices.\n")

if not FIX_MODE:
    print("\nRun with --fix to delete bad records and re-run inference.")
    sys.exit(0)

print("=" * 70)
print("STEP 3 — Delete mbie_evidence/csv_inferred records for flagged notices")
print("=" * 70)

affected_ids = list(flagged_notices.keys())

deleted = db.execute(
    """
    DELETE FROM bidder_pool
     WHERE notice_id = ANY(%s)
       AND match_type IN ('mbie_evidence', 'csv_inferred')
    """,
    (affected_ids,),
)
print(f"Deleted non-ACH bidder records for {len(affected_ids)} notices.\n")

print("=" * 70)
print("STEP 4 — Re-run bidder inference with corrected exclusion logic")
print("=" * 70)

notice_rows = db.fetchall(
    """
    SELECT s.notice_id, p.sector_tag, p.value_band, p.geographic_scope,
           r.title, r.description, r.agency, r.category_raw
      FROM scored_notices s
      JOIN parsed_notices p ON p.notice_id = s.notice_id
      JOIN raw_notices r    ON r.notice_id = s.notice_id
     WHERE s.notice_id = ANY(%s)
    """,
    (affected_ids,),
)

all_bidders = load_bidders()
stored = 0
empty = 0
failed = 0

for notice in notice_rows:
    nid = notice["notice_id"]
    try:
        bidders = score_bidders_for_notice(notice, all_bidders)
        if bidders:
            _store_bidders(nid, bidders)
            stored += 1
            logger.info("  ✓ %s — %d bidder(s)", nid, len(bidders))
        else:
            empty += 1
            logger.info("  ○ %s — no bidders (ok)", nid)
    except Exception as exc:
        failed += 1
        logger.warning("  ✗ %s — %s", nid, exc)

print(f"\nRe-run complete:")
print(f"  Stored new bidders: {stored} notices")
print(f"  No bidders found:   {empty} notices (clean — no wrong firms shown)")
print(f"  Errors:             {failed} notices")
print(f"  Total processed:    {len(notice_rows)}")
