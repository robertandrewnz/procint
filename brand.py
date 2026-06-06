"""
BidEdge / Groundwork brand constants and shared HTML/CSS helpers.

Single source of truth for all styling across HTML-generating modules.
Import this module in output.py, pursuit_package.py, watch_brief.py,
competitor_profile.py, demo_package.py, and portal.py.

Colour palette — light professional consultancy:
  Navy  #1a2d4a  — primary: headers, nav, key labels, card headers
  Gold  #c9a84c  — accent:  scores, highlights, section numbers, buttons hover
  BG    #f5f6f8  — page background
  WHITE #ffffff  — card background
  SURF2 #f0f2f5  — subtle off-white (hover, alternating rows)
  BORDER #e2e6ea — card and table borders
  TEXT   #2c3e50 — body copy
  MUTED  #6c757d — secondary labels, timestamps
"""
from __future__ import annotations
from datetime import date
from typing import Optional

# ── Brand identity ────────────────────────────────────────────────────────────

BRAND        = "BidEdge"
PRODUCT      = "Groundwork"
PRODUCT_FULL = "Groundwork by BidEdge"
TAGLINE      = (
    "We help organisations improve their chances of winning "
    "major procurement opportunities."
)
COPYRIGHT = "© BidEdge Ltd · Groundwork Procurement Intelligence · Confidential"

# ── Palette ───────────────────────────────────────────────────────────────────

NAVY   = "#1a2d4a"
GOLD   = "#c9a84c"
GOLD_L = "#f7eedb"   # light gold tint for badge backgrounds
NAVY_L = "#e8ecf3"   # light navy tint
BG     = "#f5f6f8"
WHITE  = "#ffffff"
SURF2  = "#f0f2f5"
BORDER = "#e2e6ea"
TEXT   = "#2c3e50"
MUTED  = "#6c757d"
RED    = "#c0392b"
RED_L  = "#fdecea"
GREEN  = "#27ae60"
GREEN_L= "#eafaf1"

# Sector colours — work as text on white; backgrounds are light tints
SECTOR_COLOURS: dict[str, str] = {
    "FM":                    "#1a5276",
    "infrastructure":        "#7d6608",
    "ICT":                   "#6c3483",
    "advisory":              "#1a6b3a",
    "health":                "#a93226",
    "security":              "#935116",
    "defence":               "#1a2d4a",
    "utilities":             "#5d6d00",
    "professional_services": "#1f618d",
    "other":                 "#5d6d7e",
}

# ── CSS blocks ────────────────────────────────────────────────────────────────

CSS_VARS = f"""
:root {{
  --bg:      {BG};
  --surface: {WHITE};
  --surf2:   {SURF2};
  --border:  {BORDER};
  --text:    {TEXT};
  --muted:   {MUTED};
  --navy:    {NAVY};
  --gold:    {GOLD};
  --gold-l:  {GOLD_L};
  --navy-l:  {NAVY_L};
  --red:     {RED};
  --red-l:   {RED_L};
  --green:   {GREEN};
  --accent:  {GOLD};
  --font:    'Inter', system-ui, -apple-system, sans-serif;
}}
"""

CSS_RESET = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font);
  font-size: 14px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}
a { color: var(--navy); text-decoration: none; }
a:hover { color: var(--gold); }
"""

CSS_TYPOGRAPHY = """
h1, h2, h3 { color: var(--navy); font-weight: 700; line-height: 1.3; }
p { color: var(--text); line-height: 1.75; margin-bottom: .8rem; }
.label {
  font-size: .65rem;
  font-weight: 700;
  letter-spacing: .09em;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: .35rem;
  display: block;
}
"""

CSS_CARDS = """
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
  box-shadow: 0 1px 4px rgba(26,45,74,.06);
  margin-bottom: 1.25rem;
}
.card-header-bar {
  background: var(--navy);
  color: #fff;
  padding: 1rem 1.5rem;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
}
.card-body-pad { padding: 1.25rem 1.5rem; }
"""

CSS_BADGES = """
.badge {
  display: inline-flex;
  align-items: center;
  padding: .2rem .6rem;
  border-radius: 999px;
  font-size: .68rem;
  font-weight: 600;
  letter-spacing: .03em;
  border: 1px solid;
  white-space: nowrap;
}
.badge-gold  { background: var(--gold-l);  color: #7a5c00;  border-color: var(--gold); }
.badge-navy  { background: var(--navy-l);  color: var(--navy); border-color: #b0bcd4; }
.badge-red   { background: var(--red-l);   color: var(--red);  border-color: #f1a9a0; }
.badge-grey  { background: var(--surf2);   color: var(--muted); border-color: var(--border); }
.sector-badge {
  display: inline-flex;
  align-items: center;
  padding: .2rem .6rem;
  border-radius: 4px;
  font-size: .65rem;
  font-weight: 700;
  letter-spacing: .06em;
  border: 1px solid;
}
"""

CSS_TABLES = """
table { width: 100%; border-collapse: collapse; font-size: .84rem; }
thead tr { background: var(--navy); }
th {
  color: #fff;
  padding: .65rem .85rem;
  text-align: left;
  font-size: .68rem;
  font-weight: 600;
  letter-spacing: .07em;
  text-transform: uppercase;
}
td {
  padding: .55rem .85rem;
  border-bottom: 1px solid var(--border);
  color: var(--text);
  vertical-align: top;
}
tr:last-child td { border-bottom: none; }
tbody tr:hover td { background: var(--surf2); }
"""

CSS_BUTTONS = """
.btn {
  display: inline-flex;
  align-items: center;
  gap: .4rem;
  padding: .5rem 1.1rem;
  background: var(--navy);
  color: #fff;
  border: 1.5px solid var(--navy);
  border-radius: 6px;
  font-size: .82rem;
  font-weight: 600;
  cursor: pointer;
  text-decoration: none;
  transition: background .15s, color .15s;
}
.btn:hover { background: var(--gold); border-color: var(--gold); color: #fff; text-decoration: none; }
.btn-outline {
  background: transparent;
  color: var(--navy);
  border-color: var(--navy);
}
.btn-outline:hover { background: var(--navy); color: #fff; }
"""

CSS_HEADER_FOOTER = """
.gw-page-header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  padding-bottom: 1.25rem;
  border-bottom: 2px solid var(--navy);
  margin-bottom: 2rem;
}
.gw-brand-name {
  font-size: 1.05rem;
  font-weight: 800;
  color: var(--navy);
  letter-spacing: -.01em;
}
.gw-brand-name .by { font-weight: 400; color: var(--muted); font-size: .9rem; }
.gw-product-label {
  font-size: .7rem;
  font-weight: 700;
  letter-spacing: .1em;
  text-transform: uppercase;
  color: var(--gold);
  margin-top: .2rem;
}
.gw-page-footer {
  margin-top: 2.5rem;
  padding-top: 1rem;
  border-top: 1px solid var(--border);
  font-size: .7rem;
  color: var(--muted);
  display: flex;
  justify-content: space-between;
  align-items: center;
}
"""

def all_css(extra: str = "") -> str:
    """Return the complete CSS string for a standalone HTML page."""
    return (
        CSS_VARS + CSS_RESET + CSS_TYPOGRAPHY + CSS_CARDS +
        CSS_BADGES + CSS_TABLES + CSS_BUTTONS + CSS_HEADER_FOOTER + extra
    )


# ── HTML component helpers ────────────────────────────────────────────────────

def header_html(
    subtitle: str = "Procurement Intelligence",
    right_html: str = "",
) -> str:
    """Standard page header with Groundwork by BidEdge branding."""
    return f"""
  <div class="gw-page-header">
    <div>
      <div class="gw-brand-name">Groundwork <span class="by">by BidEdge</span></div>
      <div class="gw-product-label">{subtitle}</div>
    </div>
    {f'<div style="text-align:right;font-size:.78rem;color:var(--muted);">{right_html}</div>' if right_html else ""}
  </div>"""


def footer_html(extra_left: str = "", extra_right: str = "") -> str:
    """Standard page footer with copyright."""
    left  = extra_left  or COPYRIGHT
    right = extra_right or date.today().isoformat()
    return f"""
  <div class="gw-page-footer">
    <span>{left}</span>
    <span>{right}</span>
  </div>"""


def full_page(
    title: str,
    body: str,
    extra_css: str = "",
    max_width: str = "1100px",
) -> str:
    """Wrap body HTML in a complete branded standalone HTML document."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — Groundwork by BidEdge</title>
<style>{all_css(extra_css)}</style>
</head>
<body>
<div style="max-width:{max_width};margin:0 auto;padding:2.5rem 2rem;">
{body}
</div>
</body>
</html>"""


# ── Shared rendering helpers ──────────────────────────────────────────────────

def sector_badge_html(sector: str) -> str:
    colour = SECTOR_COLOURS.get(sector or "other", MUTED)
    label = (sector or "other").replace("_", " ").upper()
    return (
        f'<span class="sector-badge" '
        f'style="background:{colour}18;color:{colour};border-color:{colour}40;">'
        f'{label}</span>'
    )


def dtc_badge_html(dtc: Optional[int]) -> str:
    if dtc is None:
        return '<span class="badge badge-grey">Close TBC</span>'
    if dtc <= 7:
        css, label = "badge-red",  f"URGENT — {dtc}d"
    elif dtc <= 14:
        css, label = "badge-gold", f"Closes in {dtc}d"
    elif dtc <= 30:
        css, label = "badge-navy", f"{dtc} days"
    else:
        css, label = "badge-grey", f"{dtc} days"
    return f'<span class="badge {css}">{label}</span>'


def score_bar_html(score: float, width_px: int = 90) -> str:
    pct = min(100, float(score) / 10 * 100)
    fill = GOLD if pct >= 65 else NAVY if pct >= 40 else MUTED
    return (
        f'<div style="height:4px;background:{BORDER};border-radius:2px;'
        f'overflow:hidden;width:{width_px}px;">'
        f'<div style="height:100%;width:{pct:.0f}%;background:{fill};border-radius:2px;"></div>'
        f'</div>'
    )


def importance_color(importance: str) -> str:
    return {
        "high":   GOLD,
        "medium": NAVY,
        "low":    MUTED,
    }.get((importance or "low").lower(), MUTED)


def maturity_color(maturity: str) -> str:
    return {
        "strong":   GREEN,
        "moderate": NAVY,
        "weak":     MUTED,
    }.get((maturity or "weak").lower(), MUTED)
