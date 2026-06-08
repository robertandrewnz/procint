"""
market_intelligence.py — Claude-powered, user-specific market signals.

Replaces the count-based pattern_flags signals with 3 tailored intelligence
signals per user, generated daily via the pipeline cron.

Usage (from pipeline or cron):
    from market_intelligence import generate_market_intelligence
    signals = generate_market_intelligence("robert")

Usage (from portal, reading stored signals):
    from market_intelligence import get_stored_signals
    signals = get_stored_signals("robert")
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Optional

import anthropic

import config
import db

logger = logging.getLogger(__name__)

_client: Optional[anthropic.Anthropic] = None


def _get_claude() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


# ── Context builders ──────────────────────────────────────────────────────────

def _recent_notices_summary(sectors: list[str], n: int = 15) -> list[dict]:
    """Pull recent high-score notices for the user's sectors."""
    try:
        sector_filter = ""
        params: list = []
        if sectors:
            placeholders = ",".join(["%s"] * len(sectors))
            sector_filter = f"AND p.sector_tag IN ({placeholders})"
            params = list(sectors)

        rows = db.fetchall(
            f"""
            SELECT r.title, r.agency, p.sector_tag,
                   s.composite_score, p.days_until_close
              FROM scored_notices s
              JOIN raw_notices r    ON r.notice_id = s.notice_id
              JOIN parsed_notices p ON p.notice_id = s.notice_id
             WHERE s.composite_score >= %s
               {sector_filter}
             ORDER BY s.composite_score DESC
             LIMIT %s
            """,
            (config.PRIORITY_THRESHOLD,) + tuple(params) + (n,),
        )
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("_recent_notices_summary: %s", exc)
        return []


def _recent_awards_summary(sectors: list[str], n: int = 10) -> list[dict]:
    """Pull recent MBIE awards for the user's sectors."""
    try:
        two_years_ago = (date.today() - timedelta(days=730)).isoformat()
        sector_filter = ""
        params: list = [two_years_ago]
        if sectors:
            placeholders = ",".join(["%s"] * len(sectors))
            sector_filter = f"AND c.sector_tag IN ({placeholders})"
            params += sectors

        rows = db.fetchall(
            f"""
            SELECT n.title, n.posting_agency, n.awarded_amount,
                   n.awarded_date, c.sector_tag
              FROM mbie_award_notices n
              JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
              JOIN mbie_award_suppliers  s ON s.rfx_id = n.rfx_id
             WHERE n.awarded_date >= %s
               AND n.is_awarded IS TRUE
               {sector_filter}
             ORDER BY n.awarded_amount DESC NULLS LAST
             LIMIT %s
            """,
            tuple(params) + (n,),
        )
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("_recent_awards_summary: %s", exc)
        return []


def _renewal_summary(sectors: list[str], n: int = 5) -> list[dict]:
    """Pull upcoming renewals for the user's sectors."""
    try:
        from renewal_radar import get_renewal_radar
        radar = get_renewal_radar(user_sectors=sectors or None, days_ahead=120)
        return radar[:n]
    except Exception as exc:
        logger.warning("_renewal_summary: %s", exc)
        return []


# ── Claude call ───────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are a senior procurement intelligence analyst specialising in New Zealand "
    "government contracting. You generate concise, commercially actionable market "
    "signals for firms competing in the NZ public sector. Your signals are specific, "
    "evidence-backed, and focused on what the firm should DO next.\n\n"
    "STRICT RULES — violating any of these invalidates the response:\n"
    "1. Only reference verifiable data: active GETS notices in the data provided, "
    "confirmed MBIE award records, or contract renewals from the renewal pipeline data. "
    "Do NOT infer or speculate about renewals based on award age alone.\n"
    "2. Never use 'no re-award detected', 'estimated renewal window', or any proxy "
    "reasoning that infers a contract is up for renewal without an explicit contract "
    "expiry date or renewal pipeline entry in the data provided.\n"
    "3. If renewal pipeline data is empty or sparse, focus signals on active notice "
    "patterns and award history — do not fabricate renewal intelligence.\n"
    "4. Each signal must cite a specific agency, notice title, dollar value, or "
    "sector pattern from the data. Generic observations are not acceptable."
)

_SIGNAL_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "signal":   {"type": "string"},
            "priority": {"type": "string", "enum": ["high", "medium", "low"]},
            "action":   {"type": "string"},
        },
        "required": ["signal", "priority", "action"],
    },
    "minItems": 3,
    "maxItems": 3,
}


def _build_prompt(
    user_id: str,
    sectors: list[str],
    notices: list[dict],
    awards: list[dict],
    renewals: list[dict],
) -> str:
    sector_str = ", ".join(sectors) if sectors else "all sectors"
    notices_str = json.dumps(notices[:10], default=str, indent=2)
    awards_str  = json.dumps(awards[:8],   default=str, indent=2)
    renewals_str = json.dumps(renewals[:5], default=str, indent=2)

    return f"""
Generate exactly 3 market intelligence signals for a firm operating in: {sector_str}.

Context data:

ACTIVE HIGH-PRIORITY NOTICES (last scored):
{notices_str}

RECENT MBIE AWARD HISTORY (last 2 years, relevant sectors):
{awards_str}

UPCOMING CONTRACT RENEWALS (next 120 days):
{renewals_str}

Instructions:
- Produce exactly 3 signals as a JSON array (no wrapper, no markdown fences).
- Each signal object must have exactly three keys: "signal", "priority", "action".
- "signal": 1–2 sentences describing what is happening in the market right now.
  Reference a specific agency, notice title, dollar value, or award from the data.
- "priority": one of "high", "medium", "low".
- "action": 1 sentence — specific next step the firm should take this week.
- Do NOT speculate about renewals unless the contract appears in UPCOMING CONTRACT
  RENEWALS above with an explicit expiry date. Do not use "no re-award detected"
  or similar proxy reasoning.
- If renewal data is sparse, produce signals from active notices and award patterns only.

Return ONLY the JSON array. No explanation, no markdown.
"""


def generate_market_intelligence(user_id: str) -> list[dict]:
    """
    Generate 3 Claude-powered market signals for *user_id* and store them.

    Pulls user preferences, builds context, calls Claude, stores in
    market_signals table. Returns the 3 signal dicts.
    """
    from preferences import get_user_preferences

    prefs = get_user_preferences(user_id)
    sectors = prefs.get("sectors") or []

    notices  = _recent_notices_summary(sectors)
    awards   = _recent_awards_summary(sectors)
    renewals = _renewal_summary(sectors)

    prompt = _build_prompt(user_id, sectors, notices, awards, renewals)

    try:
        claude = _get_claude()
        resp = claude.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=800,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        signals: list[dict] = json.loads(raw)
    except Exception as exc:
        logger.error("generate_market_intelligence(%s) Claude error: %s", user_id, exc)
        # Fallback: generic signals so the dashboard doesn't break
        signals = [
            {"signal": "Market intelligence unavailable — pipeline error.",
             "priority": "low",
             "action": "Re-run the intelligence pipeline to refresh signals."},
        ]

    # Validate and cap to 3
    valid_signals = []
    for s in signals[:3]:
        if all(k in s for k in ("signal", "priority", "action")):
            if s["priority"] not in ("high", "medium", "low"):
                s["priority"] = "medium"
            valid_signals.append(s)

    _store_signals(user_id, valid_signals)
    return valid_signals


def _store_signals(user_id: str, signals: list[dict]) -> None:
    """Delete today's existing signals for *user_id* and insert fresh ones."""
    try:
        db.execute(
            "DELETE FROM market_signals WHERE user_id = %s "
            "AND generated_at::date = CURRENT_DATE",
            (user_id,),
        )
        for s in signals:
            db.execute(
                """
                INSERT INTO market_signals (user_id, signal, priority, action, generated_at)
                VALUES (%s, %s, %s, %s, NOW())
                """,
                (user_id, s["signal"], s["priority"], s["action"]),
            )
        logger.info("Stored %d signals for %s", len(signals), user_id)
    except Exception as exc:
        logger.error("_store_signals(%s): %s", user_id, exc)


def get_stored_signals(user_id: str) -> list[dict]:
    """
    Fetch today's stored signals for *user_id*.

    If none exist (first load or pipeline hasn't run today), generate them
    on-demand and cache them.
    """
    try:
        rows = db.fetchall(
            """
            SELECT signal, priority, action
              FROM market_signals
             WHERE user_id = %s
               AND generated_at::date = CURRENT_DATE
             ORDER BY id ASC
             LIMIT 3
            """,
            (user_id,),
        )
        if rows:
            return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("get_stored_signals(%s): %s", user_id, exc)

    # No signals for today — generate on demand
    try:
        return generate_market_intelligence(user_id)
    except Exception as exc:
        logger.error("on-demand signal generation failed for %s: %s", user_id, exc)
        return []
