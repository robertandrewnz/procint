"""
Layer 1 Pipeline entry point.

Usage:
    python run_pipeline.py [--skip-ingestion] [--skip-enrichment]

Flags allow partial re-runs during development or when GETS is unreachable.
"""
import argparse
import logging
import sys
from datetime import datetime

import config

# ── Logging setup (must happen before module imports that log) ────────────────
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"pipeline_{datetime.now().strftime('%Y%m%d')}.log"),
    ],
)

logger = logging.getLogger("pipeline")

import ingestion
import parsing
import scoring
import enrichment
import bidders
import output


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Procint Layer 1 pipeline")
    p.add_argument("--skip-ingestion",  action="store_true", help="Skip GETS scrape")
    p.add_argument("--skip-enrichment", action="store_true", help="Skip Claude enrichment")
    p.add_argument("--skip-bidders",    action="store_true", help="Skip bidder inference")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    start = datetime.now()
    logger.info("=" * 60)
    logger.info("Procint Layer 1 pipeline started at %s", start.isoformat())
    logger.info("=" * 60)

    steps = [
        ("Ingestion",         ingestion.run_ingestion,            args.skip_ingestion),
        ("Parsing",           parsing.run_parsing,                False),
        ("Scoring",           scoring.run_scoring,                False),
        ("Enrichment",        enrichment.run_enrichment,          args.skip_enrichment),
        ("Bidder inference",  bidders.run_bidder_inference,       args.skip_bidders),
        ("Output",            output.run_output,                  False),
    ]

    results = {}
    for name, fn, skip in steps:
        if skip:
            logger.info("SKIPPED: %s", name)
            continue
        logger.info("--- %s ---", name)
        try:
            result = fn()
            results[name] = result
            logger.info("%s complete: %s", name, result)
        except Exception as exc:
            logger.exception("FATAL error in %s: %s", name, exc)
            sys.exit(1)

    elapsed = (datetime.now() - start).total_seconds()
    logger.info("=" * 60)
    logger.info("Pipeline complete in %.1fs", elapsed)

    json_path, md_path, html_path = results.get("Output", (None, None, None))
    if html_path:
        logger.info("Watchlist (HTML): %s", html_path)
    if md_path:
        logger.info("Watchlist  (MD):  %s", md_path)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
