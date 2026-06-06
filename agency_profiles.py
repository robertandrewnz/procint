"""
Layer 2 — Agency profiling.

For each contracting agency in the knowledge graph, builds and maintains a
structured intelligence profile:
  - Procurement volume and sector patterns from Layer 1 notices
  - Award history and preferred suppliers from contract_awards
  - Typical evaluation criteria language (inferred from notice descriptions)
  - Contract renewal patterns (extrapolated from award durations)
  - Claude-generated 3-sentence narrative summary

Profiles are stored in agency_profiles and regenerated when enough new
activity has occurred (controlled by AGENCY_PROFILE_MIN_NOTICES).
"""
import json
import logging
from collections import Counter
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


# ── Profile computation ───────────────────────────────────────────────────────

def _compute_sector_distribution(notices: list[dict]) -> list[dict]:
    counter = Counter(n["sector_tag"] for n in notices if n.get("sector_tag"))
    return [{"sector": s, "count": c} for s, c in counter.most_common(5)]


def _compute_preferred_suppliers(org_id: int) -> list[dict]:
    rows = db.fetchall(
        """
        SELECT o.name AS supplier_name,
               COUNT(*) AS award_count,
               SUM(ca.contract_value) AS total_value
          FROM contract_awards ca
          JOIN organisations o ON o.org_id = ca.supplier_org_id
         WHERE ca.agency_org_id = %s
           AND ca.supplier_org_id IS NOT NULL
         GROUP BY o.org_id, o.name
         ORDER BY award_count DESC
         LIMIT 10
        """,
        (org_id,),
    )
    return [
        {
            "name": r["supplier_name"],
            "award_count": r["award_count"],
            "total_value": float(r["total_value"]) if r["total_value"] else None,
        }
        for r in rows
    ]


def _compute_notice_types(notices: list[dict]) -> list[dict]:
    counter = Counter(n.get("category_raw", "Unknown") for n in notices)
    return [{"type": t, "count": c} for t, c in counter.most_common(5)]


def _compute_avg_days_to_close(notices: list[dict]) -> Optional[float]:
    vals = [n["days_until_close"] for n in notices
            if n.get("days_until_close") is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _extract_eval_patterns(notices: list[dict]) -> str:
    """Extract commonly mentioned evaluation criteria phrases."""
    criteria_texts = [
        n["evaluation_criteria"] for n in notices
        if n.get("evaluation_criteria")
    ]
    if not criteria_texts:
        return "No stated evaluation criteria found in recent notices."
    # Count most common phrases
    words: Counter = Counter()
    for text in criteria_texts:
        for word in text.lower().split():
            if len(word) > 4:
                words[word] += 1
    top = [w for w, _ in words.most_common(10)]
    return f"Common criteria terms: {', '.join(top[:8])}." if top else "Varied criteria observed."


def _assess_renewal_tendency(awards: list[dict]) -> str:
    """
    Infer renewal tendency based on contract durations seen.
    Short contracts (<12 months) suggest high churn; long (>36 months) suggest stability.
    """
    if not awards:
        return "unknown"
    durations = [a["duration_months"] for a in awards if a.get("duration_months")]
    if not durations:
        return "unknown"
    avg = sum(durations) / len(durations)
    if avg <= 12:
        return "high"  # frequent renewals
    if avg <= 36:
        return "medium"
    return "low"  # long-term contracts, less frequent renewal


# ── Claude narrative generation ───────────────────────────────────────────────

_PROFILE_SYSTEM = (
    "You are a procurement intelligence analyst. "
    "Respond ONLY with a valid JSON object — no preamble, no markdown fences."
)

_PROFILE_PROMPT = """Generate a procurement intelligence profile for this New Zealand government agency.

Agency: {name}
Total procurement notices observed: {total_notices}
Total awards recorded: {total_awards}
Total awarded value: {total_awarded_value}
Dominant procurement sectors: {dominant_sectors}
Preferred suppliers (by award count): {preferred_suppliers}
Average contract duration: {avg_duration} months
Renewal tendency: {renewal_tendency}
Common evaluation criteria patterns: {eval_patterns}

Return a JSON object with exactly these keys:
"profile_summary": Three sentences. First: what type of organisation this is and their primary procurement function. Second: their notable procurement patterns, preferred suppliers, or sector focus. Third: strategic intelligence for firms seeking to work with this agency.
"procurement_cadence": One sentence on when/how frequently they procure (e.g. seasonal patterns, annual refresh cycles if detectable from the data).
"incumbent_risk": "high", "medium", or "low" — assessment of how likely they are to stick with incumbent suppliers."""


def _generate_profile_narrative(
    org: dict,
    total_awards: int,
    total_awarded_value: Optional[float],
    dominant_sectors: list[dict],
    preferred_suppliers: list[dict],
    avg_duration: Optional[float],
    renewal_tendency: str,
    eval_patterns: str,
    total_notices: int,
) -> tuple[str, str]:
    """Call Claude to generate narrative. Returns (profile_summary, procurement_cadence)."""
    client = _get_claude()

    prompt = _PROFILE_PROMPT.format(
        name=org["name"],
        total_notices=total_notices,
        total_awards=total_awards,
        total_awarded_value=f"${total_awarded_value:,.0f}" if total_awarded_value else "unknown",
        dominant_sectors=", ".join(d["sector"] for d in dominant_sectors[:3]) or "mixed",
        preferred_suppliers=(
            ", ".join(s["name"] for s in preferred_suppliers[:5]) or "insufficient data"
        ),
        avg_duration=f"{avg_duration:.0f}" if avg_duration else "unknown",
        renewal_tendency=renewal_tendency,
        eval_patterns=eval_patterns,
    )

    try:
        message = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=500,
            system=_PROFILE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        result = json.loads(raw)
        return result.get("profile_summary", ""), result.get("procurement_cadence", "")
    except Exception as exc:
        logger.warning("Claude profile failed for %s: %s", org["name"], exc)
        return "", ""


# ── Storage ───────────────────────────────────────────────────────────────────

def _store_profile(org_id: int, profile: dict) -> None:
    db.execute(
        """
        INSERT INTO agency_profiles
            (org_id, total_notices, total_awards, total_awarded_value,
             avg_contract_value, dominant_sectors, preferred_suppliers,
             avg_days_to_close, typical_notice_types, eval_criteria_patterns,
             renewal_tendency, procurement_cadence, profile_summary)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (org_id) DO UPDATE SET
            total_notices         = EXCLUDED.total_notices,
            total_awards          = EXCLUDED.total_awards,
            total_awarded_value   = EXCLUDED.total_awarded_value,
            avg_contract_value    = EXCLUDED.avg_contract_value,
            dominant_sectors      = EXCLUDED.dominant_sectors,
            preferred_suppliers   = EXCLUDED.preferred_suppliers,
            avg_days_to_close     = EXCLUDED.avg_days_to_close,
            typical_notice_types  = EXCLUDED.typical_notice_types,
            eval_criteria_patterns = EXCLUDED.eval_criteria_patterns,
            renewal_tendency      = EXCLUDED.renewal_tendency,
            procurement_cadence   = EXCLUDED.procurement_cadence,
            profile_summary       = EXCLUDED.profile_summary,
            generated_at          = NOW()
        """,
        (
            org_id,
            profile["total_notices"],
            profile["total_awards"],
            profile.get("total_awarded_value"),
            profile.get("avg_contract_value"),
            json.dumps(profile["dominant_sectors"]),
            json.dumps(profile["preferred_suppliers"]),
            profile.get("avg_days_to_close"),
            json.dumps(profile["typical_notice_types"]),
            profile["eval_patterns"],
            profile["renewal_tendency"],
            profile.get("procurement_cadence", ""),
            profile.get("profile_summary", ""),
        ),
    )


# ── Main entry point ──────────────────────────────────────────────────────────

def build_profile(org_id: int) -> Optional[dict]:
    """Build a complete intelligence profile for one agency. Returns profile dict or None."""
    org = db.fetchone("SELECT * FROM organisations WHERE org_id = %s", (org_id,))
    if not org:
        return None

    # Notices for this agency
    notices = db.fetchall(
        """
        SELECT p.sector_tag, p.days_until_close, p.evaluation_criteria,
               r.category_raw
          FROM raw_notices r
          JOIN parsed_notices p ON p.notice_id = r.notice_id
          JOIN name_aliases a   ON a.alias = r.agency AND a.org_id = %s
        """,
        (org_id,),
    )

    # Awards for this agency
    awards = db.fetchall(
        """
        SELECT contract_value, duration_months, award_date, sector_tag
          FROM contract_awards
         WHERE agency_org_id = %s
        """,
        (org_id,),
    )

    total_notices = len(notices)
    total_awards = len(awards)

    if total_notices < config.AGENCY_PROFILE_MIN_NOTICES:
        logger.debug(
            "Skipping profile for %s — only %d notices (min %d)",
            org["name"], total_notices, config.AGENCY_PROFILE_MIN_NOTICES,
        )
        return None

    total_awarded_value = sum(
        float(a["contract_value"]) for a in awards if a.get("contract_value")
    ) or None
    avg_contract_value = (
        total_awarded_value / total_awards if total_awarded_value and total_awards else None
    )

    dominant_sectors = _compute_sector_distribution(notices)
    preferred_suppliers = _compute_preferred_suppliers(org_id)
    typical_notice_types = _compute_notice_types(notices)
    avg_days_to_close = _compute_avg_days_to_close(notices)
    eval_patterns = _extract_eval_patterns(notices)
    renewal_tendency = _assess_renewal_tendency(awards)

    durations = [a["duration_months"] for a in awards if a.get("duration_months")]
    avg_duration = sum(durations) / len(durations) if durations else None

    profile_summary, procurement_cadence = _generate_profile_narrative(
        org=org,
        total_awards=total_awards,
        total_awarded_value=total_awarded_value,
        dominant_sectors=dominant_sectors,
        preferred_suppliers=preferred_suppliers,
        avg_duration=avg_duration,
        renewal_tendency=renewal_tendency,
        eval_patterns=eval_patterns,
        total_notices=total_notices,
    )

    profile = {
        "total_notices": total_notices,
        "total_awards": total_awards,
        "total_awarded_value": total_awarded_value,
        "avg_contract_value": avg_contract_value,
        "dominant_sectors": dominant_sectors,
        "preferred_suppliers": preferred_suppliers,
        "typical_notice_types": typical_notice_types,
        "avg_days_to_close": avg_days_to_close,
        "eval_patterns": eval_patterns,
        "renewal_tendency": renewal_tendency,
        "profile_summary": profile_summary,
        "procurement_cadence": procurement_cadence,
    }
    _store_profile(org_id, profile)
    logger.info("Built profile for %s (%d notices)", org["name"], total_notices)
    return profile


def run_agency_profiling() -> int:
    """
    Build/refresh profiles for all agencies above the notice threshold.
    Ordered by notice_count DESC so the most active agencies are profiled first.
    Capped at MAX_AGENCY_PROFILES_PER_RUN to control API cost.
    Returns number of profiles generated.
    """
    logger.info("Starting agency profiling")

    candidates = db.fetchall(
        """
        SELECT o.org_id, o.name, o.notice_count
          FROM organisations o
         WHERE o.org_type IN ('agency', 'both')
           AND o.notice_count >= %s
         ORDER BY o.notice_count DESC
         LIMIT %s
        """,
        (config.AGENCY_PROFILE_MIN_NOTICES, config.MAX_AGENCY_PROFILES_PER_RUN),
    )

    logger.info("%d agencies eligible for profiling", len(candidates))
    count = 0
    for row in candidates:
        try:
            profile = build_profile(row["org_id"])
            if profile:
                count += 1
        except Exception as exc:
            logger.warning("Profile failed for %s: %s", row["name"], exc)

    logger.info("Agency profiling complete: %d profiles generated", count)
    return count


def get_profile(org_id: int) -> Optional[dict]:
    """Fetch stored profile for an agency."""
    return db.fetchone(
        "SELECT * FROM agency_profiles WHERE org_id = %s", (org_id,)
    )
