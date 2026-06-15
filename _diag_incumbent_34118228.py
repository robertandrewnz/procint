"""
Diagnostic script — incumbent detection for notice 34118228.

Runs the full incumbent detection chain with verbose INCUMBENT_DIAG logging
and stops before generating the full HTML package (saves cost/time).

Run:
    railway run python3 _diag_incumbent_34118228.py
"""

import logging
import sys

# Force DEBUG level so all INCUMBENT_DIAG lines appear
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("diag")

NOTICE_ID = "34118228"

import db
import config
from pursuit_package import _web_search_incumbent, _extract_doc_incumbent, _get_notice

print("\n" + "=" * 70)
print(f"STEP 1 — Fetch notice {NOTICE_ID}")
print("=" * 70)

notice = _get_notice(NOTICE_ID)
if not notice:
    print(f"ERROR: Notice {NOTICE_ID} not found in database")
    sys.exit(1)

agency  = notice.get("agency") or ""
sector  = notice.get("sector_tag") or "other"
title   = notice.get("title") or ""
print(f"  notice_id : {NOTICE_ID}")
print(f"  agency    : {agency!r}")
print(f"  sector    : {sector!r}")
print(f"  title     : {title!r}")
print(f"  close_date: {notice.get('close_date')}")

print("\n" + "=" * 70)
print("STEP 2 — Run _web_search_incumbent directly (no extra_docs)")
print(f"  sector={sector!r} → will use {'notice title' if sector.lower() in ('other','unknown','') else 'sector label'} as service anchor")
print("=" * 70)
print("(INCUMBENT_DIAG log lines will appear below)\n")

result = _web_search_incumbent(agency, sector, title)

print("\n" + "=" * 70)
print("STEP 3 — Summary")
print("=" * 70)
if result:
    print(f"  WEB SEARCH RETURNED: {result!r}")
    print("\n  → incumbent_text in prompt would be:")
    print(f"    Research result: {result}")
    print(f"    IMPORTANT: In competitive_narrative and incumbent_assessment, name the parent company...")
else:
    print("  WEB SEARCH RETURNED: None")
    print("\n  → incumbent_text in prompt would be (Option C fallback):")
    print('    "No incumbent contract record is publicly available. Based on the notice')
    print('     description, the agency is migrating from or augmenting an existing system')
    print('     — vendors with existing technology relationships at this agency should be')
    print('     treated as structural favourites regardless of absence from public award data."')
