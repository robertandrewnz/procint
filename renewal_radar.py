"""
renewal_radar.py — Renewal Pipeline: three-tier contract renewal intelligence.

Sources (in priority order):
  1. mbie_award_notices WHERE contract_expiry IS NOT NULL
     (backfilled by enrich_award_durations.py)
     — only rows with EXPLICIT expiry dates, not calculated from duration alone.
  2. contract_awards WHERE end_date IS NOT NULL
     (from GETS award detail pages when accessible)
  3. raw_notices WHERE category_raw IN ('ROI', 'RFI', 'EOI')
     (market soundings — active Requests for Information / Expressions of Interest)

Quality filters applied:
  • Financial year-end false signals removed (30 Jun, 31 Dec of any year)
  • ROI/RFI/EOI notices always shown as "market soundings" tier regardless of timing

Three output tiers:
  • imminent       — explicit end_date within next 90 days
  • approaching    — explicit end_date 90–180 days
  • market_sounding — active ROI/RFI/EOI notices (likely procurement 3–12 months out)

Label: "Renewal Pipeline — sourced from GETS award notices and market soundings"
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Optional

import db

logger = logging.getLogger(__name__)

# Days defining the three tiers
IMMINENT_DAYS   =  90
APPROACHING_DAYS = 180

# Financial year end dates to suppress (month, day)
_FY_END_DATES = {(6, 30), (12, 31)}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_fy_end(d: date) -> bool:
    """Return True if the date is a known financial year-end false signal."""
    return (d.month, d.day) in _FY_END_DATES


def _window_label(expiry: date, today: date) -> str:
    delta = (expiry - today).days
    if delta == 0:
        return f"Expires today — {expiry.strftime('%-d %b %Y')}"
    if delta <= 14:
        return f"Opens now — expires {expiry.strftime('%-d %b %Y')}"
    if delta <= 45:
        return f"Opens this month — expires {expiry.strftime('%-d %b %Y')}"
    months = round(delta / 30.44)
    if months == 1:
        return f"Opens in 1 month — expires {expiry.strftime('%-d %b %Y')}"
    if months <= 6:
        return f"Opens in {months} months — expires {expiry.strftime('%-d %b %Y')}"
    q = (expiry.month - 1) // 3 + 1
    return f"Renewal due Q{q} {expiry.year}"


def _dedup_key(agency: str, title: str) -> str:
    agency_norm = (agency or "").lower().strip()
    words = re.sub(r"[^\w\s]", "", (title or "")).lower().split()[:8]
    return f"{agency_norm}|{' '.join(words)}"


def _coerce_date(val) -> Optional[date]:
    if val is None:
        return None
    if isinstance(val, date):
        return val
    try:
        from datetime import datetime
        return datetime.strptime(str(val)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


# ── Source queries ────────────────────────────────────────────────────────────

def _query_mbie(today: date, cutoff: date, sector_where: str, sector_params: list) -> list[dict]:
    """
    MBIE award notices with explicit contract_expiry.
    Only rows where enrich_award_durations found an EXPLICIT date — we trust
    the mbie source flag for this rather than re-detecting here.
    """
    try:
        rows = db.fetchall(
            f"""
            SELECT
                m.rfx_id            AS source_id,
                m.title,
                m.posting_agency    AS agency_name,
                NULL::TEXT          AS supplier_name,
                m.awarded_amount    AS contract_value,
                m.awarded_date,
                m.contract_duration_months AS duration_months,
                m.contract_expiry   AS expiry_date,
                m.sector_tag,
                'mbie'              AS data_source,
                NULL::TEXT          AS source_url
              FROM mbie_award_notices m
             WHERE m.contract_expiry IS NOT NULL
               AND m.contract_expiry >= %s
               AND m.contract_expiry <= %s
               AND m.awarded_date IS NOT NULL
               {sector_where}
             ORDER BY m.contract_expiry ASC
             LIMIT 50
            """,
            (today.isoformat(), cutoff.isoformat(), *sector_params),
        )
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("renewal_radar mbie query failed: %s", exc)
        return []


def _query_gets_awards(today: date, cutoff: date, sector_where: str, sector_params: list) -> list[dict]:
    try:
        rows = db.fetchall(
            f"""
            SELECT
                ca.gets_notice_id   AS source_id,
                ca.title,
                COALESCE(o_a.name, ca.agency_name_raw)   AS agency_name,
                COALESCE(o_s.name, ca.supplier_name_raw) AS supplier_name,
                ca.contract_value,
                ca.award_date       AS awarded_date,
                ca.duration_months,
                ca.end_date         AS expiry_date,
                ca.sector_tag,
                'gets'              AS data_source,
                ca.source_url
              FROM contract_awards ca
              LEFT JOIN organisations o_a ON o_a.org_id = ca.agency_org_id
              LEFT JOIN organisations o_s ON o_s.org_id = ca.supplier_org_id
             WHERE ca.end_date IS NOT NULL
               AND ca.end_date >= %s
               AND ca.end_date <= %s
               {sector_where}
             ORDER BY ca.end_date ASC
             LIMIT 30
            """,
            (today.isoformat(), cutoff.isoformat(), *sector_params),
        )
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("renewal_radar gets query failed: %s", exc)
        return []


def _query_market_soundings(user_sectors: Optional[list]) -> list[dict]:
    """
    Active ROI/RFI/EOI notices from raw_notices → market sounding tier.
    These indicate procurement is likely approaching in the next 3–12 months.
    """
    sector_where = ""
    sector_params: list = []
    if user_sectors:
        placeholders = ",".join(["%s"] * len(user_sectors))
        sector_where = f"AND p.sector_tag IN ({placeholders})"
        sector_params = list(user_sectors)
    try:
        rows = db.fetchall(
            f"""
            SELECT
                r.notice_id     AS source_id,
                r.title,
                r.agency        AS agency_name,
                NULL::TEXT      AS supplier_name,
                NULL::NUMERIC   AS contract_value,
                r.fetched_at::date AS awarded_date,
                NULL::INT       AS duration_months,
                r.close_date    AS expiry_date,
                p.sector_tag,
                'market_sounding'  AS data_source,
                r.source_url
              FROM raw_notices r
              JOIN parsed_notices p ON p.notice_id = r.notice_id
             WHERE UPPER(r.category_raw) IN ('ROI', 'RFI', 'EOI',
                                             'REQUEST FOR INFORMATION',
                                             'EXPRESSION OF INTEREST',
                                             'REQUEST OF INTEREST')
               AND (p.days_until_close IS NULL OR p.days_until_close >= 0)
               {sector_where}
             ORDER BY r.close_date ASC NULLS LAST
             LIMIT 20
            """,
            sector_params,
        )
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("renewal_radar market_soundings query failed: %s", exc)
        return []


# ── Merge and filter ──────────────────────────────────────────────────────────

def _merge_and_filter(rows: list[dict], apply_fy_filter: bool = True) -> list[dict]:
    """Dedup by (agency, title-prefix), remove FY-end dates, sort by expiry."""
    seen: dict[str, dict] = {}
    for row in rows:
        ed = _coerce_date(row.get("expiry_date"))
        if ed and apply_fy_filter and _is_fy_end(ed):
            logger.debug("Filtered FY-end date %s for: %s", ed, row.get("title", "")[:50])
            continue
        key = _dedup_key(row.get("agency_name") or "", row.get("title") or "")
        existing = seen.get(key)
        if existing is None:
            seen[key] = row
        else:
            ev = float(existing.get("contract_value") or 0)
            rv = float(row.get("contract_value") or 0)
            if rv > ev:
                seen[key] = row
    return list(seen.values())


# ── Main function ─────────────────────────────────────────────────────────────

def get_renewal_radar(
    user_sectors: Optional[list] = None,
    days_ahead: int = 365,
) -> list[dict]:
    """
    DEPRECATED compatibility wrapper — returns a flat list as before.
    New callers should use get_renewal_pipeline() for the three-tier result.
    """
    pipeline = get_renewal_pipeline(user_sectors=user_sectors, days_ahead=days_ahead)
    result = []
    for tier in ("imminent", "approaching"):
        for row in pipeline.get(tier, []):
            result.append(row)
    # Append market soundings last
    for row in pipeline.get("market_sounding", []):
        result.append(row)
    return result[:10]


def get_renewal_pipeline(
    user_sectors: Optional[list] = None,
    days_ahead: int = 365,
) -> dict:
    """
    Return a three-tier renewal pipeline dict:
      {
        "imminent":       [...],   # explicit end_date within 90 days
        "approaching":    [...],   # explicit end_date 90-180 days
        "market_sounding":[...],   # active ROI/RFI/EOI notices
        "data_note":      str,     # shown when results are sparse
      }
    Each row has: title, agency_name, supplier_name, contract_value,
    expiry_date, sector_tag, window_label, data_source, source_url.
    """
    today  = date.today()
    cutoff = today + timedelta(days=APPROACHING_DAYS)

    sector_where = ""
    sector_params: list = []
    if user_sectors:
        placeholders = ",".join(["%s"] * len(user_sectors))
        sector_where = f"AND sector_tag IN ({placeholders})"
        sector_params = list(user_sectors)

    # Pull award sources
    mbie_rows  = _query_mbie(today, cutoff, sector_where, sector_params)
    gets_rows  = _query_gets_awards(today, cutoff, sector_where, sector_params)
    award_rows = _merge_and_filter(gets_rows + mbie_rows)

    # Split into tiers
    imminent   = []
    approaching = []
    for row in sorted(award_rows, key=lambda r: _coerce_date(r.get("expiry_date")) or date.max):
        ed = _coerce_date(row.get("expiry_date"))
        if not ed:
            continue
        days_left = (ed - today).days
        row["expiry_date"]  = ed
        row["window_label"] = _window_label(ed, today)
        if days_left <= IMMINENT_DAYS:
            imminent.append(row)
        elif days_left <= APPROACHING_DAYS:
            approaching.append(row)

    # Market soundings tier
    sounding_rows = _query_market_soundings(user_sectors)
    market_soundings = []
    for row in sounding_rows:
        ed = _coerce_date(row.get("expiry_date"))
        row["expiry_date"]  = ed
        row["window_label"] = (
            f"Market sounding closes {ed.strftime('%-d %b %Y')}" if ed
            else "Market sounding — close date TBC"
        )
        market_soundings.append(row)

    # Apply sector conflict resolution to all results
    try:
        from sector_classifier import resolve_sector_conflict
        for group in (imminent, approaching, market_soundings):
            for row in group:
                if row.get("sector_tag"):
                    res = resolve_sector_conflict(
                        notice_title=row.get("title") or "",
                        notice_description="",
                        stored_sector=row["sector_tag"],
                    )
                    row["sector_tag"] = res["sector"]
    except Exception as exc:
        logger.debug("Sector resolution in renewal radar failed: %s", exc)

    total = len(imminent) + len(approaching) + len(market_soundings)
    data_note = ""
    if total == 0:
        data_note = (
            "No renewal data currently available. "
            "The pipeline builds incrementally as GETS award notices are processed "
            "and contract durations are enriched."
        )
    elif total < 3:
        data_note = (
            f"Limited data — {total} result{'s' if total != 1 else ''} available. "
            "The pipeline grows as more award notices are ingested and enriched."
        )

    return {
        "imminent":        imminent[:8],
        "approaching":     approaching[:8],
        "market_sounding": market_soundings[:8],
        "data_note":       data_note,
    }
