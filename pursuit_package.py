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
from datetime import date, datetime, timedelta
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
               r.close_date, r.description, r.overview_text, r.category_raw, r.estimated_value,
               p.sector_tag, p.value_band, p.days_until_close,
               p.geographic_scope, p.evaluation_criteria, p.contract_duration,
               p.estimated_value_min, p.estimated_value_max,
               p.briefing_date, p.questions_deadline,
               p.registration_deadline, p.procurement_stage,
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


def _extract_doc_incumbent(
    extra_docs: list[dict], agency: str, notice_title: str
) -> Optional[str]:
    """
    Scan uploaded tender documents for named technology vendors, platforms, and systems.
    Returns ALL identified technology relationships as a structured list, or None.
    """
    if not extra_docs:
        return None
    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        doc_parts = []
        for doc in extra_docs[:3]:
            text = (doc.get("text") or "").strip()[:4000]
            doc_parts.append(
                f"--- {doc.get('file_name', 'Document')} ---\n{text}"
            )
        docs_block = "\n\n".join(doc_parts)
        query = (
            f"Read these tender documents from {agency} (tender: '{notice_title}') "
            f"and identify every named technology product, software system, hardware device, "
            f"platform, cloud service, or vendor mentioned anywhere in the documents.\n\n"
            f"Include ALL of the following types if present:\n"
            f"- Recording or audio management systems (e.g. For The Record, Olympus)\n"
            f"- Transcription or dictation software (e.g. Dragon NaturallySpeaking, Nuance)\n"
            f"- Identity or access management systems (e.g. Microsoft Entra ID, Azure AD)\n"
            f"- Case management, court management, or document management platforms\n"
            f"- Any other named vendor, software product, or hardware device\n\n"
            f"Do NOT restrict to only systems being replaced. List every named technology "
            f"relationship you find — even products mentioned in passing as existing tools.\n\n"
            f"For each product found, state:\n"
            f"- Product/system name\n"
            f"- Vendor or parent company\n"
            f"- Brief context (what it's used for at this agency)\n\n"
            f"Format each as a bullet: '• [Product] by [Vendor] — [context]'\n\n"
            f"Only respond with 'No incumbent identified.' if the documents contain "
            f"ZERO named technology products or vendors.\n\n"
            f"{docs_block}"
        )
        msg = client.messages.create(
            model=config.CLAUDE_MODEL_L3,
            max_tokens=600,
            messages=[{"role": "user", "content": query}],
        )
        result_parts = [
            block.text.strip()
            for block in msg.content
            if hasattr(block, "text") and block.text
        ]
        result = " ".join(result_parts).strip()
        if result and "no incumbent identified" not in result.lower() and len(result) > 20:
            logger.info("_extract_doc_incumbent found for %s: %s", agency, result[:80])
            return result[:600]
    except Exception as exc:
        logger.warning("_extract_doc_incumbent failed for %s: %s", agency, exc)
    return None


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


def _get_national_market_context(sector: str) -> dict:
    """
    National market data for this sector — last 3 years, all agencies.
    Provides baseline for: how many similar contracts exist nationally,
    typical contract size, and who dominates the sector across all buyers.
    """
    three_years_ago = (date.today() - timedelta(days=3 * 365)).isoformat()

    stats = db.fetchone(
        """
        SELECT COUNT(DISTINCT n.rfx_id) AS total_contracts,
               AVG(n.awarded_amount)     AS avg_value,
               SUM(n.awarded_amount)     AS total_value
          FROM mbie_award_notices n
          JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
         WHERE n.is_awarded
           AND c.sector_tag = %s
           AND n.awarded_date >= %s
        """,
        (sector, three_years_ago),
    )

    top3 = db.fetchall(
        """
        SELECT s.business_name, COUNT(DISTINCT n.rfx_id) AS wins,
               SUM(n.awarded_amount) AS total_value
          FROM mbie_award_notices n
          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
          JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
         WHERE n.is_awarded
           AND c.sector_tag = %s
           AND n.awarded_date >= %s
         GROUP BY s.business_name
         ORDER BY wins DESC
         LIMIT 3
        """,
        (sector, three_years_ago),
    )

    return {
        "total_contracts": int(stats["total_contracts"] or 0) if stats else 0,
        "avg_value": float(stats["avg_value"] or 0) if stats else 0,
        "total_value": float(stats["total_value"] or 0) if stats else 0,
        "top3_national": [dict(r) for r in top3],
    }


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
    return f"based on {count:,} published government contract award records (2014–2025)"


# ── Claude synthesis ──────────────────────────────────────────────────────────

_PURSUIT_SYSTEM = """You are a senior procurement strategy adviser at a boutique advisory firm in New Zealand.
You are preparing an intelligence package for a client considering bidding on a government contract.

GROUNDING RULES:
- Your analysis must be grounded strictly in the data provided — do not invent firms, award values, or procurement history not present in the context.
- When referencing contract award data, use neutral language: 'government contract award records', 'historical award data', 'published contract records', or 'contract award history'. Do NOT write 'MBIE data shows', 'according to MBIE', 'no MBIE record', or 'MBIE-recorded wins'. MBIE is a data source, not an authority — frame findings as market observations, not disqualifications.
- When a FIRM PROFILE section is provided, use it as the primary source of truth about the client's capabilities, history, and track record. Treat it as verified background.
- When NO FIRM PROFILE is provided and no client history appears in the award data, you MUST state explicitly: "No recorded government contract history found for [client name] in government contract award data." Do not speculate about capabilities the client may have. Base win positioning on opportunity structure only.

WIN POSITION LOGIC — follow this strict hierarchy:
Step 1 — Assess the opportunity structure FIRST, before considering the client:
  - Incumbent retention rate for this agency from award history
  - Whether an incumbent is detectable from the award data
  - Days to close vs contract complexity
  - Whether evaluation criteria are published or absent
  - Signs of pre-engagement (vague description, very short window, two-stage with imminent Stage 1 close)
  If structural factors alone indicate this is unlikely to go to a new entrant, state this as the base position.
Step 2 — Adjust for client-specific factors ONLY after establishing structural difficulty:
  - If award history exists for the client firm name → cite it explicitly
  - If no history exists → state this explicitly and base positioning on opportunity structure only
  - Never assume capabilities the client may or may not have
Step 3 — Separate the verdict clearly:
  - "This opportunity is [Competitive/Challenging] structurally because [specific reasons from agency data]"
  - "For [client name] specifically, [what is known / what is unknown about their position]"

DIRECTNESS AND SPECIFICITY:
- Be direct. Every claim must reference actual data from the agency profile or competitive landscape provided.
- Do not hedge with phrases like 'may', 'might', 'could potentially' — make a call and explain your reasoning.
- If data is insufficient to make a claim, say so explicitly rather than speculating.
- The client is a senior BD professional — write at that level.

NARRATIVE CONSISTENCY RULE: if go_nogo is "CONDITIONAL GO", the go_nogo_rationale MUST describe a credible path to success. Never use language that dismisses the client when the recommendation is GO or CONDITIONAL GO.

BRIEFING DATE RULE: If a briefing date is provided and it is in the past, state that the briefing has already occurred — do NOT flag it as a missed opportunity or a negative signal unless there is specific evidence the client was excluded. Past briefings are routine; late entrants can still engage via questions or direct contact.

QUESTIONS DEADLINE RULE: If a questions_deadline is provided, name it explicitly as an action item: "Questions close [date] — any clarifications must be submitted before this date." Do not omit or generalise it as a vague deadline.

DATA COMPLETENESS RULE: Where overview_text or other notice fields are absent or sparse, frame the gap as a data limitation rather than a definitive absence. Write "The notice does not include [X] — this should be confirmed with the agency directly" rather than asserting that [X] does not exist or is not required.

COMPETITIVE NARRATIVE RULE: Generate competitive_narrative first as a full analytical narrative. Then generate ach_table as a structured formalisation of the hypotheses you identified in the narrative. Every hypothesis in the ACH table must trace back to something mentioned in the narrative. Do not introduce new hypotheses in the table that weren't flagged in the narrative. The ACH table must be internally consistent with the narrative.

AUTHENTICATED DOCUMENTS RULE: When an "=== AUTHENTICATED TENDER DOCUMENTS ===" section is present, treat it as the primary source of truth about the tender scope, evaluation criteria, timeline, and requirements. It supersedes any inferences from the public notice overview. Prioritise and synthesise the document content throughout your analysis — especially in the executive summary, evaluation cone, and recommended actions. Reference specific document content where it strengthens or changes the assessment. If the documents reveal information not present in the public notice (e.g. detailed evaluation weighting, mandatory site visits, specific technical requirements), highlight this in the analysis.

FULL ANALYSIS VERDICT RULE: When authenticated tender documents are present AND they contain evaluation criteria, weightings, mandatory requirements, or pre-conditions — you MUST re-evaluate go_nogo, strategic_fit_score, and the competitive position using those specific criteria. Do NOT anchor the verdict to the Stage 1 "opportunity structure only" assessment. Specifically: (1) go_nogo_rationale must reference specific document content — evaluation criteria weightings, mandatory requirements, or pre-conditions found in the documents; (2) client_specific_factors must assess the client against the documented evaluation criteria, not just state "no MBIE history"; (3) Do NOT use the phrase "assessed on opportunity structure only" when documents specify evaluation criteria. The authenticated documents ARE the basis for a more informed verdict — use them.

TENDER TYPE RULE: The "Tender Strategic Posture" field tells you what kind of procurement this is. Adapt ALL sections accordingly:
- "Market shaping" (RFI/NOI/market research): This is NOT a live bid. Set go_nogo to "MARKET ENGAGEMENT". Frame go_nogo_rationale as a market intelligence recommendation, not a bid decision. Do NOT generate urgency language, teaming time pressure, or "limited time to respond" red flags. executive_summary must NOT use GO/NO-GO language. In risk_register and red_flags, suppress procurement-timeline and bid-submission risks — flag information gaps and engagement risks instead. recommended_actions must focus entirely on: attending market engagement events, submitting a well-positioned RFI response to shape requirements, building agency awareness and relationships — NOT bid preparation, proposal logistics, or teaming agreements. strategic_fit_score should reflect engagement value, not win probability. opportunity_structure_assessment should assess market shaping opportunity, not competitive bid dynamics.
- "Qualification stage" (ROI/EOI): The goal is to make the shortlist, not win the contract. Frame go_nogo around whether the EOI/ROI is worth pursuing. recommended_actions should focus on EOI/ROI quality, capability demonstration, and relationship building — not full bid preparation.
- "Early signal" (Advance Notice): No response required yet. Set go_nogo to "CONDITIONAL GO" reflecting pipeline readiness. Do NOT generate urgency flags. recommended_actions should focus on intelligence gathering, monitoring, and early positioning.
- "Live bid" (RFP/RFT/RFQ/panel): Standard analysis applies — the current framing is correct.

Respond ONLY with a valid JSON object, no preamble, no markdown fences."""

_PURSUIT_PROMPT = """Prepare a pursuit intelligence package for:

CLIENT: {client_name}
{firm_profile_section}NOTICE: {title}
AGENCY: {agency}
SECTOR: {sector}
VALUE: {value_band}
CLOSE DATE: {close_date} ({days_until_close} days)
GETS URL: {source_url}

=== OPPORTUNITY CONTEXT ===
Procurement Stage: {procurement_stage}
Tender Strategic Posture: {tender_posture}
Briefing Date: {briefing_date}
Questions Deadline: {questions_deadline}
Registration Deadline: {registration_deadline}
Evaluation criteria (stated): {evaluation_criteria}
Contract duration: {contract_duration}
Geographic scope: {geographic_scope}

=== NOTICE OVERVIEW ===
{overview}

=== AI ENRICHMENT (Layer 1) ===
Summary: {enrichment_summary}
Evaluation weighting (inferred): {evaluation_weighting}
Red flags identified: {red_flags}
Strategic framing: {strategic_framing}

=== CLIENT HISTORY (government contract award records) ===
Client wins in {sector} sector (all time): {client_sector_wins} contracts, {client_sector_value} total
Client wins with {agency} specifically: {client_agency_wins} contracts
Client's sectors of proven capability: {client_sectors}
Note: {client_data_note}

=== COMPETITIVE LANDSCAPE ({mbie_citation}) ===
Historical winners of similar contracts ({sector} sector, same agency or similar agencies):
{competitors_text}

=== INCUMBENT INTELLIGENCE ===
Current technology system/provider for {agency} ({sector} sector): {incumbent_text}

=== AGENCY PROFILE (contract award records) ===
Total recorded awards: {agency_total_awards} contracts worth {agency_total_value}
Average contract value: {agency_avg_value}
Unique suppliers engaged: {agency_unique_suppliers}
Awards in {sector} sector specifically: {agency_sector_awards}
Top procurement sectors: {agency_top_sectors}

=== NATIONAL MARKET CONTEXT ({sector} sector, last 3 years, all NZ agencies) ===
Similar contracts awarded nationally: {national_total_contracts} contracts ({national_total_value} combined)
Average contract value nationally: {national_avg_value}
Top 3 suppliers nationally in this category:
{national_top3_text}
Most frequent supplier to {agency} in {sector}: {most_frequent_agency_supplier}

=== PATTERN FLAGS ===
{flags_text}
{agency_plan_intel}{authenticated_docs}
Be specific — use the actual data provided above. Do not be generic. Tone: direct and analytical, written for a senior BD professional.

Return a JSON object with EXACTLY these keys:

"executive_summary": Two paragraphs. First: structural assessment of the opportunity ONLY — what it is, its strategic significance, and the structural GO/NO-GO signal (incumbent retention rate, pre-engagement signs, days to close vs complexity, evaluation criteria published/absent, agency award history). Do NOT mention {client_name} anywhere in paragraph 1. Second: open with EXACTLY one of the following data statements — if client has recorded history: "MBIE award data shows [N] recorded government contract wins for {client_name}, totalling $[X]M. The most recent win was [date] with [agency] for [description]." — if client has NO recorded history: "No government contract award history is held for {client_name} in the MBIE dataset. The client position assessment below is based on opportunity structure only — {client_name} should apply their own knowledge of their capabilities, relationships, and pipeline context to this assessment." Then continue with client-specific positioning factors. Never infer capabilities or absence of capabilities from missing award data.

"strategic_fit_score": Integer 1-10. Base this on: client's sector capability, prior agency relationship, competitive positioning.

"opportunity_structure_assessment": One paragraph assessing the opportunity STRUCTURALLY before considering the client. Cover: (1) incumbent retention rate and whether an incumbent is detectable; (2) days to close vs contract complexity; (3) whether evaluation criteria are published or absent; (4) any signs of pre-engagement. Start with: "Structurally, this opportunity is [Competitive/Challenging] because..."

"client_specific_factors": One paragraph on client-specific positioning ONLY. If award history exists for {client_name}, cite it. If no history exists, state explicitly: "No recorded government contract history found for {client_name} in government contract award data." Base positioning on opportunity structure if no history is available. Go conditions must be achievable actions, not assumptions about unknown capabilities.

"win_probability_rationale": Combine the above into a two-paragraph assessment. Paragraph 1 = opportunity structure (copy from opportunity_structure_assessment). Paragraph 2 = client-specific factors (copy from client_specific_factors). Separate these clearly.

"go_nogo": Exactly one of "GO", "CONDITIONAL GO", "NO GO", or "MARKET ENGAGEMENT" — use MARKET ENGAGEMENT only when Tender Strategic Posture is "Market shaping" (RFI/NOI); these are not live bids

"go_nogo_rationale": Two sentences. Decisive recommendation with primary reason and key condition if conditional.

"centre_of_gravity": Object with exactly these keys:
  "factor": One sentence naming the single factor that will most determine the outcome of this procurement — not a list, one dominant factor.
  "why_it_dominates": 2-3 sentences explaining why this factor outweighs all others based on agency history and notice characteristics. Reference specific data.
  "strategic_implication": One sentence on what this means for how {client_name} should allocate bid resources.

"competitive_narrative": 3-4 paragraph analytical narrative covering: (1) the realistic competitive field for this specific contract — not a generic sector list, name actual firms where data exists; (2) the incumbent risk assessment — CRITICAL RULE: distinguish clearly between (a) a named incumbent supplier identified from MBIE award data or web research for THIS specific contract type, and (b) the agency's general repeat supplier rate (agency loyalty pattern). If no named incumbent is identified for this contract type, do not use the agency loyalty rate as a proxy for incumbent risk — treat it as a market entry barrier signal only, not evidence of a specific entrenched supplier. If incumbent intelligence names a parent company and/or NZ distributor (e.g. "For The Record by Tyler Technologies, NZ distributor: Vega NZ"), name all entities explicitly in this section — do not collapse to just the product name; (3) the key competitive dynamic — what will actually determine who wins and where genuine differentiation opportunity exists; (4) any sector-specific competitive patterns relevant to this contract type. Be specific — not "several firms may bid" but analytical conclusions with reasoning.

"ach_table": Array of 3-4 objects, each representing a hypothesis about WHO WINS THIS PROCUREMENT AND WHY — modelling the competitive field, not the client's success. Each object has:
  "hypothesis": String framing a competitive outcome (e.g. "Incumbent retains contract on relationship strength", "New entrant displaces incumbent by competing on total cost of ownership", "Panel shortlist favours established IT panel suppliers"). CRITICAL: Do NOT frame any hypothesis as "{client_name} successfully [does X]" or name {client_name} as the subject of a hypothesis. The ACH models who wins in the market — {client_name} can read where they fit, but must not be named as a hypothesis subject.
  "evidence_for": Array of 2-3 strings — specific evidence supporting this hypothesis from the data provided
  "evidence_against": Array of 1-2 strings — specific evidence working against this hypothesis
  "probability": Exactly one of "High", "Medium", or "Low"
  Plus one final object: {{"hypothesis": "Most discriminating factor", "evidence_for": ["The single piece of intelligence that would most change the probability assessment — one specific data point or event"], "evidence_against": [], "probability": "N/A"}}
  CRITICAL: Every hypothesis must trace back to something mentioned in competitive_narrative. Do not introduce hypotheses here that weren't flagged in the narrative. Internal consistency is mandatory.

"incumbent_assessment": One paragraph. How entrenched the incumbent is and what it would take to displace them. If the incumbent intelligence names a parent company and NZ distributor (e.g. "For The Record by Tyler Technologies, distributed by Vega NZ"), identify all named entities and the competitive dynamics they create (e.g. a foreign-owned system with a local distributor creates different displacement dynamics than a locally-owned solution). If no named incumbent exists, state that clearly — do not characterise agency loyalty statistics as incumbent entrenchment.

"agency_insights": One to two paragraphs. What this buyer values, how they procure, and what the data reveals about their evaluation behaviour.

"evaluation_cone": Object representing three evaluation weighting scenarios. Keys:
  "label": String — "Inferred — no criteria published" if no criteria stated, otherwise "Estimated from notice and agency patterns"
  "conservative": Object with "scenario" (string — standard NZ government practice weighting, brief description) and "rationale" (string — why this is the floor scenario)
  "most_likely": Object with "scenario" (string — adjusted for this agency's actual award patterns and notice characteristics) and "rationale" (string — reasoning drawn from agency data provided)
  "optimistic": Object with "scenario" (string — weighting most favourable for {client_name}) and "rationale" (string — why this would favour the client and under what conditions)

"pursuit_positioning": Array of exactly 4 objects, each with "title" (short label) and "detail" (2-3 sentences). The 4 objects MUST address these structural questions in order: (1) What does a credible new entrant need to be competitive on this opportunity — what threshold capabilities, relationships, or proof points are required regardless of who is bidding? (2) What single piece of intelligence would most change a go/no-go decision for this opportunity, and how could a bidder realistically obtain it before close? (3) If the primary pursuit path is not viable, what is the best alternative path — a different lot, a subcontract position, a partnership, or a different timing window? (4) What does winning this contract enable strategically — what doors does it open, what capability does it prove, what relationships does it build? Do NOT assess {client_name}'s specific capabilities. Do NOT give advice premised on capabilities you cannot verify from the data provided. All four answers must be grounded in the opportunity structure, agency patterns, and competitive landscape data.

"risk_register": Array of exactly 5 objects, each with "risk" (label), "likelihood" (High/Medium/Low), "impact" (High/Medium/Low), "mitigation" (1-2 sentences).

"recommended_actions": Array of 4-6 objects, each with "action" (imperative sentence), "timeframe" (e.g. "Today", "Within 48 hours", "Week 1", "Before close"), "priority" (Critical/High/Medium)."""


# ── Re-tender signal detection ────────────────────────────────────────────────

_RETENDER_SIGNALS: frozenset = frozenset({
    "returning to market", "return to market", "back to market",
    "prior external provider", "prior provider", "existing external provider",
    "existing arrangement", "existing contract", "existing agreement",
    "current arrangement", "current provider", "current supplier",
    "incumbent provider", "incumbent supplier",
    "expiring contract", "contract expir", "contract renewal",
    "previously provided", "has been providing", "currently provided",
    "existing system", "existing solution", "existing platform",
    "prior contract", "coming to an end", "new arrangement",
    "re-tender", "retender", "go to market",
})

# Sector-specific fallback hints for when direct contract searches return nothing.
# These are appended to the prompt to guide broadening searches.
_SECTOR_FALLBACK_HINTS: dict = {
    "defence": (
        "\n\nSECTOR FALLBACK — DEFENCE/MILITARY:\n"
        "If direct contract-name searches return nothing, try these named-contractor searches:\n"
        "- 'Serco {agency} contract New Zealand' / 'Serco {agency} training'\n"
        "- 'Babcock {agency} contract New Zealand'\n"
        "- 'L3 Technologies {agency} New Zealand'\n"
        "- '{agency} training services contractor New Zealand'\n"
        "- '{agency} annual report supplier New Zealand'\n"
        "Any confirmed relationship between a named contractor and {agency} for the relevant "
        "service category is a MEDIUM-confidence incumbent signal.\n"
    ),
    "FM": (
        "\n\nSECTOR FALLBACK — FACILITIES MANAGEMENT:\n"
        "If direct searches return nothing, try:\n"
        "- 'Programmed {agency} facilities contract New Zealand'\n"
        "- 'ISS Facility Services {agency} New Zealand'\n"
        "- 'Ventia {agency} contract New Zealand'\n"
        "- 'Downer {agency} facilities New Zealand'\n"
        "Any confirmed FM contract between a named contractor and {agency} is a MEDIUM signal.\n"
    ),
    "ICT": (
        "\n\nSECTOR FALLBACK — ICT / DIGITAL SERVICES:\n"
        "If direct service-name searches return nothing, also search for the TECHNOLOGY ECOSYSTEM:\n"
        "What existing systems or infrastructure does {agency} use that this service would "
        "integrate with or replace?\n"
        "- For audio/transcription/speech services at courts or justice agencies:\n"
        "  Search 'For The Record {agency} New Zealand' and 'FTR court recording {agency} NZ'\n"
        "  (For The Record / Tyler Technologies provides court audio management in NZ courts)\n"
        "- For data/analytics services: Search '{agency} data platform New Zealand provider'\n"
        "- For cloud/infrastructure: Search '{agency} cloud provider New Zealand contract'\n"
        "- Try: 'Datacom {agency} contract', 'Spark {agency} contract', 'Fujitsu {agency} NZ'\n"
        "Name any related infrastructure provider found, noting they provide a related (not identical) service.\n"
    ),
    "health": (
        "\n\nSECTOR FALLBACK — HEALTH TECHNOLOGY:\n"
        "If direct searches return nothing, try:\n"
        "- 'Orion Health {agency} contract New Zealand'\n"
        "- 'InterSystems {agency} New Zealand'\n"
        "- 'Accenture {agency} health New Zealand'\n"
        "- '{agency} patient management system provider New Zealand'\n"
    ),
    "advisory": (
        "\n\nSECTOR FALLBACK — ADVISORY / CONSULTING:\n"
        "If direct searches return nothing, try:\n"
        "- 'Deloitte {agency} contract New Zealand'\n"
        "- 'KPMG {agency} New Zealand advisory'\n"
        "- 'PwC {agency} contract New Zealand'\n"
        "- 'EY {agency} New Zealand advisory'\n"
        "- 'McKinsey {agency} New Zealand'\n"
    ),
}


def _web_search_incumbent(
    agency: str,
    sector: str,
    notice_title: str,
    notice_text: str = "",
) -> str:
    """
    Run multiple targeted search strategies to identify the named current contract holder
    (incumbent) for this notice.

    Strategies:
      1. Contract award search — multiple query variations for award announcements.
      2. Incumbent/current provider reference search — broader phrasing.
      3. Technology ecosystem — for ICT/health/advisory: find related infrastructure
         even when no direct incumbent exists for this exact service.
      4. Notice text signals — if re-tender language detected, extract named providers.
      5. Sector fallback — when all else fails, try known major NZ contractors for sector.

    Always returns a non-empty string in Format A, B, or C.
    """
    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

        # Always use the notice title as service descriptor — more specific than sector label.
        service_desc = notice_title.strip() if notice_title.strip() else f"{sector} services"

        # Detect re-tender signals in the notice text
        notice_lower = (notice_text or "").lower()
        retender_detected = any(sig in notice_lower for sig in _RETENDER_SIGNALS)

        logger.info(
            "INCUMBENT search START: agency=%r service=%r retender_detected=%s sector=%s",
            agency, service_desc[:70], retender_detected, sector,
        )
        logger.info(
            "INCUMBENT S1 queries: %r | %r | %r",
            f"{agency} {service_desc} contract awarded New Zealand",
            f"{agency} {service_desc} supplier announcement",
            f"{agency} {service_desc} provider New Zealand",
        )
        logger.info(
            "INCUMBENT S2 queries: %r | %r | %r",
            f"{agency} {service_desc} incumbent",
            f"current {service_desc} provider {agency} New Zealand",
            f"{agency} annual report {service_desc} New Zealand",
        )

        # Strategy 4 block — re-tender signals in notice text
        retender_block = ""
        if retender_detected:
            notice_excerpt = notice_text[:800].strip()
            logger.info("INCUMBENT S4: re-tender language detected — including notice text signals")
            retender_block = (
                f"\n\nSTRATEGY 4 — NOTICE TEXT SIGNALS (re-tender detected):\n"
                f"This notice contains language suggesting an expiring or existing arrangement. "
                f"Relevant excerpt:\n\"\"\"\n{notice_excerpt}\n\"\"\"\n"
                f"— Extract every named supplier, system, or provider mentioned above.\n"
                f"— For each named entity, search: '[entity name] {agency} contract'\n"
                f"— Also search: '{agency} {service_desc} existing contract New Zealand'\n"
                f"— Any named entity in re-tender text is a strong incumbent candidate.\n"
            )
        elif notice_text.strip():
            notice_excerpt = notice_text[:400].strip()
            retender_block = (
                f"\n\nNotice context (passive scan for named providers):\n"
                f"\"\"\"\n{notice_excerpt}\n\"\"\"\n"
                f"If this text names any existing provider, system, or arrangement, "
                f"search for that entity explicitly.\n"
            )

        # Sector fallback block
        raw_hint = _SECTOR_FALLBACK_HINTS.get(sector, "")
        sector_fallback_block = raw_hint.replace("{agency}", agency) if raw_hint else ""
        if sector_fallback_block:
            logger.info("INCUMBENT: sector fallback block added for sector=%s", sector)

        prompt = (
            f"Identify who currently holds or has most recently held the contract to provide "
            f"'{service_desc}' for {agency} in New Zealand.\n\n"
            f"This is a government procurement intelligence task. Run ALL strategies below. "
            f"Adapt the exact query phrasing as needed — these are starting points, not rigid strings. "
            f"Stop early ONLY if Strategy 1 returns HIGH-confidence explicit award evidence.\n\n"
            f"STRATEGY 1 — CONTRACT AWARD SEARCH:\n"
            f"Try at least 3 query variations:\n"
            f"- '{agency} {service_desc} contract awarded'\n"
            f"- '{agency} {service_desc} supplier announcement New Zealand'\n"
            f"- '{agency} {service_desc} provider New Zealand'\n"
            f"- 'GETS {agency} contract award {service_desc} New Zealand'\n"
            f"Also try shorter, broader service terms if the full title returns nothing "
            f"(e.g., '{agency} training contract' if notice title is 'Navigation Training Services').\n"
            f"Look for: press releases, official award announcements, NZ government supplier notices, "
            f"media coverage explicitly naming the awarded supplier.\n\n"
            f"STRATEGY 2 — INCUMBENT / CURRENT PROVIDER REFERENCE:\n"
            f"Try at least 3 query variations:\n"
            f"- '{agency} {service_desc} incumbent'\n"
            f"- 'current {service_desc} provider {agency} New Zealand'\n"
            f"- '{agency} annual report {service_desc}'\n"
            f"- '{agency} {service_desc} case study New Zealand'\n"
            f"Look for: vendor case studies, agency annual reports, NZ tech/sector media, "
            f"supplier websites, LinkedIn posts mentioning current delivery.\n\n"
            f"STRATEGY 3 — TECHNOLOGY ECOSYSTEM (always run for ICT, health, advisory, justice/court services):\n"
            f"If this service relates to audio, transcription, recording, or speech technology at a "
            f"justice/court/public safety agency:\n"
            f"- Search 'For The Record {agency} court recording New Zealand'\n"
            f"- Search 'FTR Tyler Technologies {agency} New Zealand'\n"
            f"More generally: what existing systems or infrastructure does {agency} use that this "
            f"service would integrate with or replace?\n"
            f"- Search '{agency} existing [related technology] system New Zealand'\n"
            f"Name any related-infrastructure provider found, noting it is related infrastructure "
            f"rather than a direct incumbent for this exact service."
            f"{retender_block}"
            f"{sector_fallback_block}\n\n"
            f"CONFIDENCE RULES:\n"
            f"— HIGH: A press release, official notice, or supplier announcement explicitly names "
            f"the contract holder for this specific service.\n"
            f"— MEDIUM: A vendor case study, annual report, agency website, or media coverage implies "
            f"the supplier relationship without an explicit contract award announcement.\n"
            f"— IMPORTANT: Do NOT output Format C unless you have exhausted ALL strategies above "
            f"and found absolutely no relevant suppliers, contractors, or infrastructure providers "
            f"connected to {agency} and this service category. Format B (market participants) is "
            f"always preferable to Format C (inconclusive). If even one relevant firm is found, use Format B.\n\n"
            f"MANDATORY RESPONSE FORMAT — use exactly one:\n\n"
            f"FORMAT A — named contract holder found (any confidence):\n"
            f"'Named incumbent: [Full Firm Name] — [2-3 sentences: what they provide, to whom, "
            f"and the evidence basis]. Confidence: [High/Medium]. Source: [source type].'\n\n"
            f"FORMAT B — no named holder but market or ecosystem context found:\n"
            f"'No named contract holder identified from public sources. "
            f"Firms active in this market or providing related infrastructure to {agency}: "
            f"[Firm 1] — [brief context]; [Firm 2] — [brief context].'\n\n"
            f"FORMAT C — use ONLY if all strategies exhausted with zero relevant results:\n"
            f"'Incumbent search inconclusive for {agency} / {service_desc}. "
            f"Searches ran but returned no identifiable suppliers or contract holders. "
            f"Manual research recommended: check {agency} annual report, supplier panel register, "
            f"or GETS historical awards for this service type.'\n\n"
            f"Do NOT output a generic refusal. Run the searches, synthesise the results, "
            f"and use one of the three formats above."
        )

        msg = client.messages.create(
            model=config.CLAUDE_MODEL_L3,
            max_tokens=1000,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 10}],
            messages=[{"role": "user", "content": prompt}],
        )

        logger.info(
            "INCUMBENT response: stop_reason=%r blocks=%d",
            msg.stop_reason, len(msg.content),
        )
        for i, block in enumerate(msg.content):
            btype = type(block).__name__
            if hasattr(block, "text"):
                logger.info("INCUMBENT block[%d] %s: %r", i, btype, block.text[:200])
            elif hasattr(block, "name"):
                inp = getattr(block, "input", {})
                logger.info("INCUMBENT block[%d] %s: name=%r input=%r",
                            i, btype, block.name, str(inp)[:200])

        result_parts = [
            block.text.strip()
            for block in msg.content
            if hasattr(block, "text") and block.text
        ]
        result = " ".join(result_parts).strip()

        if not result:
            fallback = (
                f"Incumbent search ran but returned no text response — "
                f"manual research recommended for {agency} / {service_desc}."
            )
            logger.warning("INCUMBENT: empty result for %s / %s", agency, service_desc[:60])
            return fallback

        logger.info(
            "INCUMBENT RESULT for %s / %s: %r",
            agency, service_desc[:40], result[:250],
        )
        return result[:1000]

    except Exception as exc:
        logger.warning("_web_search_incumbent failed for %s/%s: %s", agency, sector, exc)
        return (
            f"Incumbent search failed ({exc.__class__.__name__}) — "
            f"manual research required for {agency} / {notice_title}."
        )


def _extract_incumbent_firm_name(incumbent_research: str) -> Optional[str]:
    if not incumbent_research.startswith("Named incumbent:"):
        return None
    m = re.match(r"Named incumbent:\s*([^—\-]+)", incumbent_research)
    if not m:
        return None
    return m.group(1).strip() or None


def _store_incumbent_in_bidder_pool(notice_id: str, firm_name: str, evidence: str, sector: str) -> None:
    try:
        db.execute(
            """
            INSERT INTO bidder_pool
                (notice_id, firm_name, match_type, relevance_score, strategic_importance,
                 intelligence_maturity, reasoning, company_context, sector)
            VALUES (%s, %s, 'incumbent_identified', 0.95, 'high', 'strong', %s, %s, %s)
            ON CONFLICT (notice_id, firm_name) DO UPDATE SET
                match_type = 'incumbent_identified',
                relevance_score = 0.95,
                strategic_importance = 'high',
                intelligence_maturity = 'strong',
                reasoning = EXCLUDED.reasoning,
                company_context = EXCLUDED.company_context
            """,
            (notice_id, firm_name, evidence[:200], evidence[:500], sector),
        )
        logger.info("INCUMBENT stored in bidder_pool: notice=%s firm=%s", notice_id, firm_name)
    except Exception as exc:
        logger.warning("_store_incumbent_in_bidder_pool failed: %s", exc)


def _check_client_is_incumbent_web(
    client_name: str,
    agency: str,
    service_desc: str,
    incumbent_firm: str,
) -> bool:
    """
    Use a targeted web search to confirm whether the client is (or recently was) the incumbent.

    Called as a fallback when the first-word name-overlap fast-path doesn't match.
    Returns True only on positive confirmation — defaults to False on any uncertainty or error.
    """
    try:
        api_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        prompt = (
            f"Is '{client_name}' the current or recent incumbent contract holder for "
            f"'{service_desc}' at {agency} in New Zealand?\n\n"
            f"Context: an independent search identified '{incumbent_firm}' as the likely incumbent. "
            f"Check whether '{client_name}' and '{incumbent_firm}' are the same legal entity "
            f"(e.g. one is the parent company, trading name, subsidiary, or rebranded name of the other), "
            f"OR whether '{client_name}' independently holds this contract.\n\n"
            f"Run these searches:\n"
            f"1. '{client_name} {agency} contract New Zealand'\n"
            f"2. '{client_name} {incumbent_firm} same company New Zealand'\n"
            f"3. '{client_name} {service_desc} New Zealand government'\n\n"
            f"Respond with exactly one of:\n"
            f"YES — {client_name} holds/held this contract or is the same entity as {incumbent_firm}\n"
            f"NO — {client_name} is a different entity and does not appear to hold this contract\n"
            f"UNCERTAIN — search results are insufficient to determine"
        )
        msg = api_client.messages.create(
            model=config.CLAUDE_MODEL_L3,
            max_tokens=150,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            messages=[{"role": "user", "content": prompt}],
        )
        text_parts = [
            block.text.strip()
            for block in msg.content
            if hasattr(block, "text") and block.text
        ]
        result = " ".join(text_parts).strip().lower()
        is_incumbent = result.startswith("yes")
        logger.info(
            "CLIENT-IS-INCUMBENT web check: client=%r agency=%r result=%r → %s",
            client_name[:40], agency[:40], result[:80], "YES" if is_incumbent else "NO/UNCERTAIN",
        )
        return is_incumbent
    except Exception as exc:
        logger.warning("_check_client_is_incumbent_web failed for %s: %s", client_name, exc)
        return False


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
        competitors_text = "No government contract award records found for this agency/sector combination. Market data is limited."

    # Incumbent / current system (always from doc scan or web research — never MBIE data)
    inc_research = context.get("incumbent_research") or ""
    inc_from_docs = context.get("incumbent_from_docs", False)

    if inc_from_docs and inc_research:
        incumbent_text = (
            f"Existing technology relationships identified from uploaded tender documents:\n"
            f"{inc_research}\n\n"
            f"IMPORTANT: These are active technology products and vendor relationships at this agency. "
            f"In incumbent_assessment, treat each named vendor as having an existing relationship "
            f"that confers structural competitive advantage — vendors offering compatible, integrated, "
            f"or replacement products for any of these systems are better positioned than new entrants. "
            f"Name all identified vendors and systems explicitly. Do not characterise this as "
            f"'no named incumbent' — the document evidence identifies existing technology relationships."
        )
    elif inc_research.startswith("Named incumbent:"):
        # Web search returned a confident named contract holder
        incumbent_text = (
            f"Web research result — named contract holder identified:\n"
            f"{inc_research}\n\n"
            f"IMPORTANT: This is the identified incumbent. In competitive_narrative and "
            f"incumbent_assessment, name this entity explicitly. If the research names a parent "
            f"company and/or NZ distributor (e.g. 'For The Record by Tyler Technologies, NZ "
            f"distributor: Vega NZ'), name ALL entities — do not collapse to just the product name. "
            f"Assess displacement difficulty specifically: how entrenched is this incumbent, "
            f"what switching costs exist, and what would a new entrant need to do to displace them?"
        )
    elif inc_research.startswith("No named contract holder"):
        # Web search found market participants but no confirmed holder
        incumbent_text = (
            f"Web research result — no named contract holder confirmed:\n"
            f"{inc_research}\n\n"
            f"IMPORTANT: In incumbent_assessment, state explicitly that no named contract holder "
            f"was identified from public sources, then name the active market participants listed "
            f"above as firms that may hold or have held this contract. Do NOT conclude there is no "
            f"incumbent — this is an intelligence gap. Assess what it would mean if any of the "
            f"named firms held the contract, and recommend how the client could confirm incumbency "
            f"before close (e.g. direct agency enquiry, GETS historical awards, or tender debrief)."
        )
    elif inc_research and not inc_research.startswith("Incumbent search"):
        # Web search returned something that doesn't match either standard format — use as-is
        incumbent_text = (
            f"Web research result:\n"
            f"{inc_research}\n\n"
            f"IMPORTANT: In competitive_narrative and incumbent_assessment, name the parent company "
            f"and NZ distributor/reseller explicitly if they appear in the research above. "
            f"Do not omit corporate ownership or NZ distribution chain when the data is present."
        )
    else:
        # Search ran but returned an error or inconclusive result
        incumbent_text = (
            f"{inc_research or 'Incumbent search did not run.'}\n\n"
            f"IMPORTANT: In incumbent_assessment, state this as an intelligence gap — do NOT "
            f"assume there is no incumbent. Most government service contracts have an existing "
            f"provider. Recommend the client conduct direct enquiry with the agency or check "
            f"GETS historical award notices to identify the current holder before submitting."
        )

    logger.info("INCUMBENT _call_claude text prefix=%r", incumbent_text[:120])

    # Client history note
    ch = context.get("client_history", {})
    is_full_analysis = context.get("analysis_type") == "full"
    has_docs = bool(context.get("extra_docs"))
    if ch.get("sector_wins", 0) == 0:
        if is_full_analysis and has_docs:
            client_data_note = (
                f"No government contract award history is held for {context['client_name']} in the MBIE dataset. "
                "Authenticated tender documents are present — assess this client's position against "
                "the specific evaluation criteria, requirements, and pre-conditions in those documents. "
                "Do NOT anchor the verdict to opportunity structure only — use the document content as "
                "the basis for a specific, criteria-driven assessment."
            )
        else:
            client_data_note = (
                f"No government contract award history is held for {context['client_name']} in the MBIE dataset. "
                "This is a data absence, not a capability assessment. Win position must be assessed on opportunity structure only. "
                "Do not infer capabilities, sector experience, or relationships from the absence of MBIE records."
            )
    else:
        client_data_note = f"Client has {ch['sector_wins']} confirmed sector wins in MBIE data since {str(ch.get('sector_first_win', ''))[:4]}."

    # Defend/renewal framing when client is the identified incumbent
    if context.get("client_is_incumbent"):
        client_data_note += (
            " NOTE: The client appears to be the identified current incumbent for this contract. "
            "Frame the win position as a DEFEND/RENEWAL scenario, not a new entrant bid. "
            "The key risk is displacement by a challenger, not failing to win a new contract. "
            "go_nogo_rationale should address: how strong is the retention position? "
            "What challenger threats are credible? What does the client need to demonstrate to retain?"
        )

    # Firm profile (provided for demo/well-known clients to give Claude context)
    fp = context.get("firm_profile") or {}
    if fp:
        fp_lines = [f"=== FIRM PROFILE: {fp.get('name', context['client_name'])} ==="]
        if fp.get("description"):
            fp_lines.append(f"Background: {fp['description']}")
        if fp.get("staff"):
            fp_lines.append(f"Size: {fp['staff']} staff")
        if fp.get("location"):
            fp_lines.append(f"Location: {fp['location']}")
        if fp.get("strengths"):
            fp_lines.append(f"Credentials & differentiators: {fp['strengths']}")
        if fp.get("years_operating"):
            fp_lines.append(f"Years operating: {fp['years_operating']}")
        if fp.get("key_clients"):
            fp_lines.append(f"Key clients/contracts: {fp['key_clients']}")
        if fp.get("sector_focus"):
            fp_lines.append(f"Sector focus: {fp['sector_focus']}")
        fp_lines.append(
            "IMPORTANT: Use this profile as the primary source of truth about this client. "
            "Do not describe them as having 'no history' or 'no sector experience' — they have "
            "the credentials listed above even if they do not appear in published award records."
        )
        firm_profile_section = "\n".join(fp_lines) + "\n\n"
    else:
        firm_profile_section = ""

    # Flags
    flags = context.get("flags", [])
    flags_text = "\n".join(
        f"- [{f['severity'].upper()}] {f['description'][:120]}" for f in flags
    ) or "No active intelligence flags for this agency/sector."

    # Agency procurement plan signals
    plan_sigs = context.get("agency_plan_signals") or []
    if plan_sigs:
        plan_lines = []
        for ps in plan_sigs:
            vb = ps.get("estimated_value_band") or "Unknown"
            tf = ps.get("estimated_timeframe") or "Unknown"
            body = ps.get("signal_body") or ps.get("signal_title") or ""
            plan_lines.append(
                f"- [{ps.get('signal_type','signal').replace('_',' ').title()}] "
                f"{ps.get('signal_title','')[:100]} | "
                f"Value: {vb} | Timeframe: {tf}"
                + (f"\n  Evidence: {body[:180]}" if body else "")
            )
        agency_plan_intel = (
            f"\n=== AGENCY PROCUREMENT PLAN INTELLIGENCE ===\n"
            f"The following signals were extracted from {n.get('agency','the agency')}'s "
            f"published procurement plan:\n"
            + "\n".join(plan_lines)
            + "\n\nInstruction: Where procurement plan signals align with this notice, "
            "treat them as corroborating evidence of an active pipeline. "
            "Where they contradict the notice (e.g. plan suggests a later timeframe), "
            "flag this as a potential discrepancy worth investigating.\n"
        )
    else:
        agency_plan_intel = ""

    # National market context
    nm = context.get("national_market", {})
    nat_top3 = nm.get("top3_national", [])
    if nat_top3:
        national_top3_text = "\n".join(
            f"  {i+1}. {r['business_name']}: {r['wins']} wins nationally "
            f"({_fmt_value(r.get('total_value'))} total)"
            for i, r in enumerate(nat_top3)
        )
    else:
        national_top3_text = "  Insufficient MBIE data for national ranking."

    # Agency stats
    ag = context.get("agency_stats", {})
    n = context.get("notice", {})

    # Most frequent supplier to this specific agency (top of competitive table)
    most_frequent_agency_supplier = "Not identified in MBIE data."
    if comps:
        top_comp = comps[0]
        if top_comp.get("agency_wins", 0) > 0:
            most_frequent_agency_supplier = (
                f"{top_comp['supplier_name']} — {top_comp['agency_wins']} wins with "
                f"{n.get('agency', 'this agency')}, avg {_fmt_value(top_comp.get('avg_value'))}"
            )
        else:
            most_frequent_agency_supplier = (
                f"{top_comp['supplier_name']} — {top_comp['wins']} sector wins nationally "
                f"(no recorded wins with this specific agency)"
            )
    e = context.get("enrichment", {})

    # Authenticated tender documents block
    extra_docs = context.get("extra_docs") or []
    if extra_docs:
        doc_parts = []
        for doc in extra_docs:
            doc_parts.append(
                f"--- Document: {doc.get('file_name', 'Uploaded document')} ---\n"
                f"{doc.get('text', '').strip()}"
            )
        authenticated_docs_block = (
            "\n\n=== AUTHENTICATED TENDER DOCUMENTS ===\n"
            "The following documents were uploaded directly from GETS by the client. "
            "Treat this content as the primary source of truth about the tender.\n\n"
            + "\n\n".join(doc_parts)
            + "\n=== END AUTHENTICATED DOCUMENTS ===\n"
        )
    else:
        authenticated_docs_block = ""

    prompt = _PURSUIT_PROMPT.format(
        client_name=context["client_name"],
        firm_profile_section=firm_profile_section,
        title=n.get("title", "Unknown"),
        agency=n.get("agency", "Unknown"),
        sector=n.get("sector_tag", "other"),
        value_band=n.get("value_band", "unknown"),
        close_date=str(n.get("close_date", "Unknown")),
        days_until_close=n.get("days_until_close") or "Unknown",
        source_url=n.get("source_url", ""),
        procurement_stage=n.get("procurement_stage") or "Not determined",
        tender_posture=context.get("tender_posture") or "Live bid — a contract is being awarded from this process.",
        briefing_date=str(n.get("briefing_date") or "Not found in notice"),
        questions_deadline=str(n.get("questions_deadline") or "Not found in notice"),
        registration_deadline=str(n.get("registration_deadline") or "Not found in notice"),
        overview=(n.get("overview_text") or n.get("description") or "Not provided in notice")[:2000],
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
        agency_plan_intel=agency_plan_intel,
        national_total_contracts=nm.get("total_contracts", 0),
        national_total_value=_fmt_value(nm.get("total_value", 0)),
        national_avg_value=_fmt_value(nm.get("avg_value", 0)),
        national_top3_text=national_top3_text,
        most_frequent_agency_supplier=most_frequent_agency_supplier,
        authenticated_docs=authenticated_docs_block,
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

        def _try_parse(text: str) -> Optional[dict]:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return None

        result = _try_parse(raw)
        if result:
            return result

        # Recovery pass 1: close truncated structures
        logger.warning("JSON parse failed — attempting recovery")
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
        result = _try_parse(fixed)
        if result:
            logger.info("Truncation recovery succeeded")
            return result

        # Recovery pass 2: ask Claude to emit only the JSON
        logger.warning("Truncation recovery failed — retrying Claude with JSON-only instruction")
        retry_msg = client.messages.create(
            model=config.CLAUDE_MODEL_L3,
            max_tokens=config.CLAUDE_MAX_TOKENS_L3,
            system=_PURSUIT_SYSTEM,
            messages=[
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": raw},
                {"role": "user", "content": (
                    "Your previous response contained invalid JSON. "
                    "Return ONLY a valid JSON object — no preamble, no markdown, "
                    "no commentary. Start with { and end with }."
                )},
            ],
        )
        retry_raw = retry_msg.content[0].text.strip()
        if retry_raw.startswith("```"):
            retry_raw = retry_raw.split("```")[1]
            if retry_raw.startswith("json"):
                retry_raw = retry_raw[4:]
            retry_raw = retry_raw.strip()
        result = _try_parse(retry_raw)
        if result:
            logger.info("JSON retry recovery succeeded")
            return result

        logger.error("Claude synthesis failed after retry — response: %s", raw[:200])
        return None
    except Exception as exc:
        logger.error("Claude synthesis failed: %s", exc)
        return None


# ── HTML rendering ─────────────────────────────────────────────────────────────

_CSS = """
:root {
  --bg:#f5f6f8; --surface:#ffffff; --surf2:#f0f2f5; --border:#e2e6ea;
  --text:#2c3e50; --muted:#6c757d; --navy:#1a2d4a; --gold:#2a9d8f;
  --gold-l:#e0f4f2; --navy-l:#e8ecf3; --red:#c0392b; --red-l:#fdecea;
  --green:#27ae60; --accent:#2a9d8f;
  --font:'Inter',system-ui,-apple-system,sans-serif;
}
*, *::before, *::after { box-sizing:border-box; margin:0; padding:0; }
html { background:var(--bg); }
body { background:var(--bg); color:var(--text); font-family:var(--font);
       font-size:14px; line-height:1.6; display:flex; min-height:100vh;
       max-width:1280px; margin:0 auto; width:100%;
       -webkit-font-smoothing:antialiased; }
a { color:var(--navy); text-decoration:none; }
a:hover { color:var(--gold); }
.sidebar { width:240px; flex-shrink:0; background:var(--navy);
           position:sticky; top:0; height:100vh; overflow-y:auto;
           padding:1.75rem 1.25rem; }
.sidebar-brand { font-size:.82rem; font-weight:800; color:#fff; letter-spacing:-.01em; margin-bottom:.2rem; }
.sidebar-brand .by { font-weight:400; color:rgba(255,255,255,.45); }
.sidebar-sub { font-size:.62rem; font-weight:700; letter-spacing:.1em; text-transform:uppercase; color:var(--gold); margin-bottom:1.5rem; }
.sidebar-label { font-size:.6rem; font-weight:700; letter-spacing:.09em; text-transform:uppercase; color:rgba(255,255,255,.4); margin:1.2rem 0 .4rem; }
.sidebar nav a { display:block; font-size:.8rem; color:rgba(255,255,255,.7); text-decoration:none; padding:.3rem .5rem; border-radius:4px; margin-bottom:.15rem; transition:background .12s; }
.sidebar nav a:hover { background:rgba(255,255,255,.1); color:#fff; }
.main { flex:1; min-width:0; padding:2.5rem 3rem; }
/* Mobile TOC — shown only on small screens; hidden on desktop */
.mobile-toc { display:none; }
.cover { margin-bottom:2.5rem; padding-bottom:1.75rem; border-bottom:2px solid var(--navy); }
.cover-label { font-size:.65rem; font-weight:700; letter-spacing:.1em; text-transform:uppercase; color:var(--gold); margin-bottom:.4rem; }
.cover-title { font-size:1.55rem; font-weight:800; color:var(--navy); line-height:1.3; margin-bottom:.6rem; }
.cover-agency { font-size:.95rem; color:var(--muted); margin-bottom:1.1rem; }
.cover-meta { display:flex; flex-wrap:wrap; gap:.65rem; margin-bottom:1.1rem; }
.meta-chip { font-size:.7rem; padding:.22rem .6rem; border-radius:999px; border:1px solid; font-weight:600; }
.chip-blue  { background:var(--navy-l); color:var(--navy); border-color:#b0bcd4; }
.chip-gold  { background:var(--gold-l); color:#1a6b62; border-color:var(--gold); }
.chip-red   { background:var(--red-l); color:var(--red); border-color:#f1a9a0; }
.chip-green { background:#eafaf1; color:var(--green); border-color:#a9dfbf; }
.chip-grey  { background:var(--surf2); color:var(--muted); border-color:var(--border); }
.cover-client { font-size:.8rem; color:var(--muted); }
.cover-client strong { color:var(--navy); }
.verdict { display:flex; align-items:center; gap:1.5rem; padding:1.25rem 1.5rem; border-radius:8px; border:1px solid; margin-bottom:2rem; }
.verdict.go     { background:#eafaf1; border-color:#a9dfbf; }
.verdict.cond   { background:var(--gold-l); border-color:var(--gold); }
.verdict.nogo   { background:var(--red-l); border-color:#f1a9a0; }
.verdict.engage { background:#e8f4fd; border-color:#3498db; }
.verdict-badge { font-size:1.1rem; font-weight:800; letter-spacing:.04em; flex-shrink:0; }
.verdict.go     .verdict-badge { color:var(--green); }
.verdict.cond   .verdict-badge { color:#1a6b62; }
.verdict.nogo   .verdict-badge { color:var(--red); }
.verdict.engage .verdict-badge { color:#1a5a8a; }
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
.risk-medium { color:#1a6b62;      font-weight:600; }
.risk-low    { color:var(--green); font-weight:600; }
.action-item { display:flex; align-items:flex-start; gap:.85rem; padding:.75rem 1rem; border:1px solid var(--border); border-radius:6px; margin-bottom:.5rem; background:var(--surface); }
.action-priority { flex-shrink:0; font-size:.65rem; font-weight:700; letter-spacing:.06em; text-transform:uppercase; padding:.2rem .5rem; border-radius:4px; }
.pri-critical { background:var(--red-l); color:var(--red); }
.pri-high     { background:var(--gold-l); color:#1a6b62; }
.pri-medium   { background:var(--navy-l); color:var(--navy); }
.action-body  { flex:1; }
.action-text  { font-size:.83rem; color:var(--text); margin-bottom:.2rem; }
.action-time  { font-size:.72rem; color:var(--muted); }
.citation { font-size:.7rem; color:var(--muted); font-style:italic; margin-top:.5rem; padding:.4rem .75rem; background:var(--surf2); border-radius:4px; border-left:2px solid var(--gold); }
.doc-footer { margin-top:3rem; padding-top:1.5rem; border-top:1px solid var(--border); font-size:.7rem; color:var(--muted); display:flex; justify-content:space-between; align-items:center; }

/* ── Tablet ≤768px: hide sidebar, show inline mobile TOC ── */
@media (max-width:768px) {
  body { display:block; max-width:100%; }
  .sidebar { display:none; }
  .mobile-toc { display:block; background:var(--navy); padding:.75rem 1.25rem;
                margin-bottom:1.5rem; border-radius:8px; }
  .mobile-toc-label { font-size:.6rem; font-weight:700; letter-spacing:.09em;
                      text-transform:uppercase; color:var(--gold); margin-bottom:.5rem; }
  .mobile-toc nav { display:flex; flex-direction:column; gap:.25rem; }
  .mobile-toc nav a { display:block; font-size:.78rem; color:rgba(255,255,255,.8);
                      text-decoration:none; padding:.3rem .65rem; border-radius:4px;
                      border:1px solid rgba(255,255,255,.15); }
  .mobile-toc nav a:active { background:rgba(255,255,255,.1); }
  .main { padding:1.25rem 1.25rem; }
  .verdict { flex-wrap:wrap; gap:1rem; }
  table { display:block; overflow-x:auto; -webkit-overflow-scrolling:touch; }
  .doc-footer { flex-direction:column; gap:.3rem; }
}

/* ── Phone ≤480px ── */
@media (max-width:480px) {
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
    r = rec.upper()
    if "MARKET" in r or "ENGAGE" in r:
        return "engage"
    if "NO" in r:
        return "nogo"
    if "CONDITIONAL" in r:
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
    win_pos: Optional[dict] = None,
    analysis_type: str = "public",
    extra_docs_names: Optional[list] = None,
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

    verdict = a.get("go_nogo", "GO")
    vc = _verdict_class(verdict)

    # Win position band (replaces probability percentage)
    wp = win_pos or {}
    wp_label   = wp.get("band", "Competitive")
    wp_colour  = wp.get("colour", "#d4a017")
    wp_summary = wp.get("summary", "")
    wp_top3    = wp.get("top3", [])

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

    # Positioning cards — read from pursuit_positioning (new name) or positioning_recommendations (legacy)
    pos_cards = ""
    for rec in (a.get("pursuit_positioning") or a.get("positioning_recommendations") or []):
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

    notice_id_for_upgrade = _safe(notice.get("notice_id", ""))
    client_slug_for_upgrade = _slug(client_name)
    if analysis_type == "full":
        docs_list = ""
        if extra_docs_names:
            docs_list = "<ul style='margin:.5rem 0 0;padding-left:1.25rem;'>" + "".join(
                f"<li style='font-size:.8rem;margin-bottom:.2rem;'>{_safe(d)}</li>"
                for d in extra_docs_names
            ) + "</ul>"
        analysis_banner = (
            f'<div style="background:rgba(42,157,143,.12);border:2px solid #2a9d8f;'
            f'border-radius:8px;padding:1rem 1.5rem;margin-bottom:2rem;">'
            f'<div style="display:flex;align-items:center;gap:.75rem;flex-wrap:wrap;">'
            f'<span style="background:#2a9d8f;color:#fff;font-size:.65rem;font-weight:800;'
            f'letter-spacing:.1em;text-transform:uppercase;padding:.25rem .65rem;border-radius:4px;">'
            f'FULL ANALYSIS</span>'
            f'<span style="font-size:.82rem;color:var(--text);">'
            f'This analysis incorporates authenticated tender documents uploaded from GETS.</span>'
            f'</div>'
            f'{docs_list}'
            f'</div>'
        )
    elif not is_demo:
        upgrade_url = (
            f'/groundwork/pursuits/upgrade?notice_id={notice_id_for_upgrade}'
            f'&amp;client={client_slug_for_upgrade}'
        )
        analysis_banner = (
            f'<div style="background:rgba(30,45,64,.5);border:1px solid var(--border);'
            f'border-radius:8px;padding:1rem 1.5rem;margin-bottom:2rem;">'
            f'<div style="display:flex;align-items:center;justify-content:space-between;'
            f'flex-wrap:wrap;gap:1rem;">'
            f'<div>'
            f'<span style="background:rgba(100,120,180,.25);color:#8ab4f8;font-size:.65rem;'
            f'font-weight:800;letter-spacing:.1em;text-transform:uppercase;padding:.25rem .65rem;'
            f'border-radius:4px;margin-right:.6rem;">PUBLIC INTELLIGENCE ANALYSIS</span>'
            f'<span style="font-size:.82rem;color:var(--muted);">'
            f'Based on public notice data and historical award records. '
            f'Authenticated tender documents (RFP, addenda, Q&amp;A, briefing materials) are not included.'
            f'</span>'
            f'</div>'
            f'<a href="{upgrade_url}" style="display:inline-flex;align-items:center;gap:.4rem;'
            f'background:#2a9d8f;color:#fff;font-size:.8rem;font-weight:700;padding:.5rem 1.1rem;'
            f'border-radius:5px;text-decoration:none;white-space:nowrap;">'
            f'Upgrade to Full Analysis &#8599;</a>'
            f'</div>'
            f'</div>'
        )
    else:
        analysis_banner = ""

    ch = context.get("client_history", {})
    ag = context.get("agency_stats", {})
    citation = context.get("mbie_citation", "MBIE data")

    # ── ACH table ─────────────────────────────────────────────────────────────
    ach_table_html = ""
    ach_items = a.get("ach_table") or []
    if ach_items:
        ach_rows = ""
        for h in ach_items:
            prob = h.get("probability", "")
            if prob == "High":
                pc = "color:#c0392b;font-weight:700;"
            elif prob == "Medium":
                pc = "color:#d4a017;font-weight:700;"
            elif prob == "Low":
                pc = "color:#27ae60;font-weight:700;"
            else:
                pc = "color:var(--muted);font-style:italic;"
            ev_for = "".join(f"<li>{_safe(e)}</li>" for e in (h.get("evidence_for") or []))
            ev_against = "".join(f"<li>{_safe(e)}</li>" for e in (h.get("evidence_against") or []))
            ach_rows += (
                f"<tr>"
                f"<td style='font-weight:600;font-size:.82rem;'>{_safe(h.get('hypothesis', ''))}</td>"
                f"<td><ul style='margin:0;padding-left:1.1rem;font-size:.79rem;'>{ev_for}</ul></td>"
                f"<td><ul style='margin:0;padding-left:1.1rem;font-size:.79rem;'>{ev_against}</ul></td>"
                f"<td style='{pc}'>{_safe(prob)}</td>"
                f"</tr>"
            )
        ach_table_html = (
            f'<div style="margin-top:1.5rem;">'
            f'<div style="font-size:.72rem;font-weight:700;letter-spacing:.08em;'
            f'text-transform:uppercase;color:var(--navy);margin-bottom:.5rem;">'
            f'ACH Hypothesis Analysis</div>'
            f'<table style="font-size:.82rem;">'
            f'<thead><tr>'
            f'<th style="width:26%">Hypothesis</th>'
            f'<th style="width:35%">Evidence For</th>'
            f'<th style="width:30%">Evidence Against</th>'
            f'<th style="width:9%">Probability</th>'
            f'</tr></thead>'
            f'<tbody>{ach_rows}</tbody>'
            f'</table>'
            f'<div class="citation">{_safe(citation)}</div>'
            f'</div>'
        )

    # ── Cone of Plausibility ──────────────────────────────────────────────────
    cone = a.get("evaluation_cone") or {}
    cone_label = _safe(cone.get("label", "Inferred — no criteria published"))
    cone_html = ""
    if cone:
        def _cone_cell(sd, highlight=False):
            if not sd:
                return "<td></td>"
            bg = "background:var(--gold-l);" if highlight else ""
            return (
                f'<td style="vertical-align:top;padding:.75rem 1rem;'
                f'border:1px solid var(--border);border-radius:4px;{bg}">'
                f'<div style="font-size:.82rem;color:var(--text);margin-bottom:.4rem;">'
                f'{_safe(sd.get("scenario",""))}</div>'
                f'<div style="font-size:.75rem;color:var(--muted);font-style:italic;">'
                f'{_safe(sd.get("rationale",""))}</div>'
                f'</td>'
            )
        cone_html = (
            f'<div style="margin-top:1.25rem;">'
            f'<div style="font-size:.72rem;font-weight:700;letter-spacing:.08em;'
            f'text-transform:uppercase;color:var(--navy);margin-bottom:.5rem;">'
            f'Evaluation Criteria — Cone of Plausibility'
            f'<span style="font-size:.68rem;font-weight:400;color:var(--muted);'
            f'text-transform:none;letter-spacing:0;margin-left:.5rem;">'
            f'({cone_label})</span></div>'
            f'<table style="table-layout:fixed;">'
            f'<thead><tr>'
            f'<th style="width:33%;">Conservative'
            f'<br><span style="font-size:.6rem;font-weight:400;opacity:.75;">'
            f'Standard NZ govt practice</span></th>'
            f'<th style="width:34%;background:#2d4a3e;">Most Likely'
            f'<br><span style="font-size:.6rem;font-weight:400;opacity:.75;">'
            f'Agency patterns &amp; notice signals</span></th>'
            f'<th style="width:33%;">Optimistic'
            f'<br><span style="font-size:.6rem;font-weight:400;opacity:.75;">'
            f'Most favourable for client</span></th>'
            f'</tr></thead>'
            f'<tbody><tr>'
            f'{_cone_cell(cone.get("conservative"))}'
            f'{_cone_cell(cone.get("most_likely"), highlight=True)}'
            f'{_cone_cell(cone.get("optimistic"))}'
            f'</tr></tbody>'
            f'</table>'
            f'</div>'
        )

    # ── Centre of Gravity ─────────────────────────────────────────────────────
    cog = a.get("centre_of_gravity") or {}
    cog_html = ""
    if cog:
        cog_html = (
            f'<div style="background:var(--navy);color:#fff;border-radius:8px;'
            f'padding:1.25rem 1.5rem;margin-bottom:1rem;">'
            f'<div style="font-size:.62rem;font-weight:700;letter-spacing:.1em;'
            f'text-transform:uppercase;color:var(--gold);margin-bottom:.5rem;">'
            f'The deciding factor</div>'
            f'<div style="font-size:1rem;font-weight:700;line-height:1.4;'
            f'margin-bottom:.75rem;">{_safe(cog.get("factor",""))}</div>'
            f'<div style="font-size:.84rem;color:rgba(255,255,255,.82);line-height:1.65;'
            f'margin-bottom:.65rem;">{_safe(cog.get("why_it_dominates",""))}</div>'
            f'<div style="font-size:.82rem;color:var(--gold);'
            f'border-top:1px solid rgba(255,255,255,.15);padding-top:.65rem;">'
            f'<strong>Strategic implication:</strong> '
            f'{_safe(cog.get("strategic_implication",""))}'
            f'</div></div>'
        )

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
    <a href="#agency">03 Agency Profile</a>
    <a href="#cog">04 Centre of Gravity</a>
    <a href="#competitive">05 Competitive Landscape</a>
    <a href="#positioning">06 Pursuit Positioning</a>
    <a href="#risks">07 Risk Register</a>
    <a href="#actions">08 Recommended Actions</a>
  </nav>
  <div class="sidebar-label" style="margin-top:2rem;">Data</div>
  <div style="font-size:.7rem;color:var(--muted);line-height:1.6;">
    NZ GETS notices<br>
    MBIE awards: 27,948<br>
    Supplier profiles: 5,830
  </div>
</div>

<div class="main">

  <!-- Mobile TOC (hidden on desktop via CSS; sidebar replaces this on ≥768px) -->
  <div class="mobile-toc">
    <div class="mobile-toc-label">Contents</div>
    <nav>
      <a href="#exec">01 Executive Summary</a>
      <a href="#assessment">02 Opportunity Assessment</a>
      <a href="#agency">03 Agency Profile</a>
      <a href="#cog">04 Centre of Gravity</a>
      <a href="#competitive">05 Competitive Landscape</a>
      <a href="#positioning">06 Pursuit Positioning</a>
      <a href="#risks">07 Risk Register</a>
      <a href="#actions">08 Recommended Actions</a>
    </nav>
  </div>

  {demo_banner}
  {analysis_banner}

  <!-- Cover -->
  <div class="cover">
    <div class="cover-label">Groundwork by BidEdge &mdash; Pursuit Intelligence</div>
    <div class="cover-title">{_safe(n.get('title', 'Unknown Opportunity'))}</div>
    <div class="cover-agency">{_safe(n.get('agency', ''))}</div>
    <div class="cover-meta">
      <span class="meta-chip chip-blue">{sector}</span>
      <span class="meta-chip {urgency_chip}">{_safe(urgency_label)}</span>
      <span class="meta-chip chip-grey">Close: {_safe(close_str)}</span>
      <span class="meta-chip" style="background:{wp_colour}22;color:{wp_colour};border:1px solid {wp_colour}66;">{_safe(wp_label)}</span>
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
      <div class="prob-pct" style="font-size:1rem;color:{wp_colour};">{_safe(wp_label)}</div>
      <div class="prob-label">Win position</div>
    </div>
    <div style="width:1px;height:48px;background:var(--border);"></div>
    <div class="verdict-badge">{_safe(verdict)}</div>
    <div class="verdict-text">{_safe(a.get('go_nogo_rationale', ''))}</div>
  </div>

  <!-- Win position detail -->
  <div style="background:rgba(42,157,143,.06);border:1px solid rgba(42,157,143,.2);border-radius:8px;padding:1rem 1.25rem;margin-bottom:1.75rem;font-size:.82rem;">
    <div style="font-weight:700;color:var(--navy);margin-bottom:.45rem;">Competitive Position Assessment</div>
    <div style="margin-bottom:.6rem;color:{wp_colour};font-weight:600;">{_safe(wp_summary)}</div>
    {"".join(
      f'<div style="display:flex;gap:.5rem;align-items:baseline;margin-bottom:.25rem;">'
      f'<span style="flex-shrink:0;font-size:.68rem;font-weight:700;padding:.1rem .4rem;border-radius:3px;'
      f'background:{"rgba(42,157,143,.15)" if f["score"] > 0 else ("rgba(224,85,85,.12)" if f["score"] < 0 else "rgba(150,150,150,.12)")};'
      f'color:{"#2a9d8f" if f["score"] > 0 else ("#e05555" if f["score"] < 0 else "#888")};">'
      f'{"+" if f["score"] > 0 else ""}{f["score"]}</span>'
      f'<span style="color:var(--text);">{_safe(f["reason"])}</span></div>'
      for f in wp_top3
    )}</div>

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

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-bottom:1rem;">
      <div style="background:var(--navy-l);border:1px solid #b0bcd4;border-radius:6px;padding:.9rem 1rem;">
        <div style="font-size:.62rem;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:var(--navy);margin-bottom:.4rem;">Opportunity Structure</div>
        <div style="font-size:.82rem;color:var(--text);line-height:1.65;">{_safe(a.get('opportunity_structure_assessment') or a.get('win_probability_rationale') or '')}</div>
      </div>
      <div style="background:var(--surf2);border:1px solid var(--border);border-radius:6px;padding:.9rem 1rem;">
        <div style="font-size:.62rem;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:var(--navy);margin-bottom:.4rem;">Client Position — {_safe(client_name)}</div>
        <div style="font-size:.82rem;color:var(--text);line-height:1.65;">{_safe(a.get('client_specific_factors') or '')}</div>
      </div>
    </div>

    {(
        f'<div style="background:#1a3a2a;border:1px solid #2d6a4f;border-radius:6px;'
        f'padding:.75rem 1rem;margin-bottom:.85rem;font-size:.8rem;color:#a8d8c0;">'
        f'<span style="font-weight:700;letter-spacing:.06em;text-transform:uppercase;'
        f'font-size:.68rem;color:#4ecca3;">MBIE Client Record</span>&ensp;'
        f'No government contract award history found for <strong>{_safe(client_name)}</strong> '
        f'in the MBIE dataset. This is a data absence, not a capability assessment. '
        f'Win position is assessed on opportunity structure only.</div>'
    ) if ch.get('sector_wins', 0) == 0 else (
        f'<div style="background:#1a2d3a;border:1px solid #2d4a6a;border-radius:6px;'
        f'padding:.75rem 1rem;margin-bottom:.85rem;font-size:.8rem;color:#a8c8e0;">'
        f'<span style="font-weight:700;letter-spacing:.06em;text-transform:uppercase;'
        f'font-size:.68rem;color:#4ea8cc;">MBIE Client Record</span>&ensp;'
        f'<strong>{_safe(client_name)}</strong>: '
        f'{ch.get("sector_wins", 0)} recorded government contract wins | '
        f'{_fmt_value(ch.get("sector_total_value", 0))} total. '
        f'Most recent: {str(ch.get("sector_last_win", ""))[:10] or "unknown"}.'
        f'</div>'
    )}
    <table style="margin-top:.75rem;">
      <thead><tr>
        <th>Dimension</th><th>Data</th>
      </tr></thead>
      <tbody>
        <tr><td>Client wins in this sector (contract records)</td><td>{ch.get('sector_wins', 0)} contracts | {_fmt_value(ch.get('sector_total_value', 0))}</td></tr>
        <tr><td>Client wins with this agency</td><td>{ch.get('agency_wins', 0)} contracts</td></tr>
        <tr><td>Strategic fit score</td><td>{a.get('strategic_fit_score', 'N/A')} / 10</td></tr>
        <tr><td>Win position band</td><td><span style="color:{wp_colour};font-weight:600;">{_safe(wp_label)}</span> (score {wp.get('score', 0):+d} across 8 factors)</td></tr>
        <tr><td>Days until close</td><td>{dtc if dtc is not None else 'Unknown'}</td></tr>
      </tbody>
    </table>
    <div class="citation">{_safe(citation)}</div>
  </div>

  <!-- 03 Agency Profile -->
  <div class="section" id="agency">
    <div class="section-number">03</div>
    <div class="section-title">Agency Profile — {_safe(n.get('agency', ''))}</div>
    <div class="prose">{_paras(a.get('agency_insights') or '')}</div>

    <table>
      <thead><tr><th>Metric</th><th>Value</th></tr></thead>
      <tbody>
        <tr><td>Total recorded contract awards</td><td>{ag.get('total_awards', 0)} contracts</td></tr>
        <tr><td>Total awarded value</td><td>{_fmt_value(ag.get('total_value', 0))}</td></tr>
        <tr><td>Average contract value</td><td>{_fmt_value(ag.get('avg_value', 0))}</td></tr>
        <tr><td>Unique suppliers engaged</td><td>{ag.get('unique_suppliers', 0)}</td></tr>
        <tr><td>Awards in {_safe(n.get('sector_tag', ''))} sector</td><td>{ag.get('sector_awards', 0)}</td></tr>
        <tr><td>Top procurement sectors</td><td>{_safe(', '.join(f"{s[0]} ({s[1]})" for s in ag.get('top_sectors', [])))}</td></tr>
      </tbody>
    </table>
    <div class="citation">{_safe(citation)}</div>
    {cone_html}
  </div>

  <!-- 04 Centre of Gravity -->
  <div class="section" id="cog">
    <div class="section-number">04</div>
    <div class="section-title">Centre of Gravity Assessment</div>
    {cog_html}
  </div>

  <!-- 05 Competitive Landscape -->
  <div class="section" id="competitive">
    <div class="section-number">05</div>
    <div class="section-title">Competitive Landscape</div>

    <div class="prose">{_paras(a.get('competitive_narrative') or a.get('competitive_assessment') or '')}</div>

    {inc_text}
    <div class="prose">{_paras(a.get('incumbent_assessment') or '')}</div>

    {f'''<table style="margin-top:.75rem;">
      <thead><tr>
        <th>Supplier</th>
        <th>This agency wins</th>
        <th>Sector wins total</th>
        <th>Avg contract value</th>
        <th>Last win</th>
      </tr></thead>
      <tbody>{comp_rows}</tbody>
    </table>
    <div class="citation">{_safe(citation)}</div>''' if comp_rows else '<p style="color:var(--muted);font-size:.82rem;font-style:italic;">Insufficient data for this agency/sector combination.</p>'}

    {ach_table_html}
  </div>

  <!-- 06 Pursuit Positioning -->
  <div class="section" id="positioning">
    <div class="section-number">06</div>
    <div class="section-title">Pursuit Positioning</div>
    {pos_cards}
  </div>

  <!-- 07 Risk Register -->
  <div class="section" id="risks">
    <div class="section-number">07</div>
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

  <!-- 08 Recommended Actions -->
  <div class="section" id="actions">
    <div class="section-number">08</div>
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
    firm_profile: Optional[dict] = None,
    extra_docs: Optional[list[dict]] = None,
    analysis_type: str = "public",
) -> Path:
    """
    Generate a pursuit intelligence package for a given notice and client.
    preferred_sectors: client's sector focus (e.g. ['ICT','security']).
    firm_profile: dict with keys name/description/staff/location/strengths/years_operating/
                  key_clients/sector_focus. Used to give Claude accurate context about the
                  client so narrative reflects actual capabilities, not blank-slate assumptions.
    extra_docs: list of dicts with 'file_name' and 'text' keys — authenticated tender documents
                uploaded from GETS. When provided, analysis_type should be 'full'.
    analysis_type: 'public' (default) or 'full' (when extra_docs are provided).
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
    # Incumbent detection: scan uploaded docs first, then web search.
    # MBIE award data is never used for named incumbent identification.
    incumbent = None
    incumbent_research = None
    incumbent_from_docs = False
    logger.info("INCUMBENT START: notice=%s agency=%r sector=%r title=%r has_extra_docs=%s",
                notice_id, agency, sector, (notice.get("title") or "")[:80], bool(extra_docs))
    if extra_docs:
        logger.info("INCUMBENT: extra_docs present (%d docs) — trying doc extraction first", len(extra_docs))
        incumbent_research = _extract_doc_incumbent(
            extra_docs, agency, notice.get("title") or ""
        )
        logger.info("INCUMBENT: after doc extraction: incumbent_research=%r",
                    incumbent_research[:80] if incumbent_research else None)
        if incumbent_research:
            incumbent_from_docs = True
            logger.info("Doc incumbent for %s: %s", agency, incumbent_research[:80])
    else:
        logger.info("INCUMBENT: no extra_docs — skipping doc extraction")
    if not incumbent_research:
        logger.info("INCUMBENT: no doc extraction result — running web search")
        _notice_text = (notice.get("overview_text") or notice.get("description") or "")[:2000]
        incumbent_research = _web_search_incumbent(
            agency, sector, notice.get("title") or "", _notice_text
        )
        logger.info("INCUMBENT web result for %s/%s: %r", agency, sector,
                    incumbent_research[:80] if incumbent_research else None)
    else:
        logger.info("INCUMBENT: doc extraction found result — skipping web search")
    logger.info("INCUMBENT FINAL: from_docs=%s result=%r",
                incumbent_from_docs,
                (incumbent_research or "")[:120])
    # Store named incumbent in bidder_pool so watchlist displays it
    _incumbent_firm = _extract_incumbent_firm_name(incumbent_research or "")
    if _incumbent_firm:
        _store_incumbent_in_bidder_pool(notice_id, _incumbent_firm, incumbent_research, sector)
    # Detect client-is-incumbent (re-tender of own contract).
    # Fast path: first-word name overlap in the incumbent research string.
    client_is_incumbent = bool(
        _incumbent_firm
        and client_name
        and client_name.lower().split()[0] in (incumbent_research or "").lower()
    )
    # Fallback: web search when fast-path doesn't match but an incumbent is identified.
    if not client_is_incumbent and _incumbent_firm and client_name:
        client_is_incumbent = _check_client_is_incumbent_web(
            client_name,
            agency,
            notice.get("title") or "",
            _incumbent_firm,
        )
    if client_is_incumbent:
        logger.info("INCUMBENT: client confirmed as incumbent — switching to defend/renewal framing")
    agency_stats = _get_agency_stats(agency, sector)
    flags = _get_relevant_flags(agency, sector)
    citation = _mbie_citation(sector, agency)
    national_market = _get_national_market_context(sector)

    # Fetch procurement plan signals for this agency
    agency_plan_signals: list = []
    try:
        _plan_rows = db.fetchall(
            """
            SELECT signal_type, signal_title, signal_body, estimated_value_band,
                   estimated_timeframe, confidence, strategic_weight, agency
            FROM intel_signals
            WHERE agency ILIKE %s
              AND signal_type IN (
                  'upcoming_contract','panel_refresh',
                  'renewal_risk','capability_investment','budget_signal'
              )
            ORDER BY strategic_weight DESC NULLS LAST, extracted_at DESC
            LIMIT 5
            """,
            (f"%{agency[:60]}%",),
        )
        agency_plan_signals = [dict(r) for r in _plan_rows]
        if agency_plan_signals:
            logger.info(
                "Agency plan signals for %s: %d found", agency, len(agency_plan_signals)
            )
    except Exception as _pe:
        logger.debug("Agency plan signal query failed: %s", _pe)

    # Classify tender type — determines strategic posture and whether bid framing applies
    from parsing import classify_tender_posture as _classify_posture
    tender_posture, is_live_bid = _classify_posture(
        notice.get("procurement_stage"), notice.get("category_raw")
    )
    logger.info("Tender posture for %s: %r (is_live_bid=%s)", notice_id, tender_posture[:60], is_live_bid)

    context = {
        "client_name": client_name,
        "preferred_sectors": preferred_sectors or [],
        "firm_profile": firm_profile or {},
        "notice": dict(notice),
        "enrichment": {
            "summary": notice.get("summary"),
            "evaluation_weighting": notice.get("evaluation_weighting"),
            "red_flags": notice.get("red_flags"),
            "strategic_framing": notice.get("strategic_framing"),
        },
        "competitors": [dict(c) for c in competitors],
        "client_history": client_history,
        "incumbent": None,
        "incumbent_research": incumbent_research,
        "incumbent_from_docs": incumbent_from_docs,
        "agency_stats": agency_stats,
        "flags": [dict(f) for f in flags],
        "mbie_citation": citation,
        "national_market": national_market,
        "agency_plan_signals": agency_plan_signals,
        "extra_docs": extra_docs or [],
        "tender_posture": tender_posture,
        "is_live_bid": is_live_bid,
        "analysis_type": analysis_type,
        "client_is_incumbent": client_is_incumbent,
    }

    # 2. Call Claude
    logger.info("Calling Claude for synthesis...")
    analysis = _call_claude(context)
    if not analysis:
        raise RuntimeError("Claude synthesis failed — no analysis returned")

    # 2b. Calculate win position band (replaces win_probability_pct)
    from win_position import calculate_win_position
    win_pos = calculate_win_position(
        notice=dict(notice),
        client_profile={"name": client_name},
        named_incumbent=None,
    )

    # 2c. Enforce band → recommendation consistency.
    # Prevent contradictory combinations (e.g. "Competitive — NO GO").
    # When go_nogo is overridden, also patch the rationale fields so the
    # narrative does not contradict the enforced recommendation.
    # Non-live-bid notices (RFI/NOI/ROI/advance) bypass enforcement — their
    # MARKET ENGAGEMENT verdict is intentional, not a contradiction to correct.
    _band_key = win_pos.get("css_key", "competitive")
    _rec = (analysis.get("go_nogo") or "GO").upper().strip()
    _band_label = win_pos.get("band", "Competitive")
    _go_nogo_overridden = False
    if not is_live_bid:
        # Override win position label to reflect market intelligence posture
        win_pos = {**win_pos, "band": "Market Intelligence", "colour": "#3498db", "css_key": "engage"}
        logger.info("Non-live-bid notice — win position overridden to 'Market Intelligence'")
    elif _band_key in ("strong", "competitive") and _rec == "NO GO":
        logger.info(
            "Win position is '%s' but go_nogo was '%s' — overriding to CONDITIONAL GO",
            _band_label, _rec,
        )
        analysis["go_nogo"] = "CONDITIONAL GO"
        _go_nogo_overridden = True
        # Patch rationale to be consistent with CONDITIONAL GO
        _fp = context.get("firm_profile") or {}
        _firm_creds = _fp.get("strengths") or "sector credentials"
        _existing_rationale = analysis.get("go_nogo_rationale") or ""
        if "not justified" in _existing_rationale.lower() or "no history" in _existing_rationale.lower() or "zero" in _existing_rationale.lower():
            analysis["go_nogo_rationale"] = (
                f"{client_name} has {_firm_creds} and a competitive position in this sector. "
                f"CONDITIONAL GO — viable subject to demonstrating relevant delivery capability "
                f"and engaging with the buyer before close. Key conditions: confirm scope alignment "
                f"and mobilise a credible bid team within the available window."
            )
    elif _band_key == "challenging" and _rec == "GO":
        logger.info(
            "Win position is 'Challenging' but go_nogo was 'GO' — overriding to CONDITIONAL GO"
        )
        analysis["go_nogo"] = "CONDITIONAL GO"
        _go_nogo_overridden = True
    elif _band_key == "not_recommended" and _rec != "NO GO":
        logger.info(
            "Win position is 'Not recommended' but go_nogo was '%s' — overriding to NO GO", _rec
        )
        analysis["go_nogo"] = "NO GO"
        _go_nogo_overridden = True

    # 3. Render HTML
    extra_docs_names = [d.get("file_name", "") for d in (extra_docs or [])]
    html = _render_html(notice, analysis, context, client_name,
                        is_demo=is_demo, demo_watermark=demo_watermark,
                        win_pos=win_pos, analysis_type=analysis_type,
                        extra_docs_names=extra_docs_names)

    # 4. Save
    if output_dir is None:
        output_dir = _artefact_dir(client_name)
    output_dir.mkdir(parents=True, exist_ok=True)

    suffix = "_full_analysis" if analysis_type == "full" else "_pursuit_package"
    filename = f"{notice_id}{suffix}.html"
    out_path = output_dir / filename
    out_path.write_text(html, encoding="utf-8")
    logger.info("Pursuit package written to %s", out_path)

    import storage as _storage
    import db as _db
    client_slug_val = _slug(client_name)
    storage_path = f"pursuits/{client_slug_val}/{filename}"
    if not _storage.upload_file(str(out_path), storage_path, "text/html"):
        logger.warning("Storage upload failed for %s", filename)
    output_type = "pursuit_package_full" if analysis_type == "full" else "pursuit_package"
    _db.save_output(
        output_type, date.today(), filename,
        content=html, storage_path=storage_path,
        client_slug=client_slug_val, client_name=client_name,
        notice_id=notice_id,
    )

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
