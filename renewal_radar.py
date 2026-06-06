"""
renewal_radar.py — Identify contracts approaching renewal window.

Sources:
  1. mbie_award_notices: awarded_date is a proxy for contract start.
     Contracts awarded 2–5 years ago with no subsequent award from the same
     agency on a similar title are likely approaching renewal.
  2. contract_awards: end_date directly, if populated.

Returns top 10 by awarded_amount for the given sector list.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

import db
import config

logger = logging.getLogger(__name__)


def get_renewal_radar(
    user_sectors: Optional[list[str]] = None,
    days_ahead: int = 90,
) -> list[dict]:
    """
    Return up to 10 contracts likely approaching renewal.

    Priority:
    1. contract_awards rows with end_date within *days_ahead* days (hard date).
    2. mbie_award_notices awarded 2–5 years ago (soft proxy, no repeat award seen).

    If *user_sectors* is empty/None, all sectors are included.
    """
    today = date.today()
    cutoff = today + timedelta(days=days_ahead)
    results: list[dict] = []

    # ── Source 1: contract_awards with explicit end_date ──────────────────────
    try:
        sector_filter = ""
        params_ca: list = [today.isoformat(), cutoff.isoformat()]
        if user_sectors:
            placeholders = ",".join(["%s"] * len(user_sectors))
            sector_filter = f"AND ca.sector_tag IN ({placeholders})"
            params_ca += user_sectors

        rows = db.fetchall(
            f"""
            SELECT ca.title,
                   o.name           AS agency_name,
                   ca.contract_value,
                   ca.end_date,
                   ca.sector_tag,
                   'end_date'       AS source
              FROM contract_awards ca
              LEFT JOIN organisations o ON o.org_id = ca.agency_org_id
             WHERE ca.end_date BETWEEN %s AND %s
               {sector_filter}
             ORDER BY ca.contract_value DESC NULLS LAST
             LIMIT 10
            """,
            tuple(params_ca),
        )
        results.extend([dict(r) for r in rows])
    except Exception as exc:
        logger.warning("renewal_radar contract_awards: %s", exc)

    # ── Source 2: MBIE awards 2–5 years old (if we have room) ────────────────
    if len(results) < 10:
        try:
            two_years_ago  = (today - timedelta(days=730)).isoformat()
            five_years_ago = (today - timedelta(days=1825)).isoformat()

            sector_filter2 = ""
            params_mbie: list = [five_years_ago, two_years_ago]
            if user_sectors:
                placeholders = ",".join(["%s"] * len(user_sectors))
                sector_filter2 = f"AND c.sector_tag IN ({placeholders})"
                params_mbie += user_sectors

            rows2 = db.fetchall(
                f"""
                SELECT n.title,
                       n.posting_agency            AS agency_name,
                       n.awarded_amount            AS contract_value,
                       n.awarded_date::date        AS end_date,
                       c.sector_tag,
                       'mbie_proxy'                AS source
                  FROM mbie_award_notices n
                  JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
                 WHERE n.awarded_date BETWEEN %s AND %s
                   AND n.is_awarded IS TRUE
                   {sector_filter2}
                 ORDER BY n.awarded_amount DESC NULLS LAST
                 LIMIT %s
                """,
                tuple(params_mbie) + (10 - len(results),),
            )
            results.extend([dict(r) for r in rows2])
        except Exception as exc:
            logger.warning("renewal_radar mbie_awards: %s", exc)

    return results[:10]
