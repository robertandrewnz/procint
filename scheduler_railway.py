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

def _notify_watchlist_ready(notice_count: int) -> None:
    """
    Email every active non-admin client that today's watchlist is ready.
    Only called when notice_count > 0. Non-blocking — all errors are logged.
    """
    import json
    from pathlib import Path
    from datetime import date

    if notice_count <= 0:
        logger.info("SCHEDULER: watchlist notify skipped — 0 notices processed")
        return

    cfg_path = Path("portal_config.json")
    if not cfg_path.exists():
        logger.warning("SCHEDULER: watchlist notify skipped — portal_config.json not found")
        return

    try:
        cfg = json.loads(cfg_path.read_text())
    except Exception as exc:
        logger.error("SCHEDULER: watchlist notify — could not read portal_config.json: %s", exc)
        return

    import mailer
    date_label = date.today().strftime("%-d %B %Y")
    # Build portal URL from env — fall back to relative path
    portal_base = os.getenv("PORTAL_BASE_URL", "").rstrip("/")
    watchlist_url = portal_base + "/groundwork/watchlist" if portal_base else "/groundwork/watchlist"

    sent = failed = skipped = 0
    for username, data in cfg.get("clients", {}).items():
        if data.get("is_admin"):
            continue
        if data.get("billing_status", "active") != "active":
            skipped += 1
            continue
        email = data.get("email", "").strip()
        if not email:
            skipped += 1
            continue
        display_name = data.get("display_name", username)
        try:
            ok = mailer.send_watchlist_ready(
                client_name=display_name,
                client_email=email,
                notice_count=notice_count,
                portal_url=watchlist_url,
                date_label=date_label,
            )
            if ok:
                sent += 1
            else:
                failed += 1
        except Exception as exc:
            logger.error("SCHEDULER: watchlist notify failed for %s: %s", username, exc)
            failed += 1

    logger.info("SCHEDULER: watchlist notify complete — sent=%d failed=%d skipped=%d",
                sent, failed, skipped)


def _run_layer1() -> None:
    """Full Layer 1 pipeline: ingest → parse → score → enrich → bidders → output."""
    logger.info("=" * 60)
    logger.info("SCHEDULER: Layer 1 pipeline starting at %s", datetime.utcnow().isoformat())
    logger.info("=" * 60)
    notice_count = 0
    pipeline_ok = False
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
                # Try to read notice count from scoring/output result
                if name == "Output" and isinstance(result, (int, float)):
                    notice_count = int(result)
            except Exception as exc:
                logger.exception("SCHEDULER L1: %s FAILED — %s", name, exc)
                # Continue remaining steps rather than aborting the whole run

        pipeline_ok = True

        # Attempt to get notice count from DB if not returned by output step
        if notice_count == 0:
            try:
                import db
                from datetime import date
                row = db.fetchone(
                    "SELECT COUNT(*) AS n FROM parsed_notices WHERE date_parsed::date = %s",
                    (date.today(),),
                )
                notice_count = int((row or {}).get("n", 0))
            except Exception as exc:
                logger.warning("SCHEDULER L1: could not query notice count: %s", exc)

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
        logger.info("SCHEDULER: Layer 1 pipeline finished at %s — notices=%d",
                    datetime.utcnow().isoformat(), notice_count)

    # Send watchlist-ready emails to all active clients (non-blocking)
    if pipeline_ok:
        try:
            _notify_watchlist_ready(notice_count)
        except Exception as exc:
            logger.error("SCHEDULER: _notify_watchlist_ready raised: %s", exc)


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


def _run_watch_brief() -> dict:
    """
    Weekly watch brief — generate and email to every active portal client.
    Uses pursuit_worker.send_all_watch_briefs() which reads portal_config.json,
    generates a brief per client using their saved sector preferences, emails
    each client directly, and logs each send to the brief_sends table.
    Admin receives a separate summary copy via mailer.send_admin_only().

    Returns stats dict from send_all_watch_briefs, or error dict on failure.
    """
    logger.info("=" * 60)
    logger.info("SCHEDULER: Watch brief starting at %s", datetime.utcnow().isoformat())
    logger.info("=" * 60)
    try:
        from pursuit_worker import send_all_watch_briefs
        stats = send_all_watch_briefs()
        logger.info(
            "SCHEDULER: Watch brief finished — generated=%s sent=%s failed=%s skipped=%s",
            stats.get("generated", "?"), stats.get("sent", "?"),
            stats.get("failed", "?"), stats.get("skipped", "?"),
        )
        if stats.get("errors"):
            for err in stats["errors"]:
                logger.warning("SCHEDULER: Watch brief issue: %s", err)
        return stats
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
        return {"error": str(exc)}
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
