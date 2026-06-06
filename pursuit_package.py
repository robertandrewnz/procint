"""
Layer 3 — Pursuit Intelligence Package generator.

Given a notice ID and client company name, assembles all available
Layer 1 + Layer 2 + MBIE data and calls Claude to produce a complete
pursuit intelligence package as a professional HTML document.

Data sources (all evidence-cited, no hallucination):
  - raw_notices / parsed_notices / scored_notices / enriched_notices (L1)
  - bidder_pool (L1)
  - supplier_win_history / mbie_award_notices (MBIE)
  - organisations / pattern_flags (L2)

Usage:
  python pursuit_package.py <notice_id> "<Client Name>" [--output-dir path]
"""
import argparse
import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

import anthropic

import config
import db

logger = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _slug(name: str) -> str:
    return re.sub(r"[^\w]", "_", name.lower())[:40]


def _artefact_dir(client_name: str, run_date: Optional[date] = None) -> Path:
    run_date = run_date or date.today()
    path = Path(config.ARTEFACTS_DIR) / _slug(client_name) / run_date.isoformat()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _fmt_value(v) -> str:
    if v is None:
        return "Not disclosed"
    v = float(v)
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v/1_000:.0f}K"
    return f"${v:,.0f}"


def _safe(s) -> str:
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _paras(text: str) -> str:
    """Convert double-newline-separated text into HTML paragraphs."""
    if not text:
        return ""
    return "".join(f"<p>{_safe(p)}</p>" for p in text.split("\n\n") if p.strip())



# ── Data assembly ─────────────────────────────────────────────────────────────

def _get_notice(notice_id: str) -> Optional[dict]:
    return db.fetchone(
        """
        SELECT r.notice_id, r.title, r.agency, r.source_url,
               r.close_date, r.description, r.category_raw, r.estimated_value,
               p.sector_tag, p.value_band, p.days_until_close,
               p.geographic_scope, p.evaluation_criteria, p.contract_duration,
               p.estimated_value_min, p.estimated_value_max,
               s.composite_score, s.score_reasoning,
               e.summary, e.evaluation_weighting, e.red_flags, e.strategic_framing
          FROM raw_notices r
          JOIN parsed_notices p ON p.notice_id = r.notice_id
          JOIN scored_notices s ON s.notice_id = r.notice_id
          LEFT JOIN enriched_notices e ON e.notice_id = r.notice_id
         WHERE r.notice_id = %s
        """,
        (notice_id,),
    )


def _get_competitive_landscape(agency: str, sector: str, limit: int = None) -> list[dict]:
    """Who has historically won contracts from this agency in this sector."""
    limit = limit or config.PURSUIT_COMPETITOR_LIMIT
    agency_word = agency.split()[0] if agency else ""
    return db.fetchall(
        """
        SELECT s.business_name AS supplier_name,
               COUNT(DISTINCT n.rfx_id) AS wins,
               SUM(n.awarded_amount) AS total_value,
               AVG(n.awarded_amount) AS avg_value,
               MAX(n.awarded_date) AS last_win,
               MIN(n.awarded_date) AS first_win,
               COUNT(DISTINCT n.rfx_id) FILTER (
                   WHERE LOWER(n.posting_agency) LIKE LOWER(%s)
               ) AS agency_wins
          FROM mbie_award_notices n
          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
          JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
         WHERE n.is_awarded
           AND c.sector_tag = %s
           AND s.business_name NOT IN ('', 'NULL')
         GROUP BY s.business_name
        HAVING COUNT(DISTINCT n.rfx_id) >= 1
         ORDER BY agency_wins DESC, wins DESC
         LIMIT %s
        """,
        (f"%{agency_word}%", sector, limit),
    )


def _get_client_history(client_name: str, sector: str, agency: str) -> dict:
    """Client's MBIE track record in this sector and with this agency."""
    agency_word = agency.split()[0] if agency else ""

    total = db.fetchone(
        """
        SELECT COUNT(DISTINCT n.rfx_id) AS wins,
               SUM(n.awarded_amount) AS total_value,
               MAX(n.awarded_date) AS last_win,
               MIN(n.awarded_date) AS first_win
          FROM mbie_award_notices n
          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
          JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
         WHERE n.is_awarded
           AND LOWER(s.business_name) LIKE LOWER(%s)
           AND c.sector_tag = %s
        """,
        (f"%{client_name.split()[0]}%", sector),
    )

    agency_wins = db.fetchone(
        """
        SELECT COUNT(DISTINCT n.rfx_id) AS wins,
               SUM(n.awarded_amount) AS total_value
          FROM mbie_award_notices n
          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
          JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
         WHERE n.is_awarded
           AND LOWER(s.business_name) LIKE LOWER(%s)
           AND LOWER(n.posting_agency) LIKE LOWER(%s)
        """,
        (f"%{client_name.split()[0]}%", f"%{agency_word}%"),
    )

    # Sectors client has won in (all time)
    sectors = db.fetchall(
        """
        SELECT c.sector_tag, COUNT(*) AS wins
          FROM mbie_award_notices n
          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
          JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
         WHERE n.is_awarded
           AND LOWER(s.business_name) LIKE LOWER(%s)
           AND c.sector_tag IS NOT NULL
         GROUP BY c.sector_tag
         ORDER BY wins DESC
         LIMIT 5
        """,
        (f"%{client_name.split()[0]}%",),
    )

    return {
        "sector_wins": int(total["wins"]) if total else 0,
        "sector_total_value": float(total["total_value"] or 0) if total else 0,
        "sector_last_win": total["last_win"] if total else None,
        "sector_first_win": total["first_win"] if total else None,
        "agency_wins": int(agency_wins["wins"]) if agency_wins else 0,
        "agency_total_value": float(agency_wins["total_value"] or 0) if agency_wins else 0,
        "sectors_won": [r["sector_tag"] for r in sectors],
    }


def _detect_incumbent(agency: str, sector: str) -> Optional[dict]:
    """Who most recently won a contract from this agency in this sector."""
    agency_word = agency.split()[0] if agency else ""
    return db.fetchone(
        """
        SELECT s.business_name, n.awarded_date, n.awarded_amount, n.title
          FROM mbie_award_notices n
          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
          JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
         WHERE n.is_awarded
           AND LOWER(n.posting_agency) LIKE LOWER(%s)
           AND c.sector_tag = %s
           AND s.business_name NOT IN ('', 'NULL')
         ORDER BY n.awarded_date DESC NULLS LAST
         LIMIT 1
        """,
        (f"%{agency_word}%", sector),
    )


def _get_agency_stats(agency: str, sector: str) -> dict:
    """Agency procurement behaviour stats from MBIE."""
    agency_word = agency.split()[0] if agency else ""

    stats = db.fetchone(
        """
        SELECT COUNT(DISTINCT n.rfx_id) AS total_awards,
               SUM(n.awarded_amount) AS total_value,
               AVG(n.awarded_amount) AS avg_value,
               COUNT(DISTINCT s.business_name) AS unique_suppliers,
               MIN(n.awarded_date) AS earliest_award,
               MAX(n.awarded_date) AS latest_award
          FROM mbie_award_notices n
          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
         WHERE n.is_awarded
           AND LOWER(n.posting_agency) LIKE LOWER(%s)
        """,
        (f"%{agency_word}%",),
    )

    sector_stats = db.fetchone(
        """
        SELECT COUNT(DISTINCT n.rfx_id) AS sector_awards
          FROM mbie_award_notices n
          JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
         WHERE n.is_awarded
           AND LOWER(n.posting_agency) LIKE LOWER(%s)
           AND c.sector_tag = %s
        """,
        (f"%{agency_word}%", sector),
    )

    # Top sectors for this agency
    top_sectors = db.fetchall(
        """
        SELECT c.sector_tag, COUNT(*) AS awards
          FROM mbie_award_notices n
          JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
         WHERE n.is_awarded
           AND LOWER(n.posting_agency) LIKE LOWER(%s)
           AND c.sector_tag IS NOT NULL
         GROUP BY c.sector_tag
         ORDER BY awards DESC
         LIMIT 4
        """,
        (f"%{agency_word}%",),
    )

    return {
        "total_awards": int(stats["total_awards"]) if stats else 0,
        "total_value": float(stats["total_value"] or 0) if stats else 0,
        "avg_value": float(stats["avg_value"] or 0) if stats else 0,
        "unique_suppliers": int(stats["unique_suppliers"]) if stats else 0,
        "sector_awards": int(sector_stats["sector_awards"]) if sector_stats else 0,
        "earliest_award": stats["earliest_award"] if stats else None,
        "latest_award": stats["latest_award"] if stats else None,
        "top_sectors": [(r["sector_tag"], r["awards"]) for r in top_sectors],
    }


def _get_relevant_flags(agency: str, sector: str) -> list[dict]:
    """Pattern flags relevant to this agency/sector."""
    return db.fetchall(
        """
        SELECT flag_type, description, severity, detected_at
          FROM pattern_flags
         WHERE (expires_at IS NULL OR expires_at >= CURRENT_DATE)
           AND (
               sector_tag = %s
               OR description ILIKE %s
           )
         ORDER BY severity DESC, detected_at DESC
         LIMIT 5
        """,
        (sector, f"%{agency.split()[0]}%"),
    )


def _get_notice_bidders(notice_id: str) -> list[dict]:
    """Bidders from bidder_pool with any available context."""
    return db.fetchall(
        """
        SELECT firm_name, strategic_importance, intelligence_maturity,
               relevance_score, match_type, reasoning, company_context
          FROM bidder_pool
         WHERE notice_id = %s
         ORDER BY relevance_score DESC NULLS LAST
         LIMIT 10
        """,
        (notice_id,),
    )


# ── MBIE data count (for citation) ───────────────────────────────────────────

def _mbie_citation(sector: str, agency: str) -> str:
    agency_word = agency.split()[0] if agency else ""
    r = db.fetchone(
        """
        SELECT COUNT(DISTINCT n.rfx_id) AS n
          FROM mbie_award_notices n
          JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
         WHERE n.is_awarded
           AND (c.sector_tag = %s OR LOWER(n.posting_agency) LIKE LOWER(%s))
        """,
        (sector, f"%{agency_word}%"),
    )
    count = r["n"] if r else 0
    return f"based on {count:,} MBIE award notices (2014–2025)"


# ── Claude synthesis ──────────────────────────────────────────────────────────

_PURSUIT_SYSTEM = """You are a senior procurement strategy adviser at a boutique advisory firm in New Zealand.
You are preparing an intelligence package for a client considering bidding on a government contract.
Your analysis must be grounded strictly in the data provided — do not invent firms, award values, or procurement history not present in the context.
Respond ONLY with a valid JSON object, no preamble, no markdown fences."""

_PURSUIT_PROMPT = """Prepare a pursuit intelligence package for:

CLIENT: {client_name}
NOTICE: {title}
AGENCY: {agency}
SECTOR: {sector}
VALUE: {value_band}
CLOSE DATE: {close_date} ({days_until_close} days)
GETS URL: {source_url}

=== OPPORTUNITY CONTEXT ===
Description: {description}
Evaluation criteria (stated): {evaluation_criteria}
Contract duration: {contract_duration}
Geographic scope: {geographic_scope}

=== AI ENRICHMENT (Layer 1) ===
Summary: {enrichment_summary}
Evaluation weighting (inferred): {evaluation_weighting}
Red flags identified: {red_flags}
Strategic framing: {strategic_framing}

=== CLIENT HISTORY (MBIE data) ===
Client wins in {sector} sector (all time): {client_sector_wins} contracts, {client_sector_value} total
Client wins with {agency} specifically: {client_agency_wins} contracts
Client's sectors of proven capability: {client_sectors}
Note: {client_data_note}

=== COMPETITIVE LANDSCAPE (MBIE: {mbie_citation}) ===
Historical winners of similar contracts ({sector} sector, same agency or similar agencies):
{competitors_text}

=== INCUMBENT INTELLIGENCE ===
Most recent winner from {agency} in {sector}: {incumbent_text}

=== AGENCY PROFILE (MBIE data) ===
Total MBIE-recorded awards: {agency_total_awards} contracts worth {agency_total_value}
Average contract value: {agency_avg_value}
Unique suppliers engaged: {agency_unique_suppliers}
Awards in {sector} sector specifically: {agency_sector_awards}
Top procurement sectors: {agency_top_sectors}

=== PATTERN FLAGS ===
{flags_text}

Return a JSON object with EXACTLY these keys. Be specific and cite data where available.

"executive_summary": Two paragraphs. First: what this opportunity is and its strategic significance. Second: why this matters specifically for {client_name} given their track record.

"strategic_fit_score": Integer 1-10. Base this on: client's sector capability, prior agency relationship, competitive positioning.

"win_probability_pct": Integer 0-100. Evidence-based estimate. Account for: client history with this agency, incumbent strength, field size, days to close, known red flags. Be honest — most competitive fields have 20-40% win probability for a strong bidder.

"win_probability_rationale": One paragraph explaining the probability estimate with specific references to the data.

"go_nogo": Exactly one of "GO", "CONDITIONAL GO", or "NO GO"

"go_nogo_rationale": Two sentences. Decisive recommendation with primary reason and key condition if conditional.

"competitive_assessment": Two paragraphs. Who the main threats are, their strengths, and where gaps exist for {client_name}.

"incumbent_assessment": One paragraph. How entrenched the incumbent is and what it would take to displace them.

"agency_insights": One to two paragraphs. What this buyer values, how they procure, and what the data reveals about their evaluation behaviour.

"positioning_recommendations": Array of 3-5 objects, each with "title" (short label) and "detail" (2-3 sentences of specific, actionable advice tailored to {client_name}).

"risk_register": Array of exactly 5 objects, each with "risk" (label), "likelihood" (High/Medium/Low), "impact" (High/Medium/Low), "mitigation" (1-2 sentences).

"recommended_actions": Array of 4-6 objects, each with "action" (imperative sentence), "timeframe" (e.g. "Today", "Within 48 hours", "Week 1", "Before close"), "priority" (Critical/High/Medium)."""


def _call_claude(context: dict) -> Optional[dict]:
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    # Format competitor text
    comps = context.get("competitors", [])
    if comps:
        comp_lines = []
        for i, c in enumerate(comps[:8], 1):
            lw = str(c.get("last_win", ""))[:10] if c.get("last_win") else "unknown"
            av = _fmt_value(c.get("avg_value"))
            comp_lines.append(
                f"{i}. {c['supplier_name']}: {c['wins']} sector wins total, "
                f"{c.get('agency_wins', 0)} with this agency, avg {av}, last win {lw}"
            )
        competitors_text = "\n".join(comp_lines)
    else:
        competitors_text = "No MBIE records found for this agency/sector combination. Market data is limited."

    # Incumbent
    inc = context.get("incumbent")
    if inc:
        inc_date = str(inc.get("awarded_date", ""))[:10]
        incumbent_text = (
            f"{inc['business_name']} (most recent award: {_fmt_value(inc.get('awarded_amount'))}, "
            f"{inc_date}): {inc.get('title', '')[:80]}"
        )
    else:
        incumbent_text = "No incumbent identified in MBIE data for this agency/sector."

    # Client history note
    ch = context.get("client_history", {})
    if ch.get("sector_wins", 0) == 0:
        client_data_note = "Client not found in MBIE dataset — may be private sector focused or using a different trading name."
    else:
        client_data_note = f"Client has {ch['sector_wins']} confirmed sector wins in MBIE data since {str(ch.get('sector_first_win', ''))[:4]}."

    # Flags
    flags = context.get("flags", [])
    flags_text = "\n".join(
        f"- [{f['severity'].upper()}] {f['description'][:120]}" for f in flags
    ) or "No active intelligence flags for this agency/sector."

    # Agency stats
    ag = context.get("agency_stats", {})
    n = context.get("notice", {})
    e = context.get("enrichment", {})

    prompt = _PURSUIT_PROMPT.format(
        client_name=context["client_name"],
        title=n.get("title", "Unknown"),
        agency=n.get("agency", "Unknown"),
        sector=n.get("sector_tag", "other"),
        value_band=n.get("value_band", "unknown"),
        close_date=str(n.get("close_date", "Unknown")),
        days_until_close=n.get("days_until_close") or "Unknown",
        source_url=n.get("source_url", ""),
        description=(n.get("description") or "Not provided in notice")[:1500],
        evaluation_criteria=n.get("evaluation_criteria") or "Not stated",
        contract_duration=n.get("contract_duration") or "Not stated",
        geographic_scope=n.get("geographic_scope") or "Not stated",
        enrichment_summary=e.get("summary") or "Enrichment not available for this notice.",
        evaluation_weighting=e.get("evaluation_weighting") or "Not available.",
        red_flags=e.get("red_flags") or "None identified.",
        strategic_framing=e.get("strategic_framing") or "Not available.",
        client_sector_wins=ch.get("sector_wins", 0),
        client_sector_value=_fmt_value(ch.get("sector_total_value", 0)),
        client_agency_wins=ch.get("agency_wins", 0),
        client_sectors=", ".join(ch.get("sectors_won", [])) or "None found in MBIE data",
        client_data_note=client_data_note,
        mbie_citation=context.get("mbie_citation", "MBIE data"),
        competitors_text=competitors_text,
        incumbent_text=incumbent_text,
        agency_total_awards=ag.get("total_awards", 0),
        agency_total_value=_fmt_value(ag.get("total_value", 0)),
        agency_avg_value=_fmt_value(ag.get("avg_value", 0)),
        agency_unique_suppliers=ag.get("unique_suppliers", 0),
        agency_sector_awards=ag.get("sector_awards", 0),
        agency_top_sectors=", ".join(f"{s[0]} ({s[1]})" for s in ag.get("top_sectors", [])) or "Insufficient data",
        flags_text=flags_text,
    )

    try:
        msg = client.messages.create(
            model=config.CLAUDE_MODEL_L3,
            max_tokens=config.CLAUDE_MAX_TOKENS_L3,
            system=_PURSUIT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        return json.loads(raw)
    except Exception as exc:
        logger.error("Claude synthesis failed: %s", exc)
        return None


# ── HTML rendering ─────────────────────────────────────────────────────────────

_CSS = """
:root {
  --bg:#f5f6f8; --surface:#ffffff; --surf2:#f0f2f5; --border:#e2e6ea;
  --text:#2c3e50; --muted:#6c757d; --navy:#1a2d4a; --gold:#c9a84c;
  --gold-l:#f7eedb; --navy-l:#e8ecf3; --red:#c0392b; --red-l:#fdecea;
  --green:#27ae60; --accent:#c9a84c;
  --font:'Inter',system-ui,-apple-system,sans-serif;
}
*, *::before, *::after { box-sizing:border-box; margin:0; padding:0; }
body { background:var(--bg); color:var(--text); font-family:var(--font);
       font-size:14px; line-height:1.6; display:flex; min-height:100vh;
       -webkit-font-smoothing:antialiased; }
a { color:var(--navy); text-decoration:none; }
a:hover { color:var(--gold); }
.sidebar { width:220px; flex-shrink:0; background:var(--navy);
           position:sticky; top:0; height:100vh; overflow-y:auto;
           padding:1.75rem 1.25rem; }
.sidebar-brand { font-size:.82rem; font-weight:800; color:#fff; letter-spacing:-.01em; margin-bottom:.2rem; }
.sidebar-brand .by { font-weight:400; color:rgba(255,255,255,.45); }
.sidebar-sub { font-size:.62rem; font-weight:700; letter-spacing:.1em; text-transform:uppercase; color:var(--gold); margin-bottom:1.5rem; }
.sidebar-label { font-size:.6rem; font-weight:700; letter-spacing:.09em; text-transform:uppercase; color:rgba(255,255,255,.4); margin:1.2rem 0 .4rem; }
.sidebar nav a { display:block; font-size:.8rem; color:rgba(255,255,255,.7); text-decoration:none; padding:.3rem .5rem; border-radius:4px; margin-bottom:.15rem; transition:background .12s; }
.sidebar nav a:hover { background:rgba(255,255,255,.1); color:#fff; }
.main { flex:1; padding:2.5rem 3rem; max-width:900px; }
.cover { margin-bottom:2.5rem; padding-bottom:1.75rem; border-bottom:2px solid var(--navy); }
.cover-label { font-size:.65rem; font-weight:700; letter-spacing:.1em; text-transform:uppercase; color:var(--gold); margin-bottom:.4rem; }
.cover-title { font-size:1.55rem; font-weight:800; color:var(--navy); line-height:1.3; margin-bottom:.6rem; }
.cover-agency { font-size:.95rem; color:var(--muted); margin-bottom:1.1rem; }
.cover-meta { display:flex; flex-wrap:wrap; gap:.65rem; margin-bottom:1.1rem; }
.meta-chip { font-size:.7rem; padding:.22rem .6rem; border-radius:999px; border:1px solid; font-weight:600; }
.chip-blue  { background:var(--navy-l); color:var(--navy); border-color:#b0bcd4; }
.chip-gold  { background:var(--gold-l); color:#7a5c00; border-color:var(--gold); }
.chip-red   { background:var(--red-l); color:var(--red); border-color:#f1a9a0; }
.chip-green { background:#eafaf1; color:var(--green); border-color:#a9dfbf; }
.chip-grey  { background:var(--surf2); color:var(--muted); border-color:var(--border); }
.cover-client { font-size:.8rem; color:var(--muted); }
.cover-client strong { color:var(--navy); }
.verdict { display:flex; align-items:center; gap:1.5rem; padding:1.25rem 1.5rem; border-radius:8px; border:1px solid; margin-bottom:2rem; }
.verdict.go   { background:#eafaf1; border-color:#a9dfbf; }
.verdict.cond { background:var(--gold-l); border-color:var(--gold); }
.verdict.nogo { background:var(--red-l); border-color:#f1a9a0; }
.verdict-badge { font-size:1.1rem; font-weight:800; letter-spacing:.04em; flex-shrink:0; }
.verdict.go   .verdict-badge { color:var(--green); }
.verdict.cond .verdict-badge { color:#7a5c00; }
.verdict.nogo .verdict-badge { color:var(--red); }
.verdict-text { font-size:.85rem; color:var(--text); line-height:1.55; }
.prob-ring { flex-shrink:0; text-align:center; }
.prob-pct  { font-size:1.55rem; font-weight:800; color:var(--navy); line-height:1; }
.prob-label { font-size:.6rem; text-transform:uppercase; letter-spacing:.07em; color:var(--muted); }
.section { margin-bottom:3rem; scroll-margin-top:2rem; }
.section-number { font-size:.65rem; font-weight:700; letter-spacing:.1em; text-transform:uppercase; color:var(--gold); margin-bottom:.25rem; }
.section-title  { font-size:1.05rem; font-weight:700; color:var(--navy); margin-bottom:1rem; padding-bottom:.5rem; border-bottom:2px solid var(--border); }
.prose p { color:var(--text); margin-bottom:.85rem; line-height:1.75; font-size:.88rem; }
.pos-card { background:var(--surf2); border:1px solid var(--border); border-radius:8px; padding:1rem 1.25rem; margin-bottom:.85rem; }
.pos-card-title  { font-size:.82rem; font-weight:700; color:var(--navy); margin-bottom:.3rem; }
.pos-card-detail { font-size:.82rem; color:var(--text); line-height:1.6; }
table { width:100%; border-collapse:collapse; margin-bottom:1rem; font-size:.83rem; }
thead tr { background:var(--navy); }
th { color:#fff; padding:.55rem .75rem; text-align:left; font-size:.66rem; font-weight:600; letter-spacing:.07em; text-transform:uppercase; }
td { padding:.55rem .75rem; border-bottom:1px solid var(--border); color:var(--text); vertical-align:top; }
tr:last-child td { border-bottom:none; }
tbody tr:hover td { background:var(--surf2); }
.risk-high   { color:var(--red);   font-weight:600; }
.risk-medium { color:#7a5c00;      font-weight:600; }
.risk-low    { color:var(--green); font-weight:600; }
.action-item { display:flex; align-items:flex-start; gap:.85rem; padding:.75rem 1rem; border:1px solid var(--border); border-radius:6px; margin-bottom:.5rem; background:var(--surface); }
.action-priority { flex-shrink:0; font-size:.65rem; font-weight:700; letter-spacing:.06em; text-transform:uppercase; padding:.2rem .5rem; border-radius:4px; }
.pri-critical { background:var(--red-l); color:var(--red); }
.pri-high     { background:var(--gold-l); color:#7a5c00; }
.pri-medium   { background:var(--navy-l); color:var(--navy); }
.action-body  { flex:1; }
.action-text  { font-size:.83rem; color:var(--text); margin-bottom:.2rem; }
.action-time  { font-size:.72rem; color:var(--muted); }
.citation { font-size:.7rem; color:var(--muted); font-style:italic; margin-top:.5rem; padding:.4rem .75rem; background:var(--surf2); border-radius:4px; border-left:2px solid var(--gold); }
.doc-footer { margin-top:3rem; padding-top:1.5rem; border-top:1px solid var(--border); font-size:.7rem; color:var(--muted); display:flex; justify-content:space-between; align-items:center; }

/* ── Tablet ≤768px ── */
@media (max-width:768px) {
  body { display:block; }
  .sidebar { width:100%; height:auto; position:static; padding:1rem 1.25rem;
             display:flex; flex-wrap:wrap; align-items:center; gap:.75rem 1.5rem; }
  .sidebar-label { display:none; }
  .sidebar nav { display:flex; flex-wrap:wrap; gap:.3rem; }
  .sidebar nav a { padding:.35rem .65rem; font-size:.78rem; }
  .main { padding:1.5rem 1.5rem; max-width:100%; }
  .verdict { flex-wrap:wrap; gap:1rem; }
  table { display:block; overflow-x:auto; -webkit-overflow-scrolling:touch; }
  .doc-footer { flex-direction:column; gap:.3rem; }
}

/* ── Phone ≤480px ── */
@media (max-width:480px) {
  .sidebar { padding:.75rem 1rem; gap:.5rem 1rem; }
  .sidebar-brand { font-size:.78rem; }
  .sidebar nav a { min-height:44px; display:flex; align-items:center; padding:.3rem .75rem; }
  .main { padding:1rem .85rem; }
  .cover-title { font-size:1.2rem; }
  .cover-agency { font-size:.88rem; }
  .cover-meta { gap:.4rem; }
  .verdict { padding:.9rem 1rem; }
  .prob-pct { font-size:1.25rem; }
  .section-title { font-size:.95rem; }
  .pos-card { padding:.75rem .9rem; }
  .action-item { padding:.65rem .75rem; gap:.65rem; }
  .action-priority { min-height:44px; display:flex; align-items:center; }
  td, th { padding:.45rem .55rem; font-size:.78rem; }
}
"""


def _verdict_class(rec: str) -> str:
    if "NO" in rec.upper():
        return "nogo"
    if "CONDITIONAL" in rec.upper():
        return "cond"
    return "go"


def _risk_class(level: str) -> str:
    l = (level or "").lower()
    if "high" in l:
        return "risk-high"
    if "low" in l:
        return "risk-low"
    return "risk-medium"


def _action_class(priority: str) -> str:
    p = (priority or "").lower()
    if "critical" in p:
        return "pri-critical"
    if "high" in p:
        return "pri-high"
    return "pri-medium"


def _render_html(
    notice: dict,
    analysis: dict,
    context: dict,
    client_name: str,
    is_demo: bool = False,
    demo_watermark: str = "",
) -> str:
    n = notice
    a = analysis
    run_date = date.today().isoformat()

    sector = n.get("sector_tag", "other").replace("_", " ").upper()
    value_band = n.get("value_band", "unknown")
    dtc = n.get("days_until_close")
    close_str = str(n.get("close_date") or "Unknown")

    if dtc is not None and dtc <= 7:
        urgency_chip = "chip-red"
        urgency_label = f"URGENT — {dtc}d to close"
    elif dtc is not None and dtc <= 21:
        urgency_chip = "chip-amber"
        urgency_label = f"{dtc} days to close"
    else:
        urgency_chip = "chip-blue"
        urgency_label = f"{dtc} days to close" if dtc else "Close date TBC"

    score_val = float(n.get("composite_score") or 0)
    verdict = a.get("go_nogo", "GO")
    prob = a.get("win_probability_pct", 0)
    vc = _verdict_class(verdict)

    # Competitive table rows
    competitors = context.get("competitors", [])
    comp_rows = ""
    for c in competitors:
        lw = str(c.get("last_win", ""))[:7] if c.get("last_win") else "—"
        comp_rows += (
            f"<tr><td>{_safe(c['supplier_name'])}</td>"
            f"<td>{c.get('agency_wins', 0)}</td>"
            f"<td>{c.get('wins', 0)}</td>"
            f"<td>{_fmt_value(c.get('avg_value'))}</td>"
            f"<td>{lw}</td></tr>"
        )

    # Positioning cards
    pos_cards = ""
    for rec in (a.get("positioning_recommendations") or []):
        if isinstance(rec, dict):
            pos_cards += (
                f'<div class="pos-card">'
                f'<div class="pos-card-title">{_safe(rec.get("title", ""))}</div>'
                f'<div class="pos-card-detail">{_safe(rec.get("detail", ""))}</div>'
                f'</div>'
            )

    # Risk register rows
    risk_rows = ""
    for risk in (a.get("risk_register") or []):
        if isinstance(risk, dict):
            lh = _risk_class(risk.get("likelihood", ""))
            li = _risk_class(risk.get("impact", ""))
            risk_rows += (
                f"<tr><td>{_safe(risk.get('risk', ''))}</td>"
                f'<td class="{lh}">{_safe(risk.get("likelihood", ""))}</td>'
                f'<td class="{li}">{_safe(risk.get("impact", ""))}</td>'
                f"<td>{_safe(risk.get('mitigation', ''))}</td></tr>"
            )

    # Action items
    action_html = ""
    for act in (a.get("recommended_actions") or []):
        if isinstance(act, dict):
            pc = _action_class(act.get("priority", ""))
            action_html += (
                f'<div class="action-item">'
                f'<span class="action-priority {pc}">{_safe(act.get("priority", ""))}</span>'
                f'<div class="action-body">'
                f'<div class="action-text">{_safe(act.get("action", ""))}</div>'
                f'<div class="action-time">&#128344; {_safe(act.get("timeframe", ""))}</div>'
                f'</div></div>'
            )

    incumbent = context.get("incumbent")
    inc_text = ""
    if incumbent:
        inc_text = (
            f"<p>Most recent award: <strong>{_safe(incumbent.get('business_name', ''))}</strong>"
            f" ({_fmt_value(incumbent.get('awarded_amount'))},"
            f" {str(incumbent.get('awarded_date', ''))[:10]})</p>"
        )

    demo_banner = ""
    if is_demo:
        demo_banner = (
            f'<div style="background:#facc1518;border:2px solid var(--amber);'
            f'border-radius:8px;padding:1rem 1.5rem;margin-bottom:2rem;'
            f'font-size:.82rem;color:var(--amber);">'
            f'<strong>SAMPLE DOCUMENT</strong> — {_safe(demo_watermark)}'
            f'</div>'
        )

    ch = context.get("client_history", {})
    ag = context.get("agency_stats", {})
    citation = context.get("mbie_citation", "MBIE data")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pursuit Package — {_safe(n.get('title', '')[:60])}</title>
<style>{_CSS}</style>
</head>
<body>

<div class="sidebar">
  <div class="sidebar-brand">Groundwork <span class="by">by BidEdge</span></div>
  <div class="sidebar-sub">Procurement Intelligence</div>
  <div class="sidebar-label">Sections</div>
  <nav>
    <a href="#exec">01 Executive Summary</a>
    <a href="#assessment">02 Opportunity Assessment</a>
    <a href="#competitive">03 Competitive Landscape</a>
    <a href="#agency">04 Agency Profile</a>
    <a href="#positioning">05 Positioning Brief</a>
    <a href="#risks">06 Risk Register</a>
    <a href="#actions">07 Recommended Actions</a>
  </nav>
  <div class="sidebar-label" style="margin-top:2rem;">Data</div>
  <div style="font-size:.7rem;color:var(--muted);line-height:1.6;">
    NZ GETS notices<br>
    MBIE awards: 27,948<br>
    Supplier profiles: 5,830
  </div>
</div>

<div class="main">

  {demo_banner}

  <!-- Cover -->
  <div class="cover">
    <div class="cover-label">Groundwork by BidEdge &mdash; Pursuit Intelligence</div>
    <div class="cover-title">{_safe(n.get('title', 'Unknown Opportunity'))}</div>
    <div class="cover-agency">{_safe(n.get('agency', ''))}</div>
    <div class="cover-meta">
      <span class="meta-chip chip-blue">{sector}</span>
      <span class="meta-chip {urgency_chip}">{_safe(urgency_label)}</span>
      <span class="meta-chip chip-grey">Close: {_safe(close_str)}</span>
      <span class="meta-chip chip-grey">Score: {score_val:.1f}/10</span>
    </div>
    <div class="cover-client">
      Prepared for: <strong>{_safe(client_name)}</strong> &nbsp;|&nbsp;
      Generated: {run_date} &nbsp;|&nbsp;
      <a href="{_safe(n.get('source_url', '#'))}" style="color:var(--accent);" target="_blank">View on GETS &#8599;</a>
    </div>
  </div>

  <!-- Verdict banner -->
  <div class="verdict {vc}">
    <div class="prob-ring">
      <div class="prob-pct">{prob}%</div>
      <div class="prob-label">Est. win prob.</div>
    </div>
    <div style="width:1px;height:48px;background:var(--border);"></div>
    <div class="verdict-badge">{_safe(verdict)}</div>
    <div class="verdict-text">{_safe(a.get('go_nogo_rationale', ''))}</div>
  </div>

  <!-- 01 Executive Summary -->
  <div class="section" id="exec">
    <div class="section-number">01</div>
    <div class="section-title">Executive Summary</div>
    <div class="prose">{_paras(a.get('executive_summary') or '')}</div>
  </div>

  <!-- 02 Opportunity Assessment -->
  <div class="section" id="assessment">
    <div class="section-number">02</div>
    <div class="section-title">Opportunity Assessment</div>
    <div class="prose">{_paras(a.get('win_probability_rationale') or '')}</div>

    <table style="margin-top:.75rem;">
      <thead><tr>
        <th>Dimension</th><th>Data</th>
      </tr></thead>
      <tbody>
        <tr><td>Client wins in this sector (MBIE)</td><td>{ch.get('sector_wins', 0)} contracts | {_fmt_value(ch.get('sector_total_value', 0))}</td></tr>
        <tr><td>Client wins with this agency</td><td>{ch.get('agency_wins', 0)} contracts</td></tr>
        <tr><td>Strategic fit score</td><td>{a.get('strategic_fit_score', 'N/A')} / 10</td></tr>
        <tr><td>Composite priority score (Layer 1)</td><td>{score_val:.2f} / 10</td></tr>
        <tr><td>Days until close</td><td>{dtc if dtc is not None else 'Unknown'}</td></tr>
      </tbody>
    </table>
    <div class="citation">{_safe(citation)}</div>
    {_paras(a.get('strategic_fit_assessment') or '')}
  </div>

  <!-- 03 Competitive Landscape -->
  <div class="section" id="competitive">
    <div class="section-number">03</div>
    <div class="section-title">Competitive Landscape</div>
    <div class="prose">{_paras(a.get('competitive_assessment') or '')}</div>

    {inc_text}

    <div class="prose">{_paras(a.get('incumbent_assessment') or '')}</div>

    {f'''<table>
      <thead><tr>
        <th>Supplier</th>
        <th>This agency wins</th>
        <th>Sector wins total</th>
        <th>Avg contract value</th>
        <th>Last win</th>
      </tr></thead>
      <tbody>{comp_rows}</tbody>
    </table>
    <div class="citation">{_safe(citation)}</div>''' if comp_rows else '<p style="color:var(--muted);font-size:.82rem;font-style:italic;">Insufficient MBIE data for this agency/sector combination. Field is unknown.</p>'}
  </div>

  <!-- 04 Agency Profile -->
  <div class="section" id="agency">
    <div class="section-number">04</div>
    <div class="section-title">Agency Profile — {_safe(n.get('agency', ''))}</div>
    <div class="prose">{_paras(a.get('agency_insights') or '')}</div>

    <table>
      <thead><tr><th>Metric</th><th>Value</th></tr></thead>
      <tbody>
        <tr><td>Total MBIE-recorded awards</td><td>{ag.get('total_awards', 0)} contracts</td></tr>
        <tr><td>Total awarded value</td><td>{_fmt_value(ag.get('total_value', 0))}</td></tr>
        <tr><td>Average contract value</td><td>{_fmt_value(ag.get('avg_value', 0))}</td></tr>
        <tr><td>Unique suppliers engaged</td><td>{ag.get('unique_suppliers', 0)}</td></tr>
        <tr><td>Awards in {_safe(n.get('sector_tag', ''))} sector</td><td>{ag.get('sector_awards', 0)}</td></tr>
        <tr><td>Top procurement sectors</td><td>{_safe(', '.join(f"{s[0]} ({s[1]})" for s in ag.get('top_sectors', [])))}</td></tr>
      </tbody>
    </table>
    <div class="citation">{_safe(citation)}</div>

    {"".join(f'<p style="font-size:.8rem;color:var(--muted);"><strong>Evaluation weighting (Layer 1 AI inference):</strong> {_safe(n.get("evaluation_weighting", ""))}</p>' if n.get("evaluation_weighting") else '')}
  </div>

  <!-- 05 Positioning Brief -->
  <div class="section" id="positioning">
    <div class="section-number">05</div>
    <div class="section-title">Positioning Brief for {_safe(client_name)}</div>
    {pos_cards}
  </div>

  <!-- 06 Risk Register -->
  <div class="section" id="risks">
    <div class="section-number">06</div>
    <div class="section-title">Risk Register</div>
    <table>
      <thead><tr>
        <th style="width:35%">Risk</th>
        <th>Likelihood</th>
        <th>Impact</th>
        <th>Mitigation</th>
      </tr></thead>
      <tbody>{risk_rows}</tbody>
    </table>
    {f'<div style="margin-top:.75rem;font-size:.8rem;"><strong>Red flags (Layer 1 AI):</strong> {_safe(n.get("red_flags", "None identified."))}</div>' if n.get("red_flags") else ''}
  </div>

  <!-- 07 Recommended Actions -->
  <div class="section" id="actions">
    <div class="section-number">07</div>
    <div class="section-title">Recommended Actions</div>
    {action_html}
  </div>

  <div class="doc-footer">
    <span>&copy; BidEdge Ltd &middot; Groundwork &nbsp;|&nbsp; Generated {run_date} &nbsp;|&nbsp; {_safe(citation)}</span>
    <span><a href="{_safe(n.get('source_url', '#'))}" style="color:var(--accent);" target="_blank">GETS Notice &#8599;</a></span>
  </div>

</div>
</body>
</html>"""


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_pursuit_package(
    notice_id: str,
    client_name: str,
    output_dir: Optional[Path] = None,
    is_demo: bool = False,
    demo_watermark: str = "",
    preferred_sectors: Optional[list[str]] = None,
) -> Path:
    """
    Generate a pursuit intelligence package for a given notice and client.
    preferred_sectors: client's sector focus (e.g. ['ICT','security']).
    Returns path to the generated HTML file.
    """
    logger.info("Generating pursuit package: notice=%s client=%s", notice_id, client_name)

    # 1. Gather all data
    notice = _get_notice(notice_id)
    if not notice:
        raise ValueError(f"Notice {notice_id} not found in database")

    sector = notice.get("sector_tag") or "other"
    agency = notice.get("agency") or ""

    competitors = _get_competitive_landscape(agency, sector)
    client_history = _get_client_history(client_name, sector, agency)
    incumbent = _detect_incumbent(agency, sector)
    agency_stats = _get_agency_stats(agency, sector)
    flags = _get_relevant_flags(agency, sector)
    citation = _mbie_citation(sector, agency)

    context = {
        "client_name": client_name,
        "preferred_sectors": preferred_sectors or [],
        "notice": dict(notice),
        "enrichment": {
            "summary": notice.get("summary"),
            "evaluation_weighting": notice.get("evaluation_weighting"),
            "red_flags": notice.get("red_flags"),
            "strategic_framing": notice.get("strategic_framing"),
        },
        "competitors": [dict(c) for c in competitors],
        "client_history": client_history,
        "incumbent": dict(incumbent) if incumbent else None,
        "agency_stats": agency_stats,
        "flags": [dict(f) for f in flags],
        "mbie_citation": citation,
    }

    # 2. Call Claude
    logger.info("Calling Claude for synthesis...")
    analysis = _call_claude(context)
    if not analysis:
        raise RuntimeError("Claude synthesis failed — no analysis returned")

    # 3. Render HTML
    html = _render_html(notice, analysis, context, client_name,
                        is_demo=is_demo, demo_watermark=demo_watermark)

    # 4. Save
    if output_dir is None:
        output_dir = _artefact_dir(client_name)

    filename = f"{notice_id}_pursuit_package.html"
    out_path = output_dir / filename
    out_path.write_text(html, encoding="utf-8")
    logger.info("Pursuit package written to %s", out_path)
    return out_path


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )
    p = argparse.ArgumentParser(description="Generate a pursuit intelligence package")
    p.add_argument("notice_id", help="GETS notice ID")
    p.add_argument("client_name", help="Client company name")
    p.add_argument("--output-dir", help="Output directory (optional)")
    p.add_argument(
        "--sectors",
        help="Client preferred sectors e.g. ICT,security. Used for context framing.",
    )
    args = p.parse_args()
    sectors = [s.strip() for s in args.sectors.split(",")] if args.sectors else None

    out = generate_pursuit_package(
        args.notice_id,
        args.client_name,
        Path(args.output_dir) if args.output_dir else None,
        preferred_sectors=sectors,
    )
    print(f"Generated: {out}")
