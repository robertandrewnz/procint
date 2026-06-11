"""
Manual enrichment status check + run.
Run from ~/Documents/GitHub/Procint with:
    railway run python3 _enrich_manual.py

Does NOT commit anything. Data operation only.
"""
import logging
import sys
from datetime import datetime

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("enrich_manual")

import db
import enrichment


def status_report() -> dict:
    """Query DB and return enrichment status counts."""

    # Notices qualifying for enrichment that have NOT been enriched yet
    awaiting = db.fetchall(
        """
        SELECT r.notice_id, r.title, r.agency,
               p.sector_tag, p.days_until_close,
               s.composite_score,
               (r.overview_text IS NULL OR r.overview_text = '') AS no_overview
        FROM   scored_notices s
        JOIN   raw_notices r    ON r.notice_id = s.notice_id
        JOIN   parsed_notices p ON p.notice_id = s.notice_id
        LEFT JOIN enriched_notices e ON e.notice_id = s.notice_id
        WHERE  (
                   s.composite_score >= %(threshold)s
                OR (p.days_until_close IS NOT NULL AND p.days_until_close BETWEEN 0 AND 14)
               )
          AND  e.notice_id IS NULL
          AND  (r.close_date IS NULL OR r.close_date >= CURRENT_DATE)
        ORDER  BY p.days_until_close ASC NULLS LAST, s.composite_score DESC
        """,
        {"threshold": config.PRIORITY_THRESHOLD},
    )

    # Active notices with null overview_text (enriched or not)
    null_overview = db.fetchall(
        """
        SELECT r.notice_id, r.title, r.agency,
               (e.notice_id IS NOT NULL) AS already_enriched
        FROM   raw_notices r
        LEFT JOIN enriched_notices e ON e.notice_id = r.notice_id
        WHERE  (r.overview_text IS NULL OR r.overview_text = '')
          AND  (r.close_date IS NULL OR r.close_date >= CURRENT_DATE)
        ORDER  BY r.close_date ASC NULLS LAST
        """,
    )

    # Already enriched (active notices)
    already_enriched_count = db.fetchone(
        """
        SELECT COUNT(*) AS n
        FROM   enriched_notices e
        JOIN   raw_notices r ON r.notice_id = e.notice_id
        WHERE  (r.close_date IS NULL OR r.close_date >= CURRENT_DATE)
        """
    )["n"]

    # Total active notices in pipeline
    total_active = db.fetchone(
        """
        SELECT COUNT(*) AS n
        FROM   raw_notices r
        JOIN   parsed_notices p ON p.notice_id = r.notice_id
        JOIN   scored_notices s ON s.notice_id = r.notice_id
        WHERE  (r.close_date IS NULL OR r.close_date >= CURRENT_DATE)
        """
    )["n"]

    return {
        "awaiting": awaiting,
        "null_overview": null_overview,
        "already_enriched": already_enriched_count,
        "total_active": total_active,
    }


def main():
    start = datetime.now()
    logger.info("=" * 60)
    logger.info("Enrichment status check — %s", start.strftime("%Y-%m-%d %H:%M"))
    logger.info("=" * 60)

    status = status_report()

    awaiting = status["awaiting"]
    null_ov   = status["null_overview"]

    logger.info("")
    logger.info("PIPELINE STATUS")
    logger.info("  Total active notices (parsed + scored): %d", status["total_active"])
    logger.info("  Already enriched (active):              %d", status["already_enriched"])
    logger.info("  Awaiting enrichment (qualify, not done):%d", len(awaiting))
    logger.info("  Active notices with null overview_text: %d", len(null_ov))
    logger.info("")

    if awaiting:
        logger.info("AWAITING ENRICHMENT (%d notices):", len(awaiting))
        urgent  = [r for r in awaiting if r["days_until_close"] is not None and r["days_until_close"] <= 7]
        soon    = [r for r in awaiting if r["days_until_close"] is not None and 7 < r["days_until_close"] <= 14]
        normal  = [r for r in awaiting if r["days_until_close"] is None or r["days_until_close"] > 14]
        no_ov   = [r for r in awaiting if r["no_overview"]]
        logger.info("  <= 7 days to close (urgent):  %d", len(urgent))
        logger.info("  8-14 days to close:           %d", len(soon))
        logger.info("  >14 days / score-qualified:   %d", len(normal))
        logger.info("  Of which: null overview_text: %d", len(no_ov))
        logger.info("")
        logger.info("  Top 10 by urgency / score:")
        for r in awaiting[:10]:
            dtc = f"{r['days_until_close']}d" if r["days_until_close"] is not None else "n/a"
            nov = " [NO OVERVIEW]" if r["no_overview"] else ""
            logger.info(
                "    [%s | score=%.1f | %s] %s -- %s%s",
                r["sector_tag"] or "?",
                float(r["composite_score"] or 0),
                dtc,
                r["notice_id"],
                (r["title"] or "")[:60],
                nov,
            )
    else:
        logger.info("No notices currently qualify for enrichment.")

    if null_ov:
        logger.info("")
        logger.info("NULL OVERVIEW_TEXT -- active notices (%d total):", len(null_ov))
        unenriched_null = [r for r in null_ov if not r["already_enriched"]]
        enriched_null   = [r for r in null_ov if r["already_enriched"]]
        logger.info("  Not yet enriched: %d", len(unenriched_null))
        logger.info("  Already enriched (will use description fallback): %d", len(enriched_null))
        for r in unenriched_null[:10]:
            logger.info("    %s -- %s (%s)", r["notice_id"], (r["title"] or "")[:60], r["agency"] or "?")

    logger.info("")
    logger.info("=" * 60)

    if not awaiting:
        logger.info("Nothing to enrich -- exiting.")
        return

    logger.info("Starting enrichment run for %d notices...", len(awaiting))
    logger.info("=" * 60)

    count = enrichment.run_enrichment()

    elapsed = (datetime.now() - start).total_seconds()
    logger.info("=" * 60)
    logger.info("Enrichment complete: %d/%d notices enriched in %.1fs",
                count, len(awaiting), elapsed)
    if count < len(awaiting):
        logger.warning(
            "%d notice(s) were not enriched -- check logs above for JSON/API errors.",
            len(awaiting) - count,
        )
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
