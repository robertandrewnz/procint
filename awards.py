"""
Layer 2 — Contract award tracking.

Scrapes GETS contract award notices (separate from tender notices), parses
winning supplier, contract value, duration and dates, stores against the
organisations knowledge graph.

GETS award notice URL:
  https://www.gets.govt.nz/ExternalIndex.htm?status=awarded&ResultType=tender

Award notices follow the same list-page HTML structure as tender notices
(tr.tender[id^='tender-']) but the detail page has different field labels.
The scraper tries the same requests→Playwright fallback strategy as Layer 1.

Note: GETS does not expose all historical award notices publicly. The scraper
will capture what is currently visible and build history incrementally over
daily runs.
"""
import logging
import re
from datetime import date, datetime
from typing import Optional
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup

import config
import db
import organisations as orgs
from enrich_award_durations import extract_duration, _tag_sector

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ProcintBot/1.0; +https://procint.internal)",
    "Accept-Language": "en-NZ,en;q=0.9",
}

# ── Fetch helpers ─────────────────────────────────────────────────────────────

def _fetch(url: str, params: Optional[dict] = None) -> Optional[str]:
    try:
        resp = requests.get(url, params=params, headers=HEADERS,
                            timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        logger.debug("requests failed for %s: %s", url, exc)
        return None


def _fetch_playwright(url: str) -> str:
    from playwright.sync_api import sync_playwright
    logger.info("Playwright fallback for %s", url)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, timeout=config.PLAYWRIGHT_TIMEOUT)
        try:
            page.wait_for_selector("tr.tender", timeout=15000)
        except Exception:
            pass
        html = page.content()
        browser.close()
    return html


# ── Date parsing ──────────────────────────────────────────────────────────────

def _parse_date(text: Optional[str]) -> Optional[date]:
    if not text:
        return None
    text = re.sub(r"\s*\(.*?\)", "", text).strip()
    text = re.sub(r"^\d{1,2}:\d{2}\s*(?:AM|PM)?\s*", "", text,
                  flags=re.IGNORECASE).strip()
    for fmt in ("%d %b %Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# ── Value parsing ─────────────────────────────────────────────────────────────

_VALUE_RE = re.compile(
    r"\$?\s*([\d,]+(?:\.\d+)?)\s*(m|million|k|thousand)?",
    re.IGNORECASE,
)
_MULT = {"m": 1_000_000, "million": 1_000_000, "k": 1_000, "thousand": 1_000}


def _parse_value(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    m = _VALUE_RE.search(text.replace(",", ""))
    if not m:
        return None
    val = float(m.group(1).replace(",", ""))
    suffix = (m.group(2) or "").lower()
    return val * _MULT.get(suffix, 1)


# ── Duration parsing ──────────────────────────────────────────────────────────

def _parse_duration_months(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"(\d+)\s*year", text, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 12
    m = re.search(r"(\d+)\s*month", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


# ── List-page scraping ────────────────────────────────────────────────────────

def _extract_award_stubs(html: str) -> list[dict]:
    """
    Parse GETS award list page. Same tr.tender structure as tender list.
    Returns lightweight stubs with enough to fetch the detail page.
    """
    soup = BeautifulSoup(html, "lxml")
    stubs = []
    for row in soup.select("tr.tender[id^='tender-']"):
        cells = row.find_all("td")
        if len(cells) < 6:
            continue
        row_id = row.get("id", "").replace("tender-", "").strip()
        if not row_id:
            continue
        title_cell = cells[2]
        link_tag = title_cell.find("a", href=True)
        if not link_tag:
            continue
        href = link_tag["href"]
        url = href if href.startswith("http") else urljoin(config.GETS_BASE_URL + "/", href)
        stubs.append({
            "gets_notice_id": row_id,
            "title": link_tag.get_text(strip=True),
            "agency_name_raw": cells[5].get_text(strip=True),
            "close_date_raw": cells[4].get_text(strip=True),
            "source_url": url,
        })
    return stubs


def _already_stored(gets_notice_id: str) -> bool:
    return db.fetchone(
        "SELECT 1 FROM contract_awards WHERE gets_notice_id = %s",
        (gets_notice_id,),
    ) is not None


# ── Detail-page parsing ───────────────────────────────────────────────────────
#
# GETS award DETAIL pages require authentication — unauthenticated fetches
# return a "you are not logged in" page whose text contains the notice ID
# number, causing the old _find_value() parser to misidentify it as the
# contract value.  We now detect the unauthenticated response and fall back to
# extracting what we can from the LIST PAGE data (title, award_date, agency)
# plus title-based duration/sector extraction.

_UNAUTH_MARKERS = [
    "You are not logged in",
    "TendererLogin.auth",
    "Create account",
]


def _is_unauth_page(html: str) -> bool:
    """Return True if the GETS response is an 'unauthenticated' redirect page."""
    if not html:
        return True
    for marker in _UNAUTH_MARKERS:
        if marker in html:
            return True
    return False


def _parse_award_detail(stub: dict, html: str) -> dict:
    """
    Extract structured fields from a GETS award detail page.

    When the detail page is unavailable (authentication required), falls back
    to title-based extraction only — no bogus values are stored.
    Duration and sector are extracted from the title using the same regex
    patterns used by enrich_award_durations.
    """
    award = dict(stub)
    award["raw_html"] = None  # don't store multi-MB auth-redirect HTML

    # Close date from the list page stub becomes the award_date fallback
    award["award_date"] = _parse_date(stub.get("close_date_raw"))

    # ── Unauthenticated fallback: title-only extraction ────────────────────────
    if _is_unauth_page(html):
        logger.debug(
            "Award %s: detail page requires auth — using title-only extraction",
            stub.get("gets_notice_id"),
        )
        award["supplier_name_raw"] = None
        award["contract_value"]    = None
        award["contract_value_raw"] = None
        duration, end_date = extract_duration(
            stub.get("title", ""), None, award["award_date"]
        )
        award["duration_months"] = duration
        award["start_date"]      = None
        award["end_date"]        = end_date
        award["description"]     = None
        award["sector_tag"]      = _tag_sector(stub.get("title", ""), "")
        return award

    # ── Authenticated path: full HTML parsing ──────────────────────────────────
    soup = BeautifulSoup(html, "lxml")

    def _find_value(labels: list[str]) -> Optional[str]:
        """Find the text adjacent to any of the given label strings."""
        for label in labels:
            tag = soup.find(string=re.compile(re.escape(label), re.IGNORECASE))
            if not tag:
                continue
            parent = tag.find_parent()
            if not parent:
                continue
            # Try sibling of the label element
            sib = parent.find_next_sibling()
            if sib:
                val = sib.get_text(strip=True)
                if val and len(val) < 500:
                    return val
            # Try the parent's next sibling (table-row pattern)
            grandparent = parent.find_parent()
            if grandparent:
                sib2 = grandparent.find_next_sibling()
                if sib2:
                    val = sib2.get_text(strip=True)
                    if val and len(val) < 500:
                        return val
        return None

    # Supplier
    award["supplier_name_raw"] = _find_value([
        "Supplier", "Awarded to", "Successful tenderer",
        "Contract awarded to", "Winner",
    ])

    # Contract value — guard against the notice ID being parsed as value
    raw_val = _find_value([
        "Contract value", "Award value", "Contract amount",
        "Value", "Total value",
    ])
    parsed_val = _parse_value(raw_val) if raw_val else None
    notice_id_numeric = None
    try:
        notice_id_numeric = float(stub.get("gets_notice_id", ""))
    except (TypeError, ValueError):
        pass
    if parsed_val and notice_id_numeric and parsed_val == notice_id_numeric:
        parsed_val = None  # reject — it's the notice ID, not a value
    award["contract_value_raw"] = raw_val
    award["contract_value"]     = parsed_val

    # Duration — from labelled field, fall back to title extraction
    raw_dur = _find_value([
        "Contract duration", "Duration", "Term",
        "Contract term", "Contract period",
    ])
    duration_months = _parse_duration_months(raw_dur)

    # Start/end dates
    raw_start = _find_value(["Start date", "Commencement", "Contract start"])
    raw_end   = _find_value(["End date", "Contract end", "Expiry date", "Expiry"])
    award["start_date"] = _parse_date(raw_start)
    award["end_date"]   = _parse_date(raw_end)

    # Compute end_date from start + duration if not directly available
    if award["start_date"] and duration_months and not award["end_date"]:
        sd = award["start_date"]
        try:
            award["end_date"] = sd.replace(
                month=((sd.month - 1 + duration_months) % 12) + 1,
                year=sd.year + ((sd.month - 1 + duration_months) // 12),
            )
        except ValueError:
            pass

    # If still no duration, try title extraction
    if not duration_months:
        duration_months, end_fallback = extract_duration(
            stub.get("title", ""), None, award["award_date"]
        )
        if not award["end_date"] and end_fallback:
            award["end_date"] = end_fallback

    award["duration_months"] = duration_months

    # Award date (prefer labelled field, fall back to close_date from list page)
    raw_award_date = _find_value(["Award date", "Date awarded", "Notification date"])
    award["award_date"] = _parse_date(raw_award_date) or award["award_date"]

    # Description
    desc_tag = soup.select_one(".notice-description, #noticeDescription, .description")
    award["description"] = desc_tag.get_text(separator=" ", strip=True) if desc_tag else None

    # Sector tag
    award["sector_tag"] = _tag_sector(stub.get("title", ""), award.get("description") or "")

    return award


# ── Storage ───────────────────────────────────────────────────────────────────

def _store_award(award: dict) -> int:
    """Upsert an award record, link to organisations, return award_id."""
    # Resolve / upsert agency
    agency_id = None
    if award.get("agency_name_raw"):
        agency_id = orgs.upsert_organisation(
            award["agency_name_raw"],
            org_type="agency",
            confidence="high",
            alias_source="gets_agency",
        )

    # Resolve / upsert supplier
    supplier_id = None
    if award.get("supplier_name_raw"):
        supplier_id = orgs.upsert_organisation(
            award["supplier_name_raw"],
            org_type="bidder",
            confidence="medium",
            alias_source="award_notice",
        )

    # Attempt to link to original tender notice
    tender_id = None
    if award.get("gets_notice_id"):
        row = db.fetchone(
            "SELECT notice_id FROM raw_notices WHERE notice_id = %s",
            (award["gets_notice_id"],),
        )
        if row:
            tender_id = row["notice_id"]

    row = db.fetchone(
        """
        INSERT INTO contract_awards
            (gets_notice_id, tender_notice_id, source_url, title,
             agency_org_id, supplier_org_id,
             agency_name_raw, supplier_name_raw,
             award_date, contract_value, contract_value_raw,
             duration_months, start_date, end_date,
             sector_tag, description, raw_html)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (gets_notice_id) DO UPDATE SET
            supplier_org_id     = EXCLUDED.supplier_org_id,
            supplier_name_raw   = COALESCE(EXCLUDED.supplier_name_raw,  contract_awards.supplier_name_raw),
            contract_value      = COALESCE(EXCLUDED.contract_value,     contract_awards.contract_value),
            duration_months     = COALESCE(EXCLUDED.duration_months,    contract_awards.duration_months),
            end_date            = COALESCE(EXCLUDED.end_date,           contract_awards.end_date),
            sector_tag          = COALESCE(EXCLUDED.sector_tag,         contract_awards.sector_tag)
        RETURNING award_id
        """,
        (
            award.get("gets_notice_id"),
            tender_id,
            award.get("source_url"),
            award.get("title"),
            agency_id,
            supplier_id,
            award.get("agency_name_raw"),
            award.get("supplier_name_raw"),
            award.get("award_date"),
            award.get("contract_value"),
            award.get("contract_value_raw"),
            award.get("duration_months"),
            award.get("start_date"),
            award.get("end_date"),
            award.get("sector_tag"),
            award.get("description"),
            award.get("raw_html"),
        ),
    )
    award_id = row["award_id"]

    # Record agency→supplier relationship
    if agency_id and supplier_id:
        db.execute(
            """
            INSERT INTO relationships
                (org_id_a, org_id_b, relationship_type, evidence_award_id, strength)
            VALUES (%s, %s, 'agency_supplier', %s, 'confirmed')
            ON CONFLICT (org_id_a, org_id_b, relationship_type) DO NOTHING
            """,
            (agency_id, supplier_id, award_id),
        )

    return award_id


# ── Main entry point ──────────────────────────────────────────────────────────

def run_awards_ingestion(max_pages: int = 10) -> int:
    """
    Scrape GETS award notices and store new ones.
    Returns count of new awards stored.
    """
    logger.info("Starting contract awards ingestion")
    new_count = 0

    for page in range(1, max_pages + 1):
        params = {**config.GETS_AWARDS_PARAMS, "page": page}
        url = f"{config.GETS_AWARDS_URL}?{urlencode(params)}"
        logger.debug("Fetching awards page: %s", url)

        html = _fetch(url)
        if not html:
            html = _fetch_playwright(url)

        stubs = _extract_award_stubs(html)
        if not stubs:
            logger.info("No award notices found on page %d — stopping", page)
            break

        page_new = 0
        for stub in stubs:
            if _already_stored(stub["gets_notice_id"]):
                logger.debug("Award already stored: %s", stub["gets_notice_id"])
                continue

            detail_html = _fetch(stub["source_url"])
            if not detail_html:
                detail_html = _fetch_playwright(stub["source_url"])

            try:
                award = _parse_award_detail(stub, detail_html)
                award_id = _store_award(award)
                page_new += 1
                logger.info(
                    "Stored award %s: %s → %s (£%s)",
                    award["gets_notice_id"],
                    award.get("agency_name_raw", "?"),
                    award.get("supplier_name_raw", "?"),
                    award.get("contract_value_raw", "?"),
                )
            except Exception as exc:
                logger.warning(
                    "Failed to store award %s: %s",
                    stub.get("gets_notice_id"), exc,
                )

        new_count += page_new
        if page_new == 0:
            logger.info("All awards on page %d already seen — stopping", page)
            break

    orgs.refresh_award_counts()
    logger.info("Awards ingestion complete: %d new awards stored", new_count)
    return new_count


def get_awards_for_agency(org_id: int, limit: int = 20) -> list[dict]:
    """Return recent contract awards for a given agency org_id."""
    return db.fetchall(
        """
        SELECT ca.award_id, ca.title, ca.award_date, ca.contract_value,
               ca.duration_months, ca.end_date, ca.sector_tag,
               o.name AS supplier_name
          FROM contract_awards ca
          LEFT JOIN organisations o ON o.org_id = ca.supplier_org_id
         WHERE ca.agency_org_id = %s
         ORDER BY ca.award_date DESC NULLS LAST
         LIMIT %s
        """,
        (org_id, limit),
    )


def get_awards_for_supplier(org_id: int, limit: int = 20) -> list[dict]:
    """Return recent contract wins for a given supplier org_id."""
    return db.fetchall(
        """
        SELECT ca.award_id, ca.title, ca.award_date, ca.contract_value,
               ca.duration_months, ca.end_date, ca.sector_tag,
               o.name AS agency_name
          FROM contract_awards ca
          LEFT JOIN organisations o ON o.org_id = ca.agency_org_id
         WHERE ca.supplier_org_id = %s
         ORDER BY ca.award_date DESC NULLS LAST
         LIMIT %s
        """,
        (org_id, limit),
    )
