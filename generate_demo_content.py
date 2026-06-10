"""
Generate sector-personalised demo content for the /demo public route.

For each of 7 sectors, produces 3 artefacts using a fictional NZ SME as the
client:
  - One pursuit package  (top urgency notice in that sector)
  - One competitor profile (dominant MBIE incumbent in that sector)
  - One watch brief        (filtered to that sector)

All written to output/artefacts/demo/<sector>/ with a single manifest.json:
  {
    "generated": "YYYY-MM-DD",
    "sectors": {
      "<sector_key>": {
        "firm": { name, description, staff, location, strengths },
        "items": [ {type, html_path, ...}, ... ]
      }
    }
  }

Usage:
  python generate_demo_content.py [--force] [--sector <key>]

--force   : regenerate even if artefacts already exist
--sector  : regenerate only one sector (e.g. --sector cybersecurity)
"""
import argparse
import json
import logging
import sys
import os
from datetime import date
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(__file__))

import db
from demo_package import generate_demo_package
from competitor_profile import generate_competitor_profile
from watch_brief import generate_watch_brief
from pursuit_package import _slug

logger = logging.getLogger(__name__)

DEMO_DIR = Path(__file__).parent / "output" / "artefacts" / "demo"
MANIFEST_PATH = DEMO_DIR / "manifest.json"

# ── Sector definitions ────────────────────────────────────────────────────────
# Each entry: sector DB key → display metadata + fictional firm profile

DEMO_SECTORS: dict = {
    "cybersecurity": {
        "label":           "Cybersecurity",
        "icon":            "🔒",
        "tagline":         "Government security assessments, pen testing & compliance advisory",
        "db_tag":          "cybersecurity",
        "fallback_db_tags": [],   # ICT fallback removed — caused cross-sector contamination
        "firm": {
            "name":          "Sentinel Digital",
            "description":   "mid-sized cybersecurity consultancy, 45 staff, Wellington-based, "
                             "8 years operating. Holds All-of-Government security panel membership. "
                             "Prior contracts with MBIE, DIA, and NZDF for security assessments, "
                             "penetration testing, and SOC services. Seeking to expand into health "
                             "sector cyber uplift under Budget 2026 funded programmes.",
            "staff":         45,
            "location":      "Wellington",
            "years_operating": 8,
            "key_clients":   "MBIE, Department of Internal Affairs (DIA), NZ Defence Force (NZDF)",
            "sector_focus":  "Government cybersecurity, SOC services, NZISM compliance, health sector cyber uplift",
            "strengths":     "AoG security panel member, IRAP-assessed team, NZISM compliance expertise, "
                             "existing GCIO relationships, prior NZDF and DIA delivery, "
                             "growing health sector capability",
        },
    },
    "FM": {
        "label":       "Facilities Management",
        "icon":        "🏗",
        "tagline":     "FM contracts for local government, social housing and public estates",
        "firm": {
            "name":          "Cityworks NZ",
            "description":   "established FM contractor, 120 staff, 15 years operating, national coverage. "
                             "Strong track record in local government FM with Wellington City Council and "
                             "Hutt City Council as anchor clients. Approved Kainga Ora panel supplier for "
                             "social housing maintenance. Serves commercial property portfolio clients across "
                             "the lower North Island and Auckland. Mid-market firm seeking to grow into "
                             "university and tertiary sector FM. No prior University of Otago relationship "
                             "but holds credible FM credentials across public and institutional sectors.",
            "staff":         120,
            "location":      "Auckland (national)",
            "years_operating": 15,
            "key_clients":   "Wellington City Council, Hutt City Council, Kainga Ora (social housing panel), "
                             "commercial property portfolio clients",
            "sector_focus":  "Local government FM, social housing maintenance, commercial property, "
                             "tertiary sector growth",
            "strengths":     "BWOF compliance expertise, 15-year public sector track record, "
                             "ISO 41001 certified, Kainga Ora panel supplier, WCC and Hutt City anchor "
                             "relationships, national mobilisation capability",
        },
    },
    "construction": {
        "label":       "Construction",
        "icon":        "🏛",
        "tagline":     "Civil construction, roading and infrastructure delivery",
        "db_tag":      "construction",
        "firm": {
            "name":          "Meridian Civil",
            "description":   "regional civil contractor, 80 staff, Christchurch-based, 12 years operating. "
                             "Strong roading and drainage track record with Canterbury councils and NZTA. "
                             "NZTA-approved contractor. Seeking to extend operations into Otago and "
                             "Southland markets.",
            "staff":         80,
            "location":      "Christchurch (South Island)",
            "years_operating": 12,
            "key_clients":   "Canterbury councils, NZTA / Waka Kotahi (approved contractor)",
            "sector_focus":  "Roading, drainage, civil infrastructure, South Island expansion into Otago/Southland",
            "strengths":     "NQS prequalified, NZTA-approved contractor, strong Canterbury council relationships, "
                             "local South Island supply chain, roading and drainage delivery expertise",
        },
    },
    "defence": {
        "label":       "Defence",
        "icon":        "⚙️",
        "tagline":     "Defence facilities, critical infrastructure and security engineering",
        "firm": {
            "name":          "Apex Engineering",
            "description":   "specialist engineering consultancy, 60 staff, Wellington-based. "
                             "Holds NZ security clearances. Prior NZDF contracts for infrastructure "
                             "engineering including base facilities and critical infrastructure. "
                             "Seeking to grow defence maintenance services portfolio.",
            "staff":         60,
            "location":      "Wellington",
            "years_operating": 12,
            "key_clients":   "NZ Defence Force (NZDF) — prior infrastructure engineering contracts",
            "sector_focus":  "Defence facilities, critical infrastructure engineering, defence maintenance services",
            "strengths":     "NZ security cleared staff, NZDF panel history, SCIF-capable design team, "
                             "defence estate familiarity, infrastructure engineering specialisation",
        },
    },
    "ICT": {
        "label":       "ICT",
        "icon":        "💻",
        "tagline":     "Digital transformation, cloud migration and systems integration",
        "firm": {
            "name":          "Korepath Systems",
            "description":   "digital transformation integrator, 70 staff, Auckland-based, "
                             "10 years operating. DIA-approved supplier. Prior contracts with MSD, IRD, "
                             "and MoE for system integration and platform development. "
                             "Specialises in legacy modernisation and cloud platforms.",
            "staff":         70,
            "location":      "Auckland",
            "years_operating": 10,
            "key_clients":   "Ministry of Social Development (MSD), Inland Revenue (IRD), "
                             "Ministry of Education (MoE) — system integration and platform delivery",
            "sector_focus":  "Digital transformation, legacy modernisation, cloud platforms, central government ICT",
            "strengths":     "DIA-approved supplier, All-of-Government panel (AoG ICT), "
                             "AWS & Azure certified partner, ServiceNow certified, "
                             "prior MSD/IRD/MoE delivery, central government relationships",
        },
    },
    "infrastructure": {
        "label":       "Infrastructure",
        "icon":        "🌐",
        "tagline":     "Water, transport and community infrastructure at scale",
        "firm": {
            "name":          "Southern Civil Group",
            "description":   "infrastructure contractor, 150 staff, Hamilton-based. "
                             "Strong Three Waters and transport infrastructure track record across "
                             "Waikato and Bay of Plenty councils. Tier 2 NZQA registered.",
            "staff":         150,
            "location":      "Hamilton (national)",
            "years_operating": 14,
            "key_clients":   "Waikato councils, Bay of Plenty councils — Three Waters and transport infrastructure",
            "sector_focus":  "Three Waters infrastructure, horizontal infrastructure, transport, "
                             "community infrastructure delivery",
            "strengths":     "Three Waters delivery expertise, horizontal infrastructure, "
                             "Tier 2 NZQA registered, strong Waikato/BOP council relationships, "
                             "large crew capacity for multi-site delivery",
        },
    },
    "health": {
        "label":       "Health",
        "icon":        "🏥",
        "tagline":     "Clinical systems, hospital ICT and health data platforms",
        "firm": {
            "name":          "MedTech Solutions NZ",
            "description":   "health technology provider, 35 staff, Auckland-based, 7 years operating. "
                             "Prior contracts with Te Whatu Ora (Health NZ) for clinical systems and "
                             "hospital ICT. Seeking to expand into Budget 2026 funded cyber uplift projects.",
            "staff":         35,
            "location":      "Auckland",
            "years_operating": 7,
            "key_clients":   "Te Whatu Ora (Health NZ) — clinical systems and hospital ICT delivery",
            "sector_focus":  "Clinical systems, hospital ICT, health data platforms, health sector cyber uplift",
            "strengths":     "HL7 FHIR certified, Te Whatu Ora (Health NZ) approved panel supplier, "
                             "clinical workflow expertise, prior hospital ICT delivery, "
                             "Budget 2026 health cyber uplift alignment",
        },
    },
}


# ── Demo sector allowlists ────────────────────────────────────────────────────
# Hard mapping from demo sector key → allowed DB sector_tags.
# Any notice whose sector_tag is NOT in this list is rejected for that demo.
# cybersecurity allows ICT as a fallback (sparse cybersecurity notices in DB).
DEMO_SECTOR_ALLOWLIST: dict[str, list[str]] = {
    "cybersecurity": ["cybersecurity"],
    "FM":            ["FM"],
    "construction":  ["construction"],
    "defence":       ["defence"],
    "ICT":           ["ICT"],
    "infrastructure":["infrastructure"],
    "health":        ["health"],
}


def validate_demo_notice(notice: dict, demo_sector: str) -> bool:
    """
    Return True if the notice's sector_tag is within the allowlist for demo_sector.
    Used by all three artefact types to reject cross-sector contamination.
    """
    allowed = DEMO_SECTOR_ALLOWLIST.get(demo_sector, [])
    if not allowed:
        return True  # unknown demo_sector — don't filter
    return (notice.get("sector_tag") or "other") in allowed


# ── Data queries ──────────────────────────────────────────────────────────────

def _title_matches_sector(title: str, db_tag: str) -> bool:
    """
    Quick sanity check: does the notice title contain at least one keyword
    from the expected sector? Rejects obviously wrong notices (e.g. 'Animal
    Control' appearing in cybersecurity) without calling Claude.
    """
    import config as _cfg
    kws = _cfg.SECTOR_KEYWORDS.get(db_tag, [])
    if not kws:
        return True  # unknown sector — don't filter
    title_lower = title.lower()
    for kw in kws:
        if " " in kw:
            if kw.lower() in title_lower:
                return True
        else:
            import re as _re
            if len(kw) <= 4:
                if _re.search(r"\b" + _re.escape(kw.lower()) + r"\b", title_lower):
                    return True
            else:
                if kw.lower() in title_lower:
                    return True
    return False


def _query_notices_for_sector(db_tag: str, active_only: bool = True) -> list[dict]:
    """Raw DB query for notices tagged with the given sector."""
    rank_case = (
        "CASE p.value_band "
        "WHEN '10m_plus'   THEN 5 "
        "WHEN '2m_10m'     THEN 4 "
        "WHEN '500k_2m'    THEN 3 "
        "WHEN '100k_500k'  THEN 2 "
        "WHEN 'under_100k' THEN 1 "
        "ELSE 0 END"
    )
    date_clause = (
        "AND (p.days_until_close IS NULL OR p.days_until_close >= 0)"
        if active_only
        else "AND (p.days_until_close IS NULL OR p.days_until_close >= -30)"
    )
    return db.fetchall(
        f"""
        SELECT r.notice_id, r.title, r.agency, r.description, p.sector_tag,
               p.days_until_close, p.value_band
          FROM parsed_notices p
          JOIN raw_notices r ON r.notice_id = p.notice_id
         WHERE p.sector_tag = %s
           {date_clause}
         ORDER BY p.days_until_close ASC NULLS LAST, {rank_case} DESC
         LIMIT 20
        """,
        (db_tag,),
    )


def _top_notice_for_sector(sector: str, db_tag: str, fallback_db_tags: Optional[list] = None) -> Optional[dict]:  # noqa: C901
    """
    Return the best notice for the demo sector using a two-pass filter:

    Pass 1  — sector_tag = db_tag (hard filter, never crosses sectors)
    Pass 2  — title must contain ≥1 keyword from the sector keyword list
              (rejects misclassified notices that slipped through DB tagging)

    If no active notice passes, tries a 30-day lookback on recently-closed
    notices. Returns None if nothing valid exists — the artefact is skipped
    rather than showing wrong-sector content.
    """
    for active_only in (True, False):
        rows = _query_notices_for_sector(db_tag, active_only=active_only)
        if not rows:
            continue
        for row in rows:
            title = row.get("title") or ""
            desc = row.get("description") or ""
            # Hard reject: title must pass keyword check
            if not _title_matches_sector(title, db_tag):
                logger.debug(
                    "Demo notice %s ('%s') rejected — title fails keyword check for '%s'",
                    row["notice_id"], title[:60], db_tag,
                )
                continue
            # Also check description if title alone is thin (e.g. "RFP 2026-01")
            if len(title.split()) <= 3 and not _title_matches_sector(desc[:200], db_tag):
                logger.debug(
                    "Demo notice %s rejected — short title + desc fail keyword check for '%s'",
                    row["notice_id"], db_tag,
                )
                continue
            if not active_only:
                logger.info(
                    "Demo notice %s ('%s') found via 30-day lookback for '%s'",
                    row["notice_id"], title[:60], db_tag,
                )
            return dict(row)

    # Last-resort: for sectors with genuinely sparse tagging (e.g. cybersecurity),
    # try a full-text title search regardless of sector_tag.
    import config as _cfg
    kws = _cfg.SECTOR_KEYWORDS.get(db_tag, [])
    if kws:
        # Build safe SQL conditions:
        # - Multi-word phrases: LIKE %phrase% is safe (no substring ambiguity)
        # - Short single tokens (≤4 chars): use PostgreSQL word-boundary regex \mTOK\M
        # - Longer single tokens: LIKE %token% is safe
        clauses = []
        params: list = []
        for kw in kws:
            kl = kw.lower()
            if " " in kl:
                clauses.append("LOWER(r.title) LIKE %s")
                params.append(f"%{kl}%")
            elif len(kl) <= 4:
                # PostgreSQL regex word-boundary: \m = start-of-word, \M = end-of-word
                clauses.append("LOWER(r.title) ~ %s")
                params.append(r"\m" + kl + r"\M")
            else:
                clauses.append("LOWER(r.title) LIKE %s")
                params.append(f"%{kl}%")
        kw_clauses = " OR ".join(clauses)
        rank_case = (
            "CASE p.value_band "
            "WHEN '10m_plus'   THEN 5 "
            "WHEN '2m_10m'     THEN 4 "
            "WHEN '500k_2m'    THEN 3 "
            "WHEN '100k_500k'  THEN 2 "
            "WHEN 'under_100k' THEN 1 "
            "ELSE 0 END"
        )
        fallback_rows = db.fetchall(
            f"""
            SELECT r.notice_id, r.title, r.agency, r.description, p.sector_tag,
                   p.days_until_close, p.value_band
              FROM parsed_notices p
              JOIN raw_notices r ON r.notice_id = p.notice_id
             WHERE ({kw_clauses})
               AND (p.days_until_close IS NULL OR p.days_until_close >= -30)
             ORDER BY p.days_until_close ASC NULLS LAST, {rank_case} DESC
             LIMIT 10
            """,
            tuple(params),
        )
        for row in fallback_rows:
            logger.info(
                "Demo notice %s ('%s') found via keyword fallback for sector '%s'",
                row["notice_id"], (row.get("title") or "")[:60], db_tag,
            )
            return dict(row)

    # Fallback to alternative db_tags if specified (e.g. cybersecurity → ICT)
    for alt_tag in (fallback_db_tags or []):
        if alt_tag == db_tag:
            continue
        logger.info(
            "Demo notice: no result for '%s', trying fallback db_tag '%s'",
            db_tag, alt_tag,
        )
        result = _top_notice_for_sector(sector, alt_tag, fallback_db_tags=None)
        if result:
            return result

    logger.warning(
        "No valid notice found for sector '%s' (db_tag='%s') — artefact skipped",
        sector, db_tag,
    )
    return None


def _top_competitor_for_sector(sector: str, db_tag: str) -> Optional[str]:
    """
    Return business_name of the MBIE supplier with the most wins in the given
    sector. Uses db_tag for the query. No generic fallback — if the DB tag has
    no MBIE coverage, returns None so the artefact is skipped rather than
    showing a competitor from an unrelated sector.
    """
    row = db.fetchone(
        """
        SELECT s.business_name, COUNT(DISTINCT n.rfx_id) AS wins
          FROM mbie_award_notices n
          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
          JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
         WHERE n.is_awarded
           AND c.sector_tag = %s
         GROUP BY s.business_name
         ORDER BY wins DESC
         LIMIT 1
        """,
        (db_tag,),
    )
    if row:
        logger.info("Top %s competitor: %s (%d wins)", sector, row["business_name"], row["wins"])
        return row["business_name"]

    logger.warning("No MBIE data for sector %s (db_tag=%s) — skipping competitor profile",
                   sector, db_tag)
    return None


# ── Artefact generation ───────────────────────────────────────────────────────

def generate_sector_set(
    sector_key: str,
    sector_def: dict,
    force: bool = False,
) -> list[dict]:
    """
    Generate all 3 demo artefacts for one sector.
    Returns list of manifest item dicts (may be empty on failure).
    """
    firm = sector_def["firm"]
    firm_name = firm["name"]
    # db_tag may differ from sector_key (e.g. 'cybersecurity' → 'security' in DB)
    db_tag = sector_def.get("db_tag", sector_key)
    sector_dir = DEMO_DIR / sector_key
    sector_dir.mkdir(parents=True, exist_ok=True)

    items: list[dict] = []

    # ── 1. Pursuit package ────────────────────────────────────────────────────
    notice = _top_notice_for_sector(sector_key, db_tag, fallback_db_tags=sector_def.get("fallback_db_tags"))
    if not notice:
        logger.warning("No active notices for sector '%s' (db_tag='%s') — skipping pursuit package",
                       sector_key, db_tag)
    else:
        nid = notice["notice_id"]
        title = notice.get("title", "")[:60]
        html_filename = f"DEMO_{_slug(firm_name)}_{nid}.html"
        html_dest = sector_dir / html_filename

        if not html_dest.exists() or force:
            try:
                logger.info("[%s] Generating pursuit package: %s — %s", sector_key, nid, title)
                result = generate_demo_package(
                    notice_id=nid,
                    prospect_name=firm_name,
                    output_dir=sector_dir,
                    generate_pdf=False,
                    firm_profile=firm,
                )
                # generate_demo_package writes DEMO_<slug>_<nid>.html
                if result.get("html") and result["html"].exists():
                    html_dest = result["html"]
                logger.info("[%s] Pursuit package written: %s", sector_key, html_dest)
            except Exception as exc:
                logger.error("[%s] Pursuit package failed: %s", sector_key, exc, exc_info=True)
        else:
            logger.info("[%s] Pursuit package already exists: %s", sector_key, html_dest)

        if html_dest.exists():
            _upload_to_storage(html_dest, f"demo/{sector_key}/{html_dest.name}")
            _save_to_db(f"{sector_key}/{html_dest.name}", html_dest.read_text(encoding="utf-8"))
            items.append({
                "type":       "pursuit_package",
                "notice_id":  nid,
                "sector":     sector_key,
                "title":      title,
                "is_demo":    True,
                "demo_sector": sector_key,
                "demo_label": f"{sector_def['label']} Pursuit Package — {title}",
                "html_path":  str(html_dest.relative_to(Path(__file__).parent)),
                "pdf_path":   None,
            })

    # ── 2. Competitor profile ─────────────────────────────────────────────────
    comp_name = _top_competitor_for_sector(sector_key, db_tag)
    if not comp_name:
        logger.warning("No MBIE competitor for sector '%s' (db_tag='%s') — skipping competitor profile",
                       sector_key, db_tag)
    else:
        comp_filename = f"competitor_{_slug(comp_name)}.html"
        comp_dest = sector_dir / comp_filename

        if not comp_dest.exists() or force:
            try:
                logger.info("[%s] Generating competitor profile: %s", sector_key, comp_name)
                comp_path = generate_competitor_profile(
                    competitor_name=comp_name,
                    client_name=firm_name,
                    sector_context=sector_def.get("tagline", sector_def.get("label", sector_key)),
                    output_dir=sector_dir,
                    is_demo=True,
                )
                comp_dest = comp_path
                logger.info("[%s] Competitor profile written: %s", sector_key, comp_dest)
            except Exception as exc:
                logger.error("[%s] Competitor profile failed: %s", sector_key, exc, exc_info=True)
        else:
            logger.info("[%s] Competitor profile already exists: %s", sector_key, comp_dest)

        if comp_dest.exists():
            _upload_to_storage(comp_dest, f"demo/{sector_key}/{comp_dest.name}")
            _save_to_db(f"{sector_key}/{comp_dest.name}", comp_dest.read_text(encoding="utf-8"))
            items.append({
                "type":            "competitor_profile",
                "competitor_name": comp_name,
                "sector":          sector_key,
                "is_demo":         True,
                "demo_sector":     sector_key,
                "demo_label":      f"{sector_def['label']} Competitor Profile — {comp_name}",
                "html_path":       str(comp_dest.relative_to(Path(__file__).parent)),
            })

    # ── 3. Watch brief ────────────────────────────────────────────────────────
    brief_filename = f"watch_brief_{sector_key}_{date.today().isoformat()}.html"
    brief_dest = sector_dir / brief_filename

    if not brief_dest.exists() or force:
        try:
            logger.info("[%s] Generating watch brief for %s", sector_key, firm_name)
            brief_path = generate_watch_brief(
                client_name=firm_name,
                sectors=[db_tag],      # use DB tag so sector filter hits correct rows
                output_dir=sector_dir,
                demo_sector=sector_key, # hard-filter top-5 to sector allowlist
            )
            # watch_brief writes to the dir with its own filename — rename to ours
            if brief_path.exists() and brief_path != brief_dest:
                brief_path.rename(brief_dest)
            elif brief_path.exists():
                brief_dest = brief_path
            logger.info("[%s] Watch brief written: %s", sector_key, brief_dest)
        except Exception as exc:
            logger.error("[%s] Watch brief failed: %s", sector_key, exc, exc_info=True)
    else:
        logger.info("[%s] Watch brief already exists: %s", sector_key, brief_dest)

    if brief_dest.exists():
        _upload_to_storage(brief_dest, f"demo/{sector_key}/{brief_dest.name}")
        _save_to_db(f"{sector_key}/{brief_dest.name}", brief_dest.read_text(encoding="utf-8"))
        items.append({
            "type":        "watch_brief",
            "sectors":     sector_key,
            "week_of":     date.today().isoformat(),
            "is_demo":     True,
            "demo_sector": sector_key,
            "demo_label":  f"{sector_def['label']} Watch Brief — week of {date.today().isoformat()}",
            "html_path":   str(brief_dest.relative_to(Path(__file__).parent)),
        })

    return items


# ── Manifest ──────────────────────────────────────────────────────────────────

def _load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        try:
            return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"generated": date.today().isoformat(), "sectors": {}}


def _upload_to_storage(local_path: Path, storage_path: str, content_type: str = "text/html") -> None:
    """Upload a file to Supabase Storage (best-effort, silent on failure)."""
    try:
        import storage as _storage
        result = _storage.upload_file(str(local_path), storage_path, content_type=content_type)
        if result:
            logger.info("Uploaded to Storage: %s", storage_path)
        else:
            logger.debug("Storage upload skipped/failed for %s (no credentials?)", storage_path)
    except Exception as exc:
        logger.warning("Storage upload error for %s: %s", storage_path, exc)


def _save_to_db(db_filename: str, content: str, output_type: str = "demo_html") -> None:
    """Persist artefact content to pipeline_outputs (best-effort, silent on failure)."""
    try:
        db.execute(
            """
            INSERT INTO pipeline_outputs (output_type, run_date, filename, content)
            VALUES (%s, CURRENT_DATE, %s, %s)
            ON CONFLICT (output_type, run_date, filename)
            DO UPDATE SET content = EXCLUDED.content, created_at = NOW()
            """,
            (output_type, db_filename, content),
        )
        logger.info("Saved to DB pipeline_outputs: %s / %s", output_type, db_filename)
    except Exception as exc:
        logger.warning("DB save failed for %s/%s: %s", output_type, db_filename, exc)


def _write_manifest(manifest: dict) -> None:
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(manifest, indent=2, default=str)
    MANIFEST_PATH.write_text(text, encoding="utf-8")
    logger.info("Manifest written: %s", MANIFEST_PATH)
    _upload_to_storage(MANIFEST_PATH, "demo/manifest.json", content_type="application/json")
    _save_to_db("manifest.json", text, output_type="demo_manifest")


# ── Entry point ───────────────────────────────────────────────────────────────

def main(force: bool = False, only_sector: Optional[str] = None) -> dict:
    """
    Run demo content generation for all (or one) sector(s).
    Returns a stats dict: {"total": int, "sectors": int, "by_sector": {key: count}}.
    """
    DEMO_DIR.mkdir(parents=True, exist_ok=True)

    manifest = _load_manifest()
    manifest["generated"] = date.today().isoformat()
    if "sectors" not in manifest:
        manifest["sectors"] = {}

    sectors_to_run = (
        {only_sector: DEMO_SECTORS[only_sector]}
        if only_sector and only_sector in DEMO_SECTORS
        else DEMO_SECTORS
    )

    by_sector: dict[str, int] = {}
    for sector_key, sector_def in sectors_to_run.items():
        logger.info("=== Sector: %s (%s) ===", sector_key, sector_def["firm"]["name"])
        items = generate_sector_set(sector_key, sector_def, force=force)
        manifest["sectors"][sector_key] = {
            "firm":  sector_def["firm"],
            "items": items,
        }
        by_sector[sector_key] = len(items)
        logger.info("[%s] %d artefacts generated", sector_key, len(items))

    _write_manifest(manifest)

    total = sum(by_sector.values())
    logger.info("Done. %d total artefacts across %d sectors.", total, len(sectors_to_run))
    return {"total": total, "sectors": len(sectors_to_run), "by_sector": by_sector}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
    p = argparse.ArgumentParser(description="Generate sector-specific demo artefacts")
    p.add_argument("--force",  action="store_true", help="Regenerate even if files already exist")
    p.add_argument("--sector", metavar="KEY",       help="Only regenerate one sector (e.g. FM)")
    args = p.parse_args()

    if args.sector and args.sector not in DEMO_SECTORS:
        print(f"Unknown sector '{args.sector}'. Valid keys: {', '.join(DEMO_SECTORS)}")
        sys.exit(1)

    main(force=args.force, only_sector=args.sector)
