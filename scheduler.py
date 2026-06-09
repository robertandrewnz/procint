"""
Procint — Automated Job Scheduler

All jobs log to logs/scheduler.log. Failed jobs send an alert email to
ADMIN_EMAIL (set in .env). Each job records start time, end time, and
key record counts.

Cron-driven usage (recommended):
  Called by cron with --run-now <job> for each scheduled job.
  See COMMANDS.md for the full cron schedule.

Always-on usage (alternative):
  python scheduler.py [--dry-run]
  Blocks indefinitely, checking for pending jobs every 30 seconds.

Schedule:
  06:00 daily   — Layer 1  (GETS ingest → parse → score → enrich → bidders → watchlist)
  07:00 daily   — Layer 2  (GETS award scraping → org update → patterns → inject MI)
  07:30 daily   — Layer 3  (pursuit packages + portal refresh for active clients)
  08:00 Monday  — Watch brief (generate + email to BRIEFING_RECIPIENTS)
  05:00 1st/mo  — MBIE refresh (check for updated CSVs, re-ingest if changed)

Environment variables (all in .env):
  ADMIN_EMAIL, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM
  BRIEFING_RECIPIENTS   — comma-separated addresses for weekly brief
  L3_CLIENTS            — comma-separated client names for Layer 3
  L3_SECTORS            — comma-separated sector tags to filter watchlist (optional)
"""
import argparse
import logging
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import schedule

# ── Bootstrap config (must load .env before anything else) ────────────────────
sys.path.insert(0, str(Path(__file__).parent))
import config  # noqa: E402 — loads .env via dotenv_values

# ── Logging — file + stdout ───────────────────────────────────────────────────

LOGS_DIR = Path(__file__).parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)
LOG_FILE = LOGS_DIR / "scheduler.log"

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
    ],
)
logger = logging.getLogger("scheduler")

PROJECT_DIR = Path(__file__).parent
PYTHON = sys.executable  # same interpreter that launched this script


# ── SMTP helpers ──────────────────────────────────────────────────────────────

def _recipients() -> list[str]:
    raw = config.BRIEFING_RECIPIENTS or ""
    return [r.strip() for r in raw.split(",") if r.strip()]


def _admin_recipient() -> list[str]:
    admin = os.getenv("ADMIN_EMAIL", "").strip()
    if admin:
        return [admin]
    recs = _recipients()
    return recs[:1] if recs else []


def send_email(subject: str, html_body: str, recipients: list[str]) -> bool:
    """Send an HTML email via Resend. Returns True on success."""
    if not recipients:
        return False
    import mailer as _mailer
    ok = True
    for addr in recipients:
        ok = _mailer.send_email(addr, subject, html_body) and ok
    return ok


def alert(job_name: str, error: str, elapsed: Optional[float] = None) -> None:
    """Send a failure alert to the admin address."""
    to = _admin_recipient()
    if not to:
        logger.warning("No ADMIN_EMAIL configured — failure alert not sent")
        return
    elapsed_str = f"{elapsed:.0f}s" if elapsed else "unknown"
    subject = f"[PROCINT ALERT] {job_name} failed — {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    body = f"""<html><body style="font-family:system-ui;color:#e6edf3;background:#0d1117;padding:2rem;">
<h2 style="color:#ef4444;">&#9888; Pipeline Failure</h2>
<table style="font-size:14px;border-collapse:collapse;">
  <tr><td style="color:#7d8fa8;padding:4px 16px 4px 0;">Job</td><td>{job_name}</td></tr>
  <tr><td style="color:#7d8fa8;padding:4px 16px 4px 0;">Time</td><td>{datetime.now().isoformat()}</td></tr>
  <tr><td style="color:#7d8fa8;padding:4px 16px 4px 0;">Elapsed</td><td>{elapsed_str}</td></tr>
</table>
<h3 style="margin-top:1.5rem;color:#f97316;">Error output</h3>
<pre style="background:#161b22;padding:1rem;border-radius:6px;color:#f97316;font-size:12px;white-space:pre-wrap;">{error[:3000]}</pre>
<p style="color:#7d8fa8;font-size:12px;margin-top:1rem;">
  Log file: {LOG_FILE}<br>
  Project: {PROJECT_DIR}
</p>
</body></html>"""
    send_email(subject, body, to)


# ── Generic job runner ────────────────────────────────────────────────────────

def _run(
    job_name: str,
    cmd: list[str],
    timeout_seconds: int = 7200,
) -> bool:
    """
    Run a subprocess job, log start/end/counts/errors.
    Returns True on success.
    """
    start = datetime.now()
    logger.info("=" * 64)
    logger.info("JOB START: %s at %s", job_name, start.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("Command: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(PROJECT_DIR),
        )
        elapsed = (datetime.now() - start).total_seconds()

        # Log last 30 lines of stdout as a summary
        stdout_lines = [l for l in result.stdout.splitlines() if l.strip()]
        for line in stdout_lines[-30:]:
            logger.info("  %s", line)

        if result.returncode != 0:
            stderr_tail = result.stderr[-2000:] if result.stderr else "(no stderr)"
            logger.error(
                "JOB FAILED: %s in %.0fs (exit %d)\n%s",
                job_name, elapsed, result.returncode, stderr_tail,
            )
            alert(job_name, stderr_tail or result.stdout[-2000:], elapsed)
            return False

        logger.info(
            "JOB COMPLETE: %s in %.0fs",
            job_name, elapsed,
        )
        return True

    except subprocess.TimeoutExpired:
        elapsed = (datetime.now() - start).total_seconds()
        msg = f"Timed out after {timeout_seconds}s ({elapsed:.0f}s elapsed)"
        logger.error("JOB TIMEOUT: %s — %s", job_name, msg)
        alert(job_name, msg, elapsed)
        return False

    except Exception as exc:
        elapsed = (datetime.now() - start).total_seconds()
        logger.exception("JOB ERROR: %s — %s", job_name, exc)
        alert(job_name, str(exc), elapsed)
        return False

    finally:
        logger.info("=" * 64)


# ── Individual jobs ───────────────────────────────────────────────────────────

def run_layer1() -> bool:
    """
    06:00 daily — Full Layer 1 pipeline.
    Ingests fresh GETS active tender notices, parses, scores, enriches
    with Claude, runs MBIE-backed bidder inference, generates HTML/MD/JSON
    watchlist in output/.
    """
    return _run(
        "Layer 1 — GETS ingest + watchlist",
        [PYTHON, "run_pipeline.py"],
        timeout_seconds=7200,
    )


def run_layer2() -> bool:
    """
    07:00 daily — Full Layer 2 pipeline including live GETS award scraping.
    Scrapes new award notices from GETS → contract_awards.
    Updates organisations, runs agency profiling, pattern detection.
    Refreshes supplier_win_history after award scraping.
    Injects Market Intelligence section into today's watchlist HTML.
    """
    ok = _run(
        "Layer 2 — org update + award scraping + patterns",
        [PYTHON, "layer2_pipeline.py"],  # no --skip-awards: scrape daily
        timeout_seconds=7200,
    )
    if ok:
        # Refresh supplier_win_history after each daily award scrape
        try:
            from historical_data import refresh_win_history
            refresh_win_history()
            logger.info("supplier_win_history refreshed after Layer 2 awards run")
        except Exception as exc:
            logger.warning("supplier_win_history refresh failed: %s", exc)
    return ok


def run_layer3() -> bool:
    """
    07:30 daily — Layer 3 artefact generation for configured active clients.
    Generates pursuit packages for top-scored notices.
    Reads L3_CLIENTS from .env (comma-separated firm names).
    Reads L3_TOP from .env (number of notices per client, default 3).
    """
    clients_raw = os.getenv("L3_CLIENTS", "").strip()
    clients = [c.strip() for c in clients_raw.split(",") if c.strip()]

    if not clients:
        logger.info("Layer 3: no L3_CLIENTS configured in .env — skipping")
        return True

    top = int(os.getenv("L3_TOP", "3"))
    all_ok = True

    for client in clients:
        logger.info("Layer 3: generating packages for client '%s'", client)
        ok = _run(
            f"Layer 3 — {client}",
            [PYTHON, "layer3_pipeline.py",
             "--all-pursuits", "--top", str(top),
             "--client", client],
            timeout_seconds=3600,
        )
        if not ok:
            all_ok = False

    return all_ok


def run_weekly_brief() -> bool:
    """
    08:00 Monday — Generate weekly watch brief and email to recipients.
    Uses L3_CLIENTS as client names; falls back to 'Weekly Briefing'.
    """
    clients_raw = os.getenv("L3_CLIENTS", "").strip()
    clients = [c.strip() for c in clients_raw.split(",") if c.strip()]
    if not clients:
        clients = ["Procurement Intelligence Weekly"]

    sectors_raw = os.getenv("L3_SECTORS", "").strip()
    sectors = [s.strip() for s in sectors_raw.split(",") if s.strip()] or None
    recipients = _recipients()
    all_ok = True

    for client in clients:
        logger.info("Generating weekly brief for '%s'", client)
        try:
            from watch_brief import generate_watch_brief
            out_path = generate_watch_brief(client, sectors=sectors)

            if recipients:
                html = out_path.read_text(encoding="utf-8")
                week = date.today().strftime("Week of %-d %B %Y")
                subject = f"Procurement Intelligence — {week}"
                sent = send_email(subject, html, recipients)
                logger.info(
                    "Brief emailed to %d recipients: %s",
                    len(recipients), sent,
                )
            else:
                logger.warning("No BRIEFING_RECIPIENTS — brief saved but not emailed")

        except Exception as exc:
            logger.error("Weekly brief failed for '%s': %s", client, exc)
            alert(f"Weekly brief — {client}", str(exc))
            all_ok = False

    return all_ok


def run_mbie_refresh() -> bool:
    """
    05:00 1st of month — Check MBIE open data for updated files.
    Downloads changed files, re-ingests, rebuilds supplier_win_history.
    """
    logger.info("=== Monthly MBIE open data refresh ===")
    try:
        from refresh_mbie import run_mbie_refresh as _refresh
        result = _refresh(force=False, dry_run=False)
        changed = result.get("changed", [])
        errors = result.get("errors", [])
        logger.info(
            "MBIE refresh: %d files updated, %d errors",
            len(changed), len(errors),
        )
        if errors:
            alert("MBIE refresh", f"Errors on: {errors}")
            return False
        return True
    except Exception as exc:
        logger.exception("MBIE refresh failed: %s", exc)
        alert("MBIE monthly refresh", str(exc))
        return False


# ── Always-on schedule (alternative to cron) ──────────────────────────────────

def setup_always_on_schedule() -> None:
    """Configure schedule for always-on daemon mode."""
    schedule.every().day.at("06:00").do(run_layer1).tag("layer1")
    schedule.every().day.at("07:00").do(run_layer2).tag("layer2")
    schedule.every().day.at("07:30").do(run_layer3).tag("layer3")
    schedule.every().monday.at("08:00").do(run_weekly_brief).tag("brief")
    schedule.every().day.at("05:00").do(
        lambda: run_mbie_refresh() if datetime.today().day == 1 else None
    ).tag("mbie_check")

    logger.info("Always-on schedule configured:")
    for job in schedule.get_jobs():
        logger.info("  %-20s next run: %s", list(job.tags)[0], job.next_run)


# ── Entry point ───────────────────────────────────────────────────────────────

JOB_MAP = {
    "layer1":       run_layer1,
    "layer2":       run_layer2,
    "layer3":       run_layer3,
    "brief":        run_weekly_brief,
    "mbie-refresh": run_mbie_refresh,
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Procint scheduler — run jobs on demand or as a daemon",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Jobs (--run-now):
  layer1       06:00 daily   — GETS ingest, score, enrich, watchlist
  layer2       07:00 daily   — Award scraping, org update, patterns, MI inject
  layer3       07:30 daily   — Pursuit packages for L3_CLIENTS
  brief        08:00 Monday  — Weekly watch brief + email
  mbie-refresh 05:00 1st/mo  — MBIE CSV update check + re-ingest
        """,
    )
    parser.add_argument(
        "--run-now",
        choices=list(JOB_MAP.keys()),
        metavar="JOB",
        help="Run a specific job immediately and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="(daemon mode) Print schedule without running jobs",
    )
    args = parser.parse_args()

    if args.run_now:
        logger.info("Running job on demand: %s", args.run_now)
        ok = JOB_MAP[args.run_now]()
        sys.exit(0 if ok else 1)

    # Daemon mode
    setup_always_on_schedule()

    if args.dry_run:
        logger.info("Dry run — exiting.")
        sys.exit(0)

    logger.info("Daemon mode active. Ctrl+C to stop. Log: %s", LOG_FILE)
    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
