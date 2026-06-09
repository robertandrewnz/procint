"""
procurement_plan_scraper.py — Agency Procurement Plan ingestion.

Finds, downloads, and ingests NZ government agency annual procurement plans.

Usage:
    # Run all priority agencies (with 30-day cache check)
    python3 procurement_plan_scraper.py

    # Force-refresh all agencies regardless of cache
    python3 procurement_plan_scraper.py --force

    # Run a single agency by short name
    python3 procurement_plan_scraper.py --agency NZTA

Python 3.9 compatible: Optional[X] used throughout, no X | None syntax.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent))

import requests
from bs4 import BeautifulSoup

import anthropic
import config
import db

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)

# ── Priority agencies ─────────────────────────────────────────────────────────

PRIORITY_AGENCIES: List[Dict] = [
    {
        "name": "New Zealand Defence Force",
        "short": "NZDF",
        "plan_url": "https://www.nzdf.mil.nz/procurement",
        "sector_tags": ["defence", "infrastructure", "ICT"],
    },
    {
        "name": "Ministry of Health",
        "short": "MoH",
        "plan_url": "https://www.health.govt.nz/about-ministry/corporate-publications/procurement",
        "sector_tags": ["health", "ICT"],
    },
    {
        "name": "New Zealand Transport Agency",
        "short": "NZTA",
        "plan_url": "https://www.nzta.govt.nz/resources/procurement/",
        "sector_tags": ["infrastructure", "construction", "ICT"],
    },
    {
        "name": "Department of Internal Affairs",
        "short": "DIA",
        "plan_url": "https://www.dia.govt.nz/digital-government/procurement",
        "sector_tags": ["ICT", "FM"],
    },
    {
        "name": "Ministry of Social Development",
        "short": "MSD",
        "plan_url": "https://www.msd.govt.nz/about-msd-and-our-work/procurement/",
        "sector_tags": ["ICT", "FM", "health"],
    },
    {
        "name": "Corrections",
        "short": "Corrections",
        "plan_url": "https://www.corrections.govt.nz/resources/procurement",
        "sector_tags": ["FM", "construction", "infrastructure"],
    },
    {
        "name": "Accident Compensation Corporation",
        "short": "ACC",
        "plan_url": "https://www.acc.co.nz/about-acc/procurement/",
        "sector_tags": ["ICT", "health"],
    },
    {
        "name": "Kainga Ora",
        "short": "KaingaOra",
        "plan_url": "https://kaingaora.govt.nz/procurement/",
        "sector_tags": ["construction", "infrastructure", "FM"],
    },
    {
        "name": "Ministry of Education",
        "short": "MoE",
        "plan_url": "https://www.education.govt.nz/our-work/procurement/",
        "sector_tags": ["construction", "ICT", "FM"],
    },
    {
        "name": "Ministry of Business Innovation and Employment",
        "short": "MBIE",
        "plan_url": "https://www.mbie.govt.nz/about/procurement/",
        "sector_tags": ["ICT", "FM"],
    },
]

# Central procurement plans index
_CENTRAL_INDEX_URL = (
    "https://www.procurement.govt.nz/procurement/procurement-plans/"
)

# Keywords that indicate a procurement plan document
_PLAN_KEYWORDS = {
    "procurement plan",
    "annual plan",
    "forward plan",
    "supplier information",
    "procurement strategy",
    "procurement pipeline",
    "forward procurement",
}

# Current and recent years to prefer
_CURRENT_YEAR = date.today().year
_PREFERRED_YEARS = {str(_CURRENT_YEAR), str(_CURRENT_YEAR - 1), str(_CURRENT_YEAR + 1)}

# How long to use a cached plan before refreshing
_CACHE_DAYS = 30

# Delay between agencies
_INTER_AGENCY_DELAY = 2.0


# ── URL helpers ───────────────────────────────────────────────────────────────

def _is_doc_url(url: str) -> bool:
    """Return True if the URL points to a PDF or DOCX file."""
    path = urlparse(url).path.lower()
    return path.endswith(".pdf") or path.endswith(".docx") or path.endswith(".doc")


def _score_plan_link(href: str, text: str) -> int:
    """Score a link's relevance as a procurement plan. Higher is better."""
    score = 0
    combined = (href + " " + text).lower()
    for kw in _PLAN_KEYWORDS:
        if kw in combined:
            score += 3
    for yr in _PREFERRED_YEARS:
        if yr in combined:
            score += 5
    if _is_doc_url(href):
        score += 2
    return score


# ── Discovery ─────────────────────────────────────────────────────────────────

def discover_plan_urls(agency: Dict) -> List[str]:
    """
    Given an agency dict, find the actual procurement plan document URLs.

    Strategy:
    1. Fetch the agency plan_url page.
    2. Look for links to PDF, DOCX, or HTML pages containing procurement plan keywords.
    3. Prefer links with current year in URL or anchor text.
    4. Return up to 3 most relevant plan URLs.

    Returns empty list on failure.
    """
    plan_url = agency.get("plan_url", "")
    if not plan_url:
        return []

    headers = {"User-Agent": "Mozilla/5.0 (compatible; BidEdge-Intel/1.0; +https://bidedge.co.nz)"}
    try:
        resp = requests.get(plan_url, timeout=5, headers=headers, allow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:
        logger.debug("discover_plan_urls failed for %s: %s", agency.get("short"), exc)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    base = f"{urlparse(plan_url).scheme}://{urlparse(plan_url).netloc}"

    candidates: List[tuple] = []  # (score, url)
    seen = set()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        text = a_tag.get_text(strip=True)

        # Normalise href
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = base + href
        elif not href.startswith("http"):
            href = urljoin(plan_url, href)

        if href in seen:
            continue
        seen.add(href)

        score = _score_plan_link(href, text)
        if score > 0:
            candidates.append((score, href))

    # Sort by score descending, deduplicate, return top 3
    candidates.sort(key=lambda x: x[0], reverse=True)
    results = [url for _, url in candidates[:3]]

    if results:
        logger.info(
            "discover_plan_urls [%s]: found %d candidate(s): %s",
            agency.get("short"), len(results), results[0][:80],
        )
    else:
        logger.info("discover_plan_urls [%s]: no plan links found on %s", agency.get("short"), plan_url)
        # Fall back to the plan_url itself
        results = [plan_url]

    return results


def _discover_from_central_index() -> List[Dict]:
    """
    Scrape the central procurement plans index to find agencies not in PRIORITY_AGENCIES.
    Returns list of agency-like dicts {name, short, plan_url, sector_tags}.
    """
    known_shorts = {a["short"] for a in PRIORITY_AGENCIES}
    agencies: List[Dict] = []

    headers = {"User-Agent": "Mozilla/5.0 (compatible; BidEdge-Intel/1.0)"}
    try:
        resp = requests.get(_CENTRAL_INDEX_URL, timeout=5, headers=headers)
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("Central index fetch failed: %s", exc)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        text = a_tag.get_text(strip=True)
        if not text or len(text) < 5:
            continue
        if "procurement" not in href.lower() and "plan" not in href.lower():
            # Require some indication it's a plan link
            if not any(kw in text.lower() for kw in _PLAN_KEYWORDS):
                continue
        # Normalise
        if href.startswith("/"):
            href = "https://www.procurement.govt.nz" + href
        if not href.startswith("http"):
            continue
        # Skip known agencies by name match
        short_guess = text[:20].replace(" ", "")
        if short_guess in known_shorts:
            continue
        agencies.append({
            "name": text[:80],
            "short": short_guess[:15],
            "plan_url": href,
            "sector_tags": [],
        })

    logger.info("Central index: discovered %d additional agencies", len(agencies))
    return agencies[:20]  # cap to avoid runaway


# ── Download ──────────────────────────────────────────────────────────────────

def download_plan_content(url: str) -> Optional[str]:
    """
    Download and extract text from a plan URL.

    Handles PDF (pdfplumber → PyPDF2 fallback), DOCX, and HTML.
    Returns extracted text (up to 8000 chars) or None on failure.
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; BidEdge-Intel/1.0)"}

    try:
        resp = requests.get(url, timeout=5, headers=headers, stream=True)
        resp.raise_for_status()
    except Exception as exc:
        logger.debug("download_plan_content: fetch failed for %s: %s", url[:80], exc)
        return None

    content_type = resp.headers.get("Content-Type", "").lower()
    url_lower = url.lower()

    # ── PDF ───────────────────────────────────────────────────────────────────
    if "pdf" in content_type or url_lower.endswith(".pdf"):
        text = _extract_pdf_bytes(resp.content, url)
        if text:
            logger.info("PDF extracted: %d chars from %s", len(text), url[:70])
            return text[:8000]
        return None

    # ── DOCX ──────────────────────────────────────────────────────────────────
    if (
        "wordprocessingml" in content_type
        or "msword" in content_type
        or url_lower.endswith(".docx")
        or url_lower.endswith(".doc")
    ):
        text = _extract_docx_bytes(resp.content, url)
        if text:
            logger.info("DOCX extracted: %d chars from %s", len(text), url[:70])
            return text[:8000]
        return None

    # ── HTML ──────────────────────────────────────────────────────────────────
    try:
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
            tag.decompose()
        main = soup.find("main") or soup.find("article") or soup.find(id="content") or soup
        text = main.get_text(separator="\n", strip=True)
        if text and len(text) > 100:
            logger.info("HTML extracted: %d chars from %s", len(text), url[:70])
            return text[:8000]
        logger.debug("HTML text too short (%d chars) from %s", len(text or ""), url[:70])
        return None
    except Exception as exc:
        logger.debug("HTML extraction failed for %s: %s", url[:70], exc)
        return None


def _extract_pdf_bytes(content: bytes, url: str) -> Optional[str]:
    """Extract text from PDF bytes using pdfplumber, falling back to PyPDF2."""
    import io

    # pdfplumber (primary)
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            pages = []
            total = 0
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                pages.append(page_text)
                total += len(page_text)
                if total >= 8000:
                    break
            text = "\n\n".join(pages)
            if text.strip():
                return text
    except Exception as exc:
        logger.debug("pdfplumber failed for %s: %s", url[:70], exc)

    # PyPDF2 fallback
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(io.BytesIO(content))
        pages = []
        for page in reader.pages:
            pages.append(page.extract_text() or "")
            if sum(len(p) for p in pages) >= 8000:
                break
        text = "\n\n".join(pages)
        if text.strip():
            return text
    except Exception as exc:
        logger.debug("PyPDF2 fallback failed for %s: %s", url[:70], exc)

    return None


def _extract_docx_bytes(content: bytes, url: str) -> Optional[str]:
    """Extract text from DOCX bytes using python-docx."""
    import io

    try:
        import docx as python_docx
    except ImportError:
        try:
            from docx import Document as _Document
            python_docx = type("m", (), {"Document": _Document})()
        except ImportError:
            logger.warning("python-docx not installed — cannot extract DOCX from %s", url[:70])
            return None

    try:
        doc = python_docx.Document(io.BytesIO(content))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        text = "\n".join(paragraphs)
        return text if text.strip() else None
    except Exception as exc:
        logger.debug("python-docx extraction failed for %s: %s", url[:70], exc)
        return None


# ── Signal extraction ─────────────────────────────────────────────────────────

_PLAN_SIGNAL_SYSTEM = (
    "You are extracting procurement intelligence signals from an NZ government agency "
    "procurement plan. Return ONLY valid JSON — no preamble, no markdown fences."
)

_PLAN_SIGNAL_USER = """\
Extract specific, actionable procurement signals from this NZ government agency procurement plan.

Only extract signals where there is a SPECIFIC contract or programme named or clearly implied.
Do NOT extract vague statements like "we will procure services."

Return JSON with this exact structure:
{{
  "signals": [
    {{
      "signal_type": "upcoming_contract|renewal_risk|capability_investment|panel_refresh|budget_signal",
      "title": "Specific contract or programme name (max 15 words)",
      "agency": "{agency_name}",
      "estimated_value_band": "<$500K|$500K-$2M|$2M-$10M|>$10M|Unknown",
      "estimated_timeframe": "e.g. Q3 2026, FY2026/27, Unknown",
      "sector_tags": {sector_tags_json},
      "signal_text": "The specific supporting text from the plan (max 200 chars)",
      "confidence": "high|medium|low",
      "strategic_weight": 1.0
    }}
  ]
}}

Signal types:
- upcoming_contract: A specific named contract about to be tendered
- renewal_risk: An existing contract approaching renewal/re-procurement
- capability_investment: A new capability investment implying future contract(s)
- panel_refresh: A supplier panel being refreshed or created
- budget_signal: A budget allocation indicating contracting activity

Strategic weight rules:
- Named contract with value estimate and timeframe: 2.0
- Named contract with either value OR timeframe: 1.5
- Named programme without specific contract details: 1.0

Agency: {agency_name}
Sector context: {sector_tags}

Return empty signals array if no specific signals found.

--- PLAN CONTENT ---
{content}
"""


def extract_procurement_signals(
    agency_name: str,
    agency_short: str,
    plan_text: str,
    sector_tags: List[str],
) -> List[Dict]:
    """
    Use Claude to extract structured procurement signals from plan text.
    Returns list of signal dicts, empty on failure.
    """
    prompt = _PLAN_SIGNAL_USER.format(
        agency_name=agency_name,
        agency_short=agency_short,
        sector_tags=", ".join(sector_tags),
        sector_tags_json=json.dumps(sector_tags),
        content=plan_text[:7000],
    )

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        msg = client.messages.create(
            model=config.CLAUDE_MODEL_L3,
            max_tokens=2000,
            system=_PLAN_SIGNAL_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?", "", raw).rsplit("```", 1)[0].strip()
        parsed = json.loads(raw)
        signals = parsed.get("signals") or []
        logger.info(
            "extract_procurement_signals [%s]: %d signals extracted",
            agency_short, len(signals),
        )
        return signals
    except json.JSONDecodeError as exc:
        logger.warning("Claude returned invalid JSON for %s: %s", agency_short, exc)
        return []
    except Exception as exc:
        logger.error("Signal extraction failed for %s: %s", agency_short, exc)
        return []


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_plan_category_id() -> Optional[int]:
    """Return the intel_categories.id for 'Agency Procurement Plans'."""
    row = db.fetchone(
        "SELECT id FROM intel_categories WHERE name = %s",
        ("Agency Procurement Plans",),
    )
    return row["id"] if row else None


def _get_cached_source(agency_short: str) -> Optional[Dict]:
    """
    Return the most recent intel_source row for this agency plan,
    if last_checked is within _CACHE_DAYS.
    """
    cutoff = datetime.utcnow() - timedelta(days=_CACHE_DAYS)
    row = db.fetchone(
        """
        SELECT * FROM intel_sources
        WHERE short_name LIKE %s
          AND is_active = TRUE
          AND last_checked IS NOT NULL
          AND last_checked >= %s
        ORDER BY last_checked DESC
        LIMIT 1
        """,
        (f"{agency_short}%", cutoff),
    )
    return dict(row) if row else None


def _upsert_source(
    category_id: int,
    agency: Dict,
    url: str,
    year: int,
) -> int:
    """Upsert intel_sources row, return source id."""
    source_name = f"{agency['short']} Procurement Plan {year}"
    existing = db.fetchone(
        "SELECT id FROM intel_sources WHERE short_name = %s",
        (source_name,),
    )
    if existing:
        db.execute(
            "UPDATE intel_sources SET last_checked = NOW(), url = %s WHERE id = %s",
            (url, existing["id"]),
        )
        return existing["id"]

    db.execute(
        """
        INSERT INTO intel_sources (
            category_id, title, short_name, publisher, url,
            document_type, update_frequency, nz_relevance_score,
            procurement_relevance, is_active, last_checked
        ) VALUES (%s, %s, %s, %s, %s, 'report', 'annual', 9,
                  %s, TRUE, NOW())
        """,
        (
            category_id,
            f"{agency['name']} Annual Procurement Plan {year}",
            source_name,
            agency["name"],
            url,
            agency.get("sector_tags") or [],
        ),
    )
    row = db.fetchone(
        "SELECT id FROM intel_sources WHERE short_name = %s",
        (source_name,),
    )
    return row["id"] if row else 0


def _upsert_snapshot(source_id: int, content: str) -> int:
    """Insert intel_snapshot, return snapshot id."""
    import hashlib
    version_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
    db.execute(
        """
        INSERT INTO intel_snapshots (source_id, snapshot_date, raw_text, version_hash, created_at)
        VALUES (%s, %s, %s, %s, NOW())
        """,
        (source_id, date.today().isoformat(), content[:50000], version_hash),
    )
    row = db.fetchone(
        "SELECT id FROM intel_snapshots WHERE source_id = %s ORDER BY created_at DESC LIMIT 1",
        (source_id,),
    )
    return row["id"] if row else 0


def _store_plan_signals(
    snapshot_id: int,
    source_id: int,
    signals: List[Dict],
    agency: Dict,
) -> int:
    """Insert procurement plan signals into intel_signals. Return count stored."""
    count = 0
    for sig in signals:
        if not isinstance(sig, dict):
            continue
        try:
            db.execute(
                """
                INSERT INTO intel_signals (
                    snapshot_id, source_id, signal_type,
                    signal_title, signal_body,
                    affected_sectors, affected_agencies,
                    timeframe, confidence, extracted_at,
                    agency, estimated_value_band, estimated_timeframe,
                    strategic_weight
                ) VALUES (
                    %s, %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s, NOW(),
                    %s, %s, %s,
                    %s
                )
                """,
                (
                    snapshot_id,
                    source_id,
                    sig.get("signal_type", "upcoming_contract"),
                    sig.get("title", "Untitled")[:200],
                    sig.get("signal_text", ""),
                    sig.get("sector_tags") or agency.get("sector_tags") or [],
                    [agency["name"]],
                    sig.get("estimated_timeframe"),
                    sig.get("confidence", "medium"),
                    sig.get("agency", agency["name"]),
                    sig.get("estimated_value_band", "Unknown"),
                    sig.get("estimated_timeframe"),
                    float(sig.get("strategic_weight", 1.0)),
                ),
            )
            count += 1
        except Exception as exc:
            logger.warning("Failed to store signal for %s: %s", agency.get("short"), exc)
    return count


# ── Main pipeline ─────────────────────────────────────────────────────────────

def ingest_agency_plan(agency: Dict, force_refresh: bool = False) -> Dict:
    """
    Full pipeline for one agency:
      1. Check 30-day cache (skip if fresh, unless force_refresh)
      2. Discover plan document URLs
      3. Download plan content
      4. Upsert intel_sources + intel_snapshots
      5. Extract signals via Claude
      6. Store signals in intel_signals
      7. Return summary dict

    Returns:
        {agency, plans_found, signals_extracted, status}
    """
    short = agency.get("short", agency.get("name", "?"))
    result: Dict = {
        "agency": agency["name"],
        "short": short,
        "plans_found": 0,
        "signals_extracted": 0,
        "status": "no_plan_found",
    }

    # Step 1 — cache check
    if not force_refresh:
        cached = _get_cached_source(short)
        if cached:
            logger.info("[%s] Cached plan found (last_checked within %d days) — skipping", short, _CACHE_DAYS)
            result["status"] = "cached"
            return result

    # Step 2 — discover plan URLs
    plan_urls = discover_plan_urls(agency)
    if not plan_urls:
        logger.warning("[%s] No plan URLs discovered", short)
        return result

    category_id = _get_plan_category_id()
    if not category_id:
        logger.error("[%s] intel_categories missing 'Agency Procurement Plans' row", short)
        result["status"] = "error"
        return result

    year = _CURRENT_YEAR
    total_signals = 0

    for url in plan_urls:
        logger.info("[%s] Downloading plan: %s", short, url[:80])

        # Step 3 — download content
        content = download_plan_content(url)
        if not content:
            logger.warning("[%s] No content extracted from %s", short, url[:80])
            continue

        result["plans_found"] += 1

        # Step 4 — upsert source + snapshot
        source_id = _upsert_source(category_id, agency, url, year)
        if not source_id:
            logger.error("[%s] Failed to upsert source record", short)
            continue

        snapshot_id = _upsert_snapshot(source_id, content)

        # Step 5 — extract signals
        signals = extract_procurement_signals(
            agency_name=agency["name"],
            agency_short=short,
            plan_text=content,
            sector_tags=agency.get("sector_tags") or [],
        )

        # Step 6 — store signals
        stored = _store_plan_signals(snapshot_id, source_id, signals, agency)
        total_signals += stored
        db.execute("UPDATE intel_sources SET last_checked = NOW() WHERE id = %s", (source_id,))

        logger.info("[%s] Plan ingested: %d signals from %s", short, stored, url[:70])

    result["signals_extracted"] = total_signals
    result["status"] = "success" if result["plans_found"] > 0 else "no_plan_found"
    return result


def run_all_agency_plans(force_refresh: bool = False) -> List[Dict]:
    """
    Run ingest_agency_plan() for all priority agencies plus any discovered from
    the central procurement plans index. 2-second delay between agencies.
    Logs and returns summary of all results.
    """
    all_agencies = list(PRIORITY_AGENCIES)

    # Discover additional agencies from central index
    try:
        discovered = _discover_from_central_index()
        known_names = {a["name"].lower() for a in all_agencies}
        for a in discovered:
            if a["name"].lower() not in known_names:
                all_agencies.append(a)
        logger.info(
            "run_all_agency_plans: %d total agencies (%d priority + %d discovered)",
            len(all_agencies), len(PRIORITY_AGENCIES), len(discovered),
        )
    except Exception as exc:
        logger.warning("Central index discovery failed: %s", exc)

    results: List[Dict] = []
    for i, agency in enumerate(all_agencies):
        if i > 0:
            time.sleep(_INTER_AGENCY_DELAY)
        try:
            summary = ingest_agency_plan(agency, force_refresh=force_refresh)
            results.append(summary)
            status = summary["status"]
            logger.info(
                "  [%s] %s — plans: %d, signals: %d",
                summary["short"], status,
                summary["plans_found"], summary["signals_extracted"],
            )
        except Exception as exc:
            logger.error("  [%s] unhandled error: %s", agency.get("short", "?"), exc)
            results.append({
                "agency": agency["name"],
                "short": agency.get("short", "?"),
                "plans_found": 0,
                "signals_extracted": 0,
                "status": "error",
            })

    # Log summary
    success_count = sum(1 for r in results if r["status"] == "success")
    total_signals = sum(r["signals_extracted"] for r in results)
    logger.info(
        "run_all_agency_plans complete: %d/%d agencies succeeded, %d total signals",
        success_count, len(results), total_signals,
    )
    return results


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Procurement Plan Scraper")
    parser.add_argument("--force",  action="store_true", help="Force refresh ignoring 30-day cache")
    parser.add_argument("--agency", metavar="SHORT", help="Run single agency by short name")
    args = parser.parse_args()

    if args.agency:
        match = next(
            (a for a in PRIORITY_AGENCIES if a["short"].lower() == args.agency.lower()),
            None,
        )
        if not match:
            print(f"Agency '{args.agency}' not found. Available: "
                  + ", ".join(a["short"] for a in PRIORITY_AGENCIES))
            sys.exit(1)
        summary = ingest_agency_plan(match, force_refresh=args.force)
        print(json.dumps(summary, indent=2))
    else:
        results = run_all_agency_plans(force_refresh=args.force)
        print(json.dumps(results, indent=2))
