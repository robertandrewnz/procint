"""
intel_library/scoring_integration.py — Layer 1 strategic score boost.

Provides get_strategic_score_boost(notice) which:
  1. Queries v_sector_context for the notice's sector
  2. Queries v_active_signals filtered to that sector + issuing agency
  3. Applies 1.5x weight to Budget 2026 / BEFU2026 signals
  4. Returns a modifier, signal labels, and source names
  5. Records use in intel_source_usage with usage_type 'scoring_boost'

This module is additive — it adds a new optional field 'strategic_boost'
to notice output. No existing scoring logic is changed.

Usage:
    from intel_library.scoring_integration import get_strategic_score_boost

    boost = get_strategic_score_boost(notice_dict)
    # boost = {
    #   "modifier": 0.4,          # float -1.0 to +2.0
    #   "signal_labels": [...],   # short signal titles
    #   "source_names": [...],    # source short names / titles
    #   "confidence": "high",
    # }
"""
from __future__ import annotations

import logging
from typing import Optional

import db

logger = logging.getLogger(__name__)

# Source short names whose signals receive 1.5x weight
HIGH_PRIORITY_SHORT_NAMES = {"BEFU2026", "Budget2026-Full", "FSR2026"}

# Signal type → base modifier contribution
SIGNAL_TYPE_MODIFIERS = {
    "budget_increase": 0.4,
    "new_initiative":  0.35,
    "opportunity":     0.30,
    "policy_change":   0.20,
    "risk":            -0.25,
}

# Confidence multiplier
CONFIDENCE_MULTIPLIER = {"high": 1.0, "medium": 0.7, "low": 0.4}

# Cap on total modifier
MODIFIER_MIN = -1.0
MODIFIER_MAX = 2.0


def get_strategic_score_boost(
    notice: dict,
    record_usage: bool = True,
) -> dict:
    """
    Calculate a strategic score modifier for a notice based on intel signals.

    Args:
        notice: Dict with at minimum 'notice_id', 'sector_tag', 'agency' fields.
        record_usage: Whether to log usage to intel_source_usage.

    Returns:
        Dict with keys: modifier (float), signal_labels (list), source_names (list),
        confidence (str), signal_count (int).
    """
    result = {
        "modifier": 0.0,
        "signal_labels": [],
        "source_names": [],
        "confidence": "low",
        "signal_count": 0,
    }

    sector = (notice.get("sector_tag") or notice.get("sector") or "").lower()
    agency = (notice.get("agency") or "").lower()
    notice_id = notice.get("notice_id", "")

    if not sector:
        return result

    try:
        # Fetch active signals relevant to this sector or agency
        signals = db.fetchall(
            """
            SELECT sig.id, sig.signal_type, sig.signal_title, sig.confidence,
                   sig.affected_sectors, sig.affected_agencies,
                   sig.dollar_value, sig.timeframe,
                   src.id AS source_id, src.short_name, src.title AS source_title
            FROM v_active_signals sig
            JOIN intel_sources src ON src.id = sig.source_id
            WHERE (
                sig.affected_sectors @> ARRAY[%s]::TEXT[]
                OR EXISTS (
                    SELECT 1 FROM unnest(sig.affected_agencies) a
                    WHERE LOWER(a) LIKE %s
                )
            )
            ORDER BY sig.extracted_at DESC
            LIMIT 20
            """,
            (sector, f"%{agency[:30]}%" if agency else "%"),
        )

        if not signals:
            return result

        total_modifier = 0.0
        source_ids_used = []
        signal_ids_used = []

        for sig in signals:
            base = SIGNAL_TYPE_MODIFIERS.get(sig.get("signal_type", ""), 0.0)
            conf = CONFIDENCE_MULTIPLIER.get(sig.get("confidence", "medium"), 0.7)
            weight = base * conf

            # 1.5x for Budget 2026 / BEFU sources
            short_name = sig.get("short_name") or ""
            if short_name in HIGH_PRIORITY_SHORT_NAMES:
                weight *= 1.5

            total_modifier += weight
            result["signal_labels"].append(sig.get("signal_title", "")[:80])
            src_label = short_name or sig.get("source_title", "")[:40]
            if src_label not in result["source_names"]:
                result["source_names"].append(src_label)

            if sig.get("id"):
                signal_ids_used.append(sig["id"])
            if sig.get("source_id"):
                source_ids_used.append(sig["source_id"])

        # Cap modifier
        total_modifier = max(MODIFIER_MIN, min(MODIFIER_MAX, total_modifier))
        result["modifier"] = round(total_modifier, 3)
        result["signal_count"] = len(signals)

        # Determine overall confidence
        confidences = [s.get("confidence", "medium") for s in signals]
        if "high" in confidences:
            result["confidence"] = "high"
        elif "medium" in confidences:
            result["confidence"] = "medium"
        else:
            result["confidence"] = "low"

        # Record usage
        if record_usage and source_ids_used:
            unique_source_ids = list(dict.fromkeys(source_ids_used))  # preserve order, dedupe
            for src_id in unique_source_ids[:5]:  # cap to 5 sources logged per notice
                try:
                    sig_score = min(10, max(1, int(abs(total_modifier) * 5) + 3))
                    db.execute(
                        """
                        INSERT INTO intel_source_usage
                            (source_id, used_in, usage_type, significance_score, signal_ids, used_at)
                        VALUES (%s, %s, 'scoring_boost', %s, %s, NOW())
                        """,
                        (
                            src_id,
                            f"notice:{notice_id}",
                            sig_score,
                            [i for i in signal_ids_used if i],
                        ),
                    )
                except Exception as exc:
                    logger.debug("Usage recording failed for source %s: %s", src_id, exc)

    except Exception as exc:
        logger.warning("get_strategic_score_boost failed for notice %s: %s", notice_id, exc)

    return result


def apply_boost_to_composite(composite_score: float, boost: dict) -> float:
    """
    Apply a strategic boost modifier to an existing composite score.

    The boost is additive but the result is capped at 10.0.
    A negative modifier (risk signal) can reduce the score but not below 1.0.
    """
    modifier = boost.get("modifier", 0.0)
    boosted = composite_score + modifier
    return round(max(1.0, min(10.0, boosted)), 2)
