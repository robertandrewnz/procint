"""
Prioritisation output module.

Produces:
  - JSON: output/watchlist_YYYY-MM-DD.json
  - Markdown: output/watchlist_YYYY-MM-DD.md
  - HTML: output/watchlist_YYYY-MM-DD.html
"""
import json
import logging
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional, Tuple

import config
import db

logger = logging.getLogger(__name__)


# ── Shared constants ──────────────────────────────────────────────────────────

VALUE_BAND_LABELS = {
    "under_100k":  "< $100k",
    "100k_500k":   "$100k – $500k",
    "500k_2m":     "$500k – $2m",
    "2m_10m":      "$2m – $10m",
    "10m_plus":    "$10m+",
    "unknown":     "Value TBC",
}

SECTOR_COLOURS = {
    "FM":                    "#1a5276",
    "infrastructure":        "#7d6608",
    "ICT":                   "#6c3483",
    "advisory":              "#1a6b3a",
    "health":                "#a93226",
    "security":              "#935116",
    "defence":               "#1a2d4a",
    "utilities":             "#5d6d00",
    "professional_services": "#1f618d",
    "other":                 "#5d6d7e",
}

IMPORTANCE_COLOURS = {
    "high":   "#2a9d8f",
    "medium": "#1a2d4a",
    "low":    "#6c757d",
}

MATURITY_COLOURS = {
    "strong":   "#27ae60",
    "moderate": "#1a2d4a",
    "weak":     "#6c757d",
}


# ── Data assembly ─────────────────────────────────────────────────────────────

def _fetch_watchlist(preferred_sectors: Optional[list[str]] = None) -> list[dict]:
    """
    Fetch and rank the watchlist.

    When preferred_sectors is None/empty the ranking is sector-neutral: all
    sectors score equally so contract value, urgency, and complexity drive
    order.  When preferred_sectors is provided, notices in those sectors are
    boosted to the top using compute_composite_for_client().
    """
    from scoring import compute_composite_for_client  # avoid circular import at module level

    # Fetch a wider pool than TOP_N so client re-ranking has enough to work with.
    pool_limit = config.TOP_N_WATCHLIST * 4
    rows = db.fetchall(
        """
        SELECT
            r.notice_id,
            r.title,
            r.source_url,
            r.agency,
            r.close_date,
            p.sector_tag,
            p.value_band,
            p.days_until_close,
            p.geographic_scope,
            s.composite_score,
            s.score_value,
            s.score_complexity,
            s.score_urgency,
            s.score_reasoning,
            e.summary,
            e.red_flags,
            e.evaluation_weighting,
            e.strategic_framing
        FROM   scored_notices s
        JOIN   raw_notices r    ON r.notice_id = s.notice_id
        JOIN   parsed_notices p ON p.notice_id = s.notice_id
        LEFT JOIN enriched_notices e ON e.notice_id = s.notice_id
        WHERE  s.composite_score >= %s
        ORDER  BY s.composite_score DESC
        LIMIT  %s
        """,
        (config.WATCHLIST_THRESHOLD, pool_limit),
    )

    # Recalculate composite for each notice using client sector preference
    for row in rows:
        row["client_score"] = compute_composite_for_client(
            float(row.get("score_value") or 0),
            float(row.get("score_complexity") or 0),
            float(row.get("score_urgency") or 0),
            row.get("sector_tag") or "other",
            preferred_sectors,
        )

    # Re-rank by client score and return top N
    rows.sort(key=lambda r: r["client_score"], reverse=True)
    return rows[: config.TOP_N_WATCHLIST]


def _fetch_top_bidders(notice_id: str) -> list[dict]:
    """
    Return the top N bidders for *notice_id*.

    ACH rows (match_type='ach_analysis') take absolute priority over MBIE rows.
    When ACH rows are present, they are returned directly (already curated to 3).
    When absent, falls back to the deduplicated MBIE/CSV pool with exclusion logic.
    """
    # ── Prefer ACH results ──────────────────────────────────────────────────
    try:
        ach_rows = db.fetchall(
            """
            SELECT firm_name, size, strategic_importance, intelligence_maturity,
                   relevance_score, match_type, reasoning, company_context,
                   context_confidence
              FROM bidder_pool
             WHERE notice_id = %s AND match_type = 'ach_analysis'
             ORDER BY relevance_score DESC
             LIMIT 3
            """,
            (notice_id,),
        )
        if ach_rows:
            return [dict(r) for r in ach_rows]
    except Exception as exc:
        logger.warning("ACH bidder fetch failed for %s: %s", notice_id, exc)

    # ── Fall back to legacy MBIE/CSV pool ───────────────────────────────────
    from canonical_suppliers import canonical_name, deduplicate_bidders
    from bidders import _firm_is_excluded, _notice_is_specialist

    notice_ctx: dict = {}
    try:
        notice_ctx = dict(db.fetchone(
            """
            SELECT r.notice_id, r.title, r.agency, p.sector_tag
              FROM raw_notices r
              LEFT JOIN parsed_notices p ON p.notice_id = r.notice_id
             WHERE r.notice_id = %s
            """,
            (notice_id,),
        ) or {})
    except Exception:
        pass
    specialist_flag = _notice_is_specialist(notice_ctx) if notice_ctx else None

    pool = db.fetchall(
        """
        SELECT firm_name, size, strategic_importance, intelligence_maturity,
               relevance_score, match_type, reasoning, company_context,
               context_confidence, sector
        FROM   bidder_pool
        WHERE  notice_id = %s AND match_type != 'ach_analysis'
        ORDER  BY
            CASE match_type
                WHEN 'mbie_evidence' THEN 0
                ELSE 1
            END,
            COALESCE(relevance_score, 0) DESC,
            CASE strategic_importance
                WHEN 'high'   THEN 1
                WHEN 'medium' THEN 2
                ELSE               3
            END
        LIMIT 60
        """,
        (notice_id,),
    )

    filtered: list[dict] = []
    for row in pool:
        row["canonical_name"] = canonical_name(row["firm_name"])
        if row.get("relevance_score") is not None:
            row["relevance_score"] = float(row["relevance_score"])
        if notice_ctx:
            r_sectors = [s.strip() for s in (row.get("sector") or "").split("|") if s.strip()]
            if not r_sectors:
                r_sectors = [notice_ctx.get("sector_tag") or "other"]
            if specialist_flag:
                if row.get("match_type") == "csv_inferred":
                    continue
                if _firm_is_excluded(r_sectors, notice_ctx):
                    continue
            else:
                if _firm_is_excluded(r_sectors, notice_ctx):
                    continue
        filtered.append(row)

    deduped = deduplicate_bidders(filtered)
    for row in deduped:
        row["firm_name"] = row["canonical_name"]

    return deduped[: config.TOP_N_BIDDERS_PER_NOTICE]


# ── JSON output ───────────────────────────────────────────────────────────────

def _serialise(obj):
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Not serialisable: {type(obj)}")


def write_json(watchlist: list[dict], output_dir: Path, run_date: date) -> Path:
    for item in watchlist:
        item["bidders"] = _fetch_top_bidders(item["notice_id"])

    path = output_dir / f"watchlist_{run_date.isoformat()}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(watchlist, f, indent=2, default=_serialise)
    logger.info("JSON watchlist written to %s", path)
    return path


# ── Markdown output ───────────────────────────────────────────────────────────

def _format_bidder_md(b: dict) -> str:
    return (
        f"**{b['firm_name']}** "
        f"({b.get('size', '—')} | "
        f"strategic: {b.get('strategic_importance', '—')} | "
        f"intel maturity: {b.get('intelligence_maturity', '—')})"
    )


def write_markdown(watchlist: list[dict], output_dir: Path, run_date: date) -> Path:
    lines = [
        f"# Procurement Intelligence Watchlist — {run_date.isoformat()}",
        "",
        f"Top {len(watchlist)} opportunities ranked by composite strategic score.",
        "",
        "---",
        "",
    ]

    for rank, item in enumerate(watchlist, start=1):
        score = item.get("composite_score") or 0
        value_label = VALUE_BAND_LABELS.get(item.get("value_band") or "unknown", "Value TBC")
        dtc = item.get("days_until_close")
        dtc_str = f"{dtc} days" if dtc is not None else "Unknown"
        close_str = str(item.get("close_date") or "Unknown")
        bidders = _fetch_top_bidders(item["notice_id"])

        red_flags_raw = item.get("red_flags") or ""
        flags = [f.strip() for f in red_flags_raw.split(";") if f.strip()] if red_flags_raw else []
        flags_md = "\n".join(f"  - {f}" for f in flags) if flags else "  - None identified"

        bidders_md = (
            "\n".join(f"  {i+1}. {_format_bidder_md(b)}" for i, b in enumerate(bidders))
            if bidders else "  - No bidder data"
        )

        lines += [
            f"## {rank}. {item.get('title') or 'Untitled'} `[{score}/10]`",
            "",
            "| Field | Value |",
            "|---|---|",
            f"| **Agency** | {item.get('agency') or '—'} |",
            f"| **Sector** | {item.get('sector_tag') or '—'} |",
            f"| **Value** | {value_label} |",
            f"| **Close date** | {close_str} ({dtc_str}) |",
            f"| **Scope** | {item.get('geographic_scope') or '—'} |",
            f"| **Notice** | [{item.get('source_url', '')}]({item.get('source_url', '')}) |",
            "",
        ]

        if item.get("summary"):
            lines += ["**Summary**", "", item["summary"], ""]
        if item.get("strategic_framing"):
            lines += ["**Strategic framing**", "", f"_{item['strategic_framing']}_", ""]

        lines += [
            "**Red flags**", "", flags_md, "",
            "**Likely bidders**", "", bidders_md, "",
            f"_Score reasoning: {item.get('score_reasoning') or '—'}_",
            "", "---", "",
        ]

    path = output_dir / f"watchlist_{run_date.isoformat()}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    logger.info("Markdown watchlist written to %s", path)
    return path


# ── HTML output ───────────────────────────────────────────────────────────────

def _score_bar(score: float) -> str:
    pct = min(100, (float(score) / 10) * 100)
    colour = "#2a9d8f" if pct >= 65 else "#1a2d4a" if pct >= 40 else "#6c757d"
    return (
        f'<div class="score-bar-track">'
        f'<div class="score-bar-fill" style="width:{pct:.1f}%;background:{colour};"></div>'
        f'</div>'
    )


def _dtc_badge(dtc) -> str:
    if dtc is None:
        return '<span class="badge badge-grey">Close TBC</span>'
    if dtc == 0:
        return '<span class="badge badge-red">Closes today</span>'
    if dtc <= 7:
        cls, label = "badge-red",  f"URGENT — {dtc}d"
    elif dtc <= 14:
        cls, label = "badge-gold", f"Closes in {dtc}d"
    elif dtc <= 30:
        cls, label = "badge-navy", f"{dtc} days"
    else:
        cls, label = "badge-grey", f"{dtc} days"
    return f'<span class="badge {cls}">{label}</span>'


def _sector_badge(sector: str) -> str:
    colour = SECTOR_COLOURS.get(sector, "#5d6d7e")
    label = (sector or "other").replace("_", " ").upper()
    return (
        f'<span class="sector-badge" '
        f'style="background:{colour}18;color:{colour};border-color:{colour}40;">'
        f'{label}</span>'
    )


def _recommended_actions(item: dict) -> list[str]:
    """
    Generate 2-4 plain-language recommended actions for a prospective bidder.
    Heuristic fallback used when Claude enrichment has not run.
    """
    actions = []
    dtc = item.get("days_until_close")
    sector = item.get("sector_tag") or "other"
    value_band = item.get("value_band") or "unknown"
    notice_type = (item.get("category_raw") or "").upper()
    agency = item.get("agency") or "the agency"

    # Urgency-driven action
    if dtc is not None and dtc <= 3:
        actions.append(
            f"Immediate decision required — close date is in {dtc} day{'s' if dtc != 1 else ''}. "
            "Assess bid/no-bid today and assign resources if proceeding."
        )
    elif dtc is not None and dtc <= 7:
        actions.append(
            f"Fast-track internal go/no-go — only {dtc} days to close. "
            "Pull any prior relationship context with this agency immediately."
        )
    elif dtc is not None and dtc <= 21:
        actions.append(
            f"Initiate go/no-bid assessment this week. {dtc} days to close leaves limited time "
            "for teaming discussions or site visits."
        )
    else:
        actions.append(
            "Register interest on GETS to signal market presence and receive any addenda or clarification notices."
        )

    # Notice-type action
    if "EOI" in notice_type or "EXPRESSION" in notice_type:
        actions.append(
            "This is an Expression of Interest — submit a capability statement focused on relevant past performance "
            "rather than pricing. Use it to shape the RFP scope by highlighting your firm's differentiators."
        )
    elif "ROI" in notice_type or "REGISTRATION" in notice_type:
        actions.append(
            "This is a Registration of Interest — submit to be included in the shortlist for the formal RFP stage. "
            "Focus on capability, track record, and understanding of the agency's objectives."
        )
    elif "RFP" in notice_type:
        actions.append(
            "Full RFP — review evaluation criteria carefully and weight your response accordingly. "
            "Consider teaming if capability gaps exist."
        )

    # Sector-specific action
    sector_actions = {
        "infrastructure": (
            f"Contact {agency} project or procurement team to clarify scope, site access, and any "
            "pre-qualification requirements not stated in the notice."
        ),
        "FM": (
            "Review incumbent contract history via GETS award notices and OIA if needed. "
            "Understand existing service levels before pricing mobilisation risk."
        ),
        "ICT": (
            "Identify whether this is a panel arrangement or standalone contract — "
            "check All-of-Government (AoG) alignment and whether a GETS panel already covers this scope."
        ),
        "defence": (
            "Confirm security clearance requirements early — NZDF and intelligence contracts "
            "often require NZ citizenship for key personnel and facility clearances that affect teaming options."
        ),
        "health": (
            "Verify Health NZ procurement pathway — some health contracts must be channelled through "
            "national supply agreements or PHARMAC panels. Confirm this notice is outside those frameworks."
        ),
        "advisory": (
            "Review the agency's recent strategy documents and relevant Treasury/SSC guidance "
            "to demonstrate alignment with current government priorities in your proposal."
        ),
        "utilities": (
            "Check whether this falls under a Commerce Commission regulated procurement process "
            "or standard GETS pathway — regulated network businesses have additional disclosure obligations."
        ),
        "security": (
            "Confirm whether this requires a Private Security Personnel and Private Investigators Act "
            "licence and any vetting or clearance requirements for deployed staff."
        ),
    }
    if sector in sector_actions:
        actions.append(sector_actions[sector])

    # Value-scale action
    if value_band in ("2m_10m", "10m_plus"):
        actions.append(
            "Given the contract scale, assess teaming or sub-contracting options early. "
            "Large government contracts often require demonstrated consortium capability or local content commitments."
        )
    elif value_band == "unknown":
        actions.append(
            "Estimated value is not stated — request a pre-bid briefing or review the agency's "
            "annual procurement plan to calibrate the likely scale before committing bid resources."
        )

    return actions[:4]  # cap at 4


def _bidder_card(b: dict) -> str:
    """Render one bidder card with evidence bullets.

    ACH rows are dispatched to bidder_intelligence.render_ach_card() which
    uses the portal's CSS custom properties.  Legacy MBIE/CSV rows use the
    original HTML-template-scoped CSS classes.
    """
    match_type = b.get("match_type") or "csv_inferred"

    # ACH rows → use the new ACH renderer
    if match_type == "ach_analysis":
        try:
            from bidder_intelligence import render_ach_card
            return render_ach_card(b)
        except Exception as exc:
            logger.warning("render_ach_card failed: %s", exc)

    # ── Legacy MBIE/CSV renderer ─────────────────────────────────────────────
    imp = b.get("strategic_importance", "low")
    mat = b.get("intelligence_maturity", "weak")
    imp_col = IMPORTANCE_COLOURS.get(imp, "#6c757d")
    mat_col = MATURITY_COLOURS.get(mat, "#6c757d")
    size = (b.get("size") or "—").capitalize()
    confidence = b.get("context_confidence") or "unknown"

    # Evidence source badge
    if match_type == "mbie_evidence":
        src_badge = ('<span class="bidder-src-badge bidder-src-mbie">'
                     '&#10003; MBIE historical</span>')
    elif match_type in ("exact", "cross_sector"):
        src_badge = ('<span class="bidder-src-badge bidder-src-inferred">'
                     '&#9675; Sector inference</span>')
    else:
        src_badge = ""

    # Company context (Claude-generated profile)
    context = b.get("company_context") or ""
    if context and confidence != "not_run":
        conf_flag = (" &#9888;" if confidence == "low" else "")
        context_html = (f'<div class="bidder-context">{context}{conf_flag}</div>')
    else:
        context_html = ""

    # Reasoning / evidence bullets — pipe-separated in DB
    reasoning_raw = b.get("reasoning") or ""
    bullets = [r.strip() for r in reasoning_raw.split("|") if r.strip()]
    if bullets:
        bullet_html = "".join(
            f'<div class="bidder-bullet">&#x2022; {bullet}</div>'
            for bullet in bullets[:3]
        )
        reasoning_html = f'<div class="bidder-reasoning">{bullet_html}</div>'
    elif match_type == "mbie_evidence":
        reasoning_html = ('<div class="bidder-reasoning">'
                          '<div class="bidder-bullet">&#x2022; MBIE award history match</div>'
                          '</div>')
    else:
        reasoning_html = ""

    return (
        f'<div class="bidder-card">'
        f'<div class="bidder-header">'
        f'<span class="bidder-name">{b["firm_name"]}</span>'
        f'{src_badge}'
        f'</div>'
        f'<div class="bidder-pills">'
        f'<span class="bidder-pill" style="color:{imp_col};border-color:{imp_col}44;">&#9650; {imp}</span>'
        f'<span class="bidder-pill" style="color:{mat_col};border-color:{mat_col}44;">&#9711; {mat}</span>'
        f'<span class="bidder-meta">{size}</span>'
        f'</div>'
        f'{context_html}'
        f'{reasoning_html}'
        f'</div>'
    )


# Keep old name as alias so any external callers still work
_bidder_row = _bidder_card


def _notice_card(rank: int, item: dict, bidders: list[dict]) -> str:
    title = item.get("title") or "Untitled"
    agency = item.get("agency") or "—"
    score = float(item.get("composite_score") or 0)
    sector = item.get("sector_tag") or "other"
    value_label = VALUE_BAND_LABELS.get(item.get("value_band") or "unknown", "Value TBC")
    close_str = str(item.get("close_date") or "—")
    dtc = item.get("days_until_close")
    scope = item.get("geographic_scope") or "—"
    url = item.get("source_url") or "#"
    summary = item.get("summary")
    framing = item.get("strategic_framing")
    red_flags_raw = item.get("red_flags") or ""
    flags = [f.strip() for f in red_flags_raw.split(";") if f.strip()]

    summary_html = f'<p class="summary-text">{summary}</p>' if summary else \
        '<p class="summary-placeholder">AI summary will appear here once enrichment runs.</p>'

    framing_html = f'<div class="framing-block"><span class="framing-label">Strategic framing</span><p>{framing}</p></div>' \
        if framing else ""

    flags_html = "".join(
        f'<div class="flag-item"><span class="flag-icon">⚠</span>{f}</div>' for f in flags
    ) if flags else '<div class="flag-item no-flags">No red flags identified</div>'

    bidders_html = "".join(_bidder_card(b) for b in bidders) if bidders else \
        '<div class="bidder-row"><span class="bidder-meta">No bidder data</span></div>'

    actions = _recommended_actions(item)
    actions_html = "".join(
        f'<div class="action-item"><span class="action-num">{i+1}</span><span class="action-text">{a}</span></div>'
        for i, a in enumerate(actions)
    )

    return f"""
  <div class="card">
    <div class="card-header">
      <div class="rank-badge">#{rank}</div>
      <div class="card-header-main">
        <div class="card-title-row">
          <h2 class="card-title">{title}</h2>
          {_sector_badge(sector)}
          {_dtc_badge(dtc)}
        </div>
        <div class="card-agency">{agency}</div>
      </div>
      <div class="score-block">
        <div class="score-number">{score:.2f}</div>
        <div class="score-label">/ 10</div>
        {_score_bar(score)}
      </div>
    </div>

    <div class="card-meta-row">
      <div class="meta-item">
        <span class="meta-label">Value</span>
        <span class="meta-value">{value_label}</span>
      </div>
      <div class="meta-item">
        <span class="meta-label">Close date</span>
        <span class="meta-value">{close_str}</span>
      </div>
      <div class="meta-item">
        <span class="meta-label">Scope</span>
        <span class="meta-value">{scope}</span>
      </div>
      <div class="meta-item">
        <span class="meta-label">Notice</span>
        <span class="meta-value"><a href="{url}" target="_blank" rel="noopener">View on GETS ↗</a></span>
      </div>
    </div>

    <div class="card-body">
      <div class="col-intel">
        <div class="section-label">Intelligence summary</div>
        {summary_html}
        {framing_html}
        <div class="section-label" style="margin-top:1rem;">Red flags</div>
        <div class="flags-list">{flags_html}</div>
      </div>
      <div class="col-actions">
        <div class="section-label">Recommended actions</div>
        <div class="actions-list">{actions_html}</div>
      </div>
      <div class="col-bidders">
        <div class="section-label">Likely bidders</div>
        <div class="bidders-list">{bidders_html}</div>
      </div>
    </div>
  </div>"""


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Groundwork by BidEdge — Procurement Watchlist {run_date}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --bg:#f5f6f8; --surface:#ffffff; --surf2:#f0f2f5; --border:#e2e6ea;
      --text:#2c3e50; --muted:#6c757d; --navy:#1a2d4a; --gold:#2a9d8f;
      --gold-l:#e0f4f2; --navy-l:#e8ecf3; --red:#c0392b; --red-l:#fdecea;
      --green:#27ae60; --accent:#2a9d8f;
      --font:'Inter',system-ui,-apple-system,sans-serif;
    }}
    body {{ background:var(--bg); color:var(--text); font-family:var(--font);
            font-size:14px; line-height:1.6; padding:2rem 1.5rem;
            -webkit-font-smoothing:antialiased; }}
    a {{ color:var(--navy); text-decoration:none; }}
    a:hover {{ color:var(--gold); }}

    /* ── Page header ── */
    .report-header {{ max-width:1200px; margin:0 auto 2rem;
      display:flex; align-items:flex-end; justify-content:space-between;
      border-bottom:2px solid var(--navy); padding-bottom:1.25rem; }}
    .brand-name {{ font-size:1.1rem; font-weight:800; color:var(--navy);
      letter-spacing:-.01em; }}
    .brand-name .by {{ font-weight:400; color:var(--muted); font-size:.9rem; }}
    .brand-sub {{ font-size:.7rem; font-weight:700; letter-spacing:.1em;
      text-transform:uppercase; color:var(--gold); margin-top:.2rem; }}
    .report-meta {{ text-align:right; font-size:.75rem; color:var(--muted); }}
    .report-meta strong {{ display:block; font-size:1.4rem; font-weight:800;
      color:var(--navy); letter-spacing:-.03em; }}

    /* ── Explainer ── */
    .explainer {{ max-width:1200px; margin:0 auto 2rem;
      background:var(--surface); border:1px solid var(--border);
      border-radius:8px; padding:1.5rem;
      display:grid; grid-template-columns:1fr 1fr 1fr 1fr; gap:1.5rem;
      box-shadow:0 1px 4px rgba(26,45,74,.06); }}
    .explainer-section h3 {{ font-size:.7rem; font-weight:700; letter-spacing:.08em;
      text-transform:uppercase; color:var(--navy); margin-bottom:.6rem; }}
    .explainer-section p {{ font-size:.78rem; color:var(--muted); line-height:1.6; margin:0; }}
    .score-breakdown {{ display:flex; flex-direction:column; gap:.4rem; margin-top:.4rem; }}
    .score-dim {{ display:flex; align-items:center; gap:.5rem; font-size:.75rem; color:var(--muted); }}
    .score-dim-bar {{ flex:1; height:3px; background:var(--border); border-radius:2px; overflow:hidden; }}
    .score-dim-fill {{ height:100%; border-radius:2px; background:var(--gold); }}
    .score-dim-weight {{ min-width:1.5rem; text-align:right; color:var(--gold); font-weight:700; }}
    .score-dim-label {{ min-width:4.5rem; }}

    /* ── Cards ── */
    .cards {{ max-width:1200px; margin:0 auto;
      display:flex; flex-direction:column; gap:1.25rem; }}
    .card {{ background:var(--surface); border:1px solid var(--border);
      border-radius:8px; overflow:hidden;
      box-shadow:0 1px 4px rgba(26,45,74,.06); }}
    .card-header {{ background:var(--navy); color:#fff;
      display:flex; align-items:flex-start; gap:1rem;
      padding:1.25rem 1.5rem; }}
    .rank-badge {{ flex-shrink:0; width:2.2rem; height:2.2rem; border-radius:50%;
      background:rgba(42,157,143,.25); color:var(--gold);
      font-size:.72rem; font-weight:700;
      display:flex; align-items:center; justify-content:center; margin-top:.1rem; }}
    .card-header-main {{ flex:1; min-width:0; }}
    .card-title-row {{ display:flex; align-items:center; flex-wrap:wrap;
      gap:.5rem; margin-bottom:.3rem; }}
    .card-title {{ font-size:.95rem; font-weight:600; color:#fff; line-height:1.4; }}
    .card-agency {{ font-size:.78rem; color:rgba(255,255,255,.65); }}

    /* Score */
    .score-block {{ flex-shrink:0; text-align:right; min-width:80px; }}
    .score-number {{ font-size:1.75rem; font-weight:800; color:var(--gold);
      letter-spacing:-.04em; line-height:1; }}
    .score-label {{ font-size:.65rem; color:rgba(255,255,255,.5); margin-bottom:.4rem; }}
    .score-bar-track {{ height:4px; background:rgba(255,255,255,.2);
      border-radius:2px; overflow:hidden; width:80px; }}
    .score-bar-fill {{ height:100%; border-radius:2px; }}

    /* Badges */
    .badge {{ display:inline-flex; align-items:center; padding:.2rem .6rem;
      border-radius:999px; font-size:.68rem; font-weight:600;
      letter-spacing:.03em; border:1px solid; white-space:nowrap; }}
    .badge-red   {{ background:#fdecea; color:var(--red);  border-color:#f1a9a0; }}
    .badge-gold  {{ background:var(--gold-l); color:#1a6b62; border-color:var(--gold); }}
    .badge-navy  {{ background:var(--navy-l); color:var(--navy); border-color:#b0bcd4; }}
    .badge-grey  {{ background:var(--surf2);  color:var(--muted); border-color:var(--border); }}
    .sector-badge {{ display:inline-flex; align-items:center; padding:.2rem .55rem;
      border-radius:4px; font-size:.65rem; font-weight:700;
      letter-spacing:.06em; border:1px solid; }}

    /* Meta row */
    .card-meta-row {{ display:flex; border-bottom:1px solid var(--border); }}
    .meta-item {{ flex:1; padding:.65rem 1.5rem; border-right:1px solid var(--border); }}
    .meta-item:last-child {{ border-right:none; }}
    .meta-label {{ display:block; font-size:.63rem; font-weight:700; letter-spacing:.07em;
      text-transform:uppercase; color:var(--muted); margin-bottom:.2rem; }}
    .meta-value {{ font-size:.82rem; color:var(--text); }}
    .meta-value a {{ color:var(--navy); }}
    .meta-value a:hover {{ color:var(--gold); }}

    /* Card body — three columns */
    .card-body {{ display:flex; }}
    .col-intel   {{ flex:1.8; padding:1.25rem 1.5rem; border-right:1px solid var(--border); }}
    .col-actions {{ flex:1.4; padding:1.25rem 1.5rem; border-right:1px solid var(--border); }}
    .col-bidders {{ flex:1.2; padding:1.25rem 1.5rem; }}
    .section-label {{ font-size:.65rem; font-weight:700; letter-spacing:.08em;
      text-transform:uppercase; color:var(--navy); margin-bottom:.65rem; display:block; }}

    /* Summary & framing */
    .summary-text {{ font-size:.84rem; color:var(--text); line-height:1.7; }}
    .summary-placeholder {{ font-size:.82rem; color:var(--muted); font-style:italic; }}
    .framing-block {{ margin-top:1rem; padding:.75rem 1rem;
      background:#e0f4f2; border-left:3px solid var(--gold); border-radius:0 4px 4px 0; }}
    .framing-label {{ font-size:.65rem; font-weight:700; letter-spacing:.08em;
      text-transform:uppercase; color:var(--gold); display:block; margin-bottom:.3rem; }}
    .framing-block p {{ font-size:.82rem; color:var(--text); font-style:italic; margin:0; }}

    /* Flags */
    .flags-list {{ display:flex; flex-direction:column; gap:.4rem; }}
    .flag-item {{ display:flex; align-items:flex-start; gap:.5rem;
      font-size:.8rem; color:var(--text); }}
    .flag-icon {{ color:var(--red); flex-shrink:0; }}
    .no-flags {{ color:var(--muted); font-style:italic; }}

    /* Actions */
    .actions-list {{ display:flex; flex-direction:column; gap:.65rem; }}
    .action-item {{ display:flex; align-items:flex-start; gap:.6rem; }}
    .action-num {{ flex-shrink:0; width:1.3rem; height:1.3rem; border-radius:50%;
      background:var(--navy); color:#fff; font-size:.65rem; font-weight:700;
      display:flex; align-items:center; justify-content:center; margin-top:.1rem; }}
    .action-text {{ font-size:.8rem; color:var(--text); line-height:1.6; }}

    /* Bidders */
    .bidders-list {{ display:flex; flex-direction:column; gap:.75rem; }}
    .bidder-card {{ background:var(--surf2); border:1px solid var(--border);
      border-radius:6px; padding:.65rem .85rem; }}
    .bidder-header {{ display:flex; align-items:center; gap:.5rem;
      flex-wrap:wrap; margin-bottom:.3rem; }}
    .bidder-name {{ font-size:.82rem; font-weight:700; color:var(--navy); flex:1; min-width:0; }}
    .bidder-meta {{ font-size:.72rem; color:var(--muted); }}
    .bidder-pill {{ font-size:.68rem; font-weight:600; padding:.15rem .45rem;
      border-radius:999px; border:1px solid; white-space:nowrap; }}
    .bidder-context {{ font-size:.78rem; color:var(--muted); line-height:1.55;
      margin-bottom:.35rem; font-style:italic; }}
    .bidder-reasoning {{ display:flex; flex-direction:column; gap:.2rem; }}
    .bidder-bullet {{ font-size:.74rem; color:var(--muted); line-height:1.4; }}
    .conf-flag {{ font-size:.65rem; color:var(--red); margin-left:.3rem; }}
    .bidder-empty {{ font-size:.8rem; color:var(--muted); font-style:italic; }}
    .bidder-no-context {{ font-size:.72rem; color:#9aa5b4; font-style:italic;
      margin-bottom:.25rem; border-top:1px solid var(--border);
      padding-top:.3rem; margin-top:.3rem; }}
    .bidder-pills {{ display:flex; align-items:center; gap:.4rem;
      flex-wrap:wrap; margin:.3rem 0 .4rem; }}
    .bidder-src-badge {{ font-size:.62rem; font-weight:600; padding:.12rem .45rem;
      border-radius:4px; letter-spacing:.03em; flex-shrink:0; }}
    .bidder-src-mbie     {{ background:#eafaf1; color:var(--green); border:1px solid #a9dfbf; }}
    .bidder-src-inferred {{ background:var(--navy-l); color:var(--navy); border:1px solid #b0bcd4; }}

    /* Report footer */
    .report-footer {{ max-width:1200px; margin:2rem auto 0; padding-top:1rem;
      border-top:1px solid var(--border); font-size:.72rem; color:var(--muted);
      display:flex; justify-content:space-between; }}

    /* ── Mobile viewport meta & touch targets ── */
    /* (viewport meta tag is in <head> above) */

    /* ── Tablet ≤768px ── */
    @media (max-width:768px) {{
      body {{ padding:1rem .75rem; }}
      .report-header {{ flex-direction:column; align-items:flex-start; gap:.5rem; }}
      .report-meta {{ text-align:left; }}
      .explainer {{ grid-template-columns:1fr 1fr; gap:1rem; padding:1rem; }}
      .card-meta-row {{ flex-wrap:wrap; }}
      .meta-item {{ flex:0 0 50%; border-right:none !important;
        border-bottom:1px solid var(--border); padding:.5rem 1rem; }}
      .meta-item:nth-last-child(-n+2) {{ border-bottom:none; }}
      .card-body {{ flex-direction:column; }}
      .col-intel   {{ border-right:none; border-bottom:1px solid var(--border); padding:1rem; }}
      .col-actions {{ border-right:none; border-bottom:1px solid var(--border); padding:1rem; }}
      .col-bidders {{ padding:1rem; }}
      .report-footer {{ flex-direction:column; gap:.35rem; }}
    }}

    /* ── Phone ≤480px ── */
    @media (max-width:480px) {{
      body {{ padding:.75rem .5rem; font-size:13px; }}
      .report-header {{ padding-bottom:.85rem; margin-bottom:1.25rem; }}
      .brand-name {{ font-size:1rem; }}
      .report-meta strong {{ font-size:1.1rem; }}

      /* Explainer: collapse to single accordion-style column */
      .explainer {{ grid-template-columns:1fr; gap:.65rem; padding:.75rem; }}
      .explainer-section {{ border-bottom:1px solid var(--border); padding-bottom:.65rem; }}
      .explainer-section:last-child {{ border-bottom:none; padding-bottom:0; }}

      /* Card header */
      .card-header {{ padding:.85rem .9rem; gap:.65rem; }}
      .card-title {{ font-size:.88rem; }}
      .score-number {{ font-size:1.4rem; }}
      .score-bar-track {{ width:56px; }}

      /* Meta row: 2-up then 2-up */
      .meta-item {{ flex:0 0 50%; padding:.45rem .75rem; }}

      /* Body columns */
      .col-intel, .col-actions, .col-bidders {{ padding:.85rem .9rem; }}

      /* Badges/tags: ensure 44px min touch height on links */
      .badge {{ font-size:.65rem; padding:.25rem .6rem; }}
      .card-title-row {{ gap:.4rem; }}

      /* Bidder cards */
      .bidder-card {{ padding:.55rem .75rem; }}

      /* Score bar */
      .score-block {{ min-width:60px; }}
    }}
  </style>
</head>
<body>

  <div class="report-header">
    <div>
      <div class="brand-name">Groundwork <span class="by">by BidEdge</span></div>
      <div class="brand-sub">NZ Government Procurement Intelligence</div>
    </div>
    <div class="report-meta">
      <strong>{notice_count}</strong>
      opportunities &middot; {run_date}
    </div>
  </div>

  <div class="explainer">
    <div class="explainer-section">
      <h3>What this is</h3>
      <p>Daily watchlist of active NZ government procurement notices from GETS (gets.govt.nz), scored and ranked for strategic relevance. Notices are ingested each morning, AI-enriched, and matched to likely bidders from 27,948 historical awards.</p>
    </div>
    <div class="explainer-section">
      <h3>How scores are calculated</h3>
      <p>Each notice is scored 1&ndash;10 across four dimensions.</p>
      <div class="score-breakdown">
        <div class="score-dim"><span class="score-dim-label">Contract value</span><div class="score-dim-bar"><div class="score-dim-fill" style="width:75%"></div></div><span class="score-dim-weight">30%</span></div>
        <div class="score-dim"><span class="score-dim-label">Sector priority</span><div class="score-dim-bar"><div class="score-dim-fill" style="width:75%"></div></div><span class="score-dim-weight">30%</span></div>
        <div class="score-dim"><span class="score-dim-label">Eval complexity</span><div class="score-dim-bar"><div class="score-dim-fill" style="width:50%"></div></div><span class="score-dim-weight">20%</span></div>
        <div class="score-dim"><span class="score-dim-label">Days to close</span><div class="score-dim-bar"><div class="score-dim-fill" style="width:50%"></div></div><span class="score-dim-weight">20%</span></div>
      </div>
    </div>
    <div class="explainer-section">
      <h3>Sector priorities</h3>
      <p>All sectors weighted equally by default. Scores are driven by contract value, urgency, and evaluation complexity. Sector preference can be configured per client to re-rank results.</p>
    </div>
    <div class="explainer-section">
      <h3>How to use this report</h3>
      <p>Check the <strong>close date badge</strong> first — red means &le;7 days. Read the <strong>AI summary</strong> and <strong>flags</strong> to inform go/no-bid. Use <strong>recommended actions</strong> as your starting checklist.</p>
    </div>
  </div>

  <div class="cards">
{cards_html}
  </div>

  <div class="report-footer">
    <span>&copy; BidEdge Ltd &middot; Groundwork Procurement Intelligence &middot; Confidential</span>
    <span>Source: GETS (gets.govt.nz) &middot; MBIE Awards Data &middot; Generated {run_date}</span>
  </div>

</body>
</html>
"""

def write_html(watchlist: list[dict], output_dir: Path, run_date: date) -> Path:
    cards = []
    for rank, item in enumerate(watchlist, start=1):
        bidders = _fetch_top_bidders(item["notice_id"])
        cards.append(_notice_card(rank, item, bidders))

    html = _HTML_TEMPLATE.format(
        run_date=run_date.isoformat(),
        notice_count=len(watchlist),
        cards_html="\n".join(cards),
    )

    filename = f"watchlist_{run_date.isoformat()}.html"
    path = output_dir / filename
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info("HTML watchlist written to %s", path)

    import storage as _storage
    import db as _db
    storage_path = f"watchlist/{filename}"
    if not _storage.upload_file(str(path), storage_path, "text/html"):
        logger.warning("Storage upload failed for %s", filename)
    _db.save_output("watchlist_html", run_date, filename,
                    content=html, storage_path=storage_path)

    return path


# ── Main entry point ──────────────────────────────────────────────────────────

def run_output(preferred_sectors: Optional[list[str]] = None) -> Tuple[Path, Path, Path]:
    logger.info(
        "Generating prioritisation output (sectors=%s)",
        ",".join(preferred_sectors) if preferred_sectors else "neutral",
    )
    run_date = date.today()
    output_dir = Path(config.OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)

    watchlist = _fetch_watchlist(preferred_sectors=preferred_sectors)
    logger.info("%d notices in watchlist", len(watchlist))

    json_path = write_json(watchlist, output_dir, run_date)
    md_path   = write_markdown(watchlist, output_dir, run_date)
    html_path = write_html(watchlist, output_dir, run_date)

    return json_path, md_path, html_path


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
    p = argparse.ArgumentParser(description="Generate the daily watchlist output")
    p.add_argument(
        "--sectors",
        help="Comma-separated preferred sectors, e.g. ICT,security. "
             "Omit for sector-neutral ranking (all sectors weighted equally).",
    )
    args = p.parse_args()
    sectors = [s.strip() for s in args.sectors.split(",")] if args.sectors else None
    j, m, h = run_output(preferred_sectors=sectors)
    print(f"JSON: {j}\nMD:   {m}\nHTML: {h}")
