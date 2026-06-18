"""
sector_classifier.py — Hybrid three-pass sector classification system.

Pass 1 — Keyword classifier (all notices)
    Score each notice against explicit keyword lists with per-sector confidence
    thresholds.  If exactly one sector reaches HIGH confidence and no other does,
    classify immediately (cheap, no API call needed).

Pass 2 — Claude API (ambiguous notices)
    Used when Pass 1 produces: zero HIGH-confidence sectors, or multiple sectors
    tie at HIGH confidence.  Sends title + description + agency to Claude Haiku
    and gets back a structured JSON classification.

Pass 3 — Human review queue (low-confidence Claude results)
    Any notice Claude rates "low" confidence is flagged needs_sector_review=TRUE
    in the DB and appears at /admin/sector-review with a ⚠ badge.

All three passes record classification_method, classification_confidence, and
classification_reasoning on the parsed_notices row so we can audit accuracy
and improve keyword rules over time.

Human corrections (sector_corrections table) feed the audit trail.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

import config
import db

logger = logging.getLogger(__name__)

# ── Known sector aliases ───────────────────────────────────────────────────────
# Maps synonyms the DB might store to canonical sector names.
_SECTOR_ALIASES: dict[str, str] = {
    "infra":                "infrastructure",
    "roads":                "infrastructure",
    "roading":              "infrastructure",
    "cyber":                "cybersecurity",
    "ict":                  "ICT",
    "it":                   "ICT",
    "professional services":"professional_services",
    "prof_services":        "professional_services",
}

ALL_SECTORS = list(config.SECTOR_KEYWORDS.keys())


# ── Pass 0: UNSPSC code recognition ───────────────────────────────────────────
# Maps the leading two digits of an 8-digit UNSPSC code to a sector tag.
# When a notice description or title contains a recognisable UNSPSC code,
# that is treated as a high-confidence classification signal and Pass 1/2 are
# skipped.  Covers the most common mis-classified code families first.

_UNSPSC_PREFIX_SECTOR: dict[str, str] = {
    # Construction / facilities
    "72": "construction",       # Building and Facility Construction and Maintenance
    "73": "construction",       # Industrial Production and Manufacturing Services
    # FM / facilities management
    "76": "FM",                 # Domestic and Personal Services
    # Advisory / management consulting
    "80": "advisory",           # Management Advisory and Consulting Services
    # Engineering / research (professional services)
    "81": "professional_services",
    # Public utilities
    "83": "utilities",          # Public Utilities and Public Sector Related Services
    # Healthcare and pharmaceuticals
    "42": "health",             # Medical Equipment and Accessories
    "51": "health",             # Drugs and Pharmaceutical Products
    "85": "health",             # Healthcare Services
    # Education / training
    "86": "professional_services",
    # Defence / national security
    "92": "defence",            # National Defence and Military
    # Civic / politics → other
    "93": "other",              # Politics and Civic Affairs
}


def _unspsc_pass0(text: str) -> str | None:
    """
    Scan *text* for 8-digit UNSPSC codes.  If any are found, return the
    sector for the most-frequent code prefix.  Returns None when no codes
    are detected so the caller falls through to keyword/Claude classification.
    """
    from collections import Counter
    codes = re.findall(r"\b([4-9][0-9])\d{6}\b", text)
    if not codes:
        return None
    prefix = Counter(codes).most_common(1)[0][0]
    return _UNSPSC_PREFIX_SECTOR.get(prefix)


# ── Pass 1 helpers ─────────────────────────────────────────────────────────────

def _kw_matches(kw: str, text_lower: str) -> bool:
    """
    True when *kw* appears in *text_lower* as a whole word / whole phrase.

    Short acronyms (≤4 chars, all-uppercase in the original list) are matched
    with strict word boundaries so that e.g. "SOC" does not hit "associated"
    or "SIEM" does not hit "system".  Multi-word phrases and longer terms use
    a simple substring check (they are specific enough not to false-match).
    """
    kl = kw.lower()
    # Multi-word phrases: substring is fine (phrase length prevents accidents)
    if " " in kl:
        return kl in text_lower
    # Short single tokens (≤ 4 chars): require word boundaries
    if len(kl) <= 4:
        return bool(re.search(r"\b" + re.escape(kl) + r"\b", text_lower))
    # Longer single tokens: substring is safe (e.g. "chipsealing", "wastewater")
    return kl in text_lower


def _keyword_scores(text: str) -> dict[str, int]:
    """
    Count how many keywords from each sector appear in *text* (case-insensitive,
    word-boundary-safe matching).  Returns {sector: match_count}.
    """
    t = text.lower()
    return {
        sector: sum(1 for kw in kws if _kw_matches(kw, t))
        for sector, kws in config.SECTOR_KEYWORDS.items()
    }


def _pass1(title: str, description: str) -> dict | None:
    """
    Run keyword scoring.  Returns a classification dict if unambiguous, else None.

    A result is unambiguous when EXACTLY ONE sector reaches its HIGH threshold
    and no other sector reaches its own HIGH threshold.

    Returns:
        None                      — ambiguous, send to Pass 2
        {sector, confidence, method, match_count, reasoning} — definitive result
    """
    text = " ".join(filter(None, [title, description]))
    scores = _keyword_scores(text)

    high_sectors = [
        s for s, n in scores.items()
        if n >= config.SECTOR_HIGH_THRESHOLD.get(s, 2)
    ]

    if len(high_sectors) == 1:
        sector = high_sectors[0]
        n = scores[sector]
        return {
            "sector":     sector,
            "confidence": "high",
            "method":     "keyword",
            "match_count": n,
            "reasoning":  (
                f"Keyword match: {n} term(s) from '{sector}' keyword list. "
                f"No other sector reached high-confidence threshold."
            ),
        }

    # Zero or multiple high-confidence matches → ambiguous
    return None


# ── Pass 2 — Claude API ────────────────────────────────────────────────────────

_CLAUDE_SYSTEM = (
    "You are a New Zealand government procurement sector classifier. "
    "Classify the procurement notice into EXACTLY ONE sector from this list:\n"
    "infrastructure, construction, FM, cybersecurity, ICT, defence, health, "
    "advisory, security, professional_services, utilities, other\n\n"
    "Definitions:\n"
    "  infrastructure — civil / roading / water / bridge / horizontal infrastructure\n"
    "  construction   — building works, fitout, refurbishment, school property\n"
    "  FM             — facilities management, cleaning, grounds, building maintenance\n"
    "  cybersecurity  — pen testing, SOC, SIEM, infosec, zero trust\n"
    "  ICT            — software, cloud, digital transformation, ERP, data platforms\n"
    "  defence        — NZDF, military, RNZAF, RNZN\n"
    "  health         — clinical, hospital, nursing, aged care, mental health\n"
    "  advisory       — consulting, policy, evaluation, research services\n"
    "  security       — manned guarding, patrol, CCTV monitoring, animal control\n"
    "  professional_services — legal, accounting, HR, recruitment, training\n"
    "  utilities      — electricity, gas, telecoms, waste management\n"
    "  other          — cannot be classified into any of the above\n\n"
    "Return ONLY valid JSON — no markdown, no explanation outside the JSON:\n"
    '{"sector": "<sector>", "confidence": "<high|medium|low>", '
    '"reasoning": "<one sentence>"}'
)


def _call_claude(title: str, description: str, agency: str) -> dict | None:
    """
    Call Claude Haiku for sector classification.
    Returns parsed JSON dict or None on error.
    """
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        user_msg = (
            f"Notice title: {title}\n"
            f"Agency: {agency or 'Unknown'}\n"
            f"Description: {(description or '')[:800]}"
        )
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=256,
            system=_CLAUDE_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        sector = data.get("sector", "other")
        if sector not in ALL_SECTORS:
            sector = "other"
        return {
            "sector":     sector,
            "confidence": data.get("confidence", "medium"),
            "method":     "claude",
            "match_count": 0,
            "reasoning":  data.get("reasoning", "")[:500],
        }
    except Exception as exc:
        logger.warning("Claude classification failed: %s", exc)
        return None


# ── DB persistence ─────────────────────────────────────────────────────────────

def _save_classification(
    notice_id: str,
    sector: str,
    method: str,
    confidence: str,
    reasoning: str,
    needs_review: bool,
) -> None:
    """Persist classification metadata + sector_tag to parsed_notices."""
    try:
        db.execute(
            """
            UPDATE parsed_notices
               SET sector_tag                = %s,
                   classification_method     = %s,
                   classification_confidence = %s,
                   classification_reasoning  = %s,
                   needs_sector_review       = %s,
                   parsed_at                 = NOW()
             WHERE notice_id = %s
            """,
            (sector, method, confidence, reasoning, needs_review, notice_id),
        )
    except Exception as exc:
        logger.warning("DB classification save failed for %s: %s", notice_id, exc)


# ── Public interface ───────────────────────────────────────────────────────────

def classify_notice(
    notice_title: str,
    notice_description: str,
    notice_agency: str = "",
    notice_id: Optional[str] = None,
    persist: bool = True,
) -> dict:
    """
    Full three-pass classification for one notice.

    Args:
        notice_title:       Title string.
        notice_description: Description string (may be empty).
        notice_agency:      Agency name (used in Claude prompt).
        notice_id:          DB notice ID — required when persist=True.
        persist:            Write result to parsed_notices when True.

    Returns:
        {
          "sector":     str,   — canonical sector tag
          "confidence": str,   — "high" | "medium" | "low"
          "method":     str,   — "keyword" | "claude" | "fallback"
          "reasoning":  str,   — human-readable rationale
          "needs_review": bool — True when flagged for admin queue
        }
    """
    title = (notice_title or "").strip()
    desc  = (notice_description or "").strip()
    agency = (notice_agency or "").strip()

    # ── Pass 0: UNSPSC code recognition ──────────────────────────────────────
    unspsc_sector = _unspsc_pass0(f"{title} {desc}")
    if unspsc_sector:
        result: dict | None = {
            "sector":      unspsc_sector,
            "confidence":  "high",
            "method":      "unspsc",
            "match_count": 1,
            "reasoning":   f"UNSPSC code prefix detected in notice text → '{unspsc_sector}'.",
        }
    else:
        result = None

    # ── Pass 1 ────────────────────────────────────────────────────────────────
    if result is None:
        result = _pass1(title, desc)

    # ── Pass 2 ────────────────────────────────────────────────────────────────
    if result is None:
        result = _call_claude(title, desc, agency)

    # ── Fallback ──────────────────────────────────────────────────────────────
    if result is None:
        result = {
            "sector":     "other",
            "confidence": "low",
            "method":     "fallback",
            "match_count": 0,
            "reasoning":  "Claude API unavailable; assigned 'other' by fallback.",
        }

    needs_review = (
        result["method"] in ("claude", "fallback")
        and result["confidence"] == "low"
    )

    result["needs_review"] = needs_review

    if persist and notice_id:
        _save_classification(
            notice_id=notice_id,
            sector=result["sector"],
            method=result["method"],
            confidence=result["confidence"],
            reasoning=result["reasoning"],
            needs_review=needs_review,
        )
        if needs_review:
            logger.info(
                "Notice %s flagged for sector review (method=%s, confidence=low)",
                notice_id, result["method"],
            )

    logger.debug(
        "classify_notice: id=%s  sector=%s  method=%s  confidence=%s  review=%s",
        notice_id or "?", result["sector"], result["method"],
        result["confidence"], needs_review,
    )
    return result


# ── Backward-compat shim ───────────────────────────────────────────────────────

def resolve_sector_conflict(
    notice_title: str,
    notice_description: str,
    stored_sector: str,
    notice_id: Optional[str] = None,
    mbie_category: Optional[str] = None,
) -> dict:
    """
    Backward-compatible wrapper used by parsing.py, generate_demo_content.py,
    and renewal_radar.py.

    Runs the full three-pass classifier and returns a dict in the legacy shape:
        {sector, original_sector, action, confidence, match_count, note}
    """
    new = classify_notice(
        notice_title=notice_title,
        notice_description=" ".join(filter(None, [notice_description, mbie_category or ""])),
        notice_agency="",
        notice_id=notice_id,
        # Only persist when we actually have a notice_id to avoid updating wrong rows
        persist=(notice_id is not None),
    )

    action: str
    if new["sector"] != stored_sector:
        action = "corrected"
    elif new["confidence"] == "low":
        action = "flagged"
    else:
        action = "unchanged"

    note = new["reasoning"]
    if action == "flagged":
        note = (
            f"⚠ Sector unverified — stored as '{stored_sector}' but "
            f"low-confidence classification. {note}"
        )

    return {
        "sector":          new["sector"],
        "original_sector": stored_sector,
        "action":          action,
        "confidence":      new["confidence"],
        "match_count":     new.get("match_count", 0),
        "note":            note,
    }


# ── Retrospective reclassification ─────────────────────────────────────────────

def run_reclassify_all(
    only_unclassified: bool = False,
    delay_between_claude: float = 0.3,
    sector_filter: "Optional[str]" = None,
) -> dict:
    """
    Pass every existing parsed_notice through the full three-pass classifier.

    Args:
        only_unclassified: When True, skip notices that already have
                           classification_method set (faster incremental run).
        delay_between_claude: Seconds to sleep between Claude API calls to
                              avoid rate-limit bursts.
        sector_filter: When set, only reclassify notices currently tagged with
                       this sector (e.g. "cybersecurity", "health").

    Returns:
        {"keyword": n, "claude": n, "fallback": n, "total": n,
         "corrected": n, "needs_review": n}
    """
    clauses = []
    params: list = []
    if only_unclassified:
        clauses.append("p.classification_method IS NULL")
    if sector_filter:
        clauses.append("p.sector_tag = %s")
        params.append(sector_filter)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    rows = db.fetchall(
        f"""
        SELECT p.notice_id, p.sector_tag, r.title, r.description, r.agency
          FROM parsed_notices p
          JOIN raw_notices r ON r.notice_id = p.notice_id
        {where}
        """,
        params or None,
    )
    total = len(rows)
    logger.info(
        "Retrospective reclassification: %d notices (only_unclassified=%s)",
        total, only_unclassified,
    )

    counts: dict[str, int] = {
        "keyword":      0,
        "claude":       0,
        "fallback":     0,
        "total":        total,
        "corrected":    0,
        "needs_review": 0,
    }

    for i, row in enumerate(rows, 1):
        nid    = row["notice_id"]
        old_sector = row.get("sector_tag") or "other"
        try:
            result = classify_notice(
                notice_title=row.get("title") or "",
                notice_description=row.get("description") or "",
                notice_agency=row.get("agency") or "",
                notice_id=nid,
                persist=True,
            )
            method = result["method"]
            counts[method] = counts.get(method, 0) + 1
            if result["sector"] != old_sector:
                counts["corrected"] += 1
                logger.info(
                    "[%d/%d] Reclassified %s: %s → %s (%s, %s)",
                    i, total, nid, old_sector, result["sector"],
                    method, result["confidence"],
                )
            if result["needs_review"]:
                counts["needs_review"] += 1
            # Throttle Claude calls
            if method == "claude":
                time.sleep(delay_between_claude)
        except Exception as exc:
            logger.warning("[%d/%d] Reclassify failed for %s: %s", i, total, nid, exc)

    logger.info(
        "Reclassification complete — keyword=%d claude=%d fallback=%d "
        "corrected=%d needs_review=%d",
        counts["keyword"], counts["claude"], counts["fallback"],
        counts["corrected"], counts["needs_review"],
    )
    return counts


# ── Human correction ───────────────────────────────────────────────────────────

def apply_human_correction(
    notice_id: str,
    corrected_sector: str,
    corrected_by: str,
    note: str = "",
) -> bool:
    """
    Record a manual sector correction from the admin review queue.
    Updates parsed_notices and inserts into sector_corrections for audit trail.
    Returns True on success.
    """
    try:
        old = db.fetchone(
            "SELECT sector_tag FROM parsed_notices WHERE notice_id = %s",
            (notice_id,),
        )
        if not old:
            logger.warning("apply_human_correction: notice_id %s not found", notice_id)
            return False

        db.execute(
            """
            UPDATE parsed_notices
               SET sector_tag                = %s,
                   classification_method     = 'human',
                   classification_confidence = 'high',
                   classification_reasoning  = %s,
                   needs_sector_review       = FALSE,
                   parsed_at                 = NOW()
             WHERE notice_id = %s
            """,
            (
                corrected_sector,
                f"Manually corrected by {corrected_by}"
                + (f": {note}" if note else ""),
                notice_id,
            ),
        )
        db.execute(
            """
            INSERT INTO sector_corrections
                (notice_id, original_sector, corrected_sector, corrected_by, note)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (notice_id, old["sector_tag"], corrected_sector, corrected_by, note),
        )
        logger.info(
            "Human correction: %s  %s → %s  by %s",
            notice_id, old["sector_tag"], corrected_sector, corrected_by,
        )
        return True
    except Exception as exc:
        logger.error("apply_human_correction failed for %s: %s", notice_id, exc)
        return False


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import logging as _logging
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )

    ap = argparse.ArgumentParser(description="Hybrid sector classifier")
    ap.add_argument(
        "--reclassify-all",
        action="store_true",
        help="Reclassify every notice in the DB",
    )
    ap.add_argument(
        "--incremental",
        action="store_true",
        help="Only classify notices without classification_method set",
    )
    ap.add_argument(
        "--test",
        metavar="TITLE",
        help="Test classify a single notice title (prints result)",
    )
    ap.add_argument(
        "--sector",
        metavar="SECTOR",
        help="Only reclassify notices currently tagged with this sector "
             "(e.g. cybersecurity, health).  Use with --reclassify-all.",
    )
    args = ap.parse_args()

    if args.test:
        r = classify_notice(
            notice_title=args.test,
            notice_description="",
            persist=False,
        )
        print(json.dumps(r, indent=2))

    elif args.reclassify_all or args.incremental:
        result = run_reclassify_all(
            only_unclassified=args.incremental,
            sector_filter=args.sector or None,
        )
        print(json.dumps(result, indent=2))
    else:
        ap.print_help()
