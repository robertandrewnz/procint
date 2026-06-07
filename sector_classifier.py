"""
sector_classifier.py — Sector conflict detection and auto-correction.

resolve_sector_conflict() compares a notice's stored sector tag against
what the keyword classifier would assign from the title + description.

Confidence levels:
  high   (3+ keyword matches) → auto-correct stored tag, log reclassification
  medium (1–2 matches, differs) → flag notice with 'sector_unverified', no update
  low / agreement → leave unchanged, return original sector

The function is called:
  • At parse time in run_parsing() so every new notice is checked.
  • In run_reclassify_all() for a one-off retrospective pass over existing notices.
  • In generate_demo_content.py before selecting demo notices.
"""
from __future__ import annotations

import logging
from typing import Optional

import config
import db

logger = logging.getLogger(__name__)


# ── Keyword counting ──────────────────────────────────────────────────────────

def _count_matches(text: str, sector: str) -> int:
    """Count how many keywords for *sector* appear in *text* (case-insensitive)."""
    kws = config.SECTOR_KEYWORDS.get(sector, [])
    t = text.lower()
    return sum(1 for kw in kws if kw.lower() in t)


def _classify_with_confidence(title: str, description: str) -> tuple[str, int]:
    """
    Run the keyword classifier and return (best_sector, match_count).

    Returns "other" with count 0 if no sector matches.
    """
    text = " ".join(filter(None, [title, description]))
    best_sector = "other"
    best_count = 0
    for sector in config.SECTOR_KEYWORDS:
        count = _count_matches(text, sector)
        if count > best_count:
            best_count = count
            best_sector = sector
    return best_sector, best_count


# ── Main resolution function ──────────────────────────────────────────────────

def resolve_sector_conflict(
    notice_title: str,
    notice_description: str,
    stored_sector: str,
    notice_id: Optional[str] = None,
    mbie_category: Optional[str] = None,
) -> dict:
    """
    Compare the stored sector tag against what the keyword classifier assigns.

    Args:
        notice_title:       Title string from raw_notices.
        notice_description: Description string from raw_notices (may be None/empty).
        stored_sector:      Current sector_tag in parsed_notices.
        notice_id:          DB notice ID — used for logging and DB updates.
        mbie_category:      Optional MBIE category text to include in classification.

    Returns a dict:
        {
          "sector":           str — the resolved sector to use (may be unchanged),
          "original_sector":  str — the value before any change,
          "action":           "corrected" | "flagged" | "unchanged",
          "confidence":       "high" | "medium" | "low",
          "match_count":      int,
          "note":             str — human-readable summary for pursuit package display,
        }
    """
    combined_text = " ".join(filter(None, [
        notice_title, notice_description, mbie_category or ""
    ]))

    classified_sector, match_count = _classify_with_confidence(
        notice_title, notice_description
    )

    result = {
        "sector":          stored_sector,
        "original_sector": stored_sector,
        "action":          "unchanged",
        "confidence":      "low",
        "match_count":     match_count,
        "note":            "",
    }

    # Map confidence level
    if match_count >= 3:
        result["confidence"] = "high"
    elif match_count >= 1:
        result["confidence"] = "medium"
    else:
        result["confidence"] = "low"

    # Nothing to do if classifiers agree or stored_sector is already "other" with no signal
    if classified_sector == stored_sector:
        return result

    if result["confidence"] == "high":
        # Auto-correct
        result["sector"] = classified_sector
        result["action"] = "corrected"
        result["note"] = (
            f"Notice was originally tagged '{stored_sector}' — reclassified to "
            f"'{classified_sector}' based on content analysis ({match_count} keyword matches). "
            f"Competitive landscape uses '{classified_sector}' dataset."
        )
        logger.info(
            "Sector auto-corrected: notice=%s  %s → %s  (matches=%d)",
            notice_id or "?", stored_sector, classified_sector, match_count,
        )
        if notice_id:
            try:
                db.execute(
                    """
                    UPDATE parsed_notices
                       SET sector_tag  = %s,
                           parsed_at   = NOW()
                     WHERE notice_id = %s
                    """,
                    (classified_sector, notice_id),
                )
            except Exception as exc:
                logger.warning("DB sector update failed for %s: %s", notice_id, exc)

    elif result["confidence"] == "medium":
        # Flag only, no change
        result["action"] = "flagged"
        result["note"] = (
            f"⚠ Sector unverified — stored as '{stored_sector}' but "
            f"content suggests '{classified_sector}' ({match_count} keyword match"
            f"{'es' if match_count != 1 else ''}). Manual review recommended."
        )
        logger.debug(
            "Sector flagged (medium confidence): notice=%s  stored=%s  classified=%s  matches=%d",
            notice_id or "?", stored_sector, classified_sector, match_count,
        )

    return result


# ── Retrospective reclassification ────────────────────────────────────────────

def run_reclassify_all() -> dict:
    """
    Pass every existing parsed_notice through resolve_sector_conflict.
    Returns summary counts: {"corrected": n, "flagged": n, "unchanged": n}.
    """
    rows = db.fetchall(
        """
        SELECT p.notice_id, p.sector_tag, r.title, r.description
          FROM parsed_notices p
          JOIN raw_notices r ON r.notice_id = p.notice_id
        """
    )
    logger.info("Retrospective reclassification: %d notices to process", len(rows))

    counts: dict[str, int] = {"corrected": 0, "flagged": 0, "unchanged": 0}
    for row in rows:
        try:
            res = resolve_sector_conflict(
                notice_title=row.get("title") or "",
                notice_description=row.get("description") or "",
                stored_sector=row.get("sector_tag") or "other",
                notice_id=row["notice_id"],
            )
            counts[res["action"]] += 1
        except Exception as exc:
            logger.warning("Reclassify failed for %s: %s", row.get("notice_id"), exc)

    logger.info(
        "Reclassification complete — corrected=%d flagged=%d unchanged=%d",
        counts["corrected"], counts["flagged"], counts["unchanged"],
    )
    return counts


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )
    result = run_reclassify_all()
    print(result)
