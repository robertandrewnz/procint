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
from datetime import datetime, timedelta
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
    run_id = None
    try:
        import db
        try:
            row = db.fetchone(
                "INSERT INTO pipeline_runs (stage, triggered_by, status) "
                "VALUES ('layer1', 'scheduler', 'running') RETURNING id",
            )
            run_id = row["id"] if row else None
        except Exception as _e:
            logger.warning("SCHEDULER: pipeline_runs INSERT failed: %s", _e)

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
                if name == "Output" and isinstance(result, (int, float)):
                    notice_count = int(result)
            except Exception as exc:
                logger.exception("SCHEDULER L1: %s FAILED — %s", name, exc)

        pipeline_ok = True

        if notice_count == 0:
            try:
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
        if run_id is not None:
            try:
                import db as _db
                _db.execute(
                    "UPDATE pipeline_runs SET status=%s, summary=%s, finished_at=NOW() WHERE id=%s",
                    ("complete" if pipeline_ok else "failed",
                     f"{notice_count} notices processed", run_id),
                )
            except Exception as _e:
                logger.warning("SCHEDULER: pipeline_runs UPDATE failed: %s", _e)

    if pipeline_ok:
        try:
            _notify_watchlist_ready(notice_count)
        except Exception as exc:
            logger.error("SCHEDULER: _notify_watchlist_ready raised: %s", exc)
        try:
            _run_backfill_overview()
        except Exception as exc:
            logger.error("SCHEDULER: _run_backfill_overview raised: %s", exc)


def _run_backfill_overview() -> None:
    """
    Nightly backfill of overview_text for active watchlist notices where it is null.
    Called automatically at the end of _run_layer1() after the scrape completes.
    Fetches GETS detail pages at 1.5 s/request and updates raw_notices + parsed_notices.
    Logs progress to pipeline_runs (stage='backfill_overview').
    """
    logger.info("SCHEDULER: backfill_overview starting at %s", datetime.utcnow().isoformat())
    run_id = None
    try:
        import db as _db
        row = _db.fetchone(
            "INSERT INTO pipeline_runs (stage, triggered_by, status) "
            "VALUES ('backfill_overview', 'scheduler', 'running') RETURNING id",
        )
        run_id = row["id"] if row else None
    except Exception as _e:
        logger.warning("SCHEDULER: pipeline_runs INSERT failed (backfill_overview): %s", _e)

    ok = False
    summary = ""
    try:
        import time as _time
        import db as _db
        import config as _cfg
        from ingestion import _fetch_notice_detail
        from parsing import extract_key_dates

        rows = _db.fetchall(
            """
            SELECT r.notice_id, r.source_url, r.title, r.agency,
                   r.category_raw, r.description, r.close_date
              FROM raw_notices r
              JOIN parsed_notices p ON p.notice_id = r.notice_id
              JOIN scored_notices s ON s.notice_id = r.notice_id
             WHERE (r.overview_text IS NULL OR r.overview_text = '')
               AND (r.close_date IS NULL OR r.close_date >= CURRENT_DATE)
               AND (s.composite_score >= %s OR r.category_raw ILIKE '%%advance%%')
             ORDER BY r.close_date ASC NULLS LAST
            """,
            (_cfg.PRIORITY_THRESHOLD,),
        )

        if not rows:
            logger.info("SCHEDULER: backfill_overview — nothing to backfill")
            ok = True
            summary = "0 notices needed backfill"
        else:
            done = errors = 0
            for notice in rows:
                nid = notice["notice_id"]
                try:
                    nd = dict(notice)
                    nd = _fetch_notice_detail(nd)
                    overview = nd.get("overview_text") or ""
                    _db.execute(
                        """
                        UPDATE raw_notices
                           SET overview_text = %s,
                               description   = COALESCE(NULLIF(%s,''), description)
                         WHERE notice_id = %s
                        """,
                        (overview or None, overview or None, nid),
                    )
                    if overview:
                        kd = extract_key_dates(overview)
                        if any(v for v in kd.values()):
                            _db.execute(
                                """
                                UPDATE parsed_notices
                                   SET briefing_date         = COALESCE(%s, briefing_date),
                                       questions_deadline    = COALESCE(%s, questions_deadline),
                                       registration_deadline = COALESCE(%s, registration_deadline),
                                       parsed_at             = NOW()
                                 WHERE notice_id = %s
                                """,
                                (kd.get("briefing_date"), kd.get("questions_deadline"),
                                 kd.get("registration_deadline"), nid),
                            )
                    done += 1
                    _time.sleep(1.5)
                except Exception as _e:
                    logger.warning("SCHEDULER: backfill_overview %s — %s", nid, _e)
                    errors += 1
                    _time.sleep(1.5)
            ok = True
            summary = f"{done} notices backfilled, {errors} errors"
            logger.info("SCHEDULER: backfill_overview complete — %s", summary)
    except Exception as exc:
        logger.exception("SCHEDULER: backfill_overview error: %s", exc)
        summary = f"error: {str(exc)[:200]}"
    finally:
        if run_id is not None:
            try:
                import db as _db2
                _db2.execute(
                    "UPDATE pipeline_runs SET status=%s, summary=%s, finished_at=NOW() WHERE id=%s",
                    ("complete" if ok else "failed", summary, run_id),
                )
            except Exception as _e:
                logger.warning("SCHEDULER: pipeline_runs UPDATE failed (backfill_overview): %s", _e)


def _run_fix_bidder_mismatches() -> None:
    """
    Post-enrichment bidder mismatch cleanup — deletes sector-excluded mbie_evidence/
    csv_inferred records from bidder_pool and re-runs bidder inference for the
    affected notices.  Called automatically after _run_layer2().
    Logs to pipeline_runs (stage='fix_bidder_mismatches').
    """
    logger.info("SCHEDULER: fix_bidder_mismatches starting at %s", datetime.utcnow().isoformat())
    run_id = None
    try:
        import db as _db
        row = _db.fetchone(
            "INSERT INTO pipeline_runs (stage, triggered_by, status) "
            "VALUES ('fix_bidder_mismatches', 'scheduler', 'running') RETURNING id",
        )
        run_id = row["id"] if row else None
    except Exception as _e:
        logger.warning("SCHEDULER: pipeline_runs INSERT failed (fix_bidder_mismatches): %s", _e)

    ok = False
    summary = ""
    try:
        import db as _db
        import config as _cfg
        from bidders import (SECTOR_EXCLUSION_MATRIX as _SEM,
                             score_bidders_for_notice as _sbfn,
                             _store_bidders, load_bidders)

        _PHYS_W = {"construction", "roading", "civil", "infrastructure", "fm"}
        _PHYS_S = {
            "building", "construct", "infrastructure", "roading", "maintenance",
            "civil", "facility", "upgrade", "installation", "earthworks", "structural",
            "bridge", "pavement", "drainage", "demolition", "fitout",
        }
        _SVC_S = {
            "advisory", "consulting", "professional services", "management services",
            "strategy", "research", "analysis", "training", "audit",
            "software", "ict", "it services", "digital", "technology",
            "platform", "system development", "application", "data", "cyber",
            "recruitment", "legal services", "financial services",
        }
        _EXCLUDED_FIRMS = {
            "beca", "beca limited", "stantec nz", "stantec new zealand",
            "morphum environmental",
        }

        def _is_mismatch(firm_sector, notice_sector, notice_text):
            fs  = (firm_sector  or "").lower().strip()
            ns  = (notice_sector or "other").lower().strip()
            txt = notice_text.lower()
            if not fs:
                return False
            excluded = {e.lower() for e in _SEM.get(ns, set())}
            if fs in excluded:
                return True
            is_phys = any(sig in txt for sig in _PHYS_S)
            is_svc  = not is_phys and any(sig in txt for sig in _SVC_S)
            if fs in _PHYS_W and is_svc:
                return True
            if ns in ("other", "unknown", "") and fs in _PHYS_W and not is_phys:
                return True
            return False

        rows = _db.fetchall(
            """
            SELECT bp.notice_id, p.sector_tag AS notice_sector,
                   r.title || ' ' || COALESCE(r.description,'') AS combined_text,
                   bp.firm_name, wh.primary_sector AS firm_sector
              FROM bidder_pool bp
              JOIN parsed_notices p  ON p.notice_id = bp.notice_id
              JOIN raw_notices r     ON r.notice_id = bp.notice_id
              LEFT JOIN supplier_win_history wh ON wh.supplier_name = bp.firm_name
             WHERE bp.match_type IN ('mbie_evidence', 'csv_inferred')
               AND (r.close_date IS NULL OR r.close_date >= CURRENT_DATE)
               AND EXISTS (
                   SELECT 1 FROM scored_notices s
                    WHERE s.notice_id = bp.notice_id
                      AND (s.composite_score >= %s OR r.category_raw ILIKE '%%advance%%')
               )
            """,
            (_cfg.PRIORITY_THRESHOLD,),
        )

        flagged: set = set()
        for row in rows:
            firm_lower = (row["firm_name"] or "").lower().strip()
            if firm_lower in _EXCLUDED_FIRMS:
                continue
            if _is_mismatch(row.get("firm_sector"), row.get("notice_sector"),
                            row.get("combined_text") or ""):
                flagged.add(row["notice_id"])

        if not flagged:
            ok = True
            summary = "0 mismatch records — nothing to fix"
            logger.info("SCHEDULER: fix_bidder_mismatches — %s", summary)
        else:
            affected_ids = list(flagged)
            _excl_list = list(_EXCLUDED_FIRMS)
            _db.execute(
                """
                DELETE FROM bidder_pool
                 WHERE notice_id = ANY(%s)
                   AND match_type IN ('mbie_evidence', 'csv_inferred')
                   AND LOWER(firm_name) != ALL(%s)
                """,
                (affected_ids, _excl_list),
            )
            notice_rows = _db.fetchall(
                """
                SELECT s.notice_id, p.sector_tag, p.value_band, p.geographic_scope,
                       r.title, r.description, r.agency, r.category_raw
                  FROM scored_notices s
                  JOIN parsed_notices p ON p.notice_id = s.notice_id
                  JOIN raw_notices r    ON r.notice_id = s.notice_id
                 WHERE s.notice_id = ANY(%s)
                """,
                (affected_ids,),
            )
            all_bidders = load_bidders()
            stored = empty = failed = 0
            for notice in notice_rows:
                nid2 = notice["notice_id"]
                try:
                    bidders = _sbfn(notice, all_bidders)
                    if bidders:
                        _store_bidders(nid2, bidders)
                        stored += 1
                    else:
                        empty += 1
                except Exception as _e:
                    logger.warning("SCHEDULER: fix_bidder_mismatches %s — %s", nid2, _e)
                    failed += 1
            ok = True
            summary = (
                f"{len(affected_ids)} notices: "
                f"{stored} re-inferred, {empty} no bidders, {failed} errors"
            )
            logger.info("SCHEDULER: fix_bidder_mismatches complete — %s", summary)
    except Exception as exc:
        logger.exception("SCHEDULER: fix_bidder_mismatches error: %s", exc)
        summary = f"error: {str(exc)[:200]}"
    finally:
        if run_id is not None:
            try:
                import db as _db2
                _db2.execute(
                    "UPDATE pipeline_runs SET status=%s, summary=%s, finished_at=NOW() WHERE id=%s",
                    ("complete" if ok else "failed", summary, run_id),
                )
            except Exception as _e:
                logger.warning("SCHEDULER: pipeline_runs UPDATE failed (fix_bidder_mismatches): %s", _e)


def _mark_stale_runs_failed(max_hours: int = 4) -> None:
    """Mark any pipeline_runs row stuck in 'running' for over max_hours as failed.

    Called at Layer 2 start and by the hourly watchdog so a hung job is
    never displayed as 'Running' indefinitely in the admin panel.
    """
    try:
        import db as _db
        result = _db.execute(
            """
            UPDATE pipeline_runs
               SET status = 'failed',
                   finished_at = NOW(),
                   summary = 'Timed out — marked failed by watchdog after ' || %s || 'h'
             WHERE status = 'running'
               AND started_at < NOW() - INTERVAL '1 hour' * %s
            """,
            (max_hours, max_hours),
        )
        if result and hasattr(result, "rowcount") and result.rowcount:
            logger.warning(
                "SCHEDULER: marked %d stale pipeline_run(s) as failed (>%dh)",
                result.rowcount, max_hours,
            )
    except Exception as exc:
        logger.warning("SCHEDULER: _mark_stale_runs_failed error: %s", exc)


def _run_stale_job_watchdog() -> None:
    """Hourly watchdog — reaps jobs that have been 'running' for over 4 hours."""
    _mark_stale_runs_failed(max_hours=4)


def _run_layer2() -> None:
    """Layer 2: organisation seeding, awards ingestion, agency profiling, patterns."""
    logger.info("=" * 60)
    logger.info("SCHEDULER: Layer 2 pipeline starting at %s", datetime.utcnow().isoformat())
    logger.info("=" * 60)

    # Clean up any stale runs from a previous hung execution before starting.
    _mark_stale_runs_failed(max_hours=4)

    run_id = None
    layer2_ok = False
    try:
        import db as _db
        row = _db.fetchone(
            "INSERT INTO pipeline_runs (stage, triggered_by, status) "
            "VALUES ('layer2', 'scheduler', 'running') RETURNING id",
        )
        run_id = row["id"] if row else None
    except Exception as _e:
        logger.warning("SCHEDULER: pipeline_runs INSERT failed (layer2): %s", _e)

    try:
        import layer2_pipeline
        layer2_pipeline.main()
        layer2_ok = True
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
        if run_id is not None:
            try:
                import db as _db2
                _db2.execute(
                    "UPDATE pipeline_runs SET status=%s, finished_at=NOW() WHERE id=%s",
                    ("complete" if layer2_ok else "failed", run_id),
                )
            except Exception as _e:
                logger.warning("SCHEDULER: pipeline_runs UPDATE failed (layer2): %s", _e)

    if layer2_ok:
        try:
            _run_fix_bidder_mismatches()
        except Exception as exc:
            logger.error("SCHEDULER: _run_fix_bidder_mismatches raised: %s", exc)


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
    run_id = None
    try:
        import db as _db
        row = _db.fetchone(
            "INSERT INTO pipeline_runs (stage, triggered_by, status) "
            "VALUES ('watch_brief', 'scheduler', 'running') RETURNING id",
        )
        run_id = row["id"] if row else None
    except Exception as _e:
        logger.warning("SCHEDULER: pipeline_runs INSERT failed (watch_brief): %s", _e)

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
        brief_ok = not stats.get("error")
        brief_summary = (
            f"generated={stats.get('generated',0)} sent={stats.get('sent',0)} "
            f"failed={stats.get('failed',0)} skipped={stats.get('skipped',0)}"
        )
        if run_id is not None:
            try:
                import db as _db2
                _db2.execute(
                    "UPDATE pipeline_runs SET status=%s, summary=%s, finished_at=NOW() WHERE id=%s",
                    ("complete" if brief_ok else "failed", brief_summary, run_id),
                )
            except Exception as _e:
                logger.warning("SCHEDULER: pipeline_runs UPDATE failed (watch_brief): %s", _e)
        return stats
    except Exception as exc:
        logger.exception("SCHEDULER: Watch brief job error: %s", exc)
        if run_id is not None:
            try:
                import db as _db2
                _db2.execute(
                    "UPDATE pipeline_runs SET status=%s, summary=%s, finished_at=NOW() WHERE id=%s",
                    ("failed", f"error: {str(exc)[:200]}", run_id),
                )
            except Exception:
                pass
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


def _run_procurement_plans() -> None:
    """
    Monthly procurement plan scraper — first Sunday of the month at 03:00 NZT (15:00 UTC Sat).
    Refreshes all priority agency procurement plans before the AoG panel cache refresh (04:00 NZT)
    and firm enrichment (05:00 NZT) so all three are current for the week's Layer 1 runs.
    """
    logger.info("=" * 60)
    logger.info(
        "SCHEDULER: Procurement plan scraper starting at %s",
        datetime.utcnow().isoformat(),
    )
    logger.info("=" * 60)
    try:
        from procurement_plan_scraper import run_all_agency_plans
        results = run_all_agency_plans(force_refresh=False)
        success_count = sum(1 for r in results if r["status"] == "success")
        total_signals = sum(r["signals_extracted"] for r in results)
        logger.info(
            "SCHEDULER: Procurement plans complete — %d/%d agencies, %d signals extracted",
            success_count, len(results), total_signals,
        )
    except Exception as exc:
        logger.exception("SCHEDULER: Procurement plan scraper error: %s", exc)
        try:
            import mailer
            mailer.send_admin_only(
                subject="[Groundwork] Procurement plan scraper ERROR",
                html=f"<p>The monthly procurement plan scraper failed:</p><pre>{exc}</pre>",
            )
        except Exception:
            pass
    finally:
        logger.info(
            "SCHEDULER: Procurement plan scraper finished at %s",
            datetime.utcnow().isoformat(),
        )


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

    # ── Procurement plans: first Sunday of month at 03:00 NZT = Sat 15:00 UTC ─
    # day_of_week='sat' + day='1-7' matches the first Saturday of the month,
    # which is the Saturday night before the first Sunday morning in NZT.
    # This runs before AoG panel cache (04:00 NZT) and firm enrichment (05:00 NZT).
    scheduler.add_job(
        _run_procurement_plans,
        CronTrigger(day_of_week="sat", day="1-7", hour=15, minute=0, timezone="UTC"),
        id="procurement_plans_monthly",
        name="Monthly procurement plan scraper (Sun 03:00 NZT)",
        misfire_grace_time=14400,  # 4h grace — this is a low-urgency background job
        coalesce=True,
        max_instances=1,
    )

    # ── Stale-job watchdog: hourly ────────────────────────────────────────────
    scheduler.add_job(
        _run_stale_job_watchdog,
        "interval",
        hours=1,
        id="stale_job_watchdog",
        name="Stale-job watchdog (hourly)",
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
    )

    scheduler.start()

    # Log the scheduled jobs so Railway deploy logs show what's configured
    logger.info("SCHEDULER: APScheduler started — %d jobs registered:", len(scheduler.get_jobs()))
    for job in scheduler.get_jobs():
        logger.info("  • %s  next run: %s", job.name, job.next_run_time)

    # ── Startup catch-up check ────────────────────────────────────────────────
    # If Railway redeployed after the 06:00 NZT window, Layer 1 was missed.
    # Query pipeline_runs for the last successful Layer 1 run; if it's more
    # than 20 hours ago (or no record exists), fire Layer 1 immediately as a
    # one-off date job scheduled 30 seconds from now (gives Flask time to bind).
    _CATCHUP_THRESHOLD_HOURS = 20

    try:
        import db
        row = db.fetchone(
            """
            SELECT finished_at
            FROM   pipeline_runs
            WHERE  stage  = 'layer1'
              AND  status = 'complete'
            ORDER  BY finished_at DESC
            LIMIT  1
            """
        )
        now_utc = datetime.utcnow()
        if row and row.get("finished_at"):
            last_run = row["finished_at"].replace(tzinfo=None)  # strip tz for comparison
            hours_since = (now_utc - last_run).total_seconds() / 3600
            needs_catchup = hours_since > _CATCHUP_THRESHOLD_HOURS
            logger.info(
                "SCHEDULER: Last successful Layer 1 was %.1f hours ago — catchup needed: %s",
                hours_since, needs_catchup,
            )
        else:
            needs_catchup = True
            logger.info("SCHEDULER: No completed Layer 1 run found in pipeline_runs — scheduling catchup")

        if needs_catchup:
            from apscheduler.triggers.date import DateTrigger
            catchup_time = now_utc + timedelta(seconds=30)
            scheduler.add_job(
                _run_layer1,
                DateTrigger(run_date=catchup_time, timezone="UTC"),
                id="layer1_catchup",
                name="Layer 1 — startup catchup",
                max_instances=1,
            )
            logger.info("SCHEDULER: Layer 1 catchup job scheduled for %s UTC", catchup_time.isoformat())

    except Exception as exc:
        logger.warning("SCHEDULER: Startup catchup check failed — %s", exc)
