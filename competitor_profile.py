"""
Layer 3 — Competitor Profile Report.

Generates a standalone intelligence report on a named competitor,
drawing entirely from MBIE historical award data.

Usage:
  python competitor_profile.py "<Competitor Name>" [--client "<Client Name>"]
"""
import argparse
import json
import logging
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import anthropic

import config
import db
from pursuit_package import _artefact_dir, _slug, _safe, _fmt_value, _paras

logger = logging.getLogger(__name__)


# ── Markdown-to-HTML converter ────────────────────────────────────────────────

def _md_to_html(text: str) -> str:
    """
    Convert a Claude-generated markdown block to safe HTML.
    Handles the subset Claude uses: ##/###, **bold**, _italic_,
    horizontal rules (---), bullet lists, and paragraph breaks.
    Does NOT use a full markdown library to stay dependency-free.
    """
    if not text:
        return ""

    # Escape HTML entities first (work on the raw string)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    lines = text.split("\n")
    out: list[str] = []
    in_ul = False

    for line in lines:
        stripped = line.strip()

        # ── Horizontal rule ---  ───────────────────────────────────────────────
        if re.match(r"^-{3,}$", stripped) or re.match(r"^_{3,}$", stripped) or re.match(r"^\*{3,}$", stripped):
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append('<hr style="border:none;border-top:1px solid var(--border);margin:.75rem 0;">')
            continue

        # ── Headings ## / ### ─────────────────────────────────────────────────
        m = re.match(r"^(#{1,3})\s+(.*)", stripped)
        if m:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            level = len(m.group(1))
            heading_text = _inline_md(m.group(2))
            tag = "h4" if level >= 3 else "h3"
            style = (
                'style="font-size:.88rem;font-weight:700;color:var(--navy);'
                'margin:1rem 0 .35rem;letter-spacing:.01em;"'
            )
            out.append(f"<{tag} {style}>{heading_text}</{tag}>")
            continue

        # ── Bullet list item ─────────────────────────────────────────────────
        m = re.match(r"^[-*•]\s+(.*)", stripped)
        if m:
            if not in_ul:
                out.append('<ul style="margin:.4rem 0 .4rem 1.1rem;padding:0;">')
                in_ul = True
            out.append(f'<li style="margin-bottom:.2rem;color:var(--text);font-size:.85rem;">{_inline_md(m.group(1))}</li>')
            continue

        # Close list if we're in one and hit a non-list line
        if in_ul and stripped:
            out.append("</ul>")
            in_ul = False

        # ── Empty line → paragraph break ──────────────────────────────────────
        if not stripped:
            if out and out[-1] != '<div style="margin-top:.6rem;"></div>':
                out.append('<div style="margin-top:.6rem;"></div>')
            continue

        # ── Plain paragraph line ──────────────────────────────────────────────
        out.append(f'<p style="margin:0 0 .5rem;font-size:.85rem;line-height:1.75;color:var(--text);">{_inline_md(stripped)}</p>')

    if in_ul:
        out.append("</ul>")

    return "\n".join(out)


def _inline_md(text: str) -> str:
    """Convert inline markdown (**bold**, _italic_, `code`) in an already-escaped string."""
    # **bold** or __bold__
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__(.+?)__", r"<strong>\1</strong>", text)
    # *italic* or _italic_
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    text = re.sub(r"_(.+?)_", r"<em>\1</em>", text)
    # `code`
    text = re.sub(r"`(.+?)`", r'<code style="background:var(--surf2);padding:.1em .3em;border-radius:3px;font-size:.82em;">\1</code>', text)
    return text


# ── Data assembly ─────────────────────────────────────────────────────────────

def _get_competitor_data(name: str) -> dict:
    name_q = f"%{name.split()[0]}%"

    totals = db.fetchone(
        """
        SELECT COUNT(DISTINCT n.rfx_id) AS total_wins,
               SUM(n.awarded_amount)     AS total_value,
               AVG(n.awarded_amount)     AS avg_value,
               MIN(n.awarded_amount)     AS min_value,
               MAX(n.awarded_amount)     AS max_value,
               MIN(n.awarded_date)       AS first_win,
               MAX(n.awarded_date)       AS last_win
          FROM mbie_award_notices n
          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
         WHERE n.is_awarded AND LOWER(s.business_name) LIKE LOWER(%s)
        """,
        (name_q,),
    )

    sectors = db.fetchall(
        """
        SELECT c.sector_tag, COUNT(*) AS wins, SUM(n.awarded_amount) AS value
          FROM mbie_award_notices n
          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
          JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
         WHERE n.is_awarded AND LOWER(s.business_name) LIKE LOWER(%s)
           AND c.sector_tag IS NOT NULL
         GROUP BY c.sector_tag ORDER BY wins DESC LIMIT 6
        """,
        (name_q,),
    )

    agencies = db.fetchall(
        """
        SELECT n.posting_agency, COUNT(*) AS wins, SUM(n.awarded_amount) AS value
          FROM mbie_award_notices n
          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
         WHERE n.is_awarded AND LOWER(s.business_name) LIKE LOWER(%s)
         GROUP BY n.posting_agency ORDER BY wins DESC LIMIT 8
        """,
        (name_q,),
    )

    regions = db.fetchall(
        """
        SELECT r.region, COUNT(*) AS wins
          FROM mbie_award_notices n
          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
          JOIN mbie_award_regions r ON r.rfx_id = n.rfx_id
         WHERE n.is_awarded AND LOWER(s.business_name) LIKE LOWER(%s)
           AND r.region NOT IN ('NULL', 'International')
         GROUP BY r.region ORDER BY wins DESC LIMIT 6
        """,
        (name_q,),
    )

    # Recent 12 months
    cutoff = date.today() - timedelta(days=365)
    recent = db.fetchall(
        """
        SELECT n.title, n.posting_agency, n.awarded_date, n.awarded_amount, c.sector_tag
          FROM mbie_award_notices n
          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
          LEFT JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
         WHERE n.is_awarded AND LOWER(s.business_name) LIKE LOWER(%s)
           AND n.awarded_date >= %s
         ORDER BY n.awarded_date DESC LIMIT 10
        """,
        (name_q, cutoff),
    )

    # Value distribution
    value_dist = db.fetchall(
        """
        SELECT
            COUNT(*) FILTER (WHERE n.awarded_amount < 100000) AS under_100k,
            COUNT(*) FILTER (WHERE n.awarded_amount BETWEEN 100000 AND 500000) AS k100_500k,
            COUNT(*) FILTER (WHERE n.awarded_amount BETWEEN 500000 AND 2000000) AS m500k_2m,
            COUNT(*) FILTER (WHERE n.awarded_amount BETWEEN 2000000 AND 10000000) AS m2_10m,
            COUNT(*) FILTER (WHERE n.awarded_amount > 10000000) AS over_10m
          FROM mbie_award_notices n
          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
         WHERE n.is_awarded AND LOWER(s.business_name) LIKE LOWER(%s) AND n.awarded_amount > 0
        """,
        (name_q,),
    )

    # Awards by calendar year
    by_year = db.fetchall(
        """
        SELECT EXTRACT(YEAR FROM n.awarded_date)::int AS year,
               COUNT(*) AS wins,
               SUM(n.awarded_amount) AS value
          FROM mbie_award_notices n
          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
         WHERE n.is_awarded AND LOWER(s.business_name) LIKE LOWER(%s)
           AND n.awarded_date IS NOT NULL
         GROUP BY year ORDER BY year DESC LIMIT 10
        """,
        (name_q,),
    )

    # Largest single contract (with agency and date)
    largest_contract = db.fetchone(
        """
        SELECT n.title, n.posting_agency, n.awarded_amount, n.awarded_date
          FROM mbie_award_notices n
          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
         WHERE n.is_awarded AND LOWER(s.business_name) LIKE LOWER(%s)
           AND n.awarded_amount IS NOT NULL
         ORDER BY n.awarded_amount DESC LIMIT 1
        """,
        (name_q,),
    )

    # Most recent 5 wins (all-time, not just last 12 months)
    recent5 = db.fetchall(
        """
        SELECT n.title, n.posting_agency, n.awarded_date, n.awarded_amount, c.sector_tag
          FROM mbie_award_notices n
          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
          LEFT JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
         WHERE n.is_awarded AND LOWER(s.business_name) LIKE LOWER(%s)
         ORDER BY n.awarded_date DESC NULLS LAST LIMIT 5
        """,
        (name_q,),
    )

    return {
        "name": name,
        "totals": dict(totals) if totals else {},
        "sectors": [dict(r) for r in sectors],
        "agencies": [dict(r) for r in agencies],
        "regions": [dict(r) for r in regions],
        "recent": [dict(r) for r in recent],
        "value_dist": dict(value_dist[0]) if value_dist else {},
        "by_year": [dict(r) for r in by_year],
        "largest_contract": dict(largest_contract) if largest_contract else {},
        "recent5": [dict(r) for r in recent5],
    }


def _get_head_to_head(competitor_name: str, client_name: str) -> list[dict]:
    """Find agencies where both competitor and client have MBIE records."""
    comp_q = f"%{competitor_name.split()[0]}%"
    client_q = f"%{client_name.split()[0]}%"

    return db.fetchall(
        """
        WITH comp AS (
            SELECT n.posting_agency, COUNT(*) AS comp_wins, SUM(n.awarded_amount) AS comp_value
              FROM mbie_award_notices n
              JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
             WHERE n.is_awarded AND LOWER(s.business_name) LIKE LOWER(%s)
             GROUP BY n.posting_agency
        ),
        cli AS (
            SELECT n.posting_agency, COUNT(*) AS cli_wins, SUM(n.awarded_amount) AS cli_value
              FROM mbie_award_notices n
              JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
             WHERE n.is_awarded AND LOWER(s.business_name) LIKE LOWER(%s)
             GROUP BY n.posting_agency
        )
        SELECT comp.posting_agency,
               comp.comp_wins, comp.comp_value,
               COALESCE(cli.cli_wins, 0) AS cli_wins,
               COALESCE(cli.cli_value, 0) AS cli_value
          FROM comp
          LEFT JOIN cli ON cli.posting_agency = comp.posting_agency
         ORDER BY comp.comp_wins DESC
         LIMIT 8
        """,
        (comp_q, client_q),
    )


# ── Claude synthesis ──────────────────────────────────────────────────────────

def _generate_profile_insight(data: dict, client_name: Optional[str] = None) -> str:
    """Call Claude to generate a competitive intelligence paragraph from profile data."""
    name = data.get("name", "Unknown")
    totals = data.get("totals", {})
    sectors = data.get("sectors", [])
    agencies = data.get("agencies", [])
    by_year = data.get("by_year", [])
    largest = data.get("largest_contract", {})
    recent5 = data.get("recent5", [])

    # Format by_year summary
    year_lines = "\n".join(
        f"  {r['year']}: {r['wins']} wins ({_fmt_value(r.get('value'))} total)"
        for r in by_year[:6]
    ) or "  Insufficient data."

    # Format top sectors
    sector_lines = "\n".join(
        f"  {r['sector_tag']}: {r['wins']} wins ({_fmt_value(r.get('value'))} total)"
        for r in sectors[:5]
    ) or "  No sector data."

    # Format top agencies
    agency_lines = "\n".join(
        f"  {r['posting_agency']}: {r['wins']} wins ({_fmt_value(r.get('value'))} total)"
        for r in agencies[:5]
    ) or "  No agency data."

    # Largest contract
    if largest:
        largest_line = (
            f"{_safe(largest.get('title', 'Unknown'))} — "
            f"{_safe(largest.get('posting_agency', ''))} — "
            f"{_fmt_value(largest.get('awarded_amount'))} — "
            f"{str(largest.get('awarded_date', ''))[:10]}"
        )
    else:
        largest_line = "Not available."

    # Most recent 5 wins
    recent5_lines = "\n".join(
        f"  {str(r.get('awarded_date', ''))[:10]}  {_safe(r.get('title', ''))[:55]}  "
        f"({_safe(r.get('posting_agency', ''))})  {_fmt_value(r.get('awarded_amount'))}"
        for r in recent5
    ) or "  No recent wins found."

    client_context = (
        f"\nThis profile is being prepared for {client_name}, who competes in the same market."
        if client_name else ""
    )

    prompt = f"""You are a competitive intelligence analyst producing a written assessment of {name} for a senior business development professional in New Zealand's infrastructure and facilities management sector.{client_context}

Use the following factual data to write a sharp, specific 3–4 paragraph competitive profile. Do not pad, do not be generic. Every claim must be grounded in the numbers below.

=== TOTALS ===
Total MBIE wins: {totals.get('total_wins', 0)}
Total awarded value: {_fmt_value(totals.get('total_value'))}
Average contract value: {_fmt_value(totals.get('avg_value'))}
First win recorded: {str(totals.get('first_win', ''))[:10]}
Most recent win recorded: {str(totals.get('last_win', ''))[:10]}

=== WINS BY YEAR ===
{year_lines}

=== WINS BY SECTOR ===
{sector_lines}

=== TOP AGENCIES ===
{agency_lines}

=== LARGEST SINGLE CONTRACT ===
{largest_line}

=== MOST RECENT 5 WINS ===
{recent5_lines}

Write 3–4 paragraphs covering: (1) overall scale and trajectory — are they growing, shrinking, or stable? (2) sector and agency concentration — where are they dominant and what does that signal? (3) what this means for a competitor — where are they vulnerable, where are they strong, and what tactics should be considered?

Be specific. Use actual figures. Tone: competitive intelligence analyst, not marketing copy."""

    try:
        client_api = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        msg = client_api.messages.create(
            model=config.CLAUDE_MODEL_L3,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as exc:
        logger.warning("Claude insight generation failed for %s: %s", name, exc)
        return ""


# ── HTML rendering ─────────────────────────────────────────────────────────────

_PROFILE_CSS = """:root {
  --bg:#f5f6f8; --surface:#ffffff; --surf2:#f0f2f5; --border:#e2e6ea;
  --text:#2c3e50; --muted:#6c757d; --navy:#1a2d4a; --gold:#2a9d8f;
  --gold-l:#e0f4f2; --navy-l:#e8ecf3; --red:#c0392b; --red-l:#fdecea;
  --green:#27ae60;
}
*, *::before, *::after { box-sizing:border-box; margin:0; padding:0; }
body { background:var(--bg); color:var(--text);
       font-family:'Inter',system-ui,-apple-system,sans-serif;
       font-size:14px; line-height:1.6; padding:2.5rem;
       max-width:960px; margin:0 auto; -webkit-font-smoothing:antialiased; }
a { color:var(--navy); text-decoration:none; }
a:hover { color:var(--gold); }
.header { display:flex; justify-content:space-between; align-items:flex-end;
  border-bottom:2px solid var(--navy); padding-bottom:1.25rem; margin-bottom:2.5rem; }
.header-title-label { font-size:.65rem; font-weight:700; letter-spacing:.1em;
  text-transform:uppercase; color:var(--gold); margin-bottom:.3rem; }
.header-name { font-size:1.55rem; font-weight:800; color:var(--navy); }
.header-meta { font-size:.75rem; color:var(--muted); text-align:right; }
.stat-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:1rem; margin-bottom:2rem; }
.stat-box { background:var(--surface); border:1px solid var(--border);
  border-radius:8px; padding:.85rem 1rem;
  box-shadow:0 1px 3px rgba(26,45,74,.06); }
.stat-label { font-size:.65rem; font-weight:700; letter-spacing:.07em;
  text-transform:uppercase; color:var(--muted); margin-bottom:.25rem; }
.stat-value { font-size:1.3rem; font-weight:800; color:var(--navy); letter-spacing:-.03em; }
.stat-sub { font-size:.7rem; color:var(--muted); margin-top:.15rem; }
.section { margin-bottom:2.5rem; }
.section-title { font-size:.72rem; font-weight:700; letter-spacing:.1em;
  text-transform:uppercase; color:var(--navy); margin-bottom:1rem;
  padding-bottom:.4rem; border-bottom:2px solid var(--border); }
table { width:100%; border-collapse:collapse; font-size:.82rem; }
thead tr { background:var(--navy); }
th { color:#fff; font-size:.65rem; font-weight:600; letter-spacing:.07em;
  text-transform:uppercase; padding:.45rem .65rem; text-align:left; }
td { padding:.5rem .65rem; border-bottom:1px solid var(--border); color:var(--text); }
tr:last-child td { border-bottom:none; }
tbody tr:hover td { background:var(--surf2); }
.win-bar { height:8px; background:var(--surf2); border-radius:4px;
  overflow:hidden; width:100px; display:inline-block;
  vertical-align:middle; margin-left:.5rem; }
.win-bar-fill { height:100%; background:var(--gold); border-radius:4px; }
.doc-footer { margin-top:3rem; padding-top:1rem;
  border-top:1px solid var(--border); font-size:.7rem; color:var(--muted);
  display:flex; justify-content:space-between; }

/* ── Tablet ≤768px ── */
@media (max-width:768px) {
  body { padding:1.5rem 1rem; }
  .header { flex-direction:column; align-items:flex-start; gap:.5rem; }
  .header-meta { text-align:left; }
  .stat-grid { grid-template-columns:1fr 1fr; }
  table { display:block; overflow-x:auto; -webkit-overflow-scrolling:touch; }
  .doc-footer { flex-direction:column; gap:.3rem; }
}

/* ── Phone ≤480px ── */
@media (max-width:480px) {
  body { padding:1rem .75rem; font-size:13px; }
  .header-name { font-size:1.2rem; }
  .stat-grid { grid-template-columns:1fr 1fr; gap:.6rem; }
  .stat-box { padding:.65rem .75rem; }
  .stat-value { font-size:1.05rem; }
  .win-bar { width:60px; }
  td, th { padding:.4rem .5rem; font-size:.76rem; }
}"""


def _render_profile_html(data: dict, client_name: Optional[str] = None,
                          h2h: Optional[list[dict]] = None,
                          insight: str = "") -> str:
    totals = data.get("totals", {})
    name = data.get("name", "Unknown")
    run_date = date.today().isoformat()

    total_wins = int(totals.get("total_wins") or 0)
    total_value = float(totals.get("total_value") or 0)
    avg_value = float(totals.get("avg_value") or 0)
    first_win = str(totals.get("first_win", ""))[:7]
    last_win = str(totals.get("last_win", ""))[:7]

    primary_sectors = data.get("sectors", [])
    primary_sector = primary_sectors[0]["sector_tag"] if primary_sectors else "unknown"
    regions = [r["region"] for r in data.get("regions", [])]

    # Sector table
    max_wins = max((s["wins"] for s in primary_sectors), default=1)
    sector_rows = ""
    for s in primary_sectors:
        pct = int(s["wins"] / max_wins * 100)
        sector_rows += (
            f"<tr><td>{_safe(s['sector_tag'])}</td>"
            f"<td>{s['wins']} "
            f'<span class="win-bar"><span class="win-bar-fill" style="width:{pct}%"></span></span>'
            f"</td><td>{_fmt_value(s.get('value'))}</td></tr>"
        )

    # Agency table
    max_ag = max((a["wins"] for a in data.get("agencies", [])), default=1)
    agency_rows = ""
    for a in data.get("agencies", []):
        pct = int(a["wins"] / max_ag * 100)
        agency_rows += (
            f"<tr><td>{_safe(a['posting_agency'])[:55]}</td>"
            f"<td>{a['wins']} "
            f'<span class="win-bar"><span class="win-bar-fill" style="width:{pct}%"></span></span>'
            f"</td><td>{_fmt_value(a.get('value'))}</td></tr>"
        )

    # Recent wins table
    recent_rows = ""
    for r in data.get("recent", []):
        recent_rows += (
            f"<tr><td>{_safe(r.get('title', ''))[:60]}</td>"
            f"<td>{_safe(r.get('posting_agency', ''))[:35]}</td>"
            f"<td>{_fmt_value(r.get('awarded_amount'))}</td>"
            f"<td>{str(r.get('awarded_date', ''))[:10]}</td></tr>"
        )

    # Value distribution
    vd = data.get("value_dist", {})
    vd_total = sum([
        int(vd.get("under_100k") or 0),
        int(vd.get("k100_500k") or 0),
        int(vd.get("m500k_2m") or 0),
        int(vd.get("m2_10m") or 0),
        int(vd.get("over_10m") or 0),
    ])
    def vd_bar(count):
        if not vd_total:
            return ""
        pct = int(int(count or 0) / vd_total * 100)
        return f'<span class="win-bar"><span class="win-bar-fill" style="width:{pct}%"></span></span>'

    # Head-to-head section
    h2h_html = ""
    if client_name and h2h:
        h2h_rows = ""
        for row in h2h:
            comp_wins = row.get("comp_wins", 0)
            cli_wins = row.get("cli_wins", 0)
            if comp_wins > cli_wins:
                adv = f'<span style="color:var(--red);">Competitor leads ({comp_wins} vs {cli_wins})</span>'
            elif cli_wins > comp_wins:
                adv = f'<span style="color:var(--green);">Client leads ({cli_wins} vs {comp_wins})</span>'
            else:
                adv = f'<span style="color:var(--amber);">Even ({comp_wins} each)</span>'
            h2h_rows += (
                f"<tr><td>{_safe(row.get('posting_agency', ''))[:50]}</td>"
                f"<td>{adv}</td>"
                f"<td>{_fmt_value(row.get('comp_value'))}</td>"
                f"<td>{_fmt_value(row.get('cli_value') or 0)}</td></tr>"
            )
        h2h_html = f"""
        <div class="section">
          <div class="section-title">Head-to-Head vs {_safe(client_name)}</div>
          <table>
            <thead><tr>
              <th>Agency</th><th>Advantage</th>
              <th>{_safe(name)} value</th><th>{_safe(client_name)} value</th>
            </tr></thead>
            <tbody>{h2h_rows}</tbody>
          </table>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Competitor Profile — {_safe(name)}</title>
<style>{_PROFILE_CSS}</style>
</head>
<body>

<div class="header">
  <div>
    <div class="header-title-label">Competitor Intelligence Profile</div>
    <div class="header-name">{_safe(name)}</div>
  </div>
  <div class="header-meta">
    Generated {run_date}<br>
    Source: MBIE GETS Open Data (2014–2025)
  </div>
</div>

{f'''<div class="section" style="background:var(--navy-l);border:1px solid var(--border);border-radius:8px;padding:1.25rem 1.5rem;margin-bottom:2rem;">
  <div class="section-title" style="color:var(--navy);margin-bottom:.75rem;">Intelligence Assessment</div>
  <div>{_md_to_html(insight)}</div>
</div>''' if insight else ""}

<div class="stat-grid">
  <div class="stat-box">
    <div class="stat-label">Total wins (MBIE)</div>
    <div class="stat-value">{total_wins}</div>
    <div class="stat-sub">{first_win} – {last_win}</div>
  </div>
  <div class="stat-box">
    <div class="stat-label">Total awarded value</div>
    <div class="stat-value">{_fmt_value(total_value)}</div>
  </div>
  <div class="stat-box">
    <div class="stat-label">Average contract</div>
    <div class="stat-value">{_fmt_value(avg_value)}</div>
  </div>
  <div class="stat-box">
    <div class="stat-label">Primary sector</div>
    <div class="stat-value" style="font-size:.95rem;">{_safe(primary_sector.replace("_"," "))}</div>
    <div class="stat-sub">{", ".join(regions[:3]) or "National"}</div>
  </div>
</div>

<div class="section">
  <div class="section-title">Win Record by Sector</div>
  <table>
    <thead><tr><th>Sector</th><th>Wins</th><th>Total value</th></tr></thead>
    <tbody>{sector_rows}</tbody>
  </table>
</div>

<div class="section">
  <div class="section-title">Top Agencies</div>
  <table>
    <thead><tr><th>Agency</th><th>Awards</th><th>Total value</th></tr></thead>
    <tbody>{agency_rows}</tbody>
  </table>
</div>

<div class="section">
  <div class="section-title">Contract Value Distribution</div>
  <table>
    <thead><tr><th>Value band</th><th>Count</th></tr></thead>
    <tbody>
      <tr><td>&lt; $100K</td><td>{vd.get('under_100k',0)} {vd_bar(vd.get('under_100k',0))}</td></tr>
      <tr><td>$100K – $500K</td><td>{vd.get('k100_500k',0)} {vd_bar(vd.get('k100_500k',0))}</td></tr>
      <tr><td>$500K – $2M</td><td>{vd.get('m500k_2m',0)} {vd_bar(vd.get('m500k_2m',0))}</td></tr>
      <tr><td>$2M – $10M</td><td>{vd.get('m2_10m',0)} {vd_bar(vd.get('m2_10m',0))}</td></tr>
      <tr><td>&gt; $10M</td><td>{vd.get('over_10m',0)} {vd_bar(vd.get('over_10m',0))}</td></tr>
    </tbody>
  </table>
</div>

<div class="section">
  <div class="section-title">Recent Activity — Last 12 Months</div>
  {"<table><thead><tr><th>Contract</th><th>Agency</th><th>Value</th><th>Date</th></tr></thead><tbody>"
   + recent_rows + "</tbody></table>"
   if recent_rows
   else '<div style="font-size:.82rem;color:var(--muted);font-style:italic;">No MBIE award records in the past 12 months.</div>'}
</div>

{h2h_html}

<div class="doc-footer">
  <span>Procint Layer 3 &nbsp;|&nbsp; {_safe(name)} competitor profile &nbsp;|&nbsp; {run_date}</span>
  <span>MBIE GETS Open Data — 27,948 award notices (2014–2025)</span>
</div>

</body>
</html>"""


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_competitor_profile(
    competitor_name: str,
    client_name: Optional[str] = None,
    output_dir: Optional[Path] = None,
) -> Path:
    logger.info("Generating competitor profile: %s", competitor_name)

    data = _get_competitor_data(competitor_name)
    h2h = _get_head_to_head(competitor_name, client_name) if client_name else None
    insight = _generate_profile_insight(data, client_name=client_name)
    html = _render_profile_html(data, client_name=client_name, h2h=h2h, insight=insight)

    if output_dir is None:
        folder_name = client_name or f"competitor_{_slug(competitor_name)}"
        output_dir = _artefact_dir(folder_name)

    filename = f"competitor_{_slug(competitor_name)}.html"
    out_path = output_dir / filename
    out_path.write_text(html, encoding="utf-8")
    logger.info("Competitor profile written to %s", out_path)
    return out_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("competitor_name")
    p.add_argument("--client", default=None)
    p.add_argument("--output-dir", default=None)
    args = p.parse_args()

    out = generate_competitor_profile(
        args.competitor_name,
        client_name=args.client,
        output_dir=Path(args.output_dir) if args.output_dir else None,
    )
    print(f"Generated: {out}")
