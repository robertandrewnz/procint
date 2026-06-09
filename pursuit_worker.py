"""
pursuit_worker.py — Background pursuit package generation.

Called from portal.py when a client submits a request via gw_request().
Runs in a daemon thread so the web response returns immediately.

Flow:
  1. Mark request status → 'generating'
  2. Call generate_pursuit_package()
  3. Mark status → 'complete', store output_path
  4. Email client via mailer.send_pursuit_ready()
  5. On any error: mark status → 'failed', email admin

The portal_url passed in should be the absolute URL to the client's
Pursuits library page (e.g. https://app.bidedge.co.nz/groundwork/pursuits).
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import db
import mailer

logger = logging.getLogger("pursuit_worker")


# ── DB helpers ────────────────────────────────────────────────────────────────

def create_request(
    client_id: str,
    notice_id: str,
    request_type: str = "pursuit",
    details: str = "",
    priority: str = "normal",
) -> int:
    """Insert a new pursuit_requests row and return its id."""
    row = db.fetchone(
        """
        INSERT INTO pursuit_requests
            (client_id, notice_id, request_type, details, priority, status, requested_at)
        VALUES (%s, %s, %s, %s, %s, 'pending', NOW())
        RETURNING id
        """,
        (client_id, notice_id, request_type, details, priority),
    )
    return row["id"]


def _set_status(req_id: int, status: str, **kwargs) -> None:
    """Update status and optional extra columns on a pursuit_requests row."""
    sets = ["status = %s"]
    vals = [status]
    if status == "generating":
        sets.append("started_at = NOW()")
    if status in ("complete", "failed"):
        sets.append("completed_at = NOW()")
    for col in ("output_path", "error_message"):
        if col in kwargs:
            sets.append(f"{col} = %s")
            vals.append(kwargs[col])
    vals.append(req_id)
    db.execute(f"UPDATE pursuit_requests SET {', '.join(sets)} WHERE id = %s", vals)


def _log_brief_send(client_id: str, sectors: list, status: str, error_msg: str = "") -> None:
    db.execute(
        """
        INSERT INTO brief_sends (client_id, sectors, status, error_msg, sent_at)
        VALUES (%s, %s, %s, %s, NOW())
        """,
        (client_id, sectors, status, error_msg),
    )


# ── Days-until-close helper ───────────────────────────────────────────────────

def _days_until_close(notice_id: str) -> Optional[int]:
    row = db.fetchone(
        "SELECT days_until_close FROM parsed_notices WHERE notice_id = %s",
        (notice_id,),
    )
    if row and row.get("days_until_close") is not None:
        return int(row["days_until_close"])
    return None


# ── Generation worker ─────────────────────────────────────────────────────────

def _generate(
    req_id: int,
    client_id: str,
    client_name: str,
    client_email: str,
    notice_id: str,
    preferred_sectors: list,
    artefact_slug: str,
    portal_url: str,
) -> None:
    """Runs in a background thread. Does not raise — all errors are logged."""
    logger.info("WORKER: Starting pursuit package req_id=%s notice=%s client=%s",
                req_id, notice_id, client_id)
    _set_status(req_id, "generating")
    try:
        from pursuit_package import generate_pursuit_package, _artefact_dir

        output_dir = _artefact_dir(client_name)
        html_path = generate_pursuit_package(
            notice_id=notice_id,
            client_name=client_name,
            output_dir=output_dir,
            preferred_sectors=preferred_sectors or [],
        )
        # Store path relative to ARTEFACTS_DIR so it's portable
        import config
        artefacts_root = Path(config.ARTEFACTS_DIR)
        try:
            rel = str(html_path.relative_to(artefacts_root))
        except ValueError:
            rel = str(html_path)

        _set_status(req_id, "complete", output_path=rel)
        logger.info("WORKER: Complete req_id=%s → %s", req_id, rel)

        # Build a direct link to the file using serve_artefact_file route.
        # rel is like "<slug>/<filename>.html" — first component is the slug.
        rel_parts = Path(rel).parts
        if len(rel_parts) >= 2:
            file_slug = rel_parts[0]
            file_path = str(Path(*rel_parts[1:]))
            # portal_url is the base URL of the request (e.g. https://app.bidedge.co.nz)
            # Strip any path component — we only want the scheme+host
            from urllib.parse import urlparse, urlunparse
            parsed = urlparse(portal_url)
            base = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
            direct_url = f"{base}/groundwork/files/{file_slug}/{file_path}"
        else:
            direct_url = portal_url  # fallback to library page

        # Fetch notice title for the email
        row = db.fetchone("SELECT title FROM raw_notices WHERE notice_id = %s", (notice_id,))
        notice_title = (row or {}).get("title") or notice_id

        if client_email:
            mailer.send_pursuit_ready(
                client_name=client_name,
                client_email=client_email,
                notice_title=notice_title,
                notice_id=notice_id,
                portal_url=direct_url,
            )
        else:
            logger.warning("WORKER: No email for client %s — skipping notification", client_id)

    except Exception as exc:
        logger.exception("WORKER: Failed req_id=%s: %s", req_id, exc)
        _set_status(req_id, "failed", error_message=str(exc)[:500])
        mailer.send_admin_only(
            subject=f"[Groundwork] Pursuit generation FAILED — {client_name} / {notice_id}",
            html=(f"<p><b>req_id:</b> {req_id}<br>"
                  f"<b>client:</b> {client_name} ({client_id})<br>"
                  f"<b>notice:</b> {notice_id}</p>"
                  f"<pre style='background:#f5f5f5;padding:1rem;font-size:.8rem;'>"
                  f"{exc}</pre>"),
        )


def dispatch(
    req_id: int,
    client_id: str,
    client_name: str,
    client_email: str,
    notice_id: str,
    preferred_sectors: list,
    artefact_slug: str,
    portal_url: str,
    immediate: bool = False,
) -> None:
    """
    Dispatch a generation job.
    immediate=True → start a thread now (urgent notices, closes within 7 days).
    immediate=False → also starts a thread, but tagged as queued so the scheduler
                      can pick it up if the thread pool is busy.
    Either way generation begins within seconds on Railway (single process per dyno).
    """
    t = threading.Thread(
        target=_generate,
        args=(req_id, client_id, client_name, client_email,
              notice_id, preferred_sectors, artefact_slug, portal_url),
        daemon=True,
        name=f"pursuit-{req_id}",
    )
    t.start()
    logger.info("WORKER: Dispatched thread pursuit-%s (immediate=%s)", req_id, immediate)


# ── Watch brief per-client delivery ──────────────────────────────────────────

def send_all_watch_briefs(portal_base_url: str = "") -> None:
    """
    Generate and email a watch brief for every active non-admin portal client.
    Called by the APScheduler watch brief job in scheduler_railway.py.
    Reads client list from portal_config.json so no DB dependency for user data.
    """
    import json
    from pathlib import Path
    from datetime import date

    cfg_path = Path("portal_config.json")
    if not cfg_path.exists():
        logger.error("BRIEFS: portal_config.json not found")
        return

    cfg = json.loads(cfg_path.read_text())
    clients = cfg.get("clients", {})

    week_label = date.today().strftime("%-d %B %Y")

    sent = failed = skipped = 0
    for username, data in clients.items():
        if data.get("is_admin"):
            continue
        email = data.get("email", "").strip()
        if not email:
            logger.warning("BRIEFS: %s has no email — skipped", username)
            skipped += 1
            continue

        display_name = data.get("display_name", username)
        sectors = (
            data.get("preferred_sectors")
            or data.get("sectors")
            or None
        )

        logger.info("BRIEFS: Generating brief for %s (%s) sectors=%s",
                    username, email, sectors)
        try:
            from watch_brief import generate_watch_brief
            brief_path = generate_watch_brief(
                client_name=display_name,
                sectors=sectors,
            )
            brief_html = brief_path.read_text(encoding="utf-8")

            ok = mailer.send_watch_brief_email(
                client_name=display_name,
                client_email=email,
                brief_html=brief_html,
                week_label=week_label,
            )
            status = "sent" if ok else "failed"
            _log_brief_send(username, sectors or [], status)
            if ok:
                sent += 1
            else:
                failed += 1
        except Exception as exc:
            logger.exception("BRIEFS: Failed for %s: %s", username, exc)
            _log_brief_send(username, sectors or [], "failed", str(exc)[:500])
            failed += 1

    logger.info("BRIEFS: Complete — sent=%d failed=%d skipped=%d", sent, failed, skipped)

    # Admin summary
    mailer.send_admin_only(
        subject=f"[Groundwork] Watch brief batch complete — {week_label}",
        html=(f"<p>Watch brief batch finished for week of <b>{week_label}</b>.</p>"
              f"<ul><li>Sent: {sent}</li><li>Failed: {failed}</li>"
              f"<li>Skipped (no email): {skipped}</li></ul>"),
    )
