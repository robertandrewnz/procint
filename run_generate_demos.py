"""
Standalone demo generation script — run once on Railway to populate all
7 sector demo artefacts in the pipeline_outputs DB table.

Usage:
  railway run python run_generate_demos.py
  railway run python run_generate_demos.py --sector FM
  railway run python run_generate_demos.py --force

Requires DATABASE_URL and ANTHROPIC_API_KEY in the environment.
Runs sectors sequentially. Reports success/failure after each sector.
"""
import argparse
import json
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
log = logging.getLogger("run_generate_demos")

import db
from generate_demo_content import (
    DEMO_SECTORS,
    DEMO_DIR,
    MANIFEST_PATH,
    _top_competitor_for_sector,
    _save_to_db,
    _upload_to_storage,
    _load_manifest,
    _write_manifest,
    _query_notices_for_sector,
    _title_matches_sector,
)
from demo_package import generate_demo_package
from competitor_profile import generate_competitor_profile
from watch_brief import generate_watch_brief
from win_position import calculate_win_position
from pursuit_package import _slug

ACCEPTABLE_WIN_BANDS = {"strong", "competitive"}
MAX_NOTICE_TRIES = 10


def _find_acceptable_notice(sector_key: str, sector_def: dict) -> tuple:
    """
    Return (notice_dict, win_pos_dict) for the best active notice whose
    win position band is 'strong' or 'competitive'.  Falls back to last
    60 days if no active notices qualify.  Returns (None, None) on failure.
    """
    db_tag = sector_def.get("db_tag", sector_key)
    fallback_tags = sector_def.get("fallback_db_tags", [])

    for active_only in (True, False):
        rows = _query_notices_for_sector(db_tag, active_only=active_only)
        for row in rows[:MAX_NOTICE_TRIES]:
            notice = dict(row)
            nid = notice.get("notice_id", "")
            title = notice.get("title", "")

            # keyword sanity check (same as generate_demo_content)
            if not _title_matches_sector(title, db_tag):
                log.debug("  Notice %s: title fails keyword check — skip", nid)
                continue

            # pre-screen win position before spending a Claude call
            try:
                wp = calculate_win_position(
                    notice=notice,
                    client_profile={"name": sector_def["firm"]["name"]},
                )
            except Exception as e:
                log.debug("  win_position failed for %s: %s — accepting anyway", nid, e)
                wp = {"band": "Competitive", "css_key": "competitive", "score": 0, "reasoning": []}

            band = wp.get("css_key", "competitive")
            if band in ACCEPTABLE_WIN_BANDS:
                label = "active" if active_only else "recent (60d lookback)"
                log.info("  Notice %s (%s): %s — band=%s score=%+d",
                         nid, label, title[:70], wp.get("band"), wp.get("score", 0))
                return notice, wp

            log.debug("  Notice %s: band=%s — skipping", nid, band)

    # try fallback sector tags
    for alt_tag in fallback_tags:
        alt_def = {**sector_def, "db_tag": alt_tag, "fallback_db_tags": []}
        result = _find_acceptable_notice(sector_key, alt_def)
        if result[0]:
            return result

    return None, None


def generate_sector(sector_key: str, sector_def: dict, force: bool) -> list:
    firm      = sector_def["firm"]
    firm_name = firm["name"]
    db_tag    = sector_def.get("db_tag", sector_key)
    label     = sector_def.get("label", sector_key)
    sector_dir = DEMO_DIR / sector_key
    sector_dir.mkdir(parents=True, exist_ok=True)
    items: list = []

    sep = "=" * 70
    log.info(sep)
    log.info("SECTOR: %-20s  firm: %s", sector_key.upper(), firm_name)
    log.info(sep)

    # ── 1. Pursuit package ────────────────────────────────────────────────────
    log.info("  [1/3] Pursuit package")
    notice, wp = _find_acceptable_notice(sector_key, sector_def)
    if notice is None:
        log.warning("  ✗ No acceptable notice found for %s — skipping pursuit package", sector_key)
    else:
        nid = notice["notice_id"]
        title = notice.get("title", "")[:80]
        html_filename = f"DEMO_{_slug(firm_name)}_{nid}.html"
        html_dest = sector_dir / html_filename

        if not html_dest.exists() or force:
            try:
                result = generate_demo_package(
                    notice_id=nid,
                    prospect_name=firm_name,
                    output_dir=sector_dir,
                    generate_pdf=False,
                    firm_profile=firm,
                )
                if result.get("html") and result["html"].exists():
                    html_dest = result["html"]
            except Exception as exc:
                log.error("  ✗ Pursuit package failed: %s", exc, exc_info=True)
        else:
            log.info("  — Pursuit package exists on disk (skipping, use --force to regenerate)")

        if html_dest.exists():
            content = html_dest.read_text(encoding="utf-8")
            _save_to_db(f"{sector_key}/{html_dest.name}", content)
            _upload_to_storage(html_dest, f"demo/{sector_key}/{html_dest.name}")
            items.append({
                "type":        "pursuit_package",
                "notice_id":   nid,
                "sector":      sector_key,
                "title":       title[:60],
                "is_demo":     True,
                "demo_sector": sector_key,
                "demo_label":  f"{label} Pursuit Package — {title[:60]}",
                "html_path":   str(html_dest.relative_to(Path(__file__).parent)),
                "pdf_path":    None,
            })
            log.info("  ✓ Pursuit package saved: %s (%d bytes)", html_dest.name, len(content))
        else:
            log.error("  ✗ Pursuit package file not found after generation attempt")

    # ── 2. Competitor profile ─────────────────────────────────────────────────
    log.info("  [2/3] Competitor profile")
    comp_name = _top_competitor_for_sector(sector_key, db_tag)
    if not comp_name:
        log.warning("  ✗ No MBIE competitor data for %s — skipping competitor profile", sector_key)
    else:
        comp_filename = f"competitor_{_slug(comp_name)}.html"
        comp_dest = sector_dir / comp_filename

        if not comp_dest.exists() or force:
            try:
                comp_path = generate_competitor_profile(
                    competitor_name=comp_name,
                    client_name=firm_name,
                    sector_context=sector_def.get("tagline", label),
                    output_dir=sector_dir,
                    is_demo=True,
                )
                comp_dest = comp_path
            except Exception as exc:
                log.error("  ✗ Competitor profile failed: %s", exc, exc_info=True)
        else:
            log.info("  — Competitor profile exists on disk (skipping)")

        if comp_dest.exists():
            content = comp_dest.read_text(encoding="utf-8")
            _save_to_db(f"{sector_key}/{comp_dest.name}", content)
            _upload_to_storage(comp_dest, f"demo/{sector_key}/{comp_dest.name}")
            items.append({
                "type":            "competitor_profile",
                "competitor_name": comp_name,
                "sector":          sector_key,
                "is_demo":         True,
                "demo_sector":     sector_key,
                "demo_label":      f"{label} Competitor Profile — {comp_name}",
                "html_path":       str(comp_dest.relative_to(Path(__file__).parent)),
            })
            log.info("  ✓ Competitor profile saved: %s  competitor=%s (%d bytes)",
                     comp_dest.name, comp_name, len(content))
        else:
            log.error("  ✗ Competitor profile file not found after generation attempt")

    # ── 3. Watch brief ────────────────────────────────────────────────────────
    log.info("  [3/3] Watch brief")
    brief_filename = f"watch_brief_{sector_key}_{date.today().isoformat()}.html"
    brief_dest = sector_dir / brief_filename

    if not brief_dest.exists() or force:
        try:
            brief_path = generate_watch_brief(
                client_name=firm_name,
                sectors=[db_tag],
                output_dir=sector_dir,
                demo_sector=sector_key,
            )
            if brief_path.exists() and brief_path != brief_dest:
                brief_path.rename(brief_dest)
            elif brief_path.exists():
                brief_dest = brief_path
        except Exception as exc:
            log.error("  ✗ Watch brief failed: %s", exc, exc_info=True)
    else:
        log.info("  — Watch brief exists on disk (skipping)")

    if brief_dest.exists():
        content = brief_dest.read_text(encoding="utf-8")
        _save_to_db(f"{sector_key}/{brief_dest.name}", content)
        _upload_to_storage(brief_dest, f"demo/{sector_key}/{brief_dest.name}")
        items.append({
            "type":        "watch_brief",
            "sectors":     sector_key,
            "week_of":     date.today().isoformat(),
            "is_demo":     True,
            "demo_sector": sector_key,
            "demo_label":  f"{label} Watch Brief — week of {date.today().isoformat()}",
            "html_path":   str(brief_dest.relative_to(Path(__file__).parent)),
        })
        log.info("  ✓ Watch brief saved: %s (%d bytes)", brief_dest.name, len(content))
    else:
        log.error("  ✗ Watch brief file not found after generation attempt")

    log.info("  Sector %s DONE: %d/3 artefacts", sector_key, len(items))
    return items


def main() -> None:
    p = argparse.ArgumentParser(description="Generate demo artefacts for all 7 sectors")
    p.add_argument("--force",  action="store_true", default=True,
                   help="Regenerate even if files exist on disk (default: on)")
    p.add_argument("--no-force", dest="force", action="store_false")
    p.add_argument("--sector", metavar="KEY",
                   help="Only run one sector (e.g. FM, ICT, health)")
    args = p.parse_args()

    if args.sector and args.sector not in DEMO_SECTORS:
        log.error("Unknown sector '%s'. Valid: %s", args.sector, ", ".join(DEMO_SECTORS))
        sys.exit(1)

    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest()
    manifest["generated"] = date.today().isoformat()
    if "sectors" not in manifest:
        manifest["sectors"] = {}

    sectors_to_run = (
        {args.sector: DEMO_SECTORS[args.sector]}
        if args.sector
        else DEMO_SECTORS
    )

    total = 0
    for sector_key, sector_def in sectors_to_run.items():
        items = generate_sector(sector_key, sector_def, force=args.force)
        manifest["sectors"][sector_key] = {
            "firm":  sector_def["firm"],
            "items": items,
        }
        total += len(items)

    _write_manifest(manifest)

    # ── Summary ──────────────────────────────────────────────────────────────
    log.info("")
    log.info("=" * 70)
    log.info("GENERATION COMPLETE: %d artefacts across %d sectors", total, len(sectors_to_run))
    per_sector = {k: len(manifest["sectors"][k].get("items", [])) for k in sectors_to_run}
    for sk, cnt in per_sector.items():
        status = "✓" if cnt == 3 else ("⚠ PARTIAL" if cnt > 0 else "✗ FAILED")
        log.info("  %s  %s: %d/3", status, sk, cnt)
    log.info("=" * 70)

    # ── DB verification ───────────────────────────────────────────────────────
    try:
        html_row  = db.fetchone("SELECT COUNT(*) AS cnt FROM pipeline_outputs WHERE output_type='demo_html'")
        mani_row  = db.fetchone("SELECT COUNT(*) AS cnt FROM pipeline_outputs WHERE output_type='demo_manifest'")
        html_cnt  = int((html_row  or {}).get("cnt") or 0)
        mani_cnt  = int((mani_row  or {}).get("cnt") or 0)
        log.info("DB verification: %d demo_html rows, %d demo_manifest rows", html_cnt, mani_cnt)
        if html_cnt == 0:
            log.error("ZERO demo_html rows in DB — check errors above")
        elif html_cnt < 7:
            log.warning("Only %d demo_html rows — some sectors may have failed", html_cnt)
        else:
            log.info("DB looks good. Visit /demo to verify public-facing output.")
    except Exception as e:
        log.warning("DB count check failed: %s", e)

    if total == 0:
        log.error("Zero artefacts generated — see errors above")
        sys.exit(1)


if __name__ == "__main__":
    main()
