"""
Purge HEB Construction from NZDF notice bidder_pool rows and re-run
bidder inference for all affected notices.

Run from ~/Documents/GitHub/Procint:
    railway run python3 _purge_heb_nzdf.py
"""
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("purge_heb")

from unittest.mock import patch
with patch("dotenv.dotenv_values", return_value={}):
    import config

import db
import bidders as bidders_mod


def main():
    # 1. Find all notices where HEB Construction appears
    affected = db.fetchall(
        """
        SELECT DISTINCT b.notice_id, r.title, p.sector_tag, p.days_until_close
        FROM bidder_pool b
        JOIN raw_notices r ON r.notice_id = b.notice_id
        JOIN parsed_notices p ON p.notice_id = b.notice_id
        WHERE LOWER(b.firm_name) = 'heb construction'
        ORDER BY p.days_until_close ASC NULLS LAST
        """
    )

    logger.info("Found HEB Construction in %d notices total", len(affected))

    # 2. Filter to NZDF / defence notices — sector_tag = 'defence' OR
    #    title contains NZDF keywords (some are mis-tagged)
    NZDF_KEYWORDS = ["nzdf", "nz defence force", "new zealand defence force",
                     "rnzaf", "rnzn", "nz army", "new zealand army", "military"]

    nzdf_notices = [
        n for n in affected
        if (n.get("sector_tag") or "").lower() == "defence"
        or any(kw in (n.get("title") or "").lower() for kw in NZDF_KEYWORDS)
    ]

    logger.info("NZDF/defence notices to purge: %d", len(nzdf_notices))
    for n in nzdf_notices:
        logger.info(
            "  [%s | %s] %s",
            n["notice_id"],
            n.get("sector_tag") or "?",
            (n.get("title") or "")[:80],
        )

    if not nzdf_notices:
        logger.info("Nothing to do.")
        return

    nzdf_ids = [n["notice_id"] for n in nzdf_notices]
    placeholders = ",".join(["%s"] * len(nzdf_ids))

    # 3. Purge ALL bidder_pool rows for these notices so re-run is clean
    db.execute(
        f"DELETE FROM bidder_pool WHERE notice_id IN ({placeholders})",
        tuple(nzdf_ids),
    )
    logger.info("Purged all bidder_pool rows for %d notices", len(nzdf_ids))

    # 4. Fetch full notice records for re-inference
    notice_rows = db.fetchall(
        f"""
        SELECT s.notice_id, p.sector_tag, p.value_band, p.geographic_scope,
               r.title, r.description, r.agency, r.category_raw
          FROM scored_notices s
          JOIN parsed_notices p ON p.notice_id = s.notice_id
          JOIN raw_notices r    ON r.notice_id = s.notice_id
         WHERE s.notice_id IN ({placeholders})
        """,
        tuple(nzdf_ids),
    )

    all_bidders = bidders_mod.load_bidders()

    ok = 0
    errors = 0
    for notice in notice_rows:
        try:
            results = bidders_mod.score_bidders_for_notice(notice, all_bidders)
            if results:
                bidders_mod._store_bidders(notice["notice_id"], results)
                logger.info(
                    "Re-ran %s — %d bidder(s) stored: %s",
                    notice["notice_id"],
                    len(results),
                    ", ".join(r["firm_name"] for r in results),
                )
            else:
                logger.info("Re-ran %s — no bidders found", notice["notice_id"])
            ok += 1
        except Exception as exc:
            logger.error("Error re-running %s: %s", notice["notice_id"], exc)
            errors += 1

    logger.info("=" * 50)
    logger.info("Done. Re-ran %d notices: %d ok, %d errors", len(nzdf_ids), ok, errors)


if __name__ == "__main__":
    main()
