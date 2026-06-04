"""
Bidder pool inference module.

Reads the seeded bidders.csv and, for each high-priority notice, selects
likely bidders by sector match, scores their strategic interest and inferred
intelligence maturity, and writes results to bidder_pool.
"""
import csv
import logging
from pathlib import Path

import config
import db

logger = logging.getLogger(__name__)

# ── Load bidder reference list ────────────────────────────────────────────────

def load_bidders(csv_path: str = config.BIDDER_CSV_PATH) -> list[dict]:
    """
    Expected CSV columns:
      firm_name, sectors, size, headquarters, notes

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


# ── Matching & scoring ────────────────────────────────────────────────────────

SIZE_MATURITY_MAP = {
    "micro":  "weak",
    "small":  "weak",
    "medium": "moderate",
    "large":  "strong",
    "major":  "strong",
}

VALUE_IMPORTANCE_THRESHOLDS = {
    # value_band → minimum size for "high" strategic importance
    "10m_plus":   {"major", "large"},
    "2m_10m":     {"major", "large", "medium"},
    "500k_2m":    {"major", "large", "medium", "small"},
    "100k_500k":  {"major", "large", "medium", "small", "micro"},
    "under_100k": {"major", "large", "medium", "small", "micro"},
    "unknown":    {"major", "large", "medium", "small", "micro"},
}


def infer_strategic_importance(firm: dict, value_band: str, sector_match: bool) -> str:
    size = (firm.get("size") or "medium").lower()
    if not sector_match:
        return "low"
    eligible_sizes = VALUE_IMPORTANCE_THRESHOLDS.get(value_band, set())
    if size in eligible_sizes and size in ("major", "large"):
        return "high"
    if size in eligible_sizes:
        return "medium"
    return "low"


def infer_intelligence_maturity(firm: dict) -> str:
    size = (firm.get("size") or "medium").lower()
    return SIZE_MATURITY_MAP.get(size, "moderate")


def score_bidders_for_notice(
    notice: dict, all_bidders: list[dict]
) -> list[dict]:
    """Return ranked list of likely bidders for a notice."""
    sector = notice.get("sector_tag") or "other"
    value_band = notice.get("value_band") or "unknown"

    candidates = []
    for firm in all_bidders:
        firm_sectors = firm.get("_sectors", [])
        # Primary match: exact sector
        exact = sector in firm_sectors
        # Broad match: both are in the same macro-group
        broad = (
            not exact
            and any(
                s in firm_sectors
                for s in _related_sectors(sector)
            )
        )
        if not (exact or broad):
            continue

        importance = infer_strategic_importance(firm, value_band, exact)
        maturity = infer_intelligence_maturity(firm)

        # Rank: exact > broad, then high > medium importance, then large > small
        rank_key = (
            0 if exact else 1,
            {"high": 0, "medium": 1, "low": 2}.get(importance, 2),
            {"major": 0, "large": 1, "medium": 2, "small": 3, "micro": 4}.get(
                firm.get("size", "medium").lower(), 2
            ),
        )
        candidates.append(
            {
                "firm_name": firm["firm_name"],
                "sector": firm.get("sectors"),
                "size": firm.get("size"),
                "strategic_importance": importance,
                "intelligence_maturity": maturity,
                "_rank": rank_key,
            }
        )

    candidates.sort(key=lambda x: x["_rank"])
    for c in candidates:
        del c["_rank"]
    return candidates


def _related_sectors(sector: str) -> list[str]:
    """Macro-groupings for broad matching."""
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


# ── Storage ───────────────────────────────────────────────────────────────────

def _store_bidders(notice_id: str, bidders: list[dict]) -> None:
    for b in bidders:
        db.execute(
            """
            INSERT INTO bidder_pool
                (notice_id, firm_name, sector, size,
                 strategic_importance, intelligence_maturity)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (notice_id, firm_name) DO UPDATE SET
                strategic_importance  = EXCLUDED.strategic_importance,
                intelligence_maturity = EXCLUDED.intelligence_maturity
            """,
            (
                notice_id,
                b["firm_name"],
                b.get("sector"),
                b.get("size"),
                b["strategic_importance"],
                b["intelligence_maturity"],
            ),
        )


# ── Main entry point ──────────────────────────────────────────────────────────

def run_bidder_inference() -> int:
    logger.info("Starting bidder pool inference")
    all_bidders = load_bidders()

    if not all_bidders:
        logger.warning("No bidder data — skipping bidder inference")
        return 0

    notices = db.fetchall(
        """
        SELECT s.notice_id, p.sector_tag, p.value_band, p.geographic_scope
        FROM   scored_notices s
        JOIN   parsed_notices p ON p.notice_id = s.notice_id
        WHERE  s.composite_score >= %s
          AND  s.notice_id NOT IN (SELECT DISTINCT notice_id FROM bidder_pool)
        ORDER  BY s.composite_score DESC
        """,
        (config.PRIORITY_THRESHOLD,),
    )

    logger.info("%d high-priority notices require bidder inference", len(notices))
    count = 0

    for notice in notices:
        bidders = score_bidders_for_notice(notice, all_bidders)
        if bidders:
            _store_bidders(notice["notice_id"], bidders)
            logger.debug(
                "Stored %d bidders for notice %s", len(bidders), notice["notice_id"]
            )
            count += 1
        else:
            logger.debug("No matching bidders for notice %s", notice["notice_id"])

    logger.info("Bidder inference complete: %d notices processed", count)
    return count
