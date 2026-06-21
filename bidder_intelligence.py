"""
bidder_intelligence.py — ACH (Analysis of Competing Hypotheses) bidder analysis.

Three-stage architecture:

  Stage 1 — Web search identifies candidates
            Web search is the SOLE firm identification source.  The query
            anchors on the notice title ("companies providing [TITLE] New Zealand").
            Returns up to 5 named commercial providers.  Government entities are
            filtered before the candidate list is finalised.
            CSV bidder lists and MBIE category matching are NOT used for
            firm identification.

  Stage 2 — MBIE validates and contextualises
            For each web-identified firm, MBIE award history is queried per-firm:
              • Total NZ government contracts won (any sector)
              • Contracts won with this specific agency
              • Primary sector in MBIE records
            This is METADATA ONLY — not used to rank, filter, or replace firms.
            A firm with no MBIE history receives a "no history" note.  ACH sees
            this metadata and uses it for evidence assessment.

  Stage 3 — ACH assesses and ranks
            Claude receives Stage 1 candidates with their Stage 2 MBIE metadata.
            Assesses: capability match, evidence strength, agency relationship,
            competitive position.  Produces ranked list with probability band.
            Claude does NOT add any firm not in the Stage 1 candidate list.

  If Stage 1 finds no candidates, nothing is stored and nothing is shown.
  ACH never invents firm names.

Caching:
  Results stored in bidder_pool with match_type='ach_analysis'.
  context_confidence column stores badge type + wins: "category_match:5" etc.
  Staleness check compares notice parsed_at vs stored timestamp marker.
"""
from __future__ import annotations

import json
import logging
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


# ── Shared utilities ───────────────────────────────────────────────────────────

_GARBAGE_NAME_FRAGMENTS = (
    "unable to rank",
    "analysis_status",
    "cannot identify",
    "no specific firm",
    "error:",
    "insufficient data for",
)

_AGENCY_STOP_WORDS = frozenset({
    "district", "city", "regional", "council", "dc", "cc", "rc",
    "limited", "ltd", "inc", "trust", "board", "authority",
    "new", "zealand", "nz", "the", "of", "and",
})


def _normalise_for_match(name: str) -> str:
    tokens = re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
    return " ".join(t for t in tokens if t not in _AGENCY_STOP_WORDS and len(t) > 1)


def _is_agency_name(bidder_name: str, agency_name: str) -> bool:
    """Return True if *bidder_name* looks like it IS the contracting agency."""
    if not agency_name or not bidder_name:
        return False

    b_lower = bidder_name.lower().strip()
    a_lower = agency_name.lower().strip()

    if a_lower in b_lower or b_lower in a_lower:
        return True

    b_norm = _normalise_for_match(bidder_name)
    a_norm = _normalise_for_match(agency_name)
    if not a_norm or not b_norm:
        return False

    if a_norm == b_norm:
        return True

    b_tokens = set(b_norm.split())
    a_tokens = set(a_norm.split())
    if len(b_tokens & a_tokens) >= 2:
        return True

    has_expansion = bool(re.search(r"\([a-z]{4,}", b_lower))
    if not has_expansion:
        all_words = [t for t in re.sub(r"[^a-z0-9 ]", " ", a_lower).split() if len(t) > 1]
        if all_words:
            initialism = "".join(w[0] for w in all_words)
            if 2 <= len(initialism) <= 4:
                pattern = r"(?<![a-z])" + re.escape(initialism) + r"(?![a-z])"
                if re.search(pattern, b_lower):
                    return True

    return False


def _extract_json_from_response(raw: str) -> dict:
    """Robust JSON extractor — handles fences and prose after the JSON block."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    lines = cleaned.split("\n")
    json_lines = []
    for line in lines:
        if line.strip() == "```":
            break
        json_lines.append(line)
    cleaned = "\n".join(json_lines).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    start = raw.find("{")
    end   = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            pass

    return {}


def _is_garbage_name(name: str) -> bool:
    name_lower = name.lower()
    return any(frag in name_lower for frag in _GARBAGE_NAME_FRAGMENTS)


def _fuzzy_name_match(a: str, b: str) -> bool:
    """Token-overlap test for firm name matching."""
    stop = {"nz", "ltd", "limited", "new", "zealand", "the"}
    a_tokens = set(re.sub(r"[^a-z0-9 ]", " ", a).split()) - stop
    b_tokens = set(re.sub(r"[^a-z0-9 ]", " ", b).split()) - stop
    if not a_tokens or not b_tokens:
        return False
    shared = a_tokens & b_tokens
    return len(shared) >= 2 or (len(shared) == 1 and min(len(a_tokens), len(b_tokens)) <= 2)


# ── Step 1: Candidate identification ──────────────────────────────────────────

def _identify_candidates(notice: dict) -> list[dict]:
    """
    Stage 1: Web search is the sole firm identification source.

    MBIE and CSV are not used to generate firm names.  MBIE validates and
    contextualises candidates after identification (Stage 2 in
    generate_bidder_intelligence).  Always runs live.
    """
    return _identify_candidates_live(notice)


def _identify_candidates_live(notice: dict) -> list[dict]:
    """
    Stage 1: Web search is the sole firm identification source.

    MBIE is not queried here.  It runs in Stage 2 (per-firm validation)
    after the candidate list is finalised.  This eliminates false positives
    from MBIE category matching (e.g. "testing" → lab equipment suppliers
    for a cognitive assessment notice).
    """
    from bidders import _web_search_bidders, _is_government_entity
    from canonical_suppliers import canonical_name, deduplicate_bidders

    notice_id     = notice.get("notice_id", "?")
    notice_sector = notice.get("sector_tag") or "other"
    candidates: list[dict] = []

    web_rows = _web_search_bidders(
        notice.get("title") or "",
        notice.get("agency") or "",
        notice_sector,
        overview_text=notice.get("overview_text") or notice.get("description") or "",
    )
    for r in web_rows:
        if _is_government_entity(r.get("firm_name", "")):
            logger.debug("Stage 1: skipping government entity %r", r.get("firm_name"))
            continue
        cn = canonical_name(r.get("firm_name", ""))
        r["canonical_name"] = cn
        candidates.append(r)

    logger.info(
        "ACH notice %s: Stage 1 (web search) identified %d candidate(s): %s",
        notice_id,
        len(candidates),
        ", ".join(c.get("canonical_name") or c.get("firm_name", "?") for c in candidates),
    )

    return deduplicate_bidders(candidates)[:5]


# ── Stage 2: MBIE per-firm validation ─────────────────────────────────────────

def _enrich_candidates_with_mbie(candidates: list[dict], notice: dict) -> list[dict]:
    """
    Stage 2: Query MBIE per firm to attach validation metadata.

    Does NOT add new firm names — only enriches web search candidates with:
      • Total NZ government contracts won (any sector)
      • Contracts won with this specific agency
      • Primary sector(s) in MBIE records

    A firm with no MBIE history is not penalised — it receives a "no history"
    note that ACH uses as context.  MBIE data does not affect candidate ranking
    or inclusion decisions.
    """
    from bidders import _mbie_available

    if not candidates:
        return candidates

    if not _mbie_available():
        logger.debug("Stage 2: MBIE unavailable — skipping per-firm validation")
        return candidates

    agency = (notice.get("agency") or "").strip()

    for c in candidates:
        firm_name = c.get("canonical_name") or c.get("firm_name") or ""
        firm_kw   = (firm_name.split()[0] if firm_name else "").lower()
        if not firm_kw:
            continue

        total_wins = 0
        agency_wins = 0
        sectors    = ""

        try:
            # Total wins across all sectors
            any_row = db.fetchone(
                """
                SELECT COUNT(DISTINCT n.rfx_id) AS wins,
                       STRING_AGG(DISTINCT c2.sector_tag, ', '
                                  ORDER BY c2.sector_tag) AS sectors
                  FROM mbie_award_notices n
                  JOIN mbie_award_suppliers s  ON s.rfx_id  = n.rfx_id
                  JOIN mbie_award_categories c2 ON c2.rfx_id = n.rfx_id
                 WHERE n.is_awarded
                   AND LOWER(s.business_name) LIKE LOWER(%s)
                """,
                (f"%{firm_kw}%",),
            )
            if any_row:
                total_wins = int(any_row.get("wins") or 0)
                sectors    = str(any_row.get("sectors") or "")

            # Agency-specific wins
            if agency and total_wins > 0:
                agency_kw = (agency.split()[0] if agency else "").lower()
                if agency_kw:
                    agency_row = db.fetchone(
                        """
                        SELECT COUNT(DISTINCT n.rfx_id) AS agency_wins
                          FROM mbie_award_notices n
                          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
                         WHERE n.is_awarded
                           AND LOWER(s.business_name) LIKE LOWER(%s)
                           AND LOWER(n.agency_name)   LIKE LOWER(%s)
                        """,
                        (f"%{firm_kw}%", f"%{agency_kw}%"),
                    )
                    agency_wins = int((agency_row or {}).get("agency_wins") or 0)
        except Exception as exc:
            logger.debug("Stage 2 MBIE query failed for %r: %s", firm_name, exc)

        c["mbie_total_wins"]     = total_wins
        c["mbie_agency_wins"]    = agency_wins
        c["mbie_primary_sector"] = sectors

        # Append MBIE note so ACH sees government contract history
        existing = c.get("reasoning") or []
        if isinstance(existing, str):
            existing = [p.strip() for p in existing.split("|") if p.strip()]

        if total_wins > 0:
            agency_clause = (
                f", including {agency_wins} with {agency}"
                if agency_wins > 0 and agency else ""
            )
            mbie_note = f"MBIE: {total_wins} NZ government contract(s) won{agency_clause}"
            if sectors:
                mbie_note += f" — recorded sector(s): {sectors}"
        else:
            mbie_note = "MBIE: no NZ government contract history found"

        c["reasoning"] = list(existing) + [mbie_note]

    logger.debug("Stage 2: MBIE enrichment complete for %d candidate(s)", len(candidates))
    return candidates


# ── Step 2: ACH assessment ─────────────────────────────────────────────────────

_ACH_ASSESSMENT_SYSTEM = """\
You are a New Zealand government procurement intelligence analyst using Analysis of \
Competing Hypotheses (ACH).

You will receive a list of candidate organisations that have been identified as potential \
bidders for a specific NZ government contract, sourced from MBIE historical awards and web \
search results.

YOUR TASK: Assess and rank ONLY the provided candidates. Do NOT add any organisation not in \
the candidate list. You are an assessor, not a firm identifier.

For each candidate, evaluate the hypothesis "This organisation will bid and win this contract":

STEP 1 — CAPABILITY FIT
Does this firm demonstrably provide the specific service described in the notice title?
  "confirmed" = documented delivery of this exact service type for government clients
  "inferred"  = adjacent capability with plausible transfer to this service type
  "unknown"   = sector presence only, no evidence of this specific service

STEP 2 — GEOGRAPHIC AND SCALE FIT
Does the firm have confirmed delivery capability in this district or region?
Is the firm the right scale for this contract value?
Are there incumbency signals or prior agency relationship evidence?

STEP 3 — RANK AND CALIBRATE
Rank candidates from most to least likely. Apply probability bands strictly:
  "High"       = confirmed capability in this exact service type AND geographic presence confirmed
  "Medium"     = capability inferred, OR capability confirmed but geography uncertain
  "Medium-Low" = sector presence only, limited evidence of this specific capability
  "Low"        = minimal alignment — unlikely to bid even if sector-adjacent

RULES:
- Include ALL candidates in your output, even those ranked "Low"
- Do NOT add any organisation not in the candidate list
- If all candidates appear weak, rank them "Medium-Low" or "Low" — do not invent better candidates
- The contracting agency is the BUYER, not a bidder — never list them as a candidate

Return ONLY valid JSON:
{"bidders": [\
{"name": str, "probability": "High"|"Medium"|"Medium-Low"|"Low", \
"capability_match": "confirmed"|"inferred"|"unknown", \
"evidence": [str, str], "discriminator": str, \
"size": "small"|"medium"|"large"|"major"}\
]}"""


def _build_ach_assessment_prompt(
    candidates: list[dict],
    notice: dict,
    reqs_summary: str,
) -> str:
    title       = notice.get("title") or ""
    agency      = notice.get("agency") or notice.get("agency_name") or ""
    region      = notice.get("geographic_scope") or notice.get("region") or "Not specified"
    value_band  = notice.get("value_band") or "unknown"
    description = (notice.get("description") or "")[:800]

    value_labels = {
        "under_100k": "Under $100K", "100k_500k": "$100K–$500K",
        "500k_2m": "$500K–$2M",      "2m_10m": "$2M–$10M",
        "10m_plus": "$10M+",          "unknown": "Value not specified",
    }
    value_str = value_labels.get(value_band, value_band)

    lines = []
    for i, c in enumerate(candidates, 1):
        name = c.get("canonical_name") or c.get("firm_name") or "?"

        # Collect context parts: web search description + MBIE metadata
        context_parts: list[str] = []
        reasoning = c.get("reasoning") or []
        if isinstance(reasoning, list):
            for r in reasoning:
                r_str = str(r).strip()
                if r_str:
                    context_parts.append(r_str[:120])
        elif isinstance(reasoning, str) and reasoning:
            for p in reasoning.split("|"):
                p = p.strip()
                if p and not p.startswith("CAPMATCH:"):
                    context_parts.append(p[:120])

        if not context_parts and c.get("company_context"):
            context_parts.append(str(c["company_context"])[:120])

        desc = (" — " + "; ".join(context_parts[:3])) if context_parts else ""
        lines.append(f"{i}. {name}{desc}")

    candidates_block = "\n".join(lines)

    return (
        f"SERVICE BEING PROCURED: {title}\n"
        f"AGENCY (the buyer — do not list as a bidder): {agency}\n"
        f"REGION/SCOPE: {region}\n"
        f"CONTRACT VALUE: {value_str}\n\n"
        f"SPECIFIC REQUIREMENTS:\n{reqs_summary}\n\n"
        f"FULL DESCRIPTION: {description or 'Not provided'}\n\n"
        f"CANDIDATES TO ASSESS (rank ALL of these, do not add others):\n"
        f"{candidates_block}"
    )


def _run_ach_assessment(
    candidates: list[dict],
    notice: dict,
    reqs_summary: str,
) -> list[dict]:
    """
    Call Claude to assess and rank the provided candidates using ACH methodology.

    Returns at most 3 ranked bidders containing ONLY names from the input
    candidate set.  Returns [] on any failure.
    """
    notice_id = notice.get("notice_id", "?")
    if not candidates:
        return []

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    except Exception as exc:
        logger.warning("ACH assessment notice %s: Anthropic client init failed — %s",
                       notice_id, exc)
        return []

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1400,
            system=_ACH_ASSESSMENT_SYSTEM,
            messages=[{
                "role": "user",
                "content": _build_ach_assessment_prompt(candidates, notice, reqs_summary),
            }],
        )
    except Exception as api_exc:
        logger.warning("ACH assessment notice %s: Claude API call failed — %s",
                       notice_id, api_exc)
        return []

    raw = resp.content[0].text.strip()
    data = _extract_json_from_response(raw)
    if not data:
        logger.warning(
            "ACH assessment notice %s: JSON extraction failed "
            "(raw length=%d, preview=%.120r)",
            notice_id, len(raw), raw,
        )
        return []

    bidders = data.get("bidders", [])
    if not bidders:
        logger.warning(
            "ACH assessment notice %s: Claude returned valid JSON but 'bidders' is empty",
            notice_id,
        )
        return []

    # Validate: only accept names that are in the input candidate set
    candidate_names_lower = {
        (c.get("canonical_name") or c.get("firm_name") or "").lower()
        for c in candidates
    }

    validated: list[dict] = []
    for b in bidders:
        name = str(b.get("name") or "").strip()
        if not name or _is_garbage_name(name):
            continue
        name_l = name.lower()
        if not any(
            name_l in inp or inp in name_l or _fuzzy_name_match(name_l, inp)
            for inp in candidate_names_lower
        ):
            logger.info(
                "ACH assessment notice %s: rejecting %r — not in input candidate set",
                notice_id, name,
            )
            continue
        validated.append(b)

    if not validated:
        logger.warning(
            "ACH assessment notice %s: all %d returned bidder(s) rejected — "
            "none matched input candidates (candidates were: %s)",
            notice_id, len(bidders),
            ", ".join(c.get("canonical_name") or c.get("firm_name", "?")
                      for c in candidates[:5]),
        )

    return validated[:3]


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
    Apply category-gated MBIE badges to each ACH-assessed bidder.
    Does NOT modify Claude's probability rankings — MBIE is informational only.
    """
    from bidders import _is_government_entity as _is_govt
    results = []
    for b in bidders_raw[:3]:
        name = str(b.get("name") or "").strip()
        if not name:
            continue
        if agency_name and _is_agency_name(name, agency_name):
            logger.info("ACH: excluded '%s' — matches contracting agency '%s'",
                        name, agency_name)
            continue
        if _is_govt(name):
            logger.info("ACH: excluded '%s' — identified as government entity", name)
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
        conf_str = f"{badge_type}:{wins}"

        # Post-hoc confidence cap: "High" requires confirmed capability
        if prob == "High" and capability_match != "confirmed":
            prob = "Medium"
            evidence = (evidence + [
                "Probability capped at Medium — capability match is inferred, "
                "not confirmed for this specific service type"
            ])[:3]

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


# ── Backward-compat gate stub ─────────────────────────────────────────────────
# The ACH relevance gate is no longer needed in the new architecture — ACH only
# assesses candidates supplied from MBIE/web search and cannot generate firm
# names independently.  These stubs are kept because portal.py and output.py
# import them to screen stale ach_analysis rows from the old architecture while
# they remain in bidder_pool before Layer 2 replaces them.

_GATE_STOP = frozenset({
    "the", "and", "for", "of", "in", "to", "a", "an", "at", "by", "or",
    "with", "from", "new", "zealand", "nz", "government", "contract",
    "services", "service", "project", "works", "supply", "provision",
    "request", "proposal", "nzdf", "ministry", "department", "authority",
    "into", "via", "its", "this", "that",
})


def _gate_title_keywords(title: str) -> list:
    """Extract meaningful service-domain keywords from notice title."""
    words = re.findall(r"[a-zA-Z]{3,}", title.lower())
    return [w for w in words if w not in _GATE_STOP][:10]


def _ach_relevance_gate(bidders: list, notice_title: str) -> bool:
    """
    Screen ACH bidder rows against notice title keywords.

    Kept functional (not a no-op) to block stale ach_analysis rows from the
    old architecture that may still be in bidder_pool.  Once Layer 2 re-runs
    under the new architecture and replaces those rows with correctly-anchored
    results, this gate becomes a no-op that always passes.
    """
    kws = _gate_title_keywords(notice_title)
    if len(kws) < 2 or not bidders:
        return True

    failed = 0
    for b in bidders:
        name_text = (b.get("name") or b.get("firm_name") or "").lower()
        evidence = b.get("evidence") or []
        discriminator = b.get("discriminator") or ""
        reasoning_raw = b.get("reasoning") or ""
        parts = [p.strip() for p in reasoning_raw.split("|") if p.strip()
                 and not p.strip().startswith("CAPMATCH:")]

        all_text = " ".join([
            name_text,
            " ".join(str(e) for e in evidence),
            discriminator,
            " ".join(parts),
        ]).lower()

        if not any(kw in all_text for kw in kws):
            failed += 1

    passes = failed <= len(bidders) // 2
    if not passes:
        logger.warning(
            "ACH relevance gate FAILED for %r: %d/%d firms have no keyword "
            "overlap with title (kws=%s) — these are stale rows from old architecture",
            notice_title[:70], failed, len(bidders), kws[:5],
        )
    return passes


# ── Main pipeline ──────────────────────────────────────────────────────────────

def generate_bidder_intelligence(
    notice: dict,
    show_reasoning: bool = False,
) -> list[dict]:
    """
    Run ACH bidder analysis for *notice*.

    Stage 1 — Web search identifies candidates (sole firm identification source)
    Stage 2 — MBIE per-firm validation attached as metadata (does not add/remove firms)
    Stage 3 — Requirements extraction
    Stage 4 — ACH assessment: Claude ranks provided candidates, adds no new names
    Stage 5 — MBIE display badges applied post-hoc

    Returns [] if Stage 1 finds no candidates.  Nothing is stored and nothing
    is shown rather than having ACH invent firms.
    """
    notice_id     = notice.get("notice_id", "?")
    notice_sector = notice.get("sector_tag") or notice.get("sector") or "other"
    agency_name   = notice.get("agency") or notice.get("agency_name") or ""

    logger.info(
        "ACH starting for notice %s (sector=%s, agency=%s)",
        notice_id, notice_sector, agency_name,
    )

    # Stage 1 — Candidate identification (web search only)
    candidates = _identify_candidates(notice)
    if not candidates:
        logger.info(
            "ACH skip notice %s: web search found no candidates — "
            "returning [] so display shows nothing",
            notice_id,
        )
        return []

    logger.info(
        "ACH notice %s: %d candidate(s) identified — %s",
        notice_id,
        len(candidates),
        ", ".join(
            c.get("canonical_name") or c.get("firm_name", "?")
            for c in candidates[:5]
        ),
    )

    if show_reasoning:
        logger.info(
            "=== STAGE 1 CANDIDATES for %s ===\n%s",
            notice_id,
            json.dumps(
                [{"name": c.get("canonical_name") or c.get("firm_name"),
                  "source": c.get("match_type")}
                 for c in candidates],
                indent=2,
            ),
        )

    # Stage 2 — MBIE per-firm validation (metadata only; does not add/remove firms)
    candidates = _enrich_candidates_with_mbie(candidates, notice)

    if show_reasoning:
        logger.info(
            "=== STAGE 2 MBIE ENRICHMENT for %s ===\n%s",
            notice_id,
            json.dumps(
                [{"name": c.get("canonical_name") or c.get("firm_name"),
                  "mbie_total": c.get("mbie_total_wins", 0),
                  "mbie_agency": c.get("mbie_agency_wins", 0)}
                 for c in candidates],
                indent=2,
            ),
        )

    # Stage 3 — Requirements extraction
    reqs = _extract_requirements(notice)
    reqs_summary = _format_requirements_summary(reqs)

    if show_reasoning:
        logger.info("=== REQUIREMENTS for %s ===\n%s", notice_id, reqs_summary)

    # Stage 4 — ACH assessment of the candidate pool
    bidders_raw = _run_ach_assessment(candidates, notice, reqs_summary)

    if show_reasoning:
        logger.info("=== RAW ACH ASSESSMENT for %s ===\n%s",
                    notice_id, json.dumps(bidders_raw, indent=2))

    if not bidders_raw:
        logger.warning(
            "ACH notice %s: assessment returned no results — "
            "no bidders will be stored",
            notice_id,
        )
        return []

    # Stage 5 — MBIE display badges (does not change ranking)
    results = _apply_mbie_enrichment(bidders_raw, notice_sector, agency_name=agency_name)

    if not results:
        logger.warning(
            "ACH notice %s: all candidates removed by agency/govt filter",
            notice_id,
        )
        return []

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

        # Delete ALL existing ach_analysis rows before inserting new ones
        db.execute(
            "DELETE FROM bidder_pool WHERE notice_id = %s AND match_type = 'ach_analysis'",
            (notice_id,),
        )

        for rank, b in enumerate(bidders, 1):
            cap_match = b.get("capability_match", "unknown")
            evidence_parts = b.get("evidence") or []
            discriminator  = b.get("discriminator") or ""

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
                ON CONFLICT (notice_id, firm_name) DO UPDATE SET
                    sector               = EXCLUDED.sector,
                    size                 = EXCLUDED.size,
                    strategic_importance = EXCLUDED.strategic_importance,
                    intelligence_maturity = EXCLUDED.intelligence_maturity,
                    relevance_score      = EXCLUDED.relevance_score,
                    match_type           = EXCLUDED.match_type,
                    reasoning            = EXCLUDED.reasoning,
                    company_context      = EXCLUDED.company_context,
                    context_confidence   = EXCLUDED.context_confidence
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


def _run_incumbent_detection_for_notice(notice: dict) -> None:
    """
    Run incumbent web search and store result in bidder_pool.
    Called after store_ach_results() in both ACH batch runners.
    Skips silently if an incumbent_identified row already exists.
    Never raises — all failures are logged as warnings.
    """
    notice_id = notice.get("notice_id", "?")

    try:
        existing = db.fetchone(
            "SELECT firm_name FROM bidder_pool "
            "WHERE notice_id = %s AND match_type = 'incumbent_identified' LIMIT 1",
            (notice_id,),
        )
        if existing:
            logger.debug(
                "Incumbent already stored for notice %s (%s) — skipping",
                notice_id, existing.get("firm_name"),
            )
            return
    except Exception as exc:
        logger.warning("Incumbent staleness check failed for %s: %s", notice_id, exc)
        return

    agency      = notice.get("agency") or ""
    sector      = notice.get("sector_tag") or "other"
    title       = notice.get("title") or ""
    notice_text = (notice.get("overview_text") or notice.get("description") or "")[:2000]

    try:
        from pursuit_package import (
            _web_search_incumbent,
            _extract_incumbent_firm_name,
            _store_incumbent_in_bidder_pool,
        )
        incumbent_research = _web_search_incumbent(agency, sector, title, notice_text)
        logger.info(
            "INCUMBENT (ACH batch) notice %s: %r",
            notice_id, (incumbent_research or "")[:120],
        )
        firm_name = _extract_incumbent_firm_name(incumbent_research or "")
        if firm_name:
            _store_incumbent_in_bidder_pool(notice_id, firm_name, incumbent_research, sector)
            logger.info(
                "INCUMBENT stored for notice %s: %s", notice_id, firm_name,
            )
    except Exception as exc:
        logger.warning(
            "_run_incumbent_detection_for_notice failed for %s: %s", notice_id, exc,
        )


# ── Batch runner ───────────────────────────────────────────────────────────────

def run_ach_for_enriched(force: bool = False) -> dict:
    """
    Run ACH analysis on all notices with enriched_notices entries.

    When generate_bidder_intelligence() returns [] (no candidates from MBIE/web),
    any stale ach_analysis rows for that notice are deleted so the display falls
    through to Pipeline A rather than showing old wrong results.
    """
    enriched_ids = db.fetchall(
        """
        SELECT e.notice_id, r.title, r.agency, r.description,
               r.overview_text, r.category_raw,
               p.sector_tag, p.value_band, p.geographic_scope
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
                "overview_text":    row.get("overview_text") or "",
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
                # No candidates found — clear any stale ach_analysis rows so display
                # falls through to Pipeline A rather than showing old wrong results
                db.execute(
                    "DELETE FROM bidder_pool "
                    "WHERE notice_id = %s AND match_type = 'ach_analysis'",
                    (nid,),
                )
                logger.info(
                    "ACH notice %s: no candidates — cleared stale ach_analysis rows",
                    nid,
                )
                counts["failed"] += 1
            _run_incumbent_detection_for_notice(notice)
        except Exception as exc:
            logger.error("ACH batch failed for %s: %s", nid, exc)
            counts["failed"] += 1

    logger.info(
        "ACH batch complete — processed=%d skipped=%d failed=%d",
        counts["processed"], counts["skipped"], counts["failed"],
    )
    return counts


# ── Batch runner: unprocessed notices ─────────────────────────────────────────

def run_ach_for_unprocessed(batch_size: int = 5, delay: float = 15.0) -> dict:
    """
    Run ACH for every notice that has enriched_notices data but zero ach_analysis rows.

    Processes in batches of `batch_size` with `delay` seconds between batches.
    On HTTP 400 errors (rate-limit signals) waits 30 s and retries once before
    marking the notice as failed.
    """
    import time
    import anthropic as _anthropic

    rows = db.fetchall(
        """
        SELECT e.notice_id, r.title, r.agency, r.description,
               r.overview_text, r.category_raw,
               p.sector_tag, p.value_band, p.geographic_scope
          FROM enriched_notices e
          JOIN raw_notices r  ON r.notice_id  = e.notice_id
          JOIN parsed_notices p ON p.notice_id = e.notice_id
         WHERE NOT EXISTS (
               SELECT 1 FROM bidder_pool bp
                WHERE bp.notice_id  = e.notice_id
                  AND bp.match_type = 'ach_analysis'
         )
         ORDER BY e.enriched_at DESC
        """
    )

    total = len(rows)
    logger.info("ACH --all: %d notice(s) have no ach_analysis rows", total)

    counts = {"processed": 0, "failed": 0}

    for batch_start in range(0, total, batch_size):
        batch = rows[batch_start : batch_start + batch_size]
        batch_num = batch_start // batch_size + 1
        batch_total = (total + batch_size - 1) // batch_size
        logger.info(
            "ACH --all: batch %d/%d (%d notice(s))",
            batch_num, batch_total, len(batch),
        )

        for row in batch:
            nid = row["notice_id"]
            notice = {
                "notice_id":        nid,
                "title":            row.get("title") or "",
                "agency":           row.get("agency") or "",
                "description":      row.get("description") or "",
                "overview_text":    row.get("overview_text") or "",
                "sector_tag":       row.get("sector_tag") or "other",
                "value_band":       row.get("value_band") or "unknown",
                "geographic_scope": row.get("geographic_scope"),
                "category_raw":     row.get("category_raw") or "",
            }

            for attempt in (1, 2):
                try:
                    bidders = generate_bidder_intelligence(notice)
                    if bidders:
                        store_ach_results(nid, bidders)
                        counts["processed"] += 1
                        logger.info("ACH --all: %s → %d bidder(s) stored", nid, len(bidders))
                    else:
                        logger.info("ACH --all: %s → no candidates identified", nid)
                        counts["failed"] += 1
                    _run_incumbent_detection_for_notice(notice)
                    break  # success or clean empty — don't retry
                except _anthropic.APIStatusError as exc:
                    if exc.status_code == 400 and attempt == 1:
                        logger.warning(
                            "ACH --all: %s got HTTP 400 (attempt %d) — waiting 30s then retrying",
                            nid, attempt,
                        )
                        time.sleep(30)
                        continue
                    logger.error("ACH --all: %s failed (attempt %d): %s", nid, attempt, exc)
                    counts["failed"] += 1
                    break
                except Exception as exc:
                    logger.error("ACH --all: %s failed: %s", nid, exc)
                    counts["failed"] += 1
                    break

        if batch_start + batch_size < total:
            logger.info("ACH --all: sleeping %.0fs before next batch…", delay)
            time.sleep(delay)

    logger.info(
        "ACH --all complete — processed=%d failed/empty=%d total=%d",
        counts["processed"], counts["failed"], total,
    )
    return counts


def run_incumbent_detection_all(force: bool = False) -> dict:
    """
    Run incumbent web search for every notice in the watchlist, independent
    of ACH status.  Existing incumbent_identified rows are skipped unless
    force=True (which deletes them first so the search re-runs).

    Returns: {"run": int, "skipped": int, "stored": int, "failed": int}
    """
    import time as _time

    rows = db.fetchall(
        """
        SELECT e.notice_id, r.title, r.agency, r.description,
               r.overview_text, p.sector_tag
          FROM enriched_notices e
          JOIN raw_notices   r ON r.notice_id = e.notice_id
          JOIN parsed_notices p ON p.notice_id = e.notice_id
         ORDER BY e.enriched_at DESC
        """
    )

    total = len(rows)
    logger.info("INCUMBENT ALL: %d notices to check", total)

    counts = {"run": 0, "skipped": 0, "stored": 0, "failed": 0}

    for row in rows:
        nid = row["notice_id"]

        if force:
            try:
                db.execute(
                    "DELETE FROM bidder_pool "
                    "WHERE notice_id = %s AND match_type = 'incumbent_identified'",
                    (nid,),
                )
            except Exception as exc:
                logger.warning("INCUMBENT ALL: delete failed for %s: %s", nid, exc)

        # Check before calling the inner function so we can track skipped count
        try:
            existing = db.fetchone(
                "SELECT firm_name FROM bidder_pool "
                "WHERE notice_id = %s AND match_type = 'incumbent_identified' LIMIT 1",
                (nid,),
            )
            if existing:
                counts["skipped"] += 1
                continue
        except Exception as exc:
            logger.warning("INCUMBENT ALL: staleness check failed for %s: %s", nid, exc)
            counts["failed"] += 1
            continue

        notice = {
            "notice_id":     nid,
            "title":         row.get("title") or "",
            "agency":        row.get("agency") or "",
            "description":   row.get("description") or "",
            "overview_text": row.get("overview_text") or "",
            "sector_tag":    row.get("sector_tag") or "other",
        }

        pre_count = counts["stored"]
        _run_incumbent_detection_for_notice(notice)
        counts["run"] += 1

        # Check if a row was actually stored
        try:
            stored = db.fetchone(
                "SELECT firm_name FROM bidder_pool "
                "WHERE notice_id = %s AND match_type = 'incumbent_identified' LIMIT 1",
                (nid,),
            )
            if stored:
                counts["stored"] += 1
        except Exception:
            pass

        # Throttle slightly to avoid hammering the web search API
        _time.sleep(2)

    logger.info(
        "INCUMBENT ALL complete — run=%d skipped=%d stored=%d failed=%d total=%d",
        counts["run"], counts["skipped"], counts["stored"], counts["failed"], total,
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

    conf_str   = b.get("context_confidence") or "no_mbie:0"
    badge_type, wins = _parse_conf_str(conf_str)
    mbie_badge = _mbie_badge_html(badge_type, wins)

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
        f'<div style="display:flex;justify-content:space-between;align-items:flex-start;'
        f'gap:.5rem;margin-bottom:.35rem;">'
        f'<span style="font-size:.83rem;font-weight:700;color:var(--text);">{name}</span>'
        f'{mbie_badge}'
        f'</div>'
        f'<div style="display:flex;align-items:center;gap:.45rem;flex-wrap:wrap;margin-bottom:.5rem;">'
        f'<span style="font-size:.68rem;font-weight:700;letter-spacing:.05em;'
        f'text-transform:uppercase;padding:.12rem .5rem;border-radius:4px;'
        f'background:{colour}22;color:{colour};border:1px solid {colour}44;">'
        f'{prob}</span>'
        f'<span style="font-size:.68rem;color:var(--muted);">{size_label}</span>'
        f'{cap_badge}'
        f'</div>'
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
    ap.add_argument("--all",            action="store_true",
                    help="Run ACH for all notices with no ach_analysis rows (batch mode)")
    args = ap.parse_args()

    if args.notice_id:
        row = db.fetchone(
            """SELECT r.notice_id, r.title, r.agency, r.description,
                      r.overview_text, r.category_raw,
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
    elif args.all:
        result = run_ach_for_unprocessed()
        print(json.dumps(result, indent=2))
    else:
        ap.print_help()
