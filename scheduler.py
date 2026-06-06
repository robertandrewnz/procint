"""
Layer 3 — Automated Briefing Scheduler.

Runs the pipeline on a schedule:
  - Daily at 06:00: Layer 1 (ingest, parse, score, enrich, bidder inference)
  - Daily at 07:00: Layer 2 (org seeding, awards, agency profiles, patterns)
  - Monday at 08:00: Weekly watch brief generation + email to recipients

Usage:
  python scheduler.py [--dry-run]

  --dry-run: print schedule without actually running jobs

SMTP configuration (in .env):
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM
  BRIEFING_RECIPIENTS  (comma-separated email addresses)
"""
import argparse
import logging
import smtplib
import subprocess
import sys
import time
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import schedule

import config

logger = logging.getLogger(__name__)


# ── Email ─────────────────────────────────────────────────────────────────────

def _send_email(subject: str, html_body: str, recipients: list[str]) -> bool:
    """Send an HTML email via SMTP. Returns True on success."""
    if not all([config.SMTP_HOST, config.SMTP_USER, config.SMTP_PASSWORD, config.SMTP_FROM]):
        logger.warning("SMTP not configured — skipping email. Set SMTP_* in .env")
        return False
    if not recipients:
        logger.warning("No recipients configured — set BRIEFING_RECIPIENTS in .env")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = config.SMTP_FROM
        msg["To"] = ", ".join(recipients)
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.sendmail(config.SMTP_FROM, recipients, msg.as_string())

        logger.info("Email sent to %d recipients: %s", len(recipients), subject)
        return True
    except Exception as exc:
        logger.error("Email send failed: %s", exc)
        return False


def _send_failure_alert(job_name: str, error: str) -> None:
    """Send a failure notification email."""
    alert_email = config.SMTP_FROM
    if not alert_email:
        return
    subject = f"[PROCINT ALERT] {job_name} failed — {date.today().isoformat()}"
    body = f"""<html><body style="font-family:system-ui;color:#e6edf3;background:#0d1117;padding:2rem;">
<h2 style="color:#ef4444;">Pipeline Failure Alert</h2>
<p><strong>Job:</strong> {job_name}</p>
<p><strong>Time:</strong> {datetime.now().isoformat()}</p>
<p><strong>Error:</strong></p>
<pre style="background:#161b22;padding:1rem;border-radius:6px;color:#f97316;">{error[:2000]}</pre>
</body></html>"""
    recipients = [r.strip() for r in (config.BRIEFING_RECIPIENTS or alert_email).split(",") if r.strip()]
    _send_email(subject, body, recipients[:1])  # Alert only first recipient


# ── Job definitions ───────────────────────────────────────────────────────────

def run_layer1():
    """Daily Layer 1 pipeline: ingest, parse, score, enrich, bidder inference."""
    logger.info("=== SCHEDULER: Starting Layer 1 pipeline ===")
    try:
        result = subprocess.run(
            [sys.executable, "run_pipeline.py"],
            capture_output=True, text=True, timeout=3600,
            cwd=Path(__file__).parent,
        )
        if result.returncode != 0:
            logger.error("Layer 1 failed:\n%s", result.stderr[-2000:])
            _send_failure_alert("Layer 1 pipeline", result.stderr[-2000:])
        else:
            logger.info("Layer 1 complete:\n%s", result.stdout[-500:])
    except subprocess.TimeoutExpired:
        logger.error("Layer 1 timed out after 60 minutes")
        _send_failure_alert("Layer 1 pipeline", "Timed out after 60 minutes")
    except Exception as exc:
        logger.error("Layer 1 scheduler error: %s", exc)
        _send_failure_alert("Layer 1 pipeline", str(exc))


def run_layer2():
    """Daily Layer 2 pipeline: org seeding, awards, profiles, patterns."""
    logger.info("=== SCHEDULER: Starting Layer 2 pipeline ===")
    try:
        result = subprocess.run(
            [sys.executable, "layer2_pipeline.py", "--skip-awards"],
            capture_output=True, text=True, timeout=3600,
            cwd=Path(__file__).parent,
        )
        if result.returncode != 0:
            logger.error("Layer 2 failed:\n%s", result.stderr[-2000:])
            _send_failure_alert("Layer 2 pipeline", result.stderr[-2000:])
        else:
            logger.info("Layer 2 complete:\n%s", result.stdout[-500:])
    except subprocess.TimeoutExpired:
        logger.error("Layer 2 timed out after 60 minutes")
        _send_failure_alert("Layer 2 pipeline", "Timed out after 60 minutes")
    except Exception as exc:
        logger.error("Layer 2 scheduler error: %s", exc)
        _send_failure_alert("Layer 2 pipeline", str(exc))


def run_weekly_brief():
    """
    Monday 08:00: Generate watch brief and email to all configured recipients.
    Uses a default client name — customise per deployment.
    """
    logger.info("=== SCHEDULER: Generating weekly watch brief ===")
    client_name = "Weekly Briefing Subscriber"

    try:
        from watch_brief import generate_watch_brief
        out_path = generate_watch_brief(client_name)

        recipients = [
            r.strip() for r in config.BRIEFING_RECIPIENTS.split(",")
            if r.strip()
        ]
        if not recipients:
            logger.warning("No BRIEFING_RECIPIENTS configured — brief generated but not emailed")
            return

        html_content = out_path.read_text(encoding="utf-8")
        week_label = date.today().strftime("Week of %-d %B %Y")
        subject = f"Procurement Intelligence — {week_label}"
        success = _send_email(subject, html_content, recipients)
        if success:
            logger.info("Weekly brief emailed to %d recipients", len(recipients))
    except Exception as exc:
        logger.error("Weekly brief failed: %s", exc)
        _send_failure_alert("Weekly watch brief", str(exc))


# ── Schedule setup ─────────────────────────────────────────────────────────────

def setup_schedule() -> None:
    schedule.every().day.at("06:00").do(run_layer1).tag("layer1")
    schedule.every().day.at("07:00").do(run_layer2).tag("layer2")
    schedule.every().monday.at("08:00").do(run_weekly_brief).tag("weekly_brief")

    logger.info("Schedule configured:")
    for job in schedule.get_jobs():
        logger.info("  %s — next run: %s", job.tags, job.next_run)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(f"scheduler_{date.today().strftime('%Y%m')}.log"),
        ],
    )

    parser = argparse.ArgumentParser(description="Procint automated briefing scheduler")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print schedule without running jobs")
    parser.add_argument("--run-now", choices=["layer1", "layer2", "brief"],
                        help="Run a specific job immediately and exit")
    args = parser.parse_args()

    if args.run_now:
        if args.run_now == "layer1":
            run_layer1()
        elif args.run_now == "layer2":
            run_layer2()
        elif args.run_now == "brief":
            run_weekly_brief()
        sys.exit(0)

    setup_schedule()

    if args.dry_run:
        logger.info("Dry run — schedule printed above. Exiting.")
        sys.exit(0)

    logger.info("Scheduler running. Press Ctrl+C to stop.")
    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info("Scheduler stopped.")
