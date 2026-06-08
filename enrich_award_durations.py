"""
enrich_award_durations.py — Backfill contract duration and sector tags.

Reads mbie_award_notices and contract_awards, extracts contract_duration_months
from title / overview text, computes contract_expiry, and tags sector using the
same keyword classifier as Layer 1.

Run after migration 006:
    python enrich_award_durations.py [--mbie] [--awards] [--all] [--limit N]

Safe to re-run — uses UPDATE WHERE contract_expiry IS NULL (or --force flag to
re-process all rows).
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date, timedelta
from typing import Optional

import db

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)


# ── Duration extraction ───────────────────────────────────────────────────────

# Ordered from most precise (year-range with both years) to least
_YEAR_RANGE    = re.compile(r'\b(20\d{2})[-/](20\d{2})\b')
_FISCAL_YEAR   = re.compile(r'\b(20\d{2})/((\d{2}))\b')
_N_YEARS       = re.compile(r'\b(\d{1,2})\s*[-–]?\s*year', re.IGNORECASE)
_N_MONTHS      = re.compile(r'\b(\d{1,3})\s*[-–]?\s*month', re.IGNORECASE)
_UNTIL_YEAR    = re.compile(r'(?:until|to|expir(?:es?|y)|through)\s+(20[2-9]\d)', re.IGNORECASE)


def _expiry_from_year(end_year: int) -> date:
    """Return a representative expiry for an end year (30 June = end of NZ fiscal year)."""
    return date(end_year, 6, 30)


def extract_duration(
    title: str,
    overview: str,
    awarded_date: Optional[date],
) -> tuple[Optional[int], Optional[date]]:
    """
    Return (duration_months, contract_expiry) from title and overview text.

    Priority order:
      1. Year range in title: "2025-2028" → 36 months, expiry=2028-06-30
      2. Fiscal-year shorthand: "2025/26" → 12 months, expiry=2026-06-30
      3. "N years" in title or first 600 chars of overview
      4. "N months" in title or first 600 chars of overview
      5. "until YYYY" / "expires YYYY" in title or overview

    Returns (None, None) if nothing parseable is found.
    """
    title    = title    or ""
    overview = (overview or "")[:600]

    # ── 1. Year range in title ─────────────────────────────────────────────────
    m = _YEAR_RANGE.search(title)
    if m:
        start_y, end_y = int(m.group(1)), int(m.group(2))
        if end_y > start_y and (end_y - start_y) <= 20:
            expiry = _expiry_from_year(end_y)
            if awarded_date:
                # months from award to expiry
                months = (end_y - awarded_date.year) * 12 + (6 - awarded_date.month)
                if months > 0:
                    return months, expiry
            return (end_y - start_y) * 12, expiry

    # ── 2. Fiscal-year shorthand in title: "2025/26" ───────────────────────────
    m = _FISCAL_YEAR.search(title)
    if m:
        start_y = int(m.group(1))
        short   = int(m.group(2))
        # "2025/26" → end year = 2026 if short == (start_y+1) % 100
        expected_short = (start_y + 1) % 100
        if short == expected_short:
            end_y = start_y + 1
            expiry = _expiry_from_year(end_y)
            months = 12
            if awarded_date:
                months = max(6, (end_y - awarded_date.year) * 12 + (6 - awarded_date.month))
            return months, expiry

    # ── 3–5. Search title then overview snippet ────────────────────────────────
    for text in (title, overview):
        # "N years"
        m = _N_YEARS.search(text)
        if m:
            years = int(m.group(1))
            if 1 <= years <= 20:
                months = years * 12
                expiry = None
                if awarded_date:
                    try:
                        expiry = awarded_date.replace(year=awarded_date.year + years)
                    except ValueError:
                        expiry = awarded_date + timedelta(days=years * 365)
                return months, expiry

        # "N months"
        m = _N_MONTHS.search(text)
        if m:
            months = int(m.group(1))
            if 6 <= months <= 240:
                expiry = None
                if awarded_date:
                    end_y = awarded_date.year + months // 12
                    end_mo = awarded_date.month + months % 12
                    if end_mo > 12:
                        end_y += 1
                        end_mo -= 12
                    try:
                        expiry = awarded_date.replace(year=end_y, month=end_mo)
                    except ValueError:
                        expiry = awarded_date + timedelta(days=months * 30)
                return months, expiry

        # "until 2028" / "expires 2029"
        m = _UNTIL_YEAR.search(text)
        if m:
            end_y = int(m.group(1))
            expiry = _expiry_from_year(end_y)
            if awarded_date and end_y > awarded_date.year:
                months = (end_y - awarded_date.year) * 12
                return months, expiry

    return None, None


# ── Sector tagging ────────────────────────────────────────────────────────────

def _tag_sector(title: str, overview: str) -> str:
    """
    Classify to one of the platform sectors using the same keyword table as
    parsing.classify_sector().  Returns 'other' if no match.
    """
    try:
        from parsing import classify_sector
        return classify_sector(title or "", "", (overview or "")[:400])
    except Exception:
        pass

    # Fallback: inline keyword table if parsing import fails
    import config
    combined = (title + " " + (overview or "")).lower()
    best = "other"
    best_count = 0
    for sector, kws in config.SECTOR_KEYWORDS.items():
        count = sum(1 for kw in kws if kw.lower() in combined)
        if count > best_count:
            best_count = count
            best = sector
    return best


# ── MBIE enrichment ───────────────────────────────────────────────────────────

def enrich_mbie(force: bool = False, limit: Optional[int] = None) -> dict:
    """
    Backfill contract_duration_months, contract_expiry, sector_tag on
    mbie_award_notices.

    Processes rows where awarded_date IS NOT NULL (needed for expiry calc).
    If force=False, skips rows that already have contract_expiry set.
    """
    where = "WHERE awarded_date IS NOT NULL"
    if not force:
        where += " AND contract_expiry IS NULL"
    limit_sql = f"LIMIT {int(limit)}" if limit else ""

    rows = db.fetchall(
        f"""
        SELECT rfx_id, title, overview, awarded_date
          FROM mbie_award_notices
          {where}
          ORDER BY awarded_date DESC
          {limit_sql}
        """
    )
    logger.info("MBIE: processing %d rows", len(rows))

    updated = skipped = errors = 0
    for row in rows:
        try:
            duration, expiry = extract_duration(
                row["title"], row["overview"], row["awarded_date"]
            )
            sector = _tag_sector(row["title"], row["overview"])

            if duration or sector != "other":
                db.execute(
                    """
                    UPDATE mbie_award_notices
                       SET contract_duration_months = %s,
                           contract_expiry          = %s,
                           sector_tag               = %s
                     WHERE rfx_id = %s
                    """,
                    (duration, expiry, sector if sector != "other" else None,
                     row["rfx_id"]),
                )
                updated += 1
            else:
                skipped += 1
        except Exception as exc:
            logger.debug("MBIE %s error: %s", row["rfx_id"], exc)
            errors += 1

    logger.info("MBIE enrichment: %d updated, %d skipped, %d errors", updated, skipped, errors)
    return {"updated": updated, "skipped": skipped, "errors": errors}


# ── contract_awards backfill ──────────────────────────────────────────────────

def enrich_contract_awards(force: bool = False, limit: Optional[int] = None) -> dict:
    """
    Backfill sector_tag on contract_awards.  Also cleans up bogus contract_value
    rows where the value == gets_notice_id (a known parse bug from unauthenticated
    detail page fetches).

    Also re-extracts duration from title where duration_months IS NULL.
    """
    where = "WHERE title IS NOT NULL"
    if not force:
        where += " AND (sector_tag IS NULL OR duration_months IS NULL)"
    limit_sql = f"LIMIT {int(limit)}" if limit else ""

    rows = db.fetchall(
        f"""
        SELECT award_id, gets_notice_id, title, description,
               award_date, contract_value, duration_months
          FROM contract_awards
          {where}
          ORDER BY award_date DESC NULLS LAST
          {limit_sql}
        """
    )
    logger.info("contract_awards: processing %d rows", len(rows))

    updated = errors = 0
    for row in rows:
        try:
            sector = _tag_sector(row["title"], row["description"])

            # Detect and nullify bogus contract_value (== notice_id numeric)
            val = row.get("contract_value")
            notice_numeric = None
            try:
                notice_numeric = float(row["gets_notice_id"])
            except (TypeError, ValueError):
                pass
            clean_value = None if (val and notice_numeric and float(val) == notice_numeric) else val

            # Extract duration from title if missing
            duration = row.get("duration_months")
            expiry   = None
            if not duration:
                duration, expiry = extract_duration(
                    row["title"], row["description"], row.get("award_date")
                )

            db.execute(
                """
                UPDATE contract_awards
                   SET sector_tag       = %s,
                       contract_value   = %s,
                       duration_months  = COALESCE(%s, duration_months),
                       end_date         = COALESCE(%s, end_date)
                 WHERE award_id = %s
                """,
                (sector if sector != "other" else None,
                 clean_value, duration, expiry, row["award_id"]),
            )
            updated += 1
        except Exception as exc:
            logger.debug("award %s error: %s", row.get("award_id"), exc)
            errors += 1

    logger.info("contract_awards enrichment: %d updated, %d errors", updated, errors)
    return {"updated": updated, "errors": errors}


# ── Summary stats ─────────────────────────────────────────────────────────────

def print_stats() -> None:
    today = date.today()
    cutoff = today.replace(year=today.year + 1)

    mbie = db.fetchone("""
        SELECT
            COUNT(*)                                          AS total,
            COUNT(contract_duration_months)                  AS has_duration,
            COUNT(contract_expiry)                           AS has_expiry,
            COUNT(CASE WHEN contract_expiry BETWEEN %s AND %s THEN 1 END) AS expiry_12m,
            COUNT(sector_tag)                                AS has_sector
        FROM mbie_award_notices
    """, (today.isoformat(), cutoff.isoformat()))
    print("mbie_award_notices:")
    for k, v in (mbie or {}).items():
        print(f"  {k}: {v}")

    awards = db.fetchone("""
        SELECT
            COUNT(*)                                 AS total,
            COUNT(end_date)                          AS has_end_date,
            COUNT(duration_months)                   AS has_duration,
            COUNT(sector_tag)                        AS has_sector
        FROM contract_awards
    """)
    print("contract_awards:")
    for k, v in (awards or {}).items():
        print(f"  {k}: {v}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill award duration/sector enrichment")
    parser.add_argument("--mbie",   action="store_true", help="Enrich mbie_award_notices")
    parser.add_argument("--awards", action="store_true", help="Enrich contract_awards")
    parser.add_argument("--all",    action="store_true", help="Enrich both tables")
    parser.add_argument("--force",  action="store_true", help="Re-process already-enriched rows")
    parser.add_argument("--limit",  type=int, default=None, help="Limit rows processed per table")
    parser.add_argument("--stats",  action="store_true", help="Print coverage stats and exit")
    args = parser.parse_args()

    if args.stats:
        print_stats()
        sys.exit(0)

    if not (args.mbie or args.awards or args.all):
        parser.error("Specify --mbie, --awards, or --all")

    if args.mbie or args.all:
        enrich_mbie(force=args.force, limit=args.limit)

    if args.awards or args.all:
        enrich_contract_awards(force=args.force, limit=args.limit)

    print_stats()
