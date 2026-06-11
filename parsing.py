"""
Notice parsing & normalisation module.

Reads raw_notices, produces structured parsed_notices rows.
"""
import logging
import re
from datetime import date, datetime
from typing import Optional, Tuple

import config
import db

logger = logging.getLogger(__name__)


# ── Sector classification ─────────────────────────────────────────────────────

def classify_sector(title: str, category_raw: str, description: str) -> str:
    text = " ".join(filter(None, [title, category_raw, description])).lower()
    best_sector = "other"
    best_count = 0
    for sector, keywords in config.SECTOR_KEYWORDS.items():
        count = sum(1 for kw in keywords if kw.lower() in text)
        if count > best_count:
            best_count = count
            best_sector = sector
    return best_sector


# ── Value parsing ─────────────────────────────────────────────────────────────

_VALUE_MULTIPLIERS = {
    "m": 1_000_000,
    "million": 1_000_000,
    "k": 1_000,
    "thousand": 1_000,
}


def _parse_value(raw: Optional[str]) -> Optional[float]:
    if not raw:
        return None
    raw = raw.replace(",", "").replace("$", "").replace("NZD", "").strip()
    # Range: take midpoint
    range_match = re.search(r"([\d\.]+)\s*[-–to]+\s*([\d\.]+)", raw, re.IGNORECASE)
    if range_match:
        lo, hi = float(range_match.group(1)), float(range_match.group(2))
        # Check for trailing multiplier
        suffix_match = re.search(r"([mk]|million|thousand)", raw, re.IGNORECASE)
        mult = _VALUE_MULTIPLIERS.get(suffix_match.group(1).lower(), 1) if suffix_match else 1
        return ((lo + hi) / 2) * mult

    single = re.search(r"([\d\.]+)\s*([mk]|million|thousand)?", raw, re.IGNORECASE)
    if single:
        val = float(single.group(1))
        suffix = single.group(2)
        mult = _VALUE_MULTIPLIERS.get(suffix.lower(), 1) if suffix else 1
        return val * mult

    return None


def assign_value_band(raw: Optional[str]) -> Tuple[str, Optional[float], Optional[float]]:
    value = _parse_value(raw)
    if value is None:
        return config.VALUE_BAND_UNKNOWN, None, None
    for band_name, lo, hi in config.VALUE_BANDS:
        lo_ok = lo is None or value >= lo
        hi_ok = hi is None or value < hi
        if lo_ok and hi_ok:
            return band_name, lo, hi
    return config.VALUE_BAND_UNKNOWN, None, None


# ── Duration parsing ──────────────────────────────────────────────────────────

def extract_duration(description: Optional[str]) -> Optional[str]:
    if not description:
        return None
    patterns = [
        r"(\d+)\s*-?\s*year\s+contract",
        r"(\d+)\s*months?",
        r"(\d+)\s*weeks?",
        r"term of\s+(\d+\s+\w+)",
        r"duration[:\s]+([^\.]+)",
        r"(\d+)\s*-?\s*year(?:s)?\s+(?:initial\s+)?(?:term)?",
    ]
    for pat in patterns:
        m = re.search(pat, description, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


# ── Geographic scope ──────────────────────────────────────────────────────────

NZ_REGIONS = [
    "auckland", "wellington", "christchurch", "canterbury", "otago", "waikato",
    "bay of plenty", "hawke's bay", "manawatu", "southland", "taranaki",
    "nelson", "marlborough", "northland", "gisborne", "westland",
    "national", "nationwide", "new zealand", "nz-wide", "all regions",
]


def extract_geographic_scope(description: Optional[str], title: Optional[str]) -> Optional[str]:
    text = " ".join(filter(None, [description, title])).lower()
    found = [r.title() for r in NZ_REGIONS if r in text]
    if not found:
        return None
    # Prefer "National" over regional if both found
    if any(r in ("National", "Nationwide", "New Zealand", "Nz-Wide", "All Regions") for r in found):
        return "National"
    return ", ".join(sorted(set(found)))


# ── Evaluation criteria ───────────────────────────────────────────────────────

CRITERIA_PATTERNS = [
    r"evaluation criteria[:\s]+([^\n\.]{10,200})",
    r"assessed on[:\s]+([^\n\.]{10,200})",
    r"weighting[:\s]+([^\n\.]{10,200})",
    r"selection criteria[:\s]+([^\n\.]{10,200})",
]


def extract_evaluation_criteria(description: Optional[str]) -> Optional[str]:
    if not description:
        return None
    for pat in CRITERIA_PATTERNS:
        m = re.search(pat, description, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


# ── Key dates from overview text ─────────────────────────────────────────────

_DATE_PATTERN = (
    r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
    r"\s+\d{4}"
    r"|\d{1,2}/\d{1,2}/\d{4}"
    r"|\d{4}-\d{2}-\d{2})"
)

_KEY_DATE_LABELS = {
    "briefing_date": [
        r"briefing\s+date[:\s]+",
        r"site\s+visit[:\s]+",
        r"briefing[:\s]+",
        r"supplier\s+briefing[:\s]+",
    ],
    "questions_deadline": [
        r"close\s+(?:date\s+)?for\s+questions[:\s]+",
        r"questions?\s+(?:must\s+be\s+)?(?:submitted|close(?:s)?)[:\s]+",
        r"deadline\s+for\s+questions[:\s]+",
        r"clarifications?\s+(?:close(?:s)?|deadline)[:\s]+",
    ],
    "registration_deadline": [
        r"registration\s+(?:of\s+interest\s+)?(?:closes?|deadline)[:\s]+",
        r"expressions?\s+of\s+interest\s+(?:closes?|deadline)[:\s]+",
        r"eoi\s+(?:closes?|deadline)[:\s]+",
        r"registration\s+deadline[:\s]+",
    ],
}


def _parse_date_str(text: str) -> Optional[date]:
    from datetime import datetime
    text = re.sub(r"\s*\(.*?\)", "", text).strip()
    text = re.sub(r"^\d{1,2}:\d{2}\s*(?:AM|PM)?\s*", "", text, flags=re.IGNORECASE).strip()
    for fmt in ("%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def extract_key_dates(overview_text: Optional[str]) -> dict:
    """
    Extract briefing_date, questions_deadline, registration_deadline from overview_text.
    Returns a dict with those three keys; values are date objects or None.
    """
    result = {k: None for k in _KEY_DATE_LABELS}
    if not overview_text:
        return result

    for field, patterns in _KEY_DATE_LABELS.items():
        for label_pat in patterns:
            full_pat = label_pat + r"\s*" + _DATE_PATTERN
            m = re.search(full_pat, overview_text, re.IGNORECASE)
            if m:
                result[field] = _parse_date_str(m.group(1))
                break
        if result[field] is not None:
            continue
        # Fallback: scan window after label keyword
        for label_pat in patterns:
            m_label = re.search(label_pat, overview_text, re.IGNORECASE)
            if m_label:
                window = overview_text[m_label.end(): m_label.end() + 80]
                m_date = re.search(_DATE_PATTERN, window, re.IGNORECASE)
                if m_date:
                    result[field] = _parse_date_str(m_date.group(1))
                    break

    return result


def extract_procurement_stage(category_raw: Optional[str], overview_text: Optional[str]) -> Optional[str]:
    """
    Derive procurement stage label from category_raw and/or overview_text keywords.
    """
    text = " ".join(filter(None, [category_raw, overview_text])).lower()
    if re.search(r"\bregistration\s+of\s+interest\b|\broi\b|\beoi\b|\bexpression\s+of\s+interest\b", text):
        return "Expression of Interest"
    if re.search(r"\brequest\s+for\s+proposal\b|\brfp\b", text):
        return "Request for Proposal"
    if re.search(r"\brequest\s+for\s+tender\b|\brft\b", text):
        return "Request for Tender"
    if re.search(r"\bpanel\b|\bprequalif", text):
        return "Panel / Prequalification"
    if re.search(r"\brequest\s+for\s+quote\b|\brfq\b", text):
        return "Request for Quote"
    return None


# ── Days until close ──────────────────────────────────────────────────────────

def days_until_close(close_date: Optional[date]) -> Optional[int]:
    if close_date is None:
        return None
    delta = (close_date - date.today()).days
    return max(delta, 0)


# ── Storage ───────────────────────────────────────────────────────────────────

def _store_parsed(parsed: dict) -> None:
    db.execute(
        """
        INSERT INTO parsed_notices
            (notice_id, agency_name, sector_tag, value_band,
             estimated_value_min, estimated_value_max, contract_duration,
             geographic_scope, evaluation_criteria, close_date, days_until_close,
             briefing_date, questions_deadline, registration_deadline, procurement_stage)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (notice_id) DO UPDATE SET
            agency_name           = EXCLUDED.agency_name,
            sector_tag            = EXCLUDED.sector_tag,
            value_band            = EXCLUDED.value_band,
            estimated_value_min   = EXCLUDED.estimated_value_min,
            estimated_value_max   = EXCLUDED.estimated_value_max,
            contract_duration     = EXCLUDED.contract_duration,
            geographic_scope      = EXCLUDED.geographic_scope,
            evaluation_criteria   = EXCLUDED.evaluation_criteria,
            close_date            = EXCLUDED.close_date,
            days_until_close      = EXCLUDED.days_until_close,
            briefing_date         = EXCLUDED.briefing_date,
            questions_deadline    = EXCLUDED.questions_deadline,
            registration_deadline = EXCLUDED.registration_deadline,
            procurement_stage     = EXCLUDED.procurement_stage,
            parsed_at             = NOW()
        """,
        (
            parsed["notice_id"],
            parsed["agency_name"],
            parsed["sector_tag"],
            parsed["value_band"],
            parsed["estimated_value_min"],
            parsed["estimated_value_max"],
            parsed["contract_duration"],
            parsed["geographic_scope"],
            parsed["evaluation_criteria"],
            parsed["close_date"],
            parsed["days_until_close"],
            parsed["briefing_date"],
            parsed["questions_deadline"],
            parsed["registration_deadline"],
            parsed["procurement_stage"],
        ),
    )


# ── Main entry point ──────────────────────────────────────────────────────────

def run_parsing() -> int:
    logger.info("Starting notice parsing")

    raw_notices = db.fetchall(
        """
        SELECT r.notice_id, r.title, r.agency, r.category_raw,
               r.estimated_value, r.close_date, r.description, r.overview_text
        FROM   raw_notices r
        LEFT JOIN parsed_notices p ON p.notice_id = r.notice_id
        WHERE  p.notice_id IS NULL
        """
    )

    logger.info("%d raw notices to parse", len(raw_notices))
    count = 0

    for raw in raw_notices:
        try:
            sector = classify_sector(
                raw.get("title") or "",
                raw.get("category_raw") or "",
                raw.get("description") or "",
            )
            # Sector conflict check: auto-correct high-confidence mismatches
            from sector_classifier import resolve_sector_conflict as _rsc
            _rsc_result = _rsc(
                notice_title=raw.get("title") or "",
                notice_description=raw.get("description") or "",
                stored_sector=sector,
                notice_id=raw["notice_id"],
            )
            sector = _rsc_result["sector"]

            value_band, val_min, val_max = assign_value_band(raw.get("estimated_value"))
            overview = raw.get("overview_text") or raw.get("description")
            duration = extract_duration(overview)
            geo = extract_geographic_scope(overview, raw.get("title"))
            criteria = extract_evaluation_criteria(overview)
            close_dt = raw.get("close_date")
            dtc = days_until_close(close_dt)
            key_dates = extract_key_dates(overview)
            stage = extract_procurement_stage(raw.get("category_raw"), overview)

            parsed = {
                "notice_id": raw["notice_id"],
                "agency_name": raw.get("agency"),
                "sector_tag": sector,
                "value_band": value_band,
                "estimated_value_min": val_min,
                "estimated_value_max": val_max,
                "contract_duration": duration,
                "geographic_scope": geo,
                "evaluation_criteria": criteria,
                "close_date": close_dt,
                "days_until_close": dtc,
                "briefing_date": key_dates["briefing_date"],
                "questions_deadline": key_dates["questions_deadline"],
                "registration_deadline": key_dates["registration_deadline"],
                "procurement_stage": stage,
            }
            _store_parsed(parsed)
            count += 1
            logger.debug("Parsed notice %s → sector=%s, band=%s", raw["notice_id"], sector, value_band)
        except Exception as exc:
            logger.warning("Failed to parse notice %s: %s", raw.get("notice_id"), exc)

    logger.info("Parsing complete: %d notices parsed", count)

    # Reclassify recent notices in case keywords were updated
    changed = reclassify_recent(days=14)
    if changed:
        logger.info("Reclassified %d recently-parsed notices with updated keywords", changed)

    return count


def reclassify_recent(days: int = 14) -> int:
    """
    Re-run sector classification on notices parsed in the last N days.
    Updates sector_tag where classification has changed under the current keywords.
    Returns count of updated rows.
    """
    from datetime import timedelta
    cutoff = date.today() - timedelta(days=days)
    rows = db.fetchall(
        """
        SELECT r.notice_id, r.title, r.category_raw,
               r.description, r.overview_text,
               p.sector_tag AS current_sector
          FROM raw_notices r
          JOIN parsed_notices p ON p.notice_id = r.notice_id
         WHERE p.parsed_at >= %s
        """,
        (cutoff,),
    )
    changed = 0
    for row in rows:
        try:
            description = row.get("overview_text") or row.get("description") or ""
            new_sector = classify_sector(
                row.get("title") or "",
                row.get("category_raw") or "",
                description,
            )
            if new_sector != row["current_sector"]:
                db.execute(
                    "UPDATE parsed_notices SET sector_tag = %s WHERE notice_id = %s",
                    (new_sector, row["notice_id"]),
                )
                logger.info(
                    "Reclassified %s: %s → %s  ('%s')",
                    row["notice_id"], row["current_sector"], new_sector,
                    (row.get("title") or "")[:60],
                )
                changed += 1
        except Exception as exc:
            logger.warning("Reclassify failed for %s: %s", row.get("notice_id"), exc)
    return changed
