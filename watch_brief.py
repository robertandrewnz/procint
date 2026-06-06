"""
Layer 3 — Weekly Procurement Watch Brief.

A concise executive briefing document (1-2 pages) generated weekly,
personalised for a named client. Covers:
  - Top 5 strategic opportunities active this week
  - Market signals from pattern detection
  - Competitor moves (recent MBIE award activity)
  - Renewal radar (contracts expiring within 90 days)
  - One synthesised market insight

Output: output/artefacts/{client_slug}/{date}/watch_brief_{date}.html

Usage:
  python watch_brief.py "<Client Name>" [--sectors FM,infrastructure]
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
from pursuit_package import _artefact_dir, _slug, _safe, _fmt_value

logger = logging.getLogger(__name__)

_SECTOR_COLOURS = {
    "FM": "#4f9cf9", "infrastructure": "#f97316", "ICT": "#a78bfa",
    "advisory": "#34d399", "health": "#f472b6", "security": "#fb923c",
    "defence": "#ef4444", "utilities": "#facc15",
    "professional_services": "#38bdf8", "other": "#94a3b8",
}

# ── Data assembly ─────────────────────────────────────────────────────────────

def _top_opportunities(sectors: Optional[list[str]] = None, limit: int = 5) -> list[dict]:
    """Top scored notices active in the last 7 days."""
    sector_filter = ""
    params = [config.PRIORITY_THRESHOLD, limit]
    if sectors:
        placeholders = ",".join(["%s"] * len(sectors))
        sector_filter = f"AND p.sector_tag IN ({placeholders})"
        params = [config.PRIORITY_THRESHOLD] + sectors + [limit]

    return db.fetchall(
        f"""
        SELECT r.notice_id, r.title, r.agency, r.source_url, r.close_date,
               p.sector_tag, p.value_band, p.days_until_close,
               s.composite_score,
               e.summary, e.strategic_framing, e.red_flags
          FROM raw_notices r
          JOIN parsed_notices p ON p.notice_id = r.notice_id
          JOIN scored_notices s ON s.notice_id = r.notice_id
          LEFT JOIN enriched_notices e ON e.notice_id = r.notice_id
         WHERE s.composite_score >= %s
           {sector_filter}
         ORDER BY s.composite_score DESC, p.days_until_close ASC NULLS LAST
         LIMIT %s
        """,
        params,
    )


def _market_signals() -> list[dict]:
    """Pattern flags of type sector_spike or procurement_surge."""
    return db.fetchall(
        """
        SELECT flag_type, description, severity, detected_at, sector_tag
          FROM pattern_flags
         WHERE flag_type IN ('sector_spike', 'procurement_surge')
           AND (expires_at IS NULL OR expires_at >= CURRENT_DATE)
         ORDER BY severity DESC, detected_at DESC
         LIMIT 5
        """,
    )


def _competitor_moves(client_name: str, sectors: Optional[list[str]]) -> list[dict]:
    """Recent MBIE awards (last 90 days) in client-relevant sectors."""
    cutoff = date.today() - timedelta(days=90)
    sector_filter = ""
    params: list = [cutoff]
    if sectors:
        placeholders = ",".join(["%s"] * len(sectors))
        sector_filter = f"AND c.sector_tag IN ({placeholders})"
        params = [cutoff] + sectors

    rows = db.fetchall(
        f"""
        SELECT n.title, n.posting_agency, n.awarded_date, n.awarded_amount,
               s.business_name AS supplier_name, c.sector_tag
          FROM mbie_award_notices n
          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
          JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
         WHERE n.is_awarded
           AND n.awarded_date >= %s
           AND n.awarded_amount > 0
           {sector_filter}
           AND LOWER(s.business_name) NOT LIKE LOWER(%s)
         ORDER BY n.awarded_date DESC
         LIMIT 8
        """,
        params + [f"%{client_name.split()[0]}%"],
    )
    return [dict(r) for r in rows]


def _renewal_radar() -> list[dict]:
    """
    Contracts approaching expiry in next 90 days.
    Uses the Layer 2 contract_awards table (populated by awards.py scraper).
    Falls back to empty list if no end_date data exists yet.
    """
    window = date.today() + timedelta(days=config.RENEWAL_WINDOW_DAYS)
    try:
        return db.fetchall(
            """
            SELECT ca.title, ca.agency_name_raw AS posting_agency,
                   ca.award_date, ca.contract_value AS awarded_amount,
                   ca.end_date, o.name AS incumbent,
                   ca.sector_tag,
                   (ca.end_date - CURRENT_DATE) AS days_remaining
              FROM contract_awards ca
              LEFT JOIN organisations o ON o.org_id = ca.supplier_org_id
             WHERE ca.end_date IS NOT NULL
               AND ca.end_date BETWEEN CURRENT_DATE AND %s
               AND ca.contract_value > 0
             ORDER BY ca.end_date ASC
             LIMIT 8
            """,
            (window,),
        )
    except Exception:
        return []


def _loss_streak_flags() -> list[dict]:
    """Loss streak flags for intelligence gap awareness."""
    return db.fetchall(
        """
        SELECT description, severity, sector_tag
          FROM pattern_flags
         WHERE flag_type = 'loss_streak'
           AND (expires_at IS NULL OR expires_at >= CURRENT_DATE)
         ORDER BY severity DESC
         LIMIT 3
        """,
    )


# ── Claude insight synthesis ──────────────────────────────────────────────────

def _generate_insight(opportunities: list[dict], signals: list[dict],
                      client_name: str) -> str:
    """One synthesised market observation from Claude."""
    if not opportunities:
        return "Insufficient data for market observation this week."

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    top_sectors = list({o.get("sector_tag", "other") for o in opportunities})
    top_agencies = [o.get("agency", "") for o in opportunities[:3]]
    signal_descs = [s.get("description", "")[:100] for s in signals[:2]]

    prompt = (
        f"You are a procurement intelligence analyst. Based on the following NZ government "
        f"procurement data for the week, write exactly ONE short paragraph (3-4 sentences) "
        f"that synthesises the single most strategically significant market observation. "
        f"Cite specific agencies or sectors. Do not use bullet points. Return plain text only.\n\n"
        f"Client: {client_name}\n"
        f"Active sectors: {', '.join(top_sectors)}\n"
        f"Most active agencies: {', '.join(top_agencies)}\n"
        f"Pattern signals: {'; '.join(signal_descs) or 'None this week'}\n"
        f"Number of active high-priority opportunities: {len(opportunities)}"
    )
    try:
        msg = client.messages.create(
            model=config.CLAUDE_MODEL_L3,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as exc:
        logger.warning("Claude insight failed: %s", exc)
        return "Market observation unavailable this week."


# ── HTML rendering ─────────────────────────────────────────────────────────────

_BRIEF_CSS = """
:root {
  --bg: #0d1117; --surface: #161b22; --surf2: #1c2230;
  --border: #2a3344; --text: #e6edf3; --muted: #7d8fa8;
  --accent: #4f9cf9; --green: #22c55e; --amber: #facc15; --red: #ef4444;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text);
       font-family: 'Inter', system-ui, -apple-system, sans-serif;
       font-size: 14px; line-height: 1.6; padding: 2.5rem; max-width: 860px; margin: 0 auto; }

.brief-header { display: flex; justify-content: space-between; align-items: flex-end;
                border-bottom: 2px solid var(--border); padding-bottom: 1.25rem; margin-bottom: 2rem; }
.brief-title-label { font-size: .7rem; font-weight: 700; letter-spacing: .1em;
                      text-transform: uppercase; color: var(--accent); margin-bottom: .3rem; }
.brief-title { font-size: 1.4rem; font-weight: 800; color: var(--text); }
.brief-meta  { font-size: .75rem; color: var(--muted); text-align: right; }
.brief-meta strong { display: block; font-size: 1rem; font-weight: 700; color: var(--text); }

.section { margin-bottom: 2.5rem; }
.section-title { font-size: .75rem; font-weight: 700; letter-spacing: .1em;
                  text-transform: uppercase; color: var(--accent); margin-bottom: 1rem;
                  padding-bottom: .4rem; border-bottom: 1px solid var(--border); }

/* Opportunity cards */
.opp-card { background: var(--surface); border: 1px solid var(--border);
            border-radius: 8px; padding: 1rem 1.25rem; margin-bottom: .75rem; }
.opp-header { display: flex; justify-content: space-between; align-items: flex-start;
              margin-bottom: .4rem; gap: 1rem; }
.opp-title { font-size: .9rem; font-weight: 600; color: var(--text); flex: 1; }
.opp-score { font-size: 1rem; font-weight: 800; color: var(--text); flex-shrink: 0; }
.opp-agency { font-size: .75rem; color: var(--muted); margin-bottom: .5rem; }
.opp-chips  { display: flex; flex-wrap: wrap; gap: .4rem; margin-bottom: .6rem; }
.chip { font-size: .65rem; font-weight: 600; padding: .18rem .5rem; border-radius: 999px; border: 1px solid; }
.chip-blue   { background: #4f9cf922; color: var(--accent); border-color: #4f9cf940; }
.chip-amber  { background: #facc1522; color: var(--amber);  border-color: #facc1540; }
.chip-red    { background: #ef444422; color: var(--red);    border-color: #ef444440; }
.chip-grey   { background: #94a3b822; color: var(--muted);  border-color: #94a3b840; }
.opp-summary { font-size: .82rem; color: var(--muted); line-height: 1.6; }
.opp-link { font-size: .75rem; color: var(--accent); text-decoration: none; }

/* Signals / radar */
.signal-row { display: flex; gap: .75rem; align-items: flex-start;
              padding: .6rem .85rem; border: 1px solid var(--border);
              border-radius: 6px; margin-bottom: .5rem; font-size: .82rem; }
.signal-sev { flex-shrink: 0; font-size: .65rem; font-weight: 700; padding: .18rem .45rem;
              border-radius: 4px; text-transform: uppercase; }
.sev-high   { background: #ef444422; color: var(--red); }
.sev-medium { background: #facc1522; color: var(--amber); }
.sev-low    { background: #4f9cf922; color: var(--accent); }

/* Competitor / renewal table */
table { width: 100%; border-collapse: collapse; font-size: .8rem; margin-bottom: .5rem; }
th { font-size: .65rem; font-weight: 700; letter-spacing: .07em; text-transform: uppercase;
     color: var(--muted); padding: .45rem .65rem; border-bottom: 1px solid var(--border); text-align: left; }
td { padding: .5rem .65rem; border-bottom: 1px solid var(--border); color: var(--text); }
tr:last-child td { border-bottom: none; }
tr:nth-child(even) td { background: var(--surf2); }

/* Insight box */
.insight { background: #4f9cf910; border: 1px solid #4f9cf930; border-radius: 8px;
           padding: 1.25rem 1.5rem; font-size: .88rem; color: var(--text);
           line-height: 1.7; font-style: italic; }
.insight-label { font-size: .65rem; font-weight: 700; letter-spacing: .08em;
                  text-transform: uppercase; color: var(--accent); display: block; margin-bottom: .5rem; }

.doc-footer { margin-top: 2.5rem; padding-top: 1rem; border-top: 1px solid var(--border);
              font-size: .7rem; color: var(--muted); display: flex; justify-content: space-between; }
"""


def _sector_colour(sector: str) -> str:
    return _SECTOR_COLOURS.get(sector or "other", "#94a3b8")


def _render_brief_html(
    client_name: str,
    run_date: date,
    opportunities: list[dict],
    signals: list[dict],
    competitor_moves: list[dict],
    renewals: list[dict],
    insight: str,
) -> str:

    # Opportunity cards
    opp_cards = ""
    for i, opp in enumerate(opportunities, 1):
        sector = opp.get("sector_tag", "other")
        sc = _sector_colour(sector)
        dtc = opp.get("days_until_close")
        if dtc is not None and dtc <= 7:
            urg_css, urg_label = "chip-red", f"{dtc}d — URGENT"
        elif dtc is not None:
            urg_css, urg_label = "chip-amber", f"{dtc} days to close"
        else:
            urg_css, urg_label = "chip-grey", "Close TBC"

        summary_text = opp.get("summary") or opp.get("strategic_framing") or ""
        summary_text = summary_text[:220] + ("..." if len(summary_text) > 220 else "")

        opp_cards += (
            f'<div class="opp-card">'
            f'<div class="opp-header">'
            f'<div class="opp-title">#{i} &nbsp;{_safe(opp.get("title", ""))}</div>'
            f'<div class="opp-score">{float(opp.get("composite_score") or 0):.1f}/10</div>'
            f'</div>'
            f'<div class="opp-agency">{_safe(opp.get("agency", ""))}</div>'
            f'<div class="opp-chips">'
            f'<span class="chip" style="background:{sc}22;color:{sc};border-color:{sc}44;">'
            f'{sector.replace("_"," ").upper()}</span>'
            f'<span class="chip {urg_css}">{_safe(urg_label)}</span>'
            f'</div>'
            f'<div class="opp-summary">{_safe(summary_text)}</div>'
            f'<a class="opp-link" href="{_safe(opp.get("source_url", "#"))}" target="_blank">View on GETS &#8599;</a>'
            f'</div>'
        )

    # Signals
    sig_rows = ""
    for sig in signals:
        sev = (sig.get("severity") or "medium").lower()
        sig_rows += (
            f'<div class="signal-row">'
            f'<span class="signal-sev sev-{sev}">{sev}</span>'
            f'<span>{_safe(sig.get("description", "")[:160])}</span>'
            f'</div>'
        )
    if not sig_rows:
        sig_rows = '<div style="font-size:.82rem;color:var(--muted);font-style:italic;">No unusual market signals detected this week.</div>'

    # Competitor moves table
    comp_rows = ""
    for cm in competitor_moves:
        comp_rows += (
            f"<tr><td>{_safe(cm.get('supplier_name', ''))}</td>"
            f"<td>{_safe(cm.get('posting_agency', ''))[:45]}</td>"
            f"<td>{_fmt_value(cm.get('awarded_amount'))}</td>"
            f"<td>{str(cm.get('awarded_date', ''))[:10]}</td></tr>"
        )
    comp_table = (
        f"<table><thead><tr><th>Supplier</th><th>Agency</th><th>Value</th><th>Date</th></tr></thead>"
        f"<tbody>{comp_rows}</tbody></table>"
        if comp_rows
        else '<div style="font-size:.82rem;color:var(--muted);font-style:italic;">No competitor award activity in MBIE data for this period.</div>'
    )

    # Renewal radar table
    ren_rows = ""
    for r in renewals:
        dr = r.get("days_remaining")
        colour = "var(--red)" if dr and dr <= 30 else "var(--amber)"
        ren_rows += (
            f'<tr><td style="color:{colour};font-weight:600;">{dr}d</td>'
            f"<td>{_safe(r.get('title', ''))[:55]}</td>"
            f"<td>{_safe(r.get('posting_agency', ''))[:35]}</td>"
            f"<td>{_safe(r.get('incumbent', ''))[:30]}</td>"
            f"<td>{_fmt_value(r.get('awarded_amount'))}</td></tr>"
        )
    ren_table = (
        f"<table><thead><tr><th>Days</th><th>Contract</th><th>Agency</th><th>Incumbent</th><th>Value</th></tr></thead>"
        f"<tbody>{ren_rows}</tbody></table>"
        if ren_rows
        else '<div style="font-size:.82rem;color:var(--muted);font-style:italic;">No contracts approaching renewal in MBIE data within 90 days.</div>'
    )

    week_label = run_date.strftime("Week of %-d %B %Y")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Watch Brief — {week_label}</title>
<style>{_BRIEF_CSS}</style>
</head>
<body>

<div class="brief-header">
  <div>
    <div class="brief-title-label">Procurement Watch Brief</div>
    <div class="brief-title">NZ Government Market — {week_label}</div>
  </div>
  <div class="brief-meta">
    <strong>{_safe(client_name)}</strong>
    {run_date.isoformat()}
  </div>
</div>

<div class="section">
  <div class="section-title">Top 5 Strategic Opportunities</div>
  {opp_cards}
</div>

<div class="section">
  <div class="section-title">Market Signals</div>
  {sig_rows}
</div>

<div class="section">
  <div class="section-title">Competitor Moves — Last 90 Days</div>
  {comp_table}
</div>

<div class="section">
  <div class="section-title">Renewal Radar — Next 90 Days</div>
  {ren_table}
</div>

<div class="section">
  <div class="insight">
    <span class="insight-label">Intelligence Observation</span>
    {_safe(insight)}
  </div>
</div>

<div class="doc-footer">
  <span>Procint Layer 3 &nbsp;|&nbsp; Generated {run_date.isoformat()}</span>
  <span>Data: Layer 1 (276 notices) + MBIE (27,948 awards)</span>
</div>

</body>
</html>"""


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_watch_brief(
    client_name: str,
    sectors: Optional[list[str]] = None,
    output_dir: Optional[Path] = None,
) -> Path:
    """
    Generate a weekly watch brief personalised for a client.
    Returns path to the HTML file.
    """
    logger.info("Generating watch brief for %s", client_name)
    run_date = date.today()

    opportunities = _top_opportunities(sectors)
    signals = _market_signals()
    comp_moves = _competitor_moves(client_name, sectors)
    renewals = _renewal_radar()
    insight = _generate_insight(opportunities, signals, client_name)

    html = _render_brief_html(
        client_name=client_name,
        run_date=run_date,
        opportunities=opportunities,
        signals=signals,
        competitor_moves=comp_moves,
        renewals=renewals,
        insight=insight,
    )

    if output_dir is None:
        output_dir = _artefact_dir(client_name, run_date)

    filename = f"watch_brief_{run_date.isoformat()}.html"
    out_path = output_dir / filename
    out_path.write_text(html, encoding="utf-8")
    logger.info("Watch brief written to %s", out_path)
    return out_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("client_name")
    p.add_argument("--sectors", help="Comma-separated sector tags, e.g. FM,infrastructure")
    p.add_argument("--output-dir")
    args = p.parse_args()

    sectors = [s.strip() for s in args.sectors.split(",")] if args.sectors else None
    out = generate_watch_brief(
        args.client_name,
        sectors=sectors,
        output_dir=Path(args.output_dir) if args.output_dir else None,
    )
    print(f"Generated: {out}")
