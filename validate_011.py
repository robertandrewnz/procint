"""
One-shot validation script for migration 011 + overview_text fixes.

Run from the Procint directory with Railway credentials injected:
    railway run python3 validate_011.py

Steps:
  1. Apply migration 011 (ADD COLUMN IF NOT EXISTS — safe to re-run)
  2. Scrape notice 34118228 from GETS into raw_notices
  3. Parse the notice into parsed_notices; verify key dates
  4. Regenerate pursuit package via _call_claude; inspect output
  5. Print full results summary
"""
import json
import logging
import re
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s  %(name)s  %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("validate_011")

TARGET_NOTICE_ID = "34118228"
TARGET_URL = f"https://www.gets.govt.nz/MOJ/ExternalTenderDetails.htm?id={TARGET_NOTICE_ID}"
TEST_CLIENT = "Validate NZ Ltd"

# ── Imports (after config so .env is loaded) ──────────────────────────────────

import config  # noqa — loads .env into os.environ
import db

# ── Step 1: Apply migration ───────────────────────────────────────────────────

logger.info("=== STEP 1: Applying migration 011 ===")
migration_sql = Path("migrations/011_overview_and_key_dates.sql").read_text()
for stmt in migration_sql.split(";"):
    stmt = stmt.strip()
    if stmt and not stmt.startswith("--"):
        db.execute(stmt)
logger.info("Migration applied (ADD COLUMN IF NOT EXISTS — idempotent).")

cols = db.fetchall(
    """
    SELECT column_name, table_name
    FROM information_schema.columns
    WHERE table_name IN ('raw_notices', 'parsed_notices')
      AND column_name IN (
          'overview_text', 'briefing_date', 'questions_deadline',
          'registration_deadline', 'procurement_stage'
      )
    ORDER BY table_name, column_name
    """
)
if not cols:
    logger.error("FAIL: no new columns found after migration")
    sys.exit(1)
for c in cols:
    logger.info("  CONFIRMED: %s.%s", c["table_name"], c["column_name"])

# ── Step 2: Scrape the target notice ─────────────────────────────────────────

logger.info("=== STEP 2: Scraping notice %s ===", TARGET_NOTICE_ID)

# Clear prior rows so we test a clean insert
for tbl in ("parsed_notices", "enriched_notices", "scored_notices", "raw_notices"):
    try:
        db.execute(f"DELETE FROM {tbl} WHERE notice_id = %s", (TARGET_NOTICE_ID,))
    except Exception:
        pass  # table may not have the notice yet
logger.info("Cleared any existing rows for %s", TARGET_NOTICE_ID)

from ingestion import _fetch_html_requests, _fetch_html_playwright, _extract_overview_text, _store_notice
from bs4 import BeautifulSoup

html = _fetch_html_requests(TARGET_URL)
if html is None:
    logger.warning("requests fetch failed — falling back to Playwright")
    html = _fetch_html_playwright(TARGET_URL)

soup = BeautifulSoup(html, "lxml")
overview = _extract_overview_text(soup)

if not overview:
    logger.warning("WARN: overview_text is None/empty — multi-strategy extraction found nothing")
    logger.info("Dumping page title and first 1000 chars of body text for diagnosis:")
    logger.info("  title: %s", soup.title.get_text(strip=True) if soup.title else "(none)")
    body_text = soup.get_text(separator="\n", strip=True)[:1000]
    logger.info("  body preview:\n%s", body_text)
else:
    logger.info("overview_text extracted: %d chars", len(overview))
    logger.info("overview_text preview (first 500 chars):\n%s", overview[:500])

# Grab page title for the notice record
page_title = soup.title.get_text(strip=True) if soup.title else f"Notice {TARGET_NOTICE_ID}"

# Estimated value extraction (replicates ingestion._fetch_notice_detail logic)
estimated_value = None
value_label = soup.find(string=re.compile(r"[Ee]stimated [Vv]alue|[Cc]ontract [Vv]alue"))
if value_label:
    parent = value_label.find_parent()
    sibling = parent.find_next_sibling() if parent else None
    if sibling:
        estimated_value = sibling.get_text(strip=True)
    else:
        text_after = value_label.parent.get_text(strip=True) if value_label.parent else ""
        m = re.search(r"\$[\d,\.]+", text_after)
        if m:
            estimated_value = m.group(0)

notice_row = {
    "notice_id":       TARGET_NOTICE_ID,
    "source_url":      TARGET_URL,
    "title":           page_title,
    "agency":          "Ministry of Justice",
    "category_raw":    "RFP",
    "estimated_value": estimated_value,
    "open_date":       None,
    "close_date":      None,
    "description":     overview,
    "overview_text":   overview,
    "raw_html":        html,
}

_store_notice(notice_row)
logger.info("Stored notice %s to raw_notices.", TARGET_NOTICE_ID)

stored = db.fetchone(
    "SELECT notice_id, title, (overview_text IS NOT NULL AND overview_text != '') AS has_overview FROM raw_notices WHERE notice_id = %s",
    (TARGET_NOTICE_ID,),
)
if not stored:
    logger.error("FAIL: notice not found in raw_notices after store")
    sys.exit(1)
logger.info("DB CONFIRMED: notice_id=%s  has_overview=%s  title=%s",
    stored["notice_id"], stored["has_overview"], stored["title"])
if not stored["has_overview"]:
    logger.warning("WARN: overview_text is NULL in DB — cannot validate key date extraction")

# ── Step 3: Parse the notice ──────────────────────────────────────────────────

logger.info("=== STEP 3: Parsing notice %s ===", TARGET_NOTICE_ID)

from parsing import (
    classify_sector, assign_value_band, extract_duration,
    extract_geographic_scope, extract_evaluation_criteria,
    days_until_close, extract_key_dates, extract_procurement_stage,
    _store_parsed,
)

raw = db.fetchone("SELECT * FROM raw_notices WHERE notice_id = %s", (TARGET_NOTICE_ID,))
ov = raw.get("overview_text") or raw.get("description") or ""

sector = classify_sector(raw.get("title") or "", raw.get("category_raw") or "", ov)
value_band, val_min, val_max = assign_value_band(raw.get("estimated_value"))
key_dates = extract_key_dates(ov)
stage = extract_procurement_stage(raw.get("category_raw"), ov)

logger.info("--- Extracted Fields ---")
logger.info("  sector:                %s", sector)
logger.info("  value_band:            %s", value_band)
logger.info("  procurement_stage:     %s", stage)
logger.info("  briefing_date:         %s", key_dates["briefing_date"])
logger.info("  questions_deadline:    %s", key_dates["questions_deadline"])
logger.info("  registration_deadline: %s", key_dates["registration_deadline"])

from datetime import date as date_cls

expected_briefing = date_cls(2026, 5, 25)
if key_dates["briefing_date"] == expected_briefing:
    logger.info("PASS: briefing_date correctly resolved to %s", expected_briefing)
else:
    logger.warning(
        "WARN: briefing_date=%s (expected %s) — check regex patterns or overview_text content",
        key_dates["briefing_date"], expected_briefing,
    )
    if ov:
        # Show lines that contain "brief" or a date pattern to help diagnose
        relevant = [ln for ln in ov.splitlines() if re.search(r"brief|25 may|25/05", ln, re.IGNORECASE)]
        logger.info("  Relevant lines from overview_text:\n    %s", "\n    ".join(relevant[:10]))

parsed = {
    "notice_id":             TARGET_NOTICE_ID,
    "agency_name":           raw.get("agency"),
    "sector_tag":            sector,
    "value_band":            value_band,
    "estimated_value_min":   val_min,
    "estimated_value_max":   val_max,
    "contract_duration":     extract_duration(ov),
    "geographic_scope":      extract_geographic_scope(ov, raw.get("title")),
    "evaluation_criteria":   extract_evaluation_criteria(ov),
    "close_date":            raw.get("close_date"),
    "days_until_close":      days_until_close(raw.get("close_date")),
    "briefing_date":         key_dates["briefing_date"],
    "questions_deadline":    key_dates["questions_deadline"],
    "registration_deadline": key_dates["registration_deadline"],
    "procurement_stage":     stage,
}
_store_parsed(parsed)
logger.info("Stored parsed record for %s.", TARGET_NOTICE_ID)

p_row = db.fetchone(
    """
    SELECT briefing_date, questions_deadline, registration_deadline, procurement_stage
    FROM parsed_notices WHERE notice_id = %s
    """,
    (TARGET_NOTICE_ID,),
)
logger.info("DB CONFIRMED: briefing_date=%s  questions_deadline=%s  reg_deadline=%s  stage=%s",
    p_row["briefing_date"], p_row["questions_deadline"],
    p_row["registration_deadline"], p_row["procurement_stage"])

# ── Step 4: Pursuit package via _call_claude ──────────────────────────────────

logger.info("=== STEP 4: Generating pursuit package via _call_claude ===")

# scored_notices row is required by _get_notice JOIN
db.execute(
    """
    INSERT INTO scored_notices (notice_id, composite_score, score_reasoning)
    VALUES (%s, 7.5, 'validate_011 synthetic score')
    ON CONFLICT (notice_id) DO UPDATE SET composite_score = 7.5
    """,
    (TARGET_NOTICE_ID,),
)

from pursuit_package import (
    _get_notice, _get_competitive_landscape, _get_client_history,
    _detect_incumbent, _get_agency_stats, _get_relevant_flags,
    _get_national_market_context, _mbie_citation, _call_claude,
)

notice_data = _get_notice(TARGET_NOTICE_ID)
if not notice_data:
    logger.error("FAIL: _get_notice returned None — check JOIN conditions")
    sys.exit(1)

logger.info("_get_notice returned fields:")
for k in ("title", "overview_text", "briefing_date", "questions_deadline",
          "registration_deadline", "procurement_stage"):
    logger.info("  %s = %s", k, repr(notice_data.get(k))[:120] if notice_data.get(k) else None)

agency = notice_data.get("agency") or "Ministry of Justice"
sector_tag = notice_data.get("sector_tag") or "other"

context = {
    "client_name": TEST_CLIENT,
    "preferred_sectors": [],
    "firm_profile": {},
    "notice": dict(notice_data),
    "enrichment": {
        "summary": notice_data.get("summary"),
        "evaluation_weighting": notice_data.get("evaluation_weighting"),
        "red_flags": notice_data.get("red_flags"),
        "strategic_framing": notice_data.get("strategic_framing"),
    },
    "competitors":     [dict(c) for c in _get_competitive_landscape(agency, sector_tag)],
    "client_history":  _get_client_history(TEST_CLIENT, sector_tag, agency),
    "incumbent":       dict(_detect_incumbent(agency, sector_tag)) if _detect_incumbent(agency, sector_tag) else None,
    "agency_stats":    _get_agency_stats(agency, sector_tag),
    "flags":           [dict(f) for f in _get_relevant_flags(agency, sector_tag)],
    "mbie_citation":   _mbie_citation(sector_tag, agency),
    "national_market": _get_national_market_context(sector_tag),
    "agency_plan_signals": [],
}

analysis = _call_claude(context)
if not analysis:
    logger.error("FAIL: _call_claude returned None — Claude API error or JSON parse failure")
    sys.exit(1)

# ── Step 5: Results summary ───────────────────────────────────────────────────

logger.info("=== STEP 5: Results Summary ===\n")

exec_summary     = analysis.get("executive_summary", "")
go_nogo          = analysis.get("go_nogo", "")
go_nogo_rationale = analysis.get("go_nogo_rationale", "")
actions          = analysis.get("recommended_actions") or []

logger.info("Go/No-Go verdict: %s\n", go_nogo)
logger.info("--- Executive Summary ---\n%s\n", exec_summary)
logger.info("--- Go/No-Go Rationale ---\n%s\n", go_nogo_rationale)

if actions:
    logger.info("--- Recommended Actions ---")
    for a in actions:
        if isinstance(a, dict):
            logger.info("  [%s] %s (%s)", a.get("priority","?"), a.get("action",""), a.get("timeframe",""))
        else:
            logger.info("  • %s", a)

# Validation checks
all_text = " ".join([exec_summary, go_nogo_rationale,
                     " ".join(str(a) for a in actions)]).lower()

logger.info("\n--- Validation Checks ---")

# Check 1: no briefing gap language
gap_phrases = ["briefing gap", "missed briefing", "no briefing", "intelligence gap", "briefing not attended"]
gap_hits = [p for p in gap_phrases if p in all_text]
if gap_hits:
    logger.warning("FAIL (check 1): briefing gap language found: %s", gap_hits)
else:
    logger.info("PASS (check 1): no briefing gap / missed briefing language")

# Check 2: questions deadline appears as named action
q_phrases = ["questions close", "question", "clarif", "questions deadline", "questions must"]
q_hits = [p for p in q_phrases if p in all_text]
if q_hits:
    logger.info("PASS (check 2): questions deadline referenced — matched: %s", q_hits)
else:
    logger.warning("WARN (check 2): questions deadline not referenced in output")

# Check 3: partial data disclaimer (optional — present when overview is sparse)
partial_phrases = ["not include", "confirm with", "should be confirmed", "data limitation",
                   "not found in notice", "does not include"]
partial_hits = [p for p in partial_phrases if p in all_text]
if partial_hits:
    logger.info("PASS (check 3): partial data disclaimer present — matched: %s", partial_hits)
else:
    logger.info("INFO (check 3): no partial data disclaimer (may be fine if overview was complete)")

logger.info("\n=== Validation complete ===")
