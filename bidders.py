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

logger = logging.getLogger(__name__)


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

    for r in combined[:30]:
        name = r.get("supplier_name") or r.get("business_name") or ""
        if not name:
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
            "sector": sector,
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
            row["_sectors"] = [s.strip() for s in row.get("sectors", "").split("|") if s.strip()]
            row["_exclude_keywords"] = [
                k.strip().lower()
                for k in row.get("exclude_keywords", "").split("|")
                if k.strip()
            ]
            bidders.append(row)
    logger.info("Loaded %d bidders from CSV (%s)", len(bidders), csv_path)
    return bidders


def _csv_bidders_for_notice(notice: dict, all_bidders: list[dict]) -> list[dict]:
    """CSV-based fallback matching. Returns results tagged as inferred."""
    sector = notice.get("sector_tag") or "other"
    value_band = notice.get("value_band") or "unknown"
    notice_text = (
        (notice.get("title") or "") + " " + (notice.get("description") or "")
    ).lower()

    candidates = []
    for firm in all_bidders:
        firm_sectors = firm.get("_sectors", [])
        # Exclusion check
        excluded = any(
            kw and kw in notice_text
            for kw in firm.get("_exclude_keywords", [])
        )
        if excluded:
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

        bullets = [
            f"[inferred] {('Direct' if exact else 'Related')} sector match on "
            f"{sector.replace('_',' ')} — no MBIE win history found"
        ]
        if firm.get("notes"):
            bullets.append(f"[inferred] {firm['notes'][:80]}")

        rank_key = (
            0 if exact else 1,
            {"high": 0, "medium": 1, "low": 2}.get(importance, 2),
            {"major": 0, "large": 1, "medium": 2, "small": 3, "micro": 4}.get(size, 2),
        )
        candidates.append({
            "firm_name":             firm["firm_name"],
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
    Deduplicates between sources (MBIE result takes priority over CSV).
    """
    results: list[dict] = []
    mbie_names: set[str] = set()

    if _mbie_available():
        mbie_results = _mbie_bidders_for_notice(notice)
        results.extend(mbie_results)
        mbie_names = {r["firm_name"] for r in mbie_results}
        logger.debug(
            "MBIE returned %d bidders for notice %s",
            len(mbie_results), notice.get("notice_id", "?"),
        )
    else:
        logger.debug("MBIE data not available — using CSV fallback only")

    # CSV fallback: add firms not already returned by MBIE
    if all_bidders is None:
        all_bidders = load_bidders()

    csv_results = _csv_bidders_for_notice(notice, all_bidders)
    for r in csv_results:
        if r["firm_name"] not in mbie_names:
            results.append(r)

    return results


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
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (notice_id, firm_name) DO UPDATE SET
                strategic_importance  = EXCLUDED.strategic_importance,
                intelligence_maturity = EXCLUDED.intelligence_maturity,
                relevance_score       = EXCLUDED.relevance_score,
                match_type            = EXCLUDED.match_type,
                reasoning             = EXCLUDED.reasoning
            """,
            (
                notice_id,
                b["firm_name"],
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
         WHERE s.composite_score >= %s
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
