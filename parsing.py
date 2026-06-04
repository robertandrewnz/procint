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
             geographic_scope, evaluation_criteria, close_date, days_until_close)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (notice_id) DO UPDATE SET
            agency_name         = EXCLUDED.agency_name,
            sector_tag          = EXCLUDED.sector_tag,
            value_band          = EXCLUDED.value_band,
            estimated_value_min = EXCLUDED.estimated_value_min,
            estimated_value_max = EXCLUDED.estimated_value_max,
            contract_duration   = EXCLUDED.contract_duration,
            geographic_scope    = EXCLUDED.geographic_scope,
            evaluation_criteria = EXCLUDED.evaluation_criteria,
            close_date          = EXCLUDED.close_date,
            days_until_close    = EXCLUDED.days_until_close,
            parsed_at           = NOW()
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
        ),
    )


# ── Main entry point ──────────────────────────────────────────────────────────

def run_parsing() -> int:
    logger.info("Starting notice parsing")

    raw_notices = db.fetchall(
        """
        SELECT r.notice_id, r.title, r.agency, r.category_raw,
               r.estimated_value, r.close_date, r.description
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
            value_band, val_min, val_max = assign_value_band(raw.get("estimated_value"))
            duration = extract_duration(raw.get("description"))
            geo = extract_geographic_scope(raw.get("description"), raw.get("title"))
            criteria = extract_evaluation_criteria(raw.get("description"))
            close_dt = raw.get("close_date")
            dtc = days_until_close(close_dt)

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
            }
            _store_parsed(parsed)
            count += 1
            logger.debug("Parsed notice %s → sector=%s, band=%s", raw["notice_id"], sector, value_band)
        except Exception as exc:
            logger.warning("Failed to parse notice %s: %s", raw.get("notice_id"), exc)

    logger.info("Parsing complete: %d notices parsed", count)
    return count
