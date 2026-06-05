"""
Bidder pool inference module.

Matching strategy:
  1. Keyword relevance score — cosine-similarity-style overlap between notice
     text (title + description) and bidder profile text (notes + sector keywords).
  2. Sector gate — exact sector match uses BIDDER_MIN_RELEVANCE threshold;
     cross-sector matches require the higher BIDDER_CROSS_SECTOR_MIN_RELEVANCE bar.
  3. Reasoning — 1–3 human-readable bullets generated from the match evidence.
  4. Claude context — for top-N bidders on high-priority notices, a 2-sentence
     company profile is generated from Claude's training knowledge.

Layer 2 hooks:
  enrich_from_awards_history()  — will consume contract_awards table
  enrich_from_web()             — will consume web-scraped company profiles
"""
import csv
import json
import logging
import re
from pathlib import Path
from typing import Optional

import anthropic

import config
import db

logger = logging.getLogger(__name__)


# ── Stop words — stripped before keyword comparison ──────────────────────────

_STOP_WORDS = frozenset({
    "the", "and", "for", "are", "was", "were", "will", "been", "have",
    "has", "had", "not", "but", "with", "from", "that", "this", "they",
    "their", "them", "than", "its", "our", "can", "may", "including",
    "provide", "providing", "delivery", "support", "also", "other",
    "ltd", "limited", "group", "new", "zealand", "nzd", "government",
    "public", "sector", "national", "local", "services", "service",
    "contract", "contracts", "project", "projects",
    "work", "works", "related", "relevant", "based", "across",
    "control", "system", "systems", "solution", "solutions",
    # Do NOT add specific domain terms (road, marking, water etc.) — keep those
    # matchable even if they appear frequently.
})


def _extract_keywords(text: str) -> frozenset:
    words = re.findall(r"[a-z]{3,}", text.lower())
    return frozenset(w for w in words if w not in _STOP_WORDS)


def _keyword_relevance(
    notice: dict, bidder: dict
) -> tuple[float, list[str]]:
    """
    Returns (score 0.0–1.0, matched_terms).

    Score is |intersection| / sqrt(|notice_kws| × |bidder_kws|) —
    the cosine-similarity analogue for binary bag-of-words vectors.
    A score of 0.06 on a 50-word notice means ~3 meaningful overlapping terms.
    """
    notice_text = " ".join(filter(None, [
        notice.get("title") or "",
        (notice.get("description") or "")[:2000],
    ]))

    # Build bidder vocabulary from notes + all sector keyword lists
    sector_vocab = " ".join(
        " ".join(config.SECTOR_KEYWORDS.get(s, []))
        for s in bidder.get("_sectors", [])
    )
    bidder_text = " ".join(filter(None, [
        bidder.get("firm_name") or "",
        bidder.get("notes") or "",
        sector_vocab,
    ]))

    notice_kws = _extract_keywords(notice_text)
    bidder_kws = _extract_keywords(bidder_text)

    if not notice_kws or not bidder_kws:
        return 0.0, []

    matched = notice_kws & bidder_kws
    if not matched:
        return 0.0, []

    score = len(matched) / (len(notice_kws) ** 0.5 * len(bidder_kws) ** 0.5)
    score = min(round(score, 4), 1.0)

    # Return the most informative matched terms (longer words tend to be more specific)
    top_terms = sorted(matched, key=lambda w: -len(w))[:8]
    return score, top_terms


# ── Sector matching ───────────────────────────────────────────────────────────
#
# No broad sector macro-groups. Exact match = bidder sector tag == notice sector.
# Cross-sector matches are allowed ONLY when keyword relevance exceeds the higher
# threshold — this is the primary guard against irrelevant matches like
# Spark on road marking or L3Harris on animal control.

def _is_exact_sector_match(notice_sector: str, bidder_sectors: list[str]) -> bool:
    return notice_sector in bidder_sectors


# ── Reasoning generation ──────────────────────────────────────────────────────

SIZE_LABEL = {
    "micro": "micro-sized firm",
    "small": "small firm",
    "medium": "mid-sized firm",
    "large": "large firm",
    "major": "major national firm",
}

VALUE_LABEL = {
    "under_100k":  "< $100k contracts",
    "100k_500k":   "$100k–$500k contracts",
    "500k_2m":     "$500k–$2m contracts",
    "2m_10m":      "$2m–$10m contracts",
    "10m_plus":    "$10m+ contracts",
    "unknown":     "contracts of undisclosed value",
}


def _generate_reasoning(
    notice: dict,
    bidder: dict,
    match_type: str,
    matched_terms: list[str],
    relevance_score: float,
) -> list[str]:
    bullets = []
    sector = notice.get("sector_tag") or "other"
    value_band = notice.get("value_band") or "unknown"
    geo = notice.get("geographic_scope") or ""
    hq = bidder.get("headquarters") or ""
    size = (bidder.get("size") or "medium").lower()
    firm_sectors = bidder.get("_sectors", [])

    # Sector match reasoning
    if match_type == "exact":
        sector_label = sector.replace("_", " ")
        bullets.append(
            f"Direct sector match on {sector_label} — firm operates in this space"
        )
    else:
        bullets.append(
            f"Cross-sector match (firm: {', '.join(firm_sectors)}; notice: {sector}) "
            f"— keyword overlap indicates genuine relevance"
        )

    # Keyword evidence
    if matched_terms:
        terms_str = ", ".join(matched_terms[:5])
        bullets.append(f"Keyword overlap: {terms_str}")

    # Size and value fit
    size_str = SIZE_LABEL.get(size, "firm")
    val_str = VALUE_LABEL.get(value_band, "this contract size")
    bullets.append(f"{size_str.capitalize()}; typical bidder for {val_str}")

    # Geographic fit
    if hq and geo:
        geo_lower = geo.lower()
        hq_lower = hq.lower()
        if "national" in geo_lower or hq_lower in geo_lower or geo_lower in hq_lower:
            bullets.append(f"Geographic fit — headquartered {hq}; notice scope: {geo}")

    return bullets[:3]  # cap at 3 bullets


# ── Strategic importance ──────────────────────────────────────────────────────

SIZE_MATURITY_MAP = {
    "micro":  "weak",
    "small":  "weak",
    "medium": "moderate",
    "large":  "strong",
    "major":  "strong",
}

_VALUE_HIGH_SIZE = {
    "10m_plus":   {"major", "large"},
    "2m_10m":     {"major", "large", "medium"},
    "500k_2m":    {"major", "large", "medium", "small"},
    "100k_500k":  {"major", "large", "medium", "small", "micro"},
    "under_100k": {"major", "large", "medium", "small", "micro"},
    "unknown":    {"major", "large", "medium", "small", "micro"},
}


def _infer_strategic_importance(firm: dict, value_band: str, exact_match: bool) -> str:
    size = (firm.get("size") or "medium").lower()
    if not exact_match:
        return "low"
    eligible = _VALUE_HIGH_SIZE.get(value_band, set())
    if size in eligible and size in ("major", "large"):
        return "high"
    if size in eligible:
        return "medium"
    return "low"


def _infer_intelligence_maturity(firm: dict) -> str:
    size = (firm.get("size") or "medium").lower()
    return SIZE_MATURITY_MAP.get(size, "moderate")


# ── Load bidder reference list ────────────────────────────────────────────────

def load_bidders(csv_path: str = config.BIDDER_CSV_PATH) -> list[dict]:
    """
    Expected CSV columns: firm_name, sectors, size, headquarters, notes
    sectors: pipe-separated list matching config.SECTORS taxonomy
    size: micro / small / medium / large / major
    """
    path = Path(csv_path)
    if not path.exists():
        logger.warning("Bidder CSV not found at %s — bidder inference will be empty", csv_path)
        return []

    bidders = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row["_sectors"] = [s.strip() for s in row.get("sectors", "").split("|") if s.strip()]
            bidders.append(row)

    logger.info("Loaded %d bidders from %s", len(bidders), csv_path)
    return bidders


# ── Core matching ─────────────────────────────────────────────────────────────

def score_bidders_for_notice(
    notice: dict, all_bidders: list[dict]
) -> list[dict]:
    """
    Return a ranked list of credible bidders for a notice.

    Each returned dict includes: firm_name, sector, size, strategic_importance,
    intelligence_maturity, relevance_score, match_type, reasoning (list[str]).
    company_context and context_confidence are added later by enrich_bidder_context().
    """
    notice_sector = notice.get("sector_tag") or "other"
    value_band = notice.get("value_band") or "unknown"
    candidates = []

    for firm in all_bidders:
        firm_sectors = firm.get("_sectors", [])
        exact = _is_exact_sector_match(notice_sector, firm_sectors)

        score, matched_terms = _keyword_relevance(notice, firm)

        # Apply threshold gates
        if exact:
            if score < config.BIDDER_MIN_RELEVANCE:
                continue
            match_type = "exact"
        else:
            if score < config.BIDDER_CROSS_SECTOR_MIN_RELEVANCE:
                continue
            match_type = "cross_sector"

        importance = _infer_strategic_importance(firm, value_band, exact)
        maturity = _infer_intelligence_maturity(firm)
        reasoning = _generate_reasoning(
            notice, firm, match_type, matched_terms, score
        )

        rank_key = (
            0 if exact else 1,
            {"high": 0, "medium": 1, "low": 2}.get(importance, 2),
            -score,  # higher relevance scores rank first within tier
            {"major": 0, "large": 1, "medium": 2, "small": 3, "micro": 4}.get(
                firm.get("size", "medium").lower(), 2
            ),
        )
        candidates.append({
            "firm_name":             firm["firm_name"],
            "sector":                firm.get("sectors"),
            "size":                  firm.get("size"),
            "strategic_importance":  importance,
            "intelligence_maturity": maturity,
            "relevance_score":       score,
            "match_type":            match_type,
            "reasoning":             reasoning,
            "company_context":       None,
            "context_confidence":    "unknown",
            "_rank":                 rank_key,
        })

    candidates.sort(key=lambda x: x["_rank"])
    for c in candidates:
        del c["_rank"]
    return candidates


# ── Claude company context enrichment ────────────────────────────────────────

_claude_client: Optional[anthropic.Anthropic] = None


def _get_claude() -> anthropic.Anthropic:
    global _claude_client
    # Re-read key each time in case config loaded after module import
    key = config.ANTHROPIC_API_KEY
    if _claude_client is None or not _claude_client.api_key:
        _claude_client = anthropic.Anthropic(api_key=key)
    return _claude_client


_CONTEXT_SYSTEM = (
    "You are a procurement intelligence analyst with knowledge of New Zealand "
    "government market participants. Respond ONLY with a valid JSON object — "
    "no preamble, no markdown fences."
)

_CONTEXT_PROMPT = """For the firm "{firm_name}", assess their credibility as a bidder for the following NZ government procurement notice.

Notice title: {title}
Procuring agency: {agency}
Sector: {sector}
Value band: {value_band}

Return a JSON object with exactly these keys:
"company_context": Two sentences. First: what this company actually does and their market position in New Zealand. Second: why they are specifically credible for this notice.
"confidence": "high" if you have reliable knowledge of this NZ entity, "medium" if you have partial knowledge, "low" if you are uncertain whether this profile is accurate for the specific NZ entity (e.g. you may be confusing it with a different entity or have limited NZ-specific knowledge)."""


def enrich_bidder_context(
    bidder_name: str,
    notice: dict,
) -> tuple[Optional[str], str]:
    """
    Call Claude API to generate a 2-sentence company profile for a bidder
    in the context of a specific notice. Returns (company_context, confidence).
    Uses Claude's training knowledge only — no real-time web access.
    """
    client = _get_claude()
    prompt = _CONTEXT_PROMPT.format(
        firm_name=bidder_name,
        title=notice.get("title") or "Unknown",
        agency=notice.get("agency") or "Unknown",
        sector=(notice.get("sector_tag") or "other").replace("_", " "),
        value_band=notice.get("value_band") or "unknown",
    )
    try:
        message = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=300,
            system=_CONTEXT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        result = json.loads(raw)
        return result.get("company_context"), result.get("confidence", "unknown")
    except (json.JSONDecodeError, anthropic.APIError, Exception) as exc:
        logger.warning("Claude context failed for %s: %s", bidder_name, exc)
        return None, "unknown"


# ── Storage ───────────────────────────────────────────────────────────────────

def _store_bidders(notice_id: str, bidders: list[dict]) -> None:
    for b in bidders:
        reasoning_str = " | ".join(b.get("reasoning") or [])
        db.execute(
            """
            INSERT INTO bidder_pool
                (notice_id, firm_name, sector, size,
                 strategic_importance, intelligence_maturity,
                 relevance_score, match_type, reasoning,
                 company_context, context_confidence)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (notice_id, firm_name) DO UPDATE SET
                strategic_importance  = EXCLUDED.strategic_importance,
                intelligence_maturity = EXCLUDED.intelligence_maturity,
                relevance_score       = EXCLUDED.relevance_score,
                match_type            = EXCLUDED.match_type,
                reasoning             = EXCLUDED.reasoning,
                company_context       = EXCLUDED.company_context,
                context_confidence    = EXCLUDED.context_confidence
            """,
            (
                notice_id,
                b["firm_name"],
                b.get("sector"),
                b.get("size"),
                b["strategic_importance"],
                b["intelligence_maturity"],
                b.get("relevance_score"),
                b.get("match_type"),
                reasoning_str,
                b.get("company_context"),
                b.get("context_confidence"),
            ),
        )


# ── Layer 2 placeholder hooks ─────────────────────────────────────────────────

def enrich_from_awards_history(
    notice_id: str,
    bidder_name: str,
) -> Optional[dict]:
    """
    Layer 2 hook — enrich a bidder entry with evidence from the contract awards
    history stored in the Layer 2 `contract_awards` table.

    When Layer 2 is built, this function will:
      - Query `contract_awards` for past awards to `bidder_name` from the same
        agency or in the same sector as `notice_id`.
      - Count awards won, average value, last award date, and win rate against
        known competitors.
      - Return a dict with keys:
          past_awards_count: int
          last_award_date: date | None
          avg_award_value: float | None
          win_rate_this_agency: float | None  (0.0–1.0)
          award_evidence: str  (human-readable 1-sentence summary)

    Consumes (Layer 2):
      contract_awards(notice_id, awardee_name, award_date, award_value,
                      agency, sector, source_url)

    Returns None until Layer 2 is implemented.
    """
    return None


def enrich_from_web(
    bidder_name: str,
    sector: str,
) -> Optional[dict]:
    """
    Layer 2 hook — enrich a bidder entry with data scraped from the company's
    public web presence (website, LinkedIn, Companies Office register).

    When Layer 2 is built, this function will:
      - Retrieve the company's NZBN/Companies Office record for: registration
        status, registered address, director names, and annual return date.
      - Scrape the company website (if available) for: recent project references,
        key personnel names, capability statement language, and certifications.
      - Return a dict with keys:
          nzbn: str | None
          registration_status: str  (e.g. "Registered", "In Liquidation")
          registered_address: str | None
          key_people: list[str]  (names from Companies Office or website)
          recent_projects: list[str]  (scraped project reference summaries)
          website_url: str | None
          scraped_at: datetime | None

    Consumes (Layer 2):
      organisations(firm_name, nzbn, website_url, last_scraped_at, ...)
      people(name, organisation_id, role, ...)

    Returns None until Layer 2 is implemented.
    """
    return None


# ── Main entry point ──────────────────────────────────────────────────────────

def run_bidder_inference() -> int:
    logger.info("Starting bidder pool inference")
    all_bidders = load_bidders()

    if not all_bidders:
        logger.warning("No bidder data — skipping bidder inference")
        return 0

    # Fetch notices that need bidder inference, including description for keyword matching
    notices = db.fetchall(
        """
        SELECT s.notice_id, p.sector_tag, p.value_band, p.geographic_scope,
               r.title, r.description, r.agency, r.category_raw
        FROM   scored_notices s
        JOIN   parsed_notices p ON p.notice_id = s.notice_id
        JOIN   raw_notices r    ON r.notice_id = s.notice_id
        WHERE  s.composite_score >= %s
          AND  s.notice_id NOT IN (SELECT DISTINCT notice_id FROM bidder_pool)
        ORDER  BY s.composite_score DESC
        """,
        (config.PRIORITY_THRESHOLD,),
    )

    enrichment_cap = config.MAX_ENRICHMENT_NOTICES
    logger.info(
        "%d high-priority notices require bidder inference "
        "(Claude context enrichment capped at top %d by score)",
        len(notices), enrichment_cap,
    )
    count = 0

    for rank, notice in enumerate(notices):
        bidders = score_bidders_for_notice(notice, all_bidders)

        if not bidders:
            logger.debug("No credible bidders found for notice %s", notice["notice_id"])
            continue

        # Only enrich with Claude context for notices within the enrichment cap.
        # Notices outside the cap get rule-based inference only — company_context
        # stays None and context_confidence is set to "not_run" so the output
        # layer can render an appropriate label.
        within_cap = rank < enrichment_cap
        top_n = config.BIDDER_CLAUDE_CONTEXT_TOP_N if within_cap else 0

        for i, b in enumerate(bidders[:top_n]):
            logger.debug(
                "Enriching bidder context: %s for notice %s",
                b["firm_name"], notice["notice_id"],
            )
            context, confidence = enrich_bidder_context(b["firm_name"], notice)
            bidders[i]["company_context"] = context
            bidders[i]["context_confidence"] = confidence

        if not within_cap:
            for b in bidders:
                b["context_confidence"] = "not_run"

        _store_bidders(notice["notice_id"], bidders)
        logger.info(
            "Stored %d bidders for notice %s%s (%s)",
            len(bidders),
            notice["notice_id"],
            " [Claude enriched]" if within_cap else " [rule-based only]",
            notice.get("title", "")[:60],
        )
        count += 1

    logger.info("Bidder inference complete: %d notices processed", count)
    return count
