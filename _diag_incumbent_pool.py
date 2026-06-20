"""
Diagnostic: bidder_pool rows for a given notice, plus incumbent search dry-run.

Usage:
    railway run python3 _diag_incumbent_pool.py [notice_id]

Default notice: 34279032
"""
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")

import db

NOTICE_ID = sys.argv[1] if len(sys.argv) > 1 else "34279032"
DIVIDER = "\n" + "=" * 72 + "\n"


def section(title):
    print(DIVIDER)
    print(title)
    print()


# ── 1. Raw notice metadata ────────────────────────────────────────────────────
section(f"1. Notice metadata — {NOTICE_ID}")

row = db.fetchone(
    """
    SELECT r.notice_id, r.title, r.agency, r.overview_text, r.description,
           p.sector_tag, p.days_until_close,
           e.enriched_at
      FROM raw_notices r
      LEFT JOIN parsed_notices p  ON p.notice_id = r.notice_id
      LEFT JOIN enriched_notices e ON e.notice_id = r.notice_id
     WHERE r.notice_id = %s
    """,
    (NOTICE_ID,),
)

if not row:
    print(f"  *** Notice {NOTICE_ID} not found in raw_notices ***")
    sys.exit(1)

print(f"  Agency:       {row['agency']}")
print(f"  Title:        {row['title']}")
print(f"  Sector:       {row['sector_tag']}")
print(f"  Days to close:{row['days_until_close']}")
print(f"  Enriched at:  {row['enriched_at']}  (None = not in enriched_notices)")
notice_text = (row.get("overview_text") or row.get("description") or "")
print(f"  Notice text:  {notice_text[:200]!r}{'...' if len(notice_text) > 200 else ''}")


# ── 2. All bidder_pool rows ───────────────────────────────────────────────────
section(f"2. All bidder_pool rows — {NOTICE_ID}")

bidders = db.fetchall(
    """
    SELECT firm_name, match_type, relevance_score, strategic_importance,
           intelligence_maturity, reasoning
      FROM bidder_pool
     WHERE notice_id = %s
     ORDER BY match_type, relevance_score DESC NULLS LAST
    """,
    (NOTICE_ID,),
)

if not bidders:
    print("  *** No rows in bidder_pool for this notice ***")
else:
    for b in bidders:
        print(
            f"  [{b['match_type']}]  {b['firm_name']}"
            f"  score={b['relevance_score']}  importance={b['strategic_importance']}"
            f"  maturity={b['intelligence_maturity']}"
        )
        if b.get("reasoning"):
            print(f"    reasoning: {str(b['reasoning'])[:120]}")

incumbent_rows = [b for b in bidders if b["match_type"] == "incumbent_identified"]
print()
print(f"  → match_type='incumbent_identified' rows: {len(incumbent_rows)}")


# ── 3. Dry-run incumbent web search ──────────────────────────────────────────
section("3. Dry-run _web_search_incumbent()")

agency      = row.get("agency") or ""
sector      = row.get("sector_tag") or "other"
title       = row.get("title") or ""
notice_text_trunc = (row.get("overview_text") or row.get("description") or "")[:2000]

print(f"  Inputs:")
print(f"    agency:  {agency!r}")
print(f"    sector:  {sector!r}")
print(f"    title:   {title!r}")
print(f"    text len:{len(notice_text_trunc)} chars")
print()
print("  Running _web_search_incumbent() — this may take 30-60 seconds ...")
print()

try:
    from pursuit_package import (
        _web_search_incumbent,
        _extract_incumbent_firm_name,
        _store_incumbent_in_bidder_pool,
    )

    result = _web_search_incumbent(agency, sector, title, notice_text_trunc)

    print("  RAW RESULT:")
    for line in (result or "").split(". "):
        print(f"    {line.strip()}.")
    print()

    firm_name = _extract_incumbent_firm_name(result or "")
    print(f"  _extract_incumbent_firm_name → {firm_name!r}")

    if firm_name:
        print()
        print("  Would store in bidder_pool — NOT storing (diagnostic only).")
    else:
        print()
        if result and result.startswith("Named incumbent:"):
            print("  Result starts with 'Named incumbent:' but _extract failed — regex issue?")
        elif result and "No named incumbent" in result:
            print("  No named incumbent found — Format C response. Nothing would be stored.")
        else:
            print("  Unexpected result format — check raw result above.")

except Exception as exc:
    print(f"  ERROR: {exc}")
    import traceback
    traceback.print_exc()


print(DIVIDER)
print("Done.")
