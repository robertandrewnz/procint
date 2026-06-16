"""
Layer 3 — Competitor Profile Report.

Generates a structured five-part competitive intelligence profile on a named
competitor, drawing from MBIE historical award data and Claude market knowledge.

Parts:
  A — CI Battle Card
  B — Centre of Gravity Analysis
  C — Exploitable Commercial Behaviours (ECB) Profile
  D — Competitive Threat Assessment (sector-specific)
  E — Intelligence Gaps

Usage:
  python competitor_profile.py "<Competitor Name>" --client "<Client Name>" \\
      --sector "government cybersecurity and SOC services"
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

# ── Guard: fictional demo firm names must never appear in real profiles ────────

_DEMO_FIRM_NAMES = frozenset({
    "john smith", "cityworks nz", "sentinel digital", "meridian civil",
    "apex engineering", "korepath systems", "southern civil group",
    "medtech solutions nz",
})


def _assert_real_client(client_name: Optional[str]) -> None:
    """Raise ValueError if client_name is absent or a demo placeholder."""
    if not client_name or not client_name.strip():
        raise ValueError(
            "client_name must be provided — cannot generate a competitor profile "
            "without knowing which client this is for."
        )
    if client_name.strip().lower() in _DEMO_FIRM_NAMES:
        raise ValueError(
            f"client_name '{client_name}' is a demo placeholder firm name. "
            "Real competitor profiles must use the authenticated user's firm name."
        )


# ── Inline markdown helper (retained for fallback rendering) ──────────────────

def _inline_md(text: str) -> str:
    """Convert inline markdown (**bold**, _italic_, `code`) in an already-escaped string."""
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__(.+?)__", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    text = re.sub(r"_(.+?)_", r"<em>\1</em>", text)
    text = re.sub(
        r"`(.+?)`",
        r'<code style="background:var(--surf2);padding:.1em .3em;border-radius:3px;font-size:.82em;">\1</code>',
        text,
    )
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


# ── Data quality assessment ───────────────────────────────────────────────────

def _assess_data_quality(totals: dict) -> dict:
    """
    Assess MBIE data sufficiency.
    Returns {"quality": "THIN"|"ADEQUATE", "reason": str, ...}.
    """
    total_wins = int(totals.get("total_wins") or 0)
    total_value = float(totals.get("total_value") or 0)
    last_win = totals.get("last_win")

    months_since_last = 9999
    if last_win:
        try:
            from datetime import datetime as _dt
            if hasattr(last_win, "date"):
                last_win_date = last_win.date() if hasattr(last_win, "date") else last_win
            else:
                last_win_date = date.fromisoformat(str(last_win)[:10])
            delta = date.today() - last_win_date
            months_since_last = delta.days // 30
        except Exception:
            months_since_last = 9999

    thin_reasons = []
    if total_wins < 5:
        thin_reasons.append(f"{total_wins} recorded wins (threshold: 5)")
    if total_value < 1_000_000:
        thin_reasons.append(f"{_fmt_value(total_value)} total value (threshold: $1M)")
    if months_since_last > 24:
        months_label = f"{months_since_last} months ago" if months_since_last < 9999 else "never"
        thin_reasons.append(f"last recorded win: {months_label} (threshold: 24 months)")

    quality = "THIN" if thin_reasons else "ADEQUATE"
    reason = "; ".join(thin_reasons) if thin_reasons else "sufficient award records"

    last_win_str = str(totals.get("last_win", ""))[:7] if totals.get("last_win") else "no records"

    return {
        "quality": quality,
        "reason": reason,
        "total_wins": total_wins,
        "total_value": total_value,
        "last_win_str": last_win_str,
        "months_since_last": months_since_last,
    }


# ── Claude synthesis ──────────────────────────────────────────────────────────

_COMPETITOR_SYSTEM = """\
You are a competitive intelligence analyst producing a briefing for a senior BD professional \
in {sector_context} government procurement in New Zealand.

GROUND RULES:
- Draw on MBIE data where available. Where MBIE data is thin or absent, draw on your \
knowledge of this firm's actual market position — do not pretend ignorance of a well-known NZ market participant.
- Never produce confident negative conclusions from thin data. A firm may have extensive \
government relationships not captured in MBIE (direct engagements, panel arrangements, \
classified contracts, recent wins not yet published).
- Explicitly distinguish between "MBIE-sourced" and "Market knowledge" conclusions.
- Be analytically direct: make calls, explain reasoning, flag uncertainty explicitly \
rather than hedging everything.
- When data quality is THIN: you MUST open Part D with the explicit caveat statement \
as instructed. Never use phrases like "negligible presence," "unable to convert," or \
"no evident relationships" — thin MBIE data cannot support those conclusions.
- Assess this competitor specifically as a competitor in {sector_context} government \
procurement, not as a general business profile.

Respond ONLY with a valid JSON object, no preamble, no markdown fences."""


_COMPETITOR_PROMPT = """\
Generate a competitive intelligence profile of {competitor_name} for {client_name}, \
who competes in {sector_context} government procurement in New Zealand.

=== MBIE DATA QUALITY ===
Data quality: {data_quality}
{data_quality_caveat}

=== MBIE TOTALS ===
Total recorded wins: {total_wins}
Total awarded value: {total_value}
Average contract value: {avg_value}
First win recorded: {first_win}
Most recent win recorded: {last_win}

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

=== CLIENT CONTEXT ===
This profile is for {client_name}, who is competing against {competitor_name} in \
{sector_context} government procurement.

Produce a JSON object with EXACTLY these keys:

"battle_card": Object with string values for each of these fields:
  "company_overview": 2-3 sentences on company background and market position \
(MBIE-sourced OR Market knowledge — label each claim).
  "key_offerings": Their specific offerings in {sector_context} (not generic — be specific \
to this sector).
  "core_strengths": Government-relevant strengths, 2-4 bullet points as a single string \
separated by " | ".
  "key_weaknesses": Known weaknesses or gaps in their government positioning, 2-3 points \
as a single string separated by " | ".
  "market_position": Their actual standing in NZ {sector_context} government procurement — \
include whether MBIE data understates their position and why.
  "sales_bd_approach": How they typically win government work — relationship-led, \
panel-dependent, open tender, etc.
  "known_government_clients": Named agencies they have documented or publicly known \
relationships with — distinguish MBIE-sourced from Market knowledge.
  "disruption_opportunities": For {client_name} specifically — where are the openings \
to displace {competitor_name} or take market share?
  "pricing_intelligence": "Pricing intelligence requires primary research — contract \
values in MBIE reflect gross awarded amounts, not unit rates or margin structure. \
[If any pricing signals are available from public sources, note them here.]"

"centre_of_gravity": Object with:
  "cog_statement": One sentence naming {competitor_name}'s single most critical \
source of competitive strength in {sector_context} government procurement.
  "critical_capabilities": Array of exactly 3 strings — what enables their COG.
  "critical_requirements": Array of exactly 3 strings — what they must maintain \
to preserve their COG.
  "critical_vulnerabilities": Array of exactly 3 strings — what would degrade their COG.
  "most_exploitable_vulnerability": One paragraph (3-5 sentences) — the specific \
scenario that most threatens their position and what {client_name} would need to do \
to trigger it.

"ecb_profile": Object with:
  "rows": Array of exactly 6 objects, each with:
    "behaviour_category": One of: "Sales Model", "Service Delivery", "Procurement Behaviour", \
"Partnership Strategy", "Pricing Model", "Marketing & Messaging"
    "observed_behaviour": 1-2 sentences describing the observed pattern.
    "exploitable_strategy": 1-2 sentences on how {client_name} can exploit this pattern.
  "key_exploitable_behaviour": 2-3 sentence synthesis of the single most actionable \
commercial behaviour pattern — the one with the clearest path to conversion.

"threat_assessment": Object with:
  "threat_level": Exactly one of "High", "Medium", or "Low".
  "threat_reasoning": 2-3 sentences with explicit reasoning for the threat level — \
reference specific MBIE data or market knowledge, not generic statements.
  "mbie_footprint_accuracy": One sentence on whether their MBIE footprint understates \
or accurately reflects their actual government market position in {sector_context} — \
and why (panel arrangements, direct engagements, classified work, etc.).
  "non_mbie_relationships": String listing key contracts or relationships NOT visible \
in MBIE but knowable from public sources (website, press releases, media, LinkedIn). \
State source for each claim. If none known, say so.
  "vulnerable_areas": String — where they are genuinely vulnerable in {sector_context}.
  "entrenched_areas": String — where they are genuinely entrenched and displacement \
would be costly for {client_name} to attempt.

"intelligence_gaps": Array of exactly 5 strings — specific intelligence questions whose \
answers would most change this assessment. Not generic ("what is their strategy") but \
specific (e.g. "Do they hold a current panel arrangement with [specific agency] that \
gives them preferred supplier status for {sector_context} contracts?"). Each question \
must be answerable through primary research (interviews, tender result monitoring, \
LinkedIn, RFP feedback)."""


def _generate_structured_profile(
    data: dict,
    client_name: str,
    sector_context: str,
    data_quality: dict,
) -> Optional[dict]:
    """Call Claude to generate structured five-part competitive intelligence profile."""
    name = data.get("name", "Unknown")
    totals = data.get("totals", {})
    sectors = data.get("sectors", [])
    agencies = data.get("agencies", [])
    by_year = data.get("by_year", [])
    largest = data.get("largest_contract", {})
    recent5 = data.get("recent5", [])

    year_lines = "\n".join(
        f"  {r['year']}: {r['wins']} wins ({_fmt_value(r.get('value'))} total)"
        for r in by_year[:6]
    ) or "  No year data available."

    sector_lines = "\n".join(
        f"  {r['sector_tag']}: {r['wins']} wins ({_fmt_value(r.get('value'))} total)"
        for r in sectors[:5]
    ) or "  No sector data available."

    agency_lines = "\n".join(
        f"  {r['posting_agency']}: {r['wins']} wins ({_fmt_value(r.get('value'))} total)"
        for r in agencies[:5]
    ) or "  No agency data available."

    largest_line = (
        f"{_safe(largest.get('title', 'Unknown'))} — "
        f"{_safe(largest.get('posting_agency', ''))} — "
        f"{_fmt_value(largest.get('awarded_amount'))} — "
        f"{str(largest.get('awarded_date', ''))[:10]}"
        if largest else "Not available."
    )

    recent5_lines = "\n".join(
        f"  {str(r.get('awarded_date', ''))[:10]}  {_safe(r.get('title', ''))[:55]}  "
        f"({_safe(r.get('posting_agency', ''))})  {_fmt_value(r.get('awarded_amount'))}"
        for r in recent5
    ) or "  No recent wins found."

    dq = data_quality["quality"]
    dq_caveat = ""
    if dq == "THIN":
        dq_caveat = (
            f"IMPORTANT: Data quality is THIN. "
            f"({data_quality['total_wins']} recorded wins, "
            f"{_fmt_value(data_quality['total_value'])} total value, "
            f"last recorded award: {data_quality['last_win_str']}). "
            f"When data is THIN, you MUST open the threat_reasoning in Part D with: "
            f"\"MBIE award data for {name} is limited ({data_quality['total_wins']} recorded "
            f"wins, {_fmt_value(data_quality['total_value'])} total value, last recorded "
            f"award {data_quality['last_win_str']}). The analysis below draws on MBIE data "
            f"where available but relies substantially on market knowledge for a firm of this "
            f"profile. Treat MBIE-derived conclusions with caution.\" "
            f"Do NOT use phrases like 'negligible presence', 'unable to convert', or "
            f"'no evident relationships' — these conclusions are not supported by thin data."
        )

    system = _COMPETITOR_SYSTEM.format(sector_context=sector_context)

    prompt = _COMPETITOR_PROMPT.format(
        competitor_name=name,
        client_name=client_name,
        sector_context=sector_context,
        data_quality=dq,
        data_quality_caveat=dq_caveat,
        total_wins=totals.get("total_wins", 0),
        total_value=_fmt_value(totals.get("total_value")),
        avg_value=_fmt_value(totals.get("avg_value")),
        first_win=str(totals.get("first_win", ""))[:10],
        last_win=str(totals.get("last_win", ""))[:10],
        year_lines=year_lines,
        sector_lines=sector_lines,
        agency_lines=agency_lines,
        largest_line=largest_line,
        recent5_lines=recent5_lines,
    )

    try:
        client_api = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        msg = client_api.messages.create(
            model=config.CLAUDE_MODEL_L3,
            max_tokens=8192,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError as jerr:
            logger.warning("JSON parse failed (%s) — attempting recovery", jerr)
            fixed = raw
            opens = fixed.count("{") - fixed.count("}")
            arr_opens = fixed.count("[") - fixed.count("]")
            last_clean = max(fixed.rfind(","), fixed.rfind('"'))
            if last_clean > 0:
                fixed = fixed[:last_clean]
            for _ in range(arr_opens):
                fixed += "]"
            for _ in range(opens):
                fixed += "}"
            try:
                result = json.loads(fixed)
                logger.info("JSON truncation recovery succeeded")
                return result
            except Exception:
                logger.error("Competitor profile synthesis failed: %s", jerr)
                return None
    except Exception as exc:
        logger.error("Claude competitor synthesis failed: %s", exc)
        return None


# ── HTML rendering ─────────────────────────────────────────────────────────────

_PROFILE_CSS = """:root {
  --bg:#f5f6f8; --surface:#ffffff; --surf2:#f0f2f5; --border:#e2e6ea;
  --text:#2c3e50; --muted:#6c757d; --navy:#1a2d4a; --gold:#2a9d8f;
  --gold-l:#e0f4f2; --navy-l:#e8ecf3; --red:#c0392b; --red-l:#fdecea;
  --green:#27ae60; --amber:#d4a017;
  --font:'Inter',system-ui,-apple-system,sans-serif;
}
*, *::before, *::after { box-sizing:border-box; margin:0; padding:0; }
body { background:var(--bg); color:var(--text); font-family:var(--font);
       font-size:14px; line-height:1.6; padding:2.5rem;
       max-width:980px; margin:0 auto; -webkit-font-smoothing:antialiased; }
a { color:var(--navy); text-decoration:none; }
a:hover { color:var(--gold); }
.header { border-bottom:2px solid var(--navy); padding-bottom:1.25rem; margin-bottom:2rem; }
.header-label { font-size:.62rem; font-weight:700; letter-spacing:.1em;
  text-transform:uppercase; color:var(--gold); margin-bottom:.3rem; }
.header-name { font-size:1.55rem; font-weight:800; color:var(--navy); margin-bottom:.4rem; }
.header-meta { font-size:.75rem; color:var(--muted); display:flex; flex-wrap:wrap; gap:.5rem; align-items:center; }
.dq-badge { font-size:.66rem; font-weight:700; letter-spacing:.06em; text-transform:uppercase;
  padding:.22rem .6rem; border-radius:999px; border:1px solid; }
.dq-thin { background:#fff3cd; color:#856404; border-color:#ffc107; }
.dq-adequate { background:var(--gold-l); color:#1a6b62; border-color:var(--gold); }
.sector-pill { font-size:.7rem; padding:.2rem .65rem; background:var(--navy-l);
  color:var(--navy); border-radius:999px; border:1px solid #b0bcd4; font-weight:600; }
.stat-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:1rem; margin-bottom:2rem; }
.stat-box { background:var(--surface); border:1px solid var(--border);
  border-radius:8px; padding:.85rem 1rem; box-shadow:0 1px 3px rgba(26,45,74,.06); }
.stat-label { font-size:.62rem; font-weight:700; letter-spacing:.07em;
  text-transform:uppercase; color:var(--muted); margin-bottom:.25rem; }
.stat-value { font-size:1.3rem; font-weight:800; color:var(--navy); letter-spacing:-.03em; }
.stat-sub { font-size:.68rem; color:var(--muted); margin-top:.15rem; }
.section { margin-bottom:2.5rem; }
.section-label { font-size:.6rem; font-weight:700; letter-spacing:.1em;
  text-transform:uppercase; color:var(--gold); margin-bottom:.2rem; }
.section-title { font-size:.82rem; font-weight:800; color:var(--navy); margin-bottom:1rem;
  padding-bottom:.4rem; border-bottom:2px solid var(--border); letter-spacing:.01em; }
.cog-box { background:var(--navy); color:#fff; border-radius:8px;
  padding:1.25rem 1.5rem; margin-bottom:1rem; }
.cog-label { font-size:.6rem; font-weight:700; letter-spacing:.1em; text-transform:uppercase;
  color:var(--gold); margin-bottom:.4rem; }
.cog-statement { font-size:1rem; font-weight:700; line-height:1.4; margin-bottom:.85rem; }
.cog-grid { display:grid; grid-template-columns:1fr 1fr 1fr; gap:.75rem; margin-bottom:.85rem; }
.cog-col { background:rgba(255,255,255,.06); border-radius:6px; padding:.75rem; }
.cog-col-label { font-size:.6rem; font-weight:700; letter-spacing:.09em; text-transform:uppercase;
  color:rgba(255,255,255,.5); margin-bottom:.4rem; }
.cog-col ul { margin:0; padding-left:1rem; }
.cog-col li { font-size:.8rem; color:rgba(255,255,255,.82); margin-bottom:.2rem; line-height:1.5; }
.cog-exploit { border-top:1px solid rgba(255,255,255,.12); padding-top:.75rem; }
.cog-exploit-label { font-size:.6rem; font-weight:700; letter-spacing:.09em;
  text-transform:uppercase; color:var(--gold); margin-bottom:.35rem; }
.cog-exploit-text { font-size:.83rem; color:rgba(255,255,255,.85); line-height:1.65; }
.threat-badge { display:inline-block; font-size:.75rem; font-weight:800; letter-spacing:.04em;
  text-transform:uppercase; padding:.3rem .85rem; border-radius:6px; margin-bottom:.75rem; }
.threat-high   { background:var(--red-l);  color:var(--red);  border:1px solid #f1a9a0; }
.threat-medium { background:#fff3cd;       color:#856404;     border:1px solid #ffc107; }
.threat-low    { background:var(--gold-l); color:#1a6b62;     border:1px solid var(--gold); }
.intel-gap-list { counter-reset:gaps; list-style:none; padding:0; }
.intel-gap-list li { counter-increment:gaps; display:flex; gap:.75rem; align-items:flex-start;
  padding:.6rem .75rem; border:1px solid var(--border); border-radius:6px;
  margin-bottom:.5rem; background:var(--surface); font-size:.84rem; line-height:1.6; }
.intel-gap-list li::before { content:counter(gaps); flex-shrink:0; width:1.5rem; height:1.5rem;
  background:var(--navy); color:#fff; border-radius:50%; font-size:.68rem; font-weight:700;
  display:flex; align-items:center; justify-content:center; }
table { width:100%; border-collapse:collapse; font-size:.82rem; }
thead tr { background:var(--navy); }
th { color:#fff; font-size:.63rem; font-weight:600; letter-spacing:.07em;
  text-transform:uppercase; padding:.45rem .65rem; text-align:left; }
td { padding:.5rem .65rem; border-bottom:1px solid var(--border); color:var(--text);
  vertical-align:top; }
tr:last-child td { border-bottom:none; }
tbody tr:hover td { background:var(--surf2); }
.win-bar { height:8px; background:var(--surf2); border-radius:4px;
  overflow:hidden; width:80px; display:inline-block; vertical-align:middle; margin-left:.4rem; }
.win-bar-fill { height:100%; background:var(--gold); border-radius:4px; }
.two-col-table td:first-child { font-weight:600; color:var(--navy); width:22%; white-space:nowrap; }
.ecb-table td:first-child { font-weight:600; color:var(--navy); width:18%; white-space:nowrap; }
.source-tag { font-size:.62rem; padding:.1rem .35rem; border-radius:3px;
  font-weight:600; letter-spacing:.03em; margin-left:.3rem; }
.src-mbie   { background:var(--navy-l); color:var(--navy); }
.src-market { background:#fff3cd; color:#856404; }
.thin-warning { background:#fff3cd; border:1px solid #ffc107; border-radius:8px;
  padding:.85rem 1.1rem; margin-bottom:1.5rem; font-size:.82rem; color:#4a3800; }
.thin-warning strong { color:#856404; }
.doc-footer { margin-top:3rem; padding-top:1rem; border-top:1px solid var(--border);
  font-size:.7rem; color:var(--muted); display:flex; justify-content:space-between; }
@media (max-width:768px) {
  body { padding:1.5rem 1rem; }
  .stat-grid { grid-template-columns:1fr 1fr; }
  .cog-grid { grid-template-columns:1fr; }
  table { display:block; overflow-x:auto; }
  .doc-footer { flex-direction:column; gap:.3rem; }
}
@media (max-width:480px) {
  body { padding:1rem .75rem; font-size:13px; }
  .header-name { font-size:1.2rem; }
  .stat-grid { grid-template-columns:1fr 1fr; gap:.6rem; }
  .stat-value { font-size:1.05rem; }
}"""


def _render_profile_html(
    data: dict,
    analysis: Optional[dict],
    client_name: str,
    sector_context: str,
    data_quality: dict,
    h2h: Optional[list[dict]] = None,
) -> str:
    totals = data.get("totals", {})
    name = data.get("name", "Unknown")
    run_date = date.today().isoformat()

    total_wins = int(totals.get("total_wins") or 0)
    total_value = float(totals.get("total_value") or 0)
    avg_value = float(totals.get("avg_value") or 0)
    first_win = str(totals.get("first_win", ""))[:7]
    last_win  = str(totals.get("last_win", ""))[:7]

    primary_sectors = data.get("sectors", [])
    primary_sector  = primary_sectors[0]["sector_tag"] if primary_sectors else "unknown"
    regions = [r["region"] for r in data.get("regions", [])]

    dq = data_quality["quality"]
    dq_badge_cls = "dq-thin" if dq == "THIN" else "dq-adequate"
    dq_label = "THIN DATA" if dq == "THIN" else "ADEQUATE DATA"

    # ── Thin data warning ────────────────────────────────────────────────────
    thin_warning_html = ""
    if dq == "THIN":
        thin_warning_html = (
            f'<div class="thin-warning">'
            f'<strong>⚠ MBIE Data Caveat</strong> — Data quality for {_safe(name)} is '
            f'THIN ({data_quality["total_wins"]} recorded wins, '
            f'{_fmt_value(data_quality["total_value"])} total value, '
            f'last recorded award: {data_quality["last_win_str"]}). '
            f'Analysis below draws on MBIE data where available and market knowledge '
            f'for firms of this profile. MBIE-derived conclusions should be treated with '
            f'caution — this firm may have substantial government relationships not visible '
            f'in open contract award data (panel arrangements, direct engagements, '
            f'classified or sensitive contracts).'
            f'</div>'
        )

    # ── Sector/agency/value tables ────────────────────────────────────────────
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

    recent_rows = ""
    for r in data.get("recent", []):
        recent_rows += (
            f"<tr><td>{_safe(r.get('title', ''))[:60]}</td>"
            f"<td>{_safe(r.get('posting_agency', ''))[:35]}</td>"
            f"<td>{_fmt_value(r.get('awarded_amount'))}</td>"
            f"<td>{str(r.get('awarded_date', ''))[:10]}</td></tr>"
        )

    vd = data.get("value_dist", {})
    vd_total = sum([
        int(vd.get("under_100k") or 0), int(vd.get("k100_500k") or 0),
        int(vd.get("m500k_2m") or 0),   int(vd.get("m2_10m") or 0),
        int(vd.get("over_10m") or 0),
    ])
    def vd_bar(count):
        if not vd_total: return ""
        pct = int(int(count or 0) / vd_total * 100)
        return f'<span class="win-bar"><span class="win-bar-fill" style="width:{pct}%"></span></span>'

    # ── Head-to-head section ─────────────────────────────────────────────────
    h2h_html = ""
    if client_name and h2h:
        h2h_rows = ""
        for row in h2h:
            comp_wins = row.get("comp_wins", 0)
            cli_wins  = row.get("cli_wins", 0)
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
        h2h_html = (
            f'<div class="section">'
            f'<div class="section-label">MBIE Overlap</div>'
            f'<div class="section-title">Head-to-Head vs {_safe(client_name)}</div>'
            f'<table><thead><tr>'
            f'<th>Agency</th><th>Advantage</th>'
            f'<th>{_safe(name)} value</th><th>{_safe(client_name)} value</th>'
            f'</tr></thead><tbody>{h2h_rows}</tbody></table>'
            f'</div>'
        )

    # ── Analysis sections ─────────────────────────────────────────────────────
    a = analysis or {}

    # Part A — CI Battle Card
    bc = a.get("battle_card") or {}
    def _bc_row(label, value):
        if not value: return ""
        return (
            f'<tr><td class="two-col-table" style="font-weight:600;color:var(--navy);'
            f'width:22%;vertical-align:top;">{_safe(label)}</td>'
            f'<td style="font-size:.82rem;line-height:1.65;">{_safe(value)}</td></tr>'
        )
    battle_card_rows = "".join([
        _bc_row("Company overview",    bc.get("company_overview", "")),
        _bc_row("Key offerings",       bc.get("key_offerings", "")),
        _bc_row("Core strengths",      bc.get("core_strengths", "")),
        _bc_row("Key weaknesses",      bc.get("key_weaknesses", "")),
        _bc_row("Market position",     bc.get("market_position", "")),
        _bc_row("Sales / BD approach", bc.get("sales_bd_approach", "")),
        _bc_row("Known govt clients",  bc.get("known_government_clients", "")),
        _bc_row("Pricing intel",       bc.get("pricing_intelligence", "")),
        _bc_row("Disruption opps",     bc.get("disruption_opportunities", "")),
    ])

    battle_card_html = ""
    if battle_card_rows:
        battle_card_html = (
            f'<div class="section">'
            f'<div class="section-label">Part A</div>'
            f'<div class="section-title">CI Battle Card — {_safe(sector_context)}</div>'
            f'<table><tbody>{battle_card_rows}</tbody></table>'
            f'</div>'
        )

    # Part B — Centre of Gravity
    cog = a.get("centre_of_gravity") or {}
    cog_html = ""
    if cog:
        def _cog_list(items, label, col_class=""):
            if not items: return ""
            lis = "".join(f"<li>{_safe(i)}</li>" for i in items)
            return (
                f'<div class="cog-col {col_class}">'
                f'<div class="cog-col-label">{label}</div>'
                f'<ul>{lis}</ul></div>'
            )
        cog_html = (
            f'<div class="section">'
            f'<div class="section-label">Part B</div>'
            f'<div class="section-title">Centre of Gravity Analysis</div>'
            f'<div class="cog-box">'
            f'<div class="cog-label">Critical source of competitive strength</div>'
            f'<div class="cog-statement">{_safe(cog.get("cog_statement", ""))}</div>'
            f'<div class="cog-grid">'
            f'{_cog_list(cog.get("critical_capabilities", []), "Critical Capabilities")}'
            f'{_cog_list(cog.get("critical_requirements", []), "Critical Requirements")}'
            f'{_cog_list(cog.get("critical_vulnerabilities", []), "Critical Vulnerabilities")}'
            f'</div>'
            f'<div class="cog-exploit">'
            f'<div class="cog-exploit-label">Most Exploitable Vulnerability</div>'
            f'<div class="cog-exploit-text">{_safe(cog.get("most_exploitable_vulnerability", ""))}</div>'
            f'</div></div></div>'
        )

    # Part C — ECB Profile
    ecb = a.get("ecb_profile") or {}
    ecb_html = ""
    if ecb:
        ecb_rows_html = ""
        for row in (ecb.get("rows") or []):
            ecb_rows_html += (
                f'<tr>'
                f'<td class="ecb-table" style="font-weight:600;color:var(--navy);width:18%;vertical-align:top;">'
                f'{_safe(row.get("behaviour_category", ""))}</td>'
                f'<td style="font-size:.82rem;line-height:1.65;vertical-align:top;">'
                f'{_safe(row.get("observed_behaviour", ""))}</td>'
                f'<td style="font-size:.82rem;line-height:1.65;vertical-align:top;background:rgba(42,157,143,.04);">'
                f'{_safe(row.get("exploitable_strategy", ""))}</td>'
                f'</tr>'
            )
        key_ecb = _safe(ecb.get("key_exploitable_behaviour", ""))
        ecb_html = (
            f'<div class="section">'
            f'<div class="section-label">Part C</div>'
            f'<div class="section-title">Exploitable Commercial Behaviours (ECB) Profile</div>'
            f'<table>'
            f'<thead><tr><th style="width:18%">Behaviour</th>'
            f'<th style="width:41%">Observed Pattern</th>'
            f'<th style="width:41%">Exploitable Strategy for {_safe(client_name)}</th>'
            f'</tr></thead><tbody>{ecb_rows_html}</tbody></table>'
            + (f'<div style="margin-top:1rem;background:var(--gold-l);border:1px solid var(--gold);'
               f'border-radius:6px;padding:.75rem 1rem;">'
               f'<div style="font-size:.62rem;font-weight:700;letter-spacing:.09em;'
               f'text-transform:uppercase;color:#1a6b62;margin-bottom:.35rem;">'
               f'Key Exploitable Commercial Behaviour</div>'
               f'<div style="font-size:.84rem;color:var(--text);line-height:1.65;">{key_ecb}</div>'
               f'</div>' if key_ecb else "")
            + f'</div>'
        )

    # Part D — Competitive Threat Assessment
    ta = a.get("threat_assessment") or {}
    ta_html = ""
    if ta:
        tl = ta.get("threat_level", "Medium")
        tl_cls = f"threat-{tl.lower()}"
        ta_html = (
            f'<div class="section">'
            f'<div class="section-label">Part D</div>'
            f'<div class="section-title">Competitive Threat Assessment — {_safe(sector_context)}</div>'
            f'<div class="threat-badge {tl_cls}">{_safe(tl)} Threat</div>'
            + (f'<p style="font-size:.84rem;line-height:1.7;margin-bottom:.85rem;">'
               f'{_safe(ta.get("threat_reasoning", ""))}</p>' if ta.get("threat_reasoning") else "")
            + f'<table><tbody>'
            + (f'<tr><td style="font-weight:600;width:28%">MBIE footprint accuracy</td>'
               f'<td style="font-size:.82rem;">{_safe(ta.get("mbie_footprint_accuracy",""))}</td></tr>' if ta.get("mbie_footprint_accuracy") else "")
            + (f'<tr><td style="font-weight:600;">Non-MBIE relationships</td>'
               f'<td style="font-size:.82rem;">{_safe(ta.get("non_mbie_relationships",""))}</td></tr>' if ta.get("non_mbie_relationships") else "")
            + (f'<tr><td style="font-weight:600;">Vulnerable areas</td>'
               f'<td style="font-size:.82rem;">{_safe(ta.get("vulnerable_areas",""))}</td></tr>' if ta.get("vulnerable_areas") else "")
            + (f'<tr><td style="font-weight:600;">Entrenched areas</td>'
               f'<td style="font-size:.82rem;">{_safe(ta.get("entrenched_areas",""))}</td></tr>' if ta.get("entrenched_areas") else "")
            + f'</tbody></table></div>'
        )

    # Part E — Intelligence Gaps
    gaps = a.get("intelligence_gaps") or []
    gaps_html = ""
    if gaps:
        gap_items = "".join(f"<li>{_safe(g)}</li>" for g in gaps[:5])
        gaps_html = (
            f'<div class="section">'
            f'<div class="section-label">Part E</div>'
            f'<div class="section-title">Intelligence Gaps</div>'
            f'<ul class="intel-gap-list">{gap_items}</ul>'
            f'</div>'
        )

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
  <div class="header-label">Competitor Intelligence Profile</div>
  <div class="header-name">{_safe(name)}</div>
  <div class="header-meta">
    <span class="dq-badge {dq_badge_cls}">{dq_label}</span>
    <span class="sector-pill">{_safe(sector_context)}</span>
    <span>Generated {run_date}</span>
    <span style="color:var(--border);">|</span>
    <span>Prepared for {_safe(client_name)}</span>
    <span style="color:var(--border);">|</span>
    <span>Source: MBIE GETS Open Data (2014–2025)</span>
  </div>
</div>

{thin_warning_html}

<div class="stat-grid">
  <div class="stat-box">
    <div class="stat-label">Total wins (MBIE)</div>
    <div class="stat-value">{total_wins}</div>
    <div class="stat-sub">{first_win} – {last_win}</div>
  </div>
  <div class="stat-box">
    <div class="stat-label">Total awarded value</div>
    <div class="stat-value">{_fmt_value(total_value)}</div>
    {"<div class='stat-sub' style='color:#856404;'>MBIE data only — see caveat</div>" if dq == "THIN" else ""}
  </div>
  <div class="stat-box">
    <div class="stat-label">Average contract</div>
    <div class="stat-value">{_fmt_value(avg_value)}</div>
  </div>
  <div class="stat-box">
    <div class="stat-label">Primary sector</div>
    <div class="stat-value" style="font-size:.88rem;">{_safe(primary_sector.replace("_"," "))}</div>
    <div class="stat-sub">{", ".join(regions[:3]) or "National"}</div>
  </div>
</div>

{battle_card_html}

{cog_html}

{ecb_html}

{ta_html}

<div class="section">
  <div class="section-label">MBIE Award Data</div>
  <div class="section-title">Win Record by Sector</div>
  <table>
    <thead><tr><th>Sector</th><th>Wins</th><th>Total value</th></tr></thead>
    <tbody>{sector_rows or '<tr><td colspan="3" style="color:var(--muted);">No sector data.</td></tr>'}</tbody>
  </table>
</div>

<div class="section">
  <div class="section-label">MBIE Award Data</div>
  <div class="section-title">Top Agencies</div>
  <table>
    <thead><tr><th>Agency</th><th>Awards</th><th>Total value</th></tr></thead>
    <tbody>{agency_rows or '<tr><td colspan="3" style="color:var(--muted);">No agency data.</td></tr>'}</tbody>
  </table>
</div>

<div class="section">
  <div class="section-label">MBIE Award Data</div>
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
  <div class="section-label">MBIE Award Data</div>
  <div class="section-title">Recent Activity — Last 12 Months</div>
  {"<table><thead><tr><th>Contract</th><th>Agency</th><th>Value</th><th>Date</th></tr></thead><tbody>"
   + recent_rows + "</tbody></table>"
   if recent_rows
   else '<div style="font-size:.82rem;color:var(--muted);font-style:italic;">No MBIE award records in the past 12 months.</div>'}
</div>

{h2h_html}

{gaps_html}

<div class="doc-footer">
  <span>Groundwork by BidEdge &nbsp;|&nbsp; {_safe(name)} &mdash; {_safe(sector_context)} &nbsp;|&nbsp; {run_date}</span>
  <span>MBIE GETS Open Data — 27,948 award notices (2014–2025)</span>
</div>

</body>
</html>"""


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_competitor_profile(
    competitor_name: str,
    client_name: Optional[str] = None,
    sector_context: Optional[str] = None,
    output_dir: Optional[Path] = None,
    is_demo: bool = False,
) -> Path:
    """
    Generate a structured competitor intelligence profile.

    Args:
        competitor_name:  Name of the competitor to profile.
        client_name:      Authenticated user's firm name — REQUIRED, must not be a demo name.
        sector_context:   Procurement sector for framing (e.g. "government cybersecurity and SOC
                          services"). REQUIRED for meaningful analysis.
        output_dir:       Where to save the HTML file. Defaults to _artefact_dir(client_name).
        is_demo:          When True, skips the demo-name guard (used by generate_demo_content.py).
    """
    # Guard: reject empty or demo client names (bypassed for demo content generation)
    if not is_demo:
        _assert_real_client(client_name)

    if not sector_context or not sector_context.strip():
        raise ValueError(
            "sector_context is required — cannot generate a competitor profile without "
            "knowing which procurement sector to frame the analysis around."
        )

    logger.info(
        "Generating competitor profile: competitor=%s client=%s sector=%s",
        competitor_name, client_name, sector_context,
    )

    data         = _get_competitor_data(competitor_name)
    h2h          = _get_head_to_head(competitor_name, client_name)
    data_quality = _assess_data_quality(data.get("totals", {}))

    logger.info(
        "Data quality: %s (%s wins, %s total value, last win: %s)",
        data_quality["quality"], data_quality["total_wins"],
        _fmt_value(data_quality["total_value"]), data_quality["last_win_str"],
    )

    analysis = _generate_structured_profile(
        data=data,
        client_name=client_name,
        sector_context=sector_context,
        data_quality=data_quality,
    )
    if not analysis:
        logger.warning("Claude synthesis failed — rendering profile with MBIE data only")

    html = _render_profile_html(
        data=data,
        analysis=analysis,
        client_name=client_name,
        sector_context=sector_context,
        data_quality=data_quality,
        h2h=h2h,
    )

    if output_dir is None:
        output_dir = _artefact_dir(client_name)
    output_dir.mkdir(parents=True, exist_ok=True)

    filename = f"competitor_{_slug(competitor_name)}.html"
    out_path = output_dir / filename
    out_path.write_text(html, encoding="utf-8")
    logger.info("Competitor profile written to %s", out_path)

    import storage as _storage
    import db as _db
    client_slug_val = _slug(client_name) if client_name else "shared"
    storage_path = f"competitors/{client_slug_val}/{filename}"
    if not _storage.upload_file(str(out_path), storage_path, "text/html"):
        logger.warning("Storage upload failed for %s", filename)
    _db.save_output(
        "competitor_profile", date.today(), filename,
        content=html, storage_path=storage_path,
        client_slug=client_slug_val,
        client_name=client_name,
    )

    return out_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
    p = argparse.ArgumentParser(description="Generate a competitor intelligence profile")
    p.add_argument("competitor_name")
    p.add_argument("--client", required=True, help="Client firm name (authenticated user)")
    p.add_argument("--sector", required=True, help="Sector context e.g. 'government cybersecurity'")
    p.add_argument("--output-dir", default=None)
    args = p.parse_args()

    out = generate_competitor_profile(
        args.competitor_name,
        client_name=args.client,
        sector_context=args.sector,
        output_dir=Path(args.output_dir) if args.output_dir else None,
    )
    print(f"Generated: {out}")
