"""
Delete pursuit packages from pipeline_outputs with bad client_name values.

"Bad" means: client_name is NULL, empty string, 'BidEdge Admin', 'admin',
or other obvious placeholder/test values.

Run:
    railway run python3 _delete_bad_packages.py          # list only (no changes)
    railway run python3 _delete_bad_packages.py --delete # apply deletions
"""

import sys
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

import db

DELETE_MODE = "--delete" in sys.argv

# Values considered bad client names (case-insensitive)
_BAD_NAMES = {"bidedge admin", "admin", "test", "demo", "placeholder", ""}

print("\n" + "=" * 70)
print("STEP 1 — Query pipeline_outputs for bad client names")
print("=" * 70)

rows = db.fetchall(
    """
    SELECT id, output_type, client_name, client_slug, notice_id,
           filename, run_date
      FROM pipeline_outputs
     WHERE client_name IS NULL
        OR TRIM(LOWER(client_name)) = ANY(%s)
     ORDER BY run_date DESC NULLS LAST
    """,
    (list(_BAD_NAMES),),
)

print(f"Found {len(rows)} packages with bad client_name values.\n")

if not rows:
    print("Nothing to delete.")
    sys.exit(0)

print(f"{'ID':<6} {'Type':<25} {'Client':<20} {'Notice':<15} {'Date'}")
print("-" * 80)
for r in rows:
    run_date = str(r.get("run_date") or "")[:10]
    client   = repr(r.get("client_name"))
    nid      = (r.get("notice_id") or "")[:15]
    otype    = (r.get("output_type") or "")[:24]
    print(f"  {r['id']:<4} {otype:<25} {client:<20} {nid:<15} {run_date}")

if not DELETE_MODE:
    print(f"\nRun with --delete to remove these {len(rows)} packages.")
    sys.exit(0)

print("\n" + "=" * 70)
print("STEP 2 — Delete bad packages")
print("=" * 70)

ids_to_delete = [r["id"] for r in rows]

db.execute(
    "DELETE FROM pipeline_outputs WHERE id = ANY(%s)",
    (ids_to_delete,),
)

print(f"\nDeleted {len(ids_to_delete)} packages:")
for r in rows:
    client = r.get("client_name") or "(null)"
    nid    = r.get("notice_id") or "—"
    otype  = r.get("output_type") or "unknown"
    print(f"  [{r['id']}] {otype} — client: {repr(client)} — notice: {nid}")

print(f"\nTotal deleted: {len(ids_to_delete)}")
