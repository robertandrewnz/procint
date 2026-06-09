"""
scheduler_railway.py — APScheduler-based pipeline scheduler for Railway.

Runs as a background thread inside the Gunicorn/Flask process. Jobs are
scheduled in NZT (UTC+12). UTC equivalents:

  06:00 NZT daily  → 18:00 UTC  — Layer 1 full pipeline
  07:00 NZT daily  → 19:00 UTC  — Layer 2 intelligence pipeline
  08:00 NZT Monday → 20:00 UTC Sunday — Weekly watch brief

Single-instance guard
─────────────────────
Gunicorn spawns N worker processes — without a guard every worker would
start its own scheduler and jobs would run N times simultaneously.
The guard uses a lock file: the first worker to acquire it becomes the
scheduler process; all others skip silently. The lock file is held for
the lifetime of the process (released automatically on exit/crash).

Set DISABLE_SCHEDULER=1 in Railway environment variables to suppress all
scheduled jobs (useful during maintenance or testing).
"""
from __future__ import annotations

import fcntl
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("scheduler_railway")

# ── Lock file — only one Gunicorn worker runs the scheduler ──────────────────
_LOCK_PATH = Path("/tmp/groundwork_scheduler.lock")
_lock_fh = None  # keep file handle open so the lock persists


def _acquire_lock() -> bool:
    """
    Try to acquire an exclusive non-blocking flock on the lock file.
    Returns True if this process is now the scheduler instance.
    Returns False if another process already holds the lock.
    """
    global _lock_fh
    try:
        _lock_fh = open(_LOCK_PATH, "w")
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fh.write(str(os.getpid()))
        _lock_fh.flush()
        return True
    except OSError:
        # Another worker already holds the lock
        if _lock_fh:
            _lock_fh.close()
            _lock_fh = None
        return False


# ── Job functions ─────────────────────────────────────────────────────────────

def _run_layer1() -> None:
    """Full Layer 1 pipeline: ingest → parse → score → enrich → bidders → output."""
    logger.info("=" * 60)
    logger.info("SCHEDULER: Layer 1 pipeline starting at %s", datetime.utcnow().isoformat())
    logger.info("=" * 60)
    try:
        import ingestion
        import parsing
        import scoring
        import enrichment
        import bidders
        import output

        steps = [
            ("Ingestion",        ingestion.run_ingestion),
            ("Parsing",          parsing.run_parsing),
            ("Scoring",          scoring.run_scoring),
            ("Enrichment",       enrichment.run_enrichment),
            ("Bidder inference", bidders.run_bidder_inference),
            ("Output",           output.run_output),
        ]
        for name, fn in steps:
            logger.info("SCHEDULER L1: --- %s ---", name)
            try:
                result = fn()
                logger.info("SCHEDULER L1: %s complete — %s", name, result)
            except Exception as exc:
                logger.exception("SCHEDULER L1: %s FAILED — %s", name, exc)
                # Continue remaining steps rather than aborting the whole run
    except Exception as exc:
        logger.exception("SCHEDULER: Layer 1 pipeline error: %s", exc)
        try:
            import mailer
            mailer.send_admin_only(
                subject="[Groundwork] Layer 1 pipeline ERROR",
                html=f"<p>The Layer 1 daily pipeline failed:</p><pre>{exc}</pre>",
            )
        except Exception:
            pass
    finally:
        logger.info("SCHEDULER: Layer 1 pipeline finished at %s", datetime.utcnow().isoformat())


def _run_layer2() -> None:
    """Layer 2: organisation seeding, awards ingestion, agency profiling, patterns."""
    logger.info("=" * 60)
    logger.info("SCHEDULER: Layer 2 pipeline starting at %s", datetime.utcnow().isoformat())
    logger.info("=" * 60)
    try:
        import layer2_pipeline
        layer2_pipeline.main()
    except Exception as exc:
        logger.exception("SCHEDULER: Layer 2 pipeline error: %s", exc)
        try:
            import mailer
            mailer.send_admin_only(
                subject="[Groundwork] Layer 2 pipeline ERROR",
                html=f"<p>The Layer 2 intelligence pipeline failed:</p><pre>{exc}</pre>",
            )
        except Exception:
            pass
    finally:
        logger.info("SCHEDULER: Layer 2 pipeline finished at %s", datetime.utcnow().isoformat())


def _run_watch_brief() -> None:
    """
    Weekly watch brief — generate and email to every active portal client.
    Uses pursuit_worker.send_all_watch_briefs() which reads portal_config.json,
    generates a brief per client using their saved sector preferences, emails
    each client directly, and logs each send to the brief_sends table.
    Admin receives a separate summary copy via mailer.send_admin_only().
    """
    logger.info("=" * 60)
    logger.info("SCHEDULER: Watch brief starting at %s", datetime.utcnow().isoformat())
    logger.info("=" * 60)
    try:
        from pursuit_worker import send_all_watch_briefs
        send_all_watch_briefs()
    except Exception as exc:
        logger.exception("SCHEDULER: Watch brief job error: %s", exc)
        try:
            import mailer
            mailer.send_admin_only(
                subject="[Groundwork] Watch brief scheduler ERROR",
                html=f"<p>The watch brief scheduler job failed:</p><pre>{exc}</pre>",
            )
        except Exception:
            pass
    finally:
        logger.info("SCHEDULER: Watch brief finished at %s", datetime.utcnow().isoformat())


# ── Scheduler startup ─────────────────────────────────────────────────────────

def start_scheduler() -> None:
    """
    Start the APScheduler BackgroundScheduler.
    Call this once from portal.py after the Flask app is created.
    Returns immediately — jobs run in a daemon background thread.
    """
    if os.getenv("DISABLE_SCHEDULER", "").strip().lower() in ("1", "true", "yes"):
        logger.info("SCHEDULER: DISABLE_SCHEDULER is set — not starting")
        return

    if not _acquire_lock():
        logger.info("SCHEDULER: Another worker holds the lock — not starting scheduler in this process (pid=%s)", os.getpid())
        return

    logger.info("SCHEDULER: Lock acquired by pid=%s — starting APScheduler", os.getpid())

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.error("SCHEDULER: apscheduler not installed — pip install apscheduler")
        return

    scheduler = BackgroundScheduler(timezone="UTC", daemon=True)

    # ── Layer 1: daily at 06:00 NZT = 18:00 UTC ──────────────────────────────
    scheduler.add_job(
        _run_layer1,
        CronTrigger(hour=18, minute=0, timezone="UTC"),
        id="layer1_daily",
        name="Layer 1 — full pipeline (06:00 NZT)",
        misfire_grace_time=3600,   # run up to 1h late if process was down
        coalesce=True,             # skip duplicate if still running
        max_instances=1,
    )

    # ── Layer 2: daily at 07:00 NZT = 19:00 UTC ──────────────────────────────
    scheduler.add_job(
        _run_layer2,
        CronTrigger(hour=19, minute=0, timezone="UTC"),
        id="layer2_daily",
        name="Layer 2 — intelligence pipeline (07:00 NZT)",
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )

    # ── Watch brief: Monday 08:00 NZT = Sunday 20:00 UTC ─────────────────────
    scheduler.add_job(
        _run_watch_brief,
        CronTrigger(day_of_week="sun", hour=20, minute=0, timezone="UTC"),
        id="watch_brief_weekly",
        name="Weekly watch brief (Mon 08:00 NZT)",
        misfire_grace_time=7200,
        coalesce=True,
        max_instances=1,
    )

    scheduler.start()

    # Log the scheduled jobs so Railway deploy logs show what's configured
    logger.info("SCHEDULER: APScheduler started — %d jobs registered:", len(scheduler.get_jobs()))
    for job in scheduler.get_jobs():
        logger.info("  • %s  next run: %s", job.name, job.next_run_time)
