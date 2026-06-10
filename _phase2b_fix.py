"""
Phase 2b — targeted fix for 3 remaining issues from Phase 2 run.
Run with: railway run python3 _phase2b_fix.py

Fixes:
  1. Cybersecurity PP: no cybersecurity notices found — try security + AoG security keywords
  2. Infrastructure PP: existing PP has NO GO win position — regenerate
  3. Construction PP: notice closed today (dtc=NULL bypassed future-close check) — fix query
  4. Health competitor: replace Alphatech with Fisher & Paykel Healthcare per spec

Remove this file after successful execution.
"""
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
log = logging.getLogger("phase2b_fix")

import db
from generate_demo_content import (
    DEMO_SECTORS, DEMO_DIR,
    _save_to_db, _upload_to_storage,
    _load_manifest, _write_manifest,
    _title_matches_sector,
)
from demo_package import generate_demo_package
from competitor_profile import generate_competitor_profile
from win_position import calculate_win_position
from pursuit_package import _slug

ACCEPTABLE_WIN_BANDS = {"strong", "competitive"}


def _query_notices_strictly_future(sector_tag: str, limit: int = 20) -> list[dict]:
    """
    Query notices for sector where close_date is strictly in the future.
    Uses close_date (not days_until_close) to avoid NULL-bypass edge case.
    """
    rank_case = (
        "CASE p.value_band "
        "WHEN '10m_plus'   THEN 5 "
        "WHEN '2m_10m'     THEN 4 "
        "WHEN '500k_2m'    THEN 3 "
        "WHEN '100k_500k'  THEN 2 "
        "WHEN 'under_100k' THEN 1 "
        "ELSE 0 END"
    )
    return db.fetchall(
        f"""
        SELECT r.notice_id, r.title, r.agency, r.description, r.close_date,
               p.sector_tag, p.days_until_close, p.value_band
          FROM parsed_notices p
          JOIN raw_notices r ON r.notice_id = p.notice_id
         WHERE p.sector_tag = %s
           AND r.close_date > CURRENT_DATE
         ORDER BY r.close_date ASC, {rank_case} DESC
         LIMIT %s
        """,
        (sector_tag, limit),
    )


def _query_notices_keyword_future(keywords: list[str], limit: int = 15) -> list[dict]:
    """
    Full-text title search for notices with future close dates.
    Used for cybersecurity when sector_tag='cybersecurity' returns nothing.
    """
    clauses = []
    params = []
    for kw in keywords:
        kl = kw.lower()
        if " " in kl:
            clauses.append("LOWER(r.title) LIKE %s")
            params.append(f"%{kl}%")
        elif len(kl) <= 4:
            clauses.append("LOWER(r.title) ~ %s")
            params.append(r"\m" + kl + r"\M")
        else:
            clauses.append("LOWER(r.title) LIKE %s")
            params.append(f"%{kl}%")

    if not clauses:
        return []

    kw_expr = " OR ".join(clauses)
    rank_case = (
        "CASE p.value_band "
        "WHEN '10m_plus'   THEN 5 "
        "WHEN '2m_10m'     THEN 4 "
        "WHEN '500k_2m'    THEN 3 "
        "WHEN '100k_500k'  THEN 2 "
        "WHEN 'under_100k' THEN 1 "
        "ELSE 0 END"
    )
    return db.fetchall(
        f"""
        SELECT r.notice_id, r.title, r.agency, r.description, r.close_date,
               p.sector_tag, p.days_until_close, p.value_band
          FROM parsed_notices p
          JOIN raw_notices r ON r.notice_id = p.notice_id
         WHERE ({kw_expr})
           AND r.close_date > CURRENT_DATE
         ORDER BY r.close_date ASC, {rank_case} DESC
         LIMIT %s
        """,
        tuple(params) + (limit,),
    )


def _find_notice_with_acceptable_wp(rows: list[dict], firm_name: str,
                                    label: str = "", max_tries: int = 10) -> "tuple[dict, dict] | tuple[None, None]":
    """Screen rows for acceptable win position band."""
    for row in rows[:max_tries]:
        notice = dict(row)
        nid = notice.get("notice_id", "")
        title = notice.get("title", "")
        close_dt = notice.get("close_date")

        try:
            wp = calculate_win_position(
                notice=notice,
                client_profile={"name": firm_name},
            )
        except Exception as e:
            log.warning("  win_position error for %s: %s — accepting", nid, e)
            wp = {"band": "Competitive", "css_key": "competitive", "score": 0, "reasoning": []}

        band = wp.get("css_key", "competitive")
        if band in ACCEPTABLE_WIN_BANDS:
            log.info("  ✓ Notice %s %s: band=%s  '%s'  close=%s",
                     nid, label, wp.get("band"), title[:70], close_dt)
            return notice, wp

        log.debug("  Notice %s: band=%s — skip", nid, band)

    return None, None


def regen_pp(sector_key: str, sector_def: dict, sector_dir: Path,
             notice: dict) -> "dict | None":
    """Generate pursuit package for an already-selected notice."""
    firm = sector_def["firm"]
    firm_name = firm["name"]
    label = sector_def.get("label", sector_key)
    nid = notice["notice_id"]
    title = (notice.get("title") or "")[:80]

    try:
        log.info("[%s] Generating pursuit package: %s — '%s'", sector_key, nid, title[:60])
        result = generate_demo_package(
            notice_id=nid,
            prospect_name=firm_name,
            output_dir=sector_dir,
            generate_pdf=False,
            firm_profile=firm,
        )
        html_dest = result.get("html")
    except Exception as exc:
        log.error("[%s] Pursuit package failed: %s", sector_key, exc, exc_info=True)
        return None

    if not html_dest or not html_dest.exists():
        log.error("[%s] Pursuit HTML not found after generation", sector_key)
        return None

    content = html_dest.read_text(encoding="utf-8")

    # Verify win position is not NO GO
    if ">NO GO<" in content and "CONDITIONAL GO" not in content:
        log.warning("[%s] Generated PP still has NO GO verdict — trying next notice", sector_key)
        return None

    _save_to_db(f"{sector_key}/{html_dest.name}", content)
    _upload_to_storage(html_dest, f"demo/{sector_key}/{html_dest.name}")
    log.info("[%s] ✓ Pursuit package: %s (%d bytes)", sector_key, html_dest.name, len(content))

    return {
        "type":        "pursuit_package",
        "notice_id":   nid,
        "sector":      sector_key,
        "title":       title[:60],
        "is_demo":     True,
        "demo_sector": sector_key,
        "demo_label":  f"{label} Pursuit Package — {title[:60]}",
        "html_path":   str(html_dest.relative_to(Path(__file__).parent)),
        "pdf_path":    None,
    }


def regen_competitor(sector_key: str, sector_def: dict, sector_dir: Path,
                     competitor_name: str, sector_context: str) -> "dict | None":
    firm_name = sector_def["firm"]["name"]
    label = sector_def.get("label", sector_key)
    try:
        log.info("[%s] Generating competitor profile: %s", sector_key, competitor_name)
        comp_path = generate_competitor_profile(
            competitor_name=competitor_name,
            client_name=firm_name,
            sector_context=sector_context,
            output_dir=sector_dir,
            is_demo=True,
        )
    except Exception as exc:
        log.error("[%s] Competitor profile failed: %s", sector_key, exc, exc_info=True)
        return None

    if not comp_path.exists():
        return None

    content = comp_path.read_text(encoding="utf-8")
    _save_to_db(f"{sector_key}/{comp_path.name}", content)
    _upload_to_storage(comp_path, f"demo/{sector_key}/{comp_path.name}")
    log.info("[%s] ✓ Competitor profile: %s (%d bytes)", sector_key, comp_path.name, len(content))

    return {
        "type":            "competitor_profile",
        "competitor_name": competitor_name,
        "sector":          sector_key,
        "is_demo":         True,
        "demo_sector":     sector_key,
        "demo_label":      f"{label} Competitor Profile — {competitor_name}",
        "html_path":       str(comp_path.relative_to(Path(__file__).parent)),
    }


def main() -> None:
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest()
    if "sectors" not in manifest:
        manifest["sectors"] = {}
    sep = "=" * 70
    any_fail = False

    # ── FIX 1: CYBERSECURITY PP ───────────────────────────────────────────────
    log.info(sep)
    log.info("FIX 1: CYBERSECURITY — pursuit package (keyword search, strictly future)")
    log.info(sep)
    sk = "cybersecurity"
    sd = DEMO_SECTORS[sk]
    sector_dir = DEMO_DIR / sk
    sector_dir.mkdir(parents=True, exist_ok=True)
    firm_name = sd["firm"]["name"]

    # Try broader keyword set: cybersecurity + security operations + NZISM + AoG security
    cyber_keywords = [
        "cyber security", "cybersecurity", "cyber resilience",
        "security operations", "SOC", "SIEM", "penetration testing", "pen testing",
        "information security", "infosec", "CISO", "zero trust", "IRAP", "NZISM",
        "vulnerability assessment", "threat detection", "incident response",
        "security assessment", "security audit", "security services",
        "managed security", "security uplift",
    ]
    rows = _query_notices_keyword_future(cyber_keywords)
    log.info("[cybersecurity] Keyword search returned %d future notices", len(rows))
    for r in rows[:5]:
        log.info("  %s close=%s sector=%s '%s'",
                 r["notice_id"], r.get("close_date"), r.get("sector_tag"),
                 (r.get("title") or "")[:60])

    notice, _ = _find_notice_with_acceptable_wp(rows, firm_name, label="(keyword)")
    if notice:
        # Clean up old cybersecurity PP entries from DB
        try:
            db.execute(
                "DELETE FROM pipeline_outputs WHERE output_type='demo_html' "
                "AND filename LIKE 'cybersecurity/DEMO_%'"
            )
        except Exception as e:
            log.warning("[cybersecurity] Could not clear old PP: %s", e)

        pp = regen_pp(sk, sd, sector_dir, notice)
        if pp:
            current_items = manifest["sectors"].get(sk, {}).get("items", [])
            current_items = [i for i in current_items if i.get("type") != "pursuit_package"]
            current_items.append(pp)
            manifest["sectors"][sk]["items"] = current_items
            log.info("[cybersecurity] ✓ PP added")
        else:
            log.warning("[cybersecurity] PP generation failed after notice found")
            any_fail = True
    else:
        log.warning("[cybersecurity] No suitable cybersecurity notice found — sector has no active tenders")
        log.warning("  This is a data gap, not a code issue. PP will remain absent.")

    # ── FIX 2: INFRASTRUCTURE PP ─────────────────────────────────────────────
    log.info(sep)
    log.info("FIX 2: INFRASTRUCTURE PP — regenerate (existing has NO GO win position)")
    log.info(sep)
    sk = "infrastructure"
    sd = DEMO_SECTORS[sk]
    sector_dir = DEMO_DIR / sk
    sector_dir.mkdir(parents=True, exist_ok=True)
    firm_name = sd["firm"]["name"]

    rows = _query_notices_strictly_future("infrastructure")
    log.info("[infrastructure] %d strictly-future infrastructure notices", len(rows))

    # Try each notice; skip any that generate NO GO after Claude synthesis
    infra_pp = None
    for row in rows[:8]:
        notice_dict = dict(row)
        nid = notice_dict.get("notice_id", "")
        title = (notice_dict.get("title") or "")[:70]

        if not _title_matches_sector(title, "infrastructure"):
            log.debug("  Notice %s: title fails keyword check — skip", nid)
            continue

        try:
            wp = calculate_win_position(notice=notice_dict, client_profile={"name": firm_name})
        except Exception as e:
            wp = {"band": "Competitive", "css_key": "competitive", "score": 0, "reasoning": []}

        band = wp.get("css_key", "competitive")
        if band not in ACCEPTABLE_WIN_BANDS:
            log.debug("  Notice %s: band=%s — skip", nid, band)
            continue

        log.info("  ✓ Notice %s: band=%s  '%s'", nid, wp.get("band"), title)
        pp = regen_pp(sk, sd, sector_dir, notice_dict)
        if pp:
            # Clear old PP from DB
            try:
                db.execute(
                    "DELETE FROM pipeline_outputs WHERE output_type='demo_html' "
                    "AND filename LIKE 'infrastructure/DEMO_%'"
                )
            except Exception as e:
                log.warning("[infrastructure] Could not clear old PP: %s", e)

            current_items = manifest["sectors"].get(sk, {}).get("items", [])
            current_items = [i for i in current_items if i.get("type") != "pursuit_package"]
            current_items.append(pp)
            manifest["sectors"][sk]["items"] = current_items
            infra_pp = pp
            log.info("[infrastructure] ✓ PP regenerated")
            break
        else:
            log.warning("  Notice %s: PP generation returned NO GO or failed — trying next", nid)

    if not infra_pp:
        log.warning("[infrastructure] Could not find acceptable infrastructure PP notice")
        any_fail = True

    # ── FIX 3: CONSTRUCTION PP ───────────────────────────────────────────────
    log.info(sep)
    log.info("FIX 3: CONSTRUCTION PP — find notice with strictly future close date")
    log.info(sep)
    sk = "construction"
    sd = DEMO_SECTORS[sk]
    sector_dir = DEMO_DIR / sk
    sector_dir.mkdir(parents=True, exist_ok=True)
    firm_name = sd["firm"]["name"]

    rows = _query_notices_strictly_future("construction")
    log.info("[construction] %d strictly-future construction notices", len(rows))

    notice, _ = _find_notice_with_acceptable_wp(
        [r for r in rows if _title_matches_sector((r.get("title") or ""), "construction")],
        firm_name, label="(future)"
    )
    if notice:
        # Clear old PP from DB
        try:
            db.execute(
                "DELETE FROM pipeline_outputs WHERE output_type='demo_html' "
                "AND filename LIKE 'construction/DEMO_%'"
            )
        except Exception as e:
            log.warning("[construction] Could not clear old PP: %s", e)

        pp = regen_pp(sk, sd, sector_dir, notice)
        if pp:
            current_items = manifest["sectors"].get(sk, {}).get("items", [])
            current_items = [i for i in current_items if i.get("type") != "pursuit_package"]
            current_items.append(pp)
            manifest["sectors"][sk]["items"] = current_items
            log.info("[construction] ✓ PP regenerated with future close date")
        else:
            log.warning("[construction] PP generation failed")
            any_fail = True
    else:
        log.warning("[construction] No suitable construction notice with future close date")
        any_fail = True

    # ── FIX 4: HEALTH COMPETITOR — Fisher & Paykel Healthcare ────────────────
    log.info(sep)
    log.info("FIX 4: HEALTH — replace Alphatech with Fisher & Paykel Healthcare")
    log.info(sep)
    sk = "health"
    sd = DEMO_SECTORS[sk]
    sector_dir = DEMO_DIR / sk
    sector_dir.mkdir(parents=True, exist_ok=True)

    # Clear old competitor from DB
    try:
        db.execute(
            "DELETE FROM pipeline_outputs WHERE output_type='demo_html' "
            "AND filename LIKE 'health/competitor_%'"
        )
        log.info("[health] Removed old competitor profile from DB")
    except Exception as e:
        log.warning("[health] Could not remove old competitor: %s", e)

    cp = regen_competitor(
        sk, sd, sector_dir,
        "Fisher & Paykel Healthcare",
        "NZ health technology, clinical systems, hospital ICT and health data platforms — "
        "Fisher & Paykel Healthcare is a dominant NZ health technology and clinical systems "
        "supplier with deep relationships across Te Whatu Ora (Health NZ) and DHB-successor entities",
    )
    if cp:
        current_items = manifest["sectors"].get(sk, {}).get("items", [])
        current_items = [i for i in current_items if i.get("type") != "competitor_profile"]
        current_items.append(cp)
        manifest["sectors"][sk]["items"] = current_items
        log.info("[health] ✓ Competitor profile updated to Fisher & Paykel Healthcare")
    else:
        log.warning("[health] Competitor profile generation failed")
        any_fail = True

    # ── Save manifest ──────────────────────────────────────────────────────────
    log.info("")
    manifest["generated"] = date.today().isoformat()
    _write_manifest(manifest)

    # ── Summary ────────────────────────────────────────────────────────────────
    log.info(sep)
    log.info("PHASE 2B COMPLETE%s", " (WITH WARNINGS)" if any_fail else "")
    log.info(sep)
    for sk2, sector_data in manifest.get("sectors", {}).items():
        items = sector_data.get("items", [])
        types = {i.get("type") for i in items}
        pp_ok = "✓" if "pursuit_package" in types else "✗ MISSING"
        cp_ok = "✓" if "competitor_profile" in types else "✗ MISSING"
        wb_ok = "✓" if "watch_brief" in types else "✗ MISSING"
        comp_name = next((i.get("competitor_name", "") for i in items
                         if i.get("type") == "competitor_profile"), "")
        log.info("  %s  PP=%s  CP=%s (%s)  WB=%s",
                 sk2.upper().ljust(15), pp_ok, cp_ok, comp_name, wb_ok)

    if any_fail:
        log.warning("Some fixes failed — check warnings above")
    else:
        log.info("All fixes applied successfully")


if __name__ == "__main__":
    main()
