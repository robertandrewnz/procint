"""
Layer 3 — Competitor Profile Report.

Generates a standalone intelligence report on a named competitor,
drawing entirely from MBIE historical award data.

Usage:
  python competitor_profile.py "<Competitor Name>" [--client "<Client Name>"]
"""
import argparse
import logging
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import config
import db
from pursuit_package import _artefact_dir, _slug, _safe, _fmt_value, _paras

logger = logging.getLogger(__name__)

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

    return {
        "name": name,
        "totals": dict(totals) if totals else {},
        "sectors": [dict(r) for r in sectors],
        "agencies": [dict(r) for r in agencies],
        "regions": [dict(r) for r in regions],
        "recent": [dict(r) for r in recent],
        "value_dist": dict(value_dist[0]) if value_dist else {},
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


# ── HTML rendering ─────────────────────────────────────────────────────────────

_PROFILE_CSS = """
:root {
  --bg: #0d1117; --surface: #161b22; --surf2: #1c2230;
  --border: #2a3344; --text: #e6edf3; --muted: #7d8fa8;
  --accent: #4f9cf9; --red: #ef4444; --amber: #facc15; --green: #22c55e;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text);
       font-family: 'Inter', system-ui, sans-serif; font-size: 14px;
       line-height: 1.6; padding: 2.5rem; max-width: 960px; margin: 0 auto; }
.header { display: flex; justify-content: space-between; align-items: flex-end;
          border-bottom: 2px solid var(--border); padding-bottom: 1.25rem; margin-bottom: 2.5rem; }
.header-title-label { font-size: .7rem; font-weight: 700; letter-spacing: .1em;
                       text-transform: uppercase; color: var(--accent); margin-bottom: .3rem; }
.header-name { font-size: 1.6rem; font-weight: 800; color: var(--text); }
.header-meta { font-size: .75rem; color: var(--muted); text-align: right; }

.stat-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; margin-bottom: 2rem; }
.stat-box { background: var(--surface); border: 1px solid var(--border);
            border-radius: 8px; padding: .85rem 1rem; }
.stat-label { font-size: .65rem; font-weight: 700; letter-spacing: .07em;
               text-transform: uppercase; color: var(--muted); margin-bottom: .25rem; }
.stat-value { font-size: 1.3rem; font-weight: 800; color: var(--text); letter-spacing: -.03em; }
.stat-sub   { font-size: .7rem; color: var(--muted); margin-top: .15rem; }

.section { margin-bottom: 2.5rem; }
.section-title { font-size: .75rem; font-weight: 700; letter-spacing: .1em;
                  text-transform: uppercase; color: var(--accent); margin-bottom: 1rem;
                  padding-bottom: .4rem; border-bottom: 1px solid var(--border); }
table { width: 100%; border-collapse: collapse; font-size: .82rem; }
th { font-size: .65rem; font-weight: 700; letter-spacing: .07em; text-transform: uppercase;
     color: var(--muted); padding: .45rem .65rem; border-bottom: 1px solid var(--border);
     text-align: left; }
td { padding: .5rem .65rem; border-bottom: 1px solid var(--border); color: var(--text); }
tr:last-child td { border-bottom: none; }
tr:nth-child(even) td { background: var(--surf2); }
.win-bar { height: 8px; background: var(--surf2); border-radius: 4px; overflow: hidden;
           width: 100px; display: inline-block; vertical-align: middle; margin-left: .5rem; }
.win-bar-fill { height: 100%; background: var(--accent); border-radius: 4px; }
.doc-footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--border);
              font-size: .7rem; color: var(--muted); display: flex; justify-content: space-between; }
"""


def _render_profile_html(data: dict, client_name: Optional[str] = None,
                          h2h: Optional[list[dict]] = None) -> str:
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
                f"<td>{_safe(adv)}</td>"
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
    html = _render_profile_html(data, client_name=client_name, h2h=h2h)

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
