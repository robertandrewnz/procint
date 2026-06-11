"""
bidder_intelligence.py — ACH (Analysis of Competing Hypotheses) bidder analysis.

Architecture (redesigned):
  Step 1 — Requirements extraction: lightweight Claude call extracts the specific
            operational, statutory, and licensing requirements this notice demands.
            This grounds the subsequent ACH in capability specifics, not sector labels.

  Step 2 — Pure ACH reasoning: Claude reasons from its knowledge of NZ firms using
            a 4-step structured prompt (capability analysis → firm identification →
            geo/scale fit → ranked output with confidence calibration).
            NO MBIE data is injected here — MBIE would anchor Claude toward
            registered firms regardless of capability fit.

  Step 3 — Category-gated MBIE enrichment: after Claude returns its 3 firms, each
            is cross-checked against MBIE awards. The badge type depends on whether
            MBIE wins are in the SAME category as the notice or a different one:
              • "category_match"    → ✓ MBIE confirmed — N wins in this category
              • "unrelated_category" → ⚠ MBIE present — N wins in unrelated categories
              • "no_mbie"           → Training knowledge — no MBIE record
            An ⚠ badge signals the MBIE data is irrelevant, not confirming.

Confidence calibration rule (enforced in system prompt AND post-hoc):
  "High" probability requires BOTH documented capability AND geographic presence.
  capability_match: "confirmed" = documented council contracts in this service
                   "inferred"  = adjacent capability, plausible transfer
                   "unknown"   = sector presence only

Caching:
  Results stored in bidder_pool with match_type='ach_analysis'.
  context_confidence column stores badge type + wins: "category_match:5" etc.
  Staleness check compares notice parsed_at vs stored timestamp marker.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
from typing import Optional

import config
import db

logger = logging.getLogger(__name__)

# Probability band → display colour
PROBABILITY_COLOURS = {
    "High":        "#2a9d8f",   # teal
    "Medium":      "#d4a017",   # amber
    "Medium-Low":  "#e07b39",   # orange
    "Low":         "#8fa3bc",   # muted
}


# ── CSV candidate pool (for ICT / Cybersecurity seeding) ──────────────────────

_CSV_CANDIDATES: dict[str, list[str]] = {}


def _load_csv_candidates() -> dict[str, list[str]]:
    """
    Lazily load bidders.csv and index firm names by sector tag.
    Returns a dict keyed by lowercase sector string.
    """
    global _CSV_CANDIDATES
    if _CSV_CANDIDATES:
        return _CSV_CANDIDATES
    csv_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "bidders.csv"
    )
    result: dict[str, list[str]] = {}
    try:
        with open(csv_path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("canonical_name") or row.get("firm_name") or "").strip()
                if not name:
                    continue
                for sec in (row.get("sectors") or "").split("|"):
                    sec = sec.strip().lower()
                    if sec:
                        result.setdefault(sec, []).append(name)
        _CSV_CANDIDATES = result
        logger.debug("Loaded CSV candidates for %d sector keys", len(result))
    except Exception as exc:
        logger.warning("Could not load bidders.csv candidates: %s", exc)
    return result


def _sector_candidates(sector_tag: str) -> list[str]:
    """
    Return known NZ firms from bidders.csv that are relevant to ICT or
    Cybersecurity notices.  Returns empty list for all other sectors.
    """
    s = (sector_tag or "").lower()
    if not any(k in s for k in ("ict", "cyber", "security", "defence")):
        return []
    candidates = _load_csv_candidates()
    seen: set[str] = set()
    firms: list[str] = []
    for sec_key in ("cybersecurity", "ict", "security"):
        for f in candidates.get(sec_key, []):
            if f not in seen:
                seen.add(f)
                firms.append(f)
    return firms[:30]


# ── Step 1: Requirements extraction ───────────────────────────────────────────

_REQUIREMENTS_PROMPT = """\
Extract the specific operational, licensing, and statutory requirements this NZ government contract demands. Be precise — state what actual licences, designations, infrastructure, or specialist capabilities are required, not generic sector labels.

Notice title: {title}
Agency: {agency}
Description: {description}

Return ONLY valid JSON with no markdown:
{{"requirements": ["specific capability 1", "specific capability 2"],
  "statutory_obligations": ["Dog Control Act officer designation", "Security Guard licence", etc — only if applicable],
  "geographic_scope": "brief description of delivery area",
  "scale_indicators": "population size, area km², hours, or other scale signals"}}"""


def _extract_requirements(notice: dict) -> dict:
    """
    Run a lightweight Claude call to extract structured capability requirements.
    Returns a dict with: requirements, statutory_obligations, geographic_scope,
    scale_indicators. Falls back to empty dict on failure (ACH still runs).
    """
    title       = notice.get("title") or ""
    agency      = notice.get("agency") or notice.get("agency_name") or ""
    description = (notice.get("description") or "")[:800]

    prompt = _REQUIREMENTS_PROMPT.format(
        title=title,
        agency=agency,
        description=description or "Not provided",
    )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        result = json.loads(raw)
        logger.debug("Requirements extracted for %s: %s",
                     notice.get("notice_id", "?"), result)
        return result
    except Exception as exc:
        logger.warning("Requirements extraction failed for %s: %s",
                       notice.get("notice_id", "?"), exc)
        return {
            "requirements": [],
            "statutory_obligations": [],
            "geographic_scope": notice.get("geographic_scope") or "Not specified",
            "scale_indicators": notice.get("value_band") or "unknown",
        }


def _format_requirements_summary(reqs: dict) -> str:
    """Convert requirements dict to a compact string for the ACH prompt."""
    parts = []
    if reqs.get("requirements"):
        parts.append("Required capabilities: " + "; ".join(reqs["requirements"]))
    if reqs.get("statutory_obligations"):
        parts.append("Statutory/licensing: " + "; ".join(reqs["statutory_obligations"]))
    if reqs.get("geographic_scope"):
        parts.append("Geographic scope: " + reqs["geographic_scope"])
    if reqs.get("scale_indicators"):
        parts.append("Scale: " + reqs["scale_indicators"])
    return "\n".join(parts) if parts else "Requirements not extracted."


# ── Step 2: Pure ACH reasoning (no MBIE context injected) ─────────────────────

_ACH_SYSTEM = """\
You are a New Zealand government procurement intelligence analyst. Identify the 3 most \
likely bidding organisations for a specific NZ government contract using the Analysis of \
Competing Hypotheses (ACH) framework.

Your reasoning MUST follow these four steps in sequence:

STEP 1 — CAPABILITY ANALYSIS
State precisely what operational, technical, and statutory capabilities this contract demands. \
Do not use generic sector labels. For example: not "security services" but \
"Dog Control Act enforcement officer designation, animal impounding infrastructure, \
after-hours patrol SLA compliance, rural district coverage across X km²". \
State exactly what licences, designations, or specialist infrastructure are required.

STEP 2 — FIRM IDENTIFICATION
Identify NZ firms that demonstrably have those SPECIFIC capabilities based on:
- Known service delivery in this exact service category (not adjacent sectors)
- Documented council or government contracts in this specific type of work
- Public capability statements or known operational presence
Do NOT include firms active in an adjacent sector without the specific capability. \
A corporate IT security firm is NOT a match for animal control without documented council \
animal control contracts. A surveillance technology vendor is NOT a match for field patrol services. \
A corrections-technology firm is NOT a match for local government enforcement work.
IMPORTANT: The contracting agency is the BUYER, not a bidder. Never list the council or \
government agency as a bidder. If you believe the incumbent is unknown, say \
"Unknown incumbent (likely regional contractor)" rather than naming the council.

STEP 3 — GEOGRAPHIC AND SCALE FIT
For each candidate:
- Geographic coverage: does this firm deliver services in this specific district or region?
- Scale fit: small district councils (under 50,000 population) are often better served by \
regional operators than major national firms who may price themselves out or deprioritise small contracts.
- Incumbency signals: any public signals of an existing operator in this area?

STEP 4 — CONFIDENCE CALIBRATION AND RANKING
Apply this rule strictly:
- "High" ONLY if the firm has BOTH documented capability in this specific service type \
AND confirmed geographic presence. If either is uncertain → maximum "Medium".
- "Medium" = capability inferred from adjacent work OR capability confirmed but geography uncertain.
- "Medium-Low" = sector presence but no evidence of this specific capability, or \
working from title/agency alone without full specification.

capability_match values:
- "confirmed": documented council or government contracts in this exact service category
- "inferred": adjacent capability with plausible transfer, but not confirmed for this service type
- "unknown": operates in the broad sector but no evidence of this specific service

CRITICAL INSTRUCTION: You MUST always return exactly 3 bidder hypotheses. Even when the \
contract notice is sparse or lacks a description, produce your best 3 hypotheses based on \
the notice title, agency name, region, and your knowledge of the NZ market for this service type. \
When data is limited, set probability to "Medium-Low" and capability_match to "unknown" — \
but still name real NZ organisations. Never return an error, refusal, or fewer than 3 bidders. \
If you are uncertain about a firm, use "Medium-Low / unknown" rather than omitting them.

Return ONLY valid JSON — no text outside the JSON block:
{"bidders": [\
{"name": str, "probability": "High"|"Medium"|"Medium-Low", \
"capability_match": "confirmed"|"inferred"|"unknown", \
"evidence": [str, str], "discriminator": str, \
"size": "small"|"medium"|"large"|"major"}\
]}"""


def _build_ach_prompt(notice: dict, requirements_summary: str) -> str:
    title       = notice.get("title") or ""
    agency      = notice.get("agency") or notice.get("agency_name") or ""
    region      = notice.get("geographic_scope") or notice.get("region") or "Not specified"
    sector      = notice.get("sector_tag") or notice.get("sector") or "other"
    description = (notice.get("description") or "")[:1200]
    value_band  = notice.get("value_band") or "unknown"

    value_labels = {
        "under_100k": "Under $100K", "100k_500k": "$100K–$500K",
        "500k_2m": "$500K–$2M",      "2m_10m": "$2M–$10M",
        "10m_plus": "$10M+",          "unknown": "Value not specified",
    }
    value_str = value_labels.get(value_band, value_band)

    # Inject known NZ firm list for ICT / Cybersecurity notices.
    # Government cybersecurity contracts are often not published in MBIE award data,
    # so Claude's training knowledge must be anchored with the known NZ market players.
    candidates_block = ""
    candidates = _sector_candidates(sector)
    if candidates:
        candidates_block = (
            "\n\nKNOWN NZ ICT/CYBERSECURITY FIRMS (from NZ procurement database — "
            "evaluate each against the specific requirements above and include only "
            "those with genuine capability match; do not include all firms by default):\n"
            + "\n".join(f"- {c}" for c in candidates)
        )

    return (
        f"Notice title: {title}\n"
        f"Agency: {agency}\n"
        f"Region / scope: {region}\n"
        f"Sector classification: {sector}\n"
        f"Contract value: {value_str}\n\n"
        f"Specific requirements extracted:\n{requirements_summary}\n\n"
        f"Full description: {description or 'Not provided'}"
        f"{candidates_block}"
    )


_GARBAGE_NAME_FRAGMENTS = (
    "unable to rank",
    "analysis_status",
    "cannot identify",
    "no specific firm",
    "error:",
    "insufficient data for",
)

# Common suffixes/abbreviations to strip when comparing agency vs bidder names
_AGENCY_STOP_WORDS = frozenset({
    "district", "city", "regional", "council", "dc", "cc", "rc",
    "limited", "ltd", "inc", "trust", "board", "authority",
    "new", "zealand", "nz", "the", "of", "and",
})


def _normalise_for_match(name: str) -> str:
    """
    Strip punctuation and common stop-words, lowercase, return token set.
    Used for fuzzy agency vs bidder name comparison.
    """
    tokens = re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
    return " ".join(t for t in tokens if t not in _AGENCY_STOP_WORDS and len(t) > 1)


def _is_agency_name(bidder_name: str, agency_name: str) -> bool:
    """
    Return True if *bidder_name* looks like it IS the contracting agency.

    Catches:
      - Exact substring match after normalisation ("Tararua District Council" in firm name)
      - Known abbreviation patterns: "TDC", "WCC", "ACC", "HCC", etc.
      - Partial overlap: if ≥2 meaningful tokens from agency appear in bidder name
    """
    if not agency_name or not bidder_name:
        return False

    b_lower = bidder_name.lower().strip()
    a_lower = agency_name.lower().strip()

    # Direct containment (case-insensitive)
    if a_lower in b_lower or b_lower in a_lower:
        return True

    # Normalised token overlap
    b_norm = _normalise_for_match(bidder_name)
    a_norm = _normalise_for_match(agency_name)
    if not a_norm or not b_norm:
        return False

    # Full match after normalisation (exact only — substring would over-match)
    if a_norm == b_norm:
        return True

    # Token overlap: ≥2 significant tokens shared
    b_tokens = set(b_norm.split())
    a_tokens = set(a_norm.split())
    shared = b_tokens & a_tokens
    if len(shared) >= 2:
        return True

    # Initialisms: "TDC" matches "Tararua District Council", "WCC" → "Wellington City Council"
    # Use ALL words (not filtered) so "T-D-C" generates from "Tararua District Council"
    # Skip if bidder has a parenthetical expansion — that signals a different named entity
    # (e.g. "ACC (Accident Compensation Corporation)" has its own expansion, not the agency's)
    has_expansion = bool(re.search(r"\([a-z]{4,}", b_lower))
    if not has_expansion:
        all_words = [t for t in re.sub(r"[^a-z0-9 ]", " ", a_lower).split() if len(t) > 1]
        if all_words:
            initialism = "".join(w[0] for w in all_words)
            # 2- to 4-letter initialism must appear as a standalone word/token in bidder name
            if 2 <= len(initialism) <= 4:
                pattern = r"(?<![a-z])" + re.escape(initialism) + r"(?![a-z])"
                if re.search(pattern, b_lower):
                    return True

    return False


def _extract_json_from_response(raw: str) -> dict:
    """
    Robust JSON extractor. Claude sometimes wraps JSON in fences and then adds
    prose analysis afterward. This finds the first { ... } block in the response.
    """
    # Try fence-stripped parse first
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    # Take only up to the first line that is just ``` (closing fence)
    # to avoid ingesting prose after the JSON block
    lines = cleaned.split("\n")
    json_lines = []
    for line in lines:
        if line.strip() == "```":
            break
        json_lines.append(line)
    cleaned = "\n".join(json_lines).strip()

    # Try parsing the cleaned block
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Fallback: find outermost { ... } pair in original raw
    start = raw.find("{")
    end   = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            pass

    return {}


def _is_garbage_name(name: str) -> bool:
    """Return True if the bidder name is a placeholder / non-firm string."""
    name_lower = name.lower()
    return any(frag in name_lower for frag in _GARBAGE_NAME_FRAGMENTS)


def _run_ach_reasoning(notice: dict, requirements_summary: str) -> list[dict]:
    """
    Call Claude with the 4-step ACH prompt. Returns raw bidder list from Claude
    with no MBIE enrichment applied yet.

    Returns [] on any failure — caller must fall back to MBIE inference.
    Distinct log messages identify the failure mode so zero-bidder notices
    can be diagnosed from logs.
    """
    notice_id = notice.get("notice_id", "?")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    except Exception as exc:
        logger.warning(
            "ACH skip notice %s: Anthropic client init failed — %s "
            "(caller will fall back to MBIE)",
            notice_id, exc,
        )
        return []

    # --- Claude API call ---
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1400,
            system=_ACH_SYSTEM,
            messages=[{
                "role": "user",
                "content": _build_ach_prompt(notice, requirements_summary),
            }],
        )
    except Exception as api_exc:
        logger.warning(
            "ACH skip notice %s: Claude API call failed — %s "
            "(caller will fall back to MBIE)",
            notice_id, api_exc,
        )
        return []

    # --- JSON extraction ---
    raw = resp.content[0].text.strip()
    data = _extract_json_from_response(raw)
    if not data:
        logger.warning(
            "ACH skip notice %s: JSON extraction failed (raw length=%d, "
            "preview=%.120r) — caller will fall back to MBIE",
            notice_id, len(raw), raw,
        )
        return []

    bidders = data.get("bidders", [])
    if not bidders:
        logger.warning(
            "ACH skip notice %s: Claude returned valid JSON but 'bidders' "
            "list is empty — caller will fall back to MBIE",
            notice_id,
        )
        return []

    # --- Garbage-name filter ---
    before = len(bidders)
    bidders = [b for b in bidders if not _is_garbage_name(str(b.get("name") or ""))]
    dropped = before - len(bidders)
    if dropped:
        logger.info(
            "ACH notice %s: filtered %d garbage-name bidder(s) "
            "(%d remain after filter)",
            notice_id, dropped, len(bidders),
        )
    if not bidders:
        logger.warning(
            "ACH skip notice %s: all %d bidder(s) from Claude were "
            "garbage/placeholder names — caller will fall back to MBIE",
            notice_id, before,
        )

    return bidders


# ── Step 3: Category-gated MBIE enrichment ────────────────────────────────────

def _mbie_confirmation(firm_name: str, notice_sector: str) -> tuple[str, int, str]:
    """
    Check MBIE award history for *firm_name* with category gating.

    Returns:
        (badge_type, wins_count, extra_info)

        badge_type:
          "category_match"     — firm has MBIE wins in the SAME sector as the notice
          "unrelated_category" — firm has MBIE wins but in DIFFERENT sectors
          "no_mbie"            — no MBIE record found
        wins_count: number of awards
        extra_info: sector tags for unrelated wins (for display)
    """
    firm_word = (firm_name.split()[0] if firm_name else "").lower()
    if not firm_word:
        return "no_mbie", 0, ""

    try:
        # Same-sector wins
        same_row = db.fetchone(
            """
            SELECT COUNT(DISTINCT n.rfx_id) AS wins
              FROM mbie_award_notices n
              JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
              JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
             WHERE n.is_awarded
               AND LOWER(s.business_name) LIKE LOWER(%s)
               AND c.sector_tag = %s
            """,
            (f"%{firm_word}%", notice_sector),
        )
        same_wins = int((same_row or {}).get("wins") or 0)
        if same_wins > 0:
            return "category_match", same_wins, notice_sector

        # Wins in any other category
        any_row = db.fetchone(
            """
            SELECT COUNT(DISTINCT n.rfx_id) AS wins,
                   STRING_AGG(DISTINCT c.sector_tag, ', ' ORDER BY c.sector_tag) AS sectors
              FROM mbie_award_notices n
              JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
              JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
             WHERE n.is_awarded
               AND LOWER(s.business_name) LIKE LOWER(%s)
            """,
            (f"%{firm_word}%",),
        )
        any_wins = int((any_row or {}).get("wins") or 0)
        other_sectors = str((any_row or {}).get("sectors") or "")

        if any_wins > 0:
            return "unrelated_category", any_wins, other_sectors

        return "no_mbie", 0, ""

    except Exception as exc:
        logger.warning("MBIE confirmation check failed for '%s': %s", firm_name, exc)
        return "no_mbie", 0, ""


def _apply_mbie_enrichment(
    bidders_raw: list[dict],
    notice_sector: str,
    agency_name: str = "",
) -> list[dict]:
    """
    Apply category-gated MBIE badges to each Claude-identified bidder.
    Does NOT modify Claude's probability rankings — MBIE is informational only.

    agency_name: the contracting agency for this notice.  Any bidder whose name
    matches or closely resembles the agency is excluded — the buyer cannot also
    be a bidder.
    """
    results = []
    for b in bidders_raw[:3]:
        name = str(b.get("name") or "").strip()
        if not name:
            continue
        # Exclude the contracting agency itself
        if agency_name and _is_agency_name(name, agency_name):
            logger.info(
                "ACH: excluded '%s' — matches contracting agency '%s'",
                name, agency_name,
            )
            continue

        prob = b.get("probability", "Medium")
        if prob not in PROBABILITY_COLOURS:
            prob = "Medium"

        capability_match = b.get("capability_match", "unknown")
        if capability_match not in ("confirmed", "inferred", "unknown"):
            capability_match = "unknown"

        evidence      = [str(e) for e in (b.get("evidence") or [])[:3]]
        discriminator = str(b.get("discriminator") or "")[:300]
        size          = b.get("size", "medium")

        badge_type, wins, extra = _mbie_confirmation(name, notice_sector)

        # Encode badge info in context_confidence column: "badge_type:N"
        conf_str = f"{badge_type}:{wins}"

        # Post-hoc confidence calibration:
        # If Claude assigned "High" but capability_match is not "confirmed", downgrade.
        if prob == "High" and capability_match != "confirmed":
            prob = "Medium"
            evidence = evidence + [
                "Probability capped at Medium — capability match is inferred, "
                "not confirmed for this specific service type"
            ]
            evidence = evidence[:3]

        results.append({
            "name":             name,
            "probability":      prob,
            "capability_match": capability_match,
            "evidence":         evidence,
            "discriminator":    discriminator,
            "size":             size,
            "mbie_wins":        wins,
            "mbie_badge_type":  badge_type,
            "mbie_extra":       extra,
            "conf_str":         conf_str,
            "match_type":       "ach_analysis",
        })

    return results


# ── MBIE fallback ─────────────────────────────────────────────────────────────

def _mbie_fallback_bidders(notice: dict) -> list[dict]:
    """
    MBIE/CSV inference fallback — called when the ACH Claude call returns no
    usable results (API failure, JSON parse failure, or empty bidder list).

    Runs score_bidders_for_notice() from bidders.py and converts the results
    to the ACH-compatible dict format so they can be stored and rendered via
    store_ach_results() / render_ach_card().
    """
    notice_id = notice.get("notice_id", "?")
    logger.info(
        "ACH fallback: running MBIE inference for notice %s "
        "(sector=%s, agency=%s)",
        notice_id,
        notice.get("sector_tag") or "other",
        notice.get("agency") or "",
    )
    try:
        from bidders import score_bidders_for_notice
        mbie_results = score_bidders_for_notice(notice)
    except Exception as exc:
        logger.error(
            "ACH fallback: MBIE inference also failed for notice %s: %s",
            notice_id, exc,
        )
        return []

    if not mbie_results:
        logger.warning(
            "ACH fallback: MBIE inference returned 0 bidders for notice %s",
            notice_id,
        )
        return []

    out: list[dict] = []
    for b in mbie_results[:3]:
        name = (b.get("canonical_name") or b.get("firm_name") or "").strip()
        if not name:
            continue

        # Map MBIE strategic_importance to ACH probability bands
        si = (b.get("strategic_importance") or "medium").lower()
        prob_map = {
            "high": "Medium", "medium": "Medium", "low": "Medium-Low",
            "strategic": "Medium", "opportunistic": "Medium-Low",
        }
        prob = prob_map.get(si, "Medium-Low")

        # Adapt context_confidence from legacy "high"/"low" format
        conf = (b.get("context_confidence") or "no_mbie:0").strip()
        if conf == "high":
            conf = "category_match:1"
        elif conf == "low" or ":" not in conf:
            conf = "no_mbie:0"

        reasoning_parts = b.get("reasoning") or []
        if isinstance(reasoning_parts, str):
            reasoning_parts = [reasoning_parts]

        out.append({
            "name":             name,
            "probability":      prob,
            "capability_match": "inferred",
            "evidence":         [str(r) for r in reasoning_parts[:2]]
                                or ["MBIE historical award data"],
            "discriminator":    "MBIE-only fallback — ACH Claude unavailable at inference time",
            "size":             b.get("size") or "medium",
            "mbie_wins":        0,
            "mbie_badge_type":  conf.split(":")[0],
            "mbie_extra":       "",
            "conf_str":         conf,
            "match_type":       "ach_analysis",
        })

    logger.info(
        "ACH fallback: produced %d MBIE bidder(s) for notice %s — %s",
        len(out),
        notice_id,
        ", ".join(r["name"] for r in out),
    )
    return out


# ── Main ACH function ──────────────────────────────────────────────────────────

def generate_bidder_intelligence(
    notice: dict,
    show_reasoning: bool = False,
) -> list[dict]:
    """
    Run ACH bidder analysis for *notice* using Claude.

    Three-step pipeline:
      1. Extract specific capability requirements (separate Claude call)
      2. Pure ACH reasoning — no MBIE context injected (but ICT/Cyber notices
         receive a candidate list from bidders.csv to anchor Claude's hypotheses)
      3. Category-gated MBIE enrichment applied post-hoc

    On any Claude API failure, JSON parse failure, or empty result, falls back
    to MBIE-only inference rather than returning zero bidders.  Every skip
    decision is logged with a specific reason.

    Args:
        notice: Dict with at minimum: title, agency, sector_tag, value_band.
                description and geographic_scope significantly improve quality.
                category_raw improves tender-type logging.
        show_reasoning: If True, log requirements + raw ACH output at INFO level.

    Returns:
        List of up to 3 bidder dicts.  Falls back to MBIE results on failure.
    """
    notice_id     = notice.get("notice_id", "?")
    notice_sector = notice.get("sector_tag") or notice.get("sector") or "other"
    agency_name   = notice.get("agency") or notice.get("agency_name") or ""
    tender_type   = notice.get("category_raw") or "unknown"

    logger.info(
        "ACH starting for notice %s (sector=%s, tender_type=%s, agency=%s)",
        notice_id, notice_sector, tender_type, agency_name,
    )

    # Step 1 — Extract capability requirements
    reqs = _extract_requirements(notice)
    requirements_summary = _format_requirements_summary(reqs)
    if not reqs.get("requirements"):
        logger.info(
            "ACH notice %s: requirements extraction returned empty list "
            "(Step 1 fell back to defaults) — ACH will run on title/description only",
            notice_id,
        )

    if show_reasoning:
        logger.info("=== REQUIREMENTS for %s ===\n%s", notice_id, requirements_summary)

    # Step 2 — Pure ACH reasoning (no MBIE context; ICT/Cyber get candidate seeds)
    bidders_raw = _run_ach_reasoning(notice, requirements_summary)

    if show_reasoning:
        logger.info("=== RAW ACH OUTPUT for %s ===\n%s",
                    notice_id, json.dumps(bidders_raw, indent=2))

    if not bidders_raw:
        # _run_ach_reasoning already logged the specific failure reason
        return _mbie_fallback_bidders(notice)

    # Step 3 — Category-gated MBIE enrichment (agency-as-bidder filtered here)
    pre_enrichment_count = len(bidders_raw)
    results = _apply_mbie_enrichment(bidders_raw, notice_sector, agency_name=agency_name)

    if not results:
        logger.warning(
            "ACH notice %s: all %d Claude bidder(s) removed by enrichment/agency filter "
            "(agency='%s') — falling back to MBIE inference",
            notice_id, pre_enrichment_count, agency_name,
        )
        return _mbie_fallback_bidders(notice)

    logger.info(
        "ACH complete for notice %s: %d bidder(s) — %s",
        notice_id,
        len(results),
        ", ".join(
            f"{r['name']} ({r['probability']}, cap:{r['capability_match']}, "
            f"mbie:{r['mbie_badge_type']})"
            for r in results
        ),
    )
    return results


# ── Caching / persistence ──────────────────────────────────────────────────────

def _ach_is_stale(notice_id: str) -> bool:
    """
    Return True if ACH analysis needs to be (re-)generated.
    Uses the 'company_context' column as a staleness marker (stores parsed_at ISO).
    """
    try:
        ach_row = db.fetchone(
            """
            SELECT company_context
              FROM bidder_pool
             WHERE notice_id = %s AND match_type = 'ach_analysis'
             LIMIT 1
            """,
            (notice_id,),
        )
        if not ach_row:
            return True

        parsed = db.fetchone(
            "SELECT parsed_at FROM parsed_notices WHERE notice_id = %s",
            (notice_id,),
        )
        if not parsed:
            return False

        ach_ts   = (ach_row.get("company_context") or "")[:19]
        parse_ts = str(parsed.get("parsed_at") or "")[:19]
        return parse_ts > ach_ts
    except Exception as exc:
        logger.warning("ACH staleness check failed for %s: %s", notice_id, exc)
        return True


def store_ach_results(notice_id: str, bidders: list[dict]) -> None:
    """
    Persist ACH bidder results to bidder_pool.
    Encoding:
      - reasoning: "CAPMATCH:{match}| evidence1 | evidence2 | ⚡ discriminator"
      - context_confidence: "badge_type:N" (e.g. "category_match:3")
    """
    if not bidders:
        return

    try:
        row = db.fetchone(
            "SELECT parsed_at FROM parsed_notices WHERE notice_id = %s",
            (notice_id,),
        )
        ts_marker = str(row["parsed_at"])[:19] if row and row.get("parsed_at") else ""

        # Delete ALL existing ach_analysis rows for this notice before inserting
        # new ones. Previous firm-name-only deletion left stale rows when Claude
        # named different firms on a re-run (old names were never cleaned up).
        db.execute(
            "DELETE FROM bidder_pool WHERE notice_id = %s AND match_type = 'ach_analysis'",
            (notice_id,),
        )

        for rank, b in enumerate(bidders, 1):
            cap_match = b.get("capability_match", "unknown")
            evidence_parts = b.get("evidence") or []
            discriminator  = b.get("discriminator") or ""

            # Encode capability_match as first token so renderer can parse it
            reasoning_parts = [f"CAPMATCH:{cap_match}"] + [
                str(e) for e in evidence_parts
            ]
            if discriminator:
                reasoning_parts.append(f"⚡ {discriminator}")
            reasoning_str = " | ".join(reasoning_parts)

            db.execute(
                """
                INSERT INTO bidder_pool
                    (notice_id, firm_name, sector, size,
                     strategic_importance, intelligence_maturity,
                     relevance_score, match_type, reasoning,
                     company_context, context_confidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    notice_id,
                    b["name"],
                    "",
                    b.get("size", "medium"),
                    b["probability"],
                    "ach",
                    round(3.0 - (rank - 1) * 0.5, 1),
                    "ach_analysis",
                    reasoning_str[:2000],
                    ts_marker,
                    b.get("conf_str", "no_mbie:0"),
                ),
            )
        logger.info("Stored %d ACH bidders for notice %s", len(bidders), notice_id)
    except Exception as exc:
        logger.error("store_ach_results failed for %s: %s", notice_id, exc)


# ── Batch runner ───────────────────────────────────────────────────────────────

def run_ach_for_enriched(force: bool = False) -> dict:
    """
    Run ACH analysis on all notices with enriched_notices entries.

    Note on tender type filtering: this runner has NO tender-type exclusions.
    Notice of Intent and Advance Notice tender types are NOT skipped — they
    receive ACH analysis if they have been enriched.  The category_raw field
    is included in each notice dict for logging and prompt context only.
    (Tender type filtering in the legacy MBIE batch runner in bidders.py line ~857
    explicitly INCLUDES those types via an OR clause; this runner is consistent
    with that intent.)
    """
    enriched_ids = db.fetchall(
        """
        SELECT e.notice_id, r.title, r.agency, r.description,
               p.sector_tag, p.value_band, p.geographic_scope,
               r.category_raw
          FROM enriched_notices e
          JOIN raw_notices r ON r.notice_id = e.notice_id
          JOIN parsed_notices p ON p.notice_id = e.notice_id
         ORDER BY e.enriched_at DESC
        """
    )

    counts = {"processed": 0, "skipped": 0, "failed": 0}

    for row in enriched_ids:
        nid = row["notice_id"]
        if not force and not _ach_is_stale(nid):
            counts["skipped"] += 1
            logger.debug("ACH skip (not stale): %s", nid)
            continue

        try:
            notice = {
                "notice_id":        nid,
                "title":            row.get("title") or "",
                "agency":           row.get("agency") or "",
                "description":      row.get("description") or "",
                "sector_tag":       row.get("sector_tag") or "other",
                "value_band":       row.get("value_band") or "unknown",
                "geographic_scope": row.get("geographic_scope"),
                "category_raw":     row.get("category_raw") or "",
            }
            bidders = generate_bidder_intelligence(notice)
            if bidders:
                store_ach_results(nid, bidders)
                counts["processed"] += 1
            else:
                logger.warning(
                    "ACH batch: zero bidders returned for notice %s "
                    "(both ACH and MBIE fallback exhausted)",
                    nid,
                )
                counts["failed"] += 1
        except Exception as exc:
            logger.error("ACH batch failed for %s: %s", nid, exc)
            counts["failed"] += 1

    logger.info(
        "ACH batch complete — processed=%d skipped=%d failed=%d",
        counts["processed"], counts["skipped"], counts["failed"],
    )
    return counts


# ── Rendering ──────────────────────────────────────────────────────────────────

def _parse_conf_str(conf_str: str) -> tuple[str, int]:
    """Parse "badge_type:N" from context_confidence column."""
    if not conf_str:
        return "no_mbie", 0
    parts = conf_str.split(":", 1)
    badge_type = parts[0] if parts else "no_mbie"
    try:
        wins = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        wins = 0
    # Handle legacy "high"/"low" values from the old system
    if badge_type in ("high", "low"):
        badge_type = "category_match" if badge_type == "high" else "no_mbie"
        wins = 0
    return badge_type, wins


def _mbie_badge_html(badge_type: str, wins: int) -> str:
    """Render the MBIE source badge based on category-gating result."""
    if badge_type == "category_match":
        n = f"{wins} award{'s' if wins != 1 else ''}"
        return (
            f'<span style="font-size:.6rem;font-weight:700;letter-spacing:.06em;'
            f'padding:.1rem .45rem;border-radius:3px;'
            f'background:rgba(42,157,143,.15);color:#2a9d8f;white-space:nowrap;">'
            f'✓ MBIE confirmed — {n} in category</span>'
        )
    elif badge_type == "unrelated_category":
        n = f"{wins} award{'s' if wins != 1 else ''}"
        return (
            f'<span style="font-size:.6rem;font-weight:700;letter-spacing:.06em;'
            f'padding:.1rem .45rem;border-radius:3px;'
            f'background:rgba(212,160,23,.15);color:#d4a017;white-space:nowrap;">'
            f'⚠ MBIE present — {n} in other categories</span>'
        )
    else:
        return (
            f'<span style="font-size:.6rem;font-weight:700;letter-spacing:.06em;'
            f'padding:.1rem .45rem;border-radius:3px;'
            f'background:rgba(143,163,188,.12);color:#8fa3bc;white-space:nowrap;">'
            f'Training knowledge</span>'
        )


def _capability_badge_html(capability_match: str) -> str:
    """Small inline badge showing capability_match level."""
    styles = {
        "confirmed": ("rgba(42,157,143,.1)",  "#2a9d8f", "Capability confirmed"),
        "inferred":  ("rgba(212,160,23,.1)",  "#d4a017", "Capability inferred"),
        "unknown":   ("rgba(143,163,188,.1)", "#8fa3bc", "Capability unknown"),
    }
    bg, fg, label = styles.get(capability_match, styles["unknown"])
    return (
        f'<span style="font-size:.58rem;font-weight:700;letter-spacing:.05em;'
        f'padding:.08rem .35rem;border-radius:3px;'
        f'background:{bg};color:{fg};">{label}</span>'
    )


def render_ach_card(b: dict) -> str:
    """
    Render one ACH bidder as an HTML card.
    Reads context_confidence for category-gated MBIE badge.
    Parses CAPMATCH: prefix from reasoning for capability_match display.
    """
    name       = b.get("firm_name") or b.get("name") or "—"
    prob       = b.get("strategic_importance") or b.get("probability") or "Medium"
    colour     = PROBABILITY_COLOURS.get(prob, "#8fa3bc")
    size_raw   = b.get("size") or "medium"
    size_label = size_raw.capitalize()

    # Parse MBIE badge
    conf_str   = b.get("context_confidence") or "no_mbie:0"
    badge_type, wins = _parse_conf_str(conf_str)
    mbie_badge = _mbie_badge_html(badge_type, wins)

    # Parse reasoning: "CAPMATCH:{match} | evidence1 | evidence2 | ⚡ discriminator"
    reasoning_raw = b.get("reasoning") or ""
    parts = [r.strip() for r in reasoning_raw.split("|") if r.strip()]

    capability_match = "unknown"
    bullets: list[str] = []
    discriminator = ""

    for part in parts:
        if part.startswith("CAPMATCH:"):
            capability_match = part[len("CAPMATCH:"):].strip()
        elif part.startswith("⚡"):
            discriminator = part[1:].strip()
        else:
            bullets.append(part)

    cap_badge = _capability_badge_html(capability_match)

    bullets_html = "".join(
        f'<div style="font-size:.76rem;color:var(--text);line-height:1.5;'
        f'padding:.18rem 0;display:flex;gap:.4rem;">'
        f'<span style="color:var(--gold);flex-shrink:0;">•</span>'
        f'<span>{bullet}</span></div>'
        for bullet in bullets[:3]
    )
    discriminator_html = (
        f'<div style="font-size:.72rem;color:var(--muted);font-style:italic;'
        f'margin-top:.35rem;line-height:1.45;">⚡ {discriminator}</div>'
        if discriminator else ""
    )

    return (
        f'<div style="background:var(--surf2);border:1px solid var(--card-border);'
        f'border-radius:7px;padding:.75rem .9rem;margin-bottom:.55rem;">'
        # Header row: name + MBIE badge
        f'<div style="display:flex;justify-content:space-between;align-items:flex-start;'
        f'gap:.5rem;margin-bottom:.35rem;">'
        f'<span style="font-size:.83rem;font-weight:700;color:var(--text);">{name}</span>'
        f'{mbie_badge}'
        f'</div>'
        # Probability pill + size + capability badge
        f'<div style="display:flex;align-items:center;gap:.45rem;flex-wrap:wrap;margin-bottom:.5rem;">'
        f'<span style="font-size:.68rem;font-weight:700;letter-spacing:.05em;'
        f'text-transform:uppercase;padding:.12rem .5rem;border-radius:4px;'
        f'background:{colour}22;color:{colour};border:1px solid {colour}44;">'
        f'{prob}</span>'
        f'<span style="font-size:.68rem;color:var(--muted);">{size_label}</span>'
        f'{cap_badge}'
        f'</div>'
        # Evidence bullets + discriminator
        f'{bullets_html}'
        f'{discriminator_html}'
        f'</div>'
    )


def render_mbie_stub(notice_id: str) -> str:
    """Stub shown when ACH hasn't run for this notice yet."""
    try:
        rows = db.fetchall(
            """
            SELECT firm_name, match_type, reasoning, strategic_importance
              FROM bidder_pool
             WHERE notice_id = %s AND match_type != 'ach_analysis'
             ORDER BY relevance_score DESC
             LIMIT 3
            """,
            (notice_id,),
        )
    except Exception:
        rows = []

    if not rows:
        return (
            '<div style="font-size:.78rem;color:var(--muted);">'
            'No bidder data available.</div>'
        )

    stub_cards = "".join(
        f'<div style="font-size:.78rem;color:var(--text);padding:.3rem 0;'
        f'border-bottom:1px solid var(--border);">'
        f'{r["firm_name"]}'
        f'<span style="color:var(--muted);margin-left:.5rem;">MBIE historical</span>'
        f'</div>'
        for r in rows
    )
    return (
        stub_cards
        + '<div style="font-size:.7rem;color:var(--muted);margin-top:.5rem;font-style:italic;">'
        'Full ACH analysis available on enriched notices.</div>'
    )


def fetch_ach_bidders(notice_id: str) -> list[dict]:
    """Return bidder_pool rows for *notice_id*, ACH rows preferred."""
    try:
        rows = db.fetchall(
            """
            SELECT firm_name, size, strategic_importance, intelligence_maturity,
                   relevance_score, match_type, reasoning, company_context,
                   context_confidence
              FROM bidder_pool
             WHERE notice_id = %s
             ORDER BY
                CASE match_type WHEN 'ach_analysis' THEN 0 ELSE 1 END,
                relevance_score DESC
             LIMIT 3
            """,
            (notice_id,),
        )
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("fetch_ach_bidders failed for %s: %s", notice_id, exc)
        return []


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO,
                         format="%(asctime)s  %(levelname)-8s  %(message)s")

    ap = argparse.ArgumentParser(description="ACH Bidder Intelligence")
    ap.add_argument("--notice-id",      help="Run ACH for a specific notice ID")
    ap.add_argument("--run-enriched",   action="store_true",
                    help="Run ACH for all enriched notices")
    ap.add_argument("--force",          action="store_true",
                    help="Force regeneration even when not stale")
    ap.add_argument("--show-reasoning", action="store_true",
                    help="Log requirements extraction + raw ACH output before JSON")
    args = ap.parse_args()

    if args.notice_id:
        row = db.fetchone(
            """SELECT r.notice_id, r.title, r.agency, r.description,
                      r.category_raw,
                      p.sector_tag, p.value_band, p.geographic_scope
               FROM raw_notices r JOIN parsed_notices p ON p.notice_id=r.notice_id
               WHERE r.notice_id=%s""",
            (args.notice_id,),
        )
        if not row:
            print(f"Notice {args.notice_id} not found")
        else:
            notice = dict(row)
            bidders = generate_bidder_intelligence(
                notice, show_reasoning=args.show_reasoning
            )
            store_ach_results(args.notice_id, bidders)
            print(json.dumps(bidders, indent=2))
    elif args.run_enriched:
        result = run_ach_for_enriched(force=args.force)
        print(json.dumps(result, indent=2))
    else:
        ap.print_help()
