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
    "FM": "#1a5276", "infrastructure": "#7d6608", "ICT": "#6c3483",
    "advisory": "#1a6b3a", "health": "#a93226", "security": "#935116",
    "defence": "#1a2d4a", "utilities": "#5d6d00",
    "professional_services": "#1f618d", "other": "#5d6d7e",
}

# ── Data assembly ─────────────────────────────────────────────────────────────

def _top_opportunities(
    sectors: Optional[list[str]] = None,
    limit: int = 5,
    hard_sector_filter: Optional[list[str]] = None,
) -> list[dict]:
    """
    Top scored notices, re-ranked by client sector preference.

    When sectors is provided the function re-scores each notice using the
    client-aware composite (preferred sectors boosted, others demoted) and
    returns the top `limit` results — so a cybersecurity firm sees ICT/security
    at the top even if FM scored higher in the pipeline.

    When sectors is None, all sectors score equally (sector-neutral).

    hard_sector_filter: when provided (demo mode), the SQL query is restricted to
    only notices whose sector_tag is in the given list.  This prevents cross-sector
    contamination in demo watch briefs.
    """
    from scoring import compute_composite_for_client

    # Pull a wider pool so re-ranking has room to surface preferred sectors
    pool_limit = limit * 6

    if hard_sector_filter:
        placeholders = ",".join(["%s"] * len(hard_sector_filter))
        rows = db.fetchall(
            f"""
            SELECT r.notice_id, r.title, r.agency, r.source_url, r.close_date,
                   p.sector_tag, p.value_band, p.days_until_close,
                   s.composite_score, s.score_value, s.score_complexity, s.score_urgency,
                   e.summary, e.strategic_framing, e.red_flags
              FROM raw_notices r
              JOIN parsed_notices p ON p.notice_id = r.notice_id
              JOIN scored_notices s ON s.notice_id = r.notice_id
              LEFT JOIN enriched_notices e ON e.notice_id = r.notice_id
             WHERE s.composite_score >= %s
               AND p.sector_tag IN ({placeholders})
             ORDER BY s.composite_score DESC
             LIMIT %s
            """,
            (config.PRIORITY_THRESHOLD, *hard_sector_filter, pool_limit),
        )
    else:
        rows = db.fetchall(
            """
            SELECT r.notice_id, r.title, r.agency, r.source_url, r.close_date,
                   p.sector_tag, p.value_band, p.days_until_close,
                   s.composite_score, s.score_value, s.score_complexity, s.score_urgency,
                   e.summary, e.strategic_framing, e.red_flags
              FROM raw_notices r
              JOIN parsed_notices p ON p.notice_id = r.notice_id
              JOIN scored_notices s ON s.notice_id = r.notice_id
              LEFT JOIN enriched_notices e ON e.notice_id = r.notice_id
             WHERE s.composite_score >= %s
             ORDER BY s.composite_score DESC
             LIMIT %s
            """,
            (config.PRIORITY_THRESHOLD, pool_limit),
        )

    for row in rows:
        row["client_score"] = compute_composite_for_client(
            float(row.get("score_value") or 0),
            float(row.get("score_complexity") or 0),
            float(row.get("score_urgency") or 0),
            row.get("sector_tag") or "other",
            sectors,
        )

    rows.sort(key=lambda r: r["client_score"], reverse=True)
    return rows[:limit]


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
    """
    Top 3 most significant MBIE awards in the last 6 months in client-relevant
    sectors, excluding the client itself.  Ordered by awarded_amount descending
    so the highest-value competitor wins surface first.
    """
    cutoff = date.today() - timedelta(days=180)   # 6 months
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
         ORDER BY n.awarded_amount DESC NULLS LAST
         LIMIT 3
        """,
        params + [f"%{client_name.split()[0]}%"],
    )
    return [dict(r) for r in rows]


def _renewal_radar(sectors: Optional[list[str]] = None) -> list[dict]:
    """
    Contracts approaching expiry in next 12 months.
    Uses the enriched renewal_radar module (MBIE + GETS award notices with
    duration data extracted from title/description text).
    Returns rows with keys: title, posting_agency, awarded_amount,
    days_remaining, incumbent, sector_tag, window_label.
    """
    try:
        from renewal_radar import get_renewal_radar
        from datetime import date as _date
        rows = get_renewal_radar(user_sectors=sectors, days_ahead=365)
        today = _date.today()
        result = []
        for r in rows:
            expiry = r.get("expiry_date")
            days_remaining = (expiry - today).days if expiry else None
            result.append({
                "title":          r.get("title", ""),
                "posting_agency": r.get("agency_name", ""),
                "awarded_amount": r.get("contract_value"),
                "days_remaining": days_remaining,
                "incumbent":      r.get("supplier_name") or "Unknown",
                "sector_tag":     r.get("sector_tag", ""),
                "window_label":   r.get("window_label", ""),
            })
        return result
    except Exception as exc:
        logger.warning("_renewal_radar failed: %s", exc)
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
                      client_name: str,
                      competitor_moves: Optional[list[dict]] = None,
                      renewals: Optional[list[dict]] = None) -> str:
    """One synthesised market observation from Claude, grounded in actual notice data."""
    if not opportunities:
        return "Insufficient data for market observation this week."

    client_api = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    # Top opportunities with title, agency, value, sector
    opp_lines = "\n".join(
        f"  {i+1}. \"{o.get('title', '')[:70]}\" — {o.get('agency', '')} "
        f"({(o.get('sector_tag') or 'other').replace('_',' ')}) — "
        f"{_fmt_value(o.get('composite_score'))} score — closes in {o.get('days_until_close', 'TBC')} days"
        for i, o in enumerate(opportunities[:5])
    )

    # Competitor moves with supplier, agency, value
    comp_lines = "\n".join(
        f"  {cm.get('supplier_name', '')} → {cm.get('posting_agency', '')[:45]} "
        f"({(cm.get('sector_tag') or '').replace('_',' ')}) — {_fmt_value(cm.get('awarded_amount'))} — {str(cm.get('awarded_date', ''))[:10]}"
        for cm in (competitor_moves or [])[:5]
    ) or "  None recorded in MBIE data for this period."

    # Renewals with window, title, agency, incumbent, value
    def _ren_window(r):
        wl = r.get("window_label")
        if wl:
            return wl
        dr = r.get("days_remaining")
        return f"{dr}d" if dr is not None else "?"
    ren_lines = "\n".join(
        f"  {_ren_window(r)} — "
        f"\"{r.get('title', '')[:55]}\" — "
        f"{r.get('posting_agency', '')} — incumbent: {r.get('incumbent', 'unknown')} — "
        f"{_fmt_value(r.get('awarded_amount'))}"
        for r in (renewals or [])[:5]
    ) or "  No contracts with recorded durations approaching renewal in next 12 months."

    # Pattern signals
    signal_lines = "\n".join(
        f"  [{(s.get('severity') or 'medium').upper()}] {s.get('description', '')[:120]}"
        for s in signals[:3]
    ) or "  No unusual market signals detected."

    prompt = (
        f"You are a procurement intelligence analyst writing a weekly market observation for {client_name}, "
        f"a supplier competing for NZ government contracts.\n\n"
        f"Based on the data below, write exactly ONE paragraph (4-5 sentences) covering the single most "
        f"strategically significant market development this week. "
        f"Name specific agencies, suppliers, or contracts. Do not use bullet points. Return plain text only.\n\n"
        f"=== TOP OPPORTUNITIES THIS WEEK ===\n{opp_lines}\n\n"
        f"=== COMPETITOR AWARD ACTIVITY (last 6 months) ===\n{comp_lines}\n\n"
        f"=== CONTRACTS APPROACHING RENEWAL (next 12 months) ===\n{ren_lines}\n\n"
        f"=== MARKET SIGNALS ===\n{signal_lines}\n\n"
        f"Tone: direct, analytical, no filler. Written for someone who reads intelligence reports, not marketing copy."
    )

    try:
        msg = client_api.messages.create(
            model=config.CLAUDE_MODEL_L3,
            max_tokens=350,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as exc:
        logger.warning("Claude insight failed: %s", exc)
        return "Market observation unavailable this week."


# ── HTML rendering ─────────────────────────────────────────────────────────────

_BRIEF_CSS = """:root {
  --bg:#f5f6f8; --surface:#ffffff; --surf2:#f0f2f5; --border:#e2e6ea;
  --text:#2c3e50; --muted:#6c757d; --navy:#1a2d4a; --gold:#2a9d8f;
  --gold-l:#e0f4f2; --navy-l:#e8ecf3; --red:#c0392b; --red-l:#fdecea;
  --green:#27ae60; --accent:#2a9d8f;
}
*, *::before, *::after { box-sizing:border-box; margin:0; padding:0; }
body { background:var(--bg); color:var(--text);
       font-family:'Inter',system-ui,-apple-system,sans-serif;
       font-size:14px; line-height:1.6; padding:2.5rem;
       max-width:860px; margin:0 auto; -webkit-font-smoothing:antialiased; }
a { color:var(--navy); text-decoration:none; }
a:hover { color:var(--gold); }
.brief-header { display:flex; justify-content:space-between; align-items:flex-end;
  border-bottom:2px solid var(--navy); padding-bottom:1.25rem; margin-bottom:2rem; }
.brief-title-label { font-size:.65rem; font-weight:700; letter-spacing:.1em;
  text-transform:uppercase; color:var(--gold); margin-bottom:.3rem; }
.brief-title { font-size:1.35rem; font-weight:800; color:var(--navy); }
.brief-meta { font-size:.75rem; color:var(--muted); text-align:right; }
.brief-meta strong { display:block; font-size:1rem; font-weight:700; color:var(--navy); }
.section { margin-bottom:2.5rem; }
.section-title { font-size:.72rem; font-weight:700; letter-spacing:.1em; text-transform:uppercase;
  color:var(--navy); margin-bottom:1rem; padding-bottom:.4rem;
  border-bottom:2px solid var(--border); }
.opp-card { background:var(--surface); border:1px solid var(--border);
  border-radius:8px; padding:1rem 1.25rem; margin-bottom:.75rem;
  box-shadow:0 1px 3px rgba(26,45,74,.06); }
.opp-header { display:flex; justify-content:space-between; align-items:flex-start;
  margin-bottom:.4rem; gap:1rem; }
.opp-title { font-size:.9rem; font-weight:600; color:var(--navy); flex:1; }
.opp-score-chips { display:flex; gap:.35rem; flex-shrink:0; }
.opp-agency { font-size:.75rem; color:var(--muted); margin-bottom:.5rem; }
.opp-chips  { display:flex; flex-wrap:wrap; gap:.4rem; margin-bottom:.6rem; }
.chip { font-size:.65rem; font-weight:600; padding:.18rem .5rem;
  border-radius:999px; border:1px solid; }
.chip-blue  { background:var(--navy-l); color:var(--navy); border-color:#b0bcd4; }
.chip-gold  { background:var(--gold-l); color:#1a6b62; border-color:var(--gold); }
.chip-red   { background:var(--red-l);  color:var(--red); border-color:#f1a9a0; }
.chip-grey  { background:var(--surf2);  color:var(--muted); border-color:var(--border); }
.opp-summary { font-size:.82rem; color:var(--muted); line-height:1.6; }
.opp-link { font-size:.75rem; color:var(--navy); }
.signal-row { display:flex; gap:.75rem; align-items:flex-start;
  padding:.6rem .85rem; border:1px solid var(--border);
  border-radius:6px; margin-bottom:.5rem; font-size:.82rem;
  background:var(--surface); }
.signal-sev { flex-shrink:0; font-size:.65rem; font-weight:700;
  padding:.18rem .45rem; border-radius:4px; text-transform:uppercase; }
.sev-high   { background:var(--red-l);  color:var(--red); }
.sev-medium { background:var(--gold-l); color:#1a6b62; }
.sev-low    { background:var(--navy-l); color:var(--navy); }
table { width:100%; border-collapse:collapse; font-size:.82rem; margin-bottom:.5rem; }
thead tr { background:var(--navy); }
th { color:#fff; font-size:.65rem; font-weight:600; letter-spacing:.07em;
  text-transform:uppercase; padding:.45rem .7rem; text-align:left; }
td { padding:.5rem .7rem; border-bottom:1px solid var(--border); color:var(--text); }
tr:last-child td { border-bottom:none; }
tbody tr:hover td { background:var(--surf2); }
.insight { background:var(--gold-l); border:1px solid var(--gold);
  border-radius:8px; padding:1.25rem 1.5rem;
  font-size:.88rem; color:var(--text); line-height:1.7; }
.insight-label { font-size:.65rem; font-weight:700; letter-spacing:.08em;
  text-transform:uppercase; color:var(--navy); display:block; margin-bottom:.5rem; }
.doc-footer { margin-top:2.5rem; padding-top:1rem;
  border-top:1px solid var(--border); font-size:.7rem; color:var(--muted);
  display:flex; justify-content:space-between; }
.mi-empty { font-size:.82rem; color:var(--muted); font-style:italic; }

/* ── Tablet ≤768px ── */
@media (max-width:768px) {
  body { padding:1.5rem 1rem; }
  .brief-header { flex-direction:column; align-items:flex-start; gap:.5rem; }
  .brief-meta { text-align:left; }
  table { display:block; overflow-x:auto; -webkit-overflow-scrolling:touch; }
  .doc-footer { flex-direction:column; gap:.3rem; }
}

/* ── Phone ≤480px ── */
@media (max-width:480px) {
  body { padding:1rem .75rem; font-size:13px; }
  .brief-title { font-size:1.1rem; }
  .opp-card { padding:.75rem .9rem; }
  .opp-header { flex-direction:column; gap:.25rem; }
  .opp-score-chips { flex-wrap:wrap; }
  .opp-title { font-size:.86rem; }
  .signal-row { flex-wrap:wrap; }
  .signal-sev { min-height:44px; display:flex; align-items:center; }
  .insight { padding:.9rem 1rem; }
  td, th { padding:.4rem .55rem; font-size:.78rem; }
}"""


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

        # Value band label (no numeric scores shown in demo)
        vband = opp.get("value_band") or ""
        vband_labels = {
            "10m_plus": "$10M+", "2m_10m": "$2M–$10M",
            "500k_2m": "$500K–$2M", "100k_500k": "$100K–$500K",
            "under_100k": "<$100K",
        }
        vband_label = vband_labels.get(vband, "")
        vband_html = (
            f'<span class="chip" style="background:#0ea5e922;color:#0ea5e9;border-color:#0ea5e944;">'
            f'{vband_label}</span>'
        ) if vband_label else ""

        opp_cards += (
            f'<div class="opp-card">'
            f'<div class="opp-header">'
            f'<div class="opp-title">#{i} &nbsp;{_safe(opp.get("title", ""))}</div>'
            f'<div class="opp-score-chips">{vband_html}</div>'
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
        colour = "var(--red)" if dr is not None and dr <= 45 else "var(--gold)"
        window = r.get("window_label") or (f"{dr}d" if dr is not None else "TBC")
        ren_rows += (
            f'<tr><td style="color:{colour};font-weight:600;white-space:nowrap;">{_safe(window)}</td>'
            f"<td>{_safe(r.get('title', ''))[:55]}</td>"
            f"<td>{_safe(r.get('posting_agency', ''))[:35]}</td>"
            f"<td>{_safe(r.get('incumbent', ''))[:30]}</td>"
            f"<td>{_fmt_value(r.get('awarded_amount'))}</td></tr>"
        )
    ren_table = (
        f"<table><thead><tr><th>Window</th><th>Contract</th><th>Agency</th><th>Incumbent</th><th>Value</th></tr></thead>"
        f"<tbody>{ren_rows}</tbody></table>"
        if ren_rows
        else '<div style="font-size:.82rem;color:var(--muted);font-style:italic;">No contracts with recorded durations expiring in the next 12 months. Run <code>python enrich_award_durations.py --all</code> to extract durations from award notice text.</div>'
    )

    week_label = run_date.strftime("Week of %-d %B %Y")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
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
  <div class="section-title">Competitor Moves — Last 6 Months</div>
  {comp_table}
</div>

<div class="section">
  <div class="section-title">Renewal Pipeline — Next 12 Months</div>
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
    demo_sector: Optional[str] = None,
) -> Path:
    """
    Generate a weekly watch brief personalised for a client.
    Returns path to the HTML file.

    demo_sector: when set (e.g. 'FM'), hard-filters top opportunities to the
    demo sector allowlist.  Market signals are also labelled with demo context.
    """
    from generate_demo_content import DEMO_SECTOR_ALLOWLIST
    logger.info("Generating watch brief for %s (demo_sector=%s)", client_name, demo_sector)
    run_date = date.today()

    hard_filter = DEMO_SECTOR_ALLOWLIST.get(demo_sector) if demo_sector else None
    opportunities = _top_opportunities(sectors, hard_sector_filter=hard_filter)
    signals = _market_signals()
    comp_moves = _competitor_moves(client_name, sectors)
    renewals = _renewal_radar()
    insight = _generate_insight(opportunities, signals, client_name,
                               competitor_moves=comp_moves, renewals=renewals)

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
