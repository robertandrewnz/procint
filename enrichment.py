"""
AI enrichment module via Claude API.

For each notice above the priority threshold that has not yet been enriched,
calls Claude to produce:
  - 3-sentence plain language summary
  - Likely evaluation criteria weighting
  - Red flags
  - One-sentence strategic framing
"""
import json
import logging

import anthropic

import config
import db

logger = logging.getLogger(__name__)

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


# ── Prompt construction ───────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a senior procurement intelligence analyst supporting a boutique advisory firm in New Zealand. Your role is to analyse government procurement notices and provide sharp, commercially-focused insights for strategic decision-making.

Respond ONLY with a valid JSON object — no preamble, no markdown fences. Use the exact keys specified."""

USER_PROMPT_TEMPLATE = """Analyse this New Zealand government procurement notice and return a JSON object with exactly these keys:

"summary": A 3-sentence plain language summary of what is being procured, who the buyer is, and the likely contract scope. Write as if briefing a busy partner.

"evaluation_weighting": Your best inference of how the evaluation panel will actually weight criteria — even if not explicitly stated. Reference any stated criteria and supplement with sector norms. One concise paragraph.

"red_flags": A list of strings, each a concise red flag (onerous terms, unrealistic timeline, potentially wired spec, limited market engagement, unusual conditions). Empty list [] if none identified.

"strategic_framing": One sentence on what winning this contract would strategically mean for a firm — market position, reference site value, revenue, relationship access.

--- NOTICE DATA ---
Title: {title}
Agency: {agency}
Sector: {sector_tag}
Value Band: {value_band}
Close Date: {close_date}
Days Until Close: {days_until_close}
Evaluation Criteria (stated): {evaluation_criteria}
Description:
{description}
"""


def _build_prompt(notice: dict) -> str:
    return USER_PROMPT_TEMPLATE.format(
        title=notice.get("title") or "Unknown",
        agency=notice.get("agency") or "Unknown",
        sector_tag=notice.get("sector_tag") or "other",
        value_band=notice.get("value_band") or "unknown",
        close_date=str(notice.get("close_date") or "Unknown"),
        days_until_close=notice.get("days_until_close") or "Unknown",
        evaluation_criteria=notice.get("evaluation_criteria") or "Not stated",
        description=(notice.get("description") or "Not provided")[:3000],
    )


# ── Claude call ───────────────────────────────────────────────────────────────

def _enrich_notice(notice: dict) -> dict | None:
    client = _get_client()
    prompt = _build_prompt(notice)

    try:
        message = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=config.CLAUDE_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = message.content[0].text.strip()

        # Strip accidental markdown fences
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
            raw_text = raw_text.strip()

        result = json.loads(raw_text)
        return result
    except json.JSONDecodeError as exc:
        logger.warning("JSON parse failed for notice %s: %s", notice.get("notice_id"), exc)
        return None
    except anthropic.APIError as exc:
        logger.error("Anthropic API error for notice %s: %s", notice.get("notice_id"), exc)
        return None


# ── Storage ───────────────────────────────────────────────────────────────────

def _store_enrichment(notice_id: str, result: dict) -> None:
    red_flags = result.get("red_flags", [])
    red_flags_str = (
        "; ".join(red_flags) if isinstance(red_flags, list) else str(red_flags)
    )
    db.execute(
        """
        INSERT INTO enriched_notices
            (notice_id, summary, evaluation_weighting, red_flags, strategic_framing)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (notice_id) DO UPDATE SET
            summary              = EXCLUDED.summary,
            evaluation_weighting = EXCLUDED.evaluation_weighting,
            red_flags            = EXCLUDED.red_flags,
            strategic_framing    = EXCLUDED.strategic_framing,
            enriched_at          = NOW()
        """,
        (
            notice_id,
            result.get("summary"),
            result.get("evaluation_weighting"),
            red_flags_str,
            result.get("strategic_framing"),
        ),
    )


# ── Main entry point ──────────────────────────────────────────────────────────

def run_enrichment() -> int:
    logger.info(
        "Starting AI enrichment (threshold=%.1f)", config.PRIORITY_THRESHOLD
    )

    rows = db.fetchall(
        """
        SELECT r.notice_id, r.title, r.agency, r.description,
               p.sector_tag, p.value_band, p.close_date,
               p.days_until_close, p.evaluation_criteria
        FROM   scored_notices s
        JOIN   raw_notices r    ON r.notice_id = s.notice_id
        JOIN   parsed_notices p ON p.notice_id = s.notice_id
        LEFT JOIN enriched_notices e ON e.notice_id = s.notice_id
        WHERE  s.composite_score >= %s
          AND  e.notice_id IS NULL
        ORDER  BY s.composite_score DESC
        """,
        (config.PRIORITY_THRESHOLD,),
    )

    logger.info("%d notices above threshold requiring enrichment", len(rows))
    count = 0

    for notice in rows:
        logger.info(
            "Enriching notice %s: %s", notice["notice_id"], notice.get("title")
        )
        result = _enrich_notice(notice)
        if result:
            _store_enrichment(notice["notice_id"], result)
            count += 1
        else:
            logger.warning("Enrichment returned no result for %s", notice["notice_id"])

    logger.info("Enrichment complete: %d notices enriched", count)
    return count
