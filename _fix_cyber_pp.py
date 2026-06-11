"""
Fix cybersecurity pursuit package — use notice 34229494 (MSSP / SIEM).
Run with: railway run python3 _fix_cyber_pp.py
Remove this file after successful execution.
"""
import logging
import sys
import os
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    stream=sys.stdout,
    force=True,
)
log = logging.getLogger("fix_cyber_pp")

import db
from generate_demo_content import (
    DEMO_SECTORS, DEMO_DIR,
    _save_to_db, _upload_to_storage,
    _load_manifest, _write_manifest,
)
from demo_package import generate_demo_package

NOTICE_ID = "34229494"


def main() -> None:
    # Confirm the notice exists and log what we're working with
    row = db.fetchone(
        """
        SELECT r.notice_id, r.title, r.agency, r.close_date,
               p.sector_tag, p.value_band, p.days_until_close
          FROM raw_notices r
          LEFT JOIN parsed_notices p ON p.notice_id = r.notice_id
         WHERE r.notice_id = %s
        """,
        (NOTICE_ID,),
    )
    if not row:
        log.warning("Notice %s not found by ID — searching by title keywords...", NOTICE_ID)
        # Try to find by reference or title keywords
        candidates = db.fetchall(
            """
            SELECT r.notice_id, r.title, r.agency, r.close_date,
                   p.sector_tag, p.value_band
              FROM raw_notices r
              LEFT JOIN parsed_notices p ON p.notice_id = r.notice_id
             WHERE LOWER(r.title) LIKE ANY(ARRAY[
                     '%mssp%', '%siem%', '%cw45326%',
                     '%managed security%', '%security services provider%'
                   ])
                OR LOWER(r.description) LIKE '%cw45326%'
             ORDER BY r.close_date DESC NULLS LAST
             LIMIT 10
            """,
        )
        if candidates:
            log.info("Matching notices found in DB:")
            for c in candidates:
                log.info("  %s  close=%s  sector=%-15s  '%s'",
                         c["notice_id"], c["close_date"], c["sector_tag"], c["title"])
        else:
            log.warning("No matching notices found — notice may not be ingested yet")
            log.info("")
            log.info("All notices with 'security' in title (future close dates):")
            sec_rows = db.fetchall(
                """
                SELECT r.notice_id, r.title, r.agency, r.close_date, p.sector_tag
                  FROM raw_notices r
                  LEFT JOIN parsed_notices p ON p.notice_id = r.notice_id
                 WHERE LOWER(r.title) LIKE '%security%'
                   AND (r.close_date IS NULL OR r.close_date >= CURRENT_DATE)
                 ORDER BY r.close_date ASC NULLS LAST
                 LIMIT 20
                """,
            )
            for s in sec_rows:
                log.info("  %s  close=%s  sector=%-15s  '%s'",
                         s["notice_id"], s["close_date"], s["sector_tag"], s["title"])
        sys.exit(1)

    log.info("Notice found:")
    log.info("  ID:         %s", row["notice_id"])
    log.info("  Title:      %s", row["title"])
    log.info("  Agency:     %s", row["agency"])
    log.info("  Close date: %s", row["close_date"])
    log.info("  Sector tag: %s", row["sector_tag"])
    log.info("  Value band: %s", row["value_band"])

    sk = "cybersecurity"
    sd = DEMO_SECTORS[sk]
    firm_name = sd["firm"]["name"]
    sector_dir = DEMO_DIR / sk
    sector_dir.mkdir(parents=True, exist_ok=True)

    log.info("")
    log.info("Generating cybersecurity pursuit package for %s using notice %s", firm_name, NOTICE_ID)

    try:
        result = generate_demo_package(
            notice_id=NOTICE_ID,
            prospect_name=firm_name,
            output_dir=sector_dir,
            generate_pdf=False,
            firm_profile=sd["firm"],
        )
        html_dest = result.get("html")
    except Exception as exc:
        log.error("PP generation failed: %s", exc, exc_info=True)
        sys.exit(1)

    if not html_dest or not html_dest.exists():
        log.error("PP HTML not found after generation")
        sys.exit(1)

    content = html_dest.read_text(encoding="utf-8")

    if ">NO GO<" in content and "CONDITIONAL GO" not in content:
        log.warning("Generated PP has NO GO verdict — proceeding anyway (notice was explicitly specified)")

    # Clear old cybersecurity PP entries (event security notice + any others)
    try:
        db.execute(
            "DELETE FROM pipeline_outputs WHERE output_type='demo_html' "
            "AND filename LIKE 'cybersecurity/DEMO_%'"
        )
        log.info("Cleared old cybersecurity PP from DB")
    except Exception as e:
        log.warning("Could not clear old PP: %s", e)

    _save_to_db(f"{sk}/{html_dest.name}", content)
    _upload_to_storage(html_dest, f"demo/{sk}/{html_dest.name}")
    log.info("✓ Saved: %s (%d bytes)", html_dest.name, len(content))

    # Update manifest
    manifest = _load_manifest()
    if "sectors" not in manifest:
        manifest["sectors"] = {}

    pp_entry = {
        "type":        "pursuit_package",
        "notice_id":   NOTICE_ID,
        "sector":      sk,
        "title":       (row["title"] or "")[:60],
        "is_demo":     True,
        "demo_sector": sk,
        "demo_label":  f"Cybersecurity Pursuit Package — {(row['title'] or '')[:60]}",
        "html_path":   str(html_dest.relative_to(Path(__file__).parent)),
        "pdf_path":    None,
    }
    current_items = manifest["sectors"].get(sk, {}).get("items", [])
    current_items = [i for i in current_items if i.get("type") != "pursuit_package"]
    current_items.append(pp_entry)
    if sk not in manifest["sectors"]:
        manifest["sectors"][sk] = {}
    manifest["sectors"][sk]["items"] = current_items
    manifest["generated"] = date.today().isoformat()
    _write_manifest(manifest)

    log.info("")
    log.info("✓ Cybersecurity PP updated to notice %s — %s", NOTICE_ID, row["title"])

    # Run bidder inference for this notice so it shows cybersecurity firms
    log.info("")
    log.info("Running bidder inference for notice %s...", NOTICE_ID)
    try:
        from bidders import score_bidders_for_notice, _store_bidders, load_bidders
        notice_ctx = dict(row)
        notice_ctx["notice_id"] = NOTICE_ID
        # Force cybersecurity sector so specialist filtering kicks in correctly
        if not notice_ctx.get("sector_tag") or notice_ctx["sector_tag"] not in ("cybersecurity", "ICT"):
            notice_ctx["sector_tag"] = "cybersecurity"
        all_bidders = load_bidders()
        bidders_result = score_bidders_for_notice(notice_ctx, all_bidders)
        if bidders_result:
            # Clear old bidder_pool entries for this notice first
            db.execute("DELETE FROM bidder_pool WHERE notice_id = %s", (NOTICE_ID,))
            _store_bidders(NOTICE_ID, bidders_result)
            log.info("✓ Stored %d bidders for notice %s:", len(bidders_result), NOTICE_ID)
            for b in bidders_result[:6]:
                log.info("  %-35s  match=%s", b.get("firm_name", ""), b.get("match_type", ""))
        else:
            log.warning("Bidder inference returned no results for notice %s", NOTICE_ID)
            log.warning("  Check that cybersecurity firms exist in bidders.csv with specialist_flags=cybersecurity")
    except Exception as exc:
        log.error("Bidder inference failed: %s", exc, exc_info=True)


if __name__ == "__main__":
    main()
