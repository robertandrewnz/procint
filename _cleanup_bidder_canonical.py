"""
One-time script: canonicalise firm_name in bidder_pool for all rows that
have an explicit entry in CANONICAL_MAP.

Safe to run multiple times (idempotent).
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import db
from canonical_suppliers import normalise, CANONICAL_MAP, _strip_noise


def safe_canon(name: str):
    """Return canonical name only if an explicit CANONICAL_MAP entry exists."""
    c = CANONICAL_MAP.get(normalise(name))
    if not c:
        c = CANONICAL_MAP.get(_strip_noise(name).lower())
    return c  # None if no explicit mapping


def main():
    rows = db.fetchall(
        "SELECT notice_id, firm_name, relevance_score FROM bidder_pool", ()
    )
    print(f"Total rows: {len(rows)}")

    to_process = [
        (safe_canon(r["firm_name"]), r["notice_id"], r["firm_name"],
         float(r.get("relevance_score") or 0))
        for r in rows
        if safe_canon(r["firm_name"]) and safe_canon(r["firm_name"]) != r["firm_name"]
    ]
    print(f"Rows needing canonical rename: {len(to_process)}")

    renamed = deleted = 0
    for canon, notice_id, old_name, score in to_process:
        existing = db.fetchone(
            "SELECT relevance_score FROM bidder_pool "
            "WHERE notice_id = %s AND firm_name = %s",
            (notice_id, canon),
        )
        if existing:
            ex_score = float(existing.get("relevance_score") or 0)
            if score > ex_score:
                # Raw name has better score — delete canonical row, rename this
                db.execute(
                    "DELETE FROM bidder_pool WHERE notice_id = %s AND firm_name = %s",
                    (notice_id, canon),
                )
                db.execute(
                    "UPDATE bidder_pool SET firm_name = %s "
                    "WHERE notice_id = %s AND firm_name = %s",
                    (canon, notice_id, old_name),
                )
                renamed += 1
            else:
                # Canonical row is better — delete raw-name duplicate
                db.execute(
                    "DELETE FROM bidder_pool WHERE notice_id = %s AND firm_name = %s",
                    (notice_id, old_name),
                )
                deleted += 1
        else:
            # No canonical row — simply rename
            db.execute(
                "UPDATE bidder_pool SET firm_name = %s "
                "WHERE notice_id = %s AND firm_name = %s",
                (canon, notice_id, old_name),
            )
            renamed += 1

        if (renamed + deleted) % 50 == 0:
            print(f"  progress: renamed={renamed} deleted={deleted}")

    print(f"\nComplete — renamed: {renamed}, deleted duplicates: {deleted}")

    # Verify the three test notices
    for nid in ["33731454", "33885951", "33920315"]:
        title_row = db.fetchone(
            "SELECT title FROM raw_notices WHERE notice_id = %s", (nid,)
        )
        top = db.fetchall(
            "SELECT firm_name, relevance_score FROM bidder_pool "
            "WHERE notice_id = %s ORDER BY COALESCE(relevance_score,0) DESC LIMIT 5",
            (nid,),
        )
        print(f"\n{nid}: {(title_row or {}).get('title','')[:60]}")
        for r in top:
            cn = safe_canon(r["firm_name"]) or r["firm_name"]
            flag = "" if cn == r["firm_name"] else f"  (raw — canon={cn})"
            print(f"  {r['firm_name']:40s} {r['relevance_score']}{flag}")


if __name__ == "__main__":
    main()
