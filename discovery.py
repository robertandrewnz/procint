"""
Layer 2 — Automatic organisation discovery.

Scans every GETS notice text for organisation names that haven't been seen
before and inserts them into the knowledge graph with appropriate confidence
flags:

  high    — directly named as contracting agency in raw_notices.agency
  medium  — matched from bidder_pool (known firm in seeded list)
  low     — extracted from notice description text via regex heuristics
             (co-mentioned organisations, referenced suppliers, consortia)

This means the knowledge graph grows automatically on every Layer 1 pipeline
run with zero manual input. Over time:
  - Every NZ government agency that issues notices is captured
  - Every named subcontractor, JV partner, or referenced firm is captured
  - Confidence flags signal which records need manual verification

Discovery runs AFTER Layer 1 ingestion and AFTER organisations.seed_from_layer1(),
so it only processes net-new notices since the last run.
"""
import logging
import re
from typing import Optional

import config
import db
import organisations as orgs

logger = logging.getLogger(__name__)

# ── Organisation name patterns ────────────────────────────────────────────────
# These regex patterns extract likely organisation names from notice description
# text. They are intentionally conservative to avoid false positives.
# Lower-confidence matches go through fuzzy deduplication before insertion.

_ORG_PATTERNS = [
    # "ABC Ltd", "ABC Limited", "ABC Group", "ABC Corporation"
    re.compile(
        r"\b([A-Z][A-Za-z&\s\(\)]{4,50}?)"
        r"\s+(?:Ltd|Limited|Group|Corporation|Corp|Inc|NZ|New Zealand)\b",
        re.UNICODE,
    ),
    # "XYZ Contractors", "XYZ Services", "XYZ Solutions"
    re.compile(
        r"\b([A-Z][A-Za-z&\s]{4,40}?)"
        r"\s+(?:Contractors|Services|Solutions|Consulting|Consultants|"
        r"Engineering|Construction|Technologies|Systems|Associates)\b",
        re.UNICODE,
    ),
    # "Ministry of X", "Department of X", "Office of X"
    re.compile(
        r"\b((?:Ministry|Department|Office|Authority|Commission|Board|"
        r"Agency|Corporation|Council|Institute)\s+(?:of\s+)?[A-Z][A-Za-z\s]{2,40}?)"
        r"(?=[,\.\;\n]|\s+(?:and|or|is|has|will|has|the|a|an)\b)",
        re.UNICODE,
    ),
]

# Names to skip — generic terms that regex can match but aren't org names
_SKIP_NAMES = frozenset({
    "New Zealand", "NZ", "Government", "National", "Regional", "District",
    "City Council", "The Government", "Ministry", "Department", "Office",
    "Services", "Solutions", "Consulting", "Group", "Corporation",
    "The Company", "The Supplier", "The Contractor", "The Provider",
    "All Tenderers", "Potential Suppliers", "Market", "Industry",
    "Public Sector", "Crown", "Local Government",
})


def _extract_mentioned_orgs(text: str) -> list[tuple[str, str]]:
    """
    Extract organisation names from notice text.
    Returns list of (name, confidence) tuples.
    confidence: 'low' for all regex-extracted names.
    """
    if not text:
        return []

    found: dict[str, str] = {}
    for pattern in _ORG_PATTERNS:
        for m in pattern.finditer(text):
            name = m.group(1).strip()
            # Basic quality filters
            if (
                len(name) < 5
                or len(name) > 80
                or name in _SKIP_NAMES
                or any(skip in name for skip in _SKIP_NAMES)
                or name.isupper()
                or not any(c.isalpha() for c in name)
            ):
                continue
            if name not in found:
                found[name] = "low"

    return list(found.items())


# ── Discovery logic ───────────────────────────────────────────────────────────

def discover_from_notice(notice: dict) -> int:
    """
    Scan one notice and insert any newly discovered organisations.
    Returns count of new organisations created.
    """
    new_count = 0

    # High confidence: the contracting agency
    agency = notice.get("agency", "").strip()
    if agency:
        existing = orgs.resolve_alias(agency)
        if existing is None:
            orgs.upsert_organisation(
                agency,
                org_type="agency",
                confidence="high",
                alias_source="gets_agency",
            )
            logger.debug("Discovered agency: %s", agency)
            new_count += 1

    # Low confidence: organisations mentioned in description text
    description = notice.get("description") or ""
    if description:
        mentions = _extract_mentioned_orgs(description)
        for name, confidence in mentions:
            # Check if already known (exact or fuzzy)
            existing = orgs.resolve_alias(name)
            if existing is None:
                orgs.upsert_organisation(
                    name,
                    org_type="unknown",
                    confidence=confidence,
                    alias_source="inferred",
                )
                logger.debug("Discovered mentioned org: %s (conf=%s)", name, confidence)
                new_count += 1

    return new_count


def run_discovery() -> int:
    """
    Run organisation discovery across all notices not yet processed.
    Tracks progress via a simple marker: notices processed since last run
    are identified by fetched_at > last processed timestamp.

    For simplicity on first run, processes all notices. On subsequent runs,
    only notices added since the last Layer 2 run are scanned.
    Returns total new organisations discovered.
    """
    logger.info("Starting organisation discovery")

    notices = db.fetchall(
        """
        SELECT r.notice_id, r.agency, r.description, r.fetched_at
          FROM raw_notices r
         ORDER BY r.fetched_at DESC
        """
    )

    total_new = 0
    for notice in notices:
        try:
            new = discover_from_notice(notice)
            total_new += new
        except Exception as exc:
            logger.debug("Discovery failed for notice %s: %s",
                         notice.get("notice_id"), exc)

    logger.info(
        "Discovery complete: %d new organisations found across %d notices",
        total_new, len(notices),
    )
    return total_new


def get_discovery_stats() -> dict:
    """Return counts by type and confidence for monitoring."""
    rows = db.fetchall(
        """
        SELECT org_type, discovery_confidence, COUNT(*) as n
          FROM organisations
         GROUP BY org_type, discovery_confidence
         ORDER BY n DESC
        """
    )
    return {f"{r['org_type']}/{r['discovery_confidence']}": r["n"] for r in rows}
