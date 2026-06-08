"""
Strategic significance scoring module.

Reads parsed_notices, produces scored_notices rows with a composite 1–10 score.
"""
import logging
from typing import Optional

import config
import db
from intel_library.scoring_integration import get_strategic_score_boost, apply_boost_to_composite

logger = logging.getLogger(__name__)


# ── Individual dimension scorers ──────────────────────────────────────────────

def score_value(value_band: str) -> float:
    return config.VALUE_SCORE_MAP.get(value_band, config.VALUE_SCORE_MAP["unknown"])


def score_sector(sector_tag: str) -> float:
    """Pipeline-time sector score (stored to DB). Uses hardcoded SECTOR_PRIORITY."""
    return config.SECTOR_PRIORITY.get(sector_tag, config.SECTOR_PRIORITY["other"])


def client_sector_score(sector_tag: str, preferred_sectors: Optional[list[str]]) -> float:
    """
    Render-time sector score driven by client preference.

    - No preference (None / []): all sectors score equally → sector-neutral ranking.
    - Preference provided: preferred sectors score 1.0, all others score 0.2.
      This surfaces the client's sectors at the top regardless of stored pipeline scores.
    """
    if not preferred_sectors:
        return config.SECTOR_SCORE_NEUTRAL
    return (
        config.SECTOR_SCORE_PREFERRED
        if sector_tag in preferred_sectors
        else config.SECTOR_SCORE_OTHER
    )


def compute_composite_for_client(
    score_value: float,
    score_complexity: float,
    score_urgency: float,
    sector_tag: str,
    preferred_sectors: Optional[list[str]],
) -> float:
    """
    Recalculate a composite score for a given client preference at render time.
    Uses stored value/complexity/urgency scores but substitutes a client-aware
    sector score so preferred sectors rise in the ranking.
    """
    s_sector = client_sector_score(sector_tag, preferred_sectors)
    return compute_composite(score_value, s_sector, score_complexity, score_urgency)


def score_complexity(evaluation_criteria: Optional[str], description: Optional[str]) -> float:
    text = " ".join(filter(None, [evaluation_criteria, description])).lower()
    hits = sum(1 for phrase in config.COMPLEXITY_PHRASES if phrase.lower() in text)
    # Normalise: 0 hits → 0.2, 1 hit → 0.5, 2+ hits → 0.75+, cap at 1.0
    if hits == 0:
        return 0.2
    if hits == 1:
        return 0.5
    return min(0.75 + (hits - 2) * 0.1, 1.0)


def score_urgency(days_until_close: Optional[int]) -> float:
    if days_until_close is None:
        return config.URGENCY_DEFAULT
    for threshold, score in config.URGENCY_THRESHOLDS:
        if days_until_close <= threshold:
            return score
    return config.URGENCY_DEFAULT


# ── Composite scorer ──────────────────────────────────────────────────────────

def compute_composite(
    s_value: float,
    s_sector: float,
    s_complexity: float,
    s_urgency: float,
) -> float:
    w = config.SCORE_WEIGHTS
    total_weight = sum(w.values())
    weighted = (
        s_value     * w["value"]
        + s_sector    * w["sector"]
        + s_complexity * w["complexity"]
        + s_urgency   * w["urgency"]
    )
    # Scale to 1–10
    return round((weighted / total_weight) * 10, 2)


def build_reasoning(
    sector_tag: str,
    value_band: str,
    days_until_close: Optional[int],
    s_value: float,
    s_sector: float,
    s_complexity: float,
    s_urgency: float,
    composite: float,
) -> str:
    dtc_str = f"{days_until_close}d" if days_until_close is not None else "unknown"
    return (
        f"Composite {composite}/10 — "
        f"sector={sector_tag} (sector_score={s_sector:.2f}), "
        f"value_band={value_band} (value_score={s_value:.2f}), "
        f"complexity_score={s_complexity:.2f}, "
        f"urgency={dtc_str} (urgency_score={s_urgency:.2f})"
    )


# ── Storage ───────────────────────────────────────────────────────────────────

def _store_score(scored: dict) -> None:
    db.execute(
        """
        INSERT INTO scored_notices
            (notice_id, score_value, score_sector, score_complexity,
             score_urgency, composite_score, score_reasoning)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (notice_id) DO UPDATE SET
            score_value      = EXCLUDED.score_value,
            score_sector     = EXCLUDED.score_sector,
            score_complexity = EXCLUDED.score_complexity,
            score_urgency    = EXCLUDED.score_urgency,
            composite_score  = EXCLUDED.composite_score,
            score_reasoning  = EXCLUDED.score_reasoning,
            scored_at        = NOW()
        """,
        (
            scored["notice_id"],
            scored["score_value"],
            scored["score_sector"],
            scored["score_complexity"],
            scored["score_urgency"],
            scored["composite_score"],
            scored["score_reasoning"],
        ),
    )


# ── Main entry point ──────────────────────────────────────────────────────────

def run_scoring_boost_only() -> int:
    """
    Apply the intel library strategic boost to all already-scored notices.
    Called after intel library ingestion to update composite scores without
    re-running the full scoring pipeline.
    """
    logger.info("Applying strategic boost to existing scored notices")

    rows = db.fetchall(
        """
        SELECT s.notice_id, s.composite_score, s.score_reasoning,
               p.sector_tag, r.agency
        FROM   scored_notices s
        JOIN   parsed_notices p ON p.notice_id = s.notice_id
        JOIN   raw_notices    r ON r.notice_id = s.notice_id
        """
    )
    logger.info("%d notices to re-boost", len(rows))
    count = 0

    for row in rows:
        try:
            notice_for_boost = {
                "notice_id":  row.get("notice_id", ""),
                "sector_tag": row.get("sector_tag") or "other",
                "agency":     row.get("agency") or "",
            }
            boost = get_strategic_score_boost(notice_for_boost, record_usage=False)
            if boost.get("modifier", 0.0) == 0.0:
                continue

            original = float(row["composite_score"] or 5.0)
            boosted  = apply_boost_to_composite(original, boost)

            reasoning = (row.get("score_reasoning") or "")
            # Strip any previous boost annotation before appending new one
            if " | strategic_boost=" in reasoning:
                reasoning = reasoning.split(" | strategic_boost=")[0]
            reasoning += (
                f" | strategic_boost={boost['modifier']:+.3f} "
                f"({boost['signal_count']} intel signals, "
                f"sources: {', '.join(boost['source_names'][:3])})"
            )

            db.execute(
                """
                UPDATE scored_notices
                   SET composite_score  = %s,
                       score_reasoning  = %s,
                       scored_at        = NOW()
                 WHERE notice_id = %s
                """,
                (boosted, reasoning, row["notice_id"]),
            )
            count += 1
        except Exception as exc:
            logger.warning("Boost failed for notice %s: %s", row.get("notice_id"), exc)

    logger.info("Strategic boost applied to %d notices", count)
    return count


def run_scoring() -> int:
    logger.info("Starting strategic scoring")

    rows = db.fetchall(
        """
        SELECT p.notice_id, p.sector_tag, p.value_band, p.days_until_close,
               p.evaluation_criteria, r.description, r.agency
        FROM   parsed_notices p
        JOIN   raw_notices r ON r.notice_id = p.notice_id
        LEFT JOIN scored_notices s ON s.notice_id = p.notice_id
        WHERE  s.notice_id IS NULL
        """
    )

    logger.info("%d parsed notices to score", len(rows))
    count = 0

    for row in rows:
        try:
            s_value      = score_value(row.get("value_band") or "unknown")
            s_sector     = score_sector(row.get("sector_tag") or "other")
            s_complexity = score_complexity(
                row.get("evaluation_criteria"), row.get("description")
            )
            s_urgency    = score_urgency(row.get("days_until_close"))
            composite    = compute_composite(s_value, s_sector, s_complexity, s_urgency)

            # Apply intel library strategic boost
            notice_for_boost = {
                "notice_id":  row.get("notice_id", ""),
                "sector_tag": row.get("sector_tag") or "other",
                "agency":     row.get("agency") or "",
            }
            boost = get_strategic_score_boost(notice_for_boost)
            if boost.get("modifier", 0.0) != 0.0:
                composite = apply_boost_to_composite(composite, boost)

            reasoning    = build_reasoning(
                row.get("sector_tag") or "other",
                row.get("value_band") or "unknown",
                row.get("days_until_close"),
                s_value, s_sector, s_complexity, s_urgency, composite,
            )
            if boost.get("signal_count", 0) > 0:
                reasoning += (
                    f" | strategic_boost={boost['modifier']:+.3f} "
                    f"({boost['signal_count']} intel signals, "
                    f"sources: {', '.join(boost['source_names'][:3])})"
                )

            _store_score(
                {
                    "notice_id":       row["notice_id"],
                    "score_value":     s_value,
                    "score_sector":    s_sector,
                    "score_complexity": s_complexity,
                    "score_urgency":   s_urgency,
                    "composite_score": composite,
                    "score_reasoning": reasoning,
                }
            )
            count += 1
            logger.debug("Scored %s → %s", row["notice_id"], composite)
        except Exception as exc:
            logger.warning("Failed to score notice %s: %s", row.get("notice_id"), exc)

    logger.info("Scoring complete: %d notices scored", count)
    return count
