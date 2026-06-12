"""
Audit and repair likely bidder quality in the current watchlist.

Run modes:
  python3 _audit_bidder_quality.py          — report mismatches only (no changes)
  python3 _audit_bidder_quality.py --fix    — report, then delete and re-run affected notices

Usage via Railway:
  railway run python3 _audit_bidder_quality.py
  railway run python3 _audit_bidder_quality.py --fix
"""

import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

import db
import config
from bidders import SECTOR_EXCLUSION_MATRIX, score_bidders_for_notice, _store_bidders

FIX_MODE = "--fix" in sys.argv

# Physical works sectors — should not appear in advisory/services/unclassified notices
_PHYSICAL_WORKS = {"construction", "roading", "civil", "infrastructure", "FM"}
_PHYSICAL_TITLE_SIGNALS = {
    "building", "construct", "infrastructure", "roading", "maintenance",
    "civil", "facility", "upgrade", "installation", "earthworks", "structural",
    "bridge", "pavement", "drainage", "demolition", "fitout",
}


def _is_sector_mismatch(firm_sector: str, notice_sector: str, notice_title: str) -> bool:
    """Return True if this firm's sector is clearly wrong for this notice."""
    fs = (firm_sector or "").lower().strip()
    ns = (notice_sector or "other").lower().strip()
    title_lower = notice_title.lower()

    if not fs:
        return False  # unknown firm sector — can't assess

    # Rule 1: hard exclusion matrix
    if fs in SECTOR_EXCLUSION_MATRIX.get(ns, set()):
        return True

    # Rule 2: physical works firm in unclassified notice with no construction keywords
    if (
        ns in ("other", "unknown", "")
        and fs in _PHYSICAL_WORKS
        and not any(sig in title_lower for sig in _PHYSICAL_TITLE_SIGNALS)
    ):
        return True

    return False


# ── Step 1: Query all current watchlist notices with MBIE bidders ─────────────

print("\n" + "="*70)
print("STEP 1 — Querying current watchlist bidders from bidder_pool")
print("="*70)

rows = db.fetchall(
    """
    SELECT
        bp.notice_id,
        r.title          AS notice_title,
        r.agency,
        p.sector_tag     AS notice_sector,
        bp.firm_name,
        bp.match_type,
        wh.primary_sector AS firm_sector
    FROM bidder_pool bp
    JOIN parsed_notices p  ON p.notice_id = bp.notice_id
    JOIN raw_notices r     ON r.notice_id = bp.notice_id
    LEFT JOIN supplier_win_history wh ON wh.supplier_name = bp.firm_name
    WHERE bp.match_type = 'mbie_evidence'
      AND EXISTS (
          SELECT 1 FROM scored_notices s
           WHERE s.notice_id = bp.notice_id
             AND (s.composite_score >= %s OR r.category_raw ILIKE '%%advance%%')
      )
    ORDER BY bp.notice_id, bp.firm_name
    """,
    (config.PRIORITY_THRESHOLD,),
)

print(f"Found {len(rows)} MBIE bidder entries across watchlist notices.\n")

# ── Step 2: Identify mismatches ────────────────────────────────────────────────

print("="*70)
print("STEP 2 — Assessing sector relevance for each bidder")
print("="*70)

flagged: dict[str, dict] = {}  # notice_id → {notice_info, bad_firms: list}

for row in rows:
    notice_id = row["notice_id"]
    firm_name = row["firm_name"]
    firm_sector = row.get("firm_sector") or ""
    notice_sector = row.get("notice_sector") or "other"
    notice_title = row.get("notice_title") or ""
    agency = row.get("agency") or ""

    if _is_sector_mismatch(firm_sector, notice_sector, notice_title):
        if notice_id not in flagged:
            flagged[notice_id] = {
                "notice_id": notice_id,
                "title": notice_title,
                "agency": agency,
                "notice_sector": notice_sector,
                "bad_firms": [],
            }
        flagged[notice_id]["bad_firms"].append({
            "firm_name": firm_name,
            "firm_sector": firm_sector or "unknown",
        })

# ── Step 3: Report ─────────────────────────────────────────────────────────────

print(f"\n{'='*70}")
print("STEP 3 — FINDINGS REPORT")
print(f"{'='*70}\n")

if not flagged:
    print("✓ No sector mismatches found. All MBIE bidders are sector-appropriate.")
else:
    print(f"⚠  {len(flagged)} notice(s) have wrong-sector bidders:\n")
    for i, (notice_id, info) in enumerate(flagged.items(), 1):
        bad_str = ", ".join(
            f"{f['firm_name']} (sector: {f['firm_sector']})"
            for f in info["bad_firms"]
        )
        print(f"  {i}. Notice {notice_id}")
        print(f"     Title:    {info['title'][:80]}")
        print(f"     Agency:   {info['agency']}")
        print(f"     Sector:   {info['notice_sector']}")
        print(f"     WRONG:    {bad_str}")
        print()

if not FIX_MODE:
    print("\nRun with --fix to delete and re-run bidder inference for flagged notices.")
    sys.exit(0)

# ── Step 4: Re-run bidder inference for affected notices ──────────────────────

print(f"\n{'='*70}")
print("STEP 4 — REPAIRING AFFECTED NOTICES")
print(f"{'='*70}\n")

if not flagged:
    print("Nothing to fix.")
    sys.exit(0)

affected_ids = list(flagged.keys())

# Fetch full notice records for re-running inference
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

from bidders import load_bidders
all_bidders = load_bidders()

repaired = 0
for notice in notice_rows:
    nid = notice["notice_id"]
    title = (notice.get("title") or "")[:60]
    try:
        # Delete existing MBIE bidder entries for this notice
        db.execute(
            "DELETE FROM bidder_pool WHERE notice_id = %s AND match_type = 'mbie_evidence'",
            (nid,),
        )
        # Re-run with fixed logic
        bidders = score_bidders_for_notice(notice, all_bidders)
        if bidders:
            _store_bidders(nid, bidders)
            print(f"  ✓ Repaired notice {nid}: {title} → {len(bidders)} bidder(s) stored")
        else:
            print(f"  ○ Notice {nid}: {title} — no MBIE bidders found after fix (ok)")
        repaired += 1
    except Exception as exc:
        print(f"  ✗ Failed to repair notice {nid}: {exc}")

print(f"\nRepair complete: {repaired}/{len(notice_rows)} notices re-processed.")
