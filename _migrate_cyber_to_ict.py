"""
Migration: reclassify all 'cybersecurity' sector tags to 'ICT'.

Cybersecurity is no longer a standalone sector — all infosec procurement
is now classified as ICT.  This script updates:
  - parsed_notices.sector_tag
  - scored_notices (sector score is recalculated by the scoring pipeline;
    this script does not touch it — run scoring after migration)

Usage:
    railway run python3 _migrate_cyber_to_ict.py            # apply
    railway run python3 _migrate_cyber_to_ict.py --dry-run  # preview
"""
import logging
import sys

import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger(__name__)


def migrate(dry_run: bool = False) -> None:
    rows = db.fetchall(
        "SELECT notice_id FROM parsed_notices WHERE sector_tag = 'cybersecurity'"
    )
    total = len(rows)
    logger.info("Found %d notice(s) tagged 'cybersecurity'", total)

    if not total:
        logger.info("Nothing to migrate.")
        return

    if dry_run:
        for r in rows[:20]:
            logger.info("  DRY RUN — would reclassify: %s", r["notice_id"])
        if total > 20:
            logger.info("  ... and %d more", total - 20)
        logger.info("DRY RUN complete — no changes made.")
        return

    db.execute(
        """
        UPDATE parsed_notices
           SET sector_tag                = 'ICT',
               classification_method     = 'migration',
               classification_reasoning  = 'Cybersecurity sector merged into ICT (2026-06)',
               parsed_at                 = NOW()
         WHERE sector_tag = 'cybersecurity'
        """
    )
    logger.info("Migration complete — %d notice(s) reclassified cybersecurity → ICT", total)
    logger.info("Re-run scoring pipeline to recalculate composite scores for affected notices.")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    migrate(dry_run=dry_run)
