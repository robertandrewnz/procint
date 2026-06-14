"""
Bidder pool inference module — evidence-based using MBIE historical data.

Matching strategy (in priority order):
  1. MBIE win history — query supplier_win_history for firms with proven wins
     in the same UNSPSC product category and/or with the same agency.
     Produces factual reasoning bullets: "Won 14 road maintenance contracts
     since 2019 including 3 with this agency".

  2. CSV fallback — if MBIE win history is empty or sparse (new market entry,
     gap in data), fall back to keyword/sector matching against bidders.csv.
     Reasoning bullets are prefixed "[inferred]" to distinguish from evidence.

Scoring:
  - agency_wins:   contracts won with THIS specific agency in this sector  (highest weight)
  - category_wins: contracts won in the same UNSPSC product category       (medium weight)
  - total_wins:    total historical wins (sector-matched)                   (baseline)
  - recency:       years since last win (penalises stale history)

Exclusion:
  - SECTOR_EXCLUSION_MATRIX: hard rules — civil/roading firms can never appear
    in ICT/health/legal/aerospace results, etc.
  - Title keyword exclusion: checked against each firm's exclude_keywords column.
  - Specialist notice detection: for notices in SPECIALIST_SECTORS (e.g. aerospace)
    the candidate pool is filtered to firms with a matching specialist_flag; fewer
    than config.SPECIALIST_MIN_BIDDERS credible matches → show whatever is found
    rather than padding with irrelevant firms.

Deduplication:
  - canonical_suppliers.canonical_name() collapses name variants before ranking,
    ensuring "Fulton Hogan Canterbury" and "Fulton Hogan" count as one firm.

Layer 2 hooks:
  enrich_from_awards_history() — now implemented via MBIE data
  enrich_from_web()            — populated in Layer 2 Phase 2
"""
import csv
import logging
import re
from datetime import date
from pathlib import Path
from typing import Optional

import config
import db
from canonical_suppliers import canonical_name, deduplicate_bidders

logger = logging.getLogger(__name__)


# ── Sector exclusion matrix ────────────────────────────────────────────────────
# Maps a notice's effective sector → set of firm sector tags that are BANNED.
# A firm is excluded if its sector list contains ANY banned tag.
# Read: "if the notice is aerospace, exclude firms whose sectors include
#        FM, infrastructure, roading, construction, …"

SECTOR_EXCLUSION_MATRIX: dict[str, set[str]] = {
    "aerospace": {
        "FM", "infrastructure", "roading", "construction", "civil",
        "security", "ICT", "health", "legal", "advisory",
    },
    "ICT": {
        "FM", "infrastructure", "roading", "construction", "civil",
        "aerospace", "health",
    },
    "cybersecurity": {
        "FM", "infrastructure", "roading", "construction", "civil",
        "aerospace", "health",
    },
    "health": {
        "FM", "infrastructure", "roading", "construction", "civil",
        "aerospace", "ICT",
    },
    "legal": {
        "FM", "infrastructure", "roading", "construction", "civil",
        "aerospace", "ICT", "health", "security",
    },
    "advisory": {
        "FM", "infrastructure", "roading", "construction", "civil",
        "aerospace",
    },
    "roading": {
        "FM", "ICT", "health", "legal", "advisory",
        "aerospace", "cybersecurity",
    },
    "infrastructure": {
        "ICT", "health", "legal", "aerospace", "cybersecurity",
    },
    "construction": {
        "ICT", "health", "legal", "aerospace", "cybersecurity",
    },
    "FM": {
        "ICT", "health", "legal", "aerospace", "cybersecurity",
        "roading", "construction",
    },
    "security": {
        "roading", "construction", "civil", "aerospace",
        "health", "ICT",
    },
    "defence": {
        "FM", "roading", "construction", "civil",
        "health", "ICT", "cybersecurity", "advisory",
    },
}

# ── Firm-level sector overrides ───────────────────────────────────────────────
# Maps lowercase canonical firm name → correct sector tag.
# Used when supplier_win_history.primary_sector is wrong due to UNSPSC miscoding
# in MBIE data (e.g. an IT firm's contracts categorised under infrastructure codes).
# Add entries here for any known misclassifications discovered via _audit_firm_sectors.py.

FIRM_SECTOR_OVERRIDES: dict[str, str] = {
    # ── Already present ───────────────────────────────────────────────────────
    "fusion5":              "ICT",   # ERP/Microsoft Dynamics — miscoded as infrastructure
    "empired":              "ICT",   # IT managed services — now Revolent Group
    "revolent group":       "ICT",   # Formerly Empired; IT services
    # ── Added from firm sector audit (infrastructure → ICT) ──────────────────
    "asap contracting":     "ICT",   # ASAP Contracting Ltd — ICT services provider
    "fujitsu nz":           "ICT",   # Fujitsu / FUJITSU NEW ZEALAND LIMITED
    "dimension data":       "ICT",   # Dimension Data NZ — managed IT/networking
    "accenture nz":         "ICT",   # Accenture — technology consulting (all NZ variants)
    "accenture limitedc":   "ICT",   # ACCENTURE NZ LIMITEDc — DB entry with suffix typo
    "hewlett packard":      "ICT",   # Hewlett Packard New Zealand — IT hardware/services
    "datacom":              "ICT",   # DATACOM GROUP LIMITED — IT managed services
    "datacom systems":      "ICT",   # Datacom Systems Limited — IT services
    "spark nz":             "ICT",   # Spark NZ Limited — telco/ICT
    "thp":                  "ICT",   # THP INC Ltd — ICT services
    "assurity consulting":  "ICT",   # Assurity Consulting — software quality/testing
    "dxc technology":       "ICT",   # DXC Technology NZ — IT services (standard variants)
    "dxc technology limit": "ICT",   # DXC TECHNOLOGY NZ LIMIT — truncated DB entry
}

# ── Firm-level exclusion sector pins ─────────────────────────────────────────
# Hard-pins effective sector(s) for bidder EXCLUSION purposes regardless of
# what their bidders.csv entry says.  Use when MBIE award history or broad CSV
# sector tagging causes a firm to appear in notices outside their core market.
#
# Key: lowercase firm name exactly as it appears in bidder_pool.firm_name
# Value: set of sector strings used against SECTOR_EXCLUSION_MATRIX

FIRM_EXCLUSION_SECTORS: dict[str, set[str]] = {
    # Civil construction only — must never appear in ICT, defence, health,
    # advisory, cybersecurity, or psychometric notices.
    "heb construction": {"construction", "civil", "infrastructure"},
}

# ── Title-keyword exclusion triggers ──────────────────────────────────────────
# If a notice title contains any of these phrases the named sector firms are hard-excluded.
# This catches misclassified notices (e.g. aerospace notice tagged "infrastructure").

TITLE_KEYWORD_SECTOR_EXCLUSIONS: list[tuple[list[str], set[str]]] = [
    # Aerospace / defence keywords → exclude non-aerospace firms
    (
        ["aircraft", "airframe", "propulsion", "avionics", "rnzaf", "rnzn",
         "aviation", "aerospace", "airworthiness", "rotary wing", "fixed wing",
         "engine overhaul", "structural integrity"],
        {"FM", "infrastructure", "roading", "construction", "civil",
         "security", "ICT", "health", "legal", "advisory", "utilities",
         "professional_services", "cybersecurity"},
    ),
    # Legal / solicitation keywords
    (
        ["legal services", "legal advice", "solicitor", "barrister", "counsel",
         "law firm", "litigation", "conveyancing", "judicial"],
        {"FM", "infrastructure", "roading", "construction", "civil",
         "aerospace", "ICT", "health", "security"},
    ),
    # Medical / clinical keywords
    (
        ["clinical", "surgical", "pharmaceutical", "pathology", "radiology",
         "diagnostic", "patient", "hospital", "medical device", "health information"],
        {"FM", "infrastructure", "roading", "construction", "civil",
         "aerospace", "ICT"},
    ),
    # Pure cyber / infosec keywords
    (
        ["penetration test", "pen test", "soc services", "security operations centre",
         "siem", "cyber incident", "vulnerability assessment", "iso 27001",
         "information security", "data governance"],
        {"FM", "infrastructure", "roading", "construction", "civil",
         "aerospace", "health"},
    ),
    # Road maintenance / civils keywords
    (
        ["road maintenance", "pavement", "seal coat", "chip seal", "pothole",
         "roading network", "traffic management", "kerb and channel",
         "bridge maintenance", "drainage works", "earthworks"],
        {"ICT", "health", "legal", "aerospace", "cybersecurity"},
    ),
]

# ── Specialist sectors ─────────────────────────────────────────────────────────
# For notices that match these sectors the pool is pre-filtered to firms with
# a matching specialist_flag. If fewer than SPECIALIST_MIN_BIDDERS survive,
# the results are returned as-is rather than padding with irrelevant firms.

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
    Checks both sector_tag and title keywords.
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


# ── MBIE data availability check ──────────────────────────────────────────────

def _mbie_available() -> bool:
    """Check whether the MBIE supplier_win_history view is populated."""
    try:
        row = db.fetchone("SELECT COUNT(*) as n FROM supplier_win_history")
        return bool(row and row["n"] > 0)
    except Exception:
        return False


# ── UNSPSC category keyword extraction ───────────────────────────────────────
# Extracts domain-specific keywords from a notice title to drive UNSPSC lookups.

_STOP = frozenset({
    "the", "and", "for", "of", "in", "to", "a", "an", "at", "by", "or",
    "with", "from", "into", "via", "new", "zealand", "nz", "government",
    "contract", "services", "service", "project", "works", "supply",
    "provision", "request", "proposal",
})


def _title_keywords(title: str) -> list[str]:
    """Extract meaningful multi-word and single-word phrases from a notice title."""
    words = re.findall(r"[A-Za-z]{3,}", title.lower())
    return [w for w in words if w not in _STOP][:8]


# ── MBIE-based inference ──────────────────────────────────────────────────────

def _mbie_bidders_for_notice(notice: dict) -> list[dict]:
    """
    Query MBIE win history to find suppliers with proven track records
    matching this notice. Returns ranked list with factual reasoning.
    """
    from historical_data import (
        get_suppliers_by_sector_and_agency,
        get_suppliers_by_category,
        get_agency_win_count,
    )

    sector = notice.get("sector_tag") or "other"
    agency = (notice.get("agency") or "").strip()
    title = notice.get("title") or ""
    title_kws = _title_keywords(title)

    # Primary: UNSPSC category match (most specific)
    cat_results = get_suppliers_by_category(
        unspsc_desc_keywords=title_kws,
        agency_name=agency,
        limit=20,
    )

    # Secondary: sector + agency match
    sector_results = get_suppliers_by_sector_and_agency(
        sector_tag=sector,
        agency_name=agency,
        limit=20,
    )

    # Merge: prefer category results, supplement with sector results
    seen: set[str] = set()
    combined: list[dict] = []
    for r in cat_results + sector_results:
        name = r.get("supplier_name") or r.get("business_name") or ""
        if name and name not in seen:
            seen.add(name)
            combined.append(r)

    if not combined:
        return []

    results = []
    today = date.today()

    # Pre-compute exclusion context for this notice
    notice_sector = sector  # the notice's sector_tag
    excluded_for_notice = SECTOR_EXCLUSION_MATRIX.get(notice_sector, set())
    excluded_lower = {e.lower() for e in excluded_for_notice}
    notice_title_lower = title.lower()
    notice_desc_lower = (notice.get("description") or "")[:600].lower()
    notice_text_lower = notice_title_lower + " " + notice_desc_lower

    # Physical-works taxonomy (all lowercase for comparison against lowercased firm sector)
    _PHYSICAL_WORKS = {"construction", "roading", "civil", "infrastructure", "fm"}
    # Title/description signals that a notice involves physical works
    _PHYSICAL_TITLE_SIGNALS = {
        "building", "construct", "infrastructure", "roading", "maintenance",
        "civil", "facility", "upgrade", "installation", "earthworks", "structural",
        "bridge", "pavement", "drainage", "demolition", "fitout",
    }
    # UNSPSC category description keywords indicating physical-works specialist
    _PHYSICAL_CAT_SIGNALS = {
        "road", "highway", "roading", "bridge", "civil eng", "civil con",
        "construction", "surfacing", "paving", "earthwork", "drainage work",
        "demolition", "excavat", "geotechnical", "structural steel",
        "land development", "water and sewer", "pipeline",
    }
    # Signals that a notice is for services/advisory/technology (no physical works)
    _SERVICES_SIGNALS = {
        "advisory", "consulting", "professional services", "management services",
        "strategy", "research", "analysis", "training", "audit",
        "software", "ict", "it services", "digital", "technology",
        "platform", "system development", "application", "data", "cyber",
        "recruitment", "legal services", "financial services",
    }

    notice_is_physical = any(sig in notice_text_lower for sig in _PHYSICAL_TITLE_SIGNALS)
    notice_is_services = (
        not notice_is_physical
        and any(sig in notice_text_lower for sig in _SERVICES_SIGNALS)
    )

    for r in combined[:30]:
        name = r.get("supplier_name") or r.get("business_name") or ""
        if not name:
            continue

        # Get the firm's actual primary_sector from supplier_win_history.
        # Keep original case for storage (so downstream _firm_is_excluded() can
        # match against SECTOR_EXCLUSION_MATRIX which uses mixed case like "FM","ICT").
        # Use a separate lowercase var for internal comparisons.
        firm_primary_sector = (r.get("primary_sector") or "").strip()
        firm_ps_lower = firm_primary_sector.lower()

        # Apply firm-level sector override for known MBIE misclassifications.
        firm_canon_lower = canonical_name(name).lower()
        if firm_canon_lower in FIRM_SECTOR_OVERRIDES:
            firm_primary_sector = FIRM_SECTOR_OVERRIDES[firm_canon_lower]
            firm_ps_lower = firm_primary_sector.lower()

        # Detect physical-works firm via matched_categories when primary_sector is absent
        firm_categories = r.get("matched_categories") or []
        if isinstance(firm_categories, str):
            firm_categories = [firm_categories]
        cat_text = " ".join(c.lower() for c in firm_categories if isinstance(c, str))
        firm_is_physical = (
            firm_ps_lower in _PHYSICAL_WORKS
            or (cat_text and any(sig in cat_text for sig in _PHYSICAL_CAT_SIGNALS))
        )

        # Rule 1: cross-sector exclusion via matrix (case-insensitive)
        if firm_ps_lower and firm_ps_lower in excluded_lower:
            logger.debug(
                "MBIE: skipping %s (firm sector '%s' excluded from notice sector '%s', notice %s)",
                name, firm_primary_sector, notice_sector,
                notice.get("notice_id", ""),
            )
            continue

        # Rule 2: physical-works firm in a services/advisory/tech notice
        if firm_is_physical and notice_is_services:
            logger.debug(
                "MBIE: skipping %s (physical-works firm in services notice: %s)",
                name, title[:60],
            )
            continue

        # Rule 3: for unclassified notices ('other'/'unknown'), exclude physical-works
        # firms unless the notice title/description itself is construction-related.
        if (
            notice_sector in ("other", "unknown", "")
            and firm_is_physical
            and not notice_is_physical
        ):
            logger.debug(
                "MBIE: skipping %s (physical-works firm in unclassified non-construction notice: %s)",
                name, title[:60],
            )
            continue

        total_wins = int(r.get("total_wins") or r.get("category_wins") or 0)
        agency_wins = int(r.get("agency_wins") or 0)
        last_win = r.get("last_win_date") or r.get("last_category_win")

        # Recency penalty
        years_since = 10.0
        if last_win:
            try:
                lw_date = last_win if isinstance(last_win, date) else date.fromisoformat(str(last_win))
                years_since = max(0, (today - lw_date).days / 365.25)
            except Exception:
                pass

        # Relevance score: agency wins worth 3x, category wins 2x, total wins 1x
        recency_factor = max(0.2, 1.0 - (years_since / 10.0))
        relevance = (agency_wins * 3 + total_wins * 1) * recency_factor
        if relevance == 0:
            continue

        # Build factual reasoning bullets
        reasoning = _mbie_reasoning(
            name, total_wins, agency_wins, last_win, years_since,
            agency, sector,
            r.get("matched_categories") or [],
            r.get("avg_contract_value"),
        )

        results.append({
            "firm_name": name,
            "sector": firm_primary_sector or notice_sector,  # original case so _firm_is_excluded() can match the matrix
            "size": None,  # not in MBIE data; Layer 2 may enrich from organisations table
            "strategic_importance": _importance_from_wins(agency_wins, total_wins),
            "intelligence_maturity": _maturity_from_wins(total_wins, years_since),
            "relevance_score": min(round(relevance / 100, 4), 1.0),
            "match_type": "mbie_evidence",
            "reasoning": reasoning,
            "company_context": None,
            "context_confidence": "unknown",
            "total_wins": total_wins,
            "agency_wins": agency_wins,
            "last_win_date": str(last_win) if last_win else None,
            "_sort_key": (
                -agency_wins,
                -total_wins,
                years_since,
            ),
        })

    results.sort(key=lambda x: x["_sort_key"])
    for r in results:
        del r["_sort_key"]

    # Apply canonical deduplication before returning
    results = deduplicate_bidders(results)
    # Update firm_name to canonical form
    for r in results:
        r["firm_name"] = r.get("canonical_name") or r["firm_name"]

    return results


def _mbie_reasoning(
    name: str,
    total_wins: int,
    agency_wins: int,
    last_win,
    years_since: float,
    agency: str,
    sector: str,
    matched_categories: list,
    avg_value: Optional[float],
) -> list[str]:
    """Generate factual reasoning bullets from MBIE win data."""
    bullets = []

    # Primary win evidence
    if agency_wins > 0:
        agency_short = agency.split("(")[0].strip()[:40]
        since_str = ""
        if last_win:
            try:
                yr = int(str(last_win)[:4])
                since_str = f" since {yr}"
            except Exception:
                pass
        bullets.append(
            f"Won {total_wins} {sector.replace('_',' ')} contract{'s' if total_wins!=1 else ''}"
            f"{since_str} — including {agency_wins} with {agency_short}"
        )
    elif total_wins > 0:
        since_str = ""
        if last_win:
            try:
                yr = int(str(last_win)[:4])
                since_str = f" since {yr}"
            except Exception:
                pass
        bullets.append(
            f"Won {total_wins} {sector.replace('_',' ')} contract{'s' if total_wins!=1 else ''}"
            f"{since_str} (no recorded wins with this agency)"
        )

    # Category specificity
    if matched_categories and isinstance(matched_categories, list):
        cats = [c for c in matched_categories if isinstance(c, str) and len(c) > 5][:2]
        if cats:
            bullets.append(f"Matched categories: {'; '.join(c[:60] for c in cats)}")

    # Recency flag
    if years_since > 5:
        bullets.append(f"Most recent win was {years_since:.0f} years ago — verify current capability")

    # Average value
    if avg_value and avg_value > 0:
        if avg_value >= 1_000_000:
            val_str = f"${avg_value/1_000_000:.1f}M"
        elif avg_value >= 1_000:
            val_str = f"${avg_value/1_000:.0f}K"
        else:
            val_str = f"${avg_value:.0f}"
        bullets.append(f"Average win value: {val_str}")

    return bullets[:3]


def _importance_from_wins(agency_wins: int, total_wins: int) -> str:
    if agency_wins >= 2 or total_wins >= 10:
        return "high"
    if agency_wins >= 1 or total_wins >= 3:
        return "medium"
    return "low"


def _maturity_from_wins(total_wins: int, years_since: float) -> str:
    if total_wins >= 5 and years_since <= 3:
        return "strong"
    if total_wins >= 2 or years_since <= 5:
        return "moderate"
    return "weak"


# ── Shared exclusion helper ───────────────────────────────────────────────────

def _firm_is_excluded(firm_sectors: list[str], notice: dict,
                      firm_name: str = "") -> bool:
    """
    Return True if this firm should be excluded from bidder results for this notice.

    Three-pass check:
    0. Firm-level sector override — FIRM_EXCLUSION_SECTORS pins effective sectors
       for specific firms regardless of CSV/MBIE data.
    1. Sector exclusion matrix — based on notice sector_tag.
    2. Title keyword triggers — catches misclassified notices (e.g. aerospace
       notice tagged 'infrastructure' because GETS has no aerospace category).
    """
    # Pass 0: firm-level override
    if firm_name:
        override = FIRM_EXCLUSION_SECTORS.get(firm_name.strip().lower())
        if override is not None:
            firm_sectors = list(override)

    notice_sector = (notice.get("sector_tag") or "other").lower()
    title = (notice.get("title") or "").lower()

    # Pass 1: sector matrix
    banned_by_sector = SECTOR_EXCLUSION_MATRIX.get(notice_sector, set())
    if banned_by_sector and any(fs in banned_by_sector for fs in firm_sectors):
        return True

    # Pass 2: title keyword triggers
    for trigger_kws, banned_sectors in TITLE_KEYWORD_SECTOR_EXCLUSIONS:
        if any(kw in title for kw in trigger_kws):
            if any(fs in banned_sectors for fs in firm_sectors):
                return True

    return False


# ── CSV-based fallback (original logic, kept as fallback) ─────────────────────

SIZE_MATURITY_MAP = {
    "micro":  "weak",
    "small":  "weak",
    "medium": "moderate",
    "large":  "strong",
    "major":  "strong",
}

VALUE_IMPORTANCE_THRESHOLDS = {
    "10m_plus":   {"major", "large"},
    "2m_10m":     {"major", "large", "medium"},
    "500k_2m":    {"major", "large", "medium", "small"},
    "100k_500k":  {"major", "large", "medium", "small", "micro"},
    "under_100k": {"major", "large", "medium", "small", "micro"},
    "unknown":    {"major", "large", "medium", "small", "micro"},
}


def load_bidders(csv_path: str = config.BIDDER_CSV_PATH) -> list[dict]:
    path = Path(csv_path)
    if not path.exists():
        logger.warning("Bidder CSV not found at %s", csv_path)
        return []
    bidders = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Parse pipe-separated fields
            row["_sectors"] = [s.strip() for s in row.get("sectors", "").split("|") if s.strip()]
            row["_exclude_keywords"] = [
                k.strip().lower()
                for k in row.get("exclude_keywords", "").split("|")
                if k.strip()
            ]
            # canonical_name: fall back to firm_name if column absent
            if not row.get("canonical_name"):
                row["canonical_name"] = canonical_name(row.get("firm_name", ""))
            # specialist_flags column (new schema)
            # Already available as row["specialist_flags"] — used in _csv_bidders_for_notice
            # mbie_confirmed: coerce to bool
            row["mbie_confirmed"] = (row.get("mbie_confirmed") or "").strip().lower() in ("true", "1", "yes")
            bidders.append(row)
    logger.info("Loaded %d bidders from CSV (%s)", len(bidders), csv_path)
    return bidders


def _csv_bidders_for_notice(
    notice: dict,
    all_bidders: list[dict],
    specialist_flag: Optional[str] = None,
) -> list[dict]:
    """
    CSV-based fallback matching. Returns results tagged as inferred.

    If specialist_flag is set, only firms with that flag in specialist_flags
    column pass through — this filters the pool to credible specialists only.
    """
    sector = notice.get("sector_tag") or "other"
    value_band = notice.get("value_band") or "unknown"
    notice_text = (
        (notice.get("title") or "") + " " + (notice.get("description") or "")
    ).lower()

    candidates = []
    for firm in all_bidders:
        firm_sectors = firm.get("_sectors", [])

        # Specialist filter: check FIRST so specialist firms aren't dropped by
        # the exclusion matrix (a misclassified notice may have a sector_tag
        # that conflicts with the firm's real sector, e.g. RNZAF aerospace
        # notice tagged 'infrastructure' would otherwise exclude Babcock via
        # SECTOR_EXCLUSION_MATRIX["infrastructure"] ∋ "aerospace").
        if specialist_flag:
            firm_flags = [f.strip() for f in firm.get("specialist_flags", "").split("|") if f.strip()]
            if specialist_flag not in firm_flags:
                continue
            # Passed specialist filter — skip sector exclusion matrix and
            # sector-match check; the specialist_flag is sufficient evidence.
            exact = True  # used in reasoning bullet below
        else:
            # Hard sector exclusion matrix + title keyword check
            if _firm_is_excluded(firm_sectors, notice, firm.get("firm_name", "")):
                continue

            # Explicit exclude_keywords on the firm (notice-text trigger)
            if any(kw and kw in notice_text for kw in firm.get("_exclude_keywords", [])):
                continue

            exact = sector in firm_sectors
            broad = not exact and any(
                s in firm_sectors for s in _related_sectors(sector)
            )
            if not (exact or broad):
                continue

        size = (firm.get("size") or "medium").lower()
        eligible = VALUE_IMPORTANCE_THRESHOLDS.get(value_band, set())
        importance = "high" if size in eligible and size in ("major", "large") else \
                     "medium" if size in eligible else "low"
        maturity = SIZE_MATURITY_MAP.get(size, "moderate")

        if specialist_flag:
            match_reason = f"[inferred] Specialist match ({specialist_flag}) — no MBIE win history found"
        else:
            match_reason = (
                f"[inferred] {('Direct' if exact else 'Related')} sector match on "
                f"{sector.replace('_',' ')} — no MBIE win history found"
            )
        bullets = [match_reason]
        if firm.get("notes"):
            bullets.append(f"[inferred] {firm['notes'][:80]}")

        rank_key = (
            0 if exact else 1,
            {"high": 0, "medium": 1, "low": 2}.get(importance, 2),
            {"major": 0, "large": 1, "medium": 2, "small": 3, "micro": 4}.get(size, 2),
        )
        # Prefer the canonical_name column already set in load_bidders(),
        # fall back to runtime lookup
        canon = firm.get("canonical_name") or canonical_name(firm["firm_name"])
        candidates.append({
            "firm_name":             firm["firm_name"],
            "canonical_name":        canon,
            "sector":                firm.get("sectors"),
            "size":                  firm.get("size"),
            "strategic_importance":  importance,
            "intelligence_maturity": maturity,
            "relevance_score":       0.05,  # low baseline — no evidence
            "match_type":            "csv_inferred",
            "reasoning":             bullets,
            "company_context":       None,
            "context_confidence":    "unknown",
            "_rank":                 rank_key,
        })

    candidates.sort(key=lambda x: x["_rank"])
    for c in candidates:
        del c["_rank"]

    # Deduplicate by canonical name
    candidates = deduplicate_bidders(candidates)
    for c in candidates:
        c["firm_name"] = c.get("canonical_name") or c["firm_name"]
    return candidates


def _related_sectors(sector: str) -> list[str]:
    groups = [
        {"FM", "infrastructure", "utilities"},
        {"security", "defence"},
        {"ICT", "advisory", "professional_services"},
        {"health", "advisory"},
    ]
    for group in groups:
        if sector in group:
            return list(group - {sector})
    return []


# ── Combined inference ────────────────────────────────────────────────────────

def score_bidders_for_notice(
    notice: dict,
    all_bidders: Optional[list[dict]] = None,
) -> list[dict]:
    """
    Return ranked list of credible bidders for a notice.

    Uses MBIE win history as primary source. Falls back to CSV-based
    inference for suppliers not represented in the historical data.
    Applies:
      - Hard sector exclusion matrix (before any MBIE result is accepted)
      - Title keyword exclusion (catches misclassified notices)
      - Canonical deduplication (collapses name variants per parent company)
      - Specialist firm filtering (for aerospace/cyber/legal/health notices,
        only firms with the matching specialist_flag are accepted from CSV;
        fewer than MIN_BIDDERS credible matches → show what was found, no padding)
    """
    specialist_flag = _notice_is_specialist(notice)
    min_bidders = getattr(config, "SPECIALIST_MIN_BIDDERS", 1)

    results: list[dict] = []
    mbie_canonical_names: set[str] = set()

    # Build canonical name set for the specialist sector (used to filter MBIE below)
    specialist_canonical_names: set[str] = set()
    if specialist_flag:
        if all_bidders is None:
            _tmp = load_bidders()
        else:
            _tmp = all_bidders
        for _b in _tmp:
            _flags = [f.strip() for f in _b.get("specialist_flags", "").split("|") if f.strip()]
            if specialist_flag in _flags:
                specialist_canonical_names.add(
                    (_b.get("canonical_name") or canonical_name(_b.get("firm_name", ""))).lower()
                )

    if _mbie_available():
        mbie_results = _mbie_bidders_for_notice(notice)

        # Apply hard exclusion to MBIE results (MBIE ignores sector context)
        filtered_mbie = []
        for r in mbie_results:
            # Reconstruct a pseudo-sector list from the match for exclusion check
            # We use the sector stored on the result row
            r_sectors = [s.strip() for s in (r.get("sector") or "").split("|") if s.strip()]
            if not r_sectors and r.get("firm_name"):
                # If no sector on MBIE row, check firm override before trusting MBIE
                if _firm_is_excluded([], notice, r.get("firm_name", "")):
                    logger.debug(
                        "Excluded MBIE result %s from notice %s (firm override)",
                        r.get("firm_name"), notice.get("notice_id"),
                    )
                else:
                    filtered_mbie.append(r)
            elif not _firm_is_excluded(r_sectors, notice, r.get("firm_name", "")):
                filtered_mbie.append(r)
            else:
                logger.debug(
                    "Excluded MBIE result %s from notice %s (sector exclusion matrix)",
                    r.get("firm_name"), notice.get("notice_id"),
                )
                continue

        # For specialist notices, further filter MBIE to firms we recognise as
        # actual specialists (by canonical name lookup against CSV specialist set).
        # This prevents generic ICT/advisory firms winning agency-match slots that
        # should go to actual cybersecurity / legal / aerospace / health firms.
        if specialist_flag and specialist_canonical_names:
            pre_len = len(filtered_mbie)
            filtered_mbie = [
                r for r in filtered_mbie
                if (r.get("canonical_name") or r["firm_name"]).lower() in specialist_canonical_names
            ]
            if pre_len != len(filtered_mbie):
                logger.debug(
                    "Specialist filter dropped %d non-specialist MBIE results for notice %s",
                    pre_len - len(filtered_mbie), notice.get("notice_id"),
                )

        results.extend(filtered_mbie)
        mbie_canonical_names = {r.get("canonical_name") or r["firm_name"] for r in filtered_mbie}
        logger.debug(
            "MBIE returned %d bidders (after exclusion %d) for notice %s",
            len(mbie_results), len(filtered_mbie), notice.get("notice_id", "?"),
        )
    else:
        logger.debug("MBIE data not available — using CSV fallback only")

    # CSV fallback: add firms not already returned by MBIE
    if all_bidders is None:
        all_bidders = load_bidders()

    csv_results = _csv_bidders_for_notice(notice, all_bidders, specialist_flag=specialist_flag)
    for r in csv_results:
        canon = r.get("canonical_name") or r["firm_name"]
        if canon not in mbie_canonical_names:
            results.append(r)

    # For specialist notices: if we have very few results, that's correct —
    # do NOT pad with general firms. Log so the operator knows.
    if specialist_flag and len(results) < min_bidders:
        logger.info(
            "Specialist notice %s (%s): only %d credible bidder(s) found — "
            "showing %d rather than padding with irrelevant firms",
            notice.get("notice_id"), specialist_flag, len(results), len(results),
        )

    return results


# ── Storage ───────────────────────────────────────────────────────────────────

def _store_bidders(notice_id: str, bidders: list[dict]) -> None:
    for b in bidders:
        reasoning_str = " | ".join(b.get("reasoning") or [])
        # Always store under the canonical name so ON CONFLICT deduplicates
        # across raw MBIE variants (e.g. "FULTON HOGAN LIMITED" / "Fulton Hogan
        # Canterbury" both collapse to "Fulton Hogan" before hitting the DB).
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
    Layer 2 hook — now implemented via MBIE data.
    Queries supplier_win_history for the named bidder's track record
    in the context of the given notice's sector and agency.

    Returns dict with:
      past_awards_count: int
      last_award_date: date | None
      avg_award_value: float | None
      win_rate_this_agency: float | None
      award_evidence: str  (1-sentence human-readable summary)
    """
    from historical_data import get_supplier_history, get_agency_win_count

    # Get notice context
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

    total = history.get("total_wins", 0)
    last_date = history.get("last_win_date")
    avg_val = history.get("avg_contract_value")

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
        "last_award_date": last_date,
        "avg_award_value": float(avg_val) if avg_val else None,
        "win_rate_this_agency": None,  # would need total bids to compute
        "award_evidence": summary,
    }


def enrich_from_web(bidder_name: str, sector: str) -> Optional[dict]:
    """
    Layer 2 hook — will be populated in Layer 2 Phase 2.
    Will scrape Companies Office (NZBN), company website, and LinkedIn
    for: registration status, directors, recent project references,
    certifications, and capability statement language.

    Returns None until implemented.

    When Layer 2 Phase 2 is built, this will consume:
      organisations(firm_name, nzbn, website_url, last_scraped_at)
      people(name, organisation_id, role)
    """
    return None


# ── Main entry point ──────────────────────────────────────────────────────────

def run_bidder_inference() -> int:
    """
    Run bidder inference for all high-priority notices not yet processed.
    Uses MBIE win history as primary source, CSV as fallback.
    """
    logger.info("Starting bidder pool inference")

    mbie_ready = _mbie_available()
    logger.info(
        "MBIE data: %s",
        "available — using evidence-based inference" if mbie_ready
        else "NOT available — using CSV keyword fallback only"
    )

    all_bidders = load_bidders()

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

    logger.info("%d notices require bidder inference", len(notices))
    count = 0

    for notice in notices:
        try:
            bidders = score_bidders_for_notice(notice, all_bidders)
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
