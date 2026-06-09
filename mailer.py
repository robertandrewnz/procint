"""
mailer.py — Centralised email delivery for Groundwork.

All outbound emails (pursuit ready, watch brief, admin copies) go through
this module so admin BCC, logging, and SMTP config are handled in one place.

Admin BCC policy
────────────────
ADMIN_EMAIL receives a separate copy of every email sent to a client.
It is a separate send (not a CC/BCC header) so client addresses are never
exposed to the admin in the To: header, and the admin copy is clearly
labelled "[ADMIN COPY]" in the subject.

Unsubscribe
───────────
A plain-text unsubscribe footer is appended to every HTML email body.
There is no automated list management — clients reply to ADMIN_EMAIL to
opt out, and the admin removes them from portal_config.json.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import config

logger = logging.getLogger("mailer")

# ── Unsubscribe footer ────────────────────────────────────────────────────────

_UNSUB_FOOTER = """
<div style="margin-top:2.5rem;padding-top:1rem;border-top:1px solid #dde2ea;
  font-size:.75rem;color:#8899aa;line-height:1.6;">
  You're receiving this because you have an active Groundwork subscription.<br>
  To unsubscribe or update your email preferences, reply to this email or contact
  <a href="mailto:{admin}" style="color:#8899aa;">{admin}</a>.
</div>
"""


def _smtp_configured() -> bool:
    return bool(
        config.SMTP_HOST
        and config.SMTP_USER
        and config.SMTP_PASSWORD
        and config.SMTP_FROM
    )


def _admin_email() -> Optional[str]:
    e = os.getenv("ADMIN_EMAIL", "").strip()
    return e if e else None


_SMTP_TIMEOUT = 10  # seconds — prevents a hung SMTP server from killing a Gunicorn worker


def _raw_send(subject: str, html: str, to: list[str]) -> bool:
    """
    Send a single email. Returns True on success, False on any failure.
    Never raises — all exceptions are caught, logged, and swallowed so that
    a broken SMTP connection can never crash a web request or worker process.
    """
    if not _smtp_configured():
        logger.warning(
            "SMTP not configured — email skipped. "
            "Set SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM. "
            "Subject: %s → %s", subject, to
        )
        return False
    logger.info("SMTP: attempting send — host=%s port=%s user=%s from=%s to=%s subject=%s",
                config.SMTP_HOST, config.SMTP_PORT,
                config.SMTP_USER[:4] + "…" if config.SMTP_USER else "(none)",
                config.SMTP_FROM, to, subject[:60])
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = config.SMTP_FROM
        msg["To"] = ", ".join(to)
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT,
                          timeout=_SMTP_TIMEOUT) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.sendmail(config.SMTP_FROM, to, msg.as_string())
        logger.info("SMTP: sent OK — %s → %s", subject[:60], to)
        return True
    except smtplib.SMTPAuthenticationError as exc:
        logger.error("SMTP: authentication failed (check SMTP_USERNAME/SMTP_PASSWORD) — %s", exc)
        return False
    except smtplib.SMTPException as exc:
        logger.error("SMTP: send failed (%s → %s): %s", subject[:60], to, exc)
        return False
    except OSError as exc:
        logger.error("SMTP: connection error (host=%s port=%s timeout=%ss): %s",
                     config.SMTP_HOST, config.SMTP_PORT, _SMTP_TIMEOUT, exc)
        return False
    except Exception as exc:
        logger.error("SMTP: unexpected error (%s → %s): %s", subject[:60], to, exc)
        return False


def _with_footer(html: str) -> str:
    """Append the unsubscribe footer to an HTML email body."""
    admin = _admin_email() or config.SMTP_FROM or ""
    footer = _UNSUB_FOOTER.format(admin=admin)
    # Insert before </body> if present, otherwise append
    if "</body>" in html:
        return html.replace("</body>", footer + "\n</body>", 1)
    return html + footer


def send_to_client(
    subject: str,
    html: str,
    client_email: str,
    admin_subject_prefix: str = "[ADMIN COPY]",
    add_footer: bool = True,
) -> bool:
    """
    Send an email to a client, then send a separate copy to the admin.
    Returns True if the client send succeeded (admin copy failure is non-fatal).
    """
    if add_footer:
        html = _with_footer(html)

    ok = _raw_send(subject, html, [client_email])

    # Admin copy — separate send, clearly labelled
    admin = _admin_email()
    if admin and admin != client_email:
        admin_html = (
            f'<div style="background:#fff3cd;border:1px solid #ffc107;border-radius:6px;'
            f'padding:.6rem 1rem;margin-bottom:1.5rem;font-size:.82rem;color:#856404;">'
            f'<strong>Admin copy</strong> — sent to client: <code>{client_email}</code>'
            f'</div>'
        ) + html
        _raw_send(f"{admin_subject_prefix} {subject}", admin_html, [admin])

    return ok


def send_admin_only(subject: str, html: str) -> bool:
    """Send a notification to the admin only (e.g. new request submitted, pipeline error)."""
    admin = _admin_email()
    if not admin:
        logger.warning("ADMIN_EMAIL not set — admin-only email skipped: %s", subject)
        return False
    return _raw_send(subject, html, [admin])


# ── Specific email templates ──────────────────────────────────────────────────

def send_pursuit_ready(
    client_name: str,
    client_email: str,
    notice_title: str,
    notice_id: str,
    portal_url: str,
) -> bool:
    """Email sent to client when their pursuit package is ready."""
    subject = f"Your pursuit package is ready — {notice_title[:60]}"
    html = f"""
<div style="font-family:'Inter',system-ui,sans-serif;max-width:600px;
  margin:0 auto;background:#fff;color:#1a2d4a;">
  <div style="background:#1a2d4a;padding:1.5rem 2rem;">
    <div style="font-size:1rem;font-weight:800;color:#fff;letter-spacing:-.01em;">
      Groundwork <span style="color:#2a9d8f;font-weight:400;">by BidEdge</span></div>
  </div>
  <div style="padding:2rem 2rem 1.5rem;">
    <h2 style="font-size:1.25rem;font-weight:800;color:#1a2d4a;margin:0 0 .75rem;">
      Your pursuit package is ready</h2>
    <p style="color:#4a5568;line-height:1.7;margin:0 0 1.25rem;">
      Hi {client_name},<br><br>
      Your pursuit intelligence package for the following opportunity has been generated
      and is now available in your Pursuits library:</p>
    <div style="background:#f7f9fc;border:1px solid #dde2ea;border-radius:8px;
      padding:1rem 1.25rem;margin-bottom:1.5rem;">
      <div style="font-size:.72rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
        color:#2a9d8f;margin-bottom:.35rem;">Opportunity</div>
      <div style="font-size:.95rem;font-weight:700;color:#1a2d4a;line-height:1.4;">
        {notice_title}</div>
      <div style="font-size:.78rem;color:#8899aa;margin-top:.25rem;">
        GETS ref: {notice_id}</div>
    </div>
    <a href="{portal_url}" style="display:inline-block;background:#2a9d8f;color:#fff;
      font-weight:700;font-size:.9rem;padding:.7rem 1.5rem;border-radius:6px;
      text-decoration:none;">View in Pursuits library &rarr;</a>
    <p style="color:#8899aa;font-size:.82rem;line-height:1.6;margin-top:1.5rem;">
      The package includes your win position assessment, competitive landscape,
      agency history, and recommended actions. Log in to Groundwork to view it.</p>
  </div>
</div>
"""
    return send_to_client(subject, html, client_email)


def send_request_confirmation(
    client_name: str,
    client_email: str,
    notice_id: str,
    notice_title: str,
    urgent: bool = False,
) -> bool:
    """Confirmation email sent immediately when a request is submitted."""
    eta = "within the hour" if urgent else "within 24 hours"
    subject = f"Pursuit package request received — {notice_title[:60]}"
    html = f"""
<div style="font-family:'Inter',system-ui,sans-serif;max-width:600px;
  margin:0 auto;background:#fff;color:#1a2d4a;">
  <div style="background:#1a2d4a;padding:1.5rem 2rem;">
    <div style="font-size:1rem;font-weight:800;color:#fff;letter-spacing:-.01em;">
      Groundwork <span style="color:#2a9d8f;font-weight:400;">by BidEdge</span></div>
  </div>
  <div style="padding:2rem 2rem 1.5rem;">
    <h2 style="font-size:1.25rem;font-weight:800;color:#1a2d4a;margin:0 0 .75rem;">
      Request received</h2>
    <p style="color:#4a5568;line-height:1.7;margin:0 0 1.25rem;">
      Hi {client_name},<br><br>
      We've received your request for a pursuit intelligence package.
      {"<strong>This notice closes soon — your package will be prioritised.</strong><br><br>" if urgent else ""}
      Your package will be ready {eta}. We'll email you as soon as it's available
      in your Pursuits library.</p>
    <div style="background:#f7f9fc;border:1px solid #dde2ea;border-radius:8px;
      padding:1rem 1.25rem;margin-bottom:1.5rem;">
      <div style="font-size:.72rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
        color:#2a9d8f;margin-bottom:.35rem;">Opportunity</div>
      <div style="font-size:.95rem;font-weight:700;color:#1a2d4a;">{notice_title}</div>
      <div style="font-size:.78rem;color:#8899aa;margin-top:.25rem;">GETS ref: {notice_id}</div>
    </div>
  </div>
</div>
"""
    return send_to_client(subject, html, client_email)


def send_watch_brief_email(
    client_name: str,
    client_email: str,
    brief_html: str,
    week_label: str,
) -> bool:
    """Send the weekly watch brief as inline HTML to a client."""
    subject = f"Groundwork Weekly Intelligence Brief — {week_label}"
    # Wrap brief HTML in email chrome
    full_html = f"""
<div style="font-family:'Inter',system-ui,sans-serif;max-width:700px;margin:0 auto;
  background:#fff;color:#1a2d4a;">
  <div style="background:#1a2d4a;padding:1.5rem 2rem;display:flex;
    justify-content:space-between;align-items:center;">
    <div style="font-size:1rem;font-weight:800;color:#fff;letter-spacing:-.01em;">
      Groundwork <span style="color:#2a9d8f;font-weight:400;">by BidEdge</span></div>
    <div style="font-size:.78rem;color:rgba(255,255,255,.5);">{week_label}</div>
  </div>
  <div style="padding:0;">
    {brief_html}
  </div>
</div>
"""
    return send_to_client(subject, full_html, client_email)


def notify_admin_new_request(
    client_name: str,
    client_id: str,
    notice_id: str,
    request_type: str,
    priority: str,
    details: str,
) -> bool:
    """Notify admin when any client submits a request."""
    subject = f"[Groundwork] New {request_type.title()} Request — {client_name}"
    html = f"""
<p><b>Client:</b> {client_name} (<code>{client_id}</code>)<br>
<b>Type:</b> {request_type}<br>
<b>Notice ID:</b> {notice_id or '—'}<br>
<b>Priority:</b> {priority}</p>
<p><b>Details:</b><br>{details or '—'}</p>
"""
    return send_admin_only(subject, html)


def send_signup_confirmation(
    name: str,
    email: str,
    plan_label: str,
) -> bool:
    """
    Confirmation email sent immediately to the prospect after they submit the
    signup form. Non-blocking — failure is logged but never surfaced to the user.
    """
    first = name.split()[0] if name else "there"
    admin_email = _admin_email() or "hello@bidedge.co.nz"
    subject = "Thanks for your interest in Groundwork by BidEdge"
    html = f"""
<div style="font-family:'Inter',system-ui,sans-serif;max-width:600px;
  margin:0 auto;background:#fff;color:#1a2d4a;">
  <div style="background:#1a2d4a;padding:1.5rem 2rem;">
    <div style="font-size:1rem;font-weight:800;color:#fff;letter-spacing:-.01em;">
      Groundwork <span style="color:#2a9d8f;font-weight:400;">by BidEdge</span></div>
  </div>
  <div style="padding:2rem 2rem 1.5rem;">
    <h2 style="font-size:1.2rem;font-weight:800;color:#1a2d4a;margin:0 0 1rem;">
      Thanks for your interest, {first}.</h2>
    <p style="color:#4a5568;line-height:1.7;margin:0 0 1rem;">
      We've received your enquiry for the <strong>{plan_label}</strong> plan and someone from
      the BidEdge team will be in touch within one business day.</p>
    <p style="color:#4a5568;line-height:1.7;margin:0 0 1rem;">
      Groundwork scans every New Zealand government procurement notice daily, scores
      them against your firm's profile, and delivers ranked intelligence so you can
      focus pursuit effort where it counts — rather than trawling GETS yourself.</p>
    <p style="color:#4a5568;line-height:1.7;margin:0 0 1.5rem;">
      In the meantime, explore a sample of what Groundwork delivers at
      <a href="https://bidedge.co.nz/demo" style="color:#2a9d8f;">bidedge.co.nz/demo</a>.</p>
    <div style="border-top:1px solid #dde2ea;margin-top:1.5rem;padding-top:1.25rem;
      font-size:.84rem;color:#4a5568;line-height:1.7;">
      If you have any immediate questions, reply to this email or contact us at
      <a href="mailto:{admin_email}" style="color:#2a9d8f;">{admin_email}</a>.<br><br>
      The BidEdge Team
    </div>
  </div>
</div>
"""
    return send_to_client(subject, html, email, add_footer=False)


def send_watchlist_ready(
    client_name: str,
    client_email: str,
    notice_count: int,
    portal_url: str,
    date_label: str,
) -> bool:
    """
    Email sent to each active client after Layer 1 pipeline completes with new notices.
    Only called when notice_count > 0.
    """
    subject = f"Your Groundwork watchlist is ready — {date_label}"
    html = f"""
<div style="font-family:'Inter',system-ui,sans-serif;max-width:600px;
  margin:0 auto;background:#fff;color:#1a2d4a;">
  <div style="background:#1a2d4a;padding:1.5rem 2rem;">
    <div style="font-size:1rem;font-weight:800;color:#fff;letter-spacing:-.01em;">
      Groundwork <span style="color:#2a9d8f;font-weight:400;">by BidEdge</span></div>
  </div>
  <div style="padding:2rem 2rem 1.5rem;">
    <h2 style="font-size:1.2rem;font-weight:800;color:#1a2d4a;margin:0 0 .75rem;">
      Your daily watchlist is ready</h2>
    <p style="color:#4a5568;line-height:1.7;margin:0 0 1.25rem;">
      Hi {client_name},<br><br>
      Your daily procurement intelligence watchlist has been updated.
      <strong>{notice_count}</strong> {"opportunity has" if notice_count == 1 else "opportunities have"}
      been scored and ranked for your sectors today.</p>
    <a href="{portal_url}" style="display:inline-block;background:#2a9d8f;color:#fff;
      font-weight:700;font-size:.9rem;padding:.7rem 1.5rem;border-radius:6px;
      text-decoration:none;">View your watchlist &rarr;</a>
  </div>
</div>
"""
    return send_to_client(subject, html, client_email)


def send_competitor_profile_ready(
    client_name: str,
    client_email: str,
    firm_name: str,
    portal_url: str,
) -> bool:
    """Email sent to client when their competitor profile is ready."""
    subject = f"Your competitor profile is ready — {firm_name}"
    html = f"""
<div style="font-family:'Inter',system-ui,sans-serif;max-width:600px;
  margin:0 auto;background:#fff;color:#1a2d4a;">
  <div style="background:#1a2d4a;padding:1.5rem 2rem;">
    <div style="font-size:1rem;font-weight:800;color:#fff;letter-spacing:-.01em;">
      Groundwork <span style="color:#2a9d8f;font-weight:400;">by BidEdge</span></div>
  </div>
  <div style="padding:2rem 2rem 1.5rem;">
    <h2 style="font-size:1.2rem;font-weight:800;color:#1a2d4a;margin:0 0 .75rem;">
      Your competitor profile is ready</h2>
    <p style="color:#4a5568;line-height:1.7;margin:0 0 1.25rem;">
      Hi {client_name},<br><br>
      Your competitor intelligence profile for <strong>{firm_name}</strong> has been
      generated and is now available in your Competitors library.</p>
    <a href="{portal_url}" style="display:inline-block;background:#2a9d8f;color:#fff;
      font-weight:700;font-size:.9rem;padding:.7rem 1.5rem;border-radius:6px;
      text-decoration:none;">View competitor profile &rarr;</a>
  </div>
</div>
"""
    return send_to_client(subject, html, client_email)
