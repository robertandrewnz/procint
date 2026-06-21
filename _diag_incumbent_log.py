"""
Audit log for incumbent detection pipeline runs.

Usage:
    railway run python3 _diag_incumbent_log.py [--all] [--notice <notice_id>]

Flags:
    --all              Show all rows (default: last 50)
    --notice <id>      Filter to a specific notice_id
"""
import sys
import logging

logging.basicConfig(level=logging.WARNING)

import db

args = sys.argv[1:]
SHOW_ALL   = "--all" in args
NOTICE_ID  = None
if "--notice" in args:
    idx = args.index("--notice")
    if idx + 1 < len(args):
        NOTICE_ID = args[idx + 1]

LIMIT = 999999 if SHOW_ALL else 50
DIVIDER = "\n" + "=" * 72 + "\n"

# ── Table existence check ─────────────────────────────────────────────────────
try:
    db.fetchone("SELECT 1 FROM incumbent_detection_log LIMIT 1")
except Exception:
    print("incumbent_detection_log table does not exist yet.")
    print("Deploy latest main and let the app restart to create it, then re-run.")
    sys.exit(1)

# ── Query ─────────────────────────────────────────────────────────────────────
where  = "WHERE notice_id = %s" if NOTICE_ID else ""
params = (NOTICE_ID,) if NOTICE_ID else ()

rows = db.fetchall(
    f"""
    SELECT l.notice_id, l.agency, l.title, l.firm_found,
           l.stored, l.error_message, l.run_at
      FROM incumbent_detection_log l
     {where}
     ORDER BY l.run_at DESC
     LIMIT {LIMIT}
    """,
    params,
)

# ── Summary counts ────────────────────────────────────────────────────────────
print(DIVIDER)
print(f"Incumbent Detection Log — last {LIMIT if not SHOW_ALL else 'all'} rows")
if NOTICE_ID:
    print(f"Filtered to notice: {NOTICE_ID}")
print()

total   = len(rows)
found   = sum(1 for r in rows if r.get("firm_found"))
stored  = sum(1 for r in rows if r.get("stored"))
errors  = sum(1 for r in rows if r.get("error_message"))

print(f"  Total runs:      {total}")
print(f"  Incumbent found: {found}")
print(f"  Stored to DB:    {stored}")
print(f"  Errors:          {errors}")
print()

if not rows:
    print("  No rows — the pipeline has not run incumbent detection yet")
    print("  (or the table was just created and no runs have completed).")
    print(DIVIDER)
    sys.exit(0)

# ── Row detail ────────────────────────────────────────────────────────────────
print(f"  {'notice_id':<12}  {'run_at':<20}  {'firm_found':<22}  {'stored':<6}  {'error'}")
print(f"  {'-'*12}  {'-'*20}  {'-'*22}  {'-'*6}  {'-'*40}")

for r in rows:
    ts         = str(r.get("run_at") or "")[:19]
    firm       = (r.get("firm_found") or "—")[:22]
    stored_str = "YES" if r.get("stored") else "no"
    err        = (r.get("error_message") or "")[:60]
    nid        = str(r.get("notice_id") or "")[:12]
    print(f"  {nid:<12}  {ts:<20}  {firm:<22}  {stored_str:<6}  {err}")

# ── Notices with repeated failures ────────────────────────────────────────────
if not NOTICE_ID:
    failed_rows = [r for r in rows if not r.get("stored") and not r.get("firm_found") and r.get("error_message")]
    if failed_rows:
        print()
        print("  Notices with errors (no incumbent stored, error present):")
        seen = set()
        for r in failed_rows:
            nid = r.get("notice_id")
            if nid not in seen:
                seen.add(nid)
                title = (r.get("title") or "")[:50]
                err   = (r.get("error_message") or "")[:80]
                print(f"    {nid}  {title!r}  err={err!r}")

print(DIVIDER)
print("Done.")
