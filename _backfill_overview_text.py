"""
Re-fetch overview_text for notices where it is null.

For each affected notice: fetches the GETS detail page, extracts overview_text,
updates raw_notices, then re-parses key dates (briefing, questions, registration)
and updates parsed_notices.

Run:
    railway run python3 _backfill_overview_text.py           # dry run — active notices only
    railway run python3 _backfill_overview_text.py --fetch   # fetch + update (active only)
    railway run python3 _backfill_overview_text.py --all     # dry run — all notices in DB
    railway run python3 _backfill_overview_text.py --all --fetch  # fetch + update (all)
"""

import sys
import time
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

import db
import config
from ingestion import _fetch_notice_detail, _extract_overview_text
from parsing import extract_key_dates

FETCH_MODE = "--fetch" in sys.argv
ALL_MODE   = "--all"   in sys.argv
RATE_LIMIT_SECONDS = 1.5  # polite delay between GETS requests

print("\n" + "=" * 70)
scope = "all DB notices" if ALL_MODE else "active watchlist notices"
print(f"STEP 1 — Find {scope} with null overview_text")
print("=" * 70)

if ALL_MODE:
    rows = db.fetchall(
        """
        SELECT r.notice_id, r.source_url, r.title, r.agency,
               r.category_raw, r.description, r.close_date
          FROM raw_notices r
         WHERE (r.overview_text IS NULL OR r.overview_text = '')
         ORDER BY r.close_date DESC NULLS LAST
        """,
    )
else:
    rows = db.fetchall(
        """
        SELECT r.notice_id, r.source_url, r.title, r.agency,
               r.category_raw, r.description, r.close_date
          FROM raw_notices r
          JOIN parsed_notices p  ON p.notice_id = r.notice_id
          JOIN scored_notices s  ON s.notice_id = r.notice_id
         WHERE (r.overview_text IS NULL OR r.overview_text = '')
           AND (r.close_date IS NULL OR r.close_date >= CURRENT_DATE)
           AND (s.composite_score >= %s OR r.category_raw ILIKE '%%advance%%')
         ORDER BY r.close_date ASC NULLS LAST
        """,
        (config.PRIORITY_THRESHOLD,),
    )

print(f"Found {len(rows)} {scope} with null overview_text.\n")

for i, r in enumerate(rows[:20], 1):
    dtc_str = f"closes {r['close_date']}" if r.get("close_date") else "close date TBC"
    print(f"  {i}. {r['notice_id']}: {(r['title'] or '')[:60]} ({dtc_str})")

if len(rows) > 20:
    print(f"  ... and {len(rows) - 20} more.\n")

if not rows:
    print("Nothing to do.")
    sys.exit(0)

if not FETCH_MODE:
    all_flag = " --all" if ALL_MODE else ""
    print(f"\nRun with{all_flag} --fetch to re-scrape all {len(rows)} notices.")
    sys.exit(0)

print("\n" + "=" * 70)
print("STEP 2 — Re-fetch detail pages and update overview_text")
print("=" * 70)

fetched_ok = 0
fetched_empty = 0
fetch_failed = 0
dates_updated = 0

for i, row in enumerate(rows, 1):
    nid = row["notice_id"]
    url = row.get("source_url")

    if not url:
        logger.warning("  [%d/%d] %s — no source_url, skipping", i, len(rows), nid)
        fetch_failed += 1
        continue

    try:
        logger.info("  [%d/%d] Fetching %s: %s", i, len(rows), nid, (row["title"] or "")[:50])
        notice_dict = dict(row)
        notice_dict = _fetch_notice_detail(notice_dict)
        overview = notice_dict.get("overview_text") or ""

        if not overview:
            logger.warning("    → overview_text still empty after fetch")
            fetched_empty += 1
        else:
            logger.info("    → overview_text: %d chars", len(overview))
            fetched_ok += 1

        # Update raw_notices.overview_text (and description for backwards compat)
        db.execute(
            """
            UPDATE raw_notices
               SET overview_text = %s,
                   description   = COALESCE(NULLIF(%s, ''), description)
             WHERE notice_id = %s
            """,
            (overview or None, overview or None, nid),
        )

        # Re-extract key dates and update parsed_notices
        key_dates = extract_key_dates(overview) if overview else {}
        if any(v for v in key_dates.values()):
            db.execute(
                """
                UPDATE parsed_notices
                   SET briefing_date         = COALESCE(%s, briefing_date),
                       questions_deadline    = COALESCE(%s, questions_deadline),
                       registration_deadline = COALESCE(%s, registration_deadline),
                       parsed_at             = NOW()
                 WHERE notice_id = %s
                """,
                (
                    key_dates.get("briefing_date"),
                    key_dates.get("questions_deadline"),
                    key_dates.get("registration_deadline"),
                    nid,
                ),
            )
            dates_updated += 1

        time.sleep(RATE_LIMIT_SECONDS)

    except Exception as exc:
        logger.error("  [%d/%d] %s — fetch failed: %s", i, len(rows), nid, exc)
        fetch_failed += 1
        time.sleep(RATE_LIMIT_SECONDS)

print(f"\nBackfill complete:")
print(f"  overview_text populated:  {fetched_ok} notices")
print(f"  Still empty after fetch:  {fetched_empty} notices")
print(f"  Fetch errors:             {fetch_failed} notices")
print(f"  Key dates updated:        {dates_updated} notices")
print(f"  Total processed:          {len(rows)}")
