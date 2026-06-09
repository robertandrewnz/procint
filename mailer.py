"""
mailer.py — Centralised email delivery for Groundwork (Resend API).

All outbound emails go through this module. Resend is used instead of
direct SMTP because Railway blocks outbound SMTP on all plans.

Transport
─────────
Requires RESEND_API_KEY environment variable (set in Railway Variables).
If the key is absent every send is skipped with a WARNING — the app never
crashes on a missing key.

Admin copy policy
─────────────────
ADMIN_EMAIL receives a separate copy of every client email, clearly
labelled "[ADMIN COPY]" in the subject. It is a separate Resend call so
client addresses are never visible to the admin in the To: header.

Async sends
───────────
send_async() dispatches every send in a daemon thread so email never
blocks a web request or a pipeline step. Use it everywhere except in
background threads that are themselves already off the request path
(pursuit_worker, scheduler jobs) where send_email() is fine.

Unsubscribe footer
──────────────────
_with_footer() appends a plain-text unsubscribe notice to client HTML.
There is no automated list management — clients reply to ADMIN_EMAIL.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

logger = logging.getLogger("mailer")

_FROM_ADDRESS = "Groundwork by BidEdge <robert@bidedge.co.nz>"

# ── Unsubscribe footer ────────────────────────────────────────────────────────

_UNSUB_FOOTER = """
<div style="margin-top:2.5rem;padding-top:1rem;border-top:1px solid #dde2ea;
  font-size:.75rem;color:#8899aa;line-height:1.6;">
  You're receiving this because you have an active Groundwork subscription.<br>
  To unsubscribe or update your email preferences, reply to this email or contact
  <a href="mailto:{admin}" style="color:#8899aa;">{admin}</a>.
</div>
"""


# ── Core send primitives ──────────────────────────────────────────────────────

def _resend_configured() -> bool:
    key = os.getenv("RESEND_API_KEY", "").strip()
    if not key:
        return False
    # Basic sanity check — Resend keys start with "re_"
    return True


def _admin_email() -> Optional[str]:
    e = os.getenv("ADMIN_EMAIL", "").strip()
    return e if e else None


def _with_footer(html: str) -> str:
    """Append the unsubscribe footer to an HTML email body."""
    admin = _admin_email() or "hello@bidedge.co.nz"
    footer = _UNSUB_FOOTER.format(admin=admin)
    if "</body>" in html:
        return html.replace("</body>", footer + "\n</body>", 1)
    return html + footer


def send_email(
    to: str | list[str],
    subject: str,
    html: str,
    from_address: str = _FROM_ADDRESS,
) -> bool:
    """
    Send a single email via Resend. Returns True on success.
    Never raises — all errors are caught and logged.
    Call from background threads (workers, scheduler jobs).
    For request handlers use send_async() instead.
    """
    if not _resend_configured():
        logger.warning(
            "RESEND_API_KEY not set — email skipped. "
            "Add it in Railway Variables → RESEND_API_KEY. "
            "Subject: %s → %s", subject, to
        )
        return False

    recipients = [to] if isinstance(to, str) else list(to)
    logger.info("Resend: sending — to=%s subject=%s", recipients, subject[:80])

    try:
        import resend as _resend
        _resend.api_key = os.environ["RESEND_API_KEY"]
        _resend.Emails.send({
            "from": from_address,
            "to": recipients,
            "subject": subject,
            "html": html,
        })
        logger.info("Resend: sent OK — %s → %s", subject[:80], recipients)
        return True
    except KeyError:
        logger.error("Resend: RESEND_API_KEY disappeared mid-request")
        return False
    except Exception as exc:
        logger.error("Resend: send failed (to=%s subject=%s): %s", recipients, subject[:80], exc)
        return False


def send_async(
    to: str | list[str],
    subject: str,
    html: str,
    from_address: str = _FROM_ADDRESS,
) -> None:
    """
    Dispatch send_email() in a daemon thread so it never blocks the caller.
    Use this from Flask request handlers and any synchronous code path.
    """
    t = threading.Thread(
        target=send_email,
        args=(to, subject, html, from_address),
        daemon=True,
        name=f"mailer-{subject[:20]}",
    )
    t.start()


# ── High-level helpers ────────────────────────────────────────────────────────

def send_to_client(
    subject: str,
    html: str,
    client_email: str,
    admin_subject_prefix: str = "[ADMIN COPY]",
    add_footer: bool = True,
    _async: bool = False,
) -> bool:
    """
    Send to a client, then a separate copy to ADMIN_EMAIL.
    Returns True if the client send succeeded (admin copy failure is non-fatal).
    Set _async=True to dispatch both sends in daemon threads (for request handlers).
    """
    if add_footer:
        html = _with_footer(html)

    if _async:
        send_async(client_email, subject, html)
        _send_admin_copy_async(subject, html, client_email, admin_subject_prefix)
        return True  # fire-and-forget, always "succeeds" from caller's perspective

    ok = send_email(client_email, subject, html)
    _send_admin_copy(subject, html, client_email, admin_subject_prefix)
    return ok


def _send_admin_copy(subject: str, html: str, client_email: str, prefix: str) -> None:
    admin = _admin_email()
    if not admin or admin == client_email:
        return
    admin_html = (
        f'<div style="background:#fff3cd;border:1px solid #ffc107;border-radius:6px;'
        f'padding:.6rem 1rem;margin-bottom:1.5rem;font-size:.82rem;color:#856404;">'
        f'<strong>Admin copy</strong> — sent to client: <code>{client_email}</code>'
        f'</div>'
    ) + html
    send_email(admin, f"{prefix} {subject}", admin_html)


def _send_admin_copy_async(subject: str, html: str, client_email: str, prefix: str) -> None:
    admin = _admin_email()
    if not admin or admin == client_email:
        return
    admin_html = (
        f'<div style="background:#fff3cd;border:1px solid #ffc107;border-radius:6px;'
        f'padding:.6rem 1rem;margin-bottom:1.5rem;font-size:.82rem;color:#856404;">'
        f'<strong>Admin copy</strong> — sent to client: <code>{client_email}</code>'
        f'</div>'
    ) + html
    send_async(admin, f"{prefix} {subject}", admin_html)


def send_admin_only(subject: str, html: str, _async: bool = False) -> bool:
    """Send a notification to ADMIN_EMAIL only."""
    admin = _admin_email()
    if not admin:
        logger.warning("ADMIN_EMAIL not set — admin email skipped: %s", subject)
        return False
    if _async:
        send_async(admin, subject, html)
        return True
    return send_email(admin, subject, html)


# ── Email templates ───────────────────────────────────────────────────────────

def send_signup_confirmation(name: str, email: str, plan_label: str) -> bool:
    """
    Confirmation to prospect after signup form submission.
    Always called via send_async in request handler — never blocks.
    """
    first = name.split()[0] if name else "there"
    admin = _admin_email() or "hello@bidedge.co.nz"
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
      We've received your enquiry for the <strong>{plan_label}</strong> plan and someone
      from the BidEdge team will be in touch within one business day.</p>
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
      <a href="mailto:{admin}" style="color:#2a9d8f;">{admin}</a>.<br><br>
      The BidEdge Team
    </div>
  </div>
</div>
"""
    return send_email(email, subject, html)


def send_pursuit_ready(
    client_name: str,
    client_email: str,
    notice_title: str,
    notice_id: str,
    portal_url: str,
) -> bool:
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
      <div style="font-size:.78rem;color:#8899aa;margin-top:.25rem;">GETS ref: {notice_id}</div>
    </div>
    <a href="{portal_url}" style="display:inline-block;background:#2a9d8f;color:#fff;
      font-weight:700;font-size:.9rem;padding:.7rem 1.5rem;border-radius:6px;
      text-decoration:none;">View in Pursuits library &rarr;</a>
    <p style="color:#8899aa;font-size:.82rem;line-height:1.6;margin-top:1.5rem;">
      The package includes your win position assessment, competitive landscape,
      agency history, and recommended actions.</p>
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
    eta = "within the hour" if urgent else "within 24 hours"
    subject = f"Pursuit package request received — {notice_title[:60]}"
    urgent_note = ("<strong>This notice closes soon — your package will be prioritised."
                   "</strong><br><br>" if urgent else "")
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
      {urgent_note}
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
    subject = f"Groundwork Weekly Intelligence Brief — {week_label}"
    full_html = f"""
<div style="font-family:'Inter',system-ui,sans-serif;max-width:700px;margin:0 auto;
  background:#fff;color:#1a2d4a;">
  <div style="background:#1a2d4a;padding:1.5rem 2rem;display:flex;
    justify-content:space-between;align-items:center;">
    <div style="font-size:1rem;font-weight:800;color:#fff;letter-spacing:-.01em;">
      Groundwork <span style="color:#2a9d8f;font-weight:400;">by BidEdge</span></div>
    <div style="font-size:.78rem;color:rgba(255,255,255,.5);">{week_label}</div>
  </div>
  <div style="padding:0;">{brief_html}</div>
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
    subject = f"[Groundwork] New {request_type.title()} Request — {client_name}"
    html = f"""
<p><b>Client:</b> {client_name} (<code>{client_id}</code>)<br>
<b>Type:</b> {request_type}<br>
<b>Notice ID:</b> {notice_id or '—'}<br>
<b>Priority:</b> {priority}</p>
<p><b>Details:</b><br>{details or '—'}</p>
"""
    return send_admin_only(subject, html)


def send_watchlist_ready(
    client_name: str,
    client_email: str,
    notice_count: int,
    portal_url: str,
    date_label: str,
) -> bool:
    """Sent to each active client after Layer 1 completes. Only call when notice_count > 0."""
    subject = f"Your Groundwork watchlist is ready — {date_label}"
    plural = "opportunity has" if notice_count == 1 else "opportunities have"
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
      <strong>{notice_count}</strong> {plural} been scored and ranked
      for your sectors today.</p>
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
