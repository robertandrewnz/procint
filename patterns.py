"""
Layer 2 — Longitudinal pattern detection.

Detects and flags strategically significant patterns that emerge over time:

  approaching_renewal — contracts whose end_date falls within the renewal
      window, signalling an upcoming re-tender opportunity.

  procurement_surge — agencies with significantly more notices than usual
      in the recent lookback period, signalling increased procurement activity.

  win_streak — suppliers who have won N or more contracts in the same sector
      recently, indicating momentum and incumbent risk.

  sector_spike — sectors with unusually high notice volume in the recent
      period compared to their baseline.

  loss_streak — suppliers who have appeared as likely bidders multiple times
      in a sector but have no recorded wins (possible intelligence gap or
      persistent incumbent problem).

Pattern flags are stored in pattern_flags with an expires_at date so stale
flags are not shown in the Market Intelligence section.
"""
import logging
from datetime import date, timedelta
from typing import Optional

import config
import db

logger = logging.getLogger(__name__)


# ── Approaching renewals ──────────────────────────────────────────────────────

def detect_renewal_opportunities() -> list[dict]:
    """
    Find contracts whose end_date falls within RENEWAL_WINDOW_DAYS.
    These represent likely upcoming re-tenders — high-value intelligence.
    """
    window_end = date.today() + timedelta(days=config.RENEWAL_WINDOW_DAYS)

    rows = db.fetchall(
        """
        SELECT ca.award_id, ca.title, ca.end_date, ca.contract_value,
               ca.duration_months, ca.sector_tag,
               a.name AS agency_name,
               s.name AS supplier_name
          FROM contract_awards ca
          LEFT JOIN organisations a ON a.org_id = ca.agency_org_id
          LEFT JOIN organisations s ON s.org_id = ca.supplier_org_id
         WHERE ca.end_date IS NOT NULL
           AND ca.end_date BETWEEN CURRENT_DATE AND %s
         ORDER BY ca.end_date ASC
        """,
        (window_end,),
    )

    flags = []
    for r in rows:
        days_left = (r["end_date"] - date.today()).days if r["end_date"] else None
        severity = "high" if days_left is not None and days_left <= 30 else "medium"
        value_str = (
            f"${r['contract_value']:,.0f}" if r.get("contract_value") else "unknown value"
        )
        description = (
            f"Contract expiring in {days_left} days: \"{r['title'][:80]}\" "
            f"({r.get('agency_name','?')} → {r.get('supplier_name','?')}, "
            f"{value_str}) — likely re-tender opportunity."
        )
        flag = _store_flag(
            flag_type="approaching_renewal",
            org_id=None,
            sector_tag=r.get("sector_tag"),
            notice_id=None,
            award_id=r["award_id"],
            description=description,
            severity=severity,
            expires_at=r["end_date"] + timedelta(days=30) if r["end_date"] else None,
        )
        flags.append(flag)

    logger.info("Detected %d approaching renewals", len(flags))
    return flags


# ── Procurement surges ────────────────────────────────────────────────────────

def detect_procurement_surges() -> list[dict]:
    """
    Detect agencies that have published significantly more notices than usual
    in the recent lookback period (SURGE_LOOKBACK_DAYS).

    Uses a simple heuristic: if recent_notices >= 2 * historical_rate,
    flag as a surge.
    """
    lookback = config.SURGE_LOOKBACK_DAYS
    rows = db.fetchall(
        """
        WITH baseline AS (
            SELECT r.agency,
                   COUNT(*) AS total_notices,
                   COUNT(*) FILTER (
                       WHERE r.fetched_at >= NOW() - (%s || ' days')::INTERVAL
                   ) AS recent_notices
              FROM raw_notices r
             WHERE r.agency IS NOT NULL
             GROUP BY r.agency
            HAVING COUNT(*) >= 5
        )
        SELECT b.agency,
               b.total_notices,
               b.recent_notices,
               ROUND(b.total_notices::NUMERIC / GREATEST(
                   EXTRACT(EPOCH FROM NOW() - MIN(r.fetched_at)) / 86400, 1
               ) * %s, 2) AS expected_recent
          FROM baseline b
          JOIN raw_notices r ON r.agency = b.agency
         GROUP BY b.agency, b.total_notices, b.recent_notices
        HAVING b.recent_notices::FLOAT > 1.8 * (
            b.total_notices::FLOAT / GREATEST(
                EXTRACT(EPOCH FROM NOW() - MIN(r.fetched_at)) / 86400 + 1, 1
            ) * %s
        )
         ORDER BY b.recent_notices DESC
         LIMIT 10
        """,
        (lookback, lookback, lookback),
    )

    flags = []
    for r in rows:
        org_id = _org_id_for_agency(r["agency"])
        description = (
            f"Procurement surge: {r['agency']} has published {r['recent_notices']} notices "
            f"in the last {lookback} days — above recent average. "
            f"Increased procurement activity may indicate budget spend, restructure, or new programme."
        )
        flag = _store_flag(
            flag_type="procurement_surge",
            org_id=org_id,
            sector_tag=None,
            description=description,
            severity="medium",
            expires_at=None,
        )
        flags.append(flag)

    logger.info("Detected %d procurement surges", len(flags))
    return flags


# ── Win streaks ───────────────────────────────────────────────────────────────

def detect_win_streaks() -> list[dict]:
    """
    Detect suppliers with WIN_STREAK_THRESHOLD or more wins in the same sector
    across recent awards. Signals strong incumbency or sector dominance.
    """
    rows = db.fetchall(
        """
        SELECT o.name AS supplier_name, ca.sector_tag,
               COUNT(*) AS wins,
               SUM(ca.contract_value) AS total_value,
               MAX(ca.award_date) AS last_win,
               o.org_id
          FROM contract_awards ca
          JOIN organisations o ON o.org_id = ca.supplier_org_id
         WHERE ca.sector_tag IS NOT NULL
           AND ca.award_date >= CURRENT_DATE - INTERVAL '365 days'
         GROUP BY o.org_id, o.name, ca.sector_tag
        HAVING COUNT(*) >= %s
         ORDER BY wins DESC
         LIMIT 20
        """,
        (config.WIN_STREAK_THRESHOLD,),
    )

    flags = []
    for r in rows:
        value_str = f"${r['total_value']:,.0f}" if r.get("total_value") else "unknown value"
        description = (
            f"Win streak: {r['supplier_name']} has won {r['wins']} {r['sector_tag']} "
            f"contracts in the past 12 months ({value_str} total). "
            f"Strong incumbent risk in this sector."
        )
        flag = _store_flag(
            flag_type="win_streak",
            org_id=r["org_id"],
            sector_tag=r["sector_tag"],
            description=description,
            severity="high" if r["wins"] >= config.WIN_STREAK_THRESHOLD + 2 else "medium",
            expires_at=None,
        )
        flags.append(flag)

    logger.info("Detected %d win streaks", len(flags))
    return flags


# ── Sector spikes ─────────────────────────────────────────────────────────────

def detect_sector_spikes() -> list[dict]:
    """
    Detect sectors with unusually high notice volume in the recent period.
    """
    lookback = config.SURGE_LOOKBACK_DAYS
    rows = db.fetchall(
        """
        SELECT p.sector_tag,
               COUNT(*) FILTER (
                   WHERE r.fetched_at >= NOW() - (%s || ' days')::INTERVAL
               ) AS recent_count,
               COUNT(*) AS total_count
          FROM parsed_notices p
          JOIN raw_notices r ON r.notice_id = p.notice_id
         WHERE p.sector_tag IS NOT NULL
           AND p.sector_tag != 'other'
         GROUP BY p.sector_tag
        HAVING COUNT(*) >= 10
           AND COUNT(*) FILTER (
               WHERE r.fetched_at >= NOW() - (%s || ' days')::INTERVAL
           )::FLOAT > 0.4 * COUNT(*)
         ORDER BY recent_count DESC
        """,
        (lookback, lookback),
    )

    # Total notices across ALL sectors — used as the denominator so the
    # percentage reflects share of the market, not share of that sector
    # (dividing by the same sector's total would always approach 100%).
    total_all = db.fetchone(
        """
        SELECT COUNT(*) AS n
          FROM parsed_notices p
          JOIN raw_notices r ON r.notice_id = p.notice_id
         WHERE r.fetched_at >= NOW() - (%s || ' days')::INTERVAL
        """,
        (lookback,),
    )
    total_all_n = int((total_all or {}).get("n") or 1)

    flags = []
    for r in rows:
        # Percentage of ALL notices in the window, not percentage of this sector
        pct = round(100 * r["recent_count"] / total_all_n)
        description = (
            f"Sector spike: {r['sector_tag']} accounts for {r['recent_count']} of "
            f"{total_all_n} notices in the last {lookback} days "
            f"({pct}% of all notices). "
            f"Unusual market activity — investigate drivers."
        )
        flag = _store_flag(
            flag_type="sector_spike",
            org_id=None,
            sector_tag=r["sector_tag"],
            description=description,
            severity="medium",
            expires_at=None,
        )
        flags.append(flag)

    logger.info("Detected %d sector spikes", len(flags))
    return flags


# ── Loss streaks ──────────────────────────────────────────────────────────────

def detect_loss_streaks() -> list[dict]:
    """
    Detect suppliers that appear frequently as likely bidders in a sector
    but have no recorded wins — possible intelligence gap or persistent
    incumbent disadvantage.
    """
    rows = db.fetchall(
        """
        SELECT bp.firm_name, p.sector_tag,
               COUNT(DISTINCT bp.notice_id) AS bid_appearances
          FROM bidder_pool bp
          JOIN parsed_notices p ON p.notice_id = bp.notice_id
         WHERE p.sector_tag IS NOT NULL
           AND p.sector_tag != 'other'
         GROUP BY bp.firm_name, p.sector_tag
        HAVING COUNT(DISTINCT bp.notice_id) >= 4
         ORDER BY bid_appearances DESC
         LIMIT 30
        """,
    )

    flags = []
    for r in rows:
        # Check if they have any wins in this sector
        wins = db.fetchone(
            """
            SELECT COUNT(*) as n
              FROM contract_awards ca
              JOIN organisations o ON o.org_id = ca.supplier_org_id
             WHERE o.name = %s AND ca.sector_tag = %s
            """,
            (r["firm_name"], r["sector_tag"]),
        )
        if wins and wins["n"] > 0:
            continue  # They have wins, skip

        description = (
            f"Persistent non-winner: {r['firm_name']} has appeared as a likely bidder "
            f"for {r['bid_appearances']} {r['sector_tag']} notices with no recorded wins. "
            f"Could indicate strong incumbents, positioning issues, or data gap in awards."
        )
        flag = _store_flag(
            flag_type="loss_streak",
            org_id=None,
            sector_tag=r["sector_tag"],
            description=description,
            severity="low",
            expires_at=None,
        )
        flags.append(flag)

    logger.info("Detected %d loss streaks", len(flags))
    return flags


# ── Storage helpers ───────────────────────────────────────────────────────────

def _org_id_for_agency(agency_name: str) -> Optional[int]:
    from organisations import resolve_alias
    return resolve_alias(agency_name)


def _store_flag(
    flag_type: str,
    description: str,
    severity: str,
    org_id: Optional[int] = None,
    sector_tag: Optional[str] = None,
    notice_id: Optional[str] = None,
    award_id: Optional[int] = None,
    expires_at=None,
) -> dict:
    row = db.fetchone(
        """
        INSERT INTO pattern_flags
            (flag_type, org_id, sector_tag, notice_id, award_id,
             description, severity, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING flag_id, flag_type, description, severity, detected_at
        """,
        (flag_type, org_id, sector_tag, notice_id, award_id,
         description, severity, expires_at),
    )
    return dict(row)


def expire_old_flags() -> int:
    """Remove flags that have passed their expires_at date."""
    # Count first, then delete (RETURNING COUNT(*) is not valid in PostgreSQL)
    result = db.fetchone(
        """
        SELECT COUNT(*) as n FROM pattern_flags
         WHERE expires_at IS NOT NULL AND expires_at < CURRENT_DATE
        """
    )
    count = result["n"] if result else 0
    if count:
        db.execute(
            "DELETE FROM pattern_flags WHERE expires_at IS NOT NULL AND expires_at < CURRENT_DATE"
        )
        logger.info("Expired %d old pattern flags", count)
    return count


# ── Main entry point ──────────────────────────────────────────────────────────

def run_pattern_detection() -> list[dict]:
    """
    Run all pattern detectors. Returns combined list of new flags.
    """
    logger.info("Starting longitudinal pattern detection")
    expire_old_flags()

    all_flags = []
    for detector, name in [
        (detect_renewal_opportunities, "renewal opportunities"),
        (detect_procurement_surges,    "procurement surges"),
        (detect_win_streaks,           "win streaks"),
        (detect_sector_spikes,         "sector spikes"),
        (detect_loss_streaks,          "loss streaks"),
    ]:
        try:
            flags = detector()
            all_flags.extend(flags)
        except Exception as exc:
            logger.warning("Pattern detector '%s' failed: %s", name, exc)

    logger.info("Pattern detection complete: %d flags generated", len(all_flags))
    return all_flags


def get_active_flags(
    flag_types: Optional[list[str]] = None,
    severity: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Fetch active (non-expired) pattern flags."""
    conditions = ["(expires_at IS NULL OR expires_at >= CURRENT_DATE)"]
    params: list = []

    if flag_types:
        placeholders = ", ".join(["%s"] * len(flag_types))
        conditions.append(f"flag_type IN ({placeholders})")
        params.extend(flag_types)

    if severity:
        conditions.append("severity = %s")
        params.append(severity)

    where = " AND ".join(conditions)
    params.append(limit)

    return db.fetchall(
        f"""
        SELECT pf.flag_id, pf.flag_type, pf.description, pf.severity,
               pf.detected_at, pf.expires_at, pf.sector_tag,
               o.name AS org_name
          FROM pattern_flags pf
          LEFT JOIN organisations o ON o.org_id = pf.org_id
         WHERE {where}
         ORDER BY
             CASE pf.severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
             pf.detected_at DESC
         LIMIT %s
        """,
        params,
    )
