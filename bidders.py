"""
Bidder pool inference module.

Architecture (post-rebuild):
  Firm identification: web search only (bidder_intelligence.py Stage 1).
  MBIE: per-firm validation metadata only (bidder_intelligence.py Stage 2).
  CSV:  no longer used for firm identification.

This module now provides:
  - Government entity detection (_is_government_entity)
  - Web search firm identification (_web_search_bidders)
  - MBIE availability check (_mbie_available)
  - Specialist notice detection (_notice_is_specialist) — kept for portal/output compat
  - Firm exclusion helper (_firm_is_excluded) — simplified to gov-entity check only
  - Bidder storage (_store_bidders)
  - Layer 2 hooks (enrich_from_awards_history, enrich_from_web)
  - Batch runner (run_bidder_inference) — web search only
"""
import csv
import logging
import re
from pathlib import Path
from typing import Optional

import config
import db
from canonical_suppliers import canonical_name, deduplicate_bidders

logger = logging.getLogger(__name__)


# ── Government / public-sector entity exclusion ───────────────────────────────
# Government agencies, crown entities, and public-sector bodies receive contracts
# — they do not bid for them. Filter them from all likely-bidder results.

_GOVT_EXPLICIT_NAMES: frozenset = frozenset({
    "national security group",
    "new zealand defence force",
    "nzdf",
    "new zealand police",
    "nz police",
    "inland revenue department",
    "inland revenue",
    "accident compensation corporation",
    "new zealand transport agency",
    "waka kotahi",
    "education review office",
    "serious fraud office",
    "electoral commission",
    "public service commission",
    "state services commission",
    "government communications security bureau",
    "gcsb",
    "new zealand security intelligence service",
    "nzsis",
    "department of corrections",
    "department of prime minister and cabinet",
    "te arawhiti",
    "health new zealand",
    "te whatu ora",
})

_GOVT_KEYWORDS_RE = re.compile(
    r"\bministry\b"
    r"|\bdepartment\b"
    r"|\bauthority\b"
    r"|\bcouncil\b"
    r"|\boffice\s+of\b"
    r"|\bcrown\b"
    r"|\bcommission\b"
    r"|\bcommissioner\b"
    r"|\btribunal\b",
    re.IGNORECASE,
)


def _is_government_entity(firm_name: str) -> bool:
    """
    Return True if the firm name is a government agency, crown entity,
    or public-sector body. These organisations receive contracts — they should
    never appear as likely bidders.
    """
    if not firm_name:
        return False
    lower = firm_name.lower().strip()
    for explicit in _GOVT_EXPLICIT_NAMES:
        if lower == explicit or lower.startswith(explicit + " "):
            return True
    return bool(_GOVT_KEYWORDS_RE.search(firm_name))


# ── MBIE data availability check ──────────────────────────────────────────────

def _mbie_available() -> bool:
    """Check whether the MBIE supplier_win_history view is populated."""
    try:
        row = db.fetchone("SELECT COUNT(*) as n FROM supplier_win_history")
        return bool(row and row["n"] > 0)
    except Exception:
        return False


# ── Specialist sectors ─────────────────────────────────────────────────────────
# Kept for backward compatibility with portal.py and output.py which import
# _notice_is_specialist.  No longer used for firm filtering.

SPECIALIST_SECTORS: set[str] = {"aerospace", "cybersecurity", "health", "legal"}

SPECIALIST_FLAG_MAP: dict[str, str] = {
    "aerospace":    "aerospace",
    "cybersecurity": "cybersecurity",
    "health":       "health_tech",
    "legal":        "legal",
}


def _notice_is_specialist(notice: dict) -> Optional[str]:
    """
    Return the specialist_flag key if this notice is in a specialist sector,
    or None if it's a general-market notice.
    """
    sector = (notice.get("sector_tag") or "").lower()
    if sector in SPECIALIST_FLAG_MAP:
        return SPECIALIST_FLAG_MAP[sector]
    title = (notice.get("title") or "").lower()
    AEROSPACE_KWS = ["aircraft", "airframe", "propulsion", "avionics", "rnzaf",
                     "rnzn", "aviation", "aerospace", "airworthiness"]
    if any(kw in title for kw in AEROSPACE_KWS):
        return "aerospace"
    CYBER_KWS = ["penetration test", "pen test", "soc services", "siem",
                 "cyber incident", "vulnerability assessment", "iso 27001",
                 "mssp", "managed security", "security operations centre",
                 "managed security services"]
    if any(kw in title for kw in CYBER_KWS):
        return "cybersecurity"
    LEGAL_KWS = ["legal services", "legal advice", "solicitor", "barrister",
                 "counsel", "law firm", "litigation"]
    if any(kw in title for kw in LEGAL_KWS):
        return "legal"
    return None


# ── Firm exclusion helper ──────────────────────────────────────────────────────

def _firm_is_excluded(firm_sectors: list[str], notice: dict,
                      firm_name: str = "") -> bool:
    """
    Return True if this firm should be excluded from bidder results.

    Simplified: only checks government/public-sector entities.
    Sector-matrix filtering has been removed — web search is the sole firm
    identifier and the web search prompt already excludes government bodies and
    out-of-domain firms at source.
    """
    if firm_name and _is_government_entity(firm_name):
        return True
    return False


# ── Web search firm identification ────────────────────────────────────────────

def _web_search_bidders(
    notice_title: str,
    agency: str,
    sector: str,
    overview_text: str = "",
) -> list[dict]:
    """
    Use Claude with web_search to identify commercial providers for the notice.
    Web search is the sole firm identification source in the new architecture.

    When overview_text is supplied, it is used as the primary search anchor so
    that operational specifics (e.g. "RNZN officers navigating naval vessels")
    drive the query rather than the generic notice title.

    Returns a list of dicts compatible with bidder_pool storage,
    with match_type='web_inferred'.
    """
    try:
        import anthropic as _anthropic
        _client = _anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

        overview_clean = (overview_text or "").strip()
        use_overview   = len(overview_clean) > 80

        if use_overview:
            overview_snippet = overview_clean[:500]
            service_block = (
                f"Overview (authoritative — use this as your search anchor):\n"
                f"\"\"\"\n{overview_snippet}\n\"\"\"\n\n"
                f"Notice title (generic label only — do NOT use as the search anchor): '{notice_title}'\n\n"
                f"Extract the most specific service description from the overview — "
                f"the exact type of service, who it serves, and in what operational context — "
                f"and build your web searches from those specifics.\n"
                f"For example: if the overview says 'RNZN officers navigating naval vessels', "
                f"search for 'RNZN naval navigation training New Zealand providers', "
                f"NOT 'Navigation Training Services New Zealand providers'."
            )
        else:
            service_block = (
                f"Service: '{notice_title}'\n\n"
                f"Search for: '{notice_title} New Zealand providers', "
                f"'{notice_title} companies New Zealand government'."
            )

        prompt = (
            f"Find New Zealand commercial companies that provide this service for government contracts.\n\n"
            f"{service_block}\n\n"
            f"This is for a New Zealand government procurement contract. "
            f"Anchor your search on what the service IS — use the operational specifics, "
            f"not the sector or the agency name.\n\n"
            f"Return up to 5 named commercial providers operating in New Zealand. "
            f"For each, provide the exact trading name and a brief description of their capability.\n\n"
            f"Format each entry as: '[Company Name] — [capability description]'\n\n"
            f"IMPORTANT: Only include commercial firms that deliver THIS specific service — "
            f"NOT government agencies, councils, ministries, crown entities, or public sector "
            f"organisations, and NOT firms from adjacent sectors that don't provide this service. "
            f"If you cannot find credible commercial providers, respond with: "
            f"'No providers identified.'"
        )

        msg = _client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=500,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}],
            messages=[{"role": "user", "content": prompt}],
        )

        result_parts = [
            block.text.strip()
            for block in msg.content
            if hasattr(block, "text") and block.text
        ]
        result = " ".join(result_parts).strip()

        if not result or "no providers identified" in result.lower():
            return []

        bidders = []
        for line in result.split("\n"):
            line = line.strip().lstrip("-•*0123456789. ")
            if not line:
                continue
            if " — " in line:
                parts = line.split(" — ", 1)
            elif " - " in line:
                parts = line.split(" - ", 1)
            else:
                continue
            firm_name = parts[0].strip().strip("*_")
            description = parts[1].strip() if len(parts) > 1 else ""
            if not firm_name or len(firm_name) < 3 or len(firm_name) > 120:
                continue
            if _is_government_entity(firm_name):
                logger.debug("Web search: skipping government entity %r", firm_name)
                continue
            bidders.append({
                "firm_name":            firm_name,
                "canonical_name":       canonical_name(firm_name),
                "sector":               sector,
                "size":                 "medium",
                "strategic_importance": "medium",
                "intelligence_maturity": "weak",
                "relevance_score":      0.3,
                "match_type":           "web_inferred",
                "reasoning":            [f"Web search: identified as NZ provider — {description[:120]}"],
                "company_context":      description[:200] or None,
                "context_confidence":   "unknown",
            })

        logger.info(
            "Web search bidders for %r: %d provider(s) found",
            notice_title[:60], len(bidders),
        )
        return bidders[:5]

    except Exception as exc:
        logger.warning("_web_search_bidders failed: %s", exc)
        return []


# ── Combined inference (web search only) ─────────────────────────────────────

def score_bidders_for_notice(
    notice: dict,
    all_bidders: Optional[list] = None,  # ignored — kept for call-site compat
) -> list[dict]:
    """
    Return web-search-identified bidder candidates for a notice.

    Web search is the sole firm identification source.  MBIE and CSV are no
    longer used to generate firm names.  MBIE validation runs per-firm in the
    ACH pipeline (bidder_intelligence.py Stage 2).

    `all_bidders` parameter is accepted but ignored — kept so existing callers
    that pass it do not raise TypeError.
    """
    web_results = _web_search_bidders(
        notice.get("title") or "",
        notice.get("agency") or "",
        notice.get("sector_tag") or "other",
        overview_text=notice.get("overview_text") or notice.get("description") or "",
    )
    filtered = [r for r in web_results if not _is_government_entity(r.get("firm_name", ""))]
    return deduplicate_bidders(filtered)


# ── CSV loader (kept for legacy callers; not used for firm identification) ─────

def load_bidders(csv_path: str = config.BIDDER_CSV_PATH) -> list[dict]:
    """Load bidders.csv.  Not used for firm identification in the current pipeline."""
    path = Path(csv_path)
    if not path.exists():
        logger.warning("Bidder CSV not found at %s", csv_path)
        return []
    bidders = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row["_sectors"] = [s.strip() for s in row.get("sectors", "").split("|") if s.strip()]
            row["_exclude_keywords"] = [
                k.strip().lower()
                for k in row.get("exclude_keywords", "").split("|")
                if k.strip()
            ]
            if not row.get("canonical_name"):
                row["canonical_name"] = canonical_name(row.get("firm_name", ""))
            row["mbie_confirmed"] = (row.get("mbie_confirmed") or "").strip().lower() in ("true", "1", "yes")
            bidders.append(row)
    logger.info("Loaded %d bidders from CSV (%s)", len(bidders), csv_path)
    return bidders


# ── Storage ───────────────────────────────────────────────────────────────────

def _store_bidders(notice_id: str, bidders: list[dict]) -> None:
    for b in bidders:
        reasoning_str = " | ".join(b.get("reasoning") or [])
        stored_name = b.get("canonical_name") or b["firm_name"]
        db.execute(
            """
            INSERT INTO bidder_pool
                (notice_id, firm_name, sector, size,
                 strategic_importance, intelligence_maturity,
                 relevance_score, match_type, reasoning,
                 company_context, context_confidence)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (notice_id, firm_name) DO UPDATE SET
                strategic_importance  = EXCLUDED.strategic_importance,
                intelligence_maturity = EXCLUDED.intelligence_maturity,
                relevance_score       = COALESCE(
                    GREATEST(EXCLUDED.relevance_score, bidder_pool.relevance_score),
                    EXCLUDED.relevance_score),
                match_type            = CASE WHEN bidder_pool.match_type = 'ach_analysis'
                                            THEN bidder_pool.match_type
                                            ELSE EXCLUDED.match_type END,
                reasoning             = CASE WHEN bidder_pool.match_type = 'ach_analysis'
                                            THEN bidder_pool.reasoning
                                            ELSE EXCLUDED.reasoning END
            """,
            (
                notice_id,
                stored_name,
                b.get("sector"),
                b.get("size"),
                b.get("strategic_importance", "low"),
                b.get("intelligence_maturity", "weak"),
                b.get("relevance_score"),
                b.get("match_type"),
                reasoning_str,
                b.get("company_context"),
                b.get("context_confidence", "unknown"),
            ),
        )


# ── Layer 2 hooks ─────────────────────────────────────────────────────────────

def enrich_from_awards_history(notice_id: str, bidder_name: str) -> Optional[dict]:
    """
    Layer 2 hook — queries MBIE for a specific bidder's track record in the
    context of the given notice's sector and agency.
    """
    from historical_data import get_supplier_history, get_agency_win_count

    notice = db.fetchone(
        """
        SELECT r.agency, p.sector_tag
          FROM raw_notices r
          JOIN parsed_notices p ON p.notice_id = r.notice_id
         WHERE r.notice_id = %s
        """,
        (notice_id,),
    )
    if not notice:
        return None

    history = get_supplier_history(bidder_name)
    if not history:
        return None

    agency_wins = get_agency_win_count(
        bidder_name,
        notice.get("agency", ""),
        notice.get("sector_tag", "other"),
    )

    total    = history.get("total_wins", 0)
    last_date = history.get("last_win_date")
    avg_val  = history.get("avg_contract_value")

    if total == 0:
        summary = f"{bidder_name} has no recorded wins in the MBIE dataset for this sector."
    elif agency_wins > 0:
        summary = (
            f"{bidder_name} has won {total} contracts in this sector, "
            f"including {agency_wins} with this specific agency."
        )
    else:
        summary = (
            f"{bidder_name} has won {total} contracts in this sector "
            f"(none recorded with this specific agency)."
        )

    return {
        "past_awards_count": total,
        "last_award_date":   last_date,
        "avg_award_value":   float(avg_val) if avg_val else None,
        "win_rate_this_agency": None,
        "award_evidence":    summary,
    }


def enrich_from_web(bidder_name: str, sector: str) -> Optional[dict]:
    """Layer 2 hook — stub pending Layer 2 Phase 2 implementation."""
    return None


# ── Main entry point ──────────────────────────────────────────────────────────

def run_bidder_inference() -> int:
    """
    Run web-search bidder identification for all high-priority notices not yet
    in bidder_pool.  MBIE and CSV are no longer used for firm identification.
    """
    logger.info("Starting bidder pool inference (web search only)")

    notices = db.fetchall(
        """
        SELECT s.notice_id, p.sector_tag, p.value_band, p.geographic_scope,
               r.title, r.description, r.agency, r.category_raw
          FROM scored_notices s
          JOIN parsed_notices p ON p.notice_id = s.notice_id
          JOIN raw_notices r    ON r.notice_id = s.notice_id
         WHERE (
               s.composite_score >= %s
            OR r.category_raw ILIKE ANY(ARRAY['%%advance%%','%%NOI%%','%%notice of intent%%'])
         )
           AND s.notice_id NOT IN (SELECT DISTINCT notice_id FROM bidder_pool)
         ORDER BY s.composite_score DESC
        """,
        (config.PRIORITY_THRESHOLD,),
    )

    logger.info("%d notices require bidder identification", len(notices))
    count = 0

    for notice in notices:
        try:
            bidders = score_bidders_for_notice(notice)
            if bidders:
                _store_bidders(notice["notice_id"], bidders)
                logger.info(
                    "Stored %d bidders for notice %s (%s)",
                    len(bidders), notice["notice_id"],
                    notice.get("title", "")[:60],
                )
                count += 1
            else:
                logger.debug("No bidders found for notice %s", notice["notice_id"])
        except Exception as exc:
            logger.warning(
                "Bidder inference failed for notice %s: %s",
                notice.get("notice_id"), exc,
            )

    logger.info("Bidder inference complete: %d notices processed", count)
    return count
