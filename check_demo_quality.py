"""
Demo artefact quality checker.

Reads output/artefacts/demo/manifest.json, opens every generated HTML file,
and prints:
  - Tender title, agency, sector, win position band, go/no-go verdict
  - Executive summary (first 400 chars)
  - Red flags and strategic framing (from enrichment, embedded in HTML)
  - ACH hypotheses
  - Bidder list with strategic importance
  - Cross-contamination warnings (firm name appearing in wrong sector's file)
  - Empty / placeholder field warnings

Usage (local, Railway env):
  railway run python check_demo_quality.py

Or directly if DATABASE_URL is in your shell:
  python check_demo_quality.py
"""
import json
import re
import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("ERROR: beautifulsoup4 not installed. Run: pip install beautifulsoup4")

ROOT = Path(__file__).parent
MANIFEST_PATH = ROOT / "output" / "artefacts" / "demo" / "manifest.json"


def _storage_download(storage_path: str) -> bytes | None:
    """Download from Supabase Storage; returns None if unavailable."""
    try:
        import storage as _st
        return _st.download_file(storage_path)
    except Exception:
        return None


def _ensure_manifest() -> None:
    """Download manifest.json from Storage if the local copy is missing."""
    if MANIFEST_PATH.exists():
        return
    print("Local manifest not found — trying Supabase Storage...")
    data = _storage_download("demo/manifest.json")
    if data:
        MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST_PATH.write_bytes(data)
        print(f"  Downloaded manifest ({len(data)} bytes)")
    else:
        print("  Storage download also failed — no credentials or bucket empty")


def _ensure_html(html_rel: str) -> Path | None:
    """Return local path for an artefact, downloading from Storage if needed."""
    local = ROOT / html_rel
    if local.exists():
        return local
    # Storage path mirrors html_rel but rooted at demo/
    # html_rel looks like:  output/artefacts/demo/<sector>/<file>.html
    # Storage path:         demo/<sector>/<file>.html
    try:
        parts = Path(html_rel).parts
        demo_idx = next(i for i, p in enumerate(parts) if p == "demo")
        storage_path = "/".join(parts[demo_idx:])
    except (StopIteration, ValueError):
        return None
    data = _storage_download(storage_path)
    if data:
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)
        return local
    return None

# Firm name → home sector. Used for cross-contamination checks.
FIRM_SECTOR = {
    "Sentinel Digital":       "cybersecurity",
    "Cityworks NZ":           "FM",
    "Meridian Civil":         "construction",
    "Apex Engineering":       "defence",
    "Korepath Systems":       "ICT",
    "Southern Civil Group":   "infrastructure",
    "MedTech Solutions NZ":   "health",
}

PLACEHOLDERS = [
    "lorem ipsum", "placeholder", "tbd", "n/a", "not available",
    "enrichment not available", "none identified", "no data",
    "coming soon", "todo", "[insert", "example text",
]

SEP = "─" * 80

def _txt(el) -> str:
    """Strip and normalise whitespace from a BS4 element's text."""
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else ""

def _first(soup, *selectors) -> str:
    """Return text of the first matching selector, or empty string."""
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            t = _txt(el)
            if t:
                return t
    return ""

def _is_placeholder(text: str) -> bool:
    low = text.lower().strip()
    if not low:
        return True
    for p in PLACEHOLDERS:
        if p in low:
            return True
    return False

def _check_contamination(html_text: str, home_sector: str) -> list[str]:
    """Return list of warning strings for firm names found in wrong-sector file."""
    warnings = []
    for firm, sector in FIRM_SECTOR.items():
        if sector == home_sector:
            continue
        if firm.lower() in html_text.lower():
            warnings.append(f"  ⚠  CROSS-CONTAMINATION: '{firm}' ({sector}) found in {home_sector} artefact")
    return warnings

def review_pursuit(path: Path, sector: str) -> None:
    html = path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "lxml")

    print(f"\n  {'Pursuit Package':─<70}")
    print(f"  File: {path.name}")

    # Cover fields
    title   = _first(soup, ".cover-title")
    agency  = _first(soup, ".cover-agency")
    client  = _first(soup, ".cover-client")
    wp      = _first(soup, ".prob-pct")
    verdict = _first(soup, ".verdict-badge")
    rationale = _first(soup, ".verdict-text")

    print(f"  Tender:   {title or '⚠ EMPTY'}")
    print(f"  Agency:   {agency or '⚠ EMPTY'}")
    print(f"  Client:   {client or '⚠ EMPTY'}")
    print(f"  Win pos:  {wp or '⚠ EMPTY'}  |  Verdict: {verdict or '⚠ EMPTY'}")

    if _is_placeholder(title):
        print("  ⚠ PLACEHOLDER: tender title")
    if _is_placeholder(wp):
        print("  ⚠ PLACEHOLDER: win position")

    print(f"  Rationale: {rationale[:200] or '⚠ EMPTY'}")

    # Executive summary (#exec section)
    exec_sec = soup.select_one("#exec")
    exec_text = _txt(exec_sec)[:500] if exec_sec else ""
    print(f"\n  [Executive Summary]\n  {exec_text or '⚠ EMPTY'}")
    if _is_placeholder(exec_text):
        print("  ⚠ PLACEHOLDER in executive summary")

    # Red flags + strategic framing — stored in enrichment sidebar or section
    # They appear as labelled content; search by common wrapper patterns
    rf_el  = soup.find(string=re.compile(r"red.?flag", re.I))
    sf_el  = soup.find(string=re.compile(r"strategic.?framing", re.I))
    rf_ctx = _txt(rf_el.parent.parent) if rf_el and rf_el.parent else ""
    sf_ctx = _txt(sf_el.parent.parent) if sf_el and sf_el.parent else ""
    print(f"\n  [Red Flags]")
    print(f"  {rf_ctx[:300] or '(not found as labelled section — may be embedded in narrative)'}")
    print(f"\n  [Strategic Framing]")
    print(f"  {sf_ctx[:300] or '(not found as labelled section — may be embedded in narrative)'}")

    # ACH hypotheses (#cog section)
    cog_sec  = soup.select_one("#cog")
    hyp_els  = cog_sec.select(".cog-hyp, .hyp, [class*=hyp]") if cog_sec else []
    if not hyp_els and cog_sec:
        # fallback: grab all text rows
        hyp_els = cog_sec.select("td, li, p")[:6]
    print(f"\n  [ACH Hypotheses — {len(hyp_els)} found]")
    for h in hyp_els[:5]:
        t = _txt(h)
        if t and len(t) > 10:
            print(f"  • {t[:160]}")
    if not hyp_els:
        # Try to get any text from #cog
        cog_text = _txt(cog_sec)[:400] if cog_sec else ""
        print(f"  {cog_text or '⚠ ACH section empty or missing'}")

    # Bidders (#assessment or #competitive section)
    bid_sec = soup.select_one("#assessment") or soup.select_one("#competitive")
    bid_items = bid_sec.select(".bidder, .firm, tr") if bid_sec else []
    print(f"\n  [Bidders — {len(bid_items)} rows in section]")
    for b in bid_items[:5]:
        t = _txt(b)
        if t and len(t) > 5:
            print(f"  • {t[:140]}")
    if not bid_items:
        bid_text = _txt(bid_sec)[:300] if bid_sec else ""
        print(f"  {bid_text or '⚠ Bidder section empty or missing'}")

    # Cross-contamination check
    warnings = _check_contamination(html, sector)
    for w in warnings:
        print(w)


def review_competitor(path: Path, sector: str) -> None:
    html = path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "lxml")

    print(f"\n  {'Competitor Profile':─<70}")
    print(f"  File: {path.name}")

    # Header: competitor name and client
    h1 = _first(soup, "h1", ".comp-title", ".cover-title", ".ptitle")
    sub = _first(soup, ".cover-agency", ".comp-sub", ".psub")
    print(f"  Competitor: {h1 or '⚠ EMPTY'}")
    print(f"  Context:    {sub or '(none)'}")

    # Win summary block
    win_block = soup.select_one(".win-summary, .stats, .summary-block")
    win_text  = _txt(win_block)[:300] if win_block else ""
    # Fallback: first substantial paragraph
    if not win_text:
        for p in soup.select("p"):
            t = _txt(p)
            if len(t) > 80:
                win_text = t[:300]
                break
    print(f"\n  [Win Summary]\n  {win_text or '⚠ EMPTY'}")
    if _is_placeholder(win_text):
        print("  ⚠ PLACEHOLDER in win summary")

    # Sector strength and agency relationships — look for tables or labelled sections
    tables = soup.select("table")
    print(f"\n  [Tables found: {len(tables)}]")
    for i, tbl in enumerate(tables[:3], 1):
        rows = tbl.select("tr")
        print(f"  Table {i} ({len(rows)} rows):")
        for row in rows[:4]:
            print(f"    {_txt(row)[:120]}")

    # Body content (first 600 chars)
    body_paras = [_txt(p) for p in soup.select("p, li") if len(_txt(p)) > 50]
    body_text  = " | ".join(body_paras[:4])
    print(f"\n  [Body excerpt]\n  {body_text[:500] or '⚠ EMPTY'}")

    warnings = _check_contamination(html, sector)
    for w in warnings:
        print(w)


def review_watch_brief(path: Path, sector: str) -> None:
    html = path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "lxml")

    print(f"\n  {'Watch Brief':─<70}")
    print(f"  File: {path.name}")

    title  = _first(soup, "h1", ".brief-title", ".ptitle", "title")
    client = _first(soup, ".brief-client", ".cover-client")
    print(f"  Title:  {title[:120] or '⚠ EMPTY'}")
    print(f"  Client: {client[:80] or '(none)'}")

    # Opportunity cards / rows
    opp_els = soup.select(".opp-card, .opp-row, .opp-title, .notice-title")
    if not opp_els:
        # Fallback: look for h3/h4 which typically name the opportunities
        opp_els = soup.select("h3, h4")
    print(f"\n  [Opportunities listed: {len(opp_els)}]")
    for o in opp_els[:6]:
        t = _txt(o)
        if t and len(t) > 10:
            print(f"  • {t[:120]}")

    # Awards section
    award_els = soup.select(".award, .contract-award")
    print(f"\n  [Awards listed: {len(award_els)}]")
    for a in award_els[:4]:
        print(f"  • {_txt(a)[:120]}")

    # Market signals / strategic commentary
    sig_els = soup.select(".signal, .market-signal, .intel-signal")
    print(f"\n  [Market signals: {len(sig_els)}]")
    for s in sig_els[:3]:
        print(f"  • {_txt(s)[:160]}")

    # Body excerpt
    body_paras = [_txt(p) for p in soup.select("p") if len(_txt(p)) > 60]
    body_text  = " | ".join(body_paras[:3])
    print(f"\n  [Body excerpt]\n  {body_text[:500] or '⚠ EMPTY'}")

    if _is_placeholder(body_text):
        print("  ⚠ PLACEHOLDER in body")

    warnings = _check_contamination(html, sector)
    for w in warnings:
        print(w)


def main() -> None:
    _ensure_manifest()

    if not MANIFEST_PATH.exists():
        sys.exit(f"ERROR: manifest not found at {MANIFEST_PATH}\n"
                 "Run generate_demo_content.py first, or check ARTEFACTS_DIR.")

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    sectors  = manifest.get("sectors", {})
    generated = manifest.get("generated", "unknown")

    print(f"\n{'═'*80}")
    print(f"  DEMO ARTEFACT QUALITY REVIEW")
    print(f"  Generated: {generated}   Sectors in manifest: {len(sectors)}")
    print(f"{'═'*80}")

    for sector_key, sdata in sectors.items():
        firm  = sdata.get("firm", {})
        items = sdata.get("items", [])

        print(f"\n{SEP}")
        print(f"SECTOR: {sector_key.upper()}  —  Firm: {firm.get('name', '⚠ MISSING')}")
        print(f"Staff: {firm.get('staff','?')} | Location: {firm.get('location','?')} | Yrs: {firm.get('years_operating','?')}")
        print(f"Artefacts in manifest: {len(items)}")
        print(SEP)

        if not items:
            print("  ⚠ NO ARTEFACTS GENERATED FOR THIS SECTOR")
            continue

        for item in items:
            item_type = item.get("type", "unknown")
            html_rel  = item.get("html_path", "")
            html_path = _ensure_html(html_rel) if html_rel else None

            if not html_path or not html_path.exists():
                print(f"\n  ⚠ FILE MISSING: {html_rel or '(no path in manifest)'}")
                continue

            size_kb = html_path.stat().st_size / 1024
            print(f"\n  [File size: {size_kb:.1f} KB]")

            if size_kb < 5:
                print(f"  ⚠ FILE SUSPICIOUSLY SMALL ({size_kb:.1f} KB) — may be empty or errored")

            if item_type == "pursuit_package":
                review_pursuit(html_path, sector_key)
            elif item_type == "competitor_profile":
                review_competitor(html_path, sector_key)
            elif item_type == "watch_brief":
                review_watch_brief(html_path, sector_key)
            else:
                print(f"  Unknown type: {item_type}")

    # Global cross-check: scan ALL artefact HTML files for each firm name,
    # confirm it only appears in its own sector's files.
    print(f"\n{'═'*80}")
    print("  GLOBAL CROSS-CONTAMINATION SCAN")
    print(f"{'═'*80}")
    demo_dir = ROOT / "output" / "artefacts" / "demo"
    all_html = list(demo_dir.rglob("*.html"))
    print(f"  Total HTML files scanned: {len(all_html)}")
    contamination_found = False
    for firm, home_sector in FIRM_SECTOR.items():
        for html_file in all_html:
            # Determine which sector this file belongs to
            try:
                file_sector = html_file.relative_to(demo_dir).parts[0]
            except Exception:
                file_sector = "unknown"
            if file_sector == home_sector:
                continue
            content = html_file.read_text(encoding="utf-8", errors="ignore")
            if firm.lower() in content.lower():
                print(f"  ⚠ CONTAMINATION: '{firm}' (home: {home_sector}) appears in {file_sector}/{html_file.name}")
                contamination_found = True
    if not contamination_found:
        print("  ✓ No cross-sector firm name contamination detected")

    print(f"\n{'═'*80}")
    print("  REVIEW COMPLETE")
    print(f"{'═'*80}\n")


if __name__ == "__main__":
    main()
