"""
Incumbent identification diagnostic test.

Tests _web_search_incumbent() against three known notices.
Run via: railway run python3 _test_incumbent.py

Expected outcomes:
  34159082  Navigation Training Services, NZDF  → Named incumbent: Serco
  34118228  Speech to Text Solution, MoJ        → No named STT incumbent; FTR/Tyler in audio infra
  Third notice (ICT/advisory from live watchlist) → named or credible no-result with reasoning
"""
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")

import db
from pursuit_package import _web_search_incumbent

DIVIDER = "\n" + "=" * 72 + "\n"


def test_notice(notice_id: str, label: str = "") -> None:
    print(DIVIDER)
    print(f"NOTICE: {notice_id}  {label}")

    row = db.fetchone(
        """
        SELECT r.notice_id, r.title, r.agency, r.overview_text, r.description,
               p.sector_tag
          FROM raw_notices r
          JOIN parsed_notices p ON p.notice_id = r.notice_id
         WHERE r.notice_id = %s
        """,
        (notice_id,),
    )
    if not row:
        print(f"  *** Notice {notice_id} not found in DB ***")
        return

    agency       = row.get("agency") or ""
    sector       = row.get("sector_tag") or "other"
    title        = row.get("title") or ""
    notice_text  = (row.get("overview_text") or row.get("description") or "")[:2000]

    print(f"  Agency:  {agency}")
    print(f"  Title:   {title}")
    print(f"  Sector:  {sector}")
    print(f"  Text:    {notice_text[:120]!r}{'...' if len(notice_text) > 120 else ''}")
    print()
    print("  Running _web_search_incumbent() ...")
    print()

    result = _web_search_incumbent(agency, sector, title, notice_text)

    print("  RESULT:")
    print()
    for line in result.split(". "):
        print(f"    {line.strip()}.")
    print()


def pick_third_notice() -> tuple:
    """Pick one ICT or advisory notice from the current watchlist with score >= 60."""
    row = db.fetchone(
        """
        SELECT r.notice_id, r.title, r.agency, p.sector_tag
          FROM raw_notices r
          JOIN parsed_notices p ON p.notice_id = r.notice_id
          JOIN scored_notices s ON s.notice_id = r.notice_id
         WHERE p.sector_tag IN ('ICT', 'advisory')
           AND s.composite_score >= 60
           AND p.days_until_close > 0
         ORDER BY s.composite_score DESC
         LIMIT 1
        """,
    )
    if row:
        return row["notice_id"], f"{row['title'][:50]} | {row['agency']}"
    return None, None


if __name__ == "__main__":
    print("=== Incumbent identification diagnostic ===")
    print()

    # Test 1: NZDF Navigation Training — must find Serco
    test_notice("34159082", "Navigation Training Services | NZDF")

    # Test 2: MoJ Speech to Text — no STT incumbent but FTR/Tyler audio infrastructure
    test_notice("34118228", "Speech to Text Solution | Ministry of Justice")

    # Test 3: live ICT/advisory notice
    notice_id, label = pick_third_notice()
    if notice_id:
        test_notice(notice_id, label)
    else:
        print(DIVIDER)
        print("No ICT/advisory notice found on watchlist — skipping test 3")

    print(DIVIDER)
    print("Done.")
