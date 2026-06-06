"""
Layer 3 Pipeline entry point.

Generates configured executive artefacts on demand or as part of an
automated run. Can be called standalone or via run_pipeline.py --layer3.

Usage:
  python layer3_pipeline.py [options]

  --pursuit <notice_id> --client "<Name>"   Generate pursuit package
  --demo    <notice_id> --client "<Name>"   Generate demo package
  --brief              --client "<Name>"   Generate weekly watch brief
  --competitor "<Name>" [--client "<Name>"] Generate competitor profile
  --all-pursuits        --client "<Name>"   Generate packages for all
                                            top-scored notices

Examples:
  python layer3_pipeline.py --pursuit 34060392 --client "Downer NZ"
  python layer3_pipeline.py --demo 33731454 --client "Prospect Co"
  python layer3_pipeline.py --brief --client "Advisory Firm"
  python layer3_pipeline.py --competitor "Fulton Hogan" --client "Downer NZ"
  python layer3_pipeline.py --all-pursuits --client "Downer NZ" --top 5
"""
import argparse
import logging
import sys
from datetime import date
from pathlib import Path

import config  # noqa: must be first

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"layer3_{date.today().strftime('%Y%m%d')}.log"),
    ],
)
logger = logging.getLogger("layer3")

import db
from pursuit_package import generate_pursuit_package, _artefact_dir
from demo_package import generate_demo_package
from watch_brief import generate_watch_brief
from competitor_profile import generate_competitor_profile


def _top_notice_ids(limit: int) -> list[str]:
    """Return notice_ids for the top-scored active notices."""
    rows = db.fetchall(
        """
        SELECT s.notice_id
          FROM scored_notices s
          JOIN raw_notices r ON r.notice_id = s.notice_id
         WHERE s.composite_score >= %s
           AND r.close_date >= CURRENT_DATE
         ORDER BY s.composite_score DESC
         LIMIT %s
        """,
        (config.PRIORITY_THRESHOLD, limit),
    )
    return [r["notice_id"] for r in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description="Procint Layer 3 pipeline")
    parser.add_argument("--pursuit",    metavar="NOTICE_ID",
                        help="Generate pursuit package for a single notice")
    parser.add_argument("--demo",       metavar="NOTICE_ID",
                        help="Generate demo package for a single notice")
    parser.add_argument("--brief",      action="store_true",
                        help="Generate weekly watch brief")
    parser.add_argument("--competitor", metavar="COMPANY",
                        help="Generate competitor profile")
    parser.add_argument("--all-pursuits", action="store_true",
                        help="Generate pursuit packages for all top-scored notices")
    parser.add_argument("--client",     metavar="NAME", default="Client",
                        help="Client company name (used for personalisation)")
    parser.add_argument("--top",        type=int, default=5,
                        help="Number of notices for --all-pursuits (default 5)")
    parser.add_argument("--output-dir", metavar="PATH",
                        help="Override output directory")
    parser.add_argument("--no-pdf",     action="store_true",
                        help="Skip PDF generation for demo packages")
    args = parser.parse_args()

    output_dir = Path(args.output_dir) if args.output_dir else None
    client = args.client

    if not any([args.pursuit, args.demo, args.brief, args.competitor, args.all_pursuits]):
        parser.print_help()
        sys.exit(0)

    from datetime import datetime
    start = datetime.now()
    logger.info("=" * 60)
    logger.info("Layer 3 pipeline started at %s", start.isoformat())
    logger.info("=" * 60)

    generated = []

    # ── Pursuit package ───────────────────────────────────────────────────────
    if args.pursuit:
        logger.info("--- Pursuit package: %s for %s ---", args.pursuit, client)
        try:
            out = generate_pursuit_package(
                notice_id=args.pursuit,
                client_name=client,
                output_dir=output_dir,
            )
            generated.append(str(out))
            logger.info("Pursuit package: %s", out)
        except Exception as exc:
            logger.error("Pursuit package failed: %s", exc)

    # ── Demo package ──────────────────────────────────────────────────────────
    if args.demo:
        logger.info("--- Demo package: %s for %s ---", args.demo, client)
        try:
            result = generate_demo_package(
                notice_id=args.demo,
                prospect_name=client,
                output_dir=output_dir,
                generate_pdf=not args.no_pdf,
            )
            for k, v in result.items():
                if v:
                    generated.append(str(v))
                    logger.info("Demo %s: %s", k.upper(), v)
        except Exception as exc:
            logger.error("Demo package failed: %s", exc)

    # ── Watch brief ───────────────────────────────────────────────────────────
    if args.brief:
        logger.info("--- Weekly watch brief for %s ---", client)
        try:
            out = generate_watch_brief(
                client_name=client,
                output_dir=output_dir,
            )
            generated.append(str(out))
            logger.info("Watch brief: %s", out)
        except Exception as exc:
            logger.error("Watch brief failed: %s", exc)

    # ── Competitor profile ─────────────────────────────────────────────────────
    if args.competitor:
        logger.info("--- Competitor profile: %s ---", args.competitor)
        try:
            out = generate_competitor_profile(
                competitor_name=args.competitor,
                client_name=client if client != "Client" else None,
                output_dir=output_dir,
            )
            generated.append(str(out))
            logger.info("Competitor profile: %s", out)
        except Exception as exc:
            logger.error("Competitor profile failed: %s", exc)

    # ── All pursuits ──────────────────────────────────────────────────────────
    if args.all_pursuits:
        notice_ids = _top_notice_ids(args.top)
        logger.info("--- All pursuits: %d notices for %s ---", len(notice_ids), client)
        for nid in notice_ids:
            try:
                out = generate_pursuit_package(
                    notice_id=nid,
                    client_name=client,
                    output_dir=output_dir,
                )
                generated.append(str(out))
                logger.info("  Generated: %s", out)
            except Exception as exc:
                logger.error("  Failed for %s: %s", nid, exc)

    elapsed = (datetime.now() - start).total_seconds()
    logger.info("=" * 60)
    logger.info("Layer 3 complete in %.1fs — %d artefacts generated", elapsed, len(generated))
    for path in generated:
        logger.info("  %s", path)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
