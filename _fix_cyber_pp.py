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
        log.error("Notice %s not found in DB — check the notice_id", NOTICE_ID)
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


if __name__ == "__main__":
    main()
