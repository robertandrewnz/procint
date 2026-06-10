"""
Phase 2 targeted demo regeneration.
Run with: railway run python _phase2_targeted_regen.py

Regenerates only the broken demo artefacts:
  CYBERSECURITY   — all 3 artefacts  (competitor = Datacom)
  CONSTRUCTION    — pursuit + competitor (competitor = Fletcher Construction)
  HEALTH          — pursuit package only (competitor checked first)
  DEFENCE         — competitor profile only (Nova Systems, replaces HEB Construction)
  INFRASTRUCTURE  — competitor profile only (Downer Group)
  ALL 7 SECTORS   — watch briefs regenerated with fixed sector-specific code

Remove this file after successful execution.
"""
import json
import logging
import sys
import os
import re
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    stream=sys.stdout,
    force=True,
)
log = logging.getLogger("phase2_regen")

import db
from generate_demo_content import (
    DEMO_SECTORS, DEMO_DIR,
    _save_to_db, _upload_to_storage,
    _load_manifest, _write_manifest,
    _query_notices_for_sector, _title_matches_sector,
)
from demo_package import generate_demo_package
from competitor_profile import generate_competitor_profile
from watch_brief import generate_watch_brief
from win_position import calculate_win_position
from pursuit_package import _slug

ACCEPTABLE_WIN_BANDS = {"strong", "competitive"}
MAX_NOTICE_TRIES = 10

# Specific competitor names for each sector (override MBIE top supplier)
FORCED_COMPETITORS = {
    "cybersecurity": {
        "name": "Datacom",
        "sector_context": (
            "NZ government cybersecurity, ICT security services, SOC operations and "
            "security compliance advisory — Datacom is a dominant NZ government ICT "
            "and security services supplier with extensive MBIE award history"
        ),
    },
    "construction": {
        "name": "Fletcher Construction",
        "sector_context": (
            "NZ government civil construction, infrastructure and commercial builds — "
            "Fletcher Construction is the dominant NZ government construction contractor "
            "with MBIE award history in civil, infrastructure and commercial builds"
        ),
    },
    "defence": {
        "name": "Nova Systems",
        "sector_context": (
            "NZ and Australian defence technology, systems integration and C4ISR services — "
            "Nova Systems is a specialist Australia-NZ defence technology and systems "
            "integration firm. Capabilities include electronic warfare systems, C4ISR "
            "integration, defence platform support and security-cleared workforce. "
            "Known NZ/ADF engagement history includes NZDF and allied defence contracts."
        ),
    },
    "infrastructure": {
        "name": "Downer Group",
        "sector_context": (
            "NZ government infrastructure delivery — roading, transport, utilities and "
            "Three Waters contracts. Downer Group is a dominant NZ government infrastructure "
            "contractor with extensive MBIE award history including NZTA and council "
            "infrastructure panels"
        ),
    },
}


# ── Pursuit package ────────────────────────────────────────────────────────────

def _find_acceptable_notice(sector_key: str, sector_def: dict,
                             require_future_close: bool = False,
                             max_tries: int = MAX_NOTICE_TRIES) -> tuple:
    """
    Return (notice_dict, win_pos_dict) for the first notice whose win position
    band is 'strong' or 'competitive'.  Returns (None, None) on failure.
    """
    db_tag = sector_def.get("db_tag", sector_key)
    firm_name = sector_def["firm"]["name"]

    for active_only in (True, False):
        rows = _query_notices_for_sector(db_tag, active_only=active_only)
        tried = 0
        for row in rows:
            if tried >= max_tries:
                break
            notice = dict(row)
            nid = notice.get("notice_id", "")
            title = notice.get("title", "")
            dtc = notice.get("days_until_close")

            if not _title_matches_sector(title, db_tag):
                log.debug("  Notice %s: title fails keyword check — skip", nid)
                continue

            if require_future_close and dtc is not None and dtc <= 0:
                log.info("  Notice %s ('%s'): close date past (days=%s) — skip",
                         nid, title[:50], dtc)
                continue

            tried += 1

            try:
                wp = calculate_win_position(
                    notice=notice,
                    client_profile={"name": firm_name},
                )
            except Exception as e:
                log.warning("  win_position failed for %s: %s — accepting", nid, e)
                wp = {"band": "Competitive", "css_key": "competitive",
                      "score": 0, "reasoning": []}

            band = wp.get("css_key", "competitive")
            if band in ACCEPTABLE_WIN_BANDS:
                label = "active" if active_only else "60d lookback"
                log.info("  ✓ Notice %s (%s): band=%s score=%+d  '%s'",
                         nid, label, wp.get("band"), wp.get("score", 0), title[:70])
                return notice, wp

            log.debug("  Notice %s: band=%s — skip", nid, band)

    log.warning("  No acceptable notice found for sector '%s' after %d tries",
                sector_key, max_tries)
    return None, None


def regen_pursuit(sector_key: str, sector_def: dict, sector_dir: Path,
                  require_future_close: bool = False,
                  max_tries: int = MAX_NOTICE_TRIES) -> "dict | None":
    """Generate pursuit package for sector. Returns manifest item dict or None."""
    firm = sector_def["firm"]
    firm_name = firm["name"]
    label = sector_def.get("label", sector_key)

    log.info("[%s] Finding acceptable notice (future_close=%s)...", sector_key, require_future_close)
    notice, _ = _find_acceptable_notice(sector_key, sector_def,
                                        require_future_close=require_future_close,
                                        max_tries=max_tries)
    if notice is None:
        return None

    nid = notice["notice_id"]
    title = notice.get("title", "")[:80]
    html_filename = f"DEMO_{_slug(firm_name)}_{nid}.html"
    html_dest = sector_dir / html_filename

    try:
        log.info("[%s] Generating pursuit package: %s — '%s'", sector_key, nid, title[:60])
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
        log.error("[%s] Pursuit package failed: %s", sector_key, exc, exc_info=True)
        return None

    if not html_dest.exists():
        log.error("[%s] Pursuit HTML not found after generation", sector_key)
        return None

    content = html_dest.read_text(encoding="utf-8")
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


# ── Competitor profile ─────────────────────────────────────────────────────────

def regen_competitor(sector_key: str, sector_def: dict, sector_dir: Path,
                     competitor_name: str, sector_context: str) -> "dict | None":
    """Generate competitor profile for sector. Returns manifest item dict or None."""
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
        log.error("[%s] Competitor HTML not found after generation", sector_key)
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


# ── Watch brief ────────────────────────────────────────────────────────────────

def regen_watch_brief(sector_key: str, sector_def: dict, sector_dir: Path) -> "dict | None":
    """Generate watch brief for sector. Returns manifest item dict or None."""
    firm_name = sector_def["firm"]["name"]
    label = sector_def.get("label", sector_key)
    db_tag = sector_def.get("db_tag", sector_key)

    brief_filename = f"watch_brief_{sector_key}_{date.today().isoformat()}.html"
    brief_dest = sector_dir / brief_filename

    try:
        log.info("[%s] Generating watch brief for %s", sector_key, firm_name)
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
        log.error("[%s] Watch brief failed: %s", sector_key, exc, exc_info=True)
        return None

    if not brief_dest.exists():
        log.error("[%s] Watch brief not found after generation", sector_key)
        return None

    content = brief_dest.read_text(encoding="utf-8")
    _save_to_db(f"{sector_key}/{brief_dest.name}", content)
    _upload_to_storage(brief_dest, f"demo/{sector_key}/{brief_dest.name}")
    log.info("[%s] ✓ Watch brief: %s (%d bytes)", sector_key, brief_dest.name, len(content))

    return {
        "type":        "watch_brief",
        "sectors":     sector_key,
        "week_of":     date.today().isoformat(),
        "is_demo":     True,
        "demo_sector": sector_key,
        "demo_label":  f"{label} Watch Brief — week of {date.today().isoformat()}",
        "html_path":   str(brief_dest.relative_to(Path(__file__).parent)),
    }


# ── Verification helpers ───────────────────────────────────────────────────────

def _extract_section_text(html: str, section_title: str) -> str:
    """Extract text content of a named watch brief section (first 150 chars)."""
    m = re.search(
        re.escape(section_title) + r"</div>(.*?)(?=<div class=\"section\">|</body>)",
        html, re.DOTALL,
    )
    if not m:
        return "(section not found)"
    text = re.sub(r"<[^>]+>", " ", m.group(1))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:150]


def verify_all(manifest: dict) -> None:
    """Run all post-generation verification checks."""
    log.info("")
    log.info("=" * 70)
    log.info("VERIFICATION")
    log.info("=" * 70)

    renewal_snippets: dict[str, str] = {}
    signal_snippets: dict[str, str] = {}

    for sk, sector_data in manifest.get("sectors", {}).items():
        sd = DEMO_SECTORS.get(sk, {})
        firm_name = sd.get("firm", {}).get("name", "")
        items = sector_data.get("items", [])
        types = {i.get("type") for i in items}

        pp_ok = "✓" if "pursuit_package" in types else "✗ MISSING"
        cp_ok = "✓" if "competitor_profile" in types else "✗ MISSING"
        wb_ok = "✓" if "watch_brief" in types else "✗ MISSING"
        log.info("  %s  PP=%s  CP=%s  WB=%s", sk.upper().ljust(15), pp_ok, cp_ok, wb_ok)

        # Check firm name in pursuit package
        for item in items:
            if item.get("type") == "pursuit_package":
                hp = Path(__file__).parent / item.get("html_path", "")
                if hp.exists():
                    content = hp.read_text(encoding="utf-8")
                    if firm_name.lower() in content.lower():
                        log.info("    PP firm name ✓ (%s present)", firm_name)
                    else:
                        log.warning("    PP firm name ✗ (%s NOT found)", firm_name)
                    if "SAMPLE" in content or "sample" in content.lower():
                        log.info("    PP SAMPLE banner ✓")
                    else:
                        log.warning("    PP SAMPLE banner ✗ (not found)")
                    # Check win position not NO GO
                    if ">NO GO<" in content:
                        log.warning("    PP win position = NO GO ✗ (should be Competitive/COND GO)")
                    elif "CONDITIONAL GO" in content or ">GO<" in content:
                        log.info("    PP win position ✓")

            if item.get("type") == "competitor_profile":
                comp_name = item.get("competitor_name", "")
                log.info("    CP competitor: %s", comp_name)
                hp = Path(__file__).parent / item.get("html_path", "")
                if hp.exists():
                    content = hp.read_text(encoding="utf-8")
                    if comp_name.lower() in content.lower():
                        log.info("    CP competitor name ✓")
                    else:
                        log.warning("    CP competitor name ✗ (%s not in file)", comp_name)

            if item.get("type") == "watch_brief":
                hp = Path(__file__).parent / item.get("html_path", "")
                if hp.exists():
                    content = hp.read_text(encoding="utf-8")
                    ren = _extract_section_text(content, "Renewal Pipeline — Next 12 Months")
                    sig = _extract_section_text(content, "Market Signals")
                    renewal_snippets[sk] = ren
                    signal_snippets[sk] = sig

    # Watch brief diversity
    log.info("")
    log.info("WATCH BRIEF RENEWAL PIPELINE (first 150 chars each):")
    for sk, snippet in renewal_snippets.items():
        log.info("  [%s] %s", sk.upper().ljust(14), snippet)

    unique_renewals = len(set(renewal_snippets.values()))
    if unique_renewals == len(renewal_snippets):
        log.info("  ✓ All renewal pipeline sections are distinct (%d unique)", unique_renewals)
    else:
        log.warning("  ✗ %d sectors share renewal pipeline content (expected %d unique)",
                    len(renewal_snippets) - unique_renewals, len(renewal_snippets))

    log.info("")
    log.info("WATCH BRIEF MARKET SIGNALS (first 150 chars each):")
    for sk, snippet in signal_snippets.items():
        log.info("  [%s] %s", sk.upper().ljust(14), snippet)

    unique_signals = len(set(signal_snippets.values()))
    if unique_signals == len(signal_snippets):
        log.info("  ✓ All market signals sections are distinct")
    else:
        log.warning("  ✗ Some sectors share market signals content (%d unique)", unique_signals)

    # Specific checks
    log.info("")
    log.info("SPECIFIC CHECKS:")
    # Defence competitor should be Nova Systems
    defence_items = manifest.get("sectors", {}).get("defence", {}).get("items", [])
    defence_comp = next((i for i in defence_items if i.get("type") == "competitor_profile"), None)
    if defence_comp:
        if "nova" in defence_comp.get("competitor_name", "").lower():
            log.info("  ✓ Defence competitor = Nova Systems")
        else:
            log.warning("  ✗ Defence competitor = %s (expected Nova Systems)", defence_comp.get("competitor_name"))
    else:
        log.warning("  ✗ Defence competitor profile missing")

    # Infrastructure competitor should be Downer
    infra_items = manifest.get("sectors", {}).get("infrastructure", {}).get("items", [])
    infra_comp = next((i for i in infra_items if i.get("type") == "competitor_profile"), None)
    if infra_comp:
        if "downer" in infra_comp.get("competitor_name", "").lower():
            log.info("  ✓ Infrastructure competitor = Downer Group")
        else:
            log.warning("  ✗ Infrastructure competitor = %s (expected Downer Group)", infra_comp.get("competitor_name"))

    # Construction close date future
    construction_items = manifest.get("sectors", {}).get("construction", {}).get("items", [])
    construction_pp = next((i for i in construction_items if i.get("type") == "pursuit_package"), None)
    if construction_pp:
        hp = Path(__file__).parent / construction_pp.get("html_path", "")
        if hp.exists():
            content = hp.read_text(encoding="utf-8")
            # Look for close date chip
            m = re.search(r"Close:\s*([\d\-]+)", content)
            if m:
                close_str = m.group(1)
                try:
                    close_dt = date.fromisoformat(close_str)
                    if close_dt > date.today():
                        log.info("  ✓ Construction close date is in future: %s", close_str)
                    else:
                        log.warning("  ✗ Construction close date is in past: %s", close_str)
                except ValueError:
                    log.info("  Construction close date: %s (could not parse)", close_str)

    # Cybersecurity and ICT watch briefs should differ
    cyber_wb = manifest.get("sectors", {}).get("cybersecurity", {}).get("items", [])
    ict_wb = manifest.get("sectors", {}).get("ICT", {}).get("items", [])
    cyber_renewal = renewal_snippets.get("cybersecurity", "")
    ict_renewal = renewal_snippets.get("ICT", "")
    if cyber_renewal and ict_renewal:
        if cyber_renewal != ict_renewal:
            log.info("  ✓ Cybersecurity and ICT watch briefs show different renewal content")
        else:
            log.warning("  ✗ Cybersecurity and ICT watch briefs have IDENTICAL renewal content")

    log.info("=" * 70)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest()
    manifest["generated"] = date.today().isoformat()
    if "sectors" not in manifest:
        manifest["sectors"] = {}

    sep = "=" * 70

    # ── STEP 1: CYBERSECURITY — all 3 artefacts ───────────────────────────────
    log.info(sep)
    log.info("STEP 1: CYBERSECURITY — all 3 artefacts")
    log.info(sep)
    sk = "cybersecurity"
    sd = DEMO_SECTORS[sk]
    sector_dir = DEMO_DIR / sk
    sector_dir.mkdir(parents=True, exist_ok=True)

    # Remove old stale ICT-sector competitor entries from DB
    try:
        db.execute(
            "DELETE FROM pipeline_outputs WHERE output_type='demo_html' "
            "AND filename LIKE 'cybersecurity/competitor_%'"
        )
        log.info("[cybersecurity] Cleared old competitor profile(s) from DB")
    except Exception as e:
        log.warning("[cybersecurity] Could not clear old competitor profiles: %s", e)

    cyber_items: list = []

    pp = regen_pursuit(sk, sd, sector_dir)
    if pp:
        cyber_items.append(pp)

    fc = FORCED_COMPETITORS[sk]
    cp = regen_competitor(sk, sd, sector_dir, fc["name"], fc["sector_context"])
    if cp:
        cyber_items.append(cp)

    wb = regen_watch_brief(sk, sd, sector_dir)
    if wb:
        cyber_items.append(wb)

    manifest["sectors"][sk] = {"firm": sd["firm"], "items": cyber_items}
    log.info("CYBERSECURITY done: %d/3 artefacts", len(cyber_items))

    # ── STEP 2: CONSTRUCTION — pursuit + competitor ───────────────────────────
    log.info(sep)
    log.info("STEP 2: CONSTRUCTION — pursuit package + competitor profile")
    log.info(sep)
    sk = "construction"
    sd = DEMO_SECTORS[sk]
    sector_dir = DEMO_DIR / sk
    sector_dir.mkdir(parents=True, exist_ok=True)

    # Preserve existing watch brief
    construction_items = [
        i for i in manifest["sectors"].get(sk, {}).get("items", [])
        if i.get("type") == "watch_brief"
    ]

    # Clear old competitor from DB
    try:
        db.execute(
            "DELETE FROM pipeline_outputs WHERE output_type='demo_html' "
            "AND filename LIKE 'construction/competitor_%'"
        )
    except Exception as e:
        log.warning("[construction] Could not clear old competitor: %s", e)

    pp = regen_pursuit(sk, sd, sector_dir, require_future_close=True)
    if pp:
        construction_items.append(pp)

    fc = FORCED_COMPETITORS[sk]
    cp = regen_competitor(sk, sd, sector_dir, fc["name"], fc["sector_context"])
    if cp:
        construction_items.append(cp)

    manifest["sectors"][sk] = {"firm": sd["firm"], "items": construction_items}
    log.info("CONSTRUCTION done: %d artefacts", len(construction_items))

    # ── STEP 3: HEALTH — pursuit package only ────────────────────────────────
    log.info(sep)
    log.info("STEP 3: HEALTH — pursuit package + competitor check")
    log.info(sep)
    sk = "health"
    sd = DEMO_SECTORS[sk]
    sector_dir = DEMO_DIR / sk
    sector_dir.mkdir(parents=True, exist_ok=True)

    # Preserve competitor and watch brief
    health_items = [
        i for i in manifest["sectors"].get(sk, {}).get("items", [])
        if i.get("type") in ("competitor_profile", "watch_brief")
    ]

    # Check if competitor profile exists and names a reasonable competitor
    try:
        health_cp_row = db.fetchone(
            "SELECT filename, content FROM pipeline_outputs "
            "WHERE output_type='demo_html' AND filename LIKE 'health/competitor_%' "
            "ORDER BY created_at DESC LIMIT 1"
        )
    except Exception as e:
        log.warning("[health] DB check failed: %s", e)
        health_cp_row = None

    health_comp_ok = False
    if health_cp_row:
        fname = health_cp_row.get("filename", "")
        # Check it's not a generic/wrong competitor (not "heb", not "cityworks", etc.)
        problematic = ["heb_construction", "cityworks", "meridian", "apex", "sentinel"]
        if not any(p in fname.lower() for p in problematic):
            health_comp_ok = True
            log.info("[health] Existing competitor profile acceptable: %s", fname)
        else:
            log.info("[health] Existing competitor profile is wrong (%s) — regenerating with Fisher & Paykel", fname)
    else:
        log.info("[health] No competitor profile in DB — generating Fisher & Paykel Healthcare")

    if not health_comp_ok:
        cp = regen_competitor(
            sk, sd, sector_dir,
            "Fisher & Paykel Healthcare",
            "NZ health technology, clinical systems, hospital ICT and health data platforms",
        )
        if cp:
            health_items = [i for i in health_items if i.get("type") != "competitor_profile"]
            health_items.append(cp)

    # Try up to 4 health notices for acceptable win position
    pp = regen_pursuit(sk, sd, sector_dir, max_tries=4)
    if pp:
        health_items = [i for i in health_items if i.get("type") != "pursuit_package"]
        health_items.append(pp)
    else:
        log.warning("[health] No acceptable pursuit package after 4 attempts — MANUAL REVIEW REQUIRED")
        log.warning("[health] Listing available health notices:")
        try:
            rows = _query_notices_for_sector("health", active_only=True)
            for r in rows[:4]:
                log.warning("  %s days=%s '%s'",
                            r["notice_id"], r.get("days_until_close"), (r.get("title") or "")[:70])
        except Exception as e:
            log.warning("  Could not list health notices: %s", e)

    manifest["sectors"][sk] = {"firm": sd["firm"], "items": health_items}
    log.info("HEALTH done: %d artefacts", len(health_items))

    # ── STEP 4: DEFENCE — competitor profile only ─────────────────────────────
    log.info(sep)
    log.info("STEP 4: DEFENCE — competitor profile (Nova Systems, replaces HEB Construction)")
    log.info(sep)
    sk = "defence"
    sd = DEMO_SECTORS[sk]
    sector_dir = DEMO_DIR / sk
    sector_dir.mkdir(parents=True, exist_ok=True)

    # Preserve pursuit package and watch brief
    defence_items = [
        i for i in manifest["sectors"].get(sk, {}).get("items", [])
        if i.get("type") in ("pursuit_package", "watch_brief")
    ]

    # Remove old HEB Construction profile from DB
    try:
        db.execute(
            "DELETE FROM pipeline_outputs WHERE output_type='demo_html' "
            "AND filename LIKE 'defence/competitor_%'"
        )
        log.info("[defence] Removed old competitor profile from DB")
    except Exception as e:
        log.warning("[defence] Could not remove old competitor: %s", e)

    fc = FORCED_COMPETITORS[sk]
    cp = regen_competitor(sk, sd, sector_dir, fc["name"], fc["sector_context"])
    if cp:
        defence_items.append(cp)

    manifest["sectors"][sk] = {"firm": sd["firm"], "items": defence_items}
    log.info("DEFENCE done: %d artefacts", len(defence_items))

    # ── STEP 5: INFRASTRUCTURE — competitor profile only ─────────────────────
    log.info(sep)
    log.info("STEP 5: INFRASTRUCTURE — competitor profile (Downer Group)")
    log.info(sep)
    sk = "infrastructure"
    sd = DEMO_SECTORS[sk]
    sector_dir = DEMO_DIR / sk
    sector_dir.mkdir(parents=True, exist_ok=True)

    # Preserve pursuit package and watch brief
    infra_items = [
        i for i in manifest["sectors"].get(sk, {}).get("items", [])
        if i.get("type") in ("pursuit_package", "watch_brief")
    ]

    # Remove old incorrect competitor from DB
    try:
        db.execute(
            "DELETE FROM pipeline_outputs WHERE output_type='demo_html' "
            "AND filename LIKE 'infrastructure/competitor_%'"
        )
        log.info("[infrastructure] Removed old competitor profile from DB")
    except Exception as e:
        log.warning("[infrastructure] Could not remove old competitor: %s", e)

    fc = FORCED_COMPETITORS[sk]
    cp = regen_competitor(sk, sd, sector_dir, fc["name"], fc["sector_context"])
    if cp:
        infra_items.append(cp)

    manifest["sectors"][sk] = {"firm": sd["firm"], "items": infra_items}
    log.info("INFRASTRUCTURE done: %d artefacts", len(infra_items))

    # ── STEP 6: ALL 7 WATCH BRIEFS ────────────────────────────────────────────
    log.info(sep)
    log.info("STEP 6: ALL 7 WATCH BRIEFS — regenerate with fixed sector-specific code")
    log.info(sep)
    for wb_sk, wb_sd in DEMO_SECTORS.items():
        wb_sector_dir = DEMO_DIR / wb_sk
        wb_sector_dir.mkdir(parents=True, exist_ok=True)

        # Clear old watch brief DB entries for this sector
        try:
            db.execute(
                "DELETE FROM pipeline_outputs WHERE output_type='demo_html' "
                "AND filename LIKE %s",
                (f"{wb_sk}/watch_brief_%",),
            )
        except Exception as e:
            log.warning("[%s] Could not clear old watch briefs: %s", wb_sk, e)

        wb = regen_watch_brief(wb_sk, wb_sd, wb_sector_dir)
        if wb:
            current = manifest["sectors"].get(wb_sk, {}).get("items", [])
            current = [i for i in current if i.get("type") != "watch_brief"]
            current.append(wb)
            manifest["sectors"][wb_sk] = {
                "firm": wb_sd["firm"],
                "items": current,
            }
        else:
            log.warning("[%s] Watch brief regeneration FAILED", wb_sk)

    # ── Save manifest ──────────────────────────────────────────────────────────
    log.info("")
    log.info("Writing manifest...")
    _write_manifest(manifest)

    # ── Summary ────────────────────────────────────────────────────────────────
    log.info("")
    log.info(sep)
    log.info("REGENERATION COMPLETE")
    log.info(sep)
    total = sum(
        len(manifest["sectors"].get(sk, {}).get("items", []))
        for sk in DEMO_SECTORS
    )
    log.info("Total artefacts in manifest: %d", total)

    # DB verification
    try:
        row = db.fetchone("SELECT COUNT(*) AS cnt FROM pipeline_outputs WHERE output_type='demo_html'")
        cnt = int((row or {}).get("cnt") or 0)
        log.info("DB demo_html rows: %d", cnt)
    except Exception as e:
        log.warning("DB count failed: %s", e)

    # ── Full verification ──────────────────────────────────────────────────────
    verify_all(manifest)


if __name__ == "__main__":
    main()
