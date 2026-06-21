"""
BidEdge — Client Portal

Routes:
  Public:  /  /groundwork  /terrain  /keystone  /pricing
           /login  /logout  /share/<token>  /request-access
  Client:  /groundwork/home  /groundwork/watchlist  /groundwork/pursuits
           /groundwork/competitors  /groundwork/briefs  /groundwork/request
           /groundwork/share  /groundwork/files/<slug>/<path>
           /groundwork/output/<path>
  Admin:   /admin  /admin/clients/<user>  /admin/generate  /admin/add-client

Setup:
  python portal.py --create-user alice --password secret --name "Alice Corp"
  python portal.py --list-users
  python portal.py                    # start server
"""
from __future__ import annotations
import argparse, json, logging, os, secrets, sys
from datetime import date, datetime, timedelta
from html import escape as _safe
from decimal import Decimal
from functools import wraps
from pathlib import Path
from typing import Optional

import bcrypt
from flask import (Flask, abort, flash, g, redirect, request, send_file,
                   session, url_for)
from flask_login import (LoginManager, UserMixin, current_user,
                         login_required, login_user, logout_user)

import config
import db

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s")
logger = logging.getLogger("portal")

app = Flask(__name__)
app.secret_key = config.PORTAL_SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = ""

# ── Background scheduler (Railway) ────────────────────────────────────────────
# Starts APScheduler in a daemon thread when running on Railway.
# Ensure all application DB tables exist. Idempotent — safe to run every startup.
try:
    db.ensure_tables()
except Exception as _db_err:
    logging.getLogger("portal").warning("db.ensure_tables() raised: %s", _db_err)

# Gunicorn single-instance lock prevents duplicate schedulers across workers.
# Set DISABLE_SCHEDULER=1 to suppress (local dev, maintenance windows).
try:
    from scheduler_railway import start_scheduler
    start_scheduler()
except Exception as _sched_err:
    logging.getLogger("portal").warning("Scheduler failed to start: %s", _sched_err)


def _bootstrap_demos() -> None:
    """
    Generate demo artefacts on startup if fewer than 7 are in the DB.
    Gunicorn flock ensures only one worker runs this. Uses force=True so
    an empty manifest from a previous failed run never blocks regeneration.
    """
    import os as _os, fcntl as _fcntl
    if _os.getenv("DISABLE_DEMO_BOOTSTRAP", "").strip() == "1":
        return
    _log = logging.getLogger("portal.demo_bootstrap")

    # Single-worker lock — same pattern as the scheduler
    _lock_path = "/tmp/groundwork_demo_bootstrap.lock"
    try:
        _lf = open(_lock_path, "w")
        _fcntl.flock(_lf, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except OSError:
        _log.debug("Demo bootstrap lock held by another worker — skipping")
        return

    # Check actual artefact count, not just manifest presence
    try:
        row = db.fetchone(
            "SELECT COUNT(*) AS cnt FROM pipeline_outputs WHERE output_type = 'demo_html'"
        )
        cnt = int((row or {}).get("cnt") or 0)
    except Exception as _e:
        _log.warning("Demo bootstrap DB check failed: %s — will attempt generation anyway", _e)
        cnt = 0

    if cnt >= 7:
        _log.info("Demo artefacts in DB (%d) — skipping bootstrap", cnt)
        return

    _log.info("Demo artefacts in DB: %d — starting background generation (need ≥7)", cnt)

    import threading as _thr

    def _run():
        try:
            from generate_demo_content import main as _gen_demo
            stats = _gen_demo(force=True)
            total = stats.get("total", 0)
            _log.info(
                "Bootstrap complete: %d artefacts across %d sectors — %s",
                total, stats.get("sectors", 0),
                " | ".join(f"{k}:{v}" for k, v in stats.get("by_sector", {}).items()),
            )
            if total == 0:
                _log.error("Bootstrap produced 0 artefacts — check Railway logs above for per-sector tracebacks")
        except Exception as _exc:
            _log.exception("Bootstrap demo generation failed: %s", _exc)

    t = _thr.Thread(target=_run, daemon=True, name="demo-bootstrap")
    t.start()


_bootstrap_demos()


CONFIG_FILE = Path("portal_config.json")
TOKENS_FILE = Path("data/share_tokens.json")
ARTEFACTS   = Path(config.ARTEFACTS_DIR)
OUTPUT_DIR  = Path(config.OUTPUT_DIR)
TOKEN_TTL_H = 24

config.ensure_output_dirs()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _nzt_today() -> str:
    """Return today's date in NZ time (Pacific/Auckland) as an ISO string."""
    try:
        import pytz as _pytz
        from datetime import datetime as _dt
        return _dt.now(_pytz.timezone('Pacific/Auckland')).date().isoformat()
    except ImportError:
        return date.today().isoformat()


def _slug(s: str) -> str:
    import re
    return re.sub(r"[^\w]", "_", s.lower())[:40]

def _load_cfg() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {"clients": {}, "settings": {}}

def _save_cfg(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

def _fmt_value(v) -> str:
    if v is None: return "—"
    v = float(v)
    if v >= 1_000_000: return f"${v/1_000_000:.1f}M"
    if v >= 1_000:     return f"${v/1_000:.0f}K"
    return f"${v:,.0f}"


# ── User model ────────────────────────────────────────────────────────────────

class User(UserMixin):
    def __init__(self, username: str, data: dict):
        self.id            = username
        self.username      = username
        self.name          = data.get("display_name", username)
        self.email         = data.get("email", "")
        self.phone         = data.get("phone", "")
        self.organisation  = data.get("organisation", "")
        self.is_admin_user    = data.get("is_admin", False)
        self.temp_password = data.get("temp_password", False)
        # Support both old key "sectors" and new key "preferred_sectors"
        self.preferred_sectors = (
            data.get("preferred_sectors")
            or data.get("sectors")
            or []
        )
        self.slug           = data.get("artefact_slug") or _slug(data.get("display_name", username))
        self.plan           = data.get("plan", "pursue")        # watch | pursue | edge
        self.billing_status = data.get("billing_status", "active")  # trial | active | suspended
        self.email_watchlist = data.get("email_watchlist", True)   # daily watchlist emails
        self.email_briefs    = data.get("email_briefs", True)       # weekly brief emails

    def can(self, feature: str) -> bool:
        """Plan-based feature gate. Returns True if user's plan includes this feature."""
        if self.is_admin_user:
            return True
        PLAN_FEATURES = {
            "watch":   {"watchlist", "signals", "briefs"},
            "pursue":  {"watchlist", "signals", "briefs", "pursuits", "competitors", "radar"},
            "edge":    {"watchlist", "signals", "briefs", "pursuits", "competitors", "radar",
                        "priority", "notes"},
        }
        return feature in PLAN_FEATURES.get(self.plan, PLAN_FEATURES["pursue"])

@login_manager.user_loader
def load_user(username: str) -> Optional[User]:
    data = _load_cfg().get("clients", {}).get(username)
    return User(username, data) if data else None

def _check_password(username: str, password: str) -> Optional[User]:
    data = _load_cfg().get("clients", {}).get(username)
    if not data: return None
    stored = data.get("password_hash", "").encode()
    if not stored: return None
    try:
        if bcrypt.checkpw(password.encode(), stored):
            return User(username, data)
    except Exception:
        pass
    return None

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin_user:
            abort(403)
        return f(*args, **kwargs)
    return decorated


@app.before_request
def _force_password_change():
    """Redirect temp-password users to the change-password page on every request."""
    _EXEMPT = {"login", "logout", "account_change_password", "static",
               "share_view", "serve_artefact_file"}
    if (current_user.is_authenticated
            and getattr(current_user, "temp_password", False)
            and request.endpoint not in _EXEMPT):
        return redirect(url_for("account_change_password"))


# ── Share tokens ──────────────────────────────────────────────────────────────

def _load_tokens() -> dict:
    TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if TOKENS_FILE.exists():
        try: return json.loads(TOKENS_FILE.read_text())
        except Exception: pass
    return {}

def _save_tokens(t: dict) -> None:
    TOKENS_FILE.write_text(json.dumps(t, indent=2))

def _create_share_token(filepath: str, label: str, created_by: str) -> str:
    token   = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(hours=TOKEN_TTL_H)).isoformat()
    tokens  = _load_tokens()
    now     = datetime.utcnow().isoformat()
    tokens  = {k: v for k, v in tokens.items() if v.get("expires_at", "") > now}
    tokens[token] = {"filepath": filepath, "label": label,
                     "created_by": created_by, "expires_at": expires}
    _save_tokens(tokens)
    return token

def _resolve_token(token: str) -> Optional[dict]:
    entry = _load_tokens().get(token)
    if not entry: return None
    if entry.get("expires_at", "") < datetime.utcnow().isoformat(): return None
    return entry


# ── Email ─────────────────────────────────────────────────────────────────────
# All email goes through mailer.py (Resend API). Never use smtplib directly.

import mailer as _mailer_mod  # noqa: E402 — imported after Flask setup

def _send_email(subject: str, html: str, to: list[str]) -> bool:
    """Legacy shim — delegates to mailer.send_email(). Kept for old call sites."""
    ok = True
    for addr in to:
        ok = _mailer_mod.send_email(addr, subject, html) and ok
    return ok

def _admin_emails() -> list[str]:
    a = _load_cfg().get("settings", {}).get("admin_email") or os.getenv("ADMIN_EMAIL", "")
    return [a] if a else []


# ── File helpers ──────────────────────────────────────────────────────────────

def _list_artefacts(
    slug: str,
    pattern: str = "*.html",
    db_output_types: list = None,
) -> list[dict]:
    import fnmatch
    base = ARTEFACTS / slug
    files = []
    if base.exists():
        for f in sorted(base.rglob(pattern), reverse=True)[:60]:
            rel_full = f.relative_to(ARTEFACTS)
            rel_url  = f.relative_to(ARTEFACTS / slug)
            files.append({"name": f.stem.replace("_", " ").title(),
                          "rel_path": str(rel_full),
                          "url_path": str(rel_url),
                          "date": f.parent.name,
                          "size_kb": f.stat().st_size // 1024,
                          "has_pdf": f.with_suffix(".pdf").exists()})

    if not files and db_output_types:
        # Filesystem miss after Railway redeploy — restore from pipeline_outputs DB
        try:
            rows = db.fetchall(
                """
                SELECT id, filename, content, run_date
                  FROM pipeline_outputs
                 WHERE output_type = ANY(%s)
                   AND client_slug = %s
                   AND content IS NOT NULL
                 ORDER BY run_date DESC, created_at DESC
                 LIMIT 60
                """,
                (db_output_types, slug),
            )
            for row in rows:
                filename = row["filename"]
                if not fnmatch.fnmatch(filename, pattern):
                    continue
                run_date = str(row["run_date"])
                target_dir = ARTEFACTS / slug / run_date
                target_dir.mkdir(parents=True, exist_ok=True)
                target_path = target_dir / filename
                if not target_path.exists():
                    target_path.write_text(row["content"], encoding="utf-8")
                    logger.info("_list_artefacts: restored %s from DB", target_path)
                rel_full = target_path.relative_to(ARTEFACTS)
                rel_url  = target_path.relative_to(ARTEFACTS / slug)
                files.append({
                    "name": Path(filename).stem.replace("_", " ").title(),
                    "rel_path": str(rel_full),
                    "url_path": str(rel_url),
                    "date": run_date,
                    "size_kb": len(row["content"].encode("utf-8")) // 1024,
                    "has_pdf": False,
                    "db_id": row.get("id"),
                    "filename": filename,
                })
        except Exception as exc:
            logger.warning("_list_artefacts DB fallback failed: %s", exc)

    return files

def _latest_watchlist() -> Optional[Path]:
    """
    Return the path to the most recent watchlist HTML.

    Checks the filesystem first (fast). Falls back to pipeline_outputs DB
    table and restores the file to disk — needed after Railway redeploys
    because the ephemeral filesystem loses /app/output on each deploy.
    """
    candidates = sorted(OUTPUT_DIR.glob("watchlist_*.html"), reverse=True)
    if candidates:
        logger.info("_latest_watchlist: serving from disk — %s", candidates[0])
        return candidates[0]

    # Filesystem miss — try DB
    try:
        row = db.fetchone(
            """
            SELECT filename, content, run_date
            FROM   pipeline_outputs
            WHERE  output_type = 'watchlist_html'
              AND  content IS NOT NULL
            ORDER  BY run_date DESC, created_at DESC
            LIMIT  1
            """
        )
        if row and row.get("content"):
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            path = OUTPUT_DIR / row["filename"]
            path.write_text(row["content"], encoding="utf-8")
            logger.info("_latest_watchlist: restored from DB → %s (run_date %s)", path, row["run_date"])
            return path
    except Exception as exc:
        logger.warning("_latest_watchlist: DB fallback failed — %s", exc)

    logger.info("_latest_watchlist: no watchlist found on disk or in DB")
    return None

def _watchlist_summary(
    preferred_sectors: Optional[list[str]] = None,
    user_id: Optional[str] = None,
) -> dict:
    """
    Return top notices re-ranked by client sector preference.

    Scoring:
    - preferred sector  → composite × 1.4 (capped at 10)
    - non-preferred     → composite × 0.7
    - no preference     → composite unchanged (neutral)

    If *user_id* is provided, the DB row is the authoritative source.
    The JSON-file sectors (passed as preferred_sectors) are only used as a
    fallback when no DB row exists for that user.
    """
    try:
        # DB is source of truth for sector preferences; JSON sectors are fallback only
        min_value_nzd = 0
        if user_id:
            try:
                from preferences import get_user_preferences
                db_prefs = get_user_preferences(user_id)
                db_sectors = db_prefs.get("sectors") or []
                if db_sectors:
                    preferred_sectors = db_sectors
                min_value_nzd = int(db_prefs.get("min_value_nzd") or 0)
            except Exception:
                pass

        # Fetch active (not yet closed) parsed notices ordered by urgency
        pool = db.fetchall("""
            SELECT r.notice_id, r.title, r.agency, r.close_date,
                   p.sector_tag, p.days_until_close, p.value_band
              FROM parsed_notices p
              JOIN raw_notices r ON r.notice_id = p.notice_id
             WHERE (p.days_until_close IS NULL OR p.days_until_close >= 0)
             ORDER BY p.days_until_close ASC NULLS LAST
             LIMIT 100
        """)

        pool = [dict(r) for r in pool]

        # Minimum value filter (TBC/unknown bands always pass)
        if min_value_nzd and min_value_nzd > 0:
            _BAND_MIN = {"under_100k": 0, "100k_500k": 100_000,
                         "500k_2m": 500_000, "2m_10m": 2_000_000, "10m_plus": 10_000_000}
            pool = [r for r in pool
                    if _BAND_MIN.get(r.get("value_band") or "unknown", min_value_nzd) >= min_value_nzd
                    or (r.get("value_band") or "unknown") not in _BAND_MIN]

        # If sectors preferred, surface matching notices first, then sort by urgency+value
        if preferred_sectors:
            matched   = [r for r in pool if (r.get("sector_tag") or "other") in preferred_sectors]
            unmatched = [r for r in pool if (r.get("sector_tag") or "other") not in preferred_sectors]
            matched.sort(key=_notice_sort_key)
            unmatched.sort(key=_notice_sort_key)
            pool = matched + unmatched
        else:
            pool.sort(key=_notice_sort_key)

        notices = pool[:5]

        flags = db.fetchall("""
            SELECT flag_type, description, severity FROM pattern_flags
             WHERE (expires_at IS NULL OR expires_at >= CURRENT_DATE)
             ORDER BY CASE severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END
             LIMIT 4
        """)
        total = db.fetchone("SELECT COUNT(*) as n FROM parsed_notices")
        return {"top_notices": notices,
                "flags": [dict(f) for f in flags],
                "total": total["n"] if total else 0,
                "run_date": _nzt_today(),
                "preferred_sectors": preferred_sectors or []}
    except Exception as exc:
        logger.error("watchlist_summary: %s", exc)
        return {"top_notices": [], "flags": [], "total": 0, "run_date": "",
                "preferred_sectors": []}


# ── CSS & layout ──────────────────────────────────────────────────────────────

CSS = """
<style>
:root{--bg:#1E2D40;--surf:#253345;--surf2:#2a3d54;--border:#253d5c;
      --text:#EDE8E3;--muted:#8fa3bc;--navy:#1a2d4a;--gold:#2a9d8f;
      --gold-l:rgba(42,157,143,.12);--red:#e05555;--green:#4caf7d;
      --card:#253345;--card-border:#253d5c;--card-hover:#2a3d54;
      --font:'Inter',system-ui,-apple-system,sans-serif;}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
html{scroll-behavior:smooth;}
body{background:var(--bg);color:var(--text);font-family:var(--font);
     font-size:14px;line-height:1.65;min-height:100vh;
     -webkit-font-smoothing:antialiased;}
a{color:var(--gold);text-decoration:none;}
a:hover{color:#e0c070;}
h1,h2,h3{line-height:1.3;color:var(--text);}
/* Topnav */
.nav{background:var(--surf);border-bottom:1px solid var(--border);
     padding:.85rem 2rem;display:flex;align-items:center;gap:1.5rem;
     position:sticky;top:0;z-index:100;}
.nav-brand{display:flex;flex-direction:column;margin-right:auto;}
.nav-brand-name{font-size:.92rem;font-weight:800;color:#fff;}
.nav-brand-name .by{font-weight:400;color:var(--muted);}
.nav-brand-sub{font-size:.58rem;font-weight:700;letter-spacing:.1em;
               text-transform:uppercase;color:var(--gold);}
.nav-link{font-size:.82rem;color:var(--muted);padding:.3rem .5rem;border-radius:4px;}
.nav-link:hover,.nav-link.active{color:#fff;}
.nav-link.active{background:var(--surf2);}
.nav-user{font-size:.78rem;color:var(--muted);display:flex;align-items:center;gap:.75rem;}
.nav-user strong{color:#fff;}
/* Shell */
.shell{display:flex;min-height:calc(100vh - 52px);}
.side{width:210px;flex-shrink:0;background:var(--surf);
      border-right:1px solid var(--border);padding:1.5rem 1rem;}
.side-sec{font-size:.6rem;font-weight:700;letter-spacing:.09em;text-transform:uppercase;
          color:var(--muted);margin:1.25rem 0 .5rem;padding-left:.5rem;}
.side a{display:flex;align-items:center;gap:.6rem;padding:.42rem .65rem;
        border-radius:5px;font-size:.83rem;color:var(--muted);margin-bottom:.15rem;transition:.12s;}
.side a:hover{background:var(--surf2);color:#fff;}
.side a.on{background:var(--gold-l);color:var(--gold);border:1px solid rgba(42,157,143,.25);}
/* Content */
.main{flex:1;padding:2.5rem 2.5rem;overflow-x:hidden;}
.ptitle{font-size:1.3rem;font-weight:800;color:var(--text);margin-bottom:.35rem;}
.psub{font-size:.85rem;color:var(--muted);margin-bottom:2rem;}
/* Cards */
.card{background:var(--card);border:1px solid var(--card-border);border-radius:8px;
      overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.07);margin-bottom:1.25rem;}
.ch{background:var(--surf2);border-bottom:1px solid var(--card-border);
    padding:.85rem 1.25rem;display:flex;align-items:center;justify-content:space-between;}
.ct{font-size:.88rem;font-weight:700;}
.cb{padding:1.25rem;}
/* Notice rows */
.nr{display:flex;align-items:flex-start;gap:1rem;padding:1rem 1.25rem;
    border-bottom:1px solid var(--card-border);transition:.1s;}
.nr:last-child{border-bottom:none;}
.nr:hover{background:var(--card-hover);}
.nrank{flex-shrink:0;width:1.75rem;height:1.75rem;border-radius:50%;
       background:var(--gold-l);color:var(--gold);font-size:.72rem;font-weight:700;
       display:flex;align-items:center;justify-content:center;
       border:1px solid rgba(42,157,143,.3);}
.nmain{flex:1;min-width:0;}
.ntitle{font-size:.88rem;font-weight:600;margin-bottom:.22rem;}
.nagency{font-size:.75rem;color:var(--muted);}
.nmeta{display:flex;gap:.4rem;margin-top:.35rem;flex-wrap:wrap;}
/* Badges */
.badge{display:inline-flex;align-items:center;padding:.18rem .55rem;border-radius:999px;
       font-size:.65rem;font-weight:600;border:1px solid;white-space:nowrap;}
.bg{background:rgba(42,157,143,.12);color:var(--gold);border-color:rgba(42,157,143,.35);}
.bn{background:rgba(26,45,74,.07);color:#4a6080;border-color:#c8d4e0;}
.br{background:rgba(224,85,85,.12);color:var(--red);border-color:rgba(224,85,85,.3);}
.bk{background:rgba(143,163,188,.1);color:var(--muted);border-color:var(--border);}
/* Flag rows */
.fr{display:flex;gap:.75rem;align-items:flex-start;padding:.65rem 1rem;
    border-radius:6px;background:var(--surf2);margin-bottom:.5rem;
    border:1px solid var(--card-border);font-size:.82rem;min-width:0;}
.fr>span:last-child{min-width:0;word-wrap:break-word;overflow-wrap:break-word;}
.fs{flex-shrink:0;font-size:.62rem;font-weight:700;padding:.15rem .4rem;
    border-radius:3px;text-transform:uppercase;}
.fsh{background:rgba(224,85,85,.2);color:var(--red);}
.fsm{background:rgba(42,157,143,.2);color:var(--gold);}
.fsl{background:rgba(143,163,188,.1);color:var(--muted);}
/* Stats */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
       gap:1rem;margin-bottom:2rem;}
.stat{background:var(--card);border:1px solid var(--card-border);border-radius:8px;padding:1.1rem 1.25rem;}
.sval{font-size:1.6rem;font-weight:800;color:var(--gold);line-height:1;}
.slbl{font-size:.7rem;font-weight:600;letter-spacing:.07em;text-transform:uppercase;
      color:var(--muted);margin-top:.3rem;}
/* File grid */
.fgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:1rem;}
.fc{background:var(--card);border:1px solid var(--card-border);border-radius:8px;
    padding:1rem 1.25rem;transition:border-color .15s;}
.fc:hover{border-color:var(--gold);}
.fct{font-size:.88rem;font-weight:600;margin-bottom:.18rem;}
.fcd{font-size:.72rem;color:var(--muted);margin-bottom:.75rem;}
.fca{display:flex;gap:.45rem;flex-wrap:wrap;}
/* Buttons */
.btn{display:inline-flex;align-items:center;gap:.4rem;padding:.45rem .95rem;
     border-radius:5px;font-size:.8rem;font-weight:600;cursor:pointer;
     border:1.5px solid;transition:all .15s;text-decoration:none;}
.btn:hover{text-decoration:none;}
.bg-gold{background:var(--gold);color:#fff;border-color:var(--gold);}
.bg-gold:hover{background:#238f82;border-color:#238f82;color:#fff;}
.bg-gold:hover{background:#e0c070;border-color:#e0c070;color:#0f1923;}
.bg-out{background:transparent;color:var(--text);border-color:var(--card-border);}
.bg-out:hover{border-color:var(--gold);color:var(--gold);}
.bg-ghost{background:transparent;color:var(--muted);border-color:transparent;font-size:.75rem;padding:.3rem .65rem;}
.bg-ghost:hover{color:var(--text);background:var(--surf2);}
.sm{padding:.3rem .65rem;font-size:.74rem;}
/* Forms */
.fg{margin-bottom:1.25rem;}
.fl{display:block;font-size:.75rem;font-weight:600;color:var(--muted);margin-bottom:.4rem;}
.fc2{width:100%;background:var(--surf2);border:1.5px solid var(--border);border-radius:6px;
     color:var(--text);font-size:.87rem;padding:.55rem .9rem;transition:border-color .15s;
     font-family:var(--font);}
.fc2:focus{outline:none;border-color:var(--gold);}
.fc2::placeholder{color:var(--muted);}
textarea.fc2{min-height:90px;resize:vertical;}
.fh{font-size:.72rem;color:var(--muted);margin-top:.3rem;}
/* Alerts */
.al{padding:.8rem 1rem;border-radius:6px;font-size:.83rem;margin-bottom:1rem;border:1px solid;}
.al-ok{background:rgba(76,175,125,.1);border-color:rgba(76,175,125,.3);color:var(--green);}
.al-er{background:rgba(224,85,85,.1);border-color:rgba(224,85,85,.3);color:var(--red);}
.al-in{background:var(--gold-l);border-color:rgba(42,157,143,.3);color:var(--gold);}
/* Tables */
.dt{width:100%;border-collapse:collapse;font-size:.84rem;}
.dt thead tr{background:var(--surf2);}
.dt th{padding:.6rem .85rem;text-align:left;font-size:.66rem;font-weight:600;
       letter-spacing:.07em;text-transform:uppercase;color:var(--muted);
       border-bottom:1px solid var(--card-border);}
.dt td{padding:.6rem .85rem;border-bottom:1px solid var(--card-border);}
.dt tbody tr:last-child td{border-bottom:none;}
.dt tbody tr:hover td{background:var(--card-hover);}
/* Share modal */
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.65);
          display:flex;align-items:center;justify-content:center;z-index:999;}
.modal{background:var(--card);border:1px solid var(--card-border);border-radius:10px;
       padding:2rem;width:480px;max-width:90vw;}
.modal-t{font-size:1rem;font-weight:700;margin-bottom:1rem;}
.cp{display:flex;gap:.5rem;}
.cp input{flex:1;}
/* Homepage */
.pub-nav{background:var(--surf);border-bottom:1px solid var(--border);
         padding:1rem 2.5rem;display:flex;align-items:center;gap:2rem;}
.pub-brand{font-size:1rem;font-weight:800;color:#fff;}
.pub-brand span{color:var(--gold);}
.hero{padding:7rem 2.5rem 5rem;max-width:720px;margin:0 auto;text-align:center;}
.hero h1{font-size:3rem;font-weight:900;line-height:1.15;color:#fff;
         margin-bottom:1.25rem;letter-spacing:-.03em;}
.hero h1 span{color:var(--gold);}
.hero-sub{font-size:1.05rem;color:var(--muted);line-height:1.7;
          max-width:560px;margin:0 auto 2.5rem;}
.tiers{display:grid;grid-template-columns:repeat(3,1fr);gap:1.25rem;
       max-width:960px;margin:4rem auto 5rem;padding:0 2.5rem;}
.tier{background:var(--surf);border:1px solid var(--border);border-radius:10px;padding:2rem 1.5rem;}
.tier.ft{border-color:var(--gold);}
.tier-lbl{font-size:.68rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
          color:var(--gold);margin-bottom:.5rem;}
.tier-name{font-size:1.2rem;font-weight:800;color:#fff;margin-bottom:.85rem;}
.tier-desc{font-size:.84rem;color:var(--muted);line-height:1.65;}
.tier ul{list-style:none;margin-top:1.1rem;}
.tier li{font-size:.81rem;color:var(--muted);padding:.28rem 0;
         border-top:1px solid var(--border);display:flex;gap:.5rem;}
.tier li::before{content:"✓";color:var(--gold);flex-shrink:0;}
.pub-footer{text-align:center;padding:2rem;border-top:1px solid var(--border);
            font-size:.78rem;color:var(--muted);}
/* Login */
.lw{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:2rem;}
.lb{width:400px;}
.ll{text-align:center;margin-bottom:2.5rem;}
.ll-name{font-size:1.25rem;font-weight:800;color:#fff;}
.ll-sub{font-size:.68rem;font-weight:700;letter-spacing:.1em;
        text-transform:uppercase;color:var(--gold);margin-top:.2rem;}
.lcard{background:var(--surf);border:1px solid var(--border);border-radius:10px;padding:2rem;}
/* ── Hamburger toggle (pure CSS, no JS) ── */
#nav-toggle{display:none;}
.hamburger{display:none;flex-direction:column;justify-content:center;gap:5px;
           width:44px;height:44px;cursor:pointer;flex-shrink:0;
           background:none;border:none;padding:10px;border-radius:6px;}
.hamburger span{display:block;height:2px;background:#fff;
                border-radius:2px;transition:all .2s;}
.hamburger:hover span{background:var(--gold);}
.side-overlay{display:none;}

/* ── Tablet ≤768px ── */
@media(max-width:768px){
  /* Nav */
  .nav{padding:.65rem 1rem;gap:.75rem;}
  .hamburger{display:flex;}
  /* Sidebar: slide-in drawer */
  .shell{flex-direction:column;}
  .side{position:fixed;top:0;left:-240px;width:240px;height:100vh;
        z-index:200;transition:left .22s ease;overflow-y:auto;
        border-right:1px solid var(--border);}
  #nav-toggle:checked ~ .shell .side{left:0;}
  /* Overlay behind drawer */
  .side-overlay{display:block;position:fixed;inset:0;
                background:rgba(0,0,0,.45);z-index:199;
                opacity:0;pointer-events:none;transition:opacity .22s;}
  #nav-toggle:checked ~ .shell .side-overlay{opacity:1;pointer-events:auto;}
  /* Main content full width */
  .main{padding:1.25rem 1rem;width:100%;}
  /* Homepage */
  .tiers{grid-template-columns:1fr;padding:0 1rem;margin:2rem auto 3rem;}
  .hero{padding:3.5rem 1.25rem 2.5rem;}
  .hero h1{font-size:2rem;}
  .hero-sub{font-size:.92rem;}
  .pub-nav{padding:.85rem 1.25rem;gap:1rem;}
  /* Dashboard */
  .stats{grid-template-columns:1fr 1fr;}
  .fgrid{grid-template-columns:1fr;}
  /* Tables */
  .dt{display:block;overflow-x:auto;-webkit-overflow-scrolling:touch;}
  /* Notice rows */
  .nmeta{flex-wrap:wrap;}
  /* Grid that holds top notices + signals (1.6fr 1fr on desktop) */
  div[style*="grid-template-columns:1.6fr"]{display:block!important;}
}

/* ── Phone ≤480px ── */
@media(max-width:480px){
  .nav{padding:.55rem .75rem;}
  .nav-brand-name{font-size:.82rem;}
  .main{padding:1rem .75rem;}
  /* Stat cards: 2-up */
  .stats{grid-template-columns:1fr 1fr;gap:.6rem;}
  .sval{font-size:1.3rem;}
  /* Notice rows */
  .nr{padding:.75rem .9rem;gap:.65rem;}
  .nrank{width:1.5rem;height:1.5rem;font-size:.65rem;}
  .ntitle{font-size:.82rem;}
  .nagency{font-size:.7rem;}
  /* Badges — keep readable, min 44px touch on links */
  .badge{font-size:.6rem;padding:.22rem .5rem;}
  a.badge, button.badge{min-height:44px;display:inline-flex;align-items:center;}
  /* Buttons */
  .btn{min-height:44px;padding:.5rem .9rem;}
  .btn.sm{min-height:36px;padding:.35rem .7rem;}
  /* Cards */
  .cb{padding:.9rem;}
  .ch{padding:.7rem .9rem;}
  /* Forms */
  .fc2{font-size:.86rem;padding:.55rem .75rem;}
  /* File grid */
  .fgrid{grid-template-columns:1fr;gap:.65rem;}
  .fc{padding:.85rem 1rem;}
  /* Dashboard psub wraps */
  .psub{font-size:.78rem;line-height:1.5;}
  .ptitle{font-size:1.1rem;}
  /* Login card */
  .lb{width:100%;}
  .lcard{padding:1.25rem;}
  /* Tiers on homepage */
  .tiers{margin:1.5rem auto 2rem;padding:0 .75rem;}
}
</style>
"""

_PLAUSIBLE = (
    '<!-- Privacy-friendly analytics by Plausible -->'
    '<script async src="https://plausible.io/js/pa-gmqjZOhHFkswfjIvJ8cNe.js"></script>'
    '<script>'
    'window.plausible=window.plausible||function(){(plausible.q=plausible.q||[]).push(arguments)},plausible.init=plausible.init||function(i){plausible.o=i||{}};'
    'plausible.init()'
    '</script>'
)


# ── Layout helpers ────────────────────────────────────────────────────────────

def _topnav(active: str = "", has_sidebar: bool = True) -> str:
    user_html = ""
    if current_user.is_authenticated:
        admin_link = ""
        if current_user.is_admin_user:
            cls = "active" if active == "admin" else ""
            admin_link = f'<a href="{url_for("admin_dash")}" class="nav-link {cls}">Admin</a>'
        user_html = (f'<div class="nav-user">'
                     f'{admin_link}'
                     f'<strong>{current_user.name}</strong>'
                     f'<a href="{url_for("logout")}" class="btn bg-ghost sm">Sign out</a>'
                     f'</div>')
    ham_btn = (f'<label class="hamburger" for="nav-toggle" aria-label="Open menu">'
               f'<span></span><span></span><span></span></label>')
    show_ham = current_user.is_authenticated and has_sidebar
    return (f'<nav class="nav">'
            f'{ham_btn if show_ham else ""}'
            f'<a href="/" style="display:flex;align-items:center;text-decoration:none;">'
            '<svg xmlns="http://www.w3.org/2000/svg" width="180" height="40" viewBox="0 0 220 48" style="display:block;">'
            '<path d="M4 8 Q16 3 28 6 Q38 9 48 4 L48 13 Q38 18 28 15 Q16 12 4 17 Z" fill="#FFFFFF"/>'
            '<path d="M4 22 Q16 17 28 20 Q38 23 48 18 L48 27 Q38 32 28 29 Q16 26 4 31 Z" fill="#2A9D8F"/>'
            '<path d="M4 36 Q16 31 28 34 Q38 37 48 32 L48 41 Q38 46 28 43 Q16 40 4 45 Z" fill="#2A9D8F" opacity="0.5"/>'
            '<text x="60" y="32" font-family="Inter,\'Helvetica Neue\',Arial,sans-serif" font-size="26" font-weight="700" letter-spacing="-0.4">'
            '<tspan fill="#FFFFFF">Bid</tspan><tspan fill="#2A9D8F">Edge</tspan>'
            '</text></svg>'
            f'</a>{user_html}</nav>')


def _sidebar(active: str = "") -> str:
    def lnk(href, icon, label, key):
        cls = "on" if active == key else ""
        return f'<a href="{href}" class="{cls}">{icon}&nbsp; {label}</a>'

    admin_links = ""
    if current_user.is_authenticated and current_user.is_admin_user:
        admin_links = (
            # Subtle divider between client-facing and admin sections
            f'<div style="border-top:1px solid var(--border);margin:.75rem .5rem .5rem;"></div>'
            f'<div class="side-sec">Admin</div>'
            f'{lnk(url_for("admin_dash"),          "⚙",  "Dashboard",     "admin")}'
            f'{lnk(url_for("admin_leads"),         "📥", "Leads",          "admin-leads")}'
            f'{lnk(url_for("admin_clients_list"),  "👥", "Clients",        "admin-clients")}'
            f'{lnk(url_for("admin_requests"),      "📦", "Requests",       "admin-requests")}'
            f'{lnk(url_for("admin_briefs"),        "📨", "Briefs",         "admin-briefs")}'
            f'{lnk(url_for("admin_pipeline"),      "⚡", "Pipeline",       "admin-pipeline")}'
            f'{lnk(url_for("intel_dash"),          "🛰",  "Intel Library",  "intel")}'
            f'{lnk(url_for("admin_sector_review"), "⚠",  "Sector Review",  "admin-sector")}'
        )
    return (f'<nav class="side">'
            f'<div class="side-sec">Intelligence</div>'
            f'{lnk(url_for("gw_dashboard"),        "⬛", "Dashboard",    "home")}'
            f'{lnk(url_for("gw_watchlist"),   "📋", "Watchlist",    "watchlist")}'
            f'{lnk(url_for("gw_pursuits"),    "🎯", "Pursuits",     "pursuits")}'
            f'{lnk(url_for("gw_competitors"), "📊", "Competitors",  "competitors")}'
            f'{lnk(url_for("gw_briefs"),      "📬", "Watch Briefs", "briefs")}'
            f'<div class="side-sec">Actions</div>'
            f'{lnk(url_for("gw_request"),     "✉",  "Request",      "request")}'
            f'{admin_links}'
            f'<div class="side-sec" style="margin-top:auto;padding-top:1rem;">Account</div>'
            f'{lnk(url_for("account_page"),   "👤", "My Account",   "account")}'
            f'{lnk(url_for("gw_help"),        "?",  "Help",         "help")}'
            f'</nav>')


def _page(title: str, body: str, active: str = "",
          sidebar: bool = True, public: bool = False) -> str:
    nav  = "" if public else _topnav(active, has_sidebar=(sidebar and not public))
    side = _sidebar(active) if sidebar and not public else ""
    if sidebar and not public:
        wrap_open  = '<div class="shell">'
        wrap_close = '</div>'
        cont_open  = '<div class="main">'
        cont_close = '</div>'
    else:
        wrap_open = wrap_close = cont_open = cont_close = ""
    flashes = ""
    for msg, cat in getattr(g, "_portal_flashes", []):
        css = "al-ok" if cat == "success" else "al-er"
        flashes += f'<div class="al {css}">{msg}</div>'
    # Hamburger toggle: hidden checkbox drives pure-CSS sidebar open/close.
    # Label (the ☰ button) lives in the nav; checkbox must precede .shell in DOM.
    ham = '<input type="checkbox" id="nav-toggle">' if (sidebar and not public) else ""
    # Overlay closes the drawer when tapped; label[for] toggles the checkbox off
    overlay = ('<label class="side-overlay" for="nav-toggle" aria-label="Close menu"></label>'
               if (sidebar and not public) else "")
    return (f'<!DOCTYPE html><html lang="en"><head>'
            f'<meta charset="UTF-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{title if public else f"{title} — Groundwork by BidEdge"}</title>'
            f'<link rel="icon" type="image/svg+xml" href="/static/img/bidedge-mark.svg">'
            f'<link rel="alternate icon" href="/static/img/bidedge-mark.svg">'
            f'{CSS}{_PLAUSIBLE}'
            f'</head><body>'
            f'{ham}{nav}{wrap_open}{overlay}{side}{cont_open}{flashes}{body}'
            f'{cont_close}{wrap_close}</body></html>')


def _flash(msg: str, cat: str = "success") -> None:
    if not hasattr(g, "_portal_flashes"):
        g._portal_flashes = []
    g._portal_flashes.append((msg, cat))


# ── Badge helpers ─────────────────────────────────────────────────────────────

def _dtc_badge(dtc) -> str:
    if dtc is None: return '<span class="badge bk">TBC</span>'
    if dtc <= 7:    return f'<span class="badge br">URGENT {dtc}d</span>'
    if dtc <= 14:   return f'<span class="badge bg">{dtc}d</span>'
    return f'<span class="badge bk">{dtc}d</span>'

_SECTOR_DISPLAY: dict[str, str] = {
    "ICT":                  "ICT",
    "FM":                   "FM",
    "infrastructure":       "Infrastructure",
    "construction":         "Construction",
    "defence":              "Defence",
    "health":               "Health",
    "cybersecurity":        "ICT",
    "security":             "Security",
    "advisory":             "Advisory",
    "utilities":            "Utilities",
    "professional_services":"Professional Services",
    "other":                "Other",
}

def _fmt_sector(sector: str) -> str:
    """Canonical display label for a sector tag — always correct case."""
    s = (sector or "other").strip()
    return _SECTOR_DISPLAY.get(s) or _SECTOR_DISPLAY.get(s.lower()) or s.replace("_", " ").title()

def _sector_badge(sector: str) -> str:
    return f'<span class="badge bn">{_fmt_sector(sector)}</span>'

# Tender type badge — visually distinct by posture so RFI is never confused with RFP
_TENDER_TYPE_STYLES: dict = {
    # Live bids — teal (same family as sector badge but bolder)
    "request for proposal":     ("RFP",            "background:#e0f4f2;color:#1a6b62;border:1px solid #2a9d8f;"),
    "request for tender":       ("RFT",            "background:#e0f4f2;color:#1a6b62;border:1px solid #2a9d8f;"),
    "request for quote":        ("RFQ",            "background:#e0f4f2;color:#1a6b62;border:1px solid #2a9d8f;"),
    "panel / prequalification": ("Panel",          "background:#e0f4f2;color:#1a6b62;border:1px solid #2a9d8f;"),
    # Information / market research — amber/orange
    "request for information":  ("RFI",            "background:#fff3cd;color:#7a5000;border:1px solid #d4a017;"),
    "notice of information":    ("NOI",            "background:#fff3cd;color:#7a5000;border:1px solid #d4a017;"),
    # Qualification (EOI/ROI) — blue-grey
    "expression of interest":   ("EOI",            "background:#eaf0f8;color:#2c5282;border:1px solid #6b8fc4;"),
    # Advance notice — light neutral
    "advance notice":           ("Advance Notice", "background:#f0f0f0;color:#556b7d;border:1px solid #b0bcd4;"),
}

def _tender_type_badge(procurement_stage: str, category_raw: str) -> str:
    """Return a styled tender type badge. Falls back to raw category_raw if stage unmapped."""
    stage_key = (procurement_stage or "").lower().strip()
    for key, (label, style) in _TENDER_TYPE_STYLES.items():
        if key in stage_key:
            return (
                f'<span style="font-size:.67rem;font-weight:700;padding:.15rem .45rem;'
                f'border-radius:4px;letter-spacing:.02em;white-space:nowrap;{style}">'
                f'{label}</span>'
            )
    # Fallback: derive short label from category_raw
    raw = (category_raw or "").strip()
    if not raw:
        return ""
    # Try to extract parenthesised abbreviation e.g. "(RFI)" from GETS verbose label
    import re as _re
    m = _re.search(r'\(([A-Z]{2,6})\)\s*$', raw)
    short = m.group(1) if m else raw[:12]
    # RFI/NOI/Advance in raw — amber; else neutral
    if _re.search(r'\brfi\b|\bnoi\b|information|advance', raw.lower()):
        style = "background:#fff3cd;color:#7a5000;border:1px solid #d4a017;"
    elif _re.search(r'\broi\b|\beoi\b|interest', raw.lower()):
        style = "background:#eaf0f8;color:#2c5282;border:1px solid #6b8fc4;"
    else:
        style = "background:#e0f4f2;color:#1a6b62;border:1px solid #2a9d8f;"
    return (
        f'<span style="font-size:.67rem;font-weight:700;padding:.15rem .45rem;'
        f'border-radius:4px;letter-spacing:.02em;white-space:nowrap;{style}">'
        f'{short}</span>'
    )

# Value band display labels and sort rank (higher = more valuable)
_VALUE_BAND_LABELS = {
    "under_100k": "Under $100K",
    "100k_500k":  "$100K–$500K",
    "500k_2m":    "$500K–$2M",
    "2m_10m":     "$2M–$10M",
    "10m_plus":   "$10M+",
    "unknown":    "TBC",
}
_VALUE_BAND_RANK = {
    "10m_plus": 5, "2m_10m": 4, "500k_2m": 3,
    "100k_500k": 2, "under_100k": 1, "unknown": 0,
}

def _value_badge(band: str) -> str:
    """Return a value band badge. Returns empty string for unknown/TBC bands — no noise."""
    if not band or band == "unknown":
        return ""
    label = _VALUE_BAND_LABELS.get(band, "")
    if not label or label == "TBC":
        return ""
    return f'<span class="badge bn">{label}</span>'

def _intel_sector_map() -> dict:
    """
    Return {sector: source_short_name} for all sectors that have at least one
    active intel signal. Used to render the ⚡ strategic flag badge.
    """
    try:
        rows = db.fetchall("""
            SELECT DISTINCT ON (sect)
                   unnest(sig.affected_sectors) AS sect,
                   src.short_name
              FROM v_active_signals sig
              JOIN intel_sources src ON src.id = sig.source_id
             ORDER BY sect, src.short_name
        """)
        return {r["sect"]: r["short_name"] for r in rows}
    except Exception:
        return {}

def _notice_sort_key(n: dict):
    """Sort key: urgency first (days ASC, None last), then value DESC."""
    dtc = n.get("days_until_close")
    dtc_key = dtc if dtc is not None else 9999
    val_key = -_VALUE_BAND_RANK.get(n.get("value_band") or "unknown", 0)
    return (dtc_key, val_key)


# ── Public routes ─────────────────────────────────────────────────────────────

@app.route("/about")
def about():
    body = f"""
<style>
.about-nav{{display:flex;align-items:center;padding:1.1rem 2.5rem;
  background:var(--nav-bg,rgba(15,24,41,.97));border-bottom:1px solid var(--border);
  gap:1.5rem;position:sticky;top:0;z-index:100;}}
.about-nav .pub-brand{{flex:1;font-size:1rem;font-weight:800;color:var(--text);
  letter-spacing:-.02em;}}
.about-nav .pub-brand span{{color:var(--gold);font-weight:400;}}
.about-content{{max-width:680px;margin:0 auto;padding:4rem 2rem 5rem;}}
.about-h1{{font-size:2rem;font-weight:900;color:var(--text);letter-spacing:-.03em;
  line-height:1.2;margin-bottom:.6rem;}}
.about-sub{{font-size:1rem;color:var(--muted);line-height:1.7;margin-bottom:3rem;}}
.about-section{{margin-bottom:2.5rem;}}
.about-section h2{{font-size:.75rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
  color:var(--gold);margin-bottom:.7rem;}}
.about-section p{{font-size:.95rem;color:var(--muted);line-height:1.8;margin:0;}}
.about-divider{{border:none;border-top:1px solid var(--border);margin:2.5rem 0;}}
.about-footer{{text-align:center;padding:2.5rem 0 3rem;font-size:.8rem;color:var(--muted);}}
.about-footer a{{color:var(--muted);text-decoration:none;}}
.about-footer a:hover{{color:var(--text);}}
</style>
<div class="about-nav">
  <a href="/" style="display:flex;align-items:center;text-decoration:none;">
    <svg xmlns="http://www.w3.org/2000/svg" width="180" height="40" viewBox="0 0 220 48" style="display:block;">
      <path d="M4 8 Q16 3 28 6 Q38 9 48 4 L48 13 Q38 18 28 15 Q16 12 4 17 Z" fill="#FFFFFF"/>
      <path d="M4 22 Q16 17 28 20 Q38 23 48 18 L48 27 Q38 32 28 29 Q16 26 4 31 Z" fill="#2A9D8F"/>
      <path d="M4 36 Q16 31 28 34 Q38 37 48 32 L48 41 Q38 46 28 43 Q16 40 4 45 Z" fill="#2A9D8F" opacity="0.5"/>
      <text x="60" y="32" font-family="Inter,'Helvetica Neue',Arial,sans-serif" font-size="26" font-weight="700" letter-spacing="-0.4">
        <tspan fill="#FFFFFF">Bid</tspan><tspan fill="#2A9D8F">Edge</tspan>
      </text>
    </svg>
  </a>
  <a href="{url_for('groundwork_landing')}" style="font-size:.82rem;color:var(--muted);
     text-decoration:none;transition:color .12s;"
     onmouseover="this.style.color='var(--text)'" onmouseout="this.style.color='var(--muted)'">Groundwork</a>
  <a href="{url_for('terrain_landing')}" style="font-size:.82rem;color:var(--muted);
     text-decoration:none;transition:color .12s;"
     onmouseover="this.style.color='var(--text)'" onmouseout="this.style.color='var(--muted)'">Terrain</a>
  <a href="{url_for('keystone_landing')}" style="font-size:.82rem;color:var(--muted);
     text-decoration:none;transition:color .12s;"
     onmouseover="this.style.color='var(--text)'" onmouseout="this.style.color='var(--muted)'">Keystone</a>
  <a href="{url_for('about')}" style="font-size:.82rem;color:var(--muted);
     text-decoration:none;transition:color .12s;"
     onmouseover="this.style.color='var(--text)'" onmouseout="this.style.color='var(--muted)'">About</a>
  <a href="{url_for('login')}" class="btn bg-out"
     style="margin-left:auto;font-size:.82rem;">Client Login</a>
</div>
<div class="about-content">
  <h1 class="about-h1">About BidEdge</h1>
  <p class="about-sub">New Zealand procurement intelligence — built to help NZ organisations
  compete more effectively for government contracts.</p>

  <div class="about-section">
    <h2>Who we are</h2>
    <p>BidEdge is a New Zealand-owned procurement intelligence firm. We build tools that help NZ
    organisations understand the government contracting market before they commit resources to
    pursuits. Our platform, Groundwork, monitors every published GETS tender daily, enriches each
    one with a decade of contract award history, and delivers actionable intelligence — so you know
    the field, the incumbents, and whether it is worth your team's time.</p>
  </div>

  <hr class="about-divider">

  <div class="about-section">
    <h2>Our approach</h2>
    <p>We believe in evidence over intuition. Every assessment Groundwork produces is grounded in
    published procurement data — award histories, agency buying patterns, supplier win records — not
    guesswork or market rumour. We tell clients what the data says, including when it says
    don't bid. That honesty is the most useful thing we can offer.</p>
  </div>

  <hr class="about-divider">

  <div class="about-section">
    <h2>Ethical intelligence</h2>
    <p>We only use publicly available information. We do not access confidential tender data,
    maintain relationships with evaluators, or provide any service that would compromise the
    integrity of a procurement process. Our job is to help you understand the market clearly —
    not to game it. Every piece of intelligence Groundwork produces can be traced back to
    a public source.</p>
  </div>

  <hr class="about-divider">

  <div class="about-section">
    <h2>Contact</h2>
    <p>Questions, feedback, or requests — reach us at
    <a href="mailto:robert@bidedge.co.nz"
       style="color:var(--gold);text-decoration:none;">robert@bidedge.co.nz</a></p>
  </div>
</div>
<div class="about-footer">
  &copy; BidEdge Ltd &middot;
  <a href="{url_for('homepage')}">BidEdge suite</a> &middot;
  <a href="{url_for('groundwork_landing')}">Groundwork</a> &middot;
  <a href="{url_for('groundwork_landing')}#pricing">Pricing</a> &middot;
  <a href="{url_for('demo')}">Demo</a> &middot;
  <a href="{url_for('login')}">Client Login</a>
</div>
"""
    return _page("About BidEdge", body, public=True, sidebar=False)


@app.route("/")
def homepage():
    body = (
        '<style>'
        '.pub-nav-link{font-size:.82rem;color:var(--muted);padding:.3rem .5rem;'
        'text-decoration:none;transition:color .12s;white-space:nowrap;}'
        '.pub-nav-link:hover{color:var(--text);}'
        '.pub-nav-login{margin-left:auto;font-size:.82rem;flex-shrink:0;}'
        '.suite-cards{display:grid;grid-template-columns:repeat(3,1fr);gap:1.5rem;'
        'max-width:1020px;margin:3rem auto 5rem;padding:0 2.5rem;}'
        '.suite-card{background:var(--surf);border:1px solid var(--border);border-radius:12px;'
        'padding:2rem 1.75rem;display:flex;flex-direction:column;}'
        '.suite-card.primary{border-color:var(--gold);border-width:2px;'
        'box-shadow:0 0 0 1px rgba(42,157,143,.15),0 8px 32px rgba(0,0,0,.35);}'
        '.suite-lbl{font-size:.65rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;'
        'color:var(--gold);margin-bottom:.5rem;}'
        '.suite-name{font-size:1.5rem;font-weight:900;color:var(--text);margin-bottom:.6rem;}'
        '.suite-tagline{font-size:.9rem;color:var(--text);line-height:1.5;font-weight:600;'
        'margin-bottom:.75rem;}'
        '.suite-desc{font-size:.83rem;color:var(--muted);line-height:1.65;flex:1;'
        'margin-bottom:1.75rem;}'
        '@media(max-width:768px){'
        '.suite-cards{grid-template-columns:1fr;padding:0 1rem;gap:1.25rem;margin:2rem auto 3rem;}'
        '.suite-card{padding:1.5rem 1.25rem;}'
        '.pub-nav-link{display:none;}'
        '.pub-nav{overflow:hidden;}'
        '}'
        '</style>'
        f'<nav class="pub-nav">'
        '<a href="/" style="display:flex;align-items:center;text-decoration:none;flex-shrink:0;">'
        '<svg xmlns="http://www.w3.org/2000/svg" width="180" height="40" viewBox="0 0 220 48" style="display:block;">'
        '<path d="M4 8 Q16 3 28 6 Q38 9 48 4 L48 13 Q38 18 28 15 Q16 12 4 17 Z" fill="#FFFFFF"/>'
        '<path d="M4 22 Q16 17 28 20 Q38 23 48 18 L48 27 Q38 32 28 29 Q16 26 4 31 Z" fill="#2A9D8F"/>'
        '<path d="M4 36 Q16 31 28 34 Q38 37 48 32 L48 41 Q38 46 28 43 Q16 40 4 45 Z" fill="#2A9D8F" opacity="0.5"/>'
        '<text x="60" y="32" font-family="Inter,\'Helvetica Neue\',Arial,sans-serif" font-size="26" font-weight="700" letter-spacing="-0.4">'
        '<tspan fill="#FFFFFF">Bid</tspan><tspan fill="#2A9D8F">Edge</tspan>'
        '</text></svg>'
        '</a>'
        f'<a href="{url_for("groundwork_landing")}" class="pub-nav-link">Groundwork</a>'
        f'<a href="{url_for("terrain_landing")}" class="pub-nav-link">Terrain</a>'
        f'<a href="{url_for("keystone_landing")}" class="pub-nav-link">Keystone</a>'
        f'<a href="{url_for("about")}" class="pub-nav-link">About</a>'
        f'<a href="{url_for("login")}" class="btn bg-out pub-nav-login">Client Login</a>'
        f'</nav>'
        f'<div class="hero">'
        f'<h1>BidEdge<br><span>Most organisations act on incomplete intelligence.</span></h1>'
        f'<p class="hero-sub">Know before you bid. Know before you enter. Know before you decide.</p>'
        f'</div>'
        f'<div style="text-align:center;padding:0 0 1rem;">'
        f'<div style="font-size:.72rem;font-weight:700;letter-spacing:.12em;'
        f'text-transform:uppercase;color:var(--muted);">Our products</div>'
        f'</div>'
        f'<div class="suite-cards">'
        f'<div class="suite-card primary">'
        f'<div class="suite-lbl">Procurement Intelligence</div>'
        f'<div class="suite-name">Groundwork</div>'
        f'<div class="suite-tagline">Know before you bid. Win when you do.</div>'
        f'<p class="suite-desc">Daily scored opportunity monitoring across every NZ government'
        f' tender — enriched with a decade of contract award history. Know the field, the likely'
        f' incumbents, and whether it\'s worth pursuing before you\'ve read a single page of the'
        f' tender document.</p>'
        f'<a href="{url_for("groundwork_landing")}" class="btn bg-gold">Explore Groundwork &rarr;</a>'
        f'</div>'
        f'<div class="suite-card">'
        f'<div class="suite-lbl">Market Intelligence</div>'
        f'<div class="suite-name">Terrain</div>'
        f'<div class="suite-tagline">Know the ground before you move.</div>'
        f'<p class="suite-desc">A fixed-price market opportunity scan. Understand which'
        f' segments to prioritise, where you\'re positioned to compete, and which clients'
        f' to target first — built from public data, competitive analysis, and sector'
        f' signal intelligence.</p>'
        f'<a href="{url_for("terrain_landing")}" class="btn bg-out">Request a scan &rarr;</a>'
        f'</div>'
        f'<div class="suite-card">'
        f'<div class="suite-lbl">Strategic Intelligence</div>'
        f'<div class="suite-name">Keystone</div>'
        f'<div class="suite-tagline">Every signal. One decision agenda.</div>'
        f'<p class="suite-desc">Regulatory, financial, performance, operational, workforce,'
        f' and external environment and risk signals synthesised into a single ranked'
        f' decision agenda for leadership teams. Act, Watch, or Defer — with the'
        f' evidence to back it.</p>'
        f'<a href="{url_for("keystone_landing")}" class="btn bg-out">Talk to us &rarr;</a>'
        f'</div>'
        f'</div>'
        f'<div class="pub-footer">&copy; BidEdge Ltd &middot; '
        f'Intelligence for NZ organisations &middot; '
        f'<a href="{url_for("about")}">About</a> &middot; '
        f'<a href="{url_for("login")}">Client Login</a>'
        f'</div>'
    )
    return _page("BidEdge — Strategic Intelligence for New Zealand Organisations", body, public=True, sidebar=False)


@app.route("/terrain", methods=["GET", "POST"])
def terrain_landing():
    import json as _json
    from pathlib import Path as _Path
    from html import escape as _esc

    sent  = False
    error = ""

    if request.method == "POST":
        name    = request.form.get("name", "").strip()
        org     = request.form.get("org", "").strip()
        email   = request.form.get("email", "").strip()
        role    = request.form.get("role", "").strip()
        context = request.form.get("context", "").strip()

        if not (name and email):
            error = "Please provide your name and email address."
        else:
            try:
                db.execute(
                    """INSERT INTO leads
                           (name, organisation, role, email, plan, source, status, notes)
                       VALUES (%s, %s, %s, %s, 'terrain', 'terrain_form', 'enquiry', %s)""",
                    (name, org, role, email, context or None),
                )
                logger.info("Terrain lead saved: %s <%s>", name, email)
            except Exception as exc:
                logger.error("terrain lead save failed: %s", exc)
                signups_path = _Path(__file__).parent / "signups.json"
                try:
                    existing = _json.loads(signups_path.read_text()) if signups_path.exists() else []
                except Exception:
                    existing = []
                import time as _time
                existing.append({"ts": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
                                  "source": "terrain_form", "name": name, "org": org,
                                  "email": email, "role": role, "context": context})
                signups_path.write_text(_json.dumps(existing, indent=2))
            return redirect(url_for("terrain_landing") + "?sent=1")

    sent = bool(request.args.get("sent"))
    err_banner = (f'<div class="al al-er">{_esc(error)}</div>') if error else ""
    sent_banner = (
        '<div class="al al-ok" style="max-width:560px;margin:0 auto 1.5rem;">'
        'Request received — a BidEdge adviser will be in touch within one business day.'
        '</div>'
    ) if sent else ""

    form_html = (
        f'<div class="lcard" style="max-width:480px;margin:0 auto;">'
        f'{err_banner}'
        f'<form action="{url_for("terrain_landing")}" method="POST">'
        f'<div class="fg"><label class="fl">Full name *</label>'
        f'<input name="name" type="text" class="fc2" placeholder="Jane Smith" required></div>'
        f'<div class="fg"><label class="fl">Organisation *</label>'
        f'<input name="org" type="text" class="fc2" placeholder="Your company or agency"></div>'
        f'<div class="fg"><label class="fl">Email address *</label>'
        f'<input name="email" type="email" class="fc2" placeholder="jane@example.com" required></div>'
        f'<div class="fg"><label class="fl">Your role <span style="color:var(--muted);font-weight:400;">(optional)</span></label>'
        f'<input name="role" type="text" class="fc2" placeholder="e.g. Property Manager, Director"></div>'
        f'<div class="fg"><label class="fl">Tell us about your market focus <span style="color:var(--muted);font-weight:400;">(optional)</span></label>'
        f'<textarea name="context" class="fc2" rows="3" placeholder="Which sector, segment, or client type are you assessing? What decision is this scan informing?"></textarea></div>'
        f'<button type="submit" class="btn bg-gold" style="width:100%;justify-content:center;'
        f'font-size:.9rem;padding:.7rem 1.5rem;">Request a scan &rarr;</button>'
        f'<p style="font-size:.75rem;color:var(--muted);text-align:center;margin-top:1rem;">'
        f'No commitment — we\'ll discuss your requirements and confirm scope.</p>'
        f'</form></div>'
    )

    body = (
        f'<nav class="pub-nav">'
        f'<a href="/" class="pub-brand" style="flex-shrink:0;text-decoration:none;color:#fff;">'
        f'BidEdge <span>&#8594; Terrain</span></a>'
        f'<a href="{url_for("groundwork_landing")}" class="pub-nav-link">Groundwork</a>'
        f'<a href="{url_for("terrain_landing")}" class="pub-nav-link">Terrain</a>'
        f'<a href="{url_for("keystone_landing")}" class="pub-nav-link">Keystone</a>'
        f'<a href="{url_for("about")}" class="pub-nav-link">About</a>'
        f'<a href="{url_for("login")}" class="btn bg-out pub-nav-login">Client Login</a>'
        f'</nav>'
        f'<div style="max-width:680px;margin:0 auto;padding:3.5rem 1.5rem 5rem;">'
        f'{sent_banner}'
        f'<div style="font-size:.65rem;font-weight:700;letter-spacing:.12em;'
        f'text-transform:uppercase;color:var(--gold);margin-bottom:.65rem;">'
        f'Market Intelligence</div>'
        f'<h1 style="font-size:2.25rem;font-weight:900;color:var(--text);'
        f'letter-spacing:-.02em;line-height:1.2;margin-bottom:.75rem;">Terrain</h1>'
        f'<p style="font-size:1.05rem;font-weight:600;color:var(--text);margin-bottom:1rem;">'
        f'Know the ground before you move.</p>'
        f'<p style="font-size:.93rem;color:var(--muted);line-height:1.75;margin-bottom:1.75rem;">'
        f'Terrain is a fixed-price market opportunity scan for organisations entering new'
        f' segments, expanding into new client relationships, or stress-testing where to'
        f' focus BD investment. Each scan maps market size, competitive density, segment'
        f' friction, and emerging signals — then identifies your highest-fit targets based'
        f' on evidence, not instinct.</p>'
        f'<div style="background:var(--surf);border:1px solid var(--border);border-radius:10px;'
        f'padding:1.5rem 1.75rem;margin-bottom:2.5rem;">'
        f'<div style="font-size:.7rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;'
        f'color:var(--gold);margin-bottom:1rem;">What you get</div>'
        f'<ul style="list-style:none;margin:0;padding:0;">'
        f'<li style="font-size:.87rem;color:var(--muted);padding:.4rem 0;'
        f'border-top:1px solid var(--border);display:flex;gap:.6rem;">'
        f'<span style="color:var(--gold);flex-shrink:0;">&#10003;</span>'
        f'Market sizing — TAM, SAM, and obtainable share with evidence basis and proxy logic</li>'
        f'<li style="font-size:.87rem;color:var(--muted);padding:.4rem 0;'
        f'border-top:1px solid var(--border);display:flex;gap:.6rem;">'
        f'<span style="color:var(--gold);flex-shrink:0;">&#10003;</span>'
        f'Segment friction/value analysis — which segments are accessible now versus long-game</li>'
        f'<li style="font-size:.87rem;color:var(--muted);padding:.4rem 0;'
        f'border-top:1px solid var(--border);display:flex;gap:.6rem;">'
        f'<span style="color:var(--gold);flex-shrink:0;">&#10003;</span>'
        f'Growth signal mapping across a 6–18 month horizon: risks, procurement shifts, and scenarios</li>'
        f'<li style="font-size:.87rem;color:var(--muted);padding:.4rem 0;'
        f'border-top:1px solid var(--border);display:flex;gap:.6rem;">'
        f'<span style="color:var(--gold);flex-shrink:0;">&#10003;</span>'
        f'High-fit BD target shortlist with strategic fit ratings and signal evidence</li>'
        f'<li style="font-size:.87rem;color:var(--muted);padding:.4rem 0;'
        f'border-top:1px solid var(--border);display:flex;gap:.6rem;">'
        f'<span style="color:var(--gold);flex-shrink:0;">&#10003;</span>'
        f'Competitiveness summary and recommended segment focus</li>'
        f'</ul></div>'
        f'<p style="text-align:center;margin:.5rem 0 1.5rem;">'
        f'<a href="{url_for("terrain_sample")}" style="color:#2a9d8f;font-size:.87rem;'
        f'text-decoration:underline;">See a sample output &rarr;</a></p>'
        f'<div class="pricing-anchor" style="margin:1.5rem 0 1.25rem;">'
        f'<span style="font-size:1.5rem;font-weight:800;color:#fff;">$6,500 '
        f'<span style="font-size:1.2rem;color:#2a9d8f;">+ GST</span></span><br>'
        f'<span style="font-size:.85rem;color:var(--muted);">Delivered within 10 business days of scope confirmation</span>'
        f'</div>'
        f'<h2 style="font-size:1.1rem;font-weight:800;color:var(--text);margin-bottom:.75rem;">'
        f'Request a Terrain scan</h2>'
        f'<p style="font-size:.87rem;color:var(--muted);margin-bottom:1.5rem;">'
        f'Tell us about your market focus and we\'ll be in touch to discuss scope and timeline.</p>'
        f'{form_html}'
        f'</div>'
        f'<div class="pub-footer">&copy; BidEdge Ltd &middot; '
        f'<a href="{url_for("homepage")}">Suite</a> &middot; '
        f'<a href="{url_for("login")}">Client Login</a></div>'
    )
    return _page("Terrain by BidEdge — Market Opportunity Scans", body, public=True, sidebar=False)


@app.route("/terrain/sample")
def terrain_sample():
    body = (
        f'<nav class="pub-nav">'
        f'<a href="/" class="pub-brand" style="flex-shrink:0;text-decoration:none;color:#fff;">'
        f'BidEdge <span>&#8594; Terrain Sample</span></a>'
        f'<a href="{url_for("groundwork_landing")}" class="pub-nav-link">Groundwork</a>'
        f'<a href="{url_for("terrain_landing")}" class="pub-nav-link">Terrain</a>'
        f'<a href="{url_for("keystone_landing")}" class="pub-nav-link">Keystone</a>'
        f'<a href="{url_for("about")}" class="pub-nav-link">About</a>'
        f'<a href="{url_for("login")}" class="btn bg-out pub-nav-login">Client Login</a>'
        f'</nav>'
        f'<div style="background:#2a9d8f;color:#fff;text-align:center;padding:1rem 1.5rem;margin-bottom:0;">'
        f'<div style="font-size:.65rem;font-weight:800;letter-spacing:.15em;text-transform:uppercase;margin-bottom:.6rem;">SAMPLE OUTPUT</div>'
        f'<p style="font-size:.88rem;color:rgba(255,255,255,.85);max-width:680px;margin:0 auto .75rem;line-height:1.55;">BD investment gets wasted on markets that look attractive until you&rsquo;re already committed. Terrain maps where you&rsquo;re actually positioned to compete — identifying your highest-fit targets from public data, competitive density, and sector signal intelligence before you spend a day on pursuit.</p>'
        f'<div style="font-size:1.1rem;font-weight:700;">Terrain by BidEdge — Market Opportunity Scan</div>'
        f'<div style="font-size:.82rem;color:#ffffff;margin-top:.25rem;">This is an example of a completed Terrain engagement. Client details are fictional.</div>'
        f'</div>'
        f'<div style="max-width:960px;margin:0 auto;padding:2.5rem 1.5rem 5rem;">'
        f'<div style="margin-bottom:2.5rem;padding-top:.5rem;">'
        f'<div style="font-size:.65rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:#2a9d8f;margin-bottom:.5rem;">Terrain by BidEdge</div>'
        f'<h1 style="font-size:1.75rem;font-weight:900;color:#ffffff;letter-spacing:-.02em;margin-bottom:.4rem;">Client A — Government ICT Services</h1>'
        f'<p style="font-size:18px;font-weight:500;color:#fff;margin-bottom:1.5rem;">Market Opportunity Scan · Central Government &amp; Education Sectors</p>'
        f'<a href="/terrain" class="btn bg-gold" style="display:inline-flex;align-items:center;gap:.4rem;text-decoration:none;background:#2a9d8f;color:#fff;padding:.6rem 1.4rem;border-radius:6px;font-size:.87rem;font-weight:600;">Request your scan — $6,500 + GST &rarr;</a>'
        f'</div>'
        f'<!-- SECTION 1: Market Sizing -->'
        f'<div style="background:#1E2D40;color:#fff;padding:.85rem 1.25rem;border-radius:8px 8px 0 0;border-left:4px solid #2a9d8f;margin-bottom:0;">'
        f'<div style="font-size:.65rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#2a9d8f;margin-bottom:.2rem;">Section 1</div>'
        f'<div style="font-size:22px;font-weight:600;color:#fff;">Market Sizing</div>'
        f'</div>'
        f'<div style="background:#fff;border:1px solid #dde2e8;border-top:none;border-radius:0 0 8px 8px;padding:1.5rem;margin-bottom:2rem;">'
        f'<div style="display:flex;flex-wrap:wrap;gap:1.5rem;">'
        f'<div style="flex:1;min-width:280px;">'
        f'<div style="font-size:.9rem;font-weight:700;color:#1E2D40;margin-bottom:1rem;">Central Government</div>'
        f'<div style="background:#2a9d8f18;border-left:4px solid #2a9d8f;padding:.75rem 1rem;border-radius:4px;margin-bottom:.5rem;">'
        f'<div style="font-size:.62rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#2a9d8f;margin-bottom:.2rem;">TAM</div>'
        f'<div style="font-size:1.4rem;font-weight:800;color:#1E2D40;">$820M</div>'
        f'<div style="font-size:.78rem;color:#1E2D40;margin-top:.25rem;">Total NZ central government ICT services spend (MBIE procurement data, scaled from published agency budgets)</div>'
        f'</div>'
        f'<div style="background:#1E2D4012;border-left:4px solid #1E2D40;padding:.75rem 1rem;border-radius:4px;margin-bottom:.5rem;margin-left:1.5rem;">'
        f'<div style="font-size:.62rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#1E2D40;margin-bottom:.2rem;">SAM</div>'
        f'<div style="font-size:1.4rem;font-weight:800;color:#1E2D40;">$328M</div>'
        f'<div style="font-size:.78rem;color:#1E2D40;margin-top:.25rem;">Agencies where Client A has capability fit and panel access or eligibility (~40% of TAM)</div>'
        f'</div>'
        f'<div style="background:#1E2D4008;border-left:4px solid #1E2D40;padding:.75rem 1rem;border-radius:4px;margin-left:3rem;">'
        f'<div style="font-size:.62rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#1E2D40;margin-bottom:.2rem;">SOM</div>'
        f'<div style="font-size:1.4rem;font-weight:800;color:#1E2D40;">$33M</div>'
        f'<div style="font-size:.78rem;color:#1E2D40;margin-top:.25rem;">Realistically contestable share based on firm size, existing relationships, and panel presence (~10% of SAM)</div>'
        f'</div>'
        f'<p style="font-size:.78rem;color:#1E2D40;margin-top:1rem;border-top:1px solid #dde2e8;padding-top:.75rem;">Evidence-informed — based on MBIE procurement data, published agency ICT budgets, and AoG panel spend reporting. SOM assumes 3-4% market share achievable within 18 months given current pipeline.</p>'
        f'</div>'
        f'<div style="flex:1;min-width:280px;">'
        f'<div style="font-size:.9rem;font-weight:700;color:#1E2D40;margin-bottom:1rem;">Education Sector</div>'
        f'<div style="background:#2a9d8f18;border-left:4px solid #2a9d8f;padding:.75rem 1rem;border-radius:4px;margin-bottom:.5rem;">'
        f'<div style="font-size:.62rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#2a9d8f;margin-bottom:.2rem;">TAM</div>'
        f'<div style="font-size:1.4rem;font-weight:800;color:#1E2D40;">$210M</div>'
        f'<div style="font-size:.78rem;color:#1E2D40;margin-top:.25rem;">Total tertiary and secondary sector ICT services spend</div>'
        f'</div>'
        f'<div style="background:#1E2D4012;border-left:4px solid #1E2D40;padding:.75rem 1rem;border-radius:4px;margin-bottom:.5rem;margin-left:1.5rem;">'
        f'<div style="font-size:.62rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#1E2D40;margin-bottom:.2rem;">SAM</div>'
        f'<div style="font-size:1.4rem;font-weight:800;color:#1E2D40;">$105M</div>'
        f'<div style="font-size:.78rem;color:#1E2D40;margin-top:.25rem;">Institutions above $5M ICT budget with outsourced delivery models</div>'
        f'</div>'
        f'<div style="background:#1E2D4008;border-left:4px solid #1E2D40;padding:.75rem 1rem;border-radius:4px;margin-left:3rem;">'
        f'<div style="font-size:.62rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#1E2D40;margin-bottom:.2rem;">SOM</div>'
        f'<div style="font-size:1.4rem;font-weight:800;color:#1E2D40;">$16M</div>'
        f'<div style="font-size:.78rem;color:#1E2D40;margin-top:.25rem;">Client A obtainable share based on current education sector presence</div>'
        f'</div>'
        f'<p style="font-size:.78rem;color:#1E2D40;margin-top:1rem;border-top:1px solid #dde2e8;padding-top:.75rem;">Partially evidenced — TAM derived from TEC funding data and institutional ICT cost benchmarks. SOM assumes selective targeting of universities and large polytechnics only.</p>'
        f'</div>'
        f'</div>'
        f'</div>'
        f'<!-- SECTION 2: Friction/Value Analysis -->'
        f'<div style="background:#1E2D40;color:#fff;padding:.85rem 1.25rem;border-radius:8px 8px 0 0;border-left:4px solid #2a9d8f;margin-bottom:0;">'
        f'<div style="font-size:.65rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#2a9d8f;margin-bottom:.2rem;">Section 2</div>'
        f'<div style="font-size:22px;font-weight:600;color:#fff;">Friction / Value Analysis</div>'
        f'</div>'
        f'<div style="background:#fff;border:1px solid #dde2e8;border-top:none;border-radius:0 0 8px 8px;padding:1.5rem;margin-bottom:2rem;">'
        f'<style>.fv-grid{{display:grid;grid-template-columns:1fr 1fr;gap:3px;background:#dde2e8;min-height:420px;}}@media(max-width:640px){{.fv-grid{{grid-template-columns:1fr;}}}}</style>'
        f'<div style="display:flex;gap:0;align-items:stretch;">'
        f'<div style="display:flex;align-items:center;padding-right:.75rem;flex-shrink:0;">'
        f'<div style="writing-mode:vertical-rl;transform:rotate(180deg);font-size:.7rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#1E2D40;white-space:nowrap;">&#8593; STRATEGIC VALUE</div>'
        f'</div>'
        f'<div style="flex:1;min-width:0;">'
        f'<div class="fv-grid">'
        f'<div style="background:#2A9D8F;padding:1.5rem;color:#fff;">'
        f'<div style="font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;">HIGH VALUE &middot; LOW FRICTION</div>'
        f'<div style="font-size:14px;font-style:italic;margin-top:4px;">Prioritise &#8212; resource BD here</div>'
        f'<div style="font-size:13px;margin-top:8px;">AoG panel-eligible agencies with open procurement cycles. Moderate RFP complexity, established evaluation criteria.</div>'
        f'<div style="font-size:12px;margin-top:6px;">e.g. MSD, ACC, Waka Kotahi</div>'
        f'</div>'
        f'<div style="background:#1E2D40;border-left:3px solid #2A9D8F;padding:1.5rem;color:#fff;">'
        f'<div style="font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;">HIGH VALUE &middot; HIGH FRICTION</div>'
        f'<div style="font-size:14px;font-style:italic;margin-top:4px;">Engage strategically</div>'
        f'<div style="font-size:13px;margin-top:8px;">Core government ICT programmes with heavy security requirements, incumbent lock-in, or lengthy prequal processes.</div>'
        f'<div style="font-size:12px;margin-top:6px;">e.g. NZ Police, MBIE enterprise, IRD</div>'
        f'</div>'
        f'<div style="background:#243B55;padding:1.5rem;color:#fff;">'
        f'<div style="font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;">LOW VALUE &middot; LOW FRICTION</div>'
        f'<div style="font-size:14px;font-style:italic;margin-top:4px;">Exploit if capacity available</div>'
        f'<div style="font-size:13px;margin-top:8px;">Smaller agencies and polytechnics with limited ICT budgets but accessible procurement.</div>'
        f'<div style="font-size:12px;margin-top:6px;">e.g. TEC, small Crown entities, polytechnics</div>'
        f'</div>'
        f'<div style="background:#2C2C2C;padding:1.5rem;color:#ffffff;">'
        f'<div style="font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;">LOW VALUE &middot; HIGH FRICTION</div>'
        f'<div style="font-size:14px;font-style:italic;margin-top:4px;">Avoid unless strategic logic applies</div>'
        f'<div style="font-size:13px;margin-top:8px;">Remote or specialist agencies with high compliance burden relative to contract value.</div>'
        f'<div style="font-size:12px;margin-top:6px;">e.g. Defence-adjacent, specialist regulators</div>'
        f'</div>'
        f'</div>'
        f'<div style="text-align:center;font-size:.7rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#1E2D40;margin-top:.5rem;">ACCESS FRICTION &#8594;</div>'
        f'</div>'
        f'</div>'
        f'</div>'
        f'<!-- SECTION 3: Growth Signals -->'
        f'<div style="background:#1E2D40;color:#fff;padding:.85rem 1.25rem;border-radius:8px 8px 0 0;border-left:4px solid #2a9d8f;margin-bottom:0;">'
        f'<div style="font-size:.65rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#2a9d8f;margin-bottom:.2rem;">Section 3</div>'
        f'<div style="font-size:22px;font-weight:600;color:#fff;">Growth Signals</div>'
        f'</div>'
        f'<div style="background:#fff;border:1px solid #dde2e8;border-top:none;border-radius:0 0 8px 8px;padding:1.5rem;margin-bottom:2rem;">'
        f'<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;color:#1E2D40;">'
        f'<thead><tr style="background:#1E2D40;">'
        f'<th style="padding:.7rem .9rem;text-align:left;font-size:.82rem;font-weight:700;color:#fff;border-right:1px solid #2a9d8f40;">Signal</th>'
        f'<th style="padding:.7rem .9rem;text-align:left;font-size:.82rem;font-weight:700;color:#fff;border-right:1px solid #2a9d8f40;">Risk if Ignored</th>'
        f'<th style="padding:.7rem .9rem;text-align:left;font-size:.82rem;font-weight:700;color:#fff;">Opportunity if Pursued</th>'
        f'</tr></thead>'
        f'<tbody>'
        f'<tr style="background:#f8f9fa;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;"><strong>AoG All-of-Government panel refresh (2025–26)</strong></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;">Missing the refresh window locks Client A out of panel-only procurement for 3–5 years</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;">Securing panel position opens 40+ agencies and removes per-RFP qualification burden</td>'
        f'</tr>'
        f'<tr style="background:#fff;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;"><strong>Tertiary sector digital transformation funding (TEC rounds)</strong></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;">Competitors already embedded in institutions; delayed engagement cedes long-term relationships</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;">Early engagement with universities undertaking systems transformation creates multi-year contract potential</td>'
        f'</tr>'
        f'<tr style="background:#f8f9fa;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;"><strong>Security uplift mandates across central agencies (DPMC directive)</strong></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;">Security-capability gaps disqualify Client A from high-value mandated work</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;">Positioning as security-capable ICT supplier unlocks Tier 1 agency contracts currently beyond reach</td>'
        f'</tr>'
        f'<tr style="background:#fff;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;"><strong>Incumbent contract expiries across 7 agencies (18-month horizon)</strong></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;">Without advance intelligence, incumbents renegotiate quietly; competitive window closes before RFP is published</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;">Proactive relationship-building during expiry window creates informed, competitive bids when procurement opens</td>'
        f'</tr>'
        f'</tbody></table></div>'
        f'</div>'
        f'<!-- SECTION 4: BD Targets -->'
        f'<div style="background:#1E2D40;color:#fff;padding:.85rem 1.25rem;border-radius:8px 8px 0 0;border-left:4px solid #2a9d8f;margin-bottom:0;">'
        f'<div style="font-size:.65rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#2a9d8f;margin-bottom:.2rem;">Section 4</div>'
        f'<div style="font-size:22px;font-weight:600;color:#fff;">High-Fit BD Targets</div>'
        f'</div>'
        f'<div style="background:#fff;border:1px solid #dde2e8;border-top:none;border-radius:0 0 8px 8px;padding:1.5rem;margin-bottom:2rem;">'
        f'<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;color:#1E2D40;">'
        f'<thead><tr style="background:#1E2D40;">'
        f'<th style="padding:.7rem .9rem;text-align:left;font-size:.82rem;font-weight:700;color:#fff;border-right:1px solid #2a9d8f40;">Organisation</th>'
        f'<th style="padding:.7rem .9rem;text-align:left;font-size:.82rem;font-weight:700;color:#fff;border-right:1px solid #2a9d8f40;">Segment</th>'
        f'<th style="padding:.7rem .9rem;text-align:left;font-size:.82rem;font-weight:700;color:#fff;border-right:1px solid #2a9d8f40;">Strategic Fit</th>'
        f'<th style="padding:.7rem .9rem;text-align:left;font-size:.82rem;font-weight:700;color:#fff;">Signal Evidence</th>'
        f'</tr></thead>'
        f'<tbody>'
        f'<tr style="background:#f8f9fa;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;"><strong>Ministry of Social Development</strong></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;">Central Government</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;"><span style="color:#27ae60;font-weight:700;">HIGH</span></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;">$28M ICT services budget, AoG panel-eligible contracts, active digital transformation programme. Incumbent contract expiry Q3 2025.</td>'
        f'</tr>'
        f'<tr style="background:#fff;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;"><strong>Accident Compensation Corporation</strong></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;">Central Government</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;"><span style="color:#27ae60;font-weight:700;">HIGH</span></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;">Recent GETS activity in managed services and application support. Historically uses panel procurement; accessible to positioned suppliers.</td>'
        f'</tr>'
        f'<tr style="background:#f8f9fa;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;"><strong>University of Auckland</strong></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;">Education</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;"><span style="color:#2a9d8f;font-weight:700;">MEDIUM-HIGH</span></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;">Largest NZ tertiary ICT budget. Multi-year systems transformation underway. Procurement historically open to new suppliers with strong references.</td>'
        f'</tr>'
        f'<tr style="background:#fff;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;"><strong>Waka Kotahi NZ Transport Agency</strong></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;">Central Government</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;"><span style="color:#2a9d8f;font-weight:700;">MEDIUM-HIGH</span></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;">Active ICT procurement pipeline, multiple open contracts. Capability match on application development and managed services.</td>'
        f'</tr>'
        f'<tr style="background:#f8f9fa;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;"><strong>Victoria University of Wellington</strong></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;">Education</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;"><span style="color:#1E2D40;font-weight:600;">MEDIUM</span></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;">Active ICT services spend. Smaller budget than Auckland but more accessible procurement process and willingness to consider new suppliers.</td>'
        f'</tr>'
        f'</tbody></table></div>'
        f'</div>'
        f'<!-- SECTION 5: Competitiveness Summary -->'
        f'<div style="background:#1E2D40;color:#fff;padding:.85rem 1.25rem;border-radius:8px 8px 0 0;border-left:4px solid #2a9d8f;margin-bottom:0;">'
        f'<div style="font-size:.65rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#2a9d8f;margin-bottom:.2rem;">Section 5</div>'
        f'<div style="font-size:22px;font-weight:600;color:#fff;">Competitiveness Summary</div>'
        f'</div>'
        f'<div style="background:#fff;border:1px solid #dde2e8;border-top:none;border-radius:0 0 8px 8px;padding:1.5rem;margin-bottom:2rem;">'
        f'<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;color:#1E2D40;">'
        f'<thead><tr style="background:#1E2D40;">'
        f'<th style="padding:.7rem .9rem;text-align:left;font-size:.82rem;font-weight:700;color:#fff;border-right:1px solid #2a9d8f40;">Dimension</th>'
        f'<th style="padding:.7rem .9rem;text-align:left;font-size:.82rem;font-weight:700;color:#fff;border-right:1px solid #2a9d8f40;">Client A</th>'
        f'<th style="padding:.7rem .9rem;text-align:left;font-size:.82rem;font-weight:700;color:#fff;border-right:1px solid #2a9d8f40;">Market Benchmark</th>'
        f'<th style="padding:.7rem .9rem;text-align:left;font-size:.82rem;font-weight:700;color:#fff;">Assessment</th>'
        f'</tr></thead>'
        f'<tbody>'
        f'<tr style="background:#f8f9fa;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;"><strong>Panel Presence</strong></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;">2 AoG panels (application dev, managed services)</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;">Tier 1 competitors hold 4–6 panel positions</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;"><strong>Gap — prioritise panel refresh participation</strong></td>'
        f'</tr>'
        f'<tr style="background:#fff;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;"><strong>Government Reference Base</strong></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;">3 active central government clients</td>'
        f'<td style="padding:.7rm .9rem;font-size:.84rem;border-right:1px solid #dde2e8;">Mid-market peers average 5–8 government clients</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;"><strong>Developing — sufficient for credibility, limited for scale</strong></td>'
        f'</tr>'
        f'<tr style="background:#f8f9fa;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;"><strong>Security Certifications</strong></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;">ISO 27001 in progress; no NZISM certification</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;">Mandatory for Tier 1 agency work; standard for Tier 2</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;"><strong>Critical gap — limits addressable market by ~35%</strong></td>'
        f'</tr>'
        f'<tr style="background:#fff;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;"><strong>BD Pipeline Maturity</strong></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;">Reactive — primarily inbound referrals and GETS monitoring</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;">Leading firms run structured 18-month BD pipeline with pre-procurement engagement</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;"><strong>Below benchmark — structured pipeline investment required</strong></td>'
        f'</tr>'
        f'<tr style="background:#f8f9fa;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;"><strong>Pricing Competitiveness</strong></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;">Day rates 8–12% below market mid-point</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;">Government buyers balance value and assurance; lowest rate not always preferred</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;"><strong>Advantage — leverage in price-sensitive segments</strong></td>'
        f'</tr>'
        f'</tbody></table></div>'
        f'</div>'
        f'<div style="text-align:center;padding:2rem;background:#1E2D40;border-radius:8px;margin-top:1rem;">'
        f'<p style="color:#ffffff;font-size:.87rem;margin-bottom:1rem;">Ready to see what Terrain can do for your organisation?</p>'
        f'<a href="/terrain" class="btn bg-gold" style="display:inline-flex;align-items:center;gap:.4rem;text-decoration:none;background:#2a9d8f;color:#fff;padding:.7rem 1.75rem;border-radius:6px;font-size:.9rem;font-weight:600;">Request your scan — $6,500 + GST &rarr;</a>'
        f'</div>'
        f'</div>'
        f'<div class="pub-footer">&copy; BidEdge Ltd &middot; '
        f'<a href="{url_for("homepage")}">Suite</a> &middot; '
        f'<a href="{url_for("login")}">Client Login</a></div>'
    )
    return _page("Terrain Sample — BidEdge", body, public=True, sidebar=False)


@app.route("/keystone", methods=["GET", "POST"])
def keystone_landing():
    import json as _json
    from pathlib import Path as _Path
    from html import escape as _esc

    sent  = False
    error = ""

    if request.method == "POST":
        name    = request.form.get("name", "").strip()
        org     = request.form.get("org", "").strip()
        email   = request.form.get("email", "").strip()
        role    = request.form.get("role", "").strip()
        context = request.form.get("context", "").strip()

        if not (name and email):
            error = "Please provide your name and email address."
        else:
            try:
                db.execute(
                    """INSERT INTO leads
                           (name, organisation, role, email, plan, source, status, notes)
                       VALUES (%s, %s, %s, %s, 'keystone', 'keystone_form', 'enquiry', %s)""",
                    (name, org, role, email, context or None),
                )
                logger.info("Keystone lead saved: %s <%s>", name, email)
            except Exception as exc:
                logger.error("keystone lead save failed: %s", exc)
                signups_path = _Path(__file__).parent / "signups.json"
                try:
                    existing = _json.loads(signups_path.read_text()) if signups_path.exists() else []
                except Exception:
                    existing = []
                import time as _time
                existing.append({"ts": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
                                  "source": "keystone_form", "name": name, "org": org,
                                  "email": email, "role": role, "context": context})
                signups_path.write_text(_json.dumps(existing, indent=2))
            return redirect(url_for("keystone_landing") + "?sent=1")

    sent = bool(request.args.get("sent"))
    err_banner = (f'<div class="al al-er">{_esc(error)}</div>') if error else ""
    sent_banner = (
        '<div class="al al-ok" style="max-width:560px;margin:0 auto 1.5rem;">'
        'Request received — a BidEdge adviser will be in touch within one business day.'
        '</div>'
    ) if sent else ""

    form_html = (
        f'<div class="lcard" style="max-width:480px;margin:0 auto;">'
        f'{err_banner}'
        f'<form action="{url_for("keystone_landing")}" method="POST">'
        f'<div class="fg"><label class="fl">Full name *</label>'
        f'<input name="name" type="text" class="fc2" placeholder="Jane Smith" required></div>'
        f'<div class="fg"><label class="fl">Organisation *</label>'
        f'<input name="org" type="text" class="fc2" placeholder="Your company or agency"></div>'
        f'<div class="fg"><label class="fl">Email address *</label>'
        f'<input name="email" type="email" class="fc2" placeholder="jane@example.com" required></div>'
        f'<div class="fg"><label class="fl">Your role <span style="color:var(--muted);font-weight:400;">(optional)</span></label>'
        f'<input name="role" type="text" class="fc2" placeholder="e.g. CEO, Strategy Director"></div>'
        f'<div class="fg"><label class="fl">Tell us about your decision context <span style="color:var(--muted);font-weight:400;">(optional)</span></label>'
        f'<textarea name="context" class="fc2" rows="3" placeholder="What decisions is your leadership team facing? What streams of information are you currently trying to integrate?"></textarea></div>'
        f'<button type="submit" class="btn bg-gold" style="width:100%;justify-content:center;'
        f'font-size:.9rem;padding:.7rem 1.5rem;">Talk to us &rarr;</button>'
        f'<p style="font-size:.75rem;color:var(--muted);text-align:center;margin-top:1rem;">'
        f'We\'ll discuss your intelligence needs and tailor a briefing schedule.</p>'
        f'</form></div>'
    )

    body = (
        f'<nav class="pub-nav">'
        f'<a href="/" class="pub-brand" style="flex-shrink:0;text-decoration:none;color:#fff;">'
        f'BidEdge <span>&#8594; Keystone</span></a>'
        f'<a href="{url_for("groundwork_landing")}" class="pub-nav-link">Groundwork</a>'
        f'<a href="{url_for("terrain_landing")}" class="pub-nav-link">Terrain</a>'
        f'<a href="{url_for("keystone_landing")}" class="pub-nav-link">Keystone</a>'
        f'<a href="{url_for("about")}" class="pub-nav-link">About</a>'
        f'<a href="{url_for("login")}" class="btn bg-out pub-nav-login">Client Login</a>'
        f'</nav>'
        f'<div style="max-width:680px;margin:0 auto;padding:3.5rem 1.5rem 5rem;">'
        f'{sent_banner}'
        f'<div style="font-size:.65rem;font-weight:700;letter-spacing:.12em;'
        f'text-transform:uppercase;color:var(--gold);margin-bottom:.65rem;">'
        f'Strategic Intelligence</div>'
        f'<h1 style="font-size:2.25rem;font-weight:900;color:var(--text);'
        f'letter-spacing:-.02em;line-height:1.2;margin-bottom:.75rem;">Keystone</h1>'
        f'<p style="font-size:1.05rem;font-weight:600;color:var(--text);margin-bottom:1rem;">'
        f'Every signal. One decision agenda.</p>'
        f'<p style="font-size:.93rem;color:var(--muted);line-height:1.75;margin-bottom:1.75rem;">'
        f'Most organisations receive more information than they can act on. Keystone'
        f' synthesises your regulatory intelligence, financial performance, operational'
        f' data, workforce signals, and external environment and risk inputs into a single'
        f' prioritised decision agenda — so your leadership team can choose to Act, Watch,'
        f' or Defer with confidence and evidence.</p>'
        f'<div style="background:var(--surf);border:1px solid var(--border);border-radius:10px;'
        f'padding:1.5rem 1.75rem;margin-bottom:2.5rem;">'
        f'<div style="font-size:.7rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;'
        f'color:var(--gold);margin-bottom:1rem;">What you get</div>'
        f'<ul style="list-style:none;margin:0;padding:0;">'
        f'<li style="font-size:.87rem;color:var(--muted);padding:.4rem 0;'
        f'border-top:1px solid var(--border);display:flex;gap:.6rem;">'
        f'<span style="color:var(--gold);flex-shrink:0;">&#10003;</span>'
        f'A single view across all your intelligence streams — where things stand, colour-coded by urgency</li>'
        f'<li style="font-size:.87rem;color:var(--muted);padding:.4rem 0;'
        f'border-top:1px solid var(--border);display:flex;gap:.6rem;">'
        f'<span style="color:var(--gold);flex-shrink:0;">&#10003;</span>'
        f'Where multiple signals point to the same underlying issue — surfaced before it becomes a problem</li>'
        f'<li style="font-size:.87rem;color:var(--muted);padding:.4rem 0;'
        f'border-top:1px solid var(--border);display:flex;gap:.6rem;">'
        f'<span style="color:var(--gold);flex-shrink:0;">&#10003;</span>'
        f'Who is moving in your market and in which direction — competitors, partners, and emerging threats</li>'
        f'<li style="font-size:.87rem;color:var(--muted);padding:.4rem 0;'
        f'border-top:1px solid var(--border);display:flex;gap:.6rem;">'
        f'<span style="color:var(--gold);flex-shrink:0;">&#10003;</span>'
        f'What information gaps would change the assessment — so you know where to look next</li>'
        f'<li style="font-size:.87rem;color:var(--muted);padding:.4rem 0;'
        f'border-top:1px solid var(--border);display:flex;gap:.6rem;">'
        f'<span style="color:var(--gold);flex-shrink:0;">&#10003;</span>'
        f'Direct access to the BidEdge team to interpret findings and brief your leadership</li>'
        f'</ul></div>'
        f'<p style="text-align:center;margin:.5rem 0 1.5rem;">'
        f'<a href="{url_for("keystone_sample")}" style="color:#2a9d8f;font-size:.87rem;'
        f'text-decoration:underline;">See a sample output &rarr;</a></p>'
        f'<div class="pricing-anchor" style="margin:1.5rem 0 1.25rem;">'
        f'<span style="font-size:1.5rem;font-weight:800;color:#fff;">From $8,500 '
        f'<span style="font-size:1.2rem;color:#2a9d8f;">+ GST</span></span><br>'
        f'<span style="font-size:.85rem;color:var(--muted);">Retainer available for ongoing strategic intelligence support</span>'
        f'</div>'
        f'<h2 style="font-size:1.1rem;font-weight:800;color:var(--text);margin-bottom:.75rem;">'
        f'Talk to us about Keystone</h2>'
        f'<p style="font-size:.87rem;color:var(--muted);margin-bottom:1.5rem;">'
        f'Tell us about your organisation and we\'ll tailor a strategic intelligence solution.</p>'
        f'{form_html}'
        f'</div>'
        f'<div class="pub-footer">&copy; BidEdge Ltd &middot; '
        f'<a href="{url_for("homepage")}">Suite</a> &middot; '
        f'<a href="{url_for("login")}">Client Login</a></div>'
    )
    return _page("Keystone by BidEdge — Executive Decision Intelligence", body, public=True, sidebar=False)


@app.route("/keystone/sample")
def keystone_sample():
    body = (
        f'<nav class="pub-nav">'
        f'<a href="/" class="pub-brand" style="flex-shrink:0;text-decoration:none;color:#fff;">'
        f'BidEdge <span>&#8594; Keystone Sample</span></a>'
        f'<a href="{url_for("groundwork_landing")}" class="pub-nav-link">Groundwork</a>'
        f'<a href="{url_for("terrain_landing")}" class="pub-nav-link">Terrain</a>'
        f'<a href="{url_for("keystone_landing")}" class="pub-nav-link">Keystone</a>'
        f'<a href="{url_for("about")}" class="pub-nav-link">About</a>'
        f'<a href="{url_for("login")}" class="btn bg-out pub-nav-login">Client Login</a>'
        f'</nav>'
        f'<div style="background:#2a9d8f;color:#fff;text-align:center;padding:1rem 1.5rem;margin-bottom:0;">'
        f'<div style="font-size:.65rem;font-weight:800;letter-spacing:.15em;text-transform:uppercase;margin-bottom:.6rem;">SAMPLE OUTPUT</div>'
        f'<p style="font-size:.88rem;color:rgba(255,255,255,.85);max-width:680px;margin:0 auto .75rem;line-height:1.55;">Most leadership teams receive more information than they can act on. Keystone synthesises your regulatory, financial, operational, and market signals into a single prioritised decision agenda — so your team acts on what matters, not what&rsquo;s loudest.</p>'
        f'<div style="font-size:24px;font-weight:700;color:#ffffff;">Keystone by BidEdge — Executive Decision Pack</div>'
        f'<div style="font-size:15px;font-weight:400;color:#ffffff;margin-top:.25rem;">This is an example of a completed Keystone engagement. Client details are fictional.</div>'
        f'</div>'
        f'<div style="max-width:960px;margin:0 auto;padding:2.5rem 1.5rem 5rem;">'
        f'<div style="margin-bottom:2.5rem;padding-top:.5rem;">'
        f'<div style="font-size:.65rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:#2a9d8f;margin-bottom:.5rem;">Keystone by BidEdge</div>'
        f'<h1 style="font-size:1.75rem;font-weight:900;color:#ffffff;letter-spacing:-.02em;margin-bottom:.4rem;">Client B — Pacific Ports Authority</h1>'
        f'<p style="font-size:.9rem;color:rgba(255,255,255,.7);margin-bottom:1.5rem;">Executive Decision Pack &middot; Integrated Intelligence Synthesis</p>'
        f'<a href="/keystone" class="btn bg-gold" style="display:inline-flex;align-items:center;gap:.4rem;text-decoration:none;background:#2a9d8f;color:#fff;padding:.6rem 1.4rem;border-radius:6px;font-size:.87rem;font-weight:600;">Talk to us — from $8,500 + GST &rarr;</a>'
        f'</div>'
        f'<!-- SECTION 1: Situational Snapshot -->'
        f'<div style="background:#1E2D40;color:#fff;padding:.85rem 1.25rem;border-radius:8px 8px 0 0;border-left:4px solid #2a9d8f;margin-bottom:0;">'
        f'<div style="font-size:.65rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#2a9d8f;margin-bottom:.2rem;">Section 1</div>'
        f'<div style="font-size:1rem;font-weight:700;">Situational Snapshot</div>'
        f'</div>'
        f'<div style="background:#fff;border:1px solid #dde2e8;border-top:none;border-radius:0 0 8px 8px;padding:1.5rem;margin-bottom:2rem;">'
        f'<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;color:#1E2D40;">'
        f'<thead><tr style="background:#1E2D40;">'
        f'<th style="padding:.7rem .9rem;text-align:left;font-size:.82rem;font-weight:700;color:#fff;border-right:1px solid #2a9d8f40;width:22%;">Stream</th>'
        f'<th style="padding:.7rem .9rem;text-align:left;font-size:.82rem;font-weight:700;color:#fff;border-right:1px solid #2a9d8f40;width:14%;">Status</th>'
        f'<th style="padding:.7rem .9rem;text-align:left;font-size:.82rem;font-weight:700;color:#fff;">Summary</th>'
        f'</tr></thead>'
        f'<tbody>'
        f'<tr style="background:#f8f9fa;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;font-weight:600;">Regulatory &amp; Operating Environment</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;"><span style="background:#E67E22;color:#fff;font-size:.7rem;font-weight:700;padding:.2rem .55rem;border-radius:4px;letter-spacing:.04em;">AMBER ↓</span></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;color:#1E2D40;">Commerce Commission investigation into port sector pricing practices is active. Two related Port Authority cases have set precedent for pricing transparency obligations. Regulatory exposure elevated; legal cost risk is material over 18-month horizon.</td>'
        f'</tr>'
        f'<tr style="background:#fff;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;font-weight:600;">Competitive &amp; Market Position</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;"><span style="background:#E67E22;color:#fff;font-size:.7rem;font-weight:700;padding:.2rem .55rem;border-radius:4px;letter-spacing:.04em;">AMBER ↗</span></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;color:#1E2D40;">Pacific Ports Authority holds a natural geographic monopoly for container throughput in its catchment. No near-term competitive threat from alternative port infrastructure. Landside logistics competitors (road, rail) are gaining share on high-value cargo segments; monitoring required but not a strategic threat within 3 years.</td>'
        f'</tr>'
        f'<tr style="background:#f8f9fa;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;font-weight:600;">Capex &amp; Balance Sheet Position</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;"><span style="background:#C0392B;color:#fff;font-size:.7rem;font-weight:700;padding:.2rem .55rem;border-radius:4px;letter-spacing:.04em;">RED</span></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;color:#1E2D40;">Debt-to-equity ratio has moved to 1.8x following the wharf extension drawdown. Covenant breach risk emerges at 2.1x; the planned crane acquisition would push to approximately 2.0x under base-case revenue assumptions. The Board must decide whether to proceed, stage, or defer the crane programme before the next covenant review in Q3.</td>'
        f'</tr>'
        f'<tr style="background:#fff;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;font-weight:600;">Contractor &amp; Labour Market</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;"><span style="background:#E67E22;color:#fff;font-size:.7rem;font-weight:700;padding:.2rem .55rem;border-radius:4px;letter-spacing:.04em;">AMBER</span></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;color:#1E2D40;">Civil contractor capacity in the region is constrained. Three of the four preferred contractors for the crane foundation works are committed to competing infrastructure projects through Q2 next year. Procurement window and contractor availability do not currently align; early market engagement is advised.</td>'
        f'</tr>'
        f'</tbody></table></div>'
        f'</div>'
        f'<!-- SECTION 2: Convergence Analysis -->'
        f'<div style="background:#1E2D40;color:#fff;padding:.85rem 1.25rem;border-radius:8px 8px 0 0;border-left:4px solid #2a9d8f;margin-bottom:0;">'
        f'<div style="font-size:.65rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#2a9d8f;margin-bottom:.2rem;">Section 2</div>'
        f'<div style="font-size:1rem;font-weight:700;">Convergence Analysis</div>'
        f'</div>'
        f'<div style="background:#fff;border:1px solid #dde2e8;border-top:none;border-radius:0 0 8px 8px;padding:1.5rem;margin-bottom:2rem;">'
        f'<p style="font-size:.85rem;color:#1E2D40;margin-bottom:1.25rem;">Where two or more streams point to the same underlying issue. These are the highest priority items.</p>'
        f'<div style="background:#1E2D40;border-left:4px solid #2a9d8f;border-radius:8px;padding:1.25rem 1.5rem;margin-bottom:1rem;">'
        f'<div style="font-size:.95rem;font-weight:700;color:#fff;margin-bottom:.75rem;"><span style="color:#2a9d8f;">1. </span>Crane Acquisition Timing is the Critical Path Item</div>'
        f'<ul style="list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:.45rem;">'
        f'<li style="font-size:.83rem;color:#c0c8d4;display:flex;gap:.5rem;"><span style="color:#2a9d8f;font-weight:600;min-width:6rem;flex-shrink:0;">Capex:</span> Proceeding takes D/E to ~2.0x — within 0.1x of covenant breach trigger</li>'
        f'<li style="font-size:.83rem;color:#c0c8d4;display:flex;gap:.5rem;"><span style="color:#2a9d8f;font-weight:600;min-width:6rem;flex-shrink:0;">Contractors:</span> Preferred civil contractors unavailable until Q2; proceeding now means using Tier 2 contractors or paying availability premium</li>'
        f'<li style="font-size:.83rem;color:#c0c8d4;display:flex;gap:.5rem;"><span style="color:#2a9d8f;font-weight:600;min-width:6rem;flex-shrink:0;">Synthesis:</span> The balance sheet and contractor market both argue for staging or deferring. Neither stream alone is decisive; together they strongly indicate delay.</li>'
        f'</ul>'
        f'</div>'
        f'<div style="background:#1E2D40;border-left:4px solid #2a9d8f;border-radius:8px;padding:1.25rem 1.5rem;margin-bottom:1rem;">'
        f'<div style="font-size:.95rem;font-weight:700;color:#fff;margin-bottom:.75rem;"><span style="color:#2a9d8f;">2. </span>Regulatory Risk is Compounding Capex Risk</div>'
        f'<ul style="list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:.45rem;">'
        f'<li style="font-size:.83rem;color:#c0c8d4;display:flex;gap:.5rem;"><span style="color:#2a9d8f;font-weight:600;min-width:6rem;flex-shrink:0;">Regulatory:</span> ComCom investigation may require pricing disclosure and retrospective review — legal cost and management distraction through 2025</li>'
        f'<li style="font-size:.83rem;color:#c0c8d4;display:flex;gap:.5rem;"><span style="color:#2a9d8f;font-weight:600;min-width:6rem;flex-shrink:0;">Capex:</span> Management bandwidth for a major crane programme and a regulatory response simultaneously is limited for a business of this size</li>'
        f'<li style="font-size:.83rem;color:#c0c8d4;display:flex;gap:.5rem;"><span style="color:#2a9d8f;font-weight:600;min-width:6rem;flex-shrink:0;">Synthesis:</span> Regulatory uncertainty is a second independent argument for deferring discretionary capex until the investigation resolves or scope is clarified.</li>'
        f'</ul>'
        f'</div>'
        f'<div style="background:#1E2D40;border-left:4px solid #2a9d8f;border-radius:8px;padding:1.25rem 1.5rem;margin-bottom:1rem;">'
        f'<div style="font-size:.95rem;font-weight:700;color:#fff;margin-bottom:.75rem;"><span style="color:#2a9d8f;">3. </span>Market Position is Stable but Dependent on Infrastructure Lead</div>'
        f'<ul style="list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:.45rem;">'
        f'<li style="font-size:.83rem;color:#c0c8d4;display:flex;gap:.5rem;"><span style="color:#2a9d8f;font-weight:600;min-width:6rem;flex-shrink:0;">Competitive:</span> Geographic monopoly is intact; landside competition not material within 3 years</li>'
        f'<li style="font-size:.83rem;color:#c0c8d4;display:flex;gap:.5rem;"><span style="color:#2a9d8f;font-weight:600;min-width:6rem;flex-shrink:0;">Capex:</span> The crane programme is the primary mechanism for maintaining throughput capacity advantage. Indefinite deferral risks competitive position beyond the 3-year window.</li>'
        f'<li style="font-size:.83rem;color:#c0c8d4;display:flex;gap:.5rem;"><span style="color:#2a9d8f;font-weight:600;min-width:6rem;flex-shrink:0;">Synthesis:</span> Deferral is advisable in the near term but the programme should be re-evaluated at the 12-month mark. Indefinite delay is not a viable strategic position.</li>'
        f'</ul>'
        f'</div>'
        f'<div style="background:#1E2D40;border-left:4px solid #2a9d8f;border-radius:8px;padding:1.25rem 1.5rem;margin-bottom:0;">'
        f'<div style="font-size:.95rem;font-weight:700;color:#fff;margin-bottom:.75rem;"><span style="color:#2a9d8f;">4. </span>Contractor Market Conditions Create a Natural Decision Window</div>'
        f'<ul style="list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:.45rem;">'
        f'<li style="font-size:.83rem;color:#c0c8d4;display:flex;gap:.5rem;"><span style="color:#2a9d8f;font-weight:600;min-width:6rem;flex-shrink:0;">Contractors:</span> Preferred contractors free from Q2 next year — a natural procurement window opens without a premium</li>'
        f'<li style="font-size:.83rem;color:#c0c8d4;display:flex;gap:.5rem;"><span style="color:#2a9d8f;font-weight:600;min-width:6rem;flex-shrink:0;">Capex:</span> Q2 window aligns with the next covenant review; if debt position improves (revenue outperformance or partial repayment), the programme may be viable at that point</li>'
        f'<li style="font-size:.83rem;color:#c0c8d4;display:flex;gap:.5rem;"><span style="color:#2a9d8f;font-weight:600;min-width:6rem;flex-shrink:0;">Synthesis:</span> The Q2 contractor availability window is the logical re-evaluation trigger. Set a formal decision gate at Q2 with defined conditions for proceeding.</li>'
        f'</ul>'
        f'</div>'
        f'</div>'
        f'<!-- SECTION 3: Ranked Decision Agenda -->'
        f'<div style="background:#1E2D40;color:#fff;padding:.85rem 1.25rem;border-radius:8px 8px 0 0;border-left:4px solid #2a9d8f;margin-bottom:0;">'
        f'<div style="font-size:.65rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#2a9d8f;margin-bottom:.2rem;">Section 3</div>'
        f'<div style="font-size:1rem;font-weight:700;">Ranked Decision Agenda</div>'
        f'</div>'
        f'<div style="background:#fff;border:1px solid #dde2e8;border-top:none;border-radius:0 0 8px 8px;padding:1.5rem;margin-bottom:2rem;">'
        f'<p style="font-size:.85rem;color:#1E2D40;margin-bottom:1.25rem;">These items reflect convergence across all four input streams. For each item select Act, Watch, or Defer. Record decision and owner in Section 04. Decisions can be made in the context of current capex constraints and contractor market conditions.</p>'
        f'<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;border:1px solid #dde2e8;color:#1E2D40;">'
        f'<thead><tr style="background:#f0f2f5;">'
        f'<th style="padding:.65rem .9rem;text-align:left;font-size:.75rem;font-weight:700;letter-spacing:.06em;color:#1E2D40;border-bottom:2px solid #1E2D40;">WHY NOW</th>'
        f'<th style="padding:.65rem .9rem;text-align:left;font-size:.75rem;font-weight:700;letter-spacing:.06em;color:#27ae60;border-bottom:2px solid #1E2D40;">ACT &rarr;</th>'
        f'<th style="padding:.65rem .9rem;text-align:left;font-size:.75rem;font-weight:700;letter-spacing:.06em;color:#E67E22;border-bottom:2px solid #1E2D40;">WATCH &rarr;</th>'
        f'<th style="padding:.65rem .9rem;text-align:left;font-size:.75rem;font-weight:700;letter-spacing:.06em;color:#C0392B;border-bottom:2px solid #1E2D40;">DEFER &rarr;</th>'
        f'</tr></thead>'
        f'<tbody>'
        f'<tr><th colspan="4" style="background:#1E2D40;color:#fff;padding:.75rem 1rem;text-align:left;font-size:.88rem;">1. Crane Acquisition Programme</th></tr>'
        f'<tr>'
        f'<td style="width:25%;padding:.85rem .9rem;font-size:.82rem;vertical-align:top;border-right:1px solid #dde2e8;">Covenant review in Q3. Contractor availability window opens Q2. Regulatory investigation creates parallel management demand. All three signals converge to a short decision window.</td>'
        f'<td style="width:25%;padding:.85rem .9rem;font-size:.82rem;vertical-align:top;border-right:1px solid #dde2e8;background:#f0faf8;">Commence early market engagement with Tier 1 contractors now, ahead of formal procurement, to secure Q2 availability at standard rates.</td>'
        f'<td style="width:25%;padding:.85rem .9rem;font-size:.82rem;vertical-align:top;border-right:1px solid #dde2e8;background:#fafafa;">Monitor D/E trajectory quarterly and set a formal go/no-go gate for Q2 against defined covenant headroom and revenue conditions.</td>'
        f'<td style="width:25%;padding:.85rem .9rem;font-size:.82rem;vertical-align:top;background:#fff9f5;">Defer formal Board approval and procurement launch until Q2 gate review. Do not proceed under current balance sheet conditions.</td>'
        f'</tr>'
        f'<tr style="height:.5rem;"></tr>'
        f'<tr><th colspan="4" style="background:#1E2D40;color:#fff;padding:.75rem 1rem;text-align:left;font-size:.88rem;">2. Commerce Commission Regulatory Response</th></tr>'
        f'<tr>'
        f'<td style="width:25%;padding:.85rem .9rem;font-size:.82rem;vertical-align:top;border-right:1px solid #dde2e8;">Investigation is active. Precedent cases indicate disclosure obligations will be imposed. Delay in engaging specialist regulatory counsel increases legal cost and reduces Board control over the response narrative.</td>'
        f'<td style="width:25%;padding:.85rem .9rem;font-size:.82rem;vertical-align:top;border-right:1px solid #dde2e8;background:#f0faf8;">Retain specialist regulatory counsel with port sector experience. Commission an internal pricing review before ComCom requests it. Establish a Board sub-committee for regulatory oversight.</td>'
        f'<td style="width:25%;padding:.85rem .9rem;font-size:.82rem;vertical-align:top;border-right:1px solid #dde2e8;background:#fafafa;">Monitor investigation scope and precedent outcomes from the two related cases. Brief the full Board on exposure scenarios at the next meeting.</td>'
        f'<td style="width:25%;padding:.85rem .9rem;font-size:.82rem;vertical-align:top;background:#fff9f5;">Defer any public comment on pricing practices or investigation until legal counsel advises. Do not make voluntary disclosures ahead of legal review.</td>'
        f'</tr>'
        f'<tr style="height:.5rem;"></tr>'
        f'<tr><th colspan="4" style="background:#1E2D40;color:#fff;padding:.75rem 1rem;text-align:left;font-size:.88rem;">3. Landside Logistics Competitive Response</th></tr>'
        f'<tr>'
        f'<td style="width:25%;padding:.85rem .9rem;font-size:.82rem;vertical-align:top;border-right:1px solid #dde2e8;">Landside competitors are gaining share on high-value cargo. Not a near-term threat but the window for a proactive response (pricing, service differentiation, shipper engagement) is open now before patterns become entrenched.</td>'
        f'<td style="width:25%;padding:.85rem .9rem;font-size:.82rem;vertical-align:top;border-right:1px solid #dde2e8;background:#f0faf8;">Commission a focused analysis of high-value cargo segment trends and shipper decision criteria. Use findings to brief the commercial team on response options within 90 days.</td>'
        f'<td style="width:25%;padding:.85rem .9rem;font-size:.82rem;vertical-align:top;border-right:1px solid #dde2e8;background:#fafafa;">Track landside competitor capacity investments and pricing moves on a quarterly basis. Set a threshold (e.g., 15% share loss in any segment) for escalation to Board.</td>'
        f'<td style="width:25%;padding:.85rem .9rem;font-size:.82rem;vertical-align:top;background:#fff9f5;">Defer major service or pricing restructure until cargo analysis is complete and regulatory environment is clearer. Avoid reactive changes.</td>'
        f'</tr>'
        f'</tbody></table></div>'
        f'</div>'
        f'<!-- SECTION 4: Information Gaps & Decision Record -->'
        f'<div style="background:#1E2D40;color:#fff;padding:.85rem 1.25rem;border-radius:8px 8px 0 0;border-left:4px solid #2a9d8f;margin-bottom:0;">'
        f'<div style="font-size:.65rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#2a9d8f;margin-bottom:.2rem;">Section 4</div>'
        f'<div style="font-size:1rem;font-weight:700;">Information Gaps &amp; Decision Record</div>'
        f'</div>'
        f'<div style="background:#fff;border:1px solid #dde2e8;border-top:none;border-radius:0 0 8px 8px;padding:1.5rem;margin-bottom:2rem;">'
        f'<div style="font-size:.82rem;font-weight:700;color:#1E2D40;margin-bottom:.75rem;">Significant gaps — information that would materially change the assessment if available:</div>'
        f'<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;color:#1E2D40;">'
        f'<thead><tr style="background:#1E2D40;">'
        f'<th style="padding:.7rem .9rem;text-align:left;font-size:.82rem;font-weight:700;color:#fff;border-right:1px solid #2a9d8f40;width:40%;">Information Gap</th>'
        f'<th style="padding:.7rem .9rem;text-align:left;font-size:.82rem;font-weight:700;color:#fff;">Why It Matters</th>'
        f'</tr></thead>'
        f'<tbody>'
        f'<tr style="background:#f8f9fa;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;color:#1E2D40;border-right:1px solid #dde2e8;"><strong>ComCom investigation scope and likely timeline</strong></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;color:#1E2D40;">Determines whether regulatory cost risk is $200K or $2M; changes the materiality weighting of all capex decisions</td>'
        f'</tr>'
        f'<tr style="background:#fff;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;color:#1E2D40;border-right:1px solid #dde2e8;"><strong>Revenue forecast confidence interval under base and downside scenarios</strong></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;color:#1E2D40;">Covenant breach risk is highly sensitive to revenue assumptions; a 5% revenue downside moves D/E to 2.05x — above the breach threshold</td>'
        f'</tr>'
        f'<tr style="background:#f8f9fa;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;color:#1E2D40;border-right:1px solid #dde2e8;"><strong>Crane manufacturer lead time and pricing validity window</strong></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;color:#1E2D40;">If lead times have extended since the original quote, the Q2 procurement window may not yield the expected delivery timeline; changes the deferral calculus</td>'
        f'</tr>'
        f'<tr style="background:#fff;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;color:#1E2D40;border-right:1px solid #dde2e8;"><strong>Lender covenant waiver appetite</strong></td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;color:#1E2D40;">If lenders would grant a temporary covenant waiver for a strategic capex programme, the balance sheet constraint is less binding than the current analysis suggests</td>'
        f'</tr>'
        f'</tbody></table></div>'
        f'<div style="margin:1.5rem 0;border-top:1px solid #dde2e8;"></div>'
        f'<div style="font-size:.82rem;font-weight:700;color:#1E2D40;margin-bottom:.75rem;">Decision record — to be completed by leadership team:</div>'
        f'<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;color:#1E2D40;">'
        f'<thead><tr style="background:#1E2D40;">'
        f'<th style="padding:.7rem .9rem;text-align:left;font-size:.82rem;font-weight:700;color:#fff;border-right:1px solid #2a9d8f40;width:35%;">Item</th>'
        f'<th style="padding:.7rem .9rem;text-align:left;font-size:.82rem;font-weight:700;color:#fff;border-right:1px solid #2a9d8f40;width:20%;">Decision (Act/Watch/Defer)</th>'
        f'<th style="padding:.7rem .9rem;text-align:left;font-size:.82rem;font-weight:700;color:#fff;border-right:1px solid #2a9d8f40;width:20%;">Owner</th>'
        f'<th style="padding:.7rem .9rem;text-align:left;font-size:.82rem;font-weight:700;color:#fff;">Follow-up by</th>'
        f'</tr></thead>'
        f'<tbody>'
        f'<tr style="background:#f8f9fa;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;color:#1E2D40;border-right:1px solid #dde2e8;">Crane acquisition programme</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;color:#1E2D40;font-style:italic;text-align:center;">&mdash;</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;color:#1E2D40;font-style:italic;text-align:center;">&mdash;</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;color:#1E2D40;font-style:italic;text-align:center;">&mdash;</td>'
        f'</tr>'
        f'<tr style="background:#fff;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;color:#1E2D40;border-right:1px solid #dde2e8;">Regulatory counsel engagement</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;color:#1E2D40;font-style:italic;text-align:center;">&mdash;</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;color:#1E2D40;font-style:italic;text-align:center;">&mdash;</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;color:#1E2D40;font-style:italic;text-align:center;">&mdash;</td>'
        f'</tr>'
        f'<tr style="background:#f8f9fa;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;color:#1E2D40;border-right:1px solid #dde2e8;">Q2 crane programme decision gate</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;color:#1E2D40;font-style:italic;text-align:center;">&mdash;</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;color:#1E2D40;font-style:italic;text-align:center;">&mdash;</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;color:#1E2D40;font-style:italic;text-align:center;">&mdash;</td>'
        f'</tr>'
        f'<tr style="background:#fff;">'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;color:#1E2D40;border-right:1px solid #dde2e8;">Landside cargo segment analysis</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;color:#1E2D40;font-style:italic;text-align:center;">&mdash;</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;border-right:1px solid #dde2e8;color:#1E2D40;font-style:italic;text-align:center;">&mdash;</td>'
        f'<td style="padding:.7rem .9rem;font-size:.84rem;color:#1E2D40;font-style:italic;text-align:center;">&mdash;</td>'
        f'</tr>'
        f'</tbody></table></div>'
        f'</div>'
        f'<div style="text-align:center;padding:2rem;background:#1E2D40;border-radius:8px;margin-top:1rem;">'
        f'<p style="color:#ffffff;font-size:.87rem;margin-bottom:1rem;">See how Keystone can integrate your intelligence streams into a clear decision agenda.</p>'
        f'<a href="/keystone" style="display:inline-flex;align-items:center;gap:.4rem;text-decoration:none;background:#2a9d8f;color:#fff;padding:.7rem 1.75rem;border-radius:6px;font-size:.9rem;font-weight:600;">Talk to us — from $8,500 + GST &rarr;</a>'
        f'</div>'
        f'</div>'
        f'<div class="pub-footer">&copy; BidEdge Ltd &middot; '
        f'<a href="{url_for("homepage")}">Suite</a> &middot; '
        f'<a href="{url_for("login")}">Client Login</a></div>'
    )
    return _page("Keystone Sample — BidEdge", body, public=True, sidebar=False)


@app.route("/pricing")
def pricing_redirect():
    return redirect(url_for("groundwork_landing") + "#pricing", 301)


@app.route("/groundwork")
def groundwork_landing():
    sent = ('<div class="al al-ok" style="max-width:540px;margin:0 auto 1.5rem;">'
            'Request sent — we will be in touch.</div>') if request.args.get("sent") else ""

    # ── Pricing section toggle + card CSS (scoped to this page) ──────────────
    pricing_css = """
<style>
/* Billing toggle */
.billing-toggle{display:inline-flex;align-items:center;gap:.85rem;
  background:var(--surf);border:1px solid var(--border);border-radius:999px;
  padding:.55rem 1.25rem;font-size:.85rem;color:var(--muted);margin-bottom:2.75rem;}
.billing-toggle span{white-space:nowrap;}
.toggle-switch{position:relative;display:inline-block;width:42px;height:24px;flex-shrink:0;}
.toggle-switch input{opacity:0;width:0;height:0;}
.toggle-knob{position:absolute;cursor:pointer;inset:0;background:var(--surf2);
  border-radius:999px;transition:.2s;}
.toggle-knob::before{content:"";position:absolute;height:16px;width:16px;
  left:4px;bottom:4px;background:#fff;border-radius:50%;transition:.2s;}
.toggle-switch input:checked + .toggle-knob{background:var(--gold);}
.toggle-switch input:checked + .toggle-knob::before{transform:translateX(18px);}
/* Annual badge */
.annual-badge{display:inline-flex;align-items:center;
  background:rgba(42,157,143,.15);color:var(--gold);
  border:1px solid rgba(42,157,143,.3);border-radius:999px;
  font-size:.65rem;font-weight:700;padding:.15rem .55rem;
  margin-left:.4rem;vertical-align:middle;}
/* Pricing cards */
.pricing-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:1.5rem;
  max-width:1020px;margin:0 auto 5rem;padding:0 2.5rem;}
.ptier{background:var(--surf);border:1px solid var(--border);border-radius:12px;
  padding:2rem 1.75rem;display:flex;flex-direction:column;position:relative;}
.ptier.pop{border-color:var(--gold);border-width:2px;
  box-shadow:0 0 0 1px rgba(42,157,143,.15),0 8px 32px rgba(0,0,0,.35);}
.ptier-pop-lbl{position:absolute;top:-14px;left:50%;transform:translateX(-50%);
  background:var(--gold);color:#fff;font-size:.68rem;font-weight:700;
  letter-spacing:.06em;text-transform:uppercase;padding:.25rem .9rem;border-radius:999px;
  white-space:nowrap;}
.ptier-lbl{font-size:.65rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
  color:var(--gold);margin-bottom:.5rem;}
.ptier-name{font-size:1.25rem;font-weight:800;color:var(--text);margin-bottom:.6rem;}
.ptier-price{margin-bottom:.3rem;}
.ptier-price .amount{font-size:2.25rem;font-weight:900;color:var(--text);line-height:1;}
.ptier-price .period{font-size:.82rem;color:var(--muted);margin-left:.25rem;}
.ptier-price-sub{font-size:.75rem;color:var(--muted);margin-bottom:1.25rem;min-height:1.1em;}
.ptier-desc{font-size:.83rem;color:var(--muted);line-height:1.6;margin-bottom:1.25rem;}
.ptier ul{list-style:none;flex:1;margin-bottom:1.75rem;}
.ptier li{font-size:.82rem;color:var(--muted);padding:.32rem 0;
  border-top:1px solid var(--border);display:flex;gap:.55rem;align-items:baseline;}
.ptier li::before{content:"✓";color:var(--gold);flex-shrink:0;font-weight:700;}
.ptier li.inherited::before{content:"✓";color:var(--muted);}
.ptier .cta{display:block;text-align:center;}
/* Mobile */
@media(max-width:768px){
  .pricing-grid{grid-template-columns:1fr;padding:0 1rem;gap:1.25rem;margin-bottom:3rem;}
  .ptier{padding:1.5rem 1.25rem;}
}
</style>
"""

    # ── Pricing section HTML ──────────────────────────────────────────────────
    pricing_section = f"""
<section id="pricing" style="padding:5rem 0 0;text-align:center;">
  <h2 style="font-size:2rem;font-weight:900;color:var(--text);margin-bottom:.75rem;letter-spacing:-.02em;">
    Simple, transparent pricing</h2>
  <p style="color:var(--muted);font-size:1rem;max-width:520px;margin:0 auto 2rem;line-height:1.7;">
    No setup fees. Cancel any time. Every plan includes daily scored opportunity monitoring
    across all NZ government procurement channels.</p>
  <div class="billing-toggle">
    <span id="lbl-mo" style="color:var(--muted);">Monthly</span>
    <label class="toggle-switch">
      <input type="checkbox" id="billing-annual" checked onchange="toggleBilling(this)">
      <span class="toggle-knob"></span>
    </label>
    <span id="lbl-yr" style="color:var(--text);font-weight:600;">Annual
      <span class="annual-badge" id="annual-badge">2 months free</span></span>
  </div>

  <div class="pricing-grid">

    <!-- Watch -->
    <div class="ptier">
      <div class="ptier-lbl">Foundation</div>
      <div class="ptier-name">Watch</div>
      <div class="ptier-price">
        <span class="amount" id="watch-price">$4,900</span>
        <span class="period" id="watch-period">/yr</span>
      </div>
      <div class="ptier-price-sub" id="watch-sub">billed annually &mdash; 2 months free</div>
      <div class="ptier-desc">Daily intelligence on active GETS tenders,
        scored and ranked for your sectors.</div>
      <ul>
        <li>Daily scored watchlist</li>
        <li>Sector &amp; region filtering</li>
        <li>Opportunity scoring and ranking</li>
        <li>Likely bidders &amp; agency history</li>
        <li>AI enrichment &mdash; flags, framing</li>
      </ul>
      <a href="{url_for('signup')}?plan=watch" class="btn bg-out cta"
         style="font-size:.85rem;padding:.6rem 1.25rem;">Get started &rarr;</a>
    </div>

    <!-- Pursue (featured) -->
    <div class="ptier pop">
      <div class="ptier-pop-lbl">Most popular</div>
      <div class="ptier-lbl">Best value</div>
      <div class="ptier-name">Pursue</div>
      <div class="ptier-price">
        <span class="amount" id="pursue-price">$9,900</span>
        <span class="period" id="pursue-period">/yr</span>
      </div>
      <div class="ptier-price-sub" id="pursue-sub">billed annually &mdash; 2 months free</div>
      <div class="ptier-desc">Everything in Watch, plus competitive intelligence for every opportunity you decide to pursue — who's likely to bid, who holds the incumbent position, your win probability based on a decade of award history, and a clear go/no-go assessment before you commit a single hour of bid resource.</div>
      <ul>
        <li class="inherited">Everything in Watch</li>
        <li>AI-generated pursuit packages</li>
        <li>Competitor intelligence profiles</li>
        <li>Weekly watch briefs</li>
        <li>Contract renewal radar</li>
        <li>Full Analysis upgrade — upload authenticated tender documents for deep synthesis of ROI specifications, Q&amp;A, and briefing materials combined with competitive intelligence</li>
      </ul>
      <a href="{url_for('signup')}?plan=pursue" class="btn bg-gold cta"
         style="font-size:.85rem;padding:.6rem 1.25rem;">Get started &rarr;</a>
    </div>

    <!-- Edge -->
    <div class="ptier">
      <div class="ptier-lbl">Enterprise</div>
      <div class="ptier-name">Edge</div>
      <div class="ptier-price">
        <span class="amount" style="font-size:1.6rem;">Custom</span>
      </div>
      <div class="ptier-price-sub">talk to us about your requirements</div>
      <div class="ptier-desc">The full platform plus dedicated analyst support
        and bespoke strategic intelligence.</div>
      <ul>
        <li class="inherited">Everything in Pursue</li>
        <li>Custom agency deep-dives</li>
        <li>Strategic briefings</li>
        <li>Priority turnaround</li>
        <li>Direct access to the BidEdge team</li>
      </ul>
      <a href="{url_for('signup')}?plan=edge" class="btn bg-out cta"
         style="font-size:.85rem;padding:.6rem 1.25rem;">Talk to us &rarr;</a>
    </div>

  </div>
</section>

<script>
function toggleBilling(cb) {{
  var annual = cb.checked;
  // Prices
  document.getElementById('watch-price').textContent   = annual ? '$4,900' : '$490';
  document.getElementById('watch-period').textContent  = annual ? '/yr'    : '/mo';
  document.getElementById('watch-sub').innerHTML       = annual ? 'billed annually &mdash; 2 months free' : 'billed monthly';
  document.getElementById('pursue-price').textContent  = annual ? '$9,900' : '$990';
  document.getElementById('pursue-period').textContent = annual ? '/yr'    : '/mo';
  document.getElementById('pursue-sub').innerHTML      = annual ? 'billed annually &mdash; 2 months free' : 'billed monthly';
  // Toggle labels
  document.getElementById('lbl-mo').style.color  = annual ? 'var(--muted)' : 'var(--text)';
  document.getElementById('lbl-mo').style.fontWeight = annual ? '400' : '600';
  document.getElementById('lbl-yr').style.color  = annual ? 'var(--text)' : 'var(--muted)';
  document.getElementById('lbl-yr').style.fontWeight = annual ? '600' : '400';
  document.getElementById('annual-badge').style.display = annual ? '' : 'none';
}}
</script>
"""

    body = (
        f'{pricing_css}'
        f'<style>'
        f'.pub-nav-link{{font-size:.82rem;color:var(--muted);padding:.3rem .5rem;'
        f'text-decoration:none;transition:color .12s;white-space:nowrap;}}'
        f'.pub-nav-link:hover{{color:var(--text);}}'
        f'.pub-nav-login{{margin-left:auto;font-size:.82rem;flex-shrink:0;}}'
        f'@media(max-width:768px){{'
        f'.pub-nav-link{{display:none;}}'
        f'.pub-nav{{overflow:hidden;}}'
        f'}}'
        f'</style>'
        f'<nav class="pub-nav">'
        f'<a href="/" class="pub-brand" style="flex-shrink:0;text-decoration:none;color:#fff;">'
        f'Groundwork <span>by BidEdge</span></a>'
        f'<a href="{url_for("groundwork_landing")}" class="pub-nav-link">Groundwork</a>'
        f'<a href="{url_for("terrain_landing")}" class="pub-nav-link">Terrain</a>'
        f'<a href="{url_for("keystone_landing")}" class="pub-nav-link">Keystone</a>'
        f'<a href="{url_for("about")}" class="pub-nav-link">About</a>'
        f'<a href="{url_for("login")}" class="btn bg-out pub-nav-login">Client Login</a>'
        f'</nav>'
        f'<div class="hero">{sent}'
        f'<div style="margin-bottom:1.35rem;">'
        f'<a href="/" style="font-size:.74rem;color:var(--muted);text-decoration:none;'
        f'border:1px solid var(--border);border-radius:999px;padding:.25rem .75rem;'
        f'transition:color .12s;" onmouseover="this.style.color=\'var(--text)\'"'
        f' onmouseout="this.style.color=\'var(--muted)\'">&#8592; BidEdge suite</a>'
        f'</div>'
        f'<h1>Know before you bid.<br><span>Win when you do.</span></h1>'
        f'<p class="hero-sub">Before your competitors know the tender exists, you know '
        f'the field, the incumbents, and whether it\'s worth your team\'s time.</p>'
        f'<a href="#pricing" class="btn bg-gold" style="font-size:.9rem;padding:.65rem 1.75rem;">See pricing &darr;</a>'
        f'&nbsp; <a href="{url_for("demo")}" class="btn bg-out" style="font-size:.9rem;padding:.65rem 1.75rem;">View Demo &rarr;</a>'
        f'<div style="margin-top:2.75rem;background:var(--surf);border:1px solid var(--card-border);'
        f'border-radius:12px;padding:1.5rem 1.75rem;text-align:left;max-width:540px;margin-left:auto;margin-right:auto;">'
        f'<div style="font-size:.62rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase;'
        f'color:var(--gold);margin-bottom:.9rem;">Sample Output</div>'
        f'<div style="display:flex;align-items:center;gap:.6rem;flex-wrap:wrap;margin-bottom:.75rem;">'
        f'<span style="font-size:.65rem;font-weight:700;letter-spacing:.09em;text-transform:uppercase;'
        f'background:rgba(42,157,143,.13);color:var(--gold);border:1px solid rgba(42,157,143,.28);'
        f'border-radius:4px;padding:.15rem .55rem;">Facilities Management</span>'
        f'<span style="font-size:.7rem;color:var(--muted);">Wellington City Council</span>'
        f'</div>'
        f'<div style="font-size:1rem;font-weight:700;color:var(--text);margin-bottom:1rem;line-height:1.35;">'
        f'Facilities Management Services 2026&ndash;2029</div>'
        f'<div style="display:flex;align-items:center;gap:.75rem;flex-wrap:wrap;margin-bottom:.4rem;">'
        f'<span style="font-size:.72rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;'
        f'background:rgba(212,160,23,.14);color:#d4a017;border:1px solid rgba(212,160,23,.32);'
        f'border-radius:5px;padding:.25rem .75rem;">Conditional Go</span>'
        f'</div>'
        f'<div style="font-size:.78rem;color:var(--muted);margin-bottom:1.1rem;line-height:1.55;">'
        f'Competitive &mdash; moderate agency fit, one known incumbent, 14 days to close</div>'
        f'<div style="font-size:.7rem;font-weight:700;letter-spacing:.07em;text-transform:uppercase;'
        f'color:var(--muted);margin-bottom:.5rem;">Critical Actions</div>'
        f'<ol style="margin:0;padding-left:1.1rem;display:flex;flex-direction:column;gap:.4rem;">'
        f'<li style="font-size:.8rem;color:var(--text);line-height:1.5;">'
        f'Request pre-bid briefing before close</li>'
        f'<li style="font-size:.8rem;color:var(--text);line-height:1.5;">'
        f'Review incumbent contract history (2 prior awards, avg $2.1M)</li>'
        f'<li style="font-size:.8rem;color:var(--text);line-height:1.5;">'
        f'Confirm H&amp;S capability evidence for WCC evaluation criteria</li>'
        f'</ol>'
        f'<div style="border-top:1px solid var(--border);margin-top:1.1rem;padding-top:.85rem;'
        f'font-size:.74rem;color:var(--muted);font-style:italic;line-height:1.55;">'
        f'Groundwork doesn&rsquo;t find you more tenders. It tells you which ones are worth your time.'
        f'</div>'
        f'</div>'
        f'</div>'
        f'<div class="tiers" id="tiers">'
        f'<div class="tier"><div class="tier-lbl">Foundation</div>'
        f'<div class="tier-name">Groundwork Watch</div>'
        f'<div class="tier-desc">Daily intelligence on active GETS tenders, scored and ranked for strategic relevance.</div>'
        f'<ul><li>Daily scored watchlist (25+ notices)</li><li>AI enrichment — summary, red flags, framing</li>'
        f'<li>Weekly watch brief via email</li><li>MBIE-evidenced likely bidders</li></ul></div>'
        f'<div class="tier ft"><div class="tier-lbl">Best value</div>'
        f'<div class="tier-name">Groundwork Pursue</div>'
        f'<div class="tier-desc">Everything in Watch, plus competitive intelligence for every opportunity you decide to pursue — who\'s likely to bid, who holds the incumbent position, your win probability based on a decade of award history, and a clear go/no-go assessment before you commit a single hour of bid resource.</div>'
        f'<ul><li>Pursuit intelligence packages</li><li>Win position assessment from 27,948 MBIE awards</li>'
        f'<li>Incumbent detection &amp; competitor analysis</li>'
        f'<li>Agency procurement profiling</li><li>Recommended actions per notice</li>'
        f'<li>Full Analysis upgrade — upload authenticated tender documents for deep synthesis of ROI specifications, Q&amp;A, and briefing materials combined with competitive intelligence</li></ul></div>'
        f'<div class="tier"><div class="tier-lbl">Enterprise</div>'
        f'<div class="tier-name">Groundwork Edge</div>'
        f'<div class="tier-desc">The complete platform — pursuit intelligence, competitor profiling, renewal radar, and analyst support.</div>'
        f'<ul><li>Competitor intelligence profiles</li><li>Contract renewal radar (90-day)</li>'
        f'<li>Longitudinal pattern detection</li><li>Dedicated BidEdge analyst support</li></ul></div>'
        f'</div>'
        f"""
<section id="how-it-works" style="padding:5rem 2rem 4rem;text-align:center;">
  <h2 style="font-size:2rem;font-weight:900;color:var(--text);margin-bottom:.6rem;
    letter-spacing:-.02em;">How Groundwork works</h2>
  <p style="color:var(--muted);font-size:.95rem;max-width:480px;margin:0 auto 3rem;
    line-height:1.7;">Three steps. No subscriptions to GETS. No spreadsheets.</p>
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:2rem;
    max-width:860px;margin:0 auto;">
    <div style="text-align:left;background:var(--surf);border:1px solid var(--border);
      border-radius:12px;padding:2rem 1.75rem;">
      <div style="width:40px;height:40px;border-radius:50%;background:rgba(42,157,143,.15);
        border:1.5px solid rgba(42,157,143,.4);display:flex;align-items:center;
        justify-content:center;font-size:1rem;font-weight:900;color:var(--gold);
        margin-bottom:1.25rem;flex-shrink:0;">1</div>
      <div style="font-size:.72rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
        color:var(--gold);margin-bottom:.5rem;">Monitor</div>
      <div style="font-size:1.05rem;font-weight:800;color:var(--text);margin-bottom:.65rem;
        line-height:1.25;">Every NZ government tender, scored and ranked for your sectors.</div>
      <div style="font-size:.84rem;color:var(--muted);line-height:1.7;">Every morning.
        No noise, no manual searching — just the opportunities that are relevant to you,
        ranked by strategic fit.</div>
    </div>
    <div style="text-align:left;background:var(--surf);border:1px solid var(--border);
      border-radius:12px;padding:2rem 1.75rem;">
      <div style="width:40px;height:40px;border-radius:50%;background:rgba(42,157,143,.15);
        border:1.5px solid rgba(42,157,143,.4);display:flex;align-items:center;
        justify-content:center;font-size:1rem;font-weight:900;color:var(--gold);
        margin-bottom:1.25rem;flex-shrink:0;">2</div>
      <div style="font-size:.72rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
        color:var(--gold);margin-bottom:.5rem;">Analyse</div>
      <div style="font-size:1.05rem;font-weight:800;color:var(--text);margin-bottom:.65rem;
        line-height:1.25;">Know who you're up against before you commit.</div>
      <div style="font-size:.84rem;color:var(--muted);line-height:1.7;">See the incumbent,
        the likely field, the agency's buying history, and a clear-eyed assessment of whether
        the contract is worth your team's time and resources.</div>
    </div>
    <div style="text-align:left;background:var(--surf);border:1px solid var(--border);
      border-radius:12px;padding:2rem 1.75rem;">
      <div style="width:40px;height:40px;border-radius:50%;background:rgba(42,157,143,.15);
        border:1.5px solid rgba(42,157,143,.4);display:flex;align-items:center;
        justify-content:center;font-size:1rem;font-weight:900;color:var(--gold);
        margin-bottom:1.25rem;flex-shrink:0;">3</div>
      <div style="font-size:.72rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
        color:var(--gold);margin-bottom:.5rem;">Act</div>
      <div style="font-size:1.05rem;font-weight:800;color:var(--text);margin-bottom:.65rem;
        line-height:1.25;">Request a competitive intelligence assessment for any opportunity.</div>
      <div style="font-size:.84rem;color:var(--muted);line-height:1.7;">Know the likely field,
        the incumbent, the agency's buying history, and your realistic win position — before you
        commit your team to a bid. We tell you whether to pursue, who to watch, and what your
        path to winning looks like.</div>
    </div>
  </div>
  <style>
  @media(max-width:768px){{
    #how-it-works > div[style*="grid-template-columns"]{{
      grid-template-columns:1fr !important;max-width:100% !important;
    }}
  }}
  </style>
</section>
"""
        f'{pricing_section}'
        f'<div style="text-align:center;padding:3rem 2rem;">'
        f'<h2 style="font-size:1.5rem;margin-bottom:.75rem;">Questions? Talk to us.</h2>'
        f'<p style="color:var(--muted);margin-bottom:2rem;">Not sure which plan fits? A BidEdge adviser will help you choose.</p>'
        f'<a href="{url_for("keystone_landing")}" class="btn bg-gold" style="font-size:.9rem;padding:.65rem 1.75rem;">Get in touch &rarr;</a>'
        f'</div>'
        f'<div class="pub-footer">&copy; BidEdge Ltd &middot; Groundwork Procurement Intelligence &middot; '
        f'<a href="#how-it-works">How it works</a> &middot; '
        f'<a href="#pricing">Pricing</a> &middot; '
        f'<a href="{url_for("demo")}">Demo</a> &middot; '
        f'<a href="{url_for("about")}">About</a> &middot; '
        f'<a href="{url_for("login")}">Client Login</a></div>'
    )
    return _page("Groundwork by BidEdge — Procurement Intelligence", body, public=True, sidebar=False)


@app.route("/request-access", methods=["POST"])
def request_access():
    name  = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    org   = request.form.get("org", "").strip()
    if name and email:
        subject = f"[BidEdge] Access Request — {name}"
        html = (f"<p><b>Name:</b> {name}<br><b>Email:</b> {email}<br>"
                f"<b>Organisation:</b> {org or '(not given)'}</p>")
        _mailer_mod.send_admin_only(subject, html, _async=True)
    return redirect(url_for("homepage") + "?sent=1")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    import json as _json
    from pathlib import Path as _Path
    from html import escape as _esc

    PLAN_LABELS = {"watch": "Watch", "pursue": "Pursue", "edge": "Edge"}

    # ── POST: process submission ──────────────────────────────────────────────
    if request.method == "POST":
        plan     = request.form.get("plan", "").strip().lower()
        name     = request.form.get("name", "").strip()
        org      = request.form.get("org", "").strip()
        email    = request.form.get("email", "").strip()
        phone    = request.form.get("phone", "").strip()
        role     = request.form.get("role", "").strip()
        sectors  = request.form.get("sectors", "").strip()
        goals    = request.form.get("goals", "").strip()  # Edge only

        if not (name and email):
            return redirect(url_for("signup") + f"?plan={plan}&err=1")

        plan_label = PLAN_LABELS.get(plan, plan.title() or "Unknown")
        notes = goals  # Edge form calls it "goals"

        # ── 1. Save lead to DB ────────────────────────────────────────────────
        try:
            db.execute(
                """
                INSERT INTO leads
                    (name, organisation, role, email, phone, sectors, plan, source, status, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'signup_form', 'enquiry', %s)
                """,
                (name, org, role, email, phone, sectors, plan, notes),
            )
            logger.info("Lead saved: %s <%s> plan=%s", name, email, plan)
        except Exception as exc:
            logger.error("Failed to save lead to DB: %s", exc)
            # Fallback: JSON file
            signups_path = _Path(__file__).parent / "signups.json"
            try:
                existing = _json.loads(signups_path.read_text()) if signups_path.exists() else []
            except Exception:
                existing = []
            import time as _time
            existing.append({"ts": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
                              "plan": plan, "name": name, "org": org,
                              "email": email, "phone": phone, "goals": goals})
            signups_path.write_text(_json.dumps(existing, indent=2))

        # ── 2. Notify admin ───────────────────────────────────────────────────
        import mailer as _mailer
        rows = (f"<tr><td><b>Plan</b></td><td>{_esc(plan_label)}</td></tr>"
                f"<tr><td><b>Name</b></td><td>{_esc(name)}</td></tr>"
                f"<tr><td><b>Organisation</b></td><td>{_esc(org) or '(not given)'}</td></tr>"
                f"<tr><td><b>Role</b></td><td>{_esc(role) or '(not given)'}</td></tr>"
                f"<tr><td><b>Email</b></td><td>{_esc(email)}</td></tr>")
        if phone:
            rows += f"<tr><td><b>Phone</b></td><td>{_esc(phone)}</td></tr>"
        if goals:
            rows += f"<tr><td><b>Goals/notes</b></td><td>{_esc(goals)}</td></tr>"
        approve_url = request.host_url.rstrip("/") + url_for("admin_leads")
        email_html = (
            f"<h2>New sign-up: {_esc(plan_label)} plan</h2>"
            f"<table border='0' cellpadding='6' style='border-collapse:collapse;'>"
            f"{rows}</table>"
            f"<p style='margin-top:1.5rem;'>"
            f"<a href='{approve_url}' style='background:#2a9d8f;color:#fff;padding:.5rem 1.2rem;"
            f"border-radius:5px;text-decoration:none;font-weight:700;'>Review in Admin → Leads</a></p>"
        )
        _mailer.send_admin_only(
            subject=f"[BidEdge] New sign-up — {plan_label} — {name}",
            html=email_html,
            _async=True,
        )

        # ── 3. Prospect confirmation email (non-blocking) ─────────────────────
        # Prospect confirmation — fire-and-forget, never blocks the redirect
        import threading as _thr
        _thr.Thread(
            target=_mailer.send_signup_confirmation,
            kwargs={"name": name, "email": email, "plan_label": plan_label},
            daemon=True,
            name="mailer-signup-confirm",
        ).start()

        return redirect(url_for("signup") + f"?plan={plan}&sent=1&name={_esc(name)}")

    # ── GET: render form ──────────────────────────────────────────────────────
    plan = request.args.get("plan", "watch").strip().lower()
    if plan not in PLAN_LABELS:
        plan = "watch"
    plan_label = PLAN_LABELS[plan]
    sent = bool(request.args.get("sent"))
    err  = bool(request.args.get("err"))

    # Confirmation state
    lead_name = request.args.get("name", "").strip() or "there"
    if sent:
        confirm_body = (
            f'<div style="text-align:center;padding:4rem 2rem;">'
            f'<div style="font-size:2.5rem;margin-bottom:1rem;">✓</div>'
            f'<h2 style="font-size:1.5rem;font-weight:800;color:var(--text);margin-bottom:.75rem;">'
            f'Thanks, {_esc(lead_name)} — a BidEdge adviser will be in touch within one business day.</h2>'
            f'<p style="color:var(--muted);margin-bottom:2rem;max-width:480px;margin-left:auto;margin-right:auto;line-height:1.7;">'
            f'Your {plan_label} plan enquiry has been received. '
            f'In the meantime, explore what Groundwork looks like in practice.</p>'
            f'<a href="{url_for("demo")}" class="btn bg-gold" style="margin-right:.75rem;font-size:.9rem;">'
            f'Explore the demo &rarr;</a>'
            f'<a href="{url_for("groundwork_landing")}" class="btn bg-out" style="font-size:.9rem;">← Back to home</a>'
            f'</div>'
        )
        body = (f'<nav class="pub-nav">'
                f'<a href="/" class="pub-brand" style="text-decoration:none;color:#fff;">Groundwork <span>by BidEdge</span></a>'
                f'<a href="{url_for("login")}" class="btn bg-out" style="margin-left:auto;font-size:.82rem;">Client Login</a>'
                f'</nav>'
                f'<div style="max-width:600px;margin:0 auto;">{confirm_body}</div>'
                f'<div class="pub-footer">&copy; BidEdge Ltd &middot; '
                f'<a href="{url_for("homepage")}">Home</a></div>')
        return _page(f"BidEdge — Sign up for {plan_label}", body, public=True, sidebar=False)

    err_banner = ('<div class="al al-er">Please fill in your name and email address.</div>'
                  if err else "")

    # ── Form variants ─────────────────────────────────────────────────────────
    if plan == "edge":
        # Edge: simplified form + goals textarea, no submit button → "we'll be in touch"
        form_fields = (
            f'<div class="fg"><label class="fl">Full name *</label>'
            f'<input name="name" type="text" class="fc2" placeholder="Jane Smith" required></div>'
            f'<div class="fg"><label class="fl">Organisation *</label>'
            f'<input name="org" type="text" class="fc2" placeholder="Your company or agency" required></div>'
            f'<div class="fg"><label class="fl">Email address *</label>'
            f'<input name="email" type="email" class="fc2" placeholder="jane@example.com" required></div>'
            f'<div class="fg"><label class="fl">Your role <span style="color:var(--muted);font-weight:400;">(optional)</span></label>'
            f'<input name="role" type="text" class="fc2" placeholder="e.g. Bid Manager, CEO, Contracts Lead"></div>'
            f'<div class="fg"><label class="fl">Sectors you compete in <span style="color:var(--muted);font-weight:400;">(optional)</span></label>'
            f'<input name="sectors" type="text" class="fc2" placeholder="e.g. construction, facilities, transport"></div>'
            f'<div class="fg"><label class="fl">Tell us about your procurement goals</label>'
            f'<textarea name="goals" class="fc2" rows="3" placeholder="What markets do you operate in? What are your biggest procurement challenges?"></textarea></div>'
        )
        submit_section = (
            f'<div class="al al-in" style="margin-bottom:1.5rem;">'
            f'We\'ll be in touch to discuss your requirements and tailor a solution '
            f'for your organisation.</div>'
            f'<button type="submit" class="btn bg-gold" style="width:100%;justify-content:center;'
            f'font-size:.9rem;padding:.7rem 1.5rem;">Send enquiry &rarr;</button>'
        )
    else:
        billing_note = "$4,900/yr (or $490/mo)" if plan == "watch" else "$9,900/yr (or $990/mo)"
        form_fields = (
            f'<div class="fg"><label class="fl">Full name *</label>'
            f'<input name="name" type="text" class="fc2" placeholder="Jane Smith" required></div>'
            f'<div class="fg"><label class="fl">Organisation *</label>'
            f'<input name="org" type="text" class="fc2" placeholder="Your company or agency" required></div>'
            f'<div class="fg"><label class="fl">Email address *</label>'
            f'<input name="email" type="email" class="fc2" placeholder="jane@example.com" required></div>'
            f'<div class="fg"><label class="fl">Phone number <span style="color:var(--muted);font-weight:400;">(optional)</span></label>'
            f'<input name="phone" type="tel" class="fc2" placeholder="+64 21 000 0000"></div>'
            f'<div class="fg"><label class="fl">Your role <span style="color:var(--muted);font-weight:400;">(optional)</span></label>'
            f'<input name="role" type="text" class="fc2" placeholder="e.g. Bid Manager, CEO, Contracts Lead"></div>'
            f'<div class="fg"><label class="fl">Sectors you compete in <span style="color:var(--muted);font-weight:400;">(optional)</span></label>'
            f'<input name="sectors" type="text" class="fc2" placeholder="e.g. construction, facilities, transport"></div>'
            f'<div class="fg"><label class="fl">Plan</label>'
            f'<input name="plan_display" type="text" class="fc2" value="{plan_label} — {billing_note}" readonly '
            f'style="opacity:.65;cursor:default;"></div>'
        )
        submit_section = (
            f'<button type="submit" class="btn bg-gold" style="width:100%;justify-content:center;'
            f'font-size:.9rem;padding:.7rem 1.5rem;">Get started &rarr;</button>'
            f'<p style="font-size:.75rem;color:var(--muted);text-align:center;margin-top:1rem;">'
            f'No payment taken now — a BidEdge adviser will confirm your plan and '
            f'send an invoice within one business day.</p>'
        )

    plan_pill = (
        f'<span style="background:rgba(42,157,143,.15);color:var(--gold);'
        f'border:1px solid rgba(42,157,143,.3);border-radius:999px;'
        f'font-size:.72rem;font-weight:700;padding:.2rem .65rem;'
        f'margin-left:.6rem;vertical-align:middle;">{plan_label}</span>'
    )
    form_subtitle = ("Tell us about your organisation and we'll tailor the right solution."
                     if plan == "edge"
                     else "Fill in your details and we'll be in touch within one business day.")

    form_html = (
        f'<div style="max-width:480px;margin:0 auto;padding:2rem 1rem 4rem;">'
        f'<div style="text-align:center;margin-bottom:2.5rem;">'
        f'<div class="pub-brand" style="font-size:1.1rem;margin-bottom:.5rem;">'
        f'Groundwork <span style="color:var(--gold);">by BidEdge</span></div>'
        f'<h1 style="font-size:1.6rem;font-weight:900;color:var(--text);margin-bottom:.35rem;">'
        f'Get started{plan_pill}</h1>'
        f'<p style="color:var(--muted);font-size:.88rem;">{form_subtitle}</p>'
        f'</div>'
        f'{err_banner}'
        f'<div class="lcard">'
        f'<form action="{url_for("signup")}" method="POST">'
        f'<input type="hidden" name="plan" value="{plan}">'
        f'{form_fields}'
        f'{submit_section}'
        f'</form></div>'
        f'<div style="text-align:center;margin-top:1.5rem;">'
        f'<a href="{url_for("groundwork_landing")}#pricing" '
        f'style="font-size:.8rem;color:var(--muted);">← Back to pricing</a>'
        f'</div></div>'
    )

    body = (f'<nav class="pub-nav">'
            f'<a href="/" class="pub-brand" style="text-decoration:none;color:#fff;">Groundwork <span>by BidEdge</span></a>'
            f'<a href="{url_for("groundwork_landing")}" class="pub-nav-link" style="font-size:.82rem;">Groundwork</a>'
            f'<a href="{url_for("login")}" class="btn bg-out" style="margin-left:auto;font-size:.82rem;">Client Login</a>'
            f'</nav>'
            f'{form_html}'
            f'<div class="pub-footer">&copy; BidEdge Ltd &middot; '
            f'<a href="{url_for("homepage")}">Home</a> &middot; '
            f'<a href="{url_for("login")}">Client Login</a></div>')
    return _page(f"BidEdge — Sign up for {plan_label}", body, public=True, sidebar=False)


def _load_demo_manifest() -> dict:
    """Load the sector demo manifest; falls back to Storage then DB if local file is absent."""
    import json as _json
    from pathlib import Path as _Path
    mp = _Path(__file__).parent / "output" / "artefacts" / "demo" / "manifest.json"
    if mp.exists():
        try:
            return _json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            pass
    # Try Supabase Storage
    try:
        import storage as _storage
        data = _storage.download_file("demo/manifest.json")
        if data:
            mp.parent.mkdir(parents=True, exist_ok=True)
            mp.write_bytes(data)
            return _json.loads(data.decode("utf-8"))
    except Exception:
        pass
    # Try database pipeline_outputs
    try:
        row = db.fetchone(
            "SELECT content FROM pipeline_outputs WHERE output_type = 'demo_manifest' "
            "ORDER BY run_date DESC, created_at DESC LIMIT 1"
        )
        if row and row.get("content"):
            mp.parent.mkdir(parents=True, exist_ok=True)
            mp.write_text(row["content"], encoding="utf-8")
            return _json.loads(row["content"])
    except Exception:
        pass
    return {}


# Sector metadata mirrored from generate_demo_content (no import to avoid slowdown)
_DEMO_SECTOR_META = {
    "FM":            {"label": "Facilities Management", "icon": "🏗",
                      "tagline": "FM contracts for local government, social housing and public estates"},
    "construction":  {"label": "Construction",          "icon": "🏛",
                      "tagline": "Civil construction, roading and infrastructure delivery"},
    "defence":       {"label": "Defence",               "icon": "⚙️",
                      "tagline": "Defence facilities, critical infrastructure and security engineering"},
    "ICT":           {"label": "ICT",                   "icon": "💻",
                      "tagline": "Digital transformation, cloud migration and systems integration"},
    "infrastructure":{"label": "Infrastructure",        "icon": "🌐",
                      "tagline": "Water, transport and community infrastructure at scale"},
    "health":        {"label": "Health",                "icon": "🏥",
                      "tagline": "Clinical systems, hospital ICT and health data platforms"},
}


@app.route("/demo")
def demo():
    """
    Public demo route.
    Without ?sector=: show sector selection grid.
    With ?sector=<key>: show the three artefacts for that sector.
    """
    sector_param = request.args.get("sector", "").strip()
    # Match case-insensitively against known sector keys (FM, ICT, etc. are uppercase)
    sector_key = next(
        (k for k in _DEMO_SECTOR_META if k.lower() == sector_param.lower()),
        "",
    )

    manifest = _load_demo_manifest()
    sectors_data = manifest.get("sectors", {})

    # ── Sector detail view ────────────────────────────────────────────────────
    if sector_key and sector_key in _DEMO_SECTOR_META:
        meta   = _DEMO_SECTOR_META[sector_key]
        sdata  = sectors_data.get(sector_key, {})
        firm   = sdata.get("firm", {})
        items  = sdata.get("items", [])

        type_labels = {
            "pursuit_package":   ("Pursuit Package",   "A tailored bid intelligence brief for a live GETS notice in your sector."),
            "competitor_profile":("Competitor Profile", "An evidence-based profile of the dominant incumbent supplier."),
            "watch_brief":       ("Watch Brief",        "A weekly market briefing filtered to your sector and firm context."),
        }
        type_icons = {
            "pursuit_package": "🎯",
            "competitor_profile": "📊",
            "watch_brief": "📬",
        }

        artefact_cards = ""
        for it in items:
            kind = it.get("type", "")
            html_path = it.get("html_path", "")
            if not html_path:
                continue
            type_name, type_desc = type_labels.get(kind, (kind.replace("_", " ").title(), ""))
            icon = type_icons.get(kind, "📄")
            view_url = url_for("demo_file",
                               filepath=html_path.replace("output/artefacts/demo/", ""))
            artefact_cards += (
                f'<div class="card" style="margin-bottom:1rem;">'
                f'<div class="ch" style="gap:.75rem;">'
                f'<span style="font-size:1.2rem;">{icon}</span>'
                f'<span class="ct">{type_name}</span>'
                f'</div>'
                f'<div class="cb" style="padding:1rem 1.25rem;">'
                f'<p style="font-size:.82rem;color:var(--muted);margin-bottom:.85rem;">{type_desc}</p>'
                f'<a href="{view_url}" target="_blank" class="btn bg-gold" '
                f'style="font-size:.8rem;padding:.45rem 1.1rem;">View sample &rarr;</a>'
                f'</div></div>'
            )

        if not artefact_cards:
            artefact_cards = (
                '<div class="card cb" style="text-align:center;color:var(--muted);padding:2rem;">'
                'Demo content for this sector is being prepared. Check back shortly.</div>'
            )

        firm_blurb = ""
        if firm.get("name"):
            firm_blurb = (
                f'<div style="background:rgba(42,157,143,.07);border:1px solid rgba(42,157,143,.2);'
                f'border-radius:8px;padding:.85rem 1.1rem;margin-bottom:1.5rem;font-size:.82rem;">'
                f'<strong style="color:var(--gold);">{firm["name"]}</strong>'
                f'<span style="color:var(--muted);"> — {firm.get("description","")}</span>'
                f'</div>'
            )

        signup_url = url_for("signup") + f"?plan=pursue"
        body = (
            f'<nav class="pub-nav">'
            f'<div class="pub-brand">Groundwork <span>by BidEdge</span></div>'
            f'<a href="{url_for("demo")}" style="font-size:.82rem;color:var(--muted);padding:.3rem .5rem;text-decoration:none;">← All sectors</a>'
            f'<a href="{url_for("login")}" class="btn bg-out" style="margin-left:auto;font-size:.82rem;">Client Login</a>'
            f'</nav>'
            f'<div style="max-width:900px;margin:0 auto;padding:2.5rem 1.5rem 4rem;">'
            f'<div style="font-size:2rem;margin-bottom:.5rem;">{meta["icon"]}</div>'
            f'<div class="ptitle">{meta["label"]} — Sample Intelligence</div>'
            f'<div class="psub" style="margin-bottom:1.5rem;">'
            f'Generated from real NZ procurement data &middot; {meta["tagline"]}</div>'
            f'<div style="background:linear-gradient(90deg,#1a6b62,#2a9d8f);color:#fff;'
            f'padding:.85rem 1.25rem;border-radius:8px;margin-bottom:1.5rem;font-size:.82rem;">'
            f'<strong>EXAMPLE CONTENT</strong> — These samples show what Groundwork delivers for a '
            f'{meta["label"].lower()} firm. Your live account will receive intelligence '
            f'personalised to your organisation, sectors and targets.</div>'
            f'{firm_blurb}'
            f'{artefact_cards}'
            f'<div class="card" style="margin-top:1.5rem;border:1px solid rgba(42,157,143,.3);">'
            f'<div class="cb" style="padding:1.75rem;text-align:center;">'
            f'<h2 style="font-size:1.15rem;color:var(--gold);margin-bottom:.6rem;">Ready for your sector?</h2>'
            f'<p style="color:var(--muted);font-size:.85rem;margin-bottom:1.1rem;">'
            f'Get live intelligence tailored to {meta["label"].lower()} — '
            f'daily opportunities, competitor tracking, and renewal radar.</p>'
            f'<a href="{signup_url}" class="btn bg-gold" style="font-size:.88rem;padding:.6rem 1.5rem;">'
            f'Get started &rarr;</a>'
            f'&nbsp;<a href="{url_for("demo")}" class="btn bg-out" style="font-size:.88rem;padding:.6rem 1.25rem;">'
            f'View other sectors</a>'
            f'</div></div>'
            f'</div>'
            f'<div class="pub-footer">&copy; BidEdge Ltd &middot; '
            f'<a href="{url_for("homepage")}">Home</a> &middot; '
            f'<a href="{url_for("demo")}">Demo</a> &middot; '
            f'<a href="{url_for("login")}">Client Login</a></div>'
        )
        return _page(f"Groundwork Demo — {meta['label']}", body, public=True, sidebar=False)

    # ── Sector selection grid ─────────────────────────────────────────────────
    sector_card_css = """
<style>
.demo-sector-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));
  gap:1rem;margin:1.5rem 0 2rem;}
.demo-sc{background:var(--surf);border:1px solid var(--border);border-radius:10px;
  padding:1.5rem 1.25rem;cursor:pointer;text-decoration:none;display:block;
  transition:border-color .15s,box-shadow .15s;}
.demo-sc:hover{border-color:var(--gold);
  box-shadow:0 0 0 1px rgba(42,157,143,.2),0 4px 16px rgba(0,0,0,.25);
  text-decoration:none;}
.demo-sc-icon{font-size:1.8rem;margin-bottom:.65rem;line-height:1;}
.demo-sc-name{font-size:.95rem;font-weight:800;color:var(--text);margin-bottom:.3rem;}
.demo-sc-tag{font-size:.78rem;color:var(--muted);line-height:1.5;}
@media(max-width:480px){.demo-sector-grid{grid-template-columns:1fr 1fr;gap:.65rem;}
  .demo-sc{padding:1.1rem 1rem;}.demo-sc-icon{font-size:1.5rem;}}
</style>
"""
    sector_grid = ""
    for sk, meta in _DEMO_SECTOR_META.items():
        has_content = bool(sectors_data.get(sk, {}).get("items"))
        ready_badge = "" if has_content else (
            ' <span style="font-size:.65rem;color:var(--muted);font-weight:400;">(coming soon)</span>'
        )
        sector_url = url_for("demo") + f"?sector={sk}"
        sector_grid += (
            f'<a href="{sector_url}" class="demo-sc">'
            f'<div class="demo-sc-icon">{meta["icon"]}</div>'
            f'<div class="demo-sc-name">{meta["label"]}{ready_badge}</div>'
            f'<div class="demo-sc-tag">{meta["tagline"]}</div>'
            f'</a>'
        )

    body = (
        f'{sector_card_css}'
        f'<nav class="pub-nav">'
        f'<a href="/" class="pub-brand" style="flex-shrink:0;text-decoration:none;color:#fff;">Groundwork <span>by BidEdge</span></a>'
        f'<a href="{url_for("groundwork_landing")}" class="pub-nav-link">Groundwork</a>'
        f'<a href="{url_for("terrain_landing")}" class="pub-nav-link">Terrain</a>'
        f'<a href="{url_for("keystone_landing")}" class="pub-nav-link">Keystone</a>'
        f'<a href="{url_for("about")}" class="pub-nav-link">About</a>'
        f'<a href="{url_for("login")}" class="btn bg-out pub-nav-login">Client Login</a>'
        f'</nav>'
        f'<div style="max-width:900px;margin:0 auto;padding:2.5rem 1.5rem 4rem;">'
        f'<div class="ptitle">Groundwork Demo</div>'
        f'<div class="psub">Choose your sector to see real sample intelligence</div>'
        f'<p style="font-size:.88rem;color:var(--muted);max-width:560px;line-height:1.7;margin-bottom:2rem;">'
        f'Each demo shows three live artefacts — a pursuit package, a competitor profile, and a watch brief — '
        f'generated from real NZ government procurement data and personalised to a fictional firm in that sector.</p>'
        f'<div class="demo-sector-grid">{sector_grid}</div>'
        f'<div class="card" style="border:1px solid rgba(42,157,143,.3);margin-top:.5rem;">'
        f'<div class="cb" style="padding:1.75rem;text-align:center;">'
        f'<h2 style="font-size:1.1rem;color:var(--gold);margin-bottom:.55rem;">'
        f'See Groundwork working for your sector</h2>'
        f'<p style="color:var(--muted);font-size:.85rem;margin-bottom:1.1rem;">'
        f'Live accounts get daily scoring, competitor tracking, renewal radar and AI-generated '
        f'pursuit packages tailored to your firm.</p>'
        f'<a href="{url_for("signup")}?plan=pursue" class="btn bg-gold" '
        f'style="font-size:.88rem;padding:.6rem 1.5rem;">Get started &rarr;</a>'
        f'&nbsp;<a href="{url_for("groundwork_landing")}#pricing" class="btn bg-out" '
        f'style="font-size:.88rem;padding:.6rem 1.25rem;">See pricing</a>'
        f'</div></div>'
        f'</div>'
        f'<div class="pub-footer">&copy; BidEdge Ltd &middot; '
        f'<a href="{url_for("homepage")}">Home</a> &middot; '
        f'<a href="{url_for("groundwork_landing")}#pricing">Pricing</a> &middot; '
        f'<a href="{url_for("login")}">Client Login</a></div>'
    )
    return _page("Groundwork Demo — BidEdge", body, public=True, sidebar=False)


@app.route("/demo/manifest.json")
def demo_manifest_json():
    """Serve the demo manifest publicly so external tools can discover artefact paths."""
    from pathlib import Path as _Path
    import json as _json
    mp = _Path(__file__).parent / "output" / "artefacts" / "demo" / "manifest.json"
    if mp.exists():
        return app.response_class(
            response=mp.read_text(encoding="utf-8"),
            status=200, mimetype="application/json"
        )
    # Fallback 1: Supabase Storage
    try:
        import storage as _storage
        data = _storage.download_file("demo/manifest.json")
        if data:
            mp.parent.mkdir(parents=True, exist_ok=True)
            mp.write_bytes(data)
            return app.response_class(
                response=data.decode("utf-8"),
                status=200, mimetype="application/json"
            )
    except Exception:
        pass
    # Fallback 2: database pipeline_outputs
    try:
        row = db.fetchone(
            "SELECT content FROM pipeline_outputs WHERE output_type = 'demo_manifest' "
            "ORDER BY run_date DESC, created_at DESC LIMIT 1"
        )
        if row and row.get("content"):
            return app.response_class(
                response=row["content"],
                status=200, mimetype="application/json"
            )
    except Exception:
        pass
    return app.response_class(
        response=_json.dumps({"error": "manifest not found"}),
        status=404, mimetype="application/json"
    )


@app.route("/demo/file/<path:filepath>")
def demo_file(filepath: str):
    """Serve a demo artefact HTML file publicly (read-only)."""
    from pathlib import Path as _Path
    demo_dir = _Path(__file__).parent / "output" / "artefacts" / "demo"
    full = (demo_dir / filepath).resolve()
    try:
        full.relative_to(demo_dir.resolve())
    except ValueError:
        abort(403)
    if not full.exists():
        # Fallback 1: Supabase Storage
        _loaded = False
        try:
            import storage as _storage
            data = _storage.download_file(f"demo/{filepath}")
            if data:
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_bytes(data)
                _loaded = True
        except Exception:
            pass
        # Fallback 2: database pipeline_outputs
        if not _loaded:
            try:
                row = db.fetchone(
                    "SELECT content FROM pipeline_outputs WHERE output_type = 'demo_html' "
                    "AND filename = %s ORDER BY run_date DESC, created_at DESC LIMIT 1",
                    (filepath,),
                )
                if row and row.get("content"):
                    full.parent.mkdir(parents=True, exist_ok=True)
                    full.write_text(row["content"], encoding="utf-8")
                    _loaded = True
            except Exception:
                pass
        if not _loaded:
            abort(404)

    # Read the HTML and inject the gold demo banner at the top of <body>
    html = full.read_text(encoding="utf-8")
    artefact_type = "Pursuit Package"
    if "competitor" in filepath.lower():
        artefact_type = "Competitor Profile"
    elif "watch_brief" in filepath.lower():
        artefact_type = "Watch Brief"

    # Banner uses position:fixed so it floats over the document without becoming
    # a flex item. The pursuit package body uses display:flex — injecting a plain
    # div at <body> start would make it a flex sibling of sidebar and main,
    # breaking the two-column layout.  position:fixed + padding-top/top overrides
    # on the CSS classes keep the layout intact for all three artefact types.
    banner_height = "48px"
    banner = (
        f'<div style="position:fixed;top:0;left:0;right:0;z-index:9999;'
        f'background:linear-gradient(90deg,#1a6b62,#2a9d8f);color:#fff;'
        f'font-weight:600;font-size:.82rem;padding:.7rem 1.5rem;text-align:center;'
        f'letter-spacing:.01em;line-height:1.3;">'
        f'<strong>EXAMPLE</strong> — This is a sample {artefact_type} generated from real NZ '
        f'procurement data to illustrate what Groundwork delivers. Your live account will '
        f'receive intelligence tailored to your sectors and targets.'
        f'</div>'
        # Push body content down and re-anchor the sticky sidebar below the fixed banner
        f'<style>'
        f'body{{padding-top:{banner_height}!important}}'
        f'.sidebar{{top:{banner_height}!important;height:calc(100vh - {banner_height})!important}}'
        f'</style>'
    )
    import re as _re
    if "<body>" in html:
        html = html.replace("<body>", "<body>" + banner, 1)
    else:
        html = _re.sub(r"(<body[^>]*>)", r"\1" + banner, html, count=1)

    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


# Sector slug → canonical sector key (for clean /demo/<slug> URLs)
_DEMO_SLUG_MAP = {
    "fm":             "FM",
    "cybersecurity":  "ICT",
    "construction":   "construction",
    "defence":        "defence",
    "ict":            "ICT",
    "infrastructure": "infrastructure",
    "health":         "health",
}


@app.route("/demo/<sector_slug>")
def demo_sector(sector_slug: str):
    """Clean sector URLs: /demo/fm, /demo/ict, /demo/cybersecurity, etc."""
    key = _DEMO_SLUG_MAP.get(sector_slug.lower())
    if not key:
        abort(404)
    return redirect(url_for("demo") + f"?sector={key}", 302)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("gw_dashboard"))
    error = ""
    if request.method == "POST":
        user = _check_password(request.form.get("username","").strip(),
                               request.form.get("password",""))
        if user:
            login_user(user, remember=request.form.get("remember") == "on")
            if user.temp_password:
                return redirect(url_for("account_change_password"))
            next_url = request.args.get("next")
            if not next_url:
                from preferences import has_preferences
                next_url = url_for("gw_dashboard") if has_preferences(user.id) else url_for("onboarding")
            return redirect(next_url)
        error = "Invalid username or password."
    err_html = f'<div class="al al-er">{error}</div>' if error else ""
    body = (f'<div class="lw"><div class="lb">'
            f'<div class="ll"><div class="ll-name">Groundwork by BidEdge</div>'
            f'<div class="ll-sub">Procurement Intelligence Platform</div></div>'
            f'<div class="lcard">'
            f'<div style="font-size:1rem;font-weight:700;margin-bottom:1.5rem;">Sign in to your account</div>'
            f'{err_html}'
            f'<form method="POST" action="{url_for("login")}">'
            f'<div class="fg"><label class="fl">Username</label>'
            f'<input name="username" type="text" class="fc2" autofocus required></div>'
            f'<div class="fg"><label class="fl">Password</label>'
            f'<input name="password" type="password" class="fc2" required></div>'
            f'<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:1.25rem;">'
            f'<label style="display:flex;align-items:center;gap:.5rem;font-size:.8rem;color:var(--muted);cursor:pointer;">'
            f'<input name="remember" type="checkbox"> Remember me</label></div>'
            f'<button type="submit" class="btn bg-gold" style="width:100%;justify-content:center;padding:.65rem;">'
            f'Sign in &rarr;</button></form></div>'
            f'<div style="text-align:center;margin-top:1.5rem;font-size:.78rem;color:var(--muted);">'
            f'<a href="{url_for("homepage")}" style="color:var(--muted);">← Back to BidEdge.com</a></div>'
            f'</div></div>')
    return _page("Sign In", body, public=True, sidebar=False)


@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))


# ── Onboarding ────────────────────────────────────────────────────────────────

@app.route("/onboarding", methods=["GET", "POST"])
@login_required
def onboarding():
    from preferences import save_user_preferences

    # Fetch distinct sector tags from DB for the multi-select
    try:
        sector_rows = db.fetchall(
            "SELECT DISTINCT sector_tag FROM parsed_notices "
            "WHERE sector_tag IS NOT NULL ORDER BY sector_tag",
            (),
        )
        available_sectors = [r["sector_tag"] for r in sector_rows]
    except Exception:
        available_sectors = list(config.SECTORS)

    # Load saved preferences to pre-populate form on GET (and re-populate on POST error)
    from preferences import get_user_preferences as _get_prefs
    saved_prefs = _get_prefs(current_user.id)
    saved_sectors = set(saved_prefs.get("sectors") or [])
    saved_agency_focus = ", ".join(saved_prefs.get("agency_focus") or [])
    saved_min_value = saved_prefs.get("min_value_nzd") or 0

    error = ""
    if request.method == "POST":
        selected = request.form.getlist("sectors")
        agency_focus = [a.strip() for a in
                        request.form.get("agency_focus", "").split(",")
                        if a.strip()]
        try:
            min_val = int(request.form.get("min_value_nzd") or 0)
        except ValueError:
            min_val = 0

        if not selected:
            error = "Please select at least one sector."
            # Re-populate with whatever the user submitted (so nothing is lost on error)
            saved_sectors = set(selected)
            saved_agency_focus = request.form.get("agency_focus", "")
            saved_min_value = min_val
        else:
            save_user_preferences(
                user_id=current_user.id,
                sectors=selected,
                agency_focus=agency_focus,
                min_value_nzd=min_val,
            )
            _flash(f"Preferences saved — showing {', '.join(selected)} opportunities.", "success")
            return redirect(url_for("gw_dashboard"))

    err_html = f'<div class="al al-er">{error}</div>' if error else ""

    # Build sector checkboxes — mark saved sectors as checked
    sector_opts = ""
    for s in available_sectors:
        label = _fmt_sector(s)
        checked = "checked" if s in saved_sectors else ""
        sector_opts += (
            f'<label style="display:flex;align-items:center;gap:.6rem;'
            f'padding:.45rem .6rem;border-radius:5px;cursor:pointer;'
            f'border:1px solid var(--border);background:var(--surf2);'
            f'font-size:.84rem;transition:border-color .15s;" '
            f'onmouseover="this.style.borderColor=\'var(--gold)\'" '
            f'onmouseout="this.style.borderColor=\'var(--border)\'">'
            f'<input type="checkbox" name="sectors" value="{s}" {checked} '
            f'style="accent-color:var(--gold);width:15px;height:15px;"> {label}</label>'
        )

    body = (
        f'<div style="min-height:100vh;display:flex;align-items:center;'
        f'justify-content:center;padding:2rem;background:var(--bg);">'
        f'<div style="width:100%;max-width:560px;">'
        f'<div style="text-align:center;margin-bottom:2rem;">'
        f'<div style="font-size:1.25rem;font-weight:800;color:#fff;">Welcome, {current_user.name}</div>'
        f'<div style="font-size:.75rem;font-weight:700;letter-spacing:.1em;'
        f'text-transform:uppercase;color:var(--gold);margin-top:.3rem;">'
        f'Set your intelligence preferences</div></div>'
        f'<div class="card">'
        f'<div class="ch"><span class="ct">Sector Focus</span>'
        f'<span style="font-size:.72rem;color:var(--muted);">Select all that apply</span></div>'
        f'<div class="cb">'
        f'{err_html}'
        f'<form method="POST" action="{url_for("onboarding")}">'
        f'<div class="fg">'
        f'<label class="fl">Which sectors are most relevant to your firm?</label>'
        f'<div style="display:grid;grid-template-columns:1fr 1fr;gap:.5rem;margin-top:.35rem;">'
        f'{sector_opts}'
        f'</div></div>'
        f'<div class="fg">'
        f'<label class="fl">Agency focus <span style="font-weight:400;color:var(--muted);">'
        f'(optional — comma-separated)</span></label>'
        f'<input name="agency_focus" class="fc2" '
        f'value="{saved_agency_focus}" '
        f'placeholder="e.g. NZTA, Ministry of Education, Waka Kotahi">'
        f'<div class="fh">Opportunities from these agencies will be highlighted.</div>'
        f'</div>'
        f'<div class="fg">'
        f'<label class="fl">Minimum contract value (NZD) '
        f'<span style="font-weight:400;color:var(--muted);">(optional)</span></label>'
        f'<input name="min_value_nzd" type="number" class="fc2" '
        f'value="{saved_min_value if saved_min_value else ""}" '
        f'placeholder="e.g. 100000" min="0" step="50000">'
        f'<div class="fh">Filter out opportunities below this threshold.</div>'
        f'</div>'
        f'<button type="submit" class="btn bg-gold" '
        f'style="width:100%;justify-content:center;padding:.65rem;">'
        f'Save preferences &rarr;</button>'
        f'<div style="text-align:center;margin-top:1rem;">'
        f'<a href="{url_for("gw_dashboard")}" '
        f'style="font-size:.78rem;color:var(--muted);">Skip for now</a>'
        f'</div>'
        f'</form></div></div></div></div>'
    )
    return _page("Set Preferences — Groundwork", body, public=False, sidebar=False)


# ── Share ─────────────────────────────────────────────────────────────────────

# ── Account ───────────────────────────────────────────────────────────────────

@app.route("/account", methods=["GET", "POST"])
@login_required
def account_page():
    cfg = _load_cfg()
    data = cfg.get("clients", {}).get(current_user.id, {})
    msg = ""

    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "profile":
            data["display_name"]  = request.form.get("display_name", "").strip() or data.get("display_name", "")
            data["organisation"]  = request.form.get("organisation", "").strip()
            data["email"]         = request.form.get("email", "").strip()
            data["phone"]         = request.form.get("phone", "").strip()
            _save_cfg(cfg)
            msg = '<div class="al al-ok">Profile updated.</div>'
        elif action == "email_prefs":
            data["email_watchlist"] = request.form.get("email_watchlist") == "1"
            data["email_briefs"]    = request.form.get("email_briefs") == "1"
            _save_cfg(cfg)
            msg = '<div class="al al-ok">Email preferences saved.</div>'
        data = _load_cfg().get("clients", {}).get(current_user.id, {})

    from preferences import get_user_preferences
    prefs = get_user_preferences(current_user.id)
    sectors_display = ", ".join(prefs.get("sectors") or []) or "—"
    min_val = prefs.get("min_value_nzd") or 0

    # Request history from DB
    try:
        pursuit_reqs = db.fetchall(
            "SELECT id, notice_id, status, requested_at, completed_at FROM pursuit_requests "
            "WHERE client_id=%s ORDER BY requested_at DESC LIMIT 20",
            (current_user.id,)
        )
    except Exception:
        pursuit_reqs = []
    try:
        comp_reqs = db.fetchall(
            "SELECT id, firm_name, status, requested_at, completed_at FROM competitor_requests "
            "WHERE user_id=%s ORDER BY requested_at DESC LIMIT 20",
            (current_user.id,)
        )
    except Exception:
        comp_reqs = []

    def _req_status_pill(s):
        c = {"pending": "color:var(--gold);", "generating": "color:#60a5fa;",
             "complete": "color:#4ade80;", "failed": "color:#f87171;"}.get(s or "", "color:var(--muted);")
        return f'<span style="font-size:.72rem;{c}">{(s or "pending").title()}</span>'

    req_rows = "".join(
        f'<tr><td style="font-size:.78rem;">Pursuit</td>'
        f'<td style="font-size:.75rem;color:var(--muted);">{r.get("notice_id","")}</td>'
        f'<td>{_req_status_pill(r.get("status"))}</td>'
        f'<td style="font-size:.72rem;color:var(--muted);">{str(r.get("requested_at",""))[:16]}</td>'
        f'</tr>'
        for r in pursuit_reqs
    ) + "".join(
        f'<tr><td style="font-size:.78rem;">Competitor</td>'
        f'<td style="font-size:.75rem;color:var(--muted);">{r.get("firm_name","")}</td>'
        f'<td>{_req_status_pill(r.get("status"))}</td>'
        f'<td style="font-size:.72rem;color:var(--muted);">{str(r.get("requested_at",""))[:16]}</td>'
        f'</tr>'
        for r in comp_reqs
    )

    ew_chk = "checked" if data.get("email_watchlist", True) else ""
    eb_chk = "checked" if data.get("email_briefs", True) else ""

    billing_colour = {"active": "#4ade80", "trial": "#fbbf24", "suspended": "#f87171"}.get(
        data.get("billing_status", "active"), "var(--muted)")

    body = (
        f'<div class="ptitle">My Account</div>'
        f'<div class="psub">{current_user.name}</div>'
        f'{msg}'
        # ── Profile card ──────────────────────────────────────────────────────
        f'<div class="card" style="max-width:600px;">'
        f'<div class="ch"><span class="ct">Profile</span></div>'
        f'<div class="cb">'
        f'<form method="POST">'
        f'<input type="hidden" name="action" value="profile">'
        f'<div class="fg"><label class="fl">Display name</label>'
        f'<input name="display_name" class="fc2" value="{data.get("display_name","")}" required></div>'
        f'<div class="fg"><label class="fl">Organisation</label>'
        f'<input name="organisation" class="fc2" value="{data.get("organisation","")}"></div>'
        f'<div class="fg"><label class="fl">Email address</label>'
        f'<input name="email" type="email" class="fc2" value="{data.get("email","")}"></div>'
        f'<div class="fg"><label class="fl">Phone <span style="color:var(--muted);font-weight:400;">(optional)</span></label>'
        f'<input name="phone" type="tel" class="fc2" value="{data.get("phone","")}"></div>'
        f'<div class="fg">'
        f'<label class="fl">Sector preferences</label>'
        f'<div style="font-size:.83rem;color:var(--muted);margin-bottom:.35rem;">{sectors_display}</div>'
        f'<a href="{url_for("onboarding")}" class="btn bg-out sm">Update sectors →</a>'
        f'</div>'
        f'<div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-top:.5rem;">'
        f'<div><div style="font-size:.7rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:.3rem;">Plan</div>'
        f'<div>{_plan_pill(data.get("plan","pursue"))}</div></div>'
        f'<div><div style="font-size:.7rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:.3rem;">Status</div>'
        f'<div style="font-size:.8rem;color:{billing_colour};">{data.get("billing_status","active").title()}</div></div>'
        f'</div>'
        f'<div style="margin-top:1.25rem;">'
        f'<button type="submit" class="btn bg-gold sm">Save profile</button></div>'
        f'</form></div></div>'
        # ── Password card ─────────────────────────────────────────────────────
        f'<div class="card" style="max-width:600px;">'
        f'<div class="ch"><span class="ct">Change Password</span></div>'
        f'<div class="cb">'
        f'<a href="{url_for("account_change_password")}" class="btn bg-out sm">Change password →</a>'
        f'</div></div>'
        # ── Email preferences ─────────────────────────────────────────────────
        f'<div class="card" style="max-width:600px;">'
        f'<div class="ch"><span class="ct">Email Preferences</span></div>'
        f'<div class="cb">'
        f'<form method="POST">'
        f'<input type="hidden" name="action" value="email_prefs">'
        f'<label style="display:flex;align-items:center;gap:.75rem;margin-bottom:1rem;cursor:pointer;">'
        f'<input type="checkbox" name="email_watchlist" value="1" {ew_chk} style="accent-color:var(--gold);width:15px;height:15px;">'
        f'<div><div style="font-size:.85rem;color:var(--text);">Daily watchlist emails</div>'
        f'<div style="font-size:.75rem;color:var(--muted);">Get notified each morning when your watchlist is updated</div></div>'
        f'</label>'
        f'<label style="display:flex;align-items:center;gap:.75rem;margin-bottom:1.25rem;cursor:pointer;">'
        f'<input type="checkbox" name="email_briefs" value="1" {eb_chk} style="accent-color:var(--gold);width:15px;height:15px;">'
        f'<div><div style="font-size:.85rem;color:var(--text);">Weekly watch brief emails</div>'
        f'<div style="font-size:.75rem;color:var(--muted);">Receive your weekly intelligence brief each Monday morning</div></div>'
        f'</label>'
        f'<button type="submit" class="btn bg-gold sm">Save preferences</button>'
        f'</form></div></div>'
        # ── Request history ───────────────────────────────────────────────────
        f'<div class="card" style="max-width:600px;">'
        f'<div class="ch"><span class="ct">Request History</span></div>'
        f'<div style="overflow-x:auto;">'
        f'<table class="dt"><thead><tr>'
        f'<th>Type</th><th>Reference</th><th>Status</th><th>Date</th>'
        f'</tr></thead><tbody>'
        f'{req_rows or "<tr><td colspan=4 style=color:var(--muted);text-align:center;padding:1.5rem>No requests yet</td></tr>"}'
        f'</tbody></table></div></div>'
    )
    return _page("My Account — Groundwork", body, "account")


@app.route("/account/password", methods=["GET", "POST"])
@login_required
def account_change_password():
    msg = ""
    error = ""

    if request.method == "POST":
        current_pw = request.form.get("current_password", "")
        new_pw     = request.form.get("new_password", "")
        confirm_pw = request.form.get("confirm_password", "")

        if not new_pw or len(new_pw) < 8:
            error = "New password must be at least 8 characters."
        elif new_pw != confirm_pw:
            error = "New passwords do not match."
        else:
            # Verify current password
            user_check = _check_password(current_user.id, current_pw)
            if not user_check:
                error = "Current password is incorrect."
            else:
                cfg = _load_cfg()
                data = cfg.get("clients", {}).get(current_user.id, {})
                data["password_hash"] = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt()).decode()
                data["temp_password"] = False
                _save_cfg(cfg)
                _flash("Password changed successfully.", "success")
                from preferences import has_preferences
                return redirect(url_for("gw_dashboard") if has_preferences(current_user.id) else url_for("onboarding"))

    is_temp = getattr(current_user, "temp_password", False)
    temp_banner = (
        '<div class="al al-in" style="margin-bottom:1.25rem;">'
        '<strong>Welcome!</strong> You\'re signed in with a temporary password. '
        'Please set a permanent password before continuing.</div>'
    ) if is_temp else ""
    err_html = f'<div class="al al-er">{error}</div>' if error else ""

    body = (
        f'<div class="ptitle">Change Password</div>'
        f'<div class="psub">Choose a strong password for your account.</div>'
        f'<div class="card" style="max-width:480px;">'
        f'<div class="ch"><span class="ct">Set New Password</span></div>'
        f'<div class="cb">'
        f'{temp_banner}'
        f'{err_html}'
        f'<form method="POST">'
        f'<div class="fg"><label class="fl">Current password</label>'
        f'<input name="current_password" type="password" class="fc2" required autofocus></div>'
        f'<div class="fg"><label class="fl">New password <span style="color:var(--muted);font-weight:400;">(min. 8 characters)</span></label>'
        f'<input name="new_password" type="password" class="fc2" required minlength="8"></div>'
        f'<div class="fg"><label class="fl">Confirm new password</label>'
        f'<input name="confirm_password" type="password" class="fc2" required minlength="8"></div>'
        f'<button type="submit" class="btn bg-gold">Update password &rarr;</button>'
        f'</form></div></div>'
    )
    return _page("Change Password — Groundwork", body, "account")


@app.route("/share/<token>")
def share_view(token: str):
    entry = _resolve_token(token)
    if not entry:
        body = ('<div style="padding:4rem;text-align:center;">'
                '<h2 style="margin-bottom:1rem;">This link has expired</h2>'
                '<p style="color:var(--muted);">Share links are valid for 24 hours. '
                'Contact your BidEdge adviser for a new link.</p></div>')
        return _page("Link Expired", body, public=True, sidebar=False), 410
    full = ARTEFACTS / entry["filepath"]
    if not full.exists():
        abort(404)
    if request.args.get("dl") or full.suffix == ".pdf":
        return send_file(str(full), as_attachment=bool(request.args.get("dl")))
    label   = entry.get("label", full.name)
    expires = entry.get("expires_at", "")[:16].replace("T", " ")
    content = full.read_text(encoding="utf-8")
    safe    = content.replace('"', "&quot;").replace("'", "&#39;")
    body = (f'<div style="padding:1.25rem;border-bottom:1px solid var(--border);'
            f'background:var(--surf2);display:flex;align-items:center;gap:1rem;">'
            f'<div><div style="font-size:.82rem;font-weight:700;">{label}</div>'
            f'<div style="font-size:.7rem;color:var(--muted);">Shared by BidEdge &middot; Expires {expires} UTC</div></div>'
            f'<a href="?dl=1" class="btn bg-out sm" style="margin-left:auto;">Download</a></div>'
            f'<iframe srcdoc="{safe}" style="width:100%;height:calc(100vh - 80px);border:none;"></iframe>')
    return _page(label, body, public=True, sidebar=False)


@app.route("/groundwork/share", methods=["POST"])
@login_required
def create_share():
    rel_path = request.form.get("filepath", "")
    label    = request.form.get("label", rel_path)
    full = ARTEFACTS / rel_path
    if not full.exists():
        full = OUTPUT_DIR / rel_path
    if not full.exists():
        return {"error": "File not found"}, 404
    try:
        full.resolve().relative_to(ARTEFACTS.resolve())
    except ValueError:
        try:
            full.resolve().relative_to(OUTPUT_DIR.resolve())
        except ValueError:
            abort(403)
    token = _create_share_token(rel_path, label, current_user.id)
    share_url = request.host_url.rstrip("/") + url_for("share_view", token=token)
    return {"url": share_url, "expires_in": f"{TOKEN_TTL_H}h"}


# ── Client: /groundwork ───────────────────────────────────────────────────────

@app.route("/groundwork/home")
@login_required
def gw_dashboard():
    # DB is source of truth for preferred sectors; JSON value is fallback only
    data     = _watchlist_summary(
        preferred_sectors=current_user.preferred_sectors,  # fallback if no DB row
        user_id=current_user.id,                           # DB overrides if row exists
    )
    top      = data.get("top_notices", [])
    total    = data.get("total", 0)
    run_date = data.get("run_date", date.today().isoformat())
    # Effective sectors come from _watchlist_summary which now uses DB as truth
    eff_sectors = data.get("preferred_sectors") or []
    pursuits = len(_list_artefacts(current_user.slug, "*pursuit*.html"))
    comps    = len(_list_artefacts(current_user.slug, "competitor_*.html"))

    # ── Section 6: Claude-powered market signals ──────────────────────────────
    try:
        from market_intelligence import get_stored_signals
        signals = get_stored_signals(current_user.id)
    except Exception as _e:
        logger.warning("market_intelligence unavailable: %s", _e)
        signals = []

    # ── Section 4: Renewal pipeline (three-tier) ─────────────────────────────
    try:
        from renewal_radar import get_renewal_pipeline
        _pipeline = get_renewal_pipeline(user_sectors=eff_sectors or None, days_ahead=365)
    except Exception as _e:
        logger.warning("renewal_radar unavailable: %s", _e)
        _pipeline = {"imminent": [], "approaching": [], "market_sounding": [], "data_note": ""}

    # ── Build notices HTML ────────────────────────────────────────────────────
    intel_map = _intel_sector_map()
    notices_html = ""
    for i, n in enumerate(top, 1):
        sector = n.get("sector_tag") or "other"
        sector_match = (eff_sectors and sector in eff_sectors)
        intel_source = intel_map.get(sector)

        extra_badges = ""
        if sector_match:
            extra_badges += ('<span class="badge" style="background:rgba(42,157,143,.15);'
                             'color:var(--gold);border:1px solid rgba(42,157,143,.35);">'
                             '✓ Matches your sectors</span>')
        if intel_source:
            extra_badges += (f'<span class="badge" style="background:rgba(42,157,143,.1);'
                             f'color:var(--gold);border:1px solid rgba(42,157,143,.3);">'
                             f'⚡ {intel_source}</span>')

        _dash_notice_id = n.get("notice_id", "")
        _dash_gets_ref = (
            f'<div style="font-size:.68rem;color:var(--muted);margin:.05rem 0 .25rem;">'
            f'GETS ref: {_dash_notice_id}</div>'
        ) if _dash_notice_id else ""
        notices_html += (f'<div class="nr">'
                         f'<div class="nrank">{i}</div>'
                         f'<div class="nmain">'
                         f'<div class="ntitle">{n.get("title","")[:80]}</div>'
                         f'<div class="nagency">{n.get("agency","")}</div>'
                         f'{_dash_gets_ref}'
                         f'<div class="nmeta">'
                         f'{_sector_badge(sector)}'
                         f'{_value_badge(n.get("value_band"))}'
                         f'{_dtc_badge(n.get("days_until_close"))}'
                         f'{extra_badges}'
                         f'</div></div>'
                         f'</div>')

    # ── Build market signals HTML (Claude-generated) ──────────────────────────
    priority_css = {"high": "fsh", "medium": "fsm", "low": "fsl"}
    signals_html = ""
    for sig in signals:
        sev = (sig.get("priority") or "low").lower()
        action_text = sig.get("action", "")
        signals_html += (
            f'<div class="fr">'
            f'<span class="fs {priority_css.get(sev,"fsl")}">{sev}</span>'
            f'<span style="line-height:1.5;">'
            f'<strong style="font-size:.83rem;">{sig.get("signal","")}</strong>'
            f'{"<br><span style=font-size:.78rem;color:var(--muted);>" + action_text + "</span>" if action_text else ""}'
            f'</span></div>'
        )
    if not signals_html:
        signals_html = (
            '<p style="color:var(--muted);font-size:.82rem;">'
            'No signals yet — run the intelligence pipeline.</p>'
        )

    # ── Build Contract Expiry Radar HTML ─────────────────────────────────────
    def _expiry_row_html(r: dict, tier: str) -> str:
        val_str   = _fmt_value(r.get("contract_value"))
        supplier  = r.get("supplier_name") or ""
        ed        = r.get("expiry_date")
        ad        = r.get("awarded_date")
        term_src  = r.get("term_source", "inferred")
        dur_mo    = r.get("duration_months")

        # Expiry label
        if ed:
            from datetime import date as _date
            today_d = _date.today()
            try:
                if hasattr(ed, "strftime"):
                    ed_date = ed
                else:
                    from datetime import datetime as _dt
                    ed_date = _dt.strptime(str(ed)[:10], "%Y-%m-%d").date()
                months_left = round((ed_date - today_d).days / 30.44)
                expiry_str  = ed_date.strftime("%-d %b %Y")
                months_label = (
                    "this month" if months_left <= 1
                    else f"in {months_left} month{'s' if months_left != 1 else ''}"
                )
            except Exception:
                expiry_str   = str(ed)[:10]
                months_label = ""
        else:
            expiry_str   = "Unknown"
            months_label = ""

        award_str = ""
        if ad:
            try:
                if hasattr(ad, "strftime"):
                    award_str = ad.strftime("%b %Y")
                else:
                    from datetime import datetime as _dt
                    award_str = _dt.strptime(str(ad)[:10], "%Y-%m-%d").date().strftime("%b %Y")
            except Exception:
                award_str = str(ad)[:7]

        wl_colour = "var(--red)" if tier == "imminent" else "#e07b39"

        # Term source badge
        if term_src == "confirmed":
            term_badge = (
                f'<span style="display:inline-block;padding:.1rem .4rem;border-radius:3px;'
                f'font-size:.62rem;font-weight:700;letter-spacing:.05em;'
                f'background:rgba(78,204,163,.15);color:#4ecca3;border:1px solid rgba(78,204,163,.3);">'
                f'✓ Stated term</span>'
            )
        else:
            dur_label = f"{dur_mo} month typical term" if dur_mo else "estimated term"
            term_badge = (
                f'<span style="display:inline-block;padding:.1rem .4rem;border-radius:3px;'
                f'font-size:.62rem;font-weight:700;letter-spacing:.05em;'
                f'background:rgba(224,123,57,.15);color:#e07b39;border:1px solid rgba(224,123,57,.3);">'
                f'~ Estimated {dur_label}</span>'
            )

        supplier_line = (
            f'<div style="font-size:.72rem;margin-top:.15rem;">'
            f'<span style="color:var(--muted);">Incumbent: </span>'
            f'<span style="color:var(--text);">{supplier[:60]}</span></div>'
        ) if supplier else ""
        award_line = (
            f'<div style="font-size:.72rem;color:var(--muted);">Awarded {award_str}</div>'
        ) if award_str else ""
        return (
            f'<div class="nr" style="padding:.65rem 1.1rem;">'
            f'<div class="nmain">'
            f'<div class="ntitle" style="font-size:.82rem;">{(r.get("title") or "")[:70]}</div>'
            f'<div class="nagency">{(r.get("agency_name") or "")[:55]}</div>'
            f'{supplier_line}'
            f'<div style="margin-top:.3rem;font-size:.76rem;font-weight:600;color:{wl_colour};">'
            f'⏱ Expires {expiry_str}'
            f'{" — " + months_label if months_label else ""}'
            f'</div>'
            f'{award_line}'
            f'<div class="nmeta" style="margin-top:.25rem;">'
            f'{_sector_badge(r.get("sector_tag",""))}'
            f'&ensp;{term_badge}'
            f'</div></div>'
            f'<div style="flex-shrink:0;text-align:right;">'
            f'<div style="font-size:.85rem;font-weight:700;color:var(--gold);">{val_str}</div>'
            f'<div style="font-size:.65rem;color:var(--muted);">contract value</div>'
            f'</div></div>'
        )

    def _expiry_tier_section(label: str, tier_key: str, colour: str) -> str:
        items = _pipeline.get(tier_key, [])
        if not items:
            return ""
        hdr = (
            f'<div style="padding:.5rem 1.1rem;font-size:.68rem;font-weight:700;'
            f'letter-spacing:.07em;text-transform:uppercase;color:{colour};'
            f'border-bottom:1px solid var(--border);background:rgba(0,0,0,.15);">'
            f'{label} — {len(items)} contract{"s" if len(items)!=1 else ""}</div>'
        )
        rows = "".join(_expiry_row_html(r, tier_key) for r in items)
        return hdr + rows

    _imminent_html   = _expiry_tier_section("Expires within 3 months",  "imminent",   "var(--red)")
    _approching_html = _expiry_tier_section("Expires in 3–12 months",   "approaching","#e07b39")

    _renewal_body = _imminent_html + _approching_html
    _data_note = _pipeline.get("data_note", "")

    if not _renewal_body:
        _renewal_body = (
            f'<div style="padding:1rem 1.1rem;font-size:.82rem;color:var(--muted);">'
            + (_data_note or "No contracts with calculable expiry dates found in the next 12 months for your sectors. Try widening your sector preferences.")
            + '</div>'
        )
        _data_note = ""

    _data_note_html = (
        f'<div style="padding:.6rem 1.1rem;font-size:.76rem;color:var(--muted);'
        f'border-top:1px solid var(--border);margin-top:.25rem;">'
        f'{_data_note}</div>'
    ) if _data_note else ""

    renewal_card = (
        f'<div class="card" style="margin-top:1.25rem;">'
        f'<div class="ch"><span class="ct">Contract Expiry Radar</span>'
        f'<span style="font-size:.72rem;color:var(--muted);">'
        f'Contracts approaching estimated re-procurement in the next 12 months</span></div>'
        f'{_renewal_body}{_data_note_html}'
        f'</div>'
    )

    # ── Demo items section (shown only when client has < 3 real artefacts) ──────
    real_artefact_count = (
        len(_list_artefacts(current_user.slug, "*pursuit*.html"))
        + len(_list_artefacts(current_user.slug, "competitor_*.html"))
    )

    demo_section = ""
    if real_artefact_count < 3:
        _manifest_data = _load_demo_manifest()
        _sectors_data  = _manifest_data.get("sectors", {})

        # Pick the best matching sector demo: first preferred sector with demo
        # content, otherwise just the first sector that has items.
        _best_sector = None
        if eff_sectors:
            for _sk in eff_sectors:
                if _sectors_data.get(_sk, {}).get("items"):
                    _best_sector = _sk
                    break
        if not _best_sector:
            for _sk, _sd in _sectors_data.items():
                if _sd.get("items"):
                    _best_sector = _sk
                    break

        if _best_sector:
            _smeta = _DEMO_SECTOR_META.get(_best_sector, {})
            _sdata = _sectors_data[_best_sector]
            _firm  = _sdata.get("firm", {})
            _items = _sdata.get("items", [])

            _type_labels = {
                "pursuit_package":    "Pursuit Package",
                "competitor_profile": "Competitor Profile",
                "watch_brief":        "Watch Brief",
            }
            _type_icons = {
                "pursuit_package": "🎯",
                "competitor_profile": "📊",
                "watch_brief": "📬",
            }
            _demo_rows = ""
            for it in _items:
                _html_path = it.get("html_path", "")
                if not _html_path:
                    continue
                _kind = it.get("type", "")
                _type_name = _type_labels.get(_kind, "Report")
                _icon = _type_icons.get(_kind, "📄")
                _view_url = url_for(
                    "demo_file",
                    filepath=_html_path.replace("output/artefacts/demo/", ""),
                )
                _demo_rows += (
                    f'<div style="display:flex;align-items:center;gap:.75rem;'
                    f'padding:.55rem 0;border-bottom:1px solid var(--border);">'
                    f'<span style="font-size:1rem;">{_icon}</span>'
                    f'<span style="flex:1;font-size:.82rem;color:var(--muted);">{_type_name}</span>'
                    f'<a href="{_view_url}" target="_blank" class="btn bg-out sm" '
                    f'style="font-size:.74rem;padding:.28rem .7rem;">View &rarr;</a>'
                    f'</div>'
                )

            _sector_label = _smeta.get("label", _best_sector.title())
            _firm_name    = _firm.get("name", "Demo Client")
            demo_section = (
                f'<div class="card" style="margin-top:1.25rem;'
                f'border:1px dashed rgba(42,157,143,.35);">'
                f'<div class="ch" style="border-bottom:1px dashed rgba(42,157,143,.2);">'
                f'<span class="ct">Sample Intelligence — {_sector_label}</span>'
                f'<span style="font-size:.68rem;color:var(--gold);background:rgba(42,157,143,.12);'
                f'padding:.15rem .5rem;border-radius:4px;border:1px solid rgba(42,157,143,.3);">'
                f'EXAMPLE</span>'
                f'</div>'
                f'<div style="background:rgba(42,157,143,.05);border-bottom:1px dashed rgba(42,157,143,.2);'
                f'padding:.65rem 1.1rem;font-size:.78rem;color:var(--muted);">'
                f'These examples show what Groundwork generates for your sector. '
                f'Your live intelligence will appear here as it is produced.</div>'
                f'<div class="cb" style="padding:.65rem 1.1rem;">'
                f'<div style="font-size:.72rem;color:var(--muted);margin-bottom:.5rem;">'
                f'Shown as: <strong style="color:var(--text);">{_firm_name}</strong> '
                f'({_sector_label})</div>'
                f'{_demo_rows}'
                f'<div style="padding-top:.65rem;">'
                f'<a href="{url_for("demo")}?sector={_best_sector}" '
                f'style="font-size:.75rem;color:var(--gold);">View full demo for this sector &rarr;</a>'
                f'</div>'
                f'</div></div>'
            )

    wl_link = f'<a href="{url_for("gw_watchlist")}" class="btn bg-out sm">Full watchlist &rarr;</a>'
    prefs_link = (f'<a href="{url_for("onboarding")}" '
                  f'style="font-size:.72rem;color:var(--muted);margin-left:.75rem;">Edit preferences</a>')

    # Sector preference indicator
    if eff_sectors:
        pills = "".join(
            f'<span style="background:rgba(42,157,143,.15);color:var(--gold);'
            f'border:1px solid rgba(42,157,143,.3);border-radius:4px;'
            f'padding:.1rem .4rem;font-size:.7rem;font-weight:600;margin-right:.3rem;">'
            f'{s}</span>'
            for s in eff_sectors
        )
        sector_note = f' &middot; Ranked for: {pills}{prefs_link}'
    else:
        sector_note = (f' &middot; <span style="font-size:.75rem;color:var(--muted);">'
                       f'Sector-neutral ranking</span>{prefs_link}')

    body = (f'<div class="ptitle">Dashboard</div>'
            f'<div class="psub">Good morning, {current_user.name} &middot; {run_date}{sector_note}</div>'
            f'<div class="stats">'
            f'<div class="stat"><div class="sval">{total}</div><div class="slbl">Active opportunities</div></div>'
            f'<div class="stat"><div class="sval">{pursuits}</div><div class="slbl">Pursuit packages</div></div>'
            f'<div class="stat"><div class="sval">{comps}</div><div class="slbl">Competitor profiles</div></div>'
            f'<div class="stat"><div class="sval">{len(signals)}</div><div class="slbl">Market signals</div></div>'
            f'</div>'
            f'<div style="display:grid;grid-template-columns:1.6fr 1fr;gap:1.25rem;">'
            f'<div class="card"><div class="ch"><span class="ct">Top Opportunities</span>{wl_link}</div>'
            f'{notices_html or "<div class=cb><p style=color:var(--muted)>No scored notices yet.</p></div>"}'
            f'</div>'
            f'<div>'
            f'<div class="card"><div class="ch"><span class="ct">Market Signals</span>'
            f'<span style="font-size:.68rem;color:var(--muted);">AI · refreshed daily</span></div>'
            f'<div class="cb" style="padding:.85rem 1rem;">{signals_html}</div></div>'
            f'</div>'
            f'</div>'
            f'{renewal_card}'
            f'{demo_section}')
    return _page("Dashboard — Groundwork", body, "home")


@app.route("/groundwork/watchlist")
@login_required
def gw_watchlist():
    # Fetch active notices pool for watchlist view
    try:
        from preferences import get_user_preferences
        db_prefs = get_user_preferences(current_user.id)
        db_sectors = db_prefs.get("sectors") or []
        if db_sectors:
            preferred_sectors = db_sectors
        else:
            preferred_sectors = list(current_user.preferred_sectors or []) or None

        min_value_nzd = int(db_prefs.get("min_value_nzd") or 0)

        pool = db.fetchall("""
            SELECT r.notice_id, r.title, r.agency, r.source_url, r.close_date,
                   r.category_raw,
                   p.sector_tag, p.days_until_close, p.value_band,
                   p.geographic_scope, p.procurement_stage,
                   e.summary, e.evaluation_weighting, e.red_flags, e.strategic_framing
              FROM parsed_notices p
              JOIN raw_notices r    ON r.notice_id = p.notice_id
              LEFT JOIN enriched_notices e ON e.notice_id = p.notice_id
             WHERE (r.close_date IS NULL OR r.close_date >= CURRENT_DATE)
               AND (p.days_until_close IS NULL OR p.days_until_close >= 0)
             ORDER BY p.days_until_close ASC NULLS LAST
             LIMIT 200
        """)

        pool = [dict(r) for r in pool]

        # Apply minimum value filter: exclude known-value notices below threshold.
        # "unknown" / TBC value_band always passes — we never know the true value.
        if min_value_nzd and min_value_nzd > 0:
            _BAND_MIN_NZD = {
                "under_100k": 0,
                "100k_500k":  100_000,
                "500k_2m":    500_000,
                "2m_10m":     2_000_000,
                "10m_plus":   10_000_000,
            }
            def _passes_min_value(notice):
                band = notice.get("value_band") or "unknown"
                if band in ("unknown", "", None):
                    return True  # TBC — always show
                band_min = _BAND_MIN_NZD.get(band, 0)
                return band_min >= min_value_nzd
            pool = [r for r in pool if _passes_min_value(r)]

        # Sector-preferred notices bubble up first, then urgency+value within each group
        if preferred_sectors:
            matched   = [r for r in pool if (r.get("sector_tag") or "other") in preferred_sectors]
            unmatched = [r for r in pool if (r.get("sector_tag") or "other") not in preferred_sectors]
            matched.sort(key=_notice_sort_key)
            unmatched.sort(key=_notice_sort_key)
            pool = (matched + unmatched)[:100]
        else:
            pool.sort(key=_notice_sort_key)
            pool = pool[:100]

    except Exception as exc:
        logger.error("gw_watchlist: %s", exc)
        pool = []
        preferred_sectors = None

    # Collect distinct sectors for filter pills
    all_sectors = sorted({(r.get("sector_tag") or "other") for r in pool})

    if not pool:
        body = ('<div class="ptitle">Daily Watchlist</div>'
                '<div class="card cb"><p style="color:var(--muted);">No watchlist yet. Run Layer 1 pipeline.</p></div>')
        return _page("Watchlist", body, "watchlist")

    run_date = _nzt_today()

    # ── Sector filter bar HTML ─────────────────────────────────────────────────
    sector_pills = (
        '<button class="sf-pill sf-active" data-sector="all" onclick="sfFilter(this)">All Sectors</button>'
    )
    for s in all_sectors:
        label = _fmt_sector(s)
        sector_pills += (
            f'<button class="sf-pill" data-sector="{s}" onclick="sfFilter(this)">{label}</button>'
        )

    request_base_url = url_for("gw_request")
    filter_bar = (
        # Search bar (above pills)
        f'<div id="sf-search-wrap" style="position:relative;margin-bottom:.7rem;">'
        f'<input id="sf-search" type="search" autocomplete="off"'
        f' placeholder="Search notices by title, agency or sector..."'
        f' oninput="wlSearch(this.value)"'
        f' style="width:100%;padding:.55rem .75rem .55rem 2.1rem;'
        f'border:1px solid var(--border);border-radius:6px;'
        f'background:var(--surf);color:var(--text);font-size:.82rem;font-family:inherit;'
        f'outline:none;transition:border-color .15s;"'
        f' onfocus="this.style.borderColor=\'#2a9d8f\'" onblur="this.style.borderColor=\'\'">'
        f'<span style="position:absolute;left:.65rem;top:50%;transform:translateY(-50%);'
        f'font-size:.85rem;color:var(--muted);pointer-events:none;">&#128269;</span>'
        f'<button id="sf-clear" onclick="wlClearSearch()" title="Clear search"'
        f' style="display:none;position:absolute;right:.5rem;top:50%;transform:translateY(-50%);'
        f'background:none;border:none;font-size:1rem;color:var(--muted);cursor:pointer;'
        f'line-height:1;padding:.1rem .3rem;">&#215;</button>'
        f'</div>'
        # Sort toggle (between search and sector pills)
        f'<div style="display:flex;align-items:center;gap:.45rem;margin-bottom:.55rem;">'
        f'<span style="font-size:.72rem;color:var(--muted);font-weight:600;white-space:nowrap;">Sort:</span>'
        f'<button id="sort-urgency" class="sf-pill sort-pill" onclick="wlSort(\'urgency\')">Urgency</button>'
        f'<button id="sort-recent" class="sf-pill sort-pill" onclick="wlSort(\'recent\')">Recently Added</button>'
        f'</div>'
        # Sector pills (below sort)
        f'<div id="sf-bar" style="display:flex;flex-wrap:wrap;gap:.45rem;'
        f'padding-bottom:.85rem;margin-bottom:.25rem;">'
        f'{sector_pills}'
        f'</div>'
    )

    filter_css = """<style>
/* ── Sector filter pills (portal chrome — keep explicit colours) ── */
.sf-pill{border:1px solid #c8d0d8;background:#eef1f4;color:#1e2d40;
  border-radius:999px;padding:.28rem .85rem;font-size:.75rem;font-weight:600;
  cursor:pointer;transition:background .15s,color .15s,border-color .15s;
  white-space:nowrap;font-family:inherit;}
.sf-pill:hover{border-color:#2a9d8f;color:#2a9d8f;}
.sf-pill.sf-active{background:#2a9d8f;color:#fff;border-color:#2a9d8f;}
@media(max-width:480px){.sf-pill{font-size:.7rem;padding:.22rem .65rem;}}

/* ── Light-document wrapper — overrides dark portal variables for the
      watchlist report area only. Nav/sidebar are unaffected. ── */
#wl-doc{
  background:#f5f0eb;
  border-radius:10px;
  padding:1rem 1.1rem 1.25rem;
  margin-top:.5rem;
  /* Override dark-theme CSS vars for all descendants */
  --surf:#ffffff;
  --surf2:#f0ece6;
  --border:#d8dde3;
  --text:#1e2d40;
  --muted:#556b7d;
  --navy:#1e2d40;
  --navy-l:#eef2f7;
  --gold:#2a9d8f;
  --gold-l:#e0f4f2;
  --red:#c0392b;
  --green:#2e7d4f;
}
/* Card backgrounds and text inside the light doc */
#wl-doc .wl-card{
  background:#ffffff !important;
  border-color:#d8dde3 !important;
  color:#1e2d40;
}

/* ── Bidder cards (output.py classes, now styled for light doc) ── */
#wl-doc .bidder-card{background:#f5f8fa;border:1px solid #d8dde3;
  border-radius:6px;padding:.65rem .85rem;margin-bottom:.5rem;}
#wl-doc .bidder-header{display:flex;align-items:center;gap:.5rem;margin-bottom:.3rem;flex-wrap:wrap;}
#wl-doc .bidder-name{font-size:.83rem;font-weight:700;color:#1e2d40;flex:1;min-width:0;}
#wl-doc .bidder-meta{font-size:.72rem;color:#556b7d;}
#wl-doc .bidder-pills{display:flex;align-items:center;gap:.4rem;flex-wrap:wrap;margin-bottom:.35rem;}
#wl-doc .bidder-pill{font-size:.68rem;font-weight:600;padding:.15rem .45rem;
  border-radius:4px;border:1px solid currentColor;}
#wl-doc .bidder-context{font-size:.78rem;color:#556b7d;line-height:1.55;margin-bottom:.3rem;}
#wl-doc .bidder-reasoning{display:flex;flex-direction:column;gap:.2rem;}
#wl-doc .bidder-bullet{font-size:.74rem;color:#556b7d;line-height:1.4;}
#wl-doc .bidder-src-badge{font-size:.62rem;font-weight:600;padding:.12rem .45rem;
  border-radius:4px;border:1px solid transparent;}
#wl-doc .bidder-src-mbie{background:#eafaf1;color:#2e7d4f;border-color:#a9dfbf;}
#wl-doc .bidder-src-inferred{background:#eef2f7;color:#4a6080;border-color:#b0bcd4;}
</style>"""

    filter_js = """<script>
var _wlActiveSector='all', _wlSearchTerm='';
var _wlSortMode = (sessionStorage.getItem('wl-sort') || 'urgency');

var _BAND_RANK = {'10m_plus':5,'2m_10m':4,'500k_2m':3,'100k_500k':2,'under_100k':1};

function wlSort(mode) {
  _wlSortMode = mode;
  try { sessionStorage.setItem('wl-sort', mode); } catch(e) {}
  document.querySelectorAll('.sort-pill').forEach(function(p){ p.classList.remove('sf-active'); });
  var btn = document.getElementById('sort-' + mode);
  if (btn) btn.classList.add('sf-active');
  var list = document.getElementById('wl-list');
  if (!list) return;
  var cards = Array.from(list.querySelectorAll('.wl-card'));
  cards.sort(function(a, b) {
    if (mode === 'recent') {
      return parseInt(b.getAttribute('data-notice-id') || 0) - parseInt(a.getAttribute('data-notice-id') || 0);
    }
    var da = parseInt(a.getAttribute('data-dtc') || 9999);
    var db = parseInt(b.getAttribute('data-dtc') || 9999);
    if (da !== db) return da - db;
    var ba = _BAND_RANK[a.getAttribute('data-value-band')] || 0;
    var bb = _BAND_RANK[b.getAttribute('data-value-band')] || 0;
    return bb - ba;
  });
  cards.forEach(function(c) { list.appendChild(c); });
  _wlApplyFilters();
}

function _wlApplyFilters(){
  var matched=0;
  document.querySelectorAll('.wl-card').forEach(function(card){
    var sectorOk = _wlActiveSector==='all' || card.getAttribute('data-sector')===_wlActiveSector;
    var searchOk = true;
    if(_wlSearchTerm){
      var hay = (
        (card.getAttribute('data-title')||'') + ' ' +
        (card.getAttribute('data-agency')||'') + ' ' +
        (card.getAttribute('data-sector')||'')
      ).toLowerCase();
      searchOk = hay.indexOf(_wlSearchTerm) !== -1;
    }
    var show = sectorOk && searchOk;
    card.style.display = show ? '' : 'none';
    if(show) matched++;
  });
  var noRes = document.getElementById('wl-no-results');
  if(noRes) noRes.style.display = matched===0 ? 'block' : 'none';
}

function sfFilter(btn){
  document.querySelectorAll('.sf-pill').forEach(function(p){p.classList.remove('sf-active');});
  btn.classList.add('sf-active');
  _wlActiveSector = btn.getAttribute('data-sector');
  _wlApplyFilters();
}

function wlSearch(val){
  _wlSearchTerm = val.trim().toLowerCase();
  var clr = document.getElementById('sf-clear');
  if(clr) clr.style.display = val ? 'block' : 'none';
  _wlApplyFilters();
}

function wlClearSearch(){
  var inp = document.getElementById('sf-search');
  if(inp){ inp.value=''; inp.focus(); }
  _wlSearchTerm='';
  var clr = document.getElementById('sf-clear');
  if(clr) clr.style.display='none';
  _wlApplyFilters();
}

(function(){
  var btn = document.getElementById('sort-' + _wlSortMode);
  if (btn) btn.classList.add('sf-active');
  wlSort(_wlSortMode);
})();
</script>"""

    # ── Batch-fetch all enriched fields and bidders for the pool ─────────────
    try:
        from output import _recommended_actions, _bidder_card
    except Exception as _oe:
        logger.warning("Could not import output helpers: %s", _oe)
        _recommended_actions = lambda item: []
        _bidder_card = lambda b: ""

    # Bidders: one query for all notice IDs, group in Python (avoids N+1)
    notice_ids = [n["notice_id"] for n in pool if n.get("notice_id")]
    bidders_by_notice: dict = {}
    if notice_ids:
        try:
            from canonical_suppliers import deduplicate_bidders
            from bidders import _firm_is_excluded, _notice_is_specialist
            placeholders = ",".join(["%s"] * len(notice_ids))
            raw_bidders = db.fetchall(
                f"""
                SELECT b.notice_id, b.firm_name, b.size, b.strategic_importance,
                       b.intelligence_maturity, b.relevance_score, b.match_type,
                       b.reasoning, b.company_context, b.context_confidence, b.sector
                  FROM bidder_pool b
                 WHERE b.notice_id IN ({placeholders})
                 ORDER BY b.notice_id,
                          CASE b.match_type
                               WHEN 'ach_analysis' THEN 0
                               WHEN 'incumbent_identified' THEN 0
                               ELSE 1 END,
                          b.relevance_score DESC NULLS LAST
                """,
                tuple(notice_ids),
            )
            # Build a notice_ctx lookup for exclusion checking (legacy path only)
            notice_ctx_map: dict = {}
            for n in pool:
                notice_ctx_map[n["notice_id"]] = {
                    "notice_id": n["notice_id"],
                    "title": n.get("title", ""),
                    "agency": n.get("agency", ""),
                    "sector_tag": n.get("sector_tag", ""),
                }
            # Group rows by notice_id; ACH notices bypass exclusion logic
            from collections import defaultdict
            grouped: dict = defaultdict(list)
            for row in raw_bidders:
                grouped[row["notice_id"]].append(dict(row))
            from bidder_intelligence import _ach_relevance_gate
            for nid, rows in grouped.items():
                # If this notice has ACH rows, gate them before use
                ach_rows = [r for r in rows if r.get("match_type") == "ach_analysis"]
                if ach_rows:
                    notice_title_for_gate = (notice_ctx_map.get(nid) or {}).get("title") or ""
                    if _ach_relevance_gate(ach_rows, notice_title_for_gate):
                        bidders_by_notice[nid] = ach_rows[:3]
                        continue
                    # Gate failed — strip ACH rows (stored with empty sector, bypass
                    # exclusion logic) and fall through to Pipeline A rows only
                    rows = [r for r in rows if r.get("match_type") != "ach_analysis"]
                # Legacy path: apply exclusions + dedup on MBIE/CSV rows
                ctx = notice_ctx_map.get(nid, {})
                is_specialist = _notice_is_specialist(ctx) if ctx else False
                filtered = []
                for row in rows:
                    r_sectors = [row.get("sector") or ""]
                    if is_specialist and row.get("match_type") == "csv_inferred":
                        continue
                    if _firm_is_excluded(r_sectors, ctx, row.get("firm_name", "")):
                        continue
                    filtered.append(row)
                deduped = deduplicate_bidders(filtered)
                bidders_by_notice[nid] = deduped[:config.TOP_N_BIDDERS_PER_NOTICE]
        except Exception as _be:
            logger.warning("Watchlist bidder batch-fetch failed: %s", _be)

    # ── Pre-fetch intel sector map once for all cards ─────────────────────────
    intel_map = _intel_sector_map()

    # ── Notice cards ──────────────────────────────────────────────────────────
    cards_html = ""
    for i, n in enumerate(pool, 1):
        sector = n.get("sector_tag") or "other"
        dtc = n.get("days_until_close")
        summary = n.get("summary") or ""

        if dtc is not None and dtc <= 7:
            dtc_badge = f'<span class="badge br">⚡ {dtc}d — URGENT</span>'
        elif dtc is not None and dtc <= 21:
            dtc_badge = f'<span class="badge bk">{dtc} days</span>'
        elif dtc is not None:
            dtc_badge = f'<span class="badge bg">{dtc} days</span>'
        else:
            dtc_badge = '<span class="badge bk">Close TBC</span>'

        tender_badge = _tender_type_badge(
            n.get("procurement_stage") or "", n.get("category_raw") or ""
        )

        sector_match_badge = ""
        if preferred_sectors and sector in preferred_sectors:
            sector_match_badge = ('<span class="badge" style="background:rgba(42,157,143,.15);'
                                  'color:var(--gold);border:1px solid rgba(42,157,143,.35);">'
                                  '✓ Matches your sectors</span>')

        intel_source = intel_map.get(sector)
        strategic_badge = ""
        if intel_source:
            strategic_badge = (f'<span class="badge" style="background:rgba(42,157,143,.1);'
                               f'color:var(--gold);border:1px solid rgba(42,157,143,.3);">'
                               f'⚡ {intel_source}</span>')

        src_link = n.get("source_url", "#")

        # ── Detail sections ───────────────────────────────────────────────────
        # Intelligence summary drives enrichment state
        value_labels = {
            "under_100k": "< $100K", "100k_500k": "$100K\u2013$500K",
            "500k_2m": "$500K\u2013$2M", "2m_10m": "$2M\u2013$10M",
            "10m_plus": "$10M+", "unknown": "Value TBC",
        }
        value_label = value_labels.get(n.get("value_band") or "unknown", "Value TBC")
        close_str = str(n.get("close_date") or "TBC")
        scope = n.get("geographic_scope") or "\u2014"

        if not summary:
            # Compact card for unenriched notices \u2014 show only known facts, no empty placeholders
            detail_html = (
                f'<div style="border-top:1px solid var(--border);margin-top:.75rem;padding-top:.9rem;">'
                f'<div style="display:flex;flex-wrap:wrap;gap:.75rem 1.5rem;'
                f'margin-bottom:.75rem;font-size:.78rem;">'
                f'<div><span style="color:var(--muted);">Value </span>'
                f'<strong>{value_label}</strong></div>'
                f'<div><span style="color:var(--muted);">Close </span>'
                f'<strong>{close_str}</strong></div>'
                f'<div><span style="color:var(--muted);">Scope </span>'
                f'<strong>{scope}</strong></div>'
                f'</div>'
                f'<span style="display:inline-block;font-size:.68rem;color:var(--muted);'
                f'background:rgba(0,0,0,.06);border:1px solid var(--border);border-radius:3px;'
                f'padding:.15rem .5rem;letter-spacing:.03em;">Summary pending</span>'
                f'</div>'
            )
        else:
            # Full intelligence card for enriched notices
            summary_html = f'<p style="margin:0;color:var(--text);line-height:1.6;">{summary}</p>'

            # Strategic framing
            framing = n.get("strategic_framing") or ""
            framing_html = ""
            if framing:
                framing_html = (
                    f'<div style="margin-top:.75rem;padding:.6rem .85rem;'
                    f'background:rgba(42,157,143,.07);border-left:3px solid var(--gold);'
                    f'border-radius:0 6px 6px 0;">'
                    f'<div style="font-size:.7rem;font-weight:700;text-transform:uppercase;'
                    f'letter-spacing:.06em;color:var(--gold);margin-bottom:.25rem;">Strategic framing</div>'
                    f'<div style="font-size:.82rem;color:var(--text);line-height:1.55;">{framing}</div>'
                    f'</div>'
                )

            # Red flags
            red_flags_raw = n.get("red_flags") or ""
            flags = [f.strip() for f in red_flags_raw.split(";") if f.strip()]
            if flags:
                flags_html = "".join(
                    f'<div style="display:flex;gap:.5rem;align-items:flex-start;margin-bottom:.3rem;">'
                    f'<span style="color:#e05555;flex-shrink:0;font-size:.85rem;">\u26a0</span>'
                    f'<span style="font-size:.8rem;color:var(--text);line-height:1.5;">{f}</span></div>'
                    for f in flags
                )
            else:
                flags_html = '<div style="font-size:.8rem;color:var(--muted);font-style:italic;">No red flags identified</div>'

            # Recommended actions
            actions = _recommended_actions(n)
            if actions:
                actions_html = "".join(
                    f'<div style="display:flex;gap:.6rem;align-items:flex-start;margin-bottom:.5rem;">'
                    f'<span style="flex-shrink:0;width:1.35rem;height:1.35rem;border-radius:50%;'
                    f'background:var(--gold);color:#fff;font-size:.68rem;font-weight:700;'
                    f'display:flex;align-items:center;justify-content:center;">{i2+1}</span>'
                    f'<span style="font-size:.8rem;color:var(--text);line-height:1.5;">{a}</span></div>'
                    for i2, a in enumerate(actions)
                )
            else:
                actions_html = '<div style="font-size:.8rem;color:var(--muted);">No actions available.</div>'

            # Likely bidders \u2014 use ACH renderer for ACH rows, legacy for MBIE rows
            notice_id = n.get("notice_id", "")
            bidders = bidders_by_notice.get(notice_id, [])
            if bidders:
                try:
                    from bidder_intelligence import render_ach_card
                    def _render_bidder(b):
                        if b.get("match_type") == "ach_analysis":
                            return render_ach_card(b)
                        return _bidder_card(b)
                    bidders_html = "".join(_render_bidder(b) for b in bidders)
                except Exception:
                    bidders_html = "".join(_bidder_card(b) for b in bidders)
                if 0 < len(bidders) < 3:
                    bidders_html += (
                        f'<div style="font-size:.7rem;color:var(--muted);'
                        f'font-style:italic;margin-top:.5rem;">'
                        f'Limited field \u2014 {len(bidders)} provider(s) identified '
                        f'for this service type in the NZ market.</div>'
                    )
            else:
                bidders_html = '<div style="font-size:.8rem;color:var(--muted);">No bidder data available.</div>'

            detail_html = (
                f'<div style="border-top:1px solid var(--border);margin-top:.75rem;padding-top:.9rem;">'
                # Meta row
                f'<div style="display:flex;flex-wrap:wrap;gap:.75rem 1.5rem;'
                f'margin-bottom:1rem;font-size:.78rem;">'
                f'<div><span style="color:var(--muted);">Value </span>'
                f'<strong>{value_label}</strong></div>'
                f'<div><span style="color:var(--muted);">Close </span>'
                f'<strong>{close_str}</strong></div>'
                f'<div><span style="color:var(--muted);">Scope </span>'
                f'<strong>{scope}</strong></div>'
                f'</div>'
                # Three-column body (stacks on mobile via flex-wrap)
                f'<div style="display:flex;flex-wrap:wrap;gap:1.25rem;align-items:flex-start;">'
                # Left: intelligence summary + framing + red flags
                f'<div style="flex:2;min-width:240px;">'
                f'<div style="font-size:.7rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:.06em;color:var(--muted);margin-bottom:.45rem;">Intelligence summary</div>'
                f'{summary_html}'
                f'{framing_html}'
                f'<div style="font-size:.7rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:.06em;color:var(--muted);margin:.85rem 0 .4rem;">Red flags</div>'
                f'{flags_html}'
                f'</div>'
                # Middle: recommended actions
                f'<div style="flex:1;min-width:200px;">'
                f'<div style="font-size:.7rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:.06em;color:var(--muted);margin-bottom:.45rem;">Recommended actions</div>'
                f'{actions_html}'
                f'</div>'
                # Right: likely bidders
                f'<div style="flex:1;min-width:200px;">'
                f'<div style="font-size:.7rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:.06em;color:var(--muted);margin-bottom:.45rem;">Likely bidders</div>'
                f'{bidders_html}'
                f'</div>'
                f'</div>'  # end body columns
                f'</div>'  # end detail panel
            )

        notice_id = n.get("notice_id", "")
        title_attr = (n.get("title") or "").replace('"', '&quot;')
        agency_attr = (n.get("agency") or "").replace('"', '&quot;')
        gets_ref_html = (
            f'<div style="font-size:.68rem;color:var(--muted);margin-top:.1rem;'
            f'margin-bottom:.35rem;letter-spacing:.01em;">'
            f'GETS ref: {notice_id}</div>'
        ) if notice_id else ""
        req_url = f'{request_base_url}?notice_id={notice_id}'
        cards_html += (
            f'<div class="wl-card" data-sector="{sector}"'
            f' data-title="{title_attr}" data-agency="{agency_attr}"'
            f' data-dtc="{dtc if dtc is not None else 9999}"'
            f' data-notice-id="{n.get("notice_id", "0")}"'
            f' data-value-band="{n.get("value_band") or "unknown"}"'
            f' style="background:var(--surf);border:1px solid var(--border);'
            f'border-radius:8px;padding:.9rem 1.1rem;margin-bottom:.6rem;">'
            f'<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:.75rem;">'
            f'<div style="flex:1;min-width:0;">'
            f'<div style="font-size:.88rem;font-weight:600;color:var(--navy);'
            f'margin-bottom:.25rem;line-height:1.4;">'
            f'<span style="color:var(--muted);font-size:.75rem;font-weight:400;margin-right:.4rem;">#{i}</span>'
            f'{n.get("title","")[:90]}</div>'
            f'<div style="font-size:.75rem;color:var(--muted);margin-bottom:.1rem;">{n.get("agency","")}</div>'
            f'{gets_ref_html}'
            f'<div style="display:flex;flex-wrap:wrap;gap:.35rem;align-items:center;">'
            f'{_sector_badge(sector)}'
            f'{tender_badge}'
            f'{_value_badge(n.get("value_band"))}'
            f'{dtc_badge}'
            f'{sector_match_badge}'
            f'{strategic_badge}'
            f'</div>'
            f'</div>'
            f'<div style="flex-shrink:0;text-align:right;display:flex;flex-direction:column;gap:.4rem;align-items:flex-end;">'
            f'<a href="{src_link}" target="_blank" style="font-size:.72rem;color:var(--muted);'
            f'white-space:nowrap;">GETS &nearr;</a>'
            f'<a href="{req_url}" style="font-size:.7rem;color:var(--gold);white-space:nowrap;'
            f'border:1px solid rgba(42,157,143,.4);border-radius:4px;padding:.15rem .5rem;'
            f'text-decoration:none;transition:background .12s;"'
            f' onmouseover="this.style.background=\'rgba(42,157,143,.1)\'"'
            f' onmouseout="this.style.background=\'\'">Request package &#8250;</a>'
            f'</div>'
            f'</div>'
            f'{detail_html}'
            f'</div>'
        )

    legend_html = """
<div id="wl-key" style="margin-bottom:.6rem;">
  <button onclick="(function(){var b=document.getElementById('wl-key-body');var t=document.getElementById('wl-key-toggle');if(b.style.display==='none'){b.style.display='block';t.textContent='Key ▲';}else{b.style.display='none';t.textContent='Key ▼';}})()"
    style="background:none;border:none;font-size:.72rem;color:var(--muted);cursor:pointer;
           padding:.2rem 0;font-family:inherit;font-weight:600;letter-spacing:.02em;"
    id="wl-key-toggle">Key ▼</button>
  <div id="wl-key-body" style="display:none;background:rgba(0,0,0,.06);border-radius:6px;
       padding:.75rem 1rem;margin-top:.35rem;font-size:.76rem;color:var(--text);line-height:1.8;">
    <div style="display:flex;flex-wrap:wrap;gap:.4rem 2rem;">
      <div><span style="color:var(--red);font-weight:700;">⚡ Urgent badge</span> — closes within 7 days</div>
      <div><span style="background:rgba(42,157,143,.15);color:var(--gold);border:1px solid rgba(42,157,143,.35);
           font-size:.68rem;font-weight:600;padding:.12rem .4rem;border-radius:3px;">✓ Matches your sectors</span>
           — notice matches your saved sector preferences</div>
      <div><span style="background:rgba(42,157,143,.1);color:var(--gold);border:1px solid rgba(42,157,143,.3);
           font-size:.68rem;font-weight:600;padding:.12rem .4rem;border-radius:3px;">⚡ Budget 2026</span>
           — alignment signal with government policy and investment</div>
    </div>
    <div style="margin-top:.5rem;display:flex;flex-wrap:wrap;gap:.4rem 2rem;">
      <div><strong>Value bands:</strong>
        Under $100K &nbsp;·&nbsp; $100K–$500K &nbsp;·&nbsp; $500K–$2M &nbsp;·&nbsp; $2M–$10M &nbsp;·&nbsp; $10M+
      </div>
      <div><strong>GETS ↗</strong> — link to view the full notice on the Government Electronic Tenders Service</div>
    </div>
  </div>
</div>"""

    body = (
        f'{filter_css}'
        f'<div class="ptitle">Daily Watchlist</div>'
        f'<div class="psub">{run_date} &middot; {len(pool)} active notices</div>'
        f'<div id="wl-doc">'
        f'{filter_bar}'
        f'{legend_html}'
        f'<div id="wl-list">{cards_html}</div>'
        f'<div id="wl-no-results" style="display:none;padding:2rem 1rem;text-align:center;'
        f'color:var(--muted);font-size:.85rem;">No notices match your search.</div>'
        f'</div>'
        f'{filter_js}'
    )
    return _page("Watchlist — Groundwork", body, "watchlist")


@app.route("/groundwork/output/<path:filepath>")
@login_required
def serve_output_file(filepath: str):
    full = OUTPUT_DIR / filepath
    try: full.resolve().relative_to(OUTPUT_DIR.resolve())
    except ValueError: abort(403)
    if not full.exists(): abort(404)
    if request.args.get("dl"):
        return send_file(str(full), as_attachment=True)
    return send_file(str(full))


_SHARE_JS = """
<div id="sm" class="modal-bg" style="display:none;" onclick="if(event.target===this)cls()">
  <div class="modal">
    <div class="modal-t">Share link &mdash; valid 24 hours</div>
    <p style="color:var(--muted);font-size:.82rem;margin-bottom:1rem;">
      Anyone with this link can view the file without logging in.</p>
    <div class="cp">
      <input id="su" type="text" class="fc2" readonly>
      <button class="btn bg-gold sm" onclick="cp()">Copy</button>
    </div>
    <div style="margin-top:1.25rem;text-align:right;">
      <button class="btn bg-ghost" onclick="cls()">Close</button>
    </div>
  </div>
</div>
<script>
function share(path,label){
  document.getElementById("sm").style.display="flex";
  fetch("/groundwork/share",{method:"POST",
    headers:{"Content-Type":"application/x-www-form-urlencoded"},
    body:"filepath="+encodeURIComponent(path)+"&label="+encodeURIComponent(label)
  }).then(r=>r.json()).then(d=>{document.getElementById("su").value=d.url||d.error;});
}
function cp(){const e=document.getElementById("su");e.select();document.execCommand("copy");}
function cls(){document.getElementById("sm").style.display="none";}
</script>"""

_QA_AUDIT_JS = """
<script>
function runQaAudit() {
  var btn = document.getElementById('qa-run-btn');
  var box = document.getElementById('qa-results');
  btn.disabled = true;
  btn.textContent = 'Running…';
  box.innerHTML = '<p style="color:var(--muted);font-size:.83rem;padding:.25rem 0;">Running audit checks — this may take 10–20 seconds…</p>';
  fetch('/admin/qa-audit', {method:'POST', headers:{'Content-Type':'application/json'}})
    .then(r => r.json())
    .then(d => { renderQaResults(d); })
    .catch(e => {
      box.innerHTML = '<div class="al al-er">Audit failed: ' + e + '</div>';
    })
    .finally(() => { btn.disabled = false; btn.textContent = 'Run QA Audit'; });
}

function renderQaResults(d) {
  var box = document.getElementById('qa-results');
  if (!d.ok) {
    box.innerHTML = '<div class="al al-er">Error: ' + (d.error || 'unknown') + '</div>';
    return;
  }
  var html = '<div style="font-size:.75rem;color:var(--muted);margin-bottom:1rem;">Last run: ' + d.timestamp + ' &nbsp;|&nbsp; ' + d.notice_count + ' notices checked, ' + d.pursuit_count + ' pursuit packages checked</div>';
  if (d.total_issues === 0) {
    html += '<div class="al al-ok">&#10003; No issues found across ' + d.notice_count + ' notices and ' + d.pursuit_count + ' pursuit packages.</div>';
  } else {
    var grouped = d.grouped;
    var CHECK_LABELS = {
      'Bidder sector mismatch':          {color:'#e05555', icon:'&#9888;'},
      'Overview text missing':           {color:'#e07b39', icon:'&#9888;'},
      'Key dates in text but fields null':{color:'#e07b39', icon:'&#9888;'},
      'Sector classification suspect':   {color:'#d4a017', icon:'&#9888;'},
      'Stale enrichment':                {color:'#d4a017', icon:'&#9888;'},
      'Pursuit: bad client name':        {color:'#e05555', icon:'&#9888;'},
      'Pursuit: incumbent not identified':{color:'#e07b39', icon:'&#9888;'},
      'Pursuit: type/filename mismatch': {color:'#d4a017', icon:'&#9888;'},
    };
    for (var check in grouped) {
      var items = grouped[check];
      var meta = CHECK_LABELS[check] || {color:'#999', icon:'&#9888;'};
      html += '<div style="margin-bottom:1.25rem;">';
      html += '<div style="font-size:.78rem;font-weight:700;color:' + meta.color + ';text-transform:uppercase;letter-spacing:.04em;margin-bottom:.5rem;">';
      html += meta.icon + ' ' + check + ' (' + items.length + ')</div>';
      html += '<table style="width:100%;border-collapse:collapse;font-size:.8rem;">';
      html += '<thead><tr style="color:var(--muted);font-size:.73rem;text-transform:uppercase;border-bottom:1px solid var(--border);">';
      html += '<th style="text-align:left;padding:.25rem .5rem;width:7rem;">ID</th>';
      html += '<th style="text-align:left;padding:.25rem .5rem;width:35%;">Title</th>';
      html += '<th style="text-align:left;padding:.25rem .5rem;">Issue</th></tr></thead><tbody>';
      for (var i = 0; i < items.length; i++) {
        var it = items[i];
        var tr_bg = i % 2 === 0 ? '' : 'background:rgba(0,0,0,.02);';
        html += '<tr style="' + tr_bg + 'border-bottom:1px solid var(--border);">';
        html += '<td style="padding:.35rem .5rem;font-family:monospace;font-size:.74rem;color:var(--muted);">' + it.notice_id + '</td>';
        html += '<td style="padding:.35rem .5rem;">' + _escHtml(it.title.substring(0,60)) + (it.title.length>60?'…':'') + '</td>';
        html += '<td style="padding:.35rem .5rem;color:var(--muted);">' + _escHtml(it.description.substring(0,100)) + (it.description.length>100?'…':'') + '</td>';
        html += '</tr>';
      }
      html += '</tbody></table></div>';
    }
    html += '<div style="margin-top:1rem;padding-top:.75rem;border-top:1px solid var(--border);font-size:.8rem;">';
    html += '<strong>Summary</strong> &nbsp;';
    var parts = [];
    for (var ck in d.summary) { parts.push(ck + ': <strong>' + d.summary[ck] + '</strong>'); }
    html += parts.join(' &nbsp;|&nbsp; ');
    html += ' &nbsp;|&nbsp; <strong>Total: ' + d.total_issues + '</strong></div>';
  }
  box.innerHTML = html;
}

function _escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
</script>"""


_FIX_OPS_JS = """
<script>
// ── Audit Firm Sectors ────────────────────────────────────────────────────────
function runFirmAudit() {
  var btn=document.getElementById('afs-btn'),box=document.getElementById('afs-results');
  btn.disabled=true;btn.textContent='Auditing…';
  box.innerHTML='<p style="color:var(--muted);font-size:.83rem;">Running firm sector audit…</p>';
  fetch('/admin/audit-firm-sectors',{method:'POST',headers:{'Content-Type':'application/json'}})
    .then(function(r){return r.json();}).then(renderFirmAudit)
    .catch(function(e){box.innerHTML='<div class="al al-er">Error: '+e+'</div>';})
    .finally(function(){btn.disabled=false;btn.textContent='Run Audit';});
}
function renderFirmAudit(d) {
  var box=document.getElementById('afs-results');
  if(!d.ok){box.innerHTML='<div class="al al-er">'+(d.error||'Error')+'</div>';return;}
  var html='';
  if(d.misclassified_ict.length===0&&d.misclassified_physical.length===0){
    html='<div class="al al-ok">&#10003; No obvious misclassifications found.</div>';
  } else {
    if(d.misclassified_ict.length>0){
      html+='<div style="font-size:.78rem;font-weight:700;color:#e07b39;margin:.75rem 0 .4rem;">'+d.misclassified_ict.length+' known IT firms with non-ICT sector:</div>';
      html+='<table style="width:100%;border-collapse:collapse;font-size:.8rem;margin-bottom:1rem;"><thead><tr style="color:var(--muted);font-size:.73rem;border-bottom:1px solid var(--border);"><th style="text-align:left;padding:.25rem .5rem;">Firm</th><th style="text-align:left;padding:.25rem .5rem;">Sector</th><th style="text-align:left;padding:.25rem .5rem;">Wins</th></tr></thead><tbody>';
      for(var i=0;i<d.misclassified_ict.length;i++){var r=d.misclassified_ict[i];html+='<tr style="border-bottom:1px solid var(--border);"><td style="padding:.3rem .5rem;">'+_escHtml(r.name)+'</td><td style="padding:.3rem .5rem;color:#e07b39;">'+_escHtml(r.sector)+'</td><td style="padding:.3rem .5rem;color:var(--muted);">'+r.wins+'</td></tr>';}
      html+='</tbody></table>';
    }
    if(d.misclassified_physical.length>0){
      html+='<div style="font-size:.78rem;font-weight:700;color:#e05555;margin:.75rem 0 .4rem;">'+d.misclassified_physical.length+' known construction firms classified as ICT/advisory:</div>';
      html+='<table style="width:100%;border-collapse:collapse;font-size:.8rem;margin-bottom:1rem;"><thead><tr style="color:var(--muted);font-size:.73rem;border-bottom:1px solid var(--border);"><th style="text-align:left;padding:.25rem .5rem;">Firm</th><th style="text-align:left;padding:.25rem .5rem;">Sector</th><th style="text-align:left;padding:.25rem .5rem;">Wins</th></tr></thead><tbody>';
      for(var i=0;i<d.misclassified_physical.length;i++){var r=d.misclassified_physical[i];html+='<tr style="border-bottom:1px solid var(--border);"><td style="padding:.3rem .5rem;">'+_escHtml(r.name)+'</td><td style="padding:.3rem .5rem;color:#e05555;">'+_escHtml(r.sector)+'</td><td style="padding:.3rem .5rem;color:var(--muted);">'+r.wins+'</td></tr>';}
      html+='</tbody></table>';
    }
    html+='<button onclick="applyIctReclassifications()" style="margin-top:.5rem;padding:.35rem .9rem;background:#2A9D8F;color:#fff;border:none;border-radius:4px;font-size:.8rem;cursor:pointer;">Apply ICT Reclassifications</button>';
    html+='<div id="afs-apply-result" style="margin-top:.5rem;font-size:.78rem;"></div>';
  }
  box.innerHTML=html;
}

function applyIctReclassifications() {
  var res=document.getElementById('afs-apply-result');
  res.textContent='Applying…';
  fetch('/admin/apply-ict-reclassifications',{method:'POST',headers:{'Content-Type':'application/json'}})
    .then(function(r){return r.json();})
    .then(function(d){
      if(d.ok){res.style.color='#2A9D8F';res.textContent='Applied '+d.applied+' ICT overrides to DB. Bidder scoring will use updated sectors immediately.';}
      else{res.style.color='#e05555';res.textContent='Error: '+(d.error||'unknown');}
    })
    .catch(function(e){res.style.color='#e05555';res.textContent='Error: '+e;});
}

function resetStuckJobs() {
  var res=document.getElementById('stuck-jobs-result');
  if(res)res.textContent='Resetting…';
  fetch('/admin/reset-stuck-jobs',{method:'POST',headers:{'Content-Type':'application/json'}})
    .then(function(r){return r.json();})
    .then(function(d){
      if(d.ok){
        var msg=d.reset>0?'Reset '+d.reset+' stuck job(s) to Failed.':'No stuck jobs found (nothing running >2h).';
        if(res){res.style.color=d.reset>0?'#2A9D8F':'var(--muted)';res.textContent=msg;}
        else{alert(msg);}
      } else {
        if(res){res.style.color='#e05555';res.textContent='Error: '+(d.error||'unknown');}
        else{alert('Error: '+(d.error||'unknown'));}
      }
    })
    .catch(function(e){if(res){res.style.color='#e05555';res.textContent='Error: '+e;}else{alert('Error: '+e);}});
}

// ── Delete Bad Packages ───────────────────────────────────────────────────────
function previewBadPackages() {
  var btn=document.getElementById('dbp-btn'),box=document.getElementById('dbp-results');
  btn.disabled=true;btn.textContent='Loading…';
  box.innerHTML='<p style="color:var(--muted);font-size:.83rem;">Checking pipeline_outputs…</p>';
  fetch('/admin/delete-bad-packages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'preview'})})
    .then(function(r){return r.json();}).then(renderBadPackages)
    .catch(function(e){box.innerHTML='<div class="al al-er">Error: '+e+'</div>';})
    .finally(function(){btn.disabled=false;btn.textContent='Preview';});
}
function deleteBadPackages() {
  var btn=document.getElementById('dbp-delete-btn'),box=document.getElementById('dbp-results');
  if(!confirm('Permanently delete all packages with bad client names? This cannot be undone.'))return;
  btn.disabled=true;btn.textContent='Deleting…';
  fetch('/admin/delete-bad-packages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'delete'})})
    .then(function(r){return r.json();}).then(function(d){
      if(d.ok){box.innerHTML='<div class="al al-ok">Deleted '+d.deleted+' package'+(d.deleted!==1?'s':'')+' with bad client names.</div>';}
      else{box.innerHTML='<div class="al al-er">Error: '+(d.error||'unknown')+'</div>';}
    }).catch(function(e){box.innerHTML='<div class="al al-er">Error: '+e+'</div>';});
}
function renderBadPackages(d) {
  var box=document.getElementById('dbp-results');
  if(!d.ok){box.innerHTML='<div class="al al-er">'+(d.error||'Error')+'</div>';return;}
  if(d.count===0){box.innerHTML='<div class="al al-ok">&#10003; No packages with bad client names found.</div>';return;}
  var html='<div style="font-size:.78rem;font-weight:700;color:#e05555;margin-bottom:.5rem;">'+d.count+' packages with bad client names</div>';
  html+='<table style="width:100%;border-collapse:collapse;font-size:.8rem;margin-bottom:1rem;"><thead><tr style="color:var(--muted);font-size:.73rem;border-bottom:1px solid var(--border);"><th style="text-align:left;padding:.25rem .5rem;">ID</th><th style="text-align:left;padding:.25rem .5rem;">Type</th><th style="text-align:left;padding:.25rem .5rem;">Client</th><th style="text-align:left;padding:.25rem .5rem;">Notice</th><th style="text-align:left;padding:.25rem .5rem;">Date</th></tr></thead><tbody>';
  for(var i=0;i<d.packages.length;i++){
    var p=d.packages[i];
    html+='<tr style="border-bottom:1px solid var(--border);"><td style="padding:.3rem .5rem;color:var(--muted);">'+p.id+'</td><td style="padding:.3rem .5rem;">'+_escHtml(p.output_type)+'</td><td style="padding:.3rem .5rem;color:#e05555;">'+_escHtml(p.client_name)+'</td><td style="padding:.3rem .5rem;font-family:monospace;font-size:.73rem;color:var(--muted);">'+_escHtml(p.notice_id)+'</td><td style="padding:.3rem .5rem;color:var(--muted);">'+_escHtml(p.run_date)+'</td></tr>';
  }
  html+='</tbody></table>';
  html+='<button id="dbp-delete-btn" class="btn" style="background:#e05555;color:#fff;border:none;padding:.35rem .9rem;border-radius:5px;font-size:.8rem;cursor:pointer;" onclick="deleteBadPackages()">Delete All ('+d.count+')</button>';
  box.innerHTML=html;
}
</script>"""

_BACKFILL_OVERVIEW_STATUS: dict = {
    "running": False, "done": 0, "total": 0, "errors": 0, "started": None,
}


def _artefact_page(
    title: str,
    pattern: str,
    empty_msg: str,
    active: str,
    db_output_types: list = None,
    allow_delete: bool = False,
) -> str:
    files = _list_artefacts(current_user.slug, pattern, db_output_types)
    if not files:
        body = (f'<div class="ptitle">{title}</div>'
                f'<div class="card cb"><p style="color:var(--muted);">{empty_msg}</p>'
                f'<a href="{url_for("gw_request")}" class="btn bg-gold" style="margin-top:1rem;">Request one &rarr;</a>'
                f'</div>')
        return _page(title, body, active)

    cards = ""
    for f in files:
        view_url = url_for("serve_artefact_file",
                           client_slug=current_user.slug, filepath=f["url_path"])
        pdf_btn = ""
        if f.get("has_pdf"):
            pdf_path = f["url_path"].rsplit(".", 1)[0] + ".pdf"
            pdf_url  = url_for("serve_artefact_file",
                               client_slug=current_user.slug, filepath=pdf_path)
            pdf_btn = f'<a href="{pdf_url}" target="_blank" class="btn bg-out sm">PDF</a>'
        db_id = f.get("db_id")
        fname = f.get("filename", "")
        slug  = current_user.slug
        card_id = f"pcard-{db_id}" if db_id else f"pcard-{fname}"
        del_btn = ""
        if allow_delete and (db_id or fname):
            db_id_js = db_id if db_id is not None else "null"
            del_btn = (
                f'<button class="btn bg-ghost sm" style="color:#e05555;" '
                f'onclick="deletePursuit({db_id_js},\'{_safe(fname)}\',\'{_safe(slug)}\',\'{card_id}\')">'
                f'Delete</button>'
            )
        cards += (f'<div class="fc" id="{card_id}">'
                  f'<div class="fct">{f["name"]}</div>'
                  f'<div class="fcd">{f["date"]} &middot; {f["size_kb"]}KB</div>'
                  f'<div class="fca">'
                  f'<a href="{view_url}" target="_blank" class="btn bg-gold sm">View</a>'
                  f'<a href="{view_url}?dl=1" class="btn bg-out sm">Download</a>'
                  f'{pdf_btn}'
                  f'<button class="btn bg-ghost sm" '
                  f'onclick="share(\'{f["rel_path"]}\',\'{f["name"]}\')">Share &#128279;</button>'
                  f'{del_btn}'
                  f'</div></div>')

    delete_js = ""
    if allow_delete:
        delete_js = """
<script>
function deletePursuit(id, filename, slug, cardId) {
  if (!confirm('Delete this pursuit package? This cannot be undone.')) return;
  fetch('/groundwork/pursuits/delete', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id: id, filename: filename, client_slug: slug})
  }).then(r => r.json()).then(d => {
    if (d.ok) {
      var el = document.getElementById(cardId);
      if (el) el.remove();
    } else {
      alert('Delete failed: ' + (d.error || 'unknown error'));
    }
  }).catch(e => alert('Delete failed: ' + e));
}
</script>"""

    body = (f'<div class="ptitle">{title}</div>'
            f'<div class="psub">{len(files)} file{"s" if len(files)!=1 else ""}</div>'
            f'<div class="fgrid">{cards}</div>'
            f'{_SHARE_JS}{delete_js}')
    return _page(title, body, active)


@app.route("/groundwork/help")
@login_required
def gw_help():
    body = """
<style>
.help-wrap{max-width:760px;}
.help-section{margin-bottom:2.75rem;}
.help-h2{font-size:1.15rem;font-weight:800;color:var(--text);margin:0 0 .6rem;
  padding-bottom:.45rem;border-bottom:1px solid var(--border);letter-spacing:-.01em;}
.help-p{font-size:.88rem;color:var(--muted);line-height:1.75;margin:.5rem 0;}
.help-p strong{color:var(--text);}
.help-ul{list-style:none;padding:0;margin:.5rem 0;}
.help-ul li{font-size:.88rem;color:var(--muted);line-height:1.7;padding:.25rem 0 .25rem 1.4rem;
  position:relative;}
.help-ul li::before{content:"→";position:absolute;left:0;color:var(--gold);font-weight:700;}
.help-tip{background:rgba(42,157,143,.07);border:1px solid rgba(42,157,143,.2);
  border-left:3px solid var(--gold);border-radius:6px;padding:.75rem 1rem;
  font-size:.84rem;color:var(--muted);line-height:1.7;margin:.75rem 0;}
.help-tip strong{color:var(--text);}
</style>
<div class="ptitle">Help Guide</div>
<div class="psub">How to get the most from Groundwork</div>
<div class="help-wrap">

  <div class="help-section">
    <div class="help-h2">What Groundwork does</div>
    <p class="help-p">Groundwork monitors every NZ government procurement notice published each day,
    enriches each one with a decade of contract award history, and surfaces who's likely bidding,
    who the incumbent is, and whether the opportunity is worth your team's time.
    The goal is straightforward: replace the hours you spend reading GETS with a daily briefing
    you can act on in minutes.</p>
  </div>

  <div class="help-section">
    <div class="help-h2">The Daily Watchlist</div>
    <p class="help-p">The <strong>Watchlist</strong> is your daily briefing. Each morning it shows
    the most strategically relevant open notices for your sectors, ranked by opportunity score.</p>
    <ul class="help-ul">
      <li><strong>Sector filter pills</strong> — click any sector tag to show only notices in that
      category. Click it again (or click All) to clear the filter. Your sector preferences control
      which sectors appear by default.</li>
      <li><strong>Search bar</strong> — type any word to filter cards in real time by title, agency,
      or sector. The search works on top of any active sector filter.</li>
      <li><strong>Urgency badges</strong> — a red or amber badge showing days until close means the
      notice is closing soon. Green means plenty of time. No badge means the close date isn't
      published yet.</li>
      <li><strong>GETS ref</strong> — the reference number for the notice on the GETS portal. Click
      the <em>GETS ↗</em> link on any card to open the original notice in a new tab.</li>
      <li><strong>Request package →</strong> — click this on any card to go straight to the
      Request form with the notice pre-filled.</li>
    </ul>
  </div>

  <div class="help-section">
    <div class="help-h2">Requesting a Pursuit Package</div>
    <p class="help-p">A <strong>pursuit package</strong> is a full bid intelligence brief for a
    specific opportunity. It covers: a win position assessment, the likely field of competitors,
    the agency's procurement history and buying patterns, critical risks, and a set of recommended
    actions before you commit to bidding.</p>
    <ul class="help-ul">
      <li><strong>When to use it</strong> — as soon as you identify a notice worth pursuing.
      The earlier you request it, the more time you have to act on the intelligence before close.</li>
      <li><strong>How to request one</strong> — click <em>Request package →</em> on a watchlist card,
      or go to <em>Request</em> in the sidebar and enter the GETS notice ID manually.</li>
      <li><strong>What you receive</strong> — a formatted HTML document delivered to your
      Pursuits library, typically within 24 hours (urgent requests within 4 hours).</li>
    </ul>
    <div class="help-tip"><strong>Tip:</strong> Don't wait until the day before close to request a
    pursuit package. Request it the moment the opportunity looks relevant — you need time to act on
    the recommended actions.</div>
  </div>

  <div class="help-section">
    <div class="help-h2">Requesting a Competitor Profile</div>
    <p class="help-p">A <strong>competitor profile</strong> is an evidence-based summary of a
    specific firm's NZ government contract history — what they win, who they win it from, how often,
    and at what contract values.</p>
    <ul class="help-ul">
      <li><strong>When to use it</strong> — before a major bid when you keep seeing the same firm
      shortlisted, or when you want to understand a market entrant before they show up in your
      sectors.</li>
      <li><strong>How to request one</strong> — go to <em>Request</em> in the sidebar, select
      <em>Competitor Profile</em>, and name the firm. Include any context you have (sector, likely
      regions, known client relationships).</li>
    </ul>
  </div>

  <div class="help-section">
    <div class="help-h2">The Weekly Watch Brief</div>
    <p class="help-p">The <strong>Watch Brief</strong> is a weekly summary of market activity in your
    sectors — significant notices that opened, contracts that were awarded, and any strategic signals
    worth noting. It arrives on Monday morning and is archived in your <em>Watch Briefs</em>
    library.</p>
    <p class="help-p">It's designed to be read in under five minutes. If something in it warrants
    deeper attention, it links directly to the relevant notices or pursuit packages.</p>
  </div>

  <div class="help-section">
    <div class="help-h2">Setting your sector preferences</div>
    <p class="help-p">Your sector preferences control which notices appear in your watchlist and
    which sectors are highlighted throughout the platform. To update them:</p>
    <ul class="help-ul">
      <li>Go to your account settings (accessible from the top nav).</li>
      <li>Select the sectors most relevant to your business. You can choose multiple.</li>
      <li>Your watchlist will update the next time it's generated (usually overnight).</li>
    </ul>
    <div class="help-tip"><strong>Set your sectors on first login.</strong> Until you do,
    the watchlist shows a broad default set that may not match your work.</div>
  </div>

  <div class="help-section">
    <div class="help-h2">Getting the most from Groundwork</div>
    <ul class="help-ul">
      <li><strong>Set your sectors on day one.</strong> The platform is only as relevant as your
      sector settings. Spend two minutes on this and everything else improves immediately.</li>
      <li><strong>Check the watchlist every morning.</strong> Opportunities move fast. A notice that
      opens today may close in 10 days. Seeing it on day one gives you options; seeing it on day
      eight doesn't.</li>
      <li><strong>Request pursuit packages early.</strong> The intelligence is most useful when you
      still have time to act — confirm capability evidence, seek clarification, adjust your
      approach. Requesting it the day before close is too late.</li>
      <li><strong>Use competitor profiles before major bids, not after you've lost.</strong>
      Understanding a competitor's win pattern before you price gives you a genuine advantage.
      Post-loss analysis is useful, but it's the wrong order.</li>
    </ul>
  </div>

</div>
"""
    return _page("Help — Groundwork", body, "help")


@app.route("/groundwork/pursuits")
@login_required
def gw_pursuits():
    if current_user.is_admin_user:
        return _admin_pursuits_page()
    return _artefact_page(
        "Pursuit Packages", "*pursuit_package*.html",
        "No pursuit packages yet for your account.", "pursuits",
        db_output_types=["pursuit_package", "pursuit_package_full"],
        allow_delete=True,
    )


def _admin_pursuits_page() -> str:
    """Admin view: all pursuit packages across all clients, newest first."""
    import traceback as _tb
    try:
        rows = db.fetchall(
            """
            SELECT id, filename, run_date, client_slug, notice_id, output_type,
                   client_name
              FROM pipeline_outputs
             WHERE output_type IN ('pursuit_package', 'pursuit_package_full')
               AND content IS NOT NULL
             ORDER BY run_date DESC, created_at DESC
             LIMIT 100
            """
        )
    except Exception as exc:
        _trace = _tb.format_exc()
        logger.warning("_admin_pursuits_page query failed: %s", exc)
        body = (
            f'<div class="ptitle">All Pursuit Packages (Admin)</div>'
            f'<div class="al al-er" style="white-space:pre-wrap;font-family:monospace;font-size:.75rem;">'
            f'<b>DB error (admin diagnostic):</b>\n{_safe(_trace)}'
            f'</div>'
        )
        return _page("Pursuits — Admin", body, "pursuits")

    if not rows:
        body = (
            f'<div class="ptitle">All Pursuit Packages (Admin)</div>'
            f'<div class="card cb"><p style="color:var(--muted);">No packages in pipeline_outputs yet.</p></div>'
        )
        return _page("Pursuits — Admin", body, "pursuits")

    cards = ""
    for row in rows:
        try:
            row_id      = row["id"]
            filename    = row["filename"]
            run_date    = str(row["run_date"])
            slug        = row.get("client_slug") or "unknown"
            cname       = row.get("client_name") or slug
            notice_id   = row.get("notice_id") or "—"
            full_label  = " (Full)" if row.get("output_type") == "pursuit_package_full" else ""
            view_url    = f"/groundwork/files/{slug}/{run_date}/{filename}"
            cards += (
                f'<div class="fc" id="pcard-{row_id}">'
                f'<div class="fct">{_safe(cname)}{full_label}</div>'
                f'<div class="fcd">{run_date} &middot; notice {_safe(notice_id)}</div>'
                f'<div class="fca">'
                f'<a href="{view_url}" target="_blank" class="btn bg-gold sm">View</a>'
                f'<a href="{view_url}?dl=1" class="btn bg-out sm">Download</a>'
                f'<button class="btn bg-ghost sm" style="color:#e05555;" '
                f'onclick="deletePursuit({row_id},\'{_safe(filename)}\',\'{_safe(slug)}\')">Delete</button>'
                f'</div></div>'
            )
        except Exception as _row_exc:
            logger.warning("_admin_pursuits_page: skipping row due to error: %s", _row_exc)

    delete_js = """
<script>
function deletePursuit(id, filename, slug) {
  if (!confirm('Delete this pursuit package? This cannot be undone.')) return;
  fetch('/groundwork/pursuits/delete', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id: id, filename: filename, client_slug: slug})
  }).then(r => r.json()).then(d => {
    if (d.ok) {
      var el = document.getElementById('pcard-' + id);
      if (el) el.remove();
    } else {
      alert('Delete failed: ' + (d.error || 'unknown error'));
    }
  }).catch(e => alert('Delete failed: ' + e));
}
</script>"""

    body = (
        f'<div class="ptitle">All Pursuit Packages (Admin)</div>'
        f'<div class="psub">{len(rows)} package{"s" if len(rows)!=1 else ""} across all clients</div>'
        f'<div class="fgrid">{cards}</div>'
        f'{delete_js}'
    )
    return _page("Pursuits — Admin", body, "pursuits")


@app.route("/groundwork/pursuits/delete", methods=["POST"])
@login_required
def gw_pursuits_delete():
    from flask import jsonify
    data = request.get_json(silent=True) or {}
    row_id    = data.get("id")
    filename  = data.get("filename", "").strip()
    client_slug = data.get("client_slug", "").strip()

    if not filename or not client_slug:
        return jsonify({"ok": False, "error": "Missing filename or client_slug"}), 400

    # Non-admin users may only delete their own packages
    if not current_user.is_admin_user and client_slug != current_user.slug:
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    try:
        if row_id:
            db.execute(
                "DELETE FROM pipeline_outputs WHERE id = %s",
                (row_id,),
            )
        else:
            db.execute(
                """DELETE FROM pipeline_outputs
                    WHERE filename = %s AND client_slug = %s
                      AND output_type IN ('pursuit_package', 'pursuit_package_full')""",
                (filename, client_slug),
            )
        logger.info("Deleted pursuit package: id=%s filename=%s slug=%s by %s",
                    row_id, filename, client_slug, current_user.username)
        return jsonify({"ok": True})
    except Exception as exc:
        logger.error("gw_pursuits_delete failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/groundwork/competitors")
@login_required
def gw_competitors():
    return _artefact_page("Competitor Profiles", "competitor_*.html",
                          "No competitor profiles yet.", "competitors")


@app.route("/groundwork/briefs")
@login_required
def gw_briefs():
    return _artefact_page("Weekly Watch Briefs", "watch_brief_*.html",
                          "No watch briefs yet.", "briefs")


@app.route("/groundwork/files/<client_slug>/<path:filepath>")
@login_required
def serve_artefact_file(client_slug: str, filepath: str):
    if client_slug != current_user.slug and not current_user.is_admin_user:
        abort(403)
    full = ARTEFACTS / client_slug / filepath
    try: full.resolve().relative_to(ARTEFACTS.resolve())
    except ValueError: abort(403)

    if full.exists():
        if request.args.get("dl"):
            return send_file(str(full), as_attachment=True)
        return send_file(str(full))

    # Local file missing (Railway restart) — try Supabase Storage
    import storage as _storage
    filename = Path(filepath).name
    is_pdf = filename.lower().endswith(".pdf")
    content_type = "application/pdf" if is_pdf else "text/html; charset=utf-8"

    # Derive storage path: strip date segment if present (e.g. pursuits/slug/YYYY-MM-DD/file → pursuits/slug/file)
    parts = filepath.replace("\\", "/").split("/")
    storage_candidates = [
        f"pursuits/{client_slug}/{filename}",
        f"briefs/{client_slug}/{filename}",
        f"competitors/{client_slug}/{filename}",
        f"demo/{filename}",
        f"watchlist/{filename}",
    ]
    for sp in storage_candidates:
        data = _storage.download_file(sp)
        if data:
            if request.args.get("dl"):
                from flask import send_file as _sf
                import io
                return _sf(io.BytesIO(data), as_attachment=True,
                           download_name=filename, mimetype=content_type)
            from flask import Response
            return Response(data, content_type=content_type)

    # Last resort: check pipeline_outputs DB content
    row = db.fetchone(
        "SELECT content, content_bytes FROM pipeline_outputs WHERE filename = %s"
        " ORDER BY created_at DESC LIMIT 1",
        (filename,),
    )
    if row:
        if row.get("content_bytes"):
            from flask import Response
            return Response(bytes(row["content_bytes"]), content_type=content_type)
        if row.get("content"):
            from flask import Response
            return Response(row["content"], content_type="text/html; charset=utf-8")

    abort(404)


@app.route("/groundwork/request", methods=["GET", "POST"])
@login_required
def gw_request():
    # Pre-fill notice_id from query string (e.g. ?notice_id=32705858)
    prefill_notice_id = request.args.get("notice_id", "").strip()
    sent = False
    ok_msg = ""

    if request.method == "POST":
        rtype      = request.form.get("type", "pursuit")
        details    = request.form.get("details", "")
        notice     = request.form.get("notice_id", "").strip()
        prio       = request.form.get("priority", "normal")
        client_org = request.form.get("client_org", "").strip()

        # ── Pursuit package: automated generation ─────────────────────────────
        if rtype == "pursuit" and notice and not client_org:
            ok_msg = '<div class="al al-er">Please enter your organisation name — this is required to generate the analysis.</div>'
        elif rtype == "pursuit" and notice:
            try:
                import pursuit_worker
                import mailer as _mailer

                # Check urgency: notices closing within 7 days → immediate thread
                dtc = pursuit_worker._days_until_close(notice)
                urgent = (dtc is not None and dtc <= 7) or (prio == "urgent")

                # 1. Save request to DB
                req_id = pursuit_worker.create_request(
                    client_id=current_user.id,
                    notice_id=notice,
                    request_type="pursuit",
                    details=details,
                    priority="urgent" if urgent else prio,
                )

                # 2. Fetch notice title for confirmation email
                nrow = db.fetchone(
                    "SELECT title FROM raw_notices WHERE notice_id = %s", (notice,)
                )
                notice_title = (nrow or {}).get("title") or notice

                # 3. Send confirmation email to client (async — never blocks request)
                if current_user.email:
                    import threading as _thr
                    _thr.Thread(
                        target=_mailer.send_request_confirmation,
                        kwargs=dict(
                            client_name=current_user.name,
                            client_email=current_user.email,
                            notice_id=notice,
                            notice_title=notice_title,
                            urgent=urgent,
                        ),
                        daemon=True, name="mailer-req-confirm",
                    ).start()

                # 4. Notify admin (async)
                _mailer.notify_admin_new_request(
                    client_name=current_user.name,
                    client_id=current_user.id,
                    notice_id=notice,
                    request_type=rtype,
                    priority="urgent" if urgent else prio,
                    details=details,
                )

                # 5. Dispatch background generation
                portal_base = request.host_url.rstrip("/")
                portal_url = portal_base + url_for("gw_pursuits")
                pursuit_worker.dispatch(
                    req_id=req_id,
                    client_id=current_user.id,
                    client_name=client_org,
                    client_email=current_user.email or "",
                    notice_id=notice,
                    preferred_sectors=list(current_user.preferred_sectors or []),
                    artefact_slug=current_user.slug,
                    portal_url=portal_url,
                    immediate=urgent,
                )

                eta = "within the hour" if urgent else "within 24 hours"
                ok_msg = (
                    f'<div class="al al-ok">'
                    f'Your pursuit package is being generated — '
                    f'you\'ll receive an email when it\'s ready ({eta}). '
                    f'It will appear in your <a href="{url_for("gw_pursuits")}" '
                    f'style="color:inherit;font-weight:700;">Pursuits library</a>.'
                    f'</div>'
                )
                sent = True

            except Exception as exc:
                logger.exception("gw_request pursuit dispatch failed: %s", exc)
                ok_msg = (
                    '<div class="al al-er">Request submitted but automated generation '
                    'could not start — the BidEdge team has been notified and will '
                    'process it manually.</div>'
                )
                # Fallback: email admin as before
                import mailer as _mailer
                _mailer.notify_admin_new_request(
                    client_name=current_user.name,
                    client_id=current_user.id,
                    notice_id=notice,
                    request_type=rtype,
                    priority=prio,
                    details=f"{details}\n\n[AUTOMATION FAILED: {exc}]",
                )
                sent = True

        elif rtype == "competitor":
            # ── Competitor profile: automated generation ──────────────────────
            firm_name       = request.form.get("firm_name", "").strip()
            comp_context    = request.form.get("comp_context", "").strip()
            sector_context  = request.form.get("sector_context", "").strip()
            comp_notice_id  = request.form.get("comp_notice_id", "").strip()
            client_org_comp = request.form.get("client_org", "").strip()
            if firm_name and not client_org_comp:
                ok_msg = '<div class="al al-er">Please enter your organisation name — this is required for the competitive analysis.</div>'
            elif firm_name and not sector_context and not comp_notice_id:
                ok_msg = (
                    '<div class="al al-er">Please supply either a GETS Notice ID (recommended) or '
                    'a procurement sector — at least one is required to frame the competitive analysis.</div>'
                )
            elif firm_name:
                try:
                    import threading as _thr
                    import mailer as _mailer

                    # Save to competitor_requests table
                    try:
                        comp_req_id = db.fetchone(
                            "INSERT INTO competitor_requests "
                            "(user_id, firm_name, context, status, requested_at) "
                            "VALUES (%s, %s, %s, 'pending', NOW()) RETURNING id",
                            (current_user.id, firm_name, comp_context),
                        )
                        comp_req_id = (comp_req_id or {}).get("id")
                    except Exception as exc:
                        logger.warning("Could not save competitor_request to DB: %s", exc)
                        comp_req_id = None

                    portal_base = request.host_url.rstrip("/")
                    comp_portal_url = portal_base + url_for("gw_competitors")

                    def _generate_competitor(req_id, firm, context, sector, client_id,
                                             client_name, client_email, slug, portal_url,
                                             notice_id=None):
                        try:
                            if req_id:
                                db.execute(
                                    "UPDATE competitor_requests SET status='generating' WHERE id=%s",
                                    (req_id,))
                            from competitor_profile import generate_competitor_profile
                            import config as _cfg
                            from pathlib import Path as _Path
                            output_dir = _Path(_cfg.ARTEFACTS_DIR) / slug
                            output_dir.mkdir(parents=True, exist_ok=True)
                            profile_path = generate_competitor_profile(
                                competitor_name=firm,
                                client_name=client_name,
                                sector_context=sector,
                                output_dir=output_dir,
                                notice_id=notice_id,
                            )
                            if req_id:
                                try:
                                    rel = str(profile_path.relative_to(_Path(_cfg.ARTEFACTS_DIR)))
                                except ValueError:
                                    rel = str(profile_path)
                                db.execute(
                                    "UPDATE competitor_requests SET status='complete', "
                                    "artefact_path=%s, completed_at=NOW() WHERE id=%s",
                                    (rel, req_id))
                            # Build direct file URL (slug-stripped path for serve_artefact_file)
                            from urllib.parse import urlparse as _urlparse
                            _parsed = _urlparse(portal_url)
                            _base = f"{_parsed.scheme}://{_parsed.netloc}"
                            direct_url = f"{_base}/groundwork/files/{slug}/{profile_path.name}"
                            import mailer as _m
                            if client_email:
                                _m.send_competitor_profile_ready(
                                    client_name=client_name,
                                    client_email=client_email,
                                    firm_name=firm,
                                    portal_url=direct_url,
                                )
                        except Exception as exc:
                            logger.exception("Competitor generation failed for %s: %s", firm, exc)
                            if req_id:
                                try:
                                    db.execute(
                                        "UPDATE competitor_requests SET status='failed' WHERE id=%s",
                                        (req_id,))
                                except Exception:
                                    pass
                            import mailer as _m
                            _m.send_admin_only(
                                subject=f"[Groundwork] Competitor profile FAILED — {firm}",
                                html=f"<p>Client: {client_name}<br>Firm: {firm}</p><pre>{exc}</pre>",
                            )

                    _thr.Thread(
                        target=_generate_competitor,
                        args=(comp_req_id, firm_name, comp_context, sector_context,
                              current_user.id, client_org_comp, current_user.email,
                              current_user.slug, comp_portal_url),
                        kwargs={"notice_id": comp_notice_id or None},
                        daemon=True, name=f"comp-{firm_name[:20]}",
                    ).start()

                    ok_msg = (
                        f'<div class="al al-ok">'
                        f'Competitor profile for <strong>{firm_name}</strong> is being generated — '
                        f'you\'ll receive an email when it\'s ready. '
                        f'It will appear in your <a href="{url_for("gw_competitors")}" '
                        f'style="color:inherit;font-weight:700;">Competitors library</a>.'
                        f'</div>'
                    )
                    sent = True
                except Exception as exc:
                    logger.exception("gw_request competitor dispatch failed: %s", exc)
                    ok_msg = '<div class="al al-er">Request submitted — BidEdge will be in touch.</div>'
                    sent = True
            else:
                ok_msg = '<div class="al al-er">Please enter the competitor firm name.</div>'

        else:
            # ── Other request types: email admin ──────────────────────────────
            import mailer as _mailer
            _mailer.notify_admin_new_request(
                client_name=current_user.name,
                client_id=current_user.id,
                notice_id=notice,
                request_type=rtype,
                priority=prio,
                details=details,
            )
            ok_msg = '<div class="al al-ok">Request submitted — BidEdge will be in touch.</div>'
            sent = True

    # ── Build forms ────────────────────────────────────────────────────────────
    # Determine which tab is active (from query param or form submission)
    active_tab = request.args.get("tab", "pursuit")
    if request.method == "POST":
        rtype_post = request.form.get("type", "pursuit")
        if rtype_post == "competitor":
            active_tab = "competitor"

    # Notice ID field — read-only if pre-filled from watchlist
    if prefill_notice_id:
        notice_id_field = (
            f'<div class="fg"><label class="fl">GETS Notice ID</label>'
            f'<input name="notice_id" class="fc2" value="{prefill_notice_id}" readonly'
            f' style="background:var(--surf2,rgba(255,255,255,.04));color:var(--muted);'
            f'cursor:not-allowed;">'
            f'<div class="fh">Pre-filled from your watchlist. Not editable.</div></div>'
        )
    else:
        notice_id_field = (
            f'<div class="fg"><label class="fl">GETS Notice ID</label>'
            f'<input name="notice_id" class="fc2" placeholder="e.g. 34060392">'
            f'<div class="fh">Find the notice ID in the GETS URL or your watchlist.</div></div>'
        )

    def _tab_btn(tab_id, label):
        active_style = ("background:var(--gold);color:#fff;"
                        if active_tab == tab_id
                        else "background:var(--surf2);color:var(--muted);")
        return (f'<a href="{url_for("gw_request")}?tab={tab_id}" '
                f'style="padding:.45rem 1.1rem;border-radius:5px;font-size:.83rem;'
                f'font-weight:600;text-decoration:none;{active_style}">{label}</a>')

    pursuit_form = (
        f'<form method="POST">'
        f'<input type="hidden" name="type" value="pursuit">'
        f'{notice_id_field}'
        f'<div class="fg"><label class="fl">Your organisation *</label>'
        f'<input name="client_org" class="fc2" placeholder="Your firm or organisation name" required>'
        f'<div class="fh">Used throughout the analysis — enter your trading name as you would in a bid.</div></div>'
        f'<div class="fg"><label class="fl">Additional context <span style="color:var(--muted);font-weight:400;">(optional)</span></label>'
        f'<textarea name="details" class="fc2" rows="3" '
        f'placeholder="Any specific aspects to focus on, evaluation criteria, known incumbents..."></textarea></div>'
        f'<div class="fg"><label class="fl">Priority</label>'
        f'<select name="priority" class="fc2">'
        f'<option value="normal">Normal — within 24 hours</option>'
        f'<option value="urgent">Urgent — within 4 hours (closes soon)</option>'
        f'</select></div>'
        f'<button type="submit" class="btn bg-gold">Request pursuit package &rarr;</button>'
        f'</form>'
    )

    org_val = current_user.organisation or ""
    competitor_form = (
        f'<form method="POST">'
        f'<input type="hidden" name="type" value="competitor">'
        f'<div class="fg"><label class="fl">Competitor firm name *</label>'
        f'<input name="firm_name" class="fc2" placeholder="e.g. Bastion, Kordia, Datacom" required></div>'
        f'<div class="fg"><label class="fl">Your organisation *</label>'
        f'<input name="client_org" class="fc2" value="{org_val}" placeholder="Your firm name" required></div>'
        f'<div class="fg"><label class="fl">GETS Notice ID <span style="background:var(--gold);color:#fff;'
        f'font-size:.6rem;font-weight:700;padding:.1rem .4rem;border-radius:3px;margin-left:.4rem;'
        f'letter-spacing:.04em;">RECOMMENDED</span></label>'
        f'<input name="comp_notice_id" class="fc2" placeholder="e.g. 34279032">'
        f'<div class="fh">Anchors the profile to the specific opportunity — pulls incumbent data, '
        f'likely bidders, and evaluation context automatically.</div></div>'
        f'<div class="fg"><label class="fl">Procurement sector '
        f'<span style="color:var(--muted);font-weight:400;">(or enter Notice ID above)</span></label>'
        f'<input name="sector_context" class="fc2" '
        f'placeholder="e.g. government cybersecurity and SOC services, ICT infrastructure, facilities management">'
        f'<div class="fh">Required if no Notice ID supplied — frames the competitive analysis '
        f'around the sector where you compete against this firm.</div></div>'
        f'<div class="fg"><label class="fl">Additional context <span style="color:var(--muted);font-weight:400;">(optional)</span></label>'
        f'<textarea name="comp_context" class="fc2" rows="3" '
        f'placeholder="e.g. Particularly interested in their contract history with Auckland Council, or their approach to central government ICT panels..."></textarea></div>'
        f'<button type="submit" class="btn bg-gold">Request competitor profile &rarr;</button>'
        f'</form>'
    )

    body = (
        f'<div class="ptitle">Request Intelligence</div>'
        f'<div class="psub">Request a pursuit package for a specific tender, or a competitor profile for any NZ firm.</div>'
        f'{ok_msg}'
        f'<div style="display:flex;gap:.5rem;margin-bottom:1.25rem;">'
        f'{_tab_btn("pursuit", "🎯 Pursuit Package")}'
        f'{_tab_btn("competitor", "📊 Competitor Profile")}'
        f'</div>'
        f'<div class="card" style="max-width:580px;">'
        f'<div class="ch"><span class="ct">{"Pursuit Intelligence Package" if active_tab == "pursuit" else "Competitor Profile"}</span></div>'
        f'<div class="cb">'
        f'{pursuit_form if active_tab == "pursuit" else competitor_form}'
        f'</div></div>'
    )
    return _page("Request — Groundwork", body, "request")


# ── Pursuit upgrade (Full Analysis) ──────────────────────────────────────────

@app.route("/groundwork/pursuits/upgrade", methods=["GET", "POST"])
@login_required
def gw_pursuit_upgrade():
    """
    Upload authenticated tender documents from GETS to trigger a Full Analysis
    regeneration of a public pursuit package.
    """
    notice_id   = request.args.get("notice_id", "").strip()
    client_slug = request.args.get("client", "").strip() or current_user.slug

    if not notice_id:
        return redirect(url_for("gw_pursuits"))

    # Fetch notice title for display
    nrow = db.fetchone("SELECT title, agency FROM raw_notices WHERE notice_id = %s", (notice_id,))
    notice_title  = (nrow or {}).get("title") or notice_id
    notice_agency = (nrow or {}).get("agency") or ""

    # Resolve the client organisation name from the original package record, not from
    # the session user. The client_slug comes from the query parameter; client_name
    # was stored in pipeline_outputs when the original package was generated.
    org_row = db.fetchone(
        """
        SELECT client_name FROM pipeline_outputs
         WHERE notice_id = %s AND client_slug = %s AND client_name IS NOT NULL
         ORDER BY created_at DESC LIMIT 1
        """,
        (notice_id, client_slug),
    )
    resolved_client_name = (org_row or {}).get("client_name") or ""

    msg = ""
    if request.method == "POST":
        uploaded_files = request.files.getlist("docs")
        if not uploaded_files or all(f.filename == "" for f in uploaded_files):
            msg = '<div class="al al-er">Please select at least one file to upload.</div>'
        else:
            import storage as _storage
            import threading as _thr

            saved_docs = []   # list of dicts: {file_name, file_path, text}
            errors = []

            for uf in uploaded_files:
                if not uf or uf.filename == "":
                    continue
                fname = uf.filename
                fdata = uf.read()
                fsize = len(fdata)

                if fsize == 0:
                    errors.append(f"{fname}: empty file")
                    continue

                # Store in Supabase Storage
                storage_path = f"uploads/{client_slug}/{notice_id}/{fname}"
                _storage.upload_bytes(fdata, storage_path, uf.content_type or "application/octet-stream")

                # Save metadata to DB
                try:
                    db.execute(
                        "INSERT INTO package_documents "
                        "(notice_id, client_slug, file_path, file_name, file_size) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (notice_id, client_slug, storage_path, fname, fsize),
                    )
                except Exception as _de:
                    logger.warning("package_documents insert failed: %s", _de)

                # Extract text
                text = ""
                try:
                    import io
                    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
                    if ext == "pdf":
                        import pdfplumber
                        with pdfplumber.open(io.BytesIO(fdata)) as pdf:
                            text = "\n\n".join(
                                p.extract_text() or "" for p in pdf.pages
                            ).strip()
                    elif ext in ("docx", "doc"):
                        import docx as _docx
                        doc = _docx.Document(io.BytesIO(fdata))
                        text = "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
                    else:
                        try:
                            text = fdata.decode("utf-8", errors="replace")
                        except Exception:
                            pass
                except Exception as _te:
                    logger.warning("Text extraction failed for %s: %s", fname, _te)

                saved_docs.append({"file_name": fname, "text": text, "file_path": storage_path})

            if not saved_docs and errors:
                msg = '<div class="al al-er">Upload failed: ' + "; ".join(errors) + '</div>'
            elif saved_docs:
                # Dispatch full analysis generation in background thread
                client_org = request.form.get("client_org", "").strip() or notice_id

                def _gen_full(docs, org, nid, c_email, c_slug, p_url):
                    try:
                        from pursuit_package import generate_pursuit_package, _artefact_dir
                        import config as _cfg
                        out_dir = _artefact_dir(org)
                        generate_pursuit_package(
                            notice_id=nid,
                            client_name=org,
                            output_dir=out_dir,
                            preferred_sectors=[],
                            extra_docs=docs,
                            analysis_type="full",
                        )
                        if c_email:
                            import mailer as _m
                            row = db.fetchone("SELECT title FROM raw_notices WHERE notice_id=%s", (nid,))
                            ntitle = (row or {}).get("title") or nid
                            _m.send_pursuit_ready(
                                client_name=org,
                                client_email=c_email,
                                notice_title=f"Full Analysis: {ntitle}",
                                notice_id=nid,
                                portal_url=p_url,
                            )
                    except Exception as exc:
                        logger.exception("Full analysis generation failed notice=%s: %s", nid, exc)

                portal_url = request.host_url.rstrip("/") + url_for("gw_pursuits")
                _thr.Thread(
                    target=_gen_full,
                    args=(saved_docs, client_org, notice_id,
                          current_user.email, client_slug, portal_url),
                    daemon=True, name=f"full-{notice_id[:12]}",
                ).start()

                doc_names = ", ".join(d["file_name"] for d in saved_docs)
                msg = (
                    f'<div class="al al-ok">'
                    f'<strong>{len(saved_docs)} document{"s" if len(saved_docs) != 1 else ""} uploaded</strong> '
                    f'({doc_names}). Your Full Analysis is being generated — '
                    f'you\'ll receive an email when it\'s ready. It will appear in your '
                    f'<a href="{url_for("gw_pursuits")}" style="color:inherit;font-weight:700;">Pursuits library</a>.'
                    f'</div>'
                )

    # Build upload page
    gets_checklist = (
        f'<ul style="margin:.75rem 0;padding-left:1.25rem;font-size:.83rem;line-height:1.8;">'
        f'<li>Main RFP or RFT document (the primary procurement document)</li>'
        f'<li>Any addenda or amendments issued after the original notice</li>'
        f'<li>Supplier briefing presentation <span style="color:var(--muted);">(if applicable)</span></li>'
        f'<li>Q&amp;A document or question log <span style="color:var(--muted);">(if available)</span></li>'
        f'<li>Response form appendices or pricing schedules <span style="color:var(--muted);">(if published)</span></li>'
        f'</ul>'
    )

    body = (
        f'<div class="ptitle">Upgrade to Full Analysis</div>'
        f'<div class="psub">'
        f'Upload authenticated tender documents from GETS to generate a Full Analysis '
        f'that incorporates the actual RFP scope, evaluation criteria, and requirements.'
        f'</div>'
        f'{msg}'
        f'<div class="card" style="max-width:640px;">'
        f'<div class="ch"><span class="ct">Upload Documents — {_safe(notice_title)}</span></div>'
        f'<div class="cb">'
        f'<div style="background:rgba(100,120,180,.1);border:1px solid rgba(100,120,180,.25);'
        f'border-radius:6px;padding:.85rem 1rem;margin-bottom:1.25rem;font-size:.82rem;">'
        f'<strong style="color:#8ab4f8;">What to retrieve from GETS:</strong>'
        f'{gets_checklist}'
        f'Go to <a href="https://www.gets.govt.nz" target="_blank" '
        f'style="color:var(--gold);">gets.govt.nz</a>, find notice {_safe(notice_id)}, '
        f'and download documents from the tender\'s Documents tab.'
        f'</div>'
        f'<form method="POST" enctype="multipart/form-data" id="upgrade-form">'
        f'<input type="hidden" name="client_org" value="{_safe(resolved_client_name)}">'
        f'<div class="fg">'
        f'<label class="fl">Select documents to upload *</label>'
        f'<div id="drop-zone" style="border:2px dashed var(--border);border-radius:8px;'
        f'padding:2rem;text-align:center;cursor:pointer;transition:border-color .2s;'
        f'background:var(--surf2);" '
        f'onclick="document.getElementById(\'doc-input\').click()" '
        f'ondragover="event.preventDefault();this.style.borderColor=\'#2a9d8f\'" '
        f'ondragleave="this.style.borderColor=\'\'" '
        f'ondrop="event.preventDefault();this.style.borderColor=\'\';'
        f'handleDrop(event.dataTransfer.files)">'
        f'<div style="font-size:2rem;margin-bottom:.5rem;">&#128196;</div>'
        f'<div style="font-size:.87rem;color:var(--muted);">Drag and drop PDF or DOCX files here, or click to browse</div>'
        f'<div id="file-list" style="margin-top:.75rem;font-size:.8rem;color:var(--text);"></div>'
        f'</div>'
        f'<input type="file" id="doc-input" name="docs" multiple accept=".pdf,.docx,.doc" '
        f'style="display:none;" onchange="updateFileList(this.files)">'
        f'<div class="fh">Accepted formats: PDF, DOCX. Maximum 10 files.</div>'
        f'</div>'
        f'<button type="submit" id="submit-btn" class="btn bg-gold" disabled '
        f'style="opacity:.5;cursor:not-allowed;">'
        f'Generate Full Analysis &rarr;</button>'
        f'</form>'
        f'</div></div>'
        f'<script>'
        f'function updateFileList(files){{'
        f'  var list=document.getElementById("file-list");'
        f'  var btn=document.getElementById("submit-btn");'
        f'  if(!files||files.length===0){{list.textContent="";btn.disabled=true;btn.style.opacity=".5";btn.style.cursor="not-allowed";return;}}'
        f'  var names=Array.from(files).map(function(f){{return f.name;}}).join(", ");'
        f'  list.textContent=files.length+" file(s) selected: "+names;'
        f'  btn.disabled=false;btn.style.opacity="1";btn.style.cursor="pointer";'
        f'}}'
        f'function handleDrop(files){{'
        f'  var input=document.getElementById("doc-input");'
        f'  var dt=new DataTransfer();'
        f'  Array.from(files).forEach(function(f){{dt.items.add(f);}});'
        f'  input.files=dt.files;'
        f'  updateFileList(input.files);'
        f'}}'
        f'document.getElementById("doc-input").addEventListener("change",function(){{updateFileList(this.files);}});'
        f'</script>'
    )
    return _page("Upgrade to Full Analysis — Groundwork", body, "pursuits")


# ── Admin ─────────────────────────────────────────────────────────────────────

_PLAN_LABELS = {"watch": "Watch", "pursue": "Pursue", "edge": "Edge"}
_PLAN_PILL_CSS = {
    "watch":   "background:rgba(100,120,180,.2);color:#8ab4f8;border:1px solid rgba(100,120,180,.35);",
    "pursue":  "background:rgba(42,157,143,.15);color:var(--gold);border:1px solid rgba(42,157,143,.3);",
    "edge":    "background:rgba(212,160,23,.14);color:#d4a017;border:1px solid rgba(212,160,23,.32);",
}

def _plan_pill(plan: str) -> str:
    label = _PLAN_LABELS.get(plan, plan or "—")
    css = _PLAN_PILL_CSS.get(plan, "")
    return (f'<span style="font-size:.65rem;font-weight:700;letter-spacing:.07em;'
            f'text-transform:uppercase;border-radius:4px;padding:.15rem .5rem;{css}">'
            f'{label}</span>')

def _billing_pill(status: str) -> str:
    colours = {
        "active":    "color:#4ade80;",
        "trial":     "color:#fbbf24;",
        "suspended": "color:#f87171;",
    }
    return (f'<span style="font-size:.7rem;{colours.get(status,"color:var(--muted);")}>'
            f'{(status or "active").title()}</span>')


# ── Admin — Leads ─────────────────────────────────────────────────────────────

@app.route("/admin/leads", methods=["GET", "POST"])
@login_required
@admin_required
def admin_leads():
    import mailer as _mailer

    msg = ""
    if request.method == "POST":
        action  = request.form.get("action", "")
        lead_id = request.form.get("lead_id", "")
        notes   = request.form.get("notes", "").strip()

        if action == "notes" and lead_id:
            db.execute("UPDATE leads SET notes=%s, updated_at=NOW() WHERE id=%s",
                       (notes, lead_id))
            msg = '<div class="al al-ok">Notes saved.</div>'

        elif action == "reject" and lead_id:
            db.execute("UPDATE leads SET status='rejected', updated_at=NOW() WHERE id=%s",
                       (lead_id,))
            msg = '<div class="al al-ok">Lead marked rejected.</div>'

        elif action == "delete" and lead_id:
            db.execute("DELETE FROM leads WHERE id=%s", (lead_id,))
            msg = '<div class="al al-ok">Lead deleted.</div>'

        elif action == "delete_all_test":
            db.execute(
                "DELETE FROM leads WHERE email ILIKE '%test%' OR email ILIKE '%example%' "
                "OR name ILIKE '%test%'"
            )
            msg = '<div class="al al-ok">Test/example leads deleted.</div>'

        elif action == "approve" and lead_id:
            lead = db.fetchone("SELECT * FROM leads WHERE id=%s", (lead_id,))
            if not lead:
                msg = '<div class="al al-er">Lead not found.</div>'
            else:
                # Generate username from email (before @)
                base_un = (lead["email"].split("@")[0]
                           .lower().replace(".", "_").replace("+", "_"))
                username = base_un
                # Ensure unique
                cfg = _load_cfg()
                n = 1
                while username in cfg.get("clients", {}):
                    username = f"{base_un}{n}"; n += 1

                temp_pw = secrets.token_urlsafe(12)
                plan    = lead.get("plan") or "pursue"
                sectors = [s.strip() for s in (lead.get("sectors") or "").split(",")
                           if s.strip()]
                _add_user(username, temp_pw, lead["name"], lead["email"],
                          is_admin=False, sectors=sectors, plan=plan, billing_status="active",
                          temp_password=True)

                db.execute(
                    "UPDATE leads SET status='approved', portal_username=%s, "
                    "updated_at=NOW() WHERE id=%s",
                    (username, lead_id),
                )

                # Welcome email
                login_url = request.host_url.rstrip("/") + url_for("login")
                welcome_html = f"""
<div style="font-family:'Inter',system-ui,sans-serif;max-width:600px;margin:0 auto;">
  <div style="background:#1a2d4a;padding:1.5rem 2rem;">
    <div style="font-size:1rem;font-weight:800;color:#fff;">
      Groundwork <span style="color:#2a9d8f;font-weight:400;">by BidEdge</span></div>
  </div>
  <div style="padding:2rem;">
    <h2 style="font-size:1.2rem;font-weight:800;color:#1a2d4a;margin:0 0 1rem;">
      Welcome to Groundwork, {lead['name'].split()[0]}!</h2>
    <p style="color:#4a5568;line-height:1.7;margin:0 0 1.25rem;">
      Your Groundwork account is ready. Here are your login details:</p>
    <div style="background:#f7f9fc;border:1px solid #dde2ea;border-radius:8px;
      padding:1rem 1.25rem;margin-bottom:1.5rem;font-size:.9rem;">
      <div style="margin-bottom:.5rem;"><b>Login URL:</b>
        <a href="{login_url}" style="color:#2a9d8f;">{login_url}</a></div>
      <div style="margin-bottom:.5rem;"><b>Username:</b> <code>{username}</code></div>
      <div><b>Temporary password:</b> <code>{temp_pw}</code></div>
    </div>
    <p style="color:#4a5568;line-height:1.7;margin:0 0 1rem;">
      <b>Getting started:</b><br>
      1. Log in and set your sector preferences on first login — this filters the
      watchlist to your relevant markets.<br>
      2. Check the Daily Watchlist each morning for scored opportunities.<br>
      3. Request a pursuit package on any notice worth pursuing.<br>
      4. Reply to this email for support.</p>
    <a href="{login_url}" style="display:inline-block;background:#2a9d8f;color:#fff;
      font-weight:700;font-size:.9rem;padding:.7rem 1.5rem;border-radius:6px;
      text-decoration:none;margin-top:.5rem;">Log in to Groundwork &rarr;</a>
  </div>
</div>
"""
                _mailer.send_to_client(
                    subject="Welcome to Groundwork — your account is ready",
                    html=welcome_html,
                    client_email=lead["email"],
                    _async=True,
                )
                msg = (f'<div class="al al-ok">Lead approved — account <strong>{username}</strong> '
                       f'created and welcome email sent to {lead["email"]}.</div>')

    leads = db.fetchall(
        "SELECT * FROM leads ORDER BY created_at DESC LIMIT 200"
    )

    STATUS_COLOUR = {
        "enquiry":  "color:var(--gold);",
        "approved": "color:#4ade80;",
        "rejected": "color:#f87171;",
        "duplicate":"color:var(--muted);",
    }

    rows = ""
    for ld in leads:
        sid = ld["id"]
        sc  = STATUS_COLOUR.get(ld.get("status", "enquiry"), "")
        approved = ld.get("status") == "approved"
        rejected = ld.get("status") == "rejected"
        delete_btn = (
            f'<form method="POST" style="display:inline;margin-left:.25rem;">'
            f'<input type="hidden" name="lead_id" value="{sid}">'
            f'<input type="hidden" name="action" value="delete">'
            f'<button class="btn bg-ghost sm" type="submit" style="color:#f87171;"'
            f' onclick="return confirm(\'Delete this lead permanently?\')">Delete</button></form>'
        )
        action_btns = ""
        if not approved and not rejected:
            action_btns = (
                f'<form method="POST" style="display:inline;">'
                f'<input type="hidden" name="lead_id" value="{sid}">'
                f'<input type="hidden" name="action" value="approve">'
                f'<button class="btn bg-gold sm" type="submit">Approve</button></form> '
                f'<form method="POST" style="display:inline;">'
                f'<input type="hidden" name="lead_id" value="{sid}">'
                f'<input type="hidden" name="action" value="reject">'
                f'<button class="btn bg-ghost sm" type="submit"'
                f' onclick="return confirm(\'Reject this lead?\')">Reject</button></form>'
                + delete_btn
            )
        elif approved:
            action_btns = (
                f'<span style="color:#4ade80;font-size:.75rem;">✓ {ld.get("portal_username","")}</span>'
                + delete_btn
            )
        else:
            action_btns = delete_btn

        notes_form = (
            f'<form method="POST" style="margin-top:.35rem;display:flex;gap:.4rem;">'
            f'<input type="hidden" name="lead_id" value="{sid}">'
            f'<input type="hidden" name="action" value="notes">'
            f'<input name="notes" class="fc2" style="font-size:.75rem;padding:.25rem .5rem;'
            f'flex:1;" value="{(ld.get("notes") or "").replace(chr(34), "&quot;")}" '
            f'placeholder="Notes...">'
            f'<button class="btn bg-out sm" type="submit" style="white-space:nowrap;">Save</button>'
            f'</form>'
        )
        created = str(ld.get("created_at") or "")[:16]
        rows += (
            f'<tr>'
            f'<td><strong>{ld.get("name","")}</strong><br>'
            f'<span style="color:var(--muted);font-size:.72rem;">{ld.get("organisation","")}</span></td>'
            f'<td>{_plan_pill(ld.get("plan",""))}</td>'
            f'<td style="font-size:.78rem;">{ld.get("email","")}</td>'
            f'<td style="font-size:.72rem;color:var(--muted);">{ld.get("sectors","") or "—"}</td>'
            f'<td style="font-size:.72rem;color:var(--muted);white-space:nowrap;">{created}</td>'
            f'<td style="font-size:.75rem;{sc}">{(ld.get("status") or "enquiry").title()}</td>'
            f'<td style="min-width:180px;">{action_btns}{notes_form}</td>'
            f'</tr>'
        )

    pending = sum(1 for l in leads if l.get("status") == "enquiry")
    body = (
        f'<div class="ptitle">Leads</div>'
        f'<div class="psub">{len(leads)} total &middot; {pending} pending review</div>'
        f'{msg}'
        f'<div class="card">'
        f'<div class="ch"><span class="ct">Signup Enquiries</span>'
        f'<form method="POST" style="display:inline;">'
        f'<input type="hidden" name="action" value="delete_all_test">'
        f'<button class="btn bg-ghost sm" type="submit" style="color:#f87171;font-size:.72rem;"'
        f' onclick="return confirm(\'Delete all test/example leads?\')">Delete test entries</button>'
        f'</form></div>'
        f'<div style="overflow-x:auto;">'
        f'<table class="dt"><thead><tr>'
        f'<th>Name / Org</th><th>Plan</th><th>Email</th>'
        f'<th>Sectors</th><th>Date</th><th>Status</th><th>Actions / Notes</th>'
        f'</tr></thead><tbody>'
        f'{rows or "<tr><td colspan=7 style=color:var(--muted);text-align:center;padding:2rem>No leads yet</td></tr>"}'
        f'</tbody></table></div></div>'
    )
    return _page("Admin — Leads", body, "admin-leads")


# ── Admin — Clients list ──────────────────────────────────────────────────────

@app.route("/admin/clients", methods=["GET", "POST"])
@login_required
@admin_required
def admin_clients_list():
    msg = ""
    cfg = _load_cfg()

    if request.method == "POST":
        action   = request.form.get("action", "")
        username = request.form.get("username", "")
        data     = cfg.get("clients", {}).get(username, {})
        if not data:
            msg = '<div class="al al-er">User not found.</div>'
        elif action == "change_plan":
            new_plan = request.form.get("plan", "pursue")
            data["plan"] = new_plan
            _save_cfg(cfg)
            msg = f'<div class="al al-ok">Plan updated to {new_plan} for {username}.</div>'
        elif action == "change_billing":
            new_status = request.form.get("billing_status", "active")
            data["billing_status"] = new_status
            _save_cfg(cfg)
            msg = f'<div class="al al-ok">Billing status updated for {username}.</div>'
        elif action == "reset_password":
            new_pw = secrets.token_urlsafe(12)
            data["password_hash"] = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt()).decode()
            _save_cfg(cfg)
            msg = f'<div class="al al-ok">Password reset for {username}: <code>{new_pw}</code></div>'
        elif action == "suspend":
            data["billing_status"] = "suspended"
            _save_cfg(cfg)
            msg = f'<div class="al al-ok">{username} suspended.</div>'
        elif action == "delete_client" and username:
            clients_dict = cfg.get("clients", {})
            if username in clients_dict and not clients_dict[username].get("is_admin"):
                del clients_dict[username]
                _save_cfg(cfg)
                msg = f'<div class="al al-ok">Account <strong>{username}</strong> deleted.</div>'
            else:
                msg = '<div class="al al-er">Cannot delete — user not found or is admin.</div>'
        cfg = _load_cfg()  # reload after save

    clients = {u: d for u, d in cfg.get("clients", {}).items() if not d.get("is_admin")}

    rows = ""
    for un, d in sorted(clients.items()):
        plan    = d.get("plan", "pursue")
        billing = d.get("billing_status", "active")
        plan_sel = "".join(
            f'<option value="{p}" {"selected" if plan==p else ""}>{_PLAN_LABELS[p]}</option>'
            for p in ("watch", "pursue", "edge")
        )
        bill_sel = "".join(
            f'<option value="{s}" {"selected" if billing==s else ""}>{s.title()}</option>'
            for s in ("trial", "active", "suspended")
        )
        slug = d.get("artefact_slug") or _slug(d.get("display_name", un))
        n_pursuits = len(_list_artefacts(slug, "*pursuit*.html"))

        rows += (
            f'<tr>'
            f'<td><strong>{d.get("display_name", un)}</strong>'
            f'<br><span style="color:var(--muted);font-size:.72rem;">@{un}</span></td>'
            f'<td style="font-size:.78rem;">{d.get("email","—")}</td>'
            f'<td>{_plan_pill(plan)}</td>'
            f'<td>{_billing_pill(billing)}</td>'
            f'<td style="font-size:.75rem;color:var(--muted);">{n_pursuits}</td>'
            f'<td style="white-space:nowrap;min-width:320px;">'
            # Change plan
            f'<form method="POST" style="display:inline-flex;gap:.35rem;margin-right:.4rem;">'
            f'<input type="hidden" name="username" value="{un}">'
            f'<input type="hidden" name="action" value="change_plan">'
            f'<select name="plan" class="fc2" style="font-size:.72rem;padding:.2rem .4rem;">'
            f'{plan_sel}</select>'
            f'<button class="btn bg-out sm" type="submit">Set plan</button></form>'
            # Reset password
            f'<form method="POST" style="display:inline;">'
            f'<input type="hidden" name="username" value="{un}">'
            f'<input type="hidden" name="action" value="reset_password">'
            f'<button class="btn bg-ghost sm" type="submit"'
            f' onclick="return confirm(\'Reset password for {un}?\')">Reset pw</button></form>'
            # Suspend
            + ('' if billing == 'suspended' else
               f'<form method="POST" style="display:inline;margin-left:.25rem;">'
               f'<input type="hidden" name="username" value="{un}">'
               f'<input type="hidden" name="action" value="suspend">'
               f'<button class="btn bg-ghost sm" type="submit" style="color:#f87171;"'
               f' onclick="return confirm(\'Suspend {un}?\')">Suspend</button></form>')
            # Delete
            + (f'<form method="POST" style="display:inline;margin-left:.25rem;">'
               f'<input type="hidden" name="username" value="{un}">'
               f'<input type="hidden" name="action" value="delete_client">'
               f'<button class="btn bg-ghost sm" type="submit" style="color:#f87171;"'
               f' onclick="return confirm(\'Delete account {un} permanently? This cannot be undone.\')">Delete</button></form>')
            # Manage link
            + f'&nbsp;<a href="{url_for("admin_client", username=un)}" class="btn bg-out sm">Manage</a>'
            f'</td></tr>'
        )

    body = (
        f'<div class="ptitle">Clients</div>'
        f'<div class="psub">{len(clients)} active accounts</div>'
        f'{msg}'
        f'<div class="card">'
        f'<div class="ch"><span class="ct">All Accounts</span>'
        f'<a href="{url_for("admin_add_client")}" class="btn bg-gold sm">+ Add client</a></div>'
        f'<div style="overflow-x:auto;">'
        f'<table class="dt"><thead><tr>'
        f'<th>Name</th><th>Email</th><th>Plan</th><th>Billing</th>'
        f'<th>Pursuits</th><th>Actions</th>'
        f'</tr></thead><tbody>'
        f'{rows or "<tr><td colspan=6 style=color:var(--muted);text-align:center;padding:2rem>No clients yet</td></tr>"}'
        f'</tbody></table></div></div>'
    )
    return _page("Admin — Clients", body, "admin-clients")


# ── Admin — Requests (all pursuit/competitor requests) ────────────────────────

@app.route("/admin/requests")
@login_required
@admin_required
def admin_requests():
    try:
        reqs = db.fetchall(
            """
            SELECT r.id, r.client_id, r.notice_id, r.request_type,
                   r.priority, r.status, r.requested_at, r.completed_at,
                   r.output_path, r.error_message,
                   n.title AS notice_title
              FROM pursuit_requests r
              LEFT JOIN raw_notices n ON n.notice_id = r.notice_id
             ORDER BY r.requested_at DESC
             LIMIT 200
            """
        )
    except Exception as exc:
        reqs = []
        logger.warning("admin_requests query failed (table may not exist yet): %s", exc)

    STATUS_CSS = {
        "pending":    "color:var(--gold);",
        "generating": "color:#60a5fa;",
        "complete":   "color:#4ade80;",
        "failed":     "color:#f87171;",
    }

    rows = ""
    for r in reqs:
        sc  = STATUS_CSS.get(r.get("status", "pending"), "")
        ts  = str(r.get("requested_at") or "")[:16]
        rows += (
            f'<tr>'
            f'<td style="font-size:.75rem;color:var(--muted);">{r.get("client_id","")}</td>'
            f'<td style="font-size:.78rem;max-width:280px;overflow:hidden;text-overflow:ellipsis;">'
            f'{r.get("notice_title") or r.get("notice_id","—")}</td>'
            f'<td style="font-size:.72rem;">{r.get("request_type","")}</td>'
            f'<td><span style="font-size:.72rem;{sc}">{r.get("status","").title()}</span></td>'
            f'<td style="font-size:.72rem;color:var(--muted);white-space:nowrap;">{ts}</td>'
            f'<td>'
        )
        if r.get("output_path"):
            # output_path stored in DB is relative to ARTEFACTS root (includes slug).
            # serve_artefact_file expects filepath WITHOUT the slug prefix.
            _op = r["output_path"]
            _op_stripped = "/".join(_op.split("/", 1)[1:]) if "/" in _op else _op
            _vurl = url_for("serve_artefact_file",
                            client_slug=r["client_id"],
                            filepath=_op_stripped)
            rows += f'<a href="{_vurl}" target="_blank" class="btn bg-out sm">View</a>'
        elif r.get("status") in ("pending", "failed"):
            rows += (
                f'<form method="POST" action="{url_for("admin_trigger_request")}" '
                f'style="display:inline;">'
                f'<input type="hidden" name="req_id" value="{r["id"]}">'
                f'<button class="btn bg-gold sm" type="submit">Retry</button></form>'
            )
        rows += f'</td></tr>'

    body = (
        f'<div class="ptitle">Requests</div>'
        f'<div class="psub">All pursuit and competitor profile requests</div>'
        f'<div class="card">'
        f'<div class="ch"><span class="ct">Request Queue</span></div>'
        f'<div style="overflow-x:auto;">'
        f'<table class="dt"><thead><tr>'
        f'<th>Client</th><th>Notice</th><th>Type</th>'
        f'<th>Status</th><th>Requested</th><th></th>'
        f'</tr></thead><tbody>'
        f'{rows or "<tr><td colspan=6 style=color:var(--muted);text-align:center;padding:2rem>No requests yet</td></tr>"}'
        f'</tbody></table></div></div>'
    )
    return _page("Admin — Requests", body, "admin-requests")


@app.route("/admin/requests/trigger", methods=["POST"])
@login_required
@admin_required
def admin_trigger_request():
    """Manually retry a failed or pending pursuit request."""
    req_id = request.form.get("req_id", "")
    if req_id:
        try:
            import pursuit_worker
            row = db.fetchone("SELECT * FROM pursuit_requests WHERE id=%s", (req_id,))
            if row:
                cfg = _load_cfg()
                client_data = cfg.get("clients", {}).get(row["client_id"], {})
                portal_url = request.host_url.rstrip("/") + url_for("gw_pursuits")
                pursuit_worker.dispatch(
                    req_id=int(req_id),
                    client_id=row["client_id"],
                    client_name=client_data.get("display_name", row["client_id"]),
                    client_email=client_data.get("email", ""),
                    notice_id=row["notice_id"],
                    preferred_sectors=client_data.get("preferred_sectors") or [],
                    artefact_slug=client_data.get("artefact_slug") or _slug(client_data.get("display_name", row["client_id"])),
                    portal_url=portal_url,
                    immediate=True,
                )
                _flash(f"Request {req_id} dispatched.", "success")
        except Exception as exc:
            logger.exception("admin_trigger_request failed: %s", exc)
            _flash(f"Trigger failed: {exc}", "error")
    return redirect(url_for("admin_requests"))


# ── Admin — Briefs send history ───────────────────────────────────────────────

@app.route("/admin/briefs")
@login_required
@admin_required
def admin_briefs():
    try:
        sends = db.fetchall(
            "SELECT * FROM brief_sends ORDER BY sent_at DESC LIMIT 200"
        )
    except Exception as exc:
        sends = []
        logger.warning("admin_briefs query failed: %s", exc)

    rows = ""
    for s in sends:
        ts = str(s.get("sent_at") or "")[:16]
        sectors = ", ".join(s.get("sectors") or []) or "all"
        status_css = "color:#4ade80;" if s.get("status") == "sent" else "color:#f87171;"
        rows += (
            f'<tr>'
            f'<td style="font-size:.78rem;">{s.get("client_id","")}</td>'
            f'<td style="font-size:.72rem;color:var(--muted);">{sectors}</td>'
            f'<td style="font-size:.72rem;color:var(--muted);white-space:nowrap;">{ts}</td>'
            f'<td style="font-size:.75rem;{status_css}">{(s.get("status") or "").title()}</td>'
            f'<td style="font-size:.7rem;color:#f87171;">{s.get("error_msg") or ""}</td>'
            f'</tr>'
        )

    body = (
        f'<div class="ptitle">Watch Brief Sends</div>'
        f'<div class="psub">{len(sends)} records</div>'
        f'<div class="card">'
        f'<div class="ch"><span class="ct">Send History</span></div>'
        f'<div style="overflow-x:auto;">'
        f'<table class="dt"><thead><tr>'
        f'<th>Client</th><th>Sectors</th><th>Sent at</th><th>Status</th><th>Error</th>'
        f'</tr></thead><tbody>'
        f'{rows or "<tr><td colspan=5 style=color:var(--muted);text-align:center;padding:2rem>No sends yet</td></tr>"}'
        f'</tbody></table></div></div>'
    )
    return _page("Admin — Briefs", body, "admin-briefs")


# ── Admin — Pipeline control ──────────────────────────────────────────────────

@app.route("/admin/pipeline", methods=["GET", "POST"])
@login_required
@admin_required
def admin_pipeline():
    msg = ""
    if request.method == "POST":
        stage = request.form.get("stage", "")
        _demo_force = request.form.get("force") == "1"
        if stage in ("layer1", "layer2", "ach_enriched", "ach_unprocessed", "incumbent_all", "watch_brief", "demo_content"):
            import threading as _thr

            def _run_stage(s):
                run_id = None
                summary = ""
                status = "failed"
                try:
                    row = db.fetchone(
                        "INSERT INTO pipeline_runs (stage, triggered_by, status) "
                        "VALUES (%s, 'admin', 'running') RETURNING id",
                        (s,),
                    )
                    run_id = row["id"] if row else None
                except Exception as _e:
                    logger.warning("pipeline_runs INSERT failed: %s", _e)
                try:
                    if s == "layer1":
                        from scheduler_railway import _run_layer1
                        _run_layer1()
                        summary = "layer1 completed"
                        status = "complete"
                    elif s == "layer2":
                        # Call layer2_pipeline.main() directly so exceptions propagate
                        # (scheduler wrapper absorbs them, masking failures as complete)
                        import layer2_pipeline
                        layer2_pipeline.main()
                        summary = "layer2 completed"
                        status = "complete"
                    elif s == "ach_enriched":
                        from bidder_intelligence import run_ach_for_enriched
                        counts = run_ach_for_enriched()
                        summary = (f"processed={counts.get('processed',0)} "
                                   f"skipped={counts.get('skipped',0)} "
                                   f"failed={counts.get('failed',0)}")
                        status = "complete"
                    elif s == "ach_unprocessed":
                        from bidder_intelligence import run_ach_for_unprocessed
                        counts = run_ach_for_unprocessed()
                        summary = (f"processed={counts.get('processed',0)} "
                                   f"failed={counts.get('failed',0)}")
                        status = "complete"
                    elif s == "incumbent_all":
                        from bidder_intelligence import run_incumbent_detection_all
                        counts = run_incumbent_detection_all()
                        summary = (f"run={counts.get('run',0)} "
                                   f"stored={counts.get('stored',0)} "
                                   f"skipped={counts.get('skipped',0)} "
                                   f"failed={counts.get('failed',0)}")
                        status = "complete"
                    elif s == "watch_brief":
                        from scheduler_railway import _run_watch_brief
                        result = _run_watch_brief()
                        if isinstance(result, dict):
                            if result.get("error"):
                                summary = f"Error: {result['error'][:300]}"
                                status = "failed"
                            else:
                                gen = result.get("generated", 0)
                                sent = result.get("sent", 0)
                                failed = result.get("failed", 0)
                                skipped = result.get("skipped", 0)
                                errs = result.get("errors", [])
                                summary = (f"generated={gen} sent={sent} failed={failed} "
                                           f"skipped={skipped}")
                                if errs:
                                    summary += f" | {'; '.join(errs[:3])}"
                                status = "complete" if sent > 0 else (
                                    "failed" if failed > 0 else "complete"
                                )
                        else:
                            summary = "watch_brief completed"
                            status = "complete"
                    elif s == "demo_content":
                        from generate_demo_content import main as _gen_demo
                        stats = _gen_demo(force=_demo_force)
                        total = stats.get("total", 0)
                        by_sec = stats.get("by_sector", {})
                        detail = " | ".join(f"{k}:{v}" for k, v in by_sec.items())
                        summary = f"{total} artefacts across {stats.get('sectors',0)} sectors — {detail}"
                        status = "complete" if total > 0 else "failed"
                        if total == 0:
                            logger.error(
                                "demo_content run produced 0 artefacts — check individual sector logs above"
                            )
                except Exception as exc:
                    summary = str(exc)[:500]
                    status = "failed"
                    logger.exception("_run_stage %s failed: %s", s, exc)
                finally:
                    if run_id:
                        try:
                            db.execute(
                                "UPDATE pipeline_runs SET status=%s, summary=%s, finished_at=NOW() "
                                "WHERE id=%s",
                                (status, summary, run_id),
                            )
                        except Exception as _e:
                            logger.warning("pipeline_runs UPDATE failed: %s", _e)

            t = _thr.Thread(target=_run_stage, args=(stage,), daemon=True)
            t.start()
            labels = {
                "layer1": "Layer 1", "layer2": "Layer 2",
                "ach_enriched": "ACH (refresh stale)", "ach_unprocessed": "ACH (catch up new)",
                "incumbent_all": "Incumbent Detection — All Notices",
                "watch_brief": "Watch Briefs", "demo_content": "Demo Content",
            }
            msg = (f'<div class="al al-ok" id="pipeline-msg">'
                   f'<strong>{labels.get(stage, stage)} started</strong> — running in background. '
                   f'This page will refresh automatically every 15 seconds until complete.</div>')

    # Recent runs
    try:
        runs = db.fetchall(
            "SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT 50"
        )
    except Exception as exc:
        runs = []
        logger.warning("pipeline_runs query failed (table may not exist): %s", exc)

    STATUS_CSS = {
        "running":  "color:#60a5fa;",
        "complete": "color:#4ade80;",
        "failed":   "color:#f87171;",
    }

    run_rows = ""
    for r in runs:
        sc = STATUS_CSS.get(r.get("status",""), "")
        ts = str(r.get("started_at") or "")[:16]
        ft = str(r.get("finished_at") or "")[:16] or "—"
        run_rows += (
            f'<tr>'
            f'<td style="font-size:.78rem;">{r.get("stage","")}</td>'
            f'<td style="font-size:.72rem;color:var(--muted);">{r.get("triggered_by","")}</td>'
            f'<td style="font-size:.72rem;color:var(--muted);white-space:nowrap;">{ts}</td>'
            f'<td style="font-size:.72rem;color:var(--muted);white-space:nowrap;">{ft}</td>'
            f'<td style="font-size:.75rem;{sc}">{(r.get("status") or "").title()}</td>'
            f'<td style="font-size:.7rem;color:var(--muted);max-width:300px;overflow:hidden;'
            f'text-overflow:ellipsis;">{r.get("summary") or ""}</td>'
            f'</tr>'
        )

    # Demo DB diagnostic
    try:
        _demo_html_count = db.fetchone(
            "SELECT COUNT(*) AS cnt FROM pipeline_outputs WHERE output_type = 'demo_html'"
        ) or {}
        _demo_manifest_row = db.fetchone(
            "SELECT run_date, created_at FROM pipeline_outputs "
            "WHERE output_type = 'demo_manifest' ORDER BY run_date DESC, created_at DESC LIMIT 1"
        )
    except Exception:
        _demo_html_count = {}
        _demo_manifest_row = None

    _demo_html_n = _demo_html_count.get("cnt", "?")
    if _demo_manifest_row:
        _mdate = str(_demo_manifest_row.get("run_date", ""))
        _mts   = str(_demo_manifest_row.get("created_at", ""))[:16]
        _demo_db_info = (
            f'<span style="color:#4ade80;">&#10003;</span> Manifest in DB (run_date={_mdate}, saved {_mts} UTC) — '
            f'{_demo_html_n} demo_html artefacts stored'
        )
    else:
        _demo_db_info = (
            f'<span style="color:#f87171;">&#10007;</span> No manifest found in DB — '
            f'generation has not yet completed successfully'
        )

    import os as _os
    _resend_ok = bool(_os.getenv("RESEND_API_KEY", "").strip())
    _resend_warn = (
        f'<div class="al al-er" style="margin-bottom:1.25rem;">'
        f'<strong>⚠ RESEND_API_KEY not set</strong> — Watch Brief emails will not be delivered. '
        f'Set <code>RESEND_API_KEY</code> in Railway Variables before triggering Watch Briefs. '
        f'Briefs will still be generated and saved to the portal.</div>'
    ) if not _resend_ok else ""

    def _trigger_btn(stage: str, label: str, colour: str = "bg-gold") -> str:
        return (
            f'<form method="POST" style="display:inline;">'
            f'<input type="hidden" name="stage" value="{stage}">'
            f'<button class="btn {colour}" type="submit" '
            f'onclick="if(!confirm(\'Run {label}?\')){{return false;}}'
            f'this.textContent=\'Starting…\';this.disabled=true;">'
            f'{label}</button></form>'
        )

    _has_running = any(r.get("status") == "running" for r in runs)
    _autorefresh = (
        '<script>setTimeout(function(){location.reload();}, 15000);</script>'
        if _has_running else ""
    )

    body = (
        f'{_autorefresh}'
        f'<div class="ptitle">Pipeline Control</div>'
        f'<div class="psub">Trigger pipeline stages manually. Jobs run in a background thread. '
        f'Page auto-refreshes every 15 s while a job is running.</div>'
        f'{_resend_warn}'
        f'{msg}'
        f'<div class="card" style="margin-bottom:1.5rem;">'
        f'<div class="ch"><span class="ct">Layer 1 &amp; Layer 2</span></div>'
        f'<div class="cb" style="display:flex;gap:1rem;flex-wrap:wrap;padding-bottom:.5rem;">'
        f'{_trigger_btn("layer1", "⚙ Run Layer 1 now")}'
        f'{_trigger_btn("layer2", "🛰 Run Layer 2 now", "bg-out")}'
        f'</div>'
        f'<div style="padding:.25rem 1.25rem .75rem;font-size:.78rem;color:var(--muted);">'
        f'<strong>Layer 1</strong> — GETS ingest → parse → score → enrich → output<br>'
        f'<strong>Layer 2</strong> — Awards ingestion → org profiles → pattern detection → MI'
        f'</div></div>'
        f'<div class="card" style="margin-bottom:1.5rem;">'
        f'<div class="ch"><span class="ct">ACH Bidder Intelligence</span></div>'
        f'<div class="cb" style="display:flex;gap:1rem;flex-wrap:wrap;padding-bottom:.5rem;">'
        f'{_trigger_btn("ach_enriched", "🔍 ACH — Refresh stale", "bg-out")}'
        f'{_trigger_btn("ach_unprocessed", "🔍 ACH — Catch up new notices", "bg-out")}'
        f'{_trigger_btn("incumbent_all", "🏷 Incumbent Detection — All Notices", "bg-out")}'
        f'</div>'
        f'<div style="padding:.25rem 1.25rem .75rem;font-size:.78rem;color:var(--muted);">'
        f'<strong>Refresh stale</strong> — Re-runs ACH for all enriched notices where results are outdated. '
        f'Also runs incumbent detection per notice.<br>'
        f'<strong>Catch up new</strong> — Runs ACH only for notices that have zero ach_analysis rows yet.<br>'
        f'<strong>Incumbent Detection — All Notices</strong> — Runs incumbent web search independently '
        f'of ACH for every notice in the watchlist. Skips notices that already have an '
        f'incumbent_identified row. Use this when ACH has already run but incumbents are missing.'
        f'</div></div>'
        f'<div class="card" style="margin-bottom:1.5rem;">'
        f'<div class="ch"><span class="ct">Watch Briefs &amp; Demo Content</span></div>'
        f'<div class="cb" style="display:flex;gap:1rem;flex-wrap:wrap;padding-bottom:.5rem;">'
        f'{_trigger_btn("watch_brief", "📬 Generate Watch Briefs now", "bg-out")}'
        f'<form method="POST" style="display:inline;">'
        f'<input type="hidden" name="stage" value="demo_content">'
        f'<input type="hidden" name="force" value="1">'
        f'<button class="btn bg-out" type="submit" '
        f'onclick="if(!confirm(\'Regenerate ALL demo content for all 7 sectors? Overwrites existing artefacts. Takes 5–10 mins.\')){{return false;}}'
        f'this.textContent=\'Starting…\';this.disabled=true;">'
        f'🎬 Regenerate Demo Content</button></form>'
        f'</div>'
        f'<div style="padding:.25rem 1.25rem .75rem;font-size:.78rem;color:var(--muted);">'
        f'<strong>Watch Briefs</strong> — Generate and email brief to all active clients<br>'
        f'<strong>Demo Content</strong> — Generate 3 artefacts × 7 sectors for the public /demo page'
        f'</div></div>'
        f'<div class="card" style="margin-bottom:1.5rem;">'
        f'<div class="ch"><span class="ct">Demo Content DB Status</span></div>'
        f'<div class="cb" style="font-size:.82rem;padding:.85rem 1.25rem;">'
        f'{_demo_db_info}'
        f'</div></div>'
        f'<div class="card">'
        f'<div class="ch"><span class="ct">Recent Runs</span>'
        f'{"<span style=color:#60a5fa;font-size:.75rem;margin-left:.75rem;>● Running — page refreshing every 15 s</span>" if _has_running else ""}'
        f'</div>'
        f'<div style="overflow-x:auto;">'
        f'<table class="dt"><thead><tr>'
        f'<th>Stage</th><th>Triggered by</th><th>Started</th><th>Finished</th>'
        f'<th>Status</th><th>Summary</th>'
        f'</tr></thead><tbody>'
        f'{run_rows or "<tr><td colspan=6 style=color:var(--muted);text-align:center;padding:2rem>No runs recorded yet</td></tr>"}'
        f'</tbody></table></div></div>'
    )
    return _page("Admin — Pipeline", body, "admin-pipeline")


# ── Admin — Dashboard (existing, kept) ───────────────────────────────────────

@app.route("/admin")
@login_required
@admin_required
def admin_dash():
    cfg     = _load_cfg()
    clients = {u: d for u, d in cfg.get("clients", {}).items() if not d.get("is_admin")}
    wl      = _latest_watchlist()

    # Pending leads count
    try:
        pending_leads = db.fetchone(
            "SELECT COUNT(*) AS n FROM leads WHERE status='enquiry'"
        ) or {}
        n_pending = pending_leads.get("n", 0)
    except Exception:
        n_pending = "—"

    # Scheduler status — latest run per stage
    _SCHED_STAGES = [
        ("layer1",               "Layer 1 Pipeline"),
        ("backfill_overview",    "Overview Text Backfill"),
        ("layer2",               "Layer 2 Intelligence"),
        ("fix_bidder_mismatches","Bidder Mismatch Fix"),
        ("watch_brief",          "Watch Brief"),
    ]
    try:
        _sched_rows = db.fetchall(
            "SELECT DISTINCT ON (stage) stage, status, started_at, finished_at, summary "
            "FROM pipeline_runs "
            "WHERE stage = ANY(%s) "
            "ORDER BY stage, started_at DESC",
            ([s for s, _ in _SCHED_STAGES],),
        )
        sched_map = {r["stage"]: r for r in _sched_rows}
    except Exception:
        sched_map = {}

    _STATUS_DOT = {
        "complete": ('<span style="color:#4ade80;font-size:.9rem;">&#9679;</span>', "Complete"),
        "running":  ('<span style="color:#60a5fa;font-size:.9rem;">&#9679;</span>', "Running"),
        "failed":   ('<span style="color:#f87171;font-size:.9rem;">&#9679;</span>', "Failed"),
    }
    sched_rows_html = ""
    for stage_key, stage_label in _SCHED_STAGES:
        r = sched_map.get(stage_key)
        if r:
            dot, status_text = _STATUS_DOT.get(r.get("status", ""), ('<span style="color:var(--muted);font-size:.9rem;">&#9679;</span>', r.get("status","?")))
            ts = str(r.get("started_at") or "")[:16].replace("T", " ")
            ft = str(r.get("finished_at") or "")[:16].replace("T", " ") or "—"
            summ = (r.get("summary") or "")[:120]
        else:
            dot, status_text = '<span style="color:var(--muted);font-size:.9rem;">&#9711;</span>', "Never run"
            ts = "—"
            ft = "—"
            summ = ""
        sched_rows_html += (
            f'<tr style="border-bottom:1px solid var(--border);">'
            f'<td style="padding:.45rem .75rem;font-size:.82rem;">{stage_label}</td>'
            f'<td style="padding:.45rem .75rem;font-size:.82rem;">{dot} {status_text}</td>'
            f'<td style="padding:.45rem .75rem;font-size:.78rem;color:var(--muted);">{ts}</td>'
            f'<td style="padding:.45rem .75rem;font-size:.78rem;color:var(--muted);">{ft}</td>'
            f'<td style="padding:.45rem .75rem;font-size:.75rem;color:var(--muted);max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="{summ}">{summ}</td>'
            f'</tr>'
        )

    rows = ""
    for username, data in clients.items():
        slug = data.get("artefact_slug") or _slug(data.get("display_name", username))
        p = len(_list_artefacts(slug, "*pursuit*.html"))
        c = len(_list_artefacts(slug, "competitor_*.html"))
        rows += (f'<tr><td><strong>{data.get("display_name",username)}</strong></td>'
                 f'<td style="color:var(--muted);">{username}</td>'
                 f'<td>{data.get("email","—")}</td>'
                 f'<td>{_plan_pill(data.get("plan","pursue"))}</td>'
                 f'<td>{p}</td><td>{c}</td>'
                 f'<td><a href="{url_for("admin_client",username=username)}" '
                 f'class="btn bg-out sm">Manage</a></td></tr>')

    leads_alert = ""
    if n_pending and int(str(n_pending)) > 0:
        leads_alert = (
            f'<div class="al al-in" style="margin-bottom:1.25rem;">'
            f'<strong>{n_pending} new lead{"s" if int(str(n_pending))!=1 else ""}</strong> '
            f'awaiting review. '
            f'<a href="{url_for("admin_leads")}" style="color:inherit;font-weight:700;">'
            f'Review now &rarr;</a></div>'
        )

    body = (f'<div class="ptitle">Admin Dashboard</div>'
            f'<div class="psub">BidEdge platform administration</div>'
            f'{leads_alert}'
            f'<div class="stats">'
            f'<div class="stat"><div class="sval">{len(clients)}</div><div class="slbl">Active clients</div></div>'
            f'<div class="stat"><div class="sval">{n_pending}</div><div class="slbl">Pending leads</div></div>'
            f'<div class="stat"><div class="sval">{"Today" if wl and wl.stem.endswith(_nzt_today()) else "—"}</div>'
            f'<div class="slbl">Last watchlist</div></div></div>'
            f'<div class="card">'
            f'<div class="ch"><span class="ct">Client Accounts</span>'
            f'<a href="{url_for("admin_clients_list")}" class="btn bg-out sm" style="margin-right:.5rem;">All clients</a>'
            f'<a href="{url_for("admin_add_client")}" class="btn bg-gold sm">+ Add client</a></div>'
            f'<table class="dt"><thead><tr>'
            f'<th>Name</th><th>Username</th><th>Email</th><th>Plan</th>'
            f'<th>Pursuits</th><th>Competitors</th><th></th>'
            f'</tr></thead><tbody>'
            f'{rows or "<tr><td colspan=7 style=color:var(--muted);text-align:center;padding:1.5rem>No clients yet</td></tr>"}'
            f'</tbody></table></div>'
            f'<div class="card" style="margin-top:1.5rem;">'
            f'<div class="ch"><span class="ct">Data Quality Audit</span>'
            f'<button id="qa-run-btn" class="btn bg-gold sm" onclick="runQaAudit()">Run QA Audit</button>'
            f'</div>'
            f'<div id="qa-results" style="padding:1rem 1.25rem 0.5rem;">'
            f'<p style="color:var(--muted);font-size:.83rem;">Click <strong>Run QA Audit</strong> to check '
            f'all watchlist notices and pursuit packages for data quality issues.</p>'
            f'</div>'
            f'</div>'
            f'<div class="card" style="margin-top:1.5rem;">'
            f'<div class="ch"><span class="ct">Scheduler Status</span>'
            f'<div style="display:flex;gap:.5rem;align-items:center;">'
            f'<button onclick="resetStuckJobs()" class="btn bg-out sm" style="color:#e07b39;border-color:#e07b39;">Reset Stuck Jobs</button>'
            f'<a href="{url_for("admin_pipeline")}" class="btn bg-out sm">Pipeline control &rarr;</a>'
            f'</div></div>'
            f'<div id="stuck-jobs-result" style="font-size:.78rem;padding:.3rem 1.25rem;"></div>'
            f'<table style="width:100%;border-collapse:collapse;">'
            f'<thead><tr style="color:var(--muted);font-size:.73rem;border-bottom:1px solid var(--border);">'
            f'<th style="text-align:left;padding:.4rem .75rem;">Job</th>'
            f'<th style="text-align:left;padding:.4rem .75rem;">Status</th>'
            f'<th style="text-align:left;padding:.4rem .75rem;">Started</th>'
            f'<th style="text-align:left;padding:.4rem .75rem;">Finished</th>'
            f'<th style="text-align:left;padding:.4rem .75rem;">Summary</th>'
            f'</tr></thead>'
            f'<tbody>{sched_rows_html}</tbody>'
            f'</table></div>'
            f'<div class="card" style="margin-top:1.5rem;">'
            f'<div class="ch"><span class="ct">On-Demand Operations</span></div>'
            f'<div style="padding:1rem 1.25rem;border-bottom:1px solid var(--border);">'
            f'<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:1rem;">'
            f'<div><strong style="font-size:.88rem;">Audit Firm Sectors</strong>'
            f'<div style="font-size:.78rem;color:var(--muted);margin-top:.2rem;">Check supplier_win_history for known IT or construction firm misclassifications</div></div>'
            f'<button id="afs-btn" class="btn bg-out sm" style="white-space:nowrap;flex-shrink:0;" onclick="runFirmAudit()">Run Audit</button></div>'
            f'<div id="afs-results" style="margin-top:.75rem;"></div></div>'
            f'<div style="padding:1rem 1.25rem;">'
            f'<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:1rem;">'
            f'<div><strong style="font-size:.88rem;">Delete Bad Packages</strong>'
            f'<div style="font-size:.78rem;color:var(--muted);margin-top:.2rem;">Remove pipeline_outputs entries with null, empty, or placeholder client names</div></div>'
            f'<button id="dbp-btn" class="btn bg-out sm" style="white-space:nowrap;flex-shrink:0;" onclick="previewBadPackages()">Preview</button></div>'
            f'<div id="dbp-results" style="margin-top:.75rem;"></div></div>'
            f'</div>'
            + _QA_AUDIT_JS + _FIX_OPS_JS)
    return _page("Admin — Groundwork", body, "admin")


@app.route("/admin/qa-audit", methods=["POST"])
@login_required
@admin_required
def admin_qa_audit():
    """Run all data-quality checks in-process and return JSON results."""
    import re as _re
    from datetime import date as _date, timedelta as _td
    from flask import jsonify as _jsonify

    today = _date.today()
    _STALE_DAYS = 30
    _PURSUIT_LOOKBACK = 90

    _DATE_PAT = _re.compile(
        r"\b(\d{1,2}[\s/\-]\w+[\s/\-]\d{4}|\w+\s+\d{1,2}[,\s]+\d{4}|\d{1,2}/\d{1,2}/\d{4})\b",
        _re.IGNORECASE,
    )
    _DATE_LABEL_PAT = _re.compile(
        r"(briefing|site\s+visit|hui|questions?\s+due|queries?\s+due|registration|"
        r"expressions?\s+of\s+interest|EOI|close|submission)",
        _re.IGNORECASE,
    )
    _PHYS = {"construction", "roading", "civil", "infrastructure", "FM"}
    _PHYS_SIG = {
        "building", "construct", "infrastructure", "roading", "maintenance",
        "civil", "facility", "upgrade", "installation", "earthworks", "structural",
        "bridge", "pavement", "drainage", "demolition", "fitout",
    }
    _INC_NOT_FOUND = [
        "no current system or provider identified",
        "no incumbent identified",
        "incumbent not identified",
        "no named incumbent",
        "not identifiable",
        "could not be identified",
        "incumbent: unknown",
        "incumbent: none",
    ]
    _BAD_CLIENTS = {"bidedge admin", "admin", ""}

    try:
        from bidders import SECTOR_EXCLUSION_MATRIX as _SEM
    except Exception:
        _SEM = {}

    findings: list[dict] = []

    def _add(check, notice_id, title, description):
        findings.append({"check": check, "notice_id": str(notice_id),
                         "title": str(title or ""), "description": str(description or "")})

    def _kw_hits(sector, text):
        kws = config.SECTOR_KEYWORDS.get(sector, [])
        tl = text.lower()
        return sum(1 for kw in kws if kw.lower() in tl)

    def _has_date_refs(ov):
        if not ov:
            return False
        labels = list(_DATE_LABEL_PAT.finditer(ov))
        dates  = list(_DATE_PAT.finditer(ov))
        return any(abs(lm.start()-dm.start()) < 200 for lm in labels for dm in dates)

    # ── Check 1: Bidder sector mismatch ──────────────────────────────────────
    try:
        rows = db.fetchall(
            """
            SELECT bp.notice_id, r.title AS notice_title, p.sector_tag AS notice_sector,
                   r.title || ' ' || COALESCE(r.description,'') AS combined_text,
                   bp.firm_name, wh.primary_sector AS firm_sector
              FROM bidder_pool bp
              JOIN parsed_notices p  ON p.notice_id = bp.notice_id
              JOIN raw_notices r     ON r.notice_id = bp.notice_id
              LEFT JOIN supplier_win_history wh ON wh.supplier_name = bp.firm_name
             WHERE bp.match_type = 'mbie_evidence'
               AND (r.close_date IS NULL OR r.close_date >= CURRENT_DATE)
               AND EXISTS (
                   SELECT 1 FROM scored_notices s
                    WHERE s.notice_id = bp.notice_id
                      AND (s.composite_score >= %s OR r.category_raw ILIKE '%%advance%%')
               )
             ORDER BY bp.notice_id, bp.firm_name
            """,
            (config.PRIORITY_THRESHOLD,),
        )
        for row in rows:
            fs = (row.get("firm_sector") or "").lower().strip()
            ns = (row.get("notice_sector") or "other").lower().strip()
            ct = (row.get("combined_text") or "").lower()
            reason = ""
            if fs and fs in _SEM.get(ns, set()):
                reason = (f"Firm sector '{fs}' excluded from notice sector "
                          f"'{ns}' by exclusion matrix")
            elif (ns in ("other", "unknown", "") and fs in _PHYS
                  and not any(sig in ct for sig in _PHYS_SIG)):
                reason = (f"Physical works firm (sector '{fs}') in unclassified "
                          f"notice with no construction keywords")
            if reason:
                _add("Bidder sector mismatch", row["notice_id"],
                     row.get("notice_title"), f"{row['firm_name']} — {reason}")
    except Exception as exc:
        logger.warning("QA check 1 failed: %s", exc)

    # ── Check 2: Overview text missing ───────────────────────────────────────
    try:
        rows = db.fetchall(
            """
            SELECT r.notice_id, r.title
              FROM raw_notices r
              JOIN scored_notices s ON s.notice_id = r.notice_id
             WHERE (r.overview_text IS NULL OR TRIM(r.overview_text) = '')
               AND (r.close_date IS NULL OR r.close_date >= CURRENT_DATE)
               AND (s.composite_score >= %s OR r.category_raw ILIKE '%%advance%%')
             ORDER BY r.notice_id
            """,
            (config.PRIORITY_THRESHOLD,),
        )
        for row in rows:
            _add("Overview text missing", row["notice_id"], row["title"],
                 "overview_text is null/empty — enrichment and date extraction will fail")
    except Exception as exc:
        logger.warning("QA check 2 failed: %s", exc)

    # ── Check 3: Key dates in text but fields null ────────────────────────────
    try:
        rows = db.fetchall(
            """
            SELECT r.notice_id, r.title, r.overview_text
              FROM raw_notices r
              JOIN parsed_notices p  ON p.notice_id = r.notice_id
              JOIN scored_notices s  ON s.notice_id = r.notice_id
             WHERE (r.overview_text IS NOT NULL AND TRIM(r.overview_text) != '')
               AND p.briefing_date IS NULL
               AND p.questions_deadline IS NULL
               AND p.registration_deadline IS NULL
               AND (r.close_date IS NULL OR r.close_date >= CURRENT_DATE)
               AND (s.composite_score >= %s OR r.category_raw ILIKE '%%advance%%')
             ORDER BY r.notice_id
            """,
            (config.PRIORITY_THRESHOLD,),
        )
        for row in rows:
            ov = row.get("overview_text") or ""
            if _has_date_refs(ov):
                m = _DATE_LABEL_PAT.search(ov)
                snip = ov[max(0, m.start()-20): m.start()+80].replace("\n"," ").strip() if m else ""
                _add("Key dates in text but fields null", row["notice_id"], row["title"],
                     f"overview_text mentions dates (e.g. «{snip[:60]}») "
                     f"but briefing/questions/registration date fields all null")
    except Exception as exc:
        logger.warning("QA check 3 failed: %s", exc)

    # ── Check 4: Sector classification suspect ────────────────────────────────
    try:
        rows = db.fetchall(
            """
            SELECT r.notice_id, r.title, r.description, p.sector_tag
              FROM raw_notices r
              JOIN parsed_notices p ON p.notice_id = r.notice_id
              JOIN scored_notices s ON s.notice_id = r.notice_id
             WHERE p.sector_tag IS NOT NULL
               AND p.sector_tag NOT IN ('other','unknown')
               AND (r.close_date IS NULL OR r.close_date >= CURRENT_DATE)
               AND (s.composite_score >= %s OR r.category_raw ILIKE '%%advance%%')
             ORDER BY r.notice_id
            """,
            (config.PRIORITY_THRESHOLD,),
        )
        for row in rows:
            sector = row["sector_tag"]
            if sector not in config.SECTOR_KEYWORDS:
                continue
            text = (row.get("title") or "") + " " + (row.get("description") or "")
            if _kw_hits(sector, text) == 0:
                best_other = max(
                    ((s, _kw_hits(s, text)) for s in config.SECTOR_KEYWORDS if s != sector),
                    key=lambda x: x[1], default=("none", 0),
                )
                note = (f"; '{best_other[0]}' has {best_other[1]} keyword hits"
                        if best_other[1] >= 2 else "")
                _add("Sector classification suspect", row["notice_id"], row["title"],
                     f"Tagged '{sector}' but 0 sector keywords in title/description{note}")
    except Exception as exc:
        logger.warning("QA check 4 failed: %s", exc)

    # ── Check 5: Stale enrichment ─────────────────────────────────────────────
    try:
        cutoff = today + _td(days=_STALE_DAYS)
        rows = db.fetchall(
            """
            SELECT r.notice_id, r.title, r.close_date,
                   p.days_until_close, s.composite_score
              FROM raw_notices r
              JOIN parsed_notices p    ON p.notice_id = r.notice_id
              JOIN scored_notices s    ON s.notice_id = r.notice_id
              LEFT JOIN enriched_notices e ON e.notice_id = r.notice_id
             WHERE e.notice_id IS NULL
               AND r.close_date IS NOT NULL
               AND r.close_date >= CURRENT_DATE
               AND r.close_date <= %s
               AND (s.composite_score >= %s OR r.category_raw ILIKE '%%advance%%')
             ORDER BY r.close_date ASC
            """,
            (cutoff, config.PRIORITY_THRESHOLD),
        )
        for row in rows:
            _add("Stale enrichment", row["notice_id"], row["title"],
                 f"Closes {row.get('close_date')} ({row.get('days_until_close')} days) "
                 f"— not enriched. Score: {row.get('composite_score','?')}")
    except Exception as exc:
        logger.warning("QA check 5 failed: %s", exc)

    # ── Check 6: Pursuit bad client name ─────────────────────────────────────
    try:
        cutoff6 = today - _td(days=_PURSUIT_LOOKBACK)
        rows = db.fetchall(
            """
            SELECT id, filename, client_slug, notice_id, client_name, output_type
              FROM pipeline_outputs
             WHERE output_type IN ('pursuit_package','pursuit_package_full')
               AND run_date >= %s AND content IS NOT NULL
             ORDER BY run_date DESC
            """,
            (cutoff6,),
        )
        for row in rows:
            cname = (row.get("client_name") or "").strip()
            if cname.lower() in _BAD_CLIENTS:
                _add("Pursuit: bad client name",
                     row.get("notice_id") or row["filename"], row["filename"],
                     f"client_name='{cname}' (slug: {row.get('client_slug','?')}) "
                     f"— admin-generated placeholder")
    except Exception as exc:
        logger.warning("QA check 6 failed: %s", exc)

    # ── Check 7: Pursuit incumbent not found ──────────────────────────────────
    try:
        cutoff7 = today - _td(days=_PURSUIT_LOOKBACK)
        rows = db.fetchall(
            """
            SELECT id, filename, client_slug, notice_id, client_name, output_type, content
              FROM pipeline_outputs
             WHERE output_type IN ('pursuit_package','pursuit_package_full')
               AND run_date >= %s AND content IS NOT NULL
             ORDER BY run_date DESC
            """,
            (cutoff7,),
        )
        for row in rows:
            content_lower = (row.get("content") or "").lower()
            if any(pat in content_lower for pat in _INC_NOT_FOUND):
                cname = row.get("client_name") or row.get("client_slug") or "?"
                _add("Pursuit: incumbent not identified",
                     row.get("notice_id") or row["filename"], row["filename"],
                     f"Package for '{cname}' — incumbent assessment shows no "
                     f"system/provider identified")
    except Exception as exc:
        logger.warning("QA check 7 failed: %s", exc)

    # ── Check 8: Pursuit type/filename consistency ────────────────────────────
    try:
        cutoff8 = today - _td(days=_PURSUIT_LOOKBACK)
        rows = db.fetchall(
            """
            SELECT id, filename, client_slug, notice_id, client_name, output_type
              FROM pipeline_outputs
             WHERE output_type IN ('pursuit_package','pursuit_package_full')
               AND run_date >= %s AND content IS NOT NULL
             ORDER BY run_date DESC
            """,
            (cutoff8,),
        )
        valid_types = {"pursuit_package", "pursuit_package_full"}
        for row in rows:
            ot = row.get("output_type") or ""
            fn = row.get("filename") or ""
            cname = row.get("client_name") or row.get("client_slug") or "?"
            if ot not in valid_types:
                _add("Pursuit: unexpected output_type",
                     row.get("notice_id") or fn, fn,
                     f"output_type='{ot}' for '{cname}'")
            is_full_fn = "_full" in fn.lower() or "full_analysis" in fn.lower()
            if ot == "pursuit_package_full" and not is_full_fn:
                _add("Pursuit: type/filename mismatch",
                     row.get("notice_id") or fn, fn,
                     f"output_type=full but filename has no 'full' marker: '{fn}'")
            elif ot == "pursuit_package" and is_full_fn:
                _add("Pursuit: type/filename mismatch",
                     row.get("notice_id") or fn, fn,
                     f"output_type=public but filename suggests full analysis: '{fn}'")
    except Exception as exc:
        logger.warning("QA check 8 failed: %s", exc)

    # ── Counts for "no issues" message ────────────────────────────────────────
    try:
        nc_row = db.fetchone(
            """
            SELECT COUNT(DISTINCT s.notice_id) AS n
              FROM scored_notices s
              JOIN raw_notices r ON r.notice_id = s.notice_id
             WHERE (s.composite_score >= %s OR r.category_raw ILIKE '%%advance%%')
               AND (r.close_date IS NULL OR r.close_date >= CURRENT_DATE)
            """,
            (config.PRIORITY_THRESHOLD,),
        )
        notice_count = int((nc_row or {}).get("n") or 0)
    except Exception:
        notice_count = 0

    try:
        pc_row = db.fetchone(
            """
            SELECT COUNT(*) AS n FROM pipeline_outputs
             WHERE output_type IN ('pursuit_package','pursuit_package_full')
               AND run_date >= %s AND content IS NOT NULL
            """,
            (today - _td(days=_PURSUIT_LOOKBACK),),
        )
        pursuit_count = int((pc_row or {}).get("n") or 0)
    except Exception:
        pursuit_count = 0

    # ── Build structured response ─────────────────────────────────────────────
    grouped: dict[str, list[dict]] = {}
    for f in findings:
        grouped.setdefault(f["check"], []).append(
            {"notice_id": f["notice_id"], "title": f["title"],
             "description": f["description"]}
        )
    summary = {check: len(items) for check, items in grouped.items()}

    return _jsonify({
        "ok": True,
        "timestamp": today.strftime("%Y-%m-%d") + " " + __import__("datetime").datetime.now().strftime("%H:%M:%S"),
        "notice_count": notice_count,
        "pursuit_count": pursuit_count,
        "total_issues": len(findings),
        "grouped": grouped,
        "summary": summary,
    })


@app.route("/admin/fix-bidder-mismatches", methods=["POST"])
@login_required
@admin_required
def admin_fix_bidder_mismatches():
    """Preview or apply deletion + re-inference of sector-mismatched bidder records."""
    from flask import jsonify as _jfy

    payload = request.get_json(silent=True) or {}
    action  = payload.get("action", "preview")

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
    # Engineering consultancies / environmental firms that legitimately span sector types.
    # Never purged wholesale — they must be reviewed and overridden individually via
    # FIRM_SECTOR_OVERRIDES in bidders.py if reclassification is needed.
    _EXCLUDED_FIRMS = {
        "beca", "beca limited", "stantec nz", "stantec new zealand",
        "morphum environmental",
    }

    try:
        from bidders import SECTOR_EXCLUSION_MATRIX as _SEM
    except Exception:
        _SEM = {}

    def _is_mismatch(firm_sector, notice_sector, notice_text):
        fs   = (firm_sector  or "").lower().strip()
        ns   = (notice_sector or "other").lower().strip()
        txt  = notice_text.lower()
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

    try:
        rows = db.fetchall(
            """
            SELECT bp.notice_id, r.title AS notice_title,
                   p.sector_tag AS notice_sector,
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
             ORDER BY bp.notice_id, bp.firm_name
            """,
            (config.PRIORITY_THRESHOLD,),
        )

        flagged: dict = {}
        for row in rows:
            nid = row["notice_id"]
            firm_lower = (row["firm_name"] or "").lower().strip()
            if firm_lower in _EXCLUDED_FIRMS:
                continue
            if _is_mismatch(row.get("firm_sector"), row.get("notice_sector"),
                            row.get("combined_text") or ""):
                if nid not in flagged:
                    flagged[nid] = {"notice_id": nid, "title": row.get("notice_title") or "",
                                    "bad_firms": []}
                firm_info = (row["firm_name"] + " (sector: "
                             + str(row.get("firm_sector") or "unknown") + ")")
                flagged[nid]["bad_firms"].append(firm_info)

        if action == "preview":
            notices = list(flagged.values())
            total_records = sum(len(v["bad_firms"]) for v in notices)
            return _jfy({"ok": True, "count": len(notices),
                         "total_records": total_records, "notices": notices})

        # action == "fix"
        affected_ids = list(flagged.keys())
        if not affected_ids:
            return _jfy({"ok": True, "deleted": 0, "stored": 0, "empty": 0, "failed": 0})

        _excl_list = list(_EXCLUDED_FIRMS)
        db.execute(
            """
            DELETE FROM bidder_pool
             WHERE notice_id = ANY(%s)
               AND match_type IN ('mbie_evidence', 'csv_inferred')
               AND LOWER(firm_name) != ALL(%s)
            """,
            (affected_ids, _excl_list),
        )

        notice_rows = db.fetchall(
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

        from bidders import score_bidders_for_notice as _sbfn, _store_bidders, load_bidders
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
            except Exception as exc:
                logger.warning("admin_fix_bidder_mismatches: %s — %s", nid2, exc)
                failed += 1

        return _jfy({"ok": True, "deleted": len(affected_ids),
                     "stored": stored, "empty": empty, "failed": failed})

    except Exception as exc:
        logger.exception("admin_fix_bidder_mismatches: %s", exc)
        return _jfy({"ok": False, "error": str(exc)})


@app.route("/admin/audit-firm-sectors", methods=["POST"])
@login_required
@admin_required
def admin_audit_firm_sectors():
    """Read-only check for known IT/construction firms misclassified in supplier_win_history."""
    from flask import jsonify as _jfy

    _KNOWN_ICT = {
        "fusion5", "empired", "revolent", "datacom", "spark nz", "gen-i",
        "unisys", "hewlett packard", "hp", "microsoft", "ibm nz", "ibm",
        "cisco", "oracle", "sap", "accenture", "wipro", "infosys",
        "theta", "provoke", "solnet", "jade software", "intergen",
        "dimension data", "ntt", "computacenter", "logicalis",
        "axon networks", "psi", "tait communications",
        "dxc", "dxc technology", "fujitsu", "tata",
        "assurity", "beca ict", "pricewaterhousecoopers ict",
        "kpmg ict", "deloitte digital",
    }
    _KNOWN_PHYS = {
        "fulton hogan", "downer", "heb construction", "higgins",
        "mcconnell dowell", "fletcher construction", "cpb contractors",
        "laing o'rourke", "naylor love", "arrow international",
        "hawkins", "leighs construction", "citycare", "mwh",
        "jacobs", "beca infrastructure", "stantec", "aecom",
    }

    try:
        ict_rows = db.fetchall(
            """
            SELECT supplier_name, primary_sector, total_wins
              FROM supplier_win_history
             WHERE primary_sector NOT IN ('ICT','advisory','other')
               AND total_wins >= 1
             ORDER BY total_wins DESC
            """
        )
        misclassified_ict = []
        for r in ict_rows:
            nl = (r["supplier_name"] or "").lower()
            if any(k in nl for k in _KNOWN_ICT):
                misclassified_ict.append({
                    "name": r["supplier_name"],
                    "sector": r["primary_sector"],
                    "wins": r["total_wins"],
                })

        phys_rows = db.fetchall(
            """
            SELECT supplier_name, primary_sector, total_wins
              FROM supplier_win_history
             WHERE primary_sector IN ('ICT','advisory','health')
               AND total_wins >= 2
             ORDER BY total_wins DESC
             LIMIT 100
            """
        )
        misclassified_physical = []
        for r in phys_rows:
            nl = (r["supplier_name"] or "").lower()
            if any(k in nl for k in _KNOWN_PHYS):
                misclassified_physical.append({
                    "name": r["supplier_name"],
                    "sector": r["primary_sector"],
                    "wins": r["total_wins"],
                })

        return _jfy({
            "ok": True,
            "misclassified_ict": misclassified_ict,
            "misclassified_physical": misclassified_physical,
        })

    except Exception as exc:
        logger.exception("admin_audit_firm_sectors: %s", exc)
        return _jfy({"ok": False, "error": str(exc)})


@app.route("/admin/reset-stuck-jobs", methods=["POST"])
@login_required
@admin_required
def admin_reset_stuck_jobs():
    """Mark any pipeline_runs row stuck in 'running' for over 2h as failed."""
    from flask import jsonify as _jfy
    try:
        result = db.execute(
            """
            UPDATE pipeline_runs
               SET status = 'failed',
                   finished_at = NOW(),
                   summary = 'Manually reset via admin panel'
             WHERE status = 'running'
               AND started_at < NOW() - INTERVAL '2 hours'
            """,
        )
        count = result.rowcount if result and hasattr(result, "rowcount") else 0
        return _jfy({"ok": True, "reset": count})
    except Exception as exc:
        logger.exception("admin_reset_stuck_jobs: %s", exc)
        return _jfy({"ok": False, "error": str(exc)})


@app.route("/admin/apply-ict-reclassifications", methods=["POST"])
@login_required
@admin_required
def admin_apply_ict_reclassifications():
    """Write ICT firm sector overrides to the firm_sector_overrides DB table."""
    from flask import jsonify as _jfy

    _KNOWN_ICT = {
        "fusion5", "empired", "revolent", "datacom", "spark nz", "gen-i",
        "unisys", "hewlett packard", "hp", "microsoft", "ibm nz", "ibm",
        "cisco", "oracle", "sap", "accenture", "wipro", "infosys",
        "theta", "provoke", "solnet", "jade software", "intergen",
        "dimension data", "ntt", "computacenter", "logicalis",
        "axon networks", "psi", "tait communications",
        "dxc", "dxc technology", "fujitsu", "tata",
        "assurity", "beca ict", "pricewaterhousecoopers ict",
        "kpmg ict", "deloitte digital",
    }

    try:
        # Ensure table exists (idempotent)
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS firm_sector_overrides (
                firm_name_lower TEXT PRIMARY KEY,
                sector          TEXT NOT NULL,
                added_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

        ict_rows = db.fetchall(
            """
            SELECT supplier_name, primary_sector
              FROM supplier_win_history
             WHERE primary_sector NOT IN ('ICT','advisory','other')
               AND total_wins >= 1
             ORDER BY total_wins DESC
            """
        )

        applied = []
        for r in ict_rows:
            nl = (r["supplier_name"] or "").lower()
            if any(k in nl for k in _KNOWN_ICT):
                db.execute(
                    """
                    INSERT INTO firm_sector_overrides (firm_name_lower, sector)
                    VALUES (%s, 'ICT')
                    ON CONFLICT (firm_name_lower) DO UPDATE SET sector = 'ICT'
                    """,
                    (nl,),
                )
                applied.append(r["supplier_name"])

        # Invalidate the bidders module cache so new overrides load immediately
        import sys
        if "bidders" in sys.modules:
            del sys.modules["bidders"]

        logger.info("admin_apply_ict_reclassifications: applied %d overrides", len(applied))
        return _jfy({"ok": True, "applied": len(applied), "firms": applied[:20]})
    except Exception as exc:
        logger.exception("admin_apply_ict_reclassifications: %s", exc)
        return _jfy({"ok": False, "error": str(exc)})


@app.route("/admin/backfill-overview-text", methods=["POST"])
@login_required
@admin_required
def admin_backfill_overview_text():
    """Preview or start background GETS re-scrape to populate null overview_text."""
    import threading as _thr
    from flask import jsonify as _jfy

    payload = request.get_json(silent=True) or {}
    action  = payload.get("action", "preview")

    try:
        rows = db.fetchall(
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
            (config.PRIORITY_THRESHOLD,),
        )

        if action == "preview":
            preview = [
                {
                    "notice_id": r["notice_id"],
                    "title": (r["title"] or "")[:70],
                    "close_date": str(r.get("close_date") or ""),
                }
                for r in rows[:30]
            ]
            return _jfy({
                "ok": True, "count": len(rows), "preview": preview,
                "status": dict(_BACKFILL_OVERVIEW_STATUS),
            })

        # action == "run"
        if _BACKFILL_OVERVIEW_STATUS.get("running"):
            return _jfy({"ok": False,
                         "error": "Backfill already running — check Railway logs for progress."})

        if not rows:
            return _jfy({"ok": True,
                         "message": "Nothing to backfill — all active notices have overview_text."})

        _BACKFILL_OVERVIEW_STATUS.update({
            "running": True, "done": 0, "total": len(rows), "errors": 0,
            "started": datetime.now().strftime("%H:%M:%S"),
        })

        def _run_backfill(notice_rows):
            import time as _time
            try:
                from ingestion import _fetch_notice_detail
                from parsing import extract_key_dates
                for i, row in enumerate(notice_rows, 1):
                    nid = row["notice_id"]
                    try:
                        nd = dict(row)
                        nd = _fetch_notice_detail(nd)
                        overview = nd.get("overview_text") or ""
                        db.execute(
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
                                db.execute(
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
                        _BACKFILL_OVERVIEW_STATUS["done"] = i
                        _time.sleep(1.5)
                    except Exception as exc:
                        logger.warning("backfill_overview: %s — %s", nid, exc)
                        _BACKFILL_OVERVIEW_STATUS["errors"] += 1
                        _BACKFILL_OVERVIEW_STATUS["done"] = i
            finally:
                _BACKFILL_OVERVIEW_STATUS["running"] = False
                logger.info(
                    "backfill_overview: complete — %d done, %d errors",
                    _BACKFILL_OVERVIEW_STATUS["done"],
                    _BACKFILL_OVERVIEW_STATUS["errors"],
                )

        _thr.Thread(
            target=_run_backfill, args=(rows,), daemon=True, name="backfill-overview"
        ).start()

        n = len(rows)
        return _jfy({
            "ok": True, "started": n,
            "message": ("Backfill started for " + str(n) + " notices. "
                        "Check Railway logs for progress. "
                        "Re-run Preview to see remaining count."),
        })

    except Exception as exc:
        logger.exception("admin_backfill_overview_text: %s", exc)
        _BACKFILL_OVERVIEW_STATUS["running"] = False
        return _jfy({"ok": False, "error": str(exc)})


@app.route("/admin/delete-bad-packages", methods=["POST"])
@login_required
@admin_required
def admin_delete_bad_packages():
    """Preview or delete pipeline_outputs with null/placeholder client names."""
    from flask import jsonify as _jfy

    _BAD = {"bidedge admin", "admin", "test", "demo", "placeholder", ""}
    payload = request.get_json(silent=True) or {}
    action  = payload.get("action", "preview")

    try:
        rows = db.fetchall(
            """
            SELECT id, output_type, client_name, client_slug, notice_id, run_date
              FROM pipeline_outputs
             WHERE output_type NOT IN ('demo_html', 'demo_pdf')
               AND (client_name IS NULL OR TRIM(LOWER(client_name)) = ANY(%s))
             ORDER BY run_date DESC NULLS LAST
            """,
            (list(_BAD),),
        )

        if action == "preview":
            packages = [
                {
                    "id":          r["id"],
                    "output_type": (r.get("output_type") or "")[:30],
                    "client_name": repr(r.get("client_name")),
                    "notice_id":   (r.get("notice_id") or "")[:20],
                    "run_date":    str(r.get("run_date") or "")[:10],
                }
                for r in rows
            ]
            return _jfy({"ok": True, "count": len(rows), "packages": packages})

        # action == "delete"
        if not rows:
            return _jfy({"ok": True, "deleted": 0})

        ids_to_delete = [r["id"] for r in rows]
        db.execute("DELETE FROM pipeline_outputs WHERE id = ANY(%s)", (ids_to_delete,))
        return _jfy({"ok": True, "deleted": len(ids_to_delete)})

    except Exception as exc:
        logger.exception("admin_delete_bad_packages: %s", exc)
        return _jfy({"ok": False, "error": str(exc)})


@app.route("/admin/clients/<username>")
@login_required
@admin_required
def admin_client(username: str):
    cfg  = _load_cfg()
    data = cfg.get("clients", {}).get(username)
    if not data: abort(404)
    u    = User(username, data)
    slug = u.slug
    pursuits = _list_artefacts(slug, "*pursuit*.html")
    comps    = _list_artefacts(slug, "competitor_*.html")
    briefs   = _list_artefacts(slug, "watch_brief_*.html")

    def ftable(files):
        if not files:
            return '<p style="color:var(--muted);font-size:.82rem;">None generated yet.</p>'
        rows = ""
        for f in files:
            vurl = url_for("serve_artefact_file", client_slug=slug, filepath=f["url_path"])
            rows += (f'<tr><td>{f["name"]}</td><td style="color:var(--muted);">{f["date"]}</td>'
                     f'<td>{f["size_kb"]}KB</td>'
                     f'<td><a href="{vurl}" target="_blank" class="btn bg-out sm">View</a></td></tr>')
        return (f'<table class="dt"><thead><tr>'
                f'<th>Name</th><th>Date</th><th>Size</th><th></th>'
                f'</tr></thead><tbody>{rows}</tbody></table>')

    sector_pills = ""
    for s in (u.preferred_sectors or []):
        sector_pills += (f'<span style="background:rgba(42,157,143,.15);color:var(--gold);'
                         f'border:1px solid rgba(42,157,143,.3);border-radius:4px;'
                         f'padding:.1rem .4rem;font-size:.7rem;font-weight:600;margin-right:.3rem;">'
                         f'{s}</span>')
    sector_display = sector_pills or '<span style="color:var(--muted);font-size:.78rem;">Sector-neutral (all sectors equal)</span>'

    body = (f'<div class="ptitle">{u.name}</div>'
            f'<div class="psub">@{username} &middot; {data.get("email","")}'
            f' &middot; Preferred sectors: {sector_display}</div>'
            f'<div class="card">'
            f'<div class="ch"><span class="ct">Generate Artefacts</span></div>'
            f'<div class="cb">'
            f'<form method="POST" action="{url_for("admin_generate")}">'
            f'<input type="hidden" name="username" value="{username}">'
            f'<div style="display:flex;gap:.75rem;flex-wrap:wrap;align-items:flex-end;">'
            f'<div class="fg" style="margin:0;flex:1;min-width:180px;">'
            f'<label class="fl">Notice ID (pursuit)</label>'
            f'<input name="notice_id" class="fc2" placeholder="e.g. 34060392"></div>'
            f'<div class="fg" style="margin:0;">'
            f'<label class="fl">Type</label>'
            f'<select name="atype" class="fc2">'
            f'<option value="pursuit">Pursuit package</option>'
            f'<option value="brief">Watch brief</option>'
            f'<option value="competitor">Competitor profile</option>'
            f'</select></div>'
            f'<div class="fg" style="margin:0;flex:1;min-width:180px;">'
            f'<label class="fl">Competitor name</label>'
            f'<input name="competitor_name" class="fc2" placeholder="e.g. Fulton Hogan"></div>'
            f'<div class="fg" style="margin:0;flex:1;min-width:200px;">'
            f'<label class="fl">Sector context (competitor only)</label>'
            f'<input name="sector_context" class="fc2" placeholder="e.g. facilities management"></div>'
            f'<button type="submit" class="btn bg-gold">Generate</button>'
            f'</div></form></div></div>'
            f'<div class="card"><div class="ch"><span class="ct">Pursuit Packages ({len(pursuits)})</span></div>'
            f'<div class="cb">{ftable(pursuits)}</div></div>'
            f'<div class="card"><div class="ch"><span class="ct">Competitor Profiles ({len(comps)})</span></div>'
            f'<div class="cb">{ftable(comps)}</div></div>'
            f'<div class="card"><div class="ch"><span class="ct">Watch Briefs ({len(briefs)})</span></div>'
            f'<div class="cb">{ftable(briefs)}</div></div>')
    return _page(f"Admin — {u.name}", body, "admin")


@app.route("/admin/generate", methods=["POST"])
@login_required
@admin_required
def admin_generate():
    import threading as _thr
    username  = request.form.get("username", "")
    atype     = request.form.get("atype", "pursuit")
    notice_id = request.form.get("notice_id", "").strip()
    comp_name = request.form.get("competitor_name", "").strip()
    cfg       = _load_cfg()
    client_data = cfg.get("clients", {}).get(username, {})
    cname     = client_data.get("display_name", username)
    client_sectors = (
        client_data.get("preferred_sectors")
        or client_data.get("sectors")
        or []
    )
    try:
        if atype == "pursuit" and notice_id:
            # Dispatch in background — web search + LLM synthesis takes 5+ minutes
            # and will exceed Gunicorn's worker timeout if run synchronously.
            def _run_pursuit(nid, cname_, sects):
                try:
                    from pursuit_package import generate_pursuit_package
                    generate_pursuit_package(nid, cname_, preferred_sectors=sects or None)
                    logger.info("admin_generate bg: done notice=%s client=%s", nid, cname_)
                except Exception as exc:
                    logger.exception("admin_generate bg: FAILED %s / %s: %s", nid, cname_, exc)
            _thr.Thread(
                target=_run_pursuit,
                args=(notice_id, cname, client_sectors or []),
                daemon=True,
                name=f"adm-pursuit-{notice_id[:12]}",
            ).start()
            _flash(f"Pursuit package generation started in background for {cname} / {notice_id}. Allow 3-5 min, then refresh.", "success")
        elif atype == "brief":
            from watch_brief import generate_watch_brief
            generate_watch_brief(cname, sectors=client_sectors or None)
            _flash(f"Generated brief for {cname}.", "success")
        elif atype == "competitor" and comp_name:
            from competitor_profile import generate_competitor_profile
            sector_ctx = request.form.get("sector_context", "").strip()
            if not sector_ctx:
                raise ValueError(
                    "Sector context is required for competitor profiles. "
                    "Add it in the 'Sector context' field."
                )
            generate_competitor_profile(
                comp_name, client_name=cname, sector_context=sector_ctx
            )
            _flash(f"Generated competitor profile for {comp_name}.", "success")
    except Exception as exc:
        logger.error("admin_generate: %s", exc)
        _flash(f"Generation failed: {exc}", "error")
    return redirect(url_for("admin_client", username=username))


@app.route("/admin/gen-bg", methods=["GET", "POST"])
@login_required
@admin_required
def admin_gen_bg():
    """
    Dispatch a pursuit package generation in a background thread and return
    immediately — avoids Railway's 60-second response timeout.
    GET  → show a simple form.
    POST → kick off generation, show confirmation.
    """
    import threading as _thr
    import traceback as _tb

    msg = ""
    if request.method == "POST":
        try:
            notice_id   = request.form.get("notice_id", "").strip()
            client_name = request.form.get("client_name", "").strip()
            sectors_raw = request.form.get("sectors", "").strip()
            preferred   = [s.strip() for s in sectors_raw.split(",") if s.strip()]

            if not notice_id or not client_name:
                msg = '<div class="al al-er">Notice ID and client name are required.</div>'
            else:
                def _run(nid, cname, sects):
                    try:
                        from pursuit_package import generate_pursuit_package, _artefact_dir
                        out = generate_pursuit_package(
                            notice_id=nid,
                            client_name=cname,
                            output_dir=_artefact_dir(cname),
                            preferred_sectors=sects or [],
                        )
                        logger.info("admin_gen_bg: done — %s", out)
                    except Exception as exc:
                        logger.exception("admin_gen_bg: FAILED %s / %s: %s", nid, cname, exc)

                _thr.Thread(
                    target=_run,
                    args=(notice_id, client_name, preferred),
                    daemon=True,
                    name=f"admin-gen-{notice_id[:12]}",
                ).start()
                msg = (
                    f'<div class="al al-ok">'
                    f'Generation started in background for <strong>{_safe(client_name)}</strong> '
                    f'/ notice <strong>{_safe(notice_id)}</strong>. '
                    f'Allow 3-5 minutes, then check '
                    f'<a href="/groundwork/pursuits" style="color:inherit;font-weight:700;">'
                    f'the pursuits page</a>.'
                    f'</div>'
                )
        except Exception as _exc:
            _trace = _tb.format_exc()
            logger.exception("admin_gen_bg POST handler error: %s", _exc)
            msg = (
                f'<div class="al al-er" style="white-space:pre-wrap;font-family:monospace;font-size:.75rem;">'
                f'<b>Diagnostic error (admin only):</b>\n{_safe(_trace)}'
                f'</div>'
            )

    body = (
        f'<div class="ptitle">Admin — Generate Pursuit Package (Background)</div>'
        f'{msg}'
        f'<div class="card" style="max-width:520px;">'
        f'<div class="ch"><span class="ct">Dispatch Generation</span></div>'
        f'<div class="cb">'
        f'<form method="POST">'
        f'<div class="fg"><label class="fl">Notice ID *</label>'
        f'<input name="notice_id" class="fc2" placeholder="e.g. 34118228" required></div>'
        f'<div class="fg"><label class="fl">Client name *</label>'
        f'<input name="client_name" class="fc2" placeholder="e.g. Pacific Transcription NZ" required></div>'
        f'<div class="fg"><label class="fl">Preferred sectors (comma-separated, optional)</label>'
        f'<input name="sectors" class="fc2" placeholder="e.g. ICT,other"></div>'
        f'<button type="submit" class="btn bg-gold">Generate in background &rarr;</button>'
        f'</form>'
        f'</div></div>'
    )
    return _page("Admin — Gen Background", body, "admin")


@app.route("/admin/add-client", methods=["GET", "POST"])
@login_required
@admin_required
def admin_add_client():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        name     = request.form.get("display_name", username)
        email    = request.form.get("email", "")
        is_admin = request.form.get("is_admin") == "on"
        sectors  = [s.strip() for s in request.form.get("sectors","").split(",") if s.strip()]
        if username and password:
            _add_user(username, password, name, email, is_admin, sectors)
            _flash(f"Client '{username}' created.", "success")
            return redirect(url_for("admin_dash"))
        _flash("Username and password required.", "error")
    body = (f'<div class="ptitle">Add Client</div>'
            f'<div class="card" style="max-width:500px;">'
            f'<div class="ch"><span class="ct">New client account</span></div>'
            f'<div class="cb"><form method="POST">'
            f'<div class="fg"><label class="fl">Username</label>'
            f'<input name="username" class="fc2" required></div>'
            f'<div class="fg"><label class="fl">Display name</label>'
            f'<input name="display_name" class="fc2"></div>'
            f'<div class="fg"><label class="fl">Email</label>'
            f'<input name="email" type="email" class="fc2"></div>'
            f'<div class="fg"><label class="fl">Password</label>'
            f'<input name="password" type="password" class="fc2" required></div>'
            f'<div class="fg"><label class="fl">Preferred sectors (comma-separated)</label>'
            f'<input name="sectors" class="fc2" placeholder="ICT, security"></div>'
            f'<div class="fg"><label style="display:flex;align-items:center;gap:.5rem;'
            f'font-size:.82rem;cursor:pointer;">'
            f'<input name="is_admin" type="checkbox"> Admin access</label></div>'
            f'<button type="submit" class="btn bg-gold">Create account</button>'
            f'</form></div></div>')
    return _page("Add Client — Admin", body, "admin")


# ── Sector review admin page ──────────────────────────────────────────────────

@app.route("/admin/sector-review", methods=["GET", "POST"])
@login_required
@admin_required
def admin_sector_review():
    """
    Human review queue for low-confidence sector classifications.
    GET  — list all notices flagged needs_sector_review=TRUE.
    POST — apply a manual correction for a single notice.
    """
    from sector_classifier import apply_human_correction, ALL_SECTORS

    flash_msg = ""

    if request.method == "POST":
        nid              = request.form.get("notice_id", "").strip()
        new_sector       = request.form.get("sector", "").strip()
        note             = request.form.get("note", "").strip()
        corrector        = current_user.username if hasattr(current_user, "username") else "admin"
        if nid and new_sector in ALL_SECTORS:
            ok = apply_human_correction(nid, new_sector, corrector, note)
            flash_msg = (
                f'<div class="al al-ok">Sector updated for notice {nid[:12]}… → {new_sector}</div>'
                if ok else
                f'<div class="al al-er">Failed to update notice {nid[:12]}…</div>'
            )
        else:
            flash_msg = '<div class="al al-er">Invalid notice ID or sector.</div>'

    # ── Fetch pending notices ─────────────────────────────────────────────────
    pending = db.fetchall(
        """
        SELECT
            p.notice_id,
            p.sector_tag,
            p.classification_method,
            p.classification_confidence,
            p.classification_reasoning,
            p.parsed_at,
            r.title,
            r.agency,
            r.description
          FROM parsed_notices p
          JOIN raw_notices r ON r.notice_id = p.notice_id
         WHERE p.needs_sector_review = TRUE
         ORDER BY p.parsed_at DESC
         LIMIT 100
        """
    )

    # ── Sector distribution (before/after awareness) ──────────────────────────
    dist = db.fetchall(
        """
        SELECT sector_tag, COUNT(*) AS n,
               SUM(CASE WHEN classification_method='keyword' THEN 1 ELSE 0 END) AS kw,
               SUM(CASE WHEN classification_method='claude'  THEN 1 ELSE 0 END) AS cl,
               SUM(CASE WHEN classification_method='human'   THEN 1 ELSE 0 END) AS hu,
               SUM(CASE WHEN classification_method IS NULL   THEN 1 ELSE 0 END) AS legacy
          FROM parsed_notices
         GROUP BY sector_tag
         ORDER BY n DESC
        """
    )

    # ── Sector options for dropdown ───────────────────────────────────────────
    sector_opts = "".join(
        f'<option value="{s}">{s}</option>' for s in sorted(ALL_SECTORS)
    )

    # ── Distribution table ────────────────────────────────────────────────────
    dist_rows = "".join(
        f'<tr><td>{r["sector_tag"] or "(null)"}</td>'
        f'<td style="text-align:right;font-weight:700;">{r["n"]}</td>'
        f'<td style="text-align:right;color:var(--muted);">{r["kw"] or 0}</td>'
        f'<td style="text-align:right;color:var(--muted);">{r["cl"] or 0}</td>'
        f'<td style="text-align:right;color:var(--muted);">{r["hu"] or 0}</td>'
        f'<td style="text-align:right;color:var(--muted);">{r["legacy"] or 0}</td>'
        f'</tr>'
        for r in dist
    )

    # ── Pending review cards ──────────────────────────────────────────────────
    if not pending:
        review_body = (
            '<div class="al al-ok" style="margin-top:1rem;">'
            '✓ No notices currently require sector review.</div>'
        )
    else:
        cards = []
        for r in pending:
            nid    = _esc(r["notice_id"])
            title  = _esc(r["title"] or "—")
            agency = _esc(r["agency"] or "—")
            sector = _esc(r["sector_tag"] or "other")
            method = _esc(r["classification_method"] or "legacy")
            conf   = _esc(r["classification_confidence"] or "—")
            reason = _esc((r["classification_reasoning"] or "")[:200])
            desc   = _esc((r["description"] or "")[:250])

            cards.append(
                f'<div class="card" style="margin-bottom:1rem;">'
                f'<div class="ch" style="display:flex;justify-content:space-between;align-items:center;">'
                f'<span class="ct">⚠ {title[:80]}</span>'
                f'<span style="font-size:.7rem;color:var(--muted);">{nid[:12]}…</span>'
                f'</div>'
                f'<div class="cb">'
                f'<div style="font-size:.78rem;color:var(--muted);margin-bottom:.75rem;">'
                f'{agency} &nbsp;·&nbsp; '
                f'Current sector: <strong style="color:var(--text);">{sector}</strong> &nbsp;·&nbsp; '
                f'Method: {method} &nbsp;·&nbsp; Confidence: {conf}'
                f'</div>'
                f'<div style="font-size:.78rem;color:var(--muted);margin-bottom:.75rem;'
                f'font-style:italic;">{reason}</div>'
                f'<div style="font-size:.75rem;color:var(--muted);margin-bottom:1rem;'
                f'line-height:1.55;max-height:4rem;overflow:hidden;">{desc}</div>'
                f'<form method="POST" style="display:flex;gap:.75rem;flex-wrap:wrap;align-items:flex-end;">'
                f'<input type="hidden" name="notice_id" value="{nid}">'
                f'<div>'
                f'<label class="fl">Correct sector</label>'
                f'<select name="sector" class="fc2" style="min-width:180px;">'
                f'<option value="{sector}" selected>{sector}</option>'
                f'{sector_opts}'
                f'</select>'
                f'</div>'
                f'<div style="flex:1;min-width:200px;">'
                f'<label class="fl">Note (optional)</label>'
                f'<input name="note" class="fc2" placeholder="reason for correction">'
                f'</div>'
                f'<button type="submit" class="btn bg-gold sm" style="align-self:flex-end;">'
                f'Save &rarr;</button>'
                f'</form>'
                f'</div></div>'
            )
        review_body = "".join(cards)

    body = (
        f'<div class="ptitle">Sector Review Queue</div>'
        f'<div class="psub">Low-confidence classifications flagged for manual verification</div>'
        f'{flash_msg}'

        # Stats bar
        f'<div style="display:flex;gap:1.5rem;flex-wrap:wrap;margin-bottom:1.5rem;">'
        f'<div class="card" style="flex:1;min-width:140px;padding:1rem 1.25rem;">'
        f'<div style="font-size:1.6rem;font-weight:800;color:var(--red);">{len(pending)}</div>'
        f'<div style="font-size:.72rem;color:var(--muted);text-transform:uppercase;'
        f'letter-spacing:.08em;">Pending review</div></div>'
        f'</div>'

        # Sector distribution
        f'<div class="card" style="margin-bottom:1.5rem;">'
        f'<div class="ch"><span class="ct">Sector distribution — all notices</span></div>'
        f'<div class="cb">'
        f'<table class="dt"><thead><tr>'
        f'<th>Sector</th><th style="text-align:right;">Total</th>'
        f'<th style="text-align:right;">Keyword</th>'
        f'<th style="text-align:right;">Claude</th>'
        f'<th style="text-align:right;">Human</th>'
        f'<th style="text-align:right;">Legacy</th>'
        f'</tr></thead><tbody>{dist_rows}</tbody></table>'
        f'</div></div>'

        # Review cards
        f'<div class="ptitle" style="font-size:1rem;margin-top:1rem;">Notices awaiting review</div>'
        f'{review_body}'
    )

    return _page("Sector Review — Admin", body, "admin")


# ── Intel Library admin page ─────────────────────────────────────────────────

@app.route("/intel")
@login_required
@admin_required
def intel_dash():
    """Strategic intelligence library — admin only."""
    from intel_library.scheduler_jobs import get_library_stats
    import json as _json

    stats = get_library_stats()

    # ── Section 1: Library overview ──────────────────────────────────────────
    last_refresh_str = "Never"
    if stats.get("last_refresh"):
        try:
            lr = stats["last_refresh"]
            if hasattr(lr, "strftime"):
                last_refresh_str = lr.strftime("%-d %b %Y %H:%M")
            else:
                last_refresh_str = str(lr)[:16]
        except Exception:
            pass

    s1 = (
        f'<div class="psub" style="margin-bottom:1.5rem;">Strategic intelligence document library — '
        f'{stats["sources_active"]} active sources, {stats["signals_total"]} signals extracted</div>'
        f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:1rem;margin-bottom:2rem;">'
        f'<div class="stat"><div class="sval">{stats["sources_active"]}</div>'
        f'<div class="sl">Active sources</div></div>'
        f'<div class="stat"><div class="sval">{stats["sources_total"]}</div>'
        f'<div class="sl">Total sources</div></div>'
        f'<div class="stat"><div class="sval">{stats["signals_total"]}</div>'
        f'<div class="sl">Signals extracted</div></div>'
        f'<div class="stat"><div class="sval">{stats["signals_30d"]}</div>'
        f'<div class="sl">Signals (30 days)</div></div>'
        f'<div class="stat"><div class="sval" style="color:#2a9d8f;">{stats["budget_signals"]}</div>'
        f'<div class="sl">Budget 2026 signals</div></div>'
        f'<div class="stat"><div class="sval" style="font-size:1rem;padding-top:.2rem;">'
        f'{last_refresh_str}</div>'
        f'<div class="sl">Last refresh</div></div>'
        f'</div>'
    )

    # ── Section 2: Source table ───────────────────────────────────────────────
    try:
        sources = db.fetchall(
            """
            SELECT s.id, s.title, s.short_name, s.publisher, s.url,
                   s.document_type, s.update_frequency, s.nz_relevance_score,
                   s.is_active, s.last_checked, s.notes,
                   c.name AS category_name,
                   COALESCE(u.total_references, 0) AS total_references,
                   u.avg_significance, u.last_used,
                   (SELECT COUNT(*) FROM intel_signals sig WHERE sig.source_id = s.id) AS signal_count,
                   (SELECT summary FROM intel_snapshots sn WHERE sn.source_id = s.id
                    ORDER BY sn.created_at DESC LIMIT 1) AS latest_summary
            FROM intel_sources s
            LEFT JOIN intel_categories c ON c.id = s.category_id
            LEFT JOIN v_source_usage_summary u ON u.source_id = s.id
            ORDER BY
                CASE WHEN s.short_name IN ('BEFU2026','Budget2026-Full') THEN 0 ELSE 1 END ASC,
                s.nz_relevance_score DESC NULLS LAST,
                s.title ASC
            """
        )
    except Exception as exc:
        logger.warning("Intel /intel source fetch failed: %s", exc)
        sources = []

    def _doc_badge(dt):
        colours = {
            "forecast": "#2a9d8f", "strategy": "#3b82f6", "policy": "#8b5cf6",
            "report": "#f59e0b", "guidance": "#10b981", "news": "#ef4444", "speech": "#ec4899",
        }
        bg = colours.get(dt, "#64748b")
        return f'<span style="background:{bg};color:#fff;padding:1px 7px;border-radius:4px;font-size:.72rem;font-weight:700;">{dt}</span>'

    def _relevance_dots(score):
        if not score:
            return "—"
        filled = "●" * score
        empty  = "○" * (10 - score)
        return f'<span style="color:#2a9d8f;font-size:.85rem;">{filled}</span><span style="color:#253d5c;font-size:.85rem;">{empty}</span>'

    rows_html = ""
    for src in sources:
        is_budget = (src.get("short_name") or "") in ("BEFU2026", "Budget2026-Full")
        row_style = 'background:rgba(42,157,143,.06);border-left:3px solid #2a9d8f;' if is_budget else ""
        active_badge = ('<span class="badge bg">ACTIVE</span>' if src.get("is_active")
                        else '<span class="badge br">INACTIVE</span>')
        budget_badge = '<span class="badge bg" style="margin-left:.25rem;">BUDGET&nbsp;2026</span>' if is_budget else ""
        title_cell = (
            f'<td style="max-width:300px;">'
            f'<a href="{src.get("url","#")}" target="_blank" rel="noopener" '
            f'style="color:var(--text);font-size:.88rem;line-height:1.35;">'
            f'{src.get("title","")[:80]}</a>'
            f'{budget_badge}</td>'
        )
        last_checked = "—"
        if src.get("last_checked"):
            try:
                lc = src["last_checked"]
                last_checked = lc.strftime("%-d %b") if hasattr(lc, "strftime") else str(lc)[:10]
            except Exception:
                pass
        last_used_str = "—"
        if src.get("last_used"):
            try:
                lu = src["last_used"]
                last_used_str = lu.strftime("%-d %b") if hasattr(lu, "strftime") else str(lu)[:10]
            except Exception:
                pass

        rows_html += (
            f'<tr style="{row_style}">'
            f'{title_cell}'
            f'<td><code style="font-size:.78rem;color:var(--gold);">{src.get("short_name") or "—"}</code></td>'
            f'<td style="font-size:.82rem;color:var(--muted);">{(src.get("publisher") or "")[:30]}</td>'
            f'<td style="font-size:.78rem;color:var(--muted);">{(src.get("category_name") or "")[:25]}</td>'
            f'<td>{_doc_badge(src.get("document_type",""))}</td>'
            f'<td style="font-size:.82rem;text-align:center;">{last_checked}</td>'
            f'<td style="text-align:center;">{src.get("signal_count",0)}</td>'
            f'<td style="text-align:center;">{src.get("total_references",0)}</td>'
            f'<td style="font-size:.82rem;text-align:center;">'
            f'{src.get("avg_significance") or "—"}</td>'
            f'<td style="font-size:.82rem;text-align:center;">{last_used_str}</td>'
            f'<td>{active_badge}</td>'
            f'</tr>'
        )
        # Expandable row for latest summary + top signals
        if src.get("latest_summary"):
            rows_html += (
                f'<tr style="{"background:rgba(42,157,143,.03);" if is_budget else ""}">'
                f'<td colspan="11" style="padding:.6rem 1rem;font-size:.82rem;color:var(--muted);">'
                f'<em>{src["latest_summary"][:300]}</em></td></tr>'
            )

    s2 = (
        f'<div class="card" style="margin-bottom:2rem;">'
        f'<div class="ch" style="display:flex;align-items:center;justify-content:space-between;">'
        f'<span style="font-weight:700;">Source Library</span>'
        f'<a href="/intel/run?job=weekly" class="btn bg-out sm" '
        f'style="font-size:.78rem;padding:.3rem .8rem;" '
        f'onclick="return confirm(\'Run full source refresh now?\')">Refresh all sources</a>'
        f'</div>'
        f'<div style="overflow-x:auto;">'
        f'<table class="dt" style="width:100%;font-size:.83rem;">'
        f'<thead><tr>'
        f'<th>Title</th><th>Short name</th><th>Publisher</th><th>Category</th>'
        f'<th>Type</th><th>Last checked</th><th>Signals</th>'
        f'<th>Uses in GW</th><th>Avg sig.</th><th>Last used</th><th>Status</th>'
        f'</tr></thead>'
        f'<tbody>{rows_html}</tbody></table></div></div>'
    )

    # Add source form
    s2 += (
        f'<div class="card" style="margin-bottom:2rem;">'
        f'<div class="ch"><span style="font-weight:700;">Add New Source</span></div>'
        f'<div style="padding:1.25rem;">'
        f'<form method="POST" action="/intel/add-source">'
        f'<div style="display:grid;grid-template-columns:1fr 1fr;gap:.75rem;">'
        f'<div class="fg"><label class="fl">Title *</label>'
        f'<input name="title" class="fc2" required placeholder="Document title"></div>'
        f'<div class="fg"><label class="fl">Short name</label>'
        f'<input name="short_name" class="fc2" placeholder="e.g. BEFU2026"></div>'
        f'<div class="fg"><label class="fl">Publisher</label>'
        f'<input name="publisher" class="fc2" placeholder="e.g. The Treasury"></div>'
        f'<div class="fg"><label class="fl">URL *</label>'
        f'<input name="url" class="fc2" required placeholder="https://..."></div>'
        f'<div class="fg"><label class="fl">PDF URL</label>'
        f'<input name="pdf_url" class="fc2" placeholder="https://...pdf"></div>'
        f'<div class="fg"><label class="fl">Document type *</label>'
        f'<select name="document_type" class="fc2">'
        f'<option value="policy">policy</option><option value="forecast">forecast</option>'
        f'<option value="strategy">strategy</option><option value="report">report</option>'
        f'<option value="guidance">guidance</option><option value="news">news</option>'
        f'<option value="speech">speech</option>'
        f'</select></div>'
        f'<div class="fg"><label class="fl">Update frequency</label>'
        f'<input name="update_frequency" class="fc2" placeholder="e.g. quarterly, annual"></div>'
        f'<div class="fg"><label class="fl">NZ relevance (1-10)</label>'
        f'<input name="nz_relevance_score" class="fc2" type="number" min="1" max="10" value="7"></div>'
        f'</div>'
        f'<div class="fg" style="margin-top:.75rem;"><label class="fl">Notes</label>'
        f'<textarea name="notes" class="fc2" rows="2" placeholder="Procurement context..."></textarea></div>'
        f'<button type="submit" class="btn bg-gold" style="margin-top:.75rem;">Add Source</button>'
        f'</form></div></div>'
    )

    # ── Section 3: Signal feed ────────────────────────────────────────────────
    try:
        recent_signals = db.fetchall(
            """
            SELECT sig.id, sig.signal_type, sig.signal_title, sig.signal_body,
                   sig.affected_sectors, sig.affected_agencies, sig.dollar_value,
                   sig.timeframe, sig.confidence, sig.extracted_at,
                   src.short_name, src.title AS source_title
            FROM v_active_signals sig
            JOIN intel_sources src ON src.id = sig.source_id
            ORDER BY sig.extracted_at DESC
            LIMIT 50
            """
        )
    except Exception:
        recent_signals = []

    def _conf_badge(c):
        clr = {"high": "#2a9d8f", "medium": "#f59e0b", "low": "#64748b"}.get(c, "#64748b")
        return f'<span style="background:{clr}22;color:{clr};padding:1px 7px;border-radius:4px;font-size:.72rem;font-weight:700;text-transform:uppercase;">{c}</span>'

    def _type_badge(t):
        icons = {
            "budget_increase": "💰", "policy_change": "📋",
            "new_initiative": "🚀", "risk": "⚠️", "opportunity": "🎯",
        }
        return icons.get(t, "•") + " " + t.replace("_", " ").title()

    def _fmt_nzd(v):
        if not v: return ""
        try:
            v = int(v)
            if v >= 1_000_000_000: return f"${v/1_000_000_000:.1f}B"
            if v >= 1_000_000: return f"${v/1_000_000:.0f}M"
            if v >= 1_000: return f"${v/1_000:.0f}K"
            return f"${v:,}"
        except Exception: return ""

    sig_cards = ""
    for sig in recent_signals:
        is_budget = (sig.get("short_name") or "") in ("BEFU2026", "Budget2026-Full", "FSR2026")
        border = "border-left:3px solid #2a9d8f;" if is_budget else "border-left:3px solid var(--border);"
        src_label = sig.get("short_name") or sig.get("source_title", "")[:40]
        sectors_str = ", ".join(sig.get("affected_sectors") or [])
        agencies_str = ", ".join(sig.get("affected_agencies") or [])
        dv_str = _fmt_nzd(sig.get("dollar_value"))
        extracted_str = ""
        if sig.get("extracted_at"):
            try:
                ea = sig["extracted_at"]
                extracted_str = ea.strftime("%-d %b %Y") if hasattr(ea, "strftime") else str(ea)[:10]
            except Exception:
                pass

        budget_tag = ('<span class="badge bg" style="margin-left:.4rem;font-size:.68rem;">BUDGET 2026</span>'
                      if is_budget else "")
        sig_cards += (
            f'<div class="nr" style="padding:.9rem 1rem;{border}">'
            f'<div style="display:flex;align-items:flex-start;gap:.75rem;">'
            f'<div style="flex:1;">'
            f'<div style="font-weight:700;font-size:.9rem;margin-bottom:.25rem;">'
            f'{sig.get("signal_title","")}{budget_tag}</div>'
            f'<div style="font-size:.82rem;color:var(--muted);margin-bottom:.5rem;">'
            f'{sig.get("signal_body","")[:250]}</div>'
            f'<div style="display:flex;flex-wrap:wrap;gap:.4rem;font-size:.75rem;color:var(--muted);">'
            + (f'<span>Sectors: <strong style="color:var(--text);">{sectors_str}</strong></span>' if sectors_str else "")
            + (f'<span>Agencies: <strong style="color:var(--text);">{agencies_str}</strong></span>' if agencies_str else "")
            + (f'<span>Value: <strong style="color:#2a9d8f;">{dv_str}</strong></span>' if dv_str else "")
            + (f'<span>Timeframe: {sig.get("timeframe","")}</span>' if sig.get("timeframe") else "")
            + f'<span>Source: <code style="color:var(--gold);">{src_label}</code></span>'
            + f'<span>{extracted_str}</span>'
            + f'</div></div>'
            f'<div style="flex-shrink:0;display:flex;flex-direction:column;align-items:flex-end;gap:.35rem;">'
            f'{_conf_badge(sig.get("confidence","medium"))}'
            f'<span style="font-size:.72rem;color:var(--muted);">{_type_badge(sig.get("signal_type",""))}</span>'
            f'</div></div></div>'
        )

    s3 = (
        f'<div class="card" style="margin-bottom:2rem;">'
        f'<div class="ch"><span style="font-weight:700;">Signal Feed</span>'
        f'<span style="font-size:.8rem;color:var(--muted);margin-left:.75rem;">50 most recent</span></div>'
        f'<div style="max-height:600px;overflow-y:auto;">'
        + (sig_cards if sig_cards else '<div style="padding:2rem;color:var(--muted);text-align:center;">No signals extracted yet. Run the extractor to populate.</div>')
        + f'</div></div>'
    )

    # ── Section 4: Usage log ──────────────────────────────────────────────────
    try:
        usage_rows = db.fetchall(
            """
            SELECT u.id, u.used_in, u.usage_type, u.significance_score, u.used_at,
                   src.short_name, src.title AS source_title
            FROM intel_source_usage u
            JOIN intel_sources src ON src.id = u.source_id
            WHERE u.usage_type != 'signal_extracted'
            ORDER BY u.used_at DESC
            LIMIT 100
            """
        )
    except Exception:
        usage_rows = []

    usage_html = ""
    for u in usage_rows:
        used_str = ""
        if u.get("used_at"):
            try:
                ua = u["used_at"]
                used_str = ua.strftime("%-d %b %Y %H:%M") if hasattr(ua, "strftime") else str(ua)[:16]
            except Exception:
                pass
        src_label = u.get("short_name") or u.get("source_title", "")[:30]
        usage_html += (
            f'<tr>'
            f'<td style="font-size:.82rem;">{used_str}</td>'
            f'<td><code style="font-size:.78rem;color:var(--gold);">{src_label}</code></td>'
            f'<td style="font-size:.82rem;">{u.get("used_in","")[:50]}</td>'
            f'<td>{_doc_badge(u.get("usage_type",""))}</td>'
            f'<td style="text-align:center;">{u.get("significance_score","—")}</td>'
            f'</tr>'
        )

    s4 = (
        f'<div class="card" style="margin-bottom:2rem;">'
        f'<div class="ch"><span style="font-weight:700;">Usage Log</span>'
        f'<span style="font-size:.8rem;color:var(--muted);margin-left:.75rem;">How intel sources have influenced Groundwork outputs</span></div>'
        f'<div style="overflow-x:auto;">'
        f'<table class="dt" style="width:100%;font-size:.83rem;">'
        f'<thead><tr><th>Date</th><th>Source</th><th>Used in</th><th>Artefact type</th><th>Significance</th></tr></thead>'
        f'<tbody>'
        + (usage_html if usage_html else '<tr><td colspan="5" style="padding:1.5rem;color:var(--muted);text-align:center;">No usage recorded yet.</td></tr>')
        + f'</tbody></table></div></div>'
    )

    # ── Section 5: Procurement Plans ─────────────────────────────────────────
    try:
        _plan_cat = db.fetchone(
            "SELECT id FROM intel_categories WHERE name = %s",
            ("Agency Procurement Plans",),
        )
        _plan_cat_id = _plan_cat["id"] if _plan_cat else None
        if _plan_cat_id:
            plan_sources = db.fetchall(
                """
                SELECT s.id, s.title, s.short_name, s.publisher, s.url,
                       s.last_checked, s.is_active,
                       (SELECT COUNT(*) FROM intel_signals sig
                        WHERE sig.source_id = s.id) AS signal_count
                FROM intel_sources s
                WHERE s.category_id = %s
                ORDER BY s.last_checked DESC NULLS LAST, s.title ASC
                """,
                (_plan_cat_id,),
            )
        else:
            plan_sources = []
    except Exception as _exc:
        logger.warning("Intel plan sources query failed: %s", _exc)
        plan_sources = []

    from datetime import datetime as _dt_cls
    _now_utc = _dt_cls.utcnow()

    def _plan_age_badge(last_checked):
        if not last_checked:
            return '<span style="color:#ef4444;font-size:.72rem;font-weight:700;">NEVER FETCHED</span>'
        try:
            lc = last_checked if hasattr(last_checked, "date") else _dt_cls.fromisoformat(str(last_checked)[:19])
            age_days = (_now_utc - lc.replace(tzinfo=None)).days
            if age_days > 30:
                return f'<span style="color:#ef4444;font-size:.72rem;font-weight:700;">DUE REFRESH ({age_days}d old)</span>'
            return f'<span style="color:#2a9d8f;font-size:.72rem;">{age_days}d ago</span>'
        except Exception:
            return '<span style="color:var(--muted);font-size:.72rem;">unknown</span>'

    plan_rows_html = ""
    for ps in plan_sources:
        lc_str = "—"
        if ps.get("last_checked"):
            try:
                lc = ps["last_checked"]
                lc_str = lc.strftime("%-d %b %Y") if hasattr(lc, "strftime") else str(lc)[:10]
            except Exception:
                pass
        plan_rows_html += (
            f'<tr>'
            f'<td style="font-size:.85rem;">'
            f'<a href="{ps.get("url","#")}" target="_blank" style="color:var(--text);">'
            f'{ps.get("title","")[:70]}</a></td>'
            f'<td style="font-size:.82rem;color:var(--muted);">{(ps.get("publisher") or "")[:30]}</td>'
            f'<td style="text-align:center;">{ps.get("signal_count",0)}</td>'
            f'<td style="font-size:.82rem;">{lc_str}</td>'
            f'<td>{_plan_age_badge(ps.get("last_checked"))}</td>'
            f'<td style="text-align:center;">'
            f'<a href="/intel/run-plans?agency={ps.get("short_name","")}" '
            f'class="btn bg-out" style="font-size:.72rem;padding:.2rem .6rem;" '
            f'onclick="return confirm(\'Refresh this agency plan?\')">↻ Refresh</a>'
            f'</td></tr>'
        )

    due_count = sum(
        1 for ps in plan_sources
        if ps.get("last_checked") is None or (
            hasattr(ps["last_checked"], "date") and
            (_now_utc - ps["last_checked"].replace(tzinfo=None)).days > 30
        )
    )

    s_plans = (
        f'<div class="card" style="margin-bottom:2rem;">'
        f'<div class="ch" style="display:flex;align-items:center;justify-content:space-between;">'
        f'<div>'
        f'<span style="font-weight:700;">📋 Agency Procurement Plans</span>'
        f'<span style="font-size:.78rem;color:var(--muted);margin-left:.75rem;">'
        f'{len(plan_sources)} plans ingested'
        + (f' — <span style="color:#ef4444;font-weight:700;">{due_count} due refresh</span>' if due_count else "")
        + f'</span></div>'
        f'<div style="display:flex;gap:.5rem;">'
        f'<a href="/intel/run-plans" class="btn bg-gold sm" style="font-size:.78rem;" '
        f'onclick="return confirm(\'Refresh all agency procurement plans now? This calls Claude for each plan.\')">Refresh all procurement plans</a>'
        f'</div></div>'
        + (
            f'<div style="overflow-x:auto;">'
            f'<table class="dt" style="width:100%;font-size:.83rem;">'
            f'<thead><tr><th>Agency plan</th><th>Publisher</th><th>Signals</th>'
            f'<th>Last fetched</th><th>Status</th><th></th></tr></thead>'
            f'<tbody>{plan_rows_html}</tbody></table></div>'
            if plan_sources else
            f'<div style="padding:2rem;color:var(--muted);text-align:center;">'
            f'No agency procurement plans ingested yet. '
            f'<a href="/intel/run-plans" class="btn bg-gold sm" style="font-size:.78rem;" '
            f'onclick="return confirm(\'Run procurement plan fetch now?\')">Run now</a></div>'
        )
        + f'</div>'
    )

    body = (
        f'<div class="ptitle" style="display:flex;align-items:center;justify-content:space-between;">'
        f'<div>'
        f'<h1 class="ph">Intelligence Library</h1>'
        f'<div class="psub">Strategic document monitoring — {stats["sources_active"]} sources, '
        f'{stats["signals_total"]} signals</div>'
        f'</div>'
        f'<div style="display:flex;gap:.5rem;">'
        f'<a href="/intel/run?job=daily" class="btn bg-out sm" '
        f'style="font-size:.78rem;" '
        f'onclick="return confirm(\'Fetch Beehive daily sources now?\')">Daily fetch</a>'
        f'<a href="/intel/run?job=initial" class="btn bg-gold sm" '
        f'style="font-size:.78rem;" '
        f'onclick="return confirm(\'Run initial Budget 2026 fetch? This calls Claude for each source.\')">Initial Budget fetch</a>'
        f'</div></div>'
        f'{s1}{s_plans}{s2}{s3}{s4}'
    )
    return _page("Intelligence Library", body, "admin")


@app.route("/intel/run")
@login_required
@admin_required
def intel_run_job():
    """Trigger an intel library job from the admin UI."""
    from intel_library.scheduler_jobs import (
        fetch_beehive_daily, refresh_all_sources, initial_budget_fetch,
    )
    job = request.args.get("job", "")
    try:
        if job == "daily":
            result = fetch_beehive_daily()
            msg = f"Daily fetch complete — {result['succeeded']} sources processed."
        elif job == "weekly":
            result = refresh_all_sources()
            msg = f"Weekly refresh complete — {result['succeeded']} sources processed."
        elif job == "initial":
            result = initial_budget_fetch()
            msg = f"Initial Budget 2026 fetch complete — {result['succeeded']} sources processed."
        else:
            msg = f"Unknown job: {job}"
    except Exception as exc:
        msg = f"Job failed: {exc}"
        logger.error("intel_run_job error: %s", exc)
    _flash(msg)
    return redirect(url_for("intel_dash"))


@app.route("/intel/run-plans")
@login_required
@admin_required
def intel_run_plans():
    """Trigger procurement plan scraper from admin UI."""
    agency_short = request.args.get("agency", "").strip() or None
    try:
        from procurement_plan_scraper import run_all_agency_plans, ingest_agency_plan, PRIORITY_AGENCIES
        if agency_short:
            match = next(
                (a for a in PRIORITY_AGENCIES if a["short"].lower() == agency_short.lower()),
                None,
            )
            if match:
                result = ingest_agency_plan(match, force_refresh=True)
                msg = (
                    f"Plan refresh for {match['name']}: "
                    f"{result['plans_found']} plan(s) found, "
                    f"{result['signals_extracted']} signal(s) extracted. "
                    f"Status: {result['status']}"
                )
            else:
                msg = f"Agency '{agency_short}' not found in priority list."
        else:
            results = run_all_agency_plans(force_refresh=True)
            success = sum(1 for r in results if r["status"] == "success")
            total_sig = sum(r["signals_extracted"] for r in results)
            msg = (
                f"Procurement plan refresh complete — "
                f"{success}/{len(results)} agencies, {total_sig} signals extracted."
            )
    except Exception as exc:
        msg = f"Procurement plan refresh failed: {exc}"
        logger.error("intel_run_plans error: %s", exc)
    _flash(msg)
    return redirect(url_for("intel_dash"))


@app.route("/intel/add-source", methods=["POST"])
@login_required
@admin_required
def intel_add_source():
    """Add a new intel source from the admin form."""
    title       = request.form.get("title", "").strip()
    short_name  = request.form.get("short_name", "").strip() or None
    publisher   = request.form.get("publisher", "").strip() or None
    url_val     = request.form.get("url", "").strip() or None
    pdf_url     = request.form.get("pdf_url", "").strip() or None
    doc_type    = request.form.get("document_type", "report")
    update_freq = request.form.get("update_frequency", "").strip() or None
    notes       = request.form.get("notes", "").strip() or None
    try:
        nz_score = int(request.form.get("nz_relevance_score", 7))
    except ValueError:
        nz_score = 7

    if not title:
        _flash("Title is required.", "error")
        return redirect(url_for("intel_dash"))

    try:
        existing = db.fetchone("SELECT id FROM intel_sources WHERE title = %s", (title,))
        if existing:
            _flash(f"Source already exists: {title[:60]}", "error")
        else:
            db.execute(
                """
                INSERT INTO intel_sources
                    (title, short_name, publisher, url, pdf_url, document_type,
                     update_frequency, nz_relevance_score, notes, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                """,
                (title, short_name, publisher, url_val, pdf_url, doc_type,
                 update_freq, nz_score, notes),
            )
            _flash(f"Source added: {title[:60]}")
    except Exception as exc:
        logger.error("intel_add_source error: %s", exc)
        _flash(f"Error adding source: {exc}", "error")

    return redirect(url_for("intel_dash"))


# ── Error handlers ────────────────────────────────────────────────────────────

@app.errorhandler(403)
def e403(e):
    return _page("Access Denied",
                 '<div style="padding:4rem;text-align:center;"><h2>403 — Access Denied</h2></div>',
                 public=True, sidebar=False), 403

@app.errorhandler(404)
def e404(e):
    return _page("Not Found",
                 '<div style="padding:4rem;text-align:center;"><h2>404 — Not Found</h2></div>',
                 public=True, sidebar=False), 404


# ── User management CLI ───────────────────────────────────────────────────────

def _add_user(username, password, display_name="", email="",
              is_admin=False, sectors=None, plan="pursue", billing_status="active",
              temp_password=False):
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    cfg = _load_cfg()
    cfg.setdefault("clients", {})[username] = {
        "password_hash":     hashed,
        "display_name":      display_name or username,
        "email":             email,
        "is_admin":          is_admin,
        "preferred_sectors": sectors or [],
        "artefact_slug":     _slug(display_name or username),
        "plan":              plan,
        "billing_status":    billing_status,
        "temp_password":     temp_password,
        "email_watchlist":   True,
        "email_briefs":      True,
    }
    _save_cfg(cfg)
    print(f"User '{username}' {'[admin] ' if is_admin else ''}created (plan={plan}).")


def _bootstrap():
    if CONFIG_FILE.exists(): return
    cfg = {"clients": {}, "settings": {"admin_email": os.getenv("ADMIN_EMAIL", ""),
                                        "site_name": "Groundwork by BidEdge"}}
    _save_cfg(cfg)
    print(f"Created {CONFIG_FILE}")


@app.route("/admin/demo-review")
@login_required
@admin_required
def admin_demo_review():
    """Admin: quality report for all generated demo artefacts."""
    import json as _json
    import re as _re
    from pathlib import Path as _Path
    from bs4 import BeautifulSoup as _BS

    ROOT = _Path(__file__).parent
    MANIFEST_PATH = ROOT / "output" / "artefacts" / "demo" / "manifest.json"

    FIRM_SECTOR = {
        "Sentinel Digital": "ICT", "Cityworks NZ": "FM",
        "Meridian Civil": "construction",    "Apex Engineering": "defence",
        "Korepath Systems": "ICT",           "Southern Civil Group": "infrastructure",
        "MedTech Solutions NZ": "health",
    }
    PLACEHOLDERS = ["lorem ipsum","placeholder","tbd","n/a","not available",
                    "enrichment not available","none identified","no data",
                    "coming soon","todo","[insert"]

    def _t(el):
        return _re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""

    def _first(soup, *sels):
        for s in sels:
            el = soup.select_one(s)
            if el:
                t = _t(el)
                if t: return t
        return ""

    def _is_ph(text):
        low = (text or "").lower().strip()
        if not low: return True
        return any(p in low for p in PLACEHOLDERS)

    def _badge(ok, msg):
        c = "#4ade80" if ok else "#f87171"
        return f'<span style="background:{c}22;color:{c};border:1px solid {c}55;border-radius:4px;font-size:.72rem;padding:.15rem .55rem;font-weight:700;">{msg}</span>'

    def _row(label, value, warn=False):
        colour = "var(--text)" if not warn else "#f87171"
        return (f'<tr>'
                f'<td style="font-size:.75rem;color:var(--muted);padding:.3rem .6rem;white-space:nowrap;vertical-align:top;">{label}</td>'
                f'<td style="font-size:.78rem;color:{colour};padding:.3rem .6rem;line-height:1.55;">{value or "<em style=color:var(--muted)>—</em>"}</td>'
                f'</tr>')

    manifest = _load_demo_manifest()
    if not manifest:
        body = ('<div class="ptitle">Demo Review</div>'
                '<div class="al al-er">Manifest not found — run Generate Demo Content first.</div>')
        return _page("Admin — Demo Review", body, "admin")

    sectors  = manifest.get("sectors", {})
    generated = manifest.get("generated", "?")

    # Global cross-contamination scan
    demo_dir = ROOT / "output" / "artefacts" / "demo"
    all_html = list(demo_dir.rglob("*.html"))
    global_warnings = []
    for firm, home in FIRM_SECTOR.items():
        for hf in all_html:
            try:
                file_sector = hf.relative_to(demo_dir).parts[0]
            except Exception:
                file_sector = "unknown"
            if file_sector == home:
                continue
            if firm.lower() in hf.read_text(encoding="utf-8", errors="ignore").lower():
                global_warnings.append(f"<strong>{firm}</strong> ({home}) found in <code>{file_sector}/{hf.name}</code>")

    cross_html = ""
    if global_warnings:
        cross_html = ('<div class="al al-er" style="margin-bottom:1.5rem;"><strong>⚠ Cross-contamination detected:</strong><ul style="margin:.5rem 0 0 1.2rem;">'
                      + "".join(f"<li>{w}</li>" for w in global_warnings) + "</ul></div>")
    else:
        cross_html = '<div class="al al-ok" style="margin-bottom:1.5rem;">✓ No cross-sector firm name contamination detected across all HTML files.</div>'

    sections_html = ""
    for sector_key, sdata in sectors.items():
        firm  = sdata.get("firm", {})
        items = sdata.get("items", [])

        cards = ""
        for item in items:
            itype    = item.get("type", "?")
            html_rel = item.get("html_path", "")
            html_path = (ROOT / html_rel) if html_rel else None

            if html_path and not html_path.exists():
                # Fallback 1: Supabase Storage
                _loaded = False
                try:
                    import storage as _storage
                    _parts = _Path(html_rel).parts
                    _didx = next((i for i, p in enumerate(_parts) if p == "demo"), None)
                    if _didx is not None:
                        _sp = "/".join(_parts[_didx:])
                        _data = _storage.download_file(_sp)
                        if _data:
                            html_path.parent.mkdir(parents=True, exist_ok=True)
                            html_path.write_bytes(_data)
                            _loaded = True
                except Exception:
                    pass
                # Fallback 2: database pipeline_outputs
                if not _loaded:
                    try:
                        _parts2 = _Path(html_rel).parts
                        _didx2 = next((i for i, p in enumerate(_parts2) if p == "demo"), None)
                        if _didx2 is not None:
                            _db_fn = "/".join(_parts2[_didx2 + 1:])
                            _row = db.fetchone(
                                "SELECT content FROM pipeline_outputs "
                                "WHERE output_type = 'demo_html' AND filename = %s "
                                "ORDER BY run_date DESC, created_at DESC LIMIT 1",
                                (_db_fn,),
                            )
                            if _row and _row.get("content"):
                                html_path.parent.mkdir(parents=True, exist_ok=True)
                                html_path.write_text(_row["content"], encoding="utf-8")
                    except Exception:
                        pass

            if not html_path or not html_path.exists():
                cards += f'<div class="al al-er">⚠ File missing: {html_rel or "(no path)"}</div>'
                continue

            size_kb  = html_path.stat().st_size / 1024
            html_txt = html_path.read_text(encoding="utf-8")
            soup     = _BS(html_txt, "lxml")
            rows     = ""

            size_badge = _badge(size_kb >= 10, f"{size_kb:.0f} KB")
            if size_kb < 5:
                rows += _row("⚠ File size", f"{size_kb:.1f} KB — suspiciously small", warn=True)

            # Firm name contamination in this specific file
            file_warnings = []
            for other_firm, other_sector in FIRM_SECTOR.items():
                if other_sector == sector_key:
                    continue
                if other_firm.lower() in html_txt.lower():
                    file_warnings.append(f'⚠ "{other_firm}" ({other_sector}) in this file')
            for w in file_warnings:
                rows += _row("⚠ Contamination", w, warn=True)

            type_icon = {"pursuit_package":"🎯","competitor_profile":"📊","watch_brief":"📬"}.get(itype,"📄")
            card_title = f"{type_icon} {itype.replace('_',' ').title()}"

            if itype == "pursuit_package":
                title   = _first(soup, ".cover-title")
                agency  = _first(soup, ".cover-agency")
                wp      = _first(soup, ".prob-pct")
                verdict = _first(soup, ".verdict-badge")
                rat     = _first(soup, ".verdict-text")
                exec_el = soup.select_one("#exec")
                exec_t  = _t(exec_el)[:600] if exec_el else ""
                cog_el  = soup.select_one("#cog")
                hyps    = [_t(h)[:180] for h in (cog_el.select("td,li,p") if cog_el else []) if len(_t(h)) > 15][:5]
                bid_el  = soup.select_one("#assessment") or soup.select_one("#competitive")
                bids    = [_t(b)[:140] for b in (bid_el.select("tr") if bid_el else []) if len(_t(b)) > 5][:6]

                rows += _row("Tender", title, warn=_is_ph(title))
                rows += _row("Agency", agency, warn=_is_ph(agency))
                rows += _row("Win position", wp, warn=_is_ph(wp))
                rows += _row("Go/No-go", verdict, warn=_is_ph(verdict))
                rows += _row("Rationale", rat[:300], warn=_is_ph(rat))
                rows += _row("Exec summary", exec_t[:500] or "—", warn=_is_ph(exec_t))
                rows += _row("ACH hypotheses", ("<ul style='margin:0;padding-left:1.2rem;'>"
                             + "".join(f"<li>{h}</li>" for h in hyps) + "</ul>") if hyps else "—",
                             warn=not hyps)
                rows += _row("Bidders", ("<ul style='margin:0;padding-left:1.2rem;'>"
                             + "".join(f"<li>{b}</li>" for b in bids) + "</ul>") if bids else "—",
                             warn=not bids)

            elif itype == "competitor_profile":
                h1 = _first(soup, "h1",".cover-title",".ptitle",".comp-title")
                paras = [_t(p) for p in soup.select("p,li") if len(_t(p)) > 60]
                body_t = " | ".join(paras[:4])
                tables = soup.select("table")
                tbl_html = ""
                for tbl in tables[:2]:
                    tbl_html += "<table style='border-collapse:collapse;font-size:.74rem;margin-bottom:.5rem;'>"
                    for tr in tbl.select("tr")[:5]:
                        tbl_html += "<tr>" + "".join(
                            f"<td style='border:1px solid var(--border);padding:.2rem .4rem;'>{_t(td)[:80]}</td>"
                            for td in tr.select("td,th")) + "</tr>"
                    tbl_html += "</table>"

                rows += _row("Competitor", h1, warn=_is_ph(h1))
                rows += _row("Body excerpt", body_t[:500] or "—", warn=_is_ph(body_t))
                rows += _row("Tables", tbl_html or "—")

            elif itype == "watch_brief":
                t_h1 = _first(soup, "h1",".brief-title",".ptitle")
                opps = [_t(o)[:120] for o in soup.select("h3,h4,.opp-title,.notice-title") if len(_t(o)) > 10][:6]
                paras = [_t(p) for p in soup.select("p") if len(_t(p)) > 60]
                body_t = " | ".join(paras[:3])

                rows += _row("Title", t_h1, warn=_is_ph(t_h1))
                rows += _row("Opportunities", ("<ul style='margin:0;padding-left:1.2rem;'>"
                             + "".join(f"<li>{o}</li>" for o in opps) + "</ul>") if opps else "—",
                             warn=not opps)
                rows += _row("Body excerpt", body_t[:500] or "—", warn=_is_ph(body_t))

            view_url = url_for("demo_file", filepath=html_rel.replace("output/artefacts/demo/",""))
            cards += (f'<div class="card" style="margin-bottom:1rem;">'
                      f'<div class="ch"><span class="ct">{card_title}</span>'
                      f'{size_badge}'
                      f'<a href="{view_url}" target="_blank" class="btn bg-out sm" style="margin-left:auto;">View →</a>'
                      f'</div>'
                      f'<div style="overflow-x:auto;">'
                      f'<table style="width:100%;border-collapse:collapse;">{rows}</table>'
                      f'</div></div>')

        sections_html += (
            f'<div class="card" style="margin-bottom:2rem;">'
            f'<div class="ch"><span class="ct">{sector_key.upper()} — {firm.get("name","?")} '
            f'<span style="font-weight:400;font-size:.8rem;color:var(--muted);">'
            f'{firm.get("staff","?")} staff · {firm.get("location","?")} · {firm.get("years_operating","?")} yrs</span></span>'
            f'<span style="font-size:.75rem;color:var(--muted);">{len(items)} artefact(s)</span>'
            f'</div>'
            f'<div class="cb">{cards or "<p style=color:var(--muted);>No artefacts generated.</p>"}</div>'
            f'</div>'
        )

    body = (
        f'<div class="ptitle">Demo Artefact Review</div>'
        f'<div class="psub">Generated: {generated} &nbsp;·&nbsp; {len(sectors)} sectors &nbsp;·&nbsp; '
        f'{len(all_html)} HTML files</div>'
        f'{cross_html}'
        f'{sections_html}'
    )
    return _page("Admin — Demo Review", body, "admin")


@app.route("/admin/storage-check")
@login_required
@admin_required
def admin_storage_check():
    """Admin: list Supabase Storage contents and flag local-only files."""
    import storage as _storage
    storage_files = _storage.list_files()
    local_only = []
    for subdir in ["pursuits", "competitors", "briefs", "watchlist", "demo"]:
        d = Path(config.ARTEFACTS_DIR)
        for f in d.rglob("*.html") if d.exists() else []:
            rel = str(f.relative_to(d)).replace("\\", "/")
            if rel not in storage_files:
                local_only.append(rel)
        d2 = Path(config.OUTPUT_DIR) / subdir
        for f in (d2.rglob("*.html") if d2.exists() else []):
            rel = f"{subdir}/{f.name}"
            if rel not in storage_files:
                local_only.append(rel)
    return render_template_string(
        """<!DOCTYPE html><html><head><title>Storage Check</title>
        <style>body{font-family:system-ui;padding:2rem;max-width:800px;margin:auto}
        h1{font-size:1.4rem}pre{background:#f4f4f4;padding:1rem;border-radius:6px;overflow:auto}
        .ok{color:#16a34a}.warn{color:#d97706}</style></head><body>
        <h1>Supabase Storage Check</h1>
        <p>Bucket: <code>{{ bucket }}</code> — <strong>{{ storage_files|length }}</strong> files</p>
        {% if storage_files %}
        <pre>{% for f in storage_files %}<span class="ok">✓ {{ f }}</span>
{% endfor %}</pre>
        {% else %}<p class="warn">Storage is empty or not configured.</p>{% endif %}
        {% if local_only %}
        <p class="warn">Local files not yet in Storage ({{ local_only|length }}):</p>
        <pre>{% for f in local_only %}{{ f }}
{% endfor %}</pre>
        {% endif %}
        <p><a href="/admin">← Admin</a></p></body></html>""",
        bucket=config.STORAGE_BUCKET,
        storage_files=sorted(storage_files),
        local_only=local_only,
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BidEdge Groundwork Portal")
    parser.add_argument("--create-user", metavar="USERNAME")
    parser.add_argument("--password",    metavar="PASSWORD")
    parser.add_argument("--name",        metavar="DISPLAY_NAME", default="")
    parser.add_argument("--email",       metavar="EMAIL",        default="")
    parser.add_argument("--admin",       action="store_true")
    parser.add_argument("--sectors",     metavar="SECTORS",      default="")
    parser.add_argument("--list-users",  action="store_true")
    args = parser.parse_args()

    _bootstrap()

    if args.list_users:
        cfg = _load_cfg()
        for u, d in cfg.get("clients", {}).items():
            flag = " [ADMIN]" if d.get("is_admin") else ""
            print(f"  {u}{flag} — {d.get('display_name','')} <{d.get('email','')}>")
        sys.exit(0)

    if args.create_user:
        if not args.password: print("--password required"); sys.exit(1)
        sectors = [s.strip() for s in args.sectors.split(",") if s.strip()]
        _add_user(args.create_user, args.password, args.name, args.email, args.admin, sectors)
        sys.exit(0)

    # Railway injects PORT; fall back to PORTAL_PORT for local dev
    port = int(os.environ.get("PORT", config.PORTAL_PORT))
    logger.info("Groundwork portal starting at http://0.0.0.0:%s", port)
    app.run(host="0.0.0.0", port=port, debug=False)
