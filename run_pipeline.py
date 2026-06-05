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
    p = argparse.ArgumentParser(description="Procint Layer 1 + optional Layer 2 pipeline")
    p.add_argument("--skip-ingestion",  action="store_true", help="Skip GETS scrape")
    p.add_argument("--skip-enrichment", action="store_true", help="Skip Claude enrichment")
    p.add_argument("--skip-bidders",    action="store_true", help="Skip bidder inference")
    p.add_argument("--layer2",          action="store_true",
                   help="Run Layer 2 intelligence pipeline after Layer 1 completes")
    p.add_argument("--skip-awards",     action="store_true",
                   help="(Layer 2) Skip contract award scraping")
    p.add_argument("--skip-profiles",   action="store_true",
                   help="(Layer 2) Skip Claude agency profile generation")
    p.add_argument("--company",         type=str, default=None,
                   help="(Layer 2) Firm name for competitor intelligence")
    p.add_argument("--layer3",          action="store_true",
                   help="Run Layer 3 artefact generation after Layer 1+2")
    p.add_argument("--l3-client",       type=str, default=None,
                   help="(Layer 3) Client name for artefact personalisation")
    p.add_argument("--l3-brief",        action="store_true",
                   help="(Layer 3) Generate weekly watch brief")
    p.add_argument("--l3-top",          type=int, default=3,
                   help="(Layer 3) Number of pursuit packages to generate (default 3)")
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
    logger.info("Layer 1 complete in %.1fs", elapsed)

    out = results.get("Output")
    if isinstance(out, tuple):
        for p in out:
            if p and str(p).endswith(".html"):
                logger.info("Watchlist (HTML): %s", p)
            elif p and str(p).endswith(".md"):
                logger.info("Watchlist  (MD):  %s", p)
    elif out:
        logger.info("Watchlist: %s", out)
    logger.info("=" * 60)

    # ── Optional Layer 2 ─────────────────────────────────────────────────────
    if args.layer2:
        logger.info("--- Layer 2: Intelligence synthesis ---")
        try:
            import layer2_pipeline
            layer2_pipeline.main(
                skip_awards=args.skip_awards,
                skip_profiles=args.skip_profiles,
                company_name=args.company,
            )
        except Exception as exc:
            logger.exception("Layer 2 pipeline failed: %s", exc)
            # Layer 2 failure does not exit — Layer 1 output is still valid

    # ── Optional Layer 3 ─────────────────────────────────────────────────────
    if args.layer3:
        logger.info("--- Layer 3: Executive artefact generation ---")
        client = args.l3_client or "Client"
        try:
            import layer3_pipeline
            # Build sys.argv for layer3_pipeline.main() to parse
            import sys as _sys
            _saved_argv = _sys.argv
            _sys.argv = ["layer3_pipeline.py", "--client", client]
            if args.l3_brief:
                _sys.argv.append("--brief")
            if args.l3_top > 0:
                _sys.argv += ["--all-pursuits", "--top", str(args.l3_top)]
            try:
                layer3_pipeline.main()
            finally:
                _sys.argv = _saved_argv
        except Exception as exc:
            logger.exception("Layer 3 pipeline failed: %s", exc)
            # Layer 3 failure does not exit — earlier output is still valid


if __name__ == "__main__":
    main()
