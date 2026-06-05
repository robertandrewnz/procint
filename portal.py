"""
Layer 3 — Client Portal (Flask).

Lightweight password-protected web interface for clients to view their
personalised procurement intelligence: daily watchlist, pursuit packages,
and competitor profiles. Single shared password per deployment.

Run:
  python portal.py
  # or: flask --app portal run --host 0.0.0.0 --port 5000

Environment variables:
  PORTAL_PASSWORD   shared client password
  PORTAL_SECRET_KEY Flask session secret
  PORTAL_HOST       bind host (default 127.0.0.1)
  PORTAL_PORT       port (default 5000)
"""
import json
import logging
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional

from flask import (
    Flask, render_template_string, request, session,
    redirect, url_for, jsonify, send_file, abort
)

import config
import db

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = config.PORTAL_SECRET_KEY


# ── Auth ──────────────────────────────────────────────────────────────────────

def _require_auth():
    if not session.get("authenticated"):
        return redirect(url_for("login"))
    return None


# ── Templates ─────────────────────────────────────────────────────────────────

_BASE_STYLE = """
<style>
:root{--bg:#0d1117;--surface:#161b22;--surf2:#1c2230;--border:#2a3344;
      --text:#e6edf3;--muted:#7d8fa8;--accent:#4f9cf9;
      --green:#22c55e;--amber:#facc15;--red:#ef4444;}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:'Inter',system-ui,sans-serif;
     font-size:14px;line-height:1.6;}
a{color:var(--accent);text-decoration:none;}
a:hover{text-decoration:underline;}
.nav{background:var(--surface);border-bottom:1px solid var(--border);
     padding:.75rem 2rem;display:flex;align-items:center;gap:2rem;}
.nav-brand{font-size:.85rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--accent);}
.nav-link{font-size:.8rem;color:var(--muted);}
.nav-link:hover{color:var(--text);}
.nav-right{margin-left:auto;font-size:.75rem;color:var(--muted);}
.page{max-width:1100px;margin:0 auto;padding:2.5rem 2rem;}
.page-title{font-size:1.25rem;font-weight:800;color:var(--text);margin-bottom:1.5rem;}
.card{background:var(--surface);border:1px solid var(--border);border-radius:8px;
      padding:1.25rem 1.5rem;margin-bottom:1rem;}
.card-title{font-size:.9rem;font-weight:600;color:var(--text);margin-bottom:.3rem;}
.card-meta{font-size:.75rem;color:var(--muted);margin-bottom:.6rem;}
.chip{display:inline-flex;align-items:center;padding:.18rem .5rem;border-radius:999px;
      font-size:.65rem;font-weight:600;border:1px solid;}
.chip-blue{background:#4f9cf922;color:var(--accent);border-color:#4f9cf940;}
.chip-amber{background:#facc1522;color:var(--amber);border-color:#facc1540;}
.chip-red{background:#ef444422;color:var(--red);border-color:#ef444440;}
.chip-green{background:#22c55e22;color:var(--green);border-color:#22c55e40;}
.chip-grey{background:#94a3b822;color:var(--muted);border-color:#94a3b840;}
table{width:100%;border-collapse:collapse;font-size:.82rem;}
th{font-size:.65rem;font-weight:700;letter-spacing:.07em;text-transform:uppercase;
   color:var(--muted);padding:.45rem .7rem;border-bottom:1px solid var(--border);text-align:left;}
td{padding:.5rem .7rem;border-bottom:1px solid var(--border);}
tr:last-child td{border-bottom:none;}
tr:nth-child(even) td{background:var(--surf2);}
.btn{display:inline-flex;align-items:center;gap:.4rem;padding:.5rem 1rem;
     border-radius:6px;font-size:.8rem;font-weight:600;cursor:pointer;border:none;
     text-decoration:none;}
.btn-primary{background:var(--accent);color:#0d1117;}
.btn-primary:hover{background:#6ab0fa;text-decoration:none;}
.btn-outline{background:transparent;color:var(--accent);border:1px solid var(--accent);}
.btn-outline:hover{background:#4f9cf922;text-decoration:none;}
.form-group{margin-bottom:1.25rem;}
label{display:block;font-size:.75rem;font-weight:600;color:var(--muted);margin-bottom:.4rem;}
input[type=text],input[type=password],select{
  width:100%;background:var(--surf2);border:1px solid var(--border);
  border-radius:6px;color:var(--text);font-size:.85rem;padding:.55rem .85rem;}
input:focus,select:focus{outline:none;border-color:var(--accent);}
.alert{padding:.75rem 1rem;border-radius:6px;font-size:.82rem;margin-bottom:1rem;}
.alert-error{background:#ef444418;border:1px solid #ef444440;color:var(--red);}
.alert-success{background:#22c55e18;border:1px solid #22c55e40;color:var(--green);}
</style>
"""

_LOGIN_HTML = _BASE_STYLE + """
<div style="min-height:100vh;display:flex;align-items:center;justify-content:center;">
  <div style="width:360px;">
    <div style="text-align:center;margin-bottom:2rem;">
      <div style="font-size:.75rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
                  color:var(--accent);margin-bottom:.5rem;">Procurement Intelligence</div>
      <div style="font-size:1.2rem;font-weight:800;color:var(--text);">Client Portal</div>
    </div>
    <div class="card">
      {% if error %}<div class="alert alert-error">{{ error }}</div>{% endif %}
      <form method="POST">
        <div class="form-group">
          <label>Password</label>
          <input type="password" name="password" autofocus placeholder="Enter your access password">
        </div>
        <button type="submit" class="btn btn-primary" style="width:100%;">Access Portal</button>
      </form>
    </div>
  </div>
</div>
"""

_DASHBOARD_HTML = _BASE_STYLE + """
<div class="nav">
  <span class="nav-brand">Procurement Intelligence</span>
  <a class="nav-link" href="{{ url_for('dashboard') }}">Watchlist</a>
  <a class="nav-link" href="{{ url_for('packages_page') }}">Pursuit Packages</a>
  <a class="nav-link" href="{{ url_for('competitors_page') }}">Competitors</a>
  <div class="nav-right">
    <a href="{{ url_for('logout') }}" style="color:var(--muted);">Sign out</a>
  </div>
</div>
<div class="page">
  <div class="page-title">Daily Watchlist — {{ run_date }}</div>

  {% if notices %}
  <table>
    <thead><tr>
      <th>#</th><th>Opportunity</th><th>Agency</th>
      <th>Sector</th><th>Score</th><th>Closes</th><th>Actions</th>
    </tr></thead>
    <tbody>
    {% for n in notices %}
    <tr>
      <td style="color:var(--muted);">{{ loop.index }}</td>
      <td>
        <a href="{{ n.source_url }}" target="_blank" style="font-weight:600;">
          {{ n.title[:70] }}{% if n.title|length > 70 %}…{% endif %}
        </a>
        {% if n.summary %}
        <div style="font-size:.75rem;color:var(--muted);margin-top:.2rem;">
          {{ n.summary[:120] }}{% if n.summary|length > 120 %}…{% endif %}
        </div>
        {% endif %}
      </td>
      <td style="font-size:.8rem;color:var(--muted);">{{ n.agency[:40] }}</td>
      <td>
        <span class="chip chip-blue" style="font-size:.62rem;">
          {{ (n.sector_tag or 'other').replace('_',' ').upper() }}
        </span>
      </td>
      <td style="font-weight:700;">{{ "%.1f"|format(n.composite_score|float) }}</td>
      <td>
        {% if n.days_until_close is not none %}
          {% if n.days_until_close <= 7 %}
            <span class="chip chip-red">{{ n.days_until_close }}d</span>
          {% elif n.days_until_close <= 21 %}
            <span class="chip chip-amber">{{ n.days_until_close }}d</span>
          {% else %}
            <span class="chip chip-grey">{{ n.days_until_close }}d</span>
          {% endif %}
        {% else %}<span style="color:var(--muted);">—</span>{% endif %}
      </td>
      <td>
        <form action="{{ url_for('request_package') }}" method="POST" style="display:inline;">
          <input type="hidden" name="notice_id" value="{{ n.notice_id }}">
          <button type="submit" class="btn btn-outline" style="padding:.3rem .7rem;font-size:.72rem;">
            Request Package
          </button>
        </form>
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="card" style="color:var(--muted);font-style:italic;">
    No active notices above the priority threshold. Run the Layer 1 pipeline to ingest fresh notices.
  </div>
  {% endif %}
</div>
"""

_PACKAGES_HTML = _BASE_STYLE + """
<div class="nav">
  <span class="nav-brand">Procurement Intelligence</span>
  <a class="nav-link" href="{{ url_for('dashboard') }}">Watchlist</a>
  <a class="nav-link" href="{{ url_for('packages_page') }}">Pursuit Packages</a>
  <a class="nav-link" href="{{ url_for('competitors_page') }}">Competitors</a>
  <div class="nav-right"><a href="{{ url_for('logout') }}" style="color:var(--muted);">Sign out</a></div>
</div>
<div class="page">
  <div class="page-title">Pursuit Packages</div>
  {% if packages %}
    {% for p in packages %}
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <div>
          <div class="card-title">{{ p.name }}</div>
          <div class="card-meta">{{ p.date }}</div>
        </div>
        <a href="{{ url_for('serve_artefact', filepath=p.rel_path) }}" class="btn btn-outline">
          Open &#8599;
        </a>
      </div>
    </div>
    {% endfor %}
  {% else %}
  <div class="card" style="color:var(--muted);font-style:italic;">
    No pursuit packages generated yet. Request one from the Watchlist tab.
  </div>
  {% endif %}
</div>
"""

_COMPETITORS_HTML = _BASE_STYLE + """
<div class="nav">
  <span class="nav-brand">Procurement Intelligence</span>
  <a class="nav-link" href="{{ url_for('dashboard') }}">Watchlist</a>
  <a class="nav-link" href="{{ url_for('packages_page') }}">Pursuit Packages</a>
  <a class="nav-link" href="{{ url_for('competitors_page') }}">Competitors</a>
  <div class="nav-right"><a href="{{ url_for('logout') }}" style="color:var(--muted);">Sign out</a></div>
</div>
<div class="page">
  <div class="page-title">Competitor Profiles</div>
  <div class="card" style="margin-bottom:1.5rem;">
    <form action="{{ url_for('generate_competitor') }}" method="POST"
          style="display:flex;gap:1rem;align-items:flex-end;">
      <div class="form-group" style="margin-bottom:0;flex:1;">
        <label>Competitor Name</label>
        <input type="text" name="competitor_name" placeholder="e.g. Fulton Hogan">
      </div>
      <div class="form-group" style="margin-bottom:0;flex:1;">
        <label>Your Company (for head-to-head)</label>
        <input type="text" name="client_name" placeholder="optional">
      </div>
      <button type="submit" class="btn btn-primary">Generate Profile</button>
    </form>
  </div>
  {% if message %}<div class="alert alert-success">{{ message }}</div>{% endif %}
  {% if profiles %}
    {% for p in profiles %}
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <div>
          <div class="card-title">{{ p.name }}</div>
          <div class="card-meta">{{ p.date }}</div>
        </div>
        <a href="{{ url_for('serve_artefact', filepath=p.rel_path) }}" class="btn btn-outline">
          Open &#8599;
        </a>
      </div>
    </div>
    {% endfor %}
  {% endif %}
</div>
"""


# ── Data helpers ──────────────────────────────────────────────────────────────

def _get_watchlist() -> list[dict]:
    return db.fetchall(
        """
        SELECT r.notice_id, r.title, r.agency, r.source_url, r.close_date,
               p.sector_tag, p.days_until_close, s.composite_score,
               e.summary
          FROM scored_notices s
          JOIN raw_notices r ON r.notice_id = s.notice_id
          JOIN parsed_notices p ON p.notice_id = s.notice_id
          LEFT JOIN enriched_notices e ON e.notice_id = s.notice_id
         WHERE s.composite_score >= %s
         ORDER BY s.composite_score DESC
         LIMIT 30
        """,
        (config.PRIORITY_THRESHOLD,),
    )


def _list_artefacts(pattern: str) -> list[dict]:
    """List artefact files matching pattern under ARTEFACTS_DIR."""
    artefacts_path = Path(config.ARTEFACTS_DIR)
    results = []
    if artefacts_path.exists():
        for f in sorted(artefacts_path.rglob(pattern), reverse=True)[:20]:
            rel = f.relative_to(artefacts_path)
            results.append({
                "name": f.stem.replace("_", " ").title(),
                "date": f.parent.name,
                "rel_path": str(rel),
            })
    return results


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
def login():
    if session.get("authenticated"):
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        if request.form.get("password") == config.PORTAL_PASSWORD:
            session["authenticated"] = True
            return redirect(url_for("dashboard"))
        error = "Incorrect password."
    return render_template_string(_LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/watchlist")
def dashboard():
    auth = _require_auth()
    if auth:
        return auth
    notices = _get_watchlist()
    return render_template_string(
        _DASHBOARD_HTML,
        notices=notices,
        run_date=date.today().isoformat(),
    )


@app.route("/packages")
def packages_page():
    auth = _require_auth()
    if auth:
        return auth
    packages = _list_artefacts("*pursuit_package*.html")
    return render_template_string(_PACKAGES_HTML, packages=packages)


@app.route("/request-package", methods=["POST"])
def request_package():
    auth = _require_auth()
    if auth:
        return auth
    notice_id = request.form.get("notice_id", "")
    if not notice_id:
        abort(400)
    # Generate in background — redirect to packages page with status
    try:
        from pursuit_package import generate_pursuit_package
        generate_pursuit_package(notice_id, client_name="Portal Client")
        msg = f"Pursuit package generated for notice {notice_id}"
    except Exception as exc:
        logger.error("Package generation failed: %s", exc)
        msg = f"Generation failed: {exc}"
    return redirect(url_for("packages_page") + f"?msg={msg}")


@app.route("/competitors")
def competitors_page():
    auth = _require_auth()
    if auth:
        return auth
    profiles = _list_artefacts("competitor_*.html")
    message = request.args.get("msg")
    return render_template_string(_COMPETITORS_HTML, profiles=profiles, message=message)


@app.route("/generate-competitor", methods=["POST"])
def generate_competitor():
    auth = _require_auth()
    if auth:
        return auth
    comp_name = request.form.get("competitor_name", "").strip()
    client_name = request.form.get("client_name", "").strip() or None
    if not comp_name:
        return redirect(url_for("competitors_page"))
    try:
        from competitor_profile import generate_competitor_profile
        generate_competitor_profile(comp_name, client_name=client_name)
        msg = f"Profile generated for {comp_name}"
    except Exception as exc:
        logger.error("Competitor profile failed: %s", exc)
        msg = f"Generation failed: {exc}"
    return redirect(url_for("competitors_page") + f"?msg={msg}")


@app.route("/artefacts/<path:filepath>")
def serve_artefact(filepath: str):
    auth = _require_auth()
    if auth:
        return auth
    full_path = Path(config.ARTEFACTS_DIR) / filepath
    if not full_path.exists() or not full_path.is_file():
        abort(404)
    # Security: ensure path stays within ARTEFACTS_DIR
    try:
        full_path.resolve().relative_to(Path(config.ARTEFACTS_DIR).resolve())
    except ValueError:
        abort(403)
    return send_file(str(full_path))


@app.route("/api/watchlist")
def api_watchlist():
    """JSON endpoint for programmatic access."""
    auth = _require_auth()
    if auth:
        return jsonify({"error": "Unauthorized"}), 401

    def _serialise(obj):
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        return str(obj)

    notices = _get_watchlist()
    return app.response_class(
        json.dumps([dict(n) for n in notices], default=_serialise),
        mimetype="application/json",
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )
    logger.info("Starting portal on %s:%s", config.PORTAL_HOST, config.PORTAL_PORT)
    app.run(host=config.PORTAL_HOST, port=config.PORTAL_PORT, debug=False)
