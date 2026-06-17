"""
Migration script: clear legacy MBIE/CSV firm identification rows from bidder_pool.

Identifies all notices that have mbie_evidence or csv_inferred rows but NO
ach_analysis rows.  These notices were processed by the old pipeline where MBIE
category matching was used to generate firm names.  The results are unreliable
(e.g. "testing" → lab equipment suppliers for a cognitive assessment notice).

Clearing these rows allows the ACH pipeline to re-run web search for correct
firm identification on the next Layer 2 cycle.

Usage:
    railway run python3 _migrate_bidder_pool.py            # apply
    railway run python3 _migrate_bidder_pool.py --dry-run  # preview only
"""
import logging
import sys

import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger(__name__)


def identify_stale_notices() -> list[str]:
    """
    Find notices with mbie_evidence or csv_inferred rows but no ach_analysis rows.
    """
    rows = db.fetchall(
        """
        SELECT DISTINCT notice_id
          FROM bidder_pool
         WHERE match_type IN ('mbie_evidence', 'csv_inferred')
           AND notice_id NOT IN (
               SELECT DISTINCT notice_id
                 FROM bidder_pool
                WHERE match_type = 'ach_analysis'
           )
         ORDER BY notice_id
        """
    )
    return [r["notice_id"] for r in rows]


def migrate(dry_run: bool = False) -> None:
    stale = identify_stale_notices()
    logger.info(
        "Found %d notice(s) with stale MBIE/CSV rows and no ACH analysis.",
        len(stale),
    )

    if not stale:
        logger.info("Nothing to migrate.")
        return

    total_deleted = 0
    for nid in stale:
        counts = db.fetchone(
            """
            SELECT
                COUNT(*) FILTER (WHERE match_type = 'mbie_evidence') AS mbie_count,
                COUNT(*) FILTER (WHERE match_type = 'csv_inferred')  AS csv_count
              FROM bidder_pool
             WHERE notice_id = %s
            """,
            (nid,),
        )
        mbie_n = int((counts or {}).get("mbie_count") or 0)
        csv_n  = int((counts or {}).get("csv_count") or 0)
        logger.info(
            "  Notice %s: %d mbie_evidence + %d csv_inferred row(s) to clear",
            nid, mbie_n, csv_n,
        )

        if not dry_run:
            db.execute(
                """
                DELETE FROM bidder_pool
                 WHERE notice_id = %s
                   AND match_type IN ('mbie_evidence', 'csv_inferred')
                """,
                (nid,),
            )
            total_deleted += mbie_n + csv_n

    if dry_run:
        logger.info(
            "DRY RUN complete — no changes made. "
            "Run without --dry-run to clear %d notice(s).",
            len(stale),
        )
    else:
        logger.info(
            "Migration complete. Cleared %d row(s) across %d notice(s). "
            "ACH pipeline will re-identify firms via web search on next run.",
            total_deleted,
            len(stale),
        )


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    migrate(dry_run=dry_run)
