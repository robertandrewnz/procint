"""
Layer 2 — Organisation entity management.

Every named organisation that appears anywhere in the system is stored in the
`organisations` table with a canonical name. Raw name variants are stored in
`name_aliases` and resolved back to the canonical record on lookup.

The knowledge graph grows automatically each time Layer 1 runs:
  - Contracting agencies from raw_notices are seeded as org_type='agency'.
  - Bidder names from bidder_pool are seeded as org_type='bidder'.
  - If an agency appears as a supplier in an award notice, its type is promoted
    to 'both'.

Fuzzy matching (rapidfuzz) is used to catch common name variants:
  "Ministry of Education - School Infrastructure" → "Ministry of Education"
  "NZ Transport Agency" → "New Zealand Transport Agency (Waka Kotahi)"
"""
import logging
import re
from typing import Optional

import config
import db

logger = logging.getLogger(__name__)

try:
    from rapidfuzz import fuzz, process as rfuzz_process
    _RAPIDFUZZ_AVAILABLE = True
except ImportError:
    _RAPIDFUZZ_AVAILABLE = False
    logger.warning("rapidfuzz not installed — fuzzy org matching disabled; pip install rapidfuzz")


# ── Name normalisation ────────────────────────────────────────────────────────

_STRIP_SUFFIXES = re.compile(
    r"\s*[-–—]\s*(school infrastructure|historic[a-z]*|nortthland|"
    r"te whatu ora|health new zealand|waka kotahi|northern|southern|"
    r"eastern|western|central|auckland|wellington|christchurch|"
    r"regional|district|city|metropolitan)\s*$",
    re.IGNORECASE,
)

_NORMALISE_WORDS = {
    "nz":    "New Zealand",
    "dept":  "Department",
    "dept.": "Department",
    "moe":   "Ministry of Education",
    "mpi":   "Ministry for Primary Industries",
    "msd":   "Ministry of Social Development",
    "acc":   "Accident Compensation Corporation",
    "nzdf":  "New Zealand Defence Force",
}


def normalise_name(raw: str) -> str:
    """
    Light normalisation for fuzzy matching — does NOT change the canonical
    stored name, just makes comparison more reliable.
    """
    name = raw.strip()
    # Strip common subdivision suffixes
    name = _STRIP_SUFFIXES.sub("", name).strip()
    # Collapse multiple spaces
    name = re.sub(r"\s{2,}", " ", name)
    return name


# ── Alias resolution ──────────────────────────────────────────────────────────

def resolve_alias(raw_name: str) -> Optional[int]:
    """
    Look up an organisation by raw name. Tries exact alias match first,
    then fuzzy match against all canonical names if rapidfuzz is available.
    Returns org_id or None.
    """
    if not raw_name or not raw_name.strip():
        return None

    name = raw_name.strip()

    # 1. Exact alias lookup
    row = db.fetchone(
        "SELECT org_id FROM name_aliases WHERE alias = %s", (name,)
    )
    if row:
        return row["org_id"]

    # 2. Exact canonical name lookup
    row = db.fetchone(
        "SELECT org_id FROM organisations WHERE name = %s", (name,)
    )
    if row:
        return row["org_id"]

    # 3. Fuzzy match against canonical names
    if _RAPIDFUZZ_AVAILABLE:
        canonical_rows = db.fetchall("SELECT org_id, name FROM organisations")
        if canonical_rows:
            names_list = [r["name"] for r in canonical_rows]
            result = rfuzz_process.extractOne(
                normalise_name(name),
                [normalise_name(n) for n in names_list],
                scorer=fuzz.token_set_ratio,
                score_cutoff=config.ORG_FUZZY_MATCH_THRESHOLD,
            )
            if result:
                idx = result[2]  # index into names_list
                return canonical_rows[idx]["org_id"]

    return None


# ── Upsert ────────────────────────────────────────────────────────────────────

def upsert_organisation(
    name: str,
    org_type: str = "unknown",
    sector_tags: Optional[str] = None,
    size: Optional[str] = None,
    headquarters: Optional[str] = None,
    confidence: str = "medium",
    alias_source: str = "inferred",
) -> int:
    """
    Find or create an organisation record. Returns org_id.

    If the name matches an existing record (exact or fuzzy), increments
    last_seen and returns the existing org_id. Otherwise creates a new record.

    org_type promotion: agency → both ← bidder (once seen on both sides).
    """
    # Check if already known
    existing_id = resolve_alias(name)

    if existing_id is not None:
        # Update last_seen and potentially promote type
        existing = db.fetchone(
            "SELECT org_id, org_type, sector_tags FROM organisations WHERE org_id = %s",
            (existing_id,),
        )
        if existing:
            new_type = _promote_type(existing["org_type"], org_type)
            db.execute(
                """
                UPDATE organisations
                   SET last_seen = NOW(),
                       org_type  = %s
                 WHERE org_id    = %s
                """,
                (new_type, existing_id),
            )
            # Register this name as an alias if not already recorded
            _add_alias(existing_id, name, alias_source, confidence)
            return existing_id

    # Create new record
    row = db.fetchone(
        """
        INSERT INTO organisations
            (name, org_type, sector_tags, size, headquarters, discovery_confidence)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (name) DO UPDATE SET
            last_seen = NOW(),
            org_type  = CASE
                WHEN organisations.org_type = EXCLUDED.org_type THEN EXCLUDED.org_type
                WHEN organisations.org_type = 'both'            THEN 'both'
                WHEN EXCLUDED.org_type = 'both'                 THEN 'both'
                WHEN organisations.org_type = 'unknown'         THEN EXCLUDED.org_type
                WHEN EXCLUDED.org_type = 'unknown'              THEN organisations.org_type
                ELSE 'both'
            END
        RETURNING org_id
        """,
        (name, org_type, sector_tags, size, headquarters, confidence),
    )
    org_id = row["org_id"]
    _add_alias(org_id, name, alias_source, confidence)
    logger.debug("Upserted organisation: %s (id=%d type=%s)", name, org_id, org_type)
    return org_id


def _promote_type(existing: str, incoming: str) -> str:
    """Promote org_type when a new role is observed."""
    if existing == incoming or incoming == "unknown":
        return existing
    if existing == "unknown":
        return incoming
    if existing == "both" or incoming == "both":
        return "both"
    if {existing, incoming} == {"agency", "bidder"}:
        return "both"
    return existing


def _add_alias(org_id: int, alias: str, source: str, confidence: str) -> None:
    db.execute(
        """
        INSERT INTO name_aliases (org_id, alias, source, confidence)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (alias) DO NOTHING
        """,
        (org_id, alias, source, confidence),
    )


# ── Stats refresh ─────────────────────────────────────────────────────────────

def refresh_notice_counts() -> None:
    """
    Recompute notice_count on organisations by counting alias matches in
    raw_notices.agency. Called at the end of each Layer 2 run.
    """
    db.execute(
        """
        UPDATE organisations o
           SET notice_count = (
               SELECT COUNT(DISTINCT r.notice_id)
                 FROM raw_notices r
                 JOIN name_aliases a ON a.alias = r.agency
                WHERE a.org_id = o.org_id
           )
        """
    )
    logger.debug("Refreshed notice counts on organisations")


def refresh_award_counts() -> None:
    """Recompute award_count and total_awarded_value from contract_awards."""
    db.execute(
        """
        UPDATE organisations o
           SET award_count          = COALESCE(s.cnt, 0),
               total_awarded_value  = COALESCE(s.total, 0)
          FROM (
              SELECT supplier_org_id,
                     COUNT(*)       AS cnt,
                     SUM(contract_value) AS total
                FROM contract_awards
               WHERE supplier_org_id IS NOT NULL
               GROUP BY supplier_org_id
          ) s
         WHERE o.org_id = s.supplier_org_id
        """
    )
    logger.debug("Refreshed award counts on organisations")


# ── Seeding from Layer 1 ──────────────────────────────────────────────────────

def _insert_org(
    name: str,
    org_type: str = "unknown",
    sector_tags: Optional[str] = None,
    size: Optional[str] = None,
    headquarters: Optional[str] = None,
    confidence: str = "medium",
    alias_source: str = "inferred",
) -> int:
    """
    Direct insert/upsert without the resolve_alias lookup overhead.
    Used during bulk seeding when we've already checked the cache.
    """
    row = db.fetchone(
        """
        INSERT INTO organisations
            (name, org_type, sector_tags, size, headquarters, discovery_confidence)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (name) DO UPDATE SET
            last_seen = NOW(),
            org_type  = CASE
                WHEN organisations.org_type = 'both'    THEN 'both'
                WHEN EXCLUDED.org_type = 'both'         THEN 'both'
                WHEN organisations.org_type = 'unknown' THEN EXCLUDED.org_type
                WHEN EXCLUDED.org_type = 'unknown'      THEN organisations.org_type
                WHEN organisations.org_type = EXCLUDED.org_type THEN EXCLUDED.org_type
                ELSE 'both'
            END
        RETURNING org_id
        """,
        (name, org_type, sector_tags, size, headquarters, confidence),
    )
    org_id = row["org_id"]
    _add_alias(org_id, name, alias_source, confidence)
    return org_id


def _touch_org(org_id: int, incoming_type: str) -> None:
    """Update last_seen and promote org_type if needed. No resolve_alias call."""
    db.execute(
        """
        UPDATE organisations
           SET last_seen = NOW(),
               org_type  = CASE
                   WHEN org_type = 'both'    THEN 'both'
                   WHEN %s = 'both'          THEN 'both'
                   WHEN org_type = 'unknown' THEN %s
                   WHEN %s = 'unknown'       THEN org_type
                   WHEN org_type = %s        THEN org_type
                   ELSE 'both'
               END
         WHERE org_id = %s
        """,
        (incoming_type, incoming_type, incoming_type, incoming_type, org_id),
    )


def _build_lookup_cache() -> tuple[dict, dict]:
    """
    Load all existing names and aliases into memory for bulk seeding.
    Returns (aliases_dict: alias→org_id, canonical_dict: name→org_id).
    """
    aliases = {r["alias"]: r["org_id"]
               for r in db.fetchall("SELECT alias, org_id FROM name_aliases")}
    canonicals = {r["name"]: r["org_id"]
                  for r in db.fetchall("SELECT org_id, name FROM organisations")}
    return aliases, canonicals


def _resolve_from_cache(
    name: str,
    aliases_cache: dict,
    canonicals_cache: dict,
) -> Optional[int]:
    """Resolve org name using in-memory caches — no DB round-trips per call."""
    if not name:
        return None
    if name in aliases_cache:
        return aliases_cache[name]
    if name in canonicals_cache:
        return canonicals_cache[name]
    if _RAPIDFUZZ_AVAILABLE and canonicals_cache:
        names_list = list(canonicals_cache.keys())
        result = rfuzz_process.extractOne(
            normalise_name(name),
            [normalise_name(n) for n in names_list],
            scorer=fuzz.token_set_ratio,
            score_cutoff=config.ORG_FUZZY_MATCH_THRESHOLD,
        )
        if result:
            matched_name = names_list[result[2]]
            return canonicals_cache[matched_name]
    return None


def seed_from_layer1() -> dict[str, int]:
    """
    Seed the organisations table from existing Layer 1 data.
    Idempotent — safe to re-run. Returns counts of new records created.
    Uses in-memory caches to avoid N+1 DB queries during bulk seeding.
    """
    new_agencies = 0
    new_bidders = 0

    # Build in-memory lookup caches once upfront
    aliases_cache, canonicals_cache = _build_lookup_cache()

    # ── Contracting agencies from raw_notices ────────────────────────────────
    agencies = db.fetchall(
        "SELECT DISTINCT agency FROM raw_notices WHERE agency IS NOT NULL AND agency != ''"
    )
    for row in agencies:
        name = row["agency"].strip()
        if not name:
            continue
        existing = _resolve_from_cache(name, aliases_cache, canonicals_cache)
        if existing is None:
            org_id = _insert_org(name, org_type="agency", confidence="high",
                                  alias_source="gets_agency")
            aliases_cache[name] = org_id
            canonicals_cache[name] = org_id
            new_agencies += 1
        else:
            _touch_org(existing, "agency")

    # ── Bidder names from bidder_pool ────────────────────────────────────────
    # Use only columns guaranteed present across all schema versions.
    # Richer metadata (headquarters) is sourced from bidder CSV below.
    bidders = db.fetchall(
        """
        SELECT DISTINCT bp.firm_name,
               MAX(bp.sector) as sector,
               MAX(bp.size) as size
          FROM bidder_pool bp
         GROUP BY bp.firm_name
        """
    )

    # Also pull from bidder CSV for richer metadata
    from bidders import load_bidders
    csv_bidders = {b["firm_name"]: b for b in load_bidders()}

    seen_bidders: set[str] = set()
    for row in bidders:
        name = row["firm_name"]
        if name in seen_bidders:
            continue
        seen_bidders.add(name)

        csv_row = csv_bidders.get(name, {})
        sector_tags = csv_row.get("sectors") or row.get("sector")
        size = csv_row.get("size") or row.get("size")
        hq = csv_row.get("headquarters") or row.get("headquarters")

        existing = _resolve_from_cache(name, aliases_cache, canonicals_cache)
        if existing is None:
            org_id = _insert_org(name, org_type="bidder",
                                  sector_tags=sector_tags, size=size,
                                  headquarters=hq, confidence="high",
                                  alias_source="bidder_csv")
            aliases_cache[name] = org_id
            canonicals_cache[name] = org_id
            new_bidders += 1
        else:
            _touch_org(existing, "bidder")

    refresh_notice_counts()
    logger.info(
        "Seeding complete: %d new agencies, %d new bidders",
        new_agencies, new_bidders,
    )
    return {"new_agencies": new_agencies, "new_bidders": new_bidders}


def get_org_by_name(name: str) -> Optional[dict]:
    """Fetch a full organisation record by name (exact or alias)."""
    org_id = resolve_alias(name)
    if org_id is None:
        return None
    return db.fetchone("SELECT * FROM organisations WHERE org_id = %s", (org_id,))


def get_top_agencies_by_activity(limit: int = 20) -> list[dict]:
    """Return agencies ranked by notice count."""
    return db.fetchall(
        """
        SELECT o.org_id, o.name, o.notice_count, o.award_count,
               o.total_awarded_value, o.sector_tags
          FROM organisations o
         WHERE o.org_type IN ('agency', 'both')
         ORDER BY o.notice_count DESC
         LIMIT %s
        """,
        (limit,),
    )
