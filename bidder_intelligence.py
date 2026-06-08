"""
bidder_intelligence.py — ACH (Analysis of Competing Hypotheses) bidder analysis.

Replaces the MBIE-only keyword matching approach with Claude-powered reasoning
that considers bundled service requirements, geographic fit, scale, incumbency
signals, and statutory licensing obligations.

Three-tier evidence model:
  ach_analysis — ACH result from Claude (always generated for enriched notices)
  mbie_confirmed — Claude named a firm that also appears in MBIE award history
  mbie_only — legacy MBIE matching (shown when ACH hasn't run yet)

Caching:
  Results are stored in bidder_pool with match_type='ach_analysis'.
  ACH is NOT re-run on every pipeline pass — only when:
    • No ach_analysis row exists for the notice_id, OR
    • The notice's parsed_at timestamp is newer than the ACH row's timestamp
      (i.e. the notice was updated).

Usage:
  from bidder_intelligence import generate_bidder_intelligence
  result = generate_bidder_intelligence(notice)   # returns list[dict] of top 3
  store_ach_results(notice_id, result)            # persists to bidder_pool
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

import config
import db

logger = logging.getLogger(__name__)

# Probability band → display colour (matches win_position.py palette)
PROBABILITY_COLOURS = {
    "High":        "#2a9d8f",   # teal
    "Medium":      "#d4a017",   # amber
    "Medium-Low":  "#e07b39",   # orange
    "Low":         "#8fa3bc",   # muted
}

# ── MBIE context query ─────────────────────────────────────────────────────────

def _mbie_context_for_notice(
    sector: str,
    agency: str,
    limit: int = 8,
) -> str:
    """
    Fetch the top MBIE award winners in this sector and/or with this agency.
    Returned as a compact text string for injection into the Claude prompt.
    """
    agency_word = (agency or "").split()[0] if agency else ""
    try:
        # Sector-level winners
        sector_rows = db.fetchall(
            """
            SELECT s.business_name, COUNT(DISTINCT n.rfx_id) AS wins
              FROM mbie_award_notices n
              JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
              JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
             WHERE n.is_awarded AND c.sector_tag = %s
             GROUP BY s.business_name
             ORDER BY wins DESC
             LIMIT %s
            """,
            (sector, limit),
        )
        # Agency-specific winners (any sector)
        agency_rows: list[dict] = []
        if agency_word:
            agency_rows = db.fetchall(
                """
                SELECT s.business_name, COUNT(DISTINCT n.rfx_id) AS wins
                  FROM mbie_award_notices n
                  JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
                 WHERE n.is_awarded
                   AND LOWER(n.posting_agency) LIKE LOWER(%s)
                 GROUP BY s.business_name
                 ORDER BY wins DESC
                 LIMIT 5
                """,
                (f"%{agency_word}%",),
            )

        lines = []
        if sector_rows:
            names = ", ".join(
                f"{r['business_name']} ({r['wins']} wins)"
                for r in sector_rows
            )
            lines.append(f"Top {sector} sector winners: {names}")
        if agency_rows:
            names = ", ".join(
                f"{r['business_name']} ({r['wins']} wins)"
                for r in agency_rows
            )
            lines.append(f"Top suppliers to {agency}: {names}")
        return "\n".join(lines) if lines else "No MBIE award history available for this sector/agency."
    except Exception as exc:
        logger.warning("MBIE context query failed: %s", exc)
        return "MBIE award history unavailable."


# ── MBIE confirmation lookup ───────────────────────────────────────────────────

def _mbie_wins_for_firm(firm_name: str, sector: str) -> int:
    """
    Return how many MBIE awards the named firm has in this sector.
    Uses first-word matching to handle name variants.
    """
    firm_word = firm_name.split()[0] if firm_name else ""
    if not firm_word:
        return 0
    try:
        row = db.fetchone(
            """
            SELECT COUNT(DISTINCT n.rfx_id) AS wins
              FROM mbie_award_notices n
              JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
              JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
             WHERE n.is_awarded
               AND LOWER(s.business_name) LIKE LOWER(%s)
               AND c.sector_tag = %s
            """,
            (f"%{firm_word}%", sector),
        )
        return int((row or {}).get("wins") or 0)
    except Exception:
        return 0


# ── Claude ACH prompt ──────────────────────────────────────────────────────────

_ACH_SYSTEM = """You are a New Zealand government procurement intelligence analyst specialising in \
competitive market analysis using the Analysis of Competing Hypotheses (ACH) framework.

Your task is to identify the 3 most likely bidding organisations for a specific NZ government \
procurement notice. You must reason systematically about who is actually positioned to win, \
not just who operates in the sector generally.

Consider:
- Bundled service requirements that narrow the eligible field
- Geographic constraints (rural, regional, national scale)
- Scale fit — is this council-scale or national-agency scale?
- Incumbency signals in the agency name or contract type
- Statutory or certification requirements (security licensing, NZQA, etc.)
- Whether large national firms or smaller regional specialists are better positioned
- Animal control, parking enforcement, and after-hours services are distinct from corporate security

For each of the 3 most likely bidders provide:
1. Organisation name (real NZ organisations only)
2. Probability: High / Medium / Medium-Low
3. 2-3 specific evidence bullets explaining WHY they are likely (capabilities, relationships, geography, track record)
4. One key discriminating factor that could disqualify them
5. Size: national / regional / boutique

Return ONLY valid JSON — no markdown, no text outside JSON:
{"bidders": [{"name": str, "probability": "High"|"Medium"|"Medium-Low", "evidence": [str, str], "discriminator": str, "size": "national"|"regional"|"boutique"}]}"""


def _build_ach_prompt(notice: dict, mbie_context: str) -> str:
    title       = notice.get("title") or ""
    agency      = notice.get("agency") or notice.get("agency_name") or ""
    region      = notice.get("geographic_scope") or notice.get("region") or "Not specified"
    sector      = notice.get("sector_tag") or notice.get("sector") or "other"
    description = (notice.get("description") or "")[:1200]
    value_band  = notice.get("value_band") or "unknown"

    value_labels = {
        "under_100k": "Under $100K", "100k_500k": "$100K–$500K",
        "500k_2m": "$500K–$2M", "2m_10m": "$2M–$10M",
        "10m_plus": "$10M+", "unknown": "Value not specified",
    }
    value_str = value_labels.get(value_band, value_band)

    return (
        f"Notice title: {title}\n"
        f"Agency: {agency}\n"
        f"Region / scope: {region}\n"
        f"Sector: {sector}\n"
        f"Contract value: {value_str}\n"
        f"Description: {description or 'Not provided'}\n\n"
        f"MBIE award history context:\n{mbie_context}"
    )


# ── Main ACH function ──────────────────────────────────────────────────────────

def generate_bidder_intelligence(notice: dict) -> list[dict]:
    """
    Run ACH bidder analysis for *notice* using Claude.

    Args:
        notice: Dict with at minimum: title, agency (or agency_name), sector_tag,
                value_band. Description and geographic_scope improve quality.

    Returns:
        List of up to 3 bidder dicts, each containing:
          {name, probability, evidence, discriminator, size,
           mbie_wins, mbie_confirmed, match_type}

    Raises nothing — returns [] on API failure so callers degrade gracefully.
    """
    sector = notice.get("sector_tag") or notice.get("sector") or "other"
    agency = notice.get("agency") or notice.get("agency_name") or ""

    mbie_context = _mbie_context_for_notice(sector, agency)

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            system=_ACH_SYSTEM,
            messages=[{
                "role": "user",
                "content": _build_ach_prompt(notice, mbie_context),
            }],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        bidders_raw = data.get("bidders", [])
    except Exception as exc:
        logger.warning("ACH Claude call failed for notice %s: %s",
                       notice.get("notice_id", "?"), exc)
        return []

    # Validate and enrich each bidder with MBIE confirmation
    results = []
    for b in bidders_raw[:3]:
        name = str(b.get("name") or "").strip()
        if not name:
            continue
        prob  = b.get("probability", "Medium")
        if prob not in PROBABILITY_COLOURS:
            prob = "Medium"
        evidence      = [str(e) for e in (b.get("evidence") or [])[:3]]
        discriminator = str(b.get("discriminator") or "")[:300]
        size          = b.get("size", "national")

        # MBIE confirmation
        wins = _mbie_wins_for_firm(name, sector)
        if wins > 0:
            mbie_note = f"✓ MBIE confirmed — {wins} award{'s' if wins != 1 else ''} in {sector}"
            evidence = [mbie_note] + evidence[:2]   # prepend; keep total ≤ 3
            mbie_confirmed = True
        else:
            evidence = evidence + ["Training knowledge — no MBIE record for this firm in sector"]
            mbie_confirmed = False

        results.append({
            "name":           name,
            "probability":    prob,
            "evidence":       evidence[:3],
            "discriminator":  discriminator,
            "size":           size,
            "mbie_wins":      wins,
            "mbie_confirmed": mbie_confirmed,
            "match_type":     "ach_analysis",
        })

    logger.info(
        "ACH analysis for notice %s: %d bidders identified (%s)",
        notice.get("notice_id", "?"),
        len(results),
        ", ".join(r["name"] for r in results),
    )
    return results


# ── Caching / persistence ──────────────────────────────────────────────────────

def _ach_is_stale(notice_id: str) -> bool:
    """
    Return True if ACH analysis needs to be (re-)generated.
    Stale when: no ach_analysis row exists, OR the notice's parsed_at is newer
    than the most recent ACH row's context_confidence timestamp proxy.
    We use the 'company_context' column as a staleness marker (set to parsed_at ISO string).
    """
    try:
        ach_row = db.fetchone(
            """
            SELECT company_context
              FROM bidder_pool
             WHERE notice_id = %s AND match_type = 'ach_analysis'
             LIMIT 1
            """,
            (notice_id,),
        )
        if not ach_row:
            return True   # No ACH entry yet

        parsed = db.fetchone(
            "SELECT parsed_at FROM parsed_notices WHERE notice_id = %s",
            (notice_id,),
        )
        if not parsed:
            return False

        ach_ts   = (ach_row.get("company_context") or "")[:19]   # "YYYY-MM-DDTHH:MM:SS"
        parse_ts = str(parsed.get("parsed_at") or "")[:19]
        return parse_ts > ach_ts   # notice updated after last ACH run
    except Exception as exc:
        logger.warning("ACH staleness check failed for %s: %s", notice_id, exc)
        return True   # default to regenerate on error


def store_ach_results(notice_id: str, bidders: list[dict]) -> None:
    """
    Persist ACH bidder results to bidder_pool.
    Existing ach_analysis rows for this notice are deleted first (clean replace).
    The parsed_at timestamp is stored in company_context as a staleness marker.
    """
    if not bidders:
        return

    try:
        # Record current parsed_at so staleness check can compare later
        row = db.fetchone(
            "SELECT parsed_at FROM parsed_notices WHERE notice_id = %s",
            (notice_id,),
        )
        ts_marker = str(row["parsed_at"])[:19] if row and row.get("parsed_at") else ""

        # Remove any existing rows for firms that will be written as ACH results.
        # We keep non-ACH rows for other firms intact (they still serve the
        # legacy fallback path for non-enriched notices), but we must remove
        # any row whose (notice_id, firm_name) would conflict with our INSERT.
        firm_names = [b["name"] for b in bidders]
        if firm_names:
            placeholders = ",".join(["%s"] * len(firm_names))
            db.execute(
                f"DELETE FROM bidder_pool WHERE notice_id = %s AND firm_name IN ({placeholders})",
                (notice_id, *firm_names),
            )

        for rank, b in enumerate(bidders, 1):
            reasoning_str = " | ".join(b.get("evidence") or [])
            discriminator = b.get("discriminator") or ""
            if discriminator:
                reasoning_str += f" | ⚡ {discriminator}"

            db.execute(
                """
                INSERT INTO bidder_pool
                    (notice_id, firm_name, sector, size,
                     strategic_importance, intelligence_maturity,
                     relevance_score, match_type, reasoning,
                     company_context, context_confidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    notice_id,
                    b["name"],
                    "",                                        # sector stored on notice
                    b.get("size", "national"),
                    b["probability"],                          # re-use strategic_importance col
                    "ach",                                     # intelligence_maturity marker
                    round(3.0 - (rank - 1) * 0.5, 1),        # 3.0 / 2.5 / 2.0 by rank
                    "ach_analysis",
                    reasoning_str[:2000],
                    ts_marker,                                 # staleness marker
                    "high" if b.get("mbie_confirmed") else "low",
                ),
            )
        logger.info("Stored %d ACH bidders for notice %s", len(bidders), notice_id)
    except Exception as exc:
        logger.error("store_ach_results failed for %s: %s", notice_id, exc)


# ── Batch runner ───────────────────────────────────────────────────────────────

def run_ach_for_enriched(force: bool = False) -> dict:
    """
    Run ACH analysis on all notices that have an enriched_notices entry
    (i.e. the notices that already get AI summaries).

    Args:
        force: Regenerate even when ACH is not stale.

    Returns:
        {"processed": n, "skipped": n, "failed": n}
    """
    enriched_ids = db.fetchall(
        """
        SELECT e.notice_id, r.title, r.agency, r.description,
               p.sector_tag, p.value_band, p.geographic_scope
          FROM enriched_notices e
          JOIN raw_notices r ON r.notice_id = e.notice_id
          JOIN parsed_notices p ON p.notice_id = e.notice_id
         ORDER BY e.enriched_at DESC
        """
    )

    counts = {"processed": 0, "skipped": 0, "failed": 0}

    for row in enriched_ids:
        nid = row["notice_id"]
        if not force and not _ach_is_stale(nid):
            counts["skipped"] += 1
            logger.debug("ACH skip (not stale): %s", nid)
            continue

        try:
            notice = {
                "notice_id":       nid,
                "title":           row.get("title") or "",
                "agency":          row.get("agency") or "",
                "description":     row.get("description") or "",
                "sector_tag":      row.get("sector_tag") or "other",
                "value_band":      row.get("value_band") or "unknown",
                "geographic_scope": row.get("geographic_scope"),
            }
            bidders = generate_bidder_intelligence(notice)
            if bidders:
                store_ach_results(nid, bidders)
                counts["processed"] += 1
            else:
                counts["failed"] += 1
        except Exception as exc:
            logger.error("ACH batch failed for %s: %s", nid, exc)
            counts["failed"] += 1

    logger.info(
        "ACH batch complete — processed=%d skipped=%d failed=%d",
        counts["processed"], counts["skipped"], counts["failed"],
    )
    return counts


# ── Bidder card rendering ──────────────────────────────────────────────────────

def render_ach_card(b: dict) -> str:
    """
    Render one ACH bidder as an HTML card using the portal's CSS custom
    properties. Used by both output.py and portal.py watchlist rendering.

    The dict *b* comes from the bidder_pool row and may contain either the raw
    ACH fields (from generate_bidder_intelligence) or the reconstructed fields
    from _fetch_ach_bidders().
    """
    name        = b.get("firm_name") or b.get("name") or "—"
    prob        = b.get("strategic_importance") or b.get("probability") or "Medium"
    colour      = PROBABILITY_COLOURS.get(prob, "#8fa3bc")
    size        = (b.get("size") or "national").capitalize()
    match_type  = b.get("match_type") or "ach_analysis"
    conf        = b.get("context_confidence") or "low"
    mbie_confirmed = conf == "high"

    # Source badge
    if match_type == "ach_analysis" and mbie_confirmed:
        src_badge = (
            '<span style="font-size:.6rem;font-weight:700;letter-spacing:.06em;'
            'padding:.1rem .4rem;border-radius:3px;background:rgba(42,157,143,.15);'
            'color:#2a9d8f;white-space:nowrap;">✓ MBIE confirmed</span>'
        )
    elif match_type == "ach_analysis":
        src_badge = (
            '<span style="font-size:.6rem;font-weight:700;letter-spacing:.06em;'
            'padding:.1rem .4rem;border-radius:3px;background:rgba(143,163,188,.12);'
            'color:#8fa3bc;white-space:nowrap;">ACH Analysis</span>'
        )
    else:
        src_badge = (
            '<span style="font-size:.6rem;font-weight:700;letter-spacing:.06em;'
            'padding:.1rem .4rem;border-radius:3px;background:rgba(143,163,188,.12);'
            'color:#8fa3bc;white-space:nowrap;">MBIE historical</span>'
        )

    # Evidence bullets and discriminator
    reasoning_raw = b.get("reasoning") or ""
    parts    = [r.strip() for r in reasoning_raw.split("|") if r.strip()]
    bullets  = [p for p in parts if not p.startswith("⚡")]
    disc_parts = [p[2:].strip() for p in parts if p.startswith("⚡")]
    discriminator = disc_parts[0] if disc_parts else ""

    bullets_html = "".join(
        f'<div style="font-size:.76rem;color:var(--text);line-height:1.5;'
        f'padding:.18rem 0;display:flex;gap:.4rem;">'
        f'<span style="color:var(--gold);flex-shrink:0;">•</span>'
        f'<span>{bullet}</span></div>'
        for bullet in bullets[:3]
    )
    discriminator_html = (
        f'<div style="font-size:.72rem;color:var(--muted);font-style:italic;'
        f'margin-top:.35rem;line-height:1.45;">⚡ {discriminator}</div>'
        if discriminator else ""
    )

    return (
        f'<div style="background:var(--surf2);border:1px solid var(--card-border);'
        f'border-radius:7px;padding:.75rem .9rem;margin-bottom:.55rem;">'
        f'<div style="display:flex;justify-content:space-between;align-items:flex-start;'
        f'gap:.5rem;margin-bottom:.4rem;">'
        f'<span style="font-size:.83rem;font-weight:700;color:var(--text);">{name}</span>'
        f'{src_badge}'
        f'</div>'
        f'<div style="display:flex;align-items:center;gap:.5rem;margin-bottom:.5rem;">'
        f'<span style="font-size:.68rem;font-weight:700;letter-spacing:.05em;'
        f'text-transform:uppercase;padding:.12rem .5rem;border-radius:4px;'
        f'background:{colour}22;color:{colour};border:1px solid {colour}44;">'
        f'{prob}</span>'
        f'<span style="font-size:.68rem;color:var(--muted);">{size}</span>'
        f'</div>'
        f'{bullets_html}'
        f'{discriminator_html}'
        f'</div>'
    )


def render_mbie_stub(notice_id: str) -> str:
    """
    Render a lightweight stub shown when ACH hasn't run for this notice.
    Fetches top 3 MBIE-only bidders from bidder_pool for display.
    """
    try:
        rows = db.fetchall(
            """
            SELECT firm_name, match_type, reasoning, strategic_importance
              FROM bidder_pool
             WHERE notice_id = %s AND match_type != 'ach_analysis'
             ORDER BY relevance_score DESC
             LIMIT 3
            """,
            (notice_id,),
        )
    except Exception:
        rows = []

    if not rows:
        return (
            '<div style="font-size:.78rem;color:var(--muted);">'
            'No bidder data available.</div>'
        )

    stub_cards = "".join(
        f'<div style="font-size:.78rem;color:var(--text);padding:.3rem 0;'
        f'border-bottom:1px solid var(--border);">'
        f'{r["firm_name"]}'
        f'<span style="color:var(--muted);margin-left:.5rem;">MBIE historical</span>'
        f'</div>'
        for r in rows
    )
    return (
        stub_cards
        + '<div style="font-size:.7rem;color:var(--muted);margin-top:.5rem;font-style:italic;">'
        'Full ACH analysis available on enriched notices.</div>'
    )


# ── Fetch helpers for existing rendering pipeline ─────────────────────────────

def fetch_ach_bidders(notice_id: str) -> list[dict]:
    """
    Return bidder_pool rows for *notice_id*, preferring ACH rows over MBIE rows.
    Returns [] if none exist.
    """
    try:
        rows = db.fetchall(
            """
            SELECT firm_name, size, strategic_importance, intelligence_maturity,
                   relevance_score, match_type, reasoning, company_context,
                   context_confidence
              FROM bidder_pool
             WHERE notice_id = %s
             ORDER BY
                CASE match_type WHEN 'ach_analysis' THEN 0 ELSE 1 END,
                relevance_score DESC
             LIMIT 3
            """,
            (notice_id,),
        )
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("fetch_ach_bidders failed for %s: %s", notice_id, exc)
        return []


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO,
                         format="%(asctime)s  %(levelname)-8s  %(message)s")

    ap = argparse.ArgumentParser(description="ACH Bidder Intelligence")
    ap.add_argument("--notice-id", help="Run ACH for a specific notice ID")
    ap.add_argument("--run-enriched", action="store_true",
                    help="Run ACH for all enriched notices")
    ap.add_argument("--force", action="store_true",
                    help="Force regeneration even when not stale")
    args = ap.parse_args()

    if args.notice_id:
        row = db.fetchone(
            """SELECT r.notice_id, r.title, r.agency, r.description,
                      p.sector_tag, p.value_band, p.geographic_scope
               FROM raw_notices r JOIN parsed_notices p ON p.notice_id=r.notice_id
               WHERE r.notice_id=%s""",
            (args.notice_id,),
        )
        if not row:
            print(f"Notice {args.notice_id} not found")
        else:
            notice = dict(row)
            bidders = generate_bidder_intelligence(notice)
            store_ach_results(args.notice_id, bidders)
            print(json.dumps(bidders, indent=2))
    elif args.run_enriched:
        result = run_ach_for_enriched(force=args.force)
        print(json.dumps(result, indent=2))
    else:
        ap.print_help()
