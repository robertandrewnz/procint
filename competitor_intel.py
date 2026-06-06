"""
Layer 2 — Competitor intelligence.

Given a company name and a specific notice ID, generates a competitor
assessment covering:
  - Who else is likely bidding (from bidder_pool)
  - Their win history in this sector (from contract_awards)
  - Incumbent detection (did the agency last award this type of contract
    to one of the likely bidders?)
  - Differentiation opportunities for the named company
  - Claude-generated competitive assessment narrative

Results are ephemeral (not stored) — generated on demand by the Layer 2
pipeline for the top-scored notices.
"""
import json
import logging
from typing import Optional

import anthropic

import config
import db
import organisations as orgs

logger = logging.getLogger(__name__)

_client: Optional[anthropic.Anthropic] = None


def _get_claude() -> anthropic.Anthropic:
    global _client
    key = config.ANTHROPIC_API_KEY
    if _client is None:
        _client = anthropic.Anthropic(api_key=key)
    return _client


# ── Data assembly ─────────────────────────────────────────────────────────────

def _get_likely_bidders(notice_id: str) -> list[dict]:
    return db.fetchall(
        """
        SELECT bp.firm_name, bp.size, bp.strategic_importance,
               bp.intelligence_maturity, bp.relevance_score
          FROM bidder_pool bp
         WHERE bp.notice_id = %s
         ORDER BY bp.relevance_score DESC NULLS LAST
         LIMIT 10
        """,
        (notice_id,),
    )


def _get_sector_win_history(sector: str, bidder_names: list[str]) -> list[dict]:
    """For each likely bidder, count their wins in this sector."""
    if not bidder_names:
        return []
    placeholders = ", ".join(["%s"] * len(bidder_names))
    return db.fetchall(
        f"""
        SELECT o.name, COUNT(*) as wins,
               SUM(ca.contract_value) as total_value,
               MAX(ca.award_date) as last_win
          FROM contract_awards ca
          JOIN organisations o ON o.org_id = ca.supplier_org_id
         WHERE ca.sector_tag = %s
           AND o.name IN ({placeholders})
         GROUP BY o.org_id, o.name
         ORDER BY wins DESC
        """,
        [sector] + bidder_names,
    )


def _detect_incumbent(agency_org_id: Optional[int], sector: str) -> Optional[str]:
    """
    Detect the most likely incumbent supplier for this agency in this sector.
    Returns supplier name or None.
    """
    if not agency_org_id:
        return None
    row = db.fetchone(
        """
        SELECT o.name, COUNT(*) as wins
          FROM contract_awards ca
          JOIN organisations o ON o.org_id = ca.supplier_org_id
         WHERE ca.agency_org_id = %s
           AND ca.sector_tag = %s
         GROUP BY o.org_id, o.name
         ORDER BY wins DESC
         LIMIT 1
        """,
        (agency_org_id, sector),
    )
    return row["name"] if row else None


# ── Claude assessment ─────────────────────────────────────────────────────────

_COMP_SYSTEM = (
    "You are a senior procurement strategy adviser for a New Zealand advisory firm. "
    "Respond ONLY with valid JSON — no preamble, no markdown fences."
)

_COMP_PROMPT = """Generate a competitor intelligence assessment for this NZ government procurement notice.

The firm we are advising: {company_name}

Notice:
  Title: {title}
  Agency: {agency}
  Sector: {sector}
  Value band: {value_band}
  Close date: {close_date}

Likely competing firms (from bidder analysis):
{competitors_text}

Sector win history (from contract awards database):
{win_history_text}

Incumbent supplier (if known): {incumbent}

Return a JSON object with exactly these keys:
"competitive_landscape": 2-3 sentences on who the main competitors are and their relative strengths for this specific notice.
"incumbent_assessment": 1-2 sentences on whether an incumbent exists and how strong their position is.
"win_probability_factors": A list of 3-4 strings, each a specific factor that will determine who wins this contract.
"differentiation_opportunities": 2-3 sentences on specific ways {company_name} could differentiate from the field given this agency's known preferences and the nature of this notice.
"intelligence_confidence": "high", "medium", or "low" — based on how much contract award data you have for this sector and agency."""


def assess_competitors(
    company_name: str,
    notice_id: str,
) -> Optional[dict]:
    """
    Generate a competitor intelligence assessment for a notice.
    Returns assessment dict or None if insufficient data.
    """
    # Fetch notice data
    notice = db.fetchone(
        """
        SELECT r.notice_id, r.title, r.agency, r.close_date,
               p.sector_tag, p.value_band
          FROM raw_notices r
          JOIN parsed_notices p ON p.notice_id = r.notice_id
         WHERE r.notice_id = %s
        """,
        (notice_id,),
    )
    if not notice:
        logger.warning("Notice %s not found", notice_id)
        return None

    sector = notice.get("sector_tag") or "other"
    agency_name = notice.get("agency") or ""

    # Resolve agency org
    agency_org_id = orgs.resolve_alias(agency_name)

    # Get competing bidders (exclude the company we're advising)
    bidders = _get_likely_bidders(notice_id)
    competitors = [b for b in bidders if b["firm_name"] != company_name]
    bidder_names = [b["firm_name"] for b in competitors]

    # Win history in sector
    win_history = _get_sector_win_history(sector, bidder_names)

    # Incumbent
    incumbent = _detect_incumbent(agency_org_id, sector)

    # Format for prompt
    competitors_text = "\n".join(
        f"  - {b['firm_name']} ({b.get('size','?')}, "
        f"strategic importance: {b.get('strategic_importance','?')}, "
        f"relevance score: {b.get('relevance_score',0):.3f})"
        for b in competitors[:6]
    ) or "  No bidder data available"

    if win_history:
        win_history_text = "\n".join(
            f"  - {w['name']}: {w['wins']} win(s) in {sector} sector"
            + (f", last win: {w['last_win']}" if w.get("last_win") else "")
            for w in win_history
        )
    else:
        win_history_text = "  No sector win history in database yet (will populate as awards are scraped)"

    client = _get_claude()
    prompt = _COMP_PROMPT.format(
        company_name=company_name,
        title=notice.get("title") or "Unknown",
        agency=agency_name or "Unknown",
        sector=sector,
        value_band=notice.get("value_band") or "unknown",
        close_date=str(notice.get("close_date") or "Unknown"),
        competitors_text=competitors_text,
        win_history_text=win_history_text,
        incumbent=incumbent or "Unknown / not yet in database",
    )

    try:
        message = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=600,
            system=_COMP_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        result = json.loads(raw)
        result["notice_id"] = notice_id
        result["company_name"] = company_name
        result["incumbent"] = incumbent
        return result
    except Exception as exc:
        logger.warning("Competitor assessment failed for %s / %s: %s",
                       company_name, notice_id, exc)
        return None


def run_competitor_assessments(
    company_name: str,
    notice_ids: Optional[list[str]] = None,
) -> list[dict]:
    """
    Run competitor assessments for the top-N high-priority notices.
    If notice_ids is None, uses the top MAX_COMPETITOR_ASSESSMENTS notices by score.
    Returns list of assessment dicts.
    """
    if notice_ids is None:
        rows = db.fetchall(
            """
            SELECT notice_id FROM scored_notices
             WHERE composite_score >= %s
             ORDER BY composite_score DESC
             LIMIT %s
            """,
            (config.PRIORITY_THRESHOLD, config.MAX_COMPETITOR_ASSESSMENTS),
        )
        notice_ids = [r["notice_id"] for r in rows]

    logger.info(
        "Running competitor assessments for %s across %d notices",
        company_name, len(notice_ids),
    )

    results = []
    for nid in notice_ids:
        assessment = assess_competitors(company_name, nid)
        if assessment:
            results.append(assessment)

    logger.info("Competitor assessments complete: %d results", len(results))
    return results
