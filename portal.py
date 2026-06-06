"""
BidEdge Groundwork — Client Portal

Routes:
  Public:  /  /login  /logout  /share/<token>  /request-access
  Client:  /groundwork  /groundwork/watchlist  /groundwork/pursuits
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
import argparse, json, logging, os, secrets, smtplib, sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
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

CONFIG_FILE = Path("portal_config.json")
TOKENS_FILE = Path("data/share_tokens.json")
ARTEFACTS   = Path(config.ARTEFACTS_DIR)
OUTPUT_DIR  = Path(config.OUTPUT_DIR)
TOKEN_TTL_H = 24


# ── Helpers ───────────────────────────────────────────────────────────────────

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
        self.is_admin_user    = data.get("is_admin", False)
        # Support both old key "sectors" and new key "preferred_sectors"
        self.preferred_sectors = (
            data.get("preferred_sectors")
            or data.get("sectors")
            or []
        )
        self.slug          = data.get("artefact_slug") or _slug(data.get("display_name", username))

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


# ── SMTP ──────────────────────────────────────────────────────────────────────

def _send_email(subject: str, html: str, to: list[str]) -> bool:
    if not (config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASSWORD and config.SMTP_FROM):
        logger.warning("SMTP not configured — email skipped")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject; msg["From"] = config.SMTP_FROM; msg["To"] = ", ".join(to)
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as s:
            s.ehlo(); s.starttls(); s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.sendmail(config.SMTP_FROM, to, msg.as_string())
        return True
    except Exception as exc:
        logger.error("Email failed: %s", exc); return False

def _admin_emails() -> list[str]:
    a = _load_cfg().get("settings", {}).get("admin_email") or os.getenv("ADMIN_EMAIL", "")
    return [a] if a else []


# ── File helpers ──────────────────────────────────────────────────────────────

def _list_artefacts(slug: str, pattern: str = "*.html") -> list[dict]:
    base = ARTEFACTS / slug
    if not base.exists(): return []
    files = []
    for f in sorted(base.rglob(pattern), reverse=True)[:60]:
        rel = f.relative_to(ARTEFACTS)
        files.append({"name": f.stem.replace("_", " ").title(),
                      "rel_path": str(rel), "date": f.parent.name,
                      "size_kb": f.stat().st_size // 1024,
                      "has_pdf": f.with_suffix(".pdf").exists()})
    return files

def _latest_watchlist() -> Optional[Path]:
    c = sorted(OUTPUT_DIR.glob("watchlist_*.html"), reverse=True)
    return c[0] if c else None

def _watchlist_summary(preferred_sectors: Optional[list[str]] = None) -> dict:
    """
    Return top notices re-ranked by client sector preference.
    When preferred_sectors is None/empty all sectors score equally (neutral).
    """
    try:
        from scoring import compute_composite_for_client
        # Pull a wider pool so re-ranking has room to surface preferred sectors
        pool = db.fetchall("""
            SELECT r.notice_id, r.title, r.agency, r.close_date,
                   p.sector_tag, p.days_until_close, s.composite_score,
                   s.score_value, s.score_complexity, s.score_urgency
              FROM scored_notices s
              JOIN raw_notices r  ON r.notice_id = s.notice_id
              JOIN parsed_notices p ON p.notice_id = s.notice_id
             WHERE s.composite_score >= %s
             ORDER BY s.composite_score DESC LIMIT 50
        """, (config.PRIORITY_THRESHOLD,))

        for row in pool:
            row["client_score"] = compute_composite_for_client(
                float(row.get("score_value") or 0),
                float(row.get("score_complexity") or 0),
                float(row.get("score_urgency") or 0),
                row.get("sector_tag") or "other",
                preferred_sectors,
            )
        pool.sort(key=lambda r: r["client_score"], reverse=True)
        notices = pool[:5]

        flags = db.fetchall("""
            SELECT flag_type, description, severity FROM pattern_flags
             WHERE (expires_at IS NULL OR expires_at >= CURRENT_DATE)
             ORDER BY CASE severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END
             LIMIT 4
        """)
        total = db.fetchone(
            "SELECT COUNT(*) as n FROM scored_notices WHERE composite_score >= %s",
            (config.PRIORITY_THRESHOLD,))
        return {"top_notices": [dict(n) for n in notices],
                "flags": [dict(f) for f in flags],
                "total": total["n"] if total else 0,
                "run_date": date.today().isoformat(),
                "preferred_sectors": preferred_sectors or []}
    except Exception as exc:
        logger.error("watchlist_summary: %s", exc)
        return {"top_notices": [], "flags": [], "total": 0, "run_date": "",
                "preferred_sectors": []}


# ── CSS & layout ──────────────────────────────────────────────────────────────

CSS = """
<style>
:root{--bg:#0f1923;--surf:#162032;--surf2:#1e2f45;--border:#253d5c;
      --text:#f0f4f8;--muted:#8fa3bc;--navy:#1a2d4a;--gold:#c9a84c;
      --gold-l:rgba(201,168,76,.12);--red:#e05555;--green:#4caf7d;
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
.nav-user strong{color:var(--text);}
/* Shell */
.shell{display:flex;min-height:calc(100vh - 52px);}
.side{width:210px;flex-shrink:0;background:var(--surf);
      border-right:1px solid var(--border);padding:1.5rem 1rem;}
.side-sec{font-size:.6rem;font-weight:700;letter-spacing:.09em;text-transform:uppercase;
          color:var(--muted);margin:1.25rem 0 .5rem;padding-left:.5rem;}
.side a{display:flex;align-items:center;gap:.6rem;padding:.42rem .65rem;
        border-radius:5px;font-size:.83rem;color:var(--muted);margin-bottom:.15rem;transition:.12s;}
.side a:hover{background:var(--surf2);color:#fff;}
.side a.on{background:var(--gold-l);color:var(--gold);border:1px solid rgba(201,168,76,.25);}
/* Content */
.main{flex:1;padding:2.5rem 2.5rem;overflow-x:hidden;}
.ptitle{font-size:1.3rem;font-weight:800;color:var(--text);margin-bottom:.35rem;}
.psub{font-size:.85rem;color:var(--muted);margin-bottom:2rem;}
/* Cards */
.card{background:var(--surf);border:1px solid var(--border);border-radius:8px;
      overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.2);margin-bottom:1.25rem;}
.ch{background:var(--surf2);border-bottom:1px solid var(--border);
    padding:.85rem 1.25rem;display:flex;align-items:center;justify-content:space-between;}
.ct{font-size:.88rem;font-weight:700;}
.cb{padding:1.25rem;}
/* Notice rows */
.nr{display:flex;align-items:flex-start;gap:1rem;padding:1rem 1.25rem;
    border-bottom:1px solid var(--border);transition:.1s;}
.nr:last-child{border-bottom:none;}
.nr:hover{background:var(--surf2);}
.nrank{flex-shrink:0;width:1.75rem;height:1.75rem;border-radius:50%;
       background:var(--gold-l);color:var(--gold);font-size:.72rem;font-weight:700;
       display:flex;align-items:center;justify-content:center;
       border:1px solid rgba(201,168,76,.3);}
.nmain{flex:1;min-width:0;}
.ntitle{font-size:.88rem;font-weight:600;margin-bottom:.22rem;}
.nagency{font-size:.75rem;color:var(--muted);}
.nmeta{display:flex;gap:.4rem;margin-top:.35rem;flex-wrap:wrap;}
.nscore{flex-shrink:0;font-size:1.1rem;font-weight:800;color:var(--gold);text-align:center;}
.nscore small{display:block;font-size:.6rem;color:var(--muted);font-weight:400;}
/* Badges */
.badge{display:inline-flex;align-items:center;padding:.18rem .55rem;border-radius:999px;
       font-size:.65rem;font-weight:600;border:1px solid;white-space:nowrap;}
.bg{background:rgba(201,168,76,.12);color:var(--gold);border-color:rgba(201,168,76,.35);}
.bn{background:rgba(26,45,74,.5);color:#8fa3bc;border-color:var(--border);}
.br{background:rgba(224,85,85,.12);color:var(--red);border-color:rgba(224,85,85,.3);}
.bk{background:rgba(143,163,188,.1);color:var(--muted);border-color:var(--border);}
/* Flag rows */
.fr{display:flex;gap:.75rem;align-items:flex-start;padding:.65rem 1rem;
    border-radius:6px;background:var(--surf2);margin-bottom:.5rem;
    border:1px solid var(--border);font-size:.82rem;}
.fs{flex-shrink:0;font-size:.62rem;font-weight:700;padding:.15rem .4rem;
    border-radius:3px;text-transform:uppercase;}
.fsh{background:rgba(224,85,85,.2);color:var(--red);}
.fsm{background:rgba(201,168,76,.2);color:var(--gold);}
.fsl{background:rgba(143,163,188,.1);color:var(--muted);}
/* Stats */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
       gap:1rem;margin-bottom:2rem;}
.stat{background:var(--surf);border:1px solid var(--border);border-radius:8px;padding:1.1rem 1.25rem;}
.sval{font-size:1.6rem;font-weight:800;color:var(--gold);line-height:1;}
.slbl{font-size:.7rem;font-weight:600;letter-spacing:.07em;text-transform:uppercase;
      color:var(--muted);margin-top:.3rem;}
/* File grid */
.fgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:1rem;}
.fc{background:var(--surf);border:1px solid var(--border);border-radius:8px;
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
.bg-gold{background:var(--gold);color:#0f1923;border-color:var(--gold);}
.bg-gold:hover{background:#e0c070;border-color:#e0c070;color:#0f1923;}
.bg-out{background:transparent;color:var(--text);border-color:var(--border);}
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
.al-in{background:var(--gold-l);border-color:rgba(201,168,76,.3);color:var(--gold);}
/* Tables */
.dt{width:100%;border-collapse:collapse;font-size:.84rem;}
.dt thead tr{background:var(--surf2);}
.dt th{padding:.6rem .85rem;text-align:left;font-size:.66rem;font-weight:600;
       letter-spacing:.07em;text-transform:uppercase;color:var(--muted);
       border-bottom:1px solid var(--border);}
.dt td{padding:.6rem .85rem;border-bottom:1px solid var(--border);}
.dt tbody tr:last-child td{border-bottom:none;}
.dt tbody tr:hover td{background:var(--surf2);}
/* Share modal */
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.65);
          display:flex;align-items:center;justify-content:center;z-index:999;}
.modal{background:var(--surf);border:1px solid var(--border);border-radius:10px;
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
/* Responsive */
@media(max-width:768px){
  .shell{flex-direction:column;}
  .side{width:100%;border-right:none;border-bottom:1px solid var(--border);
        padding:.75rem 1rem;}
  .side a{display:inline-flex;margin-right:.2rem;}
  .side-sec{display:none;}
  .main{padding:1.5rem 1rem;}
  .tiers{grid-template-columns:1fr;}
  .hero h1{font-size:2rem;}
  .stats{grid-template-columns:1fr 1fr;}
  .fgrid{grid-template-columns:1fr;}
  .nav{flex-wrap:wrap;gap:.5rem;padding:.75rem 1rem;}
}
</style>
"""


# ── Layout helpers ────────────────────────────────────────────────────────────

def _topnav(active: str = "") -> str:
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
    return (f'<nav class="nav">'
            f'<div class="nav-brand">'
            f'<div class="nav-brand-name">Groundwork <span class="by">by BidEdge</span></div>'
            f'<div class="nav-brand-sub">Procurement Intelligence</div>'
            f'</div>{user_html}</nav>')


def _sidebar(active: str = "") -> str:
    def lnk(href, icon, label, key):
        cls = "on" if active == key else ""
        return f'<a href="{href}" class="{cls}">{icon}&nbsp; {label}</a>'
    return (f'<nav class="side">'
            f'<div class="side-sec">Intelligence</div>'
            f'{lnk(url_for("gw_home"),        "⬛", "Dashboard",    "home")}'
            f'{lnk(url_for("gw_watchlist"),   "📋", "Watchlist",    "watchlist")}'
            f'{lnk(url_for("gw_pursuits"),    "🎯", "Pursuits",     "pursuits")}'
            f'{lnk(url_for("gw_competitors"), "📊", "Competitors",  "competitors")}'
            f'{lnk(url_for("gw_briefs"),      "📬", "Watch Briefs", "briefs")}'
            f'<div class="side-sec">Actions</div>'
            f'{lnk(url_for("gw_request"),     "✉", "Request",      "request")}'
            f'</nav>')


def _page(title: str, body: str, active: str = "",
          sidebar: bool = True, public: bool = False) -> str:
    nav  = "" if public else _topnav(active)
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
    return (f'<!DOCTYPE html><html lang="en"><head>'
            f'<meta charset="UTF-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{title} — Groundwork by BidEdge</title>'
            f'{CSS}</head><body>'
            f'{nav}{wrap_open}{side}{cont_open}{flashes}{body}'
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

def _sector_badge(sector: str) -> str:
    return f'<span class="badge bn">{(sector or "other").replace("_"," ").upper()}</span>'


# ── Public routes ─────────────────────────────────────────────────────────────

@app.route("/")
def homepage():
    sent = '<div class="al al-ok" style="max-width:540px;margin:0 auto 1.5rem;">Request sent — we will be in touch.</div>' if request.args.get("sent") else ""
    body = (f'<nav class="pub-nav">'
            f'<div class="pub-brand">Groundwork <span>by BidEdge</span></div>'
            f'<a href="{url_for("login")}" class="btn bg-out" style="margin-left:auto;font-size:.82rem;">Client Login</a>'
            f'</nav>'
            f'<div class="hero">{sent}'
            f'<h1>Win more <span>government contracts.</span></h1>'
            f'<p class="hero-sub">Groundwork is BidEdge\'s procurement intelligence platform — built on '
            f'10+ years of NZ government award data, daily GETS monitoring, and AI-powered analysis. '
            f'We help your organisation find the right opportunities, understand the competitive '
            f'landscape, and arrive better prepared than your competitors.</p>'
            f'<a href="#tiers" class="btn bg-gold" style="font-size:.9rem;padding:.65rem 1.75rem;">See what\'s included &darr;</a>'
            f'</div>'
            f'<div class="tiers" id="tiers">'
            f'<div class="tier"><div class="tier-lbl">Foundation</div>'
            f'<div class="tier-name">Groundwork Watch</div>'
            f'<div class="tier-desc">Daily intelligence on active GETS tenders, scored and ranked for strategic relevance.</div>'
            f'<ul><li>Daily scored watchlist (25+ notices)</li><li>AI enrichment — summary, red flags, framing</li>'
            f'<li>Weekly watch brief via email</li><li>MBIE-evidenced likely bidders</li></ul></div>'
            f'<div class="tier ft"><div class="tier-lbl">Most popular</div>'
            f'<div class="tier-name">Groundwork Pursue</div>'
            f'<div class="tier-desc">Everything in Watch, plus a full pursuit intelligence package for each opportunity you target.</div>'
            f'<ul><li>Pursuit intelligence packages</li><li>Win probability from 27,948 MBIE awards</li>'
            f'<li>Incumbent detection &amp; competitor analysis</li>'
            f'<li>Agency procurement profiling</li><li>Recommended actions per notice</li></ul></div>'
            f'<div class="tier"><div class="tier-lbl">Full platform</div>'
            f'<div class="tier-name">Groundwork Intel</div>'
            f'<div class="tier-desc">The complete platform — pursuit intelligence, competitor profiling, renewal radar, and analyst support.</div>'
            f'<ul><li>Competitor intelligence profiles</li><li>Contract renewal radar (90-day)</li>'
            f'<li>Longitudinal pattern detection</li><li>Dedicated BidEdge analyst support</li></ul></div>'
            f'</div>'
            f'<div style="text-align:center;padding:3rem 2rem;">'
            f'<h2 style="font-size:1.5rem;margin-bottom:.75rem;">Ready to get started?</h2>'
            f'<p style="color:var(--muted);margin-bottom:2rem;">Request access — a BidEdge adviser will be in touch within one business day.</p>'
            f'<form action="{url_for("request_access")}" method="POST" '
            f'style="display:inline-flex;gap:.75rem;flex-wrap:wrap;justify-content:center;max-width:500px;">'
            f'<input name="name" class="fc2" placeholder="Your name" style="width:190px;" required>'
            f'<input name="email" type="email" class="fc2" placeholder="Work email" style="width:210px;" required>'
            f'<input name="org" class="fc2" placeholder="Organisation" style="width:190px;">'
            f'<button type="submit" class="btn bg-gold" style="padding:.55rem 1.5rem;">Request Access &rarr;</button>'
            f'</form></div>'
            f'<div class="pub-footer">&copy; BidEdge Ltd &middot; Groundwork Procurement Intelligence &middot; '
            f'<a href="{url_for("login")}">Client Login</a></div>')
    return _page("BidEdge — Groundwork Procurement Intelligence", body, public=True, sidebar=False)


@app.route("/request-access", methods=["POST"])
def request_access():
    name  = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    org   = request.form.get("org", "").strip()
    if name and email:
        subject = f"[BidEdge] Access Request — {name}"
        html = (f"<p><b>Name:</b> {name}<br><b>Email:</b> {email}<br>"
                f"<b>Organisation:</b> {org or '(not given)'}</p>")
        _send_email(subject, html, _admin_emails())
    return redirect(url_for("homepage") + "?sent=1")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("gw_home"))
    error = ""
    if request.method == "POST":
        user = _check_password(request.form.get("username","").strip(),
                               request.form.get("password",""))
        if user:
            login_user(user, remember=request.form.get("remember") == "on")
            return redirect(request.args.get("next") or url_for("gw_home"))
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


# ── Share ─────────────────────────────────────────────────────────────────────

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

@app.route("/groundwork")
@login_required
def gw_home():
    data    = _watchlist_summary(preferred_sectors=current_user.preferred_sectors)
    top     = data.get("top_notices", [])
    flags   = data.get("flags", [])
    total   = data.get("total", 0)
    run_date = data.get("run_date", date.today().isoformat())
    pursuits = len(_list_artefacts(current_user.slug, "*pursuit*.html"))
    comps    = len(_list_artefacts(current_user.slug, "competitor_*.html"))

    notices_html = ""
    for i, n in enumerate(top, 1):
        display_score = float(n.get("client_score") or n.get("composite_score") or 0)
        notices_html += (f'<div class="nr">'
                         f'<div class="nrank">{i}</div>'
                         f'<div class="nmain">'
                         f'<div class="ntitle">{n.get("title","")[:80]}</div>'
                         f'<div class="nagency">{n.get("agency","")}</div>'
                         f'<div class="nmeta">'
                         f'{_sector_badge(n.get("sector_tag",""))}'
                         f'{_dtc_badge(n.get("days_until_close"))}'
                         f'</div></div>'
                         f'<div class="nscore">{display_score:.1f}<small>/10</small></div>'
                         f'</div>')

    flags_html = ""
    for fl in flags:
        sev = (fl.get("severity") or "low").lower()
        css_map = {"high": "fsh", "medium": "fsm", "low": "fsl"}
        flags_html += (f'<div class="fr">'
                       f'<span class="fs {css_map.get(sev,"fsl")}">{sev}</span>'
                       f'<span>{fl.get("description","")[:130]}</span></div>')
    if not flags_html:
        flags_html = '<p style="color:var(--muted);font-size:.82rem;">No active market signals.</p>'

    wl_link = f'<a href="{url_for("gw_watchlist")}" class="btn bg-out sm">Full watchlist &rarr;</a>'
    # Sector preference indicator
    if current_user.preferred_sectors:
        pills = "".join(
            f'<span style="background:rgba(201,168,76,.15);color:var(--gold);'
            f'border:1px solid rgba(201,168,76,.3);border-radius:4px;'
            f'padding:.1rem .4rem;font-size:.7rem;font-weight:600;margin-right:.3rem;">'
            f'{s}</span>'
            for s in current_user.preferred_sectors
        )
        sector_note = f' &middot; Ranked for: {pills}'
    else:
        sector_note = ' &middot; <span style="font-size:.75rem;color:var(--muted);">Sector-neutral ranking</span>'
    body = (f'<div class="ptitle">Dashboard</div>'
            f'<div class="psub">Good morning, {current_user.name} &middot; {run_date}{sector_note}</div>'
            f'<div class="stats">'
            f'<div class="stat"><div class="sval">{total}</div><div class="slbl">Active opportunities</div></div>'
            f'<div class="stat"><div class="sval">{pursuits}</div><div class="slbl">Pursuit packages</div></div>'
            f'<div class="stat"><div class="sval">{comps}</div><div class="slbl">Competitor profiles</div></div>'
            f'<div class="stat"><div class="sval">{len(flags)}</div><div class="slbl">Market signals</div></div>'
            f'</div>'
            f'<div style="display:grid;grid-template-columns:1.6fr 1fr;gap:1.25rem;">'
            f'<div class="card"><div class="ch"><span class="ct">Top Opportunities</span>{wl_link}</div>'
            f'{notices_html or "<div class=cb><p style=color:var(--muted)>No scored notices yet.</p></div>"}'
            f'</div>'
            f'<div class="card"><div class="ch"><span class="ct">Market Signals</span></div>'
            f'<div class="cb">{flags_html}</div></div></div>')
    return _page("Dashboard — Groundwork", body, "home")


@app.route("/groundwork/watchlist")
@login_required
def gw_watchlist():
    wl = _latest_watchlist()
    if not wl:
        body = ('<div class="ptitle">Daily Watchlist</div>'
                '<div class="card cb"><p style="color:var(--muted);">No watchlist yet. Run Layer 1 pipeline.</p></div>')
        return _page("Watchlist", body, "watchlist")
    rel = str(wl.relative_to(OUTPUT_DIR))
    src = url_for("serve_output_file", filepath=rel)
    date_str = wl.stem.replace("watchlist_", "")
    body = (f'<div class="ptitle">Daily Watchlist</div>'
            f'<div class="psub">{date_str} &middot; '
            f'<a href="{src}" target="_blank">Open full screen &rarr;</a> &middot; '
            f'<a href="{src}?dl=1">Download HTML</a></div>'
            f'<iframe src="{src}" style="width:100%;height:calc(100vh - 210px);'
            f'border:1px solid var(--border);border-radius:8px;" loading="lazy"></iframe>')
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


def _artefact_page(title: str, pattern: str, empty_msg: str, active: str) -> str:
    files = _list_artefacts(current_user.slug, pattern)
    if not files:
        body = (f'<div class="ptitle">{title}</div>'
                f'<div class="card cb"><p style="color:var(--muted);">{empty_msg}</p>'
                f'<a href="{url_for("gw_request")}" class="btn bg-gold" style="margin-top:1rem;">Request one &rarr;</a>'
                f'</div>')
        return _page(title, body, active)

    cards = ""
    for f in files:
        view_url = url_for("serve_artefact_file",
                           client_slug=current_user.slug, filepath=f["rel_path"])
        pdf_btn = ""
        if f.get("has_pdf"):
            pdf_path = f["rel_path"].rsplit(".", 1)[0] + ".pdf"
            pdf_url  = url_for("serve_artefact_file",
                               client_slug=current_user.slug, filepath=pdf_path)
            pdf_btn = f'<a href="{pdf_url}" target="_blank" class="btn bg-out sm">PDF</a>'
        cards += (f'<div class="fc">'
                  f'<div class="fct">{f["name"]}</div>'
                  f'<div class="fcd">{f["date"]} &middot; {f["size_kb"]}KB</div>'
                  f'<div class="fca">'
                  f'<a href="{view_url}" target="_blank" class="btn bg-gold sm">View</a>'
                  f'<a href="{view_url}?dl=1" class="btn bg-out sm">Download</a>'
                  f'{pdf_btn}'
                  f'<button class="btn bg-ghost sm" '
                  f'onclick="share(\'{f["rel_path"]}\',\'{f["name"]}\')">Share &#128279;</button>'
                  f'</div></div>')

    body = (f'<div class="ptitle">{title}</div>'
            f'<div class="psub">{len(files)} file{"s" if len(files)!=1 else ""}</div>'
            f'<div class="fgrid">{cards}</div>'
            f'{_SHARE_JS}')
    return _page(title, body, active)


@app.route("/groundwork/pursuits")
@login_required
def gw_pursuits():
    return _artefact_page("Pursuit Packages", "*pursuit_package*.html",
                          "No pursuit packages yet for your account.", "pursuits")


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
    if not full.exists(): abort(404)
    if request.args.get("dl"):
        return send_file(str(full), as_attachment=True)
    return send_file(str(full))


@app.route("/groundwork/request", methods=["GET", "POST"])
@login_required
def gw_request():
    sent = False
    if request.method == "POST":
        rtype   = request.form.get("type", "pursuit")
        details = request.form.get("details", "")
        notice  = request.form.get("notice_id", "")
        prio    = request.form.get("priority", "normal")
        subject = (f"[Groundwork] {rtype.title()} Request — "
                   f"{current_user.name} ({current_user.id})")
        html    = (f"<p><b>Client:</b> {current_user.name}<br>"
                   f"<b>Type:</b> {rtype}<br><b>Notice ID:</b> {notice or '—'}<br>"
                   f"<b>Priority:</b> {prio}</p><p>{details}</p>")
        _send_email(subject, html, _admin_emails())
        sent = True
    ok = '<div class="al al-ok">Request submitted — BidEdge will be in touch.</div>' if sent else ""
    body = (f'<div class="ptitle">Request Intelligence</div>'
            f'<div class="psub">Your request goes directly to the BidEdge team.</div>'
            f'{ok}'
            f'<div class="card" style="max-width:580px;">'
            f'<div class="ch"><span class="ct">New Request</span></div>'
            f'<div class="cb">'
            f'<form method="POST">'
            f'<div class="fg"><label class="fl">Request type</label>'
            f'<select name="type" class="fc2">'
            f'<option value="pursuit">Pursuit Intelligence Package</option>'
            f'<option value="competitor">Competitor Profile</option>'
            f'<option value="brief">Weekly Watch Brief (immediate)</option>'
            f'<option value="other">Other</option>'
            f'</select></div>'
            f'<div class="fg"><label class="fl">GETS Notice ID (for pursuit packages)</label>'
            f'<input name="notice_id" class="fc2" placeholder="e.g. 34060392">'
            f'<div class="fh">Find the notice ID in the GETS URL or watchlist.</div></div>'
            f'<div class="fg"><label class="fl">Details</label>'
            f'<textarea name="details" class="fc2" placeholder="Please describe what you need..."></textarea></div>'
            f'<div class="fg"><label class="fl">Priority</label>'
            f'<select name="priority" class="fc2">'
            f'<option value="normal">Normal — within 24 hours</option>'
            f'<option value="urgent">Urgent — within 4 hours</option>'
            f'</select></div>'
            f'<button type="submit" class="btn bg-gold">Submit request &rarr;</button>'
            f'</form></div></div>')
    return _page("Request — Groundwork", body, "request")


# ── Admin ─────────────────────────────────────────────────────────────────────

@app.route("/admin")
@login_required
@admin_required
def admin_dash():
    cfg     = _load_cfg()
    clients = {u: d for u, d in cfg.get("clients", {}).items() if not d.get("is_admin")}
    wl      = _latest_watchlist()
    rows    = ""
    for username, data in clients.items():
        slug = data.get("artefact_slug") or _slug(data.get("display_name", username))
        p = len(_list_artefacts(slug, "*pursuit*.html"))
        c = len(_list_artefacts(slug, "competitor_*.html"))
        rows += (f'<tr><td><strong>{data.get("display_name",username)}</strong></td>'
                 f'<td style="color:var(--muted);">{username}</td>'
                 f'<td>{data.get("email","—")}</td><td>{p}</td><td>{c}</td>'
                 f'<td><a href="{url_for("admin_client",username=username)}" '
                 f'class="btn bg-out sm">Manage</a></td></tr>')
    body = (f'<div class="ptitle">Admin Dashboard</div>'
            f'<div class="psub">BidEdge platform administration</div>'
            f'<div class="stats">'
            f'<div class="stat"><div class="sval">{len(clients)}</div><div class="slbl">Active clients</div></div>'
            f'<div class="stat"><div class="sval">{"Today" if wl and wl.stem.endswith(date.today().isoformat()) else "—"}</div>'
            f'<div class="slbl">Last watchlist</div></div></div>'
            f'<div class="card">'
            f'<div class="ch"><span class="ct">Client Accounts</span>'
            f'<a href="{url_for("admin_add_client")}" class="btn bg-gold sm">+ Add client</a></div>'
            f'<table class="dt"><thead><tr>'
            f'<th>Name</th><th>Username</th><th>Email</th><th>Pursuits</th><th>Competitors</th><th></th>'
            f'</tr></thead><tbody>'
            f'{rows or "<tr><td colspan=6 style=color:var(--muted);text-align:center;padding:1.5rem>No clients yet</td></tr>"}'
            f'</tbody></table></div>')
    return _page("Admin — Groundwork", body, "admin")


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
            vurl = url_for("serve_artefact_file", client_slug=slug, filepath=f["rel_path"])
            rows += (f'<tr><td>{f["name"]}</td><td style="color:var(--muted);">{f["date"]}</td>'
                     f'<td>{f["size_kb"]}KB</td>'
                     f'<td><a href="{vurl}" target="_blank" class="btn bg-out sm">View</a></td></tr>')
        return (f'<table class="dt"><thead><tr>'
                f'<th>Name</th><th>Date</th><th>Size</th><th></th>'
                f'</tr></thead><tbody>{rows}</tbody></table>')

    sector_pills = ""
    for s in (u.preferred_sectors or []):
        sector_pills += (f'<span style="background:rgba(201,168,76,.15);color:var(--gold);'
                         f'border:1px solid rgba(201,168,76,.3);border-radius:4px;'
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
    username  = request.form.get("username", "")
    atype     = request.form.get("atype", "pursuit")
    notice_id = request.form.get("notice_id", "").strip()
    comp_name = request.form.get("competitor_name", "").strip()
    cfg       = _load_cfg()
    client_data = cfg.get("clients", {}).get(username, {})
    cname     = client_data.get("display_name", username)
    # Respect client's sector preferences when generating artefacts
    client_sectors = (
        client_data.get("preferred_sectors")
        or client_data.get("sectors")
        or []
    )
    try:
        if atype == "pursuit" and notice_id:
            from pursuit_package import generate_pursuit_package
            generate_pursuit_package(notice_id, cname, preferred_sectors=client_sectors or None)
        elif atype == "brief":
            from watch_brief import generate_watch_brief
            generate_watch_brief(cname, sectors=client_sectors or None)
        elif atype == "competitor" and comp_name:
            from competitor_profile import generate_competitor_profile
            generate_competitor_profile(comp_name, client_name=cname)
        _flash(f"Generated {atype} for {cname}.", "success")
    except Exception as exc:
        logger.error("admin_generate: %s", exc)
        _flash(f"Generation failed: {exc}", "error")
    return redirect(url_for("admin_client", username=username))


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
              is_admin=False, sectors=None):
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    cfg = _load_cfg()
    cfg.setdefault("clients", {})[username] = {
        "password_hash":  hashed,
        "display_name":   display_name or username,
        "email":          email,
        "is_admin":       is_admin,
        "preferred_sectors": sectors or [],
        "artefact_slug":  _slug(display_name or username),
    }
    _save_cfg(cfg)
    print(f"User '{username}' {'[admin] ' if is_admin else ''}created.")


def _bootstrap():
    if CONFIG_FILE.exists(): return
    cfg = {"clients": {}, "settings": {"admin_email": os.getenv("ADMIN_EMAIL", ""),
                                        "site_name": "Groundwork by BidEdge"}}
    _save_cfg(cfg)
    print(f"Created {CONFIG_FILE}")


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

    host = config.PORTAL_HOST
    port = config.PORTAL_PORT
    logger.info("Groundwork portal starting at http://%s:%s", host, port)
    app.run(host=host, port=port, debug=False)
