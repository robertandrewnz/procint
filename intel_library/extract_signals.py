"""
intel_library/extract_signals.py — Fetch documents and extract procurement signals.

For each active intel_source:
  1. Fetch content from URL (requests + BeautifulSoup) or PDF (pdfplumber)
  2. Compute SHA-256 hash; skip if unchanged
  3. Pass content to Claude for structured signal extraction
  4. Store intel_snapshot + intel_signals
  5. Record intel_source_usage

Usage:
    # Process all active sources (weekly run)
    python intel_library/extract_signals.py --all

    # Process a single source by short_name or title fragment
    python intel_library/extract_signals.py --source BEFU2026

    # Process only Budget 2026 sources (highest priority)
    python intel_library/extract_signals.py --budget

    # Force re-extraction even if content unchanged
    python intel_library/extract_signals.py --all --force

    # Process Beehive daily sources only
    python intel_library/extract_signals.py --daily
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from datetime import date
from pathlib import Path
from typing import Optional

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic
import requests
from bs4 import BeautifulSoup

import config
import db

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)

# ── Highest-priority source short names (signals weighted 1.5x) ───────────────
HIGH_PRIORITY_SHORT_NAMES = {"BEFU2026", "Budget2026-Full", "FSR2026"}

# ── Signal extraction prompt ──────────────────────────────────────────────────

SIGNAL_SYSTEM = (
    "You are a procurement intelligence analyst specialising in NZ government contracting. "
    "Return ONLY valid JSON — no preamble, no markdown fences."
)

SIGNAL_USER_TEMPLATE = """\
You are a procurement intelligence analyst specialising in NZ government contracting.
Given this document content, extract structured procurement signals for organisations
competing for NZ government contracts.

Return JSON with exactly this structure:
{{
  "summary": "2-3 sentence summary of the document's procurement relevance",
  "signals": [
    {{
      "signal_type": "budget_increase|policy_change|new_initiative|risk|opportunity",
      "signal_title": "Short title (max 12 words)",
      "signal_body": "2-3 sentences. Specific. Use actual figures where available.",
      "affected_sectors": ["sector1", "sector2"],
      "affected_agencies": ["Agency1", "Agency2"],
      "dollar_value": 12345678,
      "timeframe": "2026-2030 or similar",
      "confidence": "high|medium|low"
    }}
  ]
}}

Focus ONLY on signals indicating:
- Where government will spend money on contracts
- How procurement will be structured (panels, open tender, sole source)
- Which sectors will grow or shrink in government contracting activity
- New mandatory requirements that create contracting demand
- Shifts in competitive dynamics (new entrants, panel changes, supplier risk)
- Macro factors affecting contract pricing or delivery (oil, inflation, supply chain)

Ignore political content with no procurement implication. Extract 3-8 signals maximum.

--- SOURCE ---
Title: {title}
Publisher: {publisher}
Document type: {document_type}
Notes: {notes}

--- CONTENT ---
{content}
"""

# ── PDF extraction ─────────────────────────────────────────────────────────────

def _extract_pdf_text(url: str, max_chars: int = 40000) -> Optional[str]:
    """Download and extract text from a PDF URL using pdfplumber."""
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber not installed — cannot extract PDF. Run: pip install pdfplumber")
        return None

    try:
        logger.info("Downloading PDF: %s", url)
        resp = requests.get(url, timeout=config.REQUEST_TIMEOUT, stream=True)
        resp.raise_for_status()
        import io
        pdf_bytes = io.BytesIO(resp.content)
        with pdfplumber.open(pdf_bytes) as pdf:
            pages = []
            total = 0
            for page in pdf.pages:
                text = page.extract_text() or ""
                pages.append(text)
                total += len(text)
                if total >= max_chars:
                    break
            return "\n\n".join(pages)[:max_chars]
    except Exception as exc:
        logger.warning("PDF extraction failed for %s: %s", url, exc)
        return None


# ── HTML scraping ──────────────────────────────────────────────────────────────

def _fetch_html_text(url: str, max_chars: int = 30000) -> Optional[str]:
    """Fetch a web page and extract readable text."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; BidEdge-Intel/1.0)"}
        resp = requests.get(url, timeout=config.REQUEST_TIMEOUT, headers=headers)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        # Remove navigation, header, footer, script, style
        for tag in soup(["script", "style", "nav", "header", "footer",
                          "aside", "form", "noscript"]):
            tag.decompose()
        # Get main content
        main = soup.find("main") or soup.find("article") or soup.find("div", class_="content")
        text = (main or soup).get_text(separator="\n", strip=True)
        return text[:max_chars]
    except Exception as exc:
        logger.warning("HTML fetch failed for %s: %s", url, exc)
        return None


# ── Beehive scraping ──────────────────────────────────────────────────────────

def _fetch_beehive_recent(url: str, max_items: int = 20) -> Optional[str]:
    """
    Scrape Beehive press releases or speeches (RSS-style listing).
    Returns concatenated text of recent items.
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; BidEdge-Intel/1.0)"}
        resp = requests.get(url, timeout=config.REQUEST_TIMEOUT, headers=headers)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        items = []
        # Each press release is a article or list item with heading + summary
        for item in soup.find_all(["article", "li"], limit=max_items * 3):
            heading = item.find(["h2", "h3", "h4"])
            excerpt = item.find(class_=lambda c: c and any(
                k in c.lower() for k in ["excerpt", "summary", "intro", "teaser"]
            ))
            if heading:
                text = heading.get_text(strip=True)
                if excerpt:
                    text += " — " + excerpt.get_text(strip=True)
                items.append(text)
            if len(items) >= max_items:
                break
        return "\n\n".join(items) if items else _fetch_html_text(url)
    except Exception as exc:
        logger.warning("Beehive fetch failed: %s", exc)
        return None


# ── Content fetcher ────────────────────────────────────────────────────────────

def _fetch_content(source: dict) -> Optional[str]:
    """Fetch content for a source, choosing strategy based on type/URL."""
    url = source.get("url") or ""
    pdf_url = source.get("pdf_url") or ""
    doc_type = source.get("document_type", "")
    notes = source.get("notes") or ""

    # Try PDF first if available
    if pdf_url:
        text = _extract_pdf_text(pdf_url)
        if text and len(text) > 200:
            return text

    # Beehive sources
    if "beehive.govt.nz" in url:
        return _fetch_beehive_recent(url)

    # All other URLs
    if url:
        text = _fetch_html_text(url)
        if text and len(text) > 200:
            return text

    # Fallback: use document description/notes for Claude to work from
    if notes:
        logger.info("Using notes/description as content for source: %s", source.get("title", "")[:60])
        return f"[Document description — no direct content fetched]\n\n{notes}"

    logger.warning("No fetchable content for source: %s", source.get("title", "")[:60])
    return None


# ── Claude signal extraction ──────────────────────────────────────────────────

def _extract_signals_via_claude(source: dict, content: str) -> Optional[dict]:
    """Call Claude to extract signals. Returns parsed JSON dict or None."""
    prompt = SIGNAL_USER_TEMPLATE.format(
        title=source.get("title", ""),
        publisher=source.get("publisher", ""),
        document_type=source.get("document_type", ""),
        notes=source.get("notes") or "None",
        content=content[:25000],  # hard cap to control tokens
    )

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        msg = client.messages.create(
            model=config.CLAUDE_MODEL_L3,
            max_tokens=2000,
            system=SIGNAL_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Claude returned invalid JSON for '%s': %s", source.get("title", "")[:50], exc)
        return None
    except Exception as exc:
        logger.error("Claude extraction failed for '%s': %s", source.get("title", "")[:50], exc)
        return None


# ── Snapshot storage ──────────────────────────────────────────────────────────

def _store_snapshot(source_id: int, content: str, extracted: dict) -> int:
    """Insert intel_snapshot, return new snapshot id."""
    version_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
    summary = extracted.get("summary", "")
    key_signals = json.dumps({"signals": extracted.get("signals", [])})

    db.execute(
        """
        INSERT INTO intel_snapshots (source_id, snapshot_date, raw_text, summary,
                                     key_signals, version_hash, created_at)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s, NOW())
        """,
        (
            source_id,
            date.today().isoformat(),
            content[:50000],
            summary,
            key_signals,
            version_hash,
        ),
    )
    row = db.fetchone(
        "SELECT id FROM intel_snapshots WHERE source_id = %s ORDER BY created_at DESC LIMIT 1",
        (source_id,),
    )
    return row["id"] if row else 0


def _store_signals(snapshot_id: int, source_id: int, signals: list) -> int:
    """Insert extracted signals, return count stored."""
    count = 0
    for sig in signals:
        if not isinstance(sig, dict):
            continue
        try:
            dollar_raw = sig.get("dollar_value")
            dollar_value = int(dollar_raw) if dollar_raw is not None else None
        except (ValueError, TypeError):
            dollar_value = None

        db.execute(
            """
            INSERT INTO intel_signals (
                snapshot_id, source_id, signal_type, signal_title, signal_body,
                affected_sectors, affected_agencies, dollar_value, timeframe,
                confidence, extracted_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            """,
            (
                snapshot_id,
                source_id,
                sig.get("signal_type", "opportunity"),
                sig.get("signal_title", "Untitled signal")[:200],
                sig.get("signal_body", ""),
                sig.get("affected_sectors") or [],
                sig.get("affected_agencies") or [],
                dollar_value,
                sig.get("timeframe"),
                sig.get("confidence", "medium"),
            ),
        )
        count += 1
    return count


def _record_usage(source_id: int, snapshot_id: int, sig_count: int) -> None:
    """Record that this source was processed."""
    db.execute(
        """
        INSERT INTO intel_source_usage (source_id, used_in, usage_type, significance_score, used_at)
        VALUES (%s, %s, 'signal_extracted', %s, NOW())
        """,
        (source_id, f"snapshot:{snapshot_id}", min(sig_count + 3, 10)),
    )
    db.execute(
        "UPDATE intel_sources SET last_checked = NOW() WHERE id = %s",
        (source_id,),
    )


# ── Version check ─────────────────────────────────────────────────────────────

def _content_changed(source_id: int, content: str) -> bool:
    """True if content hash differs from the latest snapshot."""
    new_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
    row = db.fetchone(
        "SELECT version_hash FROM intel_snapshots WHERE source_id = %s ORDER BY created_at DESC LIMIT 1",
        (source_id,),
    )
    if not row:
        return True
    return row.get("version_hash") != new_hash


# ── Main processor ────────────────────────────────────────────────────────────

def process_source(source: dict, force: bool = False) -> bool:
    """
    Process a single source: fetch, extract signals, store.
    Returns True on success.
    """
    source_id = source["id"]
    title = source.get("title", "")[:70]

    logger.info("Processing: %s", title)

    # Fetch content
    content = _fetch_content(source)
    if not content:
        logger.warning("No content retrieved for: %s", title)
        return False

    # Skip if unchanged (unless forced)
    if not force and not _content_changed(source_id, content):
        logger.info("Content unchanged — skipping: %s", title)
        return True

    # Extract signals
    extracted = _extract_signals_via_claude(source, content)
    if not extracted:
        logger.warning("Signal extraction failed for: %s", title)
        return False

    signals = extracted.get("signals") or []
    logger.info("Extracted %d signals from: %s", len(signals), title)

    # Store
    snapshot_id = _store_snapshot(source_id, content, extracted)
    if not snapshot_id:
        logger.error("Failed to store snapshot for: %s", title)
        return False

    sig_count = _store_signals(snapshot_id, source_id, signals)
    _record_usage(source_id, snapshot_id, sig_count)

    logger.info("Stored snapshot %s with %d signals for: %s", snapshot_id, sig_count, title)
    return True


def process_all_sources(
    force: bool = False,
    budget_only: bool = False,
    daily_only: bool = False,
    source_filter: Optional[str] = None,
    delay_seconds: float = 1.5,
) -> dict:
    """
    Process active sources with optional filters.

    Args:
        force: Re-extract even if content unchanged.
        budget_only: Only process Budget 2026 / BEFU sources.
        daily_only: Only process daily-refresh sources (Beehive, NCSC news).
        source_filter: Short name or title fragment to filter to one source.
        delay_seconds: Pause between sources to avoid rate-limiting.

    Returns:
        {"processed": n, "succeeded": n, "failed": n}
    """
    query = "SELECT * FROM intel_sources WHERE is_active = TRUE"
    params = []

    if budget_only:
        query += " AND (short_name = ANY(%s))"
        params.append(list(HIGH_PRIORITY_SHORT_NAMES))
    elif daily_only:
        query += " AND update_frequency IN ('daily', 'weekly')"
    elif source_filter:
        query += " AND (short_name ILIKE %s OR title ILIKE %s)"
        params.extend([f"%{source_filter}%", f"%{source_filter}%"])

    # Always process high-priority sources first
    query += " ORDER BY nz_relevance_score DESC NULLS LAST, id ASC"

    sources = db.fetchall(query, params if params else None)
    logger.info("Found %d sources to process", len(sources))

    stats = {"processed": 0, "succeeded": 0, "failed": 0}

    for source in sources:
        stats["processed"] += 1
        try:
            ok = process_source(source, force=force)
            if ok:
                stats["succeeded"] += 1
            else:
                stats["failed"] += 1
        except Exception as exc:
            logger.error("Unhandled error processing '%s': %s", source.get("title", "")[:50], exc)
            stats["failed"] += 1

        if delay_seconds > 0:
            time.sleep(delay_seconds)

    logger.info(
        "Processing complete. Processed: %d, Succeeded: %d, Failed: %d",
        stats["processed"], stats["succeeded"], stats["failed"],
    )
    return stats


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Intel Library — Signal Extractor")
    parser.add_argument("--all",     action="store_true", help="Process all active sources")
    parser.add_argument("--budget",  action="store_true", help="Process Budget 2026 sources only")
    parser.add_argument("--daily",   action="store_true", help="Process daily-refresh sources (Beehive, news)")
    parser.add_argument("--source",  metavar="NAME", help="Filter to one source by short_name or title fragment")
    parser.add_argument("--force",   action="store_true", help="Re-extract even if content unchanged")
    parser.add_argument("--delay",   type=float, default=1.5, help="Seconds between requests (default 1.5)")
    args = parser.parse_args()

    if not any([args.all, args.budget, args.daily, args.source]):
        parser.error("Specify --all, --budget, --daily, or --source NAME")

    result = process_all_sources(
        force=args.force,
        budget_only=args.budget,
        daily_only=args.daily,
        source_filter=args.source,
        delay_seconds=args.delay,
    )
    sys.exit(0 if result["failed"] == 0 else 1)
