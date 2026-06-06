"""
MBIE Open Data — monthly refresh checker.

Compares Content-Length and Last-Modified headers of MBIE CSV files
against locally stored metadata. Downloads any changed files and
re-ingests them into the database, then rebuilds supplier_win_history.

Run:
  python refresh_mbie.py [--force] [--dry-run]

  --force    Download and re-ingest all files regardless of changes
  --dry-run  Check for changes and log, but do not download or ingest

Scheduled: 1st of each month at 05:00 via cron.
Log output goes to logs/scheduler.log (via scheduler.py) and stdout.
"""
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

METADATA_FILE = Path("data/mbie/metadata.json")

MBIE_URLS = {
    "award_notices":          "https://www.mbie.govt.nz/assets/Data-Files/NZGPP-GETS-Open-Data/GETS-award-notices.csv",
    "award_notices_historic": "https://www.mbie.govt.nz/assets/Data-Files/NZGPP-GETS-Open-Data/GETS-award-notices-historic.csv",
    "supplier_data":          "https://www.mbie.govt.nz/assets/Data-Files/NZGPP-GETS-Open-Data/GETS-supplier-data.csv",
    "supplier_data_historic": "https://www.mbie.govt.nz/assets/Data-Files/NZGPP-GETS-Open-Data/GETS-supplier-data-historic.csv",
    "product_categories":     "https://www.mbie.govt.nz/assets/Data-Files/NZGPP-GETS-Open-Data/GETS-product-categories.csv",
    "region_by_tender":       "https://www.mbie.govt.nz/assets/Data-Files/NZGPP-GETS-Open-Data/GETS-region-by-tender.csv",
}

HEADERS = {"User-Agent": "ProcintBot/1.0 (NZ govt open data monitor)"}


# ── Metadata store ────────────────────────────────────────────────────────────

def _load_metadata() -> dict:
    if METADATA_FILE.exists():
        try:
            return json.loads(METADATA_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_metadata(meta: dict) -> None:
    METADATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    METADATA_FILE.write_text(json.dumps(meta, indent=2))


# ── Remote header check ───────────────────────────────────────────────────────

def _get_remote_meta(key: str) -> dict:
    """HEAD request to get Content-Length and Last-Modified."""
    url = MBIE_URLS[key]
    try:
        resp = requests.head(url, headers=HEADERS, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        return {
            "url":           url,
            "content_length": resp.headers.get("Content-Length"),
            "last_modified":  resp.headers.get("Last-Modified"),
            "etag":           resp.headers.get("ETag"),
            "checked_at":     datetime.utcnow().isoformat(),
        }
    except Exception as exc:
        logger.warning("HEAD check failed for %s: %s", key, exc)
        return {}


def _has_changed(key: str, stored: dict, remote: dict) -> bool:
    """Return True if remote file differs from stored metadata."""
    if not stored or not remote:
        return True
    # Compare Content-Length first (fastest signal)
    if remote.get("content_length") and stored.get("content_length"):
        if remote["content_length"] != stored["content_length"]:
            logger.info(
                "%s: content_length changed %s → %s",
                key, stored["content_length"], remote["content_length"],
            )
            return True
    # Fall back to Last-Modified
    if remote.get("last_modified") and stored.get("last_modified"):
        if remote["last_modified"] != stored["last_modified"]:
            logger.info(
                "%s: last_modified changed %s → %s",
                key, stored["last_modified"], remote["last_modified"],
            )
            return True
    # If neither header available, check local file size vs content-length
    local = Path("data/mbie") / Path(MBIE_URLS[key]).name
    if local.exists() and remote.get("content_length"):
        if str(local.stat().st_size) != remote["content_length"]:
            logger.info("%s: local size differs from remote", key)
            return True
    return False


# ── Download ──────────────────────────────────────────────────────────────────

def _download(key: str) -> Optional[Path]:
    url = MBIE_URLS[key]
    dest = Path("data/mbie") / Path(url).name
    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading %s ...", dest.name)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=120, stream=True)
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        size_mb = dest.stat().st_size / 1_048_576
        logger.info("Downloaded %s → %.1f MB", dest.name, size_mb)
        return dest
    except Exception as exc:
        logger.error("Download failed for %s: %s", key, exc)
        return None


# ── Main entry point ──────────────────────────────────────────────────────────

def run_mbie_refresh(force: bool = False, dry_run: bool = False) -> dict:
    """
    Check MBIE files for changes. Download and re-ingest any that have changed.
    Returns summary dict: {changed: [keys], skipped: [keys], errors: [keys]}.
    """
    logger.info("Starting MBIE open data refresh check (force=%s, dry_run=%s)", force, dry_run)
    stored_meta = _load_metadata()
    new_meta = {}

    changed, skipped, errors = [], [], []

    for key in MBIE_URLS:
        remote = _get_remote_meta(key)
        new_meta[key] = remote

        if force or _has_changed(key, stored_meta.get(key, {}), remote):
            if dry_run:
                logger.info("[DRY-RUN] Would download: %s", key)
                changed.append(key)
                continue
            path = _download(key)
            if path:
                changed.append(key)
            else:
                errors.append(key)
        else:
            logger.info("%s: unchanged — skipping download", key)
            skipped.append(key)

    if not dry_run:
        _save_metadata(new_meta)

    if changed and not dry_run:
        logger.info("Re-ingesting %d changed files: %s", len(changed), changed)
        try:
            from historical_data import run_historical_ingestion, refresh_win_history
            import db

            # Truncate and reload only the tables (files already on disk)
            logger.info("Truncating MBIE tables for fresh load...")
            db.execute("TRUNCATE mbie_award_notices, mbie_award_suppliers, mbie_award_categories, mbie_award_regions CASCADE")
            result = run_historical_ingestion(force_download=False)
            logger.info("Re-ingestion complete: %s", result)
        except Exception as exc:
            logger.error("Re-ingestion failed: %s", exc)
            errors.append("ingestion")

    summary = {"changed": changed, "skipped": skipped, "errors": errors}
    logger.info(
        "MBIE refresh complete — changed: %d, skipped: %d, errors: %d",
        len(changed), len(skipped), len(errors),
    )
    return summary


if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )
    p = argparse.ArgumentParser(description="MBIE open data monthly refresh")
    p.add_argument("--force",   action="store_true", help="Re-download all files")
    p.add_argument("--dry-run", action="store_true", help="Check only, do not download")
    args = p.parse_args()
    result = run_mbie_refresh(force=args.force, dry_run=args.dry_run)
    print(result)
