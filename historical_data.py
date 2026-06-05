"""
MBIE GETS Open Data — historical procurement ingestion module.

Downloads and loads NZ government contract award data from MBIE's GETS open
data portal (2014–2025) into the database, then builds the supplier_win_history
materialised view used for evidence-based bidder inference.

Data sources (downloaded to data/mbie/):
  GETS-award-notices.csv          — current dataset (~20K rows, utf-8-sig)
  GETS-award-notices-historic.csv — historic dataset (~7K rows, cp1252)
  GETS-supplier-data.csv          — current suppliers (~22K rows)
  GETS-supplier-data-historic.csv — historic suppliers (~7K rows, cp1252)
  GETS-product-categories.csv     — UNSPSC category tags (~35K rows)
  GETS-region-by-tender.csv       — geographic regions (~19K rows)

Key join: RFx ID (called rfx_id in our tables) links all files.

"Awarded" detection (data quality note):
  - Current file: Award Type is unreliable (all 'Not Awarded'); use
    Awarded Amount > 0 as the award signal.
  - Historic file: Award Type = 'Awarded' is reliable; Awarded Amount column
    absent, so any row with Award Type = 'Awarded' is treated as awarded.
"""
import csv
import logging
import os
import re
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import requests
import psycopg2.extras

import config
import db

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

MBIE_DIR = Path("data/mbie")

MBIE_FILES = {
    "award_notices":         "GETS-award-notices.csv",
    "award_notices_historic":"GETS-award-notices-historic.csv",
    "supplier_data":         "GETS-supplier-data.csv",
    "supplier_data_historic":"GETS-supplier-data-historic.csv",
    "product_categories":    "GETS-product-categories.csv",
    "region_by_tender":      "GETS-region-by-tender.csv",
}

MBIE_URLS = {
    "award_notices":
        "https://www.mbie.govt.nz/assets/Data-Files/NZGPP-GETS-Open-Data/GETS-award-notices.csv",
    "award_notices_historic":
        "https://www.mbie.govt.nz/assets/Data-Files/NZGPP-GETS-Open-Data/GETS-award-notices-historic.csv",
    "supplier_data":
        "https://www.mbie.govt.nz/assets/Data-Files/NZGPP-GETS-Open-Data/GETS-supplier-data.csv",
    "supplier_data_historic":
        "https://www.mbie.govt.nz/assets/Data-Files/NZGPP-GETS-Open-Data/GETS-supplier-data-historic.csv",
    "product_categories":
        "https://www.mbie.govt.nz/assets/Data-Files/NZGPP-GETS-Open-Data/GETS-product-categories.csv",
    "region_by_tender":
        "https://www.mbie.govt.nz/assets/Data-Files/NZGPP-GETS-Open-Data/GETS-region-by-tender.csv",
}


# ── UNSPSC → sector taxonomy mapping ─────────────────────────────────────────
# Maps UNSPSC family codes (first 2 digits) and key description substrings
# to our sector taxonomy.

_UNSPSC_SECTOR_MAP = [
    # Infrastructure / construction / roading
    ("infrastructure", [
        "road", "highway", "roading", "bridge", "civil engineering", "civil eng",
        "construction", "infrastructure building", "surfacing", "paving",
        "structures and building", "building and construction",
        "land and buildings", "water and sewer", "pipeline",
        "earthworks", "geotechnical",
    ]),
    # FM / facilities
    ("FM", [
        "building and facility construction and maintenance",
        "facility management", "facilities management",
        "cleaning", "catering", "grounds", "waste collection", "pest control",
        "property management", "landscaping", "mowing",
    ]),
    # ICT
    ("ICT", [
        "information technology", "broadcasting and telecommunications",
        "software", "cloud", "cyber", "digital", "data management",
        "network", "telecommunications", "computer",
    ]),
    # Advisory / professional services
    ("advisory", [
        "management and business professionals", "professional services",
        "consulting", "advisory", "research and technology",
        "audit", "accounting", "finance", "legal", "human resources",
        "management advisory", "strategy",
    ]),
    # Health
    ("health", [
        "healthcare services", "medical", "pharmaceutical", "laboratory",
        "health services", "mental health", "dental", "hospital",
        "aged care", "rehabilitation", "ambulance",
    ]),
    # Security
    ("security", [
        "security", "guarding", "protective services", "surveillance",
        "access control",
    ]),
    # Defence
    ("defence", [
        "defence", "military", "naval", "air force",
        "weapons", "ammunition", "armaments",
    ]),
    # Utilities / energy
    ("utilities", [
        "power generation", "electricity", "energy",
        "water and sewer utilities", "gas",
        "public utilities", "waste management",
    ]),
    # Education / training
    ("professional_services", [
        "education and training", "training services", "recruitment",
        "employment", "legal services",
    ]),
]


def _map_unspsc_to_sector(desc: str) -> Optional[str]:
    """Map UNSPSC description text to our sector taxonomy."""
    if not desc or desc == "NULL":
        return None
    desc_lower = desc.lower()
    for sector, keywords in _UNSPSC_SECTOR_MAP:
        for kw in keywords:
            if kw in desc_lower:
                return sector
    return "other"


# ── Date parsing ──────────────────────────────────────────────────────────────

def _parse_date(val: str) -> Optional[date]:
    if not val or val.strip() in ("", "NULL"):
        return None
    val = val.strip()
    for fmt in ("%d/%m/%Y", "%Y%m%d", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(val, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(val: str) -> Optional[float]:
    if not val or val.strip() in ("", "NULL", "0"):
        return None
    try:
        cleaned = re.sub(r"[^\d.]", "", val)
        v = float(cleaned)
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


# ── File download ─────────────────────────────────────────────────────────────

def download_files(force: bool = False) -> None:
    """
    Download MBIE CSV files to data/mbie/.
    Skips files already present unless force=True.
    """
    MBIE_DIR.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "ProcintBot/1.0 (procurement intelligence; NZ govt open data)"}

    for key, fname in MBIE_FILES.items():
        dest = MBIE_DIR / fname
        if dest.exists() and not force:
            logger.info("Already downloaded: %s (%s)", fname, _human_size(dest.stat().st_size))
            continue

        url = MBIE_URLS[key]
        logger.info("Downloading %s ...", fname)
        try:
            resp = requests.get(url, headers=headers, timeout=120, stream=True)
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
            logger.info("Downloaded %s → %s", fname, _human_size(dest.stat().st_size))
        except Exception as exc:
            logger.error("Failed to download %s: %s", fname, exc)
            raise


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


# ── CSV readers ───────────────────────────────────────────────────────────────

def _read_csv(fname: str, encoding: str = "utf-8-sig") -> list[dict]:
    path = MBIE_DIR / fname
    with open(path, newline="", encoding=encoding) as f:
        return list(csv.DictReader(f))


# ── Schema application ────────────────────────────────────────────────────────

def apply_schema() -> None:
    """Create MBIE tables if they don't exist."""
    with open("migrations/003_mbie_historical_data.sql") as f:
        sql = f.read()
    # Execute statements separated by semicolons
    for stmt in re.split(r";\s*\n", sql):
        stmt = stmt.strip()
        if stmt:
            try:
                db.execute(stmt + ";")
            except Exception as exc:
                # Materialised view "already exists" is OK when re-running
                if "already exists" in str(exc):
                    logger.debug("Already exists: %s", str(exc)[:80])
                else:
                    raise
    logger.info("MBIE schema applied")


# ── Load award notices ────────────────────────────────────────────────────────


# ── Batch insert helper ───────────────────────────────────────────────────────

def _batch_execute(sql: str, rows: list, page_size: int = 1000) -> None:
    """
    Execute a bulk insert using psycopg2.extras.execute_values with pagination.
    Splits into page_size chunks to avoid Supabase connection timeout on large loads.
    """
    for i in range(0, len(rows), page_size):
        chunk = rows[i:i + page_size]
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, sql, chunk, page_size=page_size)


def _load_award_notices_current(rows: list[dict]) -> int:
    """Load GETS-award-notices.csv using batched execute_values."""
    batch = []
    for r in rows:
        rfx_id = r.get("RFx ID", "").strip()
        if not rfx_id:
            continue
        amount = _parse_amount(r.get("Awarded Amount", ""))
        is_awarded = amount is not None and amount > 0
        batch.append((
            rfx_id,
            r.get("Posting Agency", "").strip() or None,
            r.get("RFx Type", "").strip() or None,
            r.get("Competition Type", "").strip() or None,
            r.get("Title", "").strip() or None,
            r.get("Reference Number", "").strip() or None,
            _parse_date(r.get("Open Date", "")),
            _parse_date(r.get("Close Date", "")),
            _parse_date(r.get("Awarded Date ", "")),
            r.get("Department", "").strip() or None,
            r.get("Tender Coverage", "").strip() or None,
            (r.get("Overview", "") or "").strip()[:2000] or None,
            r.get("Award Type", "").strip() or None,
            amount,
            is_awarded,
            "current",
            _parse_date(r.get("Report Date", "")),
        ))
    if not batch:
        return 0
    _batch_execute(
        """
        INSERT INTO mbie_award_notices
            (rfx_id, posting_agency, rfx_type, competition_type, title,
             reference_number, open_date, close_date, awarded_date,
             department, tender_coverage, overview, award_type,
             awarded_amount, is_awarded, source_file, report_date)
        VALUES %s
        ON CONFLICT (rfx_id) DO UPDATE SET
            awarded_amount = COALESCE(EXCLUDED.awarded_amount, mbie_award_notices.awarded_amount),
            is_awarded     = EXCLUDED.is_awarded OR mbie_award_notices.is_awarded,
            source_file    = 'current'
        """,
        batch,
    )
    return len(batch)


def _load_award_notices_historic(rows: list[dict]) -> int:
    """Load GETS-award-notices-historic.csv using batched execute_values."""
    batch = []
    for r in rows:
        rfx_id = r.get("RFx ID", "").strip()
        if not rfx_id:
            continue
        award_type = r.get("Award Type", "").strip()
        is_awarded = award_type == "Awarded"
        batch.append((
            rfx_id,
            r.get("Posting Agency", "").strip() or None,
            r.get("RFx Type", "").strip() or None,
            r.get("Competition Type", "").strip() or None,
            r.get("Title", "").strip() or None,
            r.get("Reference Number", "").strip() or None,
            _parse_date(r.get("Open Date", "")),
            _parse_date(r.get("Close Date", "")),
            _parse_date(r.get("Awarded Date ", "")),
            r.get("Department", "").strip() or None,
            r.get("Tender Coverage", "").strip() or None,
            (r.get("Overview", "") or "").strip()[:2000] or None,
            award_type or None,
            is_awarded,
            "historic",
            _parse_date(r.get("Report Date", "")),
        ))
    if not batch:
        return 0
    _batch_execute(
        """
        INSERT INTO mbie_award_notices
            (rfx_id, posting_agency, rfx_type, competition_type, title,
             reference_number, open_date, close_date, awarded_date,
             department, tender_coverage, overview, award_type,
             is_awarded, source_file, report_date)
        VALUES %s
        ON CONFLICT (rfx_id) DO UPDATE SET
            is_awarded = EXCLUDED.is_awarded OR mbie_award_notices.is_awarded,
            award_type = COALESCE(EXCLUDED.award_type, mbie_award_notices.award_type)
        """,
        batch,
    )
    return len(batch)


# ── Load suppliers ────────────────────────────────────────────────────────────

def _load_suppliers(rows: list[dict], source: str) -> int:
    batch = []
    for r in rows:
        rfx_id = r.get("RFx ID", "").strip()
        name = (r.get("Business Name", "") or "").strip()
        if not rfx_id or not name or name in ("NULL", ""):
            continue
        batch.append((
            rfx_id,
            r.get("Supplier NZBN", "").strip() or None,
            name,
            r.get("Full Address", "").strip() or None,
            r.get("Country", "").strip() or None,
            r.get("Website", "").strip() or None,
            source,
        ))
    if not batch:
        return 0
    _batch_execute(
        """
        INSERT INTO mbie_award_suppliers
            (rfx_id, supplier_nzbn, business_name, full_address,
             country, website, source_file)
        VALUES %s
        ON CONFLICT (rfx_id, business_name) DO NOTHING
        """,
        batch,
    )
    return len(batch)


# ── Load categories ───────────────────────────────────────────────────────────

def _load_categories(rows: list[dict]) -> int:
    batch = []
    for r in rows:
        rfx_id = r.get("RFx ID", "").strip()
        code = r.get("UNSPC Classification", "").strip()
        desc = r.get("UNSPC Description", "").strip()
        if not rfx_id or not code or code == "NULL":
            continue
        sector = _map_unspsc_to_sector(desc)
        batch.append((rfx_id, code, desc or None, sector))
    if not batch:
        return 0
    _batch_execute(
        "INSERT INTO mbie_award_categories (rfx_id, unspsc_code, unspsc_desc, sector_tag) VALUES %s ON CONFLICT (rfx_id, unspsc_code) DO NOTHING",
        batch,
    )
    return len(batch)


# ── Load regions ──────────────────────────────────────────────────────────────

def _load_regions(rows: list[dict]) -> int:
    batch = [(r.get("RFx ID","").strip(), r.get("Region","").strip())
             for r in rows
             if r.get("RFx ID","").strip() and r.get("Region","").strip() not in ("","NULL")]
    if not batch:
        return 0
    _batch_execute(
        "INSERT INTO mbie_award_regions (rfx_id, region) VALUES %s ON CONFLICT (rfx_id, region) DO NOTHING",
        batch,
    )
    return len(batch)


# ── Materialised view refresh ─────────────────────────────────────────────────

def refresh_win_history() -> None:
    """Refresh the supplier_win_history materialised view."""
    logger.info("Refreshing supplier_win_history materialised view...")
    db.execute("REFRESH MATERIALIZED VIEW supplier_win_history")
    count = db.fetchone("SELECT COUNT(*) as n FROM supplier_win_history")
    logger.info("supplier_win_history: %d supplier profiles", count["n"] if count else 0)


# ── Query helpers (used by bidders.py) ───────────────────────────────────────

def get_supplier_history(supplier_name: str) -> Optional[dict]:
    """Fetch win history for a named supplier. Exact match first, then fuzzy."""
    row = db.fetchone(
        "SELECT * FROM supplier_win_history WHERE supplier_name = %s",
        (supplier_name,),
    )
    if row:
        return dict(row)
    # Try case-insensitive
    row = db.fetchone(
        "SELECT * FROM supplier_win_history WHERE LOWER(supplier_name) = LOWER(%s)",
        (supplier_name,),
    )
    return dict(row) if row else None


def get_suppliers_by_sector_and_agency(
    sector_tag: str,
    agency_name: str,
    min_wins: int = 1,
    limit: int = 20,
) -> list[dict]:
    """
    Find suppliers who have won contracts in the given sector.
    Ranks by: (wins with this agency DESC, total wins DESC).
    """
    return db.fetchall(
        """
        WITH agency_wins AS (
            SELECT s.business_name,
                   COUNT(*) AS agency_win_count
              FROM mbie_award_notices n
              JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
              JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
             WHERE n.is_awarded
               AND LOWER(n.posting_agency) LIKE LOWER(%s)
               AND c.sector_tag = %s
             GROUP BY s.business_name
        )
        SELECT wh.supplier_name,
               wh.total_wins,
               wh.total_contract_value,
               wh.avg_contract_value,
               wh.last_win_date,
               wh.regions,
               COALESCE(aw.agency_win_count, 0) AS agency_wins
          FROM supplier_win_history wh
          LEFT JOIN agency_wins aw ON aw.business_name = wh.supplier_name
         WHERE wh.primary_sector = %s
            OR wh.sectors_json::text LIKE %s
         ORDER BY COALESCE(aw.agency_win_count, 0) DESC,
                  wh.total_wins DESC
         LIMIT %s
        """,
        (f"%{agency_name.split()[0]}%", sector_tag, sector_tag,
         f'%"{sector_tag}"%', limit),
    )


def get_suppliers_by_category(
    unspsc_desc_keywords: list[str],
    agency_name: str,
    limit: int = 15,
) -> list[dict]:
    """
    Find suppliers who have won contracts matching specific UNSPSC description keywords.
    More granular than sector matching — uses the actual product category text.
    E.g. keywords=["road maintenance", "resurfacing"] for road marking contracts.
    """
    if not unspsc_desc_keywords:
        return []

    # Build ILIKE conditions using positional params to avoid mixed-format error
    safe_kws = [kw.replace("'", "").replace("%", "") for kw in unspsc_desc_keywords[:5]]
    keyword_conditions = " OR ".join(
        f"LOWER(c.unspsc_desc) LIKE %s" for _ in safe_kws
    )
    kw_params = [f"%{kw.lower()}%" for kw in safe_kws]
    agency_prefix = f"%{agency_name.split()[0]}%"

    return db.fetchall(
        f"""
        WITH cat_wins AS (
            SELECT s.business_name,
                   COUNT(DISTINCT n.rfx_id) AS category_wins,
                   SUM(n.awarded_amount)     AS category_value,
                   MAX(n.awarded_date)       AS last_category_win,
                   COUNT(DISTINCT n.rfx_id) FILTER (
                       WHERE LOWER(n.posting_agency) LIKE LOWER(%s)
                   )                         AS agency_wins,
                   array_agg(DISTINCT c.unspsc_desc ORDER BY c.unspsc_desc)
                                             AS matched_categories
              FROM mbie_award_notices n
              JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
              JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
             WHERE n.is_awarded
               AND ({keyword_conditions})
             GROUP BY s.business_name
            HAVING COUNT(DISTINCT n.rfx_id) >= 1
        )
        SELECT cw.*,
               wh.avg_contract_value,
               wh.regions
          FROM cat_wins cw
          LEFT JOIN supplier_win_history wh ON wh.supplier_name = cw.business_name
         ORDER BY agency_wins DESC, category_wins DESC
         LIMIT %s
        """,
        [agency_prefix] + kw_params + [limit],
    )


def get_agency_win_count(supplier_name: str, agency_name: str, sector_tag: str) -> int:
    """Count how many times a supplier has won a contract from this agency in this sector."""
    row = db.fetchone(
        """
        SELECT COUNT(DISTINCT n.rfx_id) as n
          FROM mbie_award_notices n
          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
          JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
         WHERE n.is_awarded
           AND LOWER(s.business_name) = LOWER(%s)
           AND LOWER(n.posting_agency) LIKE LOWER(%s)
           AND c.sector_tag = %s
        """,
        (supplier_name, f"%{agency_name.split()[0]}%", sector_tag),
    )
    return row["n"] if row else 0


# ── Main entry point ──────────────────────────────────────────────────────────

def run_historical_ingestion(force_download: bool = False) -> dict:
    """
    Full MBIE data ingestion pipeline.
    Returns dict with row counts for each stage.
    """
    logger.info("Starting MBIE historical data ingestion")

    # 1. Download files
    download_files(force=force_download)

    # 2. Apply schema
    apply_schema()

    # Check if already loaded (idempotent)
    existing = db.fetchone("SELECT COUNT(*) as n FROM mbie_award_notices")
    if existing and existing["n"] > 0 and not force_download:
        logger.info(
            "MBIE data already loaded (%d notices). Use force_download=True to reload.",
            existing["n"],
        )
        # Still refresh the view in case new data was added
        try:
            refresh_win_history()
        except Exception as exc:
            logger.warning("View refresh failed: %s", exc)
        return {"status": "already_loaded", "notices": existing["n"]}

    # 3. Load award notices (current, then historic — current wins on conflict)
    logger.info("Loading historic award notices...")
    hist_rows = _read_csv(MBIE_FILES["award_notices_historic"], encoding="cp1252")
    n_hist = _load_award_notices_historic(hist_rows)
    logger.info("Loaded %d historic notice rows", n_hist)

    logger.info("Loading current award notices...")
    cur_rows = _read_csv(MBIE_FILES["award_notices"])
    n_cur = _load_award_notices_current(cur_rows)
    logger.info("Loaded %d current notice rows", n_cur)

    # 4. Load suppliers
    logger.info("Loading historic supplier data...")
    supp_hist = _read_csv(MBIE_FILES["supplier_data_historic"], encoding="cp1252")
    n_supp_hist = _load_suppliers(supp_hist, "historic")
    logger.info("Loaded %d historic supplier rows", n_supp_hist)

    logger.info("Loading current supplier data...")
    supp_cur = _read_csv(MBIE_FILES["supplier_data"])
    n_supp_cur = _load_suppliers(supp_cur, "current")
    logger.info("Loaded %d current supplier rows", n_supp_cur)

    # 5. Load product categories
    logger.info("Loading product categories...")
    cat_rows = _read_csv(MBIE_FILES["product_categories"])
    n_cats = _load_categories(cat_rows)
    logger.info("Loaded %d category rows", n_cats)

    # 6. Load regions
    logger.info("Loading regions...")
    region_rows = _read_csv(MBIE_FILES["region_by_tender"])
    n_regions = _load_regions(region_rows)
    logger.info("Loaded %d region rows", n_regions)

    # 7. Build/refresh supplier_win_history view
    refresh_win_history()

    # Stats
    stats = db.fetchone(
        """
        SELECT
            COUNT(*) AS total_notices,
            COUNT(*) FILTER (WHERE is_awarded) AS awarded_notices,
            (SELECT COUNT(*) FROM mbie_award_suppliers) AS supplier_rows,
            (SELECT COUNT(*) FROM supplier_win_history) AS unique_suppliers
          FROM mbie_award_notices
        """
    )
    logger.info(
        "MBIE ingestion complete: %d total notices, %d awarded, "
        "%d supplier rows, %d unique suppliers in win history",
        stats["total_notices"], stats["awarded_notices"],
        stats["supplier_rows"], stats["unique_suppliers"],
    )

    return {
        "notices_current": n_cur,
        "notices_historic": n_hist,
        "suppliers_current": n_supp_cur,
        "suppliers_historic": n_supp_hist,
        "categories": n_cats,
        "regions": n_regions,
        "total_awarded": stats["awarded_notices"],
        "unique_suppliers": stats["unique_suppliers"],
    }


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )
    force = "--force" in sys.argv
    result = run_historical_ingestion(force_download=force)
    print(result)
