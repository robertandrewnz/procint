"""
intel_library/layer3_integration.py — Layer 3 strategic context enrichment.

Provides build_strategic_context(notice_sector, agency_name) that:
  1. Queries v_sector_context for sector profile
  2. Queries v_active_signals, Budget 2026 signals first
  3. Returns a STRATEGIC_ENVIRONMENT block injected before MBIE data
  4. Records usage in intel_source_usage

This block is injected into Layer 3 Claude prompts (pursuit_package,
competitor_profile, watch_brief) to add live strategic intelligence context.

Usage:
    from intel_library.layer3_integration import build_strategic_context

    ctx = build_strategic_context("infrastructure", "Waka Kotahi NZTA")
    # Returns a formatted text block for injection into Claude prompts, e.g.:
    #
    # STRATEGIC ENVIRONMENT — Infrastructure
    # Policy framework: GPS-Transport 2024, NPS-Infrastructure 2025, ...
    # Investment pipeline: $185B [NZ Infrastructure Pipeline — Quarterly Snapshot]
    # Budget 2026: $400M state highway resilience [BUDGET 2026 — BEFU2026]
    # Key signals: ...
    # Macro context: ...
    # Competitive dynamics: ...
"""
from __future__ import annotations

import logging
from typing import Optional

import db

logger = logging.getLogger(__name__)

HIGH_PRIORITY_SHORT_NAMES = {"BEFU2026", "Budget2026-Full", "FSR2026"}

# Sector label prettifier
_SECTOR_LABELS = {
    "FM":                   "Facilities Management",
    "infrastructure":       "Infrastructure",
    "ICT":                  "ICT & Digital",
    "advisory":             "Advisory & Consulting",
    "health":               "Health",
    "security":             "Security",
    "defence":              "Defence",
    "utilities":            "Utilities & Energy",
    "professional_services": "Professional Services",
    "Construction":         "Construction",
    "Roading":              "Roading & Transport",
}


def _fmt_value(v: Optional[int]) -> Optional[str]:
    if v is None:
        return None
    if v >= 1_000_000_000:
        return f"${v / 1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.0f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:,}"


def _get_sector_profile(sector: str) -> Optional[dict]:
    """Fetch sector profile from intel_sector_profiles (case-insensitive)."""
    row = db.fetchone(
        "SELECT * FROM intel_sector_profiles WHERE LOWER(sector) = LOWER(%s)",
        (sector,),
    )
    return dict(row) if row else None


def _get_active_signals(sector: str, agency: Optional[str], limit: int = 8) -> list:
    """
    Fetch active signals relevant to this sector + agency.
    Budget 2026 signals come first.
    """
    try:
        signals = db.fetchall(
            """
            SELECT sig.id, sig.signal_type, sig.signal_title, sig.signal_body,
                   sig.dollar_value, sig.timeframe, sig.confidence,
                   sig.affected_sectors, sig.affected_agencies,
                   src.id AS source_id, src.short_name, src.title AS source_title,
                   src.nz_relevance_score,
                   CASE WHEN src.short_name = ANY(%s) THEN 0 ELSE 1 END AS priority_order
            FROM v_active_signals sig
            JOIN intel_sources src ON src.id = sig.source_id
            WHERE (
                sig.affected_sectors @> ARRAY[%s]::TEXT[]
                OR (
                    %s IS NOT NULL
                    AND EXISTS (
                        SELECT 1 FROM unnest(sig.affected_agencies) a
                        WHERE LOWER(a) LIKE %s
                    )
                )
            )
            ORDER BY priority_order ASC, src.nz_relevance_score DESC, sig.extracted_at DESC
            LIMIT %s
            """,
            (
                list(HIGH_PRIORITY_SHORT_NAMES),
                sector,
                agency,
                f"%{(agency or '')[:30].lower()}%" if agency else None,
                limit,
            ),
        )
        return signals
    except Exception as exc:
        logger.warning("_get_active_signals failed: %s", exc)
        return []


def _record_layer3_usage(
    source_ids: list,
    signal_ids: list,
    used_in: str,
    usage_type: str,
) -> None:
    """Record Layer 3 usage for each source."""
    unique_ids = list(dict.fromkeys(source_ids))[:8]
    for src_id in unique_ids:
        try:
            db.execute(
                """
                INSERT INTO intel_source_usage
                    (source_id, used_in, usage_type, significance_score, signal_ids, used_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                """,
                (src_id, used_in, usage_type, 7, [i for i in signal_ids if i]),
            )
        except Exception as exc:
            logger.debug("Layer 3 usage recording failed for source %s: %s", src_id, exc)


def build_strategic_context(
    notice_sector: str,
    agency_name: Optional[str] = None,
    used_in: str = "",
    usage_type: str = "pursuit_package",
    max_signals: int = 5,
) -> str:
    """
    Build a STRATEGIC ENVIRONMENT block for injection into Layer 3 Claude prompts.

    Args:
        notice_sector: Sector tag (e.g. 'infrastructure', 'ICT', 'FM').
        agency_name: Procuring agency name for signal filtering (optional).
        used_in: Identifier for usage tracking (e.g. notice_id or 'watch_brief').
        usage_type: One of the intel_source_usage usage_type values.
        max_signals: Maximum number of key signals to include.

    Returns:
        Formatted text block string. Empty string if no relevant data found.
    """
    sector_label = _SECTOR_LABELS.get(notice_sector, notice_sector.replace("_", " ").title())

    # Fetch sector profile
    profile = _get_sector_profile(notice_sector)

    # Fetch active signals
    signals = _get_active_signals(notice_sector, agency_name, limit=max_signals + 3)

    if not profile and not signals:
        return ""

    lines = [f"STRATEGIC ENVIRONMENT — {sector_label}"]

    # Policy framework
    if profile and profile.get("policy_drivers"):
        drivers = profile["policy_drivers"]
        lines.append(f"Policy framework: {', '.join(drivers[:4])}")

    # Investment pipeline
    if profile:
        pipeline = _fmt_value(profile.get("pipeline_value"))
        spend = _fmt_value(profile.get("government_spend_annual"))
        pipeline_src = None
        # Find the infrastructure pipeline source if relevant
        src_row = db.fetchone(
            "SELECT title, short_name FROM intel_sources WHERE short_name = 'InfraPipeline' AND is_active = TRUE"
        )
        if src_row:
            pipeline_src = src_row.get("short_name") or src_row.get("title", "")[:40]
        parts = []
        if pipeline and pipeline_src:
            parts.append(f"{pipeline} [{pipeline_src}]")
        elif pipeline:
            parts.append(pipeline)
        if spend:
            parts.append(f"annual government spend ~{spend}")
        if parts:
            lines.append(f"Investment pipeline: {' | '.join(parts)}")

    # Budget 2026 signals (prominently labelled)
    budget_signals = [
        s for s in signals
        if (s.get("short_name") or "") in HIGH_PRIORITY_SHORT_NAMES
    ]
    if budget_signals:
        budget_lines = []
        for sig in budget_signals[:3]:
            dv = _fmt_value(sig.get("dollar_value"))
            label = sig.get("signal_title", "")
            body = sig.get("signal_body", "")
            src = sig.get("short_name") or sig.get("source_title", "")[:30]
            parts = [label]
            if dv:
                parts.append(f"({dv})")
            if body:
                parts.append(f"— {body[:120]}")
            budget_lines.append(f"  • {'  '.join(parts)} [{src}]")
        lines.append("Budget 2026:\n" + "\n".join(budget_lines))

    # Key signals (non-Budget)
    other_signals = [
        s for s in signals
        if (s.get("short_name") or "") not in HIGH_PRIORITY_SHORT_NAMES
    ][:max_signals]
    if other_signals:
        sig_lines = []
        for sig in other_signals:
            dv = _fmt_value(sig.get("dollar_value"))
            title = sig.get("signal_title", "")
            src = sig.get("short_name") or sig.get("source_title", "")[:30]
            tf = sig.get("timeframe", "")
            parts = [title]
            if dv:
                parts.append(f"({dv})")
            if tf:
                parts.append(f"[{tf}]")
            parts.append(f"[{src}]")
            sig_lines.append(f"  • {' '.join(parts)}")
        lines.append("Key signals:\n" + "\n".join(sig_lines))

    # Macro context from BEFU if relevant (oil/economic)
    befu_signals = [
        s for s in signals
        if s.get("short_name") == "BEFU2026"
        and any(kw in (s.get("signal_body") or "").lower()
                for kw in ["oil", "fuel", "inflation", "gdp", "unemployment", "hormuz"])
    ]
    if befu_signals:
        macro_bodies = [s.get("signal_body", "")[:150] for s in befu_signals[:2]]
        lines.append("Macro context: " + " | ".join(macro_bodies))

    # Competitive dynamics
    if profile:
        competitive_parts = []
        if profile.get("dominant_suppliers"):
            suppliers = ", ".join(profile["dominant_suppliers"][:5])
            competitive_parts.append(f"Dominant suppliers: {suppliers}")
        if profile.get("risk_factors"):
            risks = "; ".join(profile["risk_factors"][:3])
            competitive_parts.append(f"Key risks: {risks}")
        if profile.get("opportunity_factors"):
            opps = "; ".join(profile["opportunity_factors"][:2])
            competitive_parts.append(f"Opportunities: {opps}")
        if competitive_parts:
            lines.append("Competitive dynamics: " + " | ".join(competitive_parts))

    if len(lines) <= 1:
        return ""

    # Record usage
    if used_in:
        source_ids = [s["source_id"] for s in signals if s.get("source_id")]
        signal_ids = [s["id"] for s in signals if s.get("id")]
        _record_layer3_usage(source_ids, signal_ids, used_in, usage_type)

    return "\n".join(lines)


def get_sector_context_dict(notice_sector: str, agency_name: Optional[str] = None) -> dict:
    """
    Return sector context as a structured dict (for programmatic use).

    Keys: sector, profile (dict or None), signals (list), budget_signals (list).
    """
    profile = _get_sector_profile(notice_sector)
    signals = _get_active_signals(notice_sector, agency_name, limit=10)
    budget_signals = [
        s for s in signals
        if (s.get("short_name") or "") in HIGH_PRIORITY_SHORT_NAMES
    ]
    return {
        "sector": notice_sector,
        "profile": profile,
        "signals": signals,
        "budget_signals": budget_signals,
    }
