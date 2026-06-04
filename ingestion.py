"""
GETS ingestion module.

Strategy:
  1. Attempt lightweight requests + BeautifulSoup fetch.
  2. If the page appears JS-rendered (no notices found), fall back to Playwright.

Stores each new notice in raw_notices. Already-seen notice_ids are skipped.
"""
import logging
import re
from datetime import date
from typing import Optional
from urllib.parse import urljoin, urlencode

import requests
from bs4 import BeautifulSoup

import config
import db

logger = logging.getLogger(__name__)

# ── Lightweight fetch ─────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; ProcintBot/1.0; +https://procint.internal)"
    ),
    "Accept-Language": "en-NZ,en;q=0.9",
}


def _fetch_html_requests(url: str, params: Optional[dict] = None) -> Optional[str]:
    try:
        resp = requests.get(
            url, params=params, headers=HEADERS, timeout=config.REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        logger.debug("requests fetch failed for %s: %s", url, exc)
        return None


# ── Playwright fetch ──────────────────────────────────────────────────────────

def _fetch_html_playwright(url: str) -> str:
    from playwright.sync_api import sync_playwright

    logger.info("Falling back to Playwright for %s", url)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, timeout=config.PLAYWRIGHT_TIMEOUT)
        # Wait for the notice list container to appear
        try:
            page.wait_for_selector(
                "table.result-table, .tender-list, #tenderResults",
                timeout=config.PLAYWRIGHT_TIMEOUT,
            )
        except Exception:
            logger.debug("Selector wait timed out; proceeding with current DOM")
        html = page.content()
        browser.close()
    return html


# ── Parsing helpers ───────────────────────────────────────────────────────────

def _parse_date(text: Optional[str]) -> Optional[date]:
    if not text:
        return None
    text = text.strip()
    for fmt in ("%d/%m/%Y", "%d %b %Y", "%Y-%m-%d", "%d-%b-%Y"):
        try:
            from datetime import datetime
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _extract_notices_from_html(html: str, base_url: str) -> list[dict]:
    """
    Parse GETS search results HTML into a list of notice dicts.

    GETS renders notices in a <table> with class 'result-table' or similar.
    The selectors here are based on the public GETS HTML structure as observed;
    they may need adjustment if the site is updated.
    """
    soup = BeautifulSoup(html, "lxml")
    notices = []

    # Primary: table-based results
    rows = soup.select("table.result-table tbody tr, #tenderResults tbody tr")
    if not rows:
        # Fallback: any <tr> inside a results container
        rows = soup.select(".tender-list tr, .results-list tr")

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 3:
            continue

        link_tag = row.find("a", href=True)
        if not link_tag:
            continue

        href = link_tag["href"]
        notice_url = href if href.startswith("http") else urljoin(base_url, href)

        # Extract GETS notice ID from URL (e.g. ?noticeId=12345 or /Notices/12345)
        notice_id_match = re.search(r"[Nn]otice[Ii]d=(\d+)|/Notices?/(\d+)", notice_url)
        if not notice_id_match:
            # Use URL hash as fallback ID
            notice_id = re.sub(r"[^a-zA-Z0-9]", "_", notice_url[-40:])
        else:
            notice_id = notice_id_match.group(1) or notice_id_match.group(2)

        title = link_tag.get_text(strip=True)
        cell_texts = [c.get_text(strip=True) for c in cells]

        # Column order on GETS: Title | Agency | Category | Close Date (approximate)
        agency = cell_texts[1] if len(cell_texts) > 1 else None
        category = cell_texts[2] if len(cell_texts) > 2 else None
        close_date_raw = cell_texts[3] if len(cell_texts) > 3 else None
        open_date_raw = cell_texts[4] if len(cell_texts) > 4 else None

        notices.append(
            {
                "notice_id": notice_id,
                "source_url": notice_url,
                "title": title,
                "agency": agency,
                "category_raw": category,
                "estimated_value": None,  # populated from detail page if needed
                "open_date": _parse_date(open_date_raw),
                "close_date": _parse_date(close_date_raw),
                "description": None,
                "raw_html": None,
            }
        )

    return notices


def _fetch_notice_detail(notice: dict) -> dict:
    """Fetch the individual notice page and extract description + estimated value."""
    html = _fetch_html_requests(notice["source_url"])
    if html is None:
        html = _fetch_html_playwright(notice["source_url"])

    soup = BeautifulSoup(html, "lxml")

    # Description
    desc_tag = soup.select_one(
        ".notice-description, #noticeDescription, .description, .tender-description"
    )
    notice["description"] = desc_tag.get_text(separator=" ", strip=True) if desc_tag else None

    # Estimated value — look for currency pattern near a label
    value_label = soup.find(string=re.compile(r"[Ee]stimated [Vv]alue|[Cc]ontract [Vv]alue"))
    if value_label:
        parent = value_label.find_parent()
        sibling = parent.find_next_sibling() if parent else None
        if sibling:
            notice["estimated_value"] = sibling.get_text(strip=True)
        else:
            # Try adjacent text
            text_after = value_label.parent.get_text(strip=True) if value_label.parent else ""
            match = re.search(r"\$[\d,\.]+", text_after)
            if match:
                notice["estimated_value"] = match.group(0)

    notice["raw_html"] = html
    return notice


# ── Storage ───────────────────────────────────────────────────────────────────

def _store_notice(notice: dict) -> None:
    db.execute(
        """
        INSERT INTO raw_notices
            (notice_id, source_url, title, agency, category_raw,
             estimated_value, open_date, close_date, description, raw_html)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (notice_id) DO NOTHING
        """,
        (
            notice["notice_id"],
            notice["source_url"],
            notice["title"],
            notice["agency"],
            notice["category_raw"],
            notice["estimated_value"],
            notice["open_date"],
            notice["close_date"],
            notice["description"],
            notice["raw_html"],
        ),
    )


# ── Main entry point ──────────────────────────────────────────────────────────

def _get_search_pages() -> list[str]:
    """
    GETS uses a query-param based search. We iterate over pages to collect all
    active notices. Returns a list of page URLs to scrape.
    """
    # Base search: all active notices (status=active), ordered by close date
    base_params = {
        "status": "active",
        "ResultType": "tender",
    }
    # We'll collect pages until we find no new notices (max guard: 50 pages)
    return [
        f"{config.GETS_SEARCH_URL}?{urlencode({**base_params, 'page': p})}"
        for p in range(1, 51)
    ]


def run_ingestion() -> int:
    """
    Scrape GETS for active notices. Returns count of new notices stored.
    """
    logger.info("Starting GETS ingestion")
    new_count = 0

    for page_url in _get_search_pages():
        logger.debug("Fetching page: %s", page_url)

        html = _fetch_html_requests(page_url)
        if html is None:
            html = _fetch_html_playwright(page_url)

        notices = _extract_notices_from_html(html, config.GETS_BASE_URL)

        if not notices:
            logger.info("No notices found on %s — stopping pagination", page_url)
            break

        page_new = 0
        for notice in notices:
            if db.notice_already_seen(notice["notice_id"]):
                logger.debug("Already seen %s — skipping", notice["notice_id"])
                continue

            try:
                notice = _fetch_notice_detail(notice)
                _store_notice(notice)
                page_new += 1
                logger.info("Stored new notice %s: %s", notice["notice_id"], notice["title"])
            except Exception as exc:
                logger.warning("Failed to process notice %s: %s", notice["notice_id"], exc)

        new_count += page_new
        if page_new == 0:
            # All notices on this page were already seen — stop paginating
            logger.info("All notices on page already seen — stopping pagination")
            break

    logger.info("Ingestion complete: %d new notices stored", new_count)
    return new_count
