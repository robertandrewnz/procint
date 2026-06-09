"""
renewal_radar.py — Contract Expiry Radar.

Shows contracts approaching estimated re-procurement. When a contract has an
explicitly stated term, that is used (labelled "Confirmed term"). When no term
is stated, the sector's typical duration from INFERRED_CONTRACT_DURATIONS is
applied (labelled "Estimated — typical [sector] contract duration").

Filters applied:
  • Expiry dates falling on 30 June or 31 December are suppressed (FY artefacts)
  • Expiry dates more than 18 months in the past are excluded (likely re-tendered)
  • Only expiries within the next 12 months are shown
  • Top 10 results ordered by expiry ascending

Tiers:
  • imminent    — expiry within next 90 days
  • approaching — expiry 90–365 days out

Note on market soundings: ROI/RFI/EOI notices are excluded — they appear in the
main watchlist, not here.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Optional

import db
from config import INFERRED_CONTRACT_DURATIONS

logger = logging.getLogger(__name__)

# Radar window (days)
IMMINENT_DAYS   =  90
RADAR_DAYS      = 365   # full 12-month forward window

# How far in the past an inferred expiry can be before we drop it
STALE_MONTHS    = 18

# Financial year end dates to suppress (month, day)
_FY_END_DATES = {(6, 30), (12, 31)}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_fy_end(d: date) -> bool:
    return (d.month, d.day) in _FY_END_DATES


def _add_months(d: date, months: int) -> date:
    import calendar
    month = d.month - 1 + months
    year  = d.year + month // 12
    month = month % 12 + 1
    day   = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _inferred_duration(sector_tag: Optional[str]) -> Optional[int]:
    """Return the typical duration (months) for a sector, or None if unknown."""
    if not sector_tag:
        return INFERRED_CONTRACT_DURATIONS["other"]["typical"]
    t = INFERRED_CONTRACT_DURATIONS.get(sector_tag)
    if t:
        return t["typical"]
    # Case-insensitive fallback
    lower = sector_tag.lower()
    for k, v in INFERRED_CONTRACT_DURATIONS.items():
        if k.lower() == lower:
            return v["typical"]
    return INFERRED_CONTRACT_DURATIONS["other"]["typical"]


def _window_label(expiry: date, today: date) -> str:
    delta = (expiry - today).days
    if delta < 0:
        months_ago = round(abs(delta) / 30.44)
        return f"Expired ~{months_ago} month{'s' if months_ago != 1 else ''} ago"
    if delta == 0:
        return f"Expires today — {expiry.strftime('%-d %b %Y')}"
    if delta <= 14:
        return f"Opens now — expires {expiry.strftime('%-d %b %Y')}"
    if delta <= 45:
        return f"Opens this month — expires {expiry.strftime('%-d %b %Y')}"
    months = round(delta / 30.44)
    if months == 1:
        return f"Opens in 1 month — expires {expiry.strftime('%-d %b %Y')}"
    if months <= 12:
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

def _query_mbie_confirmed(
    today: date,
    stale_cutoff: date,
    radar_cutoff: date,
    sector_where: str,
    sector_params: list,
) -> list[dict]:
    """
    MBIE records with BOTH awarded_date AND contract_duration_months present.
    Expiry = contract_expiry (pre-computed) or awarded_date + duration.
    """
    try:
        rows = db.fetchall(
            f"""
            SELECT
                m.rfx_id            AS source_id,
                m.title,
                m.posting_agency    AS agency_name,
                s.business_name     AS supplier_name,
                m.awarded_amount    AS contract_value,
                m.awarded_date,
                m.contract_duration_months AS duration_months,
                COALESCE(m.contract_expiry,
                    m.awarded_date + (m.contract_duration_months || ' months')::interval
                )::date             AS expiry_date,
                m.sector_tag,
                'mbie'              AS data_source,
                'confirmed'         AS term_source
              FROM mbie_award_notices m
              LEFT JOIN LATERAL (
                  SELECT business_name FROM mbie_award_suppliers
                  WHERE rfx_id = m.rfx_id
                  LIMIT 1
              ) s ON true
             WHERE m.contract_duration_months IS NOT NULL
               AND m.awarded_date IS NOT NULL
               AND COALESCE(m.contract_expiry,
                   m.awarded_date + (m.contract_duration_months || ' months')::interval
               )::date >= %s
               AND COALESCE(m.contract_expiry,
                   m.awarded_date + (m.contract_duration_months || ' months')::interval
               )::date <= %s
               {sector_where}
             ORDER BY expiry_date ASC
             LIMIT 100
            """,
            (stale_cutoff.isoformat(), radar_cutoff.isoformat(), *sector_params),
        )
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("renewal_radar mbie confirmed query failed: %s", exc)
        return []


def _query_mbie_inferred(
    today: date,
    stale_cutoff: date,
    radar_cutoff: date,
    sector_where: str,
    sector_params: list,
    sectors_requested: Optional[list],
) -> list[dict]:
    """
    MBIE records WITHOUT contract_duration_months — infer expiry from sector typical.
    We pull all records without a duration and compute expiry in Python so we can
    apply per-sector typical values from INFERRED_CONTRACT_DURATIONS.
    """
    try:
        rows = db.fetchall(
            f"""
            SELECT
                m.rfx_id            AS source_id,
                m.title,
                m.posting_agency    AS agency_name,
                s.business_name     AS supplier_name,
                m.awarded_amount    AS contract_value,
                m.awarded_date,
                m.sector_tag,
                'mbie'              AS data_source,
                'inferred'          AS term_source
              FROM mbie_award_notices m
              LEFT JOIN LATERAL (
                  SELECT business_name FROM mbie_award_suppliers
                  WHERE rfx_id = m.rfx_id
                  LIMIT 1
              ) s ON true
             WHERE m.contract_duration_months IS NULL
               AND m.awarded_date IS NOT NULL
               {sector_where}
             ORDER BY m.awarded_date DESC
             LIMIT 5000
            """,
            sector_params,
        )
        result = []
        for row in rows:
            r = dict(row)
            awarded = _coerce_date(r.get("awarded_date"))
            if not awarded:
                continue
            typical = _inferred_duration(r.get("sector_tag"))
            if typical is None:
                continue
            expiry = _add_months(awarded, typical)
            # Apply window filters in Python
            if expiry < stale_cutoff or expiry > radar_cutoff:
                continue
            r["expiry_date"]    = expiry
            r["duration_months"] = typical
            result.append(r)
        logger.debug("renewal_radar inferred: %d rows after window filter", len(result))
        return result
    except Exception as exc:
        logger.warning("renewal_radar mbie inferred query failed: %s", exc)
        return []


def _query_gets_awards(
    today: date,
    stale_cutoff: date,
    radar_cutoff: date,
    sector_where: str,
    sector_params: list,
) -> list[dict]:
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
                'confirmed'         AS term_source
              FROM contract_awards ca
              LEFT JOIN organisations o_a ON o_a.org_id = ca.agency_org_id
              LEFT JOIN organisations o_s ON o_s.org_id = ca.supplier_org_id
             WHERE ca.end_date IS NOT NULL
               AND ca.end_date >= %s
               AND ca.end_date <= %s
               {sector_where}
             ORDER BY ca.end_date ASC
             LIMIT 50
            """,
            (stale_cutoff.isoformat(), radar_cutoff.isoformat(), *sector_params),
        )
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("renewal_radar gets query failed: %s", exc)
        return []


# ── Merge and filter ──────────────────────────────────────────────────────────

def _merge_and_filter(rows: list[dict]) -> list[dict]:
    """Dedup by (agency, title-prefix), remove FY-end dates, sort by expiry."""
    seen: dict[str, dict] = {}
    dropped_fy = 0
    for row in rows:
        ed = _coerce_date(row.get("expiry_date"))
        if not ed:
            continue
        if _is_fy_end(ed):
            dropped_fy += 1
            logger.debug("Filtered FY-end date %s for: %s", ed, row.get("title", "")[:50])
            continue
        key = _dedup_key(row.get("agency_name") or "", row.get("title") or "")
        existing = seen.get(key)
        if existing is None:
            seen[key] = row
        else:
            # Prefer confirmed terms over inferred; otherwise prefer higher value
            existing_confirmed = existing.get("term_source") == "confirmed"
            row_confirmed      = row.get("term_source") == "confirmed"
            if row_confirmed and not existing_confirmed:
                seen[key] = row
            elif not row_confirmed and existing_confirmed:
                pass
            else:
                ev = float(existing.get("contract_value") or 0)
                rv = float(row.get("contract_value") or 0)
                if rv > ev:
                    seen[key] = row
    if dropped_fy:
        logger.debug("Suppressed %d FY-end-date records", dropped_fy)
    return sorted(seen.values(), key=lambda r: _coerce_date(r.get("expiry_date")) or date.max)


# ── Main function ─────────────────────────────────────────────────────────────

def get_renewal_radar(
    user_sectors: Optional[list] = None,
    days_ahead: int = 365,
) -> list[dict]:
    """Compatibility wrapper — returns a flat list."""
    pipeline = get_renewal_pipeline(user_sectors=user_sectors, days_ahead=days_ahead)
    result = []
    for tier in ("imminent", "approaching"):
        result.extend(pipeline.get(tier, []))
    return result[:10]


def get_renewal_pipeline(
    user_sectors: Optional[list] = None,
    days_ahead: int = 365,
) -> dict:
    """
    Return Contract Expiry Radar results — two tiers, expiry within 12 months.

    Includes both confirmed-term contracts (contract_duration_months present) and
    inferred-term contracts (expiry calculated from sector typical duration).

    Result dict:
      {
        "imminent":    [...],   # expiry within 90 days
        "approaching": [...],   # expiry 90–365 days
        "data_note":   str,
      }
    Each row: title, agency_name, supplier_name, contract_value,
    awarded_date, expiry_date, duration_months, sector_tag, data_source,
    term_source ('confirmed'|'inferred'), window_label.
    """
    today        = date.today()
    radar_cutoff = today + timedelta(days=RADAR_DAYS)
    stale_cutoff = _add_months(today, -STALE_MONTHS)

    sector_where  = ""
    sector_params: list = []
    if user_sectors:
        placeholders  = ",".join(["%s"] * len(user_sectors))
        sector_where  = f"AND sector_tag IN ({placeholders})"
        sector_params = list(user_sectors)

    logger.debug(
        "renewal_radar: today=%s, stale_cutoff=%s, radar_cutoff=%s, sectors=%s",
        today, stale_cutoff, radar_cutoff, user_sectors,
    )

    mbie_confirmed = _query_mbie_confirmed(
        today, stale_cutoff, radar_cutoff, sector_where, sector_params
    )
    mbie_inferred  = _query_mbie_inferred(
        today, stale_cutoff, radar_cutoff, sector_where, sector_params,
        user_sectors,
    )
    gets_rows = _query_gets_awards(
        today, stale_cutoff, radar_cutoff, sector_where, sector_params
    )

    logger.info(
        "renewal_radar raw counts: confirmed=%d, inferred=%d, gets=%d",
        len(mbie_confirmed), len(mbie_inferred), len(gets_rows),
    )

    all_rows   = gets_rows + mbie_confirmed + mbie_inferred
    award_rows = _merge_and_filter(all_rows)

    # Annotate with window_label and split into tiers
    imminent    = []
    approaching = []
    for row in award_rows:
        ed = _coerce_date(row.get("expiry_date"))
        if not ed:
            continue
        row["expiry_date"]  = ed
        row["window_label"] = _window_label(ed, today)
        days_left = (ed - today).days
        if days_left <= IMMINENT_DAYS:
            imminent.append(row)
        else:
            approaching.append(row)

    # Cap tiers and pick top 10 total
    imminent    = imminent[:5]
    approaching = approaching[:5]
    total       = len(imminent) + len(approaching)

    logger.info(
        "renewal_radar final: imminent=%d, approaching=%d (total=%d)",
        len(imminent), len(approaching), total,
    )

    if total == 0:
        data_note = (
            "No contracts with calculable expiry dates found in the next 12 months "
            "for your sectors. Try widening your sector preferences."
        )
    else:
        # Count confirmed vs inferred
        n_confirmed = sum(
            1 for r in imminent + approaching
            if r.get("term_source") == "confirmed"
        )
        n_inferred  = total - n_confirmed
        if n_inferred > 0:
            data_note = (
                "Showing contracts approaching estimated re-procurement. "
                "Expiry dates marked '~ Estimated' are calculated from typical contract "
                "durations for this sector — not confirmed published terms. "
                "Treat as a market signal to monitor, not a confirmed tender date."
            )
        else:
            data_note = ""

    return {
        "imminent":    imminent,
        "approaching": approaching,
        "data_note":   data_note,
    }
