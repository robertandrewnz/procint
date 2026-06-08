"""
intel_library/scheduler_jobs.py — Scheduled refresh jobs for the intel library.

Integrates with the existing scheduler.py. Call these functions directly or
register them with the schedule library.

Schedule (to add to scheduler.py or crontab):
  Daily 05:00    — fetch_beehive_daily() — Beehive press releases + speeches
  Sunday 06:00   — refresh_all_sources() — All active sources, skip if unchanged
  Quarterly      — refresh_quarterly() — Infrastructure Pipeline snapshot
  Monthly 1st    — refresh_monthly() — Sector + agency profiles
  One-off        — initial_budget_fetch() — All Budget 2026 Vote PDFs + key PDFs

Cron examples (add to COMMANDS.md):
  0 5 * * *   cd /path/to/procint && python -m intel_library.scheduler_jobs --daily
  0 6 * * 0   cd /path/to/procint && python -m intel_library.scheduler_jobs --weekly
  0 6 1 1,4,7,10 * cd /path/to/procint && python -m intel_library.scheduler_jobs --quarterly
  0 5 1 * *   cd /path/to/procint && python -m intel_library.scheduler_jobs --monthly
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

import db
from intel_library.extract_signals import process_all_sources, process_source

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)


def fetch_beehive_daily() -> dict:
    """
    Daily 05:00 — Fetch Beehive press releases and speeches.
    Only processes daily-frequency sources.
    """
    logger.info("=== Intel Library: Daily Beehive fetch ===")
    return process_all_sources(daily_only=True, force=False, delay_seconds=2.0)


def refresh_all_sources() -> dict:
    """
    Weekly Sunday 06:00 — Fetch all active sources.
    Skips sources whose content hash has not changed.
    """
    logger.info("=== Intel Library: Weekly full source refresh ===")
    return process_all_sources(force=False, delay_seconds=2.0)


def refresh_quarterly() -> dict:
    """
    Quarterly — Fetch the Infrastructure Pipeline snapshot from Te Waihanga.
    Forced re-fetch since these are quarterly publications.
    """
    logger.info("=== Intel Library: Quarterly infrastructure pipeline refresh ===")
    return process_all_sources(source_filter="InfraPipeline", force=True, delay_seconds=1.0)


def refresh_monthly() -> dict:
    """
    Monthly 1st 05:00 — Refresh sector profiles and fortnightly economic updates.
    """
    logger.info("=== Intel Library: Monthly profile refresh ===")
    # Refresh FEU (fortnightly updates) and any monthly-ish sources
    result1 = process_all_sources(source_filter="FEU", force=True, delay_seconds=1.0)
    result2 = process_all_sources(source_filter="NCSR", force=False, delay_seconds=1.0)

    # Refresh sector profiles (re-run seed if new signals have been extracted)
    try:
        from intel_library.seed_sources import seed_initial_sector_profiles
        seed_initial_sector_profiles()
        logger.info("Sector profiles refreshed")
    except Exception as exc:
        logger.warning("Sector profile refresh failed: %s", exc)

    return {
        "processed": result1["processed"] + result2["processed"],
        "succeeded": result1["succeeded"] + result2["succeeded"],
        "failed":    result1["failed"] + result2["failed"],
    }


def initial_budget_fetch() -> dict:
    """
    One-off first run — process all Budget 2026 sources and key PDFs.

    Sources processed:
      - Budget2026-Full (meta-source)
      - BEFU2026 (PDF)
      - FSR2026
      - Budget 2026 Summary of Initiatives (PDF)
      - Budget 2026 Vote Documents
      - LTIB-Treasury-2025 (Te Ara Mokopuna PDF)
      - LTIB-MBIE-2025 (PDF)
      - DCP2025
      - NZDIS (PDF)
      - NZCSS-2026 (PDF)
      - NPS-Infrastructure
      - GPR5 (PDF)
      - NIP2025
      - InfraPipeline
    """
    logger.info("=== Intel Library: Initial Budget 2026 + key document fetch ===")

    priority_filters = [
        "Budget2026-Full",
        "BEFU2026",
        "FSR2026",
        "LTIB-Treasury-2025",
        "LTIB-MBIE-2025",
        "DCP2025",
        "NZDIS",
        "NZCSS-2026",
        "NPS-Infrastructure",
        "GPR5",
        "NIP2025",
        "InfraPipeline",
        "Budget 2026 Summary",  # title fragment
        "Budget 2026 — Vote",   # title fragment
    ]

    total = {"processed": 0, "succeeded": 0, "failed": 0}
    seen_titles = set()

    for filt in priority_filters:
        result = process_all_sources(source_filter=filt, force=True, delay_seconds=2.0)
        for k in total:
            total[k] += result[k]

    logger.info(
        "Initial fetch complete. Processed: %d, Succeeded: %d, Failed: %d",
        total["processed"], total["succeeded"], total["failed"],
    )
    return total


def get_library_stats() -> dict:
    """Return summary statistics for the admin /intel page."""
    try:
        sources_total = db.fetchone("SELECT COUNT(*) AS n FROM intel_sources")
        sources_active = db.fetchone("SELECT COUNT(*) AS n FROM intel_sources WHERE is_active = TRUE")
        signals_total = db.fetchone("SELECT COUNT(*) AS n FROM intel_signals")
        signals_30d = db.fetchone(
            "SELECT COUNT(*) AS n FROM intel_signals WHERE extracted_at >= NOW() - INTERVAL '30 days'"
        )
        budget_signals = db.fetchone(
            """
            SELECT COUNT(*) AS n
            FROM intel_signals sig
            JOIN intel_sources src ON src.id = sig.source_id
            WHERE src.short_name = ANY(%s)
            """,
            (["BEFU2026", "Budget2026-Full", "FSR2026"],),
        )
        last_refresh = db.fetchone(
            "SELECT MAX(last_checked) AS ts FROM intel_sources"
        )
        return {
            "sources_total":   (sources_total or {}).get("n", 0),
            "sources_active":  (sources_active or {}).get("n", 0),
            "signals_total":   (signals_total or {}).get("n", 0),
            "signals_30d":     (signals_30d or {}).get("n", 0),
            "budget_signals":  (budget_signals or {}).get("n", 0),
            "last_refresh":    (last_refresh or {}).get("ts"),
        }
    except Exception as exc:
        logger.warning("get_library_stats failed: %s", exc)
        return {
            "sources_total": 0, "sources_active": 0, "signals_total": 0,
            "signals_30d": 0, "budget_signals": 0, "last_refresh": None,
        }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Intel Library Scheduler Jobs")
    parser.add_argument("--daily",      action="store_true", help="Daily Beehive fetch")
    parser.add_argument("--weekly",     action="store_true", help="Weekly full source refresh")
    parser.add_argument("--quarterly",  action="store_true", help="Quarterly pipeline refresh")
    parser.add_argument("--monthly",    action="store_true", help="Monthly profile refresh")
    parser.add_argument("--initial",    action="store_true", help="One-off initial Budget 2026 fetch")
    parser.add_argument("--stats",      action="store_true", help="Print library stats and exit")
    args = parser.parse_args()

    if args.stats:
        stats = get_library_stats()
        print(f"Sources: {stats['sources_active']} active / {stats['sources_total']} total")
        print(f"Signals: {stats['signals_total']} total, {stats['signals_30d']} last 30 days")
        print(f"Budget 2026 signals: {stats['budget_signals']}")
        print(f"Last refresh: {stats['last_refresh']}")
        sys.exit(0)

    if args.daily:
        result = fetch_beehive_daily()
    elif args.weekly:
        result = refresh_all_sources()
    elif args.quarterly:
        result = refresh_quarterly()
    elif args.monthly:
        result = refresh_monthly()
    elif args.initial:
        result = initial_budget_fetch()
    else:
        parser.error("Specify --daily, --weekly, --quarterly, --monthly, --initial, or --stats")

    sys.exit(0 if result.get("failed", 0) == 0 else 1)
